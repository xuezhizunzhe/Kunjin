# KunJin Phase D1.1-C Current-Holdings Coverage And Independent Review

Date: 2026-07-15

## Decision

The bounded D1.1-C current-report adapter has completed its declared engineering
scope, but full D1 cannot close. Automatic official-document discovery covered
only one of ten current held-fund codes, and no held fund reached a `verified`
classification. The observed current-holdings sample therefore is not automatic
onboarding for a beginner.

The independent beginner-workflow score is **54/100**, down from the prior
58/100. The 90 percent target is not reached. D2, D3, and Phase E remain
unimplemented and receive no new credit. No result in this review is a
direction, position size, target, real-product Phase C mapping, or purchase
authorization.

## Private Scope And Method

KunJin synchronized the owner's current portfolio privately. The audit used
only the ten unique amount-free fund codes:

`001060`, `003411`, `008888`, `011613`, `011840`, `016067`, `017811`,
`027329`, `160610`, and `519755`.

For every code, the audit ran profile, holdings, official-document, classify,
and authenticated classification-evidence commands in the declared order. It
did not hand-edit the database, add a fund-specific exception, copy an amount,
share count, cost, profit, account identifier, managed path, raw document,
source excerpt, URL, fingerprint, or canonical manifest into this audit.

The local converter was `ready` with parser `4-docker-libreoffice-v1`. Its
successful availability is a technical prerequisite, not financial evidence.

## Objective Coverage

| Measure | Result | Interpretation |
| --- | ---: | --- |
| Unique held-fund codes | 10 | Amount-free denominator |
| Basic-profile success | 7 / 10 | Three codes returned `unsupported_fund_status` |
| Manager-history success | 10 / 10 | Tier-2 profile evidence only |
| Both tier-2 disclosure sections successful | 8 / 10 | Two funds failed the requested holdings sync |
| Official-document discovery and parse success | 1 / 10 | 10 percent automatic D1 source coverage |
| All three current periodic kinds selected | 1 / 10 | Annual, quarterly, and semiannual |
| At least one current asset-allocation fact | 1 / 10 | Two facts in total, stock and bond |
| Authenticated `verified` classification | 0 / 10 | Zero authenticated verified classifications |
| Authenticated `stale` classification | 1 / 10 | `519755` |
| Authenticated classification `missing` | 9 / 10 | No current classification existed |
| Assessable classification conflicts | 0 / 1 | Nine missing classifications had no conflict state to assess |
| Authenticated current industry facts | 0 / 10 | Production controlled-taxonomy registry is empty |

The successful code, `519755`, selected one candidate for each periodic kind
and did not fall back to an older candidate. Its current quarterly report
produced exact stock and bond percentages with a `2026-03-31` effective date.
It remained `research_only`, `stale`, and `not_eligible`; its annual evidence
was stale and its authenticated result still lacked holdings, industry,
largest-security, and top-ten evidence.

Nine codes failed official-document discovery with the exact technical chain:

- top-level code: `official_document_unavailable`;
- `failure_stage=discovery`; and
- `failure_reason=http_unavailable`.

Those failures produced `classification-evidence.status=missing` and no
selection or classification conflict state to assess. KunJin did
not reuse history, downgrade to a platform mirror, infer a product family from
the fund name, or present missing facts as zero.

Two tier-2 holdings synchronizations also failed independently:

| Code | Quarterly holdings | Industry exposure | Top-level error |
| --- | --- | --- | --- |
| `003411` | `missing_publication_date` | `missing_industry_exposure` | `fund_disclosure_sync_failed` |
| `027329` | `invalid_disclosure_date` | `missing_industry_exposure` | `fund_disclosure_sync_failed` |

These disclosure failures are not product-family or purchase signals.

## Selection And Attempt Bounds

Nine latest official-document refreshes recorded zero candidate attempts
because discovery failed before selection. The one successful refresh recorded
three periodic attempts in total: one annual, one quarterly, and one
semiannual. Therefore:

- fund-level periodic-attempt median: `0`;
- fund-level periodic-attempt maximum: `3`;
- attempted periodic-kind median: `1`;
- attempted periodic-kind maximum: `1`; and
- historical fallback count: `0`.

The low median does not indicate efficiency across the held sample. It mainly
records that nine funds never reached candidate selection.

## Source-Coverage And Adapter Gap Diagnosis

The technical code `http_unavailable` alone does not prove that a public source
is objectively absent or that an external outage is unavoidable. Repository
inspection provides the missing context: the production official-source
registry contains only the audited `fund001` manager registration for
交银施罗德基金管理有限公司. An unmatched manager falls back to that sole fund-
identity-bound source.

The nine failing codes therefore require audited official manager-domain
registrations, or evidence of another supported official route, before a
document-shape adapter can be evaluated:

`001060`, `003411`, `008888`, `011613`, `011840`, `016067`, `017811`,
`027329`, and `160610`.

For `003411`, `011613`, and `017811`, the tier-2 basic-profile parser also
returns `unsupported_fund_status`; their identity/status handling must be
resolved before relying on manager routing. Two observed codes share a manager,
so coverage work should be manager-domain based rather than fund-specific.

No new document-shape adapter is justified from these nine failures. Discovery
did not yield authenticated candidate documents, so claiming a parser-shape
problem would be speculation. After domain discovery exists, each new shape
must pass the same bounded identity, date, container, conversion, parser,
selection, Manifest V3, and freshness gates.

The production industry taxonomy is still empty. Adding domain adapters alone
will not produce authenticated current industry observations.

## Beginner Errors Prevented

The current workflow materially helps prevent these errors:

- treating a fund name or platform category as official product evidence;
- treating a missing holding, industry, or stock row as zero exposure;
- silently using an older periodic report after a current candidate fails;
- treating a successful conversion as financial evidence;
- presenting tier-2 profile or disclosure data as tier-1 official evidence;
- hiding stale, missing, conflict, and technical-failure states behind a
  successful command; and
- turning a D1 family result into a buy signal or purchase amount.

## Beginner Errors Not Yet Prevented

The current workflow cannot reliably prevent these central purchase errors:

- buying another fund with the same theme, manager, factor, issuer, or hidden
  look-through exposure;
- choosing among peer funds, share classes, channels, and fee schedules;
- buying while subscription, redemption, liquidity, settlement, limit, or tax
  conditions are unsuitable;
- mapping a real fund into the Phase C abstract layers or choosing a target
  weight and contribution amount;
- assessing complete fixed-income credit, duration, leverage, issuer,
  convertible, derivatives, country, and currency risk;
- distinguishing broad-index core suitability from a narrow thematic index
  across the observed portfolio; and
- monitoring target bands, drift, thesis invalidation, or a rebalancing action.

## Independent Beginner-Workflow Score

The same ten-area rubric is applied to verified workflow help, not code volume,
test count, or the number of technical gates.

| Decision area | Weight | Score | Independent assessment |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 10 | Phase B remains useful; tax, insurance, irregular spending, and wider affordability remain incomplete |
| Risk capacity and willingness | 10 | 5 | Transparent inputs exist; subjective inputs and fixed stress assumptions still limit confidence |
| Goals and investment horizon | 10 | 7 | Goal sleeves and zero-return states help; inflation, probability, and complete planning remain absent |
| Asset allocation and risk budget | 15 | 4 | Phase C remains an abstract feasible region; D2 real-product fit and targets are absent |
| Fund-category identification | 10 | 5 | Held-fund official coverage is 1/10, with nine missing and zero verified classifications |
| Individual fund quality research | 15 | 9 | Only one held fund produced current periodic facts, and it still lacks holdings, industry, top-ten, and largest-security evidence |
| Portfolio overlap and concentration | 10 | 5 | Existing weights and top-ten overlap are partial; D2 construction controls are absent |
| Fees, purchase, and redemption conditions | 5 | 3 | Schedules exist; transaction-specific D3 checks are absent |
| Monitoring and rebalancing | 5 | 2 | Synchronization and reports exist; Phase E policy and actions are absent |
| Source provenance, freshness, and conflict handling | 5 | 4 | The successful path is strongly authenticated; observed official-domain coverage is only 10 percent |
| **Total** | **100** | **54** | **54 percent verified beginner-workflow coverage** |

The fresh current-holdings measurement causes a four-point downward
reassessment; this is not an implementation regression. Fund-category
identification falls from 7 to 5, and individual-fund research falls from 11 to
9. The current held-fund sample contains zero verified classifications and nine
funds without official-document discovery. Parser v4, Manifest V3, bounded
attempts, and stable errors prove engineering reliability; they do not replace
financial decision coverage.

## 90 Percent And Phase Decision

KunJin does **not** provide 90 percent of the reasonably automatable help a
beginner needs to purchase funds. The evidence-backed score is **54/100**.
It is useful as a private financial-safety gate, evidence organizer, and strict
refusal system, but it does not complete portfolio construction, product
selection, transaction readiness, or ongoing monitoring.

**D1.1-C engineering decision: PASS for its bounded current-report adapter and
audit scope. Full D1 decision: FAIL / remain open.** D1 should not be declared
complete before D2. The next D1 work must first raise manager-domain coverage
for the observed common public funds, preserve the same failure-closed
contracts, populate an independently reviewed controlled industry taxonomy,
and demonstrate materially broader `verified` coverage. D2 design may be
studied in parallel, but no D2 portfolio-fit result may depend on a fund whose
D1 evidence is missing or stale.
