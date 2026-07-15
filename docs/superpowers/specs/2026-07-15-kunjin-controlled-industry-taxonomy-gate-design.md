# KunJin Controlled Industry Taxonomy Gate Design

Date: 2026-07-15
Status: Approved design, pending implementation

## 1. Purpose

This design amends Phase D1.1-C after adversarial review proved that a
free-text denylist cannot distinguish an unknown aggregate bucket such as
`Other sectors` from a legitimate named industry such as
`Other Consumer Services`.

KunJin must not publish current industry count, largest-industry name, or
largest-industry weight unless every included row belongs to one authenticated,
controlled classification system. Missing taxonomy evidence remains missing
evidence and cannot improve a classification.

The result remains `research_only`. This change adds no recommendation,
allocation, target, or trading instruction.

## 2. Decisions

### 2.1 Controlled taxonomy, not free-text inference

Industry observations require a stable taxonomy identity. A complete-looking
table containing only rank, free-text industry name, and weight is insufficient.
KunJin does not decide whether a label is a real industry by maintaining an
open-ended blacklist of words such as `other`, `remaining`, or `unclassified`.

### 2.2 Initial recognized taxonomy metadata

KunJin initially recognizes this candidate taxonomy metadata:

- taxonomy id: `sw_level1_2021`
- exact source labels after bounded normalization:
  - `申万一级行业分类（2021）`
  - `申万一级行业分类(2021)`
- expected code shape: six decimal digits beginning with `801`

This metadata is justified by the existing structured fixture and parser shape.
It is not fact-eligible by itself. Code shape and a source label do not prove
that a code is a valid member of the taxonomy or that it maps to the supplied
industry name.

Fact eligibility requires a complete, immutable, versioned code-to-canonical-
name mapping with source provenance and a pinned checksum. The current
repository has no such asset. Therefore the initial enabled fact-eligible
taxonomy set is empty.

The implementation records the recognized metadata boundary but publishes no
industry observations until a separately reviewed complete mapping is added.
It does not upgrade a tier-2 source to tier 1.

#### 2.2.1 Canonical mapping-source provenance

An eligible mapping's `source_url` is authenticated provenance for one pinned
official asset, not a general-purpose fetch target. It must be an exact ASCII
HTTPS URL with:

- a lowercase DNS hostname made only of valid DNS labels, including lowercase
  `xn--` punycode labels when needed;
- no IP literal, bracketed authority, port, user information, query, or
  fragment; and
- a non-empty absolute path beginning with `/`.

The raw authority must equal the parsed hostname exactly. Uppercase hostnames,
Unicode hostnames, alternate authority syntax, implicit normalization, and URL
parser ambiguities are invalid registry data. These restrictions do not enable
any production mapping: `PRODUCTION_TAXONOMY_MAPPINGS` remains empty until a
complete official mapping receives separate evidence and review.

### 2.3 No CSRC claim from the HYPZ JSON adapter

The HYPZ JSON adapter receives `HYDM` and `HYMC`, but its current
`classification_standard` value is assigned locally rather than supplied by the
upstream response. Those records do not pass the controlled taxonomy gate.

KunJin must not infer an authenticated CSRC taxonomy from that hard-coded label.

### 2.4 Whole-distribution validation

Industry facts are all-or-nothing for one distribution. Validation requires:

1. one exact fact-eligible taxonomy for every row;
2. a non-empty code for every row;
3. every code existing in the pinned taxonomy mapping;
4. every normalized name matching that code's canonical name or an audited
   alias from the same mapping;
5. unique normalized codes and unique safe normalized names;
6. no control, `Cf`, or default-ignorable characters;
7. one supported denominator for every row;
8. exact ranks without duplicates;
9. an explicit complete-distribution scope; and
10. every row parsing successfully;
11. ranks equal the exact sequence `1..N`;
12. weights are non-increasing by rank; and
13. rank 1 has a unique strictly greater weight than rank 2 when largest-
    industry facts are requested.

If any requirement fails, the whole distribution emits none of:

- `current_largest_industry_name`
- `current_largest_industry_weight_percent`
- `current_industry_count`

No partial largest-industry result is retained from a taxonomy-invalid table.

### 2.5 Report-table contract

Periodic-report industry extraction accepts only a structurally bound table
with all of these columns:

- classification standard;
- industry code;
- rank;
- industry name; and
- weight with a supported denominator.

The current three-column rank/name/weight shape remains available as text
evidence for auditing but produces no industry observations.

### 2.6 Existing disclosure-service contract

The current `FundIndustryExposure` model does not preserve rank, denominator,
or authenticated complete-scope evidence. The service cannot reconstruct those
fields and cannot apply the report whole-distribution gate honestly.

This amendment therefore disables synthesis of
`current_largest_industry_weight_percent` and `current_industry_count` from
`FundIndustryExposure`. It also does not add a largest-industry-name synthesis
path. The existing fallback from missing code to name is removed from current
risk facts.

A future service path requires a separately designed model, schema, parser,
and migration that preserve rank, denominator, complete scope, taxonomy id,
and mapping checksum. The source remains tier 2 when it originated from
Eastmoney. Taxonomy validation and source authority are separate dimensions.

### 2.7 Time-context hardening

Before parsing a report section's period context, KunJin rejects control,
`Cf`, and default-ignorable characters. This prevents variants such as
`历\u200b史数据` from bypassing historical-context detection.

The existing period-plus-residual-cue model remains:

- a generic authenticated current phrase may bind to the candidate report
  period;
- an explicit period must resolve uniquely to that same period; and
- an unresolved or conflicting temporal cue fails closed.

Support for exact Chinese-numeral dates may improve coverage later, but failure
to parse them cannot publish a current fact.

#### 2.7.1 Periodic PDF current-section authorization

Periodic-report PDFs begin with current-observation eligibility disabled. Page
starts, unknown headings, ordinary uppercase text, and legal-section headings
cannot authorize current facts. Eligibility is enabled only by one exact,
audited current-section heading. The initial allowlist contains:

- `CURRENT ASSET ALLOCATION`

A Chinese current-section heading may be added only when an exact authenticated
fixture and parser test justify that literal form. Generic uppercase shape is
not evidence that a PDF line is a section heading.

The PDF section and eligibility state persists across page boundaries. An exact
trusted current heading may remain active on following pages. A line containing
an existing historical-context pattern, or any control, `Cf`, or default-
ignorable character, disables current eligibility until a later exact trusted
current heading replaces the state. Historical and unsafe lines never authorize
a fact themselves. Legal fact extraction remains independent of this current-
observation gate.

## 3. Components

### 3.1 Taxonomy registry

Add `src/kunjin/funds/industry_taxonomy.py` with exact immutable records and
pure validation functions. The module owns:

- taxonomy ids, versions, and exact source aliases;
- pinned mapping provenance and checksum;
- exact code-to-canonical-name and audited-alias validation;
- safe name and code normalization; and
- whole-distribution validation results.

It does not fetch remote taxonomies, rank industries, or classify funds.

### 3.2 Report fact extraction

`src/kunjin/funds/risk/report_facts.py` consumes a validated industry table and
publishes the three industry observations only after the whole-distribution
gate succeeds. The free-text unknown-industry grammar is removed from the
authorization path.

### 3.3 External disclosure synthesis

`src/kunjin/funds/risk/service.py` stops synthesizing current industry facts
from the incomplete `FundIndustryExposure` contract. No independent
service-only taxonomy logic or inferred evidence envelope is allowed.

### 3.4 Parser context validation

`src/kunjin/funds/risk/parsers.py` rejects unsafe invisible characters before
historical/current period matching. Periodic PDFs additionally use an exact
current-section allowlist and carry their section authorization state across
pages; legal fact parsing is unchanged.

### 3.5 Parser version and provenance

The report parser version changes from `2` to `3`. Parser provenance and parse
result identity must bind the new version. Existing v2 artifacts, facts,
classification records, and manifests remain immutable and readable.

The legacy converter capability contract accepts active parser version
`3-docker-libreoffice-v1` while retaining exact readback validation for
historical `2-docker-libreoffice-v1`. The pinned image, LibreOffice package,
conversion filter, and container identity do not change.

Current evidence must be regenerated by synchronizing the selected current
periodic documents through parser v3. The implementation must not attach the
new fact set to an existing v2 parse identity or silently reuse v2 free-text
industry facts as current evidence.

## 4. Data Flow

1. A periodic report or disclosure adapter produces bounded structured rows.
2. The report parser provides the explicit classification-standard label, code, name,
   rank, weight, denominator, and complete-scope evidence.
3. The taxonomy registry resolves the exact supported taxonomy.
4. The whole-distribution validator authenticates every row and the shared
   structure.
5. Only a successful validated distribution reaches industry observation
   extraction.
6. Parser v3 observation fingerprints retain the original bounded source fields and
   period binding.
7. Any failure returns no current industry observations; historical evidence
   remains immutable.

## 5. Failure And Compatibility Rules

- Recognized metadata without a complete pinned mapping: no industry
  observations.
- Unsupported taxonomy: no industry observations.
- Missing or malformed code: no industry observations.
- Mixed taxonomies or denominators: no industry observations.
- Duplicate code, name, or rank: no industry observations.
- Free-text three-column report table: no industry observations.
- HYPZ locally assigned CSRC label: no industry observations through this gate.
- Existing `FundIndustryExposure` records: no synthesized current industry
  observations because their evidence envelope is incomplete.
- Unsafe temporal context: no current observations from that context.
- Periodic PDF text before an exact trusted current-section heading: no current
  observations.
- Unknown, ordinary uppercase, or historical PDF headings: no current
  authorization; historical state remains closed across pages.
- Existing parser v2 facts: immutable history only; they do not satisfy the
  new current industry gate.
- Existing stored classifications and manifest bytes are not rewritten.
- Current reclassification may become more conservative when prior evidence
  lacked authenticated taxonomy data. That is intentional fail-closed behavior.

## 6. Testing

Add tests that prove:

- recognized `sw_level1_2021` metadata without a complete mapping is not
  fact-eligible;
- an empty fact-eligible registry emits no industry observations;
- synthetic test-only complete mappings prove the validator contract without
  enabling a production taxonomy;
- arbitrary aliases, versions, code shapes, unmapped codes, name mismatches,
  and missing codes fail;
- mixed standards, duplicate codes/names/ranks, unsafe Unicode, mixed
  denominators, and incomplete scopes fail;
- the existing three-column free-text report table emits no industry facts;
- aggregate-looking and legitimate `Other...` labels cannot bypass or control
  the decision because taxonomy/code validation is the gate;
- HYPZ records with a locally assigned standard do not pass;
- service synthesis produces no current industry facts from the incomplete
  `FundIndustryExposure` contract;
- source tier remains unchanged;
- `历\u200b史数据`, `往\u200b期`, and `prior\u200b period` fail period binding;
- Policy V1 thresholds and non-industry current facts are unchanged; and
- historical manifest/readback fixtures remain byte-for-byte stable.

Parser and live tests must also prove:

- parser v3 creates a distinct authenticated parse result from historical v2;
- current selection/classification does not reuse v2 free-text industry facts;
- a current three-column report and current HYPZ disclosure lose only the
  unauthenticated industry facts;
- non-industry facts from the same documents continue to parse; and
- four-fund live acceptance reports the stricter industry coverage as missing
  rather than a technical success or a zero exposure.

## 7. Scope Boundaries

This amendment does not:

- vendor a complete official industry name catalogue;
- enable `sw_level1_2021` for facts without a complete pinned mapping;
- claim that any `801xxx` code/name pair is correct;
- add CSRC, GICS, or other taxonomies without separately audited evidence;
- change source authority tiers;
- introduce network taxonomy synchronization;
- change Policy V1 numeric thresholds; or
- provide a purchase decision or amount.

Future name-only support requires a complete versioned official taxonomy asset
with provenance and checksum. It cannot be added as a list of convenient names.

An eligible mapping asset must be published by the taxonomy owner, regulator,
or another authenticated official methodology source. Its canonical artifact
uses sorted UTF-8 JSON with exact taxonomy id, version, source URL, publication
date, canonical code/name pairs, and audited aliases. KunJin pins the SHA-256
of those canonical bytes. Name and alias matching uses NFKC normalization,
bounded whitespace normalization, and exact case-folded comparison; unsafe
characters remain invalid. The pinned source URL must also satisfy the canonical
DNS HTTPS provenance contract in section 2.2.1; IP literals and generic URLs are
not accepted as substitutes for an authenticated official asset location.

## 8. Acceptance Criteria

The design is complete when:

1. no free-text industry label can independently authorize current industry
   observations;
2. report industry facts require one controlled whole-distribution gate, while
   the incomplete service path emits no industry facts;
3. unsupported or incomplete taxonomy evidence remains missing;
4. unsafe invisible time-context characters fail closed;
5. all existing non-industry classifications retain their behavior;
6. complete relevant tests and Ruff pass; and
7. independent specification and quality reviews find no P0/P1/P2 defects.

The owner-facing acceptance summary must state plainly that this amendment
reduces current industry coverage to zero until a complete pinned taxonomy is
introduced. It improves correctness and failure behavior, not feature coverage.
