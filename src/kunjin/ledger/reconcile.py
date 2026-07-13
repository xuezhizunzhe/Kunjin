from __future__ import annotations

from decimal import Decimal
from typing import Iterable, List, Optional, Tuple

from kunjin.ledger.models import (
    EvidenceLevel,
    LedgerDraft,
    LedgerTransaction,
    ReconciliationResult,
    TransactionType,
)
from kunjin.models import StoredPosition

_ACQUISITION_TYPES = {
    TransactionType.SUBSCRIPTION,
    TransactionType.RECURRING_SUBSCRIPTION,
}
_UNSUPPORTED_COST_BASIS_TYPES = {
    TransactionType.REDEMPTION,
    TransactionType.CONVERSION_IN,
    TransactionType.CONVERSION_OUT,
    TransactionType.CASH_DIVIDEND,
    TransactionType.REINVESTED_DIVIDEND,
}
_CONFIRMED_EVIDENCE = {
    EvidenceLevel.TRANSACTION_CONFIRMED.value,
    EvidenceLevel.USER_CONFIRMED.value,
}


def reconcile_fund(
    position: StoredPosition,
    transactions: Iterable[LedgerTransaction],
    pending_drafts: Iterable[LedgerDraft],
) -> ReconciliationResult:
    """Compare confirmed ledger cash flow with a position-inferred cost.

    The inferred cost is an observation-derived consistency check. It is not a
    confirmed Alipay cost basis and must retain ``position_inferred`` evidence.
    """

    warnings: List[str] = []
    current_nav = _position_nav(position, warnings)
    inferred_position_cost = _inferred_position_cost(position, current_nav, warnings)
    confirmed_cash_flow, cash_flow_complete = _confirmed_cash_flow(
        position.fund_code, transactions, warnings
    )
    pending_inflow_total = _pending_inflow_total(
        position.fund_code, pending_drafts, warnings
    )

    difference: Optional[Decimal] = None
    tolerance: Optional[Decimal] = None
    if inferred_position_cost is not None:
        tolerance = max(
            Decimal("0.02"), abs(inferred_position_cost) * Decimal("0.002")
        )
    if confirmed_cash_flow is not None and inferred_position_cost is not None:
        difference = confirmed_cash_flow - inferred_position_cost

    if difference is None or tolerance is None or not cash_flow_complete:
        status = "insufficient_data"
    elif abs(difference) <= tolerance:
        status = "consistent"
    else:
        projected_difference = difference + pending_inflow_total
        if (
            abs(projected_difference) <= tolerance
            and abs(projected_difference) < abs(difference)
        ):
            status = "explainable_difference"
        else:
            status = "needs_investigation"

    return ReconciliationResult(
        fund_code=position.fund_code,
        status=status,
        confirmed_cash_flow=confirmed_cash_flow,
        inferred_position_cost=inferred_position_cost,
        difference=difference,
        tolerance=tolerance,
        evidence_level=EvidenceLevel.POSITION_INFERRED,
        warnings=warnings,
    )


def _position_nav(
    position: StoredPosition, warnings: List[str]
) -> Optional[Decimal]:
    if position.formal_nav is not None:
        if position.formal_nav <= 0:
            _warn_once(warnings, "formal NAV must be positive")
            return None
        return position.formal_nav
    if position.estimated_nav is not None:
        if position.estimated_nav <= 0:
            _warn_once(warnings, "estimated NAV must be positive")
            return None
        _warn_once(warnings, "estimated NAV was used")
        return position.estimated_nav
    _warn_once(warnings, "position NAV is missing")
    return None


def _inferred_position_cost(
    position: StoredPosition,
    current_nav: Optional[Decimal],
    warnings: List[str],
) -> Optional[Decimal]:
    if position.shares <= 0:
        _warn_once(warnings, "position shares must be positive")
    if position.observed_profit is None:
        _warn_once(warnings, "observed profit is missing")
    if (
        position.shares <= 0
        or current_nav is None
        or position.observed_profit is None
    ):
        return None
    return position.shares * current_nav - position.observed_profit


def _confirmed_cash_flow(
    fund_code: str,
    transactions: Iterable[LedgerTransaction],
    warnings: List[str],
) -> Tuple[Optional[Decimal], bool]:
    total = Decimal("0")
    included_amount = False
    cash_flow_complete = True

    for item in transactions:
        if item.fund_code != fund_code:
            continue
        transaction_type = TransactionType(item.transaction_type)
        if transaction_type in _UNSUPPORTED_COST_BASIS_TYPES:
            _warn_once(
                warnings,
                f"{transaction_type.value} transaction requires independent cost-basis handling",
            )
            cash_flow_complete = False
            continue
        if transaction_type not in _ACQUISITION_TYPES:
            continue
        if item.amount is None:
            _warn_once(
                warnings,
                f"{transaction_type.value} transaction amount is missing",
            )
            cash_flow_complete = False
            continue
        if not _has_confirmed_amount(item.evidence_level, item.field_evidence):
            _warn_once(
                warnings,
                f"{transaction_type.value} transaction amount is not confirmed",
            )
            cash_flow_complete = False
            continue
        total += item.amount
        included_amount = True

    if not included_amount:
        _warn_once(warnings, "confirmed acquisition cash flow is missing")
        return None, False
    if not cash_flow_complete:
        _warn_once(warnings, "confirmed acquisition cash flow is incomplete")
    return total, cash_flow_complete


def _pending_inflow_total(
    fund_code: str,
    drafts: Iterable[LedgerDraft],
    warnings: List[str],
) -> Decimal:
    total = Decimal("0")
    for item in drafts:
        if item.status != "pending" or item.fund_code != fund_code:
            continue
        transaction_type = TransactionType(item.transaction_type)
        if transaction_type not in _ACQUISITION_TYPES:
            _warn_once(
                warnings,
                f"{transaction_type.value} pending draft cannot explain acquisition cost",
            )
            continue
        if item.amount is None:
            _warn_once(
                warnings,
                f"{transaction_type.value} pending draft amount is missing",
            )
            continue
        if not _has_confirmed_amount(item.evidence_level, item.field_evidence):
            _warn_once(
                warnings,
                f"{transaction_type.value} pending draft amount is not confirmed",
            )
            continue
        total += item.amount
    return total


def _has_confirmed_amount(evidence_level, field_evidence) -> bool:
    try:
        overall_evidence = EvidenceLevel(evidence_level)
    except (TypeError, ValueError):
        return False
    return (
        overall_evidence is not EvidenceLevel.POSITION_INFERRED
        and field_evidence.get("amount") in _CONFIRMED_EVIDENCE
    )


def _warn_once(warnings: List[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)
