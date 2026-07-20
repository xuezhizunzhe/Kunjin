from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import kunjin.holding_review.store as holding_store_module
from kunjin.brief.models import thesis_record_fingerprint
from kunjin.brief.store import BriefStore
from kunjin.decision.models import ActionKind, RequestTerminalStatus, canonical_json_bytes
from kunjin.decision.store import DecisionAuditStore
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    AdjudicationDecision,
    BindingState,
    EvidenceReadiness,
    FlowStatus,
    HoldingReviewSnapshot,
    RedemptionFeasibility,
    ReviewDisposition,
    ReviewEvidenceItem,
    ThesisEvidenceAdjudication,
    ThesisMatchProjection,
    ThesisMatchProjectionState,
    ThesisMatchState,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1
from kunjin.holding_review.store import HoldingReviewStore, HoldingReviewStoreError
from kunjin.intelligence.models import (
    EventConfidenceState,
    EventType,
    IntegrityState,
    IntelligenceWorkflow,
    LineageEdge,
    LineageKind,
    NewsEvent,
)
from kunjin.intelligence.store import IntelligenceStore
from kunjin.models import InvestmentThesis
from kunjin.storage.repository import Repository
from tests.unit.test_brief_store import _publish as publish_brief
from tests.unit.test_holding_review_models_policy import review_result
from tests.unit.test_intelligence_store import (
    _seed_request_and_evidence,
)
from tests.unit.test_intelligence_store import (
    _snapshot as intelligence_snapshot,
)

NOW = datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path: Path):
    repository = Repository(tmp_path / "held-review.db")
    repository.migrate()
    decision_store = DecisionAuditStore(repository)
    brief_store = BriefStore(repository, decision_store)
    brief_run_id, _, brief = publish_brief(
        decision_store, brief_store, request_id="b" * 32
    )
    intelligence_store = IntelligenceStore(repository, decision_store)
    budget, intelligence_run_id, attempt_id, _item_ids, _items = (
        _seed_request_and_evidence(repository, intelligence_store)
    )
    conflict_event = NewsEvent(
        event_id="event_conflict",
        event_type=EventType.POLICY,
        normalized_title="Conflicting policy event",
        supporting_item_ids=("item_one",),
        opposing_item_ids=("item_two",),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.CONFLICTED,
        earliest_published_at=datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc),
        latest_published_at=datetime(2026, 7, 18, 5, 0, tzinfo=timezone.utc),
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("Resolve the conflict.",),
    )
    with repository.connect() as connection, connection:
        intelligence_store.save_lineage_and_events((), (conflict_event,), connection)
    intelligence = intelligence_store.publish_snapshot(
        intelligence_run_id,
        lambda value: replace(
            intelligence_snapshot(value, budget.request_id),
            workflow=IntelligenceWorkflow.FUND_INTELLIGENCE,
            subject_fund_code="123456",
            source_attempt_ids=(attempt_id,),
            event_ids=(conflict_event.event_id,),
        ),
        datetime(2026, 7, 18, 6, 0, 4, tzinfo=timezone.utc),
        RequestTerminalStatus.COMPLETE,
        (),
        budget,
    )
    thesis = InvestmentThesis(
        fund_code="123456",
        rationale="Long-term learning thesis.",
        horizon="Three years.",
        invalidation="Policy support is withdrawn.",
        created_at=NOW - timedelta(days=30),
    )
    thesis_id = repository.add_thesis(thesis)
    return {
        "repository": repository,
        "store": HoldingReviewStore(repository),
        "brief_run_id": brief_run_id,
        "brief": brief,
        "intelligence_run_id": intelligence_run_id,
        "intelligence": intelligence,
        "thesis_id": thesis_id,
        "thesis_fingerprint": thesis_record_fingerprint(thesis_id, thesis),
    }


def desired_projection(context, **changes: object) -> ThesisMatchProjection:
    evidence = ReviewEvidenceItem(
        evidence_id="item_one",
        source_tier=1,
        lineage_kind=LineageKind.DIRECT_QUOTE,
        current=True,
        graph_closed=True,
        original_lineage=False,
        retracted=False,
        conflicted=True,
        direct_subject_binding=False,
    )
    intelligence = context["intelligence"]
    value = ThesisMatchProjection(
        fund_code="123456",
        thesis_id=context["thesis_id"],
        thesis_fingerprint=context["thesis_fingerprint"],
        intelligence_request_run_id=context["intelligence_run_id"],
        intelligence_snapshot_id=intelligence.id,
        intelligence_snapshot_checksum=intelligence.result_checksum,
        matcher_policy_version="1",
        matcher_policy_checksum="d" * 64,
        projection_state=ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH,
        evidence_descriptors=(evidence,),
        evidence_set_checksum="a" * 64,
        created_at=NOW,
        record_checksum="a" * 64,
    )
    value = replace(value, **changes)
    if "evidence_set_checksum" not in changes:
        value = replace(value, evidence_set_checksum=value.expected_evidence_set_checksum())
    if "record_checksum" not in changes:
        value = replace(value, record_checksum=value.expected_record_checksum())
    return value


def desired_adjudication(context, projection, **changes: object):
    evidence_ids = ("item_one",)
    value = ThesisEvidenceAdjudication(
        fund_code="123456",
        thesis_id=context["thesis_id"],
        thesis_fingerprint=context["thesis_fingerprint"],
        thesis_match_projection_id=projection.id,
        thesis_match_projection_checksum=projection.value.record_checksum,
        intelligence_request_run_id=context["intelligence_run_id"],
        intelligence_snapshot_checksum=context["intelligence"].result_checksum,
        evidence_ids=evidence_ids,
        evidence_set_checksum=hashlib.sha256(canonical_json_bytes(evidence_ids)).hexdigest(),
        decision=AdjudicationDecision.PRESENTED_MATCH_REJECTED,
        superseded_adjudication_id=None,
        created_at=NOW + timedelta(seconds=1),
        record_checksum="a" * 64,
    )
    value = replace(value, **changes)
    if "record_checksum" not in changes:
        value = replace(value, record_checksum=value.expected_record_checksum())
    return value


def desired_review(context, projection, adjudication, **changes: object):
    policy = HeldFundManualReviewPolicyV1()
    action = changes.get("action", ActionKind.CONTINUE_HOLDING)
    result = review_result(
        action=action,
        flow_status=FlowStatus.COMPLETE,
        evidence_readiness=EvidenceReadiness.INSUFFICIENT_DATA,
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_REJECTED,
        review_disposition=ReviewDisposition.ABSTAIN,
        redemption_feasibility=(
            RedemptionFeasibility.NOT_REQUESTED
            if action is ActionKind.CONTINUE_HOLDING
            else RedemptionFeasibility.INSUFFICIENT_DATA
        ),
        official_negative_check_complete=False,
        evidence_ids=("item_one",),
        policy_version=policy.version,
        policy_checksum=policy.checksum(),
        created_at=NOW + timedelta(seconds=2),
    )
    value = HoldingReviewSnapshot(
        fund_code="123456",
        action=action,
        brief_request_run_id=context["brief_run_id"],
        brief_snapshot_id=context["brief"].id,
        brief_snapshot_checksum=context["brief"].result_checksum,
        intelligence_request_run_id=context["intelligence_run_id"],
        intelligence_snapshot_id=context["intelligence"].id,
        intelligence_snapshot_checksum=context["intelligence"].result_checksum,
        thesis_match_projection_id=projection.id,
        thesis_match_projection_checksum=projection.value.record_checksum,
        active_thesis_state=BindingState.PRESENT,
        active_thesis_id=context["thesis_id"],
        active_thesis_fingerprint=context["thesis_fingerprint"],
        adjudication_state=BindingState.PRESENT,
        adjudication_id=adjudication.id,
        adjudication_checksum=adjudication.value.record_checksum,
        previous_review_id=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
        policy_version=policy.version,
        policy_checksum=policy.checksum(),
        created_at=NOW + timedelta(seconds=3),
        semantic_identity_checksum="a" * 64,
        record_checksum="a" * 64,
    )
    value = replace(value, **changes)
    if "semantic_identity_checksum" not in changes:
        value = replace(
            value, semantic_identity_checksum=value.expected_semantic_identity_checksum()
        )
    if "record_checksum" not in changes:
        value = replace(value, record_checksum=value.expected_record_checksum())
    return value


def test_preview_store_has_no_official_publish_api(context) -> None:
    store = context["store"]
    assert callable(store.publish_thesis_match)
    assert callable(store.publish_adjudication)
    assert callable(store.publish_review)
    assert not hasattr(store, "publish_announcement_content")


def test_projection_round_trip_and_semantic_idempotency(context) -> None:
    store = context["store"]
    desired = desired_projection(context)
    stored = store.publish_thesis_match(desired)
    later = replace(desired, created_at=desired.created_at + timedelta(minutes=1))
    later = replace(later, record_checksum=later.expected_record_checksum())
    assert store.publish_thesis_match(later) == stored
    assert store.latest_thesis_match("123456", context["intelligence_run_id"]) == stored


def test_projection_rejects_wrong_subject_and_checksum_drift(context) -> None:
    with pytest.raises(HoldingReviewStoreError, match="binding"):
        context["store"].publish_thesis_match(
            desired_projection(context, fund_code="654321")
        )
    with pytest.raises(HoldingReviewStoreError, match="binding"):
        context["store"].publish_thesis_match(
            desired_projection(context, intelligence_snapshot_checksum="f" * 64)
        )


def test_projection_rejects_contradictory_clean_direct_and_original_claims(context) -> None:
    valid = desired_projection(context)
    evidence = valid.evidence_descriptors[0]
    contradictory = (
        replace(evidence, conflicted=False),
        replace(evidence, direct_subject_binding=True),
        replace(evidence, current=False),
        replace(
            evidence,
            lineage_kind=LineageKind.ORIGINAL,
            original_lineage=True,
        ),
    )
    for descriptor in contradictory:
        value = replace(valid, evidence_descriptors=(descriptor,))
        value = replace(value, evidence_set_checksum=value.expected_evidence_set_checksum())
        value = replace(value, record_checksum=value.expected_record_checksum())
        with pytest.raises(HoldingReviewStoreError, match="evidence descriptor"):
            context["store"].publish_thesis_match(value)


def test_lineage_direction_requires_structural_proof() -> None:
    edge = LineageEdge(
        edge_id="edge_reprint",
        from_item_id="derivative",
        to_item_id="source",
        kind=LineageKind.REPRINT,
        evidence_ids=("derivative", "source"),
    )
    ambiguous_one = replace(edge, edge_id="edge_one", to_item_id="source_one")
    ambiguous_two = replace(edge, edge_id="edge_two", to_item_id="source_two")

    derived = holding_store_module._derive_lineage_states(
        ("derivative", "isolated", "source"),
        (edge,),
        graph_complete=True,
    )
    ambiguous = holding_store_module._derive_lineage_states(
        ("derivative", "source_one", "source_two"),
        (ambiguous_one, ambiguous_two),
        graph_complete=True,
    )

    assert derived["derivative"] == (LineageKind.REPRINT, True)
    assert derived["source"] == (LineageKind.ORIGINAL, True)
    assert derived["isolated"] == (LineageKind.UNKNOWN, False)
    assert ambiguous["derivative"] == (LineageKind.UNKNOWN, False)
    assert ambiguous["source_one"] == (LineageKind.UNKNOWN, False)
    assert ambiguous["source_two"] == (LineageKind.UNKNOWN, False)

    for kind in (LineageKind.UNKNOWN, LineageKind.INDEPENDENTLY_REPORTED):
        unsupported = replace(edge, kind=kind)
        unsupported_result = holding_store_module._derive_lineage_states(
            ("derivative", "source"),
            (unsupported,),
            graph_complete=True,
        )
        assert unsupported_result["derivative"] == (LineageKind.UNKNOWN, False)
        assert unsupported_result["source"] == (LineageKind.UNKNOWN, False)


@pytest.mark.parametrize("request_run_id", [True, 0, -1])
def test_latest_projection_rejects_nonpositive_or_bool_id(context, request_run_id) -> None:
    with pytest.raises(ValueError, match="positive exact integer"):
        context["store"].latest_thesis_match("123456", request_run_id)


def test_adjudication_round_trip_and_supersession(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    first = store.publish_adjudication(desired_adjudication(context, projection))
    assert store.publish_adjudication(first.value) == first
    assert store.current_adjudication(projection.id) == first
    replacement = store.publish_adjudication(
        desired_adjudication(
            context,
            projection,
            decision=AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
            superseded_adjudication_id=first.id,
            created_at=NOW + timedelta(seconds=2),
        )
    )
    assert store.current_adjudication(projection.id) == replacement
    with pytest.raises(HoldingReviewStoreError, match="supersession"):
        store.publish_adjudication(
            desired_adjudication(
                context,
                projection,
                decision=AdjudicationDecision.UNCERTAIN,
                superseded_adjudication_id=first.id,
                created_at=NOW + timedelta(seconds=3),
            )
        )


def test_review_rejects_superseded_adjudication(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    first = store.publish_adjudication(desired_adjudication(context, projection))
    store.publish_adjudication(
        desired_adjudication(
            context,
            projection,
            decision=AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
            superseded_adjudication_id=first.id,
            created_at=NOW + timedelta(seconds=2),
        )
    )

    with pytest.raises(HoldingReviewStoreError, match="current adjudication"):
        store.publish_review(desired_review(context, projection, first))


def test_legacy_drifted_projection_fails_on_every_decision_read_path(context) -> None:
    store = context["store"]
    evidence = desired_projection(context).evidence_descriptors[0]
    drifted_evidence = replace(evidence, direct_subject_binding=True)
    drifted = replace(
        desired_projection(context),
        evidence_descriptors=(drifted_evidence,),
        evidence_set_checksum="a" * 64,
        record_checksum="a" * 64,
    )
    drifted = replace(drifted, evidence_set_checksum=drifted.expected_evidence_set_checksum())
    drifted = replace(drifted, record_checksum=drifted.expected_record_checksum())
    with context["repository"].connect() as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO thesis_match_projections(
                fund_code, thesis_id, thesis_fingerprint,
                intelligence_request_run_id, intelligence_snapshot_id,
                intelligence_snapshot_checksum, matcher_policy_version,
                matcher_policy_checksum, projection_state, evidence_ids_json,
                evidence_descriptors_json, evidence_set_checksum, created_at,
                record_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                drifted.fund_code,
                drifted.thesis_id,
                drifted.thesis_fingerprint,
                drifted.intelligence_request_run_id,
                drifted.intelligence_snapshot_id,
                drifted.intelligence_snapshot_checksum,
                drifted.matcher_policy_version,
                drifted.matcher_policy_checksum,
                drifted.projection_state.value,
                canonical_json_bytes(drifted.evidence_ids).decode("ascii"),
                canonical_json_bytes(
                    tuple(item.to_canonical_dict() for item in drifted.evidence_descriptors)
                ).decode("ascii"),
                drifted.evidence_set_checksum,
                drifted.created_at.isoformat(),
                drifted.record_checksum,
            ),
        )
        projection_row = connection.execute(
            "SELECT * FROM thesis_match_projections WHERE id=?", (int(cursor.lastrowid),)
        ).fetchone()
        projection = store._stored_projection(projection_row)
        adjudication = desired_adjudication(context, projection)
        adjudication_cursor = connection.execute(
            """
            INSERT INTO thesis_evidence_adjudications(
                fund_code, thesis_id, thesis_fingerprint,
                thesis_match_projection_id, thesis_match_projection_checksum,
                intelligence_request_run_id, intelligence_snapshot_checksum,
                evidence_ids_json, evidence_set_checksum, decision,
                superseded_adjudication_id, created_at, record_checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                adjudication.fund_code,
                adjudication.thesis_id,
                adjudication.thesis_fingerprint,
                adjudication.thesis_match_projection_id,
                adjudication.thesis_match_projection_checksum,
                adjudication.intelligence_request_run_id,
                adjudication.intelligence_snapshot_checksum,
                canonical_json_bytes(adjudication.evidence_ids).decode("ascii"),
                adjudication.evidence_set_checksum,
                adjudication.decision.value,
                adjudication.superseded_adjudication_id,
                adjudication.created_at.isoformat(),
                adjudication.record_checksum,
            ),
        )
        adjudication_row = connection.execute(
            "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
            (int(adjudication_cursor.lastrowid),),
        ).fetchone()
        stored_adjudication = store._stored_adjudication(adjudication_row)

    with pytest.raises(HoldingReviewStoreError, match="evidence descriptor"):
        store.current_adjudication(projection.id)
    with pytest.raises(HoldingReviewStoreError, match="evidence descriptor"):
        store.publish_review(
            desired_review(context, projection, stored_adjudication)
        )


def test_review_round_trip_previous_binding_and_privacy(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(desired_adjudication(context, projection))
    first = store.publish_review(desired_review(context, projection, adjudication))
    assert store.publish_review(first.value) == first
    assert store.latest_comparable_review(
        "123456",
        ActionKind.CONTINUE_HOLDING,
        context["thesis_fingerprint"],
        HeldFundManualReviewPolicyV1().checksum(),
    ) == first
    later_result = replace(
        first.value.result,
        omitted_work=("later_check",),
        created_at=NOW + timedelta(seconds=3),
    )
    second_value = desired_review(
        context,
        projection,
        adjudication,
        previous_review_id=first.id,
        result=later_result,
        result_fingerprint=later_result.expected_result_fingerprint(),
        created_at=NOW + timedelta(seconds=4),
    )
    second_value = replace(
        second_value,
        semantic_identity_checksum=second_value.expected_semantic_identity_checksum(),
    )
    second_value = replace(
        second_value, record_checksum=second_value.expected_record_checksum()
    )
    second = store.publish_review(second_value)
    assert second.id != first.id
    with context["repository"].connect() as connection:
        payload = connection.execute(
            "SELECT result_json FROM holding_review_snapshots WHERE id=?", (second.id,)
        ).fetchone()[0]
        assert "normalized_content" not in payload
        assert '"amount":' not in payload
        assert connection.execute(
            "SELECT count(*) FROM fund_official_announcement_contents"
        ).fetchone()[0] == 0


def test_review_rejects_skipped_comparable_history_and_pointer_conflict(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(desired_adjudication(context, projection))
    first = store.publish_review(desired_review(context, projection, adjudication))

    pointer_conflict = replace(first.value, previous_review_id=first.id)
    pointer_conflict = replace(
        pointer_conflict, record_checksum=pointer_conflict.expected_record_checksum()
    )
    with pytest.raises(HoldingReviewStoreError, match="semantic identity"):
        store.publish_review(pointer_conflict)

    second_result = replace(
        first.value.result,
        omitted_work=("second_check",),
        created_at=NOW + timedelta(seconds=3),
    )
    second = desired_review(
        context,
        projection,
        adjudication,
        previous_review_id=first.id,
        result=second_result,
        result_fingerprint=second_result.expected_result_fingerprint(),
        created_at=NOW + timedelta(seconds=4),
    )
    second = replace(
        second,
        semantic_identity_checksum=second.expected_semantic_identity_checksum(),
    )
    second = replace(second, record_checksum=second.expected_record_checksum())
    store.publish_review(second)

    third_result = replace(
        first.value.result,
        omitted_work=("third_check",),
        created_at=NOW + timedelta(seconds=5),
    )
    skipped = desired_review(
        context,
        projection,
        adjudication,
        previous_review_id=first.id,
        result=third_result,
        result_fingerprint=third_result.expected_result_fingerprint(),
        created_at=NOW + timedelta(seconds=6),
    )
    skipped = replace(
        skipped, semantic_identity_checksum=skipped.expected_semantic_identity_checksum()
    )
    skipped = replace(skipped, record_checksum=skipped.expected_record_checksum())
    with pytest.raises(HoldingReviewStoreError, match="latest comparable"):
        store.publish_review(skipped)


def test_review_rejects_cross_thesis_previous_pointer(context) -> None:
    store = context["store"]
    first_projection = store.publish_thesis_match(desired_projection(context))
    first_adjudication = store.publish_adjudication(
        desired_adjudication(context, first_projection)
    )
    first = store.publish_review(
        desired_review(context, first_projection, first_adjudication)
    )
    thesis = InvestmentThesis(
        fund_code="123456",
        rationale="Replacement long-term thesis.",
        horizon="Five years.",
        invalidation="The replacement condition fails.",
        created_at=NOW - timedelta(days=1),
    )
    thesis_id = context["repository"].add_thesis(thesis)
    fingerprint = thesis_record_fingerprint(thesis_id, thesis)
    second_projection = store.publish_thesis_match(
        desired_projection(
            context,
            thesis_id=thesis_id,
            thesis_fingerprint=fingerprint,
            created_at=NOW + timedelta(seconds=4),
        )
    )
    second_adjudication_value = replace(
        desired_adjudication(context, second_projection),
        thesis_id=thesis_id,
        thesis_fingerprint=fingerprint,
        created_at=NOW + timedelta(seconds=5),
        record_checksum="a" * 64,
    )
    second_adjudication_value = replace(
        second_adjudication_value,
        record_checksum=second_adjudication_value.expected_record_checksum(),
    )
    second_adjudication = store.publish_adjudication(second_adjudication_value)
    second = desired_review(
        context,
        second_projection,
        second_adjudication,
        active_thesis_id=thesis_id,
        active_thesis_fingerprint=fingerprint,
        previous_review_id=first.id,
        created_at=NOW + timedelta(seconds=6),
    )
    second = replace(
        second,
        semantic_identity_checksum=second.expected_semantic_identity_checksum(),
    )
    second = replace(second, record_checksum=second.expected_record_checksum())
    assert second_adjudication_value.thesis_id == thesis_id

    with pytest.raises(HoldingReviewStoreError, match="comparable"):
        store.publish_review(second)


def test_latest_comparable_review_supports_missing_thesis(context) -> None:
    store = context["store"]
    regular_projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(
        desired_adjudication(context, regular_projection)
    )
    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE id=?",
            (context["thesis_id"],),
        )
    projection_value = desired_projection(
        context,
        thesis_id=None,
        thesis_fingerprint=None,
        projection_state=ThesisMatchProjectionState.THESIS_MISSING,
        evidence_descriptors=(),
    )
    projection = store.publish_thesis_match(projection_value)
    value = desired_review(context, regular_projection, adjudication)
    result = replace(
        value.result,
        thesis_review_state=ThesisMatchState.THESIS_MISSING,
        evidence_ids=(),
        action_review_source_sufficiency=(
            ActionReviewSourceSufficiency.INSUFFICIENT_DATA
        ),
    )
    value = replace(
        value,
        thesis_match_projection_id=projection.id,
        thesis_match_projection_checksum=projection.value.record_checksum,
        active_thesis_state=BindingState.MISSING,
        active_thesis_id=None,
        active_thesis_fingerprint=None,
        adjudication_state=BindingState.MISSING,
        adjudication_id=None,
        adjudication_checksum=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
    )
    value = replace(value, semantic_identity_checksum=value.expected_semantic_identity_checksum())
    value = replace(value, record_checksum=value.expected_record_checksum())
    stored = store.publish_review(value)
    context["repository"].add_thesis(
        InvestmentThesis(
            fund_code="123456",
            rationale="Replacement active thesis.",
            horizon="Five years.",
            invalidation="Replacement condition.",
            created_at=NOW,
        )
    )

    assert store.latest_comparable_review(
        "123456",
        ActionKind.CONTINUE_HOLDING,
        None,
        HeldFundManualReviewPolicyV1().checksum(),
    ) == stored


def test_stale_thesis_missing_projection_is_unusable_after_active_thesis_added(
    context,
) -> None:
    store = context["store"]
    regular_projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(
        desired_adjudication(context, regular_projection)
    )
    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE id=?",
            (context["thesis_id"],),
        )
    missing_value = desired_projection(
        context,
        thesis_id=None,
        thesis_fingerprint=None,
        projection_state=ThesisMatchProjectionState.THESIS_MISSING,
        evidence_descriptors=(),
    )
    missing = store.publish_thesis_match(missing_value)
    context["repository"].add_thesis(
        InvestmentThesis(
            fund_code="123456",
            rationale="New active thesis.",
            horizon="Five years.",
            invalidation="New invalidation.",
            created_at=NOW,
        )
    )
    value = desired_review(context, regular_projection, adjudication)
    result = replace(
        value.result,
        thesis_review_state=ThesisMatchState.THESIS_MISSING,
        evidence_ids=(),
        action_review_source_sufficiency=(
            ActionReviewSourceSufficiency.INSUFFICIENT_DATA
        ),
    )
    value = replace(
        value,
        thesis_match_projection_id=missing.id,
        thesis_match_projection_checksum=missing.value.record_checksum,
        active_thesis_state=BindingState.MISSING,
        active_thesis_id=None,
        active_thesis_fingerprint=None,
        adjudication_state=BindingState.MISSING,
        adjudication_id=None,
        adjudication_checksum=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
    )
    value = replace(value, semantic_identity_checksum=value.expected_semantic_identity_checksum())
    value = replace(value, record_checksum=value.expected_record_checksum())

    with pytest.raises(HoldingReviewStoreError, match="active thesis absence"):
        store.latest_thesis_match("123456", context["intelligence_run_id"])
    with pytest.raises(HoldingReviewStoreError, match="active thesis absence"):
        store.publish_review(value)


def test_historical_review_survives_adjudication_supersession_and_thesis_replacement(
    context,
) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(desired_adjudication(context, projection))
    review = store.publish_review(desired_review(context, projection, adjudication))
    store.publish_adjudication(
        desired_adjudication(
            context,
            projection,
            decision=AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
            superseded_adjudication_id=adjudication.id,
            created_at=NOW + timedelta(seconds=4),
        )
    )
    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE id=?",
            (context["thesis_id"],),
        )
    context["repository"].add_thesis(
        InvestmentThesis(
            fund_code="123456",
            rationale="Replacement thesis.",
            horizon="Five years.",
            invalidation="Replacement invalidation.",
            created_at=NOW,
        )
    )

    assert store.latest_comparable_review(
        "123456",
        ActionKind.CONTINUE_HOLDING,
        context["thesis_fingerprint"],
        HeldFundManualReviewPolicyV1().checksum(),
    ) == review


def test_review_rejects_wrong_action_and_unknown_previous_review(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(desired_adjudication(context, projection))
    with pytest.raises(HoldingReviewStoreError, match="binding"):
        store.publish_review(
            desired_review(
                context, projection, adjudication, action=ActionKind.FULL_EXIT
            )
        )
    with pytest.raises(HoldingReviewStoreError, match="binding"):
        store.publish_review(
            desired_review(context, projection, adjudication, previous_review_id=999)
        )


def test_preview_review_rejects_completed_official_negative_check(context) -> None:
    store = context["store"]
    projection = store.publish_thesis_match(desired_projection(context))
    adjudication = store.publish_adjudication(desired_adjudication(context, projection))
    value = desired_review(context, projection, adjudication)
    result = replace(value.result, official_negative_check_complete=True)
    value = replace(
        value,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
    )
    value = replace(
        value,
        semantic_identity_checksum=value.expected_semantic_identity_checksum(),
    )
    value = replace(value, record_checksum=value.expected_record_checksum())

    with pytest.raises(HoldingReviewStoreError, match="preview boundary"):
        store.publish_review(value)
