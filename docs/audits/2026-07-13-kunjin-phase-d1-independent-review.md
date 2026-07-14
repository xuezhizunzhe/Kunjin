# KunJin Phase D1 Independent Financial And Beginner-Workflow Review

Date: 2026-07-13

Evidence basis: the current worktree, the Phase D1 design and implementation
plan, current source and focused tests, and the captured real v5 acceptance
record with its isolated database and managed public documents. Test counts
and code volume were not treated as financial evidence.

Independent decision: **Phase D1 does not pass its full stage acceptance.** v5
does establish one authentic tier-1 official `sector_theme` classification as
`concentrated_equity` and `satellite_only`, with `evidence_status=verified`.
The other three samples fail closed for materially defensible reasons. However,
only one of four classifications is verified, the broad-index hypothesis is
still unclassified, 44 of 57 discovered documents fail validation, all sampled
periodic reports fail, official coverage is limited to one manager, and D2,
D3, and Phase E are not implemented. Beginner fund-purchase workflow coverage
is **58/100**. The 90% target is not reached, and a beginner cannot rely on
KunJin to choose or purchase a fund.

## Findings

### P1 - The `164905` verified sector/theme result is genuinely supported by tier-1 official evidence

The v5 result is not a title-only or platform-category inference. Its 12 bound
classification facts come from two current manager-published documents:

| Evidence | Result |
| --- | --- |
| Product summary | Tier 1, official manager domain, separate landing and final attachment URLs, checksum-authenticated OOXML |
| Prospectus update | Tier 1, official manager domain, separate landing and final attachment URLs, checksum-authenticated OOXML |
| Publication date | 2025-10-10 for both current evidence documents |
| Freshness | Current through 2026-10-10 under Policy V1 |
| Authenticated replay | Stored evidence reclassified to the same input fingerprint and result |

The official clauses establish:

- equity-fund legal asset class;
- index-fund legal product type;
- a benchmark with 95% exposure to the named new-energy index;
- index-replication and tracking-error objectives;
- at least 90% of non-cash assets in the index constituents and alternatives;
- an explicit definition of eligible new-energy and new-energy-vehicle
  industries; and
- a formal sector/theme mandate.

The deterministic result is therefore financially defensible as a product
classification:

- `product_family=sector_theme`
- `risk_bucket=concentrated_equity`
- `portfolio_role=satellite_only`
- `evidence_status=verified`
- `reason_codes=[classification_verified]`
- `missing_evidence=[]`
- `conflicts=[]`

Tier-2 profile, size, top-ten holdings, and industry sources are also listed in
the report, but the 12 verified classification facts are bound to the two
tier-1 artifacts. The result does not depend on a lower-tier platform label
overriding official evidence.

This verifies the formal product identity and the Policy V1 role. It does not
verify personal suitability, portfolio fit, current complete holdings,
execution conditions, or whether the fund should be bought. `verified` remains
a narrow evidence-status label.

### P1 - Real document usability remains low: 77.2% of discovered documents failed validation

Across the four v5 samples, production discovery returned 57 documents:

| Result | Count | Rate |
| --- | ---: | ---: |
| Successful parse and publication | 13 | 22.8% |
| Failed with `official_document_invalid` | 44 | 77.2% |

Every success was a product summary or prospectus update:

- 6 product summaries succeeded;
- 7 prospectus updates succeeded;
- 11 annual reports failed;
- 21 quarterly reports failed;
- 11 semiannual reports failed; and
- 1 fund contract failed.

This is the largest remaining production limitation. Legal product identity can
now work, but the real periodic-report path did not accept a single report in
the sample. The public JSON intentionally redacts parser internals, so the 44
failures cannot be independently separated into legacy format, landing-page,
attachment, identity, MIME, or parser-specific causes from the acceptance
record alone.

Failing closed is correct. A 22.8% document success rate is not sufficient
operational coverage for a general real-fund risk service, especially when the
failed set contains the reports needed for current allocation, duration,
credit, leverage, issuer, holdings, and industry evidence.

### P1 - `519706` fails closed safely, but exposes an unresolved broad-index and ETF-feeder coverage gap

The current official product summary and prospectus produced 7 authenticated
facts, including:

- `legal_asset_class=equity_fund`;
- `legal_product_type=index_fund`;
- the tracked benchmark name;
- an index-tracking objective; and
- two minimum-liquid-assets clauses.

The engine nevertheless returns:

- `product_family=unclassified`
- `risk_bucket=unclassified`
- `portfolio_role=not_eligible`
- `evidence_status=unclassified`
- `reason_codes=[classification_unclassified,critical_evidence_missing,official_scope_missing]`
- `missing_evidence=[legal_product_family_evidence_missing]`

The conservative outcome is reasonable. A generic `index_fund` legal type and
benchmark name do not prove broad-index scope, diversification, or core
eligibility. The current system requires an authenticated index methodology
and explicit non-theme scope before promoting an index to `broad_index`.

The missing-evidence label is imprecise: legal index type evidence is present;
the practical gap is broad-versus-theme scope and methodology evidence. This
sample therefore validates safe refusal, not broad-index acceptance. The first
required real-acceptance scenario, separate broad-index family and
core-eligibility results, remains unmet.

### P1 - `519718` identifies an ordinary bond and correctly refuses a risk bucket without current bond evidence

The v5 legal path now succeeds despite the tier-2 basic-profile failure. Two
tier-1 official documents establish a bond fund and pure-bond objective. The
authenticated result is:

- `product_family=ordinary_bond`
- `risk_bucket=unclassified`
- `portfolio_role=not_eligible`
- `evidence_status=partial`

The 20 missing-evidence codes cover both mandate and current-observation gaps
for stock, convertible and exchangeable bonds, derivatives, foreign exposure,
duration, credit quality, below-AA+ exposure, unrated non-sovereign exposure,
leverage, and issuer concentration.

This is a financially appropriate failure-closed result. A name or pure-bond
objective must not be converted into `high_quality_fixed_income`. The failed
holdings synchronization and total absence of accepted periodic reports mean
the system cannot prove the current duration, credit, leverage, or issuer
conditions required by Policy V1.

The result satisfies the acceptance requirement to enumerate missing bond
facts. It does not demonstrate a real bond passing every high-quality gate.

### P1 - `519755` correctly remains partial because top-ten platform disclosures are not complete portfolio evidence

Two current tier-1 official documents establish an equity-mixed mandate and a
0%-95% stock range. Tier-2 profile, fee, size, top-ten holdings, and industry
synchronization also succeeded. The authenticated result is:

- `product_family=equity_mixed`
- `risk_bucket=concentrated_equity`
- `portfolio_role=not_eligible`
- `evidence_status=partial`
- `reason_codes=[classification_partial,critical_evidence_missing,holdings_evidence_missing,industry_evidence_missing]`

The exact missing evidence is current asset allocation, complete holdings,
industry concentration, industry count, largest security, and top-ten totals.

This is reasonable. A successful top-ten endpoint is still only top-ten
disclosure. It cannot prove complete holdings, zero omitted concentration, or
current total asset allocation. The periodic official reports that could
provide stronger evidence all failed validation in v5.

### P1 - D1 coverage is improved but does not meet the full phase target

The four-sample v5 classification distribution is:

| Evidence status | Count | Rate |
| --- | ---: | ---: |
| `verified` | 1 | 25% |
| `partial` | 2 | 50% |
| `unclassified` | 1 | 25% |
| `conflicted` or `stale` | 0 | 0% |

Three of four products receive a formal family: sector/theme, ordinary bond,
and equity mixed. One generic index/ETF-feeder candidate remains unclassified.
This is meaningful progress, but it is not a representative common-fund
coverage rate:

- all four samples use one manager adapter;
- no money-market, active-equity, index-enhanced, QDII, FOF, commodity, REIT,
  convertible, fixed-income-plus, or other manager sample was accepted;
- no real broad index reached family or core-role acceptance;
- no real fixed-income product reached a verified risk bucket;
- no accepted real index-provider methodology is present;
- no v5 stale, superseded, or name/platform-conflict case is recorded; and
- all real periodic reports failed.

The 23 of 24 command exit-zero rate should not be confused with classification
coverage. Financial fail-closed outcomes correctly exit zero. The single
nonzero command was the bond holdings synchronization, which reported no
successful requested disclosure section.

### P1 - D2, D3, and Phase E remain absent and receive zero new credit

D1 provides public-product evidence only. It does not decide whether a product
fits the owner's existing portfolio, which comparable product is preferable,
whether a transaction is currently executable, or how the result should be
monitored after purchase.

Missing D2 controls include correlation and stress co-movement, same-theme and
same-manager aggregation, complete-holdings overlap, candidate-versus-current
portfolio limits, and post-purchase industry, factor, country, currency,
issuer, credit, duration, and risk-bucket concentration.

Missing D3 controls include product selection, transaction-specific fees and
taxes, channel and share-class checks, subscription/redemption state, limits,
settlement, lockup, liquidity, and minimum-purchase validation.

Phase E target bands, drift policy, rebalancing authorization, and ongoing
decision monitoring are also unimplemented. All three areas receive **zero new
credit** in this audit.

### P2 - Policy thresholds remain screening assumptions rather than independently validated financial standards

The bond gate is conservative but not empirically validated. Exact zero stock,
convertible, exchangeable, below-AA+, and unrated exposure creates false
precision when disclosures round or omit positions. A five-year duration and
120% leverage ceiling can still permit material risk. Domestic ratings do not
fully capture subordination, guarantor quality, structured credit, liquidity,
or downgrade risk. A 10% issuer ceiling does not aggregate related issuers,
guarantors, sectors, or sponsors.

The sector/theme result is much more defensible as concentrated and
satellite-only than as a complete risk assessment. Formal 90% index-constituent
exposure establishes thematic identity, but it does not measure current
constituent concentration, liquidity, turnover, valuation, or tracking quality.

Broad-index gates based on constituent count and largest-name, top-ten,
industry, and industry-count limits do not establish country, board, size,
factor, ownership, currency, liquidity, capacity, replication, derivatives, or
governance quality. A broad family and `core_eligible` role would still require
D2 portfolio context before user reliance.

Freshness remains policy-defined rather than event-complete. A one-year legal
document window can miss an intervening mandate or methodology change, while
period-end deadlines can retain economically old holdings. Legal validity,
review due date, latest expected report, constituent as-of date, publication
delay, and source availability should remain distinguishable.

### P2 - `verified`, `core_eligible`, and `high_quality_fixed_income` can still be overread

`verified` means that Policy V1 found all evidence required for that narrow
classification. In the sector/theme path it does not require current complete
holdings. It does not mean low risk, high quality, suitable, fairly valued,
liquid, cheap, or worth buying.

`core_eligible` sounds like portfolio approval even though D2 is absent.
`high_quality_fixed_income` sounds independently quality-assured even though no
real v5 bond reached that bucket and the thresholds remain uncalibrated.
`cash_like_candidate` remains distinct from protected cash.

Every user-facing result must retain the `research_only` capability, exact
reason and missing-evidence codes, and the explicit `d2_d3_not_evaluated`
limitation.

## Verified D1 Capabilities

- Current networked amount-free JSON acceptance through profile, holdings,
  official-document, classify, evidence, and history commands.
- One real tier-1 official sector/theme classification reaching
  `verified` / `concentrated_equity` / `satellite_only`.
- Real ordinary-bond and equity-mixed family identification with conservative
  partial results and exact missing evidence.
- Safe refusal to promote an index/ETF-feeder product without formal broad or
  thematic scope evidence.
- Manager-domain, fund-identity, share-class, landing-page, attachment,
  publication-date, OOXML, checksum, and freshness validation.
- Separate persisted landing and final attachment URLs.
- Immutable artifact, fact, policy, and classification records with
  fingerprint-authenticated readback.
- Deterministic failure-closed classification and `research_only` boundaries.

These capabilities satisfy an important subset of D1. They do not establish
general source coverage or purchase readiness.

## Beginner Purchase-Workflow Coverage

This is a fresh application of the same Phase C 100-point, ten-area rubric.
Scores measure verified beginner workflow help, not code volume, command exit
rate, or expected investment performance. D2, D3, and Phase E receive zero new
credit.

| Decision area | Weight | Score | Independent assessment |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 10 | No D1 change; Phase B gates remain useful, but tax, insurance, irregular spending, user classification, and wider affordability remain incomplete |
| Risk capacity and willingness | 10 | 5 | No D1 change; subjective inputs and fixed stress coefficients still limit confidence |
| Goals and investment horizon | 10 | 7 | No D1 change; useful sleeves and zero-return states remain incomplete for inflation, probabilities, and full multi-goal planning |
| Asset allocation and risk budget | 15 | 4 | D2 gets zero new credit; Phase C remains an abstract feasible region without real product mapping, targets, contribution mix, or purchase amount |
| Fund-category identification | 10 | 7 | One tier-1 official theme fund is verified and two other real families are identified conservatively; the broad-index case remains unclassified and family/manager coverage is narrow |
| Individual fund quality research | 15 | 11 | Real official legal documents, immutable facts, provenance, NAV/manager/fee/size/benchmark/disclosure context, and authenticated classification are useful; 44 of 57 documents fail, all periodic reports fail, and valuation, attribution, flows, liquidity, and broad official coverage remain incomplete |
| Portfolio overlap and concentration | 10 | 5 | D2 gets zero new credit; HHI, largest position, and top-ten disclosed overlap remain partial with no proposed-purchase or complete-look-through controls |
| Fees, purchase, and redemption conditions | 5 | 3 | D3 gets zero new credit; sourced schedules exist but transaction-specific fee, tax, channel, share-class, liquidity, settlement, and execution checks do not |
| Monitoring and rebalancing | 5 | 2 | Phase E gets zero new credit; there are no target bands, drift policy, or rebalancing actions |
| Source provenance, freshness, and conflict handling | 5 | 4 | Accepted evidence has strong tier, checksum, URL, date, freshness, and authenticated-binding contracts; one manager, no accepted methodology provider, opaque document-invalid causes, and low report coverage prevent full credit |
| **Total** | **100** | **58** | **58% verified beginner-workflow coverage** |

The increase from the prior review is limited to new v5 evidence: one real
sector/theme result is verified from tier-1 documents, and the bond and mixed
samples now demonstrate truthful family-level partial outcomes. No points were
awarded for the 23/24 command exit rate, number of files, test count, code
volume, D2, D3, or Phase E.

## 90% And Beginner-Purchase Conclusion

KunJin does **not** provide 90% of the reasonably automatable help a beginner
needs to purchase funds. The independent score is **58/100**.

A beginner can use D1 to understand why one real product is formally thematic
and why several other products cannot yet receive a safer classification. A
beginner cannot rely on the current system to choose or buy a fund. The system
does not establish broad common-fund coverage, portfolio fit, product selection,
transaction readiness, or ongoing rebalancing controls.

No D1 result, including `verified`, is a buy, hold, add, reduce, sell,
rebalance, target-weight, contribution, or purchase-amount signal.

## Phase Decision

**Phase D1 stage decision: FAIL / not accepted as complete.**

The following stage evidence is now credible:

- source-traceable real official documents and normalized facts;
- deterministic and authenticated classifications;
- a verified real sector/theme outcome;
- a bond outcome enumerating missing high-quality evidence;
- an evidence-incomplete index outcome that stays unclassified; and
- privacy-preserving amount-free JSON results.

The following stage conditions remain unmet or insufficiently demonstrated:

- a real broad-index family and separate core-eligibility result;
- an accepted operational index-methodology provider path;
- usable annual, semiannual, and quarterly official-report ingestion;
- a real fixed-income product passing every high-quality gate;
- representative cross-manager and cross-family coverage measurement;
- recorded real stale/superseded and name/platform-conflict acceptance cases;
- calibrated financial thresholds and safer public semantics; and
- D2, D3, and Phase E implementation and independent acceptance.

The verified theme result is a real milestone. It is not enough to declare the
full D1 stage complete.

## Next Acceptance Priorities

1. Diagnose the 44 `official_document_invalid` cases without weakening source,
   identity, MIME, attachment, or parser security, then accept current periodic
   reports end to end.
2. Add and accept an authenticated index-methodology provider path, then prove
   one real broad-index family separately from core eligibility.
3. Parse current bond duration, credit, leverage, issuer, stock, convertible,
   and exchangeable observations and demonstrate one real high-quality pass or
   fully evidenced disqualification.
4. Publish a predeclared multi-manager, multi-family sample with technical
   document success and classification-state rates reported separately.
5. Record real stale/superseded and name/platform-conflict cases.
6. Calibrate and qualify Policy V1 thresholds and labels before beginner
   reliance.
7. Implement and independently accept D2, D3, and Phase E before any end-to-end
   purchase claim.

## Verification Record

- The v5 database authenticated and deterministically recomputed all four
  stored results with matching input fingerprints and classifications.
- The two `164905` official document checksums match their managed OOXML files.
- Focused D1 source, document, parser, engine, policy, store, service, research,
  and Schema V10/V11 tests: 211 passed.
- The main v5 verification record reports: full pytest 1073 passed, Ruff
  checks passed, compileall passed, `pip check` passed, and `git diff --check`
  passed. The design and implementation plan now record the v5 results.
- No default personal database, Keychain, non-JSON suitability, or non-JSON
  allocation command was accessed.
- v5 is a recorded live network acceptance. This independent review did not
  issue additional network requests; it authenticated the captured database,
  documents, checksums, JSON results, and deterministic recomputation locally.
- No personal amount or private profile name is included.
- The score arithmetic is 10 + 5 + 7 + 4 + 7 + 11 + 5 + 3 + 2 + 4 = 58.
- Only this audit document was modified by this review task.
