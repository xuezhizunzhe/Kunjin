# KunJin Controlled Industry Taxonomy Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task. Each task requires an implementation subagent followed by independent specification and quality reviews.

**Goal:** Remove free-text industry facts from current classification, establish an empty-by-default controlled taxonomy boundary, upgrade report parsing to authenticated parser v3, and fail closed on incomplete disclosure and unsafe time context.

**Architecture:** A pure taxonomy module separates recognized metadata from fact-eligible pinned mappings. Production begins with no eligible mapping, so periodic-report and disclosure-service industry facts remain missing. Parser v3 changes provenance without rewriting v2 history; known v2 provenance remains readable but cannot satisfy current evidence after the upgrade.

**Tech Stack:** Python 3.11, frozen dataclasses, regular expressions, Decimal, SQLite immutable evidence store, pytest/unittest, Ruff, SHA-256 canonical JSON.

---

## File Map

- `docs/superpowers/specs/2026-07-15-kunjin-controlled-industry-taxonomy-gate-design.md`: approved architecture and failure rules.
- `docs/superpowers/plans/2026-07-15-kunjin-controlled-industry-taxonomy-gate.md`: this execution plan.
- `src/kunjin/funds/industry_taxonomy.py`: pure metadata, pinned mapping, row, and whole-distribution validation.
- `src/kunjin/funds/risk/report_facts.py`: report-table adapter with no free-text industry authorization.
- `src/kunjin/funds/risk/parsers.py`: parser v3 and unsafe time-context rejection.
- `src/kunjin/funds/risk/audit.py`: active v3 provenance plus exact historical v2 validation.
- `src/kunjin/funds/risk/legacy_doc.py`: active converter status accepts parser v3 without changing the image contract.
- `src/kunjin/funds/risk/service.py`: historical provenance handling and removal of incomplete external industry synthesis.
- `tests/unit/test_industry_taxonomy.py`: exact registry and distribution contracts.
- `tests/unit/test_risk_report_facts.py`: report-table fail-closed and synthetic mapping tests.
- `tests/unit/test_risk_parsers.py`: parser v3 and time-context tests.
- `tests/unit/test_risk_audit.py`: native/legacy v2 readback and v3 active provenance tests.
- `tests/unit/test_risk_service.py`: stale parser behavior and zero external industry facts.
- `tests/unit/test_risk_store.py`: immutable v2 readback and distinct v3 parse-result identity.
- `tests/integration/test_cli.py`: current classification behavior after parser upgrade.

## Task 0: Record The Approved Boundary

**Files:**

- Add: `docs/superpowers/specs/2026-07-15-kunjin-controlled-industry-taxonomy-gate-design.md`
- Add: `docs/superpowers/plans/2026-07-15-kunjin-controlled-industry-taxonomy-gate.md`

- [ ] **Step 1: Verify the design scope**

Run:

```bash
rg -n "Status:|fact-eligible|parser v3|reduces current industry coverage to zero|purchase decision" \
  docs/superpowers/specs/2026-07-15-kunjin-controlled-industry-taxonomy-gate-design.md
```

Expected: the empty production eligibility set, parser v3, zero industry coverage, and research-only boundary are explicit.

- [ ] **Step 2: Scan for placeholders and whitespace errors**

Run:

```bash
rg -n "T[B]D|T[O]DO|F[I]XME" \
  docs/superpowers/specs/2026-07-15-kunjin-controlled-industry-taxonomy-gate-design.md \
  docs/superpowers/plans/2026-07-15-kunjin-controlled-industry-taxonomy-gate.md
git diff --check
```

Expected: no placeholder matches and no whitespace errors.

- [ ] **Step 3: Commit the approved documents**

```bash
git add \
  docs/superpowers/specs/2026-07-15-kunjin-controlled-industry-taxonomy-gate-design.md \
  docs/superpowers/plans/2026-07-15-kunjin-controlled-industry-taxonomy-gate.md
git commit -m "docs: define controlled industry taxonomy gate"
```

## Task 1: Add The Pure Controlled Taxonomy Boundary

**Files:**

- Create: `src/kunjin/funds/industry_taxonomy.py`
- Create: `tests/unit/test_industry_taxonomy.py`

- [ ] **Step 1: Write failing exact-record and empty-production tests**

Create tests for these records and functions:

```python
from kunjin.funds.industry_taxonomy import (
    PRODUCTION_TAXONOMY_MAPPINGS,
    IndustryDistributionRow,
    IndustryTaxonomyMapping,
    RecognizedIndustryTaxonomy,
    validate_industry_distribution,
)


def test_production_registry_enables_no_mapping() -> None:
    assert PRODUCTION_TAXONOMY_MAPPINGS == ()


def test_complete_test_mapping_validates_one_distribution() -> None:
    mapping = test_mapping(entries=(("801780", "银行"), ("801080", "电子")))
    rows = (
        row(1, "801780", "银行", "12.50"),
        row(2, "801080", "电子", "8.35"),
    )
    validated = validate_industry_distribution(
        rows=rows,
        complete_scope=True,
        mappings=(mapping,),
    )
    assert validated is not None
    assert validated.taxonomy_id == "sw_level1_2021"
```

Add failures for subclasses, mutable containers, unsafe Unicode, unsupported labels, missing/unmapped/malformed codes, code-name mismatch, duplicate code/name/rank, mixed standard/unit, non-contiguous ranks, increasing weights, tied largest weights, incomplete scope, checksum mismatch, non-canonical JSON, and the empty production registry.

- [ ] **Step 2: Run the new tests and record red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_industry_taxonomy.py
```

Expected: collection fails because the module does not exist.

- [ ] **Step 3: Implement exact immutable records**

Implement:

```python
@dataclass(frozen=True)
class RecognizedIndustryTaxonomy:
    taxonomy_id: str
    version: str
    source_aliases: Tuple[str, ...]
    expected_code_pattern: str


@dataclass(frozen=True)
class IndustryTaxonomyMapping:
    metadata: RecognizedIndustryTaxonomy
    source_url: str
    published_at: date
    entries: Tuple[Tuple[str, str, Tuple[str, ...]], ...]
    canonical_json: str
    checksum: str


@dataclass(frozen=True)
class IndustryDistributionRow:
    classification_standard: str
    industry_code: str
    industry_name: str
    rank: int
    weight: Decimal
    unit: str


@dataclass(frozen=True)
class ValidatedIndustryDistribution:
    taxonomy_id: str
    mapping_checksum: str
    rows: Tuple[IndustryDistributionRow, ...]
```

Define recognized `sw_level1_2021` metadata, but keep:

```python
PRODUCTION_TAXONOMY_MAPPINGS: Tuple[IndustryTaxonomyMapping, ...] = ()
```

Canonical mapping JSON contains taxonomy id/version, official source URL, publication date, sorted code/name entries, and sorted aliases. Validate exact ASCII-escaped canonical JSON and its SHA-256.

- [ ] **Step 4: Implement whole-distribution validation**

```text
validate_industry_distribution(
    *,
    rows: Tuple[IndustryDistributionRow, ...],
    complete_scope: bool,
    mappings: Tuple[IndustryTaxonomyMapping, ...] = PRODUCTION_TAXONOMY_MAPPINGS,
) -> Optional[ValidatedIndustryDistribution]
```

Return `None` for unsupported/incomplete evidence. Raise `ValueError` only for malformed caller-owned records or an invalid registry. Require exact ranks `1..N`, non-increasing weights, a unique largest weight, one denominator, one taxonomy, exact code-name mapping, and safe unique normalized names/codes.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest -q tests/unit/test_industry_taxonomy.py
.venv/bin/ruff check src/kunjin/funds/industry_taxonomy.py \
  tests/unit/test_industry_taxonomy.py
git diff --check
git add src/kunjin/funds/industry_taxonomy.py \
  tests/unit/test_industry_taxonomy.py
git commit -m "feat: add controlled industry taxonomy boundary"
```

## Task 2: Gate Periodic-Report Industry Facts

**Files:**

- Modify: `src/kunjin/funds/risk/report_facts.py`
- Modify: `tests/unit/test_risk_report_facts.py`

- [ ] **Step 1: Write failing shutdown and synthetic-mapping tests**

Change the existing three-column industry tests so rank/name/weight tables emit no industry facts. Add a five-column table with classification standard and industry code; it also emits no production facts because production mappings are empty.

Inject a synthetic test mapping and assert the same table emits:

```python
{
    "current_largest_industry_name": "银行",
    "current_largest_industry_weight_percent": Decimal("12.50"),
    "current_industry_count": 2,
}
```

Add mixed-standard, missing/unmapped code, name mismatch, duplicate, partial scope, mixed denominator, unsafe Unicode, and tied-maximum failures.

- [ ] **Step 2: Run focused tests and record red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py -k industry
```

Expected: current free-text tables still publish industry facts.

- [ ] **Step 3: Replace free-text authorization**

Add exact standard/code header aliases and extend:

```text
extract_common_report_observations(
    *,
    text_blocks: Tuple[str, ...],
    tables: Tuple[ReportTable, ...],
    taxonomy_mappings: Tuple[IndustryTaxonomyMapping, ...] = (
        PRODUCTION_TAXONOMY_MAPPINGS
    ),
) -> Tuple[CurrentReportObservation, ...]
```

Only five-column complete industry tables become `IndustryDistributionRow`. Publish all three industry facts only after `validate_industry_distribution` succeeds. Delete the free-text unknown-industry grammar from the authorization path. Never infer code or standard.

- [ ] **Step 4: Verify non-industry compatibility and commit**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_report_facts.py
.venv/bin/ruff check src/kunjin/funds/risk/report_facts.py \
  tests/unit/test_risk_report_facts.py
git diff --check
git add src/kunjin/funds/risk/report_facts.py \
  tests/unit/test_risk_report_facts.py
git commit -m "fix: require controlled taxonomy for report industry facts"
```

## Task 3: Upgrade Parser Provenance And Close Time-Context Bypasses

**Files:**

- Modify: `src/kunjin/funds/risk/parsers.py`
- Modify: `src/kunjin/funds/risk/audit.py`
- Modify: `src/kunjin/funds/risk/legacy_doc.py`
- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_parsers.py`
- Modify: `tests/unit/test_risk_audit.py`
- Modify: `tests/unit/test_risk_legacy_doc.py`
- Modify: `tests/unit/test_risk_service.py`
- Modify: `tests/unit/test_risk_store.py`
- Modify: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing parser-v3 provenance tests**

Assert:

```python
assert PARSER_VERSION == "3"
assert native_parser_provenance().parser_version == "3"
legacy = legacy_parser_provenance(
    image_id="sha256:" + "a" * 64,
    architecture="linux/arm64",
    libreoffice_version="4:7.4.7-1+deb12u14",
    package_manifest_checksum="b" * 64,
)
assert legacy.parser_version == "3-docker-libreoffice-v1"
```

Add exact historical v2 native and legacy canonical payload fixtures. Prove they remain valid/readable but are not returned as active provenance. Add a store test proving the same artifact parsed under v2 and v3 creates distinct immutable parse results.

Add converter-status tests proving active v3 is `ready`, exact historical v2
records remain parseable as history, and any other parser version is rejected.

- [ ] **Step 2: Write failing current-evidence isolation tests**

Create a latest successful refresh containing only known v2 periodic records. Assert classification returns `official_document_unavailable`, not `classification_storage_failed`, until a v3 refresh exists. After publishing a v3 refresh, assert current evidence contains only v3 periodic parse-result ids.

- [ ] **Step 3: Write failing unsafe-time-context tests**

Add cases:

```python
(
    "历\u200b史数据",
    "往\u200b期资产配置",
    "prior\u200b period asset allocation",
)
```

Each emits no current observations. Exact current periods and generic `报告期末` controls still pass.

- [ ] **Step 4: Implement active v3 and historical v2 provenance**

Set report `PARSER_VERSION = "3"`. Active native and legacy provenance use `3` and `3-docker-libreoffice-v1`. In `audit.py`, validate exact known v2 and v3 payloads, while `native_parser_provenance()` and `legacy_parser_provenance()` return only v3.

The LibreOffice image identity, package manifest, export filter, and conversion contract remain unchanged; only parser/provenance version changes.

- [ ] **Step 5: Fail closed on inactive known provenance**

In `_current_provenance_checksums`, distinguish:

- malformed/unknown provenance -> `classification_storage_failed`;
- exact known historical v2 provenance -> `official_document_unavailable`;
- active v3 provenance -> accepted.

Do not reuse v2 free-text industry facts as current evidence.

- [ ] **Step 6: Reject unsafe time context before normalization**

Before period parsing, reject section names containing control, `Cf`, or default-ignorable characters. Do not strip and continue.

- [ ] **Step 7: Run complete verification**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_audit.py \
  tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_store.py \
  tests/unit/test_risk_service.py \
  tests/integration/test_cli.py
.venv/bin/ruff check \
  src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/audit.py \
  src/kunjin/funds/risk/legacy_doc.py \
  src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_audit.py \
  tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_store.py \
  tests/unit/test_risk_service.py \
  tests/integration/test_cli.py
git diff --check
```

Expected: v2 history is readable, active current evidence requires v3, and no parse identity conflict occurs.

- [ ] **Step 8: Commit**

```bash
git add src/kunjin/funds/risk/parsers.py \
  src/kunjin/funds/risk/audit.py \
  src/kunjin/funds/risk/legacy_doc.py \
  src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_parsers.py \
  tests/unit/test_risk_audit.py \
  tests/unit/test_risk_legacy_doc.py \
  tests/unit/test_risk_store.py \
  tests/unit/test_risk_service.py \
  tests/integration/test_cli.py
git commit -m "feat: activate report parser v3"
```

## Task 4: Disable Incomplete External Industry Synthesis

**Files:**

- Modify: `src/kunjin/funds/risk/service.py`
- Modify: `tests/unit/test_risk_service.py`
- Modify: `tests/unit/test_risk_engine.py`

- [ ] **Step 1: Write failing service tests**

Build a complete-looking `FundIndustryExposure` bundle with codes and names. Assert `_external_facts` emits none of:

```python
{
    "current_largest_industry_name",
    "current_largest_industry_weight_percent",
    "current_industry_count",
}
```

Assert source references/fingerprints remain present and non-industry holdings, profile, fee, size, and NAV behavior remains unchanged.

- [ ] **Step 2: Run focused tests and record red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_risk_service.py \
  -k 'industry and external'
```

Expected: service currently synthesizes largest-industry weight and count.

- [ ] **Step 3: Remove incomplete industry synthesis**

Delete the `_external_facts` block that derives industry count or largest weight from `FundIndustryExposure`. Do not delete bundle storage, research display, source references, or external fingerprints.

- [ ] **Step 4: Verify missing is not zero**

Add/adjust engine-facing service tests so absent industry facts cannot satisfy diversification or concentration gates. Preserve exact reason, conflict, and missing-evidence codes.

- [ ] **Step 5: Run and commit**

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_risk_service.py tests/unit/test_risk_engine.py
.venv/bin/ruff check src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_service.py tests/unit/test_risk_engine.py
git diff --check
git add src/kunjin/funds/risk/service.py \
  tests/unit/test_risk_service.py tests/unit/test_risk_engine.py
git commit -m "fix: disable unauthenticated industry synthesis"
```

## Task 5: Full Regression And Live Acceptance

**Files:**

- Modify only if a verified in-scope regression requires correction.
- Write acceptance output under `/private/tmp`; never commit downloaded or personal data.

- [ ] **Step 1: Run the complete local suite**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
git diff --check
```

Expected: full suite passes and historical manifest fixed-byte tests remain unchanged.

- [ ] **Step 2: Create an isolated acceptance environment and verify converter status**

Use the existing authenticated image with fresh private directories:

```bash
STAMP="$(date +%Y%m%d-%H%M%S)"
export KUNJIN_DATA_DIR="/private/tmp/kunjin-taxonomy-v3-data-$STAMP"
export KUNJIN_STATE_DIR="/private/tmp/kunjin-taxonomy-v3-state-$STAMP"
export PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache
export KUNJIN_LEGACY_DOC_IMAGE_ID='sha256:b0b1fcf864473ec8dbcad10fa49c29b0978ce89bb4ffe0829d4607a5f6cb19a9'
OUT="/private/tmp/kunjin-taxonomy-v3-results-$STAMP"
mkdir -p "$KUNJIN_DATA_DIR" "$KUNJIN_STATE_DIR" "$OUT"

.venv/bin/kunjin --json version >"$OUT/version.json" 2>"$OUT/version.stderr"
printf '%s\n' "$?" >"$OUT/version.exit"
.venv/bin/kunjin --json fund converter-status \
  >"$OUT/converter-status.json" 2>"$OUT/converter-status.stderr"
printf '%s\n' "$?" >"$OUT/converter-status.exit"
```

Expected: `status=ready`, parser version `3-docker-libreoffice-v1`, and the same image/conversion inputs except for the parser-version-bound provenance checksum.

- [ ] **Step 3: Run four-fund v3 synchronization**

```bash
run_acceptance() {
  local label="$1"
  shift
  "$@" >"$OUT/$label.json" 2>"$OUT/$label.stderr"
  local exit_code=$?
  printf '%s\n' "$exit_code" >"$OUT/$label.exit"
  printf '%s exit=%s\n' "$label" "$exit_code"
}

for code in 519706 164905 519718 519755; do
  run_acceptance "$code.profile" .venv/bin/kunjin --json sync fund-profile "$code"
  run_acceptance "$code.holdings" .venv/bin/kunjin --json sync fund-holdings "$code"
  run_acceptance "$code.documents" .venv/bin/kunjin --json sync fund-documents "$code"
  run_acceptance "$code.classify" .venv/bin/kunjin --json fund classify "$code"
  run_acceptance "$code.evidence" .venv/bin/kunjin --json fund classification-evidence "$code"
  run_acceptance "$code.history" .venv/bin/kunjin --json fund classification-history "$code"
done

printf 'acceptance_results=%s\n' "$OUT"
```

Expected:

- current periodic results use parser v3;
- free-text and HYPZ-derived industry current facts are absent;
- missing industry evidence is not zero;
- supported non-industry report facts remain available;
- classifications remain `research_only`; and
- `519718` may retain its separately known holdings gaps without becoming a taxonomy success.

Every `.exit` file must contain `0` except an explicitly accepted known
fact-availability failure. Inspect JSON evidence to confirm parser v3
provenance, absence of current industry facts, and preservation of non-industry
facts. Never infer success from the shell loop alone.

- [ ] **Step 4: Run independent reviews**

Dispatch one specification reviewer and one quality/financial-evidence reviewer over the complete Task 0-4 diff and acceptance summary. No P0/P1/P2 finding may remain.

- [ ] **Step 5: Record the objective financial assessment**

State explicitly:

- correctness and failure behavior improved;
- current industry coverage is intentionally zero until a complete pinned official mapping exists;
- D2 portfolio overlap and D3 purchase checks remain unimplemented;
- no buy direction or amount is authorized; and
- KunJin still cannot support a 90% beginner-help claim.

- [ ] **Step 6: Commit only a required acceptance correction**

If no code correction is required, create no empty commit. Otherwise stage only the verified in-scope files and use:

```bash
git commit -m "fix: complete controlled taxonomy acceptance"
```
