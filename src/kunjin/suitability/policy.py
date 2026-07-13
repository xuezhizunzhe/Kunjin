from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import FrozenSet, Tuple

from kunjin.suitability.models import DebtType, RiskReaction

_SUPPORTED_DEBT_TYPES = frozenset(
    {
        DebtType.MORTGAGE,
        DebtType.AUTO_LOAN,
        DebtType.CREDIT_CARD,
        DebtType.CONSUMER_LOAN,
        DebtType.PERSONAL_LOAN,
        DebtType.STUDENT_LOAN,
    }
)
_CONSUMER_DEBT_TYPES = frozenset(
    {
        DebtType.CREDIT_CARD,
        DebtType.CONSUMER_LOAN,
        DebtType.PERSONAL_LOAN,
    }
)
_RISK_REACTION_SEVERITY = (
    (RiskReaction.HOLD, 0),
    (RiskReaction.REDUCE, 1),
    (RiskReaction.REDEEM, 2),
)
_EFFECTIVE_AT = datetime(2026, 7, 12, tzinfo=timezone.utc)


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _require_exact(value: object, expected: object, name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"policy V1 {name} must be {expected!r}")


@dataclass(frozen=True)
class SuitabilityPolicyV1:
    version: str = "1"
    supported_debt_types: FrozenSet[DebtType] = _SUPPORTED_DEBT_TYPES
    consumer_debt_types: FrozenSet[DebtType] = _CONSUMER_DEBT_TYPES
    high_interest_annual_rate: Decimal = Decimal("0.08")
    reserve_months_stable: int = 6
    reserve_months_variable: int = 9
    reserve_months_high_risk: int = 12
    material_obligation_expense_months: Decimal = Decimal("1")
    short_horizon_years: int = 1
    medium_horizon_years: int = 3
    assessment_freshness_hours: int = 24
    risk_reaction_severity: Tuple[Tuple[RiskReaction, int], ...] = _RISK_REACTION_SEVERITY
    money_quantum: Decimal = Decimal("0.01")
    required_amount_rounding: str = "ROUND_CEILING"
    available_amount_rounding: str = "ROUND_FLOOR"
    effective_at: datetime = _EFFECTIVE_AT

    def validate(self) -> None:
        _require_exact(self.version, "1", "version")
        _require_exact(
            self.supported_debt_types,
            _SUPPORTED_DEBT_TYPES,
            "supported debt types",
        )
        _require_exact(
            self.consumer_debt_types,
            _CONSUMER_DEBT_TYPES,
            "consumer debt types",
        )
        self._validate_decimal(
            self.high_interest_annual_rate,
            Decimal("0.08"),
            "high-interest rate",
        )
        _require_exact(self.reserve_months_stable, 6, "stable reserve months")
        _require_exact(self.reserve_months_variable, 9, "variable reserve months")
        _require_exact(self.reserve_months_high_risk, 12, "high-risk reserve months")
        self._validate_decimal(
            self.material_obligation_expense_months,
            Decimal("1"),
            "material obligation threshold",
        )
        _require_exact(self.short_horizon_years, 1, "short horizon years")
        _require_exact(self.medium_horizon_years, 3, "medium horizon years")
        _require_exact(self.assessment_freshness_hours, 24, "assessment freshness hours")
        self._validate_decimal(self.money_quantum, Decimal("0.01"), "money quantum")
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
        self._validate_risk_severity()
        if type(self.effective_at) is not datetime:
            raise ValueError("policy V1 effective_at must be a datetime")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("policy V1 effective_at must be timezone-aware")
        if self.effective_at.astimezone(timezone.utc) != _EFFECTIVE_AT:
            raise ValueError("policy V1 effective_at must be the fixed effective instant")

    @staticmethod
    def _validate_decimal(value: object, expected: Decimal, name: str) -> None:
        if not isinstance(value, Decimal) or not value.is_finite() or value != expected:
            raise ValueError(f"policy V1 {name} must be {_canonical_decimal(expected)}")

    def _validate_risk_severity(self) -> None:
        expected = {
            RiskReaction.HOLD: 0,
            RiskReaction.REDUCE: 1,
            RiskReaction.REDEEM: 2,
        }
        if not isinstance(self.risk_reaction_severity, tuple):
            raise ValueError("policy V1 risk reaction severity must be a tuple")
        try:
            actual = dict(self.risk_reaction_severity)
        except (TypeError, ValueError) as exc:
            raise ValueError("policy V1 risk reaction severity is invalid") from exc
        valid_entries = all(
            isinstance(reaction, RiskReaction) and type(severity) is int
            for reaction, severity in self.risk_reaction_severity
        )
        if (
            not valid_entries
            or actual != expected
            or len(self.risk_reaction_severity) != len(expected)
        ):
            raise ValueError("policy V1 risk reaction severity must be the fixed mapping")

    def canonical_json(self) -> bytes:
        self.validate()
        payload = {
            "assessment_freshness_hours": self.assessment_freshness_hours,
            "available_amount_rounding": self.available_amount_rounding,
            "consumer_debt_types": sorted(value.value for value in self.consumer_debt_types),
            "effective_at": self.effective_at.astimezone(timezone.utc).isoformat(),
            "high_interest_annual_rate": _canonical_decimal(
                self.high_interest_annual_rate
            ),
            "material_obligation_expense_months": _canonical_decimal(
                self.material_obligation_expense_months
            ),
            "medium_horizon_years": self.medium_horizon_years,
            "money_quantum": _canonical_decimal(self.money_quantum),
            "required_amount_rounding": self.required_amount_rounding,
            "reserve_months_high_risk": self.reserve_months_high_risk,
            "reserve_months_stable": self.reserve_months_stable,
            "reserve_months_variable": self.reserve_months_variable,
            "risk_reaction_severity": {
                reaction.value: severity for reaction, severity in self.risk_reaction_severity
            },
            "short_horizon_years": self.short_horizon_years,
            "supported_debt_types": sorted(value.value for value in self.supported_debt_types),
            "version": self.version,
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()
