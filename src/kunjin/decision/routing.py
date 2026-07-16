from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ActionState,
    DecisionRoute,
    RequestMode,
    RiskEffect,
    WorkflowLevel,
    validate_identifier,
    validate_request_id,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1

_ActionRule = Tuple[RiskEffect, Tuple[str, ...]]

ACTION_RULES: Mapping[ActionKind, _ActionRule] = MappingProxyType(
    {
        ActionKind.FACT_RESEARCH: (RiskEffect.INFORMATION, ()),
        ActionKind.CONTINUE_HOLDING: (
            RiskEffect.RISK_MAINTAINING,
            ("phase_b_context", "phase_e_policy"),
        ),
        ActionKind.REDUCE_TO_CASH: (
            RiskEffect.RISK_REDUCING,
            ("position", "fees", "settlement", "minimum_remainder"),
        ),
        ActionKind.FULL_EXIT: (
            RiskEffect.RISK_REDUCING,
            ("exit_reason", "position", "fees", "settlement", "use_of_proceeds"),
        ),
        ActionKind.BUY_OR_ADD: (
            RiskEffect.RISK_INCREASING,
            ("phase_b", "phase_c", "d1", "d2", "d3", "post_trade"),
        ),
    }
)

_CURRENT_PHASE_B_STATUSES = frozenset(
    ("blocked", "constrained", "ready_for_allocation")
)


class _PhaseBContext:
    __slots__ = ("constraints", "hard_blocks", "status")

    def __init__(
        self,
        status: Optional[str],
        hard_blocks: Tuple[str, ...] = (),
        constraints: Tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.hard_blocks = hard_blocks
        self.constraints = constraints

    @property
    def current(self) -> bool:
        return self.status in _CURRENT_PHASE_B_STATUSES


class ActionRouter:
    """Build deterministic action routes without executing transactions."""

    def route(
        self,
        *,
        request_id: str,
        mode: RequestMode,
        actions: Tuple[ActionKind, ...],
        suitability_status: object,
    ) -> DecisionRoute:
        validate_request_id(request_id)
        if type(mode) is not RequestMode:
            raise ValueError("mode must be an exact RequestMode")
        validate_actions(actions)
        phase_b = _phase_b_context(suitability_status)
        routed = []
        missing_fields = []
        opposing_evidence = []

        for action in actions:
            if action is ActionKind.SWITCH_FUNDS:
                candidates = (
                    ("switch_reduce", ActionKind.REDUCE_TO_CASH),
                    ("switch_buy", ActionKind.BUY_OR_ADD),
                )
            else:
                candidates = ((action.value, action),)
            for action_id, routed_action in candidates:
                route, missing, opposing = self._route_action(
                    action_id,
                    routed_action,
                    phase_b,
                )
                routed.append(route)
                _extend_unique(missing_fields, missing)
                _extend_unique(opposing_evidence, opposing)

        policy = EvidencePolicyV1()
        registry = SourceRegistryV1()
        result = DecisionRoute(
            request_id=request_id,
            mode=mode,
            workflow_level=(
                WorkflowLevel.RAPID_EVIDENCE
                if mode is RequestMode.RAPID
                else WorkflowLevel.DECISION_EVIDENCE
            ),
            actions=tuple(routed),
            conclusion_evidence=(),
            opposing_evidence=tuple(opposing_evidence),
            missing_fields=tuple(missing_fields),
            policy_version=policy.version,
            policy_checksum=policy.checksum(),
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
        )
        result.validate()
        return result

    @staticmethod
    def _route_action(
        action_id: str,
        action: ActionKind,
        phase_b: _PhaseBContext,
    ) -> Tuple[ActionRoute, Tuple[str, ...], Tuple[str, ...]]:
        risk_effect, required_gates = ACTION_RULES[action]
        blocking_codes: Tuple[str, ...]
        missing_fields: Tuple[str, ...]
        opposing: Tuple[str, ...]
        maturity = ActionMaturity.EXPERIMENTAL_SHADOW

        if action is ActionKind.FACT_RESEARCH:
            blocking_codes = ()
            missing_fields = ()
            opposing = ()
            minimum_state = ActionState.RESEARCH_ONLY
            maturity = ActionMaturity.MATURE
        elif action is ActionKind.CONTINUE_HOLDING:
            missing_fields = ("phase_e_policy",)
            if phase_b.status == "blocked":
                blocking_codes = (
                    "phase_b_blocked",
                    *phase_b.hard_blocks,
                    "phase_e_policy_missing",
                )
                opposing = (
                    "continued_exposure_is_not_risk_free",
                    "financial_safety_conflicts_with_continued_exposure",
                    *phase_b.constraints,
                )
                minimum_state = ActionState.NO_ADD
                maturity = ActionMaturity.MATURE
            elif phase_b.current:
                blocking_codes = ("phase_e_policy_missing",)
                opposing = (
                    "continued_exposure_is_not_risk_free",
                    *phase_b.constraints,
                )
                minimum_state = ActionState.EXPERIMENTAL_SHADOW
            else:
                blocking_codes = (
                    "financial_safety_not_current",
                    "phase_e_policy_missing",
                )
                missing_fields = ("phase_b", "phase_e_policy")
                opposing = ("continued_exposure_is_not_risk_free",)
                minimum_state = ActionState.EXPERIMENTAL_SHADOW
        elif action is ActionKind.REDUCE_TO_CASH:
            missing_fields = required_gates
            blocking_codes = tuple(f"{field}_missing" for field in missing_fields)
            opposing = ("reduction_may_create_transaction_costs",)
            minimum_state = ActionState.EXPERIMENTAL_SHADOW
        elif action is ActionKind.FULL_EXIT:
            missing_fields = required_gates
            blocking_codes = tuple(f"{field}_missing" for field in missing_fields)
            opposing = ("full_exit_may_change_portfolio_balance",)
            minimum_state = ActionState.EXPERIMENTAL_SHADOW
        else:
            missing_fields_list = ["phase_c", "d1", "d2", "d3", "post_trade"]
            blocking_list = []
            if phase_b.status == "blocked":
                blocking_list.extend(("phase_b_blocked", *phase_b.hard_blocks))
            elif not phase_b.current:
                blocking_list.append("financial_safety_not_current")
                missing_fields_list.insert(0, "phase_b")
            blocking_list.extend(f"{field}_missing" for field in missing_fields_list)
            blocking_codes = tuple(blocking_list)
            missing_fields = tuple(missing_fields_list)
            opposing = ("new_money_increases_risk", *phase_b.constraints)
            minimum_state = ActionState.RESEARCH_ONLY

        route = ActionRoute(
            action_id=action_id,
            action=action,
            risk_effect=risk_effect,
            required_gates=required_gates,
            blocking_codes=blocking_codes,
            research_available=True,
            exact_amount_available=False,
            minimum_state=minimum_state,
            action_maturity=maturity,
        )
        route.validate()
        return route, missing_fields, opposing


def validate_actions(actions: object) -> Tuple[ActionKind, ...]:
    if type(actions) is not tuple or not actions:
        raise ValueError("actions must be a non-empty exact tuple")
    if len(actions) > 128:
        raise ValueError("actions must be bounded")
    for action in actions:
        if type(action) is not ActionKind:
            raise ValueError("actions must contain exact ActionKind values")
    if len(actions) != len(set(actions)):
        raise ValueError("actions must not contain duplicates")
    return actions


def _phase_b_context(value: object) -> _PhaseBContext:
    if type(value) is not dict:
        return _PhaseBContext(None)
    state = value.get("state")
    freshness = value.get("freshness")
    status = value.get("status")
    if state != "fresh" or freshness != "fresh" or status not in _CURRENT_PHASE_B_STATUSES:
        return _PhaseBContext(None)
    try:
        hard_blocks = _safe_codes(value.get("hard_blocks", ()), "hard blocks")
        constraints = _safe_codes(value.get("constraints", ()), "constraints")
    except ValueError:
        return _PhaseBContext(None)
    if status == "blocked" and not hard_blocks:
        return _PhaseBContext(None)
    if status != "blocked" and hard_blocks:
        return _PhaseBContext(None)
    return _PhaseBContext(status, hard_blocks, constraints)


def _safe_codes(value: object, name: str) -> Tuple[str, ...]:
    if type(value) not in (list, tuple) or len(value) > 128:
        raise ValueError(f"{name} must be a bounded list or tuple")
    result = tuple(value)
    for item in result:
        validate_identifier(item, name)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _extend_unique(target: list, values: Tuple[str, ...]) -> None:
    for value in values:
        if value not in target:
            target.append(value)
