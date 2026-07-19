from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations
from pathlib import Path

import pytest

from kunjin.brief.models import BriefCoverage, BriefEvidenceState, RelationshipEvidence
from kunjin.diagnosis.models import CandidateImpact
from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
    FundBenchmark,
    FundHolding,
    FundIdentity,
    FundManagerTenure,
)
from kunjin.funds.risk.models import (
    EvidenceStatus,
    FundRiskClassification,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
)
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import AccountObservation, PositionObservation
from kunjin.selection.service import ShortlistService, project_personal_gate
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 19, 6, tzinfo=timezone.utc)
CODES = ("000001", "000002", "000003", "000004", "000005")


def test_personal_gate_projection_is_shared_and_amount_free() -> None:
    gate = project_personal_gate(
        {
            "state": "fresh",
            "freshness": "fresh",
            "status": "blocked",
            "hard_blocks": ["emergency_reserve_shortfall"],
            "constraints": ["monthly_ceiling_constrained"],
        },
        {"state": "missing", "freshness": "missing"},
    )

    gate.validate()
    assert gate.blocking_codes == ("emergency_reserve_shortfall",)
    assert gate.constraint_codes == ("monthly_ceiling_constrained",)
    assert not hasattr(gate, "amount")


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "selection.db")
    repository.migrate()
    return repository


def _save_positions(repository: Repository, *codes: str) -> None:
    account = AccountObservation(
        source="yangjibao",
        source_account_id="account-one",
        title="local-owner-account",
        observed_at=NOW,
    )
    repository.replace_snapshot(
        account,
        (
            PositionObservation(
                source_account_id=account.source_account_id,
                fund_code=code,
                fund_name=f"Fund {code}",
                shares=Decimal("10"),
                observed_at=NOW,
                share_class="A",
                formal_nav=Decimal("1"),
            )
            for code in codes
        ),
    )


def _bundle(
    code: str,
    *,
    fund_type: str = "混合型-灵活",
    benchmark: str = "沪深300指数收益率",
    holdings: bool = True,
) -> DisclosureBundle:
    result = DisclosureBundle(
        fund_code=code,
        identity=FundIdentity(
            code,
            f"Fund {code}",
            "active",
            fund_type,
            date(2020, 1, 1),
            "Manager Company",
            None,
        ),
        share_classes=(),
        manager_tenures=(
            FundManagerTenure(code, f"Manager {code}", date(2024, 1, 1), None, None),
        ),
        fee_rules=(),
        sizes=(),
        benchmarks=(FundBenchmark(code, benchmark, None, None, None),),
        holdings=(
            (
                FundHolding(
                    code,
                    date(2026, 3, 31),
                    datetime(2026, 4, 20, tzinfo=timezone.utc),
                    1,
                    f"security-{code}",
                    f"Security {code}",
                    AssetType.STOCK,
                    Decimal("100"),
                    "complete",
                    None,
                ),
            )
            if holdings
            else ()
        ),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )
    result.validate()
    return result


def _classification(
    code: str,
    *,
    evidence_status: EvidenceStatus = EvidenceStatus.VERIFIED,
    risk_bucket: RiskBucket = RiskBucket.DIVERSIFIED_EQUITY,
    portfolio_role: PortfolioRole = PortfolioRole.CORE_ELIGIBLE,
) -> FundRiskClassification:
    result = FundRiskClassification(
        fund_code=code,
        policy_version="1",
        input_fingerprint=(code[-1] or "a") * 64,
        product_family=ProductFamily.ACTIVE_EQUITY,
        risk_bucket=risk_bucket,
        portfolio_role=portfolio_role,
        evidence_status=evidence_status,
        evidence_tags=(),
        reason_codes=(),
        missing_evidence=(),
        conflicts=(),
        evidence_document_ids=(),
        evidence_fact_ids=(),
        freshness=(),
        classified_at=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(days=1),
    )
    result.validate()
    return result


def _compare_report(codes: tuple[str, ...]) -> dict[str, object]:
    return {
        "as_of": NOW.isoformat(),
        "calculation_version": "1",
        "rule_version": "1",
        "windows": {
            "90d": [
                {
                    "fund_code": code,
                    "effective_start": "2026-04-19",
                    "effective_end": "2026-07-18",
                    "total_return": str(index + 1),
                    "portfolio_weight": "private",
                }
                for index, code in enumerate(codes)
            ],
            "365d": [],
            "manager_tenure": [{"portfolio_weight": "private"}],
        },
        "metric_orderings": {
            "90d": {
                "total_return": {
                    "direction": "higher",
                    "fund_codes": list(codes),
                    "values": {
                        **{code: str(index) for index, code in enumerate(codes)},
                        "portfolio_weight": "private",
                    },
                }
            },
            "365d": {},
            "manager_tenure": {"portfolio_weight": "private"},
            "ongoing_annual_fee_rate": {},
            "size_stability": {},
            "portfolio_overlap": {},
        },
        "managers": {code: [{"shares": "private"}] for code in codes},
        "fees": {code: [{"amount": "private"}] for code in codes},
        "ongoing_annual_fee_rates": {
            **{code: "1" for code in codes},
            "portfolio_weight": "private",
        },
        "sizes": {code: {"observations": 3, "shares": "private"} for code in codes},
        "pairwise_overlap": [{"portfolio_weight": "private"}],
        "portfolio_overlap": {
            "portfolio_weight_coverage": "1",
            "raw_positions": [{"shares": "private"}],
        },
        "candidate_portfolio_overlap": {
            code: {
                "evidence_level": "deterministic_calculation",
                "candidate_disclosed_weight": "1",
                "overlap": "0",
                "report_period": "2026-03-31",
                "portfolio_weight": "private",
            }
            for code in codes
        },
        "advantages": [f"90d:total_return:{codes[0]}"],
        "tradeoffs": [f"90d:total_return:{codes[-1]}"],
        "coverage": {"portfolio_weight_coverage": "1"},
        "data_dates": {
            "common_nav_end": "2026-07-18",
            "manager_team_starts": {code: "2024-01-01" for code in codes},
            "private": {"shares": "private"},
        },
        "data_gaps": [],
        "warnings": [],
        "errors": [],
        "raw_positions": [{"amount": "private"}],
    }


class _FakeD2:
    relationships = ()
    missing_fields = ()
    conflicts = ()
    warnings = ()
    coverage = BriefCoverage(
        coverage_id="d2_minimum_relationship_coverage",
        scope="minimum_relationship_coverage",
        evidence_state=BriefEvidenceState.COMPLETE,
        included_fund_codes=(),
        omitted_fund_codes=(),
        known_percent=None,
        unknown_fields=(),
        evidence_ids=(),
    )
    holdings_coverage = BriefCoverage(
        coverage_id="d2_disclosed_holdings_coverage",
        scope="disclosed_holdings_overlap",
        evidence_state=BriefEvidenceState.COMPLETE,
        included_fund_codes=(),
        omitted_fund_codes=(),
        known_percent=None,
        unknown_fields=(),
        evidence_ids=(),
    )

    def validate(self) -> None:
        return None


@pytest.fixture
def shortlist_service_factory(tmp_path: Path, monkeypatch):
    from kunjin.selection import service as service_module

    repository = _repository(tmp_path)
    store = FundDisclosureStore(repository)
    bundles = {code: _bundle(code) for code in (*CODES, "000009")}

    monkeypatch.setattr(store, "load_bundle", lambda code: bundles[code])
    monkeypatch.setattr(
        service_module,
        "build_source_linked_facts",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(service_module, "build_d2_relationships", lambda *args, **kwargs: _FakeD2())
    monkeypatch.setattr(
        service_module,
        "build_explicit_compare_report",
        lambda codes, *args, **kwargs: _compare_report(tuple(codes)),
    )
    monkeypatch.setattr(
        service_module,
        "project_candidate_impact",
        lambda code, *args, **kwargs: CandidateImpact(
            fund_code=code,
            label="observed_adds_distinct_exposure",
            relationship_ids=(),
            disclosed_weight=Decimal("1"),
            observed_overlap=Decimal("0"),
            unknown_fields=(),
        ),
    )

    def factory(
        *,
        classification_loader=None,
        suitability_state: str = "fresh",
        suitability_status: str | None = "ready_for_allocation",
        allocation_state: str = "fresh",
        allocation_status: str | None = "range_available",
        suitability_status_loader=None,
        allocation_status_loader=None,
    ) -> ShortlistService:
        suitability = {
            "state": suitability_state,
            "freshness": suitability_state,
            "status": suitability_status,
            "hard_blocks": [] if suitability_status != "blocked" else ["reserve_shortfall"],
            "constraints": ["horizon_binding"],
            "assessment_id": 91,
            "profile_version_id": 92,
            "safe_summary": {"private": "value"},
        }
        allocation = {
            "state": allocation_state,
            "freshness": allocation_state,
            "status": allocation_status,
            "binding_constraints": ["loss_budget_binding"],
            "permitted_region": {"equity": "private"},
            "input_fingerprint": "private",
        }
        return ShortlistService(
            repository,
            store,
            classification_loader=classification_loader or _classification,
            suitability_status_loader=suitability_status_loader or (lambda: suitability),
            allocation_status_loader=allocation_status_loader or (lambda: allocation),
            clock=lambda: NOW,
        )

    factory.repository = repository
    factory.store = store
    factory.bundles = bundles
    return factory


def _dynamic_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key).casefold() for key in value} | {
            key for item in value.values() for key in _dynamic_keys(item)
        }
    if isinstance(value, (list, tuple)):
        return {key for item in value for key in _dynamic_keys(item)}
    return set()


@pytest.mark.parametrize("codes", (("000002", "000001"), CODES))
def test_service_loads_once_and_reuses_existing_engines(
    shortlist_service_factory,
    monkeypatch,
    codes: tuple[str, ...],
) -> None:
    from kunjin.selection import service as service_module

    repository = shortlist_service_factory.repository
    store = shortlist_service_factory.store
    bundles = shortlist_service_factory.bundles
    calls = {"positions": 0, "bundle": [], "history": [], "classification": [], "compare": []}
    status_calls = {"suitability": 0, "allocation": 0}
    original_positions = repository.latest_positions
    original_history = repository.fund_history

    def positions_loader():
        calls["positions"] += 1
        return original_positions()

    def history_loader(code):
        calls["history"].append(code)
        return original_history(code)

    def bundle_loader(code):
        calls["bundle"].append(code)
        return bundles[code]

    def classification_loader(code):
        calls["classification"].append(code)
        return _classification(code)

    def compare_loader(candidate_codes, *args, **kwargs):
        calls["compare"].append(tuple(candidate_codes))
        return _compare_report(tuple(candidate_codes))

    def suitability_loader():
        status_calls["suitability"] += 1
        return {
            "state": "fresh",
            "freshness": "fresh",
            "status": "ready_for_allocation",
            "hard_blocks": [],
            "constraints": [],
        }

    def allocation_loader():
        status_calls["allocation"] += 1
        return {
            "state": "fresh",
            "freshness": "fresh",
            "status": "range_available",
            "binding_constraints": [],
        }

    monkeypatch.setattr(repository, "latest_positions", positions_loader)
    monkeypatch.setattr(repository, "fund_history", history_loader)
    monkeypatch.setattr(store, "load_bundle", bundle_loader)
    monkeypatch.setattr(service_module, "build_explicit_compare_report", compare_loader)

    result = shortlist_service_factory(
        classification_loader=classification_loader,
        suitability_status_loader=suitability_loader,
        allocation_status_loader=allocation_loader,
    ).review(codes)

    result.validate()
    assert result.candidate_codes == codes
    assert calls == {
        "positions": 1,
        "bundle": list(codes),
        "history": list(codes),
        "classification": list(codes),
        "compare": [codes],
    }
    assert status_calls == {"suitability": 1, "allocation": 1}
    actual_pairs = tuple(
        (item.left_fund_code, item.right_fund_code)
        for item in result.comparability
    )
    assert actual_pairs == tuple(combinations(codes, 2))


def test_service_has_no_sync_or_network_dependency(shortlist_service_factory) -> None:
    service = shortlist_service_factory()
    assert not hasattr(service, "sync_service")
    assert not hasattr(service, "client")
    service.review(("000001", "000002"))


def test_real_local_engines_degrade_without_public_or_personal_evidence(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    service = ShortlistService(
        repository,
        FundDisclosureStore(repository),
        classification_loader=lambda code: None,
        suitability_status_loader=lambda: {"state": "missing", "freshness": "missing"},
        allocation_status_loader=lambda: {"state": "missing", "freshness": "missing"},
        clock=lambda: NOW,
    )

    result = service.review(("000001", "000002"))

    result.validate()
    assert result.comparison_state == "insufficient_data"
    assert result.shortlist_codes == ()
    assert all(item.d1_evidence_status is None for item in result.candidate_reviews)


def test_metric_projection_is_whitelisted_and_keeps_common_date_evidence(
    shortlist_service_factory,
) -> None:
    result = shortlist_service_factory().review(("000001", "000002"))
    metrics = dict(result.metric_comparisons)

    assert set(metrics) == {
        "candidate_portfolio_overlap",
        "data_dates",
        "fees",
        "formal_nav_365d",
        "formal_nav_90d",
        "managers",
        "metric_orderings",
        "ongoing_annual_fee_rates",
        "pairwise_disclosed_overlap",
        "size_stability",
    }
    assert metrics["data_dates"]["common_nav_end"] == "2026-07-18"
    assert metrics["formal_nav_90d"][0]["effective_end"] == "2026-07-18"
    assert not _dynamic_keys(metrics).intersection(
        {"amount", "portfolio_weight", "shares", "raw_positions", "portfolio_weight_coverage"}
    )


def test_metric_gaps_do_not_pollute_portfolio_impact_projection(
    shortlist_service_factory,
    monkeypatch,
) -> None:
    from kunjin.selection import service as service_module

    projection_unknowns = []

    def project(code, bundle, relationships, report):
        projection_unknowns.append(tuple(report["_candidate_projection_unknown_fields"]))
        return CandidateImpact(
            fund_code=code,
            label="observed_adds_distinct_exposure",
            relationship_ids=(),
            disclosed_weight=Decimal("1"),
            observed_overlap=Decimal("0"),
            unknown_fields=(),
        )

    monkeypatch.setattr(service_module, "project_candidate_impact", project)

    result = shortlist_service_factory().review(("000001", "000002"))

    assert result.candidate_reviews[0].portfolio_impact_state == "usable"
    assert projection_unknowns == [(), ()]


def test_held_candidate_blocks_future_marginal_impact_without_amount(
    shortlist_service_factory,
) -> None:
    _save_positions(shortlist_service_factory.repository, "000001")

    result = shortlist_service_factory().review(("000001", "000003"))

    held = result.candidate_reviews[0]
    assert held.position_state == "held"
    assert "marginal_impact_requires_purchase_amount" in held.blocking_codes
    assert held.portfolio_impact_label != "observed_adds_distinct_exposure"
    assert result.exact_amount_available is False
    assert result.comparison_state == "relative_tradeoffs_only"


@pytest.mark.parametrize(
    (
        "suitability_state",
        "suitability_status",
        "allocation_state",
        "allocation_status",
        "expected",
    ),
    (
        ("missing", None, "missing", None, "relative_tradeoffs_only"),
        ("fresh", "blocked", "missing", None, "relative_tradeoffs_only"),
        ("fresh", "ready_for_allocation", "stale", "range_available", "relative_tradeoffs_only"),
        ("fresh", "ready_for_allocation", "fresh", "range_available", "conditional_shortlist"),
    ),
)
def test_personal_gates_only_filter_and_never_rank(
    shortlist_service_factory,
    suitability_state: str,
    suitability_status: str | None,
    allocation_state: str,
    allocation_status: str | None,
    expected: str,
) -> None:
    result = shortlist_service_factory(
        suitability_state=suitability_state,
        suitability_status=suitability_status,
        allocation_state=allocation_state,
        allocation_status=allocation_status,
    ).review(("000002", "000001"))

    assert result.comparison_state == expected
    assert result.shortlist_codes == (
        ("000002", "000001") if expected == "conditional_shortlist" else ()
    )
    assert not hasattr(result.personal_gate, "assessment_id")
    assert not hasattr(result.personal_gate, "profile_version_id")
    assert not hasattr(result.personal_gate, "permitted_region")


@pytest.mark.parametrize(
    ("evidence_status", "risk_bucket", "expected_block"),
    (
        (EvidenceStatus.PARTIAL, RiskBucket.DIVERSIFIED_EQUITY, "d1_evidence_not_verified"),
        (EvidenceStatus.CONFLICTED, RiskBucket.DIVERSIFIED_EQUITY, "d1_evidence_not_verified"),
        (EvidenceStatus.STALE, RiskBucket.DIVERSIFIED_EQUITY, "d1_evidence_not_verified"),
        (EvidenceStatus.UNCLASSIFIED, RiskBucket.UNCLASSIFIED, "d1_evidence_not_verified"),
        (
            EvidenceStatus.VERIFIED,
            RiskBucket.CASH_LIKE_CANDIDATE,
            "cash_like_is_not_protected_cash",
        ),
    ),
)
def test_d1_evidence_must_pass_the_narrow_mapping(
    shortlist_service_factory,
    evidence_status: EvidenceStatus,
    risk_bucket: RiskBucket,
    expected_block: str,
) -> None:
    def loader(code: str) -> FundRiskClassification:
        if code == "000001":
            return _classification(code, evidence_status=evidence_status, risk_bucket=risk_bucket)
        return _classification(code)

    result = shortlist_service_factory(classification_loader=loader).review(
        ("000001", "000002")
    )

    candidate = result.candidate_reviews[0]
    assert candidate.mapped_asset_layer is None
    assert expected_block in candidate.blocking_codes
    assert result.comparison_state == "relative_tradeoffs_only"


def test_different_verified_layers_are_not_comparable(shortlist_service_factory) -> None:
    def loader(code: str) -> FundRiskClassification:
        return _classification(
            code,
            risk_bucket=(
                RiskBucket.HIGH_QUALITY_FIXED_INCOME
                if code == "000001"
                else RiskBucket.DIVERSIFIED_EQUITY
            ),
        )

    result = shortlist_service_factory(classification_loader=loader).review(
        ("000001", "000002")
    )

    assert result.comparison_state == "not_comparable"
    assert result.shortlist_codes == ()


def test_candidate_local_d1_failure_does_not_discard_other_candidate(
    shortlist_service_factory,
) -> None:
    def loader(code: str) -> FundRiskClassification:
        if code == "000001":
            raise RuntimeError("private exception text")
        return _classification(code)

    result = shortlist_service_factory(classification_loader=loader).review(
        ("000001", "000002")
    )

    failed, usable = result.candidate_reviews
    assert "d1_classification_unavailable" in failed.missing_evidence
    assert failed.d1_evidence_status is None
    assert usable.d1_evidence_status == "verified"
    assert "private exception text" not in repr(result)


def test_missing_bundle_is_passed_as_empty_and_degrades_only_that_candidate(
    shortlist_service_factory,
    monkeypatch,
) -> None:
    from kunjin.selection import service as service_module

    store = shortlist_service_factory.store
    bundles = shortlist_service_factory.bundles
    compared_bundles = []

    def bundle_loader(code: str) -> DisclosureBundle:
        if code == "000001":
            raise ValueError("private disclosure failure")
        return bundles[code]

    def compare_loader(codes, loaded, histories, positions, as_of):
        compared_bundles.append(loaded)
        return _compare_report(tuple(codes))

    monkeypatch.setattr(store, "load_bundle", bundle_loader)
    monkeypatch.setattr(service_module, "build_explicit_compare_report", compare_loader)

    result = shortlist_service_factory().review(("000001", "000002"))

    assert len(compared_bundles) == 1
    assert set(compared_bundles[0]) == {"000001", "000002"}
    assert compared_bundles[0]["000001"].identity is None
    assert "disclosure_bundle_unavailable" in result.candidate_reviews[0].missing_evidence
    assert result.candidate_reviews[1].d1_evidence_status == "verified"


@pytest.mark.parametrize(
    ("missing_dimension", "expected_gap"),
    (
        ("nav", "formal_nav_90d_unavailable"),
        ("fees", "fees_unavailable"),
        ("holdings", "holdings_industries_000001"),
    ),
)
def test_dimension_gaps_are_isolated_to_one_candidate(
    shortlist_service_factory,
    monkeypatch,
    missing_dimension: str,
    expected_gap: str,
) -> None:
    from kunjin.selection import service as service_module

    def compare_loader(codes, *args, **kwargs):
        report = _compare_report(tuple(codes))
        report["windows"]["365d"] = [
            {**item, "window": "365d"} for item in report["windows"]["90d"]
        ]
        if missing_dimension == "nav":
            for window in ("90d", "365d"):
                report["windows"][window] = [
                    item
                    for item in report["windows"][window]
                    if item["fund_code"] != "000001"
                ]
        elif missing_dimension == "fees":
            report["fees"]["000001"] = []
            report["ongoing_annual_fee_rates"]["000001"] = None
        else:
            report["candidate_portfolio_overlap"]["000001"] = {
                "evidence_level": "insufficient_data"
            }
        return report

    class MissingHoldingsD2(_FakeD2):
        missing_fields = ("holdings_industries_000001",)

    def d2_loader(code, *args, **kwargs):
        if missing_dimension == "holdings" and code == "000001":
            return MissingHoldingsD2()
        return _FakeD2()

    def impact_loader(code, bundle, relationships, report):
        unknowns = tuple(report["_candidate_projection_unknown_fields"])
        return CandidateImpact(
            fund_code=code,
            label=(
                "insufficient_data"
                if unknowns
                else "observed_adds_distinct_exposure"
            ),
            relationship_ids=(),
            disclosed_weight=None if unknowns else Decimal("1"),
            observed_overlap=None if unknowns else Decimal("0"),
            unknown_fields=unknowns,
        )

    monkeypatch.setattr(service_module, "build_explicit_compare_report", compare_loader)
    monkeypatch.setattr(service_module, "build_d2_relationships", d2_loader)
    monkeypatch.setattr(service_module, "project_candidate_impact", impact_loader)

    result = shortlist_service_factory().review(("000001", "000002"))

    affected, unaffected = result.candidate_reviews
    assert expected_gap in affected.missing_evidence
    assert expected_gap not in unaffected.missing_evidence
    assert unaffected.d1_evidence_status == "verified"
    assert unaffected.portfolio_impact_state == "usable"
    if missing_dimension == "fees":
        assert "fees_unavailable" in affected.blocking_codes
        assert "fees_unavailable" not in unaffected.blocking_codes
    elif missing_dimension == "holdings":
        assert affected.portfolio_impact_state == "insufficient_data"
    else:
        assert affected.portfolio_impact_state == "usable"


def test_pairwise_classification_failure_is_isolated(
    shortlist_service_factory,
    monkeypatch,
) -> None:
    from kunjin.selection import service as service_module

    original = service_module.classify_peer

    def classify(left, right, as_of):
        if (left.fund_code, right.fund_code) == ("000001", "000002"):
            raise ValueError("private peer classification failure")
        return original(left, right, as_of)

    monkeypatch.setattr(service_module, "classify_peer", classify)

    result = shortlist_service_factory().review(("000001", "000002", "000003"))

    assert len(result.comparability) == 3
    assert result.comparability[0].state == "insufficient_data"
    assert result.comparability[0].reason_code == "peer_classification_unavailable"
    assert result.comparability[1].state == "comparable"


def test_held_candidate_keeps_current_relationships_but_not_distinct_addition(
    shortlist_service_factory,
    monkeypatch,
) -> None:
    from kunjin.selection import service as service_module

    _save_positions(shortlist_service_factory.repository, "000001")
    relationship = RelationshipEvidence(
        relationship_id="same_manager_000001_000003",
        relationship_type="same_manager",
        fund_codes=("000001", "000003"),
        evidence_state=BriefEvidenceState.COMPLETE,
        metrics={"shared_manager_name": "Shared Manager"},
        evidence_ids=("fact_manager_000001", "fact_manager_000003"),
        report_periods=(),
        publication_times=(NOW,),
        warnings=(),
    )
    d2_calls = []
    projection_relationships = {}

    class RelationshipD2(_FakeD2):
        relationships = (relationship,)

    def d2_loader(code, *args, **kwargs):
        d2_calls.append(code)
        return RelationshipD2()

    def impact_loader(code, bundle, relationships, report):
        projection_relationships[code] = tuple(item.relationship_id for item in relationships)
        return CandidateImpact(
            fund_code=code,
            label="observed_duplicates_existing_exposure",
            relationship_ids=projection_relationships[code],
            disclosed_weight=Decimal("1"),
            observed_overlap=Decimal("1"),
            unknown_fields=(),
        )

    monkeypatch.setattr(service_module, "build_d2_relationships", d2_loader)
    monkeypatch.setattr(service_module, "project_candidate_impact", impact_loader)

    result = shortlist_service_factory().review(("000001", "000003"))

    held = result.candidate_reviews[0]
    assert d2_calls == ["000001", "000003"]
    assert held.relationship_ids == ("same_manager_000001_000003",)
    assert held.portfolio_impact_label == "observed_duplicates_existing_exposure"
    assert "marginal_impact_requires_purchase_amount" in held.blocking_codes


def test_status_loader_failures_are_sanitized_and_called_once(
    shortlist_service_factory,
) -> None:
    calls = {"suitability": 0, "allocation": 0}

    def suitability_loader():
        calls["suitability"] += 1
        raise RuntimeError("private suitability failure")

    def allocation_loader():
        calls["allocation"] += 1
        raise RuntimeError("private allocation failure")

    result = shortlist_service_factory(
        suitability_status_loader=suitability_loader,
        allocation_status_loader=allocation_loader,
    ).review(("000001", "000002"))

    assert calls == {"suitability": 1, "allocation": 1}
    assert result.personal_gate.suitability_state == "transient"
    assert result.personal_gate.allocation_state == "transient"
    assert "suitability_status_unavailable" in result.missing_evidence
    assert "allocation_status_unavailable" in result.missing_evidence
    assert "private" not in repr(result)


def test_fingerprint_is_deterministic_and_binds_safe_status_and_d1(
    shortlist_service_factory,
) -> None:
    first = shortlist_service_factory().review(("000002", "000001"))
    second = shortlist_service_factory().review(("000002", "000001"))
    reversed_result = shortlist_service_factory().review(("000001", "000002"))
    changed_status = shortlist_service_factory(allocation_status="blocked").review(
        ("000002", "000001")
    )

    assert first.input_fingerprint == second.input_fingerprint
    assert first.input_fingerprint != reversed_result.input_fingerprint
    assert first.input_fingerprint != changed_status.input_fingerprint
    changed_d1 = shortlist_service_factory(
        classification_loader=lambda code: replace(
            _classification(code), input_fingerprint="f" * 64
        )
    ).review(("000002", "000001"))
    assert first.input_fingerprint != changed_d1.input_fingerprint
