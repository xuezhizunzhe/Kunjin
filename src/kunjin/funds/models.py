from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class DocumentKind(str, Enum):
    BASIC_PROFILE = "basic_profile"
    MANAGER_HISTORY = "manager_history"
    FEE_SCHEDULE = "fee_schedule"
    SIZE_HISTORY = "size_history"
    BENCHMARK = "benchmark"
    QUARTERLY_HOLDINGS = "quarterly_holdings"
    INDUSTRY_EXPOSURE = "industry_exposure"
    ANNOUNCEMENT = "announcement"


class FeeType(str, Enum):
    MANAGEMENT = "management"
    CUSTODY = "custody"
    SALES_SERVICE = "sales_service"
    SUBSCRIPTION = "subscription"
    REDEMPTION = "redemption"


class AssetType(str, Enum):
    STOCK = "stock"
    BOND = "bond"
    FUND = "fund"
    CASH = "cash"
    OTHER = "other"


def _validate_fund_code(fund_code: str) -> None:
    if not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError(f"invalid fund code: {fund_code}")


def _validate_required(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} is required")


def _validate_aware(value: Optional[datetime], field_name: str) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_source_document_id(source_document_id: Optional[int]) -> None:
    if source_document_id is not None and source_document_id <= 0:
        raise ValueError("source document id must be positive")


def _validate_weight(weight: Decimal) -> None:
    if weight < 0 or weight > 100:
        raise ValueError("weight must be between 0 and 100")


@dataclass(frozen=True)
class SourceDocument:
    id: Optional[int]
    fund_code: str
    document_kind: DocumentKind
    title: str
    url: str
    source_name: str
    source_tier: int
    publisher: str
    published_at: Optional[datetime]
    retrieved_at: datetime
    checksum: str

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        if self.id is not None and self.id <= 0:
            raise ValueError("source document id must be positive")
        _validate_required(self.title, "title")
        parsed_url = urlparse(self.url)
        if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
            raise ValueError("source URL must use HTTPS")
        _validate_required(self.source_name, "source name")
        if self.source_tier not in {1, 2, 3}:
            raise ValueError("source tier must be between 1 and 3")
        _validate_required(self.publisher, "publisher")
        _validate_aware(self.published_at, "published_at")
        _validate_aware(self.retrieved_at, "retrieved_at")
        if not CHECKSUM_PATTERN.fullmatch(self.checksum):
            raise ValueError("checksum must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class FundIdentity:
    fund_code: str
    fund_name: str
    status: str
    fund_type: Optional[str]
    established_date: Optional[date]
    manager_name: Optional[str]
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.fund_name, "fund name")
        _validate_required(self.status, "status")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundShareClass:
    fund_code: str
    related_fund_code: str
    share_class: str
    fund_name: Optional[str]
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_fund_code(self.related_fund_code)
        if self.share_class not in {"A", "C"}:
            raise ValueError("share class must be A or C")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundManagerTenure:
    fund_code: str
    manager_name: str
    start_date: date
    end_date: Optional[date]
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.manager_name, "manager name")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("manager end date cannot precede start date")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundFeeRule:
    fund_code: str
    fee_type: FeeType
    source_document_id: Optional[int]
    share_class: Optional[str] = None
    rate: Optional[Decimal] = None
    fixed_amount: Optional[Decimal] = None
    amount_min: Optional[Decimal] = None
    amount_max: Optional[Decimal] = None
    holding_days_min: Optional[int] = None
    holding_days_max: Optional[int] = None
    rule_order: int = 0
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    raw_rule_text: str = ""

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_source_document_id(self.source_document_id)
        if self.share_class not in {None, "A", "C"}:
            raise ValueError("share class must be A, C, or missing")
        for field_name in ("rate", "fixed_amount", "amount_min", "amount_max"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        for field_name in ("holding_days_min", "holding_days_max"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} holding day bound cannot be negative")
        if (
            self.amount_min is not None
            and self.amount_max is not None
            and self.amount_max < self.amount_min
        ):
            raise ValueError("amount interval maximum cannot be below minimum")
        if (
            self.holding_days_min is not None
            and self.holding_days_max is not None
            and self.holding_days_max < self.holding_days_min
        ):
            raise ValueError("holding day interval maximum cannot be below minimum")
        if self.rule_order < 0:
            raise ValueError("rule order cannot be negative")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective end date cannot precede start date")


@dataclass(frozen=True)
class FundSizeObservation:
    fund_code: str
    report_date: date
    net_assets: Optional[Decimal]
    total_shares: Optional[Decimal]
    published_at: Optional[datetime]
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        for field_name in ("net_assets", "total_shares"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        _validate_aware(self.published_at, "published_at")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundBenchmark:
    fund_code: str
    description: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.description, "benchmark description")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("benchmark effective end date cannot precede start date")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundHolding:
    fund_code: str
    report_period: date
    published_at: Optional[datetime]
    rank: int
    security_code: str
    security_name: str
    asset_type: AssetType
    weight: Decimal
    disclosure_scope: str
    source_document_id: Optional[int]
    shares: Optional[Decimal] = None
    market_value: Optional[Decimal] = None

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_aware(self.published_at, "published_at")
        if self.rank <= 0:
            raise ValueError("holding rank must be positive")
        _validate_required(self.security_code, "security code")
        _validate_required(self.security_name, "security name")
        _validate_weight(self.weight)
        _validate_required(self.disclosure_scope, "disclosure scope")
        for field_name in ("shares", "market_value"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} cannot be negative")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundIndustryExposure:
    fund_code: str
    report_period: date
    published_at: Optional[datetime]
    classification_standard: str
    industry_name: str
    weight: Decimal
    source_document_id: Optional[int]
    industry_code: Optional[str] = None
    market_value: Optional[Decimal] = None

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_aware(self.published_at, "published_at")
        _validate_required(self.classification_standard, "classification standard")
        _validate_required(self.industry_name, "industry name")
        _validate_weight(self.weight)
        if self.market_value is not None and self.market_value < 0:
            raise ValueError("market_value cannot be negative")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class FundAnnouncement:
    fund_code: str
    title: str
    category: Optional[str]
    publisher: str
    published_at: datetime
    url: str
    source_tier: int
    source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.title, "announcement title")
        _validate_required(self.publisher, "publisher")
        _validate_aware(self.published_at, "published_at")
        parsed_url = urlparse(self.url)
        if parsed_url.scheme.lower() != "https" or not parsed_url.hostname:
            raise ValueError("announcement URL must use HTTPS")
        if self.source_tier not in {1, 2, 3}:
            raise ValueError("source tier must be between 1 and 3")
        _validate_source_document_id(self.source_document_id)


@dataclass(frozen=True)
class DisclosureBundle:
    fund_code: str
    identity: Optional[FundIdentity]
    share_classes: Tuple[FundShareClass, ...]
    manager_tenures: Tuple[FundManagerTenure, ...]
    fee_rules: Tuple[FundFeeRule, ...]
    sizes: Tuple[FundSizeObservation, ...]
    benchmarks: Tuple[FundBenchmark, ...]
    holdings: Tuple[FundHolding, ...]
    industry_exposure: Tuple[FundIndustryExposure, ...]
    announcements: Tuple[FundAnnouncement, ...]
    source_documents: Dict[int, SourceDocument]
    section_states: Dict[str, str]
    section_statuses: Dict[str, Dict[str, Optional[str]]]
    warnings: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        facts = (
            ((self.identity,) if self.identity is not None else ())
            + self.share_classes
            + self.manager_tenures
            + self.fee_rules
            + self.sizes
            + self.benchmarks
            + self.holdings
            + self.industry_exposure
            + self.announcements
        )
        for fact in facts:
            if fact.fund_code != self.fund_code:
                raise ValueError("bundle facts must match the bundle fund code")
            fact.validate()
            source_document_id = fact.source_document_id
            if (
                source_document_id is not None
                and source_document_id not in self.source_documents
            ):
                raise ValueError("bundle fact references a missing source document")
        for source_id, source in self.source_documents.items():
            if source.id != source_id:
                raise ValueError("source document key must match its id")
            if source.fund_code != self.fund_code:
                raise ValueError("source documents must match the bundle fund code")
            source.validate()
