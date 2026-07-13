# KunJin Phase B Independent Financial And Software Review

Date: 2026-07-12

Scope: the current working tree, Phase B design and implementation plan,
deterministic suitability engine, encrypted assessment persistence, CLI privacy
views, README, repository and installed Skill copies, complete automated suite,
isolated synthetic runtime acceptance, and amount-free real-profile metadata
acceptance. No project prompt or Skill claim was accepted as evidence without
direct inspection or fresh command output.

Current Phase B decision: **the automated, synthetic, and real personal
acceptance gates pass for Phase B's limited scope; verified beginner
fund-purchase workflow coverage remains 45/100 and 90% is not reached**.

## Findings

### P1 - Phase B still cannot determine what fund to buy or whether a proposed purchase is suitable

Phase B answers whether the current financial foundation is blocked,
constrained, or able to proceed to a later allocation calculation. It does not
calculate an asset-allocation range, convert loss capacity and willingness into
a risk budget, classify funds into portfolio risk buckets, screen a candidate
against a target allocation, project a purchase, approve an amount, or produce
buy, add, reduce, sell, hold, or rebalance direction.

This is a deliberate scope boundary, but it remains the central beginner risk.
Even a correct `ready_for_allocation` result leaves the purchase decision chain
unfinished. Asset allocation and risk budget (Phase C), portfolio construction
and overlap/manager/theme guardrails (Phase D), and pre-purchase checks and
directional Skill integration (Phase E) receive zero credit in this audit.

### P2 - `ready_for_allocation` can still be misread as a buy signal

The CLI, README, repository Skill, installed Skill, and default prompt all
counter this risk by returning `capability=research_only` for every state and
stating that `ready_for_allocation` is not a recommendation. That is the correct
boundary.

The state name nevertheless sounds affirmative to a beginner. The protection
depends on consumers preserving the capability field and explanatory text. It
is not a software authorization boundary that prevents another client or model
from turning the state into a recommendation. Until Phase C-E exist, the only
defensible interpretation is: no Phase B hard block was found from the supplied
profile; no fund or amount has been approved.

### P2 - The policy is transparent and conservative, but several thresholds are judgment rules rather than universal financial facts

Blocking delinquency, revolving credit-card interest, unknown nonzero debt
types, nonpositive investable cash flow, overdue commitments, and unfunded
nonpostponable priority-one goals inside one year is financially defensible for
a beginner safety gate. The 8% rate-only block applies only to `credit_card`,
`consumer_loan`, and `personal_loan`. A high-rate `auto_loan`, `student_loan`,
or `mortgage` does not block on rate alone unless another rule fires. This
creates a coverage gap for expensive debts outside the three configured
categories and makes the debt-type choice financially consequential.

The 8% threshold and 6/9/12 reserve-month schedule are clear, versioned, and
testable, but they are fixed policy choices. They do not account for tax,
employer benefits, insurance coverage, debt prepayment penalties, subsidized
debt, household support, jurisdiction, or the user's actual secure alternative
return.

Mortgage rate alone not blocking is reasonable because the required payment is
still included in reserve and cash-flow calculations. It does not establish
that a mortgage is affordable beyond the entered payment facts. Near-term
obligations can be included both in the full reserve requirement and monthly
saving calculation; this is deliberately conservative and may over-restrict,
but it does not create an unsafe allowance.

### P2 - “Verified emergency reserve” is bounded user-reported data, not external account verification

The engine correctly uses the smaller of designated emergency reserve and
immediately available cash plus cash-like assets. This prevents a designated
reserve from exceeding the liquidity the user reported. It does not verify bank
balances, withdrawal restrictions, settlement delays, guarantees, currency,
credit risk, or whether a reported cash-like asset is genuinely available in an
emergency. The term `verified` should be understood as internally supported by
profile fields, not independently verified by a financial institution.

### P2 - Risk-response validation detects contradictions but does not establish an investable risk budget

The engine retains separate maximum-loss, maximum-drawdown, and 10%/20%/30%
reaction inputs and fails closed on specified contradictions. This is useful:
an increasingly severe loss cannot produce a less defensive reaction, and a
zero loss budget cannot coexist with hold responses or loss-accepting goals.
These are specified partial checks, not a complete behavioral-risk assessment.
Captured fields including `experienced_material_loss`,
`understands_multi_year_recovery`, and profile-level `can_postpone_goal_use`
do not currently participate in the engine result.

However, internally consistent answers may still be unrealistic, unstable, or
behaviorally untested. Phase B does not reconcile willingness with financial
capacity, translate either into equity exposure, or model stress losses. A
`risk_answers_consistent=true` value means only that the coded subset of
contradiction rules did not fire.

### P2 - Goal and cash-flow coverage is useful but intentionally incomplete

The engine evaluates each obligation and goal, respects fully reserved items,
blocks overdue gaps and critical short-horizon gaps, calculates priority-one
goal saving, and constrains the user-confirmed monthly ceiling. Stable reason
codes make the result traceable, but only partially actionable.

Lower-priority goals are not deducted from monthly cash flow, and the model does
not evaluate taxes, insurance premiums, irregular annual spending, future
income changes, education or medical uncertainty, or goal probability. These
omissions can matter materially. The profile must therefore be maintained
conservatively, and Phase C must not treat the Phase B ceiling as a recommended
investment amount.

### P2 - The beginner editor leaves material input-quality risk

Debt types and several profile concepts are entered through normalized English
terms such as `consumer_loan`, `personal_loan`, `revolving_interest`, and risk
reaction values. Exact choices improve deterministic matching, but the local
editor does not provide enough plain-language financial definitions, examples,
or boundary explanations for a beginner to classify every debt and answer every
risk field consistently. A technically valid but misunderstood answer can
change whether the 8% gate applies or whether a conflict is detected.

This is especially important because the engine intentionally refuses fuzzy
classification. Fail-closed unknown debt handling is safer than guessing, but
the editor must eventually explain the required terminology and distinctions
without steering the user toward a more favorable answer.

### P2 - Skill injection defenses are explicit but remain prompt-layer controls

The Skill includes adversarial examples for ignoring blocks, asking for a small
starter amount, treating long holding periods as overriding debt, suppressing
explanations, promoting `ready_for_allocation`, and reusing stale assessments.
Repository and installed Skill files are byte-identical.

These rules reduce accidental overreach but do not cryptographically bind an AI
response to the assessment. No adversarial model-execution harness proved that
every future model, client, or modified Skill will preserve the state. The CLI
itself exposes research data without generating a trade, so prompt injection
can still create unsupported advice outside KunJin's deterministic engine.

### P2 - Real personal acceptance passed, but the local exact view retains terminal exposure

The owner privately ran the real non-JSON assessment and confirmed that the
exact calculations were correct. The primary agent then ran only the amount-free
real JSON assessment, status, and history paths. They authenticated and read the
live encrypted profile, bound the result to profile version 1 and policy version
1, and returned a fresh current assessment without exposing amounts, names,
nonce, ciphertext, fingerprint, or raw policy material.

The current real state is `blocked`, with
`emergency_reserve_shortfall` and `monthly_ceiling_constrained`. This does not
approve a fund purchase. The hard block `emergency_reserve_shortfall` must first
be resolved through actual financial-condition changes and followed by a new
assessment. `monthly_ceiling_constrained` is not a hard block and need not be
eliminated to pursue a more favorable status; unless the underlying facts
naturally change, future Phase C must retain it as a binding constraint that
reduces the permitted allocation or contribution range. The audit does not
infer a remediation amount from the amount-free result.

The local exact view intentionally exposes amounts in terminal scrollback and
is not protected from screen recording, shoulder-surfing, clipboard capture,
malicious local processes, or memory inspection.

## Verified Phase B Capabilities

- A fixed, immutable policy V1 has canonical JSON and a stable SHA-256 checksum.
- Debt types are exact allowlisted values; unknown nonzero types fail closed.
- Delinquency and revolving interest block, supported unsecured consumer debt
  blocks at 8%, and mortgage rate alone does not block.
- Emergency reserve uses the smaller supported amount, conservative cent
  rounding, 6/9/12-month rules, debt service, and unfunded obligations inside
  one year.
- Obligations and goals use explicit one-year and three-year boundaries,
  preserve fully funded items, and return stable block and constraint codes.
- Monthly safety residual and safe monthly ceiling are deterministic; itemized
  debt payments cannot be understated by a lower aggregate profile field.
- Risk-answer and field conflicts produce stable conflict codes and a hard
  `profile_conflict` block.
- All applicable reasons are retained in stable enum order instead of stopping
  at the first failure.
- Safety-monotonicity tests cover stricter debt, reserve, cash-flow, goal,
  obligation, and risk inputs. This is strong evidence for coded dimensions,
  not a formal proof over every real-world financial condition.
- Assessments are encrypted, immutable, policy-bound, profile-bound, fresh for
  at most 24 hours, and never valid beyond profile expiry.
- Local non-JSON output includes exact derived amounts. JSON assess, status,
  and history expose amount-free metadata and remain `research_only`.

## Reason Traceability And Actionability Review

The reason set is substantially more useful than a single score. Debt,
reserve, overdue commitment, critical-goal, cash-flow, stale-profile, and field
conflict codes identify the category of condition that must be reviewed without
naming a fund or trade. Multiple simultaneous reasons are preserved, which
avoids a misleading one-problem-at-a-time workflow.

Codes alone are not a financial plan. Exact reserve shortfalls and safe ceilings
are available only in the private local view, while goal and obligation names,
individual gaps, and per-item monthly contributions are not shown alongside the
aggregated derived amounts. A user with several commitments may therefore know
that a category blocked or constrained the result without seeing a complete
item-by-item remediation chain in the assessment output.

The reasons are traceable but only partially actionable. Remediation may
require changing obligations, debt, spending, or profile facts. The Skill
should explain a code's condition and request a local reassessment after
correction; it should not prescribe borrowing, debt consolidation, product
selection, or a purchase amount.

## Privacy And Security Evidence

An isolated synthetic profile used sentinel amounts `73129`, `84217`, and
`95311`, a temporary SQLite database and state directory, and an in-memory fake
32-byte key store. The local CLI path returned the expected exact calculations.
JSON assess, status, and history contained no sentinel, private obligation name,
amount object, policy checksum, input fingerprint, nonce, ciphertext, or keyed
fingerprint. Searching the isolated data and state directories returned exit 1
with no plaintext sentinel matches.

The assessment encryption uses a domain separated from profile encryption,
strict canonical amount serialization, AES-256-GCM, and keyed fingerprints.
Schema V8 constrains metadata and prevents assessment or policy update/delete.
Logging redacts raw profile values, derived Phase B values, ciphertext, nonce,
and fingerprints, including structured fields and Decimal representations.

This evidence covers exercised persistence, output, and logging contracts. It
does not prove protection from a compromised session, terminal capture,
malicious dependencies, memory inspection, filesystem snapshots taken while
the local view is visible, or backups containing both SQLite and Keychain data.
Assessment status, reason codes, counts, and timestamps intentionally remain
plaintext metadata and may themselves reveal limited financial context.

## Beginner Purchase-Workflow Coverage

The rubric measures verified workflow help, not expected return or protection
from loss. Tests support only the capabilities they exercise. Phase C-E features
receive zero credit.

| Decision area | Weight | Score | State and evidence |
| --- | ---: | ---: | --- |
| Personal cash flow and financial safety | 15 | 10 | `verified_partial`: useful debt, reserve, obligation, and monthly-flow gates; 8% rate-only coverage excludes auto, student, and mortgage debt, inputs are user-classified, and tax, insurance, irregular spending, and broader affordability remain omitted |
| Risk capacity and willingness | 10 | 3 | `verified_partial`: separate loss/drawdown/reaction facts and a specified subset of conflict checks; several collected behavioral fields are unused and there is no capacity-willingness reconciliation, stress budget, or exposure limit |
| Goals and investment horizon | 10 | 6 | `verified_partial`: individual horizon, funding-gap, urgency, postponement, and priority-one saving rules; no complete multi-goal feasibility or capital segmentation |
| Asset allocation and risk budget | 15 | 0 | `designed_only` Phase C |
| Fund-category identification | 10 | 4 | `verified_partial`: sourced type and benchmark evidence exists; no Phase D portfolio risk-bucket classification |
| Individual fund quality research | 15 | 8 | `verified_partial`: formal NAV, drawdown, manager, fee, size, benchmark, disclosure, announcement, and peer evidence; no complete active-return attribution, bond-credit/duration analysis, valuation, earnings, or persistent-flow evidence |
| Portfolio overlap and concentration | 10 | 5 | `verified_partial`: weights, HHI, largest position, and top-10 disclosed overlap; no Phase D theme, manager, bucket, and post-purchase structural decision |
| Fees, purchase, and redemption conditions | 5 | 3 | `verified_partial`: sourced class and tier schedules; no personalized transaction eligibility or pre-purchase fee check |
| Monitoring and rebalancing | 5 | 2 | `verified_partial`: synchronization, freshness, theses, and weekly reporting; no allocation bands, drift policy, or rebalancing action |
| Source provenance, freshness, and conflict handling | 5 | 4 | `verified_partial`: strong dated provenance, freshness, source-tier, and conflict contracts; provider coverage and real-world validation remain incomplete |
| **Total** | **100** | **45** | **45% verified coverage** |

The 15-point increase from Phase A's 30/100 comes only from implemented and
verified financial-safety, risk-consistency, and goal-horizon capabilities. No
points were added for test count, documentation volume, encryption strength, or
designed future phases.

## 90% Conclusion

KunJin does not provide 90% of the reasonably automatable help a beginner needs
to purchase funds. The independent score is **45/100**. Phase B is useful and
material: it can prevent several common unsafe starting conditions and explain
why analysis is blocked or constrained. It is not complete purchase guidance.

The missing central chain is substantial: transparent asset allocation,
capacity-versus-willingness risk limits, fund risk-bucket classification,
portfolio theme/manager/overlap construction rules, candidate screening,
post-purchase projection, allowed-amount tracing, execution-condition checks,
and ongoing allocation-band monitoring and rebalancing. Fund research also
still lacks complete bond-fund risk, valuation, earnings, persistent flows, and
fully validated news attribution.

Passing 575 tests demonstrates consistency for covered code paths. It does not
prove the user's inputs are true, that the policy fits every household, that a
fund is suitable, that markets will behave like stress assumptions, that a
model cannot ignore the Skill, or that the user will avoid investment loss.

## Verification Record

Fresh commands run from `/Users/yanzihao/KunJin`:

```text
.venv/bin/python -m unittest discover -s tests -q
Ran 575 tests in 3.375s
OK
exit 0

PYTHONPYCACHEPREFIX=/private/tmp/kunjin-phase-b-audit-pycache \
  .venv/bin/python -m compileall -q src tests
no output
exit 0

.venv/bin/ruff check .
All checks passed!
exit 0

git diff --check
no output
exit 0

Isolated synthetic local assessment
local exact amount contract passed
exit 0

Isolated synthetic JSON assess/status/history
amount-free and private-material checks passed
exit 0

rg -a -n '73129|84217|95311' <isolated data dir> <isolated state dir>
no output
exit 1 (no matches)
```

The first synthetic harness attempt used a 33-byte fake key and failed before
assessment with `encrypted_profile_unavailable`; the encryption contract and
existing working fixture confirmed the required length is 32 bytes. Re-running
the unchanged acceptance flow with the correct fake key passed. This was an
audit-fixture error, not a product-code failure, and no implementation file was
changed.

Repository/installed file pairs are byte-identical:

```text
SKILL.md SHA-256:
a43f88481f85f5645f931e2695c39672016f8ccd18efd2968ad0b7867722aad0

agents/openai.yaml SHA-256:
9421377170b1aef213db022941e75fcee7cb7b6e6d830c9f373b03ab7c19eb44
```

Real personal acceptance, performed without exposing exact local values:

```text
Owner local non-JSON assessment
exact calculation chain reviewed privately and confirmed correct

Real --json suitability assess
exit 0
assessment_id = 2
profile_version = 1
policy_version = 1
status = blocked
hard_blocks = [emergency_reserve_shortfall]
constraints = [monthly_ceiling_constrained]
profile_conflicts = []
required_reserve_months = 6
risk_answers_consistent = true
debt_count = 0; obligation_count = 0; goal_count = 0
freshness = fresh
capability = research_only
valid_until = 2026-07-13T11:07:33.722344+00:00

Real --json suitability status
exit 0
fresh; assessment_id = 2; profile_version_id = 1; policy_version = 1
status and reason/constraint binding matched the assessment

Real --json suitability history
exit 0
assessment_count = 2
both records = blocked with the same reason, constraint, and research_only capability
```

The real JSON outputs contained no exact amount, goal or obligation name,
ciphertext, nonce, fingerprint, or raw policy. The audit agent did not run or
capture the real non-JSON command; the owner reviewed that local output
privately before the primary agent ran the amount-free JSON commands.

## Phase B Acceptance Decision

Phase B meets its automated, isolated synthetic, and real personal acceptance
gates for its deliberately limited scope: the rules are deterministic and
substantially conservative, stricter coded inputs do not improve the tested
safety result, exact derived amounts are confined to the owner-reviewed local
view and encrypted storage, JSON is amount-free, and documentation and Skill
wording preserve `research_only`.

The accepted real result is `blocked`, not purchase approval. The reserve
shortfall hard block must be resolved locally and followed by a fresh
reassessment before proceeding to Phase C. The monthly-ceiling constraint is
not something the user must remove by changing truthful profile inputs; Phase C
must preserve it as a binding limit unless the underlying facts naturally
change. Passing personal acceptance does not change the independent coverage
result of **45/100**.

## Next Priority

Phase C transparent allocation ranges are the next highest-priority gap. They
must reconcile risk capacity and willingness, convert stress loss into a
permitted risky-asset ceiling, separate near-term from investable assets, expose
binding constraints and allocation ranges, and keep every output non-directional.
Phase C must not treat `safe_monthly_ceiling` as a recommended contribution or
award credit to Phase D-E features before they are implemented and audited.
