from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import MAX_TUPLE_ITEMS, ActionKind, canonical_json_bytes
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    AdjudicationDecision,
    BindingState,
    ConditionalReviewUsability,
    EvidenceDelta,
    EvidenceReadiness,
    ExitReason,
    FlowStatus,
    HeldReviewOfficialEventProjection,
    HistoryComparability,
    HoldingReviewInputs,
    HoldingReviewOutcome,
    HoldingReviewResult,
    HoldingReviewSnapshot,
    OfficialAnnouncementContent,
    OfficialEventEvidenceReference,
    RedemptionComponentState,
    RedemptionEvidence,
    RedemptionFeasibility,
    RemainderIntent,
    ReviewBoundary,
    ReviewDisposition,
    ReviewEvidenceItem,
    ThesisEvidenceAdjudication,
    ThesisMatchProjection,
    ThesisMatchProjectionState,
    ThesisMatchState,
    ThesisReviewReadiness,
    TransientHoldingReviewOutcome,
    TriggeredReviewCode,
    UseOfProceeds,
)
from kunjin.holding_review.policy import (
    HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM,
    HeldFundManualReviewPolicyV1,
)
from kunjin.intelligence.models import LineageKind

NOW = datetime(2026, 7, 19, 4, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def redemption_evidence() -> RedemptionEvidence:
    missing = RedemptionComponentState.MISSING
    return RedemptionEvidence(
        current_position=missing,
        exact_share_class=missing,
        applicable_holding_period_tier=missing,
        channel_rule=missing,
        current_redemption_restriction=missing,
        applicable_fee_schedule=missing,
        settlement_rule=missing,
    )


def review_result(**changes: object) -> HoldingReviewResult:
    values: dict[str, object] = {
        "fund_code": "123456",
        "action": ActionKind.CONTINUE_HOLDING,
        "flow_status": FlowStatus.COMPLETE,
        "evidence_readiness": EvidenceReadiness.READY,
        "history_comparability": HistoryComparability.NOT_AVAILABLE,
        "thesis_review_state": ThesisMatchState.PRESENTED_MATCH_REJECTED,
        "review_disposition": ReviewDisposition.CONTINUE_OBSERVING,
        "triggered_reviews": (),
        "official_event_evidence": (),
        "redemption_feasibility": RedemptionFeasibility.NOT_REQUESTED,
        "redemption_evidence": redemption_evidence(),
        "sell_timing": "insufficient_data",
        "upstream_action_boundary": ("research_only",),
        "boundary": ReviewBoundary(),
        "omitted_work": (),
        "official_negative_check_complete": True,
        "intelligence_schedule_complete": True,
        "intelligence_omitted_work": (),
        "intelligence_degraded_sources": (),
        "action_review_source_sufficiency": ActionReviewSourceSufficiency.SUFFICIENT,
        "hard_event_review": False,
        "evidence_ids": ("evidence_a",),
        "evidence_delta": EvidenceDelta(
            history_comparability=HistoryComparability.NOT_AVAILABLE,
            evidence_unchanged=False,
        ),
        "remainder_intent": RemainderIntent.UNKNOWN,
        "exit_reason": ExitReason.UNKNOWN,
        "use_of_proceeds": UseOfProceeds.UNKNOWN,
        "policy_version": "1",
        "policy_checksum": CHECKSUM,
        "created_at": NOW,
    }
    values.update(changes)
    return HoldingReviewResult(**values)  # type: ignore[arg-type]


def test_every_closed_enum_has_the_reviewed_values() -> None:
    assert tuple(item.value for item in FlowStatus) == ("complete", "partial", "failed")
    assert tuple(item.value for item in ReviewDisposition) == (
        "continue_observing",
        "manual_thesis_review_required",
        "reduce_review",
        "exit_review",
        "abstain",
    )
    assert tuple(item.value for item in ThesisMatchState) == (
        "thesis_missing",
        "no_matching_evidence",
        "manual_review_pending",
        "manual_review_uncertain",
        "presented_match_rejected",
        "presented_match_confirmed",
        "thesis_binding_invalid",
    )
    assert tuple(item.value for item in AdjudicationDecision) == (
        "presented_match_confirmed",
        "presented_match_rejected",
        "uncertain",
    )
    assert tuple(item.value for item in RedemptionFeasibility) == (
        "not_requested",
        "insufficient_data",
        "restricted",
        "evidence_complete_non_authorizing",
    )
    assert tuple(item.value for item in EvidenceReadiness) == (
        "ready",
        "partial",
        "insufficient_data",
    )
    assert tuple(item.value for item in HistoryComparability) == (
        "comparable",
        "not_comparable",
        "not_available",
    )
    assert tuple(item.value for item in ThesisMatchProjectionState) == (
        "thesis_missing",
        "no_matching_evidence",
        "possible_invalidation_match",
    )
    assert tuple(item.value for item in ActionReviewSourceSufficiency) == (
        "sufficient",
        "insufficient_data",
    )
    assert tuple(item.value for item in RedemptionComponentState) == (
        "usable",
        "restricted",
        "missing",
        "stale",
        "conflicted",
        "unsupported",
    )
    assert tuple(item.value for item in TriggeredReviewCode) == (
        "full_exit_feasibility_review",
        "redemption_restriction_review",
        "manager_change_review",
        "fee_change_review",
        "benchmark_change_review",
    )
    assert tuple(item.value for item in RemainderIntent) == (
        "retain_some",
        "no_minimum_intent",
        "unknown",
    )
    assert tuple(item.value for item in ExitReason) == (
        "owner_believes_thesis_invalidated",
        "goal_changed",
        "cash_need",
        "risk_reduction",
        "other",
        "unknown",
    )
    assert tuple(item.value for item in UseOfProceeds) == (
        "cash_reserve",
        "known_goal",
        "reallocation_review",
        "other",
        "unknown",
    )
    assert tuple(item.value for item in ThesisReviewReadiness) == (
        "ready",
        "manual_review_required",
        "missing",
        "insufficient_data",
    )
    assert tuple(item.value for item in ConditionalReviewUsability) == (
        "observed_for_request",
        "partial",
        "not_testable",
    )
    assert tuple(item.value for item in BindingState) == ("present", "missing")


def test_minimal_review_result_is_canonical_and_valid() -> None:
    result = review_result()
    result.validate()
    canonical = result.canonical_json()
    assert canonical == json.dumps(
        json.loads(canonical), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    assert result.checksum() == result.checksum()
    assert result.to_canonical_dict()["review_disposition"] == "continue_observing"


def test_review_boundary_is_permanently_non_authorizing() -> None:
    boundary = ReviewBoundary()
    boundary.validate()
    assert boundary.review_maturity == "evidence_only"
    assert boundary.action_authorized is False
    assert boundary.exact_amount_available is False
    assert boundary.automatic_trade is False


@pytest.mark.parametrize(
    "omitted",
    (
        "identity_profile",
        "personal_position_observation",
        "formal_nav",
        "manager_fee_profile",
        "holdings_industries",
        "official_announcements",
    ),
)
def test_continue_observing_rejects_each_core_omission(omitted: str) -> None:
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(omitted_work=(omitted,)).validate()


@pytest.mark.parametrize(
    "changes",
    (
        {"intelligence_schedule_complete": False},
        {"intelligence_omitted_work": ("stcn_detail_cap_reached",)},
        {"intelligence_degraded_sources": ("stcn_fund_news",)},
    ),
)
def test_continue_observing_rejects_incomplete_intelligence_negative_check(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(**changes).validate()


def test_owner_confirmation_cannot_upgrade_source_sufficiency() -> None:
    result = review_result(
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
        action=ActionKind.FULL_EXIT,
        action_review_source_sufficiency=ActionReviewSourceSufficiency.INSUFFICIENT_DATA,
        review_disposition=ReviewDisposition.EXIT_REVIEW,
    )
    with pytest.raises(ValueError, match="exit review source evidence is insufficient"):
        result.validate()


@pytest.mark.parametrize("invalid", (True, False, 0, -1, "1"))
def test_database_request_run_ids_are_positive_exact_integers(invalid: object) -> None:
    from kunjin.holding_review.models import HoldingReviewInputs

    with pytest.raises(ValueError, match="positive exact integer"):
        HoldingReviewInputs.minimal(
            fund_code="123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=invalid,  # type: ignore[arg-type]
            intelligence_request_run_id=2,
            now=NOW,
            policy_checksum=CHECKSUM,
        ).validate()


@pytest.mark.parametrize("fund_code", ("000000", "12345", "１２３４５６", 123456))
def test_fund_code_is_six_ascii_digits_and_non_reserved(fund_code: object) -> None:
    with pytest.raises(ValueError, match="non-reserved six-digit"):
        replace(review_result(), fund_code=fund_code).validate()  # type: ignore[arg-type]


def test_exact_types_subclasses_sorted_unique_ids_and_utc_are_required() -> None:
    class ResultSubclass(HoldingReviewResult):
        pass

    with pytest.raises(ValueError, match="exact HoldingReviewResult"):
        ResultSubclass(**vars(review_result())).validate()
    with pytest.raises(ValueError, match="sorted unique"):
        replace(review_result(), evidence_ids=("evidence_b", "evidence_a")).validate()
    with pytest.raises(ValueError, match="UTC"):
        replace(review_result(), created_at=NOW.astimezone(timezone(timedelta(hours=8)))).validate()
    with pytest.raises(ValueError, match="exact boolean"):
        replace(review_result(), official_negative_check_complete=1).validate()  # type: ignore[arg-type]


def test_triggered_reviews_and_redemption_components_are_exact() -> None:
    with pytest.raises(ValueError, match="triggered reviews"):
        replace(review_result(), triggered_reviews=("unknown_review",)).validate()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact RedemptionComponentState"):
        replace(redemption_evidence(), settlement_rule="missing").validate()  # type: ignore[arg-type]


def test_noncanonical_checksum_and_private_instance_key_are_rejected() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(review_result(), policy_checksum="A" * 64).validate()
    result = review_result()
    object.__setattr__(result, "shares", "private")
    with pytest.raises(ValueError, match="unexpected instance state"):
        result.validate()


def test_policy_v1_is_frozen_canonical_and_has_a_golden_checksum() -> None:
    policy = HeldFundManualReviewPolicyV1()
    policy.validate()
    assert policy.orchestration_window_seconds == 30 * 60
    assert policy.maximum_announcement_candidates == 20
    assert policy.maximum_announcement_body_bytes == 512 * 1024
    assert policy.maximum_announcement_total_bytes == 4 * 1024 * 1024
    assert policy.sell_timing == "insufficient_data"
    assert policy.action_authorized is False
    assert policy.exact_amount_available is False
    assert policy.automatic_trade is False
    assert policy.checksum() == HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM
    with pytest.raises(FrozenInstanceError):
        policy.orchestration_window_seconds = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical"):
        replace(policy, orchestration_window_seconds=1).validate()


def test_policy_rejects_subclasses() -> None:
    class DerivedPolicy(HeldFundManualReviewPolicyV1):
        pass

    with pytest.raises(ValueError, match="subclasses"):
        DerivedPolicy().validate()


@pytest.mark.parametrize(
    "changes",
    (
        {"flow_status": FlowStatus.PARTIAL},
        {"flow_status": FlowStatus.FAILED},
        {"evidence_readiness": EvidenceReadiness.PARTIAL},
        {"evidence_readiness": EvidenceReadiness.INSUFFICIENT_DATA},
        {"thesis_review_state": ThesisMatchState.THESIS_MISSING},
        {"thesis_review_state": ThesisMatchState.MANUAL_REVIEW_PENDING},
        {"thesis_review_state": ThesisMatchState.MANUAL_REVIEW_UNCERTAIN},
        {"thesis_review_state": ThesisMatchState.PRESENTED_MATCH_CONFIRMED},
        {"hard_event_review": True},
    ),
)
def test_continue_observing_requires_a_closed_complete_negative_check(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(**changes).validate()


def test_bare_hard_event_boolean_cannot_bypass_action_source_sufficiency() -> None:
    result = review_result(
        action=ActionKind.FULL_EXIT,
        review_disposition=ReviewDisposition.EXIT_REVIEW,
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
        action_review_source_sufficiency=ActionReviewSourceSufficiency.INSUFFICIENT_DATA,
        hard_event_review=True,
        triggered_reviews=(),
        redemption_feasibility=RedemptionFeasibility.INSUFFICIENT_DATA,
    )
    with pytest.raises(ValueError, match="authenticated hard-event trigger"):
        result.validate()


def test_non_hard_trigger_cannot_bypass_action_source_sufficiency() -> None:
    reference = official_reference(
        event_code=OfficialEventCode.MANAGER_CHANGE_NOTICE,
        triggered_review_code=TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
    )
    result = review_result(
        action=ActionKind.FULL_EXIT,
        review_disposition=ReviewDisposition.EXIT_REVIEW,
        thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
        action_review_source_sufficiency=ActionReviewSourceSufficiency.INSUFFICIENT_DATA,
        hard_event_review=True,
        triggered_reviews=(TriggeredReviewCode.MANAGER_CHANGE_REVIEW,),
        official_event_evidence=(reference,),
        evidence_ids=("evidence_a", reference.support_evidence_id),
        redemption_feasibility=RedemptionFeasibility.INSUFFICIENT_DATA,
    )
    with pytest.raises(ValueError, match="authenticated hard-event trigger"):
        result.validate()


def test_redemption_feasibility_must_match_all_seven_components() -> None:
    usable = RedemptionComponentState.USABLE
    complete = RedemptionEvidence(usable, usable, usable, usable, usable, usable, usable)
    replace(
        review_result(),
        action=ActionKind.FULL_EXIT,
        review_disposition=ReviewDisposition.ABSTAIN,
        redemption_feasibility=RedemptionFeasibility.EVIDENCE_COMPLETE_NON_AUTHORIZING,
        redemption_evidence=complete,
    ).validate()
    with pytest.raises(ValueError, match="redemption feasibility"):
        replace(
            review_result(),
            action=ActionKind.FULL_EXIT,
            review_disposition=ReviewDisposition.ABSTAIN,
            redemption_feasibility=RedemptionFeasibility.EVIDENCE_COMPLETE_NON_AUTHORIZING,
            redemption_evidence=replace(
                complete,
                applicable_holding_period_tier=RedemptionComponentState.MISSING,
            ),
        ).validate()
    with pytest.raises(ValueError, match="redemption feasibility"):
        replace(
            review_result(),
            action=ActionKind.FULL_EXIT,
            review_disposition=ReviewDisposition.ABSTAIN,
            redemption_feasibility=RedemptionFeasibility.RESTRICTED,
            redemption_evidence=complete,
        ).validate()


def desired_announcement(**changes: object) -> OfficialAnnouncementContent:
    content = "基金公告正文"
    values: dict[str, object] = {
        "brief_request_run_id": 1,
        "source_attempt_id": 2,
        "fund_code": "123456",
        "listing_source_document_id": 3,
        "canonical_announcement_url": "https://www.fund001.com/fund/123456/notice.html",
        "announcement_title": "123456基金公告",
        "announcement_published_at": NOW,
        "publisher": "交银施罗德基金管理有限公司",
        "normalized_content": content,
        "normalized_content_bytes": len(content.encode("utf-8")),
        "normalized_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "original_source_id": "fund_manager_official_documents",
        "quoted_source_id": None,
        "integrity_status": "active",
        "integrity_checked_at": NOW,
        "retrieved_at": NOW,
        "record_checksum": CHECKSUM,
    }
    values.update(changes)
    value = OfficialAnnouncementContent(**values)  # type: ignore[arg-type]
    return replace(value, record_checksum=value.expected_record_checksum())


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1/fund/123456/notice.html",
        "https://localhost/fund/123456/notice.html",
        "https://manager.local/fund/123456/notice.html",
        "https://WWW.FUND001.COM/fund/123456/notice.html",
        "https://www.fund001.com/fund/123456/notice.html?access_token=secret",
    ),
)
def test_announcement_rejects_nonpublic_or_noncanonical_urls(url: str) -> None:
    with pytest.raises(ValueError, match="canonical public HTTPS URL"):
        desired_announcement(canonical_announcement_url=url).validate()


def test_announcement_host_must_bind_exact_registered_manager_publisher() -> None:
    with pytest.raises(ValueError, match="registered manager"):
        desired_announcement(publisher="其他基金管理人").validate()


@pytest.mark.parametrize(
    "private_content",
    (
        "authorization bearer abc",
        "access token: abc",
        "password=abc",
        "api_key=abc",
        "cookie: sessionvalue",
        "https://example.test/doc?token=abc",
        "local path /private/tmp/input",
    ),
)
def test_announcement_content_rejects_private_markers(private_content: str) -> None:
    with pytest.raises(ValueError, match="private|secret"):
        desired_announcement(
            normalized_content=private_content,
            normalized_content_bytes=len(private_content.encode()),
            normalized_content_sha256=hashlib.sha256(private_content.encode()).hexdigest(),
        ).validate()


def test_authenticated_record_checksum_detects_key_field_tampering() -> None:
    value = desired_announcement()
    value.validate()
    with pytest.raises(ValueError, match="record checksum does not match"):
        replace(value, fund_code="654321").validate()


def test_publish_before_storage_values_do_not_contain_database_ids() -> None:
    assert "adjudication_id" not in {field.name for field in fields(ThesisEvidenceAdjudication)}
    assert "review_snapshot_id" not in {field.name for field in fields(HoldingReviewSnapshot)}


def evidence_item() -> ReviewEvidenceItem:
    return ReviewEvidenceItem(
        evidence_id="evidence_a",
        source_tier=1,
        lineage_kind=LineageKind.ORIGINAL,
        current=True,
        graph_closed=True,
        original_lineage=True,
        retracted=False,
        conflicted=False,
        direct_subject_binding=True,
    )


def official_reference(
    *,
    projection_id: int = 7,
    event_code: OfficialEventCode = OfficialEventCode.FUND_LIQUIDATION_NOTICE,
    triggered_review_code: TriggeredReviewCode = (
        TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW
    ),
) -> OfficialEventEvidenceReference:
    return OfficialEventEvidenceReference(
        projection_id=projection_id,
        projection_checksum="8" * 64,
        event_code=event_code,
        triggered_review_code=triggered_review_code,
    )


def desired_event_projection() -> HeldReviewOfficialEventProjection:
    value = HeldReviewOfficialEventProjection(
        brief_request_run_id=1,
        fund_code="123456",
        announcement_row_id=2,
        announcement_content_id=3,
        event_code=OfficialEventCode.MANAGER_CHANGE_NOTICE,
        triggered_review_code=TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
        policy_version="1",
        policy_checksum=CHECKSUM,
        record_checksum=CHECKSUM,
    )
    return replace(value, record_checksum=value.expected_record_checksum())


def desired_projection() -> ThesisMatchProjection:
    value = ThesisMatchProjection(
        fund_code="123456",
        thesis_id=4,
        thesis_fingerprint="b" * 64,
        intelligence_request_run_id=5,
        intelligence_snapshot_id=6,
        intelligence_snapshot_checksum="c" * 64,
        matcher_policy_version="1",
        matcher_policy_checksum="d" * 64,
        projection_state=ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH,
        evidence_descriptors=(evidence_item(),),
        evidence_set_checksum=CHECKSUM,
        created_at=NOW,
        record_checksum=CHECKSUM,
    )
    value = replace(value, evidence_set_checksum=value.expected_evidence_set_checksum())
    return replace(value, record_checksum=value.expected_record_checksum())


def desired_adjudication() -> ThesisEvidenceAdjudication:
    evidence_ids = ("evidence_a",)
    value = ThesisEvidenceAdjudication(
        fund_code="123456",
        thesis_id=4,
        thesis_fingerprint="b" * 64,
        thesis_match_projection_id=7,
        thesis_match_projection_checksum="e" * 64,
        intelligence_request_run_id=5,
        intelligence_snapshot_checksum="c" * 64,
        evidence_ids=evidence_ids,
        evidence_set_checksum=hashlib.sha256(canonical_json_bytes(evidence_ids)).hexdigest(),
        decision=AdjudicationDecision.PRESENTED_MATCH_REJECTED,
        superseded_adjudication_id=None,
        created_at=NOW,
        record_checksum=CHECKSUM,
    )
    return replace(value, record_checksum=value.expected_record_checksum())


def desired_snapshot() -> HoldingReviewSnapshot:
    result = review_result()
    value = HoldingReviewSnapshot(
        fund_code="123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=1,
        brief_snapshot_id=2,
        brief_snapshot_checksum="f" * 64,
        intelligence_request_run_id=5,
        intelligence_snapshot_id=6,
        intelligence_snapshot_checksum="c" * 64,
        thesis_match_projection_id=7,
        thesis_match_projection_checksum="e" * 64,
        active_thesis_state=BindingState.PRESENT,
        active_thesis_id=4,
        active_thesis_fingerprint="b" * 64,
        adjudication_state=BindingState.PRESENT,
        adjudication_id=8,
        adjudication_checksum="9" * 64,
        previous_review_id=None,
        result=result,
        result_fingerprint=result.expected_result_fingerprint(),
        policy_version="1",
        policy_checksum=CHECKSUM,
        created_at=NOW,
        semantic_identity_checksum=CHECKSUM,
        record_checksum=CHECKSUM,
    )
    value = replace(
        value,
        semantic_identity_checksum=value.expected_semantic_identity_checksum(),
    )
    return replace(value, record_checksum=value.expected_record_checksum())


def test_every_exact_record_has_a_valid_canonical_roundtrip() -> None:
    snapshot = desired_snapshot()
    records = (
        ReviewBoundary(),
        redemption_evidence(),
        official_reference(),
        desired_announcement(),
        desired_event_projection(),
        evidence_item(),
        desired_projection(),
        desired_adjudication(),
        EvidenceDelta(HistoryComparability.NOT_AVAILABLE, False),
        HoldingReviewInputs.minimal(
            fund_code="123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=1,
            intelligence_request_run_id=2,
            now=NOW,
            policy_checksum=CHECKSUM,
        ),
        review_result(),
        snapshot,
        HoldingReviewOutcome(FlowStatus.COMPLETE, snapshot),
        TransientHoldingReviewOutcome(
            FlowStatus.PARTIAL,
            None,
            ("intelligence_snapshot_missing",),
        ),
    )
    for record in records:
        record.validate()
        encoded = record.canonical_json()
        assert encoded == json.dumps(
            json.loads(encoded), ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")


@pytest.mark.parametrize(
    "factory,changes",
    (
        (desired_announcement, {"source_attempt_id": 99}),
        (desired_event_projection, {"announcement_content_id": 99}),
        (desired_projection, {"intelligence_snapshot_id": 99}),
        (desired_adjudication, {"decision": AdjudicationDecision.UNCERTAIN}),
        (desired_snapshot, {"previous_review_id": 99}),
    ),
)
def test_each_authenticated_record_rejects_business_field_tampering(
    factory: object,
    changes: dict[str, object],
) -> None:
    value = factory()  # type: ignore[operator]
    with pytest.raises(ValueError, match="record checksum does not match"):
        replace(value, **changes).validate()


def test_authenticated_pre_checksum_payload_excludes_only_record_checksum() -> None:
    for value in (
        desired_announcement(),
        desired_event_projection(),
        desired_projection(),
        desired_adjudication(),
        desired_snapshot(),
    ):
        assert "record_checksum" not in value.pre_checksum_canonical_dict()
        assert value.to_canonical_dict()["record_checksum"] == value.record_checksum
        assert value.expected_record_checksum() == value.record_checksum


def test_snapshot_semantic_identity_excludes_time_and_history_but_record_covers_them() -> None:
    original = desired_snapshot()
    changed = replace(
        original,
        previous_review_id=99,
        created_at=NOW + timedelta(minutes=1),
    )
    assert changed.expected_semantic_identity_checksum() == original.semantic_identity_checksum
    changed = replace(
        changed,
        semantic_identity_checksum=changed.expected_semantic_identity_checksum(),
    )
    assert changed.expected_record_checksum() != original.record_checksum
    changed = replace(changed, record_checksum=changed.expected_record_checksum())
    changed.validate()


def test_projection_thesis_missing_state_requires_null_binding_and_empty_evidence() -> None:
    value = desired_projection()
    with pytest.raises(ValueError, match="thesis-missing projection"):
        replace(value, projection_state=ThesisMatchProjectionState.THESIS_MISSING).validate()
    missing = replace(
        value,
        thesis_id=None,
        thesis_fingerprint=None,
        projection_state=ThesisMatchProjectionState.THESIS_MISSING,
        evidence_descriptors=(),
    )
    missing = replace(
        missing,
        evidence_set_checksum=missing.expected_evidence_set_checksum(),
    )
    missing = replace(missing, record_checksum=missing.expected_record_checksum())
    missing.validate()


def test_evidence_descriptor_rejects_free_string_lineage() -> None:
    with pytest.raises(ValueError, match="exact LineageKind"):
        replace(evidence_item(), lineage_kind="original").validate()  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "event_code,trigger",
    (
        (
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
            TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
        ),
        (
            OfficialEventCode.FUND_TERMINATION_NOTICE,
            TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
        ),
        (
            OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
            TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
        ),
        (OfficialEventCode.MANAGER_CHANGE_NOTICE, TriggeredReviewCode.MANAGER_CHANGE_REVIEW),
        (OfficialEventCode.FEE_CHANGE_NOTICE, TriggeredReviewCode.FEE_CHANGE_REVIEW),
        (OfficialEventCode.BENCHMARK_CHANGE_NOTICE, TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW),
    ),
)
def test_official_event_projection_has_one_fixed_nonempty_trigger(
    event_code: OfficialEventCode,
    trigger: TriggeredReviewCode,
) -> None:
    value = replace(
        desired_event_projection(),
        event_code=event_code,
        triggered_review_code=trigger,
    )
    value = replace(value, record_checksum=value.expected_record_checksum())
    value.validate()
    wrong = replace(value, triggered_review_code=TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW)
    if trigger is not TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW:
        with pytest.raises(ValueError, match="do not match"):
            wrong.validate()


def test_snapshot_rejects_failed_or_incoherent_thesis_and_adjudication_bindings() -> None:
    snapshot = desired_snapshot()
    failed_result = replace(
        snapshot.result,
        flow_status=FlowStatus.FAILED,
        evidence_readiness=EvidenceReadiness.INSUFFICIENT_DATA,
        review_disposition=ReviewDisposition.ABSTAIN,
    )
    with pytest.raises(ValueError, match="failed holding review"):
        replace(
            snapshot,
            result=failed_result,
            result_fingerprint=failed_result.expected_result_fingerprint(),
        ).validate()
    with pytest.raises(ValueError, match="active thesis state"):
        replace(snapshot, active_thesis_state=BindingState.MISSING).validate()
    with pytest.raises(ValueError, match="adjudication state"):
        replace(snapshot, adjudication_state=BindingState.MISSING).validate()


def test_action_review_requires_confirmed_thesis_or_authenticated_hard_event() -> None:
    incomplete = RedemptionEvidence(
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
    )
    with pytest.raises(ValueError, match="confirmed thesis evidence or a hard event"):
        review_result(
            action=ActionKind.FULL_EXIT,
            thesis_review_state=ThesisMatchState.NO_MATCHING_EVIDENCE,
            review_disposition=ReviewDisposition.EXIT_REVIEW,
            redemption_feasibility=RedemptionFeasibility.INSUFFICIENT_DATA,
            redemption_evidence=incomplete,
        ).validate()


def test_redemption_restriction_requires_exact_component_and_trigger_pair() -> None:
    restricted = replace(
        redemption_evidence(),
        current_redemption_restriction=RedemptionComponentState.RESTRICTED,
    )
    with pytest.raises(ValueError, match="authenticated review trigger"):
        review_result(
            action=ActionKind.FULL_EXIT,
            review_disposition=ReviewDisposition.ABSTAIN,
            redemption_feasibility=RedemptionFeasibility.RESTRICTED,
            redemption_evidence=restricted,
        ).validate()
    reference = official_reference(
        event_code=OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        triggered_review_code=TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
    )
    review_result(
        action=ActionKind.FULL_EXIT,
        review_disposition=ReviewDisposition.ABSTAIN,
        redemption_feasibility=RedemptionFeasibility.RESTRICTED,
        redemption_evidence=restricted,
        triggered_reviews=(TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,),
        official_event_evidence=(reference,),
        evidence_ids=("evidence_a", reference.support_evidence_id),
    ).validate()


def test_bounded_content_above_public_text_limit_is_validated_without_truncation() -> None:
    content = "基金公告正文。" * 800
    value = desired_announcement(
        normalized_content=content,
        normalized_content_bytes=len(content.encode()),
        normalized_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )
    value.validate()
    assert value.normalized_content == content
    assert value.to_canonical_dict()["normalized_content"] == content


@pytest.mark.parametrize(
    "alias",
    (
        "https://www.fund001.com/fund/123456/%7enotice.html",
        "https://www.fund001.com/fund/123456/%7Enotice.html",
        "https://www.fund001.com/fund/123456/%2e%2e/notice.html",
        "https://www.fund001.com/fund/123456/%2fnotice.html",
        "https://www.fund001.com/fund/123456/%2Fnotice.html",
        "https://www.fund001.com/fund/123456/%5cnotice.html",
        "https://www.fund001.com/fund/123456/%5Cnotice.html",
    ),
)
def test_announcement_url_rejects_percent_encoded_identity_aliases(alias: str) -> None:
    with pytest.raises(ValueError, match="canonical public HTTPS URL"):
        desired_announcement(canonical_announcement_url=alias).validate()


@pytest.mark.parametrize(
    "changes",
    (
        {"flow_status": FlowStatus.PARTIAL},
        {"evidence_readiness": EvidenceReadiness.PARTIAL},
        {"official_negative_check_complete": False},
        {"intelligence_schedule_complete": False},
        {"omitted_work": ("formal_nav",)},
        {"intelligence_omitted_work": ("stcn_detail_cap_reached",)},
        {"intelligence_degraded_sources": ("stcn_fund_news",)},
    ),
)
def test_evidence_unchanged_rejects_every_coverage_gap(changes: dict[str, object]) -> None:
    result = review_result(
        history_comparability=HistoryComparability.COMPARABLE,
        evidence_delta=EvidenceDelta(HistoryComparability.COMPARABLE, True),
        **changes,
    )
    with pytest.raises(ValueError, match="unchanged evidence requires complete current coverage"):
        result.validate()


def test_sufficient_source_state_requires_nonempty_evidence_ids() -> None:
    with pytest.raises(ValueError, match="sufficient source evidence requires evidence ids"):
        review_result(evidence_ids=()).validate()
    incomplete = RedemptionEvidence(
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
    )
    with pytest.raises(ValueError, match="sufficient source evidence requires evidence ids"):
        review_result(
            action=ActionKind.FULL_EXIT,
            thesis_review_state=ThesisMatchState.PRESENTED_MATCH_CONFIRMED,
            review_disposition=ReviewDisposition.EXIT_REVIEW,
            evidence_ids=(),
            redemption_feasibility=RedemptionFeasibility.INSUFFICIENT_DATA,
            redemption_evidence=incomplete,
        ).validate()


def test_result_timestamp_does_not_change_semantic_fingerprint_or_snapshot_identity() -> None:
    original = desired_snapshot()
    later_result = replace(original.result, created_at=NOW + timedelta(minutes=1))
    assert later_result.expected_result_fingerprint() == original.result_fingerprint
    changed = replace(
        original,
        result=later_result,
        created_at=NOW + timedelta(minutes=2),
    )
    assert changed.expected_semantic_identity_checksum() == original.semantic_identity_checksum
    changed = replace(
        changed,
        semantic_identity_checksum=changed.expected_semantic_identity_checksum(),
    )
    assert changed.expected_record_checksum() != original.record_checksum
    changed = replace(changed, record_checksum=changed.expected_record_checksum())
    changed.validate()


def test_real_result_change_changes_fingerprint_semantic_identity_and_record_checksum() -> None:
    original = desired_snapshot()
    changed_result = replace(
        original.result,
        upstream_action_boundary=("no_add", "research_only"),
    )
    changed_fingerprint = changed_result.expected_result_fingerprint()
    assert changed_fingerprint != original.result_fingerprint
    changed = replace(
        original,
        result=changed_result,
        result_fingerprint=changed_fingerprint,
    )
    changed_semantic = changed.expected_semantic_identity_checksum()
    assert changed_semantic != original.semantic_identity_checksum
    changed = replace(changed, semantic_identity_checksum=changed_semantic)
    assert changed.expected_record_checksum() != original.record_checksum
    changed = replace(changed, record_checksum=changed.expected_record_checksum())
    changed.validate()


def test_normalized_content_requires_nfkc_and_rejects_fullwidth_private_marker() -> None:
    fullwidth = "ａｃｃｅｓｓ　ｔｏｋｅｎ abc"
    with pytest.raises(ValueError, match="NFKC"):
        desired_announcement(
            normalized_content=fullwidth,
            normalized_content_bytes=len(fullwidth.encode()),
            normalized_content_sha256=hashlib.sha256(fullwidth.encode()).hexdigest(),
        ).validate()


@pytest.mark.parametrize(
    "ordinary_text",
    (
        "The form contains an email address field.",
        "The transfer may reference an account number field.",
        "The notice confirms that no secret is included.",
    ),
)
def test_ordinary_official_privacy_words_are_not_false_positives(ordinary_text: str) -> None:
    value = desired_announcement(
        normalized_content=ordinary_text,
        normalized_content_bytes=len(ordinary_text.encode()),
        normalized_content_sha256=hashlib.sha256(ordinary_text.encode()).hexdigest(),
    )
    value.validate()
    assert value.to_canonical_dict()["normalized_content"] == ordinary_text


def test_evidence_descriptor_tuples_are_bounded() -> None:
    descriptors = tuple(
        replace(evidence_item(), evidence_id=f"evidence_{index:03d}")
        for index in range(MAX_TUPLE_ITEMS + 1)
    )
    projection = replace(desired_projection(), evidence_descriptors=descriptors)
    with pytest.raises(ValueError, match="too many"):
        projection.validate()


def test_official_event_reference_is_the_only_result_event_identity() -> None:
    import kunjin.holding_review.models as review_models

    reference_type = review_models.OfficialEventEvidenceReference
    reference = reference_type(
        projection_id=7,
        projection_checksum="b" * 64,
        event_code=OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        triggered_review_code=TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
    )
    reference.validate()
    assert reference.to_canonical_dict()["projection_id"] == 7
    assert "official_event_evidence" in {field.name for field in fields(HoldingReviewInputs)}
    assert "official_event_codes" not in {field.name for field in fields(HoldingReviewInputs)}
    assert "official_event_evidence" in {field.name for field in fields(HoldingReviewResult)}
    assert "official_event_codes" not in {field.name for field in fields(HoldingReviewResult)}


def test_official_event_reference_rejects_wrong_mapping_type_order_and_duplicate() -> None:
    import kunjin.holding_review.models as review_models

    reference_type = review_models.OfficialEventEvidenceReference
    good = reference_type(
        projection_id=7,
        projection_checksum="b" * 64,
        event_code=OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        triggered_review_code=TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
    )
    with pytest.raises(ValueError, match="do not match"):
        replace(
            good,
            triggered_review_code=TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
        ).validate()
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(good, projection_checksum="B" * 64).validate()
    with pytest.raises(ValueError, match="positive exact integer"):
        replace(good, projection_id=True).validate()
    with pytest.raises(ValueError, match="exact OfficialEventEvidenceReference"):
        review_result(official_event_evidence=("official_event_projection_7",)).validate()  # type: ignore[arg-type]

    class ReferenceSubclass(OfficialEventEvidenceReference):
        pass

    with pytest.raises(ValueError, match="exact OfficialEventEvidenceReference"):
        ReferenceSubclass(**vars(good)).validate()
    later = replace(good, projection_id=8)
    with pytest.raises(ValueError, match="ordered by unique projection id"):
        review_result(official_event_evidence=(later, good)).validate()
    with pytest.raises(ValueError, match="ordered by unique projection id"):
        review_result(official_event_evidence=(good, good)).validate()


def test_continue_observing_rejects_authenticated_event_reference() -> None:
    reference = official_reference(
        event_code=OfficialEventCode.MANAGER_CHANGE_NOTICE,
        triggered_review_code=TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
    )
    with pytest.raises(ValueError, match="continue observing evidence is incomplete"):
        review_result(
            triggered_reviews=(TriggeredReviewCode.MANAGER_CHANGE_REVIEW,),
            official_event_evidence=(reference,),
            evidence_ids=("evidence_a", reference.support_evidence_id),
        ).validate()


@pytest.mark.parametrize(
    "synthetic_id",
    (
        "official_event_projection_99",
        "fund_liquidation_notice",
        "full_exit_feasibility_review",
    ),
)
def test_synthetic_or_code_only_event_evidence_id_is_invalid(synthetic_id: str) -> None:
    with pytest.raises(ValueError, match="support evidence ids"):
        review_result(evidence_ids=("evidence_a", synthetic_id)).validate()


def test_hard_event_reference_derives_trigger_hard_flag_and_support_id() -> None:
    reference = official_reference()
    incomplete = RedemptionEvidence(
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.MISSING,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.USABLE,
        RedemptionComponentState.MISSING,
    )
    result = review_result(
        action=ActionKind.FULL_EXIT,
        thesis_review_state=ThesisMatchState.NO_MATCHING_EVIDENCE,
        review_disposition=ReviewDisposition.EXIT_REVIEW,
        action_review_source_sufficiency=ActionReviewSourceSufficiency.INSUFFICIENT_DATA,
        triggered_reviews=(TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,),
        official_event_evidence=(reference,),
        hard_event_review=True,
        evidence_ids=("evidence_a", reference.support_evidence_id),
        redemption_feasibility=RedemptionFeasibility.INSUFFICIENT_DATA,
        redemption_evidence=incomplete,
    )
    result.validate()
    with pytest.raises(ValueError, match="support evidence ids"):
        replace(result, evidence_ids=("evidence_a",)).validate()


@pytest.mark.parametrize(
    "path",
    (
        "%3A",
        "%2B",
        "%E5%9F%BA%E9%87%91",
    ),
)
def test_canonical_uppercase_percent_paths_are_accepted(path: str) -> None:
    value = desired_announcement(
        canonical_announcement_url=f"https://www.fund001.com/fund/123456/{path}.html"
    )
    value.validate()


@pytest.mark.parametrize(
    "path",
    (
        "%3a",
        "%2b",
        "%e5%9f%ba%e9%87%91",
        "%3F",
        "%253F",
        "%00",
        "%7F",
        "%FF",
        "%E5%9F",
    ),
)
def test_noncanonical_or_unsafe_percent_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="canonical public HTTPS URL"):
        desired_announcement(
            canonical_announcement_url=(
                f"https://www.fund001.com/fund/123456/{path}.html"
            )
        ).validate()


@pytest.mark.parametrize(
    "private_content",
    (
        "/Users/owner/Documents/fund.txt",
        "/private/tmp/kunjin/input.txt",
        "/private/var/kunjin/input.txt",
        "file:///Users/owner/Documents/fund.txt",
        "Authorization: Bearer credentialvalue",
        "session_id=credentialvalue",
        "password=credentialvalue",
        "api_key=credentialvalue",
        "access_token=credentialvalue",
        "token=credentialvalue",
    ),
)
def test_announcement_content_rejects_owner_paths_and_shaped_credentials(
    private_content: str,
) -> None:
    with pytest.raises(ValueError, match="private|secret"):
        desired_announcement(
            normalized_content=private_content,
            normalized_content_bytes=len(private_content.encode()),
            normalized_content_sha256=hashlib.sha256(private_content.encode()).hexdigest(),
        ).validate()


@pytest.mark.parametrize(
    "ordinary_text",
    (
        "The notice discusses an exact amount without owner data.",
        "The form contains a local path description but no filesystem value.",
        "The policy uses the phrase private value as a general concept.",
    ),
)
def test_broad_privacy_words_remain_valid_public_content(ordinary_text: str) -> None:
    value = desired_announcement(
        normalized_content=ordinary_text,
        normalized_content_bytes=len(ordinary_text.encode()),
        normalized_content_sha256=hashlib.sha256(ordinary_text.encode()).hexdigest(),
    )
    value.validate()


@pytest.mark.parametrize(
    "private_content",
    (
        "/var/folders/ab/private-input.txt",
        "/tmp/private-input.txt",
        "/home/owner/private-input.txt",
        "file:///var/folders/ab/private-input.txt",
        "file:///tmp/private-input.txt",
        "file:///home/owner/private-input.txt",
        r"C:\Users\owner\private-input.txt",
        r"d:\users\owner\private-input.txt",
        "file:///C:/Users/owner/private-input.txt",
        "file:///d:/users/owner/private-input.txt",
        "credential = credentialvalue",
        "secret: credentialvalue",
        "client_secret = credentialvalue",
        "auth: credentialvalue",
        "authorization = credentialvalue",
        "session: credentialvalue",
        "session_id = credentialvalue",
        "password: credentialvalue",
        "api_key = credentialvalue",
        "access_token: credentialvalue",
        "token = credentialvalue",
        "cookie = credentialvalue",
        "Authorization: Basic credentialvalue",
        "Authorization : Digest credentialvalue",
        "Authorization=Bearer credentialvalue",
    ),
)
def test_sensitive_shape_validator_rejects_extended_paths_and_credentials(
    private_content: str,
) -> None:
    with pytest.raises(ValueError, match="private|secret"):
        desired_announcement(
            normalized_content=private_content,
            normalized_content_bytes=len(private_content.encode()),
            normalized_content_sha256=hashlib.sha256(private_content.encode()).hexdigest(),
        ).validate()


@pytest.mark.parametrize(
    "ordinary_text",
    (
        "The credential policy is described without a value.",
        "The notice confirms that no secret is present.",
        "The public session remains available.",
        "The local path field is discussed without a filesystem value.",
        "The document discusses an exact amount in general terms.",
        "Authorization schemes include Basic, Digest, and Bearer.",
        "Client secret handling is described conceptually.",
    ),
)
def test_sensitive_words_without_assignment_header_or_path_remain_valid(
    ordinary_text: str,
) -> None:
    value = desired_announcement(
        normalized_content=ordinary_text,
        normalized_content_bytes=len(ordinary_text.encode()),
        normalized_content_sha256=hashlib.sha256(ordinary_text.encode()).hexdigest(),
    )
    value.validate()


@pytest.mark.parametrize(
    "private_content",
    (
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
        "bearer a.b.c",
        "Bearer abcdefghijklmnop",
        "bearer opaque_token_1234567890",
        "file://localhost/Users/owner/private-input.txt",
        "FILE://public-host.example/private-input.txt",
        "FiLe:/tmp/private-input.txt",
        "file:relative/private-input.txt",
        "~/Documents/private-input.txt",
        "owner path is ~/private-input.txt",
    ),
)
def test_sensitive_shape_rejects_bare_tokens_file_schemes_and_tilde_paths(
    private_content: str,
) -> None:
    with pytest.raises(ValueError, match="private|secret"):
        desired_announcement(
            normalized_content=private_content,
            normalized_content_bytes=len(private_content.encode()),
            normalized_content_sha256=hashlib.sha256(private_content.encode()).hexdigest(),
        ).validate()


@pytest.mark.parametrize(
    "ordinary_text",
    (
        "bearer funds may have different fee schedules.",
        "The Bearer authentication scheme is discussed conceptually.",
        "A bearer token format is described without a token value.",
    ),
)
def test_bearer_words_without_token_shape_remain_valid(ordinary_text: str) -> None:
    value = desired_announcement(
        normalized_content=ordinary_text,
        normalized_content_bytes=len(ordinary_text.encode()),
        normalized_content_sha256=hashlib.sha256(ordinary_text.encode()).hexdigest(),
    )
    value.validate()
