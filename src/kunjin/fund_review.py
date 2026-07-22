from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from urllib.parse import urlparse

_THEME_MARKET_TERMS = (
    "人工智能",
    "ai",
    "算力",
    "芯片",
    "半导体",
    "大模型",
    "科技成长",
    "科创",
)


def build_fund_review(
    *,
    fund_code: str,
    action: str,
    brief: Mapping[str, object],
    intelligence: Mapping[str, object],
    market_scan: Mapping[str, object],
    portfolio: Mapping[str, object] | None,
    horizon: str | None,
    risk_tolerance: str | None,
    near_term_use: str | None,
    guardrails: Mapping[str, object] | None = None,
    portfolio_weight_context: Mapping[str, object] | None = None,
    related_fund_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    missing = [
        label
        for label, value in (
            ("持有期限", horizon),
            ("风险承受程度", risk_tolerance),
            ("近期是否可能使用这笔钱", near_term_use),
        )
        if not value
    ]
    disposition = "需补充信息" if missing else _disposition(action)
    if guardrails is not None and guardrails.get("readiness") != "可以继续研究":
        disposition = "需补充信息"
    market_context = _market_context(intelligence, market_scan, brief)
    return {
        "conclusion": {
            "disposition": disposition,
            "text": "以下是条件性复核，不构成买卖指令、收益承诺或精确金额建议。",
        },
        "fund_code": fund_code,
        "public_facts": {
            "brief": brief,
            "recent_market_facts": market_context.get("facts", []),
            "retrieval": intelligence.get("retrieval", {}),
            "benchmark_evidence": _benchmark_evidence(brief),
        },
        "market_and_industry_context": market_context,
        "portfolio_context": portfolio
        if portfolio is not None
        else {"state": "not_provided", "text": "未提供组合上下文，本次不判断组合集中度或重叠。"},
        "portfolio_weight_context": portfolio_weight_context
        if portfolio_weight_context is not None
        else {"state": "not_requested", "text": "本次未请求当前组合权重上下文。"},
        "related_fund_context": related_fund_context
        if related_fund_context is not None
        else {"state": "not_requested", "text": "本次未指定相关主题基金组。"},
        "constraints": {
            "horizon": horizon,
            "risk_tolerance": risk_tolerance,
            "near_term_use": near_term_use,
            "missing": missing,
        },
        "investor_guardrails": guardrails,
        "risks_and_unknowns": [
            "公开信息和披露可能不完整或滞后。",
            "市场线索不是涨跌原因的完整证明。",
            "条件变化、基金披露更新或个人资金用途变化后应重新复核。",
        ],
        "conditional_guidance": {
            "action_authorized": False,
            "automatic_trade": False,
            "text": "证据足够时仅给出继续研究、观察或人工复核方向。",
        },
    }


def _disposition(action: str) -> str:
    return {
        "continue_holding": "继续持有复核",
        "reduce_to_cash": "减仓观察",
        "full_exit": "退出复核",
    }.get(action, "暂不动作")


def build_portfolio_weight_context(
    *,
    fund_code: str,
    weights: Mapping[str, str],
    value_basis: str,
) -> dict[str, object]:
    weight = weights.get(fund_code)
    if weight is None:
        return {
            "state": "target_not_in_current_portfolio",
            "text": "当前组合观察中未找到该基金，不能说明其组合权重。",
        }
    return {
        "state": "observed",
        "target_fund_code": fund_code,
        "target_portfolio_weight": weight,
        "value_basis": value_basis,
        "text": "权重来自本次组合净值观察，不包含金额，也不代表目标配置。",
    }


def build_related_fund_context(
    *,
    fund_codes: Sequence[str],
    weights: Mapping[str, str],
    comparison: Mapping[str, object],
) -> dict[str, object]:
    codes = tuple(fund_codes)
    present = [code for code in codes if code in weights]
    group_weight = sum((Decimal(weights[code]) for code in present), Decimal("0"))
    coverage = _mapping(comparison.get("coverage"))
    return {
        "state": "observed",
        "fund_codes": list(codes),
        "present_fund_codes": present,
        "group_portfolio_weight": format(group_weight, "f"),
        "group_weight_boundary": (
            "合计仅覆盖本次组合中列出的相关基金；未列入的持仓没有按零处理。"
        ),
        "disclosure_coverage": {
            "members_total": coverage.get("members_total"),
            "members_with_disclosures": coverage.get("members_with_disclosures"),
            "boundary": "披露覆盖只说明比较基金是否有公开披露，不代表完整实时持仓。",
        },
        "industry_disclosed_overlaps": _industry_overlaps(comparison.get("pairwise_overlap")),
        "sources": _disclosure_sources(comparison.get("sources"), codes),
    }


def _market_context(
    intelligence: Mapping[str, object],
    market_scan: Mapping[str, object],
    brief: Mapping[str, object],
) -> dict[str, object]:
    benchmark = _benchmark_evidence(brief)
    facts = _structured_market_facts(
        _source_facts(intelligence.get("what_happened")), benchmark
    )
    if not facts:
        return {
            "state": "insufficient_data",
            "text": "本次未取得足以支持该市场结论的可核验事实。",
            "fund_relationship": "基金关联仍只可依据有日期的披露持仓、基准或指数。",
            "market_scan_state": _market_scan_state(market_scan),
        }
    return {
        "state": "source_backed",
        "facts": facts,
        "fund_relationship": (
            "这些市场事实仅作为与基金基准或已披露方向的研究线索，不证明因果、"
            "未来收益或买卖时点。"
            if benchmark
            else "尚未取得可用基准证据，市场事实不能直接归因于该基金。"
        ),
        "benchmark_evidence_available": bool(benchmark),
        "market_scan_state": _market_scan_state(market_scan),
    }


def _benchmark_evidence(brief: Mapping[str, object]) -> list[dict[str, object]]:
    facts = brief.get("facts")
    if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
        return []
    values = []
    for fact in facts:
        if not isinstance(fact, Mapping) or fact.get("field_id") != "current_benchmark":
            continue
        values.append(
            {
                "value": fact.get("value"),
                "source_name": fact.get("publisher"),
                "url": fact.get("canonical_url"),
                "published_at": fact.get("published_at"),
                "retrieved_at": fact.get("retrieved_at"),
                "source_tier": fact.get("source_tier"),
            }
        )
    return values


def _source_facts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    facts = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        title = item.get("title")
        excerpt = item.get("excerpt")
        topic_text = " ".join(
            item for item in (title, excerpt) if isinstance(item, str)
        ).casefold()
        if not any(term in topic_text for term in _THEME_MARKET_TERMS):
            continue
        source = item.get("source")
        if not isinstance(source, Mapping) or not source.get("url"):
            continue
        facts.append(
            {
                "title": title,
                "excerpt": excerpt,
                "source": dict(source),
            }
        )
    return facts


def _structured_market_facts(
    values: Sequence[Mapping[str, object]],
    benchmark: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    facts = []
    seen_events = set()
    benchmark_description = _benchmark_description(benchmark)
    for item in values:
        title = item.get("title")
        excerpt = item.get("excerpt")
        source = item.get("source")
        if not isinstance(title, str) or not isinstance(excerpt, str):
            continue
        if not isinstance(source, Mapping):
            continue
        event_key = (" ".join(title.casefold().split()), source.get("published_at"))
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        source_projection = _market_source(source)
        if source_projection is None:
            continue
        matched_terms = [term for term in _THEME_MARKET_TERMS if term in (
            f"{title} {excerpt}".casefold()
        )]
        facts.append(
            {
                "what_happened": excerpt,
                "title": title,
                "source_kind": "media_report",
                "source": source_projection,
                "statistics_period": _statistics_period(f"{title} {excerpt}"),
                "why_it_may_relate": _market_relationship(
                    benchmark_description, matched_terms
                ),
                "relation_boundary": (
                    "这是媒体报道中的公开信息，不是指数公司、基金管理人或监管机构对"
                    "本基金表现的证明；相关性不等于因果或涨跌预测。"
                ),
            }
        )
    return facts


def _market_source(source: Mapping[str, object]) -> dict[str, object] | None:
    url = source.get("url")
    if not isinstance(url, str) or not url:
        return None
    host = (urlparse(url).hostname or "").casefold()
    if host.endswith("stcn.com"):
        source_name = "证券时报网/公开媒体（待核验）"
    elif host.endswith("eastmoney.com"):
        source_name = "东方财富/公开平台"
    else:
        source_name = f"{host or '公开媒体'}（待核验）"
    return {
        "source_name": source_name,
        "url": url,
        "published_at": source.get("published_at"),
        "retrieved_at": source.get("retrieved_at"),
        "source_tier": source.get("source_tier"),
    }


def _benchmark_description(values: Sequence[Mapping[str, object]]) -> str | None:
    for item in values:
        value = item.get("value")
        if isinstance(value, Mapping) and isinstance(value.get("description"), str):
            return value["description"]
    return None


def _market_relationship(
    benchmark_description: str | None, matched_terms: Sequence[str]
) -> str:
    topic = "、".join(matched_terms)
    if benchmark_description is None:
        return "尚未取得可核验的基金基准，不能把该媒体事实直接归因于本基金。"
    return (
        f"基金已核验基准为“{benchmark_description}”；媒体摘要明确提及{topic}，"
        "因此可作为同主题公开研究线索。"
    )


def _statistics_period(value: str) -> str | None:
    if "2026年二季度" in value:
        return "2026年二季度（来源摘要）"
    if "二季度" in value:
        return "二季度（来源摘要未标明年份）"
    if "2026年" in value:
        return "2026年（来源摘要）"
    return None


def _market_scan_state(value: Mapping[str, object]) -> str:
    directions = value.get("directions")
    if not isinstance(directions, Sequence) or isinstance(directions, (str, bytes)):
        return "not_available"
    return "source_backed" if any(
        isinstance(item, Mapping) and item.get("evidence_state") == "observed"
        for item in directions
    ) else "insufficient_data"


def _industry_overlaps(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    overlaps = []
    for pair in value:
        if not isinstance(pair, Mapping):
            continue
        industry = pair.get("industry")
        if not isinstance(industry, Mapping) or industry.get("overlap") is None:
            continue
        overlaps.append(
            {
                "left_fund_code": industry.get("left_fund_code"),
                "right_fund_code": industry.get("right_fund_code"),
                "overlap": industry.get("overlap"),
                "left_report_period": industry.get("left_report_period"),
                "right_report_period": industry.get("right_report_period"),
                "left_published_at": industry.get("left_published_at"),
                "right_published_at": industry.get("right_published_at"),
                "explanation": (
                    "这是同一报告期、同一行业分类下共同类别的较小权重之和；"
                    "不等于相同百分比的底层股票，也不代表实时完整持仓。"
                ),
            }
        )
    return overlaps


def _disclosure_sources(value: object, codes: Sequence[str]) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    code_set = set(codes)
    sources = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if item.get("fund_code") not in code_set or item.get("document_kind") not in {
            "quarterly_holdings",
            "industry_exposure",
        }:
            continue
        sources.append(
            {
                key: item.get(key)
                for key in (
                    "fund_code",
                    "document_kind",
                    "source_name",
                    "url",
                    "published_at",
                    "retrieved_at",
                    "source_tier",
                )
            }
        )
    return sources


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
