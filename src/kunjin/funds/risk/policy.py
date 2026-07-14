from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Tuple

CLASSIFICATION_FINANCIAL_CODES = tuple(
    sorted(
        (
            "classification_verified",
            "classification_partial",
            "classification_conflicted",
            "classification_stale",
            "classification_unclassified",
            "unsupported_product_family",
            "critical_evidence_missing",
            "critical_evidence_stale",
            "official_scope_missing",
            "index_methodology_missing",
            "holdings_evidence_missing",
            "industry_evidence_missing",
            "duration_evidence_missing",
            "credit_quality_evidence_missing",
            "leverage_evidence_missing",
            "liquidity_evidence_missing",
        )
    )
)

CLASSIFICATION_CONFLICT_CODES = tuple(
    sorted(
        (
            "name_conflicts_with_formal_scope",
            "platform_category_conflicts_with_formal_scope",
            "benchmark_conflicts_with_mandate",
            "holdings_conflict_with_mandate",
            "industry_conflict_with_broad_index",
            "nav_behavior_conflicts_with_declared_scope",
            "convertible_exposure_conflict",
            "equity_exposure_conflict",
            "duration_conflict",
            "credit_quality_conflict",
            "leverage_conflict",
            "source_version_conflict",
        )
    )
)

_EFFECTIVE_AT = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _canonical_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _require_exact(value: object, expected: object, name: str) -> None:
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"classification policy V1 {name} must be {expected!r}")


def _require_decimal(value: object, expected: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value != expected:
        raise ValueError(f"classification policy V1 {name} must be {_canonical_decimal(expected)}")


@dataclass(frozen=True)
class ClassificationPolicyV1:
    version: str = "1"
    high_quality_stock_percent_max: Decimal = Decimal("0")
    high_quality_convertible_percent_max: Decimal = Decimal("0")
    high_quality_duration_years_max: Decimal = Decimal("5")
    high_quality_weighted_average_maturity_days_max: int = 397
    high_quality_credit_floor_percent: Decimal = Decimal("80")
    high_quality_below_aa_plus_percent_max: Decimal = Decimal("0")
    high_quality_unrated_non_sovereign_percent_max: Decimal = Decimal("0")
    high_quality_gross_leverage_percent_max: Decimal = Decimal("120")
    high_quality_non_sovereign_issuer_percent_max: Decimal = Decimal("10")
    broad_index_constituents_min: int = 100
    broad_index_largest_constituent_percent_max: Decimal = Decimal("10")
    broad_index_top_ten_percent_max: Decimal = Decimal("40")
    broad_index_largest_industry_percent_max: Decimal = Decimal("35")
    broad_index_industries_min: int = 5
    sector_theme_legal_non_cash_percent_min: Decimal = Decimal("80")
    sector_theme_largest_industry_percent_min: Decimal = Decimal("50")
    active_largest_security_percent_max: Decimal = Decimal("10")
    active_top_ten_percent_max: Decimal = Decimal("50")
    active_largest_industry_percent_max: Decimal = Decimal("40")
    active_industries_min: int = 5
    legal_document_review_days: int = 365
    product_summary_review_days: int = 365
    index_methodology_review_days: int = 365
    quarterly_report_deadline_days: int = 30
    semiannual_report_deadline_days: int = 75
    annual_report_deadline_days: int = 105
    financial_codes: Tuple[str, ...] = CLASSIFICATION_FINANCIAL_CODES
    conflict_codes: Tuple[str, ...] = CLASSIFICATION_CONFLICT_CODES
    effective_at: datetime = _EFFECTIVE_AT

    def validate(self) -> None:
        if type(self) is not ClassificationPolicyV1:
            raise ValueError("classification policy V1 subclasses are not accepted")
        if set(vars(self)) != {field.name for field in dataclass_fields(type(self))}:
            raise ValueError("classification policy V1 has unexpected dataclass state")
        _require_exact(self.version, "1", "version")
        for name, expected in (
            ("high_quality_stock_percent_max", Decimal("0")),
            ("high_quality_convertible_percent_max", Decimal("0")),
            ("high_quality_duration_years_max", Decimal("5")),
            ("high_quality_credit_floor_percent", Decimal("80")),
            ("high_quality_below_aa_plus_percent_max", Decimal("0")),
            ("high_quality_unrated_non_sovereign_percent_max", Decimal("0")),
            ("high_quality_gross_leverage_percent_max", Decimal("120")),
            ("high_quality_non_sovereign_issuer_percent_max", Decimal("10")),
            ("broad_index_largest_constituent_percent_max", Decimal("10")),
            ("broad_index_top_ten_percent_max", Decimal("40")),
            ("broad_index_largest_industry_percent_max", Decimal("35")),
            ("sector_theme_legal_non_cash_percent_min", Decimal("80")),
            ("sector_theme_largest_industry_percent_min", Decimal("50")),
            ("active_largest_security_percent_max", Decimal("10")),
            ("active_top_ten_percent_max", Decimal("50")),
            ("active_largest_industry_percent_max", Decimal("40")),
        ):
            _require_decimal(getattr(self, name), expected, name)
        for name, expected in (
            ("high_quality_weighted_average_maturity_days_max", 397),
            ("broad_index_constituents_min", 100),
            ("broad_index_industries_min", 5),
            ("active_industries_min", 5),
            ("legal_document_review_days", 365),
            ("product_summary_review_days", 365),
            ("index_methodology_review_days", 365),
            ("quarterly_report_deadline_days", 30),
            ("semiannual_report_deadline_days", 75),
            ("annual_report_deadline_days", 105),
        ):
            _require_exact(getattr(self, name), expected, name)
        _require_exact(self.financial_codes, CLASSIFICATION_FINANCIAL_CODES, "financial codes")
        _require_exact(self.conflict_codes, CLASSIFICATION_CONFLICT_CODES, "conflict codes")
        if type(self.effective_at) is not datetime or self.effective_at.tzinfo is not timezone.utc:
            raise ValueError("classification policy V1 effective_at must use canonical UTC")
        if self.effective_at != _EFFECTIVE_AT:
            raise ValueError("classification policy V1 effective_at must be the fixed instant")

    def canonical_json(self) -> bytes:
        self.validate()
        payload = {}
        for field in dataclass_fields(self):
            value = getattr(self, field.name)
            if type(value) is Decimal:
                payload[field.name] = _canonical_decimal(value)
            elif type(value) is tuple:
                payload[field.name] = list(value)
            elif type(value) is datetime:
                payload[field.name] = value.astimezone(timezone.utc).isoformat()
            else:
                payload[field.name] = value
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()

    def periodic_report_deadline(self, period_end: date) -> date:
        if type(period_end) is not date:
            raise ValueError("report period end must be an exact date")
        if (period_end.month, period_end.day) in {(3, 31), (9, 30)}:
            return period_end + timedelta(days=self.quarterly_report_deadline_days)
        if (period_end.month, period_end.day) == (6, 30):
            return period_end + timedelta(days=self.semiannual_report_deadline_days)
        if (period_end.month, period_end.day) == (12, 31):
            return period_end + timedelta(days=self.annual_report_deadline_days)
        raise ValueError("unsupported report period end")


CLASSIFICATION_POLICY_V1_CHECKSUM = (
    "d3bf0765af2f230a7f332b14324c7e28780093f1d539ac22d84c665454f6ad55"
)
