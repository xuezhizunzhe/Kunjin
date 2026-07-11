# KunJin Fund Research Assistant Design

Date: 2026-07-11
Status: Approved design awaiting written-spec review

## 1. Purpose

KunJin is a local, read-only fund research assistant operated through Codex. It removes the need to repeatedly capture and send screenshots by synchronizing personal holdings from Yangjibao, collecting public fund and market data, calculating reproducible analytics, and presenting evidence-based explanations suitable for a beginner.

KunJin is not an automatic trading system and does not operate Alipay or place fund orders. It produces research conclusions such as "worth further research", "insufficient evidence", "watch cautiously", and "high risk". The user remains responsible for investment decisions.

## 2. Goals

- Use Codex as the single user-facing entry point.
- Read personal fund accounts and holdings from Yangjibao after one-time QR authorization.
- Save daily snapshots so analysis does not depend on the current availability of an unofficial interface.
- Synchronize public fund NAV, manager, fee, benchmark, size, holdings, announcement, index, sector, ETF, stock, capital-flow, and news data.
- Explain portfolio performance, concentration, overlap, style exposure, and major risk sources.
- Research a candidate fund using both supporting and opposing evidence.
- Analyze market and sector conditions without equating recent price strength with investment merit.
- Preserve the user's original thesis and later review whether a correct result came from sound reasoning or luck.
- Attach source, observation time, freshness, and evidence level to material conclusions.

## 3. Non-goals

- Logging in to or controlling Alipay.
- Buying, redeeming, converting, or rebalancing funds.
- Modifying Yangjibao accounts or holdings.
- Predicting short-term price movements with certainty.
- Producing automatic buy, add, reduce, or sell instructions.
- Building a full replacement mobile or web fund application in phase one.
- Treating platform rankings, recent returns, or a single weighted score as proof of future quality.
- Deploying an always-on MCP server before the CLI and data model are stable.

## 4. User Context and Required Corrections

The initial learning portfolio was selected largely through recent returns, historical platform ratings, maximum drawdown and recovery figures, fund holdings, fund age, and manager history. KunJin must explicitly detect and teach against common errors exposed by this process:

- Performance chasing and recency bias.
- Assuming historical excellence implies future certainty.
- Using the same evaluation method for active, index, and sector funds.
- Ignoring manager-tenure boundaries when reading historical returns.
- Ignoring hidden overlap among AI, semiconductor, technology, and broad technology-index funds.
- Reading holdings without considering publication delay or position concentration.
- Forming a favorable conclusion first and searching only for confirming evidence.
- Confusing a correct outcome with a correct decision process.

Fund codes and names supplied across conversations have not always been identical. The system must validate code, name, share class, and effective dates before merging records. A/C share classes are separate instruments even when they share an underlying portfolio.

## 5. Architecture

KunJin uses a Codex Skill, a local Python CLI, deterministic analytics, SQLite storage, and scheduled synchronization.

```text
Codex conversation
        |
KunJin Codex Skill
        |
kunjin CLI with stable JSON contracts
        |
+----------------+----------------+----------------+
| synchronization| analytics      | reports        |
| Yangjibao      | return/risk    | portfolio      |
| public funds   | overlap/style  | fund research  |
| market/sectors | benchmark      | market/weekly  |
| news/notices   | evidence rules | learning review|
+----------------+----------------+----------------+
        |
SQLite normalized data + immutable raw snapshots
```

Codex interprets user intent and explains results. The CLI performs retrieval, normalization, validation, and calculations. Language-model reasoning must not replace deterministic calculations for NAV returns, drawdown, recovery time, volatility, correlations, fees, or allocation totals.

## 6. Project Layout

```text
KunJin/
|-- pyproject.toml
|-- README.md
|-- src/kunjin/
|   |-- cli/
|   |-- adapters/
|   |-- storage/
|   |-- analytics/
|   |-- research/
|   |-- reports/
|   |-- models/
|   `-- security/
|-- tests/
|   |-- unit/
|   |-- integration/
|   |-- fixtures/
|   `-- golden/
|-- integrations/codex/kunjin-fund/
|-- scripts/
|   |-- install_skill.sh
|   `-- install_launchd.sh
`-- docs/superpowers/specs/
```

The repository copy under `integrations/codex/kunjin-fund/` is the source of truth for the Skill. The installer may create or update only `/Users/yanzihao/.codex/skills/kunjin-fund/`. Existing Skills must remain byte-for-byte untouched.

The first release does not modify `/Users/yanzihao/.codex/AGENTS.md`. After the Skill passes installation and invocation tests, a separate reviewed change may append a narrowly scoped `KunJin Fund Skill` section. Existing AGENTS.md content must remain byte-for-byte unchanged.

## 7. Runtime Data and Configuration

Runtime state is kept outside Git:

```text
~/.local/share/kunjin/kunjin.db
~/.local/share/kunjin/snapshots/
~/.local/state/kunjin/logs/
~/.config/kunjin/config.toml
```

The Yangjibao token is stored in macOS Keychain and is not stored in the database, configuration, Skill, repository, report, or logs.

## 8. Data Sources and Reliability

### 8.1 Personal portfolio

Yangjibao is the initial source for account, position, share, value, and profit observations. Access uses a QR code scanned with the Yangjibao app. The adapter is read-only and limited to an audited endpoint allowlist.

Yangjibao observations are not treated as authoritative Alipay transaction confirmations. If exact transaction lots, subscription fees, redemption fees, dividends, or confirmation dates are unavailable, the system marks them as unavailable rather than reconstructing them from current holdings.

### 8.2 Public fund data

AkShare-backed public sources and Eastmoney/Tiantian-style public endpoints may be used behind adapters with explicit provenance. Each data type defines a primary source, fallback source, freshness policy, and validation rules. An adapter can be replaced without changing analytics consumers.

### 8.3 Market and sector data

The project may reuse audited concepts or code from `simonlin1212/a-stock-data`, subject to license compatibility and code review. A-share indices, ETFs, industry/concept sectors, underlying stocks, capital flows, announcements, and market breadth are normalized behind KunJin interfaces.

### 8.4 News and announcements

Source priority is fund companies, exchanges, regulators, and official announcements, followed by established financial media. News is evidence of a reported event or opinion, not proof of market causality.

### 8.5 Freshness

Every observation stores source, source timestamp when available, retrieval timestamp, market date, data type, and freshness status. Stale data is never silently described as current.

## 9. Data Flow

1. A scheduled or on-demand sync creates a `sync_run` record.
2. The adapter retrieves data using HTTPS and the read-only allowlist.
3. The original response is saved as an immutable, redacted raw snapshot.
4. Parsing converts data into versioned normalized models.
5. Identity validation checks fund code, name, share class, account, and effective date.
6. Database writes occur transactionally; invalid batches do not replace valid data.
7. Analytics record their input dataset versions and calculation parameters.
8. Reports combine facts, calculations, inferences, conflicts, and missing-data warnings.
9. Codex explains the structured result in beginner-appropriate language.

## 10. Core Data Model

- `data_sources`: provenance, reliability tier, supported data types.
- `sync_runs`: trigger, start/end time, status, counts, and errors.
- `raw_snapshots`: redacted original payload, checksum, and parser version.
- `accounts`: Yangjibao account identity and observation metadata.
- `positions`: dated position snapshots, shares, value, observed cost/profit fields.
- `funds`: code, name, type, share class, benchmark, and status.
- `fund_nav`: formal NAV and separately identified intraday estimates.
- `fund_managers`: manager identity and exact tenure intervals.
- `fund_fees`: subscription, management, custody, sales-service, and redemption rules.
- `fund_holdings`: report period, publication date, stock/bond/sector weights.
- `benchmarks`: benchmark definitions and time series.
- `market_series`: index, ETF, sector, stock, breadth, valuation, and flow observations.
- `news_items`: publisher, URL, publication time, category, and linked entities.
- `investment_theses`: user rationale, horizon, expected mechanism, risks, invalidation conditions.
- `analysis_results`: metric, inputs, parameters, result, and calculation version.
- `reports`: report type, as-of time, source set, warnings, and rendered content.

Raw, normalized, calculated, and interpreted data remain separate.

## 11. Analysis Capabilities

### 11.1 Portfolio diagnosis

- Total observed investment, value, daily profit, and cumulative profit when available.
- Per-fund contribution to portfolio movement.
- Allocation by fund type, theme, sector, index, manager, and underlying security.
- Look-through overlap using latest published holdings with explicit report-date warnings.
- Concentration, HHI, correlation clusters, and dominant risk factors.
- Drawdown and recovery analysis from available formal NAV and snapshot history.
- Identification of hidden duplication among technology-related funds.
- Separation of market, sector, manager/style, fee, and timing hypotheses; no unsupported single-cause claim.

### 11.2 Fund research by type

- Index funds: tracking difference/error, fees, size, liquidity where relevant, index methodology, valuation.
- Active equity/mixed funds: manager-tenure returns, benchmark excess return, rolling performance, style stability, concentration, turnover when available.
- Sector/theme funds: theme purity, concentration, cycle, valuation, trend, crowding, and catalyst risks.
- Bond/fixed-income funds: duration, credit exposure, interest-rate sensitivity, drawdown, and liquidity when data permits.

Shared metrics include rolling returns, maximum drawdown, drawdown duration, recovery time, volatility, downside volatility, return-to-drawdown measures, fund-size changes, fee drag, and holdings staleness.

### 11.3 Candidate validation

For a watched fund, KunJin records the user's thesis, collects supporting and opposing evidence, checks whether historical performance belongs to the current manager, tests overlap with existing holdings, compares appropriate peers, identifies cheaper or more stable alternatives, and defines invalidation conditions.

### 11.4 Market and sector analysis

Sector assessment uses six distinct dimensions:

- Trend and market breadth.
- Valuation and historical percentile.
- Earnings and industry fundamentals.
- Trading volume, ETF shares, and capital flows.
- Policy, industry, and company catalysts.
- Crowding, turnover, recent acceleration, and consensus risk.

"Recently strong", "fundamentals improving", "valuation reasonable", and "suitable for the watchlist" are separate conclusions and must not be substituted for one another.

### 11.5 Learning review

The system preserves what the user knew, expected, and considered invalidating at decision time. Later reviews distinguish outcome quality from decision quality and flag performance chasing, platform-rating dependence, manager-change errors, duplicated themes, and confirmation bias.

## 12. Evidence Contract

Every material statement is labeled as one of:

- `verified_fact`: directly supported by formal NAV, filing, announcement, or equivalent source.
- `deterministic_calculation`: reproducible from identified inputs and parameters.
- `reasoned_inference`: supported but not uniquely proven.
- `market_opinion`: attributed view from media or an institution.
- `insufficient_data`: the available evidence does not support a conclusion.

Reports must include opposing evidence and material data limitations. Conflicting sources are retained and surfaced. KunJin does not choose the value that best supports a preferred narrative.

No universal weighted fund score is used as the primary conclusion. Classification-specific metrics and evidence are displayed separately.

## 13. CLI and Codex Contract

Initial command families:

```text
kunjin auth login yangjibao
kunjin auth status
kunjin auth revoke yangjibao
kunjin sync portfolio
kunjin sync funds [codes...]
kunjin sync market
kunjin sync news
kunjin status
kunjin portfolio show
kunjin portfolio analyze
kunjin fund research CODE
kunjin fund compare CODE...
kunjin market sectors
kunjin market research NAME
kunjin thesis add|list|review
kunjin report weekly
```

Commands provide a stable JSON mode for Codex and a concise human-readable mode for local diagnostics. JSON responses include schema version, as-of time, freshness, sources, warnings, and errors.

The `kunjin-fund` Skill recognizes requests to synchronize Yangjibao, analyze personal funds, research a fund code, compare funds, study a sector, review a thesis, or generate a weekly fund report. It calls the CLI rather than inventing unavailable data.

## 14. Synchronization

- Post-close daily sync stores formal NAV, personal snapshots, and market observations.
- On-demand sync runs for questions containing current/latest/today intent.
- News and market data use short caches; quarterly holdings and manager records use longer caches.
- macOS `launchd` performs scheduling without an always-on application server.
- Scheduled failures are recorded and visible through `kunjin status`.

## 15. Security and Privacy

- HTTPS is mandatory. KunJin does not fall back to plaintext HTTP.
- The Yangjibao adapter uses only audited read endpoints.
- Tokens, Authorization values, QR contents, and sensitive headers are redacted before logging or snapshot storage.
- Debug mode cannot print credentials.
- File permissions restrict runtime data to the current macOS user.
- Revocation removes local credentials and disables synchronization.
- KunJin does not read browser cookies or profiles.
- Tests and Git history contain no live credentials or unredacted personal payloads.

The Yangjibao interface appears to be an internal browser-plugin interface rather than a documented developer platform. Interface instability and applicable service terms remain explicit operational risks.

## 16. Error Handling and Degradation

- Adapter failures do not delete or overwrite previously valid history.
- Each result reports the most recent successful sync and data age.
- Stale data is marked as stale after type-specific thresholds.
- Formal NAV and intraday estimates are stored separately; formal NAV controls settled historical performance.
- Identity mismatches stop merging and create a validation error.
- Insufficient history produces `insufficient_data`, not a fabricated metric.
- A fallback source is used only when provenance remains visible and validation passes.
- News correlation is phrased as possible relevance unless stronger causal evidence exists.
- Partial reports list omitted sections and reasons.

## 17. Testing Strategy

- Unit tests for returns, drawdown, recovery, volatility, correlations, HHI, overlap, and benchmark calculations.
- Boundary tests for missing NAV, non-trading days, zero history, manager changes, and share-class mismatches.
- Adapter contract tests against redacted fixtures.
- Integration tests for transactional synchronization and schema migrations.
- Failure tests for timeouts, malformed payloads, stale caches, conflicts, and fallback sources.
- Security tests asserting that credentials never appear in logs, snapshots, reports, or errors.
- Golden-file tests for structured portfolio, fund, market, and weekly reports.
- Skill installation tests proving that only `kunjin-fund` is created or updated.
- A final live smoke test uses QR authorization and audited read-only requests.

## 18. Phase-one Acceptance Criteria

1. Codex can trigger synchronization and list Yangjibao accounts and holdings.
2. Codex can explain portfolio profit observations, concentration, and overlapping exposure.
3. Codex can research a specified fund with supporting evidence, opposing evidence, source times, and limitations.
4. Codex can analyze major market sectors using the six-dimensional framework.
5. Codex can generate a weekly report with freshness and evidence labels.
6. Source failure produces visible degradation and never a false current-data claim.
7. No existing Codex Skill is modified.
8. No Alipay or Yangjibao write operation exists.
9. No live credential is stored outside macOS Keychain or exposed in logs.
10. All deterministic analytics have reproducible tests.

## 19. Delivery Sequence

1. Scaffold the Python package, CLI contract, storage paths, logging, and tests.
2. Implement schema, migrations, raw snapshots, and synchronization records.
3. Implement and audit Yangjibao QR authentication and read-only portfolio sync.
4. Implement formal NAV and fund metadata adapters with provenance and fallback rules.
5. Implement deterministic portfolio and fund analytics.
6. Implement market/sector and announcement/news adapters.
7. Implement evidence-structured research and reports.
8. Create and install the isolated `kunjin-fund` Codex Skill.
9. Install optional `launchd` scheduling.
10. Run live read-only smoke tests and review any later AGENTS.md addition separately.

## 20. Future Evolution

After CLI contracts and analytics are stable, the same service layer may be exposed through a local MCP server. A small visualization UI may be added only when it materially improves inspection of time series, allocations, or overlap. Neither evolution changes the read-only boundary or makes Codex-generated trading decisions automatic.
