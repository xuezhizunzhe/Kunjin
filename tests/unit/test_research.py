import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.analytics.research import analyze_fund_history, analyze_sectors
from kunjin.models import FundNavObservation, SectorObservation


class ResearchTest(unittest.TestCase):
    def nav(self, day, value):
        return FundNavObservation(
            "017811",
            day,
            Decimal(value),
            None,
            None,
            "eastmoney",
            datetime.now(timezone.utc),
        )

    def test_fund_research_calculates_drawdown_and_recovery(self) -> None:
        history = [
            self.nav(date(2026, 1, 1), "1.0"),
            self.nav(date(2026, 2, 1), "0.8"),
            self.nav(date(2026, 3, 1), "1.1"),
        ]

        result = analyze_fund_history(history)

        self.assertEqual(result["max_drawdown"], "0.2")
        self.assertEqual(result["recovery_date"], "2026-03-01")

    def test_insufficient_history_is_explicit(self) -> None:
        result = analyze_fund_history([self.nav(date(2026, 1, 1), "1.0")])
        self.assertEqual(result["evidence_level"], "insufficient_data")

    def test_sector_analysis_does_not_claim_investment_merit(self) -> None:
        now = datetime.now(timezone.utc)
        sectors = [
            SectorObservation(
                "BK1", "半导体", "industry", Decimal("2"), None, 8, 2, "eastmoney", now
            )
        ]

        result = analyze_sectors(sectors)

        self.assertEqual(result["scope"], "recent_strength_and_breadth_only")
        self.assertTrue(any("not evidence" in item for item in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
