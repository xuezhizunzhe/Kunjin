from __future__ import annotations

from decimal import Decimal
from typing import Dict, Sequence

from kunjin.models import PortfolioAnalysis, StoredPosition


def analyze_portfolio(positions: Sequence[StoredPosition]) -> PortfolioAnalysis:
    if not positions:
        return PortfolioAnalysis(
            total_value=None,
            value_kind="missing",
            weights={},
            hhi=None,
            largest_position_share=None,
            observed_profit=None,
            profit_coverage=Decimal("0"),
            evidence_level="insufficient_data",
            warnings=["no portfolio positions are available"],
        )

    values: Dict[str, Decimal] = {}
    warnings = []
    used_estimate = False
    for position in positions:
        nav = position.formal_nav
        if nav is None:
            nav = position.estimated_nav
            used_estimate = nav is not None or used_estimate
        if nav is None:
            warnings.append(f"missing NAV for {position.fund_code}")
            continue
        values[position.fund_code] = values.get(position.fund_code, Decimal("0")) + (
            position.shares * nav
        )

    if len(values) != len({position.fund_code for position in positions}):
        return PortfolioAnalysis(
            total_value=None,
            value_kind="missing",
            weights={},
            hhi=None,
            largest_position_share=None,
            observed_profit=None,
            profit_coverage=Decimal("0"),
            evidence_level="insufficient_data",
            warnings=warnings,
        )

    total_value = sum(values.values(), Decimal("0"))
    if total_value <= 0:
        warnings.append("portfolio value is not positive")
        return PortfolioAnalysis(
            total_value=total_value,
            value_kind="estimated" if used_estimate else "formal",
            weights={},
            hhi=None,
            largest_position_share=None,
            observed_profit=None,
            profit_coverage=Decimal("0"),
            evidence_level="insufficient_data",
            warnings=warnings,
        )

    weights = {code: value / total_value for code, value in values.items()}
    hhi = sum((weight * weight for weight in weights.values()), Decimal("0"))
    profit_values = [position.observed_profit for position in positions if position.observed_profit is not None]
    coverage = Decimal(len(profit_values)) / Decimal(len(positions))
    observed_profit = (
        sum(profit_values, Decimal("0")) if len(profit_values) == len(positions) else None
    )
    if observed_profit is None:
        warnings.append("observed profit has partial coverage")
    if used_estimate:
        warnings.append("portfolio value includes intraday estimated NAV")

    return PortfolioAnalysis(
        total_value=total_value,
        value_kind="estimated" if used_estimate else "formal",
        weights=weights,
        hhi=hhi,
        largest_position_share=max(weights.values()),
        observed_profit=observed_profit,
        profit_coverage=coverage,
        evidence_level="deterministic_calculation",
        warnings=warnings,
    )

