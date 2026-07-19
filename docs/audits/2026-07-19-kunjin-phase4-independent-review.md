# KunJin Phase 4 Independent Financial And Product Review

## Decision

Phase 4 is a technically disciplined evidence-composition feature, not a usable
fund-selection adviser. It improves one part of the third owner goal: given two
to five exact fund codes, KunJin can place existing local comparison, D1 product,
portfolio-relationship, suitability, and allocation evidence into one bounded,
unordered report. It does not discover candidates, refresh evidence, decide what
to buy, recommend an amount or channel, or tell the owner when to sell.

The real owner result did not produce a conditional shortlist. Both candidates
were `insufficient_data`, and the group state was `insufficient_data`. Therefore
the owner acceptance proves privacy, deterministic degradation, and
non-authorization on a real read-only database copy. It does not prove that
Phase 4 can currently compare the owner's intended candidates, identify a
financially preferable fund, or support a purchase decision.

The claim that KunJin now provides 90% of the help a beginner needs is not
supportable. A beginner still lacks authenticated current market direction,
complete product and look-through evidence, a usable personal capacity gate in
the observed owner run, purchase execution checks, and mature hold/sell
monitoring. Phase 4 improves presentation and abstention more than real financial
decision coverage.

## Evidence Reviewed

- Phase 4 design and completion contract;
- current README Phase 2, Phase 3, Phase 4, and limitations sections;
- `src/kunjin/selection/{models,policy,service,research}.py` and their unit and
  CLI integration tests;
- Phase 2 pragmatic MVP and Phase 3 independent financial reviews;
- Phase 4 commits `a886421`, `6318812`, and `c585a45`; and
- the supplied anonymous owner acceptance facts. The private owner run was not
  repeated for this review.

Fresh independent verification on 2026-07-19 produced:

- complete repository regression: 2877 passed in 237.45 seconds;
- Phase 4 local acceptance: 183 passed, no process residue;
- Phase 4 fault acceptance: 73 passed, no process residue; and
- Ruff on the selection implementation and relevant tests: passed;
- Phase 4 acceptance-script Bash syntax and `git diff --check`: passed; and
- repository KunJin Skill validation and the two Phase 4 smoke contracts:
  passed (`Skill is valid!`; 2 passed).

These results are strong evidence for code contracts and regression safety. The
local and fault suites are synthetic and reuse overlapping test files; their
counts are not independent proof of live financial usefulness.

## Owner Acceptance: What It Actually Proves

The supplied owner summary was:

- `candidate_count=2`;
- `comparison_state=insufficient_data`;
- `evidence_state_counts={insufficient_data: 2}`;
- `suitability_state=transient` and `allocation_state=transient`;
- `action_maturity=evidence_only`;
- `action_authorized=false`;
- `exact_amount_available=false`;
- `automatic_trade=false`;
- `privacy_scan_passed=true`;
- `real_database_opened_read_only=true`;
- `shortlist_ran_on_private_copy=true`; and
- no reported conflicts.

Missing categories included `allocation_status_unavailable`,
`suitability_status_unavailable`, the reported
`d1_classification_missing` / `d1_classification_unavailable` conditions,
`current_benchmark_<fund>`, `holdings_evidence_missing_<fund>`,
`identity_evidence_missing_<fund>`, and
`authenticated_index_identity_<fund>`.

This is a successful fail-closed privacy test. It is a failed financial
shortlist result. The acceptance script also chooses the two codes with the
largest count of populated evidence tables; it does not replay a real owner's
named-candidate question. That selection makes the result useful for system
degradation testing but weaker as product acceptance. Even the locally
best-covered pair did not have enough authenticated evidence for a usable
comparison.

An empty conflict list must not be read as source agreement. With identity,
classification, benchmark identity, and holdings evidence missing, the system
often lacks the evidence needed to detect a conflict in the first place.

## What Phase 4 Improves

- It consolidates several scattered commands into one report for exactly two to
  five owner-supplied codes.
- It preserves common NAV dates and metric-specific return, volatility,
  drawdown, manager, fee, size, and disclosed-overlap evidence instead of
  manufacturing a universal score.
- It reuses the Phase 3 observed-portfolio-impact projection and keeps missing
  holdings unknown rather than treating them as zero exposure.
- It requires verified D1 evidence, a narrow common Phase C layer, usable
  observed portfolio impact, and fresh non-blocked personal gates before using
  `conditional_shortlist`.
- It blocks held-candidate marginal-impact claims without a purchase amount and
  always remains `evidence_only`, amount-free, and non-trading.
- It gives a beginner one place to see why a comparison is unavailable. In the
  real owner run, that explanation is the principal delivered value.

## What Phase 4 Does Not Improve

- News and industry coverage are unchanged. Phase 2's limited media breadth,
  partial source outcome, failed live market source, and zero authenticated
  controlled-industry coverage remain material.
- It does not establish current market direction or answer what to buy. Formal
  NAV history and disclosed holdings are not live quotes, valuation evidence,
  market timing, or a forecast.
- It does not resolve names or discover candidates inside the command. The owner
  must already have two to five exact codes, which is a substantial part of a
  beginner's original problem.
- It does not refresh fund identity, holdings, NAV, peer, D1, suitability, or
  allocation evidence. The user must orchestrate those prerequisites elsewhere.
- Disclosed top-ten holdings are dated, incomplete snapshots. Missing holdings
  do not support a diversification, exposure, or economic-cycle conclusion.
- `suitability_state=transient` and `allocation_state=transient` mean the observed
  run gave no usable personal economic-capacity or allocation guidance.
- It does not implement complete D2 look-through, correlation, stress testing,
  valuation/fundamentals, D3 transaction and channel checks, exact amounts, or
  Phase E monitoring and sell timing.

There is also a product-language risk: `conditional_shortlist` sounds closer to
a recommendation than its contract permits. The implementation repeatedly says
it is unordered and not a buy signal, but a beginner can still overread the
label. Any future UI must put the abstention and missing gates ahead of the list,
not below it. Allowing a Phase B `constrained` state to pass the shortlist gate
also requires especially clear display of every retained constraint; it must
never look like personal purchase approval.

## Five Owner Goals

The following 0-100 figures are deliberately rough judgment bands, not measured
scientific scores. They describe current observed usefulness, including the
Phase 2 and Phase 3 evidence inherited by Phase 4.

| Owner goal | Current usefulness | Independent assessment |
| --- | ---: | --- |
| 1. News and industry understanding | about 50/100 | Dated bounded news can support learning, but source breadth is narrow, prior live coverage was partial, industry authentication is effectively absent, and Phase 4 adds nothing here. |
| 2. Market direction and how to buy | about 15/100 | The system can explain why it must abstain. It still lacks accepted live market dimensions, valuation, a mature buy gate, channel checks, and an amount. Phase 4 adds no direction. |
| 3. Named candidates plus market, holdings, and personal-economic advice | about 35/100 | The workflow and synthetic comparison capability improve materially, but the real pair produced only `insufficient_data`; names must already be resolved, holdings are disclosed snapshots, and personal gates were transient. This is candidate evidence organization, not selection advice. |
| 4. Daily question: can I sell? | about 10/100 | Phase 4 is unrelated to sell timing. The earlier owner brief lacked core evidence, thesis matching still needs manual semantic review, and mature monitoring/fees/settlement/action authorization remain absent. |
| 5. Whole-portfolio diagnosis | about 55/100 | Phase 3 remains the useful component for visible concentration and duplication, but its owner evidence had partial relationships and insufficient holdings. Phase 4 consumes that limited projection for candidates; it does not complete whole-portfolio diagnosis. |

Overall current beginner usefulness remains roughly one third, with large
variation by question. That estimate gives credit for safe factual organization
and explicit abstention. It does not confuse those qualities with the ability to
reach a financially mature action. Neither the overall result nor any individual
risk-increasing workflow approaches 90%.

## Technical Completion Versus Financial Usability

The Phase 4 technical contract is substantially implemented and verified:
bounded input, deterministic policy, one comparison composition path, partial
failure isolation, public projection, privacy checks, stable non-authorization,
and structured `insufficient_data` all have test evidence.

Financial usability is a separate gate and did not pass on the supplied owner
evidence. No candidate had sufficient authenticated evidence, no personal gate
was usable, and no conditional shortlist existed. Passing 2877 tests cannot
convert missing identity, classification, benchmark, holdings, suitability, or
allocation evidence into a financial conclusion.

## Recommended Next Priority

The best next priority is not a score, candidate-discovery crawler, exact amount,
or additional shortlist UI. It is a bounded evidence-readiness and refresh path
for a user-named fund pair that closes the highest-value existing gaps before
comparison: authenticated identity and index identity, current D1
classification, current benchmark, usable quarterly holdings, and reliable
amount-free Phase B/C status. It should show a preflight checklist, refresh only
already-supported sources, and end with explicit manual-supplementation requests
when evidence remains unavailable.

Success for that next phase should be measured on an explicitly chosen real
owner pair: at least `relative_tradeoffs_only` with usable dated comparison
dimensions and transparent coverage, not merely a synthetic
`conditional_shortlist`. Market-direction and daily-sell workflows should remain
separate workstreams; no candidate shortlist can repair those missing engines.
