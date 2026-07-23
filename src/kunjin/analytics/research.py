from __future__ import annotations

import math
import statistics
from datetime import timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Sequence

from kunjin.models import FundNavObservation, SectorObservation


def _period_return(history: Sequence[FundNavObservation], days: int) -> Optional[Decimal]:
    latest = history[-1]
    target = latest.nav_date - timedelta(days=days)
    candidates = [item for item in history if item.nav_date <= target]
    if not candidates:
        return None
    baseline = candidates[-1]
    return latest.unit_nav / baseline.unit_nav - Decimal("1")


def analyze_fund_history(history: Sequence[FundNavObservation]) -> Dict[str, Any]:
    ordered = sorted(history, key=lambda item: item.nav_date)
    warnings = [
        "该计算只覆盖本地已保存的正式净值；未计算相对基准超额收益。",
        "经理、费用、规模和季度披露应通过基金复核中的带日期公开资料分别核对。",
    ]
    if len(ordered) < 2:
        return {
            "evidence_level": "insufficient_data",
            "observations": len(ordered),
            "warnings": warnings + ["at least two formal NAV observations are required"],
        }

    daily_returns = [
        float(ordered[index].unit_nav / ordered[index - 1].unit_nav - Decimal("1"))
        for index in range(1, len(ordered))
    ]
    annualized_volatility = Decimal(str(statistics.pstdev(daily_returns) * math.sqrt(252)))

    peak_nav = ordered[0].unit_nav
    peak_date = ordered[0].nav_date
    drawdown_peak_nav = peak_nav
    drawdown_peak_date = peak_date
    max_drawdown = Decimal("0")
    trough_date = ordered[0].nav_date
    for item in ordered:
        if item.unit_nav > peak_nav:
            peak_nav = item.unit_nav
            peak_date = item.nav_date
        drawdown = Decimal("1") - item.unit_nav / peak_nav
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            trough_date = item.nav_date
            drawdown_peak_nav = peak_nav
            drawdown_peak_date = peak_date

    recovery_date = None
    if max_drawdown > 0:
        for item in ordered:
            if item.nav_date > trough_date and item.unit_nav >= drawdown_peak_nav:
                recovery_date = item.nav_date
                break

    return {
        "fund_code": ordered[-1].fund_code,
        "as_of": ordered[-1].nav_date.isoformat(),
        "observations": len(ordered),
        "latest_unit_nav": str(ordered[-1].unit_nav),
        "period_returns": {
            "30d": (
                None
                if _period_return(ordered, 30) is None
                else str(_period_return(ordered, 30))
            ),
            "90d": (
                None
                if _period_return(ordered, 90) is None
                else str(_period_return(ordered, 90))
            ),
            "365d": (
                None
                if _period_return(ordered, 365) is None
                else str(_period_return(ordered, 365))
            ),
        },
        "annualized_volatility": str(annualized_volatility),
        "max_drawdown": str(max_drawdown),
        "drawdown_peak_date": drawdown_peak_date.isoformat(),
        "trough_date": trough_date.isoformat(),
        "recovery_date": None if recovery_date is None else recovery_date.isoformat(),
        "peak_to_trough_days": (
            None if max_drawdown == 0 else (trough_date - drawdown_peak_date).days
        ),
        "trough_to_recovery_days": (
            None if recovery_date is None else (recovery_date - trough_date).days
        ),
        "peak_to_recovery_days": (
            None if recovery_date is None else (recovery_date - drawdown_peak_date).days
        ),
        "evidence_level": "deterministic_calculation",
        "warnings": warnings,
    }


def analyze_sectors(observations: Sequence[SectorObservation], limit: int = 10) -> Dict[str, Any]:
    available = [item for item in observations if item.pct_change is not None]
    ranked = sorted(available, key=lambda item: item.pct_change or Decimal("0"), reverse=True)

    def item_payload(item: SectorObservation) -> Dict[str, Any]:
        total = None
        breadth = None
        if item.advancers is not None and item.decliners is not None:
            total = item.advancers + item.decliners
            breadth = None if total == 0 else Decimal(item.advancers) / Decimal(total)
        return {
            "sector_code": item.sector_code,
            "sector_name": item.sector_name,
            "sector_kind": item.sector_kind,
            "pct_change": None if item.pct_change is None else str(item.pct_change),
            "turnover_rate": None if item.turnover_rate is None else str(item.turnover_rate),
            "breadth": None if breadth is None else str(breadth),
            "retrieved_at": item.retrieved_at.isoformat(),
        }

    return {
        "evidence_level": "verified_fact",
        "scope": "recent_strength_and_breadth_only",
        "top_gainers": [item_payload(item) for item in ranked[:limit]],
        "top_decliners": [item_payload(item) for item in list(reversed(ranked[-limit:]))],
        "warnings": [
            "recent sector strength is not evidence that a sector is suitable to buy",
            "valuation, earnings, capital-flow persistence, catalysts, and crowding are incomplete",
        ],
    }
