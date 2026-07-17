from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from kunjin.decision.models import (
    MAX_PUBLIC_TEXT_CHARS,
    MAX_TUPLE_ITEMS,
    ActionMaturity,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestFieldResolution,
    RequestMode,
    SourceFieldState,
    SourceTier,
    canonical_decimal,
    canonical_json_bytes,
    canonical_value,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
    validate_public_text_tuple,
)
from kunjin.models import InvestmentThesis

_FUND_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_PRIVATE_PATH_TOKENS = frozenset(
    (
        "amount",
        "ciphertext",
        "cost",
        "credential",
        "debt",
        "income",
        "nonce",
        "private",
        "profit",
        "reserve",
        "shares",
        "token",
    )
)
_PRIVATE_PATH_COMPOUNDS = frozenset(
    (
        "asset",
        "assets",
        "current_value",
        "local_path",
        "loss_budget",
        "managed_path",
        "position_value",
        "portfolio_weight",
        "purchase_lots",
        "raw_body",
        "response_body",
        "total_asset",
        "owner_weight",
    )
)
_PUBLIC_ASSET_PATH_ALLOWLIST = frozenset(
    (
        "asset_class",
        "candidate_asset_coverage",
    )
)
_MAX_PUBLIC_TREE_DEPTH = 12
_MAX_PUBLIC_MAP_ITEMS = 128
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


class BriefState(str, Enum):
    NO_ADD = "no_add"
    HOLD = "hold"
    WATCH = "watch"
    REDUCE_OR_EXIT_REVIEW = "reduce_or_exit_review"
    ABSTAIN = "abstain"


class BriefEvidenceState(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class BriefEvidenceStatus:
    state: BriefEvidenceState
    required_fields: Tuple[str, ...]
    obtained_fields: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    stale_fields: Tuple[str, ...]
    conflicted_fields: Tuple[str, ...]
    unsupported_fields: Tuple[str, ...]
    cooldown_fields: Tuple[str, ...]
    supported_interpretations: Tuple[str, ...]
    unsupported_interpretations: Tuple[str, ...]
    acceptable_alternative_ids: Tuple[str, ...]
    manual_supplementation_codes: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, BriefEvidenceStatus, "brief evidence status")
        if type(self.state) is not BriefEvidenceState:
            raise ValueError("brief evidence status state must be exact")
        for values, name in (
            (self.required_fields, "required fields"),
            (self.obtained_fields, "obtained fields"),
            (self.missing_fields, "missing fields"),
            (self.stale_fields, "stale fields"),
            (self.conflicted_fields, "conflicted fields"),
            (self.unsupported_fields, "unsupported fields"),
            (self.cooldown_fields, "cooldown fields"),
            (self.supported_interpretations, "supported interpretations"),
            (self.unsupported_interpretations, "unsupported interpretations"),
            (self.acceptable_alternative_ids, "acceptable alternative ids"),
            (self.manual_supplementation_codes, "manual supplementation codes"),
        ):
            validate_identifier_tuple(values, f"brief evidence status {name}")
        if set(self.supported_interpretations).intersection(self.unsupported_interpretations):
            raise ValueError("brief evidence interpretation states must not overlap")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "acceptable_alternative_ids": list(self.acceptable_alternative_ids),
            "conflicted_fields": list(self.conflicted_fields),
            "cooldown_fields": list(self.cooldown_fields),
            "manual_supplementation_codes": list(self.manual_supplementation_codes),
            "missing_fields": list(self.missing_fields),
            "obtained_fields": list(self.obtained_fields),
            "required_fields": list(self.required_fields),
            "stale_fields": list(self.stale_fields),
            "state": self.state.value,
            "supported_interpretations": list(self.supported_interpretations),
            "unsupported_fields": list(self.unsupported_fields),
            "unsupported_interpretations": list(self.unsupported_interpretations),
        }


class OfficialEventCode(str, Enum):
    FUND_LIQUIDATION_NOTICE = "fund_liquidation_notice"
    FUND_TERMINATION_NOTICE = "fund_termination_notice"
    MANAGER_CHANGE_NOTICE = "manager_change_notice"
    SUBSCRIPTION_SUSPENSION_NOTICE = "subscription_suspension_notice"
    REDEMPTION_RESTRICTION_NOTICE = "redemption_restriction_notice"
    FEE_CHANGE_NOTICE = "fee_change_notice"
    BENCHMARK_CHANGE_NOTICE = "benchmark_change_notice"
    OTHER_OFFICIAL_PRODUCT_NOTICE = "other_official_product_notice"


_OFFICIAL_EVENT_ACTIONS = {
    OfficialEventCode.FUND_LIQUIDATION_NOTICE: frozenset(
        {
            "fact_research",
            "continue_holding",
            "reduce_to_cash",
            "full_exit",
            "switch_reduce",
        }
    ),
    OfficialEventCode.FUND_TERMINATION_NOTICE: frozenset(
        {
            "fact_research",
            "continue_holding",
            "reduce_to_cash",
            "full_exit",
            "switch_reduce",
        }
    ),
    OfficialEventCode.MANAGER_CHANGE_NOTICE: frozenset(
        {"fact_research", "continue_holding", "switch_buy"}
    ),
    OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE: frozenset(
        {"fact_research", "continue_holding", "switch_buy"}
    ),
    OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE: frozenset(
        {"fact_research", "reduce_to_cash", "full_exit", "switch_reduce"}
    ),
    OfficialEventCode.FEE_CHANGE_NOTICE: frozenset(
        {
            "fact_research",
            "continue_holding",
            "reduce_to_cash",
            "full_exit",
            "switch_reduce",
            "switch_buy",
        }
    ),
    OfficialEventCode.BENCHMARK_CHANGE_NOTICE: frozenset(
        {"fact_research", "continue_holding", "switch_buy"}
    ),
    OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE: frozenset({"fact_research"}),
}


def canonical_event_affected_actions(
    event_code: OfficialEventCode,
    action_ids: Tuple[str, ...],
) -> Tuple[str, ...]:
    if type(event_code) is not OfficialEventCode:
        raise ValueError("event code must be an exact OfficialEventCode")
    validate_identifier_tuple(action_ids, "event action ids", allow_empty=False)
    return tuple(
        action_id for action_id in action_ids if action_id in _OFFICIAL_EVENT_ACTIONS[event_code]
    )


def thesis_record_fingerprint(thesis_id: int, thesis: InvestmentThesis) -> str:
    if type(thesis_id) is not int or thesis_id <= 0:
        raise ValueError("thesis id must be a positive integer")
    if type(thesis) is not InvestmentThesis:
        raise ValueError("thesis must be an exact InvestmentThesis")
    thesis.validate()
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "active": thesis.active,
                "created_at": thesis.created_at,
                "fund_code": thesis.fund_code,
                "horizon": thesis.horizon,
                "invalidation": thesis.invalidation,
                "rationale": thesis.rationale,
                "thesis_id": thesis_id,
            }
        )
    ).hexdigest()


def _validate_exact_record(value: object, expected_type: type, name: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be an exact {expected_type.__name__}")
    validate_exact_dataclass_state(value, name)


def _validate_fund_code(value: object, name: str = "fund code") -> str:
    if type(value) is not str or _FUND_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be exactly six ASCII digits")
    return value


def _validate_fund_code_tuple(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    if type(value) is not tuple or len(value) > MAX_TUPLE_ITEMS:
        raise ValueError(f"{name} must be a bounded exact tuple")
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    for item in value:
        _validate_fund_code(item, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates")
    return value


def _validate_utc_datetime(value: object, name: str) -> datetime:
    validated = validate_aware_datetime(value, name)
    if validated.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return validated


def _validate_optional_utc(value: object, name: str) -> None:
    if value is not None:
        _validate_utc_datetime(value, name)


def _validate_https_url(value: object, name: str) -> str:
    error = f"{name} must be a canonical public HTTPS URL"
    if type(value) is not str or not value or len(value) > MAX_PUBLIC_TEXT_CHARS:
        raise ValueError(error)
    if any(
        ord(character) <= 0x1F or ord(character) == 0x7F or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise ValueError(error)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    hostname = parsed.hostname
    if (
        not value.startswith("https://")
        or parsed.scheme != "https"
        or not hostname
        or hostname != hostname.lower()
        or not hostname.isascii()
        or parsed.netloc != hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or bool(parsed.params)
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError(error)
    return value


def _normalized_path_tokens(value: str) -> Tuple[str, ...]:
    return tuple(part for part in re.split(r"[^a-z0-9]+", value.casefold()) if part)


def _is_private_path(value: str) -> bool:
    tokens = _normalized_path_tokens(value)
    token_set = set(tokens)
    joined = "_".join(tokens)
    return bool(
        _PRIVATE_PATH_TOKENS.intersection(tokens)
        or joined in _PRIVATE_PATH_COMPOUNDS
        or (
            {"asset", "assets"}.intersection(token_set)
            and joined not in _PUBLIC_ASSET_PATH_ALLOWLIST
        )
        or ("value" in token_set and {"current", "position"}.intersection(token_set))
        or ("weight" in token_set and {"owner", "portfolio", "position"}.intersection(token_set))
        or ("path" in token_set and {"local", "managed"}.intersection(token_set))
        or ("body" in token_set and {"raw", "response"}.intersection(token_set))
        or {"purchase", "lots"}.issubset(token_set)
        or {"total", "asset"}.issubset(token_set)
    )


def _freeze_public_tree(value: object) -> object:
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        return MappingProxyType({key: _freeze_public_tree(item) for key, item in value.items()})
    if type(value) is tuple:
        return tuple(_freeze_public_tree(item) for item in value)
    return value


def _canonical_public_tree(value: object) -> object:
    if type(value) is _MAPPING_PROXY_TYPE:
        return {key: _canonical_public_tree(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_canonical_public_tree(item) for item in value]
    return canonical_value(value)


def _validate_public_tree(value: object, path: str, *, depth: int = 0) -> None:
    if depth > _MAX_PUBLIC_TREE_DEPTH:
        raise ValueError(f"{path} exceeds the public tree depth limit")
    if type(value) is Decimal:
        raise ValueError(f"{path} contains Decimal, which persisted brief records forbid")
    if type(value) is float:
        raise ValueError(f"{path} contains an unsupported float")
    if isinstance(value, Enum):
        raise ValueError(f"{path} contains an unsupported Enum")
    if type(value) is datetime:
        _validate_utc_datetime(value, path)
        return
    if type(value) is date:
        return
    if type(value) is _MAPPING_PROXY_TYPE:
        if len(value) > _MAX_PUBLIC_MAP_ITEMS:
            raise ValueError(f"{path} has too many mapping items")
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"{path} mapping keys must be exact strings")
            if _is_private_path(key):
                raise ValueError(f"{path}.{key} is a private path")
            validate_identifier(key, f"{path} key")
            _validate_public_tree(item, f"{path}.{key}", depth=depth + 1)
        return
    if type(value) is tuple:
        if len(value) > MAX_TUPLE_ITEMS:
            raise ValueError(f"{path} has too many tuple items")
        for index, item in enumerate(value):
            _validate_public_tree(item, f"{path}[{index}]", depth=depth + 1)
        return
    if type(value) is str:
        validate_public_text(value, path)
        return
    if type(value) is bool or value is None:
        return
    if type(value) is int:
        raise ValueError(f"{path} contains an unsupported int")
    raise ValueError(f"{path} contains unsupported {type(value).__name__}")


def _validate_record_tuple(
    value: object,
    record_type: type,
    name: str,
    id_field: str,
    maximum: int,
) -> Tuple[object, ...]:
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded exact tuple")
    ids = []
    for item in value:
        if type(item) is not record_type:
            raise ValueError(f"{name} must contain exact {record_type.__name__} records")
        item.validate()
        ids.append(getattr(item, id_field))
    if len(ids) != len(set(ids)):
        id_name = id_field.replace("_", " ")
        prefix = name[:-1]
        if id_name not in {"fact id", "event id", "relationship id"}:
            prefix = f"{prefix} {id_name.rsplit(' ', 1)[0]}".rstrip()
        raise ValueError(f"{prefix} ids must not contain duplicates")
    return value


@dataclass(frozen=True)
class BriefFact:
    fact_id: str
    field_id: str
    value: object
    unit: Optional[str]
    data_as_of: Optional[datetime]
    published_at: Optional[datetime]
    retrieved_at: datetime
    source_id: str
    source_tier: SourceTier
    publisher: str
    canonical_url: str
    freshness: EvidenceFreshness
    completeness: EvidenceCompleteness
    conflict_ids: Tuple[str, ...]
    calculated: bool
    source_lineage_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _freeze_public_tree(self.value))

    def validate(self) -> None:
        _validate_exact_record(self, BriefFact, "brief fact")
        validate_identifier(self.fact_id, "fact id")
        validate_identifier(self.field_id, "field id")
        _validate_public_tree(self.value, "fact.value")
        if self.unit is not None:
            validate_identifier(self.unit, "fact unit")
        _validate_optional_utc(self.data_as_of, "fact data as of")
        _validate_optional_utc(self.published_at, "fact publication time")
        _validate_utc_datetime(self.retrieved_at, "fact retrieval time")
        if self.data_as_of is not None and self.data_as_of > self.retrieved_at:
            raise ValueError("fact data time cannot follow retrieval time")
        if self.published_at is not None and self.published_at > self.retrieved_at:
            raise ValueError("fact publication time cannot follow retrieval time")
        validate_identifier(self.source_id, "fact source id")
        if type(self.source_tier) is not SourceTier:
            raise ValueError("fact source tier must be an exact SourceTier")
        validate_public_text(self.publisher, "fact publisher")
        _validate_https_url(self.canonical_url, "fact canonical URL")
        if type(self.freshness) is not EvidenceFreshness:
            raise ValueError("fact freshness must be an exact EvidenceFreshness")
        if type(self.completeness) is not EvidenceCompleteness:
            raise ValueError("fact completeness must be an exact EvidenceCompleteness")
        validate_identifier_tuple(self.conflict_ids, "fact conflict ids")
        if type(self.calculated) is not bool:
            raise ValueError("fact calculated flag must be an exact boolean")
        validate_identifier(self.source_lineage_id, "fact source lineage id")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "calculated": self.calculated,
            "canonical_url": self.canonical_url,
            "completeness": self.completeness.value,
            "conflict_ids": list(self.conflict_ids),
            "data_as_of": canonical_value(self.data_as_of),
            "fact_id": self.fact_id,
            "field_id": self.field_id,
            "freshness": self.freshness.value,
            "published_at": canonical_value(self.published_at),
            "publisher": self.publisher,
            "retrieved_at": canonical_value(self.retrieved_at),
            "source_id": self.source_id,
            "source_lineage_id": self.source_lineage_id,
            "source_tier": self.source_tier.value,
            "unit": self.unit,
            "value": _canonical_public_tree(self.value),
        }


@dataclass(frozen=True)
class OfficialEvent:
    event_id: str
    event_code: OfficialEventCode
    title: str
    summary: str
    publisher: str
    canonical_url: str
    published_at: datetime
    retrieved_at: datetime
    source_tier: SourceTier
    original_source_id: str
    quoted_source_id: Optional[str]
    content_fingerprint: str
    integrity_status: str
    affected_action_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, OfficialEvent, "official event")
        validate_identifier(self.event_id, "event id")
        if type(self.event_code) is not OfficialEventCode:
            raise ValueError("event code must be an exact OfficialEventCode")
        validate_public_text(self.title, "event title")
        validate_public_text(self.summary, "event summary")
        validate_public_text(self.publisher, "event publisher")
        _validate_https_url(self.canonical_url, "event canonical URL")
        _validate_utc_datetime(self.published_at, "event publication time")
        _validate_utc_datetime(self.retrieved_at, "event retrieval time")
        if self.published_at > self.retrieved_at:
            raise ValueError("event publication time cannot follow retrieval time")
        if self.source_tier is not SourceTier.TIER_1:
            raise ValueError("official event source tier must be exact SourceTier.TIER_1")
        validate_identifier(self.original_source_id, "event original source id")
        if self.quoted_source_id is not None:
            validate_identifier(self.quoted_source_id, "event quoted source id")
        validate_checksum(self.content_fingerprint, "event content fingerprint")
        validate_identifier(self.integrity_status, "event integrity status")
        if self.integrity_status not in {"active", "corrected", "retracted"}:
            raise ValueError("event integrity status is not supported")
        validate_identifier_tuple(
            self.affected_action_ids,
            "event affected action ids",
            allow_empty=False,
        )

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "affected_action_ids": list(self.affected_action_ids),
            "canonical_url": self.canonical_url,
            "content_fingerprint": self.content_fingerprint,
            "event_code": self.event_code.value,
            "event_id": self.event_id,
            "integrity_status": self.integrity_status,
            "original_source_id": self.original_source_id,
            "published_at": canonical_value(self.published_at),
            "publisher": self.publisher,
            "quoted_source_id": self.quoted_source_id,
            "retrieved_at": canonical_value(self.retrieved_at),
            "source_tier": self.source_tier.value,
            "summary": self.summary,
            "title": self.title,
        }


@dataclass(frozen=True)
class RelationshipEvidence:
    relationship_id: str
    relationship_type: str
    fund_codes: Tuple[str, ...]
    evidence_state: BriefEvidenceState
    metrics: object
    evidence_ids: Tuple[str, ...]
    report_periods: Tuple[date, ...]
    publication_times: Tuple[datetime, ...]
    warnings: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", _freeze_public_tree(self.metrics))

    def validate(self) -> None:
        _validate_exact_record(self, RelationshipEvidence, "relationship evidence")
        validate_identifier(self.relationship_id, "relationship id")
        validate_identifier(self.relationship_type, "relationship type")
        _validate_fund_code_tuple(self.fund_codes, "relationship fund codes", allow_empty=False)
        if type(self.evidence_state) is not BriefEvidenceState:
            raise ValueError("relationship state must be an exact BriefEvidenceState")
        if type(self.metrics) is not _MAPPING_PROXY_TYPE:
            raise ValueError("relationship metrics must be an immutable exact mapping")
        _validate_public_tree(self.metrics, "relationship.metrics")
        validate_identifier_tuple(self.evidence_ids, "relationship evidence ids")
        if type(self.report_periods) is not tuple or len(self.report_periods) > MAX_TUPLE_ITEMS:
            raise ValueError("relationship report periods must be a bounded exact tuple")
        if any(type(item) is not date for item in self.report_periods):
            raise ValueError("relationship report periods must contain exact dates")
        if (
            type(self.publication_times) is not tuple
            or len(self.publication_times) > MAX_TUPLE_ITEMS
        ):
            raise ValueError("relationship publication times must be a bounded exact tuple")
        for item in self.publication_times:
            _validate_utc_datetime(item, "relationship publication time")
        validate_public_text_tuple(self.warnings, "relationship warnings")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "evidence_ids": list(self.evidence_ids),
            "evidence_state": self.evidence_state.value,
            "fund_codes": list(self.fund_codes),
            "metrics": _canonical_public_tree(self.metrics),
            "publication_times": [canonical_value(item) for item in self.publication_times],
            "relationship_id": self.relationship_id,
            "relationship_type": self.relationship_type,
            "report_periods": [canonical_value(item) for item in self.report_periods],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BriefCoverage:
    coverage_id: str
    scope: str
    evidence_state: BriefEvidenceState
    included_fund_codes: Tuple[str, ...]
    omitted_fund_codes: Tuple[str, ...]
    known_percent: Optional[str]
    unknown_fields: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        _validate_exact_record(self, BriefCoverage, "brief coverage")
        validate_identifier(self.coverage_id, "coverage id")
        validate_identifier(self.scope, "coverage scope")
        if type(self.evidence_state) is not BriefEvidenceState:
            raise ValueError("coverage state must be an exact BriefEvidenceState")
        _validate_fund_code_tuple(self.included_fund_codes, "included fund codes")
        _validate_fund_code_tuple(self.omitted_fund_codes, "omitted fund codes")
        if set(self.included_fund_codes) & set(self.omitted_fund_codes):
            raise ValueError("included and omitted fund codes must be disjoint")
        if self.known_percent is not None:
            if type(self.known_percent) is Decimal:
                raise ValueError("coverage known percent contains Decimal")
            if type(self.known_percent) is not str:
                raise ValueError("coverage known percent must be a canonical string or None")
            try:
                value = Decimal(self.known_percent)
            except Exception:
                raise ValueError(
                    "coverage known percent must be a canonical decimal string"
                ) from None
            if canonical_decimal(value) != self.known_percent or not (
                Decimal("0") <= value <= Decimal("100")
            ):
                raise ValueError("coverage known percent must be canonical and between 0 and 100")
        validate_identifier_tuple(self.unknown_fields, "coverage unknown fields")
        validate_identifier_tuple(self.evidence_ids, "coverage evidence ids")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "coverage_id": self.coverage_id,
            "evidence_ids": list(self.evidence_ids),
            "evidence_state": self.evidence_state.value,
            "included_fund_codes": list(self.included_fund_codes),
            "known_percent": self.known_percent,
            "omitted_fund_codes": list(self.omitted_fund_codes),
            "scope": self.scope,
            "unknown_fields": list(self.unknown_fields),
        }


@dataclass(frozen=True)
class BriefResolutionBinding:
    action_id: str
    field_id: str
    resolution: RequestFieldResolution
    source_states: Tuple[SourceFieldState, ...]
    source_attempt_id: int
    source_id: str
    source_field_id: str
    evaluated_at: datetime

    def validate(self) -> None:
        _validate_exact_record(self, BriefResolutionBinding, "brief resolution binding")
        validate_identifier(self.action_id, "resolution binding action id")
        validate_identifier(self.field_id, "resolution binding field id")
        if type(self.resolution) is not RequestFieldResolution:
            raise ValueError("resolution binding must use an exact resolution")
        if type(self.source_states) is not tuple or not self.source_states:
            raise ValueError("resolution binding source states must be a non-empty tuple")
        if any(type(item) is not SourceFieldState for item in self.source_states):
            raise ValueError("resolution binding source states must be exact")
        if len(self.source_states) != len(set(self.source_states)):
            raise ValueError("resolution binding source states must be unique")
        if type(self.source_attempt_id) is not int or self.source_attempt_id <= 0:
            raise ValueError("resolution binding source attempt id must be positive")
        validate_identifier(self.source_id, "resolution binding source id")
        validate_identifier(self.source_field_id, "resolution binding source field id")
        _validate_utc_datetime(self.evaluated_at, "resolution binding evaluation time")
        if self.field_id == "official_events" and (
            self.source_id != "fund_manager_official_documents"
            or self.source_field_id != "fund_manager_product_announcement"
        ):
            raise ValueError("official event resolution requires the official announcement source")
        if self.field_id != "official_events" and self.source_field_id != self.field_id:
            raise ValueError("resolution binding field does not match its source field")
        if self.resolution is RequestFieldResolution.USABLE and (
            SourceFieldState.HEALTHY not in self.source_states
        ):
            raise ValueError("usable resolution binding requires a healthy source state")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "action_id": self.action_id,
            "evaluated_at": canonical_value(self.evaluated_at),
            "field_id": self.field_id,
            "resolution": self.resolution.value,
            "source_attempt_id": self.source_attempt_id,
            "source_field_id": self.source_field_id,
            "source_id": self.source_id,
            "source_states": [item.value for item in self.source_states],
        }

    @property
    def lineage_id(self) -> str:
        return f"source_attempt_{self.source_attempt_id}"


@dataclass(frozen=True)
class BriefActionInterpretation:
    action_id: str
    state: BriefState
    action_maturity: ActionMaturity
    supporting_evidence_ids: Tuple[str, ...]
    opposing_evidence_ids: Tuple[str, ...]
    blocking_codes: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    invalidation_conditions: Tuple[str, ...]
    unavailable_actions: Tuple[str, ...]
    exact_amount_available: bool
    state_inputs: object

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_inputs", _freeze_public_tree(self.state_inputs))

    def validate(self) -> None:
        _validate_exact_record(self, BriefActionInterpretation, "brief action interpretation")
        validate_identifier(self.action_id, "interpretation action id")
        if type(self.state) is not BriefState:
            raise ValueError("interpretation state must be an exact BriefState")
        if type(self.action_maturity) is not ActionMaturity:
            raise ValueError("interpretation maturity must be an exact ActionMaturity")
        if self.state is BriefState.NO_ADD:
            if self.action_maturity is not ActionMaturity.MATURE:
                raise ValueError("no-add interpretation maturity must be mature")
        elif self.state in {BriefState.HOLD, BriefState.WATCH, BriefState.ABSTAIN} and (
            self.action_maturity is not ActionMaturity.EXPERIMENTAL_SHADOW
        ):
            raise ValueError("hold, watch, and abstain maturity must remain experimental")
        validate_identifier_tuple(self.supporting_evidence_ids, "supporting evidence ids")
        validate_identifier_tuple(self.opposing_evidence_ids, "opposing evidence ids")
        validate_identifier_tuple(self.blocking_codes, "interpretation blocking codes")
        validate_identifier_tuple(self.missing_fields, "interpretation missing fields")
        validate_public_text_tuple(self.invalidation_conditions, "invalidation conditions")
        validate_identifier_tuple(self.unavailable_actions, "unavailable actions")
        if type(self.exact_amount_available) is not bool or self.exact_amount_available:
            raise ValueError("Phase 1 exact amount availability must be false")
        if type(self.state_inputs) is not _MAPPING_PROXY_TYPE:
            raise ValueError("state inputs must be an immutable exact mapping")
        _validate_public_tree(self.state_inputs, "interpretation.state_inputs")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "action_id": self.action_id,
            "action_maturity": self.action_maturity.value,
            "blocking_codes": list(self.blocking_codes),
            "exact_amount_available": self.exact_amount_available,
            "invalidation_conditions": list(self.invalidation_conditions),
            "missing_fields": list(self.missing_fields),
            "opposing_evidence_ids": list(self.opposing_evidence_ids),
            "state": self.state.value,
            "state_inputs": _canonical_public_tree(self.state_inputs),
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "unavailable_actions": list(self.unavailable_actions),
        }


@dataclass(frozen=True)
class BriefSnapshot:
    request_run_id: int
    decision_snapshot_id: int
    fund_code: str
    action_ids: Tuple[str, ...]
    mode: RequestMode
    facts: Tuple[BriefFact, ...]
    official_events: Tuple[OfficialEvent, ...]
    relationships: Tuple[RelationshipEvidence, ...]
    coverage: BriefCoverage
    holdings_coverage: BriefCoverage
    sync_status: BriefEvidenceStatus
    decision_evidence_status: BriefEvidenceStatus
    interpretations: Tuple[BriefActionInterpretation, ...]
    primary_state: BriefState
    action_maturity: ActionMaturity
    constraints: Tuple[str, ...]
    triggered_reviews: Tuple[str, ...]
    affected_action_abstentions: Tuple[str, ...]
    blocking_codes: Tuple[str, ...]
    evidence_state: BriefEvidenceState
    missing_fields: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    source_lineage_ids: Tuple[str, ...]
    evidence_fingerprint: str
    created_at: datetime
    portfolio_evidence_state: str
    position_present: Optional[bool]
    observation_version: str
    observed_at: Optional[datetime]
    resolution_lineage_ids: Tuple[str, ...] = ()
    resolution_bindings: Tuple[BriefResolutionBinding, ...] = ()

    def validate(self) -> None:
        _validate_exact_record(self, BriefSnapshot, "brief snapshot")
        for value, name in (
            (self.request_run_id, "request run id"),
            (self.decision_snapshot_id, "decision snapshot id"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive exact integer")
        _validate_fund_code(self.fund_code)
        validate_identifier_tuple(self.action_ids, "action ids", allow_empty=False)
        allowed_action_shapes = {
            ("fact_research", "continue_holding"),
            ("fact_research", "reduce_to_cash"),
            ("fact_research", "full_exit"),
            ("fact_research", "switch_reduce", "switch_buy"),
        }
        if self.action_ids not in allowed_action_shapes:
            raise ValueError("brief action ids do not match a canonical action shape")
        if type(self.mode) is not RequestMode:
            raise ValueError("brief mode must be an exact RequestMode")
        _validate_record_tuple(self.facts, BriefFact, "facts", "fact_id", 128)
        _validate_record_tuple(
            self.official_events, OfficialEvent, "official events", "event_id", 20
        )
        _validate_record_tuple(
            self.relationships,
            RelationshipEvidence,
            "relationships",
            "relationship_id",
            128,
        )
        if type(self.coverage) is not BriefCoverage:
            raise ValueError("coverage must be an exact BriefCoverage")
        self.coverage.validate()
        if type(self.holdings_coverage) is not BriefCoverage:
            raise ValueError("holdings coverage must be an exact BriefCoverage")
        self.holdings_coverage.validate()
        if self.holdings_coverage.coverage_id == self.coverage.coverage_id:
            raise ValueError("snapshot coverage ids must be distinct")
        if type(self.sync_status) is not BriefEvidenceStatus:
            raise ValueError("snapshot sync status must be exact")
        if type(self.decision_evidence_status) is not BriefEvidenceStatus:
            raise ValueError("snapshot decision evidence status must be exact")
        self.sync_status.validate()
        self.decision_evidence_status.validate()
        if self.evidence_state is not self.decision_evidence_status.state:
            raise ValueError("snapshot evidence state must match decision evidence status")
        _validate_record_tuple(
            self.interpretations,
            BriefActionInterpretation,
            "interpretations",
            "action_id",
            MAX_TUPLE_ITEMS,
        )
        interpretation_ids = tuple(item.action_id for item in self.interpretations)
        expected_interpretation_ids = self.action_ids[1:]
        if interpretation_ids != expected_interpretation_ids:
            raise ValueError("interpretation action ids must exactly match all non-fact action ids")

        fact_ids = tuple(item.fact_id for item in self.facts)
        event_ids = tuple(item.event_id for item in self.official_events)
        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        base_evidence_namespace_ids = fact_ids + event_ids + relationship_ids
        if len(base_evidence_namespace_ids) != len(set(base_evidence_namespace_ids)):
            raise ValueError("evidence namespace ids must be globally unique")
        coverage_ids = (self.coverage.coverage_id, self.holdings_coverage.coverage_id)
        if set(coverage_ids).intersection(base_evidence_namespace_ids):
            raise ValueError("coverage id must not collide with an evidence namespace id")
        evidence_namespace_ids = base_evidence_namespace_ids + coverage_ids

        fact_or_event_ids = set(fact_ids + event_ids)
        fact_or_relationship_ids = set(fact_ids + relationship_ids)
        all_evidence_ids = set(evidence_namespace_ids)
        for relationship in self.relationships:
            if not set(relationship.evidence_ids).issubset(fact_or_event_ids):
                raise ValueError("relationship evidence ids must resolve to facts or events")
        if not set(self.coverage.evidence_ids).issubset(fact_or_relationship_ids):
            raise ValueError("coverage evidence ids must resolve to facts or relationships")
        if not set(self.holdings_coverage.evidence_ids).issubset(fact_or_relationship_ids):
            raise ValueError(
                "holdings coverage evidence ids must resolve to facts or relationships"
            )
        for interpretation in self.interpretations:
            interpretation_evidence = set(
                interpretation.supporting_evidence_ids + interpretation.opposing_evidence_ids
            )
            if not interpretation_evidence.issubset(all_evidence_ids):
                raise ValueError("interpretation evidence ids must resolve to snapshot evidence")
        action_id_set = set(self.action_ids)
        for event in self.official_events:
            expected_affected_actions = canonical_event_affected_actions(
                event.event_code,
                self.action_ids,
            )
            if event.affected_action_ids != expected_affected_actions:
                raise ValueError("event affected actions must exactly match the canonical scope")

        hard_event_codes = {
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
            OfficialEventCode.FUND_TERMINATION_NOTICE,
        }
        ordered_active_hard_events = tuple(
            event
            for event in sorted(
                self.official_events,
                key=lambda item: (item.published_at, item.event_id),
            )
            if event.integrity_status == "active" and event.event_code in hard_event_codes
        )
        expected_triggered_reviews = tuple(
            dict.fromkeys(event.event_code.value for event in ordered_active_hard_events)
        )
        if self.triggered_reviews != expected_triggered_reviews:
            raise ValueError("snapshot triggered reviews must exactly match active hard events")
        allowed_action_states = {
            "continue_holding": {
                BriefState.NO_ADD,
                BriefState.HOLD,
                BriefState.WATCH,
                BriefState.REDUCE_OR_EXIT_REVIEW,
                BriefState.ABSTAIN,
            },
            "reduce_to_cash": {BriefState.REDUCE_OR_EXIT_REVIEW, BriefState.ABSTAIN},
            "full_exit": {BriefState.REDUCE_OR_EXIT_REVIEW, BriefState.ABSTAIN},
            "switch_reduce": {BriefState.REDUCE_OR_EXIT_REVIEW, BriefState.ABSTAIN},
            "switch_buy": {BriefState.ABSTAIN},
        }
        watch_event_codes = {
            OfficialEventCode.MANAGER_CHANGE_NOTICE,
            OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE,
            OfficialEventCode.FEE_CHANGE_NOTICE,
            OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
        }
        for interpretation in self.interpretations:
            active_action_events = tuple(
                event
                for event in self.official_events
                if event.integrity_status == "active"
                and interpretation.action_id in event.affected_action_ids
            )
            inactive_action_events = tuple(
                event
                for event in self.official_events
                if event.integrity_status != "active"
                and interpretation.action_id in event.affected_action_ids
            )
            hard_events = tuple(
                event
                for event in ordered_active_hard_events
                if interpretation.action_id in event.affected_action_ids
            )
            watch_events = tuple(
                event for event in active_action_events if event.event_code in watch_event_codes
            )
            redemption_restrictions = tuple(
                event
                for event in active_action_events
                if event.event_code is OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE
            )
            if interpretation.state not in allowed_action_states[interpretation.action_id] or (
                interpretation.action_id == "continue_holding"
                and interpretation.state is BriefState.REDUCE_OR_EXIT_REVIEW
                and interpretation.action_maturity is not ActionMaturity.MATURE
            ):
                raise ValueError("interpretation action state is outside the Phase 1 matrix")
            if hard_events:
                phase_b_no_add = (
                    interpretation.state is BriefState.NO_ADD
                    and "phase_b_blocked" in interpretation.blocking_codes
                )
                if not phase_b_no_add and not (
                    interpretation.state is BriefState.REDUCE_OR_EXIT_REVIEW
                    and interpretation.action_maturity is ActionMaturity.MATURE
                ):
                    raise ValueError("active hard official event requires exit review or no-add")
                if not {event.event_id for event in hard_events}.issubset(
                    interpretation.supporting_evidence_ids
                ):
                    raise ValueError("hard official event must be supporting evidence")
                if "immediate_sale" not in interpretation.unavailable_actions:
                    raise ValueError("hard official event must keep immediate sale unavailable")
            elif (
                interpretation.state is BriefState.REDUCE_OR_EXIT_REVIEW
                and interpretation.action_maturity is ActionMaturity.MATURE
            ):
                raise ValueError("mature exit review requires an active hard official event")
            if watch_events and interpretation.state is BriefState.HOLD:
                raise ValueError("active risk event cannot retain a hold interpretation")
            if (
                any(
                    event.event_code is OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE
                    for event in watch_events
                )
                and "buy_or_add" not in interpretation.unavailable_actions
            ):
                raise ValueError("subscription suspension must keep buy or add unavailable")
            if redemption_restrictions and (
                not {event.event_id for event in redemption_restrictions}.issubset(
                    interpretation.supporting_evidence_ids
                )
                or OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE.value
                not in interpretation.blocking_codes
                or "executable_redemption" not in interpretation.unavailable_actions
                or interpretation.action_id not in self.affected_action_abstentions
            ):
                raise ValueError("redemption restriction must remain an affected action block")
            if inactive_action_events and (
                interpretation.state is not BriefState.ABSTAIN
                or not {event.event_id for event in inactive_action_events}.issubset(
                    interpretation.opposing_evidence_ids
                )
                or not {
                    f"official_event_{event.integrity_status}_{event.event_id}"
                    for event in inactive_action_events
                }.issubset(interpretation.blocking_codes)
                or interpretation.action_id not in self.affected_action_abstentions
            ):
                raise ValueError("inactive official event requires affected action abstention")

        if type(self.resolution_bindings) is not tuple:
            raise ValueError("snapshot resolution bindings must be an exact tuple")
        resolution_keys = []
        for binding in self.resolution_bindings:
            if type(binding) is not BriefResolutionBinding:
                raise ValueError("snapshot resolution bindings must contain exact records")
            binding.validate()
            if binding.action_id not in action_id_set:
                raise ValueError("resolution binding action is outside the snapshot")
            resolution_keys.append((binding.action_id, binding.field_id))
        if len(resolution_keys) != len(set(resolution_keys)):
            raise ValueError("snapshot resolution action and field bindings must be unique")

        validate_identifier_tuple(
            self.resolution_lineage_ids,
            "snapshot resolution lineage ids",
        )
        expected_resolution_lineages = tuple(
            dict.fromkeys(binding.lineage_id for binding in self.resolution_bindings)
        )
        if self.resolution_lineage_ids != expected_resolution_lineages:
            raise ValueError("snapshot resolution lineages must exactly match their bindings")
        expected_lineage_ids = []
        for lineage_id in (
            tuple(item.source_lineage_id for item in self.facts)
            + tuple(
                source_id
                for event in self.official_events
                for source_id in (event.original_source_id, event.quoted_source_id)
                if source_id is not None
            )
            + self.resolution_lineage_ids
        ):
            if lineage_id not in expected_lineage_ids:
                expected_lineage_ids.append(lineage_id)
        if self.source_lineage_ids != tuple(expected_lineage_ids):
            raise ValueError(
                "snapshot source lineage ids must exactly bind all fact and event lineage"
            )
        if type(self.primary_state) is not BriefState:
            raise ValueError("primary state must be an exact BriefState")
        if type(self.action_maturity) is not ActionMaturity:
            raise ValueError("snapshot maturity must be an exact ActionMaturity")
        if any("phase_b_blocked" in item.blocking_codes for item in self.interpretations):
            expected_primary_state = BriefState.NO_ADD
            expected_primary_maturity = ActionMaturity.MATURE
        else:
            primary = next(
                (
                    item
                    for item in self.interpretations
                    if item.state is BriefState.REDUCE_OR_EXIT_REVIEW
                ),
                self.interpretations[0],
            )
            expected_primary_state = primary.state
            expected_primary_maturity = primary.action_maturity
        if (
            self.primary_state is not expected_primary_state
            or self.action_maturity is not expected_primary_maturity
        ):
            raise ValueError("primary state and maturity do not follow brief precedence")
        for interpretation in self.interpretations:
            if interpretation.state is not BriefState.HOLD:
                continue
            inputs = interpretation.state_inputs
            official_binding = next(
                (
                    binding
                    for binding in self.resolution_bindings
                    if binding.action_id == interpretation.action_id
                    and binding.field_id == "official_events"
                    and binding.resolution is RequestFieldResolution.USABLE
                ),
                None,
            )
            record_id = inputs.get("thesis_record_id")
            fingerprint = inputs.get("thesis_fingerprint")
            if (
                inputs.get("owner_confirmed_thesis") is not True
                or not interpretation.invalidation_conditions
                or inputs.get("thesis_review_state") != "intact"
                or official_binding is None
                or type(record_id) is not str
                or not record_id.isascii()
                or not record_id.isdigit()
                or record_id.startswith("0")
                or type(fingerprint) is not str
                or inputs.get("thesis_review_source_lineage_id") != official_binding.lineage_id
                or inputs.get("thesis_reviewed_at") != official_binding.evaluated_at
            ):
                raise ValueError("hold requires an authenticated thesis and official review")
            validate_checksum(fingerprint, "hold thesis fingerprint")
        for value, name in (
            (self.constraints, "snapshot constraints"),
            (self.triggered_reviews, "triggered reviews"),
            (self.affected_action_abstentions, "affected action abstentions"),
            (self.blocking_codes, "snapshot blocking codes"),
            (self.missing_fields, "snapshot missing fields"),
            (self.conflicts, "snapshot conflicts"),
            (self.source_lineage_ids, "snapshot source lineage ids"),
        ):
            validate_identifier_tuple(value, name)
        if not set(self.affected_action_abstentions).issubset(interpretation_ids):
            raise ValueError("affected action abstentions must resolve to interpretations")
        required_blocking_codes = {
            code
            for interpretation in self.interpretations
            for code in interpretation.blocking_codes
        }
        if not required_blocking_codes.issubset(self.blocking_codes):
            raise ValueError(
                "snapshot blocking codes must include every interpretation blocking code"
            )
        required_missing_fields = set(self.coverage.unknown_fields)
        required_missing_fields.update(self.holdings_coverage.unknown_fields)
        required_missing_fields.update(self.sync_status.missing_fields)
        required_missing_fields.update(self.decision_evidence_status.missing_fields)
        required_missing_fields.update(
            field_id
            for interpretation in self.interpretations
            for field_id in interpretation.missing_fields
        )
        if not required_missing_fields.issubset(self.missing_fields):
            raise ValueError(
                "snapshot missing fields must include action and coverage missing fields"
            )
        required_conflicts = {
            conflict_id for fact in self.facts for conflict_id in fact.conflict_ids
        }
        if not required_conflicts.issubset(self.conflicts):
            raise ValueError("snapshot conflicts must include every fact conflict")
        if self.portfolio_evidence_state not in {"current", "dated", "unknown"}:
            raise ValueError("snapshot portfolio evidence state is unsupported")
        if self.position_present is not None and type(self.position_present) is not bool:
            raise ValueError("snapshot position presence must be boolean or None")
        validate_identifier(self.observation_version, "snapshot observation version")
        if self.portfolio_evidence_state == "unknown":
            if self.position_present is not None or self.observed_at is not None:
                raise ValueError("unknown portfolio evidence cannot claim position facts")
        elif self.observed_at is None:
            raise ValueError("known portfolio evidence requires an observation time")
        if self.observed_at is not None:
            _validate_utc_datetime(self.observed_at, "snapshot portfolio observation time")
        validate_checksum(self.evidence_fingerprint, "brief evidence fingerprint")
        _validate_utc_datetime(self.created_at, "brief snapshot creation time")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "action_ids": list(self.action_ids),
            "action_maturity": self.action_maturity.value,
            "affected_action_abstentions": list(self.affected_action_abstentions),
            "blocking_codes": list(self.blocking_codes),
            "conflicts": list(self.conflicts),
            "constraints": list(self.constraints),
            "coverage": self.coverage.to_canonical_dict(),
            "created_at": canonical_value(self.created_at),
            "decision_snapshot_id": self.decision_snapshot_id,
            "evidence_fingerprint": self.evidence_fingerprint,
            "evidence_state": self.evidence_state.value,
            "facts": [item.to_canonical_dict() for item in self.facts],
            "fund_code": self.fund_code,
            "interpretations": [item.to_canonical_dict() for item in self.interpretations],
            "holdings_coverage": self.holdings_coverage.to_canonical_dict(),
            "missing_fields": list(self.missing_fields),
            "mode": self.mode.value,
            "official_events": [item.to_canonical_dict() for item in self.official_events],
            "primary_state": self.primary_state.value,
            "portfolio_evidence_state": self.portfolio_evidence_state,
            "position_present": self.position_present,
            "observation_version": self.observation_version,
            "observed_at": (
                None if self.observed_at is None else canonical_value(self.observed_at)
            ),
            "relationships": [item.to_canonical_dict() for item in self.relationships],
            "request_run_id": self.request_run_id,
            "resolution_bindings": [item.to_canonical_dict() for item in self.resolution_bindings],
            "resolution_lineage_ids": list(self.resolution_lineage_ids),
            "source_lineage_ids": list(self.source_lineage_ids),
            "sync_status": self.sync_status.to_canonical_dict(),
            "decision_evidence_status": self.decision_evidence_status.to_canonical_dict(),
            "triggered_reviews": list(self.triggered_reviews),
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


@dataclass(frozen=True)
class HeldFundBriefReport:
    snapshot: BriefSnapshot
    owner_overlay: Optional[Dict[str, object]] = None

    def __post_init__(self) -> None:
        if self.owner_overlay is not None:
            object.__setattr__(
                self,
                "owner_overlay",
                _freeze_public_tree(self.owner_overlay),
            )

    def validate(self) -> None:
        _validate_exact_record(self, HeldFundBriefReport, "held fund brief report")
        if type(self.snapshot) is not BriefSnapshot:
            raise ValueError("report snapshot must be an exact BriefSnapshot")
        self.snapshot.validate()
        if self.owner_overlay is None:
            return
        if type(self.owner_overlay) is not _MAPPING_PROXY_TYPE:
            raise ValueError("owner overlay must be an immutable exact mapping or None")
        allowed = {
            "observation_version",
            "observed_at",
            "portfolio_weight",
            "position_present",
        }
        unknown = set(self.owner_overlay) - allowed
        if unknown:
            raise ValueError("unknown owner overlay keys are not accepted")
        if set(self.owner_overlay) != allowed:
            raise ValueError("owner overlay must contain its exact four public fields")
        position_present = self.owner_overlay["position_present"]
        if position_present is not None and type(position_present) is not bool:
            raise ValueError("owner overlay position presence must be boolean or None")
        if position_present is not self.snapshot.position_present:
            raise ValueError("owner overlay position presence must match the snapshot")
        weight = self.owner_overlay["portfolio_weight"]
        if weight is not None:
            if type(weight) is not str:
                raise ValueError("owner overlay portfolio weight must be canonical or None")
            try:
                parsed_weight = Decimal(weight)
            except Exception:
                raise ValueError("owner overlay portfolio weight must be canonical") from None
            if canonical_decimal(parsed_weight) != weight or not (
                Decimal("0") <= parsed_weight <= Decimal("1")
            ):
                raise ValueError("owner overlay portfolio weight must be canonical and in [0, 1]")
        if position_present is None and weight is not None:
            raise ValueError("unknown position cannot claim a portfolio weight")
        if position_present is False and weight != "0":
            raise ValueError("absent position must use zero portfolio weight")
        observed_at = self.owner_overlay["observed_at"]
        if observed_at is not None:
            _validate_utc_datetime(observed_at, "owner overlay observation time")
        if position_present is None and observed_at is not None:
            raise ValueError("unknown position cannot claim an observation time")
        validate_identifier(self.owner_overlay["observation_version"], "observation version")
        if (
            self.owner_overlay["observation_version"] != self.snapshot.observation_version
            or observed_at != self.snapshot.observed_at
        ):
            raise ValueError("owner overlay provenance must match the snapshot")

    def to_canonical_dict(self) -> dict:
        self.validate()
        overlay = None
        if self.owner_overlay is not None:
            overlay = {
                "observation_version": self.owner_overlay["observation_version"],
                "observed_at": (
                    None
                    if self.owner_overlay["observed_at"] is None
                    else canonical_value(self.owner_overlay["observed_at"])
                ),
                "portfolio_weight": self.owner_overlay["portfolio_weight"],
                "position_present": self.owner_overlay["position_present"],
            }
        return {"owner_overlay": overlay, "snapshot": self.snapshot.to_canonical_dict()}

    def persisted_checksum(self) -> str:
        self.validate()
        return self.snapshot.checksum()
