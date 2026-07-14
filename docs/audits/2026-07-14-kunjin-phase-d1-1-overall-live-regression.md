# KunJin Phase D1.1 Overall Live Regression

Date: 2026-07-14

## Scope

This regression covers the three remaining common public funds after the
`519706` D1.1-B acceptance. It verifies official-document synchronization,
legacy OLE conversion, classification persistence, authenticated evidence
readback, and strict fail-closed behavior. It does not authorize a fund purchase
or complete D2 portfolio controls or D3 product selection.

## Runtime And Results

- Image ID: `sha256:b0b1fcf864473ec8dbcad10fa49c29b0978ce89bb4ffe0829d4607a5f6cb19a9`
- Parser version: `2-docker-libreoffice-v1`
- Provenance checksum: `06a8943f8e01958aef014f19cfe5443288be26ba456fb164ea7a6890cd9a8479`
- Live results: `/private/tmp/kunjin-d1-overall-v13-results-20260714-161953`
- Live data: `/private/tmp/kunjin-d1-overall-v13-data-20260714-161953`
- Live state: `/private/tmp/kunjin-d1-overall-v13-state-20260714-161953`

All 47 discovered official documents parsed successfully:

- `164905`: 16 of 16, including 13 Docker LibreOffice OLE documents.
- `519718`: 15 of 15, including 12 Docker LibreOffice OLE documents.
- `519755`: 16 of 16, including 12 Docker LibreOffice OLE documents.

The classification V2 manifest for every fund retained the native and Docker
parser provenance checksums. The three previously failing artifacts now persist
successful immutable parse results using the exact Docker provenance above:

- `164905` 2023 annual report: one extracted fact.
- `164905` fund contract: three extracted facts.
- `519755` 2023 annual report: five extracted facts.

## Root Causes And Corrections

Direct container diagnostics proved both annual reports completed in under one
second with exit code zero and `OOM=false`. Their HTML outputs were below the
4 MiB byte limit but contained about 2.25 million and 2.27 million characters,
slightly above the former 2 Mi-character limit. The bounded character limit is
now 3 Mi while the 4 MiB file limit, 768 MiB container memory limit, 45-second
timeout, network isolation, and strict HTML validation remain unchanged.

The fund contract contained a stale template HTML title identifying a
prospectus, while its visible leading cover split the exact official candidate
title across two adjacent paragraphs. Fund contracts now accept only an exact
two-block title reconstruction within the first eight views. Conflicting HTML
title metadata is ignored only when that strong fund-contract cover evidence is
present. Visible fund-code and fund-name conflicts remain fatal, and periodic
report title and period rules are unchanged.

## Verification

- New regression tests were observed failing before the production changes.
- Focused converter and parser suites: 83 passed.
- Full test suite: 1250 passed.
- Ruff and `git diff --check`: passed.
- Clean v13 live document regression: 47 of 47 successful.

## Remaining Fail-Closed Results

Fund `519718` still has a separate tier-2 holdings synchronization failure:

- `invalid_disclosure_date`
- `missing_industry_exposure`
- top-level `fund_disclosure_sync_failed`

This does not affect the successful official-document regression. Current D1
classification evidence remains fail-closed for all three funds:

- `164905`: `stale`, product family `sector_theme`, role `not_eligible`.
- `519718`: `stale`, product family `ordinary_bond`, role `not_eligible`.
- `519755`: `stale`, product family `equity_mixed`, role `not_eligible`.

Exact reason and missing-evidence codes remain available in the v13 evidence
JSON. No purchase direction or amount is authorized.

## Objective Assessment

D1.1 now provides useful and auditable official-document coverage for the four
tested common public funds, including real legacy OLE reports. For a beginner,
this materially reduces the risk of classifying a fund from its name or from an
unverified platform label.

It is not a complete purchase assistant and does not provide 90 percent of the
help needed to buy funds. Current evidence is stale, one holdings source remains
incomplete, and D2 portfolio overlap/correlation controls and D3 product
selection and pre-purchase checks are not implemented. The correct acceptance
decision is therefore: D1.1 document coverage accepted for the tested scope,
with the overall system remaining `research_only` and strictly fail-closed.
