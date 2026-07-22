from __future__ import annotations

import hashlib
import json
import os
import pty
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.phase5_acceptance as acceptance
import scripts.phase5_owner_capture as owner_capture
import scripts.phase5_owner_run as owner_run
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


def _run_legacy_private_acceptance_for_test(
    mode: str, runtime_dir: Path
) -> dict[str, object]:
    return acceptance._run_legacy_private_acceptance_for_tests(
        mode,
        runtime_dir,
        test_token=acceptance._LEGACY_PRIVATE_TEST_TOKEN,
    )


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
        "technical_integrity_pass",
        "thesis_review_readiness",
        "owner_workflow_demonstrated",
    }


@pytest.mark.parametrize("mode", ("engineering", "owner"))
def test_project_mode_rejects_legacy_private_orchestration(
    tmp_path: Path, mode: str
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)

    with pytest.raises(ValueError, match="legacy private acceptance disabled"):
        acceptance.project_mode(mode, runtime)


def test_formal_cli_paths_do_not_dispatch_legacy_private_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    package = tmp_path / ("a" * 32)

    def legacy_dispatch(*_args, **_kwargs):
        raise AssertionError("legacy private acceptance must not be dispatched")

    monkeypatch.setattr(
        acceptance,
        "_run_legacy_private_acceptance_for_tests",
        legacy_dispatch,
        raising=False,
    )
    monkeypatch.setattr(acceptance, "_runtime_dir", lambda: runtime)
    monkeypatch.setattr(acceptance, "_run_pytest_probe", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        acceptance,
        "capture_owner",
        lambda *_args, **_kwargs: pytest.fail("main dispatched owner capture"),
    )

    assert acceptance.main(["produce", "engineering"]) == 0
    assert acceptance.main(["capture", "owner", str(package)]) == 2


def test_legacy_private_orchestration_requires_internal_test_token(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="legacy private acceptance is test-only"):
        acceptance._run_legacy_private_acceptance_for_tests(
            "engineering", tmp_path, test_token=object()
        )


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


@pytest.mark.parametrize(
    "failure",
    (
        OSError("token=secret"),
        sqlite3.OperationalError("private-path=/Users/owner"),
    ),
)
def test_owner_keychain_failure_is_classified_without_underlying_detail(
    tmp_path: Path, monkeypatch, failure: Exception
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
        lambda _phase41: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(acceptance.PrivateAcceptanceStageError) as error:
        _run_legacy_private_acceptance_for_test("owner", runtime)

    assert error.value.stage == "owner_keychain"
    assert "secret" not in str(error.value)
    assert "/Users/owner" not in str(error.value)


@pytest.mark.parametrize(
    "failure",
    (KeyboardInterrupt(), SystemExit(19), MemoryError("memory exhausted")),
)
def test_owner_keychain_system_failures_propagate_and_restore_environment(
    tmp_path: Path, monkeypatch, failure: BaseException
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
        lambda _phase41: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(type(failure)):
        _run_legacy_private_acceptance_for_test("owner", runtime)

    assert os.environ["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"] == str(subject_path)
    assert os.environ["KUNJIN_PHASE5_OWNER_APPROVED"] == (
        "explicit_private_read_only_review"
    )
    assert "KUNJIN_DATA_DIR" not in os.environ
    assert "KUNJIN_STATE_DIR" not in os.environ


def test_private_flow_memory_failure_cleans_runtime_and_restores_environment(
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
    monkeypatch.setattr(
        acceptance,
        "_run_private_chain",
        lambda *_args: (_ for _ in ()).throw(MemoryError("memory exhausted")),
    )

    with pytest.raises(MemoryError, match="memory exhausted"):
        _run_legacy_private_acceptance_for_test("owner", runtime)

    assert os.environ["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"] == str(subject_path)
    assert os.environ["KUNJIN_PHASE5_OWNER_APPROVED"] == (
        "explicit_private_read_only_review"
    )
    assert "KUNJIN_DATA_DIR" not in os.environ
    assert "KUNJIN_STATE_DIR" not in os.environ


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


@pytest.mark.parametrize("select_fails", (False, True))
def test_adjudication_digest_always_closes_connection(
    monkeypatch, select_fails: bool
) -> None:
    class Cursor:
        @staticmethod
        def fetchone():
            return (22,)

        @staticmethod
        def fetchall():
            return []

    class Connection:
        closed = False

        def execute(self, statement):
            if select_fails and statement.startswith("SELECT"):
                raise sqlite3.OperationalError("private-path=/Users/owner")
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(sqlite3, "connect", lambda _database: connection)

    if select_fails:
        with pytest.raises(sqlite3.OperationalError):
            acceptance._adjudication_digest(Path("/private/tmp/copy.db"))
    else:
        assert acceptance._adjudication_digest(Path("/private/tmp/copy.db"))[0] == 0
    assert connection.closed is True


def test_adjudication_digest_reads_fresh_wal_copy_without_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "copy.db"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute(
            "CREATE TABLE thesis_evidence_adjudications("
            "id INTEGER PRIMARY KEY, record_checksum TEXT NOT NULL)"
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()
    database.with_name(f"{database.name}-wal").unlink(missing_ok=True)
    database.with_name(f"{database.name}-shm").unlink(missing_ok=True)
    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()

    before = acceptance._adjudication_digest(database)
    assert before[0] == 0
    main_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO thesis_evidence_adjudications VALUES(1, ?)",
            ("a" * 64,),
        )
        writer.commit()
        wal = database.with_name(f"{database.name}-wal")
        assert wal.exists() and wal.stat().st_size > 0
        assert hashlib.sha256(database.read_bytes()).hexdigest() == main_digest

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


def test_owner_runtime_guard_allows_only_registered_framed_workers(
    monkeypatch,
) -> None:
    from kunjin.decision import worker as worker_runtime

    launches = []
    monkeypatch.setattr(
        acceptance.socket,
        "create_connection",
        lambda *_args, **_kwargs: "public-read-only",
    )
    monkeypatch.setattr(
        acceptance.subprocess,
        "Popen",
        lambda *args, **kwargs: launches.append((args, kwargs)) or "validated-worker",
    )

    with acceptance.NoExternalOperations(allow_workers=True):
        with pytest.raises(OSError, match="external operation prohibited"):
            acceptance.socket.create_connection(("example.invalid", 443))
        targets = (
            ("kunjin.decision.worker_main", worker_runtime.PUBLIC_WORKER_ENV),
            ("kunjin.intelligence.worker_main", worker_runtime.PUBLIC_WORKER_ENV),
            (
                "kunjin.brief.portfolio_worker_main",
                worker_runtime.PRIVATE_KEYCHAIN_WORKER_ENV,
            ),
        )
        for module, profile in targets:
            argv = worker_runtime._default_worker_argv(module)
            kwargs = {
                "shell": False,
                "stdin": acceptance.subprocess.PIPE,
                "stdout": acceptance.subprocess.PIPE,
                "stderr": acceptance.subprocess.DEVNULL,
                "close_fds": True,
                "restore_signals": True,
                "start_new_session": True,
                "env": dict(worker_runtime._worker_environment(profile)),
            }
            assert acceptance.subprocess.Popen(argv, **kwargs) == "validated-worker"
        assert len(launches) == len(targets)
        for argv, kwargs in (
            (("/usr/local/bin/docker", "run"), {}),
            ((worker_runtime.sys.executable, "-I", "-m", "unknown.worker"), {}),
            (
                worker_runtime._default_worker_argv(
                    "kunjin.intelligence.worker_main"
                ),
                {"shell": True},
            ),
        ):
            with pytest.raises(OSError, match="external operation prohibited"):
                acceptance.subprocess.Popen(argv, **kwargs)
        module = "kunjin.intelligence.worker_main"
        argv = worker_runtime._default_worker_argv(module)
        drifted_environment = dict(
            worker_runtime._worker_environment(worker_runtime.PUBLIC_WORKER_ENV)
        )
        drifted_environment["UNDECLARED_ENVIRONMENT"] = "1"
        with pytest.raises(OSError, match="external operation prohibited"):
            acceptance.subprocess.Popen(
                argv,
                shell=False,
                stdin=acceptance.subprocess.PIPE,
                stdout=acceptance.subprocess.PIPE,
                stderr=acceptance.subprocess.DEVNULL,
                close_fds=True,
                restore_signals=True,
                start_new_session=True,
                env=drifted_environment,
            )
        with pytest.raises(OSError, match="external operation prohibited"):
            acceptance.subprocess.run(["/usr/bin/true"])

    with acceptance.NoExternalOperations():
        with pytest.raises(OSError, match="external operation prohibited"):
            acceptance.socket.create_connection(("example.invalid", 443))
        with pytest.raises(OSError, match="external operation prohibited"):
            acceptance.subprocess.Popen(["validated-worker"])


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
    monkeypatch.setattr(
        acceptance.socket,
        "create_connection",
        lambda *_args, **_kwargs: "public-read-only",
    )

    class FakeCli:
        @staticmethod
        def run(argv, received_context):
            assert received_context is context
            assert subject_env not in os.environ
            assert os.environ["KUNJIN_DATA_DIR"] == str(runtime / "data")
            assert os.environ["KUNJIN_STATE_DIR"] == str(runtime / "state")
            if mode == "engineering":
                with pytest.raises(OSError, match="portfolio refresh prohibited"):
                    context.brief_service._portfolio_service.sync()
            else:
                assert context.brief_service._portfolio_service.sync() is None
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

    summary = _run_legacy_private_acceptance_for_test(mode, runtime)

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

    with pytest.raises(acceptance.PrivateAcceptanceStageError) as error:
        _run_legacy_private_acceptance_for_test("owner", runtime)

    assert error.value.stage == "private_flow"
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


def test_owner_subject_lease_deletes_only_the_opened_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    subject = parent / "subject.json"
    subject.write_text(
        '{"fund_code":"123456","action":"continue_holding"}', encoding="ascii"
    )
    subject.chmod(0o600)
    monkeypatch.setenv("KUNJIN_PHASE5_OWNER_SUBJECT_FILE", str(subject))
    monkeypatch.setenv(
        "KUNJIN_PHASE5_OWNER_APPROVED", "explicit_private_read_only_review"
    )
    monkeypatch.delenv("KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE", raising=False)

    lease = acceptance._open_owner_subject_lease()
    assert lease.subject == acceptance.PrivateSubject(
        "123456", ActionKind.CONTINUE_HOLDING
    )
    lease.delete()

    assert not subject.exists()


def test_prepared_owner_subject_is_absolute_private_and_preflight_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = acceptance.prepare_owner_subject(
        "123456",
        "continue_holding",
        parent=tmp_path,
    )
    monkeypatch.setattr(
        acceptance,
        "capture_owner",
        lambda *_args, **_kwargs: pytest.fail("preflight started capture"),
    )

    subject = prepared.preflight()
    environment = prepared.owner_environment()

    assert subject == acceptance.PrivateSubject(
        "123456", ActionKind.CONTINUE_HOLDING
    )
    assert prepared.path.is_absolute()
    assert prepared.path.stat().st_mode & 0o777 == 0o600
    assert prepared.path.parent.stat().st_mode & 0o777 == 0o700
    assert environment == {
        "KUNJIN_PHASE5_OWNER_SUBJECT_FILE": str(prepared.path)
    }
    assert "123456" not in repr(prepared)
    assert str(prepared.path) not in repr(prepared)
    prepared.cleanup()
    assert not prepared.path.exists()


def test_owner_controller_execution_is_synthetic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    monkeypatch.setattr(acceptance.subprocess, "run", runner)

    pending = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )

    assert calls == []
    with pytest.raises(ValueError, match="synthetic controller execution required"):
        pending.confirm(runner=runner)
    assert calls == []

    output = pending.confirm(
        runner=runner,
        test_token=acceptance._SYNTHETIC_OWNER_CONTROLLER_TEST_TOKEN,
    )
    confirmation_output = capsys.readouterr()

    assert output == '{"ok":true}'
    argv, kwargs = calls[0]
    assert argv[0] == [str(acceptance._OWNER_ENTRYPOINT), "owner"]
    assert kwargs["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"].startswith("/")
    assert "123456" not in confirmation_output.out + confirmation_output.err
    assert "continue_holding" not in confirmation_output.out + confirmation_output.err
    assert "/" not in confirmation_output.out + confirmation_output.err


def test_owner_run_requires_bridge_confirmation_before_subject_or_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    prepared: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "prepare_owner_subject",
        lambda *_args, **_kwargs: prepared.append(object()),
    )

    with pytest.raises(acceptance.OwnerRunFailure) as error:
        acceptance._run_confirmed_owner_once(
            None, runner=lambda *_args, **_kwargs: runner_calls.append(object())
        )

    assert error.value.stage == "input_received"
    assert prepared == []
    assert runner_calls == []


def test_owner_run_rejects_confirmation_not_issued_by_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    prepared: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "prepare_owner_subject",
        lambda *_args, **_kwargs: prepared.append(object()),
    )
    forged = acceptance._OwnerRunConfirmation(
        _seal=object(),
        _selection=acceptance.PrivateSubject(
            "123456", ActionKind.CONTINUE_HOLDING
        ),
    )

    with pytest.raises(acceptance.OwnerRunFailure) as error:
        acceptance._run_confirmed_owner_once(
            forged, runner=lambda *_args, **_kwargs: runner_calls.append(object())
        )

    assert error.value.stage == "input_received"
    assert prepared == []
    assert runner_calls == []


def test_owner_conversation_bridge_runs_exact_entrypoint_once_and_cleans_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="private output", stderr="")

    bridge = acceptance.OwnerConversationRunBridge()
    bridge.record_explicit_confirmation("123456", "continue_holding")
    result = bridge.run_confirmed_owner_once(runner=runner)
    output = capsys.readouterr()

    argv, kwargs = calls[0]
    subject = Path(kwargs["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"])
    assert result.stage == "runner_finished"
    assert argv[0] == [str(acceptance._OWNER_ENTRYPOINT), "owner"]
    assert kwargs["stdin"] is acceptance.subprocess.DEVNULL
    assert not subject.exists()
    assert "123456" not in repr(result)
    assert "private output" not in output.out + output.err
    assert "/" not in output.out + output.err


def test_owner_conversation_bridge_confirmation_is_single_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*_args, **_kwargs):
        calls.append(object())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    bridge = acceptance.OwnerConversationRunBridge()
    with pytest.raises(acceptance.OwnerRunFailure) as missing:
        bridge.run_confirmed_owner_once(runner=runner)
    assert missing.value.stage == "input_received"
    assert calls == []

    confirmation = bridge.record_explicit_confirmation("123456", "continue_holding")
    assert "123456" not in repr(confirmation)
    bridge.run_confirmed_owner_once(runner=runner)

    with pytest.raises(acceptance.OwnerRunFailure) as reused:
        bridge.run_confirmed_owner_once(runner=runner)
    assert reused.value.stage == "input_received"
    assert calls == [calls[0]]


def test_owner_conversation_bridge_failure_consumes_confirmation_and_cleans_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=70, stdout="", stderr="private detail")

    bridge = acceptance.OwnerConversationRunBridge()
    bridge.record_explicit_confirmation("123456", "continue_holding")
    with pytest.raises(acceptance.OwnerRunFailure) as error:
        bridge.run_confirmed_owner_once(runner=runner)

    subject = Path(calls[0][1]["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"])
    assert error.value.stage == "runner_finished"
    assert calls == [calls[0]]
    assert not subject.exists()
    assert "123456" not in str(error.value)
    assert "private" not in str(error.value)

    with pytest.raises(acceptance.OwnerRunFailure) as reused:
        bridge.run_confirmed_owner_once(runner=runner)
    assert reused.value.stage == "input_received"
    assert calls == [calls[0]]


def test_owner_conversation_bridge_reports_runner_started_and_cleans_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("private failure")

    bridge = acceptance.OwnerConversationRunBridge()
    bridge.record_explicit_confirmation("123456", "continue_holding")
    with pytest.raises(acceptance.OwnerRunFailure) as error:
        bridge.run_confirmed_owner_once(runner=runner)

    subject = Path(calls[0][1]["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"])
    assert error.value.stage == "runner_started"
    assert calls == [calls[0]]
    assert not subject.exists()
    assert "private" not in str(error.value)


def test_owner_run_command_invokes_bridge_once_with_anonymous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    assert owner_run.main(("123456", "continue_holding"), runner=runner) == 0
    output = capsys.readouterr()

    subject = Path(calls[0][1]["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"])
    assert calls[0][0][0] == [str(acceptance._OWNER_ENTRYPOINT), "owner"]
    assert len(calls) == 1
    assert not subject.exists()
    assert output.out == '{"ok":true}\n'
    assert "123456" not in output.out + output.err
    assert "continue_holding" not in output.out + output.err


def test_owner_run_command_stops_after_one_failure_and_hides_runner_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    calls: list[object] = []

    def runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=70, stdout="", stderr="private failure")

    assert owner_run.main(("123456", "continue_holding"), runner=runner) == 70
    output = capsys.readouterr()

    subject = Path(calls[0][1]["env"]["KUNJIN_PHASE5_OWNER_SUBJECT_FILE"])
    assert len(calls) == 1
    assert not subject.exists()
    assert output.out == (
        '{"error_code":"phase5_owner_acceptance_failed",'
        '"failure_category":"runner_finished","ok":false}\n'
    )
    assert "123456" not in output.out + output.err
    assert "private failure" not in output.out + output.err


@pytest.mark.parametrize(
    ("stderr", "expected_category"),
    (
        *(
            (
                json.dumps(
                    {
                        "error_code": "phase5_acceptance_tests_failed",
                        "failure_stage": stage,
                        "ok": False,
                    },
                    separators=(",", ":"),
                ),
                stage,
            )
            for stage in (
                "private_input",
                "owner_keychain",
                "private_database_snapshot",
                "private_flow",
                "private_verification",
            )
        ),
        (
            '{"error_code":"phase5_owner_approval_required","ok":false}',
            "owner_approval_required",
        ),
        (
            '{"error_code":"phase5_private_runtime_override","ok":false}',
            "private_runtime_override",
        ),
        (
            '{"error_code":"phase5_runtime_unavailable","ok":false}',
            "runtime_unavailable",
        ),
        (
            '{"error_code":"phase5_acceptance_output_invalid","ok":false}',
            "acceptance_output_invalid",
        ),
    ),
)
def test_owner_run_forwards_only_whitelisted_failure_categories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stderr: str,
    expected_category: str,
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )

    bridge = acceptance.OwnerConversationRunBridge()
    bridge.record_explicit_confirmation("123456", "continue_holding")
    with pytest.raises(acceptance.OwnerRunFailure) as error:
        bridge.run_confirmed_owner_once(
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=70, stdout="", stderr=stderr
            )
        )

    assert error.value.stage == expected_category
    assert keychain_calls == []
    assert stderr not in str(error.value)


@pytest.mark.parametrize(
    "stderr",
    (
        "not-json",
        "x" * 1025,
        '{"error_code":"phase5_acceptance_tests_failed","failure_stage":"private_flow","ok":false,"extra":true}',
        '{"error_code":"phase5_acceptance_tests_failed","failure_stage":"private/path","ok":false}',
        '{"error_code":"phase5_acceptance_tests_failed","ok":false}',
    ),
)
def test_owner_run_failure_parser_falls_back_without_private_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )

    bridge = acceptance.OwnerConversationRunBridge()
    bridge.record_explicit_confirmation("123456", "continue_holding")
    with pytest.raises(acceptance.OwnerRunFailure) as error:
        bridge.run_confirmed_owner_once(
            runner=lambda *_args, **_kwargs: SimpleNamespace(
                returncode=70, stdout="", stderr=stderr
            )
        )

    assert error.value.stage == "runner_finished"
    assert keychain_calls == []
    assert stderr not in str(error.value)


def test_owner_capture_child_dispatches_once_with_anonymous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package_root = tmp_path / ("a" * 32)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(owner_capture, "_runtime_dir", lambda: runtime)
    monkeypatch.setattr(
        owner_capture,
        "capture_owner",
        lambda package, work: calls.append((package, work)),
    )

    assert owner_capture.main((str(package_root),)) == 0
    output = capsys.readouterr()

    assert calls == [(package_root, runtime)]
    assert output.out == '{"ok":true}\n'
    assert str(package_root) not in output.out + output.err


def test_owner_controller_rejects_expired_failed_and_reused_confirmations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    now = [1_000.0]
    monkeypatch.setattr(acceptance.time, "time", lambda: now[0])
    calls: list[object] = []

    def runner(*_args, **_kwargs):
        calls.append(object())
        return SimpleNamespace(
            returncode=70,
            stdout="",
            stderr='{"error_code":"phase5_acceptance_tests_failed","ok":false}',
        )

    monkeypatch.setattr(acceptance.subprocess, "run", runner)
    pending = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )
    with pytest.raises(ValueError, match="owner confirmation invalid"):
        pending.confirm(
            runner=runner,
            test_token=acceptance._SYNTHETIC_OWNER_CONTROLLER_TEST_TOKEN,
        )
    failed_output = capsys.readouterr()
    assert calls == [calls[0]]
    assert "123456" not in failed_output.out + failed_output.err
    assert "subject" not in failed_output.out + failed_output.err

    with pytest.raises(ValueError, match="owner confirmation invalid"):
        acceptance._load_pending_owner_controller_request(pending.request_id)
    assert calls == [calls[0]]
    reused_output = capsys.readouterr()
    assert "123456" not in reused_output.out + reused_output.err

    expired = acceptance._prepare_owner_controller_request(
        "234567", "continue_holding"
    )
    now[0] += acceptance._OWNER_CONFIRMATION_TTL_SECONDS + 1
    with pytest.raises(ValueError, match="owner confirmation invalid"):
        acceptance._load_pending_owner_controller_request(expired.request_id)
    assert calls == [calls[0]]
    expired_output = capsys.readouterr()
    assert "234567" not in expired_output.out + expired_output.err


def test_owner_controller_prepare_path_has_zero_owner_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )
    monkeypatch.setattr(
        acceptance,
        "capture_owner",
        lambda *_args, **_kwargs: pytest.fail("controller prepare started capture"),
    )

    pending = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )
    output = capsys.readouterr()

    assert keychain_calls == []
    assert "123456" not in repr(pending)
    assert "123456" not in output.out + output.err
    assert "/" not in output.out + output.err


def test_owner_controller_next_call_purges_unconfirmed_expired_request_without_owner_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    now = [1_000.0]
    monkeypatch.setattr(acceptance.time, "time", lambda: now[0])
    expired = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )
    keychain_calls: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )
    monkeypatch.setattr(
        acceptance.subprocess,
        "run",
        lambda *_args, **_kwargs: runner_calls.append(object()),
    )
    now[0] += acceptance._OWNER_CONFIRMATION_TTL_SECONDS + 1

    with pytest.raises(ValueError, match="owner confirmation invalid"):
        acceptance._load_pending_owner_controller_request("a" * 64)
    output = capsys.readouterr()

    assert not expired._state_root.exists()
    assert keychain_calls == []
    assert runner_calls == []
    assert "123456" not in output.out + output.err
    assert "/" not in output.out + output.err


def test_owner_controller_next_call_removes_invalid_symlink_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    external = tmp_path.parent / "external-owner-controller-state"
    external.mkdir(mode=0o700)
    external.chmod(0o700)
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="ascii")
    invalid_root = tmp_path / ("b" * 64)
    invalid_root.symlink_to(external, target_is_directory=True)

    pending = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )

    assert not invalid_root.exists()
    assert sentinel.read_text(encoding="ascii") == "keep"
    pending.cleanup()


def test_owner_controller_pty_transport_records_only_fixed_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )

    def runner(*_args, **_kwargs):
        runner_calls.append(object())
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    master, slave = pty.openpty()
    try:
        os.write(
            master,
            b'{"fund_code":"123456","action":"continue_holding"}\n',
        )
        result = acceptance._run_owner_controller_transport(
            lambda: os.read(slave, 16_385), runner=runner
        )
    finally:
        os.close(master)
        os.close(slave)
    output = capsys.readouterr()

    assert result.stages == (
        "input_received",
        "request_prepared",
        "confirm_loaded",
        "runner_started",
        "runner_finished",
    )
    assert keychain_calls == []
    assert runner_calls == [runner_calls[0]]
    assert "123456" not in output.out + output.err
    assert "continue_holding" not in output.out + output.err
    assert "/" not in output.out + output.err


def test_owner_controller_transport_reports_fixed_stage_for_eof_without_owner_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )

    with pytest.raises(acceptance.OwnerControllerTransportFailure) as error:
        acceptance._run_owner_controller_transport(
            lambda: b"", runner=lambda *_args, **_kwargs: runner_calls.append(object())
        )

    assert error.value.stage == "input_received"
    assert keychain_calls == []
    assert runner_calls == []


def test_owner_controller_transport_reports_runner_finished_without_owner_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    runner_calls: list[object] = []

    def runner(*_args, **_kwargs):
        runner_calls.append(object())
        return SimpleNamespace(returncode=70, stdout="", stderr="")

    with pytest.raises(acceptance.OwnerControllerTransportFailure) as error:
        acceptance._run_owner_controller_transport(
            lambda: b'{"fund_code":"123456","action":"continue_holding"}',
            runner=runner,
        )

    assert error.value.stage == "runner_finished"
    assert runner_calls == [runner_calls[0]]


@pytest.mark.parametrize(
    "argv",
    (
        ("controller", "prepare", "123456", "continue_holding"),
        ("controller", "confirm", "a" * 64),
        ("controller",),
        ("capture", "owner", "/private/tmp/" + "a" * 32),
    ),
)
def test_owner_controller_cli_is_disabled_before_owner_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    , argv: tuple[str, ...]
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    keychain_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )
    monkeypatch.setattr(
        acceptance,
        "capture_owner",
        lambda *_args, **_kwargs: pytest.fail("public prepare started capture"),
    )

    assert acceptance.main(argv) == 2
    output = capsys.readouterr()

    assert keychain_calls == []
    assert "123456" not in output.out + output.err
    assert "continue_holding" not in output.out + output.err
    assert "/" not in output.out + output.err


@pytest.mark.parametrize(
    "kind", ("relative", "symlink", "repository", "wide", "duplicate")
)
def test_owner_controller_cli_rejects_invalid_subject_before_owner_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    monkeypatch.setattr(acceptance, "_owner_controller_parent", lambda: tmp_path)
    pending = acceptance._prepare_owner_controller_request(
        "123456", "continue_holding"
    )
    subject = pending._prepared.path
    marker = pending._state_root / "pending.json"
    if kind == "relative":
        payload = json.loads(marker.read_text(encoding="ascii"))
        payload["subject_root"] = "subject"
        marker.write_text(json.dumps(payload, sort_keys=True), encoding="ascii")
        marker.chmod(0o600)
    elif kind == "symlink":
        target = subject.parent / "target.json"
        subject.replace(target)
        subject.symlink_to(target)
    elif kind == "repository":
        (subject.parent / ".git").mkdir()
    elif kind == "wide":
        subject.chmod(0o644)
    else:
        payload = marker.read_text(encoding="ascii")
        marker.write_text(
            payload[:-1] + ',"request_id":"invalid"}', encoding="ascii"
        )
        marker.chmod(0o600)
    keychain_calls: list[object] = []
    runner_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )
    monkeypatch.setattr(
        acceptance.subprocess,
        "run",
        lambda *_args, **_kwargs: runner_calls.append(object()),
    )

    with pytest.raises(ValueError, match="owner confirmation invalid"):
        acceptance._load_pending_owner_controller_request(pending.request_id)
    output = capsys.readouterr()

    assert keychain_calls == []
    assert runner_calls == []
    assert not pending._state_root.exists()
    assert "123456" not in repr(pending)
    assert "123456" not in output.out + output.err
    assert "/" not in output.out + output.err


@pytest.mark.parametrize("kind", ("relative", "symlink", "repository", "wide"))
def test_prepared_owner_subject_preserves_private_path_rejections(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = acceptance.prepare_owner_subject(
        "123456",
        "continue_holding",
        parent=tmp_path,
    )
    if kind == "relative":
        prepared.path = Path("subject.json")
    elif kind == "symlink":
        target = prepared.path.parent / "target.json"
        prepared.path.replace(target)
        prepared.path.symlink_to(target)
    elif kind == "repository":
        (prepared.path.parent / ".git").mkdir()
    else:
        prepared.path.chmod(0o644)
    keychain_calls: list[object] = []
    monkeypatch.setattr(
        acceptance,
        "_load_owner_key_without_sensitive_environment",
        lambda *_args: keychain_calls.append(object()),
    )
    monkeypatch.setattr(
        acceptance,
        "capture_owner",
        lambda *_args, **_kwargs: pytest.fail("preflight started capture"),
    )

    with pytest.raises(ValueError, match="owner subject preflight invalid"):
        prepared.preflight()
    assert not prepared.path.exists()
    assert keychain_calls == []


@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt(),
        SystemExit(19),
        GeneratorExit(),
        MemoryError("memory exhausted"),
    ),
)
def test_capture_owner_system_failure_consumes_subject_and_owner_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    subject_parent = tmp_path / "owner-subject"
    subject_parent.mkdir(mode=0o700)
    subject_parent.chmod(0o700)
    subject = subject_parent / "subject.json"
    subject.write_text(
        '{"fund_code":"123456","action":"continue_holding"}', encoding="ascii"
    )
    subject.chmod(0o600)
    package_base = tmp_path / "captures"
    package_base.mkdir(mode=0o700)
    package_base.chmod(0o700)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setenv("KUNJIN_PHASE5_OWNER_SUBJECT_FILE", str(subject))
    monkeypatch.setenv("KUNJIN_PHASE5_OWNER_APPROVED", acceptance._OWNER_APPROVAL)
    monkeypatch.delenv("KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE", raising=False)

    def fail_capture(*_args, **_kwargs):
        assert subject.exists()
        raise failure

    monkeypatch.setattr("scripts.phase5_capture.capture_rapid", fail_capture)
    monkeypatch.setattr(acceptance, "_capture_dependencies", lambda: object())

    with pytest.raises(type(failure)):
        acceptance.capture_owner(package_base / ("a" * 32), runtime)

    assert not subject.exists()
    assert all(name not in os.environ for name in acceptance._OWNER_INPUT_NAMES)


def test_replay_rejects_owner_inputs_before_importing_live_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KUNJIN_PHASE5_OWNER_SUBJECT_FILE", "/private/subject")

    with pytest.raises(ValueError, match="private replay environment"):
        acceptance.replay_package(tmp_path / "package", tmp_path / "replay")


def test_compare_private_replays_emits_only_public_summary_and_deletes_results(
    tmp_path: Path,
) -> None:
    from scripts.phase5_replay import ReplayResult, write_protected_result

    runtime = tmp_path / "runtime"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    for path in (runtime, first_root, second_root):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    result = ReplayResult(
        engineering_flow="pass",
        technical_integrity_pass=True,
        owner_workflow_demonstrated=False,
        owner_capture=False,
        profile_key_reads=0,
        portfolio_token_reads=0,
        portfolio_token_mutation_attempts=0,
        capture_status="partial",
        target_position_binding_verified=False,
        evidence_readiness="partial",
        history_comparability="not_available",
        thesis_review_readiness="manual_review_required",
        conditional_review_usability="partial",
        redemption_feasibility="not_requested",
        sell_timing="insufficient_data",
        action_authorized=False,
        exact_amount_available=False,
        automatic_trade=False,
        binding_checksum="a" * 64,
    )
    first = write_protected_result(result, first_root)
    second = write_protected_result(result, second_root)
    summary = runtime / "summary.json"

    acceptance.compare_private_replays(
        "engineering", first, second, summary, runtime
    )

    payload = json.loads(summary.read_text(encoding="ascii"))
    assert payload["mode"] == "engineering"
    assert payload["technical_integrity_pass"] is True
    assert payload["owner_workflow_demonstrated"] is False
    assert "binding_checksum" not in payload
    assert not first.exists()
    assert not second.exists()


def test_protected_replay_rejects_tampered_owner_workflow_status(tmp_path: Path) -> None:
    from scripts.phase5_replay import ReplayResult, write_protected_result

    replay_root = tmp_path / "replay"
    replay_root.mkdir(mode=0o700)
    replay_root.chmod(0o700)
    result = ReplayResult(
        engineering_flow="pass",
        technical_integrity_pass=True,
        owner_workflow_demonstrated=False,
        owner_capture=False,
        profile_key_reads=0,
        portfolio_token_reads=0,
        portfolio_token_mutation_attempts=0,
        capture_status="partial",
        target_position_binding_verified=False,
        evidence_readiness="partial",
        history_comparability="not_available",
        thesis_review_readiness="manual_review_required",
        conditional_review_usability="partial",
        redemption_feasibility="not_requested",
        sell_timing="insufficient_data",
        action_authorized=False,
        exact_amount_available=False,
        automatic_trade=False,
        binding_checksum="a" * 64,
    )
    protected = write_protected_result(result, replay_root)
    payload = json.loads(protected.read_text(encoding="ascii"))
    payload["owner_workflow_demonstrated"] = True
    protected.write_text(json.dumps(payload, sort_keys=True), encoding="ascii")
    protected.chmod(0o600)

    with pytest.raises(ValueError, match="protected replay result invalid"):
        acceptance._read_protected_replay_result(protected)


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
