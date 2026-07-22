from __future__ import annotations

from kunjin.review_triggers import build_review_triggers


def test_review_triggers_are_on_demand_and_non_transactional() -> None:
    result = build_review_triggers("123456")

    assert result["fund_code"] == "123456"
    assert result["conclusion"]["review_now"] is False
    assert len(result["review_triggers"]) == 3
    assert result["action_boundary"] == {
        "automatic_monitoring": False,
        "automatic_trade": False,
        "exact_amount_available": False,
    }
    assert "实时完整持仓" in result["evidence_boundary"][0]
