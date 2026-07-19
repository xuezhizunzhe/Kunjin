from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from itertools import combinations
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from kunjin.brief.d2 import PortfolioEvidenceBinding, build_d2_relationships
from kunjin.brief.facts import SourceLinkedFactSet, build_source_linked_facts
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import RequestMode, canonical_json_bytes
from kunjin.diagnosis import (
    build_authenticated_portfolio_binding,
    project_candidate_impact,
    project_diagnosis_relationship,
)
from kunjin.diagnosis.models import CandidateImpact, DiagnosisRelationship
from kunjin.funds.models import DisclosureBundle
from kunjin.funds.peers.analytics import PEER_CALCULATION_VERSION
from kunjin.funds.peers.classification import PEER_RULE_VERSION, classify_peer
from kunjin.funds.peers.research import build_explicit_compare_report
from kunjin.funds.risk.models import FundRiskClassification
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import FundNavObservation
from kunjin.selection.models import (
    CandidateReview,
    ComparabilityEvidence,
    PersonalGateEvidence,
    ShortlistResult,
    validate_candidate_codes,
)
from kunjin.selection.policy import ShortlistPolicyV1
from kunjin.storage.repository import Repository

_ACTION_IDS = ("fact_research", "continue_holding")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$", flags=re.ASCII)
_GATE_STATES = frozenset({"fresh", "missing", "stale", "transient"})
_INSUFFICIENT_COMPARABILITY_REASONS = frozenset(
    {"identity_conflict", "missing_identity", "peer_classification_ambiguous"}
)
_METRIC_ORDERING_KEYS = frozenset(
    {
        "365d",
        "90d",
        "ongoing_annual_fee_rate",
        "portfolio_overlap",
        "size_stability",
    }
)
_WINDOW_FIELDS = frozenset(
    {
        "annualized_volatility",
        "drawdown_peak_date",
        "effective_end",
        "effective_start",
        "fund_code",
        "max_drawdown",
        "observations",
        "recovery_date",
        "total_return",
        "trough_date",
        "window",
    }
)
_MANAGER_FIELDS = frozenset(
    {"end_date", "manager_name", "source_document_id", "start_date"}
)
_FEE_FIELDS = frozenset(
    {
        "amount_max",
        "amount_min",
        "effective_from",
        "effective_to",
        "fee_type",
        "fixed_amount",
        "holding_days_max",
        "holding_days_min",
        "rate",
        "raw_rule_text",
        "rule_order",
        "share_class",
        "source_document_id",
    }
)
_SIZE_FIELDS = frozenset(
    {
        "earliest_net_assets",
        "earliest_report_date",
        "evidence_level",
        "latest_net_assets",
        "latest_report_date",
        "net_asset_change",
        "observations",
        "quarterly_change_pstdev",
    }
)
_ORDERING_FIELDS = frozenset({"direction", "fund_codes", "metric", "values", "window"})
_OVERLAP_FIELDS = frozenset(
    {
        "left_disclosed_weight",
        "left_fund_code",
        "left_published_at",
        "left_report_period",
        "metric_name",
        "overlap",
        "right_disclosed_weight",
        "right_fund_code",
        "right_published_at",
        "right_report_period",
        "warnings",
    }
)
_SHARED_OVERLAP_FIELDS = frozenset(
    {
        "exposure_code",
        "exposure_name",
        "exposure_type",
        "left_weight",
        "right_weight",
        "shared_weight",
    }
)
_CANDIDATE_OVERLAP_FIELDS = frozenset(
    {
        "candidate_disclosed_weight",
        "evidence_level",
        "metric_name",
        "overlap",
        "report_period",
    }
)
_CANDIDATE_SHARED_FIELDS = frozenset(
    {
        "candidate_disclosed_weight",
        "exposure_type",
        "security_code",
        "security_name",
        "shared_weight",
    }
)
_INVALIDATION_CONDITIONS = (
    "allocation_state_changes",
    "candidate_evidence_changes",
    "portfolio_observation_changes",
    "suitability_state_changes",
)


def _canonical_utc(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stable_code(value: object, *, prefix: str = "") -> Optional[str]:
    if type(value) is not str or not value:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if prefix:
        normalized = f"{prefix}_{normalized}" if normalized else prefix
    if not normalized or not normalized[0].isalpha():
        normalized = f"value_{normalized}"
    normalized = normalized[:128].rstrip("_")
    return normalized if _IDENTIFIER.fullmatch(normalized) is not None else None


def _stable_codes(values: object, *, prefix: str = "") -> Tuple[str, ...]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return ()
    return tuple(
        sorted(
            {
                code
                for value in values
                if (code := _stable_code(value, prefix=prefix)) is not None
            }
        )
    )


def _optional_code(value: object) -> Optional[str]:
    return value if type(value) is str and _IDENTIFIER.fullmatch(value) else None


def _gate_state(value: object) -> str:
    return value if type(value) is str and value in _GATE_STATES else "transient"


def _personal_gate(
    suitability_status: Mapping[str, object],
    allocation_status: Mapping[str, object],
) -> PersonalGateEvidence:
    result = PersonalGateEvidence(
        suitability_state=_gate_state(suitability_status.get("state", "missing")),
        suitability_freshness=_gate_state(suitability_status.get("freshness", "missing")),
        suitability_status=_optional_code(suitability_status.get("status")),
        allocation_state=_gate_state(allocation_status.get("state", "missing")),
        allocation_freshness=_gate_state(allocation_status.get("freshness", "missing")),
        allocation_status=_optional_code(allocation_status.get("status")),
        blocking_codes=_stable_codes(suitability_status.get("hard_blocks", ())),
        constraint_codes=tuple(
            sorted(
                set(_stable_codes(suitability_status.get("constraints", ())))
                | set(_stable_codes(allocation_status.get("binding_constraints", ())))
            )
        ),
    )
    result.validate()
    return result


def _empty_portfolio_binding(as_of: datetime) -> PortfolioEvidenceBinding:
    result = PortfolioEvidenceBinding(
        positions=(),
        snapshot_complete=False,
        observation_version="portfolio_unavailable",
        observed_at=as_of,
        source_state="unbound",
        request_id=None,
        request_mode=None,
        request_started_at=None,
        request_deadline_at=None,
    )
    result.validate()
    return result


def _empty_bundle(fund_code: str) -> DisclosureBundle:
    result = DisclosureBundle(
        fund_code=fund_code,
        identity=None,
        share_classes=(),
        manager_tenures=(),
        fee_rules=(),
        sizes=(),
        benchmarks=(),
        holdings=(),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )
    result.validate()
    return result


def _fallback_fact_set(fund_code: str) -> SourceLinkedFactSet:
    result = SourceLinkedFactSet(
        fund_code,
        (),
        (),
        ("selection_fact_projection",),
        (),
        (),
    )
    result.validate()
    return result


def _comparability(
    codes: Tuple[str, ...],
    bundles: Mapping[str, DisclosureBundle],
    failed_bundle_codes: set[str],
    as_of: datetime,
) -> Tuple[ComparabilityEvidence, ...]:
    result = []
    for left_code, right_code in combinations(codes, 2):
        if left_code in failed_bundle_codes or right_code in failed_bundle_codes:
            item = ComparabilityEvidence(
                left_fund_code=left_code,
                right_fund_code=right_code,
                state="insufficient_data",
                reason_code="missing_disclosure_bundle",
                warning_codes=(),
            )
        else:
            try:
                classification = classify_peer(
                    bundles[left_code],
                    bundles[right_code],
                    as_of.date(),
                )
                classification.validate()
                item = ComparabilityEvidence(
                    left_fund_code=left_code,
                    right_fund_code=right_code,
                    state=(
                        "comparable"
                        if classification.accepted
                        else (
                            "insufficient_data"
                            if classification.reason
                            in _INSUFFICIENT_COMPARABILITY_REASONS
                            else "not_comparable"
                        )
                    ),
                    reason_code=_stable_code(classification.reason)
                    or "peer_classification_ambiguous",
                    warning_codes=_stable_codes(classification.warnings),
                )
            except Exception:
                item = ComparabilityEvidence(
                    left_fund_code=left_code,
                    right_fund_code=right_code,
                    state="insufficient_data",
                    reason_code="peer_classification_unavailable",
                    warning_codes=(),
                )
        item.validate()
        result.append(item)
    return tuple(result)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _pick(value: object, fields: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if key in fields}


def _pick_rows(value: object, fields: frozenset[str]) -> list[dict[str, object]]:
    return [projected for item in _list(value) if (projected := _pick(item, fields))]


def _fund_code_key(value: object) -> bool:
    return type(value) is str and re.fullmatch(
        r"[0-9]{6}", value, flags=re.ASCII
    ) is not None


def _fund_rows(
    value: object,
    fields: frozenset[str],
) -> dict[str, list[dict[str, object]]]:
    return {
        str(code): _pick_rows(rows, fields)
        for code, rows in _mapping(value).items()
        if _fund_code_key(code)
    }


def _fund_records(
    value: object,
    fields: frozenset[str],
) -> dict[str, object]:
    return {
        str(code): (None if record is None else _pick(record, fields))
        for code, record in _mapping(value).items()
        if _fund_code_key(code)
    }


def _fund_scalars(value: object) -> dict[str, object]:
    return {
        str(code): item
        for code, item in _mapping(value).items()
        if _fund_code_key(code) and (item is None or type(item) in {bool, int, str})
    }


def _ordering_record(value: object) -> dict[str, object]:
    projected = _pick(value, _ORDERING_FIELDS)
    if "fund_codes" in projected:
        projected["fund_codes"] = [
            code for code in _list(projected["fund_codes"]) if _fund_code_key(code)
        ]
    if "values" in projected:
        projected["values"] = _fund_scalars(projected["values"])
    return projected


def _ordering_projection(value: object) -> dict[str, object]:
    result = {}
    for key, section in _mapping(value).items():
        if key not in _METRIC_ORDERING_KEYS:
            continue
        if key in {"90d", "365d"}:
            result[key] = {
                metric: projected
                for metric, ordering in _mapping(section).items()
                if (projected := _ordering_record(ordering))
            }
        else:
            result[key] = _ordering_record(section)
    return result


def _overlap_projection(value: object) -> Optional[dict[str, object]]:
    projected = _pick(value, _OVERLAP_FIELDS)
    if not projected:
        return None
    projected["shared"] = _pick_rows(
        _mapping(value).get("shared"),
        _SHARED_OVERLAP_FIELDS,
    )
    return projected


def _pairwise_projection(value: object) -> list[dict[str, object]]:
    result = []
    for item in _list(value):
        if not isinstance(item, Mapping):
            continue
        projected = {
            key: item[key]
            for key in ("left_fund_code", "right_fund_code")
            if key in item
        }
        for key in ("security", "industry"):
            projected[key] = _overlap_projection(item.get(key))
        if "left_fund_code" in projected and "right_fund_code" in projected:
            result.append(projected)
    return result


def _candidate_overlap_projection(value: object) -> dict[str, object]:
    result = {}
    for code, item in _mapping(value).items():
        projected = _pick(item, _CANDIDATE_OVERLAP_FIELDS)
        if projected:
            projected["shared"] = _pick_rows(
                _mapping(item).get("shared"),
                _CANDIDATE_SHARED_FIELDS,
            )
        result[str(code)] = projected
    return result


def _data_dates_projection(value: object) -> dict[str, object]:
    source = _mapping(value)
    result = {}
    if "common_nav_end" in source:
        result["common_nav_end"] = source["common_nav_end"]
    manager_starts = {
        str(code): start
        for code, start in _mapping(source.get("manager_team_starts")).items()
        if _fund_code_key(code)
    }
    if manager_starts:
        result["manager_team_starts"] = manager_starts
    return result


def _metric_projection(report: Mapping[str, object]) -> Tuple[Tuple[str, object], ...]:
    windows = _mapping(report.get("windows"))
    metrics = {
        "candidate_portfolio_overlap": _candidate_overlap_projection(
            report.get("candidate_portfolio_overlap")
        ),
        "data_dates": _data_dates_projection(report.get("data_dates")),
        "fees": _fund_rows(report.get("fees"), _FEE_FIELDS),
        "formal_nav_365d": _pick_rows(windows.get("365d"), _WINDOW_FIELDS),
        "formal_nav_90d": _pick_rows(windows.get("90d"), _WINDOW_FIELDS),
        "managers": _fund_rows(report.get("managers"), _MANAGER_FIELDS),
        "metric_orderings": _ordering_projection(report.get("metric_orderings")),
        "ongoing_annual_fee_rates": _fund_scalars(
            report.get("ongoing_annual_fee_rates")
        ),
        "pairwise_disclosed_overlap": _pairwise_projection(
            report.get("pairwise_overlap")
        ),
        "size_stability": _fund_records(report.get("sizes"), _SIZE_FIELDS),
    }
    return tuple(sorted(metrics.items()))


def _has_usable_common_dimension(metrics: Tuple[Tuple[str, object], ...]) -> bool:
    sections = dict(metrics)
    for key in ("formal_nav_90d", "formal_nav_365d"):
        rows = sections.get(key)
        if isinstance(rows, list) and len(
            {
                item.get("fund_code")
                for item in rows
                if isinstance(item, Mapping) and type(item.get("fund_code")) is str
            }
        ) >= 2:
            return True
    for key in (
        "fees",
        "managers",
        "ongoing_annual_fee_rates",
        "size_stability",
    ):
        section = sections.get(key)
        known_values = (
            sum(value not in (None, [], ()) for value in section.values())
            if isinstance(section, Mapping)
            else 0
        )
        if known_values >= 2:
            return True
    return bool(sections.get("pairwise_disclosed_overlap"))


def _candidate_metric_codes(
    report: Mapping[str, object],
    fund_code: str,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    result = []
    sections = (
        ("advantages", "relative_advantage"),
        ("tradeoffs", "relative_tradeoff"),
    )
    for report_key, prefix in sections:
        codes = set()
        for value in _list(report.get(report_key)):
            if type(value) is not str:
                continue
            parts = value.split(":")
            if parts and parts[-1] == fund_code:
                code = _stable_code("_".join(parts[:-1]), prefix=prefix)
                if code is not None:
                    codes.add(code)
        result.append(tuple(sorted(codes)))
    return result[0], result[1]


def _candidate_metric_gaps(
    report: Mapping[str, object],
    fund_code: str,
) -> Tuple[str, ...]:
    gaps = set()
    windows = _mapping(report.get("windows"))
    for window in ("90d", "365d"):
        rows = _list(windows.get(window))
        if not any(
            isinstance(item, Mapping) and item.get("fund_code") == fund_code
            for item in rows
        ):
            gaps.add(f"formal_nav_{window}_unavailable")
    fees = _mapping(report.get("fees")).get(fund_code)
    ongoing = _mapping(report.get("ongoing_annual_fee_rates")).get(fund_code)
    if not fees and ongoing is None:
        gaps.add("fees_unavailable")
    overlap = _mapping(report.get("candidate_portfolio_overlap")).get(fund_code)
    if (
        not isinstance(overlap, Mapping)
        or overlap.get("evidence_level") != "deterministic_calculation"
    ):
        gaps.add("portfolio_observed_overlap_unavailable")
    return tuple(sorted(gaps))


def _classification_projection(
    fund_code: str,
    classification: Optional[FundRiskClassification],
    policy: ShortlistPolicyV1,
) -> Tuple[
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[str],
    Tuple[str, ...],
    Tuple[str, ...],
    Tuple[str, ...],
]:
    if classification is None:
        return None, None, None, None, (), ("d1_classification_missing",), ()
    classification.validate()
    if classification.fund_code != fund_code:
        return (
            None,
            None,
            None,
            None,
            (),
            ("d1_classification_invalid",),
            ("d1_classification_subject_mismatch",),
        )
    evidence_status = classification.evidence_status.value
    risk_bucket = classification.risk_bucket.value
    portfolio_role = classification.portfolio_role.value
    mapped_layer, mapping_code = policy.map_asset_layer(
        evidence_status=evidence_status,
        risk_bucket=risk_bucket,
        portfolio_role=portfolio_role,
    )
    blocking = () if mapped_layer is not None else (mapping_code,)
    missing = tuple(sorted(set(classification.missing_evidence)))
    conflicts = tuple(sorted(set(classification.conflicts)))
    return (
        evidence_status,
        risk_bucket,
        portfolio_role,
        mapped_layer,
        blocking,
        missing,
        conflicts,
    )


def _fingerprint_payload(
    *,
    as_of: datetime,
    codes: Tuple[str, ...],
    policy: ShortlistPolicyV1,
    binding: PortfolioEvidenceBinding,
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Tuple[FundNavObservation, ...]],
    classifications: Mapping[str, Optional[FundRiskClassification]],
    personal_gate: PersonalGateEvidence,
    comparability: Tuple[ComparabilityEvidence, ...],
    metrics: Tuple[Tuple[str, object], ...],
    reviews: Tuple[CandidateReview, ...],
) -> Mapping[str, object]:
    return {
        "as_of": as_of,
        "candidate_codes": codes,
        "classification": {
            code: (
                None
                if classifications.get(code) is None
                else {
                    "classified_at": classifications[code].classified_at,
                    "evidence_document_ids": classifications[code].evidence_document_ids,
                    "evidence_fact_ids": classifications[code].evidence_fact_ids,
                    "evidence_status": classifications[code].evidence_status,
                    "input_fingerprint": classifications[code].input_fingerprint,
                    "policy_version": classifications[code].policy_version,
                    "valid_until": classifications[code].valid_until,
                }
            )
            for code in codes
        },
        "comparability": [vars(item) for item in comparability],
        "disclosures": {
            code: {
                "source_document_ids": tuple(sorted(bundle.source_documents)),
                "section_states": dict(sorted(bundle.section_states.items())),
            }
            for code, bundle in sorted(bundles.items())
        },
        "engines": {
            "peer_calculation_version": PEER_CALCULATION_VERSION,
            "peer_rule_version": PEER_RULE_VERSION,
            "shortlist_policy_checksum": policy.checksum(),
            "shortlist_policy_version": policy.version,
        },
        "histories": {
            code: {
                "count": len(history),
                "end": max((item.nav_date for item in history), default=None),
                "source_attempt_ids": tuple(
                    sorted(
                        {
                            item.source_attempt_id
                            for item in history
                            if item.source_attempt_id is not None
                        }
                    )
                ),
                "start": min((item.nav_date for item in history), default=None),
            }
            for code, history in histories.items()
        },
        "metric_comparisons": metrics,
        "personal_gate": vars(personal_gate),
        "portfolio_binding": {
            "observation_version": binding.observation_version,
            "observed_at": binding.observed_at,
            "snapshot_complete": binding.snapshot_complete,
            "source_state": binding.source_state,
        },
        "reviews": [vars(item) for item in reviews],
    }


class ShortlistService:
    def __init__(
        self,
        repository: Repository,
        disclosure_store: FundDisclosureStore,
        *,
        classification_loader: Callable[[str], Optional[FundRiskClassification]],
        suitability_status_loader: Callable[[], Mapping[str, object]],
        allocation_status_loader: Callable[[], Mapping[str, object]],
        policy: ShortlistPolicyV1 = ShortlistPolicyV1(),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(repository) is not Repository:
            raise ValueError("shortlist requires an exact Repository")
        if type(disclosure_store) is not FundDisclosureStore:
            raise ValueError("shortlist requires an exact FundDisclosureStore")
        if disclosure_store.repository is not repository:
            raise ValueError("shortlist stores must share one Repository")
        for loader, name in (
            (classification_loader, "classification loader"),
            (suitability_status_loader, "suitability status loader"),
            (allocation_status_loader, "allocation status loader"),
            (clock, "shortlist clock"),
        ):
            if not callable(loader):
                raise ValueError(f"{name} must be callable")
        policy.validate()
        self._repository = repository
        self._disclosure_store = disclosure_store
        self._classification_loader = classification_loader
        self._suitability_status_loader = suitability_status_loader
        self._allocation_status_loader = allocation_status_loader
        self._policy = policy
        self._clock = clock

    def review(self, candidate_codes: Sequence[str]) -> ShortlistResult:
        codes = validate_candidate_codes(candidate_codes)
        return self._review(codes, _canonical_utc(self._clock(), "shortlist clock"))

    def _review(self, codes: Tuple[str, ...], as_of: datetime) -> ShortlistResult:
        positions = tuple(self._repository.latest_positions())
        held_codes = tuple(sorted({item.fund_code for item in positions if item.shares > 0}))
        binding = (
            build_authenticated_portfolio_binding(self._repository, positions)
            if positions
            else _empty_portfolio_binding(as_of)
        )

        load_order = (*codes, *(code for code in held_codes if code not in codes))
        bundles: Dict[str, DisclosureBundle] = {}
        failed_bundle_codes: set[str] = set()
        for code in load_order:
            try:
                bundle = self._disclosure_store.load_bundle(code)
                if type(bundle) is not DisclosureBundle or bundle.fund_code != code:
                    raise ValueError("disclosure subject mismatch")
                bundle.validate()
                bundles[code] = bundle
            except Exception:
                failed_bundle_codes.add(code)
                bundles[code] = _empty_bundle(code)

        histories: Dict[str, Tuple[FundNavObservation, ...]] = {}
        for code in codes:
            try:
                histories[code] = tuple(self._repository.fund_history(code))
            except Exception:
                histories[code] = ()

        classifications: Dict[str, Optional[FundRiskClassification]] = {}
        classification_failures = set()
        for code in codes:
            try:
                classifications[code] = self._classification_loader(code)
                if classifications[code] is not None:
                    classifications[code].validate()
            except Exception:
                classifications[code] = None
                classification_failures.add(code)

        comparison_failed = False
        try:
            comparison_report = build_explicit_compare_report(
                codes,
                bundles,
                histories,
                positions,
                as_of,
            )
        except Exception:
            comparison_failed = True
            comparison_report = {
                "as_of": as_of.isoformat(),
                "windows": {"90d": [], "365d": []},
                "metric_orderings": {},
                "managers": {},
                "fees": {},
                "ongoing_annual_fee_rates": {},
                "sizes": {},
                "pairwise_overlap": [],
                "candidate_portfolio_overlap": {},
                "data_dates": {},
                "data_gaps": ["explicit_comparison_unavailable"],
                "warnings": [],
                "errors": [],
            }
        metrics = _metric_projection(comparison_report)
        comparability = _comparability(codes, bundles, failed_bundle_codes, as_of)

        fact_sets: Dict[str, SourceLinkedFactSet] = {}
        fact_failures = set()
        for code in load_order:
            try:
                fact_sets[code] = build_source_linked_facts(
                    bundles[code],
                    as_of,
                    action_ids=_ACTION_IDS,
                )
            except Exception:
                fact_failures.add(code)
                fact_sets[code] = _fallback_fact_set(code)

        budget = RequestBudget.create(RequestMode.RAPID, wall_clock=lambda: as_of)
        d2_by_code = {}
        d2_failures = set()
        relationship_by_id: Dict[str, DiagnosisRelationship] = {}
        for code in codes:
            try:
                d2 = build_d2_relationships(
                    code,
                    binding,
                    fact_sets,
                    as_of,
                    request_id=budget.request_id,
                    request_mode=RequestMode.RAPID,
                )
                d2.validate()
                d2_by_code[code] = d2
                for source in d2.relationships:
                    projected = project_diagnosis_relationship(source)
                    previous = relationship_by_id.setdefault(projected.relationship_id, projected)
                    if previous != projected:
                        raise ValueError("D2 relationship identity drifted")
            except Exception:
                d2_failures.add(code)

        relationships = tuple(
            sorted(relationship_by_id.values(), key=lambda item: item.relationship_id)
        )
        reviews = []
        for code in codes:
            local_missing = set(_candidate_metric_gaps(comparison_report, code))
            local_conflicts = set()
            local_warnings = set()
            local_blocking = set()
            projection_unknowns = set()
            if code in failed_bundle_codes:
                local_missing.add("disclosure_bundle_unavailable")
            if code in classification_failures:
                local_missing.add("d1_classification_unavailable")
            if code in fact_failures:
                local_missing.add("fact_projection_unavailable")
                projection_unknowns.add("fact_projection_unavailable")
            if comparison_failed:
                local_missing.add("explicit_comparison_unavailable")
                projection_unknowns.add("explicit_comparison_unavailable")

            try:
                (
                    d1_status,
                    risk_bucket,
                    portfolio_role,
                    mapped_layer,
                    mapping_blocks,
                    d1_missing,
                    d1_conflicts,
                ) = _classification_projection(
                    code,
                    classifications.get(code),
                    self._policy,
                )
            except Exception:
                d1_status = risk_bucket = portfolio_role = mapped_layer = None
                mapping_blocks = ()
                d1_missing = ("d1_classification_invalid",)
                d1_conflicts = ()
            local_blocking.update(mapping_blocks)
            local_missing.update(d1_missing)
            local_conflicts.update(d1_conflicts)

            d2 = d2_by_code.get(code)
            candidate_relationships = tuple(
                item for item in relationships if code in item.fund_codes
            )
            if d2 is not None:
                local_missing.update(d2.missing_fields)
                local_conflicts.update(d2.conflicts)
                local_warnings.update(d2.warnings)
                projection_unknowns.update(d2.missing_fields)
                projection_unknowns.update(d2.conflicts)
            if code in d2_failures:
                local_missing.add("portfolio_impact_projection_unavailable")
                projection_unknowns.add("portfolio_impact_projection_unavailable")
            projection_report = {
                **comparison_report,
                "as_of": as_of,
                "_candidate_projection_unknown_fields": tuple(
                    sorted(projection_unknowns)
                ),
            }
            impact: Optional[CandidateImpact]
            try:
                impact = project_candidate_impact(
                    code,
                    bundles[code],
                    candidate_relationships,
                    projection_report,
                )
                impact.validate()
            except Exception:
                impact = None
                local_missing.add("portfolio_impact_projection_unavailable")

            position_state = "held" if code in held_codes else "not_held"
            if position_state == "held":
                local_blocking.add("marginal_impact_requires_purchase_amount")
                if impact is not None and impact.label == "observed_adds_distinct_exposure":
                    impact = None
                    local_missing.add("marginal_portfolio_impact_unavailable")
            if "fees_unavailable" in local_missing:
                local_blocking.add("fees_unavailable")
            portfolio_impact_state = (
                "usable"
                if impact is not None and impact.label != "insufficient_data"
                else "insufficient_data"
            )
            if impact is not None:
                local_missing.update(impact.unknown_fields)
            advantage_codes, tradeoff_codes = _candidate_metric_codes(comparison_report, code)
            evidence_state = (
                "insufficient_data"
                if code in failed_bundle_codes or d1_status is None
                else "relative_tradeoffs_only"
            )
            review = CandidateReview(
                fund_code=code,
                position_state=position_state,
                evidence_state=evidence_state,
                d1_evidence_status=d1_status,
                risk_bucket=risk_bucket,
                portfolio_role=portfolio_role,
                mapped_asset_layer=mapped_layer,
                portfolio_impact_state=portfolio_impact_state,
                portfolio_impact_label=None if impact is None else impact.label,
                relationship_ids=(
                    () if impact is None else tuple(sorted(impact.relationship_ids))
                ),
                advantage_codes=advantage_codes,
                tradeoff_codes=tradeoff_codes,
                blocking_codes=tuple(sorted(local_blocking)),
                missing_evidence=tuple(sorted(local_missing)),
                conflicts=tuple(sorted(local_conflicts)),
                warnings=tuple(sorted(local_warnings)),
            )
            review.validate()
            reviews.append(review)

        status_missing = set()
        try:
            suitability_status = self._suitability_status_loader()
            if not isinstance(suitability_status, Mapping):
                raise ValueError("suitability status is not a mapping")
        except Exception:
            suitability_status = {"state": "transient", "freshness": "transient"}
            status_missing.add("suitability_status_unavailable")
        try:
            allocation_status = self._allocation_status_loader()
            if not isinstance(allocation_status, Mapping):
                raise ValueError("allocation status is not a mapping")
        except Exception:
            allocation_status = {"state": "transient", "freshness": "transient"}
            status_missing.add("allocation_status_unavailable")
        personal_gate = _personal_gate(suitability_status, allocation_status)

        review_tuple = tuple(reviews)
        comparison_state, shortlist_codes = self._policy.evaluate(
            candidate_codes=codes,
            has_usable_common_dimension=_has_usable_common_dimension(metrics),
            comparability=comparability,
            candidate_reviews=review_tuple,
            personal_gate=personal_gate,
        )
        if comparison_state == "not_comparable":
            review_tuple = tuple(
                item
                if item.evidence_state == "insufficient_data"
                else replace(item, evidence_state="not_comparable")
                for item in review_tuple
            )
        elif comparison_state == "conditional_shortlist":
            shortlist_set = set(shortlist_codes)
            review_tuple = tuple(
                replace(item, evidence_state="conditional_shortlist_member")
                if item.fund_code in shortlist_set
                else item
                for item in review_tuple
            )

        report_missing = _stable_codes(
            comparison_report.get("data_gaps", ()), prefix="comparison"
        )
        report_warnings = _stable_codes(
            comparison_report.get("warnings", ()), prefix="comparison"
        )
        report_errors = _stable_codes(
            comparison_report.get("errors", ()), prefix="comparison"
        )
        missing = tuple(
            sorted(
                status_missing
                | set(report_missing)
                | {value for item in review_tuple for value in item.missing_evidence}
            )
        )
        conflicts = tuple(
            sorted(
                set(report_errors)
                | {value for item in review_tuple for value in item.conflicts}
            )
        )
        warnings = tuple(
            sorted(
                set(report_warnings)
                | {value for item in review_tuple for value in item.warnings}
            )
        )
        fingerprint = hashlib.sha256(
            canonical_json_bytes(
                _fingerprint_payload(
                    as_of=as_of,
                    codes=codes,
                    policy=self._policy,
                    binding=binding,
                    bundles=bundles,
                    histories=histories,
                    classifications=classifications,
                    personal_gate=personal_gate,
                    comparability=comparability,
                    metrics=metrics,
                    reviews=review_tuple,
                )
            )
        ).hexdigest()
        result = ShortlistResult(
            as_of=as_of,
            candidate_codes=codes,
            comparison_state=comparison_state,
            personal_gate=personal_gate,
            comparability=comparability,
            metric_comparisons=metrics,
            candidate_reviews=review_tuple,
            shortlist_codes=shortlist_codes,
            invalidation_conditions=_INVALIDATION_CONDITIONS,
            missing_evidence=missing,
            conflicts=conflicts,
            warnings=warnings,
            input_fingerprint=fingerprint,
        )
        result.validate()
        return result


__all__ = ["ShortlistService"]
