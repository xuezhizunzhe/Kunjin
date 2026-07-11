import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.storage.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4


class SchemaV5Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_adds_disclosure_tables(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        repository.migrate()

        expected = {
            "fund_source_documents",
            "fund_identities",
            "fund_share_classes",
            "fund_manager_tenures",
            "fund_fee_rules",
            "fund_sizes",
            "fund_benchmarks",
            "fund_holdings",
            "fund_industry_exposure",
            "fund_announcements",
            "fund_section_syncs",
        }
        self.assertTrue(expected <= repository.table_names())

    def test_migration_upgrades_version_four_without_changing_ledger_data(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "version-four.db")
        with repository.connect() as connection, connection:
            connection.executescript(SCHEMA_V1)
            connection.executescript(SCHEMA_V2)
            connection.executescript(SCHEMA_V3)
            connection.executescript(SCHEMA_V4)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [
                    (1, "2026-07-01T00:00:00+00:00"),
                    (2, "2026-07-02T00:00:00+00:00"),
                    (3, "2026-07-03T00:00:00+00:00"),
                    (4, "2026-07-04T00:00:00+00:00"),
                ],
            )
            connection.execute(
                """
                INSERT INTO transactions(
                    id, transaction_type, fund_code, amount, evidence_level,
                    field_evidence_json, created_at
                ) VALUES (1, 'subscription', '519755', '20.00',
                          'user_confirmed', '{}', '2026-07-11T00:00:00+00:00')
                """
            )

        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            transaction = connection.execute(
                "SELECT id, fund_code, amount FROM transactions WHERE id = 1"
            ).fetchone()
        self.assertEqual([int(row["version"]) for row in versions], [1, 2, 3, 4, 5])
        self.assertEqual(dict(transaction), {"id": 1, "fund_code": "519755", "amount": "20.00"})


if __name__ == "__main__":
    unittest.main()
