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
            "field_lineage": _field_lineage(comparison, code, sources[code]),
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
                "需确认" if comparability_warnings else "未发现本次明确输入基金之间的类别不匹配提示"
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


def _field_lineage(
    comparison: Mapping[str, object], code: str, sources: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    """Attach each candidate-facing fact to its source or explicitly retain the gap."""

    source_by_id = {item.get("id"): item for item in sources if isinstance(item.get("id"), int)}
    disclosures = _mapping(_mapping(comparison.get("candidate_disclosures")).get(code))
    managers = _mapping(comparison.get("managers")).get(code, [])
    manager_ids = _source_ids(managers)
    benchmarks = _mapping(disclosures.get("benchmarks"))
    benchmark_ids = _source_ids(benchmarks.get("items"))
    fees = _mapping(comparison.get("fees")).get(code, [])
    fee_ids = _source_ids(fees)
    quarterly = _mapping(disclosures.get("quarterly_holdings"))
    industry = _mapping(disclosures.get("industry_exposure"))
    metrics = _nav_metrics(comparison.get("windows"), code)
    common_nav_end = _mapping(comparison.get("data_dates")).get("common_nav_end")
    selection = _mapping(quarterly.get("selection"))
    return {
        "managers": _source_backed_field(managers, manager_ids, source_by_id),
        "benchmark": _source_backed_field(benchmarks.get("items", []), benchmark_ids, source_by_id),
        "fees": _source_backed_field(fees, fee_ids, source_by_id),
        "quarterly_holdings": {
            **quarterly,
            "sources": _sources_for_ids(quarterly.get("source_document_ids"), source_by_id),
            "usage_boundary": (
                "报告期绑定未核验时，Top10 仅供人工观察，不得用于确定性证券重叠。"
                if selection.get("report_period_binding") == "unresolved"
                else "仅代表带日期的已披露范围，不代表实时完整持仓。"
            ),
        },
        "industry_exposure": {
            **industry,
            "sources": _sources_for_ids(industry.get("source_document_ids"), source_by_id),
            "usage_boundary": "行业类别交集不等于底层股票重叠，也不代表实时完整持仓。",
        },
        "formal_nav_metrics": {
            "state": "source_lineage_unavailable",
            "as_of": common_nav_end,
            "metrics": metrics,
            "data_gap": (
                "本地正式净值计算保留了区间和截至日，但当前比较输出未保存可逐项引用的公开 URL；"
                "中文回答不得为这些指标补造来源。"
            ),
        },
    }


def _source_backed_field(
    values: object, source_ids: Sequence[int], source_by_id: Mapping[int, Mapping[str, object]]
) -> dict[str, object]:
    records = (
        list(values)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
        else []
    )
    sources = _sources_for_ids(source_ids, source_by_id)
    return {
        "state": "source_backed" if records and sources else "source_lineage_unavailable",
        "items": records,
        "sources": sources,
        "data_gap": None if records and sources else "该字段缺少可逐项引用的来源或日期。",
    }


def _source_ids(values: object) -> list[int]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return sorted(
        {
            item.get("source_document_id")
            for item in values
            if isinstance(item, Mapping) and isinstance(item.get("source_document_id"), int)
        }
    )


def _sources_for_ids(
    source_ids: object, source_by_id: Mapping[int, Mapping[str, object]]
) -> list[dict[str, object]]:
    if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)):
        return []
    return [dict(source_by_id[item]) for item in source_ids if item in source_by_id]


def _nav_metrics(value: object, code: str) -> list[dict[str, object]]:
    if not isinstance(value, Mapping):
        return []
    metrics = []
    for window, rows in value.items():
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            continue
        metrics.extend(
            {"window": window, **dict(item)}
            for item in rows
            if isinstance(item, Mapping) and item.get("fund_code") == code
        )
    return metrics


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


def _candidate_state(disposition: str, has_sources: bool, has_full_disclosures: bool) -> str:
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
                    "id",
                    "document_kind",
                    "source_name",
                    "title",
                    "url",
                    "publisher",
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
