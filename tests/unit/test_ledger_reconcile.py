import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from kunjin.ledger.models import (
    EvidenceLevel,
    LedgerDraft,
    LedgerTransaction,
    TransactionType,
)
from kunjin.ledger.reconcile import reconcile_fund
from kunjin.models import StoredPosition

NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def position(**overrides) -> StoredPosition:
    values = {
        "account_title": "Alipay",
        "fund_code": "519755",
        "fund_name": "Fund 519755",
        "shares": Decimal("11.32"),
        "observed_at": NOW,
        "formal_nav": Decimal("1.7467"),
        "estimated_nav": None,
        "observed_profit": Decimal("-0.23"),
    }
    values.update(overrides)
    return StoredPosition(**values)


def transaction(
    transaction_type: TransactionType = TransactionType.SUBSCRIPTION,
    amount: Decimal = Decimal("20.00"),
    fund_code: str = "519755",
    evidence_level: EvidenceLevel = EvidenceLevel.USER_CONFIRMED,
    amount_evidence: EvidenceLevel = EvidenceLevel.USER_CONFIRMED,
) -> LedgerTransaction:
    return LedgerTransaction(
        id=1,
        source_document_id=None,
        transaction_type=transaction_type,
        fund_code=fund_code,
        fund_name=None,
        amount=amount,
        shares=None,
        nav=None,
        fee=None,
        order_time=NOW,
        confirmation_time=None,
        evidence_level=evidence_level,
        field_evidence={"amount": amount_evidence.value},
        created_at=NOW,
    )


def draft(
    transaction_type: TransactionType = TransactionType.SUBSCRIPTION,
    amount: Decimal = Decimal("1.00"),
    fund_code: str = "519755",
    evidence_level: EvidenceLevel = EvidenceLevel.USER_CONFIRMED,
    amount_evidence: EvidenceLevel = EvidenceLevel.USER_CONFIRMED,
) -> LedgerDraft:
    return LedgerDraft(
        id=1,
        source_document_id=None,
        transaction_type=transaction_type,
        fund_code=fund_code,
        fund_name=None,
        amount=amount,
        shares=None,
        nav=None,
        fee=None,
        order_time=NOW,
        confirmation_time=None,
        evidence_level=evidence_level,
        field_evidence={"amount": amount_evidence.value},
        status="pending",
        created_at=NOW,
    )


class LedgerReconciliationTest(unittest.TestCase):
    def test_approved_519755_scenario_is_consistent(self) -> None:
        result = reconcile_fund(position(), [transaction()], pending_drafts=[])

        self.assertEqual(result.status, "consistent")
        self.assertEqual(result.confirmed_cash_flow, Decimal("20.00"))
        self.assertEqual(result.inferred_position_cost, Decimal("20.002644"))
        self.assertEqual(result.difference, Decimal("-0.002644"))
        self.assertEqual(result.tolerance, Decimal("0.040005288"))
        self.assertEqual(result.evidence_level, EvidenceLevel.POSITION_INFERRED)

    def test_missing_observed_profit_is_insufficient_data(self) -> None:
        result = reconcile_fund(
            position(observed_profit=None), [transaction()], pending_drafts=[]
        )

        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.inferred_position_cost)
        self.assertIsNone(result.difference)
        self.assertIn("observed profit is missing", result.warnings)

    def test_missing_nav_is_insufficient_data(self) -> None:
        result = reconcile_fund(
            position(formal_nav=None, estimated_nav=None),
            [transaction()],
            pending_drafts=[],
        )

        self.assertEqual(result.status, "insufficient_data")
        self.assertIn("position NAV is missing", result.warnings)

    def test_no_confirmed_cost_transaction_is_insufficient_data(self) -> None:
        result = reconcile_fund(position(), [], pending_drafts=[])

        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.confirmed_cash_flow)
        self.assertIn("confirmed acquisition cash flow is missing", result.warnings)

    def test_pending_draft_can_explain_difference(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-1.23")),
            [transaction()],
            pending_drafts=[draft(amount=Decimal("1.00"))],
        )

        self.assertEqual(result.status, "explainable_difference")
        self.assertEqual(result.difference, Decimal("-1.002644"))
        self.assertEqual(result.confirmed_cash_flow, Decimal("20.00"))

    def test_uncovered_difference_needs_investigation(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-2.23")),
            [transaction()],
            pending_drafts=[draft(amount=Decimal("1.00"))],
        )

        self.assertEqual(result.status, "needs_investigation")

    def test_pending_inflow_in_the_wrong_direction_does_not_explain_difference(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("0.772644")),
            [transaction()],
            pending_drafts=[draft(amount=Decimal("1.00"))],
        )

        self.assertEqual(result.difference, Decimal("1.000000"))
        self.assertEqual(result.status, "needs_investigation")

    def test_formal_nav_is_preferred_over_estimated_nav(self) -> None:
        result = reconcile_fund(
            position(estimated_nav=Decimal("9.9999")),
            [transaction()],
            pending_drafts=[],
        )

        self.assertEqual(result.inferred_position_cost, Decimal("20.002644"))
        self.assertNotIn("estimated NAV was used", result.warnings)

    def test_estimated_nav_fallback_is_warned(self) -> None:
        result = reconcile_fund(
            position(formal_nav=None, estimated_nav=Decimal("1.7467")),
            [transaction()],
            pending_drafts=[],
        )

        self.assertEqual(result.status, "consistent")
        self.assertIn("estimated NAV was used", result.warnings)

    def test_subscription_types_are_accumulated_as_confirmed_acquisition_cost(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-3.23")),
            [
                transaction(TransactionType.SUBSCRIPTION, Decimal("10")),
                transaction(TransactionType.RECURRING_SUBSCRIPTION, Decimal("13")),
            ],
            pending_drafts=[],
        )

        self.assertEqual(result.confirmed_cash_flow, Decimal("23"))
        self.assertEqual(result.status, "consistent")

    def test_non_acquisition_confirmed_events_force_insufficient_data(self) -> None:
        unsupported_types = (
            TransactionType.REDEMPTION,
            TransactionType.CONVERSION_IN,
            TransactionType.CONVERSION_OUT,
            TransactionType.CASH_DIVIDEND,
            TransactionType.REINVESTED_DIVIDEND,
        )
        for transaction_type in unsupported_types:
            with self.subTest(transaction_type=transaction_type):
                result = reconcile_fund(
                    position(),
                    [transaction(), transaction(transaction_type, Decimal("1"))],
                    pending_drafts=[],
                )

                self.assertEqual(result.confirmed_cash_flow, Decimal("20.00"))
                self.assertEqual(result.status, "insufficient_data")
                self.assertTrue(
                    any(transaction_type.value in warning for warning in result.warnings)
                )

    def test_missing_transaction_and_draft_amounts_are_warned(self) -> None:
        missing_transaction = replace(transaction(), amount=None)
        missing_draft = replace(draft(), amount=None)

        result = reconcile_fund(
            position(),
            [transaction(), missing_transaction],
            pending_drafts=[missing_draft],
        )

        self.assertIn("subscription transaction amount is missing", result.warnings)
        self.assertIn("subscription pending draft amount is missing", result.warnings)
        self.assertEqual(result.status, "insufficient_data")

    def test_unconfirmed_amount_evidence_is_not_acquisition_cost(self) -> None:
        cases = (
            transaction(
                evidence_level=EvidenceLevel.POSITION_INFERRED,
                amount_evidence=EvidenceLevel.USER_CONFIRMED,
            ),
            transaction(amount_evidence=EvidenceLevel.POSITION_INFERRED),
            replace(transaction(), field_evidence={}),
        )
        for unconfirmed_transaction in cases:
            with self.subTest(transaction=unconfirmed_transaction):
                result = reconcile_fund(
                    position(),
                    [unconfirmed_transaction],
                    pending_drafts=[],
                )

                self.assertIsNone(result.confirmed_cash_flow)
                self.assertEqual(result.status, "insufficient_data")
                self.assertIn(
                    "subscription transaction amount is not confirmed",
                    result.warnings,
                )

    def test_unreliable_pending_amount_does_not_explain_difference(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-1.23")),
            [transaction()],
            pending_drafts=[
                draft(amount_evidence=EvidenceLevel.POSITION_INFERRED)
            ],
        )

        self.assertEqual(result.status, "needs_investigation")
        self.assertIn("subscription pending draft amount is not confirmed", result.warnings)

    def test_unsupported_pending_type_warns_but_does_not_explain(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-1.23")),
            [transaction()],
            pending_drafts=[draft(TransactionType.CONVERSION_IN)],
        )

        self.assertEqual(result.status, "needs_investigation")
        self.assertTrue(
            any("conversion_in" in warning for warning in result.warnings)
        )

    def test_only_pending_drafts_for_same_fund_and_status_explain_difference(self) -> None:
        result = reconcile_fund(
            position(observed_profit=Decimal("-1.23")),
            [transaction()],
            pending_drafts=[
                draft(amount=Decimal("1.00"), fund_code="000001"),
                replace(draft(), status="rejected"),
            ],
        )

        self.assertEqual(result.status, "needs_investigation")

    def test_zero_or_negative_formal_nav_is_invalid_without_estimated_fallback(self) -> None:
        for formal_nav in (Decimal("0"), Decimal("-1")):
            with self.subTest(formal_nav=formal_nav):
                result = reconcile_fund(
                    position(formal_nav=formal_nav, estimated_nav=Decimal("1.7467")),
                    [transaction()],
                    pending_drafts=[],
                )

                self.assertIsNone(result.inferred_position_cost)
                self.assertEqual(result.status, "insufficient_data")
                self.assertIn("formal NAV must be positive", result.warnings)
                self.assertNotIn("estimated NAV was used", result.warnings)

    def test_zero_or_negative_estimated_nav_is_invalid(self) -> None:
        for estimated_nav in (Decimal("0"), Decimal("-1")):
            with self.subTest(estimated_nav=estimated_nav):
                result = reconcile_fund(
                    position(formal_nav=None, estimated_nav=estimated_nav),
                    [transaction()],
                    pending_drafts=[],
                )

                self.assertIsNone(result.inferred_position_cost)
                self.assertEqual(result.status, "insufficient_data")
                self.assertIn("estimated NAV must be positive", result.warnings)

    def test_zero_or_negative_shares_are_insufficient_data(self) -> None:
        for shares in (Decimal("0"), Decimal("-1")):
            with self.subTest(shares=shares):
                result = reconcile_fund(
                    position(shares=shares), [transaction()], pending_drafts=[]
                )

                self.assertIsNone(result.inferred_position_cost)
                self.assertEqual(result.status, "insufficient_data")
                self.assertIn("position shares must be positive", result.warnings)


if __name__ == "__main__":
    unittest.main()
