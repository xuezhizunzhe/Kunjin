from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ActionState,
    ConclusionEvidence,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceTier,
    WorkflowLevel,
)
from kunjin.decision.policy import EVIDENCE_POLICY_V1_CHECKSUM, EvidencePolicyV1
from kunjin.decision.source_registry import (
    SOURCE_REGISTRY_V1_CHECKSUM,
    SourceRegistryV1,
)
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
REQUEST_ID = "0123456789abcdef0123456789abcdef"


def _store(tmp_path) -> tuple[Repository, DecisionAuditStore]:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    return repository, DecisionAuditStore(repository)


def _budget(
    request_id: str = REQUEST_ID,
    mode: RequestMode = RequestMode.RAPID,
) -> RequestBudget:
    return RequestBudget.create(
        mode,
        request_id=request_id,
        monotonic=lambda: 10.0,
        wall_clock=lambda: NOW,
    )


def _route(
    request_id: str = REQUEST_ID,
    mode: RequestMode = RequestMode.RAPID,
    conclusion_evidence: tuple[ConclusionEvidence, ...] = (),
) -> DecisionRoute:
    return DecisionRoute(
        request_id=request_id,
        mode=mode,
        workflow_level=WorkflowLevel.RAPID_EVIDENCE,
        actions=(
            ActionRoute(
                action_id="fact_research",
                action=ActionKind.FACT_RESEARCH,
                risk_effect=RiskEffect.INFORMATION,
                required_gates=(),
                blocking_codes=(),
                research_available=True,
                exact_amount_available=False,
                minimum_state=ActionState.RESEARCH_ONLY,
                action_maturity=ActionMaturity.MATURE,
            ),
        ),
        conclusion_evidence=conclusion_evidence,
        opposing_evidence=(),
        missing_fields=(),
        policy_version="1",
        policy_checksum=EVIDENCE_POLICY_V1_CHECKSUM,
        registry_version="1",
        registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
    )


def _complete_evidence() -> ConclusionEvidence:
    return ConclusionEvidence(
        source_tier=SourceTier.TIER_1,
        publishers=("Example Fund Manager", "Example Market Data Publisher"),
        source_ids=("fund_manager_official_documents", "eastmoney_nav"),
        publication_times=(NOW - timedelta(days=1), NOW - timedelta(hours=2)),
        market_as_of=NOW - timedelta(hours=1),
        report_as_of=NOW - timedelta(days=30),
        retrieved_at=NOW,
        independent_lineage_count=2,
        lineage_ids=("official_report", "formal_nav_series"),
        completeness=EvidenceCompleteness.COMPLETE,
        coverage_percent=Decimal("100.00"),
        freshness=EvidenceFreshness.CURRENT,
        conflicts=(),
        inferred=False,
        missing_critical_fields=(),
    )


def _attempt(number: int = 1, **overrides) -> SourceAttempt:
    values = {
        "source_id": "eastmoney_f10",
        "field_id": "identity_active_status",
        "subject_key": "fund:123456",
        "attempt_number": number,
        "outcome": SourceAttemptOutcome.SUCCESS,
        "started_at": NOW + timedelta(seconds=number),
        "finished_at": NOW + timedelta(seconds=number + 1),
        "data_as_of": NOW,
        "error_code": None,
        "cooldown_until": None,
        "force_actor": None,
        "force_reason": None,
        "registry_version": "1",
        "registry_checksum": SOURCE_REGISTRY_V1_CHECKSUM,
        "response_bytes": number * 100,
    }
    values.update(overrides)
    return SourceAttempt(**values)


def test_request_attempt_snapshot_lifecycle_and_canonical_round_trip(tmp_path) -> None:
    repository, store = _store(tmp_path)
    budget = _budget()

    request_run_id = store.begin_request(budget)
    attempt_id = store.record_source_attempt(request_run_id, _attempt())
    snapshot = store.save_decision_snapshot(
        request_run_id,
        _route(),
        EvidencePolicyV1(),
        SourceRegistryV1(),
        NOW + timedelta(seconds=4),
    )
    store.finalize_request(
        request_run_id,
        RequestTerminalStatus.PARTIAL,
        NOW + timedelta(seconds=5),
        ("fees", "market_context"),
    )

    assert attempt_id > 0
    assert snapshot.request_run_id == request_run_id
    assert snapshot.route == _route()
    assert snapshot.policy == EvidencePolicyV1()
    assert snapshot.registry == SourceRegistryV1()
    assert snapshot.created_at == NOW + timedelta(seconds=4)

    with repository.connect() as connection:
        run = connection.execute(
            "SELECT * FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()
        persisted = connection.execute(
            "SELECT * FROM decision_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()
    assert run["request_id"] == REQUEST_ID
    assert run["status"] == "partial"
    assert run["omitted_work_json"] == '["fees","market_context"]'
    assert persisted["evidence_policy_json"].encode("ascii") == (
        EvidencePolicyV1().canonical_json()
    )
    assert persisted["source_registry_json"].encode("ascii") == (
        SourceRegistryV1().canonical_json()
    )
    assert persisted["canonical_route_json"].encode("ascii") == _route().canonical_json()


def test_complete_conclusion_evidence_round_trips_without_field_loss(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    route = _route(conclusion_evidence=(_complete_evidence(),))

    snapshot = store.save_decision_snapshot(
        request_run_id,
        route,
        EvidencePolicyV1(),
        SourceRegistryV1(),
        NOW + timedelta(seconds=1),
    )

    assert snapshot.route == route
    assert snapshot.route.conclusion_evidence == (_complete_evidence(),)
    assert snapshot.route.canonical_json() == route.canonical_json()


def test_attempts_are_sequential_bounded_and_history_is_newest_first(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())

    with pytest.raises(DecisionAuditStoreError, match="attempt sequence"):
        store.record_source_attempt(request_run_id, _attempt(2))

    first_id = store.record_source_attempt(request_run_id, _attempt(1))
    second_id = store.record_source_attempt(request_run_id, _attempt(2))
    history = store.source_attempt_history(
        "eastmoney_f10", "identity_active_status", "fund:123456"
    )

    assert [item.id for item in history] == [second_id, first_id]
    assert [item.attempt.attempt_number for item in history] == [2, 1]
    with pytest.raises(DecisionAuditStoreError, match="attempt sequence"):
        store.record_source_attempt(request_run_id, _attempt(2))


def test_terminal_request_is_immutable_and_rejects_more_children(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.finalize_request(
        request_run_id,
        RequestTerminalStatus.COMPLETE,
        NOW + timedelta(seconds=1),
        (),
    )

    with pytest.raises(DecisionAuditStoreError, match="finalized"):
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.FAILED,
            NOW + timedelta(seconds=2),
            ("identity_active_status",),
        )
    with pytest.raises(DecisionAuditStoreError, match="running"):
        store.record_source_attempt(request_run_id, _attempt())
    with pytest.raises(DecisionAuditStoreError, match="running"):
        store.save_decision_snapshot(
            request_run_id,
            _route(),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW + timedelta(seconds=3),
        )


def test_attempt_insert_requires_exact_request_owner_and_record_type(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())

    with pytest.raises(DecisionAuditStoreError, match="request run"):
        store.record_source_attempt(request_run_id + 1, _attempt())
    with pytest.raises(ValueError, match="SourceAttempt"):
        store.record_source_attempt(request_run_id, {"attempt_number": 1})  # type: ignore[arg-type]


def test_attempt_insert_rejects_registry_version_without_authenticatable_bytes(
    tmp_path,
) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())

    with pytest.raises(DecisionAuditStoreError, match="registry binding"):
        store.record_source_attempt(
            request_run_id,
            _attempt(registry_version="2", registry_checksum="a" * 64),
        )


def test_aware_non_utc_api_times_are_canonicalized_for_storage(tmp_path) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    utc_plus_eight = timezone(timedelta(hours=8))
    attempt = replace(
        _attempt(),
        started_at=(NOW + timedelta(seconds=1)).astimezone(utc_plus_eight),
        finished_at=(NOW + timedelta(seconds=2)).astimezone(utc_plus_eight),
        data_as_of=NOW.astimezone(utc_plus_eight),
    )

    store.record_source_attempt(request_run_id, attempt)
    snapshot = store.save_decision_snapshot(
        request_run_id,
        _route(),
        EvidencePolicyV1(),
        SourceRegistryV1(),
        (NOW + timedelta(seconds=3)).astimezone(utc_plus_eight),
    )
    store.finalize_request(
        request_run_id,
        RequestTerminalStatus.COMPLETE,
        (NOW + timedelta(seconds=4)).astimezone(utc_plus_eight),
        (),
    )

    history = store.source_attempt_history(
        "eastmoney_f10", "identity_active_status", "fund:123456"
    )
    assert history[0].attempt == attempt
    assert snapshot.created_at == NOW + timedelta(seconds=3)
    with repository.connect() as connection:
        run = connection.execute(
            "SELECT finished_at FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()
    assert run["finished_at"] == (NOW + timedelta(seconds=4)).isoformat()


def test_snapshot_requires_request_mode_policy_and_registry_binding(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())

    with pytest.raises(DecisionAuditStoreError, match="request binding"):
        store.save_decision_snapshot(
            request_run_id,
            _route("f" * 32),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )
    with pytest.raises(DecisionAuditStoreError, match="policy binding"):
        store.save_decision_snapshot(
            request_run_id,
            replace(_route(), policy_checksum="a" * 64),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )
    with pytest.raises(DecisionAuditStoreError, match="registry binding"):
        store.save_decision_snapshot(
            request_run_id,
            replace(_route(), registry_checksum="a" * 64),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )

    deep_run_id = store.begin_request(_budget("a" * 32, RequestMode.DEEP))
    with pytest.raises(DecisionAuditStoreError, match="request binding"):
        store.save_decision_snapshot(
            deep_run_id,
            _route("a" * 32, RequestMode.RAPID),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )


def _tamper_snapshot(repository: Repository, column: str, value: str) -> None:
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            f"UPDATE decision_snapshots SET {column} = ?",  # noqa: S608
            (value,),
        )


@pytest.mark.parametrize(
    "column",
    ("evidence_policy_json", "source_registry_json", "canonical_route_json"),
)
def test_snapshot_read_fails_closed_when_any_canonical_byte_changes(
    tmp_path, column: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    with repository.connect() as connection:
        original = str(
            connection.execute(f"SELECT {column} FROM decision_snapshots").fetchone()[0]
        )
    changed = original.replace("rapid", "deep", 1)
    if changed == original:
        changed = original.replace('"version":"1"', '"version":"2"', 1)
    assert changed != original
    _tamper_snapshot(repository, column, changed)

    with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
        store._load_decision_snapshot(request_run_id)


@pytest.mark.parametrize(
    "column",
    (
        "evidence_policy_checksum",
        "source_registry_checksum",
        "result_checksum",
    ),
)
def test_snapshot_read_fails_closed_when_a_checksum_column_changes(
    tmp_path, column: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    _tamper_snapshot(repository, column, "a" * 64)

    with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
        store._load_decision_snapshot(request_run_id)


@pytest.mark.parametrize(
    ("json_column", "checksum_column"),
    (
        ("evidence_policy_json", "evidence_policy_checksum"),
        ("source_registry_json", "source_registry_checksum"),
        ("canonical_route_json", "result_checksum"),
    ),
)
def test_snapshot_read_rejects_noncanonical_json_even_with_matching_checksum(
    tmp_path, json_column: str, checksum_column: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        original = str(
            connection.execute(
                f"SELECT {json_column} FROM decision_snapshots"  # noqa: S608
            ).fetchone()[0]
        )
        noncanonical = original + " "
        connection.execute(
            f"UPDATE decision_snapshots "  # noqa: S608
            f"SET {json_column} = ?, {checksum_column} = ?",
            (
                noncanonical,
                hashlib.sha256(noncanonical.encode("ascii")).hexdigest(),
            ),
        )
    with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
        store._load_decision_snapshot(request_run_id)


@pytest.mark.parametrize(
    ("json_column", "checksum_column"),
    (
        ("evidence_policy_json", "evidence_policy_checksum"),
        ("source_registry_json", "source_registry_checksum"),
        ("canonical_route_json", "result_checksum"),
    ),
)
def test_snapshot_read_rejects_invalid_json_even_with_matching_checksum(
    tmp_path, json_column: str, checksum_column: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    invalid = "{"
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE decision_snapshots "  # noqa: S608
            f"SET {json_column} = ?, {checksum_column} = ?",
            (invalid, hashlib.sha256(invalid.encode("ascii")).hexdigest()),
        )
    with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
        store._load_decision_snapshot(request_run_id)


def test_snapshot_read_rechecks_request_binding_even_with_matching_checksum(tmp_path) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    route = json.loads(_route().canonical_json())
    route["request_id"] = "f" * 32
    tampered = json.dumps(route, separators=(",", ":"), sort_keys=True)
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            """
            UPDATE decision_snapshots
            SET canonical_route_json = ?, result_checksum = ?
            """,
            (tampered, hashlib.sha256(tampered.encode("ascii")).hexdigest()),
        )

    with pytest.raises(DecisionAuditStoreError, match="request binding"):
        store._load_decision_snapshot(request_run_id)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("mode", "deep", "request binding"),
        ("policy_version", "2", "policy binding"),
        ("registry_version", "2", "registry binding"),
    ),
)
def test_snapshot_read_rechecks_route_bindings_with_matching_result_checksum(
    tmp_path, field: str, value: str, error: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    route = json.loads(_route().canonical_json())
    route[field] = value
    tampered = json.dumps(route, separators=(",", ":"), sort_keys=True)
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            """
            UPDATE decision_snapshots
            SET canonical_route_json = ?, result_checksum = ?
            """,
            (tampered, hashlib.sha256(tampered.encode("ascii")).hexdigest()),
        )

    with pytest.raises(DecisionAuditStoreError, match=error):
        store._load_decision_snapshot(request_run_id)


def test_snapshot_read_fails_closed_on_invalid_evidence_decimal(tmp_path) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    route = _route(conclusion_evidence=(_complete_evidence(),))
    store.save_decision_snapshot(
        request_run_id, route, EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    tampered_route = json.loads(route.canonical_json())
    tampered_route["conclusion_evidence"][0]["coverage_percent"] = "not_decimal"
    tampered = json.dumps(tampered_route, separators=(",", ":"), sort_keys=True)
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            """
            UPDATE decision_snapshots
            SET canonical_route_json = ?, result_checksum = ?
            """,
            (tampered, hashlib.sha256(tampered.encode("ascii")).hexdigest()),
        )

    with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
        store._load_decision_snapshot(request_run_id)


def test_attempt_history_fails_closed_on_registry_tampering(tmp_path) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.record_source_attempt(request_run_id, _attempt())
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER source_attempt_no_update")
        connection.execute(
            "UPDATE source_attempts SET registry_checksum = ?", ("a" * 64,)
        )

    with pytest.raises(DecisionAuditStoreError, match="attempt authentication"):
        store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:123456"
        )
