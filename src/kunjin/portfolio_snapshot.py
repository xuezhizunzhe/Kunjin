"""Bounded consistency checks for read-only portfolio projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from typing import Generic, TypeVar

from kunjin.models import StoredPosition

T = TypeVar("T")


@dataclass(frozen=True)
class ConsistentPortfolioProjection(Generic[T]):
    """A portfolio-dependent result, or an explicit stale result after two changes."""

    value: T | None
    binding: dict[str, object]
    stability_state: str
    read_count: int


def run_consistent_portfolio_projection(
    read_snapshot: Callable[[], tuple[Mapping[str, object] | None, Sequence[StoredPosition]]],
    project: Callable[
        [dict[str, object], Mapping[str, object] | None, Sequence[StoredPosition]], T
    ],
) -> ConsistentPortfolioProjection[T]:
    """Run a projection against one stable snapshot, retrying only once after change."""

    initial_sync, initial_positions = read_snapshot()
    initial_binding = build_portfolio_snapshot_binding(initial_sync, initial_positions)
    initial_value = project(initial_binding, initial_sync, initial_positions)
    current_sync, current_positions = read_snapshot()
    current_binding = build_portfolio_snapshot_binding(current_sync, current_positions)
    if current_binding == initial_binding:
        return ConsistentPortfolioProjection(initial_value, initial_binding, "stable", 1)

    recalculated_value = project(current_binding, current_sync, current_positions)
    final_sync, final_positions = read_snapshot()
    final_binding = build_portfolio_snapshot_binding(final_sync, final_positions)
    if final_binding == current_binding:
        return ConsistentPortfolioProjection(
            recalculated_value, current_binding, "recomputed_after_change", 2
        )
    return ConsistentPortfolioProjection(
        None, final_binding, "changed_during_recalculation", 2
    )


def build_portfolio_snapshot_binding(
    sync: Mapping[str, object] | None,
    positions: Sequence[StoredPosition],
) -> dict[str, object]:
    """Return a public, positive-position-only identity for one portfolio observation."""

    positive = sorted(
        (position for position in positions if position.shares > 0),
        key=lambda item: (item.account_title, item.fund_code, item.share_class or ""),
    )
    sync_id = _sync_id(sync)
    observed_at = max(
        (position.observed_at.astimezone(timezone.utc) for position in positive), default=None
    )
    fingerprint_input = [
        {
            "account_title": position.account_title,
            "fund_code": position.fund_code,
            "share_class": position.share_class,
            "shares": format(position.shares, "f"),
        }
        for position in positive
    ]
    return {
        "sync_run_id": sync_id,
        "observation_version": f"sync_run_{sync_id}" if sync_id is not None else "unbound",
        "observation_at": None if observed_at is None else observed_at.isoformat(),
        "positive_position_count": len(positive),
        "positive_position_fingerprint": hashlib.sha256(
            json.dumps(
                fingerprint_input,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }


def _sync_id(sync: Mapping[str, object] | None) -> int | None:
    if sync is None:
        return None
    value = sync.get("id")
    if type(value) is not int or value <= 0:
        raise ValueError("portfolio sync binding is invalid")
    return value
