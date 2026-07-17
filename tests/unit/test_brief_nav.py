from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

import pytest

from kunjin.brief.nav import BoundedNavService, ValidatedAdjustedNavSeries
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    ForceReasonCode,
    RequestMode,
    SourceAttemptOutcome,
    SourceFieldRef,
    SourceFieldState,
    canonical_decimal,
)
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.decision.worker_protocol import (
    FundNavPayload,
    FundNavRow,
    FundNavWorkerResponse,
)
from kunjin.funds.service import SourceRequestContext
from kunjin.models import FundNavObservation
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def _context(
    repository: Repository,
    mode: RequestMode,
    request_id: str,
    *,
    force_reason: Optional[ForceReasonCode] = None,
):
    ticks = [10.0]
    budget = RequestBudget.create(
        mode,
        request_id=request_id,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: NOW,
    )
    audit = DecisionAuditStore(repository)
    run_id = audit.begin_request(budget)
    health = SourceHealthService(audit, wall_clock=lambda: NOW)
    return SourceRequestContext(run_id, budget, audit, health, force_reason), ticks


def _rows(count: int, *, accumulated: bool = True) -> tuple[FundNavRow, ...]:
    return tuple(
        FundNavRow(
            (date(2026, 7, 16) - timedelta(days=index)).isoformat(),
            canonical_decimal(Decimal("1.2") - Decimal(index) / Decimal("10000")),
            canonical_decimal(Decimal("2.2") - Decimal(index) / Decimal("10000"))
            if accumulated
            else None,
            "0.1",
            "none",
        )
        for index in range(count)
    )


def _response(request, rows: tuple[FundNavRow, ...]) -> FundNavWorkerResponse:
    return FundNavWorkerResponse(
        schema_version=1,
        request_id=request.request_id,
        source_id=request.source_id,
        field_id=request.field_id,
        subject_key=request.subject_key,
        operation=request.operation,
        ok=True,
        payload=FundNavPayload(
            fund_code=request.arguments["fund_code"],
            fund_name="测试基金",
            fund_type="混合型",
            retrieved_at=NOW,
            observation_count=len(rows),
            rows=rows,
        ),
        reason_code=None,
        retryable=None,
        message=None,
    )


@pytest.mark.parametrize(
    ("mode", "expected_pages"),
    ((RequestMode.RAPID, "6"), (RequestMode.DEEP, "50")),
)
def test_bounded_nav_uses_mode_page_limit_one_worker_and_two_attempts(
    tmp_path, mode: RequestMode, expected_pages: str
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    request_id = ("6" if mode is RequestMode.RAPID else "5") * 32
    context, _ticks = _context(repository, mode, request_id)
    calls = []

    def worker(request, budget):
        calls.append(request)
        assert budget is context.budget
        return _response(request, _rows(60))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert len(calls) == 1
    assert calls[0].arguments == {
        "fund_code": "123456",
        "max_pages": expected_pages,
    }
    assert result.formal_nav_status == "success"
    assert result.adjusted_series_status == "success"
    stored_history = repository.fund_history("123456")
    assert len(stored_history) == 60
    assert len({item.source_attempt_id for item in stored_history}) == 1
    assert stored_history[0].source_attempt_id is not None
    attempts = context.audit_store.source_attempt_history(
        "eastmoney_nav", "formal_nav", "fund:123456"
    ) + context.audit_store.source_attempt_history(
        "eastmoney_nav", "adjusted_return_series", "fund:123456"
    )
    assert {item.attempt.outcome for item in attempts} == {SourceAttemptOutcome.SUCCESS}


def test_formal_nav_persists_when_adjusted_series_is_insufficient(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "a" * 32)

    def worker(request, _budget):
        return _response(request, _rows(59))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert result.formal_nav_status == "success"
    assert result.adjusted_series_status == "insufficient"
    assert len(repository.fund_history("123456")) == 59
    formal = context.audit_store.source_attempt_history(
        "eastmoney_nav", "formal_nav", "fund:123456"
    )
    adjusted = context.audit_store.source_attempt_history(
        "eastmoney_nav", "adjusted_return_series", "fund:123456"
    )
    assert formal[0].attempt.outcome is SourceAttemptOutcome.SUCCESS
    assert adjusted[0].attempt.outcome is SourceAttemptOutcome.UNAVAILABLE


def test_insufficient_adjusted_series_uses_successful_cache_without_refetch(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    expected = datetime(2026, 7, 16, tzinfo=timezone.utc)
    first_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "7" * 32,
    )

    def first_worker(request, _budget):
        return _response(request, _rows(59))

    first = BoundedNavService(repository, worker_runner=first_worker).sync(
        "123456",
        first_context,
        latest_expected_data_as_of=expected,
    )
    assert first.adjusted_series_status == "insufficient"

    second_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "8" * 32,
    )

    def unexpected_worker(_request, _budget):
        raise AssertionError("successful source cache must not refetch")

    second = BoundedNavService(repository, worker_runner=unexpected_worker).sync(
        "123456",
        second_context,
        latest_expected_data_as_of=expected,
    )

    assert second.status == "cache_hit"
    assert second.formal_nav_status == "success"
    assert second.adjusted_series_status == "insufficient"


def test_valid_adjusted_series_remains_authenticated_on_cache_hit(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    expected = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    first_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "9" * 32,
    )

    def first_worker(request, _budget):
        return _response(request, _rows(60))

    first = BoundedNavService(repository, worker_runner=first_worker).sync(
        "123456",
        first_context,
        latest_expected_data_as_of=expected,
    )
    assert first.adjusted_series_status == "success"

    repository.save_fund_history(
        "123456",
        "测试基金",
        "混合型",
        "eastmoney",
        (
            FundNavObservation(
                fund_code="123456",
                nav_date=date(2020, 1, 2),
                unit_nav=Decimal("1"),
                accumulated_nav=Decimal("1"),
                daily_growth=Decimal("0"),
                source="eastmoney",
                retrieved_at=NOW + timedelta(seconds=1),
                corporate_action_state="unknown",
            ),
        ),
    )

    second_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "0" * 32,
    )
    second = BoundedNavService(
        repository,
        worker_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("validated cache must not refetch")
        ),
    ).sync(
        "123456",
        second_context,
        latest_expected_data_as_of=expected,
    )

    assert second.status == "cache_hit"
    assert second.formal_nav_status == "success"
    assert second.adjusted_series_status == "cache"


def test_validated_adjusted_series_returns_only_the_authenticated_batch(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    expected = datetime(2026, 7, 16, tzinfo=timezone.utc)
    context, _ticks = _context(repository, RequestMode.RAPID, "2" * 32)
    service = BoundedNavService(
        repository,
        worker_runner=lambda request, _budget: _response(request, _rows(61)),
    )
    result = service.sync(
        "123456",
        context,
        latest_expected_data_as_of=expected,
    )
    assert result.adjusted_series_status == "success"

    series = service.validated_adjusted_series(
        "123456",
        context,
        latest_expected_data_as_of=expected,
    )

    assert type(series) is ValidatedAdjustedNavSeries
    assert series.fund_code == "123456"
    assert len(series.observations) == 61
    assert tuple(item.nav_date for item in series.observations) == tuple(
        sorted(item.nav_date for item in series.observations)
    )
    assert series.data_as_of == date(2026, 7, 16)
    assert series.retrieved_at == NOW
    assert series.source_attempt_id > 0
    assert {item.source_attempt_id for item in series.observations} == {series.source_attempt_id}


def test_validated_adjusted_series_rejects_mixed_or_unbound_selected_batch(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    expected = datetime(2026, 7, 16, tzinfo=timezone.utc)
    context, _ticks = _context(repository, RequestMode.RAPID, "4" * 32)
    service = BoundedNavService(
        repository,
        worker_runner=lambda request, _budget: _response(request, _rows(61)),
    )
    result = service.sync(
        "123456",
        context,
        latest_expected_data_as_of=expected,
    )
    assert result.adjusted_series_status == "success"

    with repository.connect() as connection, connection:
        connection.execute(
            """
            UPDATE fund_nav
            SET source_attempt_id = NULL
            WHERE fund_code = '123456' AND nav_date = '2026-06-30'
            """
        )

    assert (
        service.validated_adjusted_series(
            "123456",
            context,
            latest_expected_data_as_of=expected,
        )
        is None
    )

    repository.save_fund_history(
        "123456",
        "测试基金",
        "混合型",
        "eastmoney",
        (
            FundNavObservation(
                fund_code="123456",
                nav_date=date(2026, 7, 17),
                unit_nav=Decimal("1.3"),
                accumulated_nav=Decimal("2.3"),
                daily_growth=Decimal("0.1"),
                source="eastmoney",
                retrieved_at=NOW + timedelta(seconds=1),
                corporate_action_state="none",
            ),
        ),
    )
    assert (
        service.validated_adjusted_series(
            "123456",
            context,
            latest_expected_data_as_of=expected,
        )
        is None
    )


def test_cache_quality_uses_only_latest_authenticated_retrieval_batch(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    repository.save_fund_history(
        "123456",
        "测试基金",
        "混合型",
        "eastmoney",
        (
            FundNavObservation(
                fund_code="123456",
                nav_date=date(2020, 1, 1),
                unit_nav=Decimal("1"),
                accumulated_nav=Decimal("1"),
                daily_growth=Decimal("0"),
                source="eastmoney",
                retrieved_at=NOW - timedelta(days=1),
                corporate_action_state="unknown",
            ),
        ),
    )
    expected = datetime(2026, 7, 16, tzinfo=timezone.utc)
    first_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "3" * 32,
    )

    def first_worker(request, _budget):
        return _response(request, _rows(60))

    first = BoundedNavService(repository, worker_runner=first_worker).sync(
        "123456",
        first_context,
        latest_expected_data_as_of=expected,
    )
    assert first.adjusted_series_status == "success"

    second_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "c" * 32,
    )
    second = BoundedNavService(
        repository,
        worker_runner=lambda *_args: (_ for _ in ()).throw(
            AssertionError("latest authenticated batch must be reusable")
        ),
    ).sync(
        "123456",
        second_context,
        latest_expected_data_as_of=expected,
    )

    assert second.status == "cache_hit"
    assert second.records == 60
    assert second.latest_nav_date == "2026-07-16"
    assert second.adjusted_series_status == "cache"


def test_advanced_expected_trading_date_attempts_fresh_network_data(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    calls = []

    def worker(request, _budget):
        calls.append(request)
        return _response(request, _rows(60))

    first_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "d" * 32,
    )
    BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        first_context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    second_context, _ticks = _context(
        repository,
        RequestMode.RAPID,
        "e" * 32,
    )
    second = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        second_context,
        latest_expected_data_as_of=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )

    assert len(calls) == 2
    assert second.status == "partial"
    assert second.formal_nav_status == "stale"


@pytest.mark.parametrize(
    ("latest_expected", "expected_status"),
    (
        (datetime(2026, 7, 17, tzinfo=timezone.utc), "stale"),
        (None, "unknown_current"),
    ),
)
def test_formal_nav_currentness_requires_trusted_expected_date(
    tmp_path,
    latest_expected: Optional[datetime],
    expected_status: str,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "e" * 32)

    def worker(request, _budget):
        return _response(request, _rows(60))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=latest_expected,
    )

    assert result.formal_nav_status == expected_status


def test_formal_nav_uses_callers_trading_date_not_utc_calendar_shift(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "6" * 32)

    def worker(request, _budget):
        return _response(request, _rows(60))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(
            2026,
            7,
            17,
            tzinfo=timezone(timedelta(hours=8)),
        ),
    )

    assert result.formal_nav_status == "stale"


@pytest.mark.parametrize("action_state", ("present", "unknown"))
def test_unresolved_corporate_action_does_not_authenticate_adjusted_series(
    tmp_path,
    action_state: str,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "f" * 32)
    rows = list(_rows(60))
    rows[30] = replace(rows[30], corporate_action_state=action_state)

    def worker(request, _budget):
        return _response(request, tuple(rows))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert result.formal_nav_status == "success"
    assert result.adjusted_series_status == "insufficient"
    adjusted = context.audit_store.source_attempt_history(
        "eastmoney_nav", "adjusted_return_series", "fund:123456"
    )
    assert adjusted[0].attempt.outcome is SourceAttemptOutcome.UNAVAILABLE


def test_accumulated_nav_breakpoint_does_not_authenticate_adjusted_series(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "5" * 32)
    rows = list(_rows(60))
    rows[30] = replace(rows[30], accumulated_nav="999")

    def worker(request, _budget):
        return _response(request, tuple(rows))

    result = BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    assert result.formal_nav_status == "success"
    assert result.adjusted_series_status == "insufficient"


def test_source_attempt_finished_at_uses_trusted_parent_clock(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "1" * 32)
    trusted_finish = NOW + timedelta(seconds=5)
    context.health_service.wall_clock = lambda: trusted_finish

    def worker(request, _budget):
        return _response(request, _rows(60))

    BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    formal = context.audit_store.source_attempt_history(
        "eastmoney_nav", "formal_nav", "fund:123456"
    )
    assert formal[0].attempt.finished_at == trusted_finish


@pytest.mark.parametrize("cooldown_field", ("formal_nav", "adjusted_return_series"))
def test_cooldown_on_either_field_blocks_shared_worker_group(tmp_path, cooldown_field: str) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "b" * 32)
    projections = tuple(
        SimpleNamespace(
            state=(
                SourceFieldState.COOLDOWN
                if field == cooldown_field
                else SourceFieldState.NOT_CHECKED
            ),
            history=SimpleNamespace(
                reference=SourceFieldRef("eastmoney_nav", field),
                attempts=(
                    (
                        SimpleNamespace(
                            attempt=SimpleNamespace(cooldown_until=NOW + timedelta(hours=1))
                        ),
                    )
                    if field == cooldown_field
                    else ()
                ),
            ),
        )
        for field in ("formal_nav", "adjusted_return_series")
    )
    snapshot = SimpleNamespace(projections=projections, resolutions=(), evaluated_at=NOW)

    with (
        patch.object(context.health_service, "source_status_snapshot", return_value=snapshot),
        patch("kunjin.brief.nav.run_fund_nav_worker") as worker,
    ):
        result = BoundedNavService(repository).sync("123456", context)

    worker.assert_not_called()
    assert result.status == "skipped_cooldown"
    for field in ("formal_nav", "adjusted_return_series"):
        attempt = context.audit_store.source_attempt_history("eastmoney_nav", field, "fund:123456")[
            0
        ].attempt
        assert attempt.outcome is SourceAttemptOutcome.SKIPPED_COOLDOWN


def test_deep_force_bypasses_group_cooldown_with_two_field_authorizations(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(
        repository,
        RequestMode.DEEP,
        "2" * 32,
        force_reason=ForceReasonCode.VERIFY_SOURCE_RECOVERY,
    )
    projections = tuple(
        SimpleNamespace(
            state=(
                SourceFieldState.COOLDOWN if field == "formal_nav" else SourceFieldState.NOT_CHECKED
            ),
            history=SimpleNamespace(
                reference=SourceFieldRef("eastmoney_nav", field),
                attempts=(
                    (
                        SimpleNamespace(
                            attempt=SimpleNamespace(cooldown_until=NOW + timedelta(hours=1))
                        ),
                    )
                    if field == "formal_nav"
                    else ()
                ),
            ),
        )
        for field in ("formal_nav", "adjusted_return_series")
    )
    snapshot = SimpleNamespace(projections=projections, resolutions=(), evaluated_at=NOW)
    calls = []

    def worker(request, _budget):
        calls.append(request)
        return _response(request, _rows(60))

    with patch.object(
        context.health_service,
        "source_status_snapshot",
        return_value=snapshot,
    ):
        result = BoundedNavService(repository, worker_runner=worker).sync(
            "123456",
            context,
            latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )

    assert result.status == "success"
    assert len(calls) == 1
    for field in ("formal_nav", "adjusted_return_series"):
        stored = context.audit_store.source_attempt_history("eastmoney_nav", field, "fund:123456")[
            0
        ]
        assert stored.authorization_id is not None
        assert stored.attempt.force_actor == "local_owner"


def test_deep_force_attempt_lifetime_starts_at_atomic_reservation(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(
        repository,
        RequestMode.DEEP,
        "4" * 32,
        force_reason=ForceReasonCode.VERIFY_SOURCE_RECOVERY,
    )
    clock_calls = [0]

    def advancing_clock():
        clock_calls[0] += 1
        return NOW + timedelta(seconds=clock_calls[0])

    context.health_service.wall_clock = advancing_clock

    def worker(request, _budget):
        with repository.connect() as connection:
            reserved_at = datetime.fromisoformat(
                str(
                    connection.execute(
                        """
                        SELECT reserved_at
                        FROM source_work_authorizations
                        WHERE request_run_id = ? AND kind = 'force'
                        ORDER BY id LIMIT 1
                        """,
                        (context.request_run_id,),
                    ).fetchone()[0]
                )
            )
        response = _response(request, _rows(60))
        assert response.payload is not None
        return replace(
            response,
            payload=replace(response.payload, retrieved_at=reserved_at),
        )

    BoundedNavService(repository, worker_runner=worker).sync(
        "123456",
        context,
        latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    with repository.connect() as connection:
        reservations = {
            str(row["field_id"]): datetime.fromisoformat(str(row["reserved_at"]))
            for row in connection.execute(
                """
                SELECT field_id, reserved_at
                FROM source_work_authorizations
                WHERE request_run_id = ? AND kind = 'force'
                """,
                (context.request_run_id,),
            ).fetchall()
        }

    for field in ("formal_nav", "adjusted_return_series"):
        stored = context.audit_store.source_attempt_history(
            "eastmoney_nav",
            field,
            "fund:123456",
        )[0]
        assert stored.attempt.started_at == reservations[field]
        assert stored.attempt.finished_at > stored.attempt.started_at


def test_deep_force_reserves_both_fields_atomically(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(
        repository,
        RequestMode.DEEP,
        "3" * 32,
        force_reason=ForceReasonCode.VERIFY_SOURCE_RECOVERY,
    )
    with repository.connect() as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER reject_adjusted_force_reservation
            BEFORE INSERT ON source_work_authorizations
            WHEN NEW.kind = 'force'
              AND NEW.field_id = 'adjusted_return_series'
            BEGIN
                SELECT RAISE(ABORT, 'injected second reservation failure');
            END
            """
        )

    with pytest.raises(DecisionAuditStoreError, match="reservation"):
        BoundedNavService(repository, worker_runner=lambda *_args: None).sync(
            "123456",
            context,
            latest_expected_data_as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
        )

    with repository.connect() as connection:
        count = connection.execute(
            "SELECT count(*) FROM source_work_authorizations WHERE request_run_id = ?",
            (context.request_run_id,),
        ).fetchone()[0]
    assert count == 0


def test_nav_and_both_attempts_roll_back_when_second_attempt_write_fails(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "c" * 32)

    def worker(request, _budget):
        return _response(request, _rows(60))

    original = context.audit_store.record_source_attempt
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second audit write failed")
        return original(*args, **kwargs)

    with patch.object(context.audit_store, "record_source_attempt", side_effect=fail_second):
        with pytest.raises(RuntimeError, match="second audit"):
            BoundedNavService(repository, worker_runner=worker).sync("123456", context)

    assert repository.fund_history("123456") == []
    assert (
        context.audit_store.source_attempt_history("eastmoney_nav", "formal_nav", "fund:123456")
        == ()
    )


def test_cancelled_late_worker_result_never_reaches_sqlite(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, RequestMode.RAPID, "d" * 32)

    def worker(request, budget):
        response = _response(request, _rows(60))
        budget.cancel("test_cancelled")
        return response

    with pytest.raises(BudgetExpired):
        BoundedNavService(repository, worker_runner=worker).sync("123456", context)

    assert repository.fund_history("123456") == []
    assert (
        context.audit_store.source_attempt_history("eastmoney_nav", "formal_nav", "fund:123456")
        == ()
    )
