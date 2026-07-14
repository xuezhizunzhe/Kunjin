from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional, Sequence, Set, Tuple

from kunjin.funds.risk.models import (
    EvidenceFreshness,
    EvidenceStatus,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    FundRiskClassification,
    MandateFact,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
    canonical_fact_value,
)
from kunjin.funds.risk.policy import CLASSIFICATION_CONFLICT_CODES, ClassificationPolicyV1

_FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
_STABLE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WARNING_ONLY_CONFLICTS = frozenset(
    {
        "name_conflicts_with_formal_scope",
        "platform_category_conflicts_with_formal_scope",
    }
)
_UNSUPPORTED_LEGAL_TYPES = frozenset(
    {
        "commodity_fund",
        "fof",
        "leveraged_fund",
        "public_reit",
        "structured_fund",
        "unsupported_qdii",
    }
)
_BOND_FAMILIES = frozenset(
    {
        ProductFamily.SHORT_BOND,
        ProductFamily.INTERMEDIATE_BOND,
        ProductFamily.ORDINARY_BOND,
        ProductFamily.LONG_BOND,
        ProductFamily.CREDIT_BOND,
        ProductFamily.CONVERTIBLE_BOND,
        ProductFamily.FIXED_INCOME_PLUS,
        ProductFamily.BOND_MIXED,
    }
)
_STRICT_BOND_CANDIDATES = frozenset(
    {
        ProductFamily.SHORT_BOND,
        ProductFamily.INTERMEDIATE_BOND,
        ProductFamily.ORDINARY_BOND,
    }
)
_MONEY_SCOPE = "money"
_BOND_SCOPE = "bond"
_MIXED_SCOPE = "mixed"
_EQUITY_SCOPE = "equity"
_INDEX_SCOPE = "index"
_QDII_SCOPE = "qdii"
_FOF_SCOPE = "fof"

EVIDENCE_STATUS_CONSERVATIVE_ORDER = (
    EvidenceStatus.UNCLASSIFIED,
    EvidenceStatus.STALE,
    EvidenceStatus.CONFLICTED,
    EvidenceStatus.PARTIAL,
    EvidenceStatus.VERIFIED,
)
RISK_BUCKET_CONSERVATIVE_ORDER = (
    RiskBucket.UNCLASSIFIED,
    RiskBucket.HYBRID_RISK,
    RiskBucket.CONCENTRATED_EQUITY,
    RiskBucket.DIVERSIFIED_EQUITY,
    RiskBucket.HIGH_QUALITY_FIXED_INCOME,
    RiskBucket.CASH_LIKE_CANDIDATE,
)
PORTFOLIO_ROLE_CONSERVATIVE_ORDER = (
    PortfolioRole.NOT_ELIGIBLE,
    PortfolioRole.SATELLITE_ONLY,
    PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE,
    PortfolioRole.CORE_ELIGIBLE,
    PortfolioRole.CASH_MANAGEMENT_CANDIDATE,
)


@dataclass(frozen=True)
class ClassificationEvidence:
    fund_code: str
    legal_facts: Tuple[MandateFact, ...]
    benchmark_facts: Tuple[MandateFact, ...]
    report_facts: Tuple[MandateFact, ...]
    existing_disclosure_facts: Tuple[MandateFact, ...]
    nav_conflicts: Tuple[str, ...]
    external_evidence_fingerprints: Tuple[Tuple[str, str], ...]
    external_source_references: Tuple[ExternalSourceReference, ...]
    nav_evidence_fingerprint: Optional[str]
    nav_observation_start: Optional[date]
    nav_observation_end: Optional[date]
    freshness: Tuple[EvidenceFreshness, ...]
    document_ids: Tuple[int, ...]
    fact_ids: Tuple[int, ...]
    parse_result_ids: Tuple[int, ...]
    parser_provenance_checksums: Tuple[str, ...]

    def validate(self) -> None:
        if type(self) is not ClassificationEvidence:
            raise ValueError("classification evidence subclasses are not accepted")
        if set(vars(self)) != {field.name for field in dataclass_fields(type(self))}:
            raise ValueError("classification evidence has unexpected dataclass state")
        if type(self.fund_code) is not str or not _FUND_CODE_PATTERN.fullmatch(self.fund_code):
            raise ValueError("classification evidence fund code must contain six digits")

        d1_facts: List[MandateFact] = []
        for values, label in (
            (self.legal_facts, "legal facts"),
            (self.benchmark_facts, "benchmark facts"),
            (self.report_facts, "report facts"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{label} must be an immutable tuple")
            for item in values:
                if type(item) is not MandateFact:
                    raise ValueError(f"{label} must use exact MandateFact records")
                item.validate()
                if item.fund_code != self.fund_code:
                    raise ValueError("classification facts must match the evidence fund code")
                d1_facts.append(item)

        if type(self.existing_disclosure_facts) is not tuple:
            raise ValueError("existing disclosure facts must be an immutable tuple")
        for item in self.existing_disclosure_facts:
            if type(item) is not MandateFact:
                raise ValueError("existing disclosure facts must use exact MandateFact records")
            item.validate()
            if item.fund_code != self.fund_code:
                raise ValueError("classification facts must match the evidence fund code")

        if type(self.nav_conflicts) is not tuple or self.nav_conflicts != tuple(
            sorted(set(self.nav_conflicts))
        ):
            raise ValueError("NAV conflicts must be unique and sorted")
        if not set(self.nav_conflicts).issubset(CLASSIFICATION_CONFLICT_CODES):
            raise ValueError("NAV conflicts must use declared classification conflict codes")

        if type(self.external_evidence_fingerprints) is not tuple:
            raise ValueError("external evidence fingerprints must be an immutable tuple")
        external_sections = []
        for binding in self.external_evidence_fingerprints:
            if type(binding) is not tuple or len(binding) != 2:
                raise ValueError("external evidence fingerprint bindings must be exact pairs")
            section, fingerprint = binding
            if type(section) is not str or not _STABLE_CODE_PATTERN.fullmatch(section):
                raise ValueError("external evidence section must be a stable code")
            if type(fingerprint) is not str or not _SHA256_PATTERN.fullmatch(fingerprint):
                raise ValueError("external evidence fingerprint must be lowercase SHA-256")
            external_sections.append(section)
        if self.external_evidence_fingerprints != tuple(
            sorted(self.external_evidence_fingerprints)
        ) or external_sections != sorted(set(external_sections)):
            raise ValueError("external evidence fingerprints must be sorted with unique sections")
        if self.existing_disclosure_facts and not self.external_evidence_fingerprints:
            raise ValueError("existing disclosure facts require an external evidence fingerprint")
        if type(self.external_source_references) is not tuple:
            raise ValueError("external source references must be an immutable tuple")
        reference_keys = []
        for reference in self.external_source_references:
            if type(reference) is not ExternalSourceReference:
                raise ValueError(
                    "external source references must use exact ExternalSourceReference records"
                )
            reference.validate()
            if reference.fund_code != self.fund_code:
                raise ValueError("external source reference must match the evidence fund")
            if reference.section not in external_sections:
                raise ValueError("external source reference section lacks an evidence fingerprint")
            reference_keys.append(
                (
                    reference.source_namespace,
                    reference.source_document_id,
                    reference.section,
                )
            )
        if reference_keys != sorted(set(reference_keys)):
            raise ValueError("external source references must be unique and sorted")
        reference_bindings = {
            (item.source_document_id, item.section) for item in self.external_source_references
        }
        for fact in self.existing_disclosure_facts:
            if (fact.source_document_id, fact.section_name) not in reference_bindings:
                raise ValueError("existing disclosure fact lacks an external source reference")

        nav_values = (
            self.nav_evidence_fingerprint,
            self.nav_observation_start,
            self.nav_observation_end,
        )
        if any(value is None for value in nav_values) and not all(
            value is None for value in nav_values
        ):
            raise ValueError("NAV evidence fingerprint and observation window are all-or-none")
        if self.nav_evidence_fingerprint is not None:
            if type(self.nav_evidence_fingerprint) is not str or not _SHA256_PATTERN.fullmatch(
                self.nav_evidence_fingerprint
            ):
                raise ValueError("NAV evidence fingerprint must be lowercase SHA-256")
            if (
                type(self.nav_observation_start) is not date
                or type(self.nav_observation_end) is not date
            ):
                raise ValueError("NAV observation window must use exact dates")
            if self.nav_observation_start > self.nav_observation_end:
                raise ValueError("NAV observation start cannot follow its end")

        if type(self.freshness) is not tuple:
            raise ValueError("freshness must be an immutable tuple")
        freshness_keys = []
        for item in self.freshness:
            if type(item) is not EvidenceFreshness:
                raise ValueError("freshness must use exact EvidenceFreshness records")
            item.validate()
            freshness_keys.append((item.section, item.source_document_id))
        if freshness_keys != sorted(set(freshness_keys)):
            raise ValueError("freshness must be unique and sorted")

        _validate_ids(self.document_ids, "document ids")
        _validate_ids(self.fact_ids, "fact ids")
        _validate_ids(self.parse_result_ids, "parse result ids")
        _validate_sha256s(
            self.parser_provenance_checksums,
            "parser provenance checksums",
        )
        used_document_ids = {item.source_document_id for item in d1_facts}
        used_document_ids.update(item.source_document_id for item in self.freshness)
        if not used_document_ids.issubset(self.document_ids):
            raise ValueError("document ids must bind every D1 fact and freshness record")
        if len(self.fact_ids) != len(d1_facts):
            raise ValueError("fact ids must bind every D1 normalized fact")


def _validate_ids(values: object, label: str) -> None:
    if type(values) is not tuple:
        raise ValueError(f"{label} must be an immutable tuple")
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError(f"{label} must contain positive integers")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and sorted")


def _validate_sha256s(values: object, label: str) -> None:
    if type(values) is not tuple:
        raise ValueError(f"{label} must be an immutable tuple")
    if any(type(value) is not str or not _SHA256_PATTERN.fullmatch(value) for value in values):
        raise ValueError(f"{label} must contain lowercase SHA-256 digests")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be unique and sorted")


def _canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if type(value) is Decimal:
        normalized = value.normalize()
        return format(normalized, "f") if normalized != 0 else "0"
    if type(value) is datetime:
        return value.astimezone(timezone.utc).isoformat()
    if type(value) is date:
        return value.isoformat()
    if type(value) is tuple:
        return [_canonical_value(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    raise ValueError("classification evidence contains a non-canonical value")


def _fact_payload(value: MandateFact) -> object:
    return {
        field.name: (
            canonical_fact_value(getattr(value, field.name))
            if field.name == "normalized_value"
            else _canonical_value(getattr(value, field.name))
        )
        for field in dataclass_fields(MandateFact)
    }


def _freshness_payload(value: EvidenceFreshness) -> object:
    return {
        "critical": value.critical,
        "observed_at": value.observed_at.isoformat(),
        "section": value.section,
        "source_document_id": value.source_document_id,
        "state": value.state.value,
        "valid_until": value.valid_until.isoformat(),
    }


def classification_input_manifest_v1(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> Dict[str, object]:
    """Return the exact legacy manifest used by Schema V11 classifications."""

    evidence.validate()
    policy.validate()
    if type(classified_at) is not datetime or classified_at.tzinfo is not timezone.utc:
        raise ValueError("classified_at must use canonical UTC")

    def sorted_fact_payloads(values: Tuple[MandateFact, ...]) -> List[object]:
        payloads = [_fact_payload(item) for item in values]
        return sorted(
            payloads,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    payload = {
        "benchmark_facts": sorted_fact_payloads(evidence.benchmark_facts),
        "classified_at": classified_at.isoformat(),
        "document_ids": list(evidence.document_ids),
        "external_evidence_fingerprints": [
            [section, fingerprint]
            for section, fingerprint in evidence.external_evidence_fingerprints
        ],
        "external_source_references": [
            _external_source_payload(item) for item in evidence.external_source_references
        ],
        "existing_disclosure_facts": sorted_fact_payloads(evidence.existing_disclosure_facts),
        "fact_ids": list(evidence.fact_ids),
        "freshness": [_freshness_payload(item) for item in evidence.freshness],
        "fund_code": evidence.fund_code,
        "legal_facts": sorted_fact_payloads(evidence.legal_facts),
        "nav_conflicts": list(evidence.nav_conflicts),
        "nav_evidence_fingerprint": evidence.nav_evidence_fingerprint,
        "nav_observation_end": (
            evidence.nav_observation_end.isoformat()
            if evidence.nav_observation_end is not None
            else None
        ),
        "nav_observation_start": (
            evidence.nav_observation_start.isoformat()
            if evidence.nav_observation_start is not None
            else None
        ),
        "policy_checksum": policy.checksum(),
        "policy_version": policy.version,
        "report_facts": sorted_fact_payloads(evidence.report_facts),
    }
    return payload


def classification_input_manifest_v2(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> Dict[str, object]:
    """Return the current manifest with explicit parse-result provenance bindings."""

    payload = classification_input_manifest_v1(evidence, policy, classified_at)
    payload.update(
        {
            "manifest_version": 2,
            "parse_result_ids": list(evidence.parse_result_ids),
            "parser_provenance_checksums": list(evidence.parser_provenance_checksums),
        }
    )
    return payload


def classification_input_manifest(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> Dict[str, object]:
    """Return the canonical current inputs bound by a classification fingerprint."""

    return classification_input_manifest_v2(evidence, policy, classified_at)


def _external_source_payload(value: ExternalSourceReference) -> object:
    value.validate()
    return {
        "checksum": value.checksum,
        "document_kind": value.document_kind,
        "fund_code": value.fund_code,
        "published_at": (None if value.published_at is None else value.published_at.isoformat()),
        "publisher": value.publisher,
        "retrieved_at": value.retrieved_at.isoformat(),
        "section": value.section,
        "source_document_id": value.source_document_id,
        "source_name": value.source_name,
        "source_namespace": value.source_namespace,
        "source_tier": value.source_tier,
        "title": value.title,
        "url": value.url,
    }


def _input_fingerprint(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> str:
    payload = classification_input_manifest(evidence, policy, classified_at)
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


_ASSET_ALLOCATION_UNITS = frozenset(
    {"percent_of_total_assets", "percent_of_net_assets"}
)
_CONCENTRATION_UNITS = frozenset(
    {"percent_of_total_assets", "percent_of_net_assets", "percent_of_fund_assets"}
)
_CREDIT_DISTRIBUTION_UNITS = frozenset(
    {
        "percent_of_net_assets",
        "percent_of_bond_assets",
        "percent_of_fixed_income_assets",
    }
)
_CURRENT_REPORT_FACT_UNITS = {
    "current_stock_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_bond_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_cash_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_hong_kong_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_largest_security_weight_percent": _CONCENTRATION_UNITS,
    "current_top_ten_holdings_weight_percent": _CONCENTRATION_UNITS,
    "current_largest_industry_name": frozenset({None}),
    "current_largest_industry_weight_percent": _CONCENTRATION_UNITS,
    "current_industry_count": frozenset({None}),
    "holdings_evidence_complete": frozenset({None}),
    "current_effective_duration": frozenset({"years"}),
    "current_weighted_average_maturity_days": frozenset({"days"}),
    "current_convertible_bond_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_exchangeable_bond_asset_allocation_percent": _ASSET_ALLOCATION_UNITS,
    "current_high_quality_fixed_income_percent": _CREDIT_DISTRIBUTION_UNITS,
    "current_below_aa_plus_exposure_percent": _CREDIT_DISTRIBUTION_UNITS,
    "current_unrated_non_sovereign_exposure_percent": _CREDIT_DISTRIBUTION_UNITS,
    "current_gross_leverage_percent": frozenset({"percent_of_net_assets"}),
    "current_largest_non_sovereign_issuer_percent": frozenset(
        {"percent_of_net_assets", "percent_of_fund_assets"}
    ),
}


def _current_report_unit_is_allowed(item: MandateFact) -> bool:
    allowed_units = _CURRENT_REPORT_FACT_UNITS.get(item.fact_kind)
    if allowed_units is None:
        return not item.fact_kind.startswith("current_")
    return item.unit in allowed_units


class _Facts:
    def __init__(self, groups: Sequence[Tuple[MandateFact, ...]]) -> None:
        self._values: Dict[str, List[object]] = {}
        bindings: Dict[str, List[Tuple[object, Optional[str]]]] = {}
        self.conflicts: Set[str] = set()
        self._conflicted_kinds: Set[str] = set()
        for group in groups:
            for item in group:
                if item.confidence_state is FactConfidence.AMBIGUOUS:
                    continue
                if not _current_report_unit_is_allowed(item):
                    self.conflicts.add("source_version_conflict")
                    self._conflicted_kinds.add(item.fact_kind)
                    continue
                self._values.setdefault(item.fact_kind, []).append(item.normalized_value)
                bindings.setdefault(item.fact_kind, []).append(
                    (item.normalized_value, item.unit)
                )
        for kind, values in bindings.items():
            canonical = {
                json.dumps(
                    {
                        "unit": unit,
                        "value": canonical_fact_value(value),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
                for value, unit in values
            }
            if len(canonical) > 1:
                self.conflicts.add("source_version_conflict")
                self._conflicted_kinds.add(kind)

    def get(self, *kinds: str) -> Optional[object]:
        for kind in kinds:
            if kind in self._conflicted_kinds:
                continue
            values = self._values.get(kind, ())
            if values:
                return values[-1]
        return None


def _as_decimal(value: object) -> Optional[Decimal]:
    if type(value) is Decimal and value.is_finite():
        return value
    if type(value) is int:
        return Decimal(value)
    return None


def _as_int(value: object) -> Optional[int]:
    if type(value) is int:
        return value
    return None


def _as_bool(value: object) -> Optional[bool]:
    return value if type(value) is bool else None


def _declared_family(legal: _Facts) -> Optional[ProductFamily]:
    value = legal.get("legal_product_family", "product_family")
    if type(value) is str:
        try:
            family = ProductFamily(value)
        except ValueError:
            family = None
        if family not in {None, ProductFamily.UNSUPPORTED, ProductFamily.UNCLASSIFIED}:
            return family

    legal_type = legal.get("legal_product_type")
    generic = {
        "money_market_fund": ProductFamily.MONEY_MARKET,
        "bond_fund": ProductFamily.ORDINARY_BOND,
        "equity_fund": ProductFamily.ACTIVE_EQUITY,
        "mixed_fund": ProductFamily.EQUITY_MIXED,
        "index_enhanced_fund": ProductFamily.INDEX_ENHANCED,
        "qdii_fund": None,
    }
    return generic.get(legal_type) if type(legal_type) is str else None


def _is_unsupported(legal: _Facts) -> bool:
    value = legal.get("legal_product_type", "legal_product_family", "product_family")
    return type(value) is str and value in _UNSUPPORTED_LEGAL_TYPES


def _formal_scope_signals(legal: _Facts) -> Set[str]:
    family = _declared_family(legal)
    if family is ProductFamily.MONEY_MARKET:
        return {_MONEY_SCOPE}
    if family in {
        ProductFamily.SHORT_BOND,
        ProductFamily.INTERMEDIATE_BOND,
        ProductFamily.ORDINARY_BOND,
        ProductFamily.LONG_BOND,
        ProductFamily.CREDIT_BOND,
        ProductFamily.CONVERTIBLE_BOND,
    }:
        return {_BOND_SCOPE}
    if family in {ProductFamily.FIXED_INCOME_PLUS, ProductFamily.BOND_MIXED}:
        return {_BOND_SCOPE, _MIXED_SCOPE}
    if family in {ProductFamily.BROAD_INDEX, ProductFamily.INDEX_ENHANCED}:
        return {_EQUITY_SCOPE, _INDEX_SCOPE}
    if family is ProductFamily.SECTOR_THEME:
        return {_EQUITY_SCOPE, _INDEX_SCOPE, _MIXED_SCOPE}
    if family is ProductFamily.ACTIVE_EQUITY:
        return {_EQUITY_SCOPE}
    if family is ProductFamily.EQUITY_MIXED:
        return {_EQUITY_SCOPE, _MIXED_SCOPE}
    if family in {ProductFamily.QDII_BROAD_EQUITY, ProductFamily.QDII_SECTOR_THEME}:
        return {_EQUITY_SCOPE, _INDEX_SCOPE, _QDII_SCOPE}

    legal_type = legal.get("legal_product_type")
    if legal_type == "fof":
        return {_FOF_SCOPE}
    if legal_type in {"qdii_fund", "unsupported_qdii"}:
        return {_QDII_SCOPE}
    return set()


def _external_scope_signals(value: object) -> Set[str]:
    if type(value) is not str:
        return set()
    normalized = unicodedata.normalize("NFKC", value).casefold()
    signals: Set[str] = set()
    for signal, expressions in (
        (_MONEY_SCOPE, (r"货币", r"\bmoney\s+market\b")),
        (_BOND_SCOPE, (r"纯债", r"债券", r"\bbonds?\b", r"\bfixed\s+income\b")),
        (_MIXED_SCOPE, (r"混合", r"\bmixed\b", r"\bhybrid\b")),
        (_EQUITY_SCOPE, (r"股票", r"\bequity\b", r"\bstocks?\b")),
        (_INDEX_SCOPE, (r"指数", r"联接", r"\bindex\b", r"etf", r"\bfeeder\b")),
        (_QDII_SCOPE, (r"qdii",)),
        (_FOF_SCOPE, (r"基金中基金", r"fof", r"\bfund\s+of\s+funds\b")),
    ):
        if any(
            re.search(expression, normalized, flags=re.IGNORECASE) for expression in expressions
        ):
            signals.add(signal)
    return signals


def _identity_scope_conflicts(legal: _Facts, disclosures: _Facts) -> Set[str]:
    formal_signals = _formal_scope_signals(legal)
    if not formal_signals:
        return set()

    conflicts: Set[str] = set()
    name_signals = _external_scope_signals(disclosures.get("fund_name"))
    if name_signals and name_signals.isdisjoint(formal_signals):
        conflicts.add("name_conflicts_with_formal_scope")
    platform_signals = _external_scope_signals(disclosures.get("platform_category"))
    if platform_signals and platform_signals.isdisjoint(formal_signals):
        conflicts.add("platform_category_conflicts_with_formal_scope")
    return conflicts


def _strict_bond_result(
    family: ProductFamily,
    legal: _Facts,
    report: _Facts,
    policy: ClassificationPolicyV1,
) -> Tuple[ProductFamily, RiskBucket, PortfolioRole, Set[str], Set[str]]:
    missing: Set[str] = set()
    reasons: Set[str] = set()

    gates = (
        (
            "stock_mandate_evidence_missing",
            _as_decimal(legal.get("stock_exposure_max_percent")),
            policy.high_quality_stock_percent_max,
            "max",
        ),
        (
            "stock_observation_evidence_missing",
            _as_decimal(report.get("current_stock_asset_allocation_percent")),
            policy.high_quality_stock_percent_max,
            "max",
        ),
        (
            "convertible_exposure_evidence_missing",
            _as_decimal(legal.get("convertible_bond_exposure_max_percent")),
            policy.high_quality_convertible_percent_max,
            "max",
        ),
        (
            "convertible_observation_evidence_missing",
            _as_decimal(report.get("current_convertible_bond_asset_allocation_percent")),
            policy.high_quality_convertible_percent_max,
            "max",
        ),
        (
            "exchangeable_exposure_evidence_missing",
            _as_decimal(legal.get("exchangeable_bond_exposure_max_percent")),
            policy.high_quality_convertible_percent_max,
            "max",
        ),
        (
            "exchangeable_observation_evidence_missing",
            _as_decimal(report.get("current_exchangeable_bond_asset_allocation_percent")),
            policy.high_quality_convertible_percent_max,
            "max",
        ),
        (
            "credit_quality_mandate_evidence_missing",
            _as_decimal(legal.get("high_quality_fixed_income_min_percent")),
            policy.high_quality_credit_floor_percent,
            "min",
        ),
        (
            "credit_quality_observation_evidence_missing",
            _as_decimal(report.get("current_high_quality_fixed_income_percent")),
            policy.high_quality_credit_floor_percent,
            "min",
        ),
        (
            "below_aa_plus_mandate_evidence_missing",
            _as_decimal(legal.get("below_aa_plus_exposure_max_percent")),
            policy.high_quality_below_aa_plus_percent_max,
            "max",
        ),
        (
            "below_aa_plus_observation_evidence_missing",
            _as_decimal(report.get("current_below_aa_plus_exposure_percent")),
            policy.high_quality_below_aa_plus_percent_max,
            "max",
        ),
        (
            "unrated_non_sovereign_mandate_evidence_missing",
            _as_decimal(legal.get("unrated_non_sovereign_exposure_max_percent")),
            policy.high_quality_unrated_non_sovereign_percent_max,
            "max",
        ),
        (
            "unrated_non_sovereign_observation_evidence_missing",
            _as_decimal(report.get("current_unrated_non_sovereign_exposure_percent")),
            policy.high_quality_unrated_non_sovereign_percent_max,
            "max",
        ),
        (
            "leverage_mandate_evidence_missing",
            _as_decimal(legal.get("gross_leverage_max_percent")),
            policy.high_quality_gross_leverage_percent_max,
            "max",
        ),
        (
            "leverage_observation_evidence_missing",
            _as_decimal(report.get("current_gross_leverage_percent")),
            policy.high_quality_gross_leverage_percent_max,
            "max",
        ),
        (
            "issuer_concentration_mandate_evidence_missing",
            _as_decimal(legal.get("single_non_sovereign_issuer_max_percent")),
            policy.high_quality_non_sovereign_issuer_percent_max,
            "max",
        ),
        (
            "issuer_concentration_evidence_missing",
            _as_decimal(report.get("current_largest_non_sovereign_issuer_percent")),
            policy.high_quality_non_sovereign_issuer_percent_max,
            "max",
        ),
    )

    failures: Set[str] = set()
    for missing_code, observed, threshold, direction in gates:
        if observed is None:
            missing.add(missing_code)
        elif (direction == "max" and observed > threshold) or (
            direction == "min" and observed < threshold
        ):
            failures.add(missing_code.removesuffix("_evidence_missing"))

    mandated_duration = _as_decimal(legal.get("effective_duration_max"))
    mandated_maturity = _as_decimal(legal.get("weighted_average_maturity_max"))
    observed_duration = _as_decimal(report.get("current_effective_duration"))
    observed_maturity = _as_decimal(report.get("current_weighted_average_maturity_days"))
    if mandated_duration is None and mandated_maturity is None:
        missing.add("duration_evidence_missing")
    elif not (
        mandated_duration is not None
        and mandated_duration <= policy.high_quality_duration_years_max
    ) and not (
        mandated_maturity is not None
        and mandated_maturity <= Decimal(policy.high_quality_weighted_average_maturity_days_max)
    ):
        failures.add("duration")
    if observed_duration is None and observed_maturity is None:
        missing.add("duration_observation_evidence_missing")
    elif not (
        observed_duration is not None
        and observed_duration <= policy.high_quality_duration_years_max
    ) and not (
        observed_maturity is not None
        and observed_maturity <= Decimal(policy.high_quality_weighted_average_maturity_days_max)
    ):
        failures.add("duration")

    derivatives = legal.get("derivatives_use")
    derivative_max = _as_decimal(legal.get("derivative_exposure_max_percent"))
    if derivatives is None and derivative_max is None:
        missing.add("derivatives_evidence_missing")
    elif derivatives not in {"absent", "hedging_only"} and derivative_max != Decimal("0"):
        failures.add("derivatives")

    overseas = _as_decimal(legal.get("overseas_exposure_max_percent"))
    hong_kong = _as_decimal(legal.get("hong_kong_exposure_max_percent"))
    if overseas is None or hong_kong is None:
        missing.add("foreign_exposure_evidence_missing")
    elif overseas > 0 or hong_kong > 0:
        failures.add("foreign_exposure")

    if missing:
        reasons.add("critical_evidence_missing")
        if "duration_evidence_missing" in missing:
            reasons.add("duration_evidence_missing")
        if missing & {
            "credit_quality_mandate_evidence_missing",
            "credit_quality_observation_evidence_missing",
            "below_aa_plus_mandate_evidence_missing",
            "below_aa_plus_observation_evidence_missing",
            "unrated_non_sovereign_mandate_evidence_missing",
            "unrated_non_sovereign_observation_evidence_missing",
            "issuer_concentration_mandate_evidence_missing",
            "issuer_concentration_evidence_missing",
        }:
            reasons.add("credit_quality_evidence_missing")
        if missing & {
            "leverage_mandate_evidence_missing",
            "leverage_observation_evidence_missing",
        }:
            reasons.add("leverage_evidence_missing")
        return family, RiskBucket.UNCLASSIFIED, PortfolioRole.NOT_ELIGIBLE, missing, reasons

    if failures:
        if "stock_mandate" in failures or "stock_observation" in failures:
            family = ProductFamily.FIXED_INCOME_PLUS
        elif "convertible_exposure" in failures or "convertible_observation" in failures:
            family = ProductFamily.CONVERTIBLE_BOND
        elif "exchangeable_exposure" in failures or "exchangeable_observation" in failures:
            family = ProductFamily.CONVERTIBLE_BOND
        elif "duration" in failures:
            family = ProductFamily.LONG_BOND
        elif failures & {
            "credit_quality_mandate",
            "credit_quality_observation",
            "below_aa_plus_mandate",
            "below_aa_plus_observation",
            "unrated_non_sovereign_mandate",
            "unrated_non_sovereign_observation",
            "issuer_concentration_mandate",
            "issuer_concentration",
        }:
            family = ProductFamily.CREDIT_BOND
        return family, RiskBucket.HYBRID_RISK, PortfolioRole.NOT_ELIGIBLE, missing, reasons

    return (
        family,
        RiskBucket.HIGH_QUALITY_FIXED_INCOME,
        PortfolioRole.NOT_ELIGIBLE,
        missing,
        reasons,
    )


def _money_market_result(
    legal: _Facts,
) -> Tuple[ProductFamily, RiskBucket, PortfolioRole, Set[str], Set[str]]:
    missing: Set[str] = set()
    reasons: Set[str] = set()
    failures = False
    for kind, missing_code in (
        ("stock_exposure_max_percent", "stock_mandate_evidence_missing"),
        (
            "convertible_bond_exposure_max_percent",
            "convertible_exposure_evidence_missing",
        ),
        (
            "exchangeable_bond_exposure_max_percent",
            "exchangeable_exposure_evidence_missing",
        ),
    ):
        value = _as_decimal(legal.get(kind))
        if value is None:
            missing.add(missing_code)
        elif value > 0:
            failures = True

    derivatives = legal.get("derivatives_use")
    derivative_max = _as_decimal(legal.get("derivative_exposure_max_percent"))
    if derivatives is None and derivative_max is None:
        missing.add("derivatives_evidence_missing")
    elif derivatives != "absent" and derivative_max != Decimal("0"):
        failures = True

    redemption = legal.get("redemption_restriction")
    lockup = legal.get("lockup_restriction")
    if redemption is None:
        missing.add("redemption_evidence_missing")
    elif redemption != "daily_open":
        failures = True
    if lockup is None:
        missing.add("lockup_evidence_missing")
    elif lockup != "absent":
        failures = True

    if missing:
        reasons.add("critical_evidence_missing")
        if missing & {"redemption_evidence_missing", "lockup_evidence_missing"}:
            reasons.add("liquidity_evidence_missing")
        return (
            ProductFamily.MONEY_MARKET,
            RiskBucket.UNCLASSIFIED,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )
    if failures:
        return (
            ProductFamily.MONEY_MARKET,
            RiskBucket.HYBRID_RISK,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )
    return (
        ProductFamily.MONEY_MARKET,
        RiskBucket.CASH_LIKE_CANDIDATE,
        PortfolioRole.CASH_MANAGEMENT_CANDIDATE,
        missing,
        reasons,
    )


def _theme_identity(
    legal: _Facts,
    benchmark: _Facts,
    report: _Facts,
    policy: ClassificationPolicyV1,
) -> bool:
    if legal.get("sector_theme_mandate") in {"present", True}:
        return True
    if benchmark.get("sector_theme_mandate", "index_scope") in {"present", "sector_theme"}:
        return True
    theme_floor = _as_decimal(legal.get("theme_exposure_min_percent"))
    if theme_floor is not None and theme_floor >= policy.sector_theme_legal_non_cash_percent_min:
        return True
    current_industry = _as_decimal(report.get("current_largest_industry_weight_percent"))
    return (
        _as_bool(report.get("industry_evidence_complete")) is True
        and _as_bool(report.get("industry_concentration_consistent_with_mandate")) is True
        and current_industry is not None
        and current_industry >= policy.sector_theme_largest_industry_percent_min
    )


def _formal_theme_identity(
    legal: _Facts,
    benchmark: _Facts,
    policy: ClassificationPolicyV1,
) -> bool:
    if legal.get("sector_theme_mandate") in {"present", True}:
        return True
    if benchmark.get("sector_theme_mandate", "index_scope") in {
        "present",
        "sector_theme",
        "single_industry",
        "narrow_factor",
    }:
        return True
    theme_floor = _as_decimal(legal.get("theme_exposure_min_percent"))
    return theme_floor is not None and theme_floor >= policy.sector_theme_legal_non_cash_percent_min


def _formal_broad_index_identity(benchmark: _Facts) -> bool:
    tracked_index = benchmark.get("tracked_index_name")
    return (
        type(tracked_index) is str
        and bool(tracked_index.strip())
        and _as_bool(benchmark.get("index_methodology_present")) is True
        and benchmark.get("sector_theme_mandate") == "absent"
    )


def _observed_risk_conflicts(legal: _Facts, report: _Facts) -> Set[str]:
    conflicts: Set[str] = set()
    mandated_stock = _as_decimal(legal.get("stock_exposure_max_percent"))
    observed_stock = _as_decimal(report.get("current_stock_asset_allocation_percent"))
    if (
        mandated_stock is not None
        and observed_stock is not None
        and observed_stock > mandated_stock
    ):
        conflicts.add("equity_exposure_conflict")

    for legal_kind, observed_kind in (
        (
            "convertible_bond_exposure_max_percent",
            "current_convertible_bond_asset_allocation_percent",
        ),
        (
            "exchangeable_bond_exposure_max_percent",
            "current_exchangeable_bond_asset_allocation_percent",
        ),
    ):
        mandated = _as_decimal(legal.get(legal_kind))
        observed = _as_decimal(report.get(observed_kind))
        if mandated is not None and observed is not None and observed > mandated:
            conflicts.add("convertible_exposure_conflict")

    mandated_duration = _as_decimal(legal.get("effective_duration_max"))
    observed_duration = _as_decimal(report.get("current_effective_duration"))
    if (
        mandated_duration is not None
        and observed_duration is not None
        and observed_duration > mandated_duration
    ):
        conflicts.add("duration_conflict")

    for legal_kind, observed_kind, direction in (
        (
            "high_quality_fixed_income_min_percent",
            "current_high_quality_fixed_income_percent",
            "min",
        ),
        (
            "below_aa_plus_exposure_max_percent",
            "current_below_aa_plus_exposure_percent",
            "max",
        ),
        (
            "unrated_non_sovereign_exposure_max_percent",
            "current_unrated_non_sovereign_exposure_percent",
            "max",
        ),
    ):
        mandated = _as_decimal(legal.get(legal_kind))
        observed = _as_decimal(report.get(observed_kind))
        if mandated is None or observed is None:
            continue
        if (direction == "min" and observed < mandated) or (
            direction == "max" and observed > mandated
        ):
            conflicts.add("credit_quality_conflict")

    mandated_leverage = _as_decimal(legal.get("gross_leverage_max_percent"))
    observed_leverage = _as_decimal(report.get("current_gross_leverage_percent"))
    if (
        mandated_leverage is not None
        and observed_leverage is not None
        and observed_leverage > mandated_leverage
    ):
        conflicts.add("leverage_conflict")
    return conflicts


def _evidence_tags(
    family: ProductFamily,
    legal: _Facts,
    report: _Facts,
) -> Tuple[str, ...]:
    tags: Set[str] = set()
    if family in {ProductFamily.QDII_BROAD_EQUITY, ProductFamily.QDII_SECTOR_THEME}:
        tags.add("foreign_currency")

    hong_kong_values = (
        _as_decimal(legal.get("hong_kong_exposure_min_percent")),
        _as_decimal(legal.get("hong_kong_exposure_max_percent")),
        _as_decimal(report.get("current_hong_kong_asset_allocation_percent")),
    )
    if any(value is not None and value > 0 for value in hong_kong_values):
        tags.add("hong_kong_equity")

    if family is ProductFamily.CREDIT_BOND or any(
        value is not None and value > 0
        for value in (
            _as_decimal(report.get("current_below_aa_plus_exposure_percent")),
            _as_decimal(report.get("current_unrated_non_sovereign_exposure_percent")),
        )
    ):
        tags.add("credit_exposure")

    if family is ProductFamily.CONVERTIBLE_BOND or any(
        value is not None and value > 0
        for value in (
            _as_decimal(report.get("current_convertible_bond_asset_allocation_percent")),
            _as_decimal(report.get("current_exchangeable_bond_asset_allocation_percent")),
        )
    ):
        tags.add("convertible_exposure")

    if legal.get("interest_rate_bond_mandate") in {"present", True}:
        tags.add("interest_rate_bond")
    if legal.get("policy_bank_bond_mandate") in {"present", True}:
        tags.add("policy_bank_bond")
    return tuple(sorted(tags))


def _active_equity_result(
    family: ProductFamily,
    report: _Facts,
    policy: ClassificationPolicyV1,
) -> Tuple[ProductFamily, RiskBucket, PortfolioRole, Set[str], Set[str]]:
    missing: Set[str] = set()
    reasons: Set[str] = set()
    stock = _as_decimal(report.get("current_stock_asset_allocation_percent"))
    if stock is None:
        missing.add("asset_allocation_evidence_missing")

    failures = False
    for missing_code, observed, maximum in (
        (
            "largest_security_evidence_missing",
            _as_decimal(report.get("current_largest_security_weight_percent")),
            policy.active_largest_security_percent_max,
        ),
        (
            "top_ten_holdings_evidence_missing",
            _as_decimal(report.get("current_top_ten_holdings_weight_percent")),
            policy.active_top_ten_percent_max,
        ),
        (
            "industry_concentration_evidence_missing",
            _as_decimal(report.get("current_largest_industry_weight_percent")),
            policy.active_largest_industry_percent_max,
        ),
    ):
        if observed is None:
            missing.add(missing_code)
        elif observed > maximum:
            failures = True

    industry_count = _as_int(report.get("current_industry_count", "industry_count_min"))
    if industry_count is None:
        missing.add("industry_count_evidence_missing")
    elif industry_count < policy.active_industries_min:
        failures = True
    if _as_bool(report.get("holdings_evidence_complete")) is not True:
        missing.add("holdings_evidence_missing")

    if missing:
        reasons.add("critical_evidence_missing")
        if "holdings_evidence_missing" in missing:
            reasons.add("holdings_evidence_missing")
        if missing & {
            "industry_concentration_evidence_missing",
            "industry_count_evidence_missing",
        }:
            reasons.add("industry_evidence_missing")
        return (
            family,
            RiskBucket.CONCENTRATED_EQUITY,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )
    if failures:
        return (
            family,
            RiskBucket.CONCENTRATED_EQUITY,
            PortfolioRole.SATELLITE_ONLY,
            missing,
            reasons,
        )
    return (
        family,
        RiskBucket.DIVERSIFIED_EQUITY,
        PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE,
        missing,
        reasons,
    )


def _broad_index_result(
    family: ProductFamily,
    benchmark: _Facts,
    report: _Facts,
    policy: ClassificationPolicyV1,
) -> Tuple[ProductFamily, RiskBucket, PortfolioRole, Set[str], Set[str]]:
    missing: Set[str] = set()
    reasons: Set[str] = set()
    tracked_index = benchmark.get("tracked_index_name")
    methodology = _as_bool(benchmark.get("index_methodology_present"))
    constituents = _as_int(benchmark.get("constituent_count"))
    if type(tracked_index) is not str or not tracked_index.strip():
        missing.add("tracked_index_evidence_missing")
    if methodology is not True:
        missing.add("index_methodology_evidence_missing")
        reasons.add("index_methodology_missing")
    if constituents is None:
        missing.add("constituent_count_evidence_missing")
    if missing:
        reasons.add("critical_evidence_missing")
        return (
            ProductFamily.UNCLASSIFIED,
            RiskBucket.UNCLASSIFIED,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )
    if constituents < policy.broad_index_constituents_min:
        return (
            ProductFamily.UNCLASSIFIED,
            RiskBucket.CONCENTRATED_EQUITY,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )

    concentration_gates = (
        (
            "largest_constituent_evidence_missing",
            _as_decimal(benchmark.get("largest_constituent_weight_max_percent")),
            policy.broad_index_largest_constituent_percent_max,
        ),
        (
            "top_ten_constituents_evidence_missing",
            _as_decimal(benchmark.get("top_ten_constituent_weight_max_percent")),
            policy.broad_index_top_ten_percent_max,
        ),
        (
            "industry_concentration_evidence_missing",
            _as_decimal(benchmark.get("largest_industry_weight_max_percent")),
            policy.broad_index_largest_industry_percent_max,
        ),
    )
    failures = False
    for missing_code, observed, maximum in concentration_gates:
        if observed is None:
            missing.add(missing_code)
        elif observed > maximum:
            failures = True
    industry_count = _as_int(benchmark.get("industry_count_min", "current_industry_count"))
    if industry_count is None:
        missing.add("industry_count_evidence_missing")
    elif industry_count < policy.broad_index_industries_min:
        failures = True

    for kind, missing_code in (
        ("holdings_evidence_complete", "holdings_evidence_missing"),
        ("fee_evidence_present", "fee_evidence_missing"),
        ("size_evidence_present", "size_evidence_missing"),
        ("share_class_evidence_present", "share_class_evidence_missing"),
    ):
        if _as_bool(report.get(kind)) is not True:
            missing.add(missing_code)

    if missing:
        reasons.add("critical_evidence_missing")
        if "holdings_evidence_missing" in missing:
            reasons.add("holdings_evidence_missing")
        if missing & {
            "industry_concentration_evidence_missing",
            "industry_count_evidence_missing",
        }:
            reasons.add("industry_evidence_missing")
        return (
            family,
            RiskBucket.CONCENTRATED_EQUITY,
            PortfolioRole.NOT_ELIGIBLE,
            missing,
            reasons,
        )
    if failures:
        return (
            family,
            RiskBucket.CONCENTRATED_EQUITY,
            PortfolioRole.SATELLITE_ONLY,
            missing,
            reasons,
        )
    return family, RiskBucket.DIVERSIFIED_EQUITY, PortfolioRole.CORE_ELIGIBLE, missing, reasons


def _result_status(
    *,
    family: ProductFamily,
    missing: Set[str],
    conflicts: Set[str],
    freshness: Tuple[EvidenceFreshness, ...],
    classified_at: datetime,
) -> EvidenceStatus:
    if family is ProductFamily.UNSUPPORTED:
        return EvidenceStatus.UNCLASSIFIED
    if any(
        item.critical
        and (item.state is not FreshnessState.CURRENT or item.valid_until <= classified_at)
        for item in freshness
    ):
        return EvidenceStatus.STALE
    if conflicts - _WARNING_ONLY_CONFLICTS:
        return EvidenceStatus.CONFLICTED
    if family is ProductFamily.UNCLASSIFIED:
        return EvidenceStatus.UNCLASSIFIED
    if missing:
        return EvidenceStatus.PARTIAL
    return EvidenceStatus.VERIFIED


def _valid_until(
    freshness: Tuple[EvidenceFreshness, ...],
    classified_at: datetime,
) -> datetime:
    candidates = [item.valid_until for item in freshness if item.critical]
    value = min(candidates) if candidates else classified_at + timedelta(microseconds=1)
    return value if value > classified_at else classified_at + timedelta(microseconds=1)


def classify_fund(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> FundRiskClassification:
    """Return a deterministic, amount-free fund classification without I/O."""

    evidence.validate()
    policy.validate()
    if type(classified_at) is not datetime or classified_at.tzinfo is not timezone.utc:
        raise ValueError("classified_at must use canonical UTC")

    legal = _Facts((evidence.legal_facts,))
    benchmark = _Facts((evidence.benchmark_facts,))
    disclosures = _Facts((evidence.existing_disclosure_facts,))
    report = _Facts((evidence.report_facts, evidence.existing_disclosure_facts))
    all_facts = _Facts(
        (
            evidence.legal_facts,
            evidence.benchmark_facts,
            evidence.report_facts,
            evidence.existing_disclosure_facts,
        )
    )
    conflicts = (
        set(evidence.nav_conflicts)
        | all_facts.conflicts
        | _observed_risk_conflicts(legal, report)
        | _identity_scope_conflicts(legal, disclosures)
    )
    missing: Set[str] = set()
    reasons: Set[str] = set()
    freshness_document_ids = {item.source_document_id for item in evidence.freshness}
    if set(evidence.document_ids) - freshness_document_ids:
        missing.add("freshness_evidence_missing")
        reasons.add("critical_evidence_missing")

    if _is_unsupported(legal):
        family = ProductFamily.UNSUPPORTED
        bucket = RiskBucket.UNCLASSIFIED
        role = PortfolioRole.NOT_ELIGIBLE
        reasons.add("unsupported_product_family")
    else:
        family = _declared_family(legal) or ProductFamily.UNCLASSIFIED
        if family is ProductFamily.UNCLASSIFIED and _formal_theme_identity(
            legal,
            benchmark,
            policy,
        ):
            family = ProductFamily.SECTOR_THEME
        elif family is ProductFamily.UNCLASSIFIED and _formal_broad_index_identity(benchmark):
            family = ProductFamily.BROAD_INDEX
        elif family in {
            ProductFamily.ACTIVE_EQUITY,
            ProductFamily.EQUITY_MIXED,
        } and _theme_identity(
            legal,
            benchmark,
            report,
            policy,
        ):
            family = ProductFamily.SECTOR_THEME
        elif family in {
            ProductFamily.BROAD_INDEX,
            ProductFamily.INDEX_ENHANCED,
            ProductFamily.QDII_BROAD_EQUITY,
        } and _formal_theme_identity(legal, benchmark, policy):
            family = (
                ProductFamily.QDII_SECTOR_THEME
                if family is ProductFamily.QDII_BROAD_EQUITY
                else ProductFamily.SECTOR_THEME
            )
        if family is ProductFamily.UNCLASSIFIED:
            bucket = RiskBucket.UNCLASSIFIED
            role = PortfolioRole.NOT_ELIGIBLE
            missing.add("legal_product_family_evidence_missing")
            reasons.update({"critical_evidence_missing", "official_scope_missing"})
        elif family is ProductFamily.MONEY_MARKET:
            family, bucket, role, family_missing, family_reasons = _money_market_result(legal)
            missing.update(family_missing)
            reasons.update(family_reasons)
        elif family in _STRICT_BOND_CANDIDATES:
            family, bucket, role, family_missing, family_reasons = _strict_bond_result(
                family,
                legal,
                report,
                policy,
            )
            missing.update(family_missing)
            reasons.update(family_reasons)
        elif family in _BOND_FAMILIES:
            bucket = RiskBucket.HYBRID_RISK
            role = PortfolioRole.NOT_ELIGIBLE
        elif family is ProductFamily.BROAD_INDEX:
            family, bucket, role, family_missing, family_reasons = _broad_index_result(
                family,
                benchmark,
                report,
                policy,
            )
            missing.update(family_missing)
            reasons.update(family_reasons)
        elif family in {ProductFamily.INDEX_ENHANCED, ProductFamily.QDII_BROAD_EQUITY}:
            if (
                family is ProductFamily.INDEX_ENHANCED
                and _as_bool(legal.get("enhancement_limits_present")) is not True
            ):
                bucket = RiskBucket.UNCLASSIFIED
                role = PortfolioRole.NOT_ELIGIBLE
                missing.add("enhancement_limits_evidence_missing")
                reasons.add("critical_evidence_missing")
            else:
                family, bucket, role, family_missing, family_reasons = _broad_index_result(
                    family,
                    benchmark,
                    report,
                    policy,
                )
                if role is PortfolioRole.CORE_ELIGIBLE:
                    role = PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE
                missing.update(family_missing)
                reasons.update(family_reasons)
        elif family in {ProductFamily.SECTOR_THEME, ProductFamily.QDII_SECTOR_THEME}:
            bucket = RiskBucket.CONCENTRATED_EQUITY
            role = PortfolioRole.SATELLITE_ONLY
        elif family in {ProductFamily.ACTIVE_EQUITY, ProductFamily.EQUITY_MIXED}:
            family, bucket, role, family_missing, family_reasons = _active_equity_result(
                family,
                report,
                policy,
            )
            missing.update(family_missing)
            reasons.update(family_reasons)
        else:
            bucket = RiskBucket.UNCLASSIFIED
            role = PortfolioRole.NOT_ELIGIBLE
            missing.add("family_classification_evidence_missing")
            reasons.add("critical_evidence_missing")

    status = _result_status(
        family=family,
        missing=missing,
        conflicts=conflicts,
        freshness=evidence.freshness,
        classified_at=classified_at,
    )
    status_reason = {
        EvidenceStatus.VERIFIED: "classification_verified",
        EvidenceStatus.PARTIAL: "classification_partial",
        EvidenceStatus.CONFLICTED: "classification_conflicted",
        EvidenceStatus.STALE: "classification_stale",
        EvidenceStatus.UNCLASSIFIED: "classification_unclassified",
    }[status]
    reasons.add(status_reason)

    if status is EvidenceStatus.STALE:
        reasons.add("critical_evidence_stale")
        role = PortfolioRole.NOT_ELIGIBLE
        bucket = RiskBucket.UNCLASSIFIED
    elif status is EvidenceStatus.CONFLICTED:
        role = PortfolioRole.NOT_ELIGIBLE
        if bucket in {
            RiskBucket.CASH_LIKE_CANDIDATE,
            RiskBucket.HIGH_QUALITY_FIXED_INCOME,
        }:
            bucket = RiskBucket.HYBRID_RISK
    elif status is EvidenceStatus.PARTIAL:
        role = PortfolioRole.NOT_ELIGIBLE
        if bucket in {
            RiskBucket.CASH_LIKE_CANDIDATE,
            RiskBucket.HIGH_QUALITY_FIXED_INCOME,
        }:
            bucket = RiskBucket.UNCLASSIFIED

    result = FundRiskClassification(
        fund_code=evidence.fund_code,
        policy_version=policy.version,
        input_fingerprint=_input_fingerprint(evidence, policy, classified_at),
        product_family=family,
        risk_bucket=bucket,
        portfolio_role=role,
        evidence_status=status,
        evidence_tags=_evidence_tags(family, legal, report),
        reason_codes=tuple(sorted(reasons)),
        missing_evidence=tuple(sorted(missing)),
        conflicts=tuple(sorted(conflicts)),
        evidence_document_ids=evidence.document_ids,
        evidence_fact_ids=evidence.fact_ids,
        freshness=evidence.freshness,
        classified_at=classified_at,
        valid_until=_valid_until(evidence.freshness, classified_at),
    )
    result.validate()
    return result


def conservative_classification_rank(
    result: FundRiskClassification,
) -> Tuple[int, int, int]:
    """Return an internal invariant order, never a product or recommendation score."""

    if type(result) is not FundRiskClassification:
        raise ValueError("conservative ordering requires an exact classification result")
    result.validate()
    return (
        EVIDENCE_STATUS_CONSERVATIVE_ORDER.index(result.evidence_status),
        RISK_BUCKET_CONSERVATIVE_ORDER.index(result.risk_bucket),
        PORTFOLIO_ROLE_CONSERVATIVE_ORDER.index(result.portfolio_role),
    )


__all__ = [
    "ClassificationEvidence",
    "EVIDENCE_STATUS_CONSERVATIVE_ORDER",
    "PORTFOLIO_ROLE_CONSERVATIVE_ORDER",
    "RISK_BUCKET_CONSERVATIVE_ORDER",
    "classify_fund",
    "classification_input_manifest",
    "classification_input_manifest_v1",
    "classification_input_manifest_v2",
    "conservative_classification_rank",
]
