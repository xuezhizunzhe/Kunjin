# KunJin Docker Desktop Credential Helper Implementation Plan

> **For agentic workers:** Implement task-by-task with test-first verification and review each diff before continuing.

**Goal:** Let the pinned legacy-document converter build use Docker Desktop's configured credential helper while preserving a fixed trusted executable path and strict failure closure.

**Architecture:** Extend the build script's existing Docker Desktop trust boundary to the credential helper in the same application-bundle directory. Keep caller paths excluded, authenticate exact regular executable files, and leave the Dockerfile and conversion contract unchanged.

**Tech Stack:** Bash, Docker Desktop, Python `unittest`/pytest smoke tests.

---

### Task 1: Add The Failing Trust-Boundary Regression

**Files:**
- Modify: `tests/test_smoke.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Extend the trusted-path smoke test**

Add these assertions to
`test_build_script_has_trusted_path_and_exact_cleanup_fallbacks`:

```python
self.assertIn(
    'readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"',
    script,
)
self.assertIn('readonly PATH="${DOCKER_DESKTOP_BIN}:/usr/bin:/bin"', script)
self.assertIn("EXPECTED_DOCKER_CREDENTIAL_HELPER", script)
self.assertIn('! -L "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
self.assertIn('-x "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
self.assertIn('-f "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
self.assertNotIn('readonly PATH="/usr/local/bin:/usr/bin:/bin"', script)
```

Replace the obsolete assertion for `readonly PATH="/usr/bin:/bin"`.

- [ ] **Step 2: Run the focused test and verify the intended failure**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_smoke.py::SmokeTest::test_build_script_has_trusted_path_and_exact_cleanup_fallbacks \
  -q
```

Expected: FAIL because the script has no fixed Docker Desktop binary directory or credential-helper validation yet.

- [ ] **Step 3: Add isolated invalid-helper behavior coverage**

Create one test that copies the script and reviewed Dockerfile into a temporary
repository tree. Replace only the copied script's fixed Docker Desktop directory
and Docker link with temporary paths. Use a regular executable fake Docker CLI
whose body exits `99`, then exercise these helper states:

```python
cases = ("missing", "directory", "non_executable", "symlink")
```

For every case, invoke the copied script with the approved-format base digest
and package version and assert:

```python
self.assertEqual(result.returncode, 69)
self.assertIn(b"trusted Docker setup prerequisites are unavailable", result.stderr)
```

The fake Docker CLI's `99` exit must never be observed; this proves the helper
gate closes before Docker execution.

### Task 2: Implement The Minimal Trusted Helper Support

**Files:**
- Modify: `scripts/build_legacy_doc_converter.sh`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Define the fixed Docker Desktop path before exporting `PATH`**

Replace the current path declaration with:

```bash
readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"
readonly PATH="${DOCKER_DESKTOP_BIN}:/usr/bin:/bin"
export PATH
```

- [ ] **Step 2: Derive both trusted Docker executables from that directory**

Use:

```bash
readonly EXPECTED_DOCKER_CLI="${DOCKER_DESKTOP_BIN}/docker"
readonly EXPECTED_DOCKER_CREDENTIAL_HELPER="${DOCKER_DESKTOP_BIN}/docker-credential-desktop"
```

- [ ] **Step 3: Fail closed unless both application-bundle files are trusted executables**

Replace the setup prerequisite check with:

```bash
if [[ ! -x "${EXPECTED_DOCKER_CLI}" \
   || ! -f "${EXPECTED_DOCKER_CLI}" \
   || -L "${EXPECTED_DOCKER_CLI}" \
   || ! -x "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
   || ! -f "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
   || -L "${EXPECTED_DOCKER_CREDENTIAL_HELPER}" \
   || ! -f "${DOCKERFILE}" ]]; then
    printf 'trusted Docker setup prerequisites are unavailable\n' >&2
    exit 69
fi
```

- [ ] **Step 4: Run the focused test and Bash syntax validation**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_smoke.py::SmokeTest::test_build_script_has_trusted_path_and_exact_cleanup_fallbacks \
  -q
/bin/bash -n scripts/build_legacy_doc_converter.sh
```

Expected: both commands pass.

### Task 3: Verify The Repository And Real Build Handoff

**Files:**
- Verify: `scripts/build_legacy_doc_converter.sh`
- Verify: `tests/test_smoke.py`
- Verify: `containers/legacy-doc/Dockerfile`

- [ ] **Step 1: Run all legacy converter smoke tests**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'legacy or build_script' -q
```

Expected: PASS.

- [ ] **Step 2: Confirm the approved Dockerfile is unchanged**

```bash
shasum -a 256 containers/legacy-doc/Dockerfile
```

Expected:

```text
1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b
```

- [ ] **Step 3: Run repository verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

Expected: all commands pass.

- [ ] **Step 4: Repeat the real build without exposing raw output**

Run the absolute-path build wrapper already supplied to the owner. Expected:
`BUILD_OK`, followed by safe JSON where `fund converter-status` reports
`status=ready`, parser version `2-docker-libreoffice-v1`, and non-null provenance
checksum. Do not paste the image identifier or raw Docker log into chat.
