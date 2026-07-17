# KunJin Usable Evidence-Driven Personal Fund Assistant Design

Date: 2026-07-16
Status: proposed for owner review

## 1. Decision Summary

KunJin will evolve from a strict evidence gate that frequently refuses useful
questions into a local, evidence-driven personal fund decision assistant. The
new design preserves financial safety and provenance while making ordinary
research, current-holding analysis, portfolio diagnosis, and bounded action
guidance available within a predictable time budget.

The product supports conditional action guidance such as:

- continue holding;
- do not add;
- keep on watch;
- enter a reduce-or-exit review;
- consider as a candidate; and
- abstain because material evidence is missing.

It does not promise returns, claim deterministic market timing, operate a
broker, Alipay, or Yangjibao account, or automatically place an order.

The redesign has six capabilities:

1. per-subquestion and per-action routing with bounded requests;
2. news and market intelligence with source independence and corrections;
3. D2 portfolio exposure, duplication, and concentration analysis;
4. D3 constrained candidate, share-class, fee, and transaction validation;
5. Phase E thesis, monitoring, reduce/exit review, and rebalancing; and
6. a separate learning-account policy that cannot bypass financial safety.

Official-document parsing remains an optional decision-depth capability. It is
not a prerequisite for ordinary research and cannot trigger adapter development
or Docker image construction inside an interactive query.

## 2. Problem Statement

The current implementation is conservative but poorly matched to the owner's
primary workflow:

- a request to discuss an existing holding may be stopped by Phase B before
  fund, market, or portfolio facts are explained;
- buy, hold, reduce, sell, and rebalance are routed through the same gate;
- a non-verified D1 result stops directional discussion even when useful
  lower-confidence research is available;
- official manager-domain coverage is narrow, so a common new fund may require
  engineering work before the strict path can classify it;
- the current market result covers recent strength and breadth only;
- news and causal attribution are not yet persisted;
- D2, D3, and Phase E are not complete; and
- the user cannot obtain a timely answer to a normal learning or holding
  question without encountering infrastructure work.

The last independent review scores the implemented product at 54/100 for the
beginner fund-decision workflow. This design does not change that score. Only
implemented, verified, live behavior may change it.

## 3. Product Goal

KunJin should help a beginner follow this complete chain:

```text
financial readiness
-> risk capacity and strategic allocation
-> current portfolio gaps and duplication
-> suitable product category
-> constrained candidate comparison
-> share class, channel, fees, and amount
-> monitoring, exit review, and rebalancing
```

The user should be able to:

- ask for recent sector, policy, company, and fund news with dated sources;
- ask which market directions merit research and receive conditional analysis;
- submit a fund code and receive a portfolio- and profile-aware assessment;
- ask every day whether a held fund has new evidence that changes the prior
  conclusion;
- inspect all held funds for theme, manager, disclosed-holdings, industry,
  correlation, and concentration risks; and
- receive a clear request for a URL, PDF, screenshot, or field when Codex cannot
  access critical public evidence.

The intended information advantage is faster organization, cross-checking, and
portfolio relevance of public information. It is not private information and
does not imply persistent excess returns.

## 4. Non-Goals

- Guaranteed prediction accuracy or profit.
- A universal opaque fund score.
- A claim that platform rank or recent return identifies the best fund.
- High-frequency trading or daily churn.
- Automatic order placement.
- Bypassing authentication, paywalls, robots controls, or site terms.
- Storing unlimited copyrighted article text.
- Treating quarterly holdings as real-time holdings.
- Treating a learning account as an exception to debt, reserve, or cash-flow
  safety.
- Building a new source adapter during an interactive request.
- Making Docker, MySQL, Redis, or a continuously running server mandatory.

## 5. Governing Principles

### 5.1 Strategic allocation comes first

Long-term strategic allocation, personal goals, and loss capacity control the
portfolio. News and current market form may affect entry pacing or a separately
bounded tactical sleeve; they cannot replace strategic allocation.

- Sector funds are not mandatory.
- Technology, consumption, and healthcare are not inherently suitable for a
  beginner.
- Broad index, active management, sector exposure, fixed income, and portfolio
  role are separate classification dimensions, not four mutually exclusive
  buckets that must each receive one fund.
- Every candidate comparison includes `do_nothing` and a simpler low-cost broad
  index baseline when a valid broad-index comparator exists.
- Tactical, sector, and real learning-account new money each require a
  versioned cap. A market/news conclusion cannot create or raise that cap.
- A tactical or sector cap is derived from the owner's current strategic
  allocation, loss capacity, horizon, liquidity needs, and an explicit stress-
  loss assumption. The derivation must be independently financially reviewed
  and then separately approved by the owner as a policy change. The module that
  produces a market or sector opinion cannot approve its own cap.
- Until that derivation, independent review, and owner approval exist, the
  applicable tactical or sector new-money cap is zero. A query result cannot
  improvise a percentage from current market strength.
- Every new-money cap is zero while Phase B is `blocked`.

### 5.2 Facts, calculations, inference, and opinions remain separate

Every material statement uses one of:

- `verified_fact`;
- `deterministic_calculation`;
- `reported_claim`;
- `market_opinion`;
- `reasoned_inference`; or
- `insufficient_data`.

### 5.3 Missing is not zero

Missing holdings, industries, issuers, ratings, themes, or news are never
converted into zero exposure or absence of risk.

### 5.4 Availability cannot weaken evidence requirements

The 90-second and eight-minute budgets are service budgets, not evidence
quality standards. On expiry, KunJin returns partial evidence, abstains from an
unsupported action, and produces a supplementation card. It never lowers a
decision requirement merely to finish on time.

### 5.5 Conditional action is interpretation, not execution

Action guidance always includes supporting evidence, opposing evidence,
confidence, invalidation conditions, missing data, and a review time. No action
is executed automatically.

## 6. Architecture

```text
Codex intent decomposition
-> ActionRouter
-> RequestBudget
-> cached normalized data
-> bounded source adapters
-> EvidenceAssembler
-> deterministic analytics
-> DecisionEvidenceMatrix
-> conditional interpretation
-> immutable decision snapshot
```

Codex explains intent and structured results. The KunJin CLI and services own
retrieval, normalization, freshness, deterministic calculations, constraints,
and stable machine-readable status.

SQLite remains the local system of record. Network adapters execute with finite
concurrency and publish through a single bounded write path.

## 7. Request Budget

`RequestBudget` is the first implementation prerequisite. A prompt statement
about time limits is insufficient.

Each request carries:

- a monotonic absolute deadline;
- remaining wall-clock budget;
- mode: `rapid` or `deep`;
- a bounded adapter-concurrency limit;
- per-source attempt count;
- cancellation state;
- cache policy; and
- partial-result policy.

Default policy:

| Mode | Total budget | Typical use |
| --- | ---: | --- |
| `rapid` | 90 seconds | news, one fund, current holding, portfolio snapshot |
| `deep` | 8 minutes | 2-5 final candidates, fees, transaction checks, official critical facts |

Operational rules:

- cache is inspected before network work;
- one source receives at most one retry only for a transient network failure
  and only when enough request budget remains;
- HTTP 4xx, paywalls, authentication shells, identity conflicts, deterministic
  validation failures, and parse failures are not retried;
- default adapter concurrency is bounded and configurable;
- every blocking network timeout is at most the smaller of its adapter limit
  and the remaining request budget;
- in the MVP, potentially non-cooperative network adapter work runs in bounded
  worker subprocesses, not in in-process executor threads. The parent process
  owns the monotonic request deadline and is the only process allowed to commit
  results to SQLite;
- each worker returns a bounded structured result over controlled IPC. The
  parent revalidates request ID, source/field identity, schema version, maximum
  byte size, and monotonic completion time before persistence. At deadline or
  cancellation, the parent stops new work, terminates the worker process group,
  waits a bounded cleanup grace period, force-kills if needed, reaps it, and
  discards every late result before any database write;
- the request budget reserves time for worker termination and cleanup, so the
  advertised terminal deadline includes cleanup rather than starting cleanup
  after the SLA;
- cancellation propagates cooperatively at pagination, candidate-loop,
  conversion, persistence, and downstream boundaries;
- an already running blocking call may finish after the caller stops waiting,
  but a result that completes after expiry or cancellation cannot publish as a
  current request result;
- a failed source enters a configurable cooldown, initially 30 minutes;
- an explicit owner-requested deep/force check may bypass cooldown once, while
  remaining subject to the deep deadline and retry classification;
- no Docker build, image pull, package installation, or source-adapter creation
  occurs in a request;
- budget expiry preserves successful sections and cancels remaining work; and
- the response identifies omitted work and whether it affected actionability.

The MVP is synchronous. A rapid request returns a terminal complete or partial
result within 90 seconds. It does not continue silently in the background.
Unfinished work requires a new explicit `deep` request. A future background
mode is out of scope until it has a persisted task ID, visible state, absolute
deadline, status query, cancellation command, and a rule that cancelled or
expired tasks cannot publish current results.

Schedule times, source timeouts, concurrency, retry counts, cooldowns, and cache
thresholds are configuration, not financial policy.

## 8. Per-Subquestion And Per-Action Routing

One sentence may contain research, sale, conversion, and new-money actions.
KunJin decomposes it before applying gates.

### 8.1 Information actions

Examples: news, fund identity, history, fees, holdings, market context, and
portfolio diagnosis.

- Phase B and Phase C are not gates.
- Missing evidence lowers scope or confidence.
- A blocked transaction component does not suppress independently answerable
  facts.

### 8.2 Risk-maintaining actions

Continuing to hold maintains market exposure and is not risk-free.

- Phase B status is disclosed as relevant context.
- A reserve shortfall requires an explicit conflict between cash safety and
  continued market exposure.
- While a hard Phase B block exists, the user-visible result is at least
  `no_add`; it cannot present an unqualified `hold` by itself.
- The system may still analyze the holding and determine whether new evidence
  changes the prior thesis.

### 8.3 Risk-reducing actions

Selling to cash or reducing a position normally lowers market exposure.

- Phase B `blocked` does not prohibit analysis.
- Exact reduction requires position, target-band, fee, settlement, and minimum
  balance evidence.
- A reduce-or-exit status is not an automatic order.

### 8.4 Risk-increasing actions

Buying, adding, raising equity exposure, or redirecting protected cash requires
current Phase B, Phase C, D2, D3, and post-trade simulation.

### 8.5 Switches

Selling one fund and buying another is two actions:

1. the sale or reduction leg; and
2. a new-money-equivalent purchase leg.

The second leg cannot inherit the first leg's risk-reducing classification.

When action intent is ambiguous, the transaction component uses the stricter
route while information components continue normally.

## 9. Workflow Level Versus Conclusion Evidence

`rapid_evidence` and `decision_evidence` describe work intensity only. They do
not assert that a conclusion is reliable.

Every conclusion separately records:

- source tier;
- publisher and canonical source identity;
- publication, market, report, and retrieval times;
- independent source count;
- original-source lineage;
- completeness and coverage;
- freshness;
- conflicts;
- whether the conclusion is inferred; and
- the critical fields that are missing.

Ten articles quoting one announcement count as one underlying source. URL count
does not establish independence.

User-provided URLs, PDFs, and screenshots enter as `user_provided`. They upgrade
only after publisher, document identity, date, and integrity validation.

## 10. Decision Evidence Matrix

### 10.1 Action requirements

| Action | Required evidence | Missing-field behavior |
| --- | --- | --- |
| Fact research | identifiable source, date, scope | answer supported parts |
| Continue holding | position, product status, latest formal NAV, material changes, portfolio effect, thesis or `thesis_missing` | lower confidence or abstain from action |
| Reduce to cash | position, target/risk reason, redemption conditions, settlement, minimum remainder | no exact amount if transaction facts are missing |
| Switch funds | reduction evidence plus all purchase evidence for destination | evaluate legs separately |
| Buy or add | fresh B/C, category fit, D2, D3, transaction availability, critical product facts, post-trade simulation | stop direction/amount, preserve research |
| Full exit | thesis/product invalidation or an owner-driven reason such as cash need, learning completion, simplification, shorter horizon, or reduced risk capacity; plus portfolio effect, fees, settlement, and destination/use of proceeds | enter review, do not equate trigger with immediate sale |

### 10.2 Product-specific critical fields

Broad index:

- exact fund and share class;
- tracked index and methodology identity;
- fees;
- tracking difference/error when available;
- size and operational status;
- index concentration and portfolio overlap; and
- current transaction availability.

Sector or theme:

- exact theme/index identity;
- exposure evidence and disclosure period;
- concentration;
- portfolio duplication;
- market trend, valuation, fundamentals, flow, catalysts, and crowding coverage;
- fees and transaction availability; and
- tactical/sector cap after the proposed action.

Active equity or mixed:

- current manager/team and exact tenure;
- benchmark;
- manager-period aligned performance and drawdown;
- style and concentration evidence;
- portfolio overlap;
- size and fee evidence; and
- current transaction availability.

Bond or fixed income:

- product family;
- credit quality;
- duration;
- leverage;
- issuer concentration;
- convertible, equity, derivative, foreign, and liquidity exposure;
- drawdown; and
- redemption conditions.

The policy for each field defines primary source, acceptable fallback, maximum
age, conflict behavior, and whether omission is action-blocking or confidence-
reducing. Those policies are immutable, versioned records, not scattered code
constants.

### 10.3 MVP EvidencePolicy V1

The first vertical slice uses the following executable minimum policy. A newer
policy requires a new version and independent review.

| Field | Decision evidence | Maximum age / freshness | Missing or conflict behavior |
| --- | --- | --- | --- |
| Phase B/C | authenticated current result bound to current profile/policy | no older than 24 hours and invalidated by any bound input change | blocks buy/add/switch-buy/amount; does not block facts or reduce-to-cash analysis |
| Personal position for exact action | successful same-request portfolio sync plus locally confirmed pending transactions | same request | blocks exact buy/reduce/exit amount; ratios may use labeled last observation |
| Identity and active status | one Tier 1 source, or two independently sourced structured Tier 2 records with matching code/name/share class | seven days; immediate invalidation on a newer status announcement | identity/status conflict blocks every product-specific action |
| Current manager/team | one Tier 1 source, or two independent structured Tier 2 records | seven days; immediate invalidation on a manager announcement | one Tier 2 source supports rapid research only; conflict blocks manager-dependent comparison |
| Fees and A/C relationship | Tier 1 schedule, or current verified channel evidence plus one matching structured source | Tier 1 remains valid through its stated effective period, subject to newer-announcement checks; channel discounts/limits expire after seven days or sooner when the channel states it | blocks exact fee, A/C choice, and exact amount; rapid overview remains labeled |
| Transaction availability, limits, cutoff | same-day official/channel record or validated private channel screenshot | two hours for `current/today`; same trading day otherwise | blocks executable buy/redeem conclusion and exact amount |
| Formal NAV | latest expected published formal NAV; cumulative/adjusted series passes continuity checks | expected day is derived from the applicable trading calendar, fund class, cross-border/QDII holiday rules, and normal publication window | stale data supports dated history only; no current timing conclusion |
| Adjusted-return correlation | validated cumulative-NAV or total-return series with aligned dates and policy minimum sample | common end date is latest expected comparable NAV day | dividend/split/discontinuity ambiguity returns `insufficient_data`; never fall back to NAV-level correlation |
| Holdings and industries | latest published statutory period with report/publication date and disclosure scope | current until a newer report is due under the disclosure calendar | supports observed overlap only; missing/late data preserves unknown exposure and blocks reassuring diversification claims |
| Fund/manager/product announcement | validated official item | query window plus correction/retraction check | a missing feed lowers coverage; a known unresolved official conflict blocks affected action |
| News/media context | official original source or genuinely independent lineage; media claim remains attributed | inside resolved query window; cache no older than two hours for `current` | never independently authorizes an action; conflict lowers confidence or causes abstention |
| Target point and bands | current versioned owner-approved policy, distinct from feasible ceilings | invalidated by profile, goal, or policy change | blocks exact buy/reduce/rebalance amount |

For MVP actions:

- fact research may use one identified Tier 2 source when its tier and date are
  visible;
- `hold`, `watch`, and reduce/exit interpretation remain shadow unless an
  independently promoted rule applies;
- a deterministic Phase B block may maturely produce `no_add`;
- an official liquidation/termination notice may maturely trigger an exit
  review, not an immediate sale; and
- buy/add/switch-buy and exact amounts require every action-blocking row above
  that applies to the product and transaction.

Source independence is based on original-source lineage, not domains or URL
count. User evidence remains `user_provided` until validated under the same
identity and date rules.

## 11. News And Market Intelligence

### 11.1 Acquisition model

KunJin uses scheduled cache plus bounded on-demand verification.

The MVP starts with a finite allowlist:

- regulators and exchanges;
- fund and index announcements already supported by an audited adapter; and
- a small set of stable established financial-media sources.

It does not begin by scraping every website.

Phase 1 limits actionable news to official fund/product announcements and
manually auditable citations. It records a minimal `original_source_id`,
`quoted_source_id`, canonical URL, and exact-content fingerprint so a direct
reprint is not counted as independent confirmation. Semantic event clustering,
cross-media independence, correction propagation, and broad entity mapping
remain Phase 2 capabilities. Phase 1 must not claim independent media
confirmation that it cannot yet establish.

### 11.2 News records and event lineage

Logical entities include:

- `news_items`;
- `news_corrections`;
- `news_events`;
- `news_event_links`;
- `news_source_lineage`;
- `market_entities`; and
- `entity_aliases`.

KunJin stores publisher, URL, publication time, retrieval time, bounded
excerpt/summary, content fingerprint, source tier, entities, and citation
lineage. It does not store unlimited full article text.

Retractions, corrections, and later official clarification invalidate dependent
conclusions and trigger a review snapshot.

### 11.3 Time windows

- `today`: local midnight to query time;
- `recent`: previous 72 hours;
- `near_term`: previous seven calendar days; and
- explicit user dates: exact interval.

The answer always prints the resolved interval.

### 11.4 Event processing

```text
item validation
-> publisher/time verification
-> entity linking
-> original-source lineage
-> event clustering
-> correction/retraction check
-> holding/sector/fund relevance
```

Headline or URL similarity alone does not prove independent confirmation.

### 11.5 Market dimensions

Market and sector analysis keeps six dimensions separate:

- trend and breadth;
- valuation;
- fundamentals and earnings;
- volume and persistent flow;
- policy/company/industry catalysts; and
- crowding and acceleration risk.

The current implementation has reliable recent strength and breadth only.
Unsupported dimensions remain `insufficient_data`; they are not inferred from
news sentiment.

Initial market-state labels run in shadow mode until their rules and behavior
are validated.

### 11.6 Source health and maintenance

Source availability is visible operational state, not a hidden adapter detail.
For every registered source or adapter, KunJin exposes:

- source and adapter identity, supported fields, and evidence tier;
- last successful retrieval and the data date it produced;
- last failed attempt, normalized reason, and consecutive-failure count;
- current cooldown and its expiry;
- most recent schema/fixture validation;
- configured alternative sources for the same fields; and
- the exact manual-supplementation path when no usable alternative exists.

Health uses two schema levels:

```text
source_field_state =
  not_checked | healthy | degraded | cooldown | unavailable | unsupported

request_field_resolution =
  usable | partial | manual_supplement_required
```

State evaluation is deterministic. An individual source/field pair with no
attempt is `not_checked`. Explicit permanent absence or an audited unsupported
contract is `unsupported`. A transient failure inside its cooldown is
`cooldown`. Current validated evidence is `healthy`; stale or partial evidence
that remains usable for a reduced scope is `degraded`; and a current failure
with no usable evidence, including an expired cooldown with no subsequent
success, is `unavailable`. Permanent 404/410 or an explicit statement that a
field is not supplied is not converted into recurring cooldown retries.

`request_field_resolution` is computed across every configured acceptable
alternative. It is `usable` when the field's action requirement is met,
`partial` when some dated fact remains usable but the requested action standard
is not met, and `manual_supplement_required` only when no acceptable source can
satisfy a critical field. A source in cooldown is not repeatedly polled and is
not misreported as proof that an event or fact does not exist. Health never
promotes the evidence tier of a fallback. A force bypass records owner identity,
reason, time, affected source/field, deadline, and result; ordinary queries
cannot indirectly invoke or repeat it.

## 12. D2 Portfolio Diagnosis

### 12.1 Exposure dimensions

The taxonomy separates:

- asset class;
- management style;
- index and theme;
- factor/style exposure;
- portfolio role;
- manager/team;
- industry;
- disclosed security; and
- unknown exposure.

Broad index, active, theme, and defensive are not stored as one overloaded
dimension.

### 12.2 MVP deterministic checks

The first D2 slice implements:

- A/C and master-fund relationships;
- same current manager/team;
- same tracked index or explicit benchmark/theme;
- latest disclosed top-ten overlap;
- adjusted total-return correlation on aligned dates; and
- coverage and unknown-exposure reporting.

Theme labels require an explicit index/benchmark source or remain a clearly
marked third-party provisional label. The empty production industry taxonomy is
not silently populated from free text.

### 12.3 Holdings overlap

For a shared security:

```text
shared_weight = min(fund_a_weight, fund_b_weight)
```

When only top-ten data exists, the metric remains
`top10_disclosed_overlap`. Reports include both report periods, publication
dates, disclosure scopes, covered weights, and unknown weights.

### 12.4 Correlation

Correlation uses adjusted total-return series, not NAV levels. The policy
defines:

- minimum sample count;
- aligned market dates;
- frequency;
- currency handling;
- missing observations;
- suspensions; and
- 60-, 120-, and 250-trading-day windows.

Correlation and holdings overlap remain separate facts.

The implementation prefers continuity-validated cumulative NAV or another
validated adjusted total-return series. If dividends, splits, discontinuities,
currency conversion, suspensions, or missing observations cannot be handled
reliably, correlation is `insufficient_data`; unit-NAV-level correlation is not
used as a fallback.

### 12.5 Look-through exposure

```text
lookthrough_weight = personal_fund_weight * disclosed_fund_weight
```

Reports separately display:

- covered personal portfolio weight;
- disclosed internal fund weight;
- known exposure; and
- unknown exposure.

Low coverage cannot produce a reassuring `low duplication` conclusion. It
produces `insufficient_data` or a qualified statement about observed data only.

EvidencePolicy V1 uses these minimum D2 gates for a mature risk-increasing
conclusion or exact amount:

- the candidate has complete applicable product-level identity for asset class,
  portfolio role, manager/team, and exact index/theme;
- at least 90% of the current non-cash fund portfolio by market value has
  current product-level role, manager/team, and index/theme classification;
- a sector/theme candidate has verified constituents, holdings, or industry
  mapping covering at least 80% of candidate assets by disclosed weight;
- a broad-index candidate has verified index constituents or holdings covering
  at least 90% of candidate assets by weight; and
- transaction-after look-through coverage is at least 70% for every binding
  industry/security limit, unless allocating all residual unknown exposure to
  that limit still leaves the portfolio within the approved cap.

The machine contract defines:

```text
classification_coverage =
  classified current non-cash fund market value
  / total current non-cash fund market value

candidate_asset_coverage =
  sum of verified disclosed/index constituent asset weights
  / 100% of candidate net assets

transaction_after_lookthrough_coverage =
  sum(transaction-after fund market value * verified internal coverage)
  / total transaction-after non-cash fund market value
```

Cash is excluded from these denominators. Derivatives, leverage, short/negative
weights, and unresolved residual assets are reported separately and cannot
increase apparent coverage. Fund-of-funds exposure is recursively looked
through only where verified. The engine evaluates every applicable limit before
identifying which limits bind; for each limit, all remaining unknown exposure
is also tested as if allocated to that limit. It cannot preselect only limits
that already appear binding.

For an active product without enough disclosed look-through coverage, the last
conservative-unknown test applies; a recent top-ten list alone does not prove
low concentration. Unknown exposure consumes capacity and is never treated as
unoccupied capacity. When a gate is not met, KunJin may still research, compare,
and simulate labeled scenarios, but it blocks mature buy/add direction and
exact amount. These thresholds may change only in a new policy version after
independent financial review and owner approval, never inside a query.
They are conservative governance thresholds, not statistical confidence levels
or claims about prediction accuracy.

### 12.6 Candidate impact

D2 evaluates whether a candidate adds or duplicates manager, index, theme,
industry, security, and return behavior. It does not choose an amount.

When Phase B is blocked, D2 may describe concentration and support risk-
reduction review, but it cannot turn a missing category into a buy instruction.

## 13. D3 Candidate And Transaction Validation

### 13.1 Candidate universe

The MVP compares two to five user-supplied candidates. Later versions may add a
bounded directory of no more than 20, but directory rank and score are never
comparison evidence.

Every comparison includes:

- `do_nothing`; and
- a valid simpler low-cost broad-index baseline when comparable.

For an equity, sector, theme, or active-equity candidate, a broad-index baseline
is valid only when it:

- matches the strategic portfolio role, investable market, currency exposure,
  and material access constraints being compared;
- tracks a published representative broad-market index rather than a sector,
  theme, narrow style, leveraged, inverse, or opaque strategy;
- has an exact active share class with current subscription availability,
  current recurring fees, and sufficient identity/operational evidence; and
- has enough formal NAV history for the metrics used in that comparison.

Phase 4 obtains candidates only from a versioned owner-reviewed baseline
registry containing no more than 20 exact share classes in its initial version,
or from a baseline explicitly supplied by the owner. It does not scan the whole
fund market inside a comparison request. Automated all-market discovery is out
of scope until separately designed and accepted. Each registry entry records
its inclusion/removal reason, evidence date, and next review date. An overdue
entry is excluded until reviewed. Registry membership never replaces same-
request checks of current product identity, operating status, fees, and
transaction availability.

Eligible baselines are selected deterministically: role/market compatibility
first, then index breadth and operational continuity, then lower current
recurring cost, then tracking quality, with a stable fund-code tie-break. A
lower fee cannot compensate for a mismatched market or unusable product. The
selected baseline, exclusion reasons, candidate-registry version, registry
as-of date, and eligible/total product coverage are shown. `Lower cost` means
lower within that disclosed eligible registry, never an unsupported claim of
the lowest fee in the whole market. If none qualifies, KunJin prints
`no_valid_broad_index_baseline`; it does not invent one or force an equity
baseline into a bond comparison.

### 13.2 Comparison

Candidate comparison uses common formal-NAV dates and product-specific metrics.
It preserves:

- explicit candidate-set selection-bias warnings;
- survivor-bias limitations when the supplied set excludes failed or liquidated
  products;
- current manager tenure limitations;
- data coverage;
- portfolio overlap;
- advantages;
- tradeoffs; and
- missing evidence.

The result may identify `preferred_candidate`, `acceptable_alternative`,
`watch_only`, `not_preferred`, or `insufficient_data` within the explicit
candidate set. It never claims a universal best fund.

The MVP does not require a complete historical universe of liquidated peers. It
must not claim that two-to-five user-supplied candidates represent the full
historical peer population.

### 13.3 A/C share classes

A/C siblings share economic holdings identity but retain separate NAV and fee
histories. KunJin compares:

- subscription fee and platform discount;
- management and custody fee;
- sales-service fee;
- redemption schedule;
- intended holding-period scenarios; and
- a transparent break-even holding period when all inputs are available.

No exact choice is made when amount, channel, discount, or holding horizon is
unknown.

### 13.4 Transaction checks

Checks include:

- active/inactive status;
- subscription availability;
- large-purchase and account limits;
- minimum purchase;
- channel availability;
- cutoff time;
- NAV and confirmation rules;
- lock and redemption schedule;
- settlement time; and
- QDII calendar/currency constraints where applicable.

Public sources may not expose current channel discounts and limits reliably.
KunJin requests a screenshot or specific local fields instead of guessing.

### 13.5 Product quality versus entry time

Product quality and current entry context are separate conclusions. A good
product is not automatically suitable today, and a strong sector is not
automatically a good product.

Market-timing labels remain shadow outputs until validated.

## 14. Post-Trade Simulation And Amount

An exact recommended amount requires a current profile, current portfolio,
pending transactions, a versioned target point, D2/D3 constraints, and current
transaction rules.

The target point must have an explicit derivation. A feasible ceiling or range
upper bound is never treated as a target.

Tactical and sector limits are separate policy inputs, not outputs of this
simulation. Each carries its derivation inputs, stress-loss assumption,
independent financial-review record, owner approval, version, and effective
date. A missing or unapproved applicable cap blocks new money into that sleeve.

The cap constrains post-trade total look-through exposure across all linked
accounts, current holdings, and pending transactions. It aggregates explicit
sector/theme funds plus disclosed matching exposure inside broad index, active,
and other theme funds. Unknown exposure consumes capacity under the versioned
conservative rule. The cap cannot be bypassed by splitting purchases across
funds, accounts, labels, or transactions.

The policy declares whether its denominator is all household investable assets
or the complete KunJin-managed portfolio; the MVP uses the latter and labels
that scope. The owner must affirm any unlinked investment accounts. Incomplete
account scope, unresolved material holdings, or valuation dates outside the
policy tolerance blocks exact amount. Valuation-date tolerance and stale-
account behavior are versioned policy fields, not query-time judgment.

Simulation calculates the post-trade state for:

- emergency reserve;
- monthly cash flow;
- goal horizon;
- asset class;
- individual fund;
- theme;
- manager;
- industry;
- known and unknown exposure;
- pending but unsettled subscriptions/redemptions;
- fees;
- minimum purchase; and
- channel limits.

The proposed amount is bounded by the minimum remaining capacity across all
applicable constraints, but the response also displays the resulting post-
trade percentages and every binding constraint.

If a target point or critical input is missing, KunJin abstains from an exact
amount.

## 15. Phase E Monitoring

### 15.1 Holding contract

A confirmed holding thesis records:

- rationale;
- horizon;
- expected mechanism;
- supporting and opposing evidence;
- portfolio role;
- target band;
- invalidation conditions;
- review schedule; and
- input/evidence versions.

An existing holding without a thesis receives a draft. KunJin never invents and
activates a historical rationale.

### 15.2 User-visible MVP states

- `hold` / continue holding;
- `no_add` / hold without adding;
- `watch` / new evidence needs review;
- `reduce_or_exit_review` / enter a reduction or exit review; and
- `abstain` / insufficient evidence.

Every visible state also carries `action_maturity=mature|experimental_shadow`
and a beginner-readable Chinese explanation. Until an action policy is
independently promoted, `hold`, `watch`, and `reduce_or_exit_review` are
`experimental_shadow` and are phrased as rule observations rather than mature
instructions. Audited deterministic rules, such as a current Phase B block
producing `no_add`, may be `mature`.

The response begins with:

1. whether there is new evidence today; and
2. whether the prior conclusion changed.

Repeated questioning alone cannot escalate a state.

### 15.3 Hard triggers

Hard review triggers include:

- manager or product-policy change;
- liquidation or major official announcement;
- user financial-state change after the owner updates or re-confirms the local
  profile;
- confirmed thesis invalidation;
- target-band or concentration breach; and
- material transaction-state change.

Valid owner-driven exit reasons also include a current cash need, completion of
a learning experiment, deliberate portfolio simplification, a shorter horizon,
or reduced risk capacity. A thesis or product failure is not the only valid
reason to exit.

Ordinary market moves and media opinions add watch evidence only.

Even when an exit condition is triggered, KunJin checks transaction state,
fees, settlement, minimum balance, and intended use of proceeds before
interpreting it as an immediate sale.

### 15.4 Rebalancing

Versioned target bands are distinct from Phase C feasible ranges. Rebalancing
prefers:

1. directing new permitted contributions to underweight categories;
2. stopping additions to overweight categories;
3. accounting for fees and holding periods; and
4. reducing positions only when needed to restore bounds or when the thesis is
   invalidated.

Exact reduction amounts require position, band, fee, settlement, and minimum-
balance evidence. Phase B blocked does not prohibit a risk-reducing analysis.

## 16. Signal Policy And Shadow Validation

Market states and monitoring transitions are not mature merely because their
labels exist.

Every policy is versioned and initially runs in `shadow_mode`:

- the system records the state it would have produced;
- user-facing output identifies it as experimental;
- deterministic hard safety findings may still produce `no_add` or a review;
- unvalidated timing signals cannot produce a mature buy/sell recommendation;
- historical snapshots and real cases measure action flips, turnover, false
  alerts, missed hard events, abstention, latency, and maximum adverse outcomes;
- backtest success is not described as future success; and
- `abstain` remains permanently available.

MVP acceptance inspects the actual user-visible state and its maturity marker,
not merely an internal shadow flag.

Promotion out of shadow mode requires an independent financial review and a
versioned acceptance record.

## 17. Learning Account

### 17.1 Explicit enrollment

An account name does not grant learning status. The owner configures locally:

- purpose;
- horizon;
- cumulative budget;
- tolerated total loss;
- fund-count limit;
- end date;
- whether real experiments are permitted; and
- linked account/positions.

Exact values remain local and encrypted where applicable. Policy changes retain
history and cannot reset prior cumulative use.

### 17.2 States

- `analysis_only`: research, simulation, watchlists, and management of existing
  real positions; no new real money.
- `capped_real_experiment`: available only when Phase B is not blocked and all
  learning and product gates pass.
- `closed`: no new money after end, cap, loss, financial deterioration, or
  owner closure.

Effective new learning money is:

```text
min(
  remaining locally declared learning budget,
  Phase B safe monthly ceiling,
  current investable cash flow,
  versioned policy cap,
  remaining loss budget
)
```

The value is zero when Phase B is blocked.

Until a separately reviewed stress-loss policy exists, `remaining loss budget`
uses the most conservative principal-at-risk basis. Unrealized gains do not
replenish it, and repeated small experiments cannot reset or multiply it.

### 17.3 Existing real learning positions

Existing positions remain analyzable and may receive hold, no-add, reduce, or
exit-review guidance. They are shown separately from the formal allocation but
remain included in total household investment risk.

New learning questions use simulation/watchlist entries while Phase B is
blocked.

### 17.4 Experiment contract

Each experiment records a learning objective, hypothesis, horizon, expected
observation, invalidation condition, and reason simulation alone is insufficient.
Repeated funds with the same theme, manager, or holdings require a distinct
learning purpose.

Borrowing, leverage, automatic averaging down, unverified-news trading, and
multi-account cap evasion are prohibited.

## 18. Human Supplementation

When a source is inaccessible or critical evidence is absent, KunJin returns:

```text
missing_item
why_required
suggested_location
accepted_input: URL | PDF | screenshot | field
freshness_requirement
impact_if_missing
supported_without_it
unsupported_without_it
```

It does not repeatedly poll a paywall, bypass login, infer unseen article text
from a headline, or use a search snippet as the original document.

Public fund, fee, and announcement screenshots may use this supplementation
path. Screenshots containing account balances, orders, personal amounts, or
transaction identifiers use the local private import workflow and are never
ordinary chat supplementation.

Exact personal income, debt, reserve, assets, goals, and loss budgets remain in
the local profile editor, not chat supplementation.

## 19. Data Model

The complete target design has the following logical entities, introduced only
when a verified vertical slice needs them:

- `request_runs` and `source_attempts`;
- `evidence_items`, `evidence_lineage`, and `conclusion_evidence`;
- `decision_snapshots` and `decision_changes`;
- `decision_policy_versions` and `evidence_policy_versions`;
- `news_items`, `news_corrections`, `news_events`, and event links;
- `market_entities` and aliases;
- `portfolio_exposure_runs` and coverage records;
- `candidate_comparison_runs`;
- `post_trade_simulations`;
- `holding_theses` and thesis drafts;
- `monitoring_runs` and state transitions;
- `target_band_versions`; and
- `learning_accounts`, budgets, and experiment records.

Phase 0 and Phase 1 add `request_runs`, `source_attempts`, one versioned
structured `decision_snapshots` representation, and the narrow append-only
`source_work_authorizations` table. The authorization table exists only to
reserve one force or retry atomically across independent SQLite connections and
to bind that reservation to the resulting source attempt. It is not a general
task queue, evidence graph, or background-work system. A general evidence
graph, news graph, monitoring history, target bands, and learning-account
migrations remain in their owning later phases. This prevents a broad schema
foundation from preceding user value while avoiding process-local retry and
force state.

Without adding a separate policy table, each `decision_snapshot` embeds the
canonical EvidencePolicy content it used together with policy version and
SHA-256 checksum. Reads verify the checksum, and historical embedded policies
are immutable. A code-default version string without canonical content and
checksum is not an auditable binding.

Every `source_attempt` and `decision_snapshot` also binds the canonical static
source-registry version and checksum. This preserves which alternatives,
supported fields, tiers, and supplementation paths were configured at the time.

Raw metadata, normalized facts, deterministic calculations, inferred
conclusions, and action interpretation remain separate.

SQLite remains appropriate for one owner, local scheduling, bounded concurrent
readers, and a single write queue. MySQL and Redis require demonstrated
contention or scale before adoption.

## 20. Security And Privacy

- Yangjibao remains audited read-only.
- No Alipay, Yangjibao, or broker write exists.
- Tokens remain in Keychain and never enter SQLite, logs, reports, or prompts.
- Credential-bearing workers receive secrets only through a controlled private
  pipe or equivalent anonymous IPC. Tokens never enter command arguments,
  environment variables, temporary files, worker results, or logs.
- Network requests use allowlisted HTTPS and bounded redirects, sizes, and
  timeouts.
- Private, loopback, local, and unsafe addresses remain rejected.
- Web content is untrusted data and cannot alter system instructions.
- Exact profile inputs such as income, debt, reserve, assets, goals, and loss
  budgets remain local and are never exposed in Codex-facing JSON or chat.
- Existing private portfolio values remain in the local database; decision
  output prefers ratios and binding codes.
- An exact proposed transaction amount may be returned only after the owner
  explicitly requests it, enables a per-request and per-action local exact-
  output authorization, and all decision gates pass. Authorization is short-
  lived, revocable, non-persistent by default, and expires after that response.
  The amount and authorization state never enter general logs, audit documents,
  Git, or a later Codex response without new authorization. The output must not
  reveal the underlying exact profile values used to derive it. Without that
  authorization, the amount remains in an owner-only local view.
- Yangjibao holdings, inferred cost, and pending-transaction observations cannot
  authorize an exact action amount by themselves. Exact amounts require local
  transaction/position confirmation or return `insufficient_data`.
- News storage uses metadata and bounded excerpts with a retention policy.
- Decision snapshots store identifiers and bounded structured evidence, not
  unrestricted copyrighted or private source content.
- Docker/LibreOffice remains optional, `--network=none`, `--pull=never`, and
  used only for already authenticated legacy documents in deep mode. Phase 6
  conversion creates a unique parent-readable Docker `cidfile`; after worker
  exit or termination, the parent inspects, stops, and removes only that exact
  container ID and verifies absence. Broad label/name cleanup is prohibited.

## 21. CLI And Skill Contract

The final command design is staged, but logical families include:

```text
kunjin --json status
kunjin --json sync portfolio
kunjin --json sync intelligence
kunjin --json source status
kunjin --json news recent
kunjin --json market overview
kunjin --json portfolio diagnose
kunjin --json portfolio candidate CODE
kunjin --json fund compare CODE...
kunjin --json decision review CODE
kunjin --json decision simulate-buy CODE
kunjin --json position review CODE
kunjin --json monitor daily
kunjin --json learning status
kunjin --json report weekly
```

Codex-facing envelopes preserve:

- schema and policy versions;
- command and resolved request mode;
- `as_of` and all material data dates;
- workflow level;
- conclusion evidence;
- coverage and unknown exposure;
- action tendency and shadow state;
- supporting and opposing evidence;
- conflicts;
- invalidation conditions;
- missing evidence;
- supplementation requests;
- next review time;
- warnings; and
- errors.

The installed and repository `kunjin-fund` Skills must change before news or D2
features are considered usable. The Skill will:

- decompose subquestions and actions;
- stop applying Phase B to fact-only work;
- permit risk-maintaining and risk-reducing analysis while preserving the
  financial conflict;
- apply full gates to risk-increasing and switch purchase legs;
- stop treating non-verified D1 as a block on rapid research;
- preserve D1 as one component of decision evidence; and
- never describe rapid research as decision-grade merely because it completed.

## 22. Minimum Usable Vertical Slice

The first implementation does not build the full six-section system. It proves
one end-to-end user workflow.

### 22.1 Inputs

- one new fund code; or
- one held-fund question such as whether new evidence supports holding or an
  exit review.

### 22.2 Retrieval

Within a real 90-second `RequestBudget`, use existing or bounded sources for:

- identity and A/C relationship;
- current manager;
- fee overview;
- latest formal NAV;
- latest top-ten holdings and report date;
- fund announcements;
- current personal position and portfolio weight; and
- a limited allowlist of directly relevant news/announcement sources.

Only the queried fund may perform cold synchronization in the rapid request.
Portfolio-side funds use already cached evidence. Missing or stale portfolio-
side evidence lowers coverage and is reported; the rapid request does not cold-
sync every held fund.

Phase 1 news is limited to official fund/product announcements plus manually
auditable citations with minimal original-source lineage. It does not claim
semantic cross-media independence.

Unsupported official domains lower coverage and generate supplementation. They
do not trigger adapter development.

### 22.3 Calculations

- same manager;
- same explicit index/theme;
- top-ten disclosed overlap;
- adjusted-return correlation;
- portfolio coverage and unknown exposure; and
- deterministic financial/transaction blocks already available.

### 22.4 Output

- facts and dates;
- missing evidence;
- source lineage and the independence level actually established;
- opposing evidence;
- portfolio impact;
- whether new evidence changed the prior result; and
- for a real held fund: one of `hold`, `no_add`, `watch`,
  `reduce_or_exit_review`, or `abstain`; or
- for a new candidate: one of `candidate_watch`, `not_preferred`, or `abstain`.

Every action interpretation includes `action_maturity`. Unpromoted states are
`experimental_shadow`.

Market-timing labels remain shadow-only. Exact buy amount remains gated by the
later complete B/C/D2/D3 and post-trade path.

### 22.5 Performance target

- hot-cache terminal response: target p95 at or below 10 seconds;
- cold rapid query: terminal complete or partial response at or below 90
  seconds;
- explicit deep query: terminal complete or partial response at or below 480
  seconds; and
- official deep document work: no minute-level completeness guarantee and never
  in the rapid critical path.

The deep target assumes primary sources are available or useful cache is
present. No mode promises that all fields for five candidates will complete.
The MVP has no implicit background continuation.

## 23. Delivery Sequence

### Phase 0: Usability and evidence foundation

- `RequestBudget` propagation and cancellation;
- per-subquestion/per-action router;
- workflow/evidence separation;
- decision evidence matrix foundation;
- partial-result and supplementation contracts;
- source-health view derived from bounded request/source-attempt records;
- installed/repository Skill update; and
- real latency acceptance.

### Phase 1: Minimum usable vertical slice

- one fund or one holding;
- existing profile, NAV, manager, fee, holdings, announcement, and portfolio
  sources;
- deterministic D2 subset;
- official-announcement news plus minimal source lineage;
- simple user-visible monitoring states; and
- live owner acceptance without personal amounts in audit artifacts.

### Phase 2: News and market intelligence

- event lineage, independence, correction, retraction, and entity mapping;
- bounded source registry;
- additional market dimensions as real sources become available; and
- shadow market-state validation.

### Phase 3: Incremental D2

- controlled multidimensional taxonomy;
- industry mapping;
- look-through exposures;
- correlation and stress behavior;
- candidate marginal impact; and
- coverage-aware portfolio diagnosis.

The Phase 1 deterministic D2 subset is sufficient for Phase 4 candidate work.
Complete industry, factor, and stress coverage is not a prerequisite for D3;
missing dimensions remain explicit.

### Phase 4: Complete D3

- two-to-five candidate comparison;
- product-specific metrics;
- A/C break-even scenarios;
- transaction and channel supplementation;
- target-point policy; and
- post-trade simulation and exact local amount.

The MVP D3 acceptance is limited to the explicit candidate set and reports
candidate-set selection bias. A complete historical universe of liquidated
funds is not required.

### Phase 5: Complete Phase E

- holding contracts;
- target bands;
- monitoring history;
- hard-event triggers;
- reduce/exit review;
- rebalancing; and
- shadow policy promotion process.

### Phase 6: Selective deep official evidence

- add audited manager/index-provider adapters by reusable source family;
- prioritize actual final candidates and action-critical fields;
- keep legacy Docker conversion optional; and
- never block ordinary rapid research on adapter coverage.

Each phase ends with an independent financial review, product usability review,
real latency evidence, and a fresh beginner-workflow assessment.

## 24. Testing And Acceptance

### 24.1 Budget and degradation

- deadlines propagate through pagination, peers, source retries, and conversion;
- blocking adapter timeouts are capped by remaining budget;
- a stuck DNS lookup, slow continuous response, worker that ignores graceful
  termination, and attempted late database write each leave no worker/process
  behind and cannot exceed the advertised terminal deadline;
- only the parent commits source results, and no expired/cancelled worker output
  reaches SQLite;
- cancellation stops scheduling new work and prevents late publication;
- in-flight blocking calls are safely ignored when they finish after expiry;
- one source failure preserves other results;
- cooldown prevents repeated polling;
- every `source_field_state` transition and every aggregate
  `request_field_resolution` is deterministic and schema-valid;
- source status shows last success, failure reason, cooldown, configured
  alternatives, and separately computed manual-supplementation resolution;
- budget expiry returns a bounded partial response;
- no interactive request builds an adapter or Docker image; and
- hot-cache p95, cold rapid terminal latency, and deep terminal latency are
  recorded against the explicit SLA.

Latency acceptance includes two public, amount-free live cases declared before
the run:

- with primary sources healthy, a representative fund returns identity/share
  class, current manager/team, latest formal NAV and date, current fee overview,
  latest available disclosed holdings and report date, applicable official
  announcements, position presence/absence, and at least one real portfolio-
  relationship calculation within the rapid deadline; and
- with an intentionally unsupported source family, the result still returns
  every fact obtained, each missing field's action impact, acceptable
  alternatives, and a concrete supplementation path within the rapid deadline.

A response containing only an SLA marker or bare `abstain` does not pass either
case. Latency success alone cannot increase the beginner-workflow score.

Each run records whether it uses a fresh isolated database or a prewarmed one;
the queried fund sections present before start; every portfolio-side cached fund
identifier, section freshness, and disclosure period; network-attempt count;
cache-hit count; and omitted work. The healthy-source case must include at least
one fresh isolated queried-fund cold synchronization. Portfolio-side evidence
may be prewarmed, but its exact starting state must be disclosed.

### 24.2 Routing

- one prompt with facts, sale, and switch creates separate actions;
- a sale-to-cash analysis continues under Phase B blocked;
- a switch purchase leg remains blocked when new money is blocked;
- holding explicitly reports financial-safety conflict; and
- blocked action does not suppress independent facts.

### 24.3 Evidence

- workflow level cannot promote source quality;
- reprints share one original lineage;
- corrections and retractions invalidate dependent conclusions;
- user uploads remain `user_provided` until validated;
- conflict and freshness rules are deterministic;
- missing critical fields cause abstention rather than lower standards; and
- each decision snapshot round-trips its canonical EvidencePolicy version,
  content, and verified checksum; and
- source attempts and decisions round-trip the source-registry version and
  verified checksum.

### 24.4 D2

- A/C siblings are one economic exposure;
- same manager and same company remain distinct;
- explicit same index is detected;
- top-ten overlap retains disclosure scope;
- adjusted-return correlation uses aligned samples;
- known and unknown exposures are both shown;
- low coverage cannot imply diversification; and
- insufficient product-specific D2 coverage blocks mature risk-increasing
  direction and exact amount while preserving research and labeled simulation.

### 24.5 D3

- peer comparisons use common dates;
- predecessor performance is not attributed to the current manager;
- explicit candidate-set and survivor-bias limitations are visible;
- the broad-index baseline follows the deterministic eligibility and tie-break
  policy, or explicitly reports that no valid baseline exists;
- A/C break-even scenarios require complete inputs;
- suspended subscription or unknown channel state blocks an executable purchase
  conclusion;
- post-trade simulation exposes every binding constraint; and
- tactical/sector caps aggregate transaction-after look-through exposure across
  linked accounts, holdings, and pending transactions, with conservative
  capacity reserved for unknown exposure.

### 24.6 Phase E

- one-day price movement cannot independently trigger exit review;
- repeated questions do not escalate state;
- new evidence and conclusion changes are explicit;
- thesis drafts require owner confirmation;
- hard events trigger review;
- exit triggers still run fee/settlement/use-of-proceeds checks; and
- exact reduction requires position and target-band evidence.

MVP action-state tests assert both the Chinese explanation and
`action_maturity`; unpromoted holding/watch/exit interpretations must be
`experimental_shadow`.

### 24.7 Learning account

- status requires explicit enrollment;
- account names grant no privilege;
- blocked Phase B produces zero new real budget;
- existing positions remain analyzable;
- cap history prevents reset/evasion;
- watchlist simulation remains available; and
- formal investment conversion reruns the complete chain.

### 24.8 Security and privacy

- no live credential or exact private profile amount enters tests, logs, JSON,
  audits, or Git;
- credential-bearing worker tests prove tokens are absent from argv,
  environment, temporary files, IPC results, and logs;
- no browser-cookie access or authentication bypass;
- web prompt injection cannot alter policy;
- copyrighted full article bodies are not accumulated;
- Phase 6 cancellation removes only the exact cidfile-bound Docker container
  and leaves unrelated containers untouched; and
- all account operations remain read-only.

Exact-output authorization tests prove that authorization is scoped to one
request and action, expires after response, can be revoked, does not persist by
default, and cannot expose the amount in later Codex JSON, logs, audits, or Git.

## 25. Objective Capability Claim

The implemented product remains approximately 54/100 at design time.

If all phases are implemented and pass real acceptance, an independent reviewer
estimates approximately 75% to 85% coverage of the reasonably automatable
beginner fund-decision workflow. Public-information organization, portfolio
diagnosis, and risk warnings may approach or exceed 90% within their bounded
scope. Market direction and optimal exit timing cannot credibly claim 90%
reliability.

No implementation milestone may convert that estimate into a guarantee. Every
phase must be rescored from observed behavior.

## 26. Final Acceptance Gate

This redesign is accepted only when:

- rapid research remains usable under Phase B blocked;
- risk-reducing analysis is available without opening a new-money bypass;
- a real one-fund or held-fund query returns a useful terminal complete or
  partial result within the global budget;
- unsupported sources create supplementation rather than hours of retries;
- conclusion evidence remains source- and date-specific;
- market and Phase E signals remain shadow-only until independently promoted;
- the MVP solves a real owner question before broader infrastructure expansion;
- each later phase has independent financial and product review; and
- all action output remains conditional, explainable, and non-executing.
