from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.selection.research import public_shortlist_payload
from tests.unit.test_selection_models import shortlist_result_fixture

_PRIVATE_KEYS = frozenset(
    {
        "account_title",
        "amount",
        "asset",
        "cost",
        "debt",
        "income",
        "monthly_income",
        "portfolio_weight",
        "profit",
        "profile",
        "reserve",
        "shares",
        "total_value",
    }
)
_INTERPRETATION_KEYS = frozenset(
    {"best", "buy", "rank", "recommended", "safe", "score", "winner"}
)


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key).casefold() for key in value),
            *(key for item in value.values() for key in _all_keys(item)),
        }
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _all_keys(item)}
    return set()


def test_public_shortlist_payload_has_stable_sections() -> None:
    payload = public_shortlist_payload(shortlist_result_fixture())

    assert set(payload) == {
        "action_boundary",
        "as_of",
        "beginner_explanation_zh",
        "candidate_reviews",
        "comparability",
        "comparison_state",
        "conditional_shortlist",
        "conflicts",
        "input_fingerprint",
        "metric_comparisons",
        "missing_evidence",
        "personal_gate",
        "request",
        "warnings",
    }
    assert payload["action_boundary"] == {
        "action_authorized": False,
        "action_maturity": "evidence_only",
        "automatic_trade": False,
        "exact_amount_available": False,
    }
    assert payload["request"] == {
        "candidate_codes": ["000002", "000001"],
        "candidate_count": 2,
    }
    assert payload["conditional_shortlist"] == {
        "fund_codes": ["000002", "000001"],
        "invalidation_conditions": ["allocation_state_changes"],
        "merit_ordered": False,
    }
    assert payload["personal_gate"] == {
        "allocation_freshness": "fresh",
        "allocation_state": "fresh",
        "allocation_status": "range_available",
        "blocking_codes": [],
        "constraint_codes": ["horizon_binding"],
        "suitability_freshness": "fresh",
        "suitability_state": "fresh",
        "suitability_status": "ready_for_allocation",
    }


def test_public_projection_is_recursive_private_and_interpretation_key_free() -> None:
    payload = public_shortlist_payload(shortlist_result_fixture())
    keys = _all_keys(payload)

    assert keys.isdisjoint(_PRIVATE_KEYS)
    assert keys.isdisjoint(_INTERPRETATION_KEYS)
    assert "amount_min" in keys
    assert "amount_max" in keys


def test_public_projection_serializes_decimal_dates_and_timestamps() -> None:
    result = replace(
        shortlist_result_fixture(),
        metric_comparisons=(
            (
                "fees",
                {
                    "effective_at": datetime(2026, 7, 19, 8, tzinfo=timezone.utc),
                    "effective_from": date(2026, 7, 18),
                    "rate": Decimal("0.0100"),
                },
            ),
        ),
    )

    metrics = public_shortlist_payload(result)["metric_comparisons"]

    assert metrics == {
        "fees": {
            "effective_at": "2026-07-19T08:00:00+00:00",
            "effective_from": "2026-07-18",
            "rate": "0.0100",
        }
    }


def test_beginner_explanation_separates_evidence_and_action_limits() -> None:
    explanation = public_shortlist_payload(shortlist_result_fixture())[
        "beginner_explanation_zh"
    ]

    assert set(explanation) == {
        "change_conditions",
        "observed_facts",
        "personal_gate_limits",
        "reasoned_comparisons",
        "unknown_coverage",
    }
    combined = " ".join(explanation.values())
    assert "不是买入信号" in combined
    assert "不提供精确金额" in combined
    assert "不提供卖出时机" in combined
