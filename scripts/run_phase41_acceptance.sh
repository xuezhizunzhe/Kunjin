#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
umask 077

usage() {
    printf 'usage: %s {local|fault|engineering|owner}\n' "$0" >&2
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
    printf '{"error_code":"phase41_script_path_invalid","ok":false}\n' >&2
    exit 66
fi
readonly SCRIPT_DIR="$(cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)"
readonly REPOSITORY_ROOT="$(cd -P -- "${SCRIPT_DIR}/.." && /bin/pwd -P)"
readonly PYTHON="${REPOSITORY_ROOT}/.venv/bin/python"
readonly HELPER="${SCRIPT_DIR}/phase41_acceptance.py"
if [[ ! -x "${PYTHON}" || ! -f "${HELPER}" || -L "${HELPER}" ]]; then
    printf '{"error_code":"phase41_runtime_unavailable","ok":false}\n' >&2
    exit 69
fi

if [[ "${MODE}" == "owner" ]]; then
    if [[ "${KUNJIN_PHASE41_OWNER_APPROVED:-}" != "explicit_private_keychain_read_only" ]]; then
        printf '{"error_code":"owner_approval_required","ok":false}\n' >&2
        exit 77
    fi
    if [[ -n "${KUNJIN_DATA_DIR:-}" || -n "${KUNJIN_STATE_DIR:-}" ]]; then
        printf '{"error_code":"owner_runtime_override_prohibited","ok":false}\n' >&2
        exit 77
    fi
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase41-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
export KUNJIN_PHASE41_RUNTIME_DIR="${RUNTIME_DIR}"
export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
/bin/mkdir -p "${PYTHONPYCACHEPREFIX}"
/bin/chmod 700 "${PYTHONPYCACHEPREFIX}"
CHILD_PIDS=()
set -m

terminate_children() {
    local pid
    for pid in "${CHILD_PIDS[@]:-}"; do
        if [[ -n "${pid}" ]] && /bin/kill -0 "${pid}" 2>/dev/null; then
            /bin/kill -TERM "-${pid}" 2>/dev/null || true
            if /bin/kill -0 "${pid}" 2>/dev/null; then
                /bin/kill -KILL "-${pid}" 2>/dev/null || true
            fi
        fi
    done
    for pid in "${CHILD_PIDS[@]:-}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
    CHILD_PIDS=()
}

cleanup() {
    terminate_children
    /bin/rm -rf -- "${RUNTIME_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

run_tracked() {
    local stdout_path="$1"
    local stderr_path="$2"
    shift 2
    "$@" >"${stdout_path}" 2>"${stderr_path}" &
    local pid=$!
    CHILD_PIDS+=("${pid}")
    local result=0
    wait "${pid}" || result=$?
    if /bin/kill -0 "${pid}" 2>/dev/null; then
        printf '{"error_code":"phase41_child_residue","ok":false}\n' >&2
        return 70
    fi
    if [[ -x /usr/bin/pgrep ]] && /usr/bin/pgrep -g "${pid}" >/dev/null 2>&1; then
        /bin/kill -KILL "-${pid}" 2>/dev/null || true
        printf '{"error_code":"phase41_descendant_residue","ok":false}\n' >&2
        return 70
    fi
    CHILD_PIDS=()
    return "${result}"
}

emit_scanned() {
    local capture="$1"
    local output_kind="$2"
    local emitted="${RUNTIME_DIR}/emit.out"
    local emit_error="${RUNTIME_DIR}/emit.err"
    export KUNJIN_PHASE41_CAPTURE_FILE="${capture}"
    if ! run_tracked "${emitted}" "${emit_error}" \
        "${PYTHON}" "${HELPER}" "emit-${output_kind}"; then
        printf '{"error_code":"acceptance_output_invalid","ok":false}\n' >&2
        return 70
    fi
    while IFS= read -r line || [[ -n "${line}" ]]; do
        printf '%s\n' "${line}"
    done <"${emitted}"
}

check_private_residue() {
    local check_out="${RUNTIME_DIR}/check.out"
    local check_err="${RUNTIME_DIR}/check.err"
    if ! run_tracked "${check_out}" "${check_err}" \
        "${PYTHON}" "${HELPER}" check-runtime; then
        printf '{"error_code":"phase41_runtime_permissions_invalid","ok":false}\n' >&2
        return 70
    fi
    if [[ ! -x /bin/ps ]]; then
        printf '{"error_code":"phase41_process_scan_unavailable","ok":false}\n' >&2
        return 70
    fi
    local scan_out="${RUNTIME_DIR}/process.out"
    local scan_err="${RUNTIME_DIR}/process.err"
    if ! run_tracked "${scan_out}" "${scan_err}" /bin/ps -axo pid=,command=; then
        printf '{"error_code":"phase41_process_scan_unavailable","ok":false}\n' >&2
        return 70
    fi
    local process_line
    while IFS= read -r process_line; do
        if [[ "${process_line}" == *"${RUNTIME_DIR}"* ]]; then
            printf '{"error_code":"phase41_process_residue","ok":false}\n' >&2
            return 70
        fi
    done <"${scan_out}"
}

run_tests() {
    local test_mode="$1"
    local output="${RUNTIME_DIR}/${test_mode}.out"
    local error="${RUNTIME_DIR}/${test_mode}.err"
    local result=0
    local tests=(tests/unit/test_phase41_acceptance.py)
    if [[ "${test_mode}" == "local" ]]; then
        tests+=(
            tests/unit/test_decision_health.py
            tests/unit/test_selection_scope.py
            tests/unit/test_selection_readiness.py
            tests/integration/test_cli.py
            tests/test_smoke.py
        )
    fi
    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}"
    local previous_directory="${PWD}"
    cd "${REPOSITORY_ROOT}"
    run_tracked "${output}" "${error}" "${PYTHON}" -m pytest -q \
        --basetemp "${RUNTIME_DIR}/pytest" "${tests[@]}" || result=$?
    cd "${previous_directory}"
    if [[ ${result} -ne 0 ]]; then
        printf '{"error_code":"phase41_%s_failed","ok":false}\n' "${test_mode}" >&2
        return "${result}"
    fi
    local clean_out="${RUNTIME_DIR}/clean.out"
    local clean_err="${RUNTIME_DIR}/clean.err"
    if ! run_tracked "${clean_out}" "${clean_err}" \
        "${PYTHON}" "${HELPER}" clean-tests; then
        printf '{"error_code":"phase41_test_cleanup_failed","ok":false}\n' >&2
        return 70
    fi
    emit_scanned "${output}" test
}

case "${MODE}" in
    local) run_tests local ;;
    fault) run_tests fault ;;
    engineering|owner)
        private_output="${RUNTIME_DIR}/${MODE}.out"
        private_error="${RUNTIME_DIR}/${MODE}.err"
        private_result=0
        run_tracked "${private_output}" "${private_error}" \
            "${PYTHON}" "${HELPER}" "${MODE}" || private_result=$?
        emit_scanned "${private_output}" private || private_result=$?
        check_private_residue || private_result=$?
        exit "${private_result}"
        ;;
esac
