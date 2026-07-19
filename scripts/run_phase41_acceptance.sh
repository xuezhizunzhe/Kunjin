#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

usage() {
    printf 'usage: %s {local|fault|engineering|owner}\n' "$0" >&2
    printf 'owner requires KUNJIN_PHASE41_OWNER_APPROVED=explicit_private_keychain_read_only.\n' >&2
    printf 'engineering requires KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE.\n' >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 64
fi
readonly MODE="$1"
case "${MODE}" in
    local|fault|engineering|owner) ;;
    *) usage; exit 64 ;;
esac

if [[ "$0" != */* || -L "$0" ]]; then
    printf 'acceptance script must be invoked by an explicit non-symlink path\n' >&2
    exit 66
fi
readonly SCRIPT_DIR="$(cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)"
readonly REPOSITORY_ROOT="$(cd -P -- "${SCRIPT_DIR}/.." && /bin/pwd -P)"
readonly INSTALLED_SKILL_ROOT="${HOME}/.codex/skills/kunjin-fund"
readonly PYTHON="${REPOSITORY_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
    printf 'repository virtual environment is unavailable\n' >&2
    exit 69
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase41-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
CHILD_PIDS=()

wait_for_children() {
    local pid
    for pid in "${CHILD_PIDS[@]:-}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
    CHILD_PIDS=()
}

cleanup() {
    wait_for_children
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

readonly LOCAL_CASES="research_scope readiness ready_pair not_held not_comparable privacy_scan no_network_dependency no_persistence"
readonly FAULT_CASES="keychain_unavailable candidate_failure_isolation unsupported_source source_cooldown manual_supplement_required no_automatic_retry no_network_dependency no_persistence keyboard_interrupt system_exit maximum_finite_orchestration"

prepare_private_runtime() {
    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
    export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
}

check_no_process_residue() {
    if /usr/bin/pgrep -f "${RUNTIME_DIR}" >/dev/null 2>&1; then
        printf 'no_process_residue: failed\n' >&2
        return 1
    fi
    printf 'no_process_residue: passed\n'
}

scan_captured_output() {
    local output_path="$1"
    "${PYTHON}" - "${output_path}" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="strict")
forbidden = (
    r"(?<![0-9])[0-9]{6}(?![0-9])",
    r"\b(recommended|best|winner|buy|sell|rank|score)\b",
    r"\b(monthly_net_income|emergency_reserve_months|profile_id|keyed_fingerprint)\b",
    r"\b(action_authorized|automatic_trade|exact_amount_available)\s*[=:]\s*true\b",
    r"\btarget[_ ]weight\b",
)
if any(re.search(pattern, text, re.IGNORECASE) for pattern in forbidden):
    raise SystemExit("acceptance output privacy or authorization scan failed")
PY
}

check_runtime_permissions() {
    "${PYTHON}" - "${RUNTIME_DIR}" <<'PY'
from pathlib import Path
import os
import stat
import sys

root = Path(sys.argv[1])
for path in (root, *root.rglob("*")):
    metadata = os.lstat(path)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SystemExit("private runtime permission is broader than owner-only")
PY
}

run_pytest() {
    local output_path="${RUNTIME_DIR}/pytest.out"
    shift
    prepare_private_runtime
    if (
        cd "${REPOSITORY_ROOT}"
        "${PYTHON}" -m pytest -q --basetemp "${RUNTIME_DIR}/pytest" "$@"
    ) >"${output_path}" 2>&1; then
        /bin/cat "${output_path}"
    else
        /bin/cat "${output_path}" >&2
        return 1
    fi
    scan_captured_output "${output_path}"
    check_runtime_permissions
}

run_local() {
    printf 'mode=local cases=%s\n' "${LOCAL_CASES}"
    run_pytest local \
        tests/unit/test_decision_health.py \
        tests/unit/test_selection_scope.py \
        tests/unit/test_selection_readiness.py \
        tests/unit/test_selection_service.py \
        tests/unit/test_selection_research.py \
        tests/integration/test_cli.py \
        tests/test_smoke.py
    cleanup
    check_no_process_residue
}

run_fault() {
    printf 'mode=fault cases=%s\n' "${FAULT_CASES}"
    run_pytest fault \
        tests/unit/test_decision_health.py \
        tests/unit/test_selection_scope.py \
        tests/unit/test_selection_readiness.py \
        tests/unit/test_selection_service.py \
        tests/unit/test_selection_research.py \
        tests/integration/test_cli.py \
        -k 'unavailable or failure_isolation or cooldown or manual_supplement or retry or network_dependency or persistence or process_control or interrupt or system_exit or not_held or not_comparable or ready_projection'
    "${PYTHON}" - <<'PY'
MAX_PUBLIC_SOURCE_STATUS_CALLS = 5
MAX_PUBLIC_ACTION_CALLS = 25
MAX_ENGINEERING_SOURCE_STATUS_CALLS = 4
MAX_ENGINEERING_ACTION_CALLS = 20
action_types = (
    "sync_fund",
    "sync_fund_profile",
    "sync_fund_holdings",
    "sync_fund_documents",
    "fund_classify",
)
public_roles = tuple(f"subject_{index}" for index in range(MAX_PUBLIC_SOURCE_STATUS_CALLS))
calls = {(role, action) for role in public_roles for action in action_types}
assert len(calls) == MAX_PUBLIC_ACTION_CALLS
assert MAX_ENGINEERING_SOURCE_STATUS_CALLS * len(action_types) == MAX_ENGINEERING_ACTION_CALLS
assert 1 + MAX_PUBLIC_SOURCE_STATUS_CALLS + len(calls) + 1 == 32
print("maximum_finite_orchestration: passed")
PY
    cleanup
    check_no_process_residue
}

run_engineering() {
    readonly OWNER_SOURCE_DATA_DIR="${KUNJIN_DATA_DIR:-${HOME}/.local/share/kunjin}"
    readonly OWNER_SOURCE_DB="${OWNER_SOURCE_DATA_DIR}/kunjin.db"
    readonly SUBJECT_FILE="${KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE:-}"
    if [[ -z "${SUBJECT_FILE}" ]]; then
        printf 'engineering mode requires a private subject file\n' >&2
        exit 77
    fi
    prepare_private_runtime
    readonly ENGINEERING_OUTPUT="${RUNTIME_DIR}/engineering.out"
    if ! "${PYTHON}" - "${SUBJECT_FILE}" "${REPOSITORY_ROOT}" \
        "${INSTALLED_SKILL_ROOT}" "${OWNER_SOURCE_DB}" "${KUNJIN_DATA_DIR}/kunjin.db" \
        "${RUNTIME_DIR}/subjects.json" >"${ENGINEERING_OUTPUT}" 2>&1 <<'PY'
from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import os
import re
import sqlite3
import stat
import sys

subject_file, repository_root, skill_root, source_db, copy_db, runtime_copy = sys.argv[1:]
roles = (
    "engineering_subject_1",
    "engineering_subject_2",
    "engineering_subject_3",
    "engineering_subject_4",
)
path = Path(subject_file)
try:
    metadata = os.lstat(path)
    resolved = path.resolve(strict=True)
    parent_metadata = os.lstat(resolved.parent)
except (OSError, RuntimeError):
    raise SystemExit("private subject file metadata is invalid") from None
if not path.is_absolute():
    raise SystemExit("private subject file must be absolute")
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("private subject file must be a regular non-symlink")
if stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit("private subject file must have exact mode")
if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
    raise SystemExit("private subject parent must have exact mode")
if metadata.st_uid != os.getuid() or parent_metadata.st_uid != os.getuid():
    raise SystemExit("private subject file and parent must be owner-owned")
repo = Path(repository_root).resolve(strict=True)
skill = Path(skill_root).resolve(strict=False)
if resolved == repo or repo in resolved.parents or resolved == skill or skill in resolved.parents:
    raise SystemExit("private subject file must be outside repository and Skill")
try:
    raw = resolved.read_bytes()
    payload = json.loads(raw)
except (OSError, UnicodeError, ValueError, TypeError):
    raise SystemExit("private subject file is invalid") from None
if type(payload) is not dict or set(payload) != set(roles):
    raise SystemExit("private subject file must contain exact role keys")
codes = tuple(payload[role] for role in roles)
if any(type(code) is not str or re.fullmatch(r"[0-9]{6}", code) is None for code in codes):
    raise SystemExit("private subject values must be ASCII fund codes")
if len(set(codes)) != 4 or any(code == "0" * 6 for code in codes):
    raise SystemExit("private subject file requires four unique non-reserved values")
Path(runtime_copy).write_bytes(raw)
os.chmod(runtime_copy, 0o600)
del raw, payload

source_path = Path(source_db)
if not source_path.is_file() or source_path.is_symlink():
    raise SystemExit("engineering database is unavailable")
source_uri = source_path.resolve().as_uri() + "?mode=ro"
with sqlite3.connect(source_uri, uri=True) as source:
    with sqlite3.connect(copy_db) as target:
        source.backup(target)
os.chmod(copy_db, 0o600)
os.environ["KUNJIN_DATA_DIR"] = str(Path(copy_db).parent)

from kunjin.cli import run

MAX_PUBLIC_SOURCE_STATUS_CALLS = 5
MAX_PUBLIC_ACTION_CALLS = 25
MAX_ENGINEERING_SOURCE_STATUS_CALLS = 4
MAX_ENGINEERING_ACTION_CALLS = 20
READINESS_CONTRACT = "fund shortlist-readiness"
SOURCE_STATUS_CONTRACT = "source status --fund-code"
action_patterns = (
    ("sync_fund", re.compile(r"sync fund ([0-9]{6})"), lambda code: ["--json", "sync", "fund", code]),
    ("sync_fund_profile", re.compile(r"sync fund-profile ([0-9]{6}) --mode rapid"), lambda code: ["--json", "sync", "fund-profile", code, "--mode", "rapid"]),
    ("sync_fund_holdings", re.compile(r"sync fund-holdings ([0-9]{6}) --mode rapid"), lambda code: ["--json", "sync", "fund-holdings", code, "--mode", "rapid"]),
    ("sync_fund_documents", re.compile(r"sync fund-documents ([0-9]{6})"), lambda code: ["--json", "sync", "fund-documents", code]),
    ("fund_classify", re.compile(r"fund classify ([0-9]{6})"), lambda code: ["--json", "fund", "classify", code]),
)

def invoke(argv, expected_command, allowed_exits=(0, 1)):
    result, exit_code, json_output = run(argv)
    if not json_output or exit_code not in allowed_exits or result.get("command") != expected_command:
        raise RuntimeError("bounded command returned an invalid terminal result")
    if type(result.get("data")) is not dict:
        raise RuntimeError("bounded command data is unavailable")
    return result, exit_code

initial_readiness_calls = 0
final_readiness_calls = 0
initial, _ = invoke(["--json", "fund", "shortlist-readiness", *codes], "fund.shortlist-readiness")
initial_readiness_calls += 1
source_status_calls = 0
terminal_source_roles = set()
blocked_fields_by_role = {}
for role, code in zip(roles, codes):
    source, _ = invoke(["--json", "source", "status", "--fund-code", code], "source.status", (0,))
    source_status_calls += 1
    fields = source["data"].get("source_fields", [])
    resolutions = source["data"].get("request_field_resolutions", [])
    blocked_fields = {
        item.get("field_id")
        for item in fields
        if type(item) is dict
        and item.get("state") in {"cooldown", "unavailable", "unsupported"}
        and type(item.get("field_id")) is str
    }
    blocked_fields.update(
        item.get("field_id")
        for item in resolutions
        if type(item) is dict
        and item.get("resolution") == "manual_supplement_required"
        and type(item.get("field_id")) is str
    )
    blocked_fields_by_role[role] = blocked_fields
    if blocked_fields:
        terminal_source_roles.add(role)

returned = initial["data"].get("bounded_refresh_actions")
if type(returned) is not list:
    raise RuntimeError("initial readiness actions are invalid")
code_to_role = dict(zip(codes, roles))
selected = []
seen = set()
for item in returned:
    if type(item) is not dict or set(item) != {"fund_code", "command"}:
        raise RuntimeError("undeclared refresh action")
    code, command = item["fund_code"], item["command"]
    if code not in code_to_role or type(command) is not str:
        raise RuntimeError("undeclared refresh subject")
    if "--force" in command:
        raise RuntimeError("--force is prohibited")
    match_value = None
    for order, (action_type, pattern, argv_builder) in enumerate(action_patterns):
        match = pattern.fullmatch(command)
        if match is not None and match.group(1) == code:
            match_value = (order, action_type, argv_builder)
            break
    if match_value is None:
        raise RuntimeError("undeclared refresh command")
    order, action_type, argv_builder = match_value
    key = (code_to_role[code], action_type)
    if key in seen:
        raise RuntimeError("automatic retry or duplicate action is prohibited")
    seen.add(key)
    selected.append((roles.index(code_to_role[code]), order, key, code, argv_builder))
selected.sort(key=lambda item: (item[0], item[1]))

action_states = Counter()
executed_actions = 0
blocked_by_action = {
    "sync_fund": {"formal_nav", "adjusted_return_series"},
    "sync_fund_profile": {
        "identity_active_status",
        "current_manager_team",
        "fees_share_class_relationship",
    },
    "sync_fund_holdings": {"holdings_industries"},
    "sync_fund_documents": set(),
    "fund_classify": set(),
}
for _role_order, _action_order, key, code, argv_builder in selected:
    role, action_type = key
    if blocked_by_action[action_type] & blocked_fields_by_role[role]:
        action_states["stopped_by_source_state"] += 1
        continue
    expected = {
        "sync_fund": "sync.fund",
        "sync_fund_profile": "sync.fund-profile",
        "sync_fund_holdings": "sync.fund-holdings",
        "sync_fund_documents": "sync.fund-documents",
        "fund_classify": "fund.classify",
    }[action_type]
    _payload, exit_code = invoke(argv_builder(code), expected)
    executed_actions += 1
    action_states["completed" if exit_code == 0 else "terminal_failure"] += 1

final, _ = invoke(["--json", "fund", "shortlist-readiness", *codes], "fund.shortlist-readiness")
final_readiness_calls += 1
if initial_readiness_calls != 1 or final_readiness_calls != 1:
    raise RuntimeError("readiness invocation count violated")
if source_status_calls != len(codes) or source_status_calls > MAX_ENGINEERING_SOURCE_STATUS_CALLS:
    raise RuntimeError("source status invocation count violated")
if executed_actions > MAX_ENGINEERING_ACTION_CALLS or executed_actions > MAX_PUBLIC_ACTION_CALLS:
    raise RuntimeError("refresh action invocation count violated")
if len(codes) > MAX_PUBLIC_SOURCE_STATUS_CALLS:
    raise RuntimeError("public source status invocation count violated")

final_ready = final["data"].get("comparison_evidence_ready") is True
if terminal_source_roles:
    outcome = "stopped_by_source_state"
elif final_ready:
    outcome = "completed_once"
elif selected:
    outcome = "partial_once"
else:
    outcome = "not_run"
gaps = final["data"].get("blocking_codes", [])
safe_gaps = sorted({value for value in gaps if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]*", value)})
summary = {
    "action_state_counts": dict(sorted(action_states.items())),
    "candidate_count": len(roles),
    "comparison_evidence_ready": final_ready,
    "financial_interpretation": "prohibited",
    "gap_categories": safe_gaps,
    "mode": "engineering",
    "orchestration_outcome": outcome,
    "refresh_action_calls": executed_actions,
    "roles": list(roles),
    "source_status_calls": source_status_calls,
    "subject_role": "engineering_subject",
}
encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
if any(code in encoded for code in codes) or str(resolved) in encoded:
    raise RuntimeError("private engineering value leaked")
print(encoded)
PY
    then
        /bin/cat "${ENGINEERING_OUTPUT}" >&2
        exit 1
    fi
    /bin/cat "${ENGINEERING_OUTPUT}"
    scan_captured_output "${ENGINEERING_OUTPUT}"
    printf 'subject_role=engineering_subject financial_interpretation=prohibited\n'
    printf 'owner_candidate_state=owner_candidates_unavailable\n'
    printf 'financial_usability=not_yet_testable\n'
    printf 'candidate_formation.status=research_scope_only\n'
    printf 'candidate_formation.candidate_code_discovery=not_implemented\n'
    check_runtime_permissions
    cleanup
    check_no_process_residue
}

run_owner() {
    if [[ "${KUNJIN_PHASE41_OWNER_APPROVED:-}" != "explicit_private_keychain_read_only" ]]; then
        printf 'owner mode is disabled without explicit private Keychain read-only approval\n' >&2
        exit 77
    fi
    readonly OWNER_SOURCE_DATA_DIR="${KUNJIN_DATA_DIR:-${HOME}/.local/share/kunjin}"
    readonly OWNER_SOURCE_DB="${OWNER_SOURCE_DATA_DIR}/kunjin.db"
    if [[ ! -f "${OWNER_SOURCE_DB}" || -L "${OWNER_SOURCE_DB}" ]]; then
        printf 'owner acceptance requires a regular local KunJin database\n' >&2
        exit 66
    fi
    prepare_private_runtime
    readonly OWNER_OUTPUT="${RUNTIME_DIR}/owner.out"
    if ! "${PYTHON}" - "${OWNER_SOURCE_DB}" "${KUNJIN_DATA_DIR}/kunjin.db" \
        "${RUNTIME_DIR}" "${REPOSITORY_ROOT}" >"${OWNER_OUTPUT}" 2>&1 <<'PY'
from pathlib import Path
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys

source_db, target_db, runtime_dir, repository_root = sys.argv[1:]

def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()

source_hash_before = digest(source_db)
source_uri = Path(source_db).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(source_uri, uri=True) as source:
    data_version_before = source.execute("PRAGMA data_version").fetchone()[0]
    with sqlite3.connect(target_db) as target:
        source.backup(target)
    os.chmod(target_db, 0o600)

    os.environ["KUNJIN_DATA_DIR"] = str(Path(target_db).parent)
    os.environ["KUNJIN_STATE_DIR"] = str(Path(runtime_dir) / "state")
    from kunjin.security.keychain import KeychainTokenStore
    from kunjin.suitability.crypto import ProfileKeyStore

    tracked_children = []
    original_popen = subprocess.Popen
    ACCOUNT = "v1"
    if ProfileKeyStore.SERVICE != "com.kunjin.profile-encryption" or ProfileKeyStore.ACCOUNT != ACCOUNT:
        raise RuntimeError("owner Keychain identity contract changed")
    expected_security = [
        "/usr/bin/security",
        "find-generic-password",
        "-s",
        "com.kunjin.profile-encryption",
        "-a",
        ACCOUNT,
        "-w",
    ]

    def read_only_security(_self, command):
        if command != expected_security:
            raise RuntimeError("Keychain write operation is prohibited")
        process = original_popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        tracked_children.append(process)
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if process.returncode != 0:
            raise subprocess.CalledProcessError(
                process.returncode, command, output=stdout, stderr=stderr
            )
        return result

    def prohibit_keychain_write(*_args, **_kwargs):
        # add-generic-password and delete-generic-password are both prohibited.
        raise RuntimeError("Keychain write operation is prohibited")

    def prohibit_network(*_args, **_kwargs):
        raise RuntimeError("owner network operation is prohibited")

    KeychainTokenStore._run = read_only_security
    KeychainTokenStore.save = prohibit_keychain_write
    KeychainTokenStore.delete = prohibit_keychain_write
    subprocess.Popen = prohibit_keychain_write
    socket.create_connection = prohibit_network
    socket.socket.connect = prohibit_network
    try:
        if ProfileKeyStore().load_existing_key() is None:
            raise RuntimeError("owner_keychain_unavailable")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SystemExit("owner_keychain_unavailable") from None

    from kunjin.cli import run

    def invoke(argv, expected):
        payload, exit_code, json_output = run(["--json", *argv])
        if not json_output or exit_code != 0 or payload.get("command") != expected:
            try:
                if ProfileKeyStore().load_existing_key() is None:
                    raise RuntimeError("owner_keychain_unavailable")
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise SystemExit("owner_keychain_unavailable") from None
            raise RuntimeError("owner safe status projection failed")
        data = payload.get("data")
        if type(data) is not dict:
            raise RuntimeError("owner safe status projection is invalid")
        return data

    phase_a = invoke(["profile", "status"], "profile.status")
    phase_b = invoke(["suitability", "status"], "suitability.status")
    phase_c = invoke(["allocation", "status"], "allocation.status")
    scope = invoke(["fund", "research-scope"], "fund.research-scope")

    for process in tracked_children:
        if process.returncode is None:
            process.wait()
        if process.returncode is None:
            raise RuntimeError("owner child was not waited")

    data_version_after = source.execute("PRAGMA data_version").fetchone()[0]
source_hash_after = digest(source_db)
if source_hash_before != source_hash_after or data_version_before != data_version_after:
    raise RuntimeError("real database changed during owner acceptance")

def safe_code(value):
    return value if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]*", value) else None

def safe_codes(values):
    if type(values) not in (list, tuple):
        return []
    return sorted({value for value in values if safe_code(value) is not None})

candidate = scope.get("candidate_formation")
boundary = scope.get("action_boundary")
if candidate != {"status": "research_scope_only", "candidate_code_discovery": "not_implemented"}:
    raise RuntimeError("owner candidate residual state is invalid")
if boundary != {
    "action_maturity": "evidence_only",
    "action_authorized": False,
    "exact_amount_available": False,
    "automatic_trade": False,
}:
    raise RuntimeError("owner action boundary is invalid")
summary = {
    "action_boundary": boundary,
    "candidate_formation": candidate,
    "financial_usability": "not_yet_testable",
    "mode": "owner",
    "owner_candidate_state": "owner_candidates_unavailable",
    "phase_a": {
        "state": safe_code(phase_a.get("state")),
        "freshness": safe_code(phase_a.get("freshness")),
    },
    "phase_b": {
        "state": safe_code(phase_b.get("state")),
        "freshness": safe_code(phase_b.get("freshness")),
        "status": safe_code(phase_b.get("status")),
        "blocking_codes": safe_codes(phase_b.get("hard_blocks", [])),
        "constraint_codes": safe_codes(phase_b.get("constraints", [])),
    },
    "phase_c": {
        "state": safe_code(phase_c.get("state")),
        "freshness": safe_code(phase_c.get("freshness")),
        "status": safe_code(phase_c.get("status")),
        "constraint_codes": safe_codes(phase_c.get("binding_constraints", [])),
    },
    "real_database_opened_read_only": True,
}
encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
for private_path in (source_db, target_db, runtime_dir, repository_root, str(Path.home())):
    if private_path and private_path in encoded:
        raise RuntimeError("owner private path leaked")
if re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", encoded):
    raise RuntimeError("owner fund code leaked")
for path in (Path(runtime_dir), *Path(runtime_dir).rglob("*")):
    metadata = os.lstat(path)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise RuntimeError("owner runtime permission is broader than owner-only")
print(encoded)
PY
    then
        /bin/cat "${OWNER_OUTPUT}" >&2
        exit 1
    fi
    /bin/cat "${OWNER_OUTPUT}"
    scan_captured_output "${OWNER_OUTPUT}"
    printf 'owner_candidate_state=owner_candidates_unavailable\n'
    printf 'financial_usability=not_yet_testable\n'
    printf 'candidate_formation.status=research_scope_only\n'
    printf 'candidate_formation.candidate_code_discovery=not_implemented\n'
    check_runtime_permissions
    cleanup
    check_no_process_residue
}

case "${MODE}" in
    local) run_local ;;
    fault) run_fault ;;
    engineering) run_engineering ;;
    owner) run_owner ;;
esac
