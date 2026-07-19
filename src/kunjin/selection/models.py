from __future__ import annotations

import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import combinations
from typing import Optional, Sequence, Tuple

_FUND_CODE = re.compile(r"^[0-9]{6}$", flags=re.ASCII)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$", flags=re.ASCII)
_CHECKSUM = re.compile(r"^[0-9a-f]{64}$", flags=re.ASCII)

_PRIVATE_DYNAMIC_KEYS = frozenset(
    {
        "account_title",
        "amount",
        "asset",
        "cost",
        "debt",
        "income",
        "monthly_income",
        "portfolio_weight",
        "profit",
        "profile",
        "reserve",
        "shares",
        "total_value",
    }
)
_COMPARISON_STATES = frozenset(
    {
        "insufficient_data",
        "not_comparable",
        "relative_tradeoffs_only",
        "conditional_shortlist",
    }
)
_CANDIDATE_EVIDENCE_STATES = frozenset(
    {
        "insufficient_data",
        "not_comparable",
        "relative_tradeoffs_only",
        "conditional_shortlist_member",
    }
)
_COMPARABILITY_STATES = frozenset({"comparable", "not_comparable", "insufficient_data"})
_POSITION_STATES = frozenset({"held", "not_held"})
_D1_EVIDENCE_STATES = frozenset(
    {"verified", "partial", "conflicted", "stale", "unclassified"}
)
_RISK_BUCKETS = frozenset(
    {
        "cash_like_candidate",
        "high_quality_fixed_income",
        "diversified_equity",
        "concentrated_equity",
        "hybrid_risk",
        "unclassified",
    }
)
_PORTFOLIO_ROLES = frozenset(
    {
        "cash_management_candidate",
        "core_eligible",
        "active_diversifier_eligible",
        "satellite_only",
        "not_eligible",
    }
)
_MAPPED_ASSET_LAYERS = frozenset(
    {"high_quality_fixed_income", "diversified_equity"}
)
_PORTFOLIO_IMPACT_STATES = frozenset({"usable", "insufficient_data"})
_PORTFOLIO_IMPACT_LABELS = frozenset(
    {
        "insufficient_data",
        "mixed_observed_impact",
        "observed_adds_distinct_exposure",
        "observed_duplicates_existing_exposure",
    }
)
_GATE_STATES = frozenset({"fresh", "stale", "missing", "transient"})


def _exact_dataclass(value: object, expected: type, name: str) -> None:
    if type(value) is not expected:
        raise ValueError(f"{name} must be an exact {expected.__name__}")
    if set(vars(value)) != {item.name for item in fields(expected)}:
        raise ValueError(f"{name} exact state is invalid")


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return value


def _optional_identifier(value: object, name: str) -> Optional[str]:
    if value is None:
        return None
    return _identifier(value, name)


def _fund_code(value: object, name: str = "fund code") -> str:
    if type(value) is not str or _FUND_CODE.fullmatch(value) is None or value == "000000":
        raise ValueError(f"{name} must be a non-reserved six-digit code")
    return value


def validate_candidate_codes(candidate_codes: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(candidate_codes, (str, bytes)):
        raise ValueError("candidate codes must contain two to five exact values")
    try:
        codes = tuple(candidate_codes)
    except TypeError as exc:
        raise ValueError("candidate codes must contain two to five exact values") from exc
    if not 2 <= len(codes) <= 5:
        raise ValueError("candidate codes must contain two to five exact values")
    for code in codes:
        try:
            _fund_code(code, "candidate codes")
        except ValueError as exc:
            raise ValueError(
                "candidate codes must be unique non-reserved six-digit values"
            ) from exc
    if len(codes) != len(set(codes)):
        raise ValueError("candidate codes must be unique non-reserved six-digit values")
    return codes


def _exact_candidate_codes(candidate_codes: object) -> Tuple[str, ...]:
    if type(candidate_codes) is not tuple:
        raise ValueError("candidate codes must be an exact tuple")
    return validate_candidate_codes(candidate_codes)


def _ascending_identifiers(values: object, name: str) -> Tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{name} must be an exact tuple")
    for value in values:
        _identifier(value, name)
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be unique and ascending")
    return values


def _canonical_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is not timezone.utc
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must use canonical UTC")
    return value


def _dynamic_metric(value: object, path: Tuple[str, ...]) -> None:
    if value is None or type(value) in {bool, int, str, date}:
        return
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("selection metric datetime must be timezone-aware")
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("selection metric Decimal must be finite")
        return
    if type(value) in {tuple, list}:
        for item in value:
            _dynamic_metric(item, path)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("selection metric keys must be strings")
            if key.casefold() in _PRIVATE_DYNAMIC_KEYS:
                raise ValueError("selection metrics contain a private field")
            _dynamic_metric(item, (*path, key))
        return
    location = ".".join(path) or "metric_comparisons"
    raise ValueError(f"unsupported selection metric at {location}")


@dataclass(frozen=True)
class PersonalGateEvidence:
    suitability_state: str
    suitability_freshness: str
    suitability_status: Optional[str]
    allocation_state: str
    allocation_freshness: str
    allocation_status: Optional[str]
    blocking_codes: Tuple[str, ...]
    constraint_codes: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, PersonalGateEvidence, "personal gate evidence")
        for value, name in (
            (self.suitability_state, "suitability state"),
            (self.suitability_freshness, "suitability freshness"),
            (self.allocation_state, "allocation state"),
            (self.allocation_freshness, "allocation freshness"),
        ):
            if value not in _GATE_STATES:
                raise ValueError(f"{name} is unsupported")
        _optional_identifier(self.suitability_status, "suitability status")
        _optional_identifier(self.allocation_status, "allocation status")
        _ascending_identifiers(self.blocking_codes, "personal gate blocking codes")
        _ascending_identifiers(self.constraint_codes, "personal gate constraint codes")


@dataclass(frozen=True)
class ComparabilityEvidence:
    left_fund_code: str
    right_fund_code: str
    state: str
    reason_code: str
    warning_codes: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, ComparabilityEvidence, "comparability evidence")
        left = _fund_code(self.left_fund_code, "left comparability fund code")
        right = _fund_code(self.right_fund_code, "right comparability fund code")
        if left == right:
            raise ValueError("comparability evidence requires two distinct fund codes")
        if self.state not in _COMPARABILITY_STATES:
            raise ValueError("comparability state is unsupported")
        _identifier(self.reason_code, "comparability reason code")
        _ascending_identifiers(self.warning_codes, "comparability warning codes")


@dataclass(frozen=True)
class CandidateReview:
    fund_code: str
    position_state: str
    evidence_state: str
    d1_evidence_status: Optional[str]
    risk_bucket: Optional[str]
    portfolio_role: Optional[str]
    mapped_asset_layer: Optional[str]
    portfolio_impact_state: str
    portfolio_impact_label: Optional[str]
    relationship_ids: Tuple[str, ...]
    advantage_codes: Tuple[str, ...]
    tradeoff_codes: Tuple[str, ...]
    blocking_codes: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    warnings: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, CandidateReview, "candidate review")
        _fund_code(self.fund_code, "candidate review fund code")
        if self.position_state not in _POSITION_STATES:
            raise ValueError("candidate position state is unsupported")
        if self.evidence_state not in _CANDIDATE_EVIDENCE_STATES:
            raise ValueError("candidate evidence state is unsupported")
        if (
            self.d1_evidence_status is not None
            and self.d1_evidence_status not in _D1_EVIDENCE_STATES
        ):
            raise ValueError("candidate D1 evidence status is unsupported")
        if self.risk_bucket is not None and self.risk_bucket not in _RISK_BUCKETS:
            raise ValueError("candidate risk bucket is unsupported")
        if self.portfolio_role is not None and self.portfolio_role not in _PORTFOLIO_ROLES:
            raise ValueError("candidate portfolio role is unsupported")
        if (
            self.mapped_asset_layer is not None
            and self.mapped_asset_layer not in _MAPPED_ASSET_LAYERS
        ):
            raise ValueError("candidate mapped asset layer is unsupported")
        if self.portfolio_impact_state not in _PORTFOLIO_IMPACT_STATES:
            raise ValueError("candidate portfolio impact state is unsupported")
        if (
            self.portfolio_impact_label is not None
            and self.portfolio_impact_label not in _PORTFOLIO_IMPACT_LABELS
        ):
            raise ValueError("candidate portfolio impact label is unsupported")
        if self.portfolio_impact_state == "usable" and self.portfolio_impact_label is None:
            raise ValueError("usable candidate portfolio impact requires a label")
        for values, name in (
            (self.relationship_ids, "candidate relationship ids"),
            (self.advantage_codes, "candidate advantage codes"),
            (self.tradeoff_codes, "candidate tradeoff codes"),
            (self.blocking_codes, "candidate blocking codes"),
            (self.missing_evidence, "candidate missing evidence"),
            (self.conflicts, "candidate conflicts"),
            (self.warnings, "candidate warnings"),
        ):
            _ascending_identifiers(values, name)


@dataclass(frozen=True)
class ShortlistResult:
    as_of: datetime
    candidate_codes: Tuple[str, ...]
    comparison_state: str
    personal_gate: PersonalGateEvidence
    comparability: Tuple[ComparabilityEvidence, ...]
    metric_comparisons: Tuple[Tuple[str, object], ...]
    candidate_reviews: Tuple[CandidateReview, ...]
    shortlist_codes: Tuple[str, ...]
    invalidation_conditions: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    warnings: Tuple[str, ...]
    input_fingerprint: str
    action_maturity: str = "evidence_only"
    action_authorized: bool = False
    exact_amount_available: bool = False
    automatic_trade: bool = False

    def validate(self) -> None:
        _exact_dataclass(self, ShortlistResult, "shortlist result")
        _canonical_utc(self.as_of, "shortlist result as-of")
        codes = _exact_candidate_codes(self.candidate_codes)
        if self.comparison_state not in _COMPARISON_STATES:
            raise ValueError("shortlist comparison state is unsupported")
        if type(self.personal_gate) is not PersonalGateEvidence:
            raise ValueError("shortlist personal gate must be exact")
        self.personal_gate.validate()
        self._validate_comparability(codes)
        self._validate_metrics()
        self._validate_candidates(codes)
        self._validate_shortlist(codes)
        for values, name in (
            (self.invalidation_conditions, "shortlist invalidation conditions"),
            (self.missing_evidence, "shortlist missing evidence"),
            (self.conflicts, "shortlist conflicts"),
            (self.warnings, "shortlist warnings"),
        ):
            _ascending_identifiers(values, name)
        if type(self.input_fingerprint) is not str or _CHECKSUM.fullmatch(
            self.input_fingerprint
        ) is None:
            raise ValueError("shortlist input fingerprint must be lowercase SHA-256")
        if (
            self.action_maturity != "evidence_only"
            or self.action_authorized is not False
            or self.exact_amount_available is not False
            or self.automatic_trade is not False
        ):
            raise ValueError("shortlist action boundary is invalid")

    def _validate_comparability(self, codes: Tuple[str, ...]) -> None:
        if type(self.comparability) is not tuple:
            raise ValueError("shortlist comparability must be an exact tuple")
        expected_pairs = tuple(combinations(codes, 2))
        actual_pairs = []
        for item in self.comparability:
            if type(item) is not ComparabilityEvidence:
                raise ValueError("shortlist comparability must use exact records")
            item.validate()
            actual_pairs.append((item.left_fund_code, item.right_fund_code))
        if tuple(actual_pairs) != expected_pairs:
            raise ValueError("shortlist comparability pairs do not close over the request")

    def _validate_metrics(self) -> None:
        if type(self.metric_comparisons) is not tuple:
            raise ValueError("shortlist metric comparisons must be an exact tuple")
        keys = []
        for item in self.metric_comparisons:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("shortlist metric comparison entries are invalid")
            key, value = item
            key = _identifier(key, "shortlist metric comparison key")
            if key.casefold() in _PRIVATE_DYNAMIC_KEYS:
                raise ValueError("selection metrics contain a private field")
            _dynamic_metric(value, (key,))
            keys.append(key)
        if tuple(sorted(set(keys))) != tuple(keys):
            raise ValueError("shortlist metric comparison keys must be unique and ascending")

    def _validate_candidates(self, codes: Tuple[str, ...]) -> None:
        if type(self.candidate_reviews) is not tuple:
            raise ValueError("shortlist candidate reviews must be an exact tuple")
        review_codes = []
        for item in self.candidate_reviews:
            if type(item) is not CandidateReview:
                raise ValueError("shortlist candidate reviews must use exact records")
            item.validate()
            review_codes.append(item.fund_code)
        if tuple(review_codes) != codes:
            raise ValueError("shortlist candidate reviews must close in request order")

    def _validate_shortlist(self, codes: Tuple[str, ...]) -> None:
        if type(self.shortlist_codes) is not tuple:
            raise ValueError("shortlist codes must be an exact tuple")
        for code in self.shortlist_codes:
            _fund_code(code, "shortlist code")
        expected_order = tuple(code for code in codes if code in set(self.shortlist_codes))
        if (
            len(self.shortlist_codes) != len(set(self.shortlist_codes))
            or self.shortlist_codes != expected_order
        ):
            raise ValueError("shortlist codes must be unique and remain in request order")
        reviews_by_code = {item.fund_code: item for item in self.candidate_reviews}
        if any(
            reviews_by_code[code].evidence_state != "conditional_shortlist_member"
            for code in self.shortlist_codes
        ):
            raise ValueError("every shortlist code must reference a conditional shortlist member")
        member_codes = tuple(
            item.fund_code
            for item in self.candidate_reviews
            if item.evidence_state == "conditional_shortlist_member"
        )
        if member_codes != self.shortlist_codes:
            raise ValueError("conditional shortlist members must close over shortlist codes")
        if self.comparison_state == "conditional_shortlist":
            if len(self.shortlist_codes) < 2:
                raise ValueError("conditional shortlist requires at least two members")
        elif self.shortlist_codes:
            raise ValueError("non-conditional states cannot contain shortlist codes")
