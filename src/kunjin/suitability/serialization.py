from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, NoReturn, Set

from kunjin.suitability.models import (
    Debt,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    RiskReaction,
)

PROFILE_KEYS = {
    "currency",
    "monthly_net_income",
    "monthly_essential_expenses",
    "monthly_required_debt_service",
    "monthly_investment_ceiling",
    "minimum_operating_cash",
    "minimum_monthly_cash_buffer",
    "income_stability",
    "income_interruption_risk",
    "immediately_available_cash",
    "cash_like_assets",
    "emergency_reserve",
    "low_risk_fixed_income_assets",
    "manual_equity_fund_assets",
    "manual_bond_fund_assets",
    "manual_sector_fund_assets",
    "dependents",
    "other_volatile_assets",
    "maximum_tolerable_loss",
    "maximum_tolerable_drawdown",
    "reaction_10",
    "reaction_20",
    "reaction_30",
    "experienced_material_loss",
    "understands_multi_year_recovery",
    "can_postpone_goal_use",
    "debts",
    "obligations",
    "goals",
    "confirmed_at",
}
DEBT_KEYS = {
    "debt_type",
    "outstanding_principal",
    "effective_annual_rate",
    "monthly_payment",
    "maturity_date",
    "delinquent",
    "revolving_interest",
}
OBLIGATION_KEYS = {"name", "amount", "due_date", "amount_already_reserved"}
GOAL_KEYS = {
    "name",
    "target_amount",
    "target_date",
    "priority",
    "amount_already_reserved",
    "temporary_principal_loss_acceptable",
    "use_date_can_be_postponed",
}


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("decimal values must be finite")
    return str(value)


def _profile_payload(profile: FinancialProfile) -> Dict[str, Any]:
    profile.validate()
    return {
        "currency": profile.currency,
        "monthly_net_income": _decimal_text(profile.monthly_net_income),
        "monthly_essential_expenses": _decimal_text(profile.monthly_essential_expenses),
        "monthly_required_debt_service": _decimal_text(
            profile.monthly_required_debt_service
        ),
        "monthly_investment_ceiling": _decimal_text(profile.monthly_investment_ceiling),
        "minimum_operating_cash": _decimal_text(profile.minimum_operating_cash),
        "minimum_monthly_cash_buffer": _decimal_text(profile.minimum_monthly_cash_buffer),
        "income_stability": profile.income_stability.value,
        "income_interruption_risk": profile.income_interruption_risk,
        "immediately_available_cash": _decimal_text(profile.immediately_available_cash),
        "cash_like_assets": _decimal_text(profile.cash_like_assets),
        "emergency_reserve": _decimal_text(profile.emergency_reserve),
        "low_risk_fixed_income_assets": _decimal_text(
            profile.low_risk_fixed_income_assets
        ),
        "manual_equity_fund_assets": _decimal_text(profile.manual_equity_fund_assets),
        "manual_bond_fund_assets": _decimal_text(profile.manual_bond_fund_assets),
        "manual_sector_fund_assets": _decimal_text(profile.manual_sector_fund_assets),
        "dependents": profile.dependents,
        "other_volatile_assets": _decimal_text(profile.other_volatile_assets),
        "maximum_tolerable_loss": _decimal_text(profile.maximum_tolerable_loss),
        "maximum_tolerable_drawdown": _decimal_text(profile.maximum_tolerable_drawdown),
        "reaction_10": profile.reaction_10.value,
        "reaction_20": profile.reaction_20.value,
        "reaction_30": profile.reaction_30.value,
        "experienced_material_loss": profile.experienced_material_loss,
        "understands_multi_year_recovery": profile.understands_multi_year_recovery,
        "can_postpone_goal_use": profile.can_postpone_goal_use,
        "debts": [
            {
                "debt_type": item.debt_type,
                "outstanding_principal": _decimal_text(item.outstanding_principal),
                "effective_annual_rate": _decimal_text(item.effective_annual_rate),
                "monthly_payment": _decimal_text(item.monthly_payment),
                "maturity_date": (
                    None if item.maturity_date is None else item.maturity_date.isoformat()
                ),
                "delinquent": item.delinquent,
                "revolving_interest": item.revolving_interest,
            }
            for item in profile.debts
        ],
        "obligations": [
            {
                "name": item.name,
                "amount": _decimal_text(item.amount),
                "due_date": item.due_date.isoformat(),
                "amount_already_reserved": _decimal_text(item.amount_already_reserved),
            }
            for item in profile.obligations
        ],
        "goals": [
            {
                "name": item.name,
                "target_amount": _decimal_text(item.target_amount),
                "target_date": item.target_date.isoformat(),
                "priority": item.priority,
                "amount_already_reserved": _decimal_text(item.amount_already_reserved),
                "temporary_principal_loss_acceptable": (
                    item.temporary_principal_loss_acceptable
                ),
                "use_date_can_be_postponed": item.use_date_can_be_postponed,
            }
            for item in profile.goals
        ],
        "confirmed_at": profile.confirmed_at.isoformat(),
    }


def encode_profile(profile: FinancialProfile) -> bytes:
    payload = _profile_payload(profile)
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _reject_float(value: str) -> NoReturn:
    raise ValueError(f"JSON floating-point values are not allowed: {value}")


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON constant values are not allowed: {value}")


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _array(value: Any, name: str) -> Iterable[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    return value


def _exact_keys(value: Mapping[str, Any], expected: Set[str], name: str) -> None:
    actual = set(value)
    unexpected = sorted(actual - expected)
    if unexpected:
        raise ValueError(f"unexpected {name} keys: {', '.join(unexpected)}")
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"missing {name} keys: {', '.join(missing)}")


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be encoded as a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _date(value: Any, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date string") from exc


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO datetime string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO datetime string") from exc


def _debt(value: Any) -> Debt:
    payload = _object(value, "debt")
    _exact_keys(payload, DEBT_KEYS, "debt")
    maturity = payload["maturity_date"]
    return Debt(
        debt_type=payload["debt_type"],
        outstanding_principal=_decimal(payload["outstanding_principal"], "outstanding principal"),
        effective_annual_rate=_decimal(
            payload["effective_annual_rate"], "effective annual rate"
        ),
        monthly_payment=_decimal(payload["monthly_payment"], "monthly payment"),
        maturity_date=None if maturity is None else _date(maturity, "maturity date"),
        delinquent=payload["delinquent"],
        revolving_interest=payload["revolving_interest"],
    )


def _obligation(value: Any) -> PlannedObligation:
    payload = _object(value, "obligation")
    _exact_keys(payload, OBLIGATION_KEYS, "obligation")
    return PlannedObligation(
        name=payload["name"],
        amount=_decimal(payload["amount"], "obligation amount"),
        due_date=_date(payload["due_date"], "obligation due date"),
        amount_already_reserved=_decimal(
            payload["amount_already_reserved"], "reserved obligation amount"
        ),
    )


def _goal(value: Any) -> FinancialGoal:
    payload = _object(value, "goal")
    _exact_keys(payload, GOAL_KEYS, "goal")
    return FinancialGoal(
        name=payload["name"],
        target_amount=_decimal(payload["target_amount"], "goal target amount"),
        target_date=_date(payload["target_date"], "goal target date"),
        priority=payload["priority"],
        amount_already_reserved=_decimal(
            payload["amount_already_reserved"], "goal reserved amount"
        ),
        temporary_principal_loss_acceptable=payload[
            "temporary_principal_loss_acceptable"
        ],
        use_date_can_be_postponed=payload["use_date_can_be_postponed"],
    )


def decode_profile(encoded: bytes) -> FinancialProfile:
    try:
        decoded = json.loads(
            encoded.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("profile must be valid UTF-8 JSON") from exc
    payload = _object(decoded, "profile")
    _exact_keys(payload, PROFILE_KEYS, "profile")
    profile = FinancialProfile(
        currency=payload["currency"],
        monthly_net_income=_decimal(payload["monthly_net_income"], "monthly net income"),
        monthly_essential_expenses=_decimal(
            payload["monthly_essential_expenses"], "monthly essential expenses"
        ),
        monthly_required_debt_service=_decimal(
            payload["monthly_required_debt_service"], "monthly required debt service"
        ),
        monthly_investment_ceiling=_decimal(
            payload["monthly_investment_ceiling"], "monthly investment ceiling"
        ),
        minimum_operating_cash=_decimal(
            payload["minimum_operating_cash"], "minimum operating cash"
        ),
        minimum_monthly_cash_buffer=_decimal(
            payload["minimum_monthly_cash_buffer"], "minimum monthly cash buffer"
        ),
        income_stability=IncomeStability(payload["income_stability"]),
        income_interruption_risk=payload["income_interruption_risk"],
        immediately_available_cash=_decimal(
            payload["immediately_available_cash"], "immediately available cash"
        ),
        cash_like_assets=_decimal(payload["cash_like_assets"], "cash-like assets"),
        emergency_reserve=_decimal(payload["emergency_reserve"], "emergency reserve"),
        low_risk_fixed_income_assets=_decimal(
            payload["low_risk_fixed_income_assets"], "low-risk fixed-income assets"
        ),
        manual_equity_fund_assets=_decimal(
            payload["manual_equity_fund_assets"], "manual equity-fund assets"
        ),
        manual_bond_fund_assets=_decimal(
            payload["manual_bond_fund_assets"], "manual bond-fund assets"
        ),
        manual_sector_fund_assets=_decimal(
            payload["manual_sector_fund_assets"], "manual sector-fund assets"
        ),
        dependents=payload["dependents"],
        other_volatile_assets=_decimal(
            payload["other_volatile_assets"], "other volatile assets"
        ),
        maximum_tolerable_loss=_decimal(
            payload["maximum_tolerable_loss"], "maximum tolerable loss"
        ),
        maximum_tolerable_drawdown=_decimal(
            payload["maximum_tolerable_drawdown"], "maximum tolerable drawdown"
        ),
        reaction_10=RiskReaction(payload["reaction_10"]),
        reaction_20=RiskReaction(payload["reaction_20"]),
        reaction_30=RiskReaction(payload["reaction_30"]),
        experienced_material_loss=payload["experienced_material_loss"],
        understands_multi_year_recovery=payload["understands_multi_year_recovery"],
        can_postpone_goal_use=payload["can_postpone_goal_use"],
        debts=tuple(_debt(item) for item in _array(payload["debts"], "debts")),
        obligations=tuple(
            _obligation(item) for item in _array(payload["obligations"], "obligations")
        ),
        goals=tuple(_goal(item) for item in _array(payload["goals"], "goals")),
        confirmed_at=_datetime(payload["confirmed_at"], "confirmed_at"),
    )
    profile.validate()
    return profile
