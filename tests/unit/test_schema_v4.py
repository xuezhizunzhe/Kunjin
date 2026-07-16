import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.storage.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3


class SchemaV4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_adds_ledger_tables(self) -> None:
        expected = {
            "imported_documents",
            "ocr_fields",
            "transaction_drafts",
            "transactions",
        }
        self.assertTrue(expected <= self.repository.table_names())

    def test_transactions_are_immutable_at_database_level(self) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_type, fund_code, evidence_level,
                    field_evidence_json, created_at
                ) VALUES (
                    'subscription', '519755', 'user_confirmed', '{}',
                    '2026-07-11T00:00:00+00:00'
                )
                """
            )
            with self.assertRaisesRegex(Exception, "transactions are immutable"):
                connection.execute("UPDATE transactions SET fund_code = '000001' WHERE id = 1")
            with self.assertRaisesRegex(Exception, "transactions are immutable"):
                connection.execute("DELETE FROM transactions WHERE id = 1")

    def test_migration_upgrades_version_three_database(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "kunjin-version-three.db")
        with repository.connect() as connection, connection:
            connection.executescript(SCHEMA_V1)
            connection.executescript(SCHEMA_V2)
            connection.executescript(SCHEMA_V3)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [
                    (1, "2026-07-01T00:00:00+00:00"),
                    (2, "2026-07-02T00:00:00+00:00"),
                    (3, "2026-07-03T00:00:00+00:00"),
                ],
            )

        repository.migrate()

        with repository.connect() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual([int(row["version"]) for row in rows], list(range(1, 15)))
        self.assertTrue(
            {
                "imported_documents",
                "ocr_fields",
                "transaction_drafts",
                "transactions",
            }
            <= repository.table_names()
        )


if __name__ == "__main__":
    unittest.main()
