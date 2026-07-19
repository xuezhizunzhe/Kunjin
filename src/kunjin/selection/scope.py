from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields

from kunjin.selection.models import PersonalGateEvidence
from kunjin.selection.research import public_personal_gate_payload
from kunjin.selection.service import project_personal_gate

RESEARCH_SCOPE_SCHEMA_VERSION = "1"
RESEARCH_SCOPE_TAXONOMY_VERSION = "1"

RESEARCH_OBJECTIVES = (
    "learning",
    "capital_preservation",
    "income_stability",
    "long_term_growth",
)
RESEARCH_HORIZONS = ("short_term", "medium_term", "long_term")
PRODUCT_CATEGORIES = (
    "money_market",
    "pure_bond",
    "bond_plus",
    "broad_index",
    "diversified_active_equity",
    "sector_theme",
)

_OBJECTIVE_MEANINGS = {
    "learning": "education and product understanding only",
    "capital_preservation": (
        "study of principal-volatility and liquidity risks; never a capital guarantee"
    ),
    "income_stability": (
        "study of distribution and income-stability evidence; never promised income"
    ),
    "long_term_growth": (
        "study of long-horizon capital-growth evidence; never a suitability conclusion"
    ),
}
_HORIZON_MEANINGS = {
    "short_term": "less than 1 year",
    "medium_term": "at least 1 year and no more than 3 years",
    "long_term": "more than 3 years",
}
_PRODUCT_CATEGORY_MEANINGS = {
    "money_market": (
        "products whose authenticated legal type or mandate is money-market"
    ),
    "pure_bond": (
        "bond products whose authenticated mandate excludes equity allocation"
    ),
    "bond_plus": (
        "bond-oriented products whose authenticated mandate permits equity, "
        "convertible, or other risk assets"
    ),
    "broad_index": (
        "passive or linked products tracking a broad-market, non-sector index"
    ),
    "diversified_active_equity": (
        "active equity or hybrid products without authenticated sector/theme "
        "concentration"
    ),
    "sector_theme": (
        "products with authenticated mandate or benchmark evidence of sector/theme "
        "concentration; name alone is insufficient"
    ),
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$", flags=re.ASCII)
_MISSING_INPUT_ORDER = (
    "objective_required",
    "horizon_required",
    "product_category_required",
)


def _exact_record(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be exact")
    if set(vars(value)) != {item.name for item in fields(expected)}:
        raise ValueError(f"{name} exact state is invalid")


def _choice(
    value: object,
    allowed: tuple[str, ...],
    name: str,
) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        raise ValueError(f"{name} is unsupported")
    return value


def _stable_codes(values: object, name: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    if any(type(value) is not str or _IDENTIFIER.fullmatch(value) is None for value in values):
        raise ValueError(f"{name} must contain stable identifiers")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and ascending")
    return values


@dataclass(frozen=True)
class ResearchScopeRequest:
    objective: str | None
    horizon: str | None
    product_category: str | None

    def validate(self) -> None:
        _exact_record(self, ResearchScopeRequest, "research scope request")
        _choice(self.objective, RESEARCH_OBJECTIVES, "research objective")
        _choice(self.horizon, RESEARCH_HORIZONS, "research horizon")
        _choice(self.product_category, PRODUCT_CATEGORIES, "product category")


@dataclass(frozen=True)
class ResearchScopeResult:
    request: ResearchScopeRequest
    personal_gate: PersonalGateEvidence
    missing_inputs: tuple[str, ...]
    warnings: tuple[str, ...]
    research_scope_schema_version: str = RESEARCH_SCOPE_SCHEMA_VERSION
    research_scope_taxonomy_version: str = RESEARCH_SCOPE_TAXONOMY_VERSION
    action_maturity: str = "evidence_only"
    action_authorized: bool = False
    exact_amount_available: bool = False
    automatic_trade: bool = False

    def validate(self) -> None:
        _exact_record(self, ResearchScopeResult, "research scope result")
        if type(self.request) is not ResearchScopeRequest:
            raise ValueError("research scope request must be exact")
        self.request.validate()
        if type(self.personal_gate) is not PersonalGateEvidence:
            raise ValueError("research scope personal gate must be exact")
        self.personal_gate.validate()
        expected_missing = tuple(
            code
            for code, value in zip(
                _MISSING_INPUT_ORDER,
                (
                    self.request.objective,
                    self.request.horizon,
                    self.request.product_category,
                ),
            )
            if value is None
        )
        if type(self.missing_inputs) is not tuple or self.missing_inputs != expected_missing:
            raise ValueError("research scope missing inputs must match omitted choices")
        _stable_codes(self.warnings, "research scope warnings")
        if (
            type(self.research_scope_schema_version) is not str
            or self.research_scope_schema_version != RESEARCH_SCOPE_SCHEMA_VERSION
            or type(self.research_scope_taxonomy_version) is not str
            or self.research_scope_taxonomy_version != RESEARCH_SCOPE_TAXONOMY_VERSION
        ):
            raise ValueError("research scope contract versions are invalid")
        if (
            type(self.action_maturity) is not str
            or self.action_maturity != "evidence_only"
            or self.action_authorized is not False
            or self.exact_amount_available is not False
            or self.automatic_trade is not False
        ):
            raise ValueError("research scope action boundary is invalid")


def _closed_context(
    selected: str | None,
    values: tuple[str, ...],
    meanings: Mapping[str, str],
    name: str,
) -> dict[str, object]:
    selected = _choice(selected, values, name)
    return {
        "selected": selected,
        "choices": [
            {"value": value, "meaning": meanings[value]}
            for value in values
        ],
    }


def category_context(product_category: str | None) -> dict[str, object]:
    return _closed_context(
        product_category,
        PRODUCT_CATEGORIES,
        _PRODUCT_CATEGORY_MEANINGS,
        "product category",
    )


def _gate_warnings(gate: PersonalGateEvidence) -> set[str]:
    warnings = {*gate.blocking_codes, *gate.constraint_codes}
    for prefix, state, freshness, status in (
        (
            "suitability",
            gate.suitability_state,
            gate.suitability_freshness,
            gate.suitability_status,
        ),
        (
            "allocation",
            gate.allocation_state,
            gate.allocation_freshness,
            gate.allocation_status,
        ),
    ):
        if state != "fresh":
            warnings.add(f"{prefix}_state_{state}")
        if freshness != "fresh":
            warnings.add(f"{prefix}_freshness_{freshness}")
        if status == "blocked":
            warnings.add(f"{prefix}_blocked")
    return warnings


class ResearchScopeService:
    def __init__(
        self,
        *,
        suitability_status_loader: Callable[[], Mapping[str, object]],
        allocation_status_loader: Callable[[], Mapping[str, object]],
    ) -> None:
        for loader, name in (
            (suitability_status_loader, "suitability status loader"),
            (allocation_status_loader, "allocation status loader"),
        ):
            if not callable(loader):
                raise ValueError(f"{name} must be callable")
        self._suitability_status_loader = suitability_status_loader
        self._allocation_status_loader = allocation_status_loader

    def form(
        self,
        *,
        objective: str | None,
        horizon: str | None,
        product_category: str | None,
    ) -> ResearchScopeResult:
        request = ResearchScopeRequest(
            objective=_choice(objective, RESEARCH_OBJECTIVES, "research objective"),
            horizon=_choice(horizon, RESEARCH_HORIZONS, "research horizon"),
            product_category=_choice(
                product_category,
                PRODUCT_CATEGORIES,
                "product category",
            ),
        )
        request.validate()

        warnings: set[str] = set()
        suitability_status = self._load_status(
            self._suitability_status_loader,
            "suitability_status_unavailable",
            warnings,
        )
        allocation_status = self._load_status(
            self._allocation_status_loader,
            "allocation_status_unavailable",
            warnings,
        )
        personal_gate = project_personal_gate(suitability_status, allocation_status)
        warnings.update(_gate_warnings(personal_gate))
        missing_inputs = tuple(
            code
            for code, value in zip(
                _MISSING_INPUT_ORDER,
                (request.objective, request.horizon, request.product_category),
            )
            if value is None
        )
        result = ResearchScopeResult(
            request=request,
            personal_gate=personal_gate,
            missing_inputs=missing_inputs,
            warnings=tuple(sorted(warnings)),
        )
        result.validate()
        return result

    @staticmethod
    def _load_status(
        loader: Callable[[], Mapping[str, object]],
        unavailable_code: str,
        warnings: set[str],
    ) -> Mapping[str, object]:
        try:
            value = loader()
            if not isinstance(value, Mapping):
                raise ValueError("status is not a mapping")
            return dict(value)
        except Exception:
            warnings.add(unavailable_code)
            return {"state": "transient", "freshness": "transient"}


def public_research_scope_payload(result: ResearchScopeResult) -> dict[str, object]:
    result.validate()
    return {
        "contract": {
            "research_scope_schema_version": result.research_scope_schema_version,
            "research_scope_taxonomy_version": result.research_scope_taxonomy_version,
        },
        "request": {
            "objective": result.request.objective,
            "horizon": result.request.horizon,
            "product_category": result.request.product_category,
        },
        "research_scope": {
            "objective": result.request.objective,
            "horizon": result.request.horizon,
            "product_category": result.request.product_category,
            "risk_increase_conclusion_allowed": False,
            "objective_context": _closed_context(
                result.request.objective,
                RESEARCH_OBJECTIVES,
                _OBJECTIVE_MEANINGS,
                "research objective",
            ),
            "horizon_context": _closed_context(
                result.request.horizon,
                RESEARCH_HORIZONS,
                _HORIZON_MEANINGS,
                "research horizon",
            ),
        },
        "product_category_context": category_context(result.request.product_category),
        "personal_gate": public_personal_gate_payload(result.personal_gate),
        "candidate_formation": {
            "status": "research_scope_only",
            "candidate_code_discovery": "not_implemented",
        },
        "candidate_source_contract": {
            "allowed_sources": [
                "owner_supplied_confirmed_code_or_name",
                "separately_approved_unranked_bounded_directory",
                "owner_selected_current_holding",
            ],
            "ambiguous_name_action": "manual_supplementation",
            "ranking_evidence_allowed": False,
        },
        "missing_inputs": list(result.missing_inputs),
        "warnings": list(result.warnings),
        "action_boundary": {
            "action_maturity": result.action_maturity,
            "action_authorized": result.action_authorized,
            "exact_amount_available": result.exact_amount_available,
            "automatic_trade": result.automatic_trade,
        },
    }


assert tuple(_OBJECTIVE_MEANINGS) == RESEARCH_OBJECTIVES
assert tuple(_HORIZON_MEANINGS) == RESEARCH_HORIZONS
assert tuple(_PRODUCT_CATEGORY_MEANINGS) == PRODUCT_CATEGORIES

__all__ = [
    "PRODUCT_CATEGORIES",
    "RESEARCH_HORIZONS",
    "RESEARCH_OBJECTIVES",
    "RESEARCH_SCOPE_SCHEMA_VERSION",
    "RESEARCH_SCOPE_TAXONOMY_VERSION",
    "ResearchScopeRequest",
    "ResearchScopeResult",
    "ResearchScopeService",
    "category_context",
    "public_research_scope_payload",
]
