# KunJin Phase D1.1-A Independent Financial And Beginner-Workflow Review

Date: 2026-07-13

Scope: the current D1.1-A design, implementation plan, worktree, tests, isolated
v5 and v6 public acceptance records, and the v6 isolated database and public
document artifacts. This review evaluates safe diagnostic acceptance and its
financial significance. Test count and observability were not treated as new
financial capability.

Independent decision: **D1.1-A passes its diagnostic substage acceptance.** v6
reports a safe allowlisted stage and reason for every failed document, preserves
all existing public error codes, leaves success diagnostics null, exposes no
disallowed detail, and does not change any classification fact or financial
outcome. The full D1 phase remains incomplete because D1.1-A recognizes but
does not convert or parse the 44 legacy reports. The objective beginner
fund-purchase workflow score remains **58/100**. D2, D3, and Phase E receive no
new credit, and the 90% target is not reached.

## Findings

### P1 - D1.1-A meets every defined diagnostic acceptance gate

The v6 acceptance contains the same 57 discovered public documents as v5:

| Result | Count | Rate |
| --- | ---: | ---: |
| Successful parse and publication | 13 | 22.8% |
| Failed candidate items | 44 | 77.2% |

All 44 failed items preserve:

- `error_code=official_document_invalid`
- `failure_stage=container_validation`
- `failure_reason=legacy_ole_container_unsupported`

No failed item is missing either diagnostic field. All 13 successful items
return `error_code=null`, `failure_stage=null`, and `failure_reason=null`.

This satisfies the four explicit D1.1-A gates:

1. Existing public error codes remain compatible.
2. Representative legacy reports return the required container stage and OLE
   reason.
3. Every failed live candidate has an allowlisted safe stage and reason.
4. The public diagnostics contain no disallowed data.

The result also replaces the prior undifferentiated observation that 44 items
were merely `official_document_invalid`. The evidence now shows one coherent
failure cohort rather than 44 unknown causes.

### P1 - The entire failed cohort is legacy OLE, which makes bounded conversion the evidence-based next step

The v6 distribution is exact across all failed document kinds:

| Failed document kind | Count | Stage | Reason |
| --- | ---: | --- | --- |
| Annual report | 11 | `container_validation` | `legacy_ole_container_unsupported` |
| Quarterly report | 21 | `container_validation` | `legacy_ole_container_unsupported` |
| Semiannual report | 11 | `container_validation` | `legacy_ole_container_unsupported` |
| Fund contract | 1 | `container_validation` | `legacy_ole_container_unsupported` |
| **Total** | **44** |  |  |

The concentration is financially important because every sampled periodic
report is in the failed cohort. Current asset allocation, holdings completeness,
duration, credit, leverage, issuer, convertible, exchangeable, derivative, and
foreign-exposure evidence therefore remain unavailable to the D1 engine.

The distribution supports proceeding to D1.1-B bounded legacy-DOC conversion.
It does not prove that conversion output will be parseable, identity-matched,
complete, or financially sufficient. D1.1-B must retain fail-closed behavior at
every conversion and parser boundary.

### P1 - v6 adds no financial evidence and correctly leaves all v5 classifications unchanged

Fresh synchronization changes retrieval-bound evidence and therefore refreshes
all four input fingerprints. That is expected. It is not a classification
change.

For every public code, v5 and v6 have identical:

- tier-1 source checksums;
- verified fact fingerprints;
- verified fact payloads;
- product family;
- risk bucket;
- portfolio role;
- evidence status;
- classification reason codes;
- missing-evidence codes; and
- conflict codes.

Authenticated recomputation from the v6 immutable store also matched every
stored input fingerprint and classification:

| Public code | Product family | Risk bucket | Role | Evidence status |
| --- | --- | --- | --- | --- |
| `519706` | `unclassified` | `unclassified` | `not_eligible` | `unclassified` |
| `164905` | `sector_theme` | `concentrated_equity` | `satellite_only` | `verified` |
| `519718` | `ordinary_bond` | `unclassified` | `not_eligible` | `partial` |
| `519755` | `equity_mixed` | `concentrated_equity` | `not_eligible` | `partial` |

This is the correct financial invariant. Diagnostic metadata explains a
technical boundary; it must not improve or worsen a product family, risk
bucket, role, or evidence state.

### P1 - D1.1-A does not close the current-risk evidence gap

D1.1-A deliberately stops after recognizing the exact Compound File Binary
container. It does not convert, parse, publish, or classify facts from the
legacy document.

Consequently:

- no periodic report moves from failure to success;
- the document success rate remains 22.8%;
- the broad-index candidate remains unclassified;
- the ordinary-bond candidate still lacks 20 mandate and current-observation
  evidence items;
- the equity-mixed candidate still lacks current allocation and complete
  holdings/concentration evidence; and
- no real bond reaches `high_quality_fixed_income`.

Safe observability improves the next engineering decision. It does not yet
improve the beginner's ability to evaluate a fund.

### P1 - Full D1 remains incomplete and later phases receive zero credit

Full D1 still lacks:

- accepted legacy periodic-report conversion and parsing;
- current official report evidence for material risk fields;
- a real broad-index family and separate core-role result;
- an operational official index-methodology provider path;
- a real fixed-income product passing every high-quality gate;
- representative cross-manager and cross-family coverage; and
- real stale, superseded, and source-conflict acceptance cases.

D2 portfolio correlation, overlap, construction, and post-purchase exposure
controls remain unimplemented. D3 product selection, transaction-specific fees,
tax, channel, share-class, liquidity, settlement, and execution checks remain
unimplemented. Phase E target bands, drift policy, and rebalancing controls also
remain unimplemented. These areas receive **zero new credit**.

### P2 - The additive public contract is compatible and privacy-preserving

v6 adds exactly two optional document-item fields:

- `failure_stage`
- `failure_reason`

All legacy document-item fields are equal between v5 and v6, including status,
fact count, warnings, conflicts, and public error code. The command envelope is
unchanged. The only capability label remains `research_only`, and every fund
identifier remains a six-digit public code.

All 88 emitted diagnostic values match the stable lowercase-code pattern. The
captured JSON and stderr contain no local path, managed path, traceback,
exception text, raw response body, document body, personal financial field, or
private profile field. All stderr files are empty.

The implementation uses exact enums and a frozen three-field failure record.
It rejects subclasses, hidden state, unknown codes, and free-form attributes.
Unknown failures fall back to `unspecified` / `unspecified_failure` rather than
publishing exception text. Observer failure cannot alter the sync result.

### P2 - Non-persistence is appropriate for this bounded diagnostic increment

The isolated v6 database remains Schema V11. It has no diagnostic/failure table
and no `failure_stage` or `failure_reason` persistence columns. This matches the
approved D1.1-A design: diagnostics are returned for the current sync item and
do not change immutable artifact or classification identity.

This limits historical diagnostic analysis across processes, but adding a
schema migration before a concrete history requirement would create complexity
without financial benefit. The current non-persistent design is appropriate
for choosing D1.1-B.

### P2 - Diagnostic specificity is sufficient for A, not for claiming conversion success

`container_validation` / `legacy_ole_container_unsupported` is precise enough
to distinguish this cohort from network, identity, landing-page, MIME, parser,
resource, and storage failures. It does not reveal whether a future converter
will preserve tables, text order, dates, formulas, embedded content, or product
identity.

The next phase must not interpret one diagnostic code as proof that every OLE
document is safe or semantically equivalent after conversion. Original bytes,
official provenance, document identity, resource bounds, and converted output
must each be validated independently.

## Beginner Purchase-Workflow Coverage

This is a fresh application of the established Phase C 100-point, ten-area
rubric. D1.1-A is diagnostic-only. Tests and observability earn no points unless
they add verified financial decision capability. D2, D3, and Phase E receive
zero new credit.

| Decision area | Weight | Score | Independent assessment |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 10 | No D1.1-A change; Phase B gates remain useful but wider affordability, tax, insurance, and irregular spending remain incomplete |
| Risk capacity and willingness | 10 | 5 | No D1.1-A change; subjective inputs and fixed stress coefficients still limit confidence |
| Goals and investment horizon | 10 | 7 | No D1.1-A change; inflation, probabilities, and complete multi-goal planning remain incomplete |
| Asset allocation and risk budget | 15 | 4 | D2 gets zero new credit; Phase C remains an abstract feasible region without real product mapping or purchase amount |
| Fund-category identification | 10 | 7 | v6 preserves the verified theme and two partial family outcomes, but adds no category evidence; broad-index scope remains unresolved |
| Individual fund quality research | 15 | 11 | Failure causes are safer to understand, but no legacy report is parsed and no new current-risk fact is available; score unchanged |
| Portfolio overlap and concentration | 10 | 5 | D2 gets zero new credit; top-ten disclosed overlap is not complete look-through or candidate-fit analysis |
| Fees, purchase, and redemption conditions | 5 | 3 | D3 gets zero new credit; there is no transaction-specific pre-purchase execution validation |
| Monitoring and rebalancing | 5 | 2 | Phase E gets zero new credit; target bands, drift policy, and rebalancing actions remain absent |
| Source provenance, freshness, and conflict handling | 5 | 4 | Diagnostics improve technical traceability, but no new source is accepted and full official-report coverage remains unavailable; no score increase |
| **Total** | **100** | **58** | **58% verified beginner-workflow coverage** |

The score remains 58/100. Awarding additional points for allowlisted diagnostic
codes, tests, or command observability would confuse software operability with
financial decision support.

## 90% And Beginner-Purchase Conclusion

KunJin does **not** provide 90% of the reasonably automatable help a beginner
needs to purchase funds. A beginner benefits from a clearer explanation of why
periodic reports are unavailable, but still cannot use those reports for
current-risk evaluation.

No D1 or D1.1-A result is a buy, hold, add, reduce, sell, rebalance,
target-weight, contribution, or purchase-amount signal.

## Substage And Phase Decisions

**D1.1-A decision: PASS.**

The diagnostic taxonomy is complete for the live failed cohort, compatible,
privacy-preserving, and financially inert. The failure distribution is
sufficiently specific to choose the next engineering increment.

**Full D1 decision: still incomplete / not accepted.**

D1.1-A identifies the blocker but does not make any failed report usable. Full
D1 acceptance still depends on D1.1-B and D1.1-C evidence plus the existing
broad-index, provider, bond, and coverage gaps.

## Evidence-Based Next Step For D1.1-B

Proceed with a bounded legacy-DOC conversion path because all 44 live failures
belong to the exact supported target cohort. D1.1-B acceptance should require:

1. A fixed validated converter binary invoked without a shell.
2. Strict timeout, input/output-size, process, and resource limits.
3. The original official OLE bytes and checksum as the immutable provenance
   anchor; converted text must not replace source identity.
4. Converted output validation for format, product identity, document kind,
   publication/report dates, and absence of active or embedded content.
5. At least one predeclared real periodic report completing landing,
   attachment, conversion, parse, persistence, and authenticated readback.
6. Separate success and failure rates with no claim that every legacy document
   is convertible.
7. Unavailable, timed-out, oversized, malformed, identity-mismatched, or
   incomplete conversions remaining explicit fail-closed outcomes.

D1.1-B should prove safe conversion first. Extraction of current risk facts and
latest-report selection belongs to D1.1-C and must be independently accepted.

## Verification Record

- v6 distribution: 57 documents, 13 successes, 44 failures.
- Failed diagnostics: 44/44
  `container_validation` / `legacy_ole_container_unsupported`.
- Missing failed diagnostics: 0.
- Non-null diagnostics on successful items: 0.
- Existing v5 document-item fields are equal in v6; only the two nullable safe
  fields were added.
- All v5/v6 financial fact payloads, tier-1 checksums, and classification
  outcomes are equal; all four fresh input fingerprints changed as expected.
- Authenticated v6 deterministic recomputation matched all four stored results.
- Focused failure, document, parser, service, store, CLI, logging, and smoke
  tests: 213 passed.
- Schema remains V11 with no diagnostic persistence object.
- JSON and stderr privacy/public-code checks passed.
- No default personal database, Keychain, network request, non-JSON suitability,
  or non-JSON allocation command was used in this independent review.
- Score arithmetic: 10 + 5 + 7 + 4 + 7 + 11 + 5 + 3 + 2 + 4 = 58.
- No code or existing audit file was modified by this review.
