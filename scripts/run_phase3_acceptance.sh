#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

usage() {
    printf 'usage: %s {local|fault|owner}\n' "$0" >&2
    printf 'owner requires KUNJIN_PHASE3_OWNER_APPROVED=explicit_private_read_only.\n' >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 64
fi
readonly MODE="$1"
case "${MODE}" in
    local|fault|owner) ;;
    *) usage; exit 64 ;;
esac

if [[ "$0" != */* || -L "$0" ]]; then
    printf 'acceptance script must be invoked by an explicit non-symlink path\n' >&2
    exit 66
fi
readonly SCRIPT_DIR="$(cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)"
readonly REPOSITORY_ROOT="$(cd -P -- "${SCRIPT_DIR}/.." && /bin/pwd -P)"
readonly PYTHON="${REPOSITORY_ROOT}/.venv/bin/python"
readonly CLI="${REPOSITORY_ROOT}/.venv/bin/kunjin"
if [[ ! -x "${PYTHON}" || ! -x "${CLI}" ]]; then
    printf 'repository virtual environment is unavailable\n' >&2
    exit 69
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase3-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
cleanup() {
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

# Stable case inventory for the bounded diagnosis contract.
readonly LOCAL_CASES="complete_relationships partial_holdings missing_nav benchmark_text_limitation candidate_duplication candidate_insufficient_data privacy_scan"
readonly FAULT_CASES="invalid_candidate unknown_is_not_zero non_authorizing exact_amount_unavailable no_process_residue"

check_no_process_residue() {
    if /usr/bin/pgrep -f "${RUNTIME_DIR}" >/dev/null 2>&1; then
        printf 'no_process_residue: failed; a child still references the private runtime\n' >&2
        return 1
    fi
    printf 'no_process_residue: passed\n'
}

run_pytest_mode() {
    local mode="$1"
    shift
    if [[ "${mode}" == "local" ]]; then
        printf 'mode=local cases=%s\n' "${LOCAL_CASES}"
    else
        printf 'mode=fault cases=%s\n' "${FAULT_CASES}"
    fi
    (
        cd "${REPOSITORY_ROOT}"
        "${PYTHON}" -m pytest -q --basetemp "${RUNTIME_DIR}/pytest" "$@"
    )
    check_no_process_residue
}

run_owner() {
    if [[ "${KUNJIN_PHASE3_OWNER_APPROVED:-}" != "explicit_private_read_only" ]]; then
        printf 'owner mode is disabled without explicit private read-only approval\n' >&2
        exit 77
    fi

    readonly OWNER_SOURCE_DATA_DIR="${KUNJIN_DATA_DIR:-${HOME}/.local/share/kunjin}"
    readonly OWNER_SOURCE_DB="${OWNER_SOURCE_DATA_DIR}/kunjin.db"
    if [[ ! -f "${OWNER_SOURCE_DB}" || -L "${OWNER_SOURCE_DB}" ]]; then
        printf 'owner acceptance requires a regular local KunJin database\n' >&2
        exit 66
    fi

    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/owner-data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/owner-state"
    export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/owner-pycache"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"

    # The real database is opened read-only. Diagnosis runs only against its private copy.
    "${PYTHON}" - "${CLI}" "${OWNER_SOURCE_DB}" "${KUNJIN_DATA_DIR}/kunjin.db" <<'PY'
import json
import re
import sqlite3
import subprocess
import sys


cli, source_db, target_db = sys.argv[1:]
with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as source:
    with sqlite3.connect(target_db) as target:
        source.backup(target)

completed = subprocess.run(
    [cli, "--json", "portfolio", "diagnose"],
    stdin=subprocess.DEVNULL,
    capture_output=True,
    timeout=90,
)
if completed.returncode not in {0, 1}:
    raise SystemExit(f"owner diagnosis failed with unexpected exit={completed.returncode}")
try:
    payload = json.loads(completed.stdout)
except (TypeError, ValueError) as exc:
    raise SystemExit("owner diagnosis did not return JSON") from exc
if payload.get("command") != "portfolio.diagnose":
    raise SystemExit("owner diagnosis returned an unexpected command")
errors = payload.get("errors")
if not isinstance(errors, list) or any(
    not isinstance(item, dict) or item.get("code") != "insufficient_data"
    for item in errors
):
    raise SystemExit("owner diagnosis returned an unexpected error")

data = payload.get("data")
if not isinstance(data, dict):
    raise SystemExit("owner diagnosis data is unavailable")
boundary = data.get("action_boundary")
if boundary != {
    "action_authorized": False,
    "action_maturity": "evidence_only",
    "exact_amount_available": False,
}:
    raise SystemExit("owner diagnosis violated the action boundary")
coverage = data.get("coverage")
concentration = data.get("concentration")
relationships = data.get("relationships")
findings = data.get("findings")
if not all(
    isinstance(item, expected)
    for item, expected in (
        (coverage, dict),
        (concentration, dict),
        (relationships, list),
        (findings, list),
    )
):
    raise SystemExit("owner diagnosis shape is invalid")

fund_codes = set()
for section in coverage.values():
    if not isinstance(section, dict):
        raise SystemExit("owner coverage shape is invalid")
    for key in ("included_fund_codes", "omitted_fund_codes"):
        values = section.get(key, [])
        if not isinstance(values, list):
            raise SystemExit("owner coverage code shape is invalid")
        fund_codes.update(
            value
            for value in values
            if isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value)
        )
for relationship in relationships:
    if isinstance(relationship, dict):
        fund_codes.update(
            value
            for value in relationship.get("fund_codes", [])
            if isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value)
        )

def anonymize_codes(values):
    if not isinstance(values, list):
        raise SystemExit("owner stable code list is invalid")
    return sorted({re.sub(r"[0-9]{6}", "<fund>", value) for value in values if isinstance(value, str)})

summary = {
    "mode": "owner",
    "acceptance_scope": "privacy_degradation_and_non_authorization",
    "action_authorized": boundary["action_authorized"],
    "action_maturity": boundary["action_maturity"],
    "conflict_categories": anonymize_codes(data.get("conflicts", [])),
    "diagnosis_ran_on_private_copy": True,
    "exact_amount_available": boundary["exact_amount_available"],
    "finding_count": len(findings),
    "holdings_coverage_state": coverage["holdings"]["evidence_state"],
    "missing_evidence_categories": anonymize_codes(data.get("missing_evidence", [])),
    "never_places_trades": True,
    "position_count": concentration.get("position_count"),
    "privacy_scan_passed": True,
    "real_database_opened_read_only": True,
    "relationship_count": len(relationships),
    "relationship_coverage_state": coverage["relationship"]["evidence_state"],
    "structured_insufficient_data": any(
        item.get("code") == "insufficient_data" for item in errors
    ),
}
encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
if any(code in encoded for code in fund_codes):
    raise SystemExit("owner fund code leaked into anonymous output")
private_keys = {
    "account_title", "amount", "cost", "current_value", "income", "nav",
    "portfolio_weight", "profit", "profile", "shares", "total_value",
}
if any(key in summary for key in private_keys):
    raise SystemExit("owner private key leaked into anonymous output")
if re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", encoded):
    raise SystemExit("owner fund code leaked into anonymous output")
print(encoded)
PY
    check_no_process_residue
}

case "${MODE}" in
    local)
        run_pytest_mode local \
            tests/unit/test_diagnosis_models.py \
            tests/unit/test_diagnosis_service.py \
            tests/unit/test_diagnosis_research.py \
            tests/integration/test_cli.py
        ;;
    fault)
        run_pytest_mode fault \
            tests/unit/test_diagnosis_models.py \
            tests/unit/test_diagnosis_service.py \
            tests/unit/test_diagnosis_research.py
        ;;
    owner) run_owner ;;
esac
