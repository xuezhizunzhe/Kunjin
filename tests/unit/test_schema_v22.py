from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kunjin.storage.repository import Repository
from kunjin.storage.schema import SCHEMA_VERSION
from tests.unit.test_schema_v21 import _create_version, _legacy_bytes

UTC = "2026-07-20T00:00:00+00:00"
FINISHED = "2026-07-20T00:01:00+00:00"
DEADLINE = "2026-07-20T00:30:00+00:00"
WINDOW_START = "2026-01-21T00:00:00+00:00"
FUND_CODE = "123456"
DIGESTS = tuple(character * 64 for character in "abcdef1234567890")


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _seed(connection: sqlite3.Connection, *, mode: str = "deep", outcome: str = "success"):
    request_id = int(
        connection.execute(
            """
            INSERT INTO request_runs(
                request_id, mode, status, started_at, deadline_at, finished_at,
                omitted_work_json
            ) VALUES (?, ?, 'running', ?, ?, NULL, '[]')
            """,
            ("d" * 32, mode, UTC, DEADLINE),
        ).lastrowid
    )
    if outcome == "success":
        data_as_of, error_code = FINISHED, None
    else:
        data_as_of, error_code = None, "source_contract_unsupported"
    attempt_id = int(
        connection.execute(
            """
            INSERT INTO source_attempts(
                request_run_id, source_id, field_id, subject_key, attempt_number,
                outcome, started_at, finished_at, data_as_of, error_code,
                cooldown_until, force_actor, force_reason, registry_version,
                registry_checksum, response_byte_count, authorization_id
            ) VALUES (?, 'fund_manager_official_documents',
                      'fund_manager_product_announcement', ?, 1, ?, ?, ?, ?, ?,
                      NULL, NULL, NULL, '1', ?, ?, NULL)
            """,
            (
                request_id,
                f"fund:{FUND_CODE}",
                outcome,
                UTC,
                FINISHED,
                data_as_of,
                error_code,
                "c876085a132026afab288a0a7022b7b29389fe36de4bcf9dba85a204c986953e",
                100 if outcome == "success" else 0,
            ),
        ).lastrowid
    )
    manager_document_id = int(
        connection.execute(
            """
            INSERT INTO fund_source_documents(
                fund_code, document_kind, title, url, source_name, source_tier,
                publisher, published_at, retrieved_at, checksum
            ) VALUES (?, 'basic_profile', 'profile', 'https://www.fund001.com/profile',
                      'eastmoney_f10', 2, '东方财富', ?, ?, ?)
            """,
            (FUND_CODE, UTC, UTC, DIGESTS[1]),
        ).lastrowid
    )
    manager_identity_id = int(
        connection.execute(
            """
            INSERT INTO fund_identities(
                fund_code, record_key, fund_name, status, fund_type,
                established_date, manager_name, source_document_id
            ) VALUES (?, 'identity', '测试基金', 'active', 'mixed', NULL,
                      '交银施罗德基金管理有限公司', ?)
            """,
            (FUND_CODE, manager_document_id),
        ).lastrowid
    )
    connection.execute(
        """
        INSERT INTO fund_section_syncs(
            fund_code, section, state, current_source_document_id,
            last_attempted_at, last_success_at, warning, error_code, error_message
        ) VALUES (?, 'basic_profile', 'success', ?, ?, ?, NULL, NULL, NULL)
        """,
        (FUND_CODE, manager_document_id, UTC, UTC),
    )
    page_document_id = int(
        connection.execute(
            """
            INSERT INTO fund_source_documents(
                fund_code, document_kind, title, url, source_name, source_tier,
                publisher, published_at, retrieved_at, checksum
            ) VALUES (?, 'announcement', 'official listing',
                      'https://www.fund001.com/fund/123456/sxxpl.shtml',
                      'fund_manager_official_documents', 1,
                      '交银施罗德基金管理有限公司', NULL, ?, ?)
            """,
            (FUND_CODE, FINISHED, DIGESTS[2]),
        ).lastrowid
    )
    return request_id, attempt_id, manager_document_id, manager_identity_id, page_document_id


def _closure_values(seed, *, complete: bool = True) -> dict[str, object]:
    request_id, attempt_id, manager_document_id, manager_identity_id, page_document_id = seed
    page_evidence = [
        {
            "canonical_page_url": "https://www.fund001.com/fund/123456/sxxpl.shtml",
            "page_number": 1,
            "parsed_item_count": 0,
            "parsed_items_sha256": DIGESTS[3],
            "raw_byte_count": 100,
            "raw_sha256": DIGESTS[2],
            "registration_id": "fund001",
            "reported_total_pages": 1,
            "retrieved_at": FINISHED,
            "source_document_id": page_document_id,
            "terminal_state": "source_final_page",
        }
    ]
    return {
        "brief_request_run_id": request_id,
        "fund_code": FUND_CODE,
        "listing_source_attempt_id": attempt_id,
        "official_registry_version": "1",
        "official_registry_checksum": (
            "557cac191734fbdd214ff24dabfc5afa8e3c99c1ab8ac30f230a846684c3fc9e"
        ),
        "source_registration_ids_json": _compact(["fund001"]),
        "manager_identity_state": "present",
        "manager_identity_row_id": manager_identity_id,
        "manager_identity_source_document_id": manager_document_id,
        "manager_identity_source_document_checksum": DIGESTS[1],
        "manager_identity_normalized_name": "交银施罗德基金管理有限公司",
        "manager_identity_fingerprint": DIGESTS[5],
        "listing_page_evidence_json": _compact(page_evidence),
        "window_start": WINDOW_START,
        "window_end": UTC,
        "listing_count": 0,
        "candidate_count": 0,
        "authenticated_body_count": 0,
        "projected_event_count": 0,
        "listing_truncated": 0,
        "candidate_cap_reached": 0,
        "body_cap_reached": 0,
        "gap_codes_json": "[]",
        "official_negative_check_complete": int(complete),
        "policy_version": "1",
        "policy_checksum": (
            "a78f01681f5b45dcbc9a264cfbdb2ee9805c30c7dab8583903ee60a83956fc46"
        ),
        "official_check_policy_version": "1",
        "official_check_policy_checksum": (
            "93722946c100518229531c79cabf606c23cf169536bd5c1d3213e3bf5836cb1b"
        ),
        "created_at": FINISHED,
        "record_checksum": DIGESTS[8],
    }


def _insert(connection: sqlite3.Connection, values: dict[str, object], *, replace=False):
    operation = "INSERT OR REPLACE" if replace else "INSERT"
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    return connection.execute(
        f"{operation} INTO held_review_official_check_closures({columns}) "
        f"VALUES ({placeholders})",
        tuple(values.values()),
    )


def test_v22_is_additive_and_preserves_v21_bytes(tmp_path: Path) -> None:
    repository = _create_version(tmp_path / "v21.db", 21)
    before = _legacy_bytes(repository)
    repository.migrate()
    with repository.connect() as connection:
        versions = tuple(
            row["version"]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
        )
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='held_review_official_check_closures'"
        ).fetchone()
    assert SCHEMA_VERSION == 22
    assert versions == tuple(range(1, 23))
    assert table is not None
    assert _legacy_bytes(repository) == before


def test_v22_accepts_complete_zero_candidate_closure(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "complete.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        closure_id = int(_insert(connection, _closure_values(seed)).lastrowid)
        assert closure_id > 0


def test_v22_rejects_closure_created_after_request_deadline(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "late-closure.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        values["created_at"] = "2026-07-20T00:31:00+00:00"

        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)


@pytest.mark.parametrize(
    "change",
    (
        {"official_negative_check_complete": 1, "gap_codes_json": '["source_failed"]'},
        {"official_negative_check_complete": 1, "listing_truncated": 1},
        {"official_negative_check_complete": 1, "candidate_cap_reached": 1},
        {"official_negative_check_complete": 1, "body_cap_reached": 1},
        {"official_negative_check_complete": 1, "candidate_count": 1},
        {"manager_identity_state": "missing"},
    ),
)
def test_v22_rejects_invalid_complete_closure(tmp_path: Path, change) -> None:
    repository = Repository(tmp_path / "invalid.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        values.update(change)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)


def test_v22_allows_failed_attempt_only_for_incomplete_closure(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "failed.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection, outcome="unsupported")
        values = _closure_values(seed, complete=False)
        values.update(
            {
                "source_registration_ids_json": "[]",
                "manager_identity_state": "missing",
                "manager_identity_row_id": None,
                "manager_identity_source_document_id": None,
                "manager_identity_source_document_checksum": None,
                "manager_identity_normalized_name": None,
                "manager_identity_fingerprint": None,
                "listing_page_evidence_json": "[]",
                "gap_codes_json": '["official_manager_identity_unavailable"]',
            }
        )
        assert int(_insert(connection, values).lastrowid) > 0
        values["official_negative_check_complete"] = 1
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)


def test_v22_binds_current_manager_identity_and_exact_page_bytes(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "bindings.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        connection.execute(
            "DELETE FROM fund_section_syncs WHERE fund_code=? AND section='basic_profile'",
            (FUND_CODE,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)

    repository = Repository(tmp_path / "bytes.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        pages = json.loads(str(values["listing_page_evidence_json"]))
        pages[0]["raw_byte_count"] = 99
        values["listing_page_evidence_json"] = _compact(pages)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)


def test_v22_recomputes_complete_database_counts_and_page_document_binding(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "counts.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        values.update(
            {
                "listing_count": 1,
                "candidate_count": 1,
                "authenticated_body_count": 1,
                "projected_event_count": 1,
            }
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)

    repository = Repository(tmp_path / "page-document.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        values = _closure_values(seed)
        pages = json.loads(str(values["listing_page_evidence_json"]))
        pages[0]["raw_sha256"] = DIGESTS[9]
        values["listing_page_evidence_json"] = _compact(pages)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, values)


@pytest.mark.parametrize(
    "change",
    (
        {"id": -1},
        {"listing_count": None},
        {"official_check_policy_checksum": DIGESTS[9]},
        {"source_registration_ids_json": '["fund001","fund001"]'},
    ),
)
def test_v22_rejects_negative_null_policy_and_noncanonical_identity_inputs(
    tmp_path: Path,
    change,
) -> None:
    repository = Repository(tmp_path / f"invalid-{len(str(change))}.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, {**_closure_values(seed), **change})


def test_v22_rejects_rapid_and_is_append_only(tmp_path: Path) -> None:
    rapid_repository = Repository(tmp_path / "rapid.db")
    rapid_repository.migrate()
    with rapid_repository.connect() as connection, connection:
        rapid = _seed(connection, mode="rapid")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, _closure_values(rapid))

    repository = Repository(tmp_path / "guards.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        seed = _seed(connection)
        closure_id = int(_insert(connection, _closure_values(seed)).lastrowid)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE held_review_official_check_closures SET listing_count=1 WHERE id=?",
                (closure_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM held_review_official_check_closures WHERE id=?",
                (closure_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, {**_closure_values(seed), "id": closure_id}, replace=True)


def test_v22_closure_namespace_drift_is_owned_and_rejected(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "drift.db")
    repository.migrate()
    with repository.connect() as connection, connection:
        connection.execute(
            "CREATE TABLE held_review_official_check_closures_shadow(id INTEGER PRIMARY KEY)"
        )
    with pytest.raises(sqlite3.DatabaseError, match="held review schema does not match V22"):
        repository.migrate()
