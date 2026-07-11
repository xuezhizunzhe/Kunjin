---
name: kunjin-fund
description: Use KunJin as the single Codex entry point for personal fund work. Trigger when the user asks to import an Alipay payment screenshot, inspect or reconcile the personal ledger, synchronize Yangjibao, analyze current fund holdings, research a fund code from formal NAV or sourced disclosures, inspect current A-share sector strength, check data freshness, or revoke Yangjibao authorization. Clearly distinguish verified facts, user-confirmed fields, deterministic calculations, inferred position values, recent strength, and unsupported evidence instead of inventing data.
---

# KunJin Fund

Use the local KunJin CLI. Yangjibao access is read-only; personal-ledger writes
stay in KunJin's local SQLite database and private import directory. Keep
calculations in KunJin and use Codex to explain the structured result in
beginner-appropriate language.

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
2. When the user provides an Alipay payment screenshot, run `--json ledger import IMAGE` with `--fund-code CODE` only if the user supplied or confirmed that code.
3. Show the extracted amount, order time, fund code, confidence, and field evidence. Never expose the managed screenshot path or unrelated OCR text.
4. Do not run `ledger confirm` until the user explicitly confirms the draft values. A prior general request to import or analyze the screenshot is not confirmation.
5. For current reconciliation, run `--json sync portfolio` before `--json ledger reconcile --fund-code CODE`; the reconcile command does not synchronize by itself.
6. Explain `transaction_confirmed`, `user_confirmed`, and `position_inferred` separately. Never call a payment screenshot a fund confirmation when shares, NAV, fees, or settlement details are absent.
7. For questions containing today, current, latest, or sync, run `--json sync portfolio` before portfolio analysis.
8. If authorization is missing, run `auth login yangjibao` without `--json`; tell the user to scan the local QR. Never expose the returned token.
9. Run `--json portfolio show` to inspect normalized positions.
10. Run `--json portfolio analyze` for totals, weights, HHI, largest-position share, profit coverage, and missing-data warnings.
11. Explain facts, deterministic calculations, limitations, and possible interpretations separately.
12. For a named fund's latest formal-NAV performance or risk, run `--json sync fund CODE` before `--json fund research CODE`.
13. Before answering about identity, share classes, managers, fees, size, benchmark, or announcements, inspect the relevant `freshness.sections` returned by `fund profile`, `fund fees`, or `fund announcements`. Run `--json sync fund-profile CODE` first when any required section is stale, missing, unknown, or unavailable.
14. Before answering about quarterly holdings or industry exposure, inspect `fund holdings CODE`. Run `--json sync fund-holdings CODE` first when holdings are stale, missing, unknown, or a newer report window is due. Use `--period YYYY-MM-DD` when the user asks about an exact reporting period.
15. Preserve exact report dates, publication dates, source URLs, source tiers, conflicts, warnings, and missing evidence in the answer. A successful section must not conceal a failed or stale section.
16. For current market form, run `--json sync market` before `--json market sectors`.
17. For latest peer questions, run `--json fund peers CODE` and inspect its status, data dates, coverage, warnings, errors, and stored-group freshness. Run `--json sync fund-peers CODE` when the group is missing or stale, then read it again.
18. For an explicit latest comparison, synchronize profile, holdings, and formal NAV for every code before running `--json fund compare CODE1 CODE2`.
19. For current portfolio overlap, run `--json sync portfolio`, refresh stale held-fund holdings with `--json sync fund-holdings CODE`, then run `--json portfolio overlap`.
20. Preserve aligned NAV dates, manager-team dates, metric-specific orderings, disclosure scope, coverage, source tier, warnings, and errors. Never turn platform directory order into merit.
21. Provide a buy/hold/add/reduce/sell interpretation only when the user explicitly requests one. Include opposing evidence, the relevant horizon, invalidation conditions, and data limitations; never operate an account or place a trade.
22. Record a decision thesis only when the user provides a reason, horizon, and invalidation condition.
23. Use `--json report weekly` for a combined learning-oriented summary.

## Commands

```bash
kunjin --json auth status
kunjin auth login yangjibao
kunjin --json auth revoke yangjibao
kunjin --json sync portfolio
kunjin --json status
kunjin --json portfolio show
kunjin --json portfolio analyze
kunjin --json portfolio overlap
kunjin --json ledger import /absolute/path/to/alipay.jpg --fund-code 519755
kunjin --json ledger drafts
kunjin --json ledger confirm 1 --field fund_code=519755
kunjin --json ledger add --type subscription --fund-code 519755 --amount 20.00 --order-time 2026-07-04T23:11:51+08:00
kunjin --json ledger transactions --fund-code 519755
kunjin --json ledger reconcile --fund-code 519755
kunjin --json ledger document delete 1
kunjin --json sync fund 017811
kunjin --json fund research 017811
kunjin --json sync fund-profile 017811
kunjin --json sync fund-holdings 017811
kunjin --json fund profile 017811
kunjin --json fund fees 017811
kunjin --json fund holdings 017811
kunjin --json fund holdings 017811 --period 2026-06-30
kunjin --json fund announcements 017811
kunjin --json sync fund-peers 519755
kunjin --json sync fund-peers 519755 --candidate 000001
kunjin --json fund peers 519755
kunjin --json fund compare 519755 000001
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
- Treat fund-company, regulator, and exchange documents as tier-1 only when the
  publisher and domain have been validated. Clearly label Eastmoney F10 pages as
  tier-2 fallback evidence.
- Preserve manager start and end dates exactly. Never attribute a predecessor's
  return to the current manager.
- Keep fee tiers, share classes, amount conditions, and holding-period conditions
  separate. Never calculate an exact personal fee without the required transaction
  and holding-period evidence.
- Holdings are disclosed snapshots, not real-time positions. Always retain the
  report period, publication date, and disclosure scope.
- Preserve source conflicts instead of silently choosing a lower-tier value.
- Treat the candidate directory as tier-2 enumeration evidence only. Its order
  and any platform ranking are not evidence that one fund is better.
- Preserve the common formal-NAV dates for each comparable window. Do not compare
  members over silently different periods or combine metric orderings into a
  universal score.
- Treat A/C sibling fees and NAV histories separately even when their disclosed
  holdings relationship is shared.
- Describe overlap as `top10_disclosed_overlap`, retain report periods and
  coverage, and never interpret missing or stale holdings as zero exposure.
- Call portfolio metrics deterministic calculations only when KunJin returns that evidence level.
- Treat Yangjibao values as observations, not authoritative Alipay transaction confirmations.
- Treat an Alipay payment screenshot as evidence only for fields visible in the screenshot. It is not a fund confirmation document by itself.
- A fund-code hint and draft corrections supplied by the user are `user_confirmed`, not `transaction_confirmed`.
- Treat reconciliation cost derived from current value and observed profit as `position_inferred`; never present it as a reconstructed purchase lot or authoritative cost basis.
- Do not infer purchase lots, shares, NAV, fees, dividends, or cost basis when fields are unavailable.
- Do not turn recent performance into a buy or sell interpretation unless the
  user explicitly requests one and the response includes opposing evidence,
  horizon, invalidation conditions, and data limitations.
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
- Never run `ledger confirm` without explicit confirmation of the displayed draft from the user.
- Never expose a managed screenshot path. `ledger document delete` removes only KunJin's private managed copy, not the user's original image or the immutable confirmed transaction.
- Never add automatic trading instructions.

## Unsupported Requests

Valuation, earnings, persistent capital flows, and automated news ingestion are
not implemented yet. Fund research covers formal-NAV performance and risk plus
sourced identity, manager, fee, size, benchmark, quarterly holding, industry,
announcement, peer-comparison, and disclosed-overlap evidence. Market research
currently covers sector strength and breadth. Peer comparison has no universal
score or automatic trade path. Weekly reports explicitly mark missing news and
causal evidence. Identify missing evidence and do not substitute guesses,
platform rankings, or unverified snippets.
