from __future__ import annotations

from collections.abc import Mapping, Sequence

from kunjin.public_research.summary import summarize_public_research

_DOMAINS = (
    ("power_energy", "电力与能源", ("电力", "储能", "风电", "光伏", "新能源")),
    ("coal_oil_gas", "煤炭与油气", ("煤炭", "油气", "石油", "天然气")),
    ("real_estate_materials", "房地产与建材", ("房地产", "建材", "水泥", "装修")),
    (
        "industrial_commodities",
        "工业品与大宗商品",
        ("工业品", "大宗商品", "钢铁", "有色", "铜", "铝", "化工"),
    ),
    ("autos", "汽车", ("汽车", "智能驾驶", "汽车零部件")),
    ("shipping_trade", "航运、港口与外贸", ("航运", "港口", "物流", "外贸")),
    ("ai_compute", "AI 与算力", ("人工智能", "算力", "软件", "半导体")),
    ("consumer", "消费", ("消费", "食品", "饮料", "零售", "旅游")),
    ("policy", "政策", ("政策", "国务院", "监管", "财政", "货币")),
    ("weather", "天气", ("天气", "高温", "降雨", "台风", "干旱")),
)
_STATE_PRIORITY = {
    "overheating_risk": 4,
    "improving": 3,
    "weakening": 2,
    "neutral": 1,
    "insufficient_data": 0,
}
_SIGNALS = {
    "overheating_risk": "需要谨慎",
    "improving": "值得继续研究",
    "weakening": "继续观察",
    "neutral": "继续观察",
    "insufficient_data": "证据不足",
}


def scan_public_research(payload: Mapping[str, object]) -> dict[str, object]:
    """Discover bounded cross-domain research directions from public data."""

    summary = summarize_public_research(payload)
    shadow = payload.get("experimental_shadow")
    if not isinstance(shadow, Mapping):
        raise ValueError("public research payload experimental_shadow is invalid")
    sector_states = _sector_states(shadow.get("sector_states"))
    facts = summary["what_happened"]
    if not isinstance(facts, Sequence):
        raise ValueError("public research summary facts are invalid")
    directions = [_direction(domain, sector_states, facts) for domain in _DOMAINS]
    gaps = list(summary["risks_and_unknowns"]["evidence_gaps"])
    gaps.extend(
        f"cross_domain_{item['domain_id']}_not_covered"
        for item in directions
        if item["evidence_state"] == "insufficient_data"
    )
    return {
        "conclusion": summary["conclusion"],
        "timeline": facts,
        "directions": directions,
        "conditional_guidance": summary["conditional_guidance"],
        "risks_and_unknowns": {
            **summary["risks_and_unknowns"],
            "evidence_gaps": sorted(set(gaps)),
        },
        "sources": summary["sources"],
        "retrieval": summary["retrieval"],
        "automatic_industry_data": {
            "state": "network_blocked",
            "text": (
                "当前网络环境未形成稳定自动行业来源；电力、汽车、房地产建材和航运外贸"
                "需使用用户补充的公开材料。"
            ),
        },
    }


def _sector_states(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("public research payload sector_states is invalid")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("public research payload sector state is invalid")
        name = item.get("sector_name")
        state = item.get("state")
        if not isinstance(name, str) or not name or state not in _STATE_PRIORITY:
            raise ValueError("public research payload sector state is invalid")
        result.append({"sector_name": name, "state": state})
    return result


def _direction(
    domain: tuple[str, str, tuple[str, ...]],
    sector_states: Sequence[Mapping[str, str]],
    facts: Sequence[object],
) -> dict[str, object]:
    domain_id, domain_name, keywords = domain
    matched_sectors = [
        item for item in sector_states if _matches(item["sector_name"], keywords)
    ]
    matched_facts = [
        item["title"]
        for item in facts
        if isinstance(item, Mapping)
        and isinstance(item.get("title"), str)
        and _matches(item["title"], keywords)
    ]
    if not matched_sectors and not matched_facts:
        return {
            "label": "系统分析",
            "domain_id": domain_id,
            "domain_name": domain_name,
            "evidence_state": "insufficient_data",
            "matched_sectors": [],
            "matched_facts": [],
            "signal": "证据不足",
            "caveat": "本次公开扫描没有形成该方向的可核验覆盖。",
        }
    state = max(
        (item["state"] for item in matched_sectors),
        key=lambda value: _STATE_PRIORITY[value],
        default="insufficient_data",
    )
    return {
        "label": "系统分析",
        "domain_id": domain_id,
        "domain_name": domain_name,
        "evidence_state": "observed",
        "matched_sectors": sorted(item["sector_name"] for item in matched_sectors),
        "matched_facts": sorted(set(matched_facts)),
        "signal": _SIGNALS[state],
        "caveat": "板块状态是公开市场观察，不代表未来收益、买卖时点或完整行业覆盖。",
    }


def _matches(value: str, keywords: Sequence[str]) -> bool:
    return any(keyword in value for keyword in keywords)
