from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestTerminalStatus,
    SourceTier,
    canonical_json_bytes,
    validate_identifier_tuple,
)
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.intelligence.models import (
    DimensionObservation,
    DimensionState,
    EventConfidenceState,
    EventEntityLink,
    EventEntityRelationship,
    EventType,
    IntegrityState,
    IntelligenceSnapshot,
    IntelligenceWorkflow,
    LineageEdge,
    LineageKind,
    MarketDimension,
    MarketEntity,
    MarketShadowState,
    MarketStateSnapshot,
    MetricId,
    NewsEvent,
    NewsItem,
    QueryInterval,
    SectorShadowState,
)
from kunjin.intelligence.policy import IntelligencePolicyV1
from kunjin.storage.repository import Repository

MAX_POLICY_BYTES = 128 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024


class IntelligenceStoreError(RuntimeError):
    """A sanitized authenticated-intelligence persistence failure."""


@dataclass(frozen=True)
class StoredIntelligenceSnapshot:
    id: int
    snapshot: IntelligenceSnapshot
    result_checksum: str


@dataclass(frozen=True)
class StoredIntegrityTransition:
    id: int
    transition_id: str
    request_run_id: int
    item_id: int
    evidence_item_id: int
    previous_state: IntegrityState
    current_state: IntegrityState
    occurred_at: datetime
    event_checksum: str


class IntelligenceStore:
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
        self.policy = IntelligencePolicyV1()
        self.policy.validate()

    def save_items(
        self,
        items: Tuple[NewsItem, ...],
        connection: sqlite3.Connection,
    ) -> Tuple[int, ...]:
        if type(items) is not tuple or not items:
            raise ValueError("items must be a nonempty exact tuple")
        _connection(connection)
        try:
            self._configure_connection(connection)
            self._authenticate_or_insert_policy(connection)
            stored_ids = []
            seen_keys = set()
            for item in items:
                if type(item) is not NewsItem:
                    raise ValueError("items must contain exact NewsItem records")
                item.validate()
                if item.item_id in seen_keys:
                    raise ValueError("item identifiers must be unique")
                seen_keys.add(item.item_id)
                if item.excerpt is None or item.excerpt_expired_at is not None:
                    raise ValueError("new intelligence items require an active excerpt")
                self._authenticate_item_attempt(connection, item)
                excerpt_bytes = item.excerpt.encode("utf-8")
                excerpt_checksum = hashlib.sha256(excerpt_bytes).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT INTO intelligence_news_items(
                        item_key, source_id, publisher, canonical_url, title,
                        excerpt_original_bytes, excerpt_sha256, published_at,
                        publication_precision, publication_interval_end, retrieved_at,
                        source_tier, content_fingerprint, category, integrity_state,
                        source_attempt_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.item_id,
                        item.source_id,
                        item.publisher,
                        item.canonical_url,
                        item.title,
                        item.excerpt_original_bytes,
                        excerpt_checksum,
                        _utc_text(item.published_at),
                        item.publication_precision,
                        _optional_utc_text(item.publication_interval_end),
                        _utc_text(item.retrieved_at),
                        item.source_tier.value,
                        item.content_fingerprint,
                        item.category,
                        item.integrity_state.value,
                        item.source_attempt_id,
                    ),
                )
                stored_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO intelligence_news_excerpts(
                        item_id, excerpt_text, truncated, expires_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        stored_id,
                        item.excerpt,
                        int(item.excerpt_truncated),
                        _utc_text(item.excerpt_expires_at),
                    ),
                )
                if self._authenticated_item(connection, stored_id) != item:
                    raise IntelligenceStoreError("intelligence item authentication failed")
                stored_ids.append(stored_id)
            return tuple(stored_ids)
        except IntelligenceStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            message = str(exc)
            if "source attempt binding" in message:
                raise IntelligenceStoreError(
                    "intelligence item source attempt binding failed"
                ) from None
            raise IntelligenceStoreError("intelligence item persistence failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise IntelligenceStoreError("intelligence item validation failed") from None

    def save_lineage_and_events(
        self,
        edges: Tuple[LineageEdge, ...],
        events: Tuple[NewsEvent, ...],
        connection: sqlite3.Connection,
    ) -> None:
        if type(edges) is not tuple or type(events) is not tuple:
            raise ValueError("lineage and events must be exact tuples")
        _connection(connection)
        try:
            self._authenticate_or_insert_policy(connection)
            edge_keys = set()
            for edge in edges:
                if type(edge) is not LineageEdge:
                    raise ValueError("edges must contain exact LineageEdge records")
                edge.validate()
                if edge.edge_id in edge_keys:
                    raise ValueError("lineage edge identifiers must be unique")
                edge_keys.add(edge.edge_id)
                from_id = self._item_row_id(connection, edge.from_item_id)
                to_id = self._item_row_id(connection, edge.to_item_id)
                canonical = canonical_json_bytes(edge.to_canonical_dict())
                _bounded(canonical, "lineage edge")
                checksum = hashlib.sha256(canonical).hexdigest()
                connection.execute(
                    """
                    INSERT INTO intelligence_lineage_edges(
                        edge_key, from_item_id, to_item_id, kind,
                        evidence_ids_json, canonical_edge_json, edge_checksum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edge.edge_id,
                        from_id,
                        to_id,
                        edge.kind.value,
                        _ascii_json(edge.evidence_ids),
                        canonical.decode("ascii"),
                        checksum,
                    ),
                )
                self._authenticate_payload(
                    connection,
                    "intelligence_lineage_edges",
                    "edge_key",
                    edge.edge_id,
                    "canonical_edge_json",
                    "edge_checksum",
                    canonical,
                )

            event_keys = set()
            prepared_events = []
            for event in events:
                if type(event) is not NewsEvent:
                    raise ValueError("events must contain exact NewsEvent records")
                event.validate()
                if event.event_id in event_keys:
                    raise ValueError("event identifiers must be unique")
                event_keys.add(event.event_id)
                canonical = canonical_json_bytes(event.to_canonical_dict())
                _bounded(canonical, "intelligence event")
                checksum = hashlib.sha256(canonical).hexdigest()
                prepared_events.append((event, canonical, checksum))

            existing_event_keys = {
                str(row["event_key"])
                for row in connection.execute(
                    "SELECT event_key FROM intelligence_events"
                ).fetchall()
            }
            for event, _canonical, _checksum_value in prepared_events:
                target = event.superseded_by_event_id
                if target is not None and target not in event_keys | existing_event_keys:
                    raise IntelligenceStoreError(
                        "superseded event target authentication failed"
                    )

            event_row_ids = {}
            for event, canonical, checksum in prepared_events:
                cursor = connection.execute(
                    """
                    INSERT INTO intelligence_events(
                        event_key, event_type, normalized_title, confidence_state,
                        earliest_published_at, latest_published_at, integrity_state,
                        superseded_by_event_key, invalidation_conditions_json,
                        canonical_event_json, event_checksum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.event_type.value,
                        event.normalized_title,
                        event.confidence_state.value,
                        _utc_text(event.earliest_published_at),
                        _utc_text(event.latest_published_at),
                        event.integrity_state.value,
                        event.superseded_by_event_id,
                        _ascii_json(event.invalidation_conditions),
                        canonical.decode("ascii"),
                        checksum,
                    ),
                )
                event_row_ids[event.event_id] = int(cursor.lastrowid)

            for event, _canonical, _checksum_value in prepared_events:
                roles = (
                    ("supporting", event.supporting_item_ids),
                    ("opposing", event.opposing_item_ids),
                    ("correction", event.correction_item_ids),
                    ("retraction", event.retraction_item_ids),
                )
                for role, item_keys in roles:
                    for item_key in item_keys:
                        connection.execute(
                            "INSERT INTO intelligence_event_items(event_id, item_id, role) "
                            "VALUES (?, ?, ?)",
                            (
                                event_row_ids[event.event_id],
                                self._item_row_id(connection, item_key),
                                role,
                            ),
                        )

            for event, canonical, _checksum_value in prepared_events:
                self._authenticate_payload(
                    connection,
                    "intelligence_events",
                    "event_key",
                    event.event_id,
                    "canonical_event_json",
                    "event_checksum",
                    canonical,
                )
                if event.superseded_by_event_id is not None:
                    self._authenticate_event_row(
                        connection,
                        event.superseded_by_event_id,
                    )
        except IntelligenceStoreError:
            raise
        except sqlite3.DatabaseError:
            raise IntelligenceStoreError("intelligence lineage persistence failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise IntelligenceStoreError("intelligence lineage validation failed") from None

    def expire_excerpts(self, now: datetime) -> int:
        cutoff = _utc_text(now)
        try:
            with self.repository.connect() as connection, connection:
                self._configure_connection(connection, expiry_cutoff=cutoff)
                rows = connection.execute(
                    "SELECT item_id FROM intelligence_news_excerpts "
                    "WHERE expires_at <= ? ORDER BY item_id",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    connection.execute(
                        "DELETE FROM intelligence_news_excerpts WHERE item_id=?",
                        (row["item_id"],),
                    )
                return len(rows)
        except sqlite3.DatabaseError:
            raise IntelligenceStoreError("intelligence excerpt expiry failed") from None

    def record_integrity_transition(
        self,
        *,
        transition_id: str,
        request_run_id: int,
        item_id: int,
        evidence_item_id: int,
        new_state: IntegrityState,
        occurred_at: datetime,
        connection: sqlite3.Connection,
    ) -> StoredIntegrityTransition:
        _positive_id(request_run_id, "request run id")
        _positive_id(item_id, "item id")
        _positive_id(evidence_item_id, "evidence item id")
        if type(transition_id) is not str or not transition_id:
            raise ValueError("transition id must be a nonempty exact string")
        if type(new_state) is not IntegrityState:
            raise ValueError("new state must be an exact IntegrityState")
        occurred_text = _utc_text(occurred_at)
        _connection(connection)
        if item_id == evidence_item_id:
            raise IntelligenceStoreError("integrity evidence cannot reference itself")
        try:
            base_item = self._base_authenticated_item(connection, item_id)
            evidence_item = self._authenticated_item(connection, evidence_item_id)
            history = self._integrity_history(connection, base_item)
            previous_state = (
                base_item.integrity_state if not history else history[-1].current_state
            )
            if not _allowed_integrity_transition(previous_state, new_state):
                raise IntelligenceStoreError("integrity transition is not forward-only")
            if occurred_at <= max(base_item.retrieved_at, evidence_item.retrieved_at):
                raise IntelligenceStoreError("integrity transition time is not monotonic")
            if history and occurred_at <= history[-1].occurred_at:
                raise IntelligenceStoreError("integrity transition time is not monotonic")
            self._authenticate_transition_request_evidence(
                connection,
                request_run_id,
                evidence_item_id,
                require_running=True,
            )
            canonical = _integrity_transition_bytes(
                transition_id=transition_id,
                request_run_id=request_run_id,
                item_key=base_item.item_id,
                evidence_item_key=evidence_item.item_id,
                previous_state=previous_state,
                current_state=new_state,
                occurred_at=occurred_at,
            )
            checksum = hashlib.sha256(canonical).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO intelligence_item_integrity_events(
                    integrity_event_key, request_run_id, item_id, previous_state,
                    current_state, evidence_item_id, occurred_at,
                    canonical_event_json, event_checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transition_id,
                    request_run_id,
                    item_id,
                    previous_state.value,
                    new_state.value,
                    evidence_item_id,
                    occurred_text,
                    canonical.decode("ascii"),
                    checksum,
                ),
            )
            stored = self._integrity_transition_from_row(
                connection,
                connection.execute(
                    "SELECT * FROM intelligence_item_integrity_events WHERE id=?",
                    (int(cursor.lastrowid),),
                ).fetchone(),
                base_item,
                expected_previous=previous_state,
            )
            if stored.current_state is not new_state or stored.occurred_at != occurred_at:
                raise IntelligenceStoreError("integrity transition authentication failed")
            return stored
        except IntelligenceStoreError:
            raise
        except sqlite3.DatabaseError:
            raise IntelligenceStoreError("integrity transition persistence failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise IntelligenceStoreError("integrity transition validation failed") from None

    def integrity_history(self, item_id: int) -> Tuple[StoredIntegrityTransition, ...]:
        _positive_id(item_id, "item id")
        try:
            with self.repository.connect() as connection:
                base_item = self._base_authenticated_item(connection, item_id)
                return self._integrity_history(connection, base_item)
        except IntelligenceStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, UnicodeError, KeyError):
            raise IntelligenceStoreError("integrity authentication failed") from None

    def publish_snapshot(
        self,
        request_run_id: int,
        snapshot_factory: Callable[[int], IntelligenceSnapshot],
        finished_at: datetime,
        terminal_status: RequestTerminalStatus,
        omitted_work: Tuple[str, ...],
        budget: RequestBudget,
    ) -> StoredIntelligenceSnapshot:
        _positive_id(request_run_id, "request run id")
        if not callable(snapshot_factory):
            raise ValueError("snapshot factory must be callable")
        if type(terminal_status) is not RequestTerminalStatus or terminal_status not in {
            RequestTerminalStatus.COMPLETE,
            RequestTerminalStatus.PARTIAL,
        }:
            raise ValueError("snapshot status must be complete or partial")
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        validate_identifier_tuple(omitted_work, "omitted work")
        if (terminal_status is RequestTerminalStatus.COMPLETE and omitted_work) or (
            terminal_status is RequestTerminalStatus.PARTIAL and not omitted_work
        ):
            raise ValueError("terminal status and omitted work are inconsistent")
        _utc_text(finished_at)
        budget.require_publishable()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_or_insert_policy(connection)
                    try:
                        snapshot = snapshot_factory(request_run_id)
                    except Exception:
                        raise IntelligenceStoreError(
                            "intelligence snapshot factory failed"
                        ) from None
                    if type(snapshot) is not IntelligenceSnapshot:
                        raise IntelligenceStoreError(
                            "intelligence snapshot factory returned an invalid record"
                        )
                    snapshot.validate()
                    item_uses = self._validate_snapshot_bindings(
                        connection, snapshot, request_run_id, budget
                    )
                    self._save_item_uses(connection, request_run_id, item_uses)
                    entity_ids = self._save_entities(connection, snapshot.entities)
                    observation_ids = self._save_observations(
                        connection,
                        snapshot.market_state.dimensions,
                        entity_ids,
                        request_run_id,
                    )
                    self._save_event_entity_links(
                        connection,
                        snapshot.event_entity_links,
                        entity_ids,
                    )
                    state_id = self._save_market_state(
                        connection,
                        request_run_id,
                        snapshot.market_state,
                        observation_ids,
                        snapshot.created_at,
                    )
                    stored_id = self._save_snapshot(
                        connection,
                        request_run_id,
                        state_id,
                        snapshot,
                    )
                    self.decision_store.finalize_request(
                        request_run_id,
                        terminal_status,
                        finished_at,
                        omitted_work,
                        budget=budget,
                        connection=connection,
                    )
                    budget.require_publishable()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            return self.authenticated_snapshot(stored_id)
        except (BudgetExpired, IntelligenceStoreError):
            raise
        except DecisionAuditStoreError:
            raise IntelligenceStoreError("intelligence request finalization failed") from None
        except sqlite3.DatabaseError:
            raise IntelligenceStoreError("intelligence snapshot publication failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError):
            raise IntelligenceStoreError("intelligence snapshot validation failed") from None

    def authenticated_item(self, item_id: int) -> NewsItem:
        _positive_id(item_id, "item id")
        try:
            with self.repository.connect() as connection:
                return self._authenticated_item(connection, item_id)
        except IntelligenceStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, UnicodeError):
            raise IntelligenceStoreError("intelligence item authentication failed") from None

    def lineage_for_item(self, item_id: int) -> Tuple[LineageEdge, ...]:
        _positive_id(item_id, "item id")
        try:
            with self.repository.connect() as connection:
                if (
                    connection.execute(
                        "SELECT 1 FROM intelligence_news_items WHERE id=?", (item_id,)
                    ).fetchone()
                    is None
                ):
                    raise IntelligenceStoreError("intelligence item authentication failed")
                rows = connection.execute(
                    """
                    SELECT edge.* FROM intelligence_lineage_edges AS edge
                    WHERE edge.from_item_id=? OR edge.to_item_id=?
                    ORDER BY edge.id
                    """,
                    (item_id, item_id),
                ).fetchall()
                return tuple(
                    self._lineage_from_authenticated_row(connection, row) for row in rows
                )
        except IntelligenceStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, UnicodeError):
            raise IntelligenceStoreError("intelligence lineage authentication failed") from None

    def authenticated_snapshot(self, snapshot_id: int) -> StoredIntelligenceSnapshot:
        _positive_id(snapshot_id, "snapshot id")
        try:
            with self.repository.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM intelligence_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if row is None:
                    raise IntelligenceStoreError("intelligence snapshot authentication failed")
                canonical = _ascii_bytes(row["canonical_snapshot_json"], "snapshot")
                checksum = _checksum(row["result_checksum"], "snapshot checksum")
                if hashlib.sha256(canonical).hexdigest() != checksum:
                    raise IntelligenceStoreError("intelligence snapshot authentication failed")
                snapshot = _snapshot_from_dict(_json_object(canonical, "snapshot"))
                if snapshot.canonical_json() != canonical:
                    raise IntelligenceStoreError("intelligence snapshot authentication failed")
                if (
                    snapshot.request_run_id != row["request_run_id"]
                    or snapshot.workflow.value != row["workflow"]
                    or snapshot.created_at != _stored_utc(row["created_at"])
                ):
                    raise IntelligenceStoreError("intelligence snapshot authentication failed")
                try:
                    self._authenticate_or_insert_policy(connection)
                    self._authenticate_snapshot_graph(connection, row, snapshot)
                except IntelligenceStoreError:
                    raise IntelligenceStoreError(
                        "intelligence snapshot authentication failed"
                    ) from None
                return StoredIntelligenceSnapshot(int(row["id"]), snapshot, checksum)
        except IntelligenceStoreError:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError, UnicodeError, KeyError):
            raise IntelligenceStoreError("intelligence snapshot authentication failed") from None

    @staticmethod
    def _configure_connection(
        connection: sqlite3.Connection,
        *,
        expiry_cutoff: Optional[str] = None,
    ) -> None:
        connection.create_function(
            "sha256",
            1,
            lambda value: hashlib.sha256(str(value).encode("utf-8")).digest(),
            deterministic=True,
        )
        if expiry_cutoff is not None:
            connection.create_function(
                "kunjin_excerpt_expiry_cutoff",
                0,
                lambda: expiry_cutoff,
                deterministic=True,
            )

    def _authenticate_or_insert_policy(self, connection: sqlite3.Connection) -> None:
        canonical = self.policy.canonical_json()
        _bounded(canonical, "intelligence policy", maximum=MAX_POLICY_BYTES)
        checksum = self.policy.checksum()
        row = connection.execute(
            "SELECT * FROM intelligence_policy_versions WHERE version=?",
            (self.policy.version,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO intelligence_policy_versions(
                    version, canonical_policy_json, policy_checksum, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    self.policy.version,
                    canonical.decode("ascii"),
                    checksum,
                    _utc_text(datetime.now(timezone.utc)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM intelligence_policy_versions WHERE version=?",
                (self.policy.version,),
            ).fetchone()
        if row is None:
            raise IntelligenceStoreError("intelligence policy authentication failed")
        stored = _ascii_bytes(row["canonical_policy_json"], "intelligence policy")
        if (
            row["policy_checksum"] != checksum
            or not hmac.compare_digest(stored, canonical)
            or hashlib.sha256(stored).hexdigest() != checksum
        ):
            raise IntelligenceStoreError("intelligence policy authentication failed")

    def _authenticate_snapshot_graph(
        self,
        connection: sqlite3.Connection,
        snapshot_row: sqlite3.Row,
        snapshot: IntelligenceSnapshot,
    ) -> None:
        if snapshot_row["policy_version"] != self.policy.version:
            raise IntelligenceStoreError("intelligence snapshot authentication failed")
        run = connection.execute(
            "SELECT * FROM request_runs WHERE id=?", (snapshot.request_run_id,)
        ).fetchone()
        if run is None or (
            run["request_id"] != snapshot.request_id
            or run["status"] not in {"complete", "partial"}
            or run["finished_at"] is None
        ):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")

        state_row = connection.execute(
            "SELECT * FROM market_state_snapshots WHERE id=?",
            (snapshot_row["market_state_snapshot_id"],),
        ).fetchone()
        if state_row is None or (
            state_row["request_run_id"] != snapshot.request_run_id
            or state_row["policy_version"] != self.policy.version
            or _stored_utc(state_row["created_at"]) != snapshot.created_at
        ):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")
        state_canonical = _ascii_bytes(state_row["canonical_state_json"], "market state")
        state_checksum = _checksum(state_row["state_checksum"], "market state checksum")
        expected_state = canonical_json_bytes(snapshot.market_state.to_canonical_dict())
        if (
            not hmac.compare_digest(state_canonical, expected_state)
            or hashlib.sha256(state_canonical).hexdigest() != state_checksum
            or snapshot.market_state.policy_checksum != self.policy.checksum()
        ):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")

        attempt_rows = connection.execute(
            "SELECT * FROM source_attempts WHERE request_run_id=? ORDER BY id",
            (snapshot.request_run_id,),
        ).fetchall()
        attempts = {
            int(row["id"]): row
            for row in attempt_rows
            if int(row["id"]) in snapshot.source_attempt_ids
        }
        if set(attempts) != set(snapshot.source_attempt_ids) or any(
            row["outcome"] not in {"success", "cache_hit"} for row in attempts.values()
        ):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")

        item_rows = connection.execute(
            "SELECT id, item_key, source_id FROM intelligence_news_items "
            f"WHERE item_key IN ({','.join('?' for _ in snapshot.item_ids)})",
            snapshot.item_ids,
        ).fetchall()
        if len(item_rows) != len(snapshot.item_ids):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")
        items_by_key = {str(row["item_key"]): row for row in item_rows}
        for item_key in snapshot.item_ids:
            self._authenticated_item(connection, int(items_by_key[item_key]["id"]))
        uses = connection.execute(
            """
            SELECT item_use.item_id, item_use.source_attempt_id
            FROM intelligence_snapshot_item_uses AS item_use
            WHERE item_use.request_run_id=? ORDER BY item_use.item_id
            """,
            (snapshot.request_run_id,),
        ).fetchall()
        use_map = {int(row["item_id"]): int(row["source_attempt_id"]) for row in uses}
        if set(use_map) != {int(row["id"]) for row in item_rows}:
            raise IntelligenceStoreError("intelligence snapshot authentication failed")
        for _item_key, item_row in items_by_key.items():
            attempt = attempts.get(use_map[int(item_row["id"])])
            if attempt is None or attempt["source_id"] != item_row["source_id"]:
                raise IntelligenceStoreError("intelligence snapshot authentication failed")

        self._authenticate_snapshot_entities(connection, snapshot)
        self._authenticate_snapshot_observations(connection, state_row, snapshot)
        for edge_key in snapshot.lineage_edge_ids:
            edge_row = connection.execute(
                "SELECT * FROM intelligence_lineage_edges WHERE edge_key=?", (edge_key,)
            ).fetchone()
            if edge_row is None or (
                self._lineage_from_authenticated_row(connection, edge_row).edge_id
                != edge_key
            ):
                raise IntelligenceStoreError("intelligence snapshot authentication failed")
        for event_key in snapshot.event_ids:
            self._authenticate_event_row(connection, event_key)
        self._authenticate_snapshot_event_links(connection, snapshot)
        graph_item_keys = self._snapshot_graph_item_keys(connection, snapshot)
        if not graph_item_keys.issubset(snapshot.item_ids):
            raise IntelligenceStoreError("intelligence snapshot authentication failed")

    def _authenticate_snapshot_entities(
        self,
        connection: sqlite3.Connection,
        snapshot: IntelligenceSnapshot,
    ) -> None:
        for entity in snapshot.entities:
            canonical = canonical_json_bytes(entity.to_canonical_dict())
            self._authenticate_payload(
                connection,
                "market_entities",
                "entity_key",
                entity.entity_id,
                "canonical_entity_json",
                "entity_checksum",
                canonical,
            )

    def _authenticate_snapshot_observations(
        self,
        connection: sqlite3.Connection,
        state_row: sqlite3.Row,
        snapshot: IntelligenceSnapshot,
    ) -> None:
        observation_keys = tuple(
            observation.observation_id for observation in snapshot.market_state.dimensions
        )
        stored_keys = tuple(
            _json_array(
                _ascii_bytes(state_row["observation_ids_json"], "observation ids"),
                "observation ids",
            )
        )
        if stored_keys != observation_keys:
            raise IntelligenceStoreError("intelligence snapshot authentication failed")
        for observation in snapshot.market_state.dimensions:
            row = connection.execute(
                """
                SELECT observation.*, entity.entity_key
                FROM market_dimension_observations AS observation
                JOIN market_entities AS entity ON entity.id=observation.entity_id
                WHERE observation.observation_key=?
                """,
                (observation.observation_id,),
            ).fetchone()
            if row is None or row["entity_key"] != observation.entity_id:
                raise IntelligenceStoreError("intelligence snapshot authentication failed")
            canonical = canonical_json_bytes(observation.to_canonical_dict())
            if (
                _ascii_bytes(row["canonical_observation_json"], "observation") != canonical
                or hashlib.sha256(canonical).hexdigest() != row["observation_checksum"]
                or tuple(
                    _json_array(
                        _ascii_bytes(row["source_attempt_ids_json"], "attempt ids"),
                        "attempt ids",
                    )
                )
                != observation.source_attempt_ids
            ):
                raise IntelligenceStoreError("intelligence snapshot authentication failed")

    def _authenticate_snapshot_event_links(
        self,
        connection: sqlite3.Connection,
        snapshot: IntelligenceSnapshot,
    ) -> None:
        for link in snapshot.event_entity_links:
            row = connection.execute(
                """
                SELECT link.*, event.event_key, entity.entity_key
                FROM intelligence_event_entities AS link
                JOIN intelligence_events AS event ON event.id=link.event_id
                JOIN market_entities AS entity ON entity.id=link.entity_id
                WHERE link.link_key=?
                """,
                (link.link_id,),
            ).fetchone()
            canonical = canonical_json_bytes(link.to_canonical_dict())
            if row is None or (
                row["event_key"] != link.event_id
                or row["entity_key"] != link.entity_id
                or _ascii_bytes(row["canonical_link_json"], "event entity link") != canonical
                or hashlib.sha256(canonical).hexdigest() != row["link_checksum"]
            ):
                raise IntelligenceStoreError("intelligence snapshot authentication failed")
    @staticmethod
    def _authenticate_item_attempt(connection: sqlite3.Connection, item: NewsItem) -> None:
        row = connection.execute(
            "SELECT * FROM source_attempts WHERE id=?", (item.source_attempt_id,)
        ).fetchone()
        if row is None or (
            row["source_id"] != item.source_id
            or row["outcome"] not in {"success", "cache_hit"}
            or _stored_utc(row["finished_at"]) > item.retrieved_at
        ):
            raise IntelligenceStoreError("intelligence item source attempt binding failed")

    def _authenticated_item(self, connection: sqlite3.Connection, item_id: int) -> NewsItem:
        item = self._base_authenticated_item(connection, item_id)
        history = self._integrity_history(connection, item)
        if not history:
            return item
        current = NewsItem(
            **{**item.__dict__, "integrity_state": history[-1].current_state}
        )
        current.validate()
        return current

    def _base_authenticated_item(
        self,
        connection: sqlite3.Connection,
        item_id: int,
    ) -> NewsItem:
        row = connection.execute(
            """
            SELECT item.*, excerpt.excerpt_text, excerpt.truncated, excerpt.expires_at
            FROM intelligence_news_items AS item
            LEFT JOIN intelligence_news_excerpts AS excerpt ON excerpt.item_id=item.id
            WHERE item.id=?
            """,
            (item_id,),
        ).fetchone()
        if row is None:
            raise IntelligenceStoreError("intelligence item authentication failed")
        excerpt = row["excerpt_text"]
        if excerpt is not None:
            excerpt_checksum = hashlib.sha256(str(excerpt).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(excerpt_checksum, row["excerpt_sha256"]):
                raise IntelligenceStoreError("intelligence item authentication failed")
            truncated = bool(row["truncated"])
            expires_at = _stored_utc(row["expires_at"])
            expired_at = None
        else:
            truncated = int(row["excerpt_original_bytes"]) > 2048
            expires_at = _stored_utc(row["retrieved_at"]) + timedelta(days=365)
            expired_at = expires_at
        item = NewsItem(
            item_id=str(row["item_key"]),
            source_id=str(row["source_id"]),
            publisher=str(row["publisher"]),
            canonical_url=str(row["canonical_url"]),
            title=str(row["title"]),
            excerpt=None if excerpt is None else str(excerpt),
            excerpt_truncated=truncated,
            excerpt_original_bytes=int(row["excerpt_original_bytes"]),
            excerpt_expires_at=expires_at,
            excerpt_expired_at=expired_at,
            published_at=_stored_utc(row["published_at"]),
            publication_precision=str(row["publication_precision"]),
            publication_interval_end=(
                None
                if row["publication_interval_end"] is None
                else _stored_utc(row["publication_interval_end"])
            ),
            retrieved_at=_stored_utc(row["retrieved_at"]),
            source_tier=SourceTier(str(row["source_tier"])),
            content_fingerprint=_checksum(row["content_fingerprint"], "content fingerprint"),
            category=str(row["category"]),
            integrity_state=IntegrityState(str(row["integrity_state"])),
            source_attempt_id=int(row["source_attempt_id"]),
        )
        item.validate()
        self._authenticate_item_attempt(connection, item)
        return item

    def _integrity_history(
        self,
        connection: sqlite3.Connection,
        base_item: NewsItem,
    ) -> Tuple[StoredIntegrityTransition, ...]:
        item_row_id = self._item_row_id(connection, base_item.item_id)
        rows = connection.execute(
            """
            SELECT * FROM intelligence_item_integrity_events
            WHERE item_id=? ORDER BY occurred_at, id
            """,
            (item_row_id,),
        ).fetchall()
        history = []
        previous = base_item.integrity_state
        previous_time = base_item.retrieved_at
        for row in rows:
            transition = self._integrity_transition_from_row(
                connection,
                row,
                base_item,
                expected_previous=previous,
            )
            if transition.occurred_at <= previous_time:
                raise IntelligenceStoreError("integrity authentication failed")
            history.append(transition)
            previous = transition.current_state
            previous_time = transition.occurred_at
        return tuple(history)

    def _integrity_transition_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        base_item: NewsItem,
        *,
        expected_previous: IntegrityState,
    ) -> StoredIntegrityTransition:
        if row is None:
            raise IntelligenceStoreError("integrity authentication failed")
        previous_state = IntegrityState(str(row["previous_state"]))
        current_state = IntegrityState(str(row["current_state"]))
        if previous_state is not expected_previous or not _allowed_integrity_transition(
            previous_state, current_state
        ):
            raise IntelligenceStoreError("integrity authentication failed")
        item_id = int(row["item_id"])
        evidence_item_id = int(row["evidence_item_id"])
        if item_id == evidence_item_id:
            raise IntelligenceStoreError("integrity authentication failed")
        evidence_item = self._base_authenticated_item(connection, evidence_item_id)
        request_run_id = int(row["request_run_id"])
        self._authenticate_transition_request_evidence(
            connection,
            request_run_id,
            evidence_item_id,
            require_running=False,
        )
        occurred_at = _stored_utc(row["occurred_at"])
        canonical = _ascii_bytes(row["canonical_event_json"], "integrity transition")
        checksum = _checksum(row["event_checksum"], "integrity transition checksum")
        expected = _integrity_transition_bytes(
            transition_id=str(row["integrity_event_key"]),
            request_run_id=request_run_id,
            item_key=base_item.item_id,
            evidence_item_key=evidence_item.item_id,
            previous_state=previous_state,
            current_state=current_state,
            occurred_at=occurred_at,
        )
        if (
            not hmac.compare_digest(canonical, expected)
            or hashlib.sha256(canonical).hexdigest() != checksum
        ):
            raise IntelligenceStoreError("integrity authentication failed")
        return StoredIntegrityTransition(
            id=int(row["id"]),
            transition_id=str(row["integrity_event_key"]),
            request_run_id=request_run_id,
            item_id=item_id,
            evidence_item_id=evidence_item_id,
            previous_state=previous_state,
            current_state=current_state,
            occurred_at=occurred_at,
            event_checksum=checksum,
        )

    @staticmethod
    def _authenticate_transition_request_evidence(
        connection: sqlite3.Connection,
        request_run_id: int,
        evidence_item_id: int,
        *,
        require_running: bool,
    ) -> None:
        run = connection.execute(
            "SELECT status FROM request_runs WHERE id=?", (request_run_id,)
        ).fetchone()
        if run is None or (require_running and run["status"] != "running"):
            raise IntelligenceStoreError("integrity request binding failed")
        row = connection.execute(
            """
            SELECT 1
            FROM intelligence_news_items AS item
            JOIN source_attempts AS attempt ON attempt.id=item.source_attempt_id
            WHERE item.id=? AND attempt.request_run_id=?
              AND attempt.outcome IN ('success', 'cache_hit')
            UNION ALL
            SELECT 1
            FROM intelligence_snapshot_item_uses AS item_use
            JOIN source_attempts AS attempt ON attempt.id=item_use.source_attempt_id
            WHERE item_use.item_id=? AND item_use.request_run_id=?
              AND attempt.outcome IN ('success', 'cache_hit')
            LIMIT 1
            """,
            (evidence_item_id, request_run_id, evidence_item_id, request_run_id),
        ).fetchone()
        if row is None:
            raise IntelligenceStoreError("integrity evidence request binding failed")

    @staticmethod
    def _item_row_id(connection: sqlite3.Connection, item_key: str) -> int:
        row = connection.execute(
            "SELECT id FROM intelligence_news_items WHERE item_key=?", (item_key,)
        ).fetchone()
        if row is None:
            raise IntelligenceStoreError("intelligence evidence item is missing")
        return int(row["id"])

    def _lineage_from_authenticated_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> LineageEdge:
        canonical = _ascii_bytes(row["canonical_edge_json"], "lineage edge")
        checksum = _checksum(row["edge_checksum"], "lineage checksum")
        if hashlib.sha256(canonical).hexdigest() != checksum:
            raise IntelligenceStoreError("intelligence lineage authentication failed")
        payload = _json_object(canonical, "lineage edge")
        edge = LineageEdge(
            edge_id=payload["edge_id"],
            from_item_id=payload["from_item_id"],
            to_item_id=payload["to_item_id"],
            kind=LineageKind(payload["kind"]),
            evidence_ids=tuple(payload["evidence_ids"]),
        )
        edge.validate()
        endpoint_row = connection.execute(
            """
            SELECT source.item_key AS from_key, target.item_key AS to_key
            FROM intelligence_news_items AS source
            JOIN intelligence_news_items AS target ON target.id=?
            WHERE source.id=?
            """,
            (row["to_item_id"], row["from_item_id"]),
        ).fetchone()
        if endpoint_row is None or (
            canonical_json_bytes(edge.to_canonical_dict()) != canonical
            or edge.edge_id != row["edge_key"]
            or edge.kind.value != row["kind"]
            or edge.from_item_id != endpoint_row["from_key"]
            or edge.to_item_id != endpoint_row["to_key"]
            or tuple(
                _json_array(
                    _ascii_bytes(row["evidence_ids_json"], "lineage evidence ids"),
                    "lineage evidence ids",
                )
            )
            != edge.evidence_ids
        ):
            raise IntelligenceStoreError("intelligence lineage authentication failed")
        return edge

    def _authenticate_event_row(
        self,
        connection: sqlite3.Connection,
        event_key: str,
        visited: Optional[set] = None,
    ) -> NewsEvent:
        if visited is None:
            visited = set()
        if event_key in visited:
            raise IntelligenceStoreError("intelligence event authentication failed")
        active_visited = set(visited)
        active_visited.add(event_key)
        row = connection.execute(
            "SELECT * FROM intelligence_events WHERE event_key=?", (event_key,)
        ).fetchone()
        if row is None:
            raise IntelligenceStoreError("intelligence event authentication failed")
        canonical = _ascii_bytes(row["canonical_event_json"], "intelligence event")
        checksum = _checksum(row["event_checksum"], "intelligence event checksum")
        if hashlib.sha256(canonical).hexdigest() != checksum:
            raise IntelligenceStoreError("intelligence event authentication failed")
        event = _event_from_dict(_json_object(canonical, "intelligence event"))
        if canonical_json_bytes(event.to_canonical_dict()) != canonical or (
            event.event_id != row["event_key"]
            or event.event_type.value != row["event_type"]
            or event.normalized_title != row["normalized_title"]
            or event.confidence_state.value != row["confidence_state"]
            or event.earliest_published_at != _stored_utc(row["earliest_published_at"])
            or event.latest_published_at != _stored_utc(row["latest_published_at"])
            or event.integrity_state.value != row["integrity_state"]
            or event.superseded_by_event_id != row["superseded_by_event_key"]
        ):
            raise IntelligenceStoreError("intelligence event authentication failed")
        role_rows = connection.execute(
            """
            SELECT item.item_key, event_item.role
            FROM intelligence_event_items AS event_item
            JOIN intelligence_news_items AS item ON item.id=event_item.item_id
            WHERE event_item.event_id=? ORDER BY event_item.role, item.item_key
            """,
            (int(row["id"]),),
        ).fetchall()
        stored_roles = {(str(item["role"]), str(item["item_key"])) for item in role_rows}
        expected_roles = {
            *(('supporting', key) for key in event.supporting_item_ids),
            *(('opposing', key) for key in event.opposing_item_ids),
            *(('correction', key) for key in event.correction_item_ids),
            *(('retraction', key) for key in event.retraction_item_ids),
        }
        if stored_roles != expected_roles:
            raise IntelligenceStoreError("intelligence event authentication failed")
        if event.superseded_by_event_id is not None:
            if event.superseded_by_event_id == event.event_id:
                raise IntelligenceStoreError("intelligence event authentication failed")
            self._authenticate_event_row(
                connection,
                event.superseded_by_event_id,
                active_visited,
            )
        return event

    def _snapshot_graph_item_keys(
        self,
        connection: sqlite3.Connection,
        snapshot: IntelligenceSnapshot,
    ) -> set:
        result = set()
        for edge_key in snapshot.lineage_edge_ids:
            row = connection.execute(
                "SELECT * FROM intelligence_lineage_edges WHERE edge_key=?", (edge_key,)
            ).fetchone()
            if row is None:
                raise IntelligenceStoreError("intelligence snapshot graph closure failed")
            edge = self._lineage_from_authenticated_row(connection, row)
            result.update((edge.from_item_id, edge.to_item_id))
        for event_key in snapshot.event_ids:
            pending = [event_key]
            visited = set()
            while pending:
                current_key = pending.pop()
                if current_key in visited:
                    raise IntelligenceStoreError("intelligence snapshot graph closure failed")
                visited.add(current_key)
                event = self._authenticate_event_row(connection, current_key)
                result.update(event.supporting_item_ids)
                result.update(event.opposing_item_ids)
                result.update(event.correction_item_ids)
                result.update(event.retraction_item_ids)
                if event.superseded_by_event_id is not None:
                    pending.append(event.superseded_by_event_id)
        return result

    def _validate_snapshot_bindings(
        self,
        connection: sqlite3.Connection,
        snapshot: IntelligenceSnapshot,
        request_run_id: int,
        budget: RequestBudget,
    ) -> dict:
        run = connection.execute(
            "SELECT * FROM request_runs WHERE id=?", (request_run_id,)
        ).fetchone()
        if run is None or (
            run["request_id"] != budget.request_id
            or run["request_id"] != snapshot.request_id
            or run["mode"] != budget.mode.value
            or run["status"] != "running"
            or snapshot.request_run_id != request_run_id
        ):
            raise IntelligenceStoreError("intelligence snapshot request binding failed")
        attempts = connection.execute(
            "SELECT id, request_run_id, source_id, outcome FROM source_attempts "
            f"WHERE id IN ({','.join('?' for _ in snapshot.source_attempt_ids)})",
            snapshot.source_attempt_ids,
        ).fetchall()
        if len(attempts) != len(snapshot.source_attempt_ids) or any(
            row["request_run_id"] != request_run_id
            or row["outcome"] not in {"success", "cache_hit"}
            for row in attempts
        ):
            raise IntelligenceStoreError("intelligence snapshot source binding failed")
        item_rows = connection.execute(
            "SELECT id, item_key, source_id FROM intelligence_news_items "
            f"WHERE item_key IN ({','.join('?' for _ in snapshot.item_ids)})",
            snapshot.item_ids,
        ).fetchall()
        if len(item_rows) != len(snapshot.item_ids):
            raise IntelligenceStoreError("intelligence snapshot item binding failed")
        attempts_by_source = {}
        for row in attempts:
            attempts_by_source.setdefault(row["source_id"], []).append(int(row["id"]))
        item_uses = {}
        for row in item_rows:
            candidates = attempts_by_source.get(row["source_id"], [])
            if len(candidates) != 1:
                raise IntelligenceStoreError("intelligence snapshot item binding failed")
            item_uses[int(row["id"])] = candidates[0]
        for table, key_column, values, label in (
            (
                "intelligence_lineage_edges",
                "edge_key",
                snapshot.lineage_edge_ids,
                "lineage",
            ),
            ("intelligence_events", "event_key", snapshot.event_ids, "event"),
        ):
            if not values:
                continue
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE {key_column} "
                f"IN ({','.join('?' for _ in values)})",
                values,
            ).fetchone()[0]
            if count != len(values):
                raise IntelligenceStoreError(f"intelligence snapshot {label} binding failed")
        graph_item_keys = self._snapshot_graph_item_keys(connection, snapshot)
        if not graph_item_keys.issubset(snapshot.item_ids):
            raise IntelligenceStoreError("intelligence snapshot graph closure failed")
        return item_uses

    @staticmethod
    def _save_item_uses(
        connection: sqlite3.Connection,
        request_run_id: int,
        item_uses: dict,
    ) -> None:
        for item_id, attempt_id in sorted(item_uses.items()):
            connection.execute(
                """
                INSERT INTO intelligence_snapshot_item_uses(
                    request_run_id, item_id, source_attempt_id
                ) VALUES (?, ?, ?)
                """,
                (request_run_id, item_id, attempt_id),
            )
            row = connection.execute(
                """
                SELECT request_run_id, item_id, source_attempt_id
                FROM intelligence_snapshot_item_uses
                WHERE request_run_id=? AND item_id=?
                """,
                (request_run_id, item_id),
            ).fetchone()
            if row is None or tuple(row) != (request_run_id, item_id, attempt_id):
                raise IntelligenceStoreError("intelligence item use authentication failed")

    def _save_entities(
        self,
        connection: sqlite3.Connection,
        entities: Tuple[MarketEntity, ...],
    ) -> dict:
        result = {}
        for entity in entities:
            canonical = canonical_json_bytes(entity.to_canonical_dict())
            checksum = hashlib.sha256(canonical).hexdigest()
            row = connection.execute(
                "SELECT * FROM market_entities WHERE entity_key=?", (entity.entity_id,)
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO market_entities(
                        entity_key, entity_type, canonical_name, active_from, active_until,
                        evidence_ids_json, canonical_entity_json, entity_checksum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity.entity_id,
                        entity.entity_type,
                        entity.canonical_name,
                        _utc_text(entity.active_from),
                        _optional_utc_text(entity.active_until),
                        _ascii_json(entity.evidence_ids),
                        canonical.decode("ascii"),
                        checksum,
                    ),
                )
                entity_id = int(cursor.lastrowid)
            else:
                entity_id = int(row["id"])
            self._authenticate_payload(
                connection,
                "market_entities",
                "entity_key",
                entity.entity_id,
                "canonical_entity_json",
                "entity_checksum",
                canonical,
            )
            result[entity.entity_id] = entity_id
        return result

    def _save_observations(
        self,
        connection: sqlite3.Connection,
        observations: Tuple[DimensionObservation, ...],
        entity_ids: dict,
        request_run_id: int,
    ) -> dict:
        result = {}
        for observation in observations:
            if observation.entity_id not in entity_ids:
                raise IntelligenceStoreError("market observation entity binding failed")
            attempts = connection.execute(
                "SELECT id FROM source_attempts WHERE request_run_id=? AND outcome IN "
                "('success','cache_hit') AND id IN "
                f"({','.join('?' for _ in observation.source_attempt_ids)})",
                (request_run_id, *observation.source_attempt_ids),
            ).fetchall()
            if len(attempts) != len(observation.source_attempt_ids):
                raise IntelligenceStoreError("market observation source binding failed")
            canonical = canonical_json_bytes(observation.to_canonical_dict())
            checksum = hashlib.sha256(canonical).hexdigest()
            cursor = connection.execute(
                """
                INSERT INTO market_dimension_observations(
                    observation_key, entity_id, source_attempt_ids_json,
                    canonical_observation_json, observation_checksum,
                    data_as_of, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    entity_ids[observation.entity_id],
                    _ascii_json(observation.source_attempt_ids),
                    canonical.decode("ascii"),
                    checksum,
                    _utc_text(observation.data_as_of),
                    _utc_text(observation.retrieved_at),
                ),
            )
            row_id = int(cursor.lastrowid)
            self._authenticate_payload(
                connection,
                "market_dimension_observations",
                "observation_key",
                observation.observation_id,
                "canonical_observation_json",
                "observation_checksum",
                canonical,
            )
            result[observation.observation_id] = row_id
        return result

    def _save_event_entity_links(
        self,
        connection: sqlite3.Connection,
        links: Tuple[EventEntityLink, ...],
        entity_ids: dict,
    ) -> None:
        for link in links:
            if link.entity_id not in entity_ids:
                raise IntelligenceStoreError("event entity binding failed")
            event = connection.execute(
                "SELECT id FROM intelligence_events WHERE event_key=?", (link.event_id,)
            ).fetchone()
            if event is None:
                raise IntelligenceStoreError("event entity binding failed")
            canonical = canonical_json_bytes(link.to_canonical_dict())
            checksum = hashlib.sha256(canonical).hexdigest()
            connection.execute(
                """
                INSERT INTO intelligence_event_entities(
                    link_key, event_id, entity_id, relationship, evidence_ids_json,
                    canonical_link_json, link_checksum
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.link_id,
                    int(event["id"]),
                    entity_ids[link.entity_id],
                    link.relationship.value,
                    _ascii_json(link.evidence_ids),
                    canonical.decode("ascii"),
                    checksum,
                ),
            )
            self._authenticate_payload(
                connection,
                "intelligence_event_entities",
                "link_key",
                link.link_id,
                "canonical_link_json",
                "link_checksum",
                canonical,
            )

    def _save_market_state(
        self,
        connection: sqlite3.Connection,
        request_run_id: int,
        state: MarketStateSnapshot,
        observation_ids: dict,
        created_at: datetime,
    ) -> int:
        canonical = canonical_json_bytes(state.to_canonical_dict())
        checksum = hashlib.sha256(canonical).hexdigest()
        cursor = connection.execute(
            """
            INSERT INTO market_state_snapshots(
                request_run_id, policy_version, observation_ids_json,
                canonical_state_json, state_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                request_run_id,
                self.policy.version,
                _ascii_json(tuple(observation_ids)),
                canonical.decode("ascii"),
                checksum,
                _utc_text(created_at),
            ),
        )
        self._authenticate_payload(
            connection,
            "market_state_snapshots",
            "id",
            int(cursor.lastrowid),
            "canonical_state_json",
            "state_checksum",
            canonical,
        )
        return int(cursor.lastrowid)

    def _save_snapshot(
        self,
        connection: sqlite3.Connection,
        request_run_id: int,
        state_id: int,
        snapshot: IntelligenceSnapshot,
    ) -> int:
        canonical = snapshot.canonical_json()
        _bounded(canonical, "intelligence snapshot")
        checksum = snapshot.checksum()
        cursor = connection.execute(
            """
            INSERT INTO intelligence_snapshots(
                request_run_id, market_state_snapshot_id, policy_version,
                workflow, canonical_snapshot_json, result_checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_run_id,
                state_id,
                self.policy.version,
                snapshot.workflow.value,
                canonical.decode("ascii"),
                checksum,
                _utc_text(snapshot.created_at),
            ),
        )
        stored_id = int(cursor.lastrowid)
        self._authenticate_payload(
            connection,
            "intelligence_snapshots",
            "id",
            stored_id,
            "canonical_snapshot_json",
            "result_checksum",
            canonical,
        )
        return stored_id

    @staticmethod
    def _authenticate_payload(
        connection: sqlite3.Connection,
        table: str,
        key_column: str,
        key: object,
        payload_column: str,
        checksum_column: str,
        expected: bytes,
    ) -> None:
        row = connection.execute(
            f"SELECT {payload_column}, {checksum_column} FROM {table} WHERE {key_column}=?",
            (key,),
        ).fetchone()
        if row is None:
            raise IntelligenceStoreError("intelligence payload authentication failed")
        stored = _ascii_bytes(row[payload_column], "intelligence payload")
        checksum = _checksum(row[checksum_column], "intelligence checksum")
        if (
            not hmac.compare_digest(stored, expected)
            or hashlib.sha256(stored).hexdigest() != checksum
        ):
            raise IntelligenceStoreError("intelligence payload authentication failed")


def _connection(value: object) -> sqlite3.Connection:
    if type(value) is not sqlite3.Connection:
        raise ValueError("connection must be an exact sqlite3.Connection")
    return value


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _utc_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be an exact UTC datetime")
    return value.isoformat()


def _optional_utc_text(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else _utc_text(value)


def _stored_utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("stored timestamp must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed.isoformat() != value:
        raise ValueError("stored timestamp is not canonical UTC")
    return parsed


def _bounded(value: bytes, name: str, *, maximum: int = MAX_RECORD_BYTES) -> bytes:
    if type(value) is not bytes or not value or len(value) > maximum:
        raise ValueError(f"{name} bytes are invalid")
    return value


def _ascii_bytes(value: object, name: str) -> bytes:
    if type(value) is not str:
        raise ValueError(f"stored {name} must be text")
    encoded = value.encode("ascii")
    return _bounded(encoded, name)


def _checksum(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _ascii_json(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")


def _allowed_integrity_transition(
    previous_state: IntegrityState,
    current_state: IntegrityState,
) -> bool:
    allowed = {
        IntegrityState.ACTIVE: {
            IntegrityState.CORRECTED,
            IntegrityState.RETRACTED,
            IntegrityState.SUPERSEDED,
        },
        IntegrityState.CORRECTED: {
            IntegrityState.RETRACTED,
            IntegrityState.SUPERSEDED,
        },
        IntegrityState.UNKNOWN: {
            IntegrityState.ACTIVE,
            IntegrityState.CORRECTED,
            IntegrityState.RETRACTED,
            IntegrityState.SUPERSEDED,
        },
    }
    return current_state in allowed.get(previous_state, set())


def _integrity_transition_bytes(
    *,
    transition_id: str,
    request_run_id: int,
    item_key: str,
    evidence_item_key: str,
    previous_state: IntegrityState,
    current_state: IntegrityState,
    occurred_at: datetime,
) -> bytes:
    return canonical_json_bytes(
        {
            "current_state": current_state.value,
            "evidence_item_id": evidence_item_key,
            "item_id": item_key,
            "occurred_at": _utc_text(occurred_at),
            "previous_state": previous_state.value,
            "request_run_id": request_run_id,
            "transition_id": transition_id,
        }
    )


def _json_object(value: bytes, name: str) -> dict:
    try:
        parsed = json.loads(value.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"stored {name} is invalid") from None
    if type(parsed) is not dict:
        raise ValueError(f"stored {name} must be an object")
    return parsed


def _json_array(value: bytes, name: str) -> list:
    try:
        parsed = json.loads(value.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError(f"stored {name} is invalid") from None
    if type(parsed) is not list:
        raise ValueError(f"stored {name} must be an array")
    return parsed


def _entity_from_dict(value: dict) -> MarketEntity:
    return MarketEntity(
        entity_id=value["entity_id"],
        entity_type=value["entity_type"],
        canonical_name=value["canonical_name"],
        active_from=_stored_utc(value["active_from"]),
        active_until=(
            None if value["active_until"] is None else _stored_utc(value["active_until"])
        ),
        evidence_ids=tuple(value["evidence_ids"]),
    )


def _observation_from_dict(value: dict) -> DimensionObservation:
    return DimensionObservation(
        observation_id=value["observation_id"],
        entity_id=value["entity_id"],
        dimension=MarketDimension(value["dimension"]),
        metric_id=None if value["metric_id"] is None else MetricId(value["metric_id"]),
        state=DimensionState(value["state"]),
        value=None if value["value"] is None else Decimal(value["value"]),
        unit=value["unit"],
        data_as_of=_stored_utc(value["data_as_of"]),
        retrieved_at=_stored_utc(value["retrieved_at"]),
        source_tier=SourceTier(value["source_tier"]),
        source_attempt_ids=tuple(value["source_attempt_ids"]),
        evidence_ids=tuple(value["evidence_ids"]),
        freshness=EvidenceFreshness(value["freshness"]),
        completeness=EvidenceCompleteness(value["completeness"]),
        conflict_ids=tuple(value["conflict_ids"]),
    )


def _state_from_dict(value: dict) -> MarketStateSnapshot:
    return MarketStateSnapshot(
        market_state=MarketShadowState(value["market_state"]),
        sector_states=tuple(
            (item["sector_id"], SectorShadowState(item["state"])) for item in value["sector_states"]
        ),
        dimensions=tuple(_observation_from_dict(item) for item in value["dimensions"]),
        supporting_observation_ids=tuple(value["supporting_observation_ids"]),
        opposing_observation_ids=tuple(value["opposing_observation_ids"]),
        unknown_dimensions=tuple(MarketDimension(item) for item in value["unknown_dimensions"]),
        invalidation_conditions=tuple(value["invalidation_conditions"]),
        next_review_at=_stored_utc(value["next_review_at"]),
        policy_checksum=value["policy_checksum"],
    )


def _event_link_from_dict(value: dict) -> EventEntityLink:
    return EventEntityLink(
        link_id=value["link_id"],
        event_id=value["event_id"],
        entity_id=value["entity_id"],
        relationship=EventEntityRelationship(value["relationship"]),
        evidence_ids=tuple(value["evidence_ids"]),
    )


def _event_from_dict(value: dict) -> NewsEvent:
    return NewsEvent(
        event_id=value["event_id"],
        event_type=EventType(value["event_type"]),
        normalized_title=value["normalized_title"],
        supporting_item_ids=tuple(value["supporting_item_ids"]),
        opposing_item_ids=tuple(value["opposing_item_ids"]),
        correction_item_ids=tuple(value["correction_item_ids"]),
        retraction_item_ids=tuple(value["retraction_item_ids"]),
        confidence_state=EventConfidenceState(value["confidence_state"]),
        earliest_published_at=_stored_utc(value["earliest_published_at"]),
        latest_published_at=_stored_utc(value["latest_published_at"]),
        integrity_state=IntegrityState(value["integrity_state"]),
        superseded_by_event_id=value["superseded_by_event_id"],
        invalidation_conditions=tuple(value["invalidation_conditions"]),
    )


def _snapshot_from_dict(value: dict) -> IntelligenceSnapshot:
    interval = value["interval"]
    return IntelligenceSnapshot(
        workflow=IntelligenceWorkflow(value["workflow"]),
        request_id=value["request_id"],
        request_run_id=value["request_run_id"],
        interval=QueryInterval(
            start_at=_stored_utc(interval["start_at"]),
            end_at=_stored_utc(interval["end_at"]),
            timezone_name=interval["timezone_name"],
        ),
        subject_fund_code=value["subject_fund_code"],
        entities=tuple(_entity_from_dict(item) for item in value["entities"]),
        item_ids=tuple(value["item_ids"]),
        source_attempt_ids=tuple(value["source_attempt_ids"]),
        lineage_edge_ids=tuple(value["lineage_edge_ids"]),
        event_ids=tuple(value["event_ids"]),
        event_entity_links=tuple(
            _event_link_from_dict(item) for item in value["event_entity_links"]
        ),
        market_state=_state_from_dict(value["market_state"]),
        fund_relevance_link_ids=tuple(value["fund_relevance_link_ids"]),
        conflicts=tuple(value["conflicts"]),
        missing_evidence=tuple(value["missing_evidence"]),
        created_at=_stored_utc(value["created_at"]),
        exact_amount_available=value["exact_amount_available"],
    )
