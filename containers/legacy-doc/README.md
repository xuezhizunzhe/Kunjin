# Legacy Word Converter Image

This image is an optional, personal-use D1.1-B parser dependency. Provision it
only through an explicit reviewed setup. Normal `kunjin sync fund-documents`
never pulls or builds an image and the runtime always uses `--pull=never` and
`--network=none`.

## Supply Review

1. Resolve the official Debian image to a complete
   `name@sha256:<64-lowercase-hex>` reference. Resolve the exact Debian
   `libreoffice-writer-nogui` package version from the same reviewed package source.
2. Display and review both values before building. Do not use a mutable base
   tag or a package wildcard. Invoke the repository script directly, not through
   a symlink. It resolves parent-directory symlinks to the physical repository
   and rejects a symlink in the final script path.
3. Run the setup-only builder:

   ```bash
   scripts/build_legacy_doc_converter.sh \
     'debian:bookworm-slim@sha256:<reviewed-digest>' \
     '<exact-libreoffice-writer-nogui-version>'
   ```

   The script performs a manifest-probe build and a final build. Both use
   `linux/arm64`, `--pull`, `--no-cache`, `--provenance=false`, and `--iidfile`.
   Each `--iidfile` contains the private build-result digest returned by Buildx.
   Buildx v0.34.1 prefers the exporter config digest and falls back to the
   exporter image digest, so setup uses iidfile only as build evidence rather
   than assuming that rule applies to every Buildx version. For each build,
   setup creates a random private tag, first proves that tag does not exist, and
   uses it only to resolve the local config image ID. Setup then authenticates
   the image by that exact ID and rechecks that the private tag still resolves
   to the same ID.
   Runtime conversion never invokes a tag; it uses only the authenticated exact
   config image ID. KunJin's package manifest, labels, pinned base digest, exact
   LibreOffice version, Dockerfile checksum, parser provenance, and runtime
   no-network contract are unchanged.
   The script privately extracts the probe package manifest, passes its checksum
   to the final build, then
   requires the final image's actual manifest to match that checksum byte for
   byte. Repository drift between builds therefore fails closed instead of
   producing a false package-manifest label.
   The setup also requires the repository Dockerfile to be a regular,
   non-symlink file with reviewed SHA-256
   `1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b`.
   It rechecks that checksum before each build and includes it as
   `dockerfile_sha256` in the safe JSON result. Any reviewed Dockerfile change
   therefore requires a deliberate checksum update in the setup script.
   The reviewed no-GUI package reduces GUI dependencies. The
   `docker-libreoffice-v1` conversion contract remains unchanged, including the
   bounded HTML export, parser provenance, and fail-closed validation rules.
4. Export only the exact `KUNJIN_LEGACY_DOC_IMAGE_ID=sha256:...` command printed
   by the script, then run:

   ```bash
   .venv/bin/kunjin --json fund converter-status
   ```

5. Keep the converter unavailable if the exact image ID, `linux/arm64`
   architecture, contract label, base digest, LibreOffice version, package
   manifest checksum, or status differs from the reviewed result.

## Runtime Boundary

The Dockerfile declares `USER 65532:65532`, but the converter runtime supplies
`--user=<host-uid>:<host-gid>`. That deliberate runtime override lets the
non-root container write the private bind-mounted output with ownership that
the host process can authenticate and delete. It does not grant root or access
to the user's Home directory.

Runtime conversion uses an immutable local image ID, `--pull=never`,
`--network=none`, a read-only root, dropped capabilities, bounded resources,
and private input/output mounts. There is no host `textutil` fallback and no
host LibreOffice fallback. The conversion stdout and stderr are never captured,
returned, or persisted. Only private bounded metadata queries used to verify
the local image and cleanup state may be captured; those files stay in the
mode-0700 runtime directory and are deleted.

Conversion success is not financial evidence. It only produces untrusted HTML
for the normal identity, kind, period, active-content, ambiguity, and fact
checks. D1.1-C is still required for current report facts. D2 portfolio
construction, D3 product selection and pre-purchase checks, and Phase E remain
unimplemented. Every result remains `research_only`.
