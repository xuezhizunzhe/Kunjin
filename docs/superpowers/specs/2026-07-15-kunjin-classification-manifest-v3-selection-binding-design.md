# KunJin Classification Manifest V3 Selection Binding Design

**Status:** approved under the owner's standing authorization to adopt the
recommended fail-closed option.

## 1. Purpose

Current classifications authenticate facts, freshness, parse results, and
parser provenance through Manifest V2, but they do not authenticate which
periodic candidates were selected by the latest document refresh. Task 7 adds
that binding without changing Policy V1 or historical V1/V2 bytes.

The result remains `research_only`. Selection authentication is not a purchase
recommendation, suitability decision, allocation, or amount.

## 2. Compatibility Boundary

Policy V1 is immutable. `CLASSIFICATION_FINANCIAL_CODES`, canonical policy JSON,
and its checksum remain byte-for-byte unchanged. The selection codes
`current_periodic_candidate_missing` and
`current_periodic_candidate_conflict` use the existing independent
`SELECTION_REASON_CODES` allowlist from `selection.py`; they are not added to
Policy V1 reason codes.

Historical Manifest V1 and V2 records continue to decode and authenticate with
their exact original functions and fingerprints. Their reconstructed
`ClassificationEvidence` values use:

- `document_refresh_run_id=None`;
- `selection_policy_checksum=None`;
- `selection_manifest_checksum=None`;
- `candidate_run_snapshot_checksum=None`;
- `selection_reason_codes=()`.

Only selection-bound evidence produces Manifest V3. New classifications saved
by the production service must use V3. A selectionless evidence object may exist
only for historical replay and direct deterministic engine tests; the store
must reject it for a new save.

## 3. Manifest V3 Contract

`ClassificationEvidence` adds:

```python
document_refresh_run_id: Optional[int]
selection_policy_checksum: Optional[str]
selection_manifest_checksum: Optional[str]
candidate_run_snapshot_checksum: Optional[str]
selection_reason_codes: Tuple[str, ...]
```

The refresh ID and three checksums are all-or-none. A present refresh ID is a
positive integer and every checksum is a lowercase SHA-256 digest. Reason codes
are sorted, unique, and drawn only from `SELECTION_REASON_CODES`.

Manifest V3 retains every V2 field and adds the five fields above. The reason
codes must equal the missing and conflicted periodic states in the authenticated
selection manifest; allowlist validation alone is insufficient.

`classification_input_manifest_v3()` rejects selectionless or partially bound
evidence. `classification_input_manifest()` chooses V3 only when the complete
selection binding is present. Selectionless evidence remains V2-compatible for
historical replay. Store save requires V3, while readback chooses the exact
historical manifest version recorded in the row.

## 4. Authenticated Selection Snapshot

The store adds two read-only operations backed by one private transactional
loader:

- current lookup first reads the fund's absolute latest refresh and returns a
  snapshot only when that exact refresh is completed as `success` or `partial`;
  a newer running, failed, or empty refresh never falls back to an older one;
- historical lookup loads the exact fund and refresh ID referenced by a stored
  Manifest V3, without applying the current parser-version gate.

Each operation loads an immutable snapshot of:

1. the exact referenced refresh, with current lookup requiring the fund's
   absolute latest refresh to be completed as `success` or `partial`;
2. its authenticated Schema V13 selection manifest;
3. every candidate run belonging to that refresh; and
4. successful parsed records bound to candidate runs; and
5. a canonical checksum over every terminal candidate run in that refresh.

The candidate-run snapshot payload is sorted by candidate-run ID and binds each
run's ID, fingerprint, fund code, document kind, canonical URL, publication
time, outcome, source-document ID, parse-run ID, safe public error code, failure
stage, failure reason, and creation time. It contains no body, local path,
converted content, or exception text.

The snapshot validates the complete run set. Periodic runs are governed by the
selection manifest; non-periodic runs are governed by the existing refresh and
candidate authentication rules:

- every selected fingerprint has exactly one terminal candidate run;
- a selected successful run binds its artifact, parse run, parse result, fact
  set, and parser provenance;
- a selected failed run retains its exact safe failure and contributes no
  parsed record;
- missing and conflicted states have no candidate run;
- unselected or extra periodic candidate runs fail authentication;
- non-periodic candidate runs may exist only for the same refresh and fund, and
  successful non-periodic runs require exact artifact, parse-result, and stored
  provenance bindings; current service assembly additionally requires active
  provenance;
- candidate kind, fingerprint, fund code, refresh ID, outcome, and selected
  state all agree; and
- historical lookup authenticates the stored provenance version without
  requiring it to remain active; current service assembly separately enforces
  active parser v4 provenance.

Schema V13 already stores immutable selection manifests and candidate runs. No
Schema V14 is required. A candidate run appended after refresh completion, or a
safe failure changed to another allowlisted failure, changes the candidate-run
snapshot checksum and causes V3 readback to fail.

## 5. Service Assembly

The service replaces the history-wide `select_current_documents()` assembly
path with the authenticated current selection snapshot. The store snapshot does
not decide which parser version is active. The service validates every successful
record against the active native v4 provenance or the exact current reviewed
legacy converter provenance in the same calculation.

- Selected successful records may contribute evidence.
- Selected failures contribute no old fallback record.
- Missing/conflicted periodic kinds contribute their exact selection reason
  code to V3.
- Legal and other selected non-periodic documents continue to use active parser
  provenance checks.
- Periodic successful records are taken directly from the authenticated
  selection states. Same-refresh non-periodic successful records are separately
  reduced with the existing current-document rules, including newest-per-kind
  handling and `prospectus_update` superseding `prospectus`, before the two sets
  are merged.
- A current result never loads a successful artifact from an older refresh to
  replace a missing, conflicted, or failed selected candidate.

The V3 binding is included in the evidence-change token used before save and
readback, so a changed refresh, selection checksum, reason set, or candidate-run
snapshot checksum invalidates the calculation.

## 6. Financial Semantics And Freshness

Selection reason codes are audit evidence, not direct financial score inputs.
They do not enter Policy V1 reason codes and do not automatically make every
fund partial merely because an unrelated periodic kind is missing.

Financial degradation remains fact-specific:

- if a missing, conflicted, or failed selected report prevents a required fact,
  existing exact missing-evidence codes keep or worsen the classification;
- selected successful report freshness remains based on authenticated report
  period and Policy V1 deadlines;
- retrieval time never extends validity;
- missing/conflicted/failed states have no fake document and therefore no fake
  freshness row; and
- selection binding can never improve evidence status, risk bucket, or
  portfolio role.

## 7. Store Readback And Tamper Rejection

`save_classification()` re-authenticates the exact referenced refresh snapshot
inside the same SQLite write transaction used for the classification insert. It
first requires that refresh to remain the fund's absolute latest refresh and to
remain completed as `success` or `partial`. It then compares the fund and
refresh, selection policy and manifest checksums, candidate-run snapshot
checksum, reason projection, evidence documents, parse results, and stored
provenance before insertion. A newer refresh in any running or completed state,
or any content change between service assembly and save, rolls back and leaves
no classification row. Historical V3 readback still authenticates its exact
referenced refresh without requiring it to remain current.

V3 readback authenticates:

- canonical V3 keys and fingerprint;
- positive refresh ID and all three checksums;
- the exact stored selection manifest and its policy checksum;
- exact reason-code equality with missing/conflicted states;
- exact periodic candidate-run set, including selected failure and no fallback,
  plus authenticated same-refresh non-periodic runs;
- evidence document, fact, parse-result, and parser-provenance bindings; and
- deterministic classification output.

For failed candidates, the candidate row and any bound failed parse run must
carry the same public error, failure stage, and failure reason.

V1/V2 readback does not require a current selection snapshot and never upgrades
old bytes to V3.

## 8. Verification

Tests must prove:

- V1/V2 canonical bytes and fingerprints remain unchanged;
- V1/V2 reconstruct `None, None, None, None, ()`;
- new saves reject selectionless or partially bound evidence;
- V3 canonical bytes change for every selection field;
- refresh, selection checksum, run-snapshot checksum, or reason tampering is
  rejected;
- reason codes must exactly match selection states;
- selected success uses only its exact v4 parse result;
- selected failure, missing, and conflict never fall back to an older report;
- extra, missing, duplicate, or unselected periodic runs are rejected while
  authenticated same-refresh non-periodic runs remain available;
- a later refresh does not break historical V3 readback, while current lookup
  never skips a newer running, failed, or empty refresh;
- report-period freshness is unchanged by retrieval time; and
- selection bindings do not improve conservative classification ordering.

Focused tests, complete engine/store/service/schema tests, the full suite, Ruff,
`git diff --check`, and two independent reviews must pass.

## 9. Non-Goals

- No Policy V2.
- No Policy V1 code or checksum change.
- No Schema V14.
- No parser v5 or new document-shape coverage.
- No Task 8 CLI, README, or Skill wording changes.
- No D2, D3, or Phase E implementation.
- No buy direction, amount, or 90 percent beginner-help claim.
