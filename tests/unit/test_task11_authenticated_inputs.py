from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.brief.d2 import project_adjusted_return_series_evidence
from kunjin.brief.nav import _seal_validated_adjusted_nav_series
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceTier,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.models import FundNavObservation, InvestmentThesis
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc)


def _repository(tmp_path) -> Repository:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    return repository


def _budget(request_id: str, *, now: datetime = NOW) -> RequestBudget:
    return RequestBudget.create(
        RequestMode.RAPID,
        request_id=request_id,
        monotonic=lambda: 10.0,
        wall_clock=lambda: now,
    )


def _attempt(*, finished_at: datetime, response_bytes: int) -> SourceAttempt:
    registry = SourceRegistryV1()
    return SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key="fund:123456",
        attempt_number=1,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=finished_at - timedelta(seconds=1),
        finished_at=finished_at,
        data_as_of=finished_at - timedelta(days=1),
        error_code=None,
        cooldown_until=None,
        force_actor=None,
        force_reason=None,
        registry_version=registry.version,
        registry_checksum=registry.checksum(),
        response_bytes=response_bytes,
    )


def test_request_source_attempts_bind_run_budget_and_cutoff(tmp_path) -> None:
    repository = _repository(tmp_path)
    store = DecisionAuditStore(repository)
    old_budget = _budget("1" * 32, now=NOW - timedelta(days=1))
    old_run_id = store.begin_request(old_budget)
    store.record_source_attempt(
        old_run_id,
        _attempt(finished_at=old_budget.started_at + timedelta(seconds=2), response_bytes=1),
    )
    current_budget = _budget("2" * 32)
    current_run_id = store.begin_request(current_budget)
    first_id = store.record_source_attempt(
        current_run_id,
        _attempt(finished_at=NOW + timedelta(seconds=2), response_bytes=2),
    )
    second_id = store.record_source_attempt(
        current_run_id,
        replace(
            _attempt(finished_at=NOW + timedelta(seconds=4), response_bytes=3),
            field_id="current_manager_team",
        ),
    )

    first_only = store.authenticated_request_source_attempts(
        current_run_id,
        current_budget,
        NOW + timedelta(seconds=3),
    )
    all_current = store.authenticated_request_source_attempts(
        current_run_id,
        current_budget,
        NOW + timedelta(seconds=5),
    )

    assert tuple(item.id for item in first_only) == (first_id,)
    assert tuple(item.id for item in all_current) == (first_id, second_id)
    assert all(item.request_run_id == current_run_id for item in all_current)
    assert all(item.request_id == current_budget.request_id for item in all_current)
    with pytest.raises(DecisionAuditStoreError, match="budget binding"):
        store.authenticated_request_source_attempts(
            current_run_id,
            _budget("3" * 32),
            NOW + timedelta(seconds=5),
        )


@pytest.mark.parametrize(
    "cutoff",
    (NOW - timedelta(microseconds=1), NOW + timedelta(seconds=91)),
)
def test_request_source_attempts_reject_cutoff_outside_request(tmp_path, cutoff) -> None:
    store = DecisionAuditStore(_repository(tmp_path))
    budget = _budget("4" * 32)
    request_run_id = store.begin_request(budget)

    with pytest.raises(ValueError, match="outside the request lifetime"):
        store.authenticated_request_source_attempts(request_run_id, budget, cutoff)


def test_latest_active_thesis_returns_exact_id_and_deterministic_record(tmp_path) -> None:
    repository = _repository(tmp_path)
    created_at = NOW - timedelta(days=1)
    first = InvestmentThesis("123456", "first", "one year", "manager leaves", created_at)
    second = InvestmentThesis("123456", "second", "two years", "mandate changes", created_at)
    repository.add_thesis(first)
    second_id = repository.add_thesis(second)
    repository.add_thesis(
        InvestmentThesis(
            "123456",
            "inactive later record",
            "one year",
            "already invalid",
            NOW,
            active=False,
        )
    )
    repository.add_thesis(
        InvestmentThesis("654321", "other fund", "one year", "manager leaves", NOW)
    )

    result = repository.latest_active_thesis("123456")

    assert type(result) is tuple
    assert result == (second_id, second)
    assert repository.latest_active_thesis("000000") is None
    with pytest.raises(ValueError, match="fund code"):
        repository.latest_active_thesis("12345")


def test_adjusted_series_projection_is_public_deterministic_and_d2_bound() -> None:
    attempt_id = 17
    observations = tuple(
        FundNavObservation(
            fund_code="123456",
            nav_date=date(2026, 5, 1) + timedelta(days=index),
            unit_nav=Decimal("1") + Decimal(index) / Decimal("100"),
            accumulated_nav=Decimal("1") + Decimal(index) / Decimal("100"),
            daily_growth=Decimal("0") if index == 0 else Decimal("1"),
            source="eastmoney",
            retrieved_at=NOW,
            corporate_action_state="none",
            source_attempt_id=attempt_id,
        )
        for index in range(61)
    )
    series = _seal_validated_adjusted_nav_series(
        fund_code="123456",
        observations=observations,
        source_attempt_id=attempt_id,
        retrieved_at=NOW,
        data_as_of=observations[-1].nav_date,
    )

    first = project_adjusted_return_series_evidence(series)
    second = project_adjusted_return_series_evidence(series)

    assert first == second
    assert first.fund_code == "123456"
    assert first.series is series
    assert first.evidence_fact.value == {
        "fund_code": "123456",
        "sample_count": "61",
        "start_date": "2026-05-01",
        "end_date": "2026-06-30",
        "corporate_action_state": "none",
        "calculation_version": "1",
        "source_attempt_id": "17",
    }
    assert first.evidence_fact.source_id == "eastmoney_nav"
    assert first.evidence_fact.source_tier is SourceTier.TIER_2
    assert first.evidence_fact.canonical_url == "https://fund.eastmoney.com/123456.html"
    assert first.evidence_fact.freshness is EvidenceFreshness.CURRENT
    assert first.evidence_fact.completeness is EvidenceCompleteness.COMPLETE
    assert first.evidence_fact.source_lineage_id == "source_attempt_17"
    assert first.evidence_fact.calculated is True
    first.validate()
    with pytest.raises(ValueError, match="source_binding_invalid"):
        project_adjusted_return_series_evidence(replace(series, binding_mac="0" * 64))
