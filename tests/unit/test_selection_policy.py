from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import timezone

import pytest

from kunjin.selection.models import (
    CandidateReview,
    ComparabilityEvidence,
    PersonalGateEvidence,
)
from kunjin.selection.policy import (
    SHORTLIST_POLICY_V1_GOLDEN_CHECKSUM,
    ShortlistPolicyV1,
    evaluate_shortlist_state,
    personal_gate_passes,
)


def _gate(**changes: object) -> PersonalGateEvidence:
    values = {
        "suitability_state": "fresh",
        "suitability_freshness": "fresh",
        "suitability_status": "ready_for_allocation",
        "allocation_state": "fresh",
        "allocation_freshness": "fresh",
        "allocation_status": "range_available",
        "blocking_codes": (),
        "constraint_codes": (),
    }
    values.update(changes)
    return PersonalGateEvidence(**values)


def _review(code: str, **changes: object) -> CandidateReview:
    values = {
        "fund_code": code,
        "position_state": "not_held",
        "evidence_state": "relative_tradeoffs_only",
        "d1_evidence_status": "verified",
        "risk_bucket": "diversified_equity",
        "portfolio_role": "core_eligible",
        "mapped_asset_layer": "diversified_equity",
        "portfolio_impact_state": "usable",
        "portfolio_impact_label": "observed_adds_distinct_exposure",
        "relationship_ids": (),
        "advantage_codes": (),
        "tradeoff_codes": (),
        "blocking_codes": (),
        "missing_evidence": (),
        "conflicts": (),
        "warnings": (),
    }
    values.update(changes)
    return CandidateReview(**values)


def _pair(left: str = "000001", right: str = "000002", state: str = "comparable"):
    return ComparabilityEvidence(
        left_fund_code=left,
        right_fund_code=right,
        state=state,
        reason_code="same_product_family",
        warning_codes=(),
    )


def _evaluate(**changes: object):
    values = {
        "candidate_codes": ("000001", "000002"),
        "has_usable_common_dimension": True,
        "comparability": (_pair(),),
        "candidate_reviews": (_review("000001"), _review("000002")),
        "personal_gate": _gate(),
    }
    values.update(changes)
    return evaluate_shortlist_state(**values)


def test_policy_v1_is_canonical_frozen_and_checksummed() -> None:
    policy = ShortlistPolicyV1()

    policy.validate()
    assert policy.version == "1"
    assert policy.effective_at.tzinfo is timezone.utc
    assert policy.checksum() == SHORTLIST_POLICY_V1_GOLDEN_CHECKSUM
    assert policy.canonical_json() == json.dumps(
        json.loads(policy.canonical_json()),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    with pytest.raises(FrozenInstanceError):
        policy.version = "2"  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical"):
        replace(policy, version="2").validate()


def test_exported_personal_gate_predicate_retains_policy_v1_requirements() -> None:
    assert personal_gate_passes(_gate()) is True
    assert personal_gate_passes(_gate(suitability_status="blocked")) is False
    assert personal_gate_passes(_gate(allocation_freshness="stale")) is False
    assert ShortlistPolicyV1().checksum() == SHORTLIST_POLICY_V1_GOLDEN_CHECKSUM


@pytest.mark.parametrize(
    ("evidence_status", "risk_bucket", "portfolio_role", "expected"),
    (
        ("partial", "diversified_equity", "core_eligible", (None, "d1_evidence_not_verified")),
        (
            "verified",
            "diversified_equity",
            "not_eligible",
            (None, "d1_portfolio_role_not_eligible"),
        ),
        (
            "verified",
            "high_quality_fixed_income",
            "core_eligible",
            ("high_quality_fixed_income", "mapped_verified_d1_bucket"),
        ),
        (
            "verified",
            "diversified_equity",
            "core_eligible",
            ("diversified_equity", "mapped_verified_d1_bucket"),
        ),
        (
            "verified",
            "cash_like_candidate",
            "cash_management_candidate",
            (None, "cash_like_is_not_protected_cash"),
        ),
        (
            "verified",
            "concentrated_equity",
            "satellite_only",
            (None, "d1_bucket_has_no_phase4_mapping"),
        ),
    ),
)
def test_policy_v1_has_only_the_approved_narrow_layer_mappings(
    evidence_status: str,
    risk_bucket: str,
    portfolio_role: str,
    expected: tuple[str | None, str],
) -> None:
    assert ShortlistPolicyV1().map_asset_layer(
        evidence_status=evidence_status,
        risk_bucket=risk_bucket,
        portfolio_role=portfolio_role,
    ) == expected


def test_state_precedence_starts_with_insufficient_data() -> None:
    assert _evaluate(has_usable_common_dimension=False) == ("insufficient_data", ())
    assert _evaluate(
        candidate_reviews=(
            _review("000001", evidence_state="insufficient_data"),
            _review("000002", evidence_state="insufficient_data"),
        )
    ) == ("insufficient_data", ())

    assert _evaluate(
        candidate_reviews=(
            _review(
                "000001",
                d1_evidence_status=None,
                risk_bucket=None,
                portfolio_role=None,
                mapped_asset_layer=None,
            ),
            _review(
                "000002",
                d1_evidence_status=None,
                risk_bucket=None,
                portfolio_role=None,
                mapped_asset_layer=None,
            ),
        )
    ) == ("insufficient_data", ())


def test_not_comparable_precedes_personal_gate_and_relative_tradeoffs() -> None:
    assert _evaluate(
        comparability=(_pair(state="not_comparable"),),
        personal_gate=_gate(suitability_state="missing", suitability_freshness="missing"),
    ) == ("not_comparable", ())

    assert _evaluate(
        candidate_reviews=(
            _review("000001", mapped_asset_layer="diversified_equity"),
            _review("000002", mapped_asset_layer="high_quality_fixed_income"),
        )
    ) == ("not_comparable", ())


@pytest.mark.parametrize(
    "changes",
    (
        {"personal_gate": _gate(suitability_freshness="stale")},
        {"personal_gate": _gate(suitability_status="blocked")},
        {"personal_gate": _gate(allocation_freshness="stale")},
        {"personal_gate": _gate(allocation_status="blocked")},
        {
            "candidate_reviews": (
                _review("000001", portfolio_impact_state="insufficient_data"),
                _review("000002", portfolio_impact_state="insufficient_data"),
            )
        },
        {
            "candidate_reviews": (
                _review("000001", blocking_codes=("candidate_conflict",)),
                _review("000002", conflicts=("identity_conflict",)),
            )
        },
    ),
)
def test_unmet_personal_or_candidate_gates_leave_relative_tradeoffs_only(changes) -> None:
    assert _evaluate(**changes) == ("relative_tradeoffs_only", ())


def test_conditional_shortlist_returns_all_passing_members_in_request_order() -> None:
    candidate_codes = ("000003", "000001", "000002")
    reviews = (
        _review("000003"),
        _review("000001"),
        _review("000002", portfolio_impact_state="insufficient_data"),
    )
    comparability = (
        _pair("000003", "000001"),
        _pair("000003", "000002"),
        _pair("000001", "000002"),
    )

    assert _evaluate(
        candidate_codes=candidate_codes,
        candidate_reviews=reviews,
        comparability=comparability,
    ) == ("conditional_shortlist", ("000003", "000001"))

    assert ShortlistPolicyV1().evaluate(
        candidate_codes=candidate_codes,
        has_usable_common_dimension=True,
        comparability=comparability,
        candidate_reviews=reviews,
        personal_gate=_gate(),
    ) == ("conditional_shortlist", ("000003", "000001"))


def test_conditional_shortlist_requires_two_mutually_comparable_members() -> None:
    assert _evaluate(
        candidate_codes=("000001", "000002", "000003"),
        candidate_reviews=(
            _review("000001"),
            _review("000002"),
            _review("000003", mapped_asset_layer=None),
        ),
        comparability=(
            _pair("000001", "000002", "not_comparable"),
            _pair("000001", "000003", "insufficient_data"),
            _pair("000002", "000003", "insufficient_data"),
        ),
    ) == ("not_comparable", ())
