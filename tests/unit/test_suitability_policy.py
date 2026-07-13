from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
    DebtType,
    ProfileConflictCode,
    RiskReaction,
)
from kunjin.suitability.policy import SuitabilityPolicyV1


class SuitabilityPolicyTest(unittest.TestCase):
    def test_policy_v1_has_stable_canonical_checksum(self) -> None:
        first = SuitabilityPolicyV1()
        second = SuitabilityPolicyV1()

        self.assertEqual(first.version, "1")
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.checksum(), second.checksum())
        self.assertEqual(
            first.checksum(),
            "d242931fbf3b477822da1b949f7ec87d1251ef96284fe25f61565cf09177aea5",
        )
        self.assertIn(b'"high_interest_annual_rate":"0.08"', first.canonical_json())

        payload = json.loads(first.canonical_json())
        self.assertEqual(
            payload["supported_debt_types"],
            sorted(payload["supported_debt_types"]),
        )
        self.assertEqual(payload["money_quantum"], "0.01")
        self.assertEqual(payload["effective_at"], "2026-07-12T00:00:00+00:00")

    def test_equivalent_decimal_and_timezone_values_have_identical_policy_bytes(self) -> None:
        canonical = SuitabilityPolicyV1()
        equivalent = SuitabilityPolicyV1(
            high_interest_annual_rate=Decimal("0.080"),
            material_obligation_expense_months=Decimal("1.0"),
            money_quantum=Decimal("0.010"),
            effective_at=datetime(
                2026,
                7,
                12,
                8,
                tzinfo=timezone(timedelta(hours=8)),
            ),
        )

        self.assertEqual(equivalent.canonical_json(), canonical.canonical_json())
        self.assertEqual(equivalent.checksum(), canonical.checksum())

    def test_policy_rejects_invalid_thresholds(self) -> None:
        policy = SuitabilityPolicyV1()
        invalid_policies = (
            replace(policy, version="2"),
            replace(
                policy,
                supported_debt_types=policy.supported_debt_types | {DebtType.BUSINESS_LOAN},
            ),
            replace(
                policy,
                consumer_debt_types=policy.consumer_debt_types - {DebtType.CREDIT_CARD},
            ),
            replace(policy, high_interest_annual_rate=Decimal("0.09")),
            replace(policy, reserve_months_stable=5),
            replace(policy, reserve_months_variable=10),
            replace(policy, reserve_months_high_risk=11),
            replace(policy, material_obligation_expense_months=Decimal("2")),
            replace(policy, short_horizon_years=2),
            replace(policy, medium_horizon_years=4),
            replace(policy, assessment_freshness_hours=48),
            replace(policy, money_quantum=Decimal("0.1")),
            replace(policy, required_amount_rounding="ROUND_UP"),
            replace(policy, available_amount_rounding="ROUND_DOWN"),
            replace(
                policy,
                risk_reaction_severity=((RiskReaction.HOLD, 1),),
            ),
            replace(
                policy,
                effective_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            ),
        )
        for invalid in invalid_policies:
            with self.subTest(policy=invalid):
                with self.assertRaisesRegex(ValueError, "policy V1"):
                    invalid.validate()

    def test_policy_rejects_non_finite_decimals_and_naive_effective_time(self) -> None:
        for field in (
            "high_interest_annual_rate",
            "material_obligation_expense_months",
            "money_quantum",
        ):
            for invalid_value in (
                Decimal("NaN"),
                Decimal("Infinity"),
                Decimal("-Infinity"),
            ):
                with self.subTest(field=field, invalid_value=invalid_value):
                    with self.assertRaisesRegex(ValueError, "policy V1"):
                        replace(
                            SuitabilityPolicyV1(), **{field: invalid_value}
                        ).validate()

        with self.assertRaisesRegex(ValueError, "effective_at"):
            SuitabilityPolicyV1(effective_at=datetime(2026, 7, 12)).validate()

    def test_assessment_models_are_immutable_and_amount_free_summary_is_stable(self) -> None:
        result = AssessmentResult(
            status=AssessmentStatus.BLOCKED,
            hard_blocks=(BlockReason.PROFILE_MISSING,),
            constraints=(),
            required_reserve_months=0,
            risk_answers_consistent=True,
            profile_conflicts=(),
            debt_count=0,
            obligation_count=0,
            goal_count=0,
            amounts=AssessmentAmounts.zero(),
        )
        result.validate()

        with self.assertRaises(FrozenInstanceError):
            result.status = AssessmentStatus.CONSTRAINED

        self.assertEqual(
            result.safe_summary(),
            {
                "debt_count": 0,
                "goal_count": 0,
                "obligation_count": 0,
                "required_reserve_months": 0,
                "risk_answers_consistent": True,
            },
        )

    def test_assessment_amounts_require_finite_decimals(self) -> None:
        for invalid in (Decimal("NaN"), Decimal("Infinity"), "0"):
            with self.subTest(invalid=invalid):
                amounts = replace(
                    AssessmentAmounts.zero(), verified_emergency_reserve=invalid
                )
                with self.assertRaisesRegex(ValueError, "verified emergency reserve"):
                    amounts.validate()

        negative = replace(
            AssessmentAmounts.zero(), emergency_reserve_shortfall=Decimal("-0.01")
        )
        with self.assertRaisesRegex(
            ValueError, "emergency reserve shortfall cannot be negative"
        ):
            negative.validate()

        negative_residual = replace(
            AssessmentAmounts.zero(), monthly_safety_residual=Decimal("-0.01")
        )
        negative_residual.validate()

    def test_assessment_status_must_match_reasons(self) -> None:
        base = AssessmentResult(
            status=AssessmentStatus.READY_FOR_ALLOCATION,
            hard_blocks=(),
            constraints=(),
            required_reserve_months=6,
            risk_answers_consistent=True,
            profile_conflicts=(),
            debt_count=0,
            obligation_count=0,
            goal_count=0,
            amounts=AssessmentAmounts.zero(),
        )
        base.validate()

        invalid = replace(base, status=AssessmentStatus.BLOCKED)
        with self.assertRaisesRegex(ValueError, "status must match assessment reasons"):
            invalid.validate()

        constrained = replace(
            base,
            status=AssessmentStatus.CONSTRAINED,
            constraints=(ConstraintReason.MONTHLY_CEILING_CONSTRAINED,),
        )
        constrained.validate()

    def test_profile_conflict_codes_and_risk_consistency_are_validated(self) -> None:
        base = AssessmentResult(
            status=AssessmentStatus.BLOCKED,
            hard_blocks=(BlockReason.PROFILE_CONFLICT,),
            constraints=(),
            required_reserve_months=6,
            risk_answers_consistent=True,
            profile_conflicts=(
                ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS,
            ),
            debt_count=1,
            obligation_count=0,
            goal_count=0,
            amounts=AssessmentAmounts.zero(),
        )
        base.validate()

        risk_conflict = replace(
            base,
            risk_answers_consistent=False,
            profile_conflicts=(ProfileConflictCode.REACTION_10_VS_REACTION_20,),
        )
        risk_conflict.validate()

        invalid_results = (
            replace(base, hard_blocks=()),
            replace(base, profile_conflicts=()),
            replace(base, profile_conflicts=[]),
            replace(
                base,
                profile_conflicts=(
                    ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS,
                    ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS,
                ),
            ),
            replace(risk_conflict, risk_answers_consistent=True),
            replace(base, risk_answers_consistent=False),
        )
        for result in invalid_results:
            with self.subTest(result=result):
                with self.assertRaisesRegex(
                    ValueError,
                    "profile conflict|profile_conflicts|risk answers",
                ):
                    result.validate()

    def test_debt_types_and_reason_codes_are_stable(self) -> None:
        self.assertEqual(
            [item.value for item in DebtType],
            [
                "mortgage",
                "auto_loan",
                "credit_card",
                "consumer_loan",
                "personal_loan",
                "student_loan",
                "business_loan",
                "other",
            ],
        )
        self.assertEqual(
            [item.value for item in AssessmentStatus],
            ["blocked", "constrained", "ready_for_allocation"],
        )
        self.assertEqual(
            [item.value for item in BlockReason],
            [
                "profile_missing",
                "profile_invalidated",
                "profile_stale",
                "debt_type_unknown",
                "debt_delinquent",
                "revolving_credit",
                "high_interest_debt",
                "emergency_reserve_shortfall",
                "obligation_overdue",
                "goal_overdue",
                "critical_goal_shortfall",
                "no_monthly_investable_cash_flow",
                "profile_conflict",
            ],
        )
        self.assertEqual(
            [item.value for item in ConstraintReason],
            [
                "near_term_obligation_gap",
                "near_term_goal_gap",
                "monthly_ceiling_constrained",
            ],
        )
        self.assertEqual(
            [item.value for item in ProfileConflictCode],
            [
                "monthly_required_debt_service_vs_debts",
                "reaction_10_vs_reaction_20",
                "reaction_20_vs_reaction_30",
                "maximum_tolerable_drawdown_vs_reaction_10",
                "maximum_tolerable_drawdown_vs_reaction_20",
                "maximum_tolerable_drawdown_vs_reaction_30",
                "maximum_tolerable_loss_vs_reactions",
                "maximum_tolerable_loss_vs_goals",
            ],
        )
        self.assertEqual(DebtType.CONSUMER_LOAN.value, "consumer_loan")
        self.assertEqual(BlockReason.HIGH_INTEREST_DEBT.value, "high_interest_debt")
        self.assertEqual(
            ConstraintReason.MONTHLY_CEILING_CONSTRAINED.value,
            "monthly_ceiling_constrained",
        )


if __name__ == "__main__":
    unittest.main()
