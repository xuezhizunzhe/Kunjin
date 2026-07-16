import re
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import RequestMode

UTC_START = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)


def test_rapid_budget_reserves_cleanup_inside_ninety_seconds() -> None:
    ticks = [100.0]
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="a" * 32,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: UTC_START,
    )

    assert budget.total_seconds == 90.0
    assert budget.cleanup_reserve_seconds == 2.0
    assert budget.monotonic_start == 100.0
    assert budget.monotonic_deadline == 190.0
    assert budget.started_at == UTC_START
    assert budget.deadline_at == UTC_START + timedelta(seconds=90)
    assert budget.worker_seconds() == 88.0

    ticks[0] = 187.5
    assert budget.worker_seconds() == 0.5

    ticks[0] = 190.0
    with pytest.raises(BudgetExpired, match="deadline"):
        budget.require_publishable()


def test_deep_budget_is_exactly_four_hundred_eighty_seconds() -> None:
    budget = RequestBudget.create(
        RequestMode.DEEP,
        request_id="b" * 32,
        monotonic=lambda: 20.0,
        wall_clock=lambda: UTC_START,
    )

    assert budget.total_seconds == 480.0
    assert budget.monotonic_deadline == 500.0
    assert budget.deadline_at == UTC_START + timedelta(seconds=480)
    assert budget.worker_seconds() == 478.0


def test_cancelled_budget_never_becomes_publishable_again() -> None:
    ticks = [100.0]
    budget = RequestBudget.create(
        RequestMode.DEEP,
        request_id="c" * 32,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: UTC_START,
    )

    budget.cancel("owner_cancelled")
    budget.cancel("worker_timeout")

    assert budget.cancelled is True
    assert budget.cancel_reason == "owner_cancelled"
    assert budget.worker_seconds() == 0.0
    with pytest.raises(BudgetExpired, match="cancelled: owner_cancelled"):
        budget.require_publishable()

    ticks[0] = 0.0
    with pytest.raises(BudgetExpired, match="cancelled: owner_cancelled"):
        budget.require_publishable()


def test_publishability_uses_monotonic_time_not_wall_clock() -> None:
    ticks = [10.0]
    wall_times = [UTC_START]
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="d" * 32,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: wall_times[0],
    )

    wall_times[0] = UTC_START + timedelta(days=365)
    budget.require_publishable()

    ticks[0] = 100.0
    wall_times[0] = UTC_START - timedelta(days=365)
    with pytest.raises(BudgetExpired, match="deadline"):
        budget.require_publishable()


def test_create_generates_lowercase_uuid_hex_when_request_id_is_omitted() -> None:
    budget = RequestBudget.create(
        RequestMode.RAPID,
        monotonic=lambda: 1.0,
        wall_clock=lambda: UTC_START,
    )

    assert re.fullmatch(r"[0-9a-f]{32}", budget.request_id)


@pytest.mark.parametrize("mode", ["rapid", None, 1, True])
def test_create_rejects_non_exact_request_mode(mode: object) -> None:
    with pytest.raises(ValueError, match="exact RequestMode"):
        RequestBudget.create(mode)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "request_id",
    ["A" * 32, "a" * 31, "a" * 33, "a" * 16 + "-" + "a" * 16, b"a" * 32],
)
def test_create_rejects_invalid_request_id(request_id: object) -> None:
    with pytest.raises(ValueError, match="lowercase UUID hex"):
        RequestBudget.create(RequestMode.RAPID, request_id=request_id)  # type: ignore[arg-type]


@pytest.mark.parametrize("tick", [float("nan"), float("inf"), float("-inf"), 1, True])
def test_create_rejects_non_finite_or_non_float_monotonic_time(tick: object) -> None:
    with pytest.raises(ValueError, match="finite exact float"):
        RequestBudget.create(
            RequestMode.RAPID,
            monotonic=lambda: tick,  # type: ignore[return-value]
            wall_clock=lambda: UTC_START,
        )


@pytest.mark.parametrize(
    "wall_time",
    [datetime(2026, 7, 16, 6, 0), "2026-07-16T06:00:00+00:00", None],
)
def test_create_rejects_invalid_wall_clock_time(wall_time: object) -> None:
    with pytest.raises(ValueError, match="timezone-aware exact datetime"):
        RequestBudget.create(
            RequestMode.RAPID,
            monotonic=lambda: 1.0,
            wall_clock=lambda: wall_time,  # type: ignore[return-value]
        )


@pytest.mark.parametrize("reason", ["", "owner cancelled", "Owner_Cancelled", None, 1])
def test_cancel_rejects_invalid_reason_without_changing_state(reason: object) -> None:
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="e" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: UTC_START,
    )

    with pytest.raises(ValueError, match="lowercase public identifier"):
        budget.cancel(reason)  # type: ignore[arg-type]

    assert budget.cancelled is False
    assert budget.cancel_reason is None
