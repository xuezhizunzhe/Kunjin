from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from kunjin.selection.models import (
    CandidateReview,
    ComparabilityEvidence,
    PersonalGateEvidence,
    ShortlistResult,
)


def _public_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is Decimal:
        return str(value)
    if type(value) in {date, datetime}:
        return value.isoformat()
    if type(value) in {tuple, list}:
        return [_public_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _public_value(item) for key, item in value.items()}
    raise ValueError("shortlist public projection contains an unsupported value")


def public_personal_gate_payload(value: PersonalGateEvidence) -> dict[str, object]:
    value.validate()
    return {
        "allocation_freshness": value.allocation_freshness,
        "allocation_state": value.allocation_state,
        "allocation_status": value.allocation_status,
        "blocking_codes": list(value.blocking_codes),
        "constraint_codes": list(value.constraint_codes),
        "suitability_freshness": value.suitability_freshness,
        "suitability_state": value.suitability_state,
        "suitability_status": value.suitability_status,
    }


def _comparability_payload(
    values: tuple[ComparabilityEvidence, ...],
) -> list[dict[str, object]]:
    return [
        {
            "left_fund_code": item.left_fund_code,
            "reason_code": item.reason_code,
            "right_fund_code": item.right_fund_code,
            "state": item.state,
            "warning_codes": list(item.warning_codes),
        }
        for item in values
    ]


def _metric_payload(values: tuple[tuple[str, object], ...]) -> dict[str, object]:
    return {key: _public_value(value) for key, value in values}


def _candidate_payload(values: tuple[CandidateReview, ...]) -> list[dict[str, object]]:
    return [
        {
            "advantage_codes": list(item.advantage_codes),
            "blocking_codes": list(item.blocking_codes),
            "conflicts": list(item.conflicts),
            "d1_evidence_status": item.d1_evidence_status,
            "evidence_state": item.evidence_state,
            "fund_code": item.fund_code,
            "mapped_asset_layer": item.mapped_asset_layer,
            "missing_evidence": list(item.missing_evidence),
            "portfolio_impact_label": item.portfolio_impact_label,
            "portfolio_impact_state": item.portfolio_impact_state,
            "portfolio_role": item.portfolio_role,
            "position_state": item.position_state,
            "relationship_ids": list(item.relationship_ids),
            "risk_bucket": item.risk_bucket,
            "tradeoff_codes": list(item.tradeoff_codes),
            "warnings": list(item.warnings),
        }
        for item in values
    ]


def _beginner_explanation(result: ShortlistResult) -> dict[str, str]:
    unknown_text = (
        "当前仍有缺失证据，相关维度保持未知，不能把缺失当成没有风险。"
        if result.missing_evidence
        else "当前投影未报告缺失证据，但仍只代表本次本地证据快照。"
    )
    gate_passes = (
        result.personal_gate.suitability_state == "fresh"
        and result.personal_gate.suitability_freshness == "fresh"
        and result.personal_gate.suitability_status
        in {"constrained", "ready_for_allocation"}
        and not result.personal_gate.blocking_codes
        and result.personal_gate.allocation_state == "fresh"
        and result.personal_gate.allocation_freshness == "fresh"
        and result.personal_gate.allocation_status == "range_available"
    )
    gate_text = (
        "个人闸门只用于限制结论，不用于给候选基金排序。"
        if gate_passes
        else "个人适当性或资产配置闸门存在限制，当前结果不能升级为个人候选清单。"
    )
    return {
        "change_conditions": (
            "持仓、基金证据、适当性或资产配置状态变化后，应重新运行比较；"
            "本阶段不提供卖出时机。"
        ),
        "observed_facts": (
            f"本次只比较你输入的 {len(result.candidate_codes)} 只基金，"
            "展示结果来自当前本地证据快照。"
        ),
        "personal_gate_limits": gate_text,
        "reasoned_comparisons": (
            "优势和取舍是基于可比维度的条件式解释，不使用总分，也不选唯一优胜者；"
            "条件候选清单不是买入信号，不提供精确金额或交易授权。"
        ),
        "unknown_coverage": unknown_text,
    }


def public_shortlist_payload(result: ShortlistResult) -> dict[str, object]:
    result.validate()
    return {
        "action_boundary": {
            "action_authorized": result.action_authorized,
            "action_maturity": result.action_maturity,
            "automatic_trade": result.automatic_trade,
            "exact_amount_available": result.exact_amount_available,
        },
        "as_of": result.as_of.isoformat(),
        "beginner_explanation_zh": _beginner_explanation(result),
        "candidate_reviews": _candidate_payload(result.candidate_reviews),
        "comparability": _comparability_payload(result.comparability),
        "comparison_state": result.comparison_state,
        "conditional_shortlist": {
            "fund_codes": list(result.shortlist_codes),
            "invalidation_conditions": list(result.invalidation_conditions),
            "merit_ordered": False,
        },
        "conflicts": list(result.conflicts),
        "input_fingerprint": result.input_fingerprint,
        "metric_comparisons": _metric_payload(result.metric_comparisons),
        "missing_evidence": list(result.missing_evidence),
        "personal_gate": public_personal_gate_payload(result.personal_gate),
        "request": {
            "candidate_codes": list(result.candidate_codes),
            "candidate_count": len(result.candidate_codes),
        },
        "warnings": list(result.warnings),
    }


__all__ = ["public_personal_gate_payload", "public_shortlist_payload"]
