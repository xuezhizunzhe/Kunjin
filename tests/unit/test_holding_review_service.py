from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kunjin.decision.models import (
    ActionKind,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    StoredSourceAttempt,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.holding_review.models import (
    AdjudicationDecision,
    FlowStatus,
    ReviewBoundary,
    ReviewDisposition,
    ThesisMatchState,
)
from kunjin.holding_review.service import (
    HoldingReviewService,
    HoldingReviewServiceError,
    _fund_intelligence_schedule,
)
from kunjin.holding_review.thesis import ThesisReviewService
from kunjin.intelligence.policy import IntelligencePolicyV1
from kunjin.models import InvestmentThesis

pytest_plugins = ("tests.unit.test_holding_review_store",)


def _service(context, *, now: datetime | None = None) -> HoldingReviewService:
    terminal_times = (
        context["store"].intelligence_store.authenticated_terminal_request(
            context["brief_run_id"]
        ).finished_at,
        context["store"].intelligence_store.authenticated_terminal_request(
            context["intelligence_run_id"]
        ).finished_at,
    )
    return HoldingReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: now or max(terminal_times) + timedelta(minutes=1),
    )


def _align_context(context) -> None:
    aligned_at = context["intelligence"].snapshot.created_at
    snapshot = replace(context["brief"].snapshot, created_at=aligned_at)
    canonical = snapshot.canonical_json()
    checksum = hashlib.sha256(canonical).hexdigest()
    with context["repository"].connect() as connection, connection:
        connection.execute("DROP TRIGGER fund_brief_snapshot_no_update")
        connection.execute("DROP TRIGGER request_run_update_guard")
        connection.execute("DROP TRIGGER source_attempt_no_update")
        connection.execute(
            """
            UPDATE fund_brief_snapshots
            SET canonical_snapshot_json=?, result_checksum=?, created_at=?
            WHERE request_run_id=?
            """,
            (
                canonical.decode("ascii"),
                checksum,
                aligned_at.isoformat(),
                context["brief_run_id"],
            ),
        )
        connection.execute(
            """
            UPDATE request_runs
            SET started_at=?, deadline_at=?, finished_at=?
            WHERE id=?
            """,
            (
                (aligned_at - timedelta(seconds=1)).isoformat(),
                (aligned_at + timedelta(seconds=2)).isoformat(),
                (aligned_at + timedelta(seconds=1)).isoformat(),
                context["brief_run_id"],
            ),
        )
        connection.execute(
            "UPDATE investment_theses SET created_at=? WHERE id=?",
            (
                (
                    context["intelligence"].snapshot.created_at
                    - timedelta(days=1)
                ).isoformat(),
                context["thesis_id"],
            ),
        )
        connection.execute(
            """
            UPDATE source_attempts
            SET field_id='policy_events', registry_checksum=?
            WHERE request_run_id=? AND source_id='gov_cn_policy'
            """,
            (SourceRegistryV1().checksum(), context["intelligence_run_id"]),
        )
    context["brief"] = context[
        "store"
    ].brief_store.authenticated_snapshot_by_request_run_id(context["brief_run_id"])


def _project_and_reject(context):
    _align_context(context)
    thesis = ThesisReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: max(
            context["brief"].snapshot.created_at,
            context["intelligence"].snapshot.created_at,
        )
        + timedelta(minutes=1),
    )
    projection = thesis.match_project("123456", context["intelligence_run_id"])
    adjudication = thesis.adjudicate(
        "123456",
        projection.id,
        AdjudicationDecision.PRESENTED_MATCH_REJECTED,
    )
    return thesis, projection, adjudication


def _replace_brief(context, **changes: object) -> None:
    snapshot = replace(context["brief"].snapshot, **changes)
    canonical = snapshot.canonical_json()
    checksum = hashlib.sha256(canonical).hexdigest()
    with context["repository"].connect() as connection, connection:
        connection.execute(
            """
            UPDATE fund_brief_snapshots
            SET canonical_snapshot_json=?, result_checksum=?, conflicts_json=?, created_at=?
            WHERE request_run_id=?
            """,
            (
                canonical.decode("ascii"),
                checksum,
                "[\"brief_identity_conflict\"]" if snapshot.conflicts else "[]",
                snapshot.created_at.isoformat(),
                context["brief_run_id"],
            ),
        )
    context["brief"] = context[
        "store"
    ].brief_store.authenticated_snapshot_by_request_run_id(context["brief_run_id"])


def _review_count(context) -> int:
    with context["repository"].connect() as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM holding_review_snapshots"
            ).fetchone()[0]
        )


def test_preview_is_network_free_and_fixed_closed(monkeypatch, context) -> None:
    _project_and_reject(context)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("holding review preview attempted network access")

    monkeypatch.setattr("socket.create_connection", forbidden_network)
    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    outcome.validate()
    assert outcome.flow_status in {FlowStatus.COMPLETE, FlowStatus.PARTIAL}
    assert outcome.review_snapshot.result.review_disposition is ReviewDisposition.ABSTAIN
    assert outcome.review_snapshot.result.official_event_evidence == ()
    assert outcome.review_snapshot.result.official_negative_check_complete is False
    assert "official_deep_confirmation_deferred" in (
        outcome.review_snapshot.result.omitted_work
    )
    assert outcome.review_snapshot.result.boundary == ReviewBoundary()


def test_missing_exact_snapshot_is_transient_and_not_persisted(context) -> None:
    before = _review_count(context)

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"] + 999,
    )

    outcome.validate()
    assert outcome.flow_status is FlowStatus.PARTIAL
    assert outcome.review_snapshot is None
    assert outcome.missing_snapshot_codes == ("intelligence_snapshot_missing",)
    assert _review_count(context) == before


def test_required_fund_schedule_missing_sources_is_visible(context) -> None:
    _project_and_reject(context)

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    result = outcome.review_snapshot.result
    assert result.intelligence_schedule_complete is False
    assert result.intelligence_degraded_sources == (
        "eastmoney_market",
        "stcn_fund_news",
    )
    assert "eastmoney_market_attempt_missing" in result.intelligence_omitted_work
    assert "stcn_fund_news_attempt_missing" in result.intelligence_omitted_work


def test_brief_conflict_is_visible_and_abstains(context) -> None:
    _project_and_reject(context)
    _replace_brief(context, conflicts=("brief_identity_conflict",))

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    result = outcome.review_snapshot.result
    assert "brief_identity_conflict" in result.omitted_work
    assert "brief_identity_conflict" in result.upstream_action_boundary
    assert result.review_disposition is ReviewDisposition.ABSTAIN


def test_not_held_returns_transient_without_persistence(context) -> None:
    _project_and_reject(context)
    _replace_brief(context, position_present=False)
    before = _review_count(context)

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    assert outcome.review_snapshot is None
    assert outcome.missing_snapshot_codes == ("current_position_missing",)
    assert _review_count(context) == before


def test_snapshot_probe_database_failure_is_sanitized(monkeypatch, context) -> None:
    service = _service(context)

    def fail_connect():
        raise sqlite3.OperationalError("private path /private/tmp/owner.db")

    monkeypatch.setattr(context["repository"], "connect", fail_connect)

    with pytest.raises(HoldingReviewServiceError) as raised:
        service.review(
            "123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=999,
            intelligence_request_run_id=1000,
        )
    assert str(raised.value) == "snapshot existence check failed"
    assert "/private/" not in str(raised.value)


@pytest.mark.parametrize(
    "fund_code,action,error",
    (
        ("654321", ActionKind.CONTINUE_HOLDING, "subject binding"),
        ("123456", ActionKind.FULL_EXIT, "action binding"),
    ),
)
def test_wrong_fund_or_action_fails_closed(context, fund_code, action, error) -> None:
    _project_and_reject(context)

    with pytest.raises(HoldingReviewServiceError, match=error):
        _service(context).review(
            fund_code,
            action=action,
            brief_request_run_id=context["brief_run_id"],
            intelligence_request_run_id=context["intelligence_run_id"],
        )


@pytest.mark.parametrize("status", ("running", "failed"))
def test_nonterminal_or_failed_request_fails_closed(context, status) -> None:
    _project_and_reject(context)
    service = _service(context)
    with context["repository"].connect() as connection, connection:
        if status == "running":
            connection.execute(
                "UPDATE request_runs SET status='running', finished_at=NULL WHERE id=?",
                (context["intelligence_run_id"],),
            )
        else:
            connection.execute(
                "UPDATE request_runs SET status='failed' WHERE id=?",
                (context["intelligence_run_id"],),
            )

    with pytest.raises(
        HoldingReviewServiceError, match="intelligence snapshot authentication failed"
    ):
        service.review(
            "123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=context["brief_run_id"],
            intelligence_request_run_id=context["intelligence_run_id"],
        )


def test_orchestration_window_accepts_exact_boundary_and_rejects_after(context) -> None:
    _project_and_reject(context)
    terminals = (
        context["store"].intelligence_store.authenticated_terminal_request(
            context["brief_run_id"]
        ),
        context["store"].intelligence_store.authenticated_terminal_request(
            context["intelligence_run_id"]
        ),
    )
    latest = max(item.finished_at for item in terminals)

    accepted = _service(context, now=latest + timedelta(minutes=30)).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )
    assert accepted.review_snapshot is not None
    with pytest.raises(HoldingReviewServiceError, match="orchestration window"):
        _service(
            context,
            now=latest + timedelta(minutes=30, microseconds=1),
        ).review(
            "123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=context["brief_run_id"],
            intelligence_request_run_id=context["intelligence_run_id"],
        )


def test_projection_must_exist_for_exact_intelligence_request(context) -> None:
    _align_context(context)

    with pytest.raises(HoldingReviewServiceError, match="projection missing"):
        _service(context).review(
            "123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=context["brief_run_id"],
            intelligence_request_run_id=context["intelligence_run_id"],
        )


def test_current_superseding_adjudication_is_used(context) -> None:
    thesis, projection, first = _project_and_reject(context)
    thesis.adjudicate(
        "123456",
        projection.id,
        AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
        supersedes_id=first.id,
    )

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    assert outcome.review_snapshot.result.thesis_review_state is (
        ThesisMatchState.PRESENTED_MATCH_CONFIRMED
    )
    assert outcome.review_snapshot.adjudication_id != first.id


def test_stale_projection_after_thesis_replacement_fails_closed(context) -> None:
    _project_and_reject(context)
    context["repository"].add_thesis(
        InvestmentThesis(
            fund_code="123456",
            rationale="Replacement synthetic thesis.",
            horizon="Three years.",
            invalidation="Replacement invalidation condition.",
            created_at=context["intelligence"].snapshot.created_at + timedelta(minutes=2),
        )
    )

    with pytest.raises(HoldingReviewServiceError, match="authentication failed"):
        _service(context).review(
            "123456",
            action=ActionKind.CONTINUE_HOLDING,
            brief_request_run_id=context["brief_run_id"],
            intelligence_request_run_id=context["intelligence_run_id"],
        )


def test_latest_comparable_prior_is_bound_without_history_fallback(context) -> None:
    _project_and_reject(context)
    first = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )
    with context["repository"].connect() as connection:
        first_id = int(
            connection.execute(
                "SELECT id FROM holding_review_snapshots"
            ).fetchone()[0]
        )
    second = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    assert first.review_snapshot.previous_review_id is None
    assert second.review_snapshot.previous_review_id == first_id


@pytest.mark.parametrize(
    "outcome,error_code,cooldown",
    (
        (SourceAttemptOutcome.UNAVAILABLE, SourceErrorCode.SOURCE_UNAVAILABLE, False),
        (SourceAttemptOutcome.UNSUPPORTED, SourceErrorCode.FIELD_UNSUPPORTED, False),
        (SourceAttemptOutcome.SKIPPED_COOLDOWN, SourceErrorCode.COOLDOWN_ACTIVE, True),
    ),
)
def test_failed_required_source_is_degraded_and_omitted(
    outcome, error_code, cooldown
) -> None:
    now = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
    registry = SourceRegistryV1()
    fields = {
        "eastmoney_market": "market_dimensions",
        "gov_cn_policy": "policy_events",
        "stcn_fund_news": "fund_media_events",
    }
    attempts = []
    for index, (source_id, field_id) in enumerate(fields.items(), start=1):
        current_outcome = outcome if source_id == "gov_cn_policy" else SourceAttemptOutcome.SUCCESS
        attempt = SourceAttempt(
            source_id=source_id,
            field_id=field_id,
            subject_key="fund:123456",
            attempt_number=1,
            outcome=current_outcome,
            started_at=now,
            finished_at=now + timedelta(seconds=1),
            data_as_of=(
                now if current_outcome is SourceAttemptOutcome.SUCCESS else None
            ),
            error_code=(error_code if source_id == "gov_cn_policy" else None),
            cooldown_until=(
                now + timedelta(minutes=1)
                if source_id == "gov_cn_policy" and cooldown
                else None
            ),
            force_actor=None,
            force_reason=None,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=(10 if current_outcome is SourceAttemptOutcome.SUCCESS else 0),
        )
        attempts.append(StoredSourceAttempt(index, 7, "d" * 32, None, attempt))

    complete, omitted, degraded = _fund_intelligence_schedule(
        tuple(attempts), "123456", IntelligencePolicyV1()
    )

    assert complete is False
    assert degraded == ("gov_cn_policy",)
    assert f"gov_cn_policy_{outcome.value}" in omitted
