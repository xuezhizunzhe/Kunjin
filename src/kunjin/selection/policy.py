from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from typing import Dict, Optional, Tuple

from kunjin.decision.models import canonical_json_bytes
from kunjin.selection.models import (
    CandidateReview,
    ComparabilityEvidence,
    PersonalGateEvidence,
    validate_candidate_codes,
)

SHORTLIST_POLICY_V1_EFFECTIVE_AT = datetime(2026, 7, 19, tzinfo=timezone.utc)
SHORTLIST_POLICY_V1_GOLDEN_CHECKSUM = (
    "c7da201cdce7828f75ca0e5ab33287f0a2596fbd75a4c2ac6dfbbd444878051e"
)


def _pair_states(
    comparability: Tuple[ComparabilityEvidence, ...],
) -> Dict[frozenset, str]:
    states: Dict[frozenset, str] = {}
    for item in comparability:
        item.validate()
        key = frozenset((item.left_fund_code, item.right_fund_code))
        if key in states:
            raise ValueError("shortlist comparability pairs must be unique")
        states[key] = item.state
    return states


def _all_pairs_have_state(
    codes: Tuple[str, ...],
    pair_states: Dict[frozenset, str],
    state: str,
) -> bool:
    return all(
        pair_states.get(frozenset((left, right))) == state
        for left, right in combinations(codes, 2)
    )


def personal_gate_passes(personal_gate: PersonalGateEvidence) -> bool:
    return (
        personal_gate.suitability_state == "fresh"
        and personal_gate.suitability_freshness == "fresh"
        and personal_gate.suitability_status in {"constrained", "ready_for_allocation"}
        and not personal_gate.blocking_codes
        and personal_gate.allocation_state == "fresh"
        and personal_gate.allocation_freshness == "fresh"
        and personal_gate.allocation_status == "range_available"
    )


def _candidate_passes(candidate: CandidateReview) -> bool:
    return (
        candidate.d1_evidence_status == "verified"
        and candidate.mapped_asset_layer is not None
        and candidate.portfolio_impact_state == "usable"
        and not candidate.blocking_codes
        and not candidate.conflicts
    )


def evaluate_shortlist_state(
    *,
    candidate_codes: Tuple[str, ...],
    has_usable_common_dimension: bool,
    comparability: Tuple[ComparabilityEvidence, ...],
    candidate_reviews: Tuple[CandidateReview, ...],
    personal_gate: PersonalGateEvidence,
) -> Tuple[str, Tuple[str, ...]]:
    codes = validate_candidate_codes(candidate_codes)
    if type(candidate_codes) is not tuple:
        raise ValueError("candidate codes must be an exact tuple")
    if type(has_usable_common_dimension) is not bool:
        raise ValueError("usable common dimension flag must be an exact bool")
    if type(comparability) is not tuple:
        raise ValueError("comparability evidence must be an exact tuple")
    if type(candidate_reviews) is not tuple:
        raise ValueError("candidate reviews must be an exact tuple")
    if type(personal_gate) is not PersonalGateEvidence:
        raise ValueError("personal gate evidence must be exact")
    personal_gate.validate()
    review_codes = []
    for candidate in candidate_reviews:
        if type(candidate) is not CandidateReview:
            raise ValueError("candidate reviews must use exact records")
        candidate.validate()
        review_codes.append(candidate.fund_code)
    if tuple(review_codes) != codes:
        raise ValueError("candidate reviews must close in request order")
    pair_states = _pair_states(comparability)
    expected_pairs = {
        frozenset((left, right)) for left, right in combinations(codes, 2)
    }
    if set(pair_states) != expected_pairs:
        raise ValueError("comparability evidence must close over every request pair")

    if (
        not has_usable_common_dimension
        or all(
            candidate.evidence_state == "insufficient_data"
            for candidate in candidate_reviews
        )
        or all(candidate.d1_evidence_status is None for candidate in candidate_reviews)
    ):
        return "insufficient_data", ()

    mapped_by_layer: Dict[str, Tuple[str, ...]] = {}
    for layer in ("diversified_equity", "high_quality_fixed_income"):
        mapped_by_layer[layer] = tuple(
            candidate.fund_code
            for candidate in candidate_reviews
            if candidate.d1_evidence_status == "verified"
            and candidate.mapped_asset_layer == layer
        )
    mapped_codes = tuple(
        candidate.fund_code
        for candidate in candidate_reviews
        if candidate.d1_evidence_status == "verified"
        and candidate.mapped_asset_layer is not None
    )
    common_mapped_groups = tuple(
        group for group in mapped_by_layer.values() if len(group) >= 2
    )
    if len(mapped_codes) >= 2 and not common_mapped_groups:
        return "not_comparable", ()
    if common_mapped_groups and all(
        _all_pairs_have_state(group, pair_states, "not_comparable")
        for group in common_mapped_groups
    ):
        return "not_comparable", ()
    if all(state == "not_comparable" for state in pair_states.values()):
        return "not_comparable", ()

    passing_groups = []
    for layer in ("diversified_equity", "high_quality_fixed_income"):
        group = tuple(
            candidate.fund_code
            for candidate in candidate_reviews
            if candidate.mapped_asset_layer == layer and _candidate_passes(candidate)
        )
        if len(group) >= 2 and _all_pairs_have_state(group, pair_states, "comparable"):
            passing_groups.append(group)
    if len(passing_groups) != 1 or not personal_gate_passes(personal_gate):
        return "relative_tradeoffs_only", ()
    return "conditional_shortlist", passing_groups[0]


@dataclass(frozen=True)
class ShortlistPolicyV1:
    version: str = "1"
    effective_at: datetime = SHORTLIST_POLICY_V1_EFFECTIVE_AT

    def validate(self) -> None:
        if type(self) is not ShortlistPolicyV1 or set(vars(self)) != {"version", "effective_at"}:
            raise ValueError("shortlist policy V1 must be an exact ShortlistPolicyV1")
        if self != ShortlistPolicyV1():
            raise ValueError("shortlist policy V1 must be canonical")
        if type(self.version) is not str or self.version != "1":
            raise ValueError("shortlist policy V1 version must be exactly '1'")
        if (
            type(self.effective_at) is not datetime
            or self.effective_at.tzinfo is not timezone.utc
            or self.effective_at != SHORTLIST_POLICY_V1_EFFECTIVE_AT
        ):
            raise ValueError("shortlist policy V1 effective-at must use canonical UTC")

    def map_asset_layer(
        self,
        *,
        evidence_status: str,
        risk_bucket: str,
        portfolio_role: str,
    ) -> Tuple[Optional[str], str]:
        self.validate()
        if evidence_status != "verified":
            return None, "d1_evidence_not_verified"
        if portfolio_role == "not_eligible":
            return None, "d1_portfolio_role_not_eligible"
        if risk_bucket == "high_quality_fixed_income":
            return "high_quality_fixed_income", "mapped_verified_d1_bucket"
        if risk_bucket == "diversified_equity":
            return "diversified_equity", "mapped_verified_d1_bucket"
        if risk_bucket == "cash_like_candidate":
            return None, "cash_like_is_not_protected_cash"
        return None, "d1_bucket_has_no_phase4_mapping"

    def evaluate(
        self,
        *,
        candidate_codes: Tuple[str, ...],
        has_usable_common_dimension: bool,
        comparability: Tuple[ComparabilityEvidence, ...],
        candidate_reviews: Tuple[CandidateReview, ...],
        personal_gate: PersonalGateEvidence,
    ) -> Tuple[str, Tuple[str, ...]]:
        self.validate()
        return evaluate_shortlist_state(
            candidate_codes=candidate_codes,
            has_usable_common_dimension=has_usable_common_dimension,
            comparability=comparability,
            candidate_reviews=candidate_reviews,
            personal_gate=personal_gate,
        )

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "effective_at": self.effective_at,
            "layer_mappings": {
                "cash_like_candidate": None,
                "concentrated_equity": None,
                "diversified_equity": "diversified_equity",
                "high_quality_fixed_income": "high_quality_fixed_income",
                "hybrid_risk": None,
                "unclassified": None,
            },
            "minimum_mutually_comparable_members": 2,
            "not_eligible_mapping": None,
            "ranking_enabled": False,
            "required_allocation_freshness": "fresh",
            "required_allocation_status": "range_available",
            "required_candidate_blocking_codes": [],
            "required_candidate_conflicts": [],
            "required_common_mapped_layer": True,
            "required_d1_evidence_status": "verified",
            "required_portfolio_impact_state": "usable",
            "required_suitability_freshness": "fresh",
            "required_suitability_statuses": [
                "constrained",
                "ready_for_allocation",
            ],
            "state_precedence": [
                "insufficient_data",
                "not_comparable",
                "relative_tradeoffs_only",
                "conditional_shortlist",
            ],
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        checksum = hashlib.sha256(self.canonical_json()).hexdigest()
        if self.version == "1" and checksum != SHORTLIST_POLICY_V1_GOLDEN_CHECKSUM:
            raise ValueError("ShortlistPolicy V1 canonical checksum drifted")
        return checksum
