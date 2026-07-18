from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from kunjin.funds.risk.audit import (
    canonical_fact_set_fingerprint,
    known_native_parser_provenance,
    legacy_parser_provenance,
    native_parser_provenance,
)
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
    SCHEMA_V11,
    SCHEMA_VERSION,
)

SCHEMAS_THROUGH_V11 = (
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

UTC = "2026-07-13T00:00:00+00:00"
FACT_FINGERPRINT = "b" * 64
MANIFEST = '{"evidence_fact_ids":[11],"manifest_version":1}'


class SchemaV12Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def _create_v11(self, name: str = "v11.db") -> Repository:
        repository = self.repository(name)
        with repository.connect() as connection, connection:
            for schema in SCHEMAS_THROUGH_V11:
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, UTC) for version in range(1, 12)],
            )
        return repository

    def _insert_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        artifact_id: int,
        fund_code: str = "000001",
        sha256: str = "a" * 64,
        parse_status: str = "parsed",
        parse_error_code: str | None = None,
        parser_version: str = "2",
        url: str | None = None,
    ) -> None:
        final_url = url or f"https://example.test/{artifact_id}.docx"
        connection.execute(
            """
            INSERT INTO fund_document_artifacts(
                id, fund_code, document_kind, url, landing_url, publisher, title,
                published_at, retrieved_at, content_type, byte_size, sha256,
                managed_path, parse_status, parser_version, parse_error_code
            ) VALUES (?, ?, 'fund_contract', ?, ?, 'public publisher', 'public contract',
                      ?, ?,
                      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                      1024, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                fund_code,
                final_url,
                final_url,
                UTC,
                UTC,
                sha256,
                f"/private/document-{artifact_id}.docx",
                parse_status,
                parser_version,
                parse_error_code,
            ),
        )

    def _insert_v11_history(self, repository: Repository) -> None:
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=7)
            self._insert_artifact(
                connection,
                artifact_id=9,
                sha256="c" * 64,
                parse_status="failed",
                parse_error_code="official_document_parse_failed",
            )
            connection.execute(
                """
                INSERT INTO fund_mandate_facts(
                    id, fund_code, source_document_id, fact_kind,
                    normalized_value_json, unit, page_number, section_name,
                    source_excerpt, effective_from, effective_to,
                    confidence_state, parser_version, fact_fingerprint
                ) VALUES (11, '000001', 7, 'stock_exposure_max_percent',
                          '{"type":"int","value":80}', 'percent', 3,
                          'investment scope', 'public bounded excerpt',
                          '2026-07-01', NULL, 'exact', '2', ?)
                """,
                (FACT_FINGERPRINT,),
            )
            connection.execute(
                """
                INSERT INTO fund_classification_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES ('1', '{"version":"1"}', ?, ?, ?)
                """,
                ("d" * 64, UTC, UTC),
            )
            connection.execute(
                """
                INSERT INTO fund_risk_classifications(
                    id, fund_code, policy_version, input_fingerprint,
                    input_manifest_json, product_family, risk_bucket,
                    portfolio_role, evidence_status, evidence_tags_json,
                    reason_codes_json, missing_evidence_json, conflicts_json,
                    evidence_document_ids_json, evidence_fact_ids_json,
                    freshness_json, classified_at, valid_until, created_at
                ) VALUES (13, '000001', '1', ?, ?, 'broad_index',
                          'diversified_equity', 'core_eligible', 'verified',
                          '[]', '[]', '[]', '[]', '[7]', '[11]', '[]',
                          ?, '2026-07-14T00:00:00+00:00', ?)
                """,
                ("e" * 64, MANIFEST, UTC, UTC),
            )

    def _insert_native_provenance(self, connection: sqlite3.Connection) -> int:
        provenance = native_parser_provenance()
        cursor = connection.execute(
            """
            INSERT INTO fund_document_parser_provenance(
                parser_version, converter_kind, canonical_json,
                provenance_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                provenance.parser_version,
                provenance.converter_kind,
                provenance.canonical_json,
                provenance.provenance_checksum,
                UTC,
            ),
        )
        return int(cursor.lastrowid)

    def test_fresh_database_has_v12_tables_and_fact_result_binding(self) -> None:
        repository = self.repository()
        repository.migrate()
        expected_tables = {
            "fund_document_refresh_runs",
            "fund_document_refresh_completions",
            "fund_document_candidate_runs",
            "fund_document_parser_provenance",
            "fund_document_parse_results",
            "fund_document_parse_runs",
        }
        with repository.connect() as connection:
            versions = [
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(fund_mandate_facts)").fetchall()
            }
            objects = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE 'fund_document_%'"
                ).fetchall()
            }
        self.assertEqual(SCHEMA_VERSION, 19)
        self.assertEqual(versions, list(range(1, 20)))
        self.assertTrue(expected_tables <= repository.table_names())
        self.assertIn("parse_result_id", columns)
        self.assertTrue(
            {
                "fund_document_refresh_runs_fund",
                "fund_document_candidate_runs_refresh",
                "fund_document_parse_results_source",
                "fund_document_parse_runs_source",
                "fund_document_refresh_run_no_update",
                "fund_document_candidate_run_no_delete",
                "fund_document_parser_provenance_no_update",
                "fund_document_parse_result_no_delete",
                "fund_document_parse_run_no_update",
                "fund_document_fact_result_required",
            }
            <= objects
        )

        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO fund_mandate_facts(
                        fund_code, source_document_id, fact_kind,
                        normalized_value_json, source_excerpt,
                        confidence_state, parser_version, fact_fingerprint
                    ) VALUES ('000001', 1, 'objective',
                              '{"type":"str","value":"public"}',
                              'public excerpt', 'exact', '2', ?)
                    """,
                    ("f" * 64,),
                )

    def test_v11_success_backfills_provenance_result_run_and_fact_binding(self) -> None:
        repository = self._create_v11()
        self._insert_v11_history(repository)
        repository.migrate()

        with repository.connect() as connection:
            provenance = connection.execute(
                "SELECT * FROM fund_document_parser_provenance"
            ).fetchone()
            result = connection.execute(
                "SELECT * FROM fund_document_parse_results WHERE source_document_id = 7"
            ).fetchone()
            run = connection.execute(
                "SELECT * FROM fund_document_parse_runs WHERE source_document_id = 7"
            ).fetchone()
            fact = connection.execute(
                "SELECT parse_result_id FROM fund_mandate_facts WHERE id = 11"
            ).fetchone()

        expected = known_native_parser_provenance("2")
        self.assertEqual(provenance["canonical_json"], expected.canonical_json)
        self.assertEqual(provenance["provenance_checksum"], expected.provenance_checksum)
        self.assertEqual(result["parser_input_sha256"], "a" * 64)
        self.assertEqual(
            result["fact_set_fingerprint"],
            canonical_fact_set_fingerprint((FACT_FINGERPRINT,)),
        )
        self.assertEqual(fact["parse_result_id"], result["id"])
        self.assertEqual(run["run_kind"], "legacy_backfill")
        self.assertEqual(run["outcome"], "success")
        self.assertEqual(run["parse_result_id"], result["id"])
        self.assertIsNone(run["public_error_code"])

    def test_v11_migration_rejects_unknown_native_parser_version(self) -> None:
        repository = self._create_v11()
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=7, parser_version="unknown")

        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "unsupported legacy V11 parser provenance"
        ):
            repository.migrate()

    def test_v11_migration_rejects_mixed_native_parser_versions(self) -> None:
        repository = self._create_v11()
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=7, parser_version="2")
            self._insert_artifact(connection, artifact_id=9, parser_version="3")

        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "unsupported legacy V11 parser provenance"
        ):
            repository.migrate()

    def test_v11_failure_backfills_public_code_without_inventing_stage_or_reason(self) -> None:
        repository = self._create_v11()
        self._insert_v11_history(repository)
        repository.migrate()

        with repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fund_document_parse_runs WHERE source_document_id = 9"
            ).fetchone()
        self.assertEqual(row["run_kind"], "legacy_backfill")
        self.assertEqual(row["outcome"], "failed")
        self.assertEqual(row["public_error_code"], "official_document_parse_failed")
        self.assertIsNone(row["failure_stage"])
        self.assertIsNone(row["failure_reason"])
        self.assertIsNone(row["parse_result_id"])

    def test_v12_migration_preserves_artifact_fact_and_classification_ids(self) -> None:
        repository = self._create_v11()
        self._insert_v11_history(repository)
        with repository.connect() as connection, connection:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = 29 WHERE name = 'fund_mandate_facts'"
            )
        repository.migrate()

        with repository.connect() as connection:
            artifact_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM fund_document_artifacts ORDER BY id"
                ).fetchall()
            ]
            fact = connection.execute("SELECT id FROM fund_mandate_facts").fetchone()
            classification = connection.execute(
                "SELECT id, input_manifest_json FROM fund_risk_classifications"
            ).fetchone()
            refresh_count = connection.execute(
                "SELECT COUNT(*) FROM fund_document_refresh_runs"
            ).fetchone()[0]
            fact_sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'fund_mandate_facts'"
            ).fetchone()[0]
            fact_table_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fund_mandate_facts'"
            ).fetchone()[0]
        self.assertEqual(artifact_ids, [7, 9])
        self.assertEqual(fact["id"], 11)
        self.assertEqual(classification["id"], 13)
        self.assertEqual(classification["input_manifest_json"], MANIFEST)
        self.assertEqual(refresh_count, 0)
        self.assertEqual(fact_sequence, 29)
        self.assertIn("UNIQUE(parse_result_id, fact_fingerprint)", fact_table_sql)
        self.assertNotIn(
            "UNIQUE(source_document_id, parser_version, fact_fingerprint)",
            fact_table_sql,
        )

    def test_v12_migration_rolls_back_on_malformed_legacy_fact_set(self) -> None:
        repository = self._create_v11()
        self._insert_v11_history(repository)
        with repository.connect() as connection, connection:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = 31 WHERE name = 'fund_mandate_facts'"
            )
            connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE fund_mandate_facts SET normalized_value_json = '{' WHERE id = 11"
            )
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            connection.execute(
                """
                CREATE TRIGGER fund_mandate_fact_no_update
                BEFORE UPDATE ON fund_mandate_facts
                BEGIN
                    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
                END
                """
            )

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

        with repository.connect() as connection:
            versions = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(fund_mandate_facts)").fetchall()
            }
            fact_sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'fund_mandate_facts'"
            ).fetchone()[0]
        self.assertNotIn(12, versions)
        self.assertNotIn("fund_document_parse_results", tables)
        self.assertNotIn("parse_result_id", columns)
        self.assertEqual(fact_sequence, 31)

    def test_refresh_result_run_and_provenance_rows_are_immutable(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=1)
            provenance_id = self._insert_native_provenance(connection)
            result_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_results(
                        source_document_id, provenance_id, parser_input_sha256,
                        fact_set_fingerprint, created_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        provenance_id,
                        "a" * 64,
                        canonical_fact_set_fingerprint(()),
                        UTC,
                    ),
                ).lastrowid
            )
            parse_run_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_runs(
                        source_document_id, provenance_id, run_kind, outcome,
                        parse_result_id, attempted_at
                    ) VALUES (1, ?, 'live', 'success', ?, ?)
                    """,
                    (provenance_id, result_id, UTC),
                ).lastrowid
            )
            refresh_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_refresh_runs(fund_code, started_at)
                    VALUES ('000001', ?)
                    """,
                    (UTC,),
                ).lastrowid
            )
            connection.execute(
                """
                INSERT INTO fund_document_candidate_runs(
                    refresh_run_id, candidate_fingerprint, fund_code,
                    document_kind, url, published_at, outcome,
                    source_document_id, parse_run_id, created_at
                ) VALUES (?, ?, '000001', 'fund_contract', ?, ?, 'success', 1, ?, ?)
                """,
                (refresh_id, "1" * 64, "https://example.test/1.docx", UTC, parse_run_id, UTC),
            )
            connection.execute(
                """
                INSERT INTO fund_document_refresh_completions(
                    refresh_run_id, outcome, completed_at
                ) VALUES (?, 'success', ?)
                """,
                (refresh_id, UTC),
            )

        tables = (
            "fund_document_refresh_runs",
            "fund_document_refresh_completions",
            "fund_document_candidate_runs",
            "fund_document_parser_provenance",
            "fund_document_parse_results",
            "fund_document_parse_runs",
        )
        for table in tables:
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(f"UPDATE {table} SET rowid = rowid")
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(f"DELETE FROM {table}")

    def test_partial_refresh_completion_uses_candidate_failures_not_top_level_error(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            refresh_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_refresh_runs(fund_code, started_at)
                    VALUES ('000001', ?)
                    """,
                    (UTC,),
                ).lastrowid
            )
            connection.execute(
                """
                INSERT INTO fund_document_refresh_completions(
                    refresh_run_id, outcome, completed_at
                ) VALUES (?, 'partial', ?)
                """,
                (refresh_id, UTC),
            )

        with repository.connect() as connection:
            completion = connection.execute(
                "SELECT * FROM fund_document_refresh_completions WHERE refresh_run_id = ?",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(completion["outcome"], "partial")
        self.assertIsNone(completion["public_error_code"])
        self.assertIsNone(completion["failure_stage"])
        self.assertIsNone(completion["failure_reason"])

    def test_parse_failures_can_repeat_but_success_result_is_unique(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            self._insert_artifact(
                connection,
                artifact_id=1,
                parse_status="failed",
                parse_error_code="official_document_parse_failed",
            )
            provenance_id = self._insert_native_provenance(connection)
            failure_values = (
                1,
                provenance_id,
                "live",
                "failed",
                "official_document_parse_failed",
                "parser",
                "parser_format_invalid",
                UTC,
            )
            connection.executemany(
                """
                INSERT INTO fund_document_parse_runs(
                    source_document_id, provenance_id, run_kind, outcome,
                    public_error_code, failure_stage, failure_reason, attempted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (failure_values, failure_values),
            )
            connection.execute(
                """
                INSERT INTO fund_document_parse_results(
                    source_document_id, provenance_id, parser_input_sha256,
                    fact_set_fingerprint, created_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (provenance_id, "a" * 64, canonical_fact_set_fingerprint(()), UTC),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_results(
                        source_document_id, provenance_id, parser_input_sha256,
                        fact_set_fingerprint, created_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (provenance_id, "a" * 64, "9" * 64, UTC),
                )

        with repository.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fund_document_parse_runs").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fund_document_parse_results").fetchone()[
                    0
                ],
                1,
            )

    def test_fact_cannot_bind_result_for_another_document(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=1)
            self._insert_artifact(
                connection,
                artifact_id=2,
                fund_code="000002",
                sha256="2" * 64,
            )
            provenance_id = self._insert_native_provenance(connection)
            result_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_results(
                        source_document_id, provenance_id, parser_input_sha256,
                        fact_set_fingerprint, created_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (provenance_id, "a" * 64, "3" * 64, UTC),
                ).lastrowid
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO fund_mandate_facts(
                        fund_code, source_document_id, fact_kind,
                        normalized_value_json, source_excerpt,
                        confidence_state, parser_version, fact_fingerprint,
                        parse_result_id
                    ) VALUES ('000002', 2, 'objective',
                              '{"type":"str","value":"public"}',
                              'public excerpt', 'exact', '2', ?, ?)
                    """,
                    ("4" * 64, result_id),
                )

    def test_same_fact_can_bind_distinct_legacy_provenance_results(self) -> None:
        repository = self.repository()
        repository.migrate()
        first = legacy_parser_provenance(
            image_id="sha256:" + "1" * 64,
            architecture="linux/arm64",
            libreoffice_version="24.2.0",
            package_manifest_checksum="2" * 64,
        )
        second = legacy_parser_provenance(
            image_id="sha256:" + "3" * 64,
            architecture="linux/arm64",
            libreoffice_version="24.2.0",
            package_manifest_checksum="4" * 64,
        )
        with repository.connect() as connection, connection:
            self._insert_artifact(connection, artifact_id=1)
            provenance_ids = []
            for provenance in (first, second):
                provenance_ids.append(
                    int(
                        connection.execute(
                            """
                            INSERT INTO fund_document_parser_provenance(
                                parser_version, converter_kind, canonical_json,
                                provenance_checksum, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                provenance.parser_version,
                                provenance.converter_kind,
                                provenance.canonical_json,
                                provenance.provenance_checksum,
                                UTC,
                            ),
                        ).lastrowid
                    )
                )
            result_ids = []
            for provenance_id, parser_input in zip(provenance_ids, ("5" * 64, "6" * 64)):
                result_ids.append(
                    int(
                        connection.execute(
                            """
                            INSERT INTO fund_document_parse_results(
                                source_document_id, provenance_id, parser_input_sha256,
                                fact_set_fingerprint, created_at
                            ) VALUES (1, ?, ?, ?, ?)
                            """,
                            (
                                provenance_id,
                                parser_input,
                                canonical_fact_set_fingerprint((FACT_FINGERPRINT,)),
                                UTC,
                            ),
                        ).lastrowid
                    )
                )
            for result_id in result_ids:
                connection.execute(
                    """
                    INSERT INTO fund_mandate_facts(
                        fund_code, source_document_id, fact_kind,
                        normalized_value_json, source_excerpt,
                        confidence_state, parser_version, fact_fingerprint,
                        parse_result_id
                    ) VALUES ('000001', 1, 'objective',
                              '{"type":"str","value":"public"}',
                              'public excerpt', 'exact', ?, ?, ?)
                    """,
                    (first.parser_version, FACT_FINGERPRINT, result_id),
                )

        with repository.connect() as connection:
            bindings = tuple(
                row["parse_result_id"]
                for row in connection.execute(
                    "SELECT parse_result_id FROM fund_mandate_facts ORDER BY id"
                ).fetchall()
            )
        self.assertEqual(bindings, tuple(result_ids))

    def test_candidate_binding_accepts_redirect_and_rejects_publication_mismatch(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            self._insert_artifact(
                connection,
                artifact_id=1,
                url="https://example.test/final/1.docx",
            )
            connection.execute("DROP TRIGGER fund_document_artifact_no_update")
            connection.execute(
                "UPDATE fund_document_artifacts SET landing_url = ? WHERE id = 1",
                ("https://example.test/landing/1",),
            )
            provenance_id = self._insert_native_provenance(connection)
            result_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_results(
                        source_document_id, provenance_id, parser_input_sha256,
                        fact_set_fingerprint, created_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (provenance_id, "a" * 64, canonical_fact_set_fingerprint(()), UTC),
                ).lastrowid
            )
            parse_run_id = int(
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_runs(
                        source_document_id, provenance_id, run_kind, outcome,
                        parse_result_id, attempted_at
                    ) VALUES (1, ?, 'live', 'success', ?, ?)
                    """,
                    (provenance_id, result_id, UTC),
                ).lastrowid
            )
            refresh_id = int(
                connection.execute(
                    "INSERT INTO fund_document_refresh_runs(fund_code, started_at) "
                    "VALUES ('000001', ?)",
                    (UTC,),
                ).lastrowid
            )
            connection.execute(
                """
                INSERT INTO fund_document_candidate_runs(
                    refresh_run_id, candidate_fingerprint, fund_code,
                    document_kind, url, published_at, outcome,
                    source_document_id, parse_run_id, created_at
                ) VALUES (?, ?, '000001', 'fund_contract', ?, ?, 'success', 1, ?, ?)
                """,
                (
                    refresh_id,
                    "1" * 64,
                    "https://example.test/landing/1",
                    UTC,
                    parse_run_id,
                    UTC,
                ),
            )

            second_refresh = int(
                connection.execute(
                    "INSERT INTO fund_document_refresh_runs(fund_code, started_at) "
                    "VALUES ('000001', ?)",
                    (UTC,),
                ).lastrowid
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO fund_document_candidate_runs(
                        refresh_run_id, candidate_fingerprint, fund_code,
                        document_kind, url, published_at, outcome,
                        source_document_id, parse_run_id, created_at
                    ) VALUES (?, ?, '000001', 'fund_contract', ?, ?, 'success', 1, ?, ?)
                    """,
                    (
                        second_refresh,
                        "2" * 64,
                        "https://example.test/landing/1",
                        "2026-07-14T00:00:00+00:00",
                        parse_run_id,
                        UTC,
                    ),
                )

    def test_schema_tampering_is_rejected_on_reopen(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_parse_run_no_update")

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

    def test_trigger_on_unrelated_table_cannot_write_d1_without_detection(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER hidden_d1_writer
                AFTER INSERT ON sync_runs
                BEGIN
                    INSERT INTO fund_document_refresh_runs(fund_code, started_at)
                    VALUES ('000001', '2026-07-13T00:00:00+00:00');
                END
                """
            )

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()


if __name__ == "__main__":
    unittest.main()
