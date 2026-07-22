from __future__ import annotations

from copy import deepcopy

import pytest

from kunjin.public_research.summary import summarize_public_research


def _payload() -> dict[str, object]:
    return {
        "request": {
            "workflow": "news_recent",
            "finished_at": "2026-07-21T08:10:00+00:00",
            "interval": {
                "start_at": "2026-07-18T08:00:00+00:00",
                "end_at": "2026-07-21T08:00:00+00:00",
                "timezone_name": "Asia/Shanghai",
            },
        },
        "items": [
            {
                "evidence_role": "source_fact",
                "publisher": "公开来源",
                "published_at": "2026-07-20T08:00:00+00:00",
                "retrieved_at": "2026-07-20T08:10:00+00:00",
                "canonical_url": "https://example.test/item",
                "source_tier": "tier_1",
                "title": "公开行业事件",
                "excerpt": "可核验摘要",
                "integrity_state": "active",
            }
        ],
        "experimental_shadow": {
            "status": "experimental_shadow",
            "market_state": "neutral",
            "market_direction_status": "evidence_only",
        },
        "fund_relevance": {
            "subject_fund_code": None,
            "coverage_scope": None,
            "links": [],
        },
        "missing_evidence": ["official_confirmation_required"],
        "conflicts": [],
        "cross_validation": {
            "complete": False,
            "opposing_evidence_detection": "not_systematically_implemented",
        },
    }


def test_summary_preserves_dated_sources_and_separates_sections() -> None:
    result = summarize_public_research(_payload())

    assert result["what_happened"] == [
        {
            "label": "可核验事实",
            "title": "公开行业事件",
            "excerpt": "可核验摘要",
            "source": {
                "source_name": "公开来源",
                "url": "https://example.test/item",
                "published_at": "2026-07-20T08:00:00+00:00",
                "source_tier": "tier_1",
                "retrieved_at": "2026-07-20T08:10:00+00:00",
            },
        }
    ]
    assert result["sources"] == [result["what_happened"][0]["source"]]
    assert result["why_it_may_matter"]["label"] == "系统分析"
    assert result["conditional_guidance"] == {
        "label": "条件性建议",
        "text": "可继续关注相关公开信息，但不构成买卖指令。",
        "action_authorized": False,
        "automatic_trade": False,
    }
    assert result["retrieval"]["workflow"] == "news_recent"


def test_summary_degrades_honestly_when_no_fact_is_available() -> None:
    payload = _payload()
    payload["items"] = []

    result = summarize_public_research(payload)

    assert result["conclusion"] == {
        "state": "insufficient_data",
        "text": "本次公开信息不足，暂不形成方向判断。",
    }
    assert result["what_happened"] == []
    assert result["sources"] == []
    assert result["risks_and_unknowns"]["evidence_gaps"] == [
        "no_active_public_facts",
        "official_confirmation_required"
    ]


@pytest.mark.parametrize(
    "mutate",
    (
        lambda payload: payload["items"][0].update(
            {"canonical_url": "not-an-absolute-url"}
        ),
        lambda payload: payload["items"][0].update({"raw_body": "secret"}),
        lambda payload: payload.update({"private_path": "/tmp/owner"}),
    ),
)
def test_summary_rejects_malformed_or_private_input(mutate) -> None:
    payload = deepcopy(_payload())
    mutate(payload)

    with pytest.raises(ValueError, match="public research payload"):
        summarize_public_research(payload)
