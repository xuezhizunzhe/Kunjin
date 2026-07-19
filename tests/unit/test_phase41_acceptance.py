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
    sanitize_output,
    secure_read_subject_file,
    validate_engineering_coverage,
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


def _readiness(actions=(), *, ready=False, evidence=None, blocking=None):
    return {
        "command": "fund.shortlist-readiness",
        "data": {
            "bounded_refresh_actions": [
                {"fund_code": code, "command": command} for code, command in actions
            ],
            "candidate_evidence": [] if evidence is None else evidence,
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


def test_engineering_coverage_requires_held_not_comparable_gap_and_partial() -> None:
    initial = _readiness(
        evidence=[{"profile": {"freshness": "stale"}}],
        blocking=["profile_stale"],
    )["data"]
    shortlist = {
        "candidate_reviews": [{"position_state": "held"}],
        "comparability": [{"state": "not_comparable"}],
    }
    final = _readiness(ready=False, blocking=["formal_nav_missing"])["data"]

    assert validate_engineering_coverage(initial, shortlist, final) == {
        "held": True,
        "initial_missing_or_stale": True,
        "not_comparable": True,
        "partial_degradation": True,
    }
    for changed in (
        ({**shortlist, "candidate_reviews": [{"position_state": "not_held"}]}, final),
        ({**shortlist, "comparability": [{"state": "comparable"}]}, final),
        (shortlist, _readiness(ready=True)["data"]),
    ):
        with pytest.raises(StableFailure, match="engineering_coverage_not_met"):
            validate_engineering_coverage(initial, changed[0], changed[1])


def _valid_scope():
    return {
        "candidate_formation": {
            "status": "research_scope_only",
            "candidate_code_discovery": "not_implemented",
        },
        "action_boundary": dict(ACTION_BOUNDARY),
    }


def test_owner_status_validation_accepts_exact_fresh_and_missing_shapes() -> None:
    fresh = validate_owner_statuses(
        {"state": "active", "freshness": "fresh"},
        {
            "state": "fresh",
            "freshness": "fresh",
            "status": "blocked",
            "hard_blocks": ["emergency_reserve_shortfall"],
            "constraints": ["monthly_ceiling_constrained"],
        },
        {"state": "missing", "freshness": "missing", "capability": "research_only"},
        _valid_scope(),
    )
    assert fresh["phase_b"]["status"] == "blocked"
    assert fresh["phase_c"]["status"] is None

    missing = validate_owner_statuses(
        {"state": "missing", "freshness": "missing"},
        {"state": "missing", "freshness": "missing", "capability": "research_only"},
        {"state": "missing", "freshness": "missing", "capability": "research_only"},
        _valid_scope(),
    )
    assert missing["phase_a"]["state"] == "missing"


@pytest.mark.parametrize(
    "phase_a,phase_b,phase_c",
    (
        (
            {"state": "active", "freshness": None},
            {"state": "missing", "freshness": "missing"},
            {"state": "missing", "freshness": "missing"},
        ),
        (
            {"state": "active", "freshness": "fresh"},
            {"state": "fresh", "freshness": "fresh", "status": None},
            {"state": "missing", "freshness": "missing"},
        ),
        (
            {"state": "active", "freshness": "fresh"},
            {"state": "missing", "freshness": "missing"},
            {"state": "fresh", "freshness": "fresh", "status": "unknown"},
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
            return {"state": "active", "freshness": "fresh"}
        if expected == "suitability.status":
            return {
                "state": "fresh",
                "freshness": "fresh",
                "status": "blocked",
                "hard_blocks": ["emergency_reserve_shortfall"],
                "constraints": ["monthly_ceiling_constrained"],
            }
        if expected == "allocation.status":
            return {"state": "missing", "freshness": "missing"}
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


def test_engineering_controller_requires_real_coverage_and_never_emits_codes(
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
        evidence=[{"profile": {"freshness": "stale"}}],
        blocking=["profile_stale"],
    )["data"]
    final = _readiness(ready=False, blocking=["formal_nav_missing"])["data"]
    orchestration = acceptance.OrchestrationResult(
        action_state_counts={"terminal_failure": 1},
        final_data=final,
        final_readiness_calls=1,
        initial_data=initial,
        initial_readiness_calls=1,
        outcome="partial_once",
        refresh_action_calls=1,
        source_status_calls=4,
    )
    monkeypatch.setattr(acceptance, "orchestrate", lambda *_args: orchestration)
    shortlist = {
        "command": "fund.shortlist",
        "data": {
            "candidate_reviews": [{"position_state": "held"}],
            "comparability": [{"state": "not_comparable"}],
        },
    }
    monkeypatch.setattr(
        acceptance,
        "_engineering_cli_result",
        lambda *_args: CommandResult(1, shortlist),
    )
    monkeypatch.setattr(acceptance, "check_runtime_permissions", lambda _runtime: None)

    summary = acceptance.run_engineering_acceptance()
    encoded = json.dumps(summary, sort_keys=True)

    assert summary["coverage"] == {
        "held": True,
        "initial_missing_or_stale": True,
        "not_comparable": True,
        "partial_degradation": True,
    }
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
