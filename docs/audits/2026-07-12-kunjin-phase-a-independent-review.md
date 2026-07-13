# KunJin Phase A Independent Financial And Software Review

Date: 2026-07-12

Scope: current working tree, the approved personal-suitability design, the
Phase A implementation plan, installed CLI behavior, repository Skill copy,
privacy controls, and the beginner fund-purchase workflow. Implementation
claims were not accepted without direct inspection or fresh command evidence.

Final Phase A status after audit remediation: **technical Phase A exit gate
met; beginner purchase-workflow coverage remains 30/100 and 90% is not
reached**. The initial audit failures and their remediation are retained below
instead of being erased.

## Findings

### Resolved P1 - The configured static-check acceptance gate initially failed

The first audit run found `896` full-repository Ruff errors and `147` errors in
the then-selected Phase A subset. The failures included import ordering, line
length, unused imports, and upgrade rules that conflicted with the declared
Python 3.9 annotation style.

Remediation retained `E`, `F`, `I`, and compatible `UP` checks, explicitly
ignored only `UP006`, `UP007`, `UP035`, `UP037`, and `UP045` to preserve Python
3.9 syntax, then fixed import, unused-import, and line-length findings. The
fresh final command `.venv/bin/ruff check .` returns `All checks passed!`.

### P1 - No implemented path determines whether a beginner should buy a fund

The shipped profile commands capture and protect facts only. There is no
`suitability assess`, emergency-reserve calculation, debt gate, risk-capacity
or willingness result, goal-horizon gate, allocation range, risk bucket,
post-purchase projection, or allowed amount. Current fund and portfolio tools
can support research, but cannot safely turn that research into buy, add,
reduce, sell, or position-size direction.

This is an intentional Phase A boundary, not an implementation regression. It
is still the most important product limitation: a beginner cannot rely on the
system for a purchase decision. The Skill correctly labels such requests
`research_only`.

### Resolved P2 - `profile status` initially did not prove decryptability

The first audit found that `ProfileService.status()` read lifecycle metadata
without decrypting the active payload. A missing Keychain key or corrupt
ciphertext could therefore coexist with `state=confirmed` and
`freshness=fresh`.

Remediation now makes status load, authenticate, decrypt, decode, and validate
the active profile and cross-check its confirmation and validity metadata
before reporting freshness. Missing key or tampering returns the stable
`encrypted_profile_unavailable` error. A missing profile still returns
metadata-only `missing` without accessing Keychain.

### Resolved P2 - Invalidation reasons were initially free-form plaintext

The first audit found that `invalidation_reason` accepted any non-empty string,
which could allow future callers to place sensitive financial or household
details in plaintext metadata.

Remediation restricts invalidation to seven non-sensitive codes:
`income_change`, `debt_change`, `obligation_change`, `goal_change`,
`household_change`, `user_requested`, and `key_rotation`. Store and service
reject free-form strings, and history exposes only the normalized code.

### P2 - Real personal and live Keychain acceptance remain unverified

Tests use injected key stores and synthetic values. This audit did not enter a
real profile, create or delete a real Keychain item, or validate the ten real
personal scenarios required by Phase F. The installed macOS CLI read path was
exercised only with the current empty profile database. Terminal summary output
also intentionally displays exact values locally, so encryption does not
protect shell scrollback, screen recording, or shoulder-surfing.

## Verified Phase A Capabilities

- Immutable dataclasses validate exact CNY amounts, dates, booleans, enums, and
  nested debts, obligations, and goals. Decimal values serialize as strings,
  not binary floats.
- Profiles are encrypted before SQLite persistence with AES-256-GCM, random
  96-bit nonces, fixed associated data, and a key-derived HMAC-SHA256
  fingerprint. Tampering and missing-key decryption fail closed in tests.
- The 256-bit profile key is separated from SQLite through a dedicated macOS
  Keychain service/account contract: `com.kunjin.profile-encryption` / `v1`.
- SQLite schema version 7 stores version and lifecycle metadata, permits only
  one confirmed profile, supersedes atomically, and blocks payload mutation and
  deletion with database triggers.
- Interactive editing is local, requires explicit final confirmation, supports
  cancellation, and returns only status/version metadata to the CLI envelope.
- `profile status` and `profile history` return no financial values, nonce,
  ciphertext, or keyed fingerprint.
- Runtime directories are forced to mode `0700` by `RuntimePaths.ensure()`.
- README and the installed Skill accurately say Phase A is storage readiness,
  not suitability approval. Repository and installed Skill files are byte
  identical.

## Privacy And Security Evidence

An isolated synthetic profile used sentinel values `73129`, `84217`, and
`95311`. Searching the isolated data and state directories returned exit 1 with
no matches. Unit/integration tests also cover plaintext SQLite absence, JSON
redaction, log redaction, tampered ciphertext, malformed key material, and no
automatic key replacement during decryption.

These results prove the exercised persistence and normal-output paths do not
contain those sentinels. They do not prove resistance to a compromised user
session, terminal capture, malicious dependencies, memory inspection, backups
that include both database and Keychain, or untested future metadata fields.

## Skill Overreach Review

The Skill is appropriately conservative for Phase A:

- It forbids requesting exact financial values in chat.
- It directs exact entry to local `kunjin profile edit`.
- It treats status/history as metadata-only.
- It classifies all directional and position-size questions as
  `research_only` until a non-blocked Phase B assessment exists.
- It forbids turning `within_guardrails` or research evidence into investment
  merit, although those later guardrail commands are not implemented yet.

Residual risk is operational rather than textual: prompt rules are not a
software-enforced purchase gate. No adversarial Skill execution harness was run
in Phase A, and there is no CLI assessment result for the Skill to preserve.

## Beginner Purchase-Workflow Coverage

The rubric measures verified workflow help, not expected returns. Designed-only
Phase B-F features receive zero credit.

| Decision area | Weight | Score | State and evidence |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 2 | `verified_partial`: encrypted fact capture only; no reserve, debt, or cash-flow gate |
| Risk capacity and willingness | 10 | 1 | `verified_partial`: responses captured; no assessment or conflict result |
| Goals and investment horizon | 10 | 1 | `verified_partial`: goals captured; no horizon ceiling or feasibility check |
| Asset allocation and risk budget | 15 | 0 | `designed_only` Phase C |
| Fund-category identification | 10 | 4 | `verified_partial`: sourced type/benchmark evidence exists; no defensible portfolio risk bucket |
| Individual fund quality research | 15 | 8 | `verified_partial`: formal NAV, drawdown, manager, fee, size, benchmark, disclosure, announcement, and peer evidence; no complete attribution, bond-risk, valuation, earnings, or flow analysis |
| Portfolio overlap and concentration | 10 | 5 | `verified_partial`: weights, HHI, largest position, and top-10 disclosed overlap; no Phase D structural guardrails |
| Fees, purchase, and redemption conditions | 5 | 3 | `verified_partial`: fee schedules preserve classes/tiers; no personal transaction-condition or purchase check |
| Monitoring and rebalancing | 5 | 2 | `verified_partial`: sync, freshness, theses, and weekly reporting; no allocation bands or rebalancing action |
| Source provenance, freshness, and conflict handling | 5 | 4 | `verified_partial`: strong dated source/freshness/conflict contracts, but provider coverage and real-world validation remain incomplete |
| **Total** | **100** | **30** | **30% verified coverage** |

## 90% Conclusion

KunJin does not reach 90% of the reasonably automatable beginner fund-purchase
workflow. The independent score is **30/100**. The strongest verified areas are
evidence-oriented fund research, source handling, and partial portfolio
analytics. The central purchase-safety chain remains absent: no financial
foundation gate, risk assessment, goal-horizon constraint, allocation budget,
candidate risk classification, portfolio guardrail decision, or post-purchase
check is executable.

Passing 408 tests demonstrates consistency for covered code paths. It does not
establish financial suitability, source truth, real-profile correctness, Skill
resistance to adversarial prompts, or real-world purchase safety. The failed
initial Ruff gate was remediated and now passes; that engineering result does
not increase the financial-workflow score.

## Verification Record

Fresh commands run from `/Users/yanzihao/KunJin`:

```text
.venv/bin/python -m unittest discover -s tests -q
Ran 408 tests in 2.094s
OK
exit 0

PYTHONPYCACHEPREFIX=/private/tmp/kunjin-audit-pycache \
  .venv/bin/python -m compileall -q src tests
no output
exit 0

.venv/bin/ruff check .
All checks passed!
exit 0

.venv/bin/kunjin --json version
data.version = 0.1.0; warnings = []; errors = []
exit 0

.venv/bin/kunjin --json profile status
data = {"state":"missing","freshness":"missing"}
exit 0

.venv/bin/kunjin --json profile history
data = {"profiles":[]}
exit 0

rg -a -n '73129|84217|95311' <isolated data dir> <isolated state dir>
no output
exit 1 (no matches)

Skill.md repository/installed SHA-256:
c2689ddecb428bde4b0f19e9843bb80ffd6737e5b5243b4c97357abeeb589c67

agents/openai.yaml repository/installed SHA-256:
f649f9be9590945b767dda3723206fd60c580118d377ad7fdece8cbcfa1fe1f3
```

The first compile attempt failed because Python tried to write bytecode under
the sandbox-blocked macOS cache directory. Re-running with the documented
`PYTHONPYCACHEPREFIX` succeeded and is the relevant compilation result.

The first Ruff audit run failed with 896 full-repository findings. The final
passing result above was obtained only after Python-3.9-compatible rule
clarification and mechanical import, unused-import, and line-length cleanup.

## Final Phase A Acceptance Decision

Phase A is accepted for its deliberately narrow technical scope: encrypted,
versioned, locally edited profile storage with metadata-only normal output and
a research-only Skill transition. This acceptance does not mean KunJin can
assess suitability or help decide a purchase. The independent workflow score
remains **30/100**.

Real personal enrollment and live Keychain write/read remain user-operated
acceptance work. They should be performed through `kunjin profile edit` without
placing exact values in chat. Phase B must not begin producing directional
outputs until active-profile decryption and the new safety gates both pass.

## Next Priority

Phase B suitability safety gates are the next highest-priority work. They must
decrypt and validate the active profile first, calculate reserve/debt/cash-flow
and goal-horizon constraints deterministically, preserve separate risk capacity
and willingness, return explicit block reasons, and make absent, stale,
contradictory, or undecryptable critical data fail closed. Before Phase B is
accepted, the now-clean Ruff gate and bounded non-sensitive invalidation reason
codes must remain covered by regression tests and must not be weakened.
