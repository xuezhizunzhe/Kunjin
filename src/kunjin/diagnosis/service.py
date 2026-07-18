from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, Mapping, Optional

from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.brief.d2 import PortfolioEvidenceBinding, build_d2_relationships
from kunjin.brief.facts import SourceLinkedFactSet, build_source_linked_facts
from kunjin.brief.models import BriefCoverage, RelationshipEvidence
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import RequestMode
from kunjin.diagnosis.models import (
    DiagnosisCoverage,
    DiagnosisFinding,
    DiagnosisRelationship,
    PortfolioDiagnosis,
)
from kunjin.funds.peers.research import build_portfolio_overlap_report
from kunjin.funds.store import FundDisclosureStore
from kunjin.storage.repository import Repository

_FUND_CODE = re.compile(r"^[0-9]{6}$", flags=re.ASCII)
_ACTION_IDS = ("fact_research", "continue_holding")
_RELATIONSHIP_FINDINGS = {
    "disclosed_overlap": "disclosed_security_duplication",
    "same_current_benchmark": "same_current_benchmark_text",
    "same_manager": "same_current_manager",
    "share_class_sibling": "same_share_class_family",
    "top10_disclosed_overlap": "disclosed_security_duplication",
}


def _utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stable_code(prefix: str, value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"value_{normalized}"
    return f"{prefix}_{normalized}"[:128].rstrip("_")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _freeze(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("diagnosis fingerprint cannot contain non-finite Decimal")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _empty_coverage(scope: str) -> DiagnosisCoverage:
    result = DiagnosisCoverage(
        scope=scope,
        evidence_state="insufficient_data",
        included_fund_codes=(),
        omitted_fund_codes=(),
        unknown_fields=(),
        known_weight=Decimal("0"),
    )
    result.validate()
    return result


def _coverage_state(value: str) -> str:
    return "insufficient_data" if value == "insufficient" else value


def _coverage(
    source: BriefCoverage,
    weights: Mapping[str, Decimal],
) -> DiagnosisCoverage:
    known_weight = (
        sum(
            (weights[code] for code in source.included_fund_codes if code in weights),
            Decimal("0"),
        )
        if weights
        else None
    )
    result = DiagnosisCoverage(
        scope=source.scope,
        evidence_state=_coverage_state(source.evidence_state.value),
        included_fund_codes=tuple(sorted(source.included_fund_codes)),
        omitted_fund_codes=tuple(sorted(source.omitted_fund_codes)),
        unknown_fields=tuple(sorted(source.unknown_fields)),
        known_weight=known_weight,
    )
    result.validate()
    return result


def _relationship(source: RelationshipEvidence) -> DiagnosisRelationship:
    payload = source.to_canonical_dict()
    metrics = payload["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("D2 relationship metrics are not a mapping")
    result = DiagnosisRelationship(
        relationship_id=source.relationship_id,
        relationship_type=source.relationship_type,
        fund_codes=tuple(sorted(source.fund_codes)),
        evidence_state=_coverage_state(source.evidence_state.value),
        metrics=tuple((key, _freeze(value)) for key, value in sorted(metrics.items())),
        report_periods=tuple(sorted(set(source.report_periods))),
        publication_times=tuple(sorted(set(source.publication_times))),
        warnings=tuple(sorted(set(source.warnings))),
    )
    return result


def _overlap_is_positive(relationship: DiagnosisRelationship) -> bool:
    if relationship.relationship_type not in {
        "disclosed_overlap",
        "top10_disclosed_overlap",
    }:
        return True
    metrics = dict(relationship.metrics)
    try:
        return Decimal(str(metrics["overlap_percent"])) > 0
    except (KeyError, ValueError):
        return False


def _finding_for_relationship(
    relationship: DiagnosisRelationship,
) -> Optional[DiagnosisFinding]:
    finding_type = _RELATIONSHIP_FINDINGS.get(relationship.relationship_type)
    if finding_type is None or not _overlap_is_positive(relationship):
        return None
    result = DiagnosisFinding(
        finding_id=f"finding_{relationship.relationship_id}",
        finding_type=finding_type,
        severity="attention",
        fund_codes=relationship.fund_codes,
        relationship_ids=(relationship.relationship_id,),
        evidence_scope=(
            "disclosed_holdings"
            if "overlap" in relationship.relationship_type
            else "authenticated_product_relationship"
        ),
    )
    result.validate()
    return result


class DiagnosisService:
    def __init__(
        self,
        repository: Repository,
        disclosure_store: FundDisclosureStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(repository) is not Repository:
            raise ValueError("diagnosis requires an exact Repository")
        if type(disclosure_store) is not FundDisclosureStore:
            raise ValueError("diagnosis requires an exact FundDisclosureStore")
        if disclosure_store.repository is not repository:
            raise ValueError("diagnosis stores must share one Repository")
        if not callable(clock):
            raise ValueError("diagnosis clock must be callable")
        self._repository = repository
        self._disclosure_store = disclosure_store
        self._clock = clock

    def diagnose(self, candidate_fund_code: Optional[str] = None) -> PortfolioDiagnosis:
        if candidate_fund_code is not None:
            if (
                type(candidate_fund_code) is not str
                or _FUND_CODE.fullmatch(candidate_fund_code) is None
                or candidate_fund_code == "000000"
            ):
                raise ValueError("candidate fund code must be six digits and non-reserved")
            raise NotImplementedError("candidate impact is implemented in Phase 3 Task 3")
        as_of = _utc(self._clock(), "diagnosis clock")
        positions = tuple(self._repository.latest_positions())
        held_codes = tuple(sorted({item.fund_code for item in positions if item.shares > 0}))
        if not held_codes:
            result = PortfolioDiagnosis(
                as_of=as_of,
                value_basis="missing",
                position_count=0,
                hhi=None,
                largest_position_share=None,
                relationship_coverage=_empty_coverage("minimum_relationship_coverage"),
                holdings_coverage=_empty_coverage("disclosed_holdings_overlap"),
                relationships=(),
                candidate_impact=None,
                findings=(),
                missing_evidence=("no_portfolio_positions",),
                conflicts=(),
                warnings=(),
                input_fingerprint=_fingerprint(
                    {"as_of": as_of, "held_fund_codes": (), "value_basis": "missing"}
                ),
            )
            result.validate()
            return result

        analysis = analyze_portfolio(positions)
        sync = self._repository.latest_successful_sync("yangjibao")
        observed_at = max(_utc(item.observed_at, "position observation") for item in positions)
        authenticated = sync is not None and sync.get("id") is not None
        binding = PortfolioEvidenceBinding(
            positions=positions,
            snapshot_complete=authenticated,
            observation_version=(
                f"sync_run_{int(sync['id'])}" if authenticated else "portfolio_unavailable"
            ),
            observed_at=observed_at,
            source_state="authenticated_cache" if authenticated else "unbound",
            request_id=None,
            request_mode=None,
            request_started_at=None,
            request_deadline_at=None,
        )
        binding.validate()

        bundles = {
            code: self._disclosure_store.load_bundle(code) for code in held_codes
        }
        fact_sets: Dict[str, SourceLinkedFactSet] = {}
        projection_failures = []
        for code in held_codes:
            try:
                fact_sets[code] = build_source_linked_facts(
                    bundles[code],
                    as_of,
                    action_ids=_ACTION_IDS,
                )
            except (KeyError, TypeError, ValueError):
                projection_failures.append(f"fact_projection_failed_{code}")
                fallback = SourceLinkedFactSet(
                    code,
                    (),
                    (),
                    ("diagnosis_fact_projection",),
                    (),
                    (),
                )
                fallback.validate()
                fact_sets[code] = fallback

        budget = RequestBudget.create(RequestMode.RAPID, wall_clock=lambda: as_of)
        d2_results = [
            build_d2_relationships(
                code,
                binding,
                fact_sets,
                as_of,
                request_id=budget.request_id,
                request_mode=RequestMode.RAPID,
            )
            for code in held_codes
        ]
        relationship_by_id: Dict[str, DiagnosisRelationship] = {}
        for d2 in d2_results:
            d2.validate()
            for source in d2.relationships:
                projected = _relationship(source)
                previous = relationship_by_id.setdefault(
                    projected.relationship_id,
                    projected,
                )
                if previous != projected:
                    raise ValueError("D2 relationship identity drifted across held funds")
        relationships = tuple(
            sorted(relationship_by_id.values(), key=lambda item: item.relationship_id)
        )

        overlap_report = build_portfolio_overlap_report(bundles, positions, as_of)
        primary = d2_results[0]
        relationship_coverage = _coverage(primary.coverage, analysis.weights)
        holdings_coverage = _coverage(primary.holdings_coverage, analysis.weights)

        findings = []
        if analysis.hhi is not None:
            findings.append(
                DiagnosisFinding(
                    finding_id="finding_portfolio_hhi",
                    finding_type="portfolio_hhi_observation",
                    severity="information",
                    fund_codes=held_codes,
                    relationship_ids=(),
                    evidence_scope="current_portfolio_weights",
                )
            )
        if analysis.largest_position_share is not None:
            findings.append(
                DiagnosisFinding(
                    finding_id="finding_largest_position",
                    finding_type="largest_position_concentration",
                    severity="information",
                    fund_codes=held_codes,
                    relationship_ids=(),
                    evidence_scope="current_portfolio_weights",
                )
            )
        findings.extend(
            finding
            for relationship in relationships
            if (finding := _finding_for_relationship(relationship)) is not None
        )

        missing = set(projection_failures)
        conflicts = set()
        warnings = set()
        for d2 in d2_results:
            missing.update(d2.missing_fields)
            conflicts.update(d2.conflicts)
            warnings.update(d2.warnings)
        if not analysis.weights:
            missing.add("portfolio_valuation_unavailable")
        overlap_data_gaps = overlap_report.get("data_gaps", [])
        if isinstance(overlap_data_gaps, list):
            for item in overlap_data_gaps:
                if isinstance(item, str):
                    missing.add(_stable_code("overlap", item))
        if (
            missing
            or relationship_coverage.omitted_fund_codes
            or holdings_coverage.omitted_fund_codes
        ):
            findings.append(
                DiagnosisFinding(
                    finding_id="finding_coverage_gap",
                    finding_type="coverage_gap",
                    severity="insufficient_data",
                    fund_codes=held_codes,
                    relationship_ids=(),
                    evidence_scope="diagnosis_coverage",
                )
            )
        for finding in findings:
            finding.validate()
        findings_tuple = tuple(sorted(findings, key=lambda item: item.finding_id))
        value_basis = analysis.value_kind if analysis.total_value is not None else "missing"
        fingerprint_payload = {
            "as_of": as_of,
            "held_fund_codes": held_codes,
            "holdings_coverage": holdings_coverage.__dict__,
            "relationship_coverage": relationship_coverage.__dict__,
            "relationships": [
                {
                    "fund_codes": item.fund_codes,
                    "metrics": item.metrics,
                    "relationship_id": item.relationship_id,
                    "relationship_type": item.relationship_type,
                    "report_periods": item.report_periods,
                }
                for item in relationships
            ],
            "value_basis": value_basis,
        }
        result = PortfolioDiagnosis(
            as_of=as_of,
            value_basis=value_basis,
            position_count=len(held_codes),
            hhi=analysis.hhi,
            largest_position_share=analysis.largest_position_share,
            relationship_coverage=relationship_coverage,
            holdings_coverage=holdings_coverage,
            relationships=relationships,
            candidate_impact=None,
            findings=findings_tuple,
            missing_evidence=tuple(sorted(missing)),
            conflicts=tuple(sorted(conflicts)),
            warnings=tuple(sorted(warnings)),
            input_fingerprint=_fingerprint(fingerprint_payload),
        )
        result.validate()
        return result
