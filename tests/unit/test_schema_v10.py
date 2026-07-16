import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from kunjin.funds.risk.audit import (
    canonical_fact_set_fingerprint,
    known_native_parser_provenance,
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
    SCHEMA_VERSION,
)

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
)


DOCUMENT_VALUES = (
    "000001",
    "fund_contract",
    "https://example.test/fund-contract.pdf",
    "https://example.test/fund-contract-landing.html",
    "public publisher",
    "fund contract",
    "2026-07-01T00:00:00+00:00",
    "2026-07-13T00:00:00+00:00",
    "application/pdf",
    1024,
    "a" * 64,
    "/private/fund-contract.pdf",
    "parsed",
    "2",
    None,
)

FACT_VALUES = (
    "000001",
    1,
    "stock_exposure_max_percent",
    "80",
    "percent",
    3,
    "investment scope",
    "The stock exposure shall not exceed eighty percent.",
    "2026-07-01",
    None,
    "exact",
    "2",
    "b" * 64,
    1,
)

POLICY_VALUES = (
    "1",
    '{"version":"1"}',
    "c" * 64,
    "2026-07-13T00:00:00+00:00",
    "2026-07-13T00:00:00+00:00",
)

CLASSIFICATION_VALUES = (
    "000001",
    "1",
    "d" * 64,
    '{"manifest_version":1}',
    "broad_index",
    "diversified_equity",
    "core_eligible",
    "verified",
    '["hong_kong_equity"]',
    '["broad_index_verified"]',
    "[]",
    "[]",
    "[1]",
    "[1]",
    '[{"section":"mandate"}]',
    "2026-07-13T01:00:00+00:00",
    "2026-07-14T01:00:00+00:00",
    "2026-07-13T01:00:00+00:00",
)


class SchemaV10Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def insert_document(self, repository: Repository, values=DOCUMENT_VALUES) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_document_artifacts(
                    fund_code, document_kind, url, landing_url, publisher, title,
                    published_at, retrieved_at, content_type, byte_size, sha256,
                    managed_path, parse_status, parser_version, parse_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def insert_fact(self, repository: Repository, values=FACT_VALUES) -> None:
        self.ensure_native_result(repository)
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_mandate_facts(
                    fund_code, source_document_id, fact_kind,
                    normalized_value_json, unit, page_number, section_name,
                    source_excerpt, effective_from, effective_to,
                    confidence_state, parser_version, fact_fingerprint,
                    parse_result_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def ensure_native_result(self, repository: Repository) -> None:
        provenance = known_native_parser_provenance("2")
        with repository.connect() as connection, connection:
            row = connection.execute(
                "SELECT id FROM fund_document_parser_provenance WHERE provenance_checksum = ?",
                (provenance.provenance_checksum,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO fund_document_parser_provenance(
                        parser_version, converter_kind, canonical_json,
                        provenance_checksum, created_at
                    ) VALUES (?, ?, ?, ?, '2026-07-13T00:00:00+00:00')
                    """,
                    (
                        provenance.parser_version,
                        provenance.converter_kind,
                        provenance.canonical_json,
                        provenance.provenance_checksum,
                    ),
                )
                row = connection.execute(
                    "SELECT id FROM fund_document_parser_provenance WHERE provenance_checksum = ?",
                    (provenance.provenance_checksum,),
                ).fetchone()
            result = connection.execute(
                """
                SELECT id FROM fund_document_parse_results
                WHERE source_document_id = 1 AND provenance_id = ?
                """,
                (row["id"],),
            ).fetchone()
            if result is None:
                connection.execute(
                    """
                    INSERT INTO fund_document_parse_results(
                        source_document_id, provenance_id, parser_input_sha256,
                        fact_set_fingerprint, created_at
                    ) VALUES (1, ?, ?, ?, '2026-07-13T00:00:00+00:00')
                    """,
                    (
                        row["id"],
                        "a" * 64,
                        canonical_fact_set_fingerprint(("b" * 64,)),
                    ),
                )

    def insert_policy(self, repository: Repository, values=POLICY_VALUES) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_classification_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )

    def insert_classification(self, repository: Repository, values=CLASSIFICATION_VALUES) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_risk_classifications(
                    fund_code, policy_version, input_fingerprint, input_manifest_json,
                    product_family, risk_bucket, portfolio_role, evidence_status,
                    evidence_tags_json, reason_codes_json, missing_evidence_json,
                    conflicts_json, evidence_document_ids_json,
                    evidence_fact_ids_json, freshness_json, classified_at,
                    valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def prepared_repository(self, name: str = "prepared.db") -> Repository:
        repository = self.repository(name)
        repository.migrate()
        self.insert_document(repository)
        self.insert_fact(repository)
        self.insert_policy(repository)
        return repository

    def repository_at_version(self, version: int, name: str) -> Repository:
        repository = self.repository(name)
        with repository.connect() as connection, connection:
            for schema in SCHEMAS[:version]:
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(item, f"2026-07-{item:02d}T00:00:00+00:00") for item in range(1, version + 1)],
            )
            connection.execute(
                """
                INSERT INTO sync_runs(
                    id, source, trigger, started_at, finished_at, status
                ) VALUES (
                    1, 'preserved-source', 'preserved-trigger',
                    '2026-07-01T00:00:00+00:00',
                    '2026-07-01T00:01:00+00:00', 'success'
                )
                """
            )
        return repository

    def database_snapshot(self, repository: Repository) -> tuple:
        with repository.connect() as connection:
            schema = connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
            versions = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
            sync_runs = connection.execute("SELECT * FROM sync_runs ORDER BY id").fetchall()
        return tuple(tuple(tuple(row) for row in rows) for rows in (schema, versions, sync_runs))

    def d1_schema_snapshot(self, repository: Repository) -> tuple:
        with repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name LIKE 'fund_document_artifact%'
                   OR name LIKE 'fund_mandate_fact%'
                   OR name LIKE 'fund_classification_policy%'
                   OR name LIKE 'fund_risk_classification%'
                ORDER BY type, name
                """
            ).fetchall()
        return tuple(tuple(row) for row in rows)

    def test_schema_version_is_ten(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 15)

    def test_fresh_migration_adds_exact_d1_tables_and_versions(self) -> None:
        repository = self.repository()
        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 16)))
        self.assertEqual(
            repository.table_names()
            & {
                "fund_document_artifacts",
                "fund_mandate_facts",
                "fund_classification_policy_versions",
                "fund_risk_classifications",
            },
            {
                "fund_document_artifacts",
                "fund_mandate_facts",
                "fund_classification_policy_versions",
                "fund_risk_classifications",
            },
        )

    def test_fresh_migration_adds_exact_columns_indexes_and_triggers(self) -> None:
        repository = self.repository()
        repository.migrate()

        expected_columns = {
            "fund_document_artifacts": [
                "id",
                "fund_code",
                "document_kind",
                "url",
                "publisher",
                "title",
                "published_at",
                "retrieved_at",
                "content_type",
                "byte_size",
                "sha256",
                "managed_path",
                "parse_status",
                "parser_version",
                "parse_error_code",
                "landing_url",
            ],
            "fund_mandate_facts": [
                "id",
                "fund_code",
                "source_document_id",
                "fact_kind",
                "normalized_value_json",
                "unit",
                "page_number",
                "section_name",
                "source_excerpt",
                "effective_from",
                "effective_to",
                "confidence_state",
                "parser_version",
                "fact_fingerprint",
                "parse_result_id",
            ],
            "fund_classification_policy_versions": [
                "version",
                "canonical_policy_json",
                "policy_checksum",
                "effective_at",
                "created_at",
            ],
            "fund_risk_classifications": [
                "id",
                "fund_code",
                "policy_version",
                "input_fingerprint",
                "input_manifest_json",
                "product_family",
                "risk_bucket",
                "portfolio_role",
                "evidence_status",
                "evidence_tags_json",
                "reason_codes_json",
                "missing_evidence_json",
                "conflicts_json",
                "evidence_document_ids_json",
                "evidence_fact_ids_json",
                "freshness_json",
                "classified_at",
                "valid_until",
                "created_at",
            ],
        }
        expected_objects = {
            *expected_columns,
            "fund_document_artifacts_lookup",
            "fund_mandate_facts_lookup",
            "fund_risk_classifications_binding",
            "fund_risk_classifications_history",
            "fund_document_artifact_no_replace",
            "fund_document_artifact_landing_url_required",
            "fund_document_artifact_no_update",
            "fund_document_artifact_no_delete",
            "fund_mandate_fact_no_replace",
            "fund_mandate_fact_no_update",
            "fund_mandate_fact_no_delete",
            "fund_classification_policy_no_replace",
            "fund_classification_policy_no_update",
            "fund_classification_policy_no_delete",
            "fund_risk_classification_no_replace",
            "fund_risk_classification_no_update",
            "fund_risk_classification_no_delete",
        }

        with repository.connect() as connection:
            actual_columns = {
                table: [
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                ]
                for table in expected_columns
            }
            actual_objects = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE name LIKE 'fund_document_artifact%'
                       OR name LIKE 'fund_mandate_fact%'
                       OR name LIKE 'fund_classification_policy%'
                       OR name LIKE 'fund_risk_classification%'
                    """
                ).fetchall()
            }

        self.assertEqual(actual_columns, expected_columns)
        self.assertEqual(actual_objects, expected_objects)

    def test_valid_rows_satisfy_all_v10_table_checks(self) -> None:
        repository = self.prepared_repository()
        self.insert_classification(repository)

        with repository.connect() as connection:
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "fund_document_artifacts",
                    "fund_mandate_facts",
                    "fund_classification_policy_versions",
                    "fund_risk_classifications",
                )
            }
        self.assertEqual(set(counts.values()), {1})

    def test_document_rejects_invalid_text_enums_digest_integer_and_utc(self) -> None:
        cases = {
            "blob-code": (0, sqlite3.Binary(b"000001")),
            "bad-code": (0, "abc001"),
            "nul-code": (0, "00000\x00"),
            "nul-url": (2, "https://example.test/\x00contract"),
            "unknown-kind": (1, "unknown"),
            "legacy-f10-kind": (1, "basic_profile"),
            "hour-24-published-at": (6, "2026-07-01T24:00:00+00:00"),
            "naive-retrieved-at": (7, "2026-07-13T00:00:00"),
            "hour-24-retrieved-at": (7, "2026-07-13T24:00:00+00:00"),
            "nonpositive-size": (9, 0),
            "real-size": (9, 1.5),
            "uppercase-digest": (10, ("a" * 63 + "B")),
            "short-digest": (10, "a" * 63),
            "unknown-status": (12, "complete"),
            "nul-parser-version": (13, "1\x00"),
        }
        for case_number, (name, (index, invalid)) in enumerate(cases.items()):
            with self.subTest(name=name):
                repository = self.repository(f"invalid-document-{case_number}.db")
                repository.migrate()
                values = list(DOCUMENT_VALUES)
                values[index] = invalid
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_document(repository, values)

    def test_fact_rejects_invalid_json_dates_confidence_digest_and_positive_fields(self) -> None:
        cases = {
            "blob-kind": (2, sqlite3.Binary(b"stock_exposure")),
            "nul-code": (0, "00000\x00"),
            "invalid-json": (3, "{"),
            "zero-page": (5, 0),
            "nul-section": (6, "scope\x00section"),
            "empty-excerpt": (7, ""),
            "invalid-from": (8, "2026-7-1"),
            "end-before-start": (9, "2026-06-30"),
            "unknown-confidence": (10, "certain"),
            "uppercase-digest": (12, "B" * 64),
        }
        for case_number, (name, (index, invalid)) in enumerate(cases.items()):
            with self.subTest(name=name):
                repository = self.repository(f"invalid-fact-{case_number}.db")
                repository.migrate()
                self.insert_document(repository)
                values = list(FACT_VALUES)
                values[index] = invalid
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_fact(repository, values)

    def test_policy_rejects_invalid_text_object_digest_and_utc(self) -> None:
        cases = {
            "blob-version": (0, sqlite3.Binary(b"1")),
            "nul-version": (0, "1\x00"),
            "array-policy": (1, "[]"),
            "invalid-policy": (1, "{"),
            "uppercase-digest": (2, "C" * 64),
            "naive-effective-at": (3, "2026-07-13T00:00:00"),
            "hour-24-effective-at": (3, "2026-07-13T24:00:00+00:00"),
            "z-created-at": (4, "2026-07-13T00:00:00Z"),
            "hour-24-created-at": (4, "2026-07-13T24:00:00+00:00"),
        }
        for case_number, (name, (index, invalid)) in enumerate(cases.items()):
            with self.subTest(name=name):
                repository = self.repository(f"invalid-policy-{case_number}.db")
                repository.migrate()
                values = list(POLICY_VALUES)
                values[index] = invalid
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_policy(repository, values)

    def test_classification_rejects_invalid_enums_json_arrays_digest_and_utc(self) -> None:
        cases = {
            "blob-code": (0, sqlite3.Binary(b"000001")),
            "nul-code": (0, "00000\x00"),
            "uppercase-digest": (2, "D" * 64),
            "invalid-manifest-json": (3, "{"),
            "array-manifest-json": (3, "[]"),
            "blob-manifest-json": (3, sqlite3.Binary(b'{"manifest_version":1}')),
            "unknown-family": (4, "balanced"),
            "unknown-risk": (5, "medium"),
            "unknown-role": (6, "recommended"),
            "unknown-evidence": (7, "complete"),
            "invalid-tags-json": (8, "["),
            "object-tags-json": (8, "{}"),
            "object-reasons-json": (9, "{}"),
            "object-missing-json": (10, "{}"),
            "object-conflicts-json": (11, "{}"),
            "object-document-ids-json": (12, "{}"),
            "object-fact-ids-json": (13, "{}"),
            "object-freshness-json": (14, "{}"),
            "naive-classified-at": (15, "2026-07-13T01:00:00"),
            "hour-24-classified-at": (15, "2026-07-13T24:00:00+00:00"),
            "equal-valid-until": (16, "2026-07-13T01:00:00+00:00"),
            "hour-24-valid-until": (16, "2026-07-14T24:00:00+00:00"),
            "z-created-at": (17, "2026-07-13T01:00:00Z"),
            "hour-24-created-at": (17, "2026-07-13T24:00:00+00:00"),
        }
        for case_number, (name, (index, invalid)) in enumerate(cases.items()):
            with self.subTest(name=name):
                repository = self.prepared_repository(f"invalid-result-{case_number}.db")
                values = list(CLASSIFICATION_VALUES)
                values[index] = invalid
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_classification(repository, values)

    def test_migration_is_atomic_from_every_supported_prior_version(self) -> None:
        for version in range(1, 10):
            with self.subTest(version=version):
                repository = self.repository_at_version(version, f"upgrade-from-v{version}.db")
                repository.migrate()

                with repository.connect() as connection:
                    versions = connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                    sync_run = connection.execute(
                        "SELECT source, trigger FROM sync_runs WHERE id = 1"
                    ).fetchone()
                self.assertEqual([int(row["version"]) for row in versions], list(range(1, 16)))
                self.assertEqual(
                    dict(sync_run),
                    {"source": "preserved-source", "trigger": "preserved-trigger"},
                )

    def test_failed_v10_migration_rolls_back_objects_marker_and_prior_data(self) -> None:
        broken_v10 = """
        CREATE TABLE fund_document_artifacts(id INTEGER PRIMARY KEY);
        CREATE TABLE fund_mandate_facts(id INTEGER PRIMARY KEY);
        CREATE INDEX partial_fund_mandate_fact_index ON fund_mandate_facts(id);
        CREATE TABLE incomplete_v10 (
        """
        for version in range(1, 10):
            with self.subTest(version=version):
                repository = self.repository_at_version(version, f"failed-v10-from-v{version}.db")
                before = self.database_snapshot(repository)

                with patch("kunjin.storage.repository.SCHEMA_V10", broken_v10):
                    with self.assertRaises(sqlite3.OperationalError):
                        repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)

    def test_v10_name_collisions_fail_without_changing_existing_database(self) -> None:
        hostile_objects = (
            "CREATE TABLE fund_document_artifacts(hostile TEXT);",
            "CREATE TABLE fund_mandate_facts(hostile TEXT);",
            "CREATE TABLE fund_classification_policy_versions(hostile TEXT);",
            "CREATE TABLE fund_risk_classifications(hostile TEXT);",
            """
            CREATE TABLE hostile_index_target(id INTEGER);
            CREATE INDEX fund_document_artifacts_lookup ON hostile_index_target(id);
            """,
            """
            CREATE TABLE hostile_trigger_target(id INTEGER);
            CREATE TRIGGER fund_mandate_fact_no_update
            BEFORE UPDATE ON hostile_trigger_target
            BEGIN
                SELECT RAISE(ABORT, 'hostile trigger body');
            END;
            """,
        )
        for case_number, hostile_sql in enumerate(hostile_objects):
            with self.subTest(case_number=case_number):
                repository = self.repository_at_version(9, f"collision-{case_number}.db")
                with repository.connect() as connection, connection:
                    connection.executescript(hostile_sql)
                before = self.database_snapshot(repository)

                with self.assertRaisesRegex(sqlite3.OperationalError, "already exists"):
                    repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)

    def test_false_v10_marker_fails_closed_without_changes(self) -> None:
        repository = self.repository_at_version(9, "false-v10-marker.db")
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (10, '2026-07-10T00:00:00+00:00')
                """
            )
        before = self.database_snapshot(repository)

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

        self.assertEqual(self.database_snapshot(repository), before)

    def test_missing_altered_renamed_or_extra_v10_objects_fail_closed(self) -> None:
        mutations = (
            "DROP TRIGGER fund_risk_classification_no_delete",
            """
            DROP INDEX fund_risk_classifications_binding;
            CREATE INDEX fund_risk_classifications_binding
            ON fund_risk_classifications(policy_version);
            """,
            "ALTER TABLE fund_mandate_facts RENAME TO fund_mandate_facts_renamed",
            "CREATE INDEX fund_document_artifacts_extra ON fund_document_artifacts(title)",
            "CREATE TABLE fund_risk_classifications_shadow(id INTEGER PRIMARY KEY)",
        )
        for case_number, mutation in enumerate(mutations):
            with self.subTest(case_number=case_number):
                repository = self.repository(f"altered-v10-{case_number}.db")
                repository.migrate()
                with repository.connect() as connection, connection:
                    connection.executescript(mutation)
                before = self.database_snapshot(repository)

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)

    def test_unrelated_legacy_fund_object_is_not_claimed_by_v10(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute("CREATE TABLE fund_external_notes(id INTEGER PRIMARY KEY)")

        repository.migrate()

        self.assertIn("fund_external_notes", repository.table_names())

    def test_upgrade_and_fresh_migration_produce_exact_same_v10_sql(self) -> None:
        upgraded = self.repository_at_version(9, "upgraded.db")
        upgraded.migrate()
        fresh = self.repository("fresh.db")
        fresh.migrate()

        self.assertEqual(self.d1_schema_snapshot(upgraded), self.d1_schema_snapshot(fresh))

    def test_two_connections_can_contend_for_first_migration(self) -> None:
        database = Path(self.temporary_directory.name) / "contended.db"
        barrier = threading.Barrier(2)

        def migrate() -> None:
            barrier.wait(timeout=5)
            Repository(database).migrate()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(migrate) for _ in range(2)]
            for future in futures:
                future.result(timeout=10)

        repository = Repository(database)
        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 16)))
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(repository.database.stat().st_mode & 0o777, 0o600)

    def test_all_v10_rows_reject_replace_update_and_delete(self) -> None:
        repository = self.prepared_repository()
        self.insert_classification(repository)

        mutations = (
            (
                "UPDATE fund_document_artifacts SET title = 'changed' WHERE id = 1",
                "fund document artifacts are immutable",
            ),
            (
                "DELETE FROM fund_document_artifacts WHERE id = 1",
                "fund document artifacts are immutable",
            ),
            (
                "UPDATE fund_mandate_facts SET unit = NULL WHERE id = 1",
                "fund mandate facts are immutable",
            ),
            (
                "DELETE FROM fund_mandate_facts WHERE id = 1",
                "fund mandate facts are immutable",
            ),
            (
                "UPDATE fund_classification_policy_versions SET created_at = created_at",
                "fund classification policies are immutable",
            ),
            (
                "DELETE FROM fund_classification_policy_versions",
                "fund classification policies are immutable",
            ),
            (
                "UPDATE fund_risk_classifications SET created_at = created_at",
                "fund risk classifications are immutable",
            ),
            (
                "DELETE FROM fund_risk_classifications",
                "fund risk classifications are immutable",
            ),
        )
        for statement, message in mutations:
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                    with repository.connect() as connection, connection:
                        connection.execute(statement)

        replacements = (
            (
                """
                INSERT OR REPLACE INTO fund_document_artifacts(
                    fund_code, document_kind, url, landing_url, publisher, title,
                    published_at, retrieved_at, content_type, byte_size, sha256,
                    managed_path, parse_status, parser_version, parse_error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                DOCUMENT_VALUES,
                "fund document artifacts are immutable",
            ),
            (
                """
                INSERT OR REPLACE INTO fund_mandate_facts(
                    fund_code, source_document_id, fact_kind,
                    normalized_value_json, unit, page_number, section_name,
                    source_excerpt, effective_from, effective_to,
                    confidence_state, parser_version, fact_fingerprint,
                    parse_result_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                FACT_VALUES,
                "fund mandate facts are immutable",
            ),
            (
                """
                INSERT OR REPLACE INTO fund_classification_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                POLICY_VALUES,
                "fund classification policies are immutable",
            ),
            (
                """
                INSERT OR REPLACE INTO fund_risk_classifications(
                    fund_code, policy_version, input_fingerprint, input_manifest_json,
                    product_family, risk_bucket, portfolio_role, evidence_status,
                    evidence_tags_json, reason_codes_json, missing_evidence_json,
                    conflicts_json, evidence_document_ids_json,
                    evidence_fact_ids_json, freshness_json, classified_at,
                    valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                CLASSIFICATION_VALUES,
                "fund risk classifications are immutable",
            ),
        )
        for statement, values, message in replacements:
            with self.subTest(message=message):
                with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                    with repository.connect() as connection, connection:
                        connection.execute(statement, values)

    def test_artifact_identity_does_not_change_with_parser_version(self) -> None:
        repository = self.repository()
        repository.migrate()
        self.insert_document(repository)
        reparsed_values = list(DOCUMENT_VALUES)
        reparsed_values[13] = "3"

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "fund document artifacts are immutable"
        ):
            self.insert_document(repository, reparsed_values)

    def test_v10_integer_ids_are_strictly_positive(self) -> None:
        repository = self.prepared_repository()
        statements = (
            (
                """
                INSERT INTO fund_document_artifacts(
                    id, fund_code, document_kind, url, landing_url, publisher, title,
                    retrieved_at, content_type, byte_size, sha256, managed_path,
                    parse_status, parser_version
                ) VALUES (0, '000002', 'fund_contract', 'https://example.test/2',
                          'https://example.test/2-landing',
                          'publisher', 'title', '2026-07-13T00:00:00+00:00',
                          'application/pdf', 1, ?, '/private/2', 'parsed', '1')
                """,
                ("2" * 64,),
            ),
            (
                """
                INSERT INTO fund_mandate_facts(
                    id, fund_code, source_document_id, fact_kind,
                    normalized_value_json, source_excerpt, confidence_state,
                    parser_version, fact_fingerprint, parse_result_id
                ) VALUES (0, '000001', 1, 'objective', '{}', 'excerpt',
                          'exact', '2', ?, 1)
                """,
                ("3" * 64,),
            ),
            (
                """
                INSERT INTO fund_risk_classifications(
                    id, fund_code, policy_version, input_fingerprint, input_manifest_json,
                    product_family, risk_bucket, portfolio_role, evidence_status,
                    evidence_tags_json, reason_codes_json, missing_evidence_json,
                    conflicts_json, evidence_document_ids_json,
                    evidence_fact_ids_json, freshness_json, classified_at,
                    valid_until, created_at
                ) VALUES (0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                CLASSIFICATION_VALUES,
            ),
        )
        for statement, values in statements:
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(statement, values)

    def test_v10_foreign_keys_are_exact_and_restrict_missing_parents(self) -> None:
        repository = self.prepared_repository()

        with repository.connect() as connection:
            fact_foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(fund_mandate_facts)"
            ).fetchall()
            classification_foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(fund_risk_classifications)"
            ).fetchall()

        self.assertEqual(len(fact_foreign_keys), 2)
        self.assertEqual(
            {
                (
                    str(row["table"]),
                    str(row["from"]),
                    str(row["to"]),
                    str(row["on_delete"]),
                )
                for row in fact_foreign_keys
            },
            {
                ("fund_document_artifacts", "source_document_id", "id", "RESTRICT"),
                ("fund_document_parse_results", "parse_result_id", "id", "RESTRICT"),
            },
        )
        self.assertEqual(len(classification_foreign_keys), 1)
        self.assertEqual(
            (
                str(classification_foreign_keys[0]["table"]),
                str(classification_foreign_keys[0]["from"]),
                str(classification_foreign_keys[0]["to"]),
                str(classification_foreign_keys[0]["on_delete"]),
            ),
            (
                "fund_classification_policy_versions",
                "policy_version",
                "version",
                "RESTRICT",
            ),
        )

        missing_document = list(FACT_VALUES)
        missing_document[1] = 999
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_fact(repository, missing_document)

        missing_policy = list(CLASSIFICATION_VALUES)
        missing_policy[1] = "missing"
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_classification(repository, missing_policy)


if __name__ == "__main__":
    unittest.main()
