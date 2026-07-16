from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Tuple

MAX_PUBLIC_TEXT_CHARS = 4_096
MAX_FORCE_REASON_CHARS = 256
MAX_TUPLE_ITEMS = 128

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")
_CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[1-9][0-9]{0,8}$")
_PRIVATE_NUMBER_PATTERN = re.compile(r"[0-9]{3,}")
_SECRET_LIKE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_=-]{24,}(?![A-Za-z0-9])")
_PRIVATE_FORCE_REASON_TERMS = (
    "access_token",
    "api_key",
    "authorization",
    "bearer ",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "account",
    "balance",
    "email",
    "phone",
    "amount",
    "portfolio value",
    "position value",
    "order_id",
    "transaction_id",
    "income",
    "debt",
    "reserve",
    " cny",
    " rmb",
    " yuan",
    "\u00a5",
    "\uffe5",
    "$",
    "\u4f59\u989d",
    "\u8d26\u6237",
    "\u6536\u5165",
    "\u8d1f\u503a",
    "\u91d1\u989d",
    "\u8ba2\u5355",
    "\u624b\u673a\u53f7",
    "\u8eab\u4efd\u8bc1",
    "\u94f6\u884c\u5361",
)


class RequestMode(str, Enum):
    RAPID = "rapid"
    DEEP = "deep"


class ActionKind(str, Enum):
    FACT_RESEARCH = "fact_research"
    CONTINUE_HOLDING = "continue_holding"
    REDUCE_TO_CASH = "reduce_to_cash"
    FULL_EXIT = "full_exit"
    BUY_OR_ADD = "buy_or_add"
    SWITCH_FUNDS = "switch_funds"


class RiskEffect(str, Enum):
    INFORMATION = "information"
    RISK_MAINTAINING = "risk_maintaining"
    RISK_REDUCING = "risk_reducing"
    RISK_INCREASING = "risk_increasing"


class SourceFieldState(str, Enum):
    NOT_CHECKED = "not_checked"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class RequestFieldResolution(str, Enum):
    USABLE = "usable"
    PARTIAL = "partial"
    MANUAL_SUPPLEMENT_REQUIRED = "manual_supplement_required"


class ActionMaturity(str, Enum):
    MATURE = "mature"
    EXPERIMENTAL_SHADOW = "experimental_shadow"


class WorkflowLevel(str, Enum):
    RAPID_EVIDENCE = "rapid_evidence"
    DECISION_EVIDENCE = "decision_evidence"


class RequestTerminalStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SourceAttemptOutcome(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CACHE_HIT = "cache_hit"
    SKIPPED_COOLDOWN = "skipped_cooldown"


class SourceTier(str, Enum):
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"
    PRIVATE_OBSERVATION = "private_observation"
    USER_PROVIDED = "user_provided"


class EvidenceCompleteness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class EvidenceFreshness(str, Enum):
    CURRENT = "current"
    DATED_HISTORY = "dated_history"
    STALE = "stale"
    UNKNOWN = "unknown"


class FreshnessKind(str, Enum):
    FIXED_AGE = "fixed_age"
    FORMAL_NAV_CALENDAR = "formal_nav_calendar"
    EFFECTIVE_PERIOD = "effective_period"
    DISCLOSURE_CALENDAR = "disclosure_calendar"
    QUERY_WINDOW = "query_window"
    SAME_TRADING_DAY = "same_trading_day"
    SAME_REQUEST = "same_request"


def validate_exact_dataclass_state(value: object, name: str) -> None:
    state = vars(value)
    expected = {field.name for field in dataclass_fields(type(value))}
    if type(state) is not dict or set(state) != expected:
        raise ValueError(f"{name} has unexpected instance state")


def validate_public_text(
    value: object,
    name: str,
    *,
    maximum_chars: int = MAX_PUBLIC_TEXT_CHARS,
) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty exact string")
    if len(value) > maximum_chars:
        raise ValueError(f"{name} is too long")
    if any(
        ord(character) <= 0x1F
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def validate_identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase public identifier")
    return value


def validate_request_id(value: object) -> str:
    if type(value) is not str or _REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("request id must be lowercase UUID hex without separators")
    return value


def validate_checksum(value: object, name: str) -> str:
    if type(value) is not str or _CHECKSUM_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def validate_version(value: object, name: str) -> str:
    if type(value) is not str or _VERSION_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a positive decimal version")
    return value


def validate_aware_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware exact datetime")
    return value


def validate_identifier_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    if len(value) > MAX_TUPLE_ITEMS:
        raise ValueError(f"{name} has too many items")
    for item in value:
        validate_identifier(item, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates")
    return value


def validate_public_text_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    if len(value) > MAX_TUPLE_ITEMS:
        raise ValueError(f"{name} has too many items")
    for item in value:
        validate_public_text(item, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates")
    return value


def validate_force_reason(value: object) -> str:
    reason = validate_public_text(value, "force reason", maximum_chars=MAX_FORCE_REASON_CHARS)
    lowered = reason.casefold()
    if (
        any(term in lowered for term in _PRIVATE_FORCE_REASON_TERMS)
        or _PRIVATE_NUMBER_PATTERN.search(reason) is not None
        or _SECRET_LIKE_PATTERN.search(reason) is not None
        or "@" in reason
    ):
        raise ValueError("force reason contains secret-bearing or private account data")
    return reason


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("canonical decimals must be finite")
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if type(value) is datetime:
        validate_aware_datetime(value, "canonical datetime")
        return value.astimezone(timezone.utc).isoformat()
    if type(value) is date:
        return value.isoformat()
    if type(value) is Decimal:
        return _canonical_decimal(value)
    if type(value) in {tuple, list}:
        return [canonical_value(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("canonical mappings must use exact string keys")
        return {key: canonical_value(item) for key, item in value.items()}
    if hasattr(value, "to_canonical_dict"):
        return canonical_value(value.to_canonical_dict())
    if type(value) in {str, int, bool} or value is None:
        return value
    raise ValueError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        canonical_value(payload),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@dataclass(frozen=True)
class SourceFieldRef:
    source_id: str
    field_id: str

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source field reference")
        validate_identifier(self.source_id, "source id")
        validate_identifier(self.field_id, "field id")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {"field_id": self.field_id, "source_id": self.source_id}


@dataclass(frozen=True)
class FreshnessContext:
    now: datetime
    request_id: Optional[str] = None
    data_request_id: Optional[str] = None
    latest_expected_data_as_of: Optional[datetime] = None
    effective_period_start: Optional[datetime] = None
    effective_period_end: Optional[datetime] = None
    effective_period_open_ended: Optional[bool] = None
    newer_announcement_check_complete: Optional[bool] = None
    newer_announcement_found: Optional[bool] = None
    newer_announcement_checked_at: Optional[datetime] = None
    next_disclosure_due_at: Optional[datetime] = None
    query_window_start: Optional[datetime] = None
    query_window_end: Optional[datetime] = None
    correction_retraction_check_complete: Optional[bool] = None
    correction_retraction_checked_at: Optional[datetime] = None
    trading_day: Optional[date] = None
    data_trading_day: Optional[date] = None

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "freshness context")
        validate_aware_datetime(self.now, "freshness now")
        for value in (self.request_id, self.data_request_id):
            if value is not None:
                validate_request_id(value)
        for value, name in (
            (self.latest_expected_data_as_of, "latest expected data as of"),
            (self.effective_period_start, "effective period start"),
            (self.effective_period_end, "effective period end"),
            (self.newer_announcement_checked_at, "newer announcement checked at"),
            (self.next_disclosure_due_at, "next disclosure due at"),
            (self.query_window_start, "query window start"),
            (self.query_window_end, "query window end"),
            (self.correction_retraction_checked_at, "correction retraction checked at"),
        ):
            if value is not None:
                validate_aware_datetime(value, name)
        for value, name in (
            (self.effective_period_open_ended, "effective period open ended"),
            (self.newer_announcement_check_complete, "newer announcement check complete"),
            (self.newer_announcement_found, "newer announcement found"),
            (
                self.correction_retraction_check_complete,
                "correction retraction check complete",
            ),
        ):
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be an exact boolean or None")
        if self.query_window_start is not None and self.query_window_end is not None:
            if self.query_window_end < self.query_window_start:
                raise ValueError("query window end cannot precede its start")
        if self.effective_period_start is not None and self.effective_period_end is not None:
            if self.effective_period_end < self.effective_period_start:
                raise ValueError("effective period end cannot precede its start")
        effective_values_present = any(
            value is not None
            for value in (
                self.effective_period_start,
                self.effective_period_end,
                self.effective_period_open_ended,
            )
        )
        if effective_values_present:
            if self.effective_period_start is None or self.effective_period_open_ended is None:
                raise ValueError("effective period context must declare its start and end mode")
            if self.effective_period_open_ended is True and self.effective_period_end is not None:
                raise ValueError("open-ended effective period cannot declare an end")
            if self.effective_period_open_ended is False and self.effective_period_end is None:
                raise ValueError("closed effective period must declare an end")
        for value, name in (
            (self.trading_day, "trading day"),
            (self.data_trading_day, "data trading day"),
        ):
            if value is not None and type(value) is not date:
                raise ValueError(f"{name} must be an exact date or None")


@dataclass(frozen=True)
class FreshnessRule:
    kind: FreshnessKind
    maximum_age_seconds: Optional[int] = None
    dated_history_fallback_seconds: Optional[int] = None
    requires_newer_announcement_check: bool = False
    requires_correction_retraction_check: bool = False

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "freshness rule")
        if type(self.kind) is not FreshnessKind:
            raise ValueError("freshness kind must be an exact FreshnessKind")
        for value, name in (
            (self.maximum_age_seconds, "maximum age seconds"),
            (self.dated_history_fallback_seconds, "dated history fallback seconds"),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive exact integer or None")
        if self.kind is FreshnessKind.FIXED_AGE and self.maximum_age_seconds is None:
            raise ValueError("fixed-age freshness requires a maximum age")
        if (
            self.dated_history_fallback_seconds is not None
            and self.maximum_age_seconds is not None
            and self.dated_history_fallback_seconds < self.maximum_age_seconds
        ):
            raise ValueError("dated-history fallback cannot be shorter than maximum age")
        for value, name in (
            (self.requires_newer_announcement_check, "newer announcement check flag"),
            (self.requires_correction_retraction_check, "correction check flag"),
        ):
            if type(value) is not bool:
                raise ValueError(f"{name} must be an exact boolean")
    def _within_maximum_age(self, data_as_of: datetime, now: datetime) -> bool:
        age_seconds = (now - data_as_of).total_seconds()
        return age_seconds >= 0 and (
            self.maximum_age_seconds is None or age_seconds <= self.maximum_age_seconds
        )

    def is_current(self, data_as_of: Optional[datetime], context: FreshnessContext) -> bool:
        self.validate()
        if type(context) is not FreshnessContext:
            raise ValueError("freshness context must be an exact FreshnessContext")
        context.validate()
        if data_as_of is None:
            return False
        validate_aware_datetime(data_as_of, "data as of")
        if not self._within_maximum_age(data_as_of, context.now):
            return False
        if self.requires_newer_announcement_check and not (
            context.newer_announcement_check_complete is True
            and context.newer_announcement_found is False
            and context.newer_announcement_checked_at is not None
            and data_as_of <= context.newer_announcement_checked_at <= context.now
        ):
            return False
        if self.requires_correction_retraction_check and (
            context.correction_retraction_check_complete is not True
            or context.correction_retraction_checked_at is None
            or not data_as_of <= context.correction_retraction_checked_at <= context.now
        ):
            return False
        if self.kind is FreshnessKind.FIXED_AGE:
            return True
        if self.kind is FreshnessKind.FORMAL_NAV_CALENDAR:
            expected = context.latest_expected_data_as_of
            return expected is not None and data_as_of >= expected
        if self.kind is FreshnessKind.EFFECTIVE_PERIOD:
            start = context.effective_period_start
            end = context.effective_period_end
            if start is None or context.now < start:
                return False
            if end is not None:
                return context.effective_period_open_ended is False and context.now <= end
            return context.effective_period_open_ended is True
        if self.kind is FreshnessKind.DISCLOSURE_CALENDAR:
            due_at = context.next_disclosure_due_at
            return due_at is not None and context.now <= due_at
        if self.kind is FreshnessKind.QUERY_WINDOW:
            start = context.query_window_start
            end = context.query_window_end
            return start is not None and end is not None and start <= data_as_of <= end
        if self.kind is FreshnessKind.SAME_TRADING_DAY:
            return (
                context.trading_day is not None
                and context.data_trading_day is not None
                and context.data_trading_day == context.trading_day
            )
        if self.kind is FreshnessKind.SAME_REQUEST:
            return (
                context.request_id is not None
                and context.data_request_id is not None
                and context.request_id == context.data_request_id
            )
        return False

    def is_usable(self, data_as_of: Optional[datetime], context: FreshnessContext) -> bool:
        if self.is_current(data_as_of, context):
            return True
        fallback = self.dated_history_fallback_seconds
        if fallback is None or data_as_of is None:
            return False
        validate_aware_datetime(data_as_of, "data as of")
        age_seconds = (context.now - data_as_of).total_seconds()
        return 0 <= age_seconds <= fallback

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "dated_history_fallback_seconds": self.dated_history_fallback_seconds,
            "kind": self.kind.value,
            "maximum_age_seconds": self.maximum_age_seconds,
            "requires_correction_retraction_check": (
                self.requires_correction_retraction_check
            ),
            "requires_newer_announcement_check": self.requires_newer_announcement_check,
        }


@dataclass(frozen=True)
class SupplementationRequest:
    missing_item: str
    why_required: str
    suggested_location: str
    accepted_input: Tuple[str, ...]
    freshness_requirement: str
    impact_if_missing: str
    supported_without_it: str
    unsupported_without_it: str

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "supplementation request")
        validate_identifier(self.missing_item, "missing item")
        for value, name in (
            (self.why_required, "why required"),
            (self.suggested_location, "suggested location"),
            (self.freshness_requirement, "freshness requirement"),
            (self.impact_if_missing, "impact if missing"),
            (self.supported_without_it, "supported without it"),
            (self.unsupported_without_it, "unsupported without it"),
        ):
            validate_public_text(value, name)
        validate_public_text_tuple(self.accepted_input, "accepted input", allow_empty=False)

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "accepted_input": list(self.accepted_input),
            "freshness_requirement": self.freshness_requirement,
            "impact_if_missing": self.impact_if_missing,
            "missing_item": self.missing_item,
            "suggested_location": self.suggested_location,
            "supported_without_it": self.supported_without_it,
            "unsupported_without_it": self.unsupported_without_it,
            "why_required": self.why_required,
        }


@dataclass(frozen=True)
class SourceFieldPolicy:
    field_id: str
    source_tier: SourceTier
    freshness: FreshnessRule
    scope: str
    acceptable_alternatives: Tuple[SourceFieldRef, ...]
    supplementation: SupplementationRequest

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source field policy")
        validate_identifier(self.field_id, "field id")
        if type(self.source_tier) is not SourceTier:
            raise ValueError("source tier must be an exact SourceTier")
        if type(self.freshness) is not FreshnessRule:
            raise ValueError("freshness must be an exact FreshnessRule")
        self.freshness.validate()
        validate_public_text(self.scope, "scope")
        if type(self.acceptable_alternatives) is not tuple:
            raise ValueError("acceptable alternatives must be an exact tuple")
        if len(self.acceptable_alternatives) > MAX_TUPLE_ITEMS:
            raise ValueError("acceptable alternatives has too many items")
        for reference in self.acceptable_alternatives:
            if type(reference) is not SourceFieldRef:
                raise ValueError("acceptable alternatives must contain exact references")
            reference.validate()
        if len(self.acceptable_alternatives) != len(set(self.acceptable_alternatives)):
            raise ValueError("acceptable alternatives must not contain duplicates")
        if type(self.supplementation) is not SupplementationRequest:
            raise ValueError("supplementation must be an exact SupplementationRequest")
        self.supplementation.validate()

    def is_current(self, data_as_of: Optional[datetime], context: FreshnessContext) -> bool:
        return self.freshness.is_current(data_as_of, context)

    def is_usable(self, data_as_of: Optional[datetime], context: FreshnessContext) -> bool:
        return self.freshness.is_usable(data_as_of, context)

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "acceptable_alternatives": [
                reference.to_canonical_dict() for reference in self.acceptable_alternatives
            ],
            "field_id": self.field_id,
            "freshness": self.freshness.to_canonical_dict(),
            "scope": self.scope,
            "source_tier": self.source_tier.value,
            "supplementation": self.supplementation.to_canonical_dict(),
        }


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    source_kind: str
    scope: str
    fields: Tuple[SourceFieldPolicy, ...]

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source policy")
        validate_identifier(self.source_id, "source id")
        validate_identifier(self.source_kind, "source kind")
        validate_public_text(self.scope, "source scope")
        if type(self.fields) is not tuple or not self.fields or len(self.fields) > MAX_TUPLE_ITEMS:
            raise ValueError("source fields must be a non-empty bounded tuple")
        field_ids = []
        for field in self.fields:
            if type(field) is not SourceFieldPolicy:
                raise ValueError("source fields must contain exact SourceFieldPolicy records")
            field.validate()
            field_ids.append(field.field_id)
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("source field ids must be unique within a source")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "fields": [field.to_canonical_dict() for field in self.fields],
            "scope": self.scope,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class ConclusionEvidence:
    source_tier: SourceTier
    publishers: Tuple[str, ...]
    source_ids: Tuple[str, ...]
    publication_times: Tuple[datetime, ...]
    market_as_of: Optional[datetime]
    report_as_of: Optional[datetime]
    retrieved_at: datetime
    independent_lineage_count: int
    lineage_ids: Tuple[str, ...]
    completeness: EvidenceCompleteness
    coverage_percent: Optional[Decimal]
    freshness: EvidenceFreshness
    conflicts: Tuple[str, ...]
    inferred: bool
    missing_critical_fields: Tuple[str, ...]

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "conclusion evidence")
        if type(self.source_tier) is not SourceTier:
            raise ValueError("source tier must be an exact SourceTier")
        validate_public_text_tuple(self.publishers, "publishers")
        validate_identifier_tuple(self.source_ids, "source ids")
        if (
            type(self.publication_times) is not tuple
            or len(self.publication_times) > MAX_TUPLE_ITEMS
        ):
            raise ValueError("publication times must be a bounded exact tuple")
        for value in self.publication_times:
            validate_aware_datetime(value, "publication time")
        for value, name in (
            (self.market_as_of, "market as of"),
            (self.report_as_of, "report as of"),
        ):
            if value is not None:
                validate_aware_datetime(value, name)
        validate_aware_datetime(self.retrieved_at, "retrieved at")
        if type(self.independent_lineage_count) is not int or self.independent_lineage_count < 0:
            raise ValueError("independent lineage count must be a non-negative exact integer")
        validate_identifier_tuple(self.lineage_ids, "lineage ids")
        if self.independent_lineage_count != len(self.lineage_ids):
            raise ValueError("independent lineage count must match unique lineage ids")
        if type(self.completeness) is not EvidenceCompleteness:
            raise ValueError("completeness must be an exact EvidenceCompleteness")
        if self.coverage_percent is not None:
            if type(self.coverage_percent) is not Decimal or not self.coverage_percent.is_finite():
                raise ValueError("coverage percent must be a finite Decimal or None")
            if not Decimal("0") <= self.coverage_percent <= Decimal("100"):
                raise ValueError("coverage percent must be between zero and one hundred")
        if type(self.freshness) is not EvidenceFreshness:
            raise ValueError("freshness must be an exact EvidenceFreshness")
        validate_identifier_tuple(self.conflicts, "conflicts")
        if type(self.inferred) is not bool:
            raise ValueError("inferred must be an exact boolean")
        validate_identifier_tuple(self.missing_critical_fields, "missing critical fields")
        requires_complete_provenance = (
            self.source_tier is SourceTier.TIER_1
            or self.completeness is EvidenceCompleteness.COMPLETE
            or self.freshness is EvidenceFreshness.CURRENT
        )
        has_evidence_date = bool(self.publication_times) or any(
            value is not None for value in (self.market_as_of, self.report_as_of)
        )
        if requires_complete_provenance and not (
            self.publishers
            and self.source_ids
            and has_evidence_date
            and self.independent_lineage_count > 0
        ):
            raise ValueError("current, complete, or tier-1 evidence requires full provenance")
        if (
            self.completeness is EvidenceCompleteness.COMPLETE
            and self.missing_critical_fields
        ):
            raise ValueError("complete evidence cannot have missing critical fields")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "completeness": self.completeness.value,
            "conflicts": list(self.conflicts),
            "coverage_percent": canonical_value(self.coverage_percent),
            "freshness": self.freshness.value,
            "independent_lineage_count": self.independent_lineage_count,
            "inferred": self.inferred,
            "lineage_ids": list(self.lineage_ids),
            "market_as_of": canonical_value(self.market_as_of),
            "missing_critical_fields": list(self.missing_critical_fields),
            "publication_times": [canonical_value(item) for item in self.publication_times],
            "publishers": list(self.publishers),
            "report_as_of": canonical_value(self.report_as_of),
            "retrieved_at": canonical_value(self.retrieved_at),
            "source_ids": list(self.source_ids),
            "source_tier": self.source_tier.value,
        }


@dataclass(frozen=True)
class ActionRoute:
    action_id: str
    action: ActionKind
    risk_effect: RiskEffect
    required_gates: Tuple[str, ...]
    blocking_codes: Tuple[str, ...]
    research_available: bool
    exact_amount_available: bool
    minimum_state: str
    action_maturity: ActionMaturity

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "action route")
        validate_identifier(self.action_id, "action id")
        if type(self.action) is not ActionKind:
            raise ValueError("action must be an exact ActionKind")
        if type(self.risk_effect) is not RiskEffect:
            raise ValueError("risk effect must be an exact RiskEffect")
        validate_identifier_tuple(self.required_gates, "required gates")
        validate_identifier_tuple(self.blocking_codes, "blocking codes")
        if type(self.research_available) is not bool:
            raise ValueError("research available must be an exact boolean")
        if type(self.exact_amount_available) is not bool:
            raise ValueError("exact amount available must be an exact boolean")
        validate_identifier(self.minimum_state, "minimum state")
        if type(self.action_maturity) is not ActionMaturity:
            raise ValueError("action maturity must be an exact ActionMaturity")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "action": self.action.value,
            "action_id": self.action_id,
            "action_maturity": self.action_maturity.value,
            "blocking_codes": list(self.blocking_codes),
            "exact_amount_available": self.exact_amount_available,
            "minimum_state": self.minimum_state,
            "required_gates": list(self.required_gates),
            "research_available": self.research_available,
            "risk_effect": self.risk_effect.value,
        }


@dataclass(frozen=True)
class DecisionRoute:
    request_id: str
    mode: RequestMode
    workflow_level: WorkflowLevel
    actions: Tuple[ActionRoute, ...]
    conclusion_evidence: Tuple[ConclusionEvidence, ...]
    opposing_evidence: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    policy_version: str
    policy_checksum: str
    registry_version: str
    registry_checksum: str

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "decision route")
        validate_request_id(self.request_id)
        if type(self.mode) is not RequestMode:
            raise ValueError("mode must be an exact RequestMode")
        if type(self.workflow_level) is not WorkflowLevel:
            raise ValueError("workflow level must be an exact WorkflowLevel")
        if (
            type(self.actions) is not tuple
            or not self.actions
            or len(self.actions) > MAX_TUPLE_ITEMS
        ):
            raise ValueError("actions must be a non-empty bounded tuple")
        action_ids = []
        for action in self.actions:
            if type(action) is not ActionRoute:
                raise ValueError("actions must contain exact ActionRoute records")
            action.validate()
            action_ids.append(action.action_id)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("action ids must be unique")
        if (
            type(self.conclusion_evidence) is not tuple
            or len(self.conclusion_evidence) > MAX_TUPLE_ITEMS
        ):
            raise ValueError("conclusion evidence must be a bounded exact tuple")
        for evidence in self.conclusion_evidence:
            if type(evidence) is not ConclusionEvidence:
                raise ValueError("conclusion evidence contains an invalid record")
            evidence.validate()
        validate_identifier_tuple(self.opposing_evidence, "opposing evidence")
        validate_identifier_tuple(self.missing_fields, "missing fields")
        validate_version(self.policy_version, "policy version")
        validate_checksum(self.policy_checksum, "policy checksum")
        validate_version(self.registry_version, "registry version")
        validate_checksum(self.registry_checksum, "registry checksum")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "actions": [item.to_canonical_dict() for item in self.actions],
            "conclusion_evidence": [
                item.to_canonical_dict() for item in self.conclusion_evidence
            ],
            "missing_fields": list(self.missing_fields),
            "mode": self.mode.value,
            "opposing_evidence": list(self.opposing_evidence),
            "policy_checksum": self.policy_checksum,
            "policy_version": self.policy_version,
            "registry_checksum": self.registry_checksum,
            "registry_version": self.registry_version,
            "request_id": self.request_id,
            "workflow_level": self.workflow_level.value,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


@dataclass(frozen=True)
class SourceAttempt:
    source_id: str
    field_id: str
    subject_key: str
    attempt_number: int
    outcome: SourceAttemptOutcome
    started_at: datetime
    finished_at: datetime
    data_as_of: Optional[datetime]
    error_code: Optional[str]
    cooldown_until: Optional[datetime]
    force_actor: Optional[str]
    force_reason: Optional[str]
    registry_version: str
    registry_checksum: str
    response_bytes: int

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source attempt")
        validate_identifier(self.source_id, "source id")
        validate_identifier(self.field_id, "field id")
        if type(self.subject_key) is not str or _SUBJECT_KEY_PATTERN.fullmatch(
            self.subject_key
        ) is None:
            raise ValueError("subject key must be fund: followed by exactly six digits")
        if type(self.attempt_number) is not int or self.attempt_number not in {1, 2}:
            raise ValueError("attempt number must be exactly 1 or 2")
        if type(self.outcome) is not SourceAttemptOutcome:
            raise ValueError("outcome must be an exact SourceAttemptOutcome")
        validate_aware_datetime(self.started_at, "started at")
        validate_aware_datetime(self.finished_at, "finished at")
        if self.finished_at < self.started_at:
            raise ValueError("finished at cannot precede started at")
        for value, name in (
            (self.data_as_of, "data as of"),
            (self.cooldown_until, "cooldown until"),
        ):
            if value is not None:
                validate_aware_datetime(value, name)
        if self.error_code is not None:
            validate_identifier(self.error_code, "error code")
        if (self.force_actor is None) != (self.force_reason is None):
            raise ValueError("force actor and force reason must be present together")
        if self.force_actor is not None:
            if self.force_actor != "local_owner":
                raise ValueError("force actor must be local_owner")
            validate_force_reason(self.force_reason)
        validate_version(self.registry_version, "registry version")
        validate_checksum(self.registry_checksum, "registry checksum")
        if type(self.response_bytes) is not int or self.response_bytes < 0:
            raise ValueError("response bytes must be a non-negative exact integer")


@dataclass(frozen=True)
class StoredSourceAttempt:
    id: int
    request_run_id: int
    attempt: SourceAttempt

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "stored source attempt")
        for value, name in ((self.id, "attempt id"), (self.request_run_id, "request run id")):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        if type(self.attempt) is not SourceAttempt:
            raise ValueError("attempt must be an exact SourceAttempt")
        self.attempt.validate()
