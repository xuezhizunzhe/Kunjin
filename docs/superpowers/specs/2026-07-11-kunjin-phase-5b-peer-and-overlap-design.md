# KunJin Phase 5B Peer and Overlap Research Design

## 1. Goal

Phase 5B adds reproducible peer grouping, multi-dimensional fund comparison,
and disclosed-holdings overlap analysis for A-share and domestic public funds.
It builds on phase 5A fund disclosures and the existing formal-NAV history,
portfolio snapshots, and evidence labels.

The feature is intended for a beginner investor who wants KunJin to answer:

- Which funds are genuinely comparable to this fund?
- Is another fund more stable, cheaper, or less duplicated with my portfolio?
- How much of two funds' latest disclosed holdings overlap?
- Which conclusions are supported, and which are limited by missing or stale data?

The system does not create a universal score, silently reuse a platform rank,
or place orders. When the user explicitly asks for a buy, hold, add, reduce, or
sell recommendation, Codex may interpret the structured evidence, but must show
the evidence date, major opposing evidence, uncertainty, horizon, and
invalidation conditions.

## 2. Scope

### 2.1 Included

- Held funds, user-specified candidates, and a bounded automatically discovered
  candidate set.
- At most 20 valid members in one peer group.
- Versioned peer-group rules and membership persisted in SQLite.
- Three-month, one-year, and current-manager-tenure comparisons.
- Return, volatility, maximum drawdown, drawdown recovery, fees, size, manager
  tenure, holdings concentration, and disclosed-holdings overlap.
- Pairwise comparison for 2 to 10 explicitly supplied fund codes.
- Current-portfolio disclosed overlap based on the latest Yangjibao position
  observation and latest available fund disclosures.
- Independent freshness, source, coverage, warning, and conflict reporting.
- Partial failure that retains the last successful peer group and comparison
  evidence.

### 2.2 Excluded

- Full-market ranking across every domestic public fund.
- A single opaque composite score or guaranteed-return label.
- Platform star ratings or platform ranks as comparison facts.
- Automatic news ingestion, valuation, earnings, persistent capital-flow, or
  crowding analysis.
- Exact full-portfolio overlap when only top-ten holdings are disclosed.
- Automatic Alipay, Yangjibao, or broker operations.

## 3. Candidate Universe

The initial universe combines:

1. Funds currently observed in the latest Yangjibao portfolio snapshot.
2. Fund codes explicitly supplied by the user.
3. A bounded candidate list discovered from an allowlisted HTTPS public-fund
   directory or ranking endpoint.

The discovery endpoint is tier-2 fallback evidence and is used only to enumerate
possible candidates. Its rank, return, category rank, recommendation, or score
must not enter KunJin's comparison calculations.

Candidate discovery is deterministic for the same response and rule version.
The system takes a bounded upstream page, normalizes six-digit codes, removes
duplicates and inactive funds, and then validates candidates through KunJin's
own phase 5A profile, manager, fee, size, holdings, announcement, and formal-NAV
sync paths. The final peer group contains no more than 20 valid members.

If discovery is unavailable, explicit and previously stored peer members remain
usable. The result reports `candidate_discovery_unavailable` instead of treating
the peer group as complete.

## 4. Peer Classification

Peer groups use an explicit `PEER_RULE_VERSION`. Membership is based on verified
or clearly labeled fallback facts, not name similarity alone.

The first rule version considers:

- Normalized fund type and investment scope.
- Active versus passive management when disclosed or deterministically derived
  from an explicit index-fund type.
- Benchmark family after conservative text normalization.
- A/C share-class relationship.
- Establishment date and available formal-NAV history.
- Current manager tenure for manager-period comparisons.

A/C share classes may share the same underlying holdings identity, but remain
separate comparison members because fees and NAV histories differ. The result
includes a `share_class_sibling` relationship so users are not shown the two
classes as independent portfolio diversification.

Unknown management style, missing type, ambiguous benchmark, identity conflict,
or inactive status prevents automatic membership. Such a fund may still appear
in an explicit `fund compare` request, with a visible comparability warning.

Each peer group stores a human-readable rule explanation, input source IDs,
rule version, creation time, member acceptance reason, and member rejection or
warning reason.

## 5. Comparison Metrics

All deterministic performance metrics use formal NAV. Intraday estimated NAV is
never substituted for historical peer comparison.

### 5.1 Aligned windows

The default windows are:

- Three months: target 90 calendar days.
- One year: target 365 calendar days.
- Current manager tenure: from the latest active manager's start date.

For each fixed window, all members use one common effective end date: the latest
formal-NAV date available to every included member. The start observation is the
latest formal NAV on or before the target start date within 7 calendar days.
Members without sufficient aligned history receive `insufficient_data` for that
window and are not silently compared over a shorter interval.

Where multiple current co-managers began on different dates, the manager-tenure
window starts at the latest start date among the active manager team. This avoids
attributing performance to a team configuration before all current members were
in place.

### 5.2 Metrics

For each supported window, calculate:

- Total formal-NAV return.
- Annualized daily volatility when enough observations exist.
- Maximum drawdown.
- Drawdown trough date and recovery date when recovered.
- Observation count and effective start/end dates.

Additional dimensions include:

- Current manager names and exact tenure dates.
- Management, custody, sales-service, subscription, and redemption fee rules.
- Latest fund size and size-report date.
- Latest disclosed top-holdings concentration.
- Candidate overlap with the current portfolio.
- Data coverage and stale or missing sections.

Size stability uses the latest five quarterly observations with non-missing net
assets and requires at least three observations. It reports the earliest-to-
latest change and the population standard deviation of quarter-to-quarter
percentage changes. Fewer than three observations is `insufficient_data`.

The comparable ongoing annual fee rate is the sum of current management,
custody, and sales-service rates for the applicable share class. Subscription
and redemption rules are ordered only when their amount, holding-period, rate
kind, share class, and effective-date conditions are identical.

No exact personal fee is calculated unless the required amount, share class,
transaction route, and holding period are available. Platform promotional fee
rates remain distinct from original rates.

## 6. Layered Results

KunJin does not collapse dimensions into one composite score. It produces
independently reproducible comparisons such as:

- `return_higher`
- `volatility_lower`
- `max_drawdown_lower`
- `fee_lower_for_known_condition`
- `size_more_stable`
- `portfolio_overlap_lower`
- `manager_tenure_short`
- `holdings_stale`
- `insufficient_data`

The output may produce a deterministic ordering within a single named metric,
but never describes that ordering as overall investment merit. A summary groups
facts into `advantages`, `tradeoffs`, `data_gaps`, and `watch_reasons`.

Codex may provide an investment recommendation only when the user explicitly
asks for one. The recommendation remains an interpretation layer and must cite
the structured metrics, show opposing evidence, state the relevant 1-3 month or
6-12 month horizon, define invalidation conditions, and avoid certainty language.

## 7. Holdings Overlap

### 7.1 Pairwise overlap

For two funds, select holdings from the same report period when available. If no
common report period exists, use each fund's latest disclosed period only when
the period gap is within one quarter and emit `report_period_mismatch`.

For each shared security code:

```text
shared_weight = min(fund_a_weight, fund_b_weight)
```

The disclosed overlap is:

```text
disclosed_overlap = sum(shared_weight for every shared security code)
```

The result includes:

- Both report periods and publication dates.
- Both disclosure scopes.
- Sum of disclosed weights for each fund.
- Shared security codes, names, weights, and shared weights.
- Shared asset types so identical stock and bond codes are not merged.
- Disclosed overlap percentage.
- Industry overlap calculated with the same minimum-weight method when both
  funds use the same classification standard.

If either source exposes only top-ten holdings, the metric is named
`top10_disclosed_overlap`; it is not described as total portfolio overlap.

### 7.2 Portfolio overlap

Portfolio overlap weights each held fund's disclosed security exposure by the
fund's latest observed portfolio weight:

```text
lookthrough_weight(fund, security) = portfolio_weight(fund) * disclosed_weight(fund, security)
```

Security-level look-through weights are summed across held funds. The report
shows securities and industries reached through more than one fund, the
contributing funds, latest report periods, and total disclosed coverage.

When a held fund has no current value, no holdings, stale holdings, or an
unresolved identity, it is excluded from the calculation and included in a
coverage warning. Missing funds are never treated as zero exposure.

## 8. Data Model

SQLite schema version 6 adds:

### `fund_peer_groups`

- `id`
- `anchor_fund_code`
- `rule_version`
- `rule_key`
- `rule_description`
- `candidate_source_url`
- `candidate_source_tier`
- `candidate_source_checksum`
- `input_fingerprint`
- `created_at`
- `status`
- `warning`

The current successful group is selected by an explicit pointer, not by maximum
ID or retrieval time.

### `fund_peer_group_syncs`

- `anchor_fund_code`
- `current_peer_group_id`
- `state`: `success`, `partial`, or `source_unavailable`
- `last_attempted_at`
- `last_success_at`
- `error_code`
- `warning`

`anchor_fund_code` is the primary key. Publishing a valid group and moving the
pointer occurs in one transaction. A failed refresh updates the attempt status
without changing `current_peer_group_id`.

### `fund_peer_group_members`

- `peer_group_id`
- `fund_code`
- `membership_kind`: `anchor`, `held`, `user_supplied`, or `discovered`
- `classification_key`
- `acceptance_reason`
- `warning`
- `profile_source_document_id`

Membership is unique per peer group and fund code.
When one fund enters through multiple routes, `membership_kind` uses this stable
precedence: `anchor`, `user_supplied`, `held`, then `discovered`.

### `fund_comparison_runs`

- `id`
- `comparison_kind`: `peer`, `explicit`, or `portfolio_overlap`
- `anchor_fund_code`
- `peer_group_id`
- `calculation_version`
- `as_of`
- `status`
- `input_fingerprint`
- `result_json`
- `warning`

`result_json` stores only deterministic calculated output and identifiers for
the normalized facts used. It does not duplicate raw pages, credentials, or
unredacted portfolio payloads.

Existing tables remain the source of truth for identities, manager tenures,
fees, size, holdings, announcements, formal NAV, and portfolio observations.

## 9. Services and Boundaries

### Candidate directory adapter

- Fetches one allowlisted HTTPS candidate page with response-size and redirect
  limits matching the phase 5A security model.
- Returns normalized candidate codes and provenance only.
- Does not expose or trust upstream ranking values.

### Peer classifier

- Builds a versioned classification key from normalized fund facts.
- Explains every accepted or rejected membership decision.
- Has no network or database dependency.

### Peer synchronization service

- Discovers a bounded candidate set.
- Synchronizes profile, holdings, and formal NAV independently for each member.
- Publishes a new peer-group version atomically after validation.
- Retains the previous successful group on partial or total failure.

### Comparison engine

- Aligns formal-NAV windows.
- Calculates deterministic risk and return metrics.
- Calculates pairwise and portfolio disclosed overlap.
- Has no network dependency and accepts immutable normalized inputs.

### Research renderer

- Produces stable JSON with evidence levels, source IDs, dates, coverage,
  advantages, tradeoffs, gaps, and warnings.
- Does not generate automatic trading instructions.

## 10. CLI Contracts

```bash
kunjin --json sync fund-peers CODE [--candidate CODE ...]
kunjin --json fund peers CODE
kunjin --json fund compare CODE CODE [CODE ...]
kunjin --json portfolio overlap
```

Validation rules:

- Fund codes are exactly six digits.
- `fund compare` accepts 2 to 10 unique codes.
- `--candidate` values are user-supplied candidates and are never silently
  treated as discovered members.
- Read commands do not synchronize implicitly.
- The KunJin Skill checks freshness and invokes the corresponding sync command
  before answering a latest/current peer question.

Every JSON result contains:

- `as_of`
- `rule_version` or `calculation_version`
- `data_dates`
- `coverage`
- `sources`
- `advantages`
- `tradeoffs`
- `warnings`
- `errors`

## 11. Freshness and Failure Handling

- Candidate directory: stale after 7 days.
- Peer group: stale after 7 days or when its anchor classification changes.
- Formal-NAV comparison: stale after the common aligned end date falls behind
  the latest expected trading day.
- Holdings overlap: current or stale according to the existing statutory-report
  freshness logic from phase 5A.
- Portfolio overlap: stale when the portfolio snapshot or any included holdings
  input is stale.

Candidate failures are isolated. A failed member is omitted from calculations
and reported with its code and error. A peer group with fewer than two valid
members is `insufficient_data`. A failed refresh never deletes or replaces the
last successful group or comparison result.

Stable error and warning codes include:

- `candidate_discovery_unavailable`
- `candidate_limit_reached`
- `peer_classification_ambiguous`
- `peer_group_too_small`
- `aligned_nav_window_unavailable`
- `manager_tenure_history_insufficient`
- `holdings_unavailable`
- `holdings_stale`
- `report_period_mismatch`
- `disclosure_scope_partial`
- `portfolio_coverage_partial`

## 12. Security and Privacy

- Candidate and fund data use audited HTTPS GET requests only.
- Redirects remain on the exact allowlisted host.
- Private, loopback, link-local, and unresolved addresses are rejected.
- Response sizes and timeouts are bounded.
- Yangjibao remains read-only and its token remains in macOS Keychain.
- Peer and overlap outputs do not expose raw snapshots, tokens, request
  signatures, managed screenshot paths, or unrelated OCR text.
- No command writes to Alipay or Yangjibao.

## 13. Testing

Unit tests cover:

- Deterministic candidate normalization and limits.
- Strict peer acceptance and rejection reasons.
- A/C sibling handling.
- Active/passive and benchmark-family mismatches.
- Manager changes and co-manager tenure start dates.
- Aligned 90-day, 365-day, and manager-tenure windows.
- Missing NAV dates and insufficient history.
- Return, volatility, drawdown, trough, and recovery calculations.
- Pairwise overlap, zero overlap, partial top-ten scope, and period mismatch.
- Industry standard mismatch.
- Portfolio look-through weighting and partial coverage.
- Stable single-metric ordering without a composite score.

Integration tests cover:

- Schema version 5 to 6 migration without changing existing disclosure or
  ledger data.
- Atomic peer-group publication and previous-version retention.
- Candidate partial failure.
- Stable CLI envelopes and validation errors.
- Daily sync isolation when peer refresh fails.
- Credential and unsafe-URL scans.

Live read-only smoke tests use held fund `519755`, one A/C sibling pair when
available, and at least one explicitly supplied comparable fund. The smoke test
verifies classification explanations, common NAV dates, manager-tenure dates,
fee conditions, holdings periods, overlap scope, source tiers, and failure
degradation.

## 14. Acceptance Criteria

1. `fund peers CODE` returns a versioned, explained group with no more than 20
   validated members.
2. Platform ranking values are absent from every comparison calculation.
3. Three-month, one-year, and manager-tenure metrics use aligned formal NAV.
4. A/C siblings share holdings identity but retain separate fees and NAV results.
5. Pairwise overlap reports common securities, exact report dates, disclosed
   coverage, and partial-scope warnings.
6. `portfolio overlap` identifies duplicated disclosed securities and industries
   without treating missing holdings as zero.
7. Partial source failure preserves the previous successful group and facts.
8. Every result exposes sources, dates, coverage, rule/calculation versions,
   warnings, and errors.
9. No universal composite score or automatic trading operation exists.
10. An explicit user request may receive a Codex investment recommendation only
    as an evidence-linked interpretation with opposing evidence and invalidation
    conditions.
