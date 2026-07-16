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
readonly OUTPUT_DIR="${OUTPUT_PARENT}/${OUTPUT_BASENAME}"
if [[ -e "${OUTPUT_DIR}" || -L "${OUTPUT_DIR}" ]]; then
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
readonly SCRIPT_BASENAME="${SCRIPT_SOURCE##*/}"
readonly SCRIPT_DIRECTORY="$(cd -P "${SCRIPT_SOURCE%/*}" && pwd -P)"
readonly ROOT_DIR="$(cd -P "${SCRIPT_DIRECTORY}/.." && pwd -P)"
readonly CLI="${ROOT_DIR}/.venv/bin/kunjin"
readonly PYTHON="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "${CLI}" || ! -x "${PYTHON}" ]]; then
    printf 'repository virtual environment is unavailable\n' >&2
    exit 69
fi
readonly SYNC_TIMEOUT_SECONDS="${KUNJIN_PHASE0_SYNC_TIMEOUT_SECONDS:-90}"
if [[ ! "${SYNC_TIMEOUT_SECONDS}" =~ ^[1-9][0-9]*$ \
   || "${SYNC_TIMEOUT_SECONDS}" -gt 90 ]]; then
    printf 'Phase 0 sync timeout must be an integer from 1 through 90 seconds\n' >&2
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

readonly VERSION_JSON="${RUNTIME_DIR}/version.json"
readonly SOURCE_BEFORE_JSON="${RUNTIME_DIR}/source-status-before.json"
readonly SYNC_JSON="${RUNTIME_DIR}/sync-fund-profile.json"
readonly SOURCE_AFTER_JSON="${RUNTIME_DIR}/source-status-after.json"
readonly ROUTE_JSON="${RUNTIME_DIR}/decision-route.json"
readonly SUMMARY_JSON="${RUNTIME_DIR}/summary.json"
readonly SYNC_WATCHDOG_METADATA="${RUNTIME_DIR}/sync-watchdog.metadata"
readonly SYNC_WATCHDOG_STDERR="${RUNTIME_DIR}/sync-watchdog.stderr"

run_required_json() {
    local output_path="$1"
    shift
    if ! "${CLI}" "$@" > "${output_path}" 2> "${output_path}.stderr"; then
        printf 'required amount-free KunJin command failed\n' >&2
        return 1
    fi
    /bin/chmod 600 "${output_path}" "${output_path}.stderr"
}

run_required_json "${VERSION_JSON}" --json version
run_required_json "${SOURCE_BEFORE_JSON}" --json source status

if ! "${PYTHON}" - \
    "${SYNC_TIMEOUT_SECONDS}" \
    "${CLI}" \
    "${CODE}" \
    "${SYNC_JSON}" \
    "${SYNC_JSON}.stderr" \
    "${SYNC_WATCHDOG_METADATA}" \
    2> "${SYNC_WATCHDOG_STDERR}" <<'PY'
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


timeout_text, cli, code, stdout_path, stderr_path, metadata_path = sys.argv[1:]
timeout_seconds = int(timeout_text)
started = time.monotonic()
hard_deadline = started + timeout_seconds
soft_deadline = hard_deadline - min(0.5, timeout_seconds / 2)
cleanup_deadline = hard_deadline - min(0.25, timeout_seconds / 4)
timed_out = False
process = None


class WatchdogInterrupted(BaseException):
    pass


def interrupt_watchdog(_signal_number, _frame):
    raise WatchdogInterrupted()


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, interrupt_watchdog)


def terminate_process_group(child, deadline):
    for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, signal.SIG_IGN)
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=max(0, min(0.2, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait()


try:
    with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
        process = subprocess.Popen(
            [
                cli,
                "--json",
                "sync",
                "fund-profile",
                code,
                "--mode",
                "rapid",
            ],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            exit_code = process.wait(
                timeout=max(0, soft_deadline - time.monotonic())
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(process, cleanup_deadline)
            exit_code = 124
except BaseException:
    if process is not None:
        terminate_process_group(
            process,
            min(cleanup_deadline, time.monotonic() + 0.2),
        )
    raise
finally:
    if process is not None and process.poll() is None:
        terminate_process_group(
            process,
            min(cleanup_deadline, time.monotonic() + 0.2),
        )
elapsed_milliseconds = int((time.monotonic() - started) * 1000)
Path(metadata_path).write_text(
    f"{exit_code}\t{elapsed_milliseconds}\t{int(timed_out)}\n",
    encoding="ascii",
)
PY
then
    printf 'Phase 0 sync watchdog failed\n' >&2
    exit 70
fi
/bin/chmod 600 \
    "${SYNC_JSON}" \
    "${SYNC_JSON}.stderr" \
    "${SYNC_WATCHDOG_METADATA}" \
    "${SYNC_WATCHDOG_STDERR}"
IFS=$'\t' read -r SYNC_EXIT_CODE SYNC_ELAPSED_MILLISECONDS SYNC_TIMED_OUT \
    < "${SYNC_WATCHDOG_METADATA}"
readonly SYNC_EXIT_CODE SYNC_ELAPSED_MILLISECONDS SYNC_TIMED_OUT
if [[ ! "${SYNC_EXIT_CODE}" =~ ^-?[0-9]+$ \
   || ! "${SYNC_ELAPSED_MILLISECONDS}" =~ ^[0-9]+$ \
   || ! "${SYNC_TIMED_OUT}" =~ ^[01]$ ]]; then
    printf 'Phase 0 sync watchdog metadata is invalid\n' >&2
    exit 70
fi
if [[ "${SYNC_TIMED_OUT}" -eq 1 ]]; then
    printf 'Phase 0 rapid sync reached its terminal wall-clock deadline\n' >&2
    exit 124
fi

run_required_json \
    "${SOURCE_AFTER_JSON}" --json source status --fund-code "${CODE}"
run_required_json \
    "${ROUTE_JSON}" --json decision route --mode rapid \
    --action fact_research --action buy_or_add

"${PYTHON}" - \
    "${CODE}" \
    "${SYNC_EXIT_CODE}" \
    "${SYNC_ELAPSED_MILLISECONDS}" \
    "${VERSION_JSON}" \
    "${SOURCE_BEFORE_JSON}" \
    "${SYNC_JSON}" \
    "${SOURCE_AFTER_JSON}" \
    "${ROUTE_JSON}" \
    "${SUMMARY_JSON}" <<'PY'
import json
import re
import sys
from datetime import datetime
from pathlib import Path


(
    code,
    sync_exit_text,
    sync_elapsed_milliseconds_text,
    version_path,
    source_before_path,
    sync_path,
    source_after_path,
    route_path,
    summary_path,
) = sys.argv[1:]

REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
CHECKSUM = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_KEYS = {
    "access_token",
    "authorization_token",
    "body",
    "debt",
    "emergency_reserve",
    "income",
    "managed_path",
    "monthly_net_income",
    "private_key",
    "raw_body",
    "request_body",
    "token",
}
FORBIDDEN_TEXT = (
    re.compile(r"(?:^|[\s=])/(?:Users|private|tmp|var)/"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:token|private[_ -]?key)\s*[:=]"),
)
ENVELOPE_KEYS = {"schema_version", "command", "as_of", "data", "warnings", "errors"}


def fail(message):
    raise SystemExit("phase0 acceptance failed: " + message)


def read_envelope(path, expected_command):
    try:
        raw = Path(path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail(expected_command + " did not return one valid UTF-8 JSON document")
    if type(payload) is not dict or set(payload) != ENVELOPE_KEYS:
        fail(expected_command + " envelope keys are invalid")
    if payload["schema_version"] != "1" or payload["command"] != expected_command:
        fail(expected_command + " schema or command is invalid")
    if type(payload["data"]) is not dict:
        fail(expected_command + " data must be a non-empty-capable object")
    if type(payload["warnings"]) is not list or type(payload["errors"]) is not list:
        fail(expected_command + " warnings or errors are invalid")
    try:
        parsed_as_of = datetime.fromisoformat(payload["as_of"])
    except (TypeError, ValueError):
        fail(expected_command + " as_of is invalid")
    if parsed_as_of.utcoffset() is None:
        fail(expected_command + " as_of must be timezone-aware")
    return payload


def validate_public_tree(value, key=None):
    if key is not None:
        normalized_key = key.lower()
        if normalized_key in FORBIDDEN_KEYS:
            fail("private or raw field is present")
        if normalized_key == "checksum" or normalized_key.endswith("_checksum"):
            if type(value) is not str or CHECKSUM.fullmatch(value) is None:
                fail("public checksum is invalid")
    if type(value) is dict:
        for child_key, child_value in value.items():
            if type(child_key) is not str:
                fail("JSON object key is not text")
            validate_public_tree(child_value, child_key)
    elif type(value) is list:
        for child in value:
            validate_public_tree(child)
    elif type(value) is str:
        if any(pattern.search(value) for pattern in FORBIDDEN_TEXT):
            fail("path, credential, body, or private-key material is present")


version = read_envelope(version_path, "version")
source_before = read_envelope(source_before_path, "source.status")
sync = read_envelope(sync_path, "sync.fund-profile")
source_after = read_envelope(source_after_path, "source.status")
route = read_envelope(route_path, "decision.route")
for payload in (version, source_before, sync, source_after, route):
    validate_public_tree(payload)

if version["errors"] or type(version["data"].get("version")) is not str:
    fail("version is unavailable")
if source_before["errors"] or source_before["data"].get("fund_code") is not None:
    fail("pre-run source status must be amount-free and unscoped")
if source_after["errors"] or source_after["data"].get("fund_code") != code:
    fail("post-run source status is unavailable for the requested public code")
if route["errors"]:
    fail("decision route is unavailable")
if sync["data"].get("fund_code") != code:
    fail("sync result does not match the requested public code")

request_ids = {
    "source_before": source_before["data"].get("request_id"),
    "sync": sync["data"].get("request", {}).get("request_id"),
    "source_after": source_after["data"].get("request_id"),
    "route": route["data"].get("request_id"),
}
if any(type(value) is not str or REQUEST_ID.fullmatch(value) is None for value in request_ids.values()):
    fail("one or more request identifiers are invalid")
if len(set(request_ids.values())) != len(request_ids):
    fail("request identifiers must be distinct")

required_checksums = (
    source_before["data"].get("policy_checksum"),
    source_before["data"].get("registry_checksum"),
    source_after["data"].get("policy_checksum"),
    source_after["data"].get("registry_checksum"),
    route["data"].get("policy_checksum"),
    route["data"].get("registry_checksum"),
    route["data"].get("result_checksum"),
)
if any(type(value) is not str or CHECKSUM.fullmatch(value) is None for value in required_checksums):
    fail("required public checksums are missing or invalid")
policy_identities = {
    (payload["data"].get("policy_version"), payload["data"].get("policy_checksum"))
    for payload in (source_before, source_after, route)
}
registry_identities = {
    (payload["data"].get("registry_version"), payload["data"].get("registry_checksum"))
    for payload in (source_before, source_after, route)
}
if len(policy_identities) != 1 or len(registry_identities) != 1:
    fail("policy or registry identity changed during acceptance")
if any(type(value) is not str or not value for identity in (*policy_identities, *registry_identities) for value in identity):
    fail("policy or registry version identity is invalid")
if source_before["data"].get("mode") != "rapid" or source_after["data"].get("mode") != "rapid":
    fail("source status must use rapid mode")
if sync["data"].get("request", {}).get("mode") != "rapid":
    fail("fund profile sync did not use rapid mode")
if route["data"].get("mode") != "rapid":
    fail("decision route did not use rapid mode")

try:
    sync_exit_code = int(sync_exit_text)
    sync_elapsed_milliseconds = int(sync_elapsed_milliseconds_text)
except ValueError:
    fail("sync exit or elapsed metadata is invalid")
if sync_exit_code != 0 or sync["errors"]:
    fail("sync did not return a successful public partial-or-complete envelope")
sync_elapsed_seconds = sync_elapsed_milliseconds / 1000
if sync_elapsed_milliseconds < 0 or sync_elapsed_milliseconds > 90000:
    fail("rapid fund profile sync exceeded 90 seconds")

post_fields = source_after["data"].get("source_fields")
if type(post_fields) is not list or not post_fields:
    fail("post-run source fields are empty")
source_attempt_count = sum(
    1
    for item in post_fields
    if type(item) is dict
    and (item.get("last_success_at") is not None or item.get("last_failure_at") is not None)
)
if source_attempt_count < 1:
    fail("no bounded source attempt was recorded")

sections = sync["data"].get("sections")
if type(sections) is not dict or not sections:
    fail("sync returned an empty fact envelope")
fact_record_count = sum(
    item.get("records", 0)
    for item in sections.values()
    if type(item) is dict
    and item.get("status") in {"success", "not_disclosed"}
    and type(item.get("records")) is int
    and item.get("records", 0) > 0
)
if fact_record_count < 1:
    fail("sync returned no obtained public facts")

terminal_status = sync["data"].get("request", {}).get("terminal_status")
if terminal_status not in {"complete", "partial"}:
    fail("sync did not reach a usable terminal status")
resolutions = source_after["data"].get("request_field_resolutions")
if type(resolutions) is not list or not resolutions:
    fail("post-run field resolutions are empty")
missing_resolutions = [
    item for item in resolutions
    if type(item) is dict and item.get("resolution") != "usable"
]
partial = terminal_status == "partial" or bool(missing_resolutions)
if partial:
    if not missing_resolutions:
        fail("partial sync does not identify the missing field impact")
    source_by_identity = {
        (item.get("source_id"), item.get("field_id")): item
        for item in post_fields
        if type(item) is dict
    }
    for resolution in missing_resolutions:
        identity = (resolution.get("primary_source_id"), resolution.get("field_id"))
        supplementation = source_by_identity.get(identity, {}).get("supplementation")
        if type(supplementation) is not dict:
            fail("partial result lacks a concrete supplementation record")
        if not supplementation.get("impact_if_missing"):
            fail("partial result lacks missing-field action impact")
        if not supplementation.get("suggested_location") or not supplementation.get("accepted_input"):
            fail("partial result lacks a concrete supplementation path")

actions = route["data"].get("actions")
if type(actions) is not list:
    fail("decision route actions are invalid")
actions_by_name = {
    item.get("action"): item for item in actions if type(item) is dict
}
facts_action = actions_by_name.get("fact_research")
buy_action = actions_by_name.get("buy_or_add")
if type(facts_action) is not dict or facts_action.get("research_available") is not True:
    fail("independent fact research is unavailable")
if type(buy_action) is not dict:
    fail("buy_or_add route is missing")
if (
    buy_action.get("exact_amount_available") is not False
    or buy_action.get("minimum_state") == "actionable"
    or buy_action.get("action_maturity") == "mature"
    or not buy_action.get("blocking_codes")
):
    fail("Phase 0 must block a mature or exact purchase direction")

summary = {
    "schema_version": "1",
    "acceptance": "phase0_amount_free_live",
    "status": "passed",
    "fund_code": code,
    "mode": "rapid",
    "fresh_isolated_runtime": True,
    "sync_exit_code": sync_exit_code,
    "sync_elapsed_seconds": sync_elapsed_seconds,
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
validate_public_tree(summary)
Path(summary_path).write_text(
    json.dumps(summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
    encoding="utf-8",
)
PY

/bin/chmod 600 \
    "${VERSION_JSON}" \
    "${SOURCE_BEFORE_JSON}" \
    "${SYNC_JSON}" \
    "${SOURCE_AFTER_JSON}" \
    "${ROUTE_JSON}" \
    "${SUMMARY_JSON}"
/bin/mkdir "${OUTPUT_DIR}"
/bin/chmod 700 "${OUTPUT_DIR}"
/bin/cp \
    "${VERSION_JSON}" \
    "${SOURCE_BEFORE_JSON}" \
    "${SYNC_JSON}" \
    "${SOURCE_AFTER_JSON}" \
    "${ROUTE_JSON}" \
    "${SUMMARY_JSON}" \
    "${OUTPUT_DIR}/"
/bin/chmod 600 "${OUTPUT_DIR}"/*.json
printf 'Phase 0 acceptance passed.\n'
