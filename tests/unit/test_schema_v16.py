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
    SCHEMA_VERSION,
)

UTC = "2026-07-17T00:00:00+00:00"

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
            (UTC, "preserve-byte-\N{CJK UNIFIED IDEOGRAPH-8BC1}\N{CJK UNIFIED IDEOGRAPH-636E}"),
        )
    return repository


@pytest.mark.parametrize("starting_version", (13, 14, 15))
def test_v16_migration_is_additive_and_preserves_prior_bytes(
    tmp_path: Path,
    starting_version: int,
) -> None:
    repository = _create_version(tmp_path / f"v{starting_version}.db", starting_version)
    with repository.connect() as connection:
        before = bytes(
            connection.execute(
                "SELECT CAST(error_message AS BLOB) FROM sync_runs"
            ).fetchone()[0]
        )

    repository.migrate()

    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        after = bytes(
            connection.execute(
                "SELECT CAST(error_message AS BLOB) FROM sync_runs"
            ).fetchone()[0]
        )
    assert SCHEMA_VERSION == 17
    assert versions == tuple(range(1, 18))
    assert after == before


def test_v16_has_exact_brief_tables_columns_foreign_keys_and_immutability(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "fresh.db")
    repository.migrate()
    with repository.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        policy_columns = tuple(
            row["name"]
            for row in connection.execute("PRAGMA table_info(brief_policy_versions)")
        )
        snapshot_columns = tuple(
            row["name"]
            for row in connection.execute("PRAGMA table_info(fund_brief_snapshots)")
        )
        foreign_keys = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(fund_brief_snapshots)"
            )
        }
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
    assert {"brief_policy_versions", "fund_brief_snapshots"} <= tables
    assert policy_columns == (
        "version",
        "canonical_policy_json",
        "policy_checksum",
        "created_at",
    )
    assert snapshot_columns == (
        "id",
        "request_run_id",
        "decision_snapshot_id",
        "fund_code",
        "action_ids_json",
        "primary_state",
        "action_maturity",
        "triggered_reviews_json",
        "affected_action_abstentions_json",
        "blocking_codes_json",
        "evidence_state",
        "missing_fields_json",
        "conflicts_json",
        "source_lineage_ids_json",
        "evidence_fingerprint",
        "canonical_snapshot_json",
        "result_checksum",
        "conclusion_changed",
        "created_at",
    )
    assert foreign_keys == {
        ("request_run_id", "request_runs", "id", "RESTRICT"),
        ("decision_snapshot_id", "decision_snapshots", "id", "RESTRICT"),
    }
    assert {
        "brief_policy_no_replace",
        "brief_policy_no_update",
        "brief_policy_no_delete",
        "fund_brief_snapshot_insert_guard",
        "fund_brief_snapshot_private_key_guard",
        "fund_brief_snapshot_array_guard",
        "fund_brief_snapshot_no_replace",
        "fund_brief_snapshot_no_update",
        "fund_brief_snapshot_no_delete",
    } <= triggers
    assert "fund_brief_snapshots_history" in indexes


def test_v17_adds_bounded_corporate_action_state_and_preserves_nav_rows(
    tmp_path: Path,
) -> None:
    repository = _create_version(tmp_path / "v16-nav.db", 16)
    with repository.connect() as connection, connection:
        connection.execute(
            """
            INSERT INTO funds(fund_code, fund_name, fund_type, source, observed_at)
            VALUES ('123456', '测试基金', '混合型', 'eastmoney', ?)
            """,
            (UTC,),
        )
        connection.execute(
            """
            INSERT INTO fund_nav(
                fund_code, nav_date, unit_nav, accumulated_nav,
                daily_growth, source, retrieved_at
            ) VALUES ('123456', '2026-07-16', '1.2', '2.2', '0.1', 'eastmoney', ?)
            """,
            (UTC,),
        )

    repository.migrate()

    with repository.connect() as connection:
        columns = tuple(
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(fund_nav)")
        )
        row = connection.execute(
            "SELECT * FROM fund_nav WHERE fund_code = '123456'"
        ).fetchone()
        versions = tuple(
            int(item["version"])
            for item in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        )
        assert row is not None
        assert row["corporate_action_state"] == "unknown"
        assert row["source_attempt_id"] is None
        assert "corporate_action_state" in columns
        assert "source_attempt_id" in columns
        assert versions == tuple(range(1, 18))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO fund_nav(
                    fund_code, nav_date, unit_nav, source, retrieved_at,
                    corporate_action_state
                ) VALUES ('123456', '2026-07-17', '1.3', 'eastmoney', ?, 'raw text')
                """,
                (UTC,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO fund_nav(
                    fund_code, nav_date, unit_nav, source, retrieved_at,
                    source_attempt_id
                ) VALUES ('123456', '2026-07-18', '1.4', 'eastmoney', ?, 999999)
                """,
                (UTC,),
            )


def test_failed_v16_migration_rolls_back_objects_marker_and_prior_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _create_version(tmp_path / "v15.db", 15)
    broken = "CREATE TABLE partial_v16(id INTEGER PRIMARY KEY); THIS IS NOT SQL;"
    monkeypatch.setattr("kunjin.storage.repository.SCHEMA_V16", broken)
    with pytest.raises(sqlite3.OperationalError):
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
        value = connection.execute("SELECT error_message FROM sync_runs").fetchone()[0]
    assert versions == tuple(range(1, 16))
    assert "partial_v16" not in tables
    assert value == "preserve-byte-\N{CJK UNIFIED IDEOGRAPH-8BC1}\N{CJK UNIFIED IDEOGRAPH-636E}"


@pytest.mark.parametrize(
    "hostile_script",
    (
        "CREATE VIEW unrelated_brief_view AS SELECT * FROM fund_brief_snapshots;",
        """
        CREATE TRIGGER unrelated_brief_reader
        AFTER INSERT ON sync_runs
        BEGIN
            SELECT count(*) FROM brief_policy_versions;
        END;
        """,
        """
        CREATE TABLE unrelated_brief_fk(
            id INTEGER REFERENCES fund_brief_snapshots(id)
        );
        """,
    ),
)
def test_v16_rejects_unexpected_dependencies_on_brief_tables(
    tmp_path: Path,
    hostile_script: str,
) -> None:
    repository = Repository(tmp_path / "hostile.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        connection.executescript(hostile_script)
    with pytest.raises(sqlite3.DatabaseError, match="brief schema does not match V16"):
        repository.migrate()


def test_v16_rejects_unexpected_virtual_schema_objects(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "virtual.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE unrelated_brief_fts USING fts5(value)"
            )
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" in str(exc).casefold():
                pytest.skip("SQLite build does not expose FTS5")
            raise
    with pytest.raises(sqlite3.DatabaseError):
        repository.migrate()
