# KunJin BuildKit Manifest And Config Identity Implementation Plan

> **For agentic workers:** Implement task-by-task with fresh subagents, test-first evidence, and independent review.

**Goal:** Treat Buildx iidfile output as private build evidence while keeping runtime bound to an independently authenticated exact local config image ID.

**Architecture:** Validate iidfile as private build-result evidence, resolve the newly built config ID through a random tag proven absent before build, re-inspect by exact ID, and recheck the tag did not change. Remove cleanup logic that incorrectly treats iidfile content as a local image ID.

**Tech Stack:** Bash, Docker Desktop 29, Python unittest/pytest smoke tests.

---

### Task 1: Replace The Old Equality Contract Tests

**Files:**
- Modify: `tests/test_smoke.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Require distinct build-digest and image-ID variables**

Assert the script contains exact probe and final build-digest assignments from
their iidfiles, and separate image-ID assignments from private tag resolution:

```bash
readonly PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"
PROBE_IMAGE_ID="$(resolve_tag_image_id "${PROBE_BUILD_TAG}" "${PROBE_TAG_INSPECT}")"
readonly FINAL_BUILD_DIGEST="$(require_sha256_digest_file "${FINAL_IIDFILE}")"
FINAL_IMAGE_ID="$(resolve_tag_image_id "${FINAL_BUILD_TAG}" "${FINAL_TAG_INSPECT}")"
```

- [ ] **Step 2: Lock the authentication order**

For both probe and final, assert this relative order:

1. `require_private_tag_absent`
2. Docker build
3. iidfile digest validation
4. tag-to-config-ID resolution
5. exact config-ID inspection
6. tag-ID recheck
7. manifest extraction

Require exact-ID equality and tag-recheck equality expressions for both images.

- [ ] **Step 3: Reject iidfile-based cleanup**

Replace the old `recover_image_id` assertion with:

```python
self.assertNotIn("recover_image_id", script)
self.assertNotIn("RECOVERED_IMAGE_ID", script)
```

Keep the exact private-tag cleanup assertions.

- [ ] **Step 4: Run focused tests and confirm red**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'build_script' -q
```

Expected: FAIL against the old iidfile-equals-image-ID implementation.

### Task 2: Implement Build-Result Digest And Config-ID Separation

**Files:**
- Modify: `scripts/build_legacy_doc_converter.sh`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Rename iidfile validation by meaning**

Rename `require_sha256_id_file` to `require_sha256_digest_file`. Require a
regular non-symlink file containing exactly one lowercase SHA-256 digest. Accept
Buildx's 71-byte no-final-newline output or the same digest with exactly one
trailing LF, and byte-compare the source so hidden characters fail closed.

- [ ] **Step 2: Add bounded tag-resolution metadata files**

Add private files for probe/final tag inspection and tag recheck:

```bash
readonly PROBE_TAG_INSPECT="${PRIVATE_DIR}/probe-tag.inspect"
readonly PROBE_TAG_RECHECK="${PRIVATE_DIR}/probe-tag-recheck"
readonly FINAL_TAG_INSPECT="${PRIVATE_DIR}/final-tag.inspect"
readonly FINAL_TAG_RECHECK="${PRIVATE_DIR}/final-tag-recheck"
```

- [ ] **Step 3: Add exact tag-to-ID resolution**

Add:

```bash
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
```

- [ ] **Step 4: Resolve and authenticate the probe image**

After the probe build:

```bash
readonly PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"
PROBE_IMAGE_ID="$(resolve_tag_image_id "${PROBE_BUILD_TAG}" "${PROBE_TAG_INSPECT}")"
```

Inspect `PROBE_IMAGE_ID` exactly as before, then capture the tag `.Id` into
`PROBE_TAG_RECHECK` and require it equals `PROBE_IMAGE_ID` before manifest copy.

- [ ] **Step 5: Resolve and authenticate the final image**

Apply the same build-digest and tag-resolution chain to final. Preserve all
exact-ID label checks, then write the final tag `.Id` into
`FINAL_TAG_RECHECK` and require equality before final manifest copy.

- [ ] **Step 6: Remove iidfile cleanup recovery**

Delete `recover_image_id`, `RECOVERED_IMAGE_ID`, and every attempt to remove a
local image using iidfile content. Cleanup uses a resolved config ID when one is
available and always uses the owned private tag fallback. `remove_probe_image`
requires the already resolved exact `PROBE_IMAGE_ID`.

- [ ] **Step 7: Run focused tests and Bash syntax**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'build_script' -q
/bin/bash -n scripts/build_legacy_doc_converter.sh
```

Expected: PASS.

### Task 3: Correct Documentation And Verify

**Files:**
- Modify: `containers/legacy-doc/README.md`
- Modify: `docs/superpowers/plans/2026-07-13-kunjin-phase-d1-1-b-isolated-legacy-doc.md`
- Verify: `containers/legacy-doc/Dockerfile`

- [ ] **Step 1: Correct the identity wording**

State that iidfile contains the private build-result digest selected by Buildx.
Buildx v0.34.1 prefers the exporter config digest and falls back to the exporter
image digest, so setup does not assign iidfile a fixed manifest/config meaning.
Setup may inspect its unique pre-absent random tag only to resolve and recheck an
exact config ID. Runtime conversion never invokes a tag.

- [ ] **Step 2: Run full verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src/kunjin/storage/repository.py tests/unit/test_repository.py tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
/bin/bash -n scripts/build_legacy_doc_converter.sh
git diff --check
shasum -a 256 containers/legacy-doc/Dockerfile
```

Expected: all commands pass and Dockerfile SHA-256 remains:

```text
1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b
```

- [ ] **Step 3: Independent review**

Require no P0/P1/P2 for digest/ID separation, setup tag use, retag recheck,
cleanup, output ID, fixed supply values, or runtime no-tag/no-network behavior.

### Task 4: Preserve Iidfile Validation Failure Status

**Files:**
- Modify: `tests/test_smoke.py`
- Modify: `scripts/build_legacy_doc_converter.sh`

- [ ] **Step 1: Reject readonly command-substitution assignments**

Require the script to use:

```bash
PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"
readonly PROBE_BUILD_DIGEST
FINAL_BUILD_DIGEST="$(require_sha256_digest_file "${FINAL_IIDFILE}")"
readonly FINAL_BUILD_DIGEST
```

Explicitly reject `readonly PROBE_BUILD_DIGEST="$(...)"` and the equivalent
final form because Bash's `readonly` builtin masks command-substitution failure.

- [ ] **Step 2: Add an invalid-iidfile behavior test**

Run a copied build script in a temporary repository with trusted temporary
Docker paths. A fake Docker CLI must return an empty result for private-tag
absence, then report build success while writing an invalid iidfile. Assert the
script exits nonzero at probe identity and the fake Docker call log contains no
`image inspect` invocation.

- [ ] **Step 3: Implement the split assignments**

Change only the probe/final build-digest assignments to assignment followed by
`readonly`. Do not alter digest validation, tag resolution, or exact-ID checks.

- [ ] **Step 4: Run verification and independent re-review**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'build_script' -q
/bin/bash -n scripts/build_legacy_doc_converter.sh
.venv/bin/ruff check tests/test_smoke.py
.venv/bin/ruff format --check tests/test_smoke.py
git diff --check
```

Expected: PASS and no remaining P0/P1/P2.

### Task 5: Accept The Real Buildx Iidfile Byte Format

**Files:**
- Modify: `tests/test_smoke.py`
- Modify: `scripts/build_legacy_doc_converter.sh`
- Modify: `docs/superpowers/specs/2026-07-14-kunjin-buildkit-manifest-config-identity-design.md`
- Modify: `docs/superpowers/plans/2026-07-14-kunjin-buildkit-manifest-config-identity.md`
- Modify: `containers/legacy-doc/README.md`
- Modify: `docs/superpowers/plans/2026-07-13-kunjin-phase-d1-1-b-isolated-legacy-doc.md`

- [ ] **Step 1: Reproduce the no-final-newline failure**

Use a fake Docker CLI that copies exact iidfile bytes. Write a valid 71-byte
lowercase SHA-256 digest without `\n` and assert the old script stops before
`docker image inspect`.

- [ ] **Step 2: Implement exact byte validation**

Accept only 71 bytes containing the digest or 72 bytes containing the same
digest plus one LF. Validate the lowercase SHA-256 syntax and compare the source
file byte for byte against the accepted representation.

- [ ] **Step 3: Lock strict failure behavior**

Keep the invalid-iidfile test and reject empty data, CRLF, leading or embedded
newlines, multiple trailing newlines, uppercase or malformed digests, incorrect
lengths, and NUL bytes. Valid 71-byte and 72-byte forms must proceed to exact
image inspection.

- [ ] **Step 4: Correct documentation and verify**

Remove claims that iidfile always contains a manifest digest. Run the focused
build-script tests, Bash syntax check, full pytest suite, Ruff, compileall, pip
check, Dockerfile checksum verification, and `git diff --check` before another
real Docker build.
