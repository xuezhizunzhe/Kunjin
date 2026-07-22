from __future__ import annotations

from datetime import date

from kunjin.public_research.events import persist_verified_event
from kunjin.public_research.evidence import persist_verified_evidence
from kunjin.public_research.overview import build_local_research_overview
from kunjin.public_research.scan import scan_public_research
from kunjin.storage.repository import Repository


def _indicator(
    domain_id: str,
    period: str,
    name: str,
    value: str,
    unit: str,
) -> dict[str, object]:
    return {
        "source_name": "公开统计",
        "publisher": "公开统计",
        "source_kind": "industry_data",
        "title": f"{period}{name}",
        "published_at": "2026-07-20T08:00:00+08:00",
        "original_url": f"https://example.test/{domain_id}/{name}/{period}",
        "statistics_period": period,
        "indicator_name": name,
        "indicator_value": value,
        "unit": unit,
        "methodology": "页面明确为单月或当期统计。",
        "domain_id": domain_id,
        "source_verification_state": "outer_page_verified",
    }


def _payload() -> dict[str, object]:
    return {
        "request": {
            "workflow": "market_overview",
            "finished_at": "2026-07-23T08:00:00+00:00",
            "interval": {
                "start_at": "2026-07-20T08:00:00+00:00",
                "end_at": "2026-07-23T08:00:00+00:00",
                "timezone_name": "Asia/Shanghai",
            },
        },
        "items": [],
        "experimental_shadow": {
            "status": "experimental_shadow",
            "market_state": "insufficient_data",
            "market_direction_status": "evidence_only",
            "sector_states": [],
        },
        "fund_relevance": {"subject_fund_code": None, "coverage_scope": None},
        "missing_evidence": ["no_active_public_facts"],
        "conflicts": [],
        "cross_validation": {"complete": False},
    }


def test_local_overview_and_scan_reuse_all_persisted_public_evidence(tmp_path) -> None:
    repository = Repository(tmp_path / "overview.db")
    repository.migrate()
    records = [
        ("power_energy", "2026年6月", "全社会用电量", "8981", "亿千瓦时"),
        ("autos", "2026年4月", "乘用车市场零售量", "138.4", "万辆"),
        ("autos", "2026年5月", "乘用车市场零售量", "151.0", "万辆"),
        ("autos", "2026年6月", "乘用车市场零售量", "160.2", "万辆"),
        ("real_estate_materials", "2026年4月", "新房环比上涨城市数", "14", "个"),
        ("real_estate_materials", "2026年5月", "新房环比上涨城市数", "16", "个"),
        ("real_estate_materials", "2026年6月", "新房环比上涨城市数", "20", "个"),
        ("real_estate_materials", "2026年4月", "水泥单月产量", "14571", "万吨"),
        ("real_estate_materials", "2026年5月", "水泥单月产量", "14991", "万吨"),
        ("real_estate_materials", "2026年6月", "水泥单月产量", "14423", "万吨"),
    ]
    for record in records:
        persist_verified_evidence(repository, _indicator(*record))
    base_event = {
        "domain_id": "power_energy",
        "event_key": "power-energy-2026-07-20-market-move",
        "source_name": "行情数据",
        "publisher": "行情数据",
        "source_kind": "platform_data",
        "title": "电力个股日线",
        "original_url": "https://example.test/market",
        "published_at": "2026-07-20T15:00:00+08:00",
        "fact_summary": "行情记录电力相关个股当日上涨。",
        "claim_boundary": "该行情事实不确认媒体归因。",
        "source_verification_state": "outer_page_verified",
    }
    persist_verified_event(repository, base_event)
    persist_verified_event(
        repository,
        {
            **base_event,
            "source_name": "财经媒体",
            "publisher": "财经媒体",
            "source_kind": "media",
            "title": "电力板块报道",
            "original_url": "https://media.example.test/market",
        },
    )
    persist_verified_event(
        repository,
        {
            **base_event,
            "source_name": "社区",
            "publisher": "社区",
            "source_kind": "community",
            "title": "电力讨论",
            "original_url": "https://community.example.test/market",
        },
    )

    overview = build_local_research_overview(repository, as_of=date(2026, 7, 23))
    domains = {item["domain_id"]: item for item in overview["domains"]}

    assert overview["coverage"] == {
        "persisted_indicator_record_count": 10,
        "persisted_indicator_series_count": 4,
        "persisted_event_cluster_count": 1,
        "covered_domain_ids": ["autos", "power_energy", "real_estate_materials"],
    }
    assert domains["power_energy"]["event_counts"] == {
        "fact": 1,
        "reported_fact": 1,
        "lead": 1,
    }
    power_indicator = domains["power_energy"]["indicators"][0]
    assert power_indicator["covered_periods"] == ["2026年6月"]
    assert power_indicator["incremental_refresh"]["new_periods_to_fetch"] == ["2026年7月"]
    assert power_indicator["incremental_refresh"]["revision_check_periods"] == ["2026年6月"]
    assert overview["outer_discovery"]["outer_discovery_required"] is True
    assert overview["outer_discovery"]["current_news_refresh_state"] == "pending"

    scan = scan_public_research(_payload(), local_overview=overview)
    candidates = {item["domain_id"] for item in scan["candidate_directions"]}
    assert {"power_energy", "autos", "real_estate_materials"} <= candidates
    assert any(item["origin"] == "persisted_event_fact" for item in scan["timeline"])
    directions = {item["domain_id"]: item for item in scan["directions"]}
    assert directions["consumer"]["evidence_state"] == "insufficient_data"
    assert scan["automatic_industry_data"]["state"] == "persisted_history_available"
    assert scan["outer_discovery"]["current_news_refresh_state"] == "pending"
