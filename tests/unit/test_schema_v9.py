import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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
    SCHEMA_VERSION,
)
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.store import SuitabilityPolicyStore

SCHEMAS = (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
)

LEGACY_SCHEMA_V8 = """
CREATE TABLE IF NOT EXISTS suitability_policy_versions (
    version TEXT PRIMARY KEY NOT NULL,
    canonical_policy_json TEXT NOT NULL,
    policy_checksum TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suitability_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_version_id INTEGER NOT NULL
        REFERENCES financial_profile_versions(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL
        REFERENCES suitability_policy_versions(version) ON DELETE RESTRICT,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'blocked', 'constrained', 'ready_for_allocation'
    )),
    hard_blocks_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    safe_summary_json TEXT NOT NULL,
    encrypted_amount_results TEXT NOT NULL,
    encryption_algorithm TEXT NOT NULL CHECK(encryption_algorithm = 'AES-256-GCM'),
    encryption_key_version TEXT NOT NULL,
    nonce TEXT NOT NULL,
    keyed_payload_fingerprint TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_update
BEFORE UPDATE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_policy_no_delete
BEFORE DELETE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_update
BEFORE UPDATE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS suitability_assessment_no_delete
BEFORE DELETE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;
"""

PROFILE_VALUES = (
    1,
    "confirmed",
    "AES-256-GCM",
    "1",
    "profile-nonce",
    "profile-ciphertext",
    "profile-fingerprint",
    "2026-07-13T00:00:00+00:00",
    "2026-10-11T00:00:00+00:00",
    "2026-07-13T00:00:00+00:00",
)

FIXED_SUITABILITY_POLICY = SuitabilityPolicyV1()
SUITABILITY_POLICY_VALUES = (
    FIXED_SUITABILITY_POLICY.version,
    FIXED_SUITABILITY_POLICY.canonical_json().decode("utf-8"),
    FIXED_SUITABILITY_POLICY.checksum(),
    FIXED_SUITABILITY_POLICY.effective_at.isoformat(),
    "2026-07-13T00:00:00+00:00",
)

SUITABILITY_ASSESSMENT_VALUES = (
    1,
    "1",
    "b" * 64,
    "ready_for_allocation",
    "[]",
    "[]",
    '{"debt_count":0,"goal_count":1,"obligation_count":0,'
    '"required_reserve_months":6,"risk_answers_consistent":true}',
    "suitability-ciphertext",
    "AES-256-GCM",
    "1",
    "suitability-nonce",
    "c" * 64,
    "2026-07-13T01:00:00+00:00",
    "2026-07-14T01:00:00+00:00",
    "2026-07-13T01:00:00+00:00",
)

ALLOCATION_POLICY_VALUES = (
    "1",
    '{"version":"1"}',
    "d" * 64,
    "2026-07-13T00:00:00+00:00",
    "2026-07-13T02:00:00+00:00",
)

ALLOCATION_ASSESSMENT_VALUES = (
    1,
    1,
    "1",
    "e" * 64,
    "range_available",
    '{"maximum_equity":"0.30"}',
    '["horizon_binding"]',
    '{"goal_count":1}',
    "allocation-ciphertext",
    "AES-256-GCM",
    "1",
    "allocation-nonce",
    "f" * 64,
    "2026-07-13T02:00:00+00:00",
    "2026-07-14T01:00:00+00:00",
    "2026-07-13T02:00:00+00:00",
)


class SchemaV9Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

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

    def legacy_v8_repository(
        self, name: str, *, populated: bool = True, sequence=None
    ) -> Repository:
        repository = self.repository(name)
        with repository.connect() as connection, connection:
            for schema in SCHEMAS[:7]:
                connection.executescript(schema)
            connection.executescript(LEGACY_SCHEMA_V8)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(item, f"2026-07-{item:02d}T00:00:00+00:00") for item in range(1, 9)],
            )
            connection.execute(
                """
                INSERT INTO financial_profile_versions(
                    id, version, status, encryption_algorithm,
                    encryption_key_version, nonce, encrypted_payload,
                    keyed_payload_fingerprint, confirmed_at, valid_until, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                PROFILE_VALUES,
            )
            if populated:
                connection.execute(
                    """
                    INSERT INTO suitability_policy_versions(
                        version, canonical_policy_json, policy_checksum,
                        effective_at, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    SUITABILITY_POLICY_VALUES,
                )
                connection.execute(
                    """
                    INSERT INTO suitability_assessments(
                        id, profile_version_id, policy_version, input_fingerprint,
                        status, hard_blocks_json, constraints_json, safe_summary_json,
                        encrypted_amount_results, encryption_algorithm,
                        encryption_key_version, nonce, keyed_payload_fingerprint,
                        assessed_at, valid_until, created_at
                    ) VALUES (7, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    SUITABILITY_ASSESSMENT_VALUES,
                )
                second_assessment = list(SUITABILITY_ASSESSMENT_VALUES)
                second_assessment[2] = "8" * 64
                second_assessment[11] = "7" * 64
                connection.execute(
                    """
                    INSERT INTO suitability_assessments(
                        id, profile_version_id, policy_version, input_fingerprint,
                        status, hard_blocks_json, constraints_json, safe_summary_json,
                        encrypted_amount_results, encryption_algorithm,
                        encryption_key_version, nonce, keyed_payload_fingerprint,
                        assessed_at, valid_until, created_at
                    ) VALUES (19, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    second_assessment,
                )
            if sequence is not None:
                connection.execute(
                    "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                    (sequence, "suitability_assessments"),
                )
        return repository

    def legacy_v8_with_policy_values(self, name: str, values) -> Repository:
        repository = self.legacy_v8_repository(name, populated=False)
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )
        return repository

    def legacy_v8_with_assessment_values(
        self, name: str, values, *, assessment_id: int = 7
    ) -> Repository:
        repository = self.legacy_v8_with_policy_values(name, SUITABILITY_POLICY_VALUES)
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_assessments(
                    id, profile_version_id, policy_version, input_fingerprint,
                    status, hard_blocks_json, constraints_json, safe_summary_json,
                    encrypted_amount_results, encryption_algorithm,
                    encryption_key_version, nonce, keyed_payload_fingerprint,
                    assessed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (assessment_id, *values),
            )
        return repository

    def suitability_snapshot(self, repository: Repository) -> tuple:
        with repository.connect() as connection:
            schema = connection.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name LIKE 'suitability_%'
                   OR tbl_name IN (
                       'suitability_policy_versions', 'suitability_assessments'
                   )
                ORDER BY type, name
                """
            ).fetchall()
            policies = connection.execute(
                "SELECT * FROM suitability_policy_versions ORDER BY version"
            ).fetchall()
            assessments = connection.execute(
                "SELECT * FROM suitability_assessments ORDER BY id"
            ).fetchall()
            sequence = connection.execute(
                "SELECT typeof(name), name, typeof(seq), seq FROM sqlite_sequence ORDER BY rowid"
            ).fetchall()
            versions = connection.execute(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(
            tuple(tuple(row) for row in rows)
            for rows in (schema, policies, assessments, sequence, versions)
        )

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
        return (
            tuple(tuple(row) for row in schema),
            tuple(tuple(row) for row in versions),
            tuple(tuple(row) for row in sync_runs),
        )

    def prepared_repository(self, name: str = "prepared.db") -> Repository:
        repository = self.repository(name)
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO financial_profile_versions(
                    version, status, encryption_algorithm, encryption_key_version,
                    nonce, encrypted_payload, keyed_payload_fingerprint,
                    confirmed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                PROFILE_VALUES,
            )
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                SUITABILITY_POLICY_VALUES,
            )
            connection.execute(
                """
                INSERT INTO suitability_assessments(
                    profile_version_id, policy_version, input_fingerprint,
                    status, hard_blocks_json, constraints_json, safe_summary_json,
                    encrypted_amount_results, encryption_algorithm,
                    encryption_key_version, nonce, keyed_payload_fingerprint,
                    assessed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                SUITABILITY_ASSESSMENT_VALUES,
            )
            connection.execute(
                """
                INSERT INTO allocation_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ALLOCATION_POLICY_VALUES,
            )
        return repository

    def insert_allocation_assessment(
        self, repository: Repository, values=ALLOCATION_ASSESSMENT_VALUES
    ) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO allocation_assessments(
                    profile_version_id, suitability_assessment_id, policy_version,
                    input_fingerprint, status, permitted_region_json,
                    binding_constraints_json, safe_summary_json,
                    encrypted_amount_results, encryption_algorithm,
                    encryption_key_version, nonce, keyed_payload_fingerprint,
                    assessed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

    def test_current_schema_version_is_thirteen(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 22)

    def test_fresh_migration_adds_exact_tables_columns_indexes_and_versions(self) -> None:
        repository = self.repository()
        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            policy_columns = connection.execute(
                "PRAGMA table_info(allocation_policy_versions)"
            ).fetchall()
            assessment_columns = connection.execute(
                "PRAGMA table_info(allocation_assessments)"
            ).fetchall()
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 23)))
        self.assertEqual(
            [str(row["name"]) for row in policy_columns],
            [
                "version",
                "canonical_policy_json",
                "policy_checksum",
                "effective_at",
                "created_at",
            ],
        )
        self.assertEqual(
            [str(row["name"]) for row in assessment_columns],
            [
                "id",
                "profile_version_id",
                "suitability_assessment_id",
                "policy_version",
                "input_fingerprint",
                "status",
                "permitted_region_json",
                "binding_constraints_json",
                "safe_summary_json",
                "encrypted_amount_results",
                "encryption_algorithm",
                "encryption_key_version",
                "nonce",
                "keyed_payload_fingerprint",
                "assessed_at",
                "valid_until",
                "created_at",
            ],
        )
        self.assertIn("allocation_assessments_binding_lookup", indexes)
        self.assertIn("allocation_assessments_history", indexes)

    def test_migration_is_atomic_from_every_supported_prior_version(self) -> None:
        for version in range(1, 9):
            with self.subTest(version=version):
                repository = self.repository_at_version(version, f"upgrade-from-v{version}.db")
                repository.migrate()
                with repository.connect() as connection:
                    versions = connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                self.assertEqual([int(row["version"]) for row in versions], list(range(1, 23)))
                self.assertIn("allocation_policy_versions", repository.table_names())
                self.assertIn("allocation_assessments", repository.table_names())

    def test_exact_legacy_v8_empty_database_normalizes_before_v9(self) -> None:
        repository = self.legacy_v8_repository("legacy-empty.db", populated=False)

        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            policies = connection.execute(
                "SELECT COUNT(*) FROM suitability_policy_versions"
            ).fetchone()[0]
            assessments = connection.execute(
                "SELECT COUNT(*) FROM suitability_assessments"
            ).fetchone()[0]
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 23)))
        self.assertEqual(policies, 0)
        self.assertEqual(assessments, 0)

    def test_exact_legacy_v8_rows_ids_bytes_and_sequence_are_preserved(self) -> None:
        repository = self.legacy_v8_repository("legacy-populated.db", populated=True, sequence=41)
        with repository.connect() as connection:
            before_policies = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM suitability_policy_versions ORDER BY version"
                ).fetchall()
            )
            before_assessments = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM suitability_assessments ORDER BY id"
                ).fetchall()
            )

        repository.migrate()

        with repository.connect() as connection:
            after_policies = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM suitability_policy_versions ORDER BY version"
                ).fetchall()
            )
            after_assessments = tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM suitability_assessments ORDER BY id"
                ).fetchall()
            )
            sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'suitability_assessments'"
            ).fetchone()[0]
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(after_policies, before_policies)
        self.assertEqual(after_assessments, before_assessments)
        self.assertEqual(sequence, 41)
        self.assertEqual(foreign_key_errors, [])
        self.assertEqual(integrity, "ok")

    def test_legacy_v8_normalization_finishes_with_exact_current_schema(self) -> None:
        repository = self.legacy_v8_repository("legacy-exact-schema.db")
        repository.migrate()

        fresh = self.repository("fresh-exact-schema.db")
        fresh.migrate()

        with repository.connect() as connection:
            actual = connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
                """
            ).fetchall()
        with fresh.connect() as connection:
            expected = connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
                """
            ).fetchall()
        self.assertEqual(tuple(tuple(row) for row in actual), tuple(tuple(row) for row in expected))

    def test_normalized_legacy_v8_rows_keep_foreign_keys_and_immutability(self) -> None:
        repository = self.legacy_v8_repository("legacy-constraints.db")
        repository.migrate()

        with repository.connect() as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(suitability_assessments)"
            ).fetchall()
            by_table = {str(row["table"]): row for row in foreign_keys}
            self.assertEqual(str(by_table["financial_profile_versions"]["on_delete"]), "RESTRICT")
            self.assertEqual(str(by_table["suitability_policy_versions"]["on_delete"]), "RESTRICT")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE suitability_assessments SET status = 'blocked' WHERE id = 7"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM suitability_policy_versions WHERE version = '1'")

    def test_altered_missing_or_extra_legacy_v8_owned_objects_roll_back(self) -> None:
        cases = (
            ("missing", "DROP TRIGGER suitability_assessment_no_delete"),
            (
                "altered",
                """
                DROP TRIGGER suitability_policy_no_update;
                CREATE TRIGGER suitability_policy_no_update
                BEFORE UPDATE ON suitability_policy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'altered');
                END;
                """,
            ),
            (
                "extra",
                "CREATE INDEX suitability_hostile_index ON suitability_assessments(status)",
            ),
            ("extra-mixed-case", "CREATE TABLE Suitability_Hostile(id INTEGER)"),
        )
        for name, mutation in cases:
            with self.subTest(name=name):
                repository = self.legacy_v8_repository(f"legacy-owned-{name}.db")
                with repository.connect() as connection, connection:
                    connection.executescript(mutation)
                before = (
                    self.database_snapshot(repository),
                    self.suitability_snapshot(repository),
                )

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(
                    (
                        self.database_snapshot(repository),
                        self.suitability_snapshot(repository),
                    ),
                    before,
                )

    def test_legacy_v8_normalization_name_collisions_roll_back(self) -> None:
        for collision in (
            "__kunjin_legacy_v8_policy_versions",
            "__kunjin_legacy_v8_assessments",
        ):
            with self.subTest(collision=collision):
                repository = self.legacy_v8_repository(f"{collision}.db")
                with repository.connect() as connection, connection:
                    connection.execute(f'CREATE TABLE "{collision}"(id INTEGER)')
                before = (
                    self.database_snapshot(repository),
                    self.suitability_snapshot(repository),
                )

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(
                    (
                        self.database_snapshot(repository),
                        self.suitability_snapshot(repository),
                    ),
                    before,
                )

    def test_invalid_legacy_v8_row_rolls_back_without_normalization(self) -> None:
        repository = self.legacy_v8_repository("legacy-invalid-row.db", populated=False)
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES ('1', '{}', 'weak', 'not-a-time', 'also-not-a-time')
                """
            )
        before = (
            self.database_snapshot(repository),
            self.suitability_snapshot(repository),
        )

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

        self.assertEqual(
            (
                self.database_snapshot(repository),
                self.suitability_snapshot(repository),
            ),
            before,
        )

    def test_invalid_legacy_v8_sequence_rolls_back_without_normalization(self) -> None:
        repository = self.legacy_v8_repository("legacy-invalid-sequence.db")
        with repository.connect() as connection, connection:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = 'invalid' WHERE name = 'suitability_assessments'"
            )
        before = (
            self.database_snapshot(repository),
            self.suitability_snapshot(repository),
        )

        with self.assertRaisesRegex(sqlite3.DatabaseError, "sequence"):
            repository.migrate()

        self.assertEqual(
            (
                self.database_snapshot(repository),
                self.suitability_snapshot(repository),
            ),
            before,
        )

    def test_duplicate_legacy_v8_sequence_rows_fail_closed(self) -> None:
        duplicate_names = (
            "suitability_assessments",
            sqlite3.Binary(b"suitability_assessments"),
        )
        for case_number, duplicate_name in enumerate(duplicate_names):
            with self.subTest(case_number=case_number):
                repository = self.legacy_v8_repository(
                    f"legacy-duplicate-sequence-{case_number}.db"
                )
                with repository.connect() as connection, connection:
                    connection.execute(
                        "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                        (duplicate_name, 99),
                    )
                before = (
                    self.database_snapshot(repository),
                    self.suitability_snapshot(repository),
                )

                with self.assertRaisesRegex(sqlite3.DatabaseError, "sequence"):
                    repository.migrate()

                self.assertEqual(
                    (
                        self.database_snapshot(repository),
                        self.suitability_snapshot(repository),
                    ),
                    before,
                )

    def test_unexpected_inbound_foreign_key_to_legacy_v8_tables_is_rejected(self) -> None:
        targets = (
            ("suitability_policy_versions", "version", "TEXT"),
            ("suitability_assessments", "id", "INTEGER"),
            ("SUITABILITY_POLICY_VERSIONS", "version", "TEXT"),
            ("Suitability_Assessments", "id", "INTEGER"),
        )
        for case_number, (target, column, value_type) in enumerate(targets):
            with self.subTest(target=target):
                repository = self.legacy_v8_repository(f"legacy-inbound-{case_number}.db")
                with repository.connect() as connection, connection:
                    connection.execute(
                        f"""
                        CREATE TABLE hostile_inbound(
                            value {value_type} REFERENCES {target}({column})
                        )
                        """
                    )
                before = (
                    self.database_snapshot(repository),
                    self.suitability_snapshot(repository),
                )

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(
                    (
                        self.database_snapshot(repository),
                        self.suitability_snapshot(repository),
                    ),
                    before,
                )

    def test_external_view_and_trigger_keep_canonical_references(self) -> None:
        repository = self.legacy_v8_repository("legacy-external-dependencies.db")
        with repository.connect() as connection, connection:
            connection.executescript(
                """
                CREATE VIEW external_suitability_view AS
                SELECT id FROM suitability_assessments;
                CREATE TABLE external_events(id INTEGER PRIMARY KEY);
                CREATE TRIGGER external_suitability_reader
                AFTER INSERT ON external_events
                BEGIN
                    SELECT COUNT(*) FROM suitability_assessments;
                END;
                """
            )
            before_sql = connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE name IN (
                    'external_suitability_view', 'external_suitability_reader'
                )
                ORDER BY name
                """
            ).fetchall()

        repository.migrate()

        with repository.connect() as connection, connection:
            after_sql = connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE name IN (
                    'external_suitability_view', 'external_suitability_reader'
                )
                ORDER BY name
                """
            ).fetchall()
            view_rows = connection.execute(
                "SELECT id FROM external_suitability_view ORDER BY id"
            ).fetchall()
            connection.execute("INSERT INTO external_events(id) VALUES (1)")
        self.assertEqual(
            tuple(tuple(row) for row in after_sql),
            tuple(tuple(row) for row in before_sql),
        )
        self.assertEqual([int(row["id"]) for row in view_rows], [7, 19])

    def test_legacy_v8_policy_rows_require_exact_usable_content(self) -> None:
        fixed_canonical = SUITABILITY_POLICY_VALUES[1]
        tampered_payload = json.loads(fixed_canonical)
        tampered_payload["reserve_months_stable"] = 7
        tampered_canonical = json.dumps(tampered_payload, separators=(",", ":"), sort_keys=True)
        unsupported_payload = json.loads(fixed_canonical)
        unsupported_payload["version"] = "2"
        unsupported_canonical = json.dumps(
            unsupported_payload, separators=(",", ":"), sort_keys=True
        )
        cases = (
            (
                "nul-version",
                ("1\x00", *SUITABILITY_POLICY_VALUES[1:]),
            ),
            (
                "blob-json",
                (
                    "1",
                    sqlite3.Binary(fixed_canonical.encode("utf-8")),
                    *SUITABILITY_POLICY_VALUES[2:],
                ),
            ),
            (
                "duplicate-json-key",
                (
                    "1",
                    fixed_canonical[:-1] + ',"version":"1"}',
                    *SUITABILITY_POLICY_VALUES[2:],
                ),
            ),
            (
                "checksum-mismatch",
                (
                    SUITABILITY_POLICY_VALUES[0],
                    fixed_canonical,
                    "0" * 64,
                    *SUITABILITY_POLICY_VALUES[3:],
                ),
            ),
            (
                "tampered-fixed-content",
                (
                    SUITABILITY_POLICY_VALUES[0],
                    tampered_canonical,
                    SUITABILITY_POLICY_VALUES[2],
                    *SUITABILITY_POLICY_VALUES[3:],
                ),
            ),
            (
                "unsupported-version",
                (
                    "2",
                    unsupported_canonical,
                    SUITABILITY_POLICY_VALUES[2],
                    *SUITABILITY_POLICY_VALUES[3:],
                ),
            ),
            (
                "uppercase-digest",
                (
                    SUITABILITY_POLICY_VALUES[0],
                    fixed_canonical,
                    SUITABILITY_POLICY_VALUES[2].upper(),
                    *SUITABILITY_POLICY_VALUES[3:],
                ),
            ),
            (
                "wrong-effective-at",
                (
                    *SUITABILITY_POLICY_VALUES[:3],
                    "2026-07-11T00:00:00+00:00",
                    SUITABILITY_POLICY_VALUES[4],
                ),
            ),
        )
        for name, values in cases:
            with self.subTest(name=name):
                repository = self.legacy_v8_with_policy_values(
                    f"legacy-invalid-policy-{name}.db", values
                )
                before = self.suitability_snapshot(repository)

                with self.assertRaises((sqlite3.DatabaseError, sqlite3.IntegrityError)):
                    repository.migrate()

                self.assertEqual(self.suitability_snapshot(repository), before)

    def test_migrated_legacy_policy_is_readable_by_policy_store(self) -> None:
        repository = self.legacy_v8_repository("legacy-policy-store-readable.db")

        repository.migrate()
        record = SuitabilityPolicyStore(repository).get(FIXED_SUITABILITY_POLICY.version)

        self.assertIsNotNone(record)
        self.assertEqual(record.version, FIXED_SUITABILITY_POLICY.version)
        self.assertEqual(
            record.canonical_policy_json,
            FIXED_SUITABILITY_POLICY.canonical_json().decode("utf-8"),
        )
        self.assertEqual(record.policy_checksum, FIXED_SUITABILITY_POLICY.checksum())
        self.assertEqual(record.effective_at, FIXED_SUITABILITY_POLICY.effective_at)

    def test_legacy_v8_assessment_rows_require_exact_usable_content(self) -> None:
        cases = []

        def changed(index: int, value):
            values = list(SUITABILITY_ASSESSMENT_VALUES)
            values[index] = value
            return tuple(values)

        cases.extend(
            (
                ("blob-fingerprint", changed(2, sqlite3.Binary(b"b" * 64)), 7),
                ("numeric-block-reason", changed(4, "[1]"), 7),
                ("invalid-safe-summary", changed(6, "{}"), 7),
                ("blob-ciphertext", changed(7, sqlite3.Binary(b"ciphertext")), 7),
                ("nul-key-version", changed(9, "1\x00"), 7),
                ("naive-assessed-at", changed(12, "2026-07-13T01:00:00"), 7),
                (
                    "status-reason-mismatch",
                    changed(4, '["emergency_reserve_shortfall"]'),
                    7,
                ),
                ("nonpositive-id", SUITABILITY_ASSESSMENT_VALUES, 0),
            )
        )
        for name, values, assessment_id in cases:
            with self.subTest(name=name):
                repository = self.legacy_v8_with_assessment_values(
                    f"legacy-invalid-assessment-{name}.db",
                    values,
                    assessment_id=assessment_id,
                )
                before = self.suitability_snapshot(repository)

                with self.assertRaises((sqlite3.DatabaseError, sqlite3.IntegrityError)):
                    repository.migrate()

                self.assertEqual(self.suitability_snapshot(repository), before)

    def test_legacy_v8_marker_nine_is_not_normalized(self) -> None:
        repository = self.legacy_v8_repository("legacy-false-v9.db")
        with repository.connect() as connection, connection:
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (9, ?)",
                ("2026-07-09T00:00:00+00:00",),
            )
        before = (
            self.database_snapshot(repository),
            self.suitability_snapshot(repository),
        )

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

        self.assertEqual(
            (
                self.database_snapshot(repository),
                self.suitability_snapshot(repository),
            ),
            before,
        )

    def test_strong_v8_is_not_rebuilt_before_v9(self) -> None:
        repository = self.repository_at_version(8, "strong-v8.db")

        with patch(
            "kunjin.storage.repository._normalize_legacy_v8",
            side_effect=AssertionError("strong V8 was normalized"),
        ):
            repository.migrate()

        self.assertIn("allocation_assessments", repository.table_names())

    def test_failure_during_legacy_v8_rebuild_rolls_back_every_stage(self) -> None:
        for stage in ("backed_up", "schema_created", "rows_copied", "legacy_dropped"):
            with self.subTest(stage=stage):
                repository = self.legacy_v8_repository(f"legacy-injected-failure-{stage}.db")
                before = (
                    self.database_snapshot(repository),
                    self.suitability_snapshot(repository),
                )

                def fail_at_stage(actual_stage: str) -> None:
                    if actual_stage == stage:
                        raise RuntimeError(f"injected failure at {stage}")

                with patch(
                    "kunjin.storage.repository._legacy_v8_normalization_checkpoint",
                    side_effect=fail_at_stage,
                ):
                    with self.assertRaisesRegex(RuntimeError, stage):
                        repository.migrate()

                self.assertEqual(
                    (
                        self.database_snapshot(repository),
                        self.suitability_snapshot(repository),
                    ),
                    before,
                )

    def test_v9_failure_after_legacy_normalization_restores_exact_legacy_v8(self) -> None:
        repository = self.legacy_v8_repository("legacy-v9-failure.db", populated=True, sequence=41)
        before = (
            self.database_snapshot(repository),
            self.suitability_snapshot(repository),
        )
        broken_v9 = """
        CREATE TABLE allocation_policy_versions(id INTEGER PRIMARY KEY);
        CREATE TABLE allocation_assessments(id INTEGER PRIMARY KEY);
        CREATE TABLE incomplete_v9 (
        """

        with patch("kunjin.storage.repository.SCHEMA_V9", broken_v9):
            with self.assertRaises(sqlite3.OperationalError):
                repository.migrate()

        self.assertEqual(
            (
                self.database_snapshot(repository),
                self.suitability_snapshot(repository),
            ),
            before,
        )

    def test_failed_v9_migration_leaves_no_v9_objects_or_marker(self) -> None:
        broken_v9 = """
        CREATE TABLE allocation_policy_versions(id INTEGER PRIMARY KEY);
        CREATE TABLE allocation_assessments(id INTEGER PRIMARY KEY);
        CREATE INDEX partial_allocation_index ON allocation_assessments(id);
        CREATE TRIGGER partial_allocation_trigger
        AFTER INSERT ON allocation_assessments
        BEGIN
            SELECT 1;
        END;
        CREATE TABLE syntax_error (
        """

        for version in range(1, 9):
            with self.subTest(version=version):
                repository = self.repository_at_version(version, f"failed-v9-from-v{version}.db")
                before = self.database_snapshot(repository)
                with patch("kunjin.storage.repository.SCHEMA_V9", broken_v9):
                    with self.assertRaises(sqlite3.OperationalError):
                        repository.migrate()
                self.assertEqual(self.database_snapshot(repository), before)

    def test_v9_name_collisions_fail_without_changing_existing_database(self) -> None:
        hostile_objects = (
            "CREATE TABLE allocation_policy_versions(hostile TEXT);",
            "CREATE TABLE allocation_assessments(hostile TEXT);",
            """
            CREATE TABLE hostile_index_target(id INTEGER);
            CREATE INDEX allocation_assessments_binding_lookup
            ON hostile_index_target(id);
            """,
            """
            CREATE TABLE hostile_trigger_target(id INTEGER);
            CREATE TRIGGER allocation_policy_no_update
            BEFORE UPDATE ON hostile_trigger_target
            BEGIN
                SELECT RAISE(ABORT, 'hostile trigger body');
            END;
            """,
        )
        for case_number, hostile_sql in enumerate(hostile_objects):
            with self.subTest(case_number=case_number):
                repository = self.repository_at_version(8, f"hostile-{case_number}.db")
                with repository.connect() as connection, connection:
                    connection.executescript(hostile_sql)
                before = self.database_snapshot(repository)

                with self.assertRaisesRegex(sqlite3.OperationalError, "already exists"):
                    repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)
                with repository.connect() as connection:
                    marker = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = 9"
                    ).fetchone()
                self.assertIsNone(marker)

    def test_completed_migration_is_reentrant_without_replaying_schema(self) -> None:
        repository = self.repository()
        repository.migrate()
        before = self.database_snapshot(repository)

        with patch(
            "kunjin.storage.repository._execute_schema",
            side_effect=AssertionError("applied schema replayed"),
        ):
            repository.migrate()

        self.assertEqual(self.database_snapshot(repository), before)

    def test_invalid_gap_future_and_false_v9_markers_fail_without_changes(self) -> None:
        cases = (
            (
                "zero",
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (0, '2026-07-13T00:00:00+00:00')",
            ),
            (
                "negative",
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (-1, '2026-07-13T00:00:00+00:00')",
            ),
            ("gap", "DELETE FROM schema_migrations WHERE version = 4"),
            (
                "future",
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (13, '2026-07-13T00:00:00+00:00')",
            ),
            (
                "false-v9",
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (9, '2026-07-13T00:00:00+00:00')",
            ),
        )
        for name, mutation in cases:
            with self.subTest(name=name):
                repository = self.repository_at_version(8, f"marker-{name}.db")
                with repository.connect() as connection, connection:
                    connection.execute(mutation)
                before = self.database_snapshot(repository)

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)

    def test_false_earlier_marker_object_fails_without_changes(self) -> None:
        repository = self.repository_at_version(8, "missing-earlier-object.db")
        with repository.connect() as connection, connection:
            connection.execute("DROP TABLE funds")
        before = self.database_snapshot(repository)

        with self.assertRaises(sqlite3.DatabaseError):
            repository.migrate()

        self.assertEqual(self.database_snapshot(repository), before)

    def test_missing_altered_or_extra_v9_objects_fail_without_changes(self) -> None:
        cases = (
            ("missing-trigger", "DROP TRIGGER allocation_assessment_no_delete"),
            (
                "altered-table",
                """
                DROP TABLE allocation_assessments;
                CREATE TABLE allocation_assessments(id INTEGER PRIMARY KEY);
                """,
            ),
            (
                "altered-index",
                """
                DROP INDEX allocation_assessments_binding_lookup;
                CREATE INDEX allocation_assessments_binding_lookup
                ON allocation_assessments(policy_version);
                """,
            ),
            (
                "altered-trigger",
                """
                DROP TRIGGER allocation_policy_no_update;
                CREATE TRIGGER allocation_policy_no_update
                BEFORE UPDATE ON allocation_policy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'altered trigger');
                END;
                """,
            ),
            (
                "extra-index",
                "CREATE INDEX allocation_extra_index ON allocation_assessments(status);",
            ),
        )
        for name, mutation in cases:
            with self.subTest(name=name):
                repository = self.repository(f"v9-object-{name}.db")
                repository.migrate()
                with repository.connect() as connection, connection:
                    connection.executescript(mutation)
                before = self.database_snapshot(repository)

                with self.assertRaises(sqlite3.DatabaseError):
                    repository.migrate()

                self.assertEqual(self.database_snapshot(repository), before)

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
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 23)))

    def test_foreign_keys_restrict_profile_suitability_and_policy_deletion(self) -> None:
        repository = self.prepared_repository()
        self.insert_allocation_assessment(repository)

        with repository.connect() as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(allocation_assessments)"
            ).fetchall()
        by_table = {str(row["table"]): row for row in foreign_keys}
        expected = {
            "financial_profile_versions": ("profile_version_id", "id"),
            "suitability_assessments": ("suitability_assessment_id", "id"),
            "allocation_policy_versions": ("policy_version", "version"),
        }
        for table, (source, target) in expected.items():
            with self.subTest(table=table):
                self.assertEqual(str(by_table[table]["from"]), source)
                self.assertEqual(str(by_table[table]["to"]), target)
                self.assertEqual(str(by_table[table]["on_delete"]), "RESTRICT")

        invalid_values = list(ALLOCATION_ASSESSMENT_VALUES)
        for index in (0, 1, 2):
            with self.subTest(foreign_key_index=index):
                values = invalid_values.copy()
                values[index] = 999 if index < 2 else "missing"
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)

    def test_rows_are_immutable_and_policy_version_is_unique(self) -> None:
        repository = self.prepared_repository()
        self.insert_allocation_assessment(repository)

        statements = (
            (
                "UPDATE allocation_policy_versions SET canonical_policy_json = '{}'",
                "allocation policies are immutable",
            ),
            (
                "DELETE FROM allocation_policy_versions",
                "allocation policies are immutable",
            ),
            (
                "UPDATE allocation_assessments SET status = 'range_available'",
                "allocation assessments are immutable",
            ),
            (
                "DELETE FROM allocation_assessments",
                "allocation assessments are immutable",
            ),
        )
        for statement, message in statements:
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                    with repository.connect() as connection, connection:
                        connection.execute(statement)

        with self.assertRaises(sqlite3.IntegrityError):
            with repository.connect() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO allocation_policy_versions(
                        version, canonical_policy_json, policy_checksum,
                        effective_at, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ALLOCATION_POLICY_VALUES,
                )

        replace_statements = (
            (
                """
                INSERT OR REPLACE INTO allocation_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                ALLOCATION_POLICY_VALUES,
                "allocation policies are immutable",
            ),
            (
                """
                INSERT OR REPLACE INTO allocation_assessments(
                    id, profile_version_id, suitability_assessment_id,
                    policy_version, input_fingerprint, status,
                    permitted_region_json, binding_constraints_json,
                    safe_summary_json, encrypted_amount_results,
                    encryption_algorithm, encryption_key_version, nonce,
                    keyed_payload_fingerprint, assessed_at, valid_until, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ALLOCATION_ASSESSMENT_VALUES,
                "allocation assessments are immutable",
            ),
        )
        for statement, values, message in replace_statements:
            with self.subTest(replace=message):
                with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                    with repository.connect() as connection, connection:
                        connection.execute(statement, values)

    def test_index_column_order_and_trigger_sql_are_exact(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection:
            binding_index = connection.execute(
                "PRAGMA index_xinfo(allocation_assessments_binding_lookup)"
            ).fetchall()
            history_index = connection.execute(
                "PRAGMA index_xinfo(allocation_assessments_history)"
            ).fetchall()
            triggers = connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger' AND name LIKE 'allocation_%'
                ORDER BY name
                """
            ).fetchall()

        self.assertEqual(
            [(str(row["name"]), int(row["desc"])) for row in binding_index if row["key"]],
            [
                ("profile_version_id", 0),
                ("suitability_assessment_id", 0),
                ("policy_version", 0),
                ("assessed_at", 1),
            ],
        )
        self.assertEqual(
            [(str(row["name"]), int(row["desc"])) for row in history_index if row["key"]],
            [("assessed_at", 1), ("id", 1)],
        )
        expected_trigger_sql = {
            "allocation_assessment_no_delete": """
                CREATE TRIGGER allocation_assessment_no_delete
                BEFORE DELETE ON allocation_assessments
                BEGIN
                    SELECT RAISE(ABORT, 'allocation assessments are immutable');
                END
            """,
            "allocation_assessment_no_replace": """
                CREATE TRIGGER allocation_assessment_no_replace
                BEFORE INSERT ON allocation_assessments
                WHEN EXISTS (
                    SELECT 1 FROM allocation_assessments WHERE id = NEW.id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'allocation assessments are immutable');
                END
            """,
            "allocation_assessment_no_update": """
                CREATE TRIGGER allocation_assessment_no_update
                BEFORE UPDATE ON allocation_assessments
                BEGIN
                    SELECT RAISE(ABORT, 'allocation assessments are immutable');
                END
            """,
            "allocation_policy_no_delete": """
                CREATE TRIGGER allocation_policy_no_delete
                BEFORE DELETE ON allocation_policy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'allocation policies are immutable');
                END
            """,
            "allocation_policy_no_replace": """
                CREATE TRIGGER allocation_policy_no_replace
                BEFORE INSERT ON allocation_policy_versions
                WHEN EXISTS (
                    SELECT 1 FROM allocation_policy_versions WHERE version = NEW.version
                )
                BEGIN
                    SELECT RAISE(ABORT, 'allocation policies are immutable');
                END
            """,
            "allocation_policy_no_update": """
                CREATE TRIGGER allocation_policy_no_update
                BEFORE UPDATE ON allocation_policy_versions
                BEGIN
                    SELECT RAISE(ABORT, 'allocation policies are immutable');
                END
            """,
        }
        self.assertEqual({str(row["name"]) for row in triggers}, set(expected_trigger_sql))
        for row in triggers:
            actual = " ".join(str(row["sql"]).split())
            expected = " ".join(expected_trigger_sql[str(row["name"])].split())
            self.assertEqual(actual, expected)

    def test_assessment_id_must_be_positive_for_insert_and_replace(self) -> None:
        repository = self.prepared_repository()
        statement = """
            INSERT OR REPLACE INTO allocation_assessments(
                id, profile_version_id, suitability_assessment_id,
                policy_version, input_fingerprint, status,
                permitted_region_json, binding_constraints_json,
                safe_summary_json, encrypted_amount_results,
                encryption_algorithm, encryption_key_version, nonce,
                keyed_payload_fingerprint, assessed_at, valid_until, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        for invalid_id in (0, -1):
            for verb in ("INSERT", "INSERT OR REPLACE"):
                with self.subTest(invalid_id=invalid_id, verb=verb):
                    sql = statement.replace("INSERT OR REPLACE", verb, 1)
                    with self.assertRaises(sqlite3.IntegrityError):
                        with repository.connect() as connection, connection:
                            connection.execute(
                                sql,
                                (invalid_id, *ALLOCATION_ASSESSMENT_VALUES),
                            )

        self.insert_allocation_assessment(repository)
        with repository.connect() as connection:
            row = connection.execute("SELECT id FROM allocation_assessments").fetchone()
        self.assertEqual(int(row["id"]), 1)

    def test_every_policy_text_field_rejects_nul_blob_and_numeric_storage(self) -> None:
        replacements = ("valid\x00suffix", sqlite3.Binary(b"1"), 1)
        for index in range(len(ALLOCATION_POLICY_VALUES)):
            for replacement in replacements:
                with self.subTest(index=index, replacement=type(replacement).__name__):
                    repository = self.repository(
                        f"policy-storage-{index}-{type(replacement).__name__}.db"
                    )
                    repository.migrate()
                    values = list(ALLOCATION_POLICY_VALUES)
                    values[index] = replacement
                    with self.assertRaises(sqlite3.IntegrityError):
                        with repository.connect() as connection, connection:
                            connection.execute(
                                """
                                INSERT INTO allocation_policy_versions(
                                    version, canonical_policy_json, policy_checksum,
                                    effective_at, created_at
                                ) VALUES (?, ?, ?, ?, ?)
                                """,
                                values,
                            )

    def test_every_assessment_text_field_rejects_nul_blob_and_numeric_storage(self) -> None:
        replacements = ("valid\x00suffix", sqlite3.Binary(b"1"), 1)
        for index in range(2, len(ALLOCATION_ASSESSMENT_VALUES)):
            for replacement in replacements:
                with self.subTest(index=index, replacement=type(replacement).__name__):
                    repository = self.prepared_repository(
                        f"assessment-storage-{index}-{type(replacement).__name__}.db"
                    )
                    values = list(ALLOCATION_ASSESSMENT_VALUES)
                    values[index] = replacement
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_allocation_assessment(repository, values)

    def test_digest_blobs_and_non_utc_time_offsets_are_rejected(self) -> None:
        policy_cases = (
            (2, sqlite3.Binary(b"d" * 64)),
            (3, "2026-07-13T08:00:00+08:00"),
            (4, "2026-07-13T02:00:00Z"),
        )
        for case_number, (index, replacement) in enumerate(policy_cases):
            with self.subTest(table="policy", index=index):
                repository = self.repository(f"policy-special-{case_number}.db")
                repository.migrate()
                values = list(ALLOCATION_POLICY_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO allocation_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

        assessment_cases = (
            (3, sqlite3.Binary(b"e" * 64)),
            (12, sqlite3.Binary(b"f" * 64)),
            (13, "2026-07-13T10:00:00+08:00"),
            (14, "2026-07-14T09:00:00+08:00"),
            (15, "2026-07-13T02:00:00Z"),
        )
        for case_number, (index, replacement) in enumerate(assessment_cases):
            with self.subTest(table="assessment", index=index):
                repository = self.prepared_repository(f"assessment-special-{case_number}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)

    def test_noncanonical_iso_time_shapes_are_rejected(self) -> None:
        invalid_times = (
            "2026-07-13 02:00:00+00:00",
            "2026-07-13T02:00:00.1+00:00",
            "2026-07-13T02:00:00.12+00:00",
            "2026-07-13T02:00:00.123+00:00",
            "2026-07-13T02:00:00.1234+00:00",
            "2026-07-13T02:00:00.12345+00:00",
            "2026-07-13T02:00:00.1234567+00:00",
        )
        for case_number, invalid_time in enumerate(invalid_times):
            with self.subTest(table="policy", invalid_time=invalid_time):
                repository = self.repository(f"policy-time-shape-{case_number}.db")
                repository.migrate()
                values = list(ALLOCATION_POLICY_VALUES)
                values[3] = invalid_time
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO allocation_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

            with self.subTest(table="assessment", invalid_time=invalid_time):
                repository = self.prepared_repository(f"assessment-time-shape-{case_number}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[13] = invalid_time
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)

    def test_canonical_whole_and_six_fraction_times_sort_chronologically(self) -> None:
        repository = self.prepared_repository("canonical-time-order.db")
        whole = list(ALLOCATION_ASSESSMENT_VALUES)
        whole[13] = "2026-07-13T02:00:00+00:00"
        whole[14] = "2026-07-14T02:00:00+00:00"
        whole[15] = "2026-07-13T02:00:00+00:00"
        fractional = list(ALLOCATION_ASSESSMENT_VALUES)
        fractional[13] = "2026-07-13T02:00:00.000001+00:00"
        fractional[14] = "2026-07-14T02:00:00.000001+00:00"
        fractional[15] = "2026-07-13T02:00:00.000001+00:00"

        self.insert_allocation_assessment(repository, whole)
        self.insert_allocation_assessment(repository, fractional)

        with repository.connect() as connection:
            rows = connection.execute(
                "SELECT assessed_at FROM allocation_assessments ORDER BY assessed_at DESC"
            ).fetchall()
        self.assertEqual(
            [str(row["assessed_at"]) for row in rows],
            [fractional[13], whole[13]],
        )

    def test_calendar_and_clock_components_are_strict_for_every_time_field(self) -> None:
        invalid_times = (
            "2026-01-01T00:00:00.000000+00:00",
            "2023-02-29T00:00:00+00:00",
            "2026-02-30T00:00:00+00:00",
            "2026-13-01T00:00:00+00:00",
            "2026-01-00T00:00:00+00:00",
            "2026-01-01T24:00:00+00:00",
            "2026-01-01T00:60:00+00:00",
            "2026-01-01T00:00:60+00:00",
            "0000-01-01T00:00:00+00:00",
            "10000-01-01T00:00:00+00:00",
        )
        for field_index in (3, 4):
            for case_number, invalid_time in enumerate(invalid_times):
                with self.subTest(
                    table="policy", field_index=field_index, invalid_time=invalid_time
                ):
                    repository = self.repository(f"policy-calendar-{field_index}-{case_number}.db")
                    repository.migrate()
                    values = list(ALLOCATION_POLICY_VALUES)
                    values[field_index] = invalid_time
                    with self.assertRaises(sqlite3.IntegrityError):
                        with repository.connect() as connection, connection:
                            connection.execute(
                                """
                                INSERT INTO allocation_policy_versions(
                                    version, canonical_policy_json, policy_checksum,
                                    effective_at, created_at
                                ) VALUES (?, ?, ?, ?, ?)
                                """,
                                values,
                            )

        for field_index in (13, 14, 15):
            for case_number, invalid_time in enumerate(invalid_times):
                with self.subTest(
                    table="assessment", field_index=field_index, invalid_time=invalid_time
                ):
                    repository = self.prepared_repository(
                        f"assessment-calendar-{field_index}-{case_number}.db"
                    )
                    values = list(ALLOCATION_ASSESSMENT_VALUES)
                    if field_index == 14:
                        values[13] = "2025-01-01T00:00:00+00:00"
                        values[15] = "2025-01-01T00:00:00+00:00"
                    values[field_index] = invalid_time
                    with self.assertRaises(sqlite3.IntegrityError):
                        self.insert_allocation_assessment(repository, values)

    def test_valid_leap_day_is_accepted_in_every_time_field(self) -> None:
        repository = self.repository("valid-policy-leap-day.db")
        repository.migrate()
        policy_values = list(ALLOCATION_POLICY_VALUES)
        policy_values[3] = "2024-02-29T23:59:59+00:00"
        policy_values[4] = "2024-02-29T23:59:59.123456+00:00"
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO allocation_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                policy_values,
            )

        repository = self.prepared_repository("valid-assessment-leap-day.db")
        assessment_values = list(ALLOCATION_ASSESSMENT_VALUES)
        assessment_values[13] = "2024-02-29T23:59:59+00:00"
        assessment_values[14] = "2024-03-01T00:00:00+00:00"
        assessment_values[15] = "2024-02-29T23:59:59.123456+00:00"
        self.insert_allocation_assessment(repository, assessment_values)

    def test_validity_uses_exact_canonical_utc_text_ordering(self) -> None:
        accepted = (
            (
                "2026-07-13T02:00:00.000001+00:00",
                "2026-07-13T02:00:00.000002+00:00",
            ),
            (
                "2026-07-13T02:00:00+00:00",
                "2026-07-13T02:00:00.000001+00:00",
            ),
            (
                "2026-07-13T02:00:00.999999+00:00",
                "2026-07-13T02:00:01+00:00",
            ),
        )
        for case_number, (assessed_at, valid_until) in enumerate(accepted):
            with self.subTest(accepted=case_number):
                repository = self.prepared_repository(f"validity-accepted-{case_number}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[13] = assessed_at
                values[14] = valid_until
                values[15] = assessed_at
                self.insert_allocation_assessment(repository, values)

        rejected = (
            (
                "2026-07-13T02:00:00+00:00",
                "2026-07-13T02:00:00+00:00",
            ),
            (
                "2026-07-13T02:00:01+00:00",
                "2026-07-13T02:00:00.999999+00:00",
            ),
            (
                "2026-07-13T02:00:00.000001+00:00",
                "2026-07-13T02:00:00+00:00",
            ),
            (
                "2026-07-13T02:00:00+00:00",
                "2026-07-13T02:00:00.000000+00:00",
            ),
            (
                "2026-07-13T02:00:00.000000+00:00",
                "2026-07-13T02:00:00+00:00",
            ),
        )
        for case_number, (assessed_at, valid_until) in enumerate(rejected):
            with self.subTest(rejected=case_number):
                repository = self.prepared_repository(f"validity-rejected-{case_number}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[13] = assessed_at
                values[14] = valid_until
                values[15] = assessed_at
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)

    def test_policy_constraints_reject_invalid_content(self) -> None:
        cases = (
            (0, ""),
            (1, "[]"),
            (1, "not-json"),
            (2, "D" * 64),
            (2, "g" * 64),
            (2, "d" * 63),
            (3, ""),
            (3, "not-a-time"),
            (4, ""),
            (4, "not-a-time"),
        )
        for case_number, (index, replacement) in enumerate(cases):
            with self.subTest(index=index, replacement=replacement):
                repository = self.repository(f"policy-constraint-{case_number}.db")
                repository.migrate()
                values = list(ALLOCATION_POLICY_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO allocation_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_assessment_constraints_reject_invalid_content(self) -> None:
        cases = (
            (0, 0),
            (1, 0),
            (2, ""),
            (3, "E" * 64),
            (3, "g" * 64),
            (3, "e" * 63),
            (4, "blocked"),
            (5, "[]"),
            (5, "not-json"),
            (6, "{}"),
            (6, "not-json"),
            (7, "[]"),
            (7, "not-json"),
            (8, ""),
            (9, "AES-128-GCM"),
            (10, ""),
            (11, ""),
            (12, "F" * 64),
            (12, "g" * 64),
            (12, "f" * 63),
            (13, ""),
            (13, "not-a-time"),
            (14, "2026-07-13T01:59:59+00:00"),
            (14, "not-a-time"),
            (15, ""),
            (15, "not-a-time"),
        )
        for case_number, (index, replacement) in enumerate(cases):
            with self.subTest(index=index, replacement=replacement):
                repository = self.prepared_repository(f"assessment-constraint-{case_number}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)

    def test_all_fields_are_required(self) -> None:
        for index in range(len(ALLOCATION_POLICY_VALUES)):
            with self.subTest(table="policy", index=index):
                repository = self.repository(f"policy-null-{index}.db")
                repository.migrate()
                values = list(ALLOCATION_POLICY_VALUES)
                values[index] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO allocation_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

        for index in range(len(ALLOCATION_ASSESSMENT_VALUES)):
            with self.subTest(table="assessment", index=index):
                repository = self.prepared_repository(f"assessment-null-{index}.db")
                values = list(ALLOCATION_ASSESSMENT_VALUES)
                values[index] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert_allocation_assessment(repository, values)


if __name__ == "__main__":
    unittest.main()
