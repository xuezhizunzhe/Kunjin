from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple, Type
from urllib.parse import urlsplit

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    SourceAttempt,
    SourceAttemptOutcome,
    validate_identifier_tuple,
    validate_request_id,
)
from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundAnnouncement,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundIndustryExposure,
    FundManagerTenure,
    FundShareClass,
    FundSizeObservation,
    SourceDocument,
)
from kunjin.funds.official_domains import FUND_COMPANY_DOMAINS
from kunjin.storage.repository import Repository

_RECORD_TYPES: Dict[str, Tuple[Type[Any], ...]] = {
    DocumentKind.BASIC_PROFILE.value: (FundIdentity, FundShareClass, FundBenchmark),
    DocumentKind.MANAGER_HISTORY.value: (FundManagerTenure,),
    DocumentKind.FEE_SCHEDULE.value: (FundFeeRule,),
    DocumentKind.SIZE_HISTORY.value: (FundSizeObservation,),
    DocumentKind.BENCHMARK.value: (FundBenchmark,),
    DocumentKind.QUARTERLY_HOLDINGS.value: (FundHolding,),
    DocumentKind.INDUSTRY_EXPOSURE.value: (FundIndustryExposure,),
    DocumentKind.ANNOUNCEMENT.value: (FundAnnouncement,),
}

_EXCLUDED_RECORD_KEY_FIELDS = {
    "id",
    "source_document_id",
    "retrieved_at",
    "warnings",
    "conflicts",
}
_MAX_OFFICIAL_LISTING_ITEMS = 1_000


@dataclass(frozen=True)
class OfficialListingRequestContext:
    request_run_id: int
    request_id: str
    fund_code: str
    source_set_complete: bool
    window_complete: bool
    terminal_query_complete: bool
    gap_codes: Tuple[str, ...]
    deadline_at: datetime

    def validate(self) -> None:
        if type(self.request_run_id) is not int or self.request_run_id <= 0:
            raise ValueError("official listing request run id must be positive")
        validate_request_id(self.request_id)
        _official_fund_code(self.fund_code)
        for value, name in (
            (self.source_set_complete, "source set complete"),
            (self.window_complete, "window complete"),
            (self.terminal_query_complete, "terminal query complete"),
        ):
            if type(value) is not bool:
                raise ValueError(f"{name} must be an exact boolean")
        gaps = validate_identifier_tuple(
            self.gap_codes,
            "official listing gap codes",
            allow_empty=True,
        )
        if gaps != tuple(sorted(set(gaps))):
            raise ValueError("official listing gap codes must be sorted and unique")
        _utc_datetime(self.deadline_at, "official listing deadline")


@dataclass(frozen=True)
class StoredFundAnnouncement:
    id: int
    value: FundAnnouncement

    def validate(self) -> None:
        if type(self.id) is not int or self.id <= 0:
            raise ValueError("stored announcement id must be a positive exact integer")
        if type(self.value) is not FundAnnouncement:
            raise ValueError("stored announcement must contain an exact FundAnnouncement")
        self.value.validate()
        if self.value.source_document_id is None:
            raise ValueError("stored announcement requires a source document id")


@dataclass(frozen=True)
class OfficialListingRows:
    rows: Tuple[StoredFundAnnouncement, ...]
    page_evidence: Tuple[SourceDocument, ...]
    truncated: bool
    source_attempt_id: Optional[int] = None

    def validate(self) -> None:
        if type(self.rows) is not tuple or type(self.page_evidence) is not tuple:
            raise ValueError("official listing rows and page evidence must be exact tuples")
        if type(self.truncated) is not bool:
            raise ValueError("official listing truncation must be an exact boolean")
        if self.source_attempt_id is not None and (
            type(self.source_attempt_id) is not int or self.source_attempt_id <= 0
        ):
            raise ValueError("official listing source attempt id must be positive or absent")
        row_ids = []
        for item in self.rows:
            if type(item) is not StoredFundAnnouncement:
                raise ValueError("official listing rows must be exact stored records")
            item.validate()
            row_ids.append(item.id)
        if len(row_ids) != len(set(row_ids)):
            raise ValueError("official listing announcement ids must be unique")
        source_ids = []
        for item in self.page_evidence:
            if type(item) is not SourceDocument:
                raise ValueError("official page evidence must be exact source documents")
            item.validate()
            if item.id is None:
                raise ValueError("official page evidence requires stored ids")
            if (
                item.document_kind is not DocumentKind.ANNOUNCEMENT
                or item.source_name != "fund_manager_official_documents"
                or item.source_tier != 1
            ):
                raise ValueError("official page evidence source binding is invalid")
            _registered_official_publisher(item.url, item.publisher)
            source_ids.append(item.id)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("official page evidence ids must be unique")
        if any(item.value.source_document_id not in source_ids for item in self.rows):
            raise ValueError("official listing row is not bound to returned page evidence")
        pages = {item.id: item for item in self.page_evidence}
        for item in self.rows:
            page = pages[item.value.source_document_id]
            if (
                item.value.fund_code != page.fund_code
                or item.value.source_tier != 1
                or item.value.publisher != page.publisher
            ):
                raise ValueError("official listing row and page binding is invalid")
            _registered_official_publisher(item.value.url, item.value.publisher)


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in value.items()
            if str(key) not in _EXCLUDED_RECORD_KEY_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def make_record_key(record: Any) -> str:
    if not is_dataclass(record):
        raise TypeError("record key requires a dataclass record")
    payload = json.dumps(
        _canonical_value(record),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _optional_date(value: Optional[str]) -> Optional[date]:
    return None if value is None else date.fromisoformat(str(value))


def _optional_datetime(value: Optional[str]) -> Optional[datetime]:
    return None if value is None else datetime.fromisoformat(str(value))


def _official_fund_code(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 6
        or not value.isascii()
        or not value.isdigit()
        or value == "000000"
    ):
        raise ValueError("official listing fund code is invalid")
    return value


def _utc_datetime(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value


def _registered_official_publisher(url: str, publisher: str) -> None:
    host = urlsplit(url).hostname or ""
    if FUND_COMPANY_DOMAINS.get(host) != publisher:
        raise ValueError("official listing publisher does not match the registered manager")


def _section_value(section: Any) -> str:
    try:
        return DocumentKind(section).value
    except ValueError:
        raise ValueError(f"unsupported disclosure section: {section}") from None


class FundDisclosureStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def publish_official_announcement_listing(
        self,
        fund_code: str,
        page_source_documents: Tuple[SourceDocument, ...],
        announcements_by_page: Tuple[Tuple[FundAnnouncement, ...], ...],
        source_attempt: SourceAttempt,
        request_context: OfficialListingRequestContext,
    ) -> OfficialListingRows:
        _official_fund_code(fund_code)
        if type(page_source_documents) is not tuple or not page_source_documents:
            raise ValueError("official page source documents must be a non-empty exact tuple")
        if type(announcements_by_page) is not tuple or (
            len(announcements_by_page) != len(page_source_documents)
        ):
            raise ValueError("official announcement pages must match page source documents")
        if type(source_attempt) is not SourceAttempt:
            raise ValueError("official listing source attempt must be exact")
        if type(request_context) is not OfficialListingRequestContext:
            raise ValueError("official listing request context must be exact")
        source_attempt.validate()
        request_context.validate()
        self._validate_official_listing_inputs(
            fund_code,
            page_source_documents,
            announcements_by_page,
            source_attempt,
            request_context,
        )

        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._authenticate_listing_request(
                    connection, fund_code, source_attempt, request_context
                )
                attempt_cursor = connection.execute(
                    """
                    INSERT INTO source_attempts(
                        request_run_id, source_id, field_id, subject_key,
                        attempt_number, outcome, started_at, finished_at,
                        data_as_of, error_code, cooldown_until, force_actor,
                        force_reason, registry_version, registry_checksum,
                        response_byte_count, authorization_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        request_context.request_run_id,
                        source_attempt.source_id,
                        source_attempt.field_id,
                        source_attempt.subject_key,
                        source_attempt.attempt_number,
                        source_attempt.outcome.value,
                        source_attempt.started_at.isoformat(),
                        source_attempt.finished_at.isoformat(),
                        (
                            None
                            if source_attempt.data_as_of is None
                            else source_attempt.data_as_of.isoformat()
                        ),
                        (
                            None
                            if source_attempt.error_code is None
                            else source_attempt.error_code.value
                        ),
                        (
                            None
                            if source_attempt.cooldown_until is None
                            else source_attempt.cooldown_until.isoformat()
                        ),
                        source_attempt.force_actor,
                        (
                            None
                            if source_attempt.force_reason is None
                            else source_attempt.force_reason.value
                        ),
                        source_attempt.registry_version,
                        source_attempt.registry_checksum,
                        source_attempt.response_bytes,
                    ),
                )
                source_attempt_id = int(attempt_cursor.lastrowid)
                stored_pages = []
                stored_rows = []
                for source, announcements in zip(
                    page_source_documents, announcements_by_page
                ):
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO fund_source_documents(
                            fund_code, document_kind, title, url, source_name,
                            source_tier, publisher, published_at, retrieved_at, checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source.fund_code,
                            source.document_kind.value,
                            source.title,
                            source.url,
                            source.source_name,
                            source.source_tier,
                            source.publisher,
                            None,
                            source.retrieved_at.isoformat(),
                            source.checksum,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT * FROM fund_source_documents
                        WHERE fund_code=? AND document_kind='announcement'
                          AND url=? AND checksum=?
                        """,
                        (fund_code, source.url, source.checksum),
                    ).fetchone()
                    if row is None:
                        raise ValueError("official listing page reload failed")
                    stored_page = self._source_document(row)
                    # The legacy source-document key intentionally deduplicates an
                    # unchanged page by URL and digest. The V22 page manifest keeps
                    # the current request's retrieval time separately.
                    if replace(stored_page, retrieved_at=source.retrieved_at) != replace(
                        source, id=stored_page.id
                    ):
                        raise ValueError("official listing raw digest binding failed")
                    stored_pages.append(stored_page)
                    for announcement in announcements:
                        stored_value = replace(
                            announcement,
                            source_document_id=stored_page.id,
                        )
                        self._insert_record(connection, stored_value)
                        announcement_row = connection.execute(
                            """
                            SELECT * FROM fund_announcements
                            WHERE fund_code=? AND url=? AND source_document_id=?
                            """,
                            (fund_code, announcement.url, stored_page.id),
                        ).fetchone()
                        if announcement_row is None:
                            raise ValueError("official announcement row reload failed")
                        loaded = self._announcement(announcement_row)
                        if loaded != stored_value:
                            raise ValueError("official announcement row binding failed")
                        stored_rows.append(
                            StoredFundAnnouncement(int(announcement_row["id"]), loaded)
                        )
                result = OfficialListingRows(
                    rows=tuple(stored_rows),
                    page_evidence=tuple(stored_pages),
                    truncated=not (
                        request_context.source_set_complete
                        and request_context.window_complete
                        and request_context.terminal_query_complete
                        and not request_context.gap_codes
                    ),
                    source_attempt_id=source_attempt_id,
                )
                result.validate()
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def load_official_announcement_rows_with_ids(
        self,
        fund_code: str,
        window: Tuple[datetime, datetime],
    ) -> OfficialListingRows:
        _official_fund_code(fund_code)
        if type(window) is not tuple or len(window) != 2:
            raise ValueError("official listing window must be an exact two-item tuple")
        window_start = _utc_datetime(window[0], "official listing window start")
        window_end = _utc_datetime(window[1], "official listing window end")
        if window_start >= window_end:
            raise ValueError("official listing window must be half-open and increasing")
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT announcement.*
                FROM fund_announcements AS announcement
                JOIN fund_source_documents AS document
                  ON document.id=announcement.source_document_id
                WHERE announcement.fund_code=?
                  AND announcement.published_at>=?
                  AND announcement.published_at<?
                  AND announcement.source_tier=1
                  AND document.document_kind='announcement'
                  AND document.source_name='fund_manager_official_documents'
                  AND document.source_tier=1
                  AND document.publisher=announcement.publisher
                ORDER BY announcement.published_at DESC, announcement.id
                LIMIT ?
                """,
                (
                    fund_code,
                    window_start.isoformat(),
                    window_end.isoformat(),
                    _MAX_OFFICIAL_LISTING_ITEMS + 1,
                ),
            ).fetchall()
            truncated = len(rows) > _MAX_OFFICIAL_LISTING_ITEMS
            rows = rows[:_MAX_OFFICIAL_LISTING_ITEMS]
            stored_rows = tuple(
                StoredFundAnnouncement(int(row["id"]), self._announcement(row))
                for row in rows
            )
            source_ids = {
                item.value.source_document_id
                for item in stored_rows
                if item.value.source_document_id is not None
            }
            page_evidence = tuple(
                self._load_sources(connection, source_ids)[source_id]
                for source_id in sorted(source_ids)
            )
            result = OfficialListingRows(stored_rows, page_evidence, truncated)
            result.validate()
            return result

    @staticmethod
    def _authenticate_listing_request(
        connection: sqlite3.Connection,
        fund_code: str,
        source_attempt: SourceAttempt,
        request_context: OfficialListingRequestContext,
    ) -> None:
        row = connection.execute(
            "SELECT request_id, mode, status, started_at, deadline_at "
            "FROM request_runs WHERE id=?",
            (request_context.request_run_id,),
        ).fetchone()
        if (
            row is None
            or row["request_id"] != request_context.request_id
            or row["mode"] != "deep"
            or row["status"] != "running"
            or datetime.fromisoformat(str(row["deadline_at"]))
            != request_context.deadline_at
        ):
            raise ValueError("official listing requires the exact running Deep request")
        if (
            request_context.fund_code != fund_code
            or source_attempt.started_at
            < datetime.fromisoformat(str(row["started_at"]))
            or source_attempt.finished_at > request_context.deadline_at
        ):
            raise ValueError("official listing request context binding failed")

    @staticmethod
    def _validate_official_listing_inputs(
        fund_code: str,
        page_source_documents: Tuple[SourceDocument, ...],
        announcements_by_page: Tuple[Tuple[FundAnnouncement, ...], ...],
        source_attempt: SourceAttempt,
        request_context: OfficialListingRequestContext,
    ) -> None:
        if (
            source_attempt.source_id != "fund_manager_official_documents"
            or source_attempt.field_id != "fund_manager_product_announcement"
            or source_attempt.subject_key != f"fund:{fund_code}"
            or source_attempt.attempt_number != 1
            or source_attempt.force_actor is not None
            or source_attempt.force_reason is not None
            or source_attempt.outcome
            not in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
        ):
            raise ValueError("official listing source attempt binding failed")
        if source_attempt.response_bytes <= 0:
            raise ValueError("official listing raw byte evidence is empty")
        if request_context.fund_code != fund_code:
            raise ValueError("official listing request context binding failed")
        if len(page_source_documents) > 10:
            raise ValueError("official listing page bound exceeded")
        page_urls = []
        page_checksums = []
        announcement_urls = []
        item_count = 0
        for source, announcements in zip(
            page_source_documents, announcements_by_page
        ):
            if type(source) is not SourceDocument:
                raise ValueError("official listing page must be an exact SourceDocument")
            source.validate()
            if (
                source.id is not None
                or source.fund_code != fund_code
                or source.document_kind is not DocumentKind.ANNOUNCEMENT
                or source.source_name != "fund_manager_official_documents"
                or source.source_tier != 1
                or source.published_at is not None
            ):
                raise ValueError("official listing page fund or source binding failed")
            _registered_official_publisher(source.url, source.publisher)
            if not source_attempt.started_at <= source.retrieved_at <= source_attempt.finished_at:
                raise ValueError("official listing page retrieval time is outside the attempt")
            if type(announcements) is not tuple:
                raise ValueError("official listing page announcements must be an exact tuple")
            page_urls.append(source.url)
            page_checksums.append(source.checksum)
            item_count += len(announcements)
            for announcement in announcements:
                if type(announcement) is not FundAnnouncement:
                    raise ValueError("official listing row must be an exact FundAnnouncement")
                announcement.validate()
                if (
                    announcement.fund_code != fund_code
                    or announcement.source_document_id is not None
                    or announcement.source_tier != 1
                    or announcement.publisher != source.publisher
                    or announcement.published_at > source.retrieved_at
                ):
                    raise ValueError("official announcement row fund or page binding failed")
                _registered_official_publisher(
                    announcement.url, announcement.publisher
                )
                announcement_urls.append(announcement.url)
        if len(page_urls) != len(set(page_urls)) or len(page_checksums) != len(
            set(page_checksums)
        ):
            raise ValueError("official listing page URL and raw digest must be unique")
        if len(announcement_urls) != len(set(announcement_urls)):
            raise ValueError("official listing announcement URLs must be unique")
        if item_count > _MAX_OFFICIAL_LISTING_ITEMS:
            raise ValueError("official listing item bound exceeded")

    def publish_section(
        self,
        fund_code: str,
        section: Any,
        source: SourceDocument,
        records: Iterable[Any],
        state: str,
        warning: Optional[str] = None,
        *,
        budget: Optional[RequestBudget] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        self._require_budget(budget)
        section_name = _section_value(section)
        if state not in {"success", "not_disclosed"}:
            raise ValueError("publication state must be success or not_disclosed")
        source.validate()
        if source.id is not None:
            raise ValueError("source document must not already be stored")
        if source.fund_code != fund_code:
            raise ValueError("source document fund code does not match publication")
        if source.document_kind.value != section_name:
            raise ValueError("source document kind does not match publication section")

        validated_records = list(records)
        if state == "not_disclosed" and validated_records:
            raise ValueError("not_disclosed publication cannot contain records")
        allowed_types = _RECORD_TYPES[section_name]
        for record in validated_records:
            if not isinstance(record, allowed_types):
                raise ValueError("record type does not match publication section")
            record.validate()
            if (
                isinstance(record, (FundHolding, FundIndustryExposure))
                and record.published_at is None
            ):
                raise ValueError(
                    "holding and industry records require a verified publication date"
                )
            if record.fund_code != fund_code:
                raise ValueError("record fund code does not match publication")
            if record.source_document_id is not None:
                raise ValueError("publication records must not already be stored")

        attempted_at = source.retrieved_at.isoformat()
        owns_connection = connection is None
        if not owns_connection and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        manager = self.repository.connect() if owns_connection else nullcontext(connection)
        with manager as active_connection:
            if owns_connection:
                active_connection.execute("BEGIN IMMEDIATE")
            self._require_budget(budget)
            active_connection.execute(
                """
                INSERT OR IGNORE INTO fund_source_documents(
                    fund_code, document_kind, title, url, source_name, source_tier,
                    publisher, published_at, retrieved_at, checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source.fund_code,
                    source.document_kind.value,
                    source.title,
                    source.url,
                    source.source_name,
                    source.source_tier,
                    source.publisher,
                    None if source.published_at is None else source.published_at.isoformat(),
                    attempted_at,
                    source.checksum,
                ),
            )
            row = active_connection.execute(
                """
                SELECT id FROM fund_source_documents
                WHERE fund_code = ? AND document_kind = ? AND url = ? AND checksum = ?
                """,
                (fund_code, section_name, source.url, source.checksum),
            ).fetchone()
            source_document_id = int(row["id"])
            for record in validated_records:
                self._insert_record(
                    active_connection,
                    replace(record, source_document_id=source_document_id),
                )
                self._require_budget(budget)
            active_connection.execute(
                """
                INSERT INTO fund_section_syncs(
                    fund_code, section, state, current_source_document_id,
                    last_attempted_at, last_success_at, warning, error_code, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(fund_code, section) DO UPDATE SET
                    state = excluded.state,
                    current_source_document_id = excluded.current_source_document_id,
                    last_attempted_at = excluded.last_attempted_at,
                    last_success_at = excluded.last_success_at,
                    warning = excluded.warning,
                    error_code = NULL,
                    error_message = NULL
                """,
                (
                    fund_code,
                    section_name,
                    state,
                    source_document_id,
                    attempted_at,
                    attempted_at,
                    warning,
                ),
            )
            self._require_budget(budget)
            if owns_connection:
                active_connection.commit()
        return source_document_id

    def mark_section_failure(
        self,
        fund_code: str,
        section: Any,
        error_code: str,
        error_message: str,
        attempted_at: datetime,
        *,
        budget: Optional[RequestBudget] = None,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        self._require_budget(budget)
        section_name = _section_value(section)
        if len(fund_code) != 6 or not fund_code.isdigit():
            raise ValueError(f"invalid fund code: {fund_code}")
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("attempted_at must be timezone-aware")
        if not error_code or not error_message:
            raise ValueError("failure code and message are required")
        owns_connection = connection is None
        if not owns_connection and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        manager = self.repository.connect() if owns_connection else nullcontext(connection)
        with manager as active_connection:
            if owns_connection:
                active_connection.execute("BEGIN IMMEDIATE")
            self._require_budget(budget)
            active_connection.execute(
                """
                INSERT INTO fund_section_syncs(
                    fund_code, section, state, current_source_document_id,
                    last_attempted_at, last_success_at, warning, error_code, error_message
                ) VALUES (?, ?, 'source_unavailable', NULL, ?, NULL, NULL, ?, ?)
                ON CONFLICT(fund_code, section) DO UPDATE SET
                    state = 'source_unavailable',
                    last_attempted_at = excluded.last_attempted_at,
                    error_code = excluded.error_code,
                    error_message = excluded.error_message
                """,
                (fund_code, section_name, attempted_at.isoformat(), error_code, error_message),
            )
            self._require_budget(budget)
            if owns_connection:
                active_connection.commit()

    @staticmethod
    def _require_budget(budget: Optional[RequestBudget]) -> None:
        if budget is None:
            return
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget or None")
        if budget.worker_seconds() <= 0.0:
            raise BudgetExpired("request work budget is exhausted")
        budget.require_publishable()

    def section_status(self, fund_code: str) -> Dict[str, Dict[str, Optional[str]]]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT section, state, current_source_document_id, last_attempted_at,
                       last_success_at, warning, error_code, error_message
                FROM fund_section_syncs WHERE fund_code = ? ORDER BY section
                """,
                (fund_code,),
            ).fetchall()
        return {
            str(row["section"]): {
                "state": str(row["state"]),
                "current_source_document_id": (
                    None
                    if row["current_source_document_id"] is None
                    else str(row["current_source_document_id"])
                ),
                "last_attempted_at": str(row["last_attempted_at"]),
                "last_success_at": (
                    None if row["last_success_at"] is None else str(row["last_success_at"])
                ),
                "warning": None if row["warning"] is None else str(row["warning"]),
                "error_code": None if row["error_code"] is None else str(row["error_code"]),
                "error_message": (
                    None if row["error_message"] is None else str(row["error_message"])
                ),
            }
            for row in rows
        }

    def load_bundle(self, fund_code: str) -> DisclosureBundle:
        statuses = self.section_status(fund_code)
        source_ids = {
            int(status["current_source_document_id"])
            for status in statuses.values()
            if status["current_source_document_id"] is not None
        }
        with self.repository.connect() as connection:
            sources = self._load_sources(connection, source_ids)
            identity_rows = self._current_rows(connection, "fund_identities", fund_code)
            identity = None if not identity_rows else self._identity(identity_rows[0])
            bundle = DisclosureBundle(
                fund_code=fund_code,
                identity=identity,
                share_classes=tuple(
                    self._share_class(row)
                    for row in self._current_rows(connection, "fund_share_classes", fund_code)
                ),
                manager_tenures=tuple(
                    self._manager(row)
                    for row in self._current_rows(connection, "fund_manager_tenures", fund_code)
                ),
                fee_rules=tuple(
                    self._fee_rule(row)
                    for row in self._current_rows(connection, "fund_fee_rules", fund_code)
                ),
                sizes=tuple(
                    self._size(row)
                    for row in self._current_rows(connection, "fund_sizes", fund_code)
                ),
                benchmarks=tuple(
                    self._benchmark(row)
                    for row in self._current_rows(connection, "fund_benchmarks", fund_code)
                ),
                holdings=tuple(
                    self._holding(row)
                    for row in self._current_rows(connection, "fund_holdings", fund_code)
                ),
                industry_exposure=tuple(
                    self._industry(row)
                    for row in self._current_rows(connection, "fund_industry_exposure", fund_code)
                ),
                announcements=tuple(
                    self._announcement(row)
                    for row in self._current_rows(connection, "fund_announcements", fund_code)
                ),
                source_documents=sources,
                section_states={
                    section: str(status["state"])
                    for section, status in statuses.items()
                },
                section_statuses=statuses,
                warnings=tuple(
                    str(status["warning"])
                    for status in statuses.values()
                    if status["warning"] is not None
                ),
            )
        bundle.validate()
        return bundle

    @staticmethod
    def _insert_record(connection: Any, record: Any) -> None:
        key = make_record_key(record)
        if isinstance(record, FundIdentity):
            connection.execute(
                """INSERT OR IGNORE INTO fund_identities(
                    fund_code, record_key, fund_name, status, fund_type,
                    established_date, manager_name, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.fund_name, record.status, record.fund_type,
                 None if record.established_date is None else record.established_date.isoformat(),
                 record.manager_name, record.source_document_id),
            )
        elif isinstance(record, FundShareClass):
            connection.execute(
                """INSERT OR IGNORE INTO fund_share_classes(
                    fund_code, record_key, related_fund_code, share_class,
                    fund_name, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.related_fund_code, record.share_class,
                 record.fund_name, record.source_document_id),
            )
        elif isinstance(record, FundManagerTenure):
            connection.execute(
                """INSERT OR IGNORE INTO fund_manager_tenures(
                    fund_code, record_key, manager_name, start_date, end_date, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    record.fund_code,
                    key,
                    record.manager_name,
                    record.start_date.isoformat(),
                    None if record.end_date is None else record.end_date.isoformat(),
                    record.source_document_id,
                ),
            )
        elif isinstance(record, FundFeeRule):
            connection.execute(
                """INSERT OR IGNORE INTO fund_fee_rules(
                    fund_code, record_key, fee_type, share_class, rate, fixed_amount,
                    amount_min, amount_max, holding_days_min, holding_days_max,
                    rule_order, effective_from, effective_to, raw_rule_text, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.fee_type.value, record.share_class,
                 None if record.rate is None else str(record.rate),
                 None if record.fixed_amount is None else str(record.fixed_amount),
                 None if record.amount_min is None else str(record.amount_min),
                 None if record.amount_max is None else str(record.amount_max),
                 record.holding_days_min, record.holding_days_max, record.rule_order,
                 None if record.effective_from is None else record.effective_from.isoformat(),
                 None if record.effective_to is None else record.effective_to.isoformat(),
                 record.raw_rule_text, record.source_document_id),
            )
        elif isinstance(record, FundSizeObservation):
            connection.execute(
                """INSERT OR IGNORE INTO fund_sizes(
                    fund_code, record_key, report_date, net_assets, total_shares,
                    published_at, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.report_date.isoformat(),
                 None if record.net_assets is None else str(record.net_assets),
                 None if record.total_shares is None else str(record.total_shares),
                 None if record.published_at is None else record.published_at.isoformat(),
                 record.source_document_id),
            )
        elif isinstance(record, FundBenchmark):
            connection.execute(
                """INSERT OR IGNORE INTO fund_benchmarks(
                    fund_code, record_key, description, effective_from,
                    effective_to, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.description,
                 None if record.effective_from is None else record.effective_from.isoformat(),
                 None if record.effective_to is None else record.effective_to.isoformat(),
                 record.source_document_id),
            )
        elif isinstance(record, FundHolding):
            connection.execute(
                """INSERT OR IGNORE INTO fund_holdings(
                    fund_code, record_key, report_period, published_at, rank,
                    security_code, security_name, asset_type, weight, disclosure_scope,
                    shares, market_value, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.fund_code,
                    key,
                    record.report_period.isoformat(),
                    record.published_at.isoformat(),
                    record.rank,
                    record.security_code,
                    record.security_name,
                    record.asset_type.value,
                    str(record.weight),
                    record.disclosure_scope,
                    None if record.shares is None else str(record.shares),
                    None if record.market_value is None else str(record.market_value),
                    record.source_document_id,
                ),
            )
        elif isinstance(record, FundIndustryExposure):
            connection.execute(
                """INSERT OR IGNORE INTO fund_industry_exposure(
                    fund_code, record_key, report_period, published_at,
                    classification_standard, industry_name, weight, industry_code,
                    market_value, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.fund_code,
                    key,
                    record.report_period.isoformat(),
                    record.published_at.isoformat(),
                    record.classification_standard,
                    record.industry_name,
                    str(record.weight),
                    record.industry_code,
                    None if record.market_value is None else str(record.market_value),
                    record.source_document_id,
                ),
            )
        elif isinstance(record, FundAnnouncement):
            connection.execute(
                """INSERT OR IGNORE INTO fund_announcements(
                    fund_code, record_key, title, category, publisher, published_at,
                    url, source_tier, source_document_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.fund_code, key, record.title, record.category, record.publisher,
                 record.published_at.isoformat(), record.url, record.source_tier,
                 record.source_document_id),
            )
        else:
            raise TypeError(f"unsupported disclosure record: {type(record).__name__}")

    @staticmethod
    def _current_rows(connection: Any, table: str, fund_code: str) -> Sequence[Any]:
        return connection.execute(
            f"""SELECT facts.* FROM {table} facts
                JOIN fund_section_syncs sync
                  ON sync.fund_code = facts.fund_code
                 AND sync.current_source_document_id = facts.source_document_id
                WHERE facts.fund_code = ? ORDER BY facts.id""",
            (fund_code,),
        ).fetchall()

    @staticmethod
    def _source_document(row: Any) -> SourceDocument:
        return SourceDocument(
            id=int(row["id"]),
            fund_code=str(row["fund_code"]),
            document_kind=DocumentKind(str(row["document_kind"])),
            title=str(row["title"]),
            url=str(row["url"]),
            source_name=str(row["source_name"]),
            source_tier=int(row["source_tier"]),
            publisher=str(row["publisher"]),
            published_at=_optional_datetime(row["published_at"]),
            retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            checksum=str(row["checksum"]),
        )

    @staticmethod
    def _load_sources(connection: Any, source_ids: set) -> Dict[int, SourceDocument]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"SELECT * FROM fund_source_documents WHERE id IN ({placeholders}) ORDER BY id",
            tuple(sorted(source_ids)),
        ).fetchall()
        return {
            int(row["id"]): FundDisclosureStore._source_document(row)
            for row in rows
        }

    @staticmethod
    def _identity(row: Any) -> FundIdentity:
        return FundIdentity(str(row["fund_code"]), str(row["fund_name"]), str(row["status"]),
                            row["fund_type"], _optional_date(row["established_date"]),
                            row["manager_name"], int(row["source_document_id"]))

    @staticmethod
    def _share_class(row: Any) -> FundShareClass:
        return FundShareClass(
            str(row["fund_code"]),
            str(row["related_fund_code"]),
            str(row["share_class"]),
            row["fund_name"],
            int(row["source_document_id"]),
        )

    @staticmethod
    def _manager(row: Any) -> FundManagerTenure:
        return FundManagerTenure(str(row["fund_code"]), str(row["manager_name"]),
                                 date.fromisoformat(str(row["start_date"])),
                                 _optional_date(row["end_date"]), int(row["source_document_id"]))

    @staticmethod
    def _fee_rule(row: Any) -> FundFeeRule:
        return FundFeeRule(
            fund_code=str(row["fund_code"]), fee_type=FeeType(str(row["fee_type"])),
            source_document_id=int(row["source_document_id"]), share_class=row["share_class"],
            rate=_optional_decimal(row["rate"]),
            fixed_amount=_optional_decimal(row["fixed_amount"]),
            amount_min=_optional_decimal(row["amount_min"]),
            amount_max=_optional_decimal(row["amount_max"]),
            holding_days_min=row["holding_days_min"], holding_days_max=row["holding_days_max"],
            rule_order=int(row["rule_order"]), effective_from=_optional_date(row["effective_from"]),
            effective_to=_optional_date(row["effective_to"]),
            raw_rule_text=str(row["raw_rule_text"]),
        )

    @staticmethod
    def _size(row: Any) -> FundSizeObservation:
        return FundSizeObservation(
            str(row["fund_code"]),
            date.fromisoformat(str(row["report_date"])),
            _optional_decimal(row["net_assets"]),
            _optional_decimal(row["total_shares"]),
            _optional_datetime(row["published_at"]),
            int(row["source_document_id"]),
        )

    @staticmethod
    def _benchmark(row: Any) -> FundBenchmark:
        return FundBenchmark(
            str(row["fund_code"]),
            str(row["description"]),
            _optional_date(row["effective_from"]),
            _optional_date(row["effective_to"]),
            int(row["source_document_id"]),
        )

    @staticmethod
    def _holding(row: Any) -> FundHolding:
        return FundHolding(
            fund_code=str(row["fund_code"]),
            report_period=date.fromisoformat(str(row["report_period"])),
            published_at=datetime.fromisoformat(str(row["published_at"])), rank=int(row["rank"]),
            security_code=str(row["security_code"]), security_name=str(row["security_name"]),
            asset_type=AssetType(str(row["asset_type"])), weight=Decimal(str(row["weight"])),
            disclosure_scope=str(row["disclosure_scope"]),
            source_document_id=int(row["source_document_id"]),
            shares=_optional_decimal(row["shares"]),
            market_value=_optional_decimal(row["market_value"]),
        )

    @staticmethod
    def _industry(row: Any) -> FundIndustryExposure:
        return FundIndustryExposure(
            fund_code=str(row["fund_code"]),
            report_period=date.fromisoformat(str(row["report_period"])),
            published_at=datetime.fromisoformat(str(row["published_at"])),
            classification_standard=str(row["classification_standard"]),
            industry_name=str(row["industry_name"]), weight=Decimal(str(row["weight"])),
            source_document_id=int(row["source_document_id"]), industry_code=row["industry_code"],
            market_value=_optional_decimal(row["market_value"]),
        )

    @staticmethod
    def _announcement(row: Any) -> FundAnnouncement:
        return FundAnnouncement(
            fund_code=str(row["fund_code"]), title=str(row["title"]), category=row["category"],
            publisher=str(row["publisher"]),
            published_at=datetime.fromisoformat(str(row["published_at"])),
            url=str(row["url"]), source_tier=int(row["source_tier"]),
            source_document_id=int(row["source_document_id"]),
        )
