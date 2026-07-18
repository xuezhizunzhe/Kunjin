from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kunjin.storage.repository import (
    Repository,
    _execute_schema,
    _migrate_v12,
    _migration_definitions,
)
from kunjin.storage.schema import SCHEMA_VERSION

UTC = "2026-07-18T00:00:00+00:00"


def _v19_repository(path: Path) -> Repository:
    repository = Repository(path)
    with repository.connect() as connection, connection:
        for version, schema in _migration_definitions():
            if version > 19:
                break
            if version == 12:
                _migrate_v12(connection)
            else:
                _execute_schema(connection, schema)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, UTC),
            )
    return repository


def test_v20_replaces_only_the_news_attempt_time_guard_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    repository = _v19_repository(tmp_path / "v19.db")
    with repository.connect() as connection, connection:
        connection.execute(
            "INSERT INTO sync_runs(source, trigger, started_at, status, error_message) "
            "VALUES ('source', 'manual', ?, 'failed', ?)",
            (UTC, "preserve-byte-证据"),
        )
        old_guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='intelligence_news_item_insert_guard'"
        ).fetchone()[0]
        before = bytes(
            connection.execute("SELECT CAST(error_message AS BLOB) FROM sync_runs").fetchone()[0]
        )

    assert "finished_at <= NEW.retrieved_at" in old_guard

    repository.migrate()

    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='intelligence_news_item_insert_guard'"
        ).fetchone()[0]
        after = bytes(
            connection.execute("SELECT CAST(error_message AS BLOB) FROM sync_runs").fetchone()[0]
        )

    assert SCHEMA_VERSION == 20
    assert versions == tuple(range(1, 21))
    assert "julianday(started_at, '-1 second')" in guard
    assert "julianday(finished_at, '+1 second')" in guard
    assert "finished_at <= NEW.retrieved_at" not in guard
    assert after == before


def test_v20_rebuilds_only_an_empty_known_v19_intelligence_namespace(
    tmp_path: Path,
) -> None:
    repository = _v19_repository(tmp_path / "empty-legacy-v19.db")
    with repository.connect() as connection, connection:
        for trigger in (
            "intelligence_snapshot_item_use_insert_guard",
            "intelligence_snapshot_item_use_no_replace",
            "intelligence_snapshot_item_use_no_update",
            "intelligence_snapshot_item_use_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute("DROP TABLE intelligence_snapshot_item_uses")

    repository.migrate()

    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        rebuilt = connection.execute(
            "SELECT type FROM sqlite_master WHERE name='intelligence_snapshot_item_uses'"
        ).fetchone()
    assert versions == tuple(range(1, 21))
    assert rebuilt["type"] == "table"


def test_v20_refuses_to_rebuild_a_nonempty_drifted_v19_namespace(tmp_path: Path) -> None:
    repository = _v19_repository(tmp_path / "nonempty-legacy-v19.db")
    with repository.connect() as connection, connection:
        connection.execute(
            "INSERT INTO intelligence_policy_versions("
            "version, canonical_policy_json, policy_checksum, created_at"
            ") VALUES ('legacy', '{}', ?, ?)",
            ("a" * 64, UTC),
        )
        connection.execute("DROP TRIGGER intelligence_snapshot_item_use_no_update")

    with pytest.raises(sqlite3.DatabaseError, match="applied schema"):
        repository.migrate()
