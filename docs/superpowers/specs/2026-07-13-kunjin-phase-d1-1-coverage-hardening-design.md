# KunJin Phase D1.1 Official Evidence Coverage Hardening Design

Date: 2026-07-13

Status: D1.1-A accepted; revised D1.1-B design approved

## 1. Goal

Improve real official-document coverage without weakening the Phase D1
fail-closed evidence standard. D1.1 must explain document failures safely,
support the dominant legacy Word cohort through a bounded mature converter,
and ingest only the newest necessary periodic reports before extracting current
risk observations.

D1.1 remains `research_only`. It does not provide suitability, portfolio fit,
fund selection, a trade direction, a target weight, or a purchase amount.

## 2. Evidence From The V5 And V6 Acceptances

The v5 sample discovered 57 official documents. After D1.1-A added exact safe
failure diagnostics, the isolated v6 acceptance reproduced the same population:

- 13 succeeded: 6 product summaries and 7 prospectus updates.
- 44 returned `official_document_invalid` before the fact parser ran.
- The failed set contains 21 quarterly reports, 11 semiannual reports, 11
  annual reports, and 1 fund contract.
- All successful documents were declared as Microsoft Word and were actually
  ZIP/OOXML DOCX containers.
- Every one of the 44 failures now reports the exact tuple
  `container_validation / legacy_ole_container_unsupported`.
- All 13 successful documents return null error, stage, and reason fields.
- No failed item omitted its safe diagnostics, and no path, traceback, raw
  exception, response body, or private field appeared in the live JSON.
- A captured representative quarterly report had a valid official landing
  page, matching title and date, and one same-origin attachment. The attachment
  was a legacy Microsoft OLE/Compound File Word document.
- Before D1.1-A, the detector accepted PDF, HTML, and DOCX only, so the
  representative OLE document was folded into the generic public code
  `official_document_invalid`. D1.1-A now preserves that compatible public code
  while adding the exact safe stage and reason.

The v6 result proves that the entire failed cohort is legacy OLE, including the
fund contract. This makes conversion coverage the next engineering bottleneck;
it does not prove that any converted document contains sufficient or
unambiguous financial facts.

The separate `519718.holdings` exit failure is not caused by OLE conversion.
Its tier-2 quarterly-holdings response has an invalid disclosure date and its
industry response contains no usable exposure records. That source gap remains
fail closed and does not alter the D1.1-B converter design.

## 3. Constraints

The following controls are fixed and must not be relaxed:

- Tier-1 publisher and host allowlists.
- Public DNS and HTTPS-only validation.
- Redirect host and redirect-count limits.
- Fund identity, title, publication-date, and share-class binding.
- Landing-page attachment count and same-origin checks.
- Declared MIME and detected-container agreement.
- Payload, archive-entry, decompressed-size, page, character, fact, and timeout
  limits.
- Macro, active-content, external-relationship, embedded-object, and symbolic-
  link rejection.
- Immutable artifact, fact, policy, and classification persistence.
- Existing public technical error codes and all classification reason and
  missing-evidence codes.
- No raw response body, extracted document body, exception text, local managed
  path, personal value, or private profile field in diagnostics, JSON, logs,
  tests, documentation, or audits.

Fund names, lower-tier platform categories, top-ten holdings, and mandate
ceilings must never substitute for missing current official evidence.

## 4. Chosen Approach

D1.1 uses three sequential increments:

1. D1.1-A: safe failure taxonomy and explicit legacy-container recognition.
2. D1.1-B: isolated legacy-DOC conversion, parser provenance, and immutable
   run/result history.
3. D1.1-C: latest-required-report selection and current-risk fact extraction.

Each increment must pass its own local and real acceptance gate before the next
increment starts. A diagnostic-only increment may receive no financial audit
score increase; accuracy of the next engineering decision is the intended
benefit.

Rejected alternatives:

- Do not guess that all 44 failures are OLE and immediately broaden the parser.
- Do not add Schema V12 merely to persist diagnostic events before a concrete
  cross-process history requirement exists.
- Do not skip periodic reports and claim D1 completion from an index-methodology
  adapter alone.
- Do not implement a hand-written binary Word parser.
- Do not execute `/usr/bin/textutil` directly as the logged-in user. Fixed argv,
  timeouts, and POSIX resource limits do not prevent Home or network access.
- Do not use deprecated `sandbox-exec` as the primary trust boundary.
- Do not silently fall back to a host converter when Docker, the pinned image,
  or any required isolation flag is unavailable.

## 5. D1.1-A Safe Failure Taxonomy

### 5.1 New Failure Model

Add `src/kunjin/funds/risk/failures.py` with exact, immutable types:

```text
DocumentFailureStage
  discovery
  landing_validation
  retrieval
  identity_validation
  container_validation
  conversion
  parser
  persistence
  unspecified

DocumentFailureReason
  dns_unavailable
  network_unavailable
  http_unavailable
  source_unregistered
  redirect_rejected
  discovery_format_invalid
  identity_mismatch
  publication_date_missing
  landing_format_invalid
  landing_title_mismatch
  landing_date_mismatch
  attachment_missing
  attachment_ambiguous
  attachment_host_rejected
  authentication_shell
  empty_or_script_only_html
  declared_mime_unsupported
  detected_container_unknown
  declared_detected_mismatch
  legacy_ole_container_unsupported
  legacy_converter_unavailable
  legacy_converter_timeout
  legacy_converter_resource_limit
  legacy_converter_failed
  legacy_converter_output_invalid
  resource_limit
  parser_format_invalid
  parser_identity_mismatch
  parser_effective_date_invalid
  parser_ambiguous_fact
  clock_invalid
  managed_artifact_invalid
  storage_failure
  unspecified_failure
```

This is the complete D1.1 failure-reason set. Later additions require a
separately reviewed design change; runtime code cannot create new values or use
free-form text.

`SafeDocumentFailure` contains only:

```text
public_code
stage
reason_code
```

It must reject subclasses, undeclared dataclass state, unknown enum values, and
free-form attributes.

### 5.2 Public Compatibility

The existing `DocumentSyncItem.error_code` remains unchanged and continues to
use the existing public codes:

```text
official_document_unavailable
official_document_invalid
official_document_resource_limit
official_document_parse_failed
classification_storage_failed
```

`DocumentSyncItem` adds two optional, allowlisted fields for failed candidates:

```text
failure_stage
failure_reason
```

These fields contain only the enum values above. They never contain an
exception message, URL, path, HTTP status, MIME header, document title, or
document content. Successful items return both fields as null.

Existing CLI error messages, `RiskServiceError.reason`, and
`fund_document_artifacts.parse_error_code` remain unchanged. In particular,
safe diagnostic reasons must not be placed in `RiskServiceError.reason`, which
is rendered into public CLI messages.

### 5.3 Error Production And Mapping

`OfficialDocumentError` and `RiskDocumentParseError` gain a required internal
allowlisted reason. The reason is assigned at the source of the failure; the
service must never infer a reason by parsing exception text.

The service converts exceptions through one deterministic mapping function:

```text
exception -> SafeDocumentFailure -> existing public RiskServiceError
```

Unknown exceptions map to the existing fail-closed public code plus:

```text
stage=unspecified
reason_code=unspecified_failure
```

An optional observer may receive `SafeDocumentFailure` for tests and local
acceptance aggregation. Observer failure must not alter the financial or public
sync result.

### 5.4 Legacy OLE Recognition

The container detector recognizes the exact Compound File Binary signature:

```text
D0 CF 11 E0 A1 B1 1A E1
```

D1.1-A does not parse the container. It reports:

```text
error_code=official_document_invalid
failure_stage=container_validation
failure_reason=legacy_ole_container_unsupported
```

This changes the explanation, not the evidence state or classification.

### 5.5 Persistence

D1.1-A requires no schema migration.

Downloaded artifacts that fail before safe artifact construction continue to
be deleted. Parsed-artifact failure persistence continues to store only the
existing public `parse_error_code`. A future cross-process diagnostic-history
requirement would use a separate immutable failure-event table; it would not
overload artifact identity.

## 6. D1.1-B Isolated Legacy-DOC Conversion

### 6.1 Converter And Provisioning Boundary

Add a `LegacyDocConverter` protocol outside the pure classification engine and
a `DockerLegacyDocConverter` personal-use adapter. The adapter invokes a fixed
Docker CLI path without a shell and accepts no user-supplied command fragments,
image names, mount destinations, or converter options.

The converter image must be allowlisted by Docker's immutable local
`sha256:<image-id>` and must be invoked by that ID, never by a tag. A mutable
tag, substituted image ID, missing image, daemon error, architecture mismatch,
or failed image inspection returns a safe conversion failure. Normal fund
synchronization never pulls or builds an image. Image provisioning is a
separate explicit setup operation that builds with a pinned base-image digest
and exact converter packages, captures the result through `--iidfile`, inspects
the exact local image ID and architecture, and records the Dockerfile checksum,
package manifest, base digest, and resulting image ID before enabling the
adapter.

The initial image contains a pinned LibreOffice Writer conversion runtime and
exports bounded HTML rather than flattened plain text so table boundaries can
be preserved for D1.1-C. The output is still untrusted and must pass the normal
HTML active-content, external-reference, size, identity, kind, period, and fact
ambiguity checks. Converter availability never affects PDF, HTML, or DOCX
parsing and is checked lazily only for a validated OLE document.

### 6.2 Required Container Isolation

Every conversion uses a new private host directory and a disposable container
with a fixed argument vector. The adapter requires all of these controls:

- `--network=none`, `--read-only`, `--cap-drop=ALL`, and
  `--security-opt=no-new-privileges`.
- `--pull=never`, `--log-driver=none`, Docker's default seccomp profile, the
  validated non-root host UID/GID required by the private bind mounts, no host
  IPC, no privileged mode, no device access, and
  no Docker-socket or Docker API mount.
- Fixed CPU, memory, swap, PID, and wall-clock limits plus an init process for
  child reaping.
- A read-only bind mount containing only the authenticated OLE input and a
  bounded private output mount or tmpfs containing no user Home data.
- A read-only root filesystem plus only the explicitly required tmpfs paths,
  all with `nosuid`, `nodev`, and `noexec` where execution is not required. The
  private `/tmp` tmpfs is mode 0700 and owned by the same validated runtime
  UID/GID so the non-root LibreOffice process can use its working directory.
- Closed standard input, discarded stdout and stderr, a clean environment, and
  forced container removal after success, failure, or timeout.

Each run uses both a private `--cidfile` and an unpredictable validated
container name. The adapter does not rely on `--rm`: after the conversion CLI
returns or is terminated, it runs bounded `docker rm --force` cleanup using the
private container ID, with the private name as a fallback, and then verifies
through `docker inspect` that the container no longer exists. On startup it
reconciles only stale containers carrying KunJin's private converter label. If
cleanup or reconciliation cannot be proved, conversion remains disabled and
the output is discarded.

If the daemon, image inspection, isolation flags, container start, timeout
cleanup, or forced removal cannot be proved successful, the output is discarded
and no fact is published. Raw converter output, logs, command details, image
metadata beyond the allowlisted provenance, and host paths never enter public
JSON or application logs.

### 6.3 Artifact, Refresh Gates, Runs, Results, And Provenance

The original OLE document remains the authenticated official artifact. Its
official landing URL, final URL, publisher, publication time, byte size, and
SHA-256 checksum are preserved under the existing immutable artifact contract.

The converted HTML is a bounded transient parser input. It is not an official
artifact, is not treated as a new source, and is deleted after parsing. Facts
remain bound to the original official document ID.

D1.1-B adds Schema V12 because the current schema incorrectly combines official
artifact identity with one parser outcome. The additive migration separates
source identity, parser provenance, run events, and deterministic successful
results:

- A new immutable `fund_document_refresh_runs` table records the start of every
  fund-document refresh and commits before discovery or any other external
  read. A separate append-only
  `fund_document_refresh_completions` table records at most one terminal result.
  A refresh with no completion is interrupted and blocks current evidence.
- A new immutable `fund_document_candidate_runs` table records every discovered
  candidate's fingerprint and terminal outcome within that refresh. It can
  reference an authenticated artifact and parse run, or store only the safe
  public error, stage, and reason when failure occurs during landing,
  retrieval, identity, or container validation.
- `fund_document_artifacts` remains the immutable authenticated source record
  and preserves every existing document ID, checksum, and column. Its existing
  `parse_status`, `parser_version`, and `parse_error_code` columns become a
  compatibility snapshot of the first persisted attempt; they are never
  updated and are no longer the authority for current parser status.
- A new immutable `fund_document_parser_provenance` table stores a canonical
  provenance JSON document and checksum for each native or converted parser
  contract. Parser version is descriptive; the provenance checksum is the
  authenticated identity.
- A new immutable `fund_document_parse_results` table stores at most one
  deterministic successful result for each source document and provenance. It
  records the parser-input checksum and canonical fact-set fingerprint.
- A new append-only `fund_document_parse_runs` table records every live or
  backfilled run event. A live success references the deterministic result and
  has null error fields. A live failure stores the allowlisted public error,
  stage, and reason but does not occupy the success-result key, so a temporary
  daemon or resource failure can be retried under the same provenance.
- Existing Schema V11 rows are migrated as `run_kind=legacy_backfill`. Successful
  native PDF, HTML, and DOCX rows receive a provenance record, a deterministic
  result whose parser-input checksum is the original artifact SHA-256, and a
  fact-set fingerprint derived from stored facts. Historical failed rows retain
  their known public `parse_error_code`, but their unavailable stage and reason
  remain null and are explicitly marked as legacy backfill; the migration never
  invents D1.1-A diagnostics.
- `fund_mandate_facts` gains an immutable `parse_result_id` binding. Existing
  facts are backfilled to the corresponding successful result while the
  migration temporarily replaces only the table's own immutability trigger.
  New facts must reference a successful result whose document, parser version,
  provenance, parser-input checksum, and fact-set fingerprint authenticate the
  complete chain.

The legacy parser provenance is a canonical checksum-backed record that
includes the adapter contract version, local image ID, converter package version,
export-filter contract, target architecture, and normalization contract. It
does not depend on a mutable image tag or download time.

The same source checksum and parser provenance have at most one deterministic
successful result. A later success with a different parser-input checksum or
fact-set fingerprint is a storage conflict and fails closed; an identical
success reuses the existing result and appends a run event. Failed run events
remain immutable but never block a later success under the same provenance. A
later provenance may add a new result and new facts; historical failures,
successes, facts, and classifications are never rewritten.

The active provenance is a fixed per-container-family service policy, not a
mutable database choice. Current classification reads facts only when the most
recent refresh, ordered by append-only refresh ID, has a terminal completion and
the relevant candidate from that same refresh succeeded. That candidate must
bind a successful latest parse run, ordered by append-only run ID, for its
source document and active provenance. An incomplete refresh, discovery failure,
pre-parser candidate failure, or latest parse failure must not fall back to an
older candidate, success, or provenance, even when that older evidence is
otherwise fresh. A migrated V11 artifact remains historical evidence until a
new V12 refresh establishes a current candidate gate.

New classifications use an exact evidence-manifest V2 shape that explicitly
binds parse-result IDs and provenance checksums. The reader accepts either the
exact legacy V1 shape or the exact V2 shape; it does not accept arbitrary
optional fields. Historical Schema V11 classifications retain their
byte-identical V1 manifests, and authenticated readback reaches their migrated
result and provenance chain through the manifest's immutable fact IDs.

### 6.4 Converted-Output Validation

Converted HTML must pass all of the following before fact extraction:

- Conversion uses LibreOffice Writer HTML's `SkipImages` option. D1 extracts
  text and tables only; if the converter still emits image companions or other
  extra resources, validation fails closed instead of broadening the resource
  allowlist.
- LibreOffice output may cross-close the inert formatting tags `a`, `b`,
  `font`, and `span`, or omit `li` end tags before an `ol`/`ul` end tag. The
  validator applies recovery only to those exact cases. Root, body, division,
  table, row, cell, and every other structural boundary remain strictly nested.
- A converted report may render its cover title as a paragraph instead of an
  HTML heading. Document-kind and periodic-report evidence may therefore use an
  exact normalized match to the official candidate title within the first eight
  text views. Semantic title or heading evidence in that same cover window must
  still agree. Later body headings are section evidence only and cannot promote
  or change the document kind.
- The same exact cover-title match is current-fund identity evidence. Explicit
  fund names and codes remain conflict-checked except inside a parsed section
  explicitly identified as `目标基金` or `target fund`, where they describe the
  linked target product rather than the requested feeder fund. The provenance
  payload records the export filter as `html_starwriter_skip_images_v1`.
- A non-empty, strictly decoded UTF-8 document with no NUL, replacement
  character, binary signature, converter banner, script, active content,
  embedded object, external resource, or external relationship.
- Maximum extracted-character limit.
- Exact requested fund code, or exact official legal product identity when the
  report format genuinely omits the code.
- Document-kind consistency with the candidate title and report headings.
- Report-period consistency with the candidate publication and report heading.
- No conflicting fund code or legal product identity.
- Deterministic CRLF-to-LF, BOM removal, and Unicode NFC normalization. The
  conversion boundary does not use NFKC.
- Table facts are accepted only when row, column, label, unit, and value bindings
  remain explicit and unambiguous after conversion. Flattened or merged cells
  that lose those bindings remain missing evidence.
- Deterministic normalized output and fact extraction for identical input bytes
  and parser provenance.

Conversion success alone never publishes facts. Identity and parser validation
remain mandatory.

### 6.5 Exact Failure Mapping

The adapter assigns safe failures at the source without parsing exception text:

```text
daemon, CLI, image, or isolation unavailable
  -> official_document_parse_failed / conversion / legacy_converter_unavailable
timeout or process cleanup cannot be proved
  -> official_document_resource_limit / conversion / legacy_converter_timeout
CPU, memory, PID, input, or output limit exceeded
  -> official_document_resource_limit / conversion / legacy_converter_resource_limit
nonzero exit, signal exit, or container execution failure
  -> official_document_parse_failed / conversion / legacy_converter_failed
missing, empty, malformed, unsafe, or nondeterministic output
  -> official_document_parse_failed / conversion / legacy_converter_output_invalid
fund identity conflict
  -> official_document_parse_failed / parser / parser_identity_mismatch
document kind or report period conflict
  -> official_document_parse_failed / parser / parser_effective_date_invalid
```

These reasons never enter `RiskServiceError.reason`. Public messages and the
existing top-level error-code vocabulary remain unchanged.

### 6.6 Security Position

Supporting OLE increases attack surface. Therefore D1.1-B must include hostile
container, macro, embedded-object, external-link, timeout, resource-exhaustion,
malformed-output, identity mismatch, converter-unavailable, converter-error,
cleanup, image-substitution, and repeated-run determinism tests.

Bare `textutil`, host LibreOffice, mutable Docker tags, normal-sync image pulls,
and weakened container flags are prohibited fallbacks. If the Docker boundary
cannot be established on the current host, D1.1-B returns the exact converter
failure and the document remains unusable. Coverage must not be purchased by
weakening process isolation.

## 7. D1.1-C Latest Reports And Current Facts

### 7.1 Candidate Selection

For each fund, synchronize at most the newest candidate by publication time for
each periodic kind:

```text
quarterly_report
semiannual_report
annual_report
```

Product summaries and prospectus updates retain their existing current-document
rules. If the newest publication time contains multiple different official
candidate URLs for the same document kind, candidate selection is conflicted
and fails closed before download. A lower-tier title or URL order cannot break
the tie.

If the newest periodic document fails, KunJin reports that failure. It must not
silently promote an older report to current evidence. Older authenticated
artifacts remain available as history but cannot satisfy the current gate.

### 7.2 Fact Extraction Priorities

Extract only facts already required by Policy V1 and supported by explicit
report text or tables.

First priority for all products:

- Current stock, bond, and cash asset-allocation percentages.
- Largest security weight.
- Top-ten disclosed holdings weight with explicit disclosure scope.
- Largest industry name and weight.
- Industry count when the report provides a complete classified distribution.

Additional fixed-income priority:

- Current effective duration or weighted average maturity.
- Current stock, convertible-bond, and exchangeable-bond exposure.
- Current credit-quality distribution.
- Current below-AA+ and unrated non-sovereign exposure.
- Current gross leverage.
- Current largest non-sovereign issuer concentration.
- Current derivative and foreign exposure when explicitly disclosed.

Absent, rounded, aggregated, or incomplete disclosure remains missing evidence.
Top-ten holdings do not become complete holdings. A legal maximum does not
become a current observation.

### 7.3 Report Dates And Freshness

Facts retain report period, publication time, document kind, page or section,
bounded excerpt, and source-document fingerprint. Freshness uses the report
period and Policy V1 deadline, not download time.

Conflicting current reports remain `conflicted`; a later download must not
silently overwrite or supersede an authenticated conflict.

## 8. Data Flow

```text
append and commit immutable refresh start
  -> official index discovery
  -> candidate identity and publication validation
  -> landing-page and attachment validation
  -> MIME and container detection
  -> on pre-parser failure: append terminal SafeDocumentFailure candidate outcome
  -> otherwise: PDF/HTML/DOCX parser, or pinned isolated Docker legacy-DOC converter
  -> converted-HTML safety, identity, kind, and period validation
  -> deterministic fact extraction
  -> atomically persist original artifact, provenance, parse run, result, and facts
  -> append terminal candidate success bound to the parse run
  -> append refresh completion
  -> require current refresh/candidate/provenance success gates
  -> Policy V1 classification
  -> authenticated evidence and history readback
```

Every failure boundary returns the existing public code and, where a candidate
item exists, an allowlisted safe stage and reason.

## 9. Testing Strategy

### 9.1 D1.1-A

- Exact enums and immutable failure record validation.
- Hidden-state and subclass rejection.
- Stable mapping for unavailable, invalid, resource, parser, and storage
  failures.
- Explicit OLE signature recognition.
- Unknown exception fallback.
- Observer failure isolation.
- No sentinel exception text, path, URL, title, status, body, or personal key in
  result, representation, CLI JSON, logs, or database.
- Existing public error-code and CLI-message regression tests.

### 9.2 D1.1-B

- Schema V11-to-V12 migration, preserved IDs, honest legacy-backfill nullability,
  refresh/completion/candidate gates, provenance/result/run tables, fact-result
  bindings, immutable triggers, and rollback on malformed legacy rows.
- Fixed Docker path, fixed argv, no-shell invocation, `--pull=never`, immutable
  local image ID, and rejection of mutable or substituted images.
- Required network, capability, privilege, seccomp, user, mount, rootfs, CPU,
  memory, PID, timeout, cleanup, and log-isolation controls.
- Timeout, resource, process, daemon, image, malformed-container,
  malformed-output, active-content, external-reference, and cleanup failures.
- Original-artifact checksum, parser-input checksum, fact-set fingerprint,
  canonical parser provenance, and fact-to-result foreign-key authentication.
- Converted-output fund identity, document-kind, and report-period checks.
- Explicit table-boundary preservation and ambiguous-cell rejection.
- Deterministic extraction from the same bytes and parser provenance.
- Temporary failure then success under the same provenance, identical-success
  idempotency, and same-provenance nondeterminism rejection.
- Latest-active-provenance failure blocks fallback to an older success or
  provenance.
- Interrupted refresh, discovery failure, retrieval failure, and container
  failure block independent later classification from reusing historical facts.
- Docker-client termination after container creation, explicit forced cleanup,
  absence verification, and stale-container startup reconciliation.
- Historical failures, successes, facts, classifications, and manifests remain
  immutable and authenticated.
- New classification manifests bind result IDs and provenance checksums, while
  legacy manifest bytes remain unchanged and authenticate through bound facts.
- Exact evidence-manifest V1/V2 decoding rejects mixed, partial, or unknown
  shapes and preserves historical input fingerprints.

### 9.3 D1.1-C

- Latest-per-kind selection and exact-tie conflict handling.
- No fallback from failed newest evidence to older evidence.
- Table and text fixtures for asset allocation, concentration, duration, credit,
  leverage, issuer, convertible, exchangeable, derivative, and foreign facts.
- Missing and incomplete table coverage remains missing evidence.
- Report-period freshness and stale boundaries.
- Real-shape regression fixtures derived from public documents without storing
  private paths or unnecessary raw content.

Each increment runs focused unit/integration tests, the full test suite, Ruff,
format checks for touched files, compileall, `pip check`, and `git diff --check`.

## 10. Acceptance Gates

### D1.1-A Acceptance

- Existing public error codes remain byte-for-byte compatible.
- The representative legacy report returns
  `legacy_ole_container_unsupported` at `container_validation`.
- A fresh isolated live acceptance reports a safe stage and reason for every
  failed candidate item.
- No diagnostic output contains disallowed data.
- The failure distribution is recorded before D1.1-B implementation choices
  are finalized.

All D1.1-A gates were met by v6. The independent financial review passed the
engineering increment but kept the beginner-help score at 58/100 because the
increment improved diagnostics without adding usable fund evidence. D2, D3,
and Phase E still receive no credit.

### D1.1-B Acceptance

- Additive Schema V12 migrates a copy of the accepted local database without
  rebuilding the artifact table or changing existing artifact IDs, fact IDs,
  classifications, or authenticated readback.
- Migrated V11 classifications remain available as authenticated history, while
  current classification stays fail closed until a completed V12 refresh binds
  current candidate outcomes.
- The runtime verifies the exact local image ID and every required isolation
  control; normal synchronization performs no image pull or build.
- At least one predeclared real legacy periodic report completes the official
  landing, attachment, conversion, identity, parse, persistence, and
  authenticated-readback chain.
- The original official OLE checksum, not converted text, anchors provenance.
- The provenance, result, run, and fact records authenticate converter identity,
  normalized parser input, and the successful fact set without persisting
  converted document content.
- A forced unavailable, timeout, and malformed-output acceptance remains
  fail closed and leaves no container or transient output behind. A subsequent
  healthy retry under the same provenance may succeed without rewriting the
  failed run.
- Failed or unsupported legacy documents remain explicit fail-closed outcomes.
- Success and failure rates are reported separately; no 100% claim is implied.

### D1.1-C Acceptance

- A fresh predeclared real sample synchronizes only the newest necessary
  periodic candidates.
- At least one current asset-allocation path completes from official periodic
  evidence.
- A fixed-income sample either completes every Policy V1 current-evidence gate
  or preserves every exact missing-evidence code. A positive
  `high_quality_fixed_income` result is not required if public evidence is
  insufficient.
- A new independent financial review scores the actual beginner workflow and
  does not award D2, D3, or Phase E credit.

## 11. Explicit Non-Goals

- No handwritten OLE binary parser.
- No OCR in D1.1 unless a later separately approved design proves it necessary.
- No full-holdings inference from top-ten disclosure.
- No broad-index classification from fund name or benchmark name alone.
- No high-quality bond classification from `pure bond` wording or mandate
  ceilings alone.
- No lower-tier source promotion.
- No personal profile access, personal amount, allocation, or trade output.
- No D2 overlap/correlation, D3 product selection, or Phase E rebalancing work.
- No claim that D1.1 alone can reach 90% beginner purchase assistance.

## 12. Delivery Sequence

Create separate implementation plans and execution checkpoints for D1.1-A,
D1.1-B, and D1.1-C. Begin with D1.1-A only. The live diagnostic distribution
from A is a required input to the final B implementation plan, and the accepted
legacy-report path from B is a required input to the C plan.

No Git commit is created by the design stage.
