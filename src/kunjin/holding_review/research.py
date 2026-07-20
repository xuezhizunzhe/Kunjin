from __future__ import annotations

from typing import Any

from kunjin.holding_review.models import (
    EvidenceReadiness,
    HoldingReviewOutcome,
    ReviewDisposition,
    TransientHoldingReviewOutcome,
)

_HEADLINES = {
    ReviewDisposition.CONTINUE_OBSERVING: (
        "本次有界检查未发现需要升级复核的候选；不能据此排除重大风险。"
    ),
    ReviewDisposition.MANUAL_THESIS_REVIEW_REQUIRED: (
        "发现需要你判断语义的证据，系统尚不能形成减仓或退出倾向。"
    ),
    ReviewDisposition.REDUCE_REVIEW: (
        "当前证据支持进入部分减仓复核；这不是今天执行赎回的指令。"
    ),
    ReviewDisposition.EXIT_REVIEW: (
        "当前证据支持进入全部退出复核；这不是今天立即卖出的指令。"
    ),
    ReviewDisposition.ABSTAIN: (
        "关键证据不足或不一致，当前不能形成持有、减仓或退出倾向。"
    ),
}


def public_holding_review_payload(
    outcome: HoldingReviewOutcome | TransientHoldingReviewOutcome,
) -> dict[str, Any]:
    """Project an authenticated held-review outcome into a non-authorizing view."""
    if type(outcome) not in {HoldingReviewOutcome, TransientHoldingReviewOutcome}:
        raise ValueError("holding review outcome must be exact")
    outcome.validate()
    if type(outcome) is TransientHoldingReviewOutcome:
        gap_codes = tuple(
            sorted(
                {
                    *outcome.missing_snapshot_codes,
                    "insufficient_data",
                    "official_confirmation_required",
                }
            )
        )
        return {
            "flow_status": outcome.flow_status.value,
            "fund_code": None,
            "action": None,
            "facts": {"evidence_ids": [], "official_event_evidence": []},
            "interpretation": {
                "headline": _HEADLINES[ReviewDisposition.ABSTAIN],
                "review_disposition": ReviewDisposition.ABSTAIN.value,
                "thesis_review_state": None,
                "triggered_reviews": [],
            },
            "dates": {"review_created_at": None, "snapshot_created_at": None},
            "evidence_delta": None,
            "candidate_thesis_match": None,
            "owner_adjudication": None,
            "missing_snapshot_codes": list(outcome.missing_snapshot_codes),
            "gap_codes": list(gap_codes),
            "redemption": None,
            "official_negative_check_complete": False,
            "intelligence_schedule_complete": False,
            "action_review_source_sufficiency": "insufficient_data",
            "upstream_action_boundary": ["research_only"],
            "sell_timing": "insufficient_data",
            "review_boundary": outcome.boundary.to_canonical_dict(),
        }

    snapshot = outcome.review_snapshot
    result = snapshot.result
    gaps = set(result.omitted_work)
    gaps.update(result.intelligence_omitted_work)
    if not result.official_negative_check_complete:
        gaps.add("official_confirmation_required")
    if result.evidence_readiness is not EvidenceReadiness.READY:
        gaps.add("insufficient_data")
    return {
        "flow_status": outcome.flow_status.value,
        "fund_code": result.fund_code,
        "action": result.action.value,
        "facts": {
            "evidence_ids": list(result.evidence_ids),
            "official_event_evidence": [
                item.to_canonical_dict() for item in result.official_event_evidence
            ],
        },
        "interpretation": {
            "headline": _HEADLINES[result.review_disposition],
            "review_disposition": result.review_disposition.value,
            "thesis_review_state": result.thesis_review_state.value,
            "triggered_reviews": [item.value for item in result.triggered_reviews],
        },
        "dates": {
            "review_created_at": result.created_at.isoformat(),
            "snapshot_created_at": snapshot.created_at.isoformat(),
        },
        "evidence_readiness": result.evidence_readiness.value,
        "evidence_delta": result.evidence_delta.to_canonical_dict(),
        "candidate_thesis_match": {
            "projection_id": snapshot.thesis_match_projection_id,
            "projection_checksum": snapshot.thesis_match_projection_checksum,
        },
        "owner_adjudication": {
            "state": snapshot.adjudication_state.value,
            "adjudication_id": snapshot.adjudication_id,
            "adjudication_checksum": snapshot.adjudication_checksum,
        },
        "missing_snapshot_codes": [],
        "gap_codes": sorted(gaps),
        "intelligence_omitted_work": list(result.intelligence_omitted_work),
        "intelligence_degraded_sources": list(result.intelligence_degraded_sources),
        "redemption": {
            "feasibility": result.redemption_feasibility.value,
            "evidence": result.redemption_evidence.to_canonical_dict(),
            "remainder_intent": result.remainder_intent.value,
            "exit_reason": result.exit_reason.value,
            "use_of_proceeds": result.use_of_proceeds.value,
        },
        "official_negative_check_complete": result.official_negative_check_complete,
        "intelligence_schedule_complete": result.intelligence_schedule_complete,
        "action_review_source_sufficiency": (
            result.action_review_source_sufficiency.value
        ),
        "upstream_action_boundary": list(result.upstream_action_boundary),
        "sell_timing": result.sell_timing,
        "review_boundary": result.boundary.to_canonical_dict(),
    }
