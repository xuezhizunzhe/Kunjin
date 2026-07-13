# KunJin Phase C Transparent Allocation Ranges Design

Date: 2026-07-12

Status: user-approved design, pending implementation plan

## 1. Purpose

Phase C converts an authenticated, current Phase B suitability result into a
transparent asset-allocation feasible region. It answers:

1. Which capital must remain protected and outside the investment pool?
2. What existing capital and monthly cash flow are eligible for allocation?
3. What equity ceiling follows from horizon, loss capacity, drawdown tolerance,
   behavioral willingness, and financial stability?
4. Which constraints bind the result?
5. Can the declared goals be funded without assuming investment returns?

Phase C does not select a fund, classify a product, calculate sector or manager
exposure, approve a purchase, produce a position size, or create a trading
direction. Every Phase C result remains `research_only` until the later
portfolio-construction and pre-purchase phases pass.

## 2. Design Principles

- Use transparent rule intersection, not a weighted score or optimizer.
- Treat every percentage as a ceiling or feasible boundary, never a mandatory
  target.
- Keep zero equity valid at every horizon.
- Fail closed when Phase B is blocked, stale, unauthenticated, or unavailable.
- Separate existing capital from monthly cash flow so the same requirement is
  not deducted twice.
- Isolate each goal or obligation by its own date; a short-term sleeve cannot
  borrow a long-term sleeve's risk ceiling.
- Use zero-return funding checks instead of expected-return forecasts.
- Expose policy assumptions and every binding constraint.
- Keep exact CNY values and private item names local and encrypted.
- Preserve truthful constraints rather than encouraging the user to edit facts
  merely to obtain a more favorable state.

## 3. Scope

### 3.1 Included

- Strict Phase B gate.
- Protected-capital validation.
- Existing investable-capital calculation.
- Monthly discretionary allocation ceiling.
- Goal and obligation sleeves.
- Horizon equity ceilings.
- Portfolio stress-loss constraints.
- Behavioral-willingness ceilings.
- Financial-stability ceilings.
- Permitted allocation feasible region.
- Zero-return goal funding states.
- Versioned policy and immutable encrypted assessments.
- Local exact and amount-free JSON views.
- CLI, README, logging, and Codex Skill alignment.
- Synthetic and real blocked-path acceptance plus independent financial audit.

### 3.2 Excluded

- Fund risk-bucket classification.
- Deciding whether a bond or fixed-income-plus fund is defensive.
- Wide-index, active, sector, manager, theme, and security-overlap rules.
- Current-portfolio compliance or rebalancing instructions.
- Target allocation points or mandatory lower bounds.
- Expected-return, efficient-frontier, Monte Carlo, or optimization outputs.
- Candidate screening, purchase amounts, and post-purchase projection.
- Buy, hold, add, reduce, sell, or rebalance labels.

These remain Phase D or Phase E capabilities.

## 4. User-Visible States And Capability

Phase C has two financial states:

- `blocked`: no allocation region is produced.
- `range_available`: the allocation analysis completed without a hard block.
  A current-stock feasible region is present when current investable stock is
  positive. When it is zero, the response instead carries
  `no_current_investable_stock` and does not fabricate a percentage allocation.

Technical errors are not financial states. They use stable nonzero error codes.

Every successful or blocked response contains:

```text
capability = research_only
```

`range_available` means only that the deterministic Phase C analysis completed
and, when capital exists, admits one or more abstract cash/fixed-income/equity
combinations. It is not a target, fund recommendation, or purchase approval.

## 5. Strict Phase B Gate

Allocation-range assessment begins by authenticating the active profile and the
latest matching Phase B suitability assessment.

- Phase B `blocked`: return transient Phase C `blocked`; do not calculate or
  persist an allocation assessment.
- Phase B `constrained`: calculate the feasible region and preserve every
  constraint as a binding Phase C input.
- Phase B `ready_for_allocation`: calculate the feasible region, still with
  `research_only` capability.
- Missing, stale, mismatched, tampered, or undecryptable Phase B input: fail
  closed.

The current personal acceptance result is Phase B `blocked` with an emergency-
reserve hard block. The first real Phase C acceptance therefore proves strict
blocking, not a personal allocation range.

## 6. Asset-Layer Model

Phase C v1 uses only three abstract layers:

- `protected_cash`: emergency reserve, operating cash, and protected short-term
  assigned capital. Equity ceiling is 0%.
- `high_quality_fixed_income`: an abstract policy bucket with a 10% stress
  assumption. A real product cannot enter this bucket until Phase D verifies
  it.
- `diversified_equity`: an abstract broadly diversified equity bucket with a
  50% stress assumption.

Existing manual bond, equity, sector, and volatile-asset fields contribute to
the total financial-asset balance. Phase C does not certify their risk bucket
or claim that current holdings fit the permitted region. That comparison waits
for Phase D.

## 7. Capital Isolation

### 7.1 Asset-field exclusivity

The profile asset fields are mutually exclusive as-of balances. The local
editor must explain that one balance belongs in exactly one field. The engine
cannot prove that two user-entered fields refer to different real accounts, so
the local output must preserve this limitation.

```text
total_financial_assets =
    immediately_available_cash
  + cash_like_assets
  + low_risk_fixed_income_assets
  + manual_equity_fund_assets
  + manual_bond_fund_assets
  + manual_sector_fund_assets
  + other_volatile_assets
```

### 7.2 Protected liquid claims

Phase C consumes the authenticated exact amounts from Phase B. It does not
independently recalculate the Phase B emergency-reserve policy.

```text
liquid_protection_assets =
    immediately_available_cash + cash_like_assets

protected_short_term_assigned =
    sum(amount_already_reserved for obligations due within 3 years)
  + sum(amount_already_reserved for goals due within 3 years)

protected_liquid_claims =
    verified_emergency_reserve
  + minimum_operating_cash
  + protected_short_term_assigned
```

`verified_emergency_reserve` is the Phase B internally supported value, not an
external bank verification.

If `protected_liquid_claims > liquid_protection_assets`, return:

```text
protected_capital_overlap_or_shortfall
```

This detects unsupported protected claims but cannot prove that all remaining
user-entered asset fields are free from real-world overlap.

### 7.3 Existing investable capital

```text
investable_stock_assets =
    max(0, total_financial_assets - protected_liquid_claims)
```

Already-reserved amounts for goals beyond three years remain assigned
investable sleeves and are not subtracted from the investment pool. Their own
goal horizon limits their equity exposure.

### 7.4 Monthly flows and no double deduction

Phase B already subtracts essential expenses, debt service, obligation saving,
priority-one goal saving, and the monthly cash buffer.

```text
monthly_discretionary_allocation_ceiling =
    Phase B safe_monthly_ceiling
```

Phase C does not subtract the full obligation or goal gap from existing capital
when Phase B has already funded that gap through required monthly saving.

- Required obligation saving remains protected and outside discretionary
  allocation.
- Required priority-one goal saving belongs to its goal sleeve.
- The Phase B safe monthly ceiling is discretionary allocation capacity, not a
  recommended contribution.
- `monthly_ceiling_constrained` remains a binding limit; the user is not asked
  to remove it by changing truthful inputs.

## 8. Goal And Obligation Sleeves

### 8.1 Individual horizons

Every reserved goal or obligation is evaluated separately. Its capital cannot
use another item's longer horizon.

| Time until expected use | Equity ceiling |
| --- | ---: |
| Within 1 year | 0% |
| More than 1 year through 3 years | 10% |
| More than 3 years through 5 years | 30% |
| More than 5 years through 8 years | 50% |
| More than 8 years | 70% |

The boundaries use calendar periods and exact dates. Zero equity remains valid
for every sleeve.

### 8.2 Residual-capital horizon

Unassigned existing capital and discretionary monthly contributions need a
declared purpose. Phase C v1 chooses the default residual horizon
conservatively:

1. Earliest target date among positive-gap priority-one goals.
2. Otherwise, earliest target date among any positive-gap goal.
3. If neither exists, return `allocation_horizon_missing` and do not produce a
   region.

There is no implicit long-term or eight-year default. A fully funded goal does
not silently become the purpose for unassigned capital.

The owner currently has no declared goals, so a future personal range requires
local profile editing after the Phase B hard block is genuinely resolved.

### 8.3 Postponement consistency

`can_postpone_goal_use` never raises an equity ceiling or extends a date.

- If the profile-level value is false while an individual goal claims its use
  date can be postponed, return a stable profile-conflict code.
- A true profile-level value does not override an individual nonpostponable
  goal.
- Any date change requires a new confirmed profile version.

## 9. Zero-Return Funding States

Phase C v1 does not assume an equity, bond, or cash return.

For each goal:

```text
zero_return_funding =
    amount_already_reserved
  + confirmed_monthly_goal_saving * remaining_contribution_periods
```

Only the Phase B priority-one monthly saving is a confirmed goal contribution
in v1. Discretionary allocation capacity is not silently assigned to lower-
priority goals.

Goal states are:

- `fully_funded_now`: the reserved amount covers the target.
- `fundable_without_return`: confirmed saving covers the gap by the target date.
- `funding_gap_without_return`: the zero-return baseline does not cover the
  target.
- `allocation_horizon_missing`: no eligible purpose supplies a residual
  horizon.

`funding_gap_without_return` is not proof that a goal will fail. It means the
confirmed plan relies on future changes or uncertain returns. Phase C must not
increase risk to force apparent feasibility.

Goal and obligation names appear only in the explicit local exact view and in
encrypted payloads.

## 10. Allocation Policy V1

### 10.1 Stress assumptions

Stress assumptions are policy tests, not forecasts:

| Abstract layer | Stress loss |
| --- | ---: |
| Protected or discretionary cash | 0% |
| Verified high-quality fixed income | 10% |
| Diversified equity | 50% |

No low-risk assumption is permitted for an unclassified real product.

### 10.2 Behavioral-willingness ceiling

| Consistent response | Equity ceiling |
| --- | ---: |
| Would reduce or redeem at a 10% decline | 10% |
| Can hold through 10%, but not 20% | 30% |
| Can hold through 20%, but not 30% | 50% |
| Can hold through 30%, has experienced material loss, and understands multi-year recovery | 70% |
| Claims 30% tolerance but lacks either experience or recovery understanding | 50% |

This makes `experienced_material_loss` and
`understands_multi_year_recovery` active policy inputs. Consistency checks from
Phase B remain prerequisites.

### 10.3 Financial-stability ceiling

| Financial condition | Equity ceiling |
| --- | ---: |
| Stable income, no dependents, no interruption signal | 70% |
| Stable income with dependents, or variable income without dependents | 50% |
| Variable income with dependents, or any credible interruption signal | 30% |
| Unstable income | 20% |

No rule in this table may increase a stricter horizon, loss, drawdown, or
willingness result.

### 10.4 Rounding

- Money uses `Decimal` and the existing CNY quantum.
- Required or protected amounts round upward.
- Available amounts round downward.
- Percent ceilings round downward to whole percentage points.
- Rounding can never widen the feasible region.

## 11. Feasible Region

The percentage feasible region applies to the current existing investable
capital snapshot. Let:

- `E` be the diversified-equity proportion of investable capital.
- `B` be the high-quality-fixed-income proportion.
- `C` be discretionary cash.
- `I` be `investable_stock_assets`.
- `L` be maximum tolerable CNY loss.
- `D` be maximum tolerable portfolio drawdown.

All proportions are decimals between zero and one.

```text
E + B + C = 1
E >= 0
B >= 0
C >= 0

0.50 * E + 0.10 * B <= D
I * (0.50 * E + 0.10 * B) <= L

E <= weighted_horizon_ceiling
E <= behavioral_willingness_ceiling
E <= financial_stability_ceiling
```

The weighted horizon ceiling is the sum of each assigned sleeve amount times
its horizon ceiling, divided by investable capital. Residual capital uses the
conservative residual horizon from Section 8.2.

The engine returns the inequalities, maximum permitted equity proportion, and
every binding constraint. It does not choose an optimum or a mandatory point.

Monthly discretionary capacity remains a separate exact amount ceiling. Phase
C does not assign that future contribution a stock/bond mix because the safe
remaining loss budget depends on the classified portfolio at the time of the
contribution. Phase D supplies classification and Phase E checks a proposed
amount against the then-current total portfolio. This prevents Phase C from
spending the same CNY loss budget independently on existing capital and every
future monthly contribution.

When `I = 0`, Phase C returns `no_current_investable_stock` and no current-stock
percentage region. It may still report that an authenticated monthly capacity
exists in the encrypted local detail, but it does not fabricate an allocation
need or a future contribution mix.

If `I > 0` and `L` or `D` is zero, only zero-stress cash is permitted. A
zero-risk result is valid; the engine must not invent a need to invest.

## 12. Output Contract

### 12.1 Local exact view

Only this explicit local command may show exact values:

```text
kunjin allocation ranges
```

It may show:

- Protected-capital breakdown.
- Existing investable capital.
- Monthly discretionary ceiling.
- Goal and obligation names, dates, reserved amounts, gaps, and confirmed
  monthly saving.
- Exact stress-loss calculations.
- Per-sleeve and aggregate feasible-region calculations.

Codex and the installed Skill must never execute this command through tools.

### 12.2 Amount-free JSON

```text
kunjin --json allocation ranges
kunjin --json allocation status
kunjin --json allocation history
kunjin --json allocation policy
```

Allocation-range JSON may contain:

- `blocked` or `range_available`.
- Percentage ceilings and feasible-region coefficients.
- Binding constraint and profile-conflict codes.
- Goal and obligation counts and zero-return-state counts.
- Horizon bands.
- Profile, suitability-assessment, and policy versions.
- Assessed time, valid-until time, and freshness.
- `capability=research_only`.

It must not contain:

- Exact or bucketed CNY amounts.
- Goal or obligation names.
- Raw profile or encrypted payload.
- Nonce, ciphertext, keyed fingerprints, or input fingerprints.
- A target allocation or purchase amount.

`allocation policy` intentionally exposes the fixed non-sensitive percentages,
stress coefficients, effective date, version, and checksum for transparency.

## 13. Persistence And Schema V9

### 13.1 `allocation_policy_versions`

- `version`
- `canonical_policy_json`
- `policy_checksum`
- `effective_at`
- `created_at`

Rows are immutable and cannot be deleted. Reusing a version with different
content fails closed.

### 13.2 `allocation_assessments`

- `id`
- `profile_version_id`
- `suitability_assessment_id`
- `policy_version`
- `input_fingerprint`
- `status` (`range_available` only; transient blocks are not persisted)
- `permitted_region_json`
- `binding_constraints_json`
- `safe_summary_json`
- `encrypted_amount_results`
- `encryption_algorithm`
- `encryption_key_version`
- `nonce`
- `keyed_payload_fingerprint`
- `assessed_at`
- `valid_until`
- `created_at`

Rows are immutable and cannot be deleted. Foreign keys use `ON DELETE
RESTRICT`.

Plaintext JSON contains percentages, counts, horizon bands, and stable codes
only. Exact capital, loss amounts, monthly amounts, goal names, and itemized
calculations remain encrypted.

The V9 migration is atomic from every supported prior schema. A failure cannot
leave tables, triggers, or a version marker behind.

## 14. Encryption And Fingerprints

Allocation uses the existing 32-byte Keychain master key with new HKDF domains:

```text
kunjin/allocation-assessment/encryption/v1
kunjin/allocation-assessment/fingerprint/v1
```

Associated data is fixed for the allocation domain. Profile, suitability, and
allocation ciphertexts cannot be cross-decrypted.

The exact allocation payload uses strict canonical JSON with duplicate-key,
nonfinite-number, unexpected-key, and noncanonical-decimal rejection.

The keyed input fingerprint binds at least:

```text
profile version id
profile keyed fingerprint
suitability assessment id
suitability input fingerprint
allocation policy checksum
canonical UTC exact assessed_at instant
```

Decryption and fingerprint verification never create or replace a missing key.

## 15. Freshness And Concurrency

```text
valid_until = min(
    assessed_at + 24 absolute hours,
    active_profile.valid_until,
    suitability_assessment.valid_until,
)
```

An allocation is fresh only when:

- Its profile remains the active confirmed profile.
- Its referenced suitability assessment remains the latest current assessment
  for that profile and policy.
- The suitability state is not blocked.
- The allocation policy is still the selected policy.
- The keyed input fingerprint authenticates.
- The exact encrypted result authenticates and matches a deterministic
  recalculation.
- `created_at` equals the authenticated `assessed_at`, and `valid_until` equals
  the deterministic minimum defined above.
- `assessed_at <= current_time < valid_until`; a future-dated assessment is
  never fresh.
- The validity instant has not been reached.

Persistence must assert the profile and suitability binding in one
`BEGIN IMMEDIATE` transaction. After commit and before returning
`range_available`, the service reauthenticates the same bindings. Status repeats
the final binding check before reporting `fresh`.

Old immutable records remain history and can never become current again.

## 16. Stable Errors And Block Codes

Technical errors:

- `allocation_policy_unavailable`
- `allocation_calculation_failed`
- `encrypted_profile_unavailable`

Financial or input block codes include:

- `suitability_blocked`
- `allocation_horizon_missing`
- `protected_capital_overlap_or_shortfall`
- `allocation_profile_conflict`

Constraint and information codes include:

- Existing Phase B constraints such as `monthly_ceiling_constrained`.
- `funding_gap_without_return`.
- `no_current_investable_stock` when existing investable capital is zero but a
  valid monthly path may still exist.
- Binding horizon, loss-amount, drawdown, willingness, and stability codes.

A technical failure is never represented as a financial block or an empty
allocation.

## 17. Codex Skill Contract

For directional or position-size requests:

```text
--json suitability assess
    -> blocked: stop and explain Phase B reasons
    -> constrained or ready_for_allocation:
       --json allocation ranges
```

For Phase C:

- `blocked`: explain block codes and local correction conditions only.
- `range_available`: explain the feasible region and binding constraints, not a
  target or trade.
- Technical failure or stale result: `insufficient_data`.

The Skill must reject:

- "Give me the best stock percentage."
- "Use the equity maximum as my target."
- "Ignore the reserve block and show the hypothetical range."
- "Assume my fund is a high-quality bond fund."
- "Use optimistic returns to make my goal feasible."
- "Do not explain; output only a purchase amount."

Authorization, evidence capture, factual research, freshness checks, and sync
remain available without an allocation result.

## 18. Test Matrix

### 18.1 Policy and model tests

- Fixed V1 canonical bytes and checksum.
- Exact stress, horizon, willingness, stability, and rounding invariants.
- Decimal and timezone canonicalization.
- Immutable models and amount-free summaries.

### 18.2 Boundary tests

- Exactly 1, 3, 5, and 8 years, plus one day on either side.
- Zero loss budget, exact stress equality, and one cent below equality.
- Drawdown boundaries and downward percentage rounding.
- Every 10%, 20%, and 30% reaction combination.
- Material-loss experience and recovery-understanding combinations.
- Income stability, dependents, and interruption-risk combinations.
- Calendar, leap-day, and timezone behavior.

### 18.3 Capital-isolation tests

- Protected claims equal, below, and above supporting liquid assets.
- Fully reserved items do not create gaps.
- Short-term assigned capital cannot use a longer sleeve ceiling.
- Required monthly saving is not deducted twice.
- Multiple goals are order invariant.
- Residual horizon uses the earliest positive-gap priority-one goal, then the
  earliest other positive-gap goal.
- No eligible residual goal returns `allocation_horizon_missing`.

### 18.4 Safety monotonicity

With the same investable-capital denominator, otherwise identical inputs must
never receive a wider percentage feasible region when:

- Horizon shortens.
- Maximum tolerable loss or drawdown falls.
- Income becomes less stable.
- Dependents increase.
- Interruption risk appears.
- Behavioral reaction becomes more defensive.
- A Phase B constraint is added or tightened.

Protected-capital demand is a denominator-changing case and is tested using the
continuous absolute-risk boundary before the whole-percentage display rounding.
When protected claims rise while total assets and all risk inputs stay fixed,
the following continuous maximum equity CNY boundary must not increase:

```text
min(
    weighted_horizon_numerator,
    2 * maximum_tolerable_loss,
    2 * maximum_tolerable_drawdown * investable_stock_assets,
    behavioral_willingness_ceiling * investable_stock_assets,
    financial_stability_ceiling * investable_stock_assets,
)
```

The reported whole-percentage summary may move by a rounding step as the
denominator changes, but the exact inequalities must still hold and the
continuous CNY boundary above must not increase. That denominator/rounding
effect must not be described as increased risk capacity or a target allocation.

### 18.5 Crypto, storage, and service tests

- Domain-separation known vectors and cross-decryption rejection.
- Strict canonical exact-payload serialization.
- Schema V9 atomic migration, foreign keys, constraints, and immutable triggers.
- Policy insert, content conflict, and malicious-subclass rejection.
- Assessment insert, history, and current lookup.
- Profile or suitability switch before insert, after commit, and before status
  return.
- Missing key, tampering, stale input, policy mismatch, and fingerprint mismatch.

### 18.6 CLI, privacy, logging, and Skill tests

- Local exact output with synthetic values only.
- JSON ranges, status, history, and policy contracts.
- Exact CNY and private-name sentinels absent from JSON, SQLite plaintext,
  logs, exceptions, and state directories.
- Decimal and structured-log redaction for allocation-derived values.
- Financial blocks exit zero; technical failures exit nonzero.
- Prompt-injection examples preserve `research_only`.
- Repository and installed Skill files are byte-identical.
- All existing tests continue to pass.

## 19. Acceptance Criteria

Phase C is accepted only when:

1. Phase B blocked input produces no allocation region or persisted allocation.
2. Every successful synthetic range exposes all binding constraints and fixed
   policy assumptions.
3. Stricter tested inputs never widen the percentage feasible region when the
   investable-capital denominator is unchanged; higher protected claims never
   increase the continuous absolute equity CNY boundary defined in Section
   18.4, and every displayed rounded result still satisfies the exact CNY stress
   inequality.
4. Multi-goal capital is isolated and monthly requirements are not double
   deducted.
5. No exact amount or private item name appears in normal JSON, plaintext
   SQLite, logs, exceptions, or audit documents.
6. Local exact values are encrypted at rest and accessible only through the
   explicit owner-run command.
7. Policy and assessments are immutable, versioned, fresh, and bound to the
   authenticated profile and suitability result.
8. `range_available` remains `research_only` and is never described as a target
   or purchase approval.
9. The current real profile proves strict blocking. A later real range requires
   a genuinely resolved Phase B hard block and a declared goal horizon.
10. An independent financial review re-scores the same 100-point beginner
    workflow, gives Phase D-E zero new credit, and explicitly answers whether
    KunJin reaches 90%.

Test count, code volume, encryption strength, and documentation volume do not
increase the financial-workflow score by themselves.

## 20. Implementation Boundaries

The implementation should introduce a focused `kunjin.allocation` package:

```text
src/kunjin/allocation/
  models.py
  policy.py
  engine.py
  serialization.py
  crypto.py
  store.py
  service.py
```

The existing suitability package remains the owner of profile and Phase B
rules. Allocation consumes an authenticated suitability snapshot through an
explicit internal contract; it does not duplicate Phase B calculations.

`cli.py` owns parsing and routing only. Database schemas remain in the storage
package. Logging owns redaction. The Codex Skill explains amount-free outputs
without replacing deterministic calculations.

## 21. Objective Review After Phase C

After every implementation task, use fresh specification and quality reviewers.
At the end of Phase C, perform another independent financial review that starts
with findings and challenges the score rather than defending the project.

The review must explicitly examine:

- Whether the three-layer abstraction is too coarse for a beginner.
- Whether 50% equity and 10% fixed-income stress assumptions are defensible as
  policy tests.
- Whether the feasible region is useful without becoming a target.
- Whether user-entered asset categories and goal dates are understandable.
- Whether protected-capital overlap checks are sufficient.
- Whether current `blocked` personal acceptance was mistakenly counted as a
  verified personal allocation range.
- Whether any Phase D or Phase E capability was claimed early.

No design text may predetermine a favorable score or a 90% conclusion.
