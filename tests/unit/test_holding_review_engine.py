from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import ActionKind
from kunjin.holding_review.engine import (
    HoldingReviewEngine,
    compare_evidence,
    determine_action_review_source_sufficiency,
)
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    BindingState,
    EvidenceDelta,
    EvidenceReadiness,
    ExitReason,
    FlowStatus,
    HistoryComparability,
    HoldingReviewInputs,
    HoldingReviewResult,
    HoldingReviewSnapshot,
    OfficialEventEvidenceReference,
    RedemptionComponentState,
    RedemptionEvidence,
    RedemptionFeasibility,
    RemainderIntent,
    ReviewBoundary,
    ReviewDisposition,
    ReviewEvidenceItem,
    ThesisMatchState,
    TriggeredReviewCode,
    UseOfProceeds,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1
from kunjin.intelligence.models import LineageKind

NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64
THESIS_FINGERPRINT = "b" * 64
D2_FUND_SCOPED_OMISSION_STEMS = (
    "adjusted_return_accumulated_nav_missing_",
    "adjusted_return_asymmetric_dates_",
    "adjusted_return_correlation_invalid_",
    "adjusted_return_date_order_invalid_",
    "adjusted_return_discontinuity_",
    "adjusted_return_duplicate_date_",
    "adjusted_return_evidence_binding_invalid_",
    "adjusted_return_evidence_conflict_",
    "adjusted_return_evidence_future_",
    "adjusted_return_evidence_incomplete_",
    "adjusted_return_evidence_stale_",
    "adjusted_return_observation_invalid_",
    "adjusted_return_samples_insufficient_",
    "adjusted_return_series_missing_",
    "adjusted_return_source_binding_invalid_",
    "adjusted_return_subject_mismatch_",
    "adjusted_return_zero_variance_",
    "authenticated_index_identity_",
    "benchmark_effective_date_conflict_",
    "coverage_evidence_budget_",
    "current_benchmark_",
    "current_benchmark_evidence_conflict_",
    "current_benchmark_evidence_future_",
    "current_benchmark_evidence_stale_",
    "current_manager_team_",
    "current_manager_team_evidence_conflict_",
    "current_manager_team_evidence_future_",
    "current_manager_team_evidence_stale_",
    "d2_fact_id_duplicate_",
    "d2_fact_set_invalid_",
    "holdings_duplicate_exposure_",
    "holdings_evidence_conflict_",
    "holdings_evidence_future_",
    "holdings_evidence_malformed_",
    "holdings_evidence_missing_",
    "holdings_evidence_stale_",
    "holdings_industries_",
    "identity_active_status_",
    "identity_active_status_evidence_conflict_",
    "identity_active_status_evidence_future_",
    "identity_active_status_evidence_stale_",
    "identity_evidence_conflict_",
    "identity_evidence_missing_",
    "identity_subject_conflict_",
    "manager_evidence_conflict_",
    "manager_evidence_missing_",
    "share_class_evidence_conflict_",
    "share_class_evidence_future_",
    "share_class_evidence_stale_",
    "share_class_identity_",
    "share_class_sibling_not_authenticated_",
    "share_class_sibling_unconfirmed_",
)


def evidence_item(evidence_id: str = "evidence_a", **changes: object) -> ReviewEvidenceItem:
    values: dict[str, object] = {
        "evidence_id": evidence_id,
        "source_tier": 1,
        "lineage_kind": LineageKind.ORIGINAL,
        "current": True,
        "graph_closed": True,
        "original_lineage": True,
        "retracted": False,
        "conflicted": False,
        "direct_subject_binding": True,
    }
    values.update(changes)
    return ReviewEvidenceItem(**values)  # type: ignore[arg-type]


def missing_redemption() -> RedemptionEvidence:
    missing = RedemptionComponentState.MISSING
    return RedemptionEvidence(missing, missing, missing, missing, missing, missing, missing)


def usable_redemption() -> RedemptionEvidence:
    usable = RedemptionComponentState.USABLE
    return RedemptionEvidence(usable, usable, usable, usable, usable, usable, usable)


def review_inputs(**changes: object) -> HoldingReviewInputs:
    policy = HeldFundManualReviewPolicyV1()
    values: dict[str, object] = {
        "fund_code": "123456",
        "action": ActionKind.CONTINUE_HOLDING,
        "brief_request_run_id": 11,
        "intelligence_request_run_id": 12,
        "thesis_review_state": ThesisMatchState.PRESENTED_MATCH_REJECTED,
        "review_evidence_items": (evidence_item(),),
        "official_event_evidence": (),
        "omitted_work": ("official_deep_confirmation_deferred",),
        "official_negative_check_complete": False,
        "intelligence_schedule_complete": True,
        "intelligence_omitted_work": (),
        "intelligence_degraded_sources": (),
        "upstream_action_boundary": ("research_only",),
        "redemption_evidence": missing_redemption(),
        "remainder_intent": RemainderIntent.UNKNOWN,
        "exit_reason": ExitReason.UNKNOWN,
        "use_of_proceeds": UseOfProceeds.UNKNOWN,
        "previous_review_id": None,
        "thesis_fingerprint": THESIS_FINGERPRINT,
        "policy_version": policy.version,
        "policy_checksum": policy.checksum(),
        "now": NOW,
    }
    values.update(changes)
    return HoldingReviewInputs(**values)  # type: ignore[arg-type]


def event_reference(
    event_code: OfficialEventCode = OfficialEventCode.FUND_LIQUIDATION_NOTICE,
    triggered_review_code: TriggeredReviewCode = (
        TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW
    ),
) -> OfficialEventEvidenceReference:
    return OfficialEventEvidenceReference(7, CHECKSUM, event_code, triggered_review_code)


def previous_result(evidence_ids: tuple[str, ...] = ("evidence_a",)) -> HoldingReviewResult:
    policy = HeldFundManualReviewPolicyV1()
    return HoldingReviewResult(
        fund_code="123456",
        action=ActionKind.CONTINUE_HOLDING,
        flow_status=FlowStatus.COMPLETE,
        evidence_readiness=EvidenceReadiness.READY,
        history_comparability=HistoryComparability.NOT_AVAILABLE,
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_REJECTED,
        review_disposition=ReviewDisposition.CONTINUE_OBSERVING,
        triggered_reviews=(),
        official_event_evidence=(),
        redemption_feasibility=RedemptionFeasibility.NOT_REQUESTED,
        redemption_evidence=missing_redemption(),
        sell_timing="insufficient_data",
        upstream_action_boundary=("research_only",),
        boundary=ReviewBoundary(),
        omitted_work=(),
        official_negative_check_complete=True,
        intelligence_schedule_complete=True,
        intelligence_omitted_work=(),
        intelligence_degraded_sources=(),
        action_review_source_sufficiency=ActionReviewSourceSufficiency.SUFFICIENT,
        hard_event_review=False,
        evidence_ids=evidence_ids,
        evidence_delta=EvidenceDelta(HistoryComparability.NOT_AVAILABLE, False),
        remainder_intent=RemainderIntent.UNKNOWN,
        exit_reason=ExitReason.UNKNOWN,
        use_of_proceeds=UseOfProceeds.UNKNOWN,
        policy_version=policy.version,
        policy_checksum=policy.checksum(),
        created_at=NOW - timedelta(days=1),
    )


def previous_snapshot(evidence_ids: tuple[str, ...] = ("evidence_a",)) -> HoldingReviewSnapshot:
    policy = HeldFundManualReviewPolicyV1()
    result = previous_result(evidence_ids)
    value = HoldingReviewSnapshot(
        fund_code="123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=1,
        brief_snapshot_id=2,
        brief_snapshot_checksum=CHECKSUM,
        intelligence_request_run_id=3,
        intelligence_snapshot_id=4,
        intelligence_snapshot_checksum=CHECKSUM,
        thesis_match_projection_id=5,
        thesis_match_projection_checksum=CHECKSUM,
        active_thesis_state=BindingState.PRESENT,
        active_thesis_id=6,
        active_thesis_fingerprint=THESIS_FINGERPRINT,
        adjudication_state=BindingState.PRESENT,
        adjudication_id=7,
        adjudication_checksum=CHECKSUM,
        previous_review_id=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
        policy_version=policy.version,
        policy_checksum=policy.checksum(),
        created_at=NOW - timedelta(hours=23),
        semantic_identity_checksum=CHECKSUM,
        record_checksum=CHECKSUM,
    )
    value = replace(value, semantic_identity_checksum=value.expected_semantic_identity_checksum())
    return replace(value, record_checksum=value.expected_record_checksum())


def previous_snapshot_with_omissions(
    omitted_work: tuple[str, ...],
) -> HoldingReviewSnapshot:
    prior = previous_snapshot()
    result = replace(prior.result, omitted_work=omitted_work)
    prior = replace(
        prior,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
    )
    prior = replace(
        prior,
        semantic_identity_checksum=prior.expected_semantic_identity_checksum(),
    )
    return replace(prior, record_checksum=prior.expected_record_checksum())


def previous_event_snapshot(
    reference: OfficialEventEvidenceReference,
) -> HoldingReviewSnapshot:
    prior = previous_snapshot()
    result = replace(
        prior.result,
        review_disposition=ReviewDisposition.ABSTAIN,
        triggered_reviews=(reference.triggered_review_code,),
        official_event_evidence=(reference,),
        evidence_ids=("evidence_a", reference.support_evidence_id),
    )
    prior = replace(
        prior,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
    )
    prior = replace(
        prior,
        semantic_identity_checksum=prior.expected_semantic_identity_checksum(),
    )
    return replace(prior, record_checksum=prior.expected_record_checksum())


def engine(previous: HoldingReviewSnapshot | None = None) -> HoldingReviewEngine:
    if previous is None:
        return HoldingReviewEngine()
    return HoldingReviewEngine(
        previous_review=previous,
        previous_review_id=1,
        previous_evidence_items=(evidence_item(),),
    )


def test_preview_gate_allows_only_manual_pending_or_abstain() -> None:
    pending = engine().evaluate(
        review_inputs(thesis_review_state=ThesisMatchState.MANUAL_REVIEW_PENDING)
    )
    confirmed = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
            redemption_evidence=usable_redemption(),
            exit_reason=ExitReason.OWNER_BELIEVES_THESIS_INVALIDATED,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )

    assert pending.review_disposition is ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED
    assert confirmed.review_disposition is ReviewDisposition.ABSTAIN
    for result in (pending, confirmed):
        assert result.official_negative_check_complete is False
        assert result.sell_timing == "insufficient_data"
        assert result.boundary == ReviewBoundary()


def test_hard_event_remains_visible_when_preview_gate_forces_abstention() -> None:
    reference = event_reference()
    result = engine().evaluate(review_inputs(official_event_evidence=(reference,)))

    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == (TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,)
    assert result.hard_event_review is True
    assert reference.support_evidence_id in result.evidence_ids


@pytest.mark.parametrize(
    "action,expected",
    (
        (ActionKind.REDUCE_TO_CASH, ReviewDisposition.REDUCE_REVIEW),
        (ActionKind.FULL_EXIT, ReviewDisposition.EXIT_REVIEW),
    ),
)
def test_closed_check_confirmed_match_is_action_specific(action, expected) -> None:
    owner_context = (
        {"remainder_intent": RemainderIntent.RETAIN_SOME}
        if action is ActionKind.REDUCE_TO_CASH
        else {
            "exit_reason": ExitReason.OWNER_BELIEVES_THESIS_INVALIDATED,
            "use_of_proceeds": UseOfProceeds.CASH_RESERVE,
        }
    )
    result = engine().evaluate(
        review_inputs(
            action=action,
            thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=usable_redemption(),
            **owner_context,
        )
    )

    assert result.review_disposition is expected
    assert result.boundary.action_authorized is False
    assert result.sell_timing == "insufficient_data"


@pytest.mark.parametrize(
    "changes",
    (
        {"source_tier": 2},
        {"current": False},
        {"graph_closed": False},
        {"retracted": True},
        {"conflicted": True},
        {"direct_subject_binding": False},
        {"lineage_kind": LineageKind.REPRINT, "original_lineage": False},
    ),
)
def test_source_sufficiency_rejects_weak_or_integrity_failed_evidence(changes) -> None:
    assert determine_action_review_source_sufficiency((evidence_item(**changes),)) is (
        ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    )


def test_source_sufficiency_requires_every_supporting_item_to_be_valid() -> None:
    items = (evidence_item("direct"), evidence_item("weak", graph_closed=False))
    assert determine_action_review_source_sufficiency(items) is (
        ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    )
    assert determine_action_review_source_sufficiency((evidence_item(),)) is (
        ActionReviewSourceSufficiency.SUFFICIENT
    )


def test_tier_two_or_same_lineage_reprints_cannot_support_action_review() -> None:
    tier_two = evidence_item(source_tier=2)
    reprints = (
        evidence_item(
            "reprint_a", lineage_kind=LineageKind.REPRINT, original_lineage=False
        ),
        evidence_item(
            "reprint_b", lineage_kind=LineageKind.REPRINT, original_lineage=False
        ),
    )
    assert determine_action_review_source_sufficiency((tier_two,)) is (
        ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    )
    assert determine_action_review_source_sufficiency(reprints) is (
        ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    )


def test_weak_confirmed_match_abstains_even_after_negative_check_is_closed() -> None:
    result = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
            review_evidence_items=(evidence_item(source_tier=2),),
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=usable_redemption(),
            exit_reason=ExitReason.OWNER_BELIEVES_THESIS_INVALIDATED,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.action_review_source_sufficiency is (
        ActionReviewSourceSufficiency.INSUFFICIENT_DATA
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"intelligence_schedule_complete": False},
        {"intelligence_omitted_work": ("source_failed",)},
        {"intelligence_degraded_sources": ("tier2_only",)},
        {"omitted_work": ("formal_nav",)},
    ),
)
def test_closed_negative_check_still_abstains_on_any_required_gap(changes) -> None:
    values = {"official_negative_check_complete": True, "omitted_work": ()}
    values.update(changes)
    result = engine().evaluate(review_inputs(**values))
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.evidence_readiness is not EvidenceReadiness.READY


def test_phase_boundaries_and_nav_movement_are_preserved_but_do_not_trigger_action() -> None:
    boundary = ("one_day_nav_decline", "phase_b_blocked", "phase_c_constrained")
    result = engine().evaluate(review_inputs(upstream_action_boundary=boundary))
    assert result.upstream_action_boundary == boundary
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == ()


def test_closed_complete_no_candidate_can_only_continue_observing() -> None:
    result = engine().evaluate(
        review_inputs(
            thesis_review_state=ThesisMatchState.NO_MATCHING_EVIDENCE,
            review_evidence_items=(),
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.review_disposition is ReviewDisposition.CONTINUE_OBSERVING
    assert result.boundary == ReviewBoundary()
    assert result.sell_timing == "insufficient_data"


@pytest.mark.parametrize("stem", D2_FUND_SCOPED_OMISSION_STEMS)
def test_other_fund_gap_remains_visible_without_blocking_selected_fund(stem: str) -> None:
    gap = f"{stem}654321"
    result = engine().evaluate(
        review_inputs(
            official_negative_check_complete=True,
            omitted_work=(gap,),
        )
    )

    assert result.omitted_work == (gap,)
    assert result.flow_status is FlowStatus.COMPLETE
    assert result.evidence_readiness is EvidenceReadiness.READY
    assert result.review_disposition is ReviewDisposition.CONTINUE_OBSERVING


@pytest.mark.parametrize("stem", D2_FUND_SCOPED_OMISSION_STEMS)
def test_selected_fund_scoped_gap_still_blocks(stem: str) -> None:
    result = engine().evaluate(
        review_inputs(
            official_negative_check_complete=True,
            omitted_work=(f"{stem}123456",),
        )
    )

    assert result.flow_status is FlowStatus.PARTIAL
    assert result.review_disposition is ReviewDisposition.ABSTAIN


@pytest.mark.parametrize(
    "gap",
    (
        "adjusted_return_common_end_mismatch_123456_654321",
        "authenticated_index_identity_000000",
        "authenticated_index_identity_extra_654321",
        "formal_nav",
        "holdings_overlap_invalid_123456_654321",
        "holdings_pair_comparability_123456_654321",
        "holdings_report_period_unaligned_123456_654321",
        "identity_scope_unknown",
        "source_attempt_654321",
    ),
)
def test_selected_or_unscoped_gap_still_blocks(gap: str) -> None:
    result = engine().evaluate(
        review_inputs(
            official_negative_check_complete=True,
            omitted_work=(gap,),
        )
    )

    assert result.flow_status is FlowStatus.PARTIAL
    assert result.review_disposition is ReviewDisposition.ABSTAIN


def test_preview_without_official_negative_check_never_becomes_ready() -> None:
    result = engine().evaluate(
        review_inputs(
            official_negative_check_complete=False,
            omitted_work=(),
        )
    )

    assert result.flow_status is FlowStatus.PARTIAL
    assert result.evidence_readiness is EvidenceReadiness.PARTIAL
    assert result.review_disposition is ReviewDisposition.ABSTAIN


@pytest.mark.parametrize(
    "state",
    (ThesisMatchState.NO_MATCHING_EVIDENCE, ThesisMatchState.PRESENTED_MATCH_REJECTED),
)
@pytest.mark.parametrize("action", (ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT))
def test_no_candidate_cannot_reassure_reduce_or_exit_request(state, action) -> None:
    context = (
        {"remainder_intent": RemainderIntent.RETAIN_SOME}
        if action is ActionKind.REDUCE_TO_CASH
        else {
            "exit_reason": ExitReason.RISK_REDUCTION,
            "use_of_proceeds": UseOfProceeds.CASH_RESERVE,
        }
    )
    result = engine().evaluate(
        review_inputs(
            action=action,
            thesis_review_state=state,
            review_evidence_items=(),
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=usable_redemption(),
            **context,
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN


def test_confirmed_match_on_continue_request_requires_manual_thesis_review() -> None:
    result = engine().evaluate(
        review_inputs(
            thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.thesis_review_state is ThesisMatchState.PRESENTED_MATCH_CONFIRMED
    assert result.review_disposition is ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED


@pytest.mark.parametrize(
    "reference",
    (
        event_reference(
            OfficialEventCode.MANAGER_CHANGE_NOTICE,
            TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
        ),
        event_reference(
            OfficialEventCode.FEE_CHANGE_NOTICE,
            TriggeredReviewCode.FEE_CHANGE_REVIEW,
        ),
        event_reference(
            OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
            TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW,
        ),
    ),
)
def test_closed_non_hard_event_abstains_and_preserves_trigger(reference) -> None:
    result = engine().evaluate(
        review_inputs(
            official_event_evidence=(reference,),
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == (reference.triggered_review_code,)
    assert result.hard_event_review is False


def test_closed_redemption_restriction_abstains_and_preserves_trigger() -> None:
    reference = event_reference(
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
    )
    redemption = replace(
        usable_redemption(),
        current_redemption_restriction=RedemptionComponentState.RESTRICTED,
    )
    result = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            official_event_evidence=(reference,),
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=redemption,
            exit_reason=ExitReason.RISK_REDUCTION,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == (TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,)
    assert result.redemption_feasibility is RedemptionFeasibility.RESTRICTED


@pytest.mark.parametrize(
    "restriction_state",
    (
        RedemptionComponentState.MISSING,
        RedemptionComponentState.STALE,
        RedemptionComponentState.UNSUPPORTED,
    ),
)
def test_redemption_restriction_with_incomplete_component_is_stable_insufficient_data(
    restriction_state,
) -> None:
    reference = event_reference(
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
    )
    redemption = replace(
        usable_redemption(),
        current_redemption_restriction=restriction_state,
    )
    result = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            official_event_evidence=(reference,),
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=redemption,
            exit_reason=ExitReason.RISK_REDUCTION,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == (TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,)
    assert result.redemption_feasibility is RedemptionFeasibility.INSUFFICIENT_DATA


def test_thesis_binding_invalid_fails_closed_but_preserves_hard_event() -> None:
    reference = event_reference()
    result = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            thesis_review_state=ThesisMatchState.THESIS_BINDING_INVALID,
            official_event_evidence=(reference,),
            official_negative_check_complete=True,
            omitted_work=(),
            redemption_evidence=usable_redemption(),
            exit_reason=ExitReason.RISK_REDUCTION,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )
    assert result.flow_status is FlowStatus.FAILED
    assert result.evidence_readiness is EvidenceReadiness.INSUFFICIENT_DATA
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.triggered_reviews == (TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,)


def test_redemption_restriction_is_visible_independently() -> None:
    reference = event_reference(
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
    )
    evidence = replace(
        usable_redemption(),
        current_redemption_restriction=RedemptionComponentState.RESTRICTED,
    )
    result = engine().evaluate(
        review_inputs(
            action=ActionKind.FULL_EXIT,
            official_event_evidence=(reference,),
            redemption_evidence=evidence,
            exit_reason=ExitReason.CASH_NEED,
            use_of_proceeds=UseOfProceeds.CASH_RESERVE,
        )
    )
    assert result.redemption_feasibility is RedemptionFeasibility.RESTRICTED
    assert result.triggered_reviews == (TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,)
    assert result.review_disposition is ReviewDisposition.ABSTAIN


def test_evidence_delta_reports_added_removed_and_integrity_changes() -> None:
    current = (
        evidence_item("added"),
        evidence_item(
            "corrected",
            lineage_kind=LineageKind.CORRECTION_OF,
            original_lineage=False,
        ),
        evidence_item("expired", current=False),
        evidence_item("conflicted", conflicted=True),
        evidence_item("retracted", retracted=True),
    )
    prior = previous_snapshot(
        ("conflicted", "corrected", "expired", "removed", "retracted")
    )
    delta = compare_evidence(prior, current, current_coverage_complete=True)

    assert delta.added_evidence_ids == ("added",)
    assert delta.removed_evidence_ids == ("removed",)
    assert delta.corrected_evidence_ids == ("corrected",)
    assert delta.retracted_evidence_ids == ("retracted",)
    assert delta.expired_evidence_ids == ("expired",)
    assert delta.conflicted_evidence_ids == ("conflicted",)


def test_coverage_loss_never_claims_unchanged() -> None:
    delta = compare_evidence(
        previous_snapshot(("evidence_a", "evidence_b")),
        (evidence_item("evidence_a"),),
        current_coverage_complete=False,
    )
    assert delta.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert delta.evidence_unchanged is False
    assert delta.removed_evidence_ids == ("evidence_b",)
    assert "coverage_decreased" in delta.reason_codes


def test_source_failure_cannot_be_described_as_unchanged() -> None:
    result = engine(previous_snapshot()).evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
            intelligence_schedule_complete=False,
            intelligence_omitted_work=("source_failed",),
        )
    )
    assert result.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert result.evidence_delta.evidence_unchanged is False
    assert "coverage_decreased" in result.evidence_delta.reason_codes


def test_no_prior_review_is_not_available_not_unchanged() -> None:
    delta = compare_evidence(None, (evidence_item(),), current_coverage_complete=True)
    assert delta.history_comparability is HistoryComparability.NOT_AVAILABLE
    assert delta.evidence_unchanged is False


@pytest.mark.parametrize(
    "snapshot_change,input_change,reason",
    (
        ({"fund_code": "654321"}, {}, "fund_mismatch"),
        ({"action": ActionKind.FULL_EXIT}, {}, "action_mismatch"),
        ({"active_thesis_fingerprint": "c" * 64}, {}, "thesis_mismatch"),
        ({"policy_checksum": "d" * 64}, {}, "policy_mismatch"),
        ({}, {"previous_review_id": 999}, "previous_review_id_mismatch"),
    ),
)
def test_engine_rejects_noncomparable_prior_identity(snapshot_change, input_change, reason) -> None:
    prior = replace(previous_snapshot(), **snapshot_change)
    values = {"previous_review_id": 1}
    values.update(input_change)
    result = engine(prior).evaluate(review_inputs(**values))
    assert result.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert reason in result.evidence_delta.reason_codes
    assert result.evidence_delta.evidence_unchanged is False


def test_exact_comparable_history_can_report_unchanged_only_with_closed_coverage() -> None:
    prior = previous_snapshot()
    result = engine(prior).evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.history_comparability is HistoryComparability.COMPARABLE
    assert result.evidence_delta.evidence_unchanged is True


def test_other_fund_gap_does_not_create_false_history_coverage_loss() -> None:
    other_fund_gap = "authenticated_index_identity_654321"
    prior = previous_snapshot_with_omissions((other_fund_gap,))
    review_engine = engine(prior)

    repeated = review_engine.evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(other_fund_gap,),
        )
    )
    assert repeated.history_comparability is HistoryComparability.COMPARABLE
    assert repeated.evidence_delta.evidence_unchanged is True
    assert "coverage_decreased" not in repeated.evidence_delta.reason_codes


@pytest.mark.parametrize(
    "prior_gaps,current_gaps",
    (
        ((), ("authenticated_index_identity_654321",)),
        (("authenticated_index_identity_654321",), ()),
        (
            ("authenticated_index_identity_654321",),
            ("current_benchmark_654321",),
        ),
    ),
)
def test_other_fund_gap_change_is_comparable_but_not_unchanged(
    prior_gaps: tuple[str, ...],
    current_gaps: tuple[str, ...],
) -> None:
    result = engine(previous_snapshot_with_omissions(prior_gaps)).evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=current_gaps,
        )
    )

    assert result.history_comparability is HistoryComparability.COMPARABLE
    assert result.evidence_delta.evidence_unchanged is False
    assert result.evidence_delta.reason_codes == ("portfolio_coverage_gaps_changed",)


def test_selected_gap_still_causes_history_coverage_loss() -> None:
    result = engine(previous_snapshot()).evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=("authenticated_index_identity_123456",),
        )
    )

    assert result.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert result.evidence_delta.evidence_unchanged is False
    assert "coverage_decreased" in result.evidence_delta.reason_codes


def test_previous_review_constructor_arguments_are_strictly_paired() -> None:
    prior = previous_snapshot()
    with pytest.raises(ValueError, match="provided together"):
        HoldingReviewEngine(previous_review=prior)
    with pytest.raises(ValueError, match="provided together"):
        HoldingReviewEngine(previous_review_id=1)
    with pytest.raises(ValueError, match="provided together"):
        HoldingReviewEngine(previous_evidence_items=(evidence_item(),))
    with pytest.raises(ValueError, match="positive exact integer"):
        HoldingReviewEngine(
            previous_review=prior,
            previous_review_id=True,
            previous_evidence_items=(evidence_item(),),
        )


@pytest.mark.parametrize("mode", ("missing", "mismatch", "corrupt"))
def test_requested_untrusted_history_blocks_continue_observing(mode) -> None:
    prior = previous_snapshot()
    if mode == "missing":
        review_engine = HoldingReviewEngine()
        inputs = review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    else:
        if mode == "corrupt":
            prior = replace(prior, record_checksum="f" * 64)
        review_engine = HoldingReviewEngine(
            previous_review=prior,
            previous_review_id=1,
            previous_evidence_items=(evidence_item(),),
        )
        inputs = review_inputs(
            previous_review_id=2 if mode == "mismatch" else 1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    result = review_engine.evaluate(inputs)
    assert result.history_comparability is not HistoryComparability.COMPARABLE
    assert result.evidence_delta.evidence_unchanged is False
    assert result.review_disposition is ReviewDisposition.ABSTAIN


@pytest.mark.parametrize("future_field", ("snapshot", "result"))
def test_future_prior_review_is_not_comparable_and_blocks_reassurance(
    future_field,
) -> None:
    prior = previous_snapshot()
    if future_field == "result":
        result = replace(prior.result, created_at=NOW + timedelta(seconds=1))
        future = replace(
            prior,
            result=result,
            result_fingerprint=result.expected_result_fingerprint(),
            created_at=NOW + timedelta(seconds=2),
        )
        future = replace(
            future,
            semantic_identity_checksum=future.expected_semantic_identity_checksum(),
        )
    else:
        future = replace(prior, created_at=NOW + timedelta(seconds=1))
    future = replace(future, record_checksum=future.expected_record_checksum())
    result = engine(future).evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert "previous_review_from_future" in result.evidence_delta.reason_codes


def test_same_id_descriptor_change_is_not_unchanged() -> None:
    prior = previous_snapshot()
    review_engine = HoldingReviewEngine(
        previous_review=prior,
        previous_review_id=1,
        previous_evidence_items=(evidence_item(source_tier=2),),
    )
    result = review_engine.evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.evidence_delta.evidence_unchanged is False
    assert "evidence_descriptor_changed" in result.evidence_delta.reason_codes


def test_empty_prior_descriptors_cannot_bypass_exact_coverage() -> None:
    prior = previous_snapshot()
    review_engine = HoldingReviewEngine(
        previous_review=prior,
        previous_review_id=1,
        previous_evidence_items=(),
    )
    result = review_engine.evaluate(
        review_inputs(
            previous_review_id=1,
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert "previous_evidence_descriptor_set_mismatch" in result.evidence_delta.reason_codes


def test_same_official_support_id_with_checksum_drift_is_not_comparable() -> None:
    prior_reference = event_reference(
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
    )
    current_reference = replace(prior_reference, projection_checksum="c" * 64)
    prior = previous_event_snapshot(prior_reference)
    review_engine = HoldingReviewEngine(
        previous_review=prior,
        previous_review_id=1,
        previous_evidence_items=(evidence_item(),),
    )
    result = review_engine.evaluate(
        review_inputs(
            previous_review_id=1,
            official_event_evidence=(current_reference,),
            official_negative_check_complete=True,
            omitted_work=(),
        )
    )
    assert result.review_disposition is ReviewDisposition.ABSTAIN
    assert result.history_comparability is HistoryComparability.NOT_COMPARABLE
    assert result.evidence_delta.evidence_unchanged is False
    assert "official_event_reference_changed" in result.evidence_delta.reason_codes


def test_history_reports_thesis_state_and_disposition_changes() -> None:
    result = engine(previous_snapshot()).evaluate(
        review_inputs(
            previous_review_id=1,
            thesis_review_state=ThesisMatchState.NO_MATCHING_EVIDENCE,
            official_negative_check_complete=False,
        )
    )
    assert "thesis_review_state_changed" in result.evidence_delta.reason_codes
    assert "review_disposition_changed" in result.evidence_delta.reason_codes
