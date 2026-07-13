from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Callable, Dict, List, Mapping, NoReturn, Optional, Set, Tuple, Type, TypeVar

from kunjin.allocation.models import (
    AggregateAllocationInputs,
    AllocationExactResult,
    AllocationSleeveKind,
    AssignedSleeveDetail,
    GoalFundingDetail,
    GoalFundingState,
    ObligationFundingDetail,
)
from kunjin.suitability.models import MAX_PRIVATE_NAME_CHARS, validate_private_name

_EXACT_KEYS = {
    "assessment_date",
    "total_financial_assets",
    "liquid_protection_assets",
    "verified_emergency_reserve",
    "minimum_operating_cash",
    "protected_short_term_assigned",
    "protected_liquid_claims",
    "investable_stock_assets",
    "monthly_discretionary_allocation_ceiling",
    "maximum_tolerable_loss",
    "maximum_tolerable_drawdown",
    "residual_horizon_date",
    "goal_funding_details",
    "obligation_funding_details",
    "assigned_sleeves",
    "aggregate_inputs",
}
_GOAL_KEYS = {
    "name",
    "target_date",
    "target_amount",
    "amount_already_reserved",
    "confirmed_monthly_saving",
    "remaining_contribution_periods",
    "zero_return_funding",
    "funding_state",
    "horizon_equity_ceiling",
}
_OBLIGATION_KEYS = {
    "name",
    "due_date",
    "amount",
    "amount_already_reserved",
    "funding_gap",
    "confirmed_monthly_saving",
    "remaining_contribution_periods",
    "zero_return_funding",
    "horizon_equity_ceiling",
}
_SLEEVE_KEYS = {
    "sleeve_kind",
    "name",
    "assigned_amount",
    "horizon_date",
    "horizon_equity_ceiling",
    "weighted_equity_contribution",
}
_AGGREGATE_KEYS = {
    "weighted_horizon_numerator",
    "weighted_horizon_equity_ceiling",
    "loss_amount_equity_ceiling",
    "drawdown_equity_ceiling",
    "willingness_equity_ceiling",
    "stability_equity_ceiling",
    "fixed_income_stress_loss",
    "equity_stress_loss",
}
_CENT = Decimal("0.01")
_RATIO_QUANTUM = Decimal("0.01")
_FIXED_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")
_EXACT_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")
_MAX_DECIMAL_TEXT_LENGTH = 10_020
MAX_EXACT_PAYLOAD_BYTES = 1_048_576
MAX_COLLECTION_ITEMS = 10_000
MAX_TEXT_CHARS = MAX_PRIVATE_NAME_CHARS
MAX_INTEGER_DIGITS = 12
_EnumT = TypeVar("_EnumT")


def encode_exact_result(value: AllocationExactResult) -> bytes:
    if type(value) is not AllocationExactResult:
        raise ValueError("exact result must be an exact AllocationExactResult")
    _preflight_exact_result(value)
    value.validate()
    payload = {
        "assessment_date": _date_text(value.assessment_date, "assessment date"),
        "total_financial_assets": _money_text(value.total_financial_assets),
        "liquid_protection_assets": _money_text(value.liquid_protection_assets),
        "verified_emergency_reserve": _money_text(value.verified_emergency_reserve),
        "minimum_operating_cash": _money_text(value.minimum_operating_cash),
        "protected_short_term_assigned": _money_text(value.protected_short_term_assigned),
        "protected_liquid_claims": _money_text(value.protected_liquid_claims),
        "investable_stock_assets": _money_text(value.investable_stock_assets),
        "monthly_discretionary_allocation_ceiling": _money_text(
            value.monthly_discretionary_allocation_ceiling
        ),
        "maximum_tolerable_loss": _money_text(value.maximum_tolerable_loss),
        "maximum_tolerable_drawdown": _ratio_text(value.maximum_tolerable_drawdown),
        "residual_horizon_date": (
            None
            if value.residual_horizon_date is None
            else _date_text(value.residual_horizon_date, "residual horizon date")
        ),
        "goal_funding_details": [_encode_goal(item) for item in value.goal_funding_details],
        "obligation_funding_details": [
            _encode_obligation(item) for item in value.obligation_funding_details
        ],
        "assigned_sleeves": [_encode_sleeve(item) for item in value.assigned_sleeves],
        "aggregate_inputs": _encode_aggregate(value.aggregate_inputs),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_EXACT_PAYLOAD_BYTES:
        raise ValueError("allocation exact result payload is too large")
    return encoded


def decode_exact_result(payload: bytes) -> AllocationExactResult:
    if type(payload) is not bytes:
        raise ValueError("encoded allocation exact result must be bytes")
    if len(payload) > MAX_EXACT_PAYLOAD_BYTES:
        raise ValueError("allocation exact result payload is too large")
    try:
        decoded = json.loads(
            payload.decode("utf-8"),
            parse_int=_parse_int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, MemoryError):
        raise ValueError("allocation exact result JSON is invalid") from None
    if type(decoded) is not dict:
        raise ValueError("allocation exact result must be a JSON object")
    _exact_keys(decoded, _EXACT_KEYS)
    result = AllocationExactResult(
        assessment_date=_date(decoded["assessment_date"], "assessment date"),
        total_financial_assets=_money(decoded["total_financial_assets"], "total financial assets"),
        liquid_protection_assets=_money(
            decoded["liquid_protection_assets"], "liquid protection assets"
        ),
        verified_emergency_reserve=_money(
            decoded["verified_emergency_reserve"], "verified emergency reserve"
        ),
        minimum_operating_cash=_money(decoded["minimum_operating_cash"], "minimum operating cash"),
        protected_short_term_assigned=_money(
            decoded["protected_short_term_assigned"], "protected short-term assigned"
        ),
        protected_liquid_claims=_money(
            decoded["protected_liquid_claims"], "protected liquid claims"
        ),
        investable_stock_assets=_money(
            decoded["investable_stock_assets"], "investable stock assets"
        ),
        monthly_discretionary_allocation_ceiling=_money(
            decoded["monthly_discretionary_allocation_ceiling"],
            "monthly discretionary allocation ceiling",
        ),
        maximum_tolerable_loss=_money(decoded["maximum_tolerable_loss"], "maximum tolerable loss"),
        maximum_tolerable_drawdown=_ratio(
            decoded["maximum_tolerable_drawdown"], "maximum tolerable drawdown"
        ),
        residual_horizon_date=_optional_date(
            decoded["residual_horizon_date"], "residual horizon date"
        ),
        goal_funding_details=_decode_list(
            decoded["goal_funding_details"], _decode_goal, "goal_funding_details"
        ),
        obligation_funding_details=_decode_list(
            decoded["obligation_funding_details"],
            _decode_obligation,
            "obligation_funding_details",
        ),
        assigned_sleeves=_decode_list(
            decoded["assigned_sleeves"], _decode_sleeve, "assigned_sleeves"
        ),
        aggregate_inputs=_decode_aggregate(decoded["aggregate_inputs"]),
    )
    result.validate()
    if payload != encode_exact_result(result):
        raise ValueError("allocation exact result JSON is not canonical")
    return result


def _encode_goal(value: GoalFundingDetail) -> Dict[str, object]:
    return {
        "name": _text(value.name, "goal name"),
        "target_date": _date_text(value.target_date, "goal target date"),
        "target_amount": _money_text(value.target_amount),
        "amount_already_reserved": _money_text(value.amount_already_reserved),
        "confirmed_monthly_saving": _money_text(value.confirmed_monthly_saving),
        "remaining_contribution_periods": value.remaining_contribution_periods,
        "zero_return_funding": _money_text(value.zero_return_funding),
        "funding_state": value.funding_state.value,
        "horizon_equity_ceiling": _ratio_text(value.horizon_equity_ceiling),
    }


def _decode_goal(value: object) -> GoalFundingDetail:
    item = _object(value, _GOAL_KEYS, "goal detail")
    return GoalFundingDetail(
        name=_text(item["name"], "goal name"),
        target_date=_date(item["target_date"], "goal target date"),
        target_amount=_money(item["target_amount"], "goal target amount"),
        amount_already_reserved=_money(item["amount_already_reserved"], "goal reserved amount"),
        confirmed_monthly_saving=_money(
            item["confirmed_monthly_saving"], "confirmed monthly goal saving"
        ),
        remaining_contribution_periods=_integer(
            item["remaining_contribution_periods"], "remaining contribution periods"
        ),
        zero_return_funding=_money(item["zero_return_funding"], "goal zero-return funding"),
        funding_state=_enum(item["funding_state"], GoalFundingState, "funding_state"),
        horizon_equity_ceiling=_ratio(
            item["horizon_equity_ceiling"], "goal horizon equity ceiling"
        ),
    )


def _encode_obligation(value: ObligationFundingDetail) -> Dict[str, object]:
    return {
        "name": _text(value.name, "obligation name"),
        "due_date": _date_text(value.due_date, "obligation due date"),
        "amount": _money_text(value.amount),
        "amount_already_reserved": _money_text(value.amount_already_reserved),
        "funding_gap": _money_text(value.funding_gap),
        "confirmed_monthly_saving": _money_text(value.confirmed_monthly_saving),
        "remaining_contribution_periods": value.remaining_contribution_periods,
        "zero_return_funding": _money_text(value.zero_return_funding),
        "horizon_equity_ceiling": _ratio_text(value.horizon_equity_ceiling),
    }


def _decode_obligation(value: object) -> ObligationFundingDetail:
    item = _object(value, _OBLIGATION_KEYS, "obligation detail")
    return ObligationFundingDetail(
        name=_text(item["name"], "obligation name"),
        due_date=_date(item["due_date"], "obligation due date"),
        amount=_money(item["amount"], "obligation amount"),
        amount_already_reserved=_money(
            item["amount_already_reserved"], "obligation reserved amount"
        ),
        funding_gap=_money(item["funding_gap"], "obligation funding gap"),
        confirmed_monthly_saving=_money(
            item["confirmed_monthly_saving"], "confirmed monthly obligation saving"
        ),
        remaining_contribution_periods=_integer(
            item["remaining_contribution_periods"], "obligation remaining contribution periods"
        ),
        zero_return_funding=_money(item["zero_return_funding"], "obligation zero-return funding"),
        horizon_equity_ceiling=_ratio(
            item["horizon_equity_ceiling"], "obligation horizon equity ceiling"
        ),
    )


def _encode_sleeve(value: AssignedSleeveDetail) -> Dict[str, object]:
    return {
        "sleeve_kind": value.sleeve_kind.value,
        "name": _text(value.name, "assigned sleeve name"),
        "assigned_amount": _money_text(value.assigned_amount),
        "horizon_date": _date_text(value.horizon_date, "sleeve horizon date"),
        "horizon_equity_ceiling": _ratio_text(value.horizon_equity_ceiling),
        "weighted_equity_contribution": _exact_decimal_text(value.weighted_equity_contribution),
    }


def _decode_sleeve(value: object) -> AssignedSleeveDetail:
    item = _object(value, _SLEEVE_KEYS, "assigned sleeve")
    return AssignedSleeveDetail(
        sleeve_kind=_enum(item["sleeve_kind"], AllocationSleeveKind, "sleeve_kind"),
        name=_text(item["name"], "assigned sleeve name"),
        assigned_amount=_money(item["assigned_amount"], "assigned sleeve amount"),
        horizon_date=_date(item["horizon_date"], "assigned sleeve horizon date"),
        horizon_equity_ceiling=_ratio(
            item["horizon_equity_ceiling"], "assigned sleeve horizon equity ceiling"
        ),
        weighted_equity_contribution=_exact_decimal(
            item["weighted_equity_contribution"],
            "weighted equity contribution",
        ),
    )


def _encode_aggregate(value: AggregateAllocationInputs) -> Dict[str, object]:
    return {
        "weighted_horizon_numerator": _exact_decimal_text(value.weighted_horizon_numerator),
        "weighted_horizon_equity_ceiling": _ratio_text(value.weighted_horizon_equity_ceiling),
        "loss_amount_equity_ceiling": _ratio_text(value.loss_amount_equity_ceiling),
        "drawdown_equity_ceiling": _ratio_text(value.drawdown_equity_ceiling),
        "willingness_equity_ceiling": _ratio_text(value.willingness_equity_ceiling),
        "stability_equity_ceiling": _ratio_text(value.stability_equity_ceiling),
        "fixed_income_stress_loss": _ratio_text(value.fixed_income_stress_loss),
        "equity_stress_loss": _ratio_text(value.equity_stress_loss),
    }


def _decode_aggregate(value: object) -> AggregateAllocationInputs:
    item = _object(value, _AGGREGATE_KEYS, "aggregate inputs")
    return AggregateAllocationInputs(
        weighted_horizon_numerator=_exact_decimal(
            item["weighted_horizon_numerator"], "weighted horizon numerator"
        ),
        weighted_horizon_equity_ceiling=_ratio(
            item["weighted_horizon_equity_ceiling"], "weighted horizon equity ceiling"
        ),
        loss_amount_equity_ceiling=_ratio(
            item["loss_amount_equity_ceiling"], "loss amount equity ceiling"
        ),
        drawdown_equity_ceiling=_ratio(item["drawdown_equity_ceiling"], "drawdown equity ceiling"),
        willingness_equity_ceiling=_ratio(
            item["willingness_equity_ceiling"], "willingness equity ceiling"
        ),
        stability_equity_ceiling=_ratio(
            item["stability_equity_ceiling"], "stability equity ceiling"
        ),
        fixed_income_stress_loss=_ratio(
            item["fixed_income_stress_loss"], "fixed-income stress loss"
        ),
        equity_stress_loss=_ratio(item["equity_stress_loss"], "equity stress loss"),
    )


def _reject_float(_value: str) -> NoReturn:
    raise ValueError("JSON floating-point values are not allowed")


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("JSON constant values are not allowed")


def _parse_int(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_INTEGER_DIGITS:
        raise ValueError("JSON integer is too large")
    return int(value)


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate allocation key")
        result[key] = value
    return result


def _object(value: object, keys: Set[str], name: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{name} must be a JSON object")
    _exact_keys(value, keys)
    return value


def _exact_keys(value: Mapping[str, Any], expected: Set[str]) -> None:
    actual = set(value)
    if actual - expected:
        raise ValueError("unexpected allocation keys")
    if expected - actual:
        raise ValueError("missing allocation keys")


def _decode_list(
    value: object, decoder: Callable[[object], _EnumT], name: str
) -> Tuple[_EnumT, ...]:
    if type(value) is not list:
        raise ValueError(f"{name} must be a list")
    if len(value) > MAX_COLLECTION_ITEMS:
        raise ValueError(f"{name} contains too many items")
    return tuple(decoder(item) for item in value)


def _text(value: object, name: str) -> str:
    return validate_private_name(value, name)


def _preflight_exact_result(value: AllocationExactResult) -> None:
    collections = (
        (value.goal_funding_details, GoalFundingDetail, "goal_funding_details"),
        (
            value.obligation_funding_details,
            ObligationFundingDetail,
            "obligation_funding_details",
        ),
        (value.assigned_sleeves, AssignedSleeveDetail, "assigned_sleeves"),
    )
    for collection, item_type, name in collections:
        if type(collection) is not tuple:
            continue
        if len(collection) > MAX_COLLECTION_ITEMS:
            raise ValueError(f"{name} contains too many items")
        for item in collection:
            if type(item) is item_type:
                _text(item.name, f"{name} item name")


def _integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _enum(value: object, enum_type: Type[_EnumT], name: str) -> _EnumT:
    if type(value) is not str:
        raise ValueError(f"{name} must be a declared enum value")
    try:
        return enum_type(value)
    except ValueError:
        raise ValueError(f"{name} must be a declared enum value") from None


def _date(value: object, name: str) -> date:
    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a canonical ISO date") from None
    if value != parsed.isoformat():
        raise ValueError(f"{name} must be a canonical ISO date")
    return parsed


def _optional_date(value: object, name: str) -> Optional[date]:
    if value is None:
        return None
    return _date(value, name)


def _date_text(value: date, name: str) -> str:
    if type(value) is not date:
        raise ValueError(f"{name} must be an exact date")
    return value.isoformat()


def _money(value: object, name: str) -> Decimal:
    if type(value) is not str or not _canonical_decimal_text_shape(
        value,
        _FIXED_DECIMAL_PATTERN,
    ):
        raise ValueError(f"{name} must be a canonical decimal string")
    return _decimal_string(value, name)


def _ratio(value: object, name: str) -> Decimal:
    if type(value) is not str or not _canonical_decimal_text_shape(
        value,
        _FIXED_DECIMAL_PATTERN,
    ):
        raise ValueError(f"{name} must be a canonical decimal string")
    return _decimal_string(value, name)


def _exact_decimal(value: object, name: str) -> Decimal:
    if type(value) is not str or not _canonical_decimal_text_shape(
        value,
        _EXACT_DECIMAL_PATTERN,
    ):
        raise ValueError(f"{name} must be a canonical decimal string")
    return _decimal_string(value, name)


def _canonical_decimal_text_shape(value: str, pattern: re.Pattern[str]) -> bool:
    return len(value) <= _MAX_DECIMAL_TEXT_LENGTH and pattern.fullmatch(value) is not None


def _decimal_string(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise ValueError(f"{name} must be encoded as a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a valid decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _money_text(value: Decimal) -> str:
    return _fixed_decimal_text(value, _CENT, "CNY amount")


def _ratio_text(value: Decimal) -> str:
    return _fixed_decimal_text(value, _RATIO_QUANTUM, "ratio")


def _fixed_decimal_text(value: Decimal, quantum: Decimal, name: str) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    try:
        with localcontext() as context:
            context.prec = max(
                28, len(value.as_tuple().digits) + abs(value.as_tuple().exponent) + 4
            )
            quantized = value.quantize(quantum)
    except InvalidOperation:
        raise ValueError(f"{name} has invalid precision") from None
    if quantized != value:
        raise ValueError(f"{name} has invalid precision")
    if quantized == 0:
        quantized = Decimal((0, (0,), quantum.as_tuple().exponent))
    return format(quantized, f".{abs(quantum.as_tuple().exponent)}f")


def _exact_decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ValueError("derived allocation decimal must be finite and non-negative")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
