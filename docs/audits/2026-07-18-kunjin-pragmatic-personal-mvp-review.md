# KunJin Pragmatic Personal MVP Independent Review

## Decision

Phase 2 implementation and safety contracts are complete, but live financial
usability acceptance is not complete. The delivered system must not be
described as providing 90% of the help a beginner needs to buy, hold, or sell a
fund.

The practical distinction is:

- evidence integrity, privacy, bounded execution, and fail-closed behavior are
  strong enough to retain;
- current market direction, named-fund relevance, and daily holding or exit
  evidence are not strong enough to authorize an action conclusion; and
- the current real-world usefulness is approximately 4/10, not the higher
  capability possible when every public source and owner evidence stage works.

## Verification Evidence

Fresh repository verification after the closure fixes produced:

- pragmatic local acceptance: 229 passed, with no process residue;
- fault acceptance: 18 passed and 34 deselected, with no process residue;
- schema, brief, and smoke regression set: 300 passed;
- Ruff: no findings;
- Bash syntax and `git diff --check`: passed; and
- repository and installed KunJin Skill: byte-for-byte equal and valid.

The closure changes are recorded in commits `46bb32e` and `7c37340`.

## Real Live Evidence

The latest bounded public live run did not pass the complete live gate:

- recent news published 11 items;
- the government source succeeded;
- STCN returned useful items but remained partial because at least one detail
  failed deterministic parsing;
- Eastmoney ended in a retryable `transient_network_failure` with zero market
  dimensions; and
- the run exited 1 at `live_market_requires_eastmoney_evidence`, before the
  named-fund workflow could be accepted.

This supports a dated partial news answer. It does not support a current market
direction or prove that current public intelligence is related to a named fund.
The final gate now also requires an authenticated fund-relevance link, so local
fund context alone cannot pass as current relevance.

## Real Owner Evidence

The latest bounded owner run preserved the important privacy and degradation
contracts:

- it copied the real database through SQLite read-only mode into a private
  throwaway database;
- it did not mutate the real database;
- it did not expose a held-fund code, amount, profile value, cost, profit,
  shares, or portfolio weight; and
- it left no worker process behind.

The run nevertheless exited 1 because all six core brief stages were omitted:

- `identity_profile`;
- `personal_position_observation`;
- `formal_nav`;
- `manager_fee_profile`;
- `holdings_industries`; and
- `official_announcements`.

It also reported `historical_brief_comparison_unavailable`. That code proves
neither that the conclusion changed nor that it remained unchanged. The current
acceptance script is stricter: any omitted core stage produces
`owner_brief_core_sources_incomplete`, reports only whether the core evidence
set is complete, and explicitly does not claim that financial action usability
was assessed.

## Independent Financial Assessment

Scores reflect the latest degraded real run rather than an ideal healthy-source
scenario:

| Beginner workflow | Score | Evidence-based assessment |
| --- | ---: | --- |
| Recent news | 6/10 | Useful dated items exist, but STCN coverage is partial and source breadth is narrow. |
| Market direction or what to buy | 2/10 | The required live market batch failed and unsupported dimensions remain explicit gaps. |
| Named candidate fund | 3/10 | Public context is available in principle, but current relevance and purchase gates were not accepted. |
| Daily hold or sell review | 1/10 | The owner brief lacked every core stage and historical comparison was unavailable. |
| Portfolio duplication and risk | 6/10 | Existing deterministic weights, concentration, manager/benchmark relations, and disclosed top-ten overlap remain useful but coverage-limited. |

Overall real-world usefulness is approximately 4/10. A broader percentage such
as 45%-55% describes partial informational assistance, not a verified share of
all decisions a beginner must make. Neither number supports a 90% claim.

## Closed Findings

The final review found no unresolved P0 or P1 after these corrections:

- one malformed STCN detail no longer discards later valid articles;
- current STCN shapes and the reviewed historical government path are parsed
  without weakening canonical URL checks;
- remote disconnects become structured retryable network failures;
- news retrieval time is bound to the aggregate attempt finish interval;
- a known empty early-v19 intelligence namespace can be rebuilt safely, while
  non-empty or unknown drift is rejected;
- unreadable historical brief state is disclosed rather than used for a false
  changed/unchanged conclusion;
- live failure retains completed workflow summaries;
- named-fund acceptance requires actual relevance, not generic news or local
  identity fields alone; and
- incomplete owner core evidence cannot be labelled financially usable.

## Next Phase Boundary

Do not spend the next critical path on repeated public-source polling or on a
new adapter for each held fund. Phase 3 should deliver the smallest useful D2
increment by reusing existing authenticated disclosure and portfolio modules:

- coverage-aware portfolio classification;
- explicit manager, benchmark/index, theme, and disclosed-security duplication;
- known versus unknown exposure, with missing holdings never treated as zero;
- candidate marginal duplication for a user-supplied fund, without selecting an
  amount; and
- conditional diagnosis only, with complete correlation, stress testing, and
  full industry look-through remaining deferred unless already supported by
  authenticated existing data.

Phase 3 does not cure the failed Phase 2 live market or owner evidence gates.
It adds useful deterministic portfolio diagnosis while those external and
source-coverage limitations remain visible.
