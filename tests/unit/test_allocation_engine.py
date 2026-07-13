from __future__ import annotations

import dataclasses
import unittest
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from itertools import product

from kunjin.allocation.engine import (
    AllocationCapitalInputs,
    AllocationInputs,
    _contribution_periods,
    _horizon_ceiling,
    build_allocation_inputs,
    evaluate_allocation,
)
from kunjin.allocation.models import (
    REGION_INEQUALITIES,
    AllocationBlockCode,
    AllocationConstraintCode,
    AllocationProfileConflictCode,
    AllocationSleeveKind,
    AllocationStatus,
    GoalFundingState,
)
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.suitability.engine import evaluate as evaluate_suitability
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
    RiskReaction,
)
from kunjin.suitability.policy import SuitabilityPolicyV1
from tests.unit.test_suitability_models import valid_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
POLICY = AllocationPolicyV1()


def D(value: str) -> Decimal:
    return Decimal(value)


def goal(
    name: str,
    target_date: date,
    *,
    target: str = "1000.00",
    reserved: str = "0.00",
    priority: int = 2,
    postponable: bool = False,
) -> FinancialGoal:
    return FinancialGoal(
        name=name,
        target_amount=D(target),
        target_date=target_date,
        priority=priority,
        amount_already_reserved=D(reserved),
        temporary_principal_loss_acceptable=False,
        use_date_can_be_postponed=postponable,
    )


def obligation(
    name: str,
    due_date: date,
    *,
    amount: str = "1000.00",
    reserved: str = "0.00",
) -> PlannedObligation:
    return PlannedObligation(name, D(amount), due_date, D(reserved))


def profile(**changes: object):
    defaults = {
        "immediately_available_cash": D("30000.00"),
        "cash_like_assets": D("20000.00"),
        "emergency_reserve": D("20000.00"),
        "minimum_operating_cash": D("5000.00"),
        "low_risk_fixed_income_assets": D("10000.00"),
        "manual_equity_fund_assets": D("10000.00"),
        "manual_bond_fund_assets": D("0.00"),
        "manual_sector_fund_assets": D("0.00"),
        "other_volatile_assets": D("0.00"),
        "debts": (),
        "obligations": (),
        "goals": (goal("purpose", date(2031, 7, 12)),),
        "can_postpone_goal_use": False,
    }
    defaults.update(changes)
    return replace(valid_profile(), **defaults)


def assessment(
    *,
    status: AssessmentStatus = AssessmentStatus.READY_FOR_ALLOCATION,
    constraints: tuple[ConstraintReason, ...] = (),
    verified_reserve: str = "20000.00",
    obligation_saving: str = "0.00",
    goal_saving: str = "0.00",
    safe_ceiling: str = "800.00",
) -> AssessmentResult:
    hard_blocks = (
        () if status is not AssessmentStatus.BLOCKED else (BlockReason.EMERGENCY_RESERVE_SHORTFALL,)
    )
    return AssessmentResult(
        status=status,
        hard_blocks=hard_blocks,
        constraints=constraints,
        required_reserve_months=6,
        risk_answers_consistent=True,
        profile_conflicts=(),
        debt_count=0,
        obligation_count=0,
        goal_count=0,
        amounts=AssessmentAmounts(
            verified_emergency_reserve=D(verified_reserve),
            required_emergency_reserve=D(verified_reserve),
            emergency_reserve_shortfall=D("0.00"),
            required_monthly_obligation_saving=D(obligation_saving),
            required_monthly_goal_saving=D(goal_saving),
            monthly_safety_residual=D("1000.00"),
            safe_monthly_ceiling=D(safe_ceiling),
        ),
    )


class AllocationCalendarTest(unittest.TestCase):
    def test_same_instant_offset_representations_use_the_same_utc_assessment_date(self) -> None:
        utc_instant = datetime(2026, 7, 12, 11, tzinfo=timezone.utc)
        assessed_times = (
            utc_instant,
            utc_instant.astimezone(timezone(timedelta(hours=14))),
            utc_instant.astimezone(timezone(timedelta(hours=-12))),
        )
        boundary_profile = profile(
            goals=(goal("utc boundary", date(2027, 7, 12)),),
        )
        results = tuple(
            evaluate_allocation(
                boundary_profile,
                assessment(),
                POLICY,
                assessed_at,
            )
            for assessed_at in assessed_times
        )
        self.assertTrue(all(result == results[0] for result in results[1:]))
        assert results[0].exact is not None
        self.assertEqual(results[0].exact.assessment_date, date(2026, 7, 12))
        self.assertEqual(
            results[0].exact.goal_funding_details[0].horizon_equity_ceiling,
            D("0.00"),
        )

    def test_exact_horizon_boundaries_and_adjacent_days(self) -> None:
        cases = (
            (date(2027, 7, 11), D("0")),
            (date(2027, 7, 12), D("0")),
            (date(2027, 7, 13), D("0.10")),
            (date(2029, 7, 11), D("0.10")),
            (date(2029, 7, 12), D("0.10")),
            (date(2029, 7, 13), D("0.30")),
            (date(2031, 7, 11), D("0.30")),
            (date(2031, 7, 12), D("0.30")),
            (date(2031, 7, 13), D("0.50")),
            (date(2034, 7, 11), D("0.50")),
            (date(2034, 7, 12), D("0.50")),
            (date(2034, 7, 13), D("0.70")),
        )
        for target, expected in cases:
            with self.subTest(target=target):
                self.assertEqual(_horizon_ceiling(NOW.date(), target, POLICY), expected)

    def test_leap_day_anniversary_is_clamped(self) -> None:
        as_of = date(2028, 2, 29)
        self.assertEqual(_horizon_ceiling(as_of, date(2029, 2, 28), POLICY), D("0"))
        self.assertEqual(_horizon_ceiling(as_of, date(2029, 3, 1), POLICY), D("0.10"))

    def test_horizon_saturates_at_date_max(self) -> None:
        self.assertEqual(_horizon_ceiling(date.max, date.max, POLICY), D("0"))

    def test_contribution_periods_match_phase_b_calendar_months(self) -> None:
        self.assertEqual(_contribution_periods(NOW.date(), date(2026, 7, 31)), 1)
        self.assertEqual(_contribution_periods(NOW.date(), date(2027, 7, 12)), 13)


class AllocationInputBuilderTest(unittest.TestCase):
    def test_trust_boundary_rejects_dataclass_and_scalar_subclasses(self) -> None:
        class ProfileSubclass(FinancialProfile):
            def validate(self) -> None:
                raise AssertionError("must not call overridden validate")

        class DecimalSubclass(Decimal):
            def __add__(self, other: object) -> Decimal:
                raise AssertionError("must not invoke malicious Decimal arithmetic")

        class StringSubclass(str):
            pass

        class DateSubclass(date):
            pass

        base = profile()
        mutations = (
            ProfileSubclass(**base.__dict__),
            replace(base, immediately_available_cash=DecimalSubclass("30000.00")),
            replace(base, currency=StringSubclass("CNY")),
            replace(
                base,
                goals=(
                    replace(
                        base.goals[0],
                        target_date=DateSubclass(2031, 7, 12),
                    ),
                ),
            ),
        )
        for invalid in mutations:
            with self.subTest(invalid=type(invalid)):
                with self.assertRaisesRegex(ValueError, "exact"):
                    build_allocation_inputs(invalid, assessment(), POLICY, NOW)

    def test_trust_boundary_rejects_nested_dataclass_and_container_subclasses(self) -> None:
        class DebtSubclass(Debt):
            pass

        class TupleSubclass(tuple):
            pass

        base = profile()
        debt = Debt("mortgage", D("1"), D("0"), D("0"), None, False, False)
        invalid_profiles = (
            replace(base, debts=(DebtSubclass(**debt.__dict__),)),
            replace(base, goals=TupleSubclass(base.goals)),
        )
        for invalid in invalid_profiles:
            with self.assertRaisesRegex(ValueError, "exact"):
                build_allocation_inputs(invalid, assessment(), POLICY, NOW)

    def test_trust_boundary_rejects_datetime_and_timezone_subclasses(self) -> None:
        class DateTimeSubclass(datetime):
            pass

        class CustomTimezone(tzinfo):
            def utcoffset(self, value: datetime | None):
                return None

        invalid_times = (
            DateTimeSubclass(2026, 7, 12, 12, tzinfo=timezone.utc),
            datetime(2026, 7, 12, 12, tzinfo=CustomTimezone()),
        )
        for invalid in invalid_times:
            with self.assertRaisesRegex(ValueError, "exact"):
                build_allocation_inputs(profile(), assessment(), POLICY, invalid)

    def test_trust_boundary_rejects_extra_dataclass_state(self) -> None:
        stateful = profile()
        object.__setattr__(stateful, "unexpected_state", True)
        with self.assertRaisesRegex(ValueError, "state"):
            build_allocation_inputs(stateful, assessment(), POLICY, NOW)

    def test_trust_boundary_rejects_subcent_profile_money_fields(self) -> None:
        money_fields = (
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
        )
        for field_name in money_fields:
            for invalid in (D("0.001"), D("0.005"), D("0.009")):
                with self.subTest(field_name=field_name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "CNY cents"):
                        build_allocation_inputs(
                            replace(profile(), **{field_name: invalid}),
                            assessment(),
                            POLICY,
                            NOW,
                        )

    def test_trust_boundary_rejects_subcent_nested_and_assessment_money(self) -> None:
        base = profile()
        debt = Debt("mortgage", D("1.00"), D("0.01"), D("1.00"), None, False, False)
        nested_profiles = (
            replace(base, debts=(replace(debt, outstanding_principal=D("1.005")),)),
            replace(base, debts=(replace(debt, monthly_payment=D("1.009")),)),
            replace(
                base,
                obligations=(obligation("bill", date(2027, 7, 12), amount="1.001"),),
            ),
            replace(
                base,
                goals=(goal("purpose", date(2031, 7, 12), target="1.005"),),
            ),
        )
        for invalid in nested_profiles:
            with self.assertRaisesRegex(ValueError, "CNY cents"):
                build_allocation_inputs(invalid, assessment(), POLICY, NOW)

        base_assessment = assessment()
        for item in dataclasses.fields(AssessmentAmounts):
            invalid_amounts = replace(
                base_assessment.amounts,
                **{item.name: D("0.009")},
            )
            with self.subTest(field_name=item.name):
                with self.assertRaisesRegex(ValueError, "CNY cents"):
                    build_allocation_inputs(
                        base,
                        replace(base_assessment, amounts=invalid_amounts),
                        POLICY,
                        NOW,
                    )

    def test_single_and_multiple_subcent_reserved_amounts_never_disappear(self) -> None:
        profiles = (
            profile(
                goals=(
                    goal(
                        "short",
                        date(2029, 7, 12),
                        target="1.00",
                        reserved="0.005",
                    ),
                )
            ),
            profile(
                goals=(
                    goal(
                        "long one",
                        date(2032, 7, 12),
                        target="1.00",
                        reserved="0.001",
                    ),
                    goal(
                        "long two",
                        date(2033, 7, 12),
                        target="1.00",
                        reserved="0.009",
                    ),
                )
            ),
        )
        for invalid in profiles:
            with self.assertRaisesRegex(ValueError, "CNY cents"):
                build_allocation_inputs(invalid, assessment(), POLICY, NOW)

    def test_inputs_are_frozen(self) -> None:
        inputs = build_allocation_inputs(profile(), assessment(), POLICY, NOW)
        fields = {item.name for item in dataclasses.fields(AllocationInputs)}
        capital_fields = {item.name for item in dataclasses.fields(AllocationCapitalInputs)}
        self.assertIn("capital", fields)
        self.assertNotIn("exact", fields)
        self.assertNotIn("aggregate_inputs", capital_fields)
        self.assertIn("assessment_date", capital_fields)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inputs.blocks = ()
        assert inputs.capital is not None
        with self.assertRaises(dataclasses.FrozenInstanceError):
            inputs.capital.total_financial_assets = D("0.00")

    def test_engine_dataclasses_reject_top_level_and_nested_injected_state(self) -> None:
        injected_fields = (
            "target_allocation",
            "recommended_allocation",
            "selected_mix",
            "private_amount",
        )
        for field_name in injected_fields:
            top_level = build_allocation_inputs(profile(), assessment(), POLICY, NOW)
            object.__setattr__(top_level, field_name, D("123456.78"))
            with self.subTest(level="top", field_name=field_name):
                with self.assertRaisesRegex(ValueError, "unexpected state"):
                    top_level.validate()

            nested = build_allocation_inputs(profile(), assessment(), POLICY, NOW)
            assert nested.capital is not None
            object.__setattr__(nested.capital, field_name, D("123456.78"))
            with self.subTest(level="nested", field_name=field_name):
                with self.assertRaisesRegex(ValueError, "unexpected state"):
                    nested.validate()

    def test_capital_isolation_uses_exact_asset_and_claim_formulas(self) -> None:
        inputs = build_allocation_inputs(profile(), assessment(), POLICY, NOW)
        capital = inputs.capital
        assert capital is not None
        self.assertEqual(capital.total_financial_assets, D("70000.00"))
        self.assertEqual(capital.liquid_protection_assets, D("50000.00"))
        self.assertEqual(capital.verified_emergency_reserve, D("20000.00"))
        self.assertEqual(capital.minimum_operating_cash, D("5000.00"))
        self.assertEqual(capital.protected_short_term_assigned, D("0.00"))
        self.assertEqual(capital.protected_liquid_claims, D("25000.00"))
        self.assertEqual(capital.investable_stock_assets, D("45000.00"))

    def test_short_term_reserved_claims_include_exact_three_year_boundary(self) -> None:
        items = (obligation("bill", date(2029, 7, 12), reserved="1000.00"),)
        goals = (
            goal("soon", date(2029, 7, 12), target="2000.00", reserved="2000.00"),
            goal("later", date(2029, 7, 13), target="4000.00", reserved="3000.00"),
        )
        inputs = build_allocation_inputs(
            profile(obligations=items, goals=goals), assessment(), POLICY, NOW
        )
        capital = inputs.capital
        assert capital is not None
        self.assertEqual(capital.protected_short_term_assigned, D("3000.00"))
        self.assertEqual(
            tuple((item.sleeve_kind, item.name) for item in capital.assigned_sleeves),
            (
                (AllocationSleeveKind.GOAL, "later"),
                (AllocationSleeveKind.RESIDUAL, "residual"),
            ),
        )

    def test_supported_claims_equal_liquidity_are_allowed(self) -> None:
        inputs = build_allocation_inputs(
            profile(minimum_operating_cash=D("30000.00")), assessment(), POLICY, NOW
        )
        self.assertEqual(inputs.blocks, ())
        assert inputs.capital is not None
        self.assertEqual(inputs.capital.protected_liquid_claims, D("50000.00"))

    def test_claims_above_liquidity_block_without_capital_result(self) -> None:
        inputs = build_allocation_inputs(
            profile(minimum_operating_cash=D("30000.01")), assessment(), POLICY, NOW
        )
        self.assertEqual(
            inputs.blocks,
            (AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL,),
        )
        self.assertIsNone(inputs.capital)

    def test_protected_components_cannot_be_shifted_into_residual(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "short",
                        date(2029, 7, 12),
                        target="100.00",
                        reserved="100.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                )
            ),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        capital = inputs.capital
        with self.assertRaisesRegex(ValueError, "protected liquid claims"):
            replace(capital, verified_emergency_reserve=D("19999.99")).validate()
        with self.assertRaisesRegex(ValueError, "protected liquid claims"):
            replace(capital, minimum_operating_cash=D("4999.99")).validate()

        residual = next(
            item
            for item in capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        expanded = replace(
            residual,
            assigned_amount=residual.assigned_amount + D("100.00"),
            weighted_equity_contribution=(residual.assigned_amount + D("100.00"))
            * residual.horizon_equity_ceiling,
        )
        shifted = replace(
            capital,
            protected_liquid_claims=capital.protected_liquid_claims - D("100.00"),
            investable_stock_assets=capital.investable_stock_assets + D("100.00"),
            assigned_sleeves=(expanded,),
        )
        with self.assertRaisesRegex(ValueError, "protected liquid claims"):
            shifted.validate()
        with self.assertRaisesRegex(ValueError, "protected short-term assigned"):
            replace(shifted, protected_short_term_assigned=D("0.00")).validate()

    def test_long_term_assigned_sleeves_below_investable_stock_are_allowed(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "assigned goal",
                        date(2032, 7, 12),
                        target="20000.00",
                        reserved="20000.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                ),
                obligations=(
                    obligation(
                        "assigned obligation",
                        date(2032, 7, 12),
                        amount="20000.00",
                        reserved="20000.00",
                    ),
                ),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        self.assertEqual(inputs.blocks, ())
        assert inputs.capital is not None
        self.assertEqual(inputs.capital.investable_stock_assets, D("45000.00"))
        self.assertEqual(
            sum(
                (
                    item.assigned_amount
                    for item in inputs.capital.assigned_sleeves
                    if item.sleeve_kind is not AllocationSleeveKind.RESIDUAL
                ),
                D("0.00"),
            ),
            D("40000.00"),
        )
        residual = tuple(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        self.assertEqual(len(residual), 1)
        self.assertEqual(residual[0].assigned_amount, D("5000.00"))

    def test_long_term_assigned_sleeves_equal_investable_stock_are_allowed(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "assigned goal",
                        date(2032, 7, 12),
                        target="22500.00",
                        reserved="22500.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                ),
                obligations=(
                    obligation(
                        "assigned obligation",
                        date(2032, 7, 12),
                        amount="22500.00",
                        reserved="22500.00",
                    ),
                ),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        self.assertEqual(inputs.blocks, ())
        assert inputs.capital is not None
        self.assertIsNone(inputs.capital.residual_horizon_date)
        self.assertEqual(len(inputs.capital.assigned_sleeves), 2)

    def test_long_term_assigned_sleeves_above_investable_stock_block(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "assigned goal",
                        date(2032, 7, 12),
                        target="22500.00",
                        reserved="22500.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                ),
                obligations=(
                    obligation(
                        "assigned obligation",
                        date(2032, 7, 12),
                        amount="22500.01",
                        reserved="22500.01",
                    ),
                ),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        self.assertEqual(
            inputs.blocks,
            (AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL,),
        )
        self.assertIsNone(inputs.capital)

    def test_horizon_missing_and_long_term_overlap_are_both_reported(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "fully assigned",
                        date(2032, 7, 12),
                        target="45000.01",
                        reserved="45000.01",
                    ),
                ),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        self.assertEqual(
            inputs.blocks,
            (
                AllocationBlockCode.ALLOCATION_HORIZON_MISSING,
                AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL,
            ),
        )
        self.assertIsNone(inputs.capital)

    def test_duplicate_sleeves_use_complete_counter_identity(self) -> None:
        goals = (
            goal(
                "same",
                date(2032, 7, 12),
                target="100.00",
                reserved="100.00",
            ),
            goal(
                "same",
                date(2032, 7, 12),
                target="200.00",
                reserved="200.00",
            ),
            goal("purpose", date(2033, 7, 12)),
        )
        forward = build_allocation_inputs(profile(goals=goals), assessment(), POLICY, NOW)
        reverse = build_allocation_inputs(
            profile(goals=tuple(reversed(goals))), assessment(), POLICY, NOW
        )
        self.assertEqual(forward, reverse)
        assert forward.capital is not None
        available = Counter(
            (
                item.sleeve_kind,
                item.name,
                item.horizon_date,
                item.assigned_amount,
                item.horizon_equity_ceiling,
            )
            for item in forward.capital.assigned_sleeves
            if item.sleeve_kind is not AllocationSleeveKind.RESIDUAL
        )
        self.assertEqual(sum(available.values()), 2)

    def test_fully_identical_reserved_sleeves_are_allowed_but_excess_is_rejected(self) -> None:
        duplicate = goal(
            "same",
            date(2032, 7, 12),
            target="100.00",
            reserved="100.00",
        )
        inputs = build_allocation_inputs(
            profile(goals=(duplicate, duplicate, goal("purpose", date(2033, 7, 12)))),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        non_residual = tuple(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is not AllocationSleeveKind.RESIDUAL
        )
        self.assertEqual(len(non_residual), 2)
        residual = next(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        reduced_residual = replace(
            residual,
            assigned_amount=residual.assigned_amount - D("100.00"),
            weighted_equity_contribution=(residual.assigned_amount - D("100.00"))
            * residual.horizon_equity_ceiling,
        )
        tampered = replace(
            inputs.capital,
            assigned_sleeves=non_residual + (non_residual[0], reduced_residual),
        )
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            tampered.validate()

    def test_goal_sleeve_cannot_be_omitted_into_residual(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "assigned",
                        date(2032, 7, 12),
                        target="100.00",
                        reserved="100.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                )
            ),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        assigned = next(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.GOAL
        )
        residual = next(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        expanded = replace(
            residual,
            assigned_amount=residual.assigned_amount + assigned.assigned_amount,
            weighted_equity_contribution=(residual.assigned_amount + assigned.assigned_amount)
            * residual.horizon_equity_ceiling,
        )
        tampered = replace(inputs.capital, assigned_sleeves=(expanded,))
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            tampered.validate()

    def test_obligation_sleeve_cannot_be_omitted_into_residual(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                obligations=(
                    obligation(
                        "assigned",
                        date(2032, 7, 12),
                        amount="100.00",
                        reserved="100.00",
                    ),
                )
            ),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        assigned = next(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.OBLIGATION
        )
        residual = next(
            item
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        expanded = replace(
            residual,
            assigned_amount=residual.assigned_amount + assigned.assigned_amount,
            weighted_equity_contribution=(residual.assigned_amount + assigned.assigned_amount)
            * residual.horizon_equity_ceiling,
        )
        tampered = replace(inputs.capital, assigned_sleeves=(expanded,))
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            tampered.validate()

    def test_three_year_boundary_controls_goal_and_obligation_sleeves(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                goals=(
                    goal(
                        "goal before",
                        date(2029, 7, 11),
                        target="5.00",
                        reserved="5.00",
                    ),
                    goal(
                        "goal boundary",
                        date(2029, 7, 12),
                        target="10.00",
                        reserved="10.00",
                    ),
                    goal(
                        "goal boundary",
                        date(2029, 7, 12),
                        target="10.00",
                        reserved="10.00",
                    ),
                    goal(
                        "goal after",
                        date(2029, 7, 13),
                        target="20.00",
                        reserved="20.00",
                    ),
                    goal("purpose", date(2033, 7, 12)),
                ),
                obligations=(
                    obligation(
                        "obligation before",
                        date(2029, 7, 11),
                        amount="15.00",
                        reserved="15.00",
                    ),
                    obligation(
                        "obligation boundary",
                        date(2029, 7, 12),
                        amount="30.00",
                        reserved="30.00",
                    ),
                    obligation(
                        "obligation after",
                        date(2029, 7, 13),
                        amount="40.00",
                        reserved="40.00",
                    ),
                ),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        names = {
            item.name
            for item in inputs.capital.assigned_sleeves
            if item.sleeve_kind is not AllocationSleeveKind.RESIDUAL
        }
        self.assertEqual(names, {"goal after", "obligation after"})
        self.assertEqual(inputs.capital.assessment_date, NOW.date())
        self.assertEqual(inputs.capital.protected_short_term_assigned, D("70.00"))

    def test_large_exact_amounts_and_ambient_decimal_context_are_stable(self) -> None:
        huge = D("1E28")
        precise = profile(
            immediately_available_cash=huge,
            cash_like_assets=D("0.01"),
            emergency_reserve=D("0.00"),
            minimum_operating_cash=D("0.00"),
            low_risk_fixed_income_assets=D("0.00"),
            manual_equity_fund_assets=D("0.00"),
        )
        expected = build_allocation_inputs(
            precise, assessment(verified_reserve="0.00"), POLICY, NOW
        )
        with localcontext() as context:
            context.prec = 2
            context.Emax = 9
            context.Emin = -9
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            actual = build_allocation_inputs(
                precise,
                assessment(verified_reserve="0.00"),
                POLICY,
                NOW,
            )
        self.assertEqual(actual, expected)
        assert actual.capital is not None
        self.assertEqual(
            actual.capital.total_financial_assets,
            D("10000000000000000000000000000.01"),
        )

    def test_1e100_is_supported_and_pathological_exponent_is_stable_value_error(self) -> None:
        supported = profile(
            immediately_available_cash=D("1E100"),
            cash_like_assets=D("0.00"),
            emergency_reserve=D("0.00"),
            minimum_operating_cash=D("0.00"),
            low_risk_fixed_income_assets=D("0.00"),
            manual_equity_fund_assets=D("0.00"),
        )
        result = build_allocation_inputs(
            supported, assessment(verified_reserve="0.00"), POLICY, NOW
        )
        self.assertEqual(result.blocks, ())
        pathological = replace(supported, immediately_available_cash=D("1E10001"))
        with self.assertRaisesRegex(ValueError, "safely"):
            build_allocation_inputs(
                pathological,
                assessment(verified_reserve="0.00"),
                POLICY,
                NOW,
            )

    def test_phase_b_monthly_ceiling_is_not_reduced_again(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                obligations=(obligation("bill", date(2027, 7, 12), amount="130.00"),),
                goals=(goal("purpose", date(2027, 7, 12), target="130.00", priority=1),),
            ),
            assessment(obligation_saving="10.00", goal_saving="10.00", safe_ceiling="800.00"),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        self.assertEqual(
            inputs.capital.monthly_discretionary_allocation_ceiling,
            D("800.00"),
        )

    def test_goal_zero_return_states_use_only_authenticated_priority_one_saving(self) -> None:
        goals = (
            goal("funded", date(2027, 7, 12), target="100.00", reserved="100.00", priority=1),
            goal("fundable", date(2027, 7, 12), target="130.00", reserved="0.00", priority=1),
            goal("gap", date(2027, 7, 12), target="131.00", reserved="0.00", priority=2),
            goal("lower", date(2027, 7, 12), target="1.00", priority=2),
        )
        inputs = build_allocation_inputs(
            profile(goals=goals), assessment(goal_saving="10.00"), POLICY, NOW
        )
        assert inputs.capital is not None
        details = {item.name: item for item in inputs.capital.goal_funding_details}
        self.assertEqual(details["funded"].funding_state, GoalFundingState.FULLY_FUNDED_NOW)
        self.assertEqual(
            details["fundable"].funding_state,
            GoalFundingState.FUNDABLE_WITHOUT_RETURN,
        )
        self.assertEqual(
            details["gap"].funding_state,
            GoalFundingState.FUNDING_GAP_WITHOUT_RETURN,
        )
        self.assertEqual(details["lower"].confirmed_monthly_saving, D("0.00"))
        self.assertEqual(
            sum((item.confirmed_monthly_saving for item in details.values()), D("0.00")),
            D("10.00"),
        )

    def test_obligation_saving_is_only_for_short_term_gaps(self) -> None:
        obligations = (
            obligation("a", date(2027, 7, 12), amount="130.00"),
            obligation("b", date(2029, 7, 13), amount="999.00"),
        )
        inputs = build_allocation_inputs(
            profile(obligations=obligations), assessment(obligation_saving="10.00"), POLICY, NOW
        )
        assert inputs.capital is not None
        details = {item.name: item for item in inputs.capital.obligation_funding_details}
        self.assertEqual(details["a"].confirmed_monthly_saving, D("10.00"))
        self.assertEqual(details["b"].confirmed_monthly_saving, D("0.00"))

    def test_residual_horizon_uses_earliest_positive_gap_priority_one_goal(self) -> None:
        goals = (
            goal("later", date(2034, 7, 12), priority=1),
            goal("earlier", date(2031, 7, 12), priority=1),
            goal("soon", date(2028, 7, 12), priority=2),
        )
        inputs = build_allocation_inputs(
            profile(goals=goals), assessment(goal_saving="26.71"), POLICY, NOW
        )
        assert inputs.capital is not None
        self.assertEqual(inputs.capital.residual_horizon_date, date(2031, 7, 12))

    def test_residual_horizon_falls_back_to_earliest_other_positive_gap_goal(self) -> None:
        goals = (
            goal("funded priority", date(2030, 7, 12), target="1.00", reserved="1.00", priority=1),
            goal("later", date(2033, 7, 12), priority=2),
            goal("earlier", date(2032, 7, 12), priority=3),
        )
        inputs = build_allocation_inputs(profile(goals=goals), assessment(), POLICY, NOW)
        assert inputs.capital is not None
        self.assertEqual(inputs.capital.residual_horizon_date, date(2032, 7, 12))

    def test_no_positive_gap_goal_blocks(self) -> None:
        inputs = build_allocation_inputs(profile(goals=()), assessment(), POLICY, NOW)
        self.assertEqual(inputs.blocks, (AllocationBlockCode.ALLOCATION_HORIZON_MISSING,))
        self.assertIsNone(inputs.capital)

    def test_goal_order_does_not_change_capital_calculation(self) -> None:
        goals = (
            goal("b", date(2027, 7, 12), target="130.00", priority=1),
            goal("a", date(2027, 7, 12), target="260.00", priority=1),
        )
        forward = build_allocation_inputs(
            profile(goals=goals), assessment(goal_saving="30.00"), POLICY, NOW
        )
        reverse = build_allocation_inputs(
            profile(goals=tuple(reversed(goals))), assessment(goal_saving="30.00"), POLICY, NOW
        )
        self.assertEqual(forward, reverse)

    def test_goal_largest_remainder_uses_canonical_tie_break_and_is_order_invariant(
        self,
    ) -> None:
        goals = (
            goal("b", date(2026, 9, 1), target="1.00", priority=1),
            goal("a", date(2026, 9, 1), target="1.00", priority=1),
        )
        forward = build_allocation_inputs(
            profile(goals=goals), assessment(goal_saving="0.67"), POLICY, NOW
        )
        reverse = build_allocation_inputs(
            profile(goals=tuple(reversed(goals))),
            assessment(goal_saving="0.67"),
            POLICY,
            NOW,
        )
        self.assertEqual(forward, reverse)
        assert forward.capital is not None
        savings = {
            item.name: item.confirmed_monthly_saving
            for item in forward.capital.goal_funding_details
        }
        self.assertEqual(savings, {"a": D("0.34"), "b": D("0.33")})
        self.assertEqual(sum(savings.values(), D("0.00")), D("0.67"))

    def test_goal_largest_remainder_prefers_the_larger_fraction(self) -> None:
        goals = (
            goal("a", date(2026, 9, 1), target="1.00", priority=1),
            goal("z", date(2026, 9, 1), target="1.01", priority=1),
        )
        inputs = build_allocation_inputs(
            profile(goals=goals), assessment(goal_saving="0.67"), POLICY, NOW
        )
        assert inputs.capital is not None
        savings = {
            item.name: item.confirmed_monthly_saving for item in inputs.capital.goal_funding_details
        }
        self.assertEqual(savings, {"a": D("0.33"), "z": D("0.34")})

    def test_identical_goal_duplicates_are_interchangeable(self) -> None:
        duplicate = goal("same", date(2026, 9, 1), target="1.00", priority=1)
        inputs = build_allocation_inputs(
            profile(goals=(duplicate, duplicate)),
            assessment(goal_saving="0.67"),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        self.assertEqual(
            tuple(item.confirmed_monthly_saving for item in inputs.capital.goal_funding_details),
            (D("0.34"), D("0.33")),
        )

    def test_obligation_largest_remainder_uses_fraction_and_canonical_tie_break(
        self,
    ) -> None:
        obligations = (
            obligation("b", date(2026, 9, 1), amount="1.00"),
            obligation("a", date(2026, 9, 1), amount="1.00"),
        )
        forward = build_allocation_inputs(
            profile(obligations=obligations),
            assessment(obligation_saving="0.67"),
            POLICY,
            NOW,
        )
        reverse = build_allocation_inputs(
            profile(obligations=tuple(reversed(obligations))),
            assessment(obligation_saving="0.67"),
            POLICY,
            NOW,
        )
        self.assertEqual(forward, reverse)
        assert forward.capital is not None
        savings = {
            item.name: item.confirmed_monthly_saving
            for item in forward.capital.obligation_funding_details
        }
        self.assertEqual(savings, {"a": D("0.34"), "b": D("0.33")})
        self.assertEqual(sum(savings.values(), D("0.00")), D("0.67"))

    def test_profile_level_postponement_conflict_blocks(self) -> None:
        conflicting = profile(
            can_postpone_goal_use=False,
            goals=(goal("purpose", date(2031, 7, 12), postponable=True),),
        )
        inputs = build_allocation_inputs(conflicting, assessment(), POLICY, NOW)
        self.assertEqual(inputs.blocks, (AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT,))
        self.assertEqual(
            inputs.profile_conflicts,
            (AllocationProfileConflictCode.PROFILE_DISALLOWS_GOAL_POSTPONEMENT,),
        )
        self.assertIsNone(inputs.capital)

    def test_blocked_suitability_stops_before_capital_calculation(self) -> None:
        inputs = build_allocation_inputs(
            profile(), assessment(status=AssessmentStatus.BLOCKED), POLICY, NOW
        )
        self.assertEqual(inputs.blocks, (AllocationBlockCode.SUITABILITY_BLOCKED,))
        self.assertIsNone(inputs.capital)

    def test_phase_b_constraints_are_preserved_in_enum_order(self) -> None:
        inputs = build_allocation_inputs(
            profile(),
            assessment(
                status=AssessmentStatus.CONSTRAINED,
                constraints=(
                    ConstraintReason.MONTHLY_CEILING_CONSTRAINED,
                    ConstraintReason.NEAR_TERM_GOAL_GAP,
                ),
            ),
            POLICY,
            NOW,
        )
        self.assertEqual(
            inputs.inherited_constraints,
            (
                AllocationConstraintCode.NEAR_TERM_GOAL_GAP,
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
            ),
        )

    def test_authenticated_goal_saving_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "authenticated monthly goal saving"):
            build_allocation_inputs(
                profile(goals=(goal("purpose", date(2027, 7, 12), target="130.00", priority=1),)),
                assessment(goal_saving="9.99"),
                POLICY,
                NOW,
            )

    def test_authenticated_obligation_saving_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "authenticated monthly obligation saving"):
            build_allocation_inputs(
                profile(obligations=(obligation("bill", date(2027, 7, 12), amount="130.00"),)),
                assessment(obligation_saving="9.99"),
                POLICY,
                NOW,
            )

    def test_zero_investable_stock_has_no_sleeves_or_residual_horizon(self) -> None:
        inputs = build_allocation_inputs(
            profile(
                immediately_available_cash=D("25000.00"),
                cash_like_assets=D("0.00"),
                low_risk_fixed_income_assets=D("0.00"),
                manual_equity_fund_assets=D("0.00"),
                manual_bond_fund_assets=D("0.00"),
                manual_sector_fund_assets=D("0.00"),
                other_volatile_assets=D("0.00"),
            ),
            assessment(),
            POLICY,
            NOW,
        )
        assert inputs.capital is not None
        self.assertEqual(inputs.capital.investable_stock_assets, D("0.00"))
        self.assertEqual(inputs.capital.assigned_sleeves, ())
        self.assertIsNone(inputs.capital.residual_horizon_date)


class AllocationFeasibleRegionTest(unittest.TestCase):
    def evaluate(self, **profile_changes: object):
        return evaluate_allocation(profile(**profile_changes), assessment(), POLICY, NOW)

    def continuous_absolute_equity_boundary(self, result) -> Decimal:
        assert result.exact is not None
        aggregate = result.exact.aggregate_inputs
        investable = result.exact.investable_stock_assets
        equity_stress = aggregate.equity_stress_loss
        return min(
            aggregate.weighted_horizon_numerator,
            result.exact.maximum_tolerable_loss / equity_stress,
            investable * result.exact.maximum_tolerable_drawdown / equity_stress,
            investable * aggregate.willingness_equity_ceiling,
            investable * aggregate.stability_equity_ceiling,
        )

    def evaluate_protected_demand(self, verified_reserve: str):
        current_profile = profile(
            monthly_essential_expenses=D("1000.00"),
            monthly_required_debt_service=D("0.00"),
            emergency_reserve=D(verified_reserve),
            maximum_tolerable_loss=D("7000.00"),
            maximum_tolerable_drawdown=D("1"),
            reaction_10=RiskReaction.HOLD,
            reaction_20=RiskReaction.HOLD,
            reaction_30=RiskReaction.HOLD,
            experienced_material_loss=True,
            understands_multi_year_recovery=True,
            goals=(goal("purpose", date(2035, 7, 12)),),
        )
        current_assessment = evaluate_suitability(
            current_profile,
            SuitabilityPolicyV1(),
            NOW,
        )
        self.assertEqual(current_assessment.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(
            current_assessment.amounts.verified_emergency_reserve,
            D(verified_reserve),
        )
        self.assertEqual(
            current_assessment,
            evaluate_suitability(current_profile, SuitabilityPolicyV1(), NOW),
        )
        return evaluate_allocation(current_profile, current_assessment, POLICY, NOW)

    def test_blocked_inputs_return_amount_free_validated_result(self) -> None:
        result = evaluate_allocation(
            profile(),
            assessment(
                status=AssessmentStatus.BLOCKED,
                constraints=(ConstraintReason.MONTHLY_CEILING_CONSTRAINED,),
            ),
            POLICY,
            NOW,
        )
        self.assertEqual(result.status, AllocationStatus.BLOCKED)
        self.assertEqual(result.capability, "research_only")
        self.assertEqual(result.blocks, (AllocationBlockCode.SUITABILITY_BLOCKED,))
        self.assertEqual(
            result.binding_constraints,
            (AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,),
        )
        self.assertIsNone(result.permitted_region)
        self.assertIsNone(result.exact)
        self.assertEqual(result.safe_summary.horizon_equity_ceilings, ())
        result.validate()

    def test_region_contains_fixed_inequalities_and_exact_aggregate_inputs(self) -> None:
        result = self.evaluate(maximum_tolerable_loss=D("6750.00"))
        self.assertEqual(result.status, AllocationStatus.RANGE_AVAILABLE)
        assert result.permitted_region is not None
        assert result.exact is not None
        self.assertEqual(result.permitted_region.inequalities, REGION_INEQUALITIES)
        self.assertEqual(result.permitted_region.maximum_equity, D("0.30"))
        self.assertEqual(result.permitted_region.horizon_equity_ceiling, D("0.30"))
        self.assertEqual(result.permitted_region.loss_amount_equity_ceiling, D("0.30"))
        self.assertEqual(
            result.binding_constraints,
            (
                AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN,
                AllocationConstraintCode.HORIZON_BINDING,
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
            ),
        )
        self.assertEqual(
            result.exact.aggregate_inputs.weighted_horizon_numerator,
            D("13500.0000"),
        )
        self.assertEqual(result.exact.aggregate_inputs.fixed_income_stress_loss, D("0.10"))
        self.assertEqual(result.exact.aggregate_inputs.equity_stress_loss, D("0.50"))
        result.validate()

    def test_loss_budget_exact_equality_and_one_cent_boundary(self) -> None:
        exact = self.evaluate(maximum_tolerable_loss=D("6750.00"))
        below = self.evaluate(maximum_tolerable_loss=D("6749.99"))
        assert exact.permitted_region is not None
        assert below.permitted_region is not None
        self.assertEqual(exact.permitted_region.loss_amount_equity_ceiling, D("0.30"))
        self.assertEqual(below.permitted_region.loss_amount_equity_ceiling, D("0.29"))
        self.assertIn(
            AllocationConstraintCode.LOSS_AMOUNT_BINDING,
            below.binding_constraints,
        )

    def test_all_equal_ceilings_are_reported_as_binding(self) -> None:
        result = self.evaluate(
            maximum_tolerable_loss=D("6750.00"),
            maximum_tolerable_drawdown=D("0.15"),
            reaction_10=RiskReaction.HOLD,
            reaction_20=RiskReaction.REDUCE,
            income_stability=IncomeStability.VARIABLE,
            dependents=1,
        )
        self.assertEqual(
            result.binding_constraints,
            (
                AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN,
                AllocationConstraintCode.HORIZON_BINDING,
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                AllocationConstraintCode.DRAWDOWN_BINDING,
                AllocationConstraintCode.WILLINGNESS_BINDING,
                AllocationConstraintCode.STABILITY_BINDING,
            ),
        )
        result.validate()

    def test_zero_loss_or_drawdown_budget_keeps_cash_only_feasible(self) -> None:
        cases = (
            (
                {"maximum_tolerable_loss": D("0.00")},
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
            ),
            (
                {"maximum_tolerable_drawdown": D("0")},
                AllocationConstraintCode.DRAWDOWN_BINDING,
            ),
        )
        for changes, binding in cases:
            with self.subTest(changes=changes):
                result = self.evaluate(**changes)
                assert result.permitted_region is not None
                self.assertEqual(result.status, AllocationStatus.RANGE_AVAILABLE)
                self.assertEqual(result.permitted_region.maximum_equity, D("0.00"))
                self.assertIn(binding, result.binding_constraints)
                result.validate()

    def test_zero_stock_has_no_region_and_preserves_non_region_constraints(self) -> None:
        result = evaluate_allocation(
            profile(
                immediately_available_cash=D("25000.00"),
                cash_like_assets=D("0.00"),
                low_risk_fixed_income_assets=D("0.00"),
                manual_equity_fund_assets=D("0.00"),
                manual_bond_fund_assets=D("0.00"),
                manual_sector_fund_assets=D("0.00"),
                other_volatile_assets=D("0.00"),
            ),
            assessment(
                status=AssessmentStatus.CONSTRAINED,
                constraints=(ConstraintReason.MONTHLY_CEILING_CONSTRAINED,),
            ),
            POLICY,
            NOW,
        )
        self.assertEqual(result.status, AllocationStatus.RANGE_AVAILABLE)
        self.assertIsNone(result.permitted_region)
        self.assertEqual(
            result.binding_constraints,
            (
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
                AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN,
                AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK,
            ),
        )
        result.validate()

    def test_all_reaction_experience_and_recovery_combinations(self) -> None:
        reactions = tuple(RiskReaction)
        for reaction_10 in reactions:
            for reaction_20 in reactions:
                for reaction_30 in reactions:
                    for experienced in (False, True):
                        for recovery_aware in (False, True):
                            if reaction_10 is not RiskReaction.HOLD:
                                expected = D("0.10")
                            elif reaction_20 is not RiskReaction.HOLD:
                                expected = D("0.30")
                            elif reaction_30 is not RiskReaction.HOLD:
                                expected = D("0.50")
                            elif experienced and recovery_aware:
                                expected = D("0.70")
                            else:
                                expected = D("0.50")
                            result = self.evaluate(
                                reaction_10=reaction_10,
                                reaction_20=reaction_20,
                                reaction_30=reaction_30,
                                experienced_material_loss=experienced,
                                understands_multi_year_recovery=recovery_aware,
                            )
                            assert result.exact is not None
                            with self.subTest(
                                reaction_10=reaction_10,
                                reaction_20=reaction_20,
                                reaction_30=reaction_30,
                                experienced=experienced,
                                recovery_aware=recovery_aware,
                            ):
                                self.assertEqual(
                                    result.exact.aggregate_inputs.willingness_equity_ceiling,
                                    expected,
                                )

    def test_inconsistent_reactions_use_earliest_defensive_threshold_in_pure_engine(self) -> None:
        # Task 7 must reject an authenticated mismatch before this pure fallback is reachable.
        result = self.evaluate(
            reaction_10=RiskReaction.REDUCE,
            reaction_20=RiskReaction.HOLD,
            reaction_30=RiskReaction.HOLD,
        )
        assert result.exact is not None
        self.assertEqual(
            result.exact.aggregate_inputs.willingness_equity_ceiling,
            D("0.10"),
        )
        result.validate()

    def test_all_stability_dependents_and_interruption_combinations(self) -> None:
        for stability in IncomeStability:
            for dependents in (0, 2):
                for interruption in (False, True):
                    if stability is IncomeStability.UNSTABLE:
                        expected = D("0.20")
                    elif interruption:
                        expected = D("0.30")
                    elif stability is IncomeStability.STABLE and dependents == 0:
                        expected = D("0.70")
                    elif stability is IncomeStability.VARIABLE and dependents > 0:
                        expected = D("0.30")
                    else:
                        expected = D("0.50")
                    result = self.evaluate(
                        income_stability=stability,
                        dependents=dependents,
                        income_interruption_risk=interruption,
                    )
                    assert result.exact is not None
                    with self.subTest(
                        stability=stability,
                        dependents=dependents,
                        interruption=interruption,
                    ):
                        self.assertEqual(
                            result.exact.aggregate_inputs.stability_equity_ceiling,
                            expected,
                        )

    def test_each_safety_axis_is_monotone(self) -> None:
        axes = (
            (
                "loss_amount_equity_ceiling",
                tuple(
                    {"maximum_tolerable_loss": D(value)}
                    for value in ("22500", "6750", "6749.99", "0")
                ),
            ),
            (
                "drawdown_equity_ceiling",
                tuple(
                    {"maximum_tolerable_drawdown": D(value)}
                    for value in ("0.50", "0.15", "0.149", "0")
                ),
            ),
            (
                "weighted_horizon_equity_ceiling",
                tuple(
                    {"goals": (goal("purpose", target),)}
                    for target in (
                        date(2034, 7, 13),
                        date(2034, 7, 12),
                        date(2031, 7, 12),
                        date(2029, 7, 12),
                        date(2027, 7, 12),
                    )
                ),
            ),
            (
                "willingness_equity_ceiling",
                (
                    {
                        "reaction_10": RiskReaction.HOLD,
                        "reaction_20": RiskReaction.HOLD,
                        "reaction_30": RiskReaction.HOLD,
                        "experienced_material_loss": True,
                        "understands_multi_year_recovery": True,
                    },
                    {"reaction_10": RiskReaction.HOLD, "reaction_20": RiskReaction.HOLD},
                    {"reaction_10": RiskReaction.HOLD, "reaction_20": RiskReaction.REDUCE},
                    {"reaction_10": RiskReaction.REDUCE},
                ),
            ),
            (
                "stability_equity_ceiling",
                (
                    {"income_stability": IncomeStability.STABLE, "dependents": 0},
                    {"income_stability": IncomeStability.STABLE, "dependents": 1},
                    {"income_stability": IncomeStability.VARIABLE, "dependents": 1},
                    {"income_stability": IncomeStability.UNSTABLE},
                ),
            ),
        )
        for field_name, changes_sequence in axes:
            observed = []
            for changes in changes_sequence:
                result = self.evaluate(**changes)
                assert result.exact is not None
                observed.append(getattr(result.exact.aggregate_inputs, field_name))
            with self.subTest(field_name=field_name):
                self.assertEqual(observed, sorted(observed, reverse=True))

    def test_full_constraint_matrix_never_widens_when_any_axis_tightens(self) -> None:
        axis_values = (
            (
                {"goals": (goal("purpose", date(2034, 7, 13)),)},
                {"goals": (goal("purpose", date(2034, 7, 12)),)},
                {"goals": (goal("purpose", date(2031, 7, 12)),)},
            ),
            tuple(
                {"maximum_tolerable_loss": D(value)}
                for value in ("22500.00", "11250.00", "6750.00")
            ),
            tuple({"maximum_tolerable_drawdown": D(value)} for value in ("0.50", "0.25", "0.15")),
            (
                {
                    "reaction_10": RiskReaction.HOLD,
                    "reaction_20": RiskReaction.HOLD,
                    "reaction_30": RiskReaction.HOLD,
                    "experienced_material_loss": True,
                    "understands_multi_year_recovery": True,
                },
                {
                    "reaction_10": RiskReaction.HOLD,
                    "reaction_20": RiskReaction.HOLD,
                    "reaction_30": RiskReaction.HOLD,
                },
                {
                    "reaction_10": RiskReaction.HOLD,
                    "reaction_20": RiskReaction.REDUCE,
                },
            ),
            (
                {"income_stability": IncomeStability.STABLE, "dependents": 0},
                {"income_stability": IncomeStability.STABLE, "dependents": 1},
                {"income_stability": IncomeStability.VARIABLE, "dependents": 1},
            ),
        )
        maximum_by_index = {}
        for indices in product(range(3), repeat=len(axis_values)):
            changes = {}
            for axis_index, value_index in enumerate(indices):
                changes.update(axis_values[axis_index][value_index])
            result = self.evaluate(**changes)
            assert result.permitted_region is not None
            maximum_by_index[indices] = result.permitted_region.maximum_equity

        for indices, maximum in maximum_by_index.items():
            for axis_index, value_index in enumerate(indices):
                if value_index == 0:
                    continue
                wider_indices = list(indices)
                wider_indices[axis_index] -= 1
                with self.subTest(indices=indices, tightened_axis=axis_index):
                    self.assertLessEqual(
                        maximum,
                        maximum_by_index[tuple(wider_indices)],
                    )

    def test_higher_protected_demand_never_increases_continuous_absolute_risk(self) -> None:
        lower_demand = self.evaluate_protected_demand("20000.00")
        higher_demand = self.evaluate_protected_demand("20000.01")
        self.assertLessEqual(
            self.continuous_absolute_equity_boundary(higher_demand),
            self.continuous_absolute_equity_boundary(lower_demand),
        )
        for result in (lower_demand, higher_demand):
            assert result.exact is not None
            assert result.permitted_region is not None
            displayed_stress = (
                result.exact.investable_stock_assets
                * result.exact.aggregate_inputs.equity_stress_loss
                * result.permitted_region.maximum_equity
            )
            self.assertLessEqual(displayed_stress, result.exact.maximum_tolerable_loss)

    def test_protected_demand_denominator_scaling_with_display_rounding(self) -> None:
        lower_demand = self.evaluate_protected_demand("20000.00")
        higher_demand = self.evaluate_protected_demand("25000.00")
        assert lower_demand.exact is not None
        assert lower_demand.permitted_region is not None
        assert higher_demand.exact is not None
        assert higher_demand.permitted_region is not None

        self.assertEqual(lower_demand.permitted_region.maximum_equity, D("0.31"))
        self.assertEqual(higher_demand.permitted_region.maximum_equity, D("0.35"))
        self.assertEqual(
            self.continuous_absolute_equity_boundary(higher_demand),
            self.continuous_absolute_equity_boundary(lower_demand),
        )

        for result in (lower_demand, higher_demand):
            assert result.exact is not None
            assert result.permitted_region is not None
            investable = result.exact.investable_stock_assets
            displayed_equity = investable * result.permitted_region.maximum_equity
            continuous_boundary = self.continuous_absolute_equity_boundary(result)
            self.assertLessEqual(displayed_equity, continuous_boundary)
            self.assertLess(
                continuous_boundary - displayed_equity,
                investable * POLICY.percentage_quantum,
            )
            self.assertLessEqual(
                displayed_equity * result.exact.aggregate_inputs.equity_stress_loss,
                result.exact.maximum_tolerable_loss,
            )
            result_fields = {item.name for item in dataclasses.fields(result)}
            region_fields = {item.name for item in dataclasses.fields(result.permitted_region)}
            self.assertFalse(
                any(
                    marker in field_name
                    for field_name in result_fields | region_fields
                    for marker in ("target", "recommended", "selected")
                )
            )

    def test_inherited_phase_b_constraints_do_not_widen_the_region(self) -> None:
        current_profile = profile(
            monthly_essential_expenses=D("1000.00"),
            monthly_required_debt_service=D("0.00"),
            emergency_reserve=D("20000.00"),
        )
        ready = evaluate_suitability(
            current_profile,
            SuitabilityPolicyV1(),
            NOW,
        )
        self.assertEqual(ready.status, AssessmentStatus.READY_FOR_ALLOCATION)
        constrained_cases = (
            (ConstraintReason.MONTHLY_CEILING_CONSTRAINED,),
            (
                ConstraintReason.MONTHLY_CEILING_CONSTRAINED,
                ConstraintReason.NEAR_TERM_GOAL_GAP,
                ConstraintReason.NEAR_TERM_OBLIGATION_GAP,
            ),
        )
        ready_result = evaluate_allocation(current_profile, ready, POLICY, NOW)
        assert ready_result.permitted_region is not None
        ready_boundary = self.continuous_absolute_equity_boundary(ready_result)

        # Task 7 authenticates the constrained snapshot; this isolates Phase C inheritance only.
        for constraints in constrained_cases:
            constrained = replace(
                ready,
                status=AssessmentStatus.CONSTRAINED,
                constraints=constraints,
            )
            constrained_result = evaluate_allocation(
                current_profile,
                constrained,
                POLICY,
                NOW,
            )
            assert constrained_result.permitted_region is not None
            with self.subTest(constraints=constraints):
                self.assertEqual(
                    constrained_result.permitted_region.maximum_equity,
                    ready_result.permitted_region.maximum_equity,
                )
                self.assertEqual(
                    self.continuous_absolute_equity_boundary(constrained_result),
                    ready_boundary,
                )
                self.assertEqual(
                    tuple(
                        code
                        for code in constrained_result.binding_constraints
                        if code
                        in {
                            AllocationConstraintCode.NEAR_TERM_OBLIGATION_GAP,
                            AllocationConstraintCode.NEAR_TERM_GOAL_GAP,
                            AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
                        }
                    ),
                    tuple(
                        code
                        for code in AllocationConstraintCode
                        if code.value in {item.value for item in constraints}
                    ),
                )

    def test_extreme_amounts_ignore_ambient_decimal_context(self) -> None:
        huge = D("1E100")
        changes = {
            "immediately_available_cash": huge,
            "cash_like_assets": D("0.00"),
            "emergency_reserve": D("0.00"),
            "minimum_operating_cash": D("0.00"),
            "low_risk_fixed_income_assets": D("0.00"),
            "manual_equity_fund_assets": D("0.00"),
            "maximum_tolerable_loss": huge,
            "maximum_tolerable_drawdown": D("1"),
        }
        expected = evaluate_allocation(
            profile(**changes), assessment(verified_reserve="0.00"), POLICY, NOW
        )
        with localcontext() as context:
            context.prec = 2
            context.Emax = 9
            context.Emin = -9
            context.traps[Inexact] = True
            context.traps[InvalidOperation] = True
            actual = evaluate_allocation(
                profile(**changes), assessment(verified_reserve="0.00"), POLICY, NOW
            )
        self.assertEqual(actual, expected)
        actual.validate()

    def test_date_max_assessment_remains_a_valid_cash_only_boundary(self) -> None:
        assessed_at = datetime.max.replace(tzinfo=timezone.utc)
        result = evaluate_allocation(
            profile(goals=(goal("last date", date.max),)),
            assessment(),
            POLICY,
            assessed_at,
        )
        assert result.permitted_region is not None
        self.assertEqual(result.permitted_region.horizon_equity_ceiling, D("0.00"))
        self.assertEqual(result.permitted_region.maximum_equity, D("0.00"))
        self.assertIn(
            AllocationConstraintCode.HORIZON_BINDING,
            result.binding_constraints,
        )
        result.validate()

    def test_every_financial_block_path_returns_no_exact_or_region(self) -> None:
        cases = (
            (
                profile(goals=()),
                assessment(),
                AllocationBlockCode.ALLOCATION_HORIZON_MISSING,
            ),
            (
                profile(
                    immediately_available_cash=D("20000.00"),
                    cash_like_assets=D("0.00"),
                ),
                assessment(),
                AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL,
            ),
            (
                profile(
                    can_postpone_goal_use=False,
                    goals=(goal("purpose", date(2031, 7, 12), postponable=True),),
                ),
                assessment(),
                AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT,
            ),
        )
        for current_profile, current_assessment, expected_block in cases:
            with self.subTest(expected_block=expected_block):
                result = evaluate_allocation(
                    current_profile,
                    current_assessment,
                    POLICY,
                    NOW,
                )
                self.assertEqual(result.status, AllocationStatus.BLOCKED)
                self.assertIn(expected_block, result.blocks)
                self.assertIsNone(result.exact)
                self.assertIsNone(result.permitted_region)
                result.validate()


if __name__ == "__main__":
    unittest.main()
