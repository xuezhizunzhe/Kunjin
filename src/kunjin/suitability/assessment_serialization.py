from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, List, Mapping, NoReturn, Set, Tuple

from kunjin.suitability.models import AssessmentAmounts

ASSESSMENT_AMOUNT_KEYS = {
    "verified_emergency_reserve",
    "required_emergency_reserve",
    "emergency_reserve_shortfall",
    "required_monthly_obligation_saving",
    "required_monthly_goal_saving",
    "monthly_safety_residual",
    "safe_monthly_ceiling",
}
CENT = Decimal("0.01")


def encode_assessment_amounts(amounts: AssessmentAmounts) -> bytes:
    if not isinstance(amounts, AssessmentAmounts):
        raise ValueError("amounts must be AssessmentAmounts")
    amounts.validate()
    payload = {
        "verified_emergency_reserve": _canonical_decimal_text(
            amounts.verified_emergency_reserve
        ),
        "required_emergency_reserve": _canonical_decimal_text(
            amounts.required_emergency_reserve
        ),
        "emergency_reserve_shortfall": _canonical_decimal_text(
            amounts.emergency_reserve_shortfall
        ),
        "required_monthly_obligation_saving": _canonical_decimal_text(
            amounts.required_monthly_obligation_saving
        ),
        "required_monthly_goal_saving": _canonical_decimal_text(
            amounts.required_monthly_goal_saving
        ),
        "monthly_safety_residual": _canonical_decimal_text(
            amounts.monthly_safety_residual
        ),
        "safe_monthly_ceiling": _canonical_decimal_text(
            amounts.safe_monthly_ceiling
        ),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_assessment_amounts(encoded: bytes) -> AssessmentAmounts:
    if not isinstance(encoded, bytes):
        raise ValueError("encoded assessment amounts must be bytes")
    try:
        payload = json.loads(
            encoded.decode("utf-8"),
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("assessment amounts JSON is invalid") from None
    if not isinstance(payload, dict):
        raise ValueError("assessment amounts must be a JSON object")
    _exact_keys(payload, ASSESSMENT_AMOUNT_KEYS)
    amounts = AssessmentAmounts(
        verified_emergency_reserve=_decimal(
            payload["verified_emergency_reserve"],
            "verified emergency reserve",
        ),
        required_emergency_reserve=_decimal(
            payload["required_emergency_reserve"],
            "required emergency reserve",
        ),
        emergency_reserve_shortfall=_decimal(
            payload["emergency_reserve_shortfall"],
            "emergency reserve shortfall",
        ),
        required_monthly_obligation_saving=_decimal(
            payload["required_monthly_obligation_saving"],
            "required monthly obligation saving",
        ),
        required_monthly_goal_saving=_decimal(
            payload["required_monthly_goal_saving"],
            "required monthly goal saving",
        ),
        monthly_safety_residual=_decimal(
            payload["monthly_safety_residual"],
            "monthly safety residual",
        ),
        safe_monthly_ceiling=_decimal(
            payload["safe_monthly_ceiling"],
            "safe monthly ceiling",
        ),
    )
    amounts.validate()
    if encoded != encode_assessment_amounts(amounts):
        raise ValueError("assessment amounts JSON is not canonical")
    return amounts


def _reject_float(_value: str) -> NoReturn:
    raise ValueError("JSON floating-point values are not allowed")


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("JSON constant values are not allowed")


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Mapping[str, Any]:
    payload = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate assessment amount key")
        payload[key] = value
    return payload


def _exact_keys(payload: Mapping[str, Any], expected: Set[str]) -> None:
    actual = set(payload)
    unexpected = sorted(actual - expected)
    if unexpected:
        raise ValueError("unexpected assessment amount keys")
    missing = sorted(expected - actual)
    if missing:
        raise ValueError("missing assessment amount keys")


def _decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be encoded as a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{name} must be a valid decimal") from None
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if value != _canonical_decimal_text(parsed):
        raise ValueError(f"{name} must be a canonical decimal string")
    return parsed


def _canonical_decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("assessment amounts must be finite")
    try:
        parts = value.as_tuple()
        with localcontext() as context:
            context.prec = max(
                28,
                len(parts.digits) + max(parts.exponent, 0) + 2,
            )
            quantized = value.quantize(CENT)
    except InvalidOperation:
        raise ValueError("assessment amounts must be whole cents") from None
    if quantized != value:
        raise ValueError("assessment amounts must be whole cents")
    if quantized == 0:
        quantized = Decimal("0.00")
    return format(quantized, ".2f")
