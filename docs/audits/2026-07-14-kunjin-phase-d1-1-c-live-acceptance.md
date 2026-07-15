# KunJin Phase D1.1-C Live Acceptance

Date: 2026-07-15

## Scope

This acceptance verifies bounded newest-per-kind selection, real-shape current
asset-allocation extraction, immutable persistence, Manifest V3 authentication,
and strict fail-closed classification for four representative public funds. It
does not approve a fund, map a real product into a personal allocation layer,
authorize a trade, or implement D2, D3, or Phase E.

Base commit before the acceptance correction: `71def92`.

## Runtime

- Image ID: `sha256:b0b1fcf864473ec8dbcad10fa49c29b0978ce89bb4ffe0829d4607a5f6cb19a9`
- Parser version: `4-docker-libreoffice-v1`
- Provenance checksum: `d73408012e76ce6264bea8ddcaeff08027cc086c144d0b93622694ff5953c100`
- Acceptance storage: fresh isolated private data, state, and result directories
- Schema migrations: exact versions 1 through 13

The acceptance storage is local and private. No database, downloaded document,
converted HTML, canonical manifest, candidate fingerprint, unselected URL,
source excerpt, managed path, or exception text is committed.

## Command Results

The version and converter-status commands exited zero. For each of `519706`,
`164905`, `519718`, and `519755`, profile, official documents,
classification, evidence, and history exited zero.

`519718.holdings` exited one with the exact existing disclosure codes:

- `invalid_disclosure_date`
- `missing_industry_exposure`
- top-level `fund_disclosure_sync_failed`

This tier-2 holdings failure is separate from the successful official periodic-
document path and was not converted into zero exposure.

## Selection And Download Bounds

All twelve periodic kinds were `selected`. Every kind attempted exactly one
candidate and completed with `success`. Fresh isolated state proves the before
count was zero; the after count was three periodic attempts per fund and twelve
in total.

| Fund | Annual candidates | Quarterly candidates | Semiannual candidates | Attempts by kind |
| --- | ---: | ---: | ---: | --- |
| `519706` | 2 | 3 | 2 | 1 / 1 / 1 |
| `164905` | 3 | 6 | 3 | 1 / 1 / 1 |
| `519718` | 3 | 6 | 3 | 1 / 1 / 1 |
| `519755` | 3 | 6 | 3 | 1 / 1 / 1 |

For every fund the selected publication dates were:

- annual report: `2026-03-28`
- quarterly report: `2026-04-21`
- semiannual report: `2025-08-29`

No older periodic candidate was attempted after a selected result. There was no
historical fallback.

## Document And Current-Fact Results

All 26 selected official documents parsed successfully and none failed:

| Fund | Successful documents | Failed documents |
| --- | ---: | ---: |
| `519706` | 6 | 0 |
| `164905` | 7 | 0 |
| `519718` | 6 | 0 |
| `519755` | 7 | 0 |

The selected `2026-03-31` quarterly reports produced seven authenticated
current asset-allocation facts:

| Fund | Fact | Value | Unit |
| --- | --- | ---: | --- |
| `519706` | `current_stock_asset_allocation_percent` | 0.69 | `percent_of_total_assets` |
| `519706` | `current_bond_asset_allocation_percent` | 0 | `percent_of_total_assets` |
| `164905` | `current_stock_asset_allocation_percent` | 91.41 | `percent_of_total_assets` |
| `164905` | `current_bond_asset_allocation_percent` | 0.05 | `percent_of_total_assets` |
| `519718` | `current_bond_asset_allocation_percent` | 99.54 | `percent_of_total_assets` |
| `519755` | `current_stock_asset_allocation_percent` | 21.2 | `percent_of_total_assets` |
| `519755` | `current_bond_asset_allocation_percent` | 77.8 | `percent_of_total_assets` |

`519718` disclosed its stock row with an official missing-value placeholder.
KunJin correctly emitted no stock fact and did not infer zero.

No current industry fact was emitted. Authenticated production industry-
observation coverage remains zero because the controlled taxonomy registry is
empty.

## Authentication Assertions

The following machine assertions passed for all four funds:

- selection manifest JSON was canonical and its SHA-256 matched the stored
  selection checksum;
- fund code, refresh ID, and selection-policy checksum matched their Schema V13
  records;
- every selected periodic fingerprint matched its single terminal candidate
  run;
- each classification input was canonical Manifest V3 and its SHA-256 matched
  the stored input fingerprint;
- Manifest V3 refresh, selection-manifest, selection-policy, and candidate-run
  snapshot bindings matched current immutable state; and
- `classification-evidence` and `classification-history` authenticated
  readback succeeded.

No selection checksum, candidate fingerprint, or canonical payload is
reproduced here.

## Classification Results

Every result remains `research_only`, `stale`, and `not_eligible`.

### `519706`

- product family: `unclassified`
- risk bucket: `unclassified`
- reason codes:
  `classification_stale`, `critical_evidence_missing`,
  `critical_evidence_stale`, `official_scope_missing`
- missing evidence: `legal_product_family_evidence_missing`

### `164905`

- product family: `sector_theme`
- risk bucket: `unclassified`
- reason codes: `classification_stale`, `critical_evidence_stale`
- missing evidence: none

### `519718`

- product family: `ordinary_bond`
- risk bucket: `unclassified`
- reason codes:
  `classification_stale`, `credit_quality_evidence_missing`,
  `critical_evidence_missing`, `critical_evidence_stale`,
  `duration_evidence_missing`, `leverage_evidence_missing`
- missing evidence:
  `below_aa_plus_mandate_evidence_missing`,
  `below_aa_plus_observation_evidence_missing`,
  `convertible_exposure_evidence_missing`,
  `convertible_observation_evidence_missing`,
  `credit_quality_mandate_evidence_missing`,
  `credit_quality_observation_evidence_missing`,
  `derivatives_evidence_missing`, `duration_evidence_missing`,
  `duration_observation_evidence_missing`,
  `exchangeable_exposure_evidence_missing`,
  `exchangeable_observation_evidence_missing`,
  `foreign_exposure_evidence_missing`,
  `issuer_concentration_evidence_missing`,
  `issuer_concentration_mandate_evidence_missing`,
  `leverage_mandate_evidence_missing`,
  `leverage_observation_evidence_missing`,
  `stock_mandate_evidence_missing`,
  `stock_observation_evidence_missing`,
  `unrated_non_sovereign_mandate_evidence_missing`, and
  `unrated_non_sovereign_observation_evidence_missing`.

### `519755`

- product family: `equity_mixed`
- risk bucket: `unclassified`
- reason codes:
  `classification_stale`, `critical_evidence_missing`,
  `critical_evidence_stale`, `holdings_evidence_missing`,
  `industry_evidence_missing`
- missing evidence:
  `holdings_evidence_missing`,
  `industry_concentration_evidence_missing`,
  `industry_count_evidence_missing`,
  `largest_security_evidence_missing`, and
  `top_ten_holdings_evidence_missing`

All conflict arrays were empty.

## Local Verification

- Focused parser and report-fact tests: `137 passed`
- Full test suite: `1505 passed`
- `ruff check .`: passed
- compileall with private bytecode cache: passed
- `pip check`: no broken requirements
- `git diff --check`: passed

Scoped `ruff format --check` still reports the four touched historical large
files. The same files fail the formatter on the accepted HEAD baseline. They
were not bulk-formatted because doing so would add unrelated repository-wide
format churn; Ruff semantic checks pass.

## Acceptance Decision

Task 9 representative live acceptance passes. The real-shape adapter improves
correctness from zero authenticated current facts to seven while retaining
strict selection, provenance, freshness, and failure behavior.

D1.1-C is not yet closed because Task 10 must measure automatic coverage over
the owner's current amount-free fund-code set and perform the independent
beginner review. The overall beginner-workflow assessment therefore remains
about 58/100 at this checkpoint, not 90 percent. D2 portfolio construction, D3
product-selection and pre-purchase checks, and Phase E monitoring still receive
zero credit. No purchase direction or amount is authorized.
