from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Tuple

from kunjin.decision.models import (
    canonical_json_bytes,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_public_text,
)


@dataclass(frozen=True)
class EvidenceRequirement:
    field_id: str
    decision_evidence: str
    freshness: str
    missing_or_conflict_behavior: str

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "evidence requirement")
        validate_identifier(self.field_id, "field id")
        for value, name in (
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


@dataclass(frozen=True)
class CoverageGate:
    formula_id: str
    numerator: str
    denominator: str
    minimum_percent: Decimal

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "coverage gate")
        validate_identifier(self.formula_id, "formula id")
        validate_public_text(self.numerator, "coverage numerator")
        validate_public_text(self.denominator, "coverage denominator")
        if (
            type(self.minimum_percent) is not Decimal
            or not self.minimum_percent.is_finite()
            or not Decimal("0") <= self.minimum_percent <= Decimal("100")
        ):
            raise ValueError("coverage minimum must be a finite percent")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "denominator": self.denominator,
            "formula_id": self.formula_id,
            "minimum_percent": format(self.minimum_percent.normalize(), "f"),
            "numerator": self.numerator,
        }


@dataclass(frozen=True)
class D2EvidencePolicy:
    classification_coverage: CoverageGate
    sector_candidate_asset_coverage: CoverageGate
    broad_index_candidate_asset_coverage: CoverageGate
    transaction_after_lookthrough_coverage: CoverageGate
    cash_excluded_from_denominators: bool
    derivatives_leverage_shorts_residual_reported_separately: bool
    unresolved_exposure_cannot_increase_coverage: bool
    fund_of_funds_lookthrough_requires_verified_inputs: bool
    test_every_applicable_limit: bool
    allocate_all_unknown_to_each_limit: bool
    unknown_exposure_consumes_capacity: bool
    insufficient_active_coverage_blocks_reassuring_conclusion: bool

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "D2 evidence policy")
        gates = (
            (self.classification_coverage, "classification_coverage", Decimal("90")),
            (
                self.sector_candidate_asset_coverage,
                "candidate_asset_coverage_sector",
                Decimal("80"),
            ),
            (
                self.broad_index_candidate_asset_coverage,
                "candidate_asset_coverage_broad_index",
                Decimal("90"),
            ),
            (
                self.transaction_after_lookthrough_coverage,
                "transaction_after_lookthrough_coverage",
                Decimal("70"),
            ),
        )
        for gate, formula_id, minimum in gates:
            if type(gate) is not CoverageGate:
                raise ValueError("D2 coverage gates must be exact CoverageGate records")
            gate.validate()
            if gate.formula_id != formula_id or gate.minimum_percent != minimum:
                raise ValueError("D2 coverage gate differs from EvidencePolicy V1")
        for field_name in (
            "cash_excluded_from_denominators",
            "derivatives_leverage_shorts_residual_reported_separately",
            "unresolved_exposure_cannot_increase_coverage",
            "fund_of_funds_lookthrough_requires_verified_inputs",
            "test_every_applicable_limit",
            "allocate_all_unknown_to_each_limit",
            "unknown_exposure_consumes_capacity",
            "insufficient_active_coverage_blocks_reassuring_conclusion",
        ):
            if getattr(self, field_name) is not True:
                raise ValueError(f"EvidencePolicy V1 {field_name} must be true")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "allocate_all_unknown_to_each_limit": self.allocate_all_unknown_to_each_limit,
            "broad_index_candidate_asset_coverage": (
                self.broad_index_candidate_asset_coverage.to_canonical_dict()
            ),
            "cash_excluded_from_denominators": self.cash_excluded_from_denominators,
            "classification_coverage": self.classification_coverage.to_canonical_dict(),
            "derivatives_leverage_shorts_residual_reported_separately": (
                self.derivatives_leverage_shorts_residual_reported_separately
            ),
            "fund_of_funds_lookthrough_requires_verified_inputs": (
                self.fund_of_funds_lookthrough_requires_verified_inputs
            ),
            "insufficient_active_coverage_blocks_reassuring_conclusion": (
                self.insufficient_active_coverage_blocks_reassuring_conclusion
            ),
            "sector_candidate_asset_coverage": (
                self.sector_candidate_asset_coverage.to_canonical_dict()
            ),
            "test_every_applicable_limit": self.test_every_applicable_limit,
            "transaction_after_lookthrough_coverage": (
                self.transaction_after_lookthrough_coverage.to_canonical_dict()
            ),
            "unknown_exposure_consumes_capacity": self.unknown_exposure_consumes_capacity,
            "unresolved_exposure_cannot_increase_coverage": (
                self.unresolved_exposure_cannot_increase_coverage
            ),
        }


@dataclass(frozen=True)
class PostTradePolicy:
    cap_scope: str
    denominator_scope: str
    requires_unlinked_account_affirmation: bool
    requires_material_holding_completeness: bool
    includes_pending_transactions: bool
    valuation_date_tolerance_days: int
    stale_or_misaligned_valuation_blocks_exact_amount: bool
    block_exact_amount_on_failure: bool
    aggregate_matching_exposure_across_all_fund_labels: bool
    split_transactions_cannot_bypass_cap: bool
    unknown_exposure_consumes_capacity: bool
    target_requires_explicit_derivation: bool
    tactical_sector_cap_requires_derivation: bool
    tactical_sector_cap_requires_stress_loss_assumption: bool
    tactical_sector_cap_requires_independent_review: bool
    tactical_sector_cap_requires_owner_approval: bool
    tactical_sector_cap_requires_version_and_effective_date: bool

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "post-trade policy")
        if self.cap_scope != "all_linked_accounts_current_and_pending":
            raise ValueError("post-trade cap scope differs from EvidencePolicy V1")
        if self.denominator_scope != "complete_kunjin_managed_portfolio":
            raise ValueError("post-trade denominator scope differs from EvidencePolicy V1")
        if type(self.valuation_date_tolerance_days) is not int:
            raise ValueError("valuation date tolerance must be an exact integer")
        if self.valuation_date_tolerance_days != 0:
            raise ValueError("EvidencePolicy V1 requires same-date valuations")
        for field_name in (
            "requires_unlinked_account_affirmation",
            "requires_material_holding_completeness",
            "includes_pending_transactions",
            "stale_or_misaligned_valuation_blocks_exact_amount",
            "block_exact_amount_on_failure",
            "aggregate_matching_exposure_across_all_fund_labels",
            "split_transactions_cannot_bypass_cap",
            "unknown_exposure_consumes_capacity",
            "target_requires_explicit_derivation",
            "tactical_sector_cap_requires_derivation",
            "tactical_sector_cap_requires_stress_loss_assumption",
            "tactical_sector_cap_requires_independent_review",
            "tactical_sector_cap_requires_owner_approval",
            "tactical_sector_cap_requires_version_and_effective_date",
        ):
            if getattr(self, field_name) is not True:
                raise ValueError(f"EvidencePolicy V1 {field_name} must be true")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "aggregate_matching_exposure_across_all_fund_labels": (
                self.aggregate_matching_exposure_across_all_fund_labels
            ),
            "block_exact_amount_on_failure": self.block_exact_amount_on_failure,
            "cap_scope": self.cap_scope,
            "denominator_scope": self.denominator_scope,
            "includes_pending_transactions": self.includes_pending_transactions,
            "requires_material_holding_completeness": (
                self.requires_material_holding_completeness
            ),
            "requires_unlinked_account_affirmation": (
                self.requires_unlinked_account_affirmation
            ),
            "split_transactions_cannot_bypass_cap": self.split_transactions_cannot_bypass_cap,
            "stale_or_misaligned_valuation_blocks_exact_amount": (
                self.stale_or_misaligned_valuation_blocks_exact_amount
            ),
            "tactical_sector_cap_requires_derivation": (
                self.tactical_sector_cap_requires_derivation
            ),
            "tactical_sector_cap_requires_independent_review": (
                self.tactical_sector_cap_requires_independent_review
            ),
            "tactical_sector_cap_requires_owner_approval": (
                self.tactical_sector_cap_requires_owner_approval
            ),
            "tactical_sector_cap_requires_stress_loss_assumption": (
                self.tactical_sector_cap_requires_stress_loss_assumption
            ),
            "tactical_sector_cap_requires_version_and_effective_date": (
                self.tactical_sector_cap_requires_version_and_effective_date
            ),
            "target_requires_explicit_derivation": self.target_requires_explicit_derivation,
            "unknown_exposure_consumes_capacity": self.unknown_exposure_consumes_capacity,
            "valuation_date_tolerance_days": self.valuation_date_tolerance_days,
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
        "effective period with completed newer-announcement check",
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
        "resolved query window plus completed correction and retraction check",
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

_D2_POLICY = D2EvidencePolicy(
    classification_coverage=CoverageGate(
        "classification_coverage",
        "classified current non-cash fund market value",
        "total current non-cash fund market value",
        Decimal("90"),
    ),
    sector_candidate_asset_coverage=CoverageGate(
        "candidate_asset_coverage_sector",
        "sum of verified disclosed or constituent asset weights",
        "100 percent of candidate net assets",
        Decimal("80"),
    ),
    broad_index_candidate_asset_coverage=CoverageGate(
        "candidate_asset_coverage_broad_index",
        "sum of verified disclosed or constituent asset weights",
        "100 percent of candidate net assets",
        Decimal("90"),
    ),
    transaction_after_lookthrough_coverage=CoverageGate(
        "transaction_after_lookthrough_coverage",
        "sum of transaction-after fund market value times verified internal coverage",
        "total transaction-after non-cash fund market value",
        Decimal("70"),
    ),
    cash_excluded_from_denominators=True,
    derivatives_leverage_shorts_residual_reported_separately=True,
    unresolved_exposure_cannot_increase_coverage=True,
    fund_of_funds_lookthrough_requires_verified_inputs=True,
    test_every_applicable_limit=True,
    allocate_all_unknown_to_each_limit=True,
    unknown_exposure_consumes_capacity=True,
    insufficient_active_coverage_blocks_reassuring_conclusion=True,
)

_POST_TRADE_POLICY = PostTradePolicy(
    cap_scope="all_linked_accounts_current_and_pending",
    denominator_scope="complete_kunjin_managed_portfolio",
    requires_unlinked_account_affirmation=True,
    requires_material_holding_completeness=True,
    includes_pending_transactions=True,
    valuation_date_tolerance_days=0,
    stale_or_misaligned_valuation_blocks_exact_amount=True,
    block_exact_amount_on_failure=True,
    aggregate_matching_exposure_across_all_fund_labels=True,
    split_transactions_cannot_bypass_cap=True,
    unknown_exposure_consumes_capacity=True,
    target_requires_explicit_derivation=True,
    tactical_sector_cap_requires_derivation=True,
    tactical_sector_cap_requires_stress_loss_assumption=True,
    tactical_sector_cap_requires_independent_review=True,
    tactical_sector_cap_requires_owner_approval=True,
    tactical_sector_cap_requires_version_and_effective_date=True,
)


@dataclass(frozen=True)
class EvidencePolicyV1:
    version: str = "1"
    requirements: Tuple[EvidenceRequirement, ...] = _REQUIREMENTS
    d2: D2EvidencePolicy = _D2_POLICY
    post_trade: PostTradePolicy = _POST_TRADE_POLICY

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "evidence policy V1")
        if type(self) is not EvidencePolicyV1:
            raise ValueError("evidence policy V1 subclasses are not accepted")
        if type(self.version) is not str or self.version != "1":
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
        if type(self.d2) is not D2EvidencePolicy or self.d2 != _D2_POLICY:
            raise ValueError("evidence policy V1 D2 rules must be canonical")
        self.d2.validate()
        if type(self.post_trade) is not PostTradePolicy or self.post_trade != _POST_TRADE_POLICY:
            raise ValueError("evidence policy V1 post-trade rules must be canonical")
        self.post_trade.validate()

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "d2": self.d2.to_canonical_dict(),
            "post_trade": self.post_trade.to_canonical_dict(),
            "requirements": [item.to_canonical_dict() for item in self.requirements],
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


EVIDENCE_POLICY_V1_CHECKSUM = (
    "bafaf188c31ce4912485856369397c423dece2dcc48ac3e19273845763dd1428"
)
