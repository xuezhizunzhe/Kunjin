from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from kunjin.models import StoredPosition
from kunjin.portfolio_snapshot import run_consistent_portfolio_projection


def _position(code: str, shares: str, observed_at: datetime) -> StoredPosition:
    return StoredPosition(
        account_title="默认",
        fund_code=code,
        fund_name=f"基金{code}",
        shares=Decimal(shares),
        observed_at=observed_at,
        formal_nav=Decimal("1"),
    )


def _snapshot(
    sync_id: int, positions: tuple[StoredPosition, ...]
) -> tuple[dict[str, object], tuple[StoredPosition, ...]]:
    return ({"id": sync_id, "source": "yangjibao", "status": "success"}, positions)


def test_changed_snapshot_recomputes_once_and_never_returns_old_projection() -> None:
    old_at = datetime(2026, 7, 23, 1, 4, 59, tzinfo=timezone.utc)
    new_at = datetime(2026, 7, 23, 1, 6, 9, tzinfo=timezone.utc)
    old = _snapshot(46, (_position("000001", "1", old_at),))
    new = _snapshot(
        47,
        (
            _position("000001", "1", new_at),
            _position("000002", "1", new_at),
        ),
    )
    snapshots = iter((old, new, new))
    projected_sync_ids: list[int] = []

    result = run_consistent_portfolio_projection(
        lambda: next(snapshots),
        lambda binding, _sync, positions: projected_sync_ids.append(binding["sync_run_id"])
        or {"position_count": len([item for item in positions if item.shares > 0])},
    )

    assert projected_sync_ids == [46, 47]
    assert result.value == {"position_count": 2}
    assert result.binding["sync_run_id"] == 47
    assert result.binding["positive_position_count"] == 2
    assert result.stability_state == "recomputed_after_change"
    assert result.read_count == 2


def test_unchanged_snapshot_does_not_repeat_projection() -> None:
    observed_at = datetime(2026, 7, 23, 1, 6, 9, tzinfo=timezone.utc)
    snapshot = _snapshot(47, (_position("000001", "1", observed_at),))
    projected = []

    result = run_consistent_portfolio_projection(
        lambda: snapshot,
        lambda binding, _sync, _positions: projected.append(binding["sync_run_id"]) or "current",
    )

    assert projected == [47]
    assert result.value == "current"
    assert result.stability_state == "stable"
    assert result.read_count == 1


def test_second_change_discards_portfolio_projection_instead_of_mixing_versions() -> None:
    first_at = datetime(2026, 7, 23, 1, 4, 59, tzinfo=timezone.utc)
    second_at = datetime(2026, 7, 23, 1, 6, 9, tzinfo=timezone.utc)
    third_at = datetime(2026, 7, 23, 1, 8, 0, tzinfo=timezone.utc)
    snapshots = iter(
        (
            _snapshot(46, (_position("000001", "1", first_at),)),
            _snapshot(47, (_position("000001", "1", second_at),)),
            _snapshot(48, (_position("000001", "1", third_at),)),
        )
    )

    result = run_consistent_portfolio_projection(
        lambda: next(snapshots),
        lambda binding, _sync, _positions: {"sync_run_id": binding["sync_run_id"]},
    )

    assert result.value is None
    assert result.binding["sync_run_id"] == 48
    assert result.stability_state == "changed_during_recalculation"
    assert result.read_count == 2


def test_positive_position_binding_excludes_zero_share_history() -> None:
    observed_at = datetime(2026, 7, 23, 1, 6, 9, tzinfo=timezone.utc)
    snapshot = _snapshot(
        47,
        (
            _position("000001", "1", observed_at),
            _position("000002", "0", observed_at),
        ),
    )

    result = run_consistent_portfolio_projection(
        lambda: snapshot,
        lambda _binding, _sync, positions: [
            item.fund_code for item in positions if item.shares > 0
        ],
    )

    assert result.binding["positive_position_count"] == 1
    assert result.value == ["000001"]
