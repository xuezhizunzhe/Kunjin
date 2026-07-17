from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, List, Optional

FUND_CODE_PATTERN = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class AccountObservation:
    source: str
    source_account_id: str
    title: str
    observed_at: datetime

    def validate(self) -> None:
        if self.source != "yangjibao":
            raise ValueError("unsupported account source")
        if not self.source_account_id.strip():
            raise ValueError("source account id is required")


@dataclass(frozen=True)
class PositionObservation:
    source_account_id: str
    fund_code: str
    fund_name: str
    shares: Decimal
    observed_at: datetime
    share_class: Optional[str] = None
    formal_nav: Optional[Decimal] = None
    estimated_nav: Optional[Decimal] = None
    observed_profit: Optional[Decimal] = None

    def validate(self) -> None:
        if not FUND_CODE_PATTERN.fullmatch(self.fund_code):
            raise ValueError(f"invalid fund code: {self.fund_code}")
        if self.shares < 0:
            raise ValueError("shares cannot be negative")
        if self.share_class not in (None, "A", "C"):
            raise ValueError("share class must be A, C, or missing")


@dataclass(frozen=True)
class StoredPosition:
    account_title: str
    fund_code: str
    fund_name: str
    shares: Decimal
    observed_at: datetime
    share_class: Optional[str] = None
    formal_nav: Optional[Decimal] = None
    estimated_nav: Optional[Decimal] = None
    observed_profit: Optional[Decimal] = None


@dataclass(frozen=True)
class SyncResult:
    sync_run_id: int
    accounts: int
    positions: int
    observed_at: datetime


@dataclass(frozen=True)
class PortfolioAnalysis:
    total_value: Optional[Decimal]
    value_kind: str
    weights: Dict[str, Decimal]
    hhi: Optional[Decimal]
    largest_position_share: Optional[Decimal]
    observed_profit: Optional[Decimal]
    profit_coverage: Decimal
    evidence_level: str
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FundNavObservation:
    fund_code: str
    nav_date: date
    unit_nav: Decimal
    accumulated_nav: Optional[Decimal]
    daily_growth: Optional[Decimal]
    source: str
    retrieved_at: datetime
    corporate_action_state: str = "unknown"
    source_attempt_id: Optional[int] = None

    def validate(self) -> None:
        if not FUND_CODE_PATTERN.fullmatch(self.fund_code):
            raise ValueError(f"invalid fund code: {self.fund_code}")
        if self.unit_nav <= 0:
            raise ValueError("unit NAV must be positive")
        if self.corporate_action_state not in {"none", "present", "unknown"}:
            raise ValueError("corporate action state is invalid")
        if self.source_attempt_id is not None and (
            type(self.source_attempt_id) is not int or self.source_attempt_id <= 0
        ):
            raise ValueError("source attempt id must be a positive exact integer or None")


@dataclass(frozen=True)
class SectorObservation:
    sector_code: str
    sector_name: str
    sector_kind: str
    pct_change: Optional[Decimal]
    turnover_rate: Optional[Decimal]
    advancers: Optional[int]
    decliners: Optional[int]
    source: str
    retrieved_at: datetime

    def validate(self) -> None:
        if self.sector_kind not in {"industry", "concept"}:
            raise ValueError("sector kind must be industry or concept")
        if not self.sector_code or not self.sector_name:
            raise ValueError("sector code and name are required")


@dataclass(frozen=True)
class InvestmentThesis:
    fund_code: str
    rationale: str
    horizon: str
    invalidation: str
    created_at: datetime
    active: bool = True

    def validate(self) -> None:
        if not FUND_CODE_PATTERN.fullmatch(self.fund_code):
            raise ValueError(f"invalid fund code: {self.fund_code}")
        if not self.rationale.strip():
            raise ValueError("rationale is required")
        if not self.horizon.strip():
            raise ValueError("horizon is required")
        if not self.invalidation.strip():
            raise ValueError("invalidation condition is required")
