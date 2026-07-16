from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import Mapping, Optional, Tuple, Union

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    V1_SOURCE_FIELD_IDENTITIES,
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ActionState,
    ConclusionEvidence,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    ForceAuthorization,
    ForceReasonCode,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldHistory,
    SourceFieldRef,
    SourceTier,
    SourceWorkAuthorization,
    SourceWorkKind,
    StoredSourceAttempt,
    WorkflowLevel,
    canonical_json_bytes,
    validate_aware_datetime,
    validate_checksum,
    validate_identifier,
    validate_identifier_tuple,
    validate_request_id,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.worker_protocol import MAX_RESPONSE_BYTES
from kunjin.storage.repository import Repository

_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")
MAX_CANONICAL_ROUTE_BYTES = MAX_RESPONSE_BYTES
MAX_CANONICAL_ROUTE_DEPTH = 64
SOURCE_HISTORY_LIMIT = 64


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
        authorization: Optional[Union[SourceWorkAuthorization, ForceAuthorization]] = None,
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> int:
        _positive_id(request_run_id, "request run id")
        if type(attempt) is not SourceAttempt:
            raise ValueError("attempt must be an exact SourceAttempt")
        attempt.validate()
        persisted_authorization = None
        if authorization is not None:
            if type(authorization) is ForceAuthorization:
                authorization.validate()
                persisted_authorization = authorization.reservation
            elif type(authorization) is SourceWorkAuthorization:
                authorization.validate()
                persisted_authorization = authorization
            else:
                raise ValueError("authorization must be an exact typed authorization")
        registry = SourceRegistryV1()
        if (
            attempt.registry_version != registry.version
            or attempt.registry_checksum != registry.checksum()
        ):
            raise DecisionAuditStoreError("source attempt registry binding failed")
        owns_connection = connection is None
        if not owns_connection and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        manager = self.repository.connect() if owns_connection else nullcontext(connection)
        try:
            with manager as active_connection:
                if owns_connection:
                    active_connection.execute("BEGIN IMMEDIATE")
                run = active_connection.execute(
                    "SELECT * FROM request_runs WHERE id = ?",
                    (request_run_id,),
                ).fetchone()
                if run is None or run["status"] != "running":
                    raise DecisionAuditStoreError("request run is not running")
                if attempt.force_actor is not None and run["mode"] != RequestMode.DEEP.value:
                    raise DecisionAuditStoreError("force attempt requires a deep request")
                request_start = _stored_datetime(run["started_at"], "request start")
                request_deadline = _stored_datetime(run["deadline_at"], "request deadline")
                if (
                    attempt.started_at < request_start
                    or attempt.finished_at > request_deadline
                ):
                    raise DecisionAuditStoreError("attempt is outside its request lifetime")

                rows = active_connection.execute(
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

                authenticated_authorization = None
                if persisted_authorization is not None:
                    authenticated_authorization = self._load_source_work_authorization(
                        persisted_authorization.id,
                        connection=active_connection,
                    )
                    if authenticated_authorization != persisted_authorization:
                        raise DecisionAuditStoreError("source work authorization binding failed")
                    if (
                        authenticated_authorization.kind is SourceWorkKind.FORCE
                        and type(authorization) is not ForceAuthorization
                    ):
                        raise DecisionAuditStoreError("force attempt requires ForceAuthorization")
                    if (
                        authenticated_authorization.kind is SourceWorkKind.RETRY
                        and type(authorization) is not SourceWorkAuthorization
                    ):
                        raise DecisionAuditStoreError("retry attempt authorization type is invalid")
                self._validate_attempt_authorization(
                    request_run_id,
                    attempt,
                    authenticated_authorization,
                )
                if authorization is None and attempt.attempt_number == 1:
                    pending_force = active_connection.execute(
                        """
                        SELECT 1
                        FROM source_work_authorizations
                        LEFT JOIN source_attempts consumed
                          ON consumed.authorization_id = source_work_authorizations.id
                        WHERE source_work_authorizations.request_run_id = ?
                          AND source_work_authorizations.kind = 'force'
                          AND source_work_authorizations.source_id = ?
                          AND source_work_authorizations.field_id = ?
                          AND source_work_authorizations.subject_key = ?
                          AND consumed.id IS NULL
                        """,
                        (
                            request_run_id,
                            attempt.source_id,
                            attempt.field_id,
                            attempt.subject_key,
                        ),
                    ).fetchone()
                    if pending_force is not None:
                        raise DecisionAuditStoreError(
                            "ordinary attempt is blocked by a force authorization"
                        )

                cursor = active_connection.execute(
                    """
                    INSERT INTO source_attempts(
                        request_run_id, source_id, field_id, subject_key,
                        attempt_number, outcome, started_at, finished_at,
                        data_as_of, error_code, cooldown_until, force_actor,
                        force_reason, registry_version, registry_checksum,
                        response_byte_count, authorization_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        None
                        if persisted_authorization is None
                        else persisted_authorization.id,
                    ),
                )
                attempt_id = int(cursor.lastrowid)
                if owns_connection:
                    active_connection.commit()
        except DecisionAuditStoreError:
            raise
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("source attempt insert failed") from None
        if attempt_id <= 0:
            raise DecisionAuditStoreError("source attempt insert failed")
        return attempt_id

    def reserve_force(
        self,
        request_run_id: int,
        budget: RequestBudget,
        source_id: str,
        field_id: str,
        subject_key: str,
        reserved_at: datetime,
        reason: ForceReasonCode,
    ) -> Optional[ForceAuthorization]:
        reservation = self._reserve_source_work(
            request_run_id,
            budget,
            source_id,
            field_id,
            subject_key,
            reserved_at,
            SourceWorkKind.FORCE,
            parent=None,
            actor="local_owner",
            reason=reason,
        )
        if reservation is None:
            return None
        authorization = ForceAuthorization(reservation)
        authorization.validate()
        return authorization

    def reserve_retry(
        self,
        request_run_id: int,
        budget: RequestBudget,
        parent: StoredSourceAttempt,
        reserved_at: datetime,
        *,
        minimum_worker_seconds: float,
    ) -> Optional[SourceWorkAuthorization]:
        if type(parent) is not StoredSourceAttempt:
            raise ValueError("retry parent must be an exact StoredSourceAttempt")
        parent.validate()
        if (
            type(minimum_worker_seconds) is not float
            or not math.isfinite(minimum_worker_seconds)
            or minimum_worker_seconds <= 0.0
        ):
            raise ValueError("minimum worker seconds must be a positive finite exact float")
        return self._reserve_source_work(
            request_run_id,
            budget,
            parent.attempt.source_id,
            parent.attempt.field_id,
            parent.attempt.subject_key,
            reserved_at,
            SourceWorkKind.RETRY,
            parent=parent,
            actor=None,
            reason=None,
            minimum_worker_seconds=minimum_worker_seconds,
        )

    def _reserve_source_work(
        self,
        request_run_id: int,
        budget: RequestBudget,
        source_id: str,
        field_id: str,
        subject_key: str,
        reserved_at: datetime,
        kind: SourceWorkKind,
        *,
        parent: Optional[StoredSourceAttempt],
        actor: Optional[str],
        reason: Optional[ForceReasonCode],
        minimum_worker_seconds: Optional[float] = None,
    ) -> Optional[SourceWorkAuthorization]:
        _positive_id(request_run_id, "request run id")
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        validate_identifier(source_id, "source id")
        validate_identifier(field_id, "field id")
        if type(subject_key) is not str or _SUBJECT_KEY_PATTERN.fullmatch(subject_key) is None:
            raise ValueError("subject key must be fund: followed by exactly six digits")
        reserved_at = validate_aware_datetime(
            reserved_at,
            "authorization reservation time",
        ).astimezone(timezone.utc)
        if type(kind) is not SourceWorkKind:
            raise ValueError("authorization kind must be an exact SourceWorkKind")
        registry = SourceRegistryV1()
        if (source_id, field_id) not in V1_SOURCE_FIELD_IDENTITIES:
            raise ValueError("authorization source field is not registered")
        if kind is SourceWorkKind.FORCE:
            if parent is not None or actor != "local_owner" or type(reason) is not ForceReasonCode:
                raise ValueError("force reservation requires owner reason and no parent")
        elif (
            type(parent) is not StoredSourceAttempt
            or actor is not None
            or reason is not None
        ):
            raise ValueError("retry reservation requires only an exact parent attempt")
        try:
            with self.repository.connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = self._authenticate_budget_request(connection, request_run_id, budget)
                budget.require_publishable()
                if (
                    minimum_worker_seconds is not None
                    and budget.worker_seconds() < minimum_worker_seconds
                ):
                    return None
                if not budget.started_at <= reserved_at <= budget.deadline_at:
                    raise DecisionAuditStoreError("reservation is outside the request lifetime")
                if kind is SourceWorkKind.FORCE and run["mode"] != RequestMode.DEEP.value:
                    raise DecisionAuditStoreError("force reservation requires a deep request")
                if kind is SourceWorkKind.FORCE and connection.execute(
                    """
                    SELECT 1 FROM source_attempts
                    WHERE request_run_id = ? AND source_id = ? AND field_id = ?
                      AND subject_key = ? AND attempt_number = 1
                    """,
                    (request_run_id, source_id, field_id, subject_key),
                ).fetchone() is not None:
                    return None
                parent_id = None
                if kind is SourceWorkKind.RETRY:
                    if parent is None:
                        raise DecisionAuditStoreError("retry parent is missing")
                    if (
                        parent.request_run_id != request_run_id
                        or parent.request_id != budget.request_id
                        or parent.attempt.source_id != source_id
                        or parent.attempt.field_id != field_id
                        or parent.attempt.subject_key != subject_key
                        or parent.attempt.attempt_number != 1
                        or parent.attempt.outcome is not SourceAttemptOutcome.TRANSIENT_FAILURE
                    ):
                        raise DecisionAuditStoreError("retry parent binding failed")
                    parent_row = connection.execute(
                        """
                        SELECT source_attempts.*, request_runs.request_id AS request_id
                        FROM source_attempts
                        JOIN request_runs ON request_runs.id = source_attempts.request_run_id
                        WHERE source_attempts.id = ?
                        """,
                        (parent.id,),
                    ).fetchone()
                    if parent_row is None or _stored_attempt(parent_row) != parent:
                        raise DecisionAuditStoreError("retry parent authentication failed")
                    if connection.execute(
                        """
                        SELECT 1 FROM source_attempts
                        WHERE request_run_id = ? AND source_id = ? AND field_id = ?
                          AND subject_key = ? AND attempt_number = 2
                        """,
                        (request_run_id, source_id, field_id, subject_key),
                    ).fetchone() is not None:
                        raise DecisionAuditStoreError("retry attempt already exists")
                    parent_id = parent.id
                existing = connection.execute(
                    """
                    SELECT id FROM source_work_authorizations
                    WHERE request_run_id = ? AND kind = ? AND source_id = ?
                      AND field_id = ? AND subject_key = ?
                    """,
                    (request_run_id, kind.value, source_id, field_id, subject_key),
                ).fetchone()
                if existing is not None:
                    return None
                cursor = connection.execute(
                    """
                    INSERT INTO source_work_authorizations(
                        request_run_id, kind, parent_attempt_id, source_id,
                        field_id, subject_key, actor, reason, reserved_at,
                        deadline_at, registry_version, registry_checksum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_run_id,
                        kind.value,
                        parent_id,
                        source_id,
                        field_id,
                        subject_key,
                        actor,
                        None if reason is None else reason.value,
                        _utc_text(reserved_at, "reservation time"),
                        _utc_text(budget.deadline_at, "authorization deadline"),
                        registry.version,
                        registry.checksum(),
                    ),
                )
                return self._load_source_work_authorization(
                    int(cursor.lastrowid),
                    connection=connection,
                )
        except (DecisionAuditStoreError, BudgetExpired):
            raise
        except sqlite3.IntegrityError:
            raise DecisionAuditStoreError("source work reservation rejected") from None
        except sqlite3.DatabaseError:
            raise DecisionAuditStoreError("source work reservation failed") from None

    @staticmethod
    def _authenticate_budget_request(
        connection: sqlite3.Connection,
        request_run_id: int,
        budget: RequestBudget,
    ) -> sqlite3.Row:
        run = connection.execute(
            "SELECT * FROM request_runs WHERE id = ?",
            (request_run_id,),
        ).fetchone()
        if run is None or run["status"] != "running":
            raise DecisionAuditStoreError("request run is not running")
        if (
            run["request_id"] != budget.request_id
            or run["mode"] != budget.mode.value
            or _stored_datetime(run["started_at"], "request start") != budget.started_at
            or _stored_datetime(run["deadline_at"], "request deadline") != budget.deadline_at
        ):
            raise DecisionAuditStoreError("request budget binding failed")
        return run

    def _load_source_work_authorization(
        self,
        authorization_id: int,
        *,
        connection: sqlite3.Connection,
    ) -> SourceWorkAuthorization:
        _positive_id(authorization_id, "authorization id")
        row = connection.execute(
            """
            SELECT source_work_authorizations.*, request_runs.request_id AS request_id
            FROM source_work_authorizations
            JOIN request_runs
              ON request_runs.id = source_work_authorizations.request_run_id
            WHERE source_work_authorizations.id = ?
            """,
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise DecisionAuditStoreError("source work authorization does not exist")
        return _stored_source_work_authorization(row)

    @staticmethod
    def _validate_attempt_authorization(
        request_run_id: int,
        attempt: SourceAttempt,
        authorization: Optional[SourceWorkAuthorization],
    ) -> None:
        if authorization is None:
            if attempt.force_actor is not None or attempt.attempt_number != 1:
                raise DecisionAuditStoreError("source attempt requires an authorization")
            return
        if (
            authorization.request_run_id != request_run_id
            or authorization.source_id != attempt.source_id
            or authorization.field_id != attempt.field_id
            or authorization.subject_key != attempt.subject_key
            or attempt.started_at < authorization.reserved_at
            or attempt.finished_at > authorization.deadline_at
        ):
            raise DecisionAuditStoreError("source work authorization binding failed")
        if authorization.kind is SourceWorkKind.FORCE:
            if (
                attempt.attempt_number != 1
                or attempt.force_actor != authorization.actor
                or attempt.force_reason is not authorization.reason
            ):
                raise DecisionAuditStoreError("force authorization binding failed")
        elif (
            attempt.attempt_number != 2
            or attempt.force_actor is not None
            or attempt.force_reason is not None
        ):
            raise DecisionAuditStoreError("retry authorization binding failed")

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
                connection.execute("BEGIN IMMEDIATE")
                pending_fields = {
                    str(row["field_id"])
                    for row in connection.execute(
                        """
                        SELECT source_work_authorizations.field_id
                        FROM source_work_authorizations
                        LEFT JOIN source_attempts
                          ON source_attempts.authorization_id = source_work_authorizations.id
                        WHERE source_work_authorizations.request_run_id = ?
                          AND source_attempts.id IS NULL
                        """,
                        (request_run_id,),
                    ).fetchall()
                }
                if pending_fields and (
                    status is RequestTerminalStatus.COMPLETE
                    or not pending_fields.issubset(set(omitted_work))
                ):
                    raise DecisionAuditStoreError("request was not finalized exactly once")
                cursor = connection.execute(
                    """
                    UPDATE request_runs
                    SET status = ?, finished_at = ?, omitted_work_json = ?
                    WHERE id = ? AND status = 'running'
                      AND NOT EXISTS (
                          SELECT 1 FROM source_attempts
                          WHERE request_run_id = ?
                            AND finished_at COLLATE BINARY > ? COLLATE BINARY
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM decision_snapshots
                          WHERE request_run_id = ?
                            AND created_at COLLATE BINARY > ? COLLATE BINARY
                      )
                    """,
                    (
                        status.value,
                        finished_text,
                        omitted_json,
                        request_run_id,
                        request_run_id,
                        finished_text,
                        request_run_id,
                        finished_text,
                    ),
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
        policy_json_bytes = policy.canonical_json()
        registry_json_bytes = registry.canonical_json()
        route_json_bytes = route.canonical_json()
        try:
            _validate_route_frame_bounds(route_json_bytes)
        except ValueError as exc:
            raise DecisionAuditStoreError(str(exc)) from None
        policy_json = policy_json_bytes.decode("ascii")
        registry_json = registry_json_bytes.decode("ascii")
        route_json = route_json_bytes.decode("ascii")
        try:
            with self.repository.connect() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    """
                    SELECT request_id, mode, status, started_at, deadline_at
                    FROM request_runs WHERE id = ?
                    """,
                    (request_run_id,),
                ).fetchone()
                if run is None:
                    raise DecisionAuditStoreError("request run does not exist")
                if run["status"] != "running":
                    raise DecisionAuditStoreError("request run is not running")
                request_start = _stored_datetime(run["started_at"], "request start")
                request_deadline = _stored_datetime(run["deadline_at"], "request deadline")
                snapshot_created = _stored_datetime(created_text, "snapshot creation")
                if not request_start <= snapshot_created <= request_deadline:
                    raise DecisionAuditStoreError(
                        "snapshot creation is outside its request lifetime"
                    )
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
                        hashlib.sha256(route_json_bytes).hexdigest(),
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
                    SELECT source_attempts.*, request_runs.request_id AS request_id
                    FROM source_attempts
                    JOIN request_runs ON request_runs.id = source_attempts.request_run_id
                    WHERE source_id = ? AND field_id = ? AND subject_key = ?
                    ORDER BY finished_at DESC, source_attempts.id DESC
                    LIMIT ?
                    """,
                    (source_id, field_id, subject_key, SOURCE_HISTORY_LIMIT),
                ).fetchall()
            return tuple(_stored_attempt(row) for row in rows)
        except DecisionAuditStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError):
            raise DecisionAuditStoreError("source attempt authentication failed") from None

    def authenticated_source_attempt_histories(
        self,
        request_run_id: int,
        budget: RequestBudget,
        references: Tuple[SourceFieldRef, ...],
        subject_key: str,
    ) -> Tuple[SourceFieldHistory, ...]:
        _positive_id(request_run_id, "request run id")
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        if type(references) is not tuple or not references or len(references) > 128:
            raise ValueError("references must be a non-empty bounded exact tuple")
        if len(references) != len(set(references)):
            raise ValueError("references must not contain duplicates")
        for reference in references:
            if type(reference) is not SourceFieldRef:
                raise ValueError("references must contain exact SourceFieldRef records")
            reference.validate()
        if type(subject_key) is not str or _SUBJECT_KEY_PATTERN.fullmatch(subject_key) is None:
            raise ValueError("subject key must be fund: followed by exactly six digits")
        budget.require_publishable()
        predicates = " OR ".join(
            "(source_attempts.source_id = ? AND source_attempts.field_id = ?)"
            for _ in references
        )
        parameters = tuple(
            item
            for reference in references
            for item in (reference.source_id, reference.field_id)
        )
        try:
            with self.repository.connect() as connection, connection:
                connection.execute("BEGIN")
                self._authenticate_budget_request(connection, request_run_id, budget)
                rows = connection.execute(
                    f"""
                    WITH ranked AS (
                        SELECT source_attempts.*,
                               request_runs.request_id AS request_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY source_attempts.source_id,
                                                source_attempts.field_id
                                   ORDER BY source_attempts.finished_at DESC,
                                            source_attempts.id DESC
                               ) AS history_rank
                        FROM source_attempts
                        JOIN request_runs
                          ON request_runs.id = source_attempts.request_run_id
                        WHERE source_attempts.subject_key = ? AND ({predicates})
                    )
                    SELECT * FROM ranked
                    WHERE history_rank <= ?
                    ORDER BY source_id, field_id, finished_at DESC, id DESC
                    """,  # noqa: S608 - placeholders bind every dynamic value
                    (subject_key, *parameters, SOURCE_HISTORY_LIMIT),
                ).fetchall()
                budget.require_publishable()
                grouped = {reference: [] for reference in references}
                for row in rows:
                    record = _stored_attempt(row)
                    grouped[SourceFieldRef(
                        record.attempt.source_id,
                        record.attempt.field_id,
                    )].append(record)
                histories = tuple(
                    SourceFieldHistory(reference, tuple(grouped[reference]))
                    for reference in references
                )
                for history in histories:
                    history.validate()
                return histories
        except (DecisionAuditStoreError, BudgetExpired):
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError):
            raise DecisionAuditStoreError("source history authentication failed") from None

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
            metadata = connection.execute(
                """
                SELECT decision_snapshots.id,
                       decision_snapshots.request_run_id,
                       decision_snapshots.evidence_policy_version,
                       decision_snapshots.evidence_policy_checksum,
                       decision_snapshots.source_registry_version,
                       decision_snapshots.source_registry_checksum,
                       decision_snapshots.result_checksum,
                       decision_snapshots.created_at,
                       request_runs.request_id,
                       request_runs.mode AS request_mode,
                       length(CAST(evidence_policy_json AS BLOB))
                           AS evidence_policy_byte_count,
                       length(CAST(source_registry_json AS BLOB))
                           AS source_registry_byte_count,
                       length(CAST(canonical_route_json AS BLOB))
                           AS route_byte_count
                FROM decision_snapshots
                JOIN request_runs ON request_runs.id = decision_snapshots.request_run_id
                WHERE decision_snapshots.request_run_id = ?
                """,
                (request_run_id,),
            ).fetchone()
            if metadata is None:
                raise DecisionAuditStoreError("decision snapshot does not exist")
            snapshot_id = _validate_snapshot_preflight(metadata, request_run_id)
            body = connection.execute(
                """
                SELECT evidence_policy_json, source_registry_json,
                       canonical_route_json
                FROM decision_snapshots
                WHERE request_run_id = ? AND id = ?
                """,
                (request_run_id, snapshot_id),
            ).fetchone()
            if body is None:
                raise DecisionAuditStoreError("decision snapshot does not exist")
            row = dict(metadata)
            row.update(dict(body))
            return _stored_snapshot(row, request_run_id)
        except DecisionAuditStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, OverflowError, UnicodeError):
            raise DecisionAuditStoreError("snapshot authentication failed") from None


def _validate_snapshot_preflight(row: sqlite3.Row, request_run_id: int) -> int:
    snapshot_id = _positive_id(row["id"], "snapshot id")
    stored_request_run_id = _positive_id(row["request_run_id"], "request run id")
    if stored_request_run_id != request_run_id:
        raise DecisionAuditStoreError("snapshot request binding failed")

    policy = EvidencePolicyV1()
    policy_bytes = policy.canonical_json()
    policy_checksum = _required_checksum(
        row["evidence_policy_checksum"], "policy checksum"
    )
    policy_byte_count = _required_int(
        row["evidence_policy_byte_count"], "policy JSON byte count"
    )
    if (
        policy_byte_count != len(policy_bytes)
        or row["evidence_policy_version"] != policy.version
        or policy_checksum != policy.checksum()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    registry = SourceRegistryV1()
    registry_bytes = registry.canonical_json()
    registry_checksum = _required_checksum(
        row["source_registry_checksum"], "registry checksum"
    )
    registry_byte_count = _required_int(
        row["source_registry_byte_count"], "registry JSON byte count"
    )
    if (
        registry_byte_count != len(registry_bytes)
        or row["source_registry_version"] != registry.version
        or registry_checksum != registry.checksum()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    _required_checksum(row["result_checksum"], "result checksum")
    route_byte_count = _required_int(row["route_byte_count"], "route JSON byte count")
    if not 0 < route_byte_count <= MAX_CANONICAL_ROUTE_BYTES:
        raise DecisionAuditStoreError("snapshot authentication failed")
    validate_request_id(row["request_id"])
    RequestMode(row["request_mode"])
    _stored_datetime(row["created_at"], "snapshot creation")
    return snapshot_id


def _stored_snapshot(
    row: Mapping[str, object], request_run_id: int
) -> StoredDecisionSnapshot:
    snapshot_id = _positive_id(row["id"], "snapshot id")
    stored_request_run_id = _positive_id(row["request_run_id"], "request run id")
    if stored_request_run_id != request_run_id:
        raise DecisionAuditStoreError("snapshot request binding failed")

    policy = EvidencePolicyV1()
    policy_checksum = _required_checksum(
        row["evidence_policy_checksum"], "policy checksum"
    )
    _authenticated_static_json_bytes(
        row["evidence_policy_json"],
        policy_checksum,
        expected=policy.canonical_json(),
        expected_checksum=policy.checksum(),
        label="policy",
    )
    if (
        row["evidence_policy_version"] != policy.version
        or policy_checksum != policy.checksum()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    registry = SourceRegistryV1()
    registry_checksum = _required_checksum(
        row["source_registry_checksum"], "registry checksum"
    )
    _authenticated_static_json_bytes(
        row["source_registry_json"],
        registry_checksum,
        expected=registry.canonical_json(),
        expected_checksum=registry.checksum(),
        label="registry",
    )
    if (
        row["source_registry_version"] != registry.version
        or registry_checksum != registry.checksum()
    ):
        raise DecisionAuditStoreError("snapshot authentication failed")

    result_checksum = _required_checksum(row["result_checksum"], "result checksum")
    route_json = _authenticated_route_json_bytes(
        row["canonical_route_json"], result_checksum
    )
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
        request_id=validate_request_id(row["request_id"]),
        authorization_id=None
        if row["authorization_id"] is None
        else _positive_id(row["authorization_id"], "authorization id"),
        attempt=attempt,
    )
    record.validate()
    return record


def _stored_source_work_authorization(row: sqlite3.Row) -> SourceWorkAuthorization:
    reason = row["reason"]
    authorization = SourceWorkAuthorization(
        id=_positive_id(row["id"], "authorization id"),
        request_run_id=_positive_id(row["request_run_id"], "request run id"),
        request_id=validate_request_id(row["request_id"]),
        source_id=_required_text(row["source_id"], "source id"),
        field_id=_required_text(row["field_id"], "field id"),
        subject_key=_required_text(row["subject_key"], "subject key"),
        kind=SourceWorkKind(row["kind"]),
        parent_attempt_id=None
        if row["parent_attempt_id"] is None
        else _positive_id(row["parent_attempt_id"], "parent attempt id"),
        actor=None if row["actor"] is None else _required_text(row["actor"], "actor"),
        reason=None if reason is None else ForceReasonCode(reason),
        reserved_at=_stored_datetime(row["reserved_at"], "reservation time"),
        deadline_at=_stored_datetime(row["deadline_at"], "authorization deadline"),
        registry_version=_required_text(row["registry_version"], "registry version"),
        registry_checksum=_required_checksum(row["registry_checksum"], "registry checksum"),
    )
    authorization.validate()
    return authorization


def _decision_route_from_bytes(value: bytes) -> DecisionRoute:
    try:
        payload = json.loads(
            value.decode("ascii"),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise ValueError("decision route JSON is invalid") from None
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


def _authenticated_static_json_bytes(
    value: object,
    checksum: str,
    *,
    expected: bytes,
    expected_checksum: str,
    label: str,
) -> bytes:
    text = _required_text(value, f"{label} JSON")
    if len(text) != len(expected):
        raise ValueError(f"{label} JSON length is invalid")
    encoded = text.encode("ascii")
    if (
        len(encoded) != len(expected)
        or not hmac.compare_digest(encoded, expected)
        or not hmac.compare_digest(checksum, expected_checksum)
        or not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), checksum)
    ):
        raise ValueError(f"{label} JSON authentication failed")
    return encoded


def _authenticated_route_json_bytes(value: object, checksum: str) -> bytes:
    text = _required_text(value, "route JSON")
    if len(text) > MAX_CANONICAL_ROUTE_BYTES:
        raise ValueError("decision route is too large")
    encoded = text.encode("ascii")
    _validate_route_frame_bounds(encoded)
    if not hmac.compare_digest(hashlib.sha256(encoded).hexdigest(), checksum):
        raise ValueError("decision route checksum is invalid")
    return encoded


def _validate_route_frame_bounds(value: object) -> bytes:
    if type(value) is not bytes or not value:
        raise ValueError("decision route must be non-empty bytes")
    if len(value) > MAX_CANONICAL_ROUTE_BYTES:
        raise ValueError("decision route is too large")
    depth = 0
    in_string = False
    escaped = False
    for byte in value:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_CANONICAL_ROUTE_DEPTH:
                raise ValueError("decision route nesting is too deep")
        elif byte in (0x5D, 0x7D):
            depth -= 1
            if depth < 0:
                raise ValueError("decision route nesting is invalid")
    if depth != 0 or in_string or escaped:
        raise ValueError("decision route nesting is invalid")
    return value


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


def _required_checksum(value: object, label: str) -> str:
    return validate_checksum(value, label)


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
