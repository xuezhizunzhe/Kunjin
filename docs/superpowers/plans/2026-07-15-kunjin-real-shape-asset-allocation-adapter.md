# KunJin Real-Shape Asset Allocation Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task-by-task with a fresh subagent for each production task and independent review before acceptance.

**Goal:** Authenticate at least one current stock or bond asset-allocation fact from the real four-column public-fund report shape without weakening failure-closed table rules.

**Architecture:** Add a separate four-column fact adapter beside the existing three-column adapter. Recover empty sequence cells only after an exact converted-HTML shape gate; preserve all span, key-cell, report-period, provenance, selection, and Manifest V3 checks.

**Tech Stack:** Python 3.11, `html.parser`, frozen report records, `Decimal`, SQLite Schema V13, pytest, Ruff, Docker LibreOffice.

---

## File Map

- `src/kunjin/funds/risk/report_facts.py`: controlled four-column vocabulary and observations.
- `src/kunjin/funds/risk/parsers.py`: shape-gated empty-sequence recovery.
- `tests/unit/test_risk_report_facts.py`: financial-binding and negative tests.
- `tests/fixtures/funds/risk/legacy-converted-report-real-shape.html`: minimized structural fixture.
- `tests/unit/test_risk_parsers.py`: converted-HTML end-to-end and rejection tests.
- `docs/superpowers/plans/2026-07-14-kunjin-phase-d1-1-c-current-report-facts.md`: parser-v4 expectation.
- `docs/audits/2026-07-14-kunjin-phase-d1-1-c-live-acceptance.md`: sanitized acceptance.

## Task 1: Controlled Four-Column Fact Adapter

**Files:**

- Modify: `src/kunjin/funds/risk/report_facts.py`
- Modify: `tests/unit/test_risk_report_facts.py`

- [ ] **Step 1: Write failing tests**

Build an immutable `ReportTable` with the exact normalized header
`("序号", "项目", "金额(元)", "占基金总资产的比例(%)")`, explicit
`其中:股票` and `其中:债券` rows, and a bank-deposit row. Use
`[kunjin-empty-sequence]` in the two hierarchical sequence cells.

Assert exact stock and bond facts use `percent_of_total_assets`; assert no cash
fact. Add wrong-header, wrong-width, empty-key-cell, malformed-amount,
out-of-range-percent, unknown-label, equivalent-duplicate, and conflicting-value
cases. Conflicting values must remain separate for the existing conflict logic.

- [ ] **Step 2: Observe the red state**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py \
  -k 'real_shape or four_column_asset'
```

Expected: the positive path fails because only `indicator/unit/value` exists.

- [ ] **Step 3: Implement the minimal adapter**

Add:

```python
EMPTY_SEQUENCE_CELL_TEXT = "[kunjin-empty-sequence]"
_REAL_ASSET_HEADERS = {
    ("序号", "项目", "金额(元)", "占基金总资产的比例(%)"),
}
_REAL_ASSET_LABELS = {
    "其中:股票": "current_stock_asset_allocation_percent",
    "其中:债券": "current_bond_asset_allocation_percent",
}

def is_real_asset_table_header(values: Tuple[str, ...]) -> bool:
    return tuple(_normalized(value) for value in values) in _REAL_ASSET_HEADERS
```

Implement a separate four-column extractor. Require an NFKC-normalized canonical
ASCII integer or a structurally flagged private sequence placeholder, a finite
nonnegative mapped-row amount after exact comma removal, and a 0-100 percentage.
Reject later header rows and unflagged literal placeholder text. Map only the two explicit labels and use
`percent_of_total_assets`. Build the public excerpt only from project, amount,
and percentage cells. Append this adapter without changing the three-column path.

- [ ] **Step 4: Verify Task 1**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  tests/unit/test_risk_report_facts.py
```

## Task 2: Shape-Gated Empty Sequence Recovery

**Files:**

- Modify: `src/kunjin/funds/risk/parsers.py`
- Create: `tests/fixtures/funds/risk/legacy-converted-report-real-shape.html`
- Modify: `tests/unit/test_risk_parsers.py`

- [ ] **Step 1: Add a minimized real-shape fixture and failing tests**

Copy only the identity/period shell of the existing synthetic quarterly fixture
and replace its asset table with the four-column shape from Task 1. Use synthetic
amounts and no downloaded text.

Assert end-to-end stock and bond facts, total-assets units, effective report date,
and parser-v4 provenance. Add empty project/amount/percentage, nontrivial span,
fifth-column, uneven-row, near-match-header, and non-target-empty-cell negatives.

- [ ] **Step 2: Observe the red state**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_parsers.py \
  -k 'real_shape_asset or empty_sequence'
```

Expected: the positive table is rejected because its sequence cell is empty.

- [ ] **Step 3: Implement shape-gated recovery**

Retain empty cell text temporarily. Before constructing `ReportCell`, return the
raw rows unchanged when none are empty. Otherwise require exactly four cells per
row, the exact real-asset header, a fully populated header, and non-empty project,
amount, and percentage cells. Reject source cells already equal to the private
placeholder and every later row containing a header cell. Replace only empty
data-row sequence cells with `EMPTY_SEQUENCE_CELL_TEXT` and set the new exact
`ReportCell` structural-placeholder flag; reject every other empty-cell table.

Keep `_table_invalid` authoritative for nesting, duplicate attributes,
nontrivial spans, and unsupported tags. Do not enable converted-text current-fact
extraction.

- [ ] **Step 4: Verify Task 2**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_report_facts.py
.venv/bin/ruff check src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/report_facts.py tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_report_facts.py
git diff --check
```

## Task 2.1: Preserve Safe Converted Heading Layout Whitespace

**Files:**

- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `tests/fixtures/funds/risk/legacy-converted-report-real-shape.html`
- Modify: `tests/unit/test_risk_parsers.py`

- [ ] **Step 1: Add the live-derived red and adversarial cases**

Wrap the fixture heading so its visible text is surrounded by separate pure
indentation/newline fragments. Assert the real-shape facts remain present. Add
negatives where one visible heading fragment contains CR/LF, or where a pure
fragment contains U+0085, Cf, or default-ignorable content.

- [ ] **Step 2: Normalize only converted pure layout fragments**

Add an exact parser-constructor flag enabled only by `_converted_html_content`.
When active, a heading `handle_data` fragment consisting entirely of ASCII
space, tab, CR, LF, or form-feed becomes one ASCII space. Preserve every other
fragment byte-for-character for the existing `_section_context` safety gate. Do
not change `_has_unsafe_time_context_character`.

- [ ] **Step 3: Verify Task 2.1**

Run the new red-green cases, all existing unsafe-heading tests, both complete
parser/report-fact files, Ruff, and `git diff --check`. Unit tests alone do not
satisfy the live acceptance gate.

## Task 3: Full Verification And Live Re-Acceptance

**Files:**

- Modify: `docs/superpowers/plans/2026-07-14-kunjin-phase-d1-1-c-current-report-facts.md`
- Create: `docs/audits/2026-07-14-kunjin-phase-d1-1-c-live-acceptance.md`

- [ ] **Step 1: Correct the stale converter expectation**

Use parser `4-docker-libreoffice-v1` and provenance checksum
`d73408012e76ce6264bea8ddcaeff08027cc086c144d0b93622694ff5953c100`.

- [ ] **Step 2: Run full local verification**

```bash
.venv/bin/ruff check .
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
.venv/bin/python -m pytest -q
git diff --check
```

Run scoped `ruff format --check` on every changed code/test file. Record, but do
not bulk-format, unrelated historical formatter debt.

- [ ] **Step 3: Run a new isolated four-fund acceptance**

Use fresh timestamped data, state, and result directories under `/private/tmp`
and the fixed reviewed image ID. Run profile, holdings, documents, classify,
evidence, and history for `519706`, `164905`, `519718`, and `519755`.

Require converter v4 ready, Schema 1-13, canonical selection checksums, at most
one attempt per periodic kind, no old fallback, Manifest V3, authenticated
evidence/history readback, and at least one periodic official current stock or
bond fact. Preserve the exact known `519718` holdings failure if it remains.

- [ ] **Step 4: Write and review the sanitized audit**

Record only aggregate counts, selected dates, stable status/reason/missing codes,
matched authentication booleans, and current fact kinds/values/units. Exclude raw
URLs, fingerprints, canonical JSON, paths, excerpts, response text, HTML,
databases, and exception text. Two independent reviewers must report no P0/P1/P2.

- [ ] **Step 5: Commit verified files only**

Stage only production, tests, the stale-plan correction, and the final audit.
Do not stage unrelated untracked design or plan files.

```bash
git commit -m "fix: parse current asset allocation tables"
```
