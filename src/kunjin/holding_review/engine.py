from __future__ import annotations

from dataclasses import replace
from typing import Optional, Tuple

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import ActionKind
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    EvidenceDelta,
    EvidenceReadiness,
    FlowStatus,
    HistoryComparability,
    HoldingReviewInputs,
    HoldingReviewResult,
    HoldingReviewSnapshot,
    OfficialEventEvidenceReference,
    RedemptionComponentState,
    RedemptionFeasibility,
    ReviewBoundary,
    ReviewDisposition,
    ReviewEvidenceItem,
    ThesisMatchState,
    TriggeredReviewCode,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1
from kunjin.intelligence.models import LineageKind

_HARD_EVENT_CODES = frozenset(
    (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        OfficialEventCode.FUND_TERMINATION_NOTICE,
    )
)
_UNRESOLVED_THESIS_STATES = frozenset(
    (
        ThesisMatchState.MANUAL_REVIEW_PENDING,
        ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
    )
)
_NO_PENDING_MATCH_STATES = frozenset(
    (
        ThesisMatchState.NO_MATCHING_EVIDENCE,
        ThesisMatchState.PRESENTED_MATCH_REJECTED,
    )
)


def determine_action_review_source_sufficiency(
    items: Tuple[ReviewEvidenceItem, ...],
) -> ActionReviewSourceSufficiency:
    if type(items) is not tuple:
        raise ValueError("review evidence items must be an exact tuple")
    for item in items:
        if type(item) is not ReviewEvidenceItem:
            raise ValueError("review evidence items must contain exact records")
        item.validate()
    if not items:
        return ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    every_item_is_usable = all(
        item.current
        and item.graph_closed
        and item.original_lineage
        and not item.retracted
        and not item.conflicted
        for item in items
    )
    direct_tier_one = any(
        item.source_tier == 1 and item.direct_subject_binding for item in items
    )
    if every_item_is_usable and direct_tier_one:
        return ActionReviewSourceSufficiency.SUFFICIENT
    return ActionReviewSourceSufficiency.INSUFFICIENT_DATA


def compare_evidence(
    prior: Optional[HoldingReviewSnapshot],
    current_items: Tuple[ReviewEvidenceItem, ...],
    current_coverage_complete: bool,
    *,
    prior_items: Optional[Tuple[ReviewEvidenceItem, ...]] = None,
    current_additional_ids: Tuple[str, ...] = (),
    current_official_event_evidence: Tuple[OfficialEventEvidenceReference, ...] = (),
) -> EvidenceDelta:
    if type(current_items) is not tuple:
        raise ValueError("current evidence items must be an exact tuple")
    if type(current_coverage_complete) is not bool:
        raise ValueError("current coverage complete must be an exact boolean")
    current_ids = []
    corrected = []
    retracted = []
    expired = []
    conflicted = []
    for item in current_items:
        if type(item) is not ReviewEvidenceItem:
            raise ValueError("current evidence items must contain exact records")
        item.validate()
        current_ids.append(item.evidence_id)
        if item.lineage_kind is LineageKind.CORRECTION_OF:
            corrected.append(item.evidence_id)
        if item.retracted or item.lineage_kind is LineageKind.RETRACTION_OF:
            retracted.append(item.evidence_id)
        if not item.current:
            expired.append(item.evidence_id)
        if item.conflicted:
            conflicted.append(item.evidence_id)
    if type(current_additional_ids) is not tuple or any(
        type(item) is not str for item in current_additional_ids
    ):
        raise ValueError("additional evidence ids must be an exact string tuple")
    _validate_official_references(
        current_official_event_evidence,
        "current official event evidence",
    )
    current_set = set(current_ids).union(current_additional_ids)
    if len(current_set) != len(current_ids) + len(current_additional_ids):
        raise ValueError("current evidence ids must be unique")

    if prior is None:
        delta = EvidenceDelta(
            history_comparability=HistoryComparability.NOT_AVAILABLE,
            evidence_unchanged=False,
            added_evidence_ids=tuple(sorted(current_set)),
            corrected_evidence_ids=tuple(sorted(corrected)),
            retracted_evidence_ids=tuple(sorted(retracted)),
            expired_evidence_ids=tuple(sorted(expired)),
            conflicted_evidence_ids=tuple(sorted(conflicted)),
            reason_codes=("previous_review_unavailable",),
        )
        delta.validate()
        return delta
    if type(prior) is not HoldingReviewSnapshot:
        raise ValueError("prior review must be an exact HoldingReviewSnapshot")
    prior.validate()
    descriptor_changes = []
    descriptor_reasons = set()
    prior_references = prior.result.official_event_evidence
    prior_support_ids = tuple(
        reference.support_evidence_id for reference in prior_references
    )
    current_support_ids = tuple(
        reference.support_evidence_id for reference in current_official_event_evidence
    )
    prior_descriptor_set_complete = False
    if prior_items is None:
        descriptor_reasons.add("previous_evidence_descriptors_unavailable")
    else:
        _validate_evidence_items(prior_items, "previous evidence items")
        prior_declared_ids = tuple(item.evidence_id for item in prior_items)
        prior_combined_ids = prior_declared_ids + prior_support_ids
        prior_descriptor_set_complete = (
            len(prior_combined_ids) == len(set(prior_combined_ids))
            and set(prior_combined_ids) == set(prior.result.evidence_ids)
        )
        if not prior_descriptor_set_complete:
            descriptor_reasons.add("previous_evidence_descriptor_set_mismatch")
        prior_by_id = {item.evidence_id: item for item in prior_items}
        current_by_id = {item.evidence_id: item for item in current_items}
        descriptor_changes = sorted(
            evidence_id
            for evidence_id in prior_by_id.keys() & current_by_id.keys()
            if prior_by_id[evidence_id] != current_by_id[evidence_id]
        )
        if descriptor_changes:
            descriptor_reasons.add("evidence_descriptor_changed")
    current_reference_set_complete = (
        len(current_support_ids) == len(set(current_support_ids))
        and set(current_support_ids) == set(current_additional_ids)
    )
    if not current_reference_set_complete:
        descriptor_reasons.add("current_official_event_reference_set_mismatch")
    prior_references_by_id = {
        reference.projection_id: reference for reference in prior_references
    }
    current_references_by_id = {
        reference.projection_id: reference
        for reference in current_official_event_evidence
    }
    official_reference_changed = any(
        prior_references_by_id[projection_id].to_canonical_dict()
        != current_references_by_id[projection_id].to_canonical_dict()
        for projection_id in prior_references_by_id.keys()
        & current_references_by_id.keys()
    )
    if official_reference_changed:
        descriptor_reasons.add("official_event_reference_changed")
    prior_set = set(prior.result.evidence_ids)
    added = tuple(sorted(current_set - prior_set))
    removed = tuple(sorted(prior_set - current_set))
    reasons = set(descriptor_reasons)
    prior_coverage_complete = _result_coverage_complete(prior.result)
    if not current_coverage_complete or not prior_coverage_complete:
        reasons.add("coverage_decreased")
    if corrected:
        reasons.add("evidence_corrected")
    if retracted:
        reasons.add("evidence_retracted")
    if expired:
        reasons.add("evidence_expired")
    if conflicted:
        reasons.add("evidence_conflicted")
    changed = bool(
        added
        or removed
        or descriptor_changes
        or official_reference_changed
        or corrected
        or retracted
        or expired
        or conflicted
    )
    comparable = (
        current_coverage_complete
        and prior_coverage_complete
        and prior_items is not None
        and prior_descriptor_set_complete
        and current_reference_set_complete
        and not official_reference_changed
    )
    delta = EvidenceDelta(
        history_comparability=(
            HistoryComparability.COMPARABLE
            if comparable
            else HistoryComparability.NOT_COMPARABLE
        ),
        evidence_unchanged=comparable and not changed,
        added_evidence_ids=added,
        removed_evidence_ids=removed,
        corrected_evidence_ids=tuple(sorted(corrected)),
        retracted_evidence_ids=tuple(sorted(retracted)),
        expired_evidence_ids=tuple(sorted(expired)),
        conflicted_evidence_ids=tuple(sorted(conflicted)),
        reason_codes=tuple(sorted(reasons)),
    )
    delta.validate()
    return delta


class HoldingReviewEngine:
    def __init__(
        self,
        *,
        previous_review: Optional[HoldingReviewSnapshot] = None,
        previous_review_id: Optional[int] = None,
        previous_evidence_items: Optional[Tuple[ReviewEvidenceItem, ...]] = None,
        policy: Optional[HeldFundManualReviewPolicyV1] = None,
    ) -> None:
        supplied = (
            previous_review is not None,
            previous_review_id is not None,
            previous_evidence_items is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "previous review, id, and evidence items must be provided together"
            )
        if previous_review is not None and type(previous_review) is not HoldingReviewSnapshot:
            raise ValueError("previous review must be an exact HoldingReviewSnapshot")
        if previous_review_id is not None and (
            type(previous_review_id) is not int or previous_review_id <= 0
        ):
            raise ValueError("previous review id must be a positive exact integer")
        if previous_evidence_items is not None:
            _validate_evidence_items(previous_evidence_items, "previous evidence items")
        self._previous_review = previous_review
        self._previous_review_id = previous_review_id
        self._previous_evidence_items = previous_evidence_items
        self._policy = HeldFundManualReviewPolicyV1() if policy is None else policy
        if type(self._policy) is not HeldFundManualReviewPolicyV1:
            raise ValueError("policy must be an exact HeldFundManualReviewPolicyV1")
        self._policy.validate()

    def evaluate(self, inputs: HoldingReviewInputs) -> HoldingReviewResult:
        if type(inputs) is not HoldingReviewInputs:
            raise ValueError("holding review inputs must be exact")
        inputs.validate()
        if (
            inputs.policy_version != self._policy.version
            or inputs.policy_checksum != self._policy.checksum()
        ):
            raise ValueError("holding review input policy binding is invalid")

        triggered_reviews = tuple(
            code
            for code in TriggeredReviewCode
            if any(
                reference.triggered_review_code is code
                for reference in inputs.official_event_evidence
            )
        )
        hard_event_review = any(
            reference.event_code in _HARD_EVENT_CODES
            for reference in inputs.official_event_evidence
        )
        support_ids = tuple(
            reference.support_evidence_id for reference in inputs.official_event_evidence
        )
        current_coverage_complete = _input_coverage_complete(inputs)
        delta = self._evidence_delta(inputs, current_coverage_complete, support_ids)
        if (
            inputs.action is ActionKind.FULL_EXIT
            and inputs.exit_reason.value == "owner_believes_thesis_invalidated"
            and inputs.thesis_review_state is not ThesisMatchState.PRESENTED_MATCH_CONFIRMED
        ):
            delta = _with_delta_reason(delta, "owner_context_conflict")

        source_sufficiency = determine_action_review_source_sufficiency(
            inputs.review_evidence_items
        )
        flow_status, evidence_readiness = _readiness(inputs, current_coverage_complete)
        disposition = _disposition(
            inputs,
            current_coverage_complete=current_coverage_complete,
            source_sufficiency=source_sufficiency,
            hard_event_review=hard_event_review,
            triggered_reviews=triggered_reviews,
            history_blocks_reassurance=(
                inputs.previous_review_id is not None
                and delta.history_comparability is not HistoryComparability.COMPARABLE
            ),
        )
        prior_result = self._comparable_prior_result(inputs)
        if prior_result is not None:
            if prior_result.thesis_review_state is not inputs.thesis_review_state:
                delta = _with_delta_reason(
                    delta,
                    "thesis_review_state_changed",
                    make_not_comparable=False,
                )
            if prior_result.review_disposition is not disposition:
                delta = _with_delta_reason(
                    delta,
                    "review_disposition_changed",
                    make_not_comparable=False,
                )
        redemption_feasibility = _redemption_feasibility(inputs, triggered_reviews)
        evidence_ids = tuple(
            sorted(
                {item.evidence_id for item in inputs.review_evidence_items}.union(support_ids)
            )
        )
        result = HoldingReviewResult(
            fund_code=inputs.fund_code,
            action=inputs.action,
            flow_status=flow_status,
            evidence_readiness=evidence_readiness,
            history_comparability=delta.history_comparability,
            thesis_review_state=inputs.thesis_review_state,
            review_disposition=disposition,
            triggered_reviews=triggered_reviews,
            official_event_evidence=inputs.official_event_evidence,
            redemption_feasibility=redemption_feasibility,
            redemption_evidence=inputs.redemption_evidence,
            sell_timing=self._policy.sell_timing,
            upstream_action_boundary=inputs.upstream_action_boundary,
            boundary=ReviewBoundary(),
            omitted_work=inputs.omitted_work,
            official_negative_check_complete=inputs.official_negative_check_complete,
            intelligence_schedule_complete=inputs.intelligence_schedule_complete,
            intelligence_omitted_work=inputs.intelligence_omitted_work,
            intelligence_degraded_sources=inputs.intelligence_degraded_sources,
            action_review_source_sufficiency=source_sufficiency,
            hard_event_review=hard_event_review,
            evidence_ids=evidence_ids,
            evidence_delta=delta,
            remainder_intent=inputs.remainder_intent,
            exit_reason=inputs.exit_reason,
            use_of_proceeds=inputs.use_of_proceeds,
            policy_version=self._policy.version,
            policy_checksum=self._policy.checksum(),
            created_at=inputs.now,
        )
        result.validate()
        return result

    def _evidence_delta(
        self,
        inputs: HoldingReviewInputs,
        current_coverage_complete: bool,
        support_ids: Tuple[str, ...],
    ) -> EvidenceDelta:
        prior = self._previous_review
        if prior is None:
            delta = compare_evidence(
                None,
                inputs.review_evidence_items,
                current_coverage_complete,
                current_additional_ids=support_ids,
            )
            if inputs.previous_review_id is not None:
                return _with_delta_reason(delta, "previous_review_missing")
            return delta

        mismatch_reasons = []
        if inputs.previous_review_id != self._previous_review_id:
            mismatch_reasons.append("previous_review_id_mismatch")
        if prior.fund_code != inputs.fund_code:
            mismatch_reasons.append("fund_mismatch")
        if prior.action is not inputs.action:
            mismatch_reasons.append("action_mismatch")
        if prior.active_thesis_fingerprint != inputs.thesis_fingerprint:
            mismatch_reasons.append("thesis_mismatch")
        if (
            prior.policy_version != inputs.policy_version
            or prior.policy_checksum != inputs.policy_checksum
        ):
            mismatch_reasons.append("policy_mismatch")
        if mismatch_reasons:
            return _noncomparable_delta(
                prior.result.evidence_ids,
                inputs.review_evidence_items,
                support_ids,
                tuple(sorted(mismatch_reasons)),
            )
        try:
            prior.validate()
        except (TypeError, ValueError):
            return _noncomparable_delta(
                (),
                inputs.review_evidence_items,
                support_ids,
                ("corrupted_history",),
            )
        if prior.created_at > inputs.now or prior.result.created_at > inputs.now:
            return _noncomparable_delta(
                prior.result.evidence_ids,
                inputs.review_evidence_items,
                support_ids,
                ("previous_review_from_future",),
            )
        return compare_evidence(
            prior,
            inputs.review_evidence_items,
            current_coverage_complete,
            prior_items=self._previous_evidence_items,
            current_additional_ids=support_ids,
            current_official_event_evidence=inputs.official_event_evidence,
        )

    def _comparable_prior_result(
        self,
        inputs: HoldingReviewInputs,
    ) -> Optional[HoldingReviewResult]:
        prior = self._previous_review
        if (
            prior is None
            or inputs.previous_review_id != self._previous_review_id
            or prior.fund_code != inputs.fund_code
            or prior.action is not inputs.action
            or prior.active_thesis_fingerprint != inputs.thesis_fingerprint
            or prior.policy_version != inputs.policy_version
            or prior.policy_checksum != inputs.policy_checksum
        ):
            return None
        try:
            prior.validate()
        except (TypeError, ValueError):
            return None
        if prior.created_at > inputs.now or prior.result.created_at > inputs.now:
            return None
        return prior.result


def _input_coverage_complete(inputs: HoldingReviewInputs) -> bool:
    return (
        inputs.official_negative_check_complete
        and inputs.intelligence_schedule_complete
        and not inputs.omitted_work
        and not inputs.intelligence_omitted_work
        and not inputs.intelligence_degraded_sources
        and all(
            item.current
            and item.graph_closed
            and not item.retracted
            and not item.conflicted
            and item.lineage_kind
            not in {LineageKind.CORRECTION_OF, LineageKind.RETRACTION_OF}
            for item in inputs.review_evidence_items
        )
    )


def _result_coverage_complete(result: HoldingReviewResult) -> bool:
    return (
        result.flow_status is FlowStatus.COMPLETE
        and result.evidence_readiness is EvidenceReadiness.READY
        and result.official_negative_check_complete
        and result.intelligence_schedule_complete
        and not result.omitted_work
        and not result.intelligence_omitted_work
        and not result.intelligence_degraded_sources
        and not result.evidence_delta.corrected_evidence_ids
        and not result.evidence_delta.retracted_evidence_ids
        and not result.evidence_delta.expired_evidence_ids
        and not result.evidence_delta.conflicted_evidence_ids
    )


def _readiness(
    inputs: HoldingReviewInputs,
    current_coverage_complete: bool,
) -> tuple[FlowStatus, EvidenceReadiness]:
    if inputs.thesis_review_state is ThesisMatchState.THESIS_BINDING_INVALID:
        return FlowStatus.FAILED, EvidenceReadiness.INSUFFICIENT_DATA
    if current_coverage_complete:
        return FlowStatus.COMPLETE, EvidenceReadiness.READY
    if inputs.thesis_review_state is ThesisMatchState.THESIS_MISSING:
        return FlowStatus.PARTIAL, EvidenceReadiness.INSUFFICIENT_DATA
    return FlowStatus.PARTIAL, EvidenceReadiness.PARTIAL


def _disposition(
    inputs: HoldingReviewInputs,
    *,
    current_coverage_complete: bool,
    source_sufficiency: ActionReviewSourceSufficiency,
    hard_event_review: bool,
    triggered_reviews: Tuple[TriggeredReviewCode, ...],
    history_blocks_reassurance: bool,
) -> ReviewDisposition:
    if inputs.thesis_review_state is ThesisMatchState.THESIS_BINDING_INVALID:
        return ReviewDisposition.ABSTAIN
    if not inputs.official_negative_check_complete:
        if inputs.thesis_review_state in _UNRESOLVED_THESIS_STATES:
            return ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED
        return ReviewDisposition.ABSTAIN
    if not current_coverage_complete:
        return ReviewDisposition.ABSTAIN
    if hard_event_review:
        if inputs.action is ActionKind.FULL_EXIT:
            return ReviewDisposition.EXIT_REVIEW
        return ReviewDisposition.ABSTAIN
    if triggered_reviews:
        return ReviewDisposition.ABSTAIN
    if inputs.thesis_review_state in _UNRESOLVED_THESIS_STATES:
        return ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED
    if inputs.thesis_review_state is ThesisMatchState.PRESENTED_MATCH_CONFIRMED:
        if inputs.action is ActionKind.CONTINUE_HOLDING:
            return ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED
        if source_sufficiency is not ActionReviewSourceSufficiency.SUFFICIENT:
            return ReviewDisposition.ABSTAIN
        if inputs.action is ActionKind.REDUCE_TO_CASH:
            return ReviewDisposition.REDUCE_REVIEW
        if inputs.action is ActionKind.FULL_EXIT:
            return ReviewDisposition.EXIT_REVIEW
        return ReviewDisposition.ABSTAIN
    if inputs.thesis_review_state in _NO_PENDING_MATCH_STATES:
        if (
            inputs.action is ActionKind.CONTINUE_HOLDING
            and not history_blocks_reassurance
        ):
            return ReviewDisposition.CONTINUE_OBSERVING
        return ReviewDisposition.ABSTAIN
    return ReviewDisposition.ABSTAIN


def _redemption_feasibility(
    inputs: HoldingReviewInputs,
    triggered_reviews: Tuple[TriggeredReviewCode, ...],
) -> RedemptionFeasibility:
    states = inputs.redemption_evidence.component_states()
    if inputs.action is ActionKind.CONTINUE_HOLDING:
        return RedemptionFeasibility.NOT_REQUESTED
    if (
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW in triggered_reviews
        and inputs.redemption_evidence.current_redemption_restriction
        is RedemptionComponentState.RESTRICTED
    ):
        return RedemptionFeasibility.RESTRICTED
    if all(state is RedemptionComponentState.USABLE for state in states):
        return RedemptionFeasibility.EVIDENCE_COMPLETE_NON_AUTHORIZING
    return RedemptionFeasibility.INSUFFICIENT_DATA


def _with_delta_reason(
    delta: EvidenceDelta,
    reason: str,
    *,
    make_not_comparable: bool = True,
) -> EvidenceDelta:
    value = replace(
        delta,
        history_comparability=(
            HistoryComparability.NOT_COMPARABLE
            if make_not_comparable
            and delta.history_comparability is HistoryComparability.COMPARABLE
            else delta.history_comparability
        ),
        evidence_unchanged=False,
        reason_codes=tuple(sorted(set(delta.reason_codes).union((reason,)))),
    )
    value.validate()
    return value


def _validate_evidence_items(
    items: Tuple[ReviewEvidenceItem, ...],
    name: str,
) -> None:
    if type(items) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    ids = []
    for item in items:
        if type(item) is not ReviewEvidenceItem:
            raise ValueError(f"{name} must contain exact records")
        item.validate()
        ids.append(item.evidence_id)
    if tuple(ids) != tuple(sorted(set(ids))):
        raise ValueError(f"{name} ids must be sorted and unique")


def _validate_official_references(
    references: Tuple[OfficialEventEvidenceReference, ...],
    name: str,
) -> None:
    if type(references) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    projection_ids = []
    for reference in references:
        if type(reference) is not OfficialEventEvidenceReference:
            raise ValueError(f"{name} must contain exact records")
        reference.validate()
        projection_ids.append(reference.projection_id)
    if tuple(projection_ids) != tuple(sorted(set(projection_ids))):
        raise ValueError(f"{name} projection ids must be sorted and unique")


def _noncomparable_delta(
    prior_ids: Tuple[str, ...],
    current_items: Tuple[ReviewEvidenceItem, ...],
    additional_ids: Tuple[str, ...],
    reasons: Tuple[str, ...],
) -> EvidenceDelta:
    current_ids = {item.evidence_id for item in current_items}.union(additional_ids)
    prior_set = set(prior_ids)
    value = EvidenceDelta(
        history_comparability=HistoryComparability.NOT_COMPARABLE,
        evidence_unchanged=False,
        added_evidence_ids=tuple(sorted(current_ids - prior_set)),
        removed_evidence_ids=tuple(sorted(prior_set - current_ids)),
        corrected_evidence_ids=tuple(
            sorted(
                item.evidence_id
                for item in current_items
                if item.lineage_kind is LineageKind.CORRECTION_OF
            )
        ),
        retracted_evidence_ids=tuple(
            sorted(
                item.evidence_id
                for item in current_items
                if item.retracted or item.lineage_kind is LineageKind.RETRACTION_OF
            )
        ),
        expired_evidence_ids=tuple(
            sorted(item.evidence_id for item in current_items if not item.current)
        ),
        conflicted_evidence_ids=tuple(
            sorted(item.evidence_id for item in current_items if item.conflicted)
        ),
        reason_codes=reasons,
    )
    value.validate()
    return value
