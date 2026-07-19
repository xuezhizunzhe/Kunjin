from __future__ import annotations

import re
from dataclasses import FrozenInstanceError, replace

import pytest

from kunjin.selection.scope import (
    PRODUCT_CATEGORIES,
    RESEARCH_HORIZONS,
    RESEARCH_OBJECTIVES,
    RESEARCH_SCOPE_SCHEMA_VERSION,
    RESEARCH_SCOPE_TAXONOMY_VERSION,
    ResearchScopeRequest,
    ResearchScopeService,
    category_context,
    public_research_scope_payload,
)


def _status_loaders(
    *,
    suitability: object | None = None,
    allocation: object | None = None,
):
    return ResearchScopeService(
        suitability_status_loader=lambda: suitability
        if suitability is not None
        else {
            "state": "fresh",
            "freshness": "fresh",
            "status": "ready_for_allocation",
            "hard_blocks": [],
            "constraints": [],
        },
        allocation_status_loader=lambda: allocation
        if allocation is not None
        else {
            "state": "fresh",
            "freshness": "fresh",
            "status": "range_available",
            "binding_constraints": [],
        },
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


def _all_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            item for nested in value.values() for item in _all_strings(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(item for nested in value for item in _all_strings(nested))
    return ()


def test_research_scope_v1_has_exact_closed_values() -> None:
    assert RESEARCH_SCOPE_SCHEMA_VERSION == "1"
    assert RESEARCH_SCOPE_TAXONOMY_VERSION == "1"
    assert RESEARCH_OBJECTIVES == (
        "learning",
        "capital_preservation",
        "income_stability",
        "long_term_growth",
    )
    assert RESEARCH_HORIZONS == ("short_term", "medium_term", "long_term")
    assert PRODUCT_CATEGORIES == (
        "money_market",
        "pure_bond",
        "bond_plus",
        "broad_index",
        "diversified_active_equity",
        "sector_theme",
    )
    assert category_context(None) == {
        "selected": None,
        "choices": [
            {
                "value": "money_market",
                "meaning": (
                    "products whose authenticated legal type or mandate is money-market"
                ),
            },
            {
                "value": "pure_bond",
                "meaning": (
                    "bond products whose authenticated mandate excludes equity allocation"
                ),
            },
            {
                "value": "bond_plus",
                "meaning": (
                    "bond-oriented products whose authenticated mandate permits equity, "
                    "convertible, or other risk assets"
                ),
            },
            {
                "value": "broad_index",
                "meaning": (
                    "passive or linked products tracking a broad-market, non-sector index"
                ),
            },
            {
                "value": "diversified_active_equity",
                "meaning": (
                    "active equity or hybrid products without authenticated sector/theme "
                    "concentration"
                ),
            },
            {
                "value": "sector_theme",
                "meaning": (
                    "products with authenticated mandate or benchmark evidence of "
                    "sector/theme concentration; name alone is insufficient"
                ),
            },
        ],
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("objective", "learn\u0456ng"),
        ("objective", "unknown"),
        ("objective", True),
        ("horizon", 1),
        ("horizon", "LONG_TERM"),
        ("product_category", False),
        ("product_category", "broad-index"),
    ),
)
def test_research_scope_rejects_noncanonical_choices_before_loading(
    field: str,
    value: object,
) -> None:
    calls: list[str] = []
    service = ResearchScopeService(
        suitability_status_loader=lambda: calls.append("suitability") or {},
        allocation_status_loader=lambda: calls.append("allocation") or {},
    )
    values = {
        "objective": "learning",
        "horizon": "long_term",
        "product_category": "broad_index",
    }
    values[field] = value

    with pytest.raises(ValueError, match=field.replace("_", " ")):
        service.form(**values)  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.parametrize("missing", ("suitability", "allocation"))
def test_research_scope_requires_callable_status_loaders(missing: str) -> None:
    values = {
        "suitability_status_loader": lambda: {},
        "allocation_status_loader": lambda: {},
    }
    values[f"{missing}_status_loader"] = None

    with pytest.raises(ValueError, match=f"{missing} status loader"):
        ResearchScopeService(**values)  # type: ignore[arg-type]


def test_missing_choices_are_explicit_and_never_guessed() -> None:
    result = _status_loaders().form(
        objective=None,
        horizon=None,
        product_category=None,
    )
    payload = public_research_scope_payload(result)

    assert result.missing_inputs == (
        "objective_required",
        "horizon_required",
        "product_category_required",
    )
    assert payload["request"] == {
        "objective": None,
        "horizon": None,
        "product_category": None,
    }
    assert payload["product_category_context"] == category_context(None)
    assert payload["product_category_context"]["selected"] is None  # type: ignore[index]
    assert [
        choice["value"]
        for choice in payload["product_category_context"]["choices"]  # type: ignore[index]
    ] == list(PRODUCT_CATEGORIES)
    assert payload["research_scope"]["objective_context"] == {  # type: ignore[index]
        "selected": None,
        "choices": [
            {
                "value": "learning",
                "meaning": "education and product understanding only",
            },
            {
                "value": "capital_preservation",
                "meaning": (
                    "study of principal-volatility and liquidity risks; never a "
                    "capital guarantee"
                ),
            },
            {
                "value": "income_stability",
                "meaning": (
                    "study of distribution and income-stability evidence; never "
                    "promised income"
                ),
            },
            {
                "value": "long_term_growth",
                "meaning": (
                    "study of long-horizon capital-growth evidence; never a "
                    "suitability conclusion"
                ),
            },
        ],
    }
    assert payload["research_scope"]["horizon_context"] == {  # type: ignore[index]
        "selected": None,
        "choices": [
            {"value": "short_term", "meaning": "less than 1 year"},
            {
                "value": "medium_term",
                "meaning": "at least 1 year and no more than 3 years",
            },
            {"value": "long_term", "meaning": "more than 3 years"},
        ],
    }


def test_partial_scope_preserves_selection_and_exposes_omitted_closed_choice() -> None:
    payload = public_research_scope_payload(
        _status_loaders().form(
            objective="learning",
            horizon=None,
            product_category="broad_index",
        )
    )
    scope = payload["research_scope"]

    assert scope["objective_context"]["selected"] == "learning"  # type: ignore[index]
    assert [
        item["value"] for item in scope["objective_context"]["choices"]  # type: ignore[index]
    ] == list(RESEARCH_OBJECTIVES)
    assert scope["horizon_context"]["selected"] is None  # type: ignore[index]
    assert [
        item["value"] for item in scope["horizon_context"]["choices"]  # type: ignore[index]
    ] == list(RESEARCH_HORIZONS)
    assert payload["product_category_context"]["selected"] == "broad_index"  # type: ignore[index]
    assert payload["missing_inputs"] == ["horizon_required"]


@pytest.mark.parametrize(
    ("suitability", "allocation", "expected_warning"),
    (
        (
            {
                "state": "fresh",
                "freshness": "fresh",
                "status": "blocked",
                "hard_blocks": ["emergency_reserve_shortfall"],
                "constraints": ["monthly_ceiling_constrained"],
            },
            {"state": "missing", "freshness": "missing"},
            "emergency_reserve_shortfall",
        ),
        (
            {"state": "stale", "freshness": "stale", "status": "constrained"},
            {"state": "stale", "freshness": "stale", "status": "range_available"},
            "suitability_freshness_stale",
        ),
        (
            {"state": "missing", "freshness": "missing"},
            {"state": "transient", "freshness": "transient"},
            "allocation_freshness_transient",
        ),
    ),
)
def test_nonfresh_or_blocked_gates_never_erase_or_authorize_research_scope(
    suitability: object,
    allocation: object,
    expected_warning: str,
) -> None:
    result = _status_loaders(
        suitability=suitability,
        allocation=allocation,
    ).form(
        objective="long_term_growth",
        horizon="long_term",
        product_category="broad_index",
    )
    payload = public_research_scope_payload(result)

    assert result.request == ResearchScopeRequest(
        objective="long_term_growth",
        horizon="long_term",
        product_category="broad_index",
    )
    assert payload["research_scope"] == {
        "objective": "long_term_growth",
        "horizon": "long_term",
        "product_category": "broad_index",
        "risk_increase_conclusion_allowed": False,
        "objective_context": {
            "selected": "long_term_growth",
            "choices": [
                {
                    "value": value,
                    "meaning": meaning,
                }
                for value, meaning in (
                    ("learning", "education and product understanding only"),
                    (
                        "capital_preservation",
                        "study of principal-volatility and liquidity risks; never a "
                        "capital guarantee",
                    ),
                    (
                        "income_stability",
                        "study of distribution and income-stability evidence; never "
                        "promised income",
                    ),
                    (
                        "long_term_growth",
                        "study of long-horizon capital-growth evidence; never a "
                        "suitability conclusion",
                    ),
                )
            ],
        },
        "horizon_context": {
            "selected": "long_term",
            "choices": [
                {"value": "short_term", "meaning": "less than 1 year"},
                {
                    "value": "medium_term",
                    "meaning": "at least 1 year and no more than 3 years",
                },
                {"value": "long_term", "meaning": "more than 3 years"},
            ],
        },
    }
    assert payload["action_boundary"] == {
        "action_maturity": "evidence_only",
        "action_authorized": False,
        "exact_amount_available": False,
        "automatic_trade": False,
    }
    assert expected_warning in payload["warnings"]


def test_each_loader_is_called_exactly_once_and_failure_is_local() -> None:
    calls = {"suitability": 0, "allocation": 0}

    def suitability_loader():
        calls["suitability"] += 1
        raise RuntimeError("private owner detail")

    def allocation_loader():
        calls["allocation"] += 1
        return {
            "state": "fresh",
            "freshness": "fresh",
            "status": "range_available",
        }

    result = ResearchScopeService(
        suitability_status_loader=suitability_loader,
        allocation_status_loader=allocation_loader,
    ).form(
        objective="learning",
        horizon="short_term",
        product_category="money_market",
    )

    assert calls == {"suitability": 1, "allocation": 1}
    assert result.personal_gate.suitability_state == "transient"
    assert result.personal_gate.suitability_freshness == "transient"
    assert result.personal_gate.allocation_state == "fresh"
    assert "suitability_status_unavailable" in result.warnings
    assert "private owner detail" not in repr(result)


@pytest.mark.parametrize("interrupt", (KeyboardInterrupt, SystemExit))
def test_process_control_exceptions_are_not_swallowed(interrupt: type[BaseException]) -> None:
    def suitability_loader():
        raise interrupt()

    service = ResearchScopeService(
        suitability_status_loader=suitability_loader,
        allocation_status_loader=lambda: {},
    )

    with pytest.raises(interrupt):
        service.form(
            objective="learning",
            horizon="short_term",
            product_category="money_market",
        )


def test_records_are_exact_frozen_and_validate_fixed_action_boundary() -> None:
    result = _status_loaders().form(
        objective="learning",
        horizon="medium_term",
        product_category="pure_bond",
    )

    result.validate()
    with pytest.raises(FrozenInstanceError):
        result.action_authorized = True  # type: ignore[misc]
    with pytest.raises(ValueError, match="action boundary"):
        replace(result, action_authorized=True).validate()
    with pytest.raises(ValueError, match="request must be exact"):
        replace(result, request=object()).validate()  # type: ignore[arg-type]


def test_public_payload_is_exact_nonrecommending_and_recursively_private() -> None:
    result = _status_loaders().form(
        objective="learning",
        horizon="long_term",
        product_category="sector_theme",
    )
    payload = public_research_scope_payload(result)

    assert set(payload) == {
        "contract",
        "request",
        "research_scope",
        "product_category_context",
        "personal_gate",
        "candidate_formation",
        "candidate_source_contract",
        "missing_inputs",
        "warnings",
        "action_boundary",
    }
    assert payload["candidate_formation"] == {
        "status": "research_scope_only",
        "candidate_code_discovery": "not_implemented",
    }
    assert payload["candidate_source_contract"] == {
        "allowed_sources": [
            "owner_supplied_confirmed_code_or_name",
            "separately_approved_unranked_bounded_directory",
            "owner_selected_current_holding",
        ],
        "ambiguous_name_action": "manual_supplementation",
        "ranking_evidence_allowed": False,
    }
    forbidden_keys = {
        "account_title",
        "amount",
        "asset",
        "cost",
        "debt",
        "income",
        "monthly_income",
        "profile",
        "reserve",
        "shares",
        "total_value",
        "target_weight",
        "rank",
        "score",
        "winner",
        "recommended",
    }
    assert _all_keys(payload).isdisjoint(forbidden_keys)
    assert not any(re.search(r"(?<!\d)\d{6}(?!\d)", value) for value in _all_strings(payload))
