from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Tuple

from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import (
    BriefCoverage,
    BriefEvidenceState,
    BriefFact,
    RelationshipEvidence,
)
from kunjin.brief.policy import MAX_FACTS, MAX_RELATIONSHIPS
from kunjin.decision.models import (
    MAX_TUPLE_ITEMS,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    canonical_decimal,
    canonical_json_bytes,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_request_id,
)
from kunjin.models import StoredPosition

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_CURRENT_AGE = timedelta(days=1)
_DATED_FALLBACK_AGE = timedelta(days=30)
_MAX_POSITIONS = 1024
_PROVENANCE_MAC_KEY = secrets.token_bytes(32)
_RELATIONSHIP_METRIC_KEYS = {
    "duplicate_holding_identity": frozenset({"multiple_observations"}),
    "share_class_sibling": frozenset({"mutual_source_links", "aggregation_eligible"}),
    "same_manager": frozenset({"shared_manager_name"}),
    "same_company": frozenset({"company_name"}),
    "same_current_benchmark": frozenset({"benchmark_description", "exact_text_match"}),
}


def _validate_relationship_semantics(relationship: RelationshipEvidence) -> None:
    metrics = relationship.metrics
    relation_type = relationship.relationship_type
    two_sided = len(relationship.fund_codes) == 2 and len(relationship.evidence_ids) == 2
    if relation_type == "duplicate_holding_identity":
        valid = (
            len(relationship.fund_codes) == 1
            and not relationship.evidence_ids
            and metrics["multiple_observations"] is True
        )
    elif relation_type == "share_class_sibling":
        valid = (
            two_sided
            and metrics["mutual_source_links"] is True
            and type(metrics["aggregation_eligible"]) is bool
            and metrics["aggregation_eligible"]
            is (relationship.evidence_state is BriefEvidenceState.COMPLETE)
        )
    elif relation_type == "same_manager":
        name = metrics["shared_manager_name"]
        valid = two_sided and type(name) is str and bool(name)
    elif relation_type == "same_company":
        company = metrics["company_name"]
        valid = two_sided and type(company) is str and bool(company)
    elif relation_type == "same_current_benchmark":
        description = metrics["benchmark_description"]
        valid = (
            two_sided
            and type(description) is str
            and bool(description)
            and metrics["exact_text_match"] is True
            and relationship.evidence_state is BriefEvidenceState.PARTIAL
            and "benchmark_text_is_not_index_identity" in relationship.warnings
        )
    else:
        valid = False
    if not valid or len(relationship.evidence_ids) != len(set(relationship.evidence_ids)):
        raise ValueError("D2 relationship semantics are invalid")


def _utc(value: datetime, name: str) -> datetime:
    return validate_aware_datetime(value, name).astimezone(timezone.utc)


def _fund_code(value: object, name: str) -> str:
    if type(value) is not str or _FUND_CODE.fullmatch(value) is None:
        raise ValueError(f"{name} must be exactly six ASCII digits")
    return value


def _ratio(value: Optional[str], name: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical ratio string or None")
    try:
        parsed = Decimal(value)
    except Exception:
        raise ValueError(f"{name} must be a canonical ratio string or None") from None
    if canonical_decimal(parsed) != value or not Decimal("0") <= parsed <= Decimal("1"):
        raise ValueError(f"{name} must be in the closed interval [0, 1]")


@dataclass(frozen=True)
class PortfolioEvidenceBinding:
    positions: Tuple[StoredPosition, ...]
    snapshot_complete: bool
    observation_version: str
    observed_at: datetime
    source_state: str
    request_id: Optional[str]
    request_mode: Optional[RequestMode]
    request_started_at: Optional[datetime]
    request_deadline_at: Optional[datetime]

    def validate(self) -> None:
        if type(self) is not PortfolioEvidenceBinding:
            raise ValueError("portfolio evidence binding subclasses are not accepted")
        validate_exact_dataclass_state(self, "portfolio evidence binding")
        if type(self.positions) is not tuple or len(self.positions) > _MAX_POSITIONS:
            raise ValueError("portfolio positions must be a bounded exact tuple")
        if type(self.snapshot_complete) is not bool:
            raise ValueError("portfolio snapshot completeness must be an exact boolean")
        validate_identifier(self.observation_version, "portfolio observation version")
        observed_at = _utc(self.observed_at, "portfolio observation time")
        if self.observed_at.tzinfo is not timezone.utc:
            raise ValueError("portfolio observation time must use canonical UTC")
        if self.source_state not in {
            "same_request_success",
            "authenticated_cache",
            "unbound",
        }:
            raise ValueError("portfolio source state is unsupported")
        if self.source_state == "same_request_success":
            if (
                self.request_id is None
                or self.request_mode is None
                or self.request_started_at is None
                or self.request_deadline_at is None
            ):
                raise ValueError("same-request portfolio evidence requires its request window")
            validate_request_id(self.request_id)
            if type(self.request_mode) is not RequestMode:
                raise ValueError("portfolio request mode must be an exact RequestMode")
            started_at = _utc(self.request_started_at, "portfolio request start")
            deadline_at = _utc(self.request_deadline_at, "portfolio request deadline")
            expected_seconds = 90 if self.request_mode is RequestMode.RAPID else 480
            if (
                self.request_started_at.tzinfo is not timezone.utc
                or self.request_deadline_at.tzinfo is not timezone.utc
                or not started_at <= observed_at <= deadline_at
                or deadline_at - started_at != timedelta(seconds=expected_seconds)
            ):
                raise ValueError("portfolio request window does not bind its observation")
        elif any(
            item is not None
            for item in (
                self.request_id,
                self.request_mode,
                self.request_started_at,
                self.request_deadline_at,
            )
        ):
            raise ValueError("cached or unbound portfolio evidence cannot carry a request window")
        for position in self.positions:
            if type(position) is not StoredPosition:
                raise ValueError("portfolio binding requires exact StoredPosition records")
            _fund_code(position.fund_code, "position fund code")
            if type(position.account_title) is not str or not position.account_title:
                raise ValueError("position account title is invalid")
            if type(position.fund_name) is not str or not position.fund_name:
                raise ValueError("position fund name is invalid")
            if (
                type(position.shares) is not Decimal
                or not position.shares.is_finite()
                or position.shares < 0
            ):
                raise ValueError("position shares must be a non-negative Decimal")
            for nav in (position.formal_nav, position.estimated_nav):
                if nav is not None and (
                    type(nav) is not Decimal or not nav.is_finite() or nav <= 0
                ):
                    raise ValueError("position NAV must be a positive Decimal or None")
            if position.observed_profit is not None and (
                type(position.observed_profit) is not Decimal
                or not position.observed_profit.is_finite()
            ):
                raise ValueError("observed profit must be a Decimal or None")
            if _utc(position.observed_at, "position observation time") > observed_at:
                raise ValueError("position observation cannot follow its bound snapshot")


@dataclass(frozen=True)
class D2PortfolioProvenance:
    source_state: str
    snapshot_complete: bool
    observation_version: str
    observed_at: datetime
    oldest_position_observed_at: Optional[datetime]
    source_request_id: Optional[str]
    source_request_mode: Optional[RequestMode]
    source_request_started_at: Optional[datetime]
    source_request_deadline_at: Optional[datetime]
    current_request_id: str
    current_request_mode: RequestMode
    as_of: datetime
    binding_mac: str

    def binding_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "as_of": self.as_of,
                "current_request_id": self.current_request_id,
                "current_request_mode": self.current_request_mode,
                "observation_version": self.observation_version,
                "observed_at": self.observed_at,
                "oldest_position_observed_at": self.oldest_position_observed_at,
                "snapshot_complete": self.snapshot_complete,
                "source_request_deadline_at": self.source_request_deadline_at,
                "source_request_id": self.source_request_id,
                "source_request_mode": self.source_request_mode,
                "source_request_started_at": self.source_request_started_at,
                "source_state": self.source_state,
            }
        )

    def validate(self) -> None:
        if type(self) is not D2PortfolioProvenance:
            raise ValueError("D2 portfolio provenance subclasses are not accepted")
        validate_exact_dataclass_state(self, "D2 portfolio provenance")
        validate_checksum(self.binding_mac, "D2 portfolio binding MAC")
        if not hmac.compare_digest(self.binding_mac, _provenance_mac(self)):
            raise ValueError("D2 portfolio provenance MAC does not match its fields")
        if self.source_state not in {
            "same_request_success",
            "authenticated_cache",
            "unbound",
        }:
            raise ValueError("D2 portfolio provenance source state is unsupported")
        if type(self.snapshot_complete) is not bool:
            raise ValueError("D2 portfolio provenance completeness must be exact")
        validate_identifier(self.observation_version, "D2 portfolio observation version")
        observed_at = _utc(self.observed_at, "D2 portfolio observation time")
        if self.observed_at.tzinfo is not timezone.utc:
            raise ValueError("D2 portfolio observation time must use canonical UTC")
        oldest_at = None
        if self.oldest_position_observed_at is not None:
            oldest_at = _utc(
                self.oldest_position_observed_at,
                "D2 oldest position observation time",
            )
            if (
                self.oldest_position_observed_at.tzinfo is not timezone.utc
                or oldest_at > observed_at
            ):
                raise ValueError("D2 oldest position time is outside its observation")
        validate_request_id(self.current_request_id)
        if type(self.current_request_mode) is not RequestMode:
            raise ValueError("D2 current request mode must be exact")
        _utc(self.as_of, "D2 provenance as-of time")
        if self.as_of.tzinfo is not timezone.utc:
            raise ValueError("D2 provenance as-of time must use canonical UTC")
        if self.source_state == "same_request_success":
            if (
                self.source_request_id is None
                or self.source_request_mode is None
                or self.source_request_started_at is None
                or self.source_request_deadline_at is None
            ):
                raise ValueError("D2 same-request provenance is incomplete")
            validate_request_id(self.source_request_id)
            if type(self.source_request_mode) is not RequestMode:
                raise ValueError("D2 source request mode must be exact")
            started_at = _utc(self.source_request_started_at, "D2 source request start")
            deadline_at = _utc(self.source_request_deadline_at, "D2 source request deadline")
            expected_seconds = 90 if self.source_request_mode is RequestMode.RAPID else 480
            if (
                self.source_request_started_at.tzinfo is not timezone.utc
                or self.source_request_deadline_at.tzinfo is not timezone.utc
                or deadline_at - started_at != timedelta(seconds=expected_seconds)
            ):
                raise ValueError("D2 source request window is invalid")
        elif any(
            item is not None
            for item in (
                self.source_request_id,
                self.source_request_mode,
                self.source_request_started_at,
                self.source_request_deadline_at,
            )
        ):
            raise ValueError("D2 cached provenance cannot carry a source request window")

    def evidence_state(self) -> str:
        self.validate()
        as_of = self.as_of
        observed_at = self.observed_at
        if self.source_state == "unbound" or not self.snapshot_complete or observed_at > as_of:
            return "unknown"
        if self.source_state == "same_request_success":
            if (
                self.source_request_id != self.current_request_id
                or self.source_request_mode is not self.current_request_mode
                or not (
                    self.source_request_started_at
                    <= observed_at
                    <= as_of
                    <= self.source_request_deadline_at
                )
            ):
                return "unknown"
        age = as_of - observed_at
        if age > _DATED_FALLBACK_AGE:
            return "unknown"
        state = (
            "current"
            if self.source_state == "same_request_success" and age <= _CURRENT_AGE
            else "dated"
        )
        if self.oldest_position_observed_at is not None:
            if self.oldest_position_observed_at > as_of:
                return "unknown"
            oldest_age = as_of - self.oldest_position_observed_at
            if oldest_age > _DATED_FALLBACK_AGE:
                return "unknown"
            if oldest_age > _CURRENT_AGE:
                state = "dated"
        return state


def _provenance_mac(value: D2PortfolioProvenance) -> str:
    return hmac.new(
        _PROVENANCE_MAC_KEY,
        value.binding_bytes(),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class D2RelationshipSet:
    target_fund_code: str
    held_fund_codes: Tuple[str, ...]
    portfolio_evidence_state: str
    portfolio_provenance: D2PortfolioProvenance
    valuation_available: bool
    relationships: Tuple[RelationshipEvidence, ...]
    evidence_facts: Tuple[BriefFact, ...]
    coverage: BriefCoverage
    target_portfolio_weight: Optional[str]
    economic_exposure_weight: Optional[str]
    economic_exposure_hhi: Optional[str]
    largest_economic_exposure_weight: Optional[str]
    observed_at: Optional[datetime]
    position_present: Optional[bool]
    missing_fields: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    warnings: Tuple[str, ...]

    def validate(self) -> None:
        if type(self) is not D2RelationshipSet:
            raise ValueError("D2 relationship set subclasses are not accepted")
        validate_exact_dataclass_state(self, "D2 relationship set")
        _fund_code(self.target_fund_code, "D2 target fund code")
        if (
            type(self.held_fund_codes) is not tuple
            or tuple(sorted(set(self.held_fund_codes))) != self.held_fund_codes
        ):
            raise ValueError("D2 held fund codes must be a sorted unique exact tuple")
        for code in self.held_fund_codes:
            _fund_code(code, "D2 held fund code")
        if self.portfolio_evidence_state not in {"current", "dated", "unknown"}:
            raise ValueError("D2 portfolio evidence state is unsupported")
        if type(self.portfolio_provenance) is not D2PortfolioProvenance:
            raise ValueError("D2 portfolio provenance must use the exact type")
        self.portfolio_provenance.validate()
        if self.portfolio_evidence_state != self.portfolio_provenance.evidence_state():
            raise ValueError("D2 portfolio evidence state does not match its provenance")
        if self.held_fund_codes and self.portfolio_provenance.oldest_position_observed_at is None:
            raise ValueError("held D2 portfolio evidence requires its oldest observation")
        expected_observed_at = (
            None
            if self.portfolio_provenance.source_state == "unbound"
            else self.portfolio_provenance.observed_at
        )
        if self.observed_at != expected_observed_at:
            raise ValueError("D2 observation time does not match its provenance")
        if type(self.valuation_available) is not bool:
            raise ValueError("D2 valuation availability must be an exact boolean")
        if type(self.relationships) is not tuple or len(self.relationships) > MAX_RELATIONSHIPS:
            raise ValueError("D2 relationships exceed their bound")
        relationship_ids = []
        for relationship in self.relationships:
            if type(relationship) is not RelationshipEvidence:
                raise ValueError("D2 relationships require exact relationship records")
            relationship.validate()
            expected_keys = _RELATIONSHIP_METRIC_KEYS.get(relationship.relationship_type)
            if expected_keys is None or set(relationship.metrics) != expected_keys:
                raise ValueError("D2 relationship metrics do not match their exact type")
            if any(
                marker in key
                for key in relationship.metrics
                for marker in ("weight", "hhi", "amount", "ratio")
            ):
                raise ValueError("D2 relationship metrics contain a private derived ratio")
            _validate_relationship_semantics(relationship)
            relationship_ids.append(relationship.relationship_id)
        if len(relationship_ids) != len(set(relationship_ids)):
            raise ValueError("D2 relationship ids must be unique")
        if type(self.evidence_facts) is not tuple or len(self.evidence_facts) > MAX_FACTS:
            raise ValueError("D2 supporting facts must be a bounded exact tuple")
        evidence_fact_ids = []
        for fact in self.evidence_facts:
            if type(fact) is not BriefFact:
                raise ValueError("D2 supporting facts require exact BriefFact records")
            fact.validate()
            evidence_fact_ids.append(fact.fact_id)
        if len(evidence_fact_ids) != len(set(evidence_fact_ids)):
            raise ValueError("D2 supporting fact ids must be unique")
        available_evidence = set(evidence_fact_ids)
        if any(
            not set(relationship.evidence_ids).issubset(available_evidence)
            for relationship in self.relationships
        ):
            raise ValueError("D2 relationship evidence does not close over supporting facts")
        facts_by_id = {fact.fact_id: fact for fact in self.evidence_facts}
        expected_fields = {
            "share_class_sibling": "share_class_identity",
            "same_manager": "current_manager_team",
            "same_company": "identity_active_status",
            "same_current_benchmark": "current_benchmark",
        }
        for relationship in self.relationships:
            expected_field = expected_fields.get(relationship.relationship_type)
            if expected_field is None:
                continue
            supporting = tuple(facts_by_id[item] for item in relationship.evidence_ids)
            if any(fact.field_id != expected_field for fact in supporting):
                raise ValueError("D2 relationship evidence fields do not match its type")
            expected_state = _relationship_state(supporting)
            if relationship.relationship_type == "same_current_benchmark":
                expected_state = BriefEvidenceState.PARTIAL
            if relationship.evidence_state is not expected_state:
                raise ValueError("D2 relationship evidence state does not match its facts")
            scoped_by_id = {
                evidence_id: tuple(
                    code
                    for code in relationship.fund_codes
                    if evidence_id.startswith(f"fund_{code}_")
                )
                for evidence_id in relationship.evidence_ids
            }
            if any(len(subjects) > 1 for subjects in scoped_by_id.values()):
                raise ValueError("D2 relationship evidence subjects are ambiguous")
            scoped_subjects = {subjects[0] for subjects in scoped_by_id.values() if subjects}
            unscoped_ids = tuple(
                evidence_id for evidence_id, subjects in scoped_by_id.items() if not subjects
            )
            unscoped_subjects = tuple(sorted(set(relationship.fund_codes) - scoped_subjects))
            if len(unscoped_ids) != len(unscoped_subjects):
                raise ValueError("D2 relationship evidence subjects are ambiguous")
            subject_by_id = {
                evidence_id: subjects[0]
                for evidence_id, subjects in scoped_by_id.items()
                if subjects
            }
            subject_by_id.update(zip(sorted(unscoped_ids), unscoped_subjects))
            if set(subject_by_id.values()) != set(relationship.fund_codes):
                raise ValueError("D2 relationship evidence does not cover both subjects")
            if relationship.relationship_type == "share_class_sibling":
                for evidence_id, fact in zip(relationship.evidence_ids, supporting):
                    subject = subject_by_id[evidence_id]
                    expected_related = next(
                        code for code in relationship.fund_codes if code != subject
                    )
                    if _value(fact, "related_fund_code") != expected_related:
                        raise ValueError("D2 sibling evidence is not mutual")
            elif relationship.relationship_type == "same_manager":
                expected_name = relationship.metrics["shared_manager_name"]
                if any(_value(fact, "manager_name") != expected_name for fact in supporting):
                    raise ValueError("D2 manager evidence does not match its metric")
            elif relationship.relationship_type == "same_company":
                expected_company = relationship.metrics["company_name"]
                for evidence_id, fact in zip(relationship.evidence_ids, supporting):
                    if (
                        _value(fact, "fund_code") != subject_by_id[evidence_id]
                        or _value(fact, "fund_company") != expected_company
                    ):
                        raise ValueError("D2 company evidence does not match its metric")
            else:
                expected_benchmark = relationship.metrics["benchmark_description"]
                if any(_value(fact, "description") != expected_benchmark for fact in supporting):
                    raise ValueError("D2 benchmark evidence does not match its metric")
        if type(self.coverage) is not BriefCoverage:
            raise ValueError("D2 coverage must be an exact BriefCoverage")
        self.coverage.validate()
        if self.coverage.known_percent is not None:
            raise ValueError("Task 7 coverage cannot contain a synthesized coverage ratio")
        coverage_evidence = set(relationship_ids) | available_evidence
        if not set(self.coverage.evidence_ids).issubset(coverage_evidence):
            raise ValueError("D2 coverage evidence does not resolve")
        if set(self.coverage.included_fund_codes) | set(self.coverage.omitted_fund_codes) != set(
            self.held_fund_codes
        ):
            raise ValueError("D2 coverage does not partition the held fund codes")
        if set(self.coverage.evidence_ids) - available_evidence:
            raise ValueError("D2 coverage must resolve directly to supporting facts")
        coverage_by_subject: Dict[str, set[str]] = {
            code: set() for code in self.coverage.included_fund_codes
        }
        for evidence_id in self.coverage.evidence_ids:
            fact = facts_by_id[evidence_id]
            if fact.field_id == "identity_active_status":
                subject = _value(fact, "fund_code")
            else:
                subject = next(
                    (
                        code
                        for code in self.coverage.included_fund_codes
                        if evidence_id.startswith(f"fund_{code}_")
                    ),
                    self.target_fund_code,
                )
            if subject not in coverage_by_subject:
                raise ValueError("D2 coverage evidence references an unlisted subject")
            coverage_by_subject[subject].add(fact.field_id)
        required_coverage_fields = {
            "identity_active_status",
            "current_manager_team",
            "current_benchmark",
        }
        if any(fields != required_coverage_fields for fields in coverage_by_subject.values()):
            raise ValueError("D2 coverage evidence is incomplete for an included fund")
        coverage_facts = tuple(facts_by_id[item] for item in self.coverage.evidence_ids)
        if (
            self.portfolio_evidence_state == "unknown"
            or not self.valuation_available
            or not self.coverage.included_fund_codes
        ):
            expected_coverage_state = BriefEvidenceState.INSUFFICIENT
        elif (
            self.portfolio_evidence_state == "dated"
            or self.coverage.omitted_fund_codes
            or self.missing_fields
            or self.conflicts
            or self.warnings
            or any(
                fact.completeness is not EvidenceCompleteness.COMPLETE
                or fact.freshness is not EvidenceFreshness.CURRENT
                for fact in coverage_facts
            )
        ):
            expected_coverage_state = BriefEvidenceState.PARTIAL
        else:
            expected_coverage_state = BriefEvidenceState.COMPLETE
        if self.coverage.evidence_state is not expected_coverage_state:
            raise ValueError("D2 coverage state does not match its evidence")
        ratio_fields = (
            (self.target_portfolio_weight, "target portfolio weight"),
            (self.economic_exposure_weight, "economic exposure weight"),
            (self.economic_exposure_hhi, "economic exposure HHI"),
            (
                self.largest_economic_exposure_weight,
                "largest economic exposure weight",
            ),
        )
        for value, name in ratio_fields:
            _ratio(value, name)
        if self.portfolio_evidence_state == "unknown":
            if (
                self.valuation_available
                or self.held_fund_codes
                or self.relationships
                or self.evidence_facts
                or self.coverage.included_fund_codes
                or self.coverage.omitted_fund_codes
                or self.coverage.evidence_ids
                or self.position_present is not None
                or any(value is not None for value, _ in ratio_fields)
            ):
                raise ValueError("unknown D2 portfolio evidence must not retain conclusions")
        else:
            expected_presence = self.target_fund_code in self.held_fund_codes
            if self.position_present is not expected_presence:
                raise ValueError("D2 position presence does not match the held fund codes")
        if not self.valuation_available and any(value is not None for value, _ in ratio_fields):
            raise ValueError("unavailable D2 valuation must not retain derived ratios")
        if (
            self.portfolio_evidence_state != "unknown"
            and self.valuation_available
            and self.held_fund_codes
            and any(value is None for value, _ in ratio_fields)
        ):
            raise ValueError("available D2 valuation must provide every derived ratio")
        required_unknown_fields = {
            f"authenticated_index_identity_{code}" for code in self.held_fund_codes
        }
        for code in self.held_fund_codes:
            code_findings = tuple(
                item for item in self.missing_fields + self.conflicts if item.endswith(f"_{code}")
            )
            if any(
                item.startswith("identity_") or "identity_active_status" in item
                for item in code_findings
            ):
                required_unknown_fields.add(f"identity_active_status_{code}")
            if any("manager" in item for item in code_findings):
                required_unknown_fields.add(f"current_manager_team_{code}")
            if any("benchmark" in item for item in code_findings):
                required_unknown_fields.add(f"current_benchmark_{code}")
            if any("coverage_evidence_budget" in item for item in code_findings):
                required_unknown_fields.add(f"coverage_evidence_budget_{code}")
        if not required_unknown_fields.issubset(set(self.coverage.unknown_fields)):
            raise ValueError("D2 coverage unknown fields conceal required evidence gaps")
        for code in self.coverage.omitted_fund_codes:
            if not any(
                item.endswith(f"_{code}") and item != f"authenticated_index_identity_{code}"
                for item in self.coverage.unknown_fields
            ):
                raise ValueError("D2 omitted fund lacks its coverage gap")
        if self.observed_at is not None:
            if (
                type(self.observed_at) is not datetime
                or self.observed_at.tzinfo is not timezone.utc
            ):
                raise ValueError("D2 observation time must use canonical UTC or None")
        if self.position_present is not None and type(self.position_present) is not bool:
            raise ValueError("D2 position presence must be an exact boolean or None")
        for values, name in (
            (self.missing_fields, "D2 missing fields"),
            (self.conflicts, "D2 conflicts"),
            (self.warnings, "D2 warnings"),
        ):
            validate_identifier_tuple(values, name)


def _scoped_fact_id(fund_code: str, local_id: str) -> str:
    candidate = f"fund_{fund_code}_{local_id}"
    if len(candidate) <= 64:
        validate_identifier(candidate, "scoped D2 fact id")
        return candidate
    digest = hashlib.sha256(local_id.encode()).hexdigest()[:20]
    candidate = f"fund_{fund_code}_{digest}"
    validate_identifier(candidate, "scoped D2 fact id")
    return candidate


def _field_facts(facts: Tuple[BriefFact, ...], field_id: str) -> Tuple[BriefFact, ...]:
    return tuple(
        sorted(
            (item for item in facts if item.field_id == field_id),
            key=lambda item: item.fact_id,
        )
    )


def _value(fact: BriefFact, key: str) -> object:
    if not isinstance(fact.value, Mapping):
        return None
    return fact.value.get(key)


def _publication_times(facts: Tuple[BriefFact, ...]) -> Tuple[datetime, ...]:
    return tuple(sorted({item.published_at for item in facts if item.published_at is not None}))


def _relationship_state(facts: Tuple[BriefFact, ...]) -> BriefEvidenceState:
    if all(
        item.completeness is EvidenceCompleteness.COMPLETE
        and item.freshness is EvidenceFreshness.CURRENT
        for item in facts
    ):
        return BriefEvidenceState.COMPLETE
    return BriefEvidenceState.PARTIAL


def build_d2_relationships(
    target_fund_code: str,
    portfolio: PortfolioEvidenceBinding,
    facts_by_fund: Mapping[str, SourceLinkedFactSet],
    as_of: datetime,
    *,
    request_id: str,
    request_mode: RequestMode,
) -> D2RelationshipSet:
    target_fund_code = _fund_code(target_fund_code, "target fund code")
    validate_request_id(request_id)
    if type(request_mode) is not RequestMode:
        raise ValueError("D2 request mode must be an exact RequestMode")
    if type(portfolio) is not PortfolioEvidenceBinding:
        raise ValueError("D2 requires an exact PortfolioEvidenceBinding")
    portfolio.validate()
    if not isinstance(facts_by_fund, Mapping):
        raise ValueError("D2 facts must be a fund-code mapping")
    as_of = _utc(as_of, "D2 as-of time")

    missing: set[str] = set()
    conflicts: set[str] = set()
    warnings: set[str] = set()
    facts: Dict[str, Tuple[BriefFact, ...]] = {}
    for key in sorted(facts_by_fund):
        _fund_code(key, "D2 fact mapping key")
        fact_set = facts_by_fund[key]
        if type(fact_set) is not SourceLinkedFactSet or fact_set.fund_code != key:
            raise ValueError("D2 fact mapping key must equal its source-linked fund code")
        local_ids = [item.fact_id for item in fact_set.facts]
        if len(local_ids) != len(set(local_ids)):
            conflicts.add(f"d2_fact_id_duplicate_{key}")
        try:
            fact_set.validate()
        except ValueError:
            conflicts.add(f"d2_fact_set_invalid_{key}")
            facts[key] = ()
            continue
        facts[key] = tuple(sorted(fact_set.facts, key=lambda item: (item.field_id, item.fact_id)))

    positive_positions = tuple(item for item in portfolio.positions if item.shares > 0)
    observed_at = _utc(portfolio.observed_at, "portfolio observation time")
    oldest_position_observed_at = (
        None
        if not positive_positions
        else min(_utc(item.observed_at, "position observation time") for item in positive_positions)
    )
    portfolio_provenance = D2PortfolioProvenance(
        source_state=portfolio.source_state,
        snapshot_complete=portfolio.snapshot_complete,
        observation_version=portfolio.observation_version,
        observed_at=observed_at,
        oldest_position_observed_at=oldest_position_observed_at,
        source_request_id=portfolio.request_id,
        source_request_mode=portfolio.request_mode,
        source_request_started_at=portfolio.request_started_at,
        source_request_deadline_at=portfolio.request_deadline_at,
        current_request_id=request_id,
        current_request_mode=request_mode,
        as_of=as_of,
        binding_mac="0" * 64,
    )
    portfolio_provenance = replace(
        portfolio_provenance,
        binding_mac=_provenance_mac(portfolio_provenance),
    )
    portfolio_provenance.validate()
    portfolio_state = "current"
    portfolio_quality_partial = False
    if portfolio.source_state == "unbound" or not portfolio.snapshot_complete:
        portfolio_state = "unknown"
        missing.add("personal_position_observation")
    elif observed_at > as_of:
        portfolio_state = "unknown"
        conflicts.add("portfolio_observation_future")
    elif portfolio.source_state == "same_request_success" and (
        portfolio.request_id != request_id
        or portfolio.request_mode is not request_mode
        or not (
            _utc(portfolio.request_started_at, "portfolio request start")
            <= observed_at
            <= as_of
            <= _utc(portfolio.request_deadline_at, "portfolio request deadline")
        )
    ):
        portfolio_state = "unknown"
        conflicts.add("portfolio_request_binding_invalid")
    else:
        age = as_of - observed_at
        if age <= _CURRENT_AGE and portfolio.source_state == "same_request_success":
            portfolio_state = "current"
        elif age <= _DATED_FALLBACK_AGE:
            portfolio_state = "dated"
            portfolio_quality_partial = True
            warnings.add(
                "portfolio_observation_cached"
                if age <= _CURRENT_AGE
                else "portfolio_observation_dated"
            )
        else:
            portfolio_state = "unknown"
            missing.add("personal_position_observation")

    if portfolio_state != "unknown" and positive_positions:
        position_times = tuple(
            _utc(item.observed_at, "position observation time") for item in positive_positions
        )
        if any(item > as_of for item in position_times):
            portfolio_state = "unknown"
            conflicts.add("position_observation_future")
        else:
            oldest_age = as_of - min(position_times)
            if oldest_age > _DATED_FALLBACK_AGE:
                portfolio_state = "unknown"
                missing.add("position_observation_stale")
            elif oldest_age > _CURRENT_AGE:
                portfolio_state = "dated"
                portfolio_quality_partial = True
                warnings.add("position_observation_dated")

    if portfolio_state != portfolio_provenance.evidence_state():
        raise ValueError("D2 portfolio state derivation drifted from its provenance")

    held_codes = (
        ()
        if portfolio_state == "unknown"
        else tuple(sorted({item.fund_code for item in positive_positions}))
    )
    comparison_codes = tuple(sorted(set(held_codes) | {target_fund_code}))

    valid_fields: Dict[str, Dict[str, Tuple[BriefFact, ...]]] = {}
    companies: Dict[str, str] = {}
    managers: Dict[str, Tuple[str, ...]] = {}
    benchmarks: Dict[str, str] = {}
    sibling_refs: Dict[str, Dict[str, BriefFact]] = {}
    included_codes: set[str] = set()
    omitted_codes: set[str] = set()
    coverage_unknowns: set[str] = set()

    for code in comparison_codes:
        code_facts = facts.get(code, ())
        valid_fields[code] = {}
        missing.add(f"authenticated_index_identity_{code}")
        coverage_unknowns.add(f"authenticated_index_identity_{code}")
        reliable_count = 0
        for field_id, missing_prefix in (
            ("identity_active_status", "identity_evidence_missing"),
            ("current_manager_team", "manager_evidence_missing"),
            ("current_benchmark", "current_benchmark"),
        ):
            selected = _field_facts(code_facts, field_id)
            if not selected:
                missing.add(f"{missing_prefix}_{code}")
                coverage_unknowns.add(f"{field_id}_{code}")
                continue
            if any(item.retrieved_at > as_of for item in selected):
                conflicts.add(f"{field_id}_evidence_future_{code}")
                coverage_unknowns.add(f"{field_id}_{code}")
                continue
            current = tuple(
                item
                for item in selected
                if item.freshness not in {EvidenceFreshness.STALE, EvidenceFreshness.UNKNOWN}
            )
            if not current:
                missing.add(f"{field_id}_evidence_stale_{code}")
                coverage_unknowns.add(f"{field_id}_{code}")
                continue
            selected = current
            if any(item.conflict_ids for item in selected):
                conflicts.add(f"{field_id}_evidence_conflict_{code}")
                coverage_unknowns.add(f"{field_id}_{code}")
                continue
            valid_fields[code][field_id] = selected

        identity = valid_fields[code].get("identity_active_status", ())
        identity_subjects = {_value(item, "fund_code") for item in identity}
        subject_invalid = False
        if identity and identity_subjects != {code}:
            conflicts.add(f"identity_subject_conflict_{code}")
            coverage_unknowns.add(f"identity_active_status_{code}")
            identity = ()
            subject_invalid = True
        company_values = {
            value
            for item in identity
            if type(value := _value(item, "fund_company")) is str and value
        }
        if len(company_values) == 1 and identity:
            companies[code] = next(iter(company_values))
            reliable_count += 1
        elif identity:
            conflicts.add(f"identity_evidence_conflict_{code}")
            coverage_unknowns.add(f"identity_active_status_{code}")

        if subject_invalid:
            coverage_unknowns.update(
                {
                    f"current_manager_team_{code}",
                    f"current_benchmark_{code}",
                    f"share_class_identity_{code}",
                }
            )
            sibling_refs[code] = {}
            omitted_codes.add(code)
            continue

        manager_facts = valid_fields[code].get("current_manager_team", ())
        manager_values = tuple(
            sorted(
                {
                    value
                    for item in manager_facts
                    if type(value := _value(item, "manager_name")) is str and value
                }
            )
        )
        if manager_values and manager_facts:
            managers[code] = manager_values
            reliable_count += 1
        elif manager_facts:
            conflicts.add(f"manager_evidence_conflict_{code}")
            coverage_unknowns.add(f"current_manager_team_{code}")

        benchmark_facts = valid_fields[code].get("current_benchmark", ())
        active: List[BriefFact] = []
        malformed = False
        for item in benchmark_facts:
            description = _value(item, "description")
            effective_from = _value(item, "effective_from")
            effective_to = _value(item, "effective_to")
            try:
                starts = None if effective_from is None else date.fromisoformat(str(effective_from))
                ends = None if effective_to is None else date.fromisoformat(str(effective_to))
            except ValueError:
                malformed = True
                continue
            if type(description) is not str or not description:
                malformed = True
                continue
            if (starts is None or starts <= as_of.date()) and (
                ends is None or ends >= as_of.date()
            ):
                active.append(item)
        descriptions = {str(_value(item, "description")) for item in active}
        if malformed or len(descriptions) > 1:
            conflicts.add(f"benchmark_effective_date_conflict_{code}")
            coverage_unknowns.add(f"current_benchmark_{code}")
        elif len(descriptions) == 1:
            benchmarks[code] = next(iter(descriptions))
            valid_fields[code]["current_benchmark"] = tuple(active)
            reliable_count += 1
        elif benchmark_facts:
            missing.add(f"current_benchmark_{code}")
            coverage_unknowns.add(f"current_benchmark_{code}")

        refs: Dict[str, BriefFact] = {}
        for item in _field_facts(code_facts, "share_class_identity"):
            if item.retrieved_at > as_of:
                conflicts.add(f"share_class_evidence_future_{code}")
                continue
            if item.freshness in {EvidenceFreshness.STALE, EvidenceFreshness.UNKNOWN}:
                missing.add(f"share_class_evidence_stale_{code}")
                continue
            if item.conflict_ids:
                conflicts.add(f"share_class_evidence_conflict_{code}")
                continue
            related = _value(item, "related_fund_code")
            if type(related) is str and _FUND_CODE.fullmatch(related) and related != code:
                refs.setdefault(related, item)
        sibling_refs[code] = refs
        if reliable_count == 3:
            included_codes.add(code)
        else:
            omitted_codes.add(code)

    parent = {code: code for code in comparison_codes}

    def find(code: str) -> str:
        while parent[code] != code:
            parent[code] = parent[parent[code]]
            code = parent[code]
        return code

    def union(first: str, second: str) -> None:
        left, right = find(first), find(second)
        if left != right:
            parent[max(left, right)] = min(left, right)

    relationships: List[RelationshipEvidence] = []
    projected: Dict[Tuple[str, str], BriefFact] = {}
    target_fact_count = len({item.fact_id for item in facts.get(target_fund_code, ())})
    candidate_fact_budget = max(0, MAX_FACTS - target_fact_count)

    def candidate_projection_fits(
        supporting: Tuple[Tuple[str, BriefFact], ...],
    ) -> bool:
        new_candidate_keys = {
            (code, fact.fact_id)
            for code, fact in supporting
            if code != target_fund_code and (code, fact.fact_id) not in projected
        }
        projected_candidate_count = sum(1 for code, _ in projected if code != target_fund_code)
        return projected_candidate_count + len(new_candidate_keys) <= candidate_fact_budget

    def evidence_id(code: str, fact: BriefFact) -> str:
        key = (code, fact.fact_id)
        if key not in projected:
            scoped = (
                fact.fact_id if code == target_fund_code else _scoped_fact_id(code, fact.fact_id)
            )
            projected[key] = replace(fact, fact_id=scoped)
        return projected[key].fact_id

    included_codes.intersection_update(held_codes)
    omitted_codes = set(held_codes) - included_codes
    coverage_unknowns = {
        item for item in coverage_unknowns if any(item.endswith(f"_{code}") for code in held_codes)
    }
    coverage_fact_ids: set[str] = set()
    for code in tuple(sorted(included_codes)):
        coverage_support = (
            (code, valid_fields[code]["identity_active_status"][0]),
            (code, valid_fields[code]["current_manager_team"][0]),
            (code, valid_fields[code]["current_benchmark"][0]),
        )
        if not candidate_projection_fits(coverage_support):
            included_codes.remove(code)
            omitted_codes.add(code)
            coverage_unknowns.add(f"coverage_evidence_budget_{code}")
            warnings.add("d2_fact_budget_reached")
            continue
        if len(coverage_fact_ids) + len(coverage_support) > MAX_TUPLE_ITEMS:
            included_codes.remove(code)
            omitted_codes.add(code)
            coverage_unknowns.add(f"coverage_evidence_budget_{code}")
            warnings.add("coverage_evidence_limit_reached")
            continue
        projected_ids = {evidence_id(subject_code, fact) for subject_code, fact in coverage_support}
        coverage_fact_ids.update(projected_ids)

    def add_relationship(
        relationship_type: str,
        first: str,
        second: str,
        supporting: Tuple[Tuple[str, BriefFact], ...],
        metrics: Mapping[str, object],
        *,
        force_partial: bool = False,
        relationship_warnings: Tuple[str, ...] = (),
    ) -> Optional[RelationshipEvidence]:
        if len(relationships) >= MAX_RELATIONSHIPS:
            warnings.add("relationship_limit_reached")
            return None
        if not candidate_projection_fits(supporting):
            warnings.add("d2_fact_budget_reached")
            return None
        codes = tuple(sorted((first, second)))
        support_facts = tuple(item for _, item in supporting)
        state = BriefEvidenceState.PARTIAL if force_partial else _relationship_state(support_facts)
        relationship = RelationshipEvidence(
            relationship_id=f"{relationship_type}_{codes[0]}_{codes[1]}",
            relationship_type=relationship_type,
            fund_codes=codes,
            evidence_state=state,
            metrics=dict(metrics),
            evidence_ids=tuple(evidence_id(code, fact) for code, fact in supporting),
            report_periods=(),
            publication_times=_publication_times(support_facts),
            warnings=relationship_warnings,
        )
        relationship.validate()
        relationships.append(relationship)
        return relationship

    duplicate_counts: Dict[str, int] = {}
    for position in positive_positions:
        duplicate_counts[position.fund_code] = duplicate_counts.get(position.fund_code, 0) + 1
    if portfolio_state in {"current", "dated"}:
        for code, count in sorted(duplicate_counts.items()):
            if count <= 1 or len(relationships) >= MAX_RELATIONSHIPS:
                continue
            relationship = RelationshipEvidence(
                relationship_id=f"duplicate_holding_identity_{code}",
                relationship_type="duplicate_holding_identity",
                fund_codes=(code,),
                evidence_state=(
                    BriefEvidenceState.COMPLETE
                    if portfolio_state == "current"
                    else BriefEvidenceState.PARTIAL
                ),
                metrics={"multiple_observations": True},
                evidence_ids=(),
                report_periods=(),
                publication_times=(),
                warnings=(),
            )
            relationship.validate()
            relationships.append(relationship)

    for index, first in enumerate(comparison_codes):
        for second in comparison_codes[index + 1 :]:
            first_fact = sibling_refs.get(first, {}).get(second)
            second_fact = sibling_refs.get(second, {}).get(first)
            if first_fact is not None and second_fact is not None:
                sibling_state = _relationship_state((first_fact, second_fact))
                aggregation_eligible = sibling_state is BriefEvidenceState.COMPLETE
                relationship = add_relationship(
                    "share_class_sibling",
                    first,
                    second,
                    ((first, first_fact), (second, second_fact)),
                    {
                        "mutual_source_links": True,
                        "aggregation_eligible": aggregation_eligible,
                    },
                    relationship_warnings=(
                        () if aggregation_eligible else ("sibling_evidence_not_complete_current",)
                    ),
                )
                if relationship is not None and aggregation_eligible:
                    union(first, second)
                elif target_fund_code in {first, second}:
                    other = second if first == target_fund_code else first
                    missing.add(f"share_class_sibling_not_authenticated_{other}")
            elif first_fact is not None or second_fact is not None:
                other = second if first == target_fund_code else first
                if target_fund_code in {first, second}:
                    missing.add(f"share_class_sibling_unconfirmed_{other}")

    target_manager_facts = valid_fields.get(target_fund_code, {}).get("current_manager_team", ())
    target_identity_facts = valid_fields.get(target_fund_code, {}).get("identity_active_status", ())
    target_benchmark_facts = valid_fields.get(target_fund_code, {}).get("current_benchmark", ())
    for candidate in sorted(code for code in held_codes if code != target_fund_code):
        shared_managers = tuple(
            sorted(set(managers.get(target_fund_code, ())) & set(managers.get(candidate, ())))
        )
        if shared_managers:
            shared_manager = shared_managers[0]
            target_manager = next(
                item
                for item in target_manager_facts
                if _value(item, "manager_name") == shared_manager
            )
            candidate_facts = valid_fields[candidate]["current_manager_team"]
            candidate_manager = next(
                item for item in candidate_facts if _value(item, "manager_name") == shared_manager
            )
            add_relationship(
                "same_manager",
                target_fund_code,
                candidate,
                ((target_fund_code, target_manager), (candidate, candidate_manager)),
                {"shared_manager_name": shared_manager},
            )
        if (
            target_fund_code in companies
            and candidate in companies
            and companies[target_fund_code] == companies[candidate]
        ):
            add_relationship(
                "same_company",
                target_fund_code,
                candidate,
                (
                    (target_fund_code, target_identity_facts[0]),
                    (candidate, valid_fields[candidate]["identity_active_status"][0]),
                ),
                {"company_name": companies[target_fund_code]},
            )
        if (
            target_fund_code in benchmarks
            and candidate in benchmarks
            and benchmarks[target_fund_code] == benchmarks[candidate]
        ):
            add_relationship(
                "same_current_benchmark",
                target_fund_code,
                candidate,
                (
                    (target_fund_code, target_benchmark_facts[0]),
                    (candidate, valid_fields[candidate]["current_benchmark"][0]),
                ),
                {
                    "benchmark_description": benchmarks[target_fund_code],
                    "exact_text_match": True,
                },
                force_partial=True,
                relationship_warnings=("benchmark_text_is_not_index_identity",),
            )

    position_present: Optional[bool] = None
    target_weight: Optional[str] = None
    economic_weight: Optional[str] = None
    economic_hhi: Optional[str] = None
    largest_economic_weight: Optional[str] = None
    valuation_available = True
    if portfolio_state in {"current", "dated"}:
        position_present = any(item.fund_code == target_fund_code for item in positive_positions)
        if any(
            item.formal_nav is None and item.estimated_nav is None for item in positive_positions
        ):
            missing.add("portfolio_nav_missing")
            valuation_available = False
        elif positive_positions:
            if any(
                item.formal_nav is None and item.estimated_nav is not None
                for item in positive_positions
            ):
                portfolio_quality_partial = True
                warnings.add("portfolio_estimated_nav_used")
            analysis = analyze_portfolio(positive_positions)
            if analysis.evidence_level != "deterministic_calculation" or not analysis.weights:
                missing.add("portfolio_nav_missing")
                valuation_available = False
            else:
                target_weight = canonical_decimal(
                    analysis.weights.get(target_fund_code, Decimal("0"))
                )
                economic_weights: Dict[str, Decimal] = {}
                for code, weight in analysis.weights.items():
                    root = find(code) if code in parent else code
                    economic_weights[root] = economic_weights.get(root, Decimal("0")) + weight
                target_root = (
                    find(target_fund_code) if target_fund_code in parent else target_fund_code
                )
                economic_weight = canonical_decimal(economic_weights.get(target_root, Decimal("0")))
                economic_hhi = canonical_decimal(
                    sum((item * item for item in economic_weights.values()), Decimal("0"))
                )
                largest_economic_weight = canonical_decimal(max(economic_weights.values()))

    if portfolio_state == "unknown":
        valuation_available = False
        position_present = None
        target_weight = None
        economic_weight = None
        economic_hhi = None
        largest_economic_weight = None

    if portfolio_state == "unknown" or not valuation_available or not included_codes:
        coverage_state = BriefEvidenceState.INSUFFICIENT
    elif (
        portfolio_state == "dated"
        or portfolio_quality_partial
        or omitted_codes
        or conflicts
        or missing
    ):
        coverage_state = BriefEvidenceState.PARTIAL
    else:
        coverage_state = BriefEvidenceState.COMPLETE
    coverage = BriefCoverage(
        coverage_id="d2_minimum_relationship_coverage",
        scope="current_fund_portfolio",
        evidence_state=coverage_state,
        included_fund_codes=tuple(sorted(included_codes)),
        omitted_fund_codes=tuple(sorted(omitted_codes)),
        known_percent=None,
        unknown_fields=tuple(sorted(coverage_unknowns)),
        evidence_ids=tuple(sorted(coverage_fact_ids)),
    )
    coverage.validate()
    result = D2RelationshipSet(
        target_fund_code=target_fund_code,
        held_fund_codes=held_codes,
        portfolio_evidence_state=portfolio_state,
        portfolio_provenance=portfolio_provenance,
        valuation_available=valuation_available,
        relationships=tuple(sorted(relationships, key=lambda item: item.relationship_id)),
        evidence_facts=tuple(sorted(projected.values(), key=lambda item: item.fact_id)),
        coverage=coverage,
        target_portfolio_weight=target_weight,
        economic_exposure_weight=economic_weight,
        economic_exposure_hhi=economic_hhi,
        largest_economic_exposure_weight=largest_economic_weight,
        observed_at=(observed_at if portfolio.source_state != "unbound" else None),
        position_present=position_present,
        missing_fields=tuple(sorted(missing)),
        conflicts=tuple(sorted(conflicts)),
        warnings=tuple(sorted(warnings)),
    )
    result.validate()
    return result
