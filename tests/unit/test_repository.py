import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.models import AccountObservation, PositionObservation
from kunjin.storage.repository import Repository


class RepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.now = datetime.now(timezone.utc)
        self.account = AccountObservation("yangjibao", "account-1", "学习账户", self.now)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def position(self, code: str) -> PositionObservation:
        return PositionObservation(
            source_account_id="account-1",
            fund_code=code,
            fund_name="测试基金",
            shares=Decimal("10"),
            formal_nav=Decimal("1.2"),
            observed_at=self.now,
        )

    def test_migrate_creates_phase_one_tables(self) -> None:
        expected = {"schema_migrations", "sync_runs", "raw_snapshots", "accounts", "positions"}
        self.assertTrue(expected <= self.repository.table_names())

    def test_invalid_batch_rolls_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid fund code"):
            self.repository.replace_snapshot(
                self.account,
                [self.position("000001"), self.position("invalid")],
            )

        self.assertEqual(self.repository.latest_positions(), [])

    def test_valid_snapshot_can_be_read(self) -> None:
        self.repository.replace_snapshot(self.account, [self.position("000001")])

        stored = self.repository.latest_positions()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].fund_code, "000001")
        self.assertEqual(stored[0].formal_nav, Decimal("1.2"))


if __name__ == "__main__":
    unittest.main()
