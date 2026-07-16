from __future__ import annotations

import math
import re
import threading
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta
from typing import Optional, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    TRANSIENT_SOURCE_ERRORS,
    ForceReasonCode,
    FreshnessContext,
    RequestFieldResolution,
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceFieldPolicy,
    SourceFieldRef,
    SourceFieldState,
    SourceTier,
    StoredSourceAttempt,
    validate_aware_datetime,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_request_id,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore

INITIAL_COOLDOWN = timedelta(minutes=30)
_SUCCESS_OUTCOMES = frozenset(
    (SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT)
)
_EXHAUSTED_STATES = frozenset(
    (SourceFieldState.UNAVAILABLE, SourceFieldState.UNSUPPORTED)
)
_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")
_SOURCE_TIER_RANK = {
    SourceTier.TIER_1: 3,
    SourceTier.TIER_2: 2,
    SourceTier.PRIVATE_OBSERVATION: 1,
    SourceTier.USER_PROVIDED: 1,
}


@dataclass(frozen=True)
class ForceAuthorization:
    actor: str
    reason: ForceReasonCode
    request_id: str
    source_id: str
    field_id: str
    subject_key: str
    authorized_at: datetime
    deadline_at: datetime

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "force authorization")
        if self.actor != "local_owner":
            raise ValueError("force actor must be local_owner")
        if type(self.reason) is not ForceReasonCode:
            raise ValueError("force reason must be an exact ForceReasonCode")
        validate_request_id(self.request_id)
        validate_identifier(self.source_id, "source id")
        validate_identifier(self.field_id, "field id")
        if (
            type(self.subject_key) is not str
            or _SUBJECT_KEY_PATTERN.fullmatch(self.subject_key) is None
        ):
            raise ValueError("subject key must be fund: followed by exactly six digits")
        validate_aware_datetime(self.authorized_at, "force authorization time")
        validate_aware_datetime(self.deadline_at, "force authorization deadline")
        if self.authorized_at > self.deadline_at:
            raise ValueError("force authorization cannot follow its deadline")


class SourceHealthService:
    def __init__(
        self,
        audit_store: DecisionAuditStore,
        registry: Optional[SourceRegistryV1] = None,
    ) -> None:
        if type(audit_store) is not DecisionAuditStore:
            raise ValueError("audit store must be an exact DecisionAuditStore")
        if registry is None:
            registry = SourceRegistryV1()
        if type(registry) is not SourceRegistryV1:
            raise ValueError("registry must be an exact SourceRegistryV1")
        registry.validate()
        self.audit_store = audit_store
        self.registry = registry
        self._consumption_lock = threading.Lock()
        self._consumed_retries: set[tuple[str, str, str, str]] = set()
        self._consumed_forces: set[tuple[str, str, str, str]] = set()

    def source_field_state(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
        context: FreshnessContext,
    ) -> SourceFieldState:
        policy = self._field_policy(source_id, field_id)
        if type(context) is not FreshnessContext:
            raise ValueError("freshness context must be an exact FreshnessContext")
        context.validate()
        history = self.audit_store.source_attempt_history(
            source_id,
            field_id,
            subject_key,
        )
        return self._project_state(history, policy, context)

    def resolve_field(
        self,
        source_id: str,
        field_id: str,
        subject_key: str,
        context: FreshnessContext,
    ) -> RequestFieldResolution:
        primary_ref = SourceFieldRef(source_id, field_id)
        primary = self._field_policy(source_id, field_id)
        references = (primary_ref, *primary.acceptable_alternatives)
        primary_state = self.source_field_state(
            primary_ref.source_id,
            primary_ref.field_id,
            subject_key,
            context,
        )
        if primary_state is SourceFieldState.HEALTHY:
            return RequestFieldResolution.USABLE
        alternative_states = tuple(
            (
                self._field_policy(reference.source_id, reference.field_id),
                self.source_field_state(
                    reference.source_id,
                    reference.field_id,
                    subject_key,
                    context,
                ),
            )
            for reference in references[1:]
        )
        if any(
            state is SourceFieldState.HEALTHY
            and _SOURCE_TIER_RANK[policy.source_tier]
            >= _SOURCE_TIER_RANK[primary.source_tier]
            for policy, state in alternative_states
        ):
            return RequestFieldResolution.USABLE
        states = (primary_state, *(state for _, state in alternative_states))
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
        attempt: SourceAttempt,
        budget: RequestBudget,
        *,
        minimum_worker_seconds: float,
    ) -> bool:
        if type(attempt) is not SourceAttempt:
            raise ValueError("attempt must be an exact SourceAttempt")
        attempt.validate()
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        if (
            type(minimum_worker_seconds) is not float
            or not math.isfinite(minimum_worker_seconds)
            or minimum_worker_seconds <= 0.0
        ):
            raise ValueError("minimum worker seconds must be a positive finite exact float")
        if (
            attempt.attempt_number != 1
            or attempt.outcome is not SourceAttemptOutcome.TRANSIENT_FAILURE
            or attempt.error_code not in TRANSIENT_SOURCE_ERRORS
        ):
            return False
        try:
            enough_budget = budget.worker_seconds() >= minimum_worker_seconds
        except BudgetExpired:
            return False
        if not enough_budget:
            return False
        key = (
            budget.request_id,
            attempt.source_id,
            attempt.field_id,
            attempt.subject_key,
        )
        with self._consumption_lock:
            if key in self._consumed_retries:
                return False
            self._consumed_retries.add(key)
            return True

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
        authorized_at: datetime,
        force_reason: Optional[ForceReasonCode],
        *,
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
        validate_aware_datetime(authorized_at, "force authorization time")
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
        budget.require_publishable()
        if not budget.started_at <= authorized_at <= budget.deadline_at:
            raise ValueError("force authorization is outside the request lifetime")
        key = (budget.request_id, source_id, field_id, subject_key)
        with self._consumption_lock:
            if key in self._consumed_forces:
                return None
            self._consumed_forces.add(key)
        authorization = ForceAuthorization(
            actor="local_owner",
            reason=force_reason,
            request_id=budget.request_id,
            source_id=source_id,
            field_id=field_id,
            subject_key=subject_key,
            authorized_at=authorized_at,
            deadline_at=budget.deadline_at,
        )
        authorization.validate()
        return authorization

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
        history: Tuple[StoredSourceAttempt, ...],
        field_policy: SourceFieldPolicy,
        context: FreshnessContext,
    ) -> SourceFieldState:
        if type(history) is not tuple:
            raise ValueError("source attempt history must be an exact tuple")
        for record in history:
            if type(record) is not StoredSourceAttempt:
                raise ValueError("source attempt history contains an invalid record")
            record.validate()
        history = tuple(
            record for record in history if record.attempt.finished_at <= context.now
        )
        if not history:
            return SourceFieldState.NOT_CHECKED
        latest = history[0].attempt
        if latest.outcome is SourceAttemptOutcome.UNSUPPORTED:
            return SourceFieldState.UNSUPPORTED
        if latest.cooldown_until is not None and context.now < latest.cooldown_until:
            return SourceFieldState.COOLDOWN
        successful = tuple(
            record
            for record in history
            if record.attempt.outcome in _SUCCESS_OUTCOMES
        )
        if successful:
            successful_attempt = successful[0].attempt
            authenticated_context = dataclass_replace(
                context,
                data_request_id=successful[0].request_id,
                data_trading_day=successful_attempt.data_as_of.date(),
            )
        else:
            successful_attempt = None
            authenticated_context = context
        if successful_attempt is not None and field_policy.is_current(
            successful_attempt.data_as_of,
            authenticated_context,
        ):
            return SourceFieldState.HEALTHY
        if successful_attempt is not None and field_policy.is_usable(
            successful_attempt.data_as_of,
            authenticated_context,
        ):
            return SourceFieldState.DEGRADED
        return SourceFieldState.UNAVAILABLE
