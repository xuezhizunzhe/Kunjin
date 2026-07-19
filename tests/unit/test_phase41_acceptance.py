from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import time
from itertools import combinations
from pathlib import Path

import pytest

import scripts.phase41_acceptance as acceptance
from scripts.phase41_acceptance import (
    ACTION_BOUNDARY,
    CommandResult,
    InMemoryKeyStore,
    OwnerRuntimeGuards,
    ReadOnlyDatabaseGuard,
    StableFailure,
    TrackedChildren,
    assert_same_inode,
    backup_sqlite_read_only,
    load_owner_key_once,
    orchestrate,
    project_engineering_evidence,
    sanitize_output,
    secure_read_subject_file,
    validate_engineering_flow,
    validate_owner_statuses,
)

ROLES = tuple(f"engineering_subject_{index}" for index in range(1, 5))
CODES = ("000001", "000002", "000003", "000004")


def _write_subject_file(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir(mode=0o700, parents=True)
    private.chmod(0o700)
    subject = private / "subjects.json"
    subject.write_text(
        json.dumps(dict(zip(ROLES, CODES)), separators=(",", ":")),
        encoding="ascii",
    )
    subject.chmod(0o600)
    return subject


def _readiness(actions=(), *, ready=False, evidence=None, blocking=None, codes=CODES):
    candidate_evidence = (
        [{"fund_code": code} for code in codes]
        if evidence is None
        else [
            {"fund_code": code, **item}
            for code, item in zip(codes, evidence)
        ]
    )
    return {
        "command": "fund.shortlist-readiness",
        "data": {
            "request": {
                "candidate_codes": list(codes),
                "candidate_count": len(codes),
            },
            "bounded_refresh_actions": [
                {"fund_code": code, "command": command} for code, command in actions
            ],
            "candidate_evidence": candidate_evidence,
            "comparison_evidence_ready": ready,
            "blocking_codes": [] if blocking is None else blocking,
        },
    }


def _source_status(
    code: str,
    *,
    resolution: str = "usable",
    primary_state: str = "healthy",
    alternative_state: str | None = None,
):
    rows = [
        {
            "field_id": "formal_nav",
            "source_id": "eastmoney_nav",
            "state": primary_state,
        }
    ]
    if alternative_state is not None:
        rows.append(
            {
                "field_id": "formal_nav",
                "source_id": "alternative_nav",
                "state": alternative_state,
            }
        )
    return {
        "command": "source.status",
        "data": {
            "fund_code": code,
            "request_field_resolutions": [
                {
                    "field_id": "formal_nav",
                    "primary_source_id": "eastmoney_nav",
                    "resolution": resolution,
                }
            ],
            "source_fields": rows,
        },
    }


def test_private_subject_file_requires_nofollow_modes_and_outside_roots(tmp_path) -> None:
    subject = _write_subject_file(tmp_path)
    repository = tmp_path / "repository"
    skill = tmp_path / "skill"
    logs = tmp_path / "logs"
    for directory in (repository, skill, logs):
        directory.mkdir(mode=0o700)

    assert secure_read_subject_file(subject, (repository, skill, logs)) == CODES

    link = subject.parent / "link.json"
    link.symlink_to(subject)
    with pytest.raises(StableFailure, match="engineering_subject_file_invalid"):
        secure_read_subject_file(link, (repository, skill, logs))

    subject.chmod(0o640)
    with pytest.raises(StableFailure, match="engineering_subject_file_invalid"):
        secure_read_subject_file(subject, (repository, skill, logs))


def test_private_subject_file_rejects_log_root_and_nonprivate_parent(tmp_path) -> None:
    log_root = tmp_path / "logs"
    log_root.mkdir(mode=0o700)
    subject = _write_subject_file(log_root)
    with pytest.raises(StableFailure, match="engineering_subject_file_invalid"):
        secure_read_subject_file(subject, (log_root,))

    subject = _write_subject_file(tmp_path / "second")
    subject.parent.chmod(0o755)
    with pytest.raises(StableFailure, match="engineering_subject_file_invalid"):
        secure_read_subject_file(subject, ())


def test_sqlite_backup_uses_validated_inode_and_private_target(tmp_path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('encrypted')")
    source.chmod(0o600)
    target = tmp_path / "private" / "copy.db"
    target.parent.mkdir(mode=0o700)

    source_hash, data_version = backup_sqlite_read_only(source, target)

    assert len(source_hash) == 64
    assert type(data_version) is int
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("encrypted",)


def test_actual_database_guard_and_child_tracker_close_resources(tmp_path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE evidence(value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('encrypted')")
    source.chmod(0o600)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "copy.db"

    with ReadOnlyDatabaseGuard(source, target) as guard:
        guard.verify()
        with pytest.raises(sqlite3.OperationalError):
            guard.connection.execute("INSERT INTO evidence VALUES ('write')")
    assert guard.fd == -1
    assert guard.connection is None

    with TrackedChildren() as children:
        children.install_tracking()
        completed = subprocess.run(["/usr/bin/true"], check=False)
        children.restore_tracking()
        assert completed.returncode == 0
        children.assert_waited()
    assert len(children._children) == 1


def test_inode_replacement_and_database_symlink_fail_closed(tmp_path) -> None:
    database = tmp_path / "database.db"
    database.write_bytes(b"first")
    database.chmod(0o600)
    fd = os.open(database, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    replacement = tmp_path / "replacement.db"
    replacement.write_bytes(b"second")
    replacement.replace(database)
    try:
        with pytest.raises(StableFailure, match="private_database_changed"):
            assert_same_inode(fd, database)
    finally:
        os.close(fd)

    target = tmp_path / "target.db"
    target.symlink_to(database)
    with pytest.raises(StableFailure, match="private_database_invalid"):
        backup_sqlite_read_only(target, tmp_path / "copy.db")


def test_orchestration_uses_primary_source_not_blocked_alternative() -> None:
    actions = (("000001", "sync fund 000001"),)
    calls = []
    readiness_count = 0

    def invoke(argv):
        nonlocal readiness_count
        calls.append(tuple(argv))
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            return CommandResult(
                1 if readiness_count == 1 else 0,
                _readiness(actions if readiness_count == 1 else (), ready=True),
            )
        if argv[1:3] == ["source", "status"]:
            return CommandResult(0, _source_status(argv[-1], alternative_state="unsupported"))
        return CommandResult(0, {"command": "sync.fund", "data": {}})

    result = orchestrate(CODES[:2], ROLES[:2], invoke)

    assert result.refresh_action_calls == 1
    assert result.action_state_counts == {"completed": 1}
    assert result.final_readiness_calls == 1


def test_usable_alternative_overrides_terminal_primary_and_action_runs_once() -> None:
    actions = (("000001", "sync fund 000001"),)
    readiness_count = 0
    action_calls = 0

    def invoke(argv):
        nonlocal action_calls, readiness_count
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            return CommandResult(
                1 if readiness_count == 1 else 0,
                _readiness(actions if readiness_count == 1 else (), ready=True),
            )
        if argv[1:3] == ["source", "status"]:
            return CommandResult(
                0,
                _source_status(
                    argv[-1],
                    resolution="usable",
                    primary_state="unavailable",
                    alternative_state="healthy",
                ),
            )
        action_calls += 1
        return CommandResult(0, {"command": "sync.fund", "data": {}})

    result = orchestrate(CODES[:2], ROLES[:2], invoke)

    assert action_calls == 1
    assert result.refresh_action_calls == 1
    assert result.action_state_counts == {"completed": 1}
    assert readiness_count == 2


@pytest.mark.parametrize("state", ("cooldown", "unavailable", "unsupported"))
def test_orchestration_stops_action_on_terminal_primary_source(state) -> None:
    calls = []
    readiness_count = 0

    def invoke(argv):
        nonlocal readiness_count
        calls.append(tuple(argv))
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            actions = (("000001", "sync fund 000001"),) if readiness_count == 1 else ()
            return CommandResult(1, _readiness(actions))
        if argv[1:3] == ["source", "status"]:
            return CommandResult(
                0,
                _source_status(
                    argv[-1],
                    resolution="partial",
                    primary_state=state,
                ),
            )
        raise AssertionError("stopped action must not execute")

    result = orchestrate(CODES[:2], ROLES[:2], invoke)

    assert result.refresh_action_calls == 0
    assert result.action_state_counts == {"stopped_by_source_state": 1}
    assert readiness_count == 2


def test_manual_supplement_resolution_stops_primary_action() -> None:
    readiness_count = 0

    def invoke(argv):
        nonlocal readiness_count
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            actions = (("000001", "sync fund 000001"),) if readiness_count == 1 else ()
            return CommandResult(1, _readiness(actions))
        if argv[1:3] == ["source", "status"]:
            return CommandResult(
                0,
                _source_status(argv[-1], resolution="manual_supplement_required"),
            )
        raise AssertionError("manual supplementation must stop the action")

    result = orchestrate(CODES[:2], ROLES[:2], invoke)
    assert result.refresh_action_calls == 0
    assert result.final_readiness_calls == 1


def test_documents_terminal_failure_skips_classify_but_keeps_independent_action() -> None:
    actions = (
        ("000001", "sync fund-profile 000001 --mode rapid"),
        ("000001", "sync fund-documents 000001"),
        ("000001", "fund classify 000001"),
        ("000002", "sync fund 000002"),
    )
    readiness_count = 0
    calls = []

    def invoke(argv):
        nonlocal readiness_count
        calls.append(tuple(argv))
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            return CommandResult(1, _readiness(actions if readiness_count == 1 else ()))
        if argv[1:3] == ["source", "status"]:
            return CommandResult(0, _source_status(argv[-1]))
        if argv[1:3] == ["sync", "fund-documents"]:
            return CommandResult(1, None)
        command = {
            ("sync", "fund-profile"): "sync.fund-profile",
            ("sync", "fund"): "sync.fund",
        }[tuple(argv[1:3])]
        return CommandResult(0, {"command": command, "data": {}})

    result = orchestrate(("000001", "000002"), ROLES[:2], invoke)

    assert not any(call[1:3] == ("fund", "classify") for call in calls)
    assert any(call[1:3] == ("sync", "fund") and call[-1] == "000002" for call in calls)
    assert result.action_state_counts == {
        "completed": 2,
        "dependency_stopped": 1,
        "terminal_failure": 1,
    }
    assert [
        (record.role, record.action_type, record.state)
        for record in result.action_terminal_records
    ] == [
        (ROLES[0], "sync_fund_profile", "completed"),
        (ROLES[0], "sync_fund_documents", "terminal_failure"),
        (ROLES[0], "fund_classify", "dependency_stopped"),
        (ROLES[1], "sync_fund", "completed"),
    ]
    assert not any(code in repr(result.action_terminal_records) for code in CODES)
    assert readiness_count == 2


def test_duplicate_or_force_action_fails_but_final_readiness_runs_once() -> None:
    for actions in (
        (("000001", "sync fund 000001"), ("000001", "sync fund 000001")),
        (("000001", "sync fund 000001 --force"),),
    ):
        readiness_count = 0

        def invoke(argv):
            nonlocal readiness_count
            if argv[1:3] == ["fund", "shortlist-readiness"]:
                readiness_count += 1
                return CommandResult(1, _readiness(actions if readiness_count == 1 else ()))
            if argv[1:3] == ["source", "status"]:
                return CommandResult(0, _source_status(argv[-1]))
            raise AssertionError("invalid action must not execute")

        with pytest.raises(StableFailure, match="engineering_orchestration_invalid"):
            orchestrate(CODES[:2], ROLES[:2], invoke)
        assert readiness_count == 2


def test_orchestration_rejects_single_code_before_any_call() -> None:
    calls = []
    with pytest.raises(StableFailure, match="engineering_orchestration_invalid"):
        orchestrate(("000001",), ("engineering_subject_1",), calls.append)
    assert calls == []


def test_invalid_final_readiness_is_a_technical_failure() -> None:
    readiness_count = 0

    def invoke(argv):
        nonlocal readiness_count
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            if readiness_count == 1:
                return CommandResult(1, _readiness())
            return CommandResult(1, None)
        return CommandResult(0, _source_status(argv[-1]))

    with pytest.raises(StableFailure, match="engineering_orchestration_invalid"):
        orchestrate(CODES[:2], ROLES[:2], invoke)
    assert readiness_count == 2


@pytest.mark.parametrize("interrupt", (KeyboardInterrupt, SystemExit))
def test_process_control_interrupt_skips_final_readiness(interrupt) -> None:
    readiness_count = 0

    def invoke(argv):
        nonlocal readiness_count
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            return CommandResult(1, _readiness())
        raise interrupt()

    with pytest.raises(interrupt):
        orchestrate(CODES[:2], ROLES[:2], invoke)
    assert readiness_count == 1


def test_public_maximum_is_five_status_and_twenty_five_ordered_actions() -> None:
    codes = tuple(f"{index:06d}" for index in range(1, 6))
    roles = tuple(f"subject_{index}" for index in range(1, 6))
    templates = (
        "sync fund {code}",
        "sync fund-profile {code} --mode rapid",
        "sync fund-holdings {code} --mode rapid",
        "sync fund-documents {code}",
        "fund classify {code}",
    )
    actions = tuple((code, template.format(code=code)) for code in codes for template in templates)
    readiness_count = 0
    calls = []

    def invoke(argv):
        nonlocal readiness_count
        calls.append(tuple(argv))
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_count += 1
            return CommandResult(1, _readiness(actions if readiness_count == 1 else ()))
        if argv[1:3] == ["source", "status"]:
            return CommandResult(0, _source_status(argv[-1]))
        command = ".".join(argv[1:3])
        return CommandResult(0, {"command": command, "data": {}})

    result = orchestrate(codes, roles, invoke)

    assert result.source_status_calls == 5
    assert result.refresh_action_calls == 25
    assert result.initial_readiness_calls == result.final_readiness_calls == 1
    non_readiness = [call for call in calls if call[1:3] != ("fund", "shortlist-readiness")]
    assert len(set(non_readiness)) == len(non_readiness)
    assert sum(call[1:3] == ("fund", "shortlist-readiness") for call in calls) == 2


def _orchestration_result(
    *,
    actions=(),
    final_ready=False,
    final_evidence=None,
    states=None,
    outcome="not_run",
    refresh_calls=0,
):
    initial_data = _readiness(actions=actions)["data"]
    planned = acceptance._parse_actions(initial_data, CODES, ROLES)
    remaining_states = dict({} if states is None else states)
    records = []
    for action in planned:
        state = next(
            (name for name, count in remaining_states.items() if count > 0),
            None,
        )
        if state is None:
            break
        remaining_states[state] -= 1
        records.append(
            acceptance.ActionTerminalRecord(
                role=action.role,
                action_type=action.action_type,
                state=state,
            )
        )
    return acceptance.OrchestrationResult(
        action_state_counts={} if states is None else states,
        action_terminal_records=tuple(records),
        final_data=_readiness(
            ready=final_ready,
            evidence=final_evidence,
        )["data"],
        final_readiness_calls=1,
        initial_data=initial_data,
        initial_readiness_calls=1,
        outcome=outcome,
        refresh_action_calls=refresh_calls,
        source_status_calls=4,
    )


def _candidate_evidence(*, usable=False, held=True, binding_failure=False):
    source = (
        {
            "evaluated_at": "2026-07-19T08:00:00+00:00",
            "fields": [
                {
                    "acceptable_sources": [],
                    "field_id": field_id,
                    "resolution": "usable",
                }
                for field_id in (
                    "adjusted_return_series",
                    "current_manager_team",
                    "fees_share_class_relationship",
                    "formal_nav",
                    "holdings_industries",
                    "identity_active_status",
                )
            ],
        }
        if usable
        else {"technical_failure": "source_status_load_failed"}
    )
    profile = (
        {
            "authenticated": True,
            "benchmarks": {},
            "conflicts": [],
            "fees": {},
            "freshness": {},
            "identity": {},
            "managers": {},
            "missing_sections": {},
            "publication_dates": [],
            "report_dates": [],
            "warnings": [],
        }
        if usable
        else {"technical_failure": "profile_load_failed"}
    )
    holdings = (
        {
            "conflicts": [],
            "disclosed_coverage": "1",
            "disclosure_scopes": ["top_ten"],
            "evidence_level": "verified_fact",
            "freshness": "current",
            "published_at": "2026-07-19T08:00:00+00:00",
            "report_period": "2026-06-30",
            "source_document_ids": [1],
            "warnings": [],
        }
        if usable
        else {"technical_failure": "holdings_load_failed"}
    )
    d1 = (
        {
            "classification_policy_checksum": "a" * 64,
            "classification_present": True,
            "classified_at": "2026-07-19T08:00:00+00:00",
            "conflicts": [],
            "evidence_status": "verified",
            "freshness": "current",
            "mapped_asset_layer": "diversified_equity",
            "mapping_reason_code": "verified_diversified_equity",
            "missing_evidence": [],
            "policy_version": "1",
            "portfolio_role": "core_eligible",
            "reason_codes": [],
            "risk_bucket": "diversified_equity",
            "valid_until": "2026-08-19T08:00:00+00:00",
        }
        if usable
        else {
            "classification_present": False,
            "technical_failure": "classification_load_failed",
        }
    )
    return {
        "source_health": source,
        "profile": profile,
        "formal_nav": {
            "end_date": "2026-07-18" if usable else None,
            "future_observation_count": 0,
            "latest_date": "2026-07-18" if usable else None,
            "observation_count": 2 if usable else 0,
            "start_date": "2026-07-17" if usable else None,
            "technical_failure": None if usable else "formal_nav_load_failed",
            "unique_date_count": 2 if usable else 0,
            "usable": usable,
        },
        "holdings": holdings,
        "d1": d1,
        "portfolio_binding": {
            "position_state": "held" if held else "not_held",
            "technical_failure": (
                "portfolio_binding_load_failed" if binding_failure else None
            ),
        },
        "shortlist_entry": {
            "d1_conflict_free": usable,
            "d1_current": usable,
            "d1_evidence_verified": usable,
            "mapped_asset_layer": "diversified_equity" if usable else None,
            "personal_gate_passes": False,
            "portfolio_role_eligible": usable,
            "position_state": "held" if held else "not_held",
        },
    }


def _closed_evidence(*, usable_first=False, binding_failure=False):
    return [
        _candidate_evidence(
            usable=usable_first and index == 0,
            held=index == 0,
            binding_failure=binding_failure and index == 0,
        )
        for index in range(len(CODES))
    ]


def _shortlist(*, pair_overrides=None):
    pairs = [
        {
            "left_fund_code": left,
            "right_fund_code": right,
            "state": "insufficient_data",
            "reason_code": "missing_identity",
            "warning_codes": [],
        }
        for left, right in combinations(CODES, 2)
    ]
    for index, override in (pair_overrides or {}).items():
        pairs[index] = {**pairs[index], **override}
    return {
        "request": {
            "candidate_codes": list(CODES),
            "candidate_count": len(CODES),
        },
        "candidate_reviews": [
            {"fund_code": code, "position_state": "held" if index == 0 else "not_held"}
            for index, code in enumerate(CODES)
        ],
        "comparability": pairs,
    }


def test_engineering_flow_accepts_already_ready_without_actions() -> None:
    result = _orchestration_result(final_ready=True, outcome="completed_once")

    assert validate_engineering_flow(result, expected_subject_count=4) == {
        "engineering_flow": "pass"
    }


@pytest.mark.parametrize(
    ("states", "final_ready", "outcome", "refresh_calls"),
    (
        ({"completed": 1}, True, "completed_once", 1),
        ({"completed": 1}, False, "partial_once", 1),
        ({"stopped_by_source_state": 1}, False, "stopped_by_source_state", 0),
    ),
)
def test_engineering_flow_accepts_each_bounded_terminal_state(
    states, final_ready, outcome, refresh_calls
) -> None:
    result = _orchestration_result(
        actions=(("000001", "sync fund 000001"),),
        final_ready=final_ready,
        states=states,
        outcome=outcome,
        refresh_calls=refresh_calls,
    )

    assert validate_engineering_flow(result, expected_subject_count=4) == {
        "engineering_flow": "pass"
    }


@pytest.mark.parametrize(
    "changes",
    (
        {"action_state_counts": {"completed": 2}},
        {"action_state_counts": {"retried": 1}},
        {"source_status_calls": 3},
        {"final_readiness_calls": 2},
        {"refresh_action_calls": 2},
        {"outcome": "completed_once"},
    ),
)
def test_engineering_flow_fails_closed_on_invalid_counts_or_outcome(changes) -> None:
    result = _orchestration_result(
        actions=(("000001", "sync fund 000001"),),
        states={"completed": 1},
        outcome="partial_once",
        refresh_calls=1,
    )
    invalid = acceptance.OrchestrationResult(**{**vars(result), **changes})

    with pytest.raises(StableFailure, match="engineering_flow_invalid"):
        validate_engineering_flow(invalid, expected_subject_count=4)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_request",
        "final_code_order",
        "candidate_code_order",
        "nonsense_action",
        "action_code_mismatch",
        "duplicate_gap",
        "unsorted_gap",
        "invalid_gap",
    ),
)
def test_engineering_flow_closes_requests_actions_and_gaps(mutation) -> None:
    result = _orchestration_result(
        actions=(("000001", "sync fund 000001"),),
        states={"completed": 1},
        outcome="partial_once",
        refresh_calls=1,
    )
    initial = json.loads(json.dumps(result.initial_data))
    final = json.loads(json.dumps(result.final_data))
    if mutation == "missing_request":
        initial.pop("request")
    elif mutation == "final_code_order":
        final["request"]["candidate_codes"].reverse()
    elif mutation == "candidate_code_order":
        final["candidate_evidence"][0]["fund_code"] = "999999"
    elif mutation == "nonsense_action":
        initial["bounded_refresh_actions"][0]["command"] = "run arbitrary command"
    elif mutation == "action_code_mismatch":
        initial["bounded_refresh_actions"][0]["command"] = "sync fund 999999"
    elif mutation == "duplicate_gap":
        final["blocking_codes"] = ["missing_identity", "missing_identity"]
    elif mutation == "unsorted_gap":
        final["blocking_codes"] = ["profile_missing", "d1_missing"]
    else:
        final["blocking_codes"] = ["unsafe-code"]
    invalid = acceptance.OrchestrationResult(
        **{
            **vars(result),
            "initial_data": initial,
            "final_data": final,
        }
    )

    with pytest.raises(StableFailure, match="engineering_flow_invalid"):
        validate_engineering_flow(invalid, expected_subject_count=4)


def test_engineering_flow_rejects_dependency_stop_on_independent_action() -> None:
    result = _orchestration_result(
        actions=(("000001", "sync fund 000001"),),
        states={"dependency_stopped": 1},
        outcome="not_run",
        refresh_calls=0,
    )

    with pytest.raises(StableFailure, match="engineering_flow_invalid"):
        validate_engineering_flow(result, expected_subject_count=4)


def test_source_failure_stops_documents_and_classify_before_dependency_logic() -> None:
    actions = (
        ("000001", "sync fund-documents 000001"),
        ("000001", "fund classify 000001"),
    )
    readiness_calls = 0

    def invoke(argv):
        nonlocal readiness_calls
        if argv[1:3] == ["fund", "shortlist-readiness"]:
            readiness_calls += 1
            return CommandResult(
                1,
                _readiness(
                    actions=actions if readiness_calls == 1 else (),
                    codes=CODES[:2],
                ),
            )
        if argv[1:3] == ["source", "status"]:
            return CommandResult(1, None)
        raise AssertionError("source-stopped actions must not execute")

    result = orchestrate(CODES[:2], ROLES[:2], invoke)

    assert [record.state for record in result.action_terminal_records] == [
        "stopped_by_source_state",
        "stopped_by_source_state",
    ]
    assert validate_engineering_flow(result, expected_subject_count=2) == {
        "engineering_flow": "pass"
    }


def test_engineering_flow_rejects_classify_source_stop_after_documents_complete() -> None:
    result = _orchestration_result(
        actions=(
            ("000001", "sync fund-documents 000001"),
            ("000001", "fund classify 000001"),
        ),
        states={"completed": 1, "stopped_by_source_state": 1},
        outcome="stopped_by_source_state",
        refresh_calls=1,
    )

    with pytest.raises(StableFailure, match="engineering_flow_invalid"):
        validate_engineering_flow(result, expected_subject_count=4)


def test_engineering_evidence_rejects_ready_without_closed_candidates() -> None:
    result = _orchestration_result(final_ready=True, outcome="completed_once")
    result.final_data["candidate_evidence"] = []

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(result, _shortlist())


@pytest.mark.parametrize(
    "mutation",
    ("single_pair", "duplicate_pair", "foreign_pair_code", "review_code_order"),
)
def test_engineering_evidence_requires_closed_shortlist_pairs(mutation) -> None:
    shortlist = _shortlist()
    if mutation == "single_pair":
        shortlist["comparability"] = shortlist["comparability"][:1]
    elif mutation == "duplicate_pair":
        shortlist["comparability"][1] = dict(shortlist["comparability"][0])
    elif mutation == "foreign_pair_code":
        shortlist["comparability"][0]["right_fund_code"] = "999999"
    else:
        shortlist["candidate_reviews"][0]["fund_code"] = "999999"

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=_closed_evidence()),
            shortlist,
        )


def test_engineering_evidence_rejects_forged_usable_source_shape() -> None:
    evidence = _closed_evidence()
    evidence[0]["source_health"] = {
        "evaluated_at": "2026-07-19T08:00:00+00:00",
        "fields": [{"resolution": "usable"}],
    }

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=evidence),
            _shortlist(),
        )


def test_engineering_evidence_rejects_inconsistent_usable_nav() -> None:
    evidence = _closed_evidence(usable_first=True)
    evidence[0]["formal_nav"] = {
        "end_date": "not-a-date",
        "future_observation_count": 0,
        "latest_date": "not-a-date",
        "observation_count": 0,
        "start_date": "not-a-date",
        "technical_failure": "formal_nav_load_failed",
        "unique_date_count": 0,
        "usable": True,
    }

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=evidence),
            _shortlist(),
        )


def test_engineering_evidence_rejects_zero_length_usable_nav_interval() -> None:
    evidence = _closed_evidence(usable_first=True)
    evidence[0]["formal_nav"].update(
        {
            "start_date": "2026-07-18",
            "end_date": "2026-07-18",
            "latest_date": "2026-07-18",
        }
    )

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=evidence),
            _shortlist(),
        )


def test_engineering_evidence_closes_position_state_across_projections() -> None:
    shortlist = _shortlist()
    shortlist["candidate_reviews"][0]["position_state"] = "not_held"

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=_closed_evidence()),
            shortlist,
        )


@pytest.mark.parametrize(
    ("component", "field", "operation"),
    (
        ("profile", "authenticated", "remove"),
        ("formal_nav", "unexpected", "add"),
        ("holdings", "warnings", "remove"),
        ("d1", "unexpected", "add"),
        ("portfolio_binding", "technical_failure", "remove"),
        ("shortlist_entry", "unexpected", "add"),
    ),
)
def test_engineering_evidence_rejects_nonexact_component_shapes(
    component, field, operation
) -> None:
    evidence = _closed_evidence(usable_first=True)
    target = evidence[0][component]
    if operation == "remove":
        target.pop(field)
    else:
        target[field] = "forged"

    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=evidence),
            _shortlist(),
        )


def test_engineering_projection_accepts_current_public_readiness_shape(
    tmp_path, monkeypatch
) -> None:
    from kunjin.selection.readiness import public_shortlist_readiness_payload
    from tests.unit.test_selection_readiness import (
        CODES as READINESS_CODES,
    )
    from tests.unit.test_selection_readiness import (
        _build_service,
    )

    service, _repository, _calls = _build_service(tmp_path, monkeypatch)
    payload = public_shortlist_readiness_payload(service.review(READINESS_CODES))
    result = acceptance.OrchestrationResult(
        action_state_counts={},
        action_terminal_records=(),
        final_data=payload,
        final_readiness_calls=1,
        initial_data=payload,
        initial_readiness_calls=1,
        outcome="completed_once",
        refresh_action_calls=0,
        source_status_calls=len(READINESS_CODES),
    )
    shortlist = {
        "request": payload["request"],
        "candidate_reviews": [
            {
                "fund_code": code,
                "position_state": evidence["portfolio_binding"]["position_state"],
            }
            for code, evidence in zip(
                READINESS_CODES,
                payload["candidate_evidence"],
            )
        ],
        "comparability": [
            {
                "left_fund_code": READINESS_CODES[0],
                "right_fund_code": READINESS_CODES[1],
                "state": "comparable",
                "reason_code": "classification_match",
                "warning_codes": [],
            }
        ],
    }

    assert validate_engineering_flow(
        result,
        expected_subject_count=len(READINESS_CODES),
    ) == {"engineering_flow": "pass"}
    projection = project_engineering_evidence(result, shortlist)
    assert projection["evidence_readiness"] == "ready"
    assert projection["structural_comparability"] == "observed"


def test_partial_real_evidence_is_not_promoted_to_structural_observation() -> None:
    result = _orchestration_result(
        actions=(("000001", "sync fund 000001"),),
        final_evidence=_closed_evidence(usable_first=True),
        states={"completed": 1},
        outcome="partial_once",
        refresh_calls=1,
    )

    projection = project_engineering_evidence(
        result,
        _shortlist(),
    )

    assert projection == {
        "evidence_readiness": "partial",
        "comparison_evidence_readiness": "insufficient_data",
        "structural_comparability": "not_testable",
        "usable_component_count": 14,
        "held_binding_observed": True,
        "comparison_state_counts": {
            "comparable": 0,
            "not_comparable": 0,
            "insufficient_data": 6,
        },
        "comparison_reason_codes": ["missing_identity"],
    }


def test_inactive_fund_pair_is_valid_but_not_structurally_observed() -> None:
    projection = project_engineering_evidence(
        _orchestration_result(final_evidence=_closed_evidence(usable_first=True)),
        _shortlist(
            pair_overrides={
                0: {
                    "state": "not_comparable",
                    "reason_code": "inactive_fund",
                }
            }
        ),
    )

    assert projection["comparison_state_counts"] == {
        "comparable": 0,
        "not_comparable": 1,
        "insufficient_data": 5,
    }
    assert projection["comparison_reason_codes"] == [
        "inactive_fund",
        "missing_identity",
    ]
    assert projection["structural_comparability"] == "not_testable"


def test_engineering_evidence_projects_ready_and_zero_component_states() -> None:
    ready = project_engineering_evidence(
        _orchestration_result(
            final_ready=True,
            final_evidence=_closed_evidence(usable_first=True),
            outcome="completed_once",
        ),
        _shortlist(
            pair_overrides={
                0: {"state": "comparable", "reason_code": "classification_match"}
            }
        ),
    )
    missing = project_engineering_evidence(
        _orchestration_result(
            final_evidence=[
                _candidate_evidence(
                    held=index == 0,
                    binding_failure=True,
                )
                for index in range(len(CODES))
            ]
        ),
        _shortlist(),
    )

    assert ready["evidence_readiness"] == "ready"
    assert ready["comparison_evidence_readiness"] == "ready"
    assert ready["structural_comparability"] == "observed"
    assert missing["evidence_readiness"] == "insufficient_data"
    assert missing["comparison_evidence_readiness"] == "insufficient_data"
    assert missing["structural_comparability"] == "not_testable"
    assert missing["usable_component_count"] == 0


@pytest.mark.parametrize(
    "pair",
    (
        {"state": "comparable", "reason_code": "missing_identity"},
        {"state": "not_comparable", "reason_code": "missing_identity"},
        {"state": "insufficient_data", "reason_code": "classification_match"},
        {"state": "invalid", "reason_code": "classification_match"},
        {"state": "insufficient_data", "reason_code": "unsafe-code"},
    ),
)
def test_engineering_evidence_rejects_malformed_pair_state(pair) -> None:
    shortlist = _shortlist()
    shortlist["comparability"][0] = {
        **shortlist["comparability"][0],
        **pair,
    }
    with pytest.raises(StableFailure, match="engineering_evidence_invalid"):
        project_engineering_evidence(
            _orchestration_result(final_evidence=_closed_evidence()),
            shortlist,
        )


def _valid_scope():
    return {
        "candidate_formation": {
            "status": "research_scope_only",
            "candidate_code_discovery": "not_implemented",
        },
        "action_boundary": dict(ACTION_BOUNDARY),
    }


def _confirmed_profile_status():
    return {
        "state": "confirmed",
        "version": 1,
        "confirmed_at": "2026-07-19T08:00:00+00:00",
        "valid_until": "2026-10-19T08:00:00+00:00",
        "freshness": "fresh",
    }


def _missing_gate_status():
    return {"state": "missing", "freshness": "missing", "capability": "research_only"}


def _minimal_stale_gate_status():
    return {"state": "stale", "freshness": "stale", "capability": "research_only"}


def _fresh_suitability_status():
    return {
        "state": "fresh",
        "freshness": "fresh",
        "assessment_id": 1,
        "profile_version_id": 1,
        "policy_version": "1",
        "status": "blocked",
        "hard_blocks": ["emergency_reserve_shortfall"],
        "constraints": ["monthly_ceiling_constrained"],
        "assessed_at": "2026-07-19T08:00:00+00:00",
        "valid_until": "2026-08-19T08:00:00+00:00",
        "capability": "research_only",
    }


def _fresh_allocation_status():
    return {
        "state": "fresh",
        "freshness": "fresh",
        "assessment_id": 1,
        "profile_version_id": 1,
        "suitability_assessment_id": 1,
        "policy_version": "1",
        "status": "range_available",
        "binding_constraints": ["horizon_binding"],
        "safe_summary": {},
        "permitted_region": {},
        "assessed_at": "2026-07-19T08:00:00+00:00",
        "valid_until": "2026-08-19T08:00:00+00:00",
        "capability": "research_only",
    }


def test_owner_status_validation_accepts_exact_fresh_and_missing_shapes() -> None:
    fresh = validate_owner_statuses(
        _confirmed_profile_status(),
        _fresh_suitability_status(),
        _missing_gate_status(),
        _valid_scope(),
    )
    assert fresh["phase_b"]["status"] == "blocked"
    assert fresh["phase_c"]["status"] is None

    missing = validate_owner_statuses(
        {"state": "missing", "freshness": "missing"},
        _missing_gate_status(),
        _missing_gate_status(),
        _valid_scope(),
    )
    assert missing["phase_a"]["state"] == "missing"


def test_owner_status_validation_accepts_production_suitability_stale_shapes() -> None:
    minimal = validate_owner_statuses(
        _confirmed_profile_status(),
        _minimal_stale_gate_status(),
        _missing_gate_status(),
        _valid_scope(),
    )
    assert minimal["phase_b"] == {
        "state": "stale",
        "freshness": "stale",
        "status": None,
        "blocking_codes": [],
        "constraint_codes": [],
    }

    full_status = {**_fresh_suitability_status(), "state": "stale", "freshness": "stale"}
    full = validate_owner_statuses(
        _confirmed_profile_status(),
        full_status,
        _missing_gate_status(),
        _valid_scope(),
    )
    assert full["phase_b"]["status"] == "blocked"


@pytest.mark.parametrize(
    "invalid",
    (
        {**_minimal_stale_gate_status(), "capability": "actionable"},
        {**_minimal_stale_gate_status(), "status": None},
        {"state": "stale", "freshness": "missing", "capability": "research_only"},
    ),
)
def test_owner_status_validation_rejects_malformed_minimal_suitability_stale(
    invalid,
) -> None:
    with pytest.raises(StableFailure, match="owner_status_invalid"):
        validate_owner_statuses(
            _confirmed_profile_status(),
            invalid,
            _missing_gate_status(),
            _valid_scope(),
        )


@pytest.mark.parametrize(
    "phase_a,phase_b,phase_c",
    (
        (
            {**_confirmed_profile_status(), "freshness": None},
            _missing_gate_status(),
            _missing_gate_status(),
        ),
        (
            {**_confirmed_profile_status(), "state": "active"},
            _missing_gate_status(),
            _missing_gate_status(),
        ),
        (
            _confirmed_profile_status(),
            {**_fresh_suitability_status(), "status": None},
            _missing_gate_status(),
        ),
        (
            _confirmed_profile_status(),
            _missing_gate_status(),
            {**_fresh_allocation_status(), "status": "unknown"},
        ),
    ),
)
def test_owner_status_schema_drift_is_technical_failure(phase_a, phase_b, phase_c) -> None:
    with pytest.raises(StableFailure, match="owner_status_invalid"):
        validate_owner_statuses(phase_a, phase_b, phase_c, _valid_scope())


def test_keychain_is_loaded_exactly_once_and_reused_in_memory() -> None:
    expected = bytes(range(32))
    encoded = base64.urlsafe_b64encode(expected).decode("ascii") + "\n"
    calls = []

    def child(command):
        calls.append(command)
        return 0, encoded, ""

    key = load_owner_key_once(child)
    store = InMemoryKeyStore(key)

    assert calls == [[
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "com.kunjin.profile-encryption",
        "-a",
        "v1",
        "-w",
    ]]
    assert store.load_existing_key() == expected
    assert store.load_existing_key() == expected
    with pytest.raises(StableFailure, match="owner_keychain_write_prohibited"):
        store.load_or_create_key()


def test_keychain_failure_is_stable_and_never_exposes_stderr() -> None:
    def child(_command):
        return 44, "", "/private/tmp/secret-key"

    with pytest.raises(StableFailure) as error:
        load_owner_key_once(child)
    assert error.value.code == "owner_keychain_unavailable"
    assert "/private" not in str(error.value)


def test_keychain_rejects_noncanonical_base64() -> None:
    noncanonical = base64.b64encode(b"\xfb" * 32).decode("ascii")

    with pytest.raises(StableFailure, match="owner_keychain_unavailable"):
        load_owner_key_once(lambda _command: (0, noncanonical, ""))


def test_owner_runtime_guards_block_network_process_and_os_spawn() -> None:
    with OwnerRuntimeGuards():
        for operation in (
            lambda: socket.getaddrinfo("example.com", 443),
            lambda: socket.create_connection(("example.com", 443)),
            lambda: subprocess.run(["/usr/bin/true"], check=False),
            lambda: os.system("true"),
            lambda: os.execv("/usr/bin/true", ["true"]),
        ):
            with pytest.raises(StableFailure):
                operation()


class _FakePrivateContext:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _FakeChildren(_FakePrivateContext):
    def __init__(self) -> None:
        self._children = []

    def run_exact(self, command):
        self._children.append(object())
        encoded = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
        return 0, encoded, ""

    def assert_waited(self):
        return None

    def install_tracking(self):
        return None

    def restore_tracking(self):
        return None


def test_owner_controller_uses_one_key_one_context_and_four_safe_calls(
    tmp_path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    source = tmp_path / "owner.db"
    source.write_bytes(b"encrypted")
    source.chmod(0o600)
    context = object()
    calls = []
    builds = []
    children = _FakeChildren()
    monkeypatch.setenv(
        "KUNJIN_PHASE41_OWNER_APPROVED", "explicit_private_keychain_read_only"
    )
    monkeypatch.delenv("KUNJIN_DATA_DIR", raising=False)
    monkeypatch.delenv("KUNJIN_STATE_DIR", raising=False)
    monkeypatch.setattr(acceptance, "_runtime_dir", lambda: runtime)
    monkeypatch.setattr(acceptance, "_canonical_owner_database", lambda: source)
    monkeypatch.setattr(acceptance, "TrackedChildren", lambda: children)
    monkeypatch.setattr(acceptance, "ReadOnlyDatabaseGuard", lambda *_args: _FakePrivateContext())

    def build(key):
        builds.append(key)
        return object(), context

    monkeypatch.setattr(acceptance, "_build_context_with_key", build)

    def safe_call(_cli, used_context, argv, expected):
        calls.append((used_context, tuple(argv), expected))
        if expected == "profile.status":
            return _confirmed_profile_status()
        if expected == "suitability.status":
            return _fresh_suitability_status()
        if expected == "allocation.status":
            return _missing_gate_status()
        return _valid_scope()

    monkeypatch.setattr(acceptance, "_safe_cli_call", safe_call)
    monkeypatch.setattr(acceptance, "check_runtime_permissions", lambda _runtime: None)

    summary = acceptance.run_owner_acceptance()

    assert len(builds) == 1
    assert len(children._children) == 1
    assert len(calls) == 4
    assert all(item[0] is context for item in calls)
    assert summary["single_context"] is True
    assert summary["single_keychain_child"] is True
    assert "KUNJIN_DATA_DIR" not in os.environ
    assert "KUNJIN_STATE_DIR" not in os.environ


def test_engineering_controller_separates_flow_and_evidence_without_codes(
    tmp_path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    subject = tmp_path / "subjects.json"
    subject.write_text("private", encoding="ascii")
    source = Path.home() / ".local" / "share" / "kunjin" / "kunjin.db"
    context = object()
    children = _FakeChildren()
    monkeypatch.setenv("KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE", str(subject))
    monkeypatch.delenv("KUNJIN_DATA_DIR", raising=False)
    monkeypatch.delenv("KUNJIN_STATE_DIR", raising=False)
    monkeypatch.setattr(acceptance, "_runtime_dir", lambda: runtime)
    monkeypatch.setattr(acceptance, "secure_read_subject_file", lambda *_args: CODES)
    monkeypatch.setattr(acceptance, "TrackedChildren", lambda: children)
    monkeypatch.setattr(acceptance, "ReadOnlyDatabaseGuard", lambda *_args: _FakePrivateContext())
    monkeypatch.setattr(acceptance, "_build_engineering_context", lambda: (object(), context))
    monkeypatch.setattr(
        acceptance,
        "load_owner_key_once",
        lambda *_args: pytest.fail("engineering must not read Keychain"),
    )
    initial = _readiness(
        actions=(("000001", "sync fund 000001"),),
        evidence=_closed_evidence(),
        blocking=["profile_stale"],
    )["data"]
    final = _readiness(
        ready=False,
        evidence=[
            _candidate_evidence(
                held=index == 0,
                binding_failure=True,
            )
            for index in range(len(CODES))
        ],
        blocking=["formal_nav_missing"],
    )["data"]
    orchestration = acceptance.OrchestrationResult(
        action_state_counts={"terminal_failure": 1},
        action_terminal_records=(
            acceptance.ActionTerminalRecord(
                role=ROLES[0],
                action_type="sync_fund",
                state="terminal_failure",
            ),
        ),
        final_data=final,
        final_readiness_calls=1,
        initial_data=initial,
        initial_readiness_calls=1,
        outcome="partial_once",
        refresh_action_calls=1,
        source_status_calls=4,
    )
    monkeypatch.setattr(acceptance, "orchestrate", lambda *_args: orchestration)
    shortlist_data = _shortlist(
        pair_overrides={
            0: {
                "state": "not_comparable",
                "reason_code": "management_style_mismatch",
            }
        }
    )
    shortlist = {"command": "fund.shortlist", "data": shortlist_data}
    monkeypatch.setattr(
        acceptance,
        "_engineering_cli_result",
        lambda *_args: CommandResult(1, shortlist),
    )
    monkeypatch.setattr(acceptance, "check_runtime_permissions", lambda _runtime: None)

    summary = acceptance.run_engineering_acceptance()
    encoded = json.dumps(summary, sort_keys=True)

    assert summary["engineering_flow"] == "pass"
    assert summary["evidence_readiness"] == "insufficient_data"
    assert summary["comparison_evidence_readiness"] == "insufficient_data"
    assert summary["structural_comparability"] == "observed"
    assert summary["held_binding_observed"] is True
    assert summary["usable_component_count"] == 0
    assert summary["gap_categories"] == ["formal_nav_missing"]
    assert "coverage" not in summary
    assert not any(code in encoded for code in CODES)
    assert str(subject) not in encoded
    assert source.as_posix() not in encoded
    assert children._children == []


def test_sanitized_output_rejects_codes_paths_tracebacks_and_authorization() -> None:
    safe = json.dumps(
        {
            "mode": "owner",
            "owner_candidate_state": "owner_candidates_unavailable",
            "financial_usability": "not_yet_testable",
            "action_boundary": dict(ACTION_BOUNDARY),
        },
        sort_keys=True,
    )
    assert sanitize_output(safe, private_paths=(Path("/private/tmp/runtime"),)) == safe

    for leaked in (
        safe + " 000001",
        safe + " /private/tmp/runtime",
        safe + " Traceback (most recent call last)",
        safe.replace('"action_authorized": false', '"action_authorized": true'),
    ):
        with pytest.raises(StableFailure, match="acceptance_output_invalid"):
            sanitize_output(leaked, private_paths=(Path("/private/tmp/runtime"),))


def test_private_emit_never_prints_plaintext_or_base64_secret(
    tmp_path, monkeypatch, capsys
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    capture = runtime / "captured.out"
    secret = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
    monkeypatch.setenv("KUNJIN_PHASE41_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("KUNJIN_PHASE41_CAPTURE_FILE", str(capture))

    for leaked in (
        secret,
        json.dumps({"monthly_net_income": "private"}),
        "plain private profile value",
    ):
        capture.write_text(leaked, encoding="utf-8")
        capture.chmod(0o600)
        assert acceptance.main(["emit-private"]) == 1
        emitted = capsys.readouterr()
        assert emitted.err == ""
        assert leaked not in emitted.out
        assert "acceptance_output_invalid" in emitted.out


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_shell_signal_kills_ignoring_process_group(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    venv = repository / ".venv" / "bin"
    scripts.mkdir(parents=True)
    venv.mkdir(parents=True)
    wrapper = scripts / "run_phase41_acceptance.sh"
    helper = scripts / "phase41_acceptance.py"
    shutil.copy2(root / "scripts/run_phase41_acceptance.sh", wrapper)
    shutil.copy2(root / "scripts/phase41_acceptance.py", helper)
    wrapper.chmod(0o755)
    marker = tmp_path / "children.txt"
    fake_python = venv / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "trap '' TERM INT HUP\n"
        "printf '%s\\n' \"$$\" > \"${PHASE41_SIGNAL_MARKER}\"\n"
        "/bin/bash -c 'trap \"\" TERM INT HUP; printf \"%s\\n\" \"$$\" >> "
        "\"${PHASE41_SIGNAL_MARKER}\"; while :; do /bin/sleep 1; done' &\n"
        "while :; do /bin/sleep 1; done\n",
        encoding="ascii",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("PHASE41_SIGNAL_MARKER", str(marker))
    process = subprocess.Popen(
        [str(wrapper), "fault"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    process.send_signal(signal.SIGTERM)
    process.communicate(timeout=5)
    assert process.returncode == 130
    child_pids = [int(value) for value in marker.read_text().splitlines()]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(not _pid_exists(pid) for pid in child_pids):
            break
        time.sleep(0.02)
    assert all(not _pid_exists(pid) for pid in child_pids)


def test_shell_kills_descendant_when_group_leader_exits_first(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    venv = repository / ".venv" / "bin"
    scripts.mkdir(parents=True)
    venv.mkdir(parents=True)
    wrapper = scripts / "run_phase41_acceptance.sh"
    helper = scripts / "phase41_acceptance.py"
    shutil.copy2(root / "scripts/run_phase41_acceptance.sh", wrapper)
    shutil.copy2(root / "scripts/phase41_acceptance.py", helper)
    wrapper.chmod(0o755)
    marker = tmp_path / "descendant.txt"
    fake_python = venv / "python"
    fake_python.write_text(
        "#!/bin/bash\n"
        "/bin/bash -c 'trap \"\" TERM INT HUP; printf \"%s\\n\" \"$$\" > "
        "\"${PHASE41_DESCENDANT_MARKER}\"; while :; do /bin/sleep 1; done' &\n"
        "while [[ ! -s \"${PHASE41_DESCENDANT_MARKER}\" ]]; do :; done\n"
        "exit 0\n",
        encoding="ascii",
    )
    fake_python.chmod(0o755)
    monkeypatch.setenv("PHASE41_DESCENDANT_MARKER", str(marker))

    completed = subprocess.run(
        [str(wrapper), "fault"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode == 70
    assert b"phase41_descendant_residue" in completed.stderr
    descendant_pid = int(marker.read_text().strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _pid_exists(descendant_pid):
        time.sleep(0.02)
    assert not _pid_exists(descendant_pid)


@pytest.mark.parametrize("relative", (False, True))
def test_owner_mode_rejects_noncanonical_entrypoint_before_private_access(
    tmp_path, relative
) -> None:
    root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    venv = repository / ".venv" / "bin"
    scripts.mkdir(parents=True)
    venv.mkdir(parents=True)
    wrapper = scripts / "run_phase41_acceptance.sh"
    helper = scripts / "phase41_acceptance.py"
    shutil.copy2(root / "scripts/run_phase41_acceptance.sh", wrapper)
    shutil.copy2(root / "scripts/phase41_acceptance.py", helper)
    wrapper.chmod(0o755)
    marker = tmp_path / "python-started"
    fake_python = venv / "python"
    fake_python.write_text(
        "#!/bin/bash\nprintf started > \"${PHASE41_OWNER_MARKER}\"\nexit 70\n",
        encoding="ascii",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "KUNJIN_PHASE41_OWNER_APPROVED": "explicit_private_keychain_read_only",
        "PHASE41_OWNER_MARKER": str(marker),
    }
    command = (
        ["scripts/run_phase41_acceptance.sh", "owner"]
        if relative
        else [str(wrapper), "owner"]
    )

    completed = subprocess.run(
        command,
        cwd=repository,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )

    assert completed.returncode == 77
    assert completed.stdout == b""
    assert b'"error_code":"owner_entrypoint_invalid"' in completed.stderr
    assert not marker.exists()
