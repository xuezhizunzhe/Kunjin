from __future__ import annotations

import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

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
    owner_acceptance,
    parse_mode,
    possible_match_fixture,
    project_acceptance,
    sanitize_encoded_output,
    secure_read_private_subject,
    validate_summary,
)
from tests.unit.test_holding_review_engine import evidence_item
from tests.unit.test_holding_review_service import (
    _align_context,
    _project_and_reject,
    _service,
)

pytest_plugins = ("tests.unit.test_holding_review_store",)


def test_acceptance_parser_has_exact_finite_modes() -> None:
    parser = build_acceptance_parser()

    for mode in ("local", "fault", "engineering", "owner"):
        assert parse_mode(parser, mode) == mode
    with pytest.raises(SystemExit):
        parse_mode(parser, "deep")


def test_owner_acceptance_token_cannot_adjudicate() -> None:
    summary = owner_acceptance(possible_match_fixture())

    assert summary["counts"]["adjudication_calls"] == 0
    assert summary["thesis_review_readiness"] == "manual_review_required"
    assert summary["action_authorized"] is False
    assert summary["automatic_trade"] is False
    assert summary["exact_amount_available"] is False
    assert set(summary) == {
        "action_authorized",
        "automatic_trade",
        "conditional_review_usability",
        "counts",
        "engineering_flow",
        "evidence_readiness",
        "exact_amount_available",
        "history_comparability",
        "mode",
        "redemption_feasibility",
        "sell_timing",
        "thesis_review_readiness",
    }


def test_owner_keychain_child_receives_no_acceptance_secret_environment(
    tmp_path: Path, monkeypatch
) -> None:
    observed = []

    class Phase41:
        @staticmethod
        def _canonical_home():
            return tmp_path

        @staticmethod
        def load_owner_key_once(runner):
            result = runner(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    "com.kunjin.profile-encryption",
                    "-a",
                    "v1",
                    "-w",
                ]
            )
            assert result[0] == 0
            return b"k" * 32

    def fake_run(*_args, **kwargs):
        observed.append(kwargs["env"])
        return SimpleNamespace(returncode=0, stdout="opaque", stderr="")

    monkeypatch.setattr(acceptance.subprocess, "run", fake_run)

    key = acceptance._load_owner_key_without_sensitive_environment(Phase41)

    assert key == b"k" * 32
    assert len(observed) == 1
    assert all(not key.startswith("KUNJIN_PHASE5_") for key in observed[0])


def test_adjudication_digest_detects_copy_mutation(tmp_path: Path) -> None:
    database = tmp_path / "copy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE thesis_evidence_adjudications("
            "id INTEGER PRIMARY KEY, record_checksum TEXT NOT NULL)"
        )
    before = acceptance._adjudication_digest(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO thesis_evidence_adjudications VALUES(1, ?)",
            ("a" * 64,),
        )

    assert acceptance._adjudication_digest(database) != before


@pytest.mark.parametrize(
    "state,expected",
    (
        ("manual_review_pending", "manual_review_required"),
        ("manual_review_uncertain", "manual_review_required"),
        ("thesis_missing", "missing"),
        ("no_matching_evidence", "ready"),
        ("presented_match_confirmed", "ready"),
        ("presented_match_rejected", "ready"),
        ("thesis_binding_invalid", "insufficient_data"),
    ),
)
def test_private_thesis_readiness_mapping_is_explicit(state: str, expected: str) -> None:
    assert acceptance._private_thesis_readiness(state) == expected


def test_private_summary_requires_subject_projection_and_complete_call_binding() -> None:
    subject = acceptance.PrivateSubject("123456", ActionKind.CONTINUE_HOLDING)
    review = {
        "flow_status": "partial",
        "fund_code": "123456",
        "action": "continue_holding",
        "interpretation": {
            "review_disposition": "abstain",
            "thesis_review_state": "no_matching_evidence",
        },
        "candidate_thesis_match": {"projection_id": 13},
        "review_boundary": {
            "action_authorized": False,
            "automatic_trade": False,
            "exact_amount_available": False,
            "review_maturity": "evidence_only",
        },
        "evidence_readiness": "partial",
        "evidence_delta": {"history_comparability": "not_available"},
        "redemption": {"feasibility": "not_requested"},
        "sell_timing": "insufficient_data",
    }
    valid = acceptance.PrivateChainResult(review, acceptance.PREVIEW_COUNTS, 13)
    assert acceptance._private_summary_from_review(
        "engineering", subject, valid, adjudication_unchanged=True
    )["engineering_flow"] == "pass"

    invalid = (
        acceptance.PrivateChainResult(
            {**review, "fund_code": "654321"}, acceptance.PREVIEW_COUNTS, 13
        ),
        acceptance.PrivateChainResult(
            {**review, "action": "full_exit"}, acceptance.PREVIEW_COUNTS, 13
        ),
        acceptance.PrivateChainResult(review, acceptance.PREVIEW_COUNTS, 14),
        acceptance.PrivateChainResult(review, {**acceptance.PREVIEW_COUNTS, "brief_calls": 0}, 13),
    )
    for chain in invalid:
        with pytest.raises(ValueError, match="private acceptance"):
            acceptance._private_summary_from_review(
                "engineering", subject, chain, adjudication_unchanged=True
            )
    with pytest.raises(ValueError, match="private acceptance"):
        acceptance._private_summary_from_review(
            "engineering", subject, valid, adjudication_unchanged=False
        )


def _private_e2e_fixture(tmp_path: Path, mode: str):
    source_parent = tmp_path / "source"
    source_parent.mkdir(mode=0o700)
    source = source_parent / "kunjin.db"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE thesis_evidence_adjudications("
            "id INTEGER PRIMARY KEY, record_checksum TEXT NOT NULL)"
        )
    source.chmod(0o600)
    subject_parent = tmp_path / f"{mode}-subject"
    subject_parent.mkdir(mode=0o700)
    subject = subject_parent / "subject.json"
    subject.write_text(
        '{"fund_code":"123456","action":"continue_holding"}',
        encoding="ascii",
    )
    subject.chmod(0o600)
    runtime = tmp_path / f"{mode}-runtime"
    runtime.mkdir(mode=0o700)
    return source, subject, runtime


@pytest.mark.parametrize("mode", ("engineering", "owner"))
def test_private_mode_synthetic_e2e_is_copy_only_and_non_adjudicating(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    from scripts import phase41_acceptance as phase41

    source, subject_path, runtime = _private_e2e_fixture(tmp_path, mode)
    subject_env = (
        "KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE"
        if mode == "engineering"
        else "KUNJIN_PHASE5_OWNER_SUBJECT_FILE"
    )
    monkeypatch.setenv(subject_env, str(subject_path))
    if mode == "owner":
        monkeypatch.setenv(
            "KUNJIN_PHASE5_OWNER_APPROVED", "explicit_private_read_only_review"
        )
    monkeypatch.delenv("KUNJIN_DATA_DIR", raising=False)
    monkeypatch.delenv("KUNJIN_STATE_DIR", raising=False)
    monkeypatch.setattr(phase41, "_canonical_owner_database", lambda: source)
    key_calls = []

    def fake_key(_phase41):
        assert all(
            name not in os.environ
            for name in (
                "KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE",
                "KUNJIN_PHASE5_OWNER_SUBJECT_FILE",
                "KUNJIN_PHASE5_OWNER_APPROVED",
            )
        )
        key_calls.append("owner")
        return b"k" * 32

    monkeypatch.setattr(acceptance, "_load_owner_key_without_sensitive_environment", fake_key)
    portfolio = SimpleNamespace(sync=lambda *_args, **_kwargs: None)
    repository = SimpleNamespace(
        latest_positions=lambda: [SimpleNamespace(fund_code="123456", shares=1)]
    )
    context = SimpleNamespace(
        repository=repository,
        brief_service=SimpleNamespace(_portfolio_service=portfolio),
    )
    calls = []

    class FakeCli:
        @staticmethod
        def run(argv, received_context):
            assert received_context is context
            assert subject_env not in os.environ
            assert os.environ["KUNJIN_DATA_DIR"] == str(runtime / "data")
            assert os.environ["KUNJIN_STATE_DIR"] == str(runtime / "state")
            with pytest.raises(OSError, match="portfolio refresh prohibited"):
                context.brief_service._portfolio_service.sync()
            with pytest.raises(OSError, match="external operation prohibited"):
                acceptance.socket.create_connection(("example.invalid", 443))
            calls.append(tuple(argv))
            if argv[1:3] == ["fund", "brief"]:
                command, data = "fund.brief", {"request": {"request_run_id": 11}}
            elif argv[1:3] == ["fund", "intelligence"]:
                command, data = "fund.intelligence", {"request": {"request_run_id": 12}}
            elif argv[1:3] == ["thesis", "match-project"]:
                command, data = "thesis.match-project", {"id": 13, "projection": {}}
            else:
                command = "fund.holding-review"
                data = {
                    "flow_status": "partial",
                    "fund_code": "123456",
                    "action": "continue_holding",
                    "interpretation": {
                        "review_disposition": "abstain",
                        "thesis_review_state": "no_matching_evidence",
                    },
                    "candidate_thesis_match": {"projection_id": 13},
                    "review_boundary": {
                        "action_authorized": False,
                        "automatic_trade": False,
                        "exact_amount_available": False,
                        "review_maturity": "evidence_only",
                    },
                    "evidence_readiness": "partial",
                    "evidence_delta": {"history_comparability": "not_available"},
                    "redemption": {"feasibility": "not_requested"},
                    "sell_timing": "insufficient_data",
                }
            return {"command": command, "data": data}, 0, True

    def fake_build(key: bytes):
        assert key == (b"k" * 32 if mode == "owner" else b"\0" * 32)
        assert os.environ["KUNJIN_DATA_DIR"] == str(runtime / "data")
        assert os.environ["KUNJIN_STATE_DIR"] == str(runtime / "state")
        return FakeCli, context

    monkeypatch.setattr(phase41, "_build_context_with_key", fake_build)

    summary = acceptance.project_mode(mode, runtime)

    assert summary["engineering_flow"] == "pass"
    assert summary["counts"] == acceptance.PREVIEW_COUNTS
    assert summary["thesis_review_readiness"] == "ready"
    assert len(calls) == 4
    assert key_calls == (["owner"] if mode == "owner" else [])
    assert os.environ[subject_env] == str(subject_path)
    with sqlite3.connect(source) as connection:
        assert connection.execute(
            "SELECT count(*) FROM thesis_evidence_adjudications"
        ).fetchone()[0] == 0


def test_owner_synthetic_e2e_rejects_not_held_latest_local_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts import phase41_acceptance as phase41

    source, subject_path, runtime = _private_e2e_fixture(tmp_path, "owner")
    monkeypatch.setenv("KUNJIN_PHASE5_OWNER_SUBJECT_FILE", str(subject_path))
    monkeypatch.setenv(
        "KUNJIN_PHASE5_OWNER_APPROVED", "explicit_private_read_only_review"
    )
    monkeypatch.delenv("KUNJIN_DATA_DIR", raising=False)
    monkeypatch.delenv("KUNJIN_STATE_DIR", raising=False)
    monkeypatch.setattr(phase41, "_canonical_owner_database", lambda: source)
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda _phase41: b"k" * 32,
    )
    context = SimpleNamespace(
        repository=SimpleNamespace(latest_positions=lambda: []),
        brief_service=SimpleNamespace(_portfolio_service=SimpleNamespace(sync=None)),
    )
    monkeypatch.setattr(
        phase41, "_build_context_with_key", lambda _key: (SimpleNamespace(), context)
    )

    with pytest.raises(ValueError, match="latest local snapshot"):
        acceptance.run_private_acceptance("owner", runtime)

    assert os.environ["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"] == str(subject_path)


@pytest.mark.parametrize("failed_call", range(4))
def test_private_chain_rejects_exit_one_at_every_required_step(
    monkeypatch, failed_call: int
) -> None:
    from scripts import phase41_acceptance as phase41

    subject = acceptance.PrivateSubject("123456", ActionKind.CONTINUE_HOLDING)
    portfolio = SimpleNamespace(sync=lambda *_args, **_kwargs: None)
    context = SimpleNamespace(
        repository=SimpleNamespace(
            latest_positions=lambda: [SimpleNamespace(fund_code="123456", shares=1)]
        ),
        brief_service=SimpleNamespace(_portfolio_service=portfolio),
    )
    calls = []

    class FailingCli:
        @staticmethod
        def run(argv, _context):
            index = len(calls)
            calls.append(tuple(argv))
            if argv[1:3] == ["fund", "brief"]:
                command, data = "fund.brief", {"request": {"request_run_id": 11}}
            elif argv[1:3] == ["fund", "intelligence"]:
                command, data = "fund.intelligence", {"request": {"request_run_id": 12}}
            elif argv[1:3] == ["thesis", "match-project"]:
                command, data = "thesis.match-project", {"id": 13}
            else:
                command, data = "fund.holding-review", {}
            return {"command": command, "data": data}, int(index == failed_call), True

    monkeypatch.setattr(
        phase41,
        "_build_context_with_key",
        lambda _key: (FailingCli, context),
    )

    with pytest.raises(ValueError, match="private acceptance command failed"):
        acceptance._run_private_chain("engineering", subject, b"\0" * 32)

    assert len(calls) == failed_call + 1


def test_private_subject_file_is_exact_private_and_outside_git(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    subject = parent / "subject.json"
    subject.write_text(
        json.dumps({"fund_code": "123456", "action": "continue_holding"}),
        encoding="ascii",
    )
    subject.chmod(0o600)

    loaded = secure_read_private_subject(subject, excluded_roots=())

    assert loaded.fund_code == "123456"
    assert loaded.action is ActionKind.CONTINUE_HOLDING

    subject.chmod(0o644)
    with pytest.raises(ValueError, match="subject file"):
        secure_read_private_subject(subject, excluded_roots=())


@pytest.mark.parametrize(
    "encoded",
    (
        '{"fund_code":"123456","fund_code":"654321","action":"continue_holding"}',
        '{"fund_code":"123456","action":"continue_holding","extra":false}',
        '{"fund_code":["123456"],"action":"continue_holding"}',
        '{"fund_code":"000000","action":"continue_holding"}',
        '{"fund_code":"123456","action":"switch_funds"}',
    ),
)
def test_private_subject_rejects_duplicate_extra_and_invalid_values(
    tmp_path: Path, encoded: str
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    subject = parent / "subject.json"
    subject.write_text(encoded, encoding="ascii")
    subject.chmod(0o600)

    with pytest.raises(ValueError, match="subject file"):
        secure_read_private_subject(subject, excluded_roots=())


def test_private_subject_rejects_symlink_fifo_and_git_ancestor(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    target = parent / "target.json"
    target.write_text(
        '{"fund_code":"123456","action":"continue_holding"}',
        encoding="ascii",
    )
    target.chmod(0o600)
    symlink = parent / "subject-link.json"
    symlink.symlink_to(target)
    fifo = parent / "subject.fifo"
    os.mkfifo(fifo, mode=0o600)

    for path in (symlink, fifo):
        with pytest.raises(ValueError, match="subject file"):
            secure_read_private_subject(path, excluded_roots=())

    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="subject file"):
        secure_read_private_subject(target, excluded_roots=())


def test_engineering_and_owner_subject_files_cannot_alias(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    subject = parent / "subject.json"
    subject.write_text(
        json.dumps({"fund_code": "123456", "action": "continue_holding"}),
        encoding="ascii",
    )
    subject.chmod(0o600)

    with pytest.raises(ValueError, match="separate"):
        acceptance.private_subject_path(
            "owner",
            {
                "KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE": str(subject),
                "KUNJIN_PHASE5_OWNER_SUBJECT_FILE": str(subject),
            },
        )


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
