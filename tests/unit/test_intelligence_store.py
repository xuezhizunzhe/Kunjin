from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    RequestMode,
    RequestTerminalStatus,
    SourceTier,
    canonical_json_bytes,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.intelligence.models import (
    EventConfidenceState,
    IntegrityState,
    IntelligenceSnapshot,
    IntelligenceWorkflow,
    LineageEdge,
    LineageKind,
    MarketDimension,
    MarketShadowState,
    MarketStateSnapshot,
    NewsEvent,
    NewsItem,
    QueryInterval,
)
from kunjin.intelligence.policy import IntelligencePolicyV1
from kunjin.intelligence.store import IntelligenceStore, IntelligenceStoreError
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def _budget(request_id: str = "1" * 32) -> RequestBudget:
    return RequestBudget.create(
        RequestMode.RAPID,
        request_id=request_id,
        monotonic=lambda: 10.0,
        wall_clock=lambda: NOW,
    )


def _source_attempt(
    connection: sqlite3.Connection,
    request_run_id: int,
    source_id: str,
    *,
    outcome: str = "success",
) -> int:
    return int(
        connection.execute(
            """
            INSERT INTO source_attempts(
                request_run_id, source_id, field_id, subject_key, attempt_number,
                outcome, started_at, finished_at, data_as_of, error_code,
                cooldown_until, force_actor, force_reason, registry_version,
                registry_checksum, response_byte_count
            ) VALUES (?, ?, 'news_recent', 'fund:123456', 1, ?, ?, ?, ?,
                      NULL, NULL, NULL, NULL, '1', ?, 128)
            """,
            (
                request_run_id,
                source_id,
                outcome,
                NOW.isoformat(),
                (NOW + timedelta(seconds=1)).isoformat(),
                NOW.isoformat(),
                CHECKSUM,
            ),
        ).lastrowid
    )


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    value = Repository(tmp_path / "intelligence.db")
    value.migrate()
    return value


@pytest.fixture
def store(repository: Repository) -> IntelligenceStore:
    return IntelligenceStore(repository)


def _item(item_id: str, source_attempt_id: int, *, offset: int = 0) -> NewsItem:
    retrieved = NOW + timedelta(seconds=2 + offset)
    excerpt = f"Authenticated excerpt {item_id}"
    return NewsItem(
        item_id=item_id,
        source_id="gov_cn_policy",
        publisher="State Council",
        canonical_url=f"https://www.gov.cn/policy/{item_id}",
        title=f"Policy item {item_id}",
        excerpt=excerpt,
        excerpt_truncated=False,
        excerpt_original_bytes=len(excerpt.encode("utf-8")),
        excerpt_expires_at=retrieved + timedelta(days=365),
        excerpt_expired_at=None,
        published_at=NOW - timedelta(hours=1),
        publication_precision="minute",
        publication_interval_end=None,
        retrieved_at=retrieved,
        source_tier=SourceTier.TIER_1,
        content_fingerprint=("b" if item_id == "item_one" else "c") * 64,
        category="policy",
        integrity_state=IntegrityState.ACTIVE,
        source_attempt_id=source_attempt_id,
    )


def _event() -> NewsEvent:
    return NewsEvent(
        event_id="event_one",
        event_type=__import__(
            "kunjin.intelligence.models", fromlist=["EventType"]
        ).EventType.POLICY,
        normalized_title="Policy event",
        supporting_item_ids=("item_one",),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=NOW - timedelta(hours=1),
        latest_published_at=NOW - timedelta(hours=1),
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("Review on correction.",),
    )


def _superseded_event(event_id: str, target: str) -> NewsEvent:
    return NewsEvent(
        event_id=event_id,
        event_type=__import__(
            "kunjin.intelligence.models", fromlist=["EventType"]
        ).EventType.POLICY,
        normalized_title="Superseded policy event",
        supporting_item_ids=("item_two",),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.INSUFFICIENT,
        earliest_published_at=NOW - timedelta(hours=1),
        latest_published_at=NOW - timedelta(hours=1),
        integrity_state=IntegrityState.SUPERSEDED,
        superseded_by_event_id=target,
        invalidation_conditions=("Use the replacement event.",),
    )


def _snapshot(request_run_id: int, request_id: str) -> IntelligenceSnapshot:
    policy = IntelligencePolicyV1()
    return IntelligenceSnapshot(
        workflow=IntelligenceWorkflow.NEWS_RECENT,
        request_id=request_id,
        request_run_id=request_run_id,
        interval=QueryInterval(
            start_at=NOW - timedelta(hours=72),
            end_at=NOW,
            timezone_name="Asia/Shanghai",
        ),
        subject_fund_code=None,
        entities=(),
        item_ids=("item_one", "item_two"),
        source_attempt_ids=(1,),
        lineage_edge_ids=("edge_one",),
        event_ids=("event_one",),
        event_entity_links=(),
        market_state=MarketStateSnapshot(
            market_state=MarketShadowState.INSUFFICIENT_DATA,
            sector_states=(),
            dimensions=(),
            supporting_observation_ids=(),
            opposing_observation_ids=(),
            unknown_dimensions=tuple(MarketDimension),
            invalidation_conditions=("Refresh evidence.",),
            next_review_at=NOW + timedelta(hours=2),
            policy_checksum=policy.checksum(),
        ),
        fund_relevance_link_ids=(),
        conflicts=(),
        missing_evidence=("market_dimensions",),
        created_at=NOW + timedelta(seconds=3),
        exact_amount_available=False,
    )


def _seed_request_and_evidence(repository: Repository, store: IntelligenceStore):
    budget = _budget()
    request_run_id = DecisionAuditStore(repository).begin_request(budget)
    with repository.connect() as connection, connection:
        attempt_id = _source_attempt(connection, request_run_id, "gov_cn_policy")
        items = (_item("item_one", attempt_id), _item("item_two", attempt_id, offset=1))
        item_ids = store.save_items(items, connection)
        edge = LineageEdge(
            edge_id="edge_one",
            from_item_id="item_one",
            to_item_id="item_two",
            kind=LineageKind.DIRECT_QUOTE,
            evidence_ids=("item_one", "item_two"),
        )
        store.save_lineage_and_events((edge,), (_event(),), connection)
    return budget, request_run_id, attempt_id, item_ids, items


def test_items_require_exact_authenticated_source_attempt_binding(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget = _budget()
    run_id = DecisionAuditStore(repository).begin_request(budget)
    with repository.connect() as connection, connection:
        wrong_attempt = _source_attempt(connection, run_id, "stcn_fund_news")
        with pytest.raises(IntelligenceStoreError, match="source attempt binding"):
            store.save_items((_item("item_one", wrong_attempt),), connection)
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM intelligence_news_items").fetchone()[0] == 0


def test_save_is_append_only_and_reloads_exact_bytes(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    _budget_value, _run_id, _attempt_id, item_ids, items = _seed_request_and_evidence(
        repository, store
    )
    assert tuple(store.authenticated_item(item_id) for item_id in item_ids) == items
    assert store.lineage_for_item(item_ids[0])[0].edge_id == "edge_one"
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE intelligence_news_items SET title='tampered' WHERE id=?", (item_ids[0],)
        )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE intelligence_events SET normalized_title='tampered' WHERE event_key='event_one'"
        )


def test_expiring_excerpt_does_not_erase_audit_lineage(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, item_ids, items = _seed_request_and_evidence(
        repository, store
    )
    item_id = item_ids[0]
    before = store.authenticated_item(item_id)
    before_lineage = store.lineage_for_item(item_id)
    stored_snapshot = store.publish_snapshot(
        run_id,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget.request_id).__dict__,
                "source_attempt_ids": (attempt_id,),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    with repository.connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM intelligence_news_excerpts WHERE item_id=?", (item_id,))
    assert store.expire_excerpts(now=items[0].retrieved_at + timedelta(days=366)) == 2
    after = store.authenticated_item(item_id)
    assert after.excerpt is None
    assert after.content_fingerprint == before.content_fingerprint
    assert after.canonical_url == before.canonical_url
    assert store.lineage_for_item(item_id) == before_lineage
    assert store.authenticated_snapshot(stored_snapshot.id).result_checksum == (
        stored_snapshot.result_checksum
    )


def test_snapshot_publication_is_atomic_and_authenticated(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, item_ids, _items = _seed_request_and_evidence(repository, store)

    def broken_factory(_request_run_id: int) -> IntelligenceSnapshot:
        raise RuntimeError("private detail")

    with pytest.raises(IntelligenceStoreError, match="snapshot factory failed"):
        store.publish_snapshot(
            run_id,
            broken_factory,
            NOW + timedelta(seconds=4),
            RequestTerminalStatus.COMPLETE,
            (),
            budget,
        )
    with repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM intelligence_snapshots").fetchone()[0] == 0
        status = connection.execute(
            "SELECT status FROM request_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        assert status == "running"
        assert connection.execute("SELECT count(*) FROM intelligence_news_items").fetchone()[0] == 2

    def snapshot_factory(request_run_id: int) -> IntelligenceSnapshot:
        snapshot = _snapshot(request_run_id, budget.request_id)
        return IntelligenceSnapshot(
            **{
                **snapshot.__dict__,
                "source_attempt_ids": (attempt_id,),
            }
        )

    stored = store.publish_snapshot(
        run_id,
        snapshot_factory,
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    assert stored.snapshot == snapshot_factory(run_id)
    assert stored.result_checksum == stored.snapshot.checksum()
    assert store.authenticated_snapshot(stored.id) == stored
    with repository.connect() as connection:
        status = connection.execute(
            "SELECT status FROM request_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        assert status == "complete"
        assert tuple(
            row[0]
            for row in connection.execute(
                "SELECT item_key FROM intelligence_news_items ORDER BY id"
            )
        ) == ("item_one", "item_two")


def test_later_request_reuses_immutable_items_with_current_attempt_bindings(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget_one, run_one, attempt_one, item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    store.publish_snapshot(
        run_one,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget_one.request_id).__dict__,
                "source_attempt_ids": (attempt_one,),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget_one,
    )

    budget_two = _budget("2" * 32)
    run_two = DecisionAuditStore(repository).begin_request(budget_two)
    with repository.connect() as connection, connection:
        attempt_two = _source_attempt(
            connection,
            run_two,
            "gov_cn_policy",
            outcome="cache_hit",
        )
    second = store.publish_snapshot(
        run_two,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget_two.request_id).__dict__,
                "source_attempt_ids": (attempt_two,),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget_two,
    )

    assert store.authenticated_snapshot(second.id) == second
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM intelligence_news_items"
        ).fetchone()[0] == 2
        assert tuple(
            row[0]
            for row in connection.execute(
                "SELECT source_attempt_id FROM intelligence_news_items ORDER BY id"
            )
        ) == (attempt_one, attempt_one)
        assert tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT request_run_id, item_id, source_attempt_id
                FROM intelligence_snapshot_item_uses
                ORDER BY request_run_id, item_id
                """
            )
        ) == (
            (run_one, item_ids[0], attempt_one),
            (run_one, item_ids[1], attempt_one),
            (run_two, item_ids[0], attempt_two),
            (run_two, item_ids[1], attempt_two),
        )


def test_integrity_transitions_are_append_only_authenticated_and_derive_current_state(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    _budget_value, run_id, attempt_id, item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    with repository.connect() as connection, connection:
        correction_id = store.save_items(
            (_item("correction_item", attempt_id, offset=2),), connection
        )[0]
        corrected = store.record_integrity_transition(
            transition_id="item_one_corrected",
            request_run_id=run_id,
            item_id=item_ids[0],
            evidence_item_id=correction_id,
            new_state=IntegrityState.CORRECTED,
            occurred_at=NOW + timedelta(seconds=5),
            connection=connection,
        )
        retraction_id = store.save_items(
            (_item("retraction_item", attempt_id, offset=4),), connection
        )[0]
        retracted = store.record_integrity_transition(
            transition_id="item_one_retracted",
            request_run_id=run_id,
            item_id=item_ids[0],
            evidence_item_id=retraction_id,
            new_state=IntegrityState.RETRACTED,
            occurred_at=NOW + timedelta(seconds=7),
            connection=connection,
        )

    assert corrected.previous_state is IntegrityState.ACTIVE
    assert retracted.previous_state is IntegrityState.CORRECTED
    assert store.authenticated_item(item_ids[0]).integrity_state is IntegrityState.RETRACTED
    assert store.integrity_history(item_ids[0]) == (corrected, retracted)
    with repository.connect() as connection, connection:
        connection.execute("DROP TRIGGER intelligence_integrity_event_no_update")
        connection.execute(
            "UPDATE intelligence_item_integrity_events SET event_checksum=? WHERE id=?",
            ("f" * 64, corrected.id),
        )
    with pytest.raises(IntelligenceStoreError, match="integrity authentication"):
        store.integrity_history(item_ids[0])


def test_superseded_events_require_authenticated_nonself_target(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget = _budget()
    run_id = DecisionAuditStore(repository).begin_request(budget)
    with repository.connect() as connection, connection:
        attempt_id = _source_attempt(connection, run_id, "gov_cn_policy")
        store.save_items(
            (_item("item_one", attempt_id), _item("item_two", attempt_id, offset=1)),
            connection,
        )
        replacement = NewsEvent(
            **{**_event().__dict__, "event_id": "replacement_event"}
        )
        store.save_lineage_and_events(
            (),
            (_superseded_event("old_event", "replacement_event"), replacement),
            connection,
        )
        with pytest.raises(IntelligenceStoreError):
            store.save_lineage_and_events(
                (),
                (_superseded_event("missing_target_event", "does_not_exist"),),
                connection,
            )
        with pytest.raises(IntelligenceStoreError):
            store.save_lineage_and_events(
                (),
                (_superseded_event("self_event", "self_event"),),
                connection,
            )


@pytest.mark.parametrize("tamper", ("event", "market_state", "item_use"))
def test_authenticated_snapshot_rejects_relational_tampering(
    repository: Repository,
    store: IntelligenceStore,
    tamper: str,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    stored = store.publish_snapshot(
        run_id,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget.request_id).__dict__,
                "source_attempt_ids": (attempt_id,),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    with sqlite3.connect(str(repository.database)) as connection:
        if tamper == "event":
            connection.execute("DROP TRIGGER intelligence_event_no_delete")
            connection.execute("DELETE FROM intelligence_events WHERE event_key='event_one'")
        elif tamper == "market_state":
            connection.execute("DROP TRIGGER market_state_snapshot_no_update")
            connection.execute(
                "UPDATE market_state_snapshots SET state_checksum=? WHERE request_run_id=?",
                ("f" * 64, run_id),
            )
        else:
            connection.execute("DROP TRIGGER intelligence_snapshot_item_use_no_delete")
            connection.execute(
                "DELETE FROM intelligence_snapshot_item_uses WHERE request_run_id=?",
                (run_id,),
            )
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)


@pytest.mark.parametrize("reference_kind", ("event_role", "lineage_endpoint"))
def test_snapshot_rejects_evidence_graph_items_omitted_from_item_ids(
    repository: Repository,
    store: IntelligenceStore,
    reference_kind: str,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    base = _snapshot(run_id, budget.request_id)
    snapshot = IntelligenceSnapshot(
        **{
            **base.__dict__,
            "item_ids": ("item_two",),
            "source_attempt_ids": (attempt_id,),
            "lineage_edge_ids": (
                () if reference_kind == "event_role" else ("edge_one",)
            ),
            "event_ids": (() if reference_kind == "lineage_endpoint" else ("event_one",)),
        }
    )
    with pytest.raises(IntelligenceStoreError, match="graph closure"):
        store.publish_snapshot(
            run_id,
            lambda _value: snapshot,
            NOW + timedelta(seconds=4),
            RequestTerminalStatus.COMPLETE,
            (),
            budget,
        )


def test_authenticated_snapshot_rejects_lineage_numeric_fk_tamper(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    stored = store.publish_snapshot(
        run_id,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget.request_id).__dict__,
                "source_attempt_ids": (attempt_id,),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    with sqlite3.connect(str(repository.database)) as connection:
        connection.execute("DROP TRIGGER intelligence_lineage_no_update")
        connection.execute(
            """
            UPDATE intelligence_lineage_edges
            SET from_item_id=(
                    SELECT id FROM intelligence_news_items WHERE item_key='item_two'
                ),
                to_item_id=(
                    SELECT id FROM intelligence_news_items WHERE item_key='item_one'
                )
            WHERE edge_key='edge_one'
            """
        )
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)


@pytest.mark.parametrize("tamper", ("delete", "checksum"))
def test_authenticated_snapshot_recursively_authenticates_superseded_target(
    repository: Repository,
    store: IntelligenceStore,
    tamper: str,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    with repository.connect() as connection, connection:
        replacement = NewsEvent(
            **{**_event().__dict__, "event_id": "replacement_event"}
        )
        store.save_lineage_and_events(
            (),
            (_superseded_event("old_event", "replacement_event"), replacement),
            connection,
        )
    base = _snapshot(run_id, budget.request_id)
    stored = store.publish_snapshot(
        run_id,
        lambda _value: IntelligenceSnapshot(
            **{
                **base.__dict__,
                "source_attempt_ids": (attempt_id,),
                "event_ids": ("old_event",),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    with sqlite3.connect(str(repository.database)) as connection:
        if tamper == "delete":
            connection.execute("DROP TRIGGER intelligence_event_no_delete")
            connection.execute(
                "DELETE FROM intelligence_events WHERE event_key='replacement_event'"
            )
        else:
            connection.execute("DROP TRIGGER intelligence_event_no_update")
            connection.execute(
                "UPDATE intelligence_events SET event_checksum=? "
                "WHERE event_key='replacement_event'",
                ("f" * 64,),
            )
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)


def _replace_snapshot_item_set(
    repository: Repository,
    stored_id: int,
    snapshot: IntelligenceSnapshot,
    retained_item_key: str,
) -> None:
    reduced = IntelligenceSnapshot(
        **{**snapshot.__dict__, "item_ids": (retained_item_key,)}
    )
    canonical = reduced.canonical_json()
    checksum = hashlib.sha256(canonical).hexdigest()
    with sqlite3.connect(str(repository.database)) as connection:
        connection.execute("DROP TRIGGER intelligence_snapshot_no_update")
        connection.execute("DROP TRIGGER intelligence_snapshot_item_use_no_delete")
        connection.execute(
            """
            UPDATE intelligence_snapshots
            SET canonical_snapshot_json=?, result_checksum=?
            WHERE id=?
            """,
            (canonical.decode("ascii"), checksum, stored_id),
        )
        connection.execute(
            """
            DELETE FROM intelligence_snapshot_item_uses
            WHERE item_id=(
                SELECT id FROM intelligence_news_items WHERE item_key != ?
            )
            """,
            (retained_item_key,),
        )


def test_reload_rejects_event_role_item_missing_from_snapshot_and_item_use(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    stored = store.publish_snapshot(
        run_id,
        lambda value: IntelligenceSnapshot(
            **{
                **_snapshot(value, budget.request_id).__dict__,
                "source_attempt_ids": (attempt_id,),
                "lineage_edge_ids": (),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    _replace_snapshot_item_set(repository, stored.id, stored.snapshot, "item_two")
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)


def test_reload_rejects_supersession_target_only_item_missing_from_snapshot_and_use(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    with repository.connect() as connection, connection:
        replacement = NewsEvent(
            **{**_event().__dict__, "event_id": "replacement_event"}
        )
        store.save_lineage_and_events(
            (),
            (_superseded_event("old_event", "replacement_event"), replacement),
            connection,
        )
    base = _snapshot(run_id, budget.request_id)
    stored = store.publish_snapshot(
        run_id,
        lambda _value: IntelligenceSnapshot(
            **{
                **base.__dict__,
                "source_attempt_ids": (attempt_id,),
                "lineage_edge_ids": (),
                "event_ids": ("old_event",),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    _replace_snapshot_item_set(repository, stored.id, stored.snapshot, "item_two")
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)


def test_authenticated_snapshot_rejects_supersession_cycle_after_fk_off_tamper(
    repository: Repository,
    store: IntelligenceStore,
) -> None:
    budget, run_id, attempt_id, _item_ids, _items = _seed_request_and_evidence(
        repository, store
    )
    with repository.connect() as connection, connection:
        replacement = NewsEvent(
            **{**_event().__dict__, "event_id": "replacement_event"}
        )
        store.save_lineage_and_events(
            (),
            (_superseded_event("old_event", "replacement_event"), replacement),
            connection,
        )
    base = _snapshot(run_id, budget.request_id)
    stored = store.publish_snapshot(
        run_id,
        lambda _value: IntelligenceSnapshot(
            **{
                **base.__dict__,
                "source_attempt_ids": (attempt_id,),
                "event_ids": ("old_event",),
            }
        ),
        NOW + timedelta(seconds=4),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    cycle_target = NewsEvent(
        **{
            **_superseded_event("replacement_event", "old_event").__dict__,
            "supporting_item_ids": ("item_one",),
        }
    )
    canonical = canonical_json_bytes(cycle_target.to_canonical_dict())
    with sqlite3.connect(str(repository.database)) as connection:
        connection.execute("DROP TRIGGER intelligence_event_no_update")
        connection.execute(
            """
            UPDATE intelligence_events
            SET normalized_title=?, confidence_state=?, integrity_state=?,
                superseded_by_event_key=?, invalidation_conditions_json=?,
                canonical_event_json=?, event_checksum=?
            WHERE event_key='replacement_event'
            """,
            (
                cycle_target.normalized_title,
                cycle_target.confidence_state.value,
                cycle_target.integrity_state.value,
                cycle_target.superseded_by_event_id,
                canonical_json_bytes(cycle_target.invalidation_conditions).decode("ascii"),
                canonical.decode("ascii"),
                hashlib.sha256(canonical).hexdigest(),
            ),
        )
    with pytest.raises(IntelligenceStoreError, match="snapshot authentication"):
        store.authenticated_snapshot(stored.id)
