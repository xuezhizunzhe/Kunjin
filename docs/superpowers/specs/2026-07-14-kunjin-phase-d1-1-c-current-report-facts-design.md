# KunJin Phase D1.1-C Current Report Facts Design

Date: 2026-07-14
Status: Approved design, pending implementation plan

## 1. Purpose

D1.1-C completes the current-report portion of Phase D1. It turns the accepted
D1.1-B official-document and isolated OLE conversion path into a bounded,
automatic source of current risk facts.

The owner should not have to create one document or one parser workflow for
each held fund. A normal supported fund code must enter the same automatic
pipeline, and manual engineering should be required only when an official site
or document shape is outside the audited coverage.

D1.1-C has two connected goals:

1. Stop synchronizing unnecessary periodic-report history for a current
   classification.
2. Extract only the current observations already required by Policy V1 from
   the newest necessary official reports.

The result remains `research_only`. D1.1-C does not provide a purchase
direction, target allocation, contribution amount, or execution instruction.

## 2. Current State

D1.1-B now provides:

- authenticated official-document discovery and retrieval;
- native HTML, PDF, and DOCX parsing;
- pinned Docker LibreOffice conversion for legacy OLE documents;
- immutable artifact, provenance, parse-run, parse-result, and fact records;
- classification manifest V2 bindings;
- exact failure stages and reasons; and
- real acceptance across four representative public funds.

The v13 three-fund regression parsed 47 of 47 discovered documents. That result
proves document reachability but also demonstrates that the current sync is too
broad for routine personal use. Current classification evidence remains stale
or incomplete because candidate selection and report-fact extraction do not yet
implement the D1.1-C contract.

## 3. Design Principles

### 3.1 Automatic by fund code

A newly observed or researched fund uses the same sync and classification
commands as an existing fund. No fund-specific document creation step is part
of the normal workflow.

### 3.2 Latest evidence without historical fallback

Current classification uses the newest required official candidate for each
periodic kind. If that candidate fails, KunJin preserves the failure and does
not promote an older report to current evidence.

### 3.3 Observation is not mandate

A legal maximum describes what a fund may hold. A current observation describes
what the report says it actually held at the report date. The two fact classes
remain separate and cannot substitute for one another.

### 3.4 Explicit facts only

KunJin extracts a current fact only from an explicit clause or a structurally
bound table. Silence, incomplete classifications, rounded totals, platform
labels, and inferred remainders remain missing evidence.

### 3.5 Failure closed

Missing, stale, conflicting, malformed, or unsupported evidence cannot improve
the product family, risk bucket, portfolio role, or evidence status.

## 4. Scope

### 4.1 Periodic document kinds

D1.1-C applies current-candidate selection to:

- `quarterly_report`
- `semiannual_report`
- `annual_report`

Product summaries, prospectus updates, fund contracts, index methodologies,
and classification announcements keep their existing current-document rules.

### 4.2 Current fact allowlist

The implementation may extract only Policy V1 facts in the following allowlist.

Common product observations:

- `current_stock_asset_allocation_percent`
- `current_bond_asset_allocation_percent`
- `current_cash_asset_allocation_percent`
- `current_hong_kong_asset_allocation_percent`
- `current_largest_security_weight_percent`
- `current_top_ten_holdings_weight_percent`
- `current_largest_industry_name`
- `current_largest_industry_weight_percent`
- `current_industry_count`
- `holdings_evidence_complete`

Additional fixed-income observations:

- `current_effective_duration`
- `current_weighted_average_maturity_days`
- `current_convertible_bond_asset_allocation_percent`
- `current_exchangeable_bond_asset_allocation_percent`
- `current_high_quality_fixed_income_percent`
- `current_below_aa_plus_exposure_percent`
- `current_unrated_non_sovereign_exposure_percent`
- `current_gross_leverage_percent`
- `current_largest_non_sovereign_issuer_percent`

No new Policy V1 threshold is introduced in D1.1-C.

## 5. Latest Candidate Selection

### 5.1 Selection key

For each periodic document kind, candidates are ordered only by authenticated
publication time. The selected document's report period is then authenticated
through the existing title, cover, and document-contract validation.

Canonical URL ordering is never financial evidence and may not resolve a
substantive tie.

### 5.2 Exact tie handling

If the newest publication instant contains multiple distinct official URLs for
the same fund and document kind, selection fails closed as a candidate conflict.
The candidates remain recorded, but none becomes current evidence.

Byte-identical or canonically identical duplicate index entries may deduplicate
only through existing authenticated identity rules. Title similarity alone is
insufficient.

### 5.3 Bounded synchronization

One refresh attempts at most one selected candidate for each periodic kind.
Older periodic candidates may remain in immutable history but are not retrieved
again solely for current classification.

The normal upper bound is therefore three periodic downloads per fund refresh:
one quarterly, one semiannual, and one annual report. A kind with no candidate
produces explicit missing evidence rather than an invented document.

### 5.4 No fallback

If the selected newest candidate fails discovery validation, retrieval,
container validation, conversion, parsing, or persistence:

- the exact terminal failure is recorded;
- the refresh completes with that failed current candidate;
- older successful artifacts remain historical only; and
- classification cannot use an older artifact as current evidence.

## 6. Current Fact Extraction

### 6.1 Supported structures

Extraction accepts only:

- an explicit sentence that binds a label, value, unit, and current report
  context; or
- a table whose headers and row structure unambiguously bind the same fields.

Every accepted fact retains:

- fact kind and normalized value;
- unit;
- report period;
- publication time through the source document;
- document kind;
- page number or section when available;
- bounded source excerpt;
- source-document fingerprint;
- parser provenance; and
- exact confidence state.

### 6.2 Asset allocation

Stock, bond, and cash percentages require explicit current-period asset
allocation rows or sentences. Values must use the disclosed denominator. Rows
using fund net assets and rows using total fund assets remain distinguishable
through their units and cannot be silently combined.

The parser does not compute a missing category as `100 - disclosed values`.

### 6.3 Security and top-ten concentration

Largest-security weight requires an explicit maximum row or a complete sortable
security table with a single supported denominator.

Top-ten weight requires either an explicit top-ten total or exactly ten bound
rows sharing the same denominator and disclosure scope. It is tagged as a
top-ten disclosed observation and never becomes complete-holdings evidence.

`holdings_evidence_complete=true` requires an explicit complete-holdings scope
and a fully parsed supported holdings table. Top-ten, major-position, or partial
appendix disclosure cannot emit that fact. An incomplete table emits no
completeness fact rather than `false`; `false` is reserved for an explicit
authenticated incomplete scope.

### 6.4 Industry concentration

Largest-industry name and weight require a complete, structurally bound industry
distribution for the stated classification scope. Industry count is emitted
only when the report identifies the distribution as complete and every included
row is parseable.

Missing rows, an `other` bucket with unknown composition, or a partial top list
prevents `current_industry_count` from being emitted. A largest named industry
may still be emitted only when the table explicitly establishes it as the
largest disclosed category under one denominator.

### 6.5 Fixed-income observations

Duration and weighted average maturity remain different fact kinds. The parser
does not convert one into the other.

Credit-quality observations require an explicit rating distribution and a
declared scope. AA+ and above, below-AA+, and unrated non-sovereign values may be
aggregated only from a complete supported rating table. Missing ratings do not
become zero.

Convertible bonds, exchangeable bonds, Hong Kong assets, leverage, and issuer
concentration require explicit current observations. Derivative and broader
foreign-exposure gates remain bound to existing legal mandate facts in Policy
V1; D1.1-C does not invent unused current fact kinds. Legal prohibitions and
mandate ceilings remain legal facts, not current observations.

Largest non-sovereign issuer concentration may be calculated only from a
complete supported issuer table after explicitly excluding sovereign and policy
bank categories under Policy V1 vocabulary. Related issuers are not aggregated
unless the official report provides that grouping.

### 6.6 Ambiguity and conflicts

Equivalent facts deduplicate by the existing fingerprint rules. Different
values for the same current fact and report period remain visible and cause a
conflict. Later downloads do not overwrite an authenticated conflict.

## 7. Freshness

Freshness is based on report period and Policy V1 disclosure deadlines, not on
retrieval time.

For each required current fact, classification binds the newest selected report
that is valid for that fact. A successfully downloaded old annual report remains
stale after its policy deadline even if it was downloaded today.

When a newer report becomes due:

- the previous report remains authenticated history;
- it no longer satisfies the current gate;
- a missing or failed newer report produces stale or missing evidence; and
- historical classification remains readable but cannot authorize a current
  result.

## 8. Persistence And Authentication

D1.1-C adds Schema V13 with an immutable
`fund_document_selection_manifests` record keyed by `refresh_run_id`. Existing
artifact, fact, parse-result, and classification IDs must not be rebuilt or
rewritten.

The exact selection manifest contains:

- manifest version and selection-policy checksum;
- fund code and refresh-run ID;
- every discovered periodic candidate fingerprint, kind, publication time, and
  canonical official URL;
- the selected fingerprint for each periodic kind, or an exact `missing` or
  `conflicted` state; and
- a canonical selection checksum.

It stores no response body, converted content, local path, or parser exception.
The record is inserted before any selected candidate is downloaded and is
immutable after commit.

One candidate attempt atomically persists:

1. the authenticated original artifact;
2. parser provenance;
3. parse run and parse result;
4. the exact fact set; and
5. terminal candidate success.

Failure persists only the allowed terminal failure record. It cannot leave a
successful artifact without its authenticated parse bindings.

New classifications use manifest V3, which retains every V2 binding and adds
the refresh-run ID, selection-policy checksum, and selection-manifest checksum.
Historical V1 and V2 bytes remain unchanged and continue to authenticate under
their original decoders. Authenticated V3 readback rejects a mixed, partial,
stale, or tampered selection, fact, freshness, or provenance binding.

## 9. Service And CLI Behavior

The existing commands remain the public interface:

```bash
kunjin --json sync fund-documents CODE
kunjin --json fund classify CODE
kunjin --json fund classification-evidence CODE
kunjin --json fund classification-history CODE
```

No per-fund manual document command is added.

`sync fund-documents` reports selected current candidates, successful facts,
and exact safe failures without exposing managed paths, converted HTML, raw
bodies, parser exception text, or personal data.

Financially incomplete results continue to exit zero when the technical command
completed. Invalid arguments and technical persistence or authentication
failures exit nonzero under the existing public error contract.

## 10. Error Handling

The existing safe document failure stages and reasons remain authoritative.
D1.1-C must not add raw exception strings to JSON or logs.

Selection state is separate from `SafeDocumentFailure`:

- no candidate uses `current_periodic_candidate_missing`;
- an exact newest-candidate tie uses `current_periodic_candidate_conflict`; and
- a selected candidate that later fails retains its exact existing
  `failure_stage` and `failure_reason`.

The two new selection codes are schema-validated D1 evidence codes, not parser,
network, or conversion errors. A missing kind records no candidate run. A
conflicted kind records every tied fingerprint in the selection manifest and
downloads none of them.

## 11. Testing

### 11.1 Candidate selection

- Select exactly one newest candidate per periodic kind.
- Reject exact newest-time ties across distinct official URLs.
- Do not use lower-tier ordering to break a tie.
- Do not retrieve older candidates after the newest candidate fails.
- Preserve older authenticated artifacts as history only.

### 11.2 Parser facts

Use table and text fixtures for every allowlisted fact. Include real-shape
fixtures derived from public reports while excluding unnecessary raw content
and local paths.

Tests must prove that:

- explicit complete values are extracted;
- mandate limits do not become observations;
- missing columns and unknown denominators remain missing;
- top-ten disclosure does not become complete holdings;
- duration does not become maturity or vice versa;
- missing credit ratings do not become zero exposure;
- incomplete industry lists do not produce an industry count; and
- conflicting facts remain conflicted.

### 11.3 Freshness and persistence

- Test every Policy V1 report-period deadline boundary.
- Test active current evidence, stale history, failed replacement, and exact-tie
  conflict states.
- Test atomic persistence and recovery at each write boundary.
- Test classification manifest authentication after fact, provenance, selection,
  and freshness tampering.

### 11.4 Repository verification

Before completion, run:

- focused unit and integration tests;
- the full pytest suite;
- Ruff checks for the repository;
- formatting checks for touched Python files;
- compileall;
- `pip check`;
- database migration tests if the schema changes;
- privacy and output-contract smoke tests; and
- `git diff --check`.

## 12. Real Acceptance

Use fresh, isolated data and state directories and the accepted pinned Docker
image. First rerun the four representative funds already used by D1/D1.1:

- one broad-index or evidence-incomplete index candidate;
- one sector/theme fund;
- one ordinary-bond fund; and
- one equity-mixed fund.

Acceptance requires:

1. At most one attempted current candidate per periodic kind and fund.
2. At least one official current asset-allocation path completing end to end.
3. The fixed-income sample either satisfying every Policy V1 current gate or
   preserving every exact missing-evidence code.
4. No old-report fallback after a selected current candidate failure.
5. Classification evidence and history authenticating all selection, fact,
   freshness, and parser-provenance bindings.
6. No purchase direction, amount, target, or Phase C real-product mapping.

After representative acceptance, run the same automatic pipeline against the
owner's synchronized current holdings. This is a coverage audit, not a promise
that every manager and document shape is supported. Each unsupported case is
reported separately and does not weaken the general evidence rules.

## 13. Rollout

Implementation proceeds in bounded steps:

1. latest-per-kind selection and no-fallback persistence;
2. common asset-allocation and concentration facts;
3. fixed-income current facts;
4. freshness and authenticated classification integration;
5. representative live acceptance;
6. current-holdings coverage audit; and
7. independent financial and beginner-workflow review.

Each step must pass focused tests before the next begins. No substep may improve
classification status merely by omitting missing or conflicting evidence.

## 14. Non-Goals

- No handwritten OLE parser or OCR.
- No full historical-report analytics.
- No inference of complete holdings from top-ten disclosure.
- No broad-index classification from a name or benchmark alone.
- No high-quality bond result from `pure bond` wording alone.
- No lower-tier source promotion.
- No personal amount access or trade output.
- No D2 overlap/correlation implementation.
- No D3 product-selection or execution checks.
- No Phase E monitoring or rebalancing policy.
- No 90 percent beginner-help claim from D1.1-C alone.

## 15. Objective Completion Review

After real acceptance, an independent review must report:

- actual automatic coverage across the representative and current-holdings
  samples;
- the number of periodic documents attempted before and after D1.1-C;
- which current facts became usable and which remained missing;
- whether ordinary new funds require manual engineering;
- false-positive and false-confidence risks;
- the exact remaining D1, D2, D3, and Phase E gaps; and
- a fresh evidence-based beginner-workflow score.

The review must explicitly answer whether KunJin provides 90 percent or more of
the help a beginner needs to purchase funds. D2, D3, and Phase E receive zero
credit until independently implemented and accepted.
