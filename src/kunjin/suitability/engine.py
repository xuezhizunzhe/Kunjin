from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Tuple

from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
    Debt,
    FinancialProfile,
    IncomeStability,
    ProfileConflictCode,
    RiskReaction,
)
from kunjin.suitability.policy import SuitabilityPolicyV1

_ZERO = Decimal("0")


def evaluate(
    profile: FinancialProfile,
    policy: SuitabilityPolicyV1,
    assessed_at: datetime,
) -> AssessmentResult:
    """Evaluate the complete Phase B financial-foundation safety gates."""
    profile.validate()
    policy.validate()
    _validate_assessed_at(assessed_at)

    block_reasons = {
        reason
        for item in profile.debts
        for reason in _debt_reasons(item, policy)
    }
    constraint_reasons = set()
    profile_conflicts = set()
    as_of = assessed_at.date()
    one_year_cutoff = _add_years(as_of, policy.short_horizon_years)
    three_year_cutoff = _add_years(as_of, policy.medium_horizon_years)

    obligation_monthly_saving = _ZERO
    for item in profile.obligations:
        gap = max(_ZERO, item.amount - item.amount_already_reserved)
        if gap == 0:
            continue
        if item.due_date < as_of:
            block_reasons.add(BlockReason.OBLIGATION_OVERDUE)
        elif item.due_date > one_year_cutoff and item.due_date <= three_year_cutoff:
            constraint_reasons.add(ConstraintReason.NEAR_TERM_OBLIGATION_GAP)
        if item.due_date <= three_year_cutoff:
            obligation_monthly_saving += gap / Decimal(
                _contribution_periods(as_of, item.due_date)
            )

    goal_monthly_saving = _ZERO
    for item in profile.goals:
        gap = max(_ZERO, item.target_amount - item.amount_already_reserved)
        if gap == 0:
            continue
        if item.target_date < as_of:
            block_reasons.add(BlockReason.GOAL_OVERDUE)
        if (
            item.priority == 1
            and item.target_date <= one_year_cutoff
            and not item.use_date_can_be_postponed
        ):
            block_reasons.add(BlockReason.CRITICAL_GOAL_SHORTFALL)
        if item.target_date > one_year_cutoff and item.target_date <= three_year_cutoff:
            constraint_reasons.add(ConstraintReason.NEAR_TERM_GOAL_GAP)
        if item.priority == 1:
            goal_monthly_saving += gap / Decimal(
                _contribution_periods(as_of, item.target_date)
            )

    required_monthly_obligation_saving = _money_up(
        obligation_monthly_saving,
        policy,
    )
    required_monthly_goal_saving = _money_up(goal_monthly_saving, policy)

    unfunded_within_one_year = _unfunded_obligations(
        profile,
        as_of,
        policy,
    )
    reserve_months = _required_reserve_months(
        profile,
        policy,
        unfunded_within_one_year,
    )

    liquid_reserve_assets = (
        profile.immediately_available_cash + profile.cash_like_assets
    )
    verified_reserve = _money_down(
        min(profile.emergency_reserve, liquid_reserve_assets),
        policy,
    )
    itemized_required_debt_service = sum(
        (
            item.monthly_payment
            for item in profile.debts
            if item.outstanding_principal > 0
        ),
        start=_ZERO,
    )
    if profile.monthly_required_debt_service < itemized_required_debt_service:
        profile_conflicts.add(
            ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS
        )
    effective_required_debt_service = max(
        profile.monthly_required_debt_service,
        itemized_required_debt_service,
    )
    monthly_safety_cost = (
        profile.monthly_essential_expenses + effective_required_debt_service
    )
    required_reserve = _money_up(
        monthly_safety_cost * Decimal(reserve_months)
        + unfunded_within_one_year,
        policy,
    )
    reserve_shortfall = _money_up(
        max(_ZERO, required_reserve - verified_reserve),
        policy,
    )
    if reserve_shortfall > 0:
        block_reasons.add(BlockReason.EMERGENCY_RESERVE_SHORTFALL)

    monthly_safety_residual = _money_down(
        profile.monthly_net_income
        - profile.monthly_essential_expenses
        - effective_required_debt_service
        - required_monthly_obligation_saving
        - required_monthly_goal_saving
        - profile.minimum_monthly_cash_buffer,
        policy,
    )
    safe_monthly_ceiling = _money_down(
        min(
            profile.monthly_investment_ceiling,
            max(_ZERO, monthly_safety_residual),
        ),
        policy,
    )
    if monthly_safety_residual <= 0:
        block_reasons.add(BlockReason.NO_MONTHLY_INVESTABLE_CASH_FLOW)
    elif monthly_safety_residual < profile.monthly_investment_ceiling:
        constraint_reasons.add(ConstraintReason.MONTHLY_CEILING_CONSTRAINED)

    risk_conflicts = _risk_conflicts(profile, policy)
    profile_conflicts.update(risk_conflicts)
    risk_answers_consistent = not risk_conflicts
    if profile_conflicts:
        block_reasons.add(BlockReason.PROFILE_CONFLICT)

    hard_blocks = _ordered_block_reasons(block_reasons)
    constraints = _ordered_constraint_reasons(constraint_reasons)
    ordered_profile_conflicts = _ordered_profile_conflicts(profile_conflicts)
    if hard_blocks:
        status = AssessmentStatus.BLOCKED
    elif constraints:
        status = AssessmentStatus.CONSTRAINED
    else:
        status = AssessmentStatus.READY_FOR_ALLOCATION
    result = AssessmentResult(
        status=status,
        hard_blocks=hard_blocks,
        constraints=constraints,
        required_reserve_months=reserve_months,
        risk_answers_consistent=risk_answers_consistent,
        profile_conflicts=ordered_profile_conflicts,
        debt_count=len(profile.debts),
        obligation_count=len(profile.obligations),
        goal_count=len(profile.goals),
        amounts=AssessmentAmounts(
            verified_emergency_reserve=verified_reserve,
            required_emergency_reserve=required_reserve,
            emergency_reserve_shortfall=reserve_shortfall,
            required_monthly_obligation_saving=required_monthly_obligation_saving,
            required_monthly_goal_saving=required_monthly_goal_saving,
            monthly_safety_residual=monthly_safety_residual,
            safe_monthly_ceiling=safe_monthly_ceiling,
        ),
    )
    result.validate()
    return result


def _debt_reasons(
    debt: Debt,
    policy: SuitabilityPolicyV1,
) -> Tuple[BlockReason, ...]:
    if debt.outstanding_principal == 0:
        return ()

    reasons = set()
    supported_values = {item.value for item in policy.supported_debt_types}
    consumer_values = {item.value for item in policy.consumer_debt_types}

    if debt.debt_type not in supported_values:
        reasons.add(BlockReason.DEBT_TYPE_UNKNOWN)
    if debt.delinquent:
        reasons.add(BlockReason.DEBT_DELINQUENT)
    if debt.revolving_interest:
        reasons.add(BlockReason.REVOLVING_CREDIT)
    if (
        debt.debt_type in consumer_values
        and debt.effective_annual_rate >= policy.high_interest_annual_rate
    ):
        reasons.add(BlockReason.HIGH_INTEREST_DEBT)

    return _ordered_block_reasons(reasons)


def _required_reserve_months(
    profile: FinancialProfile,
    policy: SuitabilityPolicyV1,
    unfunded_obligations_within_one_year: Decimal,
) -> int:
    material_threshold = (
        profile.monthly_essential_expenses
        * policy.material_obligation_expense_months
    )
    has_material_obligation = (
        unfunded_obligations_within_one_year > 0
        and unfunded_obligations_within_one_year >= material_threshold
    )
    if (
        profile.income_stability is IncomeStability.UNSTABLE
        or profile.income_interruption_risk
        or has_material_obligation
    ):
        return policy.reserve_months_high_risk
    if (
        profile.income_stability is IncomeStability.VARIABLE
        or profile.dependents > 0
    ):
        return policy.reserve_months_variable
    return policy.reserve_months_stable


def _unfunded_obligations(
    profile: FinancialProfile,
    as_of: date,
    policy: SuitabilityPolicyV1,
) -> Decimal:
    one_year_cutoff = _add_years(as_of, policy.short_horizon_years)
    return sum(
        (
            max(_ZERO, item.amount - item.amount_already_reserved)
            for item in profile.obligations
            if item.due_date <= one_year_cutoff
        ),
        start=_ZERO,
    )


def _money_up(value: Decimal, policy: SuitabilityPolicyV1) -> Decimal:
    return value.quantize(
        policy.money_quantum,
        rounding=policy.required_amount_rounding,
    )


def _money_down(value: Decimal, policy: SuitabilityPolicyV1) -> Decimal:
    return value.quantize(
        policy.money_quantum,
        rounding=policy.available_amount_rounding,
    )


def _ordered_block_reasons(
    reasons: Iterable[BlockReason],
) -> Tuple[BlockReason, ...]:
    reason_set = set(reasons)
    return tuple(reason for reason in BlockReason if reason in reason_set)


def _ordered_constraint_reasons(
    reasons: Iterable[ConstraintReason],
) -> Tuple[ConstraintReason, ...]:
    reason_set = set(reasons)
    return tuple(reason for reason in ConstraintReason if reason in reason_set)


def _ordered_profile_conflicts(
    conflicts: Iterable[ProfileConflictCode],
) -> Tuple[ProfileConflictCode, ...]:
    conflict_set = set(conflicts)
    return tuple(item for item in ProfileConflictCode if item in conflict_set)


def _contribution_periods(as_of: date, due: date) -> int:
    return max(
        1,
        12 * (due.year - as_of.year) + due.month - as_of.month + 1,
    )


def _risk_conflicts(
    profile: FinancialProfile,
    policy: SuitabilityPolicyV1,
) -> Tuple[ProfileConflictCode, ...]:
    severity = dict(policy.risk_reaction_severity)
    reactions = (
        profile.reaction_10,
        profile.reaction_20,
        profile.reaction_30,
    )
    conflicts = set()
    if severity[profile.reaction_10] > severity[profile.reaction_20]:
        conflicts.add(ProfileConflictCode.REACTION_10_VS_REACTION_20)
    if severity[profile.reaction_20] > severity[profile.reaction_30]:
        conflicts.add(ProfileConflictCode.REACTION_20_VS_REACTION_30)

    thresholds = (
        (
            Decimal("0.10"),
            profile.reaction_10,
            ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10,
        ),
        (
            Decimal("0.20"),
            profile.reaction_20,
            ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_20,
        ),
        (
            Decimal("0.30"),
            profile.reaction_30,
            ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_30,
        ),
    )
    for threshold, reaction, conflict in thresholds:
        if (
            reaction is RiskReaction.REDEEM
            and profile.maximum_tolerable_drawdown > threshold
        ):
            conflicts.add(conflict)
    if (
        profile.maximum_tolerable_drawdown < Decimal("0.10")
        and profile.reaction_10 is RiskReaction.HOLD
    ):
        conflicts.add(
            ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10
        )
    if profile.maximum_tolerable_loss == 0:
        if RiskReaction.HOLD in reactions:
            conflicts.add(
                ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_REACTIONS
            )
        if any(item.temporary_principal_loss_acceptable for item in profile.goals):
            conflicts.add(ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_GOALS)
    return _ordered_profile_conflicts(conflicts)


def _add_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(month=2, day=28, year=value.year + years)


def _validate_assessed_at(value: datetime) -> None:
    if type(value) is not datetime:
        raise ValueError("assessed_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("assessed_at must be timezone-aware")
