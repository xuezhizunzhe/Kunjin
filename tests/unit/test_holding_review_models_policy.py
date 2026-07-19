from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.decision.models import ActionKind
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    AdjudicationDecision,
    ConditionalReviewUsability,
    EvidenceReadiness,
    ExitReason,
    FlowStatus,
    HistoryComparability,
    HoldingReviewResult,
    RedemptionComponentState,
    RedemptionEvidence,
    RedemptionFeasibility,
    RemainderIntent,
    ReviewBoundary,
    ReviewDisposition,
    ThesisMatchProjectionState,
    ThesisMatchState,
    ThesisReviewReadiness,
    TriggeredReviewCode,
    UseOfProceeds,
)
from kunjin.holding_review.policy import (
    HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM,
    HeldFundManualReviewPolicyV1,
)

NOW = datetime(2026, 7, 19, 4, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def redemption_evidence() -> RedemptionEvidence:
    return RedemptionEvidence(
        current_position=RedemptionComponentState.USABLE,
        exact_share_class=RedemptionComponentState.USABLE,
        applicable_holding_period_tier=RedemptionComponentState.MISSING,
        channel_rule=RedemptionComponentState.MISSING,
        current_redemption_restriction=RedemptionComponentState.USABLE,
        applicable_fee_schedule=RedemptionComponentState.USABLE,
        settlement_rule=RedemptionComponentState.MISSING,
    )


def review_result(**changes: object) -> HoldingReviewResult:
    values: dict[str, object] = {
        "fund_code": "123456",
        "action": ActionKind.CONTINUE_HOLDING,
        "flow_status": FlowStatus.COMPLETE,
        "evidence_readiness": EvidenceReadiness.READY,
        "history_comparability": HistoryComparability.NOT_AVAILABLE,
        "thesis_review_state": ThesisMatchState.PRESENTED_MATCH_REJECTED,
        "review_disposition": ReviewDisposition.CONTINUE_OBSERVING,
        "triggered_reviews": (),
        "redemption_feasibility": RedemptionFeasibility.NOT_REQUESTED,
        "redemption_evidence": redemption_evidence(),
        "sell_timing": "insufficient_data",
        "upstream_action_boundary": ("research_only",),
        "boundary": ReviewBoundary(),
        "omitted_work": (),
        "official_negative_check_complete": True,
        "intelligence_schedule_complete": True,
        "intelligence_omitted_work": (),
        "intelligence_degraded_sources": (),
        "action_review_source_sufficiency": ActionReviewSourceSufficiency.SUFFICIENT,
        "hard_event_review": False,
        "evidence_ids": ("evidence_a",),
        "policy_version": "1",
        "policy_checksum": CHECKSUM,
        "created_at": NOW,
    }
    values.update(changes)
    return HoldingReviewResult(**values)  # type: ignore[arg-type]


def test_every_closed_enum_has_the_reviewed_values() -> None:
    assert tuple(item.value for item in FlowStatus) == ("complete", "partial", "failed")
    assert tuple(item.value for item in ReviewDisposition) == (
        "continue_observing",
        "manual_thesis_review_required",
        "reduce_review",
        "exit_review",
        "abstain",
    )
    assert tuple(item.value for item in ThesisMatchState) == (
        "thesis_missing",
        "no_matching_evidence",
        "manual_review_pending",
        "manual_review_uncertain",
        "presented_match_rejected",
        "presented_match_confirmed",
        "thesis_binding_invalid",
    )
    assert tuple(item.value for item in AdjudicationDecision) == (
        "presented_match_confirmed",
        "presented_match_rejected",
        "uncertain",
    )
    assert tuple(item.value for item in RedemptionFeasibility) == (
        "not_requested",
        "insufficient_data",
        "restricted",
        "evidence_complete_non_authorizing",
    )
    assert tuple(item.value for item in EvidenceReadiness) == (
        "ready",
        "partial",
        "insufficient_data",
    )
    assert tuple(item.value for item in HistoryComparability) == (
        "comparable",
        "not_comparable",
        "not_available",
    )
    assert tuple(item.value for item in ThesisMatchProjectionState) == (
        "thesis_missing",
        "no_matching_evidence",
        "possible_invalidation_match",
    )
    assert tuple(item.value for item in ActionReviewSourceSufficiency) == (
        "sufficient",
        "insufficient_data",
    )
    assert tuple(item.value for item in RedemptionComponentState) == (
        "usable",
        "restricted",
        "missing",
        "stale",
        "conflicted",
        "unsupported",
    )
    assert tuple(item.value for item in TriggeredReviewCode) == (
        "full_exit_feasibility_review",
        "redemption_restriction_review",
        "manager_change_review",
        "fee_change_review",
        "benchmark_change_review",
    )
    assert tuple(item.value for item in RemainderIntent) == (
        "retain_some",
        "no_minimum_intent",
        "unknown",
    )
    assert tuple(item.value for item in ExitReason) == (
        "owner_believes_thesis_invalidated",
        "goal_changed",
        "cash_need",
        "risk_reduction",
        "other",
        "unknown",
    )
    assert tuple(item.value for item in UseOfProceeds) == (
        "cash_reserve",
        "known_goal",
        "reallocation_review",
        "other",
        "unknown",
    )
    assert tuple(item.value for item in ThesisReviewReadiness) == (
        "ready",
        "manual_review_required",
        "missing",
        "insufficient_data",
    )
    assert tuple(item.value for item in ConditionalReviewUsability) == (
        "observed_for_request",
        "partial",
        "not_testable",
    )


def test_minimal_review_result_is_canonical_and_valid() -> None:
    result = review_result()
    result.validate()
    canonical = result.canonical_json()
    assert canonical == json.dumps(
        json.loads(canonical), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    assert result.checksum() == result.checksum()
    assert result.to_canonical_dict()["review_disposition"] == "continue_observing"


def test_review_boundary_is_permanently_non_authorizing() -> None:
    boundary = ReviewBoundary()
    boundary.validate()
    assert boundary.review_maturity == "evidence_only"
    assert boundary.action_authorized is False
    assert boundary.exact_amount_available is False
    assert boundary.automatic_trade is False


@pytest.mark.parametrize(
    "omitted",
    (
        "identity_profile",
        "personal_position_observation",
        "formal_nav",
        "manager_fee_profile",
        "holdings_industries",
        "official_announcements",
    ),
)
def test_continue_observing_rejects_each_core_omission(omitted: str) -> None:
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(omitted_work=(omitted,)).validate()


@pytest.mark.parametrize(
    "changes",
    (
        {"intelligence_schedule_complete": False},
        {"intelligence_omitted_work": ("stcn_detail_cap_reached",)},
        {"intelligence_degraded_sources": ("stcn_fund_news",)},
    ),
)
def test_continue_observing_rejects_incomplete_intelligence_negative_check(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(**changes).validate()


def test_owner_confirmation_cannot_upgrade_source_sufficiency() -> None:
    result = review_result(
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
        action=ActionKind.FULL_EXIT,
        action_review_source_sufficiency=ActionReviewSourceSufficiency.INSUFFICIENT_DATA,
        review_disposition=ReviewDisposition.EXIT_REVIEW,
    )
    with pytest.raises(ValueError, match="exit review source evidence is insufficient"):
        result.validate()


@pytest.mark.parametrize("invalid", (True, False, 0, -1, "1"))
def test_database_request_run_ids_are_positive_exact_integers(invalid: object) -> None:
    from kunjin.holding_review.models import HoldingReviewInputs

    with pytest.raises(ValueError, match="positive exact integer"):
        HoldingReviewInputs.minimal(
            fund_code="123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=invalid,  # type: ignore[arg-type]
            intelligence_request_run_id=2,
            now=NOW,
            policy_checksum=CHECKSUM,
        ).validate()


@pytest.mark.parametrize("fund_code", ("000000", "12345", "１２３４５６", 123456))
def test_fund_code_is_six_ascii_digits_and_non_reserved(fund_code: object) -> None:
    with pytest.raises(ValueError, match="non-reserved six-digit"):
        replace(review_result(), fund_code=fund_code).validate()  # type: ignore[arg-type]


def test_exact_types_subclasses_sorted_unique_ids_and_utc_are_required() -> None:
    class ResultSubclass(HoldingReviewResult):
        pass

    with pytest.raises(ValueError, match="exact HoldingReviewResult"):
        ResultSubclass(**vars(review_result())).validate()
    with pytest.raises(ValueError, match="sorted unique"):
        replace(review_result(), evidence_ids=("evidence_b", "evidence_a")).validate()
    with pytest.raises(ValueError, match="UTC"):
        replace(review_result(), created_at=NOW.astimezone(timezone(timedelta(hours=8)))).validate()
    with pytest.raises(ValueError, match="exact boolean"):
        replace(review_result(), official_negative_check_complete=1).validate()  # type: ignore[arg-type]


def test_triggered_reviews_and_redemption_components_are_exact() -> None:
    with pytest.raises(ValueError, match="triggered reviews"):
        replace(review_result(), triggered_reviews=("unknown_review",)).validate()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact RedemptionComponentState"):
        replace(redemption_evidence(), settlement_rule="missing").validate()  # type: ignore[arg-type]


def test_noncanonical_checksum_and_private_instance_key_are_rejected() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(review_result(), policy_checksum="A" * 64).validate()
    result = review_result()
    object.__setattr__(result, "shares", "private")
    with pytest.raises(ValueError, match="unexpected instance state"):
        result.validate()


def test_policy_v1_is_frozen_canonical_and_has_a_golden_checksum() -> None:
    policy = HeldFundManualReviewPolicyV1()
    policy.validate()
    assert policy.orchestration_window_seconds == 30 * 60
    assert policy.maximum_announcement_candidates == 20
    assert policy.maximum_announcement_body_bytes == 512 * 1024
    assert policy.maximum_announcement_total_bytes == 4 * 1024 * 1024
    assert policy.sell_timing == "insufficient_data"
    assert policy.action_authorized is False
    assert policy.exact_amount_available is False
    assert policy.automatic_trade is False
    assert policy.checksum() == HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM
    with pytest.raises(FrozenInstanceError):
        policy.orchestration_window_seconds = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical"):
        replace(policy, orchestration_window_seconds=1).validate()


def test_policy_rejects_subclasses() -> None:
    class DerivedPolicy(HeldFundManualReviewPolicyV1):
        pass

    with pytest.raises(ValueError, match="subclasses"):
        DerivedPolicy().validate()
