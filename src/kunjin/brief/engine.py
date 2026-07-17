from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Tuple

from kunjin.brief.d2 import D2RelationshipSet
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefEvidenceState,
    BriefEvidenceStatus,
    BriefResolutionBinding,
    BriefState,
    OfficialEvent,
    OfficialEventCode,
    canonical_event_affected_actions,
    thesis_record_fingerprint,
)
from kunjin.brief.policy import HeldFundBriefPolicyV1
from kunjin.decision.models import (
    ActionMaturity,
    ActionRoute,
    ActionState,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestFieldResolution,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceFieldState,
    canonical_json_bytes,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
    validate_public_text_tuple,
    validate_request_id,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.storage.repository import Repository

_CANONICAL_ACTION_SHAPES = {
    ("fact_research", "continue_holding"),
    ("fact_research", "reduce_to_cash"),
    ("fact_research", "full_exit"),
    ("fact_research", "switch_reduce", "switch_buy"),
}
_HARD_EXIT_EVENTS = {
    OfficialEventCode.FUND_LIQUIDATION_NOTICE,
    OfficialEventCode.FUND_TERMINATION_NOTICE,
}
_WATCH_EVENTS = {
    OfficialEventCode.MANAGER_CHANGE_NOTICE,
    OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE,
    OfficialEventCode.FEE_CHANGE_NOTICE,
    OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
}
_IDENTITY_MARKERS = ("identity", "share_class")
_TRANSACTION_ACTION_IDS = {
    "reduce_to_cash",
    "full_exit",
    "switch_reduce",
    "switch_buy",
}
_THESIS_BINDING_KEY = secrets.token_bytes(32)
_SOURCE_RESOLUTION_BINDING_KEY = secrets.token_bytes(32)


class ThesisReviewState(str, Enum):
    INTACT = "intact"
    TRIGGERED = "triggered"
    UNKNOWN = "unknown"


def _unique(values: Tuple[str, ...] | list[str]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _sorted_unique(values: Tuple[str, ...] | list[str] | set[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class BriefSourceResolution:
    action_id: str
    field_id: str
    resolution: RequestFieldResolution
    source_states: Tuple[SourceFieldState, ...]
    evidence_ids: Tuple[str, ...] = ()
    acceptable_alternative_ids: Tuple[str, ...] = ()
    manual_supplementation_codes: Tuple[str, ...] = ()
    source_attempt_id: int = 0
    request_run_id: int = 0
    request_id: str = ""
    fund_code: str = ""
    source_id: str = ""
    source_field_id: str = ""
    evaluated_at: Optional[datetime] = None
    binding_mac: str = ""

    def to_snapshot_binding(self) -> BriefResolutionBinding:
        self.validate()
        binding = BriefResolutionBinding(
            action_id=self.action_id,
            field_id=self.field_id,
            resolution=self.resolution,
            source_states=self.source_states,
            source_attempt_id=self.source_attempt_id,
            source_id=self.source_id,
            source_field_id=self.source_field_id,
            evaluated_at=self.evaluated_at,
        )
        binding.validate()
        return binding

    def binding_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "acceptable_alternative_ids": self.acceptable_alternative_ids,
                "action_id": self.action_id,
                "evaluated_at": self.evaluated_at,
                "evidence_ids": self.evidence_ids,
                "field_id": self.field_id,
                "fund_code": self.fund_code,
                "manual_supplementation_codes": self.manual_supplementation_codes,
                "request_id": self.request_id,
                "request_run_id": self.request_run_id,
                "resolution": self.resolution,
                "source_attempt_id": self.source_attempt_id,
                "source_field_id": self.source_field_id,
                "source_id": self.source_id,
                "source_states": self.source_states,
            }
        )

    def validate(self) -> None:
        if type(self) is not BriefSourceResolution:
            raise ValueError("source resolution subclasses are not accepted")
        validate_exact_dataclass_state(self, "brief source resolution")
        validate_identifier(self.action_id, "source resolution action id")
        validate_identifier(self.field_id, "source resolution field id")
        if type(self.resolution) is not RequestFieldResolution:
            raise ValueError("source resolution must use an exact RequestFieldResolution")
        if type(self.source_states) is not tuple or not self.source_states:
            raise ValueError("source resolution states must be a non-empty exact tuple")
        if any(type(item) is not SourceFieldState for item in self.source_states):
            raise ValueError("source resolution states must contain exact SourceFieldState values")
        validate_identifier_tuple(self.evidence_ids, "source resolution evidence ids")
        validate_identifier_tuple(
            self.acceptable_alternative_ids,
            "source resolution acceptable alternative ids",
        )
        validate_identifier_tuple(
            self.manual_supplementation_codes,
            "source resolution manual supplementation codes",
        )
        for value, name in (
            (self.source_attempt_id, "source attempt id"),
            (self.request_run_id, "request run id"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"source resolution {name} must be positive")
        validate_request_id(self.request_id)
        if (
            type(self.fund_code) is not str
            or len(self.fund_code) != 6
            or not self.fund_code.isascii()
            or not self.fund_code.isdigit()
        ):
            raise ValueError("source resolution fund code must be six ASCII digits")
        validate_identifier(self.source_id, "source resolution source id")
        validate_identifier(self.source_field_id, "source resolution source field id")
        if type(self.evaluated_at) is not datetime or self.evaluated_at.tzinfo is None:
            raise ValueError("source resolution evaluation time must be timezone-aware")
        validate_checksum(self.binding_mac, "source resolution binding MAC")
        if not hmac.compare_digest(self.binding_mac, _source_resolution_mac(self)):
            raise ValueError("source resolution binding is not authenticated")
        if (
            self.resolution is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
            and not self.manual_supplementation_codes
        ):
            raise ValueError("manual source resolution requires supplementation codes")
        if self.resolution is RequestFieldResolution.USABLE and not any(
            item is SourceFieldState.HEALTHY for item in self.source_states
        ):
            raise ValueError("usable source resolution requires a healthy source state")
        if self.resolution is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED and any(
            item not in {SourceFieldState.UNAVAILABLE, SourceFieldState.UNSUPPORTED}
            for item in self.source_states
        ):
            raise ValueError("manual source resolution requires exhausted source states")


def _source_resolution_mac(resolution: BriefSourceResolution) -> str:
    return hmac.new(
        _SOURCE_RESOLUTION_BINDING_KEY,
        resolution.binding_bytes(),
        hashlib.sha256,
    ).hexdigest()


def source_attempt_resolution(
    attempt: SourceAttempt,
) -> Tuple[RequestFieldResolution, Tuple[SourceFieldState, ...]]:
    if type(attempt) is not SourceAttempt:
        raise ValueError("source resolution requires an exact SourceAttempt")
    attempt.validate()
    registry = SourceRegistryV1()
    registry.validate()
    try:
        field_policy = next(
            field
            for source in registry.sources
            if source.source_id == attempt.source_id
            for field in source.fields
            if field.field_id == attempt.field_id
        )
    except StopIteration:
        raise ValueError("source resolution attempt is outside the source registry") from None
    maximum_age_seconds = field_policy.freshness.maximum_age_seconds
    if field_policy.freshness.integrity_check_max_age_seconds is not None:
        maximum_age_seconds = min(
            maximum_age_seconds,
            field_policy.freshness.integrity_check_max_age_seconds,
        )
    successful_and_current = (
        attempt.outcome in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
        and attempt.data_as_of is not None
        and attempt.finished_at - attempt.data_as_of <= timedelta(seconds=maximum_age_seconds)
    )
    if attempt.outcome in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}:
        return (
            (
                RequestFieldResolution.USABLE,
                (SourceFieldState.HEALTHY,),
            )
            if successful_and_current
            else (
                RequestFieldResolution.PARTIAL,
                (SourceFieldState.DEGRADED,),
            )
        )
    return {
        SourceAttemptOutcome.TRANSIENT_FAILURE: (
            RequestFieldResolution.PARTIAL,
            (SourceFieldState.COOLDOWN,),
        ),
        SourceAttemptOutcome.SKIPPED_COOLDOWN: (
            RequestFieldResolution.PARTIAL,
            (SourceFieldState.COOLDOWN,),
        ),
        SourceAttemptOutcome.UNAVAILABLE: (
            RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED,
            (SourceFieldState.UNAVAILABLE,),
        ),
        SourceAttemptOutcome.UNSUPPORTED: (
            RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED,
            (SourceFieldState.UNSUPPORTED,),
        ),
        SourceAttemptOutcome.CANCELLED: (
            RequestFieldResolution.PARTIAL,
            (SourceFieldState.NOT_CHECKED,),
        ),
        SourceAttemptOutcome.EXPIRED: (
            RequestFieldResolution.PARTIAL,
            (SourceFieldState.NOT_CHECKED,),
        ),
    }[attempt.outcome]


def load_brief_source_resolution(
    audit_store: DecisionAuditStore,
    source_attempt_id: int,
    *,
    action_id: str,
    field_id: str,
    evidence_ids: Tuple[str, ...] = (),
    acceptable_alternative_ids: Tuple[str, ...] = (),
    manual_supplementation_codes: Tuple[str, ...] = (),
) -> BriefSourceResolution:
    if type(audit_store) is not DecisionAuditStore:
        raise ValueError("source resolution loader requires an exact DecisionAuditStore")
    try:
        stored = audit_store.authenticated_source_attempt(source_attempt_id)
    except (DecisionAuditStoreError, TypeError, ValueError):
        raise ValueError("source resolution attempt is not authenticated") from None
    attempt = stored.attempt
    expected_source_field = (
        "fund_manager_product_announcement" if field_id == "official_events" else field_id
    )
    if attempt.field_id != expected_source_field:
        raise ValueError("source resolution field does not match its authenticated attempt")
    if field_id == "official_events" and attempt.source_id != "fund_manager_official_documents":
        raise ValueError("official event resolution requires an authenticated official source")
    resolution_state, source_states = source_attempt_resolution(attempt)
    if (
        resolution_state is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
        and not manual_supplementation_codes
    ):
        manual_supplementation_codes = (f"{field_id}_manual_supplement_required",)
    fund_code = attempt.subject_key.removeprefix("fund:")
    resolution = BriefSourceResolution(
        action_id=action_id,
        field_id=field_id,
        resolution=resolution_state,
        source_states=source_states,
        evidence_ids=evidence_ids,
        acceptable_alternative_ids=acceptable_alternative_ids,
        manual_supplementation_codes=manual_supplementation_codes,
        source_attempt_id=stored.id,
        request_run_id=stored.request_run_id,
        request_id=stored.request_id,
        fund_code=fund_code,
        source_id=attempt.source_id,
        source_field_id=attempt.field_id,
        evaluated_at=attempt.finished_at,
        binding_mac="0" * 64,
    )
    resolution = replace(resolution, binding_mac=_source_resolution_mac(resolution))
    resolution.validate()
    return resolution


@dataclass(frozen=True)
class ConfirmedThesisState:
    thesis_id: int
    thesis_fingerprint: str
    fund_code: str
    reason: str
    horizon: str
    invalidation_conditions: Tuple[str, ...]
    created_at: datetime
    reviewed_at: Optional[datetime]
    review_state: ThesisReviewState
    evidence_ids: Tuple[str, ...]
    review_source_attempt_id: int
    review_request_id: str
    binding_mac: str

    def binding_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "created_at": self.created_at,
                "evidence_ids": self.evidence_ids,
                "fund_code": self.fund_code,
                "horizon": self.horizon,
                "invalidation_conditions": self.invalidation_conditions,
                "reason": self.reason,
                "review_state": self.review_state,
                "reviewed_at": self.reviewed_at,
                "review_request_id": self.review_request_id,
                "review_source_attempt_id": self.review_source_attempt_id,
                "thesis_fingerprint": self.thesis_fingerprint,
                "thesis_id": self.thesis_id,
            }
        )

    def validate(self) -> None:
        if type(self) is not ConfirmedThesisState:
            raise ValueError("confirmed thesis state subclasses are not accepted")
        validate_exact_dataclass_state(self, "confirmed thesis state")
        if type(self.thesis_id) is not int or self.thesis_id <= 0:
            raise ValueError("confirmed thesis id must be positive")
        validate_checksum(self.thesis_fingerprint, "confirmed thesis fingerprint")
        if (
            type(self.fund_code) is not str
            or len(self.fund_code) != 6
            or not self.fund_code.isascii()
            or not self.fund_code.isdigit()
        ):
            raise ValueError("confirmed thesis fund code must be six ASCII digits")
        validate_public_text(self.reason, "confirmed thesis reason")
        validate_public_text(self.horizon, "confirmed thesis horizon")
        validate_public_text_tuple(
            self.invalidation_conditions,
            "confirmed thesis invalidation conditions",
            allow_empty=False,
        )
        if type(self.created_at) is not datetime or self.created_at.tzinfo is None:
            raise ValueError("confirmed thesis creation time must be timezone-aware")
        if type(self.review_state) is not ThesisReviewState:
            raise ValueError("confirmed thesis review state must be exact")
        if self.review_state is ThesisReviewState.UNKNOWN:
            if self.reviewed_at is not None:
                raise ValueError("unknown thesis review cannot claim a review time")
        elif (
            type(self.reviewed_at) is not datetime
            or self.reviewed_at.tzinfo is None
            or self.reviewed_at < self.created_at
        ):
            raise ValueError("confirmed thesis review must follow thesis creation")
        if type(self.review_source_attempt_id) is not int or self.review_source_attempt_id <= 0:
            raise ValueError("confirmed thesis review source attempt must be positive")
        validate_request_id(self.review_request_id)
        validate_identifier_tuple(self.evidence_ids, "confirmed thesis evidence ids")
        if self.review_state is ThesisReviewState.TRIGGERED and not self.evidence_ids:
            raise ValueError("triggered thesis review requires bound evidence")
        validate_checksum(self.binding_mac, "confirmed thesis binding MAC")
        if not hmac.compare_digest(self.binding_mac, _thesis_binding_mac(self)):
            raise ValueError("confirmed thesis binding is not authenticated")


def _thesis_binding_mac(state: ConfirmedThesisState) -> str:
    return hmac.new(_THESIS_BINDING_KEY, state.binding_bytes(), hashlib.sha256).hexdigest()


def load_confirmed_thesis_state(
    repository: Repository,
    thesis_id: int,
    *,
    review_resolution: BriefSourceResolution,
    evidence_ids: Tuple[str, ...],
) -> ConfirmedThesisState:
    if type(repository) is not Repository:
        raise ValueError("confirmed thesis loader requires an exact Repository")
    if type(thesis_id) is not int or thesis_id <= 0:
        raise ValueError("confirmed thesis id must be a positive integer")
    if type(review_resolution) is not BriefSourceResolution:
        raise ValueError("confirmed thesis loader requires an exact source resolution")
    review_resolution.validate()
    thesis = repository.get_thesis(thesis_id)
    if thesis is None or not thesis.active:
        raise ValueError("no active stored thesis is available for the id")
    thesis.validate()
    if (
        review_resolution.fund_code != thesis.fund_code
        or review_resolution.action_id != "continue_holding"
        or review_resolution.field_id != "official_events"
        or review_resolution.resolution is not RequestFieldResolution.USABLE
    ):
        raise ValueError("confirmed thesis review resolution is not usable for the fund")
    thesis_fingerprint = thesis_record_fingerprint(thesis_id, thesis)
    if review_resolution.evaluated_at < thesis.created_at:
        review_state = ThesisReviewState.UNKNOWN
    elif evidence_ids:
        review_state = ThesisReviewState.TRIGGERED
    else:
        review_state = ThesisReviewState.INTACT
    state = ConfirmedThesisState(
        thesis_id=thesis_id,
        thesis_fingerprint=thesis_fingerprint,
        fund_code=thesis.fund_code,
        reason=thesis.rationale,
        horizon=thesis.horizon,
        invalidation_conditions=(thesis.invalidation,),
        created_at=thesis.created_at,
        reviewed_at=(
            None if review_state is ThesisReviewState.UNKNOWN else review_resolution.evaluated_at
        ),
        review_state=review_state,
        evidence_ids=evidence_ids,
        review_source_attempt_id=review_resolution.source_attempt_id,
        review_request_id=review_resolution.request_id,
        binding_mac="0" * 64,
    )
    state = replace(state, binding_mac=_thesis_binding_mac(state))
    state.validate()
    return state


EvidenceStatus = BriefEvidenceStatus


@dataclass(frozen=True)
class HeldFundBriefEvaluation:
    sync_status: EvidenceStatus
    decision_evidence_status: EvidenceStatus
    interpretations: Tuple[BriefActionInterpretation, ...]
    primary_state: BriefState
    action_maturity: ActionMaturity
    constraints: Tuple[str, ...]
    triggered_reviews: Tuple[str, ...]
    affected_action_abstentions: Tuple[str, ...]
    blocking_codes: Tuple[str, ...]
    missing_fields: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    resolution_lineage_ids: Tuple[str, ...]
    resolution_bindings: Tuple[BriefResolutionBinding, ...]

    def validate(self) -> None:
        if type(self) is not HeldFundBriefEvaluation:
            raise ValueError("held fund brief evaluation subclasses are not accepted")
        validate_exact_dataclass_state(self, "held fund brief evaluation")
        self.sync_status.validate()
        self.decision_evidence_status.validate()
        if type(self.interpretations) is not tuple or not self.interpretations:
            raise ValueError("brief evaluation requires interpretations")
        action_ids = []
        for item in self.interpretations:
            if type(item) is not BriefActionInterpretation:
                raise ValueError("brief evaluation requires exact interpretations")
            item.validate()
            action_ids.append(item.action_id)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("brief evaluation action ids must be unique")
        if type(self.primary_state) is not BriefState:
            raise ValueError("brief evaluation primary state must be an exact BriefState")
        if type(self.action_maturity) is not ActionMaturity:
            raise ValueError("brief evaluation maturity must be an exact ActionMaturity")
        for values, name in (
            (self.constraints, "constraints"),
            (self.triggered_reviews, "triggered reviews"),
            (self.affected_action_abstentions, "affected action abstentions"),
            (self.blocking_codes, "blocking codes"),
            (self.missing_fields, "missing fields"),
            (self.conflicts, "conflicts"),
            (self.resolution_lineage_ids, "resolution lineage ids"),
        ):
            validate_identifier_tuple(values, f"brief evaluation {name}")
        if type(self.resolution_bindings) is not tuple:
            raise ValueError("brief evaluation resolution bindings must be an exact tuple")
        for binding in self.resolution_bindings:
            if type(binding) is not BriefResolutionBinding:
                raise ValueError("brief evaluation resolution bindings must be exact")
            binding.validate()
        if self.resolution_lineage_ids != tuple(
            dict.fromkeys(binding.lineage_id for binding in self.resolution_bindings)
        ):
            raise ValueError("brief evaluation resolution bindings and lineages differ")
        if not set(self.affected_action_abstentions).issubset(action_ids):
            raise ValueError("affected abstentions must resolve to an interpretation")
        if any(item.exact_amount_available for item in self.interpretations):
            raise ValueError("Phase 1 evaluation cannot expose an exact amount")


class HeldFundBriefEngine:
    def __init__(self, policy: Optional[HeldFundBriefPolicyV1] = None) -> None:
        if policy is None:
            policy = HeldFundBriefPolicyV1()
        if type(policy) is not HeldFundBriefPolicyV1:
            raise ValueError("brief engine policy must be exact HeldFundBriefPolicyV1")
        policy.validate()
        self._policy = policy

    def evaluate(
        self,
        *,
        route: DecisionRoute,
        fact_set: SourceLinkedFactSet,
        d2: D2RelationshipSet,
        source_resolutions: Tuple[BriefSourceResolution, ...],
        confirmed_thesis: Optional[ConfirmedThesisState],
    ) -> HeldFundBriefEvaluation:
        self._validate_inputs(
            route,
            fact_set,
            d2,
            source_resolutions,
            confirmed_thesis,
        )
        action_routes = tuple(item for item in route.actions if item.action_id != "fact_research")
        active_events, inactive_events = self._active_events(
            fact_set.official_events,
            tuple(item.action_id for item in action_routes),
        )
        conflicts = _sorted_unique(
            [
                *fact_set.conflicts,
                *d2.conflicts,
                *(
                    f"official_event_{event.integrity_status}_{event.event_id}"
                    for event in inactive_events
                ),
            ]
        )
        missing_fields = _sorted_unique(
            [*fact_set.missing_fields, *d2.missing_fields, *route.missing_fields]
        )
        identity_conflicts = tuple(
            item for item in conflicts if any(marker in item for marker in _IDENTITY_MARKERS)
        )
        evidence_ids = self._evidence_namespace(fact_set, d2)
        available_fields = self._available_fields(
            fact_set,
            d2,
            source_resolutions,
            action_routes,
        )
        interpretations = []
        abstentions = []
        triggered_reviews = _unique(
            [
                event.event_code.value
                for event in active_events
                if event.event_code in _HARD_EXIT_EVENTS
            ]
        )
        for action_route in action_routes:
            interpretation = self._interpret_action(
                action_route=action_route,
                fact_set=fact_set,
                active_events=active_events,
                inactive_events=inactive_events,
                identity_conflicts=identity_conflicts,
                missing_fields=missing_fields,
                available_fields=available_fields[action_route.action_id],
                confirmed_thesis=confirmed_thesis,
            )
            if not set(
                interpretation.supporting_evidence_ids + interpretation.opposing_evidence_ids
            ).issubset(evidence_ids):
                raise ValueError("interpretation evidence does not close over brief inputs")
            interpretations.append(interpretation)
            action_has_inactive_event = any(
                action_route.action_id in event.affected_action_ids for event in inactive_events
            )
            action_has_redemption_restriction = any(
                event.event_code is OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE
                and action_route.action_id in event.affected_action_ids
                for event in active_events
            )
            action_has_critical_gap = bool(
                set(interpretation.missing_fields)
                & set(self._required_fields(action_route.action_id))
            )
            if (
                interpretation.state is BriefState.ABSTAIN
                or identity_conflicts
                or action_has_inactive_event
                or action_has_redemption_restriction
                or action_has_critical_gap
            ):
                abstentions.append(action_route.action_id)

        missing_fields = _sorted_unique(
            [
                *missing_fields,
                *(field for item in interpretations for field in item.missing_fields),
            ]
        )

        phase_b_blocked = any("phase_b_blocked" in item.blocking_codes for item in action_routes)
        if phase_b_blocked:
            primary_state = BriefState.NO_ADD
            action_maturity = ActionMaturity.MATURE
        elif any(item.state is BriefState.REDUCE_OR_EXIT_REVIEW for item in interpretations):
            primary = next(
                item for item in interpretations if item.state is BriefState.REDUCE_OR_EXIT_REVIEW
            )
            primary_state = primary.state
            action_maturity = primary.action_maturity
        else:
            primary_state = interpretations[0].state
            action_maturity = interpretations[0].action_maturity

        all_blocking_codes = _unique(
            [code for item in interpretations for code in item.blocking_codes]
        )
        constraints = _unique(
            [
                *all_blocking_codes,
                *route.opposing_evidence,
            ]
        )
        sync_status = self._sync_status(
            fact_set,
            d2,
            source_resolutions,
            conflicts,
        )
        decision_status = self._decision_status(
            action_routes=action_routes,
            interpretations=tuple(interpretations),
            available_fields=available_fields,
            conflicts=conflicts,
            source_resolutions=source_resolutions,
            d2=d2,
            fact_set=fact_set,
            affected_action_abstentions=_unique(abstentions),
        )
        result = HeldFundBriefEvaluation(
            sync_status=sync_status,
            decision_evidence_status=decision_status,
            interpretations=tuple(interpretations),
            primary_state=primary_state,
            action_maturity=action_maturity,
            constraints=constraints,
            triggered_reviews=triggered_reviews,
            affected_action_abstentions=_unique(abstentions),
            blocking_codes=all_blocking_codes,
            missing_fields=missing_fields,
            conflicts=conflicts,
            resolution_lineage_ids=tuple(
                f"source_attempt_{item.source_attempt_id}" for item in source_resolutions
            ),
            resolution_bindings=tuple(item.to_snapshot_binding() for item in source_resolutions),
        )
        result.validate()
        return result

    def _validate_inputs(
        self,
        route: DecisionRoute,
        fact_set: SourceLinkedFactSet,
        d2: D2RelationshipSet,
        source_resolutions: Tuple[BriefSourceResolution, ...],
        confirmed_thesis: Optional[ConfirmedThesisState],
    ) -> None:
        if type(route) is not DecisionRoute:
            raise ValueError("brief engine route must be an exact DecisionRoute")
        route.validate()
        if any(item.exact_amount_available for item in route.actions):
            raise ValueError("Phase 1 rejects a route claiming exact amount availability")
        action_shape = tuple(item.action_id for item in route.actions)
        if action_shape not in _CANONICAL_ACTION_SHAPES:
            raise ValueError("brief engine route action shape is not canonical")
        if type(fact_set) is not SourceLinkedFactSet:
            raise ValueError("brief engine fact set must be exact SourceLinkedFactSet")
        fact_set.validate()
        if type(d2) is not D2RelationshipSet:
            raise ValueError("brief engine D2 input must be exact D2RelationshipSet")
        d2.validate()
        if fact_set.fund_code != d2.target_fund_code:
            raise ValueError("brief engine fact and D2 subjects do not match")
        brief_as_of = d2.portfolio_provenance.as_of
        for fact in fact_set.facts:
            fact_times = (fact.data_as_of, fact.published_at, fact.retrieved_at)
            if any(value is not None and value > brief_as_of for value in fact_times):
                raise ValueError("brief fact is later than the brief as-of time")
        for event in fact_set.official_events:
            if event.published_at > brief_as_of or event.retrieved_at > brief_as_of:
                raise ValueError("official event is later than the brief as-of time")
        if type(source_resolutions) is not tuple:
            raise ValueError("source resolutions must be an exact tuple")
        action_ids = set(action_shape)
        evidence_ids = self._evidence_namespace(fact_set, d2)
        resolution_keys = []
        for item in source_resolutions:
            if type(item) is not BriefSourceResolution:
                raise ValueError("source resolutions must contain exact source resolution records")
            item.validate()
            if item.action_id not in action_ids:
                raise ValueError("source resolution action is not present in the route")
            if item.request_id != route.request_id or item.fund_code != fact_set.fund_code:
                raise ValueError("source resolution request or subject binding does not match")
            if item.evaluated_at is None or item.evaluated_at > d2.portfolio_provenance.as_of:
                raise ValueError("source resolution evaluation falls outside the brief")
            if item.action_id != "fact_research" and item.field_id not in self._required_fields(
                item.action_id
            ):
                raise ValueError("source resolution field is not required by its action")
            if not set(item.evidence_ids).issubset(evidence_ids):
                raise ValueError("source resolution evidence does not close over brief inputs")
            facts_by_id = {fact.fact_id: fact for fact in (*fact_set.facts, *d2.evidence_facts)}
            event_ids = {event.event_id for event in fact_set.official_events}
            events_by_id = {event.event_id: event for event in fact_set.official_events}
            if item.field_id == "official_events":
                if not set(item.evidence_ids).issubset(event_ids):
                    raise ValueError("official event resolution evidence has the wrong type")
            elif item.field_id == "d2":
                allowed_d2_ids = {
                    *(relationship.relationship_id for relationship in d2.relationships),
                    d2.coverage.coverage_id,
                }
                if not set(item.evidence_ids).issubset(allowed_d2_ids):
                    raise ValueError("D2 resolution evidence has the wrong type")
            elif any(
                evidence_id not in facts_by_id or facts_by_id[evidence_id].field_id != item.field_id
                for evidence_id in item.evidence_ids
            ):
                raise ValueError("source resolution evidence field does not match")
            expected_lineage = f"source_attempt_{item.source_attempt_id}"
            for evidence_id in item.evidence_ids:
                if evidence_id in facts_by_id:
                    if facts_by_id[evidence_id].source_lineage_id != expected_lineage:
                        raise ValueError("source resolution evidence lineage does not match")
                elif evidence_id in events_by_id and expected_lineage not in {
                    events_by_id[evidence_id].original_source_id,
                    events_by_id[evidence_id].quoted_source_id,
                }:
                    raise ValueError("source resolution event lineage does not match")
            if (
                item.resolution is RequestFieldResolution.USABLE
                and item.field_id != "official_events"
                and not item.evidence_ids
            ):
                raise ValueError("usable source resolution requires bound evidence")
            resolution_keys.append((item.action_id, item.field_id))
        if len(resolution_keys) != len(set(resolution_keys)):
            raise ValueError("source resolution action and field bindings must be unique")
        if confirmed_thesis is not None:
            if type(confirmed_thesis) is not ConfirmedThesisState:
                raise ValueError("confirmed thesis must use the exact state type")
            confirmed_thesis.validate()
            if confirmed_thesis.fund_code != fact_set.fund_code:
                raise ValueError("confirmed thesis subject does not match the brief")
            if confirmed_thesis.review_request_id != route.request_id:
                raise ValueError("confirmed thesis review request does not match the brief")
            if (
                confirmed_thesis.reviewed_at is not None
                and confirmed_thesis.reviewed_at > d2.portfolio_provenance.as_of
            ):
                raise ValueError("confirmed thesis review is later than the brief")
            if confirmed_thesis.review_source_attempt_id not in {
                item.source_attempt_id for item in source_resolutions
            }:
                raise ValueError("confirmed thesis review source is not in the brief")
            if not set(confirmed_thesis.evidence_ids).issubset(evidence_ids):
                raise ValueError("confirmed thesis evidence does not close over brief inputs")
        for event in fact_set.official_events:
            if event.affected_action_ids != canonical_event_affected_actions(
                event.event_code,
                action_shape,
            ):
                raise ValueError("official event action binding does not match the route")

    @staticmethod
    def _evidence_namespace(
        fact_set: SourceLinkedFactSet,
        d2: D2RelationshipSet,
    ) -> set[str]:
        namespace: dict[str, tuple[str, bytes]] = {}

        def add(identifier: str, kind: str, value: object) -> None:
            encoded = canonical_json_bytes(value)
            existing = namespace.get(identifier)
            if existing is None:
                namespace[identifier] = (kind, encoded)
                return
            if existing != (kind, encoded):
                raise ValueError("brief evidence namespace contains a conflicting identifier")

        for fact in fact_set.facts:
            add(fact.fact_id, "fact", fact)
        for event in fact_set.official_events:
            add(event.event_id, "event", event)
        for fact in d2.evidence_facts:
            add(fact.fact_id, "fact", fact)
        for relationship in d2.relationships:
            add(relationship.relationship_id, "relationship", relationship)
        add(d2.coverage.coverage_id, "coverage", d2.coverage)
        return set(namespace)

    @staticmethod
    def _active_events(
        events: Tuple[OfficialEvent, ...],
        action_ids: Tuple[str, ...],
    ) -> Tuple[Tuple[OfficialEvent, ...], Tuple[OfficialEvent, ...]]:
        active = []
        inactive = []
        allowed = set(action_ids)
        for event in sorted(events, key=lambda item: (item.published_at, item.event_id)):
            if not set(event.affected_action_ids).intersection(allowed):
                continue
            if event.integrity_status == "active":
                active.append(event)
            else:
                inactive.append(event)
        return tuple(active), tuple(inactive)

    @staticmethod
    def _available_fields(
        fact_set: SourceLinkedFactSet,
        d2: D2RelationshipSet,
        source_resolutions: Tuple[BriefSourceResolution, ...],
        action_routes: Tuple[ActionRoute, ...],
    ) -> dict[str, set[str]]:
        explicit_missing = set(fact_set.missing_fields) | set(d2.missing_fields)
        shared = {
            fact.field_id
            for fact in fact_set.facts
            if fact.field_id not in explicit_missing
            and fact.freshness is EvidenceFreshness.CURRENT
            and fact.completeness is not EvidenceCompleteness.INSUFFICIENT
            and not fact.conflict_ids
        }
        if d2.position_present is not None and d2.portfolio_evidence_state != "unknown":
            shared.add("personal_position")
        if d2.coverage.evidence_state is not BriefEvidenceState.INSUFFICIENT:
            shared.add("d2")
        available = {route.action_id: set(shared) for route in action_routes}
        for route in action_routes:
            if "financial_safety_not_current" not in route.blocking_codes:
                available[route.action_id].update(("phase_b", "phase_b_context"))
            if "personal_position" in shared:
                available[route.action_id].add("position")
            if "redemption_terms" in shared:
                available[route.action_id].update(("fees", "settlement"))
        for item in source_resolutions:
            if item.action_id in available and item.resolution is RequestFieldResolution.USABLE:
                available[item.action_id].add(item.field_id)
        return available

    def _interpret_action(
        self,
        *,
        action_route: ActionRoute,
        fact_set: SourceLinkedFactSet,
        active_events: Tuple[OfficialEvent, ...],
        inactive_events: Tuple[OfficialEvent, ...],
        identity_conflicts: Tuple[str, ...],
        missing_fields: Tuple[str, ...],
        available_fields: set[str],
        confirmed_thesis: Optional[ConfirmedThesisState],
    ) -> BriefActionInterpretation:
        action_events = tuple(
            item for item in active_events if action_route.action_id in item.affected_action_ids
        )
        hard_events = tuple(item for item in action_events if item.event_code in _HARD_EXIT_EVENTS)
        watch_events = tuple(item for item in action_events if item.event_code in _WATCH_EVENTS)
        redemption_restrictions = tuple(
            item
            for item in action_events
            if item.event_code is OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE
        )
        inactive_action_events = tuple(
            item for item in inactive_events if action_route.action_id in item.affected_action_ids
        )
        supporting = _unique([item.event_id for item in action_events])
        opposing = _unique(
            [
                *(fact.fact_id for fact in fact_set.facts if fact.conflict_ids),
                *(item.event_id for item in inactive_action_events),
            ]
        )
        blocking = list(action_route.blocking_codes)
        blocking.extend(identity_conflicts)
        blocking.extend(
            f"official_event_{item.integrity_status}_{item.event_id}"
            for item in inactive_action_events
        )
        blocking.extend(item.event_code.value for item in redemption_restrictions)
        unavailable = ["exact_amount"]
        invalidation_conditions: Tuple[str, ...] = ()
        critical_missing = tuple(
            field
            for field in self._required_fields(action_route.action_id)
            if field not in available_fields
        )
        blocking.extend(f"{field}_missing" for field in critical_missing)
        if not action_route.research_available:
            blocking.append("research_unavailable")

        fee_missing = "fees_missing" in action_route.blocking_codes or bool(
            {"fees_share_class_relationship", "redemption_fee_rules", "redemption_terms"}
            & set(missing_fields)
        )
        if fee_missing:
            unavailable.extend(("exact_fee", "executable_redemption"))
        if hard_events:
            unavailable.append("immediate_sale")
        if action_route.action_id in _TRANSACTION_ACTION_IDS:
            unavailable.append("automatic_trade")
        if redemption_restrictions:
            unavailable.append("executable_redemption")
        if (
            confirmed_thesis is not None
            and confirmed_thesis.review_state is ThesisReviewState.TRIGGERED
        ):
            blocking.append("thesis_invalidation_triggered")
            invalidation_conditions = confirmed_thesis.invalidation_conditions
            opposing = _unique([*opposing, *confirmed_thesis.evidence_ids])

        if action_route.action_id == "continue_holding":
            if action_route.minimum_state is ActionState.NO_ADD:
                state = BriefState.NO_ADD
                maturity = ActionMaturity.MATURE
            elif not action_route.research_available:
                state = BriefState.ABSTAIN
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            elif hard_events:
                state = BriefState.REDUCE_OR_EXIT_REVIEW
                maturity = ActionMaturity.MATURE
            elif identity_conflicts or inactive_action_events or critical_missing:
                state = BriefState.ABSTAIN
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            elif watch_events:
                state = BriefState.WATCH
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            elif (
                confirmed_thesis is not None
                and confirmed_thesis.review_state is ThesisReviewState.TRIGGERED
            ):
                state = BriefState.WATCH
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            elif (
                confirmed_thesis is not None
                and confirmed_thesis.review_state is ThesisReviewState.INTACT
            ):
                state = BriefState.HOLD
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
                invalidation_conditions = confirmed_thesis.invalidation_conditions
                supporting = _unique([*supporting, *confirmed_thesis.evidence_ids])
            else:
                state = BriefState.WATCH
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
        elif action_route.action_id == "switch_buy":
            state = BriefState.ABSTAIN
            maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            if watch_events:
                blocking.extend(item.event_code.value for item in watch_events)
        else:
            if not action_route.research_available:
                state = BriefState.ABSTAIN
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
            elif hard_events:
                state = BriefState.REDUCE_OR_EXIT_REVIEW
                maturity = ActionMaturity.MATURE
            elif (
                identity_conflicts
                or inactive_action_events
                or redemption_restrictions
                or critical_missing
                or "redemption_terms" not in available_fields
            ):
                state = BriefState.ABSTAIN
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW
                if "redemption_terms" not in available_fields:
                    blocking.append("redemption_terms_missing")
            else:
                state = BriefState.REDUCE_OR_EXIT_REVIEW
                maturity = ActionMaturity.EXPERIMENTAL_SHADOW

        if watch_events and any(
            item.event_code is OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE
            for item in watch_events
        ):
            unavailable.append("buy_or_add")
        state_inputs = {
            "owner_confirmed_thesis": confirmed_thesis is not None,
            "opposing_codes": tuple(action_route.blocking_codes),
            "research_available": action_route.research_available,
            "thesis_review_source_lineage_id": (
                None
                if confirmed_thesis is None
                else f"source_attempt_{confirmed_thesis.review_source_attempt_id}"
            ),
            "thesis_review_state": (
                "absent" if confirmed_thesis is None else confirmed_thesis.review_state.value
            ),
            "thesis_reviewed_at": (
                None if confirmed_thesis is None else confirmed_thesis.reviewed_at
            ),
            "thesis_record_id": (
                None if confirmed_thesis is None else str(confirmed_thesis.thesis_id)
            ),
            "thesis_fingerprint": (
                None if confirmed_thesis is None else confirmed_thesis.thesis_fingerprint
            ),
        }
        action_missing_fields = set(self._required_fields(action_route.action_id))
        action_missing_fields.update(action_route.required_gates)
        if action_route.action_id == "continue_holding" and fee_missing:
            action_missing_fields.update(
                ("fees_share_class_relationship", "redemption_fee_rules", "redemption_terms")
            )
        result = BriefActionInterpretation(
            action_id=action_route.action_id,
            state=state,
            action_maturity=maturity,
            supporting_evidence_ids=supporting,
            opposing_evidence_ids=opposing,
            blocking_codes=_unique(blocking),
            missing_fields=tuple(
                dict.fromkeys(
                    [
                        *(item for item in missing_fields if item in action_missing_fields),
                        *critical_missing,
                    ]
                )
            ),
            invalidation_conditions=invalidation_conditions,
            unavailable_actions=_unique(unavailable),
            exact_amount_available=False,
            state_inputs=state_inputs,
        )
        result.validate()
        return result

    def _required_fields(self, action_id: str) -> Tuple[str, ...]:
        return dict(self._policy.fact_requirements).get(action_id, ())

    @staticmethod
    def _resolution_fields(
        resolutions: Tuple[BriefSourceResolution, ...],
        state: SourceFieldState,
    ) -> Tuple[str, ...]:
        return _sorted_unique(
            [item.field_id for item in resolutions if state in item.source_states]
        )

    def _sync_status(
        self,
        fact_set: SourceLinkedFactSet,
        d2: D2RelationshipSet,
        resolutions: Tuple[BriefSourceResolution, ...],
        conflicts: Tuple[str, ...],
    ) -> EvidenceStatus:
        required = _sorted_unique([item.field_id for item in resolutions])
        explicit_missing = set(fact_set.missing_fields) | set(d2.missing_fields)
        obtained_values = [
            fact.field_id for fact in fact_set.facts if fact.field_id not in explicit_missing
        ]
        obtained_values.extend(
            item.field_id
            for item in resolutions
            if item.resolution is RequestFieldResolution.USABLE
        )
        if d2.position_present is not None:
            obtained_values.append("personal_position")
        obtained = _sorted_unique(obtained_values)
        missing = _sorted_unique([*fact_set.missing_fields, *d2.missing_fields])
        stale = _sorted_unique(
            [
                fact.field_id
                for fact in fact_set.facts
                if fact.freshness in {EvidenceFreshness.STALE, EvidenceFreshness.UNKNOWN}
            ]
        )
        conflicted = _sorted_unique([fact.field_id for fact in fact_set.facts if fact.conflict_ids])
        unsupported = self._resolution_fields(resolutions, SourceFieldState.UNSUPPORTED)
        cooldown = self._resolution_fields(resolutions, SourceFieldState.COOLDOWN)
        partial_resolution = any(
            item.resolution is not RequestFieldResolution.USABLE for item in resolutions
        )
        d2_partial = (
            d2.portfolio_evidence_state != "current"
            or d2.coverage.evidence_state is not BriefEvidenceState.COMPLETE
            or d2.holdings_coverage.evidence_state is not BriefEvidenceState.COMPLETE
            or bool(d2.warnings)
        )
        if not obtained:
            state = BriefEvidenceState.INSUFFICIENT
        elif (
            missing
            or stale
            or conflicted
            or unsupported
            or cooldown
            or conflicts
            or partial_resolution
            or d2_partial
        ):
            state = BriefEvidenceState.PARTIAL
        else:
            state = BriefEvidenceState.COMPLETE
        result = EvidenceStatus(
            state=state,
            required_fields=required,
            obtained_fields=obtained,
            missing_fields=missing,
            stale_fields=stale,
            conflicted_fields=conflicted,
            unsupported_fields=unsupported,
            cooldown_fields=cooldown,
            supported_interpretations=(),
            unsupported_interpretations=(),
            acceptable_alternative_ids=_sorted_unique(
                [value for item in resolutions for value in item.acceptable_alternative_ids]
            ),
            manual_supplementation_codes=_sorted_unique(
                [value for item in resolutions for value in item.manual_supplementation_codes]
            ),
        )
        result.validate()
        return result

    def _decision_status(
        self,
        *,
        action_routes: Tuple[ActionRoute, ...],
        interpretations: Tuple[BriefActionInterpretation, ...],
        available_fields: dict[str, set[str]],
        conflicts: Tuple[str, ...],
        source_resolutions: Tuple[BriefSourceResolution, ...],
        d2: D2RelationshipSet,
        fact_set: SourceLinkedFactSet,
        affected_action_abstentions: Tuple[str, ...],
    ) -> EvidenceStatus:
        required = _sorted_unique(
            [
                field
                for route in action_routes
                for field in (*self._required_fields(route.action_id), *route.required_gates)
            ]
        )
        obtained = tuple(
            field
            for field in required
            if all(
                field in available_fields[route.action_id]
                for route in action_routes
                if field in (*self._required_fields(route.action_id), *route.required_gates)
            )
        )
        facts = (*fact_set.facts, *d2.evidence_facts)
        stale = _sorted_unique(
            [
                *(
                    fact.field_id
                    for fact in facts
                    if fact.freshness in {EvidenceFreshness.STALE, EvidenceFreshness.UNKNOWN}
                ),
                *self._resolution_fields(source_resolutions, SourceFieldState.DEGRADED),
            ]
        )
        stale = tuple(field for field in stale if field in required)
        conflicted_values = {
            fact.field_id for fact in facts if fact.conflict_ids and fact.field_id in required
        }
        if "identity_active_status" in required and any(
            "identity" in conflict or "share_class" in conflict for conflict in conflicts
        ):
            conflicted_values.add("identity_active_status")
        if "d2" in required and d2.conflicts:
            conflicted_values.add("d2")
        conflicted = _sorted_unique(conflicted_values)
        unsupported = tuple(
            field
            for field in self._resolution_fields(
                source_resolutions,
                SourceFieldState.UNSUPPORTED,
            )
            if field in required
        )
        cooldown = tuple(
            field
            for field in self._resolution_fields(
                source_resolutions,
                SourceFieldState.COOLDOWN,
            )
            if field in required
        )
        classified_failures = set(stale + conflicted + unsupported + cooldown)
        missing = _sorted_unique(
            [
                *(
                    field
                    for field in required
                    if field not in classified_failures
                    if any(
                        field not in available_fields[route.action_id]
                        for route in action_routes
                        if field in (*self._required_fields(route.action_id), *route.required_gates)
                    )
                ),
                *(
                    field
                    for item in interpretations
                    for field in item.missing_fields
                    if field in required and field not in classified_failures
                ),
            ]
        )
        supported = tuple(
            item.action_id
            for item in interpretations
            if item.state is not BriefState.ABSTAIN
            and item.action_id not in affected_action_abstentions
        )
        unsupported_interpretations = tuple(
            item.action_id
            for item in interpretations
            if item.state is BriefState.ABSTAIN or item.action_id in affected_action_abstentions
        )
        if not supported:
            state = BriefEvidenceState.INSUFFICIENT
        elif (
            missing
            or conflicted
            or stale
            or unsupported
            or cooldown
            or any(item.blocking_codes for item in interpretations)
            or (
                "d2" in required
                and (
                    d2.coverage.evidence_state is not BriefEvidenceState.COMPLETE
                    or d2.holdings_coverage.evidence_state is not BriefEvidenceState.COMPLETE
                    or d2.portfolio_evidence_state != "current"
                    or bool(d2.warnings)
                )
            )
        ):
            state = BriefEvidenceState.PARTIAL
        else:
            state = BriefEvidenceState.COMPLETE
        result = EvidenceStatus(
            state=state,
            required_fields=required,
            obtained_fields=obtained,
            missing_fields=missing,
            stale_fields=stale,
            conflicted_fields=conflicted,
            unsupported_fields=unsupported,
            cooldown_fields=cooldown,
            supported_interpretations=supported,
            unsupported_interpretations=unsupported_interpretations,
            acceptable_alternative_ids=_sorted_unique(
                [value for item in source_resolutions for value in item.acceptable_alternative_ids]
            ),
            manual_supplementation_codes=_sorted_unique(
                [
                    value
                    for item in source_resolutions
                    for value in item.manual_supplementation_codes
                ]
            ),
        )
        result.validate()
        return result
