from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

from kunjin.decision.models import canonical_json_bytes, validate_public_text


@dataclass(frozen=True)
class EvidenceRequirement:
    field_id: str
    decision_evidence: str
    freshness: str
    missing_or_conflict_behavior: str

    def validate(self) -> None:
        for value, name in (
            (self.field_id, "field id"),
            (self.decision_evidence, "decision evidence"),
            (self.freshness, "freshness"),
            (self.missing_or_conflict_behavior, "missing or conflict behavior"),
        ):
            validate_public_text(value, name)

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "decision_evidence": self.decision_evidence,
            "field_id": self.field_id,
            "freshness": self.freshness,
            "missing_or_conflict_behavior": self.missing_or_conflict_behavior,
        }


_REQUIREMENTS = (
    EvidenceRequirement(
        "phase_b_c",
        "authenticated current result bound to current profile and policy",
        "24 hours; invalidate on any bound input change",
        "block buy, add, switch-buy, and exact amount; preserve facts and reduce analysis",
    ),
    EvidenceRequirement(
        "personal_position",
        "successful same-request portfolio sync plus confirmed pending transactions",
        "same request",
        "block exact buy, reduce, and exit amounts; allow labeled last-observation ratios",
    ),
    EvidenceRequirement(
        "identity_active_status",
        "one tier_1 source or two independent matching structured tier_2 records",
        "7 days; invalidate immediately on newer status announcement",
        "identity or status conflict blocks every product-specific action",
    ),
    EvidenceRequirement(
        "current_manager_team",
        "one tier_1 source or two independent structured tier_2 records",
        "7 days; invalidate immediately on manager announcement",
        "one tier_2 source supports rapid research only; conflict blocks manager comparison",
    ),
    EvidenceRequirement(
        "fees_share_class_relationship",
        "tier_1 schedule or verified current channel plus one matching structured source",
        "effective period; channel discounts and limits at most 7 days",
        "block exact fee, share-class choice, and exact amount; preserve labeled overview",
    ),
    EvidenceRequirement(
        "transaction_availability_limits_cutoff",
        "same-day official or channel record, or validated private channel screenshot",
        "2 hours for current or today; same trading day otherwise",
        "block executable buy or redeem conclusion and exact amount",
    ),
    EvidenceRequirement(
        "formal_nav",
        "latest expected formal NAV under the applicable publication calendar",
        "latest expected comparable NAV day and normal publication window",
        "stale data supports dated history only and no current timing conclusion",
    ),
    EvidenceRequirement(
        "adjusted_return_correlation",
        "validated cumulative-NAV or total-return series with aligned dates and sample",
        "common end date is the latest expected comparable NAV day",
        "ambiguity is insufficient_data; never substitute NAV-level correlation",
    ),
    EvidenceRequirement(
        "holdings_industries",
        "latest statutory period with report date, publication date, and disclosure scope",
        "current until a newer report is due under the disclosure calendar",
        "preserve unknown exposure and block reassuring diversification claims",
    ),
    EvidenceRequirement(
        "fund_manager_product_announcement",
        "validated official item",
        "resolved query window plus correction and retraction check",
        "missing feed lowers coverage; unresolved official conflict blocks affected action",
    ),
    EvidenceRequirement(
        "news_media_context",
        "official original source or genuinely independent lineage; media remains attributed",
        "resolved query window; current cache at most 2 hours",
        "never independently authorizes action; conflict lowers confidence or abstains",
    ),
    EvidenceRequirement(
        "target_point_bands",
        "current versioned owner-approved policy distinct from feasible ceilings",
        "invalidate on profile, goal, or policy change",
        "block exact buy, reduce, and rebalance amount",
    ),
)


@dataclass(frozen=True)
class EvidencePolicyV1:
    version: str = "1"
    requirements: Tuple[EvidenceRequirement, ...] = _REQUIREMENTS
    classification_coverage_min_percent: Decimal = Decimal("90")
    sector_candidate_asset_coverage_min_percent: Decimal = Decimal("80")
    broad_index_candidate_asset_coverage_min_percent: Decimal = Decimal("90")
    transaction_after_lookthrough_coverage_min_percent: Decimal = Decimal("70")
    unknown_exposure_rule: str = "allocate all residual unknown exposure to every limit"
    target_requires_explicit_derivation: bool = True
    tactical_sector_cap_requires_derivation: bool = True
    tactical_sector_cap_requires_stress_loss_assumption: bool = True
    tactical_sector_cap_requires_independent_review: bool = True
    tactical_sector_cap_requires_owner_approval: bool = True
    tactical_sector_cap_requires_version_and_effective_date: bool = True
    policy_scope: str = "complete_kunjin_managed_portfolio"

    def validate(self) -> None:
        if type(self) is not EvidencePolicyV1:
            raise ValueError("evidence policy V1 subclasses are not accepted")
        if self.version != "1" or type(self.version) is not str:
            raise ValueError("evidence policy V1 version must be '1'")
        if type(self.requirements) is not tuple or self.requirements != _REQUIREMENTS:
            raise ValueError("evidence policy V1 requirements must be canonical")
        field_ids = []
        for requirement in self.requirements:
            if type(requirement) is not EvidenceRequirement:
                raise ValueError("requirements must contain exact EvidenceRequirement records")
            requirement.validate()
            field_ids.append(requirement.field_id)
        if len(field_ids) != len(set(field_ids)):
            raise ValueError("evidence policy field ids must be unique")
        for value, expected, name in (
            (self.classification_coverage_min_percent, Decimal("90"), "classification coverage"),
            (
                self.sector_candidate_asset_coverage_min_percent,
                Decimal("80"),
                "sector candidate coverage",
            ),
            (
                self.broad_index_candidate_asset_coverage_min_percent,
                Decimal("90"),
                "broad-index candidate coverage",
            ),
            (
                self.transaction_after_lookthrough_coverage_min_percent,
                Decimal("70"),
                "post-trade look-through coverage",
            ),
        ):
            if type(value) is not Decimal or not value.is_finite() or value != expected:
                raise ValueError(f"evidence policy V1 {name} must be {expected}")
        validate_public_text(self.unknown_exposure_rule, "unknown exposure rule")
        validate_public_text(self.policy_scope, "policy scope")
        for name in (
            "target_requires_explicit_derivation",
            "tactical_sector_cap_requires_derivation",
            "tactical_sector_cap_requires_stress_loss_assumption",
            "tactical_sector_cap_requires_independent_review",
            "tactical_sector_cap_requires_owner_approval",
            "tactical_sector_cap_requires_version_and_effective_date",
        ):
            if type(getattr(self, name)) is not bool or not getattr(self, name):
                raise ValueError(f"evidence policy V1 {name} must be true")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "d2_gates": {
                "broad_index_candidate_asset_coverage_min_percent": "90",
                "classification_coverage_min_percent": "90",
                "sector_candidate_asset_coverage_min_percent": "80",
                "transaction_after_lookthrough_coverage_min_percent": "70",
                "unknown_exposure_rule": self.unknown_exposure_rule,
            },
            "policy_scope": self.policy_scope,
            "requirements": [item.to_canonical_dict() for item in self.requirements],
            "target_and_cap_approval": {
                "cap_requires_derivation": self.tactical_sector_cap_requires_derivation,
                "cap_requires_independent_review": (
                    self.tactical_sector_cap_requires_independent_review
                ),
                "cap_requires_owner_approval": self.tactical_sector_cap_requires_owner_approval,
                "cap_requires_stress_loss_assumption": (
                    self.tactical_sector_cap_requires_stress_loss_assumption
                ),
                "cap_requires_version_and_effective_date": (
                    self.tactical_sector_cap_requires_version_and_effective_date
                ),
                "target_requires_explicit_derivation": self.target_requires_explicit_derivation,
            },
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_dict())

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()
