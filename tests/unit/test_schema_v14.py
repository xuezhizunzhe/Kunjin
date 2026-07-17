from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    SCHEMA_V13,
    SCHEMA_VERSION,
)

UTC = "2026-07-16T00:00:00+00:00"
LATER = "2026-07-16T00:00:01+00:00"
DEADLINE = "2026-07-16T00:01:30+00:00"
COOLDOWN = "2026-07-16T00:31:00+00:00"
MICRO_1 = "2026-07-16T00:00:00.000001+00:00"
MICRO_2 = "2026-07-16T00:00:00.000002+00:00"
MICRO_3 = "2026-07-16T00:00:00.000003+00:00"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
POLICY_JSON = '{"version":"1"}'
REGISTRY_JSON = '{"sources":[],"version":"1"}'
ROUTE_JSON = '{"actions":[],"request_id":"0123456789abcdef0123456789abcdef"}'
MANIFEST_JSON = '{"evidence_fact_ids":[],"manifest_version":1}'

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


class SchemaV14Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def _create_at_version(self, version: int) -> Repository:
        repository = self.repository(f"v{version}.db")
        with repository.connect() as connection:
            for schema in SCHEMAS_THROUGH_V11[: min(version, 11)]:
                connection.executescript(schema)
            if version >= 12:
                _migrate_v12(connection)
            if version >= 13:
                connection.executescript(SCHEMA_V13)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(item, UTC) for item in range(1, version + 1)],
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
                ) VALUES (
                    17, '000001', '1', ?, ?, 'broad_index',
                    'diversified_equity', 'core_eligible', 'verified',
                    '[]', '[]', '[]', '[]', '[]', '[]', '[]', ?, ?, ?
                )
                """,
                ("e" * 64, MANIFEST_JSON, UTC, DEADLINE, UTC),
            )
            if version >= 13:
                connection.execute(
                    "INSERT INTO fund_document_refresh_runs(id, fund_code, started_at) "
                    "VALUES (23, '000001', ?)",
                    (UTC,),
                )
                connection.execute(
                    """
                    INSERT INTO fund_document_selection_manifests(
                        refresh_run_id, fund_code, manifest_version,
                        selection_policy_checksum, canonical_json,
                        selection_checksum, created_at
                    ) VALUES (23, '000001', 1, ?, ?, ?, ?)
                    """,
                    ("a" * 64, '{"manifest_version":1}', "b" * 64, UTC),
                )
            connection.commit()
        return repository

    def _insert_request(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str = REQUEST_ID,
        mode: str = "rapid",
        started_at: str = UTC,
        deadline_at: str = DEADLINE,
    ) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO request_runs(
                    request_id, mode, status, started_at, deadline_at,
                    finished_at, omitted_work_json
                ) VALUES (?, ?, 'running', ?, ?, NULL, '[]')
                """,
                (request_id, mode, started_at, deadline_at),
            ).lastrowid
        )

    def _attempt_values(self, request_run_id: int, **overrides: object) -> dict:
        values: dict[str, object] = {
            "request_run_id": request_run_id,
            "source_id": "eastmoney_f10",
            "field_id": "identity_active_status",
            "subject_key": "fund:123456",
            "attempt_number": 1,
            "outcome": "success",
            "started_at": UTC,
            "finished_at": LATER,
            "data_as_of": UTC,
            "error_code": None,
            "cooldown_until": None,
            "force_actor": None,
            "force_reason": None,
            "registry_version": "1",
            "registry_checksum": "a" * 64,
            "response_byte_count": 100,
        }
        values.update(overrides)
        return values

    def _insert_attempt(self, connection: sqlite3.Connection, values: dict) -> int:
        columns = tuple(values)
        return int(
            connection.execute(
                f"INSERT INTO source_attempts({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            ).lastrowid
        )

    def _insert_snapshot(self, connection: sqlite3.Connection, request_run_id: int) -> int:
        return int(
            connection.execute(
                """
                INSERT INTO decision_snapshots(
                    request_run_id, evidence_policy_version,
                    evidence_policy_json, evidence_policy_checksum,
                    source_registry_version, source_registry_json,
                    source_registry_checksum, canonical_route_json,
                    result_checksum, created_at
                ) VALUES (?, '1', ?, ?, '1', ?, ?, ?, ?, ?)
                """,
                (
                    request_run_id,
                    POLICY_JSON,
                    "b" * 64,
                    REGISTRY_JSON,
                    "c" * 64,
                    ROUTE_JSON,
                    "d" * 64,
                    UTC,
                ),
            ).lastrowid
        )

    def test_v10_through_v13_and_fresh_migrate_additively(self) -> None:
        for version in (10, 11, 12, 13):
            with self.subTest(version=version):
                repository = self._create_at_version(version)
                with repository.connect() as connection:
                    classification_before = connection.execute(
                        "SELECT id, CAST(input_manifest_json AS BLOB) AS manifest "
                        "FROM fund_risk_classifications"
                    ).fetchone()
                    selection_before = (
                        connection.execute(
                            "SELECT refresh_run_id, CAST(canonical_json AS BLOB) AS manifest "
                            "FROM fund_document_selection_manifests"
                        ).fetchone()
                        if version >= 13
                        else None
                    )

                repository.migrate()

                with repository.connect() as connection:
                    versions = tuple(
                        row["version"]
                        for row in connection.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    )
                    classification_after = connection.execute(
                        "SELECT id, CAST(input_manifest_json AS BLOB) AS manifest "
                        "FROM fund_risk_classifications"
                    ).fetchone()
                    selection_after = (
                        connection.execute(
                            "SELECT refresh_run_id, CAST(canonical_json AS BLOB) AS manifest "
                            "FROM fund_document_selection_manifests"
                        ).fetchone()
                        if version >= 13
                        else None
                    )
                self.assertEqual(versions, tuple(range(1, 19)))
                self.assertEqual(tuple(classification_after), tuple(classification_before))
                if selection_before is not None:
                    self.assertEqual(tuple(selection_after), tuple(selection_before))

        fresh = self.repository("fresh.db")
        fresh.migrate()
        with fresh.connect() as connection:
            versions = tuple(
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
        self.assertEqual(SCHEMA_VERSION, 18)
        self.assertEqual(versions, tuple(range(1, 19)))

    def test_failed_v14_migration_rolls_back_objects_marker_and_prior_bytes(self) -> None:
        repository = self._create_at_version(13)
        with repository.connect() as connection:
            classification_before = tuple(
                connection.execute(
                    "SELECT id, CAST(input_manifest_json AS BLOB) "
                    "FROM fund_risk_classifications"
                ).fetchone()
            )
            selection_before = tuple(
                connection.execute(
                    "SELECT refresh_run_id, CAST(canonical_json AS BLOB) "
                    "FROM fund_document_selection_manifests"
                ).fetchone()
            )

        broken_v14 = """
        CREATE TABLE partial_request_runs(id INTEGER PRIMARY KEY);
        CREATE TABLE incomplete_v14(
        """
        with patch("kunjin.storage.repository.SCHEMA_V14", broken_v14):
            with self.assertRaises(sqlite3.OperationalError):
                repository.migrate()

        with repository.connect() as connection:
            versions = tuple(
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            classification_after = tuple(
                connection.execute(
                    "SELECT id, CAST(input_manifest_json AS BLOB) "
                    "FROM fund_risk_classifications"
                ).fetchone()
            )
            selection_after = tuple(
                connection.execute(
                    "SELECT refresh_run_id, CAST(canonical_json AS BLOB) "
                    "FROM fund_document_selection_manifests"
                ).fetchone()
            )
        self.assertEqual(versions, tuple(range(1, 14)))
        self.assertNotIn("partial_request_runs", tables)
        self.assertNotIn("request_runs", tables)
        self.assertEqual(classification_after, classification_before)
        self.assertEqual(selection_after, selection_before)

    def test_extra_decision_audit_objects_are_rejected_even_with_indirect_binding(self) -> None:
        hostile_scripts = (
            """
            CREATE TRIGGER injected_request_ignore
            BEFORE INSERT ON request_runs
            BEGIN
                SELECT RAISE(IGNORE);
            END;
            """,
            "CREATE INDEX injected_attempt_index ON source_attempts(outcome);",
            "CREATE TABLE decision_snapshots_shadow(id INTEGER PRIMARY KEY);",
            """
            CREATE TRIGGER unrelated_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM decision_snapshots;
            END;
            """,
            """
            CREATE TRIGGER mixed_case_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM ReQuEsT_RuNs;
            END;
            """,
            """
            CREATE TRIGGER quoted_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM "request_runs";
            END;
            """,
            """
            CREATE TRIGGER bracketed_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM [source_attempts];
            END;
            """,
            """
            CREATE TRIGGER backtick_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM `decision_snapshots`;
            END;
            """,
            """
            CREATE TRIGGER single_quoted_audit_writer
            AFTER INSERT ON sync_runs
            BEGIN
                INSERT INTO 'request_runs'(
                    request_id, mode, status, started_at, deadline_at,
                    finished_at, omitted_work_json
                ) VALUES (
                    'ffffffffffffffffffffffffffffffff', 'rapid', 'running',
                    '2026-07-16T00:00:00+00:00',
                    '2026-07-16T00:01:30+00:00', NULL, '[]'
                );
            END;
            """,
            """
            CREATE TRIGGER single_quoted_audit_reader
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT count(*) FROM 'source_attempts';
            END;
            """,
            """
            CREATE TRIGGER single_quoted_audit_updater
            AFTER INSERT ON sync_runs
            BEGIN
                UPDATE 'source_attempts'
                SET response_byte_count = response_byte_count
                WHERE 0;
            END;
            """,
            """
            CREATE TRIGGER update_only_audit_reader
            AFTER UPDATE OF source ON sync_runs
            BEGIN
                SELECT count(*) FROM request_runs;
            END;
            """,
            """
            CREATE TRIGGER delete_only_audit_reader
            AFTER DELETE ON sync_runs
            BEGIN
                SELECT count(*) FROM decision_snapshots;
            END;
            """,
            "CREATE VIEW single_quoted_audit_view AS SELECT * FROM 'decision_snapshots';",
            """
            CREATE TABLE single_quoted_audit_fk(
                id INTEGER REFERENCES 'request_runs'(id)
            );
            """,
        )
        for index, hostile_script in enumerate(hostile_scripts):
            with self.subTest(index=index):
                repository = self.repository(f"hostile-{index}.db")
                repository.migrate()
                with repository.connect() as connection, connection:
                    connection.executescript(hostile_script)

                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "decision audit schema does not match V15",
                ):
                    repository.migrate()

    def test_similar_non_audit_identifiers_are_not_owned_by_v14(self) -> None:
        repository = self.repository("similar-identifiers.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.executescript(
                """
                CREATE TABLE unrelated_metrics(
                    request_runs_total INTEGER,
                    request_runs_backup INTEGER,
                    source_attempts_total INTEGER,
                    decision_snapshots_backup INTEGER
                );
                CREATE TABLE request_runtime_metrics(id INTEGER PRIMARY KEY);
                CREATE TABLE "request""_runs"(id INTEGER PRIMARY KEY);
                CREATE TABLE `source``_attempts`(id INTEGER PRIMARY KEY);
                CREATE INDEX unrelated_request_runs_total
                ON unrelated_metrics(request_runs_total);
                CREATE TRIGGER unrelated_metrics_copy
                AFTER INSERT ON sync_runs
                BEGIN
                    INSERT INTO unrelated_metrics(request_runs_backup)
                    VALUES (NEW.id);
                END;
                """
            )

        repository.migrate()

    def test_double_quoted_default_text_does_not_bind_audit_tables(self) -> None:
        repository = self.repository("double-quoted-default.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute(
                'CREATE TABLE dqs_defaults(value TEXT DEFAULT "request_runs")'
            )

        repository.migrate()

    def test_unexpected_fts_external_content_table_is_rejected(self) -> None:
        repository = self.repository("fts-external-content.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            try:
                connection.execute(
                    """
                    CREATE VIRTUAL TABLE hidden_audit_fts USING fts5(
                        request_id,
                        content='request_runs',
                        content_rowid='id'
                    )
                    """
                )
            except sqlite3.OperationalError as exc:
                if "no such module: fts5" in str(exc).casefold():
                    self.skipTest("SQLite build does not expose FTS5")
                raise
            connection.execute(
                """
                CREATE TRIGGER hidden_audit_fts_sync
                AFTER INSERT ON sync_runs
                BEGIN
                    INSERT INTO hidden_audit_fts(rowid, request_id)
                    VALUES (NEW.id, NEW.source);
                END
                """
            )

        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "decision audit schema does not match V15",
        ):
            repository.migrate()

    def test_string_literals_and_comments_do_not_bind_audit_tables(self) -> None:
        repository = self.repository("literal-and-comment-identifiers.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.executescript(
                """
                CREATE TABLE narrative_metrics(
                    id INTEGER PRIMARY KEY,
                    label TEXT DEFAULT 'request_runs',
                    escaped TEXT DEFAULT 'source_attempts'' decision_snapshots',
                    -- request_runs is prose, not an identifier
                    value INTEGER /* source_attempts and decision_snapshots */
                );
                CREATE TRIGGER narrative_metrics_note
                AFTER INSERT ON sync_runs
                BEGIN
                    SELECT 'request_runs';
                    SELECT 'source_attempts'' decision_snapshots';
                    -- request_runs remains prose here
                    SELECT 1 /* source_attempts and decision_snapshots */;
                END;
                CREATE VIEW narrative_literal_view AS
                SELECT 'decision_snapshots' AS label;
                """
            )

        repository.migrate()

    def test_exact_singular_audit_object_roots_are_rejected(self) -> None:
        hostile_scripts = (
            "CREATE TABLE request_run(id INTEGER PRIMARY KEY);",
            """
            CREATE TABLE singular_index_target(id INTEGER PRIMARY KEY);
            CREATE INDEX source_attempt ON singular_index_target(id);
            """,
            """
            CREATE TRIGGER decision_snapshot
            AFTER INSERT ON sync_runs
            BEGIN
                SELECT 1;
            END;
            """,
        )
        for index, hostile_script in enumerate(hostile_scripts):
            with self.subTest(index=index):
                repository = self.repository(f"singular-root-{index}.db")
                repository.migrate()
                with repository.connect() as connection, connection:
                    connection.executescript(hostile_script)

                with self.assertRaisesRegex(
                    sqlite3.DatabaseError,
                    "decision audit schema does not match V15",
                ):
                    repository.migrate()

    def test_failed_trigger_probe_observations_do_not_count_as_success(self) -> None:
        repository = self.repository("broken-trigger-probe.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER broken_insert_probe
                AFTER INSERT ON sync_runs
                BEGIN
                    SELECT * FROM missing_dependency;
                END
                """
            )

        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "decision audit schema does not match V15",
        ):
            repository.migrate()

    def test_v13_preseeded_audit_trigger_rolls_back_v14_upgrade(self) -> None:
        repository = self._create_at_version(13)
        with repository.connect() as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER preseeded_audit_writer
                AFTER INSERT ON sync_runs
                BEGIN
                    INSERT INTO 'request_runs'(
                        request_id, mode, status, started_at, deadline_at,
                        finished_at, omitted_work_json
                    ) VALUES (
                        'ffffffffffffffffffffffffffffffff', 'rapid', 'running',
                        '2026-07-16T00:00:00+00:00',
                        '2026-07-16T00:01:30+00:00', NULL, '[]'
                    );
                END
                """
            )

        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "decision audit schema does not match V15",
        ):
            repository.migrate()

        with repository.connect() as connection:
            versions = tuple(
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertEqual(versions, tuple(range(1, 14)))
        self.assertNotIn("request_runs", tables)

    def test_update_of_implicit_rowid_alias_triggers_are_probeable(self) -> None:
        repository = self.repository("rowid-triggers.db")
        repository.migrate()
        with repository.connect() as connection, connection:
            for index, alias in enumerate(("rowid", "_rowid_", "oid")):
                connection.execute(
                    f"""
                    CREATE TRIGGER benign_rowid_trigger_{index}
                    AFTER UPDATE OF {alias} ON sync_runs
                    BEGIN
                        SELECT 1;
                    END
                    """
                )

        repository.migrate()

    def test_current_audit_schema_has_exact_columns_foreign_keys_indexes_and_triggers(self) -> None:
        repository = self._create_at_version(13)
        with repository.connect() as connection:
            tables_before = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        repository.migrate()
        with repository.connect() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            columns = {
                table: tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                )
                for table in ("request_runs", "source_attempts", "decision_snapshots")
            }
            foreign_keys = {
                table: {
                    (row["from"], row["table"], row["to"], row["on_delete"])
                    for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                }
                for table in ("source_attempts", "decision_snapshots")
            }
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            triggers = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                )
            }

        self.assertEqual(
            tables - tables_before,
            {
                "request_runs",
                "source_attempts",
                "source_work_authorizations",
                "decision_snapshots",
                "brief_policy_versions",
                "fund_brief_snapshots",
                "portfolio_observation_snapshots",
                "portfolio_observation_accounts",
            },
        )
        self.assertEqual(
            columns["request_runs"],
            (
                "id",
                "request_id",
                "mode",
                "status",
                "started_at",
                "deadline_at",
                "finished_at",
                "omitted_work_json",
            ),
        )
        self.assertEqual(
            columns["source_attempts"],
            (
                "id",
                "request_run_id",
                "source_id",
                "field_id",
                "subject_key",
                "attempt_number",
                "outcome",
                "started_at",
                "finished_at",
                "data_as_of",
                "error_code",
                "cooldown_until",
                "force_actor",
                "force_reason",
                "registry_version",
                "registry_checksum",
                "response_byte_count",
                "authorization_id",
            ),
        )
        self.assertEqual(
            columns["decision_snapshots"],
            (
                "id",
                "request_run_id",
                "evidence_policy_version",
                "evidence_policy_json",
                "evidence_policy_checksum",
                "source_registry_version",
                "source_registry_json",
                "source_registry_checksum",
                "canonical_route_json",
                "result_checksum",
                "created_at",
            ),
        )
        self.assertEqual(
            foreign_keys["source_attempts"],
            {
                ("request_run_id", "request_runs", "id", "RESTRICT"),
                (
                    "authorization_id",
                    "source_work_authorizations",
                    "id",
                    "RESTRICT",
                ),
            },
        )
        self.assertEqual(
            foreign_keys["decision_snapshots"],
            {("request_run_id", "request_runs", "id", "RESTRICT")},
        )
        self.assertTrue(
            {
                "source_attempts_request",
                "source_attempts_history",
                "fund_brief_snapshots_history",
            }
            <= indexes
        )
        self.assertTrue(
            {
                "request_run_no_replace",
                "request_run_insert_guard",
                "request_run_update_guard",
                "request_run_no_delete",
                "source_attempt_no_replace",
                "source_attempt_no_update",
                "source_attempt_no_delete",
                "decision_snapshot_no_replace",
                "decision_snapshot_no_update",
                "decision_snapshot_no_delete",
                "brief_policy_no_replace",
                "brief_policy_no_update",
                "brief_policy_no_delete",
                "fund_brief_snapshot_insert_guard",
                "fund_brief_snapshot_private_key_guard",
                "fund_brief_snapshot_array_guard",
                "fund_brief_snapshot_duplicate_guard",
                "fund_brief_snapshot_no_replace",
                "fund_brief_snapshot_no_update",
                "fund_brief_snapshot_no_delete",
            }
            <= triggers
        )

    def test_request_run_allows_one_finalize_and_rejects_all_other_mutation(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            run_id = self._insert_request(connection)
            connection.execute(
                "UPDATE request_runs SET status = 'partial', finished_at = ?, "
                "omitted_work_json = '[\"market_context\"]' WHERE id = ?",
                (LATER, run_id),
            )
            for statement in (
                "UPDATE request_runs SET status = 'complete' WHERE id = ?",
                "UPDATE request_runs SET mode = 'deep' WHERE id = ?",
                "DELETE FROM request_runs WHERE id = ?",
                "INSERT OR REPLACE INTO request_runs "
                "VALUES (?, ?, 'rapid', 'running', ?, ?, NULL, '[]')",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    parameters = (
                        (run_id, REQUEST_ID, UTC, DEADLINE)
                        if statement.startswith("INSERT")
                        else (run_id,)
                    )
                    connection.execute(statement, parameters)

        invalid_rows = (
            (REQUEST_ID.upper(), "rapid", "running", UTC, DEADLINE, None, "[]"),
            (REQUEST_ID, "slow", "running", UTC, DEADLINE, None, "[]"),
            (REQUEST_ID, "rapid", "complete", UTC, DEADLINE, None, "[]"),
            (REQUEST_ID, "rapid", "complete", UTC, DEADLINE, LATER, "[]"),
            (REQUEST_ID, "rapid", "running", UTC, DEADLINE, LATER, "[]"),
            (REQUEST_ID, "rapid", "running", UTC, DEADLINE, None, '["pending"]'),
            (REQUEST_ID, "rapid", "running", UTC, DEADLINE, None, "{}"),
            (REQUEST_ID, "rapid", "running", "not-utc", DEADLINE, None, "[]"),
        )
        for index, values in enumerate(invalid_rows):
            with self.subTest(values=values), repository.connect() as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO request_runs(
                            request_id, mode, status, started_at, deadline_at,
                            finished_at, omitted_work_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )

    def test_request_times_preserve_exact_microsecond_order(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            self._insert_request(
                connection,
                request_id="1" * 32,
                started_at=MICRO_1,
                deadline_at=MICRO_2,
            )
            for request_id, started_at, deadline_at in (
                ("2" * 32, MICRO_2, MICRO_1),
                ("3" * 32, MICRO_1, MICRO_1),
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert_request(
                        connection,
                        request_id=request_id,
                        started_at=started_at,
                        deadline_at=deadline_at,
                    )

            inverse_finish = self._insert_request(
                connection,
                request_id="4" * 32,
                started_at=MICRO_2,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE request_runs SET status = 'failed', finished_at = ? WHERE id = ?",
                    (MICRO_1, inverse_finish),
                )

            equal_finish = self._insert_request(
                connection,
                request_id="5" * 32,
                started_at=MICRO_1,
            )
            connection.execute(
                "UPDATE request_runs SET status = 'complete', finished_at = ? WHERE id = ?",
                (MICRO_1, equal_finish),
            )

    def test_attempt_finish_and_data_times_preserve_exact_microsecond_order(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            inverse_finish_run = self._insert_request(connection, request_id="6" * 32)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_attempt(
                    connection,
                    self._attempt_values(
                        inverse_finish_run,
                        started_at=MICRO_2,
                        finished_at=MICRO_1,
                        data_as_of=MICRO_1,
                    ),
                )

            equal_finish_run = self._insert_request(connection, request_id="7" * 32)
            self._insert_attempt(
                connection,
                self._attempt_values(
                    equal_finish_run,
                    started_at=MICRO_1,
                    finished_at=MICRO_1,
                    data_as_of=MICRO_1,
                ),
            )

            future_data_run = self._insert_request(connection, request_id="8" * 32)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_attempt(
                    connection,
                    self._attempt_values(
                        future_data_run,
                        started_at=MICRO_1,
                        finished_at=MICRO_2,
                        data_as_of=MICRO_3,
                    ),
                )

            equal_data_run = self._insert_request(connection, request_id="9" * 32)
            self._insert_attempt(
                connection,
                self._attempt_values(
                    equal_data_run,
                    started_at=MICRO_1,
                    finished_at=MICRO_2,
                    data_as_of=MICRO_2,
                ),
            )

    def test_attempt_cooldown_preserves_exact_microsecond_order(self) -> None:
        repository = self.repository()
        repository.migrate()
        cases = (
            ("transient_failure", "network_timeout", ("a", "b", "c")),
            ("skipped_cooldown", "cooldown_active", ("d", "e", "f")),
        )
        for outcome, error_code, request_prefixes in cases:
            with self.subTest(outcome=outcome), repository.connect() as connection, connection:
                forward_run = self._insert_request(
                    connection,
                    request_id=request_prefixes[0] * 32,
                )
                self._insert_attempt(
                    connection,
                    self._attempt_values(
                        forward_run,
                        outcome=outcome,
                        started_at=MICRO_1,
                        finished_at=MICRO_2,
                        data_as_of=None,
                        error_code=error_code,
                        cooldown_until=MICRO_3,
                        response_byte_count=0,
                    ),
                )

                for request_prefix, cooldown_until in zip(
                    request_prefixes[1:],
                    (MICRO_2, MICRO_1),
                ):
                    run_id = self._insert_request(
                        connection,
                        request_id=request_prefix * 32,
                    )
                    with self.assertRaises(sqlite3.IntegrityError):
                        self._insert_attempt(
                            connection,
                            self._attempt_values(
                                run_id,
                                outcome=outcome,
                                started_at=MICRO_1,
                                finished_at=MICRO_2,
                                data_as_of=None,
                                error_code=error_code,
                                cooldown_until=cooldown_until,
                                response_byte_count=0,
                            ),
                        )

    def test_source_attempt_matrix_owner_uniqueness_and_immutability(self) -> None:
        repository = self.repository()
        repository.migrate()
        valid_overrides = (
            {},
            {"outcome": "cache_hit"},
            {
                "outcome": "transient_failure",
                "data_as_of": None,
                "error_code": "network_timeout",
                "cooldown_until": COOLDOWN,
                "response_byte_count": 0,
            },
            {
                "outcome": "unavailable",
                "data_as_of": None,
                "error_code": "source_unavailable",
                "response_byte_count": 0,
            },
            {
                "outcome": "unsupported",
                "data_as_of": None,
                "error_code": "field_unsupported",
                "response_byte_count": 0,
            },
            {
                "outcome": "cancelled",
                "data_as_of": None,
                "error_code": "request_cancelled",
                "response_byte_count": 0,
            },
            {
                "outcome": "expired",
                "data_as_of": None,
                "error_code": "request_expired",
                "response_byte_count": 0,
            },
            {
                "outcome": "skipped_cooldown",
                "data_as_of": None,
                "error_code": "cooldown_active",
                "cooldown_until": COOLDOWN,
                "response_byte_count": 0,
            },
        )
        for index, overrides in enumerate(valid_overrides):
            with self.subTest(overrides=overrides), repository.connect() as connection:
                run_id = self._insert_request(
                    connection,
                    request_id=f"{index + 1:032x}",
                )
                attempt_id = self._insert_attempt(
                    connection, self._attempt_values(run_id, **overrides)
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE source_attempts SET response_byte_count = 0 WHERE id = ?",
                        (attempt_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM source_attempts WHERE id = ?", (attempt_id,))
                connection.commit()

        invalid_overrides = (
            {"subject_key": "fund:12345"},
            {"attempt_number": 3},
            {"outcome": "failure"},
            {"finished_at": UTC, "started_at": LATER},
            {"data_as_of": DEADLINE},
            {"response_byte_count": -1},
            {"registry_checksum": "A" * 64},
            {"outcome": "success", "data_as_of": None},
            {
                "outcome": "transient_failure",
                "data_as_of": None,
                "error_code": "field_unsupported",
                "cooldown_until": COOLDOWN,
            },
            {
                "outcome": "skipped_cooldown",
                "data_as_of": None,
                "error_code": "cooldown_active",
                "cooldown_until": COOLDOWN,
                "force_actor": "local_owner",
                "force_reason": "verify_source_recovery",
            },
            {"force_actor": "other_owner", "force_reason": "owner_approved_retry"},
            {"force_actor": "local_owner", "force_reason": None},
            {"force_actor": "local_owner", "force_reason": "owner_approved_retry"},
            {"attempt_number": 2},
        )
        for index, overrides in enumerate(invalid_overrides, start=20):
            with self.subTest(overrides=overrides), repository.connect() as connection:
                run_id = self._insert_request(connection, request_id=f"{index:032x}")
                with self.assertRaises(sqlite3.IntegrityError):
                    self._insert_attempt(connection, self._attempt_values(run_id, **overrides))

        with repository.connect() as connection, connection:
            run_id = self._insert_request(connection, request_id="f" * 32)
            values = self._attempt_values(run_id)
            attempt_id = self._insert_attempt(connection, values)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_attempt(connection, values)
            with self.assertRaises(sqlite3.IntegrityError):
                columns = tuple(values)
                connection.execute(
                    f"INSERT OR REPLACE INTO source_attempts(id,{','.join(columns)}) "
                    f"VALUES (?,{','.join('?' for _ in columns)})",
                    (attempt_id, *(values[column] for column in columns)),
                )

    def test_snapshots_are_bound_complete_json_objects_and_immutable(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            run_id = self._insert_request(connection)
            snapshot_id = self._insert_snapshot(connection, run_id)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_snapshot(connection, run_id)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT OR REPLACE INTO decision_snapshots(
                        id, request_run_id, evidence_policy_version,
                        evidence_policy_json, evidence_policy_checksum,
                        source_registry_version, source_registry_json,
                        source_registry_checksum, canonical_route_json,
                        result_checksum, created_at
                    ) VALUES (?, ?, '1', ?, ?, '1', ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        POLICY_JSON,
                        "b" * 64,
                        REGISTRY_JSON,
                        "c" * 64,
                        ROUTE_JSON,
                        "d" * 64,
                        UTC,
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE decision_snapshots SET canonical_route_json = '{}' WHERE id = ?",
                    (snapshot_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM decision_snapshots WHERE id = ?", (snapshot_id,))

        invalid_values = (
            ("[]", "b" * 64, REGISTRY_JSON, "c" * 64, ROUTE_JSON, "d" * 64, UTC),
            (POLICY_JSON, "B" * 64, REGISTRY_JSON, "c" * 64, ROUTE_JSON, "d" * 64, UTC),
            (POLICY_JSON, "b" * 64, "{", "c" * 64, ROUTE_JSON, "d" * 64, UTC),
            (POLICY_JSON, "b" * 64, REGISTRY_JSON, "c" * 64, "[]", "d" * 64, UTC),
            (POLICY_JSON, "b" * 64, REGISTRY_JSON, "c" * 64, ROUTE_JSON, "D" * 64, UTC),
            (POLICY_JSON, "b" * 64, REGISTRY_JSON, "c" * 64, ROUTE_JSON, "d" * 64, "bad"),
        )
        for index, values in enumerate(invalid_values, start=1):
            with self.subTest(values=values), repository.connect() as connection:
                run_id = self._insert_request(connection, request_id=f"{index + 40:032x}")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        """
                        INSERT INTO decision_snapshots(
                            request_run_id, evidence_policy_version,
                            evidence_policy_json, evidence_policy_checksum,
                            source_registry_version, source_registry_json,
                            source_registry_checksum, canonical_route_json,
                            result_checksum, created_at
                        ) VALUES (?, '1', ?, ?, '1', ?, ?, ?, ?, ?)
                        """,
                        (run_id, *values),
                    )

        with repository.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_snapshot(connection, 999999)


if __name__ == "__main__":
    unittest.main()
