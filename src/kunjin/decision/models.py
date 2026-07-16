from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Tuple

MAX_PUBLIC_TEXT_CHARS = 4_096
MAX_TUPLE_ITEMS = 128


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


def validate_public_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty exact string")
    if len(value) > MAX_PUBLIC_TEXT_CHARS:
        raise ValueError(f"{name} is too long")
    if any(
        ord(character) <= 0x1F
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def validate_aware_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware exact datetime")
    return value


def validate_public_tuple(value: object, name: str, *, allow_empty: bool = True) -> Tuple[str, ...]:
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
        return value.isoformat()
    if type(value) is Decimal:
        return _canonical_decimal(value)
    if type(value) is tuple:
        return [canonical_value(item) for item in value]
    if type(value) is list:
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
        for value, name in (
            (self.missing_item, "missing item"),
            (self.why_required, "why required"),
            (self.suggested_location, "suggested location"),
            (self.freshness_requirement, "freshness requirement"),
            (self.impact_if_missing, "impact if missing"),
            (self.supported_without_it, "supported without it"),
            (self.unsupported_without_it, "unsupported without it"),
        ):
            validate_public_text(value, name)
        validate_public_tuple(self.accepted_input, "accepted input", allow_empty=False)

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
    source_tier: str
    maximum_age_seconds: int
    scope: str
    acceptable_alternatives: Tuple[str, ...]
    supplementation: SupplementationRequest
    dated_history_fallback_seconds: Optional[int] = None

    def validate(self) -> None:
        validate_public_text(self.field_id, "field id")
        validate_public_text(self.source_tier, "source tier")
        validate_public_text(self.scope, "scope")
        if type(self.maximum_age_seconds) is not int or self.maximum_age_seconds <= 0:
            raise ValueError("maximum age seconds must be a positive exact integer")
        validate_public_tuple(self.acceptable_alternatives, "acceptable alternatives")
        self.supplementation.validate()
        fallback = self.dated_history_fallback_seconds
        if fallback is not None:
            if type(fallback) is not int or fallback < self.maximum_age_seconds:
                raise ValueError("dated-history fallback must be at least the maximum age")

    def is_current(self, data_as_of: Optional[datetime], now: datetime) -> bool:
        self.validate()
        validate_aware_datetime(now, "now")
        if data_as_of is None:
            return False
        validate_aware_datetime(data_as_of, "data as of")
        age_seconds = (now - data_as_of).total_seconds()
        return 0 <= age_seconds <= self.maximum_age_seconds

    def is_usable(self, data_as_of: Optional[datetime], now: datetime) -> bool:
        if self.is_current(data_as_of, now):
            return True
        fallback = self.dated_history_fallback_seconds
        if fallback is None or data_as_of is None:
            return False
        age_seconds = (now - data_as_of).total_seconds()
        return 0 <= age_seconds <= fallback

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "acceptable_alternatives": list(self.acceptable_alternatives),
            "dated_history_fallback_seconds": self.dated_history_fallback_seconds,
            "field_id": self.field_id,
            "maximum_age_seconds": self.maximum_age_seconds,
            "scope": self.scope,
            "source_tier": self.source_tier,
            "supplementation": self.supplementation.to_canonical_dict(),
        }


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    source_kind: str
    scope: str
    fields: Tuple[SourceFieldPolicy, ...]

    def validate(self) -> None:
        validate_public_text(self.source_id, "source id")
        validate_public_text(self.source_kind, "source kind")
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
    source_tier: str
    publishers: Tuple[str, ...]
    source_ids: Tuple[str, ...]
    publication_times: Tuple[datetime, ...]
    market_as_of: Optional[datetime]
    report_as_of: Optional[datetime]
    retrieved_at: datetime
    independent_lineage_count: int
    lineage_ids: Tuple[str, ...]
    completeness: str
    coverage_percent: Optional[Decimal]
    freshness: str
    conflicts: Tuple[str, ...]
    inferred: bool
    missing_critical_fields: Tuple[str, ...]

    def validate(self) -> None:
        validate_public_text(self.source_tier, "source tier")
        validate_public_tuple(self.publishers, "publishers")
        validate_public_tuple(self.source_ids, "source ids")
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
        validate_public_tuple(self.lineage_ids, "lineage ids")
        if self.independent_lineage_count != len(self.lineage_ids):
            raise ValueError("independent lineage count must match unique lineage ids")
        validate_public_text(self.completeness, "completeness")
        if self.coverage_percent is not None:
            if type(self.coverage_percent) is not Decimal or not self.coverage_percent.is_finite():
                raise ValueError("coverage percent must be a finite Decimal or None")
            if not Decimal("0") <= self.coverage_percent <= Decimal("100"):
                raise ValueError("coverage percent must be between zero and one hundred")
        validate_public_text(self.freshness, "freshness")
        validate_public_tuple(self.conflicts, "conflicts")
        if type(self.inferred) is not bool:
            raise ValueError("inferred must be an exact boolean")
        validate_public_tuple(self.missing_critical_fields, "missing critical fields")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "completeness": self.completeness,
            "conflicts": list(self.conflicts),
            "coverage_percent": canonical_value(self.coverage_percent),
            "freshness": self.freshness,
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
            "source_tier": self.source_tier,
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
        validate_public_text(self.action_id, "action id")
        if type(self.action) is not ActionKind:
            raise ValueError("action must be an exact ActionKind")
        if type(self.risk_effect) is not RiskEffect:
            raise ValueError("risk effect must be an exact RiskEffect")
        validate_public_tuple(self.required_gates, "required gates")
        validate_public_tuple(self.blocking_codes, "blocking codes")
        if type(self.research_available) is not bool:
            raise ValueError("research available must be an exact boolean")
        if type(self.exact_amount_available) is not bool:
            raise ValueError("exact amount available must be an exact boolean")
        validate_public_text(self.minimum_state, "minimum state")
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
        validate_public_text(self.request_id, "request id")
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
        if type(self.conclusion_evidence) is not tuple:
            raise ValueError("conclusion evidence must be an exact tuple")
        for evidence in self.conclusion_evidence:
            if type(evidence) is not ConclusionEvidence:
                raise ValueError("conclusion evidence contains an invalid record")
            evidence.validate()
        validate_public_tuple(self.opposing_evidence, "opposing evidence")
        validate_public_tuple(self.missing_fields, "missing fields")
        for value, name in (
            (self.policy_version, "policy version"),
            (self.registry_version, "registry version"),
        ):
            validate_public_text(value, name)
        for value, name in (
            (self.policy_checksum, "policy checksum"),
            (self.registry_checksum, "registry checksum"),
        ):
            if (
                type(value) is not str
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")

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
        return canonical_json_bytes(self.to_canonical_dict())

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
        for value, name in (
            (self.source_id, "source id"),
            (self.field_id, "field id"),
            (self.subject_key, "subject key"),
            (self.registry_version, "registry version"),
        ):
            validate_public_text(value, name)
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
        for value, name in (
            (self.error_code, "error code"),
            (self.force_actor, "force actor"),
            (self.force_reason, "force reason"),
        ):
            if value is not None:
                validate_public_text(value, name)
        if (self.force_actor is None) != (self.force_reason is None):
            raise ValueError("force actor and force reason must be present together")
        if type(self.registry_checksum) is not str or len(self.registry_checksum) != 64 or any(
            c not in "0123456789abcdef" for c in self.registry_checksum
        ):
            raise ValueError("registry checksum must be a lowercase SHA-256 digest")
        if type(self.response_bytes) is not int or self.response_bytes < 0:
            raise ValueError("response bytes must be a non-negative exact integer")


@dataclass(frozen=True)
class StoredSourceAttempt:
    id: int
    request_run_id: int
    attempt: SourceAttempt

    def validate(self) -> None:
        for value, name in ((self.id, "attempt id"), (self.request_run_id, "request run id")):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        if type(self.attempt) is not SourceAttempt:
            raise ValueError("attempt must be an exact SourceAttempt")
        self.attempt.validate()
