# KunJin Fixed-Income Current Facts And Parser V4 Design

**Status:** approved under the owner's standing authorization to adopt the
recommended fail-closed option.

## 1. Purpose

D1.1-C Task 6 adds nine current fixed-income observations. Parser v3 is already
an accepted production identity, so changing its fact set in place would make
the same provenance label describe two different parsers. This design therefore
introduces parser v4 and preserves parser v2 and v3 as immutable history.

The result remains `research_only`. This work does not authorize a purchase
direction, amount, target allocation, or Phase C mapping for a real fund.

## 2. Chosen Approach

Use one new parser identity for the complete native and legacy parsing paths:

- active native parser: `4`;
- active legacy parser: `4-docker-libreoffice-v1`;
- known historical native parsers: `2`, `3`;
- known historical legacy parsers: `2-docker-libreoffice-v1`,
  `3-docker-libreoffice-v1`.

The reviewed Docker image, Debian digest, LibreOffice version, package manifest,
and image ID remain unchanged. Only application parser provenance and its
derived checksum change.

Rejected alternatives are a v3 sub-version and a separate fixed-income
postprocessor provenance. Both would add a second version dimension or evidence
chain without improving the financial boundary.

## 3. Fixed-Income Fact Contract

The exact new allowlist is:

- `current_effective_duration`, unit `years`;
- `current_weighted_average_maturity_days`, unit `days`;
- `current_convertible_bond_asset_allocation_percent`;
- `current_exchangeable_bond_asset_allocation_percent`;
- `current_high_quality_fixed_income_percent`;
- `current_below_aa_plus_exposure_percent`;
- `current_unrated_non_sovereign_exposure_percent`;
- `current_gross_leverage_percent`, unit `percent_of_net_assets`;
- `current_largest_non_sovereign_issuer_percent`, unit
  `percent_of_net_assets` or `percent_of_fund_assets`.

No new Policy V1 threshold is introduced.

### 3.1 Duration, Maturity, And Bond Types

Duration and weighted-average maturity are separate facts. The parser never
converts one into the other. Convertible and exchangeable bond observations are
also separate. A combined row does not authorize a split.

Legal prohibitions, mandate ceilings, product names, and fund names never become
current observations.

### 3.2 Credit Distribution

Credit facts require a complete supported rating distribution with one declared
scope and one denominator. Every row must use a finite exact rating vocabulary
and be parseable. Unknown ratings, missing ratings, ranges, duplicate categories,
unexplained `other` rows, missing columns, mixed denominators, or an incomplete
scope suppress all three credit facts.

AA+ and above contributes only to
`current_high_quality_fixed_income_percent`. Rated rows below AA+ contribute only
to `current_below_aa_plus_exposure_percent`. Explicit unrated non-sovereign rows
contribute only to `current_unrated_non_sovereign_exposure_percent`. A missing
category never becomes zero.

### 3.3 Leverage

Gross leverage requires an explicit current observation with fund net assets as
the denominator. Values above 100 percent are valid observations and must not be
rejected by ordinary percentage ceilings. An unknown or mixed denominator emits
no leverage fact.

### 3.4 Issuer Concentration

Largest non-sovereign issuer concentration requires a complete supported issuer
table with one denominator. Only rows explicitly categorized as sovereign or a
Policy V1 policy bank may be excluded. Issuer-name similarity is not a category.

Normalized duplicate issuer names, incomplete scope, unknown categories, mixed
denominators, or unparseable rows suppress the fact. Related issuers are not
aggregated without an official grouping. An empty non-sovereign set does not
become zero unless the official report explicitly discloses zero.

## 4. Parser Integration

Only annual, semiannual, and quarterly reports enter the current-fact path.
HTML, DOCX, and validated legacy-conversion HTML may supply supported structured
tables. Reliable explicit current sentences may supply duration, maturity, and
leverage only. Credit, issuer, convertible, and exchangeable observations require
the supported structured evidence defined above.

Legacy converted free text that is marked `nfc_only` remains disabled. PDF does
not gain table reconstruction and retains the existing exact trusted-heading,
temporal-context, and hidden-character failure closures.

Every emitted fact binds `effective_from` and `effective_to` to the authenticated
report-period end. Missing, impossible, or mismatched report dates emit no
current fact. Conflicting values or denominators remain ambiguous and retain the
existing conflict code.

## 5. Provenance And Historical Compatibility

The existing schema already permits one artifact to have separate parse results
for different provenance identities. No schema version bump is required.

Acceptance requires:

1. Existing v2 and v3 provenance, parse results, facts, classifications, IDs,
   fingerprints, and checksums remain byte-for-byte unchanged.
2. Current classification accepts only active v4 provenance.
3. An artifact with only v2 or v3 results must be reparsed; history cannot satisfy
   current evidence.
4. Unknown future provenance remains rejected.
5. `fund converter-status` reports `4-docker-libreoffice-v1` with the newly
   derived provenance checksum while retaining the reviewed image identity.

## 6. Missing-Evidence Monotonicity

The nine observations may remove only their matching eight observation gaps:

- duration or maturity: `duration_observation_evidence_missing`;
- convertible: `convertible_observation_evidence_missing`;
- exchangeable: `exchangeable_observation_evidence_missing`;
- high quality: `credit_quality_observation_evidence_missing`;
- below AA+: `below_aa_plus_observation_evidence_missing`;
- unrated non-sovereign: `unrated_non_sovereign_observation_evidence_missing`;
- leverage: `leverage_observation_evidence_missing`;
- issuer concentration: `issuer_concentration_evidence_missing`.

No observation removes a legal mandate, derivative, foreign-exposure, stock, or
other unrelated gap. A threshold breach may replace missing evidence with a
more conservative conflict or bucket, but it must never improve status, risk
bucket, or portfolio role. Deleting a fact, lowering confidence, making evidence
stale, or adding a conflict must not improve the result.

## 7. Verification

Tests cover all nine success paths and the following failure cases:

- incomplete, missing, duplicate, ranged, unknown, or mixed-denominator credit
  rows;
- explicit sovereign and policy-bank exclusions without name inference;
- duplicate issuers, incomplete issuer tables, and empty post-exclusion sets;
- duration versus maturity and convertible versus exchangeable separation;
- gross leverage above 100 percent and invalid denominators;
- authenticated report-period binding and parser conflict behavior;
- exact missing-evidence monotonicity;
- active v4, historical v2/v3, unknown v5, converter status, store, schema,
  service, CLI, and smoke compatibility.

Focused tests, the full test suite, Ruff, `git diff --check`, independent
specification review, and independent financial-evidence review must all pass
before Task 6 is complete.

## 8. Non-Goals

- No change to Policy V1 thresholds.
- No change to the empty production industry taxonomy.
- No implementation of selection/classification manifest Task 7.
- No D2 portfolio correlation or overlap gate.
- No D3 product-selection or pre-purchase gate.
- No Phase E monitoring or rebalancing policy.
- No 90 percent beginner-help claim.
