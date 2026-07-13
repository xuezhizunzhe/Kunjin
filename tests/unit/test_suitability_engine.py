from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import product

from kunjin.suitability.engine import evaluate
from kunjin.suitability.models import (
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
    Debt,
    FinancialGoal,
    IncomeStability,
    PlannedObligation,
    ProfileConflictCode,
    RiskReaction,
)
from kunjin.suitability.policy import SuitabilityPolicyV1
from tests.unit.test_suitability_models import valid_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
POLICY = SuitabilityPolicyV1()


def D(value: str) -> Decimal:
    return Decimal(value)


def debt(
    debt_type: str = "consumer_loan",
    principal: str = "1000",
    rate: str = "0.0799",
    payment: str = "100",
    *,
    delinquent: bool = False,
    revolving: bool = False,
) -> Debt:
    return Debt(
        debt_type=debt_type,
        outstanding_principal=D(principal),
        effective_annual_rate=D(rate),
        monthly_payment=D(payment),
        maturity_date=None,
        delinquent=delinquent,
        revolving_interest=revolving,
    )


def obligation(amount: str, reserved: str, due_date: date) -> PlannedObligation:
    return PlannedObligation("known expense", D(amount), due_date, D(reserved))


def goal(
    amount: str,
    reserved: str,
    target_date: date,
    *,
    priority: int = 1,
    loss_acceptable: bool = False,
    postponable: bool = False,
) -> FinancialGoal:
    return FinancialGoal(
        "known goal",
        D(amount),
        target_date,
        priority,
        D(reserved),
        loss_acceptable,
        postponable,
    )


def clear_profile(**changes: object):
    defaults = {
        "monthly_net_income": D("20000"),
        "monthly_essential_expenses": D("3000"),
        "monthly_required_debt_service": D("0"),
        "monthly_investment_ceiling": D("1000"),
        "minimum_monthly_cash_buffer": D("1000"),
        "immediately_available_cash": D("100000"),
        "cash_like_assets": D("0"),
        "emergency_reserve": D("100000"),
        "maximum_tolerable_loss": D("10000"),
        "maximum_tolerable_drawdown": D("0.20"),
        "reaction_10": RiskReaction.HOLD,
        "reaction_20": RiskReaction.REDUCE,
        "reaction_30": RiskReaction.REDEEM,
        "debts": (),
        "obligations": (),
        "goals": (),
    }
    defaults.update(changes)
    return replace(valid_profile(), **defaults)


class SuitabilityDebtGateTest(unittest.TestCase):
    def test_debt_reason_order_is_invariant_to_input_order(self) -> None:
        first = debt(debt_type="legacy", payment="100")
        second = debt(rate="0.08", payment="100", delinquent=True, revolving=True)
        forward = clear_profile(
            monthly_required_debt_service=D("200"),
            debts=(first, second),
        )
        reverse = replace(forward, debts=(second, first))

        forward_result = evaluate(forward, POLICY, NOW)
        reverse_result = evaluate(reverse, POLICY, NOW)

        self.assertEqual(forward_result.hard_blocks, reverse_result.hard_blocks)
        self.assertEqual(
            forward_result.profile_conflicts,
            reverse_result.profile_conflicts,
        )

    def test_itemized_debt_service_above_aggregate_blocks_and_is_used(self) -> None:
        profile = clear_profile(
            monthly_net_income=D("5000"),
            monthly_required_debt_service=D("0"),
            debts=(debt(debt_type="mortgage", payment="1500"),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.BLOCKED)
        self.assertEqual(
            result.profile_conflicts,
            (ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS,),
        )
        self.assertTrue(result.risk_answers_consistent)
        self.assertIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
        self.assertIn(
            BlockReason.NO_MONTHLY_INVESTABLE_CASH_FLOW,
            result.hard_blocks,
        )
        self.assertEqual(result.amounts.required_emergency_reserve, D("27000.00"))
        self.assertEqual(result.amounts.monthly_safety_residual, D("-500.00"))

    def test_aggregate_debt_service_above_itemized_is_used_without_conflict(self) -> None:
        profile = clear_profile(
            monthly_required_debt_service=D("200"),
            debts=(debt(debt_type="mortgage", payment="100"),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.profile_conflicts, ())
        self.assertNotIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
        self.assertEqual(result.amounts.required_emergency_reserve, D("19200.00"))
        self.assertEqual(result.amounts.monthly_safety_residual, D("15800.00"))

    def test_zero_principal_debt_payment_is_excluded_from_itemized_service(self) -> None:
        profile = clear_profile(
            debts=(
                debt(
                    debt_type="mortgage",
                    principal="0",
                    payment="999",
                ),
            ),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.profile_conflicts, ())
        self.assertNotIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
        self.assertEqual(result.amounts.required_emergency_reserve, D("18000.00"))
        self.assertEqual(result.amounts.monthly_safety_residual, D("16000.00"))

    def test_unsecured_consumer_debt_does_not_block_below_eight_percent(self) -> None:
        profile = replace(valid_profile(), debts=(debt(rate="0.0799"),))

        result = evaluate(profile, POLICY, NOW)

        self.assertNotIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)

    def test_unsecured_consumer_debt_blocks_at_eight_percent(self) -> None:
        profile = replace(valid_profile(), debts=(debt(rate="0.08"),))

        result = evaluate(profile, POLICY, NOW)

        self.assertIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)
        self.assertEqual(result.status, AssessmentStatus.BLOCKED)

    def test_all_consumer_debt_types_use_the_rate_gate(self) -> None:
        for debt_type in ("credit_card", "consumer_loan", "personal_loan"):
            with self.subTest(debt_type=debt_type):
                profile = replace(
                    valid_profile(), debts=(debt(debt_type=debt_type, rate="0.08"),)
                )
                result = evaluate(profile, POLICY, NOW)
                self.assertIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)

    def test_mortgage_is_not_rate_only_blocked(self) -> None:
        profile = replace(
            valid_profile(), debts=(debt(debt_type="mortgage", rate="0.12"),)
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertNotIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)

    def test_supported_nonconsumer_debts_are_not_rate_only_blocked(self) -> None:
        for debt_type in ("auto_loan", "student_loan"):
            with self.subTest(debt_type=debt_type):
                profile = replace(
                    valid_profile(), debts=(debt(debt_type=debt_type, rate="0.20"),)
                )
                result = evaluate(profile, POLICY, NOW)
                self.assertNotIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)
                self.assertNotIn(BlockReason.DEBT_TYPE_UNKNOWN, result.hard_blocks)

    def test_nonapprovable_and_non_normalized_nonzero_debt_types_block(self) -> None:
        for debt_type in (
            "business_loan",
            "other",
            "",
            "   ",
            "housing loan",
            "Consumer_Loan",
            " consumer_loan",
            "consumer_loan ",
        ):
            with self.subTest(debt_type=debt_type):
                profile = replace(valid_profile(), debts=(debt(debt_type=debt_type),))
                result = evaluate(profile, POLICY, NOW)
                self.assertIn(BlockReason.DEBT_TYPE_UNKNOWN, result.hard_blocks)

    def test_zero_principal_unknown_debt_does_not_return_debt_reasons(self) -> None:
        for debt_type in ("legacy debt", "", "   "):
            with self.subTest(debt_type=debt_type):
                profile = replace(
                    valid_profile(),
                    debts=(debt(debt_type=debt_type, principal="0"),),
                )

                result = evaluate(profile, POLICY, NOW)

                self.assertEqual(result.hard_blocks, ())

    def test_delinquency_and_revolving_interest_return_every_reason(self) -> None:
        profile = replace(
            valid_profile(),
            debts=(
                debt(
                    rate="0.08",
                    delinquent=True,
                    revolving=True,
                ),
            ),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(
            result.hard_blocks,
            (
                BlockReason.DEBT_DELINQUENT,
                BlockReason.REVOLVING_CREDIT,
                BlockReason.HIGH_INTEREST_DEBT,
            ),
        )

    def test_zero_principal_debt_flags_do_not_affect_assessment(self) -> None:
        profile = replace(
            valid_profile(),
            debts=(
                debt(
                    principal="0",
                    rate="0.50",
                    delinquent=True,
                    revolving=True,
                ),
            ),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertNotIn(BlockReason.DEBT_DELINQUENT, result.hard_blocks)
        self.assertNotIn(BlockReason.REVOLVING_CREDIT, result.hard_blocks)
        self.assertNotIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)


class SuitabilityReserveGateTest(unittest.TestCase):
    def test_verified_reserve_uses_smaller_supported_amount(self) -> None:
        profile = replace(
            valid_profile(),
            immediately_available_cash=D("40000"),
            cash_like_assets=D("10000"),
            emergency_reserve=D("80000"),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.verified_emergency_reserve, D("50000.00"))

    def test_designated_reserve_caps_verified_liquid_assets(self) -> None:
        profile = replace(
            valid_profile(),
            immediately_available_cash=D("40000"),
            cash_like_assets=D("10000"),
            emergency_reserve=D("30000"),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.verified_emergency_reserve, D("30000.00"))

    def test_stable_income_without_risk_signals_requires_six_months(self) -> None:
        result = evaluate(replace(valid_profile(), obligations=()), POLICY, NOW)

        self.assertEqual(result.required_reserve_months, 6)

    def test_variable_income_or_dependents_requires_nine_months(self) -> None:
        profiles = (
            replace(valid_profile(), income_stability=IncomeStability.VARIABLE),
            replace(valid_profile(), dependents=1),
        )
        for profile in profiles:
            with self.subTest(profile=profile):
                result = evaluate(profile, POLICY, NOW)
                self.assertEqual(result.required_reserve_months, 9)

    def test_unstable_income_or_interruption_risk_requires_twelve_months(self) -> None:
        profiles = (
            replace(valid_profile(), income_stability=IncomeStability.UNSTABLE),
            replace(valid_profile(), income_interruption_risk=True),
        )
        for profile in profiles:
            with self.subTest(profile=profile):
                result = evaluate(profile, POLICY, NOW)
                self.assertEqual(result.required_reserve_months, 12)

    def test_material_obligation_threshold_uses_aggregate_unfunded_gap(self) -> None:
        below = replace(
            valid_profile(),
            obligations=(
                obligation("3000", "0", date(2027, 7, 12)),
                obligation("1999.99", "0", date(2027, 7, 12)),
            ),
        )
        at_threshold = replace(
            below,
            obligations=(
                obligation("3000", "0", date(2027, 7, 12)),
                obligation("2000", "0", date(2027, 7, 12)),
            ),
        )

        self.assertEqual(evaluate(below, POLICY, NOW).required_reserve_months, 6)
        self.assertEqual(
            evaluate(at_threshold, POLICY, NOW).required_reserve_months, 12
        )

    def test_obligation_after_one_year_does_not_raise_reserve_months(self) -> None:
        profile = replace(
            valid_profile(),
            obligations=(obligation("5000", "0", date(2027, 7, 13)),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.required_reserve_months, 6)

    def test_required_reserve_includes_debt_service_and_one_year_obligations(self) -> None:
        profile = replace(
            valid_profile(),
            monthly_essential_expenses=D("1000"),
            monthly_required_debt_service=D("500"),
            immediately_available_cash=D("100000"),
            emergency_reserve=D("100000"),
            debts=(debt(debt_type="mortgage", payment="500"),),
            obligations=(obligation("1000", "250", date(2027, 7, 12)),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.required_reserve_months, 6)
        self.assertEqual(result.amounts.required_emergency_reserve, D("9750.00"))

    def test_fully_reserved_obligation_does_not_change_reserve(self) -> None:
        profile = replace(
            valid_profile(),
            obligations=(obligation("5000", "5000", date(2027, 7, 12)),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.required_reserve_months, 6)
        self.assertEqual(result.amounts.required_emergency_reserve, D("39000.00"))

    def test_equal_verified_and_required_reserve_is_not_a_shortfall(self) -> None:
        profile = replace(
            valid_profile(),
            monthly_essential_expenses=D("5000"),
            monthly_required_debt_service=D("0"),
            immediately_available_cash=D("30000"),
            cash_like_assets=D("0"),
            emergency_reserve=D("30000"),
            debts=(),
            obligations=(),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.emergency_reserve_shortfall, D("0.00"))
        self.assertNotIn(BlockReason.EMERGENCY_RESERVE_SHORTFALL, result.hard_blocks)

    def test_required_and_available_amounts_round_conservatively(self) -> None:
        profile = replace(
            valid_profile(),
            monthly_essential_expenses=D("0.001"),
            monthly_required_debt_service=D("0"),
            immediately_available_cash=D("0.009"),
            cash_like_assets=D("0"),
            emergency_reserve=D("0.009"),
            debts=(),
            obligations=(),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.verified_emergency_reserve, D("0.00"))
        self.assertEqual(result.amounts.required_emergency_reserve, D("0.01"))
        self.assertEqual(result.amounts.emergency_reserve_shortfall, D("0.01"))
        self.assertIn(BlockReason.EMERGENCY_RESERVE_SHORTFALL, result.hard_blocks)

    def test_reserve_shortfall_blocks_while_cash_flow_is_still_calculated(self) -> None:
        profile = replace(
            valid_profile(),
            immediately_available_cash=D("0"),
            cash_like_assets=D("0"),
            emergency_reserve=D("0"),
            obligations=(),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.BLOCKED)
        self.assertIn(BlockReason.EMERGENCY_RESERVE_SHORTFALL, result.hard_blocks)
        self.assertEqual(result.amounts.required_monthly_obligation_saving, D("0.00"))
        self.assertEqual(result.amounts.required_monthly_goal_saving, D("1978.03"))
        self.assertEqual(result.amounts.monthly_safety_residual, D("2521.97"))
        self.assertEqual(result.amounts.safe_monthly_ceiling, D("1000.00"))


class SuitabilityHorizonAndCashFlowTest(unittest.TestCase):
    def test_past_due_obligation_gap_blocks(self) -> None:
        profile = clear_profile(
            obligations=(obligation("1200", "200", date(2026, 7, 11)),)
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertIn(BlockReason.OBLIGATION_OVERDUE, result.hard_blocks)
        self.assertEqual(result.status, AssessmentStatus.BLOCKED)
        self.assertEqual(
            result.amounts.required_monthly_obligation_saving,
            D("1000.00"),
        )

    def test_obligation_exactly_one_year_is_in_reserve_and_monthly_saving(self) -> None:
        profile = clear_profile(
            monthly_essential_expenses=D("1000"),
            obligations=(obligation("1300", "0", date(2027, 7, 12)),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.required_emergency_reserve, D("13300.00"))
        self.assertEqual(
            result.amounts.required_monthly_obligation_saving,
            D("100.00"),
        )
        self.assertNotIn(
            ConstraintReason.NEAR_TERM_OBLIGATION_GAP,
            result.constraints,
        )

    def test_obligation_after_one_year_through_exactly_three_years_constrains(self) -> None:
        profiles = (
            clear_profile(
                obligations=(obligation("2500", "0", date(2027, 7, 13)),)
            ),
            clear_profile(
                obligations=(obligation("3700", "0", date(2029, 7, 12)),)
            ),
        )

        for profile in profiles:
            with self.subTest(due_date=profile.obligations[0].due_date):
                result = evaluate(profile, POLICY, NOW)
                self.assertIn(
                    ConstraintReason.NEAR_TERM_OBLIGATION_GAP,
                    result.constraints,
                )

        exact_three_years = evaluate(profiles[1], POLICY, NOW)
        self.assertEqual(
            exact_three_years.amounts.required_monthly_obligation_saving,
            D("100.00"),
        )

    def test_obligation_after_three_years_does_not_change_state_or_cash_flow(self) -> None:
        profile = clear_profile(
            obligations=(obligation("10000", "0", date(2029, 7, 13)),)
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(result.constraints, ())
        self.assertEqual(
            result.amounts.required_monthly_obligation_saving,
            D("0.00"),
        )

    def test_fully_reserved_short_term_items_do_not_block_or_constrain(self) -> None:
        profile = clear_profile(
            obligations=(obligation("1000", "1000", date(2026, 7, 11)),),
            goals=(goal("2000", "2000", date(2026, 7, 11)),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(result.hard_blocks, ())
        self.assertEqual(result.constraints, ())

    def test_goal_overdue_and_unpostponable_priority_one_within_one_year_block(
        self,
    ) -> None:
        profile = clear_profile(
            goals=(
                goal("1000", "0", date(2026, 7, 11), postponable=True),
                goal("12000", "0", date(2027, 7, 12)),
            )
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertIn(BlockReason.GOAL_OVERDUE, result.hard_blocks)
        self.assertIn(BlockReason.CRITICAL_GOAL_SHORTFALL, result.hard_blocks)

    def test_postponable_or_lower_priority_goal_within_one_year_is_not_critical(
        self,
    ) -> None:
        profiles = (
            clear_profile(
                goals=(goal("1200", "0", date(2027, 7, 12), postponable=True),)
            ),
            clear_profile(
                goals=(goal("1200", "0", date(2027, 7, 12), priority=2),)
            ),
        )

        for profile in profiles:
            with self.subTest(goal=profile.goals[0]):
                result = evaluate(profile, POLICY, NOW)
                self.assertNotIn(
                    BlockReason.CRITICAL_GOAL_SHORTFALL,
                    result.hard_blocks,
                )

    def test_goal_exactly_three_years_constrains(self) -> None:
        result = evaluate(
            clear_profile(
                goals=(goal("3700", "0", date(2029, 7, 12), priority=2),)
            ),
            POLICY,
            NOW,
        )

        self.assertEqual(
            result.constraints,
            (ConstraintReason.NEAR_TERM_GOAL_GAP,),
        )
        self.assertEqual(result.status, AssessmentStatus.CONSTRAINED)

    def test_goal_one_day_after_three_years_does_not_change_state(self) -> None:
        result = evaluate(
            clear_profile(
                goals=(goal("3700", "0", date(2029, 7, 13), priority=2),)
            ),
            POLICY,
            NOW,
        )

        self.assertEqual(result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(result.constraints, ())

    def test_priority_one_goal_after_three_years_still_requires_monthly_saving(
        self,
    ) -> None:
        result = evaluate(
            clear_profile(
                goals=(
                    goal("3800", "0", date(2029, 8, 1), postponable=True),
                )
            ),
            POLICY,
            NOW,
        )

        self.assertEqual(result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(result.amounts.required_monthly_goal_saving, D("100.00"))

    def test_only_priority_one_goal_gaps_create_required_monthly_saving(self) -> None:
        profile = clear_profile(
            goals=(
                goal("700", "0", date(2027, 1, 1), postponable=True),
                goal("7000", "0", date(2027, 1, 1), priority=2),
            )
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.amounts.required_monthly_goal_saving, D("100.00"))

    def test_monthly_savings_sum_then_round_up_and_residual_rounds_down(self) -> None:
        profile = clear_profile(
            monthly_net_income=D("1000.019"),
            monthly_essential_expenses=D("100"),
            minimum_monthly_cash_buffer=D("0"),
            obligations=(
                obligation("1", "0", date(2026, 9, 1)),
                obligation("1", "0", date(2026, 9, 30)),
            ),
            goals=(goal("1", "0", date(2026, 9, 1), postponable=True),),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(
            result.amounts.required_monthly_obligation_saving,
            D("0.67"),
        )
        self.assertEqual(result.amounts.required_monthly_goal_saving, D("0.34"))
        self.assertEqual(result.amounts.monthly_safety_residual, D("899.00"))

    def test_same_month_and_leap_day_boundaries_use_calendar_periods(self) -> None:
        leap_now = datetime(2028, 2, 29, 12, tzinfo=timezone.utc)
        same_month = evaluate(
            clear_profile(
                obligations=(obligation("1", "0", date(2028, 2, 29)),)
            ),
            POLICY,
            leap_now,
        )
        exact_year = evaluate(
            clear_profile(
                monthly_essential_expenses=D("2"),
                obligations=(obligation("1", "0", date(2029, 2, 28)),),
            ),
            POLICY,
            leap_now,
        )
        day_after = evaluate(
            clear_profile(
                obligations=(obligation("1", "0", date(2029, 3, 1)),)
            ),
            POLICY,
            leap_now,
        )

        self.assertEqual(
            same_month.amounts.required_monthly_obligation_saving,
            D("1.00"),
        )
        self.assertNotIn(
            ConstraintReason.NEAR_TERM_OBLIGATION_GAP,
            exact_year.constraints,
        )
        self.assertIn(
            ConstraintReason.NEAR_TERM_OBLIGATION_GAP,
            day_after.constraints,
        )

    def test_nonpositive_monthly_residual_blocks_and_safe_ceiling_is_zero(self) -> None:
        for income in ("5000", "4999.99"):
            with self.subTest(income=income):
                profile = clear_profile(
                    monthly_net_income=D(income),
                    monthly_essential_expenses=D("3000"),
                    monthly_required_debt_service=D("1000"),
                    minimum_monthly_cash_buffer=D("1000"),
                )
                result = evaluate(profile, POLICY, NOW)
                self.assertIn(
                    BlockReason.NO_MONTHLY_INVESTABLE_CASH_FLOW,
                    result.hard_blocks,
                )
                self.assertEqual(result.status, AssessmentStatus.BLOCKED)
                self.assertEqual(result.amounts.safe_monthly_ceiling, D("0.00"))

    def test_positive_residual_below_personal_ceiling_is_constrained(self) -> None:
        profile = clear_profile(
            monthly_net_income=D("10000"),
            monthly_essential_expenses=D("7000"),
            monthly_required_debt_service=D("1000"),
            minimum_monthly_cash_buffer=D("1000"),
            monthly_investment_ceiling=D("2000"),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.CONSTRAINED)
        self.assertIn(
            ConstraintReason.MONTHLY_CEILING_CONSTRAINED,
            result.constraints,
        )
        self.assertEqual(result.amounts.safe_monthly_ceiling, D("1000.00"))

    def test_residual_at_personal_ceiling_is_ready(self) -> None:
        profile = clear_profile(
            monthly_net_income=D("6000"),
            monthly_essential_expenses=D("4000"),
            minimum_monthly_cash_buffer=D("1000"),
            monthly_investment_ceiling=D("1000"),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(result.amounts.safe_monthly_ceiling, D("1000.00"))


class SuitabilityRiskConsistencyTest(unittest.TestCase):
    def test_every_risk_reaction_ordering_uses_non_decreasing_severity(self) -> None:
        severity = dict(POLICY.risk_reaction_severity)
        for reactions in product(RiskReaction, repeat=3):
            with self.subTest(reactions=reactions):
                profile = clear_profile(
                    maximum_tolerable_drawdown=D("0.10"),
                    reaction_10=reactions[0],
                    reaction_20=reactions[1],
                    reaction_30=reactions[2],
                )
                result = evaluate(profile, POLICY, NOW)
                expected = all(
                    severity[current] <= severity[later]
                    for current, later in zip(reactions, reactions[1:])
                )
                self.assertEqual(result.risk_answers_consistent, expected)
                expected_conflicts = []
                if severity[reactions[0]] > severity[reactions[1]]:
                    expected_conflicts.append(
                        ProfileConflictCode.REACTION_10_VS_REACTION_20
                    )
                if severity[reactions[1]] > severity[reactions[2]]:
                    expected_conflicts.append(
                        ProfileConflictCode.REACTION_20_VS_REACTION_30
                    )
                self.assertEqual(
                    result.profile_conflicts,
                    tuple(expected_conflicts),
                )
                self.assertEqual(
                    BlockReason.PROFILE_CONFLICT in result.hard_blocks,
                    not expected,
                )

    def test_redeem_threshold_below_declared_tolerance_conflicts(self) -> None:
        for reactions, drawdown, expected_conflict in (
            (
                (RiskReaction.REDEEM, RiskReaction.REDEEM, RiskReaction.REDEEM),
                "0.11",
                ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10,
            ),
            (
                (RiskReaction.REDUCE, RiskReaction.REDEEM, RiskReaction.REDEEM),
                "0.21",
                ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_20,
            ),
            (
                (RiskReaction.REDUCE, RiskReaction.REDUCE, RiskReaction.REDEEM),
                "0.31",
                ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_30,
            ),
        ):
            with self.subTest(reactions=reactions):
                changes = {
                    "maximum_tolerable_drawdown": D(drawdown),
                    "reaction_10": reactions[0],
                    "reaction_20": reactions[1],
                    "reaction_30": reactions[2],
                }
                result = evaluate(clear_profile(**changes), POLICY, NOW)
                self.assertIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
                self.assertIn(expected_conflict, result.profile_conflicts)

    def test_redeem_at_declared_tolerance_is_consistent(self) -> None:
        profiles = (
            clear_profile(
                maximum_tolerable_drawdown=D("0.10"),
                reaction_10=RiskReaction.REDEEM,
                reaction_20=RiskReaction.REDEEM,
                reaction_30=RiskReaction.REDEEM,
            ),
            clear_profile(
                maximum_tolerable_drawdown=D("0.20"),
                reaction_10=RiskReaction.REDUCE,
                reaction_20=RiskReaction.REDEEM,
                reaction_30=RiskReaction.REDEEM,
            ),
            clear_profile(
                maximum_tolerable_drawdown=D("0.30"),
                reaction_10=RiskReaction.REDUCE,
                reaction_20=RiskReaction.REDUCE,
                reaction_30=RiskReaction.REDEEM,
            ),
        )

        for profile in profiles:
            with self.subTest(drawdown=profile.maximum_tolerable_drawdown):
                result = evaluate(profile, POLICY, NOW)
                self.assertTrue(result.risk_answers_consistent)
                self.assertNotIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)

    def test_exactly_ten_percent_tolerance_with_hold_does_not_conflict(self) -> None:
        result = evaluate(
            clear_profile(maximum_tolerable_drawdown=D("0.10")),
            POLICY,
            NOW,
        )

        self.assertNotIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)

    def test_sub_ten_percent_tolerance_with_hold_at_ten_conflicts(self) -> None:
        profile = clear_profile(maximum_tolerable_drawdown=D("0.09"))

        result = evaluate(profile, POLICY, NOW)

        self.assertIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
        self.assertEqual(
            result.profile_conflicts,
            (ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10,),
        )

    def test_zero_loss_with_hold_or_loss_accepting_goal_conflicts(self) -> None:
        profiles = (
            clear_profile(maximum_tolerable_loss=D("0")),
            clear_profile(
                maximum_tolerable_loss=D("0"),
                reaction_10=RiskReaction.REDUCE,
                reaction_20=RiskReaction.REDUCE,
                reaction_30=RiskReaction.REDEEM,
                goals=(
                    goal(
                        "0",
                        "0",
                        date(2030, 1, 1),
                        loss_acceptable=True,
                        postponable=True,
                    ),
                ),
            ),
        )

        for profile in profiles:
            with self.subTest(profile=profile):
                result = evaluate(profile, POLICY, NOW)
                self.assertIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)
                self.assertEqual(result.status, AssessmentStatus.BLOCKED)
        self.assertIn(
            ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_REACTIONS,
            evaluate(profiles[0], POLICY, NOW).profile_conflicts,
        )
        self.assertIn(
            ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_GOALS,
            evaluate(profiles[1], POLICY, NOW).profile_conflicts,
        )

    def test_consistent_defensive_profile_does_not_conflict(self) -> None:
        profile = clear_profile(
            maximum_tolerable_loss=D("0"),
            maximum_tolerable_drawdown=D("0.10"),
            reaction_10=RiskReaction.REDUCE,
            reaction_20=RiskReaction.REDEEM,
            reaction_30=RiskReaction.REDEEM,
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertTrue(result.risk_answers_consistent)
        self.assertNotIn(BlockReason.PROFILE_CONFLICT, result.hard_blocks)

    def test_all_applicable_risk_conflicts_are_returned_in_enum_order(self) -> None:
        profile = clear_profile(
            maximum_tolerable_loss=D("0"),
            maximum_tolerable_drawdown=D("0.31"),
            reaction_10=RiskReaction.REDEEM,
            reaction_20=RiskReaction.REDUCE,
            reaction_30=RiskReaction.HOLD,
            goals=(
                goal(
                    "0",
                    "0",
                    date(2030, 1, 1),
                    loss_acceptable=True,
                    postponable=True,
                ),
            ),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(
            result.profile_conflicts,
            (
                ProfileConflictCode.REACTION_10_VS_REACTION_20,
                ProfileConflictCode.REACTION_20_VS_REACTION_30,
                ProfileConflictCode.MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10,
                ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_REACTIONS,
                ProfileConflictCode.MAXIMUM_TOLERABLE_LOSS_VS_GOALS,
            ),
        )


class SuitabilityAggregationTest(unittest.TestCase):
    def test_simultaneous_issues_return_all_reasons_in_enum_order(self) -> None:
        profile = clear_profile(
            monthly_net_income=D("100"),
            monthly_essential_expenses=D("5000"),
            monthly_required_debt_service=D("0"),
            immediately_available_cash=D("0"),
            emergency_reserve=D("0"),
            reaction_10=RiskReaction.REDUCE,
            reaction_20=RiskReaction.HOLD,
            debts=(debt(rate="0.08", delinquent=True, revolving=True),),
            obligations=(obligation("1000", "0", date(2026, 7, 11)),),
            goals=(
                goal("1000", "0", date(2026, 7, 11), postponable=True),
                goal("1000", "0", date(2027, 7, 12)),
                goal("1000", "0", date(2028, 1, 1), priority=2),
            ),
        )

        result = evaluate(profile, POLICY, NOW)

        self.assertEqual(
            result.hard_blocks,
            (
                BlockReason.DEBT_DELINQUENT,
                BlockReason.REVOLVING_CREDIT,
                BlockReason.HIGH_INTEREST_DEBT,
                BlockReason.EMERGENCY_RESERVE_SHORTFALL,
                BlockReason.OBLIGATION_OVERDUE,
                BlockReason.GOAL_OVERDUE,
                BlockReason.CRITICAL_GOAL_SHORTFALL,
                BlockReason.NO_MONTHLY_INVESTABLE_CASH_FLOW,
                BlockReason.PROFILE_CONFLICT,
            ),
        )
        self.assertEqual(
            result.constraints,
            (ConstraintReason.NEAR_TERM_GOAL_GAP,),
        )
        self.assertEqual(
            result.profile_conflicts,
            (
                ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS,
                ProfileConflictCode.REACTION_10_VS_REACTION_20,
            ),
        )
        self.assertEqual(result.status, AssessmentStatus.BLOCKED)


class SuitabilitySafetyMonotonicityTest(unittest.TestCase):
    _SEVERITY = {
        AssessmentStatus.READY_FOR_ALLOCATION: 0,
        AssessmentStatus.CONSTRAINED: 1,
        AssessmentStatus.BLOCKED: 2,
    }

    def assert_strictly_worse(self, safer, riskier) -> None:
        safer_result = evaluate(safer, POLICY, NOW)
        riskier_result = evaluate(riskier, POLICY, NOW)
        self.assertGreater(
            self._SEVERITY[riskier_result.status],
            self._SEVERITY[safer_result.status],
        )

    def test_less_reserve_never_improves_state(self) -> None:
        safer = clear_profile(emergency_reserve=D("18000"))
        riskier = replace(safer, emergency_reserve=D("17999.99"))
        self.assert_strictly_worse(safer, riskier)

    def test_more_debt_never_improves_state(self) -> None:
        safer = clear_profile(monthly_net_income=D("5000"))
        riskier = replace(
            safer,
            debts=(debt(debt_type="mortgage", principal="1", payment="1"),),
            monthly_required_debt_service=D("1"),
        )
        self.assert_strictly_worse(safer, riskier)

    def test_higher_supported_consumer_rate_never_improves_state(self) -> None:
        safer = clear_profile(
            monthly_required_debt_service=D("100"),
            debts=(debt(rate="0.0799"),),
        )
        riskier = replace(safer, debts=(debt(rate="0.08"),))
        self.assert_strictly_worse(safer, riskier)

    def test_shorter_goal_date_never_improves_state(self) -> None:
        safer = clear_profile(
            goals=(goal("1200", "0", date(2029, 7, 13), postponable=True),)
        )
        riskier = replace(
            safer,
            goals=(goal("1200", "0", date(2029, 7, 12), postponable=True),),
        )
        self.assert_strictly_worse(safer, riskier)

    def test_larger_obligation_gap_never_improves_state(self) -> None:
        safer = clear_profile(
            obligations=(obligation("100", "100", date(2028, 1, 1)),)
        )
        riskier = replace(
            safer,
            obligations=(obligation("100", "99.99", date(2028, 1, 1)),),
        )
        self.assert_strictly_worse(safer, riskier)

    def test_lower_income_never_improves_state(self) -> None:
        safer = clear_profile(monthly_net_income=D("5000"))
        riskier = replace(safer, monthly_net_income=D("3999.99"))
        self.assert_strictly_worse(safer, riskier)

    def test_higher_essential_expenses_never_improves_state(self) -> None:
        safer = clear_profile(
            monthly_net_income=D("5000"),
            monthly_essential_expenses=D("3000"),
        )
        riskier = replace(safer, monthly_essential_expenses=D("3000.01"))
        self.assert_strictly_worse(safer, riskier)


if __name__ == "__main__":
    unittest.main()
