# KunJin BuildKit Single-Image Identity Implementation Plan

> **For agentic workers:** Implement task-by-task with a fresh subagent, test-first verification, and an independent final review.

**Goal:** Restore exact local image-ID authentication under Docker Desktop 29 by disabling Docker's default outer provenance index and adding safe setup-stage diagnostics.

**Architecture:** Keep the existing two-stage pinned build and all post-build authentication. Add `--provenance=false` to both build invocations, then emit only fixed allowlisted stage labels into the private setup log.

**Tech Stack:** Bash, Docker Buildx, Python unittest/pytest smoke tests.

---

### Task 1: Add Failing Docker 29 Contract Tests

**Files:**
- Modify: `tests/test_smoke.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Require single-image build output**

In `test_build_script_uses_iidfile_and_inspects_linux_arm64_result`, add:

```python
self.assertEqual(script.count("--provenance=false"), 2)
```

- [ ] **Step 2: Require the exact safe stage sequence**

Add a new test:

```python
def test_build_script_emits_only_allowlisted_safe_setup_stages(self) -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/build_legacy_doc_converter.sh").read_text(
        encoding="utf-8"
    )
    stage_calls = [
        line.removeprefix("report_stage ")
        for line in script.splitlines()
        if line.startswith("report_stage ")
    ]
    self.assertEqual(
        stage_calls,
        [
            "probe_build",
            "probe_identity",
            "probe_manifest",
            "final_build",
            "final_identity",
            "final_manifest",
            "ready",
        ],
    )
    self.assertIn(
        "probe_build|probe_identity|probe_manifest|final_build|final_identity|final_manifest|ready",
        script,
    )
    self.assertIn("invalid setup stage", script)
```

- [ ] **Step 3: Run the two focused tests and confirm red**

```bash
.venv/bin/python -m pytest \
  tests/test_smoke.py::SmokeTest::test_build_script_uses_iidfile_and_inspects_linux_arm64_result \
  tests/test_smoke.py::SmokeTest::test_build_script_emits_only_allowlisted_safe_setup_stages \
  -q
```

Expected: FAIL because the current script has neither `--provenance=false` nor
the safe stage sequence.

### Task 2: Implement Single-Image Output And Safe Stages

**Files:**
- Modify: `scripts/build_legacy_doc_converter.sh`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Add the fixed stage reporter**

Before the explicit setup build sequence, add:

```bash
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
```

- [ ] **Step 2: Disable Docker's outer provenance index in both builds**

Add the exact argument after `--no-cache` in both Docker build invocations:

```bash
--provenance=false \
```

- [ ] **Step 3: Emit the exact stage sequence**

Call `report_stage` immediately before each boundary:

```bash
report_stage probe_build
report_stage probe_identity
report_stage probe_manifest
report_stage final_build
report_stage final_identity
report_stage final_manifest
report_stage ready
```

The calls must occur in this order. `ready` is emitted only after
`FINAL_IMAGE_VERIFIED=1`.

- [ ] **Step 4: Run focused tests and Bash syntax**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'build_script' -q
/bin/bash -n scripts/build_legacy_doc_converter.sh
```

Expected: PASS.

### Task 3: Document And Verify The Contract

**Files:**
- Modify: `containers/legacy-doc/README.md`
- Verify: `containers/legacy-doc/Dockerfile`
- Verify: `scripts/build_legacy_doc_converter.sh`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Document the BuildKit compatibility boundary**

State that setup uses `--provenance=false` only to keep `--iidfile` bound to one
local image identity. Explicitly state that KunJin's package manifest, labels,
base digest, LibreOffice version, Dockerfile checksum, and parser provenance are
unchanged.

- [ ] **Step 2: Run focused and full verification**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'legacy or build_script' -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
/bin/bash -n scripts/build_legacy_doc_converter.sh
git diff --check
shasum -a 256 containers/legacy-doc/Dockerfile
```

Expected: all commands pass and the Dockerfile SHA-256 remains:

```text
1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b
```

- [ ] **Step 3: Independent review**

Require no remaining P0/P1/P2 for identity semantics, stage-label privacy,
cleanup behavior, fixed supply values, and runtime no-network isolation before
requesting another real build.

### Task 4: Close The Independent Identity-Test Gap

**Files:**
- Modify: `tests/test_smoke.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: Assert each build block independently**

Split the script at the two exact `"${DOCKER_BIN}" build` markers and terminate
the blocks at `report_stage probe_identity` and `report_stage final_identity`.
Require each block to contain exactly one `--provenance=false`, one `--iidfile`,
one `--tag`, and one `--target`, with the probe and final variable names bound to
their corresponding block.

- [ ] **Step 2: Lock the exact identity and ordering expressions**

Assert the script retains:

```bash
require_private_tag_absent "${PROBE_BUILD_TAG}"
require_private_tag_absent "${FINAL_BUILD_TAG}"
[[ "${probe_id}" == "${PROBE_IMAGE_ID}" && "${probe_os}/${probe_arch}" == "${TARGET_PLATFORM}" ]]
[[ "${final_id}" == "${FINAL_IMAGE_ID}" ]]
[[ "${final_tag_id}" == "${FINAL_IMAGE_ID}" ]]
```

Also assert `FINAL_IMAGE_VERIFIED=1` occurs before `report_stage ready`.

- [ ] **Step 3: Run focused tests and independent re-review**

```bash
.venv/bin/python -m pytest tests/test_smoke.py -k 'build_script' -q
.venv/bin/ruff check tests/test_smoke.py
.venv/bin/ruff format --check tests/test_smoke.py
git diff --check -- tests/test_smoke.py
```

Expected: PASS, followed by an independent review with no remaining P0/P1/P2.
