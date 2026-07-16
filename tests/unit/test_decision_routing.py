from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionState,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    WorkflowLevel,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.routing import ACTION_RULES, ActionRouter
from kunjin.decision.service import DecisionRoutingService
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
REQUEST_ID = "1234567890abcdef1234567890abcdef"


class StubSuitabilityService:
    def __init__(self, status: object = None, error: BaseException | None = None) -> None:
        self.value = status
        self.error = error
        self.calls = 0

    def status(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


def _status(
    *,
    state: str = "fresh",
    status: str = "ready_for_allocation",
    hard_blocks: tuple[str, ...] = (),
    constraints: tuple[str, ...] = (),
) -> dict[str, object]:
    if state != "fresh":
        return {
            "state": state,
            "freshness": state,
            "status": status,
            "hard_blocks": list(hard_blocks),
            "constraints": list(constraints),
            "capability": "research_only",
        }
    return {
        "state": "fresh",
        "freshness": "fresh",
        "assessment_id": 7,
        "profile_version_id": 3,
        "policy_version": "1",
        "status": status,
        "hard_blocks": list(hard_blocks),
        "constraints": list(constraints),
        "assessed_at": NOW.isoformat(),
        "valid_until": datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc).isoformat(),
        "capability": "research_only",
    }


def _route(
    actions: tuple[ActionKind, ...],
    suitability_status: object,
    *,
    mode: RequestMode = RequestMode.RAPID,
):
    return ActionRouter().route(
        request_id=REQUEST_ID,
        mode=mode,
        actions=actions,
        suitability_status=suitability_status,
    )


def _budget(monotonic=lambda: 10.0) -> RequestBudget:
    return RequestBudget.create(
        RequestMode.RAPID,
        request_id=REQUEST_ID,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )


def test_action_rules_are_immutable_and_cover_only_canonical_outputs() -> None:
    assert tuple(ACTION_RULES) == (
        ActionKind.FACT_RESEARCH,
        ActionKind.CONTINUE_HOLDING,
        ActionKind.REDUCE_TO_CASH,
        ActionKind.FULL_EXIT,
        ActionKind.BUY_OR_ADD,
    )
    assert ACTION_RULES[ActionKind.BUY_OR_ADD] == (
        RiskEffect.RISK_INCREASING,
        ("phase_b", "phase_c", "d1", "d2", "d3", "post_trade"),
    )
    with pytest.raises(TypeError):
        ACTION_RULES[ActionKind.FACT_RESEARCH] = (  # type: ignore[index]
            RiskEffect.INFORMATION,
            (),
        )


@pytest.mark.parametrize(
    "suitability_status",
    (
        _status(status="blocked", hard_blocks=("emergency_reserve_shortfall",)),
        _status(state="missing"),
        _status(state="stale", status="blocked", hard_blocks=("profile_stale",)),
    ),
)
def test_fact_research_survives_every_phase_b_state(suitability_status) -> None:
    route = _route((ActionKind.FACT_RESEARCH,), suitability_status)
    action = route.actions[0]

    assert action.action is ActionKind.FACT_RESEARCH
    assert action.risk_effect is RiskEffect.INFORMATION
    assert action.blocking_codes == ()
    assert action.research_available is True
    assert action.minimum_state is ActionState.RESEARCH_ONLY
    assert action.action_maturity is ActionMaturity.MATURE
    assert route.missing_fields == ()


def test_fresh_blocked_holding_is_deterministic_mature_no_add() -> None:
    route = _route(
        (ActionKind.CONTINUE_HOLDING,),
        _status(
            status="blocked",
            hard_blocks=("emergency_reserve_shortfall", "high_interest_debt"),
        ),
    )
    action = route.actions[0]

    assert action.minimum_state is ActionState.NO_ADD
    assert action.action_maturity is ActionMaturity.MATURE
    assert action.research_available is True
    assert action.exact_amount_available is False
    assert action.blocking_codes == (
        "phase_b_blocked",
        "emergency_reserve_shortfall",
        "high_interest_debt",
        "phase_e_policy_missing",
    )
    assert "financial_safety_conflicts_with_continued_exposure" in (
        route.opposing_evidence
    )


@pytest.mark.parametrize("state", ("missing", "stale"))
def test_noncurrent_phase_b_never_produces_unqualified_hold(state: str) -> None:
    route = _route(
        (ActionKind.CONTINUE_HOLDING,),
        _status(state=state, status="blocked", hard_blocks=("reserve_shortfall",)),
    )
    action = route.actions[0]

    assert action.minimum_state is ActionState.EXPERIMENTAL_SHADOW
    assert action.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert action.blocking_codes == (
        "financial_safety_not_current",
        "phase_e_policy_missing",
    )
    assert "reserve_shortfall" not in action.blocking_codes


@pytest.mark.parametrize(
    ("status", "constraints"),
    (
        ("constrained", ("monthly_ceiling_constrained",)),
        ("ready_for_allocation", ()),
    ),
)
def test_nonblocked_phase_b_cannot_mature_hold_before_phase_e(
    status: str,
    constraints: tuple[str, ...],
) -> None:
    route = _route(
        (ActionKind.CONTINUE_HOLDING,),
        _status(status=status, constraints=constraints),
    )
    action = route.actions[0]

    assert action.minimum_state is ActionState.EXPERIMENTAL_SHADOW
    assert action.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert action.blocking_codes == ("phase_e_policy_missing",)
    assert route.opposing_evidence == (
        "continued_exposure_is_not_risk_free",
        *constraints,
    )


def test_reduce_and_exit_research_continue_under_block_but_amounts_do_not() -> None:
    route = _route(
        (ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT),
        _status(status="blocked", hard_blocks=("reserve_shortfall",)),
    )
    reduce, exit_ = route.actions

    assert reduce.risk_effect is RiskEffect.RISK_REDUCING
    assert reduce.research_available is True
    assert reduce.exact_amount_available is False
    assert reduce.minimum_state is ActionState.EXPERIMENTAL_SHADOW
    assert reduce.blocking_codes == (
        "position_missing",
        "fees_missing",
        "settlement_missing",
        "minimum_remainder_missing",
    )
    assert exit_.blocking_codes == (
        "exit_reason_missing",
        "position_missing",
        "fees_missing",
        "settlement_missing",
        "use_of_proceeds_missing",
    )
    assert "financial_safety_not_current" not in reduce.blocking_codes
    assert "phase_b_blocked" not in exit_.blocking_codes


@pytest.mark.parametrize(
    ("suitability_status", "phase_b_code"),
    (
        (
            _status(
                status="blocked",
                hard_blocks=("emergency_reserve_shortfall",),
            ),
            "phase_b_blocked",
        ),
        (
            _status(
                status="constrained",
                constraints=("monthly_ceiling_constrained",),
            ),
            None,
        ),
        (_status(status="ready_for_allocation"), None),
        (_status(state="missing"), "financial_safety_not_current"),
        (_status(state="stale"), "financial_safety_not_current"),
    ),
)
def test_buy_or_add_is_blocked_for_every_phase_b_state(
    suitability_status,
    phase_b_code: str | None,
) -> None:
    action = _route((ActionKind.BUY_OR_ADD,), suitability_status).actions[0]

    assert action.research_available is True
    assert action.exact_amount_available is False
    assert action.minimum_state is ActionState.RESEARCH_ONLY
    assert action.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    for code in ("d2_missing", "d3_missing", "post_trade_missing"):
        assert code in action.blocking_codes
    if phase_b_code is None:
        assert "phase_b_blocked" not in action.blocking_codes
        assert "financial_safety_not_current" not in action.blocking_codes
    else:
        assert phase_b_code in action.blocking_codes


def test_switch_is_deterministically_split_into_reduce_and_buy_legs() -> None:
    route = _route(
        (ActionKind.FACT_RESEARCH, ActionKind.SWITCH_FUNDS),
        _status(status="blocked", hard_blocks=("emergency_reserve_shortfall",)),
    )

    assert tuple(action.action_id for action in route.actions) == (
        "fact_research",
        "switch_reduce",
        "switch_buy",
    )
    assert route.actions[1].action is ActionKind.REDUCE_TO_CASH
    assert route.actions[1].risk_effect is RiskEffect.RISK_REDUCING
    assert route.actions[2].action is ActionKind.BUY_OR_ADD
    assert route.actions[2].risk_effect is RiskEffect.RISK_INCREASING
    assert "phase_b_blocked" not in route.actions[1].blocking_codes
    assert "phase_b_blocked" in route.actions[2].blocking_codes


def test_workflow_level_is_request_mode_not_confidence() -> None:
    rapid = _route((ActionKind.FACT_RESEARCH,), _status())
    deep = _route(
        (ActionKind.FACT_RESEARCH,),
        _status(),
        mode=RequestMode.DEEP,
    )

    assert rapid.workflow_level is WorkflowLevel.RAPID_EVIDENCE
    assert deep.workflow_level is WorkflowLevel.DECISION_EVIDENCE
    assert "confidence" not in rapid.to_canonical_dict()
    assert rapid.conclusion_evidence == ()


def test_router_rejects_duplicate_empty_and_noncanonical_actions() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _route((), _status())
    with pytest.raises(ValueError, match="duplicates"):
        _route(
            (ActionKind.FACT_RESEARCH, ActionKind.FACT_RESEARCH),
            _status(),
        )
    with pytest.raises(ValueError, match="exact ActionKind"):
        _route(("fact_research",), _status())  # type: ignore[arg-type]


def test_service_reads_only_safe_status_metadata_and_persists_one_snapshot(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    unsafe_status = _status(
        status="blocked",
        hard_blocks=("reserve_shortfall",),
        constraints=("equity_cap_binding",),
    )
    unsafe_status["exact_amounts"] = {"monthly_net_income": "999999"}
    unsafe_status["local_path"] = "/private/owner/profile"
    suitability = StubSuitabilityService(unsafe_status)
    service = DecisionRoutingService(
        suitability,
        store,
        now=lambda: NOW,
    )

    snapshot = service.route(
        _budget(),
        (ActionKind.FACT_RESEARCH, ActionKind.CONTINUE_HOLDING),
    )
    encoded = snapshot.route.canonical_json().decode("ascii")

    assert suitability.calls == 1
    assert snapshot.route.policy_version == EvidencePolicyV1().version
    assert snapshot.route.policy_checksum == EvidencePolicyV1().checksum()
    assert snapshot.route.registry_version == SourceRegistryV1().version
    assert snapshot.route.registry_checksum == SourceRegistryV1().checksum()
    assert "999999" not in encoded
    assert "monthly_net_income" not in encoded
    assert "/private/owner/profile" not in encoded
    with repository.connect() as connection:
        run = connection.execute(
            "SELECT request_id, mode, status FROM request_runs"
        ).fetchall()
        count = connection.execute(
            "SELECT count(*) FROM decision_snapshots"
        ).fetchone()[0]
    assert [tuple(item) for item in run] == [(REQUEST_ID, "rapid", "complete")]
    assert count == 1


def test_service_does_not_query_phase_b_for_information_or_risk_reduction(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    suitability = StubSuitabilityService(error=RuntimeError("private failure"))
    service = DecisionRoutingService(
        suitability,
        DecisionAuditStore(repository),
        now=lambda: NOW,
    )

    snapshot = service.route(
        _budget(),
        (ActionKind.FACT_RESEARCH, ActionKind.REDUCE_TO_CASH),
    )

    assert suitability.calls == 0
    assert snapshot.route.actions[0].research_available is True
    assert snapshot.route.actions[1].research_available is True


def test_phase_b_failure_closes_direction_without_leaking_exception(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    suitability = StubSuitabilityService(
        error=RuntimeError("token=/private/owner/secret exact=999999")
    )
    service = DecisionRoutingService(
        suitability,
        DecisionAuditStore(repository),
        now=lambda: NOW,
    )

    snapshot = service.route(_budget(), (ActionKind.BUY_OR_ADD,))
    encoded = json.dumps(snapshot.route.to_canonical_dict())

    assert snapshot.route.actions[0].blocking_codes[0] == (
        "financial_safety_not_current"
    )
    assert "private" not in encoded
    assert "999999" not in encoded


def test_malformed_fresh_phase_b_metadata_fails_closed() -> None:
    malformed = _status(status="blocked", hard_blocks=())
    malformed["hard_blocks"] = ["monthly net income"]

    action = _route((ActionKind.CONTINUE_HOLDING,), malformed).actions[0]

    assert action.minimum_state is ActionState.EXPERIMENTAL_SHADOW
    assert action.blocking_codes == (
        "financial_safety_not_current",
        "phase_e_policy_missing",
    )


def test_forged_phase_b_identifiers_never_enter_route_or_audit_codes() -> None:
    forged = _status(
        status="ready_for_allocation",
        constraints=("token_abcdef123456", "salary_999999"),
    )

    route = _route((ActionKind.CONTINUE_HOLDING,), forged)
    encoded = route.canonical_json().decode("ascii")

    assert route.actions[0].blocking_codes == (
        "financial_safety_not_current",
        "phase_e_policy_missing",
    )
    assert "token_abcdef123456" not in encoded
    assert "salary_999999" not in encoded


def test_forged_phase_b_identifiers_are_not_persisted_by_service(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    forged = _status(
        status="ready_for_allocation",
        constraints=("token_abcdef123456", "salary_999999"),
    )
    service = DecisionRoutingService(
        StubSuitabilityService(forged),
        DecisionAuditStore(repository),
        now=lambda: NOW,
    )

    snapshot = service.route(_budget(), (ActionKind.CONTINUE_HOLDING,))

    assert snapshot.route.actions[0].blocking_codes == (
        "financial_safety_not_current",
        "phase_e_policy_missing",
    )
    with repository.connect() as connection:
        stored = connection.execute(
            "SELECT canonical_route_json FROM decision_snapshots"
        ).fetchone()[0]
    assert "token_abcdef123456" not in stored
    assert "salary_999999" not in stored


def test_service_clock_failure_is_sanitized_and_request_is_finalized(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    service = DecisionRoutingService(
        StubSuitabilityService(_status()),
        DecisionAuditStore(repository),
        now=lambda: datetime(2026, 7, 16, 8, 0),
    )

    with pytest.raises(RuntimeError, match="decision routing failed") as raised:
        service.route(_budget(), (ActionKind.FACT_RESEARCH,))

    assert "timezone" not in str(raised.value)
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT status FROM request_runs"
        ).fetchone()[0] == "failed"


def test_keyboard_interrupt_cancels_and_finalizes_the_request(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    budget = _budget()
    service = DecisionRoutingService(
        StubSuitabilityService(error=KeyboardInterrupt()),
        DecisionAuditStore(repository),
        now=lambda: NOW,
    )

    with pytest.raises(KeyboardInterrupt):
        service.route(budget, (ActionKind.CONTINUE_HOLDING,))

    assert budget.cancelled is True
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT status FROM request_runs"
        ).fetchone()[0] == "cancelled"


def test_snapshot_budget_expiry_rolls_back_before_commit(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    ticks = iter((10.0, 10.0, 100.0))
    budget = _budget(monotonic=lambda: next(ticks))
    request_run_id = store.begin_request(budget)
    route = _route((ActionKind.FACT_RESEARCH,), None)

    with pytest.raises(BudgetExpired):
        store.save_decision_snapshot(
            request_run_id,
            route,
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
            budget=budget,
        )
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots"
        ).fetchone()[0] == 0


def test_expiry_between_snapshot_and_terminal_state_leaves_no_readable_snapshot(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    ticks = iter((10.0, 10.0, 10.0, 10.0, 10.0, 100.0))
    budget = _budget(monotonic=lambda: next(ticks))
    service = DecisionRoutingService(
        StubSuitabilityService(_status()),
        store,
        now=lambda: NOW,
    )

    with pytest.raises(BudgetExpired):
        service.route(budget, (ActionKind.FACT_RESEARCH,))

    with repository.connect() as connection:
        run = connection.execute(
            "SELECT status FROM request_runs"
        ).fetchone()
        snapshot_count = connection.execute(
            "SELECT count(*) FROM decision_snapshots"
        ).fetchone()[0]
    assert run["status"] == "expired"
    assert snapshot_count == 0
    with pytest.raises(DecisionAuditStoreError, match="does not exist"):
        store._load_decision_snapshot(1)


def test_success_finalization_budget_expiry_rolls_back_terminal_state(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    ticks = iter((10.0, 10.0, 100.0))
    budget = _budget(monotonic=lambda: next(ticks))
    request_run_id = store.begin_request(budget)

    with pytest.raises(BudgetExpired):
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.COMPLETE,
            NOW,
            (),
            budget=budget,
        )
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT status FROM request_runs WHERE id = ?", (request_run_id,)
        ).fetchone()[0] == "running"


def test_decision_route_rejects_workflow_level_that_disagrees_with_mode() -> None:
    route = _route((ActionKind.FACT_RESEARCH,), None)

    with pytest.raises(ValueError, match="workflow level"):
        replace(route, workflow_level=WorkflowLevel.DECISION_EVIDENCE).validate()


def test_store_rejects_workflow_level_that_disagrees_with_request_mode(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    request_run_id = store.begin_request(_budget())
    bad_route = replace(
        _route((ActionKind.FACT_RESEARCH,), None),
        workflow_level=WorkflowLevel.DECISION_EVIDENCE,
    )

    with pytest.raises(ValueError, match="workflow level"):
        store.save_decision_snapshot(
            request_run_id,
            bad_route,
            EvidencePolicyV1(),
            SourceRegistryV1(),
            NOW,
        )
    with repository.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM decision_snapshots"
        ).fetchone()[0] == 0
