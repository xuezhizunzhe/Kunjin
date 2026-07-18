from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kunjin.storage.repository import Repository, _execute_schema, _migrate_v12
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
    SCHEMA_V14,
    SCHEMA_VERSION,
)

UTC = "2026-07-16T00:00:00+00:00"
LATER = "2026-07-16T00:00:01+00:00"
DEADLINE = "2026-07-16T00:01:30+00:00"
REQUEST_ID = "0123456789abcdef0123456789abcdef"
SOURCE_ID = "eastmoney_nav"
FIELD_ID = "formal_nav"
SUBJECT_KEY = "fund:123456"
REGISTRY_VERSION = "1"
REGISTRY_CHECKSUM = "a" * 64

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


class SchemaV15Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def _create_v14(self) -> Repository:
        repository = self.repository("v14.db")
        with repository.connect() as connection, connection:
            for schema in SCHEMAS_THROUGH_V11:
                _execute_schema(connection, schema)
            _migrate_v12(connection)
            _execute_schema(connection, SCHEMA_V13)
            _execute_schema(connection, SCHEMA_V14)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, UTC) for version in range(1, 15)],
            )
        return repository

    @staticmethod
    def _insert_run(
        connection: sqlite3.Connection,
        *,
        request_id: str = REQUEST_ID,
        mode: str = "deep",
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

    @staticmethod
    def _insert_parent_attempt(
        connection: sqlite3.Connection,
        request_run_id: int,
        **overrides: object,
    ) -> int:
        values: dict[str, object] = {
            "request_run_id": request_run_id,
            "source_id": SOURCE_ID,
            "field_id": FIELD_ID,
            "subject_key": SUBJECT_KEY,
            "attempt_number": 1,
            "outcome": "transient_failure",
            "started_at": UTC,
            "finished_at": LATER,
            "data_as_of": None,
            "error_code": "network_timeout",
            "cooldown_until": "2026-07-16T00:31:00+00:00",
            "force_actor": None,
            "force_reason": None,
            "registry_version": REGISTRY_VERSION,
            "registry_checksum": REGISTRY_CHECKSUM,
            "response_byte_count": 0,
        }
        values.update(overrides)
        columns = tuple(values)
        return int(
            connection.execute(
                f"INSERT INTO source_attempts({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            ).lastrowid
        )

    @staticmethod
    def _authorization_values(
        request_run_id: int,
        *,
        kind: str = "force",
        parent_attempt_id: int | None = None,
        **overrides: object,
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "request_run_id": request_run_id,
            "kind": kind,
            "parent_attempt_id": parent_attempt_id,
            "source_id": SOURCE_ID,
            "field_id": FIELD_ID,
            "subject_key": SUBJECT_KEY,
            "actor": "local_owner" if kind == "force" else None,
            "reason": "owner_approved_retry" if kind == "force" else None,
            "reserved_at": LATER,
            "deadline_at": DEADLINE,
            "registry_version": REGISTRY_VERSION,
            "registry_checksum": REGISTRY_CHECKSUM,
        }
        values.update(overrides)
        return values

    @staticmethod
    def _insert_authorization(
        connection: sqlite3.Connection,
        values: dict[str, object],
    ) -> int:
        columns = tuple(values)
        return int(
            connection.execute(
                f"INSERT INTO source_work_authorizations({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            ).lastrowid
        )

    @staticmethod
    def _insert_consuming_attempt(
        connection: sqlite3.Connection,
        request_run_id: int,
        authorization_id: int,
        *,
        kind: str,
        **overrides: object,
    ) -> int:
        force = kind == "force"
        values: dict[str, object] = {
            "request_run_id": request_run_id,
            "source_id": SOURCE_ID,
            "field_id": FIELD_ID,
            "subject_key": SUBJECT_KEY,
            "attempt_number": 1 if force else 2,
            "outcome": "success",
            "started_at": LATER,
            "finished_at": "2026-07-16T00:00:02+00:00",
            "data_as_of": LATER,
            "error_code": None,
            "cooldown_until": None,
            "force_actor": "local_owner" if force else None,
            "force_reason": "owner_approved_retry" if force else None,
            "registry_version": REGISTRY_VERSION,
            "registry_checksum": REGISTRY_CHECKSUM,
            "response_byte_count": 100,
            "authorization_id": authorization_id,
        }
        values.update(overrides)
        columns = tuple(values)
        return int(
            connection.execute(
                f"INSERT INTO source_attempts({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            ).lastrowid
        )

    def test_v14_to_v15_is_additive_and_has_exact_owned_contract(self) -> None:
        repository = self._create_v14()
        with repository.connect() as connection:
            tables_before = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            connection.execute(
                "INSERT INTO request_runs(request_id, mode, status, started_at, "
                "deadline_at, finished_at, omitted_work_json) "
                "VALUES (?, 'rapid', 'running', ?, ?, NULL, '[]')",
                (REQUEST_ID, UTC, DEADLINE),
            )
            before = connection.execute(
                "SELECT CAST(request_id AS BLOB), CAST(omitted_work_json AS BLOB) "
                "FROM request_runs"
            ).fetchone()
            connection.commit()

        repository.migrate()

        with repository.connect() as connection:
            versions = tuple(
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            )
            tables_after = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            authorization_columns = tuple(
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(source_work_authorizations)"
                )
            )
            attempt_columns = tuple(
                row["name"] for row in connection.execute("PRAGMA table_info(source_attempts)")
            )
            authorization_foreign_keys = {
                (row["from"], row["table"], row["to"], row["on_delete"])
                for row in connection.execute(
                    "PRAGMA foreign_key_list(source_work_authorizations)"
                )
            }
            attempt_foreign_keys = {
                (row["from"], row["table"], row["to"], row["on_delete"])
                for row in connection.execute("PRAGMA foreign_key_list(source_attempts)")
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
            after = connection.execute(
                "SELECT CAST(request_id AS BLOB), CAST(omitted_work_json AS BLOB) "
                "FROM request_runs"
            ).fetchone()

        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(versions, tuple(range(1, 21)))
        self.assertEqual(
            tables_after - tables_before,
            {
                "source_work_authorizations",
                "brief_policy_versions",
                "fund_brief_snapshots",
                "portfolio_observation_snapshots",
                "portfolio_observation_accounts",
                "intelligence_policy_versions",
                "market_entities",
                "entity_aliases",
                "intelligence_news_items",
                "intelligence_news_excerpts",
                "intelligence_item_integrity_events",
                "intelligence_lineage_edges",
                "intelligence_events",
                "intelligence_event_items",
                "intelligence_event_entities",
                "market_dimension_observations",
                "market_state_snapshots",
                "intelligence_snapshots",
                "intelligence_snapshot_item_uses",
            },
        )
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(
            authorization_columns,
            (
                "id",
                "request_run_id",
                "kind",
                "parent_attempt_id",
                "source_id",
                "field_id",
                "subject_key",
                "actor",
                "reason",
                "reserved_at",
                "deadline_at",
                "registry_version",
                "registry_checksum",
            ),
        )
        self.assertEqual(attempt_columns[-1], "authorization_id")
        self.assertEqual(
            authorization_foreign_keys,
            {
                ("request_run_id", "request_runs", "id", "RESTRICT"),
                ("parent_attempt_id", "source_attempts", "id", "RESTRICT"),
            },
        )
        self.assertIn(
            ("authorization_id", "source_work_authorizations", "id", "RESTRICT"),
            attempt_foreign_keys,
        )
        self.assertIn("source_attempts_authorization_consumed", indexes)
        self.assertIn("fund_brief_snapshots_history", indexes)
        self.assertTrue(
            {
                "source_work_authorization_insert_guard",
                "source_work_authorization_no_replace",
                "source_work_authorization_no_update",
                "source_work_authorization_no_delete",
                "source_attempt_authorization_guard",
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

    def test_failed_v15_upgrade_rolls_back_all_schema_changes(self) -> None:
        repository = self._create_v14()
        broken_v15 = """
        CREATE TABLE source_work_authorizations(id INTEGER PRIMARY KEY);
        ALTER TABLE source_attempts ADD COLUMN authorization_id INTEGER;
        CREATE TABLE partial_v15(id INTEGER PRIMARY KEY);
        THIS IS NOT SQL;
        """

        with patch("kunjin.storage.repository.SCHEMA_V15", broken_v15):
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
            attempt_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(source_attempts)")
            }

        self.assertEqual(versions, tuple(range(1, 15)))
        self.assertNotIn("source_work_authorizations", tables)
        self.assertNotIn("partial_v15", tables)
        self.assertNotIn("authorization_id", attempt_columns)

    def test_force_and_retry_authorizations_are_valid_and_single_use(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            force_run = self._insert_run(connection, request_id="1" * 32)
            force_id = self._insert_authorization(
                connection,
                self._authorization_values(force_run),
            )
            self._insert_consuming_attempt(
                connection,
                force_run,
                force_id,
                kind="force",
            )
            retry_run = self._insert_run(connection, request_id="2" * 32, mode="rapid")
            parent_id = self._insert_parent_attempt(connection, retry_run)
            retry_id = self._insert_authorization(
                connection,
                self._authorization_values(
                    retry_run,
                    kind="retry",
                    parent_attempt_id=parent_id,
                ),
            )
            self._insert_consuming_attempt(
                connection,
                retry_run,
                retry_id,
                kind="retry",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_consuming_attempt(
                    connection,
                    retry_run,
                    retry_id,
                    kind="retry",
                    subject_key="fund:654321",
                )

    def test_direct_sql_cannot_reserve_unbound_authorization(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            deep_run = self._insert_run(connection, request_id="3" * 32)
            rapid_run = self._insert_run(connection, request_id="4" * 32, mode="rapid")
            foreign_parent = self._insert_parent_attempt(connection, rapid_run)
            local_parent = self._insert_parent_attempt(
                connection,
                deep_run,
                subject_key="fund:654321",
            )
            invalid = (
                self._authorization_values(rapid_run),
                self._authorization_values(deep_run, parent_attempt_id=foreign_parent),
                self._authorization_values(deep_run, reserved_at=DEADLINE, deadline_at=LATER),
                self._authorization_values(
                    deep_run,
                    kind="retry",
                    parent_attempt_id=None,
                ),
                self._authorization_values(
                    deep_run,
                    kind="retry",
                    parent_attempt_id=foreign_parent,
                ),
                self._authorization_values(
                    deep_run,
                    kind="retry",
                    parent_attempt_id=local_parent,
                ),
            )
            for values in invalid:
                with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                    self._insert_authorization(connection, values)

    def test_authorizations_are_append_only_and_attempt_binding_is_fail_closed(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            run_id = self._insert_run(connection, request_id="5" * 32)
            authorization_id = self._insert_authorization(
                connection,
                self._authorization_values(run_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE source_work_authorizations SET reason = reason WHERE id = ?",
                    (authorization_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM source_work_authorizations WHERE id = ?",
                    (authorization_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_authorization(
                    connection,
                    self._authorization_values(run_id),
                )
            for overrides in (
                {"request_run_id": self._insert_run(connection, request_id="6" * 32)},
                {"source_id": "eastmoney_f10"},
                {"registry_checksum": "b" * 64},
                {"started_at": UTC},
                {"attempt_number": 2, "force_actor": None, "force_reason": None},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    attempt_run_id = int(overrides.pop("request_run_id", run_id))
                    self._insert_consuming_attempt(
                        connection,
                        attempt_run_id,
                        authorization_id,
                        kind="force",
                        **overrides,
                    )

    def test_pending_force_authorization_blocks_direct_ordinary_attempt_one(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            run_id = self._insert_run(connection, request_id="7" * 32)
            self._insert_authorization(
                connection,
                self._authorization_values(run_id),
            )

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_parent_attempt(
                    connection,
                    run_id,
                    outcome="success",
                    data_as_of=UTC,
                    error_code=None,
                    cooldown_until=None,
                    response_byte_count=100,
                )

    def test_direct_force_authorization_is_blocked_after_ordinary_attempt_one(self) -> None:
        repository = self.repository()
        repository.migrate()
        with repository.connect() as connection, connection:
            run_id = self._insert_run(connection, request_id="8" * 32)
            self._insert_parent_attempt(
                connection,
                run_id,
                outcome="success",
                data_as_of=UTC,
                error_code=None,
                cooldown_until=None,
                response_byte_count=100,
            )

            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_authorization(
                    connection,
                    self._authorization_values(run_id),
                )


if __name__ == "__main__":
    unittest.main()
