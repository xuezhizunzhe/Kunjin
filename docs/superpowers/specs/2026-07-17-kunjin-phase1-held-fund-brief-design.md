# KunJin Phase 1 Held-Fund Brief Design

Date: 2026-07-17

Status: owner-approved design sections, pending written-spec review

## 1. Purpose

Phase 1 builds the first end-to-end useful KunJin workflow: answer one question
about one currently held public fund within a bounded request and return a
beginner-readable, evidence-linked, conditional interpretation.

The phase closes the main Phase 0 usability gap. Phase 0 can acquire and count
records, route actions, expose source health, and fail closed, but it does not
turn those records into one stable answer that a beginner can inspect.

The first supported interaction is equivalent to:

```bash
kunjin --json fund brief 519755 \
  --action continue_holding \
  --mode rapid
```

The fund code is a request parameter. No personal holding is hard-coded into
source, fixtures, scripts, documentation, or the installed Skill.

## 2. Objective Boundary

Phase 1 includes:

- one fund and one routed owner action per brief request;
- an existing current financial profile and Phase B status when the action
  requires suitability context;
- existing public identity, formal NAV, manager, fee, holdings, and official
  announcement sources;
- current position presence and an amount-free portfolio relationship view;
- a deterministic minimum D2 subset;
- official fund/product announcement events with minimal lineage;
- one user-visible monitoring interpretation;
- a fixed beginner-readable Chinese explanation; and
- local, public, unsupported-source, and real-owner acceptance.

Phase 1 excludes:

- broad financial-media ingestion or general industry-news crawling;
- semantic event clustering and independent-media confirmation;
- complete industry, style, factor, country, currency, credit, duration, or
  stress look-through;
- two-to-five candidate selection and universal ranking;
- A/C break-even purchase choice;
- post-trade simulation, target-point policy, or exact amount;
- a mature market-timing, ordinary hold, reduce, sell, or switch policy;
- automatic transactions; and
- on-request Docker builds, image pulls, package installation, or source-
  adapter development.

These exclusions bind Phase 1 even when a source title, platform category, or
model-generated narrative appears confident.

## 3. Chosen Architecture

Phase 1 adds a deterministic aggregate service and a JSON-only CLI command.
Codex explains the resulting schema but does not orchestrate a variable list of
legacy commands or invent missing conclusions.

```text
fund brief request
-> validate fund/action/mode
-> create RequestBudget and request audit
-> route fact_research plus requested owner action
-> inspect cache and source health
-> run prioritized bounded public/portfolio work
-> construct sourced facts and official events
-> calculate minimum D2 relationships
-> evaluate evidence sufficiency and action state
-> compare with the prior amount-free brief snapshot
-> atomically publish business result plus audit state
-> return strict JSON and deterministic Chinese explanation
```

The aggregate service reuses existing `decision`, `funds`, `analytics`,
`peers`, suitability, portfolio, and storage components. It does not duplicate
source adapters or bypass their validation.

Recommended module ownership:

```text
src/kunjin/brief/
  models.py       exact enums and validated immutable report records
  policy.py       Phase 1 evidence, D2, event, and action-state rules
  d2.py           amount-free deterministic portfolio relationships
  service.py      budgeted orchestration and atomic request lifecycle
  research.py     strict public JSON and Chinese explanation projection
  store.py        amount-free brief snapshot persistence and history
```

The implementation plan may merge a very small module only when doing so keeps
one clear responsibility and does not grow `cli.py` with business logic.

## 4. Request Contract

The initial CLI form is:

```text
kunjin --json fund brief FUND_CODE --action ACTION [--mode rapid|deep]
```

Rules:

- JSON mode is mandatory.
- `FUND_CODE` is exactly six digits.
- The Phase 1 owner-action allowlist is `continue_holding`,
  `reduce_to_cash`, `full_exit`, and `switch_funds`.
- A switch request expands into `switch_reduce` and `switch_buy`; the purchase
  leg retains every risk-increasing gate.
- `fact_research` is always added internally as an independent action.
- Rapid is the default and owns one 90-second terminal budget.
- Deep is explicit and owns one 480-second terminal budget.
- The command never executes a transaction.
- The command never outputs a proposed exact transaction amount.

The same request ID binds routing, source attempts, synchronized facts, D2
calculations, state interpretation, snapshot persistence, and public output.

## 5. Work Scheduling And Budget

Rapid work is scheduled in this order:

1. identity, exact share class, and active status;
2. current position presence and observation time;
3. latest expected formal NAV and date;
4. current manager/team and applicable fee overview;
5. latest available disclosed holdings and report period;
6. official fund/product announcements; and
7. D2 calculations using usable current or dated portfolio-side cache.

Scheduling rules:

- fact research does not wait for Phase B or Phase C;
- a suitability failure cannot suppress public facts or risk-reducing research;
- a failed position refresh cannot suppress public facts;
- one source failure does not cancel other sources;
- a cooldown state is terminal for that source in the current request;
- cancellation or expiry stops new scheduling and prevents late publication;
- no implicit background continuation exists;
- official deep-document work is not added to the Rapid critical path;
- existing legacy Docker conversion is used only by an explicitly configured
  Deep workflow and is never built or pulled by a brief request; and
- portfolio-side cache is not silently called current: its observation time,
  section freshness, report period, and coverage are retained.

`fund brief` is a current-state command. It always attempts to refresh the
personal position observation in the same request when authorization is
available. Failure becomes an explicit missing field and does not trigger an
authentication bypass or repeated polling. A later dated-history command must
use a separate contract rather than silently changing this one.

## 6. Source And Fact Model

Every public fact contains:

- `fact_id` and stable `field_id`;
- normalized value and unit;
- data date, publication time, and retrieval time where applicable;
- `source_id`, source tier, publisher, and canonical URL reference;
- evidence freshness and completeness;
- conflict identifiers; and
- whether the value is a direct fact or deterministic calculation.

Facts are not flattened into one universal score. At minimum the brief can
represent:

- fund code, exact name, share class, type, and active status;
- current manager/team and tenure start;
- latest formal NAV and NAV date;
- published fee overview and explicit unknown fee conditions;
- latest available disclosed holdings, report period, publication time, and
  disclosure scope;
- current D1 classification and its evidence state when available; and
- applicable official product events.

One identified Tier 2 source can support a dated Rapid fact when its tier and
date remain visible. It cannot be promoted to verified identity, a mature
personal action, or an exact amount.

## 7. Official Announcement Events

Phase 1 news is limited to official fund, product, and manager announcements
already available from audited sources or a manually auditable official URL.

Each event retains:

- stable event code;
- title and bounded summary;
- publisher;
- canonical URL;
- publication and retrieval times;
- source tier;
- original-source identifier;
- quoted-source identifier when the item is a reprint;
- exact-content fingerprint;
- correction or retraction status; and
- affected action fields.

Initial stable event codes include:

- `fund_liquidation_notice`;
- `fund_termination_notice`;
- `manager_change_notice`;
- `subscription_suspension_notice`;
- `redemption_restriction_notice`;
- `fee_change_notice`;
- `benchmark_change_notice`; and
- `other_official_product_notice`.

Reprints sharing one original source count as one lineage. A user-provided
video, article, screenshot, or URL remains `user_provided` until its identity,
publisher, date, and original-source relationship are validated. Phase 1 does
not claim broad news coverage or independent-media confirmation.

## 8. Minimum D2 Subset

Phase 1 calculates only relationships supported by current validated inputs.

### 8.1 Position And Concentration

- current fund position presence;
- observation time and observation evidence type;
- current portfolio weight when deterministically calculable;
- portfolio HHI; and
- largest single-fund share.

The brief never exposes current value, shares, cost, observed profit, or inferred
purchase lots in audit artifacts. Owner-local JSON may expose the amount-free
weight ratio and observation timestamp.

### 8.2 Economic And Organizational Relationships

- A/C or other authenticated share-class siblings are one economic exposure;
- exact same fund code is a duplicate holding identity;
- exact same authenticated index is a same-index relationship;
- benchmark-family similarity alone is not same-index evidence;
- same current manager/team and same fund company are separate relationships;
  neither implies the other; and
- unresolved identity or effective-date conflicts make the relationship
  unknown.

### 8.3 Disclosed Holdings Overlap

Pairwise holding overlap:

- retains both fund codes;
- retains both report periods and publication times;
- uses `top10_disclosed_overlap` whenever either side is top-ten-only;
- retains each side's disclosed-weight coverage;
- reports shared securities and the minimum disclosed weight contribution;
- warns on period or identity mismatch; and
- never interprets missing, stale, or omitted holdings as zero exposure.

Portfolio look-through reports included and omitted fund codes, portfolio-
weight coverage, disclosure periods, and unknown exposure separately.

### 8.4 Adjusted-Return Correlation

Correlation is available only when both funds have validated cumulative-NAV or
total-return series, aligned formal-NAV dates, a common end date, and the policy
minimum sample. NAV-level correlation, silently different windows, or an
unresolved dividend/split discontinuity returns `insufficient_data`.

Formal-NAV freshness uses a trading-date contract. The caller's aware expected
datetime is reduced to its own calendar date, then represented as UTC midnight;
the persisted NAV date uses the same representation. Time-of-day and UTC
conversion cannot shift the expected trading day.

The NAV worker projects only `none`, `present`, or `unknown` from the source's
corporate-action field and never returns the raw text. Schema v17 persists that
bounded state beside each public NAV row so live and cached quality checks are
identical. Each brief-written row also carries a nullable foreign key to the
formal success `SourceAttempt`, inserted and consumed in the same parent
transaction. Generic repository writes remain unbound and are never eligible
brief cache evidence. Cached quality uses only the latest `retrieved_at` batch
whose attempt binding authenticates source, field, fund, outcome, data date,
and request lifetime; older overlapping and newer unbound rows never
contaminate the selected window. An
adjusted-return series is usable only with at least 60 samples,
complete positive accumulated NAV, `none` for every corporate-action state, a
constant `accumulated_nav - unit_nav` across the selected window, and no sign
conflict between published daily growth and adjacent unit-NAV change. The last
two checks are deterministic continuity invariants; Phase 1 does not invent a
price-jump threshold. Missing, present, or contradictory evidence fails closed
without suppressing the dated formal unit-NAV fact.

### 8.5 Explicitly Unknown Dimensions

Phase 1 does not infer authenticated current industry exposure while the
production controlled-taxonomy registry is empty. Style, factor, country,
currency, complete credit, duration, and stress relationships remain unknown
unless an existing validated product-specific fact directly supports them.

Low D2 coverage cannot produce a reassuring diversification claim.

## 9. Evidence Status

The result separates two scopes:

- `sync_status`: completeness of work actually scheduled for this brief; and
- `decision_evidence_status`: sufficiency for the requested owner action.

Allowed high-level states are `complete`, `partial`, and `insufficient`.
Decision evidence additionally lists:

- fields required by the current action;
- fields obtained;
- fields missing, stale, conflicted, unsupported, or in cooldown;
- supported interpretations;
- unsupported interpretations;
- acceptable alternative sources; and
- a concrete manual-supplementation path.

For example, missing fee details may leave a watch interpretation available
while blocking exact fee, executable redemption, and every exact amount. It
does not convert unrelated NAV or manager facts into missing data.

## 10. Action Interpretation

The primary state is one of:

- `no_add`;
- `hold`;
- `watch`;
- `reduce_or_exit_review`; or
- `abstain`.

Every state includes `action_maturity`, supporting evidence, opposing evidence,
blocking codes, missing fields, invalidation conditions, unavailable actions,
and `exact_amount_available=false`.

The Phase 1 precedence is:

1. a current Phase B hard block makes the primary state `no_add` and preserves
   the exact hard-block codes;
2. an authenticated liquidation or termination notice adds a mandatory
   `reduce_or_exit_review` entry to `triggered_reviews` even when `no_add` is
   primary;
3. an identity conflict or action-critical evidence gap produces `abstain` for
   the affected interpretation;
4. a supported risk event that does not authorize exit produces `watch`;
5. an owner-confirmed thesis whose invalidation has not triggered may produce
   `hold`, but only as `experimental_shadow`; and
6. sufficient facts without an owner-confirmed thesis produce `watch`, not an
   inferred hold recommendation.

Primary-state precedence is presentation only. `constraints`,
`triggered_reviews`, and `affected_action_abstentions` retain every simultaneous
condition. A primary `no_add` cannot hide an identity conflict, liquidation or
termination review, missing redemption evidence, or a blocked switch-buy leg.

Maturity rules:

- deterministic Phase B `no_add` may be `mature` as a safety constraint;
- an authenticated liquidation or termination notice may maturely trigger an
  exit review, never an immediate sale;
- `hold`, ordinary `watch`, and non-policy reduce/exit interpretations remain
  `experimental_shadow` in Phase 1;
- one-day price movement, short-term ranking, or one media claim cannot by
  itself trigger reduce/exit review; and
- a Phase B block is not itself evidence that the fund should be sold.

For `switch_funds`, `switch_reduce` and `switch_buy` retain independent states.
The purchase leg cannot inherit permission from the reduction leg and remains
blocked by Phase B, Phase C, D1, D2, D3, and post-trade requirements.

## 11. Strict Public Output

The command returns one schema-versioned envelope whose `data` object has these
exact top-level sections:

```json
{
  "request": {},
  "subject": {},
  "facts": [],
  "official_events": [],
  "portfolio_relationship": {},
  "sync_status": {},
  "decision_evidence_status": {},
  "action_interpretation": {},
  "missing_evidence": [],
  "beginner_explanation_zh": {}
}
```

`beginner_explanation_zh` is deterministic and contains:

1. `headline`: a conditional one-sentence state, not a bare trade command;
2. `fund_identity`: what the fund is and the applicable data date;
3. `portfolio_relationship`: known relationships and unknown coverage;
4. `recent_official_events`: verified official events only;
5. `why_this_state`: supporting and opposing evidence separately;
6. `evidence_gaps`: what is missing and which conclusion it affects; and
7. `change_conditions`: evidence or invalidation events that require review.

The Chinese projection cannot rename stable codes, omit partial/conflicted/
stale state, translate `mature` as a mature financial judgment, infer an
industry from the name, or output an exact amount. Codex may improve phrasing
around the validated result but may not change status, maturity, evidence tier,
blocking code, missing field, or source attribution.

## 12. Persistence And Atomicity

Phase 1 stores an amount-free brief snapshot for monitoring and audit. The
snapshot contains:

- request-run ID, fund code, routed action IDs, mode, and timestamps;
- primary state, maturity, triggered-review codes, and blocking codes;
- evidence completeness/freshness and source lineage identifiers;
- missing/conflict identifiers;
- evidence fingerprint and canonical result checksum;
- whether the conclusion changed from the prior snapshot; and
- terminal request status.

The snapshot does not store exact profile amounts, current position value,
shares, cost, observed profit, proposed amount, credential material, raw
response body, or managed private paths.

The persisted evidence fingerprint and result checksum exclude personal
position weight and every value derived from an amount. The owner-local weight
is an ephemeral output overlay. Snapshot bindings may retain only position
presence, the opaque source observation version, and its timestamp.

Each completed source attempt and its validated public fact/cache mutation are
committed by the parent under the authenticated request so source health and
cooldown survive a later report failure. Expired, cancelled, failed, or late
worker output cannot reach SQLite. The final decision route, brief snapshot,
and request terminal state are then published in one transaction. A failed
final transaction leaves no current brief snapshot and cannot replace the prior
current interpretation.

## 13. Privacy And Audit Projection

Owner-local JSON may show:

- position presence;
- amount-free portfolio weight;
- observation time;
- relationship coverage; and
- stable decision/evidence codes.

Public synthetic live-audit artifacts may show:

- public fund code;
- elapsed time and mode;
- cache/network/source counts;
- section and evidence states;
- relationship type and coverage percentages from synthetic acceptance only;
- action state/maturity and blocking codes; and
- result and policy checksums.

The real-owner acceptance audit does not show the held fund code. It uses a
random opaque subject ID whose private run-local mapping is destroyed before
audit publication, plus position-present, state, maturity, coverage class,
elapsed time, and stable error/blocking codes.

Live audit artifacts must not contain real current value, shares, cost, profit,
exact profile fields, personal position weight, complete personal holdings,
token, local private path, raw exception, or response body.

All Yangjibao operations remain allowlisted and read-only. The command never
operates Alipay or mutates an external account.

## 14. Testing

### 14.1 Fact And Source Tests

- each projected fact retains source and date;
- conflicts, stale values, and missing fields cannot be silently replaced;
- one Tier 2 fact remains Tier 2;
- reprints share one lineage;
- correction/retraction state invalidates dependent interpretations; and
- user-provided material cannot self-promote to official evidence.

### 14.2 D2 Tests

- A/C siblings aggregate as one economic exposure;
- same manager and same company remain distinct;
- same-index requires exact authenticated identity;
- top-ten overlap retains dates, scope, and coverage;
- missing/stale holdings remain unknown;
- adjusted-return correlation uses aligned validated samples; and
- low coverage cannot produce a diversification conclusion.

### 14.3 Action Tests

- Phase B blocked produces at least `no_add` without suppressing facts;
- official liquidation/termination triggers review but not an immediate sale;
- one-day movement cannot trigger exit review;
- no owner-confirmed thesis means no inferred `hold`;
- identity conflict produces affected-action abstention;
- risk-reducing research remains available;
- switch legs remain independent; and
- exact amount remains unavailable in every Phase 1 state.

### 14.4 Budget And Failure Tests

- Rapid owns one 90-second deadline and Deep owns one 480-second deadline;
- DNS failure, slow continuous output, ignored termination, oversized IPC,
  child/grandchild workers, and late writes terminate safely;
- cooldown prevents repeated requests;
- one source failure preserves other facts;
- publication remains parent-only and atomic; and
- no brief request invokes Docker build/pull, package installation, or adapter
  development.

### 14.5 Chinese Projection Tests

- Chinese wording and stable state agree;
- partial, opposing evidence, and missing fields are retained;
- `mature` is not described as financial certainty;
- no unconditional trade instruction appears; and
- every displayed source marker resolves to a projected source.

## 15. Real Acceptance

### 15.1 Public Cold Healthy Case

A declared public fund code runs against a fresh queried-fund database. A
synthetic, non-personal portfolio-side fixture may be prewarmed, but its exact
starting state is recorded. Within 90 seconds the result must include:

- identity/share class;
- current manager/team;
- latest formal NAV and date;
- current fee overview or explicit fee gap;
- latest available disclosed holdings and report date;
- applicable official announcements;
- position presence/absence; and
- at least one real deterministic portfolio relationship.

A bare SLA marker, record count, or `abstain` does not pass.

### 15.2 Public Unsupported-Source Case

A different, predeclared public fund code whose source family is unsupported
runs in a fresh isolated runtime. Within 90 seconds it must return all obtained
facts plus each missing field's action impact, acceptable alternative, and
concrete supplementation path. It must not retry indefinitely or create an
adapter.

### 15.3 Real Owner Held-Fund Case

One current holding selected from the owner's live read-only portfolio runs
privately. The owner validates position presence, relationship interpretation,
and conditional state. The retained audit records only safe metadata from
Section 13 and never personal amounts or a complete holding list.

### 15.4 Action Coverage

Amount-free real CLI acceptance covers:

- `continue_holding`;
- `reduce_to_cash`;
- `full_exit`;
- `switch_reduce`; and
- `switch_buy`.

It must prove facts remain independent, reduction research remains available,
purchase remains gated, and no mature market-timing or exact output is exposed.

## 16. Independent Review And Score

Two fresh read-only reviewers complete the phase:

- a financial reviewer checks suitability bypass, no-add behavior, evidence
  sufficiency, D2 interpretation, hard-event review, switch-leg independence,
  and accidental buy/sell timing; and
- a product reviewer checks bounded latency, useful partial output, explanation
  clarity, deterministic source state, cleanup, privacy, and absence of
  interactive infrastructure work.

P0 and P1 findings must be fixed before completion. Every retained P2 requires
an explicit later-phase binding. The reviewers rescore the same 100-point
beginner workflow from observed behavior. Test count, code volume, response
speed, and documentation do not automatically increase the score. No score is
preassigned and Phase 1 cannot claim 90 percent assistance by design.

## 17. Completion Gate

Phase 1 completes only when:

- one held-fund query returns useful facts and a conditional state within the
  global Rapid budget;
- every displayed fact is date- and source-linked;
- D2 known and unknown relationships remain distinguishable;
- official events cannot become unsupported trade commands;
- current financial blocks do not suppress facts or prove a sale;
- all action routes preserve their risk effects and exact-output boundary;
- unsupported sources return bounded supplementation instead of interactive
  adapter work;
- no personal amount enters JSON acceptance artifacts, logs, audits, or Git;
- local, fault, public live, private owner, and independent-review gates pass;
  and
- the owner explicitly approves completion before Phase 2 begins.
