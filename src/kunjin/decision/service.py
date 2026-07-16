from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    ActionKind,
    RequestTerminalStatus,
    validate_aware_datetime,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.routing import ActionRouter, validate_actions
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, StoredDecisionSnapshot


class DecisionRoutingError(RuntimeError):
    """A sanitized failure to create an authenticated decision route."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecisionRoutingService:
    def __init__(
        self,
        suitability_service: object,
        store: DecisionAuditStore,
        *,
        router: Optional[ActionRouter] = None,
        policy: Optional[EvidencePolicyV1] = None,
        registry: Optional[SourceRegistryV1] = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not callable(getattr(suitability_service, "status", None)):
            raise ValueError("suitability service must expose status")
        if type(store) is not DecisionAuditStore:
            raise ValueError("store must be an exact DecisionAuditStore")
        if router is None:
            router = ActionRouter()
        if type(router) is not ActionRouter:
            raise ValueError("router must be an exact ActionRouter")
        if policy is None:
            policy = EvidencePolicyV1()
        if type(policy) is not EvidencePolicyV1:
            raise ValueError("policy must be an exact EvidencePolicyV1")
        if registry is None:
            registry = SourceRegistryV1()
        if type(registry) is not SourceRegistryV1:
            raise ValueError("registry must be an exact SourceRegistryV1")
        if not callable(now):
            raise ValueError("now must be callable")
        policy.validate()
        registry.validate()
        self._suitability_service = suitability_service
        self._store = store
        self._router = router
        self._policy = policy
        self._registry = registry
        self._now = now

    def route(
        self,
        budget: RequestBudget,
        actions: Tuple[ActionKind, ...],
    ) -> StoredDecisionSnapshot:
        if type(budget) is not RequestBudget:
            raise ValueError("budget must be an exact RequestBudget")
        validate_actions(actions)
        budget.require_publishable()
        request_run_id = self._store.begin_request(budget)
        created_at = None
        try:
            suitability_status = self._safe_suitability_status(actions)
            budget.require_publishable()
            route = self._router.route(
                request_id=budget.request_id,
                mode=budget.mode,
                actions=actions,
                suitability_status=suitability_status,
            )
            created_at = self._current_time()
            snapshot = self._store.save_decision_snapshot(
                request_run_id,
                route,
                self._policy,
                self._registry,
                created_at,
                budget=budget,
            )
            budget.require_publishable()
            finished_at = self._current_time_not_before(created_at)
            if finished_at > budget.deadline_at:
                raise BudgetExpired("request audit deadline reached")
            self._store.finalize_request(
                request_run_id,
                RequestTerminalStatus.COMPLETE,
                finished_at,
                (),
                budget=budget,
            )
            return snapshot
        except BudgetExpired:
            self._finalize_failure(
                request_run_id,
                budget,
                RequestTerminalStatus.CANCELLED
                if budget.cancelled
                else RequestTerminalStatus.EXPIRED,
                created_at,
            )
            raise
        except KeyboardInterrupt:
            budget.cancel("owner_cancelled")
            self._finalize_failure(
                request_run_id,
                budget,
                RequestTerminalStatus.CANCELLED,
                created_at,
            )
            raise
        except Exception:
            self._finalize_failure(
                request_run_id,
                budget,
                RequestTerminalStatus.FAILED,
                created_at,
            )
            raise DecisionRoutingError("decision routing failed") from None

    def _safe_suitability_status(
        self,
        actions: Tuple[ActionKind, ...],
    ) -> object:
        if not any(
            action
            in (
                ActionKind.CONTINUE_HOLDING,
                ActionKind.BUY_OR_ADD,
                ActionKind.SWITCH_FUNDS,
            )
            for action in actions
        ):
            return None
        try:
            return self._suitability_service.status()
        except Exception:
            return None

    def _current_time(self) -> datetime:
        try:
            return validate_aware_datetime(self._now(), "current time").astimezone(
                timezone.utc
            )
        except Exception:
            raise DecisionRoutingError("decision routing clock failed") from None

    def _current_time_not_before(self, lower_bound: datetime) -> datetime:
        current = self._current_time()
        return max(current, lower_bound)

    def _finalize_failure(
        self,
        request_run_id: int,
        budget: RequestBudget,
        status: RequestTerminalStatus,
        lower_bound: Optional[datetime],
    ) -> None:
        try:
            current = self._current_time()
        except DecisionRoutingError:
            current = (
                budget.deadline_at
                if status is RequestTerminalStatus.EXPIRED
                else budget.started_at
            )
        if status is RequestTerminalStatus.EXPIRED:
            current = budget.deadline_at
        if lower_bound is not None:
            current = max(current, lower_bound)
        current = min(max(current, budget.started_at), budget.deadline_at)
        try:
            self._store.finalize_request(request_run_id, status, current, ())
        except Exception:
            raise DecisionRoutingError("decision request finalization failed") from None
