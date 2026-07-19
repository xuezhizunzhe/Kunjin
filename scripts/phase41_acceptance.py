from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

ACTION_BOUNDARY = {
    "action_maturity": "evidence_only",
    "action_authorized": False,
    "exact_amount_available": False,
    "automatic_trade": False,
}
ENGINEERING_ROLES = tuple(f"engineering_subject_{index}" for index in range(1, 5))
MAX_PUBLIC_SOURCE_STATUS_CALLS = 5
MAX_PUBLIC_ACTION_CALLS = 25
_CODE = re.compile(r"[0-9]{6}")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]*")
_TERMINAL_SOURCE_STATES = frozenset({"cooldown", "unavailable", "unsupported"})
_ACTION_SPECS = (
    (
        "sync_fund",
        re.compile(r"sync fund ([0-9]{6})"),
        lambda code: ["--json", "sync", "fund", code],
        "sync.fund",
        frozenset({"formal_nav", "adjusted_return_series"}),
        None,
    ),
    (
        "sync_fund_profile",
        re.compile(r"sync fund-profile ([0-9]{6}) --mode rapid"),
        lambda code: ["--json", "sync", "fund-profile", code, "--mode", "rapid"],
        "sync.fund-profile",
        frozenset(
            {
                "identity_active_status",
                "current_manager_team",
                "fees_share_class_relationship",
            }
        ),
        None,
    ),
    (
        "sync_fund_holdings",
        re.compile(r"sync fund-holdings ([0-9]{6}) --mode rapid"),
        lambda code: ["--json", "sync", "fund-holdings", code, "--mode", "rapid"],
        "sync.fund-holdings",
        frozenset({"holdings_industries"}),
        None,
    ),
    (
        "sync_fund_documents",
        re.compile(r"sync fund-documents ([0-9]{6})"),
        lambda code: ["--json", "sync", "fund-documents", code],
        "sync.fund-documents",
        frozenset(),
        "d1_documents",
    ),
    (
        "fund_classify",
        re.compile(r"fund classify ([0-9]{6})"),
        lambda code: ["--json", "fund", "classify", code],
        "fund.classify",
        frozenset(),
        "d1_classification",
    ),
)


class StableFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        if type(code) is not str or _SAFE_CODE.fullmatch(code) is None:
            code = "phase41_runtime_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    payload: Optional[dict]


@dataclass(frozen=True)
class OrchestrationResult:
    action_state_counts: Dict[str, int]
    final_data: dict
    final_readiness_calls: int
    initial_data: dict
    initial_readiness_calls: int
    outcome: str
    refresh_action_calls: int
    source_status_calls: int


@dataclass(frozen=True)
class _PlannedAction:
    code: str
    role: str
    action_type: str
    argv: Tuple[str, ...]
    expected_command: str
    affected_fields: frozenset
    dependency: Optional[str]
    order: int


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_nofollow(path: Path, *, failure_code: str) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise StableFailure(failure_code)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(str(path), flags)
    except OSError:
        raise StableFailure(failure_code) from None


def secure_read_subject_file(path: Path, excluded_roots: Iterable[Path]) -> Tuple[str, ...]:
    failure = "engineering_subject_file_invalid"
    if type(path) is not type(Path()) or not path.is_absolute():
        raise StableFailure(failure)
    try:
        parent = path.parent.resolve(strict=True)
        parent_metadata = os.lstat(parent)
    except (OSError, RuntimeError):
        raise StableFailure(failure) from None
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != os.getuid()
    ):
        raise StableFailure(failure)
    fd = _open_nofollow(path, failure_code=failure)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
        ):
            raise StableFailure(failure)
        resolved = path.resolve(strict=True)
        current = os.lstat(path)
        if (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino):
            raise StableFailure(failure)
        if any(_under(resolved, Path(root).resolve(strict=False)) for root in excluded_roots):
            raise StableFailure(failure)
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise StableFailure(failure)
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeError, ValueError, TypeError):
            raise StableFailure(failure) from None
        if type(payload) is not dict or set(payload) != set(ENGINEERING_ROLES):
            raise StableFailure(failure)
        codes = tuple(payload[role] for role in ENGINEERING_ROLES)
        if (
            any(type(code) is not str or _CODE.fullmatch(code) is None for code in codes)
            or len(set(codes)) != len(ENGINEERING_ROLES)
            or any(code == "0" * 6 for code in codes)
        ):
            raise StableFailure(failure)
        return codes
    finally:
        os.close(fd)


def assert_same_inode(fd: int, path: Path) -> None:
    try:
        opened = os.fstat(fd)
        current = os.lstat(path)
    except OSError:
        raise StableFailure("private_database_changed") from None
    if stat.S_ISLNK(current.st_mode) or (opened.st_dev, opened.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        raise StableFailure("private_database_changed")


def _fd_digest(fd: int) -> str:
    value = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            value.update(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        raise StableFailure("private_database_invalid") from None
    return value.hexdigest()


def backup_sqlite_read_only(source_path: Path, target_path: Path) -> Tuple[str, int]:
    failure = "private_database_invalid"
    if type(source_path) is not type(Path()) or not source_path.is_absolute():
        raise StableFailure(failure)
    try:
        target_parent = target_path.parent.resolve(strict=True)
        parent_metadata = os.lstat(target_parent)
    except (OSError, RuntimeError):
        raise StableFailure(failure) from None
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or target_path.exists()
        or target_path.is_symlink()
    ):
        raise StableFailure(failure)
    fd = _open_nofollow(source_path, failure_code=failure)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_uid != os.getuid()
        ):
            raise StableFailure(failure)
        assert_same_inode(fd, source_path)
        before = _fd_digest(fd)
        uri = source_path.as_uri() + "?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as source:
                data_version = source.execute("PRAGMA data_version").fetchone()[0]
                with sqlite3.connect(target_path) as target:
                    source.backup(target)
        except sqlite3.Error:
            raise StableFailure(failure) from None
        os.chmod(target_path, 0o600)
        assert_same_inode(fd, source_path)
        if _fd_digest(fd) != before:
            raise StableFailure("private_database_changed")
        return before, data_version
    finally:
        os.close(fd)


def _command_data(result: CommandResult, expected: str, *, required: bool) -> Optional[dict]:
    if type(result) is not CommandResult or type(result.exit_code) is not int:
        if required:
            raise StableFailure("engineering_orchestration_invalid")
        return None
    if result.exit_code not in {0, 1}:
        if required:
            raise StableFailure("engineering_orchestration_invalid")
        return None
    payload = result.payload
    if payload is None:
        if required:
            raise StableFailure("engineering_orchestration_invalid")
        return None
    if type(payload) is not dict or payload.get("command") != expected:
        if required:
            raise StableFailure("engineering_orchestration_invalid")
        return None
    data = payload.get("data")
    if type(data) is not dict:
        if required:
            raise StableFailure("engineering_orchestration_invalid")
        return None
    return data


def _parse_actions(data: Mapping[str, object], codes: Sequence[str], roles: Sequence[str]):
    raw_actions = data.get("bounded_refresh_actions")
    if type(raw_actions) is not list:
        raise StableFailure("engineering_orchestration_invalid")
    role_by_code = dict(zip(codes, roles))
    seen = set()
    planned = []
    for item in raw_actions:
        if type(item) is not dict or set(item) != {"fund_code", "command"}:
            raise StableFailure("engineering_orchestration_invalid")
        code = item["fund_code"]
        command = item["command"]
        if code not in role_by_code or type(command) is not str or "--force" in command:
            raise StableFailure("engineering_orchestration_invalid")
        selected = None
        for order, spec in enumerate(_ACTION_SPECS):
            action_type, pattern, argv_builder, expected, fields, dependency = spec
            match = pattern.fullmatch(command)
            if match is not None and match.group(1) == code:
                selected = _PlannedAction(
                    code=code,
                    role=role_by_code[code],
                    action_type=action_type,
                    argv=tuple(argv_builder(code)),
                    expected_command=expected,
                    affected_fields=fields,
                    dependency=dependency,
                    order=order,
                )
                break
        if selected is None:
            raise StableFailure("engineering_orchestration_invalid")
        key = (selected.role, selected.action_type)
        if key in seen:
            raise StableFailure("engineering_orchestration_invalid")
        seen.add(key)
        planned.append(selected)
    if len(planned) > MAX_PUBLIC_ACTION_CALLS:
        raise StableFailure("engineering_orchestration_invalid")
    role_order = {role: index for index, role in enumerate(roles)}
    return tuple(sorted(planned, key=lambda item: (role_order[item.role], item.order)))


def _source_stops(data: Optional[dict], code: str) -> Tuple[bool, frozenset]:
    if data is None or data.get("fund_code") != code:
        return True, frozenset()
    resolutions = data.get("request_field_resolutions")
    fields = data.get("source_fields")
    if type(resolutions) is not list or type(fields) is not list:
        return True, frozenset()
    primary_rows = {}
    for item in fields:
        if type(item) is not dict:
            return True, frozenset()
        field_id = item.get("field_id")
        source_id = item.get("source_id")
        state = item.get("state")
        if not all(type(value) is str for value in (field_id, source_id, state)):
            return True, frozenset()
        primary_rows[(field_id, source_id)] = state
    stopped = set()
    for item in resolutions:
        if type(item) is not dict:
            return True, frozenset()
        field_id = item.get("field_id")
        source_id = item.get("primary_source_id")
        resolution = item.get("resolution")
        if not all(type(value) is str for value in (field_id, source_id, resolution)):
            return True, frozenset()
        if resolution == "manual_supplement_required":
            stopped.add(field_id)
            continue
        primary_state = primary_rows.get((field_id, source_id))
        if primary_state is None:
            return True, frozenset()
        if primary_state in _TERMINAL_SOURCE_STATES:
            stopped.add(field_id)
    return False, frozenset(stopped)


def orchestrate(
    codes: Sequence[str],
    roles: Sequence[str],
    invoke: Callable[[list], CommandResult],
) -> OrchestrationResult:
    codes = tuple(codes)
    roles = tuple(roles)
    if (
        not 2 <= len(codes) <= MAX_PUBLIC_SOURCE_STATUS_CALLS
        or len(codes) != len(roles)
        or len(set(codes)) != len(codes)
        or len(set(roles)) != len(roles)
        or any(type(code) is not str or _CODE.fullmatch(code) is None for code in codes)
    ):
        raise StableFailure("engineering_orchestration_invalid")
    readiness_argv = ["--json", "fund", "shortlist-readiness", *codes]
    initial_data = _command_data(
        invoke(readiness_argv), "fund.shortlist-readiness", required=True
    )
    initial_calls = 1
    final_calls = 0
    final_data = {}
    source_calls = 0
    action_calls = 0
    states = Counter()
    try:
        source_stops = {}
        for code, role in zip(codes, roles):
            try:
                source_result = invoke(
                    ["--json", "source", "status", "--fund-code", code]
                )
                source_data = _command_data(source_result, "source.status", required=False)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                source_data = None
            source_calls += 1
            source_stops[role] = _source_stops(source_data, code)

        planned = _parse_actions(initial_data, codes, roles)
        failed_dependencies = set()
        for action in planned:
            source_failed, stopped_fields = source_stops[action.role]
            if source_failed or action.affected_fields & stopped_fields:
                states["stopped_by_source_state"] += 1
                if action.dependency == "d1_documents":
                    failed_dependencies.add((action.role, "d1_documents"))
                continue
            if action.dependency == "d1_classification" and (
                action.role,
                "d1_documents",
            ) in failed_dependencies:
                states["dependency_stopped"] += 1
                continue
            try:
                action_result = invoke(list(action.argv))
                action_calls += 1
                action_data = _command_data(
                    action_result,
                    action.expected_command,
                    required=False,
                )
                succeeded = action_result.exit_code == 0 and action_data is not None
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                action_calls += 1
                succeeded = False
            states["completed" if succeeded else "terminal_failure"] += 1
            if not succeeded and action.dependency == "d1_documents":
                failed_dependencies.add((action.role, "d1_documents"))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        error = exc
    else:
        error = None
    try:
        final_result = invoke(readiness_argv)
        final_calls += 1
        final_data = _command_data(
            final_result,
            "fund.shortlist-readiness",
            required=True,
        )
    except BaseException as exc:
        final_calls += 1
        if error is None:
            error = exc
    if error is not None:
        raise error
    if initial_calls != 1 or final_calls != 1 or source_calls != len(codes):
        raise StableFailure("engineering_orchestration_invalid")
    ready = final_data.get("comparison_evidence_ready") is True
    if states.get("stopped_by_source_state"):
        outcome = "stopped_by_source_state"
    elif ready:
        outcome = "completed_once"
    elif action_calls:
        outcome = "partial_once"
    else:
        outcome = "not_run"
    return OrchestrationResult(
        action_state_counts=dict(sorted(states.items())),
        final_data=final_data,
        final_readiness_calls=final_calls,
        initial_data=initial_data,
        initial_readiness_calls=initial_calls,
        outcome=outcome,
        refresh_action_calls=action_calls,
        source_status_calls=source_calls,
    )


def _contains_missing_or_stale(value: object) -> bool:
    if type(value) is str:
        return value in {"missing", "stale", "insufficient_data"} or value.endswith(
            ("_missing", "_stale")
        )
    if type(value) is dict:
        return any(_contains_missing_or_stale(item) for item in value.values())
    if type(value) in {list, tuple}:
        return any(_contains_missing_or_stale(item) for item in value)
    return False


def validate_engineering_coverage(initial: dict, shortlist: dict, final: dict) -> dict:
    if not all(type(value) is dict for value in (initial, shortlist, final)):
        raise StableFailure("engineering_coverage_not_met")
    reviews = shortlist.get("candidate_reviews")
    comparability = shortlist.get("comparability")
    coverage = {
        "held": type(reviews) is list
        and any(type(item) is dict and item.get("position_state") == "held" for item in reviews),
        "initial_missing_or_stale": _contains_missing_or_stale(
            initial.get("candidate_evidence", [])
        )
        or _contains_missing_or_stale(initial.get("blocking_codes", [])),
        "not_comparable": type(comparability) is list
        and any(
            type(item) is dict and item.get("state") == "not_comparable"
            for item in comparability
        ),
        "partial_degradation": final.get("comparison_evidence_ready") is False
        and type(final.get("blocking_codes")) is list
        and bool(final["blocking_codes"]),
    }
    if not all(coverage.values()):
        raise StableFailure("engineering_coverage_not_met")
    return coverage


def _codes(value: object) -> list:
    if type(value) is not list or any(
        type(item) is not str or _SAFE_CODE.fullmatch(item) is None for item in value
    ):
        raise StableFailure("owner_status_invalid")
    return sorted(set(value))


def _state_pair(value: dict, allowed_states: set) -> Tuple[str, str]:
    if type(value) is not dict:
        raise StableFailure("owner_status_invalid")
    state = value.get("state")
    freshness = value.get("freshness")
    if state not in allowed_states or freshness not in {"fresh", "stale", "missing"}:
        raise StableFailure("owner_status_invalid")
    if state == "missing" and freshness != "missing":
        raise StableFailure("owner_status_invalid")
    if state in {"fresh", "stale"} and freshness != state:
        raise StableFailure("owner_status_invalid")
    return state, freshness


def validate_owner_statuses(
    profile: dict,
    suitability: dict,
    allocation: dict,
    scope: dict,
) -> dict:
    profile_state, profile_freshness = _state_pair(
        profile, {"active", "missing", "invalidated"}
    )
    if profile_state != "missing" and profile_freshness not in {"fresh", "stale"}:
        raise StableFailure("owner_status_invalid")
    b_state, b_freshness = _state_pair(suitability, {"fresh", "stale", "missing"})
    b_status = suitability.get("status")
    if b_state == "missing":
        if b_status is not None:
            raise StableFailure("owner_status_invalid")
        hard_blocks = []
        constraints = []
    else:
        if b_status not in {"blocked", "constrained", "ready_for_allocation"}:
            raise StableFailure("owner_status_invalid")
        hard_blocks = _codes(suitability.get("hard_blocks"))
        constraints = _codes(suitability.get("constraints"))
    c_state, c_freshness = _state_pair(allocation, {"fresh", "stale", "missing"})
    c_status = allocation.get("status")
    if c_state == "missing":
        if c_status is not None:
            raise StableFailure("owner_status_invalid")
        binding = []
    else:
        if c_status not in {"blocked", "range_available"}:
            raise StableFailure("owner_status_invalid")
        binding = _codes(allocation.get("binding_constraints"))
    if type(scope) is not dict:
        raise StableFailure("owner_status_invalid")
    candidate = scope.get("candidate_formation")
    boundary = scope.get("action_boundary")
    if candidate != {
        "status": "research_scope_only",
        "candidate_code_discovery": "not_implemented",
    } or boundary != ACTION_BOUNDARY:
        raise StableFailure("owner_status_invalid")
    return {
        "action_boundary": dict(ACTION_BOUNDARY),
        "candidate_formation": candidate,
        "financial_usability": "not_yet_testable",
        "owner_candidate_state": "owner_candidates_unavailable",
        "phase_a": {"state": profile_state, "freshness": profile_freshness},
        "phase_b": {
            "state": b_state,
            "freshness": b_freshness,
            "status": b_status,
            "blocking_codes": hard_blocks,
            "constraint_codes": constraints,
        },
        "phase_c": {
            "state": c_state,
            "freshness": c_freshness,
            "status": c_status,
            "constraint_codes": binding,
        },
    }


def load_owner_key_once(
    child_runner: Callable[[list], Tuple[int, str, str]],
) -> bytes:
    command = [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "com.kunjin.profile-encryption",
        "-a",
        "v1",
        "-w",
    ]
    try:
        returncode, stdout, _stderr = child_runner(command)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise StableFailure("owner_keychain_unavailable") from None
    if returncode != 0 or type(stdout) is not str:
        raise StableFailure("owner_keychain_unavailable")
    encoded = stdout.strip()
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError, binascii.Error):
        raise StableFailure("owner_keychain_unavailable") from None
    if len(key) != 32:
        raise StableFailure("owner_keychain_unavailable")
    return key


class InMemoryKeyStore:
    def __init__(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise StableFailure("owner_keychain_unavailable")
        self._key = key

    def load_existing_key(self) -> bytes:
        return self._key

    def load_or_create_key(self) -> bytes:
        raise StableFailure("owner_keychain_write_prohibited")

    def save_key(self, _key: bytes) -> None:
        raise StableFailure("owner_keychain_write_prohibited")


class OwnerRuntimeGuards(AbstractContextManager):
    def __init__(self) -> None:
        self._originals = []

    @staticmethod
    def _deny(*_args, **_kwargs):
        raise StableFailure("owner_external_operation_prohibited")

    def _patch(self, owner: object, name: str) -> None:
        if hasattr(owner, name):
            original = getattr(owner, name)
            self._originals.append((owner, name, original))
            setattr(owner, name, self._deny)

    def __enter__(self):
        for name in ("create_connection", "getaddrinfo"):
            self._patch(socket, name)
        for name in ("connect", "connect_ex"):
            self._patch(socket.socket, name)
        for name in (
            "Popen",
            "run",
            "call",
            "check_call",
            "check_output",
        ):
            self._patch(subprocess, name)
        for name in (
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
            "system",
            "popen",
            "fork",
            "forkpty",
            "posix_spawn",
            "posix_spawnp",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
        ):
            self._patch(os, name)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        while self._originals:
            owner, name, original = self._originals.pop()
            setattr(owner, name, original)
        return False


class TrackedChildren(AbstractContextManager):
    def __init__(self) -> None:
        self._original_popen = subprocess.Popen
        self._children = []
        self._installed = False

    def popen(self, *args, **kwargs):
        process = self._original_popen(*args, **kwargs)
        self._children.append(process)
        return process

    def run_exact(self, command: list) -> Tuple[int, str, str]:
        if type(command) is not list or command != [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "com.kunjin.profile-encryption",
            "-a",
            "v1",
            "-w",
        ]:
            raise StableFailure("owner_external_operation_prohibited")
        process = self.popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def install_tracking(self) -> None:
        if self._installed:
            raise StableFailure("phase41_child_tracking_invalid")
        subprocess.Popen = self.popen
        self._installed = True

    def restore_tracking(self) -> None:
        if self._installed:
            subprocess.Popen = self._original_popen
            self._installed = False

    def assert_waited(self) -> None:
        if any(process.poll() is None for process in self._children):
            raise StableFailure("phase41_child_residue")

    def terminate_all(self) -> None:
        for process in self._children:
            if process.poll() is None:
                process.terminate()
        for process in self._children:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.restore_tracking()
        self.terminate_all()
        self.assert_waited()
        return False


class ReadOnlyDatabaseGuard(AbstractContextManager):
    def __init__(self, source_path: Path, target_path: Path) -> None:
        self.source_path = source_path
        self.target_path = target_path
        self.fd = -1
        self.connection = None
        self.digest = None
        self.data_version = None
        self.identity = None

    def __enter__(self):
        failure = "private_database_invalid"
        if type(self.source_path) is not type(Path()) or not self.source_path.is_absolute():
            raise StableFailure(failure)
        try:
            parent = self.target_path.parent.resolve(strict=True)
            parent_metadata = os.lstat(parent)
        except (OSError, RuntimeError):
            raise StableFailure(failure) from None
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or self.target_path.exists()
            or self.target_path.is_symlink()
        ):
            raise StableFailure(failure)
        self.fd = _open_nofollow(self.source_path, failure_code=failure)
        try:
            metadata = os.fstat(self.fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_uid != os.getuid()
            ):
                raise StableFailure(failure)
            assert_same_inode(self.fd, self.source_path)
            self.identity = (metadata.st_dev, metadata.st_ino)
            self.digest = _fd_digest(self.fd)
            self.connection = sqlite3.connect(
                self.source_path.as_uri() + "?mode=ro",
                uri=True,
            )
            self.data_version = self.connection.execute("PRAGMA data_version").fetchone()[0]
            with sqlite3.connect(self.target_path) as target:
                self.connection.backup(target)
            os.chmod(self.target_path, 0o600)
            self.verify()
            return self
        except BaseException as exc:
            self._release()
            try:
                self.target_path.unlink(missing_ok=True)
            except OSError:
                pass
            if isinstance(exc, StableFailure):
                raise
            raise StableFailure(failure) from None

    def verify(self) -> None:
        if self.fd < 0 or self.connection is None:
            raise StableFailure("private_database_invalid")
        assert_same_inode(self.fd, self.source_path)
        metadata = os.fstat(self.fd)
        if (metadata.st_dev, metadata.st_ino) != self.identity:
            raise StableFailure("private_database_changed")
        if _fd_digest(self.fd) != self.digest:
            raise StableFailure("private_database_changed")
        current_version = self.connection.execute("PRAGMA data_version").fetchone()[0]
        if current_version != self.data_version:
            raise StableFailure("private_database_changed")

    def __exit__(self, exc_type, exc_value, traceback):
        close_error = None
        try:
            if self.connection is not None:
                self.verify()
        except BaseException as exc:
            close_error = exc
        finally:
            self._release()
        if exc_value is None and close_error is not None:
            raise close_error
        return False

    def _release(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _runtime_dir() -> Path:
    value = os.environ.get("KUNJIN_PHASE41_RUNTIME_DIR")
    if not value:
        raise StableFailure("phase41_runtime_invalid")
    path = Path(value)
    try:
        metadata = os.lstat(path)
    except OSError:
        raise StableFailure("phase41_runtime_invalid") from None
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise StableFailure("phase41_runtime_invalid")
    return path


def check_runtime_permissions(runtime: Path) -> None:
    try:
        paths = (runtime, *runtime.rglob("*"))
        for path in paths:
            metadata = os.lstat(path)
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise StableFailure("phase41_runtime_permissions_invalid")
    except OSError:
        raise StableFailure("phase41_runtime_permissions_invalid") from None


def _canonical_owner_database() -> Path:
    if "KUNJIN_DATA_DIR" in os.environ or "KUNJIN_STATE_DIR" in os.environ:
        raise StableFailure("owner_runtime_override_prohibited")
    return Path.home() / ".local" / "share" / "kunjin" / "kunjin.db"


def _build_context_with_key(key: bytes):
    import kunjin.cli as cli

    key_store = InMemoryKeyStore(key)
    original = cli.ProfileKeyStore
    cli.ProfileKeyStore = lambda: key_store
    try:
        context = cli.build_context()
    finally:
        cli.ProfileKeyStore = original
    return cli, context


def _build_engineering_context():
    import kunjin.cli as cli
    from kunjin.selection.readiness import ShortlistReadinessService
    from kunjin.selection.service import ShortlistService

    context = cli.build_context()

    def missing_gate():
        return {
            "state": "missing",
            "freshness": "missing",
            "capability": "research_only",
        }

    context.shortlist_readiness_service = ShortlistReadinessService(
        context.repository,
        context.fund_disclosure_store,
        source_health_service=context.source_health_service,
        classification_loader=context.fund_risk_service.current_classification,
        suitability_status_loader=missing_gate,
        allocation_status_loader=missing_gate,
    )
    context.selection_service = ShortlistService(
        context.repository,
        context.fund_disclosure_store,
        classification_loader=context.fund_risk_service.current_classification,
        suitability_status_loader=missing_gate,
        allocation_status_loader=missing_gate,
    )
    return cli, context


def _safe_cli_call(cli, context, argv: list, expected: str) -> dict:
    payload, exit_code, json_output = cli.run(["--json", *argv], context)
    if (
        not json_output
        or exit_code != 0
        or type(payload) is not dict
        or payload.get("command") != expected
        or type(payload.get("data")) is not dict
    ):
        raise StableFailure("owner_status_invalid")
    return payload["data"]


def run_owner_acceptance() -> dict:
    if os.environ.get("KUNJIN_PHASE41_OWNER_APPROVED") != (
        "explicit_private_keychain_read_only"
    ):
        raise StableFailure("owner_approval_required")
    runtime = _runtime_dir()
    source = _canonical_owner_database()
    data_dir = runtime / "data"
    state_dir = runtime / "state"
    data_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    os.chmod(data_dir, 0o700)
    os.chmod(state_dir, 0o700)
    target = data_dir / "kunjin.db"
    with TrackedChildren() as children:
        key = load_owner_key_once(children.run_exact)
        if len(children._children) != 1:
            raise StableFailure("owner_keychain_unavailable")
        children.assert_waited()
        with ReadOnlyDatabaseGuard(source, target):
            os.environ["KUNJIN_DATA_DIR"] = str(data_dir)
            os.environ["KUNJIN_STATE_DIR"] = str(state_dir)
            try:
                with OwnerRuntimeGuards():
                    cli, context = _build_context_with_key(key)
                    profile = _safe_cli_call(
                        cli, context, ["profile", "status"], "profile.status"
                    )
                    suitability = _safe_cli_call(
                        cli,
                        context,
                        ["suitability", "status"],
                        "suitability.status",
                    )
                    allocation = _safe_cli_call(
                        cli,
                        context,
                        ["allocation", "status"],
                        "allocation.status",
                    )
                    scope = _safe_cli_call(
                        cli,
                        context,
                        ["fund", "research-scope"],
                        "fund.research-scope",
                    )
            finally:
                os.environ.pop("KUNJIN_DATA_DIR", None)
                os.environ.pop("KUNJIN_STATE_DIR", None)
                key = b""
    summary = validate_owner_statuses(profile, suitability, allocation, scope)
    summary.update(
        {
            "mode": "owner",
            "real_database_opened_read_only": True,
            "single_context": True,
            "single_keychain_child": True,
        }
    )
    check_runtime_permissions(runtime)
    return summary


def _engineering_excluded_roots(runtime: Path) -> Tuple[Path, ...]:
    home = Path.home()
    repository = Path(__file__).resolve().parents[1]
    return (
        repository,
        home / ".codex" / "skills" / "kunjin-fund",
        home / ".local" / "state" / "kunjin" / "logs",
        repository / "logs",
        runtime,
    )


def _engineering_cli_result(cli, context, argv: list) -> CommandResult:
    try:
        payload, exit_code, json_output = cli.run(argv, context)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return CommandResult(1, None)
    if not json_output or exit_code not in {0, 1}:
        return CommandResult(1, None)
    return CommandResult(exit_code, payload if type(payload) is dict else None)


def run_engineering_acceptance() -> dict:
    runtime = _runtime_dir()
    subject_value = os.environ.get("KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE")
    if not subject_value:
        raise StableFailure("engineering_subject_file_required")
    codes = secure_read_subject_file(
        Path(subject_value),
        _engineering_excluded_roots(runtime),
    )
    if "KUNJIN_DATA_DIR" in os.environ or "KUNJIN_STATE_DIR" in os.environ:
        raise StableFailure("engineering_runtime_override_prohibited")
    source = Path.home() / ".local" / "share" / "kunjin" / "kunjin.db"
    data_dir = runtime / "data"
    state_dir = runtime / "state"
    data_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    target = data_dir / "kunjin.db"
    with TrackedChildren() as children:
        with ReadOnlyDatabaseGuard(source, target):
            os.environ["KUNJIN_DATA_DIR"] = str(data_dir)
            os.environ["KUNJIN_STATE_DIR"] = str(state_dir)
            try:
                cli, context = _build_engineering_context()
                children.install_tracking()
                result = orchestrate(
                    codes,
                    ENGINEERING_ROLES,
                    lambda argv: _engineering_cli_result(cli, context, argv),
                )
                shortlist_result = _engineering_cli_result(
                    cli,
                    context,
                    ["--json", "fund", "shortlist", *codes],
                )
                shortlist_data = _command_data(
                    shortlist_result,
                    "fund.shortlist",
                    required=True,
                )
                coverage = validate_engineering_coverage(
                    result.initial_data,
                    shortlist_data,
                    result.final_data,
                )
            finally:
                children.restore_tracking()
                children.assert_waited()
                os.environ.pop("KUNJIN_DATA_DIR", None)
                os.environ.pop("KUNJIN_STATE_DIR", None)
    gaps = result.final_data.get("blocking_codes", [])
    if type(gaps) is not list:
        raise StableFailure("engineering_orchestration_invalid")
    safe_gaps = sorted(
        {
            item
            for item in gaps
            if type(item) is str and _SAFE_CODE.fullmatch(item) is not None
        }
    )
    summary = {
        "action_boundary": dict(ACTION_BOUNDARY),
        "action_state_counts": result.action_state_counts,
        "candidate_formation": {
            "status": "research_scope_only",
            "candidate_code_discovery": "not_implemented",
        },
        "coverage": coverage,
        "financial_interpretation": "prohibited",
        "financial_usability": "not_yet_testable",
        "gap_categories": safe_gaps,
        "mode": "engineering",
        "orchestration_outcome": result.outcome,
        "owner_candidate_state": "owner_candidates_unavailable",
        "refresh_action_calls": result.refresh_action_calls,
        "roles": list(ENGINEERING_ROLES),
        "source_status_calls": result.source_status_calls,
        "subject_role": "engineering_subject",
    }
    encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
    sanitize_output(
        encoded,
        private_paths=(runtime, Path(subject_value), source, target),
        private_values=codes,
    )
    check_runtime_permissions(runtime)
    return summary


def sanitize_output(
    text: str,
    *,
    private_paths: Iterable[Path] = (),
    private_values: Iterable[str] = (),
) -> str:
    if type(text) is not str or not text or "Traceback" in text:
        raise StableFailure("acceptance_output_invalid")
    for value in (*tuple(str(path) for path in private_paths), *tuple(private_values)):
        if value and value in text:
            raise StableFailure("acceptance_output_invalid")
    if re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", text):
        raise StableFailure("acceptance_output_invalid")
    if re.search(r"\b(recommended|best|winner|buy|sell|rank|score)\b", text, re.I):
        raise StableFailure("acceptance_output_invalid")
    if re.search(
        r"\b(monthly_net_income|emergency_reserve_months|profile_id|"
        r"keyed_fingerprint|target_weight)\b",
        text,
        re.I,
    ):
        raise StableFailure("acceptance_output_invalid")
    if re.search(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{42,}={0,2}(?![A-Za-z0-9_-])', text):
        raise StableFailure("acceptance_output_invalid")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        raise StableFailure("acceptance_output_invalid") from None
    if type(payload) is not dict:
        raise StableFailure("acceptance_output_invalid")
    boundary = payload.get("action_boundary")
    if boundary is not None and boundary != ACTION_BOUNDARY:
        raise StableFailure("acceptance_output_invalid")
    return text


def sanitize_test_output(text: str, *, private_paths: Iterable[Path] = ()) -> str:
    if type(text) is not str or not text or "Traceback" in text:
        raise StableFailure("acceptance_output_invalid")
    for path in private_paths:
        if str(path) in text:
            raise StableFailure("acceptance_output_invalid")
    if re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", text):
        raise StableFailure("acceptance_output_invalid")
    if re.search(
        r"\b(monthly_net_income|emergency_reserve_months|profile_id|"
        r"keyed_fingerprint|target_weight)\b",
        text,
        re.I,
    ):
        raise StableFailure("acceptance_output_invalid")
    if re.search(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{42,}={0,2}(?![A-Za-z0-9_-])', text):
        raise StableFailure("acceptance_output_invalid")
    return text


def _error_payload(code: str) -> str:
    stable = StableFailure(code).code
    return json.dumps(
        {"error_code": stable, "ok": False},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _success_payload(data: dict) -> str:
    value = dict(data)
    value["ok"] = True
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {
        "engineering",
        "owner",
        "emit-private",
        "emit-test",
        "clean-tests",
        "check-runtime",
    }:
        print(_error_payload("phase41_arguments_invalid"))
        return 64
    mode = args[0]
    try:
        runtime = _runtime_dir()
        if mode == "engineering":
            encoded = _success_payload(run_engineering_acceptance())
            subject = os.environ.get("KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE", "")
            sanitize_output(
                encoded,
                private_paths=(runtime, Path(subject) if subject else runtime),
            )
        elif mode == "owner":
            encoded = _success_payload(run_owner_acceptance())
            sanitize_output(encoded, private_paths=(runtime, _canonical_owner_database()))
        elif mode == "check-runtime":
            check_runtime_permissions(runtime)
            encoded = _success_payload({"mode": "check-runtime"})
        elif mode == "clean-tests":
            test_root = runtime / "pytest"
            if test_root.is_symlink():
                test_root.unlink()
            elif test_root.exists():
                shutil.rmtree(test_root)
            encoded = _success_payload({"mode": "clean-tests"})
        else:
            capture = os.environ.get("KUNJIN_PHASE41_CAPTURE_FILE")
            if not capture:
                raise StableFailure("acceptance_output_invalid")
            capture_path = Path(capture)
            if not _under(capture_path.resolve(strict=True), runtime):
                raise StableFailure("acceptance_output_invalid")
            captured = capture_path.read_text(encoding="utf-8", errors="strict")
            encoded = (
                sanitize_output(captured, private_paths=(runtime,))
                if mode == "emit-private"
                else sanitize_test_output(captured, private_paths=(runtime,))
            )
        print(encoded)
        return 0
    except StableFailure as exc:
        print(_error_payload(exc.code))
        return 1
    except KeyboardInterrupt:
        print(_error_payload("phase41_interrupted"))
        return 130
    except SystemExit:
        print(_error_payload("phase41_interrupted"))
        return 130
    except BaseException:
        print(_error_payload("phase41_runtime_failed"))
        return 1


__all__ = [
    "ACTION_BOUNDARY",
    "CommandResult",
    "InMemoryKeyStore",
    "OrchestrationResult",
    "OwnerRuntimeGuards",
    "ReadOnlyDatabaseGuard",
    "StableFailure",
    "TrackedChildren",
    "assert_same_inode",
    "backup_sqlite_read_only",
    "load_owner_key_once",
    "main",
    "orchestrate",
    "sanitize_output",
    "sanitize_test_output",
    "secure_read_subject_file",
    "validate_engineering_coverage",
    "validate_owner_statuses",
]


if __name__ == "__main__":
    raise SystemExit(main())
