from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.brief.d2 import (
    AdjustedReturnSeriesEvidence,
    D2RelationshipSet,
    PortfolioEvidenceBinding,
    build_d2_relationships,
)
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import BriefEvidenceState, BriefFact
from kunjin.brief.nav import _seal_validated_adjusted_nav_series
from kunjin.brief.policy import MAX_FACTS
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceTier,
)
from kunjin.decision.source_registry import SOURCE_REGISTRY_V1_CHECKSUM
from kunjin.decision.store import DecisionAuditStore
from kunjin.models import FundNavObservation, StoredPosition
from kunjin.storage.repository import Repository

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
    adjusted_series_by_fund: object = None,
    decision_audit_store: DecisionAuditStore | None = None,
) -> D2RelationshipSet:
    optional = (
        {}
        if adjusted_series_by_fund is None
        else {"adjusted_series_by_fund": adjusted_series_by_fund}
    )
    if decision_audit_store is not None:
        optional["decision_audit_store"] = decision_audit_store
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
        **optional,
    )


def _holding_fact(
    code: str,
    *,
    report_period: date = date(2026, 6, 30),
    published_at: datetime = NOW - timedelta(days=2),
    scope: str = "top10",
    items: tuple[dict[str, object], ...],
    freshness: EvidenceFreshness = EvidenceFreshness.CURRENT,
    conflict_ids: tuple[str, ...] = (),
    fact_id: str = "disclosed_holdings_1",
) -> BriefFact:
    fact = _fact(
        code,
        "holdings_industries",
        {
            "report_period": report_period.isoformat(),
            "disclosure_scope": (scope,),
            "items": items,
        },
        fact_id=fact_id,
        conflict_ids=conflict_ids,
    )
    return replace(
        fact,
        data_as_of=datetime.combine(report_period, datetime.min.time(), tzinfo=timezone.utc),
        published_at=published_at,
        freshness=freshness,
        completeness=(
            EvidenceCompleteness.PARTIAL if scope == "top10" else EvidenceCompleteness.COMPLETE
        ),
    )


def _holding_item(
    security_code: str,
    security_name: str,
    disclosed_weight: str,
    *,
    rank: str = "1",
    asset_class: str = "stock",
) -> dict[str, object]:
    return {
        "rank": rank,
        "security_code": security_code,
        "security_name": security_name,
        "asset_class": asset_class,
        "disclosed_weight": disclosed_weight,
    }


def _adjusted_fact(code: str, observations: tuple[FundNavObservation, ...]) -> BriefFact:
    fact = _fact(
        code,
        "adjusted_return_series",
        {
            "fund_code": code,
            "sample_count": str(len(observations)),
            "start_date": observations[0].nav_date.isoformat(),
            "end_date": observations[-1].nav_date.isoformat(),
            "corporate_action_state": "none",
            "calculation_version": "1",
            "source_attempt_id": str(observations[0].source_attempt_id),
        },
        fact_id="adjusted_return_series_1",
    )
    return replace(
        fact,
        source_id="eastmoney_nav",
        canonical_url=f"https://fund.eastmoney.com/{code}.html",
        completeness=EvidenceCompleteness.COMPLETE,
        calculated=True,
        data_as_of=datetime.combine(
            observations[-1].nav_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
        source_lineage_id=f"source_attempt_{observations[0].source_attempt_id}",
    )


def _adjusted_rows(
    code: str,
    *,
    count: int = 61,
    scale: str = "1",
    accumulated: bool = True,
    corporate_action_state: str = "none",
    duplicate_last_date: bool = False,
    flat: bool = False,
    start_offset_days: int = 0,
    source_attempt_id: int = 1,
) -> tuple[FundNavObservation, ...]:
    rows = []
    factor = Decimal(scale)
    previous_nav: Decimal | None = None
    for index in range(count):
        nav_date = date(2026, 1, 1) + timedelta(days=start_offset_days + index)
        if duplicate_last_date and index == count - 1:
            nav_date -= timedelta(days=1)
        accumulated_nav = (
            Decimal("1")
            if flat
            else factor * (Decimal("1") + Decimal(index * index) / Decimal("10000"))
        )
        daily_growth = (
            Decimal("0")
            if previous_nav is None
            else (accumulated_nav / previous_nav - Decimal("1")) * Decimal("100")
        )
        rows.append(
            FundNavObservation(
                fund_code=code,
                nav_date=nav_date,
                unit_nav=accumulated_nav,
                accumulated_nav=(accumulated_nav if accumulated else None),
                daily_growth=daily_growth,
                source="eastmoney",
                retrieved_at=NOW,
                corporate_action_state=corporate_action_state,
                source_attempt_id=source_attempt_id,
            )
        )
        previous_nav = accumulated_nav
    return tuple(rows)


def _series(code: str, rows: tuple[FundNavObservation, ...]):
    return AdjustedReturnSeriesEvidence(
        fund_code=code,
        series=_seal_validated_adjusted_nav_series(
            fund_code=code,
            observations=rows,
            source_attempt_id=rows[0].source_attempt_id or 0,
            retrieved_at=rows[0].retrieved_at,
            data_as_of=rows[-1].nav_date,
        ),
        evidence_fact=_adjusted_fact(code, rows),
    )


def _adjusted_audit_store(
    tmp_path,
    data_as_of_by_code: dict[str, date],
    *,
    finished_at: datetime = NOW,
) -> tuple[DecisionAuditStore, dict[str, int]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    repository = Repository(tmp_path / "d2-audit.db")
    repository.migrate()
    store = DecisionAuditStore(repository)
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="a" * 32,
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW - timedelta(seconds=30),
    )
    request_run_id = store.begin_request(budget)
    attempt_ids = {}
    for code, data_as_of in sorted(data_as_of_by_code.items()):
        attempt_ids[code] = store.record_source_attempt(
            request_run_id,
            SourceAttempt(
                source_id="eastmoney_nav",
                field_id="formal_nav",
                subject_key=f"fund:{code}",
                attempt_number=1,
                outcome=SourceAttemptOutcome.SUCCESS,
                started_at=NOW - timedelta(seconds=1),
                finished_at=finished_at,
                data_as_of=datetime.combine(
                    data_as_of,
                    datetime.min.time(),
                    tzinfo=timezone.utc,
                ),
                error_code=None,
                cooldown_until=None,
                force_actor=None,
                force_reason=None,
                registry_version="1",
                registry_checksum=SOURCE_REGISTRY_V1_CHECKSUM,
                response_bytes=1,
            ),
        )
    return store, attempt_ids


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

    assert len(result.evidence_facts) <= MAX_FACTS
    assert all(
        set(item.evidence_ids) <= {fact.fact_id for fact in result.evidence_facts}
        for item in result.relationships
    )
    assert "d2_fact_budget_reached" in result.warnings


def test_irrelevant_target_facts_do_not_consume_projection_budget() -> None:
    target_base = _facts("100001", company="甲", manager="甲", benchmark="基准甲")
    irrelevant = tuple(
        _fact(
            "100001",
            "irrelevant_public_fact",
            {"label": f"value_{index}"},
            fact_id=f"irrelevant_public_fact_{index}",
        )
        for index in range(MAX_FACTS - len(target_base))
    )
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": target_base + irrelevant,
            "200001": _facts("200001", company="乙", manager="乙", benchmark="基准乙"),
        },
    )

    assert result.coverage.included_fund_codes == ("100001", "200001")
    assert "d2_fact_budget_reached" not in result.warnings
    assert len(result.evidence_facts) <= MAX_FACTS


def test_fact_budget_uses_projected_sibling_evidence_for_economic_exposure() -> None:
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
    assert result.economic_exposure_weight == "1"
    assert any(item.relationship_type == "share_class_sibling" for item in result.relationships)
    assert "d2_fact_budget_reached" not in result.warnings


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


def test_holdings_overlap_preserves_top10_coverage_shared_min_and_scoped_evidence() -> None:
    left_published = NOW - timedelta(days=2)
    right_published = NOW - timedelta(days=1)
    left_holding = _holding_fact(
        "100001",
        published_at=left_published,
        items=(
            _holding_item("600000", "浦发银行", "5"),
            _holding_item("600519", "贵州茅台", "3", rank="2"),
        ),
    )
    right_holding = _holding_fact(
        "200001",
        published_at=right_published,
        items=(
            _holding_item("600000", "浦发银行", "2"),
            _holding_item("000001", "平安银行", "4", rank="2"),
        ),
    )
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲")
            + (left_holding,),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙")
            + (right_holding,),
        },
    )

    overlap = next(
        item for item in result.relationships if item.relationship_type == "top10_disclosed_overlap"
    )
    assert overlap.evidence_state is BriefEvidenceState.PARTIAL
    assert overlap.fund_codes == ("100001", "200001")
    assert overlap.report_periods == (date(2026, 6, 30), date(2026, 6, 30))
    assert overlap.publication_times == (left_published, right_published)
    assert overlap.metrics == {
        "calculation_version": "1",
        "left_scope": "top10",
        "right_scope": "top10",
        "left_disclosed_percent": "8",
        "right_disclosed_percent": "6",
        "left_unknown_percent": "92",
        "right_unknown_percent": "94",
        "overlap_percent": "2",
        "shared_exposures": (
            {
                "asset_class": "stock",
                "security_code": "600000",
                "security_name": "浦发银行",
                "left_disclosed_percent": "5",
                "right_disclosed_percent": "2",
                "shared_percent": "2",
            },
        ),
    }
    assert "top10_scope_is_partial" in overlap.warnings
    supporting = {item.fact_id: item for item in result.evidence_facts}
    assert len(overlap.evidence_ids) == 2
    assert all(supporting[item].field_id == "holdings_industries" for item in overlap.evidence_ids)
    assert any(item.startswith("fund_200001_") for item in overlap.evidence_ids)
    assert result.holdings_coverage.scope == "disclosed_holdings_overlap"
    assert result.holdings_coverage.included_fund_codes == ("100001", "200001")
    assert result.holdings_coverage.omitted_fund_codes == ()
    assert result.holdings_coverage.known_percent is None
    assert set(result.holdings_coverage.evidence_ids) == set(overlap.evidence_ids)
    assert result.holdings_coverage is not result.coverage

    tampered_metrics = dict(overlap.metrics)
    tampered_metrics["left_unknown_percent"] = "0"
    with pytest.raises(ValueError, match="overlap metrics"):
        replace(
            result,
            relationships=(replace(overlap, metrics=tampered_metrics),),
        ).validate()


def test_holdings_overlap_adjacent_complete_period_warns_and_cross_quarter_is_unknown() -> None:
    left = _holding_fact(
        "100001",
        scope="complete",
        report_period=date(2026, 6, 30),
        items=(_holding_item("600000", "浦发银行", "5"),),
    )
    adjacent = _holding_fact(
        "200001",
        scope="complete",
        report_period=date(2026, 3, 31),
        items=(_holding_item("600000", "浦发银行", "2"),),
    )
    facts = {
        "100001": _facts("100001", company="甲", manager="甲", benchmark="甲") + (left,),
        "200001": _facts("200001", company="乙", manager="乙", benchmark="乙") + (adjacent,),
    }
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        facts,
    )
    overlap = next(
        item for item in result.relationships if item.relationship_type == "disclosed_overlap"
    )
    assert overlap.report_periods == (date(2026, 6, 30), date(2026, 3, 31))
    assert "report_period_mismatch" in overlap.warnings

    too_old = replace(
        adjacent,
        value={
            **dict(adjacent.value),
            "report_period": "2025-09-30",
        },
        data_as_of=datetime(2025, 9, 30, tzinfo=timezone.utc),
    )
    unavailable = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {**facts, "200001": facts["200001"][:-1] + (too_old,)},
    )
    assert not any(
        item.relationship_type in {"top10_disclosed_overlap", "disclosed_overlap"}
        for item in unavailable.relationships
    )
    assert "holdings_report_period_unaligned_100001_200001" in unavailable.missing_fields
    assert unavailable.holdings_coverage.evidence_state is BriefEvidenceState.PARTIAL
    assert (
        "holdings_pair_comparability_100001_200001" in unavailable.holdings_coverage.unknown_fields
    )


def test_zero_top10_overlap_is_only_zero_inside_disclosed_scope() -> None:
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲")
            + (
                _holding_fact(
                    "100001",
                    items=(_holding_item("600000", "浦发银行", "5"),),
                ),
            ),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙")
            + (
                _holding_fact(
                    "200001",
                    items=(_holding_item("000001", "平安银行", "4"),),
                ),
            ),
        },
    )
    overlap = next(
        item for item in result.relationships if item.relationship_type == "top10_disclosed_overlap"
    )
    assert overlap.metrics["overlap_percent"] == "0"
    assert overlap.metrics["left_unknown_percent"] == "95"
    assert overlap.metrics["right_unknown_percent"] == "96"
    assert overlap.evidence_state is BriefEvidenceState.PARTIAL
    assert "top10_scope_is_partial" in overlap.warnings


def test_invalid_holdings_are_omitted_and_never_zero_filled() -> None:
    valid = _holding_fact(
        "100001",
        items=(_holding_item("600000", "浦发银行", "5"),),
    )
    candidate_base = _facts("200001", company="乙", manager="乙", benchmark="乙")
    variants = (
        ((), "holdings_evidence_missing_200001"),
        (
            (
                replace(
                    _holding_fact(
                        "200001",
                        items=(_holding_item("600000", "浦发银行", "2"),),
                    ),
                    freshness=EvidenceFreshness.STALE,
                ),
            ),
            "holdings_evidence_stale_200001",
        ),
        (
            (
                replace(
                    _holding_fact(
                        "200001",
                        items=(_holding_item("600000", "浦发银行", "2"),),
                    ),
                    retrieved_at=NOW + timedelta(seconds=1),
                ),
            ),
            "holdings_evidence_future_200001",
        ),
        (
            (
                _holding_fact(
                    "200001",
                    items=(_holding_item("600000", "浦发银行", "2"),),
                    conflict_ids=("holding_conflict",),
                ),
            ),
            "holdings_evidence_conflict_200001",
        ),
        (
            (
                _holding_fact(
                    "200001",
                    items=({"rank": "1", "security_code": "600000"},),
                ),
            ),
            "holdings_evidence_malformed_200001",
        ),
        (
            (
                _holding_fact(
                    "200001",
                    items=(
                        _holding_item("600000", "浦发银行", "2"),
                        _holding_item("600000", "浦发银行", "1", rank="2"),
                    ),
                ),
            ),
            "holdings_duplicate_exposure_200001",
        ),
    )
    for candidate_holdings, expected_code in variants:
        result = _build(
            "100001",
            (_position("100001", "50"), _position("200001", "50")),
            {
                "100001": _facts("100001", company="甲", manager="甲", benchmark="甲") + (valid,),
                "200001": candidate_base + candidate_holdings,
            },
        )
        assert not any(
            item.relationship_type in {"top10_disclosed_overlap", "disclosed_overlap"}
            for item in result.relationships
        )
        assert expected_code in result.missing_fields + result.conflicts
        assert "200001" in result.holdings_coverage.omitted_fund_codes
        assert "holdings_industries_200001" in result.holdings_coverage.unknown_fields


def test_identity_conflict_blocks_holdings_overlap_even_with_valid_disclosure() -> None:
    target = _facts("100001", company="甲", manager="甲", benchmark="甲") + (
        _holding_fact(
            "100001",
            items=(_holding_item("600000", "浦发银行", "5"),),
        ),
    )
    candidate = _facts("200001", company="乙", manager="乙", benchmark="乙")
    mismatched_identity = replace(
        candidate[0],
        value={"fund_code": "999999", "fund_company": "乙"},
    )
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": target,
            "200001": (mismatched_identity,)
            + candidate[1:]
            + (
                _holding_fact(
                    "200001",
                    items=(_holding_item("600000", "浦发银行", "2"),),
                ),
            ),
        },
    )

    assert "identity_subject_conflict_200001" in result.conflicts
    assert not any(
        item.relationship_type in {"top10_disclosed_overlap", "disclosed_overlap"}
        for item in result.relationships
    )
    assert "200001" in result.holdings_coverage.omitted_fund_codes


def test_validated_adjusted_return_series_builds_separate_correlation_relationship(
    tmp_path,
) -> None:
    data_as_of = date(2026, 3, 2)
    audit_store, attempt_ids = _adjusted_audit_store(
        tmp_path,
        {"100001": data_as_of, "200001": data_as_of},
    )
    left_rows = _adjusted_rows("100001", source_attempt_id=attempt_ids["100001"])
    right_rows = _adjusted_rows("200001", scale="2", source_attempt_id=attempt_ids["200001"])
    left_series = _series("100001", left_rows)
    right_series = _series("200001", right_rows)
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
        },
        adjusted_series_by_fund={
            "100001": left_series,
            "200001": right_series,
        },
        decision_audit_store=audit_store,
    )
    correlation = next(
        item
        for item in result.relationships
        if item.relationship_type == "adjusted_return_correlation"
    )
    assert correlation.evidence_state is BriefEvidenceState.COMPLETE
    metrics = dict(correlation.metrics)
    calculation_binding_mac = metrics.pop("calculation_binding_mac")
    assert len(calculation_binding_mac) == 64
    assert metrics == {
        "aligned_observations": "61",
        "calculation_version": "1",
        "correlation": "1",
        "coverage": "full_input_alignment",
        "left_fund_code": "100001",
        "left_observations": "61",
        "left_series_binding_mac": left_series.series.binding_mac,
        "left_source_attempt_id": str(attempt_ids["100001"]),
        "right_fund_code": "200001",
        "right_observations": "61",
        "right_series_binding_mac": right_series.series.binding_mac,
        "right_source_attempt_id": str(attempt_ids["200001"]),
        "samples": "60",
        "start_date": left_rows[0].nav_date.isoformat(),
        "end_date": left_rows[-1].nav_date.isoformat(),
        "common_end_date": left_rows[-1].nav_date.isoformat(),
    }
    assert correlation.report_periods == ()
    assert len(correlation.evidence_ids) == 2
    assert all(
        next(item for item in result.evidence_facts if item.fact_id == evidence_id).field_id
        == "adjusted_return_series"
        for evidence_id in correlation.evidence_ids
    )
    assert not any(
        item.relationship_type in {"top10_disclosed_overlap", "disclosed_overlap"}
        for item in result.relationships
    )
    tampered_metrics = dict(correlation.metrics)
    tampered_metrics["correlation"] = "0.5"
    with pytest.raises(ValueError, match="semantics"):
        replace(
            result,
            relationships=(replace(correlation, metrics=tampered_metrics),),
        ).validate()


def test_adjusted_series_fact_must_bind_subject_batch_and_window(tmp_path) -> None:
    data_as_of = date(2026, 3, 2)
    audit_store, attempt_ids = _adjusted_audit_store(
        tmp_path,
        {"100001": data_as_of, "200001": data_as_of},
    )
    left = _series(
        "100001",
        _adjusted_rows("100001", source_attempt_id=attempt_ids["100001"]),
    )
    right = _series(
        "200001",
        _adjusted_rows(
            "200001",
            scale="2",
            source_attempt_id=attempt_ids["200001"],
        ),
    )
    with pytest.raises(ValueError, match="requires an exact audit store"):
        _build(
            "100001",
            (_position("100001", "50"), _position("200001", "50")),
            {
                "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
                "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
            },
            adjusted_series_by_fund={"100001": left, "200001": right},
        )
    forged_fact = replace(
        right.evidence_fact,
        value={**dict(right.evidence_fact.value), "sample_count": "999"},
    )
    result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
        },
        adjusted_series_by_fund={
            "100001": left,
            "200001": replace(right, evidence_fact=forged_fact),
        },
        decision_audit_store=audit_store,
    )

    assert not any(
        item.relationship_type == "adjusted_return_correlation" for item in result.relationships
    )
    assert "adjusted_return_evidence_binding_invalid_200001" in result.conflicts

    wrong_url = replace(
        right.evidence_fact,
        canonical_url="https://fund.eastmoney.com/100001.html",
    )
    wrong_url_result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
        },
        adjusted_series_by_fund={
            "100001": left,
            "200001": replace(right, evidence_fact=wrong_url),
        },
        decision_audit_store=audit_store,
    )
    assert not any(
        item.relationship_type == "adjusted_return_correlation"
        for item in wrong_url_result.relationships
    )
    assert "adjusted_return_evidence_binding_invalid_200001" in wrong_url_result.conflicts

    future_store, future_ids = _adjusted_audit_store(
        tmp_path / "future_attempt",
        {"100001": data_as_of, "200001": data_as_of},
        finished_at=NOW + timedelta(seconds=30),
    )
    future_left = _series(
        "100001",
        _adjusted_rows(
            "100001",
            source_attempt_id=future_ids["100001"],
        ),
    )
    future_right = _series(
        "200001",
        _adjusted_rows(
            "200001",
            scale="2",
            source_attempt_id=future_ids["200001"],
        ),
    )
    future_result = _build(
        "100001",
        (_position("100001", "50"), _position("200001", "50")),
        {
            "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
            "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
        },
        adjusted_series_by_fund={
            "100001": future_left,
            "200001": future_right,
        },
        decision_audit_store=future_store,
    )
    assert not any(
        item.relationship_type == "adjusted_return_correlation"
        for item in future_result.relationships
    )
    assert "adjusted_return_source_binding_invalid_100001" in future_result.conflicts


def test_adjusted_return_correlation_insufficiency_is_explicit(tmp_path) -> None:
    positions = (_position("100001", "50"), _position("200001", "50"))
    facts = {
        "100001": _facts("100001", company="甲", manager="甲", benchmark="甲"),
        "200001": _facts("200001", company="乙", manager="乙", benchmark="乙"),
    }
    cases = (
        (
            {"count": 60},
            "adjusted_return_samples_insufficient_200001",
        ),
        (
            {"accumulated": False},
            "adjusted_return_accumulated_nav_missing_200001",
        ),
        (
            {"duplicate_last_date": True},
            "adjusted_return_duplicate_date_200001",
        ),
        (
            {"corporate_action_state": "unknown"},
            "adjusted_return_discontinuity_200001",
        ),
        (
            {"flat": True},
            "adjusted_return_zero_variance_200001",
        ),
        (
            {"start_offset_days": 1},
            "adjusted_return_samples_insufficient_200001",
        ),
        (
            {"start_offset_days": 61},
            "adjusted_return_common_end_mismatch_100001_200001",
        ),
    )
    for index, (right_kwargs, expected_code) in enumerate(cases):
        preliminary_left = _adjusted_rows("100001")
        preliminary_right = _adjusted_rows("200001", **right_kwargs)
        audit_store, attempt_ids = _adjusted_audit_store(
            tmp_path / f"case_{index}",
            {
                "100001": preliminary_left[-1].nav_date,
                "200001": preliminary_right[-1].nav_date,
            },
        )
        left = _adjusted_rows(
            "100001",
            source_attempt_id=attempt_ids["100001"],
        )
        right = _adjusted_rows(
            "200001",
            source_attempt_id=attempt_ids["200001"],
            **right_kwargs,
        )
        result = _build(
            "100001",
            positions,
            facts,
            adjusted_series_by_fund={
                "100001": _series("100001", left),
                "200001": _series("200001", right),
            },
            decision_audit_store=audit_store,
        )
        assert not any(
            item.relationship_type == "adjusted_return_correlation" for item in result.relationships
        )
        assert expected_code in result.missing_fields + result.conflicts
