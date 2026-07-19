from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple
from urllib.parse import urlsplit

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import (
    ActionKind,
    canonical_json_bytes,
    canonical_value,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
    validate_version,
)
from kunjin.funds.official_domains import FUND_COMPANY_DOMAINS
from kunjin.intelligence.models import (
    LineageKind,
    _freeze_public_tree,
    _validate_public_https_url,
    _validate_public_tree,
)

_FUND_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_MAX_ANNOUNCEMENT_CONTENT_BYTES = 512 * 1024
_PUBLIC_TEXT_CHUNK_CHARS = 3_840
_PUBLIC_TEXT_CHUNK_OVERLAP = 128
_ALLOWED_ACTIONS = frozenset(
    (ActionKind.CONTINUE_HOLDING, ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT)
)
_REVIEWED_PUBLIC_BOUNDARY_KEYS = frozenset(("exact_amount_available",))
_SUPPORTED_OFFICIAL_EVENTS = frozenset(
    (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        OfficialEventCode.FUND_TERMINATION_NOTICE,
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        OfficialEventCode.FEE_CHANGE_NOTICE,
        OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
    )
)


class FlowStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class ReviewDisposition(str, Enum):
    CONTINUE_OBSERVING = "continue_observing"
    MANUAL_THESIS_REVIEW_REQUIRED = "manual_thesis_review_required"
    REDUCE_REVIEW = "reduce_review"
    EXIT_REVIEW = "exit_review"
    ABSTAIN = "abstain"


class ThesisMatchState(str, Enum):
    THESIS_MISSING = "thesis_missing"
    NO_MATCHING_EVIDENCE = "no_matching_evidence"
    MANUAL_REVIEW_PENDING = "manual_review_pending"
    MANUAL_REVIEW_UNCERTAIN = "manual_review_uncertain"
    PRESENTED_MATCH_REJECTED = "presented_match_rejected"
    PRESENTED_MATCH_CONFIRMED = "presented_match_confirmed"
    THESIS_BINDING_INVALID = "thesis_binding_invalid"


class AdjudicationDecision(str, Enum):
    PRESENTED_MATCH_CONFIRMED = "presented_match_confirmed"
    PRESENTED_MATCH_REJECTED = "presented_match_rejected"
    UNCERTAIN = "uncertain"


class RedemptionFeasibility(str, Enum):
    NOT_REQUESTED = "not_requested"
    INSUFFICIENT_DATA = "insufficient_data"
    RESTRICTED = "restricted"
    EVIDENCE_COMPLETE_NON_AUTHORIZING = "evidence_complete_non_authorizing"


class EvidenceReadiness(str, Enum):
    READY = "ready"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


class HistoryComparability(str, Enum):
    COMPARABLE = "comparable"
    NOT_COMPARABLE = "not_comparable"
    NOT_AVAILABLE = "not_available"


class ThesisMatchProjectionState(str, Enum):
    THESIS_MISSING = "thesis_missing"
    NO_MATCHING_EVIDENCE = "no_matching_evidence"
    POSSIBLE_INVALIDATION_MATCH = "possible_invalidation_match"


class ActionReviewSourceSufficiency(str, Enum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT_DATA = "insufficient_data"


class RedemptionComponentState(str, Enum):
    USABLE = "usable"
    RESTRICTED = "restricted"
    MISSING = "missing"
    STALE = "stale"
    CONFLICTED = "conflicted"
    UNSUPPORTED = "unsupported"


class TriggeredReviewCode(str, Enum):
    FULL_EXIT_FEASIBILITY_REVIEW = "full_exit_feasibility_review"
    REDEMPTION_RESTRICTION_REVIEW = "redemption_restriction_review"
    MANAGER_CHANGE_REVIEW = "manager_change_review"
    FEE_CHANGE_REVIEW = "fee_change_review"
    BENCHMARK_CHANGE_REVIEW = "benchmark_change_review"


class RemainderIntent(str, Enum):
    RETAIN_SOME = "retain_some"
    NO_MINIMUM_INTENT = "no_minimum_intent"
    UNKNOWN = "unknown"


class ExitReason(str, Enum):
    OWNER_BELIEVES_THESIS_INVALIDATED = "owner_believes_thesis_invalidated"
    GOAL_CHANGED = "goal_changed"
    CASH_NEED = "cash_need"
    RISK_REDUCTION = "risk_reduction"
    OTHER = "other"
    UNKNOWN = "unknown"


class UseOfProceeds(str, Enum):
    CASH_RESERVE = "cash_reserve"
    KNOWN_GOAL = "known_goal"
    REALLOCATION_REVIEW = "reallocation_review"
    OTHER = "other"
    UNKNOWN = "unknown"


class ThesisReviewReadiness(str, Enum):
    READY = "ready"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    MISSING = "missing"
    INSUFFICIENT_DATA = "insufficient_data"


class ConditionalReviewUsability(str, Enum):
    OBSERVED_FOR_REQUEST = "observed_for_request"
    PARTIAL = "partial"
    NOT_TESTABLE = "not_testable"


class BindingState(str, Enum):
    PRESENT = "present"
    MISSING = "missing"


def _exact_record(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be an exact {expected.__name__}")
    validate_exact_dataclass_state(value, name)


def _fund_code(value: object, name: str = "fund code") -> str:
    if (
        type(value) is not str
        or _FUND_CODE_PATTERN.fullmatch(value) is None
        or value == "000000"
    ):
        raise ValueError(f"{name} must be a non-reserved six-digit ASCII code")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _optional_positive_int(value: object, name: str) -> None:
    if value is not None:
        _positive_int(value, name)


def _utc(value: object, name: str) -> datetime:
    validated = validate_aware_datetime(value, name)
    if validated.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return validated


def _exact_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be an exact boolean")


def _exact_enum(value: object, enum_type: type[Enum], name: str) -> None:
    if type(value) is not enum_type:
        raise ValueError(f"{name} must be an exact {enum_type.__name__}")


def _sorted_identifiers(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> Tuple[str, ...]:
    validated = validate_identifier_tuple(value, name, allow_empty=allow_empty)
    if validated != tuple(sorted(validated)):
        raise ValueError(f"{name} must be a sorted unique exact tuple")
    return validated


def _enum_tuple(value: object, enum_type: type[Enum], name: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    for item in value:
        _exact_enum(item, enum_type, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must be unique")


def _validate_bounded_public_content(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty exact string")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_ANNOUNCEMENT_CONTENT_BYTES:
        raise ValueError(f"{name} exceeds the bounded byte limit")
    step = _PUBLIC_TEXT_CHUNK_CHARS - _PUBLIC_TEXT_CHUNK_OVERLAP
    for offset in range(0, len(value), step):
        chunk = value[offset : offset + _PUBLIC_TEXT_CHUNK_CHARS]
        validate_public_text(chunk, name)
        _validate_public_tree(_freeze_public_tree({"content": chunk}), name)
    normalized = " ".join(part for part in re.split(r"[\W_]+", value.casefold()) if part)
    private_markers = (
        "access token",
        "authorization bearer",
        "exact amount",
        "local path",
        "private value",
    )
    if any(marker in normalized for marker in private_markers):
        raise ValueError(f"{name} contains a secret or private marker")
    return value


def _validate_canonical_public_tree(value: object, name: str) -> None:
    if type(value) is dict:
        for key, item in value.items():
            validate_identifier(key, f"{name} key")
            if key not in _REVIEWED_PUBLIC_BOUNDARY_KEYS:
                _validate_public_tree(
                    _freeze_public_tree({key: "public"}),
                    f"{name}.{key}",
                )
            _validate_canonical_public_tree(item, f"{name}.{key}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_canonical_public_tree(item, f"{name}[{index}]")
        return
    if type(value) is str:
        step = _PUBLIC_TEXT_CHUNK_CHARS - _PUBLIC_TEXT_CHUNK_OVERLAP
        for offset in range(0, len(value), step):
            chunk = value[offset : offset + _PUBLIC_TEXT_CHUNK_CHARS]
            _validate_public_tree(
                _freeze_public_tree({"value": chunk}),
                name,
            )
        return
    if type(value) in {int, bool} or value is None:
        return
    raise ValueError(f"{name} contains unsupported canonical public data")


class _CanonicalRecord:
    def validate(self) -> None:
        raise NotImplementedError

    def _canonical_fields(self, excluded: frozenset[str] = frozenset()) -> dict:
        return {
            item.name: canonical_value(getattr(self, item.name))
            for item in fields(self)
            if item.name not in excluded
        }

    def to_canonical_dict(self) -> dict:
        self.validate()
        result = self._canonical_fields()
        _validate_canonical_public_tree(result, type(self).__name__)
        return result

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_dict())

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


class _AuthenticatedRecord(_CanonicalRecord):
    record_checksum: str

    def pre_checksum_canonical_dict(self) -> dict:
        return self._canonical_fields(frozenset(("record_checksum",)))

    def expected_record_checksum(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.pre_checksum_canonical_dict())).hexdigest()

    def _validate_record_checksum(self, name: str) -> None:
        validate_checksum(self.record_checksum, f"{name} record checksum")
        if self.record_checksum != self.expected_record_checksum():
            raise ValueError(f"{name} record checksum does not match")


@dataclass(frozen=True)
class ReviewBoundary(_CanonicalRecord):
    review_maturity: str = "evidence_only"
    action_authorized: bool = False
    exact_amount_available: bool = False
    automatic_trade: bool = False

    def validate(self) -> None:
        _exact_record(self, ReviewBoundary, "review boundary")
        if self != ReviewBoundary():
            raise ValueError("holding review action boundary is invalid")


@dataclass(frozen=True)
class RedemptionEvidence(_CanonicalRecord):
    current_position: RedemptionComponentState
    exact_share_class: RedemptionComponentState
    applicable_holding_period_tier: RedemptionComponentState
    channel_rule: RedemptionComponentState
    current_redemption_restriction: RedemptionComponentState
    applicable_fee_schedule: RedemptionComponentState
    settlement_rule: RedemptionComponentState

    def validate(self) -> None:
        _exact_record(self, RedemptionEvidence, "redemption evidence")
        for item in fields(self):
            _exact_enum(
                getattr(self, item.name),
                RedemptionComponentState,
                f"redemption component {item.name}",
            )

    def component_states(self) -> Tuple[RedemptionComponentState, ...]:
        self.validate()
        return tuple(getattr(self, item.name) for item in fields(self))


@dataclass(frozen=True)
class OfficialAnnouncementContent(_AuthenticatedRecord):
    brief_request_run_id: int
    source_attempt_id: int
    fund_code: str
    listing_source_document_id: int
    canonical_announcement_url: str
    announcement_title: str
    announcement_published_at: datetime
    publisher: str
    normalized_content: str
    normalized_content_bytes: int
    normalized_content_sha256: str
    original_source_id: str
    quoted_source_id: Optional[str]
    integrity_status: str
    integrity_checked_at: datetime
    retrieved_at: datetime
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, OfficialAnnouncementContent, "official announcement content")
        _positive_int(self.brief_request_run_id, "brief request run id")
        _positive_int(self.source_attempt_id, "source attempt id")
        _fund_code(self.fund_code)
        _positive_int(self.listing_source_document_id, "listing source document id")
        _validate_public_https_url(self.canonical_announcement_url, "announcement URL")
        parsed = urlsplit(self.canonical_announcement_url)
        if parsed.query or "//" in parsed.path or any(
            part in {".", ".."} for part in parsed.path.split("/")
        ):
            raise ValueError("announcement URL must be a canonical public HTTPS URL")
        validate_public_text(self.announcement_title, "announcement title")
        validate_public_text(self.publisher, "announcement publisher")
        if FUND_COMPANY_DOMAINS.get(parsed.hostname or "") != self.publisher:
            raise ValueError("announcement publisher does not match the registered manager")
        _utc(self.announcement_published_at, "announcement publication time")
        _utc(self.integrity_checked_at, "announcement integrity check time")
        _utc(self.retrieved_at, "announcement retrieval time")
        if (
            self.integrity_checked_at < self.announcement_published_at
            or self.retrieved_at < self.announcement_published_at
        ):
            raise ValueError("announcement checks cannot precede publication")
        _validate_bounded_public_content(self.normalized_content, "normalized content")
        encoded = self.normalized_content.encode("utf-8")
        if type(self.normalized_content_bytes) is not int or self.normalized_content_bytes != len(
            encoded
        ):
            raise ValueError("normalized announcement content byte count is invalid")
        validate_checksum(self.normalized_content_sha256, "normalized content checksum")
        if hashlib.sha256(encoded).hexdigest() != self.normalized_content_sha256:
            raise ValueError("normalized announcement content checksum does not match")
        validate_identifier(self.original_source_id, "announcement original source id")
        if self.original_source_id != "fund_manager_official_documents":
            raise ValueError("announcement original source is not the registered manager source")
        if self.quoted_source_id is not None:
            validate_identifier(self.quoted_source_id, "announcement quoted source id")
            if self.quoted_source_id == self.original_source_id:
                raise ValueError("announcement quote cannot duplicate its original source")
        if self.integrity_status not in {"active", "corrected", "retracted"}:
            raise ValueError("announcement integrity status is unsupported")
        self._validate_record_checksum("official announcement content")


_EVENT_TRIGGER_MAP = {
    OfficialEventCode.FUND_LIQUIDATION_NOTICE: TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
    OfficialEventCode.FUND_TERMINATION_NOTICE: TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
    OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE: (
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW
    ),
    OfficialEventCode.MANAGER_CHANGE_NOTICE: TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
    OfficialEventCode.FEE_CHANGE_NOTICE: TriggeredReviewCode.FEE_CHANGE_REVIEW,
    OfficialEventCode.BENCHMARK_CHANGE_NOTICE: TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW,
}


@dataclass(frozen=True)
class HeldReviewOfficialEventProjection(_AuthenticatedRecord):
    brief_request_run_id: int
    fund_code: str
    announcement_row_id: int
    announcement_content_id: int
    event_code: OfficialEventCode
    triggered_review_code: TriggeredReviewCode
    policy_version: str
    policy_checksum: str
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, HeldReviewOfficialEventProjection, "official event projection")
        _positive_int(self.brief_request_run_id, "brief request run id")
        _fund_code(self.fund_code)
        _positive_int(self.announcement_row_id, "announcement row id")
        _positive_int(self.announcement_content_id, "announcement content id")
        _exact_enum(self.event_code, OfficialEventCode, "official event code")
        if self.event_code not in _SUPPORTED_OFFICIAL_EVENTS:
            raise ValueError("official event code is not supported by Phase 5")
        _exact_enum(
            self.triggered_review_code,
            TriggeredReviewCode,
            "triggered review code",
        )
        if _EVENT_TRIGGER_MAP[self.event_code] is not self.triggered_review_code:
            raise ValueError("official event and triggered review code do not match")
        validate_version(self.policy_version, "official event policy version")
        validate_checksum(self.policy_checksum, "official event policy checksum")
        self._validate_record_checksum("official event projection")


@dataclass(frozen=True)
class ReviewEvidenceItem(_CanonicalRecord):
    evidence_id: str
    source_tier: int
    lineage_kind: LineageKind
    current: bool
    graph_closed: bool
    original_lineage: bool
    retracted: bool
    conflicted: bool
    direct_subject_binding: bool

    def validate(self) -> None:
        _exact_record(self, ReviewEvidenceItem, "review evidence item")
        validate_identifier(self.evidence_id, "review evidence id")
        if type(self.source_tier) is not int or self.source_tier not in (1, 2):
            raise ValueError("review evidence source tier must be exact 1 or 2")
        _exact_enum(self.lineage_kind, LineageKind, "review evidence lineage kind")
        for item in fields(self)[3:]:
            _exact_bool(getattr(self, item.name), item.name.replace("_", " "))
        if self.original_lineage != (self.lineage_kind is LineageKind.ORIGINAL):
            raise ValueError("review evidence lineage fields are inconsistent")


def _evidence_set_checksum(items: Tuple[ReviewEvidenceItem, ...]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "evidence_descriptors": [item.to_canonical_dict() for item in items],
                "evidence_ids": [item.evidence_id for item in items],
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class ThesisMatchProjection(_AuthenticatedRecord):
    fund_code: str
    thesis_id: Optional[int]
    thesis_fingerprint: Optional[str]
    intelligence_request_run_id: int
    intelligence_snapshot_id: int
    intelligence_snapshot_checksum: str
    matcher_policy_version: str
    matcher_policy_checksum: str
    projection_state: ThesisMatchProjectionState
    evidence_descriptors: Tuple[ReviewEvidenceItem, ...]
    evidence_set_checksum: str
    created_at: datetime
    record_checksum: str

    @property
    def evidence_ids(self) -> Tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence_descriptors)

    @property
    def source_tiers(self) -> Tuple[int, ...]:
        return tuple(item.source_tier for item in self.evidence_descriptors)

    @property
    def lineage_kinds(self) -> Tuple[str, ...]:
        return tuple(item.lineage_kind.value for item in self.evidence_descriptors)

    def expected_evidence_set_checksum(self) -> str:
        return _evidence_set_checksum(self.evidence_descriptors)

    def validate(self) -> None:
        _exact_record(self, ThesisMatchProjection, "thesis match projection")
        _fund_code(self.fund_code)
        _optional_positive_int(self.thesis_id, "thesis id")
        if (self.thesis_id is None) != (self.thesis_fingerprint is None):
            raise ValueError("projection thesis id and fingerprint must be paired")
        if self.thesis_fingerprint is not None:
            validate_checksum(self.thesis_fingerprint, "thesis fingerprint")
        _positive_int(self.intelligence_request_run_id, "intelligence request run id")
        _positive_int(self.intelligence_snapshot_id, "intelligence snapshot id")
        validate_checksum(self.intelligence_snapshot_checksum, "intelligence snapshot checksum")
        validate_version(self.matcher_policy_version, "matcher policy version")
        validate_checksum(self.matcher_policy_checksum, "matcher policy checksum")
        _exact_enum(self.projection_state, ThesisMatchProjectionState, "projection state")
        if type(self.evidence_descriptors) is not tuple:
            raise ValueError("projection evidence descriptors must be an exact tuple")
        for item in self.evidence_descriptors:
            if type(item) is not ReviewEvidenceItem:
                raise ValueError("projection evidence descriptors must contain exact records")
            item.validate()
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("projection evidence ids must be sorted and unique")
        has_evidence = bool(self.evidence_descriptors)
        if self.projection_state is ThesisMatchProjectionState.THESIS_MISSING:
            if self.thesis_id is not None or has_evidence:
                raise ValueError("thesis-missing projection cannot bind a thesis or evidence")
        elif self.thesis_id is None:
            raise ValueError("non-missing projection requires a bound thesis")
        elif has_evidence != (
            self.projection_state is ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
        ):
            raise ValueError("projection state and evidence descriptors are inconsistent")
        validate_checksum(self.evidence_set_checksum, "projection evidence-set checksum")
        if self.evidence_set_checksum != self.expected_evidence_set_checksum():
            raise ValueError("projection evidence-set checksum does not match")
        _utc(self.created_at, "thesis match projection creation time")
        self._validate_record_checksum("thesis match projection")


@dataclass(frozen=True)
class ThesisEvidenceAdjudication(_AuthenticatedRecord):
    fund_code: str
    thesis_id: int
    thesis_fingerprint: str
    thesis_match_projection_id: int
    thesis_match_projection_checksum: str
    intelligence_request_run_id: int
    intelligence_snapshot_checksum: str
    evidence_ids: Tuple[str, ...]
    evidence_set_checksum: str
    decision: AdjudicationDecision
    superseded_adjudication_id: Optional[int]
    created_at: datetime
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, ThesisEvidenceAdjudication, "thesis evidence adjudication")
        _fund_code(self.fund_code)
        _positive_int(self.thesis_id, "thesis id")
        validate_checksum(self.thesis_fingerprint, "thesis fingerprint")
        _positive_int(self.thesis_match_projection_id, "thesis match projection id")
        validate_checksum(
            self.thesis_match_projection_checksum, "thesis match projection checksum"
        )
        _positive_int(self.intelligence_request_run_id, "intelligence request run id")
        validate_checksum(self.intelligence_snapshot_checksum, "intelligence snapshot checksum")
        _sorted_identifiers(self.evidence_ids, "adjudication evidence ids", allow_empty=False)
        validate_checksum(self.evidence_set_checksum, "adjudication evidence-set checksum")
        expected = hashlib.sha256(canonical_json_bytes(self.evidence_ids)).hexdigest()
        if self.evidence_set_checksum != expected:
            raise ValueError("adjudication evidence-set checksum does not match")
        _exact_enum(self.decision, AdjudicationDecision, "adjudication decision")
        _optional_positive_int(self.superseded_adjudication_id, "superseded adjudication id")
        _utc(self.created_at, "adjudication creation time")
        self._validate_record_checksum("thesis evidence adjudication")


@dataclass(frozen=True)
class EvidenceDelta(_CanonicalRecord):
    history_comparability: HistoryComparability
    evidence_unchanged: bool
    added_evidence_ids: Tuple[str, ...] = ()
    removed_evidence_ids: Tuple[str, ...] = ()
    corrected_evidence_ids: Tuple[str, ...] = ()
    retracted_evidence_ids: Tuple[str, ...] = ()
    expired_evidence_ids: Tuple[str, ...] = ()
    conflicted_evidence_ids: Tuple[str, ...] = ()
    reason_codes: Tuple[str, ...] = ()

    def validate(self) -> None:
        _exact_record(self, EvidenceDelta, "evidence delta")
        _exact_enum(self.history_comparability, HistoryComparability, "history comparability")
        _exact_bool(self.evidence_unchanged, "evidence unchanged flag")
        for item in fields(self)[2:]:
            _sorted_identifiers(getattr(self, item.name), item.name.replace("_", " "))
        changed = any(getattr(self, item.name) for item in fields(self)[2:-1])
        if self.evidence_unchanged and (
            self.history_comparability is not HistoryComparability.COMPARABLE
            or changed
            or self.reason_codes
        ):
            raise ValueError("unchanged evidence requires closed comparable history")


@dataclass(frozen=True)
class HoldingReviewInputs(_CanonicalRecord):
    fund_code: str
    action: ActionKind
    brief_request_run_id: int
    intelligence_request_run_id: int
    thesis_review_state: ThesisMatchState
    review_evidence_items: Tuple[ReviewEvidenceItem, ...]
    official_event_codes: Tuple[OfficialEventCode, ...]
    omitted_work: Tuple[str, ...]
    official_negative_check_complete: bool
    intelligence_schedule_complete: bool
    intelligence_omitted_work: Tuple[str, ...]
    intelligence_degraded_sources: Tuple[str, ...]
    upstream_action_boundary: Tuple[str, ...]
    redemption_evidence: RedemptionEvidence
    remainder_intent: RemainderIntent
    exit_reason: ExitReason
    use_of_proceeds: UseOfProceeds
    previous_review_id: Optional[int]
    thesis_fingerprint: Optional[str]
    policy_version: str
    policy_checksum: str
    now: datetime

    @classmethod
    def minimal(
        cls,
        *,
        fund_code: str,
        action: ActionKind,
        brief_request_run_id: int,
        intelligence_request_run_id: int,
        now: datetime,
        policy_checksum: str,
    ) -> "HoldingReviewInputs":
        missing = RedemptionComponentState.MISSING
        return cls(
            fund_code=fund_code,
            action=action,
            brief_request_run_id=brief_request_run_id,
            intelligence_request_run_id=intelligence_request_run_id,
            thesis_review_state=ThesisMatchState.THESIS_MISSING,
            review_evidence_items=(),
            official_event_codes=(),
            omitted_work=(),
            official_negative_check_complete=False,
            intelligence_schedule_complete=False,
            intelligence_omitted_work=(),
            intelligence_degraded_sources=(),
            upstream_action_boundary=("research_only",),
            redemption_evidence=RedemptionEvidence(
                missing, missing, missing, missing, missing, missing, missing
            ),
            remainder_intent=RemainderIntent.UNKNOWN,
            exit_reason=ExitReason.UNKNOWN,
            use_of_proceeds=UseOfProceeds.UNKNOWN,
            previous_review_id=None,
            thesis_fingerprint=None,
            policy_version="1",
            policy_checksum=policy_checksum,
            now=now,
        )

    def validate(self) -> None:
        _exact_record(self, HoldingReviewInputs, "holding review inputs")
        _fund_code(self.fund_code)
        _exact_enum(self.action, ActionKind, "holding review action")
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("holding review action is not supported")
        _positive_int(self.brief_request_run_id, "brief request run id")
        _positive_int(self.intelligence_request_run_id, "intelligence request run id")
        _exact_enum(self.thesis_review_state, ThesisMatchState, "thesis review state")
        if type(self.review_evidence_items) is not tuple:
            raise ValueError("review evidence items must be an exact tuple")
        ids = []
        for item in self.review_evidence_items:
            if type(item) is not ReviewEvidenceItem:
                raise ValueError("review evidence items must contain exact records")
            item.validate()
            ids.append(item.evidence_id)
        if tuple(ids) != tuple(sorted(set(ids))):
            raise ValueError("review evidence item ids must be sorted and unique")
        _enum_tuple(self.official_event_codes, OfficialEventCode, "official event codes")
        if any(item not in _SUPPORTED_OFFICIAL_EVENTS for item in self.official_event_codes):
            raise ValueError("holding review input contains an unsupported official event")
        for values, name in (
            (self.omitted_work, "omitted work"),
            (self.intelligence_omitted_work, "intelligence omitted work"),
            (self.intelligence_degraded_sources, "intelligence degraded sources"),
            (self.upstream_action_boundary, "upstream action boundary"),
        ):
            _sorted_identifiers(values, name)
        for value, name in (
            (self.official_negative_check_complete, "official negative check complete"),
            (self.intelligence_schedule_complete, "intelligence schedule complete"),
        ):
            _exact_bool(value, name)
        if type(self.redemption_evidence) is not RedemptionEvidence:
            raise ValueError("redemption evidence must be exact")
        self.redemption_evidence.validate()
        _exact_enum(self.remainder_intent, RemainderIntent, "remainder intent")
        _exact_enum(self.exit_reason, ExitReason, "exit reason")
        _exact_enum(self.use_of_proceeds, UseOfProceeds, "use of proceeds")
        if self.action is ActionKind.CONTINUE_HOLDING and (
            self.remainder_intent is not RemainderIntent.UNKNOWN
            or self.exit_reason is not ExitReason.UNKNOWN
            or self.use_of_proceeds is not UseOfProceeds.UNKNOWN
        ):
            raise ValueError("continue holding rejects reduction and exit context")
        if self.action is ActionKind.REDUCE_TO_CASH and (
            self.exit_reason is not ExitReason.UNKNOWN
            or self.use_of_proceeds is not UseOfProceeds.UNKNOWN
        ):
            raise ValueError("partial reduction rejects full-exit context")
        if (
            self.action is ActionKind.FULL_EXIT
            and self.remainder_intent is not RemainderIntent.UNKNOWN
        ):
            raise ValueError("full exit rejects remainder intent")
        _optional_positive_int(self.previous_review_id, "previous review id")
        if self.thesis_fingerprint is not None:
            validate_checksum(self.thesis_fingerprint, "thesis fingerprint")
        validate_version(self.policy_version, "policy version")
        validate_checksum(self.policy_checksum, "policy checksum")
        _utc(self.now, "review time")


@dataclass(frozen=True)
class HoldingReviewResult(_CanonicalRecord):
    fund_code: str
    action: ActionKind
    flow_status: FlowStatus
    evidence_readiness: EvidenceReadiness
    history_comparability: HistoryComparability
    thesis_review_state: ThesisMatchState
    review_disposition: ReviewDisposition
    triggered_reviews: Tuple[TriggeredReviewCode, ...]
    redemption_feasibility: RedemptionFeasibility
    redemption_evidence: RedemptionEvidence
    sell_timing: str
    upstream_action_boundary: Tuple[str, ...]
    boundary: ReviewBoundary
    omitted_work: Tuple[str, ...]
    official_negative_check_complete: bool
    intelligence_schedule_complete: bool
    intelligence_omitted_work: Tuple[str, ...]
    intelligence_degraded_sources: Tuple[str, ...]
    action_review_source_sufficiency: ActionReviewSourceSufficiency
    hard_event_review: bool
    evidence_ids: Tuple[str, ...]
    evidence_delta: EvidenceDelta
    remainder_intent: RemainderIntent
    exit_reason: ExitReason
    use_of_proceeds: UseOfProceeds
    policy_version: str
    policy_checksum: str
    created_at: datetime

    def validate(self) -> None:
        _exact_record(self, HoldingReviewResult, "holding review result")
        _fund_code(self.fund_code)
        _exact_enum(self.action, ActionKind, "holding review action")
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("holding review action is not supported")
        for value, enum_type, name in (
            (self.flow_status, FlowStatus, "flow status"),
            (self.evidence_readiness, EvidenceReadiness, "evidence readiness"),
            (self.history_comparability, HistoryComparability, "history comparability"),
            (self.thesis_review_state, ThesisMatchState, "thesis review state"),
            (self.review_disposition, ReviewDisposition, "review disposition"),
            (self.redemption_feasibility, RedemptionFeasibility, "redemption feasibility"),
            (
                self.action_review_source_sufficiency,
                ActionReviewSourceSufficiency,
                "action review source sufficiency",
            ),
            (self.remainder_intent, RemainderIntent, "remainder intent"),
            (self.exit_reason, ExitReason, "exit reason"),
            (self.use_of_proceeds, UseOfProceeds, "use of proceeds"),
        ):
            _exact_enum(value, enum_type, name)
        _enum_tuple(self.triggered_reviews, TriggeredReviewCode, "triggered reviews")
        order = {item: index for index, item in enumerate(TriggeredReviewCode)}
        if tuple(order[item] for item in self.triggered_reviews) != tuple(
            sorted(order[item] for item in self.triggered_reviews)
        ):
            raise ValueError("triggered reviews must be in canonical order")
        if type(self.redemption_evidence) is not RedemptionEvidence:
            raise ValueError("redemption evidence must be exact")
        self.redemption_evidence.validate()
        for values, name in (
            (self.upstream_action_boundary, "upstream action boundary"),
            (self.omitted_work, "omitted work"),
            (self.intelligence_omitted_work, "intelligence omitted work"),
            (self.intelligence_degraded_sources, "intelligence degraded sources"),
            (self.evidence_ids, "evidence ids"),
        ):
            _sorted_identifiers(values, name)
        for value, name in (
            (self.official_negative_check_complete, "official negative check complete"),
            (self.intelligence_schedule_complete, "intelligence schedule complete"),
            (self.hard_event_review, "hard event review"),
        ):
            _exact_bool(value, name)
        if type(self.evidence_delta) is not EvidenceDelta:
            raise ValueError("holding review evidence delta must be exact")
        self.evidence_delta.validate()
        if self.history_comparability is not self.evidence_delta.history_comparability:
            raise ValueError("result history comparability and evidence delta differ")
        validate_version(self.policy_version, "policy version")
        validate_checksum(self.policy_checksum, "policy checksum")
        _utc(self.created_at, "review result creation time")
        if self.sell_timing != "insufficient_data" or self.boundary != ReviewBoundary():
            raise ValueError("holding review action boundary is invalid")
        self.boundary.validate()
        hard_trigger = TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW in self.triggered_reviews
        if self.review_disposition is ReviewDisposition.CONTINUE_OBSERVING:
            from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1

            forbidden = set(self.omitted_work) & set(
                HeldFundManualReviewPolicyV1().core_omission_prohibition
            )
            allowed_thesis_states = {
                ThesisMatchState.NO_MATCHING_EVIDENCE,
                ThesisMatchState.PRESENTED_MATCH_REJECTED,
            }
            if (
                self.flow_status is not FlowStatus.COMPLETE
                or self.evidence_readiness is not EvidenceReadiness.READY
                or self.thesis_review_state not in allowed_thesis_states
                or self.triggered_reviews
                or self.hard_event_review
                or forbidden
                or not self.official_negative_check_complete
                or not self.intelligence_schedule_complete
                or self.intelligence_omitted_work
                or self.intelligence_degraded_sources
            ):
                raise ValueError("continue observing evidence is incomplete")
        if self.hard_event_review != hard_trigger:
            raise ValueError("hard event review requires an authenticated hard-event trigger")
        if self.review_disposition in {
            ReviewDisposition.REDUCE_REVIEW,
            ReviewDisposition.EXIT_REVIEW,
        }:
            if (
                self.action_review_source_sufficiency
                is not ActionReviewSourceSufficiency.SUFFICIENT
                and not hard_trigger
            ):
                raise ValueError(
                    f"{self.review_disposition.value.replace('_', ' ')} source evidence is "
                    "insufficient; an authenticated hard-event trigger is required"
                )
            if (
                self.thesis_review_state is not ThesisMatchState.PRESENTED_MATCH_CONFIRMED
                and not hard_trigger
            ):
                raise ValueError("action review requires confirmed thesis evidence or a hard event")
        if self.review_disposition is ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED and (
            self.thesis_review_state
            not in {
                ThesisMatchState.MANUAL_REVIEW_PENDING,
                ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
            }
        ):
            raise ValueError("manual thesis review disposition requires unresolved thesis evidence")
        if self.flow_status is FlowStatus.FAILED and (
            self.review_disposition is not ReviewDisposition.ABSTAIN
            or self.evidence_readiness is not EvidenceReadiness.INSUFFICIENT_DATA
        ):
            raise ValueError("failed review flow must abstain with insufficient evidence")
        if self.review_disposition is ReviewDisposition.REDUCE_REVIEW and (
            self.action is not ActionKind.REDUCE_TO_CASH
        ):
            raise ValueError("reduce review requires the reduce-to-cash action")
        if self.review_disposition is ReviewDisposition.EXIT_REVIEW and (
            self.action is not ActionKind.FULL_EXIT
        ):
            raise ValueError("exit review requires the full-exit action")
        self._validate_redemption_state()
        self._validate_owner_context()

    def _validate_redemption_state(self) -> None:
        states = self.redemption_evidence.component_states()
        all_missing = all(item is RedemptionComponentState.MISSING for item in states)
        all_usable = all(item is RedemptionComponentState.USABLE for item in states)
        restricted = (
            self.redemption_evidence.current_redemption_restriction
            is RedemptionComponentState.RESTRICTED
        )
        restriction_triggered = (
            TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW in self.triggered_reviews
        )
        if restricted != restriction_triggered:
            raise ValueError("redemption restriction requires its authenticated review trigger")
        if self.action is ActionKind.CONTINUE_HOLDING:
            valid = (
                self.redemption_feasibility is RedemptionFeasibility.NOT_REQUESTED
                and all_missing
            )
        elif self.redemption_feasibility is RedemptionFeasibility.RESTRICTED:
            valid = restricted
        elif (
            self.redemption_feasibility
            is RedemptionFeasibility.EVIDENCE_COMPLETE_NON_AUTHORIZING
        ):
            valid = all_usable
        elif self.redemption_feasibility is RedemptionFeasibility.INSUFFICIENT_DATA:
            valid = not restricted and not all_usable
        else:
            valid = False
        if not valid:
            raise ValueError("redemption feasibility does not match its seven components")

    def _validate_owner_context(self) -> None:
        if self.action is ActionKind.CONTINUE_HOLDING and (
            self.remainder_intent is not RemainderIntent.UNKNOWN
            or self.exit_reason is not ExitReason.UNKNOWN
            or self.use_of_proceeds is not UseOfProceeds.UNKNOWN
        ):
            raise ValueError("continue holding rejects reduction and exit context")
        if self.action is ActionKind.REDUCE_TO_CASH and (
            self.exit_reason is not ExitReason.UNKNOWN
            or self.use_of_proceeds is not UseOfProceeds.UNKNOWN
        ):
            raise ValueError("partial reduction rejects full-exit context")
        if (
            self.action is ActionKind.FULL_EXIT
            and self.remainder_intent is not RemainderIntent.UNKNOWN
        ):
            raise ValueError("full exit rejects remainder intent")


@dataclass(frozen=True)
class HoldingReviewSnapshot(_AuthenticatedRecord):
    fund_code: str
    action: ActionKind
    brief_request_run_id: int
    brief_snapshot_id: int
    brief_snapshot_checksum: str
    intelligence_request_run_id: int
    intelligence_snapshot_id: int
    intelligence_snapshot_checksum: str
    thesis_match_projection_id: int
    thesis_match_projection_checksum: str
    active_thesis_state: BindingState
    active_thesis_id: Optional[int]
    active_thesis_fingerprint: Optional[str]
    adjudication_state: BindingState
    adjudication_id: Optional[int]
    adjudication_checksum: Optional[str]
    previous_review_id: Optional[int]
    result: HoldingReviewResult
    result_fingerprint: str
    policy_version: str
    policy_checksum: str
    created_at: datetime
    semantic_identity_checksum: str
    record_checksum: str

    def expected_semantic_identity_checksum(self) -> str:
        payload = {
            "action": self.action,
            "active_thesis_fingerprint": self.active_thesis_fingerprint,
            "active_thesis_id": self.active_thesis_id,
            "active_thesis_state": self.active_thesis_state,
            "adjudication_checksum": self.adjudication_checksum,
            "adjudication_id": self.adjudication_id,
            "adjudication_state": self.adjudication_state,
            "brief_request_run_id": self.brief_request_run_id,
            "brief_snapshot_checksum": self.brief_snapshot_checksum,
            "brief_snapshot_id": self.brief_snapshot_id,
            "exit_reason": self.result.exit_reason,
            "fund_code": self.fund_code,
            "intelligence_request_run_id": self.intelligence_request_run_id,
            "intelligence_snapshot_checksum": self.intelligence_snapshot_checksum,
            "intelligence_snapshot_id": self.intelligence_snapshot_id,
            "policy_checksum": self.policy_checksum,
            "remainder_intent": self.result.remainder_intent,
            "result_fingerprint": self.result_fingerprint,
            "thesis_match_projection_checksum": self.thesis_match_projection_checksum,
            "thesis_match_projection_id": self.thesis_match_projection_id,
            "use_of_proceeds": self.result.use_of_proceeds,
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def validate(self) -> None:
        _exact_record(self, HoldingReviewSnapshot, "holding review snapshot")
        _fund_code(self.fund_code)
        _exact_enum(self.action, ActionKind, "holding review snapshot action")
        if self.action not in _ALLOWED_ACTIONS:
            raise ValueError("holding review snapshot action is unsupported")
        for value, name in (
            (self.brief_request_run_id, "brief request run id"),
            (self.brief_snapshot_id, "brief snapshot id"),
            (self.intelligence_request_run_id, "intelligence request run id"),
            (self.intelligence_snapshot_id, "intelligence snapshot id"),
            (self.thesis_match_projection_id, "thesis match projection id"),
        ):
            _positive_int(value, name)
        for value, name in (
            (self.brief_snapshot_checksum, "brief snapshot checksum"),
            (self.intelligence_snapshot_checksum, "intelligence snapshot checksum"),
            (self.thesis_match_projection_checksum, "thesis match projection checksum"),
        ):
            validate_checksum(value, name)
        _exact_enum(self.active_thesis_state, BindingState, "active thesis state")
        _optional_positive_int(self.active_thesis_id, "active thesis id")
        if self.active_thesis_fingerprint is not None:
            validate_checksum(self.active_thesis_fingerprint, "active thesis fingerprint")
        thesis_present = (
            self.active_thesis_id is not None and self.active_thesis_fingerprint is not None
        )
        if thesis_present != (self.active_thesis_state is BindingState.PRESENT):
            raise ValueError("active thesis state and binding fields are inconsistent")
        _exact_enum(self.adjudication_state, BindingState, "adjudication state")
        _optional_positive_int(self.adjudication_id, "adjudication id")
        if self.adjudication_checksum is not None:
            validate_checksum(self.adjudication_checksum, "adjudication checksum")
        adjudication_present = (
            self.adjudication_id is not None and self.adjudication_checksum is not None
        )
        if adjudication_present != (self.adjudication_state is BindingState.PRESENT):
            raise ValueError("adjudication state and binding fields are inconsistent")
        if adjudication_present and not thesis_present:
            raise ValueError("an adjudication requires a present active thesis")
        _optional_positive_int(self.previous_review_id, "previous review id")
        if type(self.result) is not HoldingReviewResult:
            raise ValueError("holding review snapshot result must be exact")
        self.result.validate()
        if self.result.flow_status is FlowStatus.FAILED:
            raise ValueError("failed holding review results cannot be persisted")
        if self.result.fund_code != self.fund_code or self.result.action is not self.action:
            raise ValueError("holding review snapshot subject does not match its result")
        if self.active_thesis_state is BindingState.MISSING:
            if self.result.thesis_review_state is not ThesisMatchState.THESIS_MISSING:
                raise ValueError("missing thesis binding requires thesis-missing result state")
            if self.adjudication_state is not BindingState.MISSING:
                raise ValueError("missing thesis cannot have an adjudication")
        elif self.result.thesis_review_state in {
            ThesisMatchState.THESIS_MISSING,
            ThesisMatchState.THESIS_BINDING_INVALID,
        }:
            raise ValueError("present thesis binding conflicts with the result thesis state")
        adjudicated_states = {
            ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
            ThesisMatchState.PRESENTED_MATCH_REJECTED,
            ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
        }
        if (self.result.thesis_review_state in adjudicated_states) != adjudication_present:
            raise ValueError("adjudication binding and result thesis state are inconsistent")
        validate_checksum(self.result_fingerprint, "review result fingerprint")
        if self.result_fingerprint != self.result.checksum():
            raise ValueError("review result fingerprint does not match")
        validate_version(self.policy_version, "holding review policy version")
        validate_checksum(self.policy_checksum, "holding review policy checksum")
        if (
            self.result.policy_version != self.policy_version
            or self.result.policy_checksum != self.policy_checksum
        ):
            raise ValueError("snapshot and result policy bindings differ")
        _utc(self.created_at, "holding review snapshot creation time")
        if self.created_at < self.result.created_at:
            raise ValueError("review snapshot cannot precede its result")
        validate_checksum(self.semantic_identity_checksum, "semantic identity checksum")
        if self.semantic_identity_checksum != self.expected_semantic_identity_checksum():
            raise ValueError("holding review semantic identity checksum does not match")
        self._validate_record_checksum("holding review snapshot")


@dataclass(frozen=True)
class HoldingReviewOutcome(_CanonicalRecord):
    flow_status: FlowStatus
    review_snapshot: HoldingReviewSnapshot

    def validate(self) -> None:
        _exact_record(self, HoldingReviewOutcome, "holding review outcome")
        _exact_enum(self.flow_status, FlowStatus, "holding review outcome flow status")
        if self.flow_status is FlowStatus.FAILED:
            raise ValueError("stored holding review outcome cannot be failed")
        if type(self.review_snapshot) is not HoldingReviewSnapshot:
            raise ValueError("holding review outcome snapshot must be exact")
        self.review_snapshot.validate()
        if self.flow_status is not self.review_snapshot.result.flow_status:
            raise ValueError("holding review outcome and result flow states differ")


@dataclass(frozen=True)
class TransientHoldingReviewOutcome(_CanonicalRecord):
    flow_status: FlowStatus
    review_snapshot: None
    missing_snapshot_codes: Tuple[str, ...]
    boundary: ReviewBoundary = ReviewBoundary()

    def validate(self) -> None:
        _exact_record(self, TransientHoldingReviewOutcome, "transient holding review outcome")
        _exact_enum(self.flow_status, FlowStatus, "transient outcome flow status")
        if self.flow_status not in {FlowStatus.PARTIAL, FlowStatus.FAILED}:
            raise ValueError("transient holding review outcome must be partial or failed")
        if self.review_snapshot is not None:
            raise ValueError("transient holding review outcome cannot contain a snapshot")
        _sorted_identifiers(
            self.missing_snapshot_codes,
            "missing snapshot codes",
            allow_empty=False,
        )
        if type(self.boundary) is not ReviewBoundary:
            raise ValueError("transient holding review boundary must be exact")
        self.boundary.validate()
