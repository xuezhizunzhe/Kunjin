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


def scan_public_research(
    payload: Mapping[str, object],
    *,
    local_overview: Mapping[str, object] | None = None,
    include_intelligence: bool = True,
) -> dict[str, object]:
    """Discover bounded cross-domain research directions from public data."""

    summary = summarize_public_research(payload)
    shadow = payload.get("experimental_shadow")
    if not isinstance(shadow, Mapping):
        raise ValueError("public research payload experimental_shadow is invalid")
    sector_states = _sector_states(shadow.get("sector_states"))
    intelligence_facts = summary["what_happened"] if include_intelligence else []
    if not isinstance(intelligence_facts, Sequence):
        raise ValueError("public research summary facts are invalid")
    local_facts = _local_facts(local_overview)
    facts = [*intelligence_facts, *local_facts]
    directions = [_direction(domain, sector_states, facts) for domain in _DOMAINS]
    gaps = list(summary["risks_and_unknowns"]["evidence_gaps"])
    gaps.extend(
        f"cross_domain_{item['domain_id']}_not_covered"
        for item in directions
        if item["evidence_state"] == "insufficient_data"
    )
    return {
        "conclusion": _conclusion(summary["conclusion"], local_facts),
        "timeline": facts,
        "directions": directions,
        "candidate_directions": _candidate_directions(directions),
        "conditional_guidance": summary["conditional_guidance"],
        "risks_and_unknowns": {
            **summary["risks_and_unknowns"],
            "evidence_gaps": sorted(set(gaps)),
        },
        "sources": [
            *(summary["sources"] if include_intelligence else []),
            *_local_sources(local_facts),
        ],
        "retrieval": summary["retrieval"],
        "local_overview": local_overview,
        "outer_discovery": _outer_discovery(local_overview),
        "automatic_industry_data": _automatic_industry_data(local_overview),
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
    matched_facts = []
    for item in facts:
        if not isinstance(item, Mapping) or not isinstance(item.get("title"), str):
            continue
        persisted_domain = item.get("domain_id")
        if persisted_domain is not None:
            if persisted_domain == domain_id:
                matched_facts.append(item["title"])
        elif _matches(item["title"], keywords):
            matched_facts.append(item["title"])
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


def _conclusion(value: object, local_facts: Sequence[Mapping[str, object]]) -> object:
    if local_facts:
        return {
            "state": "evidence_backed_research",
            "text": "已合并本地持久化的可追溯公开事实，仍需刷新近期外部信息后再判断。",
        }
    return value


def _candidate_directions(directions: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    observed = [item for item in directions if item.get("evidence_state") == "observed"]
    return sorted(
        (dict(item) for item in observed),
        key=lambda item: (-len(item["matched_facts"]), str(item["domain_id"])),
    )[:3]


def _local_facts(local_overview: Mapping[str, object] | None) -> list[dict[str, object]]:
    if local_overview is None:
        return []
    domains = local_overview.get("domains")
    if not isinstance(domains, Sequence) or isinstance(domains, (str, bytes)):
        raise ValueError("local research overview domains are invalid")
    facts = []
    for domain in domains:
        if not isinstance(domain, Mapping):
            raise ValueError("local research overview domain is invalid")
        domain_name = domain.get("domain_name")
        indicators = domain.get("indicators")
        events = domain.get("events")
        if (
            not isinstance(domain_name, str)
            or not isinstance(indicators, Sequence)
            or isinstance(indicators, (str, bytes))
            or not isinstance(events, Sequence)
            or isinstance(events, (str, bytes))
        ):
            raise ValueError("local research overview domain is invalid")
        for indicator in indicators:
            if not isinstance(indicator, Mapping):
                raise ValueError("local research overview indicator is invalid")
            observation = indicator.get("latest_observation")
            if not isinstance(observation, Mapping):
                raise ValueError("local research overview observation is invalid")
            name = indicator.get("indicator_name")
            unit = indicator.get("unit")
            period = observation.get("statistics_period")
            value = observation.get("value")
            source = observation.get("source")
            fields_are_text = all(isinstance(item, str) for item in (name, unit, period, value))
            if not fields_are_text or not isinstance(source, Mapping):
                raise ValueError("local research overview observation is invalid")
            facts.append(
                {
                    "label": "持久化公开事实",
                    "title": f"{domain_name}：{name}（{period}）",
                    "excerpt": f"{name}为{value}{unit}，统计期为{period}。",
                    "source": dict(source),
                    "statistics_period": period,
                    "origin": "persisted_indicator",
                    "domain_id": domain.get("domain_id"),
                }
            )
        for event in events:
            if not isinstance(event, Mapping):
                raise ValueError("local research overview event is invalid")
            summaries = event.get("fact_summaries")
            sources = event.get("sources")
            if not isinstance(summaries, Sequence) or not isinstance(sources, Sequence):
                raise ValueError("local research overview event is invalid")
            fact_source = next(
                (
                    source
                    for source in sources
                    if isinstance(source, Mapping) and source.get("evidence_state") == "fact"
                ),
                None,
            )
            if not isinstance(fact_source, Mapping):
                continue
            for summary in summaries:
                if not isinstance(summary, str):
                    raise ValueError("local research overview event summary is invalid")
                facts.append(
                    {
                        "label": "持久化市场事实",
                        "title": f"{domain_name}：近期市场事件",
                        "excerpt": summary,
                        "source": {
                            "source_name": fact_source.get("publisher"),
                            "url": fact_source.get("url"),
                            "source_tier": fact_source.get("source_tier"),
                            "published_at": fact_source.get("published_at"),
                            "retrieved_at": fact_source.get("retrieved_at"),
                        },
                        "statistics_period": None,
                        "origin": "persisted_event_fact",
                        "domain_id": domain.get("domain_id"),
                    }
                )
    return facts


def _local_sources(facts: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    values = []
    seen = set()
    for fact in facts:
        source = fact.get("source")
        if not isinstance(source, Mapping) or not isinstance(source.get("url"), str):
            raise ValueError("local research fact source is invalid")
        url = source["url"]
        if url not in seen:
            values.append(dict(source))
            seen.add(url)
    return values


def _outer_discovery(local_overview: Mapping[str, object] | None) -> dict[str, object]:
    if local_overview is not None and isinstance(local_overview.get("outer_discovery"), Mapping):
        return dict(local_overview["outer_discovery"])
    return {
        "outer_discovery_required": True,
        "current_news_refresh_state": "pending",
        "text": "本次尚未记录外层互联网发现；最终回答前必须刷新或明确环境阻塞。",
    }


def _automatic_industry_data(local_overview: Mapping[str, object] | None) -> dict[str, str]:
    if local_overview is not None:
        coverage = local_overview.get("coverage")
        if isinstance(coverage, Mapping) and coverage.get("persisted_indicator_record_count"):
            return {
                "state": "persisted_history_available",
                "text": "已发现本地持久化的行业历史事实；近期外部新闻与行情仍待本次受控刷新。",
            }
    return {
        "state": "network_refresh_needed",
        "text": "本地未形成可复用的行业历史事实；需要外层进行一次受控公开发现。",
    }


def _matches(value: str, keywords: Sequence[str]) -> bool:
    return any(keyword in value for keyword in keywords)
