from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Clamped,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    FloatOperation,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    Subnormal,
    Underflow,
    localcontext,
)
from enum import Enum
from typing import Optional, Tuple

from kunjin.suitability.models import validate_private_name


class AllocationStatus(str, Enum):
    BLOCKED = "blocked"
    RANGE_AVAILABLE = "range_available"


class AssetLayer(str, Enum):
    PROTECTED_CASH = "protected_cash"
    HIGH_QUALITY_FIXED_INCOME = "high_quality_fixed_income"
    DIVERSIFIED_EQUITY = "diversified_equity"


class AllocationBlockCode(str, Enum):
    SUITABILITY_BLOCKED = "suitability_blocked"
    ALLOCATION_HORIZON_MISSING = "allocation_horizon_missing"
    PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL = "protected_capital_overlap_or_shortfall"
    ALLOCATION_PROFILE_CONFLICT = "allocation_profile_conflict"


class AllocationSleeveKind(str, Enum):
    GOAL = "goal"
    OBLIGATION = "obligation"
    RESIDUAL = "residual"


class AllocationConstraintCode(str, Enum):
    NEAR_TERM_OBLIGATION_GAP = "near_term_obligation_gap"
    NEAR_TERM_GOAL_GAP = "near_term_goal_gap"
    MONTHLY_CEILING_CONSTRAINED = "monthly_ceiling_constrained"
    FUNDING_GAP_WITHOUT_RETURN = "funding_gap_without_return"
    NO_CURRENT_INVESTABLE_STOCK = "no_current_investable_stock"
    HORIZON_BINDING = "horizon_binding"
    LOSS_AMOUNT_BINDING = "loss_amount_binding"
    DRAWDOWN_BINDING = "drawdown_binding"
    WILLINGNESS_BINDING = "willingness_binding"
    STABILITY_BINDING = "stability_binding"


class AllocationProfileConflictCode(str, Enum):
    PROFILE_DISALLOWS_GOAL_POSTPONEMENT = "profile_disallows_goal_postponement"


class GoalFundingState(str, Enum):
    FULLY_FUNDED_NOW = "fully_funded_now"
    FUNDABLE_WITHOUT_RETURN = "fundable_without_return"
    FUNDING_GAP_WITHOUT_RETURN = "funding_gap_without_return"
    ALLOCATION_HORIZON_MISSING = "allocation_horizon_missing"


NO_CURRENT_INVESTABLE_STOCK = AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK

REGION_INEQUALITIES = (
    "E+B+C=1",
    "E>=0",
    "B>=0",
    "C>=0",
    "0.50E+0.10B<=D",
    "I(0.50E+0.10B)<=L",
    "E<=weighted_horizon_ceiling",
    "E<=behavioral_willingness_ceiling",
    "E<=financial_stability_ceiling",
)

_MAX_SAFE_DECIMAL_PRECISION = 10_000
_QUANTIZE_GUARD_DIGITS = 16
_DECIMAL_SIGNALS = (
    InvalidOperation,
    FloatOperation,
    DivisionByZero,
    Overflow,
    Underflow,
    Subnormal,
    Inexact,
    Rounded,
    Clamped,
)
_TRAPPED_DECIMAL_SIGNALS = {
    InvalidOperation,
    FloatOperation,
    DivisionByZero,
    Overflow,
}


def _add_years_clamped(value: date, years: int) -> date:
    if type(value) is not date or type(years) is not int:
        raise ValueError("calendar horizon inputs must use exact date and integer values")
    target_year = value.year + years
    if target_year > date.max.year:
        return date.max
    try:
        return value.replace(year=target_year)
    except ValueError:
        if value.month == 2 and value.day == 29:
            return value.replace(month=2, day=28, year=target_year)
        raise


def horizon_equity_ceiling_v1(assessment_date: date, target_date: date) -> Decimal:
    if type(assessment_date) is not date or type(target_date) is not date:
        raise ValueError("calendar horizons require exact date values")
    for years, ceiling in (
        (1, Decimal("0")),
        (3, Decimal("0.10")),
        (5, Decimal("0.30")),
        (8, Decimal("0.50")),
    ):
        if target_date <= _add_years_clamped(assessment_date, years):
            return ceiling
    return Decimal("0.70")


def _non_negative_decimal(value: object, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be an exact finite non-negative Decimal")


def _required_quantize_precision(value: Decimal, quantum: Decimal, name: str) -> int:
    adjusted = value.adjusted() if value else 0
    quantum_exponent = quantum.as_tuple().exponent
    required = max(
        28,
        len(value.as_tuple().digits) + _QUANTIZE_GUARD_DIGITS,
        adjusted - quantum_exponent + 1 + _QUANTIZE_GUARD_DIGITS,
    )
    if required > _MAX_SAFE_DECIMAL_PRECISION:
        raise ValueError(f"{name} cannot be quantized safely")
    return required


def _fixed_decimal_context(precision: int) -> Context:
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-_MAX_SAFE_DECIMAL_PRECISION,
        Emax=_MAX_SAFE_DECIMAL_PRECISION,
        capitals=1,
        clamp=0,
    )
    for signal in _DECIMAL_SIGNALS:
        context.traps[signal] = signal in _TRAPPED_DECIMAL_SIGNALS
    context.clear_flags()
    return context


def _safe_quantize(
    value: Decimal,
    quantum: Decimal,
    name: str,
    *,
    rounding: Optional[str] = None,
) -> Decimal:
    precision = _required_quantize_precision(value, quantum, name)
    try:
        with localcontext(_fixed_decimal_context(precision)):
            return value.quantize(quantum, rounding=rounding)
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be quantized safely") from exc


def _arithmetic_precision(values: Tuple[Decimal, ...], name: str) -> int:
    if not values:
        return 28
    maximum_adjusted = max(value.adjusted() if value else 0 for value in values)
    minimum_exponent = min(value.as_tuple().exponent for value in values)
    required = max(
        28,
        maximum_adjusted - minimum_exponent + 1 + _QUANTIZE_GUARD_DIGITS,
        sum(len(value.as_tuple().digits) for value in values) + _QUANTIZE_GUARD_DIGITS,
    )
    if required > _MAX_SAFE_DECIMAL_PRECISION:
        raise ValueError(f"{name} cannot be calculated safely")
    return required


def _safe_add(left: Decimal, right: Decimal, name: str) -> Decimal:
    try:
        with localcontext(_fixed_decimal_context(_arithmetic_precision((left, right), name))):
            return left + right
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be calculated safely") from exc


def _safe_subtract(left: Decimal, right: Decimal, name: str) -> Decimal:
    try:
        with localcontext(_fixed_decimal_context(_arithmetic_precision((left, right), name))):
            return left - right
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be calculated safely") from exc


def _safe_multiply(left: Decimal, right: object, name: str) -> Decimal:
    if type(right) is int:
        right_decimal = Decimal(right)
    elif type(right) is Decimal:
        right_decimal = right
    else:
        raise ValueError(f"{name} cannot be calculated safely")
    try:
        with localcontext(
            _fixed_decimal_context(_arithmetic_precision((left, right_decimal), name))
        ):
            return left * right_decimal
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be calculated safely") from exc


def _safe_divide(numerator: Decimal, denominator: Decimal, name: str) -> Decimal:
    try:
        precision = max(
            64,
            _arithmetic_precision((numerator, denominator), name),
        )
        with localcontext(_fixed_decimal_context(precision)):
            return numerator / denominator
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be calculated safely") from exc


def _safe_sum(values: Tuple[Decimal, ...], name: str) -> Decimal:
    if not values:
        return Decimal("0")
    try:
        with localcontext(_fixed_decimal_context(_arithmetic_precision(values, name))):
            total = Decimal("0")
            for value in values:
                total += value
            return total
    except DecimalException as exc:
        raise ValueError(f"{name} cannot be calculated safely") from exc


def _safe_ratio_floor(
    numerator: Decimal,
    denominator: Decimal,
    name: str,
    *,
    denominator_scale: Decimal = Decimal("1"),
    cap_at_one: bool = False,
) -> Decimal:
    scaled_denominator = _safe_multiply(
        denominator,
        denominator_scale,
        name,
    )
    ratio = _safe_divide(numerator, scaled_denominator, name)
    if cap_at_one:
        ratio = min(Decimal("1"), ratio)
    return _safe_quantize(
        ratio,
        Decimal("0.01"),
        name,
        rounding=ROUND_FLOOR,
    )


def _money(value: object, name: str) -> None:
    _non_negative_decimal(value, name)
    assert type(value) is Decimal
    if value != _safe_quantize(value, Decimal("0.01"), name):
        raise ValueError(f"{name} must be quantized to CNY cents")


def _fraction(value: object, name: str, *, whole_percentage: bool = False) -> None:
    if type(value) is not Decimal or not value.is_finite() or not Decimal("0") <= value <= 1:
        raise ValueError(f"{name} must be an exact Decimal between zero and one")
    if whole_percentage and value != _safe_quantize(
        value,
        Decimal("0.01"),
        name,
    ):
        raise ValueError(f"{name} must be rounded down to a whole percentage point")


def _non_negative_integer(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _unique_enum_tuple(value: object, enum_type: type, name: str) -> None:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be a tuple")
    if any(type(item) is not enum_type for item in value):
        raise ValueError(f"{name} must contain exact {enum_type.__name__} values")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates")


def _require_declared_dataclass_state(value: object, name: str) -> None:
    state = vars(value)
    expected = {field.name for field in dataclass_fields(type(value))}
    if type(state) is not dict or set(state) != expected:
        raise ValueError(f"{name} has unexpected dataclass state")


@dataclass(frozen=True)
class GoalFundingDetail:
    name: str
    target_date: date
    target_amount: Decimal
    amount_already_reserved: Decimal
    confirmed_monthly_saving: Decimal
    remaining_contribution_periods: int
    zero_return_funding: Decimal
    funding_state: GoalFundingState
    horizon_equity_ceiling: Decimal

    def validate(self) -> None:
        if type(self) is not GoalFundingDetail:
            raise ValueError("goal detail must be an exact GoalFundingDetail")
        _require_declared_dataclass_state(self, "goal detail")
        validate_private_name(self.name, "goal name")
        if type(self.target_date) is not date:
            raise ValueError("goal target_date must be a date")
        for value, name in (
            (self.target_amount, "goal target amount"),
            (self.amount_already_reserved, "goal reserved amount"),
            (self.confirmed_monthly_saving, "confirmed monthly goal saving"),
            (self.zero_return_funding, "zero-return funding"),
        ):
            _money(value, name)
        if self.amount_already_reserved > self.target_amount:
            raise ValueError("goal reserved amount cannot exceed target amount")
        _non_negative_integer(
            self.remaining_contribution_periods,
            "remaining contribution periods",
        )
        if type(self.funding_state) is not GoalFundingState:
            raise ValueError("funding_state must be an exact GoalFundingState")
        _fraction(
            self.horizon_equity_ceiling,
            "goal horizon equity ceiling",
            whole_percentage=True,
        )
        expected_funding = _safe_add(
            self.amount_already_reserved,
            _safe_multiply(
                self.confirmed_monthly_saving,
                self.remaining_contribution_periods,
                "goal confirmed saving",
            ),
            "goal zero-return funding",
        )
        if self.zero_return_funding != expected_funding:
            raise ValueError("zero-return funding must equal reserved plus confirmed saving")
        expected_state = GoalFundingState.FUNDING_GAP_WITHOUT_RETURN
        if self.amount_already_reserved >= self.target_amount:
            expected_state = GoalFundingState.FULLY_FUNDED_NOW
        elif self.zero_return_funding >= self.target_amount:
            expected_state = GoalFundingState.FUNDABLE_WITHOUT_RETURN
        if self.funding_state is not expected_state:
            raise ValueError("funding_state must match the zero-return funding calculation")


@dataclass(frozen=True)
class ObligationFundingDetail:
    name: str
    due_date: date
    amount: Decimal
    amount_already_reserved: Decimal
    funding_gap: Decimal
    confirmed_monthly_saving: Decimal
    remaining_contribution_periods: int
    zero_return_funding: Decimal
    horizon_equity_ceiling: Decimal

    def validate(self) -> None:
        if type(self) is not ObligationFundingDetail:
            raise ValueError("obligation detail must be an exact ObligationFundingDetail")
        _require_declared_dataclass_state(self, "obligation detail")
        validate_private_name(self.name, "obligation name")
        if type(self.due_date) is not date:
            raise ValueError("obligation due_date must be a date")
        for value, name in (
            (self.amount, "obligation amount"),
            (self.amount_already_reserved, "obligation reserved amount"),
            (self.funding_gap, "obligation funding gap"),
            (self.confirmed_monthly_saving, "confirmed monthly obligation saving"),
            (self.zero_return_funding, "obligation zero-return funding"),
        ):
            _money(value, name)
        if self.amount_already_reserved > self.amount:
            raise ValueError("obligation reserved amount cannot exceed obligation amount")
        expected_gap = max(
            Decimal("0"),
            _safe_subtract(
                self.amount,
                self.amount_already_reserved,
                "obligation funding gap",
            ),
        )
        if self.funding_gap != expected_gap:
            raise ValueError("obligation funding gap must equal amount less reserved amount")
        _non_negative_integer(
            self.remaining_contribution_periods,
            "obligation remaining contribution periods",
        )
        expected_funding = _safe_add(
            self.amount_already_reserved,
            _safe_multiply(
                self.confirmed_monthly_saving,
                self.remaining_contribution_periods,
                "obligation confirmed saving",
            ),
            "obligation zero-return funding",
        )
        if self.zero_return_funding != expected_funding:
            raise ValueError(
                "obligation zero-return funding must equal reserved plus confirmed saving"
            )
        _fraction(
            self.horizon_equity_ceiling,
            "obligation horizon equity ceiling",
            whole_percentage=True,
        )


@dataclass(frozen=True)
class AssignedSleeveDetail:
    sleeve_kind: AllocationSleeveKind
    name: str
    assigned_amount: Decimal
    horizon_date: date
    horizon_equity_ceiling: Decimal
    weighted_equity_contribution: Decimal

    def validate(self) -> None:
        if type(self) is not AssignedSleeveDetail:
            raise ValueError("assigned sleeve must be an exact AssignedSleeveDetail")
        _require_declared_dataclass_state(self, "assigned sleeve")
        if type(self.sleeve_kind) is not AllocationSleeveKind:
            raise ValueError("sleeve_kind must be an AllocationSleeveKind")
        validate_private_name(self.name, "assigned sleeve name")
        _money(self.assigned_amount, "assigned sleeve amount")
        if type(self.horizon_date) is not date:
            raise ValueError("assigned sleeve horizon_date must be a date")
        _fraction(
            self.horizon_equity_ceiling,
            "assigned sleeve horizon equity ceiling",
            whole_percentage=True,
        )
        _non_negative_decimal(
            self.weighted_equity_contribution,
            "weighted equity contribution",
        )
        expected_contribution = _safe_multiply(
            self.assigned_amount,
            self.horizon_equity_ceiling,
            "weighted equity contribution",
        )
        if self.weighted_equity_contribution != expected_contribution:
            raise ValueError(
                "weighted equity contribution must equal assigned amount times ceiling"
            )


@dataclass(frozen=True)
class AggregateAllocationInputs:
    weighted_horizon_numerator: Decimal
    weighted_horizon_equity_ceiling: Decimal
    loss_amount_equity_ceiling: Decimal
    drawdown_equity_ceiling: Decimal
    willingness_equity_ceiling: Decimal
    stability_equity_ceiling: Decimal
    fixed_income_stress_loss: Decimal
    equity_stress_loss: Decimal

    def validate(self) -> None:
        if type(self) is not AggregateAllocationInputs:
            raise ValueError("aggregate inputs must be exact AggregateAllocationInputs")
        _require_declared_dataclass_state(self, "aggregate inputs")
        _non_negative_decimal(
            self.weighted_horizon_numerator,
            "weighted horizon numerator",
        )
        for value, name in (
            (self.weighted_horizon_equity_ceiling, "weighted horizon equity ceiling"),
            (self.loss_amount_equity_ceiling, "loss amount equity ceiling"),
            (self.drawdown_equity_ceiling, "drawdown equity ceiling"),
            (self.willingness_equity_ceiling, "willingness equity ceiling"),
            (self.stability_equity_ceiling, "stability equity ceiling"),
            (self.fixed_income_stress_loss, "fixed-income stress loss"),
            (self.equity_stress_loss, "equity stress loss"),
        ):
            _fraction(value, name, whole_percentage=True)
        if self.fixed_income_stress_loss != Decimal("0.10"):
            raise ValueError("fixed-income stress loss must remain Phase C V1 0.10")
        if self.equity_stress_loss != Decimal("0.50"):
            raise ValueError("equity stress loss must remain Phase C V1 0.50")


@dataclass(frozen=True)
class AllocationSafeSummary:
    goal_count: int
    obligation_count: int
    fully_funded_now_count: int
    fundable_without_return_count: int
    funding_gap_without_return_count: int
    horizon_equity_ceilings: Tuple[Decimal, ...]

    def validate(self) -> None:
        if type(self) is not AllocationSafeSummary:
            raise ValueError("safe_summary must be an exact AllocationSafeSummary")
        _require_declared_dataclass_state(self, "safe_summary")
        for value, name in (
            (self.goal_count, "goal_count"),
            (self.obligation_count, "obligation_count"),
            (self.fully_funded_now_count, "fully_funded_now_count"),
            (self.fundable_without_return_count, "fundable_without_return_count"),
            (self.funding_gap_without_return_count, "funding_gap_without_return_count"),
        ):
            _non_negative_integer(value, name)
        funding_state_count = (
            self.fully_funded_now_count
            + self.fundable_without_return_count
            + self.funding_gap_without_return_count
        )
        if funding_state_count > self.goal_count:
            raise ValueError("goal funding-state counts cannot exceed goal_count")
        if type(self.horizon_equity_ceilings) is not tuple:
            raise ValueError("horizon_equity_ceilings must be a tuple")
        for value in self.horizon_equity_ceilings:
            _fraction(value, "horizon equity ceiling", whole_percentage=True)


@dataclass(frozen=True)
class PermittedRegion:
    inequalities: Tuple[str, ...]
    maximum_equity: Decimal
    horizon_equity_ceiling: Decimal
    loss_amount_equity_ceiling: Decimal
    drawdown_equity_ceiling: Decimal
    willingness_equity_ceiling: Decimal
    stability_equity_ceiling: Decimal

    def validate(self) -> None:
        if type(self) is not PermittedRegion:
            raise ValueError("permitted_region must be an exact PermittedRegion")
        _require_declared_dataclass_state(self, "permitted_region")
        if type(self.inequalities) is not tuple or any(
            type(item) is not str for item in self.inequalities
        ):
            raise ValueError("inequalities must contain exact strings")
        if self.inequalities != REGION_INEQUALITIES:
            raise ValueError("inequalities must be the fixed region inequalities")
        ceilings = (
            (self.maximum_equity, "maximum equity"),
            (self.horizon_equity_ceiling, "horizon equity ceiling"),
            (self.loss_amount_equity_ceiling, "loss amount equity ceiling"),
            (self.drawdown_equity_ceiling, "drawdown equity ceiling"),
            (self.willingness_equity_ceiling, "willingness equity ceiling"),
            (self.stability_equity_ceiling, "stability equity ceiling"),
        )
        for value, name in ceilings:
            _fraction(value, name, whole_percentage=True)
        if self.maximum_equity != min(value for value, _ in ceilings[1:]):
            raise ValueError("maximum equity must equal the minimum input ceiling")


@dataclass(frozen=True)
class AllocationExactResult:
    assessment_date: date
    total_financial_assets: Decimal
    liquid_protection_assets: Decimal
    verified_emergency_reserve: Decimal
    minimum_operating_cash: Decimal
    protected_short_term_assigned: Decimal
    protected_liquid_claims: Decimal
    investable_stock_assets: Decimal
    monthly_discretionary_allocation_ceiling: Decimal
    maximum_tolerable_loss: Decimal
    maximum_tolerable_drawdown: Decimal
    residual_horizon_date: Optional[date]
    goal_funding_details: Tuple[GoalFundingDetail, ...]
    obligation_funding_details: Tuple[ObligationFundingDetail, ...]
    assigned_sleeves: Tuple[AssignedSleeveDetail, ...]
    aggregate_inputs: AggregateAllocationInputs

    def validate(self) -> None:
        if type(self) is not AllocationExactResult:
            raise ValueError("exact must be an exact AllocationExactResult")
        _require_declared_dataclass_state(self, "exact")
        if type(self.assessment_date) is not date:
            raise ValueError("assessment_date must be an exact date")
        for value, name in (
            (self.total_financial_assets, "total financial assets"),
            (self.liquid_protection_assets, "liquid protection assets"),
            (self.verified_emergency_reserve, "verified emergency reserve"),
            (self.minimum_operating_cash, "minimum operating cash"),
            (self.protected_short_term_assigned, "protected short-term assigned"),
            (self.protected_liquid_claims, "protected liquid claims"),
            (self.investable_stock_assets, "investable stock assets"),
            (
                self.monthly_discretionary_allocation_ceiling,
                "monthly discretionary allocation ceiling",
            ),
            (self.maximum_tolerable_loss, "maximum tolerable loss"),
        ):
            _money(value, name)
        if self.liquid_protection_assets > self.total_financial_assets:
            raise ValueError("liquid protection assets cannot exceed total financial assets")
        if self.protected_liquid_claims > self.liquid_protection_assets:
            raise ValueError("protected liquid claims cannot exceed liquid protection assets")
        if self.protected_short_term_assigned > self.protected_liquid_claims:
            raise ValueError("protected short-term assigned cannot exceed protected liquid claims")
        expected_investable = max(
            Decimal("0"),
            _safe_subtract(
                self.total_financial_assets,
                self.protected_liquid_claims,
                "investable stock assets",
            ),
        )
        if self.investable_stock_assets != expected_investable:
            raise ValueError("investable stock assets must match total less protected claims")
        _fraction(self.maximum_tolerable_drawdown, "maximum tolerable drawdown")
        if self.residual_horizon_date is not None and type(self.residual_horizon_date) is not date:
            raise ValueError("residual_horizon_date must be a date or None")
        if type(self.goal_funding_details) is not tuple:
            raise ValueError("goal_funding_details must be a tuple")
        for detail in self.goal_funding_details:
            if type(detail) is not GoalFundingDetail:
                raise ValueError("goal_funding_details must contain exact GoalFundingDetail values")
            detail.validate()
        if type(self.obligation_funding_details) is not tuple:
            raise ValueError("obligation_funding_details must be a tuple")
        for detail in self.obligation_funding_details:
            if type(detail) is not ObligationFundingDetail:
                raise ValueError(
                    "obligation_funding_details must contain exact ObligationFundingDetail values"
                )
            detail.validate()
        if type(self.assigned_sleeves) is not tuple:
            raise ValueError("assigned_sleeves must be a tuple")
        for detail in self.assigned_sleeves:
            if type(detail) is not AssignedSleeveDetail:
                raise ValueError("assigned_sleeves must contain exact AssignedSleeveDetail values")
            detail.validate()
        if type(self.aggregate_inputs) is not AggregateAllocationInputs:
            raise ValueError("aggregate_inputs must be exact AggregateAllocationInputs")
        self.aggregate_inputs.validate()
        self._validate_horizon_dates()
        self._validate_protected_claims()
        self._validate_sleeves()

    def _validate_protected_claims(self) -> None:
        short_term_cutoff = _add_years_clamped(self.assessment_date, 3)
        expected_short_term_assigned = _safe_sum(
            tuple(
                detail.amount_already_reserved
                for detail in self.goal_funding_details
                if detail.target_date <= short_term_cutoff
            )
            + tuple(
                detail.amount_already_reserved
                for detail in self.obligation_funding_details
                if detail.due_date <= short_term_cutoff
            ),
            "protected short-term assigned",
        )
        if self.protected_short_term_assigned != expected_short_term_assigned:
            raise ValueError("protected short-term assigned must equal reserved short-term details")
        expected_protected_claims = _safe_add(
            _safe_add(
                self.verified_emergency_reserve,
                self.minimum_operating_cash,
                "protected liquid claims",
            ),
            self.protected_short_term_assigned,
            "protected liquid claims",
        )
        if self.protected_liquid_claims != expected_protected_claims:
            raise ValueError(
                "protected liquid claims must equal reserve, operating cash, and assigned claims"
            )

    def _validate_horizon_dates(self) -> None:
        for detail in self.goal_funding_details:
            if detail.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                detail.target_date,
            ):
                raise ValueError("goal must use its date-derived horizon equity ceiling")
        for detail in self.obligation_funding_details:
            if detail.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                detail.due_date,
            ):
                raise ValueError("obligation must use its date-derived horizon equity ceiling")
        for detail in self.assigned_sleeves:
            if detail.horizon_equity_ceiling != horizon_equity_ceiling_v1(
                self.assessment_date,
                detail.horizon_date,
            ):
                raise ValueError("assigned sleeve must use its date-derived horizon equity ceiling")

    def _validate_sleeves(self) -> None:
        aggregate = self.aggregate_inputs
        available_sleeves = Counter(
            (
                AllocationSleeveKind.GOAL,
                detail.name,
                detail.target_date,
                detail.amount_already_reserved,
                detail.horizon_equity_ceiling,
            )
            for detail in self.goal_funding_details
            if detail.amount_already_reserved > 0
            and detail.target_date > _add_years_clamped(self.assessment_date, 3)
        )
        available_sleeves.update(
            (
                AllocationSleeveKind.OBLIGATION,
                detail.name,
                detail.due_date,
                detail.amount_already_reserved,
                detail.horizon_equity_ceiling,
            )
            for detail in self.obligation_funding_details
            if detail.amount_already_reserved > 0
            and detail.due_date > _add_years_clamped(self.assessment_date, 3)
        )
        assigned_sleeves = Counter(
            (
                detail.sleeve_kind,
                detail.name,
                detail.horizon_date,
                detail.assigned_amount,
                detail.horizon_equity_ceiling,
            )
            for detail in self.assigned_sleeves
            if detail.sleeve_kind is not AllocationSleeveKind.RESIDUAL
        )
        if assigned_sleeves != available_sleeves:
            raise ValueError(
                "assigned non-residual sleeves must equal the exact multiset of long-term details"
            )
        if self.investable_stock_assets == 0:
            if (
                self.assigned_sleeves
                or self.residual_horizon_date is not None
                or aggregate.weighted_horizon_numerator != 0
                or aggregate.weighted_horizon_equity_ceiling != 0
            ):
                raise ValueError(
                    "zero investable stock requires no sleeves or weighted horizon inputs"
                )
            return
        residual_sleeves = tuple(
            detail
            for detail in self.assigned_sleeves
            if detail.sleeve_kind is AllocationSleeveKind.RESIDUAL
        )
        if len(residual_sleeves) > 1:
            raise ValueError("assigned sleeves can contain at most one residual sleeve")
        if residual_sleeves:
            if self.residual_horizon_date != residual_sleeves[0].horizon_date:
                raise ValueError(
                    "residual_horizon_date must match the residual sleeve horizon_date"
                )
        elif self.residual_horizon_date is not None:
            raise ValueError("residual_horizon_date must be None without a residual sleeve")
        assigned_total = _safe_sum(
            tuple(detail.assigned_amount for detail in self.assigned_sleeves),
            "assigned sleeve amount total",
        )
        if assigned_total != self.investable_stock_assets:
            raise ValueError("assigned sleeve amounts must equal investable stock assets")
        weighted_total = _safe_sum(
            tuple(detail.weighted_equity_contribution for detail in self.assigned_sleeves),
            "weighted horizon numerator",
        )
        if aggregate.weighted_horizon_numerator != weighted_total:
            raise ValueError("weighted horizon numerator must equal assigned sleeve contributions")
        expected_ceiling = _safe_ratio_floor(
            aggregate.weighted_horizon_numerator,
            self.investable_stock_assets,
            "weighted horizon equity ceiling",
        )
        if aggregate.weighted_horizon_equity_ceiling != expected_ceiling:
            raise ValueError("weighted horizon equity ceiling must equal the rounded sleeve result")


@dataclass(frozen=True)
class AllocationResult:
    status: AllocationStatus
    capability: str
    blocks: Tuple[AllocationBlockCode, ...]
    binding_constraints: Tuple[AllocationConstraintCode, ...]
    profile_conflicts: Tuple[AllocationProfileConflictCode, ...]
    safe_summary: AllocationSafeSummary
    permitted_region: Optional[PermittedRegion]
    exact: Optional[AllocationExactResult]

    def validate(self) -> None:
        if type(self) is not AllocationResult:
            raise ValueError("result must be an exact AllocationResult")
        _require_declared_dataclass_state(self, "result")
        if type(self.status) is not AllocationStatus:
            raise ValueError("status must be an AllocationStatus")
        if type(self.capability) is not str or self.capability != "research_only":
            raise ValueError("capability must be research_only")
        _unique_enum_tuple(self.blocks, AllocationBlockCode, "blocks")
        _unique_enum_tuple(
            self.binding_constraints,
            AllocationConstraintCode,
            "binding_constraints",
        )
        _unique_enum_tuple(
            self.profile_conflicts,
            AllocationProfileConflictCode,
            "profile_conflicts",
        )
        if type(self.safe_summary) is not AllocationSafeSummary:
            raise ValueError("safe_summary must be an exact AllocationSafeSummary")
        self.safe_summary.validate()
        if self.permitted_region is not None:
            if type(self.permitted_region) is not PermittedRegion:
                raise ValueError("permitted_region must be an exact PermittedRegion or None")
            self.permitted_region.validate()
        if self.status is AllocationStatus.BLOCKED:
            if not self.blocks or self.permitted_region is not None:
                raise ValueError("status must match blocks and permitted region")
            if self.exact is not None:
                raise ValueError("blocked allocation must not contain exact calculations")
            inherited_phase_b_constraints = {
                AllocationConstraintCode.NEAR_TERM_OBLIGATION_GAP,
                AllocationConstraintCode.NEAR_TERM_GOAL_GAP,
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
            }
            if not set(self.binding_constraints).issubset(inherited_phase_b_constraints):
                raise ValueError(
                    "blocked allocation may contain only inherited Phase B constraints"
                )
            has_conflict_block = AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT in self.blocks
            if has_conflict_block != bool(self.profile_conflicts):
                raise ValueError("profile conflict block must match profile conflict details")
            return
        if self.blocks:
            raise ValueError("status must match blocks and permitted region")
        if self.profile_conflicts:
            raise ValueError("range_available cannot contain profile conflicts")
        if type(self.exact) is not AllocationExactResult:
            raise ValueError("range_available allocation requires exact calculations")
        self.exact.validate()
        self._validate_exact_summary()
        self._validate_region()

    def _validate_exact_summary(self) -> None:
        assert self.exact is not None
        goals = self.exact.goal_funding_details
        obligations = self.exact.obligation_funding_details
        state_counts = {
            state: sum(detail.funding_state is state for detail in goals)
            for state in (
                GoalFundingState.FULLY_FUNDED_NOW,
                GoalFundingState.FUNDABLE_WITHOUT_RETURN,
                GoalFundingState.FUNDING_GAP_WITHOUT_RETURN,
            )
        }
        if self.safe_summary.goal_count != len(goals):
            raise ValueError("safe_summary goal_count must match exact goal details")
        if self.safe_summary.obligation_count != len(obligations):
            raise ValueError("safe_summary obligation_count must match exact obligation details")
        if (
            self.safe_summary.fully_funded_now_count
            != state_counts[GoalFundingState.FULLY_FUNDED_NOW]
            or self.safe_summary.fundable_without_return_count
            != state_counts[GoalFundingState.FUNDABLE_WITHOUT_RETURN]
            or self.safe_summary.funding_gap_without_return_count
            != state_counts[GoalFundingState.FUNDING_GAP_WITHOUT_RETURN]
        ):
            raise ValueError("safe_summary funding-state counts must match exact goal details")
        expected_horizons = tuple(
            detail.horizon_equity_ceiling for detail in self.exact.assigned_sleeves
        )
        if self.safe_summary.horizon_equity_ceilings != expected_horizons:
            raise ValueError("safe_summary horizons must match exact assigned sleeves")
        has_gap = bool(state_counts[GoalFundingState.FUNDING_GAP_WITHOUT_RETURN])
        has_gap_code = (
            AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN in self.binding_constraints
        )
        if has_gap != has_gap_code:
            raise ValueError("funding-gap constraint must match exact goal states")

    def _validate_region(self) -> None:
        assert self.exact is not None
        has_no_stock = (
            AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK in self.binding_constraints
        )
        if self.exact.investable_stock_assets == 0:
            local_binding_codes = {
                AllocationConstraintCode.HORIZON_BINDING,
                AllocationConstraintCode.LOSS_AMOUNT_BINDING,
                AllocationConstraintCode.DRAWDOWN_BINDING,
                AllocationConstraintCode.WILLINGNESS_BINDING,
                AllocationConstraintCode.STABILITY_BINDING,
            }
            if local_binding_codes.intersection(self.binding_constraints):
                raise ValueError("zero stock range cannot contain local binding codes")
            if self.permitted_region is not None or not has_no_stock:
                raise ValueError(
                    "zero stock range must use no_current_investable_stock without a region"
                )
            return
        if self.permitted_region is None or has_no_stock:
            raise ValueError(
                "positive stock range must have a region without no_current_investable_stock"
            )
        aggregate = self.exact.aggregate_inputs
        region = self.permitted_region
        expected_loss_ceiling = _safe_ratio_floor(
            self.exact.maximum_tolerable_loss,
            self.exact.investable_stock_assets,
            "loss amount equity ceiling",
            denominator_scale=Decimal("0.50"),
            cap_at_one=True,
        )
        if (
            aggregate.loss_amount_equity_ceiling != expected_loss_ceiling
            or region.loss_amount_equity_ceiling != expected_loss_ceiling
        ):
            raise ValueError("loss amount equity ceiling must match the exact CNY loss calculation")
        expected_drawdown_ceiling = _safe_ratio_floor(
            self.exact.maximum_tolerable_drawdown,
            Decimal("0.50"),
            "drawdown equity ceiling",
            cap_at_one=True,
        )
        if (
            aggregate.drawdown_equity_ceiling != expected_drawdown_ceiling
            or region.drawdown_equity_ceiling != expected_drawdown_ceiling
        ):
            raise ValueError("drawdown equity ceiling must match the exact drawdown calculation")
        if (
            region.horizon_equity_ceiling != aggregate.weighted_horizon_equity_ceiling
            or region.loss_amount_equity_ceiling != aggregate.loss_amount_equity_ceiling
            or region.drawdown_equity_ceiling != aggregate.drawdown_equity_ceiling
            or region.willingness_equity_ceiling != aggregate.willingness_equity_ceiling
            or region.stability_equity_ceiling != aggregate.stability_equity_ceiling
        ):
            raise ValueError("permitted region inputs must match aggregate exact inputs")
        binding_by_ceiling = {
            AllocationConstraintCode.HORIZON_BINDING: region.horizon_equity_ceiling,
            AllocationConstraintCode.LOSS_AMOUNT_BINDING: region.loss_amount_equity_ceiling,
            AllocationConstraintCode.DRAWDOWN_BINDING: region.drawdown_equity_ceiling,
            AllocationConstraintCode.WILLINGNESS_BINDING: region.willingness_equity_ceiling,
            AllocationConstraintCode.STABILITY_BINDING: region.stability_equity_ceiling,
        }
        expected_local = {
            code for code, ceiling in binding_by_ceiling.items() if ceiling == region.maximum_equity
        }
        actual_local = set(self.binding_constraints).intersection(binding_by_ceiling)
        if actual_local != expected_local:
            raise ValueError("local binding codes must exactly match all binding ceilings")
