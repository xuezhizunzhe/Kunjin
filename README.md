# KunJin

KunJin is a local fund research and personal evidence-ledger foundation operated
through Codex. Its Yangjibao integration is read-only; ledger commands write only
to KunJin's local SQLite database and private import directory.

Phase one synchronizes personal account and holding observations from Yangjibao,
stores redacted snapshots in SQLite, and calculates reproducible portfolio totals
and concentration metrics. The current build also supports formal-NAV fund risk
research, sourced fund identity and disclosure research, A-share sector
strength/breadth, deterministic peer comparison and disclosed-holdings overlap,
one-held-fund conditional briefs, investment theses, weekly reports, and a
weekday post-close synchronization job.

KunJin does not log in to or operate Alipay, modify Yangjibao data, place fund
orders, or produce automatic trading instructions.

## Phase 0 Live Acceptance

Phase 0 proves bounded routing, source health, and graceful partial results.
It does not prove news intelligence, D2/D3, mature hold/sell timing, or an exact
buy amount. Full one-fund usefulness acceptance belongs to Phase 1.

After local unit verification, run the amount-free public acceptance with an
approved public fund code and a new absolute output directory:

```bash
scripts/run_phase0_acceptance.sh 000000 /private/tmp/kunjin-phase0-results
```

Replace `000000` at execution time with the approved six-digit public code. No
personal holding is hard-coded. The script uses a fresh private data/state
runtime, performs one rapid public profile synchronization, checks the pre/post
source state and fact/buy routes, and writes only validated amount-free JSON and
a summary. One global 90-second deadline covers all five CLI commands, strict
schema projection, private staging, and exclusive atomic publication without
overwriting an existing directory. A non-secret per-run marker and bounded
stable scan detect workers that detach from the CLI; the marker is not written
to result JSON or logs. Publication and rollback open the renamed directory and
verify its inode before deleting anything. A command that leaves a child behind
or encounters a mismatched concurrent directory fails closed and reports that
residue may remain. These controls support cooperative same-UID local
concurrency and prevent accidental overwrite or deletion; they are not a
security boundary against a malicious process running as the same user, which
can modify the code, process environment, or output directory. The acceptance
does not read the personal profile or database, synchronize Yangjibao, use
Docker, poll a failed source, or authorize a mature purchase or exact amount.
The output directory must not already exist.

## Held Fund Brief (Phase 1)

For one currently held fund question, use the bounded aggregate command only
when the request includes `continue_holding`, `reduce_to_cash`, `full_exit`, or
`switch_funds`, instead of assembling that action answer from separate legacy
commands:

```bash
.venv/bin/kunjin --json fund brief 519755 --action continue_holding --mode rapid
```

The supported owner actions are `continue_holding`, `reduce_to_cash`,
`full_exit`, and `switch_funds`; `fact_research` is always added internally.
Fact-only questions stay on the standalone `fact_research` route. Any buy or add
request, including an already-held fund, stays on standalone `buy_or_add`; the
brief may supply separate facts but never replaces the risk-increasing gate.
Rapid is the default with one 90-second terminal budget. Deep is explicit with
one 480-second terminal budget. A cooldown or failed source produces a useful
partial result and exact manual-supplementation requirements rather than
background retries or interactive adapter work.

The result keeps `terminal_status`, `sync_status`, and
`decision_evidence_status` separate. `terminal_status=complete` means no
scheduled work was omitted; it is not a financial conclusion, proof of complete
evidence, or action authorization. Likewise, a usable public fact does not imply
that the requested action is sufficiently supported. Each fact retains its
source tier, data date, publication time, and conflicts. A Tier 2 fact remains
visibly Tier 2 and cannot authorize a mature personal action.

Phase 1 implements a minimum D2 subset for position presence, authenticated
relationships, and disclosed-holdings overlap. Read
`minimum_relationship_coverage` and `disclosed_holdings_coverage` separately;
missing or stale evidence keeps unknown relationships unknown rather than zero.
This minimum subset never satisfies the complete D2 gate required for buy/add or
the purchase leg of a switch.
Official-event coverage is limited to audited fund, product, and manager
announcements. Every action remains conditional, automatic trading is absent,
and the public result preserves `exact_amount_available=false`.

Broad financial-media ingestion, complete D2 portfolio construction, D3
candidate selection and pre-purchase checks, and Phase E mature monitoring and
sell timing are not implemented.

## Pragmatic News And Market Intelligence MVP

Use the three bounded, JSON-only public-intelligence commands after a current
`fact_research` route:

```bash
.venv/bin/kunjin --json news recent --window recent --mode rapid
.venv/bin/kunjin --json market overview --window recent --mode rapid
.venv/bin/kunjin --json fund intelligence 519755 --window recent --mode rapid
```

Rapid owns one 90-second request budget; explicit Deep owns 480 seconds. The
result preserves each source attempt's outcome, reason, retryability, cooldown,
manual supplementation, source tier, publication date, retrieval date, and
coverage gaps. `items` are attributed source `fact` records. `events` are
`reasoned_inference`; their `lineage` identifies reprint relationships, so a
reprint never becomes independent confirmation. Partial success remains useful,
while a total source failure returns `insufficient_data` without invented facts.
Caps and deadline omissions are terminal results, not instructions to retry in a
loop or develop an adapter during the request.

Market rows currently have HTTP retrieval time but no authenticated exchange
session time. Treat that as `market_session=unknown`; the safe interpretation is
`direction=insufficient_data`, not a same-day timing or buy/sell signal. Sector
rank observations remain `experimental_shadow`, freshness is unknown, and source
accuracy is not prediction accuracy.

`fund intelligence` supplies `disclosed_context`: when available disclosed
identity terms, benchmark terms, and disclosed top-ten security names may
explain a relevance link, but they do not prove current exposure or
whole-portfolio composition. Use
`fund brief`, `fund profile`, `fund fees`, and `fund research` for held-position,
identity, manager, fee, formal-NAV, and risk facts. Use `portfolio diagnose` for
current concentration, authenticated relationships, and
`top10_disclosed_overlap`; the intelligence command does not replace it.

A thesis result of `possible_invalidation_match` or `no_matching_evidence`
always requires manual semantic review. A string match cannot understand
negation or context and cannot trigger a sale. Every intelligence response stays
`action_maturity=evidence_only`, `action_authorized=false`, and
`exact_amount_available=false`; it never authorizes an order, exact amount, or
automatic trade.

The personal MVP routes its five common questions as follows:

- latest news: `decision route --action fact_research`, then `news recent`;
- market context or a direction-to-buy question: route `fact_research` and `buy_or_add` when purchase intent exists, then `market overview`; current missing dimensions can require a direction abstention;
- named candidate: route `fact_research` and `buy_or_add`, resolve names to one unique confirmed code first, refresh bounded evidence outside the shortlist command, then use `fund shortlist` for exactly 2-5 owner-supplied codes; this remains candidate research, not a suitability approval;
- held-fund daily review: first distinguish partial reduction from full exit, use the matching `fund brief` action, then `fund intelligence` and thesis review; this cannot time a sale;
- portfolio diagnosis: `status -> sync portfolio -> portfolio diagnose`; refresh stale or missing held-fund disclosures separately when broader observed coverage is needed.

Read-only browsing may be added as visibly separate transient
`external_context`. It must retain its own URLs and dates and cannot strengthen
KunJin's persisted evidence state, source tier, conflict state, or action
authorization. Complete cross-source opposition detection is not implemented;
an empty conflict list means only that no conflict was found inside the bounded
authenticated sources.

Deferred from this personal MVP are full valuation/fundamental analysis,
complete D2 look-through/correlation/stress testing, D3 exact amount and mature
channel authorization, mature Phase E automatic monitoring/sell timing, and
broad official adapters. Privacy, read-only operation, no automatic trading,
and fail-closed `insufficient_data` remain mandatory.

## Pragmatic Portfolio Diagnosis (Phase 3)

After a current portfolio synchronization, run one local evidence projection:

```bash
.venv/bin/kunjin --json portfolio diagnose
.venv/bin/kunjin --json portfolio diagnose --candidate 519755
```

The first command reports value-basis concentration, manager and benchmark-text
relationships, share-class siblings, and observed disclosed-holdings overlap.
The second accepts one user-supplied candidate and reports only its observed
duplication or evidence gaps against the current holdings. Missing NAV,
identity, manager, benchmark, or quarterly holdings remains unknown; it is
never converted to zero overlap or a diversification claim.

This is an on-demand local projection and performs no network refresh. Preserve
included and omitted fund codes, report dates, publication dates, conflicts,
warnings, and both coverage states. The result always remains
`action_maturity=evidence_only`, `action_authorized=false`, and
`exact_amount_available=false`. It does not satisfy complete D2, D3, buy or add,
hold, reduce, or exit, or exact amount gates. Legacy `portfolio analyze` and
`portfolio overlap` remain available for lower-level inspection.

## Bounded Candidate Shortlist (Phase 4)

After resolving every name to one unique confirmed fund code, compare exactly
2-5 owner-supplied codes with the local deterministic command:

```bash
.venv/bin/kunjin --json fund shortlist 000001 000002
```

For a current personal comparison, use this sequence:

```text
resolve names to one unique confirmed code first -> status
-> current portfolio sync when needed
-> bounded evidence refresh outside the shortlist command
-> fund shortlist CODE1 CODE2 [...]
```

The command preserves owner input order only as identity; the conditional
shortlist is unordered and has no universal score or winner. It compares
authenticated metric-specific tradeoffs, common aligned formal-NAV intervals,
manager and fee evidence, disclosed holdings overlap, D1 product evidence, and
the observed impact on the current portfolio. Every date, source tier, aligned
NAV interval, D1 state, coverage value, conflict, warning, missing-evidence
code, and stable reason code remains part of the evidence rather than being
collapsed into a rank.

The result is amount-free and not a buy signal. It always retains
`action_maturity=evidence_only`, `action_authorized=false`,
`exact_amount_available=false`, and `automatic_trade=false`; it does not choose
a channel, authorize an order, or place a trade. A held candidate does not gain
an implied add amount. Missing or stale facts remain unknown and may reduce the
result to `relative_tradeoffs_only`, `not_comparable`, or `insufficient_data`.

Synchronize stale or missing profile, holdings, formal-NAV, peer, or D1 evidence
with existing bounded commands outside the shortlist command, then rerun it.
Never develop a source adapter during the query. An unsupported source lowers
the evidence state and becomes an explicit manual-supplementation gap instead
of starting an unbounded retry or adapter-development loop.

## Owner Research Scope And Readiness (Phase 4.1)

When the owner has no fund code yet, form a bounded educational research scope
from an objective, horizon, and product category. When the owner later supplies
two to five confirmed codes, inspect the stored comparison evidence without
network access or database writes:

```bash
.venv/bin/kunjin --json fund research-scope
.venv/bin/kunjin --json fund research-scope \
  --objective learning \
  --horizon long_term \
  --product-category broad_index
.venv/bin/kunjin --json fund shortlist-readiness 000001 000002
```

Research scope is educational and amount-free. Phase B/C may annotate or block
a risk-increasing conclusion, but never filter, narrow, or erase fact research
or the research scope. The public residual state is
`candidate_formation.status=research_scope_only` and
`candidate_formation.candidate_code_discovery=not_implemented`: KunJin still
cannot take a beginner from no code to two-to-five codes automatically. Phase
4.1 adds neither market direction nor candidate-code discovery.

Shortlist readiness is a local snapshot, not a refresh engine or
recommendation. It preserves component evidence and returns only existing
commands that may close a gap. For one explicit two-to-five-code request, run
initial readiness once, source status once per code, only the returned actions
at most once in dependency order, and final readiness once. Never use
`--force`, automatically retry, continue in the background, or develop an
adapter during the request. Return a partial terminal result when gaps remain.
Every legacy command keeps its own runtime and source boundary; in particular,
`sync fund` and `sync fund-documents` are not inside the Phase 0 90/480-second
budget.

All results remain `action_maturity=evidence_only`,
`action_authorized=false`, `exact_amount_available=false`, and
`automatic_trade=false`. With no real owner candidates, acceptance must retain
`owner_candidate_state=owner_candidates_unavailable` and
`financial_usability=not_yet_testable`; engineering subjects cannot be
relabelled as purchase candidates or proof of financial usability.

## Requirements

- macOS
- Python 3.9 or newer
- Yangjibao app for QR authorization
- Apple Vision and `/usr/bin/swift` for local screenshot OCR
- Docker Desktop only when the optional isolated legacy Word converter is
  explicitly provisioned

The runtime has two bounded non-standard-library security dependencies:
`cryptography>=43,<46` provides AES-256-GCM encryption for the personal
financial profile, and `pypdf>=5,<6` parses official PDF disclosures without
OCR or script execution. The optional `qrcode` package improves terminal QR
rendering but is not required by the storage or analytics code.

## Offline Installation

The system Python on this Mac uses an older packaging toolchain. The verified
offline-compatible setup is:

```bash
cd /Users/yanzihao/KunJin
python3 -m venv .venv
.venv/bin/pip install 'cryptography>=43,<46' 'pypdf>=5,<6'
.venv/bin/python setup.py develop
.venv/bin/kunjin --json version
```

When PyPI access is available, install terminal QR rendering with:

```bash
.venv/bin/pip install 'qrcode>=7.4,<9'
```

## Commands

```bash
.venv/bin/kunjin --json version
.venv/bin/kunjin --json auth status
.venv/bin/kunjin auth login yangjibao
.venv/bin/kunjin --json auth revoke yangjibao
.venv/bin/kunjin profile edit
.venv/bin/kunjin --json profile status
.venv/bin/kunjin --json profile history
.venv/bin/kunjin suitability assess
.venv/bin/kunjin --json suitability assess
.venv/bin/kunjin --json suitability status
.venv/bin/kunjin --json suitability history
.venv/bin/kunjin allocation ranges
.venv/bin/kunjin --json allocation ranges
.venv/bin/kunjin --json allocation status
.venv/bin/kunjin --json allocation history
.venv/bin/kunjin --json allocation policy
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json status
.venv/bin/kunjin --json portfolio show
.venv/bin/kunjin --json portfolio analyze
.venv/bin/kunjin --json portfolio overlap
.venv/bin/kunjin --json portfolio diagnose
.venv/bin/kunjin --json portfolio diagnose --candidate 519755
.venv/bin/kunjin --json fund research-scope
.venv/bin/kunjin --json fund research-scope --objective learning --horizon long_term --product-category broad_index
.venv/bin/kunjin --json fund shortlist-readiness 000001 000002
.venv/bin/kunjin --json fund brief 519755 --action continue_holding --mode rapid
.venv/bin/kunjin --json decision route --mode rapid --action fact_research
.venv/bin/kunjin --json decision route --mode rapid --action fact_research --action buy_or_add
.venv/bin/kunjin --json news recent --window recent --mode rapid
.venv/bin/kunjin --json market overview --window recent --mode rapid
.venv/bin/kunjin --json fund intelligence 519755 --window recent --mode rapid
.venv/bin/kunjin --json sync fund 017811
.venv/bin/kunjin --json fund research 017811
.venv/bin/kunjin --json sync fund-profile 017811
.venv/bin/kunjin --json sync fund-holdings 017811
.venv/bin/kunjin --json fund profile 017811
.venv/bin/kunjin --json fund fees 017811
.venv/bin/kunjin --json fund holdings 017811
.venv/bin/kunjin --json fund holdings 017811 --period 2026-06-30
.venv/bin/kunjin --json fund announcements 017811
.venv/bin/kunjin --json sync fund-documents 017811
.venv/bin/kunjin --json fund classify 017811
.venv/bin/kunjin --json fund classification 017811
.venv/bin/kunjin --json fund classification-history 017811
.venv/bin/kunjin --json fund classification-evidence 017811
.venv/bin/kunjin --json fund classification-policy
.venv/bin/kunjin --json sync fund-peers 519755
.venv/bin/kunjin --json sync fund-peers 519755 --candidate 000001
.venv/bin/kunjin --json fund peers 519755
.venv/bin/kunjin --json fund compare 519755 000001
.venv/bin/kunjin --json sync market
.venv/bin/kunjin --json market sectors
.venv/bin/kunjin --json thesis add 017811 \
  --reason "AI行业盈利改善" \
  --horizon "12个月" \
  --invalidation "持续落后基准且风格漂移"
.venv/bin/kunjin --json thesis review 017811
.venv/bin/kunjin --json report weekly
.venv/bin/kunjin --json sync daily
```

`auth login` is interactive and intentionally rejects JSON mode. The token is
saved directly in macOS Keychain and is never returned in command output.

## Personal Financial Profile (Phase A)

Phase A stores and versions a personal financial profile. Enter exact income,
debt, reserve, asset, goal, and loss-budget values only through the local
interactive terminal:

```bash
.venv/bin/kunjin profile edit
.venv/bin/kunjin --json profile status
.venv/bin/kunjin --json profile history
```

Do not paste exact financial values into Codex chat. `profile status` and
`profile history` expose metadata only, so their JSON output can be inspected
without revealing the encrypted values.

The profile payload is encrypted with AES-256-GCM before it is stored in SQLite.
Its encryption key is stored separately in macOS Keychain under service
`com.kunjin.profile-encryption` and account `v1`. Losing that Keychain key makes
the existing encrypted profile unavailable with `encrypted_profile_unavailable`;
KunJin does not reveal, decrypt, or reset the old profile, and does not silently
create a replacement key while trying to read it.

Profile presence is storage readiness, not suitability approval. Phase A does
not calculate suitability, asset allocation, purchase approval, or purchase
amounts.

## Suitability And Financial Safety (Phase B)

Run the four Phase B commands as follows:

```bash
.venv/bin/kunjin suitability assess
.venv/bin/kunjin --json suitability assess
.venv/bin/kunjin --json suitability status
.venv/bin/kunjin --json suitability history
```

`suitability assess` without `--json` is the explicit local exact view. It may
show derived reserve and monthly-capacity amounts in the terminal. Do not paste
that output into Codex chat. `--json suitability assess` performs and persists
the same deterministic assessment but returns amount-free metadata, stable
reason codes, counts, booleans, and dates. JSON status and history are also
amount-free; exact derived results are encrypted at rest.

An assessment is fresh for at most 24 hours and never beyond the active
profile's validity. A changed, replaced, expired, missing, or unauthenticated
profile or assessment cannot reuse an earlier successful result.

Phase B has three financial states:

- `blocked`: one or more financial-safety rules failed; inspect every returned
  reason code and address the condition locally before proceeding.
- `constrained`: no hard block applies, but near-term commitments or the
  confirmed monthly ceiling restrict available capacity.
- `ready_for_allocation`: the Phase B safety foundation passed and may proceed
  to Phase C allocation-range analysis.

All three states still have capability `research_only`. `ready_for_allocation`
is not a buy recommendation. Phase B does not calculate an allocation, classify
a fund, or approve an amount. Directional and position-size requests remain
`research_only` until later phases pass. KunJin therefore does not claim 90%
coverage of the beginner fund-purchase workflow.

## Transparent Allocation Ranges (Phase C)

Phase C is a strict continuation of Phase B, not a bypass. For a directional or
position-size question, first run `--json suitability assess`. A `blocked`
result stops the workflow. Only `constrained` or `ready_for_allocation` may
proceed to `--json allocation ranges`. Missing, stale, mismatched, tampered, or
unauthenticated Phase B evidence fails closed.

Phase C uses three abstract layers only:

- `protected_cash`, with a fixed 0% stress loss.
- `high_quality_fixed_income`, with a fixed 10% stress loss.
- `diversified_equity`, with a fixed 50% stress loss.

These are policy abstractions, not classifications of real funds. In particular,
a bond fund, pure-debt fund, or fixed-income-plus fund is not automatically
`high_quality_fixed_income`; product classification remains Phase D work.

The permitted region is the intersection of transparent inequalities. Equity
is capped independently by each goal or obligation horizon, the loss-amount and
drawdown budgets, behavioral willingness, and financial stability. The maximum
equity ceiling is not a target, minimum, recommended contribution mix, or buy
amount. Zero equity remains feasible. The monthly discretionary ceiling is also
an upper bound, not a recommendation.

Every allocation requires a declared purpose and date. Phase C uses the earliest
eligible positive-gap goal as the residual-capital horizon; without one it
returns `allocation_horizon_missing`. Goal and obligation funding is shown in
zero-return states, so optimistic expected returns are never used to make a goal
appear feasible.

Protected cash, operating cash, and short-term assigned capital cannot be reused
as investable capital. If protected liquid claims exceed the declared liquid
protection assets, the protected-capital overlap block
`protected_capital_overlap_or_shortfall` prevents a range. Asset fields must be
mutually exclusive, although KunJin cannot independently prove that the user's
real accounts do not overlap.

Use these Phase C commands:

```bash
.venv/bin/kunjin allocation ranges
.venv/bin/kunjin --json allocation ranges
.venv/bin/kunjin --json allocation status
.venv/bin/kunjin --json allocation history
.venv/bin/kunjin --json allocation policy
```

The non-JSON `allocation ranges` command is the owner's exact local view and may
show CNY amounts and private goal or obligation names. Do not paste it into chat.
The JSON commands are amount-free views containing percentages, counts, dates,
stable codes, inequalities, and binding constraints. Exact results are encrypted
at rest with AES-256-GCM. Assessments bind to the authenticated Phase B snapshot
and active profile, are fresh for at most 24 hours and never beyond profile
validity, and retain immutable authenticated history.

Every Phase C state remains `research_only`. Phase C does not classify real
funds, compare current holdings with the region, choose a target point, or issue
a trade or purchase amount. Those portfolio-construction and pre-purchase
controls remain Phase D and Phase E, so Phase C does not complete 90% of the
beginner purchase workflow.

## Real Fund Risk Classification (Phase D1)

Phase D1 turns current, source-traceable public-product evidence into a
deterministic product family, risk bucket, and portfolio-role eligibility. Its
six commands are fact-only, amount-free, and may run without a Phase B or Phase
C success state:

```bash
.venv/bin/kunjin --json sync fund-documents 017811
.venv/bin/kunjin --json fund classify 017811
.venv/bin/kunjin --json fund classification 017811
.venv/bin/kunjin --json fund classification-history 017811
.venv/bin/kunjin --json fund classification-evidence 017811
.venv/bin/kunjin --json fund classification-policy
```

Every result has capability `research_only`. Evidence status has these narrow
meanings:

- `verified`: all critical evidence required by Policy V1 is current,
  authenticated, and internally consistent.
- `partial`: some useful evidence exists, but required coverage is incomplete.
- `conflicted`: current evidence contains a material unresolved contradiction.
- `stale`: critical evidence exists but is no longer current.
- `unclassified`: the available evidence cannot support a Policy V1 class.

D1.1-C is implemented with bounded newest-per-kind selection for annual,
semiannual, and quarterly reports. A report kind is selected only when it has
exactly one unique newest candidate. No candidate returns
`current_periodic_candidate_missing`; multiple distinct candidates tied at the newest publication time return
`current_periodic_candidate_conflict`. If the selected report fails retrieval or
parsing, or the newest state is missing or conflicted, current classification
does not fall back to an older report. These selection codes are audit bindings
only; they do not replace Policy V1 financial reason, conflict, freshness, or
missing-evidence codes.

The selection and every terminal candidate-run outcome are persisted before
classification and authenticated by classification input Manifest V3. New
classifications use the active parser v4 provenance; historical Manifest V1 and
V2 records remain readable but cannot authorize a new current result. Legal
documents supply mandate facts, while periodic reports supply current
observations. KunJin does not infer one category from the other. A top-ten
disclosure is incomplete evidence of the whole portfolio, so omitted holdings,
issuers, ratings, industries, or exposures are never treated as zero.

Unsupported and missing are deliberately different outcomes.
`unsupported_product_family` means current official evidence identifies a
product family outside D1 Policy V1; it is a successful factual result with an
`unclassified` evidence status. `critical_evidence_missing` means the product
may be supported, but evidence required to classify it is unavailable. A
technical download, parse, policy, or storage failure is neither outcome and
returns a nonzero error instead.

Failed `sync fund-documents` items retain the existing `error_code` and may add
allowlisted `failure_stage` and `failure_reason`. These values explain the
technical boundary only. They do not prove a product family, risk bucket,
portfolio role, suitability result, allocation, or purchase direction, and are
not a buy signal.

The labels do not cross phase boundaries. `cash_like_candidate` is a public
product risk classification and is not the owner's Phase C `protected_cash`.
Likewise, `core_eligible` means only that the fund passed D1's product-evidence
rules; it is not a recommendation. Never place a real fund into a Phase C layer
from its name, platform label, historical volatility, or a D1 label alone.

A D1 `verified` result is not suitability, not an allocation, not a buy signal,
and not a 90% beginner-help claim. It does not evaluate the owner's portfolio or
authorize buy, hold, add, reduce, sell, rebalance, or position-size output. The
separate Phase 1 `fund brief` implements only the minimum D2 subset described
above; complete D2 portfolio construction, D3 product-selection and
pre-purchase checks, and Phase E continuous monitoring are not implemented.
Every D1 classification result remains `research_only`, with no direction or
amount authorized.

The official-domain coverage is audited and finite. A missing manager/index-provider
adapter can leave an otherwise common supported fund `partial` or `unclassified`;
platform mirrors cannot be promoted to official evidence to avoid that result.
For stale or missing evidence, refresh `sync fund-profile`, `sync fund-holdings`,
and `sync fund-documents` as applicable, rerun `fund classify`, then inspect
`fund classification-evidence`. If the official publisher is not registered,
add and test the exact manager/index-provider adapter before relying on a new
classification. This is an evidence-correction workflow, not a suggestion to
buy or sell.

### Optional legacy Word conversion (D1.1-B)

Some official periodic reports use the legacy OLE Word container. KunJin can
parse those reports only through the separately reviewed personal-use Docker
image documented in `containers/legacy-doc/README.md`. Explicit setup may use
the network to build a pinned `linux/arm64` image. Normal synchronization never
pulls or builds an image: it invokes only the allowlisted local SHA-256 image ID
with `--pull=never` and `--network=none`.

The setup script must be invoked directly rather than through a symlink. It
resolves parent-directory symlinks to the physical repository and authenticates
the reviewed, non-symlink Dockerfile by its fixed SHA-256 before each build. The
safe setup JSON records that checksum as `dockerfile_sha256`.

There is no host `textutil` fallback and no host LibreOffice fallback. The
Dockerfile's `USER 65532:65532` is deliberately overridden at runtime by
`--user=<host-uid>:<host-gid>` so the non-root converter can create a
bind-mounted output owned by the current host user. The conversion stdout and stderr are never captured or exposed; only private bounded metadata queries may
be captured to verify the exact image and cleanup state.

After reviewed provisioning, export the exact image ID printed by the build
script and inspect only safe readiness metadata:

```bash
.venv/bin/kunjin --json fund converter-status
```

Conversion success is not financial evidence. Converted HTML must still pass
the normal official identity, document-kind, report-period, active-content,
ambiguity, and fact checks. D1.1-C now applies bounded current-report selection,
Manifest V3 authentication, and parser v4 fact extraction; it does not by itself
implement the Phase 1 minimum D2 subset, complete D2, D3, or Phase E, and the
classification result remains `research_only`.

## Personal Transaction Ledger

Use Yangjibao for the current position observation and an Alipay payment-detail
screenshot for the payment fields visible in that image. Start with this flow:

```bash
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json ledger import /absolute/path/to/alipay.jpg --fund-code 519755
.venv/bin/kunjin --json ledger drafts
.venv/bin/kunjin --json ledger confirm 1
.venv/bin/kunjin --json ledger transactions --fund-code 519755
.venv/bin/kunjin --json ledger reconcile --fund-code 519755
```

Inspect the draft and explicitly confirm every relevant value before running
`ledger confirm`; replace `1` with the returned draft ID. Supply `--fund-code`
only when the code is known and confirmed by the user. Corrections can be made
at confirmation time, for example:

```bash
.venv/bin/kunjin --json ledger confirm 1 --field fund_code=519755
```

OCR uses Apple Vision locally through the bundled Swift helper. KunJin does not
send screenshots to a cloud OCR service. Import copies the image into
`~/.local/share/kunjin/imports/`, which is maintained as a private local
directory. To remove KunJin's managed copy after import:

```bash
.venv/bin/kunjin --json ledger document delete 1
```

Deletion affects only the managed copy; it does not delete the original image.
Confirmed transaction records are immutable and remain available for audit and
reconciliation.

Evidence labels have deliberately narrow meanings:

- `transaction_confirmed`: a field such as payment amount or order time is
  visibly supported by the imported payment screenshot.
- `user_confirmed`: the user supplied or explicitly confirmed the field.
- `position_inferred`: a value is calculated from a Yangjibao position
  observation rather than read from a transaction document.

An Alipay payment-detail screenshot is not a fund transaction confirmation when
it does not show confirmed shares, NAV, fees, or settlement details. Reconciliation
may compare confirmed cash flow with an inferred position cost, but that inferred
cost is not an exact reconstructed purchase lot or authoritative cost basis.

## Runtime Data

```text
~/.local/share/kunjin/kunjin.db
~/.local/share/kunjin/snapshots/
~/.local/share/kunjin/imports/
~/.local/state/kunjin/logs/
```

Tests override these directories and never use live credentials.

## Phase-one Analysis

- Current value based on shares and formal NAV.
- Clearly labeled fallback to intraday estimated NAV.
- Fund-level weights.
- Herfindahl-Hirschman concentration index.
- Largest-position share.
- Observed profit only when every position has coverage.
- Explicit `insufficient_data` results when required NAV is missing.

## Fund and Market Research

- Formal-NAV 30/90/365-day returns when sufficient history exists.
- Annualized daily volatility, maximum drawdown, trough, and recovery dates.
- Sourced fund identity, A/C share-class relationships, manager tenure, fee
  schedules, size history, benchmark descriptions, quarterly disclosed
  holdings, raw industry-exposure source records, and announcements. The
  controlled production taxonomy is currently empty, so authenticated current
  industry-observation coverage is zero; raw industry names or weights are not
  promoted to current facts.
- Exact report and publication dates, source URLs and tiers, section freshness,
  source failures, warnings, and conflicts in structured JSON.
- Sector daily strength, turnover observations, and advancing-stock breadth.
- Explicit warnings when required disclosure, valuation, earnings, persistent
  flows, catalysts, crowding, or news evidence is missing.

`sync fund CODE` remains the formal-NAV history command. Run `sync fund-profile
CODE` for identity, manager, fee, size, benchmark, and announcement sections;
run `sync fund-holdings CODE` for quarterly holdings and industry exposure.
Each disclosure section is synchronized independently, so a failed source does
not discard previously verified facts from other sections.

Industry synchronization currently preserves source records for research and
future taxonomy work only. Until a complete pinned official mapping is added,
KunJin fails closed instead of authenticating current largest-industry,
industry-weight, or industry-count facts.

Recent sector strength is never presented as proof that a sector is suitable to buy.

## Peer Comparison And Holdings Overlap

`sync fund-peers CODE` creates or refreshes a validated peer group with at most
20 members. Automatic candidate discovery uses the Eastmoney static fund
directory as tier-2 enumeration evidence only; directory order and platform
rankings are never treated as merit. `sync daily` refreshes only peer groups that
were already created, so holding a fund does not automatically enroll it in peer
screening.

`fund peers CODE` and `fund compare CODE...` calculate deterministic orderings
for individual supported metrics rather than a universal score. Formal-NAV
return, volatility, and drawdown comparisons retain their common aligned dates;
manager-team windows retain the exact dates on which the current team was in
place. A/C sibling shares may reuse the same disclosed holdings relationship,
but their fees and formal-NAV histories remain separate comparisons.

Fund and portfolio overlap reports use the common securities visible in the
latest usable quarterly disclosures. They explicitly label this as
`top10_disclosed_overlap`, retain report periods and coverage, and do not treat
missing or stale holdings as zero exposure. These reports are research evidence,
not automatic buy or sell instructions.

## Learning Journal and Reports

A thesis requires a reason, expected horizon, and invalidation condition. Weekly
reports combine the latest stored portfolio, held-fund NAV research, sector breadth,
and questions that distinguish decision quality from outcome quality.

## Daily Scheduling

Generate a weekday 18:30 LaunchAgent plist after Yangjibao authorization works:

```bash
python3 scripts/install_launchd.py
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kunjin.daily-sync.plist
```

The installer creates the plist but does not load it automatically.

## Current Limitations

- Yangjibao is not an authoritative Alipay transaction ledger.
- Exact subscription lots, fund transaction confirmations, dividends, and
  redemption fees remain unavailable unless the imported evidence actually
  contains those fields or a future authoritative source provides them.
- Full valuation and earnings research, persistent capital flows, and continuous
  full-history news crawling are not implemented.
- Broad financial-media ingestion, complete D2 look-through and stress testing,
  D3 exact-amount/channel authorization, and Phase E mature monitoring or sell
  timing are not implemented. Phase 4 provides only an unordered bounded
  candidate shortlist.
- Peer reports do not provide a universal composite score or automatic trade;
  their metric-specific orderings require the user to choose a horizon and weigh
  opposing evidence.
- Freshness currently understands weekdays but not exchange holiday calendars.
- The Yangjibao browser-plugin interface is unofficial and may change.
- Public fund and sector endpoints are also unofficial public interfaces and may change.

See the approved [design](docs/superpowers/specs/2026-07-11-kunjin-fund-research-assistant-design.md)
and [phase-one plan](docs/superpowers/plans/2026-07-11-kunjin-phase-1-portfolio-foundation.md).
