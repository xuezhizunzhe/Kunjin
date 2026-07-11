# KunJin Peer And Overlap Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add versioned peer groups, aligned formal-NAV comparison, and disclosed-holdings overlap for held, user-supplied, and a bounded set of automatically discovered domestic public funds.

**Architecture:** Add a focused `kunjin.funds.peers` package. A tier-2 static Eastmoney fund directory supplies candidate codes only; a deterministic sample is then revalidated through existing phase 5A disclosures and formal NAV. Pure classifiers and analytics calculate peer membership, aligned metrics, and overlap, while a schema-v6 store atomically publishes peer-group versions and records deterministic comparison runs.

**Tech Stack:** Python 3.9 standard library, `urllib`, SHA-256, immutable dataclasses, `Decimal`, SQLite, `unittest`, existing KunJin services and JSON CLI envelope.

---

## Scope And Non-Negotiable Rules

Implement the approved design:

```text
docs/superpowers/specs/2026-07-11-kunjin-phase-5b-peer-and-overlap-design.md
```

The first version supports:

- `sync fund-peers CODE [--candidate CODE ...]`
- `fund peers CODE`
- `fund compare CODE CODE [CODE ...]`
- `portfolio overlap`
- At most 20 current peer members.
- A 40-code deterministic discovery-validation pool.
- Aligned 90-day, 365-day, and current-manager-team windows.
- Pairwise `top10_disclosed_overlap` and portfolio look-through overlap.
- Versioned rules, data dates, coverage, sources, warnings, and errors.

Do not add pandas, NumPy, AkShare, browser automation, JavaScript execution,
MySQL, Redis, a universal composite score, platform ranking values, or an
automatic trading operation.

## Candidate Source Contract

Use this exact candidate-directory URL:

```text
https://fund.eastmoney.com/js/fundcode_search.js
```

The observed response contract is a UTF-8/BOM JavaScript assignment containing
one JSON array:

```javascript
var r = [
  ["000001", "HXCZHH", "华夏成长混合", "混合型-灵活", "HUAXIACHENGZHANGHUNHE"],
  ["000003", "ZHKZZZQA", "中海可转债债券A", "债券型-混合二级", "ZHONGHAIKEZHUANZHAIZHAIQUANA"]
];
```

Parse only the JSON value after the exact `var r =` assignment. Do not evaluate
JavaScript. Reject extra executable text after the terminating semicolon except
whitespace. Each accepted row must contain exactly five strings, a six-digit
fund code, a non-empty name, and a non-empty directory type. Structurally valid
rows whose directory type is temporarily empty are skipped because they cannot
be classified; malformed shapes, invalid codes, and empty names still reject the
directory.

The directory is tier 2 and is used only for code/name/type discovery. Never
persist or consume a platform return, rank, star rating, recommendation, or
score. Extend the phase 5A fetch allowlist only with `fund.eastmoney.com`; keep
same-host HTTPS redirects, DNS public-address checks, response bounds, and
credential-free GET requests.

Candidate selection constants:

```python
PEER_MEMBER_LIMIT = 20
DISCOVERY_VALIDATION_LIMIT = 40
PEER_RULE_VERSION = "1"
PEER_CALCULATION_VERSION = "1"
PEER_NAV_MAX_PAGES = 20
```

Selection order:

1. Anchor fund.
2. Unique user-supplied candidates in command order.
3. Unique currently held funds in fund-code order.
4. Discovered funds whose directory type exactly equals the anchor directory
   type, ordered by SHA-256 of `f"{anchor_code}:{candidate_code}"`.

Exclude the anchor, duplicates, and directory names containing `（后端）` or
`(后端)` before sampling. Validate no more than 40 discovered funds. Strict peer
classification still applies to every candidate. Stop publishing members after
20 accepted funds, but preserve bounded rejection/error counts in the sync
result.

When one code enters by multiple routes, use membership precedence:

```text
anchor > user_supplied > held > discovered
```

## File Map

Create:

- `src/kunjin/funds/peers/__init__.py`: public peer package exports.
- `src/kunjin/funds/peers/models.py`: immutable directory, classification,
  group, metric, and overlap models.
- `src/kunjin/funds/peers/sources.py`: static directory URL and safe parser.
- `src/kunjin/funds/peers/classification.py`: pure peer classification and
  candidate ordering.
- `src/kunjin/funds/peers/analytics.py`: aligned NAV metrics and overlap.
- `src/kunjin/funds/peers/store.py`: schema-v6 group and comparison persistence.
- `src/kunjin/funds/peers/service.py`: bounded synchronization orchestration.
- `src/kunjin/funds/peers/research.py`: stable beginner-oriented JSON reports.
- `tests/fixtures/funds/fundcode_search.js`: synthetic candidate directory.
- `tests/unit/test_peer_models.py`
- `tests/unit/test_peer_sources.py`
- `tests/unit/test_peer_classification.py`
- `tests/unit/test_peer_analytics.py`
- `tests/unit/test_peer_store.py`
- `tests/unit/test_peer_service.py`
- `tests/unit/test_peer_research.py`
- `tests/unit/test_schema_v6.py`

Modify:

- `src/kunjin/funds/sources.py`: allow audited candidate host.
- `src/kunjin/funds/service.py`: add a basic-profile-only classification sync.
- `src/kunjin/services/research.py`: accept an optional bounded NAV page count.
- `src/kunjin/storage/schema.py`: schema version 6 and peer tables.
- `src/kunjin/storage/repository.py`: apply migration 6.
- `src/kunjin/cli.py`: command parsing, context wiring, and stable envelopes.
- `tests/integration/test_cli.py`: peer/compare/overlap contracts and failures.
- `tests/unit/test_fund_disclosure_service.py`: classification-only sync contract.
- `tests/test_smoke.py`: packaged command discovery.
- `README.md`: usage, metric definitions, and limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: proactive peer and overlap workflow.

Keep existing untracked phase-4 and broad design documents outside the phase-5B
business commit unless the user explicitly requests otherwise.

### Task 1: Peer Domain Models

**Files:**
- Create: `src/kunjin/funds/peers/__init__.py`
- Create: `src/kunjin/funds/peers/models.py`
- Create: `tests/unit/test_peer_models.py`

- [ ] **Step 1: Write failing model-validation tests**

Test these exact contracts:

```python
candidate = DirectoryCandidate(
    fund_code="519755",
    fund_name="交银多策略回报灵活配置混合A",
    directory_type="混合型-灵活",
    source_url="https://fund.eastmoney.com/js/fundcode_search.js",
    source_checksum="a" * 64,
)
candidate.validate()

member = PeerGroupMember(
    fund_code="519755",
    membership_kind=MembershipKind.ANCHOR,
    classification_key="混合型-灵活|active_or_unspecified|equity_bond",
    acceptance_reason="anchor_classification_match",
    warning=None,
    profile_source_document_id=1,
)
member.validate()
```

Assert rejection of invalid fund codes, non-HTTPS source URLs, invalid SHA-256,
empty type/classification/reason, non-positive source IDs, unknown membership
kinds, duplicate member codes in a group, naive datetimes, group size above 20,
and overlap percentages outside `0..100`.

- [ ] **Step 2: Run the model test and verify import failure**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_models -v
```

Expected: FAIL because `kunjin.funds.peers.models` does not exist.

- [ ] **Step 3: Implement immutable models**

Define these exact enums and frozen dataclasses:

```python
class MembershipKind(str, Enum):
    ANCHOR = "anchor"
    USER_SUPPLIED = "user_supplied"
    HELD = "held"
    DISCOVERED = "discovered"


class PeerGroupStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"


class PeerSyncState(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(frozen=True)
class DirectoryCandidate:
    fund_code: str
    fund_name: str
    directory_type: str
    source_url: str
    source_checksum: str


@dataclass(frozen=True)
class PeerClassification:
    fund_code: str
    accepted: bool
    classification_key: Optional[str]
    fund_type_family: Optional[str]
    management_style: Optional[str]
    benchmark_family: Optional[str]
    reason: str
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PeerGroupMember:
    fund_code: str
    membership_kind: MembershipKind
    classification_key: str
    acceptance_reason: str
    warning: Optional[str]
    profile_source_document_id: Optional[int]


@dataclass(frozen=True)
class PeerGroup:
    id: Optional[int]
    anchor_fund_code: str
    rule_version: str
    rule_key: str
    rule_description: str
    candidate_source_url: str
    candidate_source_tier: int
    candidate_source_checksum: str
    input_fingerprint: str
    created_at: datetime
    status: PeerGroupStatus
    members: Tuple[PeerGroupMember, ...]
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class WindowMetric:
    fund_code: str
    window: str
    effective_start: date
    effective_end: date
    observations: int
    total_return: Decimal
    annualized_volatility: Optional[Decimal]
    max_drawdown: Decimal
    drawdown_peak_date: date
    trough_date: date
    recovery_date: Optional[date]


@dataclass(frozen=True)
class SharedExposure:
    exposure_type: str
    exposure_code: str
    exposure_name: str
    left_weight: Decimal
    right_weight: Decimal
    shared_weight: Decimal


@dataclass(frozen=True)
class PairwiseOverlap:
    left_fund_code: str
    right_fund_code: str
    metric_name: str
    left_report_period: date
    right_report_period: date
    left_published_at: datetime
    right_published_at: datetime
    left_disclosed_weight: Decimal
    right_disclosed_weight: Decimal
    overlap: Decimal
    shared: Tuple[SharedExposure, ...]
    warnings: Tuple[str, ...] = ()
```

Use shared validators for six-digit codes, aware datetimes, SHA-256, required
strings, positive IDs, bounded percentages, unique group members, and the 20
member limit. Export public types from `__init__.py`.

- [ ] **Step 4: Run model tests**

Run the Task 1 command. Expected: all peer model tests pass.

- [ ] **Step 5: Commit peer models**

```bash
git add src/kunjin/funds/peers/__init__.py src/kunjin/funds/peers/models.py tests/unit/test_peer_models.py
git commit -m "feat: define fund peer research models"
```

### Task 2: Safe Candidate Directory Adapter

**Files:**
- Create: `src/kunjin/funds/peers/sources.py`
- Create: `tests/fixtures/funds/fundcode_search.js`
- Create: `tests/unit/test_peer_sources.py`
- Modify: `src/kunjin/funds/sources.py`

- [ ] **Step 1: Add a redacted directory fixture and failing parser tests**

The fixture must include:

```javascript
var r = [["519755","JYDHCLHB","交银多策略回报灵活配置混合A","混合型-灵活","JIAOYINDUOCELUEHUIBAO"],["000001","HXCZHH","华夏成长混合","混合型-灵活","HUAXIACHENGZHANGHUNHE"],["000002","HXCZHH","华夏成长混合(后端)","混合型-灵活","HUAXIACHENGZHANGHUNHE"],["000003","ZHKZZZQA","中海可转债债券A","债券型-混合二级","ZHONGHAIKEZHUANZHAIZHAIQUANA"]];
```

Test:

```python
response = TextResponse(
    requested_url=PEER_DIRECTORY_URL,
    final_url=PEER_DIRECTORY_URL,
    text=fixture,
    retrieved_at=NOW,
    checksum="b" * 64,
    content_type="application/javascript; charset=utf-8",
)
items = parse_peer_directory(response)
assert [item.fund_code for item in items] == ["519755", "000001", "000002", "000003"]
assert all(item.source_checksum == "b" * 64 for item in items)
```

Also assert rejection of wrong assignment names, trailing executable text,
non-list JSON, rows with non-string values, invalid codes, empty names,
oversized row counts above 30,000, and a redirect from `fund.eastmoney.com` to
another host. Assert that a structurally valid row with an empty directory type
is skipped while valid rows in the same response remain available.

- [ ] **Step 2: Run tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_sources -v
```

Expected: FAIL because peer sources do not exist and the host is not allowlisted.

- [ ] **Step 3: Implement exact directory parsing**

Create:

```python
PEER_DIRECTORY_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
PEER_DIRECTORY_REFERER = "https://fund.eastmoney.com/"
MAX_DIRECTORY_ROWS = 30_000


def _reject_constant(value: str) -> None:
    raise FundParseError("malformed_peer_directory", f"invalid JSON constant: {value}")


def parse_peer_directory(response: TextResponse) -> Tuple[DirectoryCandidate, ...]:
    text = response.text.lstrip("\ufeff").strip()
    match = re.fullmatch(r"var\s+r\s*=\s*(\[.*\])\s*;", text, re.DOTALL)
    if match is None:
        raise FundParseError("malformed_peer_directory")
    try:
        payload = json.loads(match.group(1), parse_constant=_reject_constant)
    except (json.JSONDecodeError, FundParseError) as exc:
        raise FundParseError("malformed_peer_directory") from exc
    if not isinstance(payload, list) or len(payload) > MAX_DIRECTORY_ROWS:
        raise FundParseError("invalid_peer_directory_size")
    candidates = []
    for row in payload:
        if (
            not isinstance(row, list)
            or len(row) != 5
            or not all(isinstance(value, str) for value in row)
        ):
            raise FundParseError("malformed_peer_directory_row")
        fund_code, _, fund_name, directory_type, _ = row
        if not directory_type.strip():
            continue
        candidate = DirectoryCandidate(
            fund_code=fund_code,
            fund_name=fund_name,
            directory_type=directory_type,
            source_url=response.final_url,
            source_checksum=response.checksum,
        )
        candidate.validate()
        candidates.append(candidate)
    return tuple(candidates)
```

Add `fund.eastmoney.com` to `FETCHABLE_HOSTS` in
`src/kunjin/funds/sources.py`. Reuse `FundTextClient.fetch()` instead of adding
a weaker HTTP client.

- [ ] **Step 4: Run source security and parser tests**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_sources tests.unit.test_fund_sources -v
```

Expected: all tests pass, including existing SSRF and redirect tests.

- [ ] **Step 5: Commit candidate source support**

```bash
git add src/kunjin/funds/sources.py src/kunjin/funds/peers/sources.py tests/fixtures/funds/fundcode_search.js tests/unit/test_peer_sources.py
git commit -m "feat: add audited fund peer directory"
```

### Task 3: Deterministic Candidate Ordering And Peer Classification

**Files:**
- Create: `src/kunjin/funds/peers/classification.py`
- Create: `tests/unit/test_peer_classification.py`

- [ ] **Step 1: Write failing ordering and classification tests**

Create helpers that build `DisclosureBundle` values with identities, share
classes, benchmarks, and source IDs. Test:

- Membership precedence is anchor, user-supplied, held, discovered.
- Duplicate codes retain the highest-precedence kind.
- Back-end class names are excluded from discovery.
- Same input and anchor always produce the same SHA-256 order.
- Different anchor codes can produce a different deterministic order.
- No more than 40 discovered codes are returned for validation.
- Inactive, missing-identity, conflicting-identity, and ambiguous-type funds are
  rejected with stable reasons.
- `混合型-灵活` plus an equity-and-bond benchmark produces the exact key
  `混合型-灵活|active_or_unspecified|equity_bond`.
- Explicit `指数型-*` produces `management_style="passive"`.
- A/C siblings are accepted but include `share_class_sibling`.
- A fund with a different normalized type or benchmark family is rejected.

- [ ] **Step 2: Run tests and verify missing implementation**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_classification -v
```

Expected: FAIL because classification functions do not exist.

- [ ] **Step 3: Implement pure ordering and classification**

Expose these public signatures:

```text
PEER_MEMBER_LIMIT = 20
DISCOVERY_VALIDATION_LIMIT = 40
PEER_RULE_VERSION = "1"

ordered_candidates(
    anchor_code: str,
    directory: Sequence[DirectoryCandidate],
    user_supplied: Sequence[str],
    held_codes: Sequence[str],
) -> Tuple[Tuple[str, MembershipKind], ...]

classify_peer(
    anchor: DisclosureBundle,
    candidate: DisclosureBundle,
    as_of: date,
) -> PeerClassification
```

Normalize fund types with NFKC and whitespace removal, but do not merge distinct
directory subtypes. Management style is `passive` only when the normalized fund
type contains `指数型`; otherwise use `active_or_unspecified`.

Benchmark families are conservative:

```python
EQUITY_TOKENS = ("沪深300", "中证500", "中证1000", "创业板", "科创50", "股票指数")
BOND_TOKENS = ("中债", "债券指数", "国债指数", "信用债指数")

def benchmark_family(text: str) -> Optional[str]:
    normalized = "".join(unicodedata.normalize("NFKC", text).split())
    has_equity = any(token in normalized for token in EQUITY_TOKENS)
    has_bond = any(token in normalized for token in BOND_TOKENS)
    if has_equity and has_bond:
        return "equity_bond"
    if has_equity:
        return "equity"
    if has_bond:
        return "bond"
    return None
```

Reject automatic membership when either benchmark family is missing or the
families differ. Explicit `fund compare` may still compare such funds and must
surface `peer_classification_ambiguous`.

- [ ] **Step 4: Run classification tests**

Run the Task 3 command. Expected: all tests pass.

- [ ] **Step 5: Commit classification logic**

```bash
git add src/kunjin/funds/peers/classification.py tests/unit/test_peer_classification.py
git commit -m "feat: classify comparable public funds"
```

### Task 4: Aligned Formal-NAV Metrics

**Files:**
- Create: `src/kunjin/funds/peers/analytics.py`
- Create: `tests/unit/test_peer_analytics.py`

- [ ] **Step 1: Write failing aligned-window tests**

Use daily or sparse `FundNavObservation` fixtures with different end dates.
Assert:

- The common end date is the latest date present in every included history.
- A 90-day baseline is the latest observation on or before the target start,
  but no more than 7 calendar days earlier.
- Missing baseline produces `aligned_nav_window_unavailable` for that member.
- Members are never silently compared with different effective dates.
- Volatility uses population standard deviation of daily returns and `sqrt(252)`.
- Drawdown peak, trough, maximum drawdown, and recovery match known vectors.
- A one-observation window has return and drawdown but `annualized_volatility=None`.
- The manager-team start is the latest start date among current co-managers.
- Former-manager history is not included in the manager-team window.
- Manager-tenure returns with different effective starts are displayed with
  dates but are not placed in one return ordering.
- Three or more valid size observations calculate earliest-to-latest size change
  and population standard deviation of quarter-to-quarter changes.
- Fewer than three valid size observations returns `insufficient_data`.

- [ ] **Step 2: Run analytics tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_analytics.AlignedNavMetricTest -v
```

Expected: FAIL because aligned NAV analytics do not exist.

- [ ] **Step 3: Implement aligned metric functions**

Expose these constants and signatures:

```text
PEER_CALCULATION_VERSION = "1"
START_TOLERANCE_DAYS = 7

common_end_date(
    histories: Mapping[str, Sequence[FundNavObservation]],
) -> Optional[date]

calculate_window_metric(
    fund_code: str,
    history: Sequence[FundNavObservation],
    window: str,
    target_start: date,
    effective_end: date,
) -> Tuple[Optional[WindowMetric], Tuple[str, ...]]

current_manager_team_start(
    tenures: Sequence[FundManagerTenure], as_of: date
) -> Optional[date]

calculate_size_stability(
    observations: Sequence[FundSizeObservation],
) -> Dict[str, object]
```

`common_end_date()` returns `None` for an empty mapping, any empty member
history, or no date intersection. `current_manager_team_start()` filters active
tenures using the supplied date and returns the maximum active start date.

Quantize nothing inside the analytics layer. Preserve full `Decimal` results;
the renderer serializes them as fixed-point strings.
Returns, volatility, and maximum drawdown use decimal ratios, matching the
existing `analyze_fund_history()` convention: `0.10` means 10%. Total return
must be greater than `-1`; maximum drawdown is a positive magnitude in `0..1`.

- [ ] **Step 4: Run aligned metric tests**

Run the Task 4 command. Expected: aligned metric tests pass.

- [ ] **Step 5: Commit aligned analytics**

```bash
git add src/kunjin/funds/peers/analytics.py tests/unit/test_peer_analytics.py
git commit -m "feat: calculate aligned fund peer metrics"
```

### Task 5: Pairwise And Portfolio Disclosed Overlap

**Files:**
- Modify: `src/kunjin/funds/peers/analytics.py`
- Modify: `tests/unit/test_peer_analytics.py`

- [ ] **Step 1: Write failing pairwise-overlap tests**

Test holdings in percent units:

```python
left = [holding("600000", "浦发银行", "5.0"), holding("600519", "贵州茅台", "3.0")]
right = [holding("600000", "浦发银行", "2.0"), holding("000001", "平安银行", "4.0")]
result = pairwise_overlap(left, right)
assert result.overlap == Decimal("2.0")
assert result.shared[0].shared_weight == Decimal("2.0")
assert result.shared[0].exposure_type == "stock"
assert result.metric_name == "top10_disclosed_overlap"
```

Also test zero overlap, same-period preference, one-quarter mismatch warning,
more-than-one-quarter rejection, missing publication dates, security-code joins
instead of name joins, differing security names retained as a warning, industry
overlap only under the same classification standard, and disclosed-weight sums.

- [ ] **Step 2: Run overlap tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_analytics.OverlapTest -v
```

Expected: FAIL because overlap functions do not exist.

- [ ] **Step 3: Implement pairwise overlap**

Expose:

```text
select_overlap_periods(
    left: Sequence[FundHolding],
    right: Sequence[FundHolding],
) -> Tuple[date, date, Tuple[str, ...]]

pairwise_overlap(
    left_fund_code: str,
    right_fund_code: str,
    left: Sequence[FundHolding],
    right: Sequence[FundHolding],
) -> PairwiseOverlap

pairwise_industry_overlap(
    left_fund_code: str,
    right_fund_code: str,
    left: Sequence[FundIndustryExposure],
    right: Sequence[FundIndustryExposure],
) -> Tuple[Optional[PairwiseOverlap], Tuple[str, ...]]
```

Join by `(asset_type, security_code)`. Set `metric_name` to
`top10_disclosed_overlap` if either selected scope is `top10`; otherwise use
`disclosed_overlap`. Sum `min(left.weight, right.weight)` without normalizing the
partial disclosures to 100%.

`pairwise_industry_overlap()` returns `None` and
`industry_classification_mismatch` when the selected records use different
classification standards. Under the same standard, join by non-empty industry
code and otherwise by normalized industry name, and apply the same minimum-
weight calculation without normalizing partial data.

- [ ] **Step 4: Write failing portfolio look-through tests**

Given portfolio weights as fractions from `analyze_portfolio()` and holding
weights as percentages, assert:

```text
fund A portfolio weight 0.60 * security weight 10% = 6% look-through
fund B portfolio weight 0.40 * security weight 5%  = 2% look-through
total disclosed security exposure = 8%
duplicated shared contribution = 2%
```

Test missing NAV, missing holdings, stale holdings, duplicate accounts for the
same fund code, and partial portfolio coverage. Missing inputs must generate a
warning and must not be interpreted as zero exposure.

- [ ] **Step 5: Implement portfolio overlap**

Add a pure function with this signature:

```text
portfolio_overlap(
    portfolio_weights: Mapping[str, Decimal],
    holdings_by_fund: Mapping[str, Sequence[FundHolding]],
    stale_codes: AbstractSet[str] = frozenset(),
) -> Dict[str, object]
```

Convert holding percentages using `holding.weight / Decimal("100")`. Return
per-security contributors, summed disclosed exposure, duplicated contribution,
included fund codes, omitted fund codes, portfolio-weight coverage, disclosure
coverage, report periods, and warnings.

- [ ] **Step 6: Run all peer analytics tests**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_analytics -v
```

Expected: all aligned metric and overlap tests pass.

- [ ] **Step 7: Commit overlap analytics**

```bash
git add src/kunjin/funds/peers/analytics.py tests/unit/test_peer_analytics.py
git commit -m "feat: calculate disclosed fund overlap"
```

### Task 6: Schema Version 6 And Transactional Peer Store

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `src/kunjin/funds/peers/store.py`
- Create: `tests/unit/test_schema_v6.py`
- Create: `tests/unit/test_peer_store.py`
- Modify: `tests/unit/test_schema_v5.py`

- [ ] **Step 1: Write failing schema migration tests**

Create a version-5 database containing one disclosure source, identity,
manager, holding, transaction, and NAV observation. Run `Repository.migrate()`
and assert versions `[1, 2, 3, 4, 5, 6]`, original values unchanged, and these
tables exist:

```text
fund_peer_groups
fund_peer_group_syncs
fund_peer_group_members
fund_comparison_runs
```

Update the v5 migration expectation to include version 6 when migration runs on
the current application.

- [ ] **Step 2: Run schema tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_schema_v5 tests.unit.test_schema_v6 -v
```

Expected: FAIL because schema version 6 is absent.

- [ ] **Step 3: Add exact schema-v6 tables**

Set `SCHEMA_VERSION = 6` and add:

```sql
CREATE TABLE IF NOT EXISTS fund_peer_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anchor_fund_code TEXT NOT NULL CHECK(length(anchor_fund_code) = 6),
    rule_version TEXT NOT NULL,
    rule_key TEXT NOT NULL,
    rule_description TEXT NOT NULL,
    candidate_source_url TEXT NOT NULL,
    candidate_source_tier INTEGER NOT NULL CHECK(candidate_source_tier BETWEEN 1 AND 3),
    candidate_source_checksum TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'partial')),
    warning TEXT,
    UNIQUE(anchor_fund_code, rule_version, input_fingerprint)
);

CREATE TABLE IF NOT EXISTS fund_peer_group_members (
    peer_group_id INTEGER NOT NULL REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    membership_kind TEXT NOT NULL CHECK(membership_kind IN (
        'anchor', 'user_supplied', 'held', 'discovered'
    )),
    classification_key TEXT NOT NULL,
    acceptance_reason TEXT NOT NULL,
    warning TEXT,
    profile_source_document_id INTEGER REFERENCES fund_source_documents(id) ON DELETE RESTRICT,
    PRIMARY KEY(peer_group_id, fund_code)
);

CREATE TABLE IF NOT EXISTS fund_peer_group_syncs (
    anchor_fund_code TEXT PRIMARY KEY CHECK(length(anchor_fund_code) = 6),
    current_peer_group_id INTEGER REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK(state IN ('success', 'partial', 'source_unavailable')),
    last_attempted_at TEXT NOT NULL,
    last_success_at TEXT,
    error_code TEXT,
    warning TEXT
);

CREATE TABLE IF NOT EXISTS fund_comparison_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_kind TEXT NOT NULL CHECK(comparison_kind IN (
        'peer', 'explicit', 'portfolio_overlap'
    )),
    anchor_fund_code TEXT CHECK(anchor_fund_code IS NULL OR length(anchor_fund_code) = 6),
    peer_group_id INTEGER REFERENCES fund_peer_groups(id) ON DELETE RESTRICT,
    calculation_version TEXT NOT NULL,
    as_of TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success', 'partial', 'insufficient_data')),
    input_fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    warning TEXT,
    UNIQUE(comparison_kind, input_fingerprint, calculation_version)
);
```

Record migration version 5 before executing `SCHEMA_V6`, then record version 6.

- [ ] **Step 4: Run schema tests**

Run the Task 6 schema command. Expected: all schema tests pass.

- [ ] **Step 5: Write failing peer-store tests**

Test:

- Publishing a valid group inserts members and moves the pointer atomically.
- A second version becomes current without deleting the first version.
- Duplicate publication is idempotent under an input fingerprint generated from
  canonical JSON.
- Invalid member/source references roll back the whole publication.
- `mark_failure()` preserves `current_peer_group_id` and `last_success_at`.
- A first total failure stores `source_unavailable` with no pointer.
- Comparison JSON rejects NaN/Infinity and is serialized with sorted keys and
  compact separators.
- A repeated comparison fingerprint returns the existing run.

- [ ] **Step 6: Implement `PeerStore`**

Implement `PeerStore` with these public signatures:

```text
PeerStore(repository: Repository)
publish_group(group: PeerGroup) -> int
mark_failure(
        anchor_fund_code: str,
        error_code: str,
        warning: str,
        attempted_at: datetime,
) -> None
load_current_group(anchor_fund_code: str) -> Optional[PeerGroup]
list_anchor_codes() -> Tuple[str, ...]
save_comparison(
        comparison_kind: str,
        anchor_fund_code: Optional[str],
        peer_group_id: Optional[int],
        as_of: datetime,
        status: str,
        input_fingerprint: str,
        result: Mapping[str, object],
        warning: Optional[str],
) -> int
load_comparison(run_id: int) -> Optional[Dict[str, object]]
```

Use one SQLite transaction for group, members, and pointer. Use canonical JSON
for comparison payloads and reject non-finite constants on both write and read.

- [ ] **Step 7: Run store and migration tests**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_schema_v5 tests.unit.test_schema_v6 tests.unit.test_peer_store -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit schema and store**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py src/kunjin/funds/peers/store.py tests/unit/test_schema_v5.py tests/unit/test_schema_v6.py tests/unit/test_peer_store.py
git commit -m "feat: persist versioned fund peer groups"
```

### Task 7: Bounded Peer Synchronization Service

**Files:**
- Create: `src/kunjin/funds/peers/service.py`
- Create: `tests/unit/test_peer_service.py`
- Modify: `src/kunjin/funds/service.py`
- Modify: `src/kunjin/services/research.py`
- Modify: `tests/unit/test_fund_disclosure_service.py`

- [ ] **Step 1: Write failing orchestration tests with fakes**

Fake the directory fetch, disclosure service/store, research sync service,
repository histories/positions, and peer store. Test:

- Anchor profile is validated before candidate discovery.
- Directory is fetched once per sync.
- Explicit and held candidates precede discovered candidates.
- At most 40 discovered candidates invoke validation.
- Candidate pre-validation calls only the basic-profile classification sync.
- Only accepted members invoke full profile, holdings, and bounded NAV sync.
- Peer NAV synchronization passes `max_pages=20`.
- Each candidate profile, formal NAV, and holdings sync failure is isolated.
- A candidate with profile success but NAV failure may remain a group member but
  receives a comparison-data warning.
- A candidate with ambiguous classification is rejected.
- The final group contains at most 20 members and always contains the anchor.
- Two or more members publish `success` or `partial` depending on errors.
- Fewer than two valid members records `peer_group_too_small` and preserves the
  prior group.
- Total directory failure records `candidate_discovery_unavailable` and reuses
  explicit/held/current members when at least two validate.
- `refresh_existing_groups()` reads only stored anchors and does not create peer
  groups for every held fund automatically.

- [ ] **Step 2: Run service tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_service tests.unit.test_fund_disclosure_service -v
```

Expected: FAIL because `PeerResearchService` does not exist.

- [ ] **Step 3: Implement service contracts**

Define `PeerSyncResult` as shown, then implement `PeerResearchService` with the
listed constructor and methods:

```python
@dataclass(frozen=True)
class PeerSyncResult:
    anchor_fund_code: str
    status: PeerSyncState
    peer_group_id: Optional[int]
    members: int
    attempted_candidates: int
    rejected_candidates: int
    warnings: Tuple[str, ...]
    errors: Tuple[Dict[str, str], ...]
```

```text
PeerResearchService(
        directory_client: FundTextClient,
        disclosure_service: FundDisclosureService,
        disclosure_store: FundDisclosureStore,
        research_service: ResearchSyncService,
        repository: Repository,
        peer_store: PeerStore,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) 
sync_peers(
        anchor_fund_code: str,
        user_candidates: Sequence[str] = (),
) -> PeerSyncResult
refresh_existing_groups() -> Dict[str, PeerSyncResult]
```

Pre-validation calls `FundDisclosureService.sync_classification()`, which
synchronizes only `basic_profile`; the benchmark is already normalized from
that page. Only after a candidate passes classification, run `sync_profile()`,
`sync_holdings()`, and `ResearchSyncService.sync_fund(fund_code,
max_pages=20)` in separate `try` blocks. NAV and holdings gaps become visible
member warnings. Do not fetch more codes after 20 members are accepted or 40
discovered candidates have been validated.

Add this backward-compatible disclosure-service method and constant:

```python
CLASSIFICATION_SECTIONS = ("basic_profile",)

def sync_classification(self, fund_code: str) -> FundDisclosureSyncResult:
    return self._sync(fund_code, CLASSIFICATION_SECTIONS)
```

Change the research service signature without changing existing callers:

```python
def sync_fund(
    self,
    fund_code: str,
    max_pages: int = FUND_NAV_MAX_PAGES,
) -> ResearchSyncResult:
    _, name, fund_type, observations = self.fund_client.fetch_nav_history(
        fund_code,
        max_pages=max_pages,
    )
    self.repository.save_fund_history(
        fund_code, name, fund_type, "eastmoney", observations
    )
    return ResearchSyncResult(fund_code, len(observations))
```

Use `PeerStore.publish_group()` only after the full bounded loop completes.
Never move the current pointer to a group with fewer than two members.

- [ ] **Step 4: Run service tests**

Run the Task 7 command. Expected: all orchestration tests pass.

- [ ] **Step 5: Commit synchronization service**

```bash
git add src/kunjin/funds/peers/service.py src/kunjin/funds/service.py src/kunjin/services/research.py tests/unit/test_peer_service.py tests/unit/test_fund_disclosure_service.py
git commit -m "feat: synchronize bounded fund peer groups"
```

### Task 8: Structured Peer, Compare, And Overlap Reports

**Files:**
- Create: `src/kunjin/funds/peers/research.py`
- Create: `tests/unit/test_peer_research.py`

- [ ] **Step 1: Write failing report tests**

Build stored bundles, histories, peer groups, positions, and known metric
vectors. Assert the peer report contains:

```python
{
    "anchor_fund_code": "519755",
    "rule_version": "1",
    "calculation_version": "1",
    "members": [{"fund_code": "519755", "membership_kind": "anchor"}],
    "windows": {"90d": [], "365d": [], "manager_tenure": []},
    "metric_orderings": {"90d": {}},
    "fees": {"519755": []},
    "sizes": {"519755": None},
    "pairwise_overlap": [],
    "portfolio_overlap": {"evidence_level": "insufficient_data"},
    "advantages": [],
    "tradeoffs": [],
    "data_gaps": ["peer_group_too_small"],
    "watch_reasons": [],
    "coverage": {"members_total": 1, "members_with_90d_nav": 0},
    "data_dates": {"peer_group_created_at": "2026-07-11T00:00:00+00:00"},
    "sources": [],
    "warnings": ["peer_group_too_small"],
    "errors": [],
}
```

Test that:

- Metric orderings are independent and name their metric/window.
- No key named `score`, `overall_score`, `recommendation`, `buy`, or `sell`
  appears in a deterministic program report.
- A/C siblings carry a diversification warning.
- Missing 365-day history affects only that window.
- Fees remain raw condition-aware rules.
- Ongoing annual fee comparison sums only current management, custody, and
  sales-service percentage rules for the applicable share class.
- Subscription and redemption rules are ordered only when all rule conditions
  match; otherwise they remain side-by-side facts.
- Size stability uses at most five latest quarterly net-asset observations and
  requires at least three.
- Manager-tenure return is not ranked when effective start dates differ.
- Top-ten overlap is not labeled total overlap.
- Explicit compare accepts non-peer funds but reports comparability warnings.
- Portfolio coverage is below 100% when a held fund lacks usable holdings.

- [ ] **Step 2: Run research tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.unit.test_peer_research -v
```

Expected: FAIL because peer report builders do not exist.

- [ ] **Step 3: Implement report builders**

Expose these report-builder signatures:

```text
build_peer_report(
    group: PeerGroup,
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]

build_explicit_compare_report(
    fund_codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]

build_portfolio_overlap_report(
    bundles: Mapping[str, DisclosureBundle],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]
```

Reuse `analyze_portfolio()` for current fund weights. Use source document IDs
from bundles and the existing phase 5A freshness states. Advantages/tradeoffs
must be generated from explicit comparisons such as lower volatility or lower
drawdown, never from a hidden weighted score.

Add `comparison_fingerprint(payload)` as SHA-256 of canonical JSON containing
fund codes, source document IDs, NAV end dates, portfolio observation times,
rule version, calculation version, and window parameters.

- [ ] **Step 4: Run report tests**

Run the Task 8 command. Expected: all report tests pass.

- [ ] **Step 5: Commit research reports**

```bash
git add src/kunjin/funds/peers/research.py tests/unit/test_peer_research.py
git commit -m "feat: render fund peer and overlap research"
```

### Task 9: CLI Contracts And Application Wiring

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser and integration tests**

Add parser cases:

```python
["--json", "sync", "fund-peers", "519755"]
["--json", "sync", "fund-peers", "519755", "--candidate", "000001"]
["--json", "fund", "peers", "519755"]
["--json", "fund", "compare", "519755", "000001"]
["--json", "portfolio", "overlap"]
```

Test stable command names:

```text
sync.fund-peers
fund.peers
fund.compare
portfolio.overlap
```

Validate six-digit codes, duplicate codes, 2-to-10 explicit compare size, and
repeatable `--candidate` arguments. Read commands must not invoke network fakes.

Use fake stores/services to assert:

- Peer sync partial success exits zero and includes member errors in `data`.
- A peer group smaller than two exits one with `peer_group_too_small`.
- Missing current peer group returns `insufficient_data`, not an exception.
- `fund compare` persists a deterministic local comparison run but does not
  synchronize.
- `portfolio overlap` persists a deterministic local comparison run but does not
  synchronize Yangjibao.
- Tokens and raw snapshots never appear in JSON.

- [ ] **Step 2: Run CLI tests and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v
```

Expected: FAIL because peer commands are absent.

- [ ] **Step 3: Wire context and parsers**

Extend `ApplicationContext`:

```python
peer_store: Optional[PeerStore] = None
peer_service: Optional[PeerResearchService] = None
```

In `build_context()`, create one `PeerStore` and one `PeerResearchService` from
the existing repository, disclosure service/store, research service, and a
`FundTextClient`.

Add arguments:

```python
sync_fund_peers = sync_subparsers.add_parser("fund-peers")
sync_fund_peers.add_argument("fund_code")
sync_fund_peers.add_argument("--candidate", action="append", default=[])

fund_peers = fund_subparsers.add_parser("peers")
fund_peers.add_argument("fund_code")

fund_compare = fund_subparsers.add_parser("compare")
fund_compare.add_argument("fund_codes", nargs="+")

portfolio_subparsers.add_parser("overlap")
```

- [ ] **Step 4: Implement thin command handlers**

Handlers must:

- Validate all fund codes before service/store calls.
- Load only current peer groups and current disclosure bundles.
- Load formal NAV through `Repository.fund_history()`.
- Load positions through `Repository.latest_positions()`.
- Build reports through `peers.research`.
- Save local comparison runs with their canonical fingerprints.
- Return the existing envelope shape and preserve report-level errors under
  `data["errors"]`.
- Never synchronize inside `fund peers`, `fund compare`, or
  `portfolio overlap`.

- [ ] **Step 5: Run CLI and smoke tests**

Run the Task 9 command. Expected: all tests pass.

- [ ] **Step 6: Commit CLI integration**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose fund peer and overlap commands"
```

### Task 10: Daily Refresh, README, And KunJin Skill

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`

- [ ] **Step 1: Write failing daily-sync isolation test**

Create one stored peer anchor and fake `refresh_existing_groups()`. Assert:

- `sync daily` refreshes only previously created peer groups.
- It does not create groups for all held funds.
- A peer refresh failure does not discard portfolio, NAV, disclosure, holdings,
  or market results.
- The overall command exits one when peer refresh fails and reports the exact
  anchor/error while retaining successful data.

- [ ] **Step 2: Run the daily-sync test and verify failure**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.integration.test_cli.CliIntegrationTest.test_daily_sync_isolates_peer_refresh_failures -v
```

Expected: FAIL because daily sync does not call the peer service.

- [ ] **Step 3: Add isolated existing-group refresh**

Call `peer_service.refresh_existing_groups()` after existing fund disclosure
work. Catch errors per anchor and append them to the daily result without
short-circuiting market or other fund results.

- [ ] **Step 4: Update README commands and limitations**

Document:

```bash
.venv/bin/kunjin --json sync fund-peers 519755
.venv/bin/kunjin --json sync fund-peers 519755 --candidate 000001
.venv/bin/kunjin --json fund peers 519755
.venv/bin/kunjin --json fund compare 519755 000001
.venv/bin/kunjin --json portfolio overlap
```

Explain aligned NAV dates, deterministic per-metric orderings, A/C sibling
handling, top-ten disclosure scope, coverage, the 20-member limit, tier-2
candidate discovery, and the lack of a universal score or automatic trade.

- [ ] **Step 5: Update the project KunJin Skill**

Add workflow rules:

- For latest peer questions, run `fund peers CODE`, inspect freshness, then run
  `sync fund-peers CODE` if missing or stale.
- For explicit comparisons, synchronize profile, holdings, and formal NAV for
  every code before `fund compare CODE1 CODE2` when latest data is requested.
- For current portfolio overlap, run `sync portfolio`, refresh stale held-fund
  holdings, then run `portfolio overlap`.
- Preserve aligned dates, manager-team dates, metric-specific ordering,
  disclosure scope, coverage, source tier, warnings, and errors.
- Never turn platform directory order into merit.
- Only provide buy/hold/add/reduce/sell interpretation when the user explicitly
  requests it, and include opposing evidence, horizon, invalidation, and data
  limitations.

Move peer screening and overlap out of `Unsupported Requests`. Keep valuation,
earnings, persistent flows, and automatic news ingestion unsupported.

- [ ] **Step 6: Run daily and Skill-facing smoke tests**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit scheduling and docs**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py README.md integrations/codex/kunjin-fund/SKILL.md
git commit -m "docs: add fund peer research workflow"
```

### Task 11: Live Smoke Test, Security Review, And Final Verification

**Files:**
- Modify only files required by defects proven during this task.

- [ ] **Step 1: Run focused automated tests**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest \
  tests.unit.test_peer_models \
  tests.unit.test_peer_sources \
  tests.unit.test_peer_classification \
  tests.unit.test_peer_analytics \
  tests.unit.test_peer_store \
  tests.unit.test_peer_service \
  tests.unit.test_peer_research \
  tests.unit.test_schema_v6 \
  tests.integration.test_cli -v
```

Expected: all focused tests pass.

- [ ] **Step 2: Synchronize a real held-fund peer group**

```bash
.venv/bin/kunjin --json status
.venv/bin/kunjin --json sync fund-peers 519755
.venv/bin/kunjin --json fund peers 519755
```

Verify:

- Anchor identity is `519755` / `交银多策略回报灵活配置混合A`.
- Rule version and human-readable classification are present.
- Member count is between 2 and 20.
- Candidate source is tier 2 and exactly the static directory URL.
- No upstream rank or platform score appears.
- Every metric states effective start/end dates or `insufficient_data`.
- Current manager-team start uses the actual active team.

- [ ] **Step 3: Run explicit compare and portfolio overlap smoke tests**

Extract the first accepted non-anchor peer code from the structured result and
run the explicit comparison:

```bash
PEER_CODE=$(.venv/bin/kunjin --json fund peers 519755 | .venv/bin/python -c 'import json,sys; data=json.load(sys.stdin)["data"]; print(next(item["fund_code"] for item in data["members"] if item["fund_code"] != "519755"))')
test -n "$PEER_CODE"
.venv/bin/kunjin --json fund compare 519755 "$PEER_CODE"
.venv/bin/kunjin --json portfolio overlap
```

Do not guess a peer code in advance; the command must use the current stored
group member returned by KunJin.

Verify aligned NAV dates, fee conditions, latest size dates, pairwise report
periods, `top10_disclosed_overlap`, common security details, portfolio coverage,
and omitted-fund warnings.

- [ ] **Step 4: Run source and credential scans**

```bash
rg -n "Authorization:|Request-Sign:|never-print-this|token=\\S+" src tests README.md integrations
rg -n "http://|localhost|127\\.0\\.0\\.1|169\\.254\\." src/kunjin/funds tests/fixtures/funds
rg -n '"(score|overall_score|buy|sell|recommendation)"' src/kunjin/funds/peers tests/unit/test_peer_*.py
```

Expected: only intentional redaction/security fixtures, URL rejection guards,
and negative assertions match. No production peer report contains a forbidden
automatic recommendation or composite-score key.

- [ ] **Step 5: Run complete verification**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache .venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
git diff --check
```

Expected: all tests pass, compileall exits zero, pip reports no broken
requirements, and the diff check is clean.

- [ ] **Step 6: Inspect the final business diff**

```bash
git status --short
git diff --stat
git diff -- README.md integrations/codex/kunjin-fund/SKILL.md src/kunjin tests
```

Confirm only phase-5B implementation, fixtures, tests, README, Skill, and the
approved phase-5B spec/plan are intended for the final commit.

- [ ] **Step 7: Commit and push phase 5B**

```bash
git add \
  README.md \
  integrations/codex/kunjin-fund/SKILL.md \
  src/kunjin/cli.py \
  src/kunjin/storage/schema.py \
  src/kunjin/storage/repository.py \
  src/kunjin/funds/sources.py \
  src/kunjin/funds/service.py \
  src/kunjin/services/research.py \
  src/kunjin/funds/peers \
  tests/fixtures/funds/fundcode_search.js \
  tests/integration/test_cli.py \
  tests/test_smoke.py \
  tests/unit/test_schema_v5.py \
  tests/unit/test_schema_v6.py \
  tests/unit/test_peer_models.py \
  tests/unit/test_peer_sources.py \
  tests/unit/test_peer_classification.py \
  tests/unit/test_peer_analytics.py \
  tests/unit/test_peer_store.py \
  tests/unit/test_peer_service.py \
  tests/unit/test_peer_research.py \
  tests/unit/test_fund_disclosure_service.py \
  docs/superpowers/specs/2026-07-11-kunjin-phase-5b-peer-and-overlap-design.md \
  docs/superpowers/plans/2026-07-11-kunjin-phase-5b-peer-and-overlap-research.md
git commit -m "feat: add fund peer and overlap research"
git push origin main
```

If the Codex process still cannot write `.git/index`, do not bypass the sandbox.
Report the exact verified staging, commit, and push commands for the user to run.

## Final Acceptance Checklist

- [ ] Candidate discovery uses only the static tier-2 directory and never a
  platform performance rank.
- [ ] Candidate ordering is deterministic and validates no more than 40
  discovered codes.
- [ ] Current peer groups contain 2 to 20 members and have an explicit current
  pointer.
- [ ] Classification explains type, management style, benchmark family, and A/C
  sibling relationships.
- [ ] 90-day, 365-day, and manager-team metrics use aligned formal-NAV dates.
- [ ] Missing history is `insufficient_data`, not a shortened hidden window.
- [ ] Pairwise overlap uses minimum shared disclosed weights and exact security
  codes.
- [ ] Top-ten disclosure is never described as full portfolio overlap.
- [ ] Portfolio look-through reports omitted funds and coverage rather than
  treating missing data as zero.
- [ ] Per-metric ordering exists without a universal score.
- [ ] Explicit compare accepts 2 to 10 unique codes and reports comparability
  warnings.
- [ ] Read commands perform no network synchronization.
- [ ] Existing peer groups refresh independently during daily sync.
- [ ] Partial failures preserve the last successful peer group.
- [ ] Every result exposes data dates, versions, coverage, sources, warnings,
  and errors.
- [ ] No automatic trade or platform write path exists.
- [ ] The KunJin Skill requires explicit user intent before Codex gives an
  investment recommendation and preserves opposing evidence and limitations.
