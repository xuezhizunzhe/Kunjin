from __future__ import annotations

from kunjin.storage.repository import Repository, _execute_schema, _migration_definitions
from kunjin.storage.schema import SCHEMA_VERSION


def test_v24_adds_public_research_events_without_rewriting_v23_evidence(tmp_path) -> None:
    repository = Repository(tmp_path / "v23.db")
    with repository.connect() as connection, connection:
        for version, schema in _migration_definitions():
            if version > 23:
                break
            _execute_schema(connection, schema)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        connection.execute(
            """
            INSERT INTO public_research_evidence(
                domain_id, source_name, publisher, source_kind, source_tier, title,
                original_url, published_at, statistics_period, indicator_name,
                indicator_value, unit, methodology, short_excerpt, excerpt_sha256,
                verification_state, revision_of_evidence_id, retrieved_at, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "autos",
                "source",
                "publisher",
                "industry_data",
                "tier_2",
                "title",
                "https://example.test/value",
                "2026-07-01T00:00:00+00:00",
                "2026年6月",
                "销量",
                "100",
                "万辆",
                None,
                None,
                "a" * 64,
                "outer_page_verified",
                "2026-07-02T00:00:00+00:00",
                "b" * 64,
            ),
        )

    repository.migrate()

    with repository.connect() as connection:
        event_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_research_events'"
        ).fetchone()
        evidence = connection.execute(
            "SELECT indicator_value FROM public_research_evidence"
        ).fetchone()["indicator_value"]

    assert SCHEMA_VERSION == 25
    assert event_table is not None
    assert evidence == "100"
