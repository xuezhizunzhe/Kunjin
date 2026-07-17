from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefCoverage,
    BriefEvidenceState,
    BriefFact,
    BriefSnapshot,
    BriefState,
    RelationshipEvidence,
)
from kunjin.brief.policy import HeldFundBriefPolicyV1
from kunjin.brief.store import (
    MAX_BRIEF_POLICY_JSON_BYTES,
    MAX_BRIEF_SNAPSHOT_JSON_BYTES,
    MAX_BRIEF_SUMMARY_ITEMS,
    MAX_BRIEF_SUMMARY_JSON_BYTES,
    BriefStore,
    BriefStoreError,
    _array_json,
    _strict_json,
)
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ActionState,
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
from kunjin.decision.store import DecisionAuditStore
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 17, 6, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64
PRIVATE_SQL_KEYS = (
    "exact_amount_available",
    "portfolio_weight",
    "shares",
    "observed_profit",
    "access_token",
    "proposed_amount",
    "purchase_cost",
    "position_value",
    "raw_body",
    "managed_path",
)


def _budget(request_id: str = "1" * 32) -> RequestBudget:
    return RequestBudget.create(
        RequestMode.RAPID,
        request_id=request_id,
        monotonic=lambda: 10.0,
        wall_clock=lambda: NOW,
    )


def _route(request_id: str = "1" * 32) -> DecisionRoute:
    return DecisionRoute(
        request_id=request_id,
        mode=RequestMode.RAPID,
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
            ActionRoute(
                action_id="continue_holding",
                action=ActionKind.CONTINUE_HOLDING,
                risk_effect=RiskEffect.RISK_MAINTAINING,
                required_gates=("phase_b_context", "phase_e_policy"),
                blocking_codes=(),
                research_available=True,
                exact_amount_available=False,
                minimum_state=ActionState.EXPERIMENTAL_SHADOW,
                action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
            ),
        ),
        conclusion_evidence=(),
        opposing_evidence=(),
        missing_fields=("owner_confirmed_thesis",),
        policy_version="1",
        policy_checksum=EVIDENCE_POLICY_V1_CHECKSUM,
        registry_version="1",
        registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
    )


def _fact(
    fact_id: str,
    field_id: str,
    value: str,
    source_lineage_id: str,
) -> BriefFact:
    return BriefFact(
        fact_id=fact_id,
        field_id=field_id,
        value=value,
        unit=None,
        data_as_of=NOW - timedelta(days=1),
        published_at=NOW - timedelta(hours=1),
        retrieved_at=NOW,
        source_id="eastmoney_f10",
        source_tier=SourceTier.TIER_2,
        publisher="Eastmoney",
        canonical_url=f"https://example.test/{fact_id}",
        freshness=EvidenceFreshness.CURRENT,
        completeness=EvidenceCompleteness.COMPLETE,
        conflict_ids=(),
        calculated=False,
        source_lineage_id=source_lineage_id,
    )


def _snapshot(
    request_run_id: int,
    decision_snapshot_id: int,
    *,
    state: BriefState = BriefState.WATCH,
    created_at: datetime = NOW + timedelta(seconds=1),
) -> BriefSnapshot:
    facts = (
        _fact("formal_nav_1", "formal_nav", "1.2345", "nav_lineage"),
        _fact("manager_fact_1", "current_manager_team", "Manager", "manager_lineage"),
    )
    relationship = RelationshipEvidence(
        relationship_id="same_manager_1",
        relationship_type="same_manager",
        fund_codes=("123456", "654321"),
        evidence_state=BriefEvidenceState.COMPLETE,
        metrics={"matched": True},
        evidence_ids=("manager_fact_1",),
        report_periods=(),
        publication_times=(NOW - timedelta(hours=1),),
        warnings=(),
    )
    coverage = BriefCoverage(
        coverage_id="portfolio_relationship_coverage",
        scope="current_fund_portfolio",
        evidence_state=BriefEvidenceState.PARTIAL,
        included_fund_codes=("123456",),
        omitted_fund_codes=("654321",),
        known_percent="50",
        unknown_fields=("industry_exposure",),
        evidence_ids=("same_manager_1",),
    )
    interpretation = BriefActionInterpretation(
        action_id="continue_holding",
        state=state,
        action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
        supporting_evidence_ids=("formal_nav_1",),
        opposing_evidence_ids=(),
        blocking_codes=(),
        missing_fields=("owner_confirmed_thesis",),
        invalidation_conditions=("Review when verified evidence changes.",),
        unavailable_actions=("exact_amount",),
        exact_amount_available=False,
        state_inputs={"owner_confirmed_thesis": False},
    )
    return BriefSnapshot(
        request_run_id=request_run_id,
        decision_snapshot_id=decision_snapshot_id,
        fund_code="123456",
        action_ids=("fact_research", "continue_holding"),
        mode=RequestMode.RAPID,
        facts=facts,
        official_events=(),
        relationships=(relationship,),
        coverage=coverage,
        interpretations=(interpretation,),
        primary_state=state,
        action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
        triggered_reviews=(),
        affected_action_abstentions=(),
        blocking_codes=(),
        evidence_state=BriefEvidenceState.PARTIAL,
        missing_fields=("owner_confirmed_thesis", "industry_exposure"),
        conflicts=(),
        source_lineage_ids=("nav_lineage", "manager_lineage"),
        evidence_fingerprint=CHECKSUM,
        created_at=created_at,
    )


def _stores(tmp_path: Path) -> tuple[Repository, DecisionAuditStore, BriefStore]:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    decision_store = DecisionAuditStore(repository)
    return repository, decision_store, BriefStore(repository, decision_store)


def _direct_snapshot_context(
    decision_store: DecisionAuditStore,
    request_id: str,
) -> BriefSnapshot:
    budget = _budget(request_id)
    request_run_id = decision_store.begin_request(budget)
    decision = decision_store.save_decision_snapshot(
        request_run_id,
        _route(request_id),
        EvidencePolicyV1(),
        SourceRegistryV1(),
        NOW + timedelta(seconds=1),
    )
    return _snapshot(request_run_id, decision.id)


def _direct_insert_snapshot(
    connection: sqlite3.Connection,
    snapshot: BriefSnapshot,
    *,
    canonical_snapshot_json: str,
    overrides: dict | None = None,
) -> None:
    values = {
        "request_run_id": snapshot.request_run_id,
        "decision_snapshot_id": snapshot.decision_snapshot_id,
        "fund_code": snapshot.fund_code,
        "action_ids_json": '["fact_research","continue_holding"]',
        "primary_state": snapshot.primary_state.value,
        "action_maturity": snapshot.action_maturity.value,
        "triggered_reviews_json": "[]",
        "affected_action_abstentions_json": "[]",
        "blocking_codes_json": "[]",
        "evidence_state": snapshot.evidence_state.value,
        "missing_fields_json": '["owner_confirmed_thesis","industry_exposure"]',
        "conflicts_json": "[]",
        "source_lineage_ids_json": '["nav_lineage","manager_lineage"]',
        "evidence_fingerprint": snapshot.evidence_fingerprint,
        "canonical_snapshot_json": canonical_snapshot_json,
        "result_checksum": hashlib.sha256(
            canonical_snapshot_json.encode("ascii")
        ).hexdigest(),
        "conclusion_changed": 0,
        "created_at": snapshot.created_at.isoformat(),
    }
    if overrides:
        values.update(overrides)
    columns = tuple(values)
    connection.execute(
        f"INSERT INTO fund_brief_snapshots({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _publish(
    decision_store: DecisionAuditStore,
    brief_store: BriefStore,
    *,
    request_id: str = "1" * 32,
    state: BriefState = BriefState.WATCH,
    created_at: datetime = NOW + timedelta(seconds=1),
    status: RequestTerminalStatus = RequestTerminalStatus.PARTIAL,
    omitted_work: tuple[str, ...] = ("industry_exposure",),
):
    budget = _budget(request_id)
    request_run_id = decision_store.begin_request(budget)
    calls = []

    def factory(real_request_run_id: int, real_decision_snapshot_id: int) -> BriefSnapshot:
        calls.append((real_request_run_id, real_decision_snapshot_id))
        return _snapshot(
            real_request_run_id,
            real_decision_snapshot_id,
            state=state,
            created_at=created_at,
        )

    stored = brief_store.publish(
        request_run_id=request_run_id,
        route=_route(request_id),
        evidence_policy=EvidencePolicyV1(),
        source_registry=SourceRegistryV1(),
        brief_policy=HeldFundBriefPolicyV1(),
        snapshot_factory=factory,
        created_at=created_at,
        finished_at=created_at + timedelta(seconds=1),
        status=status,
        omitted_work=omitted_work,
        budget=budget,
    )
    return request_run_id, calls, stored


def _nested_public_mapping(depth: int) -> object:
    value: object = False
    for _ in range(depth):
        value = {"nested": value}
    return value


def _insert_terminal_snapshot_with_state_inputs(
    repository: Repository,
    decision_store: DecisionAuditStore,
    *,
    request_id: str,
    state_inputs: object,
) -> None:
    policy = HeldFundBriefPolicyV1()
    with repository.connect() as connection, connection:
        BriefStore._authenticate_or_insert_policy(
            connection,
            policy,
            NOW.isoformat(),
        )
    snapshot = _direct_snapshot_context(decision_store, request_id)
    payload = json.loads(snapshot.canonical_json())
    payload["interpretations"][0]["state_inputs"] = state_inputs
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, connection:
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=canonical,
        )
        connection.execute(
            "UPDATE request_runs SET status = 'partial', finished_at = ?, "
            "omitted_work_json = '[\"industry_exposure\"]' WHERE id = ?",
            ((NOW + timedelta(seconds=2)).isoformat(), snapshot.request_run_id),
        )


def test_publish_uses_real_ids_once_and_atomically_authenticates_round_trip(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    request_run_id, calls, stored = _publish(decision_store, brief_store)

    assert calls == [(request_run_id, stored.snapshot.decision_snapshot_id)]
    assert stored.snapshot.request_run_id == request_run_id
    assert stored.policy == HeldFundBriefPolicyV1()
    assert stored.result_checksum == stored.snapshot.checksum()
    assert stored.conclusion_changed is False
    with repository.connect() as connection:
        run = connection.execute(
            "SELECT status, omitted_work_json FROM request_runs WHERE id = ?",
            (request_run_id,),
        ).fetchone()
        decision_count = connection.execute(
            "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0]
        brief_count = connection.execute(
            "SELECT count(*) FROM fund_brief_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0]
    assert tuple(run) == ("partial", '["industry_exposure"]')
    assert decision_count == brief_count == 1


def test_publish_rejects_factory_contract_and_rolls_back_sanitized(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    budget = _budget()
    request_run_id = decision_store.begin_request(budget)
    attempt = SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key="fund:123456",
        attempt_number=1,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=NOW,
        finished_at=NOW + timedelta(microseconds=1),
        data_as_of=NOW,
        error_code=None,
        cooldown_until=None,
        force_actor=None,
        force_reason=None,
        registry_version="1",
        registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
        response_bytes=10,
    )
    attempt_id = decision_store.record_source_attempt(request_run_id, attempt)
    calls = []

    def broken_factory(real_run_id: int, real_decision_id: int) -> BriefSnapshot:
        calls.append((real_run_id, real_decision_id))
        raise RuntimeError("private-factory-sentinel")

    with pytest.raises(BriefStoreError) as raised:
        brief_store.publish(
            request_run_id=request_run_id,
            route=_route(),
            evidence_policy=EvidencePolicyV1(),
            source_registry=SourceRegistryV1(),
            brief_policy=HeldFundBriefPolicyV1(),
            snapshot_factory=broken_factory,
            created_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=2),
            status=RequestTerminalStatus.PARTIAL,
            omitted_work=("industry_exposure",),
            budget=budget,
        )
    assert "private-factory-sentinel" not in str(raised.value)
    assert len(calls) == 1
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT status FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM fund_brief_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT id FROM source_attempts WHERE id = ?", (attempt_id,)
        ).fetchone() is not None

    with pytest.raises(ValueError, match="callable"):
        brief_store.publish(
            request_run_id=request_run_id,
            route=_route(),
            evidence_policy=EvidencePolicyV1(),
            source_registry=SourceRegistryV1(),
            brief_policy=HeldFundBriefPolicyV1(),
            snapshot_factory=None,
            created_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=2),
            status=RequestTerminalStatus.PARTIAL,
            omitted_work=(),
            budget=budget,
        )


def test_factory_must_return_exact_snapshot_with_real_bindings(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    cases = (
        lambda run_id, decision_id: object(),
        lambda run_id, decision_id: _snapshot(run_id + 1, decision_id),
        lambda run_id, decision_id: _snapshot(run_id, decision_id + 1),
    )
    for index, factory in enumerate(cases, start=2):
        request_id = f"{index:x}" * 32
        budget = _budget(request_id)
        request_run_id = decision_store.begin_request(budget)
        with pytest.raises(BriefStoreError):
            brief_store.publish(
                request_run_id=request_run_id,
                route=_route(request_id),
                evidence_policy=EvidencePolicyV1(),
                source_registry=SourceRegistryV1(),
                brief_policy=HeldFundBriefPolicyV1(),
                snapshot_factory=factory,
                created_at=NOW + timedelta(seconds=1),
                finished_at=NOW + timedelta(seconds=2),
                status=RequestTerminalStatus.PARTIAL,
                omitted_work=("industry_exposure",),
                budget=budget,
            )
        with repository.connect() as connection:
            assert connection.execute(
                "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
                (request_run_id,),
            ).fetchone()[0] == 0


def test_history_authenticates_is_bounded_and_uses_sanitized_conclusion(tmp_path) -> None:
    _, decision_store, brief_store = _stores(tmp_path)
    first = _publish(decision_store, brief_store, request_id="1" * 32)[2]
    second = _publish(
        decision_store,
        brief_store,
        request_id="2" * 32,
        created_at=NOW + timedelta(seconds=3),
    )[2]
    third = _publish(
        decision_store,
        brief_store,
        request_id="3" * 32,
        state=BriefState.ABSTAIN,
        created_at=NOW + timedelta(seconds=5),
    )[2]
    history = brief_store.history("123456")
    assert [item.id for item in history] == [third.id, second.id, first.id]
    assert [item.conclusion_changed for item in history] == [True, False, False]
    assert len(history) <= 64


def test_empty_history_does_not_require_a_policy_row(tmp_path) -> None:
    _, _, brief_store = _stores(tmp_path)
    assert brief_store.history("123456") == ()


def test_history_returns_at_most_64_authenticated_snapshots(tmp_path) -> None:
    _, decision_store, brief_store = _stores(tmp_path)
    for index in range(1, 66):
        _publish(
            decision_store,
            brief_store,
            request_id=f"{index:032x}",
            created_at=NOW + timedelta(seconds=index),
        )
    history = brief_store.history("123456")
    assert len(history) == 64
    assert all(item.snapshot.fund_code == "123456" for item in history)


@pytest.mark.parametrize(
    "private_key",
    PRIVATE_SQL_KEYS,
)
def test_direct_sql_rejects_private_snapshot_keys(tmp_path, private_key: str) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    _publish(decision_store, brief_store)
    request_id = f"{4 + PRIVATE_SQL_KEYS.index(private_key):x}" * 32
    budget = _budget(request_id)
    request_run_id = decision_store.begin_request(budget)
    decision = decision_store.save_decision_snapshot(
        request_run_id,
        _route(request_id),
        EvidencePolicyV1(),
        SourceRegistryV1(),
        NOW + timedelta(seconds=1),
    )
    snapshot = _snapshot(request_run_id, decision.id)
    payload = json.loads(snapshot.canonical_json())
    payload[private_key] = "redacted"
    tampered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    checksum = hashlib.sha256(tampered.encode("ascii")).hexdigest()
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO fund_brief_snapshots(
                request_run_id, decision_snapshot_id, fund_code, action_ids_json,
                primary_state, action_maturity, triggered_reviews_json,
                affected_action_abstentions_json, blocking_codes_json, evidence_state,
                missing_fields_json, conflicts_json, source_lineage_ids_json,
                evidence_fingerprint, canonical_snapshot_json, result_checksum,
                conclusion_changed, created_at
            ) VALUES (?, ?, '123456', '["fact_research","continue_holding"]',
                'watch', 'experimental_shadow', '[]', '[]', '[]', 'partial',
                '["owner_confirmed_thesis","industry_exposure"]', '[]',
                '["nav_lineage","manager_lineage"]', ?, ?, ?, 0, ?)
            """,
            (
                request_run_id,
                decision.id,
                CHECKSUM,
                tampered,
                checksum,
                (NOW + timedelta(seconds=1)).isoformat(),
            ),
        )


def test_direct_sql_rejects_nested_exact_amount_availability_key(tmp_path) -> None:
    repository, decision_store, _ = _stores(tmp_path)
    snapshot = _direct_snapshot_context(decision_store, "e" * 32)
    payload = json.loads(snapshot.canonical_json())
    payload["interpretations"][0]["state_inputs"]["nested"] = [
        {"exact_amount_available": False}
    ]
    tampered = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=tampered,
        )


@pytest.mark.parametrize("invalid_value", (True, "false", 0))
def test_direct_sql_requires_false_exact_amount_availability(
    tmp_path,
    invalid_value: object,
) -> None:
    repository, decision_store, _ = _stores(tmp_path)
    snapshot = _direct_snapshot_context(decision_store, "d" * 32)
    payload = json.loads(snapshot.canonical_json())
    payload["interpretations"][0]["exact_amount_available"] = invalid_value
    tampered = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=tampered,
        )


@pytest.mark.parametrize(
    "column,json_field",
    (
        ("triggered_reviews_json", "triggered_reviews"),
        ("affected_action_abstentions_json", "affected_action_abstentions"),
        ("blocking_codes_json", "blocking_codes"),
        ("missing_fields_json", "missing_fields"),
        ("conflicts_json", "conflicts"),
        ("source_lineage_ids_json", "source_lineage_ids"),
    ),
)
def test_direct_sql_rejects_divergent_summary_projections(
    tmp_path,
    column: str,
    json_field: str,
) -> None:
    repository, decision_store, _ = _stores(tmp_path)
    snapshot = _direct_snapshot_context(decision_store, "7" * 32)
    canonical = snapshot.canonical_json().decode("ascii")
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=canonical,
            overrides={column: '["divergent_value"]'},
        )


@pytest.mark.parametrize(
    "column,json_field",
    (
        ("triggered_reviews_json", "triggered_reviews"),
        ("affected_action_abstentions_json", "affected_action_abstentions"),
        ("blocking_codes_json", "blocking_codes"),
        ("missing_fields_json", "missing_fields"),
        ("conflicts_json", "conflicts"),
        ("source_lineage_ids_json", "source_lineage_ids"),
    ),
)
def test_direct_sql_summary_arrays_require_bounded_identifiers(
    tmp_path,
    column: str,
    json_field: str,
) -> None:
    repository, decision_store, _ = _stores(tmp_path)
    snapshot = _direct_snapshot_context(decision_store, "8" * 32)
    payload = json.loads(snapshot.canonical_json())
    payload[json_field] = [1]
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=canonical,
            overrides={column: "[1]"},
        )


def test_policy_and_snapshot_rows_are_immutable_and_nonreplaceable(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    request_run_id, _, stored = _publish(decision_store, brief_store)
    statements = (
        ("UPDATE brief_policy_versions SET created_at = created_at", ()),
        ("DELETE FROM brief_policy_versions", ()),
        (
            "INSERT INTO brief_policy_versions SELECT * FROM brief_policy_versions",
            (),
        ),
        (
            "UPDATE fund_brief_snapshots SET conclusion_changed = conclusion_changed",
            (),
        ),
        ("DELETE FROM fund_brief_snapshots WHERE id = ?", (stored.id,)),
        (
            "INSERT INTO fund_brief_snapshots SELECT * FROM fund_brief_snapshots "
            "WHERE request_run_id = ?",
            (request_run_id,),
        ),
    )
    for statement, parameters in statements:
        with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)


def test_publish_rejects_backdated_fund_history_and_preserves_prior_snapshot(
    tmp_path,
) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    first = _publish(
        decision_store,
        brief_store,
        request_id="a" * 32,
        created_at=NOW + timedelta(seconds=10),
    )[2]

    with pytest.raises(BriefStoreError) as raised:
        _publish(
            decision_store,
            brief_store,
            request_id="b" * 32,
            state=BriefState.ABSTAIN,
            created_at=NOW + timedelta(seconds=5),
        )
    assert "created" not in str(raised.value)
    with repository.connect() as connection:
        poisoned_run = connection.execute(
            "SELECT id, status FROM request_runs WHERE request_id = ?",
            ("b" * 32,),
        ).fetchone()
        assert poisoned_run["status"] == "running"
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
            (poisoned_run["id"],),
        ).fetchone()[0] == 0
    assert [item.id for item in brief_store.history("123456")] == [first.id]

    equal = _publish(
        decision_store,
        brief_store,
        request_id="c" * 32,
        created_at=NOW + timedelta(seconds=10),
    )[2]
    assert [item.id for item in brief_store.history("123456")][:2] == [
        equal.id,
        first.id,
    ]


@pytest.mark.parametrize("raised_type", (KeyboardInterrupt, SystemExit))
def test_factory_base_exceptions_propagate_after_atomic_rollback(
    tmp_path,
    raised_type,
) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    request_id = "d" * 32 if raised_type is KeyboardInterrupt else "e" * 32
    budget = _budget(request_id)
    request_run_id = decision_store.begin_request(budget)

    def interrupting_factory(_request_run_id: int, _decision_snapshot_id: int):
        raise raised_type("control-flow-sentinel")

    with pytest.raises(raised_type, match="control-flow-sentinel"):
        brief_store.publish(
            request_run_id=request_run_id,
            route=_route(request_id),
            evidence_policy=EvidencePolicyV1(),
            source_registry=SourceRegistryV1(),
            brief_policy=HeldFundBriefPolicyV1(),
            snapshot_factory=interrupting_factory,
            created_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=2),
            status=RequestTerminalStatus.PARTIAL,
            omitted_work=("industry_exposure",),
            budget=budget,
        )
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT status FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM fund_brief_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "status,omitted_work",
    (
        (RequestTerminalStatus.COMPLETE, ("industry_exposure",)),
        (RequestTerminalStatus.PARTIAL, ()),
    ),
)
def test_publish_requires_exact_terminal_omitted_work_semantics(
    tmp_path,
    status: RequestTerminalStatus,
    omitted_work: tuple[str, ...],
) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    request_id = "f" * 32
    budget = _budget(request_id)
    request_run_id = decision_store.begin_request(budget)
    with pytest.raises(ValueError, match="omitted"):
        brief_store.publish(
            request_run_id=request_run_id,
            route=_route(request_id),
            evidence_policy=EvidencePolicyV1(),
            source_registry=SourceRegistryV1(),
            brief_policy=HeldFundBriefPolicyV1(),
            snapshot_factory=lambda run_id, decision_id: _snapshot(run_id, decision_id),
            created_at=NOW + timedelta(seconds=1),
            finished_at=NOW + timedelta(seconds=2),
            status=status,
            omitted_work=omitted_work,
            budget=budget,
        )
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots WHERE request_run_id = ?",
            (request_run_id,),
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "status,omitted_json,finished_at",
    (
        ("running", "[]", None),
        ("complete", '["industry_exposure"]', NOW + timedelta(seconds=2)),
        ("partial", "[]", NOW + timedelta(seconds=2)),
        ("partial", '["industry_exposure"]', NOW + timedelta(seconds=91)),
    ),
)
def test_history_rejects_nonterminal_or_invalid_request_binding(
    tmp_path,
    status: str,
    omitted_json: str,
    finished_at: datetime | None,
) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    _publish(decision_store, brief_store, request_id="1" * 32)
    snapshot = _direct_snapshot_context(decision_store, "2" * 32)
    with repository.connect() as connection, connection:
        _direct_insert_snapshot(
            connection,
            snapshot,
            canonical_snapshot_json=snapshot.canonical_json().decode("ascii"),
        )
        if status != "running":
            connection.execute(
                "UPDATE request_runs SET status = ?, finished_at = ?, "
                "omitted_work_json = ? WHERE id = ?",
                (status, finished_at.isoformat(), omitted_json, snapshot.request_run_id),
            )
    with pytest.raises(BriefStoreError, match="history|request|authentication"):
        brief_store.history("123456")


def test_history_sanitizes_deep_direct_sql_public_tree(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    _insert_terminal_snapshot_with_state_inputs(
        repository,
        decision_store,
        request_id="c" * 32,
        state_inputs=_nested_public_mapping(700),
    )

    with pytest.raises(BriefStoreError) as raised:
        brief_store.history("123456")
    assert "recursion" not in str(raised.value).casefold()


def test_history_accepts_task1_maximum_public_tree_depth(tmp_path) -> None:
    repository, decision_store, brief_store = _stores(tmp_path)
    expected = _nested_public_mapping(12)
    _insert_terminal_snapshot_with_state_inputs(
        repository,
        decision_store,
        request_id="b" * 32,
        state_inputs=expected,
    )

    history = brief_store.history("123456")
    assert len(history) == 1
    assert (
        history[0].snapshot.interpretations[0].to_canonical_dict()["state_inputs"]
        == expected
    )


def test_store_bounds_are_finite_and_checked_before_json_decode(tmp_path) -> None:
    assert len(HeldFundBriefPolicyV1().canonical_json()) <= MAX_BRIEF_POLICY_JSON_BYTES
    snapshot = _snapshot(1, 2)
    assert len(snapshot.canonical_json()) <= MAX_BRIEF_SNAPSHOT_JSON_BYTES
    assert len(_array_json(tuple(f"field_{index}" for index in range(128)))) <= (
        MAX_BRIEF_SUMMARY_JSON_BYTES
    )
    assert MAX_BRIEF_SUMMARY_ITEMS == 128
    with pytest.raises(BriefStoreError, match="too large"):
        _array_json(tuple(f"field_{index}" for index in range(129)))
    with patch("kunjin.brief.store.json.loads", side_effect=AssertionError("decoded")):
        with pytest.raises(BriefStoreError, match="too large"):
            _strict_json(b" " * (MAX_BRIEF_SNAPSHOT_JSON_BYTES + 1))


def test_direct_sql_rejects_oversized_policy_snapshot_and_summary(tmp_path) -> None:
    repository, decision_store, _ = _stores(tmp_path)
    oversized_policy = json.dumps(
        {"padding": "x" * MAX_BRIEF_POLICY_JSON_BYTES},
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO brief_policy_versions VALUES ('1', ?, ?, ?)",
            (oversized_policy, "a" * 64, NOW.isoformat()),
        )

    oversized_snapshot = _direct_snapshot_context(decision_store, "3" * 32)
    payload = json.loads(oversized_snapshot.canonical_json())
    payload["padding"] = "x" * MAX_BRIEF_SNAPSHOT_JSON_BYTES
    oversized_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            oversized_snapshot,
            canonical_snapshot_json=oversized_json,
        )

    too_many = _direct_snapshot_context(decision_store, "4" * 32)
    payload = json.loads(too_many.canonical_json())
    identifiers = [f"review_{index}" for index in range(129)]
    payload["triggered_reviews"] = identifiers
    too_many_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        _direct_insert_snapshot(
            connection,
            too_many,
            canonical_snapshot_json=too_many_json,
            overrides={
                "triggered_reviews_json": json.dumps(identifiers, separators=(",", ":"))
            },
        )
