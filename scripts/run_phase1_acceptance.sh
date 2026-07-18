#!/bin/bash
set -euo pipefail

umask 077

usage() {
    printf 'Usage: %s USEFUL_PARTIAL_CODE UNSUPPORTED_PUBLIC_CODE OUTPUT_DIR\n' "$0" >&2
    printf '       %s --owner OUTPUT_DIR\n' "$0" >&2
}

MODE=""
USEFUL_PARTIAL_CODE=""
UNSUPPORTED_CODE=""
OWNER_CODE_FILE=""
if [[ "$#" -eq 3 && "$1" != "--owner" ]]; then
    MODE="public"
    USEFUL_PARTIAL_CODE="$1"
    UNSUPPORTED_CODE="$2"
    OUTPUT_DIR="$3"
elif [[ "$#" -eq 2 && "$1" == "--owner" ]]; then
    MODE="owner"
    OUTPUT_DIR="$2"
else
    usage
    exit 64
fi

if [[ "${OUTPUT_DIR}" != /* ]]; then
    printf 'OUTPUT_DIR must be an absolute path\n' >&2
    exit 65
fi
if [[ "${MODE}" == "public" ]]; then
    if [[ ! "${USEFUL_PARTIAL_CODE}" =~ ^[0-9]{6}$ ]] \
        || [[ ! "${UNSUPPORTED_CODE}" =~ ^[0-9]{6}$ ]] \
        || [[ "${USEFUL_PARTIAL_CODE}" == "${UNSUPPORTED_CODE}" ]]; then
        printf 'public fund codes must be distinct six-digit ASCII codes\n' >&2
        exit 65
    fi
fi
if [[ -e "${OUTPUT_DIR}" || -L "${OUTPUT_DIR}" ]]; then
    printf 'OUTPUT_DIR must not exist\n' >&2
    exit 66
fi

readonly OUTPUT_PARENT="$(/usr/bin/dirname -- "${OUTPUT_DIR}")"
readonly OUTPUT_BASENAME="$(/usr/bin/basename -- "${OUTPUT_DIR}")"
if [[ ! -d "${OUTPUT_PARENT}" || -L "${OUTPUT_PARENT}" ]]; then
    printf 'OUTPUT_DIR parent must be an existing physical directory\n' >&2
    exit 66
fi
if [[ "$0" != */* ]]; then
    printf 'acceptance script must be invoked by an explicit path\n' >&2
    exit 67
fi
if [[ -L "$0" ]]; then
    printf 'acceptance script symlink invocation is rejected\n' >&2
    exit 67
fi

readonly SCRIPT_DIR="$(cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)"
readonly REPOSITORY_ROOT="$(cd -P -- "${SCRIPT_DIR}/.." && /bin/pwd -P)"
readonly CLI="${REPOSITORY_ROOT}/.venv/bin/kunjin"
if [[ ! -x "${CLI}" || -L "${CLI}" ]]; then
    printf 'KunJin CLI is unavailable or unsafe\n' >&2
    exit 69
fi

readonly ACCEPTANCE_TIMEOUT_SECONDS="${KUNJIN_PHASE1_ACCEPTANCE_TIMEOUT_SECONDS:-90}"
if [[ ! "${ACCEPTANCE_TIMEOUT_SECONDS}" =~ ^[0-9]+$ ]] \
    || (( ACCEPTANCE_TIMEOUT_SECONDS < 1 || ACCEPTANCE_TIMEOUT_SECONDS > 90 )); then
    printf 'Phase 1 acceptance timeout must be an integer from 1 through 90 seconds\n' >&2
    exit 65
fi
if [[ "${MODE}" == "public" \
    && "${KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION:-}" != "synthetic_non_personal" ]]; then
    printf 'public acceptance requires an explicit synthetic non-personal fixture attestation\n' >&2
    exit 65
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase1-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
cleanup() {
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT

if [[ "${MODE}" == "owner" ]]; then
    OWNER_CODE_FILE="${RUNTIME_DIR}/owner-code"
    if ! IFS= read -r OWNER_CODE; then
        printf 'owner acceptance requires one fund code on standard input\n' >&2
        exit 65
    fi
    if [[ ! "${OWNER_CODE}" =~ ^[0-9]{6}$ ]]; then
        printf 'owner fund code must contain six ASCII digits\n' >&2
        exit 65
    fi
    printf '%s' "${OWNER_CODE}" > "${OWNER_CODE_FILE}"
    /bin/chmod 600 "${OWNER_CODE_FILE}"
    unset OWNER_CODE
fi

set +e
/usr/bin/python3 - "${MODE}" "${USEFUL_PARTIAL_CODE}" "${UNSUPPORTED_CODE}" \
    "${OUTPUT_PARENT}" "${OUTPUT_BASENAME}" "${CLI}" "${RUNTIME_DIR}" \
    "${ACCEPTANCE_TIMEOUT_SECONDS}" "${OWNER_CODE_FILE}" <<'PY'
import ctypes
import errno
import hashlib
import json
import os
import re
import selectors
import secrets
import signal
import stat
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse


mode = sys.argv[1]
useful_partial_code = sys.argv[2]
unsupported_code = sys.argv[3]
output_parent_path = sys.argv[4]
output_basename = sys.argv[5]
cli = sys.argv[6]
runtime = Path(sys.argv[7])
timeout_seconds = int(sys.argv[8])
owner_code_file = Path(sys.argv[9]) if sys.argv[9] else None
started = time.monotonic()
started_epoch_seconds = int(time.time())
hard_deadline = started + timeout_seconds
publish_deadline = hard_deadline
run_identity = secrets.token_hex(32)

FUND_CODE = re.compile(r"^[0-9]{6}$", re.ASCII)
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$", re.ASCII)
CHECKSUM = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$", re.ASCII)
ENVELOPE_KEYS = {"schema_version", "command", "as_of", "data", "warnings", "errors"}
DATA_KEYS = {
    "request", "subject", "facts", "official_events", "portfolio_relationship",
    "sync_status", "decision_evidence_status", "action_interpretation",
    "missing_evidence", "beginner_explanation_zh",
}
REQUEST_KEYS = {
    "action_ids", "created_at", "decision_snapshot_id", "evidence_fingerprint",
    "mode", "omitted_work", "request_run_id", "result_checksum", "terminal_status",
}
SUBJECT_KEYS = {
    "fund_code", "observation_version", "observed_at", "portfolio_evidence_state",
    "portfolio_weight", "position_present",
}
FACT_KEYS = {
    "calculated", "canonical_url", "completeness", "conflict_ids", "data_as_of",
    "fact_id", "field_id", "freshness", "published_at", "publisher", "retrieved_at",
    "source_id", "source_lineage_id", "source_tier", "unit", "value",
}
EVENT_KEYS = {
    "affected_action_ids", "canonical_url", "content_fingerprint", "event_code",
    "event_id", "integrity_status", "original_source_id", "published_at", "publisher",
    "quoted_source_id", "retrieved_at", "source_tier", "summary", "title",
}
PORTFOLIO_KEYS = {
    "disclosed_holdings_coverage", "minimum_relationship_coverage", "relationships",
}
COVERAGE_KEYS = {
    "coverage_id", "evidence_ids", "evidence_state", "included_fund_codes",
    "known_percent", "omitted_fund_codes", "scope", "unknown_fields",
}
RELATIONSHIP_KEYS = {
    "evidence_ids", "evidence_state", "fund_codes", "metrics", "publication_times",
    "relationship_id", "relationship_type", "report_periods", "warnings",
}
STATUS_KEYS = {
    "acceptable_alternative_ids", "conflicted_fields", "cooldown_fields",
    "manual_supplementation_codes", "missing_fields", "obtained_fields",
    "required_fields", "stale_fields", "state", "supported_interpretations",
    "unsupported_fields", "unsupported_interpretations",
}
ACTION_KEYS = {
    "action_maturity", "affected_action_abstentions", "blocking_codes", "conflicts",
    "constraints", "interpretations", "primary_state", "triggered_reviews",
}
INTERPRETATION_KEYS = {
    "action_id", "action_maturity", "blocking_codes", "exact_amount_available",
    "invalidation_conditions", "missing_fields", "opposing_evidence_ids", "state",
    "state_inputs", "supporting_evidence_ids", "unavailable_actions",
}
GAP_KEYS = {"affected_action_ids", "condition", "field_id", "scope"}
BEGINNER_KEYS = {
    "headline", "fund_identity", "portfolio_relationship", "recent_official_events",
    "why_this_state", "evidence_gaps", "change_conditions",
}
HEADLINE_KEYS = {
    "action_maturity", "items", "maturity_scope", "maturity_text", "primary_state", "text",
}
HEADLINE_ITEM_KEYS = {"action_id", "action_maturity", "state", "text"}
BEGINNER_SECTION_KEYS = {
    "fund_identity": {"data_dates", "evidence_ids", "text"},
    "portfolio_relationship": {
        "coverage_ids", "relationship_ids", "text", "unknown_fields",
    },
    "recent_official_events": {"event_ids", "inactive_items", "text"},
    "why_this_state": {"items", "text"},
    "evidence_gaps": {"items", "text"},
    "change_conditions": {"items", "text"},
}
BEGINNER_GAP_KEYS = {
    "affected_action_ids", "condition", "field_id", "label_zh", "scope",
    "source_resolution", "supplementation", "next_step",
}
BEGINNER_NEXT_STEP_KEYS = {"action", "status"}
BEGINNER_SOURCE_RESOLUTION_KEYS = {
    "acceptable_alternative_ids", "primary_source_id", "resolution",
    "source_field_id", "source_states",
}
SOURCE_DATA_KEYS = {
    "fund_code", "mode", "policy_checksum", "policy_version", "registry_checksum",
    "registry_version", "request_field_resolutions", "request_id", "snapshot_at",
    "source_fields",
}
SOURCE_FIELD_KEYS = {
    "acceptable_alternatives", "consecutive_failures", "cooldown_until", "field_id",
    "field_scope", "last_failure_at", "last_failure_reason", "last_success_at",
    "last_success_data_as_of", "source_id", "source_kind", "source_scope",
    "source_tier", "state", "supplementation",
}
SUPPLEMENTATION_KEYS = {
    "accepted_input", "freshness_requirement", "impact_if_missing", "missing_item",
    "suggested_location", "supported_without_it", "unsupported_without_it",
    "why_required",
}
RESOLUTION_KEYS = {
    "action", "field_id", "primary_source_id", "resolution", "risk_effect",
}
SOURCE_TIERS = {"tier_1", "tier_2", "private_observation", "user_provided"}
EVIDENCE_STATES = {"complete", "partial", "insufficient"}
TERMINAL_STATES = {"complete", "partial"}
ACTION_STATES = {"no_add", "hold", "watch", "reduce_or_exit_review", "abstain"}
MATURITIES = {"mature", "experimental_shadow"}
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024
RENAME_EXCL = 0x00000004
PROC_PIDPATHINFO_MAXSIZE = 4096
PROC_PIDT_BSDINFOWITHUNIQID = 18
PROC_UID_ONLY = 4
CTL_KERN = 1
KERN_PROCARGS2 = 49
MAX_PROCARGS_BYTES = 1024 * 1024
RUN_ID_ENVIRONMENT = "KUNJIN_PHASE1_RUN_ID"
STABLE_SCAN_QUIET_SECONDS = 0.03
ACCEPTANCE_FIXTURE_CONTRACT = "kunjin_phase1_public_portfolio_v1"
ACCEPTANCE_MARKER_CONTRACT = "kunjin_phase1_public_portfolio_used_v1"
ACCEPTANCE_MARKER_KEYS = {
    "contract", "fund_code", "observation_version", "payload_sha256", "request_id",
    "run_id", "schema_version", "source_attempt_id",
}
SUPPLEMENT_MISSING_ITEM = {
    "fund_manager_product_announcement": "official_events",
}
FACT_VALUE_KEYS = {
    "identity_active_status": {
        "established_date", "fund_code", "fund_company", "fund_name", "fund_type", "status",
    },
    "share_class_identity": {"fund_name", "related_fund_code", "share_class"},
    "current_manager_team": {"manager_name", "tenure_end", "tenure_start"},
    "fees_share_class_relationship": {
        "effective_from", "effective_to", "fee_type", "fixed_fee",
        "holding_days_maximum", "holding_days_minimum", "rate", "rule_order",
        "share_class", "threshold_maximum", "threshold_minimum",
    },
    "holdings_industries": {"disclosure_scope", "items", "report_period"},
    "fund_manager_product_announcement": {
        "category", "record_published_at", "record_publisher", "record_url", "title",
    },
    "redemption_terms": {"fee_condition", "settlement_condition"},
}
HOLDING_ITEM_KEYS = {
    "asset_class", "disclosed_weight", "rank", "security_code", "security_name",
}
RELATIONSHIP_METRIC_KEYS = {
    "duplicate_holding_identity": {"multiple_observations"},
    "share_class_sibling": {"aggregation_eligible", "mutual_source_links"},
    "same_manager": {"shared_manager_name"},
    "same_company": {"company_name"},
    "same_current_benchmark": {"benchmark_description", "exact_text_match"},
    "top10_disclosed_overlap": {
        "calculation_version", "left_disclosed_percent", "left_scope",
        "left_unknown_percent", "overlap_percent", "right_disclosed_percent",
        "right_scope", "right_unknown_percent", "shared_exposures",
    },
    "disclosed_overlap": {
        "calculation_version", "left_disclosed_percent", "left_scope",
        "left_unknown_percent", "overlap_percent", "right_disclosed_percent",
        "right_scope", "right_unknown_percent", "shared_exposures",
    },
    "adjusted_return_correlation": {
        "aligned_observations", "calculation_binding_mac", "calculation_version",
        "common_end_date", "correlation", "coverage", "end_date", "left_fund_code",
        "left_observations", "left_series_binding_mac", "left_source_attempt_id",
        "right_fund_code", "right_observations", "right_series_binding_mac",
        "right_source_attempt_id", "samples", "start_date",
    },
}
CORRELATION_PUBLIC_METRICS = {
    "aligned_observations", "calculation_version", "common_end_date", "correlation",
    "coverage", "end_date", "left_observations", "right_observations", "samples",
    "start_date",
}


class AcceptanceFailure(Exception):
    pass


class AcceptanceDeadline(BaseException):
    pass


class AcceptanceInterrupted(BaseException):
    pass


class ProcessObservationTransient(Exception):
    pass


class ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class ProcUniqueIdentifierInfo(ctypes.Structure):
    _fields_ = [
        ("p_uuid", ctypes.c_ubyte * 16), ("p_uniqueid", ctypes.c_uint64),
        ("p_puniqueid", ctypes.c_uint64), ("p_idversion", ctypes.c_int32),
        ("p_orig_ppidversion", ctypes.c_int32), ("p_reserve2", ctypes.c_uint64),
        ("p_reserve3", ctypes.c_uint64),
    ]


class ProcBSDInfoWithUniqueID(ctypes.Structure):
    _fields_ = [("bsd", ProcBSDInfo), ("unique", ProcUniqueIdentifierInfo)]


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
            self.unique_id, self.parent_unique_id, self.uid,
            self.start_seconds, self.start_microseconds,
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
    ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
]
proc_pidinfo.restype = ctypes.c_int
proc_listpids = libproc.proc_listpids
proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
proc_listpids.restype = ctypes.c_int
sysctl = libc.sysctl
sysctl.argtypes = [
    ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t,
]
sysctl.restype = ctypes.c_int


def on_deadline(_signal_number, _frame):
    raise AcceptanceDeadline()


def on_interrupt(_signal_number, _frame):
    raise AcceptanceInterrupted()


signal.signal(signal.SIGALRM, on_deadline)
for interrupt_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(interrupt_signal, on_interrupt)
signal.setitimer(signal.ITIMER_REAL, timeout_seconds)


def require_time(label):
    if time.monotonic() >= hard_deadline:
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
    values = [identifier(item, label + " item") for item in exact_list(value, label)]
    if len(values) != len(set(values)):
        raise AcceptanceFailure(label + " contains duplicates")
    return values


def public_text(value, label, *, optional=False):
    if optional and value is None:
        return None
    if type(value) is not str or not value or len(value) > 4096:
        raise AcceptanceFailure(label + " is invalid")
    if any(ord(character) < 32 and character != "\t" for character in value):
        raise AcceptanceFailure(label + " contains control characters")
    normalized = unicodedata.normalize("NFKC", value)
    for _ in range(2):
        normalized = unquote(normalized)
    lowered = normalized.casefold()
    for token in (
        "private_acceptance_sentinel", "/users/", "/private/", "traceback",
        "raw response body", "token=", "credential=", "ciphertext=",
    ):
        if token in lowered:
            raise AcceptanceFailure(label + " contains private diagnostic material")
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


def scan_public_tree(value, label, depth=0):
    if depth > 12:
        raise AcceptanceFailure(label + " exceeds the public tree depth")
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        raise AcceptanceFailure(label + " contains a floating point value")
    if type(value) is str:
        public_text(value, label)
        return value
    if type(value) is list:
        if len(value) > 256:
            raise AcceptanceFailure(label + " list is oversized")
        return [scan_public_tree(item, label, depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > 128:
            raise AcceptanceFailure(label + " mapping is oversized")
        projected = {}
        for key, item in value.items():
            identifier(key, label + " key")
            if key in {
                "amount", "assets", "ciphertext", "cost", "credential", "current_value",
                "debt", "income", "local_path", "loss_budget", "managed_path", "nonce",
                "position_value", "profit", "purchase_lots", "raw_body", "reserve",
                "response_body", "shares", "token", "total_asset", "portfolio_weight",
                "owner_weight", "position_weight", "holding_ratio", "account_title",
                "full_holdings",
            }:
                raise AcceptanceFailure(label + " contains a private key")
            projected[key] = scan_public_tree(item, label + "." + key, depth + 1)
        return projected
    raise AcceptanceFailure(label + " contains an unsupported value type")


def child_pids(parent_pid):
    buffer = (ctypes.c_int * 4096)()
    ctypes.set_errno(0)
    count = proc_listchildpids(parent_pid, buffer, ctypes.sizeof(buffer))
    if count < 0 and ctypes.get_errno() == errno.ESRCH:
        return []
    if count < 0 or count >= len(buffer):
        raise AcceptanceFailure("process child enumeration failed")
    return [pid for pid in buffer[:count] if pid > 0]


def same_uid_pids(uid):
    pid_size = ctypes.sizeof(ctypes.c_int)
    estimated_bytes = proc_listpids(PROC_UID_ONLY, uid, None, 0)
    if estimated_bytes <= 0 or estimated_bytes % pid_size != 0:
        raise AcceptanceFailure("system process enumeration failed")
    capacity = max(1024, estimated_bytes // pid_size + 256)
    buffer = (ctypes.c_int * capacity)()
    returned_bytes = proc_listpids(PROC_UID_ONLY, uid, buffer, ctypes.sizeof(buffer))
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
        pid, PROC_PIDT_BSDINFOWITHUNIQID, 0, ctypes.byref(info), ctypes.sizeof(info)
    )
    if result == 0:
        if ctypes.get_errno() == errno.ESRCH or not process_is_present(pid):
            return None
        raise ProcessObservationTransient()
    if result != ctypes.sizeof(info) or info.bsd.pbi_pid != pid:
        raise ProcessObservationTransient()
    return (
        pid, info.bsd.pbi_pgid, info.bsd.pbi_uid, info.bsd.pbi_start_tvsec,
        info.bsd.pbi_start_tvusec, bytes(info.unique.p_uuid), info.unique.p_uniqueid,
        info.unique.p_puniqueid, info.unique.p_idversion,
    )


def process_identity(pid):
    for _ in range(5):
        try:
            metadata = process_metadata(pid)
        except ProcessObservationTransient:
            time.sleep(0.001)
            continue
        if metadata is None:
            return None
        path_buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        length = proc_pidpath(pid, path_buffer, len(path_buffer))
        if length <= 0 and not process_is_present(pid):
            return None
        if length <= 0 or length >= len(path_buffer):
            raise AcceptanceFailure("process executable identity query failed")
        try:
            confirmed = process_metadata(pid)
        except ProcessObservationTransient:
            time.sleep(0.001)
            continue
        if confirmed is None:
            return None
        if (metadata[6], metadata[7], metadata[2:5]) != (
            confirmed[6], confirmed[7], confirmed[2:5]
        ):
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
    if sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ESRCH or not process_is_present(pid):
            return None
        if error_number in {errno.EIO, errno.ENOMEM}:
            raise ProcessObservationTransient()
        raise AcceptanceFailure("process environment query failed")
    raw = bytes(buffer.raw[: size.value])
    integer_size = ctypes.sizeof(ctypes.c_int)
    argument_count = int.from_bytes(raw[:integer_size], byteorder=sys.byteorder, signed=True)
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


def process_has_run_identity(pid, expected_identity):
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
    uncertain = False
    pending = [root_identity, *observed.values()]
    visited = set()
    while pending:
        parent = pending.pop()
        if parent.unique_id in visited:
            continue
        visited.add(parent.unique_id)
        for pid in child_pids(parent.pid):
            try:
                identity = process_identity(pid)
            except AcceptanceFailure:
                uncertain = True
                continue
            if identity is None or identity.parent_unique_id != parent.unique_id:
                continue
            remember_descendant(identity, observed)
            pending.append(identity)
    if not full_scan:
        return uncertain
    metadata_by_parent = {}
    recent_metadata = []
    for pid in same_uid_pids(root_identity.uid):
        try:
            metadata = process_metadata(pid)
        except ProcessObservationTransient:
            uncertain = True
            continue
        if metadata is not None:
            metadata_by_parent.setdefault(metadata[7], []).append(metadata)
            if metadata[3] >= started_epoch_seconds - 1:
                recent_metadata.append(metadata)
    active_unique_ids = {
        metadata[6] for values in metadata_by_parent.values() for metadata in values
    }
    pending_unique_ids = [root_identity.unique_id, *observed]
    visited = set()
    while pending_unique_ids:
        parent_unique_id = pending_unique_ids.pop()
        if parent_unique_id in visited:
            continue
        visited.add(parent_unique_id)
        for metadata in metadata_by_parent.get(parent_unique_id, []):
            try:
                identity = process_identity(metadata[0])
            except AcceptanceFailure:
                uncertain = True
                continue
            if identity is None or identity.parent_unique_id != parent_unique_id:
                continue
            remember_descendant(identity, observed)
            pending_unique_ids.append(identity.unique_id)
    for metadata in recent_metadata:
        if metadata[6] == root_identity.unique_id:
            continue
        if metadata[6] not in observed and metadata[7] in active_unique_ids:
            continue
        try:
            identity = process_identity(metadata[0])
        except AcceptanceFailure:
            uncertain = True
            continue
        if identity is not None:
            try:
                matches_run = process_has_run_identity(identity.pid, identity)
            except (ProcessObservationTransient, AcceptanceFailure):
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


def observed_process_exists(expected):
    try:
        current = process_identity(expected.pid)
    except AcceptanceFailure:
        return True
    return current is not None and current.stable_key() == expected.stable_key()


def signal_identity(expected, signal_number):
    try:
        current = process_identity(expected.pid)
    except AcceptanceFailure:
        return
    if current is None or current.stable_key() != expected.stable_key():
        return
    try:
        os.kill(current.pid, signal_number)
    except ProcessLookupError:
        pass


def signal_observed(observed, signal_number):
    own_process_group = os.getpgrp()
    signaled_groups = set()
    for expected in observed.values():
        try:
            current = process_identity(expected.pid)
        except AcceptanceFailure:
            continue
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


def terminate_observed(observed, deadline):
    signal_observed(observed, signal.SIGTERM)
    for _ in range(10):
        if not any(observed_process_exists(item) for item in observed.values()):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    signal_observed(observed, signal.SIGKILL)
    for _ in range(20):
        if not any(observed_process_exists(item) for item in observed.values()):
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


def terminate_processes(child, root_identity, observed, deadline):
    signal_identity(root_identity, signal.SIGTERM)
    signal_observed(observed, signal.SIGTERM)
    for _ in range(10):
        if child.poll() is not None or time.monotonic() >= deadline:
            break
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    signal_identity(root_identity, signal.SIGKILL)
    signal_observed(observed, signal.SIGKILL)
    if child.poll() is None:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AcceptanceFailure("CLI process cleanup exceeded its deadline") from None
    stabilize_descendants(root_identity, observed, deadline)
    terminate_observed(observed, deadline)
    if any(observed_process_exists(item) for item in observed.values()):
        raise AcceptanceFailure("CLI descendant cleanup was incomplete")


def cli_environment(case_name):
    allowed = {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
    environment = {key: value for key, value in os.environ.items() if key in allowed}
    for key, value in os.environ.items():
        if key.startswith("FAKE_KUNJIN_"):
            environment[key] = value
    environment[RUN_ID_ENVIRONMENT] = run_identity
    if mode == "public":
        case_root = runtime / ("case-" + case_name)
        data_dir = case_root / "data"
        state_dir = case_root / "state"
        pycache_dir = case_root / "pycache"
        for directory in (data_dir, state_dir, pycache_dir):
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
        environment["KUNJIN_DATA_DIR"] = str(data_dir)
        environment["KUNJIN_STATE_DIR"] = str(state_dir)
        environment["PYTHONPYCACHEPREFIX"] = str(pycache_dir)
    else:
        for key in ("KUNJIN_DATA_DIR", "KUNJIN_STATE_DIR", "PYTHONPYCACHEPREFIX"):
            if key in os.environ:
                environment[key] = os.environ[key]
    return environment


def create_public_capability(environment, name, arguments):
    if not (
        mode == "public"
        and arguments[:3] == ["--json", "fund", "brief"]
        and len(arguments) >= 4
    ):
        return None
    code = arguments[3]
    if FUND_CODE.fullmatch(code) is None:
        raise AcceptanceFailure(name + " capability fund code is invalid")
    state_dir = Path(environment["KUNJIN_STATE_DIR"])
    fixture_path = state_dir / (name + ".fixture")
    fixture = json.dumps(
        {
            "contract": ACCEPTANCE_FIXTURE_CONTRACT,
            "fund_code": code,
            "run_id": run_identity,
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    write_fd = os.open(
        fixture_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        if os.write(write_fd, fixture) != len(fixture):
            raise AcceptanceFailure(name + " capability fixture write was incomplete")
        os.fsync(write_fd)
    finally:
        os.close(write_fd)
    fixture_fd = os.open(fixture_path, os.O_RDONLY | os.O_NOFOLLOW)
    fixture_path.unlink()
    marker_read_fd, marker_write_fd = os.pipe()
    environment["KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION"] = "synthetic_non_personal"
    environment["KUNJIN_PHASE1_PUBLIC_FIXTURE_FD"] = str(fixture_fd)
    environment["KUNJIN_PHASE1_PUBLIC_MARKER_FD"] = str(marker_write_fd)
    return fixture_fd, marker_read_fd, marker_write_fd, code


def validate_public_marker(marker_read_fd, expected_code, name):
    chunks = []
    remaining = 2049
    while remaining > 0:
        chunk = os.read(marker_read_fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if not raw or len(raw) > 2048 or os.read(marker_read_fd, 1):
        raise AcceptanceFailure(name + " synthetic marker size is invalid")
    try:
        marker = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise AcceptanceFailure(name + " synthetic marker is invalid") from None
    exact_dict(marker, ACCEPTANCE_MARKER_KEYS, name + " synthetic marker")
    canonical = json.dumps(
        marker,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if canonical != raw:
        raise AcceptanceFailure(name + " synthetic marker is not canonical")
    if (
        marker["schema_version"] != 1
        or marker["contract"] != ACCEPTANCE_MARKER_CONTRACT
        or marker["fund_code"] != expected_code
        or marker["run_id"] != run_identity
        or marker["observation_version"] != "synthetic_non_personal_v1"
        or CHECKSUM.fullmatch(marker["payload_sha256"]) is None
        or REQUEST_ID.fullmatch(marker["request_id"]) is None
        or type(marker["source_attempt_id"]) is not int
        or marker["source_attempt_id"] <= 0
    ):
        raise AcceptanceFailure(name + " synthetic marker identity is invalid")


command_elapsed = {}


def run_command(name, arguments, case_name):
    require_time(name)
    raw_path = runtime / (name + ".raw.json")
    stderr_path = runtime / (name + ".stderr")
    command_started = time.monotonic()
    remaining = hard_deadline - command_started
    cleanup_reserve = min(0.5, max(0.05, remaining / 4))
    wait_deadline = hard_deadline - cleanup_reserve
    child = None
    root_identity = None
    observed = {}
    marker_read_fd = None
    fixture_fd = None
    marker_write_fd = None
    try:
        with raw_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(raw_path, 0o600)
            os.chmod(stderr_path, 0o600)
            environment = cli_environment(case_name)
            capability = create_public_capability(environment, name, arguments)
            pass_fds = ()
            expected_capability_code = None
            if capability is not None:
                fixture_fd, marker_read_fd, marker_write_fd, expected_capability_code = capability
                pass_fds = (fixture_fd, marker_write_fd)
            try:
                child = subprocess.Popen(
                    [cli, *arguments],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    env=environment,
                    pass_fds=pass_fds,
                )
            finally:
                if fixture_fd is not None:
                    os.close(fixture_fd)
                    fixture_fd = None
                if marker_write_fd is not None:
                    os.close(marker_write_fd)
                    marker_write_fd = None
            if child.stdout is None or child.stderr is None:
                raise AcceptanceFailure(name + " output pipes are unavailable")
            root_identity = process_identity(child.pid)
            if root_identity is None:
                raise AcceptanceFailure(name + " process identity is unavailable")
            selector = selectors.DefaultSelector()
            streams = (
                (child.stdout, stdout, MAX_RAW_BYTES, "stdout"),
                (child.stderr, stderr, MAX_STDERR_BYTES, "stderr"),
            )
            sizes = {"stdout": 0, "stderr": 0}
            for stream, sink, limit, stream_name in streams:
                os.set_blocking(stream.fileno(), False)
                selector.register(
                    stream,
                    selectors.EVENT_READ,
                    (sink, limit, stream_name),
                )
            while child.poll() is None or selector.get_map():
                observe_descendants(root_identity, observed)
                for key, _events in selector.select(timeout=0.005):
                    sink, limit, stream_name = key.data
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    sizes[stream_name] += len(chunk)
                    if sizes[stream_name] > limit:
                        raise AcceptanceFailure(
                            name + " " + stream_name + " exceeded its size limit"
                        )
                    sink.write(chunk)
                if time.monotonic() >= wait_deadline:
                    raise AcceptanceFailure(name + " reached the global acceptance deadline")
            selector.close()
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
            stabilize_descendants(
                root_identity, observed, min(hard_deadline, time.monotonic() + cleanup_reserve)
            )
            if any(observed_process_exists(item) for item in observed.values()):
                raise AcceptanceFailure(name + " left a detached descendant")
            if child.returncode != 0:
                raise AcceptanceFailure(name + " returned a non-zero process exit")
            if not 0 < sizes["stdout"] <= MAX_RAW_BYTES:
                raise AcceptanceFailure(name + " JSON size is invalid")
            if sizes["stderr"] > MAX_STDERR_BYTES:
                raise AcceptanceFailure(name + " stderr exceeded its size limit")
            if marker_read_fd is not None:
                validate_public_marker(marker_read_fd, expected_capability_code, name)
                os.close(marker_read_fd)
                marker_read_fd = None
        command_elapsed[name] = round(time.monotonic() - command_started, 3)
        return raw_path
    except BaseException:
        if root_identity is not None:
            try:
                observe_descendants(root_identity, observed, full_scan=True)
            except (AcceptanceFailure, ProcessObservationTransient):
                pass
        if child is not None and root_identity is not None:
            try:
                terminate_processes(
                    child, root_identity, observed,
                    min(hard_deadline, time.monotonic() + max(0.1, cleanup_reserve)),
                )
            except AcceptanceFailure:
                pass
        for descriptor in (fixture_fd, marker_write_fd, marker_read_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise


def load_json_envelope(path, expected_command):
    require_time(expected_command + " validation")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder()
        payload, end = decoder.raw_decode(text)
        if text[end:].strip():
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise AcceptanceFailure(expected_command + " is not one UTF-8 JSON document") from None
    exact_dict(payload, ENVELOPE_KEYS, expected_command + " envelope")
    if payload["schema_version"] != "1" or payload["command"] != expected_command:
        raise AcceptanceFailure(expected_command + " envelope identity is invalid")
    utc_text(payload["as_of"], expected_command + " as_of")
    exact_list(payload["warnings"], expected_command + " warnings")
    if payload["errors"] != []:
        raise AcceptanceFailure(expected_command + " returned command errors")
    return payload


def load_envelope(path, expected_code):
    payload = load_json_envelope(path, "fund.brief")
    data = exact_dict(payload["data"], DATA_KEYS, "fund brief data")
    subject = exact_dict(data["subject"], SUBJECT_KEYS, "brief subject")
    if subject["fund_code"] != expected_code:
        raise AcceptanceFailure("brief subject fund code is invalid")
    return data


def project_source_status(path, expected_code):
    payload = load_json_envelope(path, "source.status")
    data = exact_dict(payload["data"], SOURCE_DATA_KEYS, "source status data")
    if data["fund_code"] != expected_code or data["mode"] != "rapid":
        raise AcceptanceFailure("source status subject or mode is invalid")
    checksum(data["policy_checksum"], "source policy checksum")
    checksum(data["registry_checksum"], "source registry checksum")
    identifier(data["policy_version"], "source policy version")
    identifier(data["registry_version"], "source registry version")
    utc_text(data["snapshot_at"], "source snapshot")
    if type(data["request_id"]) is not str or not re.fullmatch(
        r"[0-9a-f]{32}", data["request_id"], re.ASCII
    ):
        raise AcceptanceFailure("source request id is invalid")
    resolutions = {}
    for value in exact_list(data["request_field_resolutions"], "source resolutions"):
        item = exact_dict(value, RESOLUTION_KEYS, "source resolution")
        field_id = identifier(item["field_id"], "source resolution field")
        if item["action"] != "fact_research" or item["risk_effect"] != "information":
            raise AcceptanceFailure("source resolution action contract is invalid")
        if field_id in resolutions:
            raise AcceptanceFailure("source resolutions contain duplicate fields")
        resolutions[field_id] = {
            "field_id": field_id,
            "primary_source_id": identifier(
                item["primary_source_id"], "source resolution primary"
            ),
            "resolution": identifier(item["resolution"], "source resolution state"),
        }
    projected_fields = []
    fields_by_identity = {}
    for value in exact_list(data["source_fields"], "source fields"):
        item = exact_dict(value, SOURCE_FIELD_KEYS, "source field")
        field_id = identifier(item["field_id"], "source field id")
        source_id = identifier(item["source_id"], "source id")
        supplementation = item["supplementation"]
        projected_supplementation = None
        if supplementation is not None:
            supplementation = exact_dict(
                supplementation, SUPPLEMENTATION_KEYS, "source supplementation"
            )
            projected_supplementation = {
                "accepted_input": [
                    public_text(entry, "supplement accepted input")
                    for entry in exact_list(
                        supplementation["accepted_input"], "supplement accepted input"
                    )
                ],
                "impact_if_missing": public_text(
                    supplementation["impact_if_missing"], "supplement impact"
                ),
                "missing_item": identifier(
                    supplementation["missing_item"], "supplement missing item"
                ),
                "suggested_location": public_text(
                    supplementation["suggested_location"], "supplement location"
                ),
                "supported_without_it": public_text(
                    supplementation["supported_without_it"], "supplement supported scope"
                ),
                "unsupported_without_it": public_text(
                    supplementation["unsupported_without_it"], "supplement unsupported scope"
                ),
            }
            public_text(supplementation["freshness_requirement"], "supplement freshness")
            public_text(supplementation["why_required"], "supplement rationale")
        projected = {
            "field_id": field_id,
            "source_id": source_id,
            "source_tier": identifier(item["source_tier"], "source tier"),
            "state": identifier(item["state"], "source state"),
            "supplementation": projected_supplementation,
        }
        if projected["source_tier"] not in SOURCE_TIERS:
            raise AcceptanceFailure("source field tier is invalid")
        identity = (source_id, field_id)
        if identity in fields_by_identity:
            raise AcceptanceFailure("source fields contain a duplicate identity")
        fields_by_identity[identity] = projected
        projected_fields.append(projected)
    supplementation_identities = set()
    for resolution in resolutions.values():
        if resolution["resolution"] not in {"manual_supplement_required", "partial"}:
            continue
        identity = (resolution["primary_source_id"], resolution["field_id"])
        source = fields_by_identity.get(identity)
        allowed_states = (
            {"unsupported", "unavailable"}
            if resolution["resolution"] == "manual_supplement_required"
            else {"not_checked", "degraded", "cooldown", "unsupported", "unavailable"}
        )
        if source is None:
            raise AcceptanceFailure("source resolution primary identity is missing")
        if source["source_tier"] != "tier_1":
            continue
        if (
            source["state"] not in allowed_states
            or source["supplementation"] is None
        ):
            raise AcceptanceFailure("supplementation is not bound to its unresolved Tier 1 source")
        if source["supplementation"]["missing_item"] != resolution["field_id"]:
            raise AcceptanceFailure("manual supplementation missing item is not controlled")
        source["supplementation"]["missing_item"] = SUPPLEMENT_MISSING_ITEM.get(
            resolution["field_id"],
            resolution["field_id"],
        )
        supplementation_identities.add(identity)
    for source in projected_fields:
        if (source["source_id"], source["field_id"]) not in supplementation_identities:
            source["supplementation"] = None
    return {
        "fund_code": expected_code,
        "mode": "rapid",
        "request_field_resolutions": list(resolutions.values()),
        "source_fields": projected_fields,
    }


def project_status(value, label):
    status = exact_dict(value, STATUS_KEYS, label)
    if status["state"] not in EVIDENCE_STATES:
        raise AcceptanceFailure(label + " state is invalid")
    projected = {"state": status["state"]}
    for key in STATUS_KEYS - {"state"}:
        projected[key] = identifier_list(status[key], label + " " + key)
    gap_fields = set(
        projected["missing_fields"]
        + projected["stale_fields"]
        + projected["conflicted_fields"]
        + projected["unsupported_fields"]
        + projected["cooldown_fields"]
    )
    if projected["state"] == "complete" and gap_fields:
        raise AcceptanceFailure(label + " complete state conceals evidence gaps")
    return projected


def project_coverage(value, label):
    coverage = exact_dict(value, COVERAGE_KEYS, label)
    identifier(coverage["coverage_id"], label + " id")
    identifier(coverage["scope"], label + " scope")
    if coverage["evidence_state"] not in EVIDENCE_STATES:
        raise AcceptanceFailure(label + " state is invalid")
    for key in ("included_fund_codes", "omitted_fund_codes"):
        for code in exact_list(coverage[key], label + " " + key):
            if type(code) is not str or FUND_CODE.fullmatch(code) is None:
                raise AcceptanceFailure(label + " contains an invalid fund code")
    known_percent = coverage["known_percent"]
    if known_percent is not None:
        if type(known_percent) is not str:
            raise AcceptanceFailure(label + " known percent is invalid")
        try:
            numeric_percent = float(known_percent)
        except ValueError:
            raise AcceptanceFailure(label + " known percent is invalid") from None
        if not 0 <= numeric_percent <= 100:
            raise AcceptanceFailure(label + " known percent is outside its range")
    return {
        "coverage_id": coverage["coverage_id"],
        "evidence_state": coverage["evidence_state"],
        "known_percent": known_percent,
        "scope": coverage["scope"],
        "unknown_fields": identifier_list(coverage["unknown_fields"], label + " unknown"),
    }


def coverage_class(coverage):
    if coverage["evidence_state"] == "complete":
        return "complete"
    if coverage["evidence_state"] == "insufficient":
        return "unknown"
    return "partial"


def conservative_coverage_class(*coverages):
    ranks = {"complete": 2, "partial": 1, "unknown": 0}
    values = [coverage_class(item) for item in coverages]
    return min(values, key=ranks.__getitem__)


def project_fact_value(field_id, value, label):
    if field_id == "formal_nav":
        if type(value) is not str:
            raise AcceptanceFailure(label + " formal NAV value is invalid")
        return public_text(value, label + " formal NAV")
    expected_keys = FACT_VALUE_KEYS.get(field_id)
    if expected_keys is None:
        scan_public_tree(value, label + " unprojected value")
        return {"available": True, "projection": "omitted_by_public_allowlist"}
    item = exact_dict(value, expected_keys, label + " value")
    if field_id == "holdings_industries":
        holdings = exact_list(item["items"], label + " holdings")
        for holding in holdings:
            exact_dict(holding, HOLDING_ITEM_KEYS, label + " holding")
    return scan_public_tree(item, label + " value")


def project_relationship_metrics(relationship_type, value):
    expected = RELATIONSHIP_METRIC_KEYS.get(relationship_type)
    if expected is None:
        raise AcceptanceFailure("relationship type is outside the public allowlist")
    metrics = exact_dict(value, expected, "relationship metrics")
    scan_public_tree(metrics, "relationship metrics")
    if relationship_type == "adjusted_return_correlation":
        return {key: metrics[key] for key in sorted(CORRELATION_PUBLIC_METRICS)}
    return {key: metrics[key] for key in sorted(metrics)}


def project_fact(value, label):
    fact = exact_dict(value, FACT_KEYS, label)
    for key in ("fact_id", "field_id", "source_id", "source_lineage_id"):
        identifier(fact[key], label + " " + key)
    if fact["source_tier"] not in SOURCE_TIERS:
        raise AcceptanceFailure(label + " source tier is invalid")
    for key in ("data_as_of", "published_at"):
        utc_text(fact[key], label + " " + key, optional=True)
    utc_text(fact["retrieved_at"], label + " retrieved_at")
    public_text(fact["publisher"], label + " publisher")
    parsed = urlparse(fact["canonical_url"])
    if parsed.scheme != "https" or not parsed.netloc:
        raise AcceptanceFailure(label + " URL is invalid")
    public_text(fact["canonical_url"], label + " URL")
    conflicts = identifier_list(fact["conflict_ids"], label + " conflicts")
    projected_value = project_fact_value(fact["field_id"], fact["value"], label)
    return {
        "canonical_url": fact["canonical_url"],
        "completeness": identifier(fact["completeness"], label + " completeness"),
        "conflict_ids": conflicts,
        "data_as_of": fact["data_as_of"],
        "fact_id": fact["fact_id"],
        "field_id": fact["field_id"],
        "freshness": identifier(fact["freshness"], label + " freshness"),
        "published_at": fact["published_at"],
        "publisher": fact["publisher"],
        "source_id": fact["source_id"],
        "source_tier": fact["source_tier"],
        "value": projected_value,
    }


def project_interpretation(value, label):
    item = exact_dict(value, INTERPRETATION_KEYS, label)
    action_id = identifier(item["action_id"], label + " action")
    if item["state"] not in ACTION_STATES or item["action_maturity"] not in MATURITIES:
        raise AcceptanceFailure(label + " state or maturity is invalid")
    if item["exact_amount_available"] is not False:
        raise AcceptanceFailure(label + " exposed an exact amount")
    if item["state"] == "no_add" and item["action_maturity"] != "mature":
        raise AcceptanceFailure(label + " no_add maturity is invalid")
    if item["state"] in {"hold", "watch", "abstain"} and (
        item["action_maturity"] != "experimental_shadow"
    ):
        raise AcceptanceFailure(label + " experimental state was promoted to mature")
    state_inputs = scan_public_tree(item["state_inputs"], label + " state inputs")
    if item["state"] == "hold" and state_inputs.get("owner_confirmed_thesis") is not True:
        raise AcceptanceFailure(label + " inferred hold without an owner-confirmed thesis")
    unavailable = identifier_list(item["unavailable_actions"], label + " unavailable")
    if "exact_amount" not in unavailable:
        raise AcceptanceFailure(label + " does not preserve the exact-output boundary")
    if action_id in {"reduce_to_cash", "full_exit", "switch_reduce", "switch_buy"} and (
        "automatic_trade" not in unavailable
    ):
        raise AcceptanceFailure(label + " does not forbid automatic trading")
    if action_id in {"reduce_to_cash", "full_exit", "switch_reduce"} and item["state"] not in {
        "reduce_or_exit_review", "abstain",
    }:
        raise AcceptanceFailure(label + " risk-reducing action has an invalid state")
    supporting = identifier_list(
        item["supporting_evidence_ids"], label + " supporting evidence"
    )
    opposing = identifier_list(
        item["opposing_evidence_ids"], label + " opposing evidence"
    )
    return {
        "action_id": action_id,
        "action_maturity": item["action_maturity"],
        "blocking_codes": identifier_list(item["blocking_codes"], label + " blocking"),
        "exact_amount_available": False,
        "invalidation_conditions": [
            public_text(text, label + " invalidation")
            for text in exact_list(item["invalidation_conditions"], label + " invalidations")
        ],
        "missing_fields": identifier_list(item["missing_fields"], label + " missing"),
        "opposing_evidence_ids": opposing,
        "state": item["state"],
        "supporting_evidence_ids": supporting,
        "unavailable_actions": unavailable,
    }


SAFE_STATE_TEXTS = {
    "reduce_or_exit_review": {
        (
            "active 清盘或终止正式公告触发减仓或退出复核"
            "（reduce_or_exit_review）；这不是立即赎回指令。"
        ),
        (
            "本次规则结果进入减仓或退出复核流程（reduce_or_exit_review）；"
            "不表示系统发现了确定卖出信号，也不是立即赎回指令。"
        ),
    },
    "no_add": {
        "当前仅支持暂不新增风险（no_add）。这是财务安全闸门限制，不代表应持有或卖出。"
    },
    "hold": {
        "本次已核验信息未触发已确认的持有理由失效条件（hold）；"
        "这是实验性观察，不是确定持有建议。"
    },
    "watch": {
        "本次规则结果为继续观察（watch）；现有证据不足以形成确定的持有、减仓或退出结论。"
    },
    "abstain": {
        "本次暂不形成行动倾向（abstain）；请先处理列示的证据缺口、冲突或交易限制。"
    },
}
REDEMPTION_RESTRICTION_CODE = "redemption_restriction_notice"
ITEM_REDEMPTION_SUFFIX = (
    " 因当前存在赎回限制，当前不能形成可执行赎回安排；"
    "这不表示永久无法赎回，需以限制解除后的正式信息重新评估。"
)
TRIGGERED_REVIEW_SUFFIX = " 同时存在正式公告触发的退出复核，但不等于立即卖出。"
SINGLE_RESTRICTION_SUFFIX = (
    " 因当前存在赎回限制，当前不能形成可执行赎回安排；这不表示永久无法赎回。"
)
MULTI_RESTRICTION_SUFFIX = " 一个或多个动作当前存在执行限制，必须查看分腿结论。"


def safe_item_texts(state, action_id, blocking_codes):
    texts = set(SAFE_STATE_TEXTS[state])
    if REDEMPTION_RESTRICTION_CODE in blocking_codes:
        texts = {text + ITEM_REDEMPTION_SUFFIX for text in texts}
    if action_id == "switch_reduce":
        texts = {"转出腿：" + text for text in texts}
    elif action_id == "switch_buy":
        texts = {"转入腿：" + text + " 不得从转出腿继承许可。" for text in texts}
    return texts


def safe_top_texts(state, triggered_reviews, interpretations):
    texts = set(SAFE_STATE_TEXTS[state])
    if triggered_reviews:
        texts = {text + TRIGGERED_REVIEW_SUFFIX for text in texts}
    restricted = [
        item["action_id"]
        for item in interpretations
        if REDEMPTION_RESTRICTION_CODE in item["blocking_codes"]
    ]
    if len(interpretations) == 1 and restricted:
        texts = {text + SINGLE_RESTRICTION_SUFFIX for text in texts}
    elif restricted:
        texts = {text + MULTI_RESTRICTION_SUFFIX for text in texts}
    return texts


def conditional_financial_text(value, label, allowed_texts):
    text = public_text(value, label)
    if text not in allowed_texts:
        raise AcceptanceFailure(label + " is outside the state-bound safe wording contract")
    return text


def project_brief(path, expected_code, expected_action):
    data = load_envelope(path, expected_code)
    request = exact_dict(data["request"], REQUEST_KEYS, "brief request")
    expected_action_ids = {
        "continue_holding": ["fact_research", "continue_holding"],
        "reduce_to_cash": ["fact_research", "reduce_to_cash"],
        "full_exit": ["fact_research", "full_exit"],
        "switch_funds": ["fact_research", "switch_reduce", "switch_buy"],
    }[expected_action]
    if request["action_ids"] != expected_action_ids or request["mode"] != "rapid":
        raise AcceptanceFailure("brief request action or mode is invalid")
    if request["terminal_status"] not in TERMINAL_STATES:
        raise AcceptanceFailure("brief terminal status is invalid")
    omitted_work = identifier_list(request["omitted_work"], "brief omitted work")
    if (request["terminal_status"] == "complete") == bool(omitted_work):
        raise AcceptanceFailure("brief terminal status and omitted work conflict")
    checksum(request["result_checksum"], "brief result checksum")
    checksum(request["evidence_fingerprint"], "brief evidence fingerprint")
    utc_text(request["created_at"], "brief creation time")

    subject = data["subject"]
    if subject["portfolio_evidence_state"] not in {"current", "dated", "unknown"}:
        raise AcceptanceFailure("portfolio evidence state is invalid")
    if subject["position_present"] is not None and type(subject["position_present"]) is not bool:
        raise AcceptanceFailure("position presence is invalid")
    if subject["portfolio_weight"] is not None and type(subject["portfolio_weight"]) is not str:
        raise AcceptanceFailure("portfolio weight is invalid")
    identifier(subject["observation_version"], "portfolio observation version")
    if mode == "public" and subject["observation_version"] != "synthetic_non_personal_v1":
        raise AcceptanceFailure("public acceptance lacks authenticated synthetic portfolio evidence")

    all_facts = [
        project_fact(item, "fact") for item in exact_list(data["facts"], "brief facts")
    ]
    if len({item["fact_id"] for item in all_facts}) != len(all_facts):
        raise AcceptanceFailure("brief fact ids are duplicated")
    facts = [
        item
        for item in all_facts
        if item["source_tier"] in {"tier_1", "tier_2"}
        and re.match(r"^fund_[0-9]{6}_", item["fact_id"], re.ASCII) is None
    ]

    events = []
    for value in exact_list(data["official_events"], "official events"):
        event = exact_dict(value, EVENT_KEYS, "official event")
        if event["source_tier"] != "tier_1":
            raise AcceptanceFailure("official event source tier is invalid")
        events.append({
            "affected_action_ids": identifier_list(
                event["affected_action_ids"], "event affected actions"
            ),
            "event_code": identifier(event["event_code"], "event code"),
            "integrity_status": identifier(event["integrity_status"], "event integrity"),
            "published_at": utc_text(event["published_at"], "event publication"),
            "publisher": public_text(event["publisher"], "event publisher"),
            "source_tier": event["source_tier"],
            "title": public_text(event["title"], "event title"),
        })

    portfolio = exact_dict(data["portfolio_relationship"], PORTFOLIO_KEYS, "portfolio")
    minimum_coverage = project_coverage(
        portfolio["minimum_relationship_coverage"], "minimum relationship coverage"
    )
    holdings_coverage = project_coverage(
        portfolio["disclosed_holdings_coverage"], "holdings coverage"
    )
    relationships = []
    for value in exact_list(portfolio["relationships"], "relationships"):
        item = exact_dict(value, RELATIONSHIP_KEYS, "relationship")
        relationship_type = identifier(item["relationship_type"], "relationship type")
        relationships.append({
            "evidence_state": identifier(item["evidence_state"], "relationship state"),
            "metrics": project_relationship_metrics(relationship_type, item["metrics"]),
            "publication_times": [
                utc_text(entry, "relationship publication")
                for entry in exact_list(item["publication_times"], "relationship publications")
            ],
            "relationship_type": relationship_type,
            "report_periods": [
                public_text(entry, "relationship report period")
                for entry in exact_list(item["report_periods"], "relationship periods")
            ],
        })

    sync_status = project_status(data["sync_status"], "sync status")
    decision_status = project_status(
        data["decision_evidence_status"], "decision evidence status"
    )
    action = exact_dict(data["action_interpretation"], ACTION_KEYS, "action interpretation")
    if action["primary_state"] not in ACTION_STATES or action["action_maturity"] not in MATURITIES:
        raise AcceptanceFailure("primary action state is invalid")
    interpretations = [
        project_interpretation(item, "interpretation")
        for item in exact_list(action["interpretations"], "interpretations")
    ]
    if [item["action_id"] for item in interpretations] != expected_action_ids[1:]:
        raise AcceptanceFailure("brief interpretations do not match the routed action")
    affected_abstentions = identifier_list(
        action["affected_action_abstentions"], "affected action abstentions"
    )
    if expected_action == "switch_funds":
        buy_leg = interpretations[1]
        if (
            buy_leg["state"] != "abstain"
            or buy_leg["action_maturity"] != "experimental_shadow"
            or "switch_buy" not in affected_abstentions
            or not {"d3_missing", "post_trade_missing"}.issubset(buy_leg["blocking_codes"])
            or not {"d3", "post_trade"}.issubset(buy_leg["missing_fields"])
        ):
            raise AcceptanceFailure("switch buy leg is not independently fail-closed")
    if any("phase_b_blocked" in item["blocking_codes"] for item in interpretations):
        expected_primary_state = "no_add"
        expected_primary_maturity = "mature"
    else:
        primary = next(
            (
                item
                for item in interpretations
                if item["state"] == "reduce_or_exit_review"
            ),
            interpretations[0],
        )
        expected_primary_state = primary["state"]
        expected_primary_maturity = primary["action_maturity"]
    if (
        action["primary_state"] != expected_primary_state
        or action["action_maturity"] != expected_primary_maturity
    ):
        raise AcceptanceFailure("top-level action state does not follow brief precedence")
    top_blocking_codes = identifier_list(action["blocking_codes"], "action blocking")
    required_blocking_codes = {
        code for item in interpretations for code in item["blocking_codes"]
    }
    if not required_blocking_codes.issubset(top_blocking_codes):
        raise AcceptanceFailure("top-level action concealed a leg blocking code")

    gaps = []
    for value in exact_list(data["missing_evidence"], "missing evidence"):
        gap = exact_dict(value, GAP_KEYS, "missing evidence item")
        gaps.append({
            "affected_action_ids": identifier_list(
                gap["affected_action_ids"], "gap affected actions"
            ),
            "condition": identifier(gap["condition"], "gap condition"),
            "field_id": identifier(gap["field_id"], "gap field"),
            "scope": identifier(gap["scope"], "gap scope"),
        })
    beginner = exact_dict(data["beginner_explanation_zh"], BEGINNER_KEYS, "beginner output")
    projected_beginner = {}
    for section_name, section_keys in BEGINNER_SECTION_KEYS.items():
        section = exact_dict(beginner[section_name], section_keys, section_name)
        scan_public_tree(section, section_name)
        projected_beginner[section_name] = section
    beginner_gaps = []
    for value in exact_list(beginner["evidence_gaps"]["items"], "beginner gaps"):
        item = exact_dict(value, BEGINNER_GAP_KEYS, "beginner gap")
        projected_item = {
            "affected_action_ids": identifier_list(
                item["affected_action_ids"], "beginner gap affected actions"
            ),
            "condition": identifier(item["condition"], "beginner gap condition"),
            "field_id": identifier(item["field_id"], "beginner gap field"),
            "label_zh": public_text(item["label_zh"], "beginner gap label"),
            "scope": identifier(item["scope"], "beginner gap scope"),
            "source_resolution": None,
            "supplementation": None,
            "next_step": None,
        }
        next_step = exact_dict(
            item["next_step"], BEGINNER_NEXT_STEP_KEYS, "beginner gap next step"
        )
        projected_item["next_step"] = {
            "action": public_text(next_step["action"], "beginner gap next action"),
            "status": identifier(next_step["status"], "beginner gap next status"),
        }
        resolution = item["source_resolution"]
        if resolution is not None:
            resolution = exact_dict(
                resolution,
                BEGINNER_SOURCE_RESOLUTION_KEYS,
                "beginner source resolution",
            )
            projected_resolution = {
                "acceptable_alternative_ids": identifier_list(
                    resolution["acceptable_alternative_ids"],
                    "beginner acceptable alternatives",
                ),
                "primary_source_id": identifier(
                    resolution["primary_source_id"], "beginner primary source"
                ),
                "resolution": identifier(
                    resolution["resolution"], "beginner resolution"
                ),
                "source_field_id": identifier(
                    resolution["source_field_id"], "beginner source field"
                ),
                "source_states": identifier_list(
                    resolution["source_states"], "beginner source states"
                ),
            }
            if projected_item["field_id"] == "official_events" and (
                projected_resolution["source_field_id"]
                != "fund_manager_product_announcement"
            ):
                raise AcceptanceFailure("official event gap uses an invalid source field")
            projected_item["source_resolution"] = projected_resolution
        supplementation = item["supplementation"]
        if supplementation is not None:
            supplementation = exact_dict(
                supplementation,
                SUPPLEMENTATION_KEYS,
                "beginner supplementation",
            )
            projected_supplementation = {
                "accepted_input": [
                    public_text(entry, "beginner accepted input")
                    for entry in exact_list(
                        supplementation["accepted_input"], "beginner accepted input"
                    )
                ],
                "freshness_requirement": public_text(
                    supplementation["freshness_requirement"],
                    "beginner supplement freshness",
                ),
                "impact_if_missing": public_text(
                    supplementation["impact_if_missing"], "beginner supplement impact"
                ),
                "missing_item": identifier(
                    supplementation["missing_item"], "beginner supplement missing item"
                ),
                "suggested_location": public_text(
                    supplementation["suggested_location"], "beginner supplement location"
                ),
                "supported_without_it": public_text(
                    supplementation["supported_without_it"], "beginner supported scope"
                ),
                "unsupported_without_it": public_text(
                    supplementation["unsupported_without_it"],
                    "beginner unsupported scope",
                ),
                "why_required": public_text(
                    supplementation["why_required"], "beginner supplement rationale"
                ),
            }
            projected_item["supplementation"] = projected_supplementation
        resolution = projected_item["source_resolution"]
        if resolution is None and projected_item["supplementation"] is not None:
            raise AcceptanceFailure("beginner supplementation lacks a source resolution")
        if resolution is not None:
            manual = resolution["resolution"] == "manual_supplement_required"
            if resolution["resolution"] == "usable":
                raise AcceptanceFailure("beginner evidence gap cannot be labeled usable")
            if manual != (projected_item["supplementation"] is not None):
                raise AcceptanceFailure("beginner manual resolution and supplementation conflict")
            if manual and projected_item["next_step"]["status"] != resolution["resolution"]:
                raise AcceptanceFailure("beginner manual gap has a conflicting next step")
            if projected_item["supplementation"] is not None and (
                projected_item["supplementation"]["missing_item"]
                != resolution["source_field_id"]
            ):
                raise AcceptanceFailure("beginner supplementation is spliced from another field")
        beginner_gaps.append(projected_item)
    if [
        {
            "affected_action_ids": item["affected_action_ids"],
            "condition": item["condition"],
            "field_id": item["field_id"],
            "scope": item["scope"],
        }
        for item in beginner_gaps
    ] != gaps:
        raise AcceptanceFailure("beginner gaps do not exactly preserve top-level gaps")
    projected_beginner["evidence_gaps"] = {
        **projected_beginner["evidence_gaps"],
        "items": beginner_gaps,
    }
    headline = exact_dict(beginner["headline"], HEADLINE_KEYS, "headline")
    scan_public_tree(headline, "headline")
    projected_beginner["headline"] = headline
    triggered_reviews = identifier_list(action["triggered_reviews"], "triggered reviews")
    headline_text = conditional_financial_text(
        headline["text"],
        "beginner headline",
        safe_top_texts(action["primary_state"], triggered_reviews, interpretations),
    )
    maturity_text = public_text(headline["maturity_text"], "headline maturity")
    if headline["action_maturity"] not in MATURITIES:
        raise AcceptanceFailure("headline maturity is invalid")
    if headline["action_maturity"] != action["action_maturity"]:
        raise AcceptanceFailure("headline maturity conflicts with the action result")
    if headline["primary_state"] != action["primary_state"]:
        raise AcceptanceFailure("headline primary state conflicts with the action result")
    expected_maturity_scope = (
        "primary_state_only" if expected_action == "switch_funds" else "all_actions"
    )
    if headline["maturity_scope"] != expected_maturity_scope:
        raise AcceptanceFailure("headline maturity scope is invalid")
    headline_items = []
    for value in exact_list(headline["items"], "headline items"):
        item = exact_dict(value, HEADLINE_ITEM_KEYS, "headline item")
        headline_items.append(item)
    if [item["action_id"] for item in headline_items] != [
        item["action_id"] for item in interpretations
    ]:
        raise AcceptanceFailure("headline items do not preserve routed action legs")
    for headline_item, interpretation in zip(headline_items, interpretations):
        if (
            headline_item["state"] != interpretation["state"]
            or headline_item["action_maturity"] != interpretation["action_maturity"]
        ):
            raise AcceptanceFailure("headline item conflicts with its action interpretation")
        conditional_financial_text(
            headline_item["text"],
            "headline item text",
            safe_item_texts(
                interpretation["state"],
                interpretation["action_id"],
                interpretation["blocking_codes"],
            ),
        )
    if headline["action_maturity"] == "experimental_shadow" and "不授权交易" not in maturity_text:
        raise AcceptanceFailure("experimental maturity text does not preserve the trade boundary")

    projected_events_by_action = {}
    for event in events:
        if event["integrity_status"] != "active":
            continue
        for action_id in event["affected_action_ids"]:
            projected_events_by_action.setdefault(action_id, []).append(event)
    for interpretation in interpretations:
        if (
            interpretation["state"] == "reduce_or_exit_review"
            and interpretation["action_maturity"] == "mature"
        ):
            hard_events = projected_events_by_action.get(interpretation["action_id"], ())
            if not any(
                event["event_code"] in {
                    "fund_liquidation_notice", "fund_termination_notice",
                }
                and event["source_tier"] == "tier_1"
                for event in hard_events
            ):
                raise AcceptanceFailure("mature exit review lacks an active Tier 1 hard event")

    return {
        "action_interpretation": {
            "action_maturity": action["action_maturity"],
            "blocking_codes": top_blocking_codes,
            "interpretations": interpretations,
            "primary_state": action["primary_state"],
            "triggered_reviews": identifier_list(action["triggered_reviews"], "reviews"),
        },
        "decision_evidence_status": decision_status,
        "facts": facts,
        "beginner_explanation_zh": projected_beginner,
        "headline": headline_text,
        "maturity_explanation": maturity_text,
        "missing_evidence": gaps,
        "official_events": events,
        "portfolio_relationship": {
            "disclosed_holdings_coverage": holdings_coverage,
            "minimum_relationship_coverage": minimum_coverage,
            "relationships": relationships,
        },
        "request": {
            "action_ids": expected_action_ids,
            "elapsed_seconds": command_elapsed.get("pending", 0),
            "mode": "rapid",
            "omitted_work": omitted_work,
            "result_checksum": request["result_checksum"],
            "terminal_status": request["terminal_status"],
        },
        "subject": {
            "fund_code": expected_code,
            "portfolio_fixture": (
                "synthetic_non_personal" if mode == "public" else "owner_private"
            ),
            "portfolio_evidence_state": subject["portfolio_evidence_state"],
            "position_present": subject["position_present"],
        },
        "sync_status": sync_status,
    }, subject


def require_useful_partial(projected):
    facts_by_field = {}
    for item in projected["facts"]:
        facts_by_field.setdefault(item["field_id"], []).append(item)
    fields = set(facts_by_field)
    required_groups = (
        {"identity_active_status", "share_class_identity"},
        {"current_manager_team"},
        {"formal_nav"},
    )
    limited_required_fields = set()
    for group in required_groups:
        matches = [item for field in group for item in facts_by_field.get(field, ())]
        if not matches:
            raise AcceptanceFailure("useful partial lacks a required sourced fact")
        for item in matches:
            if item["freshness"] in {"stale", "unknown"} or item["conflict_ids"]:
                raise AcceptanceFailure("useful partial has stale, unknown, or conflicted facts")
            if item["source_tier"] == "tier_1" and (
                item["freshness"] != "current" or item["completeness"] != "complete"
            ):
                raise AcceptanceFailure("useful partial Tier 1 fact is not current and complete")
            if item["source_tier"] == "tier_2" and (
                item["freshness"] not in {"current", "dated_history"}
                or item["completeness"] not in {"partial", "complete"}
            ):
                raise AcceptanceFailure("useful partial Tier 2 fact is not explicitly labeled")
            if item["source_tier"] == "tier_2" and (
                item["freshness"] != "current" or item["completeness"] != "complete"
            ):
                limited_required_fields.add(item["field_id"])
    gaps = {item["field_id"] for item in projected["missing_evidence"]}
    if limited_required_fields:
        if (
            projected["sync_status"]["state"] == "complete"
            or projected["decision_evidence_status"]["state"] == "complete"
        ):
            raise AcceptanceFailure("labeled partial facts are inconsistent with complete status")
        action = projected["action_interpretation"]
        if action["primary_state"] != "abstain" or any(
            item["state"] != "abstain" for item in action["interpretations"]
        ):
            raise AcceptanceFailure("action-critical labeled partial facts did not abstain")
        if limited_required_fields.intersection(
            {"identity_active_status", "share_class_identity"}
        ) and gaps.isdisjoint({"identity_active_status", "share_class_identity"}):
            raise AcceptanceFailure("labeled partial identity lacks an explicit action gap")
    beginner = projected["beginner_explanation_zh"]
    identity_text = beginner["fund_identity"]["text"]
    why_text = beginner["why_this_state"]["text"]
    relationship_text = beginner["portfolio_relationship"]["text"]
    if "Tier " not in identity_text:
        raise AcceptanceFailure("useful partial identity omits its source tier")
    if (
        re.search(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", identity_text) is None
        and gaps.isdisjoint({"identity_active_status", "share_class_identity"})
    ):
        raise AcceptanceFailure("undated identity lacks an explicit identity gap")
    if any(value not in why_text for value in ("当前经理", "正式净值", "费用")):
        raise AcceptanceFailure("useful partial omits key beginner fact explanations")
    if any(value not in relationship_text for value in ("覆盖", "未知", "不是完整 D2")):
        raise AcceptanceFailure("useful partial relationship explanation is not explicit")

    def fact_marker(item):
        data_date = item["data_as_of"] or item["published_at"] or "日期未知"
        tier = item["source_tier"].replace("tier_", "Tier ")
        return data_date, tier

    fund_code = projected["subject"]["fund_code"]
    identity_candidates = facts_by_field.get("identity_active_status") or []
    identity = next(
        (
            item
            for item in identity_candidates
            if isinstance(item["value"], dict)
            and item["value"].get("fund_code") == fund_code
        ),
        None,
    )
    share_candidates = facts_by_field.get("share_class_identity") or []
    share_class = next(
        (
            item
            for item in share_candidates
            if isinstance(item["value"], dict)
            and item["value"].get("related_fund_code") == fund_code
        ),
        None,
    )
    identity_tokens = []
    if share_class is not None:
        identity_tokens.extend(
            value
            for value in (
                share_class["value"].get("fund_name"),
                share_class["value"].get("share_class"),
                *fact_marker(share_class),
            )
            if value
        )
    if identity is not None:
        identity_tokens.extend(fact_marker(identity))
        for key in ("fund_name", "status"):
            value = identity["value"].get(key)
            if value:
                identity_tokens.append(value)
    if any(str(value) not in identity_text for value in identity_tokens):
        raise AcceptanceFailure("beginner identity text conflicts with structured facts")
    identity_evidence_ids = set(beginner["fund_identity"]["evidence_ids"])
    facts_by_id = {item["fact_id"]: item for item in projected["facts"]}
    for evidence_id in identity_evidence_ids:
        evidence = facts_by_id.get(evidence_id)
        if evidence is None:
            raise AcceptanceFailure("beginner identity evidence id is unresolved")
        value = evidence["value"]
        if evidence["field_id"] == "share_class_identity" and (
            not isinstance(value, dict) or value.get("related_fund_code") != fund_code
        ):
            raise AcceptanceFailure("beginner identity evidence includes a non-target share")
        if evidence["field_id"] == "identity_active_status" and (
            not isinstance(value, dict) or value.get("fund_code") != fund_code
        ):
            raise AcceptanceFailure("beginner identity evidence includes a non-target identity")
    for sibling in share_candidates:
        if sibling is share_class or not isinstance(sibling["value"], dict):
            continue
        sibling_name = sibling["value"].get("fund_name")
        if sibling_name and sibling_name in identity_text:
            raise AcceptanceFailure("beginner identity selected a non-target sibling share")

    managers = facts_by_field.get("current_manager_team") or []
    nav = (facts_by_field.get("formal_nav") or [None])[0]
    fee = (facts_by_field.get("fees_share_class_relationship") or [None])[0]
    holdings = (facts_by_field.get("holdings_industries") or [None])[0]
    why_tokens = []
    for manager in managers:
        why_tokens.extend((manager["value"]["manager_name"], *fact_marker(manager)))
    if nav is not None:
        why_tokens.extend((nav["value"], *fact_marker(nav)))
    if fee is not None:
        why_tokens.extend(fact_marker(fee))
    if holdings is not None:
        why_tokens.extend((holdings["value"]["report_period"], *fact_marker(holdings)))
    if any(str(value) not in why_text for value in why_tokens):
        raise AcceptanceFailure("beginner key-fact text conflicts with structured facts")

    risk_reducing_ids = {"reduce_to_cash", "full_exit", "switch_reduce"}
    redemption_facts = facts_by_field.get("redemption_terms", ())
    redemption_current = bool(redemption_facts) and all(
        item["freshness"] == "current"
        and item["completeness"] == "complete"
        and not item["conflict_ids"]
        for item in redemption_facts
    )
    for interpretation in projected["action_interpretation"]["interpretations"]:
        if interpretation["action_id"] not in risk_reducing_ids:
            continue
        if redemption_current:
            continue
        if "redemption_terms" not in gaps or interpretation["state"] != "abstain":
            raise AcceptanceFailure(
                "risk-reducing action lacks current redemption terms or explicit abstention"
            )
    for fact_field, accepted_gaps in (
        ("fees_share_class_relationship", {"fees_share_class_relationship"}),
        (
            "holdings_industries",
            {"holdings_industries", f"holdings_industries_{fund_code}"},
        ),
    ):
        if fact_field not in fields and accepted_gaps.isdisjoint(gaps):
            raise AcceptanceFailure("useful partial silently omitted a required fact or gap")
    announcement_obtained = "official_events" in projected["sync_status"]["obtained_fields"]
    if not (
        "fund_manager_product_announcement" in fields
        or projected["official_events"]
        or announcement_obtained
        or "official_events" in gaps
    ):
        raise AcceptanceFailure("useful partial silently omitted its announcement scope")
    if projected["subject"]["position_present"] is None:
        raise AcceptanceFailure("useful partial lacks position presence")
    if not projected["portfolio_relationship"]["relationships"]:
        raise AcceptanceFailure("useful partial lacks a deterministic relationship")
    if not any(
        item["relationship_type"] == "duplicate_holding_identity"
        and item["metrics"] == {"multiple_observations": True}
        for item in projected["portfolio_relationship"]["relationships"]
    ):
        raise AcceptanceFailure("useful partial lacks its synthetic duplicate binding")
    if any(
        item["evidence_state"] == "insufficient"
        for item in projected["portfolio_relationship"]["relationships"]
    ):
        raise AcceptanceFailure("useful partial relationship evidence is insufficient")
    for key in ("minimum_relationship_coverage", "disclosed_holdings_coverage"):
        coverage = projected["portfolio_relationship"][key]
        if coverage["evidence_state"] == "insufficient" and not coverage["unknown_fields"]:
            raise AcceptanceFailure("useful partial concealed insufficient D2 coverage")
        if coverage["evidence_state"] == "insufficient" and "覆盖不足" not in relationship_text:
            raise AcceptanceFailure("useful partial softened insufficient D2 in Chinese")


def require_unsupported(projected, source_status):
    status = projected["decision_evidence_status"]
    if not projected["facts"] or not projected["missing_evidence"]:
        raise AcceptanceFailure("unsupported brief is not a useful partial")
    if not status["acceptable_alternative_ids"]:
        raise AcceptanceFailure("unsupported brief lacks an acceptable alternative")
    manual_suffix = "_manual_supplement_required"
    manual_code_fields = {
        code[: -len(manual_suffix)]
        for code in status["manual_supplementation_codes"]
        if code.endswith(manual_suffix)
    }
    if len(manual_code_fields) != len(status["manual_supplementation_codes"]):
        raise AcceptanceFailure("brief manual supplementation code is not canonical")
    beginner_manual_fields = set()
    for item in projected["beginner_explanation_zh"]["evidence_gaps"]["items"]:
        if not item["next_step"]["action"] or not item["next_step"]["status"]:
            raise AcceptanceFailure("brief gap lacks a controlled next step")
        resolution = item["source_resolution"]
        if resolution is None or resolution["resolution"] != "manual_supplement_required":
            continue
        if item["supplementation"] is None or not resolution["acceptable_alternative_ids"]:
            raise AcceptanceFailure("brief manual gap lacks its bound supplementation path")
        source_field = resolution["source_field_id"]
        beginner_manual_fields.add(
            "official_events"
            if source_field == "fund_manager_product_announcement"
            else source_field
        )
    if beginner_manual_fields != manual_code_fields:
        raise AcceptanceFailure("brief manual codes and field-bound gaps do not match")
    gap_fields = {item["field_id"] for item in projected["missing_evidence"]}
    supplemented_sources = [
        item
        for item in source_status["source_fields"]
        if item["supplementation"] is not None
        and item["supplementation"]["missing_item"] in gap_fields
    ]
    if not supplemented_sources or any(
        not item["supplementation"]["accepted_input"]
        or not item["supplementation"]["suggested_location"]
        or not item["supplementation"]["impact_if_missing"]
        for item in supplemented_sources
    ):
        raise AcceptanceFailure("source status lacks a concrete supplementation path")
    resolutions = {
        (item["primary_source_id"], item["field_id"]): item["resolution"]
        for item in source_status["request_field_resolutions"]
    }
    if any(
        resolutions.get((item["source_id"], item["field_id"]))
        not in {"manual_supplement_required", "partial"}
        for item in supplemented_sources
    ):
        raise AcceptanceFailure("source supplementation is not bound to an unresolved field")
    supplemented_items = {
        item["supplementation"]["missing_item"] for item in supplemented_sources
    }
    if len(supplemented_items) != len(supplemented_sources):
        raise AcceptanceFailure("source supplementation paths are ambiguous")
    if not supplemented_items.issubset(gap_fields):
        raise AcceptanceFailure("brief gaps are not bound to source supplementation")


parent_fd = None
staging_fd = None
staging_name = None
published = False
publication_residual = False


def write_json_at(directory_fd, filename, value):
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    descriptor = os.open(
        filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600, dir_fd=directory_fd,
    )
    try:
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_named_staging(directory_name, directory_fd):
    if directory_fd is not None:
        try:
            for name in os.listdir(directory_fd):
                os.unlink(name, dir_fd=directory_fd)
            os.close(directory_fd)
        except OSError:
            pass
    if parent_fd is not None and directory_name is not None:
        try:
            os.rmdir(directory_name, dir_fd=parent_fd)
        except OSError:
            pass


def remove_staging():
    global staging_fd, staging_name
    remove_named_staging(staging_name, staging_fd)
    staging_fd = None
    staging_name = None


def exclusive_rename(source_name, target_name):
    renameatx_np = libc.renameatx_np
    renameatx_np.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        parent_fd, os.fsencode(source_name), parent_fd, os.fsencode(target_name), RENAME_EXCL
    )
    if result != 0:
        if ctypes.get_errno() in {errno.EEXIST, errno.ENOTEMPTY}:
            raise AcceptanceFailure("OUTPUT_DIR appeared before atomic publication")
        raise AcceptanceFailure("exclusive atomic output publication failed")


def same_file_identity(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def drain_pending_alarm():
    while signal.SIGALRM in signal.sigpending():
        signal.sigwait({signal.SIGALRM})


def mark_publication_residual():
    global publication_residual
    publication_residual = True
    (runtime / "publication-residual").write_text("1\n", encoding="ascii")


def commit_staging():
    global published, staging_fd, staging_name
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    staging_identity = os.fstat(staging_fd)
    renamed = False
    try:
        exclusive_rename(staging_name, output_basename)
        renamed = True
        staging_name = output_basename
        hook = os.environ.get("KUNJIN_PHASE1_TEST_RENAME_HOOK")
        if hook == "replace_after_rename":
            replacement = Path(
                os.environ["KUNJIN_PHASE1_TEST_REPLACEMENT_SOURCE"]
            ).resolve()
            if replacement.parent != Path(output_parent_path).resolve():
                raise AcceptanceFailure("test replacement parent is invalid")
            displaced = ".kunjin-phase1-displaced-" + secrets.token_hex(16)
            exclusive_rename(output_basename, displaced)
            exclusive_rename(replacement.name, output_basename)
            staging_name = displaced
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
            quarantine_name = ".kunjin-phase1-quarantine-" + secrets.token_hex(16)
            try:
                exclusive_rename(output_basename, quarantine_name)
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
                    exclusive_rename(quarantine_name, output_basename)
                except AcceptanceFailure:
                    raise AcceptanceFailure(
                        "unverified concurrent output remains quarantined"
                    ) from None
                raise AcceptanceFailure("unverified concurrent output was restored") from None
            try:
                quarantine_identity = os.fstat(quarantine_fd)
            finally:
                os.close(quarantine_fd)
            if not same_file_identity(staging_identity, quarantine_identity):
                mark_publication_residual()
                try:
                    exclusive_rename(quarantine_name, output_basename)
                except AcceptanceFailure:
                    raise AcceptanceFailure("concurrent output remains quarantined") from None
                raise AcceptanceFailure("concurrent output identity was restored") from None
            remove_named_staging(quarantine_name, staging_fd)
            staging_fd = None
            staging_name = None
            try:
                os.stat(output_basename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                mark_publication_residual()
                raise AcceptanceFailure("expired output remains published") from None
            os.fsync(parent_fd)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def publish(files):
    global parent_fd, staging_fd, staging_name
    require_time("output staging")
    parent_fd = os.open(output_parent_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    staging_name = ".kunjin-phase1-" + secrets.token_hex(16)
    os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
    staging_fd = os.open(
        staging_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
    )
    for filename, value in files.items():
        write_json_at(staging_fd, filename, value)
    if set(os.listdir(staging_fd)) != set(files):
        raise AcceptanceFailure("staging directory contents are invalid")
    os.fsync(staging_fd)
    commit_staging()


try:
    if mode == "public":
        cases = (
            (
                "useful-partial-continue_holding", useful_partial_code, "continue_holding",
                "useful-partial-continue_holding",
            ),
            (
                "useful-partial-reduce_to_cash", useful_partial_code, "reduce_to_cash",
                "useful-partial-reduce_to_cash",
            ),
            (
                "useful-partial-full_exit", useful_partial_code, "full_exit",
                "useful-partial-full_exit",
            ),
            (
                "useful-partial-switch_funds", useful_partial_code, "switch_funds",
                "useful-partial-switch_funds",
            ),
            ("unsupported-continue_holding", unsupported_code, "continue_holding", "unsupported"),
        )
        files = {}
        projections = {}
        for name, code, action, case_name in cases:
            path = run_command(
                name,
                [
                    "--json", "fund", "brief", code,
                    "--action", action, "--mode", "rapid",
                ],
                case_name,
            )
            projected, _subject = project_brief(path, code, action)
            projected["request"]["elapsed_seconds"] = command_elapsed[name]
            projections[name] = projected
            files[name + ".json"] = projected
        for name in (
            "useful-partial-continue_holding", "useful-partial-reduce_to_cash",
            "useful-partial-full_exit", "useful-partial-switch_funds",
        ):
            require_useful_partial(projections[name])
        source_path = run_command(
            "unsupported-source-status",
            ["--json", "source", "status", "--fund-code", unsupported_code],
            "unsupported",
        )
        source_status = project_source_status(source_path, unsupported_code)
        files["unsupported-source-status.json"] = source_status
        require_unsupported(projections["unsupported-continue_holding"], source_status)
        files["summary.json"] = {
            "acceptance": "phase1_public_live",
            "acceptance_scope": "technical_safety_not_financial_sufficiency",
            "fund_fact_scope": "live_public_sources",
            "global_deadline_seconds": timeout_seconds,
            "useful_partial": {
                "code": useful_partial_code,
                "decision_evidence_state": projections["useful-partial-continue_holding"][
                    "decision_evidence_status"
                ]["state"],
                "relationship_types": sorted({
                    item["relationship_type"]
                    for item in projections["useful-partial-continue_holding"][
                        "portfolio_relationship"
                    ]["relationships"]
                }),
                "useful_fact_fields": [
                    item["field_id"]
                    for item in projections["useful-partial-continue_holding"]["facts"]
                ],
                "sync_state": projections["useful-partial-continue_holding"]["sync_status"][
                    "state"
                ],
                "terminal_status": projections["useful-partial-continue_holding"]["request"][
                    "terminal_status"
                ],
            },
            "mode": "rapid",
            "portfolio_fixture": "synthetic_non_personal",
            "status": "passed",
            "unsupported": {
                "code": unsupported_code,
                "gap_fields": [
                    item["field_id"]
                    for item in projections["unsupported-continue_holding"]["missing_evidence"]
                ],
                "supplementation_codes": projections["unsupported-continue_holding"][
                    "decision_evidence_status"
                ]["manual_supplementation_codes"],
            },
        }
        publish(files)
    else:
        try:
            owner_code = owner_code_file.read_text(encoding="ascii")
        finally:
            owner_code_file.unlink(missing_ok=True)
        if FUND_CODE.fullmatch(owner_code) is None:
            raise AcceptanceFailure("owner code mapping is invalid")
        path = run_command(
            "owner",
            [
                "--json", "fund", "brief", owner_code,
                "--action", "continue_holding", "--mode", "rapid",
            ],
            "owner",
        )
        projected, subject = project_brief(path, owner_code, "continue_holding")
        relationship_coverage = projected["portfolio_relationship"][
            "minimum_relationship_coverage"
        ]
        candidate_codes = sorted(set(
            projected["action_interpretation"]["blocking_codes"]
            + projected["decision_evidence_status"]["missing_fields"]
            + projected["decision_evidence_status"]["unsupported_fields"]
        ))
        stable_codes = [
            code for code in candidate_codes if re.search(r"[0-9]{6}", code) is None
        ]
        owner_code = None
        publish({
            "summary.json": {
                "acceptance": "phase1_owner_private",
                "acceptance_scope": "technical_safety_not_financial_sufficiency",
                "action_maturity": projected["action_interpretation"]["action_maturity"],
                "decision_evidence_state": projected["decision_evidence_status"]["state"],
                "elapsed_seconds": command_elapsed["owner"],
                "opaque_subject_id": secrets.token_hex(16),
                "position_present": subject["position_present"],
                "primary_state": projected["action_interpretation"]["primary_state"],
                "relationship_coverage_class": conservative_coverage_class(
                    relationship_coverage,
                    projected["portfolio_relationship"]["disclosed_holdings_coverage"],
                ),
                "stable_codes": stable_codes,
                "status": "passed",
                "terminal_status": projected["request"]["terminal_status"],
            }
        })
except (AcceptanceFailure, AcceptanceDeadline, AcceptanceInterrupted):
    if not published:
        remove_staging()
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

if [[ "${DRIVER_EXIT_CODE}" -ne 0 ]]; then
    if [[ -e "${RUNTIME_DIR}/publication-residual" ]] \
        || [[ "${KUNJIN_PHASE1_TEST_RENAME_HOOK:-}" == "replace_after_rename" ]]; then
        printf 'Phase 1 acceptance failed closed; concurrent publication residue may remain in OUTPUT_DIR parent.\n' >&2
    fi
    printf 'Phase 1 acceptance failed closed.\n' >&2
    exit "${DRIVER_EXIT_CODE}"
fi
printf 'Phase 1 acceptance passed.\n'
