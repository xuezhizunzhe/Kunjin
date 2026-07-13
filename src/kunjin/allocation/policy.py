from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

from kunjin.allocation.models import AssetLayer

_STRESS_LOSS_BY_LAYER = (
    (AssetLayer.PROTECTED_CASH, Decimal("0")),
    (AssetLayer.HIGH_QUALITY_FIXED_INCOME, Decimal("0.10")),
    (AssetLayer.DIVERSIFIED_EQUITY, Decimal("0.50")),
)
_HORIZON_EQUITY_CEILINGS = (
    (1, Decimal("0")),
    (3, Decimal("0.10")),
    (5, Decimal("0.30")),
    (8, Decimal("0.50")),
    (None, Decimal("0.70")),
)
_WILLINGNESS_EQUITY_CEILINGS = (
    ("reduce_or_redeem_at_10", Decimal("0.10")),
    ("hold_10_not_20", Decimal("0.30")),
    ("hold_20_not_30", Decimal("0.50")),
    ("hold_30_experienced_and_recovery_aware", Decimal("0.70")),
    ("hold_30_missing_experience_or_recovery_awareness", Decimal("0.50")),
)
_STABILITY_EQUITY_CEILINGS = (
    ("stable_no_dependents_no_interruption", Decimal("0.70")),
    ("stable_with_dependents_or_variable_without_dependents", Decimal("0.50")),
    ("variable_with_dependents_or_interruption_signal", Decimal("0.30")),
    ("unstable", Decimal("0.20")),
)
_EFFECTIVE_AT = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _validate_decimal(value: object, expected: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value != expected:
        raise ValueError(f"allocation policy V1 {name} must be {_canonical_decimal(expected)}")


def _require_exact(value: object, expected: object, name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"allocation policy V1 {name} must be {expected!r}")


def _require_declared_dataclass_state(value: object) -> None:
    state = vars(value)
    expected = {field.name for field in dataclass_fields(type(value))}
    if type(state) is not dict or set(state) != expected:
        raise ValueError("allocation policy V1 has unexpected dataclass state")


def _validate_decimal_mapping(
    value: object,
    expected: Tuple[Tuple[object, Decimal], ...],
    name: str,
) -> None:
    if type(value) is not tuple or len(value) != len(expected):
        raise ValueError(f"allocation policy V1 {name} must be the fixed tuple mapping")
    for actual_entry, expected_entry in zip(value, expected):
        if type(actual_entry) is not tuple or len(actual_entry) != 2:
            raise ValueError(f"allocation policy V1 {name} must be the fixed tuple mapping")
        actual_key, actual_decimal = actual_entry
        expected_key, expected_decimal = expected_entry
        if type(actual_key) is not type(expected_key) or actual_key != expected_key:
            raise ValueError(f"allocation policy V1 {name} must be the fixed tuple mapping")
        _validate_decimal(actual_decimal, expected_decimal, name)


@dataclass(frozen=True)
class AllocationPolicyV1:
    version: str = "1"
    stress_loss_by_layer: Tuple[Tuple[AssetLayer, Decimal], ...] = _STRESS_LOSS_BY_LAYER
    horizon_equity_ceilings: Tuple[Tuple[Optional[int], Decimal], ...] = _HORIZON_EQUITY_CEILINGS
    willingness_equity_ceilings: Tuple[Tuple[str, Decimal], ...] = _WILLINGNESS_EQUITY_CEILINGS
    stability_equity_ceilings: Tuple[Tuple[str, Decimal], ...] = _STABILITY_EQUITY_CEILINGS
    protected_short_term_years: int = 3
    assessment_freshness_hours: int = 24
    money_quantum: Decimal = Decimal("0.01")
    percentage_quantum: Decimal = Decimal("0.01")
    required_amount_rounding: str = "ROUND_CEILING"
    available_amount_rounding: str = "ROUND_FLOOR"
    percentage_rounding: str = "ROUND_FLOOR"
    monthly_saving_apportionment: str = "largest_remainder"
    effective_at: datetime = _EFFECTIVE_AT

    def validate(self) -> None:
        if type(self) is not AllocationPolicyV1:
            raise ValueError("allocation policy V1 subclasses are not accepted")
        _require_declared_dataclass_state(self)
        _require_exact(self.version, "1", "version")
        _validate_decimal_mapping(
            self.stress_loss_by_layer,
            _STRESS_LOSS_BY_LAYER,
            "stress loss by layer",
        )
        _validate_decimal_mapping(
            self.horizon_equity_ceilings,
            _HORIZON_EQUITY_CEILINGS,
            "horizon equity ceilings",
        )
        _validate_decimal_mapping(
            self.willingness_equity_ceilings,
            _WILLINGNESS_EQUITY_CEILINGS,
            "willingness equity ceilings",
        )
        _validate_decimal_mapping(
            self.stability_equity_ceilings,
            _STABILITY_EQUITY_CEILINGS,
            "stability equity ceilings",
        )
        _require_exact(self.protected_short_term_years, 3, "protected short-term years")
        _require_exact(self.assessment_freshness_hours, 24, "assessment freshness hours")
        _validate_decimal(self.money_quantum, Decimal("0.01"), "money quantum")
        _validate_decimal(
            self.percentage_quantum,
            Decimal("0.01"),
            "percentage quantum",
        )
        _require_exact(
            self.required_amount_rounding,
            "ROUND_CEILING",
            "required amount rounding",
        )
        _require_exact(
            self.available_amount_rounding,
            "ROUND_FLOOR",
            "available amount rounding",
        )
        _require_exact(
            self.percentage_rounding,
            "ROUND_FLOOR",
            "percentage rounding",
        )
        _require_exact(
            self.monthly_saving_apportionment,
            "largest_remainder",
            "monthly saving apportionment",
        )
        effective_at = self.effective_at
        if type(effective_at) is not datetime:
            raise ValueError("allocation policy V1 effective_at must be a datetime")
        if type(effective_at.tzinfo) is not timezone:
            raise ValueError("allocation policy V1 effective_at must use datetime.timezone")
        if effective_at.astimezone(timezone.utc) != _EFFECTIVE_AT:
            raise ValueError(
                "allocation policy V1 effective_at must be the fixed effective instant"
            )

    def canonical_json(self) -> bytes:
        self.validate()
        payload = {
            "assessment_freshness_hours": self.assessment_freshness_hours,
            "available_amount_rounding": self.available_amount_rounding,
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "horizon_equity_ceilings": [
                {
                    "maximum_years": maximum_years,
                    "equity_ceiling": _canonical_decimal(ceiling),
                }
                for maximum_years, ceiling in self.horizon_equity_ceilings
            ],
            "money_quantum": _canonical_decimal(self.money_quantum),
            "monthly_saving_apportionment": self.monthly_saving_apportionment,
            "percentage_quantum": _canonical_decimal(self.percentage_quantum),
            "percentage_rounding": self.percentage_rounding,
            "protected_short_term_years": self.protected_short_term_years,
            "required_amount_rounding": self.required_amount_rounding,
            "stability_equity_ceilings": {
                name: _canonical_decimal(ceiling)
                for name, ceiling in self.stability_equity_ceilings
            },
            "stress_loss_by_layer": {
                layer.value: _canonical_decimal(loss) for layer, loss in self.stress_loss_by_layer
            },
            "version": self.version,
            "willingness_equity_ceilings": {
                name: _canonical_decimal(ceiling)
                for name, ceiling in self.willingness_equity_ceilings
            },
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


ALLOCATION_POLICY_V1_CHECKSUM = "4ab1bfde13afbbc87730e6ce9f842757d64d6565fe27dee18c0d03e125f3d708"
