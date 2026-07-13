from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import (
    ROUND_DOWN,
    ROUND_UP,
    Clamped,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)

from kunjin.allocation.models import (
    NO_CURRENT_INVESTABLE_STOCK,
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
    horizon_equity_ceiling_v1,
)
from kunjin.allocation.policy import (
    ALLOCATION_POLICY_V1_CHECKSUM,
    AllocationPolicyV1,
)
from kunjin.suitability.models import UNSAFE_PRIVATE_TEXT_CODEPOINTS

D = Decimal


def sample_result() -> AllocationResult:
    obligation_detail = ObligationFundingDetail(
        name="synthetic obligation",
        due_date=date(2028, 7, 12),
        amount=D("120.00"),
        amount_already_reserved=D("20.00"),
        funding_gap=D("100.00"),
        confirmed_monthly_saving=D("10.00"),
        remaining_contribution_periods=10,
        zero_return_funding=D("120.00"),
        horizon_equity_ceiling=D("0.10"),
    )
    assigned_sleeve = AssignedSleeveDetail(
        sleeve_kind=AllocationSleeveKind.GOAL,
        name="synthetic goal",
        assigned_amount=D("50.00"),
        horizon_date=date(2032, 7, 12),
        horizon_equity_ceiling=D("0.50"),
        weighted_equity_contribution=D("25.00"),
    )
    residual_sleeve = AssignedSleeveDetail(
        sleeve_kind=AllocationSleeveKind.RESIDUAL,
        name="residual capital",
        assigned_amount=D("700.00"),
        horizon_date=date(2032, 7, 12),
        horizon_equity_ceiling=D("0.50"),
        weighted_equity_contribution=D("350.00"),
    )
    exact = AllocationExactResult(
        assessment_date=date(2026, 7, 12),
        total_financial_assets=D("1000.00"),
        liquid_protection_assets=D("300.00"),
        verified_emergency_reserve=D("200.00"),
        minimum_operating_cash=D("30.00"),
        protected_short_term_assigned=D("20.00"),
        protected_liquid_claims=D("250.00"),
        investable_stock_assets=D("750.00"),
        monthly_discretionary_allocation_ceiling=D("100.00"),
        maximum_tolerable_loss=D("75.00"),
        maximum_tolerable_drawdown=D("0.10"),
        residual_horizon_date=date(2032, 7, 12),
        goal_funding_details=(
            GoalFundingDetail(
                name="synthetic goal",
                target_date=date(2032, 7, 12),
                target_amount=D("200.00"),
                amount_already_reserved=D("50.00"),
                confirmed_monthly_saving=D("10.00"),
                remaining_contribution_periods=15,
                zero_return_funding=D("200.00"),
                funding_state=GoalFundingState.FUNDABLE_WITHOUT_RETURN,
                horizon_equity_ceiling=D("0.50"),
            ),
        ),
        obligation_funding_details=(obligation_detail,),
        assigned_sleeves=(assigned_sleeve, residual_sleeve),
        aggregate_inputs=AggregateAllocationInputs(
            weighted_horizon_numerator=D("375.00"),
            weighted_horizon_equity_ceiling=D("0.50"),
            loss_amount_equity_ceiling=D("0.20"),
            drawdown_equity_ceiling=D("0.20"),
            willingness_equity_ceiling=D("0.50"),
            stability_equity_ceiling=D("0.70"),
            fixed_income_stress_loss=D("0.10"),
            equity_stress_loss=D("0.50"),
        ),
    )
    return AllocationResult(
        status=AllocationStatus.RANGE_AVAILABLE,
        capability="research_only",
        blocks=(),
        binding_constraints=(
            AllocationConstraintCode.LOSS_AMOUNT_BINDING,
            AllocationConstraintCode.DRAWDOWN_BINDING,
        ),
        profile_conflicts=(),
        safe_summary=AllocationSafeSummary(
            goal_count=1,
            obligation_count=1,
            fully_funded_now_count=0,
            fundable_without_return_count=1,
            funding_gap_without_return_count=0,
            horizon_equity_ceilings=(D("0.50"), D("0.50")),
        ),
        permitted_region=PermittedRegion(
            inequalities=REGION_INEQUALITIES,
            maximum_equity=D("0.20"),
            horizon_equity_ceiling=D("0.50"),
            loss_amount_equity_ceiling=D("0.20"),
            drawdown_equity_ceiling=D("0.20"),
            willingness_equity_ceiling=D("0.50"),
            stability_equity_ceiling=D("0.70"),
        ),
        exact=exact,
    )


class AllocationModelsTest(unittest.TestCase):
    def test_exact_detail_names_reject_unsafe_formatting_but_allow_emoji_zwj(self) -> None:
        exact = sample_result().exact
        assert exact is not None
        goal = exact.goal_funding_details[0]
        obligation = exact.obligation_funding_details[0]
        sleeve = exact.assigned_sleeves[0]

        replace(goal, name="家庭\U0001f469\u200d\U0001f4bb目标").validate()
        replace(obligation, name="教育\U0001f393支出").validate()
        replace(sleeve, name="家庭\U0001f469\u200d\U0001f4bb目标").validate()

        for codepoint in sorted(UNSAFE_PRIVATE_TEXT_CODEPOINTS):
            invalid_name = f"private{chr(codepoint)}name"
            with self.subTest(codepoint=f"U+{codepoint:04X}"):
                with self.assertRaisesRegex(ValueError, "unsupported characters"):
                    replace(goal, name=invalid_name).validate()
                with self.assertRaisesRegex(ValueError, "unsupported characters"):
                    replace(obligation, name=invalid_name).validate()
                with self.assertRaisesRegex(ValueError, "unsupported characters"):
                    replace(sleeve, name=invalid_name).validate()

    def test_enum_values_are_stable(self) -> None:
        self.assertEqual([item.value for item in AllocationStatus], ["blocked", "range_available"])
        self.assertEqual(
            [item.value for item in AssetLayer],
            ["protected_cash", "high_quality_fixed_income", "diversified_equity"],
        )
        self.assertEqual(
            [item.value for item in AllocationBlockCode],
            [
                "suitability_blocked",
                "allocation_horizon_missing",
                "protected_capital_overlap_or_shortfall",
                "allocation_profile_conflict",
            ],
        )
        self.assertEqual(
            [item.value for item in GoalFundingState],
            [
                "fully_funded_now",
                "fundable_without_return",
                "funding_gap_without_return",
                "allocation_horizon_missing",
            ],
        )
        self.assertEqual(
            [item.value for item in AllocationSleeveKind],
            ["goal", "obligation", "residual"],
        )
        self.assertEqual(
            [item.value for item in AllocationConstraintCode],
            [
                "near_term_obligation_gap",
                "near_term_goal_gap",
                "monthly_ceiling_constrained",
                "funding_gap_without_return",
                "no_current_investable_stock",
                "horizon_binding",
                "loss_amount_binding",
                "drawdown_binding",
                "willingness_binding",
                "stability_binding",
            ],
        )
        self.assertEqual(
            [item.value for item in AllocationProfileConflictCode],
            ["profile_disallows_goal_postponement"],
        )
        self.assertIs(
            NO_CURRENT_INVESTABLE_STOCK,
            AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK,
        )

    def test_calendar_horizon_boundaries_and_leap_day_clamping(self) -> None:
        assessed = date(2024, 2, 29)
        cases = (
            (date(2025, 2, 27), D("0")),
            (date(2025, 2, 28), D("0")),
            (date(2025, 3, 1), D("0.10")),
            (date(2027, 2, 27), D("0.10")),
            (date(2027, 2, 28), D("0.10")),
            (date(2027, 3, 1), D("0.30")),
            (date(2029, 2, 27), D("0.30")),
            (date(2029, 2, 28), D("0.30")),
            (date(2029, 3, 1), D("0.50")),
            (date(2032, 2, 28), D("0.50")),
            (date(2032, 2, 29), D("0.50")),
            (date(2032, 3, 1), D("0.70")),
        )
        for target, expected in cases:
            with self.subTest(target=target):
                self.assertEqual(
                    horizon_equity_ceiling_v1(assessed, target),
                    expected,
                )

    def test_calendar_horizons_saturate_safely_near_date_max(self) -> None:
        expected_at_max = {
            9992: D("0.50"),
            9993: D("0.50"),
            9994: D("0.50"),
            9995: D("0.30"),
            9996: D("0.30"),
            9997: D("0.10"),
            9998: D("0.10"),
            9999: D("0"),
        }
        for year, expected in expected_at_max.items():
            with self.subTest(year=year):
                self.assertEqual(
                    horizon_equity_ceiling_v1(date(year, 1, 1), date.max),
                    expected,
                )

        assessed = date(9992, 1, 1)
        branch_cases = (
            (date(9993, 1, 1), D("0")),
            (date(9993, 1, 2), D("0.10")),
            (date(9995, 1, 1), D("0.10")),
            (date(9995, 1, 2), D("0.30")),
            (date(9997, 1, 1), D("0.30")),
            (date(9997, 1, 2), D("0.50")),
            (date.max, D("0.50")),
        )
        for target, expected in branch_cases:
            with self.subTest(target=target):
                self.assertEqual(
                    horizon_equity_ceiling_v1(assessed, target),
                    expected,
                )

        leap_assessed = date(9996, 2, 29)
        self.assertEqual(
            horizon_equity_ceiling_v1(leap_assessed, date(9997, 2, 28)),
            D("0"),
        )
        self.assertEqual(
            horizon_equity_ceiling_v1(leap_assessed, date(9997, 3, 1)),
            D("0.10"),
        )
        self.assertEqual(
            horizon_equity_ceiling_v1(leap_assessed, date.max),
            D("0.30"),
        )
        self.assertEqual(horizon_equity_ceiling_v1(date.max, date.max), D("0"))

    def test_exact_details_and_sleeves_must_use_date_derived_horizon_bands(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        goal = exact.goal_funding_details[0]
        attacks = (
            replace(
                exact,
                goal_funding_details=(
                    replace(
                        goal,
                        target_date=date(2029, 7, 12),
                        horizon_equity_ceiling=D("0.30"),
                    ),
                ),
            ),
            replace(
                exact,
                goal_funding_details=(
                    replace(
                        goal,
                        target_date=date(2029, 7, 13),
                        horizon_equity_ceiling=D("0.10"),
                    ),
                ),
            ),
            replace(
                exact,
                assigned_sleeves=(
                    exact.assigned_sleeves[0],
                    replace(
                        exact.assigned_sleeves[1],
                        horizon_equity_ceiling=D("0.30"),
                        weighted_equity_contribution=D("210.00"),
                    ),
                ),
                aggregate_inputs=replace(
                    exact.aggregate_inputs,
                    weighted_horizon_numerator=D("235.00"),
                    weighted_horizon_equity_ceiling=D("0.31"),
                ),
            ),
        )
        for attack in attacks:
            with self.subTest(attack=attack):
                with self.assertRaisesRegex(ValueError, "date-derived horizon"):
                    attack.validate()

    def test_result_is_immutable_and_has_no_target_or_recommendation_field(self) -> None:
        result = sample_result()
        result.validate()
        fields = {item.name for item in dataclasses.fields(AllocationResult)}
        self.assertNotIn("target", fields)
        self.assertNotIn("recommended", fields)
        self.assertFalse(
            any("target_allocation" in item.name for item in dataclasses.fields(AllocationResult))
        )
        with self.assertRaises(FrozenInstanceError):
            result.status = AllocationStatus.BLOCKED

    def test_nested_models_reject_mutable_or_invalid_values(self) -> None:
        result = sample_result()
        with self.assertRaisesRegex(ValueError, "binding_constraints must be a tuple"):
            replace(result, binding_constraints=[]).validate()
        with self.assertRaisesRegex(ValueError, "capability must be research_only"):
            replace(result, capability="purchase_ready").validate()
        with self.assertRaisesRegex(ValueError, "whole percentage"):
            replace(
                result,
                permitted_region=replace(result.permitted_region, maximum_equity=D("0.505")),
            ).validate()
        with self.assertRaisesRegex(ValueError, "status must match"):
            replace(result, status=AllocationStatus.BLOCKED).validate()

    def test_blocked_result_has_no_exact_allocation_calculation(self) -> None:
        available = sample_result()
        blocked = replace(
            available,
            status=AllocationStatus.BLOCKED,
            blocks=(AllocationBlockCode.SUITABILITY_BLOCKED,),
            binding_constraints=(),
            safe_summary=replace(
                available.safe_summary,
                fully_funded_now_count=0,
                fundable_without_return_count=0,
                funding_gap_without_return_count=0,
                horizon_equity_ceilings=(),
            ),
            permitted_region=None,
            exact=None,
        )
        blocked.validate()

        with self.assertRaisesRegex(ValueError, "blocked allocation must not contain exact"):
            replace(blocked, exact=available.exact).validate()
        with self.assertRaisesRegex(ValueError, "range_available allocation requires exact"):
            replace(available, exact=None).validate()

    def test_exact_records_are_frozen_and_validate_calculation_relationships(self) -> None:
        result = sample_result()
        exact = result.exact
        self.assertIsNotNone(exact)
        with self.assertRaises(FrozenInstanceError):
            exact.assigned_sleeves = ()
        with self.assertRaisesRegex(ValueError, "funding gap"):
            replace(
                exact,
                obligation_funding_details=(
                    replace(exact.obligation_funding_details[0], funding_gap=D("99.99")),
                ),
            ).validate()
        with self.assertRaisesRegex(ValueError, "weighted equity contribution"):
            replace(
                exact,
                assigned_sleeves=(
                    replace(
                        exact.assigned_sleeves[0],
                        weighted_equity_contribution=D("24.99"),
                    ),
                    exact.assigned_sleeves[1],
                ),
            ).validate()
        with self.assertRaisesRegex(ValueError, "weighted horizon numerator"):
            replace(
                exact,
                aggregate_inputs=replace(
                    exact.aggregate_inputs,
                    weighted_horizon_numerator=D("374.99"),
                ),
            ).validate()

    def test_sleeve_kind_rejects_raw_or_product_bucket_strings(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        for invalid in ("goal", "sector_fund", "high_quality_fixed_income"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "AllocationSleeveKind"):
                    replace(
                        exact.assigned_sleeves[0],
                        sleeve_kind=invalid,
                    ).validate()

    def test_assigned_sleeves_must_equal_investable_stock(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        with self.assertRaisesRegex(ValueError, "assigned sleeve amounts"):
            replace(
                exact,
                assigned_sleeves=(
                    exact.assigned_sleeves[0],
                    replace(
                        exact.assigned_sleeves[1],
                        assigned_amount=D("699.99"),
                        weighted_equity_contribution=D("349.995"),
                    ),
                ),
            ).validate()

    def test_weighted_horizon_ceiling_is_recomputed_and_rounded_down(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        sleeves = (
            exact.assigned_sleeves[0],
            AssignedSleeveDetail(
                sleeve_kind=AllocationSleeveKind.RESIDUAL,
                name="long residual",
                assigned_amount=D("700.00"),
                horizon_date=date(2035, 7, 12),
                horizon_equity_ceiling=D("0.70"),
                weighted_equity_contribution=D("490.0000"),
            ),
        )
        rounded = replace(
            exact,
            residual_horizon_date=date(2035, 7, 12),
            assigned_sleeves=sleeves,
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=D("515.0000"),
                weighted_horizon_equity_ceiling=D("0.68"),
            ),
        )
        rounded.validate()

        with self.assertRaisesRegex(ValueError, "weighted horizon equity ceiling"):
            replace(
                rounded,
                aggregate_inputs=replace(
                    rounded.aggregate_inputs,
                    weighted_horizon_equity_ceiling=D("0.69"),
                ),
            ).validate()

    def test_zero_stock_has_no_assigned_sleeves_or_weighted_horizon(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        zero = replace(
            exact,
            total_financial_assets=D("250.00"),
            liquid_protection_assets=D("250.00"),
            investable_stock_assets=D("0.00"),
            residual_horizon_date=None,
            goal_funding_details=(),
            assigned_sleeves=(),
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=D("0.00"),
                weighted_horizon_equity_ceiling=D("0.00"),
            ),
        )
        zero.validate()

        contradictions = (
            replace(
                zero,
                goal_funding_details=exact.goal_funding_details,
                assigned_sleeves=exact.assigned_sleeves,
            ),
            replace(
                zero,
                aggregate_inputs=replace(
                    zero.aggregate_inputs,
                    weighted_horizon_numerator=D("0.01"),
                ),
            ),
            replace(
                zero,
                aggregate_inputs=replace(
                    zero.aggregate_inputs,
                    weighted_horizon_equity_ceiling=D("0.01"),
                ),
            ),
        )
        for invalid in contradictions:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "zero investable stock"):
                    invalid.validate()

    def test_range_region_and_zero_stock_information_code_must_match_exact_stock(self) -> None:
        available = sample_result()
        exact = available.exact
        self.assertIsNotNone(exact)
        zero_exact = replace(
            exact,
            total_financial_assets=D("250.00"),
            liquid_protection_assets=D("250.00"),
            investable_stock_assets=D("0.00"),
            residual_horizon_date=None,
            goal_funding_details=(),
            assigned_sleeves=(),
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=D("0.00"),
                weighted_horizon_equity_ceiling=D("0.00"),
            ),
        )
        zero_stock = replace(
            available,
            binding_constraints=(AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK,),
            safe_summary=replace(
                available.safe_summary,
                goal_count=0,
                fundable_without_return_count=0,
                horizon_equity_ceilings=(),
            ),
            permitted_region=None,
            exact=zero_exact,
        )
        zero_stock.validate()

        invalid_results = (
            replace(zero_stock, permitted_region=available.permitted_region),
            replace(zero_stock, binding_constraints=()),
            replace(available, permitted_region=None),
            replace(
                available,
                binding_constraints=(AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK,),
            ),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "no_current_investable_stock"):
                    invalid.validate()

    def test_goal_funding_math_and_state_are_cross_validated(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        detail = exact.goal_funding_details[0]
        invalid = (
            replace(detail, zero_return_funding=D("199.99")),
            replace(detail, funding_state=GoalFundingState.FULLY_FUNDED_NOW),
            replace(detail, funding_state=GoalFundingState.FUNDING_GAP_WITHOUT_RETURN),
            replace(detail, funding_state=GoalFundingState.ALLOCATION_HORIZON_MISSING),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "zero-return|funding_state|horizon"):
                    value.validate()

    def test_obligation_funding_math_is_cross_validated(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        detail = exact.obligation_funding_details[0]
        for value in (
            replace(detail, funding_gap=D("99.99")),
            replace(detail, zero_return_funding=D("119.99")),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "funding gap|zero-return"):
                    value.validate()

    def test_exact_capital_summary_and_region_contradictions_are_rejected(self) -> None:
        result = sample_result()
        exact = result.exact
        self.assertIsNotNone(exact)
        invalid_results = (
            replace(result, exact=replace(exact, liquid_protection_assets=D("1000.01"))),
            replace(result, exact=replace(exact, protected_liquid_claims=D("300.01"))),
            replace(result, exact=replace(exact, protected_short_term_assigned=D("250.01"))),
            replace(result, exact=replace(exact, investable_stock_assets=D("749.99"))),
            replace(result, safe_summary=replace(result.safe_summary, goal_count=2)),
            replace(
                result,
                permitted_region=replace(
                    result.permitted_region,
                    willingness_equity_ceiling=D("0.40"),
                ),
            ),
            replace(
                result,
                permitted_region=replace(result.permitted_region, maximum_equity=D("0.10")),
            ),
            replace(
                result,
                exact=replace(
                    exact,
                    aggregate_inputs=replace(
                        exact.aggregate_inputs,
                        equity_stress_loss=D("0.40"),
                    ),
                ),
            ),
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    invalid.validate()

    def test_protected_claim_components_cannot_be_shifted_into_residual(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        attacks = (
            (D("50.00"), D("950.00"), D("900.00"), D("475.00")),
            (D("220.00"), D("780.00"), D("730.00"), D("390.00")),
            (D("230.00"), D("770.00"), D("720.00"), D("385.00")),
        )
        for claims, stock, residual_amount, numerator in attacks:
            with self.subTest(claims=claims):
                residual = replace(
                    exact.assigned_sleeves[1],
                    assigned_amount=residual_amount,
                    weighted_equity_contribution=residual_amount * D("0.50"),
                )
                attack = replace(
                    exact,
                    protected_liquid_claims=claims,
                    investable_stock_assets=stock,
                    assigned_sleeves=(exact.assigned_sleeves[0], residual),
                    aggregate_inputs=replace(
                        exact.aggregate_inputs,
                        weighted_horizon_numerator=numerator,
                    ),
                )
                with self.assertRaisesRegex(ValueError, "protected liquid claims"):
                    attack.validate()

        with self.assertRaisesRegex(ValueError, "protected liquid claims"):
            replace(exact, verified_emergency_reserve=D("0.00")).validate()

    def test_short_term_protected_sum_handles_duplicates_and_three_year_boundary(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        base_goal = exact.goal_funding_details[0]
        boundary_goal = replace(
            base_goal,
            name="boundary duplicate",
            target_date=date(2029, 7, 12),
            target_amount=D("100.00"),
            amount_already_reserved=D("15.00"),
            zero_return_funding=D("165.00"),
            horizon_equity_ceiling=D("0.10"),
        )
        duplicate_exact = replace(
            exact,
            protected_liquid_claims=D("280.00"),
            protected_short_term_assigned=D("50.00"),
            investable_stock_assets=D("720.00"),
            goal_funding_details=(base_goal, boundary_goal, boundary_goal),
            assigned_sleeves=(
                exact.assigned_sleeves[0],
                replace(
                    exact.assigned_sleeves[1],
                    assigned_amount=D("670.00"),
                    weighted_equity_contribution=D("335.00"),
                ),
            ),
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=D("360.00"),
            ),
        )
        duplicate_exact.validate()

        after_boundary = replace(
            boundary_goal,
            name="after boundary",
            target_date=date(2029, 7, 13),
            horizon_equity_ceiling=D("0.30"),
        )
        after_sleeve = AssignedSleeveDetail(
            AllocationSleeveKind.GOAL,
            after_boundary.name,
            D("15.00"),
            after_boundary.target_date,
            D("0.30"),
            D("4.50"),
        )
        after_exact = replace(
            duplicate_exact,
            protected_liquid_claims=D("265.00"),
            protected_short_term_assigned=D("35.00"),
            investable_stock_assets=D("735.00"),
            goal_funding_details=(base_goal, boundary_goal, after_boundary),
            assigned_sleeves=(
                exact.assigned_sleeves[0],
                after_sleeve,
                replace(
                    exact.assigned_sleeves[1],
                    assigned_amount=D("670.00"),
                    weighted_equity_contribution=D("335.00"),
                ),
            ),
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=D("364.50"),
                weighted_horizon_equity_ceiling=D("0.49"),
            ),
        )
        after_exact.validate()

    def test_amount_free_fields_reject_strings_unknown_formulas_and_private_text(self) -> None:
        result = sample_result()
        invalid = (
            replace(result, binding_constraints=("horizon_binding",)),
            replace(result, binding_constraints=("private-goal-name",)),
            replace(result, profile_conflicts=("unknown-conflict",)),
            replace(
                result,
                permitted_region=replace(
                    result.permitted_region,
                    inequalities=("E <= 123456.78",),
                ),
            ),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "AllocationConstraintCode|AllocationProfileConflictCode|fixed region",
                ):
                    value.validate()

    def test_decimal_and_nested_dataclass_subclasses_are_rejected(self) -> None:
        class DecimalSubclass(Decimal):
            pass

        class GoalDetailSubclass(GoalFundingDetail):
            pass

        class ObligationDetailSubclass(ObligationFundingDetail):
            pass

        class AssignedSleeveSubclass(AssignedSleeveDetail):
            pass

        class AggregateInputsSubclass(AggregateAllocationInputs):
            pass

        class SafeSummarySubclass(AllocationSafeSummary):
            pass

        class PermittedRegionSubclass(PermittedRegion):
            pass

        class ExactResultSubclass(AllocationExactResult):
            pass

        class AllocationResultSubclass(AllocationResult):
            pass

        result = sample_result()
        exact = result.exact
        self.assertIsNotNone(exact)
        with self.assertRaisesRegex(ValueError, "Decimal"):
            replace(exact, total_financial_assets=DecimalSubclass("1000.00")).validate()
        with self.assertRaisesRegex(ValueError, "AllocationSafeSummary"):
            replace(
                result,
                safe_summary=SafeSummarySubclass(**result.safe_summary.__dict__),
            ).validate()
        nested_subclasses = (
            GoalDetailSubclass(**exact.goal_funding_details[0].__dict__),
            ObligationDetailSubclass(**exact.obligation_funding_details[0].__dict__),
            AssignedSleeveSubclass(**exact.assigned_sleeves[0].__dict__),
            AggregateInputsSubclass(**exact.aggregate_inputs.__dict__),
            PermittedRegionSubclass(**result.permitted_region.__dict__),
            ExactResultSubclass(**exact.__dict__),
            AllocationResultSubclass(**result.__dict__),
        )
        for value in nested_subclasses:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "exact"):
                    value.validate()

    def test_all_dataclasses_reject_hidden_instance_state_at_nested_boundaries(self) -> None:
        attacks = (
            ("target", lambda result: result.exact.goal_funding_details[0]),
            ("recommended", lambda result: result.exact.obligation_funding_details[0]),
            ("selected", lambda result: result.exact.assigned_sleeves[0]),
            ("private_goal", lambda result: result.exact.aggregate_inputs),
            ("target", lambda result: result.safe_summary),
            ("recommended", lambda result: result.permitted_region),
            ("selected", lambda result: result.exact),
            ("private_amount", lambda result: result),
        )
        for field, selector in attacks:
            with self.subTest(field=field):
                result = sample_result()
                object.__setattr__(selector(result), field, "private-sentinel-123456.78")
                with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
                    result.validate()

        policy = AllocationPolicyV1()
        object.__setattr__(policy, "recommended", "private-policy-sentinel")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            policy.validate()

    def test_profile_conflict_block_and_typed_details_must_match(self) -> None:
        available = sample_result()
        blocked = replace(
            available,
            status=AllocationStatus.BLOCKED,
            blocks=(AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT,),
            binding_constraints=(AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,),
            profile_conflicts=(AllocationProfileConflictCode.PROFILE_DISALLOWS_GOAL_POSTPONEMENT,),
            permitted_region=None,
            exact=None,
        )
        blocked.validate()
        for invalid in (
            replace(blocked, profile_conflicts=()),
            replace(blocked, blocks=(AllocationBlockCode.SUITABILITY_BLOCKED,)),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "profile conflict"):
                    invalid.validate()

    def test_region_inequalities_reject_str_subclasses_even_when_equality_lies(self) -> None:
        class SentinelStr(str):
            def __eq__(self, other: object) -> bool:
                return True

            __hash__ = str.__hash__

        region = sample_result().permitted_region
        self.assertIsNotNone(region)
        malicious = tuple(SentinelStr("private-amount-123456.78") for _ in REGION_INEQUALITIES)
        with self.assertRaisesRegex(ValueError, "exact strings"):
            replace(region, inequalities=malicious).validate()

    def test_blocked_results_allow_only_inherited_phase_b_constraints(self) -> None:
        available = sample_result()
        blocked = replace(
            available,
            status=AllocationStatus.BLOCKED,
            blocks=(AllocationBlockCode.SUITABILITY_BLOCKED,),
            binding_constraints=(
                AllocationConstraintCode.NEAR_TERM_OBLIGATION_GAP,
                AllocationConstraintCode.NEAR_TERM_GOAL_GAP,
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
            ),
            permitted_region=None,
            exact=None,
        )
        blocked.validate()
        forbidden = (
            AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN,
            AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK,
            AllocationConstraintCode.HORIZON_BINDING,
            AllocationConstraintCode.LOSS_AMOUNT_BINDING,
            AllocationConstraintCode.DRAWDOWN_BINDING,
            AllocationConstraintCode.WILLINGNESS_BINDING,
            AllocationConstraintCode.STABILITY_BINDING,
        )
        for code in forbidden:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, "inherited Phase B"):
                    replace(blocked, binding_constraints=(code,)).validate()

    def test_funding_gap_constraint_matches_goal_states(self) -> None:
        result = sample_result()
        exact = result.exact
        self.assertIsNotNone(exact)
        gap_goal = replace(
            exact.goal_funding_details[0],
            confirmed_monthly_saving=D("0.00"),
            zero_return_funding=D("50.00"),
            funding_state=GoalFundingState.FUNDING_GAP_WITHOUT_RETURN,
        )
        gap_result = replace(
            result,
            binding_constraints=(
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                AllocationConstraintCode.DRAWDOWN_BINDING,
                AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN,
            ),
            safe_summary=replace(
                result.safe_summary,
                fundable_without_return_count=0,
                funding_gap_without_return_count=1,
            ),
            exact=replace(exact, goal_funding_details=(gap_goal,)),
        )
        gap_result.validate()
        with self.assertRaisesRegex(ValueError, "funding-gap constraint"):
            replace(
                gap_result,
                binding_constraints=(
                    AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                    AllocationConstraintCode.DRAWDOWN_BINDING,
                ),
            ).validate()

    def test_binding_codes_are_derived_and_all_ties_are_required(self) -> None:
        result = sample_result()
        result.validate()
        for invalid in (
            replace(
                result,
                binding_constraints=(AllocationConstraintCode.LOSS_AMOUNT_BINDING,),
            ),
            replace(
                result,
                binding_constraints=(
                    AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                    AllocationConstraintCode.DRAWDOWN_BINDING,
                    AllocationConstraintCode.HORIZON_BINDING,
                ),
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "binding codes"):
                    invalid.validate()

        tied_region = replace(
            result.permitted_region,
            horizon_equity_ceiling=D("0.20"),
            willingness_equity_ceiling=D("0.20"),
            stability_equity_ceiling=D("0.20"),
        )
        tied_goal = replace(
            result.exact.goal_funding_details[0],
            name="tied medium goal",
            target_date=date(2030, 7, 12),
            target_amount=D("300.00"),
            amount_already_reserved=D("275.00"),
            zero_return_funding=D("425.00"),
            horizon_equity_ceiling=D("0.30"),
        )
        tied_goal_sleeve = AssignedSleeveDetail(
            AllocationSleeveKind.GOAL,
            tied_goal.name,
            D("275.00"),
            tied_goal.target_date,
            D("0.30"),
            D("82.50"),
        )
        tied_aggregate = replace(
            result.exact.aggregate_inputs,
            weighted_horizon_numerator=D("150.00"),
            weighted_horizon_equity_ceiling=D("0.20"),
            willingness_equity_ceiling=D("0.20"),
            stability_equity_ceiling=D("0.20"),
        )
        tied = replace(
            result,
            binding_constraints=(
                AllocationConstraintCode.HORIZON_BINDING,
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                AllocationConstraintCode.DRAWDOWN_BINDING,
                AllocationConstraintCode.WILLINGNESS_BINDING,
                AllocationConstraintCode.STABILITY_BINDING,
            ),
            safe_summary=replace(
                result.safe_summary,
                goal_count=2,
                fundable_without_return_count=2,
                horizon_equity_ceilings=(D("0.50"), D("0.30"), D("0.10")),
            ),
            permitted_region=tied_region,
            exact=replace(
                result.exact,
                residual_horizon_date=date(2028, 7, 12),
                goal_funding_details=(
                    result.exact.goal_funding_details[0],
                    tied_goal,
                ),
                assigned_sleeves=(
                    result.exact.assigned_sleeves[0],
                    tied_goal_sleeve,
                    AssignedSleeveDetail(
                        sleeve_kind=AllocationSleeveKind.RESIDUAL,
                        name="tied residual",
                        assigned_amount=D("425.00"),
                        horizon_date=date(2028, 7, 12),
                        horizon_equity_ceiling=D("0.10"),
                        weighted_equity_contribution=D("42.50"),
                    ),
                ),
                aggregate_inputs=tied_aggregate,
            ),
        )
        tied.validate()
        with self.assertRaisesRegex(ValueError, "binding codes"):
            replace(
                tied,
                binding_constraints=tied.binding_constraints[:-1],
            ).validate()

    def test_loss_and_drawdown_ceilings_are_recomputed_without_widening(self) -> None:
        base = sample_result()

        def result_with_limits(
            loss: Decimal,
            drawdown: Decimal,
            loss_ceiling: Decimal,
            drawdown_ceiling: Decimal,
        ) -> AllocationResult:
            maximum = min(
                D("0.50"),
                loss_ceiling,
                drawdown_ceiling,
                D("0.50"),
                D("0.70"),
            )
            binding_by_value = (
                (AllocationConstraintCode.HORIZON_BINDING, D("0.50")),
                (AllocationConstraintCode.LOSS_AMOUNT_BINDING, loss_ceiling),
                (AllocationConstraintCode.DRAWDOWN_BINDING, drawdown_ceiling),
                (AllocationConstraintCode.WILLINGNESS_BINDING, D("0.50")),
                (AllocationConstraintCode.STABILITY_BINDING, D("0.70")),
            )
            aggregate = replace(
                base.exact.aggregate_inputs,
                loss_amount_equity_ceiling=loss_ceiling,
                drawdown_equity_ceiling=drawdown_ceiling,
            )
            region = replace(
                base.permitted_region,
                maximum_equity=maximum,
                loss_amount_equity_ceiling=loss_ceiling,
                drawdown_equity_ceiling=drawdown_ceiling,
            )
            return replace(
                base,
                binding_constraints=tuple(
                    code for code, value in binding_by_value if value == maximum
                ),
                permitted_region=region,
                exact=replace(
                    base.exact,
                    maximum_tolerable_loss=loss,
                    maximum_tolerable_drawdown=drawdown,
                    aggregate_inputs=aggregate,
                ),
            )

        loss_cases = (
            (D("0.00"), D("0.00")),
            (D("0.01"), D("0.00")),
            (D("75.00"), D("0.20")),
            (D("74.99"), D("0.19")),
        )
        for loss, expected in loss_cases:
            with self.subTest(loss=loss):
                result_with_limits(loss, D("0.50"), expected, D("1.00")).validate()

        drawdown_cases = (
            (D("0.00"), D("0.00")),
            (D("0.01"), D("0.02")),
            (D("0.10"), D("0.20")),
            (D("0.099"), D("0.19")),
        )
        for drawdown, expected in drawdown_cases:
            with self.subTest(drawdown=drawdown):
                result_with_limits(D("1000.00"), drawdown, D("1.00"), expected).validate()

        understated = result_with_limits(D("75.00"), D("0.50"), D("0.19"), D("1.00"))
        with self.assertRaisesRegex(ValueError, "loss amount equity ceiling"):
            understated.validate()
        overstated = result_with_limits(D("1000.00"), D("0.10"), D("1.00"), D("0.21"))
        with self.assertRaisesRegex(ValueError, "drawdown equity ceiling"):
            overstated.validate()

    def test_residual_sleeve_and_horizon_date_are_consistent(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        with self.assertRaisesRegex(ValueError, "residual_horizon_date"):
            replace(exact, residual_horizon_date=date(2033, 7, 12)).validate()
        with self.assertRaisesRegex(ValueError, "residual_horizon_date"):
            replace(
                exact,
                assigned_sleeves=(exact.assigned_sleeves[0],),
                residual_horizon_date=date(2032, 7, 12),
            ).validate()

    def test_cny_amounts_require_cent_quantization_but_products_keep_precision(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        with self.assertRaisesRegex(ValueError, "CNY cents"):
            replace(exact, maximum_tolerable_loss=D("75.001")).validate()
        with self.assertRaisesRegex(ValueError, "CNY cents"):
            replace(
                exact.goal_funding_details[0],
                confirmed_monthly_saving=D("10.001"),
            ).validate()
        precise = replace(
            exact.assigned_sleeves[0],
            assigned_amount=D("50.01"),
            weighted_equity_contribution=D("25.005"),
        )
        precise.validate()
        replace(
            exact.aggregate_inputs,
            weighted_horizon_numerator=D("375.005"),
        ).validate()

    def test_safe_quantization_supports_large_values_and_stabilizes_bad_exponents(self) -> None:
        goal = sample_result().exact.goal_funding_details[0]
        for value in (D("1E+28"), D("1E+100")):
            with self.subTest(value=value):
                replace(
                    goal,
                    target_amount=value,
                    amount_already_reserved=value,
                    confirmed_monthly_saving=D("0.00"),
                    remaining_contribution_periods=0,
                    zero_return_funding=value,
                    funding_state=GoalFundingState.FULLY_FUNDED_NOW,
                ).validate()

        malformed = D("1E+100000")
        with self.assertRaisesRegex(ValueError, "quantized safely"):
            replace(
                goal,
                target_amount=malformed,
                amount_already_reserved=malformed,
                confirmed_monthly_saving=D("0.00"),
                remaining_contribution_periods=0,
                zero_return_funding=malformed,
                funding_state=GoalFundingState.FULLY_FUNDED_NOW,
            ).validate()

    def test_decimal_arithmetic_is_exact_and_global_context_independent(self) -> None:
        goal = sample_result().exact.goal_funding_details[0]
        exact_sum = D("10000000000000000000000000000.01")
        large_goal = replace(
            goal,
            target_amount=exact_sum,
            amount_already_reserved=D("1E+28"),
            confirmed_monthly_saving=D("0.01"),
            remaining_contribution_periods=1,
            zero_return_funding=exact_sum,
            funding_state=GoalFundingState.FUNDABLE_WITHOUT_RETURN,
        )

        exact = sample_result().exact
        self.assertIsNotNone(exact)
        total = D("10000000000000000000000000000.02")
        claims = D("0.01")
        stock = D("10000000000000000000000000000.01")
        weighted = D("5000000000000000000000000000.005")
        residual = AssignedSleeveDetail(
            AllocationSleeveKind.RESIDUAL,
            "large residual",
            stock,
            exact.residual_horizon_date,
            D("0.50"),
            weighted,
        )
        large_exact = replace(
            exact,
            total_financial_assets=total,
            liquid_protection_assets=claims,
            verified_emergency_reserve=D("0.00"),
            minimum_operating_cash=claims,
            protected_short_term_assigned=D("0.00"),
            protected_liquid_claims=claims,
            investable_stock_assets=stock,
            goal_funding_details=(),
            obligation_funding_details=(),
            assigned_sleeves=(residual,),
            aggregate_inputs=replace(
                exact.aggregate_inputs,
                weighted_horizon_numerator=weighted,
                weighted_horizon_equity_ceiling=D("0.50"),
            ),
        )

        outcomes = []
        for precision in (6, 28, 40):
            with self.subTest(precision=precision), localcontext() as context:
                context.prec = precision
                large_goal.validate()
                large_exact.validate()
                outcomes.append(
                    (
                        large_goal.zero_return_funding,
                        large_exact.investable_stock_assets,
                    )
                )
        self.assertEqual(outcomes, [(exact_sum, stock)] * 3)

        invalid_goal = replace(large_goal, zero_return_funding=D("1E+28"))
        failures = []
        for precision in (6, 28, 40):
            with localcontext() as context:
                context.prec = precision
                try:
                    invalid_goal.validate()
                except ValueError as exc:
                    failures.append(str(exc))
        self.assertEqual(len(set(failures)), 1)
        self.assertIn("zero-return funding", failures[0])

    def test_decimal_helpers_ignore_all_ambient_context_controls(self) -> None:
        goal = sample_result().exact.goal_funding_details[0]
        exact_sum = D("10000000000000000000000000000.01")
        valid = replace(
            goal,
            target_amount=exact_sum,
            amount_already_reserved=D("1E+28"),
            confirmed_monthly_saving=D("0.01"),
            remaining_contribution_periods=1,
            zero_return_funding=exact_sum,
            funding_state=GoalFundingState.FUNDABLE_WITHOUT_RETURN,
        )
        invalid = replace(
            valid,
            target_amount=D("10000000000000000000000000000.011"),
        )
        configurations = (
            (6, 9, -9, ROUND_DOWN, True, False, True),
            (28, 999, -999, ROUND_UP, False, True, False),
            (40, 99, -99, ROUND_DOWN, True, True, True),
        )
        outcomes = []
        messages = []
        for precision, emax, emin, rounding, inexact, invalid_op, overflow in configurations:
            with localcontext() as ambient:
                ambient.prec = precision
                ambient.Emax = emax
                ambient.Emin = emin
                ambient.rounding = rounding
                ambient.traps[Inexact] = inexact
                ambient.traps[Rounded] = inexact
                ambient.traps[InvalidOperation] = invalid_op
                ambient.traps[Overflow] = overflow
                ambient.traps[Clamped] = not overflow
                valid.validate()
                outcomes.append(valid.zero_return_funding)
                try:
                    invalid.validate()
                except ValueError as exc:
                    messages.append(str(exc))
        self.assertEqual(outcomes, [exact_sum] * len(configurations))
        self.assertEqual(len(set(messages)), 1)
        self.assertIn("CNY cents", messages[0])

    def test_sleeve_matching_uses_multisets_for_collisions_and_order(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        base_goal = exact.goal_funding_details[0]
        first_goal = replace(
            base_goal,
            name="collision",
            target_amount=D("200.00"),
            amount_already_reserved=D("100.00"),
            zero_return_funding=D("250.00"),
        )
        second_goal = replace(
            base_goal,
            name="collision",
            target_amount=D("300.00"),
            amount_already_reserved=D("200.00"),
            zero_return_funding=D("350.00"),
        )
        first_sleeve = AssignedSleeveDetail(
            AllocationSleeveKind.GOAL,
            "collision",
            D("100.00"),
            first_goal.target_date,
            D("0.50"),
            D("50.00"),
        )
        second_sleeve = AssignedSleeveDetail(
            AllocationSleeveKind.GOAL,
            "collision",
            D("200.00"),
            second_goal.target_date,
            D("0.50"),
            D("100.00"),
        )
        residual = replace(
            exact.assigned_sleeves[1],
            assigned_amount=D("450.00"),
            weighted_equity_contribution=D("225.00"),
        )
        collision_exact = replace(
            exact,
            goal_funding_details=(second_goal, first_goal),
            assigned_sleeves=(first_sleeve, residual, second_sleeve),
        )
        collision_exact.validate()

        identical_goal = replace(
            base_goal,
            name="identical",
            target_amount=D("200.00"),
            amount_already_reserved=D("100.00"),
            zero_return_funding=D("250.00"),
        )
        identical_sleeve = replace(first_sleeve, name="identical")
        identical_residual = replace(
            residual,
            assigned_amount=D("550.00"),
            weighted_equity_contribution=D("275.00"),
        )
        identical_exact = replace(
            exact,
            goal_funding_details=(identical_goal, identical_goal),
            assigned_sleeves=(identical_sleeve, identical_sleeve, identical_residual),
        )
        identical_exact.validate()

        with self.assertRaisesRegex(ValueError, "exact multiset"):
            replace(
                identical_exact,
                goal_funding_details=(identical_goal,),
            ).validate()

        missing_duplicate_residual = replace(
            identical_residual,
            assigned_amount=D("650.00"),
            weighted_equity_contribution=D("325.00"),
        )
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            replace(
                identical_exact,
                assigned_sleeves=(identical_sleeve, missing_duplicate_residual),
            ).validate()

    def test_long_term_reserved_items_cannot_be_hidden_in_residual(self) -> None:
        exact = sample_result().exact
        self.assertIsNotNone(exact)
        all_residual = replace(
            exact.assigned_sleeves[1],
            assigned_amount=D("750.00"),
            weighted_equity_contribution=D("375.00"),
        )
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            replace(exact, assigned_sleeves=(all_residual,)).validate()

        long_obligation = replace(
            exact.obligation_funding_details[0],
            due_date=date(2030, 7, 12),
            horizon_equity_ceiling=D("0.30"),
        )
        with self.assertRaisesRegex(ValueError, "exact multiset"):
            replace(
                exact,
                protected_liquid_claims=D("230.00"),
                protected_short_term_assigned=D("0.00"),
                investable_stock_assets=D("770.00"),
                obligation_funding_details=(long_obligation,),
                assigned_sleeves=(
                    exact.assigned_sleeves[0],
                    replace(
                        exact.assigned_sleeves[1],
                        assigned_amount=D("720.00"),
                        weighted_equity_contribution=D("360.00"),
                    ),
                ),
                aggregate_inputs=replace(
                    exact.aggregate_inputs,
                    weighted_horizon_numerator=D("385.00"),
                ),
            ).validate()


class AllocationPolicyTest(unittest.TestCase):
    def test_policy_v1_is_fixed_and_canonical(self) -> None:
        policy = AllocationPolicyV1()
        policy.validate()
        self.assertEqual(policy.version, "1")
        self.assertEqual(policy.stress_loss_by_layer[0], (AssetLayer.PROTECTED_CASH, D("0")))
        self.assertEqual(
            policy.stress_loss_by_layer[1],
            (AssetLayer.HIGH_QUALITY_FIXED_INCOME, D("0.10")),
        )
        self.assertEqual(policy.stress_loss_by_layer[2], (AssetLayer.DIVERSIFIED_EQUITY, D("0.50")))
        self.assertEqual(
            policy.horizon_equity_ceilings,
            ((1, D("0")), (3, D("0.10")), (5, D("0.30")), (8, D("0.50")), (None, D("0.70"))),
        )
        self.assertEqual(policy.effective_at, datetime(2026, 7, 12, tzinfo=timezone.utc))
        self.assertEqual(policy.assessment_freshness_hours, 24)
        self.assertEqual(policy.money_quantum, D("0.01"))
        self.assertEqual(policy.required_amount_rounding, "ROUND_CEILING")
        self.assertEqual(policy.available_amount_rounding, "ROUND_FLOOR")
        self.assertEqual(policy.percentage_rounding, "ROUND_FLOOR")
        self.assertEqual(policy.percentage_quantum, D("0.01"))
        self.assertEqual(policy.monthly_saving_apportionment, "largest_remainder")
        self.assertEqual(
            hashlib.sha256(policy.canonical_json()).hexdigest(),
            ALLOCATION_POLICY_V1_CHECKSUM,
        )

    def test_policy_canonicalizes_equivalent_decimals_and_timezones(self) -> None:
        canonical = AllocationPolicyV1()
        equivalent = AllocationPolicyV1(
            stress_loss_by_layer=(
                (AssetLayer.PROTECTED_CASH, D("0.00")),
                (AssetLayer.HIGH_QUALITY_FIXED_INCOME, D("0.100")),
                (AssetLayer.DIVERSIFIED_EQUITY, D("0.500")),
            ),
            money_quantum=D("0.010"),
            percentage_quantum=D("0.010"),
            effective_at=datetime(2026, 7, 12, 8, tzinfo=timezone(timedelta(hours=8))),
        )
        self.assertEqual(equivalent.canonical_json(), canonical.canonical_json())
        self.assertEqual(equivalent.checksum(), canonical.checksum())

    def test_policy_canonical_decimal_normalizes_negative_zero(self) -> None:
        canonical = AllocationPolicyV1()
        negative_zero = replace(
            canonical,
            stress_loss_by_layer=(
                (AssetLayer.PROTECTED_CASH, D("-0.00")),
                *canonical.stress_loss_by_layer[1:],
            ),
        )
        self.assertEqual(negative_zero.canonical_json(), canonical.canonical_json())
        self.assertNotIn(b'"-0"', negative_zero.canonical_json())

    def test_policy_rejects_subclasses_and_mutated_parameters(self) -> None:
        class DecimalSubclass(Decimal):
            pass

        class DerivedPolicy(AllocationPolicyV1):
            pass

        class TupleSubclass(tuple):
            pass

        class DateTimeSubclass(datetime):
            pass

        class StatefulTimezone(tzinfo):
            calls = 0

            def utcoffset(self, value: object) -> timedelta:
                self.calls += 1
                return timedelta(hours=self.calls % 2)

            def dst(self, value: object) -> timedelta:
                return timedelta(0)

        invalid = (
            DerivedPolicy(),
            replace(AllocationPolicyV1(), version="2"),
            replace(AllocationPolicyV1(), assessment_freshness_hours=48),
            replace(AllocationPolicyV1(), percentage_rounding="ROUND_HALF_UP"),
            replace(
                AllocationPolicyV1(),
                monthly_saving_apportionment="pro_rata_rounding",
            ),
            replace(AllocationPolicyV1(), horizon_equity_ceilings=((1, D("0.01")),)),
            replace(
                AllocationPolicyV1(),
                money_quantum=DecimalSubclass("0.01"),
            ),
            replace(
                AllocationPolicyV1(),
                stress_loss_by_layer=TupleSubclass(AllocationPolicyV1().stress_loss_by_layer),
            ),
            replace(
                AllocationPolicyV1(),
                stress_loss_by_layer=(
                    TupleSubclass(AllocationPolicyV1().stress_loss_by_layer[0]),
                    *AllocationPolicyV1().stress_loss_by_layer[1:],
                ),
            ),
            replace(
                AllocationPolicyV1(),
                effective_at=DateTimeSubclass(2026, 7, 12, tzinfo=timezone.utc),
            ),
            replace(
                AllocationPolicyV1(),
                effective_at=datetime(2026, 7, 12, tzinfo=StatefulTimezone()),
            ),
        )
        for policy in invalid:
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(ValueError, "allocation policy V1"):
                    policy.validate()

    def test_monthly_saving_apportionment_is_fixed_and_canonical(self) -> None:
        policy = AllocationPolicyV1()
        payload = json.loads(policy.canonical_json())
        self.assertEqual(payload["monthly_saving_apportionment"], "largest_remainder")
        with self.assertRaisesRegex(ValueError, "monthly saving apportionment"):
            replace(policy, monthly_saving_apportionment="largest_remainder_v2").validate()

    def test_canonical_json_is_sorted_and_has_a_hard_coded_golden_checksum(self) -> None:
        policy = AllocationPolicyV1()
        payload = json.loads(policy.canonical_json())
        self.assertEqual(
            policy.canonical_json(),
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(),
        )
        self.assertEqual(
            ALLOCATION_POLICY_V1_CHECKSUM,
            "4ab1bfde13afbbc87730e6ce9f842757d64d6565fe27dee18c0d03e125f3d708",
        )


if __name__ == "__main__":
    unittest.main()
