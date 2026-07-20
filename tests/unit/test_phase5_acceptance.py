from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import scripts.phase5_acceptance as acceptance
from kunjin.decision.models import ActionKind
from kunjin.holding_review.engine import determine_action_review_source_sufficiency
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    ThesisMatchProjectionState,
)
from kunjin.holding_review.research import public_holding_review_payload
from kunjin.holding_review.service import HoldingReviewService
from kunjin.holding_review.thesis import ThesisReviewService
from kunjin.intelligence.models import LineageKind
from scripts.phase5_acceptance import (
    FAULT_CASES,
    MAX_SUMMARY_BYTES,
    PREVIEW_COUNTS,
    build_acceptance_parser,
    fault_fixture,
    local_fixture,
    parse_mode,
    project_acceptance,
    sanitize_encoded_output,
    validate_summary,
)
from tests.unit.test_holding_review_engine import evidence_item
from tests.unit.test_holding_review_service import (
    _align_context,
    _project_and_reject,
    _service,
)

pytest_plugins = ("tests.unit.test_holding_review_store",)


def test_preview_has_no_private_modes() -> None:
    parser = build_acceptance_parser()

    assert parse_mode(parser, "local") == "local"
    assert parse_mode(parser, "fault") == "fault"
    for mode in ("engineering", "owner"):
        with pytest.raises(SystemExit):
            parse_mode(parser, mode)


def test_preview_summary_is_non_authorizing_with_fixed_counts() -> None:
    summary = project_acceptance(local_fixture())

    assert summary["mode"] == "local"
    assert summary["outcome"] == "accepted_preview"
    assert summary["counts"] == PREVIEW_COUNTS
    assert summary["official_negative_check_complete"] is False
    assert summary["review_disposition"] == "abstain"
    assert summary["review_maturity"] == "evidence_only"
    assert summary["sell_timing"] == "insufficient_data"
    assert summary["action_authorized"] is False
    assert summary["exact_amount_available"] is False
    assert summary["automatic_trade"] is False
    assert summary["network_retries"] == 0


def test_local_preview_runs_authenticated_chain_once_without_network(
    context, monkeypatch
) -> None:
    _align_context(context)
    now = context["intelligence"].snapshot.created_at + timedelta(minutes=1)

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("phase5 acceptance attempted network access")

    monkeypatch.setattr("socket.create_connection", forbidden_network)
    thesis = ThesisReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: now,
    )
    thesis.match_project("123456", context["intelligence_run_id"])
    outcome = HoldingReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: now + timedelta(minutes=1),
    ).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )
    payload = public_holding_review_payload(outcome)

    assert payload["official_negative_check_complete"] is False
    assert payload["sell_timing"] == "insufficient_data"
    assert payload["review_boundary"] == {
        "action_authorized": False,
        "automatic_trade": False,
        "exact_amount_available": False,
        "review_maturity": "evidence_only",
    }
    with context["repository"].connect() as connection:
        observed = {
            "brief_calls": connection.execute(
                "SELECT count(*) FROM fund_brief_snapshots"
            ).fetchone()[0],
            "intelligence_calls": connection.execute(
                "SELECT count(*) FROM intelligence_snapshots"
            ).fetchone()[0],
            "match_projection_calls": connection.execute(
                "SELECT count(*) FROM thesis_match_projections"
            ).fetchone()[0],
            "adjudication_calls": connection.execute(
                "SELECT count(*) FROM thesis_evidence_adjudications"
            ).fetchone()[0],
            "holding_review_calls": connection.execute(
                "SELECT count(*) FROM holding_review_snapshots"
            ).fetchone()[0],
            "network_retries": 0,
        }
    assert observed == PREVIEW_COUNTS


def test_core_brief_snapshot_omission_is_transient_and_not_persisted(context) -> None:
    _project_and_reject(context)

    outcome = _service(context).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"] + 999,
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    assert outcome.review_snapshot is None
    assert outcome.missing_snapshot_codes == ("brief_snapshot_missing",)


def test_core_thesis_omission_runs_authenticated_abstaining_chain(context) -> None:
    _align_context(context)
    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE id=?",
            (context["thesis_id"],),
        )
    now = context["intelligence"].snapshot.created_at + timedelta(minutes=1)
    projection = ThesisReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: now,
    ).match_project("123456", context["intelligence_run_id"])
    assert projection.value.projection_state is ThesisMatchProjectionState.THESIS_MISSING

    outcome = HoldingReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: now + timedelta(minutes=1),
    ).review(
        "123456",
        action=ActionKind.CONTINUE_HOLDING,
        brief_request_run_id=context["brief_run_id"],
        intelligence_request_run_id=context["intelligence_run_id"],
    )

    assert outcome.review_snapshot.result.review_disposition.value == "abstain"
    assert outcome.review_snapshot.result.thesis_review_state.value == "thesis_missing"


def test_tier_two_only_probe_is_insufficient() -> None:
    result = determine_action_review_source_sufficiency(
        (evidence_item(source_tier=2),)
    )

    assert result is ActionReviewSourceSufficiency.INSUFFICIENT_DATA


def test_same_lineage_reprint_probe_is_insufficient() -> None:
    result = determine_action_review_source_sufficiency(
        (
            evidence_item(
                "reprint_a",
                original_lineage=False,
                lineage_kind=LineageKind.REPRINT,
            ),
            evidence_item(
                "reprint_b",
                original_lineage=False,
                lineage_kind=LineageKind.REPRINT,
            ),
        )
    )

    assert result is ActionReviewSourceSufficiency.INSUFFICIENT_DATA


@pytest.mark.parametrize("case", FAULT_CASES)
def test_fault_inventory_always_fails_closed(case: str) -> None:
    summary = project_acceptance(fault_fixture(case))
    special_outcomes = {
        "repeated_request": "history_bound_preview",
        "interrupt_cleanup": "interrupted_cleanly",
        "unexpected_exit": "child_failure_rejected",
    }

    assert summary["mode"] == "fault"
    assert summary["outcome"] == special_outcomes.get(case, "fail_closed")
    assert summary["observed_faults"] == [case]
    assert summary["review_disposition"] in {
        "abstain",
        "manual_thesis_review_required",
    }
    assert summary["official_negative_check_complete"] is False
    assert summary["action_authorized"] is False
    assert summary["exact_amount_available"] is False
    assert summary["automatic_trade"] is False
    assert summary["sell_timing"] == "insufficient_data"


@pytest.mark.parametrize(
    "case",
    (
        "brief_snapshot_missing",
        "intelligence_snapshot_missing",
        "thesis_missing",
        "official_confirmation_missing",
        "redemption_evidence_missing",
    ),
)
def test_every_core_omission_remains_visible(case: str) -> None:
    summary = project_acceptance(fault_fixture(case))

    assert case in summary["gap_codes"]
    assert "insufficient_data" in summary["gap_codes"]


def test_privacy_scan_rejects_codes_paths_amounts_and_long_secrets() -> None:
    forbidden = (
        {"fund_code": "123456"},
        {"path": "/Users/private/.local/share/kunjin/kunjin.db"},
        {"amount": "20.00"},
        {"token": "a" * 48},
    )

    for value in forbidden:
        with pytest.raises(ValueError, match="acceptance output invalid"):
            sanitize_encoded_output(json.dumps(value, sort_keys=True))


def test_acceptance_summary_is_privacy_safe() -> None:
    encoded = json.dumps(project_acceptance(local_fixture()), sort_keys=True)

    assert sanitize_encoded_output(encoded) == encoded
    assert "123456" not in encoded
    assert "/Users/" not in encoded
    assert '"amount":' not in encoded


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: {**value, "unknown": False},
        lambda value: {**value, "action_authorized": 0},
        lambda value: {**value, "network_retries": False},
        lambda value: {**value, "mode": "fault"},
        lambda value: {**value, "outcome": "accepted"},
        lambda value: {**value, "review_disposition": "continue_observing"},
        lambda value: {**value, "review_maturity": "mature"},
        lambda value: {**value, "sell_timing": "today"},
        lambda value: {**value, "owner_email": "private@example.test"},
        lambda value: {**value, "counts": {**value["counts"], "extra": 0}},
        lambda value: {**value, "gap_codes": list(reversed(value["gap_codes"]))},
    ),
)
def test_strict_local_schema_rejects_shape_type_and_fixed_value_drift(mutation) -> None:
    value = project_acceptance(local_fixture())

    with pytest.raises(ValueError, match="acceptance output invalid"):
        validate_summary(mutation(value), expected_mode="local")


def test_summary_validator_rejects_oversized_content_and_private_sentinels() -> None:
    value = project_acceptance(local_fixture())
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    assert len(encoded.encode("ascii")) < MAX_SUMMARY_BYTES

    for private in (
        "private@example.test",
        "/Users/private/kunjin.db",
        "OWNER_PRIVATE_SENTINEL",
        "a" * 48,
    ):
        with pytest.raises(ValueError, match="acceptance output invalid"):
            sanitize_encoded_output(encoded[:-1] + json.dumps(private) + "}")

    with pytest.raises(ValueError, match="acceptance output invalid"):
        sanitize_encoded_output("{" + " " * MAX_SUMMARY_BYTES + "}")


def test_fault_summary_requires_one_verified_observation_per_case() -> None:
    summary = {
        "acceptance_scope": "synthetic_local_faults_only",
        "action_authorized": False,
        "automatic_trade": False,
        "case_count": len(FAULT_CASES),
        "exact_amount_available": False,
        "fault_cases": list(FAULT_CASES),
        "mode": "fault",
        "network_retries": 0,
        "observations": [
            {
                "case": case,
                "evidence_checksum": f"{index:064x}",
                "probe_kind": "pytest",
                "status": "verified",
            }
            for index, case in enumerate(FAULT_CASES, start=1)
        ],
        "official_negative_check_complete": False,
        "outcome": "fault_contract_verified",
        "review_disposition": "abstain",
        "review_maturity": "evidence_only",
        "sell_timing": "insufficient_data",
    }
    validate_summary(summary, expected_mode="fault")

    for changed in (
        {**summary, "observations": summary["observations"][:-1]},
        {
            **summary,
            "observations": [
                *summary["observations"][:-1],
                {**summary["observations"][-1], "status": "declared"},
            ],
        },
        {**summary, "case_count": True},
    ):
        with pytest.raises(ValueError, match="acceptance output invalid"):
            validate_summary(changed, expected_mode="fault")


def test_independent_file_validator_rejects_tampering(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    tmp_path.chmod(0o700)
    summary_path = tmp_path / "summary.json"
    valid = project_acceptance(local_fixture())
    monkeypatch.setenv("KUNJIN_PHASE5_RUNTIME_DIR", str(tmp_path))
    tampered_values = (
        json.dumps({**valid, "action_authorized": 0}),
        json.dumps({**valid, "owner_email": "private@example.test"}),
        '{"mode":"local","mode":"fault"}',
        "{" + " " * MAX_SUMMARY_BYTES + "}",
    )

    for encoded in tampered_values:
        summary_path.write_text(encoded, encoding="ascii")
        summary_path.chmod(0o600)
        assert acceptance.main(["validate", "local", str(summary_path)]) == 70
        captured = capsys.readouterr()
        assert "phase5_acceptance_failed" in captured.out
        assert "private@example.test" not in captured.out
