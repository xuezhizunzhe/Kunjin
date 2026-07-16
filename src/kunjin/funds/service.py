from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    ForceAuthorization,
    ForceReasonCode,
    FreshnessContext,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldHistory,
    SourceFieldState,
    SourceWorkAuthorization,
    StoredSourceAttempt,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.decision.worker import WorkerExecutionError, run_public_worker
from kunjin.decision.worker_protocol import SCHEMA_VERSION, WorkerRequest, WorkerResponse
from kunjin.funds import parsers
from kunjin.funds.html import FundParseError
from kunjin.funds.models import DisclosureBundle, DocumentKind
from kunjin.funds.parsers import ParsedSection
from kunjin.funds.sources import (
    FundTextClient,
    TextResponse,
    build_disclosure_url,
    build_f10_url,
)
from kunjin.funds.store import FundDisclosureStore

SHANGHAI = ZoneInfo("Asia/Shanghai")
REFERER = "https://fundf10.eastmoney.com/"
FRESHNESS_VALUES = frozenset({"fresh", "stale", "missing", "unknown"})


@dataclass(frozen=True)
class SectionSpec:
    document_kind: DocumentKind
    parser_name: str
    worker_field_id: str
    audit_field_id: str


@dataclass(frozen=True)
class SourceRequestContext:
    request_run_id: int
    budget: RequestBudget
    audit_store: DecisionAuditStore
    health_service: SourceHealthService
    force_reason: Optional[ForceReasonCode] = None

    def __post_init__(self) -> None:
        if type(self) is not SourceRequestContext:
            raise ValueError("request context subclasses are not accepted")
        if type(self.request_run_id) is not int or self.request_run_id <= 0:
            raise ValueError("request run id must be a positive exact integer")
        if type(self.budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        if type(self.audit_store) is not DecisionAuditStore:
            raise ValueError("audit store must be an exact DecisionAuditStore")
        if type(self.health_service) is not SourceHealthService:
            raise ValueError("health service must be an exact SourceHealthService")
        if self.health_service.audit_store is not self.audit_store:
            raise ValueError("health service and request context must share the audit store")
        if self.force_reason is not None and type(self.force_reason) is not ForceReasonCode:
            raise ValueError("force reason must be an exact ForceReasonCode")


@dataclass(frozen=True)
class SectionSyncResult:
    section: str
    status: str
    records: int
    freshness: str
    error_code: Optional[str] = None
    as_of: Optional[str] = None
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None


@dataclass(frozen=True)
class FundDisclosureSyncResult:
    fund_code: str
    sections: Dict[str, SectionSyncResult]
    conflicts: Tuple[str, ...]
    omitted_work: Tuple[str, ...] = ()


class FundDisclosureSyncInterrupted(KeyboardInterrupt):
    def __init__(self, omitted_work: Tuple[str, ...]) -> None:
        self.omitted_work = omitted_work
        super().__init__("bounded fund disclosure synchronization was interrupted")


@dataclass(frozen=True)
class _PendingSectionMutation:
    section_name: str
    spec: SectionSpec
    parsed: Optional[ParsedSection] = None
    failure_code: Optional[str] = None

    def __post_init__(self) -> None:
        if (self.parsed is None) == (self.failure_code is None):
            raise ValueError("section mutation requires exactly one result")


SECTION_SPECS = {
    "basic_profile": SectionSpec(
        DocumentKind.BASIC_PROFILE,
        "parse_basic_profile",
        "basic_profile",
        "identity_active_status",
    ),
    "manager_history": SectionSpec(
        DocumentKind.MANAGER_HISTORY,
        "parse_manager_history",
        "manager_history",
        "current_manager_team",
    ),
    "fee_schedule": SectionSpec(
        DocumentKind.FEE_SCHEDULE,
        "parse_fee_schedule",
        "fee_schedule",
        "fees_share_class_relationship",
    ),
    "size_history": SectionSpec(
        DocumentKind.SIZE_HISTORY,
        "parse_size_history",
        "size_history",
        "identity_active_status",
    ),
    "quarterly_holdings": SectionSpec(
        DocumentKind.QUARTERLY_HOLDINGS,
        "parse_quarterly_holdings",
        "quarterly_holdings",
        "holdings_industries",
    ),
    "industry_exposure": SectionSpec(
        DocumentKind.INDUSTRY_EXPOSURE,
        "parse_industry_exposure",
        "industry_exposure",
        "holdings_industries",
    ),
    "announcements": SectionSpec(
        DocumentKind.ANNOUNCEMENT,
        "parse_announcements",
        "announcement",
        "fund_manager_product_announcement",
    ),
}

PROFILE_SECTIONS = (
    "basic_profile",
    "manager_history",
    "fee_schedule",
    "size_history",
    "announcements",
)
CLASSIFICATION_SECTIONS = ("basic_profile",)
HOLDING_SECTIONS = ("quarterly_holdings", "industry_exposure")
AGE_LIMITS = {
    DocumentKind.BASIC_PROFILE: timedelta(days=30),
    DocumentKind.MANAGER_HISTORY: timedelta(days=7),
    DocumentKind.FEE_SCHEDULE: timedelta(days=30),
    DocumentKind.SIZE_HISTORY: timedelta(days=30),
    DocumentKind.ANNOUNCEMENT: timedelta(hours=24),
}


def expected_report_period(as_of: date) -> date:
    if as_of >= date(as_of.year, 11, 7):
        return date(as_of.year, 9, 30)
    if as_of >= date(as_of.year, 8, 7):
        return date(as_of.year, 6, 30)
    if as_of >= date(as_of.year, 5, 7):
        return date(as_of.year, 3, 31)
    if as_of >= date(as_of.year, 4, 7):
        return date(as_of.year - 1, 12, 31)
    return date(as_of.year - 1, 9, 30)


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return str(code) if code else error.__class__.__name__.casefold()


def announcement_report_period(title: str) -> Optional[date]:
    normalized = "".join(title.split())
    quarter_match = re.search(
        r"(?<!\d)(\d{4})年(?:第)?([一二三四1234])季度报告", normalized
    )
    if quarter_match is not None:
        quarter_values = {"一": 1, "二": 2, "三": 3, "四": 4}
        raw_quarter = quarter_match.group(2)
        quarter = int(raw_quarter) if raw_quarter.isdigit() else quarter_values[raw_quarter]
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
        return date(int(quarter_match.group(1)), *month_day)
    half_year_match = re.search(r"(?<!\d)(\d{4})年(?:半年度|中期)报告", normalized)
    if half_year_match is not None:
        return date(int(half_year_match.group(1)), 6, 30)
    annual_match = re.search(r"(?<!\d)(\d{4})年年度报告", normalized)
    if annual_match is not None:
        return date(int(annual_match.group(1)), 12, 31)
    return None


class FundDisclosureService:
    def __init__(
        self,
        client: FundTextClient,
        store: FundDisclosureStore,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        worker_runner: Callable[[WorkerRequest, RequestBudget], WorkerResponse] = (
            run_public_worker
        ),
    ) -> None:
        self.client = client
        self.store = store
        self.now = now
        self.worker_runner = worker_runner

    def sync_profile(
        self,
        fund_code: str,
        *,
        request_context: Optional[SourceRequestContext] = None,
    ) -> FundDisclosureSyncResult:
        return self._sync(fund_code, PROFILE_SECTIONS, request_context=request_context)

    def sync_classification(
        self,
        fund_code: str,
        *,
        request_context: Optional[SourceRequestContext] = None,
    ) -> FundDisclosureSyncResult:
        return self._sync(
            fund_code,
            CLASSIFICATION_SECTIONS,
            request_context=request_context,
        )

    def sync_holdings(
        self,
        fund_code: str,
        *,
        request_context: Optional[SourceRequestContext] = None,
    ) -> FundDisclosureSyncResult:
        return self._sync(fund_code, HOLDING_SECTIONS, request_context=request_context)

    def sync_all(
        self,
        fund_code: str,
        *,
        request_context: Optional[SourceRequestContext] = None,
    ) -> FundDisclosureSyncResult:
        return self._sync(
            fund_code,
            PROFILE_SECTIONS + HOLDING_SECTIONS,
            request_context=request_context,
        )

    def section_snapshot(self, fund_code: str, section: str) -> SectionSyncResult:
        spec = self._spec(section)
        as_of = self._aware_now()
        bundle = self.store.load_bundle(fund_code)
        return self._result(section, spec, bundle, as_of)

    def _sync(
        self,
        fund_code: str,
        section_names: Tuple[str, ...],
        *,
        request_context: Optional[SourceRequestContext],
    ) -> FundDisclosureSyncResult:
        # Validate before entering the isolated loop; an invalid identifier is a
        # request error, not a remote section failure.
        build_f10_url(DocumentKind.BASIC_PROFILE, fund_code)
        if request_context is not None:
            if type(request_context) is not SourceRequestContext:
                raise ValueError("request context must be an exact SourceRequestContext")
            return self._sync_bounded(fund_code, section_names, request_context)
        conflicts = []
        for section_name in section_names:
            spec = self._spec(section_name)
            try:
                parsed = self._fetch_and_parse(fund_code, spec)
                self.store.publish_section(
                    fund_code,
                    spec.document_kind,
                    parsed.source,
                    parsed.records,
                    parsed.state,
                    warning="; ".join(parsed.warnings) or None,
                )
                conflicts.extend(
                    f"{section_name}:{conflict}" for conflict in parsed.conflicts
                )
            except Exception as error:
                attempted_at = self._aware_now()
                code = _error_code(error)
                self.store.mark_section_failure(
                    fund_code,
                    spec.document_kind,
                    code,
                    str(error),
                    attempted_at,
                )
                if code == "identity_conflict":
                    conflicts.append(f"{section_name}:{code}")

        as_of = self._aware_now()
        bundle = self.store.load_bundle(fund_code)
        return FundDisclosureSyncResult(
            fund_code=fund_code,
            sections={
                section_name: self._result(
                    section_name, self._spec(section_name), bundle, as_of
                )
                for section_name in section_names
            },
            conflicts=tuple(dict.fromkeys(conflicts)),
        )

    def _sync_bounded(
        self,
        fund_code: str,
        section_names: Tuple[str, ...],
        context: SourceRequestContext,
    ) -> FundDisclosureSyncResult:
        groups: Dict[str, List[str]] = {}
        for section_name in section_names:
            spec = self._spec(section_name)
            groups.setdefault(spec.audit_field_id, []).append(section_name)
        conflicts: List[str] = []
        omitted: List[str] = []
        completed: List[str] = []
        subject_key = f"fund:{fund_code}"
        try:
            for audit_field_id, grouped_sections in groups.items():
                if context.budget.cancelled or context.budget.worker_seconds() <= 0.0:
                    if not context.budget.cancelled:
                        context.budget.cancel("request_deadline_reached")
                    omitted.extend(grouped_sections)
                    omitted.append(audit_field_id)
                    continue
                self._sync_bounded_group(
                    fund_code,
                    audit_field_id,
                    tuple(grouped_sections),
                    subject_key,
                    context,
                    conflicts,
                    omitted,
                )
                completed.extend(
                    section
                    for section in grouped_sections
                    if section not in omitted
                )
        except KeyboardInterrupt as error:
            context.budget.cancel("request_cancelled")
            incomplete = tuple(
                section for section in section_names if section not in completed
            )
            omitted.extend(incomplete)
            omitted.extend(self._spec(section).audit_field_id for section in incomplete)
            raise FundDisclosureSyncInterrupted(
                tuple(dict.fromkeys(omitted))
            ) from error
        except SystemExit:
            context.budget.cancel("request_cancelled")
            omitted.extend(section for section in section_names if section not in omitted)
            raise

        as_of = self._aware_now()
        bundle = self.store.load_bundle(fund_code)
        return FundDisclosureSyncResult(
            fund_code=fund_code,
            sections={
                section_name: self._result(
                    section_name, self._spec(section_name), bundle, as_of
                )
                for section_name in section_names
            },
            conflicts=tuple(dict.fromkeys(conflicts)),
            omitted_work=tuple(dict.fromkeys(omitted)),
        )

    def _sync_bounded_group(
        self,
        fund_code: str,
        audit_field_id: str,
        section_names: Tuple[str, ...],
        subject_key: str,
        context: SourceRequestContext,
        conflicts: List[str],
        omitted: List[str],
    ) -> None:
        authorization = None
        if context.force_reason is not None:
            authorization = context.health_service.force_authorization(
                context.budget,
                "eastmoney_f10",
                audit_field_id,
                subject_key,
                context.force_reason,
                request_run_id=context.request_run_id,
                attempt_number=1,
            )
            if authorization is None:
                omitted.extend(section_names)
                omitted.append(audit_field_id)
                return
        else:
            freshness_context = self._health_context(
                fund_code,
                audit_field_id,
                context,
            )
            state, trusted_history = (
                context.health_service.source_field_state_and_history(
                "eastmoney_f10",
                audit_field_id,
                subject_key,
                freshness_context,
                request_run_id=context.request_run_id,
                budget=context.budget,
            )
            )
            cached_data_as_of = next(
                (
                    record.attempt.data_as_of
                    for record in trusted_history.attempts
                    if record.attempt.outcome
                    in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
                    and record.attempt.data_as_of is not None
                ),
                None,
            )
            if (
                state is SourceFieldState.HEALTHY
                and cached_data_as_of is not None
                and self._group_cache_is_current(fund_code, section_names)
            ):
                now = self._audit_now(context)
                self._record_attempt(
                    audit_field_id,
                    subject_key,
                    1,
                    SourceAttemptOutcome.CACHE_HIT,
                    now,
                    now,
                    cached_data_as_of,
                    None,
                    None,
                    0,
                    context,
                    None,
                )
                return
            if state is SourceFieldState.COOLDOWN:
                self._record_cooldown_skip(
                    audit_field_id,
                    subject_key,
                    context,
                    trusted_history,
                )
                omitted.extend(section_names)
                omitted.append(audit_field_id)
                return

        first = self._execute_bounded_attempt(
            fund_code,
            audit_field_id,
            section_names,
            subject_key,
            1,
            context,
            authorization,
            conflicts,
            omitted,
        )
        if first[0] is SourceAttemptOutcome.SUCCESS:
            return
        if first[0] is not SourceAttemptOutcome.TRANSIENT_FAILURE:
            omitted.extend(first[3])
            omitted.append(audit_field_id)
            return
        parent = first[4]
        retry = context.health_service.retry_allowed(
            parent,
            context.budget,
            request_run_id=context.request_run_id,
            minimum_worker_seconds=0.25,
        )
        if retry is None:
            omitted.extend(first[3])
            omitted.append(audit_field_id)
            return
        second = self._execute_bounded_attempt(
            fund_code,
            audit_field_id,
            tuple(item[0] for item in first[5]),
            subject_key,
            2,
            context,
            retry,
            conflicts,
            omitted,
            carried_failures=first[6],
        )
        if second[0] is not SourceAttemptOutcome.SUCCESS:
            omitted.extend(second[3])
            omitted.append(audit_field_id)

    def _execute_bounded_attempt(
        self,
        fund_code: str,
        audit_field_id: str,
        section_names: Tuple[str, ...],
        subject_key: str,
        attempt_number: int,
        context: SourceRequestContext,
        authorization: object,
        conflicts: List[str],
        omitted: List[str],
        *,
        carried_failures: Tuple[
            Tuple[str, SourceAttemptOutcome, SourceErrorCode], ...
        ] = (),
    ) -> Tuple[
        SourceAttemptOutcome,
        Optional[SourceErrorCode],
        int,
        Tuple[str, ...],
        StoredSourceAttempt,
        Tuple[Tuple[str, SourceAttemptOutcome, SourceErrorCode], ...],
        Tuple[Tuple[str, SourceAttemptOutcome, SourceErrorCode], ...],
    ]:
        started_at = self._audit_now(context)
        response_bytes = 0
        data_dates: List[datetime] = []
        failures: List[Tuple[str, SourceAttemptOutcome, SourceErrorCode]] = list(
            carried_failures
        )
        mutations: List[_PendingSectionMutation] = []
        pending_conflicts: List[str] = []
        for index, section_name in enumerate(section_names):
            spec = self._spec(section_name)
            try:
                context.budget.require_publishable()
                response = self.worker_runner(
                    self._worker_request(fund_code, spec, context.budget),
                    context.budget,
                )
                if not response.ok:
                    error_code = SourceErrorCode(response.reason_code)
                    outcome = self._failure_outcome(error_code)
                    failures.append((section_name, outcome, error_code))
                    mutations.append(
                        _PendingSectionMutation(
                            section_name,
                            spec,
                            failure_code=error_code.value,
                        )
                    )
                    continue
                payload = response.payload
                if payload is None:
                    raise ValueError("worker success payload is missing")
                response_bytes += len(payload.text.encode("utf-8"))
                parsed = self._parse_response(
                    fund_code,
                    spec,
                    TextResponse(
                        requested_url=payload.requested_url,
                        final_url=payload.final_url,
                        text=payload.text,
                        retrieved_at=payload.retrieved_at,
                        checksum=payload.checksum,
                        content_type=payload.content_type,
                    ),
                )
                context.budget.require_publishable()
                mutations.append(_PendingSectionMutation(section_name, spec, parsed=parsed))
                pending_conflicts.extend(
                    f"{section_name}:{conflict}" for conflict in parsed.conflicts
                )
                data_dates.append(payload.retrieved_at)
            except BudgetExpired:
                context.budget.cancel("request_deadline_reached")
                failures.append(
                    (
                        section_name,
                        SourceAttemptOutcome.EXPIRED,
                        SourceErrorCode.REQUEST_EXPIRED,
                    )
                )
                omitted.extend(section_names[index:])
                break
            except WorkerExecutionError:
                if context.budget.cancelled:
                    outcome = SourceAttemptOutcome.EXPIRED
                    code = SourceErrorCode.REQUEST_EXPIRED
                    omitted.extend(section_names[index:])
                else:
                    outcome = SourceAttemptOutcome.UNAVAILABLE
                    code = SourceErrorCode.SOURCE_UNAVAILABLE
                failures.append((section_name, outcome, code))
                if not context.budget.cancelled:
                    mutations.append(
                        _PendingSectionMutation(
                            section_name,
                            spec,
                            failure_code=code.value,
                        )
                    )
                else:
                    break
            except FundParseError as error:
                code = (
                    SourceErrorCode.IDENTITY_CONFLICT
                    if error.code == "identity_conflict"
                    else SourceErrorCode.PARSE_FAILURE
                )
                failures.append((section_name, SourceAttemptOutcome.UNAVAILABLE, code))
                mutations.append(
                    _PendingSectionMutation(
                        section_name,
                        spec,
                        failure_code=error.code,
                    )
                )
                if code is SourceErrorCode.IDENTITY_CONFLICT:
                    pending_conflicts.append(f"{section_name}:identity_conflict")
            except (KeyboardInterrupt, SystemExit):
                failures.append(
                    (
                        section_name,
                        SourceAttemptOutcome.CANCELLED,
                        SourceErrorCode.REQUEST_CANCELLED,
                    )
                )
                omitted.extend(section_names[index:])
                context.budget.cancel("request_cancelled")
                self._record_attempt(
                    audit_field_id,
                    subject_key,
                    attempt_number,
                    SourceAttemptOutcome.CANCELLED,
                    started_at,
                    self._audit_now_or_deadline(context),
                    None,
                    SourceErrorCode.REQUEST_CANCELLED,
                    None,
                    response_bytes,
                    context,
                    authorization,
                    (),
                )
                raise
            except (TypeError, ValueError):
                failures.append(
                    (
                        section_name,
                        SourceAttemptOutcome.UNAVAILABLE,
                        SourceErrorCode.VALIDATION_FAILURE,
                    )
                )
                mutations.append(
                    _PendingSectionMutation(
                        section_name,
                        spec,
                        failure_code=SourceErrorCode.VALIDATION_FAILURE.value,
                    )
                )

        outcome, error_code = self._group_outcome(failures)
        finished_at = self._audit_now_or_deadline(context)
        data_as_of = min(data_dates) if outcome is SourceAttemptOutcome.SUCCESS else None
        cooldown_until = (
            context.health_service.cooldown_until(finished_at)
            if outcome is SourceAttemptOutcome.TRANSIENT_FAILURE
            else None
        )
        try:
            stored_attempt = self._record_attempt(
                audit_field_id,
                subject_key,
                attempt_number,
                outcome,
                started_at,
                finished_at,
                data_as_of,
                error_code,
                cooldown_until,
                response_bytes,
                context,
                authorization,
                tuple(mutations),
            )
        except BudgetExpired:
            context.budget.cancel("request_deadline_reached")
            outcome = SourceAttemptOutcome.EXPIRED
            error_code = SourceErrorCode.REQUEST_EXPIRED
            finished_at = self._audit_now_or_deadline(context)
            stored_attempt = self._record_attempt(
                audit_field_id,
                subject_key,
                attempt_number,
                outcome,
                started_at,
                finished_at,
                None,
                error_code,
                None,
                response_bytes,
                context,
                authorization,
                (),
            )
            failures.extend(
                (section_name, outcome, error_code)
                for section_name in section_names
                if section_name not in {item[0] for item in failures}
            )
            omitted.extend(section_names)
        else:
            conflicts.extend(pending_conflicts)
        return (
            outcome,
            error_code,
            response_bytes,
            tuple(item[0] for item in failures),
            stored_attempt,
            tuple(
                item
                for item in failures
                if item[1] is SourceAttemptOutcome.TRANSIENT_FAILURE
            ),
            tuple(
                item
                for item in failures
                if item[1] is not SourceAttemptOutcome.TRANSIENT_FAILURE
            ),
        )

    @staticmethod
    def _group_outcome(
        failures: List[Tuple[str, SourceAttemptOutcome, SourceErrorCode]],
    ) -> Tuple[SourceAttemptOutcome, Optional[SourceErrorCode]]:
        if not failures:
            return SourceAttemptOutcome.SUCCESS, None
        if any(item[1] is SourceAttemptOutcome.CANCELLED for item in failures):
            return SourceAttemptOutcome.CANCELLED, SourceErrorCode.REQUEST_CANCELLED
        if any(item[1] is SourceAttemptOutcome.EXPIRED for item in failures):
            return SourceAttemptOutcome.EXPIRED, SourceErrorCode.REQUEST_EXPIRED
        transient = next(
            (
                item[2]
                for item in failures
                if item[1] is SourceAttemptOutcome.TRANSIENT_FAILURE
            ),
            None,
        )
        if transient is not None:
            return SourceAttemptOutcome.TRANSIENT_FAILURE, transient
        if all(item[1] is SourceAttemptOutcome.UNSUPPORTED for item in failures):
            return SourceAttemptOutcome.UNSUPPORTED, failures[0][2]
        non_transient = next(
            (item[2] for item in failures if item[1] is SourceAttemptOutcome.UNAVAILABLE),
            SourceErrorCode.SOURCE_UNAVAILABLE,
        )
        return SourceAttemptOutcome.UNAVAILABLE, non_transient

    @staticmethod
    def _failure_outcome(error_code: SourceErrorCode) -> SourceAttemptOutcome:
        if error_code in {
            SourceErrorCode.DNS_FAILURE,
            SourceErrorCode.TRANSIENT_NETWORK_FAILURE,
            SourceErrorCode.NETWORK_TIMEOUT,
        }:
            return SourceAttemptOutcome.TRANSIENT_FAILURE
        if error_code in {
            SourceErrorCode.FIELD_UNSUPPORTED,
            SourceErrorCode.SOURCE_CONTRACT_UNSUPPORTED,
            SourceErrorCode.HTTP_NOT_FOUND,
            SourceErrorCode.HTTP_GONE,
        }:
            return SourceAttemptOutcome.UNSUPPORTED
        return SourceAttemptOutcome.UNAVAILABLE

    def _record_attempt(
        self,
        audit_field_id: str,
        subject_key: str,
        attempt_number: int,
        outcome: SourceAttemptOutcome,
        started_at: datetime,
        finished_at: datetime,
        data_as_of: Optional[datetime],
        error_code: Optional[SourceErrorCode],
        cooldown_until: Optional[datetime],
        response_bytes: int,
        context: SourceRequestContext,
        authorization: object,
        mutations: Tuple[_PendingSectionMutation, ...] = (),
    ) -> StoredSourceAttempt:
        registry = SourceRegistryV1()
        force_actor = None
        force_reason = None
        if context.force_reason is not None and attempt_number == 1:
            force_actor = "local_owner"
            force_reason = context.force_reason
        attempt = SourceAttempt(
            source_id="eastmoney_f10",
            field_id=audit_field_id,
            subject_key=subject_key,
            attempt_number=attempt_number,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            data_as_of=data_as_of,
            error_code=error_code,
            cooldown_until=cooldown_until,
            force_actor=force_actor,
            force_reason=force_reason,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=response_bytes,
        )
        attempt.validate()
        if mutations:
            if (
                self.store.repository.database.resolve()
                != context.audit_store.repository.database.resolve()
            ):
                raise ValueError("business and audit stores must share one database")
            with context.audit_store.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for mutation in mutations:
                    if mutation.parsed is not None:
                        parsed = mutation.parsed
                        self.store.publish_section(
                            parsed.source.fund_code,
                            mutation.spec.document_kind,
                            parsed.source,
                            parsed.records,
                            parsed.state,
                            warning="; ".join(parsed.warnings) or None,
                            budget=context.budget,
                            connection=connection,
                        )
                    else:
                        failure_code = mutation.failure_code
                        if failure_code is None:
                            raise ValueError("failure mutation is missing its code")
                        self.store.mark_section_failure(
                            attempt.subject_key.removeprefix("fund:"),
                            mutation.spec.document_kind,
                            failure_code,
                            failure_code,
                            self._aware_now(),
                            budget=context.budget,
                            connection=connection,
                        )
                attempt_id = context.audit_store.record_source_attempt(
                    context.request_run_id,
                    attempt,
                    authorization,
                    connection=connection,
                )
                self.store._require_budget(context.budget)
                connection.commit()
        else:
            attempt_id = context.audit_store.record_source_attempt(
                context.request_run_id,
                attempt,
                authorization,
            )
        authorization_id = None
        if type(authorization) is ForceAuthorization:
            authorization_id = authorization.reservation.id
        elif type(authorization) is SourceWorkAuthorization:
            authorization_id = authorization.id
        elif authorization is not None:
            raise ValueError("authorization must use an exact supported type")
        stored = StoredSourceAttempt(
            id=attempt_id,
            request_run_id=context.request_run_id,
            request_id=context.budget.request_id,
            authorization_id=authorization_id,
            attempt=attempt,
        )
        stored.validate()
        return stored

    def _record_cooldown_skip(
        self,
        audit_field_id: str,
        subject_key: str,
        context: SourceRequestContext,
        history: SourceFieldHistory,
    ) -> None:
        cooldown_until = next(
            (
                item.attempt.cooldown_until
                for item in history.attempts
                if item.attempt.cooldown_until is not None
                and item.attempt.cooldown_until > context.budget.started_at
            ),
            None,
        )
        if cooldown_until is None:
            raise ValueError("cooldown state has no authenticated deadline")
        now = self._audit_now(context)
        self._record_attempt(
            audit_field_id,
            subject_key,
            1,
            SourceAttemptOutcome.SKIPPED_COOLDOWN,
            now,
            now,
            None,
            SourceErrorCode.COOLDOWN_ACTIVE,
            cooldown_until,
            0,
            context,
            None,
        )

    def _health_context(
        self,
        fund_code: str,
        audit_field_id: str,
        context: SourceRequestContext,
    ) -> FreshnessContext:
        now = context.budget.started_at
        values = {"now": now}
        if audit_field_id == "holdings_industries":
            bundle = self.store.load_bundle(fund_code)
            periods = tuple(
                record.report_period
                for record in (*bundle.holdings, *bundle.industry_exposure)
            )
            expected = expected_report_period(now.astimezone(SHANGHAI).date())
            values.update(
                {
                    "next_disclosure_due_at": self._next_disclosure_due(now),
                    "expected_report_period_end": expected,
                    "data_report_period_end": max(periods) if periods else None,
                }
            )
        freshness = FreshnessContext(**values)
        freshness.validate()
        return freshness

    def _group_cache_is_current(
        self,
        fund_code: str,
        section_names: Tuple[str, ...],
    ) -> bool:
        return all(
            snapshot.freshness == "fresh"
            and snapshot.error_code is None
            and snapshot.status in {"success", "not_disclosed"}
            for snapshot in (
                self.section_snapshot(fund_code, section_name)
                for section_name in section_names
            )
        )

    @staticmethod
    def _next_disclosure_due(now: datetime) -> datetime:
        local = now.astimezone(SHANGHAI)
        current = local.date()
        if current >= date(current.year, 11, 7):
            due = date(current.year + 1, 4, 7)
        elif current >= date(current.year, 8, 7):
            due = date(current.year, 11, 7)
        elif current >= date(current.year, 5, 7):
            due = date(current.year, 8, 7)
        elif current >= date(current.year, 4, 7):
            due = date(current.year, 5, 7)
        else:
            due = date(current.year, 4, 7)
        return datetime.combine(due, datetime.min.time(), tzinfo=SHANGHAI).astimezone(
            timezone.utc
        )

    @staticmethod
    def _worker_request(
        fund_code: str,
        spec: SectionSpec,
        budget: RequestBudget,
    ) -> WorkerRequest:
        year = (
            budget.started_at.astimezone(SHANGHAI).year
            if spec.document_kind is DocumentKind.INDUSTRY_EXPOSURE
            else None
        )
        request = WorkerRequest(
            schema_version=SCHEMA_VERSION,
            request_id=budget.request_id,
            source_id="eastmoney_f10",
            field_id=spec.worker_field_id,
            subject_key=f"fund:{fund_code}",
            operation="fund_text_fetch",
            arguments={
                "url": build_disclosure_url(spec.document_kind, fund_code, year=year),
                "referer": REFERER,
            },
        )
        request.validate()
        return request

    def _parse_response(
        self,
        fund_code: str,
        spec: SectionSpec,
        response: TextResponse,
    ) -> ParsedSection:
        parser = getattr(parsers, spec.parser_name)
        if spec.document_kind is DocumentKind.ANNOUNCEMENT:
            identity = self.store.load_bundle(fund_code).identity
            manager_name = "" if identity is None else (identity.manager_name or "")
            return parser(response, fund_code, manager_name)
        parsed = parser(response, fund_code)
        if spec.document_kind in {
            DocumentKind.QUARTERLY_HOLDINGS,
            DocumentKind.INDUSTRY_EXPOSURE,
        }:
            return self._attach_publication_dates(parsed, fund_code)
        return parsed

    @staticmethod
    def _audit_now(context: SourceRequestContext) -> datetime:
        value = context.health_service.wall_clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("health wall clock must be timezone-aware")
        value = value.astimezone(timezone.utc)
        if not context.budget.started_at <= value <= context.budget.deadline_at:
            raise BudgetExpired("audit clock is outside request lifetime")
        return value

    @staticmethod
    def _audit_now_or_deadline(context: SourceRequestContext) -> datetime:
        try:
            return FundDisclosureService._audit_now(context)
        except BudgetExpired:
            return context.budget.deadline_at

    def _fetch_and_parse(
        self, fund_code: str, spec: SectionSpec
    ) -> ParsedSection:
        year = (
            self._aware_now().astimezone(SHANGHAI).year
            if spec.document_kind is DocumentKind.INDUSTRY_EXPOSURE
            else None
        )
        response = self.client.fetch(
            build_disclosure_url(spec.document_kind, fund_code, year=year), REFERER
        )
        return self._parse_response(fund_code, spec, response)

    def _attach_publication_dates(
        self, parsed: ParsedSection, fund_code: str
    ) -> ParsedSection:
        if parsed.state != "success" or not parsed.records:
            return parsed
        announcements = self.store.load_bundle(fund_code).announcements
        publication_dates: Dict[date, datetime] = {}
        for announcement in announcements:
            report_period = announcement_report_period(announcement.title)
            if report_period is not None and report_period not in publication_dates:
                publication_dates[report_period] = announcement.published_at
        enriched = []
        for record in parsed.records:
            if record.published_at is not None:
                enriched.append(record)
                continue
            published_at = publication_dates.get(record.report_period)
            if published_at is None:
                raise FundParseError(
                    "missing_publication_date",
                    "no announcement exactly matches the disclosure report period",
                )
            enriched.append(replace(record, published_at=published_at))
        warnings = tuple(
            warning
            for warning in parsed.warnings
            if warning != "publication_date_requires_announcement_match"
        )
        return replace(parsed, records=tuple(enriched), warnings=warnings)

    def _aware_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("disclosure service clock must be timezone-aware")
        return value

    def _result(
        self,
        section_name: str,
        spec: SectionSpec,
        bundle: DisclosureBundle,
        as_of: datetime,
    ) -> SectionSyncResult:
        status = bundle.section_statuses.get(spec.document_kind.value)
        state = "missing" if status is None else str(status["state"])
        last_success_at = None if status is None else status["last_success_at"]
        last_attempt_at = None if status is None else status["last_attempted_at"]
        freshness = self._freshness(spec.document_kind, bundle, as_of, last_success_at)
        if freshness not in FRESHNESS_VALUES:
            raise ValueError(f"unsupported freshness value: {freshness}")
        return SectionSyncResult(
            section=section_name,
            status=state,
            records=self._record_count(spec.document_kind, bundle),
            freshness=freshness,
            error_code=None if status is None else status["error_code"],
            as_of=as_of.isoformat(),
            last_success_at=last_success_at,
            last_attempt_at=last_attempt_at,
        )

    @staticmethod
    def _freshness(
        kind: DocumentKind,
        bundle: DisclosureBundle,
        as_of: datetime,
        last_success_at: Optional[str],
    ) -> str:
        if last_success_at is None:
            return "missing"
        if kind in {DocumentKind.QUARTERLY_HOLDINGS, DocumentKind.INDUSTRY_EXPOSURE}:
            records = (
                bundle.holdings
                if kind is DocumentKind.QUARTERLY_HOLDINGS
                else bundle.industry_exposure
            )
            if not records:
                return "unknown"
            latest_period = max(record.report_period for record in records)
            expected = expected_report_period(as_of.astimezone(SHANGHAI).date())
            return "fresh" if latest_period >= expected else "stale"
        try:
            successful_at = datetime.fromisoformat(last_success_at)
            if successful_at.tzinfo is None or successful_at.utcoffset() is None:
                return "unknown"
        except (TypeError, ValueError):
            return "unknown"
        age = as_of - successful_at
        return "fresh" if age <= AGE_LIMITS[kind] else "stale"

    @staticmethod
    def _record_count(kind: DocumentKind, bundle: DisclosureBundle) -> int:
        if kind is DocumentKind.BASIC_PROFILE:
            return (
                (1 if bundle.identity is not None else 0)
                + len(bundle.share_classes)
                + len(bundle.benchmarks)
            )
        records = {
            DocumentKind.MANAGER_HISTORY: bundle.manager_tenures,
            DocumentKind.FEE_SCHEDULE: bundle.fee_rules,
            DocumentKind.SIZE_HISTORY: bundle.sizes,
            DocumentKind.QUARTERLY_HOLDINGS: bundle.holdings,
            DocumentKind.INDUSTRY_EXPOSURE: bundle.industry_exposure,
            DocumentKind.ANNOUNCEMENT: bundle.announcements,
        }
        return len(records[kind])

    @staticmethod
    def _spec(section: str) -> SectionSpec:
        try:
            return SECTION_SPECS[section]
        except KeyError:
            raise ValueError(f"unsupported disclosure section: {section}") from None
