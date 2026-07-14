from __future__ import annotations

import json
import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional, Tuple
from urllib.parse import urlparse

FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MAX_EXCERPT_CHARACTERS = 4096
MAX_SECTION_CHARACTERS = 256

_PERSONAL_KEYS = frozenset(
    {
        "amount",
        "buy",
        "goal",
        "goal_name",
        "monthly_income",
        "obligation",
        "obligation_name",
        "personal_name",
        "profile",
        "recommended",
        "sell",
        "target",
        "user",
        "user_name",
    }
)
_FORBIDDEN_EVIDENCE_TAG_TOKENS = _PERSONAL_KEYS | frozenset(
    {"candidate", "eligible", "personal", "purchase", "recommend"}
)


class ProductFamily(str, Enum):
    MONEY_MARKET = "money_market"
    SHORT_BOND = "short_bond"
    INTERMEDIATE_BOND = "intermediate_bond"
    ORDINARY_BOND = "ordinary_bond"
    LONG_BOND = "long_bond"
    CREDIT_BOND = "credit_bond"
    CONVERTIBLE_BOND = "convertible_bond"
    FIXED_INCOME_PLUS = "fixed_income_plus"
    BOND_MIXED = "bond_mixed"
    BROAD_INDEX = "broad_index"
    INDEX_ENHANCED = "index_enhanced"
    SECTOR_THEME = "sector_theme"
    ACTIVE_EQUITY = "active_equity"
    EQUITY_MIXED = "equity_mixed"
    QDII_BROAD_EQUITY = "qdii_broad_equity"
    QDII_SECTOR_THEME = "qdii_sector_theme"
    UNSUPPORTED = "unsupported"
    UNCLASSIFIED = "unclassified"


class RiskBucket(str, Enum):
    CASH_LIKE_CANDIDATE = "cash_like_candidate"
    HIGH_QUALITY_FIXED_INCOME = "high_quality_fixed_income"
    DIVERSIFIED_EQUITY = "diversified_equity"
    CONCENTRATED_EQUITY = "concentrated_equity"
    HYBRID_RISK = "hybrid_risk"
    UNCLASSIFIED = "unclassified"


class PortfolioRole(str, Enum):
    CASH_MANAGEMENT_CANDIDATE = "cash_management_candidate"
    CORE_ELIGIBLE = "core_eligible"
    ACTIVE_DIVERSIFIER_ELIGIBLE = "active_diversifier_eligible"
    SATELLITE_ONLY = "satellite_only"
    NOT_ELIGIBLE = "not_eligible"


class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"
    STALE = "stale"
    UNCLASSIFIED = "unclassified"


class FactConfidence(str, Enum):
    EXACT = "exact"
    BOUNDED_RANGE = "bounded_range"
    PRESENT = "present"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"


class FreshnessState(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class ExternalSourceReference:
    source_namespace: str
    source_document_id: int
    fund_code: str
    document_kind: str
    section: str
    title: str
    url: str
    source_name: str
    source_tier: int
    publisher: str
    published_at: Optional[datetime]
    retrieved_at: datetime
    checksum: str

    def validate(self) -> None:
        _require_exact_dataclass_state(
            self,
            ExternalSourceReference,
            "external source reference",
        )
        if self.source_namespace != "fund_disclosure":
            raise ValueError("external source namespace must be fund_disclosure")
        _validate_positive_id(self.source_document_id, "external source document id")
        _validate_fund_code(self.fund_code)
        _validate_required_code(self.document_kind, "external document kind")
        _validate_required_code(self.section, "external source section")
        _validate_required_text(self.title, "external source title", maximum=4096)
        _validate_required_text(self.url, "external source URL", maximum=4096)
        parsed = urlparse(self.url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("external source URL has an invalid port") from None
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or port not in {None, 443}
        ):
            raise ValueError("external source URL must use HTTPS")
        _validate_required_text(self.source_name, "external source name", maximum=512)
        if type(self.source_tier) is not int or self.source_tier not in {1, 2, 3}:
            raise ValueError("external source tier must be between one and three")
        _validate_required_text(self.publisher, "external source publisher", maximum=512)
        if self.published_at is not None:
            _validate_utc(self.published_at, "external source published_at")
        _validate_utc(self.retrieved_at, "external source retrieved_at")
        _validate_sha256(self.checksum, "external source checksum")


def _validate_fund_code(value: object) -> None:
    if type(value) is not str or not FUND_CODE_PATTERN.fullmatch(value):
        raise ValueError(f"invalid fund code: {value}")


def _validate_required_code(value: object, field_name: str) -> None:
    if type(value) is not str or not CODE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase stable code")


def _validate_version(value: object, field_name: str) -> None:
    if type(value) is not str or not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a stable version")


def _validate_optional_text(value: Optional[str], field_name: str, *, maximum: int) -> None:
    if value is None:
        return
    if type(value) is not str or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded non-empty text")


def _validate_required_text(value: object, field_name: str, *, maximum: int) -> None:
    if type(value) is not str or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field_name} must be bounded non-empty text")


def _validate_positive_id(value: object, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _validate_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or not SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _validate_utc(value: object, field_name: str) -> None:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{field_name} must use canonical UTC")


def _validate_sorted_unique_codes(values: object, field_name: str) -> None:
    if type(values) is not tuple:
        raise ValueError(f"{field_name} must be a tuple")
    for value in values:
        _validate_required_code(value, field_name)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field_name} must be unique and sorted")


def _validate_evidence_tags(values: object) -> None:
    _validate_sorted_unique_codes(values, "evidence tags")
    for value in values:
        if set(value.split("_")) & _FORBIDDEN_EVIDENCE_TAG_TOKENS:
            raise ValueError("evidence tags cannot contain personal or directional terms")


def _validate_sorted_unique_ids(values: object, field_name: str) -> None:
    if type(values) is not tuple:
        raise ValueError(f"{field_name} must be a tuple")
    for value in values:
        _validate_positive_id(value, field_name)
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field_name} must be unique and sorted")


def _validate_public_value(value: object) -> None:
    if value is None or type(value) in {str, bool, int, Decimal}:
        if type(value) is Decimal and not value.is_finite():
            raise ValueError("normalized value Decimal must be finite")
        if type(value) is str and "\x00" in value:
            raise ValueError("normalized value cannot contain NUL")
        return
    if type(value) is tuple:
        if len(value) == 2 and type(value[0]) is str:
            key, item = value
            if key.lower() in _PERSONAL_KEYS:
                raise ValueError("normalized value cannot contain personal keys")
            _validate_public_value(item)
            return
        if value and all(
            type(item) is tuple and len(item) == 2 and type(item[0]) is str for item in value
        ):
            keys = tuple(item[0] for item in value)
            if keys != tuple(sorted(set(keys))):
                raise ValueError("normalized value mapping keys must be unique and sorted")
            for key, item in value:
                if key.lower() in _PERSONAL_KEYS:
                    raise ValueError("normalized value cannot contain personal keys")
                _validate_public_value(item)
            return
        if any(type(item) is tuple and len(item) == 2 and type(item[0]) is str for item in value):
            raise ValueError("normalized value mapping must contain only sorted pairs")
        for item in value:
            _validate_public_value(item)
        return
    raise ValueError("normalized value must use deterministic public fact types")


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("normalized value Decimal must be finite")
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def canonical_fact_value(value: object) -> object:
    """Return a typed canonical JSON value without collapsing scalar types."""

    _validate_public_value(value)
    if value is None:
        return {"type": "none", "value": None}
    if type(value) is str:
        return {"type": "str", "value": value}
    if type(value) is bool:
        return {"type": "bool", "value": value}
    if type(value) is int:
        return {"type": "int", "value": value}
    if type(value) is Decimal:
        return {"type": "decimal", "value": _canonical_decimal(value)}
    if type(value) is tuple:
        return {
            "type": "tuple",
            "value": [canonical_fact_value(item) for item in value],
        }
    raise ValueError("normalized value must use deterministic public fact types")


def fact_value_from_canonical(value: object) -> object:
    """Decode one exact typed canonical fact value."""

    if type(value) is not dict or set(value) != {"type", "value"}:
        raise ValueError("canonical fact value must contain exact type and value fields")
    tag = value["type"]
    raw = value["value"]
    if type(tag) is not str:
        raise ValueError("canonical fact value type must be exact text")
    if tag == "none":
        if raw is not None:
            raise ValueError("canonical none fact value must contain null")
        decoded = None
    elif tag == "str":
        if type(raw) is not str or "\x00" in raw:
            raise ValueError("canonical string fact value is invalid")
        decoded = raw
    elif tag == "bool":
        if type(raw) is not bool:
            raise ValueError("canonical boolean fact value is invalid")
        decoded = raw
    elif tag == "int":
        if type(raw) is not int:
            raise ValueError("canonical integer fact value is invalid")
        decoded = raw
    elif tag == "decimal":
        if type(raw) is not str:
            raise ValueError("canonical Decimal fact value must be text")
        try:
            decoded = Decimal(raw)
        except InvalidOperation:
            raise ValueError("canonical Decimal fact value is invalid") from None
        if _canonical_decimal(decoded) != raw:
            raise ValueError("canonical Decimal fact value is not normalized")
    elif tag == "tuple":
        if type(raw) is not list:
            raise ValueError("canonical tuple fact value must contain an array")
        decoded = tuple(fact_value_from_canonical(item) for item in raw)
    else:
        raise ValueError("canonical fact value type is unknown")
    _validate_public_value(decoded)
    return decoded


def encode_fact_value_json(value: object) -> str:
    return json.dumps(
        canonical_fact_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def decode_fact_value_json(value: object) -> object:
    if type(value) is not str:
        raise ValueError("stored normalized value must be JSON text")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError):
        raise ValueError("stored normalized value is invalid JSON") from None
    decoded = fact_value_from_canonical(parsed)
    if encode_fact_value_json(decoded) != value:
        raise ValueError("stored normalized value JSON is not canonical")
    return decoded


def _require_exact_dataclass_state(value: object, expected_type: type, name: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} subclasses are not accepted")
    state = vars(value)
    expected_fields = {field.name for field in dataclass_fields(expected_type)}
    if type(state) is not dict or set(state) != expected_fields:
        raise ValueError(f"{name} has unexpected dataclass state")


@dataclass(frozen=True)
class MandateFact:
    fund_code: str
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    source_document_id: int
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    confidence_state: FactConfidence
    parser_version: str
    fact_fingerprint: str

    def validate(self) -> None:
        _require_exact_dataclass_state(self, MandateFact, "mandate fact")
        _validate_fund_code(self.fund_code)
        _validate_required_code(self.fact_kind, "fact kind")
        _validate_public_value(self.normalized_value)
        _validate_optional_text(self.unit, "unit", maximum=64)
        _validate_positive_id(self.source_document_id, "source document id")
        if self.page_number is not None:
            _validate_positive_id(self.page_number, "page number")
        _validate_optional_text(self.section_name, "section name", maximum=MAX_SECTION_CHARACTERS)
        _validate_required_text(
            self.source_excerpt,
            "source excerpt",
            maximum=MAX_EXCERPT_CHARACTERS,
        )
        if type(self.effective_from) not in {date, type(None)} or type(self.effective_to) not in {
            date,
            type(None),
        }:
            raise ValueError("effective dates must be exact dates")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective end date cannot precede effective start date")
        if type(self.confidence_state) is not FactConfidence:
            raise ValueError("unknown fact confidence")
        _validate_version(self.parser_version, "parser version")
        _validate_sha256(self.fact_fingerprint, "fact fingerprint")


@dataclass(frozen=True)
class EvidenceFreshness:
    section: str
    source_document_id: int
    state: FreshnessState
    observed_at: datetime
    valid_until: datetime
    critical: bool

    def validate(self) -> None:
        _require_exact_dataclass_state(self, EvidenceFreshness, "evidence freshness")
        _validate_required_code(self.section, "freshness section")
        _validate_positive_id(self.source_document_id, "source document id")
        if type(self.state) is not FreshnessState:
            raise ValueError("unknown freshness state")
        _validate_utc(self.observed_at, "observed_at")
        _validate_utc(self.valid_until, "valid_until")
        if self.valid_until <= self.observed_at:
            raise ValueError("freshness valid_until must follow observed_at")
        if type(self.critical) is not bool:
            raise ValueError("freshness critical must be boolean")


@dataclass(frozen=True)
class FundRiskClassification:
    fund_code: str
    policy_version: str
    input_fingerprint: str
    product_family: ProductFamily
    risk_bucket: RiskBucket
    portfolio_role: PortfolioRole
    evidence_status: EvidenceStatus
    evidence_tags: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    evidence_document_ids: Tuple[int, ...]
    evidence_fact_ids: Tuple[int, ...]
    freshness: Tuple[EvidenceFreshness, ...]
    classified_at: datetime
    valid_until: datetime
    capability: str = "research_only"

    def validate(self) -> None:
        _require_exact_dataclass_state(self, FundRiskClassification, "fund risk classification")
        _validate_fund_code(self.fund_code)
        _validate_version(self.policy_version, "policy version")
        _validate_sha256(self.input_fingerprint, "input fingerprint")
        for value, expected_type, field_name in (
            (self.product_family, ProductFamily, "product family"),
            (self.risk_bucket, RiskBucket, "risk bucket"),
            (self.portfolio_role, PortfolioRole, "portfolio role"),
            (self.evidence_status, EvidenceStatus, "evidence status"),
        ):
            if type(value) is not expected_type:
                raise ValueError(f"unknown {field_name}")
        _validate_evidence_tags(self.evidence_tags)
        _validate_sorted_unique_codes(self.reason_codes, "reason codes")
        _validate_sorted_unique_codes(self.missing_evidence, "missing evidence")
        _validate_sorted_unique_codes(self.conflicts, "conflicts")
        from kunjin.funds.risk.policy import (
            CLASSIFICATION_CONFLICT_CODES,
            CLASSIFICATION_FINANCIAL_CODES,
        )

        if not set(self.reason_codes).issubset(CLASSIFICATION_FINANCIAL_CODES):
            raise ValueError("reason codes must use declared financial codes")
        if not set(self.conflicts).issubset(CLASSIFICATION_CONFLICT_CODES):
            raise ValueError("conflicts must use declared conflict codes")
        _validate_sorted_unique_ids(self.evidence_document_ids, "evidence document ids")
        _validate_sorted_unique_ids(self.evidence_fact_ids, "evidence fact ids")
        if type(self.freshness) is not tuple:
            raise ValueError("freshness must be a tuple")
        freshness_keys = []
        for item in self.freshness:
            if type(item) is not EvidenceFreshness:
                raise ValueError("freshness entries must use EvidenceFreshness")
            item.validate()
            freshness_keys.append((item.section, item.source_document_id))
        if freshness_keys != sorted(set(freshness_keys)):
            raise ValueError("freshness entries must be unique and sorted")
        _validate_utc(self.classified_at, "classified_at")
        _validate_utc(self.valid_until, "valid_until")
        if self.valid_until <= self.classified_at:
            raise ValueError("classification valid_until must follow classified_at")
        if type(self.capability) is not str or self.capability != "research_only":
            raise ValueError("classification capability must be research_only")
