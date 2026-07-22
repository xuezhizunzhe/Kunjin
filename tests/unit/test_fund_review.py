from __future__ import annotations

from kunjin.fund_review import build_fund_review


def test_review_is_conservative_when_personal_constraints_are_missing() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={"request": {}},
        intelligence={"sources": [], "what_happened": [], "retrieval": {}},
        market_scan={"directions": []},
        portfolio=None,
        horizon=None,
        risk_tolerance=None,
        near_term_use=None,
    )

    assert result["conclusion"]["disposition"] == "需补充信息"
    assert result["constraints"]["missing"] == [
        "持有期限",
        "风险承受程度",
        "近期是否可能使用这笔钱",
    ]
    assert result["conditional_guidance"]["automatic_trade"] is False


def test_review_uses_conditional_action_label_with_constraints() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="reduce_to_cash",
        brief={}, intelligence={}, market_scan={}, portfolio={"input_source": "cached"},
        horizon="长期", risk_tolerance="中等", near_term_use="no",
    )

    assert result["conclusion"]["disposition"] == "减仓观察"
    assert result["portfolio_context"]["input_source"] == "cached"


def test_review_keeps_a_non_ready_profile_at_information_gathering() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={},
        intelligence={},
        market_scan={},
        portfolio=None,
        horizon="long",
        risk_tolerance="high",
        near_term_use="no",
        guardrails={"readiness": "先降低风险"},
    )

    assert result["conclusion"]["disposition"] == "需补充信息"


def test_review_keeps_market_context_empty_without_source_backed_facts() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={"facts": []},
        intelligence={"what_happened": []},
        market_scan={"directions": []},
        portfolio=None,
        horizon="medium",
        risk_tolerance="high",
        near_term_use="no",
        guardrails={"readiness": "可以继续研究"},
    )

    assert result["market_and_industry_context"]["text"] == (
        "本次未取得足以支持该市场结论的可核验事实。"
    )


def test_review_rejects_unrelated_market_facts_for_a_theme_conclusion() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={"facts": []},
        intelligence={
            "what_happened": [
                {
                    "title": "某基金公司高管变更",
                    "excerpt": "该公告仅涉及基金公司治理。",
                    "source": {
                        "source_name": "公开媒体",
                        "url": "https://example.test/unrelated",
                        "published_at": "2026-07-21",
                    },
                }
            ]
        },
        market_scan={"directions": []},
        portfolio=None,
        horizon="medium",
        risk_tolerance="high",
        near_term_use="no",
        guardrails={"readiness": "可以继续研究"},
    )

    assert result["market_and_industry_context"]["state"] == "insufficient_data"


def test_review_keeps_only_theme_relevant_market_facts_with_sources() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={"facts": []},
        intelligence={
            "what_happened": [
                {
                    "title": "AI 芯片板块的公开观察",
                    "excerpt": "该来源记录半导体与算力相关事实。",
                    "source": {
                        "source_name": "公开媒体",
                        "url": "https://example.test/ai",
                        "published_at": "2026-07-21",
                    },
                },
                {
                    "title": "某基金公司高管变更",
                    "excerpt": "该公告仅涉及基金公司治理。",
                    "source": {
                        "source_name": "公开媒体",
                        "url": "https://example.test/unrelated",
                        "published_at": "2026-07-21",
                    },
                },
            ]
        },
        market_scan={"directions": []},
        portfolio=None,
        horizon="medium",
        risk_tolerance="high",
        near_term_use="no",
        guardrails={"readiness": "可以继续研究"},
    )

    context = result["market_and_industry_context"]
    assert context["state"] == "source_backed"
    assert [item["source"]["url"] for item in context["facts"]] == [
        "https://example.test/ai"
    ]


def test_review_projects_deduplicated_market_facts_with_verified_source_label() -> None:
    result = build_fund_review(
        fund_code="123456",
        action="continue_holding",
        brief={
            "facts": [
                {
                    "field_id": "current_benchmark",
                    "value": {"description": "中证人工智能主题指数收益率"},
                    "publisher": "公开平台",
                    "canonical_url": "https://example.test/benchmark",
                    "published_at": None,
                    "retrieved_at": "2026-07-22",
                    "source_tier": "tier_2",
                }
            ]
        },
        intelligence={
            "what_happened": [
                {
                    "title": "二季度人工智能产业链公开数据",
                    "excerpt": "2026年二季度，电子行业配置上升。",
                    "source": {
                        "source_name": "上海证券报",
                        "url": "https://www.stcn.com/article/detail/one.html",
                        "published_at": "2026-07-22",
                    },
                },
                {
                    "title": "二季度人工智能产业链公开数据",
                    "excerpt": "2026年二季度，电子行业配置上升。",
                    "source": {
                        "source_name": "上海证券报",
                        "url": "https://www.stcn.com/article/detail/two.html",
                        "published_at": "2026-07-22",
                    },
                },
            ]
        },
        market_scan={"directions": []},
        portfolio=None,
        horizon="medium",
        risk_tolerance="high",
        near_term_use="no",
        guardrails={"readiness": "可以继续研究"},
    )

    facts = result["market_and_industry_context"]["facts"]
    assert len(facts) == 1
    assert facts[0]["source"]["source_name"] == "证券时报网/公开媒体（待核验）"
    assert facts[0]["source_kind"] == "media_report"
    assert facts[0]["statistics_period"] == "2026年二季度（来源摘要）"
    assert "中证人工智能主题指数" in facts[0]["why_it_may_relate"]


def test_related_context_explains_industry_overlap_as_disclosure_only() -> None:
    from kunjin.fund_review import build_related_fund_context

    context = build_related_fund_context(
        fund_codes=("123456", "654321"),
        weights={"123456": "0.2", "654321": "0.1"},
        comparison={
            "coverage": {"members_total": 2, "members_with_disclosures": 2},
            "pairwise_overlap": [
                {
                    "industry": {
                        "left_fund_code": "123456",
                        "right_fund_code": "654321",
                        "overlap": "81.39",
                        "left_report_period": "2026-06-30",
                        "right_report_period": "2026-06-30",
                    }
                }
            ],
            "sources": [],
        },
    )

    assert context["group_portfolio_weight"] == "0.3"
    assert "不等于相同百分比的底层股票" in context["industry_disclosed_overlaps"][0]["explanation"]
