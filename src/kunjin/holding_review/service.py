from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from kunjin.brief.store import BriefStore, BriefStoreError
from kunjin.decision.models import (
    ActionKind,
    RequestTerminalStatus,
    SourceAttemptOutcome,
    StoredSourceAttempt,
)
from kunjin.decision.store import DecisionAuditStoreError
from kunjin.holding_review.engine import HoldingReviewEngine
from kunjin.holding_review.models import (
    AdjudicationDecision,
    BindingState,
    ExitReason,
    FlowStatus,
    HoldingReviewInputs,
    HoldingReviewOutcome,
    HoldingReviewSnapshot,
    RedemptionComponentState,
    RedemptionEvidence,
    RemainderIntent,
    ThesisMatchProjectionState,
    ThesisMatchState,
    TransientHoldingReviewOutcome,
    UseOfProceeds,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1
from kunjin.holding_review.store import HoldingReviewStore, HoldingReviewStoreError
from kunjin.intelligence.models import IntelligenceWorkflow
from kunjin.intelligence.policy import IntelligencePolicyV1
from kunjin.intelligence.store import IntelligenceStore, IntelligenceStoreError
from kunjin.storage.repository import Repository

_SUPPORTED_ACTIONS = frozenset(
    (ActionKind.CONTINUE_HOLDING, ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT)
)
_FUND_INTELLIGENCE_REQUIRED_SOURCE_FIELDS = (
    ("eastmoney_market", "market_dimensions"),
    ("gov_cn_policy", "policy_events"),
    ("stcn_fund_news", "fund_media_events"),
)
_ADJUDICATED_STATES = {
    AdjudicationDecision.PRESENTED_MATCH_CONFIRMED: (
        ThesisMatchState.PRESENTED_MATCH_CONFIRMED
    ),
    AdjudicationDecision.PRESENTED_MATCH_REJECTED: (
        ThesisMatchState.PRESENTED_MATCH_REJECTED
    ),
    AdjudicationDecision.UNCERTAIN: ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
}
_PROJECTED_STATES = {
    ThesisMatchProjectionState.THESIS_MISSING: ThesisMatchState.THESIS_MISSING,
    ThesisMatchProjectionState.NO_MATCHING_EVIDENCE: (
        ThesisMatchState.NO_MATCHING_EVIDENCE
    ),
    ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH: (
        ThesisMatchState.MANUAL_REVIEW_PENDING
    ),
}
class HoldingReviewServiceError(RuntimeError):
    """A sanitized local held-review coordination failure."""


class HoldingReviewService:
    def __init__(
        self,
        repository: Repository,
        *,
        brief_store: Optional[BriefStore] = None,
        intelligence_store: Optional[IntelligenceStore] = None,
        holding_review_store: Optional[HoldingReviewStore] = None,
        policy: Optional[HeldFundManualReviewPolicyV1] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        self.repository = repository
        self.brief_store = brief_store or BriefStore(repository)
        self.intelligence_store = intelligence_store or IntelligenceStore(repository)
        self.holding_review_store = holding_review_store or HoldingReviewStore(repository)
        if (
            not isinstance(self.brief_store, BriefStore)
            or self.brief_store.repository is not repository
        ):
            raise ValueError("brief store must own the same Repository")
        if (
            not isinstance(self.intelligence_store, IntelligenceStore)
            or self.intelligence_store.repository is not repository
        ):
            raise ValueError("intelligence store must own the same Repository")
        if (
            not isinstance(self.holding_review_store, HoldingReviewStore)
            or self.holding_review_store.repository is not repository
        ):
            raise ValueError("holding review store must own the same Repository")
        self.policy = policy or HeldFundManualReviewPolicyV1()
        if type(self.policy) is not HeldFundManualReviewPolicyV1:
            raise ValueError("policy must be an exact HeldFundManualReviewPolicyV1")
        self.policy.validate()
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def review(
        self,
        fund_code: str,
        *,
        action: ActionKind,
        brief_request_run_id: int,
        intelligence_request_run_id: int,
        remainder_intent: RemainderIntent = RemainderIntent.UNKNOWN,
        exit_reason: ExitReason = ExitReason.UNKNOWN,
        use_of_proceeds: UseOfProceeds = UseOfProceeds.UNKNOWN,
    ) -> HoldingReviewOutcome | TransientHoldingReviewOutcome:
        _fund_code(fund_code)
        if type(action) is not ActionKind or action not in _SUPPORTED_ACTIONS:
            raise ValueError("holding review action is unsupported")
        _positive_id(brief_request_run_id, "brief request run id")
        _positive_id(intelligence_request_run_id, "intelligence request run id")
        if type(remainder_intent) is not RemainderIntent:
            raise ValueError("remainder intent must be exact")
        if type(exit_reason) is not ExitReason:
            raise ValueError("exit reason must be exact")
        if type(use_of_proceeds) is not UseOfProceeds:
            raise ValueError("use of proceeds must be exact")

        missing = []
        brief = None
        intelligence = None
        try:
            brief = self.brief_store.authenticated_snapshot_by_request_run_id(
                brief_request_run_id
            )
        except BriefStoreError as exc:
            if self._snapshot_exists("fund_brief_snapshots", brief_request_run_id):
                raise HoldingReviewServiceError(
                    "brief snapshot authentication failed"
                ) from exc
            missing.append("brief_snapshot_missing")
        try:
            intelligence = self.intelligence_store.authenticated_snapshot_by_request_run_id(
                intelligence_request_run_id
            )
        except IntelligenceStoreError as exc:
            if self._snapshot_exists("intelligence_snapshots", intelligence_request_run_id):
                raise HoldingReviewServiceError(
                    "intelligence snapshot authentication failed"
                ) from exc
            missing.append("intelligence_snapshot_missing")
        if missing:
            outcome = TransientHoldingReviewOutcome(
                flow_status=FlowStatus.PARTIAL,
                review_snapshot=None,
                missing_snapshot_codes=tuple(sorted(missing)),
            )
            outcome.validate()
            return outcome
        assert brief is not None and intelligence is not None

        try:
            now = _utc_now(self.clock())
            brief_terminal = self.intelligence_store.authenticated_terminal_request(
                brief_request_run_id
            )
            intelligence_terminal = self.intelligence_store.authenticated_terminal_request(
                intelligence_request_run_id
            )
            self._authenticate_request_bindings(
                fund_code,
                action,
                brief.snapshot,
                intelligence.snapshot,
            )
            self._authenticate_window(
                now,
                brief_terminal.finished_at,
                intelligence_terminal.finished_at,
            )
            if brief.snapshot.position_present is not True:
                outcome = TransientHoldingReviewOutcome(
                    flow_status=FlowStatus.PARTIAL,
                    review_snapshot=None,
                    missing_snapshot_codes=("current_position_missing",),
                )
                outcome.validate()
                return outcome

            projection = self.holding_review_store.latest_thesis_match(
                fund_code, intelligence_request_run_id
            )
            if projection is None:
                raise HoldingReviewServiceError("thesis projection missing")
            if (
                projection.value.intelligence_snapshot_id != intelligence.id
                or projection.value.intelligence_snapshot_checksum
                != intelligence.result_checksum
            ):
                raise HoldingReviewServiceError("thesis projection binding failed")
            adjudication = self.holding_review_store.current_adjudication(projection.id)
            thesis_state = (
                _PROJECTED_STATES[projection.value.projection_state]
                if adjudication is None
                else _ADJUDICATED_STATES[adjudication.value.decision]
            )

            previous = self.holding_review_store.latest_comparable_review(
                fund_code,
                action,
                projection.value.thesis_fingerprint,
                self.policy.checksum(),
            )
            previous_items = (
                None
                if previous is None
                else self.holding_review_store.authenticated_review_evidence_descriptors(
                    previous.id
                )
            )
            engine = (
                HoldingReviewEngine(policy=self.policy)
                if previous is None
                else HoldingReviewEngine(
                    previous_review=previous.value,
                    previous_review_id=previous.id,
                    previous_evidence_items=previous_items,
                    policy=self.policy,
                )
            )

            interpretation = next(
                (
                    item
                    for item in brief.snapshot.interpretations
                    if item.action_id == action.value
                ),
                None,
            )
            if interpretation is None:
                raise HoldingReviewServiceError("review action binding failed")
            brief_omitted = _canonical_union(
                brief_terminal.omitted_work,
                brief.snapshot.missing_fields,
                brief.snapshot.conflicts,
                interpretation.missing_fields,
                ("official_deep_confirmation_deferred",),
            )
            upstream_boundary = _canonical_union(
                brief.snapshot.blocking_codes,
                brief.snapshot.constraints,
                brief.snapshot.affected_action_abstentions,
                interpretation.blocking_codes,
                interpretation.unavailable_actions,
                brief.snapshot.conflicts,
            )
            attempts = self.intelligence_store.authenticated_terminal_source_attempts(
                intelligence_request_run_id
            )
            schedule_complete, schedule_omitted, degraded = _fund_intelligence_schedule(
                attempts,
                fund_code,
                IntelligencePolicyV1(),
            )
            intelligence_omitted = _canonical_union(
                intelligence_terminal.omitted_work,
                intelligence.snapshot.missing_evidence,
                intelligence.snapshot.conflicts,
                schedule_omitted,
            )
            inputs = HoldingReviewInputs(
                fund_code=fund_code,
                action=action,
                brief_request_run_id=brief_request_run_id,
                intelligence_request_run_id=intelligence_request_run_id,
                thesis_review_state=thesis_state,
                review_evidence_items=projection.value.evidence_descriptors,
                official_event_evidence=(),
                omitted_work=brief_omitted,
                official_negative_check_complete=False,
                intelligence_schedule_complete=(
                    intelligence_terminal.status is RequestTerminalStatus.COMPLETE
                    and not intelligence_terminal.omitted_work
                    and schedule_complete
                ),
                intelligence_omitted_work=intelligence_omitted,
                intelligence_degraded_sources=degraded,
                upstream_action_boundary=upstream_boundary,
                redemption_evidence=_redemption_evidence(
                    action, brief.snapshot.position_present
                ),
                remainder_intent=remainder_intent,
                exit_reason=exit_reason,
                use_of_proceeds=use_of_proceeds,
                previous_review_id=None if previous is None else previous.id,
                thesis_fingerprint=projection.value.thesis_fingerprint,
                policy_version=self.policy.version,
                policy_checksum=self.policy.checksum(),
                now=now,
            )
            result = engine.evaluate(inputs)
            snapshot = HoldingReviewSnapshot(
                fund_code=fund_code,
                action=action,
                brief_request_run_id=brief_request_run_id,
                brief_snapshot_id=brief.id,
                brief_snapshot_checksum=brief.result_checksum,
                intelligence_request_run_id=intelligence_request_run_id,
                intelligence_snapshot_id=intelligence.id,
                intelligence_snapshot_checksum=intelligence.result_checksum,
                thesis_match_projection_id=projection.id,
                thesis_match_projection_checksum=projection.value.record_checksum,
                active_thesis_state=(
                    BindingState.MISSING
                    if projection.value.thesis_id is None
                    else BindingState.PRESENT
                ),
                active_thesis_id=projection.value.thesis_id,
                active_thesis_fingerprint=projection.value.thesis_fingerprint,
                adjudication_state=(
                    BindingState.MISSING
                    if adjudication is None
                    else BindingState.PRESENT
                ),
                adjudication_id=None if adjudication is None else adjudication.id,
                adjudication_checksum=(
                    None if adjudication is None else adjudication.value.record_checksum
                ),
                previous_review_id=None if previous is None else previous.id,
                result=result,
                result_fingerprint=result.expected_result_fingerprint(),
                policy_version=self.policy.version,
                policy_checksum=self.policy.checksum(),
                created_at=now,
                semantic_identity_checksum="0" * 64,
                record_checksum="0" * 64,
            )
            snapshot = replace(
                snapshot,
                semantic_identity_checksum=snapshot.expected_semantic_identity_checksum(),
            )
            snapshot = replace(
                snapshot, record_checksum=snapshot.expected_record_checksum()
            )
            stored = self.holding_review_store.publish_review(snapshot)
            outcome = HoldingReviewOutcome(stored.value.result.flow_status, stored.value)
            outcome.validate()
            return outcome
        except HoldingReviewServiceError:
            raise
        except (
            BriefStoreError,
            DecisionAuditStoreError,
            IntelligenceStoreError,
            HoldingReviewStoreError,
        ) as exc:
            raise HoldingReviewServiceError("holding review authentication failed") from exc
        except (KeyError, TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise HoldingReviewServiceError("holding review coordination failed") from exc

    def _snapshot_exists(self, table: str, request_run_id: int) -> bool:
        if table not in {"fund_brief_snapshots", "intelligence_snapshots"}:
            raise ValueError("snapshot table is unsupported")
        try:
            with self.repository.connect() as connection:
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE request_run_id=?",  # noqa: S608
                    (request_run_id,),
                ).fetchone()
            return row is not None
        except (sqlite3.DatabaseError, OSError, TypeError, ValueError):
            raise HoldingReviewServiceError("snapshot existence check failed") from None

    def _authenticate_request_bindings(
        self, fund_code, action, brief_snapshot, intelligence_snapshot
    ) -> None:
        if (
            brief_snapshot.fund_code != fund_code
            or intelligence_snapshot.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE
            or intelligence_snapshot.subject_fund_code != fund_code
        ):
            raise HoldingReviewServiceError("review subject binding failed")
        if action.value not in brief_snapshot.action_ids:
            raise HoldingReviewServiceError("review action binding failed")

    def _authenticate_window(
        self, now: datetime, brief_finished_at: datetime, intelligence_finished_at: datetime
    ) -> None:
        window = timedelta(seconds=self.policy.orchestration_window_seconds)
        if (
            brief_finished_at > now
            or intelligence_finished_at > now
            or now - brief_finished_at > window
            or now - intelligence_finished_at > window
            or abs(brief_finished_at - intelligence_finished_at) > window
        ):
            raise HoldingReviewServiceError("review orchestration window failed")


def _canonical_union(*values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set().union(*values)))


def _fund_intelligence_schedule(
    attempts: tuple[StoredSourceAttempt, ...],
    fund_code: str,
    policy: IntelligencePolicyV1,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    if type(attempts) is not tuple or any(
        type(item) is not StoredSourceAttempt for item in attempts
    ):
        raise ValueError("source attempts must be an exact stored-attempt tuple")
    _fund_code(fund_code)
    if type(policy) is not IntelligencePolicyV1:
        raise ValueError("intelligence policy must be exact")
    policy.validate()
    enabled = {
        source_id
        for source_id, _tier, _entry, state, _reason in policy.source_registry
        if state == "enabled"
    }
    required = dict(_FUND_INTELLIGENCE_REQUIRED_SOURCE_FIELDS)
    if not set(required).issubset(enabled):
        raise HoldingReviewServiceError("fund intelligence schedule policy failed")

    by_source: dict[str, list[StoredSourceAttempt]] = {}
    for stored in attempts:
        stored.validate()
        by_source.setdefault(stored.attempt.source_id, []).append(stored)
    omitted = set()
    degraded = set()
    for source_id, field_id in required.items():
        source_attempts = by_source.get(source_id, [])
        if not source_attempts:
            degraded.add(source_id)
            omitted.add(f"{source_id}_attempt_missing")
            continue
        if len(source_attempts) != 1:
            degraded.add(source_id)
            omitted.add(f"{source_id}_attempt_ambiguous")
            continue
        attempt = source_attempts[0].attempt
        if (
            attempt.subject_key != f"fund:{fund_code}"
            or attempt.field_id != field_id
        ):
            degraded.add(source_id)
            omitted.add(f"{source_id}_attempt_binding_invalid")
            continue
        if attempt.outcome not in {
            SourceAttemptOutcome.SUCCESS,
            SourceAttemptOutcome.CACHE_HIT,
        }:
            degraded.add(source_id)
            omitted.add(f"{source_id}_{attempt.outcome.value}")
    return not degraded, tuple(sorted(omitted)), tuple(sorted(degraded))


def _redemption_evidence(
    action: ActionKind, position_present: Optional[bool]
) -> RedemptionEvidence:
    missing = RedemptionComponentState.MISSING
    if action is ActionKind.CONTINUE_HOLDING:
        return RedemptionEvidence(missing, missing, missing, missing, missing, missing, missing)
    current = RedemptionComponentState.USABLE if position_present is True else missing
    return RedemptionEvidence(current, missing, missing, missing, missing, missing, missing)


def _fund_code(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 6
        or not value.isascii()
        or not value.isdigit()
        or value == "000000"
    ):
        raise ValueError("fund code must be a non-reserved six-digit ASCII code")
    return value


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _utc_now(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError("review time must be an exact UTC datetime")
    return value
