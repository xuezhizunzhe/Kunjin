from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional, Tuple

from kunjin.allocation.models import (
    REGION_INEQUALITIES,
    AggregateAllocationInputs,
    AllocationBlockCode,
    AllocationConstraintCode,
    AllocationExactResult,
    AllocationProfileConflictCode,
    AllocationResult,
    AllocationSafeSummary,
    AllocationSleeveKind,
    AllocationStatus,
    AssetLayer,
    AssignedSleeveDetail,
    GoalFundingDetail,
    GoalFundingState,
    ObligationFundingDetail,
    PermittedRegion,
    _safe_add,
    _safe_divide,
    _safe_multiply,
    _safe_quantize,
    _safe_ratio_floor,
    _safe_subtract,
    _safe_sum,
    horizon_equity_ceiling_v1,
)
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
    Debt,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    ProfileConflictCode,
    RiskReaction,
)

_ZERO = Decimal("0")
_PHASE_B_CONSTRAINT_MAP = {
    ConstraintReason.NEAR_TERM_OBLIGATION_GAP: (AllocationConstraintCode.NEAR_TERM_OBLIGATION_GAP),
    ConstraintReason.NEAR_TERM_GOAL_GAP: AllocationConstraintCode.NEAR_TERM_GOAL_GAP,
    ConstraintReason.MONTHLY_CEILING_CONSTRAINED: (
        AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED
    ),
}


@dataclass(frozen=True)
class AllocationCapitalInputs:
    assessment_date: date
    total_financial_assets: Decimal
    liquid_protection_assets: Decimal
    verified_emergency_reserve: Decimal
    minimum_operating_cash: Decimal
    protected_short_term_assigned: Decimal
    protected_liquid_claims: Decimal
    investable_stock_assets: Decimal
    monthly_discretionary_allocation_ceiling: Decimal
    maximum_tolerable_loss: Decimal
    maximum_tolerable_drawdown: Decimal
    residual_horizon_date: Optional[date]
    goal_funding_details: Tuple[GoalFundingDetail, ...]
    obligation_funding_details: Tuple[ObligationFundingDetail, ...]
    assigned_sleeves: Tuple[AssignedSleeveDetail, ...]

    def validate(self) -> None:
        _validate_declared_state(self, AllocationCapitalInputs, "capital")
        if type(self.assessment_date) is not date:
            raise ValueError("assessment_date must be an exact date")
        for value, name in (
            (self.total_financial_assets, "total financial assets"),
            (self.liquid_protection_assets, "liquid protection assets"),
            (self.verified_emergency_reserve, "verified emergency reserve"),
            (self.minimum_operating_cash, "minimum operating cash"),
            (self.protected_short_term_assigned, "protected short-term assigned"),
            (self.protected_liquid_claims, "protected liquid claims"),
            (self.investable_stock_assets, "investable stock assets"),
            (
                self.monthly_discretionary_allocation_ceiling,
                "monthly discretionary allocation ceiling",
            ),
            (self.maximum_tolerable_loss, "maximum tolerable loss"),
        ):
            _validate_money(value, name)
        if self.liquid_protection_assets > self.total_financial_assets:
            raise ValueError("liquid protection assets cannot exceed total financial assets")
        if self.protected_liquid_claims > self.liquid_protection_assets:
            raise ValueError("protected liquid claims cannot exceed liquid protection assets")
        if self.protected_short_term_assigned > self.protected_liquid_claims:
            raise ValueError("protected short-term assigned cannot exceed protected claims")
        if self.investable_stock_assets != max(
            _ZERO,
            _safe_subtract(
                self.total_financial_assets,
                self.protected_liquid_claims,
                "investable stock assets",
            ),
        ):
            raise ValueError("investable stock must equal total assets less protected claims")
        if (
            type(self.maximum_tolerable_drawdown) is not Decimal
            or not self.maximum_tolerable_drawdown.is_finite()
            or not _ZERO <= self.maximum_tolerable_drawdown <= Decimal("1")
        ):
            raise ValueError("maximum tolerable drawdown must be a Decimal fraction")
        if self.residual_horizon_date is not None and type(self.residual_horizon_date) is not date:
            raise ValueError("residual horizon must be a date or None")
        if type(self.goal_funding_details) is not tuple:
            raise ValueError("goal funding details must be a tuple")
        for detail in self.goal_funding_details:
            if type(detail) is not GoalFundingDetail:
                raise ValueError("goal funding details must contain exact goal details")
            detail.validate()
        if type(self.obligation_funding_details) is not tuple:
            raise ValueError("obligation funding details must be a tuple")
        for detail in self.obligation_funding_details:
            if type(detail) is not ObligationFundingDetail:
                raise ValueError("obligation funding details must contain exact obligation details")
            detail.validate()
        self._validate_horizon_dates()
        self._validate_protected_claims()
        self._validate_sleeves()

    def _validate_protected_claims(self) -> None:
        short_term_cutoff = _add_years_clamped(self.assessment_date, 3)
        expected_short_term_assigned = _safe_sum(
            tuple(
                detail.amount_already_reserved
                for detail in self.goal_funding_details
                if detail.target_date <= short_term_cutoff
            )
            + tuple(
                detail.amount_already_reserved
                for detail in self.obligation_funding_details
                if detail.due_date <= short_term_cutoff
            ),
            "protected short-term assigned",
        )
        if self.protected_short_term_assigned != expected_short_term_assigned:
            raise ValueError("protected short-term assigned must equal reserved short-term details")
        expected_claims = _safe_sum(
            (
                self.verified_emergency_reserve,
                self.minimum_operating_cash,
                self.protected_short_term_assigned,
            ),
            "protected liquid claims",
        )
        if self.protected_liquid_claims != expected_claims:
            raise ValueError(
                "protected liquid claims must equal reserve, operating cash, and assigned claims"
            )

    def _validate_horizon_dates(self) -> None:
        for detail in self.goal_funding_details:
            if detail.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                detail.target_date,
            ):
                raise ValueError("goal must use its date-derived horizon equity ceiling")
        for detail in self.obligation_funding_details:
            if detail.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                detail.due_date,
            ):
                raise ValueError("obligation must use its date-derived horizon equity ceiling")
        for sleeve in self.assigned_sleeves:
            if sleeve.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                sleeve.horizon_date,
            ):
                raise ValueError("assigned sleeve must use its date-derived horizon equity ceiling")

    def _validate_sleeves(self) -> None:
        if type(self.assigned_sleeves) is not tuple:
            raise ValueError("assigned sleeves must be a tuple")
        for sleeve in self.assigned_sleeves:
            if type(sleeve) is not AssignedSleeveDetail:
                raise ValueError("assigned sleeves must contain exact sleeve details")
            sleeve.validate()
        short_term_cutoff = _add_years_clamped(self.assessment_date, 3)
        available = Counter(
            (
                AllocationSleeveKind.GOAL,
                detail.name,
                detail.target_date,
                detail.amount_already_reserved,
                detail.horizon_equity_ceiling,
            )
            for detail in self.goal_funding_details
            if detail.amount_already_reserved > 0 and detail.target_date > short_term_cutoff
        )
        available.update(
            (
                AllocationSleeveKind.OBLIGATION,
                detail.name,
                detail.due_date,
                detail.amount_already_reserved,
                detail.horizon_equity_ceiling,
            )
            for detail in self.obligation_funding_details
            if detail.amount_already_reserved > 0 and detail.due_date > short_term_cutoff
        )
        assigned = Counter(
            (
                sleeve.sleeve_kind,
                sleeve.name,
                sleeve.horizon_date,
                sleeve.assigned_amount,
                sleeve.horizon_equity_ceiling,
            )
            for sleeve in self.assigned_sleeves
            if sleeve.sleeve_kind is not AllocationSleeveKind.RESIDUAL
        )
        if assigned != available:
            raise ValueError(
                "assigned non-residual sleeves must equal the exact multiset of long-term details"
            )
        if self.investable_stock_assets == 0:
            if self.assigned_sleeves or self.residual_horizon_date is not None:
                raise ValueError("zero investable stock cannot have assigned sleeves")
            return
        assigned_total = _safe_sum(
            tuple(item.assigned_amount for item in self.assigned_sleeves),
            "assigned sleeve amount total",
        )
        if assigned_total != self.investable_stock_assets:
            raise ValueError("assigned sleeves must equal investable stock assets")
        residuals = tuple(
            item
            for item in self.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        if len(residuals) > 1:
            raise ValueError("assigned sleeves can contain at most one residual")
        if residuals:
            if residuals[0].horizon_date != self.residual_horizon_date:
                raise ValueError("residual sleeve must match residual horizon")
        elif self.residual_horizon_date is not None:
            raise ValueError("residual horizon requires a residual sleeve")


@dataclass(frozen=True)
class AllocationInputs:
    blocks: Tuple[AllocationBlockCode, ...]
    profile_conflicts: Tuple[AllocationProfileConflictCode, ...]
    inherited_constraints: Tuple[AllocationConstraintCode, ...]
    capital: Optional[AllocationCapitalInputs]

    def validate(self) -> None:
        _validate_declared_state(self, AllocationInputs, "allocation inputs")
        _validate_unique_enum_tuple(self.blocks, AllocationBlockCode, "blocks")
        _validate_unique_enum_tuple(
            self.profile_conflicts,
            AllocationProfileConflictCode,
            "profile conflicts",
        )
        _validate_unique_enum_tuple(
            self.inherited_constraints,
            AllocationConstraintCode,
            "inherited constraints",
        )
        if self.capital is None:
            if not self.blocks:
                raise ValueError("allocation inputs without capital require a block")
            return
        if type(self.capital) is not AllocationCapitalInputs:
            raise ValueError("capital must be an exact AllocationCapitalInputs or None")
        if self.blocks or self.profile_conflicts:
            raise ValueError("allocation inputs with capital cannot contain blocks or conflicts")
        self.capital.validate()


def build_allocation_inputs(
    profile: FinancialProfile,
    suitability: AssessmentResult,
    policy: AllocationPolicyV1,
    assessed_at: datetime,
) -> AllocationInputs:
    """Build deterministic Phase C capital inputs without persistence or I/O."""
    _validate_authenticated_inputs(profile, suitability, assessed_at)
    profile.validate()
    suitability.validate()
    policy.validate()
    _validate_assessed_at(assessed_at)

    inherited_constraints = _ordered_constraints(
        _PHASE_B_CONSTRAINT_MAP[item] for item in suitability.constraints
    )
    if suitability.status is AssessmentStatus.BLOCKED:
        return _validated_allocation_inputs(
            AllocationInputs(
                blocks=(AllocationBlockCode.SUITABILITY_BLOCKED,),
                profile_conflicts=(),
                inherited_constraints=inherited_constraints,
                capital=None,
            )
        )

    as_of = assessed_at.astimezone(timezone.utc).date()
    short_term_cutoff = _add_years_clamped(
        as_of,
        policy.protected_short_term_years,
    )
    ordered_goals = tuple(sorted(profile.goals, key=_goal_sort_key))
    ordered_obligations = tuple(sorted(profile.obligations, key=_obligation_sort_key))

    profile_conflicts = set()
    if not profile.can_postpone_goal_use and any(
        item.use_date_can_be_postponed for item in ordered_goals
    ):
        profile_conflicts.add(AllocationProfileConflictCode.PROFILE_DISALLOWS_GOAL_POSTPONEMENT)

    total_financial_assets = _money_down(
        _safe_sum(
            (
                profile.immediately_available_cash,
                profile.cash_like_assets,
                profile.low_risk_fixed_income_assets,
                profile.manual_equity_fund_assets,
                profile.manual_bond_fund_assets,
                profile.manual_sector_fund_assets,
                profile.other_volatile_assets,
            ),
            "total financial assets",
        ),
        policy,
    )
    liquid_protection_assets = _money_down(
        _safe_sum(
            (profile.immediately_available_cash, profile.cash_like_assets),
            "liquid protection assets",
        ),
        policy,
    )
    verified_emergency_reserve = suitability.amounts.verified_emergency_reserve
    minimum_operating_cash = profile.minimum_operating_cash
    protected_short_term_assigned = _safe_sum(
        tuple(
            item.amount_already_reserved
            for item in ordered_obligations
            if item.due_date <= short_term_cutoff
        )
        + tuple(
            item.amount_already_reserved
            for item in ordered_goals
            if item.target_date <= short_term_cutoff
        ),
        "protected short-term assigned",
    )
    protected_liquid_claims = _safe_sum(
        (
            verified_emergency_reserve,
            minimum_operating_cash,
            protected_short_term_assigned,
        ),
        "protected liquid claims",
    )
    investable_stock_assets = _money_down(
        max(
            _ZERO,
            _safe_subtract(
                total_financial_assets,
                protected_liquid_claims,
                "investable stock assets",
            ),
        ),
        policy,
    )
    long_term_assigned = _long_term_profile_assigned_total(
        ordered_goals,
        ordered_obligations,
        short_term_cutoff,
    )

    residual_horizon = _residual_horizon(ordered_goals)
    blocks = set()
    if profile_conflicts:
        blocks.add(AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT)
    if (
        protected_liquid_claims > liquid_protection_assets
        or long_term_assigned > investable_stock_assets
    ):
        blocks.add(AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL)
    if residual_horizon is None:
        blocks.add(AllocationBlockCode.ALLOCATION_HORIZON_MISSING)
    if blocks:
        return _validated_allocation_inputs(
            AllocationInputs(
                blocks=_ordered_blocks(blocks),
                profile_conflicts=_ordered_profile_conflicts(profile_conflicts),
                inherited_constraints=inherited_constraints,
                capital=None,
            )
        )

    goal_details = _goal_funding_details(
        ordered_goals,
        suitability,
        policy,
        as_of,
    )
    obligation_details = _obligation_funding_details(
        ordered_obligations,
        suitability,
        policy,
        as_of,
        short_term_cutoff,
    )
    assigned_sleeves, assigned_residual_horizon = _assigned_sleeves(
        goal_details,
        obligation_details,
        investable_stock_assets,
        residual_horizon,
        as_of,
        short_term_cutoff,
        policy,
    )
    capital = AllocationCapitalInputs(
        assessment_date=as_of,
        total_financial_assets=total_financial_assets,
        liquid_protection_assets=liquid_protection_assets,
        verified_emergency_reserve=verified_emergency_reserve,
        minimum_operating_cash=minimum_operating_cash,
        protected_short_term_assigned=protected_short_term_assigned,
        protected_liquid_claims=protected_liquid_claims,
        investable_stock_assets=investable_stock_assets,
        monthly_discretionary_allocation_ceiling=suitability.amounts.safe_monthly_ceiling,
        maximum_tolerable_loss=profile.maximum_tolerable_loss,
        maximum_tolerable_drawdown=profile.maximum_tolerable_drawdown,
        residual_horizon_date=assigned_residual_horizon,
        goal_funding_details=goal_details,
        obligation_funding_details=obligation_details,
        assigned_sleeves=assigned_sleeves,
    )
    capital.validate()
    return _validated_allocation_inputs(
        AllocationInputs(
            blocks=(),
            profile_conflicts=(),
            inherited_constraints=inherited_constraints,
            capital=capital,
        )
    )


def evaluate_allocation(
    profile: FinancialProfile,
    suitability: AssessmentResult,
    policy: AllocationPolicyV1,
    assessed_at: datetime,
) -> AllocationResult:
    """Evaluate the transparent Phase C feasible region without persistence or I/O."""
    inputs = build_allocation_inputs(profile, suitability, policy, assessed_at)
    if inputs.blocks:
        result = AllocationResult(
            status=AllocationStatus.BLOCKED,
            capability="research_only",
            blocks=inputs.blocks,
            binding_constraints=inputs.inherited_constraints,
            profile_conflicts=inputs.profile_conflicts,
            safe_summary=AllocationSafeSummary(
                goal_count=suitability.goal_count,
                obligation_count=suitability.obligation_count,
                fully_funded_now_count=0,
                fundable_without_return_count=0,
                funding_gap_without_return_count=0,
                horizon_equity_ceilings=(),
            ),
            permitted_region=None,
            exact=None,
        )
        result.validate()
        return result

    capital = inputs.capital
    if capital is None:
        raise ValueError("unblocked allocation inputs require capital calculations")

    weighted_horizon_numerator = _safe_sum(
        tuple(item.weighted_equity_contribution for item in capital.assigned_sleeves),
        "weighted horizon numerator",
    )
    if capital.investable_stock_assets == 0:
        weighted_horizon_ceiling = _ZERO
        loss_amount_ceiling = _ZERO
    else:
        weighted_horizon_ceiling = _safe_ratio_floor(
            weighted_horizon_numerator,
            capital.investable_stock_assets,
            "weighted horizon equity ceiling",
        )
        loss_amount_ceiling = _safe_ratio_floor(
            capital.maximum_tolerable_loss,
            capital.investable_stock_assets,
            "loss amount equity ceiling",
            denominator_scale=_policy_stress_loss(
                policy,
                AssetLayer.DIVERSIFIED_EQUITY,
            ),
            cap_at_one=True,
        )
    drawdown_ceiling = _safe_ratio_floor(
        capital.maximum_tolerable_drawdown,
        _policy_stress_loss(policy, AssetLayer.DIVERSIFIED_EQUITY),
        "drawdown equity ceiling",
        cap_at_one=True,
    )
    willingness_ceiling = _behavioral_willingness_ceiling(profile, policy)
    stability_ceiling = _financial_stability_ceiling(profile, policy)
    fixed_income_stress = _policy_stress_loss(
        policy,
        AssetLayer.HIGH_QUALITY_FIXED_INCOME,
    )
    equity_stress = _policy_stress_loss(policy, AssetLayer.DIVERSIFIED_EQUITY)
    aggregate = AggregateAllocationInputs(
        weighted_horizon_numerator=weighted_horizon_numerator,
        weighted_horizon_equity_ceiling=weighted_horizon_ceiling,
        loss_amount_equity_ceiling=loss_amount_ceiling,
        drawdown_equity_ceiling=drawdown_ceiling,
        willingness_equity_ceiling=willingness_ceiling,
        stability_equity_ceiling=stability_ceiling,
        fixed_income_stress_loss=fixed_income_stress,
        equity_stress_loss=equity_stress,
    )
    aggregate.validate()

    exact = AllocationExactResult(
        assessment_date=capital.assessment_date,
        total_financial_assets=capital.total_financial_assets,
        liquid_protection_assets=capital.liquid_protection_assets,
        verified_emergency_reserve=capital.verified_emergency_reserve,
        minimum_operating_cash=capital.minimum_operating_cash,
        protected_short_term_assigned=capital.protected_short_term_assigned,
        protected_liquid_claims=capital.protected_liquid_claims,
        investable_stock_assets=capital.investable_stock_assets,
        monthly_discretionary_allocation_ceiling=(capital.monthly_discretionary_allocation_ceiling),
        maximum_tolerable_loss=capital.maximum_tolerable_loss,
        maximum_tolerable_drawdown=capital.maximum_tolerable_drawdown,
        residual_horizon_date=capital.residual_horizon_date,
        goal_funding_details=capital.goal_funding_details,
        obligation_funding_details=capital.obligation_funding_details,
        assigned_sleeves=capital.assigned_sleeves,
        aggregate_inputs=aggregate,
    )
    exact.validate()

    summary = _allocation_safe_summary(capital)
    constraints = set(inputs.inherited_constraints)
    if summary.funding_gap_without_return_count:
        constraints.add(AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN)

    permitted_region = None
    if capital.investable_stock_assets == 0:
        constraints.add(AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK)
    else:
        ceilings = (
            weighted_horizon_ceiling,
            loss_amount_ceiling,
            drawdown_ceiling,
            willingness_ceiling,
            stability_ceiling,
        )
        maximum_equity = min(ceilings)
        permitted_region = PermittedRegion(
            inequalities=REGION_INEQUALITIES,
            maximum_equity=maximum_equity,
            horizon_equity_ceiling=weighted_horizon_ceiling,
            loss_amount_equity_ceiling=loss_amount_ceiling,
            drawdown_equity_ceiling=drawdown_ceiling,
            willingness_equity_ceiling=willingness_ceiling,
            stability_equity_ceiling=stability_ceiling,
        )
        permitted_region.validate()
        for code, ceiling in (
            (AllocationConstraintCode.HORIZON_BINDING, weighted_horizon_ceiling),
            (AllocationConstraintCode.LOSS_AMOUNT_BINDING, loss_amount_ceiling),
            (AllocationConstraintCode.DRAWDOWN_BINDING, drawdown_ceiling),
            (AllocationConstraintCode.WILLINGNESS_BINDING, willingness_ceiling),
            (AllocationConstraintCode.STABILITY_BINDING, stability_ceiling),
        ):
            if ceiling == maximum_equity:
                constraints.add(code)

    result = AllocationResult(
        status=AllocationStatus.RANGE_AVAILABLE,
        capability="research_only",
        blocks=(),
        binding_constraints=_ordered_constraints(constraints),
        profile_conflicts=(),
        safe_summary=summary,
        permitted_region=permitted_region,
        exact=exact,
    )
    result.validate()
    return result


def _behavioral_willingness_ceiling(
    profile: FinancialProfile,
    policy: AllocationPolicyV1,
) -> Decimal:
    if profile.reaction_10 is not RiskReaction.HOLD:
        key = "reduce_or_redeem_at_10"
    elif profile.reaction_20 is not RiskReaction.HOLD:
        key = "hold_10_not_20"
    elif profile.reaction_30 is not RiskReaction.HOLD:
        key = "hold_20_not_30"
    elif profile.experienced_material_loss and profile.understands_multi_year_recovery:
        key = "hold_30_experienced_and_recovery_aware"
    else:
        key = "hold_30_missing_experience_or_recovery_awareness"
    return _policy_named_ceiling(policy.willingness_equity_ceilings, key)


def _financial_stability_ceiling(
    profile: FinancialProfile,
    policy: AllocationPolicyV1,
) -> Decimal:
    if profile.income_stability is IncomeStability.UNSTABLE:
        key = "unstable"
    elif profile.income_interruption_risk:
        key = "variable_with_dependents_or_interruption_signal"
    elif profile.income_stability is IncomeStability.STABLE and profile.dependents == 0:
        key = "stable_no_dependents_no_interruption"
    elif profile.income_stability is IncomeStability.VARIABLE and profile.dependents > 0:
        key = "variable_with_dependents_or_interruption_signal"
    else:
        key = "stable_with_dependents_or_variable_without_dependents"
    return _policy_named_ceiling(policy.stability_equity_ceilings, key)


def _policy_named_ceiling(
    entries: Tuple[Tuple[str, Decimal], ...],
    key: str,
) -> Decimal:
    for entry_key, value in entries:
        if entry_key == key:
            return value
    raise ValueError("allocation policy is missing a required ceiling")


def _policy_stress_loss(
    policy: AllocationPolicyV1,
    layer: AssetLayer,
) -> Decimal:
    for entry_layer, value in policy.stress_loss_by_layer:
        if entry_layer is layer:
            return value
    raise ValueError("allocation policy is missing a required stress loss")


def _allocation_safe_summary(capital: AllocationCapitalInputs) -> AllocationSafeSummary:
    state_counts = Counter(item.funding_state for item in capital.goal_funding_details)
    summary = AllocationSafeSummary(
        goal_count=len(capital.goal_funding_details),
        obligation_count=len(capital.obligation_funding_details),
        fully_funded_now_count=state_counts[GoalFundingState.FULLY_FUNDED_NOW],
        fundable_without_return_count=state_counts[GoalFundingState.FUNDABLE_WITHOUT_RETURN],
        funding_gap_without_return_count=(
            state_counts[GoalFundingState.FUNDING_GAP_WITHOUT_RETURN]
        ),
        horizon_equity_ceilings=tuple(
            item.horizon_equity_ceiling for item in capital.assigned_sleeves
        ),
    )
    summary.validate()
    return summary


def _goal_funding_details(
    goals: Tuple[FinancialGoal, ...],
    suitability: AssessmentResult,
    policy: AllocationPolicyV1,
    as_of: date,
) -> Tuple[GoalFundingDetail, ...]:
    raw_requirements = []
    for index, item in enumerate(goals):
        gap = max(
            _ZERO,
            _safe_subtract(
                item.target_amount,
                item.amount_already_reserved,
                "goal funding gap",
            ),
        )
        if item.priority == 1 and gap > 0:
            raw_requirements.append(
                (
                    index,
                    _safe_divide(
                        gap,
                        Decimal(_contribution_periods(as_of, item.target_date)),
                        "goal monthly saving requirement",
                    ),
                    _goal_sort_key(item),
                )
            )
    apportioned = _apportion_monthly_requirements(
        raw_requirements,
        suitability.amounts.required_monthly_goal_saving,
        policy,
        "goal",
    )
    details = []
    for index, item in enumerate(goals):
        target_amount = item.target_amount
        reserved = item.amount_already_reserved
        periods = _contribution_periods(as_of, item.target_date)
        confirmed = apportioned.get(index, _money_down(_ZERO, policy))
        zero_return_funding = _safe_add(
            reserved,
            _safe_multiply(confirmed, periods, "goal confirmed saving"),
            "goal zero-return funding",
        )
        if reserved >= target_amount:
            state = GoalFundingState.FULLY_FUNDED_NOW
        elif zero_return_funding >= target_amount:
            state = GoalFundingState.FUNDABLE_WITHOUT_RETURN
        else:
            state = GoalFundingState.FUNDING_GAP_WITHOUT_RETURN
        details.append(
            GoalFundingDetail(
                name=item.name,
                target_date=item.target_date,
                target_amount=target_amount,
                amount_already_reserved=reserved,
                confirmed_monthly_saving=confirmed,
                remaining_contribution_periods=periods,
                zero_return_funding=zero_return_funding,
                funding_state=state,
                horizon_equity_ceiling=_horizon_ceiling(
                    as_of,
                    item.target_date,
                    policy,
                ),
            )
        )
    result = tuple(details)
    for detail in result:
        detail.validate()
    return result


def _obligation_funding_details(
    obligations: Tuple[PlannedObligation, ...],
    suitability: AssessmentResult,
    policy: AllocationPolicyV1,
    as_of: date,
    short_term_cutoff: date,
) -> Tuple[ObligationFundingDetail, ...]:
    raw_requirements = []
    for index, item in enumerate(obligations):
        raw_gap = max(
            _ZERO,
            _safe_subtract(
                item.amount,
                item.amount_already_reserved,
                "obligation funding gap",
            ),
        )
        if item.due_date <= short_term_cutoff and raw_gap > 0:
            raw_requirements.append(
                (
                    index,
                    _safe_divide(
                        raw_gap,
                        Decimal(_contribution_periods(as_of, item.due_date)),
                        "obligation monthly saving requirement",
                    ),
                    _obligation_sort_key(item),
                )
            )
    apportioned = _apportion_monthly_requirements(
        raw_requirements,
        suitability.amounts.required_monthly_obligation_saving,
        policy,
        "obligation",
    )
    details = []
    for index, item in enumerate(obligations):
        amount = item.amount
        reserved = item.amount_already_reserved
        gap = max(
            _ZERO,
            _safe_subtract(amount, reserved, "obligation funding gap"),
        )
        periods = _contribution_periods(as_of, item.due_date)
        confirmed = apportioned.get(index, _money_down(_ZERO, policy))
        details.append(
            ObligationFundingDetail(
                name=item.name,
                due_date=item.due_date,
                amount=amount,
                amount_already_reserved=reserved,
                funding_gap=gap,
                confirmed_monthly_saving=confirmed,
                remaining_contribution_periods=periods,
                zero_return_funding=_safe_add(
                    reserved,
                    _safe_multiply(
                        confirmed,
                        periods,
                        "obligation confirmed saving",
                    ),
                    "obligation zero-return funding",
                ),
                horizon_equity_ceiling=_horizon_ceiling(
                    as_of,
                    item.due_date,
                    policy,
                ),
            )
        )
    result = tuple(details)
    for detail in result:
        detail.validate()
    return result


def _apportion_monthly_requirements(
    requirements: list[tuple[int, Decimal, tuple]],
    authenticated_total: Decimal,
    policy: AllocationPolicyV1,
    item_kind: str,
) -> dict[int, Decimal]:
    if (
        type(authenticated_total) is not Decimal
        or not authenticated_total.is_finite()
        or authenticated_total < 0
        or authenticated_total
        != _safe_quantize(
            authenticated_total,
            policy.money_quantum,
            f"authenticated monthly {item_kind} saving",
        )
    ):
        raise ValueError(f"authenticated monthly {item_kind} saving must be non-negative CNY cents")
    raw_total = _safe_sum(
        tuple(raw for _, raw, _ in requirements),
        f"monthly {item_kind} raw requirement total",
    )
    expected_total = _money_up(raw_total, policy)
    if authenticated_total != expected_total:
        raise ValueError(
            f"authenticated monthly {item_kind} saving does not match deterministic Phase B inputs"
        )

    base_floors = {index: _money_down(raw, policy) for index, raw, _ in requirements}
    apportioned = dict(base_floors)
    residual = _safe_subtract(
        authenticated_total,
        _safe_sum(
            tuple(base_floors.values()),
            f"monthly {item_kind} floor total",
        ),
        f"monthly {item_kind} residual",
    )
    residual_units_decimal = _safe_divide(
        residual,
        policy.money_quantum,
        f"monthly {item_kind} residual units",
    )
    residual_units = int(residual_units_decimal)
    if residual < 0 or residual_units_decimal != Decimal(residual_units):
        raise ValueError(f"monthly {item_kind} saving residual is impossible")
    if residual_units > len(requirements):
        raise ValueError(f"monthly {item_kind} saving residual exceeds eligible items")

    ranked = sorted(requirements, key=lambda entry: entry[2])
    ranked.sort(
        key=lambda entry: _safe_subtract(
            entry[1],
            base_floors[entry[0]],
            f"monthly {item_kind} fractional remainder",
        ),
        reverse=True,
    )
    for index, _, _ in ranked[:residual_units]:
        apportioned[index] = _safe_add(
            apportioned[index],
            policy.money_quantum,
            f"monthly {item_kind} apportioned amount",
        )
    if any(
        _safe_subtract(
            apportioned[index],
            floor,
            f"monthly {item_kind} item increment",
        )
        not in (_ZERO, policy.money_quantum)
        for index, floor in base_floors.items()
    ):
        raise ValueError(f"monthly {item_kind} saving item increment is impossible")
    if (
        _safe_sum(
            tuple(apportioned.values()),
            f"monthly {item_kind} apportioned total",
        )
        != authenticated_total
    ):
        raise ValueError(f"monthly {item_kind} saving apportionment is not conservative")
    return apportioned


def _assigned_sleeves(
    goals: Tuple[GoalFundingDetail, ...],
    obligations: Tuple[ObligationFundingDetail, ...],
    investable_stock_assets: Decimal,
    residual_horizon: date,
    as_of: date,
    short_term_cutoff: date,
    policy: AllocationPolicyV1,
) -> Tuple[Tuple[AssignedSleeveDetail, ...], Optional[date]]:
    if investable_stock_assets == 0:
        return (), None
    sleeves = []
    for item in goals:
        if item.target_date > short_term_cutoff and item.amount_already_reserved > 0:
            sleeves.append(
                _sleeve(
                    AllocationSleeveKind.GOAL,
                    item.name,
                    item.amount_already_reserved,
                    item.target_date,
                    item.horizon_equity_ceiling,
                )
            )
    for item in obligations:
        if item.due_date > short_term_cutoff and item.amount_already_reserved > 0:
            sleeves.append(
                _sleeve(
                    AllocationSleeveKind.OBLIGATION,
                    item.name,
                    item.amount_already_reserved,
                    item.due_date,
                    item.horizon_equity_ceiling,
                )
            )
    assigned = _safe_sum(
        tuple(item.assigned_amount for item in sleeves),
        "assigned long-term sleeve total",
    )
    if assigned > investable_stock_assets:
        raise ValueError("assigned long-term sleeves exceed investable stock assets")
    residual = _money_down(
        _safe_subtract(
            investable_stock_assets,
            assigned,
            "residual sleeve amount",
        ),
        policy,
    )
    assigned_residual_horizon = None
    if residual > 0:
        assigned_residual_horizon = residual_horizon
        sleeves.append(
            _sleeve(
                AllocationSleeveKind.RESIDUAL,
                "residual",
                residual,
                residual_horizon,
                _horizon_ceiling(
                    as_of,
                    residual_horizon,
                    policy,
                ),
            )
        )
    result = tuple(sleeves)
    for detail in result:
        detail.validate()
    return result, assigned_residual_horizon


def _long_term_profile_assigned_total(
    goals: Tuple[FinancialGoal, ...],
    obligations: Tuple[PlannedObligation, ...],
    short_term_cutoff: date,
) -> Decimal:
    return _safe_add(
        _safe_sum(
            tuple(
                item.amount_already_reserved
                for item in goals
                if item.target_date > short_term_cutoff
            ),
            "long-term goal assigned total",
        ),
        _safe_sum(
            tuple(
                item.amount_already_reserved
                for item in obligations
                if item.due_date > short_term_cutoff
            ),
            "long-term obligation assigned total",
        ),
        "long-term assigned total",
    )


def _sleeve(
    kind: AllocationSleeveKind,
    name: str,
    amount: Decimal,
    horizon_date: date,
    ceiling: Decimal,
) -> AssignedSleeveDetail:
    return AssignedSleeveDetail(
        sleeve_kind=kind,
        name=name,
        assigned_amount=amount,
        horizon_date=horizon_date,
        horizon_equity_ceiling=ceiling,
        weighted_equity_contribution=_safe_multiply(
            amount,
            ceiling,
            "weighted equity contribution",
        ),
    )


def _residual_horizon(goals: Tuple[FinancialGoal, ...]) -> Optional[date]:
    positive_gap = tuple(
        item for item in goals if item.target_amount > item.amount_already_reserved
    )
    priority_one = tuple(item for item in positive_gap if item.priority == 1)
    eligible = priority_one or positive_gap
    return min((item.target_date for item in eligible), default=None)


def _horizon_ceiling(
    as_of: date,
    target_date: date,
    policy: AllocationPolicyV1,
) -> Decimal:
    policy.validate()
    return horizon_equity_ceiling_v1(as_of, target_date)


def _contribution_periods(as_of: date, due: date) -> int:
    return max(1, 12 * (due.year - as_of.year) + due.month - as_of.month + 1)


def _add_years_clamped(value: date, years: int) -> date:
    if type(value) is not date or type(years) is not int:
        raise ValueError("calendar horizon inputs must use exact date and integer values")
    target_year = value.year + years
    if target_year > date.max.year:
        return date.max
    try:
        return value.replace(year=target_year)
    except ValueError:
        if value.month == 2 and value.day == 29:
            return value.replace(month=2, day=28, year=target_year)
        raise


def _money_up(value: Decimal, policy: AllocationPolicyV1) -> Decimal:
    return _safe_quantize(
        value,
        policy.money_quantum,
        "required allocation amount",
        rounding=policy.required_amount_rounding,
    )


def _money_down(value: Decimal, policy: AllocationPolicyV1) -> Decimal:
    return _safe_quantize(
        value,
        policy.money_quantum,
        "available allocation amount",
        rounding=policy.available_amount_rounding,
    )


def _validate_money(value: object, name: str) -> None:
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or value < 0
        or value != _safe_quantize(value, Decimal("0.01"), name)
    ):
        raise ValueError(f"{name} must be non-negative CNY cents")


def _goal_sort_key(item: FinancialGoal) -> tuple:
    return (
        item.target_date,
        item.priority,
        item.name,
        item.target_amount,
        item.amount_already_reserved,
        item.temporary_principal_loss_acceptable,
        item.use_date_can_be_postponed,
    )


def _obligation_sort_key(item: PlannedObligation) -> tuple:
    return (item.due_date, item.name, item.amount, item.amount_already_reserved)


def _ordered_blocks(
    blocks: Iterable[AllocationBlockCode],
) -> Tuple[AllocationBlockCode, ...]:
    values = set(blocks)
    return tuple(item for item in AllocationBlockCode if item in values)


def _ordered_profile_conflicts(
    conflicts: Iterable[AllocationProfileConflictCode],
) -> Tuple[AllocationProfileConflictCode, ...]:
    values = set(conflicts)
    return tuple(item for item in AllocationProfileConflictCode if item in values)


def _ordered_constraints(
    constraints: Iterable[AllocationConstraintCode],
) -> Tuple[AllocationConstraintCode, ...]:
    values = set(constraints)
    return tuple(item for item in AllocationConstraintCode if item in values)


def _validated_allocation_inputs(value: AllocationInputs) -> AllocationInputs:
    value.validate()
    return value


def _validate_declared_state(value: object, expected_type: type, name: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be an exact {expected_type.__name__}")
    expected_fields = {item.name for item in fields(expected_type)}
    state = vars(value)
    if type(state) is not dict or set(state) != expected_fields:
        raise ValueError(f"{name} contains unexpected state")


def _validate_unique_enum_tuple(value: object, enum_type: type, name: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    if any(type(item) is not enum_type for item in value):
        raise ValueError(f"{name} must contain exact {enum_type.__name__} values")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} cannot contain duplicates")


def _validate_assessed_at(value: datetime) -> None:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ValueError("assessed_at must use an exact datetime and timezone")


def _validate_authenticated_inputs(
    profile: object,
    suitability: object,
    assessed_at: object,
) -> None:
    _require_exact_dataclass(profile, FinancialProfile, "profile")
    assert type(profile) is FinancialProfile
    _require_exact_text(profile.currency, "profile currency")
    for field_name in (
        "monthly_net_income",
        "monthly_essential_expenses",
        "monthly_required_debt_service",
        "monthly_investment_ceiling",
        "minimum_operating_cash",
        "minimum_monthly_cash_buffer",
        "immediately_available_cash",
        "cash_like_assets",
        "emergency_reserve",
        "low_risk_fixed_income_assets",
        "manual_equity_fund_assets",
        "manual_bond_fund_assets",
        "manual_sector_fund_assets",
        "other_volatile_assets",
        "maximum_tolerable_loss",
    ):
        _require_exact_money(getattr(profile, field_name), field_name)
    _require_exact_decimal(
        profile.maximum_tolerable_drawdown,
        "maximum_tolerable_drawdown",
    )
    _require_exact_enum(profile.income_stability, IncomeStability, "income stability")
    for field_name in ("reaction_10", "reaction_20", "reaction_30"):
        _require_exact_enum(getattr(profile, field_name), RiskReaction, field_name)
    for field_name in (
        "income_interruption_risk",
        "experienced_material_loss",
        "understands_multi_year_recovery",
        "can_postpone_goal_use",
    ):
        _require_exact_bool(getattr(profile, field_name), field_name)
    _require_exact_int(profile.dependents, "dependents")
    _require_exact_datetime(profile.confirmed_at, "confirmed_at")
    _require_exact_tuple(profile.debts, "debts")
    for item in profile.debts:
        _validate_debt(item)
    _require_exact_tuple(profile.obligations, "obligations")
    for item in profile.obligations:
        _validate_obligation(item)
    _require_exact_tuple(profile.goals, "goals")
    for item in profile.goals:
        _validate_goal(item)

    _require_exact_dataclass(suitability, AssessmentResult, "assessment result")
    assert type(suitability) is AssessmentResult
    _require_exact_enum(suitability.status, AssessmentStatus, "assessment status")
    _require_enum_tuple(suitability.hard_blocks, BlockReason, "hard blocks")
    _require_enum_tuple(suitability.constraints, ConstraintReason, "constraints")
    _require_enum_tuple(
        suitability.profile_conflicts,
        ProfileConflictCode,
        "profile conflicts",
    )
    for field_name in (
        "required_reserve_months",
        "debt_count",
        "obligation_count",
        "goal_count",
    ):
        _require_exact_int(getattr(suitability, field_name), field_name)
    _require_exact_bool(suitability.risk_answers_consistent, "risk answers consistent")
    _require_exact_dataclass(suitability.amounts, AssessmentAmounts, "assessment amounts")
    for item in fields(AssessmentAmounts):
        _require_exact_money(getattr(suitability.amounts, item.name), item.name)
    _require_exact_datetime(assessed_at, "assessed_at")


def _require_exact_dataclass(value: object, expected_type: type, name: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be an exact {expected_type.__name__}")
    expected_fields = {item.name for item in fields(expected_type)}
    state = getattr(value, "__dict__", None)
    if type(state) is not dict or set(state) != expected_fields:
        raise ValueError(f"{name} contains unexpected state")


def _require_exact_decimal(value: object, name: str) -> None:
    if type(value) is not Decimal:
        raise ValueError(f"{name} must be an exact Decimal")


def _require_exact_money(value: object, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be exact CNY cents")
    if value != _safe_quantize(value, Decimal("0.01"), name):
        raise ValueError(f"{name} must be exact CNY cents")


def _require_exact_text(value: object, name: str) -> None:
    if type(value) is not str:
        raise ValueError(f"{name} must be an exact str")


def _require_exact_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be an exact bool")


def _require_exact_int(value: object, name: str) -> None:
    if type(value) is not int:
        raise ValueError(f"{name} must be an exact int")


def _require_exact_date(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be an exact date")


def _require_exact_datetime(value: object, name: str) -> None:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ValueError(f"{name} must use an exact datetime and timezone")


def _require_exact_tuple(value: object, name: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")


def _require_exact_enum(value: object, enum_type: type, name: str) -> None:
    if type(value) is not enum_type:
        raise ValueError(f"{name} must be an exact {enum_type.__name__}")


def _require_enum_tuple(value: object, enum_type: type, name: str) -> None:
    _require_exact_tuple(value, name)
    assert type(value) is tuple
    for item in value:
        _require_exact_enum(item, enum_type, name)


def _validate_debt(value: object) -> None:
    _require_exact_dataclass(value, Debt, "debt")
    assert type(value) is Debt
    _require_exact_text(value.debt_type, "debt type")
    _require_exact_money(value.outstanding_principal, "outstanding principal")
    _require_exact_decimal(value.effective_annual_rate, "effective annual rate")
    _require_exact_money(value.monthly_payment, "monthly payment")
    if value.maturity_date is not None:
        _require_exact_date(value.maturity_date, "maturity date")
    _require_exact_bool(value.delinquent, "delinquent")
    _require_exact_bool(value.revolving_interest, "revolving interest")


def _validate_obligation(value: object) -> None:
    _require_exact_dataclass(value, PlannedObligation, "obligation")
    assert type(value) is PlannedObligation
    _require_exact_text(value.name, "obligation name")
    _require_exact_money(value.amount, "obligation amount")
    _require_exact_date(value.due_date, "obligation due date")
    _require_exact_money(value.amount_already_reserved, "obligation reserved amount")


def _validate_goal(value: object) -> None:
    _require_exact_dataclass(value, FinancialGoal, "goal")
    assert type(value) is FinancialGoal
    _require_exact_text(value.name, "goal name")
    _require_exact_money(value.target_amount, "goal target amount")
    _require_exact_date(value.target_date, "goal target date")
    _require_exact_int(value.priority, "goal priority")
    _require_exact_money(value.amount_already_reserved, "goal reserved amount")
    _require_exact_bool(
        value.temporary_principal_loss_acceptable,
        "temporary principal loss acceptable",
    )
    _require_exact_bool(value.use_date_can_be_postponed, "goal postponement")
