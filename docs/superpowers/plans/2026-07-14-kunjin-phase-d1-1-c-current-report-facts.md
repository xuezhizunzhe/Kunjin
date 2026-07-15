# KunJin Phase D1.1-C Current Report Facts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synchronize only the newest required official periodic reports, extract Policy V1 current-risk facts without inference, and authenticate the selection through Schema V13 and classification manifest V3.

**Architecture:** Add a pure candidate-selection module that creates an immutable canonical selection manifest before download. Persist that manifest in additive Schema V13, let document synchronization attempt only the selected periodic candidates, extract current facts through a focused report-facts module, and bind the selected refresh and checksum into classification manifest V3 while retaining exact V1/V2 history decoders.

**Tech Stack:** Python 3.11, frozen dataclasses, `Decimal`, SQLite migrations and immutable triggers, existing KunJin risk service/store/parser APIs, pytest, Ruff, Docker LibreOffice live acceptance.

---

## File Map

**New files**

- `src/kunjin/funds/risk/selection.py`: pure latest-per-kind selection, canonical manifest, stable selection codes, and checksum validation.
- `src/kunjin/funds/risk/report_facts.py`: pure current-report table/text models and explicit observation extraction.
- `tests/unit/test_risk_selection.py`: selection, tie, missing, checksum, and immutable-record tests.
- `tests/unit/test_risk_report_facts.py`: common and fixed-income report-shape tests.
- `tests/unit/test_schema_v13.py`: additive migration, immutability, compatibility, and tamper tests.
- `docs/audits/2026-07-14-kunjin-phase-d1-1-c-live-acceptance.md`: final representative acceptance evidence.
- `docs/audits/2026-07-14-kunjin-phase-d1-1-c-independent-review.md`: final current-holdings coverage and objective financial review.

**Modified files**

- `src/kunjin/storage/schema.py`: set schema version 13 and define the selection-manifest table and triggers.
- `src/kunjin/storage/repository.py`: register and apply Schema V13.
- `src/kunjin/funds/risk/audit.py`: expose exact candidate evidence payload helpers used by selection manifests.
- `src/kunjin/funds/risk/store.py`: persist and authenticate selection manifests; decode classification manifests V1/V2/V3.
- `src/kunjin/funds/risk/service.py`: persist selection before retrieval, attempt only selected candidates, expose selection states, and assemble selection-bound evidence.
- `src/kunjin/funds/risk/parsers.py`: retain table structure from HTML, DOCX, and converted HTML, keep PDF limited to reliable explicit text, and adapt extracted observations into `ParsedMandateFact`.
- `src/kunjin/funds/risk/engine.py`: extend `ClassificationEvidence` with optional selection bindings and generate manifest V3 for new classifications.
- `src/kunjin/funds/risk/policy.py`: allowlist the two D1 selection evidence codes without changing financial thresholds.
- `src/kunjin/funds/risk/__init__.py`: export the new exact records and helpers.
- `src/kunjin/cli.py`: serialize selection states without private paths or raw content.
- `tests/unit/test_risk_audit.py`: selection candidate payload authentication tests.
- `tests/unit/test_risk_store.py`: selection persistence and V3 readback tests.
- `tests/unit/test_risk_service.py`: bounded sync, no fallback, selection-bound current evidence, and concurrency tests.
- `tests/unit/test_risk_parsers.py`: parser adapter and provenance parity tests.
- `tests/unit/test_risk_engine.py`: V3 manifest and selection-code monotonicity tests.
- `tests/integration/test_cli.py`: JSON selection output and technical/financial exit behavior.
- `tests/test_smoke.py`: README/Skill wording, privacy, stable-code, and installed-skill checks.
- `README.md`: D1.1-C behavior, bounded downloads, current facts, and non-goals.
- `integrations/codex/kunjin-fund/SKILL.md`: current-candidate and current-fact gates.
- `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`: byte-identical installed copy after explicit external-write approval.

## Task 0: Freeze The Accepted D1.1-B Baseline

**Files:**

- Existing modified files from the accepted v13 regression only.
- Exclude the D1.1-C spec and this implementation plan from the baseline commit.

- [ ] **Step 1: Verify the worktree contains only the accepted D1.1-B regression plus planning documents**

Run:

```bash
git status --short
git diff --check
git diff -- src/kunjin/funds/risk/legacy_doc.py \
  src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/reports.py \
  tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_parsers.py
```

Expected: the five accepted code/test files, the D1.1 overall audit, the D1.1-C
spec, and this plan are visible; `git diff --check` exits zero.

- [ ] **Step 2: Re-run the accepted baseline verification**

Run:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Expected: Ruff exits zero and the current baseline reports at least `1250 passed`.

- [ ] **Step 3: Commit only the accepted D1.1-B regression**

Run:

```bash
git add \
  src/kunjin/funds/risk/legacy_doc.py \
  src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/reports.py \
  tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_parsers.py \
  docs/audits/2026-07-14-kunjin-phase-d1-1-overall-live-regression.md
git commit -m "fix: complete phase D1.1 legacy document coverage"
```

Expected: the commit succeeds; the D1.1-C spec and plan remain untracked and no
other user changes are staged.

## Task 1: Add Pure Latest-Periodic Candidate Selection

**Files:**

- Create: `src/kunjin/funds/risk/selection.py`
- Create: `tests/unit/test_risk_selection.py`
- Modify: `src/kunjin/funds/risk/audit.py`
- Modify: `src/kunjin/funds/risk/__init__.py`

- [ ] **Step 1: Write failing exact-record and selection tests**

Create tests for a unique newest periodic candidate, a newest-time tie, a missing
kind, nonperiodic preservation, non-UTC publication times, duplicate candidate
fingerprints, mutable inputs, subclasses, checksum tampering, and deterministic
ordering. The central assertions are:

```python
plan = select_current_candidates(
    "519755",
    refresh_run_id=7,
    candidates=(old_quarter, new_quarter, annual, product_summary),
)
assert tuple(item.url for item in plan.attempted_candidates) == (
    new_quarter.url,
    annual.url,
    product_summary.url,
)
assert plan.status_for(DocumentKind.QUARTERLY_REPORT).state == "selected"

tied = select_current_candidates(
    "519755",
    refresh_run_id=8,
    candidates=(same_time_quarter_a, same_time_quarter_b),
)
assert tied.attempted_candidates == ()
assert tied.status_for(DocumentKind.QUARTERLY_REPORT).reason_code == (
    "current_periodic_candidate_conflict"
)

missing = select_current_candidates("519755", refresh_run_id=9, candidates=())
assert missing.status_for(DocumentKind.ANNUAL_REPORT).reason_code == (
    "current_periodic_candidate_missing"
)
```

- [ ] **Step 2: Run the tests and observe the missing module failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_selection.py
```

Expected: FAIL with `ModuleNotFoundError: kunjin.funds.risk.selection`.

- [ ] **Step 3: Implement the exact selection records and policy**

Create these public shapes in `selection.py`:

```python
PERIODIC_DOCUMENT_KINDS = (
    DocumentKind.ANNUAL_REPORT,
    DocumentKind.QUARTERLY_REPORT,
    DocumentKind.SEMIANNUAL_REPORT,
)
SELECTION_STATES = frozenset({"selected", "missing", "conflicted"})
SELECTION_REASON_CODES = frozenset(
    {"current_periodic_candidate_missing", "current_periodic_candidate_conflict"}
)

@dataclass(frozen=True)
class SelectionCandidate:
    candidate_fingerprint: str
    document_kind: DocumentKind
    url: str
    published_at: datetime

@dataclass(frozen=True)
class PeriodicSelectionState:
    document_kind: DocumentKind
    state: str
    candidate_fingerprints: Tuple[str, ...]
    selected_fingerprint: Optional[str]
    reason_code: Optional[str]

@dataclass(frozen=True)
class DocumentSelectionPlan:
    fund_code: str
    refresh_run_id: int
    periodic_candidates: Tuple[SelectionCandidate, ...]
    periodic_states: Tuple[PeriodicSelectionState, ...]
    attempted_candidates: Tuple[OfficialDocumentCandidate, ...]
    selection_policy_checksum: str
    canonical_json: str
    selection_checksum: str

    def status_for(self, kind: DocumentKind) -> PeriodicSelectionState:
        return next(item for item in self.periodic_states if item.document_kind is kind)
```

Implement `select_current_candidates()` by validating exact immutable inputs,
grouping periodic candidates, selecting the unique maximum publication time,
marking multiple distinct maximum fingerprints as conflicted, preserving
nonperiodic candidates under existing deterministic order, canonicalizing with
`ensure_ascii=True`, `separators=(",", ":")`, `sort_keys=True`, and calculating
the lowercase SHA-256 checksum. Expose `canonical_candidate_payload()` from
`audit.py` so selection reuses the exact fingerprint-bound fields.

- [ ] **Step 4: Run focused tests**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_risk_selection.py \
  tests/unit/test_risk_audit.py
.venv/bin/ruff check \
  src/kunjin/funds/risk/selection.py \
  src/kunjin/funds/risk/audit.py \
  tests/unit/test_risk_selection.py \
  tests/unit/test_risk_audit.py
```

Expected: tests pass and Ruff exits zero.

- [ ] **Step 5: Commit**

```bash
git add \
  src/kunjin/funds/risk/selection.py \
  src/kunjin/funds/risk/audit.py \
  src/kunjin/funds/risk/__init__.py \
  tests/unit/test_risk_selection.py \
  tests/unit/test_risk_audit.py \
  docs/superpowers/specs/2026-07-14-kunjin-phase-d1-1-c-current-report-facts-design.md \
  docs/superpowers/plans/2026-07-14-kunjin-phase-d1-1-c-current-report-facts.md
git commit -m "feat: add current periodic candidate selection"
```

## Task 2: Add Schema V13 Selection Manifest Persistence

**Files:**

- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Modify: `src/kunjin/funds/risk/store.py`
- Create: `tests/unit/test_schema_v13.py`
- Modify: `tests/unit/test_risk_store.py`

- [ ] **Step 1: Write failing Schema V13 migration tests**

Migrate copies starting at V9, V10, V11, and V12. Assert `SCHEMA_VERSION == 13`,
versions are contiguous through 13, the new table has exactly the seven designed
columns, and all existing IDs and classification manifest bytes remain unchanged.
Reject update, delete, duplicate refresh ID, noncanonical JSON, checksum mismatch,
fund/refresh mismatch, invalid timestamp, and unknown manifest version.

- [ ] **Step 2: Run migration tests and observe failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_schema_v13.py
```

Expected: FAIL because Schema V13 is absent.

- [ ] **Step 3: Add additive Schema V13**

Set `SCHEMA_VERSION = 13`, register `(13, SCHEMA_V13)`, and create:

```sql
CREATE TABLE fund_document_selection_manifests (
    refresh_run_id INTEGER PRIMARY KEY
        REFERENCES fund_document_refresh_runs(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(
        typeof(fund_code) = 'text' AND length(fund_code) = 6
        AND fund_code NOT GLOB '*[^0-9]*'
    ),
    manifest_version INTEGER NOT NULL CHECK(manifest_version = 1),
    selection_policy_checksum TEXT NOT NULL CHECK(
        length(CAST(selection_policy_checksum AS BLOB)) = 64
        AND selection_policy_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_json TEXT NOT NULL CHECK(
        typeof(canonical_json) = 'text' AND instr(canonical_json, char(0)) = 0
        AND json_valid(canonical_json) AND json_type(canonical_json) = 'object'
    ),
    selection_checksum TEXT NOT NULL UNIQUE CHECK(
        length(CAST(selection_checksum AS BLOB)) = 64
        AND selection_checksum NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(
        julianday(created_at) IS NOT NULL AND substr(created_at, -6) = '+00:00'
        AND substr(created_at, 11, 1) = 'T'
    )
);
```

Add insert binding to the matching refresh fund code plus no-update and no-delete
immutability triggers. Apply the migration under the same exclusive transaction
and contiguous-version verification used by V12.

- [ ] **Step 4: Add authenticated store methods**

Add exact `StoredDocumentSelectionManifest` and methods:

```python
publish_document_selection(plan: DocumentSelectionPlan, created_at: datetime)
document_selection_for_refresh(refresh_run_id: int)
current_document_selection(fund_code: str)
```

Every read must reparse canonical JSON, recompute SHA-256, validate policy,
candidate fields, states, fund/refresh binding, and exact record shape.

- [ ] **Step 5: Run schema and store tests**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_schema_v13.py \
  tests/unit/test_schema_v12.py \
  tests/unit/test_risk_store.py
.venv/bin/ruff check src/kunjin/storage/schema.py \
  src/kunjin/storage/repository.py src/kunjin/funds/risk/store.py \
  tests/unit/test_schema_v13.py tests/unit/test_risk_store.py
```

Expected: all suites pass and historical manifest bytes are unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py \
  src/kunjin/funds/risk/store.py tests/unit/test_schema_v13.py \
  tests/unit/test_risk_store.py
git commit -m "feat: persist authenticated document selections"
```

## Task 3: Bound Document Synchronization To The Selection Plan

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `src/kunjin/cli.py`
- Modify: `tests/unit/test_risk_service.py`
- Modify: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing bounded-sync tests**

Prove selection is persisted before fetch, only selected periodic candidates are
fetched, newest failure does not fetch an older report, a tie downloads neither,
and selection races cannot return mixed results. Add public result assertions:

```python
assert events[:3] == ["begin_refresh", "discover", "publish_selection"]
assert fetched_urls == [NEW_QUARTER_URL, NEW_ANNUAL_URL, SUMMARY_URL]
assert OLD_QUARTER_URL not in fetched_urls
assert result.selections[0].reason_code in {
    None,
    "current_periodic_candidate_missing",
    "current_periodic_candidate_conflict",
}
```

- [ ] **Step 2: Run tests and observe current all-candidate behavior**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_service.py \
  tests/integration/test_cli.py -k 'document or fund_documents'
```

Expected: FAIL because sync fetches every candidate and has no selections field.

- [ ] **Step 3: Extend exact public result records**

Add:

```python
@dataclass(frozen=True)
class DocumentSelectionItem:
    document_kind: str
    status: str
    selected_url: Optional[str]
    candidate_count: int
    reason_code: Optional[str]
```

Extend `DocumentSyncResult` with sorted `selections` and lowercase
`selection_checksum`. Reject candidate fingerprints, unselected URLs, and any
unknown status or reason code.

- [ ] **Step 4: Integrate selection before retrieval**

Use this order:

```python
refresh_id = self._risk_store.begin_document_refresh(fund_code, attempted_at)
candidates = self._discovery.discover(
    fund_code,
    manager_name=manager_name,
    announcements=getattr(bundle, "announcements", ()),
)
selection = select_current_candidates(
    fund_code, refresh_run_id=refresh_id, candidates=candidates
)
self._risk_store.publish_document_selection(selection, attempted_at)
items = tuple(
    self._sync_candidate(refresh_id, candidate)
    for candidate in selection.attempted_candidates
)
```

Missing/conflicted periodic states create no candidate runs. Selected failures
retain existing safe failure codes. Complete the refresh and return amount-free
public selection items.

- [ ] **Step 5: Update CLI JSON and privacy tests**

Emit selected kind, status, selected URL only when selected, candidate count,
reason code, and checksum. Never emit raw manifest JSON, candidate fingerprints,
unselected URLs, local paths, HTML, or exception text.

- [ ] **Step 6: Run focused verification**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_selection.py \
  tests/unit/test_risk_service.py tests/integration/test_cli.py \
  -k 'document or fund_documents or selection'
.venv/bin/ruff check src/kunjin/funds/risk/service.py src/kunjin/cli.py \
  tests/unit/test_risk_service.py tests/integration/test_cli.py
```

Expected: focused tests and Ruff pass.

- [ ] **Step 7: Commit**

```bash
git add src/kunjin/funds/risk/service.py src/kunjin/cli.py \
  tests/unit/test_risk_service.py tests/integration/test_cli.py
git commit -m "feat: bound official report synchronization"
```

## Task 4: Add Structured Current-Report Evidence Models

**Files:**

- Create: `src/kunjin/funds/risk/report_facts.py`
- Create: `tests/unit/test_risk_report_facts.py`
- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/unit/test_risk_parsers.py`

- [ ] **Step 1: Write failing exact-record and adapter tests**

Define test expectations for exact frozen `ReportCell`, `ReportRow`,
`ReportTable`, and `CurrentReportObservation` records. Assert HTML, converted
HTML, and DOCX adapters normalize the same supported table. Assert PDF extracts
only explicit supported sentences and does not reconstruct a table from visual
spacing. Reject nested tables, non-unit rowspan/colspan, duplicate headers,
empty cells, mixed denominators, oversized values, mutable containers, and
subclasses.

- [ ] **Step 2: Run tests and observe missing types**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py -k 'table or current_report'
```

Expected: FAIL because the module and adapters do not exist.

- [ ] **Step 3: Implement bounded structures**

Use:

```python
MAX_REPORT_TABLES = 256
MAX_REPORT_ROWS = 20_000
MAX_REPORT_CELLS_PER_ROW = 32
MAX_REPORT_CELL_CHARACTERS = 4_096

@dataclass(frozen=True)
class ReportCell:
    text: str
    is_header: bool

@dataclass(frozen=True)
class ReportRow:
    cells: Tuple[ReportCell, ...]

@dataclass(frozen=True)
class ReportTable:
    rows: Tuple[ReportRow, ...]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str

@dataclass(frozen=True)
class CurrentReportObservation:
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    confidence_state: FactConfidence
```

Each `validate()` rejects subclasses, unexpected state, non-tuples, control
characters, invalid page/section values, and configured limits.

- [ ] **Step 4: Retain tables without breaking existing legal extraction**

Modify HTML, converted-HTML, and DOCX adapters to return normalized text blocks
plus supported tables. Keep PDF on its existing text-block path and pass an
empty table tuple unless a future separately approved parser establishes
reliable structural evidence. Continue generating existing bound text blocks
so legal-fact and historical tests remain compatible. Add one adapter that
converts a `CurrentReportObservation` to an exact `ParsedMandateFact` and
computes the existing `fact_fingerprint` from every source field.

- [ ] **Step 5: Run parser verification**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py
git commit -m "feat: preserve structured current report evidence"
```

## Task 5: Extract Common Asset And Concentration Facts

**Files:**

- Modify: `src/kunjin/funds/risk/report_facts.py`
- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/unit/test_risk_report_facts.py`
- Modify: `tests/unit/test_risk_parsers.py`

- [ ] **Step 1: Write failing common-fact tests**

Cover the exact allowlist:

```python
COMMON_FACTS = {
    "current_stock_asset_allocation_percent",
    "current_bond_asset_allocation_percent",
    "current_cash_asset_allocation_percent",
    "current_hong_kong_asset_allocation_percent",
    "current_largest_security_weight_percent",
    "current_top_ten_holdings_weight_percent",
    "current_largest_industry_name",
    "current_largest_industry_weight_percent",
    "current_industry_count",
    "holdings_evidence_complete",
}
```

Include explicit supported tables and sentences plus missing-header, mixed
denominator, rounded-range, partial-scope, unknown-other, duplicate-rank,
fewer-than-ten, incomplete-appendix, and mandate-wording rejection cases.

- [ ] **Step 2: Run tests and observe missing extraction**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k common
```

Expected: FAIL because common extraction is absent.

- [ ] **Step 3: Implement explicit common extraction**

Add:

```python
def extract_common_report_observations(
    *, text_blocks: Tuple[str, ...], tables: Tuple[ReportTable, ...]
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    observations.extend(_asset_allocation_observations(tables))
    observations.extend(_security_concentration_observations(tables))
    observations.extend(_industry_observations(tables))
    observations.extend(_explicit_common_text_observations(text_blocks))
    return _validated_unique_observations(observations)
```

Use exact allowlisted header aliases and units only. Do not calculate remainders,
use fuzzy similarity, or infer complete holdings from top ten. Emit
`holdings_evidence_complete=true` only for an explicit complete scope and fully
parsed supported table.

- [ ] **Step 4: Bind report period to every observation**

Set both effective dates to `report_period_end(candidate.title)`. Invalid or
missing report period remains `parser_effective_date_invalid` and emits no
current facts.

- [ ] **Step 5: Run common-fact and compatibility suites**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k common \
  tests/unit/test_risk_parsers.py tests/unit/test_risk_engine.py \
  -k 'common or active or sector or broad or current'
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py
```

Expected: tests pass and no classification improves from missing evidence.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py
git commit -m "feat: extract current asset and concentration facts"
```

## Task 6: Extract Fixed-Income Current Facts

**Files:**

- Modify: `src/kunjin/funds/risk/report_facts.py`
- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/unit/test_risk_report_facts.py`
- Modify: `tests/unit/test_risk_parsers.py`
- Modify: `tests/unit/test_risk_engine.py`

- [ ] **Step 1: Write failing fixed-income tests**

Cover:

```python
FIXED_INCOME_FACTS = {
    "current_effective_duration",
    "current_weighted_average_maturity_days",
    "current_convertible_bond_asset_allocation_percent",
    "current_exchangeable_bond_asset_allocation_percent",
    "current_high_quality_fixed_income_percent",
    "current_below_aa_plus_exposure_percent",
    "current_unrated_non_sovereign_exposure_percent",
    "current_gross_leverage_percent",
    "current_largest_non_sovereign_issuer_percent",
}
```

Include complete/incomplete rating distributions, absent ratings, sovereign and
policy-bank exclusions, duplicate issuers, incomplete issuer tables,
duration-versus-maturity separation, convertible/exchangeable separation, and
gross-leverage denominator validation. Missing rows must not become zero.

- [ ] **Step 2: Run tests and observe failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k fixed_income
```

Expected: FAIL because fixed-income extraction is absent.

- [ ] **Step 3: Implement fixed-income extraction**

Add:

```python
def extract_fixed_income_report_observations(
    *, text_blocks: Tuple[str, ...], tables: Tuple[ReportTable, ...]
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    observations.extend(_duration_and_maturity_observations(tables, text_blocks))
    observations.extend(_convertible_exchangeable_observations(tables))
    observations.extend(_credit_distribution_observations(tables))
    observations.extend(_gross_leverage_observations(tables, text_blocks))
    observations.extend(_issuer_concentration_observations(tables))
    return _validated_unique_observations(observations)
```

Credit aggregation requires a complete supported distribution and one
denominator. Issuer concentration excludes only explicit sovereign and
policy-bank vocabulary. Legal prohibitions and ceilings remain legal facts.

- [ ] **Step 4: Prove missing-evidence monotonicity**

Start from the existing 20 bond missing-evidence codes. Add one current fact at
a time and assert only its matching observation code can disappear. Removing a
fact, lowering confidence, making evidence stale, or adding a conflict must not
improve evidence status, bucket, or role.

- [ ] **Step 5: Run fixed-income verification**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k fixed_income \
  tests/unit/test_risk_parsers.py tests/unit/test_risk_engine.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_engine.py
```

Expected: all tests pass with exact missing-evidence preservation.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py tests/unit/test_risk_report_facts.py \
  tests/unit/test_risk_parsers.py tests/unit/test_risk_engine.py
git commit -m "feat: extract current fixed income facts"
```

## Task 7: Bind Selection And Freshness In Classification Manifest V3

**Files:**

- Modify: `src/kunjin/funds/risk/engine.py`
- Modify: `src/kunjin/funds/risk/store.py`
- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `src/kunjin/funds/risk/policy.py`
- Modify: `tests/unit/test_risk_engine.py`
- Modify: `tests/unit/test_risk_store.py`
- Modify: `tests/unit/test_risk_service.py`

- [ ] **Step 1: Write failing V3 tests**

Extend `ClassificationEvidence` with all-or-none optional selection bindings:

```python
document_refresh_run_id: Optional[int]
selection_policy_checksum: Optional[str]
selection_manifest_checksum: Optional[str]
selection_reason_codes: Tuple[str, ...]
```

Prove V1/V2 reconstruct `None, None, None, ()`; new evidence requires all three
digests/ID and sorted allowlisted codes. Test canonical-byte preservation and
tamper rejection for every V3 field.

- [ ] **Step 2: Run tests and observe V2-only failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py -k manifest \
  tests/unit/test_risk_store.py -k manifest
```

Expected: FAIL because current manifests stop at V2.

- [ ] **Step 3: Implement manifest V3**

Add:

```python
def classification_input_manifest_v3(evidence, policy, classified_at):
    payload = classification_input_manifest_v2(evidence, policy, classified_at)
    payload.update(
        {
            "manifest_version": 3,
            "document_refresh_run_id": evidence.document_refresh_run_id,
            "selection_policy_checksum": evidence.selection_policy_checksum,
            "selection_manifest_checksum": evidence.selection_manifest_checksum,
            "selection_reason_codes": list(evidence.selection_reason_codes),
        }
    )
    return payload
```

Extend exact V3 keys, decoding, reconstruction, recalculation, and store
authentication. New current classifications always use V3; historical V1/V2
functions and bytes remain unchanged.

- [ ] **Step 4: Assemble only selection-bound current reports**

Load the current selection manifest and the exact candidate runs under its
refresh. For selected kinds require the selected candidate and current parse;
for missing/conflicted kinds bind the exact selection reason; for selected
failure retain its existing failure and never load an older artifact. Keep
history-wide selection only for authenticating historical V1/V2 records.

- [ ] **Step 5: Bind report-period freshness and monotonic codes**

Download time must not extend report validity. Add the two selection codes to
the exact D1 evidence allowlist; they can keep or worsen results, never improve
them, and remain separate from technical document failures.

- [ ] **Step 6: Run V3 verification**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py \
  tests/unit/test_risk_store.py tests/unit/test_risk_service.py \
  tests/unit/test_schema_v13.py
.venv/bin/ruff check src/kunjin/funds/risk/engine.py \
  src/kunjin/funds/risk/store.py src/kunjin/funds/risk/service.py \
  src/kunjin/funds/risk/policy.py
```

Expected: all tests pass; new records use V3 and old records authenticate.

- [ ] **Step 7: Commit**

```bash
git add src/kunjin/funds/risk/engine.py src/kunjin/funds/risk/store.py \
  src/kunjin/funds/risk/service.py src/kunjin/funds/risk/policy.py \
  tests/unit/test_risk_engine.py tests/unit/test_risk_store.py \
  tests/unit/test_risk_service.py
git commit -m "feat: authenticate current report selections"
```

## Task 8: Update Documentation, Skill, And Privacy Contracts

**Files:**

- Modify: `src/kunjin/cli.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`
- Sync after approval: `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`

- [ ] **Step 1: Write failing wording and privacy tests**

Require bounded per-kind selection, no historical fallback, observation versus
mandate separation, top-ten incompleteness, exact selection codes, D2/D3/Phase E
exclusions, and no 90 percent claim. Prove JSON/logs exclude raw selection JSON,
candidate fingerprints, unselected URLs, managed paths, HTML, database paths,
and exception text.

- [ ] **Step 2: Run tests and observe failure**

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py \
  tests/test_smoke.py -k 'skill or readme or privacy or fund_documents'
```

Expected: FAIL until new behavior is documented and serialized.

- [ ] **Step 3: Update README, Skill, and CLI explanations**

Keep the gate order unchanged. Explain selection missing/conflict separately
from document technical failures. Never permit old-report reuse, missing-fact
inference, Phase C real-product mapping, direction, or amount.

- [ ] **Step 4: Verify and synchronize the installed Skill**

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py tests/test_smoke.py
.venv/bin/ruff check src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
```

After explicit external-write approval:

```bash
cp integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp -s integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
```

Expected: tests pass and `cmp` exits zero.

- [ ] **Step 5: Commit**

```bash
git add src/kunjin/cli.py README.md integrations/codex/kunjin-fund/SKILL.md \
  tests/integration/test_cli.py tests/test_smoke.py
git commit -m "docs: expose phase D1.1 current evidence gates"
```

## Task 9: Full Verification And Representative Live Acceptance

**Files:**

- Create: `docs/audits/2026-07-14-kunjin-phase-d1-1-c-live-acceptance.md`

- [ ] **Step 1: Run complete local verification**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check src/kunjin/funds/risk/selection.py \
  src/kunjin/funds/risk/report_facts.py src/kunjin/funds/risk/audit.py \
  src/kunjin/funds/risk/store.py src/kunjin/funds/risk/service.py \
  src/kunjin/funds/risk/parsers.py src/kunjin/funds/risk/engine.py \
  src/kunjin/funds/risk/policy.py src/kunjin/storage/schema.py \
  src/kunjin/storage/repository.py src/kunjin/cli.py
.venv/bin/python -m compileall -q src tests
.venv/bin/pip check
.venv/bin/python -m pytest -q
git diff --check
```

Expected: all commands exit zero. Record the fresh final pytest count.

- [ ] **Step 2: Confirm converter status**

```bash
.venv/bin/kunjin --json fund converter-status
```

Expected: ready, parser `4-docker-libreoffice-v1`, checksum
`d73408012e76ce6264bea8ddcaeff08027cc086c144d0b93622694ff5953c100`.

- [ ] **Step 3: Run a clean four-fund acceptance**

Use fresh timestamped data/state/results directories. For `519706`, `164905`,
`519718`, and `519755`, run profile, holdings, documents, classify, evidence,
and history. Preserve stdout, stderr, and exits.

Assert each periodic kind attempts at most one candidate, no selected failure is
followed by an older attempt, every new classification is manifest V3, and each
V3 selection checksum matches Schema V13. Do not require the bond fund to pass;
require every remaining missing code to remain exact.

- [ ] **Step 4: Inspect real facts and write acceptance audit**

Record selected periods, success/failure counts, current common/fixed-income
facts, selection codes, technical failures, classification state, provenance,
and before/after periodic download counts. At least one official current asset
allocation fact must complete end to end.

- [ ] **Step 5: Commit acceptance**

```bash
git add docs/audits/2026-07-14-kunjin-phase-d1-1-c-live-acceptance.md
git commit -m "test: record phase D1.1-C live acceptance"
```

## Task 10: Current-Holdings Coverage Audit And Independent Review

**Files:**

- Create: `docs/audits/2026-07-14-kunjin-phase-d1-1-c-independent-review.md`
- No production modification is authorized by this review task.

- [ ] **Step 1: Synchronize current holdings privately**

```bash
.venv/bin/kunjin --json status
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json portfolio show
```

Use only the amount-free unique fund-code list for coverage. Do not copy personal
amounts into chat, audits, or fixtures.

- [ ] **Step 2: Run automatic D1.1-C coverage for every held fund code**

For each unique code:

```bash
.venv/bin/kunjin --json sync fund-profile CODE
.venv/bin/kunjin --json sync fund-holdings CODE
.venv/bin/kunjin --json sync fund-documents CODE
.venv/bin/kunjin --json fund classify CODE
.venv/bin/kunjin --json fund classification-evidence CODE
```

Do not hand-edit the database or add fund-specific exceptions during the audit.

- [ ] **Step 3: Calculate objective coverage**

Report total codes, automatic discovery coverage, selected current-periodic
coverage, current asset-allocation coverage, technical/selection/stale/missing/
conflict counts, median and maximum periodic attempts, and cases requiring new
manager/domain or document-shape adapters. Exit-zero partial/stale results are
not verified coverage.

- [ ] **Step 4: Perform the independent financial review**

Use the same ten-area beginner rubric. Give D2, D3, and Phase E zero credit.
Explicitly decide whether onboarding is automatic for the observed sample,
which beginner errors are prevented, which categories remain weak, whether 90
percent is reached, and whether D1 can close before D2.

- [ ] **Step 5: Run final verification and commit review**

```bash
git diff --check
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
git add docs/audits/2026-07-14-kunjin-phase-d1-1-c-independent-review.md
git commit -m "docs: review phase D1.1-C beginner coverage"
```

## Completion Gate

D1.1-C is complete only when the accepted baseline is committed separately,
Schema V13 preserves history, selection is persisted before downloads, at most
one periodic candidate per kind is attempted, no fallback occurs, explicit
current facts remain non-inferred, manifest V3 authenticates selection and
freshness, full verification passes, representative live acceptance passes,
current-holdings coverage is measured, and the independent review reassesses the
90 percent claim and D1-to-D2 transition.

No result may include a purchase direction, amount, target weight, real-product
Phase C mapping, or automatic trade instruction.
