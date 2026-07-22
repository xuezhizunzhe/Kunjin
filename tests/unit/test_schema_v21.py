from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from kunjin.storage.repository import (
    Repository,
    _execute_schema,
    _migrate_v12,
    _migration_definitions,
)
from kunjin.storage.schema import SCHEMA_V19, SCHEMA_V20, SCHEMA_V21, SCHEMA_VERSION

UTC = "2026-07-20T00:00:00+00:00"
FINISHED = "2026-07-20T00:01:00+00:00"
DEADLINE = "2026-07-20T00:30:00+00:00"
FUND_CODE = "123456"
CHECKSUMS = tuple(character * 64 for character in "abcdef1234567890")
EXPECTED_PHASE5_TABLES = {
    "fund_official_announcement_contents",
    "held_review_official_event_projections",
    "thesis_match_projections",
    "thesis_evidence_adjudications",
    "holding_review_snapshots",
}
OLD_SCHEMA_DIGESTS = {
    19: "f24aa5c98ec176a3cc9945f7e8cd219cea43fd54710115c7faba5aefaf26da75",
    20: "d724355222a471a5123db21838d36732f0ba14776d013869abc4fcaf88a544bc",
}


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _projection_evidence_checksum(
    evidence_ids: list[str],
    evidence_descriptors: list[dict[str, object]],
) -> str:
    return hashlib.sha256(
        _compact(
            {
                "evidence_descriptors": evidence_descriptors,
                "evidence_ids": evidence_ids,
            }
        ).encode()
    ).hexdigest()


def _insert_mapping(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, object],
    *,
    or_replace: bool = False,
) -> sqlite3.Cursor:
    operation = "INSERT OR REPLACE" if or_replace else "INSERT"
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    return connection.execute(
        f"{operation} INTO {table}({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def _create_version(path: Path, version: int) -> Repository:
    repository = Repository(path)
    with repository.connect() as connection, connection:
        for current, schema in _migration_definitions():
            if current > version:
                break
            if current == 12:
                _migrate_v12(connection)
            else:
                _execute_schema(connection, schema)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (current, UTC),
            )
        connection.execute(
            "INSERT INTO sync_runs(source, trigger, started_at, status, error_message) "
            "VALUES ('source', 'manual', ?, 'failed', ?)",
            (UTC, "preserve-byte-证据"),
        )
    return repository


def _legacy_bytes(repository: Repository) -> bytes:
    with repository.connect() as connection:
        value = connection.execute(
            "SELECT CAST(error_message AS BLOB) FROM sync_runs"
        ).fetchone()[0]
    return bytes(value)


def _request(connection: sqlite3.Connection, request_id: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO request_runs(
            request_id, mode, status, started_at, deadline_at, finished_at,
            omitted_work_json
        ) VALUES (?, 'rapid', 'running', ?, ?, NULL, '[]')
        """,
        (request_id, UTC, DEADLINE),
    )
    return int(cursor.lastrowid)


def _finish_request(connection: sqlite3.Connection, request_run_id: int) -> None:
    connection.execute(
        "UPDATE request_runs SET status='complete', finished_at=? WHERE id=?",
        (FINISHED, request_run_id),
    )


def _parent_fixture(connection: sqlite3.Connection) -> dict[str, int | str]:
    brief_request_id = _request(connection, "1" * 32)
    intelligence_request_id = _request(connection, "2" * 32)
    source_attempt_id = int(
        connection.execute(
            """
            INSERT INTO source_attempts(
                request_run_id, source_id, field_id, subject_key, attempt_number,
                outcome, started_at, finished_at, data_as_of, error_code,
                cooldown_until, force_actor, force_reason, registry_version,
                registry_checksum, response_byte_count, authorization_id
            ) VALUES (?, 'fund_manager_official_documents',
                      'fund_manager_product_announcement', ?,
                      1, 'success', ?, ?, ?, NULL, NULL, NULL, NULL, '1', ?, 100, NULL)
            """,
            (
                brief_request_id,
                f"fund:{FUND_CODE}",
                UTC,
                FINISHED,
                FINISHED,
                CHECKSUMS[0],
            ),
        ).lastrowid
    )
    source_document_id = int(
        connection.execute(
            """
            INSERT INTO fund_source_documents(
                fund_code, document_kind, title, url, source_name, source_tier,
                publisher, published_at, retrieved_at, checksum
            ) VALUES (?, 'announcement', 'official list', 'https://manager.example/list',
                      'fund_manager_official_documents', 1, 'manager', ?, ?, ?)
            """,
            (FUND_CODE, UTC, FINISHED, CHECKSUMS[1]),
        ).lastrowid
    )
    announcement_id = int(
        connection.execute(
            """
            INSERT INTO fund_announcements(
                fund_code, record_key, title, category, publisher, published_at,
                url, source_tier, source_document_id
            ) VALUES (?, 'notice_1', '基金经理变更公告', 'manager_change', 'manager', ?,
                      'https://manager.example/notice/1', 1, ?)
            """,
            (FUND_CODE, UTC, source_document_id),
        ).lastrowid
    )
    decision_snapshot_id = int(
        connection.execute(
            """
            INSERT INTO decision_snapshots(
                request_run_id, evidence_policy_version, evidence_policy_json,
                evidence_policy_checksum, source_registry_version,
                source_registry_json, source_registry_checksum,
                canonical_route_json, result_checksum, created_at
            ) VALUES (?, '1', '{}', ?, '1', '{}', ?, '{}', ?, ?)
            """,
            (brief_request_id, CHECKSUMS[2], CHECKSUMS[3], CHECKSUMS[4], UTC),
        ).lastrowid
    )
    brief_payload = {
        "action_ids": ["fact_research", "continue_holding"],
        "action_maturity": "experimental_shadow",
        "affected_action_abstentions": [],
        "blocking_codes": [],
        "conflicts": [],
        "created_at": UTC,
        "decision_snapshot_id": decision_snapshot_id,
        "evidence_fingerprint": CHECKSUMS[5],
        "evidence_state": "complete",
        "fund_code": FUND_CODE,
        "missing_fields": [],
        "primary_state": "hold",
        "request_run_id": brief_request_id,
        "source_lineage_ids": [],
        "triggered_reviews": [],
    }
    brief_snapshot_id = int(
        connection.execute(
            """
            INSERT INTO fund_brief_snapshots(
                request_run_id, decision_snapshot_id, fund_code, action_ids_json,
                primary_state, action_maturity, triggered_reviews_json,
                affected_action_abstentions_json, blocking_codes_json, evidence_state,
                missing_fields_json, conflicts_json, source_lineage_ids_json,
                evidence_fingerprint, canonical_snapshot_json, result_checksum,
                conclusion_changed, created_at
            ) VALUES (?, ?, ?, '["fact_research","continue_holding"]', 'hold',
                      'experimental_shadow', '[]', '[]', '[]', 'complete', '[]',
                      '[]', '[]', ?, ?, ?, 0, ?)
            """,
            (
                brief_request_id,
                decision_snapshot_id,
                FUND_CODE,
                CHECKSUMS[5],
                _compact(brief_payload),
                CHECKSUMS[6],
                UTC,
            ),
        ).lastrowid
    )
    connection.execute(
        "INSERT INTO intelligence_policy_versions(version, canonical_policy_json, "
        "policy_checksum, created_at) VALUES ('1', '{}', ?, ?)",
        (CHECKSUMS[7], UTC),
    )
    intelligence_attempt_id = int(
        connection.execute(
            """
            INSERT INTO source_attempts(
                request_run_id, source_id, field_id, subject_key, attempt_number,
                outcome, started_at, finished_at, data_as_of, error_code,
                cooldown_until, force_actor, force_reason, registry_version,
                registry_checksum, response_byte_count, authorization_id
            ) VALUES (?, 'fund_manager_official_documents',
                      'fund_manager_product_announcement', ?, 1, 'success', ?, ?, ?,
                      NULL, NULL, NULL, NULL, '1', ?, 100, NULL)
            """,
            (
                intelligence_request_id,
                f"fund:{FUND_CODE}",
                UTC,
                FINISHED,
                FINISHED,
                CHECKSUMS[0],
            ),
        ).lastrowid
    )
    intelligence_item_id = int(
        connection.execute(
            """
            INSERT INTO intelligence_news_items(
                item_key, source_id, publisher, canonical_url, title,
                excerpt_original_bytes, excerpt_sha256, published_at,
                publication_precision, publication_interval_end, retrieved_at,
                source_tier, content_fingerprint, category, integrity_state,
                source_attempt_id
            ) VALUES (
                'evidence_one', 'fund_manager_official_documents', 'manager',
                'https://manager.example/notice/1', '基金经理变更公告', 100, ?, ?,
                'minute', NULL, ?, 'tier_1', ?, 'fund_official', 'active', ?
            )
            """,
            (CHECKSUMS[1], UTC, FINISHED, CHECKSUMS[2], intelligence_attempt_id),
        ).lastrowid
    )
    connection.execute(
        "INSERT INTO intelligence_snapshot_item_uses("
        "request_run_id, item_id, source_attempt_id) VALUES (?, ?, ?)",
        (intelligence_request_id, intelligence_item_id, intelligence_attempt_id),
    )
    market_state_id = int(
        connection.execute(
            """
            INSERT INTO market_state_snapshots(
                request_run_id, policy_version, observation_ids_json,
                canonical_state_json, state_checksum, created_at
            ) VALUES (?, '1', '[]', '{}', ?, ?)
            """,
            (intelligence_request_id, CHECKSUMS[8], UTC),
        ).lastrowid
    )
    intelligence_payload = {
        "item_ids": ["evidence_one"],
        "request_run_id": intelligence_request_id,
        "source_attempt_ids": [intelligence_attempt_id],
        "subject_fund_code": FUND_CODE,
        "workflow": "fund_intelligence",
    }
    intelligence_snapshot_id = int(
        connection.execute(
            """
            INSERT INTO intelligence_snapshots(
                request_run_id, market_state_snapshot_id, policy_version, workflow,
                canonical_snapshot_json, result_checksum, created_at
            ) VALUES (?, ?, '1', 'fund_intelligence', ?, ?, ?)
            """,
            (
                intelligence_request_id,
                market_state_id,
                _compact(intelligence_payload),
                CHECKSUMS[9],
                UTC,
            ),
        ).lastrowid
    )
    thesis_id = int(
        connection.execute(
            """
            INSERT INTO investment_theses(
                fund_code, rationale, horizon, invalidation, created_at, active
            ) VALUES (?, 'rationale', 'long', 'condition', ?, 1)
            """,
            (FUND_CODE, UTC),
        ).lastrowid
    )
    return {
        "announcement_id": announcement_id,
        "brief_request_id": brief_request_id,
        "brief_snapshot_checksum": CHECKSUMS[6],
        "brief_snapshot_id": brief_snapshot_id,
        "intelligence_request_id": intelligence_request_id,
        "intelligence_snapshot_checksum": CHECKSUMS[9],
        "intelligence_snapshot_id": intelligence_snapshot_id,
        "source_attempt_id": source_attempt_id,
        "source_document_id": source_document_id,
        "thesis_id": thesis_id,
    }


def _official_content_values(values: dict[str, int | str]) -> dict[str, object]:
    content = "本基金基金经理发生变更。"
    return {
        "brief_request_run_id": values["brief_request_id"],
        "source_attempt_id": values["source_attempt_id"],
        "fund_code": FUND_CODE,
        "listing_source_document_id": values["source_document_id"],
        "canonical_announcement_url": "https://manager.example/notice/1",
        "announcement_title": "基金经理变更公告",
        "announcement_published_at": UTC,
        "publisher": "manager",
        "normalized_content": content,
        "normalized_content_bytes": len(content.encode()),
        "normalized_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "original_source_id": "fund_manager_official_documents",
        "quoted_source_id": None,
        "integrity_status": "active",
        "integrity_checked_at": FINISHED,
        "retrieved_at": FINISHED,
        "record_checksum": CHECKSUMS[10],
    }


def _insert_official_content(
    connection: sqlite3.Connection,
    values: dict[str, int | str],
) -> int:
    return int(
        _insert_mapping(
            connection,
            "fund_official_announcement_contents",
            _official_content_values(values),
        ).lastrowid
    )


def _insert_official_event(
    connection: sqlite3.Connection,
    values: dict[str, int | str],
    content_id: int,
) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO held_review_official_event_projections(
                brief_request_run_id, fund_code, announcement_row_id,
                announcement_content_id, event_code, triggered_review_code,
                policy_version, policy_checksum, record_checksum
            ) VALUES (?, ?, ?, ?, 'manager_change_notice', 'manager_change_review',
                      '1', ?, ?)
            """,
            (
                values["brief_request_id"],
                FUND_CODE,
                values["announcement_id"],
                content_id,
                CHECKSUMS[11],
                CHECKSUMS[12],
            ),
        ).lastrowid
    )


def _phase5_fixture(connection: sqlite3.Connection) -> dict[str, int | str]:
    values = _parent_fixture(connection)
    content_id = _insert_official_content(connection, values)
    event_id = _insert_official_event(connection, values, content_id)
    _finish_request(connection, int(values["brief_request_id"]))
    _finish_request(connection, int(values["intelligence_request_id"]))
    descriptor = {
        "conflicted": False,
        "current": True,
        "direct_subject_binding": True,
        "evidence_id": "evidence_one",
        "graph_closed": True,
        "lineage_kind": "original",
        "original_lineage": True,
        "retracted": False,
        "source_tier": 1,
    }
    evidence_ids = _compact(["evidence_one"])
    evidence_descriptors = _compact([descriptor])
    evidence_set_checksum = hashlib.sha256(
        _compact(
            {
                "evidence_descriptors": [descriptor],
                "evidence_ids": ["evidence_one"],
            }
        ).encode()
    ).hexdigest()
    thesis_fingerprint = CHECKSUMS[13]
    projection_id = int(
        connection.execute(
            """
            INSERT INTO thesis_match_projections(
                fund_code, thesis_id, thesis_fingerprint,
                intelligence_request_run_id, intelligence_snapshot_id,
                intelligence_snapshot_checksum, matcher_policy_version,
                matcher_policy_checksum, projection_state, evidence_ids_json,
                evidence_descriptors_json, evidence_set_checksum, created_at,
                record_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, '1', ?, 'possible_invalidation_match',
                      ?, ?, ?, ?, ?)
            """,
            (
                FUND_CODE,
                values["thesis_id"],
                thesis_fingerprint,
                values["intelligence_request_id"],
                values["intelligence_snapshot_id"],
                values["intelligence_snapshot_checksum"],
                CHECKSUMS[14],
                evidence_ids,
                evidence_descriptors,
                evidence_set_checksum,
                FINISHED,
                CHECKSUMS[15],
            ),
        ).lastrowid
    )
    adjudication_evidence_checksum = hashlib.sha256(evidence_ids.encode()).hexdigest()
    adjudication_id = int(
        connection.execute(
            """
            INSERT INTO thesis_evidence_adjudications(
                fund_code, thesis_id, thesis_fingerprint,
                thesis_match_projection_id, thesis_match_projection_checksum,
                intelligence_request_run_id, intelligence_snapshot_checksum,
                evidence_ids_json, evidence_set_checksum, decision,
                superseded_adjudication_id, created_at, record_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'uncertain', NULL, ?, ?)
            """,
            (
                FUND_CODE,
                values["thesis_id"],
                thesis_fingerprint,
                projection_id,
                CHECKSUMS[15],
                values["intelligence_request_id"],
                values["intelligence_snapshot_checksum"],
                evidence_ids,
                adjudication_evidence_checksum,
                FINISHED,
                CHECKSUMS[0],
            ),
        ).lastrowid
    )
    result_json = _compact(
        {
            "action": "continue_holding",
            "exit_reason": "unknown",
            "fund_code": FUND_CODE,
            "remainder_intent": "unknown",
            "use_of_proceeds": "unknown",
        }
    )
    review_id = int(
        connection.execute(
            """
            INSERT INTO holding_review_snapshots(
                fund_code, action, brief_request_run_id, brief_snapshot_id,
                brief_snapshot_checksum, intelligence_request_run_id,
                intelligence_snapshot_id, intelligence_snapshot_checksum,
                thesis_match_projection_id, thesis_match_projection_checksum,
                active_thesis_state, active_thesis_id, active_thesis_fingerprint,
                adjudication_state, adjudication_id, adjudication_checksum,
                previous_review_id, result_json, result_fingerprint,
                policy_version, policy_checksum, created_at,
                semantic_identity_checksum, record_checksum
            ) VALUES (?, 'continue_holding', ?, ?, ?, ?, ?, ?, ?, ?, 'present', ?, ?,
                      'present', ?, ?, NULL, ?, ?, '1', ?, ?, ?, ?)
            """,
            (
                FUND_CODE,
                values["brief_request_id"],
                values["brief_snapshot_id"],
                values["brief_snapshot_checksum"],
                values["intelligence_request_id"],
                values["intelligence_snapshot_id"],
                values["intelligence_snapshot_checksum"],
                projection_id,
                CHECKSUMS[15],
                values["thesis_id"],
                thesis_fingerprint,
                adjudication_id,
                CHECKSUMS[0],
                result_json,
                CHECKSUMS[1],
                CHECKSUMS[2],
                FINISHED,
                CHECKSUMS[3],
                CHECKSUMS[4],
            ),
        ).lastrowid
    )
    values.update(
        {
            "adjudication_id": adjudication_id,
            "content_id": content_id,
            "event_id": event_id,
            "projection_id": projection_id,
            "projection_checksum": CHECKSUMS[15],
            "review_id": review_id,
            "thesis_fingerprint": thesis_fingerprint,
        }
    )
    return values


def test_v21_is_additive_and_preserves_v20_bytes(tmp_path: Path) -> None:
    repository = _create_version(tmp_path / "v20.db", 20)
    before = _legacy_bytes(repository)
    repository.migrate()
    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert SCHEMA_VERSION == 24
    assert versions == tuple(range(1, 25))
    assert EXPECTED_PHASE5_TABLES <= tables
    assert _legacy_bytes(repository) == before


@pytest.mark.parametrize("starting_version", tuple(range(1, 21)))
def test_every_prior_version_migrates_to_v21(tmp_path: Path, starting_version: int) -> None:
    repository = _create_version(tmp_path / f"v{starting_version}.db", starting_version)
    before = _legacy_bytes(repository)
    repository.migrate()
    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
    assert versions == tuple(range(1, 25))
    assert _legacy_bytes(repository) == before


def test_v19_and_v20_schema_constants_remain_byte_identical() -> None:
    assert hashlib.sha256(SCHEMA_V19.encode()).hexdigest() == OLD_SCHEMA_DIGESTS[19]
    assert hashlib.sha256(SCHEMA_V20.encode()).hexdigest() == OLD_SCHEMA_DIGESTS[20]
    assert SCHEMA_V21


def test_v21_has_exact_tables_foreign_keys_and_append_only_guards(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "fresh.db")
    repository.migrate()
    with repository.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        review_fks = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(holding_review_snapshots)")
        }
    assert EXPECTED_PHASE5_TABLES <= tables
    for table in EXPECTED_PHASE5_TABLES:
        prefix = table.removesuffix("s")
        assert f"{prefix}_no_update" in triggers
        assert f"{prefix}_no_delete" in triggers
    assert {
        ("brief_request_run_id", "request_runs", "id", "RESTRICT"),
        ("brief_snapshot_id", "fund_brief_snapshots", "id", "RESTRICT"),
        ("intelligence_request_run_id", "request_runs", "id", "RESTRICT"),
        ("intelligence_snapshot_id", "intelligence_snapshots", "id", "RESTRICT"),
        ("thesis_match_projection_id", "thesis_match_projections", "id", "RESTRICT"),
        ("active_thesis_id", "investment_theses", "id", "RESTRICT"),
        ("adjudication_id", "thesis_evidence_adjudications", "id", "RESTRICT"),
        ("previous_review_id", "holding_review_snapshots", "id", "RESTRICT"),
    } <= review_fks


def test_v21_primary_keys_and_evidence_sets_are_bounded(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "bounds.db")
    repository.migrate()
    with repository.connect() as connection:
        table_sql = {
            table: "".join(
                str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()[0]
                ).split()
            )
            for table in EXPECTED_PHASE5_TABLES
        }
    for sql in table_sql.values():
        assert "idINTEGERPRIMARYKEYAUTOINCREMENTCHECK(typeof(id)='integer'ANDid>0)" in sql
    assert "json_array_length(evidence_ids_json)<=128" in table_sql[
        "thesis_match_projections"
    ]
    assert "json_array_length(evidence_descriptors_json)<=128" in table_sql[
        "thesis_match_projections"
    ]
    assert "json_array_length(evidence_ids_json)BETWEEN1AND128" in table_sql[
        "thesis_evidence_adjudications"
    ]


def test_v21_namespace_drift_is_rejected(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "drift.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        connection.execute(
            "CREATE TABLE holding_review_snapshots_shadow(id INTEGER PRIMARY KEY)"
        )
    with pytest.raises(sqlite3.DatabaseError, match="held review schema"):
        repository.migrate()


@pytest.mark.parametrize("table", sorted(EXPECTED_PHASE5_TABLES))
@pytest.mark.parametrize("operation", ("UPDATE", "DELETE"))
def test_phase5_rows_are_immutable(tmp_path: Path, table: str, operation: str) -> None:
    repository = Repository(tmp_path / f"{table}-{operation}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        _phase5_fixture(connection)
        statement = f"UPDATE {table} SET id=id" if operation == "UPDATE" else f"DELETE FROM {table}"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement)


def test_phase5_insert_guards_reject_wrong_subject_and_parent_checksums(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "binding.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = connection.execute(
            "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
            (fixture["adjudication_id"],),
        ).fetchone()
        values = dict(row)
        values.pop("id")
        values["decision"] = "presented_match_confirmed"
        for column, value in (
            ("fund_code", "654321"),
            ("thesis_match_projection_checksum", CHECKSUMS[5]),
            ("intelligence_request_run_id", int(fixture["brief_request_id"])),
            ("intelligence_snapshot_checksum", CHECKSUMS[5]),
            ("evidence_ids_json", _compact(["other_evidence"])),
            ("evidence_set_checksum", CHECKSUMS[5]),
        ):
            tampered = dict(values)
            tampered[column] = value
            columns = ",".join(tampered)
            placeholders = ",".join("?" for _ in tampered)
            with pytest.raises(sqlite3.IntegrityError, match="binding"):
                connection.execute(
                    f"INSERT INTO thesis_evidence_adjudications({columns}) VALUES ({placeholders})",
                    tuple(tampered.values()),
                )


@pytest.mark.parametrize(
    ("state", "thesis_id", "fingerprint"),
    (
        ("present", None, None),
        ("missing", 1, CHECKSUMS[5]),
    ),
)
def test_review_active_thesis_state_is_closed(
    tmp_path: Path,
    state: str,
    thesis_id: int | None,
    fingerprint: str | None,
) -> None:
    repository = Repository(tmp_path / f"thesis-{state}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?", (fixture["review_id"],)
            ).fetchone()
        )
        row.pop("id")
        row["active_thesis_state"] = state
        row["active_thesis_id"] = thesis_id
        row["active_thesis_fingerprint"] = fingerprint
        row["semantic_identity_checksum"] = CHECKSUMS[6]
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        with pytest.raises(sqlite3.IntegrityError, match="binding|CHECK constraint"):
            connection.execute(
                f"INSERT INTO holding_review_snapshots({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )


@pytest.mark.parametrize(
    ("state", "adjudication_id", "checksum"),
    (("present", None, None), ("missing", 1, CHECKSUMS[5])),
)
def test_review_adjudication_state_is_closed(
    tmp_path: Path,
    state: str,
    adjudication_id: int | None,
    checksum: str | None,
) -> None:
    repository = Repository(tmp_path / f"adjudication-{state}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?", (fixture["review_id"],)
            ).fetchone()
        )
        row.pop("id")
        row["adjudication_state"] = state
        row["adjudication_id"] = adjudication_id
        row["adjudication_checksum"] = checksum
        row["semantic_identity_checksum"] = CHECKSUMS[7]
        columns = ",".join(row)
        placeholders = ",".join("?" for _ in row)
        with pytest.raises(sqlite3.IntegrityError, match="binding|CHECK constraint"):
            connection.execute(
                f"INSERT INTO holding_review_snapshots({columns}) VALUES ({placeholders})",
                tuple(row.values()),
            )


def test_review_semantic_identity_is_unique_but_changed_result_can_append(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "identity.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        original = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?", (fixture["review_id"],)
            ).fetchone()
        )
        original.pop("id")
        original["created_at"] = "2026-07-20T00:02:00+00:00"
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced|UNIQUE"):
            connection.execute(
                f"INSERT INTO holding_review_snapshots({','.join(original)}) "
                f"VALUES ({','.join('?' for _ in original)})",
                tuple(original.values()),
            )
        changed = dict(original)
        changed["result_fingerprint"] = CHECKSUMS[8]
        changed["semantic_identity_checksum"] = CHECKSUMS[9]
        cursor = connection.execute(
            f"INSERT INTO holding_review_snapshots({','.join(changed)}) "
            f"VALUES ({','.join('?' for _ in changed)})",
            tuple(changed.values()),
        )
        assert int(cursor.lastrowid) > int(fixture["review_id"])


def test_thesis_projection_requires_authenticated_intelligence_snapshot(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "projection.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["intelligence_snapshot_id"] = int(fixture["intelligence_snapshot_id"]) + 100
        row["matcher_policy_checksum"] = CHECKSUMS[1]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO thesis_match_projections({','.join(row)}) "
                f"VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )


def test_thesis_missing_projection_logical_identity_cannot_be_replaced(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "missing-projection.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        values = (
            FUND_CODE,
            fixture["intelligence_request_id"],
            fixture["intelligence_snapshot_id"],
            fixture["intelligence_snapshot_checksum"],
            CHECKSUMS[1],
            _projection_evidence_checksum([], []),
            FINISHED,
            CHECKSUMS[3],
        )
        statement = """
            INSERT INTO thesis_match_projections(
                fund_code, thesis_id, thesis_fingerprint,
                intelligence_request_run_id, intelligence_snapshot_id,
                intelligence_snapshot_checksum, matcher_policy_version,
                matcher_policy_checksum, projection_state, evidence_ids_json,
                evidence_descriptors_json, evidence_set_checksum, created_at,
                record_checksum
            ) VALUES (?, NULL, NULL, ?, ?, ?, '1', ?, 'thesis_missing',
                      '[]', '[]', ?, ?, ?)
        """
        connection.execute(statement, values)
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced"):
            connection.execute(statement, values)


def test_changed_adjudication_can_append_with_explicit_supersession(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "changed-adjudication.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        original = dict(
            connection.execute(
                "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
                (fixture["adjudication_id"],),
            ).fetchone()
        )
        original.pop("id")
        original["decision"] = "presented_match_confirmed"
        original["superseded_adjudication_id"] = fixture["adjudication_id"]
        original["created_at"] = "2026-07-20T00:02:00+00:00"
        original["record_checksum"] = CHECKSUMS[5]
        cursor = connection.execute(
            f"INSERT INTO thesis_evidence_adjudications({','.join(original)}) "
            f"VALUES ({','.join('?' for _ in original)})",
            tuple(original.values()),
        )
        assert int(cursor.lastrowid) > int(fixture["adjudication_id"])


def test_projection_evidence_must_close_over_authenticated_snapshot(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "foreign-evidence.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        descriptor = {
            "conflicted": False,
            "current": True,
            "direct_subject_binding": True,
            "evidence_id": "foreign_evidence",
            "graph_closed": True,
            "lineage_kind": "original",
            "original_lineage": True,
            "retracted": False,
            "source_tier": 1,
        }
        row["evidence_ids_json"] = _compact(["foreign_evidence"])
        row["evidence_descriptors_json"] = _compact([descriptor])
        row["evidence_set_checksum"] = _projection_evidence_checksum(
            ["foreign_evidence"], [descriptor]
        )
        row["matcher_policy_checksum"] = CHECKSUMS[1]
        with pytest.raises(sqlite3.IntegrityError, match="projection binding"):
            _insert_mapping(connection, "thesis_match_projections", row)


@pytest.mark.parametrize(
    "descriptors",
    (
        [{}],
        [{"evidence_id": "evidence_one", "source_tier": 999}],
        [
            {
                "conflicted": False,
                "current": True,
                "direct_subject_binding": True,
                "evidence_id": "evidence_one",
                "graph_closed": True,
                "lineage_kind": "original",
                "original_lineage": True,
                "private_note": "owner-only",
                "retracted": False,
                "source_tier": 1,
            }
        ],
    ),
)
def test_projection_descriptor_must_have_exact_public_shape(
    tmp_path: Path,
    descriptors: list[dict[str, object]],
) -> None:
    repository = Repository(tmp_path / "descriptor.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["evidence_descriptors_json"] = _compact(descriptors)
        row["evidence_set_checksum"] = _projection_evidence_checksum(
            ["evidence_one"], descriptors
        )
        row["matcher_policy_checksum"] = CHECKSUMS[1]
        with pytest.raises(sqlite3.IntegrityError, match="projection binding"):
            _insert_mapping(connection, "thesis_match_projections", row)


def test_projection_evidence_checksum_is_recomputed(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "projection-checksum.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["evidence_set_checksum"] = CHECKSUMS[1]
        row["matcher_policy_checksum"] = CHECKSUMS[2]
        with pytest.raises(sqlite3.IntegrityError, match="projection binding"):
            _insert_mapping(connection, "thesis_match_projections", row)


def test_projection_descriptor_key_order_must_match_canonical_model(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "descriptor-order.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        original = json.loads(str(row["evidence_descriptors_json"]))[0]
        reversed_descriptor = dict(reversed(tuple(original.items())))
        descriptor_json = json.dumps(
            [reversed_descriptor],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )
        evidence_ids_json = str(row["evidence_ids_json"])
        row["evidence_descriptors_json"] = descriptor_json
        row["evidence_set_checksum"] = hashlib.sha256(
            (
                '{"evidence_descriptors":'
                + descriptor_json
                + ',"evidence_ids":'
                + evidence_ids_json
                + "}"
            ).encode()
        ).hexdigest()
        row["matcher_policy_checksum"] = CHECKSUMS[1]
        with pytest.raises(sqlite3.IntegrityError, match="projection binding"):
            _insert_mapping(connection, "thesis_match_projections", row)


@pytest.mark.parametrize(
    ("field", "value"),
    (("source_tier", 2), ("retracted", True)),
)
def test_projection_descriptor_cannot_misstate_stored_item(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repository = Repository(tmp_path / f"descriptor-item-{field}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_match_projections WHERE id=?",
                (fixture["projection_id"],),
            ).fetchone()
        )
        row.pop("id")
        descriptors = json.loads(str(row["evidence_descriptors_json"]))
        descriptors[0][field] = value
        row["evidence_descriptors_json"] = _compact(descriptors)
        row["evidence_set_checksum"] = _projection_evidence_checksum(
            ["evidence_one"], descriptors
        )
        row["matcher_policy_checksum"] = CHECKSUMS[1]
        with pytest.raises(sqlite3.IntegrityError, match="projection binding"):
            _insert_mapping(connection, "thesis_match_projections", row)


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("canonical_announcement_url", "https://manager.example/notice/not-listed"),
        ("announcement_title", "未列入披露清单的公告"),
        ("announcement_published_at", "2026-07-19T00:00:00+00:00"),
    ),
)
def test_official_content_requires_exact_announcement_row(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    repository = Repository(tmp_path / f"official-{column}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _parent_fixture(connection)
        row = _official_content_values(fixture)
        row[column] = value
        with pytest.raises(sqlite3.IntegrityError, match="content binding"):
            _insert_mapping(connection, "fund_official_announcement_contents", row)


def test_official_content_requires_exact_source_field(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "official-field.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _parent_fixture(connection)
        wrong_attempt_id = int(
            connection.execute(
                """
                INSERT INTO source_attempts(
                    request_run_id, source_id, field_id, subject_key, attempt_number,
                    outcome, started_at, finished_at, data_as_of, error_code,
                    cooldown_until, force_actor, force_reason, registry_version,
                    registry_checksum, response_byte_count, authorization_id
                ) VALUES (?, 'fund_manager_official_documents', 'identity_active_status', ?,
                          1, 'success', ?, ?, ?, NULL, NULL, NULL, NULL, '1', ?, 100, NULL)
                """,
                (
                    fixture["brief_request_id"],
                    f"fund:{FUND_CODE}",
                    UTC,
                    FINISHED,
                    FINISHED,
                    CHECKSUMS[0],
                ),
            ).lastrowid
        )
        row = _official_content_values(fixture)
        row["source_attempt_id"] = wrong_attempt_id
        with pytest.raises(sqlite3.IntegrityError, match="content binding"):
            _insert_mapping(connection, "fund_official_announcement_contents", row)


def test_changed_adjudication_requires_current_supersession(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "adjudication-supersession.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
                (fixture["adjudication_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["decision"] = "presented_match_confirmed"
        row["created_at"] = "2026-07-20T00:02:00+00:00"
        row["record_checksum"] = CHECKSUMS[5]
        with pytest.raises(sqlite3.IntegrityError, match="adjudication binding"):
            _insert_mapping(connection, "thesis_evidence_adjudications", row)

        row["superseded_adjudication_id"] = fixture["adjudication_id"]
        confirmed_id = int(
            _insert_mapping(connection, "thesis_evidence_adjudications", row).lastrowid
        )
        assert confirmed_id > int(fixture["adjudication_id"])

        row["decision"] = "presented_match_rejected"
        row["created_at"] = "2026-07-20T00:03:00+00:00"
        row["record_checksum"] = CHECKSUMS[6]
        with pytest.raises(sqlite3.IntegrityError, match="adjudication binding"):
            _insert_mapping(connection, "thesis_evidence_adjudications", row)


@pytest.mark.parametrize(
    "private_key",
    (
        "account_balance",
        "account/balance",
        "account_number",
        "api_key",
        "assets",
        "authorization_header",
        "authorizationHeader",
        "current_value",
        "currentValue",
        "debt",
        "holding_value",
        "localFilePath",
        "memo",
        "note",
        "password",
        "profile",
        "raw_headers",
        "session_id",
    ),
)
def test_review_snapshot_rejects_private_result_keys(
    tmp_path: Path,
    private_key: str,
) -> None:
    repository = Repository(tmp_path / f"private-{private_key}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?",
                (fixture["review_id"],),
            ).fetchone()
        )
        row.pop("id")
        payload = json.loads(str(row["result_json"]))
        payload[private_key] = "private"
        row["result_json"] = _compact(payload)
        row["result_fingerprint"] = CHECKSUMS[8]
        row["semantic_identity_checksum"] = CHECKSUMS[9]
        row["record_checksum"] = CHECKSUMS[10]
        with pytest.raises(sqlite3.IntegrityError, match="private key"):
            _insert_mapping(connection, "holding_review_snapshots", row)


def test_review_snapshot_requires_owner_context_keys(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "missing-result-keys.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?",
                (fixture["review_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["result_json"] = "{}"
        row["result_fingerprint"] = CHECKSUMS[8]
        row["semantic_identity_checksum"] = CHECKSUMS[9]
        row["record_checksum"] = CHECKSUMS[10]
        with pytest.raises(sqlite3.IntegrityError, match="snapshot binding"):
            _insert_mapping(connection, "holding_review_snapshots", row)


def test_review_history_pointer_cannot_bypass_semantic_identity(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "semantic-fields.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        fixture = _phase5_fixture(connection)
        row = dict(
            connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?",
                (fixture["review_id"],),
            ).fetchone()
        )
        row.pop("id")
        row["created_at"] = "2026-07-20T00:02:00+00:00"
        row["previous_review_id"] = fixture["review_id"]
        row["semantic_identity_checksum"] = CHECKSUMS[9]
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced|UNIQUE"):
            _insert_mapping(connection, "holding_review_snapshots", row)


@pytest.mark.parametrize(
    "table",
    sorted(EXPECTED_PHASE5_TABLES),
)
def test_phase5_logical_records_reject_insert_or_replace(tmp_path: Path, table: str) -> None:
    repository = Repository(tmp_path / f"replace-{table}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        if table == "fund_official_announcement_contents":
            fixture = _parent_fixture(connection)
            _insert_official_content(connection, fixture)
        elif table == "held_review_official_event_projections":
            fixture = _parent_fixture(connection)
            content_id = _insert_official_content(connection, fixture)
            _insert_official_event(connection, fixture, content_id)
        else:
            fixture = _phase5_fixture(connection)
        if table == "thesis_evidence_adjudications":
            connection.execute("DROP TRIGGER holding_review_snapshot_no_delete")
            connection.execute("DELETE FROM holding_review_snapshots")
        row = dict(connection.execute(f"SELECT * FROM {table} LIMIT 1").fetchone())
        with pytest.raises(sqlite3.IntegrityError, match="cannot be replaced|adjudication binding"):
            _insert_mapping(connection, table, row, or_replace=True)
