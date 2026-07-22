from __future__ import annotations

import pytest

from kunjin.public_research.supplement import (
    build_supplement_timeline,
    summarize_user_supplied_evidence,
)


def _evidence(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_name": "公开行业统计",
        "source_kind": "industry_data",
        "title": "月度行业统计",
        "published_at": "2026-07-20T08:00:00+00:00",
        "original_url": "https://example.test/monthly",
        "statistics_period": "2026年6月",
        "indicator_name": "样本产量",
        "indicator_value": "100",
        "unit": "万台",
        "methodology": "来源页面标明月度统计口径",
        "domain_id": "autos",
    }
    value.update(overrides)
    return value


def test_https_industry_material_is_traceable_but_only_provisional_and_deferred() -> None:
    result = summarize_user_supplied_evidence(_evidence())

    assert result["conclusion"]["state"] == "supplemented_fact_available"
    assert result["fact"]["label"] == "可追溯的用户补充事实"
    assert result["fact"]["source"]["research_source_level"] == "provisional_tier_2"
    assert result["fact"]["source"]["source_verification_state"] == (
        "user_material_https_url_not_fetched"
    )
    assert result["fact"]["indicator"] == {
        "name": "样本产量",
        "value": "100",
        "unit": "万台",
        "statistics_period": "2026年6月",
        "methodology": "来源页面标明月度统计口径",
    }
    assert result["current_research_use"]["state"] == "prepared_for_next_research"
    assert result["current_research_use"]["usable_for_current_research"] is False
    assert result["current_research_use"]["strong_direction_eligible"] is False
    assert result["automatic_industry_data"]["state"] == "network_blocked"


def test_incomplete_indicator_requests_missing_public_fields_without_fetching() -> None:
    result = summarize_user_supplied_evidence(
        _evidence(indicator_value=None, unit=None, statistics_period=None)
    )

    assert result["conclusion"]["state"] == "manual_supplement_required"
    assert result["fact"] is None
    assert result["evidence_gaps"] == [
        "indicator_statistics_period_missing",
        "indicator_unit_missing",
        "indicator_value_missing",
    ]
    assert result["material_handling"] == "no_url_fetch_no_fulltext_storage"


def test_official_claim_without_url_is_only_a_source_unverified_lead() -> None:
    result = summarize_user_supplied_evidence(
        _evidence(
            source_kind="official",
            original_url=None,
            statistics_period=None,
            indicator_name=None,
            indicator_value=None,
            unit=None,
            methodology=None,
        )
    )

    assert result["conclusion"]["state"] == "source_verification_required"
    assert result["fact"] is None
    assert result["lead"]["label"] == "用户补充、来源待核验的研究线索"
    assert result["lead"]["source_verification_state"] == "missing_original_url"
    assert result["current_research_use"]["usable_for_current_research"] is False
    assert result["evidence_gaps"] == ["original_url_not_provided"]


def test_community_material_never_becomes_a_fact() -> None:
    result = summarize_user_supplied_evidence(
        _evidence(source_kind="community", original_url="https://example.test/post")
    )

    assert result["conclusion"]["state"] == "lead_only"
    assert result["fact"] is None
    assert result["lead"]["label"] == "待查社区线索"
    assert result["current_research_use"]["usable_for_current_research"] is False


def test_media_material_is_not_labeled_as_a_verifiable_fact() -> None:
    result = summarize_user_supplied_evidence(
        _evidence(source_kind="media", original_url="https://example.test/article")
    )

    assert result["conclusion"]["state"] == "lead_only"
    assert result["fact"] is None
    assert result["lead"]["label"] == "媒体报道或观点线索"


def test_http_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="original URL"):
        summarize_user_supplied_evidence(_evidence(original_url="http://example.test/item"))


def test_timeline_uses_multiple_statistical_periods_without_strong_direction() -> None:
    march = _evidence(
        title="3月全社会用电量",
        published_at="2026-04-20T00:00:00+08:00",
        statistics_period="2026年3月",
        indicator_name="全社会用电量",
        indicator_value="8595",
        unit="亿千瓦时",
    )
    april = _evidence(
        title="4月全社会用电量",
        published_at="2026-05-19T00:00:00+08:00",
        statistics_period="2026年4月",
        indicator_name="全社会用电量",
        indicator_value="8205",
        unit="亿千瓦时",
    )

    result = build_supplement_timeline((april, march))

    assert result["conclusion"]["state"] == "timeline_available"
    assert [item["statistics_period"] for item in result["timeline"]] == [
        "2026年3月",
        "2026年4月",
    ]
    assert result["current_research_use"]["state"] == "used_in_temporary_timeline"
    assert result["current_research_use"]["strong_direction_eligible"] is False
    assert result["coverage"] == {
        "domains": ["autos"],
        "indicator_name": "全社会用电量",
        "unit": "亿千瓦时",
        "period_kind": "monthly",
        "covered_periods": ["2026年3月", "2026年4月"],
        "missing_periods": ["2026年1月", "2026年2月", "2026年5月及以后"],
        "duplicate_periods": [],
        "conflicting_periods": [],
    }


def test_timeline_rejects_mixed_domains_indicators_or_units() -> None:
    result = build_supplement_timeline(
        (
            _evidence(statistics_period="2026年3月"),
            _evidence(
                domain_id="power_energy",
                indicator_name="全社会用电量",
                unit="亿千瓦时",
                statistics_period="2026年4月",
            ),
        )
    )

    assert result["conclusion"]["state"] == "incomparable_materials"
    assert result["timeline"] == []
    assert result["current_research_use"]["strong_direction_eligible"] is False
    assert "timeline_materials_not_comparable" in result["evidence_gaps"]


def test_timeline_deduplicates_same_statistical_period_before_counting_evidence() -> None:
    march = _evidence(statistics_period="2026年3月", indicator_value="100")
    duplicate_march = _evidence(
        title="同一统计期转载材料",
        published_at="2026-04-21T00:00:00+08:00",
        statistics_period="2026年3月",
        indicator_value="100",
    )
    april = _evidence(statistics_period="2026年4月", indicator_value="110")

    result = build_supplement_timeline((march, duplicate_march, april))

    assert [item["statistics_period"] for item in result["timeline"]] == [
        "2026年3月",
        "2026年4月",
    ]
    assert result["coverage"]["duplicate_periods"] == ["2026年3月"]
    assert len(result["coverage"]["covered_periods"]) == 2


def test_timeline_exposes_conflicting_values_for_a_statistical_period() -> None:
    march = _evidence(statistics_period="2026年3月", indicator_value="100")
    conflicting_march = _evidence(
        title="同一统计期冲突材料",
        statistics_period="2026年3月",
        indicator_value="101",
    )
    april = _evidence(statistics_period="2026年4月", indicator_value="110")

    result = build_supplement_timeline((march, conflicting_march, april))

    assert result["conclusion"]["state"] == "insufficient_data"
    assert result["coverage"]["covered_periods"] == ["2026年4月"]
    assert result["coverage"]["conflicting_periods"][0]["statistics_period"] == "2026年3月"
    assert "timeline_conflicting_periods" in result["evidence_gaps"]


def test_timeline_sorts_by_statistical_period_not_publication_time() -> None:
    march = _evidence(
        published_at="2026-05-20T00:00:00+08:00",
        statistics_period="2026年3月",
    )
    april = _evidence(
        published_at="2026-04-19T00:00:00+08:00",
        statistics_period="2026年4月",
    )

    result = build_supplement_timeline((april, march))

    assert [item["statistics_period"] for item in result["timeline"]] == [
        "2026年3月",
        "2026年4月",
    ]


def test_timeline_rejects_mixed_monthly_and_quarterly_periods() -> None:
    monthly = _evidence(statistics_period="2026年3月")
    quarterly = _evidence(statistics_period="2026年第1季度")

    result = build_supplement_timeline((monthly, quarterly))

    assert result["conclusion"]["state"] == "incomparable_materials"
    assert result["timeline"] == []
    assert result["coverage"]["period_kinds"] == ["monthly", "quarterly"]


def test_quarterly_timeline_reports_missing_quarters() -> None:
    first_quarter = _evidence(statistics_period="2026年第1季度", indicator_value="100")
    third_quarter = _evidence(statistics_period="2026年第3季度", indicator_value="110")

    result = build_supplement_timeline((third_quarter, first_quarter))

    assert result["coverage"]["period_kind"] == "quarterly"
    assert result["coverage"]["covered_periods"] == ["2026年第1季度", "2026年第3季度"]
    assert result["coverage"]["missing_periods"] == ["2026年第2季度", "2026年第4季度及以后"]


def test_monthly_timeline_uses_next_year_after_december() -> None:
    january = _evidence(statistics_period="2026年1月", indicator_value="100")
    december = _evidence(statistics_period="2026年12月", indicator_value="110")

    result = build_supplement_timeline((december, january))

    assert result["coverage"]["missing_periods"][-1] == "2027年及以后"
    assert "2026年13月及以后" not in result["coverage"]["missing_periods"]
