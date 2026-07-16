from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple, Type

from kunjin.decision.budget import BudgetExpired, RequestBudget
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


def _section_value(section: Any) -> str:
    try:
        return DocumentKind(section).value
    except ValueError:
        raise ValueError(f"unsupported disclosure section: {section}") from None


class FundDisclosureStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

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
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_budget(budget)
            connection.execute(
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
            row = connection.execute(
                """
                SELECT id FROM fund_source_documents
                WHERE fund_code = ? AND document_kind = ? AND url = ? AND checksum = ?
                """,
                (fund_code, section_name, source.url, source.checksum),
            ).fetchone()
            source_document_id = int(row["id"])
            for record in validated_records:
                self._insert_record(
                    connection,
                    replace(record, source_document_id=source_document_id),
                )
                self._require_budget(budget)
            connection.execute(
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
            connection.commit()
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
    ) -> None:
        self._require_budget(budget)
        section_name = _section_value(section)
        if len(fund_code) != 6 or not fund_code.isdigit():
            raise ValueError(f"invalid fund code: {fund_code}")
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("attempted_at must be timezone-aware")
        if not error_code or not error_message:
            raise ValueError("failure code and message are required")
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_budget(budget)
            connection.execute(
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
            connection.commit()

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
    def _load_sources(connection: Any, source_ids: set) -> Dict[int, SourceDocument]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        rows = connection.execute(
            f"SELECT * FROM fund_source_documents WHERE id IN ({placeholders}) ORDER BY id",
            tuple(sorted(source_ids)),
        ).fetchall()
        return {
            int(row["id"]): SourceDocument(
                id=int(row["id"]), fund_code=str(row["fund_code"]),
                document_kind=DocumentKind(str(row["document_kind"])), title=str(row["title"]),
                url=str(row["url"]), source_name=str(row["source_name"]),
                source_tier=int(row["source_tier"]), publisher=str(row["publisher"]),
                published_at=_optional_datetime(row["published_at"]),
                retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
                checksum=str(row["checksum"]),
            )
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
