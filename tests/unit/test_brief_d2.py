from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.brief.d2 import (
    D2RelationshipSet,
    PortfolioEvidenceBinding,
    build_d2_relationships,
)
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import BriefEvidenceState, BriefFact
from kunjin.brief.policy import MAX_FACTS
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    SourceTier,
)
from kunjin.models import StoredPosition

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
REQUEST_ID = "1234567890abcdef1234567890abcdef"
PRIVATE_ACCOUNT = "private-account-title-4acdb7"
PRIVATE_SHARES = Decimal("73129.17")
PRIVATE_PROFIT = Decimal("-98765.43")


def _fact(
    code: str,
    field_id: str,
    value: object,
    *,
    fact_id: str,
    conflict_ids: tuple[str, ...] = (),
) -> BriefFact:
    return BriefFact(
        fact_id=fact_id,
        field_id=field_id,
        value=value,
        unit=None,
        data_as_of=NOW - timedelta(days=1),
        published_at=NOW - timedelta(days=1),
        retrieved_at=NOW,
        source_id="eastmoney_f10",
        source_tier=SourceTier.TIER_2,
        publisher="东方财富",
        canonical_url=f"https://fundf10.eastmoney.com/{field_id}/{code}",
        freshness=EvidenceFreshness.CURRENT,
        completeness=EvidenceCompleteness.PARTIAL,
        conflict_ids=conflict_ids,
        calculated=False,
        source_lineage_id=f"document_{code}_{field_id}",
    )


def _facts(
    code: str,
    *,
    company: str,
    manager: str,
    benchmark: str,
    sibling: str | None = None,
    sibling_complete: bool = False,
) -> tuple[BriefFact, ...]:
    related = code if sibling is None else sibling
    return (
        _fact(
            code,
            "identity_active_status",
            {"fund_code": code, "fund_company": company},
            fact_id="identity_active_status",
        ),
        _fact(
            code,
            "current_manager_team",
            {"manager_name": manager, "tenure_start": "2024-01-01", "tenure_end": None},
            fact_id="current_manager_team_1",
        ),
        _fact(
            code,
            "current_benchmark",
            {
                "description": benchmark,
                "effective_from": "2025-01-01",
                "effective_to": None,
            },
            fact_id="current_benchmark_1",
        ),
        replace(
            _fact(
                code,
                "share_class_identity",
                {
                    "related_fund_code": related,
                    "share_class": "A",
                    "fund_name": f"基金{code}",
                },
                fact_id="share_class_1",
            ),
            completeness=(
                EvidenceCompleteness.COMPLETE if sibling_complete else EvidenceCompleteness.PARTIAL
            ),
        ),
    )


def _position(
    code: str,
    shares: str,
    *,
    account: str = "account",
    nav: str | None = "1",
    observed_at: datetime = NOW,
) -> StoredPosition:
    return StoredPosition(
        account_title=account,
        fund_code=code,
        fund_name=f"基金{code}",
        shares=Decimal(shares),
        observed_at=observed_at,
        share_class="A",
        formal_nav=None if nav is None else Decimal(nav),
        estimated_nav=None,
        observed_profit=PRIVATE_PROFIT,
    )


def _set(code: str, facts: tuple[BriefFact, ...]) -> SourceLinkedFactSet:
    return SourceLinkedFactSet(code, facts, (), (), (), ())


def _binding(
    positions: tuple[StoredPosition, ...],
    *,
    snapshot_complete: bool = True,
    source_state: str = "same_request_success",
    observed_at: datetime | None = None,
) -> PortfolioEvidenceBinding:
    if observed_at is None:
        observed_at = max((item.observed_at for item in positions), default=NOW)
    return PortfolioEvidenceBinding(
        positions=positions,
        snapshot_complete=snapshot_complete,
        observation_version="portfolio_snapshot_v1",
        observed_at=observed_at,
        source_state=source_state,
        request_id=(REQUEST_ID if source_state == "same_request_success" else None),
        request_mode=(RequestMode.RAPID if source_state == "same_request_success" else None),
        request_started_at=(
            NOW - timedelta(seconds=30) if source_state == "same_request_success" else None
        ),
        request_deadline_at=(
            NOW + timedelta(seconds=60) if source_state == "same_request_success" else None
        ),
    )


def _build(
    target: str,
    positions: tuple[StoredPosition, ...],
    facts: dict[str, tuple[BriefFact, ...]],
    *,
    as_of: datetime = NOW,
    snapshot_complete: bool = True,
    source_state: str = "same_request_success",
    observed_at: datetime | None = None,
) -> D2RelationshipSet:
    return build_d2_relationships(
        target,
        _binding(
            positions,
            snapshot_complete=snapshot_complete,
            source_state=source_state,
            observed_at=observed_at,
        ),
        {code: _set(code, values) for code, values in facts.items()},
        as_of,
        request_id=REQUEST_ID,
        request_mode=RequestMode.RAPID,
    )


def test_position_multi_account_and_sibling_economic_aggregation_is_private_safe() -> None:
    positions = (
        _position("100001", "10", account=PRIVATE_ACCOUNT),
        _position(
            "100001",
            "10",
            account="second-private-account",
            observed_at=NOW - timedelta(minutes=1),
        ),
        _position("100002", "20"),
        _position("200001", "60"),
    )
    facts = {
        "100001": _facts(
            "100001",
            company="甲公司",
            manager="张三",
            benchmark="沪深300",
            sibling="100002",
            sibling_complete=True,
        ),
        "100002": _facts(
            "100002",
            company="甲公司",
            manager="张三",
            benchmark="沪深300",
            sibling="100001",
            sibling_complete=True,
        ),
        "200001": _facts("200001", company="乙公司", manager="李四", benchmark="中证500"),
    }

    result = _build("100001", positions, facts)

    assert result.position_present is True
    assert result.target_portfolio_weight == "0.2"
    assert result.economic_exposure_weight == "0.4"
    assert result.economic_exposure_hhi == "0.52"
    assert any(item.relationship_type == "share_class_sibling" for item in result.relationships)
    assert any(
        item.relationship_type == "duplicate_holding_identity"
        and item.fund_codes == ("100001",)
        and item.metrics == {"multiple_observations": True}
        for item in result.relationships
    )
    assert "authenticated_index_identity_100001" in result.missing_fields
    assert "authenticated_index_identity_100001" in result.coverage.unknown_fields
    assert result.coverage.evidence_state is not BriefEvidenceState.COMPLETE
    rendered = repr(result)
    assert PRIVATE_ACCOUNT not in rendered
    assert str(PRIVATE_SHARES) not in rendered
    assert str(PRIVATE_PROFIT) not in rendered
    for relationship in result.relationships:
        metrics = repr(relationship.metrics)
        assert "weight" not in metrics
        assert "hhi" not in metrics
    assert result.coverage.known_percent is None


def test_sibling_aggregation_requires_mutual_authenticated_facts() -> None:
    positions = (_position("100001", "50"), _position("100002", "50"))
    facts = {
        "100001": _facts(
            "100001", company="甲公司", manager="张三", benchmark="沪深300", sibling="100002"
        ),
        "100002": _facts("100002", company="甲公司", manager="张三", benchmark="沪深300"),
    }

    result = _build("100001", positions, facts)

    assert result.target_portfolio_weight == "0.5"
    assert result.economic_exposure_weight == "0.5"
    assert not any(item.relationship_type == "share_class_sibling" for item in result.relationships)
    assert "share_class_sibling_unconfirmed_100002" in result.missing_fields


def test_fund_scoped_evidence_closes_snapshot_references_and_is_order_stable() -> None:
    positions = (_position("100001", "50"), _position("200001", "50"))
    target = _facts("100001", company="甲公司", manager="张三", benchmark="沪深300")
    candidate = _facts("200001", company="甲公司", manager="张三", benchmark="沪深300")

    first = _build("100001", positions, {"100001": target, "200001": candidate})
    second = _build(
        "100001",
        tuple(reversed(positions)),
        {"200001": tuple(reversed(candidate)), "100001": tuple(reversed(target))},
    )

    assert first == second
    projected_ids = {item.fact_id for item in first.evidence_facts}
    candidate_ids = {item for item in projected_ids if item.startswith("fund_200001_")}
    assert candidate_ids
    assert all(len(item) <= 64 for item in candidate_ids)
    all_evidence_ids = projected_ids
    assert all(
        set(relationship.evidence_ids) <= all_evidence_ids
        and any(item.startswith("fund_200001_") for item in relationship.evidence_ids)
        and any(not item.startswith("fund_") for item in relationship.evidence_ids)
        for relationship in first.relationships
        if len(relationship.fund_codes) == 2
    )
    for projected in first.evidence_facts:
        originals = target if not projected.fact_id.startswith("fund_") else candidate
        original = next(item for item in originals if item.field_id == projected.field_id)
        assert replace(projected, fact_id=original.fact_id) == original


def test_index_requires_exact_active_benchmark_not_family_similarity() -> None:
    positions = (
        _position("100001", "34"),
        _position("200001", "33"),
        _position("300001", "33"),
    )
    facts = {
        "100001": _facts("100001", company="甲", manager="甲", benchmark="沪深300指数"),
        "200001": _facts("200001", company="乙", manager="乙", benchmark="沪深300指数"),
        "300001": _facts("300001", company="丙", manager="丙", benchmark="沪深300增强指数"),
    }

    result = _build("100001", positions, facts)

    same_index_codes = {
        item.fund_codes
        for item in result.relationships
        if item.relationship_type == "same_current_benchmark"
    }
    assert same_index_codes == {("100001", "200001")}
    assert ("100001", "300001") not in same_index_codes
    matched = next(
        item for item in result.relationships if item.relationship_type == "same_current_benchmark"
    )
    assert matched.evidence_state is BriefEvidenceState.PARTIAL
    assert not any(
        item.relationship_type == "same_index"
        and item.evidence_state is BriefEvidenceState.COMPLETE
        for item in result.relationships
    )


def test_manager_and_company_relationships_are_independent() -> None:
    positions = (
        _position("100001", "34"),
        _position("200001", "33"),
        _position("300001", "33"),
    )
    facts = {
        "100001": _facts("100001", company="甲公司", manager="张三", benchmark="基准甲"),
        "200001": _facts("200001", company="乙公司", manager="张三", benchmark="基准乙"),
        "300001": _facts("300001", company="甲公司", manager="李四", benchmark="基准丙"),
    }

    result = _build("100001", positions, facts)

    by_type = {
        item.relationship_type: item.fund_codes
        for item in result.relationships
        if item.relationship_type in {"same_manager", "same_company"}
    }
    assert by_type == {
        "same_manager": ("100001", "200001"),
        "same_company": ("100001", "300001"),
    }


def test_effective_benchmark_conflict_is_unknown_with_exact_code() -> None:
    positions = (_position("100001", "50"), _position("200001", "50"))
    target = _facts("100001", company="甲", manager="甲", benchmark="沪深300")
    candidate = _facts("200001", company="乙", manager="乙", benchmark="沪深300")
    conflicting = _fact(
        "200001",
        "current_benchmark",
        {
            "description": "中证500",
            "effective_from": "2025-06-01",
            "effective_to": None,
        },
        fact_id="current_benchmark_2",
    )

    result = _build("100001", positions, {"100001": target, "200001": candidate + (conflicting,)})

    assert "benchmark_effective_date_conflict_200001" in result.conflicts
    assert not any(
        item.relationship_type == "same_current_benchmark" for item in result.relationships
    )
    assert "current_benchmark_200001" in result.coverage.unknown_fields


def test_missing_nav_and_portfolio_freshness_fail_closed() -> None:
    facts = {"100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲")}

    missing_nav = _build(
        "100001", (_position("100001", "10"), _position("100001", "1", nav=None)), facts
    )
    assert missing_nav.target_portfolio_weight is None
    assert missing_nav.economic_exposure_hhi is None
    assert missing_nav.position_present is True
    assert "portfolio_nav_missing" in missing_nav.missing_fields
    assert missing_nav.coverage.evidence_state is BriefEvidenceState.INSUFFICIENT
    with pytest.raises(ValueError, match="valuation"):
        replace(missing_nav, target_portfolio_weight="1").validate()

    dated_at = NOW - timedelta(days=2)
    dated = _build(
        "100001",
        (_position("100001", "10", observed_at=dated_at),),
        facts,
        observed_at=dated_at,
        source_state="authenticated_cache",
    )
    assert dated.target_portfolio_weight == "1"
    assert dated.coverage.evidence_state is BriefEvidenceState.PARTIAL
    assert "portfolio_observation_dated" in dated.warnings

    expired_at = NOW - timedelta(days=31)
    expired = _build(
        "100001",
        (_position("100001", "10", observed_at=expired_at),),
        facts,
        observed_at=expired_at,
        source_state="authenticated_cache",
    )
    assert expired.target_portfolio_weight is None
    assert expired.coverage.evidence_state is BriefEvidenceState.INSUFFICIENT
    assert "personal_position_observation" in expired.missing_fields

    future_at = NOW + timedelta(seconds=1)
    future = _build(
        "100001",
        (_position("100001", "10", observed_at=future_at),),
        facts,
        observed_at=future_at,
    )
    assert future.target_portfolio_weight is None
    assert "portfolio_observation_future" in future.conflicts


def test_fresh_empty_is_known_absent_and_unbound_snapshot_is_unknown() -> None:
    facts = {"100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲")}

    absent = _build("100001", (), facts)
    assert absent.position_present is False
    assert absent.target_portfolio_weight is None
    assert "personal_position_observation" not in absent.missing_fields

    unbound = _build(
        "100001",
        (_position("100001", "10"), _position("200001", "10")),
        {
            **facts,
            "200001": _facts("200001", company="甲", manager="甲", benchmark="基准甲"),
        },
        source_state="unbound",
    )
    assert unbound.position_present is None
    assert unbound.valuation_available is False
    assert unbound.target_portfolio_weight is None
    assert unbound.economic_exposure_hhi is None
    assert unbound.coverage.evidence_state is BriefEvidenceState.INSUFFICIENT
    assert "personal_position_observation" in unbound.missing_fields
    assert unbound.relationships == ()
    assert unbound.evidence_facts == ()

    cached = _build(
        "100001",
        (_position("100001", "10"),),
        facts,
        source_state="authenticated_cache",
    )
    assert cached.target_portfolio_weight == "1"
    assert cached.coverage.evidence_state is BriefEvidenceState.PARTIAL
    assert "portfolio_observation_cached" in cached.warnings
    with pytest.raises(ValueError, match="provenance"):
        replace(
            cached,
            portfolio_evidence_state="current",
            warnings=(),
        ).validate()
    erased_oldest = replace(
        cached.portfolio_provenance,
        oldest_position_observed_at=None,
    )
    with pytest.raises(ValueError, match="MAC"):
        replace(
            cached,
            portfolio_evidence_state="current",
            portfolio_provenance=erased_oldest,
            warnings=(),
        ).validate()


def test_fact_map_key_must_equal_source_linked_set_fund_code() -> None:
    facts = _facts("100001", company="甲", manager="甲", benchmark="基准甲")

    with pytest.raises(ValueError, match="fund code"):
        build_d2_relationships(
            "100001",
            _binding((_position("100001", "10"),)),
            {"999999": _set("100001", facts)},
            NOW,
            request_id=REQUEST_ID,
            request_mode=RequestMode.RAPID,
        )


def test_empty_duplicate_and_conflicting_facts_return_bounded_unknowns() -> None:
    positions = (_position("100001", "50"), _position("200001", "50"))
    empty = _build("100001", positions, {})
    assert empty.relationships == ()
    assert empty.evidence_facts == ()
    assert empty.coverage.evidence_state is BriefEvidenceState.INSUFFICIENT
    assert "identity_evidence_missing_100001" in empty.missing_fields

    target = _facts("100001", company="甲", manager="甲", benchmark="基准甲")
    duplicate = replace(target[0], value={"fund_code": "100001", "fund_company": "乙"})
    candidate = _facts("200001", company="丙", manager="丙", benchmark="基准丙")
    result = _build(
        "100001",
        positions,
        {"100001": target + (duplicate,), "200001": candidate},
    )
    assert "d2_fact_id_duplicate_100001" in result.conflicts
    assert "d2_fact_set_invalid_100001" in result.conflicts
    assert "identity_evidence_missing_100001" in result.missing_fields
    assert result.coverage.evidence_state is not BriefEvidenceState.COMPLETE


def test_result_contract_rejects_private_or_decimal_mutation() -> None:
    result = _build(
        "100001",
        (_position("100001", str(PRIVATE_SHARES), account=PRIVATE_ACCOUNT),),
        {"100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲")},
    )
    assert type(result) is D2RelationshipSet
    result.validate()
    assert PRIVATE_ACCOUNT not in repr(result)
    assert str(PRIVATE_SHARES) not in repr(result)
    assert str(PRIVATE_PROFIT) not in repr(result)
    assert not isinstance(result.target_portfolio_weight, Decimal)
    assert not isinstance(result.economic_exposure_hhi, Decimal)


def test_estimated_nav_downgrades_and_stale_or_mismatched_facts_do_not_relate() -> None:
    target = _facts("100001", company="甲", manager="张三", benchmark="沪深300")
    candidate = _facts("200001", company="甲", manager="张三", benchmark="沪深300")
    stale_candidate = tuple(replace(item, freshness=EvidenceFreshness.STALE) for item in candidate)
    estimated = replace(
        _position("100001", "50"),
        formal_nav=None,
        estimated_nav=Decimal("1"),
    )

    stale = _build(
        "100001",
        (estimated, _position("200001", "50")),
        {"100001": target, "200001": stale_candidate},
    )
    assert stale.coverage.evidence_state is BriefEvidenceState.PARTIAL
    assert "portfolio_estimated_nav_used" in stale.warnings
    assert not any(
        item.relationship_type in {"same_manager", "same_company", "same_current_benchmark"}
        for item in stale.relationships
    )
    assert "current_manager_team_evidence_stale_200001" in stale.missing_fields

    mismatched_identity = replace(
        candidate[0],
        value={"fund_code": "999999", "fund_company": "甲"},
    )
    mismatch = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {"100001": target, "200001": (mismatched_identity,) + candidate[1:]},
    )
    assert "identity_subject_conflict_200001" in mismatch.conflicts
    assert not any(
        item.relationship_type
        in {
            "same_company",
            "same_manager",
            "same_current_benchmark",
            "share_class_sibling",
        }
        and "200001" in item.fund_codes
        for item in mismatch.relationships
    )


def test_sibling_conflict_or_staleness_blocks_union_and_sibling_only_is_exposure() -> None:
    target = _facts(
        "100001",
        company="甲",
        manager="张三",
        benchmark="沪深300",
        sibling="100002",
        sibling_complete=True,
    )
    sibling = _facts(
        "100002",
        company="甲",
        manager="张三",
        benchmark="沪深300",
        sibling="100001",
        sibling_complete=True,
    )
    stale_share = replace(sibling[3], freshness=EvidenceFreshness.STALE)
    blocked = _build(
        "100001",
        (_position("100002", "10"),),
        {"100001": target, "100002": sibling[:3] + (stale_share,)},
    )
    assert blocked.position_present is False
    assert blocked.target_portfolio_weight == "0"
    assert blocked.economic_exposure_weight == "0"
    assert not any(
        item.relationship_type == "share_class_sibling" for item in blocked.relationships
    )

    partial_target = target[:3] + (replace(target[3], completeness=EvidenceCompleteness.PARTIAL),)
    partial_sibling = sibling[:3] + (
        replace(sibling[3], completeness=EvidenceCompleteness.PARTIAL),
    )
    partial = _build(
        "100001",
        (_position("100002", "10"),),
        {"100001": partial_target, "100002": partial_sibling},
    )
    partial_relation = next(
        item for item in partial.relationships if item.relationship_type == "share_class_sibling"
    )
    assert partial_relation.evidence_state is BriefEvidenceState.PARTIAL
    assert partial_relation.metrics["aggregation_eligible"] is False
    assert partial.economic_exposure_weight == "0"
    assert "share_class_sibling_not_authenticated_100002" in partial.missing_fields

    authenticated = _build(
        "100001",
        (_position("100002", "10"),),
        {"100001": target, "100002": sibling},
    )
    assert authenticated.position_present is False
    assert authenticated.target_portfolio_weight == "0"
    assert authenticated.economic_exposure_weight == "1"
    assert authenticated.economic_exposure_hhi == "1"
    assert authenticated.coverage.included_fund_codes == ("100002",)


def test_incomplete_snapshot_is_unknown_but_account_times_may_precede_binding() -> None:
    facts = {"100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲")}
    positions = (
        _position("100001", "5", observed_at=NOW - timedelta(minutes=2)),
        _position("100001", "5", observed_at=NOW),
    )

    complete = _build("100001", positions, facts, observed_at=NOW)
    assert complete.position_present is True
    assert complete.target_portfolio_weight == "1"

    incomplete = _build(
        "100001",
        positions,
        facts,
        observed_at=NOW,
        snapshot_complete=False,
    )
    assert incomplete.position_present is None
    assert incomplete.target_portfolio_weight is None
    assert "personal_position_observation" in incomplete.missing_fields


def test_only_current_effective_benchmark_participates() -> None:
    positions = (_position("100001", "50"), _position("200001", "50"))
    target = _facts("100001", company="甲", manager="甲", benchmark="沪深300")
    candidate = _facts("200001", company="乙", manager="乙", benchmark="沪深300")
    historical = replace(
        candidate[2],
        fact_id="current_benchmark_historical",
        value={
            "description": "历史其他基准",
            "effective_from": "2020-01-01",
            "effective_to": "2024-12-31",
        },
    )

    result = _build(
        "100001",
        positions,
        {"100001": target, "200001": candidate + (historical,)},
    )
    assert any(item.relationship_type == "same_current_benchmark" for item in result.relationships)
    assert "benchmark_effective_date_conflict_200001" not in result.conflicts


def test_supporting_fact_budget_preserves_snapshot_fact_bound() -> None:
    positions = [_position("100001", "1")]
    fact_sets = {"100001": _facts("100001", company="甲", manager="甲", benchmark="共同基准")}
    for number in range(200001, 200051):
        code = str(number)
        positions.append(_position(code, "1"))
        fact_sets[code] = _facts(code, company="甲", manager="甲", benchmark="共同基准")

    result = _build("100001", tuple(positions), fact_sets)

    target_fact_ids = {item.fact_id for item in fact_sets["100001"]}
    merged_ids = target_fact_ids | {item.fact_id for item in result.evidence_facts}
    assert len(merged_ids) <= MAX_FACTS
    assert all(
        set(item.evidence_ids) <= {fact.fact_id for fact in result.evidence_facts}
        for item in result.relationships
    )
    assert "d2_fact_budget_reached" in result.warnings


def test_fact_budget_cannot_change_economic_exposure_without_sibling_evidence() -> None:
    target_base = _facts(
        "100001",
        company="甲",
        manager="甲",
        benchmark="共同基准",
        sibling="100002",
        sibling_complete=True,
    )
    fillers = tuple(
        replace(
            target_base[0],
            fact_id=f"other_fact_{index}",
            field_id="other_public_fact",
            value={"label": str(index)},
            source_lineage_id=f"other_lineage_{index}",
        )
        for index in range(124)
    )
    sibling = _facts(
        "100002",
        company="甲",
        manager="甲",
        benchmark="共同基准",
        sibling="100001",
        sibling_complete=True,
    )

    result = _build(
        "100001",
        (_position("100001", "50"), _position("100002", "50")),
        {"100001": target_base + fillers, "100002": sibling},
    )

    assert result.target_portfolio_weight == "0.5"
    assert result.economic_exposure_weight == "0.5"
    assert not any(item.relationship_type == "share_class_sibling" for item in result.relationships)
    assert "d2_fact_budget_reached" in result.warnings


def test_relationship_metrics_are_exact_per_type() -> None:
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="共同基准"),
            "200001": _facts("200001", company="甲", manager="甲", benchmark="共同基准"),
        },
    )
    relationship = next(item for item in result.relationships if item.evidence_ids)
    invalid = replace(relationship, metrics={**dict(relationship.metrics), "weight": "0.5"})

    with pytest.raises(ValueError, match="metrics"):
        replace(result, relationships=(invalid,)).validate()

    wrong_value = replace(
        relationship,
        metrics={key: False for key in relationship.metrics},
    )
    with pytest.raises(ValueError, match="semantics"):
        replace(result, relationships=(wrong_value,)).validate()

    missing_side = replace(relationship, evidence_ids=relationship.evidence_ids[:1])
    with pytest.raises(ValueError, match="semantics"):
        replace(result, relationships=(missing_side,)).validate()

    manager = next(
        item for item in result.relationships if item.relationship_type == "same_manager"
    )
    escalated = replace(manager, evidence_state=BriefEvidenceState.COMPLETE)
    with pytest.raises(ValueError, match="evidence state"):
        replace(result, relationships=(escalated,)).validate()

    manager = next(
        item for item in result.relationships if item.relationship_type == "same_manager"
    )
    identity_ids = tuple(
        item.fact_id for item in result.evidence_facts if item.field_id == "identity_active_status"
    )
    wrong_evidence = replace(manager, evidence_ids=identity_ids)
    with pytest.raises(ValueError, match="evidence fields"):
        replace(result, relationships=(wrong_evidence,)).validate()


def test_portfolio_request_window_row_freshness_and_finite_numbers_fail_closed() -> None:
    facts = {
        "100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲"),
        "200001": _facts("200001", company="乙", manager="乙", benchmark="基准乙"),
    }
    old_at = NOW - timedelta(hours=23)
    old_request = PortfolioEvidenceBinding(
        positions=(_position("100001", "10", observed_at=old_at),),
        snapshot_complete=True,
        observation_version="portfolio_snapshot_old",
        observed_at=old_at,
        source_state="same_request_success",
        request_id=REQUEST_ID,
        request_mode=RequestMode.RAPID,
        request_started_at=old_at - timedelta(seconds=30),
        request_deadline_at=old_at + timedelta(seconds=60),
    )
    request_mismatch = build_d2_relationships(
        "100001",
        old_request,
        {code: _set(code, values) for code, values in facts.items()},
        NOW,
        request_id=REQUEST_ID,
        request_mode=RequestMode.RAPID,
    )
    assert request_mismatch.position_present is None
    assert request_mismatch.valuation_available is False
    assert "portfolio_request_binding_invalid" in request_mismatch.conflicts
    with pytest.raises(ValueError, match="unknown D2"):
        replace(request_mismatch, target_portfolio_weight="1").validate()

    mode_mismatch = build_d2_relationships(
        "100001",
        _binding((_position("100001", "10"),)),
        {code: _set(code, values) for code, values in facts.items()},
        NOW,
        request_id=REQUEST_ID,
        request_mode=RequestMode.DEEP,
    )
    assert mode_mismatch.position_present is None
    assert mode_mismatch.valuation_available is False
    assert "portfolio_request_binding_invalid" in mode_mismatch.conflicts

    stale_row = _build(
        "100001",
        (
            _position("100001", "10"),
            _position("200001", "10", observed_at=NOW - timedelta(days=365)),
        ),
        facts,
    )
    assert stale_row.position_present is None
    assert "position_observation_stale" in stale_row.missing_fields

    nonfinite = replace(_position("100001", "10"), shares=Decimal("Infinity"))
    with pytest.raises(ValueError, match="shares"):
        _build("100001", (nonfinite,), {"100001": facts["100001"]})


def test_coverage_included_codes_resolve_to_snapshot_facts_without_a_match() -> None:
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲"),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="基准乙"),
        },
    )

    assert result.coverage.included_fund_codes == ("100001", "200001")
    assert result.coverage.evidence_ids
    evidence_by_id = {item.fact_id: item for item in result.evidence_facts}
    assert set(result.coverage.evidence_ids) <= set(evidence_by_id)
    assert {evidence_by_id[item].field_id for item in result.coverage.evidence_ids} == {
        "identity_active_status",
        "current_manager_team",
        "current_benchmark",
    }

    unknown_subject = replace(
        result.coverage,
        included_fund_codes=("999999",),
        omitted_fund_codes=result.held_fund_codes,
    )
    with pytest.raises(ValueError, match="partition"):
        replace(result, coverage=unknown_subject).validate()

    promoted = replace(result.coverage, evidence_state=BriefEvidenceState.COMPLETE)
    with pytest.raises(ValueError, match="coverage state"):
        replace(result, coverage=promoted).validate()

    unsupported = replace(result.coverage, evidence_ids=())
    with pytest.raises(ValueError, match="coverage evidence"):
        replace(result, coverage=unsupported).validate()

    hidden_unknowns = replace(result.coverage, unknown_fields=())
    with pytest.raises(ValueError, match="unknown fields"):
        replace(result, coverage=hidden_unknowns).validate()

    with pytest.raises(ValueError, match="position presence"):
        replace(result, position_present=False).validate()


def test_non_target_siblings_are_scoped_and_audited_before_hhi_aggregation() -> None:
    result = _build(
        "100001",
        (
            _position("100001", "20"),
            _position("200001", "40"),
            _position("200002", "40"),
        ),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="基准甲"),
            "200001": _facts(
                "200001",
                company="乙",
                manager="乙",
                benchmark="基准乙",
                sibling="200002",
                sibling_complete=True,
            ),
            "200002": _facts(
                "200002",
                company="乙",
                manager="乙",
                benchmark="基准乙",
                sibling="200001",
                sibling_complete=True,
            ),
        },
    )

    sibling = next(
        item
        for item in result.relationships
        if item.relationship_type == "share_class_sibling"
        and item.fund_codes == ("200001", "200002")
    )
    assert all(item.startswith("fund_20") for item in sibling.evidence_ids)
    assert result.economic_exposure_hhi == "0.68"
    assert result.largest_economic_exposure_weight == "0.8"


def test_same_request_window_is_bounded_by_rapid_or_deep_policy() -> None:
    with pytest.raises(ValueError, match="request window"):
        PortfolioEvidenceBinding(
            positions=(_position("100001", "10", observed_at=NOW - timedelta(hours=23)),),
            snapshot_complete=True,
            observation_version="portfolio_snapshot_too_long",
            observed_at=NOW - timedelta(hours=23),
            source_state="same_request_success",
            request_id=REQUEST_ID,
            request_mode=RequestMode.RAPID,
            request_started_at=NOW - timedelta(hours=23),
            request_deadline_at=NOW + timedelta(minutes=1),
        ).validate()
