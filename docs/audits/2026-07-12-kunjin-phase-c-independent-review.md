# KunJin Phase C Independent Financial And Beginner-Workflow Review

Date: 2026-07-13

Scope: the current working tree, Phase C design and implementation plan,
allocation models and engine, encrypted persistence, CLI privacy views, README,
repository and installed Skills, automated suites, synthetic paths, and real
amount-free acceptance. Project prompts and Skill claims were not accepted as
evidence without code, test, or command verification.

Current Phase C decision: **the automated synthetic acceptance, legacy V8-to-V9
migration, installed Skill synchronization, and amount-free real personal
blocked-path acceptance pass for Phase C's limited scope. Verified beginner
fund-purchase workflow coverage is 52/100 and 90% is not reached**.

## Findings

### P1 - Phase C still cannot answer which fund to buy or how much to buy

Phase C returns an abstract feasible region and equity ceilings. It does not:

- classify a real fund into protected cash, high-quality fixed income, or
  diversified equity;
- distinguish fixed-income-plus, convertible-bond, long-duration, credit,
  sector, concentrated, overseas, or leveraged fund risk;
- map a candidate fund into the personal portfolio;
- check the proposed post-purchase theme, manager, security, or asset-class
  overlap;
- select a target allocation; or
- calculate a purchase amount or trade direction.

Those are Phase D and Phase E capabilities and receive zero new credit here.

### P2 - Real personal acceptance proves the strict block, not a usable personal allocation range

The real personal database was backed up successfully before migration. The
5.95 MB online backup passed SQLite integrity and foreign-key checks and retained
migration markers 1 through 8. The live database then migrated successfully to
schema V9 and passed integrity and foreign-key checks with markers 1 through 9,
one allocation policy row, no allocation assessment rows, and no leftover
legacy temporary objects.

The amount-free real suitability assessment returned a fresh `blocked` state
with hard block `emergency_reserve_shortfall`, constraint
`monthly_ceiling_constrained`, no profile conflicts, and
`capability=research_only`. The acceptance-only amount-free allocation command
correctly returned:

- `status=blocked`
- block `suitability_blocked`
- `permitted_region=null`
- `assessment_id=null`
- inherited binding `monthly_ceiling_constrained`
- `freshness=transient`
- `capability=research_only`

Allocation history remained empty and allocation status remained missing. This
is the correct fail-closed outcome and proves that Phase C does not bypass a real
Phase B hard block or persist a fabricated range.

It does not prove that the owner's inputs can produce, understand, or safely use
a real `range_available` result. Successful range behavior remains supported by
synthetic profiles only. The owner must not alter truthful facts merely to reach
a more favorable state.

### P2 - Installed Skill acceptance remains a prompt-layer control

The repository and installed copies of `SKILL.md` and `agents/openai.yaml` are
byte-identical by both `cmp` and SHA-256 comparison. The active Skill therefore
enforces the suitability-then-allocation sequence, preserves `research_only`,
refuses Phase B bypasses and target conversion, and prohibits Codex from
executing the exact non-JSON allocation view.

This closes the previous deployment inconsistency. It does not prove that every
future model or modified client will obey prompt-layer controls, and it adds no
financial-workflow score by itself.

### P2 - The 50% equity and 10% fixed-income stresses are policy assumptions

The fixed 0% cash, 10% high-quality-fixed-income, and 50% diversified-equity
loss assumptions are transparent and useful as simple guardrails. They are not
universal financial facts.

Fifty percent does not cover every equity fund, especially concentrated,
sector, small-cap, single-country, currency-exposed, or leveraged products. Ten
percent does not represent long-duration, low-credit, convertible-bond,
fixed-income-plus, overseas-debt, or illiquid products. Nominal zero-loss cash
also omits inflation, currency, access, and redemption risks.

Until Phase D verifies real product characteristics, these coefficients can
support only abstract stress tests. They cannot establish a real fund's risk
bucket or personal suitability.

### P2 - The three-layer abstraction is too coarse for purchase decisions

The model cannot distinguish demand deposits, money-market funds, short bonds,
long bonds, credit bonds, convertible bonds, fixed-income-plus, balanced funds,
broad indexes, sectors, themes, small caps, overseas equities, or currency
exposure. A product name containing "bond", "stable", or "fixed income" is not
evidence that it belongs in `high_quality_fixed_income`.

The three layers are a defensible first boundary, not a complete beginner
portfolio model.

### P2 - Maximum equity can anchor a beginner as if it were a target

The engine, README, and repository Skill correctly state that the ceiling is not
a target. Nevertheless, `maximum_equity` is the most salient number and may be
misread as recommended or optimal.

It does not mean the user should invest up to the maximum, use it as a monthly
contribution mix, or assume the current portfolio is correctly classified. Zero
equity remains feasible. Future presentation should emphasize the feasible
inequalities and binding reasons before the maximum value.

### P2 - Input comprehension and truth remain major error sources

Phase C improves asset-field exclusivity explanations, dates, and postponement
conflicts, but it cannot verify account overlap, actual liquidity, correct asset
classification, behavioral loss tolerance, income stability, or interruption
risk. A beginner may also invent a distant goal date merely to avoid
`allocation_horizon_missing`, which would create false risk capacity.

### P2 - Overlap protection remains incomplete

Phase C detects when protected liquid claims exceed declared liquid protection
assets. It cannot prove that the underlying real accounts are distinct.

Existing portfolio overlap evidence is primarily top-ten disclosed-security
overlap. KunJin does not yet implement the video's complete three-not rules:
same theme, same manager, and excessive full-holdings overlap. It also lacks
post-purchase industry, style, issuer, credit, and bucket overlap analysis.

### P2 - Zero-return funding is useful but not complete planning

`fully_funded_now`, `fundable_without_return`, and
`funding_gap_without_return` prevent optimistic return assumptions from hiding a
gap. They do not model inflation, tax, changing goal costs, changing income, or
probability scenarios. They are conservative baseline states, not a success
forecast, and a gap must not be solved by automatically increasing equity risk.

## Verified Phase C Capabilities

- Strict authenticated Phase B gating with no persisted allocation when Phase B
  is blocked.
- Deterministic separation of protected capital, investable stock, and monthly
  discretionary capacity.
- Individual goal and obligation horizons with calendar-date boundaries.
- Zero-return funding states and explicit missing-horizon/protected-capital
  blocks.
- Transparent fixed inequalities, stress limits, willingness and stability
  ceilings, and complete binding-code retention.
- No target point, optimizer, real-product classification, purchase amount, or
  trade direction.
- Exact local results encrypted at rest; JSON views contain only amount-free
  percentages, counts, dates, inequalities, and stable codes.
- Logging redaction covers actual Phase C objects, exact payloads, private names,
  generic obligation amounts, encryption metadata, fingerprints, nested
  structures, and mixed-context rendering while preserving safe diagnostics.
- Exact legacy-V8 detection and transactional normalization into the current
  strict schema before V9, with hostile and unusable historical states rejected.

## Beginner Purchase-Workflow Coverage

The rubric measures verified workflow help, not expected return or protection
from loss. Tests support only the capabilities they exercise. Phase D and Phase
E receive zero new credit.

| Decision area | Weight | Score | Independent assessment |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 10 | Same as Phase B: useful gates, but user classification, tax, insurance, irregular spending, and broader affordability remain incomplete |
| Risk capacity and willingness | 10 | 5 | Loss amount, drawdown, willingness, and stability now intersect into equity ceilings; fixed coefficients and subjective answers materially limit confidence |
| Goals and investment horizon | 10 | 7 | Individual sleeves, calendar horizons, and zero-return states are useful; inflation, probabilities, and complete multi-goal planning remain absent |
| Asset allocation and risk budget | 15 | 4 | Synthetic transparent three-layer feasible regions and a real fail-closed block exist; no real personal range, target selection, contribution mix, or product mapping |
| Fund-category identification | 10 | 4 | Existing sourced type and benchmark evidence remains partial; Phase C intentionally does not classify real products |
| Individual fund quality research | 15 | 8 | Existing NAV, drawdown, manager, fee, size, benchmark, disclosure, announcement, and peer evidence remains partial; Phase C adds no credit |
| Portfolio overlap and concentration | 10 | 5 | Existing weights, HHI, largest position, and top-ten disclosed overlap remain partial; no Phase D structural decision or proposed-purchase overlap |
| Fees, purchase, and redemption conditions | 5 | 3 | Sourced schedules exist; no personalized pre-purchase execution check |
| Monitoring and rebalancing | 5 | 2 | Synchronization and reporting exist; no target bands, drift policy, or rebalancing action |
| Source provenance, freshness, and conflict handling | 5 | 4 | Strong dated provenance, freshness, migration, and conflict contracts; product-level validation remains incomplete |
| **Total** | **100** | **52** | **52% verified coverage** |

The seven-point increase from Phase B's 45/100 comes only from implemented
risk-ceiling intersection, goal sleeves and funding states, and the synthetic
transparent allocation feasible region. No points were added for test count,
code volume, encryption strength, or documentation volume.

## 90% Conclusion

KunJin does not provide 90% of the reasonably automatable help a beginner needs
to purchase funds. The independent score is **52/100**.

Phase C is useful: it can protect declared emergency capital, require a purpose
and horizon, connect loss capacity to an equity ceiling, and avoid using
optimistic returns to hide a goal gap. It still completes only part of the
pre-purchase risk boundary.

The missing central chain remains real-fund risk classification, portfolio
construction, same-theme/same-manager/full-holdings overlap guards, candidate
fit, post-purchase exposure, permitted amount tracing, execution-condition
checks, and allocation-band monitoring. A beginner should not use Phase C alone
to decide what to buy or how much.

## Verification Record

Fresh repository verification from `/Users/yanzihao/KunJin`:

```text
Phase C focused allocation suite
Ran 222 tests
OK

Full suite before legacy compatibility repair
Ran 840 tests
OK

Final full suite after reviewed legacy V8 repair
Ran 858 tests
OK

Ruff check src tests
All checks passed

Ruff format --check on touched files
All files already formatted

compileall with PYTHONPYCACHEPREFIX under /private/tmp
exit 0

git diff --check
exit 0
```

Independent Task 10 specification and privacy/security reviews ended with no
P0-P2 findings. Independent legacy-V8 specification and quality/security
reviews also ended with no P0-P2 findings after adversarial cases for mixed-case
foreign keys, duplicate sequences, BLOB/NUL values, policy tampering, external
views/triggers, and transactional rollback.

The first real JSON acceptance attempt exited before profile access with
`applied schema does not match migration markers`. The compatibility root cause
was fixed, independently reviewed, and then exercised against the live database
after a verified online backup. The final live database has markers 1 through 9,
passes integrity and foreign-key checks, contains one allocation policy and zero
allocation assessments, and has no legacy normalization objects.

Real amount-free acceptance returned the expected Phase B hard block and
transient Phase C `suitability_blocked` result. Allocation status remained
missing and history remained empty. No non-JSON exact suitability or allocation
command was run, and the JSON outputs exposed no exact amount or private name.

The repository and installed Skill pairs are byte-identical. Live data and state
directories also contained no synthetic allocation sentinel matches in the
filename-only privacy scan.

## Acceptance Decision And Next Priority

Phase C passes automated, synthetic, migration, installed-Skill, and real
personal blocked-path acceptance for its limited scope. The real result is a
hard block, not a personal allocation range or purchase approval.

Phase D is the next product priority: real-fund risk classification plus theme,
manager, holdings, industry, style, duration, credit, issuer, and
portfolio-construction guardrails. Phase E purchase amounts or directional
advice must not begin before Phase D is implemented and independently audited.
