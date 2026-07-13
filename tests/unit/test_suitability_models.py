from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.allocation.serialization import MAX_TEXT_CHARS
from kunjin.suitability.models import (
    MAX_PRIVATE_NAME_CHARS,
    UNSAFE_PRIVATE_TEXT_CODEPOINTS,
    Debt,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    RiskReaction,
)
from kunjin.suitability.serialization import decode_profile, encode_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def valid_profile() -> FinancialProfile:
    return FinancialProfile(
        currency="CNY",
        monthly_net_income=Decimal("12000"),
        monthly_essential_expenses=Decimal("5000"),
        monthly_required_debt_service=Decimal("1500"),
        monthly_investment_ceiling=Decimal("1000"),
        minimum_operating_cash=Decimal("3000"),
        minimum_monthly_cash_buffer=Decimal("1000"),
        income_stability=IncomeStability.STABLE,
        income_interruption_risk=False,
        immediately_available_cash=Decimal("50000"),
        cash_like_assets=Decimal("10000"),
        emergency_reserve=Decimal("40000"),
        low_risk_fixed_income_assets=Decimal("5000"),
        manual_equity_fund_assets=Decimal("123.45"),
        manual_bond_fund_assets=Decimal("234.56"),
        manual_sector_fund_assets=Decimal("345.67"),
        dependents=0,
        other_volatile_assets=Decimal("456.78"),
        maximum_tolerable_loss=Decimal("20000"),
        maximum_tolerable_drawdown=Decimal("0.20"),
        reaction_10=RiskReaction.HOLD,
        reaction_20=RiskReaction.HOLD,
        reaction_30=RiskReaction.REDUCE,
        experienced_material_loss=False,
        understands_multi_year_recovery=True,
        can_postpone_goal_use=True,
        debts=(
            Debt(
                debt_type="mortgage",
                outstanding_principal=Decimal("500000"),
                effective_annual_rate=Decimal("0.035"),
                monthly_payment=Decimal("1500"),
                maturity_date=date(2045, 1, 1),
                delinquent=False,
                revolving_interest=False,
            ),
        ),
        obligations=(
            PlannedObligation(
                name="education",
                amount=Decimal("10000"),
                due_date=date(2027, 9, 1),
                amount_already_reserved=Decimal("3000"),
            ),
        ),
        goals=(
            FinancialGoal(
                name="long-term growth",
                target_amount=Decimal("200000"),
                target_date=date(2034, 1, 1),
                priority=1,
                amount_already_reserved=Decimal("20000"),
                temporary_principal_loss_acceptable=True,
                use_date_can_be_postponed=True,
            ),
        ),
        confirmed_at=NOW,
    )


class SuitabilityModelsTest(unittest.TestCase):
    def test_unsafe_private_text_codepoints_are_explicit_and_keep_emoji_zwj(self) -> None:
        expected = frozenset(
            {
                0x061C,
                0x200E,
                0x200F,
                0x2028,
                0x2029,
                *range(0x202A, 0x202F),
                *range(0x2066, 0x2070),
            }
        )

        self.assertEqual(UNSAFE_PRIVATE_TEXT_CODEPOINTS, expected)
        self.assertNotIn(0x200D, UNSAFE_PRIVATE_TEXT_CODEPOINTS)

    def test_complete_profile_round_trips_without_float_values(self) -> None:
        profile = valid_profile()
        profile.validate()
        encoded = encode_profile(profile)
        self.assertNotIn(b"12000.0", encoded)
        self.assertEqual(decode_profile(encoded), profile)

    def test_private_names_round_trip_chinese_emoji_and_maximum_length(self) -> None:
        self.assertEqual(MAX_PRIVATE_NAME_CHARS, MAX_TEXT_CHARS)
        profile = valid_profile()
        maximum_name = "目" * (MAX_PRIVATE_NAME_CHARS - 1) + "\U0001f680"
        profile = replace(
            profile,
            obligations=(
                replace(profile.obligations[0], name="家庭\U0001f469\u200d\U0001f4bb计划"),
            ),
            goals=(replace(profile.goals[0], name=maximum_name),),
        )

        decoded = decode_profile(encode_profile(profile))

        self.assertEqual(decoded, profile)
        self.assertEqual(len(decoded.goals[0].name), MAX_PRIVATE_NAME_CHARS)

    def test_private_names_reject_wrong_type_blank_controls_surrogates_and_overlong(
        self,
    ) -> None:
        class StringSubclass(str):
            pass

        profile = valid_profile()
        invalid_names = (
            None,
            StringSubclass("subclass"),
            "",
            "   ",
            "bad\x00name",
            "bad\x1fname",
            "bad\x1b[2Jname",
            "bad\x7fname",
            "bad\x85name",
            "bad\ud800name",
            "x" * (MAX_PRIVATE_NAME_CHARS + 1),
            *(f"bad{chr(codepoint)}name" for codepoint in UNSAFE_PRIVATE_TEXT_CODEPOINTS),
        )
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=ascii(invalid_name)):
                obligation = replace(profile.obligations[0], name=invalid_name)
                goal = replace(profile.goals[0], name=invalid_name)
                with self.assertRaises(ValueError):
                    obligation.validate()
                with self.assertRaises(ValueError):
                    goal.validate()

    def test_profile_serializer_applies_private_name_validation_consistently(self) -> None:
        profile = valid_profile()
        base_cases = (
            (("obligations", 0, "name"), "bad\x00name"),
            (("obligations", 0, "name"), "bad\x1b[2Jname"),
            (("obligations", 0, "name"), "bad\x85name"),
            (("goals", 0, "name"), "bad\ud800name"),
            (("goals", 0, "name"), "x" * (MAX_PRIVATE_NAME_CHARS + 1)),
        )
        formatting_cases = tuple(
            (
                ("obligations" if index % 2 == 0 else "goals", 0, "name"),
                f"bad{chr(codepoint)}name",
            )
            for index, codepoint in enumerate(sorted(UNSAFE_PRIVATE_TEXT_CODEPOINTS))
        )
        for path, invalid_name in base_cases + formatting_cases:
            with self.subTest(path=path, invalid_name=ascii(invalid_name)):
                if path[0] == "obligations":
                    invalid_profile = replace(
                        profile,
                        obligations=(replace(profile.obligations[0], name=invalid_name),),
                    )
                else:
                    invalid_profile = replace(
                        profile,
                        goals=(replace(profile.goals[0], name=invalid_name),),
                    )
                with self.assertRaises(ValueError):
                    encode_profile(invalid_profile)

                payload = json.loads(encode_profile(profile).decode("utf-8"))
                payload[path[0]][path[1]][path[2]] = invalid_name
                encoded = json.dumps(payload, ensure_ascii=True).encode("utf-8")
                with self.assertRaises(ValueError):
                    decode_profile(encoded)

    def test_optional_debt_maturity_round_trips(self) -> None:
        profile = valid_profile()
        debt = Debt(**{**profile.debts[0].__dict__, "maturity_date": None})
        profile = FinancialProfile(**{**profile.__dict__, "debts": (debt,)})

        decoded = decode_profile(encode_profile(profile))

        self.assertIsNone(decoded.debts[0].maturity_date)

    def test_blank_debt_type_round_trips_as_an_unknown_fact_without_floats(self) -> None:
        for debt_type in ("", "   "):
            with self.subTest(debt_type=debt_type):
                profile = valid_profile()
                debt = Debt(**{**profile.debts[0].__dict__, "debt_type": debt_type})
                profile = FinancialProfile(**{**profile.__dict__, "debts": (debt,)})

                encoded = encode_profile(profile)
                decoded = decode_profile(encoded)

                self.assertEqual(decoded.debts[0].debt_type, debt_type)
                self.assertNotIn(b"500000.0", encoded)

    def test_debt_type_must_remain_a_string(self) -> None:
        profile = valid_profile()
        debt = Debt(**{**profile.debts[0].__dict__, "debt_type": None})

        with self.assertRaisesRegex(ValueError, "debt type must be a string"):
            debt.validate()

    def test_manual_asset_fields_round_trip_exactly(self) -> None:
        decoded = decode_profile(encode_profile(valid_profile()))

        self.assertEqual(decoded.manual_equity_fund_assets, Decimal("123.45"))
        self.assertEqual(decoded.manual_bond_fund_assets, Decimal("234.56"))
        self.assertEqual(decoded.manual_sector_fund_assets, Decimal("345.67"))
        self.assertEqual(decoded.other_volatile_assets, Decimal("456.78"))

    def test_negative_amount_is_rejected(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(**{**profile.__dict__, "emergency_reserve": Decimal("-1")})
        with self.assertRaisesRegex(ValueError, "emergency reserve cannot be negative"):
            invalid.validate()

    def test_drawdown_must_be_a_fraction(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(
            **{**profile.__dict__, "maximum_tolerable_drawdown": Decimal("20")}
        )
        with self.assertRaisesRegex(ValueError, "drawdown must be between zero and one"):
            invalid.validate()

        wrong_type = FinancialProfile(**{**profile.__dict__, "maximum_tolerable_drawdown": "0.20"})
        with self.assertRaisesRegex(ValueError, "drawdown must be a Decimal"):
            wrong_type.validate()

    def test_confirmed_at_must_be_timezone_aware(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(
            **{**profile.__dict__, "confirmed_at": datetime(2026, 7, 12, 12)}
        )
        with self.assertRaisesRegex(ValueError, "confirmed_at must be timezone-aware"):
            invalid.validate()

    def test_reserved_obligation_cannot_exceed_amount(self) -> None:
        obligation = PlannedObligation(
            "education", Decimal("100"), date(2027, 1, 1), Decimal("101")
        )
        with self.assertRaisesRegex(ValueError, "reserved obligation amount"):
            obligation.validate()

    def test_decoder_rejects_unexpected_keys(self) -> None:
        payload = json.loads(encode_profile(valid_profile()).decode("utf-8"))
        payload["unexpected"] = "value"

        with self.assertRaisesRegex(ValueError, "unexpected profile keys"):
            decode_profile(json.dumps(payload).encode("utf-8"))

    def test_decoder_rejects_non_standard_json_numbers(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                encoded = encode_profile(valid_profile()).replace(b'"12000"', constant.encode())
                with self.assertRaisesRegex(ValueError, "JSON constant values are not allowed"):
                    decode_profile(encoded)

    def test_decoder_rejects_non_boolean_values_for_every_boolean_field(self) -> None:
        mutations = (
            (("income_interruption_risk",), 1),
            (("experienced_material_loss",), "false"),
            (("understands_multi_year_recovery",), 0),
            (("can_postpone_goal_use",), "true"),
            (("debts", 0, "delinquent"), 1),
            (("debts", 0, "revolving_interest"), "false"),
            (("goals", 0, "temporary_principal_loss_acceptable"), 1),
            (("goals", 0, "use_date_can_be_postponed"), "true"),
        )
        for path, invalid_value in mutations:
            with self.subTest(path=path, invalid_value=invalid_value):
                payload = json.loads(encode_profile(valid_profile()).decode("utf-8"))
                target = payload
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = invalid_value
                with self.assertRaisesRegex(ValueError, "must be a boolean"):
                    decode_profile(json.dumps(payload).encode("utf-8"))

    def test_decoder_rejects_non_integer_dependents_and_priority(self) -> None:
        for field, invalid_value in (
            ("dependents", True),
            ("dependents", "0"),
            ("priority", False),
            ("priority", "1"),
        ):
            with self.subTest(field=field, invalid_value=invalid_value):
                payload = json.loads(encode_profile(valid_profile()).decode("utf-8"))
                if field == "priority":
                    payload["goals"][0][field] = invalid_value
                else:
                    payload[field] = invalid_value
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    decode_profile(json.dumps(payload).encode("utf-8"))

    def test_direct_models_reject_wrong_enum_and_boolean_types(self) -> None:
        profile = valid_profile()
        invalid_profiles = (
            ("income_stability", "stable", "income_stability must be an IncomeStability"),
            ("reaction_10", "hold", "reaction_10 must be a RiskReaction"),
            ("income_interruption_risk", 0, "income_interruption_risk must be a boolean"),
        )
        for field, value, message in invalid_profiles:
            with self.subTest(field=field):
                invalid = FinancialProfile(**{**profile.__dict__, field: value})
                with self.assertRaisesRegex(ValueError, message):
                    invalid.validate()

        debt = Debt(**{**profile.debts[0].__dict__, "delinquent": 0})
        with self.assertRaisesRegex(ValueError, "delinquent must be a boolean"):
            debt.validate()

        goal = FinancialGoal(
            **{**profile.goals[0].__dict__, "temporary_principal_loss_acceptable": 1}
        )
        with self.assertRaisesRegex(
            ValueError, "temporary_principal_loss_acceptable must be a boolean"
        ):
            goal.validate()

    def test_direct_models_reject_bool_or_non_integer_counts(self) -> None:
        profile = valid_profile()
        for invalid_value in (True, "0"):
            with self.subTest(dependents=invalid_value):
                invalid = FinancialProfile(**{**profile.__dict__, "dependents": invalid_value})
                with self.assertRaisesRegex(ValueError, "dependents must be an integer"):
                    invalid.validate()
        for invalid_value in (False, "1"):
            with self.subTest(priority=invalid_value):
                goal = FinancialGoal(**{**profile.goals[0].__dict__, "priority": invalid_value})
                with self.assertRaisesRegex(ValueError, "goal priority must be an integer"):
                    goal.validate()

    def test_direct_models_reject_wrong_date_and_datetime_types(self) -> None:
        profile = valid_profile()
        debt = Debt(**{**profile.debts[0].__dict__, "maturity_date": NOW})
        with self.assertRaisesRegex(ValueError, "maturity_date must be a date or None"):
            debt.validate()

        obligation = PlannedObligation(
            **{**profile.obligations[0].__dict__, "due_date": "2027-09-01"}
        )
        with self.assertRaisesRegex(ValueError, "due_date must be a date"):
            obligation.validate()

        goal = FinancialGoal(**{**profile.goals[0].__dict__, "target_date": NOW})
        with self.assertRaisesRegex(ValueError, "target_date must be a date"):
            goal.validate()

        invalid = FinancialProfile(**{**profile.__dict__, "confirmed_at": date(2026, 7, 12)})
        with self.assertRaisesRegex(ValueError, "confirmed_at must be a datetime"):
            invalid.validate()


if __name__ == "__main__":
    unittest.main()
