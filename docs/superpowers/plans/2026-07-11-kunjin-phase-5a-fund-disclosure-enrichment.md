# KunJin Fund Disclosure Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich any six-digit domestic public-fund code with traceable identity, share-class, manager-tenure, fee, size, benchmark, quarterly holding, industry-exposure, and announcement evidence.

**Architecture:** Add a focused `kunjin.funds` domain package and SQLite schema version 5. Eastmoney F10 HTTPS pages provide a clearly labeled tier-2 structured fallback, while official announcement and fund-company links are preserved as tier-1 evidence only after domain and publisher validation. Each section synchronizes independently, retains its last successful data, and exposes source documents, publication dates, retrieval times, conflicts, and explicit `not_disclosed` or `source_unavailable` states.

**Tech Stack:** Python 3.9 standard library, `urllib`, `html.parser`, SQLite, `unittest`, existing KunJin JSON CLI envelope.

---

## Scope And Plan Split

This plan implements the disclosure foundation for stage 2 of `docs/superpowers/specs/2026-07-11-kunjin-a-share-intelligence-and-personal-ledger-design.md`.

It intentionally does not implement peer ranking or portfolio overlap. Those depend on stable classifications, manager intervals, fee rules, holdings, and benchmark data, and belong in the follow-up plan:

```text
docs/superpowers/plans/2026-07-11-kunjin-phase-5b-peer-and-overlap-research.md
```

Phase 5A is independently useful: after it ships, asking about a fund code returns the actual manager, fees, size, benchmark, latest disclosed holdings, disclosure dates, announcement links, source tier, and unresolved evidence gaps.

## Source Contract

Use only HTTPS GET requests. Basic profile, manager history, and fees use the
audited F10 pages below; the remaining sections use the observed dynamic data
contracts so the stored source-document URL is the URL that actually returned
the normalized facts:

```text
https://fundf10.eastmoney.com/jbgk_CODE.html   basic profile and share-class links
https://fundf10.eastmoney.com/jjjl_CODE.html   manager history
https://fundf10.eastmoney.com/jjfl_CODE.html   fee rules
https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&mode=0&code=CODE
https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=CODE&topline=10&year=&month=
https://api.fund.eastmoney.com/f10/HYPZ/?fundCode=CODE&year=YEAR
https://api.fund.eastmoney.com/f10/JJGG?fundcode=CODE&pageIndex=1&pageSize=20&type=0
```

Replace `CODE` with a validated six-digit code and `YEAR` with the current year
in Asia/Shanghai. All returned facts remain tier 2. The F10 host uses
`source_name=eastmoney_f10`; the API host uses `source_name=eastmoney_api`.
Announcement API records use publisher `东方财富公告索引`; the title is never
used to guess an official publisher. The announcement category mapping is
1=发行运作, 2=分红, 3=定期报告, 4=人事调整, 5=基金销售, 6=其他.

The older `gmbd_CODE.html`, `ccmx_CODE.html`, `hytz_CODE.html`, and
`jjgg_CODE.html` pages are discovery/fallback pages only. They are not the
source URL for facts returned by the dynamic endpoints.

Quarterly holdings and industry responses do not provide a publication date.
Their parsed candidates may carry a missing date, but they are published to the
v5 store only after an already stored announcement title maps exactly to the
same report period. Quarter 4 and annual reports both map to December 31 of the
report year; matching is by report period, not by whichever announcement has a
later publication timestamp. Without an exact match, the section fails with
`missing_publication_date`; retrieval time is never substituted.

Announcement links may be promoted to `source_tier=1` only when all are true:

- HTTPS is used.
- The normalized publisher matches the fund manager or a regulator/exchange.
- The host is present in `src/kunjin/funds/official_domains.py`.
- The link is not an IP literal, localhost, a private address, or a user-info URL.

Initial regulator/exchange hosts:

```text
www.csrc.gov.cn
www.sse.com.cn
www.szse.cn
www.cninfo.com.cn
```

Fund-company domains are added only with a fixture showing the manager identity and official link source. Unknown domains remain tier 2 and visible; they are not silently trusted.

## File Map

Create:

- `src/kunjin/funds/__init__.py`: public fund-disclosure exports.
- `src/kunjin/funds/models.py`: immutable source, profile, manager, fee, size, benchmark, holding, industry, and announcement models.
- `src/kunjin/funds/html.py`: small DOM/table parser based on `html.parser`, with no business rules.
- `src/kunjin/funds/sources.py`: HTTPS text client, F10 URL construction, official-link classification, and response limits.
- `src/kunjin/funds/parsers.py`: page-specific normalization into fund models.
- `src/kunjin/funds/store.py`: transactional schema-v5 persistence and typed reads.
- `src/kunjin/funds/service.py`: independent section synchronization, freshness, conflicts, and partial failures.
- `src/kunjin/funds/research.py`: structured disclosure report and completeness rules.
- `src/kunjin/funds/official_domains.py`: audited official-domain registry.
- `tests/fixtures/funds/*.html`: synthetic or explicitly redacted page fixtures.
- `tests/unit/test_fund_models.py`
- `tests/unit/test_fund_html.py`
- `tests/unit/test_fund_sources.py`
- `tests/unit/test_fund_parsers.py`
- `tests/unit/test_fund_store.py`
- `tests/unit/test_fund_disclosure_service.py`
- `tests/unit/test_fund_disclosure_research.py`
- `tests/unit/test_schema_v5.py`

Modify:

- `src/kunjin/storage/schema.py`: schema version 5.
- `src/kunjin/storage/repository.py`: run version-5 migration only.
- `src/kunjin/cli.py`: fund-profile synchronization and read commands.
- `tests/integration/test_cli.py`: JSON contracts and partial failure.
- `README.md`: new commands and evidence limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: proactive enrichment workflow.
- `tests/test_smoke.py`: command discovery without network access.

Do not add AkShare, pandas, BeautifulSoup, browser automation, JavaScript execution, PDF text guessing, MySQL, or Redis in this plan.

### Task 1: Fund Disclosure Models And Provenance

**Files:**
- Create: `src/kunjin/funds/__init__.py`
- Create: `src/kunjin/funds/models.py`
- Create: `tests/unit/test_fund_models.py`

- [ ] **Step 1: Write failing validation tests**

Create tests for:

```python
source = SourceDocument(
    id=None,
    fund_code="519755",
    document_kind=DocumentKind.MANAGER_HISTORY,
    title="基金经理变更记录",
    url="https://fundf10.eastmoney.com/jjjl_519755.html",
    source_name="eastmoney_f10",
    source_tier=2,
    publisher="东方财富",
    published_at=None,
    retrieved_at=NOW,
    checksum="a" * 64,
)
source.validate()
```

Assert invalid fund codes, non-HTTPS URLs, tier values outside `1..3`, naive retrieval times, negative fees, holding weights outside `0..100`, and manager end dates before start dates are rejected.

- [ ] **Step 2: Run the model test and verify import failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_models -v
```

Expected: FAIL because `kunjin.funds.models` does not exist.

- [ ] **Step 3: Define exact enums and dataclasses**

Define:

```python
class DocumentKind(str, Enum):
    BASIC_PROFILE = "basic_profile"
    MANAGER_HISTORY = "manager_history"
    FEE_SCHEDULE = "fee_schedule"
    SIZE_HISTORY = "size_history"
    BENCHMARK = "benchmark"
    QUARTERLY_HOLDINGS = "quarterly_holdings"
    INDUSTRY_EXPOSURE = "industry_exposure"
    ANNOUNCEMENT = "announcement"


class FeeType(str, Enum):
    MANAGEMENT = "management"
    CUSTODY = "custody"
    SALES_SERVICE = "sales_service"
    SUBSCRIPTION = "subscription"
    REDEMPTION = "redemption"


class AssetType(str, Enum):
    STOCK = "stock"
    BOND = "bond"
    FUND = "fund"
    CASH = "cash"
    OTHER = "other"
```

Create frozen dataclasses:

```python
SourceDocument
FundIdentity
FundShareClass
FundManagerTenure
FundFeeRule
FundSizeObservation
FundBenchmark
FundHolding
FundIndustryExposure
FundAnnouncement
DisclosureBundle
```

Every normalized fact carries `source_document_id: Optional[int]`. Use this exact bundle contract so store, service, and research code agree:

```python
@dataclass(frozen=True)
class DisclosureBundle:
    fund_code: str
    identity: Optional[FundIdentity]
    share_classes: Tuple[FundShareClass, ...]
    manager_tenures: Tuple[FundManagerTenure, ...]
    fee_rules: Tuple[FundFeeRule, ...]
    sizes: Tuple[FundSizeObservation, ...]
    benchmarks: Tuple[FundBenchmark, ...]
    holdings: Tuple[FundHolding, ...]
    industry_exposure: Tuple[FundIndustryExposure, ...]
    announcements: Tuple[FundAnnouncement, ...]
    source_documents: Dict[int, SourceDocument]
    section_states: Dict[str, str]
    section_statuses: Dict[str, Dict[str, Optional[str]]]
    warnings: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()
```

`identity` is the current basic-profile identity selected through that section's source pointer. All tuple fields contain only rows belonging to the current source document for their section. `source_documents` contains every source referenced by those current facts; historical source versions remain queryable in SQLite but are not mixed into the current bundle.

Use `Decimal` for rates, amounts, shares, assets, and weights; `date` for report/effective dates; aware `datetime` for publication and retrieval timestamps.

- [ ] **Step 4: Run model tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_models -v
```

Expected: all model tests pass.

- [ ] **Step 5: Commit models**

```bash
git add src/kunjin/funds/__init__.py src/kunjin/funds/models.py tests/unit/test_fund_models.py
git commit -m "feat: add fund disclosure models"
```

### Task 2: Schema Version 5 And Typed Store

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `src/kunjin/funds/store.py`
- Create: `tests/unit/test_schema_v5.py`
- Create: `tests/unit/test_fund_store.py`

- [ ] **Step 1: Write failing migration and transaction tests**

Assert migration from a version-4 database preserves ledger transaction `1`, records versions `[1,2,3,4,5]`, and adds:

```text
fund_source_documents
fund_identities
fund_share_classes
fund_manager_tenures
fund_fee_rules
fund_sizes
fund_benchmarks
fund_holdings
fund_industry_exposure
fund_announcements
fund_section_syncs
```

Add a store test that calls `publish_section()` for manager history, then verifies a failed publication rolls back and preserves the previous successful section pointer and facts.

- [ ] **Step 2: Run tests and verify schema/store failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_schema_v5 tests.unit.test_fund_store -v
```

Expected: FAIL because schema version 5 and `FundDisclosureStore` do not exist.

- [ ] **Step 3: Add schema version 5**

Set `SCHEMA_VERSION = 5`. Record literal version `4` after `SCHEMA_V4`, then execute `SCHEMA_V5` and record `SCHEMA_VERSION`.

The schema must use these stable uniqueness rules:

```text
fund_source_documents: UNIQUE(fund_code, document_kind, url, checksum)
fund_identities: UNIQUE(fund_code, record_key, source_document_id)
fund_share_classes: UNIQUE(fund_code, record_key, source_document_id)
fund_manager_tenures: UNIQUE(fund_code, record_key, source_document_id)
fund_fee_rules: UNIQUE(fund_code, record_key, source_document_id)
fund_sizes: UNIQUE(fund_code, record_key, source_document_id)
fund_benchmarks: UNIQUE(fund_code, record_key, source_document_id)
fund_holdings: UNIQUE(fund_code, record_key, source_document_id)
fund_industry_exposure: UNIQUE(fund_code, record_key, source_document_id)
fund_announcements: UNIQUE(fund_code, url, source_document_id)
fund_section_syncs: PRIMARY KEY(fund_code, section)
```

The source-document `checksum` is a SHA-256 over the parser normalization-contract
version and the raw response checksum. A normalization change therefore creates a
new source-document version even when the upstream page bytes are unchanged, so
the current section pointer cannot mix facts produced by old and new parsers.

Implement `make_record_key(record) -> str` as SHA-256 over UTF-8 canonical JSON with `sort_keys=True`, `separators=(",", ":")`, dates and aware datetimes in ISO-8601, and `Decimal` values serialized as fixed-point strings. Include normalized business fields and exclude database IDs, `source_document_id`, retrieval timestamps, warnings, and conflicts. This makes optional dates idempotent without sentinel values.

All source-document foreign keys use `ON DELETE RESTRICT`. `fund_section_syncs` stores `current_source_document_id`. Publishing a newer section inserts a new source version and its facts, then atomically moves this pointer; it never deletes facts from older source documents.

- [ ] **Step 4: Implement `FundDisclosureStore`**

Public methods:

```text
FundDisclosureStore(repository)
publish_section(fund_code, section, source, records, state, warning=None) -> source_document_id
mark_section_failure(fund_code, section, error_code, error_message, attempted_at) -> None
load_bundle(fund_code) -> DisclosureBundle
section_status(fund_code) -> dict[str, dict[str, optional[str]]]
```

`publish_section()` validates every record before opening one SQLite transaction, inserts/reuses the source document, appends that source version's facts, moves only the current pointer for the exact fund and section, and accepts only `success` or `not_disclosed`. The `basic_profile` section may publish `FundIdentity`, `FundShareClass`, and `FundBenchmark` records because the benchmark has no independent F10 page. `not_disclosed` still stores the fetched source document and an empty record set. A failure in one section never deletes another section or an older version.

`mark_section_failure()` sets the latest attempt state to `source_unavailable`, updates attempt/error fields, and retains `current_source_document_id`, last-success timestamp, and last valid rows. Consequently `load_bundle()` may return stale rows together with `section_states[section] == "source_unavailable"`; consumers must use `section_statuses` to distinguish retained evidence from a successful current fetch.

- [ ] **Step 5: Run store and migration tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_schema_v5 tests.unit.test_fund_store tests.unit.test_schema_v4 tests.unit.test_ledger_store -v
```

Expected: all tests pass and version-4 ledger data remains unchanged.

- [ ] **Step 6: Commit schema and store**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py src/kunjin/funds/store.py tests/unit/test_schema_v5.py tests/unit/test_fund_store.py
git commit -m "feat: persist fund disclosures with provenance"
```

### Task 3: HTTPS Text Client And Official-Link Classification

**Files:**
- Create: `src/kunjin/funds/official_domains.py`
- Create: `src/kunjin/funds/sources.py`
- Create: `tests/unit/test_fund_sources.py`

- [ ] **Step 1: Write failing source-security tests**

Cover:

- all seven F10 URLs use HTTPS and a validated six-digit code;
- HTTP, IP-literal, localhost, user-info, private-address, unregistered hosts, and over-5-MiB responses are rejected;
- redirects are accepted only when the final URL remains HTTPS on `fundf10.eastmoney.com`;
- known regulator domains classify as tier 1;
- an unknown company domain remains tier 2;
- a fund-company domain becomes tier 1 only when registered with the exact normalized manager name.

- [ ] **Step 2: Run tests and verify source module failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_sources -v
```

Expected: FAIL because `kunjin.funds.sources` does not exist.

- [ ] **Step 3: Implement bounded HTTPS reads**

Define:

```python
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
FETCHABLE_HOSTS = frozenset({"fundf10.eastmoney.com"})


class FundSourceError(RuntimeError):
    code = "fund_source_error"


@dataclass(frozen=True)
class TextResponse:
    requested_url: str
    final_url: str
    text: str
    retrieved_at: datetime
    checksum: str
    content_type: str
```

`FundTextClient.fetch(url, referer) -> TextResponse` fetches only `FETCHABLE_HOSTS`. Official announcement links are classified and stored in this phase, not automatically fetched.

Read at most `MAX_RESPONSE_BYTES + 1`; reject oversized content before parsing. Decode from the HTTP charset when it is one of UTF-8, GB18030, or GBK; otherwise try UTF-8 then GB18030. Error messages never include response bodies.

Use `ipaddress.ip_address()` plus DNS result validation before the request and final URL validation after redirects. Reject loopback, private, link-local, multicast, reserved, and unspecified addresses. Reject any redirect whose final host differs from `fundf10.eastmoney.com`.

- [ ] **Step 4: Implement source registry and URL builder**

Provide:

```python
F10_PAGE_PATHS = {
    DocumentKind.BASIC_PROFILE: "jbgk_{code}.html",
    DocumentKind.MANAGER_HISTORY: "jjjl_{code}.html",
    DocumentKind.FEE_SCHEDULE: "jjfl_{code}.html",
    DocumentKind.SIZE_HISTORY: "gmbd_{code}.html",
    DocumentKind.QUARTERLY_HOLDINGS: "ccmx_{code}.html",
    DocumentKind.INDUSTRY_EXPOSURE: "hytz_{code}.html",
    DocumentKind.ANNOUNCEMENT: "jjgg_{code}.html",
}
```

`classify_source(url, publisher, manager_name)` returns tier 1 only under the rules in the Source Contract; otherwise tier 2.

- [ ] **Step 5: Run source tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_sources -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit source security**

```bash
git add src/kunjin/funds/official_domains.py src/kunjin/funds/sources.py tests/unit/test_fund_sources.py
git commit -m "feat: add audited fund disclosure sources"
```

### Task 4: Reusable HTML Table Parser

**Files:**
- Create: `src/kunjin/funds/html.py`
- Create: `tests/unit/test_fund_html.py`

- [ ] **Step 1: Write failing DOM/table tests**

Use synthetic HTML containing nested tags, entities, comments, repeated headers, `rowspan`, `colspan`, links, and whitespace. Assert output preserves:

```python
HtmlTable(
    caption="基金经理",
    headers=("姓名", "任职日期", "离任日期"),
    rows=(("张三", "2024-01-01", "至今"),),
    links=(("张三", "https://example.com/manager/1"),),
)
```

Malformed HTML must not execute scripts or fetch subresources; it either returns a partial DOM or raises `FundParseError` with no page body in the message.

- [ ] **Step 2: Run the test and verify import failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_html -v
```

Expected: FAIL because `kunjin.funds.html` does not exist.

- [ ] **Step 3: Implement structured HTML parsing**

Use `html.parser.HTMLParser(convert_charrefs=True)` to build only the nodes needed by fund pages: headings, tables, rows, cells, anchors, paragraphs, and definition lists. Ignore `script`, `style`, `iframe`, and comments entirely.

Expose:

```text
parse_tables(text, base_url) -> list[HtmlTable]
extract_labeled_values(text, base_url) -> dict[str, list[str]]
extract_links(text, base_url) -> list[HtmlLink]
```

Resolve links with `urllib.parse.urljoin`; do not fetch them.

- [ ] **Step 4: Run parser tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_html -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit HTML parser**

```bash
git add src/kunjin/funds/html.py tests/unit/test_fund_html.py
git commit -m "feat: parse structured fund disclosure pages"
```

### Task 5: Basic Profile, Share Classes, Benchmark, And Manager Tenure

**Files:**
- Create: `src/kunjin/funds/parsers.py`
- Create: `tests/fixtures/funds/basic_profile.html`
- Create: `tests/fixtures/funds/manager_history.html`
- Create: `tests/unit/test_fund_parsers.py`

- [ ] **Step 1: Write failing profile and manager tests**

The synthetic profile fixture must include code `519755`, a distinct fund name, active status, fund type, establishment date, manager/company names, benchmark text, and explicit sibling A/C share links.

Assert:

```python
bundle.identity.fund_code == "519755"
bundle.identity.status == "active"
bundle.share_classes[0].share_class == "A"
bundle.benchmarks[0].description != ""
bundle.manager_tenures[0].start_date == date(2024, 1, 1)
bundle.manager_tenures[0].end_date is None
```

Add manager-change cases with exact start/end dates, overlapping tenures for co-managers, and former-manager rows. Assert historical returns are not assigned in this parser.

- [ ] **Step 2: Run parser tests and verify missing implementation**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers -v
```

Expected: FAIL because page-specific parsers do not exist.

- [ ] **Step 3: Implement strict labeled-field parsing**

Define the normalized parser result before the page functions:

```python
@dataclass(frozen=True)
class ParsedSection:
    section: str
    source: SourceDocument
    records: Tuple[object, ...]
    state: str
    warnings: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()
```

Provide these functions:

```text
parse_basic_profile(response, fund_code) -> ParsedSection
parse_manager_history(response, fund_code) -> ParsedSection
```

Normalize only explicit labeled values. A/C relationship requires an explicit six-digit sibling link and a normalized base-name match; name similarity alone never creates a relationship.

Store benchmark text verbatim after whitespace normalization. Do not decompose benchmark weights until a later audited parser exists.

Return `not_disclosed` when the fetched page explicitly has no records, and raise `FundParseError(code="identity_conflict")` when page code/name conflicts with the requested code. Fetch failures never enter a parser; the service records them through `mark_section_failure()` as `source_unavailable`.

- [ ] **Step 4: Run parser tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers -v
```

Expected: all profile and manager tests pass.

- [ ] **Step 5: Commit profile parsers**

```bash
git add src/kunjin/funds/parsers.py tests/fixtures/funds/basic_profile.html tests/fixtures/funds/manager_history.html tests/unit/test_fund_parsers.py
git commit -m "feat: parse fund identity and manager tenure"
```

### Task 6: Fee Schedule And Size History

**Files:**
- Modify: `src/kunjin/funds/parsers.py`
- Create: `tests/fixtures/funds/fee_schedule.html`
- Create: `tests/fixtures/funds/size_history.html`
- Modify: `tests/unit/test_fund_parsers.py`

- [ ] **Step 1: Add failing tiered-fee and size tests**

Cover management, custody, sales-service, subscription, and redemption rules. Include percentage rules, fixed-amount rules, amount brackets, holding-day brackets, A/C differences, and an explicit “not charged” value.

Assert fee rules preserve:

```text
fee_type, rate, fixed_amount, amount_min, amount_max,
holding_days_min, holding_days_max, rule_order,
effective_from, raw_rule_text
```

Size tests cover report date, net assets, total shares, publication date, and unit normalization from `亿元` and `万份` into base yuan/shares.

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers.FundFeeParserTest tests.unit.test_fund_parsers.FundSizeParserTest -v
```

Expected: FAIL because fee and size parsers are absent.

- [ ] **Step 3: Implement fee and size normalization**

Provide:

```text
parse_fee_schedule(response, fund_code) -> ParsedSection
parse_size_history(response, fund_code) -> ParsedSection
```

Never collapse tiered fees into one number. A missing rate is `None`; explicit zero is `Decimal("0")`. Unknown units or ambiguous intervals raise `FundParseError(code="ambiguous_fee_rule")` rather than guessing.

- [ ] **Step 4: Run fee and size tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers -v
```

Expected: all parser tests pass.

- [ ] **Step 5: Commit fee and size parsing**

```bash
git add src/kunjin/funds/parsers.py tests/fixtures/funds/fee_schedule.html tests/fixtures/funds/size_history.html tests/unit/test_fund_parsers.py
git commit -m "feat: parse fund fees and size history"
```

### Task 7: Quarterly Holdings, Industry Exposure, And Announcements

**Files:**
- Modify: `src/kunjin/funds/parsers.py`
- Create: `tests/fixtures/funds/quarterly_holdings.html`
- Create: `tests/fixtures/funds/industry_exposure.html`
- Create: `tests/fixtures/funds/announcements.html`
- Modify: `tests/unit/test_fund_parsers.py`

- [ ] **Step 1: Add failing disclosure-date and completeness tests**

Holdings fixtures must include report period, publication date, rank, security code/name, asset type, weight, and explicit top-10 versus complete disclosure scope. Cover a page containing two quarters and ensure they do not merge.

Industry fixtures must include the classification standard and weights. Announcement fixtures must include title, category, publisher, publication time, and links with known and unknown domains.

Assert:

```python
holding.report_period == date(2026, 6, 30)
holding.published_at.date() > holding.report_period
holding.disclosure_scope == "top10"
announcement.source_tier == 1  # only for an audited official domain
```

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers.FundHoldingParserTest tests.unit.test_fund_parsers.FundAnnouncementParserTest -v
```

Expected: FAIL because holdings, industry, and announcement parsers are absent.

- [ ] **Step 3: Implement dated disclosure parsing**

Provide:

```text
parse_quarterly_holdings(response, fund_code) -> ParsedSection
parse_industry_exposure(response, fund_code) -> ParsedSection
parse_announcements(response, fund_code, manager_name) -> ParsedSection
```

Reject weights outside `0..100`. Preserve stock and bond codes as strings, including leading zeros. Never describe top-10 holdings as a complete portfolio. Store announcement page discovery as tier 2 and classify each linked document separately.

- [ ] **Step 4: Run all parser tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_parsers -v
```

Expected: all parser tests pass.

- [ ] **Step 5: Commit holdings and announcements**

```bash
git add src/kunjin/funds/parsers.py tests/fixtures/funds tests/unit/test_fund_parsers.py
git commit -m "feat: parse fund holdings and announcements"
```

### Task 8: Section-Isolated Synchronization And Freshness

**Files:**
- Create: `src/kunjin/funds/service.py`
- Create: `tests/unit/test_fund_disclosure_service.py`
- Modify: `src/kunjin/cli.py`

- [ ] **Step 1: Write failing partial-sync tests**

Use fake clients where manager and fee pages succeed, holdings times out, and announcements explicitly contain no records. Assert:

```python
result.sections["manager_history"].status == "success"
result.sections["fee_schedule"].status == "success"
result.sections["quarterly_holdings"].status == "source_unavailable"
result.sections["announcements"].status == "not_disclosed"
```

Verify a failed second holdings sync retains the first successful holdings and last-success timestamp. Verify identity conflict stops only basic-profile merging and is visible in `conflicts`.

- [ ] **Step 2: Run service tests and verify import failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_disclosure_service -v
```

Expected: FAIL because `FundDisclosureService` does not exist.

- [ ] **Step 3: Implement section registry and sync service**

Define the service types:

```python
@dataclass(frozen=True)
class SectionSpec:
    document_kind: DocumentKind
    parser_name: str


@dataclass(frozen=True)
class SectionSyncResult:
    section: str
    status: str
    records: int
    freshness: str
    error_code: Optional[str] = None


@dataclass(frozen=True)
class FundDisclosureSyncResult:
    fund_code: str
    sections: Dict[str, SectionSyncResult]
    conflicts: Tuple[str, ...]
```

Define the registry:

```python
SECTION_SPECS = {
    "basic_profile": SectionSpec(DocumentKind.BASIC_PROFILE, "parse_basic_profile"),
    "manager_history": SectionSpec(DocumentKind.MANAGER_HISTORY, "parse_manager_history"),
    "fee_schedule": SectionSpec(DocumentKind.FEE_SCHEDULE, "parse_fee_schedule"),
    "size_history": SectionSpec(DocumentKind.SIZE_HISTORY, "parse_size_history"),
    "quarterly_holdings": SectionSpec(DocumentKind.QUARTERLY_HOLDINGS, "parse_quarterly_holdings"),
    "industry_exposure": SectionSpec(DocumentKind.INDUSTRY_EXPOSURE, "parse_industry_exposure"),
    "announcements": SectionSpec(DocumentKind.ANNOUNCEMENT, "parse_announcements"),
}
```

`sync_profile(fund_code)` runs basic profile, manager, fees, size, and announcements. `sync_holdings(fund_code)` runs holdings and industry exposure. `sync_all(fund_code)` runs both sets and returns every section result; it never raises merely because one section failed.

Initial freshness rules:

```text
basic_profile: 30 days
manager_history: 7 days
fee_schedule: 30 days
size_history: 30 days
quarterly_holdings: until the next statutory report window plus 7 days
industry_exposure: same as holdings
announcements: 24 hours
```

For holdings and industry exposure, derive the newest expected report period from these conservative operational deadlines, all in Asia/Shanghai calendar dates:

```text
Q4 previous year: April 7
Q1 current year: May 7
Q2 current year: August 7
Q3 current year: November 7
```

Before a deadline, the preceding report period remains current. On and after a deadline, mark the section stale when its latest `report_period` is older than the newly expected period. These dates intentionally include a seven-calendar-day collection buffer and do not claim to be the legal deadline itself.

Store exact `as_of`, `last_success_at`, `last_attempt_at`, and `freshness` per section. Allowed freshness values are `fresh`, `stale`, `missing`, and `unknown`; `source_unavailable` is a sync state, not a freshness value.

- [ ] **Step 4: Run service and store tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_disclosure_service tests.unit.test_fund_store -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit synchronization**

```bash
git add src/kunjin/funds/service.py src/kunjin/cli.py tests/unit/test_fund_disclosure_service.py
git commit -m "feat: synchronize fund disclosure sections"
```

### Task 9: Structured Fund Disclosure Research

**Files:**
- Create: `src/kunjin/funds/research.py`
- Create: `tests/unit/test_fund_disclosure_research.py`

- [ ] **Step 1: Write failing completeness and conflict tests**

Build bundles for:

- complete active fund;
- current manager missing;
- only former managers present;
- fees present but redemption holding-period rule missing;
- latest holdings older than the latest statutory report;
- A/C sibling with different fee schedule;
- conflicting manager or benchmark values from tier 1 and tier 2.

Assert every output contains:

```text
evidence_level=verified_fact or insufficient_data
sources
publication/report dates
freshness
warnings
conflicts
missing_sections
```

- [ ] **Step 2: Run tests and verify research import failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_disclosure_research -v
```

Expected: FAIL because `build_disclosure_report` does not exist.

- [ ] **Step 3: Implement deterministic report assembly**

Provide:

```text
build_disclosure_report(bundle, as_of) -> dict[str, object]
```

Rules:

- Tier 1 wins as the displayed primary value; tier-2 disagreement remains in `conflicts`.
- A current manager requires `start_date <= as_of.date()` and `end_date is None or end_date >= as_of.date()`.
- Holdings always display report period, publication date, disclosure scope, and age.
- Fees display each tier and condition separately.
- Missing implementation is never phrased as missing data; only actual section state controls `source_unavailable`, `not_disclosed`, or `insufficient_data`.
- No weighted universal score is produced.

- [ ] **Step 4: Run research tests**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_fund_disclosure_research -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit research assembly**

```bash
git add src/kunjin/funds/research.py tests/unit/test_fund_disclosure_research.py
git commit -m "feat: build sourced fund disclosure reports"
```

### Task 10: CLI, Daily Sync, Skill, And Live Verification

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Add failing CLI contract tests**

Add commands:

```text
kunjin sync fund-profile CODE
kunjin sync fund-holdings CODE
kunjin fund profile CODE
kunjin fund fees CODE
kunjin fund holdings CODE [--period YYYY-MM-DD]
kunjin fund announcements CODE
```

JSON responses retain the existing envelope and add section-level `sources`, `freshness`, `warnings`, `conflicts`, and `errors` inside `data`.

Assert invalid codes return `invalid_fund_code`; a partial sync exits zero when at least one requested section succeeds and returns failed sections in `data`, while a total failure exits one.

- [ ] **Step 2: Run integration tests and verify command failures**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.integration.test_cli -v
```

Expected: new commands fail before CLI implementation.

- [ ] **Step 3: Wire context and commands**

Add `fund_disclosure_service` and `fund_disclosure_store` to `ApplicationContext`. Existing `sync fund CODE` remains formal-NAV synchronization for backward compatibility.

Extend `sync daily` so each held fund independently runs:

```text
sync fund CODE
sync fund-profile CODE when stale
sync fund-holdings CODE when stale or a new report window is due
```

One fund or section failure must not block other funds, portfolio sync, or market sync.

- [ ] **Step 4: Update README and Skill workflow**

The Skill must proactively run `sync fund-profile CODE` and `sync fund-holdings CODE` before answering manager, fee, size, benchmark, holdings, or announcement questions when data is stale. It must preserve report dates, sources, conflicts, and missing evidence.

Keep peer comparison and overlap under `Unsupported Requests` until phase 5B ships.

- [ ] **Step 5: Run full automated verification**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
git diff --check
```

Expected: all tests pass, compileall and diff check are silent, and pip reports no broken requirements.

- [ ] **Step 6: Run live read-only smoke tests**

First use one held fund, `519755`:

```bash
.venv/bin/kunjin --json sync fund-profile 519755
.venv/bin/kunjin --json sync fund-holdings 519755
.venv/bin/kunjin --json fund profile 519755
.venv/bin/kunjin --json fund fees 519755
.venv/bin/kunjin --json fund holdings 519755
.venv/bin/kunjin --json fund announcements 519755
```

Verify code/name identity, exact manager intervals, fee tiers, source URLs, and latest holding report/publication dates against at least one official or clearly labeled fallback page.

Then run `sync fund-profile` for all currently held codes. A source failure must retain successful sections and report the exact stale/missing state without inventing values.

- [ ] **Step 7: Run security and credential scans**

Run:

```bash
rg -n "Authorization:|Request-Sign:|never-print-this|token=[^[]" src tests README.md integrations
rg -n "http://|localhost|127\\.0\\.0\\.1|169\\.254\\." src/kunjin/funds tests/fixtures/funds
```

Expected: only intentional negative security fixtures or synthetic redaction assertions match.

- [ ] **Step 8: Commit and push phase 5A**

```bash
git add README.md integrations/codex/kunjin-fund/SKILL.md src/kunjin/funds src/kunjin/cli.py src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests
git commit -m "feat: add sourced fund disclosure research"
git push origin main
```

If `.git/index` remains unavailable to the Codex process, report the verified commit command for the user to run rather than bypassing the sandbox.

## Phase 5A Acceptance Checklist

- [ ] Any valid fund code can synchronize identity, current and former managers, fees, size, benchmark, holdings, industry exposure, and announcements independently.
- [ ] A/C relationships require explicit source evidence.
- [ ] Manager tenure uses exact dates and never inherits a predecessor's performance.
- [ ] Tiered fees and holding-period conditions remain separate rules.
- [ ] Holdings always expose report period, publication date, and disclosure scope.
- [ ] Tier-1 facts override tier-2 display values while conflicts remain visible.
- [ ] Source failures preserve last successful data and show staleness.
- [ ] Daily sync failures remain isolated by fund and section.
- [ ] No peer ranking, overlap claim, universal score, or trading instruction is introduced in phase 5A.
- [ ] Full tests, compile checks, dependency checks, live smoke tests, and security scans pass or have an explicitly reported external blocker.
