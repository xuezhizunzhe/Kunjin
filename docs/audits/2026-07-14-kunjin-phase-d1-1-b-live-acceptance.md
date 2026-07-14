# KunJin Phase D1.1-B Live Acceptance

Date: 2026-07-14

## Scope

This acceptance verifies the isolated Docker LibreOffice path for authenticated
legacy OLE public fund documents. It does not approve a fund, authorize a trade,
or complete D2 portfolio controls or D3 product selection.

## Runtime

- Image ID: `sha256:b0b1fcf864473ec8dbcad10fa49c29b0978ce89bb4ffe0829d4607a5f6cb19a9`
- Parser version: `2-docker-libreoffice-v1`
- Export filter provenance: `html_starwriter_skip_images_v1`
- Provenance checksum: `06a8943f8e01958aef014f19cfe5443288be26ba456fb164ea7a6890cd9a8479`
- Live result directory: `/private/tmp/kunjin-d1-live-results-v11-20260714-154552`

## Live Evidence

Fund `519706` completed profile, holdings, official-document synchronization,
classification, classification evidence, and classification history with zero
command failures and empty stderr logs.

All ten discovered official documents parsed successfully:

- Two annual reports through Docker LibreOffice.
- Two native product summaries.
- One native prospectus update.
- Three quarterly reports through Docker LibreOffice.
- Two semiannual reports through Docker LibreOffice.

The seven OLE documents persisted immutable parse results using the exact Docker
provenance above. The classification V2 manifest included OLE annual-report parse
result `2` and the same provenance checksum. Authenticated classification evidence
and history readback completed successfully.

## Implemented Corrections

- Own the private `/tmp` tmpfs with the validated runtime UID/GID.
- Request Writer HTML `SkipImages` and keep the single-file output allowlist.
- Recover only bounded LibreOffice formatting-tag and list-item misnesting.
- Accept an exact official candidate title in the first eight text views as cover
  document-kind, report-period, and current-fund identity evidence.
- Ignore explicit fund identity fields only inside a parsed `目标基金` or
  `target fund` section.
- Bind the exact image-free export filter in parser provenance.

## Verification

- Focused risk audit, parser, and converter tests: 92 passed.
- Full unit suite: 1154 passed.
- Integration and smoke suite: 94 passed.
- Total: 1248 passed.
- Ruff and `git diff --check`: passed.

## Remaining D1 Result

The live `519706` classification remains `stale` and `unclassified`; this is not
an OLE converter failure. Exact reason codes are:

- `classification_stale`
- `critical_evidence_missing`
- `critical_evidence_stale`
- `official_scope_missing`

Missing evidence remains `legal_product_family_evidence_missing`. The selected
2025 annual report was considered stale after `2026-04-30`, while the product
summary and prospectus update remained current. This freshness and classification
coverage gap must remain fail-closed and be handled separately from D1.1-B.

## Acceptance Decision

Phase D1.1-B isolated legacy-document conversion is accepted for the tested
common public-fund path. The result remains `research_only` and does not imply
that KunJin provides a purchase recommendation or 90 percent beginner decision
coverage.
