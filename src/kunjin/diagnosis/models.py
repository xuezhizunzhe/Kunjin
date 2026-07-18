from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

_FUND_CODE = re.compile(r"^[0-9]{6}$", flags=re.ASCII)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$", flags=re.ASCII)
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)
_PRIVATE_KEY_PARTS = (
    "amount",
    "cost",
    "debt",
    "income",
    "profile",
    "profit",
    "reserve",
    "shares",
)
_EVIDENCE_STATES = frozenset({"complete", "partial", "insufficient_data"})
_RELATIONSHIP_TYPES = frozenset(
    {
        "disclosed_industry_overlap",
        "disclosed_overlap",
        "duplicate_holding_identity",
        "same_company",
        "same_current_benchmark",
        "same_manager",
        "share_class_sibling",
        "top10_disclosed_overlap",
    }
)
_FINDING_TYPES = frozenset(
    {
        "candidate_observed_duplication",
        "coverage_gap",
        "disclosed_industry_duplication",
        "disclosed_security_duplication",
        "largest_position_concentration",
        "portfolio_hhi_observation",
        "same_current_benchmark_text",
        "same_current_manager",
        "same_exact_index_or_theme",
        "same_share_class_family",
    }
)
_SEVERITIES = frozenset({"attention", "information", "insufficient_data", "risk_flag"})
_CANDIDATE_LABELS = frozenset(
    {
        "insufficient_data",
        "mixed_observed_impact",
        "observed_adds_distinct_exposure",
        "observed_duplicates_existing_exposure",
    }
)


def _exact_dataclass(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be an exact {expected.__name__}")
    if set(value.__dict__) != {item.name for item in fields(expected)}:
        raise ValueError(f"{name} exact state is invalid")


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return value


def _fund_code(value: object, name: str = "fund code") -> str:
    if type(value) is not str or _FUND_CODE.fullmatch(value) is None or value == "000000":
        raise ValueError(f"{name} must be a non-reserved six-digit code")
    return value


def _ascending(values: object, name: str, *, identifiers: bool = True) -> Tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    for value in values:
        if identifiers:
            _identifier(value, name)
        elif type(value) is not str or not value:
            raise ValueError(f"{name} entries must be non-empty strings")
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and ascending")
    return values


def _fund_codes(values: object, name: str, *, minimum: int = 0) -> Tuple[str, ...]:
    if type(values) is not tuple or len(values) < minimum:
        raise ValueError(f"{name} must be an exact tuple")
    for value in values:
        _fund_code(value, name)
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and ascending")
    return values


def _ratio(value: object, name: str) -> Optional[Decimal]:
    if value is None:
        return None
    if (
        type(value) is not Decimal
        or not value.is_finite()
        or not Decimal("0") <= value <= Decimal("1")
    ):
        raise ValueError(f"{name} must be a finite Decimal in [0, 1] or None")
    return value


def _aware_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not timezone.utc
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must use canonical UTC")
    return value


def _metric_value(value: object, key_path: Tuple[str, ...]) -> None:
    if value is None or type(value) in {bool, int, str, date}:
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("diagnosis metric Decimal must be finite")
        return
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("diagnosis metric datetime must be timezone-aware")
        return
    if type(value) is tuple:
        for item in value:
            _metric_value(item, key_path)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("diagnosis metric keys must be strings")
            _private_key(key)
            _metric_value(item, (*key_path, key))
        return
    raise ValueError(f"unsupported diagnosis metric at {'.'.join(key_path)}")


def _private_key(value: str) -> None:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold())
    if any(part in normalized for part in _PRIVATE_KEY_PARTS):
        raise ValueError("diagnosis metrics contain a private field")


@dataclass(frozen=True)
class DiagnosisCoverage:
    scope: str
    evidence_state: str
    included_fund_codes: Tuple[str, ...]
    omitted_fund_codes: Tuple[str, ...]
    unknown_fields: Tuple[str, ...]
    known_weight: Optional[Decimal]

    def validate(self) -> None:
        _exact_dataclass(self, DiagnosisCoverage, "diagnosis coverage")
        _identifier(self.scope, "diagnosis coverage scope")
        if self.evidence_state not in _EVIDENCE_STATES:
            raise ValueError("diagnosis coverage evidence state is unsupported")
        included = _fund_codes(self.included_fund_codes, "included fund codes")
        omitted = _fund_codes(self.omitted_fund_codes, "omitted fund codes")
        if set(included).intersection(omitted):
            raise ValueError("diagnosis coverage fund partitions overlap")
        _ascending(self.unknown_fields, "diagnosis coverage unknown fields")
        _ratio(self.known_weight, "known weight")


@dataclass(frozen=True)
class DiagnosisRelationship:
    relationship_id: str
    relationship_type: str
    fund_codes: Tuple[str, ...]
    evidence_state: str
    metrics: Tuple[Tuple[str, object], ...]
    report_periods: Tuple[date, ...]
    publication_times: Tuple[datetime, ...]
    warnings: Tuple[str, ...]

    def validate(self, as_of: datetime) -> None:
        _exact_dataclass(self, DiagnosisRelationship, "diagnosis relationship")
        _identifier(self.relationship_id, "diagnosis relationship id")
        if self.relationship_type not in _RELATIONSHIP_TYPES:
            raise ValueError("diagnosis relationship type is unsupported")
        _fund_codes(self.fund_codes, "diagnosis relationship fund codes", minimum=2)
        if self.evidence_state not in _EVIDENCE_STATES:
            raise ValueError("diagnosis relationship evidence state is unsupported")
        if type(self.metrics) is not tuple:
            raise ValueError("diagnosis relationship metrics must be an exact tuple")
        metric_keys = []
        for item in self.metrics:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("diagnosis relationship metric entries are invalid")
            key, value = item
            key = _identifier(key, "diagnosis relationship metric key")
            _private_key(key)
            _metric_value(value, (key,))
            metric_keys.append(key)
        if tuple(sorted(set(metric_keys))) != tuple(metric_keys):
            raise ValueError("diagnosis relationship metric keys must be unique and ascending")
        if type(self.report_periods) is not tuple or any(
            type(value) is not date for value in self.report_periods
        ):
            raise ValueError("diagnosis relationship report periods require exact dates")
        if tuple(sorted(set(self.report_periods))) != self.report_periods:
            raise ValueError("diagnosis relationship report periods must be unique and ascending")
        if type(self.publication_times) is not tuple:
            raise ValueError("diagnosis relationship publication times must be an exact tuple")
        for value in self.publication_times:
            timestamp = _aware_utc(value, "diagnosis relationship publication time")
            if timestamp > as_of:
                raise ValueError("diagnosis relationship evidence is from the future")
        if tuple(sorted(set(self.publication_times))) != self.publication_times:
            raise ValueError(
                "diagnosis relationship publication times must be unique and ascending"
            )
        _ascending(self.warnings, "diagnosis relationship warnings")


@dataclass(frozen=True)
class DiagnosisFinding:
    finding_id: str
    finding_type: str
    severity: str
    fund_codes: Tuple[str, ...]
    relationship_ids: Tuple[str, ...]
    evidence_scope: str

    def validate(self) -> None:
        _exact_dataclass(self, DiagnosisFinding, "diagnosis finding")
        _identifier(self.finding_id, "diagnosis finding id")
        if self.finding_type not in _FINDING_TYPES:
            raise ValueError("diagnosis finding type is unsupported")
        if self.severity not in _SEVERITIES:
            raise ValueError("diagnosis finding severity is unsupported")
        _fund_codes(self.fund_codes, "diagnosis finding fund codes")
        _ascending(self.relationship_ids, "diagnosis finding relationship ids")
        _identifier(self.evidence_scope, "diagnosis finding evidence scope")


@dataclass(frozen=True)
class CandidateImpact:
    fund_code: str
    label: str
    relationship_ids: Tuple[str, ...]
    disclosed_weight: Optional[Decimal]
    observed_overlap: Optional[Decimal]
    unknown_fields: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, CandidateImpact, "candidate impact")
        _fund_code(self.fund_code, "candidate fund code")
        if self.label not in _CANDIDATE_LABELS:
            raise ValueError("candidate label is unsupported")
        _ascending(self.relationship_ids, "candidate relationship ids")
        _ratio(self.disclosed_weight, "candidate disclosed weight")
        _ratio(self.observed_overlap, "candidate observed overlap")
        _ascending(self.unknown_fields, "candidate unknown fields")
        if (self.disclosed_weight is None) != (self.observed_overlap is None):
            raise ValueError("candidate weights must be both available or both unavailable")
        if (
            self.disclosed_weight is not None
            and self.observed_overlap is not None
            and self.observed_overlap > self.disclosed_weight
        ):
            raise ValueError("candidate overlap cannot exceed disclosed weight")


@dataclass(frozen=True)
class PortfolioDiagnosis:
    as_of: datetime
    value_basis: str
    position_count: int
    hhi: Optional[Decimal]
    largest_position_share: Optional[Decimal]
    relationship_coverage: DiagnosisCoverage
    holdings_coverage: DiagnosisCoverage
    relationships: Tuple[DiagnosisRelationship, ...]
    candidate_impact: Optional[CandidateImpact]
    findings: Tuple[DiagnosisFinding, ...]
    missing_evidence: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    warnings: Tuple[str, ...]
    input_fingerprint: str
    action_maturity: str = "evidence_only"
    action_authorized: bool = False
    exact_amount_available: bool = False

    def validate(self) -> None:
        _exact_dataclass(self, PortfolioDiagnosis, "portfolio diagnosis")
        as_of = _aware_utc(self.as_of, "portfolio diagnosis as-of")
        if self.value_basis not in {"estimated", "formal", "missing"}:
            raise ValueError("portfolio diagnosis value basis is unsupported")
        if type(self.position_count) is not int or self.position_count < 0:
            raise ValueError("portfolio diagnosis position count is invalid")
        _ratio(self.hhi, "portfolio HHI")
        _ratio(self.largest_position_share, "largest position share")
        self.relationship_coverage.validate()
        self.holdings_coverage.validate()
        relationship_partition = set(self.relationship_coverage.included_fund_codes) | set(
            self.relationship_coverage.omitted_fund_codes
        )
        holdings_partition = set(self.holdings_coverage.included_fund_codes) | set(
            self.holdings_coverage.omitted_fund_codes
        )
        if relationship_partition != holdings_partition:
            raise ValueError("diagnosis coverage partitions do not match")
        if len(relationship_partition) != self.position_count:
            raise ValueError("diagnosis coverage partition does not match position count")
        if type(self.relationships) is not tuple:
            raise ValueError("diagnosis relationships must be an exact tuple")
        relationship_ids = []
        for relationship in self.relationships:
            relationship.validate(as_of)
            relationship_ids.append(relationship.relationship_id)
        if tuple(sorted(set(relationship_ids))) != tuple(relationship_ids):
            raise ValueError("diagnosis relationships must have unique ascending ids")
        available_relationships = set(relationship_ids)
        if self.candidate_impact is not None:
            self.candidate_impact.validate()
            if not set(self.candidate_impact.relationship_ids).issubset(
                available_relationships
            ):
                raise ValueError("candidate relationship evidence does not close")
        if type(self.findings) is not tuple:
            raise ValueError("diagnosis findings must be an exact tuple")
        finding_ids = []
        for finding in self.findings:
            finding.validate()
            finding_ids.append(finding.finding_id)
            if not set(finding.relationship_ids).issubset(available_relationships):
                raise ValueError("diagnosis finding relationship evidence does not close")
        if tuple(sorted(set(finding_ids))) != tuple(finding_ids):
            raise ValueError("diagnosis findings must have unique ascending ids")
        for values, name in (
            (self.missing_evidence, "diagnosis missing evidence"),
            (self.conflicts, "diagnosis conflicts"),
            (self.warnings, "diagnosis warnings"),
        ):
            _ascending(values, name)
        if type(self.input_fingerprint) is not str or _CHECKSUM.fullmatch(
            self.input_fingerprint
        ) is None:
            raise ValueError("diagnosis input fingerprint must be lowercase SHA-256")
        if (
            self.action_maturity != "evidence_only"
            or self.action_authorized is not False
            or self.exact_amount_available is not False
        ):
            raise ValueError("portfolio diagnosis action boundary is invalid")
