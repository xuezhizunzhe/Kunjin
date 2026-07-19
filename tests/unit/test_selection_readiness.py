from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import kunjin.selection.readiness as readiness_module
from kunjin.decision.health import (
    ProjectedSourceField,
    SourceHealthService,
    SourceStatusSnapshot,
)
from kunjin.decision.models import (
    RequestFieldResolution,
    SourceFieldHistory,
    SourceFieldState,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundManagerTenure,
    SourceDocument,
)
from kunjin.funds.risk.models import (
    EvidenceStatus,
    FundRiskClassification,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
)
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import FundNavObservation, StoredPosition
from kunjin.selection.readiness import (
    ShortlistReadinessService,
    public_shortlist_readiness_payload,
)
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 19, 6, tzinfo=timezone.utc)
CODES = ("000002", "000001")


def _repository(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "readiness.db")
    repository.migrate()
    return repository


def _document(code: str) -> SourceDocument:
    return SourceDocument(
        1,
        code,
        DocumentKind.BASIC_PROFILE,
        f"Official document {code}",
        f"https://example.com/{code}",
        "official",
        1,
        "Fund Manager",
        NOW - timedelta(days=10),
        NOW - timedelta(days=1),
        code[-1] * 64,
    )


def _bundle(code: str) -> DisclosureBundle:
    document = _document(code)
    result = DisclosureBundle(
        fund_code=code,
        identity=FundIdentity(
            code,
            f"Fund {code}",
            "active",
            "股票型",
            date(2020, 1, 1),
            "Fund Manager",
            1,
        ),
        share_classes=(),
        manager_tenures=(
            FundManagerTenure(code, f"Manager {code}", date(2024, 1, 1), None, 1),
        ),
        fee_rules=(
            FundFeeRule(
                code,
                FeeType.REDEMPTION,
                1,
                share_class="A",
                rate=Decimal("0.5"),
                amount_min=Decimal("1"),
                amount_max=Decimal("100"),
                holding_days_min=0,
                holding_days_max=6,
            ),
        ),
        sizes=(),
        benchmarks=(FundBenchmark(code, "沪深300指数收益率", None, None, 1),),
        holdings=(
            FundHolding(
                code,
                date(2026, 6, 30),
                datetime(2026, 7, 15, tzinfo=timezone.utc),
                1,
                f"security-{code}",
                f"Security {code}",
                AssetType.STOCK,
                Decimal("12.5"),
                "top10",
                1,
            ),
        ),
        industry_exposure=(),
        announcements=(),
        source_documents={1: document},
        section_states={},
        section_statuses={},
    )
    result.validate()
    return result


def _history(code: str) -> tuple[FundNavObservation, ...]:
    return tuple(
        FundNavObservation(
            code,
            nav_date,
            Decimal("1.1"),
            Decimal("1.1"),
            Decimal("0"),
            "official",
            NOW - timedelta(hours=1),
            "none",
        )
        for nav_date in (date(2026, 7, 17), date(2026, 7, 18))
    )


def _classification(code: str) -> FundRiskClassification:
    result = FundRiskClassification(
        fund_code=code,
        policy_version="1",
        input_fingerprint=code[-1] * 64,
        product_family=ProductFamily.ACTIVE_EQUITY,
        risk_bucket=RiskBucket.DIVERSIFIED_EQUITY,
        portfolio_role=PortfolioRole.CORE_ELIGIBLE,
        evidence_status=EvidenceStatus.VERIFIED,
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


def _snapshot(
    service: ShortlistReadinessService,
    *,
    state_by_field: dict[str, SourceFieldState] | None = None,
) -> SourceStatusSnapshot:
    state_by_field = state_by_field or {}
    references = []
    for primary in service._source_primary_references:
        policy = next(
            field
            for source in service.source_health_service.registry.sources
            if source.source_id == primary.source_id
            for field in source.fields
            if field.field_id == primary.field_id
        )
        references.extend((primary, *policy.acceptable_alternatives))
    references = list(dict.fromkeys(references))
    projections = tuple(
        ProjectedSourceField(
            state_by_field.get(reference.field_id, SourceFieldState.HEALTHY),
            SourceFieldHistory(reference, ()),
        )
        for reference in references
    )
    resolutions = tuple(
        RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
        if state_by_field.get(reference.field_id) in {
            SourceFieldState.UNAVAILABLE,
            SourceFieldState.UNSUPPORTED,
        }
        else RequestFieldResolution.USABLE
        for reference in service._source_primary_references
    )
    result = SourceStatusSnapshot(projections, resolutions, NOW)
    result.validate()
    return result


def _build_service(tmp_path: Path, monkeypatch):
    repository = _repository(tmp_path)
    disclosure_store = FundDisclosureStore(repository)
    health = SourceHealthService(
        DecisionAuditStore(repository),
        wall_clock=lambda: NOW,
    )
    calls = {
        "allocation": 0,
        "bundle": {},
        "classification": {},
        "history": {},
        "positions": 0,
        "source": {},
        "suitability": 0,
    }

    def count_code(kind, code):
        calls[kind][code] = calls[kind].get(code, 0) + 1

    def load_bundle(code):
        count_code("bundle", code)
        return _bundle(code)

    def load_history(code):
        count_code("history", code)
        return list(_history(code))

    def load_classification(code):
        count_code("classification", code)
        return _classification(code)

    def latest_positions():
        calls["positions"] += 1
        return [
            StoredPosition(
                "private account",
                CODES[0],
                "held fund",
                Decimal("99"),
                NOW,
            )
        ]

    def suitability():
        calls["suitability"] += 1
        return {
            "state": "fresh",
            "freshness": "fresh",
            "status": "constrained",
            "hard_blocks": [],
        }

    def allocation():
        calls["allocation"] += 1
        return {
            "state": "fresh",
            "freshness": "fresh",
            "status": "range_available",
        }

    monkeypatch.setattr(disclosure_store, "load_bundle", load_bundle)
    monkeypatch.setattr(repository, "fund_history", load_history)
    monkeypatch.setattr(repository, "latest_positions", latest_positions)
    service = ShortlistReadinessService(
        repository,
        disclosure_store,
        source_health_service=health,
        classification_loader=load_classification,
        suitability_status_loader=suitability,
        allocation_status_loader=allocation,
        clock=lambda: NOW,
    )

    def load_source(subject_key, *_args):
        code = subject_key.removeprefix("fund:")
        count_code("source", code)
        return _snapshot(service)

    monkeypatch.setattr(health, "stored_source_status_snapshot", load_source)
    return service, repository, calls


def test_ready_projection_preserves_components_and_loads_each_once(
    tmp_path, monkeypatch
) -> None:
    service, _repository_value, calls = _build_service(tmp_path, monkeypatch)
    build_calls = []
    original = readiness_module.build_disclosure_report

    def counted_report(bundle, as_of):
        build_calls.append(bundle.fund_code)
        return original(bundle, as_of)

    monkeypatch.setattr(readiness_module, "build_disclosure_report", counted_report)

    result = service.review(CODES)
    payload = public_shortlist_readiness_payload(result)

    result.validate()
    assert result.candidate_codes == CODES
    assert result.comparison_evidence_ready is True
    assert result.conditional_shortlist_gate_ready is True
    assert result.bounded_refresh_actions == ()
    assert result.manual_supplementation == ()
    assert calls == {
        "allocation": 1,
        "bundle": {"000002": 1, "000001": 1},
        "classification": {"000002": 1, "000001": 1},
        "history": {"000002": 1, "000001": 1},
        "positions": 1,
        "source": {"000002": 1, "000001": 1},
        "suitability": 1,
    }
    assert build_calls == list(CODES)
    assert payload["candidate_evidence"][0]["portfolio_binding"] == {
        "position_state": "held",
        "technical_failure": None,
    }
    assert payload["candidate_evidence"][1]["portfolio_binding"]["position_state"] == (
        "not_held"
    )
    assert payload["candidate_evidence"][0]["source_health"]["fields"][0][
        "resolution"
    ] == "usable"
    assert payload["candidate_evidence"][0]["profile"]["fees"]["rules"][0][
        "amount_min"
    ] == "1"
    assert payload["candidate_evidence"][0]["profile"]["warnings"] == []
    assert payload["candidate_evidence"][0]["d1"]["conflicts"] == []
    assert set(payload) == {
        "action_boundary",
        "blocking_codes",
        "bounded_refresh_actions",
        "candidate_evidence",
        "comparison_evidence_ready",
        "conditional_shortlist_gate_ready",
        "manual_supplementation",
        "personal_gate",
        "request",
    }


@pytest.mark.parametrize(
    "codes",
    (
        ("000001",),
        tuple(f"{value:06d}" for value in range(1, 7)),
        ("000001", "000001"),
        ("000000", "000001"),
        ("０００００１", "000002"),
    ),
)
def test_validation_occurs_before_any_loader(tmp_path, monkeypatch, codes) -> None:
    service, _repository_value, calls = _build_service(tmp_path, monkeypatch)

    with pytest.raises(ValueError):
        service.review(codes)

    assert calls == {
        "allocation": 0,
        "bundle": {},
        "classification": {},
        "history": {},
        "positions": 0,
        "source": {},
        "suitability": 0,
    }


def test_missing_components_return_exact_actions_in_dependency_order(
    tmp_path, monkeypatch
) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service.disclosure_store,
        "load_bundle",
        lambda code: DisclosureBundle(code, None, (), (), (), (), (), (), (), (), {}, {}, {}),
    )
    monkeypatch.setattr(repository, "fund_history", lambda _code: [])
    service.classification_loader = lambda _code: None

    result = service.review(CODES)

    assert result.comparison_evidence_ready is False
    assert result.bounded_refresh_actions[:5] == tuple(
        (CODES[0], command)
        for command in (
            "sync fund 000002",
            "sync fund-profile 000002 --mode rapid",
            "sync fund-holdings 000002 --mode rapid",
            "sync fund-documents 000002",
            "fund classify 000002",
        )
    )
    assert len(result.bounded_refresh_actions) == 10
    assert all("--force" not in command for _, command in result.bounded_refresh_actions)


def test_unavailable_source_is_preserved_as_manual_supplement_not_refresh(
    tmp_path, monkeypatch
) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(repository, "fund_history", lambda _code: [])

    def unavailable_source(*_args):
        return _snapshot(
            service,
            state_by_field={"formal_nav": SourceFieldState.UNAVAILABLE},
        )

    monkeypatch.setattr(
        service.source_health_service,
        "stored_source_status_snapshot",
        unavailable_source,
    )

    result = service.review(CODES)
    payload = public_shortlist_readiness_payload(result)

    assert all(
        not command.startswith("sync fund ")
        for _, command in result.bounded_refresh_actions
    )
    assert result.manual_supplementation == (
        ("000002", "formal_nav"),
        ("000001", "formal_nav"),
    )
    formal_field = next(
        item
        for item in payload["candidate_evidence"][0]["source_health"]["fields"]
        if item["field_id"] == "formal_nav"
    )
    assert formal_field["resolution"] == "manual_supplement_required"
    assert {item["state"] for item in formal_field["acceptable_sources"]} == {
        "unavailable"
    }


def test_candidate_failure_is_local_and_base_exceptions_propagate(
    tmp_path, monkeypatch
) -> None:
    service, _repository_value, _calls = _build_service(tmp_path, monkeypatch)
    original = service.classification_loader

    def one_failure(code):
        if code == CODES[0]:
            raise RuntimeError("one candidate failed")
        return original(code)

    service.classification_loader = one_failure
    result = service.review(CODES)
    payload = public_shortlist_readiness_payload(result)
    assert result.comparison_evidence_ready is False
    assert payload["candidate_evidence"][0]["d1"]["technical_failure"] == (
        "classification_load_failed"
    )
    assert payload["candidate_evidence"][1]["d1"]["evidence_status"] == "verified"

    for error in (KeyboardInterrupt(), SystemExit()):
        service.classification_loader = lambda _code, error=error: (_ for _ in ()).throw(error)
        with pytest.raises(type(error)):
            service.review(CODES)


def test_comparison_readiness_is_independent_of_personal_gate(
    tmp_path, monkeypatch
) -> None:
    service, _repository_value, _calls = _build_service(tmp_path, monkeypatch)
    service.suitability_status_loader = lambda: {
        "state": "fresh",
        "freshness": "fresh",
        "status": "blocked",
        "hard_blocks": ["emergency_reserve_shortfall"],
    }

    result = service.review(CODES)

    assert result.comparison_evidence_ready is True
    assert result.conditional_shortlist_gate_ready is False
    assert result.action_authorized is False
    assert result.exact_amount_available is False
    assert result.automatic_trade is False


def test_healthy_alternative_wins_without_manual_supplementation(
    tmp_path, monkeypatch
) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(repository, "fund_history", lambda _code: [])

    def usable_alternative(*_args):
        snapshot = _snapshot(
            service,
            state_by_field={"formal_nav": SourceFieldState.UNAVAILABLE},
        )
        return replace(
            snapshot,
            resolutions=tuple(
                RequestFieldResolution.USABLE
                for _reference in service._source_primary_references
            ),
        )

    monkeypatch.setattr(
        service.source_health_service,
        "stored_source_status_snapshot",
        usable_alternative,
    )

    result = service.review(CODES)

    assert result.manual_supplementation == ()
    assert tuple(command for _, command in result.bounded_refresh_actions).count(
        "sync fund 000002"
    ) == 1


def test_degraded_source_returns_existing_refresh_command(tmp_path, monkeypatch) -> None:
    service, _repository_value, _calls = _build_service(tmp_path, monkeypatch)

    def degraded_source(*_args):
        snapshot = _snapshot(
            service,
            state_by_field={"formal_nav": SourceFieldState.DEGRADED},
        )
        return replace(
            snapshot,
            resolutions=tuple(
                RequestFieldResolution.PARTIAL
                if reference.field_id == "formal_nav"
                else resolution
                for reference, resolution in zip(
                    service._source_primary_references,
                    snapshot.resolutions,
                )
            ),
        )

    monkeypatch.setattr(
        service.source_health_service,
        "stored_source_status_snapshot",
        degraded_source,
    )

    result = service.review(CODES)

    assert ("000002", "sync fund 000002") in result.bounded_refresh_actions
    assert ("000001", "sync fund 000001") in result.bounded_refresh_actions
    assert result.manual_supplementation == ()


def test_source_projection_failure_is_visible_even_when_product_facts_exist(
    tmp_path, monkeypatch
) -> None:
    service, _repository_value, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service.source_health_service,
        "stored_source_status_snapshot",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("source read failed")),
    )

    result = service.review(CODES)

    assert result.manual_supplementation == (
        ("000002", "source_status"),
        ("000001", "source_status"),
    )
    assert result.bounded_refresh_actions == ()


def test_future_nav_and_future_classification_fail_closed(tmp_path, monkeypatch) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        repository,
        "fund_history",
        lambda code: [
            *_history(code),
            replace(_history(code)[0], nav_date=NOW.date() + timedelta(days=1)),
        ],
    )
    original = service.classification_loader
    service.classification_loader = lambda code: replace(
        original(code),
        classified_at=NOW + timedelta(hours=1),
        valid_until=NOW + timedelta(days=2),
    )

    result = service.review(CODES)
    payload = public_shortlist_readiness_payload(result)

    assert result.comparison_evidence_ready is False
    assert payload["candidate_evidence"][0]["formal_nav"]["future_observation_count"] == 1
    assert payload["candidate_evidence"][0]["d1"]["freshness"] == "stale"


def test_portfolio_binding_failure_prevents_comparison_readiness(
    tmp_path, monkeypatch
) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        repository,
        "latest_positions",
        lambda: (_ for _ in ()).throw(RuntimeError("position read failed")),
    )

    result = service.review(CODES)

    assert result.comparison_evidence_ready is False
    assert "portfolio_binding_load_failed" in result.blocking_codes


def test_review_does_not_write_repository_tables(tmp_path, monkeypatch) -> None:
    service, repository, _calls = _build_service(tmp_path, monkeypatch)
    with repository.connect() as connection:
        before = tuple(
            connection.execute(f'SELECT count(*) FROM "{row[0]}"').fetchone()[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        )

    service.review(CODES)

    with repository.connect() as connection:
        after = tuple(
            connection.execute(f'SELECT count(*) FROM "{row[0]}"').fetchone()[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        )
    assert after == before


def test_exact_records_reject_private_and_nonfinite_dynamic_values(
    tmp_path, monkeypatch
) -> None:
    service, _repository_value, _calls = _build_service(tmp_path, monkeypatch)
    result = service.review(CODES)
    candidate = result.candidate_evidence[0]

    with pytest.raises(ValueError, match="private field"):
        replace(candidate, profile=(("shares", "secret"),)).validate()
    with pytest.raises(ValueError, match="finite"):
        replace(candidate, formal_nav=(("value", Decimal("NaN")),)).validate()
    with pytest.raises(ValueError, match="fixed"):
        replace(result, action_authorized=True).validate()
