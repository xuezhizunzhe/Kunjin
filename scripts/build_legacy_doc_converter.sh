#!/bin/bash
set -euo pipefail

readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"
readonly PATH="${DOCKER_DESKTOP_BIN}:/usr/bin:/bin"
export PATH

readonly TARGET_PLATFORM="linux/arm64"
readonly CONTRACT_VERSION="docker-libreoffice-v1"
readonly DOCKER_BIN="/usr/local/bin/docker"
readonly EXPECTED_DOCKER_CLI="${DOCKER_DESKTOP_BIN}/docker"
readonly EXPECTED_DOCKER_CREDENTIAL_HELPER="${DOCKER_DESKTOP_BIN}/docker-credential-desktop"
readonly EXPECTED_DOCKERFILE_SHA256="1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b"
SCRIPT_SOURCE="${BASH_SOURCE[0]}"
if [[ "${SCRIPT_SOURCE}" != */* || "${SCRIPT_SOURCE}" == *$'\n'* ]]; then
    printf 'setup script must be invoked by an explicit path\n' >&2
    exit 64
fi
if [[ "${SCRIPT_SOURCE}" != /* ]]; then
    SCRIPT_SOURCE="${PWD}/${SCRIPT_SOURCE}"
fi
if [[ -L "${SCRIPT_SOURCE}" ]]; then
    printf 'setup script symlink invocation is rejected\n' >&2
    exit 66
fi
readonly SCRIPT_BASENAME="${SCRIPT_SOURCE##*/}"
readonly LEXICAL_SCRIPT_DIRECTORY="${SCRIPT_SOURCE%/*}"
readonly PHYSICAL_SCRIPT_DIRECTORY="$(cd -P "${LEXICAL_SCRIPT_DIRECTORY}" && pwd -P)"
SCRIPT_SOURCE="${PHYSICAL_SCRIPT_DIRECTORY}/${SCRIPT_BASENAME}"
if [[ ! -f "${SCRIPT_SOURCE}" || -L "${SCRIPT_SOURCE}" ]]; then
    printf 'physical setup script is not a trusted regular file\n' >&2
    exit 66
fi
readonly SCRIPT_SOURCE
readonly ROOT_DIR="$(cd -P "${PHYSICAL_SCRIPT_DIRECTORY}/.." && pwd -P)"
readonly BUILD_CONTEXT="${ROOT_DIR}/containers/legacy-doc"
readonly REPOSITORY_DOCKERFILE="${ROOT_DIR}/containers/legacy-doc/Dockerfile"
readonly DOCKERFILE="${REPOSITORY_DOCKERFILE}"
readonly MAX_METADATA_BYTES=4096
readonly MAX_MANIFEST_BYTES=1048576
readonly MAX_METADATA_BLOCKS=8
readonly MAX_MANIFEST_BLOCKS=2048

require_reviewed_dockerfile() {
    local actual_checksum remainder
    [[ -f "${REPOSITORY_DOCKERFILE}" && ! -L "${REPOSITORY_DOCKERFILE}" ]] || return 1
    read -r actual_checksum remainder \
        < <(/usr/bin/shasum -a 256 "${REPOSITORY_DOCKERFILE}")
    [[ -n "${remainder}" && "${actual_checksum}" =~ ^[0-9a-f]{64}$ ]] || return 1
    [[ "${actual_checksum}" == "${EXPECTED_DOCKERFILE_SHA256}" ]]
}

if [[ $# -ne 2 ]]; then
    printf 'usage: %s BASE_IMAGE@sha256:DIGEST EXACT_LIBREOFFICE_VERSION\n' "$0" >&2
    exit 64
fi

readonly BASE_IMAGE="$1"
readonly LIBREOFFICE_VERSION="$2"

if [[ ! "${BASE_IMAGE}" =~ ^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$ ]]; then
    printf 'BASE_IMAGE must be an exact name@sha256 digest reference\n' >&2
    exit 65
fi
if [[ ! "${LIBREOFFICE_VERSION}" =~ ^[0-9][0-9A-Za-z.+:~_-]{0,127}$ ]]; then
    printf 'LIBREOFFICE_VERSION must be one exact package version\n' >&2
    exit 65
fi
if ! require_reviewed_dockerfile; then
    printf 'reviewed legacy converter Dockerfile authentication failed\n' >&2
    exit 66
fi
if [[ ! -L "${DOCKER_BIN}" || "$(/usr/bin/readlink "${DOCKER_BIN}")" != "${EXPECTED_DOCKER_CLI}" ]]; then
    printf 'trusted Docker Desktop CLI link is unavailable\n' >&2
    exit 69
fi
if ! [[ -x "${EXPECTED_DOCKER_CLI}" \
     && -f "${EXPECTED_DOCKER_CLI}" \
     && ! -L "${EXPECTED_DOCKER_CLI}" \
     && -x "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
     && -f "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
     && ! -L "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
     && -f "${DOCKERFILE}" ]]; then
    printf 'trusted Docker setup prerequisites are unavailable\n' >&2
    exit 69
fi

readonly BASE_IMAGE_DIGEST="${BASE_IMAGE##*@}"
readonly PRIVATE_DIR="$(/usr/bin/mktemp -d /private/tmp/kunjin-legacy-build.XXXXXXXX)"
readonly PROBE_IIDFILE="${PRIVATE_DIR}/probe.iid"
readonly FINAL_IIDFILE="${PRIVATE_DIR}/final.iid"
readonly PROBE_MANIFEST="${PRIVATE_DIR}/probe.manifest"
readonly FINAL_MANIFEST="${PRIVATE_DIR}/final.manifest"
readonly PROBE_INSPECT="${PRIVATE_DIR}/probe.inspect"
readonly FINAL_INSPECT="${PRIVATE_DIR}/final.inspect"
readonly PROBE_TAG_INSPECT="${PRIVATE_DIR}/probe-tag.inspect"
readonly PROBE_TAG_RECHECK="${PRIVATE_DIR}/probe-tag-recheck"
readonly FINAL_TAG_INSPECT="${PRIVATE_DIR}/final-tag.inspect"
readonly FINAL_TAG_RECHECK="${PRIVATE_DIR}/final-tag-recheck"
readonly CONTAINER_ABSENCE="${PRIVATE_DIR}/container-absence"
readonly TAG_ABSENCE="${PRIVATE_DIR}/tag-absence"
readonly CONTAINER_CIDFILE="${PRIVATE_DIR}/container.cid"
readonly BUILD_TOKEN="$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')"
if [[ ! "${BUILD_TOKEN}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
    /bin/rm -rf "${PRIVATE_DIR}"
    printf 'private build identity generation failed\n' >&2
    exit 70
fi
readonly PROBE_BUILD_TAG="kunjin-legacy-probe-${BUILD_TOKEN}:setup"
readonly FINAL_BUILD_TAG="kunjin-legacy-final-${BUILD_TOKEN}:setup"
if [[ ! "${PROBE_BUILD_TAG}" =~ ^kunjin-legacy-probe-[0-9a-f-]{36}:setup$ \
   || ! "${FINAL_BUILD_TAG}" =~ ^kunjin-legacy-final-[0-9a-f-]{36}:setup$ ]]; then
    /bin/rm -rf "${PRIVATE_DIR}"
    printf 'private image tag validation failed\n' >&2
    exit 70
fi
ACTIVE_CONTAINER_ID=""
ACTIVE_CONTAINER_NAME=""
PROBE_IMAGE_ID=""
FINAL_IMAGE_ID=""
FINAL_IMAGE_VERIFIED=0
PROBE_TAG_OWNED=0
FINAL_TAG_OWNED=0
RECOVERED_CONTAINER_ID=""

/bin/chmod 700 "${PRIVATE_DIR}"

require_sha256_digest_file() {
    local iidfile="$1"
    local byte_count digest
    [[ -f "${iidfile}" && ! -L "${iidfile}" ]] || return 1
    byte_count="$(/usr/bin/wc -c < "${iidfile}")"
    byte_count="${byte_count//[[:space:]]/}"
    [[ "${byte_count}" == "71" || "${byte_count}" == "72" ]] || return 1
    digest="$(<"${iidfile}")"
    [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    # Buildx writes no final newline; accept that or exactly one trailing LF.
    if [[ "${byte_count}" == "71" ]]; then
        /usr/bin/cmp -s "${iidfile}" <(printf '%s' "${digest}") || return 1
    else
        /usr/bin/cmp -s "${iidfile}" <(printf '%s\n' "${digest}") || return 1
    fi
    printf '%s\n' "${digest}"
}

recover_container_id() {
    local byte_count candidate
    RECOVERED_CONTAINER_ID=""
    if [[ -f "${CONTAINER_CIDFILE}" && ! -L "${CONTAINER_CIDFILE}" ]]; then
        byte_count="$(/usr/bin/wc -c < "${CONTAINER_CIDFILE}")"
        byte_count="${byte_count//[[:space:]]/}"
        if [[ "${byte_count}" == "64" || "${byte_count}" == "65" ]]; then
            candidate="$(<"${CONTAINER_CIDFILE}")"
            if [[ "${candidate}" =~ ^[0-9a-f]{64}$ ]]; then
                if [[ "${byte_count}" == "64" ]] \
                   && /usr/bin/cmp -s "${CONTAINER_CIDFILE}" <(printf '%s' "${candidate}"); then
                    RECOVERED_CONTAINER_ID="${candidate}"
                elif [[ "${byte_count}" == "65" ]] \
                     && /usr/bin/cmp -s "${CONTAINER_CIDFILE}" <(printf '%s\n' "${candidate}"); then
                    RECOVERED_CONTAINER_ID="${candidate}"
                fi
            fi
        fi
    fi
    return 0
}

cleanup() {
    local container_id="${ACTIVE_CONTAINER_ID}"
    local probe_id="${PROBE_IMAGE_ID}"
    local final_id="${FINAL_IMAGE_ID}"

    if [[ ! "${container_id}" =~ ^[0-9a-f]{64}$ ]]; then
        recover_container_id
        container_id="${RECOVERED_CONTAINER_ID}"
    fi
    if [[ "${container_id}" =~ ^[0-9a-f]{64}$ ]]; then
        "${DOCKER_BIN}" container rm --force "${container_id}" >/dev/null 2>&1 || true
    fi
    if [[ "${ACTIVE_CONTAINER_NAME}" =~ ^kunjin-legacy-meta-[0-9a-f-]{36}$ ]]; then
        "${DOCKER_BIN}" container rm --force "${ACTIVE_CONTAINER_NAME}" \
            >/dev/null 2>&1 || true
    fi

    if [[ "${probe_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        "${DOCKER_BIN}" image rm "${probe_id}" >/dev/null 2>&1 || true
    fi
    if [[ "${PROBE_TAG_OWNED}" -eq 1 ]]; then
        "${DOCKER_BIN}" image rm "${PROBE_BUILD_TAG}" >/dev/null 2>&1 || true
    fi

    if [[ "${FINAL_IMAGE_VERIFIED}" -ne 1 ]]; then
        if [[ "${final_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            "${DOCKER_BIN}" image rm "${final_id}" >/dev/null 2>&1 || true
        fi
        if [[ "${FINAL_TAG_OWNED}" -eq 1 ]]; then
            "${DOCKER_BIN}" image rm "${FINAL_BUILD_TAG}" >/dev/null 2>&1 || true
        fi
    fi
    /bin/rm -rf "${PRIVATE_DIR}"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

capture_metadata() {
    local output_path="$1"
    shift
    : > "${output_path}"
    /bin/chmod 600 "${output_path}"
    (ulimit -f "${MAX_METADATA_BLOCKS}"; "$@" > "${output_path}" 2>/dev/null)
    [[ -f "${output_path}" && ! -L "${output_path}" ]] || return 1
    [[ "$(/usr/bin/wc -c < "${output_path}")" -le "${MAX_METADATA_BYTES}" ]] || return 1
    [[ "$(/usr/bin/wc -l < "${output_path}")" -eq 1 ]] || return 1
    ! LC_ALL=C /usr/bin/grep -q $'\r' "${output_path}"
}

resolve_tag_image_id() {
    local image_tag="$1"
    local output_path="$2"
    local image_id image_os image_arch image_extra
    capture_metadata \
        "${output_path}" \
        "${DOCKER_BIN}" image inspect "${image_tag}" \
        --format '{{printf "%s\t%s\t%s" .Id .Os .Architecture}}'
    IFS=$'\t' read -r image_id image_os image_arch image_extra < "${output_path}"
    [[ -z "${image_extra:-}" ]]
    [[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]
    [[ "${image_os}/${image_arch}" == "${TARGET_PLATFORM}" ]]
    printf '%s\n' "${image_id}"
}

capture_bounded_output() {
    local output_path="$1"
    shift
    : > "${output_path}"
    /bin/chmod 600 "${output_path}"
    (ulimit -f "${MAX_METADATA_BLOCKS}"; "$@" > "${output_path}" 2>/dev/null)
    [[ -f "${output_path}" && ! -L "${output_path}" ]] || return 1
    [[ "$(/usr/bin/wc -c < "${output_path}")" -le "${MAX_METADATA_BYTES}" ]] || return 1
    ! LC_ALL=C /usr/bin/grep -q $'\r' "${output_path}"
}

require_container_name_absent() {
    local container_name="$1"
    [[ "${container_name}" =~ ^kunjin-legacy-meta-[0-9a-f-]{36}$ ]] || return 1
    capture_bounded_output \
        "${CONTAINER_ABSENCE}" \
        "${DOCKER_BIN}" container ls --all --no-trunc --quiet \
        --filter "name=^${container_name}$"
    [[ ! -s "${CONTAINER_ABSENCE}" ]]
}

new_private_uuid() {
    local value
    value="$(/usr/bin/uuidgen | /usr/bin/tr '[:upper:]' '[:lower:]')"
    [[ "${value}" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]] \
        || return 1
    printf '%s\n' "${value}"
}

copy_image_file() {
    local image_id="$1"
    local source_path="$2"
    local destination_path="$3"
    local container_token

    if [[ -e "${CONTAINER_CIDFILE}" || -L "${CONTAINER_CIDFILE}" ]]; then
        [[ -f "${CONTAINER_CIDFILE}" && ! -L "${CONTAINER_CIDFILE}" ]] || return 1
        /bin/rm -f "${CONTAINER_CIDFILE}"
    fi
    container_token="$(new_private_uuid)"
    local container_name="kunjin-legacy-meta-${container_token}"
    [[ "${container_name}" =~ ^kunjin-legacy-meta-[0-9a-f-]{36}$ ]] || return 1
    require_container_name_absent "${container_name}"
    ACTIVE_CONTAINER_NAME="${container_name}"
    "${DOCKER_BIN}" container create \
        --pull=never \
        --network=none \
        --name "${ACTIVE_CONTAINER_NAME}" \
        --cidfile "${CONTAINER_CIDFILE}" \
        "${image_id}" /bin/true >/dev/null 2>&1
    recover_container_id
    ACTIVE_CONTAINER_ID="${RECOVERED_CONTAINER_ID}"
    [[ "${ACTIVE_CONTAINER_ID}" =~ ^[0-9a-f]{64}$ ]] || return 1
    (
        ulimit -f "${MAX_MANIFEST_BLOCKS}"
        "${DOCKER_BIN}" container cp \
            "${ACTIVE_CONTAINER_ID}:${source_path}" "${destination_path}" >/dev/null 2>&1
    )
    "${DOCKER_BIN}" container rm --force "${ACTIVE_CONTAINER_ID}" >/dev/null 2>&1
    require_container_name_absent "${ACTIVE_CONTAINER_NAME}"
    ACTIVE_CONTAINER_ID=""
    ACTIVE_CONTAINER_NAME=""
    /bin/rm -f "${CONTAINER_CIDFILE}"
    [[ -f "${destination_path}" && ! -L "${destination_path}" ]] || return 1
    [[ "$(/usr/bin/wc -c < "${destination_path}")" -le "${MAX_MANIFEST_BYTES}" ]] \
        || return 1
    /bin/chmod 600 "${destination_path}"
}

remove_probe_image() {
    local probe_id="${PROBE_IMAGE_ID}"
    [[ "${probe_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    "${DOCKER_BIN}" image rm "${probe_id}" >/dev/null 2>&1
    PROBE_IMAGE_ID=""
    PROBE_TAG_OWNED=0
}

sha256_file() {
    local path="$1"
    local digest remainder
    read -r digest remainder < <(/usr/bin/shasum -a 256 "${path}")
    [[ -n "${remainder}" && "${digest}" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "${digest}"
}

require_private_tag_absent() {
    local image_tag="$1"
    [[ "${image_tag}" =~ ^kunjin-legacy-(probe|final)-[0-9a-f-]{36}:setup$ ]] || return 1
    capture_bounded_output \
        "${TAG_ABSENCE}" \
        "${DOCKER_BIN}" image ls --no-trunc --quiet --filter "reference=${image_tag}"
    [[ ! -s "${TAG_ABSENCE}" ]]
}

report_stage() {
    local stage="$1"
    case "${stage}" in
        probe_build|probe_identity|probe_manifest|final_build|final_identity|final_manifest|ready) ;;
        *)
            printf 'invalid setup stage\n' >&2
            exit 70
            ;;
    esac
    printf 'setup stage: %s\n' "${stage}" >&2
}

# Explicit setup is the only phase allowed to pull or use the network.
report_stage probe_build
require_private_tag_absent "${PROBE_BUILD_TAG}"
PROBE_TAG_OWNED=1
require_reviewed_dockerfile
"${DOCKER_BIN}" build \
    --platform "${TARGET_PLATFORM}" \
    --pull \
    --no-cache \
    --provenance=false \
    --iidfile "${PROBE_IIDFILE}" \
    --tag "${PROBE_BUILD_TAG}" \
    --target manifest-probe \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --build-arg "BASE_IMAGE_DIGEST=${BASE_IMAGE_DIGEST}" \
    --build-arg "LIBREOFFICE_VERSION=${LIBREOFFICE_VERSION}" \
    --file "${DOCKERFILE}" \
    "${BUILD_CONTEXT}"

report_stage probe_identity
PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"
readonly PROBE_BUILD_DIGEST
PROBE_IMAGE_ID="$(resolve_tag_image_id "${PROBE_BUILD_TAG}" "${PROBE_TAG_INSPECT}")"
capture_metadata \
    "${PROBE_INSPECT}" \
    "${DOCKER_BIN}" image inspect "${PROBE_IMAGE_ID}" \
    --format '{{printf "%s\t%s\t%s" .Id .Os .Architecture}}'
IFS=$'\t' read -r probe_id probe_os probe_arch probe_extra < "${PROBE_INSPECT}"
[[ -z "${probe_extra:-}" ]]
[[ "${probe_id}" == "${PROBE_IMAGE_ID}" && "${probe_os}/${probe_arch}" == "${TARGET_PLATFORM}" ]]
capture_metadata \
    "${PROBE_TAG_RECHECK}" \
    "${DOCKER_BIN}" image inspect "${PROBE_BUILD_TAG}" \
    --format '{{.Id}}'
IFS= read -r probe_tag_id < "${PROBE_TAG_RECHECK}"
[[ "${probe_tag_id}" == "${PROBE_IMAGE_ID}" ]]

report_stage probe_manifest
copy_image_file "${PROBE_IMAGE_ID}" /opt/kunjin-package-manifest.txt "${PROBE_MANIFEST}"
/usr/bin/grep -Fx "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" \
    "${PROBE_MANIFEST}" >/dev/null
readonly PACKAGE_MANIFEST_SHA256="$(sha256_file "${PROBE_MANIFEST}")"

report_stage final_build
require_private_tag_absent "${FINAL_BUILD_TAG}"
FINAL_TAG_OWNED=1
require_reviewed_dockerfile
"${DOCKER_BIN}" build \
    --platform "${TARGET_PLATFORM}" \
    --pull \
    --no-cache \
    --provenance=false \
    --iidfile "${FINAL_IIDFILE}" \
    --tag "${FINAL_BUILD_TAG}" \
    --target runtime \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --build-arg "BASE_IMAGE_DIGEST=${BASE_IMAGE_DIGEST}" \
    --build-arg "LIBREOFFICE_VERSION=${LIBREOFFICE_VERSION}" \
    --build-arg "PACKAGE_MANIFEST_SHA256=${PACKAGE_MANIFEST_SHA256}" \
    --file "${DOCKERFILE}" \
    "${BUILD_CONTEXT}"

report_stage final_identity
FINAL_BUILD_DIGEST="$(require_sha256_digest_file "${FINAL_IIDFILE}")"
readonly FINAL_BUILD_DIGEST
FINAL_IMAGE_ID="$(resolve_tag_image_id "${FINAL_BUILD_TAG}" "${FINAL_TAG_INSPECT}")"
capture_metadata \
    "${FINAL_INSPECT}" \
    "${DOCKER_BIN}" image inspect "${FINAL_IMAGE_ID}" \
    --format '{{printf "%s\t%s\t%s\t%s\t%s\t%s\t%s" .Id .Os .Architecture (index .Config.Labels "com.kunjin.legacy-doc.contract") (index .Config.Labels "com.kunjin.legacy-doc.base-image-digest") (index .Config.Labels "com.kunjin.legacy-doc.libreoffice-version") (index .Config.Labels "com.kunjin.legacy-doc.package-manifest-sha256")}}'
IFS=$'\t' read -r final_id final_os final_arch final_contract final_base_digest \
    final_libreoffice_version final_manifest_checksum final_extra < "${FINAL_INSPECT}"
[[ -z "${final_extra:-}" ]]
[[ "${final_id}" == "${FINAL_IMAGE_ID}" ]]
[[ "${final_os}/${final_arch}" == "${TARGET_PLATFORM}" ]]
[[ "${final_contract}" == "${CONTRACT_VERSION}" ]]
[[ "${final_base_digest}" == "${BASE_IMAGE_DIGEST}" ]]
[[ "${final_libreoffice_version}" == "${LIBREOFFICE_VERSION}" ]]
[[ "${final_manifest_checksum}" == "${PACKAGE_MANIFEST_SHA256}" ]]
capture_metadata \
    "${FINAL_TAG_RECHECK}" \
    "${DOCKER_BIN}" image inspect "${FINAL_BUILD_TAG}" \
    --format '{{.Id}}'
IFS= read -r final_tag_id < "${FINAL_TAG_RECHECK}"
[[ "${final_tag_id}" == "${FINAL_IMAGE_ID}" ]]

report_stage final_manifest
copy_image_file "${FINAL_IMAGE_ID}" /opt/kunjin-package-manifest.txt "${FINAL_MANIFEST}"
[[ "$(sha256_file "${FINAL_MANIFEST}")" == "${PACKAGE_MANIFEST_SHA256}" ]]
/usr/bin/cmp -s "${PROBE_MANIFEST}" "${FINAL_MANIFEST}"
/usr/bin/grep -Fx "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" \
    "${FINAL_MANIFEST}" >/dev/null
remove_probe_image
FINAL_IMAGE_VERIFIED=1

report_stage ready
printf '{"architecture":"%s","base_image_digest":"%s","contract":"%s","dockerfile_sha256":"%s","image_id":"%s","libreoffice_version":"%s","package_manifest_sha256":"%s","status":"ready"}\n' \
    "${TARGET_PLATFORM}" "${BASE_IMAGE_DIGEST}" "${CONTRACT_VERSION}" \
    "${EXPECTED_DOCKERFILE_SHA256}" "${FINAL_IMAGE_ID}" "${LIBREOFFICE_VERSION}" \
    "${PACKAGE_MANIFEST_SHA256}"
printf "export KUNJIN_LEGACY_DOC_IMAGE_ID='%s'\n" "${FINAL_IMAGE_ID}"
