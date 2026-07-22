"""Bounded, non-persistent projection of public evidence supplied by the owner."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import urlparse

_SOURCE_KINDS = frozenset(
    {"official", "platform_data", "industry_data", "media", "community"}
)
_DOMAIN_IDS = frozenset(
    {
        "power_energy",
        "coal_oil_gas",
        "real_estate_materials",
        "industrial_commodities",
        "autos",
        "shipping_trade",
        "ai_compute",
        "consumer",
        "policy",
        "weather",
    }
)
_MAX_TEXT = 1_000
_MONTH_PERIOD = re.compile(r"^(?P<year>20\d{2})年(?P<month>1[0-2]|[1-9])月$")
_QUARTER_PERIOD = re.compile(r"^(?P<year>20\d{2})年第(?P<quarter>[1-4])季度$")


def summarize_user_supplied_evidence(value: Mapping[str, object]) -> dict[str, object]:
    """Project one owner-supplied public fact without fetching or retaining its source."""

    material = _material(value)
    indicator_gaps = _indicator_gaps(material)
    event = not material["indicator_name"]
    gaps = ["original_url_not_provided"] if material["original_url"] is None else []
    gaps.extend(indicator_gaps)
    if indicator_gaps:
        return _lead_result(
            material,
            state="manual_supplement_required",
            label="用户补充、字段不完整的研究线索",
            text="材料缺少指标的统计期、数值或单位，暂不能作为研究事实。",
            gaps=gaps,
        )
    if material["source_kind"] == "community":
        return _lead_result(
            material,
            state="lead_only",
            label="待查社区线索",
            text="社区内容不作为事实、方向升级或基金映射证据。",
            gaps=gaps,
        )
    if material["source_kind"] == "media":
        return _lead_result(
            material,
            state="lead_only",
            label="媒体报道或观点线索",
            text="媒体材料需要回到原始数据或公告后才能作为研究事实。",
            gaps=gaps,
        )
    if material["original_url"] is None:
        return _lead_result(
            material,
            state="source_verification_required",
            label="用户补充、来源待核验的研究线索",
            text="缺少可追溯的 HTTPS 原始链接，不能据来源声明认定为事实。",
            gaps=gaps,
        )
    if event and not _is_verified_official_material(material):
        return _lead_result(
            material,
            state="source_verification_required",
            label="用户补充、来源待核验的研究线索",
            text="事件或政策材料需要可核对的官方原始链接后才能作为研究事实。",
            gaps=gaps,
        )
    fact_kind = "event_or_policy" if event else "indicator"
    fact = {
        "label": "可追溯的用户补充事实",
        "fact_kind": fact_kind,
        "title": material["title"],
        "source": {
            "source_name": material["source_name"],
            "source_kind_claimed": material["source_kind"],
            "research_source_level": _source_level(material),
            "source_verification_state": _verification_state(material),
            "url": material["original_url"],
            "published_at": material["published_at"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        },
        "indicator": (
            None
            if event
            else {
                "name": material["indicator_name"],
                "value": material["indicator_value"],
                "unit": material["unit"],
                "statistics_period": material["statistics_period"],
                "methodology": material["methodology"],
            }
        ),
    }
    return {
        "conclusion": {
            "state": "supplemented_fact_available",
            "text": "已整理一条用户补充的公开事实，等待下一次明确研究调用引用，仅作预备线索。",
        },
        "fact": fact,
        "analysis": {
            "label": "系统分析",
            "text": "单条补充材料不能单独形成较强方向、基金推荐或交易结论。",
        },
        "evidence_gaps": sorted(set(gaps)),
        "current_research_use": _research_use(material, prepared=True),
        "automatic_industry_data": _automatic_block(),
        "material_handling": "no_url_fetch_no_fulltext_storage",
        "conditional_guidance": _guidance(),
    }


def build_supplement_timeline(materials: tuple[Mapping[str, object], ...]) -> dict[str, object]:
    """Build one non-persistent, same-topic timeline from supplied materials."""

    if not 2 <= len(materials) <= 12:
        raise ValueError("supplement timeline requires two to twelve materials")
    entries = []
    gaps = set()
    domains = set()
    indicator_names = set()
    units = set()
    period_kinds = set()
    for material in materials:
        result = summarize_user_supplied_evidence(material)
        fact = result.get("fact")
        if not isinstance(fact, Mapping) or fact.get("fact_kind") != "indicator":
            gaps.add("timeline_material_not_a_traceable_indicator")
            continue
        indicator = fact.get("indicator")
        source = fact.get("source")
        if not isinstance(indicator, Mapping) or not isinstance(source, Mapping):
            gaps.add("timeline_material_structure_invalid")
            continue
        period = indicator.get("statistics_period")
        name = indicator.get("name")
        unit = indicator.get("unit")
        if not isinstance(period, str) or not isinstance(name, str) or not isinstance(unit, str):
            gaps.add("timeline_indicator_fields_missing")
            continue
        parsed_period = _timeline_period(period)
        if parsed_period is None:
            gaps.add("timeline_statistics_period_not_comparable")
            continue
        domain = result["current_research_use"]["domain_id"]
        if not isinstance(domain, str):
            gaps.add("timeline_domain_missing")
            continue
        domains.add(domain)
        indicator_names.add(_comparison_text(name))
        units.add(_comparison_text(unit))
        period_kinds.add(parsed_period[1])
        entries.append(
            {
                "label": "用户补充事实",
                "statistics_period": period,
                "statistics_period_sort_key": parsed_period,
                "indicator_name": name,
                "value": indicator.get("value"),
                "unit": unit,
                "published_at": source.get("published_at"),
                "source_name": source.get("source_name"),
                "url": source.get("url"),
                "source_verification_state": source.get("source_verification_state"),
            }
        )
    if (
        len(domains) > 1
        or len(indicator_names) > 1
        or len(units) > 1
        or len(period_kinds) > 1
    ):
        return _incomparable_timeline_result(
            domains, indicator_names, units, period_kinds, gaps
        )

    deduplicated, duplicate_periods, conflicting_periods = _deduplicate_periods(entries)
    deduplicated.sort(key=lambda item: item["statistics_period_sort_key"])
    for entry in deduplicated:
        entry.pop("statistics_period_sort_key", None)
    covered_periods = [item["statistics_period"] for item in deduplicated]
    missing_periods = _missing_periods(covered_periods)
    return {
        "conclusion": {
            "state": "timeline_available" if len(deduplicated) >= 2 else "insufficient_data",
            "text": (
                "已按统计期整理同主题、同指标的临时证据时间线，仅用于继续查证。"
                if len(deduplicated) >= 2
                else "可追溯的指标材料不足两条，暂不能形成时间线。"
            ),
        },
        "timeline": deduplicated,
        "coverage": {
            "domains": sorted(domains),
            "indicator_name": next(iter(indicator_names), None),
            "unit": next(iter(units), None),
            "period_kind": _period_kind_label(next(iter(period_kinds), None)),
            "covered_periods": covered_periods,
            "missing_periods": missing_periods,
            "duplicate_periods": duplicate_periods,
            "conflicting_periods": conflicting_periods,
        },
        "analysis": {
            "label": "系统分析",
            "text": (
                "时间线按统计期排序；同一统计期的重复材料不增加有效期间数，"
                "冲突值单列而不静默合并。"
            ),
        },
        "evidence_gaps": sorted(
            gaps | ({"timeline_conflicting_periods"} if conflicting_periods else set())
        ),
        "current_research_use": {
            "state": "used_in_temporary_timeline",
            "strong_direction_eligible": False,
            "fund_mapping_boundary": "基金关联仍只可使用带日期的基准、指数或披露持仓。",
        },
        "automatic_industry_data": _automatic_block(),
        "conditional_guidance": _guidance(),
    }


def _comparison_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _timeline_period(value: str) -> tuple[int, int, int] | None:
    if match := _MONTH_PERIOD.fullmatch(value):
        return (int(match["year"]), 1, int(match["month"]))
    if match := _QUARTER_PERIOD.fullmatch(value):
        return (int(match["year"]), 2, int(match["quarter"]))
    return None


def _deduplicate_periods(
    entries: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str], list[dict[str, object]]]:
    by_period: dict[str, list[dict[str, object]]] = {}
    for entry in entries:
        by_period.setdefault(str(entry["statistics_period"]), []).append(entry)

    deduplicated = []
    duplicate_periods = []
    conflicts = []
    for period, period_entries in by_period.items():
        values = {(entry["value"], entry["unit"]) for entry in period_entries}
        if len(values) > 1:
            conflicts.append(
                {
                    "statistics_period": period,
                    "observations": [
                        {
                            "value": entry["value"],
                            "unit": entry["unit"],
                            "source_name": entry["source_name"],
                            "url": entry["url"],
                        }
                        for entry in period_entries
                    ],
                }
            )
            continue
        if len(period_entries) > 1:
            duplicate_periods.append(period)
        deduplicated.append(min(period_entries, key=lambda item: str(item["published_at"])))
    return deduplicated, sorted(duplicate_periods), sorted(
        conflicts, key=lambda item: str(item["statistics_period"])
    )


def _missing_periods(periods: list[str]) -> list[str]:
    parsed = [_timeline_period(period) for period in periods]
    if not parsed or any(period is None for period in parsed):
        return []
    comparable = [period for period in parsed if period is not None]
    kinds = {period[1] for period in comparable}
    years = {period[0] for period in comparable}
    if len(kinds) != 1 or len(years) != 1:
        return []
    year = comparable[0][0]
    kind = next(iter(kinds))
    covered_indexes = {period[2] for period in comparable}
    last_index = max(covered_indexes)
    if kind == 1:
        missing = [
            f"{year}年{month}月"
            for month in range(1, last_index + 1)
            if month not in covered_indexes
        ]
        future = f"{year + 1}年及以后" if last_index == 12 else f"{year}年{last_index + 1}月及以后"
    else:
        missing = [
            f"{year}年第{quarter}季度"
            for quarter in range(1, last_index + 1)
            if quarter not in covered_indexes
        ]
        future = (
            f"{year + 1}年及以后"
            if last_index == 4
            else f"{year}年第{last_index + 1}季度及以后"
        )
    missing.append(future)
    return missing


def _incomparable_timeline_result(
    domains: set[str],
    indicator_names: set[str],
    units: set[str],
    period_kinds: set[int],
    gaps: set[str],
) -> dict[str, object]:
    return {
        "conclusion": {
            "state": "incomparable_materials",
            "text": "材料的领域、指标名称、单位或统计粒度不一致，不能合并为一条连续时间线。",
        },
        "timeline": [],
        "coverage": {
            "domains": sorted(domains),
            "indicator_names": sorted(indicator_names),
            "units": sorted(units),
            "period_kinds": sorted(_period_kind_label(kind) for kind in period_kinds),
            "covered_periods": [],
            "missing_periods": [],
            "duplicate_periods": [],
            "conflicting_periods": [],
        },
        "analysis": {
            "label": "系统分析",
            "text": "请按同一领域、同一指标、可比较单位和同一统计粒度分别整理材料。",
        },
        "evidence_gaps": sorted(gaps | {"timeline_materials_not_comparable"}),
        "current_research_use": {
            "state": "not_used_in_timeline",
            "strong_direction_eligible": False,
            "fund_mapping_boundary": "基金关联仍只可使用带日期的基准、指数或披露持仓。",
        },
        "automatic_industry_data": _automatic_block(),
        "conditional_guidance": _guidance(),
    }


def _period_kind_label(kind: int | None) -> str | None:
    return {1: "monthly", 2: "quarterly"}.get(kind)
def _material(value: Mapping[str, object]) -> dict[str, str | None]:
    if not isinstance(value, Mapping):
        raise ValueError("supplemented evidence must be a mapping")
    source_kind = _required_text(value.get("source_kind"), "source kind")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError("supplemented evidence source kind is invalid")
    domain_id = _required_text(value.get("domain_id"), "domain")
    if domain_id not in _DOMAIN_IDS:
        raise ValueError("supplemented evidence domain is invalid")
    published_at = _required_text(value.get("published_at"), "publication time")
    try:
        parsed = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("supplemented evidence publication time is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("supplemented evidence publication time is invalid")
    return {
        "source_name": _required_text(value.get("source_name"), "source name"),
        "source_kind": source_kind,
        "title": _required_text(value.get("title"), "title"),
        "published_at": published_at,
        "original_url": _optional_url(value.get("original_url")),
        "statistics_period": _optional_text(value.get("statistics_period"), "statistics period"),
        "indicator_name": _optional_text(value.get("indicator_name"), "indicator name"),
        "indicator_value": _optional_text(value.get("indicator_value"), "indicator value"),
        "unit": _optional_text(value.get("unit"), "unit"),
        "methodology": _optional_text(value.get("methodology"), "methodology"),
        "domain_id": domain_id,
        "source_verification_state": _optional_verification_state(
            value.get("source_verification_state")
        ),
    }


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value, name)
    if text is None:
        raise ValueError(f"supplemented evidence {name} is required")
    return text


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (text := " ".join(value.split())) or len(text) > _MAX_TEXT:
        raise ValueError(f"supplemented evidence {name} is invalid")
    return text


def _optional_url(value: object) -> str | None:
    if value is None:
        return None
    url = _required_text(value, "original URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("supplemented evidence original URL is invalid")
    return url


def _optional_verification_state(value: object) -> str | None:
    if value is None:
        return None
    state = _required_text(value, "source verification state")
    if state != "outer_page_verified":
        raise ValueError("supplemented evidence source verification state is invalid")
    return state


def _indicator_gaps(material: Mapping[str, str | None]) -> list[str]:
    indicator_values = (
        material["indicator_name"],
        material["indicator_value"],
        material["unit"],
        material["statistics_period"],
    )
    if all(value is None for value in indicator_values):
        return []
    names = ("indicator_name", "indicator_value", "indicator_unit", "indicator_statistics_period")
    return [f"{name}_missing" for name, value in zip(names, indicator_values) if value is None]


def _lead_result(
    material: Mapping[str, str | None],
    *,
    state: str,
    label: str,
    text: str,
    gaps: list[str],
) -> dict[str, object]:
    verification_state = (
        "missing_original_url"
        if material["original_url"] is None
        else "user_material_https_url_not_fetched"
    )
    return {
        "conclusion": {"state": state, "text": text},
        "fact": None,
        "lead": {
            "label": label,
            "title": material["title"],
            "source_kind_claimed": material["source_kind"],
            "source_verification_state": verification_state,
            "url": material["original_url"],
            "published_at": material["published_at"],
        },
        "analysis": {"label": "系统分析", "text": "本轮未用该材料推导行业方向或基金关系。"},
        "evidence_gaps": sorted(set(gaps)),
        "current_research_use": _research_use(material, prepared=False),
        "automatic_industry_data": _automatic_block(),
        "material_handling": "no_url_fetch_no_fulltext_storage",
        "conditional_guidance": _guidance(),
    }


def _is_verified_official_material(material: Mapping[str, str | None]) -> bool:
    if material["source_kind"] != "official" or material["original_url"] is None:
        return False
    host = (urlparse(material["original_url"]).hostname or "").casefold()
    return host == "gov.cn" or host.endswith(".gov.cn")


def _source_level(material: Mapping[str, str | None]) -> str:
    if material["source_verification_state"] == "outer_page_verified":
        return "tier_1" if material["source_kind"] == "official" else "tier_2"
    if _is_verified_official_material(material):
        return "provisional_tier_1"
    return "provisional_tier_2"


def _verification_state(material: Mapping[str, str | None]) -> str:
    if material["source_verification_state"] is not None:
        return material["source_verification_state"]
    if _is_verified_official_material(material):
        return "official_domain_https_url_not_fetched"
    return "user_material_https_url_not_fetched"


def _research_use(material: Mapping[str, str | None], *, prepared: bool) -> dict[str, object]:
    return {
        "domain_id": material["domain_id"],
        "state": "prepared_for_next_research" if prepared else "not_ready",
        "usable_for_current_research": False,
        "evidence_level": "preliminary" if prepared else "insufficient_data",
        "strong_direction_eligible": False,
        "fund_mapping_boundary": "基金关联仅可使用带日期的基金披露、基准或指数关系。",
    }


def _automatic_block() -> dict[str, str]:
    return {
        "state": "network_blocked",
        "text": "当前网络环境未形成稳定自动行业来源；本次仅使用用户主动提供的公开材料。",
    }


def _guidance() -> dict[str, object]:
    return {
        "label": "条件性建议",
        "text": "补充第二个独立、可比来源或不同统计期后再做人工复核，不构成交易结论。",
        "action_authorized": False,
        "automatic_trade": False,
    }
