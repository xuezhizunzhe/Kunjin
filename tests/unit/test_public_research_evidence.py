from __future__ import annotations

import pytest

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


def test_persisted_evidence_rebuilds_timeline_and_plans_incremental_months(tmp_path) -> None:
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
        _material("2026年3月", "100", original_url="https://example.test/march"),
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
