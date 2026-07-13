from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

MAX_PRIVATE_NAME_CHARS = 4_096
UNSAFE_PRIVATE_TEXT_CODEPOINTS = frozenset(
    (
        0x061C,
        0x200E,
        0x200F,
        0x2028,
        0x2029,
        *range(0x202A, 0x202F),
        *range(0x2066, 0x2070),
    )
)


class IncomeStability(str, Enum):
    STABLE = "stable"
    VARIABLE = "variable"
    UNSTABLE = "unstable"


class RiskReaction(str, Enum):
    HOLD = "hold"
    REDUCE = "reduce"
    REDEEM = "redeem"


class DebtType(str, Enum):
    MORTGAGE = "mortgage"
    AUTO_LOAN = "auto_loan"
    CREDIT_CARD = "credit_card"
    CONSUMER_LOAN = "consumer_loan"
    PERSONAL_LOAN = "personal_loan"
    STUDENT_LOAN = "student_loan"
    BUSINESS_LOAN = "business_loan"
    OTHER = "other"


class AssessmentStatus(str, Enum):
    BLOCKED = "blocked"
    CONSTRAINED = "constrained"
    READY_FOR_ALLOCATION = "ready_for_allocation"


class BlockReason(str, Enum):
    PROFILE_MISSING = "profile_missing"
    PROFILE_INVALIDATED = "profile_invalidated"
    PROFILE_STALE = "profile_stale"
    DEBT_TYPE_UNKNOWN = "debt_type_unknown"
    DEBT_DELINQUENT = "debt_delinquent"
    REVOLVING_CREDIT = "revolving_credit"
    HIGH_INTEREST_DEBT = "high_interest_debt"
    EMERGENCY_RESERVE_SHORTFALL = "emergency_reserve_shortfall"
    OBLIGATION_OVERDUE = "obligation_overdue"
    GOAL_OVERDUE = "goal_overdue"
    CRITICAL_GOAL_SHORTFALL = "critical_goal_shortfall"
    NO_MONTHLY_INVESTABLE_CASH_FLOW = "no_monthly_investable_cash_flow"
    PROFILE_CONFLICT = "profile_conflict"


class ConstraintReason(str, Enum):
    NEAR_TERM_OBLIGATION_GAP = "near_term_obligation_gap"
    NEAR_TERM_GOAL_GAP = "near_term_goal_gap"
    MONTHLY_CEILING_CONSTRAINED = "monthly_ceiling_constrained"


class ProfileConflictCode(str, Enum):
    MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS = "monthly_required_debt_service_vs_debts"
    REACTION_10_VS_REACTION_20 = "reaction_10_vs_reaction_20"
    REACTION_20_VS_REACTION_30 = "reaction_20_vs_reaction_30"
    MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_10 = "maximum_tolerable_drawdown_vs_reaction_10"
    MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_20 = "maximum_tolerable_drawdown_vs_reaction_20"
    MAXIMUM_TOLERABLE_DRAWDOWN_VS_REACTION_30 = "maximum_tolerable_drawdown_vs_reaction_30"
    MAXIMUM_TOLERABLE_LOSS_VS_REACTIONS = "maximum_tolerable_loss_vs_reactions"
    MAXIMUM_TOLERABLE_LOSS_VS_GOALS = "maximum_tolerable_loss_vs_goals"


def validate_private_name(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be an exact string")
    if not value.strip():
        raise ValueError(f"{name} is required")
    if len(value) > MAX_PRIVATE_NAME_CHARS:
        raise ValueError(f"{name} text is too long")
    if any(
        ord(character) <= 0x1F
        or 0x7F <= ord(character) <= 0x9F
        or 0xD800 <= ord(character) <= 0xDFFF
        or ord(character) in UNSAFE_PRIVATE_TEXT_CODEPOINTS
        for character in value
    ):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def _non_negative(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} cannot be negative")


def _finite_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal):
        raise ValueError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")


def _boolean(value: bool, name: str) -> None:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")


def _date_value(value: date, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"{name} must be a date")


@dataclass(frozen=True)
class Debt:
    debt_type: str
    outstanding_principal: Decimal
    effective_annual_rate: Decimal
    monthly_payment: Decimal
    maturity_date: Optional[date]
    delinquent: bool
    revolving_interest: bool

    def validate(self) -> None:
        if not isinstance(self.debt_type, str):
            raise ValueError("debt type must be a string")
        _non_negative(self.outstanding_principal, "outstanding principal")
        _non_negative(self.effective_annual_rate, "effective annual rate")
        _non_negative(self.monthly_payment, "monthly payment")
        if self.maturity_date is not None and type(self.maturity_date) is not date:
            raise ValueError("maturity_date must be a date or None")
        _boolean(self.delinquent, "delinquent")
        _boolean(self.revolving_interest, "revolving_interest")


@dataclass(frozen=True)
class PlannedObligation:
    name: str
    amount: Decimal
    due_date: date
    amount_already_reserved: Decimal

    def validate(self) -> None:
        validate_private_name(self.name, "obligation name")
        _non_negative(self.amount, "obligation amount")
        _date_value(self.due_date, "due_date")
        _non_negative(self.amount_already_reserved, "reserved obligation amount")
        if self.amount_already_reserved > self.amount:
            raise ValueError("reserved obligation amount cannot exceed obligation amount")


@dataclass(frozen=True)
class FinancialGoal:
    name: str
    target_amount: Decimal
    target_date: date
    priority: int
    amount_already_reserved: Decimal
    temporary_principal_loss_acceptable: bool
    use_date_can_be_postponed: bool

    def validate(self) -> None:
        validate_private_name(self.name, "goal name")
        _non_negative(self.target_amount, "goal target amount")
        _date_value(self.target_date, "target_date")
        _non_negative(self.amount_already_reserved, "goal reserved amount")
        if self.amount_already_reserved > self.target_amount:
            raise ValueError("goal reserved amount cannot exceed target amount")
        if type(self.priority) is not int:
            raise ValueError("goal priority must be an integer")
        if self.priority < 1:
            raise ValueError("goal priority must be positive")
        _boolean(
            self.temporary_principal_loss_acceptable,
            "temporary_principal_loss_acceptable",
        )
        _boolean(self.use_date_can_be_postponed, "use_date_can_be_postponed")


@dataclass(frozen=True)
class FinancialProfile:
    currency: str
    monthly_net_income: Decimal
    monthly_essential_expenses: Decimal
    monthly_required_debt_service: Decimal
    monthly_investment_ceiling: Decimal
    minimum_operating_cash: Decimal
    minimum_monthly_cash_buffer: Decimal
    income_stability: IncomeStability
    income_interruption_risk: bool
    immediately_available_cash: Decimal
    cash_like_assets: Decimal
    emergency_reserve: Decimal
    low_risk_fixed_income_assets: Decimal
    manual_equity_fund_assets: Decimal
    manual_bond_fund_assets: Decimal
    manual_sector_fund_assets: Decimal
    dependents: int
    other_volatile_assets: Decimal
    maximum_tolerable_loss: Decimal
    maximum_tolerable_drawdown: Decimal
    reaction_10: RiskReaction
    reaction_20: RiskReaction
    reaction_30: RiskReaction
    experienced_material_loss: bool
    understands_multi_year_recovery: bool
    can_postpone_goal_use: bool
    debts: Tuple[Debt, ...]
    obligations: Tuple[PlannedObligation, ...]
    goals: Tuple[FinancialGoal, ...]
    confirmed_at: datetime

    def validate(self) -> None:
        if not isinstance(self.currency, str):
            raise ValueError("profile currency must be a string")
        if self.currency != "CNY":
            raise ValueError("profile currency must be CNY")
        for value, name in (
            (self.monthly_net_income, "monthly net income"),
            (self.monthly_essential_expenses, "monthly essential expenses"),
            (self.monthly_required_debt_service, "monthly required debt service"),
            (self.monthly_investment_ceiling, "monthly investment ceiling"),
            (self.minimum_operating_cash, "minimum operating cash"),
            (self.minimum_monthly_cash_buffer, "minimum monthly cash buffer"),
            (self.immediately_available_cash, "immediately available cash"),
            (self.cash_like_assets, "cash-like assets"),
            (self.emergency_reserve, "emergency reserve"),
            (self.low_risk_fixed_income_assets, "low-risk fixed-income assets"),
            (self.manual_equity_fund_assets, "manual equity-fund assets"),
            (self.manual_bond_fund_assets, "manual bond-fund assets"),
            (self.manual_sector_fund_assets, "manual sector-fund assets"),
            (self.other_volatile_assets, "other volatile assets"),
            (self.maximum_tolerable_loss, "maximum tolerable loss"),
        ):
            _non_negative(value, name)
        if not isinstance(self.maximum_tolerable_drawdown, Decimal):
            raise ValueError("drawdown must be a Decimal")
        if not self.maximum_tolerable_drawdown.is_finite() or not (
            Decimal("0") <= self.maximum_tolerable_drawdown <= Decimal("1")
        ):
            raise ValueError("drawdown must be between zero and one")
        if not isinstance(self.income_stability, IncomeStability):
            raise ValueError("income_stability must be an IncomeStability")
        for value, name in (
            (self.reaction_10, "reaction_10"),
            (self.reaction_20, "reaction_20"),
            (self.reaction_30, "reaction_30"),
        ):
            if not isinstance(value, RiskReaction):
                raise ValueError(f"{name} must be a RiskReaction")
        for value, name in (
            (self.income_interruption_risk, "income_interruption_risk"),
            (self.experienced_material_loss, "experienced_material_loss"),
            (
                self.understands_multi_year_recovery,
                "understands_multi_year_recovery",
            ),
            (self.can_postpone_goal_use, "can_postpone_goal_use"),
        ):
            _boolean(value, name)
        if type(self.dependents) is not int:
            raise ValueError("dependents must be an integer")
        if self.dependents < 0:
            raise ValueError("dependents cannot be negative")
        if type(self.confirmed_at) is not datetime:
            raise ValueError("confirmed_at must be a datetime")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmed_at must be timezone-aware")
        if not isinstance(self.debts, tuple):
            raise ValueError("debts must be a tuple")
        for item in self.debts:
            if not isinstance(item, Debt):
                raise ValueError("debts must contain Debt values")
            item.validate()
        if not isinstance(self.obligations, tuple):
            raise ValueError("obligations must be a tuple")
        for item in self.obligations:
            if not isinstance(item, PlannedObligation):
                raise ValueError("obligations must contain PlannedObligation values")
            item.validate()
        if not isinstance(self.goals, tuple):
            raise ValueError("goals must be a tuple")
        for item in self.goals:
            if not isinstance(item, FinancialGoal):
                raise ValueError("goals must contain FinancialGoal values")
            item.validate()


@dataclass(frozen=True)
class AssessmentAmounts:
    verified_emergency_reserve: Decimal
    required_emergency_reserve: Decimal
    emergency_reserve_shortfall: Decimal
    required_monthly_obligation_saving: Decimal
    required_monthly_goal_saving: Decimal
    monthly_safety_residual: Decimal
    safe_monthly_ceiling: Decimal

    @classmethod
    def zero(cls) -> AssessmentAmounts:
        zero = Decimal("0")
        return cls(zero, zero, zero, zero, zero, zero, zero)

    def validate(self) -> None:
        for value, name in (
            (self.verified_emergency_reserve, "verified emergency reserve"),
            (self.required_emergency_reserve, "required emergency reserve"),
            (self.emergency_reserve_shortfall, "emergency reserve shortfall"),
            (
                self.required_monthly_obligation_saving,
                "required monthly obligation saving",
            ),
            (self.required_monthly_goal_saving, "required monthly goal saving"),
            (self.monthly_safety_residual, "monthly safety residual"),
            (self.safe_monthly_ceiling, "safe monthly ceiling"),
        ):
            _finite_decimal(value, name)
        for value, name in (
            (self.verified_emergency_reserve, "verified emergency reserve"),
            (self.required_emergency_reserve, "required emergency reserve"),
            (self.emergency_reserve_shortfall, "emergency reserve shortfall"),
            (
                self.required_monthly_obligation_saving,
                "required monthly obligation saving",
            ),
            (self.required_monthly_goal_saving, "required monthly goal saving"),
            (self.safe_monthly_ceiling, "safe monthly ceiling"),
        ):
            if value < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class AssessmentResult:
    status: AssessmentStatus
    hard_blocks: Tuple[BlockReason, ...]
    constraints: Tuple[ConstraintReason, ...]
    required_reserve_months: int
    risk_answers_consistent: bool
    profile_conflicts: Tuple[ProfileConflictCode, ...]
    debt_count: int
    obligation_count: int
    goal_count: int
    amounts: AssessmentAmounts

    def validate(self) -> None:
        if not isinstance(self.status, AssessmentStatus):
            raise ValueError("status must be an AssessmentStatus")
        self._validate_reason_tuple(self.hard_blocks, BlockReason, "hard_blocks")
        self._validate_reason_tuple(self.constraints, ConstraintReason, "constraints")
        self._validate_reason_tuple(
            self.profile_conflicts,
            ProfileConflictCode,
            "profile_conflicts",
        )
        for value, name in (
            (self.required_reserve_months, "required_reserve_months"),
            (self.debt_count, "debt_count"),
            (self.obligation_count, "obligation_count"),
            (self.goal_count, "goal_count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        _boolean(self.risk_answers_consistent, "risk_answers_consistent")
        has_profile_conflict_block = BlockReason.PROFILE_CONFLICT in self.hard_blocks
        if has_profile_conflict_block != bool(self.profile_conflicts):
            raise ValueError("profile_conflict block must match profile conflict codes")
        risk_conflicts = {
            item
            for item in self.profile_conflicts
            if item is not ProfileConflictCode.MONTHLY_REQUIRED_DEBT_SERVICE_VS_DEBTS
        }
        if self.risk_answers_consistent == bool(risk_conflicts):
            raise ValueError("risk answers must match risk-related profile conflicts")
        if not isinstance(self.amounts, AssessmentAmounts):
            raise ValueError("amounts must be AssessmentAmounts")
        self.amounts.validate()

        expected_status = AssessmentStatus.READY_FOR_ALLOCATION
        if self.hard_blocks:
            expected_status = AssessmentStatus.BLOCKED
        elif self.constraints:
            expected_status = AssessmentStatus.CONSTRAINED
        if self.status is not expected_status:
            raise ValueError("status must match assessment reasons")

    @staticmethod
    def _validate_reason_tuple(value: object, enum_type: type, name: str) -> None:
        if not isinstance(value, tuple):
            raise ValueError(f"{name} must be a tuple")
        if any(not isinstance(item, enum_type) for item in value):
            raise ValueError(f"{name} contains an invalid reason")
        if len(set(value)) != len(value):
            raise ValueError(f"{name} cannot contain duplicates")

    def safe_summary(self) -> dict:
        return {
            "debt_count": self.debt_count,
            "goal_count": self.goal_count,
            "obligation_count": self.obligation_count,
            "required_reserve_months": self.required_reserve_months,
            "risk_answers_consistent": self.risk_answers_consistent,
        }
