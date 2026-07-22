from __future__ import annotations

from kunjin.fund_candidates import build_fund_candidate_review


def _comparison(*, warning: str | None = None) -> dict[str, object]:
    return {
        "coverage": {"members_with_disclosures": 2, "members_total": 2},
        "metric_orderings": {"90d": {"total_return": {"fund_codes": ["111111", "222222"]}}},
        "sources": [
            {
                "fund_code": "111111",
                "source_name": "基金公告",
                "title": "定期报告",
                "url": "https://example.test/one",
                "published_at": "2026-07-01",
                "retrieved_at": "2026-07-22",
                "source_tier": "official",
            },
            {
                "fund_code": "222222",
                "source_name": "基金公告",
                "title": "定期报告",
                "url": "https://example.test/two",
                "published_at": "2026-07-01",
                "retrieved_at": "2026-07-22",
                "source_tier": "official",
            },
        ],
        "candidate_portfolio_overlap": {},
        "pairwise_overlap": [],
        "warnings": [] if warning is None else [warning],
        "data_gaps": [],
    }


def test_ready_profile_keeps_explicit_candidates_as_research_only() -> None:
    result = build_fund_candidate_review(
        fund_codes=("111111", "222222"),
        comparison=_comparison(),
        guardrails={"readiness": "可以继续研究"},
    )

    assert result["conclusion"]["disposition"] == "可作为研究候选"
    assert result["scope"]["candidate_fund_codes"] == ["111111", "222222"]
    assert result["candidate_reviews"][0]["public_sources"][0]["url"]
    assert result["conditional_guidance"]["automatic_trade"] is False
    assert "唯一优胜者" in result["analysis"]["text"]


def test_missing_profile_or_category_mismatch_stays_conservative() -> None:
    profile_missing = build_fund_candidate_review(
        fund_codes=("111111", "222222"),
        comparison=_comparison(),
        guardrails={"readiness": "需补充信息"},
    )
    mismatch = build_fund_candidate_review(
        fund_codes=("111111", "222222"),
        comparison=_comparison(warning="comparability_warning:222222:type_mismatch"),
        guardrails={"readiness": "可以继续研究"},
    )

    assert profile_missing["conclusion"]["disposition"] == "需补充个人信息"
    assert mismatch["conclusion"]["disposition"] == "先确认是否同类"
    assert mismatch["analysis"]["same_category_check"] == "需确认"
