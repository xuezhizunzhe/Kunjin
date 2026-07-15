# KunJin Fixed-Income Parser V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every implementation task requires independent specification and quality reviews before the next task.

**Goal:** Extract nine fail-closed current fixed-income facts under a new parser v4 identity while preserving parser v2/v3 history byte-for-byte.

**Architecture:** Keep the immutable provenance model and introduce active native parser `4` plus active legacy parser `4-docker-libreoffice-v1`. Add a bounded fixed-income extractor beside the common extractor, then bind its observations through the existing report-period and conflict pipeline without changing Policy V1.

**Tech Stack:** Python 3, immutable dataclasses, Decimal, SQLite provenance storage, pytest/unittest, Ruff, Docker LibreOffice provenance status.

---

## File Map

- `src/kunjin/funds/risk/audit.py`: active and historical parser identities.
- `src/kunjin/funds/risk/legacy_doc.py`: converter status and legacy provenance.
- `src/kunjin/funds/risk/service.py`: current-versus-historical evidence gate.
- `src/kunjin/funds/risk/report_facts.py`: fixed-income structural extraction.
- `src/kunjin/funds/risk/parsers.py`: report integration, date binding, conflicts.
- `tests/unit/test_risk_audit.py`, `test_risk_legacy_doc.py`,
  `test_risk_service.py`, `test_risk_store.py`, `test_schema_v12.py`, and
  `tests/test_smoke.py`: parser v4 and historical compatibility.
- `tests/unit/test_risk_report_facts.py`, `test_risk_parsers.py`, and
  `test_risk_engine.py`: financial extraction and monotonicity.

## Task 1: Preserve Parser V3 Until V4 Behavior Is Complete

**Files:**

- Verify: `src/kunjin/funds/risk/audit.py`
- Verify: `src/kunjin/funds/risk/parsers.py`

- [x] **Step 1: Prove early activation is unsafe**

Independent review demonstrated that activating v4 before the nine fixed-income
facts exist permits an artifact to persist the old fact set under the future v4
identity. Reprocessing the same artifact after Task 3 would then violate the
immutable `(source_document_id, provenance_id)` result contract.

- [x] **Step 2: Restore the accepted v3 runtime identity**

The active identity remains:

```python
ACTIVE_NATIVE_PARSER_VERSION = "3"
ACTIVE_LEGACY_PARSER_VERSION = "3-docker-libreoffice-v1"
HISTORICAL_NATIVE_PARSER_VERSIONS = frozenset({"2"})
HISTORICAL_LEGACY_PARSER_VERSIONS = frozenset({"2-docker-libreoffice-v1"})
```

Do not run a real sync under an incomplete v4 identity. The v4 switch and all
v4 compatibility tests move to Task 3 and must land in the same commit as the
complete parser behavior.

## Task 2: Extract Structurally Complete Fixed-Income Facts

**Files:**

- Modify: `src/kunjin/funds/risk/report_facts.py`
- Modify: `tests/unit/test_risk_report_facts.py`

- [ ] **Step 1: Write failing tests for all nine facts and closures**

Use this exact allowlist and success expectation:

```python
FIXED_INCOME_FACTS = frozenset(
    {
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
)
self.assertEqual(set(values), FIXED_INCOME_FACTS)
self.assertEqual(values["current_high_quality_fixed_income_percent"], Decimal("80"))
```

Cover incomplete or mixed-denominator credit tables, missing/unknown/ranged or
duplicate ratings, unexplained other rows, duplicate issuers, incomplete issuer
scope, empty post-exclusion sets, sovereign/policy-bank category exclusion,
combined convertible/exchangeable rows, and leverage above 100 with valid and
invalid denominators.

- [ ] **Step 2: Run tests and verify a missing-symbol failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k fixed_income
```

Expected: the fixed-income allowlist or extractor is absent.

- [ ] **Step 3: Add a separate allowlist and extractor**

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
    return _validated_unique_observations(tuple(observations))
```

Use exact header, section, rating, issuer-category, scope, and denominator
vocabularies. Return no credit aggregate if any row cannot be classified. Use a
leverage-specific non-negative Decimal parser instead of the ordinary 100 limit.

- [ ] **Step 4: Verify and commit extraction**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k fixed_income
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  tests/unit/test_risk_report_facts.py
git diff --check
git add src/kunjin/funds/risk/report_facts.py tests/unit/test_risk_report_facts.py
git commit -m "feat: extract current fixed income facts"
```

## Task 3: Bind V4 Facts To Current Reports And Policy V1

**Files:**

- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `src/kunjin/funds/risk/audit.py`
- Verify or modify only when required: `src/kunjin/funds/risk/legacy_doc.py`
- Verify or modify only when required: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_parsers.py`
- Modify: `tests/unit/test_risk_engine.py`
- Modify: `tests/unit/test_risk_audit.py`
- Modify: `tests/unit/test_risk_legacy_doc.py`
- Modify: `tests/unit/test_risk_service.py`
- Modify: `tests/unit/test_risk_store.py`
- Modify: `tests/unit/test_schema_v12.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser and monotonicity tests**

Prove duration and maturity bind to the authenticated report period. Map the
nine facts to exactly eight observation missing codes:

```python
self.assertEqual(
    set(current.missing_evidence),
    set(baseline.missing_evidence) - {matching_observation_code},
)
self.assert_not_improved(current, baseline)
```

Also test deletion, ambiguous confidence, stale freshness, conflicting values,
and threshold breaches. In the same red phase, add the complete v4 provenance
contract: active native/legacy v4, v2/v3 immutable history, stored native and
legacy v3 rejected as current, same-artifact v3/v4 coexistence, unknown v5
rejection, and v4 converter-status JSON.

- [ ] **Step 2: Run tests and observe missing parser integration**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_parsers.py -k fixed_income
.venv/bin/python -m pytest -q tests/unit/test_risk_engine.py \
  -k 'bond and monotonic'
```

- [ ] **Step 3: Integrate without opening unsafe sources**

```python
observations = list(extract_common_report_observations(text_blocks=(), tables=tables))
observations.extend(
    extract_fixed_income_report_observations(text_blocks=(), tables=tables)
)
```

For each eligible non-`nfc_only` text block, call both extractors and preserve
page/section binding. Do not reconstruct PDF tables, enable legacy `nfc_only`
text, or relax trusted-heading and temporal-context checks. Reuse the existing
report-period validation and `duplicate_conflicting_clause` handling.

Only after the complete extraction path is present, atomically switch identity:

```python
ACTIVE_NATIVE_PARSER_VERSION = "4"
ACTIVE_LEGACY_PARSER_VERSION = "4-docker-libreoffice-v1"
HISTORICAL_NATIVE_PARSER_VERSIONS = frozenset({"2", "3"})
HISTORICAL_LEGACY_PARSER_VERSIONS = frozenset(
    {"2-docker-libreoffice-v1", "3-docker-libreoffice-v1"}
)
```

- [ ] **Step 4: Verify and commit report binding**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k fixed_income
.venv/bin/python -m pytest -q \
  tests/unit/test_risk_report_facts.py tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_engine.py \
  tests/unit/test_risk_audit.py tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_service.py tests/unit/test_risk_store.py \
  tests/unit/test_schema_v12.py tests/integration/test_cli.py tests/test_smoke.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  src/kunjin/funds/risk/parsers.py src/kunjin/funds/risk/audit.py \
  src/kunjin/funds/risk/legacy_doc.py src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_report_facts.py tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_engine.py tests/unit/test_risk_audit.py \
  tests/unit/test_risk_legacy_doc.py tests/unit/test_risk_service.py \
  tests/unit/test_risk_store.py tests/unit/test_schema_v12.py \
  tests/integration/test_cli.py tests/test_smoke.py
git diff --check
git add src/kunjin/funds/risk/audit.py src/kunjin/funds/risk/legacy_doc.py \
  src/kunjin/funds/risk/service.py src/kunjin/funds/risk/parsers.py \
  tests/unit/test_risk_parsers.py tests/unit/test_risk_engine.py \
  tests/unit/test_risk_audit.py tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_service.py tests/unit/test_risk_store.py \
  tests/unit/test_schema_v12.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: activate fixed income report parser v4"
```

## Task 4: Full Regression And Independent Acceptance

**Files:**

- Modify only if an independently verified in-scope defect requires correction.

- [ ] **Step 1: Run complete local verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
git diff --check
git status --short
```

- [ ] **Step 2: Verify converter status using the reviewed image**

```bash
.venv/bin/kunjin --json fund converter-status
```

Expected: `status=ready`, parser `4-docker-libreoffice-v1`, unchanged image
identity and inputs, and a parser-version-derived checksum.

- [ ] **Step 3: Run two independent read-only reviews**

One reviewer checks the design and exact Task 1-3 diff. A second reviewer checks
financial false positives, missing-evidence monotonicity, provenance history,
beginner-facing claims, and the absence of trade direction or amount. No
P0/P1/P2 finding may remain.

- [ ] **Step 4: Record the objective financial assessment**

State which bond observation gaps can now be filled, which live funds remain
missing or stale, whether live coverage improved, and why D2/D3/Phase E still
prevent a 90 percent beginner-help claim.

- [ ] **Step 5: Commit only a required acceptance correction**

Do not create an empty commit. Add a focused regression test before any review
correction, rerun Step 1, and stage only the in-scope files.
