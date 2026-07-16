from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import Optional, Tuple

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
    StoredSourceAttempt,
    WorkflowLevel,
    canonical_json_bytes,
    validate_aware_datetime,
    validate_identifier,
    validate_identifier_tuple,
    validate_request_id,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.storage.repository import Repository

_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")


class DecisionAuditStoreError(RuntimeError):
    """A deterministic parent-side decision audit persistence failure."""


@dataclass(frozen=True)
class StoredDecisionSnapshot:
    id: int
    request_run_id: int
    route: DecisionRoute
    policy: EvidencePolicyV1
    registry: SourceRegistryV1
    created_at: datetime


class DecisionAuditStore:
    def __init__(self, repository: Repository) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        self.repository = repository

    def begin_request(self, budget: RequestBudget) -> int:
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        validate_request_id(budget.request_id)
        if type(budget.mode) is not RequestMode:
            raise ValueError("budget mode must be an exact RequestMode")
        started_at = _utc_text(budget.started_at, "request start")
        deadline_at = _utc_text(budget.deadline_at, "request deadline")
        try:
            with self.repository.connect() as connection, connection:
                cursor = connection.execute(
                    """
                    INSERT INTO request_runs(
                        request_id, mode, status, started_at, deadline_at,
                        finished_at, omitted_work_json
                    ) VALUES (?, ?, 'running', ?, ?, NULL, '[]')
                    """,
                    (budget.request_id, budget.mode.value, started_at, deadline_at),
                )
                request_run_id = int(cursor.lastrowid)
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("request begin failed") from None
        if request_run_id <= 0:
            raise DecisionAuditStoreError("request begin failed")
        return request_run_id

    def record_source_attempt(
        self,
        request_run_id: int,
        attempt: SourceAttempt,
    ) -> int:
        _positive_id(request_run_id, "request run id")
        if type(attempt) is not SourceAttempt:
            raise ValueError("attempt must be an exact SourceAttempt")
        attempt.validate()
        registry = SourceRegistryV1()
        if (
            attempt.registry_version != registry.version
            or attempt.registry_checksum != registry.checksum()
        ):
            raise DecisionAuditStoreError("source attempt registry binding failed")
        try:
            with self.repository.connect() as connection, connection:
                run = connection.execute(
                    "SELECT status, started_at, deadline_at FROM request_runs WHERE id = ?",
                    (request_run_id,),
                ).fetchone()
                if run is None:
                    raise DecisionAuditStoreError("request run does not exist")
                if run["status"] != "running":
                    raise DecisionAuditStoreError("request run is not running")
                request_start = _stored_datetime(run["started_at"], "request start")
                request_deadline = _stored_datetime(run["deadline_at"], "request deadline")
                if (
                    attempt.started_at < request_start
                    or attempt.finished_at > request_deadline
                ):
                    raise DecisionAuditStoreError("attempt is outside its request lifetime")

                rows = connection.execute(
                    """
                    SELECT attempt_number
                    FROM source_attempts
                    WHERE request_run_id = ? AND source_id = ? AND field_id = ?
                      AND subject_key = ?
                    ORDER BY attempt_number
                    """,
                    (
                        request_run_id,
                        attempt.source_id,
                        attempt.field_id,
                        attempt.subject_key,
                    ),
                ).fetchall()
                existing = tuple(int(row["attempt_number"]) for row in rows)
                expected = tuple(range(1, len(existing) + 1))
                if existing != expected or len(existing) >= 2:
                    raise DecisionAuditStoreError("source attempt sequence is invalid")
                if attempt.attempt_number != len(existing) + 1:
                    raise DecisionAuditStoreError("source attempt sequence is invalid")

                cursor = connection.execute(
                    """
                    INSERT INTO source_attempts(
                        request_run_id, source_id, field_id, subject_key,
                        attempt_number, outcome, started_at, finished_at,
                        data_as_of, error_code, cooldown_until, force_actor,
                        force_reason, registry_version, registry_checksum,
                        response_byte_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_run_id,
                        attempt.source_id,
                        attempt.field_id,
                        attempt.subject_key,
                        attempt.attempt_number,
                        attempt.outcome.value,
                        _utc_text(attempt.started_at, "attempt start"),
                        _utc_text(attempt.finished_at, "attempt finish"),
                        _optional_utc_text(attempt.data_as_of, "attempt data as of"),
                        None if attempt.error_code is None else attempt.error_code.value,
                        _optional_utc_text(attempt.cooldown_until, "attempt cooldown"),
                        attempt.force_actor,
                        None if attempt.force_reason is None else attempt.force_reason.value,
                        attempt.registry_version,
                        attempt.registry_checksum,
                        attempt.response_bytes,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
        except DecisionAuditStoreError:
            raise
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("source attempt insert failed") from None
        if attempt_id <= 0:
            raise DecisionAuditStoreError("source attempt insert failed")
        return attempt_id

    def finalize_request(
        self,
        request_run_id: int,
        status: RequestTerminalStatus,
        finished_at: datetime,
        omitted_work: Tuple[str, ...],
    ) -> None:
        _positive_id(request_run_id, "request run id")
        if type(status) is not RequestTerminalStatus:
            raise ValueError("status must be an exact RequestTerminalStatus")
        finished_text = _utc_text(finished_at, "request finish")
        validate_identifier_tuple(omitted_work, "omitted work")
        omitted_json = canonical_json_bytes(omitted_work).decode("ascii")
        try:
            with self.repository.connect() as connection, connection:
                cursor = connection.execute(
                    """
                    UPDATE request_runs
                    SET status = ?, finished_at = ?, omitted_work_json = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (status.value, finished_text, omitted_json, request_run_id),
                )
                if cursor.rowcount != 1:
                    raise DecisionAuditStoreError("request was not finalized exactly once")
        except DecisionAuditStoreError:
            raise
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("request finalization failed") from None

    def save_decision_snapshot(
        self,
        request_run_id: int,
        route: DecisionRoute,
        policy: EvidencePolicyV1,
        registry: SourceRegistryV1,
        created_at: datetime,
    ) -> StoredDecisionSnapshot:
        _positive_id(request_run_id, "request run id")
        if type(route) is not DecisionRoute:
            raise ValueError("route must be an exact DecisionRoute")
        if type(policy) is not EvidencePolicyV1:
            raise ValueError("policy must be an exact EvidencePolicyV1")
        if type(registry) is not SourceRegistryV1:
            raise ValueError("registry must be an exact SourceRegistryV1")
        policy.validate()
        registry.validate()
        policy_checksum = policy.checksum()
        registry_checksum = registry.checksum()
        if (
            route.policy_version != policy.version
            or route.policy_checksum != policy_checksum
        ):
            raise DecisionAuditStoreError("snapshot policy binding failed")
        if (
            route.registry_version != registry.version
            or route.registry_checksum != registry_checksum
        ):
            raise DecisionAuditStoreError("snapshot registry binding failed")
        route.validate()
        created_text = _utc_text(created_at, "snapshot creation")
        policy_json = policy.canonical_json().decode("ascii")
        registry_json = registry.canonical_json().decode("ascii")
        route_json = route.canonical_json().decode("ascii")
        try:
            with self.repository.connect() as connection, connection:
                run = connection.execute(
                    "SELECT request_id, mode, status FROM request_runs WHERE id = ?",
                    (request_run_id,),
                ).fetchone()
                if run is None:
                    raise DecisionAuditStoreError("request run does not exist")
                if run["status"] != "running":
                    raise DecisionAuditStoreError("request run is not running")
                if route.request_id != run["request_id"] or route.mode.value != run["mode"]:
                    raise DecisionAuditStoreError("snapshot request binding failed")
                connection.execute(
                    """
                    INSERT INTO decision_snapshots(
                        request_run_id, evidence_policy_version,
                        evidence_policy_json, evidence_policy_checksum,
                        source_registry_version, source_registry_json,
                        source_registry_checksum, canonical_route_json,
                        result_checksum, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_run_id,
                        policy.version,
                        policy_json,
                        policy_checksum,
                        registry.version,
                        registry_json,
                        registry_checksum,
                        route_json,
                        route.checksum(),
                        created_text,
                    ),
                )
                return self._load_decision_snapshot(
                    request_run_id,
                    connection=connection,
                )
        except DecisionAuditStoreError:
            raise
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("decision snapshot insert failed") from None

    def source_attempt_history(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
    ) -> Tuple[StoredSourceAttempt, ...]:
        validate_identifier(source_id, "source id")
        validate_identifier(field_id, "field id")
        if type(subject_key) is not str or _SUBJECT_KEY_PATTERN.fullmatch(subject_key) is None:
            raise ValueError("subject key must be fund: followed by exactly six digits")
        try:
            with self.repository.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT source_attempts.*
                    FROM source_attempts
                    JOIN request_runs ON request_runs.id = source_attempts.request_run_id
                    WHERE source_id = ? AND field_id = ? AND subject_key = ?
                    ORDER BY finished_at DESC, source_attempts.id DESC
                    """,
                    (source_id, field_id, subject_key),
                ).fetchall()
            return tuple(_stored_attempt(row) for row in rows)
        except DecisionAuditStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError):
            raise DecisionAuditStoreError("source attempt authentication failed") from None

    def _load_decision_snapshot(
        self,
        request_run_id: int,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> StoredDecisionSnapshot:
        _positive_id(request_run_id, "request run id")
        if connection is None:
            try:
                with self.repository.connect() as owned_connection:
                    return self._load_decision_snapshot(
                        request_run_id,
                        connection=owned_connection,
                    )
            except DecisionAuditStoreError:
                raise
            except sqlite3.DatabaseError:
                raise DecisionAuditStoreError("snapshot authentication failed") from None
        try:
            row = connection.execute(
                """
                SELECT decision_snapshots.*, request_runs.request_id,
                       request_runs.mode AS request_mode
                FROM decision_snapshots
                JOIN request_runs ON request_runs.id = decision_snapshots.request_run_id
                WHERE decision_snapshots.request_run_id = ?
                """,
                (request_run_id,),
            ).fetchone()
            if row is None:
                raise DecisionAuditStoreError("decision snapshot does not exist")
            return _stored_snapshot(row, request_run_id)
        except DecisionAuditStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError, UnicodeError):
            raise DecisionAuditStoreError("snapshot authentication failed") from None


def _stored_snapshot(row: sqlite3.Row, request_run_id: int) -> StoredDecisionSnapshot:
    snapshot_id = _positive_id(row["id"], "snapshot id")
    stored_request_run_id = _positive_id(row["request_run_id"], "request run id")
    if stored_request_run_id != request_run_id:
        raise DecisionAuditStoreError("snapshot request binding failed")

    policy_json = _canonical_object_bytes(row["evidence_policy_json"], "policy")
    policy_checksum = _required_text(row["evidence_policy_checksum"], "policy checksum")
    if hashlib.sha256(policy_json).hexdigest() != policy_checksum:
        raise DecisionAuditStoreError("snapshot authentication failed")
    policy = EvidencePolicyV1()
    if (
        row["evidence_policy_version"] != policy.version
        or policy_checksum != policy.checksum()
        or policy_json != policy.canonical_json()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    registry_json = _canonical_object_bytes(row["source_registry_json"], "registry")
    registry_checksum = _required_text(row["source_registry_checksum"], "registry checksum")
    if hashlib.sha256(registry_json).hexdigest() != registry_checksum:
        raise DecisionAuditStoreError("snapshot authentication failed")
    registry = SourceRegistryV1()
    if (
        row["source_registry_version"] != registry.version
        or registry_checksum != registry.checksum()
        or registry_json != registry.canonical_json()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    route_json = _canonical_object_bytes(row["canonical_route_json"], "route")
    result_checksum = _required_text(row["result_checksum"], "result checksum")
    if hashlib.sha256(route_json).hexdigest() != result_checksum:
        raise DecisionAuditStoreError("snapshot authentication failed")
    route = _decision_route_from_bytes(route_json)
    request_id = validate_request_id(row["request_id"])
    request_mode = RequestMode(row["request_mode"])
    if route.request_id != request_id or route.mode is not request_mode:
        raise DecisionAuditStoreError("snapshot request binding failed")
    if route.policy_version != policy.version or route.policy_checksum != policy_checksum:
        raise DecisionAuditStoreError("snapshot policy binding failed")
    if (
        route.registry_version != registry.version
        or route.registry_checksum != registry_checksum
    ):
        raise DecisionAuditStoreError("snapshot registry binding failed")
    return StoredDecisionSnapshot(
        id=snapshot_id,
        request_run_id=stored_request_run_id,
        route=route,
        policy=policy,
        registry=registry,
        created_at=_stored_datetime(row["created_at"], "snapshot creation"),
    )


def _stored_attempt(row: sqlite3.Row) -> StoredSourceAttempt:
    registry_version = _required_text(row["registry_version"], "registry version")
    registry_checksum = _required_text(row["registry_checksum"], "registry checksum")
    registry = SourceRegistryV1()
    if registry_version != registry.version or registry_checksum != registry.checksum():
        raise DecisionAuditStoreError("source attempt authentication failed")
    error_code = row["error_code"]
    force_reason = row["force_reason"]
    attempt = SourceAttempt(
        source_id=_required_text(row["source_id"], "source id"),
        field_id=_required_text(row["field_id"], "field id"),
        subject_key=_required_text(row["subject_key"], "subject key"),
        attempt_number=_required_int(row["attempt_number"], "attempt number"),
        outcome=SourceAttemptOutcome(row["outcome"]),
        started_at=_stored_datetime(row["started_at"], "attempt start"),
        finished_at=_stored_datetime(row["finished_at"], "attempt finish"),
        data_as_of=_optional_stored_datetime(row["data_as_of"], "attempt data as of"),
        error_code=None if error_code is None else SourceErrorCode(error_code),
        cooldown_until=_optional_stored_datetime(row["cooldown_until"], "attempt cooldown"),
        force_actor=None
        if row["force_actor"] is None
        else _required_text(row["force_actor"], "force actor"),
        force_reason=None if force_reason is None else ForceReasonCode(force_reason),
        registry_version=registry_version,
        registry_checksum=registry_checksum,
        response_bytes=_required_int(row["response_byte_count"], "response byte count"),
    )
    record = StoredSourceAttempt(
        id=_positive_id(row["id"], "attempt id"),
        request_run_id=_positive_id(row["request_run_id"], "request run id"),
        attempt=attempt,
    )
    record.validate()
    return record


def _decision_route_from_bytes(value: bytes) -> DecisionRoute:
    payload = json.loads(
        value.decode("ascii"),
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    _exact_keys(
        payload,
        {
            "actions",
            "conclusion_evidence",
            "missing_fields",
            "mode",
            "opposing_evidence",
            "policy_checksum",
            "policy_version",
            "registry_checksum",
            "registry_version",
            "request_id",
            "workflow_level",
        },
        "decision route",
    )
    actions = _required_list(payload["actions"], "actions")
    evidence = _required_list(payload["conclusion_evidence"], "conclusion evidence")
    route = DecisionRoute(
        request_id=payload["request_id"],
        mode=RequestMode(payload["mode"]),
        workflow_level=WorkflowLevel(payload["workflow_level"]),
        actions=tuple(_action_route(item) for item in actions),
        conclusion_evidence=tuple(_conclusion_evidence(item) for item in evidence),
        opposing_evidence=_string_tuple(payload["opposing_evidence"], "opposing evidence"),
        missing_fields=_string_tuple(payload["missing_fields"], "missing fields"),
        policy_version=payload["policy_version"],
        policy_checksum=payload["policy_checksum"],
        registry_version=payload["registry_version"],
        registry_checksum=payload["registry_checksum"],
    )
    route.validate()
    if route.canonical_json() != value:
        raise ValueError("decision route did not round trip canonically")
    return route


def _action_route(value: object) -> ActionRoute:
    _exact_keys(
        value,
        {
            "action",
            "action_id",
            "action_maturity",
            "blocking_codes",
            "exact_amount_available",
            "minimum_state",
            "required_gates",
            "research_available",
            "risk_effect",
        },
        "action route",
    )
    return ActionRoute(
        action_id=value["action_id"],
        action=ActionKind(value["action"]),
        risk_effect=RiskEffect(value["risk_effect"]),
        required_gates=_string_tuple(value["required_gates"], "required gates"),
        blocking_codes=_string_tuple(value["blocking_codes"], "blocking codes"),
        research_available=value["research_available"],
        exact_amount_available=value["exact_amount_available"],
        minimum_state=ActionState(value["minimum_state"]),
        action_maturity=ActionMaturity(value["action_maturity"]),
    )


def _conclusion_evidence(value: object) -> ConclusionEvidence:
    _exact_keys(
        value,
        {
            "completeness",
            "conflicts",
            "coverage_percent",
            "freshness",
            "independent_lineage_count",
            "inferred",
            "lineage_ids",
            "market_as_of",
            "missing_critical_fields",
            "publication_times",
            "publishers",
            "report_as_of",
            "retrieved_at",
            "source_ids",
            "source_tier",
        },
        "conclusion evidence",
    )
    coverage = value["coverage_percent"]
    if coverage is not None and type(coverage) is not str:
        raise ValueError("coverage percent is invalid")
    return ConclusionEvidence(
        source_tier=SourceTier(value["source_tier"]),
        publishers=_string_tuple(value["publishers"], "publishers"),
        source_ids=_string_tuple(value["source_ids"], "source ids"),
        publication_times=tuple(
            _stored_datetime(item, "publication time")
            for item in _required_list(value["publication_times"], "publication times")
        ),
        market_as_of=_optional_stored_datetime(value["market_as_of"], "market as of"),
        report_as_of=_optional_stored_datetime(value["report_as_of"], "report as of"),
        retrieved_at=_stored_datetime(value["retrieved_at"], "retrieved at"),
        independent_lineage_count=value["independent_lineage_count"],
        lineage_ids=_string_tuple(value["lineage_ids"], "lineage ids"),
        completeness=EvidenceCompleteness(value["completeness"]),
        coverage_percent=_optional_decimal(coverage),
        freshness=EvidenceFreshness(value["freshness"]),
        conflicts=_string_tuple(value["conflicts"], "conflicts"),
        inferred=value["inferred"],
        missing_critical_fields=_string_tuple(
            value["missing_critical_fields"], "missing critical fields"
        ),
    )


def _canonical_object_bytes(value: object, label: str) -> bytes:
    text = _required_text(value, f"{label} JSON")
    encoded = text.encode("ascii")
    parsed = json.loads(
        text,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
    )
    if type(parsed) is not dict or canonical_json_bytes(parsed) != encoded:
        raise ValueError(f"{label} JSON is not a canonical object")
    return encoded


def _optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(value)
    except DecimalException:
        raise ValueError("decimal value is invalid") from None


def _exact_keys(value: object, expected: set[str], label: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} keys are invalid")


def _required_list(value: object, label: str) -> list:
    if type(value) is not list:
        raise ValueError(f"{label} must be a list")
    return value


def _string_tuple(value: object, label: str) -> Tuple[str, ...]:
    items = _required_list(value, label)
    if any(type(item) is not str for item in items):
        raise ValueError(f"{label} must contain strings")
    return tuple(items)


def _positive_id(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive exact integer")
    return value


def _required_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an exact integer")
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty exact string")
    return value


def _utc_text(value: object, label: str) -> str:
    parsed = validate_aware_datetime(value, label)
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_utc_text(value: object, label: str) -> Optional[str]:
    return None if value is None else _utc_text(value, label)


def _stored_datetime(value: object, label: str) -> datetime:
    text = _required_text(value, label)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be stored as UTC")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.isoformat() != text:
        raise ValueError(f"{label} is not canonical")
    return parsed


def _optional_stored_datetime(value: object, label: str) -> Optional[datetime]:
    return None if value is None else _stored_datetime(value, label)
