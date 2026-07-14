# KunJin Phase D1.1-B Isolated Legacy-DOC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely convert authenticated legacy OLE Word fund documents in a pinned, no-network Docker container while preserving immutable refresh, parser-provenance, result, run, fact, and classification history.

**Architecture:** Schema V12 adds append-only refresh/candidate gates and separates parser provenance, deterministic successful results, and retryable run events. A fixed Docker adapter accepts only a validated local `sha256:<image-id>`, runs LibreOffice with mandatory isolation flags, validates bounded HTML, and binds every published fact to the original OLE checksum and one authenticated parse result. Current classification may use only candidates from the latest completed refresh and never falls back after a current technical failure.

**Tech Stack:** Python 3.9+, SQLite migrations and immutable triggers, frozen dataclasses, `subprocess` without a shell, Docker Desktop, LibreOffice Writer no-GUI package, `html.parser`, existing KunJin risk engine, unittest/pytest, Ruff.

The pinned `libreoffice-writer-nogui` package reduces GUI dependencies. The
`docker-libreoffice-v1` conversion contract remains unchanged, including the
bounded HTML export, parser provenance, and fail-closed validation rules.

---

## Scope And File Map

Create:

- `src/kunjin/funds/risk/audit.py`: exact refresh, candidate, provenance, parse-result, and parse-run value objects plus canonical fingerprints.
- `src/kunjin/funds/risk/legacy_doc.py`: Docker command boundary, cleanup state machine, output validation, and legacy conversion result.
- `src/kunjin/funds/risk/reports.py`: shared exact document-kind and report-period validation.
- `tests/unit/test_risk_audit.py`: exact-model and canonical-fingerprint tests.
- `tests/unit/test_risk_legacy_doc.py`: Docker argv, isolation, cleanup, output, and privacy tests using a fake runner.
- `tests/unit/test_schema_v12.py`: fresh-schema, V11 migration, rollback, trigger, and compatibility tests.
- `tests/fixtures/funds/risk/legacy-converted-report.html`: synthetic bounded converted-HTML fixture with explicit identity, period, and table cells.
- `containers/legacy-doc/Dockerfile`: parameterized but digest-enforced LibreOffice image build.
- `containers/legacy-doc/README.md`: reviewed provisioning and image-ID capture procedure.
- `scripts/build_legacy_doc_converter.sh`: trusted setup-only image build and inspection script.
- `docs/audits/2026-07-13-kunjin-phase-d1-1-b-independent-review.md`: post-acceptance independent financial review.

Modify:

- `src/kunjin/storage/schema.py`: Schema V12 DDL and immutable triggers.
- `src/kunjin/storage/repository.py`: V12 migration ordering, honest legacy backfill, and D1 schema ownership validation.
- `src/kunjin/funds/risk/models.py`: parse-result bindings in current classification evidence.
- `src/kunjin/funds/risk/engine.py`: exact V1/V2 classification manifests.
- `src/kunjin/funds/risk/documents.py`: accept authenticated OLE as `legacy_ole_doc` instead of rejecting it before parsing.
- `src/kunjin/funds/risk/parsers.py`: provenance-aware parsing, converted-HTML validation, identity/kind/period checks, and native compatibility wrapper.
- `src/kunjin/funds/risk/store.py`: refresh/candidate/result/run persistence, current-gate queries, fact bindings, V1/V2 authenticated readback.
- `src/kunjin/funds/risk/service.py`: persist refresh start before discovery, terminal candidate outcomes, exact converter failures, and current no-fallback gates.
- `src/kunjin/funds/risk/__init__.py`: export the new exact public-internal types.
- `src/kunjin/cli.py`: lazy converter binding and safe `fund converter-status` metadata command.
- `src/kunjin/paths.py`: private converter workspace/config path helpers if needed by the adapter.
- `README.md`: setup, strict failure, and research-only limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: preserve converter diagnostics and never treat conversion as financial evidence.
- `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`: synchronize only after repository verification.
- `tests/unit/test_risk_documents.py`: OLE acceptance and MIME mismatch tests.
- `tests/unit/test_risk_models_policy.py`: exact evidence-binding model tests.
- `tests/unit/test_risk_engine.py`: V2 manifest/fingerprint tests.
- `tests/unit/test_risk_store.py`: storage, retry, migration-readback, and no-fallback tests.
- `tests/unit/test_risk_service.py`: refresh ordering, crash/failure, and converter propagation tests.
- `tests/unit/test_risk_parsers.py`: legacy converted-HTML and native parser compatibility tests.
- `tests/integration/test_cli.py`: converter status, sync JSON, manifest V1/V2, and privacy tests.
- `tests/test_smoke.py`: package, docs, Skill, Dockerfile, and no-host-fallback contract.
- `tests/unit/test_schema_v10.py`, `tests/unit/test_schema_v11.py`: update current-version expectations without weakening historical migration assertions.

Do not implement D1.1-C candidate pruning or new financial facts in this plan. Do not change Policy V1, Phase B, Phase C, D2, D3, Phase E, public purchase behavior, or the existing top-level document error-code vocabulary. Do not create a Git commit; the owner decides when to commit after local and live acceptance.

### Task 1: Add Exact Audit And Parser-Provenance Models

**Files:**

- Create: `src/kunjin/funds/risk/audit.py`
- Create: `tests/unit/test_risk_audit.py`
- Modify: `src/kunjin/funds/risk/__init__.py`

- [x] **Step 1: Write failing exact-model tests**

Create tests that import the following exact names and reject subclasses, mutable collections, hidden attributes, unknown codes, non-canonical JSON, invalid SHA-256 values, and non-UTC times:

```python
from kunjin.funds.risk.audit import (
    CandidateRunOutcome,
    ParseRunKind,
    ParseRunOutcome,
    ParserProvenance,
    RefreshOutcome,
    canonical_fact_set_fingerprint,
    candidate_fingerprint,
    legacy_parser_provenance,
    native_parser_provenance,
)


def test_native_provenance_is_fixed_and_canonical():
    value = native_parser_provenance()
    value.validate()
    assert value.parser_version == "2"
    assert value.converter_kind == "none"
    assert len(value.provenance_checksum) == 64
    assert value.canonical_json.encode("ascii").decode("ascii") == value.canonical_json


def test_retryable_failure_and_success_outcomes_are_distinct():
    assert ParseRunOutcome.FAILED.value == "failed"
    assert ParseRunOutcome.SUCCESS.value == "success"
    assert ParseRunKind.LEGACY_BACKFILL.value == "legacy_backfill"
    assert RefreshOutcome.PARTIAL.value == "partial"
    assert CandidateRunOutcome.FAILED.value == "failed"
```

Use an `OfficialDocumentCandidate` fixture to assert that `candidate_fingerprint(candidate)` changes when and only when a bound public candidate field changes. Use two ordered fact-fingerprint tuples to assert that `canonical_fact_set_fingerprint` is order-independent but duplicate-rejecting.

- [x] **Step 2: Run the focused tests and confirm the missing-module failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_audit -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'kunjin.funds.risk.audit'`.

- [x] **Step 3: Implement the exact models and canonical helpers**

Implement these enums and frozen dataclasses with exact-state validation matching existing risk-model patterns:

```python
class RefreshOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    EMPTY = "empty"


class CandidateRunOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class ParseRunKind(str, Enum):
    LIVE = "live"
    LEGACY_BACKFILL = "legacy_backfill"


class ParseRunOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class ParserProvenance:
    parser_version: str
    converter_kind: str
    canonical_json: str
    provenance_checksum: str
```

Implement `validate()` using exact type/state checks, the existing stable-code rules, canonical ASCII JSON re-encoding, and a recomputed SHA-256 equality check.

The canonical provenance payloads are exact dictionaries:

```python
{"contract_version":"native-v1","converter_kind":"none","parser_version":"2"}
```

and:

```python
{
    "adapter_contract_version": "docker-libreoffice-v1",
    "architecture": architecture,
    "converter_kind": "docker_libreoffice",
    "export_filter": "html_starwriter_skip_images_v1",
    "image_id": image_id,
    "libreoffice_version": libreoffice_version,
    "normalization_contract": "legacy_html_nfc_v1",
    "package_manifest_checksum": package_manifest_checksum,
    "parser_version": "2-docker-libreoffice-v1",
}
```

Expose the converted constructor with this exact signature:

```python
legacy_parser_provenance(
    *,
    image_id: str,
    architecture: str,
    libreoffice_version: str,
    package_manifest_checksum: str,
) -> ParserProvenance
```

It accepts only `sha256:` plus 64 lowercase hex for `image_id`, exact `linux/arm64` architecture, a bounded nonempty LibreOffice version, and a lowercase SHA-256 package-manifest checksum.

Implement `candidate_fingerprint()` over exact public candidate fields and `canonical_fact_set_fingerprint()` over the sorted unique fact fingerprints. JSON encoding must use `ensure_ascii=True`, `sort_keys=True`, and separators `(",", ":")`.

- [x] **Step 4: Verify the model boundary**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_audit -v
.venv/bin/ruff check src/kunjin/funds/risk/audit.py tests/unit/test_risk_audit.py
```

Expected: all tests pass and Ruff reports no errors.

### Task 2: Add Additive Schema V12 And Honest V11 Backfill

**Files:**

- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v12.py`
- Modify: `tests/unit/test_schema_v10.py`
- Modify: `tests/unit/test_schema_v11.py`

- [x] **Step 1: Write failing fresh-schema and migration tests**

Cover these exact test names and assertions:

- `test_fresh_database_has_v12_tables_and_fact_result_binding`: every V12 table, index, trigger, and `parse_result_id` column exists.
- `test_v11_success_backfills_provenance_result_run_and_fact_binding`: one successful V11 artifact produces one native provenance, result, run, and matching fact binding.
- `test_v11_failure_backfills_public_code_without_inventing_stage_or_reason`: error code is retained while stage/reason remain null.
- `test_v12_migration_preserves_artifact_fact_and_classification_ids`: primary keys and manifest bytes are unchanged.
- `test_v12_migration_rolls_back_on_malformed_legacy_fact_set`: no marker or partial V12 row survives.
- `test_refresh_result_run_and_provenance_rows_are_immutable`: update/delete statements abort.
- `test_parse_failures_can_repeat_but_success_result_is_unique`: multiple failures insert, one deterministic result inserts, conflicting second result aborts.
- `test_fact_cannot_bind_result_for_another_document`: cross-document binding aborts.
- `test_schema_tampering_is_rejected_on_reopen`: owned-object validation raises `sqlite3.DatabaseError`.

Build the V11 fixture through `SCHEMA_V1` through `SCHEMA_V11`, insert one parsed artifact with facts, one failed artifact, and one immutable classification, then run `Repository.migrate()`.

- [x] **Step 2: Run the V12 tests and confirm schema-version/table failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_schema_v12 -v
```

Expected: FAIL because `SCHEMA_VERSION` is 11 and the V12 tables do not exist.

- [x] **Step 3: Add Schema V12 DDL**

Set `SCHEMA_VERSION = 12`. Add DDL for these exact owned tables:

```text
fund_document_refresh_runs
  id, fund_code, started_at

fund_document_refresh_completions
  refresh_run_id, outcome, public_error_code, failure_stage,
  failure_reason, completed_at

fund_document_candidate_runs
  id, refresh_run_id, candidate_fingerprint, fund_code, document_kind,
  url, published_at, outcome, source_document_id, parse_run_id,
  public_error_code, failure_stage, failure_reason, created_at

fund_document_parser_provenance
  id, parser_version, converter_kind, canonical_json,
  provenance_checksum, created_at

fund_document_parse_results
  id, source_document_id, provenance_id, parser_input_sha256,
  fact_set_fingerprint, created_at

fund_document_parse_runs
  id, source_document_id, provenance_id, run_kind, outcome,
  parse_result_id, public_error_code, failure_stage, failure_reason,
  attempted_at
```

Add nullable `parse_result_id` to `fund_mandate_facts`. Enforce these cross-field rules with SQL checks and triggers:

- One refresh completion per refresh start.
- Candidate success requires source document and parse run, with null error fields.
- Candidate failure requires a public error; live failures require stage/reason.
- One provenance row per checksum and canonical content.
- One parse result per `(source_document_id, provenance_id)`.
- Live parse success requires a result and null error fields.
- Live parse failure requires public error/stage/reason and null result.
- Legacy-backfill failures allow null stage/reason but require the known V11 public error.
- Facts may bind only a result for the same source document and matching parser version.
- Update/delete triggers reject mutation after insertion.

Extend `_D1_TABLES` and `_D1_OBJECT_PREFIXES` so schema ownership validation includes every V12 object.

- [x] **Step 4: Implement deterministic V11 backfill inside the migration transaction**

Special-case version 12 in `Repository.migrate()` so the order is exact:

```text
BEGIN IMMEDIATE
  -> execute V12 table/column DDL with fact update trigger temporarily removed
  -> validate every V11 artifact and fact row through strict Python decoders
  -> ensure fixed native parser provenance
  -> create one deterministic result for each parsed artifact
  -> bind its facts and derive fact-set fingerprint
  -> append one legacy_backfill success run
  -> append one legacy_backfill failure run for each failed artifact without stage/reason
  -> recreate the fact immutability trigger
  -> insert migration marker 12
  -> validate exact final schema
COMMIT
```

Do not create refresh rows for V11 history. This intentionally forces a new V12 refresh before current classification while preserving V11 classification history.

- [x] **Step 5: Run schema tests and all historical schema regressions**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_schema_v10 \
  tests.unit.test_schema_v11 \
  tests.unit.test_schema_v12 -v
.venv/bin/ruff check src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v12.py
```

Expected: all schema tests pass; malformed migration tests leave no V12 marker or partial rows.

### Task 3: Add Store APIs For Refresh Gates, Results, Runs, And Fact Bindings

**Execution split:** Run Task 3A first for `models.py`, `engine.py`, and their tests. After 3A passes review, run Task 3B for `store.py` and `test_risk_store.py`. This reduces agent context size without changing the Task 3 acceptance criteria or allowing parallel edits.

**Files:**

- Modify: `src/kunjin/funds/risk/store.py`
- Modify: `src/kunjin/funds/risk/models.py`
- Modify: `src/kunjin/funds/risk/engine.py`
- Modify: `tests/unit/test_risk_store.py`
- Modify: `tests/unit/test_risk_models_policy.py`
- Modify: `tests/unit/test_risk_engine.py`

- [ ] **Step 1: Write failing store and manifest tests**

Add focused tests for the exact public-internal API:

```python
refresh_id = store.begin_document_refresh("519755", started_at)
store.complete_document_refresh(refresh_id, RefreshOutcome.PARTIAL, completed_at)

stored = store.publish_candidate_success(
    refresh_id=refresh_id,
    candidate=candidate,
    parsed=parsed,
    provenance=native_parser_provenance(),
    parser_input_sha256=parsed.artifact.sha256,
    attempted_at=attempted_at,
)

store.publish_candidate_failure(
    refresh_id=refresh_id,
    candidate=candidate,
    failure=failure,
    attempted_at=attempted_at,
    artifact=None,
    provenance=None,
)
```

Assert:

- Refresh start commits independently before any candidate row.
- A failed run followed by an identical-provenance success is accepted.
- A second different successful result for the same artifact/provenance fails.
- Candidate success binds the parse run, result, artifact, and every fact.
- Latest incomplete or failed refresh returns no current D1 documents.
- Latest partial refresh returns only same-refresh successful candidates.
- A current candidate failure never falls back to an earlier success.
- V11 classification evidence remains byte-authenticated after migration.
- V2 manifests include exact `manifest_version=2`, `parse_result_ids`, and `parser_provenance_checksums`.
- Mixed V1/V2, partial V2, unknown fields, or reordered non-canonical JSON is rejected.

- [ ] **Step 2: Run the focused tests and confirm missing-method/model failures**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_store \
  tests.unit.test_risk_models_policy \
  tests.unit.test_risk_engine -v
```

Expected: FAIL on missing refresh APIs and missing V2 evidence bindings.

- [ ] **Step 3: Extend current evidence with exact result/provenance bindings**

Add to `ClassificationEvidence`:

```python
parse_result_ids: Tuple[int, ...]
parser_provenance_checksums: Tuple[str, ...]
```

Validation requires sorted unique positive IDs and sorted unique lowercase SHA-256 checksums. Every official D1 fact ID must resolve through `fund_mandate_facts.parse_result_id` to one listed result; external tier-2 synthetic facts are not parse-result bound.

Split manifest generation into exact functions:

```python
classification_input_manifest_v1(evidence, policy, classified_at)
classification_input_manifest_v2(evidence, policy, classified_at)
```

The current `classification_input_manifest()` delegates to V2. V1 emits the original byte-compatible key set. V2 adds only:

```python
{
    "manifest_version": 2,
    "parse_result_ids": list(evidence.parse_result_ids),
    "parser_provenance_checksums": list(evidence.parser_provenance_checksums),
}
```

- [ ] **Step 4: Implement transactional store methods**

Implement exact methods for refresh start/completion, candidate failure, candidate success, parse failure, current-gated document loading, and authenticated result/provenance loading. `publish_candidate_success()` performs one `BEGIN IMMEDIATE` transaction:

```text
authenticate candidate and parsed artifact
  -> ensure immutable artifact by source identity only
  -> ensure exact provenance
  -> ensure/reuse deterministic successful result
  -> insert or authenticate complete fact set bound to result
  -> append successful parse run
  -> append successful candidate outcome bound to parse run
  -> commit
```

`publish_candidate_parse_failure()` persists the artifact, provenance, failed parse run, and failed candidate outcome atomically. Pre-parser failure persists only the candidate outcome. Store raw exception text nowhere.

Replace `parsed_document_history()` usage for current classification with `current_parsed_documents(fund_code, active_provenance_checksums)`. Keep historical read methods for authenticated history.

- [ ] **Step 5: Implement exact V1/V2 authenticated readback**

Detect the manifest by exact key set, not by optional-field probing:

```python
if set(manifest) == MANIFEST_V1_KEYS:
    version = 1
elif set(manifest) == MANIFEST_V2_KEYS and manifest["manifest_version"] == 2:
    version = 2
else:
    raise ValueError("classification input manifest has unexpected fields")
```

For V1, authenticate original bytes and derive migrated result/provenance bindings through immutable fact IDs. For V2, require the explicit arrays to match those bindings exactly.

- [ ] **Step 6: Run store/model/engine tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_store \
  tests.unit.test_risk_models_policy \
  tests.unit.test_risk_engine -v
.venv/bin/ruff check \
  src/kunjin/funds/risk/store.py \
  src/kunjin/funds/risk/models.py \
  src/kunjin/funds/risk/engine.py
```

Expected: all focused tests pass, including retry and no-fallback cases.

### Task 4: Persist Refresh State Before Discovery And Enforce Current Gates

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_service.py`

- [ ] **Step 1: Write failing refresh-order and crash tests**

Add recording fakes and these exact tests:

- `test_refresh_start_is_committed_before_discovery_is_called`
- `test_discovery_failure_completes_refresh_failed_without_reusing_history`
- `test_process_like_interruption_leaves_incomplete_refresh_that_blocks_classify`
- `test_landing_retrieval_and_container_failures_persist_candidate_outcomes`
- `test_partial_refresh_uses_only_same_refresh_successes`
- `test_latest_parse_failure_blocks_older_success_for_independent_classify`
- `test_storage_failure_leaves_refresh_incomplete_and_returns_existing_public_code`

For the ordering test, assert the recording store contains `begin_refresh` before the discovery fake records `discover`. For the independent-classify tests, construct a prior successful refresh, append a new failed or incomplete refresh, create a new service instance, and assert the old facts are absent.

- [ ] **Step 2: Run the service tests and confirm ordering/gate failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_service -v
```

Expected: FAIL because the current service discovers before persisting any refresh state and loads historical parsed documents directly.

- [ ] **Step 3: Rewrite `sync_documents()` around the persistent refresh state machine**

Use this exact control flow:

```text
refresh_id = begin_document_refresh(fund_code, attempted_at)
discover with the existing fund code, manager name, and announcement arguments
on discovery exception:
  map the exact SafeDocumentFailure
  complete refresh as failed with the same failure and canonical completion time
  notify the isolated observer
  raise the existing mapped RiskServiceError
otherwise:
  call _sync_candidate(refresh_id, candidate) for every discovered candidate
  derive success, partial, failed, or empty from the returned item statuses
  append the terminal refresh completion
  return DocumentSyncResult with fund_code, status, documents, and attempted_at
```

Do not put completion in a `finally` block: an unhandled process/storage interruption must leave the refresh incomplete so later classification fails closed.

- [ ] **Step 4: Route candidate outcomes through atomic store APIs**

Change `_sync_candidate()` to accept `refresh_id`. Pre-parser failures call `publish_candidate_failure`. Parser failures with an authenticated artifact call `publish_candidate_parse_failure`. Success calls `publish_candidate_success`. The public `DocumentSyncItem` fields and existing top-level error codes remain compatible.

If a persistence call fails, preserve `classification_storage_failed`, do not fabricate a completed refresh, and do not expose the database error.

- [ ] **Step 5: Enforce the current refresh gate in `_assemble_evidence()`**

Replace:

```python
select_current_documents(self._risk_store.parsed_document_history(fund_code))
```

with the current-gated store query using active provenance checksums. A failed, empty, or incomplete latest refresh raises an existing safe technical service error. A partial refresh supplies only its successful candidates; no older artifact is eligible.

Populate `ClassificationEvidence.parse_result_ids` and `parser_provenance_checksums` from the selected records before calling the engine.

- [ ] **Step 6: Verify service behavior**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_service -v
.venv/bin/ruff check src/kunjin/funds/risk/service.py tests/unit/test_risk_service.py
```

Expected: all service tests pass and every current-failure case refuses historical fallback.

### Task 5: Implement The Strict Docker Legacy-DOC Converter

**Files:**

- Create: `src/kunjin/funds/risk/legacy_doc.py`
- Create: `tests/unit/test_risk_legacy_doc.py`
- Modify: `src/kunjin/paths.py`
- Modify: `src/kunjin/funds/risk/__init__.py`

- [ ] **Step 1: Write failing converter contract tests with a fake command runner**

Define a fake runner that records exact argv and returns scripted exit states. Add these exact tests:

- `test_status_requires_exact_local_sha256_image_id_and_matching_inspect`
- `test_run_uses_every_required_isolation_flag_and_no_shell`
- `test_input_is_private_copy_with_original_sha256`
- `test_stdout_stderr_and_stdin_are_never_captured_or_inherited`
- `test_timeout_kills_client_then_force_removes_cid_and_verifies_absence`
- `test_name_fallback_cleans_container_when_cidfile_is_missing`
- `test_startup_reconciliation_removes_only_kunjin_labeled_stale_containers`
- `test_cleanup_failure_disables_converter_and_discards_output`
- `test_invalid_utf8_nul_replacement_script_external_or_oversized_output_fails`
- `test_valid_output_is_nfc_normalized_and_has_stable_checksum`
- `test_failure_repr_and_public_result_do_not_contain_path_argv_stderr_or_html`

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_legacy_doc -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'kunjin.funds.risk.legacy_doc'`.

- [ ] **Step 3: Implement exact converter types and runner protocol**

Use these interfaces:

```python
class DockerCommandRunner(Protocol):
    def run(self, argv: Tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        raise NotImplementedError


@dataclass(frozen=True)
class LegacyConversionResult:
    normalized_html: str
    parser_input_sha256: str
    provenance: ParserProvenance


class LegacyDocConverter(Protocol):
    def status(self) -> ConverterStatus:
        raise NotImplementedError

    def convert(self, artifact: RetrievedArtifact) -> LegacyConversionResult:
        raise NotImplementedError


class DockerLegacyDocConverter:
    DOCKER_PATH = Path("/usr/local/bin/docker")
```

The production runner uses `subprocess.Popen` with `shell=False`, `stdin/stdout/stderr=DEVNULL`, `close_fds=True`, `restore_signals=True`, and `start_new_session=True`. On timeout, terminate and then kill the Docker CLI process group before starting bounded container cleanup.

- [ ] **Step 4: Build the exact `docker run` argv**

The tuple must contain all of these fixed controls and no user-controlled token:

```text
/usr/local/bin/docker run
--pull=never
--name <validated-random-name>
--cidfile <private-cidfile>
--label com.kunjin.legacy-doc=1
--network=none
--read-only
--cap-drop=ALL
--security-opt=no-new-privileges
--ipc=none
--pids-limit=64
--memory=768m
--memory-swap=768m
--cpus=1.0
--init
--user=<validated-host-uid>:<validated-host-gid>
--log-driver=none
--mount type=bind,src=<private-input-dir>,dst=/input,readonly
--mount type=bind,src=<private-output-dir>,dst=/output
--tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=<validated-host-uid>,gid=<validated-host-gid>
--workdir=/tmp
<exact-sha256-image-id>
/usr/bin/libreoffice
--headless --nologo --nodefault --nolockcheck --norestore --safe-mode
-env:UserInstallation=file:///tmp/lo-profile
--convert-to html:HTML\ (StarWriter):SkipImages
--outdir /output
/input/input.doc
```

Validate the Docker symlink resolves exactly to Docker Desktop's CLI before use. `docker image inspect <image-id>` must return the same ID, `linux/arm64`, and the required KunJin labels. Never invoke a tag.

- [ ] **Step 5: Implement private file and cleanup validation**

Create a fresh mode-0700 directory under KunJin's private runtime temp root. Copy the authenticated artifact through file descriptors into `input/input.doc` mode 0600 while recomputing its SHA-256. Run the container as that same validated host UID/GID and assign the private `/tmp` tmpfs to the same identity so LibreOffice can enter its working directory without granting root privileges. Accept output only when `output/input.html` is a regular, non-symlink, single-link file owned by the current user, has bounded bytes, and remains the same inode/size before and after reading.

After every run, call bounded `docker rm --force` by CID, fall back to the private name, and require `docker inspect` to return not found. Delete the private directory only after handles are closed. Cleanup uncertainty maps to `legacy_converter_timeout` and disables future conversions for that process.

- [ ] **Step 6: Implement strict output validation and exact error mapping**

Request LibreOffice's Writer HTML `SkipImages` filter option because D1 fact extraction needs text and tables, not report artwork. Strictly decode UTF-8; reject empty output, NUL, U+FFFD, abnormal controls, scripts, event-handler attributes, embedded objects, iframes, external URLs/resources, converter banners, extra files, and output over the fixed byte/character limits. Normalize BOM, CRLF, and Unicode NFC only. The adapter remains fail-closed if LibreOffice ignores the option and emits companion resources.

LibreOffice may emit browser-recoverable crossing end tags for the exact inert
formatting set `a`, `b`, `font`, and `span`, and may omit `li` end tags at an
`ol`/`ul` boundary. Recover only those cases. Continue to reject mismatched or
unclosed root, body, division, table, row, cell, and all other structural tags.

Treat an exact normalized match to the authenticated official candidate title
within the first eight converted text views as bounded cover evidence when
LibreOffice exports the title as a paragraph. Any semantic title or heading in
that cover window must agree on document kind and report period. Do not scan
later body headings for document-kind promotion because report sections can
legitimately mention terms such as `基金合同`.

Use the same exact leading cover match as current-fund identity evidence. Ignore
explicit fund code and fund name fields only when their parsed section is
explicitly `目标基金` or `target fund`; all differently scoped identity conflicts
remain fatal. Bind the exact `SkipImages` export behavior in provenance as
`html_starwriter_skip_images_v1`.

Map failures exactly to the approved public/stage/reason triples. Never parse stderr or exception text to choose a reason.

- [ ] **Step 7: Verify the converter boundary**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_risk_legacy_doc -v
.venv/bin/ruff check src/kunjin/funds/risk/legacy_doc.py tests/unit/test_risk_legacy_doc.py
```

Expected: all converter tests pass without a Docker daemon because they use the fake runner.

### Task 6: Accept OLE Artifacts And Parse Validated Converted HTML

**Files:**

- Create: `src/kunjin/funds/risk/reports.py`
- Create: `tests/fixtures/funds/risk/legacy-converted-report.html`
- Modify: `src/kunjin/funds/risk/documents.py`
- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/unit/test_risk_documents.py`
- Modify: `tests/unit/test_risk_parsers.py`

- [ ] **Step 1: Write failing OLE acceptance and converted-parser tests**

Add these exact tests:

- `test_msword_ole_is_authenticated_and_persisted_as_retrieved_artifact`
- `test_ooxml_only_mime_with_ole_payload_is_rejected_as_mismatch`
- `test_ole_parser_requires_injected_converter`
- `test_converted_html_requires_exact_fund_code_or_exact_legal_name`
- `test_converted_html_rejects_conflicting_fund_identity`
- `test_converted_html_requires_document_kind_heading_match`
- `test_periodic_converted_html_requires_exact_report_period_match`
- `test_explicit_table_cells_preserve_label_unit_and_value_binding`
- `test_merged_or_flattened_ambiguous_cells_publish_no_financial_fact`
- `test_native_html_pdf_and_docx_keep_native_provenance_and_existing_facts`

Use synthetic OLE bytes only to test routing. Use the converted-HTML fixture for parser behavior; do not pretend synthetic OLE is a real Word document.

- [ ] **Step 2: Run focused tests and confirm OLE remains rejected**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_documents \
  tests.unit.test_risk_parsers -v
```

Expected: FAIL because `_validated_container_family()` still raises `legacy_ole_container_unsupported` and the parser has no converter boundary.

- [ ] **Step 3: Accept exact OLE/MSWord agreement at the document boundary**

Change `_families_match()` so `legacy_ole_doc` is accepted only for the generic Microsoft Word MIME family. Preserve `declared_detected_mismatch` for an OOXML-only declaration. Return `legacy_ole_doc` from `_validated_container_family()` and persist the original bytes with the existing content-addressed managed-artifact rules.

- [ ] **Step 4: Add shared report contract helpers**

In `reports.py`, implement exact deterministic helpers:

```python
report_period_end(title_or_heading: str) -> Optional[date]
document_kind_markers(kind: DocumentKind) -> Tuple[str, ...]
validate_converted_document_contract(
    blocks: Tuple[TextBlockView, ...],
    candidate: OfficialDocumentCandidate,
) -> None
```

Recognize only the already supported annual, semiannual, and Q1-Q4 title forms. Missing or conflicting period/kind evidence raises the existing parser stage reason; it never guesses from publication date alone.

- [ ] **Step 5: Add provenance-aware parsing while keeping the compatibility wrapper**

Add:

```python
@dataclass(frozen=True)
class ParsedArtifactResult:
    document: ParsedRiskDocument
    parser_input_sha256: str
    provenance: ParserProvenance


def parse_artifact(artifact: RetrievedArtifact) -> ParsedRiskDocument:
    return parse_artifact_with_provenance(artifact).document
```

Add the exact callable signature `parse_artifact_with_provenance(artifact: RetrievedArtifact, *, legacy_converter: Optional[LegacyDocConverter] = None) -> ParsedArtifactResult`. Its body authenticates and reads the artifact once, routes OLE through the converter and validated HTML blocks, routes native families through the existing block readers, extracts facts, validates `ParsedRiskDocument`, and returns the matching input checksum and provenance.

Native parser input checksum is the original artifact SHA-256 and provenance is fixed native v2. OLE requires the injected converter, validates its normalized HTML, then reuses bounded HTML block extraction plus exact identity/kind/period checks.

- [ ] **Step 6: Verify parser compatibility and legacy behavior**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_documents \
  tests.unit.test_risk_parsers -v
.venv/bin/ruff check \
  src/kunjin/funds/risk/documents.py \
  src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/reports.py
```

Expected: all tests pass; native parser results remain unchanged and OLE succeeds only through a validated injected conversion result.

### Task 7: Bind The Converter Into Service And Preserve Safe Public JSON

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `src/kunjin/cli.py`
- Modify: `tests/unit/test_risk_service.py`
- Modify: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing service and CLI tests**

Add these exact tests:

- `test_non_ole_sync_never_checks_converter_status`
- `test_ole_unavailable_timeout_resource_failed_and_output_invalid_propagate_exact_codes`
- `test_conversion_failure_persists_retryable_parse_and_candidate_runs`
- `test_conversion_success_publishes_result_bound_facts`
- `test_converter_status_json_exposes_only_safe_metadata`
- `test_sync_json_never_contains_docker_path_argv_container_name_stderr_or_html`
- `test_v1_history_and_v2_current_evidence_both_authenticate_through_cli`

- [ ] **Step 2: Run tests and confirm converter injection/status failures**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_service \
  tests.integration.test_cli -v
```

Expected: FAIL because production service does not inject a converter and the CLI has no converter-status command.

- [ ] **Step 3: Inject provenance-aware parser and lazy converter**

Add an optional `legacy_converter` constructor dependency to `FundRiskService`. The default parser callable invokes `parse_artifact_with_provenance(artifact, legacy_converter=self._legacy_converter)`. Existing tests that inject a custom parser remain supported by adapting exact `ParsedRiskDocument` results to native provenance.

Converter status is checked only after the parser identifies the OLE signature. PDF, HTML, and DOCX paths must not invoke Docker or require converter configuration.

- [ ] **Step 4: Preserve exact failure propagation and retry semantics**

For each `RiskDocumentParseError`, persist the safe failure directly through `publish_candidate_parse_failure`. The service must not infer from message text. A later healthy retry under the same provenance may reuse/create the deterministic success result and append a new run; the earlier failed run remains unchanged.

- [ ] **Step 5: Add safe `fund converter-status` JSON**

Add the parser:

```bash
kunjin --json fund converter-status
```

Return only:

```json
{
  "capability": "research_only",
  "status": "ready|unavailable|invalid",
  "reason_code": "legacy_converter_unavailable|null",
  "parser_version": "2-docker-libreoffice-v1|null",
  "provenance_checksum": "<sha256-or-null>"
}
```

Do not return image IDs, Docker paths, container names, host paths, labels, argv, package lists, or daemon errors.

Production converter enablement uses `KUNJIN_LEGACY_DOC_IMAGE_ID`, which must be an exact local `sha256:<64 lowercase hex>` ID. An unset or invalid value keeps the converter unavailable without affecting application startup or native document parsing.

- [ ] **Step 6: Verify service and CLI integration**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_service \
  tests.integration.test_cli -v
.venv/bin/ruff check src/kunjin/funds/risk/service.py src/kunjin/cli.py
```

Expected: all focused tests pass and public JSON contains only allowlisted safe metadata.

### Task 8: Add The Reviewed LibreOffice Image Build And Documentation Contract

**Files:**

- Create: `containers/legacy-doc/Dockerfile`
- Create: `containers/legacy-doc/README.md`
- Create: `scripts/build_legacy_doc_converter.sh`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing smoke tests for the build and no-fallback contract**

Add these exact smoke tests:

- `test_legacy_image_build_requires_digest_base_and_exact_package_version`
- `test_build_script_uses_iidfile_and_inspects_linux_arm64_result`
- `test_runtime_docs_require_pull_never_network_none_and_no_host_fallback`
- `test_skill_preserves_conversion_stage_and_reason_as_technical_only`

The tests must reject a mutable `FROM debian:bookworm-slim`, host `textutil`, host LibreOffice, or runtime image pull.

- [ ] **Step 2: Run smoke tests and confirm missing build assets**

Run:

```bash
.venv/bin/python -m unittest tests.test_smoke -v
```

Expected: FAIL because the container assets and D1.1-B wording do not exist.

- [ ] **Step 3: Add the parameterized digest-enforced Dockerfile**

Use this exact structure:

```dockerfile
# syntax=docker/dockerfile:1.7
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG LIBREOFFICE_VERSION
ARG BASE_IMAGE_DIGEST
LABEL com.kunjin.legacy-doc.contract="docker-libreoffice-v1"
LABEL com.kunjin.legacy-doc.base-image-digest="${BASE_IMAGE_DIGEST}"
RUN test -n "${LIBREOFFICE_VERSION}" \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}" ca-certificates fontconfig fonts-noto-cjk \
 && libreoffice --version \
 && dpkg-query -W -f='${Package}=${Version}\n' | LC_ALL=C sort > /opt/kunjin-package-manifest.txt \
 && rm -rf /var/lib/apt/lists/*
USER 65532:65532
WORKDIR /tmp
```

`BASE_IMAGE` must be supplied as `name@sha256:<digest>` by the build script. The image has no entrypoint that can accept document-controlled arguments.

- [ ] **Step 4: Add the trusted setup-only build script**

The script must use `set -euo pipefail`, reject non-digest base references and non-exact package versions, invoke Docker without `eval`, and build for `linux/arm64` with `--pull --no-cache --iidfile`. Treat each iidfile value as private build-result evidence selected by Buildx, not as an authoritative runtime identity or as a digest with one fixed manifest/config meaning. Buildx v0.34.1 prefers the exporter config digest and falls back to the exporter image digest; setup must not assume that selection rule applies to every Buildx version. Accept only an exact lowercase SHA-256 digest with zero or one trailing LF. For each build, setup may create a random private tag only after proving that tag did not exist before the build; it may inspect that tag only to resolve the local config image ID, authenticate the image by that exact ID, and recheck that the tag still resolves to the same ID. Inspect the exact config image ID, architecture, labels, LibreOffice version, and package-manifest checksum, then print safe JSON plus the exact environment export command.

The script is allowed to use network during explicit setup. Normal `kunjin sync fund-documents` remains `--pull=never` and no-network. The runtime converter never invokes a tag and uses only the authenticated exact config image ID.

- [ ] **Step 5: Document the two-phase supply review**

`containers/legacy-doc/README.md` must prescribe:

1. Resolve and display the official Debian base digest and exact LibreOffice package version.
2. Review those resolved values before building.
3. Build with the script and capture the authenticated exact config image ID; do not use the private setup tag at runtime.
4. Run `kunjin --json fund converter-status`.
5. Keep the converter unavailable when any inspection differs.

README and Skill must state that conversion success is not financial evidence, D1.1-C is still required for current report facts, and D2/D3/Phase E remain unimplemented.

- [ ] **Step 6: Run smoke, formatting, and repository/installed Skill identity checks**

Run:

```bash
.venv/bin/python -m unittest tests.test_smoke -v
.venv/bin/ruff check tests/test_smoke.py
cmp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
```

Expected: smoke tests pass and the two Skill files are byte-identical. Synchronize the installed Skill only after all repository tests pass; if sandbox permissions block it, provide the owner the exact `cp` command and do not claim synchronization.

### Task 9: Full Verification, Live OLE Acceptance, And Independent Financial Review

**Files:**

- Modify: `docs/audits/2026-07-13-kunjin-phase-d1-1-b-independent-review.md`
- Modify if required by verified behavior: `README.md`

- [ ] **Step 1: Run focused D1.1-B suites**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_risk_audit \
  tests.unit.test_schema_v12 \
  tests.unit.test_risk_legacy_doc \
  tests.unit.test_risk_documents \
  tests.unit.test_risk_parsers \
  tests.unit.test_risk_store \
  tests.unit.test_risk_service \
  tests.unit.test_risk_models_policy \
  tests.unit.test_risk_engine \
  tests.integration.test_cli \
  tests.test_smoke -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Run complete repository verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check \
  src/kunjin/funds/risk \
  src/kunjin/storage \
  src/kunjin/cli.py \
  tests/unit \
  tests/integration/test_cli.py \
  tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

Expected: tests, Ruff, format check, compileall, dependency check, and diff check all pass.

- [ ] **Step 3: Run privacy and forbidden-fallback scans**

Run exact searches over changed source, tests, docs, and generated JSON fixtures for:

```text
/Users/
managed_path
traceback
stderr
response body
textutil
host libreoffice
--network=host
--privileged
--cap-add
```

Expected: no public JSON, logs, Skill output, or audit contains a private path or raw diagnostic. Source-only fixed Docker and test sentinel references are reviewed manually and are not emitted.

- [ ] **Step 4: Provision and verify the converter image explicitly**

Because provisioning changes Docker state and may use network, run it only with the owner's explicit authorization. Resolve the public base digest and package version, review them, run `scripts/build_legacy_doc_converter.sh`, export the returned exact image ID, and verify:

```bash
.venv/bin/kunjin --json fund converter-status
```

Expected: `status=ready`, a non-null parser version and provenance checksum, and no image ID or Docker detail in JSON.

- [ ] **Step 5: Run isolated real OLE acceptance**

Use new empty `KUNJIN_DATA_DIR`, `KUNJIN_STATE_DIR`, and `PYTHONPYCACHEPREFIX` directories. Predeclare the same four public fund codes used by v6. For every code run profile, holdings, documents, classify, evidence, and history commands, preserving stdout, stderr, and exit status in a timestamped `/private/tmp/kunjin-d1-live-results-v7-*` directory.

Acceptance requires at least one real periodic OLE document to complete:

```text
official discovery
-> landing and attachment authentication
-> original OLE checksum persistence
-> no-network Docker conversion
-> converted HTML validation
-> exact identity/kind/period validation
-> parse result and fact persistence
-> V2 classification manifest
-> authenticated evidence readback
```

Also force unavailable, timeout, and malformed-output cases with test-only injected runners; prove no container or transient output remains. Report exact success/failure distribution and never imply 100% coverage.

- [ ] **Step 6: Perform the independent financial review**

Create the audit with these required conclusions:

- D1.1-B engineering gate: PASS or FAIL based only on verified acceptance.
- Full D1 status: incomplete until D1.1-C current report extraction passes.
- Beginner usefulness score with explicit rubric and no credit for D2, D3, or Phase E.
- Whether the implementation materially improves real official evidence rather than only diagnostics.
- Exact remaining gaps for broad index, sector/theme, active equity, pure bond, and fixed-income-plus workflows.
- Explicit statement that no recommendation, direction, allocation target, or purchase amount is authorized.

- [ ] **Step 7: Present verification evidence and stop before D1.1-C**

Report changed files, exact test counts, live result directory, real OLE success/failure distribution, converter isolation evidence, privacy scan outcome, and independent score. Do not begin D1.1-C until the owner reviews and confirms the D1.1-B result.

---

## Execution Order And Agent Boundaries

Execute Tasks 1 through 9 strictly in order. Use one fresh implementation subagent per task, then perform two reviews before starting the next task:

1. Spec-compliance review against this plan and the approved design.
2. Code-quality/security review with focused tests rerun by the main agent.

Tasks must not run in parallel because Schema V12, `FundRiskStore`, `FundRiskService`, and their tests overlap. Subagents must not commit, reset, revert, or clean unrelated worktree changes. The main agent owns final integration, installed-Skill synchronization, live acceptance, and the independent financial review.
