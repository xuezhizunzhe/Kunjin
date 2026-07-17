# KunJin Phase 0 Independent Financial And Product Review

Date: 2026-07-17

Scope: Phase 0 bounded disclosure execution, source health, action routing,
amount-free public projection, process cleanup, the complete local test suite,
and a fresh real-network acceptance for public fund code `000001`. Two fresh
read-only reviewers independently inspected the financial boundaries and the
product/reliability boundaries. Project prompts and Skill claims were not
accepted as evidence without code, test, or live-output support.

Decision: **Phase 0 passes its limited infrastructure scope. There are no open
P0 or P1 findings. The current beginner fund-workflow score is 58/100, not 90%.
Phase 0 infrastructure reliability is separately assessed at 85/100.**

The score increase from the pre-Phase-0 baseline of 54/100 is deliberately
small. Phase 0 makes fact research bounded and independent from suitability
blocks and prevents immature purchase output. It does not implement news
intelligence, D2 portfolio diagnosis, D3 candidate selection, or Phase E
holding and exit monitoring.

## Live Acceptance

The accepted evidence directory is:

```text
/private/tmp/kunjin-phase0-live-task12-000001-b258aac-r3
```

Observed results:

- `status=passed` under a 90-second global deadline;
- pre-publication elapsed time: 4.51 seconds;
- bounded profile synchronization: 3.032 seconds;
- 4 recorded source attempts and 163 obtained public records;
- `partial=true`, with `ambiguous_fee_rule` retained rather than promoted to
  success;
- fact research remained available;
- buy/add remained `experimental_shadow`, non-actionable, and
  `exact_amount_available=false`;
- all six published JSON files parsed and used the strict public projection;
- no personal amount, token, local path, raw response body, or Docker work was
  found in the published output.

The first two live attempts failed closed. Systematic diagnosis established a
test-fixture/schema mismatch: business report sections use plural
`announcements`, while freshness sections use the canonical document-kind key
`announcement`. The offline fake CLI had incorrectly used the plural form, so
the old acceptance allowlist rejected real output after all commands succeeded.
The fix replaces the derived allowlist with the exact seven production
freshness keys. This is a strict shape correction, not a relaxation of unknown
fields. The updated real-shape regression failed before the fix and passed
after it.

## Findings

### P2 - Live action coverage is narrower than the routing contract

The real acceptance covers `fact_research` and `buy_or_add`. It does not run
real CLI acceptance for `continue_holding`, `reduce_to_cash`, `full_exit`, or
both legs of `switch_funds`.

Code and tests show the intended boundaries: blocked holding is at least
`no_add`; reduction and exit research remain available but non-executable;
switch reduction and purchase are evaluated independently; purchase cannot
inherit permission from reduction. These are verified locally, but the missing
live cases remain a release-evidence gap. Phase 1 acceptance must add them.

### P2 - Record acquisition is not semantic fact verification

The live run proves that 163 public records were acquired and passed bounded
storage/projection checks. It does not independently verify that every field is
financially complete or correct. The fee section was unavailable because of
`ambiguous_fee_rule`, and every current request-field resolution remained
`partial`.

Consequently, the acceptance evidence supports dated partial research. It does
not support claims that fund information is complete, that all facts are tier-1
verified, or that the fund is suitable to buy.

### P2 - `action_maturity=mature` needs beginner-facing qualification

For facts and some safety states, `mature` means the routing rule is established.
It does not mean the financial evidence is complete, a holding is approved, or
a mature buy/sell timing signal exists. Consumers must present `research_only`,
`minimum_state`, blocking codes, evidence status, and missing fields before the
maturity label. Phase 1 should avoid displaying an unqualified maturity label
to a beginner.

### P2 - The single `partial` summary mixes two different scopes

The acceptance summary is partial when either the profile sync is partial or
any registered request field is not usable. Source status includes NAV, market,
holdings, and transaction fields that the profile sync did not attempt. This is
safe but noisy and can make a successful scoped sync look broadly incomplete.

Phase 1 should separate at least `sync_partial` from
`request_evidence_partial`, and identify the fields actually requested by the
current workflow.

### P2 - Deep, cooldown, and beginner-readable fact output lack live proof

The real acceptance exercises Rapid only. The 480-second Deep boundary and
cooldown behavior are covered by code and automated fault tests, not a live
network run. Fault coverage includes timeout, slow and late output, ignored
SIGTERM, child and grandchild cleanup, oversized IPC, and cooldown state.

The public acceptance projection also reports section counts and status rather
than a beginner-readable list of facts with dates and sources. Producing that
usable one-fund answer is explicitly a Phase 1 responsibility.

## Verified Financial Boundaries

- Phase B and Phase C do not suppress independently supported fact research.
- A blocked Phase B holding review is not called an approved hold and is at
  least `no_add`.
- Reduction and full-exit research remain available under a Phase B block, but
  the block itself is not treated as a sell signal.
- Buy/add requires current Phase B, Phase C, D1, D2, D3, and post-trade gates.
- Switch is split into reduction and purchase legs; the purchase leg retains
  the full risk-increasing gate.
- No route exposes a proposed exact amount in Phase 0.
- No mature buy, add, reduction, exit, switch-buy, or market-timing conclusion
  is established by the live evidence.

## Verification Evidence

- Complete test suite: `1961 passed`.
- Ruff: `All checks passed!` for `src` and `tests`.
- Bash syntax: passed for `scripts/run_phase0_acceptance.sh`.
- `git diff --check`: passed.
- Real amount-free acceptance: passed in 4.51 seconds.
- Focused financial routing review: 31 route tests and 2 blocked/switch CLI
  integration tests passed.
- Focused product fault review: 11 timeout, cleanup, IPC, and cooldown tests
  passed.

## Objective Score

| Area | Weight | Score | Current evidence |
| --- | ---: | ---: | --- |
| Personal suitability and risk budget | 20 | 14 | Phase B/C deterministic and guarded, but still research-only |
| Public fund facts and evidence | 20 | 14 | Bounded useful partial facts; tier/field completeness remains limited |
| Portfolio structure and overlap | 15 | 7 | Existing basic metrics and disclosed overlap; D2 decision controls absent |
| Candidate selection and purchase checks | 15 | 5 | Purchase is safely blocked; D3 comparison and transaction checks absent |
| News and current market interpretation | 10 | 4 | Legacy market facts exist; audited news intelligence is absent |
| Holding, exit, and monitoring workflow | 15 | 9 | Action separation exists; Phase E rules and mature timing are absent |
| Source reliability and bounded execution | 5 | 5 | Strong Rapid deadline, failure, cleanup, privacy, and partial contracts |
| **Total** | **100** | **58** | **58% verified workflow coverage** |

The two independent reviewers assessed the complete beginner workflow at
58/100 and 61/100. This audit adopts **58/100** as the conservative phase score
and does not average engineering reliability into financial usefulness. The
exact number is a coverage judgment, not a prediction accuracy, return forecast,
or guarantee.

## 90% Conclusion And Next Gate

KunJin does not currently provide more than 90% of the reasonably automatable
help a beginner needs to buy and manage funds. It is now more reliable at
bounded public-fact research and at refusing unsafe purchase output, but it
still cannot complete the user's intended chain from current news and market
context through portfolio diagnosis, candidate choice, purchase checks,
ongoing monitoring, and conditional exit review.

Phase 1 may start only after owner confirmation. Its acceptance must produce a
beginner-readable one-fund answer with dated sources and limitations, exercise
all action routes, and preserve every Phase 0 fail-closed and privacy boundary.
