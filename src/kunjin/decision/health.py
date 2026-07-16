from __future__ import annotations

import math
from dataclasses import dataclass
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
    StoredSourceAttempt,
    validate_aware_datetime,
    validate_exact_dataclass_state,
    validate_identifier,
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


@dataclass(frozen=True)
class ForceAuthorization:
    actor: str
    reason: ForceReasonCode

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "force authorization")
        if self.actor != "local_owner":
            raise ValueError("force actor must be local_owner")
        if type(self.reason) is not ForceReasonCode:
            raise ValueError("force reason must be an exact ForceReasonCode")


class SourceHealthService:
    def __init__(
        self,
        audit_store: DecisionAuditStore,
        registry: Optional[SourceRegistryV1] = None,
    ) -> None:
        if not isinstance(audit_store, DecisionAuditStore):
            raise ValueError("audit store must be a DecisionAuditStore")
        if registry is None:
            registry = SourceRegistryV1()
        if type(registry) is not SourceRegistryV1:
            raise ValueError("registry must be an exact SourceRegistryV1")
        registry.validate()
        self.audit_store = audit_store
        self.registry = registry

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
        states = tuple(
            self.source_field_state(
                reference.source_id,
                reference.field_id,
                subject_key,
                context,
            )
            for reference in references
        )
        if SourceFieldState.HEALTHY in states:
            return RequestFieldResolution.USABLE
        if SourceFieldState.DEGRADED in states:
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
            return budget.worker_seconds() >= minimum_worker_seconds
        except BudgetExpired:
            return False

    @staticmethod
    def cooldown_until(finished_at: datetime) -> datetime:
        validate_aware_datetime(finished_at, "attempt finish")
        return finished_at + INITIAL_COOLDOWN

    @staticmethod
    def force_authorization(
        mode: RequestMode,
        force_reason: Optional[ForceReasonCode],
        *,
        attempt_number: int,
    ) -> Optional[ForceAuthorization]:
        if type(mode) is not RequestMode:
            raise ValueError("mode must be an exact RequestMode")
        if type(attempt_number) is not int or attempt_number not in {1, 2}:
            raise ValueError("attempt number must be exactly 1 or 2")
        if force_reason is None:
            return None
        if type(force_reason) is not ForceReasonCode:
            raise ValueError("force reason must be an exact ForceReasonCode")
        if mode is not RequestMode.DEEP:
            raise ValueError("force requires deep mode")
        if attempt_number != 1:
            return None
        authorization = ForceAuthorization(
            actor="local_owner",
            reason=force_reason,
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
        if not history:
            return SourceFieldState.NOT_CHECKED
        latest = history[0].attempt
        if latest.outcome is SourceAttemptOutcome.UNSUPPORTED:
            return SourceFieldState.UNSUPPORTED
        if latest.cooldown_until is not None and context.now < latest.cooldown_until:
            return SourceFieldState.COOLDOWN
        successful = tuple(
            record.attempt
            for record in history
            if record.attempt.outcome in _SUCCESS_OUTCOMES
        )
        if successful and field_policy.is_current(successful[0].data_as_of, context):
            return SourceFieldState.HEALTHY
        if successful and field_policy.is_usable(successful[0].data_as_of, context):
            return SourceFieldState.DEGRADED
        return SourceFieldState.UNAVAILABLE
