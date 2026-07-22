from __future__ import annotations

from collections.abc import Mapping, Sequence


def build_fund_candidate_review(
    *,
    fund_codes: Sequence[str],
    comparison: Mapping[str, object],
    guardrails: Mapping[str, object],
) -> dict[str, object]:
    """Project an explicit comparison into a beginner-safe research view."""

    codes = tuple(fund_codes)
    coverage = _mapping(comparison.get("coverage"))
    disclosed_count = coverage.get("members_with_disclosures", 0)
    has_full_disclosures = disclosed_count == len(codes)
    warnings = _strings(comparison.get("warnings"))
    comparability_warnings = tuple(
        item for item in warnings if item.startswith("comparability_warning:")
    )
    readiness = guardrails.get("readiness")
    disposition, text = _disposition(
        readiness=readiness,
        has_full_disclosures=has_full_disclosures,
        has_comparability_warnings=bool(comparability_warnings),
    )
    sources = _sources_by_fund(comparison.get("sources"), codes)
    candidates = [
        {
            "fund_code": code,
            "research_state": _candidate_state(
                disposition, bool(sources[code]), has_full_disclosures
            ),
            "public_sources": sources[code],
            "portfolio_overlap": _mapping(
                _mapping(comparison.get("candidate_portfolio_overlap")).get(code)
            ),
        }
        for code in codes
    ]
    return {
        "conclusion": {"disposition": disposition, "text": text},
        "scope": {
            "candidate_fund_codes": list(codes),
            "text": "只比较你明确提供的基金，不自动发现或推荐新的基金代码。",
        },
        "verifiable_facts": {
            "coverage": coverage,
            "metrics": _mapping(comparison.get("metric_orderings")),
            "sources": [source for values in sources.values() for source in values],
        },
        "analysis": {
            "same_category_check": (
                "需确认"
                if comparability_warnings
                else "未发现本次明确输入基金之间的类别不匹配提示"
            ),
            "comparability_warnings": list(comparability_warnings),
            "disclosed_overlap": comparison.get("pairwise_overlap", []),
            "portfolio_context": comparison.get("candidate_portfolio_overlap", {}),
            "text": "历史指标、费用和披露重叠只用于横向研究，不组成总分或唯一优胜者。",
        },
        "candidate_reviews": candidates,
        "investor_guardrails": guardrails,
        "conditional_guidance": {
            "text": _guidance_text(disposition),
            "automatic_trade": False,
            "exact_amount_available": False,
        },
        "risks_and_evidence_gaps": _risks(
            has_full_disclosures, comparability_warnings, comparison.get("data_gaps")
        ),
    }


def _disposition(
    *,
    readiness: object,
    has_full_disclosures: bool,
    has_comparability_warnings: bool,
) -> tuple[str, str]:
    if readiness != "可以继续研究":
        return "需补充个人信息", "先补齐画像或降低风险，再把候选比较用于个人研究。"
    if not has_full_disclosures:
        return "需补充公开资料", "至少一只基金缺少公开披露，暂不形成研究候选。"
    if has_comparability_warnings:
        return "先确认是否同类", "输入基金可能不是同类产品，先核对投资范围或基准。"
    return "可作为研究候选", "可继续比较公开资料与组合重叠，但不是买入建议。"


def _candidate_state(
    disposition: str, has_sources: bool, has_full_disclosures: bool
) -> str:
    if not has_sources or not has_full_disclosures:
        return "待补公开资料"
    if disposition == "可作为研究候选":
        return "可继续研究"
    return "仅作对照"


def _guidance_text(disposition: str) -> str:
    if disposition == "可作为研究候选":
        return "从候选中继续核对跟踪标的、费用、披露更新与组合重叠后再人工复核。"
    return "先补齐本次标出的信息，不以当前比较结果执行买入、加仓或切换。"


def _risks(
    has_full_disclosures: bool,
    comparability_warnings: tuple[str, ...],
    data_gaps: object,
) -> list[str]:
    risks = ["基金披露有日期且可能滞后，不能代表实时完整持仓。"]
    if not has_full_disclosures:
        risks.append("存在公开披露缺口，缺失不能被理解为没有风险或没有重叠。")
    if comparability_warnings:
        risks.append("输入基金可能不属于同类，历史指标不宜直接横向比较。")
    if _strings(data_gaps):
        risks.append("本次比较还有数据缺口，应在资料补齐后重新复核。")
    return risks


def _sources_by_fund(value: object, codes: tuple[str, ...]) -> dict[str, list[dict[str, object]]]:
    result = {code: [] for code in codes}
    if not isinstance(value, list):
        return result
    for source in value:
        if not isinstance(source, Mapping):
            continue
        code = source.get("fund_code")
        if code not in result:
            continue
        result[code].append(
            {
                key: source.get(key)
                for key in (
                    "source_name",
                    "title",
                    "url",
                    "published_at",
                    "retrieved_at",
                    "source_tier",
                )
            }
        )
    return result


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))
