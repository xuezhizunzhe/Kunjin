from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple
from urllib.parse import urlparse

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import (
    MAX_PUBLIC_TEXT_CHARS,
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

_FUND_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_ALLOWED_ACTIONS = frozenset(
    (ActionKind.CONTINUE_HOLDING, ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT)
)
_MAX_ANNOUNCEMENT_CONTENT_BYTES = 512 * 1024


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


def _positive_int_tuple(value: object, name: str, *, allow_empty: bool = True) -> None:
    if type(value) is not tuple or (not allow_empty and not value):
        raise ValueError(f"{name} must be an exact tuple")
    for item in value:
        _positive_int(item, name)
    if len(value) != len(set(value)) or value != tuple(sorted(value)):
        raise ValueError(f"{name} must be a sorted unique exact tuple")


def _bool_tuple(value: object, name: str, expected_length: int) -> None:
    if type(value) is not tuple or len(value) != expected_length:
        raise ValueError(f"{name} must align with the evidence set")
    for item in value:
        _exact_bool(item, name)


def _enum_tuple(value: object, enum_type: type[Enum], name: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    for item in value:
        _exact_enum(item, enum_type, name)
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must be unique")


def _https_url(value: object, name: str) -> str:
    if type(value) is not str or len(value) > MAX_PUBLIC_TEXT_CHARS:
        raise ValueError(f"{name} must be a canonical public HTTPS URL")
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name} must be a canonical public HTTPS URL") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname != parsed.hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a canonical public HTTPS URL")
    return value


class _CanonicalRecord:
    def validate(self) -> None:
        raise NotImplementedError

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            item.name: canonical_value(getattr(self, item.name))
            for item in fields(self)
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_dict())

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


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


@dataclass(frozen=True)
class OfficialAnnouncementContent(_CanonicalRecord):
    fund_code: str
    brief_request_run_id: int
    listing_source_document_id: str
    canonical_announcement_url: str
    announcement_title: str
    published_at: datetime
    publisher: str
    registered_manager_domain: str
    lineage_kind: str
    integrity_status: str
    integrity_checked_at: datetime
    retrieved_at: datetime
    normalized_content: str
    normalized_content_sha256: str
    normalized_content_bytes: int

    def validate(self) -> None:
        _exact_record(self, OfficialAnnouncementContent, "official announcement content")
        _fund_code(self.fund_code)
        _positive_int(self.brief_request_run_id, "brief request run id")
        validate_identifier(self.listing_source_document_id, "listing source document id")
        _https_url(self.canonical_announcement_url, "announcement URL")
        validate_public_text(self.announcement_title, "announcement title")
        validate_public_text(self.publisher, "announcement publisher")
        validate_public_text(self.registered_manager_domain, "registered manager domain")
        validate_identifier(self.lineage_kind, "announcement lineage kind")
        validate_identifier(self.integrity_status, "announcement integrity status")
        _utc(self.published_at, "announcement publication time")
        _utc(self.integrity_checked_at, "announcement integrity check time")
        _utc(self.retrieved_at, "announcement retrieval time")
        if self.integrity_checked_at < self.published_at or self.retrieved_at < self.published_at:
            raise ValueError("announcement checks cannot precede publication")
        if type(self.normalized_content) is not str or not self.normalized_content:
            raise ValueError("normalized announcement content must be a non-empty exact string")
        encoded = self.normalized_content.encode("utf-8")
        if len(encoded) > _MAX_ANNOUNCEMENT_CONTENT_BYTES:
            raise ValueError("normalized announcement content exceeds the bounded byte limit")
        if type(self.normalized_content_bytes) is not int or self.normalized_content_bytes != len(
            encoded
        ):
            raise ValueError("normalized announcement content byte count is invalid")
        validate_checksum(self.normalized_content_sha256, "normalized content checksum")
        if hashlib.sha256(encoded).hexdigest() != self.normalized_content_sha256:
            raise ValueError("normalized announcement content checksum does not match")


@dataclass(frozen=True)
class HeldReviewOfficialEventProjection(_CanonicalRecord):
    fund_code: str
    brief_request_run_id: int
    announcement_content_id: int
    event_code: OfficialEventCode
    triggered_review: Optional[TriggeredReviewCode]
    active: bool
    corrected: bool
    retracted: bool
    policy_version: str
    policy_checksum: str
    created_at: datetime
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, HeldReviewOfficialEventProjection, "official event projection")
        _fund_code(self.fund_code)
        _positive_int(self.brief_request_run_id, "brief request run id")
        _positive_int(self.announcement_content_id, "announcement content id")
        _exact_enum(self.event_code, OfficialEventCode, "official event code")
        if self.triggered_review is not None:
            _exact_enum(self.triggered_review, TriggeredReviewCode, "triggered review code")
        for value, name in (
            (self.active, "official event active flag"),
            (self.corrected, "official event corrected flag"),
            (self.retracted, "official event retracted flag"),
        ):
            _exact_bool(value, name)
        if self.active and (self.corrected or self.retracted):
            raise ValueError("corrected or retracted official events cannot be active")
        validate_version(self.policy_version, "official event policy version")
        validate_checksum(self.policy_checksum, "official event policy checksum")
        _utc(self.created_at, "official event projection creation time")
        validate_checksum(self.record_checksum, "official event projection record checksum")


@dataclass(frozen=True)
class ThesisMatchProjection(_CanonicalRecord):
    fund_code: str
    thesis_id: int
    thesis_fingerprint: str
    matcher_policy_version: str
    matcher_policy_checksum: str
    intelligence_request_run_id: int
    intelligence_snapshot_checksum: str
    projection_state: ThesisMatchProjectionState
    evidence_ids: Tuple[str, ...]
    source_tiers: Tuple[int, ...]
    lineage_kinds: Tuple[str, ...]
    current_flags: Tuple[bool, ...]
    integrity_flags: Tuple[bool, ...]
    conflict_flags: Tuple[bool, ...]
    direct_subject_binding_flags: Tuple[bool, ...]
    created_at: datetime
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, ThesisMatchProjection, "thesis match projection")
        _fund_code(self.fund_code)
        _positive_int(self.thesis_id, "thesis id")
        validate_checksum(self.thesis_fingerprint, "thesis fingerprint")
        validate_version(self.matcher_policy_version, "matcher policy version")
        validate_checksum(self.matcher_policy_checksum, "matcher policy checksum")
        _positive_int(self.intelligence_request_run_id, "intelligence request run id")
        validate_checksum(self.intelligence_snapshot_checksum, "intelligence snapshot checksum")
        _exact_enum(self.projection_state, ThesisMatchProjectionState, "projection state")
        _sorted_identifiers(self.evidence_ids, "projection evidence ids")
        count = len(self.evidence_ids)
        if type(self.source_tiers) is not tuple or len(self.source_tiers) != count:
            raise ValueError("projection source tiers must align with the evidence set")
        if any(type(item) is not int or item not in (1, 2) for item in self.source_tiers):
            raise ValueError("projection source tiers must contain exact public tier integers")
        if type(self.lineage_kinds) is not tuple or len(self.lineage_kinds) != count:
            raise ValueError("projection lineage kinds must align with the evidence set")
        for item in self.lineage_kinds:
            validate_identifier(item, "projection lineage kind")
        for values, name in (
            (self.current_flags, "projection current flags"),
            (self.integrity_flags, "projection integrity flags"),
            (self.conflict_flags, "projection conflict flags"),
            (self.direct_subject_binding_flags, "projection direct subject flags"),
        ):
            _bool_tuple(values, name, count)
        has_evidence = bool(self.evidence_ids)
        if has_evidence != (
            self.projection_state is ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
        ):
            raise ValueError("projection state and evidence set are inconsistent")
        _utc(self.created_at, "thesis match projection creation time")
        validate_checksum(self.record_checksum, "thesis match projection record checksum")


@dataclass(frozen=True)
class ThesisEvidenceAdjudication(_CanonicalRecord):
    adjudication_id: int
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
    semantic_identity_checksum: str
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, ThesisEvidenceAdjudication, "thesis evidence adjudication")
        _positive_int(self.adjudication_id, "adjudication id")
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
        expected_evidence_checksum = hashlib.sha256(
            canonical_json_bytes(self.evidence_ids)
        ).hexdigest()
        if self.evidence_set_checksum != expected_evidence_checksum:
            raise ValueError("adjudication evidence-set checksum does not match")
        _exact_enum(self.decision, AdjudicationDecision, "adjudication decision")
        _optional_positive_int(self.superseded_adjudication_id, "superseded adjudication id")
        if self.superseded_adjudication_id == self.adjudication_id:
            raise ValueError("an adjudication cannot supersede itself")
        _utc(self.created_at, "adjudication creation time")
        validate_checksum(self.semantic_identity_checksum, "semantic identity checksum")
        validate_checksum(self.record_checksum, "adjudication record checksum")


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
class ReviewEvidenceItem(_CanonicalRecord):
    evidence_id: str
    source_tier: int
    lineage_kind: str
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
        validate_identifier(self.lineage_kind, "review evidence lineage kind")
        for item in fields(self)[3:]:
            _exact_bool(getattr(self, item.name), item.name.replace("_", " "))
        if self.original_lineage != (self.lineage_kind == "original"):
            raise ValueError("review evidence lineage fields are inconsistent")


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
        evidence_ids = []
        for item in self.review_evidence_items:
            if type(item) is not ReviewEvidenceItem:
                raise ValueError("review evidence items must contain exact records")
            item.validate()
            evidence_ids.append(item.evidence_id)
        if tuple(evidence_ids) != tuple(sorted(set(evidence_ids))):
            raise ValueError("review evidence item ids must be sorted and unique")
        _enum_tuple(self.official_event_codes, OfficialEventCode, "official event codes")
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
        ):
            _exact_enum(value, enum_type, name)
        _enum_tuple(self.triggered_reviews, TriggeredReviewCode, "triggered reviews")
        canonical_review_order = {item: index for index, item in enumerate(TriggeredReviewCode)}
        if tuple(canonical_review_order[item] for item in self.triggered_reviews) != tuple(
            sorted(canonical_review_order[item] for item in self.triggered_reviews)
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
        validate_version(self.policy_version, "policy version")
        validate_checksum(self.policy_checksum, "policy checksum")
        _utc(self.created_at, "review result creation time")
        if self.sell_timing != "insufficient_data" or self.boundary != ReviewBoundary():
            raise ValueError("holding review action boundary is invalid")
        self.boundary.validate()
        if self.review_disposition is ReviewDisposition.CONTINUE_OBSERVING:
            from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1

            forbidden = set(self.omitted_work) & set(
                HeldFundManualReviewPolicyV1().core_omission_prohibition
            )
            if (
                forbidden
                or not self.official_negative_check_complete
                or not self.intelligence_schedule_complete
                or self.intelligence_omitted_work
                or self.intelligence_degraded_sources
            ):
                raise ValueError("continue observing evidence is incomplete")
        if self.review_disposition in {
            ReviewDisposition.REDUCE_REVIEW,
            ReviewDisposition.EXIT_REVIEW,
        } and (
            self.action_review_source_sufficiency
            is not ActionReviewSourceSufficiency.SUFFICIENT
            and not self.hard_event_review
        ):
            disposition = self.review_disposition.value.replace("_", " ")
            raise ValueError(f"{disposition} source evidence is insufficient")
        if self.review_disposition is ReviewDisposition.REDUCE_REVIEW and (
            self.action is not ActionKind.REDUCE_TO_CASH
        ):
            raise ValueError("reduce review requires the reduce-to-cash action")
        if self.review_disposition is ReviewDisposition.EXIT_REVIEW and (
            self.action is not ActionKind.FULL_EXIT
        ):
            raise ValueError("exit review requires the full-exit action")
        if self.redemption_feasibility is RedemptionFeasibility.NOT_REQUESTED and (
            self.action is not ActionKind.CONTINUE_HOLDING
        ):
            raise ValueError("redemption evidence is required for reduction and exit questions")


@dataclass(frozen=True)
class HoldingReviewSnapshot(_CanonicalRecord):
    review_snapshot_id: int
    result: HoldingReviewResult
    brief_request_run_id: int
    brief_snapshot_id: int
    brief_snapshot_checksum: str
    intelligence_request_run_id: int
    intelligence_snapshot_checksum: str
    thesis_id: Optional[int]
    thesis_fingerprint: Optional[str]
    adjudication_id: Optional[int]
    previous_comparable_review_id: Optional[int]
    result_fingerprint: str
    record_checksum: str

    def validate(self) -> None:
        _exact_record(self, HoldingReviewSnapshot, "holding review snapshot")
        _positive_int(self.review_snapshot_id, "review snapshot id")
        if type(self.result) is not HoldingReviewResult:
            raise ValueError("holding review snapshot result must be exact")
        self.result.validate()
        for value, name in (
            (self.brief_request_run_id, "brief request run id"),
            (self.brief_snapshot_id, "brief snapshot id"),
            (self.intelligence_request_run_id, "intelligence request run id"),
        ):
            _positive_int(value, name)
        validate_checksum(self.brief_snapshot_checksum, "brief snapshot checksum")
        validate_checksum(self.intelligence_snapshot_checksum, "intelligence snapshot checksum")
        _optional_positive_int(self.thesis_id, "thesis id")
        if (self.thesis_id is None) != (self.thesis_fingerprint is None):
            raise ValueError("snapshot thesis id and fingerprint must be present together")
        if self.thesis_fingerprint is not None:
            validate_checksum(self.thesis_fingerprint, "thesis fingerprint")
        _optional_positive_int(self.adjudication_id, "adjudication id")
        _optional_positive_int(
            self.previous_comparable_review_id, "previous comparable review id"
        )
        validate_checksum(self.result_fingerprint, "review result fingerprint")
        if self.result_fingerprint != self.result.checksum():
            raise ValueError("review result fingerprint does not match")
        validate_checksum(self.record_checksum, "holding review snapshot record checksum")


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
