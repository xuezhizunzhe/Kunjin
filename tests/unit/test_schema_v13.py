from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository, _migrate_v12
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
    SCHEMA_V11,
    SCHEMA_VERSION,
)

UTC = "2026-07-14T00:00:00+00:00"
MANIFEST = '{"evidence_fact_ids":[11],"manifest_version":1}'

SCHEMAS = (
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
    SCHEMA_V11,
)


class SchemaV13Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str) -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def _create_at_version(self, version: int) -> Repository:
        repository = self.repository(f"v{version}.db")
        with repository.connect() as connection:
            for index, schema in enumerate(SCHEMAS, start=1):
                if index > version:
                    break
                connection.executescript(schema)
            if version >= 12:
                _migrate_v12(connection)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(item, UTC) for item in range(1, version + 1)],
            )
            if version >= 10:
                connection.execute(
                    "INSERT INTO fund_classification_policy_versions("
                    "version, canonical_policy_json, policy_checksum, effective_at, created_at"
                    ") VALUES ('1', '{\"version\":\"1\"}', ?, ?, ?)",
                    ("d" * 64, UTC, UTC),
                )
                connection.execute(
                    "INSERT INTO fund_risk_classifications("
                    "id, fund_code, policy_version, input_fingerprint, input_manifest_json, "
                    "product_family, risk_bucket, portfolio_role, evidence_status, "
                    "evidence_tags_json, reason_codes_json, missing_evidence_json, "
                    "conflicts_json, evidence_document_ids_json, evidence_fact_ids_json, "
                    "freshness_json, classified_at, valid_until, created_at"
                    ") VALUES (17, '000001', '1', ?, ?, 'broad_index', "
                    "'diversified_equity', 'core_eligible', 'verified', '[]', '[]', '[]', "
                    "'[]', '[]', '[11]', '[]', ?, '2026-07-15T00:00:00+00:00', ?)",
                    ("e" * 64, MANIFEST, UTC, UTC),
                )
            connection.commit()
        return repository

    def test_v9_through_v12_migrate_additively_and_preserve_history(self) -> None:
        for version in (9, 10, 11, 12):
            with self.subTest(version=version):
                repository = self._create_at_version(version)
                before = None
                if version >= 10:
                    with repository.connect() as connection:
                        row = connection.execute(
                            "SELECT id, input_manifest_json FROM fund_risk_classifications"
                        ).fetchone()
                        before = (row["id"], bytes(row["input_manifest_json"], "utf-8"))

                repository.migrate()

                with repository.connect() as connection:
                    versions = tuple(
                        row["version"]
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    )
                    columns = tuple(
                        row["name"]
                        for row in connection.execute(
                            "PRAGMA table_info(fund_document_selection_manifests)"
                        )
                    )
                    if version >= 10:
                        row = connection.execute(
                            "SELECT id, input_manifest_json FROM fund_risk_classifications"
                        ).fetchone()
                        after = (row["id"], bytes(row["input_manifest_json"], "utf-8"))

                self.assertEqual(SCHEMA_VERSION, 13)
                self.assertEqual(versions, tuple(range(1, 14)))
                self.assertEqual(
                    columns,
                    (
                        "refresh_run_id",
                        "fund_code",
                        "manifest_version",
                        "selection_policy_checksum",
                        "canonical_json",
                        "selection_checksum",
                        "created_at",
                    ),
                )
                if version >= 10:
                    self.assertEqual(after, before)

    def test_selection_rows_are_bound_immutable_and_schema_constrained(self) -> None:
        repository = self.repository("constraints.db")
        repository.migrate()
        canonical = '{"manifest_version":1}'
        values = (1, "000001", 1, "a" * 64, canonical, "b" * 64, UTC)
        with repository.connect() as connection, connection:
            connection.executemany(
                "INSERT INTO fund_document_refresh_runs(id, fund_code, started_at) "
                "VALUES (?, '000001', ?)",
                [(item, UTC) for item in range(1, 8)],
            )
            connection.execute(
                "INSERT INTO fund_document_selection_manifests VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO fund_document_selection_manifests VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE fund_document_selection_manifests SET canonical_json = '{}'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM fund_document_selection_manifests")

        invalid = (
            (2, "000002", 1, "a" * 64, canonical, "c" * 64, UTC),
            (3, "000001", 2, "a" * 64, canonical, "d" * 64, UTC),
            (4, "000001", 1, "A" * 64, canonical, "e" * 64, UTC),
            (5, "000001", 1, "a" * 64, canonical, "f" * 64, "not-a-time"),
        )
        for values in invalid:
            with self.subTest(values=values), repository.connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO fund_document_selection_manifests "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )

        blob_digests = (
            (6, "000001", 1, sqlite3.Binary(b"a" * 64), canonical, "b" * 64, UTC),
            (7, "000001", 1, "a" * 64, canonical, sqlite3.Binary(b"b" * 64), UTC),
        )
        for values in blob_digests:
            with self.subTest(blob_column=values), repository.connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "INSERT INTO fund_document_selection_manifests "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )


if __name__ == "__main__":
    unittest.main()
