from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from kunjin.suitability.editor import ProfileEditor
from kunjin.suitability.models import (
    UNSAFE_PRIVATE_TEXT_CODEPOINTS,
    DebtType,
    IncomeStability,
    RiskReaction,
)

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
APPROVED_DEBT_TYPES = (
    "mortgage",
    "auto_loan",
    "credit_card",
    "consumer_loan",
    "personal_loan",
    "student_loan",
    "business_loan",
    "other",
)
SUPPORTED_DEBT_TYPES = (
    "mortgage",
    "auto_loan",
    "credit_card",
    "consumer_loan",
    "personal_loan",
    "student_loan",
)
UNCLASSIFIED_DEBT_WARNING = (
    "This debt type cannot pass suitability policy v1 until its risk type is clarified."
)
ASSET_EXCLUSIVITY_EXPLANATION = (
    "Asset balances are mutually exclusive as-of balances: enter each real "
    "balance in exactly one field. The engine cannot prove whether entries "
    "overlap real accounts. Any real product entered under Low-risk "
    "fixed-income assets is only the owner's bookkeeping classification; it "
    "is not verified as low risk or high quality and is not placed in Phase C's "
    "abstract high_quality_fixed_income bucket. Phase D verification is "
    "required."
)
GOAL_DATE_EXPLANATION = (
    "Goal dates remain authoritative until a new profile is confirmed. If "
    "'Can postpone use of goal funds?' is no, it conflicts with any "
    "goal-level yes; a profile-level yes does not override a goal-level no. "
    "Dates are never extended automatically."
)


class RecordingService:
    def __init__(self) -> None:
        self.profiles = []

    def confirm_profile(self, profile):
        self.profiles.append(profile)
        return SimpleNamespace(version=7)


def base_answers(*, confirmation: str = "yes") -> list[str]:
    return [
        "CNY",
        "12000",
        "5000",
        "1500",
        "1000",
        "3000",
        "1000",
        "stable",
        "no",
        "50000",
        "10000",
        "40000",
        "5000",
        "6000",
        "7000",
        "8000",
        "0",
        "9000",
        "20000",
        "20",
        "hold",
        "hold",
        "reduce",
        "no",
        "yes",
        "yes",
        "no",
        "no",
        "no",
        confirmation,
    ]


def run_editor(answers: list[str], events: list[tuple[str, str]] | None = None):
    iterator = iter(answers)
    prompts: list[str] = []
    output: list[str] = []
    service = RecordingService()

    def reader(prompt: str) -> str:
        prompts.append(prompt)
        if events is not None:
            events.append(("prompt", prompt))
        return next(iterator)

    def writer(message: str) -> None:
        output.append(message)
        if events is not None:
            events.append(("output", message))

    result = ProfileEditor(
        service,
        reader=reader,
        writer=writer,
        now=lambda: NOW,
    ).edit()
    return result, service, prompts, output


def answers_with_debt_type(*debt_type_answers: str) -> list[str]:
    answers = base_answers()
    answers[26:29] = [
        "yes",
        *debt_type_answers,
        "500000",
        "3.5",
        "1500",
        "no",
        "no",
        "no",
        "no",
        "no",
        "no",
    ]
    return answers


class ProfileEditorTest(unittest.TestCase):
    def test_invalid_decimal_is_reprompted_without_a_traceback(self) -> None:
        answers = base_answers()
        answers[1:2] = ["1,200", "1e3", "nan", "-1", "12000"]

        result, service, prompts, output = run_editor(answers)

        self.assertEqual(result, {"status": "confirmed", "version": 7})
        self.assertEqual(len(service.profiles), 1)
        self.assertGreaterEqual(sum("Monthly net income" in prompt for prompt in prompts), 5)
        rendered = "\n".join(output)
        self.assertIn("Enter a non-negative decimal without commas", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_cancel_exits_without_confirming(self) -> None:
        result, service, _, output = run_editor(["cancel"])

        self.assertEqual(result, {"status": "cancelled"})
        self.assertEqual(service.profiles, [])
        self.assertIn("Profile edit cancelled.", output)

    def test_no_at_final_confirmation_stores_nothing(self) -> None:
        result, service, _, _ = run_editor(base_answers(confirmation="no"))

        self.assertEqual(result, {"status": "cancelled"})
        self.assertEqual(service.profiles, [])

    def test_yes_stores_one_complete_profile_and_returns_metadata_only(self) -> None:
        result, service, _, _ = run_editor(base_answers())

        self.assertEqual(result, {"status": "confirmed", "version": 7})
        self.assertEqual(set(result), {"status", "version"})
        self.assertEqual(len(service.profiles), 1)
        profile = service.profiles[0]
        profile.validate()
        self.assertEqual(profile.manual_equity_fund_assets, Decimal("6000"))
        self.assertEqual(profile.manual_bond_fund_assets, Decimal("7000"))
        self.assertEqual(profile.manual_sector_fund_assets, Decimal("8000"))
        self.assertEqual(profile.income_stability, IncomeStability.STABLE)
        self.assertEqual(profile.reaction_30, RiskReaction.REDUCE)
        self.assertEqual(profile.maximum_tolerable_drawdown, Decimal("0.2"))
        self.assertEqual(profile.confirmed_at, NOW)

    def test_asset_prompts_explain_exclusivity_and_classification_limits(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        _, _, prompts, output = run_editor(base_answers(), events)

        self.assertEqual(output.count(ASSET_EXCLUSIVITY_EXPLANATION), 1)
        self.assertLess(
            events.index(("output", ASSET_EXCLUSIVITY_EXPLANATION)),
            events.index(("prompt", "Immediately available cash: ")),
        )
        self.assertEqual(
            prompts[9],
            "Immediately available cash: ",
        )

    def test_private_names_reprompt_without_echoing_rejected_input(self) -> None:
        formatting_family_representatives = (0x061C, 0x200E, 0x2028, 0x202A, 0x2066)
        self.assertTrue(set(formatting_family_representatives) <= UNSAFE_PRIVATE_TEXT_CODEPOINTS)
        rejected_obligation_names = (
            "secret\x1b[2J",
            "secret\x85",
            "secret\ud800",
            "x" * 4097,
            *(f"secret{chr(codepoint)}" for codepoint in formatting_family_representatives),
        )
        rejected_goal_names = ("secret\x00", "secret\x1f")
        answers = base_answers()
        answers[26:29] = [
            "no",
            "yes",
            *rejected_obligation_names,
            "家庭\U0001f469\u200d\U0001f4bb计划",
            "10000",
            "2027-09-01",
            "3000",
            "no",
            "yes",
            *rejected_goal_names,
            "长期目标\U0001f680",
            "200000",
            "2034-01-01",
            "1",
            "20000",
            "yes",
            "yes",
            "no",
            "yes",
        ]

        result, service, _, output = run_editor(answers)

        self.assertEqual(result, {"status": "confirmed", "version": 7})
        self.assertEqual(
            service.profiles[0].obligations[0].name,
            "家庭\U0001f469\u200d\U0001f4bb计划",
        )
        self.assertEqual(service.profiles[0].goals[0].name, "长期目标\U0001f680")
        rendered = "\n".join(output)
        for rejected in rejected_obligation_names + rejected_goal_names:
            self.assertNotIn(rejected, rendered)
        self.assertNotIn("\x1b[2J", rendered)

    def test_goal_prompts_explain_authoritative_dates_and_postponement_rules(
        self,
    ) -> None:
        events: list[tuple[str, str]] = []
        _, _, prompts, output = run_editor(base_answers(), events)

        self.assertEqual(output.count(GOAL_DATE_EXPLANATION), 1)
        self.assertLess(
            events.index(("output", GOAL_DATE_EXPLANATION)),
            events.index(("prompt", "Add a financial goal? [yes/no]: ")),
        )
        self.assertEqual(prompts[28], "Add a financial goal? [yes/no]: ")

    def test_repeated_records_capture_every_nested_field(self) -> None:
        answers = base_answers()
        answers[26:29] = [
            "yes",
            "mortgage",
            "500000",
            "3.5",
            "1500",
            "yes",
            "2045-01-01",
            "no",
            "no",
            "no",
            "yes",
            "education",
            "10000",
            "2027-09-01",
            "3000",
            "no",
            "yes",
            "long-term growth",
            "200000",
            "2034-01-01",
            "1",
            "20000",
            "yes",
            "yes",
            "no",
        ]

        _, service, _, _ = run_editor(answers)

        profile = service.profiles[0]
        self.assertEqual(len(profile.debts), 1)
        self.assertEqual(profile.debts[0].effective_annual_rate, Decimal("0.035"))
        self.assertEqual(profile.debts[0].maturity_date.isoformat(), "2045-01-01")
        self.assertEqual(len(profile.obligations), 1)
        self.assertEqual(profile.obligations[0].amount_already_reserved, Decimal("3000"))
        self.assertEqual(len(profile.goals), 1)
        self.assertTrue(profile.goals[0].temporary_principal_loss_acceptable)

    def test_debt_type_rejects_arbitrary_text_and_stores_normalized_value(
        self,
    ) -> None:
        result, service, prompts, output = run_editor(
            answers_with_debt_type("housing loan", "mortgage")
        )

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(service.profiles[0].debts[0].debt_type, "mortgage")
        debt_type_prompt = f"Debt type [{'/'.join(APPROVED_DEBT_TYPES)}]: "
        self.assertEqual(
            [prompt for prompt in prompts if prompt.startswith("Debt type")],
            [debt_type_prompt, debt_type_prompt],
        )
        self.assertIn(
            "Choose one of: " + ", ".join(APPROVED_DEBT_TYPES) + ".",
            output,
        )

    def test_supported_debt_types_persist_without_policy_warning(self) -> None:
        for debt_type in SUPPORTED_DEBT_TYPES:
            with self.subTest(debt_type=debt_type):
                result, service, _, output = run_editor(answers_with_debt_type(debt_type))

                self.assertEqual(result["status"], "confirmed")
                self.assertEqual(
                    service.profiles[0].debts[0].debt_type,
                    debt_type,
                )
                self.assertNotIn(UNCLASSIFIED_DEBT_WARNING, output)

    def test_unclassified_normalized_debt_types_warn_locally(self) -> None:
        for debt_type in (DebtType.BUSINESS_LOAN, DebtType.OTHER):
            with self.subTest(debt_type=debt_type.value):
                result, service, _, output = run_editor(answers_with_debt_type(debt_type.value))

                self.assertEqual(result["status"], "confirmed")
                self.assertEqual(
                    service.profiles[0].debts[0].debt_type,
                    debt_type.value,
                )
                self.assertEqual(output.count(UNCLASSIFIED_DEBT_WARNING), 1)

    def test_cancel_at_debt_type_stores_nothing(self) -> None:
        answers = base_answers()
        answers[26:29] = ["yes", "cancel"]

        result, service, _, output = run_editor(answers)

        self.assertEqual(result, {"status": "cancelled"})
        self.assertEqual(service.profiles, [])
        self.assertIn("Profile edit cancelled.", output)

    def test_strict_boolean_date_and_enum_inputs_are_reprompted(self) -> None:
        answers = base_answers()
        answers[26:29] = [
            "no",
            "yes",
            "education",
            "10000",
            "2027/09/01",
            "2027-09-01",
            "3000",
            "no",
            "no",
        ]
        answers.insert(8, "y")
        answers.insert(7, "Stable")

        result, service, _, output = run_editor(answers)

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(len(service.profiles), 1)
        rendered = "\n".join(output)
        self.assertIn("Choose one of: stable, variable, unstable", rendered)
        self.assertIn("Enter exactly yes or no", rendered)
        self.assertIn("Enter a date as YYYY-MM-DD", rendered)

    def test_summary_is_local_and_exact_values_are_not_returned(self) -> None:
        result, _, _, output = run_editor(base_answers())

        rendered = "\n".join(output)
        self.assertIn("Financial profile summary", rendered)
        self.assertIn("monthly_net_income: 12000", rendered)
        self.assertIn("emergency_reserve: 40000", rendered)
        self.assertIn("manual_sector_fund_assets: 8000", rendered)
        self.assertNotIn("12000", repr(result))
        self.assertNotIn("40000", repr(result))

    def test_prompts_never_request_credentials_or_tokens(self) -> None:
        _, _, prompts, _ = run_editor(base_answers())

        prompt_text = " ".join(prompts).lower()
        for forbidden in (
            "password",
            "card number",
            "verification code",
            "token",
            "密码",
            "卡号",
            "验证码",
        ):
            self.assertNotIn(forbidden, prompt_text)


if __name__ == "__main__":
    unittest.main()
