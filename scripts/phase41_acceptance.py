from __future__ import annotations

import hashlib
import json
import os
import pwd
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
from datetime import date, datetime
from itertools import combinations
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
_READINESS_SOURCE_FIELDS = (
    "adjusted_return_series",
    "current_manager_team",
    "fees_share_class_relationship",
    "formal_nav",
    "holdings_industries",
    "identity_active_status",
)
_SOURCE_RESOLUTIONS = frozenset(
    {"usable", "partial", "manual_supplement_required"}
)
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
class ActionTerminalRecord:
    role: str
    action_type: str
    state: str


@dataclass(frozen=True)
class OrchestrationResult:
    action_state_counts: Dict[str, int]
    action_terminal_records: Tuple[ActionTerminalRecord, ...]
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
        if resolution == "usable":
            continue
        if resolution != "partial":
            return True, frozenset()
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
    terminal_records = []
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
                state = "stopped_by_source_state"
                states[state] += 1
                terminal_records.append(
                    ActionTerminalRecord(action.role, action.action_type, state)
                )
                if action.dependency == "d1_documents":
                    failed_dependencies.add((action.role, "d1_documents"))
                continue
            if action.dependency == "d1_classification" and (
                action.role,
                "d1_documents",
            ) in failed_dependencies:
                state = "dependency_stopped"
                states[state] += 1
                terminal_records.append(
                    ActionTerminalRecord(action.role, action.action_type, state)
                )
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
            state = "completed" if succeeded else "terminal_failure"
            states[state] += 1
            terminal_records.append(
                ActionTerminalRecord(action.role, action.action_type, state)
            )
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
        action_terminal_records=tuple(terminal_records),
        final_data=final_data,
        final_readiness_calls=final_calls,
        initial_data=initial_data,
        initial_readiness_calls=initial_calls,
        outcome=outcome,
        refresh_action_calls=action_calls,
        source_status_calls=source_calls,
    )


def _readiness_request_codes(
    data: dict,
    *,
    expected_count: Optional[int],
    failure_code: str,
) -> Tuple[str, ...]:
    request = data.get("request")
    if type(request) is not dict or set(request) != {
        "candidate_codes",
        "candidate_count",
    }:
        raise StableFailure(failure_code)
    codes = request.get("candidate_codes")
    count = request.get("candidate_count")
    if (
        type(codes) is not list
        or type(count) is not int
        or count != len(codes)
        or not 2 <= count <= MAX_PUBLIC_SOURCE_STATUS_CALLS
        or (expected_count is not None and count != expected_count)
        or any(
            type(code) is not str
            or _CODE.fullmatch(code) is None
            or code == "0" * 6
            for code in codes
        )
        or len(set(codes)) != len(codes)
    ):
        raise StableFailure(failure_code)
    return tuple(codes)


def _candidate_rows(
    data: dict,
    codes: Sequence[str],
    *,
    failure_code: str,
) -> list:
    rows = data.get("candidate_evidence")
    if (
        type(rows) is not list
        or len(rows) != len(codes)
        or any(type(row) is not dict for row in rows)
        or tuple(row.get("fund_code") for row in rows) != tuple(codes)
    ):
        raise StableFailure(failure_code)
    return rows


def _safe_sorted_codes(value: object, *, failure_code: str) -> list:
    if (
        type(value) is not list
        or any(
            type(item) is not str or _SAFE_CODE.fullmatch(item) is None
            for item in value
        )
        or value != sorted(set(value))
    ):
        raise StableFailure(failure_code)
    return value


def validate_engineering_flow(
    result: OrchestrationResult,
    *,
    expected_subject_count: int,
) -> dict[str, str]:
    if (
        type(result) is not OrchestrationResult
        or type(result.initial_data) is not dict
        or type(result.final_data) is not dict
    ):
        raise StableFailure("engineering_flow_invalid")
    initial_codes = _readiness_request_codes(
        result.initial_data,
        expected_count=expected_subject_count,
        failure_code="engineering_flow_invalid",
    )
    final_codes = _readiness_request_codes(
        result.final_data,
        expected_count=expected_subject_count,
        failure_code="engineering_flow_invalid",
    )
    if final_codes != initial_codes:
        raise StableFailure("engineering_flow_invalid")
    _candidate_rows(
        result.initial_data,
        initial_codes,
        failure_code="engineering_flow_invalid",
    )
    _candidate_rows(
        result.final_data,
        final_codes,
        failure_code="engineering_flow_invalid",
    )
    _safe_sorted_codes(
        result.final_data.get("blocking_codes"),
        failure_code="engineering_flow_invalid",
    )
    actions = result.initial_data.get("bounded_refresh_actions")
    states = result.action_state_counts
    allowed_states = {
        "completed",
        "terminal_failure",
        "stopped_by_source_state",
        "dependency_stopped",
    }
    if (
        type(expected_subject_count) is not int
        or not 2 <= expected_subject_count <= MAX_PUBLIC_SOURCE_STATUS_CALLS
        or type(actions) is not list
        or len(actions) > expected_subject_count * len(_ACTION_SPECS)
        or type(states) is not dict
        or any(
            key not in allowed_states
            or type(value) is not int
            or value < 0
            for key, value in states.items()
        )
        or sum(states.values()) != len(actions)
        or type(result.refresh_action_calls) is not int
        or result.refresh_action_calls < 0
        or states.get("completed", 0) + states.get("terminal_failure", 0)
        != result.refresh_action_calls
        or type(result.initial_readiness_calls) is not int
        or result.initial_readiness_calls != 1
        or type(result.final_readiness_calls) is not int
        or result.final_readiness_calls != 1
        or type(result.source_status_calls) is not int
        or result.source_status_calls != expected_subject_count
        or type(result.outcome) is not str
        or type(result.final_data.get("comparison_evidence_ready")) is not bool
    ):
        raise StableFailure("engineering_flow_invalid")
    try:
        parsed_actions = _parse_actions(
            result.initial_data,
            initial_codes,
            tuple(
                f"engineering_subject_{index}"
                for index in range(1, len(initial_codes) + 1)
            ),
        )
    except StableFailure:
        raise StableFailure("engineering_flow_invalid") from None
    if len(parsed_actions) != len(actions):
        raise StableFailure("engineering_flow_invalid")
    records = result.action_terminal_records
    if type(records) is not tuple or len(records) != len(parsed_actions):
        raise StableFailure("engineering_flow_invalid")
    record_states = Counter()
    state_by_action = {}
    for action, record in zip(parsed_actions, records):
        if (
            type(record) is not ActionTerminalRecord
            or set(vars(record)) != {"role", "action_type", "state"}
            or record.role != action.role
            or record.action_type != action.action_type
            or record.state not in allowed_states
        ):
            raise StableFailure("engineering_flow_invalid")
        record_states[record.state] += 1
        state_by_action[(record.role, record.action_type)] = record.state
    if dict(sorted(record_states.items())) != dict(sorted(states.items())):
        raise StableFailure("engineering_flow_invalid")
    for record in records:
        document_state = state_by_action.get((record.role, "sync_fund_documents"))
        if record.action_type != "fund_classify":
            if record.state == "dependency_stopped":
                raise StableFailure("engineering_flow_invalid")
            continue
        if record.state == "dependency_stopped" and document_state != "terminal_failure":
            raise StableFailure("engineering_flow_invalid")
        if document_state == "terminal_failure" and record.state != "dependency_stopped":
            raise StableFailure("engineering_flow_invalid")
        if (
            document_state == "stopped_by_source_state"
            and record.state != "stopped_by_source_state"
        ):
            raise StableFailure("engineering_flow_invalid")
        if document_state == "completed" and record.state in {
            "dependency_stopped",
            "stopped_by_source_state",
        }:
            raise StableFailure("engineering_flow_invalid")
    if states.get("stopped_by_source_state", 0):
        expected_outcome = "stopped_by_source_state"
    elif result.final_data["comparison_evidence_ready"] is True:
        expected_outcome = "completed_once"
    elif result.refresh_action_calls:
        expected_outcome = "partial_once"
    else:
        expected_outcome = "not_run"
    if result.outcome != expected_outcome:
        raise StableFailure("engineering_flow_invalid")
    return {"engineering_flow": "pass"}


def _is_timestamp(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_safe_optional_code(value: object) -> bool:
    return value is None or (
        type(value) is str and _SAFE_CODE.fullmatch(value) is not None
    )


def _validate_source_component(source: object) -> int:
    if type(source) is not dict:
        raise StableFailure("engineering_evidence_invalid")
    if set(source) == {"technical_failure"}:
        if source["technical_failure"] != "source_status_load_failed":
            raise StableFailure("engineering_evidence_invalid")
        return 0
    if set(source) != {"evaluated_at", "fields"} or not _is_timestamp(
        source["evaluated_at"]
    ):
        raise StableFailure("engineering_evidence_invalid")
    fields = source["fields"]
    if type(fields) is not list or len(fields) != len(_READINESS_SOURCE_FIELDS):
        raise StableFailure("engineering_evidence_invalid")
    field_ids = []
    usable = 0
    for field in fields:
        if type(field) is not dict or set(field) != {
            "acceptable_sources",
            "field_id",
            "resolution",
        }:
            raise StableFailure("engineering_evidence_invalid")
        if type(field["acceptable_sources"]) is not list:
            raise StableFailure("engineering_evidence_invalid")
        field_id = field["field_id"]
        resolution = field["resolution"]
        if (
            type(field_id) is not str
            or type(resolution) is not str
            or resolution not in _SOURCE_RESOLUTIONS
        ):
            raise StableFailure("engineering_evidence_invalid")
        field_ids.append(field_id)
        usable += resolution == "usable"
    if tuple(field_ids) != _READINESS_SOURCE_FIELDS:
        raise StableFailure("engineering_evidence_invalid")
    return usable


def _validate_profile_component(profile: object) -> int:
    if type(profile) is not dict:
        raise StableFailure("engineering_evidence_invalid")
    if set(profile) == {"technical_failure"}:
        if profile["technical_failure"] != "profile_load_failed":
            raise StableFailure("engineering_evidence_invalid")
        return 0
    if set(profile) != {
        "authenticated",
        "benchmarks",
        "conflicts",
        "fees",
        "freshness",
        "identity",
        "managers",
        "missing_sections",
        "publication_dates",
        "report_dates",
        "warnings",
    }:
        raise StableFailure("engineering_evidence_invalid")
    if (
        type(profile["authenticated"]) is not bool
        or type(profile["benchmarks"]) is not dict
        or type(profile["conflicts"]) is not list
        or type(profile["fees"]) is not dict
        or type(profile["freshness"]) is not dict
        or (profile["identity"] is not None and type(profile["identity"]) is not dict)
        or type(profile["managers"]) is not dict
        or type(profile["missing_sections"]) is not dict
        or type(profile["publication_dates"]) is not list
        or type(profile["report_dates"]) is not list
        or type(profile["warnings"]) is not list
    ):
        raise StableFailure("engineering_evidence_invalid")
    return int(profile["authenticated"])


def _validate_nav_component(nav: object) -> int:
    if type(nav) is not dict or set(nav) != {
        "end_date",
        "future_observation_count",
        "latest_date",
        "observation_count",
        "start_date",
        "technical_failure",
        "unique_date_count",
        "usable",
    }:
        raise StableFailure("engineering_evidence_invalid")
    for name in ("future_observation_count", "observation_count", "unique_date_count"):
        if type(nav[name]) is not int or nav[name] < 0:
            raise StableFailure("engineering_evidence_invalid")
    parsed_dates = {}
    for name in ("end_date", "latest_date", "start_date"):
        value = nav[name]
        if value is None:
            parsed_dates[name] = None
            continue
        if type(value) is not str:
            raise StableFailure("engineering_evidence_invalid")
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise StableFailure("engineering_evidence_invalid") from None
        if parsed.isoformat() != value:
            raise StableFailure("engineering_evidence_invalid")
        parsed_dates[name] = parsed
    if (
        (
            nav["technical_failure"] is not None
            and nav["technical_failure"] != "formal_nav_load_failed"
        )
        or type(nav["usable"]) is not bool
        or nav["unique_date_count"] > nav["observation_count"]
    ):
        raise StableFailure("engineering_evidence_invalid")
    if nav["usable"] and (
        nav["technical_failure"] is not None
        or nav["future_observation_count"] != 0
        or nav["observation_count"] < 2
        or nav["unique_date_count"] < 2
        or any(value is None for value in parsed_dates.values())
        or parsed_dates["start_date"] >= parsed_dates["end_date"]
        or parsed_dates["latest_date"] != parsed_dates["end_date"]
    ):
        raise StableFailure("engineering_evidence_invalid")
    return int(nav["usable"])


def _validate_holdings_component(holdings: object) -> int:
    if type(holdings) is not dict:
        raise StableFailure("engineering_evidence_invalid")
    if set(holdings) == {"technical_failure"}:
        if holdings["technical_failure"] != "holdings_load_failed":
            raise StableFailure("engineering_evidence_invalid")
        return 0
    if set(holdings) != {
        "conflicts",
        "disclosed_coverage",
        "disclosure_scopes",
        "evidence_level",
        "freshness",
        "published_at",
        "report_period",
        "source_document_ids",
        "warnings",
    }:
        raise StableFailure("engineering_evidence_invalid")
    if (
        type(holdings["conflicts"]) is not list
        or type(holdings["disclosed_coverage"]) is not str
        or type(holdings["disclosure_scopes"]) is not list
        or type(holdings["evidence_level"]) is not str
        or type(holdings["freshness"]) is not str
        or (
            holdings["published_at"] is not None
            and type(holdings["published_at"]) is not str
        )
        or (
            holdings["report_period"] is not None
            and type(holdings["report_period"]) is not str
        )
        or type(holdings["source_document_ids"]) is not list
        or any(
            type(item) is not int or item <= 0
            for item in holdings["source_document_ids"]
        )
        or type(holdings["warnings"]) is not list
    ):
        raise StableFailure("engineering_evidence_invalid")
    return int(
        holdings["evidence_level"] == "verified_fact"
        and bool(holdings["source_document_ids"])
    )


def _validate_d1_component(d1: object) -> int:
    if type(d1) is not dict:
        raise StableFailure("engineering_evidence_invalid")
    if d1.get("classification_present") is False:
        failure = d1.get("technical_failure")
        if set(d1) != {"classification_present", "technical_failure"} or (
            failure is not None and failure != "classification_load_failed"
        ):
            raise StableFailure("engineering_evidence_invalid")
        return 0
    if set(d1) != {
        "classification_policy_checksum",
        "classification_present",
        "classified_at",
        "conflicts",
        "evidence_status",
        "freshness",
        "mapped_asset_layer",
        "mapping_reason_code",
        "missing_evidence",
        "policy_version",
        "portfolio_role",
        "reason_codes",
        "risk_bucket",
        "valid_until",
    }:
        raise StableFailure("engineering_evidence_invalid")
    if (
        d1["classification_present"] is not True
        or type(d1["classification_policy_checksum"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", d1["classification_policy_checksum"]) is None
        or not _is_timestamp(d1["classified_at"])
        or type(d1["conflicts"]) is not list
        or type(d1["evidence_status"]) is not str
        or d1["evidence_status"]
        not in {"verified", "partial", "conflicted", "stale", "unclassified"}
        or type(d1["freshness"]) is not str
        or d1["freshness"] not in {"current", "stale"}
        or not _is_safe_optional_code(d1["mapped_asset_layer"])
        or (
            d1["mapped_asset_layer"] is not None
            and d1["mapped_asset_layer"]
            not in {"high_quality_fixed_income", "diversified_equity"}
        )
        or not _is_safe_optional_code(d1["mapping_reason_code"])
        or type(d1["missing_evidence"]) is not list
        or type(d1["policy_version"]) is not str
        or not d1["policy_version"]
        or type(d1["portfolio_role"]) is not str
        or d1["portfolio_role"]
        not in {
            "cash_management_candidate",
            "core_eligible",
            "active_diversifier_eligible",
            "satellite_only",
            "not_eligible",
        }
        or type(d1["reason_codes"]) is not list
        or type(d1["risk_bucket"]) is not str
        or d1["risk_bucket"]
        not in {
            "cash_like_candidate",
            "high_quality_fixed_income",
            "diversified_equity",
            "concentrated_equity",
            "hybrid_risk",
            "unclassified",
        }
        or not _is_timestamp(d1["valid_until"])
    ):
        raise StableFailure("engineering_evidence_invalid")
    return 1


def _validate_binding_component(binding: object) -> Tuple[int, str]:
    if type(binding) is not dict or set(binding) != {
        "position_state",
        "technical_failure",
    }:
        raise StableFailure("engineering_evidence_invalid")
    state = binding["position_state"]
    failure = binding["technical_failure"]
    if (
        type(state) is not str
        or state not in {"held", "not_held"}
        or (failure is not None and failure != "portfolio_binding_load_failed")
    ):
        raise StableFailure("engineering_evidence_invalid")
    return int(failure is None), state


def _validate_shortlist_entry(entry: object, *, position_state: str) -> None:
    if type(entry) is not dict or set(entry) != {
        "d1_conflict_free",
        "d1_current",
        "d1_evidence_verified",
        "mapped_asset_layer",
        "personal_gate_passes",
        "portfolio_role_eligible",
        "position_state",
    }:
        raise StableFailure("engineering_evidence_invalid")
    for name in (
        "d1_conflict_free",
        "d1_current",
        "d1_evidence_verified",
        "personal_gate_passes",
        "portfolio_role_eligible",
    ):
        if type(entry[name]) is not bool:
            raise StableFailure("engineering_evidence_invalid")
    if (
        not _is_safe_optional_code(entry["mapped_asset_layer"])
        or (
            entry["mapped_asset_layer"] is not None
            and entry["mapped_asset_layer"]
            not in {"high_quality_fixed_income", "diversified_equity"}
        )
        or entry["position_state"] != position_state
    ):
        raise StableFailure("engineering_evidence_invalid")


def _usable_component_projection(
    candidate_evidence: object,
    codes: Sequence[str],
) -> Tuple[int, Tuple[str, ...]]:
    if type(candidate_evidence) is not list or len(candidate_evidence) != len(codes):
        raise StableFailure("engineering_evidence_invalid")
    count = 0
    position_states = []
    expected_keys = {
        "fund_code",
        "source_health",
        "profile",
        "formal_nav",
        "holdings",
        "d1",
        "portfolio_binding",
        "shortlist_entry",
    }
    for code, candidate in zip(codes, candidate_evidence):
        if (
            type(candidate) is not dict
            or set(candidate) != expected_keys
            or candidate["fund_code"] != code
        ):
            raise StableFailure("engineering_evidence_invalid")
        count += _validate_source_component(candidate["source_health"])
        count += _validate_profile_component(candidate["profile"])
        count += _validate_nav_component(candidate["formal_nav"])
        count += _validate_holdings_component(candidate["holdings"])
        count += _validate_d1_component(candidate["d1"])
        binding_count, position_state = _validate_binding_component(
            candidate["portfolio_binding"]
        )
        count += binding_count
        position_states.append(position_state)
        _validate_shortlist_entry(
            candidate["shortlist_entry"],
            position_state=position_state,
        )
    return count, tuple(position_states)


def project_engineering_evidence(
    result: OrchestrationResult,
    shortlist: dict,
) -> dict[str, object]:
    if (
        type(result) is not OrchestrationResult
        or type(result.final_data) is not dict
        or type(shortlist) is not dict
    ):
        raise StableFailure("engineering_evidence_invalid")
    readiness_codes = _readiness_request_codes(
        result.final_data,
        expected_count=None,
        failure_code="engineering_evidence_invalid",
    )
    comparison_ready = result.final_data.get("comparison_evidence_ready")
    if type(comparison_ready) is not bool:
        raise StableFailure("engineering_evidence_invalid")
    usable_component_count, binding_position_states = _usable_component_projection(
        result.final_data.get("candidate_evidence"),
        readiness_codes,
    )
    evidence_state = (
        "ready"
        if comparison_ready
        else "partial"
        if usable_component_count
        else "insufficient_data"
    )
    comparison_state = "ready" if comparison_ready else "insufficient_data"
    pairs = shortlist.get("comparability")
    reviews = shortlist.get("candidate_reviews")
    shortlist_codes = _readiness_request_codes(
        shortlist,
        expected_count=len(readiness_codes),
        failure_code="engineering_evidence_invalid",
    )
    if type(pairs) is not list or type(reviews) is not list:
        raise StableFailure("engineering_evidence_invalid")
    if shortlist_codes != readiness_codes or (
        len(reviews) != len(readiness_codes)
        or any(type(review) is not dict for review in reviews)
        or tuple(review.get("fund_code") for review in reviews) != readiness_codes
    ):
        raise StableFailure("engineering_evidence_invalid")
    review_position_states = tuple(review.get("position_state") for review in reviews)
    if review_position_states != binding_position_states:
        raise StableFailure("engineering_evidence_invalid")
    expected_pairs = tuple(combinations(readiness_codes, 2))
    if len(pairs) != len(expected_pairs):
        raise StableFailure("engineering_evidence_invalid")
    counts = Counter()
    reasons = set()
    observed = False
    structural_reasons = {
        "type_mismatch",
        "management_style_mismatch",
        "benchmark_mismatch",
    }
    pair_reasons = {
        "comparable": {"classification_match"},
        "not_comparable": structural_reasons | {"inactive_fund"},
        "insufficient_data": {
            "identity_conflict",
            "missing_disclosure_bundle",
            "missing_identity",
            "peer_classification_ambiguous",
            "peer_classification_unavailable",
        },
    }
    for expected_pair, pair in zip(expected_pairs, pairs):
        if type(pair) is not dict or set(pair) != {
            "left_fund_code",
            "right_fund_code",
            "state",
            "reason_code",
            "warning_codes",
        }:
            raise StableFailure("engineering_evidence_invalid")
        if (
            (pair["left_fund_code"], pair["right_fund_code"]) != expected_pair
            or type(pair["warning_codes"]) is not list
            or any(
                type(item) is not str or _SAFE_CODE.fullmatch(item) is None
                for item in pair["warning_codes"]
            )
            or pair["warning_codes"] != sorted(set(pair["warning_codes"]))
        ):
            raise StableFailure("engineering_evidence_invalid")
        state = pair.get("state")
        reason = pair.get("reason_code")
        if (
            type(state) is not str
            or state not in {"comparable", "not_comparable", "insufficient_data"}
            or type(reason) is not str
            or _SAFE_CODE.fullmatch(reason) is None
            or reason not in pair_reasons.get(state, set())
        ):
            raise StableFailure("engineering_evidence_invalid")
        counts[state] += 1
        reasons.add(reason)
        observed = observed or state == "comparable" or (
            state == "not_comparable" and reason in structural_reasons
        )
    if any(state not in {"held", "not_held"} for state in review_position_states):
        raise StableFailure("engineering_evidence_invalid")
    held_binding_observed = any(
        state == "held" for state in review_position_states
    )
    structural_state = "observed" if observed else "not_testable"
    return {
        "evidence_readiness": evidence_state,
        "comparison_evidence_readiness": comparison_state,
        "structural_comparability": structural_state,
        "usable_component_count": usable_component_count,
        "held_binding_observed": held_binding_observed,
        "comparison_state_counts": {
            state: counts.get(state, 0)
            for state in ("comparable", "not_comparable", "insufficient_data")
        },
        "comparison_reason_codes": sorted(reasons),
    }


def _codes(value: object) -> list:
    if type(value) is not list or any(
        type(item) is not str or _SAFE_CODE.fullmatch(item) is None for item in value
    ):
        raise StableFailure("owner_status_invalid")
    if len(set(value)) != len(value):
        raise StableFailure("owner_status_invalid")
    return sorted(value)


def _exact_keys(value: dict, expected: set) -> None:
    if type(value) is not dict or set(value) != expected:
        raise StableFailure("owner_status_invalid")


def _positive_integer(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise StableFailure("owner_status_invalid")


def _nonempty_text(value: object) -> None:
    if type(value) is not str or not value:
        raise StableFailure("owner_status_invalid")


def _aware_timestamp(value: object) -> None:
    _nonempty_text(value)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise StableFailure("owner_status_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StableFailure("owner_status_invalid")


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
    if type(profile) is not dict:
        raise StableFailure("owner_status_invalid")
    profile_state = profile.get("state")
    profile_freshness = profile.get("freshness")
    if profile_state == "missing":
        _exact_keys(profile, {"state", "freshness"})
        if profile_freshness != "missing":
            raise StableFailure("owner_status_invalid")
    elif profile_state == "confirmed":
        _exact_keys(
            profile,
            {"state", "version", "confirmed_at", "valid_until", "freshness"},
        )
        if profile_freshness not in {"fresh", "stale"}:
            raise StableFailure("owner_status_invalid")
        _positive_integer(profile["version"])
        _aware_timestamp(profile["confirmed_at"])
        _aware_timestamp(profile["valid_until"])
    else:
        raise StableFailure("owner_status_invalid")
    b_state, b_freshness = _state_pair(suitability, {"fresh", "stale", "missing"})
    b_status = suitability.get("status")
    minimal_nonfresh_keys = {"state", "freshness", "capability"}
    if b_state == "missing" or (
        b_state == "stale" and set(suitability) == minimal_nonfresh_keys
    ):
        _exact_keys(suitability, {"state", "freshness", "capability"})
        if suitability["capability"] != "research_only":
            raise StableFailure("owner_status_invalid")
        if b_status is not None:
            raise StableFailure("owner_status_invalid")
        hard_blocks = []
        constraints = []
    else:
        _exact_keys(
            suitability,
            {
                "state",
                "freshness",
                "assessment_id",
                "profile_version_id",
                "policy_version",
                "status",
                "hard_blocks",
                "constraints",
                "assessed_at",
                "valid_until",
                "capability",
            },
        )
        if b_status not in {"blocked", "constrained", "ready_for_allocation"}:
            raise StableFailure("owner_status_invalid")
        if suitability["capability"] != "research_only":
            raise StableFailure("owner_status_invalid")
        _positive_integer(suitability["assessment_id"])
        _positive_integer(suitability["profile_version_id"])
        _nonempty_text(suitability["policy_version"])
        _aware_timestamp(suitability["assessed_at"])
        _aware_timestamp(suitability["valid_until"])
        hard_blocks = _codes(suitability.get("hard_blocks"))
        constraints = _codes(suitability.get("constraints"))
    c_state, c_freshness = _state_pair(allocation, {"fresh", "stale", "missing"})
    c_status = allocation.get("status")
    if c_state == "missing":
        _exact_keys(allocation, {"state", "freshness", "capability"})
        if allocation["capability"] != "research_only":
            raise StableFailure("owner_status_invalid")
        if c_status is not None:
            raise StableFailure("owner_status_invalid")
        binding = []
    else:
        _exact_keys(
            allocation,
            {
                "state",
                "freshness",
                "assessment_id",
                "profile_version_id",
                "suitability_assessment_id",
                "policy_version",
                "status",
                "binding_constraints",
                "safe_summary",
                "permitted_region",
                "assessed_at",
                "valid_until",
                "capability",
            },
        )
        if c_status not in {"blocked", "range_available"}:
            raise StableFailure("owner_status_invalid")
        if allocation["capability"] != "research_only":
            raise StableFailure("owner_status_invalid")
        for key in ("assessment_id", "profile_version_id", "suitability_assessment_id"):
            _positive_integer(allocation[key])
        _nonempty_text(allocation["policy_version"])
        _aware_timestamp(allocation["assessed_at"])
        _aware_timestamp(allocation["valid_until"])
        if type(allocation["safe_summary"]) is not dict or type(
            allocation["permitted_region"]
        ) is not dict:
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
    from kunjin.suitability.crypto import ProfileCryptoError, ProfileKeyStore

    class ReadOnlyTokenStore:
        def __init__(self) -> None:
            self.load_calls = 0

        def load(self):
            self.load_calls += 1
            if self.load_calls != 1:
                raise OSError("repeated Keychain read is prohibited")
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
                raise OSError("Keychain read failed") from None
            if returncode == 44:
                return None
            if returncode != 0 or type(stdout) is not str:
                raise OSError("Keychain read failed")
            return stdout.strip() or None

        def save(self, _value):
            raise OSError("Keychain write is prohibited")

        def delete(self):
            raise OSError("Keychain write is prohibited")

    token_store = ReadOnlyTokenStore()
    try:
        key = ProfileKeyStore(token_store=token_store).load_existing_key()
    except (KeyboardInterrupt, SystemExit):
        raise
    except (ProfileCryptoError, OSError, TypeError, ValueError):
        raise StableFailure("owner_keychain_unavailable") from None
    if token_store.load_calls != 1 or type(key) is not bytes or len(key) != 32:
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
    return _canonical_home() / ".local" / "share" / "kunjin" / "kunjin.db"


def _canonical_home() -> Path:
    try:
        value = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        raise StableFailure("phase41_runtime_invalid") from None
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise StableFailure("phase41_runtime_invalid")
    return Path(value)


def _validate_cli_origin(cli) -> None:
    expected = Path(__file__).resolve().parents[1] / "src" / "kunjin" / "cli.py"
    actual_value = getattr(cli, "__file__", None)
    if type(actual_value) is not str:
        raise StableFailure("phase41_import_origin_invalid")
    try:
        actual = Path(actual_value).resolve(strict=True)
    except (OSError, RuntimeError):
        raise StableFailure("phase41_import_origin_invalid") from None
    if actual != expected.resolve(strict=True):
        raise StableFailure("phase41_import_origin_invalid")


def _build_context_with_key(key: bytes):
    import kunjin.cli as cli

    _validate_cli_origin(cli)
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

    _validate_cli_origin(cli)
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
    home = _canonical_home()
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
    source = _canonical_home() / ".local" / "share" / "kunjin" / "kunjin.db"
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
                flow = validate_engineering_flow(
                    result,
                    expected_subject_count=len(ENGINEERING_ROLES),
                )
                evidence = project_engineering_evidence(result, shortlist_data)
            finally:
                children.restore_tracking()
                children.assert_waited()
                os.environ.pop("KUNJIN_DATA_DIR", None)
                os.environ.pop("KUNJIN_STATE_DIR", None)
    safe_gaps = list(result.final_data["blocking_codes"])
    summary = {
        **flow,
        **evidence,
        "action_boundary": dict(ACTION_BOUNDARY),
        "action_state_counts": result.action_state_counts,
        "candidate_formation": {
            "status": "research_scope_only",
            "candidate_code_discovery": "not_implemented",
        },
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
    "ActionTerminalRecord",
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
    "project_engineering_evidence",
    "sanitize_output",
    "sanitize_test_output",
    "secure_read_subject_file",
    "validate_engineering_flow",
    "validate_owner_statuses",
]


if __name__ == "__main__":
    raise SystemExit(main())
