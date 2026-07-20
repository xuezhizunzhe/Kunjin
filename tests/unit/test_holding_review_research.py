from __future__ import annotations

import json
from dataclasses import replace

from kunjin.decision.models import ActionKind
from kunjin.holding_review.models import (
    BindingState,
    FlowStatus,
    HoldingReviewOutcome,
    HoldingReviewSnapshot,
    ReviewDisposition,
    ThesisMatchState,
    TransientHoldingReviewOutcome,
)
from kunjin.holding_review.research import public_holding_review_payload
from tests.unit.test_holding_review_engine import (
    CHECKSUM,
    NOW,
    THESIS_FINGERPRINT,
    engine,
    event_reference,
    review_inputs,
)


def _preview_outcome(
    thesis_state: ThesisMatchState = ThesisMatchState.NO_MATCHING_EVIDENCE,
    *,
    with_hard_event: bool = False,
) -> HoldingReviewOutcome:
    result = engine().evaluate(
        review_inputs(
            thesis_review_state=thesis_state,
            official_event_evidence=(event_reference(),) if with_hard_event else (),
        )
    )
    adjudicated = thesis_state in {
        ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
        ThesisMatchState.PRESENTED_MATCH_REJECTED,
        ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
    }
    snapshot = HoldingReviewSnapshot(
        fund_code="123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=11,
        brief_snapshot_id=21,
        brief_snapshot_checksum=CHECKSUM,
        intelligence_request_run_id=12,
        intelligence_snapshot_id=22,
        intelligence_snapshot_checksum=CHECKSUM,
        thesis_match_projection_id=31,
        thesis_match_projection_checksum=CHECKSUM,
        active_thesis_state=BindingState.PRESENT,
        active_thesis_id=41,
        active_thesis_fingerprint=THESIS_FINGERPRINT,
        adjudication_state=(BindingState.PRESENT if adjudicated else BindingState.MISSING),
        adjudication_id=51 if adjudicated else None,
        adjudication_checksum=CHECKSUM if adjudicated else None,
        previous_review_id=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
        policy_version=result.policy_version,
        policy_checksum=result.policy_checksum,
        created_at=NOW,
        semantic_identity_checksum="0" * 64,
        record_checksum="0" * 64,
    )
    snapshot = replace(
        snapshot,
        semantic_identity_checksum=snapshot.expected_semantic_identity_checksum(),
    )
    snapshot = replace(snapshot, record_checksum=snapshot.expected_record_checksum())
    outcome = HoldingReviewOutcome(result.flow_status, snapshot)
    outcome.validate()
    return outcome


def test_preview_explains_official_gap_without_absence_claim() -> None:
    payload = public_holding_review_payload(_preview_outcome())

    assert payload["official_negative_check_complete"] is False
    assert "official_confirmation_required" in payload["gap_codes"]
    assert payload["interpretation"]["review_disposition"] == ReviewDisposition.ABSTAIN.value
    assert "没有重大风险" not in payload["interpretation"]["headline"]
    assert "没有重大风险" not in json.dumps(payload, ensure_ascii=False)


def test_public_review_preserves_evidence_and_is_not_trade_authorization() -> None:
    payload = public_holding_review_payload(_preview_outcome())

    assert payload["facts"]["evidence_ids"] == ["evidence_a"]
    assert payload["dates"]["review_created_at"] == NOW.isoformat()
    assert payload["evidence_delta"]["history_comparability"] == "not_available"
    assert payload["candidate_thesis_match"]["projection_id"] == 31
    assert payload["owner_adjudication"] == {
        "state": "missing",
        "adjudication_id": None,
        "adjudication_checksum": None,
    }
    assert payload["sell_timing"] == "insufficient_data"
    assert payload["upstream_action_boundary"] == ["research_only"]
    assert payload["review_boundary"] == {
        "review_maturity": "evidence_only",
        "action_authorized": False,
        "exact_amount_available": False,
        "automatic_trade": False,
    }


def test_source_sufficiency_is_explicit_and_independent_from_owner_adjudication() -> None:
    projected = public_holding_review_payload(_preview_outcome())
    adjudicated = public_holding_review_payload(
        _preview_outcome(ThesisMatchState.PRESENTED_MATCH_REJECTED)
    )

    assert projected["action_review_source_sufficiency"] == "sufficient"
    assert adjudicated["action_review_source_sufficiency"] == "sufficient"
    assert projected["owner_adjudication"]["state"] == "missing"
    assert adjudicated["owner_adjudication"]["state"] == "present"


def test_hard_event_keeps_facts_separate_from_interpretation_and_authorization() -> None:
    payload = public_holding_review_payload(_preview_outcome(with_hard_event=True))

    assert len(payload["facts"]["official_event_evidence"]) == 1
    assert "triggered_reviews" not in payload["facts"]
    assert payload["interpretation"]["triggered_reviews"] == [
        "full_exit_feasibility_review"
    ]
    assert payload["interpretation"]["review_disposition"] == "abstain"
    assert payload["review_boundary"]["action_authorized"] is False
    assert payload["review_boundary"]["exact_amount_available"] is False
    assert payload["review_boundary"]["automatic_trade"] is False


def test_transient_review_fails_closed_with_visible_missing_snapshots() -> None:
    outcome = TransientHoldingReviewOutcome(
        flow_status=FlowStatus.PARTIAL,
        review_snapshot=None,
        missing_snapshot_codes=("brief_snapshot_missing",),
    )

    payload = public_holding_review_payload(outcome)

    assert payload["interpretation"]["review_disposition"] == "abstain"
    assert payload["missing_snapshot_codes"] == ["brief_snapshot_missing"]
    assert payload["official_negative_check_complete"] is False
    assert payload["sell_timing"] == "insufficient_data"
    assert payload["action_review_source_sufficiency"] == "insufficient_data"
    assert payload["review_boundary"]["action_authorized"] is False
