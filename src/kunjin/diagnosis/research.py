from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Mapping

from kunjin.diagnosis.models import DiagnosisCoverage, PortfolioDiagnosis


def _value(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    return value


def _coverage(value: DiagnosisCoverage) -> dict[str, object]:
    return {
        "evidence_state": value.evidence_state,
        "included_fund_codes": list(value.included_fund_codes),
        "known_weight": _value(value.known_weight),
        "omitted_fund_codes": list(value.omitted_fund_codes),
        "scope": value.scope,
        "unknown_fields": list(value.unknown_fields),
    }


def public_diagnosis_payload(result: PortfolioDiagnosis) -> dict[str, object]:
    result.validate()
    relationships = [
        {
            "evidence_state": item.evidence_state,
            "fund_codes": list(item.fund_codes),
            "metrics": {key: _value(value) for key, value in item.metrics},
            "publication_times": [_value(value) for value in item.publication_times],
            "relationship_id": item.relationship_id,
            "relationship_type": item.relationship_type,
            "report_periods": [_value(value) for value in item.report_periods],
            "warnings": list(item.warnings),
        }
        for item in result.relationships
    ]
    candidate = (
        None
        if result.candidate_impact is None
        else {
            "disclosed_weight": _value(result.candidate_impact.disclosed_weight),
            "fund_code": result.candidate_impact.fund_code,
            "label": result.candidate_impact.label,
            "observed_overlap": _value(result.candidate_impact.observed_overlap),
            "relationship_ids": list(result.candidate_impact.relationship_ids),
            "unknown_fields": list(result.candidate_impact.unknown_fields),
        }
    )
    findings = [
        {
            "evidence_scope": item.evidence_scope,
            "finding_id": item.finding_id,
            "finding_type": item.finding_type,
            "fund_codes": list(item.fund_codes),
            "relationship_ids": list(item.relationship_ids),
            "severity": item.severity,
        }
        for item in result.findings
    ]
    return {
        "action_boundary": {
            "action_authorized": result.action_authorized,
            "action_maturity": result.action_maturity,
            "exact_amount_available": result.exact_amount_available,
        },
        "as_of": result.as_of.isoformat(),
        "beginner_explanation_zh": {
            "coverage": "所有重复结论只覆盖已披露且通过验证的数据，未知部分没有按零处理。",
            "meaning": "结果描述当前组合中观察到的集中与重复，不代表实时完整持仓。",
            "action_boundary": "这份诊断不能授权买入、加仓、持有、减仓、卖出或精确金额。",
        },
        "candidate_impact": candidate,
        "concentration": {
            "hhi": _value(result.hhi),
            "largest_position_share": _value(result.largest_position_share),
            "position_count": result.position_count,
            "value_basis": result.value_basis,
        },
        "conflicts": list(result.conflicts),
        "coverage": {
            "holdings": _coverage(result.holdings_coverage),
            "relationship": _coverage(result.relationship_coverage),
        },
        "findings": findings,
        "input_fingerprint": result.input_fingerprint,
        "missing_evidence": list(result.missing_evidence),
        "relationships": relationships,
        "warnings": list(result.warnings),
    }
