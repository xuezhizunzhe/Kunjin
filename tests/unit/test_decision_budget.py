import re
from datetime import datetime, timedelta, timezone

import pytest

import kunjin.decision.budget as budget_module
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


def _constructor_values(**overrides: object) -> dict:
    values = {
        "_token": budget_module._CONSTRUCTION_TOKEN,
        "mode": RequestMode.RAPID,
        "request_id": "f" * 32,
        "total_seconds": 90.0,
        "cleanup_reserve_seconds": 2.0,
        "monotonic": lambda: 100.0,
        "monotonic_start": 100.0,
        "started_at": UTC_START,
    }
    values.update(overrides)
    return values


def test_direct_constructor_is_rejected_without_private_factory_token() -> None:
    values = _constructor_values()
    values.pop("_token")

    with pytest.raises(ValueError, match="factory"):
        RequestBudget(**values)  # type: ignore[arg-type]


def test_controlled_constructor_rejects_rapid_budget_with_deep_total() -> None:
    with pytest.raises(ValueError, match="total seconds"):
        RequestBudget(**_constructor_values(total_seconds=480.0))  # type: ignore[arg-type]


def test_controlled_constructor_rejects_negative_cleanup_reserve() -> None:
    with pytest.raises(ValueError, match="cleanup reserve"):
        RequestBudget(  # type: ignore[arg-type]
            **_constructor_values(cleanup_reserve_seconds=-2.0)
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("mode", RequestMode.DEEP),
        ("request_id", "0" * 32),
        ("total_seconds", 480.0),
        ("cleanup_reserve_seconds", 0.0),
        ("monotonic_start", 0.0),
        ("monotonic_deadline", 1_000.0),
        ("started_at", UTC_START + timedelta(days=1)),
        ("deadline_at", UTC_START + timedelta(days=1)),
        ("monotonic", lambda: 0.0),
        ("_last_monotonic", 0.0),
        ("_cancelled", True),
        ("_cancel_reason", "owner_cancelled"),
    ],
)
def test_budget_fields_reject_normal_assignment(field: str, value: object) -> None:
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="1" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: UTC_START,
    )

    with pytest.raises(AttributeError, match="read-only"):
        setattr(budget, field, value)


def test_budget_fields_reject_deletion() -> None:
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="5" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: UTC_START,
    )

    with pytest.raises(AttributeError, match="read-only"):
        del budget.monotonic_deadline


def test_cancelled_state_cannot_be_reversed_by_normal_assignment() -> None:
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="2" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: UTC_START,
    )
    budget.cancel("owner_cancelled")

    with pytest.raises(AttributeError, match="read-only"):
        budget._cancelled = False

    assert budget.cancelled is True
    with pytest.raises(BudgetExpired, match="cancelled"):
        budget.require_publishable()


def test_total_seconds_policy_cannot_be_mutated() -> None:
    assert not hasattr(budget_module, "TOTAL_SECONDS")
    with pytest.raises(TypeError):
        budget_module._TOTAL_SECONDS[RequestMode.RAPID] = 480.0


def test_wall_clock_audit_timestamps_are_normalized_to_utc() -> None:
    utc_plus_eight = timezone(timedelta(hours=8))
    local_start = datetime(2026, 7, 16, 14, 0, tzinfo=utc_plus_eight)

    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="3" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: local_start,
    )

    assert budget.started_at == UTC_START
    assert budget.started_at.tzinfo is timezone.utc
    assert budget.deadline_at == UTC_START + timedelta(seconds=90)
    assert budget.deadline_at.tzinfo is timezone.utc


def test_monotonic_clock_regression_fails_closed_without_expanding_budget() -> None:
    ticks = [100.0]
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="4" * 32,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: UTC_START,
    )

    ticks[0] = 110.0
    assert budget.worker_seconds() == 78.0
    ticks[0] = 105.0
    with pytest.raises(BudgetExpired, match="regressed"):
        budget.worker_seconds()

    assert budget.cancelled is True
    assert budget.cancel_reason == "monotonic_clock_regressed"
    assert budget.worker_seconds() == 0.0
    ticks[0] = 120.0
    with pytest.raises(BudgetExpired, match="cancelled"):
        budget.require_publishable()
