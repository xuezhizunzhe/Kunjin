import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.storage.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5


class SchemaV6Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_adds_peer_tables(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        repository.migrate()

        expected = {
            "fund_peer_groups",
            "fund_peer_group_syncs",
            "fund_peer_group_members",
            "fund_comparison_runs",
        }
        self.assertTrue(expected <= repository.table_names())

    def test_migration_upgrades_version_five_without_changing_existing_data(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "version-five.db")
        with repository.connect() as connection, connection:
            for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5):
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, f"2026-07-0{version}T00:00:00+00:00") for version in range(1, 6)],
            )
            connection.execute(
                """
                INSERT INTO fund_source_documents(
                    id, fund_code, document_kind, title, url, source_name,
                    source_tier, publisher, published_at, retrieved_at, checksum
                ) VALUES (1, '519755', 'basic_profile', '基金资料',
                          'https://fundf10.eastmoney.com/jbgk_519755.html',
                          'eastmoney_f10', 2, '东方财富', NULL,
                          '2026-07-11T00:00:00+00:00', ?)
                """,
                ("a" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_identities(
                    id, fund_code, record_key, fund_name, status, fund_type,
                    established_date, manager_name, source_document_id
                ) VALUES (1, '519755', ?, '交银多策略回报灵活配置混合A',
                          'active', '混合型-灵活', '2015-06-02', '交银施罗德基金', 1)
                """,
                ("b" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_manager_tenures(
                    id, fund_code, record_key, manager_name, start_date,
                    end_date, source_document_id
                ) VALUES (1, '519755', ?, '王艺伟', '2025-01-01', NULL, 1)
                """,
                ("c" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_holdings(
                    id, fund_code, record_key, report_period, published_at,
                    rank, security_code, security_name, asset_type, weight,
                    disclosure_scope, shares, market_value, source_document_id
                ) VALUES (1, '519755', ?, '2026-03-31',
                          '2026-04-21T00:00:00+08:00', 1, '600000',
                          '浦发银行', 'stock', '5.25', 'top10', NULL, NULL, 1)
                """,
                ("d" * 64,),
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
            connection.execute(
                """
                INSERT INTO fund_nav(
                    fund_code, nav_date, unit_nav, accumulated_nav,
                    daily_growth, source, retrieved_at
                ) VALUES ('519755', '2026-07-10', '1.6680', '1.6680',
                          '0.10', 'eastmoney', '2026-07-11T00:00:00+00:00')
                """
            )

        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            identity = connection.execute(
                "SELECT fund_name, fund_type FROM fund_identities WHERE id = 1"
            ).fetchone()
            manager = connection.execute(
                "SELECT manager_name, start_date FROM fund_manager_tenures WHERE id = 1"
            ).fetchone()
            holding = connection.execute(
                "SELECT security_code, weight FROM fund_holdings WHERE id = 1"
            ).fetchone()
            transaction = connection.execute(
                "SELECT fund_code, amount FROM transactions WHERE id = 1"
            ).fetchone()
            nav = connection.execute(
                "SELECT nav_date, unit_nav FROM fund_nav WHERE fund_code = '519755'"
            ).fetchone()

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 20)))
        self.assertEqual(
            dict(identity),
            {"fund_name": "交银多策略回报灵活配置混合A", "fund_type": "混合型-灵活"},
        )
        self.assertEqual(dict(manager), {"manager_name": "王艺伟", "start_date": "2025-01-01"})
        self.assertEqual(dict(holding), {"security_code": "600000", "weight": "5.25"})
        self.assertEqual(dict(transaction), {"fund_code": "519755", "amount": "20.00"})
        self.assertEqual(dict(nav), {"nav_date": "2026-07-10", "unit_nav": "1.6680"})


if __name__ == "__main__":
    unittest.main()
