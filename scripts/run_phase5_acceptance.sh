#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
unset PYTHONHOME PYTHONPATH
umask 077

usage() {
    printf '%s\n' 'usage: run_phase5_acceptance.sh local|fault' >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 2
fi
readonly MODE="$1"
case "${MODE}" in
    local|fault) ;;
    *) usage; exit 2 ;;
esac

if [[ "$0" != */* || -L "$0" ]]; then
    printf '%s\n' '{"error_code":"phase5_script_path_invalid","ok":false}' >&2
    exit 66
fi
readonly SCRIPT_DIR="$(cd -P -- "$(/usr/bin/dirname -- "$0")" && /bin/pwd -P)"
readonly REPOSITORY_ROOT="$(cd -P -- "${SCRIPT_DIR}/.." && /bin/pwd -P)"
readonly PYTHON="${REPOSITORY_ROOT}/.venv/bin/python"
readonly HELPER="${SCRIPT_DIR}/phase5_acceptance.py"
if [[ ! -x "${PYTHON}" || ! -f "${HELPER}" || -L "${HELPER}" ]]; then
    printf '%s\n' '{"error_code":"phase5_runtime_unavailable","ok":false}' >&2
    exit 69
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase5-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
export KUNJIN_PHASE5_RUNTIME_DIR="${RUNTIME_DIR}"
export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
/bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"
/bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}" "${PYTHONPYCACHEPREFIX}"

CHILD_PIDS=()
set -m

process_group_alive() {
    local pgid="$1"
    local result=0
    /bin/kill -0 "-${pgid}" 2>/dev/null || result=$?
    if [[ ${result} -eq 0 || ${result} -eq 1 ]]; then
        return "${result}"
    fi
    return 70
}

kill_process_group() {
    local pgid="$1"
    local group_state=0
    process_group_alive "${pgid}" || group_state=$?
    if [[ ${group_state} -gt 1 ]]; then
        return 70
    fi
    if [[ ${group_state} -eq 0 ]]; then
        /bin/kill -TERM "-${pgid}" 2>/dev/null || true
        /bin/kill -KILL "-${pgid}" 2>/dev/null || true
    fi
}

terminate_children() {
    local pid
    for pid in "${CHILD_PIDS[@]:-}"; do
        if [[ -n "${pid}" ]]; then
            kill_process_group "${pid}" || true
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
    if [[ ${result} -ne 0 ]]; then
        kill_process_group "${pid}" || true
        CHILD_PIDS=()
        return "${result}"
    fi
    local group_state=0
    process_group_alive "${pid}" || group_state=$?
    if [[ ${group_state} -gt 1 ]]; then
        CHILD_PIDS=()
        return 70
    fi
    if [[ ${group_state} -eq 0 ]]; then
        kill_process_group "${pid}" || true
        CHILD_PIDS=()
        return 70
    fi
    CHILD_PIDS=()
}

readonly MAX_CAPTURE_BYTES=32768
readonly SUMMARY_OUT="${RUNTIME_DIR}/summary.out"
readonly SUMMARY_ERR="${RUNTIME_DIR}/summary.err"
readonly VALIDATED_OUT="${RUNTIME_DIR}/validated.out"
readonly VALIDATED_ERR="${RUNTIME_DIR}/validated.err"

capture_is_bounded() {
    local path="$1"
    local size
    size="$(/usr/bin/wc -c <"${path}")" || return 1
    [[ "${size}" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] || return 1
    (( size <= MAX_CAPTURE_BYTES ))
}

if ! run_tracked "${SUMMARY_OUT}" "${SUMMARY_ERR}" \
    "${PYTHON}" "${HELPER}" produce "${MODE}"; then
    printf '%s\n' '{"error_code":"phase5_acceptance_tests_failed","ok":false}' >&2
    exit 70
fi
if ! capture_is_bounded "${SUMMARY_OUT}" || ! capture_is_bounded "${SUMMARY_ERR}" \
    || [[ ! -s "${SUMMARY_OUT}" || -s "${SUMMARY_ERR}" ]]; then
    printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
    exit 70
fi
/bin/chmod 600 "${SUMMARY_OUT}"

if ! run_tracked "${VALIDATED_OUT}" "${VALIDATED_ERR}" \
    "${PYTHON}" "${HELPER}" validate "${MODE}" "${SUMMARY_OUT}"; then
    printf '%s\n' '{"error_code":"phase5_acceptance_validation_failed","ok":false}' >&2
    exit 70
fi
if ! capture_is_bounded "${VALIDATED_OUT}" || ! capture_is_bounded "${VALIDATED_ERR}" \
    || [[ ! -s "${VALIDATED_OUT}" || -s "${VALIDATED_ERR}" ]]; then
    printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
    exit 70
fi
while IFS= read -r line || [[ -n "${line}" ]]; do
    printf '%s\n' "${line}"
done <"${VALIDATED_OUT}"
