"""Read-only overview for persisted public indicators and events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

from kunjin.public_research.events import build_persisted_event_timeline
from kunjin.public_research.evidence import _timeline_period, build_refresh_plan
from kunjin.storage.repository import Repository

_DOMAIN_NAMES = {
    "power_energy": "电力与能源",
    "coal_oil_gas": "煤炭与油气",
    "real_estate_materials": "房地产与建材",
    "industrial_commodities": "工业品与大宗商品",
    "autos": "汽车",
    "shipping_trade": "航运、港口与外贸",
    "ai_compute": "AI 与算力",
    "consumer": "消费",
    "policy": "政策",
    "weather": "天气",
}
_VERIFIED_STATE = "outer_page_verified"


def build_local_research_overview(
    repository: Repository, *, as_of: date | None = None
) -> dict[str, object]:
    """Summarize reusable public evidence without selecting a domain in advance."""

    today = as_of or datetime.now(timezone.utc).date()
    rows = _current_indicator_rows(repository)
    grouped: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["domain_id"]), str(row["indicator_name"]), str(row["unit"]))].append(
            row
        )
    event_domains = _event_domains(repository)
    domain_ids = sorted({key[0] for key in grouped} | event_domains)
    domains = []
    for domain_id in domain_ids:
        indicators = [
            _indicator_summary(repository, domain_id, indicator_name, unit, values, today)
            for (stored_domain, indicator_name, unit), values in grouped.items()
            if stored_domain == domain_id
        ]
        indicators.sort(key=lambda item: (str(item["indicator_name"]), str(item["unit"])))
        events = build_persisted_event_timeline(repository, domain_id)["events"]
        if not isinstance(events, list):
            raise ValueError("persisted event overview is invalid")
        domains.append(
            {
                "domain_id": domain_id,
                "domain_name": _DOMAIN_NAMES.get(domain_id, domain_id),
                "indicators": indicators,
                "events": events,
                "event_counts": _event_counts(events),
                "latest_event_at": _latest_event_at(events),
            }
        )
    retrieved_values = [str(row["retrieved_at"]) for row in rows]
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "data_cutoff": max(retrieved_values, default=None),
        "domains": domains,
        "coverage": {
            "persisted_indicator_record_count": len(rows),
            "persisted_indicator_series_count": len(grouped),
            "persisted_event_cluster_count": sum(len(item["events"]) for item in domains),
            "covered_domain_ids": [item["domain_id"] for item in domains],
        },
        "outer_discovery": {
            "outer_discovery_required": True,
            "current_news_refresh_state": "pending",
            "text": (
                "本地历史证据可复用，但近期新闻和行情尚未在本次调用中由外层刷新；"
                "最终回答前必须进行一次受控发现，或明确记录环境阻塞。"
            ),
        },
        "network_action": "outer_research_required_for_current_news",
        "fulltext_stored": False,
    }


def _current_indicator_rows(repository: Repository) -> list[Any]:
    with repository.connect() as connection:
        return connection.execute(
            """
            SELECT * FROM public_research_evidence AS evidence
            WHERE evidence.verification_state=?
              AND NOT EXISTS (
                  SELECT 1 FROM public_research_evidence AS revision
                  WHERE revision.revision_of_evidence_id=evidence.id
              )
            ORDER BY evidence.domain_id, evidence.indicator_name, evidence.unit,
                evidence.statistics_period, evidence.published_at, evidence.id
            """,
            (_VERIFIED_STATE,),
        ).fetchall()


def _event_domains(repository: Repository) -> set[str]:
    with repository.connect() as connection:
        return {
            str(row["domain_id"])
            for row in connection.execute("SELECT DISTINCT domain_id FROM public_research_events")
        }


def _indicator_summary(
    repository: Repository,
    domain_id: str,
    indicator_name: str,
    unit: str,
    rows: Iterable[Any],
    today: date,
) -> dict[str, object]:
    values = list(rows)
    periods = sorted({str(row["statistics_period"]) for row in values}, key=_period_key)
    latest = max(
        values,
        key=lambda row: (
            _period_key(str(row["statistics_period"])),
            str(row["published_at"]),
        ),
    )
    first_period = periods[0]
    refresh_plan = build_refresh_plan(
        repository,
        domain_id,
        indicator_name,
        unit,
        _current_period(first_period, today),
        first_period,
    )
    observations: dict[str, set[str]] = defaultdict(set)
    for row in values:
        observations[str(row["statistics_period"])].add(str(row["indicator_value"]))
    return {
        "indicator_name": indicator_name,
        "unit": unit,
        "covered_periods": periods,
        "latest_published_at": str(latest["published_at"]),
        "latest_retrieved_at": str(latest["retrieved_at"]),
        "conflicting_periods": [
            period for period, observed in observations.items() if len(observed) > 1
        ],
        "latest_observation": {
            "statistics_period": str(latest["statistics_period"]),
            "value": str(latest["indicator_value"]),
            "methodology": latest["methodology"],
            "source": {
                "source_name": str(latest["source_name"]),
                "publisher": str(latest["publisher"]),
                "url": str(latest["original_url"]),
                "source_tier": str(latest["source_tier"]),
                "published_at": str(latest["published_at"]),
                "retrieved_at": str(latest["retrieved_at"]),
            },
        },
        "incremental_refresh": {
            "covered_periods": refresh_plan["covered_periods"],
            "new_periods_to_fetch": refresh_plan["new_periods_to_fetch"],
            "revision_check_periods": refresh_plan["revision_check_periods"],
            "conflicting_periods": refresh_plan["conflicting_periods"],
        },
    }


def _period_key(period: str) -> tuple[int, int, int]:
    parsed = _timeline_period(period)
    if parsed is None:
        raise ValueError("persisted indicator period is invalid")
    return parsed


def _current_period(first_period: str, today: date) -> str:
    _, kind, _ = _period_key(first_period)
    if kind == 1:
        return f"{today.year}年{today.month}月"
    return f"{today.year}年第{(today.month - 1) // 3 + 1}季度"


def _event_counts(events: Iterable[object]) -> dict[str, int]:
    counts = {"fact": 0, "reported_fact": 0, "lead": 0}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("persisted event is invalid")
        sources = event.get("sources")
        if not isinstance(sources, list):
            raise ValueError("persisted event sources are invalid")
        for source in sources:
            if not isinstance(source, dict) or source.get("evidence_state") not in counts:
                raise ValueError("persisted event source is invalid")
            counts[str(source["evidence_state"])] += 1
    return counts


def _latest_event_at(events: Iterable[object]) -> str | None:
    published = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("published_at"), list):
            raise ValueError("persisted event is invalid")
        published.extend(value for value in event["published_at"] if isinstance(value, str))
    return max(published, default=None)
