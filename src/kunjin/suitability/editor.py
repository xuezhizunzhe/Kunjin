from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, List, Tuple, Type, TypeVar

from kunjin.suitability.models import (
    Debt,
    DebtType,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    RiskReaction,
    validate_private_name,
)
from kunjin.suitability.service import ProfileService

_DECIMAL_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_INTEGER_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$")
_EnumValue = TypeVar("_EnumValue", bound=Enum)


class ProfileEditCancelled(Exception):
    pass


class ProfileEditor:
    def __init__(
        self,
        service: ProfileService,
        reader: Callable[[str], str] = input,
        writer: Callable[[str], None] = print,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._service = service
        self._reader = reader
        self._writer = writer
        self._now = now

    def edit(self) -> dict:
        try:
            profile = FinancialProfile(
                currency=self._required_text("Currency (CNY)"),
                monthly_net_income=self._decimal("Monthly net income"),
                monthly_essential_expenses=self._decimal("Monthly essential expenses"),
                monthly_required_debt_service=self._decimal("Monthly required debt service"),
                monthly_investment_ceiling=self._decimal("Monthly investment ceiling"),
                minimum_operating_cash=self._decimal("Minimum operating cash"),
                minimum_monthly_cash_buffer=self._decimal("Minimum monthly cash buffer"),
                income_stability=self._choice("Income stability", IncomeStability),
                income_interruption_risk=self._boolean("Income interruption risk?"),
                immediately_available_cash=self._first_asset_decimal("Immediately available cash"),
                cash_like_assets=self._decimal("Cash-like assets"),
                emergency_reserve=self._decimal("Emergency reserve"),
                low_risk_fixed_income_assets=self._decimal("Low-risk fixed-income assets"),
                manual_equity_fund_assets=self._decimal("Manual equity-fund assets"),
                manual_bond_fund_assets=self._decimal("Manual bond-fund assets"),
                manual_sector_fund_assets=self._decimal("Manual sector-fund assets"),
                dependents=self._integer("Dependents"),
                other_volatile_assets=self._decimal("Other volatile assets"),
                maximum_tolerable_loss=self._decimal("Maximum tolerable loss"),
                maximum_tolerable_drawdown=self._percentage("Maximum tolerable drawdown (%)"),
                reaction_10=self._choice("Reaction to a 10% loss", RiskReaction),
                reaction_20=self._choice("Reaction to a 20% loss", RiskReaction),
                reaction_30=self._choice("Reaction to a 30% loss", RiskReaction),
                experienced_material_loss=self._boolean("Experienced a material investment loss?"),
                understands_multi_year_recovery=self._boolean(
                    "Understand that recovery may take multiple years?"
                ),
                can_postpone_goal_use=self._boolean("Can postpone use of goal funds?"),
                debts=self._debts(),
                obligations=self._obligations(),
                goals=self._goals(),
                confirmed_at=self._current_time(),
            )
            profile.validate()
            self._write_summary(profile)
            if not self._boolean("Confirm and encrypt this profile?"):
                self._writer("Profile edit cancelled.")
                return {"status": "cancelled"}
            metadata = self._service.confirm_profile(profile)
            return {"status": "confirmed", "version": metadata.version}
        except ProfileEditCancelled:
            self._writer("Profile edit cancelled.")
            return {"status": "cancelled"}

    def _read(self, label: str) -> str:
        value = self._reader(f"{label}: ")
        if value == "cancel":
            raise ProfileEditCancelled
        return value

    def _required_text(self, label: str) -> str:
        while True:
            value = self._read(label)
            try:
                validate_private_name(value, label)
                return value.strip()
            except ValueError:
                self._writer(
                    "Enter nonblank text of at most 4096 characters without "
                    "control or unsupported characters."
                )

    def _decimal(self, label: str) -> Decimal:
        while True:
            value = self._read(label)
            if _DECIMAL_PATTERN.fullmatch(value):
                return Decimal(value)
            self._writer("Enter a non-negative decimal without commas.")

    def _first_asset_decimal(self, label: str) -> Decimal:
        self._writer(
            "Asset balances are mutually exclusive as-of balances: enter "
            "each real balance in exactly one field. The engine cannot prove "
            "whether entries overlap real accounts. Any real product entered "
            "under Low-risk fixed-income assets is only the owner's "
            "bookkeeping classification; it is not verified as low risk or "
            "high quality and is not placed in Phase C's abstract "
            "high_quality_fixed_income bucket. Phase D verification is "
            "required."
        )
        return self._decimal(label)

    def _integer(self, label: str) -> int:
        while True:
            value = self._read(label)
            if _INTEGER_PATTERN.fullmatch(value):
                return int(value)
            self._writer("Enter a non-negative whole number.")

    def _positive_integer(self, label: str) -> int:
        while True:
            value = self._integer(label)
            if value > 0:
                return value
            self._writer("Enter a positive whole number.")

    def _boolean(self, label: str) -> bool:
        while True:
            value = self._read(f"{label} [yes/no]")
            if value == "yes":
                return True
            if value == "no":
                return False
            self._writer("Enter exactly yes or no.")

    def _date(self, label: str) -> date:
        while True:
            value = self._read(f"{label} [YYYY-MM-DD]")
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                self._writer("Enter a date as YYYY-MM-DD.")
                continue
            if parsed.isoformat() == value:
                return parsed
            self._writer("Enter a date as YYYY-MM-DD.")

    def _choice(self, label: str, enum_type: Type[_EnumValue]) -> _EnumValue:
        choices = tuple(str(item.value) for item in enum_type)
        while True:
            value = self._read(f"{label} [{'/'.join(choices)}]")
            try:
                return enum_type(value)
            except ValueError:
                self._writer(f"Choose one of: {', '.join(choices)}.")

    def _percentage(self, label: str) -> Decimal:
        while True:
            percentage = self._decimal(label)
            if percentage <= Decimal("100"):
                return percentage / Decimal("100")
            self._writer("Enter a percentage between 0 and 100.")

    def _debts(self) -> Tuple[Debt, ...]:
        debts: List[Debt] = []
        while self._boolean("Add a debt?"):
            debt_type = self._choice("Debt type", DebtType)
            if debt_type in (DebtType.BUSINESS_LOAN, DebtType.OTHER):
                self._writer(
                    "This debt type cannot pass suitability policy v1 until "
                    "its risk type is clarified."
                )
            debt = Debt(
                debt_type=debt_type.value,
                outstanding_principal=self._decimal("Debt outstanding principal"),
                effective_annual_rate=self._percentage("Debt effective annual rate (%)"),
                monthly_payment=self._decimal("Debt monthly payment"),
                maturity_date=(
                    self._date("Debt maturity date")
                    if self._boolean("Debt has a maturity date?")
                    else None
                ),
                delinquent=self._boolean("Debt is delinquent?"),
                revolving_interest=self._boolean("Debt charges revolving interest?"),
            )
            debt.validate()
            debts.append(debt)
        return tuple(debts)

    def _obligations(self) -> Tuple[PlannedObligation, ...]:
        obligations: List[PlannedObligation] = []
        while self._boolean("Add a planned obligation?"):
            obligation = PlannedObligation(
                name=self._required_text("Obligation name"),
                amount=self._decimal("Obligation amount"),
                due_date=self._date("Obligation due date"),
                amount_already_reserved=self._decimal("Obligation amount already reserved"),
            )
            obligation.validate()
            obligations.append(obligation)
        return tuple(obligations)

    def _goals(self) -> Tuple[FinancialGoal, ...]:
        self._writer(
            "Goal dates remain authoritative until a new profile is "
            "confirmed. If 'Can postpone use of goal funds?' is no, it "
            "conflicts with any goal-level yes; a profile-level yes does "
            "not override a goal-level no. Dates are never extended "
            "automatically."
        )
        goals: List[FinancialGoal] = []
        while self._boolean("Add a financial goal?"):
            goal = FinancialGoal(
                name=self._required_text("Goal name"),
                target_amount=self._decimal("Goal target amount"),
                target_date=self._date("Goal target date"),
                priority=self._positive_integer("Goal priority"),
                amount_already_reserved=self._decimal("Goal amount already reserved"),
                temporary_principal_loss_acceptable=self._boolean(
                    "Temporary principal loss acceptable for this goal?"
                ),
                use_date_can_be_postponed=self._boolean("Goal use date can be postponed?"),
            )
            goal.validate()
            goals.append(goal)
        return tuple(goals)

    def _current_time(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime):
            raise ValueError("current time must be a datetime")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("current time must be timezone-aware")
        return current

    def _write_summary(self, profile: FinancialProfile) -> None:
        self._writer("Financial profile summary")
        for name in (
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
            "confirmed_at",
        ):
            self._writer(f"{name}: {self._display(getattr(profile, name))}")
        self._write_items("debts", profile.debts)
        self._write_items("obligations", profile.obligations)
        self._write_items("goals", profile.goals)

    def _write_items(self, name: str, values: tuple) -> None:
        self._writer(f"{name}: {len(values)}")
        for index, value in enumerate(values, start=1):
            self._writer(f"{name}[{index}]")
            for field_name in value.__dataclass_fields__:
                field_value = getattr(value, field_name)
                self._writer(f"  {field_name}: {self._display(field_value)}")

    @staticmethod
    def _display(value: object) -> str:
        if isinstance(value, Enum):
            return str(value.value)
        if type(value) is bool:
            return "yes" if value else "no"
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if value is None:
            return "none"
        return str(value)
