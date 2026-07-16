from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kunjin.decision.budget import RequestBudget
from kunjin.decision.health import (
    INITIAL_COOLDOWN,
    ForceAuthorization,
    SourceHealthService,
)
from kunjin.decision.models import (
    ForceReasonCode,
    FreshnessContext,
    RequestFieldResolution,
    RequestMode,
    RequestTerminalStatus,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldState,
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
        self.service = SourceHealthService(self.store)
        self.sequence = 0

    def record(self, **overrides) -> SourceAttempt:
        self.sequence += 1
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
            RequestMode.RAPID,
            request_id=f"{self.sequence:032x}",
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
        return attempt


def _context(**overrides) -> FreshnessContext:
    values = {
        "now": NOW,
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


def test_manual_supplement_is_only_a_request_resolution() -> None:
    assert RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED.value == (
        "manual_supplement_required"
    )
    assert "manual_supplement_required" not in {item.value for item in SourceFieldState}


def test_source_without_history_is_not_checked(tmp_path) -> None:
    harness = _Harness(tmp_path)

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.NOT_CHECKED
    )


def test_current_and_dated_success_are_projected_from_stored_dates(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(data_as_of=NOW - timedelta(days=10))

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.DEGRADED
    )

    harness.record(data_as_of=NOW - timedelta(days=1))

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.HEALTHY
    )


def test_cache_hit_is_success_evidence(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.CACHE_HIT,
        data_as_of=NOW - timedelta(days=1),
    )

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.HEALTHY
    )


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

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.COOLDOWN
    )


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

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.UNAVAILABLE
    )


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

    assert harness.service.source_field_state(*PRIMARY, SUBJECT, _context()) is (
        SourceFieldState.UNSUPPORTED
    )


def test_successful_alternative_makes_field_usable(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNSUPPORTED,
        data_as_of=None,
        error_code=SourceErrorCode.HTTP_NOT_FOUND,
        response_bytes=0,
    )
    harness.record(identity=ALTERNATIVE, data_as_of=NOW - timedelta(days=1))

    assert harness.service.resolve_field(*PRIMARY, SUBJECT, _context()) is (
        RequestFieldResolution.USABLE
    )


def test_dated_alternative_is_partial_without_promoting_it(tmp_path) -> None:
    harness = _Harness(tmp_path)
    harness.record(
        outcome=SourceAttemptOutcome.UNAVAILABLE,
        data_as_of=None,
        error_code=SourceErrorCode.PARSE_FAILURE,
        response_bytes=0,
    )
    harness.record(identity=ALTERNATIVE, data_as_of=NOW - timedelta(days=10))

    assert harness.service.resolve_field(*PRIMARY, SUBJECT, _context()) is (
        RequestFieldResolution.PARTIAL
    )


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

    assert harness.service.resolve_field(*PRIMARY, SUBJECT, _context()) is (
        RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
    )


def test_unchecked_or_cooling_alternative_is_not_misreported_as_exhausted(tmp_path) -> None:
    harness = _Harness(tmp_path)
    assert harness.service.resolve_field(*PRIMARY, SUBJECT, _context()) is (
        RequestFieldResolution.PARTIAL
    )

    harness.record(
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        data_as_of=None,
        error_code=SourceErrorCode.TRANSIENT_NETWORK_FAILURE,
        cooldown_until=NOW + INITIAL_COOLDOWN,
        response_bytes=0,
    )
    assert harness.service.resolve_field(*PRIMARY, SUBJECT, _context()) is (
        RequestFieldResolution.PARTIAL
    )


def test_initial_cooldown_is_exactly_thirty_minutes() -> None:
    assert INITIAL_COOLDOWN == timedelta(minutes=30)
    assert SourceHealthService.cooldown_until(NOW) == NOW + timedelta(minutes=30)


def test_only_first_retryable_transient_attempt_with_budget_can_retry(tmp_path) -> None:
    harness = _Harness(tmp_path)
    clock = _Clock(10.0)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="a" * 32,
        monotonic=clock,
        wall_clock=lambda: NOW,
    )

    assert harness.service.retry_allowed(
        _attempt(), budget, minimum_worker_seconds=5.0
    )
    assert not harness.service.retry_allowed(
        _attempt(attempt_number=2), budget, minimum_worker_seconds=5.0
    )

    clock.value = 94.0
    assert not harness.service.retry_allowed(
        _attempt(), budget, minimum_worker_seconds=5.0
    )


@pytest.mark.parametrize(
    "attempt",
    (
        _attempt(
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            error_code=SourceErrorCode.HTTP_4XX,
            cooldown_until=None,
        ),
        _attempt(
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            error_code=SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED,
            cooldown_until=None,
        ),
        _attempt(
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            error_code=SourceErrorCode.IDENTITY_CONFLICT,
            cooldown_until=None,
        ),
        _attempt(
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            error_code=SourceErrorCode.VALIDATION_FAILURE,
            cooldown_until=None,
        ),
        _attempt(
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            error_code=SourceErrorCode.PARSE_FAILURE,
            cooldown_until=None,
        ),
    ),
)
def test_deterministic_failures_never_retry(tmp_path, attempt: SourceAttempt) -> None:
    harness = _Harness(tmp_path)
    budget = RequestBudget.create(
        RequestMode.DEEP,
        request_id="b" * 32,
        monotonic=lambda: 10.0,
        wall_clock=lambda: NOW,
    )

    assert not harness.service.retry_allowed(
        attempt, budget, minimum_worker_seconds=5.0
    )


def test_force_requires_deep_mode_allowlisted_reason_and_first_attempt(tmp_path) -> None:
    service = _Harness(tmp_path).service
    reason = ForceReasonCode.OWNER_APPROVED_RETRY

    with pytest.raises(ValueError, match="deep"):
        service.force_authorization(RequestMode.RAPID, reason, attempt_number=1)
    with pytest.raises(ValueError, match="force reason"):
        service.force_authorization(RequestMode.DEEP, "", attempt_number=1)

    assert service.force_authorization(
        RequestMode.DEEP, reason, attempt_number=1
    ) == ForceAuthorization(actor="local_owner", reason=reason)
    assert service.force_authorization(
        RequestMode.DEEP, reason, attempt_number=2
    ) is None


def test_ordinary_request_never_inherits_prior_force(tmp_path) -> None:
    service = _Harness(tmp_path).service
    forced = service.force_authorization(
        RequestMode.DEEP,
        ForceReasonCode.VERIFY_SOURCE_RECOVERY,
        attempt_number=1,
    )
    assert forced is not None

    assert service.force_authorization(
        RequestMode.RAPID,
        None,
        attempt_number=1,
    ) is None
