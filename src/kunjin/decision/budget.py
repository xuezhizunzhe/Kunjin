from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from kunjin.decision.models import (
    RequestMode,
    validate_aware_datetime,
    validate_identifier,
    validate_request_id,
)

TOTAL_SECONDS = {
    RequestMode.RAPID: 90.0,
    RequestMode.DEEP: 480.0,
}
CLEANUP_RESERVE_SECONDS = 2.0


class BudgetExpired(RuntimeError):
    """Raised when a request result may no longer be published."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_monotonic_time(value: object) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("monotonic clock must return a finite exact float")
    return value


class RequestBudget:
    """A request lifetime governed exclusively by a monotonic deadline."""

    __slots__ = (
        "_cancel_reason",
        "_cancelled",
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
        mode: RequestMode,
        request_id: str,
        total_seconds: float,
        cleanup_reserve_seconds: float,
        monotonic: Callable[[], float],
        monotonic_start: float,
        started_at: datetime,
    ) -> None:
        self.mode = mode
        self.request_id = request_id
        self.total_seconds = total_seconds
        self.cleanup_reserve_seconds = cleanup_reserve_seconds
        self.monotonic = monotonic
        self.monotonic_start = monotonic_start
        self.monotonic_deadline = monotonic_start + total_seconds
        self.started_at = started_at
        self.deadline_at = started_at + timedelta(seconds=total_seconds)
        self._cancelled = False
        self._cancel_reason: Optional[str] = None

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

        monotonic_start = _validate_monotonic_time(monotonic())
        started_at = validate_aware_datetime(wall_clock(), "wall clock")
        return cls(
            mode=mode,
            request_id=request_id,
            total_seconds=TOTAL_SECONDS[mode],
            cleanup_reserve_seconds=CLEANUP_RESERVE_SECONDS,
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
        if self._cancelled:
            return
        validate_identifier(reason, "cancel reason")
        self._cancel_reason = reason
        self._cancelled = True

    def worker_seconds(self) -> float:
        if self._cancelled:
            return 0.0
        current = _validate_monotonic_time(self.monotonic())
        return max(
            0.0,
            self.monotonic_deadline - current - self.cleanup_reserve_seconds,
        )

    def require_publishable(self) -> None:
        if self._cancelled:
            raise BudgetExpired(f"request cancelled: {self._cancel_reason}")
        current = _validate_monotonic_time(self.monotonic())
        if current >= self.monotonic_deadline:
            raise BudgetExpired("request deadline reached")
