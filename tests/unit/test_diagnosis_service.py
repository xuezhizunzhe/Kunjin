from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from kunjin.brief.models import BriefCoverage, BriefEvidenceState, RelationshipEvidence
from kunjin.diagnosis.models import PortfolioDiagnosis
from kunjin.diagnosis.service import DiagnosisService
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import AccountObservation, PositionObservation
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 19, 5, tzinfo=timezone.utc)


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "diagnosis.db")
    repository.migrate()
    return repository


def _save_positions(
    repository: Repository,
    *positions: tuple[str, str, str | None],
) -> None:
    account = AccountObservation(
        source="yangjibao",
        source_account_id="account-one",
        title="学习账户",
        observed_at=NOW,
    )
    observations = [
        PositionObservation(
            source_account_id=account.source_account_id,
            fund_code=code,
            fund_name=f"基金{code}",
            shares=Decimal(shares),
            observed_at=NOW,
            share_class="A",
            formal_nav=None if nav is None else Decimal(nav),
        )
        for code, shares, nav in positions
    ]
    repository.replace_snapshot(account, observations)


def _service(repository: Repository) -> DiagnosisService:
    return DiagnosisService(
        repository,
        FundDisclosureStore(repository),
        clock=lambda: NOW,
    )


def test_empty_portfolio_returns_authenticated_insufficient_diagnosis(
    tmp_path: Path,
) -> None:
    result = _service(_repository(tmp_path)).diagnose()

    assert type(result) is PortfolioDiagnosis
    result.validate()
    assert result.position_count == 0
    assert result.value_basis == "missing"
    assert result.relationship_coverage.evidence_state == "insufficient_data"
    assert result.holdings_coverage.evidence_state == "insufficient_data"
    assert result.relationships == ()
    assert result.action_authorized is False


def test_service_reuses_existing_d2_and_portfolio_engines(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from kunjin.diagnosis import service as service_module

    repository = _repository(tmp_path)
    _save_positions(
        repository,
        ("000001", "10", "1"),
        ("000002", "20", "1"),
    )
    d2_calls = []
    overlap_calls = []
    original_d2 = service_module.build_d2_relationships
    original_overlap = service_module.build_portfolio_overlap_report

    def record_d2(*args, **kwargs):
        d2_calls.append(args[0])
        return original_d2(*args, **kwargs)

    def record_overlap(*args, **kwargs):
        overlap_calls.append(tuple(sorted(args[0])))
        return original_overlap(*args, **kwargs)

    monkeypatch.setattr(service_module, "build_d2_relationships", record_d2)
    monkeypatch.setattr(service_module, "build_portfolio_overlap_report", record_overlap)

    result = _service(repository).diagnose()

    result.validate()
    assert d2_calls == ["000001", "000002"]
    assert overlap_calls == [("000001", "000002")]
    assert result.position_count == 2
    assert result.value_basis == "formal"
    assert result.hhi == Decimal("0.5555555555555555555555555556")


def test_missing_nav_and_holdings_never_become_zero_overlap(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _save_positions(
        repository,
        ("000001", "10", "1"),
        ("000002", "20", None),
    )

    result = _service(repository).diagnose()

    result.validate()
    assert result.value_basis == "missing"
    assert result.hhi is None
    assert result.largest_position_share is None
    assert result.holdings_coverage.evidence_state == "insufficient_data"
    assert "holdings_industries_000001" in result.holdings_coverage.unknown_fields
    assert "holdings_industries_000002" in result.holdings_coverage.unknown_fields
    assert all(
        finding.finding_type != "disclosed_security_duplication"
        for finding in result.findings
    )
    assert "portfolio_valuation_unavailable" in result.missing_evidence


def test_d2_manager_and_benchmark_relationships_keep_their_exact_limits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from kunjin.diagnosis import service as service_module

    repository = _repository(tmp_path)
    _save_positions(
        repository,
        ("000001", "10", "1"),
        ("000002", "20", "1"),
    )
    manager = RelationshipEvidence(
        relationship_id="same_manager_000001_000002",
        relationship_type="same_manager",
        fund_codes=("000001", "000002"),
        evidence_state=BriefEvidenceState.COMPLETE,
        metrics={"shared_manager_name": "示例经理"},
        evidence_ids=(),
        report_periods=(),
        publication_times=(NOW,),
        warnings=(),
    )
    benchmark = RelationshipEvidence(
        relationship_id="same_benchmark_000001_000002",
        relationship_type="same_current_benchmark",
        fund_codes=("000001", "000002"),
        evidence_state=BriefEvidenceState.PARTIAL,
        metrics={"benchmark_description": "沪深300指数", "exact_text_match": True},
        evidence_ids=(),
        report_periods=(),
        publication_times=(NOW,),
        warnings=("benchmark_text_is_not_index_identity",),
    )
    relationship_coverage = BriefCoverage(
        coverage_id="d2_minimum_relationship_coverage",
        scope="minimum_relationship_coverage",
        evidence_state=BriefEvidenceState.COMPLETE,
        included_fund_codes=("000001", "000002"),
        omitted_fund_codes=(),
        known_percent=None,
        unknown_fields=(
            "authenticated_index_identity_000001",
            "authenticated_index_identity_000002",
        ),
        evidence_ids=(),
    )
    holdings = BriefCoverage(
        coverage_id="d2_disclosed_holdings_coverage",
        scope="disclosed_holdings_overlap",
        evidence_state=BriefEvidenceState.INSUFFICIENT,
        included_fund_codes=(),
        omitted_fund_codes=("000001", "000002"),
        known_percent=None,
        unknown_fields=("holdings_industries_000001", "holdings_industries_000002"),
        evidence_ids=(),
    )

    class FakeD2:
        relationships = (manager, benchmark)
        coverage = relationship_coverage
        holdings_coverage = holdings
        missing_fields = ()
        conflicts = ()
        warnings = ()

        def validate(self) -> None:
            return None

    monkeypatch.setattr(service_module, "build_d2_relationships", lambda *args, **kwargs: FakeD2())

    result = _service(repository).diagnose()

    result.validate()
    assert {finding.finding_type for finding in result.findings} >= {
        "same_current_manager",
        "same_current_benchmark_text",
    }
    projected_benchmark = next(
        item
        for item in result.relationships
        if item.relationship_type == "same_current_benchmark"
    )
    assert projected_benchmark.evidence_state == "partial"
    assert projected_benchmark.warnings == ("benchmark_text_is_not_index_identity",)
    assert "same_exact_index_or_theme" not in {
        finding.finding_type for finding in result.findings
    }


def test_candidate_without_local_public_evidence_is_insufficient(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _save_positions(repository, ("000001", "10", "1"))

    result = _service(repository).diagnose("000003")

    result.validate()
    assert result.candidate_impact is not None
    assert result.candidate_impact.fund_code == "000003"
    assert result.candidate_impact.label == "insufficient_data"
    assert set(result.candidate_impact.unknown_fields) >= {
        "candidate_identity",
        "candidate_manager",
        "candidate_benchmark",
        "candidate_holdings",
    }
    assert result.action_authorized is False


@pytest.mark.parametrize("code", ("000000", "12345", "abcdef"))
def test_candidate_code_is_exact_and_non_reserved(tmp_path: Path, code: str) -> None:
    with pytest.raises(ValueError, match="candidate fund code"):
        _service(_repository(tmp_path)).diagnose(code)


def test_candidate_partial_overlap_is_mixed_and_never_called_safe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from kunjin.diagnosis import service as service_module

    repository = _repository(tmp_path)
    _save_positions(repository, ("000001", "10", "1"))
    manager = RelationshipEvidence(
        relationship_id="same_manager_000001_000003",
        relationship_type="same_manager",
        fund_codes=("000001", "000003"),
        evidence_state=BriefEvidenceState.COMPLETE,
        metrics={"shared_manager_name": "示例经理"},
        evidence_ids=(),
        report_periods=(),
        publication_times=(NOW,),
        warnings=(),
    )
    relationship_coverage = BriefCoverage(
        coverage_id="d2_minimum_relationship_coverage",
        scope="minimum_relationship_coverage",
        evidence_state=BriefEvidenceState.COMPLETE,
        included_fund_codes=("000001",),
        omitted_fund_codes=(),
        known_percent=None,
        unknown_fields=("authenticated_index_identity_000001",),
        evidence_ids=(),
    )
    holdings = BriefCoverage(
        coverage_id="d2_disclosed_holdings_coverage",
        scope="disclosed_holdings_overlap",
        evidence_state=BriefEvidenceState.COMPLETE,
        included_fund_codes=("000001",),
        omitted_fund_codes=(),
        known_percent=None,
        unknown_fields=(),
        evidence_ids=(),
    )

    class FakeD2:
        relationships = (manager,)
        coverage = relationship_coverage
        holdings_coverage = holdings
        missing_fields = ()
        conflicts = ()
        warnings = ()

        def validate(self) -> None:
            return None

    monkeypatch.setattr(service_module, "build_d2_relationships", lambda *args, **kwargs: FakeD2())
    monkeypatch.setattr(service_module, "_candidate_unknown_fields", lambda bundle, as_of: ())
    monkeypatch.setattr(
        service_module,
        "build_explicit_compare_report",
        lambda *args, **kwargs: {
            "candidate_portfolio_overlap": {
                "000003": {
                    "evidence_level": "deterministic_calculation",
                    "candidate_disclosed_weight": "0.8",
                    "overlap": "0.2",
                }
            }
        },
    )

    result = _service(repository).diagnose("000003")

    result.validate()
    assert result.candidate_impact is not None
    assert result.candidate_impact.label == "mixed_observed_impact"
    assert result.candidate_impact.disclosed_weight == Decimal("0.8")
    assert result.candidate_impact.observed_overlap == Decimal("0.2")
    assert "safe" not in result.candidate_impact.label
    assert "recommended" not in result.candidate_impact.label
    assert "candidate_observed_duplication" in {
        finding.finding_type for finding in result.findings
    }
