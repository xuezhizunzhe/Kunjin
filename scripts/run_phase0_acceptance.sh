#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

if [[ $# -ne 2 ]]; then
    printf 'usage: %s CODE OUTPUT_DIR\n' "$0" >&2
    exit 64
fi

readonly CODE="$1"
readonly REQUESTED_OUTPUT_DIR="$2"
if [[ ! "${CODE}" =~ ^[0-9]{6}$ ]]; then
    printf 'CODE must contain exactly six digits\n' >&2
    exit 65
fi
if [[ "${REQUESTED_OUTPUT_DIR}" != /* \
   || "${REQUESTED_OUTPUT_DIR}" == *$'\n'* \
   || "${REQUESTED_OUTPUT_DIR}" == */ \
   || "${REQUESTED_OUTPUT_DIR##*/}" == "." \
   || "${REQUESTED_OUTPUT_DIR##*/}" == ".." ]]; then
    printf 'OUTPUT_DIR must be a safe absolute path\n' >&2
    exit 65
fi
if [[ -e "${REQUESTED_OUTPUT_DIR}" || -L "${REQUESTED_OUTPUT_DIR}" ]]; then
    printf 'OUTPUT_DIR must not already exist or be a symbolic link\n' >&2
    exit 66
fi

readonly OUTPUT_PARENT_LEXICAL="${REQUESTED_OUTPUT_DIR%/*}"
readonly OUTPUT_BASENAME="${REQUESTED_OUTPUT_DIR##*/}"
if [[ ! -d "${OUTPUT_PARENT_LEXICAL}" ]]; then
    printf 'OUTPUT_DIR parent must already exist\n' >&2
    exit 66
fi
readonly OUTPUT_PARENT="$(cd -P "${OUTPUT_PARENT_LEXICAL}" && pwd -P)"
if [[ -e "${OUTPUT_PARENT}/${OUTPUT_BASENAME}" \
   || -L "${OUTPUT_PARENT}/${OUTPUT_BASENAME}" ]]; then
    printf 'physical OUTPUT_DIR must not already exist or be a symbolic link\n' >&2
    exit 66
fi

SCRIPT_SOURCE="${BASH_SOURCE[0]}"
if [[ "${SCRIPT_SOURCE}" != */* || "${SCRIPT_SOURCE}" == *$'\n'* ]]; then
    printf 'acceptance script must be invoked by an explicit path\n' >&2
    exit 64
fi
if [[ "${SCRIPT_SOURCE}" != /* ]]; then
    SCRIPT_SOURCE="${PWD}/${SCRIPT_SOURCE}"
fi
if [[ -L "${SCRIPT_SOURCE}" ]]; then
    printf 'acceptance script symlink invocation is rejected\n' >&2
    exit 66
fi
readonly SCRIPT_DIRECTORY="$(cd -P "${SCRIPT_SOURCE%/*}" && pwd -P)"
readonly ROOT_DIR="$(cd -P "${SCRIPT_DIRECTORY}/.." && pwd -P)"
readonly CLI="${ROOT_DIR}/.venv/bin/kunjin"
readonly PYTHON="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${CLI}" || ! -x "${PYTHON}" ]]; then
    printf 'repository virtual environment is unavailable\n' >&2
    exit 69
fi

# The override can only shorten the production 90-second global acceptance budget.
readonly ACCEPTANCE_TIMEOUT_SECONDS="${KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS:-90}"
if [[ ! "${ACCEPTANCE_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ \
   || "${ACCEPTANCE_TIMEOUT_SECONDS}" -gt 90 ]]; then
    printf 'Phase 0 acceptance timeout must be an integer from 1 through 90 seconds\n' >&2
    exit 65
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase0-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
cleanup() {
    /bin/rm -rf "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
/bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
/bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"

set +e
"${PYTHON}" - \
    "${ACCEPTANCE_TIMEOUT_SECONDS}" \
    "${CLI}" \
    "${CODE}" \
    "${RUNTIME_DIR}" \
    "${OUTPUT_PARENT}" \
    "${OUTPUT_BASENAME}" \
    2> "${RUNTIME_DIR}/driver.stderr" <<'PY'
import ctypes
import errno
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path


(
    timeout_text,
    cli,
    code,
    runtime_path,
    output_parent_path,
    output_basename,
) = sys.argv[1:]
timeout_seconds = int(timeout_text)
runtime = Path(runtime_path)

started = time.monotonic()
started_epoch_seconds = int(time.time())
run_identity = secrets.token_hex(16)
hard_deadline = started + timeout_seconds
publish_deadline = hard_deadline - min(0.25, timeout_seconds / 4)
command_deadline = publish_deadline - min(0.25, timeout_seconds / 4)

REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
VERSION = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
FUND_CODE = re.compile(r"^[0-9]{6}$")
ENVELOPE_KEYS = {
    "schema_version",
    "command",
    "as_of",
    "data",
    "warnings",
    "errors",
}
SOURCE_DATA_KEYS = {
    "fund_code",
    "mode",
    "policy_checksum",
    "policy_version",
    "registry_checksum",
    "registry_version",
    "request_field_resolutions",
    "request_id",
    "snapshot_at",
    "source_fields",
}
SOURCE_FIELD_KEYS = {
    "acceptable_alternatives",
    "consecutive_failures",
    "cooldown_until",
    "field_id",
    "field_scope",
    "last_failure_at",
    "last_failure_reason",
    "last_success_at",
    "last_success_data_as_of",
    "source_id",
    "source_kind",
    "source_scope",
    "source_tier",
    "state",
    "supplementation",
}
SUPPLEMENTATION_KEYS = {
    "accepted_input",
    "freshness_requirement",
    "impact_if_missing",
    "missing_item",
    "suggested_location",
    "supported_without_it",
    "unsupported_without_it",
    "why_required",
}
RESOLUTION_KEYS = {
    "action",
    "field_id",
    "primary_source_id",
    "resolution",
    "risk_effect",
}
SYNC_DATA_KEYS = {
    "conflicts",
    "errors",
    "freshness",
    "fund_code",
    "request",
    "sections",
    "sources",
    "warnings",
}
SECTION_KEYS = {
    "as_of",
    "error_code",
    "freshness",
    "last_attempt_at",
    "last_success_at",
    "records",
    "section",
    "status",
}
REQUEST_KEYS = {
    "deadline_at",
    "mode",
    "omitted_work",
    "request_id",
    "terminal_status",
}
SOURCE_DOCUMENT_KEYS = {
    "document_kind",
    "id",
    "published_at",
    "publisher",
    "retrieved_at",
    "source_name",
    "source_tier",
    "title",
    "url",
}
FRESHNESS_KEYS = {"as_of", "sections"}
FRESHNESS_SECTION_KEYS = {
    "age_days",
    "last_attempted_at",
    "last_success_at",
    "state",
}
SYNC_ERROR_KEYS = {"code", "message", "section"}
ROUTE_DATA_KEYS = {
    "actions",
    "conclusion_evidence",
    "created_at",
    "missing_fields",
    "mode",
    "opposing_evidence",
    "policy_checksum",
    "policy_version",
    "registry_checksum",
    "registry_version",
    "request_id",
    "result_checksum",
    "workflow_level",
}
ACTION_KEYS = {
    "action",
    "action_id",
    "action_maturity",
    "blocking_codes",
    "exact_amount_available",
    "minimum_state",
    "required_gates",
    "research_available",
    "risk_effect",
}
CONCLUSION_KEYS = {
    "completeness",
    "conflicts",
    "coverage_percent",
    "freshness",
    "independent_lineage_count",
    "inferred",
    "lineage_ids",
    "market_as_of",
    "missing_critical_fields",
    "publication_times",
    "publishers",
    "report_as_of",
    "retrieved_at",
    "source_ids",
    "source_tier",
}
SOURCE_STATES = {
    "not_checked",
    "healthy",
    "degraded",
    "cooldown",
    "unavailable",
    "unsupported",
}
RESOLUTIONS = {"usable", "partial", "manual_supplement_required"}
SOURCE_TIERS = {"tier_1", "tier_2", "private_observation", "user_provided"}
ACTION_MATURITIES = {"mature", "experimental_shadow"}
ACTION_STATES = {"research_only", "no_add", "experimental_shadow", "actionable"}
RISK_EFFECTS = {"information", "risk_increasing"}
WORKFLOW_LEVELS = {"rapid_evidence"}
MAX_RAW_BYTES = 4 * 1024 * 1024
RENAME_EXCL = 0x00000004
PROC_PIDPATHINFO_MAXSIZE = 4096
PROC_PIDT_BSDINFOWITHUNIQID = 18
PROC_UID_ONLY = 4
CTL_KERN = 1
KERN_PROCARGS2 = 49
MAX_PROCARGS_BYTES = 1024 * 1024
RUN_ID_ENVIRONMENT = "KUNJIN_PHASE0_RUN_ID"
STABLE_SCAN_QUIET_SECONDS = 0.03


class ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ProcUniqueIdentifierInfo(ctypes.Structure):
    _fields_ = [
        ("p_uuid", ctypes.c_ubyte * 16),
        ("p_uniqueid", ctypes.c_uint64),
        ("p_puniqueid", ctypes.c_uint64),
        ("p_idversion", ctypes.c_int32),
        ("p_orig_ppidversion", ctypes.c_int32),
        ("p_reserve2", ctypes.c_uint64),
        ("p_reserve3", ctypes.c_uint64),
    ]


class ProcBSDInfoWithUniqueID(ctypes.Structure):
    _fields_ = [
        ("bsd", ProcBSDInfo),
        ("unique", ProcUniqueIdentifierInfo),
    ]


class ProcessIdentity:
    def __init__(self, metadata, executable_path):
        self.pid = metadata[0]
        self.process_group = metadata[1]
        self.uid = metadata[2]
        self.start_seconds = metadata[3]
        self.start_microseconds = metadata[4]
        self.executable_uuid = metadata[5]
        self.unique_id = metadata[6]
        self.parent_unique_id = metadata[7]
        self.id_version = metadata[8]
        self.executable_path = executable_path

    def stable_key(self):
        return (
            self.unique_id,
            self.parent_unique_id,
            self.uid,
            self.start_seconds,
            self.start_microseconds,
        )


libc = ctypes.CDLL(None, use_errno=True)
libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
proc_listchildpids = libproc.proc_listchildpids
proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
proc_listchildpids.restype = ctypes.c_int
proc_pidpath = libproc.proc_pidpath
proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
proc_pidpath.restype = ctypes.c_int
proc_pidinfo = libproc.proc_pidinfo
proc_pidinfo.argtypes = [
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_int,
]
proc_pidinfo.restype = ctypes.c_int
proc_listpids = libproc.proc_listpids
proc_listpids.argtypes = [
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_int,
]
proc_listpids.restype = ctypes.c_int
sysctl = libc.sysctl
sysctl.argtypes = [
    ctypes.POINTER(ctypes.c_int),
    ctypes.c_uint,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
]
sysctl.restype = ctypes.c_int


class AcceptanceFailure(Exception):
    pass


class AcceptanceDeadline(BaseException):
    pass


class AcceptanceInterrupted(BaseException):
    pass


class ProcessObservationTransient(Exception):
    pass


def on_deadline(_signal_number, _frame):
    raise AcceptanceDeadline()


def on_interrupt(_signal_number, _frame):
    raise AcceptanceInterrupted()


signal.signal(signal.SIGALRM, on_deadline)
for interrupt_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(interrupt_signal, on_interrupt)
signal.setitimer(signal.ITIMER_REAL, timeout_seconds)


def require_time(deadline, label):
    if time.monotonic() >= deadline:
        raise AcceptanceFailure(label + " exceeded the global acceptance budget")


def exact_dict(value, keys, label):
    if type(value) is not dict or set(value) != keys:
        raise AcceptanceFailure(label + " keys are invalid")
    return value


def exact_list(value, label):
    if type(value) is not list:
        raise AcceptanceFailure(label + " must be a list")
    return value


def identifier(value, label):
    if type(value) is not str or IDENTIFIER.fullmatch(value) is None:
        raise AcceptanceFailure(label + " is invalid")
    return value


def identifier_list(value, label):
    values = exact_list(value, label)
    projected = [identifier(item, label + " item") for item in values]
    if len(projected) != len(set(projected)):
        raise AcceptanceFailure(label + " contains duplicates")
    return projected


def public_text(value, label, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or not value or len(value) > 4096:
        raise AcceptanceFailure(label + " is invalid")
    if any(ord(character) < 32 and character not in "\t" for character in value):
        raise AcceptanceFailure(label + " contains control characters")
    return value


def utc_text(value, label, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or not value.endswith("+00:00"):
        raise AcceptanceFailure(label + " is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AcceptanceFailure(label + " is invalid") from None
    if parsed.utcoffset() != timedelta(0):
        raise AcceptanceFailure(label + " is not UTC")
    return value


def checksum(value, label):
    if type(value) is not str or CHECKSUM.fullmatch(value) is None:
        raise AcceptanceFailure(label + " is invalid")
    return value


def request_id(value, label):
    if type(value) is not str or REQUEST_ID.fullmatch(value) is None:
        raise AcceptanceFailure(label + " is invalid")
    return value


def child_pids(parent_pid):
    buffer = (ctypes.c_int * 4096)()
    ctypes.set_errno(0)
    count = proc_listchildpids(parent_pid, buffer, ctypes.sizeof(buffer))
    if count < 0 and ctypes.get_errno() == errno.ESRCH:
        return []
    if count < 0:
        raise AcceptanceFailure("process child enumeration failed")
    if count >= len(buffer):
        raise AcceptanceFailure("process child enumeration was truncated")
    if count == 0:
        return []
    return [pid for pid in buffer[:count] if pid > 0]


def same_uid_pids(uid):
    pid_size = ctypes.sizeof(ctypes.c_int)
    estimated_bytes = proc_listpids(PROC_UID_ONLY, uid, None, 0)
    if estimated_bytes <= 0 or estimated_bytes % pid_size != 0:
        raise AcceptanceFailure("system process enumeration failed")
    estimated_count = estimated_bytes // pid_size
    capacity = max(1024, estimated_count + 256)
    buffer = (ctypes.c_int * capacity)()
    ctypes.set_errno(0)
    returned_bytes = proc_listpids(
        PROC_UID_ONLY, uid, buffer, ctypes.sizeof(buffer)
    )
    if returned_bytes <= 0 or returned_bytes % pid_size != 0:
        raise AcceptanceFailure("system process enumeration failed")
    count = returned_bytes // pid_size
    if count >= capacity:
        raise AcceptanceFailure("system process enumeration was truncated")
    return [pid for pid in buffer[:count] if pid > 0]


def process_is_present(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_metadata(pid):
    info = ProcBSDInfoWithUniqueID()
    ctypes.set_errno(0)
    result = proc_pidinfo(
        pid,
        PROC_PIDT_BSDINFOWITHUNIQID,
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if result == 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH or not process_is_present(pid):
            return None
    if result != ctypes.sizeof(info) or info.bsd.pbi_pid != pid:
        raise AcceptanceFailure("process identity query failed")
    return (
        pid,
        info.bsd.pbi_pgid,
        info.bsd.pbi_uid,
        info.bsd.pbi_start_tvsec,
        info.bsd.pbi_start_tvusec,
        bytes(info.unique.p_uuid),
        info.unique.p_uniqueid,
        info.unique.p_puniqueid,
        info.unique.p_idversion,
    )


def process_identity(pid):
    for _ in range(3):
        metadata = process_metadata(pid)
        if metadata is None:
            return None
        path_buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        ctypes.set_errno(0)
        length = proc_pidpath(pid, path_buffer, len(path_buffer))
        if length <= 0 and not process_is_present(pid):
            return None
        if length <= 0 or length >= len(path_buffer):
            raise AcceptanceFailure("process executable identity query failed")
        confirmed = process_metadata(pid)
        if confirmed is None:
            return None
        stable_metadata = (metadata[6], metadata[7], metadata[2:5])
        stable_confirmation = (confirmed[6], confirmed[7], confirmed[2:5])
        if stable_metadata != stable_confirmation:
            return None
        if confirmed[5:] != metadata[5:]:
            continue
        identity = ProcessIdentity(metadata, bytes(path_buffer.raw[:length]))
        identity.process_group = confirmed[1]
        return identity
    raise AcceptanceFailure("process identity did not stabilize after exec")


def process_environment(pid):
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    ctypes.set_errno(0)
    if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH or not process_is_present(pid):
            return None
        if error_number in {errno.EIO, errno.ENOMEM}:
            raise ProcessObservationTransient()
        raise AcceptanceFailure("process environment size query failed")
    if size.value <= ctypes.sizeof(ctypes.c_int) or size.value > MAX_PROCARGS_BYTES:
        raise AcceptanceFailure("process environment size is invalid")
    buffer = ctypes.create_string_buffer(size.value)
    ctypes.set_errno(0)
    if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH or not process_is_present(pid):
            return None
        if error_number in {errno.EIO, errno.ENOMEM}:
            raise ProcessObservationTransient()
        raise AcceptanceFailure("process environment query failed")
    raw = bytes(buffer.raw[: size.value])
    integer_size = ctypes.sizeof(ctypes.c_int)
    argument_count = int.from_bytes(
        raw[:integer_size], byteorder=sys.byteorder, signed=True
    )
    if argument_count < 0 or argument_count > 4096:
        raise AcceptanceFailure("process argument count is invalid")
    cursor = integer_size
    executable_end = raw.find(b"\0", cursor)
    if executable_end < 0:
        raise AcceptanceFailure("process executable arguments are invalid")
    cursor = executable_end + 1
    while cursor < len(raw) and raw[cursor] == 0:
        cursor += 1
    for _ in range(argument_count):
        argument_end = raw.find(b"\0", cursor)
        if argument_end < 0:
            raise AcceptanceFailure("process arguments are truncated")
        cursor = argument_end + 1
    environment = []
    while cursor < len(raw):
        entry_end = raw.find(b"\0", cursor)
        if entry_end < 0:
            raise AcceptanceFailure("process environment is truncated")
        entry = raw[cursor:entry_end]
        cursor = entry_end + 1
        if entry:
            environment.append(entry)
    return environment


def process_has_run_identity(pid, expected_identity, run_identity):
    before = process_identity(pid)
    if before is None or before.stable_key() != expected_identity.stable_key():
        return False
    environment = process_environment(pid)
    if environment is None:
        return False
    after = process_identity(pid)
    if after is None or after.stable_key() != before.stable_key():
        return False
    marker = (RUN_ID_ENVIRONMENT + "=" + run_identity).encode("ascii")
    return environment.count(marker) == 1


def remember_descendant(identity, observed):
    previous = observed.get(identity.unique_id)
    if previous is not None and previous.stable_key() != identity.stable_key():
        raise AcceptanceFailure("observed descendant identity changed")
    observed[identity.unique_id] = identity


def observe_descendants(root_identity, observed, *, full_scan=False):
    pending = [root_identity, *observed.values()]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent.unique_id in visited:
            continue
        visited.add(parent.unique_id)
        for pid in child_pids(parent.pid):
            identity = process_identity(pid)
            if identity is None or identity.parent_unique_id != parent.unique_id:
                continue
            remember_descendant(identity, observed)
            pending.append(identity)
    if not full_scan:
        return False
    metadata_by_parent = {}
    recent_metadata = []
    for pid in same_uid_pids(root_identity.uid):
        metadata = process_metadata(pid)
        if metadata is not None:
            metadata_by_parent.setdefault(metadata[7], []).append(metadata)
            if metadata[3] >= started_epoch_seconds - 1:
                recent_metadata.append(metadata)
    active_unique_ids = {
        metadata[6]
        for values in metadata_by_parent.values()
        for metadata in values
    }
    pending_unique_ids = [root_identity.unique_id, *observed]
    visited = set()
    while pending_unique_ids:
        parent_unique_id = pending_unique_ids.pop()
        if parent_unique_id in visited:
            continue
        visited.add(parent_unique_id)
        for metadata in metadata_by_parent.get(parent_unique_id, []):
            identity = process_identity(metadata[0])
            if identity is None or identity.parent_unique_id != parent_unique_id:
                continue
            remember_descendant(identity, observed)
            pending_unique_ids.append(identity.unique_id)
    uncertain = False
    for metadata in recent_metadata:
        if metadata[6] == root_identity.unique_id:
            continue
        if metadata[6] not in observed and metadata[7] in active_unique_ids:
            continue
        identity = process_identity(metadata[0])
        if identity is not None:
            try:
                matches_run = process_has_run_identity(
                    identity.pid, identity, run_identity
                )
            except ProcessObservationTransient:
                uncertain = True
                continue
            if matches_run:
                remember_descendant(identity, observed)
    return uncertain


def stabilize_descendants(root_identity, observed, deadline):
    quiet_since = None
    while True:
        before = frozenset(observed)
        uncertain = observe_descendants(root_identity, observed, full_scan=True)
        now = time.monotonic()
        if uncertain:
            quiet_since = None
        elif frozenset(observed) != before:
            quiet_since = now
        elif quiet_since is None:
            quiet_since = now
        elif now - quiet_since >= STABLE_SCAN_QUIET_SECONDS:
            return
        if now >= deadline:
            raise AcceptanceFailure("descendant scan did not become stable")
        time.sleep(min(0.005, max(0, deadline - now)))


def observed_process_exists(pid, expected_identity):
    current = process_identity(pid)
    return current is not None and (
        current.stable_key() == expected_identity.stable_key()
    )


def signal_observed_descendants(observed, signal_number):
    own_process_group = os.getpgrp()
    signaled_groups = set()
    for expected in observed.values():
        current = process_identity(expected.pid)
        if current is None or current.stable_key() != expected.stable_key():
            continue
        try:
            if (
                current.process_group == current.pid
                and current.process_group != own_process_group
                and current.process_group not in signaled_groups
            ):
                os.killpg(current.process_group, signal_number)
                signaled_groups.add(current.process_group)
            else:
                os.kill(current.pid, signal_number)
        except ProcessLookupError:
            pass


def terminate_observed_descendants(observed, deadline):
    signal_observed_descendants(observed, signal.SIGTERM)
    for _ in range(10):
        if not any(
            observed_process_exists(pid, identity)
            for pid, identity in (
                (identity.pid, identity) for identity in observed.values()
            )
        ) or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    signal_observed_descendants(observed, signal.SIGKILL)
    for _ in range(20):
        if not any(
            observed_process_exists(pid, identity)
            for pid, identity in (
                (identity.pid, identity) for identity in observed.values()
            )
        ) or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


def signal_root_process(root_identity, signal_number):
    current = process_identity(root_identity.pid)
    if current is None or current.stable_key() != root_identity.stable_key():
        return
    try:
        os.kill(current.pid, signal_number)
    except ProcessLookupError:
        pass


def terminate_process_tree(child, root_identity, observed, deadline):
    signal_root_process(root_identity, signal.SIGTERM)
    signal_observed_descendants(observed, signal.SIGTERM)
    for _ in range(10):
        if child.poll() is not None or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    signal_root_process(root_identity, signal.SIGKILL)
    signal_observed_descendants(observed, signal.SIGKILL)
    if child.poll() is None:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AcceptanceFailure("CLI process cleanup exceeded its deadline") from None
    for _ in range(20):
        if not any(
            observed_process_exists(identity.pid, identity)
            for identity in observed.values()
        ) or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


command_elapsed_seconds = {}


def run_command(name, arguments):
    require_time(command_deadline, name)
    raw_path = runtime / (name + ".raw.json")
    stderr_path = runtime / (name + ".stderr")
    command_started = time.monotonic()
    remaining = command_deadline - command_started
    cleanup_reserve = min(0.5, max(0.05, remaining / 4))
    child = None
    root_identity = None
    observed_descendants = {}
    cli_environment = dict(os.environ)
    cli_environment[RUN_ID_ENVIRONMENT] = run_identity
    try:
        with raw_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(raw_path, 0o600)
            os.chmod(stderr_path, 0o600)
            child = subprocess.Popen(
                [cli, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                env=cli_environment,
            )
            root_identity = process_identity(child.pid)
            if root_identity is None:
                raise AcceptanceFailure(name + " process identity is unavailable")
            wait_deadline = command_started + max(0, remaining - cleanup_reserve)
            while True:
                if os.environ.get(
                    "KUNJIN_PHASE0_TEST_SKIP_LIVE_DESCENDANT_SCAN"
                ) != "1":
                    observe_descendants(root_identity, observed_descendants)
                exit_code = child.poll()
                if exit_code is not None:
                    stabilize_descendants(
                        root_identity,
                        observed_descendants,
                        min(
                            command_deadline,
                            time.monotonic() + cleanup_reserve,
                        ),
                    )
                    break
                if time.monotonic() >= wait_deadline:
                    exit_code = None
                    break
                time.sleep(min(0.005, max(0, wait_deadline - time.monotonic())))
            if exit_code is None:
                observe_descendants(
                    root_identity,
                    observed_descendants,
                    full_scan=True,
                )
                terminate_process_tree(
                    child,
                    root_identity,
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
                stabilize_descendants(
                    root_identity,
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
                terminate_observed_descendants(
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
                raise AcceptanceFailure(name + " reached the global acceptance deadline")
        if any(
            observed_process_exists(pid, identity)
            for pid, identity in (
                (identity.pid, identity)
                for identity in observed_descendants.values()
            )
        ):
            terminate_observed_descendants(
                observed_descendants,
                min(command_deadline, time.monotonic() + cleanup_reserve),
            )
            raise AcceptanceFailure(name + " left a detached descendant")
        if exit_code != 0:
            raise AcceptanceFailure(name + " returned a non-zero process exit")
        size = raw_path.stat().st_size
        if size <= 0 or size > MAX_RAW_BYTES:
            raise AcceptanceFailure(name + " JSON size is invalid")
        command_elapsed_seconds[name] = round(time.monotonic() - command_started, 3)
        require_time(command_deadline, name)
        return raw_path
    except BaseException:
        if root_identity is not None:
            try:
                observe_descendants(
                    root_identity,
                    observed_descendants,
                    full_scan=True,
                )
            except AcceptanceFailure:
                pass
        if child is not None and root_identity is not None:
            try:
                terminate_process_tree(
                    child,
                    root_identity,
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
            except AcceptanceFailure:
                pass
            try:
                stabilize_descendants(
                    root_identity,
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
                terminate_observed_descendants(
                    observed_descendants,
                    min(command_deadline, time.monotonic() + cleanup_reserve),
                )
            except AcceptanceFailure:
                pass
        raise


def load_envelope(path, expected_command):
    require_time(publish_deadline, expected_command + " validation")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AcceptanceFailure(expected_command + " is not one UTF-8 JSON document") from None
    exact_dict(payload, ENVELOPE_KEYS, expected_command + " envelope")
    if payload["schema_version"] != "1" or payload["command"] != expected_command:
        raise AcceptanceFailure(expected_command + " envelope identity is invalid")
    as_of = utc_text(payload["as_of"], expected_command + " as_of")
    exact_list(payload["warnings"], expected_command + " warnings")
    for warning in payload["warnings"]:
        public_text(warning, expected_command + " warning")
    if payload["errors"] != []:
        raise AcceptanceFailure(expected_command + " returned command errors")
    if type(payload["data"]) is not dict:
        raise AcceptanceFailure(expected_command + " data is invalid")
    return payload, as_of


def projected_envelope(command, as_of, data):
    return {
        "schema_version": "1",
        "command": command,
        "as_of": as_of,
        "data": data,
        "warnings": [],
        "errors": [],
    }


def project_version(path):
    payload, as_of = load_envelope(path, "version")
    data = exact_dict(payload["data"], {"version"}, "version data")
    if type(data["version"]) is not str or VERSION.fullmatch(data["version"]) is None:
        raise AcceptanceFailure("version value is invalid")
    return projected_envelope("version", as_of, {"version": data["version"]})


def project_supplementation(value, label):
    item = exact_dict(value, SUPPLEMENTATION_KEYS, label)
    accepted_input = exact_list(item["accepted_input"], label + " accepted input")
    projected_inputs = [
        public_text(entry, label + " accepted input entry") for entry in accepted_input
    ]
    if not projected_inputs:
        raise AcceptanceFailure(label + " accepted input is empty")
    projected = {
        "accepted_input": projected_inputs,
        "impact_if_missing": public_text(item["impact_if_missing"], label + " impact"),
        "missing_item": identifier(item["missing_item"], label + " missing item"),
        "suggested_location": public_text(item["suggested_location"], label + " location"),
        "supported_without_it": public_text(
            item["supported_without_it"], label + " supported scope"
        ),
        "unsupported_without_it": public_text(
            item["unsupported_without_it"], label + " unsupported scope"
        ),
    }
    public_text(item["freshness_requirement"], label + " freshness")
    public_text(item["why_required"], label + " rationale")
    return projected


def project_source(path, expected_code):
    payload, as_of = load_envelope(path, "source.status")
    data = exact_dict(payload["data"], SOURCE_DATA_KEYS, "source status data")
    fund_code = data["fund_code"]
    if fund_code is not None and (
        type(fund_code) is not str or FUND_CODE.fullmatch(fund_code) is None
    ):
        raise AcceptanceFailure("source status fund code is invalid")
    if fund_code != expected_code:
        raise AcceptanceFailure("source status fund code scope is invalid")
    if data["mode"] != "rapid":
        raise AcceptanceFailure("source status mode is invalid")
    projected_fields = []
    identities = set()
    for index, value in enumerate(exact_list(data["source_fields"], "source fields")):
        item = exact_dict(value, SOURCE_FIELD_KEYS, "source field")
        source_id = identifier(item["source_id"], "source id")
        field_id = identifier(item["field_id"], "field id")
        identity = (source_id, field_id)
        if identity in identities:
            raise AcceptanceFailure("source fields contain duplicate identities")
        identities.add(identity)
        identifier(item["source_kind"], "source kind")
        public_text(item["source_scope"], "source scope")
        public_text(item["field_scope"], "field scope")
        if item["source_tier"] not in SOURCE_TIERS:
            raise AcceptanceFailure("source tier is invalid")
        alternatives = exact_list(item["acceptable_alternatives"], "alternatives")
        for alternative in alternatives:
            exact_dict(alternative, {"field_id", "source_id"}, "alternative")
            identifier(alternative["source_id"], "alternative source id")
            identifier(alternative["field_id"], "alternative field id")
        if item["state"] not in SOURCE_STATES:
            raise AcceptanceFailure("source state is invalid")
        if type(item["consecutive_failures"]) is not int or not (
            0 <= item["consecutive_failures"] <= 64
        ):
            raise AcceptanceFailure("source failure count is invalid")
        cooldown_until = utc_text(item["cooldown_until"], "cooldown", optional=True)
        last_failure_at = utc_text(
            item["last_failure_at"], "last failure", optional=True
        )
        last_success_at = utc_text(
            item["last_success_at"], "last success", optional=True
        )
        last_success_data_as_of = utc_text(
            item["last_success_data_as_of"], "last success data", optional=True
        )
        failure_reason = item["last_failure_reason"]
        if failure_reason is not None:
            identifier(failure_reason, "last failure reason")
        if (last_failure_at is None) != (failure_reason is None):
            raise AcceptanceFailure("source failure evidence is inconsistent")
        if (last_success_at is None) != (last_success_data_as_of is None):
            raise AcceptanceFailure("source success evidence is inconsistent")
        if item["state"] == "not_checked" and any(
            value is not None
            for value in (cooldown_until, last_failure_at, last_success_at)
        ):
            raise AcceptanceFailure("not-checked source contains attempt evidence")
        projected_fields.append(
            {
                "consecutive_failures": item["consecutive_failures"],
                "field_id": field_id,
                "last_failure_at": last_failure_at,
                "last_failure_reason": failure_reason,
                "last_success_at": last_success_at,
                "last_success_data_as_of": last_success_data_as_of,
                "source_id": source_id,
                "state": item["state"],
                "supplementation": project_supplementation(
                    item["supplementation"], "source supplementation " + str(index)
                ),
            }
        )
    if not projected_fields:
        raise AcceptanceFailure("source status fields are empty")
    projected_resolutions = []
    resolution_fields = set()
    for value in exact_list(data["request_field_resolutions"], "field resolutions"):
        item = exact_dict(value, RESOLUTION_KEYS, "field resolution")
        if item["action"] != "fact_research" or item["risk_effect"] != "information":
            raise AcceptanceFailure("field resolution action is invalid")
        field_id = identifier(item["field_id"], "resolution field id")
        if field_id in resolution_fields:
            raise AcceptanceFailure("field resolutions contain duplicates")
        resolution_fields.add(field_id)
        if item["resolution"] not in RESOLUTIONS:
            raise AcceptanceFailure("field resolution is invalid")
        projected_resolutions.append(
            {
                "action": "fact_research",
                "field_id": field_id,
                "primary_source_id": identifier(
                    item["primary_source_id"], "resolution primary source id"
                ),
                "resolution": item["resolution"],
                "risk_effect": "information",
            }
        )
    if not projected_resolutions:
        raise AcceptanceFailure("field resolutions are empty")
    projected = {
        "fund_code": fund_code,
        "mode": "rapid",
        "policy_checksum": checksum(data["policy_checksum"], "policy checksum"),
        "policy_version": identifier(data["policy_version"], "policy version"),
        "registry_checksum": checksum(data["registry_checksum"], "registry checksum"),
        "registry_version": identifier(data["registry_version"], "registry version"),
        "request_field_resolutions": projected_resolutions,
        "request_id": request_id(data["request_id"], "source request id"),
        "snapshot_at": utc_text(data["snapshot_at"], "source snapshot"),
        "source_fields": projected_fields,
    }
    return projected_envelope("source.status", as_of, projected)


def project_sync(path):
    payload, as_of = load_envelope(path, "sync.fund-profile")
    data = exact_dict(payload["data"], SYNC_DATA_KEYS, "sync data")
    if data["fund_code"] != code:
        raise AcceptanceFailure("sync fund code is invalid")
    sections = exact_dict(data["sections"], set(data["sections"]), "sync sections")
    allowed_sections = {
        "announcements",
        "basic_profile",
        "fee_schedule",
        "manager_history",
        "size_history",
    }
    allowed_freshness_sections = {
        "announcement",
        "basic_profile",
        "fee_schedule",
        "industry_exposure",
        "manager_history",
        "quarterly_holdings",
        "size_history",
    }
    if not sections or not set(sections).issubset(allowed_sections):
        raise AcceptanceFailure("sync sections are invalid")
    projected_sections = {}
    for section_name, value in sorted(sections.items()):
        item = exact_dict(value, SECTION_KEYS, "sync section")
        if item["section"] != section_name:
            raise AcceptanceFailure("sync section identity is invalid")
        if type(item["records"]) is not int or item["records"] < 0:
            raise AcceptanceFailure("sync section record count is invalid")
        status = identifier(item["status"], "sync section status")
        freshness = identifier(item["freshness"], "sync section freshness")
        error_code = item["error_code"]
        if error_code is not None:
            identifier(error_code, "sync section error code")
        projected_sections[section_name] = {
            "as_of": utc_text(item["as_of"], "sync section as_of", optional=True),
            "error_code": error_code,
            "freshness": freshness,
            "last_attempt_at": utc_text(
                item["last_attempt_at"], "sync last attempt", optional=True
            ),
            "last_success_at": utc_text(
                item["last_success_at"], "sync last success", optional=True
            ),
            "records": item["records"],
            "section": section_name,
            "status": status,
        }
    request = exact_dict(data["request"], REQUEST_KEYS, "sync request")
    if request["mode"] != "rapid" or request["terminal_status"] not in {
        "complete",
        "partial",
    }:
        raise AcceptanceFailure("sync request terminal contract is invalid")
    projected_request = {
        "deadline_at": utc_text(request["deadline_at"], "sync deadline"),
        "mode": "rapid",
        "omitted_work": identifier_list(request["omitted_work"], "omitted work"),
        "request_id": request_id(request["request_id"], "sync request id"),
        "terminal_status": request["terminal_status"],
    }
    for source in exact_list(data["sources"], "sync sources"):
        item = exact_dict(source, SOURCE_DOCUMENT_KEYS, "sync source")
        if type(item["id"]) is not int or item["id"] <= 0:
            raise AcceptanceFailure("sync source id is invalid")
        identifier(item["document_kind"], "sync document kind")
        public_text(item["title"], "sync source title")
        public_text(item["url"], "sync source URL")
        public_text(item["source_name"], "sync source name")
        public_text(item["publisher"], "sync source publisher")
        if type(item["source_tier"]) is not int or item["source_tier"] <= 0:
            raise AcceptanceFailure("sync source tier is invalid")
        utc_text(item["retrieved_at"], "sync source retrieval")
        if item["published_at"] is not None:
            public_text(item["published_at"], "sync source publication date")
    freshness = exact_dict(data["freshness"], FRESHNESS_KEYS, "sync freshness")
    utc_text(freshness["as_of"], "sync freshness as_of")
    freshness_sections = exact_dict(
        freshness["sections"], set(freshness["sections"]), "freshness sections"
    )
    for section_name, value in freshness_sections.items():
        if section_name not in allowed_freshness_sections:
            raise AcceptanceFailure("freshness section identity is invalid")
        item = exact_dict(value, FRESHNESS_SECTION_KEYS, "freshness section")
        identifier(item["state"], "freshness state")
        utc_text(item["last_attempted_at"], "freshness attempt", optional=True)
        utc_text(item["last_success_at"], "freshness success", optional=True)
        if item["age_days"] is not None and (
            type(item["age_days"]) is not int or item["age_days"] < 0
        ):
            raise AcceptanceFailure("freshness age is invalid")
    for label in ("warnings", "conflicts"):
        for entry in exact_list(data[label], "sync " + label):
            public_text(entry, "sync " + label + " entry")
    projected_errors = []
    for value in exact_list(data["errors"], "sync errors"):
        item = exact_dict(value, SYNC_ERROR_KEYS, "sync error")
        projected_errors.append(
            {
                "code": identifier(item["code"], "sync error code"),
                "section": identifier(item["section"], "sync error section"),
            }
        )
        public_text(item["message"], "sync error message")
    return projected_envelope(
        "sync.fund-profile",
        as_of,
        {
            "fund_code": code,
            "request": projected_request,
            "section_errors": projected_errors,
            "sections": projected_sections,
        },
    )


def validate_conclusion(value):
    item = exact_dict(value, CONCLUSION_KEYS, "route conclusion evidence")
    if item["completeness"] not in {"complete", "partial", "insufficient"}:
        raise AcceptanceFailure("route evidence completeness is invalid")
    if item["freshness"] not in {"current", "dated_history", "stale", "unknown"}:
        raise AcceptanceFailure("route evidence freshness is invalid")
    if item["source_tier"] not in SOURCE_TIERS:
        raise AcceptanceFailure("route evidence source tier is invalid")
    for label in (
        "conflicts",
        "lineage_ids",
        "missing_critical_fields",
        "source_ids",
    ):
        identifier_list(item[label], "route evidence " + label)
    for label in ("publication_times",):
        for entry in exact_list(item[label], "route evidence " + label):
            utc_text(entry, "route evidence publication")
    for publisher in exact_list(item["publishers"], "route evidence publishers"):
        public_text(publisher, "route evidence publisher")
    for label in ("market_as_of", "report_as_of"):
        utc_text(item[label], "route evidence " + label, optional=True)
    utc_text(item["retrieved_at"], "route evidence retrieval")
    if type(item["independent_lineage_count"]) is not int or item[
        "independent_lineage_count"
    ] < 0:
        raise AcceptanceFailure("route evidence lineage count is invalid")
    if type(item["inferred"]) is not bool:
        raise AcceptanceFailure("route evidence inferred flag is invalid")
    coverage = item["coverage_percent"]
    if coverage is not None:
        if type(coverage) is not str or re.fullmatch(
            r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]+)?", coverage
        ) is None:
            raise AcceptanceFailure("route evidence coverage is invalid")
        if float(coverage) > 100:
            raise AcceptanceFailure("route evidence coverage is invalid")


def project_route(path):
    payload, as_of = load_envelope(path, "decision.route")
    data = exact_dict(payload["data"], ROUTE_DATA_KEYS, "route data")
    if data["mode"] != "rapid" or data["workflow_level"] not in WORKFLOW_LEVELS:
        raise AcceptanceFailure("route mode is invalid")
    actions = exact_list(data["actions"], "route actions")
    if len(actions) != 2:
        raise AcceptanceFailure("route must contain exactly two actions")
    projected_actions = []
    action_names = []
    for value in actions:
        item = exact_dict(value, ACTION_KEYS, "route action")
        action = item["action"]
        if action not in {"fact_research", "buy_or_add"}:
            raise AcceptanceFailure("route action is outside the acceptance scope")
        if item["action_id"] != action:
            raise AcceptanceFailure("route action id is invalid")
        if item["action_maturity"] not in ACTION_MATURITIES:
            raise AcceptanceFailure("route action maturity is invalid")
        if item["minimum_state"] not in ACTION_STATES:
            raise AcceptanceFailure("route action state is invalid")
        if item["risk_effect"] not in RISK_EFFECTS:
            raise AcceptanceFailure("route risk effect is invalid")
        if (
            action == "fact_research" and item["risk_effect"] != "information"
        ) or (
            action == "buy_or_add" and item["risk_effect"] != "risk_increasing"
        ):
            raise AcceptanceFailure("route action risk contract is invalid")
        if type(item["research_available"]) is not bool or type(
            item["exact_amount_available"]
        ) is not bool:
            raise AcceptanceFailure("route action booleans are invalid")
        projected_actions.append(
            {
                "action": action,
                "action_id": action,
                "action_maturity": item["action_maturity"],
                "blocking_codes": identifier_list(
                    item["blocking_codes"], "route blocking codes"
                ),
                "exact_amount_available": item["exact_amount_available"],
                "minimum_state": item["minimum_state"],
                "required_gates": identifier_list(
                    item["required_gates"], "route required gates"
                ),
                "research_available": item["research_available"],
                "risk_effect": item["risk_effect"],
            }
        )
        action_names.append(action)
    if action_names != ["fact_research", "buy_or_add"]:
        raise AcceptanceFailure("route actions are duplicate or non-canonical")
    for conclusion in exact_list(data["conclusion_evidence"], "route conclusions"):
        validate_conclusion(conclusion)
    projected = {
        "actions": projected_actions,
        "created_at": utc_text(data["created_at"], "route creation"),
        "missing_fields": identifier_list(data["missing_fields"], "route missing fields"),
        "mode": "rapid",
        "opposing_evidence": identifier_list(
            data["opposing_evidence"], "route opposing evidence"
        ),
        "policy_checksum": checksum(data["policy_checksum"], "route policy checksum"),
        "policy_version": identifier(data["policy_version"], "route policy version"),
        "registry_checksum": checksum(
            data["registry_checksum"], "route registry checksum"
        ),
        "registry_version": identifier(
            data["registry_version"], "route registry version"
        ),
        "request_id": request_id(data["request_id"], "route request id"),
        "result_checksum": checksum(data["result_checksum"], "route result checksum"),
        "workflow_level": "rapid_evidence",
    }
    return projected_envelope("decision.route", as_of, projected)


def write_json_at(directory_fd, filename, value):
    require_time(publish_deadline, "staging write")
    payload = (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    file_fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(file_fd, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(file_fd)
        os.fchmod(file_fd, 0o600)
    finally:
        os.close(file_fd)


def remove_staging(parent_fd, staging_fd, staging_name):
    if staging_fd is not None:
        try:
            for name in os.listdir(staging_fd):
                try:
                    os.unlink(name, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        try:
            os.close(staging_fd)
        except OSError:
            pass
    if staging_name is not None:
        try:
            os.rmdir(staging_name, dir_fd=parent_fd)
        except OSError:
            pass


def exclusive_rename(parent_fd, staging_name, target_name):
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        parent_fd,
        os.fsencode(staging_name),
        parent_fd,
        os.fsencode(target_name),
        RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise AcceptanceFailure("OUTPUT_DIR appeared before atomic publication")
        raise AcceptanceFailure("exclusive atomic output publication failed")


def same_file_identity(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def drain_pending_alarm():
    while signal.SIGALRM in signal.sigpending():
        signal.sigwait({signal.SIGALRM})


def mark_publication_residual():
    (runtime / "publication-residual").write_text("1\n", encoding="ascii")


def run_rename_test_hook(position):
    global staging_name
    hook = os.environ.get("KUNJIN_PHASE0_TEST_RENAME_HOOK")
    if hook == "pending_before_rename" and position == "before_rename":
        os.kill(os.getpid(), signal.SIGALRM)
    elif hook == "pending_after_rename" and position == "after_rename":
        os.kill(os.getpid(), signal.SIGALRM)
    elif hook == "expire_after_rename" and position == "after_rename":
        time.sleep(max(0, hard_deadline - time.monotonic()) + 0.01)
    elif hook == "expire_after_fsync" and position == "after_fsync":
        time.sleep(max(0, hard_deadline - time.monotonic()) + 0.01)
    elif hook == "replace_after_quarantine_removal" and position == "after_fsync":
        time.sleep(max(0, hard_deadline - time.monotonic()) + 0.01)
    elif hook == "replace_after_rename" and position == "after_rename":
        replacement = Path(
            os.environ["KUNJIN_PHASE0_TEST_REPLACEMENT_SOURCE"]
        ).resolve()
        if replacement.parent != Path(output_parent_path).resolve():
            raise AcceptanceFailure("test replacement parent is invalid")
        displaced_name = ".kunjin-phase0-displaced-" + secrets.token_hex(16)
        exclusive_rename(parent_fd, output_basename, displaced_name)
        exclusive_rename(parent_fd, replacement.name, output_basename)
        staging_name = displaced_name
    elif (
        hook == "replace_after_quarantine_removal"
        and position == "after_quarantine_removal"
    ):
        replacement = Path(
            os.environ["KUNJIN_PHASE0_TEST_REPLACEMENT_SOURCE"]
        ).resolve()
        if replacement.parent != Path(output_parent_path).resolve():
            raise AcceptanceFailure("test replacement parent is invalid")
        exclusive_rename(parent_fd, replacement.name, output_basename)


def commit_staging():
    global published, staging_fd, staging_name

    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    staging_identity = os.fstat(staging_fd)
    renamed = False
    try:
        run_rename_test_hook("before_rename")
        exclusive_rename(parent_fd, staging_name, output_basename)
        renamed = True
        staging_name = output_basename
        run_rename_test_hook("after_rename")

        target_fd = os.open(
            output_basename,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            target_identity = os.fstat(target_fd)
            if not stat.S_ISDIR(target_identity.st_mode) or not same_file_identity(
                staging_identity, target_identity
            ):
                raise AcceptanceFailure("published output identity changed")
        finally:
            os.close(target_fd)
        if time.monotonic() >= hard_deadline:
            raise AcceptanceDeadline()
        os.fsync(parent_fd)
        run_rename_test_hook("after_fsync")
        if time.monotonic() >= hard_deadline:
            raise AcceptanceDeadline()
        signal.setitimer(signal.ITIMER_REAL, 0)
        drain_pending_alarm()
        os.close(staging_fd)
        staging_fd = None
        staging_name = None
        published = True
    except BaseException:
        signal.setitimer(signal.ITIMER_REAL, 0)
        drain_pending_alarm()
        if renamed and not published:
            staging_name = None
            quarantine_name = (
                ".kunjin-phase0-quarantine-" + secrets.token_hex(16)
            )
            try:
                exclusive_rename(parent_fd, output_basename, quarantine_name)
            except AcceptanceFailure:
                mark_publication_residual()
                raise
            try:
                quarantine_fd = os.open(
                    quarantine_name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except OSError:
                mark_publication_residual()
                try:
                    exclusive_rename(parent_fd, quarantine_name, output_basename)
                except AcceptanceFailure:
                    raise AcceptanceFailure(
                        "unverified concurrent output remains quarantined"
                    ) from None
                raise AcceptanceFailure(
                    "unverified concurrent output was restored"
                ) from None
            try:
                quarantine_identity = os.fstat(quarantine_fd)
            finally:
                os.close(quarantine_fd)
            if not same_file_identity(staging_identity, quarantine_identity):
                mark_publication_residual()
                try:
                    exclusive_rename(parent_fd, quarantine_name, output_basename)
                except AcceptanceFailure:
                    raise AcceptanceFailure(
                        "concurrent output remains quarantined"
                    ) from None
                raise AcceptanceFailure(
                    "concurrent output identity was restored"
                ) from None
            staging_name = quarantine_name
            remove_staging(parent_fd, staging_fd, quarantine_name)
            staging_fd = None
            staging_name = None
            run_rename_test_hook("after_quarantine_removal")
            try:
                os.stat(output_basename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                mark_publication_residual()
                raise AcceptanceFailure("expired output remains published") from None
            try:
                os.stat(quarantine_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                mark_publication_residual()
                raise AcceptanceFailure("expired output quarantine remains") from None
            os.fsync(parent_fd)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


staging_name = None
staging_fd = None
parent_fd = None
published = False
try:
    version_path = run_command("version", ["--json", "version"])
    source_before_path = run_command(
        "source_status_before", ["--json", "source", "status"]
    )
    sync_path = run_command(
        "sync_fund_profile",
        ["--json", "sync", "fund-profile", code, "--mode", "rapid"],
    )
    source_after_path = run_command(
        "source_status_after",
        ["--json", "source", "status", "--fund-code", code],
    )
    route_path = run_command(
        "decision_route",
        [
            "--json",
            "decision",
            "route",
            "--mode",
            "rapid",
            "--action",
            "fact_research",
            "--action",
            "buy_or_add",
        ],
    )

    version = project_version(version_path)
    source_before = project_source(source_before_path, None)
    sync = project_sync(sync_path)
    source_after = project_source(source_after_path, code)
    route = project_route(route_path)

    identities = (
        source_before["data"]["policy_version"],
        source_before["data"]["policy_checksum"],
        source_before["data"]["registry_version"],
        source_before["data"]["registry_checksum"],
    )
    for result in (source_after, route):
        if identities != (
            result["data"]["policy_version"],
            result["data"]["policy_checksum"],
            result["data"]["registry_version"],
            result["data"]["registry_checksum"],
        ):
            raise AcceptanceFailure("policy or registry identity changed during acceptance")

    request_ids = {
        "source_before": source_before["data"]["request_id"],
        "sync": sync["data"]["request"]["request_id"],
        "source_after": source_after["data"]["request_id"],
        "route": route["data"]["request_id"],
    }
    if len(set(request_ids.values())) != 4:
        raise AcceptanceFailure("acceptance request ids are not distinct")

    source_attempt_count = sum(
        1
        for item in source_after["data"]["source_fields"]
        if item["last_success_at"] is not None or item["last_failure_at"] is not None
    )
    if source_attempt_count < 1:
        raise AcceptanceFailure("no bounded source attempt was recorded")
    fact_record_count = sum(
        item["records"]
        for item in sync["data"]["sections"].values()
        if item["status"] in {"success", "not_disclosed"} and item["records"] > 0
    )
    if fact_record_count < 1:
        raise AcceptanceFailure("sync returned no obtained public facts")
    missing_resolutions = [
        item
        for item in source_after["data"]["request_field_resolutions"]
        if item["resolution"] != "usable"
    ]
    partial = (
        sync["data"]["request"]["terminal_status"] == "partial"
        or bool(missing_resolutions)
    )
    if partial:
        if not missing_resolutions:
            raise AcceptanceFailure("partial result does not identify missing impact")
        source_by_identity = {
            (item["source_id"], item["field_id"]): item
            for item in source_after["data"]["source_fields"]
        }
        for resolution in missing_resolutions:
            supplementation = source_by_identity.get(
                (resolution["primary_source_id"], resolution["field_id"]), {}
            ).get("supplementation")
            if not supplementation:
                raise AcceptanceFailure("partial result lacks supplementation")
            if not supplementation["impact_if_missing"]:
                raise AcceptanceFailure("partial result lacks missing impact")
            if not supplementation["suggested_location"] or not supplementation[
                "accepted_input"
            ]:
                raise AcceptanceFailure("partial result lacks supplementation path")

    facts_action, buy_action = route["data"]["actions"]
    if facts_action["research_available"] is not True:
        raise AcceptanceFailure("independent fact research is unavailable")
    if (
        buy_action["exact_amount_available"] is not False
        or buy_action["minimum_state"] == "actionable"
        or buy_action["action_maturity"] == "mature"
        or not buy_action["blocking_codes"]
    ):
        raise AcceptanceFailure("Phase 0 exposed a mature or exact purchase direction")

    summary = {
        "schema_version": "1",
        "acceptance": "phase0_amount_free_live",
        "status": "passed",
        "fund_code": code,
        "mode": "rapid",
        "fresh_isolated_runtime": True,
        "sync_exit_code": 0,
        "sync_elapsed_seconds": command_elapsed_seconds["sync_fund_profile"],
        "global_deadline_seconds": timeout_seconds,
        "pre_publish_elapsed_seconds": 0,
        "command_elapsed_seconds": command_elapsed_seconds,
        "source_attempt_count": source_attempt_count,
        "fact_record_count": fact_record_count,
        "partial": partial,
        "request_ids": request_ids,
        "checksums": {
            "policy_version": route["data"]["policy_version"],
            "policy_checksum": route["data"]["policy_checksum"],
            "registry_version": route["data"]["registry_version"],
            "registry_checksum": route["data"]["registry_checksum"],
            "route_result_checksum": route["data"]["result_checksum"],
        },
    }

    require_time(publish_deadline, "output staging")
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(output_parent_path, parent_flags)
    staging_name = ".kunjin-phase0-" + secrets.token_hex(16)
    os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
    staging_fd = os.open(staging_name, parent_flags, dir_fd=parent_fd)
    os.fchmod(staging_fd, 0o700)
    exports = {
        "decision-route.json": route,
        "source-status-after.json": source_after,
        "source-status-before.json": source_before,
        "sync-fund-profile.json": sync,
        "version.json": version,
    }
    for filename, value in exports.items():
        write_json_at(staging_fd, filename, value)
    summary["pre_publish_elapsed_seconds"] = round(time.monotonic() - started, 3)
    if summary["pre_publish_elapsed_seconds"] > timeout_seconds:
        raise AcceptanceFailure("pre-publication elapsed time exceeded its budget")
    write_json_at(staging_fd, "summary.json", summary)
    expected_files = set(exports) | {"summary.json"}
    if set(os.listdir(staging_fd)) != expected_files:
        raise AcceptanceFailure("staging directory contents are invalid")
    os.fsync(staging_fd)
    require_time(publish_deadline, "atomic publication")
    commit_staging()
except BaseException:
    if parent_fd is not None and not published:
        remove_staging(parent_fd, staging_fd, staging_name)
    raise
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
    if parent_fd is not None:
        try:
            os.close(parent_fd)
        except OSError:
            pass

PY
DRIVER_EXIT_CODE=$?
set -e
/bin/chmod 600 "${RUNTIME_DIR}/driver.stderr"
if [[ "${DRIVER_EXIT_CODE}" -ne 0 ]]; then
    if [[ -e "${RUNTIME_DIR}/publication-residual" ]]; then
        printf 'Phase 0 acceptance failed closed; concurrent publication residue may remain in OUTPUT_DIR parent.\n' >&2
    fi
    printf 'Phase 0 acceptance failed closed.\n' >&2
    exit "${DRIVER_EXIT_CODE}"
fi
printf 'Phase 0 acceptance passed.\n'
