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


def test_field_lineage_marks_unbound_top10_and_missing_nav_url() -> None:
    comparison = _comparison()
    comparison.update(
        {
            "sources": [
                {
                    "id": 10,
                    "fund_code": "111111",
                    "document_kind": "quarterly_holdings",
                    "source_name": "基金披露",
                    "title": "季度持仓",
                    "url": "https://example.test/one/holdings",
                    "publisher": "基金公司",
                    "published_at": "2026-07-20",
                    "retrieved_at": "2026-07-22",
                    "source_tier": 2,
                },
                {
                    "id": 11,
                    "fund_code": "111111",
                    "document_kind": "manager_history",
                    "source_name": "基金披露",
                    "title": "经理信息",
                    "url": "https://example.test/one/manager",
                    "publisher": "基金公司",
                    "published_at": "2026-07-20",
                    "retrieved_at": "2026-07-22",
                    "source_tier": 2,
                },
                {
                    "id": 12,
                    "fund_code": "111111",
                    "document_kind": "fee_schedule",
                    "source_name": "基金披露",
                    "title": "费率说明",
                    "url": "https://example.test/one/fees",
                    "publisher": "基金公司",
                    "published_at": "2026-07-20",
                    "retrieved_at": "2026-07-22",
                    "source_tier": 2,
                },
            ],
            "managers": {
                "111111": [
                    {
                        "manager_name": "示例经理",
                        "source_document_id": 11,
                    }
                ]
            },
            "fees": {"111111": [{"fee_type": "management", "source_document_id": 12}]},
            "candidate_disclosures": {
                "111111": {
                    "benchmarks": {"items": []},
                    "quarterly_holdings": {
                        "evidence_level": "partial",
                        "report_period": "2026-06-30",
                        "selection": {"report_period_binding": "unresolved"},
                        "source_document_ids": [10],
                        "conflicts": ["multiple_top10_table_groups_unbound"],
                    },
                    "industry_exposure": {
                        "evidence_level": "verified_fact",
                        "source_document_ids": [],
                    },
                }
            },
            "windows": {
                "90d": [
                    {
                        "fund_code": "111111",
                        "effective_end": "2026-07-22",
                        "max_drawdown": "0.1",
                    }
                ]
            },
            "data_dates": {"common_nav_end": "2026-07-22"},
        }
    )

    result = build_fund_candidate_review(
        fund_codes=("111111", "222222"),
        comparison=comparison,
        guardrails={"readiness": "可以继续研究"},
    )

    lineage = result["candidate_reviews"][0]["field_lineage"]
    assert lineage["managers"]["state"] == "source_backed"
    assert lineage["managers"]["sources"][0]["url"].endswith("/manager")
    assert lineage["fees"]["sources"][0]["url"].endswith("/fees")
    assert lineage["quarterly_holdings"]["evidence_level"] == "partial"
    assert lineage["quarterly_holdings"]["selection"]["report_period_binding"] == "unresolved"
    assert "不得用于确定性证券重叠" in lineage["quarterly_holdings"]["usage_boundary"]
    assert lineage["formal_nav_metrics"]["state"] == "source_lineage_unavailable"
    assert lineage["formal_nav_metrics"]["as_of"] == "2026-07-22"
