from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    ActionKind,
    ForceAuthorization,
    ForceReasonCode,
    FreshnessContext,
    FreshnessKind,
    RequestFieldResolution,
    RequestMode,
    RiskEffect,
    SourceAttemptOutcome,
    SourceFieldHistory,
    SourceFieldPolicy,
    SourceFieldRef,
    SourceFieldState,
    SourceTier,
    SourceWorkAuthorization,
    StoredSourceAttempt,
    validate_aware_datetime,
    validate_exact_dataclass_state,
    validate_identifier,
)
from kunjin.decision.policy import EvidencePolicyV1, EvidenceRequirement
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore

INITIAL_COOLDOWN = timedelta(minutes=30)
_SUCCESS_OUTCOMES = frozenset(
    (SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT)
)
_EXHAUSTED_STATES = frozenset(
    (SourceFieldState.UNAVAILABLE, SourceFieldState.UNSUPPORTED)
)
_REQUEST_LIFECYCLE_OUTCOMES = frozenset(
    (
        SourceAttemptOutcome.CANCELLED,
        SourceAttemptOutcome.EXPIRED,
        SourceAttemptOutcome.SKIPPED_COOLDOWN,
    )
)
_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")
_ACTION_RISK = {
    ActionKind.FACT_RESEARCH: RiskEffect.INFORMATION,
    ActionKind.CONTINUE_HOLDING: RiskEffect.RISK_MAINTAINING,
    ActionKind.REDUCE_TO_CASH: RiskEffect.RISK_REDUCING,
    ActionKind.FULL_EXIT: RiskEffect.RISK_REDUCING,
    ActionKind.BUY_OR_ADD: RiskEffect.RISK_INCREASING,
    ActionKind.SWITCH_FUNDS: RiskEffect.RISK_INCREASING,
}
_REGISTRY_POLICY_REQUIREMENT_V1 = {
    "adjusted_return_series": "adjusted_return_correlation",
    "current_manager_team": "current_manager_team",
    "fees_share_class_relationship": "fees_share_class_relationship",
    "formal_nav": "formal_nav",
    "fund_manager_product_announcement": "fund_manager_product_announcement",
    "holdings_industries": "holdings_industries",
    "identity_active_status": "identity_active_status",
    "market_context": "news_media_context",
    "personal_position_observation": "personal_position",
    "transaction_availability_limits_cutoff": (
        "transaction_availability_limits_cutoff"
    ),
    "transaction_channel_observation": "transaction_availability_limits_cutoff",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ActionEvidenceRequirement:
    field_id: str
    action: ActionKind
    risk_effect: RiskEffect
    policy_requirement: EvidenceRequirement

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "action evidence requirement")
        if type(self) is not ActionEvidenceRequirement:
            raise ValueError("action evidence requirement subclasses are not accepted")
        validate_identifier(self.field_id, "field id")
        if type(self.action) is not ActionKind:
            raise ValueError("action must be an exact ActionKind")
        if type(self.risk_effect) is not RiskEffect:
            raise ValueError("risk effect must be an exact RiskEffect")
        if _ACTION_RISK[self.action] is not self.risk_effect:
            raise ValueError("action and risk effect do not match")
        if type(self.policy_requirement) is not EvidenceRequirement:
            raise ValueError("policy requirement must be an exact EvidenceRequirement")
        self.policy_requirement.validate()


@dataclass(frozen=True)
class ProjectedSourceField:
    state: SourceFieldState
    history: SourceFieldHistory

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "projected source field")
        if type(self.state) is not SourceFieldState:
            raise ValueError("projected state must be an exact SourceFieldState")
        if type(self.history) is not SourceFieldHistory:
            raise ValueError("projected history must be an exact SourceFieldHistory")
        self.history.validate()


@dataclass(frozen=True)
class SourceStatusSnapshot:
    projections: tuple[ProjectedSourceField, ...]
    resolutions: tuple[RequestFieldResolution, ...]

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source status snapshot")
        if type(self.projections) is not tuple or not self.projections:
            raise ValueError("source status projections must be a non-empty exact tuple")
        references = []
        for projection in self.projections:
            if type(projection) is not ProjectedSourceField:
                raise ValueError(
                    "source status projections must contain exact projected fields"
                )
            projection.validate()
            references.append(projection.history.reference)
        if len(references) != len(set(references)):
            raise ValueError("source status projections must have unique references")
        if type(self.resolutions) is not tuple:
            raise ValueError("source status resolutions must be an exact tuple")
        for resolution in self.resolutions:
            if type(resolution) is not RequestFieldResolution:
                raise ValueError(
                    "source status resolutions must contain exact resolution values"
                )


class SourceHealthService:
    def __init__(
        self,
        audit_store: DecisionAuditStore,
        registry: Optional[SourceRegistryV1] = None,
        policy: Optional[EvidencePolicyV1] = None,
        *,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(audit_store) is not DecisionAuditStore:
            raise ValueError("audit store must be an exact DecisionAuditStore")
        if registry is None:
            registry = SourceRegistryV1()
        if type(registry) is not SourceRegistryV1:
            raise ValueError("registry must be an exact SourceRegistryV1")
        registry.validate()
        if policy is None:
            policy = EvidencePolicyV1()
        if type(policy) is not EvidencePolicyV1:
            raise ValueError("policy must be an exact EvidencePolicyV1")
        policy.validate()
        if not callable(wall_clock):
            raise ValueError("wall clock must be callable")
        self.audit_store = audit_store
        self.registry = registry
        self.policy = policy
        self.wall_clock = wall_clock
        self._validate_requirement_bindings()

    def action_requirement(
        self,
        field_id: str,
        action: ActionKind,
        risk_effect: RiskEffect,
    ) -> ActionEvidenceRequirement:
        validate_identifier(field_id, "field id")
        requirement_id = _REGISTRY_POLICY_REQUIREMENT_V1.get(field_id)
        if requirement_id is None:
            raise ValueError("field has no executable EvidencePolicy V1 requirement")
        matches = tuple(
            requirement
            for requirement in self.policy.requirements
            if requirement.field_id == requirement_id
        )
        if len(matches) != 1:
            raise ValueError("field has no executable EvidencePolicy V1 requirement")
        requirement = ActionEvidenceRequirement(field_id, action, risk_effect, matches[0])
        requirement.validate()
        return requirement

    def source_field_state(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
        context: FreshnessContext,
        *,
        request_run_id: int,
        budget: RequestBudget,
    ) -> SourceFieldState:
        state, _history = self.source_field_state_and_history(
            source_id,
            field_id,
            subject_key,
            context,
            request_run_id=request_run_id,
            budget=budget,
        )
        return state

    def source_field_state_and_history(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
        context: FreshnessContext,
        *,
        request_run_id: int,
        budget: RequestBudget,
    ) -> tuple[SourceFieldState, SourceFieldHistory]:
        reference = SourceFieldRef(source_id, field_id)
        policy = self._field_policy(source_id, field_id)
        trusted_context = self._trusted_context(context, budget)
        histories = self.audit_store.authenticated_source_attempt_histories(
            request_run_id,
            budget,
            (reference,),
            subject_key,
            trusted_context.now,
        )
        filtered = SourceFieldHistory(
            reference,
            tuple(
                record
                for record in histories[0].attempts
                if record.attempt.finished_at <= trusted_context.now
            ),
        )
        filtered.validate()
        return self._project_state(filtered, policy, trusted_context), filtered

    def source_status_snapshot(
        self,
        subject_key: str,
        context: FreshnessContext,
        primary_references: tuple[SourceFieldRef, ...],
        requirements: tuple[ActionEvidenceRequirement, ...],
        *,
        request_run_id: int,
        budget: RequestBudget,
    ) -> SourceStatusSnapshot:
        if (
            type(primary_references) is not tuple
            or not primary_references
            or len(primary_references) > 128
            or len(primary_references) != len(requirements)
        ):
            raise ValueError("source status primary references are invalid")
        if type(requirements) is not tuple:
            raise ValueError("source status requirements must be an exact tuple")
        for reference, requirement in zip(primary_references, requirements):
            if type(reference) is not SourceFieldRef:
                raise ValueError("source status primaries must contain exact references")
            reference.validate()
            if type(requirement) is not ActionEvidenceRequirement:
                raise ValueError("source status requirements must be exact records")
            requirement.validate()
            if (
                requirement.field_id != reference.field_id
                or requirement.policy_requirement not in self.policy.requirements
                or requirement.policy_requirement.field_id
                != _REGISTRY_POLICY_REQUIREMENT_V1.get(reference.field_id)
            ):
                raise ValueError("source status requirement does not match its primary")
            self._field_policy(reference.source_id, reference.field_id)
        if len(primary_references) != len(set(primary_references)):
            raise ValueError("source status primaries must be unique")

        trusted_context = self._trusted_context(context, budget)
        references = tuple(
            SourceFieldRef(source.source_id, field.field_id)
            for source in self.registry.sources
            for field in source.fields
        )
        histories = self.audit_store.authenticated_source_attempt_histories(
            request_run_id,
            budget,
            references,
            subject_key,
            trusted_context.now,
        )
        filtered_histories = tuple(
            SourceFieldHistory(
                history.reference,
                tuple(
                    record
                    for record in history.attempts
                    if record.attempt.finished_at <= trusted_context.now
                    and record.attempt.outcome not in _REQUEST_LIFECYCLE_OUTCOMES
                ),
            )
            for history in histories
        )
        projections = tuple(
            ProjectedSourceField(
                self._project_state(
                    history,
                    self._field_policy(
                        history.reference.source_id,
                        history.reference.field_id,
                    ),
                    trusted_context,
                ),
                history,
            )
            for history in filtered_histories
        )
        projected_by_reference = {
            projection.history.reference: projection for projection in projections
        }
        resolutions = []
        for primary_ref, requirement in zip(primary_references, requirements):
            primary = self._field_policy(primary_ref.source_id, primary_ref.field_id)
            projected = tuple(
                (
                    self._field_policy(reference.source_id, reference.field_id),
                    projected_by_reference[reference].state,
                )
                for reference in (primary_ref, *primary.acceptable_alternatives)
            )
            resolutions.append(self._resolution_from_projected(projected, requirement))
        snapshot = SourceStatusSnapshot(projections, tuple(resolutions))
        snapshot.validate()
        return snapshot

    def resolve_field(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
        context: FreshnessContext,
        requirement: ActionEvidenceRequirement,
        *,
        request_run_id: int,
        budget: RequestBudget,
    ) -> RequestFieldResolution:
        if type(requirement) is not ActionEvidenceRequirement:
            raise ValueError("requirement must be an exact ActionEvidenceRequirement")
        requirement.validate()
        if requirement.policy_requirement not in self.policy.requirements:
            raise ValueError("requirement is not bound to the active evidence policy")
        if requirement.field_id != field_id or (
            requirement.policy_requirement.field_id
            != _REGISTRY_POLICY_REQUIREMENT_V1.get(field_id)
        ):
            raise ValueError("requirement does not match the requested field")
        primary_ref = SourceFieldRef(source_id, field_id)
        primary = self._field_policy(source_id, field_id)
        references = (primary_ref, *primary.acceptable_alternatives)
        trusted_context = self._trusted_context(context, budget)
        histories = self.audit_store.authenticated_source_attempt_histories(
            request_run_id,
            budget,
            references,
            subject_key,
            trusted_context.now,
        )
        projected = tuple(
            (
                self._field_policy(history.reference.source_id, history.reference.field_id),
                self._project_state(history, self._field_policy(
                    history.reference.source_id,
                    history.reference.field_id,
                ), trusted_context),
            )
            for history in histories
        )
        return self._resolution_from_projected(projected, requirement)

    @staticmethod
    def _resolution_from_projected(
        projected: tuple[tuple[SourceFieldPolicy, SourceFieldState], ...],
        requirement: ActionEvidenceRequirement,
    ) -> RequestFieldResolution:
        if requirement.risk_effect is RiskEffect.INFORMATION:
            if any(state is SourceFieldState.HEALTHY for _, state in projected):
                return RequestFieldResolution.USABLE
        elif any(
            policy.source_tier is SourceTier.TIER_1
            and state is SourceFieldState.HEALTHY
            for policy, state in projected
        ):
            return RequestFieldResolution.USABLE
        states = tuple(state for _, state in projected)
        if any(
            state in {SourceFieldState.HEALTHY, SourceFieldState.DEGRADED}
            for state in states
        ):
            return RequestFieldResolution.PARTIAL
        if states and all(state in _EXHAUSTED_STATES for state in states):
            return RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
        return RequestFieldResolution.PARTIAL

    def retry_allowed(
        self,
        parent: StoredSourceAttempt,
        budget: RequestBudget,
        *,
        request_run_id: int,
        minimum_worker_seconds: float,
    ) -> Optional[SourceWorkAuthorization]:
        if type(parent) is not StoredSourceAttempt:
            raise ValueError("retry parent must be an exact StoredSourceAttempt")
        parent.validate()
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        if (
            type(minimum_worker_seconds) is not float
            or not math.isfinite(minimum_worker_seconds)
            or minimum_worker_seconds <= 0.0
        ):
            raise ValueError("minimum worker seconds must be a positive finite exact float")
        if (
            parent.request_run_id != request_run_id
            or parent.request_id != budget.request_id
            or parent.attempt.attempt_number != 1
            or parent.attempt.outcome is not SourceAttemptOutcome.TRANSIENT_FAILURE
        ):
            return None
        try:
            enough_budget = budget.worker_seconds() >= minimum_worker_seconds
        except BudgetExpired:
            return None
        if not enough_budget:
            return None
        reserved_at = self._trusted_authorization_time(budget)
        return self.audit_store.reserve_retry(
            request_run_id,
            budget,
            parent,
            reserved_at,
            minimum_worker_seconds=minimum_worker_seconds,
        )

    @staticmethod
    def cooldown_until(finished_at: datetime) -> datetime:
        validate_aware_datetime(finished_at, "attempt finish")
        return finished_at + INITIAL_COOLDOWN

    def force_authorization(
        self,
        budget: RequestBudget,
        source_id: str,
        field_id: str,
        subject_key: str,
        force_reason: Optional[ForceReasonCode],
        *,
        request_run_id: int,
        attempt_number: int,
    ) -> Optional[ForceAuthorization]:
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        self._field_policy(source_id, field_id)
        if (
            type(subject_key) is not str
            or _SUBJECT_KEY_PATTERN.fullmatch(subject_key) is None
        ):
            raise ValueError("subject key must be fund: followed by exactly six digits")
        if type(attempt_number) is not int or attempt_number not in {1, 2}:
            raise ValueError("attempt number must be exactly 1 or 2")
        if force_reason is None:
            return None
        if type(force_reason) is not ForceReasonCode:
            raise ValueError("force reason must be an exact ForceReasonCode")
        if budget.mode is not RequestMode.DEEP:
            raise ValueError("force requires deep mode")
        if attempt_number != 1:
            raise ValueError("force is allowed only on the first attempt")
        authorized_at = self._trusted_authorization_time(budget)
        authorization = self.audit_store.reserve_force(
            request_run_id,
            budget,
            source_id,
            field_id,
            subject_key,
            authorized_at,
            force_reason,
        )
        if authorization is None:
            return None
        authorization.validate()
        return authorization

    def _validate_requirement_bindings(self) -> None:
        registry_fields = {
            field.field_id
            for source in self.registry.sources
            for field in source.fields
        }
        if registry_fields != set(_REGISTRY_POLICY_REQUIREMENT_V1):
            raise ValueError("registry fields differ from EvidencePolicy V1 bindings")
        policy_fields = tuple(requirement.field_id for requirement in self.policy.requirements)
        if len(policy_fields) != len(set(policy_fields)) or not set(
            _REGISTRY_POLICY_REQUIREMENT_V1.values()
        ).issubset(policy_fields):
            raise ValueError("EvidencePolicy V1 binding target is missing or ambiguous")

    def _trusted_authorization_time(
        self,
        budget: RequestBudget,
    ) -> datetime:
        budget.require_publishable()
        try:
            trusted_at = validate_aware_datetime(
                self.wall_clock(),
                "health wall clock",
            ).astimezone(timezone.utc)
        except Exception:
            raise ValueError("health wall clock failed") from None
        if not budget.started_at <= trusted_at <= budget.deadline_at:
            raise ValueError("health wall clock is outside the request lifetime")
        return trusted_at

    def _trusted_context(
        self,
        context: FreshnessContext,
        budget: RequestBudget,
    ) -> FreshnessContext:
        if type(context) is not FreshnessContext:
            raise ValueError("freshness context must be an exact FreshnessContext")
        context.validate()
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        budget.require_publishable()
        try:
            now = validate_aware_datetime(self.wall_clock(), "health wall clock").astimezone(
                timezone.utc
            )
        except Exception:
            raise ValueError("health wall clock failed") from None
        if not budget.started_at <= now <= budget.deadline_at:
            raise ValueError("health wall clock is outside the request lifetime")
        if context.data_request_id is not None or context.data_trading_day is not None:
            raise ValueError("data lineage fields are derived from authenticated attempts")
        return replace(context, now=now, request_id=budget.request_id)

    def _field_policy(self, source_id: str, field_id: str) -> SourceFieldPolicy:
        validate_identifier(source_id, "source id")
        validate_identifier(field_id, "field id")
        for source in self.registry.sources:
            if source.source_id == source_id:
                for field in source.fields:
                    if field.field_id == field_id:
                        return field
        raise ValueError("source field is not declared by the active registry")

    @staticmethod
    def _project_state(
        history: SourceFieldHistory,
        field_policy: SourceFieldPolicy,
        context: FreshnessContext,
    ) -> SourceFieldState:
        if type(history) is not SourceFieldHistory:
            raise ValueError("history must be an exact SourceFieldHistory")
        history.validate()
        records = tuple(
            record
            for record in history.attempts
            if record.attempt.finished_at <= context.now
            and record.attempt.outcome not in _REQUEST_LIFECYCLE_OUTCOMES
        )
        if not records:
            return SourceFieldState.NOT_CHECKED
        latest = records[0].attempt
        if latest.outcome is SourceAttemptOutcome.UNSUPPORTED:
            return SourceFieldState.UNSUPPORTED
        if latest.cooldown_until is not None and context.now < latest.cooldown_until:
            return SourceFieldState.COOLDOWN
        successful = tuple(
            record for record in records if record.attempt.outcome in _SUCCESS_OUTCOMES
        )
        if not successful:
            return SourceFieldState.UNAVAILABLE
        successful_record = successful[0]
        successful_attempt = successful_record.attempt
        data_request_id = (
            None
            if successful_attempt.outcome is SourceAttemptOutcome.CACHE_HIT
            else successful_record.request_id
        )
        authenticated_context = replace(
            context,
            data_request_id=data_request_id,
            data_trading_day=None,
        )
        can_be_current = field_policy.freshness.kind is not FreshnessKind.SAME_TRADING_DAY
        if can_be_current and field_policy.is_current(
            successful_attempt.data_as_of,
            authenticated_context,
        ):
            return SourceFieldState.HEALTHY
        if field_policy.is_usable(
            successful_attempt.data_as_of,
            authenticated_context,
        ):
            return SourceFieldState.DEGRADED
        return SourceFieldState.UNAVAILABLE
