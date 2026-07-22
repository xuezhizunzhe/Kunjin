from __future__ import annotations

from collections.abc import Mapping


def build_investor_guardrails(
    *,
    emergency_fund: str | None,
    near_term_use: str | None,
    horizon: str | None,
    volatility: str | None,
    portfolio: Mapping[str, object] | None,
) -> dict[str, object]:
    missing = [
        label
        for label, value in (
            ("应急资金情况", emergency_fund),
            ("近期资金用途", near_term_use),
            ("预计持有期限", horizon),
            ("可承受波动程度", volatility),
        )
        if value is None or value == "unknown"
    ]
    if emergency_fund == "no" or near_term_use == "yes":
        readiness = "先降低风险"
        boundary = _conservative_boundary()
    elif missing:
        readiness = "需补充信息"
        boundary = _conservative_boundary()
    else:
        readiness = "可以继续研究"
        boundary = _boundary(horizon, volatility)
    concentration = _concentration_note(portfolio)
    return {
        "readiness": readiness,
        "risk_profile": _risk_label(volatility),
        "allocation_boundary": boundary,
        "portfolio_context": concentration,
        "portfolio_research": _portfolio_research_summary(readiness, concentration),
        "research_candidates": _candidates(readiness, volatility),
        "avoid_or_defer": _avoid(readiness, concentration),
        "missing_information": missing,
        "risks_and_uncertainty": [
            "配置边界是类别和比例区间，不是交易金额或保证收益。",
            "行业主题基金波动与集中风险通常高于宽基或债券类研究候选。",
        ],
        "action_boundary": {
            "automatic_trade": False,
            "exact_amount_available": False,
            "text": "仅用于研究和人工复核，不构成必须买卖。",
        },
    }


def _conservative_boundary() -> dict[str, str]:
    return {
        "risk_assets": "先不增加或仅在信息补齐后研究",
        "low_volatility_assets": "优先保留流动性与低波动类别研究",
        "theme_assets": "暂不作为新增重点",
    }


def _boundary(horizon: str | None, volatility: str | None) -> dict[str, str]:
    if horizon == "long" and volatility == "high":
        risk_assets = "可研究中等到较高比例的分散权益类别"
    elif volatility == "low" or horizon == "short":
        risk_assets = "以较低比例的风险资产研究为边界"
    else:
        risk_assets = "可研究中等比例的分散权益类别"
    return {
        "risk_assets": risk_assets,
        "low_volatility_assets": "保留与近期用途相匹配的低波动和流动性类别",
        "theme_assets": "行业主题只宜作为小比例研究候选，先核对集中度",
    }


def _risk_label(volatility: str | None) -> str:
    return {"low": "低波动偏好", "medium": "中等波动偏好", "high": "较高波动承受"}.get(
        volatility, "尚未确认"
    )


def _concentration_note(portfolio: Mapping[str, object] | None) -> dict[str, object]:
    if portfolio is None:
        return {"state": "not_provided", "text": "未提供组合，无法判断重复或集中。"}
    overview = portfolio.get("portfolio_overview")
    exposures = portfolio.get("observed_exposures")
    coverage = portfolio.get("coverage")
    missing_evidence = portfolio.get("missing_evidence")
    return {
        "state": "provided",
        "overview": overview,
        "exposures": exposures,
        "coverage": coverage,
        "missing_evidence": missing_evidence,
        "text": "组合结论仅覆盖已同步或临时提供的持仓和带日期披露。",
    }


def _portfolio_research_summary(
    readiness: str, concentration: Mapping[str, object]
) -> dict[str, object]:
    if concentration.get("state") != "provided":
        return {
            "state": "portfolio_unavailable",
            "confirmed_observations": [],
            "unknown_boundaries": ["尚未提供组合，无法判断当前重复、集中或配置缺口。"],
            "priority_categories": [],
            "text": "先提供已同步组合或临时比例后，再讨论组合角色。",
        }

    overview = _mapping(concentration.get("overview"))
    relationships = _mappings(concentration.get("exposures"))
    coverage = _mapping(concentration.get("coverage"))
    observations = []
    count = overview.get("position_count")
    largest = overview.get("largest_position_share")
    if isinstance(count, int) and count > 0:
        observations.append(f"当前缓存中有{count}只非零持仓。")
    if isinstance(largest, str):
        observations.append("已观察到最大单只持仓占比，需结合个人波动承受继续复核。")
    for relationship in relationships:
        if relationship.get("relationship_type") != "same_company":
            continue
        company = _mapping(relationship.get("metrics")).get("company_name")
        if isinstance(company, str) and company:
            observations.append(f"已观察到同一基金公司关系：{company}。")

    unknown = [
        "季度持仓只是带日期披露快照，不能代表实时完整持仓。",
        "未知披露、身份或基准不能按零处理。",
    ]
    holdings = _mapping(coverage.get("holdings"))
    if holdings.get("evidence_state") != "ready":
        unknown.append("当前完整股票与行业重叠认证覆盖不足，不能得出精确全组合重叠率。")
    if _strings(concentration.get("missing_evidence")):
        unknown.append("部分基金的身份、基准或披露资料仍需补充。")

    priorities = [
        "先研究与现有持仓驱动不同的分散宽基角色，不指定具体基金。",
        "研究低波动或高质量固定收益类别如何配合近期用途和流动性边界。",
    ]
    if readiness != "可以继续研究":
        priorities = ["先补齐个人信息或降低风险，再讨论新增风险类别。"]
    else:
        priorities.insert(0, "先核对当前主题与披露重叠，再研究新的行业主题角色。")
    return {
        "state": "research_ready" if readiness == "可以继续研究" else "profile_limited",
        "confirmed_observations": observations,
        "unknown_boundaries": unknown,
        "priority_categories": priorities,
        "text": "这些是研究类别与组合角色，不是具体基金推荐、交易指令或金额建议。",
    }


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _candidates(readiness: str, volatility: str | None) -> list[str]:
    if readiness != "可以继续研究":
        return ["先补齐应急资金、期限或风险信息，再研究宽基或债券类候选。"]
    values = ["分散宽基基金可作为优先研究候选", "债券类基金可用于低波动类别研究"]
    if volatility == "high":
        values.append("行业主题基金可在集中度可控时作为小比例研究候选")
    return values


def _avoid(readiness: str, concentration: Mapping[str, object]) -> list[str]:
    values = []
    if readiness != "可以继续研究":
        values.append("在信息不足、近期可能用钱或应急资金不足时，不宜增加风险资产")
    if concentration.get("state") == "provided":
        values.append("已观察到的重复主题或披露重叠未核对前，不宜继续叠加同类主题")
    return values
