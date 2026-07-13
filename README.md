# KunJin

KunJin is a local fund research and personal evidence-ledger foundation operated
through Codex. Its Yangjibao integration is read-only; ledger commands write only
to KunJin's local SQLite database and private import directory.

Phase one synchronizes personal account and holding observations from Yangjibao,
stores redacted snapshots in SQLite, and calculates reproducible portfolio totals
and concentration metrics. The current build also supports formal-NAV fund risk
research, sourced fund identity and disclosure research, A-share sector
strength/breadth, deterministic peer comparison and disclosed-holdings overlap,
investment theses, weekly reports, and a weekday post-close synchronization job.

KunJin does not log in to or operate Alipay, modify Yangjibao data, place fund
orders, or produce automatic trading instructions.

## Requirements

- macOS
- Python 3.9 or newer
- Yangjibao app for QR authorization
- Apple Vision and `/usr/bin/swift` for local screenshot OCR

The runtime has one bounded non-standard-library security dependency:
`cryptography>=43,<46`. It provides AES-256-GCM encryption for the personal
financial profile. The optional `qrcode` package improves terminal QR rendering
but is not required by the storage or analytics code.

## Offline Installation

The system Python on this Mac uses an older packaging toolchain. The verified
offline-compatible setup is:

```bash
cd /Users/yanzihao/KunJin
python3 -m venv .venv
.venv/bin/pip install 'cryptography>=43,<46'
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
.venv/bin/kunjin --json sync fund 017811
.venv/bin/kunjin --json fund research 017811
.venv/bin/kunjin --json sync fund-profile 017811
.venv/bin/kunjin --json sync fund-holdings 017811
.venv/bin/kunjin --json fund profile 017811
.venv/bin/kunjin --json fund fees 017811
.venv/bin/kunjin --json fund holdings 017811
.venv/bin/kunjin --json fund holdings 017811 --period 2026-06-30
.venv/bin/kunjin --json fund announcements 017811
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
  holdings, industry exposure, and announcements.
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
- Full valuation and earnings research, persistent capital flows, and automatic
  news persistence are not implemented.
- Peer reports do not provide a universal composite score or automatic trade;
  their metric-specific orderings require the user to choose a horizon and weigh
  opposing evidence.
- Freshness currently understands weekdays but not exchange holiday calendars.
- The Yangjibao browser-plugin interface is unofficial and may change.
- Public fund and sector endpoints are also unofficial public interfaces and may change.

See the approved [design](docs/superpowers/specs/2026-07-11-kunjin-fund-research-assistant-design.md)
and [phase-one plan](docs/superpowers/plans/2026-07-11-kunjin-phase-1-portfolio-foundation.md).
