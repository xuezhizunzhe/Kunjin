import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.models import FundNavObservation, SectorObservation
from kunjin.storage.repository import Repository


class SchemaV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_adds_research_tables(self) -> None:
        self.assertTrue({"funds", "fund_nav", "sector_snapshots"} <= self.repository.table_names())

    def test_fund_history_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        item = FundNavObservation(
            "017811",
            date(2026, 7, 10),
            Decimal("1.2"),
            Decimal("1.2"),
            Decimal("0.5"),
            "eastmoney",
            now,
        )
        self.repository.save_fund_history("017811", "人工智能混合C", "混合型", "eastmoney", [item])

        stored = self.repository.fund_history("017811")
        self.assertEqual(stored[0].unit_nav, Decimal("1.2"))
        self.assertEqual(self.repository.fund_profile("017811")["fund_type"], "混合型")

    def test_sector_snapshot_round_trip(self) -> None:
        now = datetime.now(timezone.utc)
        item = SectorObservation(
            "BK1",
            "半导体",
            "industry",
            Decimal("1.2"),
            Decimal("3.4"),
            20,
            10,
            "eastmoney",
            now,
        )
        self.repository.save_sector_snapshots([item])

        self.assertEqual(self.repository.latest_sector_snapshots()[0].sector_name, "半导体")


if __name__ == "__main__":
    unittest.main()
