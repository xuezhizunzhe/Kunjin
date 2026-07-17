# KunJin Classification Manifest V3 Selection Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each implementation task requires independent specification and quality reviews before the next task.

**Goal:** Authenticate the exact current document selection and candidate-run snapshot in Classification Manifest V3 while preserving Policy V1 and historical Manifest V1/V2 bytes.

**Architecture:** Extend `ClassificationEvidence` with optional selection bindings and emit V3 only when all bindings are present. Add a store-level authenticated current-selection snapshot, make new saves require V3, and replace service history-wide report selection with the snapshot while retaining parser v4, report-period freshness, and strict no-fallback behavior.

**Tech Stack:** Python 3, immutable dataclasses, canonical JSON/SHA-256, SQLite Schema V13, pytest/unittest, Ruff.

---

## File Map

- `src/kunjin/funds/risk/engine.py`: evidence fields, V3 canonical manifest,
  version-aware fingerprints, and conservative invariants.
- `src/kunjin/funds/risk/store.py`: authenticated selection snapshot, V3 save,
  V1/V2/V3 readback, and candidate-run tamper rejection.
- `src/kunjin/funds/risk/service.py`: selection-bound current evidence assembly
  and evidence-change token.
- `src/kunjin/funds/risk/selection.py`: existing selection reason allowlist;
  modify only if an exported validation helper removes real duplication.
- `tests/unit/test_risk_engine.py`: V1/V2 bytes and V3 field contract.
- `tests/unit/test_risk_store.py`: selection snapshot, persistence, and tamper
  authentication.
- `tests/unit/test_risk_service.py`: selected success/failure/missing/conflict,
  no fallback, freshness, and parser v4 behavior.
- `tests/unit/test_schema_v13.py`: unchanged Schema V13 regression.

## Task 1: Add Version-Aware Manifest V3 Evidence

**Files:**

- Modify: `src/kunjin/funds/risk/engine.py`
- Modify: `tests/unit/test_risk_engine.py`

- [ ] **Step 1: Write failing V3 evidence tests**

Extend test evidence builders with exact defaults:

```python
document_refresh_run_id=None,
selection_policy_checksum=None,
selection_manifest_checksum=None,
candidate_run_snapshot_checksum=None,
selection_reason_codes=(),
```

Test all-or-none validation, positive refresh ID, lowercase SHA-256 checksums,
sorted unique reason codes, and exact membership in `SELECTION_REASON_CODES`.
Prove V1 and V2 canonical bytes are unchanged.

- [ ] **Step 2: Run the red tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py -k manifest
```

Expected: failures show missing evidence fields and absent V3 helper.

- [ ] **Step 3: Extend ClassificationEvidence without changing Policy V1**

Add the five fields after parser provenance bindings:

```python
document_refresh_run_id: Optional[int]
selection_policy_checksum: Optional[str]
selection_manifest_checksum: Optional[str]
candidate_run_snapshot_checksum: Optional[str]
selection_reason_codes: Tuple[str, ...]
```

Validate the refresh ID and three checksums as all-or-none. When absent, reason
codes must be empty.
When present, validate exact selection reason codes with
`SELECTION_REASON_CODES`. Do not modify `policy.py`, Policy V1 JSON, financial
codes, or checksum.

- [ ] **Step 4: Add explicit V3 construction and version-aware defaulting**

```python
def classification_input_manifest_v3(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> Dict[str, object]:
    payload = classification_input_manifest_v2(evidence, policy, classified_at)
    payload.update(
        {
            "manifest_version": 3,
            "document_refresh_run_id": evidence.document_refresh_run_id,
            "selection_policy_checksum": evidence.selection_policy_checksum,
            "selection_manifest_checksum": evidence.selection_manifest_checksum,
            "candidate_run_snapshot_checksum": (
                evidence.candidate_run_snapshot_checksum
            ),
            "selection_reason_codes": list(evidence.selection_reason_codes),
        }
    )
    return payload
```

`classification_input_manifest_v3()` raises for missing or partial selection
binding. `classification_input_manifest()` returns V3 only for fully bound
evidence; selectionless evidence returns V2 for historical replay and
deterministic tests.
Every individual V3 field must alter the fingerprint. Selection reason codes are
audit bindings only and must not be added to classification missing/reason codes.

- [ ] **Step 5: Verify and commit Task 1**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py
.venv/bin/ruff check src/kunjin/funds/risk/engine.py tests/unit/test_risk_engine.py
git diff --check
git add src/kunjin/funds/risk/engine.py tests/unit/test_risk_engine.py
git commit -m "feat: add classification manifest v3"
```

## Task 2: Authenticate The Current Selection Snapshot In Store

**Files:**

- Modify: `src/kunjin/funds/risk/store.py`
- Modify: `tests/unit/test_risk_store.py`
- Verify: `tests/unit/test_schema_v13.py`

- [ ] **Step 1: Write failing snapshot and V3 persistence tests**

Cover:

- absolute latest refresh running/failed/empty returns no current snapshot and
  never falls back to an older success;
- exact selection manifest and policy checksum;
- one terminal run for each selected fingerprint;
- zero runs for missing/conflicted periodic states;
- selected success with exact artifact/parse-result/provenance binding;
- selected failure with exact safe failure and no parsed record;
- failed candidate and any bound failed parse run must have identical public
  error, stage, and reason;
- extra, duplicate, unselected, missing, or kind-mismatched periodic runs
  rejected, while valid same-refresh non-periodic runs remain available;
- V3 save rejects selectionless evidence;
- V1/V2 `classification_history`, `classification_evidence`, and
  `current_classification` readback reconstruct `None, None, None, None, ()`,
  preserve canonical bytes and stored fingerprints, and never call V3;
- V3 tampering of every field or reason-state mismatch is rejected;
- run-snapshot checksum changes for an appended run or changed safe failure; and
- historical V3-A remains readable after a later V3-B refresh exists.

Also simulate a run appended or safe failure changed between evidence assembly
and save. `save_classification()` must fail and leave no new classification row.
After assembling refresh A, insert a newer refresh B in each state: running,
failed, empty, partial, and success. Saving A must fail with no classification
row in every case; only A remaining the absolute latest completed success or
partial may be saved.

- [ ] **Step 2: Run store red tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_store.py \
  -k 'selection or manifest or classification'
```

Expected: failures show no authenticated snapshot and V2-only decoding.

- [ ] **Step 3: Add immutable snapshot records**

Add exact frozen records equivalent to:

```python
@dataclass(frozen=True)
class StoredSelectionCandidateRun:
    candidate_fingerprint: str
    document_kind: DocumentKind
    outcome: ParseRunOutcome
    parsed_record: Optional[ParsedDocumentRecord]
    failure: Optional[SafeDocumentFailure]

@dataclass(frozen=True)
class CurrentDocumentSelectionSnapshot:
    selection: StoredDocumentSelectionManifest
    candidate_runs: Tuple[StoredSelectionCandidateRun, ...]
    selected_periodic_records: Tuple[ParsedDocumentRecord, ...]
    nonperiodic_successful_records: Tuple[ParsedDocumentRecord, ...]
    candidate_run_snapshot_checksum: str
    selection_reason_codes: Tuple[str, ...]
```

Validation requires exact records, canonical ordering, exact reason-code equality
with missing/conflicted states, and the complete run-set rules from the design.

- [ ] **Step 4: Implement current and historical snapshot queries**

Add:

```python
def current_document_selection_snapshot(
    self,
    fund_code: str,
) -> Optional[CurrentDocumentSelectionSnapshot]:
    ...

def document_selection_snapshot_for_refresh(
    self,
    fund_code: str,
    refresh_run_id: int,
) -> CurrentDocumentSelectionSnapshot:
    ...
```

The current method first reads the absolute latest refresh and returns a snapshot
only if that same refresh completed as success or partial. The historical method
loads the exact referenced refresh regardless of later refreshes or current
parser version. Authenticate the selection manifest and load all candidate runs
in the same SQLite read transaction. Periodic runs must match the exact selected,
missing, and conflicted states. Non-periodic runs remain allowed only under the
same refresh/fund and existing candidate authentication. Bind every successful
record through `_load_parsed_record` and reject any mismatch. Compute the exact
canonical candidate-run snapshot checksum. Active parser checks are a service
responsibility, not a historical store-read requirement.

- [ ] **Step 5: Decode and authenticate V1/V2/V3 separately**

Add exact `_MANIFEST_V3_KEYS` and make `_decode_manifest_envelope()` accept only
V1, exact V2, or exact V3. `_evidence_from_manifest()` fills historical selection
defaults for V1/V2 and validates V3 fields. V3 readback reloads the referenced
selection snapshot and requires refresh ID, three checksums, reason codes, and
successful parse-result bindings to match. Historical V1/V2 deterministic
replay continues to use V1/V2 input fingerprints; it never calls the V3 helper.

`save_classification()` must reject a new non-V3 evidence object before insert.
Inside its existing SQLite write transaction, it must call the private
current-snapshot loader first, require its absolute latest refresh ID to equal
the evidence refresh ID and its completion to be success or partial, then call
the exact-refresh loader and compare the fund, refresh ID, three checksums,
reason projection, evidence documents, parse results, and stored provenance
before `INSERT`. Any newer refresh or binding mismatch rolls back atomically.
Historical `classification_history()` remains readable and version-aware.

- [ ] **Step 6: Verify and commit Task 2**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_store.py \
  tests/unit/test_schema_v13.py
.venv/bin/ruff check src/kunjin/funds/risk/store.py tests/unit/test_risk_store.py
git diff --check
git add src/kunjin/funds/risk/store.py tests/unit/test_risk_store.py
git commit -m "feat: authenticate document selection snapshots"
```

## Task 3: Assemble Only Selection-Bound Current Evidence

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_service.py`

- [ ] **Step 1: Write failing service tests**

Test these separate cases:

```text
selected success -> exact current v4 parsed record is used
selected failure -> no older successful report is used
missing state -> exact missing selection code is bound, no fake freshness
conflicted state -> exact conflict selection code is bound, no candidate attempted
unrelated missing periodic kind -> no unconditional global downgrade
changed refresh/checksum/reason/run set -> evidence_changed or storage failure
historical parser v2/v3 selected success -> unavailable, never current
later refresh -> old V3 history stays readable, current uses absolute latest only
mixed active and historical parser records -> entire current assembly is rejected
two nonperiodic records of one kind -> existing newest-per-kind rule still applies
prospectus plus prospectus_update -> update continues to supersede prospectus
```

Also prove retrieval time does not change report validity and selection binding
cannot improve conservative classification rank.

- [ ] **Step 2: Run service red tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_service.py \
  -k 'selection or fallback or freshness or evidence_changed'
```

- [ ] **Step 3: Replace history-wide assembly with the snapshot**

In `_assemble_evidence()`:

```python
snapshot = self._risk_store.current_document_selection_snapshot(fund_code)
if snapshot is None:
    raise RiskServiceError("official_document_unavailable")
all_records = snapshot.selected_periodic_records + snapshot.nonperiodic_successful_records
if not all_records:
    raise RiskServiceError("official_document_unavailable")
self._require_active_provenance(all_records)
periodic_records = snapshot.selected_periodic_records
nonperiodic_records = select_current_documents(snapshot.nonperiodic_successful_records)
candidate_records = periodic_records + nonperiodic_records
```

Do not re-select periodic records. Apply the existing current-document helper
only to same-refresh non-periodic successes, preserving newest-per-kind and
prospectus supersession rules. `_require_active_provenance()` must inspect every
successful record and raise on any historical, unknown, mixed, unavailable, or
mismatched legacy provenance; it must not silently filter records. Populate
the five selection fields from `snapshot.selection`,
`snapshot.candidate_run_snapshot_checksum`, and
`snapshot.selection_reason_codes`. Existing report fact selection and
`evidence_freshness()` continue to operate only on selected successful records.

- [ ] **Step 4: Preserve fact-specific degradation**

Do not union selection reason codes into Policy V1 reason or missing-evidence
sets. Missing/conflicted/failed reports degrade only through the existing facts
and freshness that are actually required for the classified product family.
Missing states create no synthetic document or freshness row.

- [ ] **Step 5: Verify and commit Task 3**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py \
  tests/unit/test_risk_store.py tests/unit/test_risk_service.py \
  tests/unit/test_schema_v13.py
.venv/bin/ruff check src/kunjin/funds/risk/engine.py \
  src/kunjin/funds/risk/store.py src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_engine.py tests/unit/test_risk_store.py \
  tests/unit/test_risk_service.py
git diff --check
git add src/kunjin/funds/risk/service.py tests/unit/test_risk_service.py
git commit -m "feat: bind classifications to current selections"
```

## Task 4: Full Regression And Live Acceptance

**Files:**

- Modify only if an independently verified in-scope defect requires correction.
- Write acceptance artifacts under `/private/tmp`; do not commit public downloads.

- [ ] **Step 1: Run complete local verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
git status --short
```

- [ ] **Step 2: Run isolated Manifest V3 classification acceptance**

Use fresh temporary data/state directories and the reviewed parser v4 converter
image. Synchronize `519718` documents, classify it, and inspect evidence/history.
Expected:

- new classification input manifest version is 3;
- refresh ID, selection checksums, and candidate-run snapshot checksum authenticate;
- reason codes equal the selection manifest states;
- six v4 parse results remain bound;
- `519718` remains stale/unclassified/not_eligible with its exact missing facts;
- no old-report fallback, direction, or amount appears.

- [ ] **Step 3: Run two independent read-only reviews**

One reviewer checks V1/V2 byte compatibility, V3 canonical authentication, and
candidate-run completeness. A second reviewer checks financial monotonicity,
freshness, no fallback, beginner-facing false confidence, and Policy V1 checksum
preservation. No P0/P1/P2 finding may remain.

- [ ] **Step 4: Record the objective financial assessment**

State that selection authentication improves evidence integrity, not real fact
coverage. Report any live result exactly and retain the current 90-percent,
D2/D3, Phase E, industry-zero, and holdings limitations.

- [ ] **Step 5: Commit only required acceptance corrections**

Do not create an empty commit. Add a focused regression test before any fix and
stage only verified in-scope files.
