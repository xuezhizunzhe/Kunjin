from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Optional

from kunjin.decision.models import (
    RequestMode,
    validate_aware_datetime,
    validate_identifier,
    validate_request_id,
)

_TOTAL_SECONDS = MappingProxyType(
    {
        RequestMode.RAPID: 90.0,
        RequestMode.DEEP: 480.0,
    }
)
CLEANUP_RESERVE_SECONDS = 2.0
_CONSTRUCTION_TOKEN = object()


class BudgetExpired(RuntimeError):
    """Raised when a request result may no longer be published."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_monotonic_time(value: object) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("monotonic clock must return a finite exact float")
    return value


class RequestBudget:
    """A single-parent request lifetime governed by a monotonic deadline.

    The controlling parent path owns the instance; it is not shared across threads.
    """

    __slots__ = (
        "_cancel_reason",
        "_cancelled",
        "_last_monotonic",
        "cleanup_reserve_seconds",
        "deadline_at",
        "mode",
        "monotonic",
        "monotonic_deadline",
        "monotonic_start",
        "request_id",
        "started_at",
        "total_seconds",
    )

    def __init__(
        self,
        *,
        _token: object = None,
        mode: RequestMode,
        request_id: str,
        total_seconds: float,
        cleanup_reserve_seconds: float,
        monotonic: Callable[[], float],
        monotonic_start: float,
        started_at: datetime,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise ValueError("RequestBudget must be created through its factory")
        if type(mode) is not RequestMode:
            raise ValueError("mode must be an exact RequestMode")
        validate_request_id(request_id)
        expected_total = 90.0 if mode is RequestMode.RAPID else 480.0
        if (
            type(total_seconds) is not float
            or not math.isfinite(total_seconds)
            or total_seconds != expected_total
        ):
            raise ValueError("total seconds must match the request mode")
        if (
            type(cleanup_reserve_seconds) is not float
            or not math.isfinite(cleanup_reserve_seconds)
            or cleanup_reserve_seconds <= 0.0
            or cleanup_reserve_seconds >= total_seconds
            or cleanup_reserve_seconds != 2.0
        ):
            raise ValueError("cleanup reserve must be the positive bounded policy value")
        if not callable(monotonic):
            raise ValueError("monotonic clock must be callable")
        monotonic_start = _validate_monotonic_time(monotonic_start)
        started_at = validate_aware_datetime(started_at, "started at")
        if started_at.utcoffset() != timedelta(0):
            raise ValueError("started at must be UTC")
        started_at = started_at.astimezone(timezone.utc)
        monotonic_deadline = monotonic_start + total_seconds
        if not math.isfinite(monotonic_deadline) or monotonic_deadline <= monotonic_start:
            raise ValueError("monotonic deadline must be finite and after its start")
        try:
            deadline_at = started_at + timedelta(seconds=total_seconds)
        except OverflowError:
            raise ValueError("audit deadline is outside the datetime range") from None

        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "total_seconds", total_seconds)
        object.__setattr__(self, "cleanup_reserve_seconds", cleanup_reserve_seconds)
        object.__setattr__(self, "monotonic", monotonic)
        object.__setattr__(self, "monotonic_start", monotonic_start)
        object.__setattr__(self, "monotonic_deadline", monotonic_deadline)
        object.__setattr__(self, "_last_monotonic", monotonic_start)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "deadline_at", deadline_at)
        object.__setattr__(self, "_cancelled", False)
        object.__setattr__(self, "_cancel_reason", None)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"RequestBudget field {name!r} is read-only")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"RequestBudget field {name!r} is read-only")

    @classmethod
    def create(
        cls,
        mode: RequestMode,
        *,
        request_id: Optional[str] = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> RequestBudget:
        if type(mode) is not RequestMode:
            raise ValueError("mode must be an exact RequestMode")
        if request_id is None:
            request_id = uuid.uuid4().hex
        validate_request_id(request_id)
        if not callable(monotonic):
            raise ValueError("monotonic clock must be callable")
        if not callable(wall_clock):
            raise ValueError("wall clock must be callable")

        monotonic_start = _validate_monotonic_time(monotonic())
        started_at = validate_aware_datetime(wall_clock(), "wall clock").astimezone(
            timezone.utc
        )
        return cls(
            _token=_CONSTRUCTION_TOKEN,
            mode=mode,
            request_id=request_id,
            total_seconds=90.0 if mode is RequestMode.RAPID else 480.0,
            cleanup_reserve_seconds=2.0,
            monotonic=monotonic,
            monotonic_start=monotonic_start,
            started_at=started_at,
        )

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def cancel_reason(self) -> Optional[str]:
        return self._cancel_reason

    def cancel(self, reason: str) -> None:
        self._cancel(reason)

    def _cancel(self, reason: str) -> None:
        if self._cancelled:
            return
        validate_identifier(reason, "cancel reason")
        object.__setattr__(self, "_cancel_reason", reason)
        object.__setattr__(self, "_cancelled", True)

    def _read_monotonic(self) -> float:
        try:
            current = _validate_monotonic_time(self.monotonic())
        except Exception:
            self._cancel("monotonic_clock_invalid")
            raise BudgetExpired("monotonic clock returned an invalid reading") from None
        if current < self._last_monotonic:
            self._cancel("monotonic_clock_regressed")
            raise BudgetExpired("monotonic clock regressed")
        object.__setattr__(self, "_last_monotonic", current)
        return current

    def worker_seconds(self) -> float:
        if self._cancelled:
            return 0.0
        current = self._read_monotonic()
        return max(
            0.0,
            self.monotonic_deadline - current - self.cleanup_reserve_seconds,
        )

    def require_publishable(self) -> None:
        if self._cancelled:
            raise BudgetExpired(f"request cancelled: {self._cancel_reason}")
        current = self._read_monotonic()
        if current >= self.monotonic_deadline:
            raise BudgetExpired("request deadline reached")
