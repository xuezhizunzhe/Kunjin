from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
    SCHEMA_V12,
    SCHEMA_V13,
    SCHEMA_V14,
    SCHEMA_V15,
    SCHEMA_V16,
    SCHEMA_V17,
    SCHEMA_V18,
    SCHEMA_V19,
    SCHEMA_VERSION,
)

UTC = "2026-07-18T00:00:00+00:00"
SCHEMAS = {
    1: SCHEMA_V1,
    2: SCHEMA_V2,
    3: SCHEMA_V3,
    4: SCHEMA_V4,
    5: SCHEMA_V5,
    6: SCHEMA_V6,
    7: SCHEMA_V7,
    8: SCHEMA_V8,
    9: SCHEMA_V9,
    10: SCHEMA_V10,
    11: SCHEMA_V11,
    12: SCHEMA_V12,
    13: SCHEMA_V13,
    14: SCHEMA_V14,
    15: SCHEMA_V15,
    16: SCHEMA_V16,
    17: SCHEMA_V17,
    18: SCHEMA_V18,
    19: SCHEMA_V19,
}


def _create_version(path: Path, version: int) -> Repository:
    repository = Repository(path)
    with repository.connect() as connection, connection:
        for current in range(1, version + 1):
            if current == 12:
                _migrate_v12(connection)
            else:
                _execute_schema(connection, SCHEMAS[current])
        connection.executemany(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ((current, UTC) for current in range(1, version + 1)),
        )
        connection.execute(
            "INSERT INTO sync_runs(source, trigger, started_at, status, error_message) "
            "VALUES ('source', 'manual', ?, 'failed', ?)",
            (UTC, "preserve-byte-证据"),
        )
    return repository


@pytest.mark.parametrize("starting_version", (16, 17, 18))
def test_v19_migration_is_additive_and_preserves_prior_bytes(
    tmp_path: Path,
    starting_version: int,
) -> None:
    repository = _create_version(tmp_path / f"v{starting_version}.db", starting_version)
    with repository.connect() as connection:
        before = bytes(
            connection.execute("SELECT CAST(error_message AS BLOB) FROM sync_runs").fetchone()[0]
        )

    repository.migrate()

    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        after = bytes(
            connection.execute("SELECT CAST(error_message AS BLOB) FROM sync_runs").fetchone()[0]
        )
    assert SCHEMA_VERSION == 25
    assert versions == tuple(range(1, 26))
    assert after == before


def test_v19_has_exact_tables_foreign_keys_and_append_only_guards(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "fresh.db")
    repository.migrate()
    expected_tables = {
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
    }
    with repository.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        item_fks = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(intelligence_news_items)")
        }
        snapshot_fks = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(intelligence_snapshots)")
        }
        use_fks = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(intelligence_snapshot_item_uses)"
            )
        }
        event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='intelligence_events'"
        ).fetchone()[0]
        triggers = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    assert expected_tables <= tables
    assert ("source_attempt_id", "source_attempts", "id", "RESTRICT") in item_fks
    assert ("request_run_id", "request_runs", "id", "RESTRICT") in snapshot_fks
    assert use_fks == {
        ("request_run_id", "request_runs", "id", "RESTRICT"),
        ("item_id", "intelligence_news_items", "id", "RESTRICT"),
        ("source_attempt_id", "source_attempts", "id", "RESTRICT"),
    }
    assert "REFERENCES intelligence_events(event_key)" in event_sql
    assert "DEFERRABLE INITIALLY DEFERRED" in event_sql
    assert {
        "intelligence_news_item_no_replace",
        "intelligence_news_item_no_update",
        "intelligence_news_item_no_delete",
        "intelligence_excerpt_delete_guard",
        "intelligence_excerpt_no_update",
        "intelligence_lineage_no_replace",
        "intelligence_lineage_no_update",
        "intelligence_lineage_no_delete",
        "intelligence_event_no_replace",
        "intelligence_event_no_update",
        "intelligence_event_no_delete",
        "market_dimension_observation_no_replace",
        "market_dimension_observation_no_update",
        "market_dimension_observation_no_delete",
        "intelligence_snapshot_no_replace",
        "intelligence_snapshot_no_update",
        "intelligence_snapshot_no_delete",
        "intelligence_snapshot_item_use_insert_guard",
        "intelligence_snapshot_item_use_no_update",
        "intelligence_snapshot_item_use_no_delete",
    } <= triggers


def test_v19_schema_drift_and_cross_namespace_dependencies_are_rejected(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "drift.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        connection.execute(
            "CREATE TABLE intelligence_news_items_shadow(id INTEGER PRIMARY KEY, shadow_value TEXT)"
        )
    with pytest.raises(sqlite3.DatabaseError, match="intelligence schema"):
        repository.migrate()
