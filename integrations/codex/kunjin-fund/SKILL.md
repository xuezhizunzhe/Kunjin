---
name: kunjin-fund
description: Use KunJin as the single Codex entry point for personal fund work. Trigger when the user asks to synchronize Yangjibao, inspect or analyze current fund holdings, explain portfolio profit or concentration, research a fund code from formal NAV, inspect current A-share sector strength, check data freshness, or revoke Yangjibao authorization. Clearly distinguish verified facts, deterministic calculations, recent strength, and unsupported evidence instead of inventing data.
---

# KunJin Fund

Use the local, read-only KunJin CLI. Keep calculations in KunJin and use Codex to explain the structured result in beginner-appropriate language.

## Locate the CLI

Use this project root:

```text
/Users/yanzihao/KunJin
```

Prefer the installed command when it exists:

```bash
/Users/yanzihao/KunJin/.venv/bin/kunjin --json version
```

Otherwise use the zero-dependency source command:

```bash
PYTHONPATH=/Users/yanzihao/KunJin/src python3 -m kunjin.cli --json version
```

Set `PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache` if the execution environment cannot write the default Python cache.

## Workflow

1. Run `--json status` before portfolio work.
2. For questions containing today, current, latest, or sync, run `--json sync portfolio` before analysis.
3. If authorization is missing, run `auth login yangjibao` without `--json`; tell the user to scan the local QR. Never expose the returned token.
4. Run `--json portfolio show` to inspect normalized positions.
5. Run `--json portfolio analyze` for totals, weights, HHI, largest-position share, profit coverage, and missing-data warnings.
6. Explain facts, deterministic calculations, limitations, and possible interpretations separately.
7. For a named fund, run `--json sync fund CODE` before `--json fund research CODE` when latest data is requested.
8. For current market form, run `--json sync market` before `--json market sectors`.
9. Record a decision thesis only when the user provides a reason, horizon, and invalidation condition.
10. Use `--json report weekly` for a combined learning-oriented summary.

## Commands

```bash
kunjin --json auth status
kunjin auth login yangjibao
kunjin --json auth revoke yangjibao
kunjin --json sync portfolio
kunjin --json status
kunjin --json portfolio show
kunjin --json portfolio analyze
kunjin --json sync fund 017811
kunjin --json fund research 017811
kunjin --json sync market
kunjin --json market sectors
kunjin --json sync daily
kunjin --json thesis add 017811 --reason "..." --horizon "..." --invalidation "..."
kunjin --json thesis list --fund-code 017811
kunjin --json thesis review 017811
kunjin --json report weekly
```

Replace `kunjin` with the full source command when the virtualenv command is unavailable.

## Evidence Rules

- Treat formal NAV and intraday estimated NAV as different data types.
- Preserve the reported `as_of`, `freshness`, `warnings`, and `errors` in the explanation.
- Call portfolio metrics deterministic calculations only when KunJin returns that evidence level.
- Treat Yangjibao values as observations, not authoritative Alipay transaction confirmations.
- Do not infer purchase lots, fees, dividends, or cost basis when fields are unavailable.
- Do not turn recent performance into a buy or sell instruction.
- State `insufficient_data` plainly when KunJin cannot support a conclusion.

## Latest News Workflow

When the user asks why a fund or sector moved, first use KunJin for the dated
portfolio/NAV/sector facts. Then use available read-only browsing or search tools
for official fund-company announcements, exchange notices, regulator releases,
and dated company disclosures. Use established financial media only as secondary
context. Cite the source and publication time, separate reported events from
market commentary, and label any causal link as `reasoned_inference` unless the
evidence directly establishes it. Do not persist news in KunJin until the audited
news adapter exists.

## Safety Boundaries

- Use only KunJin's allowlisted read-only Yangjibao operations.
- Never call a Yangjibao endpoint manually or downgrade HTTPS to HTTP.
- Never print, log, store, or request the Keychain token.
- Never operate Alipay or modify Yangjibao holdings.
- Never add automatic trading instructions.

## Unsupported Requests

Fund manager/fee/holding analysis, benchmark comparison, valuation, earnings, persistent capital flows, automated news ingestion, and candidate-fund peer comparison are not implemented yet. Fund research currently covers formal-NAV performance and risk; market research currently covers sector strength and breadth. Weekly reports explicitly mark missing news and causal evidence. Identify missing evidence and do not substitute guesses, platform rankings, or unverified snippets.
