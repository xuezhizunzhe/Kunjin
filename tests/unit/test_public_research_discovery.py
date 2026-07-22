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
    assert all(item["current_news_refresh_state"] == "pending" for item in plans)


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
