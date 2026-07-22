from __future__ import annotations

import pytest

from kunjin.public_research.events import (
    build_persisted_event_timeline,
    persist_verified_event,
)
from kunjin.public_research.evidence import (
    build_persisted_timeline,
    build_refresh_plan,
    persist_verified_evidence,
)
from kunjin.storage.repository import Repository


def _material(period: str, value: str, **overrides: object) -> dict[str, object]:
    material: dict[str, object] = {
        "source_name": "公开行业统计",
        "publisher": "公开行业统计",
        "source_kind": "industry_data",
        "title": f"{period}样本产量",
        "published_at": "2026-08-01T08:00:00+08:00",
        "original_url": f"https://example.test/{period}",
        "statistics_period": period,
        "indicator_name": "样本产量",
        "indicator_value": value,
        "unit": "万台",
        "methodology": "来源页面标明单月统计口径",
        "domain_id": "autos",
        "source_verification_state": "outer_page_verified",
        "short_excerpt": "页面明确给出单月样本产量。",
    }
    material.update(overrides)
    return material


def test_isolated_cold_start_persists_then_reuses_monthly_evidence(tmp_path) -> None:
    """A temporary repository models a fresh task without relying on thread memory."""
    repository = Repository(tmp_path / "research.db")
    repository.migrate()

    for month, value in ((3, "100"), (4, "101"), (5, "102"), (6, "103"), (7, "104")):
        persist_verified_evidence(repository, _material(f"2026年{month}月", value))

    timeline = build_persisted_timeline(
        repository, "autos", "样本产量", "万台"
    )
    plan = build_refresh_plan(
        repository, "autos", "样本产量", "万台", "2026年10月"
    )

    assert timeline["current_research_use"]["state"] == "used_in_persisted_timeline"
    assert timeline["timeline"][0]["label"] == "经外层核验的结构化公开事实"
    assert "持久化、经外层核验" in timeline["conclusion"]["text"]
    assert timeline["timeline"][0]["source_tier"] == "tier_2"
    assert timeline["timeline"][0]["publisher"] == "公开行业统计"
    assert timeline["timeline"][0]["evidence_id"] is not None
    assert timeline["coverage"]["covered_periods"] == [
        "2026年3月",
        "2026年4月",
        "2026年5月",
        "2026年6月",
        "2026年7月",
    ]
    assert plan["new_periods_to_fetch"] == ["2026年8月", "2026年9月", "2026年10月"]
    assert plan["revision_check_periods"] == [
        "2026年3月",
        "2026年4月",
        "2026年5月",
        "2026年6月",
        "2026年7月",
    ]
    assert plan["historical_evidence_state"] == "usable_pending_lightweight_revision_check"
    assert plan["network_action"] == "outer_research_only"


def test_cold_start_refresh_plan_has_an_explicit_requested_month_range(tmp_path) -> None:
    repository = Repository(tmp_path / "cold-start.db")
    repository.migrate()

    plan = build_refresh_plan(
        repository,
        "autos",
        "样本产量",
        "万台",
        "2026年7月",
        "2026年3月",
    )

    assert plan["new_periods_to_fetch"] == [
        "2026年3月",
        "2026年4月",
        "2026年5月",
        "2026年6月",
        "2026年7月",
    ]
    assert plan["revision_check_periods"] == []


def test_conflicting_sources_remain_visible_in_persisted_timeline(tmp_path) -> None:
    repository = Repository(tmp_path / "conflict.db")
    repository.migrate()
    persist_verified_evidence(repository, _material("2026年3月", "100"))
    persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "101",
            source_name="第二来源",
            publisher="第二来源",
            original_url="https://second.example.test/march",
        ),
    )
    persist_verified_evidence(repository, _material("2026年4月", "110"))

    timeline = build_persisted_timeline(
        repository, "autos", "样本产量", "万台"
    )

    assert timeline["coverage"]["conflicting_periods"][0]["statistics_period"] == "2026年3月"
    assert "timeline_conflicting_periods" in timeline["evidence_gaps"]


def test_newer_same_source_record_is_linked_as_a_revision_not_a_conflict(tmp_path) -> None:
    repository = Repository(tmp_path / "revision.db")
    repository.migrate()
    first = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "100",
            original_url="https://example.test/march",
            published_at="2026-04-01T08:00:00+08:00",
        ),
    )
    revised = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "101",
            original_url="https://example.test/march",
            published_at="2026-04-02T08:00:00+08:00",
        ),
    )

    assert revised["revision_of_evidence_id"] == first["evidence_id"]
    timeline = build_persisted_timeline(
        repository, "autos", "样本产量", "万台"
    )
    assert timeline["coverage"]["covered_periods"] == ["2026年3月"]
    assert timeline["coverage"]["conflicting_periods"] == []


def test_reverse_order_same_source_values_remain_a_visible_conflict(tmp_path) -> None:
    repository = Repository(tmp_path / "reverse-revision.db")
    repository.migrate()
    newer = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "101",
            original_url="https://example.test/march",
            published_at="2026-04-02T08:00:00+08:00",
        ),
    )
    older = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "100",
            original_url="https://example.test/march",
            published_at="2026-04-01T08:00:00+08:00",
        ),
    )

    assert newer["revision_of_evidence_id"] is None
    assert older["revision_of_evidence_id"] is None
    timeline = build_persisted_timeline(repository, "autos", "样本产量", "万台")
    assert timeline["coverage"]["conflicting_periods"][0]["statistics_period"] == "2026年3月"


def test_revision_order_compares_instants_not_timezone_text(tmp_path) -> None:
    repository = Repository(tmp_path / "timezone-revision.db")
    repository.migrate()
    later = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "101",
            original_url="https://example.test/march",
            published_at="2026-04-02T01:00:00+00:00",
        ),
    )
    earlier_in_a_later_looking_offset = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "100",
            original_url="https://example.test/march",
            published_at="2026-04-02T08:30:00+08:00",
        ),
    )

    assert later["revision_of_evidence_id"] is None
    assert earlier_in_a_later_looking_offset["revision_of_evidence_id"] is None
    timeline = build_persisted_timeline(repository, "autos", "样本产量", "万台")
    assert timeline["coverage"]["conflicting_periods"][0]["statistics_period"] == "2026年3月"


def test_refresh_plan_spans_year_boundaries_for_monthly_indicators(tmp_path) -> None:
    repository = Repository(tmp_path / "cross-year.db")
    repository.migrate()
    persist_verified_evidence(repository, _material("2026年10月", "100"))

    plan = build_refresh_plan(
        repository,
        "autos",
        "样本产量",
        "万台",
        "2027年3月",
        "2026年10月",
    )

    assert plan["new_periods_to_fetch"] == [
        "2026年11月",
        "2026年12月",
        "2027年1月",
        "2027年2月",
        "2027年3月",
    ]


def test_event_sources_cluster_without_upgrading_media_claims(tmp_path) -> None:
    repository = Repository(tmp_path / "events.db")
    repository.migrate()
    event = {
        "domain_id": "power_energy",
        "event_key": "power-sector-2026-07-22",
        "source_name": "交易所行情",
        "publisher": "交易所行情",
        "source_kind": "platform_data",
        "title": "电力板块盘中异动",
        "original_url": "https://example.test/market",
        "event_occurred_at": "2026-07-22T10:30:00+08:00",
        "published_at": "2026-07-22T10:35:00+08:00",
        "fact_summary": "行情页面记录电力板块盘中异动。",
        "claim_boundary": "未将任何媒体归因写成市场原因事实。",
        "source_verification_state": "outer_page_verified",
    }
    first = persist_verified_event(repository, event)
    second = persist_verified_event(
        repository,
        {
            **event,
            "source_name": "监管公告",
            "publisher": "监管公告",
            "source_kind": "official",
            "original_url": "https://www.csrc.gov.cn/example",
            "published_at": "2026-07-22T11:00:00+08:00",
            "fact_summary": "公告页面确认同日相关事项。",
        },
    )

    assert first["storage_state"] == "stored"
    assert second["storage_state"] == "stored"
    timeline = build_persisted_event_timeline(repository, "power_energy")
    assert len(timeline["events"]) == 1
    assert len(timeline["events"][0]["sources"]) == 2
    assert timeline["events"][0]["sources"][1]["source_tier"] == "tier_1"
    lead = persist_verified_event(
        repository,
        {
            **event,
            "source_name": "百家号",
            "publisher": "百家号",
            "source_kind": "community",
            "original_url": "https://baijiahao.baidu.com/s?id=example",
        },
    )
    assert lead["storage_state"] == "stored"
    media_report = persist_verified_event(
        repository,
        {
            **event,
            "source_name": "财经媒体",
            "publisher": "财经媒体",
            "source_kind": "media",
            "title": "盘后报道：电力板块走强",
            "original_url": "https://media.example.test/market",
        },
    )
    assert media_report["storage_state"] == "stored"
    second_media_report = persist_verified_event(
        repository,
        {
            **event,
            "source_name": "第二财经媒体",
            "publisher": "第二财经媒体",
            "source_kind": "media",
            "title": "另一标题：电力板块午后走强",
            "original_url": "https://second-media.example.test/market",
        },
    )
    assert second_media_report["storage_state"] == "stored"
    timeline = build_persisted_event_timeline(repository, "power_energy")
    leads = [
        source for source in timeline["events"][0]["sources"] if source["evidence_state"] == "lead"
    ]
    reports = [
        source
        for source in timeline["events"][0]["sources"]
        if source["evidence_state"] == "reported_fact"
    ]
    assert {source["publisher"] for source in leads} == {"百家号"}
    assert {source["publisher"] for source in reports} == {"财经媒体", "第二财经媒体"}
    assert reports[0]["source_kind"] == "media"
    assert reports[0]["source_tier"] == "tier_2"
    assert timeline["events"][0]["reported_facts"] == ["行情页面记录电力板块盘中异动。"]
    assert timeline["events"][0]["direct_fact_source_count"] == 2
    assert timeline["events"][0]["reported_fact_source_count"] == 2


def test_event_duplicate_and_comparable_fact_conflict_remain_explicit(tmp_path) -> None:
    repository = Repository(tmp_path / "event-conflict.db")
    repository.migrate()
    event = {
        "domain_id": "power_energy",
        "event_key": "power-sector-2026-07-22",
        "source_name": "行情平台甲",
        "publisher": "行情平台甲",
        "source_kind": "platform_data",
        "title": "电力板块异动",
        "original_url": "https://example.test/market-a",
        "published_at": "2026-07-22T10:35:00+08:00",
        "fact_summary": "行情页面记录电力板块异动。",
        "claim_boundary": "未将媒体归因写成市场原因事实。",
        "event_fact_key": "涨停家数",
        "event_fact_value": "4",
        "event_fact_unit": "家",
        "source_verification_state": "outer_page_verified",
    }
    first = persist_verified_event(repository, event)
    duplicate = persist_verified_event(repository, event)
    second = persist_verified_event(
        repository,
        {
            **event,
            "source_name": "行情平台乙",
            "publisher": "行情平台乙",
            "original_url": "https://example.test/market-b",
            "event_fact_value": "5",
        },
    )

    assert first["storage_state"] == "stored"
    assert duplicate["storage_state"] == "duplicate_unchanged"
    assert second["storage_state"] == "stored"
    timeline = build_persisted_event_timeline(repository, "power_energy")
    assert timeline["events"][0]["comparable_fact_conflict"] is True


def test_event_rejects_incomplete_comparable_fact(tmp_path) -> None:
    repository = Repository(tmp_path / "incomplete-event.db")
    repository.migrate()

    with pytest.raises(ValueError, match="event comparable fact is incomplete"):
        persist_verified_event(
            repository,
            {
                "domain_id": "power_energy",
                "event_key": "power-sector-2026-07-22",
                "source_name": "行情平台",
                "source_kind": "platform_data",
                "title": "电力板块异动",
                "original_url": "https://example.test/market",
                "published_at": "2026-07-22T10:35:00+08:00",
                "fact_summary": "行情页面记录电力板块异动。",
                "claim_boundary": "未将媒体归因写成市场原因事实。",
                "event_fact_key": "涨停家数",
                "event_fact_value": "4",
                "source_verification_state": "outer_page_verified",
            },
        )


def test_unregistered_official_claim_is_downgraded_to_tier_two(tmp_path) -> None:
    repository = Repository(tmp_path / "official-domain.db")
    repository.migrate()
    stored = persist_verified_evidence(
        repository,
        _material(
            "2026年3月",
            "100",
            source_kind="official",
            original_url="https://unrelated.example.test/release",
            official_publisher_domain="unrelated.example.test",
        ),
    )
    assert stored["storage_state"] == "stored"
    with repository.connect() as connection:
        source_tier = connection.execute(
            "SELECT source_tier FROM public_research_evidence WHERE id=?",
            (stored["evidence_id"],),
        ).fetchone()["source_tier"]
    assert source_tier == "tier_2"


def test_persistence_rejects_material_not_verified_by_outer_reader(tmp_path) -> None:
    repository = Repository(tmp_path / "unverified.db")
    repository.migrate()

    with pytest.raises(ValueError, match="outer page verification"):
        persist_verified_evidence(
            repository,
            _material(
                "2026年6月",
                "100",
                source_verification_state="user_material_https_url_not_fetched",
            ),
        )
