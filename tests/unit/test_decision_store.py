from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

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
    ForceReasonCode,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceTier,
    WorkflowLevel,
)
from kunjin.decision.policy import EVIDENCE_POLICY_V1_CHECKSUM, EvidencePolicyV1
from kunjin.decision.source_registry import (
    SOURCE_REGISTRY_V1_CHECKSUM,
    SourceRegistryV1,
)
from kunjin.decision.store import (
    MAX_CANONICAL_ROUTE_BYTES,
    MAX_CANONICAL_ROUTE_DEPTH,
    DecisionAuditStore,
    DecisionAuditStoreError,
)
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
REQUEST_ID = "0123456789abcdef0123456789abcdef"


def _store(tmp_path) -> tuple[Repository, DecisionAuditStore]:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    return repository, DecisionAuditStore(repository)


class _PausingRepository(Repository):
    def __init__(
        self,
        database,
        *,
        function_name: str,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(database)
        self.function_name = function_name
        self.entered = entered
        self.release = release

    @contextmanager
    def connect(self):
        with super().connect() as connection:
            connection.create_function(self.function_name, 0, self._pause)
            yield connection

    def _pause(self) -> int:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test transaction pause timed out")
        return 0


class _SelectPausingCursor:
    def __init__(
        self,
        cursor,
        *,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self.cursor = cursor
        self.entered = entered
        self.release = release

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is not None and row["status"] == "running":
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test select pause timed out")
        return row

    def __getattr__(self, name: str):
        return getattr(self.cursor, name)


class _SelectPausingConnection:
    def __init__(
        self,
        connection,
        *,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        self.connection = connection
        self.entered = entered
        self.release = release

    def execute(self, sql: str, parameters=()):
        cursor = self.connection.execute(sql, parameters)
        normalized = " ".join(sql.split()).casefold()
        if normalized.startswith("select ") and " from request_runs where id = ?" in normalized:
            return _SelectPausingCursor(
                cursor,
                entered=self.entered,
                release=self.release,
            )
        return cursor

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.connection.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


class _SelectPausingRepository(Repository):
    def __init__(
        self,
        database,
        *,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(database)
        self.entered = entered
        self.release = release

    @contextmanager
    def connect(self):
        with super().connect() as connection:
            yield _SelectPausingConnection(
                connection,
                entered=self.entered,
                release=self.release,
            )


def _pausing_store(tmp_path, *, operation: str, table: str):
    entered = threading.Event()
    release = threading.Event()
    function_name = "kunjin_test_pause"
    repository = _PausingRepository(
        tmp_path / "kunjin.db",
        function_name=function_name,
        entered=entered,
        release=release,
    )
    repository.migrate()
    with repository.connect() as connection, connection:
        connection.execute(
            f"CREATE TRIGGER kunjin_test_pause_trigger "  # noqa: S608
            f"BEFORE {operation} ON {table} "
            f"BEGIN SELECT {function_name}(); END"
        )
    return repository, DecisionAuditStore(repository), entered, release


def _select_pausing_store(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    repository = _SelectPausingRepository(
        tmp_path / "kunjin.db",
        entered=entered,
        release=release,
    )
    repository.migrate()
    return repository, DecisionAuditStore(repository), entered, release


def _capture_thread_result(results: dict, key: str, action) -> None:
    try:
        results[key] = action()
    except BaseException as exc:  # test boundary records the exact thread outcome
        results[key] = exc


def _join_thread(thread: threading.Thread) -> None:
    thread.join(timeout=5)
    assert not thread.is_alive()


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


def _write_child(
    store: DecisionAuditStore,
    request_run_id: int,
    child_kind: str,
):
    if child_kind == "attempt":
        return store.record_source_attempt(request_run_id, _attempt())
    assert child_kind == "snapshot"
    return store.save_decision_snapshot(
        request_run_id,
        _route(),
        EvidencePolicyV1(),
        SourceRegistryV1(),
        NOW + timedelta(seconds=3),
    )


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
    budget = _budget()
    request_run_id = store.begin_request(budget)

    with pytest.raises(DecisionAuditStoreError, match="attempt sequence"):
        store.record_source_attempt(request_run_id, _attempt(2))

    first = _attempt(
        1,
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=NOW + timedelta(minutes=30),
        response_bytes=0,
    )
    first_id = store.record_source_attempt(request_run_id, first)
    parent = store.source_attempt_history(
        first.source_id, first.field_id, first.subject_key
    )[0]
    authorization = store.reserve_retry(
        request_run_id,
        budget,
        parent,
        NOW + timedelta(seconds=2),
        minimum_worker_seconds=5.0,
    )
    assert authorization is not None
    second_id = store.record_source_attempt(
        request_run_id,
        _attempt(2),
        authorization,
    )
    history = store.source_attempt_history(
        "eastmoney_f10", "identity_active_status", "fund:123456"
    )

    assert [item.id for item in history] == [second_id, first_id]
    assert [item.attempt.attempt_number for item in history] == [2, 1]
    assert [item.request_id for item in history] == [REQUEST_ID, REQUEST_ID]
    with pytest.raises(DecisionAuditStoreError, match="attempt sequence"):
        store.record_source_attempt(request_run_id, _attempt(2))


def test_force_attempt_requires_deep_run_and_round_trips_once(tmp_path) -> None:
    _, store = _store(tmp_path)
    forced = replace(
        _attempt(),
        force_actor="local_owner",
        force_reason=ForceReasonCode.OWNER_APPROVED_RETRY,
    )
    rapid_budget = _budget(request_id="7" * 32)
    rapid_run_id = store.begin_request(rapid_budget)

    with pytest.raises(DecisionAuditStoreError, match="deep"):
        store.record_source_attempt(rapid_run_id, forced)
    with pytest.raises(DecisionAuditStoreError, match="deep"):
        store.reserve_force(
            rapid_run_id,
            rapid_budget,
            forced.source_id,
            forced.field_id,
            forced.subject_key,
            NOW + timedelta(seconds=1),
            ForceReasonCode.OWNER_APPROVED_RETRY,
        )

    deep_request_id = "8" * 32
    deep_budget = _budget(request_id=deep_request_id, mode=RequestMode.DEEP)
    deep_run_id = store.begin_request(deep_budget)
    with pytest.raises(DecisionAuditStoreError, match="authorization"):
        store.record_source_attempt(deep_run_id, forced)
    authorization = store.reserve_force(
        deep_run_id,
        deep_budget,
        forced.source_id,
        forced.field_id,
        forced.subject_key,
        NOW + timedelta(seconds=1),
        ForceReasonCode.OWNER_APPROVED_RETRY,
    )
    assert authorization is not None
    attempt_id = store.record_source_attempt(deep_run_id, forced, authorization)
    history = store.source_attempt_history(
        forced.source_id,
        forced.field_id,
        forced.subject_key,
    )

    assert [(item.id, item.request_id) for item in history] == [
        (attempt_id, deep_request_id)
    ]
    assert history[0].attempt.force_actor == "local_owner"
    assert history[0].attempt.force_reason is ForceReasonCode.OWNER_APPROVED_RETRY
    assert history[0].authorization_id == authorization.authorization_id

    wrong_identity = replace(
        forced,
        source_id="eastmoney_nav",
        field_id="formal_nav",
    )
    with pytest.raises(DecisionAuditStoreError, match="binding"):
        store.record_source_attempt(deep_run_id, wrong_identity, authorization)


def test_unconsumed_authorization_blocks_complete_and_requires_omitted_work(
    tmp_path,
) -> None:
    _, store = _store(tmp_path)
    budget = _budget(request_id="9" * 32, mode=RequestMode.DEEP)
    request_run_id = store.begin_request(budget)
    authorization = store.reserve_force(
        request_run_id,
        budget,
        "eastmoney_f10",
        "identity_active_status",
        "fund:123456",
        NOW + timedelta(seconds=1),
        ForceReasonCode.OWNER_APPROVED_RETRY,
    )
    assert authorization is not None

    with pytest.raises(DecisionAuditStoreError, match="exactly once"):
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.COMPLETE,
            NOW + timedelta(seconds=2),
            (),
        )
    with pytest.raises(DecisionAuditStoreError, match="exactly once"):
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.PARTIAL,
            NOW + timedelta(seconds=2),
            (),
        )

    store.finalize_request(
        request_run_id,
        RequestTerminalStatus.PARTIAL,
        NOW + timedelta(seconds=2),
        ("identity_active_status",),
    )


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


@pytest.mark.parametrize("child_kind", ("attempt", "snapshot"))
def test_child_transaction_commits_before_waiting_finalize_transaction(
    tmp_path, child_kind: str
) -> None:
    repository, store, child_entered, release_child = _select_pausing_store(tmp_path)
    request_run_id = store.begin_request(_budget())
    results = {}
    child_done = threading.Event()
    finalize_done = threading.Event()

    def write_child() -> None:
        _capture_thread_result(
            results,
            "child",
            lambda: _write_child(store, request_run_id, child_kind),
        )
        child_done.set()

    def finalize() -> None:
        _capture_thread_result(
            results,
            "finalize",
            lambda: store.finalize_request(
                request_run_id,
                RequestTerminalStatus.COMPLETE,
                NOW + timedelta(seconds=4),
                (),
            ),
        )
        finalize_done.set()

    child_thread = threading.Thread(target=write_child, daemon=True)
    finalize_thread = threading.Thread(target=finalize, daemon=True)
    child_thread.start()
    assert child_entered.wait(timeout=2)
    finalize_thread.start()
    try:
        assert not finalize_done.wait(timeout=0.1)
    finally:
        release_child.set()
    _join_thread(child_thread)
    _join_thread(finalize_thread)

    assert child_done.is_set()
    assert not isinstance(results["child"], BaseException)
    assert results["finalize"] is None
    child_table = "source_attempts" if child_kind == "attempt" else "decision_snapshots"
    with repository.connect() as connection:
        status = connection.execute(
            "SELECT status FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()[0]
        child_count = connection.execute(
            f"SELECT count(*) FROM {child_table}"  # noqa: S608
        ).fetchone()[0]
    assert status == "complete"
    assert child_count == 1


@pytest.mark.parametrize("child_kind", ("attempt", "snapshot"))
def test_finalize_transaction_commits_before_waiting_child_is_rejected(
    tmp_path, child_kind: str
) -> None:
    repository, store, finalize_entered, release_finalize = _pausing_store(
        tmp_path, operation="UPDATE", table="request_runs"
    )
    request_run_id = store.begin_request(_budget())
    results = {}
    child_done = threading.Event()
    finalize_done = threading.Event()

    def finalize() -> None:
        _capture_thread_result(
            results,
            "finalize",
            lambda: store.finalize_request(
                request_run_id,
                RequestTerminalStatus.COMPLETE,
                NOW + timedelta(seconds=4),
                (),
            ),
        )
        finalize_done.set()

    def write_child() -> None:
        _capture_thread_result(
            results,
            "child",
            lambda: _write_child(store, request_run_id, child_kind),
        )
        child_done.set()

    finalize_thread = threading.Thread(target=finalize, daemon=True)
    child_thread = threading.Thread(target=write_child, daemon=True)
    finalize_thread.start()
    assert finalize_entered.wait(timeout=2)
    child_thread.start()
    try:
        assert not child_done.wait(timeout=0.1)
    finally:
        release_finalize.set()
    _join_thread(finalize_thread)
    _join_thread(child_thread)

    assert finalize_done.is_set()
    assert results["finalize"] is None
    assert isinstance(results["child"], DecisionAuditStoreError)
    assert "not running" in str(results["child"])
    child_table = "source_attempts" if child_kind == "attempt" else "decision_snapshots"
    with repository.connect() as connection:
        status = connection.execute(
            "SELECT status FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()[0]
        child_count = connection.execute(
            f"SELECT count(*) FROM {child_table}"  # noqa: S608
        ).fetchone()[0]
    assert status == "complete"
    assert child_count == 0


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


@pytest.mark.parametrize(
    "created_at",
    (NOW - timedelta(microseconds=1), NOW + timedelta(seconds=90, microseconds=1)),
)
def test_snapshot_creation_must_be_inside_request_lifetime(
    tmp_path, created_at: datetime
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())

    with pytest.raises(DecisionAuditStoreError, match="request lifetime"):
        store.save_decision_snapshot(
            request_run_id,
            _route(),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            created_at,
        )

    with repository.connect() as connection:
        count = connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize("child_kind", ("attempt", "snapshot"))
def test_finalize_cannot_precede_latest_child_evidence(
    tmp_path, child_kind: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    _write_child(store, request_run_id, child_kind)

    with pytest.raises(DecisionAuditStoreError, match="exactly once"):
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.COMPLETE,
            NOW + timedelta(seconds=1),
            (),
        )

    with repository.connect() as connection:
        run = connection.execute(
            "SELECT status, finished_at FROM request_runs WHERE id = ?",
            (request_run_id,),
        ).fetchone()
    assert tuple(run) == ("running", None)


def test_expired_cleanup_may_finalize_after_request_deadline(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.record_source_attempt(request_run_id, _attempt())

    store.finalize_request(
        request_run_id,
        RequestTerminalStatus.EXPIRED,
        NOW + timedelta(seconds=91),
        ("market_context",),
    )


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


def test_static_policy_and_registry_bytes_are_authenticated_before_route_json_parse(
    tmp_path,
) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )

    with patch("kunjin.decision.store.json.loads", wraps=json.loads) as loads:
        store._load_decision_snapshot(request_run_id)

    assert loads.call_count == 1


@pytest.mark.parametrize(
    "tampered",
    (
        '{"padding":"' + ("x" * MAX_CANONICAL_ROUTE_BYTES) + '"}',
        '{"nested":' + ("[" * (MAX_CANONICAL_ROUTE_DEPTH + 1)) + "0"
        + ("]" * (MAX_CANONICAL_ROUTE_DEPTH + 1)) + "}",
    ),
    ids=("oversized", "too_deep"),
)
def test_snapshot_read_rejects_oversized_or_too_deep_route_before_json_parse(
    tmp_path, tampered: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            """
            UPDATE decision_snapshots
            SET canonical_route_json = ?, result_checksum = ?
            """,
            (tampered, hashlib.sha256(tampered.encode("ascii")).hexdigest()),
        )

    with (
        patch(
            "kunjin.decision.store.json.loads",
            side_effect=AssertionError("bounded route reached JSON parser"),
        ),
        pytest.raises(DecisionAuditStoreError, match="snapshot authentication"),
    ):
        store._load_decision_snapshot(request_run_id)


def test_snapshot_read_wraps_unexpected_json_recursion(tmp_path) -> None:
    _, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )

    with (
        patch("kunjin.decision.store.json.loads", side_effect=RecursionError),
        pytest.raises(DecisionAuditStoreError, match="snapshot authentication"),
    ):
        store._load_decision_snapshot(request_run_id)


def test_snapshot_write_rejects_oversized_canonical_route(tmp_path) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    oversized = b"{" + (b"x" * MAX_CANONICAL_ROUTE_BYTES) + b"}"

    with (
        patch.object(DecisionRoute, "canonical_json", return_value=oversized),
        pytest.raises(DecisionAuditStoreError, match="route is too large"),
    ):
        store.save_decision_snapshot(
            request_run_id,
            _route(),
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )

    with repository.connect() as connection:
        count = connection.execute("SELECT count(*) FROM decision_snapshots").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    ("json_column", "checksum_column"),
    (
        ("evidence_policy_json", "evidence_policy_checksum"),
        ("source_registry_json", "source_registry_checksum"),
        ("canonical_route_json", "result_checksum"),
    ),
)
def test_snapshot_preflight_rejects_oversized_body_before_materializing_json(
    tmp_path, json_column: str, checksum_column: str
) -> None:
    repository, store = _store(tmp_path)
    request_run_id = store.begin_request(_budget())
    store.save_decision_snapshot(
        request_run_id, _route(), EvidencePolicyV1(), SourceRegistryV1(), NOW
    )
    oversized = '{"padding":"' + ("x" * MAX_CANONICAL_ROUTE_BYTES) + '"}'
    checksum = hashlib.sha256(oversized.encode("ascii")).hexdigest()
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER decision_snapshot_no_update")
        connection.execute(
            f"UPDATE decision_snapshots "  # noqa: S608
            f"SET {json_column} = ?, {checksum_column} = ?",
            (oversized, checksum),
        )

    traced_sql = []
    with repository.connect() as connection:
        connection.set_trace_callback(traced_sql.append)
        with pytest.raises(DecisionAuditStoreError, match="snapshot authentication"):
            store._load_decision_snapshot(request_run_id, connection=connection)

    normalized = [" ".join(statement.split()).casefold() for statement in traced_sql]
    assert any("length(cast(evidence_policy_json as blob))" in item for item in normalized)
    assert all("decision_snapshots.*" not in item for item in normalized)
    assert not any(
        item.startswith(
            "select evidence_policy_json, source_registry_json, canonical_route_json"
        )
        for item in normalized
    )


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
