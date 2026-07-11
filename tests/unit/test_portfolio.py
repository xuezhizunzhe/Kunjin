import unittest
from datetime import datetime, timezone
from decimal import Decimal

from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.models import StoredPosition


def position(code, shares, nav):
    return StoredPosition(
        account_title="学习账户",
        fund_code=code,
        fund_name=code,
        shares=Decimal(shares),
        formal_nav=None if nav is None else Decimal(nav),
        observed_at=datetime.now(timezone.utc),
        observed_profit=Decimal("0"),
    )


class PortfolioAnalysisTest(unittest.TestCase):
    def test_analysis_calculates_weights_and_hhi(self) -> None:
        result = analyze_portfolio([position("000001", "60", "1"), position("000002", "40", "1")])

        self.assertEqual(result.total_value, Decimal("100"))
        self.assertEqual(result.weights["000001"], Decimal("0.6"))
        self.assertEqual(result.hhi, Decimal("0.52"))

    def test_missing_nav_returns_insufficient_data(self) -> None:
        result = analyze_portfolio([position("000001", "10", None)])

        self.assertIsNone(result.total_value)
        self.assertEqual(result.evidence_level, "insufficient_data")
        self.assertTrue(any("missing NAV" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
