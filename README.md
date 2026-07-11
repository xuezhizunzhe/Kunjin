# KunJin

KunJin is a local, read-only fund research foundation operated through Codex.

Phase one synchronizes personal account and holding observations from Yangjibao,
stores redacted snapshots in SQLite, and calculates reproducible portfolio totals
and concentration metrics.

KunJin does not log in to or operate Alipay, modify Yangjibao data, place fund
orders, or produce automatic trading instructions.

## Requirements

- macOS
- Python 3.9 or newer
- Yangjibao app for QR authorization

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
```

`auth login` is interactive and intentionally rejects JSON mode. The token is
saved directly in macOS Keychain and is never returned in command output.

## Runtime Data

```text
~/.local/share/kunjin/kunjin.db
~/.local/share/kunjin/snapshots/
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

## Current Limitations

- Yangjibao is not an authoritative Alipay transaction ledger.
- Exact subscription lots, transaction confirmations, dividends, and redemption
  fees are unavailable unless a future source provides them.
- Public-fund research, manager/fee/holding history, market sectors, news, and
  weekly evidence reports are later phases.
- Freshness currently understands weekdays but not exchange holiday calendars.
- The Yangjibao browser-plugin interface is unofficial and may change.

See the approved [design](docs/superpowers/specs/2026-07-11-kunjin-fund-research-assistant-design.md)
and [phase-one plan](docs/superpowers/plans/2026-07-11-kunjin-phase-1-portfolio-foundation.md).

