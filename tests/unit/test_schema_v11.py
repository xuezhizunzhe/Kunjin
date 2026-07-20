from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.storage.schema import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
    SCHEMA_VERSION,
)

SCHEMAS_THROUGH_V10 = (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
)


class SchemaV11Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "kunjin.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_fresh_migration_requires_landing_url_and_records_version_11(self) -> None:
        repository = Repository(self.database)
        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            columns = connection.execute("PRAGMA table_info(fund_document_artifacts)").fetchall()
            triggers = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }

        self.assertEqual(SCHEMA_VERSION, 22)
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 23)))
        self.assertIn("landing_url", {row["name"] for row in columns})
        self.assertIn("fund_document_artifact_landing_url_required", triggers)

    def test_v10_upgrade_backfills_landing_url_without_changing_final_url(self) -> None:
        with sqlite3.connect(self.database) as connection:
            for schema in SCHEMAS_THROUGH_V10:
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, f"2026-07-{version:02d}T00:00:00+00:00") for version in range(1, 11)],
            )
            connection.execute(
                """
                INSERT INTO fund_document_artifacts(
                    fund_code, document_kind, url, publisher, title,
                    published_at, retrieved_at, content_type, byte_size, sha256,
                    managed_path, parse_status, parser_version, parse_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "000001",
                    "fund_contract",
                    "https://www.fund001.com/final.docx",
                    "交银施罗德基金管理有限公司",
                    "public contract",
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-13T00:00:00+00:00",
                    "application/msword",
                    1,
                    "a" * 64,
                    "/private/public.docx",
                    "parsed",
                    "2",
                    None,
                ),
            )

        repository = Repository(self.database)
        repository.migrate()

        with repository.connect() as connection:
            row = connection.execute(
                "SELECT url, landing_url FROM fund_document_artifacts WHERE id = 1"
            ).fetchone()
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual(row["url"], "https://www.fund001.com/final.docx")
        self.assertEqual(row["landing_url"], row["url"])
        self.assertEqual([int(item["version"]) for item in versions], list(range(1, 23)))

    def test_new_artifact_cannot_omit_or_mutate_landing_url(self) -> None:
        repository = Repository(self.database)
        repository.migrate()
        with repository.connect() as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "landing URL"):
                connection.execute(
                    """
                    INSERT INTO fund_document_artifacts(
                        fund_code, document_kind, url, publisher, title,
                        retrieved_at, content_type, byte_size, sha256, managed_path,
                        parse_status, parser_version
                    ) VALUES ('000001', 'fund_contract', 'https://www.fund001.com/final',
                              '交银施罗德基金管理有限公司', 'public contract',
                              '2026-07-13T00:00:00+00:00', 'application/pdf', 1,
                              ?, '/private/public.pdf', 'parsed', '2')
                    """,
                    ("b" * 64,),
                )


if __name__ == "__main__":
    unittest.main()
