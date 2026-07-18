from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.diagnosis.models import (
    CandidateImpact,
    DiagnosisCoverage,
    DiagnosisFinding,
    DiagnosisRelationship,
    PortfolioDiagnosis,
)

NOW = datetime(2026, 7, 19, 4, tzinfo=timezone.utc)


def _diagnosis() -> PortfolioDiagnosis:
    relationship = DiagnosisRelationship(
        relationship_id="same_manager_000001_000002",
        relationship_type="same_manager",
        fund_codes=("000001", "000002"),
        evidence_state="complete",
        metrics=(("shared_manager_name", "示例经理"),),
        report_periods=(),
        publication_times=(NOW - timedelta(days=1),),
        warnings=(),
    )
    relationship_coverage = DiagnosisCoverage(
        scope="minimum_relationship_coverage",
        evidence_state="complete",
        included_fund_codes=("000001", "000002"),
        omitted_fund_codes=(),
        unknown_fields=(
            "authenticated_index_identity_000001",
            "authenticated_index_identity_000002",
        ),
        known_weight=Decimal("1"),
    )
    holdings_coverage = DiagnosisCoverage(
        scope="disclosed_holdings_overlap",
        evidence_state="partial",
        included_fund_codes=("000001", "000002"),
        omitted_fund_codes=(),
        unknown_fields=("holdings_pair_comparability_000001_000002",),
        known_weight=Decimal("0.85"),
    )
    finding = DiagnosisFinding(
        finding_id="finding_same_manager_000001_000002",
        finding_type="same_current_manager",
        severity="attention",
        fund_codes=("000001", "000002"),
        relationship_ids=(relationship.relationship_id,),
        evidence_scope="authenticated_product_relationship",
    )
    candidate = CandidateImpact(
        fund_code="000003",
        label="mixed_observed_impact",
        relationship_ids=(relationship.relationship_id,),
        disclosed_weight=Decimal("0.72"),
        observed_overlap=Decimal("0.12"),
        unknown_fields=("candidate_residual_exposure",),
    )
    return PortfolioDiagnosis(
        as_of=NOW,
        value_basis="formal",
        position_count=2,
        hhi=Decimal("0.52"),
        largest_position_share=Decimal("0.6"),
        relationship_coverage=relationship_coverage,
        holdings_coverage=holdings_coverage,
        relationships=(relationship,),
        candidate_impact=candidate,
        findings=(finding,),
        missing_evidence=("candidate_residual_exposure",),
        conflicts=(),
        warnings=("top10_disclosed_overlap_only",),
        input_fingerprint="a" * 64,
    )


def test_portfolio_diagnosis_is_amount_free_and_non_authorizing() -> None:
    result = _diagnosis()

    result.validate()

    assert result.action_maturity == "evidence_only"
    assert result.action_authorized is False
    assert result.exact_amount_available is False


def test_relationship_and_finding_evidence_must_close() -> None:
    result = _diagnosis()
    broken_finding = replace(
        result.findings[0],
        relationship_ids=("missing_relationship",),
    )

    with pytest.raises(ValueError, match="relationship"):
        replace(result, findings=(broken_finding,)).validate()

    with pytest.raises(ValueError, match="candidate"):
        replace(
            result,
            candidate_impact=replace(
                result.candidate_impact,
                relationship_ids=("missing_relationship",),
            ),
        ).validate()


def test_coverage_partitions_must_match_and_ratios_are_bounded() -> None:
    result = _diagnosis()

    with pytest.raises(ValueError, match="partition"):
        replace(
            result,
            holdings_coverage=replace(
                result.holdings_coverage,
                omitted_fund_codes=("000004",),
            ),
        ).validate()

    with pytest.raises(ValueError, match="known weight"):
        replace(
            result,
            relationship_coverage=replace(
                result.relationship_coverage,
                known_weight=Decimal("1.01"),
            ),
        ).validate()


def test_future_relationship_evidence_and_private_metric_keys_are_rejected() -> None:
    result = _diagnosis()

    with pytest.raises(ValueError, match="future"):
        replace(
            result,
            relationships=(
                replace(
                    result.relationships[0],
                    publication_times=(NOW + timedelta(seconds=1),),
                ),
            ),
        ).validate()

    with pytest.raises(ValueError, match="private"):
        replace(
            result,
            relationships=(
                replace(
                    result.relationships[0],
                    metrics=(("position_shares", "10"),),
                ),
            ),
        ).validate()


@pytest.mark.parametrize(
    "label",
    (
        "observed_adds_distinct_exposure",
        "observed_duplicates_existing_exposure",
        "mixed_observed_impact",
        "insufficient_data",
    ),
)
def test_candidate_labels_are_an_exact_closed_set(label: str) -> None:
    result = _diagnosis()
    replace(
        result,
        candidate_impact=replace(result.candidate_impact, label=label),
    ).validate()

    with pytest.raises(ValueError, match="candidate label"):
        replace(
            result,
            candidate_impact=replace(result.candidate_impact, label="recommended"),
        ).validate()


def test_exact_types_sorted_identifiers_and_canonical_fingerprint_are_required() -> None:
    result = _diagnosis()

    with pytest.raises(ValueError, match="ascending"):
        replace(
            result,
            missing_evidence=("z_missing", "a_missing"),
        ).validate()

    with pytest.raises(ValueError, match="fingerprint"):
        replace(result, input_fingerprint="A" * 64).validate()

    class DiagnosisSubclass(PortfolioDiagnosis):
        pass

    with pytest.raises(ValueError, match="exact"):
        DiagnosisSubclass(**result.__dict__).validate()


def test_report_periods_require_exact_dates() -> None:
    result = _diagnosis()
    relationship = replace(
        result.relationships[0],
        report_periods=(date(2026, 6, 30),),
    )

    replace(result, relationships=(relationship,)).validate()
