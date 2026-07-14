import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import kunjin.storage.repository as repository_module
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

    def test_enable_wal_retries_database_locked_once(self) -> None:
        class Connection:
            def __init__(self) -> None:
                self.statements = []

            def execute(self, statement: str) -> None:
                self.statements.append(statement)
                if len(self.statements) == 1:
                    raise sqlite3.OperationalError("database is locked")

        connection = Connection()
        with patch("kunjin.storage.repository.time.sleep") as sleep:
            repository_module._enable_wal(connection)

        self.assertEqual(
            connection.statements,
            ["PRAGMA journal_mode = WAL", "PRAGMA journal_mode = WAL"],
        )
        sleep.assert_called_once_with(0.01)

    def test_enable_wal_reraises_database_locked_at_deadline(self) -> None:
        error = sqlite3.OperationalError("database is locked")

        class Connection:
            def __init__(self) -> None:
                self.statements = []

            def execute(self, statement: str) -> None:
                self.statements.append(statement)
                raise error

        connection = Connection()
        with (
            patch(
                "kunjin.storage.repository.time.monotonic",
                side_effect=[10.0, 14.0, 15.0],
            ) as monotonic,
            patch("kunjin.storage.repository.time.sleep") as sleep,
        ):
            with self.assertRaises(sqlite3.OperationalError) as raised:
                repository_module._enable_wal(connection)

        self.assertIs(raised.exception, error)
        self.assertEqual(
            connection.statements,
            ["PRAGMA journal_mode = WAL", "PRAGMA journal_mode = WAL"],
        )
        sleep.assert_called_once_with(0.01)
        self.assertEqual(monotonic.call_count, 3)

    def test_enable_wal_reraises_unrelated_operational_error(self) -> None:
        error = sqlite3.OperationalError("disk I/O error")

        class Connection:
            def execute(self, statement: str) -> None:
                del statement
                raise error

        with patch("kunjin.storage.repository.time.sleep") as sleep:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                repository_module._enable_wal(Connection())

        self.assertIs(raised.exception, error)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
