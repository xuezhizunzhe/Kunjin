from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from kunjin.selection.models import (
    CandidateReview,
    ComparabilityEvidence,
    PersonalGateEvidence,
    ShortlistResult,
    validate_candidate_codes,
)

NOW = datetime(2026, 7, 19, 4, tzinfo=timezone.utc)


def _gate() -> PersonalGateEvidence:
    return PersonalGateEvidence(
        suitability_state="fresh",
        suitability_freshness="fresh",
        suitability_status="ready_for_allocation",
        allocation_state="fresh",
        allocation_freshness="fresh",
        allocation_status="range_available",
        blocking_codes=(),
        constraint_codes=("horizon_binding",),
    )


def _review(code: str, state: str = "conditional_shortlist_member") -> CandidateReview:
    return CandidateReview(
        fund_code=code,
        position_state="not_held",
        evidence_state=state,
        d1_evidence_status="verified",
        risk_bucket="diversified_equity",
        portfolio_role="core_eligible",
        mapped_asset_layer="diversified_equity",
        portfolio_impact_state="usable",
        portfolio_impact_label="observed_adds_distinct_exposure",
        relationship_ids=(),
        advantage_codes=("lower_ongoing_fee",),
        tradeoff_codes=("higher_volatility_365d",),
        blocking_codes=(),
        missing_evidence=(),
        conflicts=(),
        warnings=(),
    )


def shortlist_result_fixture() -> ShortlistResult:
    return ShortlistResult(
        as_of=NOW,
        candidate_codes=("000002", "000001"),
        comparison_state="conditional_shortlist",
        personal_gate=_gate(),
        comparability=(
            ComparabilityEvidence(
                left_fund_code="000002",
                right_fund_code="000001",
                state="comparable",
                reason_code="same_product_family",
                warning_codes=(),
            ),
        ),
        metric_comparisons=(
            (
                "fees",
                {
                    "bands": (
                        {"amount_min": Decimal("0"), "amount_max": Decimal("1000")},
                    ),
                    "as_of": date(2026, 7, 18),
                },
            ),
        ),
        candidate_reviews=(_review("000002"), _review("000001")),
        shortlist_codes=("000002", "000001"),
        invalidation_conditions=("allocation_state_changes",),
        missing_evidence=(),
        conflicts=(),
        warnings=(),
        input_fingerprint="a" * 64,
    )


def test_shortlist_result_is_ordered_amount_free_and_non_authorizing() -> None:
    result = shortlist_result_fixture()

    result.validate()

    assert result.candidate_codes == ("000002", "000001")
    assert result.shortlist_codes == ("000002", "000001")
    assert result.action_maturity == "evidence_only"
    assert result.action_authorized is False
    assert result.exact_amount_available is False
    assert result.automatic_trade is False
    with pytest.raises(FrozenInstanceError):
        result.comparison_state = "relative_tradeoffs_only"  # type: ignore[misc]


@pytest.mark.parametrize(
    "codes",
    (
        ("000001",),
        tuple(f"{index:06d}" for index in range(1, 7)),
        ("000001", "000001"),
        ("000000", "000001"),
        ("１２３４５６", "000001"),
    ),
)
def test_candidate_codes_require_two_to_five_exact_unique_values(codes) -> None:
    with pytest.raises(ValueError, match="candidate codes"):
        replace(shortlist_result_fixture(), candidate_codes=codes).validate()

    with pytest.raises(ValueError, match="candidate codes"):
        validate_candidate_codes(codes)


def test_candidate_order_and_all_pair_and_candidate_references_must_close() -> None:
    result = shortlist_result_fixture()

    with pytest.raises(ValueError, match="comparability.*close"):
        replace(
            result,
            comparability=(
                replace(result.comparability[0], right_fund_code="000003"),
            ),
        ).validate()

    with pytest.raises(ValueError, match="candidate reviews.*request order"):
        replace(result, candidate_reviews=tuple(reversed(result.candidate_reviews))).validate()

    with pytest.raises(ValueError, match="shortlist.*request order"):
        replace(result, shortlist_codes=("000001", "000002")).validate()

    with pytest.raises(ValueError, match="conditional shortlist member"):
        replace(
            result,
            candidate_reviews=(
                replace(result.candidate_reviews[0], evidence_state="relative_tradeoffs_only"),
                result.candidate_reviews[1],
            ),
        ).validate()


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("comparison_state", "recommended", "comparison state"),
        ("evidence_state", "best", "candidate evidence state"),
        ("position_state", "unknown", "position state"),
        ("portfolio_impact_state", "recommended", "portfolio impact state"),
    ),
)
def test_states_are_closed(field_name: str, value: str, message: str) -> None:
    result = shortlist_result_fixture()
    if field_name == "comparison_state":
        invalid = replace(result, comparison_state=value)
    else:
        invalid = replace(
            result,
            candidate_reviews=(
                replace(result.candidate_reviews[0], **{field_name: value}),
                result.candidate_reviews[1],
            ),
        )
    with pytest.raises(ValueError, match=message):
        invalid.validate()


@pytest.mark.parametrize(
    "private_key",
    (
        "account_title",
        "AMOUNT",
        "asset",
        "cost",
        "debt",
        "income",
        "monthly_income",
        "profit",
        "profile",
        "reserve",
        "portfolio_weight",
        "shares",
        "total_value",
    ),
)
def test_metric_comparisons_recursively_reject_exact_private_keys(private_key: str) -> None:
    result = shortlist_result_fixture()
    private_tree = ({"public": [{private_key: "private"}]},)

    with pytest.raises(ValueError, match="private field"):
        replace(result, metric_comparisons=(("fees", private_tree),)).validate()


def test_fee_band_keys_and_private_substrings_remain_valid() -> None:
    result = shortlist_result_fixture()
    replace(
        result,
        metric_comparisons=(
            (
                "fees",
                {
                    "amount_min": Decimal("0"),
                    "amount_max": Decimal("1000"),
                    "asset_class": "equity",
                },
            ),
        ),
    ).validate()


def test_stable_code_lists_fingerprint_and_exact_record_types_are_enforced() -> None:
    result = shortlist_result_fixture()

    with pytest.raises(ValueError, match="ascending"):
        replace(result, warnings=("z_warning", "a_warning")).validate()
    with pytest.raises(ValueError, match="fingerprint"):
        replace(result, input_fingerprint="A" * 64).validate()
    with pytest.raises(ValueError, match="canonical UTC"):
        replace(result, as_of=NOW.astimezone()).validate()

    class ShortlistSubclass(ShortlistResult):
        pass

    with pytest.raises(ValueError, match="exact"):
        ShortlistSubclass(**result.__dict__).validate()

