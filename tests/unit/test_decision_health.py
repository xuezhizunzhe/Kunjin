from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import (
    INITIAL_COOLDOWN,
    ForceAuthorization,
    SourceHealthService,
)
from kunjin.decision.models import (
    ActionKind,
    ForceReasonCode,
    FreshnessContext,
    RequestFieldResolution,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldRef,
    SourceFieldState,
    StoredSourceAttempt,
)
from kunjin.decision.source_registry import SOURCE_REGISTRY_V1_CHECKSUM
from kunjin.decision.store import DecisionAuditStore
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)
SUBJECT = "fund:123456"
PRIMARY = ("eastmoney_nav", "formal_nav")
ALTERNATIVE = ("fund_manager_official_documents", "formal_nav")


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Harness:
    def __init__(self, tmp_path) -> None:
        repository = Repository(tmp_path / "kunjin.db")
        repository.migrate()
        self.store = DecisionAuditStore(repository)
        self.service = SourceHealthService(self.store, wall_clock=lambda: NOW)
        self.sequence = 0
        self.query_sequence = 200

    def record(self, **overrides) -> StoredSourceAttempt:
        self.sequence += 1
        mode = overrides.pop("mode", RequestMode.RAPID)
        request_id = overrides.pop("request_id", f"{self.sequence:032x}")
        finished_at = overrides.pop(
            "finished_at", NOW - timedelta(minutes=20 - self.sequence)
        )
        source_id, field_id = overrides.pop("identity", PRIMARY)
        values = {
            "source_id": source_id,
            "field_id": field_id,
            "subject_key": SUBJECT,
            "attempt_number": 1,
            "outcome": SourceAttemptOutcome.SUCCESS,
            "started_at": finished_at - timedelta(seconds=1),
            "finished_at": finished_at,
            "data_as_of": NOW - timedelta(days=1),
            "error_code": None,
            "cooldown_until": None,
            "force_actor": None,
            "force_reason": None,
            "registry_version": "1",
            "registry_checksum": SOURCE_REGISTRY_V1_CHECKSUM,
            "response_bytes": 100,
        }
        values.update(overrides)
        attempt = SourceAttempt(**values)
        budget = RequestBudget.create(
            mode,
            request_id=request_id,
            monotonic=lambda: 10.0,
            wall_clock=lambda: attempt.started_at,
        )
        run_id = self.store.begin_request(budget)
        self.store.record_source_attempt(run_id, attempt)
        self.store.finalize_request(
            run_id,
            RequestTerminalStatus.COMPLETE,
            attempt.finished_at,
            (),
        )
        return self.store.source_attempt_history(source_id, field_id, SUBJECT)[0]

    def begin(
        self,
        *,
        mode: RequestMode = RequestMode.RAPID,
        request_id: str | None = None,
        started_at: datetime = NOW,
        clock: _Clock | None = None,
    ) -> tuple[int, RequestBudget]:
        self.query_sequence += 1
        if request_id is None:
            request_id = f"{self.query_sequence:032x}"
        if clock is None:
            clock = _Clock(10.0)
        budget = RequestBudget.create(
            mode,
            request_id=request_id,
            monotonic=clock,
            wall_clock=lambda: started_at,
        )
        return self.store.begin_request(budget), budget

    def transient_parent(
        self,
        *,
        request_id: str,
        clock: _Clock | None = None,
        started_at: datetime = NOW - timedelta(seconds=1),
        finished_at: datetime = NOW,
    ) -> tuple[int, RequestBudget, StoredSourceAttempt]:
        request_run_id, budget = self.begin(
            request_id=request_id,
            started_at=started_at,
            clock=clock,
        )
        attempt = _attempt(
            started_at=finished_at - timedelta(seconds=1),
            finished_at=finished_at,
            cooldown_until=finished_at + INITIAL_COOLDOWN,
        )
        self.store.record_source_attempt(request_run_id, attempt)
        parent = self.store.source_attempt_history(*PRIMARY, SUBJECT)[0]
        return request_run_id, budget, parent

    def state(self, identity=PRIMARY, **context_overrides) -> SourceFieldState:
        request_run_id, budget = self.begin()
        return self.service.source_field_state(
            *identity,
            SUBJECT,
            _context(budget, **context_overrides),
            request_run_id=request_run_id,
            budget=budget,
        )

    def resolve(
        self,
        identity=PRIMARY,
        *,
        action: ActionKind = ActionKind.FACT_RESEARCH,
        risk_effect: RiskEffect = RiskEffect.INFORMATION,
        **context_overrides,
    ) -> RequestFieldResolution:
        request_run_id, budget = self.begin()
        requirement = self.service.action_requirement(identity[1], action, risk_effect)
        return self.service.resolve_field(
            *identity,
            SUBJECT,
            _context(budget, **context_overrides),
            requirement,
            request_run_id=request_run_id,
            budget=budget,
        )


def _context(budget: RequestBudget, **overrides) -> FreshnessContext:
    values = {
        "now": NOW,
        "request_id": budget.request_id,
        "latest_expected_data_as_of": NOW - timedelta(days=1),
    }
    values.update(overrides)
    return FreshnessContext(**values)


def _attempt(**overrides) -> SourceAttempt:
    values = {
        "source_id": PRIMARY[0],
        "field_id": PRIMARY[1],
        "subject_key": SUBJECT,
        "attempt_number": 1,
        "outcome": SourceAttemptOutcome.TRANSIENT_FAILURE,
        "started_at": NOW - timedelta(seconds=1),
        "finished_at": NOW,
        "data_as_of": None,
        "error_code": SourceErrorCode.NETWORK_TIMEOUT,
        "cooldown_until": NOW + INITIAL_COOLDOWN,
        "force_actor": None,
        "force_reason": None,
        "registry_version": "1",
        "registry_checksum": SOURCE_REGISTRY_V1_CHECKSUM,
        "response_bytes": 0,
    }
    values.update(overrides)
    attempt = SourceAttempt(**values)
    attempt.validate()
    return attempt


def _budget(
    mode: RequestMode,
    request_id: str,
    *,
    clock: _Clock | None = None,
) -> RequestBudget:
    if clock is None:
        clock = _Clock(10.0)
    return RequestBudget.create(
        mode,
        request_id=request_id,
        monotonic=clock,
        wall_clock=lambda: NOW,
    )


def test_manual_supplement_is_only_a_request_resolution() -> None:
    assert RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED.value == (
        "manual_supplement_required"
    )
    assert "manual_supplement_required" not in {item.value for item in SourceFieldState}


def test_source_status_snapshot_uses_one_authenticated_sqlite_snapshot(
    tmp_path, monkeypatch
) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        identity=PRIMARY,
        finished_at=NOW + timedelta(minutes=1),
        data_as_of=NOW,
    )
    harness.record(
        identity=PRIMARY,
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        finished_at=NOW + timedelta(minutes=2),
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=NOW + INITIAL_COOLDOWN,
    )
    request_run_id, budget = harness.begin()
    primary = SourceFieldRef(*PRIMARY)
    requirement = harness.service.action_requirement(
        PRIMARY[1],
        ActionKind.FACT_RESEARCH,
        RiskEffect.INFORMATION,
    )
    original = harness.store.authenticated_source_attempt_histories
    calls = 0

    def interleaved_read(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            harness.record(identity=PRIMARY)
        return result

    monkeypatch.setattr(
        harness.store,
        "authenticated_source_attempt_histories",
        interleaved_read,
    )

    snapshot = harness.service.source_status_snapshot(
        SUBJECT,
        _context(budget),
        (primary,),
        (requirement,),
        request_run_id=request_run_id,
        budget=budget,
    )

    assert calls == 1
    projection = next(
        item for item in snapshot.projections if item.history.reference == primary
    )
    assert projection.state is SourceFieldState.NOT_CHECKED
    assert projection.history.attempts == ()
    assert snapshot.resolutions == (RequestFieldResolution.PARTIAL,)


def test_future_attempts_cannot_evict_past_success_before_history_limit(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        identity=PRIMARY,
        finished_at=NOW - timedelta(minutes=1),
        data_as_of=NOW - timedelta(days=1),
    )
    for offset in range(1, 65):
        harness.record(
            identity=PRIMARY,
            finished_at=NOW + timedelta(minutes=offset),
            data_as_of=NOW,
        )

    request_run_id, budget = harness.begin()
    requirement = harness.service.action_requirement(
        PRIMARY[1],
        ActionKind.FACT_RESEARCH,
        RiskEffect.INFORMATION,
    )
    snapshot = harness.service.source_status_snapshot(
        SUBJECT,
        _context(budget),
        (SourceFieldRef(*PRIMARY),),
        (requirement,),
        request_run_id=request_run_id,
        budget=budget,
    )

    projection = next(
        item
        for item in snapshot.projections
        if item.history.reference == SourceFieldRef(*PRIMARY)
    )
    assert len(projection.history.attempts) == 1
    assert projection.state is SourceFieldState.HEALTHY
    assert snapshot.resolutions == (RequestFieldResolution.USABLE,)


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    (
        (SourceAttemptOutcome.CANCELLED, SourceErrorCode.REQUEST_CANCELLED),
        (SourceAttemptOutcome.EXPIRED, SourceErrorCode.REQUEST_EXPIRED),
    ),
)
def test_request_lifecycle_outcome_is_not_a_source_state(
    tmp_path,
    outcome: SourceAttemptOutcome,
    error_code: SourceErrorCode,
) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=outcome,
        data_as_of=None,
        error_code=error_code,
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.NOT_CHECKED


def test_request_lifecycle_outcomes_do_not_override_source_state(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.FIELD_UNSUPPORTED,
        response_bytes=0,
    )
    harness.record(
        outcome=SourceAttemptOutcome.CANCELLED,
        data_as_of=None,
        error_code=SourceErrorCode.REQUEST_CANCELLED,
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.UNSUPPORTED


def test_cooldown_skip_does_not_replace_transient_failure_state(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=NOW + INITIAL_COOLDOWN,
        response_bytes=0,
    )
    harness.record(
        outcome=SourceAttemptOutcome.SKIPPED_COOLDOWN,
        data_as_of=None,
        error_code=SourceErrorCode.COOLDOWN_ACTIVE,
        cooldown_until=NOW + INITIAL_COOLDOWN,
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.COOLDOWN


def test_lifecycle_attempts_cannot_evict_source_failure_before_history_limit(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    cooldown_until = NOW + timedelta(days=1)
    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=cooldown_until,
        response_bytes=0,
    )
    for index in range(64):
        finished_at = NOW - timedelta(seconds=64 - index)
        if index % 2 == 0:
            harness.record(
                outcome=SourceAttemptOutcome.SKIPPED_COOLDOWN,
                data_as_of=None,
                error_code=SourceErrorCode.COOLDOWN_ACTIVE,
                cooldown_until=cooldown_until,
                finished_at=finished_at,
                response_bytes=0,
            )
        else:
            harness.record(
                outcome=SourceAttemptOutcome.CANCELLED,
                data_as_of=None,
                error_code=SourceErrorCode.REQUEST_CANCELLED,
                finished_at=finished_at,
                response_bytes=0,
            )

    request_run_id, budget = harness.begin()
    requirement = harness.service.action_requirement(
        PRIMARY[1],
        ActionKind.FACT_RESEARCH,
        RiskEffect.INFORMATION,
    )
    snapshot = harness.service.source_status_snapshot(
        SUBJECT,
        _context(budget),
        (SourceFieldRef(*PRIMARY),),
        (requirement,),
        request_run_id=request_run_id,
        budget=budget,
    )
    projection = next(
        item
        for item in snapshot.projections
        if item.history.reference == SourceFieldRef(*PRIMARY)
    )

    assert len(projection.history.attempts) == 1
    assert projection.history.attempts[0].attempt.outcome is (
        SourceAttemptOutcome.TRANSIENT_FAILURE
    )
    assert projection.state is SourceFieldState.COOLDOWN


def test_every_registry_field_has_an_explicit_policy_requirement_binding(tmp_path) -> None:
    service = _Harness(tmp_path).service
    expected = {
        "adjusted_return_series": "adjusted_return_correlation",
        "current_manager_team": "current_manager_team",
        "fees_share_class_relationship": "fees_share_class_relationship",
        "formal_nav": "formal_nav",
        "fund_manager_product_announcement": "fund_manager_product_announcement",
        "holdings_industries": "holdings_industries",
        "identity_active_status": "identity_active_status",
        "market_context": "news_media_context",
        "market_dimensions": "news_media_context",
        "policy_events": "news_media_context",
        "fund_media_events": "news_media_context",
        "fund_official_events": "fund_manager_product_announcement",
        "personal_position_observation": "personal_position",
        "transaction_availability_limits_cutoff": (
            "transaction_availability_limits_cutoff"
        ),
        "transaction_channel_observation": "transaction_availability_limits_cutoff",
    }

    for field_id, requirement_id in expected.items():
        requirement = service.action_requirement(
            field_id,
            ActionKind.FACT_RESEARCH,
            RiskEffect.INFORMATION,
        )
        assert requirement.policy_requirement.field_id == requirement_id


def test_tier2_market_context_cannot_independently_authorize_an_action(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = ("eastmoney_market", "market_context")
    harness.record(identity=identity, finished_at=NOW, data_as_of=NOW)

    freshness = {
        "query_window_start": NOW - timedelta(hours=1),
        "query_window_end": NOW,
    }
    assert harness.resolve(identity, **freshness) is RequestFieldResolution.USABLE
    assert harness.resolve(
        identity,
        action=ActionKind.CONTINUE_HOLDING,
        risk_effect=RiskEffect.RISK_MAINTAINING,
        **freshness,
    ) is RequestFieldResolution.PARTIAL


def test_tier2_fund_media_cannot_independently_authorize_an_action(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = ("stcn_fund_news", "fund_media_events")
    harness.record(identity=identity, finished_at=NOW, data_as_of=NOW)

    freshness = {
        "query_window_start": NOW - timedelta(hours=1),
        "query_window_end": NOW,
        "correction_retraction_check_complete": True,
        "correction_retraction_found": False,
        "correction_retraction_checked_at": NOW,
    }
    assert harness.resolve(identity, **freshness) is RequestFieldResolution.USABLE
    assert harness.resolve(
        identity,
        action=ActionKind.CONTINUE_HOLDING,
        risk_effect=RiskEffect.RISK_MAINTAINING,
        **freshness,
    ) is RequestFieldResolution.PARTIAL


def test_source_without_history_is_not_checked(tmp_path) -> None:
    harness = _Harness(tmp_path)

    assert harness.state() is SourceFieldState.NOT_CHECKED


def test_current_and_dated_success_are_projected_from_stored_dates(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(data_as_of=NOW - timedelta(days=10))

    assert harness.state() is SourceFieldState.DEGRADED

    harness.record(data_as_of=NOW - timedelta(days=1))

    assert harness.state() is SourceFieldState.HEALTHY


def test_cache_hit_is_success_evidence(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.CACHE_HIT,
        data_as_of=NOW - timedelta(days=1),
    )

    assert harness.state() is SourceFieldState.HEALTHY


def test_same_request_uses_authenticated_run_request_id(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = ("yangjibao_portfolio_observation", "personal_position_observation")
    actual_request_id = "c" * 32
    claimed_request_id = "d" * 32
    harness.record(
        identity=identity,
        request_id=actual_request_id,
        finished_at=NOW,
        data_as_of=NOW - timedelta(minutes=1),
    )
    request_run_id, budget = harness.begin(request_id=claimed_request_id)

    state = harness.service.source_field_state(
        *identity,
        SUBJECT,
        _context(budget),
        request_run_id=request_run_id,
        budget=budget,
    )

    assert state is SourceFieldState.DEGRADED


def test_same_request_can_be_current_from_authenticated_run(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = ("yangjibao_portfolio_observation", "personal_position_observation")
    request_id = "e" * 32
    request_run_id, budget = harness.begin(
        request_id=request_id,
        started_at=NOW - timedelta(minutes=1),
    )
    attempt = SourceAttempt(
        source_id=identity[0],
        field_id=identity[1],
        subject_key=SUBJECT,
        attempt_number=1,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=NOW - timedelta(seconds=1),
        finished_at=NOW,
        data_as_of=NOW - timedelta(minutes=1),
        error_code=None,
        cooldown_until=None,
        force_actor=None,
        force_reason=None,
        registry_version="1",
        registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
        response_bytes=100,
    )
    harness.store.record_source_attempt(request_run_id, attempt)

    state = harness.service.source_field_state(
        *identity,
        SUBJECT,
        _context(budget),
        request_run_id=request_run_id,
        budget=budget,
    )

    assert state is SourceFieldState.HEALTHY


def test_same_trading_day_fails_closed_without_authenticated_calendar(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = (
        "fund_manager_official_documents",
        "transaction_availability_limits_cutoff",
    )
    harness.record(
        identity=identity,
        finished_at=NOW,
        data_as_of=NOW - timedelta(minutes=1),
    )

    state = harness.state(identity, trading_day=NOW.date())

    assert state is SourceFieldState.UNAVAILABLE


def test_old_cache_hit_cannot_satisfy_same_request(tmp_path) -> None:
    harness = _Harness(tmp_path)
    identity = ("yangjibao_portfolio_observation", "personal_position_observation")
    harness.record(
        identity=identity,
        outcome=SourceAttemptOutcome.CACHE_HIT,
        finished_at=NOW,
        data_as_of=NOW - timedelta(minutes=1),
    )

    assert harness.state(identity) is SourceFieldState.DEGRADED


def test_future_attempts_are_ignored(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        finished_at=NOW + timedelta(minutes=1),
        data_as_of=NOW,
    )

    assert harness.state() is SourceFieldState.NOT_CHECKED


def test_caller_cannot_backdate_health_evaluation(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(data_as_of=NOW - timedelta(days=10))

    assert harness.state(now=NOW - timedelta(days=10)) is SourceFieldState.DEGRADED


def test_active_transient_cooldown_precedes_older_success(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(data_as_of=NOW - timedelta(days=1))
    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=NOW + INITIAL_COOLDOWN,
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.COOLDOWN


def test_expired_cooldown_without_success_is_unavailable(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.NETWORK_TIMEOUT,
        cooldown_until=NOW - timedelta(minutes=1),
        finished_at=NOW - timedelta(minutes=31),
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.UNAVAILABLE


@pytest.mark.parametrize(
    "error_code",
    (
        SourceErrorCode.HTTP_NOT_FOUND,
        SourceErrorCode.HTTP_GONE,
        SourceErrorCode.FIELD_UNSUPPORTED,
        SourceErrorCode.SOURCE_CONTRACT_UNSUPPORTED,
    ),
)
def test_permanent_absence_and_audited_unsupported_are_unsupported(
    tmp_path,
    error_code: SourceErrorCode,
) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=error_code,
        response_bytes=0,
    )

    assert harness.state() is SourceFieldState.UNSUPPORTED


def test_successful_alternative_makes_field_usable(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.HTTP_NOT_FOUND,
        response_bytes=0,
    )
    harness.record(identity=ALTERNATIVE, data_as_of=NOW - timedelta(days=1))

    assert harness.resolve() is RequestFieldResolution.USABLE


def test_dated_alternative_is_partial_without_promoting_it(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNAVAILABLE,
        data_as_of=None,
        error_code=SourceErrorCode.PARSE_FAILURE,
        response_bytes=0,
    )
    harness.record(identity=ALTERNATIVE, data_as_of=NOW - timedelta(days=10))

    assert harness.resolve() is RequestFieldResolution.PARTIAL


def test_lower_tier_healthy_alternative_cannot_satisfy_tier_one_field(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        identity=ALTERNATIVE,
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.HTTP_NOT_FOUND,
        response_bytes=0,
    )
    harness.record(identity=PRIMARY, data_as_of=NOW - timedelta(days=1))

    assert harness.resolve(
        ALTERNATIVE,
        action=ActionKind.BUY_OR_ADD,
        risk_effect=RiskEffect.RISK_INCREASING,
    ) is RequestFieldResolution.PARTIAL


def test_single_tier_two_identity_is_research_usable_but_not_action_usable(
    tmp_path,
) -> None:
    harness = _Harness(tmp_path)
    official = ("fund_manager_official_documents", "identity_active_status")
    tier_two = ("eastmoney_f10", "identity_active_status")
    harness.record(
        identity=official,
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.HTTP_NOT_FOUND,
        response_bytes=0,
    )
    harness.record(identity=tier_two, data_as_of=NOW - timedelta(days=1))
    freshness = {
        "newer_announcement_check_complete": True,
        "newer_announcement_found": False,
        "newer_announcement_checked_at": NOW,
    }

    assert harness.resolve(official, **freshness) is RequestFieldResolution.USABLE
    assert harness.resolve(
        official,
        action=ActionKind.BUY_OR_ADD,
        risk_effect=RiskEffect.RISK_INCREASING,
        **freshness,
    ) is RequestFieldResolution.PARTIAL


def test_manual_supplement_requires_all_alternatives_to_be_exhausted(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.HTTP_GONE,
        response_bytes=0,
    )
    harness.record(
        identity=ALTERNATIVE,
        outcome=SourceAttemptOutcome.UNAVAILABLE,
        data_as_of=None,
        error_code=SourceErrorCode.IDENTITY_CONFLICT,
        response_bytes=0,
    )

    assert harness.resolve() is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED


def test_unchecked_or_cooling_alternative_is_not_misreported_as_exhausted(tmp_path) -> None:
    harness = _Harness(tmp_path)
    assert harness.resolve() is RequestFieldResolution.PARTIAL

    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.TRANSIENT_NETWORK_FAILURE,
        cooldown_until=NOW + INITIAL_COOLDOWN,
        response_bytes=0,
    )
    assert harness.resolve() is RequestFieldResolution.PARTIAL


def test_initial_cooldown_is_exactly_thirty_minutes() -> None:
    assert INITIAL_COOLDOWN == timedelta(minutes=30)
    assert SourceHealthService.cooldown_until(NOW) == NOW + timedelta(minutes=30)


def test_only_first_retryable_transient_attempt_with_budget_can_retry(tmp_path) -> None:
    harness = _Harness(tmp_path)
    request_run_id, budget, parent = harness.transient_parent(
        request_id="a" * 32
    )

    first = harness.service.retry_allowed(
        parent,
        budget,
        request_run_id=request_run_id,
        minimum_worker_seconds=5.0,
    )
    second = harness.service.retry_allowed(
        parent,
        budget,
        request_run_id=request_run_id,
        minimum_worker_seconds=5.0,
    )

    assert first is not None
    assert first.reserved_at == NOW
    assert second is None


def test_retry_authorization_is_atomic_under_concurrency(tmp_path) -> None:
    harness = _Harness(tmp_path)
    request_run_id, budget, parent = harness.transient_parent(request_id="1" * 32)
    services = (
        harness.service,
        SourceHealthService(
            DecisionAuditStore(harness.store.repository),
            wall_clock=lambda: NOW,
        ),
    )
    barrier = threading.Barrier(3)
    results: list[object] = []

    def attempt_retry(service: SourceHealthService) -> None:
        barrier.wait()
        results.append(
            service.retry_allowed(
                parent,
                budget,
                request_run_id=request_run_id,
                minimum_worker_seconds=5.0,
            )
        )

    threads = [threading.Thread(target=attempt_retry, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1


def test_retry_requires_sufficient_budget(tmp_path) -> None:
    harness = _Harness(tmp_path)
    clock = _Clock(10.0)
    request_run_id, budget, parent = harness.transient_parent(
        request_id="b" * 32,
        clock=clock,
    )
    clock.value = 94.0

    assert harness.service.retry_allowed(
        parent,
        budget,
        request_run_id=request_run_id,
        minimum_worker_seconds=5.0,
    ) is None


def test_force_requires_deep_mode_allowlisted_reason_and_first_attempt(tmp_path) -> None:
    harness = _Harness(tmp_path)
    service = harness.service
    reason = ForceReasonCode.OWNER_APPROVED_RETRY
    rapid_run_id, rapid_budget = harness.begin(
        mode=RequestMode.RAPID, request_id="2" * 32
    )
    deep_run_id, deep_budget = harness.begin(
        mode=RequestMode.DEEP, request_id="3" * 32
    )

    with pytest.raises(ValueError, match="deep"):
        service.force_authorization(
            rapid_budget,
            *PRIMARY,
            SUBJECT,
            reason,
            request_run_id=rapid_run_id,
            attempt_number=1,
        )
    with pytest.raises(ValueError, match="force reason"):
        service.force_authorization(
            deep_budget,
            *PRIMARY,
            SUBJECT,
            "",
            request_run_id=deep_run_id,
            attempt_number=1,
        )
    with pytest.raises(ValueError, match="first attempt"):
        service.force_authorization(
            deep_budget,
            *PRIMARY,
            SUBJECT,
            reason,
            request_run_id=deep_run_id,
            attempt_number=2,
        )

    authorization = service.force_authorization(
        deep_budget,
        *PRIMARY,
        SUBJECT,
        reason,
        request_run_id=deep_run_id,
        attempt_number=1,
    )
    assert authorization is not None
    authorization.validate()
    assert authorization.request_run_id == deep_run_id
    assert authorization.request_id == deep_budget.request_id
    assert authorization.reason is reason

    class DerivedForceAuthorization(ForceAuthorization):
        pass

    with pytest.raises(ValueError, match="subclasses"):
        DerivedForceAuthorization(authorization.reservation).validate()
    forced_attempt = SourceAttempt(
        source_id=PRIMARY[0],
        field_id=PRIMARY[1],
        subject_key=SUBJECT,
        attempt_number=1,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=NOW + timedelta(seconds=1),
        finished_at=NOW + timedelta(seconds=2),
        data_as_of=NOW,
        error_code=None,
        cooldown_until=None,
        force_actor=authorization.actor,
        force_reason=authorization.reason,
        registry_version="1",
        registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
        response_bytes=100,
    )
    attempt_id = harness.store.record_source_attempt(
        deep_run_id,
        forced_attempt,
        authorization,
    )
    history = harness.store.source_attempt_history(*PRIMARY, SUBJECT)
    assert history[0].id == attempt_id
    assert history[0].authorization_id == authorization.authorization_id
    assert service.force_authorization(
        deep_budget,
        *PRIMARY,
        SUBJECT,
        reason,
        request_run_id=deep_run_id,
        attempt_number=1,
    ) is None


def test_force_requires_publishable_budget_and_bounded_wall_time(tmp_path) -> None:
    harness = _Harness(tmp_path)
    service = harness.service
    clock = _Clock(10.0)
    request_run_id, budget = harness.begin(
        mode=RequestMode.DEEP,
        request_id="4" * 32,
        clock=clock,
    )
    reason = ForceReasonCode.VERIFY_SOURCE_RECOVERY
    outside_service = SourceHealthService(
        DecisionAuditStore(harness.store.repository),
        wall_clock=lambda: NOW - timedelta(microseconds=1),
    )

    with pytest.raises(ValueError, match="wall clock"):
        outside_service.force_authorization(
            budget,
            *PRIMARY,
            SUBJECT,
            reason,
            request_run_id=request_run_id,
            attempt_number=1,
        )

    clock.value = 490.0
    with pytest.raises(BudgetExpired, match="deadline"):
        service.force_authorization(
            budget,
            *PRIMARY,
            SUBJECT,
            reason,
            request_run_id=request_run_id,
            attempt_number=1,
        )


def test_force_authorization_uses_one_trusted_wall_clock_read(tmp_path) -> None:
    harness = _Harness(tmp_path)
    request_run_id, budget = harness.begin(
        mode=RequestMode.DEEP,
        request_id="7" * 32,
    )
    calls = 0

    def advancing_clock() -> datetime:
        nonlocal calls
        calls += 1
        return NOW + timedelta(microseconds=calls)

    service = SourceHealthService(
        DecisionAuditStore(harness.store.repository),
        wall_clock=advancing_clock,
    )
    authorization = service.force_authorization(
        budget,
        *PRIMARY,
        SUBJECT,
        ForceReasonCode.OWNER_APPROVED_RETRY,
        request_run_id=request_run_id,
        attempt_number=1,
    )

    assert authorization is not None
    assert authorization.reservation.reserved_at == NOW + timedelta(microseconds=1)
    assert calls == 1


def test_force_authorization_is_atomic_under_concurrency(tmp_path) -> None:
    harness = _Harness(tmp_path)
    request_run_id, budget = harness.begin(
        mode=RequestMode.DEEP,
        request_id="9" * 32,
    )
    services = (
        harness.service,
        SourceHealthService(
            DecisionAuditStore(harness.store.repository),
            wall_clock=lambda: NOW,
        ),
    )
    barrier = threading.Barrier(3)
    results: list[ForceAuthorization | None] = []

    def authorize_force(service: SourceHealthService) -> None:
        barrier.wait()
        results.append(
            service.force_authorization(
                budget,
                *PRIMARY,
                SUBJECT,
                ForceReasonCode.OWNER_APPROVED_RETRY,
                request_run_id=request_run_id,
                attempt_number=1,
            )
        )

    threads = [threading.Thread(target=authorize_force, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 1
    assert sum(result is None for result in results) == 1


def test_force_authorization_is_single_use_across_service_instances(tmp_path) -> None:
    harness = _Harness(tmp_path)
    first_service = harness.service
    second_service = SourceHealthService(
        DecisionAuditStore(harness.store.repository),
        wall_clock=lambda: NOW,
    )
    request_run_id, budget = harness.begin(
        mode=RequestMode.DEEP,
        request_id="0" * 32,
    )
    arguments = (
        budget,
        *PRIMARY,
        SUBJECT,
        ForceReasonCode.OWNER_APPROVED_RETRY,
    )

    assert first_service.force_authorization(
        *arguments, request_run_id=request_run_id, attempt_number=1
    ) is not None
    assert second_service.force_authorization(
        *arguments, request_run_id=request_run_id, attempt_number=1
    ) is None


def test_old_attempt_cannot_receive_retry_from_a_new_request(tmp_path) -> None:
    harness = _Harness(tmp_path)
    old_run_id, old_budget, old_parent = harness.transient_parent(
        request_id="c" * 32,
        started_at=NOW - timedelta(days=10, seconds=1),
        finished_at=NOW - timedelta(days=10),
    )
    del old_run_id, old_budget
    new_run_id, new_budget = harness.begin(request_id="d" * 32)

    assert harness.service.retry_allowed(
        old_parent,
        new_budget,
        request_run_id=new_run_id,
        minimum_worker_seconds=5.0,
    ) is None


def test_ordinary_request_never_inherits_prior_force(tmp_path) -> None:
    harness = _Harness(tmp_path)
    service = harness.service
    deep_run_id, deep_budget = harness.begin(
        mode=RequestMode.DEEP, request_id="5" * 32
    )
    forced = service.force_authorization(
        deep_budget,
        *PRIMARY,
        SUBJECT,
        ForceReasonCode.VERIFY_SOURCE_RECOVERY,
        request_run_id=deep_run_id,
        attempt_number=1,
    )
    assert forced is not None

    rapid_run_id, rapid_budget = harness.begin(request_id="6" * 32)
    assert service.force_authorization(
        rapid_budget,
        *PRIMARY,
        SUBJECT,
        None,
        request_run_id=rapid_run_id,
        attempt_number=1,
    ) is None


def test_health_service_requires_exact_audit_store_type(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()

    class DerivedStore(DecisionAuditStore):
        pass

    with pytest.raises(ValueError, match="exact DecisionAuditStore"):
        SourceHealthService(DerivedStore(repository))


def test_default_health_wall_clock_does_not_require_caller_time_prediction(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    service = SourceHealthService(store)
    budget = RequestBudget.create(RequestMode.DEEP, request_id="e" * 32)
    request_run_id = store.begin_request(budget)

    state = service.source_field_state(
        *PRIMARY,
        SUBJECT,
        FreshnessContext(now=NOW, request_id="f" * 32),
        request_run_id=request_run_id,
        budget=budget,
    )

    assert state is SourceFieldState.NOT_CHECKED
    assert service.force_authorization(
        budget,
        *PRIMARY,
        SUBJECT,
        ForceReasonCode.OWNER_APPROVED_RETRY,
        request_run_id=request_run_id,
        attempt_number=1,
    ) is not None
