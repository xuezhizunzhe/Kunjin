from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kunjin.brief.d2 import PortfolioEvidenceBinding
from kunjin.brief.models import HeldFundBriefOutcome
from kunjin.brief.nav import NavSyncResult
from kunjin.brief.portfolio import PortfolioObservationResult
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import (
    ActionKind,
    RequestMode,
    RequestTerminalStatus,
    SourceAttempt,
    SourceAttemptOutcome,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.models import (
    DocumentKind,
    FundBenchmark,
    FundIdentity,
    FundShareClass,
    SourceDocument,
)
from kunjin.funds.service import FundDisclosureSyncResult, SourceRequestContext
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import InvestmentThesis, StoredPosition
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
FUND_CODE = "519755"


class _Suitability:
    def status(self) -> dict[str, object]:
        return {
            "state": "fresh",
            "freshness": "fresh",
            "assessment_id": 7,
            "profile_version_id": 3,
            "policy_version": "1",
            "status": "ready_for_allocation",
            "hard_blocks": [],
            "constraints": [],
            "assessed_at": NOW.isoformat(),
            "valid_until": (NOW + timedelta(days=1)).isoformat(),
            "capability": "research_only",
        }


@dataclass
class _Script:
    calls: list[tuple[str, int, int, RequestBudget]]
    fail_at: str | None = None
    cancel_at: str | None = None
    interrupt_at: str | None = None
    cancel_reason: str = "owner_cancelled"
    system_exit_at: str | None = None

    def call(self, name: str, context: SourceRequestContext) -> None:
        self.calls.append((name, context.request_run_id, id(context), context.budget))
        if self.interrupt_at == name:
            raise KeyboardInterrupt
        if self.system_exit_at == name:
            raise SystemExit(2)
        if self.cancel_at == name:
            context.budget.cancel(self.cancel_reason)
        if self.fail_at == name:
            raise RuntimeError("scripted source failure")


class _Disclosure:
    def __init__(self, store: FundDisclosureStore, script: _Script) -> None:
        self.store = store
        self.script = script

    def sync_sections(
        self,
        fund_code: str,
        section_names: tuple[str, ...],
        *,
        request_context: SourceRequestContext,
    ) -> FundDisclosureSyncResult:
        names = {
            ("basic_profile",): "identity",
            ("manager_history", "fee_schedule"): "manager_fee",
            ("announcements",): "announcements",
        }
        self.script.call(names[section_names], request_context)
        return FundDisclosureSyncResult(fund_code, {}, ())

    def sync_holdings(
        self,
        fund_code: str,
        *,
        request_context: SourceRequestContext,
    ) -> FundDisclosureSyncResult:
        self.script.call("holdings", request_context)
        return FundDisclosureSyncResult(fund_code, {}, ())


class _Portfolio:
    def __init__(
        self,
        repository: Repository,
        script: _Script,
        *,
        success: bool = False,
    ) -> None:
        self.repository = repository
        self.script = script
        self.success = success

    def sync(
        self,
        fund_code: str,
        context: SourceRequestContext,
    ) -> PortfolioObservationResult:
        self.script.call("portfolio", context)
        source_attempt_id = 1
        if self.success:
            registry = SourceRegistryV1()
            source_attempt_id = context.audit_store.record_source_attempt(
                context.request_run_id,
                SourceAttempt(
                    "yangjibao_portfolio_observation",
                    "personal_position_observation",
                    f"fund:{fund_code}",
                    1,
                    SourceAttemptOutcome.SUCCESS,
                    context.budget.started_at,
                    context.budget.started_at,
                    context.budget.started_at,
                    None,
                    None,
                    None,
                    None,
                    registry.version,
                    registry.checksum(),
                    0,
                ),
            )
        binding = PortfolioEvidenceBinding(
            positions=(),
            snapshot_complete=self.success,
            observation_version=(
                f"source_attempt_{source_attempt_id}" if self.success else "portfolio_unavailable"
            ),
            observed_at=context.budget.started_at,
            source_state="same_request_success" if self.success else "unbound",
            request_id=context.budget.request_id if self.success else None,
            request_mode=context.budget.mode if self.success else None,
            request_started_at=context.budget.started_at if self.success else None,
            request_deadline_at=context.budget.deadline_at if self.success else None,
        )
        binding.validate()
        return PortfolioObservationResult(
            fund_code,
            "success" if self.success else "unavailable",
            0,
            0,
            False if self.success else None,
            context.budget.started_at.isoformat() if self.success else None,
            source_attempt_id,
            binding,
            None if self.success else "authentication_required",
        )


class _Nav:
    def __init__(
        self,
        repository: Repository,
        script: _Script,
        *,
        success: bool = False,
    ) -> None:
        self.repository = repository
        self.script = script
        self.success = success

    def sync(
        self,
        fund_code: str,
        context: SourceRequestContext,
        *,
        latest_expected_data_as_of: datetime | None = None,
    ) -> NavSyncResult:
        self.script.call("nav", context)
        if self.success:
            return NavSyncResult(
                fund_code,
                "success",
                "success",
                "success",
                0,
                None,
                (),
            )
        return NavSyncResult(
            fund_code,
            "unavailable",
            "unavailable",
            "unavailable",
            0,
            None,
            ("formal_nav", "adjusted_return_series"),
        )

    def validated_adjusted_series(
        self,
        fund_code: str,
        context: SourceRequestContext,
        *,
        latest_expected_data_as_of: datetime | None = None,
    ) -> None:
        return None


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    disclosure = FundDisclosureStore(repository)
    source = SourceDocument(
        id=None,
        fund_code=FUND_CODE,
        document_kind=DocumentKind.BASIC_PROFILE,
        title="基金基本资料",
        url=f"https://fundf10.eastmoney.com/jbgk_{FUND_CODE}.html",
        source_name="eastmoney_f10",
        source_tier=2,
        publisher="东方财富",
        published_at=NOW - timedelta(days=1),
        retrieved_at=NOW,
        checksum="a" * 64,
    )
    disclosure.publish_section(
        FUND_CODE,
        DocumentKind.BASIC_PROFILE,
        source,
        (
            FundIdentity(
                FUND_CODE,
                "测试基金A",
                "active",
                "混合型",
                date(2020, 1, 1),
                "测试基金公司",
                None,
            ),
            FundShareClass(FUND_CODE, FUND_CODE, "A", "测试基金A", None),
            FundBenchmark(FUND_CODE, "沪深300指数收益率", None, None, None),
        ),
        "success",
    )
    return repository


def _service(
    tmp_path: Path,
    *,
    fail_at: str | None = None,
    cancel_at: str | None = None,
    interrupt_at: str | None = None,
    cancel_reason: str = "owner_cancelled",
    system_exit_at: str | None = None,
    monotonic=None,
    all_sources_complete: bool = False,
):
    from kunjin.brief.service import HeldFundBriefService

    repository = _repository(tmp_path)
    audit_store = DecisionAuditStore(repository)
    script = _Script(
        [],
        fail_at,
        cancel_at,
        interrupt_at,
        cancel_reason,
        system_exit_at,
    )
    service = HeldFundBriefService(
        repository=repository,
        suitability_service=_Suitability(),
        disclosure_service=_Disclosure(FundDisclosureStore(repository), script),
        portfolio_service=_Portfolio(repository, script, success=all_sources_complete),
        nav_service=_Nav(repository, script, success=all_sources_complete),
        audit_store=audit_store,
        now=lambda: NOW,
        monotonic=(lambda: 1.0) if monotonic is None else monotonic,
        announcement_content_loader=(
            (lambda _bundle, _context: ()) if all_sources_complete else None
        ),
    )
    return repository, audit_store, script, service


@pytest.mark.parametrize("mode", (RequestMode.RAPID, RequestMode.DEEP))
@pytest.mark.parametrize(
    "action",
    (
        ActionKind.CONTINUE_HOLDING,
        ActionKind.REDUCE_TO_CASH,
        ActionKind.FULL_EXIT,
        ActionKind.SWITCH_FUNDS,
    ),
)
def test_one_budget_request_and_context_bind_fixed_priority(
    tmp_path: Path,
    mode: RequestMode,
    action: ActionKind,
) -> None:
    repository, _audit, script, service = _service(tmp_path)

    report = service.brief(FUND_CODE, action=action, mode=mode)

    assert [item[0] for item in script.calls] == [
        "identity",
        "portfolio",
        "nav",
        "manager_fee",
        "holdings",
        "announcements",
    ]
    assert len({item[1] for item in script.calls}) == 1
    assert len({item[2] for item in script.calls}) == 1
    assert len({id(item[3]) for item in script.calls}) == 1
    assert report.snapshot.mode is mode
    expected_actions = (
        ("fact_research", "switch_reduce", "switch_buy")
        if action is ActionKind.SWITCH_FUNDS
        else ("fact_research", action.value)
    )
    assert report.snapshot.action_ids == expected_actions
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["id"] == report.snapshot.request_run_id
    assert run["request_id"] == script.calls[0][3].request_id
    assert run["status"] == "partial"


def test_source_exception_is_omitted_and_later_public_work_continues(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(tmp_path, fail_at="portfolio")

    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert [item[0] for item in script.calls] == [
        "identity",
        "portfolio",
        "nav",
        "manager_fee",
        "holdings",
        "announcements",
    ]
    assert report.snapshot.portfolio_evidence_state == "unknown"
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "personal_position_observation" in run["omitted_work_json"]


def test_expiry_stops_new_scheduling_and_publishes_no_snapshot(tmp_path: Path) -> None:
    readings = iter((1.0, 1.0, 1.0, 91.0, 91.0, 91.0))
    repository, _audit, script, service = _service(
        tmp_path,
        monotonic=lambda: next(readings, 91.0),
    )

    with pytest.raises(BudgetExpired):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert [item[0] for item in script.calls] == ["identity"]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[0] == 0
    assert run["status"] == "expired"


def test_complete_requires_empty_omitted_work(tmp_path: Path) -> None:
    repository, _audit, _script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )

    service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "complete"
    assert run["omitted_work_json"] == "[]"


def test_brief_outcome_returns_authenticated_complete_terminal_contract(tmp_path: Path) -> None:
    _repository_value, _audit, script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )

    outcome = service.brief_outcome(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    outcome.validate()
    assert outcome.terminal_status is RequestTerminalStatus.COMPLETE
    assert outcome.omitted_work == ()
    assert len(script.calls) == 6


def test_brief_outcome_returns_authenticated_partial_terminal_contract(tmp_path: Path) -> None:
    _repository_value, _audit, script, service = _service(tmp_path)

    outcome = service.brief_outcome(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    outcome.validate()
    assert outcome.terminal_status is RequestTerminalStatus.PARTIAL
    assert outcome.omitted_work
    assert len(script.calls) == 6


def test_brief_marks_unreadable_historical_comparison_as_omitted(tmp_path: Path) -> None:
    _repository_value, _audit, _script, service = _service(tmp_path)

    with patch.object(
        service._brief_store,
        "latest_history_comparable",
        return_value=False,
    ):
        outcome = service.brief_outcome(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert "historical_brief_comparison_unavailable" in outcome.omitted_work


def test_compatible_brief_executes_once_and_returns_outcome_report(tmp_path: Path) -> None:
    _repository_value, _audit, script, service = _service(tmp_path)
    original = service.brief_outcome
    outcomes = []

    def counted(*args, **kwargs):
        outcome = original(*args, **kwargs)
        outcomes.append(outcome)
        return outcome

    service.brief_outcome = counted
    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert len(outcomes) == 1
    assert report is outcomes[0].report
    assert len(script.calls) == 6


def test_budget_cancellation_stops_after_current_source(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(
        tmp_path,
        cancel_at="portfolio",
    )

    with pytest.raises(BudgetExpired):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert [item[0] for item in script.calls] == ["identity", "portfolio"]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
        snapshot_count = connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[
            0
        ]
    assert run["status"] == "cancelled"
    assert snapshot_count == 0


def test_worker_timeout_cancellation_is_terminal_expiry(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(tmp_path)
    original_sync = service._portfolio_service.sync

    def timed_out(fund_code, context):
        result = original_sync(fund_code, context)
        context.budget.cancel("worker_timeout")
        return result

    service._portfolio_service.sync = timed_out
    with pytest.raises(BudgetExpired):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert [item[0] for item in script.calls] == ["identity", "portfolio"]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "expired"


def test_owner_interrupt_finalizes_cancelled_without_later_work(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(
        tmp_path,
        interrupt_at="nav",
    )

    with pytest.raises(KeyboardInterrupt):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert [item[0] for item in script.calls] == ["identity", "portfolio", "nav"]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "cancelled"


@pytest.mark.parametrize(
    "late_stage",
    ("identity", "portfolio", "nav", "manager_fee", "holdings", "announcements"),
)
def test_deadline_after_each_source_result_stops_later_scheduling(
    tmp_path: Path,
    late_stage: str,
) -> None:
    stages = [
        "identity",
        "portfolio",
        "nav",
        "manager_fee",
        "holdings",
        "announcements",
    ]
    repository, _audit, script, service = _service(
        tmp_path,
        cancel_at=late_stage,
        cancel_reason="request_deadline_reached",
    )

    with pytest.raises(BudgetExpired):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    cutoff = stages.index(late_stage) + 1
    assert [item[0] for item in script.calls] == stages[:cutoff]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
        decision_count = connection.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0]
        brief_count = connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[0]
    assert run["status"] == "expired"
    assert decision_count == 0
    assert brief_count == 0


def test_system_exit_finalizes_cancelled_without_later_work(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(
        tmp_path,
        system_exit_at="holdings",
    )

    with pytest.raises(SystemExit):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    assert [item[0] for item in script.calls] == [
        "identity",
        "portfolio",
        "nav",
        "manager_fee",
        "holdings",
    ]
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
        assert connection.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[0] == 0
    assert run["status"] == "cancelled"


def test_cooldown_result_is_not_retried_and_other_sources_continue(tmp_path: Path) -> None:
    repository, _audit, script, service = _service(tmp_path)
    original_sync = service._portfolio_service.sync

    def cooldown(fund_code, context):
        result = original_sync(fund_code, context)
        return PortfolioObservationResult(
            result.fund_code,
            "skipped_cooldown",
            result.accounts,
            result.positions,
            result.position_present,
            result.observed_at,
            result.source_attempt_id,
            result.portfolio_binding,
            "cooldown_active",
        )

    service._portfolio_service.sync = cooldown

    service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert [item[0] for item in script.calls].count("portfolio") == 1
    assert [item[0] for item in script.calls][-1] == "announcements"
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "personal_position_observation" in run["omitted_work_json"]


def test_final_publish_failure_rolls_back_and_preserves_prior_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _audit, _script, service = _service(tmp_path)
    first = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    def fail_after_decision_insert(*_args, **_kwargs):
        raise RuntimeError("scripted brief insert failure")

    monkeypatch.setattr(
        service._brief_store,
        "_insert_snapshot",
        fail_after_decision_insert,
    )
    with pytest.raises(Exception, match="held fund brief failed"):
        service.brief(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    history = service._brief_store.history(FUND_CODE)
    assert len(history) == 1
    assert history[0].snapshot == first.snapshot
    with repository.connect() as connection:
        runs = connection.execute("SELECT status FROM request_runs ORDER BY id").fetchall()
        decisions = connection.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0]
        briefs = connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[0]
    assert [row["status"] for row in runs] == ["partial", "failed"]
    assert decisions == 1
    assert briefs == 1


def test_outcome_validation_failure_rolls_back_and_preserves_prior_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _audit, _script, service = _service(tmp_path)
    first = service.brief_outcome(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    def reject_outcome(_outcome) -> None:
        raise ValueError("scripted outcome validation failure")

    monkeypatch.setattr(HeldFundBriefOutcome, "validate", reject_outcome)
    with pytest.raises(Exception, match="held fund brief failed"):
        service.brief_outcome(
            FUND_CODE,
            action=ActionKind.CONTINUE_HOLDING,
            mode=RequestMode.RAPID,
        )

    history = service._brief_store.history(FUND_CODE)
    assert len(history) == 1
    assert history[0].snapshot == first.report.snapshot
    with repository.connect() as connection:
        runs = connection.execute("SELECT status FROM request_runs ORDER BY id").fetchall()
        decisions = connection.execute("SELECT COUNT(*) FROM decision_snapshots").fetchone()[0]
        briefs = connection.execute("SELECT COUNT(*) FROM fund_brief_snapshots").fetchone()[0]
    assert [row["status"] for row in runs] == ["partial", "failed"]
    assert decisions == 1
    assert briefs == 1


def test_service_starts_no_background_thread_or_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import threading

    _repository_value, _audit, _script, service = _service(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("background or process work is forbidden")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )


def test_source_resolution_uses_final_retry_not_first_transient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository_value, audit, _script, service = _service(tmp_path)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="1234567890abcdef1234567890abcdef",
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    request_run_id = audit.begin_request(budget)
    attempts = (
        SimpleNamespace(
            id=11,
            attempt=SimpleNamespace(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
            ),
        ),
        SimpleNamespace(
            id=12,
            attempt=SimpleNamespace(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.SUCCESS,
            ),
        ),
        SimpleNamespace(
            id=13,
            attempt=SimpleNamespace(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key="fund:000001",
                outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
            ),
        ),
    )
    monkeypatch.setattr(
        audit,
        "authenticated_request_source_attempts",
        lambda *_args: attempts,
    )
    loaded: list[int] = []

    def resolution_loader(_store, source_attempt_id, **_kwargs):
        loaded.append(source_attempt_id)
        return source_attempt_id

    monkeypatch.setattr(
        "kunjin.brief.service.load_brief_source_resolution",
        resolution_loader,
    )
    fact = SimpleNamespace(
        fact_id="formal_nav_fact",
        field_id="formal_nav",
        source_lineage_id="source_attempt_12",
    )
    route = SimpleNamespace(
        actions=(SimpleNamespace(action_id="fact_research"),),
    )
    fact_set = SimpleNamespace(
        fund_code=FUND_CODE,
        facts=(fact,),
        official_events=(),
    )
    d2 = SimpleNamespace(evidence_facts=())

    resolutions = service._source_resolutions(
        request_run_id,
        budget,
        route,
        fact_set,
        d2,
        NOW,
    )

    assert resolutions == (12,)
    assert loaded == [12]


def test_unavailable_source_resolution_preserves_registry_alternatives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository_value, audit, _script, service = _service(tmp_path)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="1234567890abcdef1234567890abcdef",
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    request_run_id = audit.begin_request(budget)
    attempts = (
        SimpleNamespace(
            id=11,
            attempt=SimpleNamespace(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.UNAVAILABLE,
            ),
        ),
    )
    monkeypatch.setattr(
        audit,
        "authenticated_request_source_attempts",
        lambda *_args: attempts,
    )
    captured: list[tuple[str, ...]] = []

    def resolution_loader(_store, source_attempt_id, **kwargs):
        captured.append(
            (
                kwargs["acceptable_alternative_ids"],
                kwargs["manual_supplement_ready"],
            )
        )
        return source_attempt_id

    monkeypatch.setattr(
        "kunjin.brief.service.load_brief_source_resolution",
        resolution_loader,
    )
    route = SimpleNamespace(actions=(SimpleNamespace(action_id="fact_research"),))
    fact_set = SimpleNamespace(fund_code=FUND_CODE, facts=(), official_events=())
    d2 = SimpleNamespace(evidence_facts=())

    resolutions = service._source_resolutions(
        request_run_id,
        budget,
        route,
        fact_set,
        d2,
        NOW,
    )

    assert resolutions == (11,)
    assert captured == [(('fund_manager_official_documents',), False)]


def test_successful_alternative_wins_over_later_unavailable_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository_value, audit, _script, service = _service(tmp_path)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="1234567890abcdef1234567890abcdef",
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    request_run_id = audit.begin_request(budget)
    attempts = (
        SimpleNamespace(
            id=11,
            attempt=SimpleNamespace(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.SUCCESS,
            ),
        ),
        SimpleNamespace(
            id=12,
            attempt=SimpleNamespace(
                source_id="fund_manager_official_documents",
                field_id="formal_nav",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.UNAVAILABLE,
            ),
        ),
    )
    monkeypatch.setattr(
        audit,
        "authenticated_request_source_attempts",
        lambda *_args: attempts,
    )
    loaded: list[int] = []

    def resolution_loader(_store, source_attempt_id, **_kwargs):
        loaded.append(source_attempt_id)
        return source_attempt_id

    monkeypatch.setattr(
        "kunjin.brief.service.load_brief_source_resolution",
        resolution_loader,
    )
    fact = SimpleNamespace(
        fact_id="formal_nav_fact",
        field_id="formal_nav",
        source_lineage_id="source_attempt_11",
    )
    route = SimpleNamespace(actions=(SimpleNamespace(action_id="fact_research"),))
    fact_set = SimpleNamespace(
        fund_code=FUND_CODE,
        facts=(fact,),
        official_events=(),
    )
    d2 = SimpleNamespace(evidence_facts=())

    resolutions = service._source_resolutions(
        request_run_id,
        budget,
        route,
        fact_set,
        d2,
        NOW,
    )

    assert resolutions == (11,)
    assert loaded == [11]


def test_cross_field_alternatives_are_counted_when_deciding_manual_supplementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repository_value, audit, _script, service = _service(tmp_path)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="1234567890abcdef1234567890abcdef",
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    request_run_id = audit.begin_request(budget)
    attempts = (
        SimpleNamespace(
            id=11,
            attempt=SimpleNamespace(
                source_id="fund_manager_official_documents",
                field_id="transaction_availability_limits_cutoff",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.UNAVAILABLE,
            ),
        ),
        SimpleNamespace(
            id=12,
            attempt=SimpleNamespace(
                source_id="yangjibao_portfolio_observation",
                field_id="transaction_channel_observation",
                subject_key=f"fund:{FUND_CODE}",
                outcome=SourceAttemptOutcome.UNSUPPORTED,
            ),
        ),
    )
    monkeypatch.setattr(
        audit,
        "authenticated_request_source_attempts",
        lambda *_args: attempts,
    )
    captured: list[bool] = []

    def resolution_loader(_store, source_attempt_id, **kwargs):
        if source_attempt_id == 11:
            captured.append(kwargs["manual_supplement_ready"])
        return source_attempt_id

    monkeypatch.setattr(
        "kunjin.brief.service.load_brief_source_resolution",
        resolution_loader,
    )
    route = SimpleNamespace(actions=(SimpleNamespace(action_id="fact_research"),))
    fact_set = SimpleNamespace(fund_code=FUND_CODE, facts=(), official_events=())
    d2 = SimpleNamespace(evidence_facts=())

    service._source_resolutions(
        request_run_id,
        budget,
        route,
        fact_set,
        d2,
        NOW,
    )

    assert captured == [True]


def test_constructor_rejects_disclosure_store_from_another_database(
    tmp_path: Path,
) -> None:
    from kunjin.brief.service import HeldFundBriefService

    (tmp_path / "primary").mkdir()
    (tmp_path / "other").mkdir()
    repository = _repository(tmp_path / "primary")
    other = _repository(tmp_path / "other")
    audit = DecisionAuditStore(repository)
    script = _Script([])

    with pytest.raises(ValueError, match="disclosure store must share"):
        HeldFundBriefService(
            repository=repository,
            suitability_service=_Suitability(),
            disclosure_service=_Disclosure(FundDisclosureStore(other), script),
            portfolio_service=_Portfolio(repository, script),
            nav_service=_Nav(repository, script),
            audit_store=audit,
            now=lambda: NOW,
            monotonic=lambda: 1.0,
        )


def test_active_thesis_without_invalidation_matcher_fails_closed(tmp_path: Path) -> None:
    repository, _audit, _script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )
    repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="长期观察基金策略是否持续。",
            horizon="三年以上",
            invalidation="基金投资策略发生重大变化",
            created_at=NOW - timedelta(days=10),
            active=True,
        )
    )

    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert report.snapshot.primary_state.value != "hold"
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "thesis_review" in run["omitted_work_json"]


def test_peer_fact_failure_is_omitted_without_losing_target_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _audit, _script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )
    original_sync = service._portfolio_service.sync

    def portfolio_with_peer(fund_code, context):
        result = original_sync(fund_code, context)
        peer = StoredPosition(
            account_title="测试账户",
            fund_code="000001",
            fund_name="测试同组合基金",
            shares=Decimal("1"),
            observed_at=context.budget.started_at,
            share_class="A",
            formal_nav=Decimal("1"),
            estimated_nav=None,
            observed_profit=None,
        )
        binding = replace(result.portfolio_binding, positions=(peer,))
        binding.validate()
        return replace(result, positions=1, portfolio_binding=binding)

    original_load = service._disclosure_store.load_bundle

    def fail_peer(code):
        if code == "000001":
            raise ValueError("scripted unsupported peer")
        return original_load(code)

    monkeypatch.setattr(service._portfolio_service, "sync", portfolio_with_peer)
    monkeypatch.setattr(service._disclosure_store, "load_bundle", fail_peer)

    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert report.snapshot.fund_code == FUND_CODE
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "peer_fact_projection" in run["omitted_work_json"]


def test_forged_same_request_portfolio_binding_degrades_to_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _audit, _script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )
    original_sync = service._portfolio_service.sync

    def forged(fund_code, context):
        result = original_sync(fund_code, context)
        binding = replace(
            result.portfolio_binding,
            observation_version="source_attempt_999",
        )
        binding.validate()
        return replace(
            result,
            source_attempt_id=999,
            portfolio_binding=binding,
        )

    monkeypatch.setattr(service._portfolio_service, "sync", forged)

    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert report.snapshot.portfolio_evidence_state == "unknown"
    assert report.snapshot.position_present is None
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "personal_position_observation" in run["omitted_work_json"]


def test_success_with_unbound_portfolio_binding_is_still_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _audit, _script, service = _service(
        tmp_path,
        all_sources_complete=True,
    )
    original_sync = service._portfolio_service.sync

    def forged_unbound(fund_code, context):
        result = original_sync(fund_code, context)
        binding = PortfolioEvidenceBinding(
            positions=(),
            snapshot_complete=False,
            observation_version="portfolio_unbound_forgery",
            observed_at=context.budget.started_at,
            source_state="unbound",
            request_id=None,
            request_mode=None,
            request_started_at=None,
            request_deadline_at=None,
        )
        binding.validate()
        return replace(result, portfolio_binding=binding)

    monkeypatch.setattr(service._portfolio_service, "sync", forged_unbound)

    report = service.brief(
        FUND_CODE,
        action=ActionKind.CONTINUE_HOLDING,
        mode=RequestMode.RAPID,
    )

    assert report.snapshot.portfolio_evidence_state == "unknown"
    with repository.connect() as connection:
        run = connection.execute("SELECT * FROM request_runs").fetchone()
    assert run["status"] == "partial"
    assert "personal_position_observation" in run["omitted_work_json"]


def test_constructor_rejects_source_service_without_repository(tmp_path: Path) -> None:
    from kunjin.brief.service import HeldFundBriefService

    repository = _repository(tmp_path)
    audit = DecisionAuditStore(repository)
    script = _Script([])
    portfolio_without_repository = SimpleNamespace(sync=lambda *_args, **_kwargs: None)

    with pytest.raises(ValueError, match="portfolio service must expose repository"):
        HeldFundBriefService(
            repository=repository,
            suitability_service=_Suitability(),
            disclosure_service=_Disclosure(FundDisclosureStore(repository), script),
            portfolio_service=portfolio_without_repository,
            nav_service=_Nav(repository, script),
            audit_store=audit,
            now=lambda: NOW,
            monotonic=lambda: 1.0,
        )
