from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Mapping, Optional, Tuple

from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefCoverage,
    BriefEvidenceState,
    BriefFact,
    BriefSnapshot,
    BriefState,
    OfficialEvent,
    OfficialEventCode,
    RelationshipEvidence,
)
from kunjin.brief.policy import HeldFundBriefPolicyV1
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    ActionMaturity,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    RequestTerminalStatus,
    SourceTier,
    canonical_json_bytes,
    validate_identifier_tuple,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.storage.repository import Repository

BRIEF_HISTORY_LIMIT = 64
MAX_BRIEF_POLICY_JSON_BYTES = 64 * 1024
MAX_BRIEF_SNAPSHOT_JSON_BYTES = 4 * 1024 * 1024
MAX_BRIEF_SUMMARY_ITEMS = 128
MAX_BRIEF_SUMMARY_JSON_BYTES = 16 * 1024
MAX_BRIEF_PUBLIC_TREE_DEPTH = 12
_FUND_CODE_PATTERN = re.compile(r"^[0-9]{6}$")


class BriefStoreError(RuntimeError):
    """A sanitized held-fund brief persistence failure."""


@dataclass(frozen=True)
class StoredBriefSnapshot:
    id: int
    snapshot: BriefSnapshot
    policy: HeldFundBriefPolicyV1
    result_checksum: str
    conclusion_changed: bool


class BriefStore:
    def __init__(
        self,
        repository: Repository,
        decision_store: Optional[DecisionAuditStore] = None,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if decision_store is None:
            decision_store = DecisionAuditStore(repository)
        if type(decision_store) is not DecisionAuditStore:
            raise ValueError("decision store must be an exact DecisionAuditStore")
        if decision_store.repository is not repository:
            raise ValueError("decision store must own the same Repository")
        self.repository = repository
        self.decision_store = decision_store

    def publish(
        self,
        *,
        request_run_id: int,
        route: DecisionRoute,
        evidence_policy: EvidencePolicyV1,
        source_registry: SourceRegistryV1,
        brief_policy: HeldFundBriefPolicyV1,
        snapshot_factory: Callable[[int, int], BriefSnapshot],
        created_at: datetime,
        finished_at: datetime,
        status: RequestTerminalStatus,
        omitted_work: Tuple[str, ...],
        budget: RequestBudget,
    ) -> StoredBriefSnapshot:
        _positive_id(request_run_id, "request run id")
        if type(route) is not DecisionRoute:
            raise ValueError("route must be an exact DecisionRoute")
        if type(evidence_policy) is not EvidencePolicyV1:
            raise ValueError("evidence policy must be an exact EvidencePolicyV1")
        if type(source_registry) is not SourceRegistryV1:
            raise ValueError("source registry must be an exact SourceRegistryV1")
        if type(brief_policy) is not HeldFundBriefPolicyV1:
            raise ValueError("brief policy must be an exact HeldFundBriefPolicyV1")
        if not callable(snapshot_factory):
            raise ValueError("snapshot factory must be callable")
        if type(status) is not RequestTerminalStatus or status not in {
            RequestTerminalStatus.COMPLETE,
            RequestTerminalStatus.PARTIAL,
        }:
            raise ValueError("brief publication status must be complete or partial")
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        validate_identifier_tuple(omitted_work, "omitted work")
        if (
            status is RequestTerminalStatus.COMPLETE
            and omitted_work
        ) or (
            status is RequestTerminalStatus.PARTIAL
            and not omitted_work
        ):
            raise ValueError("terminal status and omitted work are inconsistent")
        created_text = _utc_text(created_at, "brief creation time")
        _utc_text(finished_at, "brief finish time")
        if finished_at < created_at:
            raise ValueError("brief finish time cannot precede creation time")
        brief_policy.validate()
        budget.require_publishable()

        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_or_insert_policy(
                        connection,
                        brief_policy,
                        created_text,
                    )
                    decision = self.decision_store.save_decision_snapshot(
                        request_run_id,
                        route,
                        evidence_policy,
                        source_registry,
                        created_at,
                        budget=budget,
                        connection=connection,
                    )
                    try:
                        snapshot = snapshot_factory(request_run_id, decision.id)
                    except Exception:
                        raise BriefStoreError("brief snapshot factory failed") from None
                    if type(snapshot) is not BriefSnapshot:
                        raise BriefStoreError(
                            "brief snapshot factory returned an invalid record"
                        )
                    snapshot.validate()
                    self._validate_snapshot_bindings(
                        snapshot,
                        request_run_id,
                        decision.id,
                        route,
                        created_at,
                    )
                    conclusion_changed = self._conclusion_changed(
                        connection,
                        snapshot,
                        brief_policy,
                    )
                    stored = self._insert_snapshot(
                        connection,
                        snapshot,
                        brief_policy,
                        conclusion_changed,
                    )
                    self.decision_store.finalize_request(
                        request_run_id,
                        status,
                        finished_at,
                        omitted_work,
                        budget=budget,
                        connection=connection,
                    )
                    budget.require_publishable()
                    connection.commit()
                    return stored
                except BaseException:
                    connection.rollback()
                    raise
        except BudgetExpired:
            raise
        except BriefStoreError:
            raise
        except (DecisionAuditStoreError, sqlite3.DatabaseError):
            raise BriefStoreError("brief publication failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise BriefStoreError("brief publication validation failed") from None

    def history(self, fund_code: str) -> Tuple[StoredBriefSnapshot, ...]:
        _fund_code(fund_code)
        try:
            with self.repository.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM fund_brief_snapshots
                    WHERE fund_code = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (fund_code, BRIEF_HISTORY_LIMIT + 1),
                ).fetchall()
                if not rows:
                    return ()
                policy = self._load_policy(connection)
                authenticated = tuple(
                    self._stored_snapshot(row, policy, connection) for row in rows
                )
                result = []
                for index, item in enumerate(authenticated[:BRIEF_HISTORY_LIMIT]):
                    previous = (
                        None
                        if index + 1 >= len(authenticated)
                        else authenticated[index + 1]
                    )
                    derived_changed = previous is not None and (
                        _conclusion_bytes(previous.snapshot)
                        != _conclusion_bytes(item.snapshot)
                    )
                    if item.conclusion_changed is not derived_changed:
                        raise BriefStoreError(
                            "brief conclusion history authentication failed"
                        )
                    result.append(item)
                return tuple(result)
        except BriefStoreError:
            raise
        except (
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
            OverflowError,
            UnicodeError,
            RecursionError,
        ):
            raise BriefStoreError("brief history authentication failed") from None

    @staticmethod
    def _authenticate_or_insert_policy(
        connection: sqlite3.Connection,
        policy: HeldFundBriefPolicyV1,
        created_text: str,
    ) -> None:
        policy_bytes = policy.canonical_json()
        _require_maximum_bytes(
            policy_bytes,
            MAX_BRIEF_POLICY_JSON_BYTES,
            "brief policy",
        )
        checksum = policy.checksum()
        row = connection.execute(
            "SELECT * FROM brief_policy_versions WHERE version = ?",
            (policy.version,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO brief_policy_versions(
                    version, canonical_policy_json, policy_checksum, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (policy.version, policy_bytes.decode("ascii"), checksum, created_text),
            )
            row = connection.execute(
                "SELECT * FROM brief_policy_versions WHERE version = ?",
                (policy.version,),
            ).fetchone()
        if row is None:
            raise BriefStoreError("brief policy authentication failed")
        stored_bytes = _ascii_bytes(
            row["canonical_policy_json"],
            "brief policy",
            maximum=MAX_BRIEF_POLICY_JSON_BYTES,
        )
        if (
            row["version"] != policy.version
            or row["policy_checksum"] != checksum
            or not hmac.compare_digest(stored_bytes, policy_bytes)
            or hashlib.sha256(stored_bytes).hexdigest() != checksum
        ):
            raise BriefStoreError("brief policy authentication failed")
        _stored_utc(row["created_at"], "brief policy creation time")

    @staticmethod
    def _load_policy(connection: sqlite3.Connection) -> HeldFundBriefPolicyV1:
        policy = HeldFundBriefPolicyV1()
        policy.validate()
        row = connection.execute(
            "SELECT * FROM brief_policy_versions WHERE version = ?",
            (policy.version,),
        ).fetchone()
        if row is None:
            raise BriefStoreError("brief policy authentication failed")
        BriefStore._authenticate_or_insert_policy(
            connection,
            policy,
            _utc_text(_stored_utc(row["created_at"], "brief policy creation time"), "time"),
        )
        return policy

    @staticmethod
    def _validate_snapshot_bindings(
        snapshot: BriefSnapshot,
        request_run_id: int,
        decision_snapshot_id: int,
        route: DecisionRoute,
        created_at: datetime,
    ) -> None:
        route_action_ids = tuple(item.action_id for item in route.actions)
        if (
            snapshot.request_run_id != request_run_id
            or snapshot.decision_snapshot_id != decision_snapshot_id
            or snapshot.action_ids != route_action_ids
            or snapshot.mode is not route.mode
            or snapshot.created_at != created_at
        ):
            raise BriefStoreError("brief snapshot binding failed")

    def _conclusion_changed(
        self,
        connection: sqlite3.Connection,
        snapshot: BriefSnapshot,
        policy: HeldFundBriefPolicyV1,
    ) -> bool:
        row = connection.execute(
            """
            SELECT * FROM fund_brief_snapshots
            WHERE fund_code = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (snapshot.fund_code,),
        ).fetchone()
        if row is None:
            return False
        previous = self._stored_snapshot(row, policy, connection).snapshot
        if snapshot.created_at < previous.created_at:
            raise BriefStoreError("brief publication order rejected")
        return _conclusion_bytes(previous) != _conclusion_bytes(snapshot)

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot: BriefSnapshot,
        policy: HeldFundBriefPolicyV1,
        conclusion_changed: bool,
    ) -> StoredBriefSnapshot:
        snapshot_bytes = snapshot.canonical_json()
        _require_maximum_bytes(
            snapshot_bytes,
            MAX_BRIEF_SNAPSHOT_JSON_BYTES,
            "brief snapshot",
        )
        snapshot_json = snapshot_bytes.decode("ascii")
        checksum = hashlib.sha256(snapshot_bytes).hexdigest()
        created_text = _utc_text(snapshot.created_at, "brief creation time")
        values = (
            snapshot.request_run_id,
            snapshot.decision_snapshot_id,
            snapshot.fund_code,
            _array_json(snapshot.action_ids),
            snapshot.primary_state.value,
            snapshot.action_maturity.value,
            _array_json(snapshot.triggered_reviews),
            _array_json(snapshot.affected_action_abstentions),
            _array_json(snapshot.blocking_codes),
            snapshot.evidence_state.value,
            _array_json(snapshot.missing_fields),
            _array_json(snapshot.conflicts),
            _array_json(snapshot.source_lineage_ids),
            snapshot.evidence_fingerprint,
            snapshot_json,
            checksum,
            int(conclusion_changed),
            created_text,
        )
        cursor = connection.execute(
            """
            INSERT INTO fund_brief_snapshots(
                request_run_id, decision_snapshot_id, fund_code, action_ids_json,
                primary_state, action_maturity, triggered_reviews_json,
                affected_action_abstentions_json, blocking_codes_json, evidence_state,
                missing_fields_json, conflicts_json, source_lineage_ids_json,
                evidence_fingerprint, canonical_snapshot_json, result_checksum,
                conclusion_changed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = connection.execute(
            "SELECT * FROM fund_brief_snapshots WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        if row is None:
            raise BriefStoreError("brief snapshot reload failed")
        stored = self._stored_snapshot(
            row,
            policy,
            connection,
            require_terminal=False,
        )
        if (
            stored.snapshot.canonical_json() != snapshot_bytes
            or stored.result_checksum != checksum
            or stored.conclusion_changed is not conclusion_changed
        ):
            raise BriefStoreError("brief snapshot byte comparison failed")
        return stored

    def _stored_snapshot(
        self,
        row: Mapping[str, object],
        policy: HeldFundBriefPolicyV1,
        connection: sqlite3.Connection,
        *,
        require_terminal: bool = True,
    ) -> StoredBriefSnapshot:
        snapshot_id = _positive_id(row["id"], "brief snapshot id")
        if type(require_terminal) is not bool:
            raise ValueError("terminal requirement must be an exact boolean")
        snapshot_bytes = _ascii_bytes(
            row["canonical_snapshot_json"],
            "brief snapshot",
            maximum=MAX_BRIEF_SNAPSHOT_JSON_BYTES,
        )
        checksum = _checksum(row["result_checksum"], "brief result checksum")
        if hashlib.sha256(snapshot_bytes).hexdigest() != checksum:
            raise BriefStoreError("brief snapshot authentication failed")
        snapshot = _decode_snapshot(snapshot_bytes)
        if snapshot.canonical_json() != snapshot_bytes:
            raise BriefStoreError("brief snapshot authentication failed")
        projections = {
            "request_run_id": snapshot.request_run_id,
            "decision_snapshot_id": snapshot.decision_snapshot_id,
            "fund_code": snapshot.fund_code,
            "action_ids_json": _array_json(snapshot.action_ids),
            "primary_state": snapshot.primary_state.value,
            "action_maturity": snapshot.action_maturity.value,
            "triggered_reviews_json": _array_json(snapshot.triggered_reviews),
            "affected_action_abstentions_json": _array_json(
                snapshot.affected_action_abstentions
            ),
            "blocking_codes_json": _array_json(snapshot.blocking_codes),
            "evidence_state": snapshot.evidence_state.value,
            "missing_fields_json": _array_json(snapshot.missing_fields),
            "conflicts_json": _array_json(snapshot.conflicts),
            "source_lineage_ids_json": _array_json(snapshot.source_lineage_ids),
            "evidence_fingerprint": snapshot.evidence_fingerprint,
            "created_at": _utc_text(snapshot.created_at, "brief creation time"),
        }
        if any(row[key] != value for key, value in projections.items()):
            raise BriefStoreError("brief snapshot projection authentication failed")
        decision = self.decision_store._load_decision_snapshot(
            snapshot.request_run_id,
            connection=connection,
        )
        if (
            decision.id != snapshot.decision_snapshot_id
            or decision.route.mode is not snapshot.mode
            or tuple(item.action_id for item in decision.route.actions)
            != snapshot.action_ids
        ):
            raise BriefStoreError("brief snapshot decision binding failed")
        if require_terminal:
            _authenticate_terminal_request(connection, snapshot)
        changed_raw = row["conclusion_changed"]
        if type(changed_raw) is not int or changed_raw not in {0, 1}:
            raise BriefStoreError("brief snapshot authentication failed")
        policy.validate()
        return StoredBriefSnapshot(
            id=snapshot_id,
            snapshot=snapshot,
            policy=policy,
            result_checksum=checksum,
            conclusion_changed=bool(changed_raw),
        )


def _authenticate_terminal_request(
    connection: sqlite3.Connection,
    snapshot: BriefSnapshot,
) -> None:
    row = connection.execute(
        """
        SELECT status, started_at, deadline_at, finished_at, omitted_work_json
        FROM request_runs WHERE id = ?
        """,
        (snapshot.request_run_id,),
    ).fetchone()
    if row is None or row["status"] not in {"complete", "partial"}:
        raise BriefStoreError("brief request terminal authentication failed")
    started_at = _stored_utc(row["started_at"], "request start")
    deadline_at = _stored_utc(row["deadline_at"], "request deadline")
    finished_at = _stored_utc(row["finished_at"], "request finish")
    if not (
        started_at <= snapshot.created_at <= finished_at <= deadline_at
    ):
        raise BriefStoreError("brief request lifetime authentication failed")
    omitted_bytes = _ascii_bytes(
        row["omitted_work_json"],
        "request omitted work",
        maximum=MAX_BRIEF_SUMMARY_JSON_BYTES,
    )
    omitted_value = _strict_json(
        omitted_bytes,
        maximum=MAX_BRIEF_SUMMARY_JSON_BYTES,
    )
    omitted_work = _string_tuple(omitted_value, "request omitted work")
    validate_identifier_tuple(omitted_work, "request omitted work")
    if len(omitted_work) > MAX_BRIEF_SUMMARY_ITEMS:
        raise BriefStoreError("request omitted work is too large")
    if canonical_json_bytes(omitted_work) != omitted_bytes:
        raise BriefStoreError("request omitted work authentication failed")
    if (row["status"] == "complete" and omitted_work) or (
        row["status"] == "partial" and not omitted_work
    ):
        raise BriefStoreError("brief request terminal authentication failed")


def _decode_snapshot(payload: bytes) -> BriefSnapshot:
    value = _strict_json(payload, maximum=MAX_BRIEF_SNAPSHOT_JSON_BYTES)
    _keys(
        value,
        {
            "action_ids",
            "action_maturity",
            "affected_action_abstentions",
            "blocking_codes",
            "conflicts",
            "coverage",
            "created_at",
            "decision_snapshot_id",
            "evidence_fingerprint",
            "evidence_state",
            "facts",
            "fund_code",
            "interpretations",
            "missing_fields",
            "mode",
            "official_events",
            "primary_state",
            "relationships",
            "request_run_id",
            "source_lineage_ids",
            "triggered_reviews",
        },
        "brief snapshot",
    )
    snapshot = BriefSnapshot(
        request_run_id=value["request_run_id"],
        decision_snapshot_id=value["decision_snapshot_id"],
        fund_code=value["fund_code"],
        action_ids=_string_tuple(value["action_ids"], "action ids"),
        mode=RequestMode(value["mode"]),
        facts=tuple(_decode_fact(item) for item in _list(value["facts"], "facts")),
        official_events=tuple(
            _decode_event(item) for item in _list(value["official_events"], "events")
        ),
        relationships=tuple(
            _decode_relationship(item)
            for item in _list(value["relationships"], "relationships")
        ),
        coverage=_decode_coverage(value["coverage"]),
        interpretations=tuple(
            _decode_interpretation(item)
            for item in _list(value["interpretations"], "interpretations")
        ),
        primary_state=BriefState(value["primary_state"]),
        action_maturity=ActionMaturity(value["action_maturity"]),
        triggered_reviews=_string_tuple(value["triggered_reviews"], "reviews"),
        affected_action_abstentions=_string_tuple(
            value["affected_action_abstentions"], "abstentions"
        ),
        blocking_codes=_string_tuple(value["blocking_codes"], "blocking codes"),
        evidence_state=BriefEvidenceState(value["evidence_state"]),
        missing_fields=_string_tuple(value["missing_fields"], "missing fields"),
        conflicts=_string_tuple(value["conflicts"], "conflicts"),
        source_lineage_ids=_string_tuple(value["source_lineage_ids"], "lineage ids"),
        evidence_fingerprint=value["evidence_fingerprint"],
        created_at=_stored_utc(value["created_at"], "brief creation time"),
    )
    snapshot.validate()
    return snapshot


def _decode_fact(value: object) -> BriefFact:
    _keys(
        value,
        {
            "calculated",
            "canonical_url",
            "completeness",
            "conflict_ids",
            "data_as_of",
            "fact_id",
            "field_id",
            "freshness",
            "published_at",
            "publisher",
            "retrieved_at",
            "source_id",
            "source_lineage_id",
            "source_tier",
            "unit",
            "value",
        },
        "brief fact",
    )
    return BriefFact(
        fact_id=value["fact_id"],
        field_id=value["field_id"],
        value=_restore_public(value["value"]),
        unit=value["unit"],
        data_as_of=_optional_utc(value["data_as_of"], "fact data time"),
        published_at=_optional_utc(value["published_at"], "fact publication time"),
        retrieved_at=_stored_utc(value["retrieved_at"], "fact retrieval time"),
        source_id=value["source_id"],
        source_tier=SourceTier(value["source_tier"]),
        publisher=value["publisher"],
        canonical_url=value["canonical_url"],
        freshness=EvidenceFreshness(value["freshness"]),
        completeness=EvidenceCompleteness(value["completeness"]),
        conflict_ids=_string_tuple(value["conflict_ids"], "fact conflicts"),
        calculated=value["calculated"],
        source_lineage_id=value["source_lineage_id"],
    )


def _decode_event(value: object) -> OfficialEvent:
    _keys(
        value,
        {
            "affected_action_ids",
            "canonical_url",
            "content_fingerprint",
            "event_code",
            "event_id",
            "integrity_status",
            "original_source_id",
            "published_at",
            "publisher",
            "quoted_source_id",
            "retrieved_at",
            "source_tier",
            "summary",
            "title",
        },
        "official event",
    )
    return OfficialEvent(
        event_id=value["event_id"],
        event_code=OfficialEventCode(value["event_code"]),
        title=value["title"],
        summary=value["summary"],
        publisher=value["publisher"],
        canonical_url=value["canonical_url"],
        published_at=_stored_utc(value["published_at"], "event publication time"),
        retrieved_at=_stored_utc(value["retrieved_at"], "event retrieval time"),
        source_tier=SourceTier(value["source_tier"]),
        original_source_id=value["original_source_id"],
        quoted_source_id=value["quoted_source_id"],
        content_fingerprint=value["content_fingerprint"],
        integrity_status=value["integrity_status"],
        affected_action_ids=_string_tuple(value["affected_action_ids"], "actions"),
    )


def _decode_relationship(value: object) -> RelationshipEvidence:
    _keys(
        value,
        {
            "evidence_ids",
            "evidence_state",
            "fund_codes",
            "metrics",
            "publication_times",
            "relationship_id",
            "relationship_type",
            "report_periods",
            "warnings",
        },
        "relationship",
    )
    return RelationshipEvidence(
        relationship_id=value["relationship_id"],
        relationship_type=value["relationship_type"],
        fund_codes=_string_tuple(value["fund_codes"], "fund codes"),
        evidence_state=BriefEvidenceState(value["evidence_state"]),
        metrics=_restore_public(value["metrics"]),
        evidence_ids=_string_tuple(value["evidence_ids"], "evidence ids"),
        report_periods=tuple(
            _stored_date(item, "report period")
            for item in _list(value["report_periods"], "report periods")
        ),
        publication_times=tuple(
            _stored_utc(item, "relationship publication time")
            for item in _list(value["publication_times"], "publication times")
        ),
        warnings=_string_tuple(value["warnings"], "warnings"),
    )


def _decode_coverage(value: object) -> BriefCoverage:
    _keys(
        value,
        {
            "coverage_id",
            "evidence_ids",
            "evidence_state",
            "included_fund_codes",
            "known_percent",
            "omitted_fund_codes",
            "scope",
            "unknown_fields",
        },
        "coverage",
    )
    return BriefCoverage(
        coverage_id=value["coverage_id"],
        scope=value["scope"],
        evidence_state=BriefEvidenceState(value["evidence_state"]),
        included_fund_codes=_string_tuple(value["included_fund_codes"], "included"),
        omitted_fund_codes=_string_tuple(value["omitted_fund_codes"], "omitted"),
        known_percent=value["known_percent"],
        unknown_fields=_string_tuple(value["unknown_fields"], "unknown fields"),
        evidence_ids=_string_tuple(value["evidence_ids"], "evidence ids"),
    )


def _decode_interpretation(value: object) -> BriefActionInterpretation:
    _keys(
        value,
        {
            "action_id",
            "action_maturity",
            "blocking_codes",
            "exact_amount_available",
            "invalidation_conditions",
            "missing_fields",
            "opposing_evidence_ids",
            "state",
            "state_inputs",
            "supporting_evidence_ids",
            "unavailable_actions",
        },
        "interpretation",
    )
    return BriefActionInterpretation(
        action_id=value["action_id"],
        state=BriefState(value["state"]),
        action_maturity=ActionMaturity(value["action_maturity"]),
        supporting_evidence_ids=_string_tuple(value["supporting_evidence_ids"], "support"),
        opposing_evidence_ids=_string_tuple(value["opposing_evidence_ids"], "opposition"),
        blocking_codes=_string_tuple(value["blocking_codes"], "blocks"),
        missing_fields=_string_tuple(value["missing_fields"], "missing"),
        invalidation_conditions=_string_tuple(
            value["invalidation_conditions"], "conditions"
        ),
        unavailable_actions=_string_tuple(value["unavailable_actions"], "unavailable"),
        exact_amount_available=value["exact_amount_available"],
        state_inputs=_restore_public(value["state_inputs"]),
    )


def _conclusion_bytes(snapshot: BriefSnapshot) -> bytes:
    return canonical_json_bytes(
        {
            "action_ids": snapshot.action_ids,
            "action_maturity": snapshot.action_maturity,
            "affected_action_abstentions": snapshot.affected_action_abstentions,
            "blocking_codes": snapshot.blocking_codes,
            "conflicts": snapshot.conflicts,
            "evidence_state": snapshot.evidence_state,
            "interpretations": tuple(
                {
                    "action_id": item.action_id,
                    "action_maturity": item.action_maturity,
                    "blocking_codes": item.blocking_codes,
                    "missing_fields": item.missing_fields,
                    "state": item.state,
                }
                for item in snapshot.interpretations
            ),
            "missing_fields": snapshot.missing_fields,
            "primary_state": snapshot.primary_state,
            "triggered_reviews": snapshot.triggered_reviews,
        }
    )


def _strict_json(
    payload: bytes,
    *,
    maximum: int = MAX_BRIEF_SNAPSHOT_JSON_BYTES,
) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("unsupported JSON constant")

    try:
        if type(payload) is not bytes:
            raise ValueError("payload must be exact bytes")
        _require_maximum_bytes(payload, maximum, "canonical JSON")
        text = payload.decode("ascii")
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError):
        raise BriefStoreError("brief snapshot authentication failed") from None


def _keys(value: object, expected: set, name: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise BriefStoreError(f"{name} has an invalid canonical shape")


def _list(value: object, name: str) -> list:
    if type(value) is not list:
        raise BriefStoreError(f"{name} must be a canonical array")
    return value


def _string_tuple(value: object, name: str) -> Tuple[str, ...]:
    items = _list(value, name)
    if any(type(item) is not str for item in items):
        raise BriefStoreError(f"{name} must contain exact strings")
    return tuple(items)


def _restore_public(value: object, *, depth: int = 0) -> object:
    if depth > MAX_BRIEF_PUBLIC_TREE_DEPTH:
        raise BriefStoreError("brief public tree exceeds the depth limit")
    if type(value) is list:
        return tuple(_restore_public(item, depth=depth + 1) for item in value)
    if type(value) is dict:
        return {
            key: _restore_public(item, depth=depth + 1)
            for key, item in value.items()
        }
    return value


def _array_json(value: Tuple[str, ...]) -> str:
    if type(value) is not tuple or len(value) > MAX_BRIEF_SUMMARY_ITEMS:
        raise BriefStoreError("brief summary array is too large")
    encoded = canonical_json_bytes(value)
    _require_maximum_bytes(
        encoded,
        MAX_BRIEF_SUMMARY_JSON_BYTES,
        "brief summary array",
    )
    return encoded.decode("ascii")


def _ascii_bytes(
    value: object,
    name: str,
    *,
    maximum: Optional[int] = None,
) -> bytes:
    if type(value) is not str:
        raise BriefStoreError(f"{name} is not canonical ASCII")
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        raise BriefStoreError(f"{name} is not canonical ASCII") from None
    if maximum is not None:
        _require_maximum_bytes(encoded, maximum, name)
    return encoded


def _require_maximum_bytes(value: bytes, maximum: int, name: str) -> None:
    if type(value) is not bytes or type(maximum) is not int or maximum <= 0:
        raise ValueError("byte-bound validation requires exact positive inputs")
    if len(value) > maximum:
        raise BriefStoreError(f"{name} is too large")


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _fund_code(value: object) -> str:
    if type(value) is not str or _FUND_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("fund code must be exactly six ASCII digits")
    return value


def _checksum(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BriefStoreError(f"{name} is invalid")
    return value


def _utc_text(value: object, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an exact UTC datetime")
    return value.isoformat()


def _stored_utc(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise BriefStoreError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise BriefStoreError(f"{name} is invalid") from None
    if _utc_text(parsed, name) != value:
        raise BriefStoreError(f"{name} is invalid")
    return parsed


def _optional_utc(value: object, name: str) -> Optional[datetime]:
    return None if value is None else _stored_utc(value, name)


def _stored_date(value: object, name: str) -> date:
    if type(value) is not str:
        raise BriefStoreError(f"{name} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise BriefStoreError(f"{name} is invalid") from None
    if parsed.isoformat() != value:
        raise BriefStoreError(f"{name} is invalid")
    return parsed
