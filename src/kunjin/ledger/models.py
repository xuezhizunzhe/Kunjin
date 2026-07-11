from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional


class EvidenceLevel(str, Enum):
    TRANSACTION_CONFIRMED = "transaction_confirmed"
    USER_CONFIRMED = "user_confirmed"
    POSITION_INFERRED = "position_inferred"


class TransactionType(str, Enum):
    SUBSCRIPTION = "subscription"
    RECURRING_SUBSCRIPTION = "recurring_subscription"
    REDEMPTION = "redemption"
    CASH_DIVIDEND = "cash_dividend"
    REINVESTED_DIVIDEND = "reinvested_dividend"
    CONVERSION_IN = "conversion_in"
    CONVERSION_OUT = "conversion_out"


@dataclass(frozen=True)
class OcrBlock:
    text: str
    confidence: Decimal
    x: Decimal
    y: Decimal
    width: Decimal
    height: Decimal


@dataclass(frozen=True)
class ExtractedField:
    name: str
    raw_text: str
    normalized_value: Optional[str]
    confidence: Decimal
    evidence_level: EvidenceLevel


@dataclass(frozen=True)
class LedgerDraft:
    id: Optional[int]
    source_document_id: Optional[int]
    transaction_type: TransactionType
    fund_code: Optional[str]
    fund_name: Optional[str]
    amount: Optional[Decimal]
    shares: Optional[Decimal]
    nav: Optional[Decimal]
    fee: Optional[Decimal]
    order_time: Optional[datetime]
    confirmation_time: Optional[datetime]
    evidence_level: EvidenceLevel
    field_evidence: Dict[str, str]
    status: str
    created_at: datetime


@dataclass(frozen=True)
class LedgerTransaction:
    id: Optional[int]
    source_document_id: Optional[int]
    transaction_type: TransactionType
    fund_code: str
    fund_name: Optional[str]
    amount: Optional[Decimal]
    shares: Optional[Decimal]
    nav: Optional[Decimal]
    fee: Optional[Decimal]
    order_time: Optional[datetime]
    confirmation_time: Optional[datetime]
    evidence_level: EvidenceLevel
    field_evidence: Dict[str, str]
    created_at: datetime


@dataclass(frozen=True)
class ReconciliationResult:
    fund_code: str
    status: str
    confirmed_cash_flow: Optional[Decimal]
    inferred_position_cost: Optional[Decimal]
    difference: Optional[Decimal]
    tolerance: Optional[Decimal]
    evidence_level: EvidenceLevel
    warnings: List[str] = field(default_factory=list)
