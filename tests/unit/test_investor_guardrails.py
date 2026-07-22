from __future__ import annotations

from kunjin.investor_guardrails import build_investor_guardrails


def test_missing_profile_information_stays_conservative() -> None:
    result = build_investor_guardrails(
        emergency_fund=None,
        near_term_use=None,
        horizon=None,
        volatility=None,
        portfolio=None,
    )

    assert result["readiness"] == "需补充信息"
    assert result["action_boundary"]["automatic_trade"] is False


def test_long_horizon_with_emergency_fund_allows_research_not_trade() -> None:
    result = build_investor_guardrails(
        emergency_fund="yes",
        near_term_use="no",
        horizon="long",
        volatility="medium",
        portfolio=None,
    )

    assert result["readiness"] == "可以继续研究"
    assert result["action_boundary"]["exact_amount_available"] is False


def test_near_term_use_or_missing_emergency_reserve_does_not_expand_risk() -> None:
    result = build_investor_guardrails(
        emergency_fund="no",
        near_term_use="yes",
        horizon="long",
        volatility="high",
        portfolio=None,
    )

    assert result["readiness"] == "先降低风险"
    assert result["allocation_boundary"]["risk_assets"] == "先不增加或仅在信息补齐后研究"


def test_cached_portfolio_separates_observations_from_coverage_gaps() -> None:
    result = build_investor_guardrails(
        emergency_fund="yes",
        near_term_use="no",
        horizon="medium",
        volatility="high",
        portfolio={
            "portfolio_overview": {
                "position_count": 11,
                "largest_position_share": "0.16",
            },
            "observed_exposures": [
                {
                    "relationship_type": "same_company",
                    "metrics": {"company_name": "示例基金公司"},
                }
            ],
            "coverage": {"holdings": {"evidence_state": "insufficient_data"}},
            "missing_evidence": ["current_benchmark"],
        },
    )

    research = result["portfolio_research"]
    assert research["state"] == "research_ready"
    assert "当前缓存中有11只非零持仓。" in research["confirmed_observations"]
    assert "已观察到同一基金公司关系：示例基金公司。" in research[
        "confirmed_observations"
    ]
    assert any("完整股票与行业重叠认证覆盖不足" in item for item in research["unknown_boundaries"])
    assert all("基金代码" not in item for item in research["priority_categories"])
