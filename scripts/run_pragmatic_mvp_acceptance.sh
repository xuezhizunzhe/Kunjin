#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

usage() {
    printf 'usage: %s {local|fault|live|owner}\n' "$0" >&2
    printf 'live requires KUNJIN_PRAGMATIC_LIVE_APPROVED=explicit_public_read_only and KUNJIN_PRAGMATIC_PUBLIC_FUND_CODE.\n' >&2
    printf 'owner requires both explicit private and public read-only approvals.\n' >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 64
fi
readonly MODE="$1"
case "${MODE}" in
    local|fault|live|owner) ;;
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

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-pragmatic-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
cleanup() {
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

# Stable acceptance labels. They are also the human-readable coverage inventory.
readonly LOCAL_CASES="official_policy media_reprint partial_market named_fund_public decision_routing held_fund_review thesis_review portfolio_diagnosis candidate_gate_abstention"
readonly FAULT_CASES="malformed_payload unsafe_redirect source_timeout source_cooldown source_cap_reached manual_supplement_required no_process_residue"
readonly LIVE_SOURCES="gov_cn_policy stcn_fund_news eastmoney_market"
readonly OWNER_CASE="anonymous_owner"
# CLI shapes: news recent --window recent --mode rapid;
# market overview --window recent --mode rapid; fund intelligence CODE --window recent --mode rapid.

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
    printf 'mode=%s cases=%s\n' "${mode}" "$([[ "${mode}" == local ]] && printf '%s' "${LOCAL_CASES}" || printf '%s' "${FAULT_CASES}")"
    (
        cd "${REPOSITORY_ROOT}"
        "${PYTHON}" -m pytest -q --basetemp "${RUNTIME_DIR}/pytest" "$@"
    )
    check_no_process_residue
}

run_live() {
    if [[ "${KUNJIN_PRAGMATIC_LIVE_APPROVED:-}" != "explicit_public_read_only" ]]; then
        printf 'live mode is disabled without explicit public read-only approval\n' >&2
        exit 77
    fi
    readonly CODE="${KUNJIN_PRAGMATIC_PUBLIC_FUND_CODE:-}"
    if [[ ! "${CODE}" =~ ^[0-9]{6}$ || "${CODE}" == "000000" ]]; then
        printf 'live mode requires a non-reserved six-digit public fund code\n' >&2
        exit 65
    fi

    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
    export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"

    # Public response bodies and SQLite state remain inside RUNTIME_DIR and are removed on exit.
    "${PYTHON}" - "${CLI}" "${CODE}" "${LIVE_SOURCES}" <<'PY'
import json
import subprocess
import sys
import time

cli, code, live_sources = sys.argv[1:]
all_source_ids = set(live_sources.split())
usable_outcomes = {"success", "cache_hit"}
commands = (
    ("news_recent", [cli, "--json", "news", "recent", "--window", "recent", "--mode", "rapid"]),
    ("market_overview", [cli, "--json", "market", "overview", "--window", "recent", "--mode", "rapid"]),
    ("fund_intelligence", [cli, "--json", "fund", "intelligence", code, "--window", "recent", "--mode", "rapid"]),
)
summaries = []

def fail_live(label, reason_code):
    print(json.dumps({
        "mode": "live",
        "status": "failed",
        "failed_workflow": label,
        "failure_reason_code": reason_code,
        "source_allowlist": live_sources.split(),
        "stores_response_bodies_in_git": False,
        "never_places_trades": True,
        "results": summaries,
    }, ensure_ascii=True, sort_keys=True))
    raise SystemExit(1)

for label, command in commands:
    started = time.monotonic()
    completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=95)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        raise SystemExit(f"{label} failed with exit={completed.returncode}")
    payload = json.loads(completed.stdout)
    data = payload["data"]
    request = data["request"]
    sources = [
        {
            "source_id": source["source_id"],
            "source_tier": source["source_tier"],
            "outcome": source["outcome"],
            "reason_code": source.get("reason_code"),
            "retryable": source.get("retryable"),
            "cooldown_until": source.get("cooldown_until"),
        }
        for source in request["sources"]
    ]
    summaries.append(
        {
            "workflow": label,
            "elapsed_ms": elapsed_ms,
            "terminal_status": request["terminal_status"],
            "published_item_count": len(data["items"]),
            "dimension_count": len(data["dimensions"]),
            "fund_context_field_count": len(
                data["fund_relevance"].get("covered_fields", [])
            ),
            "fund_relevance_link_count": len(data["fund_relevance"]["links"]),
            "sources": sources,
            "omitted_work": request["omitted_work"],
            "action_maturity": data["action_maturity"],
            "action_authorized": data["action_authorized"],
            "exact_amount_available": data["exact_amount_available"],
        }
    )
    expected_source_ids = (
        {"gov_cn_policy", "stcn_fund_news"}
        if label == "news_recent"
        else all_source_ids
    )
    returned_source_ids = {source["source_id"] for source in request["sources"]}
    if returned_source_ids != expected_source_ids:
        fail_live(label, "live_source_set_mismatch")
    usable_source_ids = {
        source["source_id"]
        for source in request["sources"]
        if source["outcome"] in usable_outcomes
    }
    if (
        data["action_maturity"] != "evidence_only"
        or data["action_authorized"] is not False
        or data["exact_amount_available"] is not False
    ):
        fail_live(label, "live_action_boundary_violation")
    if label == "news_recent" and (
        not usable_source_ids.intersection({"gov_cn_policy", "stcn_fund_news"})
        or not data["items"]
    ):
        fail_live(label, "live_news_requires_published_items")
    if label == "market_overview" and (
        "eastmoney_market" not in usable_source_ids or not data["dimensions"]
    ):
        fail_live(label, "live_market_requires_eastmoney_evidence")
    if label == "fund_intelligence":
        if (
            request["subject_scope"] != "named_public_fund"
            or request["subject_fund_code"] != code
        ):
            fail_live(label, "live_fund_subject_mismatch")
        if not usable_source_ids or not (data["items"] or data["dimensions"]):
            fail_live(label, "live_fund_requires_usable_evidence")
        if (
            data["fund_relevance"]["context"] is None
            or not data["fund_relevance"].get("covered_fields")
            or not data["fund_relevance"]["links"]
        ):
            fail_live(label, "live_fund_requires_named_context")
print(json.dumps({
    "mode": "live",
    "source_allowlist": live_sources.split(),
    "stores_response_bodies_in_git": False,
    "never places trades": True,
    "results": summaries,
}, ensure_ascii=True, sort_keys=True))
PY
    check_no_process_residue
}

run_owner() {
    if [[ "${KUNJIN_PRAGMATIC_OWNER_APPROVED:-}" != "explicit_private_read_only" ]]; then
        printf 'owner mode is disabled without explicit private read-only approval\n' >&2
        exit 77
    fi
    if [[ "${KUNJIN_PRAGMATIC_LIVE_APPROVED:-}" != "explicit_public_read_only" ]]; then
        printf 'owner mode is disabled without explicit public network read-only approval\n' >&2
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

    # SQLite backup reads the owner database without mutating it. All commands write only to the
    # private throwaway copy. The emitted audit has no code, amount, NAV, fee, or profile value.
    readonly OWNER_FINANCIAL_GATE_MARKER="${RUNTIME_DIR}/owner-financial-gate-failed"
    "${PYTHON}" - "${CLI}" "${OWNER_CASE}" "${OWNER_SOURCE_DB}" \
        "${KUNJIN_DATA_DIR}/kunjin.db" "${OWNER_FINANCIAL_GATE_MARKER}" <<'PY'
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

cli, owner_case, source_db, target_db, financial_gate_marker = sys.argv[1:]
with sqlite3.connect(f"file:{source_db}?mode=ro", uri=True) as source:
    with sqlite3.connect(target_db) as target:
        source.backup(target)

def invoke(label, arguments):
    completed = subprocess.run([cli, "--json", *arguments], stdin=subprocess.DEVNULL, capture_output=True, timeout=95)
    if completed.returncode != 0:
        try:
            failed_payload = json.loads(completed.stdout)
            error_codes = sorted(
                error["code"]
                for error in failed_payload.get("errors", [])
                if isinstance(error, dict) and isinstance(error.get("code"), str)
            )
        except (KeyError, TypeError, ValueError):
            error_codes = []
        rendered_codes = ",".join(error_codes) if error_codes else "unavailable"
        raise SystemExit(
            f"owner {label} read failed with exit={completed.returncode} "
            f"error_codes={rendered_codes}"
        )
    payload = json.loads(completed.stdout)
    return payload["data"], payload

status, _ = invoke("status", ["status"])
portfolio, _ = invoke("portfolio_show", ["portfolio", "show"])
positions = portfolio.get("positions", []) if isinstance(portfolio, dict) else []
codes = [item.get("fund_code") for item in positions if isinstance(item, dict)]
codes = [code for code in codes if isinstance(code, str) and re.fullmatch(r"[0-9]{6}", code)]
if not codes:
    raise SystemExit("owner acceptance requires at least one locally stored held fund")
fund_code = sorted(set(codes))[0]
analysis, _ = invoke("portfolio_analyze", ["portfolio", "analyze"])
overlap, _ = invoke("portfolio_overlap", ["portfolio", "overlap"])
brief, _ = invoke(
    "fund_brief",
    ["fund", "brief", fund_code, "--action", "continue_holding", "--mode", "rapid"],
)
result, raw_intelligence = invoke(
    "fund_intelligence",
    ["fund", "intelligence", fund_code, "--window", "recent", "--mode", "rapid"],
)
thesis, _ = invoke("thesis_review", ["thesis", "review", fund_code])
raw_intelligence_text = json.dumps(raw_intelligence, ensure_ascii=True, sort_keys=True)
for forbidden_key in (
    '"amount"', '"cost"', '"current_value"', '"debt"', '"income"',
    '"portfolio_weight"', '"profile"', '"profit"', '"shares"',
):
    if forbidden_key in raw_intelligence_text:
        raise SystemExit("public intelligence leaked a forbidden private field")
request = result["request"]
brief_omitted = set(brief["request"]["omitted_work"])
brief_core_stages = {
    "identity_profile",
    "personal_position_observation",
    "formal_nav",
    "manager_fee_profile",
    "holdings_industries",
    "official_announcements",
}
core_brief_evidence_complete = brief_core_stages.isdisjoint(brief_omitted)
summary = {
    "mode": "owner",
    "case": owner_case,
    "acceptance_scope": "privacy_and_degradation_contract",
    "core_brief_evidence_complete": core_brief_evidence_complete,
    "financial_action_usability_assessed": False,
    "held_fund_selected_internally": True,
    "held_fund_code_exposed": False,
    "private_amount_exposed": False,
    "real_database_mutated": False,
    "status_checked": isinstance(status, dict),
    "portfolio_analysis_checked": isinstance(analysis, dict),
    "portfolio_overlap_checked": isinstance(overlap, dict),
    "held_fund_brief_checked": isinstance(brief, dict),
    "held_fund_brief_terminal_status": brief["request"]["terminal_status"],
    "held_fund_brief_omitted_work": brief["request"]["omitted_work"],
    "thesis_review_checked": isinstance(thesis, dict),
    "public_intelligence_private_field_scan_passed": True,
    "terminal_status": request["terminal_status"],
    "source_outcomes": [source["outcome"] for source in request["sources"]],
    "source_tiers": [source["source_tier"] for source in request["sources"]],
    "omitted_work": request["omitted_work"],
    "action_maturity": result["action_maturity"],
    "action_authorized": result["action_authorized"],
    "exact_amount_available": result["exact_amount_available"],
    "never places trades": True,
}
encoded = json.dumps(summary, ensure_ascii=True, sort_keys=True)
if fund_code in encoded:
    raise SystemExit("owner code leaked into the audit summary")
print(encoded)
if not core_brief_evidence_complete:
    Path(financial_gate_marker).write_text(
        "owner_brief_core_sources_incomplete\n",
        encoding="ascii",
    )
PY
    check_no_process_residue
    if [[ -f "${OWNER_FINANCIAL_GATE_MARKER}" ]]; then
        printf 'owner financial evidence gate failed: owner_brief_core_sources_incomplete\n' >&2
        return 1
    fi
}

case "${MODE}" in
    local)
        run_pytest_mode local \
            tests/unit/test_intelligence_parsers.py \
            tests/unit/test_intelligence_research.py \
            tests/unit/test_intelligence_service.py \
            tests/unit/test_brief_service.py \
            tests/unit/test_portfolio.py \
            tests/unit/test_thesis.py \
            tests/integration/test_cli.py
        ;;
    fault)
        run_pytest_mode fault \
            tests/unit/test_intelligence_worker.py \
            tests/unit/test_intelligence_service.py -k 'timeout or malformed or redirect or partial or cooldown or cap or all_sources_failed or sanitiz'
        ;;
    live) run_live ;;
    owner) run_owner ;;
esac
