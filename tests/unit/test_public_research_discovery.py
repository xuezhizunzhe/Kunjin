from __future__ import annotations

import pytest

from kunjin.public_research.discovery import (
    assess_candidate_discovery_outcome,
    build_candidate_discovery_plan,
)


def _scan() -> dict[str, object]:
    return {
        "candidate_directions": [
            {"domain_id": "power_energy", "domain_name": "电力与能源"},
            {"domain_id": "autos", "domain_name": "汽车"},
            {"domain_id": "real_estate_materials", "domain_name": "房地产与建材"},
        ]
    }


def test_discovery_plan_uses_separate_bounded_query_per_candidate() -> None:
    result = build_candidate_discovery_plan(_scan())

    plans = result["candidate_plans"]
    assert [item["domain_id"] for item in plans] == [
        "power_energy",
        "autos",
        "real_estate_materials",
    ]
    assert len({item["query"] for item in plans}) == 3
    assert all(item["attempt_limits"]["direct_page_attempts"] == 2 for item in plans)
    assert all(item["time_budget"]["per_direct_page_seconds"] == 30 for item in plans)
    assert all(item["time_budget"]["outer_discovery_soft_budget_seconds"] == 120 for item in plans)
    assert all(
        item["time_budget"]["explicit_deep_outer_discovery_soft_budget_seconds"] == 480
        for item in plans
    )
    assert all("不终止整份回答" in item["time_budget"]["scope"] for item in plans)
    assert all("低质量线索" in item["time_budget"]["on_soft_budget"] for item in plans)
    assert all(item["current_news_refresh_state"] == "pending" for item in plans)
    assert all(item["research_window"] == ["近一周", "近一月"] for item in plans)
    assert all("原始 HTTPS URL" in item["evidence_requirements"] for item in plans)


def test_discovery_plan_uses_domain_terms_without_fixed_source_sites() -> None:
    result = build_candidate_discovery_plan(
        {
            "candidate_directions": [
                {"domain_id": "coal_oil_gas", "domain_name": "煤炭与油气"},
                {"domain_id": "shipping_trade", "domain_name": "航运、港口与外贸"},
                {"domain_id": "ai_compute", "domain_name": "AI 与算力"},
                {"domain_id": "consumer", "domain_name": "消费"},
            ]
        }
    )

    plans = result["candidate_plans"]
    assert [item["domain_id"] for item in plans] == [
        "coal_oil_gas",
        "shipping_trade",
        "ai_compute",
    ]
    assert "煤炭 油气" in plans[0]["query"]
    assert "集装箱吞吐量 出口" in plans[1]["query"]
    assert "算力 半导体" in plans[2]["query"]
    assert all("http" not in item["query"] for item in plans)


def test_discovery_plan_expands_healthcare_and_consumer_without_fixed_sites() -> None:
    result = build_candidate_discovery_plan(
        {
            "candidate_directions": [
                {"domain_id": "healthcare", "domain_name": "医药"},
                {"domain_id": "consumer", "domain_name": "消费"},
            ]
        }
    )

    plans = result["candidate_plans"]
    assert "医疗器械" in plans[0]["query"]
    assert "食品饮料 旅游 服装 家电" in plans[1]["query"]


def test_search_only_discovery_is_partial_not_completed() -> None:
    plan = build_candidate_discovery_plan(_scan())["candidate_plans"][0]

    result = assess_candidate_discovery_outcome(plan, discovery_query_executed=True)

    assert result["discovery_query_executed"] is True
    assert result["direct_page_read_count"] == 0
    assert result["current_news_refresh_state"] == "partial"


def test_trusted_direct_current_page_completes_discovery() -> None:
    plan = build_candidate_discovery_plan(_scan())["candidate_plans"][0]

    result = assess_candidate_discovery_outcome(
        plan,
        discovery_query_executed=True,
        direct_page_attempts=(
            {
                "attempt_role": "primary",
                "source_class": "official_or_regulator",
                "read_state": "read",
                "current_window_validated": True,
                "original_publisher": "公开主管部门",
            },
        ),
        newly_persisted_evidence_count=1,
    )

    assert result["direct_page_read_count"] == 1
    assert result["independent_source_count"] == 1
    assert result["newly_persisted_evidence_count"] == 1
    assert result["current_news_refresh_state"] == "completed"


def test_primary_and_one_alternative_failure_stop_as_blocked() -> None:
    plan = build_candidate_discovery_plan(_scan())["candidate_plans"][0]
    attempts = (
        {
            "attempt_role": "primary",
            "source_class": "official_or_regulator",
            "read_state": "blocked",
            "current_window_validated": False,
        },
        {
            "attempt_role": "trusted_alternative",
            "source_class": "industry_association_or_structured_data",
            "read_state": "blocked",
            "current_window_validated": False,
        },
    )

    result = assess_candidate_discovery_outcome(
        plan, discovery_query_executed=True, direct_page_attempts=attempts
    )

    assert result["current_news_refresh_state"] == "blocked"
    assert result["direct_page_attempt_count"] == 2
    with pytest.raises(ValueError, match="attempt limit"):
        assess_candidate_discovery_outcome(
            plan,
            discovery_query_executed=True,
            direct_page_attempts=(*attempts, attempts[1]),
        )


def test_reposts_with_same_original_publisher_do_not_add_independent_source_count() -> None:
    plan = build_candidate_discovery_plan(_scan())["candidate_plans"][0]

    result = assess_candidate_discovery_outcome(
        plan,
        discovery_query_executed=True,
        direct_page_attempts=(
            {
                "attempt_role": "primary",
                "source_class": "credible_financial_media",
                "read_state": "read",
                "current_window_validated": True,
                "original_publisher": "同一原始发布者",
            },
            {
                "attempt_role": "trusted_alternative",
                "source_class": "credible_financial_media",
                "read_state": "read",
                "current_window_validated": True,
                "original_publisher": "同一原始发布者",
                "is_repost": True,
            },
        ),
    )

    assert result["direct_page_read_count"] == 2
    assert result["independent_source_count"] == 1
