from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from kunjin.public_research.scan import scan_public_research

_SIGNAL_RANK = {"需要谨慎": 3, "值得继续研究": 2, "继续观察": 1}
_MAX_DIRECTIONS = 3
_MAX_FACTS_PER_DIRECTION = 3


def build_cross_domain_panorama(
    windows: Sequence[tuple[str, Mapping[str, object]]],
) -> dict[str, object]:
    """Build a bounded, source-linked panorama from public research windows."""

    if not 1 <= len(windows) <= 3:
        raise ValueError("cross-domain panorama requires one to three windows")
    scans = [(label, scan_public_research(payload)) for label, payload in windows]
    preliminary = _preliminary_directions(scans)
    candidates = _strong_directions(scans, preliminary)
    return {
        "conclusion": {
            "state": (
                "evidence_backed_research"
                if candidates
                else "preliminary_research_available"
                if preliminary
                else "insufficient_data"
            ),
            "text": (
                "已从多个公开窗口筛出较强证据方向，不构成买卖结论。"
                if candidates
                else "已找到可进一步查证的预备研究方向，不构成市场结论或基金建议。"
                if preliminary
                else "三个时间窗口均未形成可继续研究的方向，暂不形成方向判断。"
            ),
        },
        "preliminary_directions": preliminary,
        "candidate_directions": candidates,
        "strong_evidence_directions": candidates,
        "coverage": _coverage(scans, preliminary),
        "automatic_industry_data": scans[0][1]["automatic_industry_data"],
        "time_windows": [
            {
                "label": label,
                "timeline": _display_facts(scan["timeline"]),
                "sources": _display_sources(scan["sources"]),
                "evidence_gaps": scan["risks_and_unknowns"]["evidence_gaps"],
            }
            for label, scan in scans
        ],
        "analysis_boundary": {
            "possible_causes": "可能原因只来自各时间窗口中的可核验事实和市场观察。",
            "alternative_explanation": "短期变化也可能受市场情绪、流动性或未覆盖因素影响。",
            "fund_relationship": (
                "基金关联只能依据有日期的披露持仓、基准或指数；"
                "本扫描未指定基金，不推断实时完整持仓。"
            ),
        },
        "conditional_guidance": {
            "label": "条件性建议",
            "text": "对候选方向继续补充官方、行业协会或基金披露资料后再做人工复核。",
            "action_authorized": False,
            "automatic_trade": False,
        },
    }


def _preliminary_directions(
    scans: Sequence[tuple[str, Mapping[str, object]]],
) -> list[dict[str, object]]:
    by_domain: dict[str, dict[str, object]] = {}
    for label, scan in scans:
        directions = scan.get("directions")
        timeline = scan.get("timeline")
        if not isinstance(directions, Sequence) or not isinstance(timeline, Sequence):
            raise ValueError("cross-domain scan directions are invalid")
        for direction in directions:
            if not isinstance(direction, Mapping) or direction.get("evidence_state") != "observed":
                continue
            domain_id = direction.get("domain_id")
            matched_titles = direction.get("matched_facts")
            if not isinstance(domain_id, str) or not isinstance(matched_titles, Sequence):
                raise ValueError("cross-domain scan direction is invalid")
            facts = _matched_facts(timeline, matched_titles, label)
            if not facts:
                continue
            current = by_domain.setdefault(
                domain_id,
                {
                    "label": "预备研究方向",
                    "domain_id": domain_id,
                    "domain_name": direction.get("domain_name"),
                    "evidence_level": "preliminary",
                    "observed_in": [],
                    "facts": [],
                    "signal": direction.get("signal"),
                    "why_matched": _why_matched(direction),
                    "alternative_explanation": (
                        "单条媒体或公开事实也可能反映短期情绪、流动性或未覆盖变量。"
                    ),
                    "evidence_needed": (
                        "需要官方或行业协会的连续产量、订单、价格、指数或基金披露数据进一步查证。"
                    ),
                },
            )
            if label not in current["observed_in"]:
                current["observed_in"].append(label)
            _extend_unique_facts(current["facts"], facts)
            if direction.get("signal") in _SIGNAL_RANK and (
                current["signal"] not in _SIGNAL_RANK
                or _SIGNAL_RANK[direction["signal"]] > _SIGNAL_RANK[current["signal"]]
            ):
                current["signal"] = direction["signal"]
    return sorted(
        by_domain.values(),
        key=lambda item: (-len(item["observed_in"]), item["domain_id"]),
    )[:_MAX_DIRECTIONS]


def _coverage(
    scans: Sequence[tuple[str, Mapping[str, object]]],
    preliminary: Sequence[Mapping[str, object]],
) -> dict[str, list[dict[str, str]]]:
    covered = [
        {
            "domain_id": item["domain_id"],
            "domain_name": item["domain_name"],
        }
        for item in preliminary
        if isinstance(item.get("domain_id"), str)
        and isinstance(item.get("domain_name"), str)
    ]
    uncovered: dict[str, dict[str, str]] = {}
    for _, scan in scans:
        directions = scan.get("directions")
        if not isinstance(directions, Sequence):
            raise ValueError("cross-domain scan directions are invalid")
        for direction in directions:
            if not isinstance(direction, Mapping):
                raise ValueError("cross-domain scan direction is invalid")
            domain_id = direction.get("domain_id")
            domain_name = direction.get("domain_name")
            if (
                direction.get("evidence_state") == "insufficient_data"
                and isinstance(domain_id, str)
                and isinstance(domain_name, str)
            ):
                uncovered[domain_id] = {
                    "domain_id": domain_id,
                    "domain_name": domain_name,
                    "evidence_needed": "需要带日期的官方、行业协会或交易所公开数据后再判断。",
                }
    return {
        "covered_domains": covered,
        "domains_without_conclusion": sorted(
            uncovered.values(), key=lambda item: item["domain_id"]
        ),
    }


def _why_matched(direction: Mapping[str, object]) -> str:
    sectors = direction.get("matched_sectors")
    titles = direction.get("matched_facts")
    sector_values = sectors if isinstance(sectors, Sequence) else ()
    title_values = titles if isinstance(titles, Sequence) else ()
    sector_text = _bounded_names(sector_values)
    title_text = _bounded_names(title_values)
    parts = []
    if sector_text:
        parts.append(f"公开市场观察包含{sector_text}")
    if title_text:
        parts.append(f"带日期的公开事实标题匹配{title_text}")
    return "；".join(parts) or "仅按预设领域关键词匹配，尚无可核验事实。"


def _bounded_names(values: Sequence[object]) -> str:
    names = [item for item in values if isinstance(item, str)]
    suffix = "等" if len(names) > _MAX_FACTS_PER_DIRECTION else ""
    return "、".join(names[:_MAX_FACTS_PER_DIRECTION]) + suffix


def _strong_directions(
    scans: Sequence[tuple[str, Mapping[str, object]]],
    preliminary: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_domain: dict[str, dict[str, object]] = {}
    for label, scan in scans:
        directions = scan.get("directions")
        if not isinstance(directions, Sequence):
            raise ValueError("cross-domain scan directions are invalid")
        for direction in directions:
            if not isinstance(direction, Mapping) or direction.get("evidence_state") != "observed":
                continue
            domain_id = direction.get("domain_id")
            signal = direction.get("signal")
            if signal == "证据不足":
                continue
            if not isinstance(domain_id, str) or signal not in _SIGNAL_RANK:
                raise ValueError("cross-domain scan direction is invalid")
            current = by_domain.setdefault(
                domain_id,
                {
                    "domain_id": domain_id,
                    "domain_name": direction["domain_name"],
                    "signal": signal,
                    "observed_in": [],
                    "matched_sectors": [],
                    "possible_reason": "至少两个时间窗口出现同领域的有效方向信号。",
                    "alternative_explanation": "还可能受到市场情绪、流动性或未覆盖变量影响。",
                    "risk": direction["caveat"],
                },
            )
            if _SIGNAL_RANK[signal] > _SIGNAL_RANK[current["signal"]]:
                current["signal"] = signal
                current["risk"] = direction["caveat"]
            if label not in current["observed_in"]:
                current["observed_in"].append(label)
            _extend_unique_texts(current["matched_sectors"], direction["matched_sectors"])
    preliminary_by_domain = {
        item["domain_id"]: item for item in preliminary if isinstance(item.get("domain_id"), str)
    }
    values = []
    for domain_id, item in by_domain.items():
        lead = preliminary_by_domain.get(domain_id)
        if lead is None:
            continue
        source_urls = {
            _mapping(fact.get("source")).get("url")
            for fact in lead["facts"]
            if isinstance(fact, Mapping)
        }
        fact_windows = {
            fact.get("window") for fact in lead["facts"] if isinstance(fact, Mapping)
        }
        multi_window = len(item["observed_in"]) >= 2 and len(fact_windows - {None}) >= 2
        independent_sources = len(source_urls - {None}) >= 2
        if not multi_window and not independent_sources:
            continue
        evidence_basis = "multiple_windows" if multi_window else "independent_sources"
        values.append(
            {
                **item,
                "facts": lead["facts"],
                "evidence_level": "stronger",
                "evidence_basis": evidence_basis,
                "possible_reason": (
                    "至少两个时间窗口出现同领域的有效方向信号。"
                    if multi_window
                    else "同一方向至少有两个不同公开来源的可核验事实，仍需补充跨窗口确认。"
                ),
            }
        )
    return sorted(
        values,
        key=lambda item: (-_SIGNAL_RANK[item["signal"]], item["domain_id"]),
    )[:_MAX_DIRECTIONS]


def _matched_facts(
    timeline: Sequence[object], matched_titles: Sequence[object], window: str
) -> list[dict[str, object]]:
    titles = {item for item in matched_titles if isinstance(item, str)}
    result = []
    for fact in timeline:
        if not isinstance(fact, Mapping) or fact.get("title") not in titles:
            continue
        projected = _display_fact(fact)
        if projected is not None:
            result.append({"window": window, **projected})
    return result


def _extend_unique_facts(target: object, values: Sequence[Mapping[str, object]]) -> None:
    if not isinstance(target, list):
        raise ValueError("preliminary facts are invalid")
    known = {(item.get("title"), _mapping(item.get("source")).get("url")) for item in target}
    for item in values:
        if len(target) >= _MAX_FACTS_PER_DIRECTION:
            break
        identity = (item.get("title"), _mapping(item.get("source")).get("url"))
        if identity not in known:
            target.append(dict(item))
            known.add(identity)


def _extend_unique_texts(target: object, values: object) -> None:
    if (
        not isinstance(target, list)
        or not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
    ):
        raise ValueError("cross-domain scan matched sectors are invalid")
    for value in values:
        if not isinstance(value, str):
            raise ValueError("cross-domain scan matched sector is invalid")
        if value not in target:
            target.append(value)


def _display_facts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("cross-domain scan timeline is invalid")
    return [item for fact in value if (item := _display_fact(fact)) is not None]


def _display_fact(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    source = _display_source(value.get("source"))
    if source is None:
        return None
    title = value.get("title")
    excerpt = value.get("excerpt")
    if not isinstance(title, str) or not isinstance(excerpt, str):
        return None
    return {
        "label": "可核验事实",
        "title": title,
        "what_happened": excerpt,
        "source": source,
        "statistics_period": _statistics_period(f"{title} {excerpt}"),
    }


def _display_sources(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("cross-domain scan sources are invalid")
    sources = []
    seen = set()
    for item in value:
        source = _display_source(item)
        if source is None or source["url"] in seen:
            continue
        sources.append(source)
        seen.add(source["url"])
    return sources


def _display_source(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    url = value.get("url")
    if not isinstance(url, str) or not url:
        return None
    host = (urlparse(url).hostname or "").casefold()
    reported_name = value.get("source_name")
    if host.endswith("stcn.com"):
        source_name = "证券时报网/公开媒体（待核验）"
        source_kind = "media_report"
    elif host.endswith("gov.cn"):
        source_name = "中国政府网"
        source_kind = "official_publication"
    else:
        source_name = f"{host or '公开媒体'}（待核验）"
        source_kind = "media_report"
    return {
        "source_name": source_name,
        "reported_source_name": reported_name,
        "source_kind": source_kind,
        "url": url,
        "published_at": value.get("published_at"),
        "retrieved_at": value.get("retrieved_at"),
        "source_tier": value.get("source_tier"),
    }


def _statistics_period(value: str) -> str | None:
    if "2026年二季度" in value:
        return "2026年二季度（来源摘要）"
    if "2026年一季度" in value:
        return "2026年一季度（来源摘要）"
    if "二季度" in value:
        return "二季度（来源摘要未标明年份）"
    return None


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}
