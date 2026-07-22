#!/bin/bash
set -euo pipefail

readonly PATH="/usr/bin:/bin"
export PATH
unset PYTHONHOME PYTHONPATH
umask 077
readonly OWNER_ENTRYPOINT="/Users/yanzihao/KunJin/scripts/run_phase5_acceptance.sh"

usage() {
    printf '%s\n' 'usage: run_phase5_acceptance.sh local|fault|engineering|owner' >&2
}

if [[ $# -ne 1 ]]; then
    usage
    exit 2
fi
readonly MODE="$1"
case "${MODE}" in
    local|fault|engineering|owner) ;;
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
readonly OWNER_CAPTURE_HELPER="${SCRIPT_DIR}/phase5_owner_capture.py"
if [[ ! -x "${PYTHON}" || ! -f "${HELPER}" || -L "${HELPER}" ]]; then
    printf '%s\n' '{"error_code":"phase5_runtime_unavailable","ok":false}' >&2
    exit 69
fi

if [[ "${MODE}" == "engineering" || "${MODE}" == "owner" ]]; then
    if [[ -n "${KUNJIN_DATA_DIR:-}" || -n "${KUNJIN_STATE_DIR:-}" ]]; then
        printf '%s\n' '{"error_code":"phase5_private_runtime_override","ok":false}' >&2
        exit 77
    fi
    if [[ "${MODE}" == "engineering" ]]; then
        if [[ -n "${KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE:-}" \
            || -n "${KUNJIN_PHASE5_OWNER_SUBJECT_FILE:-}" \
            || -n "${KUNJIN_PHASE5_OWNER_APPROVED:-}" ]]; then
            printf '%s\n' '{"error_code":"phase5_engineering_private_input_prohibited","ok":false}' >&2
            exit 77
        fi
    else
        if [[ "$0" != "${OWNER_ENTRYPOINT}" \
            || "${KUNJIN_PHASE5_OWNER_APPROVED:-}" != "explicit_private_read_only_review" \
            || -z "${KUNJIN_PHASE5_OWNER_SUBJECT_FILE:-}" \
            || -n "${KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE:-}" ]]; then
            printf '%s\n' '{"error_code":"phase5_owner_approval_required","ok":false}' >&2
            exit 77
        fi
        if [[ ! -f "${OWNER_CAPTURE_HELPER}" || -L "${OWNER_CAPTURE_HELPER}" ]]; then
            printf '%s\n' '{"error_code":"phase5_runtime_unavailable","ok":false}' >&2
            exit 69
        fi
    fi
fi

readonly RUNTIME_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-phase5-acceptance.XXXXXXXX)"
/bin/chmod 700 "${RUNTIME_DIR}"
export KUNJIN_PHASE5_RUNTIME_DIR="${RUNTIME_DIR}"
export PYTHONPYCACHEPREFIX="${RUNTIME_DIR}/pycache"
/bin/mkdir -p "${PYTHONPYCACHEPREFIX}"
/bin/chmod 700 "${PYTHONPYCACHEPREFIX}"
if [[ "${MODE}" == "local" || "${MODE}" == "fault" ]]; then
    export KUNJIN_DATA_DIR="${RUNTIME_DIR}/data"
    export KUNJIN_STATE_DIR="${RUNTIME_DIR}/state"
    /bin/mkdir -p "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}"
    /bin/chmod 700 "${KUNJIN_DATA_DIR}" "${KUNJIN_STATE_DIR}"
fi

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
    if [[ "${MODE}" == "owner" && -n "${PACKAGE_ROOT:-}" ]]; then
        if [[ "${capture_sealed:-0}" -eq 0 || "${acceptance_passed:-0}" -eq 1 ]]; then
            /bin/rm -rf -- "${PACKAGE_ROOT}"
        fi
    fi
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
readonly COMPARE_OUT="${RUNTIME_DIR}/compare.out"
readonly COMPARE_ERR="${RUNTIME_DIR}/compare.err"
readonly CAPTURE_OUT="${RUNTIME_DIR}/capture.out"
readonly CAPTURE_ERR="${RUNTIME_DIR}/capture.err"
readonly REPLAY_A_OUT="${RUNTIME_DIR}/replay-a.out"
readonly REPLAY_A_ERR="${RUNTIME_DIR}/replay-a.err"
readonly REPLAY_B_OUT="${RUNTIME_DIR}/replay-b.out"
readonly REPLAY_B_ERR="${RUNTIME_DIR}/replay-b.err"

capture_attempted=0
capture_sealed=0
acceptance_passed=0
PACKAGE_ROOT=""
if [[ "${MODE}" == "owner" ]]; then
    readonly PACKAGE_BASE="/private/tmp/kunjin-phase5-owner-captures"
    /bin/mkdir -p -- "${PACKAGE_BASE}"
    /bin/chmod 700 "${PACKAGE_BASE}"
    readonly CAPTURE_ID="$(/usr/bin/uuidgen | /usr/bin/tr -d '-')"
    readonly PACKAGE_ROOT="${PACKAGE_BASE}/${CAPTURE_ID}"
fi

capture_is_bounded() {
    local path="$1"
    local size
    size="$(/usr/bin/wc -c <"${path}")" || return 1
    [[ "${size}" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] || return 1
    (( size <= MAX_CAPTURE_BYTES ))
}

private_failure() {
    local result="$1"
    local failure_stage=""
    case "${result}" in
        71) failure_stage="private_input" ;;
        72) failure_stage="owner_keychain" ;;
        73) failure_stage="private_database_snapshot" ;;
        74) failure_stage="private_flow" ;;
        75) failure_stage="private_verification" ;;
    esac
    if [[ -n "${failure_stage}" ]]; then
        printf '{"error_code":"phase5_acceptance_tests_failed","failure_stage":"%s","ok":false}\n' \
            "${failure_stage}" >&2
    else
        printf '%s\n' '{"error_code":"phase5_acceptance_tests_failed","ok":false}' >&2
    fi
}

if [[ "${MODE}" == "owner" ]]; then
    capture_attempted=1
    capture_result=0
    run_tracked "${CAPTURE_OUT}" "${CAPTURE_ERR}" \
        "${PYTHON}" "${OWNER_CAPTURE_HELPER}" "${PACKAGE_ROOT}" || capture_result=$?
    unset KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE
    unset KUNJIN_PHASE5_OWNER_SUBJECT_FILE
    unset KUNJIN_PHASE5_OWNER_APPROVED
    if [[ -f "${PACKAGE_ROOT}/capture-complete" ]]; then
        capture_sealed=1
    fi
    if [[ ${capture_result} -ne 0 ]]; then
        private_failure "${capture_result}"
        exit 70
    fi
    if ! capture_is_bounded "${CAPTURE_OUT}" || ! capture_is_bounded "${CAPTURE_ERR}" \
        || [[ ! -s "${CAPTURE_OUT}" || -s "${CAPTURE_ERR}" ]]; then
        printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
        exit 70
    fi
    /bin/chmod 600 "${CAPTURE_OUT}"

    readonly REPLAY_A="${RUNTIME_DIR}/replay-a"
    readonly REPLAY_B="${RUNTIME_DIR}/replay-b"
    replay_result=0
    run_tracked "${REPLAY_A_OUT}" "${REPLAY_A_ERR}" \
        "${PYTHON}" "${HELPER}" replay "${PACKAGE_ROOT}" "${REPLAY_A}" || replay_result=$?
    if [[ ${replay_result} -eq 0 ]]; then
        run_tracked "${REPLAY_B_OUT}" "${REPLAY_B_ERR}" \
            "${PYTHON}" "${HELPER}" replay "${PACKAGE_ROOT}" "${REPLAY_B}" || replay_result=$?
    fi
    if [[ ${replay_result} -ne 0 ]]; then
        private_failure "${replay_result}"
        exit 70
    fi
    if ! capture_is_bounded "${REPLAY_A_OUT}" || ! capture_is_bounded "${REPLAY_A_ERR}" \
        || ! capture_is_bounded "${REPLAY_B_OUT}" || ! capture_is_bounded "${REPLAY_B_ERR}" \
        || [[ ! -s "${REPLAY_A_OUT}" || -s "${REPLAY_A_ERR}" \
            || ! -s "${REPLAY_B_OUT}" || -s "${REPLAY_B_ERR}" ]]; then
        printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
        exit 70
    fi
    first_result="${REPLAY_A}/protected-replay-result.json"
    second_result="${REPLAY_B}/protected-replay-result.json"
    if [[ ! -f "${first_result}" || ! -f "${second_result}" ]]; then
        printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
        exit 70
    fi
    run_tracked "${COMPARE_OUT}" "${COMPARE_ERR}" \
        "${PYTHON}" "${HELPER}" compare owner "${first_result}" "${second_result}" "${SUMMARY_OUT}" || replay_result=$?
    if [[ ${replay_result} -ne 0 ]]; then
        private_failure "${replay_result}"
        exit 70
    fi
    if ! capture_is_bounded "${COMPARE_OUT}" || ! capture_is_bounded "${COMPARE_ERR}" \
        || [[ ! -s "${COMPARE_OUT}" || -s "${COMPARE_ERR}" ]]; then
        printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
        exit 70
    fi
else
    produce_result=0
    run_tracked "${SUMMARY_OUT}" "${SUMMARY_ERR}" \
        "${PYTHON}" "${HELPER}" produce "${MODE}" || produce_result=$?
    if [[ ${produce_result} -ne 0 ]]; then
        private_failure "${produce_result}"
        exit 70
    fi
fi

if [[ ! -f "${SUMMARY_OUT}" ]] || ! capture_is_bounded "${SUMMARY_OUT}"; then
    printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
    exit 70
fi
if [[ "${MODE}" != "owner" ]] && { ! capture_is_bounded "${SUMMARY_ERR}" || [[ -s "${SUMMARY_ERR}" ]]; }; then
    printf '%s\n' '{"error_code":"phase5_acceptance_output_invalid","ok":false}' >&2
    exit 70
fi
/bin/chmod 600 "${SUMMARY_OUT}"
if [[ "${MODE}" == "engineering" || "${MODE}" == "owner" ]]; then
    unset KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE
    unset KUNJIN_PHASE5_OWNER_SUBJECT_FILE
    unset KUNJIN_PHASE5_OWNER_APPROVED
fi

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
if [[ "${MODE}" == "owner" ]]; then
    /bin/rm -rf -- "${PACKAGE_ROOT}"
    acceptance_passed=1
fi
while IFS= read -r line || [[ -n "${line}" ]]; do
    printf '%s\n' "${line}"
done <"${VALIDATED_OUT}"
