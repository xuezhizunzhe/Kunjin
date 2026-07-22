from __future__ import annotations

from kunjin.storage.repository import Repository, _execute_schema, _migration_definitions
from kunjin.storage.schema import SCHEMA_VERSION


def test_v23_adds_public_research_evidence_without_rewriting_existing_rows(tmp_path) -> None:
    repository = Repository(tmp_path / "v22.db")
    with repository.connect() as connection, connection:
        for version, schema in _migration_definitions():
            if version > 22:
                break
            _execute_schema(connection, schema)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        connection.execute(
            "INSERT INTO sync_runs(source, trigger, started_at, status) VALUES (?, ?, ?, ?)",
            ("legacy", "manual", "2026-07-22T00:00:00+00:00", "success"),
        )

    repository.migrate()

    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        evidence_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_research_evidence'"
        ).fetchone()
        legacy_row = connection.execute("SELECT source FROM sync_runs").fetchone()

    assert SCHEMA_VERSION == 24
    assert versions == tuple(range(1, 25))
    assert evidence_table is not None
    assert legacy_row["source"] == "legacy"
