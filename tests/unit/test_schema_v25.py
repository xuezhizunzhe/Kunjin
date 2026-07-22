from __future__ import annotations

from kunjin.storage.repository import Repository, _execute_schema, _migration_definitions
from kunjin.storage.schema import SCHEMA_VERSION


def test_v25_backfills_event_evidence_state_without_rewriting_v24_rows(tmp_path) -> None:
    repository = Repository(tmp_path / "v24.db")
    with repository.connect() as connection, connection:
        for version, schema in _migration_definitions():
            if version > 24:
                break
            _execute_schema(connection, schema)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, "2026-07-22T00:00:00+00:00"),
            )
        for source_kind, source_tier, record_sha256 in (
            ("media", "tier_2", "a" * 64),
            ("community", "lead", "b" * 64),
        ):
            connection.execute(
                """
                INSERT INTO public_research_events(
                    event_key, domain_id, source_name, publisher, source_kind, source_tier,
                    title, original_url, event_occurred_at, published_at, fact_summary,
                    claim_boundary, event_fact_key, event_fact_value, event_fact_unit,
                    short_excerpt, excerpt_sha256, verification_state, retrieved_at, record_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    "power-sector-2026-07-22",
                    "power_energy",
                    source_kind,
                    source_kind,
                    source_kind,
                    source_tier,
                    "title",
                    f"https://example.test/{source_kind}",
                    "2026-07-22T00:00:00+00:00",
                    "summary",
                    "boundary",
                    "c" * 64,
                    "outer_page_verified",
                    "2026-07-22T00:00:00+00:00",
                    record_sha256,
                ),
            )

    repository.migrate()

    with repository.connect() as connection:
        states = [
            tuple(row)
            for row in connection.execute(
                "SELECT source_kind, evidence_state FROM public_research_events ORDER BY id"
            )
        ]

    assert SCHEMA_VERSION == 25
    assert states == [("media", "reported_fact"), ("community", "lead")]
