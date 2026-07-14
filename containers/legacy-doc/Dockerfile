# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS manifest-probe

ARG BASE_IMAGE
ARG LIBREOFFICE_VERSION
ARG BASE_IMAGE_DIGEST

RUN printf '%s\n' "${BASE_IMAGE}" \
      | grep -Eq '^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$' \
 && printf '%s\n' "${BASE_IMAGE_DIGEST}" | grep -Eq '^sha256:[0-9a-f]{64}$' \
 && case "${BASE_IMAGE}" in *@"${BASE_IMAGE_DIGEST}") ;; *) exit 1 ;; esac \
 && printf '%s\n' "${LIBREOFFICE_VERSION}" \
      | grep -Eq '^[0-9][0-9A-Za-z.+:~_-]{0,127}$' \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" ca-certificates fontconfig fonts-noto-cjk \
 && libreoffice --version > /opt/kunjin-libreoffice-version.txt \
 && dpkg-query -W -f='${binary:Package}=${Version}\n' \
      | LC_ALL=C sort > /opt/kunjin-package-manifest.txt \
 && grep -Fx "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" \
      /opt/kunjin-package-manifest.txt \
 && rm -rf /var/lib/apt/lists/*

USER 65532:65532
WORKDIR /tmp

FROM manifest-probe AS runtime

USER root
ARG LIBREOFFICE_VERSION
ARG BASE_IMAGE_DIGEST
ARG PACKAGE_MANIFEST_SHA256

RUN printf '%s\n' "${PACKAGE_MANIFEST_SHA256}" | grep -Eq '^[0-9a-f]{64}$' \
 && test "$(sha256sum /opt/kunjin-package-manifest.txt | cut -d ' ' -f 1)" \
      = "${PACKAGE_MANIFEST_SHA256}" \
 && grep -Fx "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" \
      /opt/kunjin-package-manifest.txt

LABEL com.kunjin.legacy-doc.contract="docker-libreoffice-v1" \
      com.kunjin.legacy-doc.base-image-digest="${BASE_IMAGE_DIGEST}" \
      com.kunjin.legacy-doc.libreoffice-version="${LIBREOFFICE_VERSION}" \
      com.kunjin.legacy-doc.package-manifest-sha256="${PACKAGE_MANIFEST_SHA256}"

USER 65532:65532
WORKDIR /tmp
