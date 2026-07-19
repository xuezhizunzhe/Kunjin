#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

usage() {
    printf 'usage: %s {local|fault|owner}\n' "$0" >&2
    printf 'owner requires KUNJIN_PHASE4_OWNER_APPROVED=explicit_private_read_only.\n' >&2
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

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase4-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
cleanup() {
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

# Stable case inventory for the bounded shortlist contract.
readonly LOCAL_CASES="two_candidate_tradeoffs five_candidate_boundary conditional_shortlist not_comparable held_candidate_amount_boundary cash_like_not_protected_cash privacy_scan no_network_dependency"
readonly FAULT_CASES="partial_candidate_isolation insufficient_data non_authorizing exact_amount_unavailable automatic_trade_disabled no_process_residue"

check_no_process_residue() {
    if /usr/bin/pgrep -f "${RUNTIME_DIR}" >/dev/null 2>&1; then
        printf 'no_process_residue: failed; a child still references the private runtime\n' >&2
        return 1
    fi
    printf 'no_process_residue: passed\n'
}

prepare_private_runtime() {
    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
    export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
}

run_pytest_mode() {
    local mode="$1"
    shift
    prepare_private_runtime
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
    if [[ "${KUNJIN_PHASE4_OWNER_APPROVED:-}" != "explicit_private_read_only" ]]; then
        printf 'owner mode is disabled without explicit private read-only approval\n' >&2
        exit 77
    fi

    readonly OWNER_SOURCE_DATA_DIR="${KUNJIN_DATA_DIR:-${HOME}/.local/share/kunjin}"
    readonly OWNER_SOURCE_DB="${OWNER_SOURCE_DATA_DIR}/kunjin.db"
    if [[ ! -f "${OWNER_SOURCE_DB}" || -L "${OWNER_SOURCE_DB}" ]]; then
        printf 'owner acceptance requires a regular local KunJin database\n' >&2
        exit 66
    fi

    prepare_private_runtime
    readonly OWNER_COPY_DB="${KUNJIN_DATA_DIR}/kunjin.db"

    # The source is opened with SQLite URI mode=ro. All CLI work uses the private copy.
    "${PYTHON}" - "${CLI}" "${OWNER_SOURCE_DB}" "${OWNER_COPY_DB}" "${RUNTIME_DIR}" "${REPOSITORY_ROOT}" <<'PY'
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


cli, source_db, target_db, runtime_dir, repository_root = sys.argv[1:]
source_uri = Path(source_db).resolve().as_uri() + "?mode=ro"
with sqlite3.connect(source_uri, uri=True) as source:
    with sqlite3.connect(target_db) as target:
        source.backup(target)
os.chmod(target_db, 0o600)

evidence_tables = (
    "positions",
    "funds",
    "fund_nav",
    "fund_identities",
    "fund_manager_tenures",
    "fund_fee_rules",
    "fund_sizes",
    "fund_benchmarks",
    "fund_holdings",
    "fund_risk_classifications",
)
all_fund_codes = set()
evidence_counts = {}
with sqlite3.connect(target_db) as private:
    table_names = {
        row[0]
        for row in private.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        if isinstance(row[0], str)
    }
    for table in sorted(table_names):
        quoted_table = table.replace('"', '""')
        columns = [
            row[1]
            for row in private.execute(f'PRAGMA table_info("{quoted_table}")')
            if isinstance(row[1], str) and "fund_code" in row[1]
        ]
        for column in columns:
            quoted_column = column.replace('"', '""')
            query = f'SELECT DISTINCT "{quoted_column}" FROM "{quoted_table}"'
            for (value,) in private.execute(query):
                if isinstance(value, str) and re.fullmatch(r"[0-9]{6}", value):
                    all_fund_codes.add(value)
    for table in evidence_tables:
        if table not in table_names:
            continue
        columns = {
            row[1] for row in private.execute(f'PRAGMA table_info("{table}")')
        }
        if "fund_code" not in columns:
            continue
        for (code,) in private.execute(f'SELECT DISTINCT fund_code FROM "{table}"'):
            if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code):
                evidence_counts[code] = evidence_counts.get(code, 0) + 1

candidates = sorted(evidence_counts, key=lambda code: (-evidence_counts[code], code))[:2]
if len(candidates) != 2:
    raise SystemExit("owner_candidates_unavailable")

completed = subprocess.run(
    [cli, "--json", "fund", "shortlist", *candidates],
    stdin=subprocess.DEVNULL,
    capture_output=True,
    timeout=90,
)
if completed.returncode not in {0, 1}:
    raise SystemExit(f"owner shortlist failed with unexpected exit={completed.returncode}")
try:
    payload = json.loads(completed.stdout)
except (TypeError, ValueError) as exc:
    raise SystemExit("owner shortlist did not return JSON") from exc
if payload.get("command") != "fund.shortlist":
    raise SystemExit("owner shortlist returned an unexpected command")
errors = payload.get("errors")
if not isinstance(errors, list) or any(
    not isinstance(item, dict) or item.get("code") != "insufficient_data"
    for item in errors
):
    raise SystemExit("owner shortlist returned an unexpected error")

data = payload.get("data")
if not isinstance(data, dict):
    raise SystemExit("owner shortlist data is unavailable")
boundary = data.get("action_boundary")
if boundary != {
    "action_authorized": False,
    "action_maturity": "evidence_only",
    "automatic_trade": False,
    "exact_amount_available": False,
}:
    raise SystemExit("owner shortlist violated the action boundary")
request = data.get("request")
gate = data.get("personal_gate")
reviews = data.get("candidate_reviews")
if not all(
    isinstance(value, expected)
    for value, expected in ((request, dict), (gate, dict), (reviews, list))
):
    raise SystemExit("owner shortlist shape is invalid")
if request.get("candidate_count") != 2 or len(reviews) != 2:
    raise SystemExit("owner shortlist candidate count is invalid")

def anonymize_categories(values):
    if not isinstance(values, list):
        raise SystemExit("owner stable category list is invalid")
    categories = set()
    for value in values:
        if isinstance(value, str):
            categories.add(re.sub(r"(?<![0-9])[0-9]{6}(?![0-9])", "<fund>", value))
    return sorted(categories)

evidence_state_counts = {}
for review in reviews:
    if not isinstance(review, dict) or not isinstance(review.get("evidence_state"), str):
        raise SystemExit("owner shortlist review shape is invalid")
    state = review["evidence_state"]
    evidence_state_counts[state] = evidence_state_counts.get(state, 0) + 1

summary = {
    "acceptance_scope": "privacy_degradation_and_non_authorization",
    "action_authorized": boundary["action_authorized"],
    "action_maturity": boundary["action_maturity"],
    "allocation_state": gate.get("allocation_state"),
    "automatic_trade": boundary["automatic_trade"],
    "candidate_count": request["candidate_count"],
    "comparison_state": data.get("comparison_state"),
    "conflict_categories": anonymize_categories(data.get("conflicts", [])),
    "shortlist_ran_on_private_copy": True,
    "evidence_state_counts": evidence_state_counts,
    "exact_amount_available": boundary["exact_amount_available"],
    "missing_evidence_categories": anonymize_categories(data.get("missing_evidence", [])),
    "mode": "owner",
    "never_places_trades": True,
    "privacy_scan_passed": True,
    "real_database_opened_read_only": True,
    "structured_insufficient_data": any(item.get("code") == "insufficient_data" for item in errors),
    "suitability_state": gate.get("suitability_state"),
}
encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
if any(code in encoded for code in all_fund_codes):
    raise SystemExit("owner fund code leaked into anonymous output")
private_keys = {
    "account_title", "amount", "asset", "cost", "current_value", "debt",
    "income", "monthly_income", "nav", "portfolio_weight", "profit", "profile",
    "reserve", "shares", "total_value",
}
def all_keys(value):
    if isinstance(value, dict):
        return {
            *(str(key).casefold() for key in value),
            *(key for item in value.values() for key in all_keys(item)),
        }
    if isinstance(value, list):
        return {key for item in value for key in all_keys(item)}
    return set()

if not all_keys(summary).isdisjoint(private_keys):
    raise SystemExit("owner private key leaked into anonymous output")
for private_path in (source_db, target_db, runtime_dir, repository_root, str(Path.home())):
    if private_path and private_path in encoded:
        raise SystemExit("owner private path leaked into anonymous output")
if re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", encoded):
    raise SystemExit("owner fund code leaked into anonymous output")
print(encoded)
PY

    cleanup
    check_no_process_residue
}

case "${MODE}" in
    local)
        run_pytest_mode local \
            tests/unit/test_selection_models.py \
            tests/unit/test_selection_policy.py \
            tests/unit/test_selection_service.py \
            tests/unit/test_selection_research.py \
            tests/integration/test_cli.py
        ;;
    fault)
        run_pytest_mode fault \
            tests/unit/test_selection_models.py \
            tests/unit/test_selection_policy.py \
            tests/unit/test_selection_service.py \
            tests/unit/test_selection_research.py
        ;;
    owner) run_owner ;;
esac
