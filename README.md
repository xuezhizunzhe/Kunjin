# KunJin

KunJin is a local fund research and personal evidence-ledger foundation operated
through Codex. Its Yangjibao integration is read-only; ledger commands write only
to KunJin's local SQLite database and private import directory.

Phase one synchronizes personal account and holding observations from Yangjibao,
stores redacted snapshots in SQLite, and calculates reproducible portfolio totals
and concentration metrics. The current build also supports formal-NAV fund risk
research, A-share sector strength/breadth, investment theses, weekly reports, and
a weekday post-close synchronization job.

KunJin does not log in to or operate Alipay, modify Yangjibao data, place fund
orders, or produce automatic trading instructions.

## Requirements

- macOS
- Python 3.9 or newer
- Yangjibao app for QR authorization
- Apple Vision and `/usr/bin/swift` for local screenshot OCR

Phase-one runtime uses the Python standard library. The optional `qrcode` package
improves terminal QR rendering but is not required by the storage or analytics code.

## Offline Installation

The system Python on this Mac uses an older packaging toolchain. The verified
offline-compatible setup is:

```bash
cd /Users/yanzihao/KunJin
python3 -m venv .venv
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
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json status
.venv/bin/kunjin --json portfolio show
.venv/bin/kunjin --json portfolio analyze
.venv/bin/kunjin --json sync fund 017811
.venv/bin/kunjin --json fund research 017811
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
- Sector daily strength, turnover observations, and advancing-stock breadth.
- Explicit warnings when benchmark, manager, fee, holdings, valuation, earnings,
  persistent flows, catalysts, crowding, or news evidence is missing.

Recent sector strength is never presented as proof that a sector is suitable to buy.

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
- Manager/fee/holding history, benchmark comparison, full valuation/fundamental
  sector research, peer screening, and automatic news persistence are not complete.
- Freshness currently understands weekdays but not exchange holiday calendars.
- The Yangjibao browser-plugin interface is unofficial and may change.
- Public fund and sector endpoints are also unofficial public interfaces and may change.

See the approved [design](docs/superpowers/specs/2026-07-11-kunjin-fund-research-assistant-design.md)
and [phase-one plan](docs/superpowers/plans/2026-07-11-kunjin-phase-1-portfolio-foundation.md).
