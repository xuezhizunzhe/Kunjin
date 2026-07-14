# KunJin Phase D1 Real Fund Risk Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn authenticated official fund documents and existing sourced disclosures into deterministic, fail-closed product-family, risk-bucket, portfolio-role, and evidence-status classifications without producing a buy recommendation or amount.

**Architecture:** Add an isolated `kunjin.funds.risk` domain. Bounded network and parser adapters produce immutable public-product facts; a pure Policy V1 engine classifies those facts; Schema V10 stores immutable artifacts, facts, policies, and fingerprint-bound results, while Schema V11 preserves the validated landing URL separately from the final attachment URL; service and CLI layers expose amount-free `research_only` views. Phase B/C remain mandatory only for directional requests, while D1 fact-only classification remains available when suitability is blocked.

**Tech Stack:** Python 3.9+, frozen dataclasses, `Decimal`, timezone-aware `datetime`, canonical JSON and SHA-256, SQLite transactions/triggers, `urllib`, `pypdf`, bounded standard-library OOXML/ZIP/XML parsing, existing KunJin HTML parser and CLI envelopes, `unittest`, Ruff.

---

## Scope And File Map

Create:

- `src/kunjin/funds/risk/__init__.py`: public D1 exports.
- `src/kunjin/funds/risk/models.py`: strict enums and immutable evidence/result records.
- `src/kunjin/funds/risk/policy.py`: fixed Policy V1, canonical JSON, validation, checksum, and freshness deadlines.
- `src/kunjin/funds/risk/documents.py`: official-document discovery, HTTPS retrieval, content validation, resource limits, and managed artifact writing.
- `src/kunjin/funds/risk/parsers.py`: bounded HTML/PDF extraction into source-traceable normalized facts.
- `src/kunjin/funds/risk/engine.py`: pure product-family, risk-bucket, role, conflict, and downgrade rules.
- `src/kunjin/funds/risk/store.py`: immutable artifact, fact, policy, and classification persistence.
- `src/kunjin/funds/risk/service.py`: sync, freshness, evidence assembly, fingerprinting, classification, and authenticated current/history reads.
- `src/kunjin/funds/risk/research.py`: beginner-safe JSON report construction.
- `tests/unit/test_risk_models_policy.py`: enums, validation, policy checksum, and freshness boundaries.
- `tests/unit/test_risk_documents.py`: official-domain, redirect, MIME, size, artifact, and hostile-response tests.
- `tests/unit/test_risk_parsers.py`: traceability, limits, ambiguity, and deterministic parsing tests.
- `tests/unit/test_risk_engine.py`: full product/risk/role matrix and monotonic invariants.
- `tests/unit/test_schema_v10.py`: exact migration, constraints, triggers, collision, and rollback tests.
- `tests/unit/test_risk_store.py`: idempotency, immutable readback, history, binding, and concurrency tests.
- `tests/unit/test_risk_service.py`: orchestration, freshness, evidence switching, and fail-closed tests.
- `tests/unit/test_risk_research.py`: amount-free output and wording boundaries.
- `tests/fixtures/funds/risk/`: synthetic public official HTML/PDF, index, mandate, report, and hostile fixtures.
- `docs/audits/2026-07-13-kunjin-phase-d1-independent-review.md`: independent financial and beginner-workflow audit written after verification.

Modify:

- `pyproject.toml`: add bounded PDF parsing dependency.
- `setup.py`: keep legacy editable-install dependencies aligned with `pyproject.toml`.
- `src/kunjin/paths.py`: add private managed fund-document directory.
- `src/kunjin/funds/models.py`: extend public document kinds only; do not add D1 policy decisions here.
- `src/kunjin/funds/official_domains.py`: add only audited manager/index-provider domains required by real acceptance.
- `src/kunjin/storage/schema.py`: add Schema V10 immutable D1 objects and the
  bounded Schema V11 landing-URL migration.
- `src/kunjin/storage/repository.py`: register V10/V11 and validate the exact
  D1-owned object set.
- `src/kunjin/cli.py`: wire D1 services and six amount-free JSON commands.
- `src/kunjin/logging.py`: redact managed artifact paths, raw bodies, and parser internals.
- `tests/unit/test_paths.py`: verify private document-directory permissions.
- `tests/unit/test_fund_models.py`: verify extended document kinds.
- `tests/unit/test_fund_sources.py`: verify newly audited official domains.
- `tests/unit/test_logging.py`: verify D1 technical-detail redaction.
- `tests/unit/test_schema_v7.py`, `test_schema_v8.py`, `test_schema_v9.py`: update current-version assertions while preserving exact historical fixtures.
- `tests/integration/test_cli.py`: D1 command envelopes, exit codes, persistence, and privacy.
- `tests/test_smoke.py`: packaged parser/help and Skill contract coverage.
- `README.md`: D1 commands, evidence states, limitations, and D2/D3 boundary.
- `integrations/codex/kunjin-fund/SKILL.md`: D1 fact-only and directional-gate workflow.
- `integrations/codex/kunjin-fund/agents/openai.yaml`: classification evidence and `research_only` prompt contract.
- `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`: copy from the repository only after tests pass, then verify byte identity.

Never place real personal amounts, private goal/obligation names, managed paths, raw response bodies, or parser exception details in source, fixtures, documentation, logs, commits, or audits. D1 must not call, decrypt, or depend on the personal profile. Unsupported products are successful factual outcomes; unavailable evidence for a supported product is `critical_evidence_missing`, not `unsupported_product_family`.

## Post-Implementation Live Acceptance Record

The fresh isolated v5 acceptance on 2026-07-13 exercised profile, holdings,
official-document sync, classification, evidence, and history for four real
public funds. Twenty-three of twenty-four commands exited successfully. The
only command failure was the `519718` holdings sync; classification remained a
successful fail-closed factual outcome.

- `519706`: `unclassified / unclassified / not_eligible`, evidence
  `unclassified`, with `classification_unclassified`,
  `critical_evidence_missing`, and `official_scope_missing`. The official fund
  documents establish an equity index fund but do not authenticate the index
  methodology and broad-index concentration scope required by Policy V1.
- `164905`: `sector_theme / concentrated_equity / satellite_only`, evidence
  `verified`, with `classification_verified`. Tier-1 product-summary and
  prospectus evidence explicitly establish the equity asset class, index
  strategy, and new-energy sector mandate. This is product classification only,
  not a suitability result or purchase direction.
- `519718`: `ordinary_bond / unclassified / not_eligible`, evidence `partial`,
  with credit-quality, duration, leverage, issuer, stock, convertible,
  exchangeable, derivative, foreign-exposure, and current-observation evidence
  missing. The holdings source did not provide a disclosure shape that could be
  safely normalized, so the engine did not infer defensive quality from the
  fund name or legal bond type.
- `519755`: `equity_mixed / concentrated_equity / not_eligible`, evidence
  `partial`, because current complete asset allocation, holdings, industry
  concentration, industry count, largest-security, and top-ten evidence remain
  unavailable.

The acceptance demonstrates one positive verified sector/theme path and three
distinct fail-closed paths. It does not validate broad-index core eligibility,
high-quality fixed-income eligibility, D2 overlap/correlation controls, D3
product selection, Phase E pre-purchase checks, or a 90% beginner-help claim.

### Task 1: Add Strict Risk Models And Fixed Policy V1

**Files:**
- Create: `src/kunjin/funds/risk/__init__.py`
- Create: `src/kunjin/funds/risk/models.py`
- Create: `src/kunjin/funds/risk/policy.py`
- Create: `tests/unit/test_risk_models_policy.py`
- Modify: `src/kunjin/funds/models.py`
- Modify: `tests/unit/test_fund_models.py`

- [ ] **Step 1: Write failing enum, model, policy, and freshness tests**

Require exact enum values, frozen dataclasses, six-digit fund codes, lowercase SHA-256 digests, positive evidence IDs, bounded excerpts, timezone-aware UTC timestamps, unique sorted codes/IDs, no personal keys, canonical Policy V1 bytes, and report deadlines:

```python
class RiskPolicyTest(unittest.TestCase):
    def test_policy_v1_is_fixed_and_canonical(self) -> None:
        policy = ClassificationPolicyV1()
        policy.validate()
        self.assertEqual(policy.version, "1")
        self.assertEqual(policy.high_quality_duration_years_max, D("5"))
        self.assertEqual(policy.high_quality_credit_floor_percent, D("80"))
        self.assertEqual(policy.broad_index_constituents_min, 100)
        self.assertEqual(policy.broad_index_top_ten_percent_max, D("40"))
        self.assertEqual(policy.active_top_ten_percent_max, D("50"))
        self.assertEqual(
            hashlib.sha256(policy.canonical_json()).hexdigest(),
            CLASSIFICATION_POLICY_V1_CHECKSUM,
        )

    def test_q1_deadline_is_thirty_days_after_period_end(self) -> None:
        self.assertEqual(
            ClassificationPolicyV1().periodic_report_deadline(date(2026, 3, 31)),
            date(2026, 4, 30),
        )

    def test_result_has_no_recommendation_or_amount_fields(self) -> None:
        names = {field.name for field in dataclasses.fields(FundRiskClassification)}
        for forbidden in ("amount", "target", "recommended", "buy", "sell"):
            self.assertNotIn(forbidden, names)
```

- [ ] **Step 2: Run the focused tests and verify the missing-module failure**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_models_policy -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'kunjin.funds.risk'`.

- [ ] **Step 3: Implement the exact public enum and record contract**

Use these stable values and shapes:

```python
class ProductFamily(str, Enum):
    MONEY_MARKET = "money_market"
    SHORT_BOND = "short_bond"
    INTERMEDIATE_BOND = "intermediate_bond"
    ORDINARY_BOND = "ordinary_bond"
    LONG_BOND = "long_bond"
    CREDIT_BOND = "credit_bond"
    CONVERTIBLE_BOND = "convertible_bond"
    FIXED_INCOME_PLUS = "fixed_income_plus"
    BOND_MIXED = "bond_mixed"
    BROAD_INDEX = "broad_index"
    INDEX_ENHANCED = "index_enhanced"
    SECTOR_THEME = "sector_theme"
    ACTIVE_EQUITY = "active_equity"
    EQUITY_MIXED = "equity_mixed"
    QDII_BROAD_EQUITY = "qdii_broad_equity"
    QDII_SECTOR_THEME = "qdii_sector_theme"
    UNSUPPORTED = "unsupported"
    UNCLASSIFIED = "unclassified"

class RiskBucket(str, Enum):
    CASH_LIKE_CANDIDATE = "cash_like_candidate"
    HIGH_QUALITY_FIXED_INCOME = "high_quality_fixed_income"
    DIVERSIFIED_EQUITY = "diversified_equity"
    CONCENTRATED_EQUITY = "concentrated_equity"
    HYBRID_RISK = "hybrid_risk"
    UNCLASSIFIED = "unclassified"

class PortfolioRole(str, Enum):
    CASH_MANAGEMENT_CANDIDATE = "cash_management_candidate"
    CORE_ELIGIBLE = "core_eligible"
    ACTIVE_DIVERSIFIER_ELIGIBLE = "active_diversifier_eligible"
    SATELLITE_ONLY = "satellite_only"
    NOT_ELIGIBLE = "not_eligible"

class EvidenceStatus(str, Enum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    CONFLICTED = "conflicted"
    STALE = "stale"
    UNCLASSIFIED = "unclassified"

class FactConfidence(str, Enum):
    EXACT = "exact"
    BOUNDED_RANGE = "bounded_range"
    PRESENT = "present"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"

@dataclass(frozen=True)
class MandateFact:
    fund_code: str
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    source_document_id: int
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    confidence_state: FactConfidence
    parser_version: str
    fact_fingerprint: str

@dataclass(frozen=True)
class FundRiskClassification:
    fund_code: str
    policy_version: str
    input_fingerprint: str
    product_family: ProductFamily
    risk_bucket: RiskBucket
    portfolio_role: PortfolioRole
    evidence_status: EvidenceStatus
    evidence_tags: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    missing_evidence: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    evidence_document_ids: Tuple[int, ...]
    evidence_fact_ids: Tuple[int, ...]
    freshness: Tuple[EvidenceFreshness, ...]
    classified_at: datetime
    valid_until: datetime
    capability: str = "research_only"
```

Extend `DocumentKind` with `FUND_CONTRACT`, `PROSPECTUS`, `PROSPECTUS_UPDATE`, `PRODUCT_SUMMARY`, `ANNUAL_REPORT`, `SEMIANNUAL_REPORT`, `QUARTERLY_REPORT`, `INDEX_METHODOLOGY`, and `CLASSIFICATION_ANNOUNCEMENT`. Keep existing F10 mappings unchanged so those kinds cannot accidentally route through Eastmoney fetch helpers.

- [ ] **Step 4: Implement Policy V1 as immutable scalar/tuple data**

Set all thresholds from the confirmed spec. `periodic_report_deadline()` returns report-period end plus 30 days for March/September, September 13 for June 30 plus 75 days, and April 15 of the following year for December 31 plus 105 days. Reject unsupported period ends. Canonical JSON must use sorted keys, ASCII output, fixed decimal strings, and no runtime clock/state.

The fixed numeric policy is:

- High-quality fixed income: stock 0%, convertible/exchangeable 0%, duration at
  most 5 years or legal weighted-average maturity at most 397 days, sovereign /
  policy-bank / cash / deposit / AAA at least 80%, below AA+ 0%, unrated
  non-sovereign 0%, gross leverage at most 120%, and one non-sovereign issuer at
  most 10% of fund assets.
- Broad-index core: at least 100 constituents, largest constituent at most 10%,
  top ten at most 40%, largest industry at most 35%, and at least five
  industries.
- Sector/theme: legal non-cash theme floor at least 80%, or complete current
  evidence showing at least 50% of fund assets in one industry.
- Active diversified: largest security at most 10%, top ten stocks at most 50%
  of fund assets, largest industry at most 40%, and at least five industries.
- Legal documents and product summaries receive a one-year review checkpoint;
  index methodology receives a one-year review checkpoint and immediate
  invalidation on a sourced methodology change.

Define stable financial codes for `classification_verified`,
`classification_partial`, `classification_conflicted`, `classification_stale`,
`classification_unclassified`, `unsupported_product_family`,
`critical_evidence_missing`, `critical_evidence_stale`,
`official_scope_missing`, `index_methodology_missing`,
`holdings_evidence_missing`, `industry_evidence_missing`,
`duration_evidence_missing`, `credit_quality_evidence_missing`,
`leverage_evidence_missing`, and `liquidity_evidence_missing`. Define conflict
codes for `name_conflicts_with_formal_scope`,
`platform_category_conflicts_with_formal_scope`,
`benchmark_conflicts_with_mandate`, `holdings_conflict_with_mandate`,
`industry_conflict_with_broad_index`,
`nav_behavior_conflicts_with_declared_scope`,
`convertible_exposure_conflict`, `equity_exposure_conflict`,
`duration_conflict`, `credit_quality_conflict`, `leverage_conflict`, and
`source_version_conflict`. Technical error codes remain separate from these
financial results.

Evidence tags are separate factual labels, never recommendation codes. They use
sorted unique stable strings and may include `interest_rate_bond`,
`policy_bank_bond`, `credit_exposure`, `convertible_exposure`,
`hong_kong_equity`, `foreign_currency`, and sourced geographic tags.

- [ ] **Step 5: Run the model and policy tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_models_policy tests.unit.test_fund_models -v`

Expected: PASS with one hard-coded Policy V1 checksum.

- [ ] **Step 6: Commit the isolated model/policy change**

```bash
git add src/kunjin/funds/models.py src/kunjin/funds/risk tests/unit/test_fund_models.py tests/unit/test_risk_models_policy.py
git commit -m "feat: add fund risk policy and models"
```

### Task 2: Build The Bounded Official Document Chain

**Files:**
- Modify: `pyproject.toml`
- Modify: `setup.py`
- Modify: `src/kunjin/paths.py`
- Modify: `src/kunjin/funds/official_domains.py`
- Create: `src/kunjin/funds/risk/documents.py`
- Create: `tests/unit/test_risk_documents.py`
- Modify: `tests/unit/test_paths.py`
- Modify: `tests/unit/test_fund_sources.py`
- Create: `tests/fixtures/funds/risk/official-index.html`
- Create: `tests/fixtures/funds/risk/authentication-shell.html`
- Create: `tests/fixtures/funds/risk/mime-mismatch.bin`

- [ ] **Step 1: Add failing path, discovery, domain, redirect, resource, and artifact tests**

```python
def test_tier_one_announcement_discovers_only_validated_official_document(self) -> None:
    candidate = discover_candidate(official_announcement(), manager_name=MANAGER)
    self.assertEqual(candidate.document_kind, DocumentKind.QUARTERLY_REPORT)
    self.assertEqual(candidate.source_tier, 1)

def test_official_index_paginates_until_all_required_document_kinds_are_found(self) -> None:
    discovery = OfficialDocumentDiscovery(client=PagedIndexClient(total_pages=3))
    result = discovery.discover("519755", manager_name=MANAGER)
    self.assertEqual(index_client.requested_pages, [1, 2, 3])
    self.assertIn(DocumentKind.FUND_CONTRACT, {item.document_kind for item in result})

def test_redirect_to_unregistered_host_is_rejected(self) -> None:
    client = OfficialDocumentClient(opener=RedirectingOpener("https://evil.example/a.pdf"))
    with self.assertRaisesRegex(OfficialDocumentError, "redirect"):
        client.fetch(candidate())

def test_managed_artifact_is_private_and_checksum_named(self) -> None:
    artifact = client.fetch(candidate())
    self.assertEqual(stat.S_IMODE(artifact.managed_path.stat().st_mode), 0o600)
    self.assertEqual(artifact.managed_path.name, artifact.sha256 + ".pdf")
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_documents tests.unit.test_paths tests.unit.test_fund_sources -v`

Expected: FAIL because the document client, private path, and new domain entries do not exist.

- [ ] **Step 3: Add PDF dependency and private artifact directory**

Add `pypdf>=5,<6` to both `pyproject.toml` and legacy `setup.py` dependencies. Add `RuntimePaths.fund_documents` returning `database.parent / "fund-documents"`; `ensure()` creates it with mode `0700`. Artifact files are opened with exclusive creation and mode `0600`; temporary partial files live in the same directory and are removed on failure.

- [ ] **Step 4: Implement discovery and safe retrieval**

Use explicit limits and types:

```python
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 1500
MAX_EXTRACTED_CHARACTERS = 20_000_000
MAX_FACTS = 10_000
MAX_EXCERPT_CHARACTERS = 4096

@dataclass(frozen=True)
class OfficialDocumentCandidate:
    fund_code: str
    document_kind: DocumentKind
    title: str
    url: str
    publisher: str
    published_at: Optional[datetime]
    source_tier: int

@dataclass(frozen=True)
class RetrievedArtifact:
    candidate: OfficialDocumentCandidate
    final_url: str
    retrieved_at: datetime
    content_type: str
    byte_size: int
    sha256: str
    managed_path: Path

class OfficialDocumentError(RuntimeError):
    code = "official_document_invalid"
```

Add a fixed `OfficialSourceRegistration` registry containing the exact manager or index-provider identity, accepted hosts, and paginated document-index URL templates. `OfficialDocumentDiscovery` must query every registered page until the source reports the final page or the fixed page/item cap is reached; it must not rely on the existing Eastmoney first-page announcement feed. Existing `FundAnnouncement` links may contribute candidates only when their final publisher/domain pair independently passes tier-1 validation. `pdf.dfcfw.com` and other platform mirrors remain tier 2 and cannot satisfy D1.

Determine kind from normalized titles using explicit contract/prospectus/product-summary/annual/semiannual/quarterly/index-methodology patterns; ambiguous titles are ignored with `document_kind_ambiguous`. Fetching must reuse the current public-DNS/HTTPS principles, use a custom redirect handler that validates every hop before following it, forbid credentials/non-443 ports, limit bytes while streaming, accept only detected PDF, HTML, or validated macro-free OOXML/DOCX, reject raw archives/legacy OLE DOC/executables/authentication shells/script-only HTML, and never return raw response bodies in errors. An audited registration may map fixed manager aliases to its canonical identity and may resolve one same-host HTTPS attachment from a disclosure landing page. The index parser must scope candidates to the target product identity declared by the official page, preserve adjacent publication dates, ignore unrelated navigation links before validating candidate URLs, and exclude related products or explicit non-target share classes.

- [ ] **Step 5: Add only acceptance-required audited domains**

For each real acceptance candidate, add the exact official manager or index-provider hostname, publisher identity, and document-index URL template. Add a separate immutable index-provider mapping instead of pretending an index company is a fund manager. Tests must prove that a valid domain plus wrong publisher remains tier 2 and cannot satisfy D1, pagination cannot loop forever, and a platform mirror cannot be promoted by title alone.

- [ ] **Step 6: Run document tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_documents tests.unit.test_paths tests.unit.test_fund_sources -v`

Expected: PASS for safe redirects, DNS rejection, MIME detection, byte limits, authentication-shell rejection, private permissions, checksum identity, and idempotent artifact reuse.

- [ ] **Step 7: Commit the document boundary**

```bash
git add pyproject.toml setup.py src/kunjin/paths.py src/kunjin/funds/official_domains.py src/kunjin/funds/risk/documents.py tests/unit/test_paths.py tests/unit/test_fund_sources.py tests/unit/test_risk_documents.py tests/fixtures/funds/risk
git commit -m "feat: add bounded official fund documents"
```

### Task 3: Parse Official HTML And PDF Into Traceable Facts

**Files:**
- Create: `src/kunjin/funds/risk/parsers.py`
- Create: `tests/unit/test_risk_parsers.py`
- Create: `tests/fixtures/funds/risk/pure-bond-prospectus.html`
- Create: `tests/fixtures/funds/risk/broad-index-methodology.html`
- Create: `tests/fixtures/funds/risk/sector-fund-summary.html`
- Create: `tests/fixtures/funds/risk/current-report.pdf`
- Create: `tests/fixtures/funds/risk/conflicting-clauses.html`

- [ ] **Step 1: Write failing exact, bounded, absent, ambiguous, traceability, and resource tests**

```python
def test_stock_ceiling_keeps_exact_source_location(self) -> None:
    result = parse_artifact(pure_bond_artifact())
    fact = one(result.facts, "stock_exposure_max_percent")
    self.assertEqual(fact.normalized_value, "0")
    self.assertEqual(fact.confidence_state, FactConfidence.EXACT)
    self.assertIn("不投资于股票", fact.source_excerpt)
    self.assertEqual(fact.section_name, "投资范围")

def test_conflicting_clauses_are_not_silently_collapsed(self) -> None:
    result = parse_artifact(conflicting_artifact())
    self.assertIn("duplicate_conflicting_clause", result.conflicts)
    self.assertEqual(
        [f.confidence_state for f in result.facts if f.fact_kind == "stock_exposure_max_percent"],
        [FactConfidence.AMBIGUOUS, FactConfidence.AMBIGUOUS],
    )
```

- [ ] **Step 2: Run parser tests and verify missing implementation failures**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_parsers -v`

Expected: FAIL because `parse_artifact` is absent.

- [ ] **Step 3: Implement deterministic extraction without AI inference**

Expose:

```python
PARSER_VERSION = "1"

@dataclass(frozen=True)
class ParsedRiskDocument:
    artifact: RetrievedArtifact
    facts: Tuple[ParsedMandateFact, ...]
    warnings: Tuple[str, ...]
    conflicts: Tuple[str, ...]

def parse_artifact(artifact: RetrievedArtifact) -> ParsedRiskDocument:
    """Parse bounded official HTML/PDF/DOCX text into normalized, traceable facts."""
```

`ParsedMandateFact` is a frozen parser-boundary record with `fact_kind`,
immutable `normalized_value`, `unit`, `page_number`, `section_name`,
`source_excerpt`, effective dates, and `confidence_state`. It deliberately has
no database ID. Task 6 binds `fund_code`, the persisted artifact ID,
`PARSER_VERSION`, and the canonical fact fingerprint in the same transaction
that publishes the successfully parsed artifact.

HTML parsing uses the existing safe text utilities. PDF parsing uses `PdfReader(strict=True)`, checks page count before extraction, rejects encrypted PDFs and embedded files, and counts cumulative characters. DOCX parsing uses only bounded standard-library ZIP/XML reads, validates the WordprocessingML content type, rejects macros, embedded objects, external relationships, encrypted/duplicate/path-traversing members, DTDs and entities, and extracts paragraph/table text without executing or resolving content. Normalize only explicitly sourced legal product type, objective, exposure bounds, tracked index/benchmark, duration/maturity, ratings, leverage, issuer limits, derivatives, liquidity restrictions, asset allocation, holdings, and industry facts. Percentages use `Decimal`; durations use years or days with explicit units. `present` and `absent` are permitted only for literal clauses. No regex match may turn silence into `absent`. Duplicate equivalent facts deduplicate by fingerprint; different values remain visible and ambiguous.

- [ ] **Step 4: Generate the small deterministic PDF fixture using pypdf/reportlab test tooling**

Use the workspace document runtime or an existing local PDF generator to produce a one-page fixture containing public synthetic clauses. Store no personal data. Verify extraction text before committing the fixture.

- [ ] **Step 5: Run parser tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_parsers -v`

Expected: PASS for HTML/PDF parity, page/section excerpts, effective dates, ambiguous conflicts, exact fingerprints, parser determinism, page/character/fact/excerpt limits, malformed/encrypted PDF rejection, and no partial-success publication.

- [ ] **Step 6: Commit deterministic parsing**

```bash
git add src/kunjin/funds/risk/parsers.py tests/unit/test_risk_parsers.py tests/fixtures/funds/risk
git commit -m "feat: parse official fund risk evidence"
```

### Task 4: Implement The Pure Fail-Closed Classification Engine

**Files:**
- Create: `src/kunjin/funds/risk/engine.py`
- Create: `tests/unit/test_risk_engine.py`

- [ ] **Step 1: Write the failing classification matrix**

Create table-driven cases for all supported and unsupported families, then explicit boundary tests for every Policy V1 threshold. Include strict bond admission, broad-index identity versus core eligibility, index-enhanced limitations, sector/theme paths, active-equity concentration, QDII tags, source conflicts, missing/stale evidence, and NAV conflict-only behavior.

```python
def test_verified_broad_index_can_still_fail_core_eligibility(self) -> None:
    result = classify(evidence(broad_index=True, largest_industry="35.01"), POLICY, NOW)
    self.assertEqual(result.product_family, ProductFamily.BROAD_INDEX)
    self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)
    self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)

def test_missing_one_bond_gate_never_passes_high_quality(self) -> None:
    result = classify(evidence(strict_bond=True, issuer_concentration=None), POLICY, NOW)
    self.assertNotEqual(result.risk_bucket, RiskBucket.HIGH_QUALITY_FIXED_INCOME)
    self.assertIn("issuer_concentration_evidence_missing", result.missing_evidence)

def test_nav_behavior_cannot_promote_classification(self) -> None:
    baseline = classify(evidence(product_family_unknown=True), POLICY, NOW)
    stable_nav = classify(evidence(product_family_unknown=True, stable_nav=True), POLICY, NOW)
    self.assertEqual(stable_nav.risk_bucket, baseline.risk_bucket)
```

- [ ] **Step 2: Run engine tests and verify they fail**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_engine -v`

Expected: FAIL because the pure engine is absent.

- [ ] **Step 3: Implement evidence assembly input and deterministic classification**

```python
@dataclass(frozen=True)
class ClassificationEvidence:
    fund_code: str
    legal_facts: Tuple[MandateFact, ...]
    benchmark_facts: Tuple[MandateFact, ...]
    report_facts: Tuple[MandateFact, ...]
    existing_disclosure_facts: Tuple[MandateFact, ...]
    nav_conflicts: Tuple[str, ...]
    freshness: Tuple[EvidenceFreshness, ...]
    document_ids: Tuple[int, ...]
    fact_ids: Tuple[int, ...]

def classify_fund(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
    classified_at: datetime,
) -> FundRiskClassification:
    """Return a deterministic research-only classification with no I/O."""
```

Classification order is: validate evidence and freshness; identify unsupported family; determine legal product family; add observed-risk disqualifiers; determine risk bucket; determine role; resolve critical conflicts; calculate final evidence status and `valid_until`. More risk may downgrade a bucket or role, never improve it. A supported product with missing documents is `UNCLASSIFIED` plus `critical_evidence_missing`. Tier-1 conflicts block `VERIFIED`; name/platform conflicts remain warnings. `cash_like_candidate`, `high_quality_fixed_income`, and `core_eligible` are classification states only.

Populate evidence tags only from explicit facts. Every QDII family retains
`foreign_currency`; Hong Kong exposure retains `hong_kong_equity`; credit and
convertible exposure retain their corresponding factual tags. Tags do not
change the conservative ordering and cannot promote a result.

- [ ] **Step 4: Add property-style monotonic tests without a new dependency**

Iterate deterministic boundary grids for stock, convertible, duration, credit, leverage, issuer, constituent, security, and industry values. Assert that removing evidence, making evidence stale, adding risk, or lowering source tier never improves `(evidence_status, risk_bucket, portfolio_role)` under an explicit conservative ordering.

- [ ] **Step 5: Run engine tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_engine -v`

Expected: PASS for all product-family paths, exact threshold boundaries, conflicts, missing/stale evidence, and monotonic invariants.

- [ ] **Step 6: Commit the pure engine**

```bash
git add src/kunjin/funds/risk/engine.py tests/unit/test_risk_engine.py
git commit -m "feat: classify real fund risk evidence"
```

### Task 5: Add Atomic Schema V10

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v10.py`
- Modify: `tests/unit/test_schema_v7.py`
- Modify: `tests/unit/test_schema_v8.py`
- Modify: `tests/unit/test_schema_v9.py`

- [ ] **Step 1: Write failing exact-schema and migration tests**

Copy the strong V9 test structure and require exactly four tables, all indexes/triggers, versions 1 through 10, upgrade from every prior version, rollback on syntax/name collision/false markers, concurrent first migration, immutable rows, positive IDs, canonical UTC, lowercase digest, enum/JSON constraints, foreign-key restrictions, and exact object SQL.

- [ ] **Step 2: Run Schema V10 tests and verify current-version failure**

Run: `.venv/bin/python -m unittest tests.unit.test_schema_v10 -v`

Expected: FAIL because `SCHEMA_VERSION` is 9 and V10 objects do not exist.

- [ ] **Step 3: Add exact V10 tables and immutable triggers**

Add `fund_document_artifacts`, `fund_mandate_facts`, `fund_classification_policy_versions`, and `fund_risk_classifications` with the confirmed columns, including `input_manifest_json` as the canonical public object required to recompute `input_fingerprint`, and `evidence_tags_json` as a sorted unique JSON array of public stable codes. Use V9-style strong SQLite checks: explicit text types, NUL rejection, canonical lowercase 64-character SHA-256, valid JSON arrays/objects, positive IDs, canonical `+00:00` timestamps, `valid_until > classified_at`, and `ON DELETE RESTRICT`. Add `no_replace`, `no_update`, and `no_delete` triggers to every immutable table. Do not use `IF NOT EXISTS` in V10.

- [ ] **Step 4: Register V10 and exact owned-object validation**

In `Repository._migration_definitions()` append `(10, SCHEMA_V10)`. Preserve `WAL`, `BEGIN IMMEDIATE`, statement iteration, before/after validation, rollback, and mode `0600`. Add an explicit D1-owned table/object set; do not use the broad `fund_*` prefix because older schemas already own fund tables. Reject extra, missing, renamed, or altered D1 tables/indexes/triggers whenever marker 10 exists.

- [ ] **Step 5: Update older current-version assertions only**

Tests that migrate to the current database must expect versions 1 through 10. Helpers constructing exact historical V7/V8/V9 databases must remain historical and must not precreate V10.

- [ ] **Step 6: Run all schema tests**

Run: `.venv/bin/python -m unittest tests.unit.test_schema_v2 tests.unit.test_schema_v4 tests.unit.test_schema_v5 tests.unit.test_schema_v6 tests.unit.test_schema_v7 tests.unit.test_schema_v8 tests.unit.test_schema_v9 tests.unit.test_schema_v10 -v`

Expected: PASS, including atomic rollback and concurrent migration cases.

- [ ] **Step 7: Commit Schema V10**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v7.py tests/unit/test_schema_v8.py tests/unit/test_schema_v9.py tests/unit/test_schema_v10.py
git commit -m "feat: add immutable fund risk schema"
```

### Task 6: Persist And Authenticate Artifacts, Facts, Policies, And Classifications

**Files:**
- Create: `src/kunjin/funds/risk/store.py`
- Create: `tests/unit/test_risk_store.py`

- [ ] **Step 1: Write failing store idempotency, conflict, history, and concurrency tests**

Require exact same input to return the same row; same fingerprint with different metadata to fail; two writers to serialize to one row; insert readback before commit; rollback on any valid-but-different readback; absolute-time then ID ordering; immutable-trigger enforcement; and evidence IDs belonging to the same fund.

```python
def test_same_classification_fingerprint_is_idempotent(self) -> None:
    first = store.save_classification(sample_classification())
    second = store.save_classification(sample_classification())
    self.assertEqual(first.id, second.id)

def test_fact_from_another_fund_is_rejected(self) -> None:
    classification = replace(sample_classification(), evidence_fact_ids=(foreign_fact_id,))
    with self.assertRaisesRegex(RiskStoreError, "evidence fund"):
        store.save_classification(classification)
```

- [ ] **Step 2: Run store tests and verify missing implementation failures**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_store -v`

Expected: FAIL because `FundRiskStore` is absent.

- [ ] **Step 3: Implement transactionally strict persistence**

Implement `FundRiskStore.publish_parsed_document`, `save_failed_artifact`,
`ensure_policy`, `save_classification`, `current_classification`,
`classification_history`, and `classification_evidence`. Define frozen
`StoredDocumentArtifact`, `StoredFact`, `StoredClassificationPolicy`,
`StoredClassification`, and `ClassificationEvidenceRecord` records in
`store.py`; each record mirrors its table columns exactly and validates positive
database IDs before return. `publish_parsed_document` inserts or authenticates
the final successful artifact row, obtains its positive ID, converts every
`ParsedMandateFact` into a stored `MandateFact`, inserts the facts, and reads all
rows back before one commit. `save_failed_artifact` may persist final failed
parse metadata but never publishes facts.

Validate the complete parsed draft before opening a write transaction. Use `BEGIN IMMEDIATE`, query by the unique key/fingerprint, compare every field, insert only when absent, bind the newly known artifact ID into facts, read back before commit, and compare again. Artifact uniqueness is `(fund_code, document_kind, url, sha256)`; fact uniqueness is `(source_document_id, parser_version, fact_fingerprint)`; policy uniqueness is version plus exact canonical bytes/checksum; classification uniqueness is input fingerprint plus exact bound contents. Never catch a uniqueness error and blindly return an existing row. Successful artifact and fact publication is one transaction, so no successful artifact can exist without its complete fact set.

- [ ] **Step 4: Authenticate current/history reads**

Recompute policy checksum, canonical fact fingerprints, evidence ownership,
artifact checksums from stored metadata, and the classification input fingerprint
from `input_manifest_json` before returning. The manifest preserves legal /
benchmark / report / existing-disclosure fact groups, external evidence
fingerprints, NAV conflict binding, policy checksum, IDs, and canonical time.
Invalid historical rows raise `classification_storage_failed`; they are never
softened into a financial `unclassified` result.

- [ ] **Step 5: Run store and schema tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_store tests.unit.test_schema_v10 -v`

Expected: PASS for idempotency, concurrency, readback, ownership, ordering, tamper detection, and immutability.

- [ ] **Step 6: Commit persistence**

```bash
git add src/kunjin/funds/risk/store.py tests/unit/test_risk_store.py
git commit -m "feat: persist fund risk classifications"
```

### Task 7: Orchestrate Sync, Freshness, Fingerprinting, And Research Views

**Files:**
- Create: `src/kunjin/funds/risk/service.py`
- Create: `src/kunjin/funds/risk/research.py`
- Create: `tests/unit/test_risk_service.py`
- Create: `tests/unit/test_risk_research.py`

- [ ] **Step 1: Write failing service and report tests**

Cover partial document synchronization, no successful parse on technical failure, supported-but-missing evidence, unsupported factual results, report deadlines, superseded legal documents, index-methodology expiry, evidence switch before insert/after commit/before return, share-class binding, NAV conflict input, public error codes, and output privacy.

```python
def test_phase_b_block_does_not_prevent_fact_only_classification(self) -> None:
    result = service.classify("519755")
    self.assertEqual(result.capability, "research_only")
    self.assertEqual(profile_service.calls, [])
    self.assertEqual(suitability_service.calls, [])

def test_evidence_change_before_return_never_returns_false_current_result(self) -> None:
    service = service_with_switch(point="before_return")
    with self.assertRaisesRegex(RiskServiceError, "evidence_changed"):
        service.classify("519755")
```

- [ ] **Step 2: Run service tests and verify missing implementation failures**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_service tests.unit.test_risk_research -v`

Expected: FAIL because the service and report builder are absent.

- [ ] **Step 3: Implement document synchronization**

`sync_documents(fund_code)` loads the current existing disclosure bundle, discovers official candidates, fetches and parses each candidate independently, then calls `publish_parsed_document` so the final artifact metadata and bound facts become visible atomically. Technical failures receive stable codes; a parse failure may retain an immutable failed-artifact audit row but cannot create successful facts. A partial sync may preserve successful documents while clearly returning failed sections.

- [ ] **Step 4: Implement evidence assembly and fingerprinting**

`classify(fund_code)` loads current identity/share class, official facts, benchmark, holdings, industry, size, fees, and formal-NAV conflict evidence; calculates evidence-specific freshness; canonicalizes all bound IDs/fingerprints/policy checksum/time; runs the pure engine; persists a technically successful financial result; then reloads and reauthenticates all bindings before return. The service constructor has no profile, suitability, allocation, ledger, or Yangjibao dependency.

- [ ] **Step 5: Implement beginner-safe research reports**

Return separate sections for `verified_facts`, `classification`, `evidence_status`, `missing_evidence`, `conflicts`, `freshness`, `sources`, and `limitations`. Include exact stable codes and dates. Always state `capability=research_only`, `classification_is_not_recommendation`, `cash_like_is_not_protected_cash`, and `d2_d3_not_evaluated`. Never include a target, amount, trade direction, universal score, managed path, raw body, or parser exception.

- [ ] **Step 6: Run service and research tests**

Run: `.venv/bin/python -m unittest tests.unit.test_risk_service tests.unit.test_risk_research -v`

Expected: PASS for synchronization isolation, freshness downgrade, binding races, fact-only behavior, and amount-free output.

- [ ] **Step 7: Commit orchestration and views**

```bash
git add src/kunjin/funds/risk/service.py src/kunjin/funds/risk/research.py tests/unit/test_risk_service.py tests/unit/test_risk_research.py
git commit -m "feat: orchestrate fund risk research"
```

### Task 8: Wire CLI Contracts And Technical Failure Handling

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `src/kunjin/logging.py`
- Modify: `tests/unit/test_logging.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser, envelope, exit-code, and privacy tests**

Add cases for:

```text
kunjin --json sync fund-documents CODE
kunjin --json fund classify CODE
kunjin --json fund classification CODE
kunjin --json fund classification-history CODE
kunjin --json fund classification-evidence CODE
kunjin --json fund classification-policy
```

Financial `partial`, `stale`, `conflicted`, `unsupported`, and `unclassified` results exit 0 and preserve exact codes. Invalid arguments and technical failures exit nonzero with redacted public messages. Verify that no response contains personal amount keys, private names, `managed_path`, local paths, raw response bodies, or parser exception text.

- [ ] **Step 2: Run CLI tests and verify parser failures**

Run: `.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke tests.unit.test_logging -v`

Expected: FAIL because the D1 commands and context wiring are absent.

- [ ] **Step 3: Wire context and parsers**

Add optional `fund_risk_store` and `fund_risk_service` fields to `ApplicationContext`. `build_context()` creates them from `RuntimePaths.fund_documents`, `FundDisclosureStore`, the existing research repository, and fixed Policy V1. Add the six parser entries exactly as specified. Reuse `_validate_fund_code` for all code-bearing commands.

- [ ] **Step 4: Add command dispatch and stable technical errors**

`sync.fund-documents` returns per-document status. `fund.classify` runs a current calculation. `fund.classification` and history/evidence are read-only authenticated views. `fund.classification-policy` returns canonical public thresholds and checksum. Map only these technical errors to nonzero exits: `official_document_unavailable`, `official_document_invalid`, `official_document_resource_limit`, `official_document_parse_failed`, `classification_policy_unavailable`, `classification_calculation_failed`, and `classification_storage_failed`.

- [ ] **Step 5: Extend logging redaction**

Add D1 managed artifact paths, raw parser bodies, embedded-file metadata, local exception chains, and response-body fields to redaction rules. Public source URLs, titles, bounded excerpts, checksums, and dates remain visible because they are audit evidence.

- [ ] **Step 6: Run CLI and privacy tests**

Run: `.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke tests.unit.test_logging -v`

Expected: PASS for all envelopes, financial-versus-technical exit behavior, help packaging, and redaction.

- [ ] **Step 7: Commit CLI integration**

```bash
git add src/kunjin/cli.py src/kunjin/logging.py tests/unit/test_logging.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose fund risk classification commands"
```

### Task 9: Update User Documentation And Codex Skill

**Files:**
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `integrations/codex/kunjin-fund/agents/openai.yaml`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing documentation-contract tests**

Require all six commands, all evidence states, `research_only`, unsupported-versus-missing distinction, `cash_like_candidate` versus `protected_cash`, `core_eligible` versus recommendation, Phase B/C directional gate order, and D2/D3 unimplemented boundary. Require the Skill to prohibit assuming a real fund belongs to a Phase C layer and to preserve every D1 reason/conflict/missing-evidence code.

- [ ] **Step 2: Run the smoke contract and verify missing text**

Run: `.venv/bin/python -m unittest tests.test_smoke.SmokeTest.test_phase_d1_readme_and_skill_contracts_are_packaged -v`

Expected: FAIL until README and Skill contain the complete D1 contract.

- [ ] **Step 3: Document D1 without overstating capability**

Explain that D1 classifies product evidence only. A `verified` result is not suitability, allocation, a buy signal, or a 90% beginner-help claim. State that official-domain coverage is audited and finite; missing a manager/index-provider adapter can leave a supported fund unclassified. Include correction instructions for stale/missing documents without suggesting a purchase.

- [ ] **Step 4: Update Skill workflow**

For fact-only classification, allow document sync and classification without Phase B/C. For buy/hold/add/reduce/sell/rebalance/position-size requests, keep the existing JSON suitability and allocation gates first, then require current D1 evidence; stop on non-verified D1 states and state that D2/D3 are still absent. Preserve the existing ban on non-JSON suitability/allocation execution by Codex.

- [ ] **Step 5: Run documentation tests**

Run: `.venv/bin/python -m unittest tests.test_smoke -v`

Expected: PASS with no weakened Phase A-C privacy or gate contract.

- [ ] **Step 6: Commit repository documentation and Skill**

```bash
git add README.md integrations/codex/kunjin-fund/SKILL.md integrations/codex/kunjin-fund/agents/openai.yaml tests/test_smoke.py
git commit -m "docs: add phase d1 fund classification workflow"
```

### Task 10: Run Full Verification And Real Amount-Free Acceptance

**Files:**
- Modify only if verification exposes a defect in files already in this plan.
- Create: `docs/audits/2026-07-13-kunjin-phase-d1-independent-review.md`

Before the final verification, preserve the 2026-07-13 live regression evidence:

- Eastmoney tier-2 profile/holding synchronization succeeded for three active
  samples, proving that public networking and the existing disclosure adapter
  were operational.
- Official document discovery remained empty because Eastmoney returned the
  audited manager short name while the registration accepted only the legal
  full name, and because the registered `index.shtml` URL was a product page
  rather than the real `sxxpl.shtml` disclosure page.
- The real disclosure page contains unrelated unsafe navigation links, adjacent
  publication-date spans, related-product documents, and same-host landing
  pages whose attachments may be DOCX or legacy DOC despite a `.doc` suffix.
- Fix these production-reachability defects without promoting platform mirrors,
  fund names, legacy DOC, or unsupported binary content into official facts.

Add failing tests first for the audited alias, disclosure URL, product scoping,
date extraction, irrelevant unsafe links, one-hop attachment resolution, DOCX
MIME/content detection, OOXML security limits, and parser traceability. Then
rerun the isolated public acceptance before changing the audit score.

The post-regression repair also requires Schema V11 to retain both the official
landing page and final attachment URL. Real saved fund001 pages must discover
only the requested product/share class. A real DOCX without an embedded fund
code may pass only when its exact legal fund name matches the validated official
announcement title; a present but mismatched code still fails closed. The saved
519755 official prospectus must classify through the real parser, store, service,
and engine as `equity_mixed / concentrated_equity / partial / not_eligible`
until current complete holdings and industry evidence exist.

- [ ] **Step 1: Install the declared dependency and run focused D1 tests**

Run:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m unittest \
  tests.unit.test_risk_models_policy \
  tests.unit.test_risk_documents \
  tests.unit.test_risk_parsers \
  tests.unit.test_risk_engine \
  tests.unit.test_schema_v10 \
  tests.unit.test_schema_v11 \
  tests.unit.test_risk_store \
  tests.unit.test_risk_service \
  tests.unit.test_risk_research -v
```

Expected: PASS.

- [ ] **Step 2: Run format, lint, and the complete regression suite**

Run:

```bash
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

Expected: all commands exit 0; Phase A-C, ledger, fund, peer, overlap, and scheduling behavior remains unchanged.

- [ ] **Step 3: Run amount-free real acceptance**

Use only JSON commands. Select public candidates covering: one broad index, one sector/theme fund, one bond/fixed-income fund, and one unsupported or evidence-incomplete fund. For each supported candidate run `sync fund-profile`, `sync fund-holdings`, `sync fund-documents`, and `fund classify`; then read `fund classification-evidence`. Also verify one stale/superseded document and one name/platform conflict. Do not run non-JSON `suitability assess` or `allocation ranges`, and do not expose personal values.

- [ ] **Step 4: Record acceptance evidence without overstating coverage**

Record public fund codes, product families, report/publication dates, official source URLs/domains, evidence states, stable codes, and whether each strict gate passed. Record the count and percentage of attempted common funds that remained partial/unclassified. Do not describe an unclassified candidate as an implementation failure unless the CLI returned a technical error.

- [ ] **Step 5: Perform a fresh independent financial review**

The reviewer must lead with findings, challenge the fixed-income and concentration thresholds, identify false-confidence wording, give D2 and D3 zero new credit, and rescore the same 100-point beginner workflow from evidence. The audit must explicitly answer whether KunJin now provides 90%+ beginner purchasing help; it must not preserve the previous 52/100 score without evidence.

- [ ] **Step 6: Sync the installed Skill and verify byte identity**

Run:

```bash
cp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp -s integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
```

Expected: `cmp` exits 0. This external copy requires the owner's approved filesystem permission if the sandbox blocks it.

- [ ] **Step 7: Verify the final worktree and commit the audit**

Run:

```bash
git diff --check
git status --short
git add docs/audits/2026-07-13-kunjin-phase-d1-independent-review.md
git commit -m "docs: audit phase d1 fund classification"
```

Expected: only intentional D1 files are present before the final commit; no generated cache, real personal data, or managed document appears in Git.

## Completion Conditions

D1 is complete only when all ten tasks pass, real evidence remains source-traceable, supported-but-incomplete products fail closed, unsupported products remain factual research outcomes, the installed and repository Skills are byte-identical, and the independent review has rescored the beginner workflow. Completion does not authorize D2 portfolio limits, D3 pre-purchase checks, a target allocation, a purchase amount, a trade direction, or a claim of 90% beginner help.
