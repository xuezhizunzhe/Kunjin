# KunJin Phase C Transparent Allocation Ranges Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert a current authenticated Phase B suitability result into an amount-private, deterministic feasible region across protected cash, abstract high-quality fixed income, and diversified equity without producing a target allocation or purchase recommendation.

**Architecture:** Add a focused `kunjin.allocation` package containing immutable models, fixed Policy V1, pure capital-isolation and feasible-region calculations, strict canonical serialization, domain-separated encryption, persistence, and orchestration. Phase C consumes a new authenticated Phase B snapshot contract, fails closed on blocked or stale suitability, stores only successful encrypted assessments in schema V9, and exposes exact values only through the owner-run non-JSON command.

**Tech Stack:** Python 3.9+, frozen dataclasses, `Decimal`, timezone-aware `datetime`, canonical JSON, SQLite transactions/triggers, `cryptography` AES-256-GCM/HKDF/HMAC, macOS Keychain, `unittest`, Ruff, existing KunJin CLI envelopes.

---

## Scope And File Map

Create:

- `src/kunjin/allocation/__init__.py`: public Phase C exports.
- `src/kunjin/allocation/models.py`: stable enums and frozen input/result records.
- `src/kunjin/allocation/policy.py`: immutable Policy V1, canonical JSON, validation, and checksum.
- `src/kunjin/allocation/engine.py`: capital isolation, horizon bands, zero-return funding, ceilings, inequalities, and binding constraints.
- `src/kunjin/allocation/serialization.py`: strict canonical exact-result encoding and decoding.
- `src/kunjin/allocation/crypto.py`: allocation-specific HKDF domains and authenticated encryption.
- `src/kunjin/allocation/store.py`: immutable policy and assessment persistence.
- `src/kunjin/allocation/service.py`: strict Phase B gate, authenticated orchestration, freshness, and views.
- `tests/unit/test_allocation_models_policy.py`: model and policy invariants and golden checksum.
- `tests/unit/test_allocation_engine.py`: horizon, capital, funding, feasible-region, and monotonicity tests.
- `tests/unit/test_allocation_serialization_crypto.py`: canonicalization, known vectors, tamper, and cross-domain tests.
- `tests/unit/test_schema_v9.py`: atomic migration, foreign keys, checks, and immutability.
- `tests/unit/test_allocation_store.py`: policy and assessment store behavior.
- `tests/unit/test_allocation_service.py`: gate, concurrency, recalculation, freshness, and privacy behavior.
- `docs/audits/2026-07-12-kunjin-phase-c-independent-review.md`: evidence-backed financial review written only after final verification.

Modify:

- `src/kunjin/suitability/service.py`: expose an authenticated, exact internal suitability snapshot without weakening public amount-free views.
- `src/kunjin/suitability/editor.py`: explain asset-field exclusivity and goal postponement semantics.
- `src/kunjin/storage/schema.py`: add schema V9 tables and immutable triggers.
- `src/kunjin/storage/repository.py`: migrate atomically through V9.
- `src/kunjin/cli.py`: add `allocation ranges/status/history/policy`, context wiring, and exact/local versus JSON routing.
- `src/kunjin/logging.py`: redact allocation-derived exact fields.
- `tests/unit/test_suitability_editor.py`: editor wording and conflict inputs.
- `tests/unit/test_logging.py`: allocation redaction coverage.
- `tests/integration/test_cli.py`: allocation command, privacy, exit-code, and persistence contracts.
- `tests/test_smoke.py`: packaged parser/help coverage.
- `README.md`: Phase C commands, states, policy assumptions, and limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: strict Phase B-to-C workflow and refusal rules.
- `integrations/codex/kunjin-fund/agents/openai.yaml`: amount-free Phase C prompt contract.

Do not alter or remove unrelated dirty-worktree files. Never place real personal amounts or private goal/obligation names in source, tests, documentation, logs, commits, or agent prompts. Never execute non-JSON `kunjin allocation ranges` through Codex tools.

### Task 1: Add Allocation Models And Fixed Policy V1

**Files:**
- Create: `src/kunjin/allocation/__init__.py`
- Create: `src/kunjin/allocation/models.py`
- Create: `src/kunjin/allocation/policy.py`
- Create: `tests/unit/test_allocation_models_policy.py`

- [ ] **Step 1: Write failing model and policy tests**

Define tests requiring frozen records, stable enum values, exact fixed parameters, whole-percentage rounding, canonical bytes, and a hard-coded SHA-256 checksum:

```python
class AllocationPolicyTest(unittest.TestCase):
    def test_policy_v1_is_fixed_and_canonical(self) -> None:
        policy = AllocationPolicyV1()
        policy.validate()
        self.assertEqual(policy.version, "1")
        self.assertEqual(policy.stress_loss_by_layer[AssetLayer.PROTECTED_CASH], D("0"))
        self.assertEqual(policy.stress_loss_by_layer[AssetLayer.HIGH_QUALITY_FIXED_INCOME], D("0.10"))
        self.assertEqual(policy.stress_loss_by_layer[AssetLayer.DIVERSIFIED_EQUITY], D("0.50"))
        self.assertEqual(policy.horizon_equity_ceilings, ((1, D("0")), (3, D("0.10")), (5, D("0.30")), (8, D("0.50")), (None, D("0.70"))))
        self.assertEqual(hashlib.sha256(policy.canonical_json()).hexdigest(), ALLOCATION_POLICY_V1_CHECKSUM)

    def test_result_is_immutable_and_has_no_target_field(self) -> None:
        fields = {item.name for item in dataclasses.fields(AllocationResult)}
        self.assertNotIn("target", fields)
        self.assertNotIn("recommended", fields)
        with self.assertRaises(FrozenInstanceError):
            object.__setattr__(sample_result(), "status", AllocationStatus.BLOCKED)
```

- [ ] **Step 2: Run the focused test and confirm the missing module failure**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_models_policy -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'kunjin.allocation'`.

- [ ] **Step 3: Implement stable models and fixed policy**

Use these public shapes and enum values:

```python
class AllocationStatus(str, Enum):
    BLOCKED = "blocked"
    RANGE_AVAILABLE = "range_available"

class AssetLayer(str, Enum):
    PROTECTED_CASH = "protected_cash"
    HIGH_QUALITY_FIXED_INCOME = "high_quality_fixed_income"
    DIVERSIFIED_EQUITY = "diversified_equity"

class AllocationBlockCode(str, Enum):
    SUITABILITY_BLOCKED = "suitability_blocked"
    ALLOCATION_HORIZON_MISSING = "allocation_horizon_missing"
    PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL = "protected_capital_overlap_or_shortfall"
    ALLOCATION_PROFILE_CONFLICT = "allocation_profile_conflict"

class GoalFundingState(str, Enum):
    FULLY_FUNDED_NOW = "fully_funded_now"
    FUNDABLE_WITHOUT_RETURN = "fundable_without_return"
    FUNDING_GAP_WITHOUT_RETURN = "funding_gap_without_return"
    ALLOCATION_HORIZON_MISSING = "allocation_horizon_missing"

class AllocationSleeveKind(str, Enum):
    GOAL = "goal"
    OBLIGATION = "obligation"
    RESIDUAL = "residual"

class AllocationConstraintCode(str, Enum):
    NEAR_TERM_OBLIGATION_GAP = "near_term_obligation_gap"
    NEAR_TERM_GOAL_GAP = "near_term_goal_gap"
    MONTHLY_CEILING_CONSTRAINED = "monthly_ceiling_constrained"
    FUNDING_GAP_WITHOUT_RETURN = "funding_gap_without_return"
    NO_CURRENT_INVESTABLE_STOCK = "no_current_investable_stock"
    HORIZON_BINDING = "horizon_binding"
    LOSS_AMOUNT_BINDING = "loss_amount_binding"
    DRAWDOWN_BINDING = "drawdown_binding"
    WILLINGNESS_BINDING = "willingness_binding"
    STABILITY_BINDING = "stability_binding"

@dataclass(frozen=True)
class AllocationResult:
    status: AllocationStatus
    capability: str
    blocks: Tuple[AllocationBlockCode, ...]
    binding_constraints: Tuple[AllocationConstraintCode, ...]
    profile_conflicts: Tuple[AllocationProfileConflictCode, ...]
    safe_summary: AllocationSafeSummary
    permitted_region: Optional[PermittedRegion]
    exact: Optional[AllocationExactResult]
```

`AllocationExactResult` contains exact goal, obligation, assigned-sleeve, and aggregate-input records. Its validation enforces CNY cent precision, capital conservation, zero-return funding equations, stable sleeve vocabulary, weighted-horizon arithmetic, loss/drawdown ceiling recomputation, region presence, binding-code truth, and amount-free plaintext fields. Blocked results require `exact=None`. `AllocationPolicyV1` must reject subclasses, stateful containers/timezones, and mutated parameters at persistence boundaries; use exact tuple mappings rather than mutable dictionaries. Set the fixed effective instant to `2026-07-12T00:00:00+00:00`, freshness to 24 hours, CNY quantum to `0.01`, required rounding to `ROUND_CEILING`, available and percentage rounding to `ROUND_FLOOR`, and derive `ALLOCATION_POLICY_V1_CHECKSUM` from the final canonical bytes before freezing the golden test.

- [ ] **Step 4: Run model and policy tests**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_models_policy -v`

Expected: 39 tests PASS with fixed checksum `4ab1bfde13afbbc87730e6ce9f842757d64d6565fe27dee18c0d03e125f3d708`.

- [ ] **Step 5: Commit the isolated model/policy change**

```bash
git add src/kunjin/allocation/__init__.py src/kunjin/allocation/models.py src/kunjin/allocation/policy.py tests/unit/test_allocation_models_policy.py
git commit -m "feat: add allocation policy and models"
```

### Task 2: Implement Capital Isolation, Horizon Bands, And Zero-Return Funding

**Files:**
- Create: `src/kunjin/allocation/engine.py`
- Create: `tests/unit/test_allocation_engine.py`

- [ ] **Step 1: Write failing date, isolation, and funding tests**

Cover exact 1/3/5/8-year boundaries and one day around each boundary, leap-day clamping, order-independent goals, short-term reserved claims, equal/below/above liquid support, no double deduction of monthly saving, earliest positive-gap priority-one residual goal, fallback to earliest other positive-gap goal, and no-goal blocking:

```python
def test_residual_horizon_uses_earliest_positive_gap_priority_one_goal(self) -> None:
    profile = profile_with_goals(
        goal("later", years=8, priority=1, gap=D("100")),
        goal("earlier", years=5, priority=1, gap=D("100")),
        goal("soon", years=2, priority=2, gap=D("100")),
    )
    inputs = build_allocation_inputs(profile, assessment_result(), POLICY, ASSESSED_AT)
    self.assertEqual(inputs.capital.residual_horizon_date, date(2031, 7, 12))

def test_phase_b_monthly_ceiling_is_not_reduced_again(self) -> None:
    inputs = build_allocation_inputs(profile_with_priority_goal(), assessment_result(safe_monthly_ceiling=D("800.00")), POLICY, ASSESSED_AT)
    self.assertEqual(inputs.capital.monthly_discretionary_allocation_ceiling, D("800.00"))
```

- [ ] **Step 2: Run the focused engine tests and confirm missing implementation failures**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_engine -v`

Expected: FAIL because `evaluate_allocation` and horizon helpers are absent.

- [ ] **Step 3: Implement exact capital and sleeve calculations**

Implement a pure capital/sleeve builder. It deliberately stops before the final
feasible region so Task 2 never fabricates Task 3 outputs:

```python
@dataclass(frozen=True)
class AllocationCapitalInputs:
    assessment_date: date
    total_financial_assets: Decimal
    liquid_protection_assets: Decimal
    verified_emergency_reserve: Decimal
    minimum_operating_cash: Decimal
    protected_short_term_assigned: Decimal
    protected_liquid_claims: Decimal
    investable_stock_assets: Decimal
    monthly_discretionary_allocation_ceiling: Decimal
    maximum_tolerable_loss: Decimal
    maximum_tolerable_drawdown: Decimal
    residual_horizon_date: Optional[date]
    goal_funding_details: Tuple[GoalFundingDetail, ...]
    obligation_funding_details: Tuple[ObligationFundingDetail, ...]
    assigned_sleeves: Tuple[AssignedSleeveDetail, ...]

@dataclass(frozen=True)
class AllocationInputs:
    blocks: Tuple[AllocationBlockCode, ...]
    profile_conflicts: Tuple[AllocationProfileConflictCode, ...]
    inherited_constraints: Tuple[AllocationConstraintCode, ...]
    capital: Optional[AllocationCapitalInputs]

def build_allocation_inputs(
    profile: FinancialProfile,
    suitability: AssessmentResult,
    policy: AllocationPolicyV1,
    assessed_at: datetime,
) -> AllocationInputs:
    """Return deterministic capital and sleeve inputs without persistence or I/O."""
```

Use `total_financial_assets`, `liquid_protection_assets`, `protected_short_term_assigned`, `protected_liquid_claims`, and `investable_stock_assets` exactly as specified. `_horizon_ceiling(as_of, target_date, policy)` must use calendar anniversary dates, not `days / 365`. `_contribution_periods` must match Phase B. Reconcile Phase B's aggregate rounded required saving to eligible individual goals and obligations with Policy V1 `largest_remainder`: floor raw per-item requirements to cents, distribute remaining authenticated cents by descending fractional remainder with a canonical tie-break, and require the final sum to equal the authenticated aggregate. Never assign the discretionary ceiling to a goal. Detect protected-liquid and long-term-assigned capital overlap independently and accumulate every applicable financial block before returning `capital=None`.

- [ ] **Step 4: Run isolation and horizon tests**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_engine -v`

Expected: all capital-isolation, calendar, residual-horizon, and funding-state cases PASS.

- [ ] **Step 5: Commit the pure isolation engine**

```bash
git add src/kunjin/allocation/engine.py tests/unit/test_allocation_engine.py
git commit -m "feat: isolate allocation capital and horizons"
```

### Task 3: Complete The Stress Feasible-Region Engine And Monotonicity

**Files:**
- Modify: `src/kunjin/allocation/engine.py`
- Modify: `tests/unit/test_allocation_engine.py`

- [ ] **Step 1: Add failing ceiling, equality, and monotonicity tests**

Require the loss, drawdown, horizon, willingness, and stability constraints; exact stress equality; one-cent-below loss cases; zero-loss and zero-drawdown cash-only results; every reaction/experience/recovery combination; every income/dependent/interruption combination; and paired safety-monotonicity cases. Percentage ceilings must be monotone when the investable denominator is fixed. For increased protected claims, compare the continuous CNY boundary `min(weighted_horizon_numerator, 2L, 2DI, willingness*I, stability*I)` before whole-percentage display rounding; it must not increase, and the displayed result must still satisfy `I * 0.50 * E <= L`. A one-percentage-point display sawtooth is not treated as new risk capacity.

```python
def test_region_never_widens_when_loss_budget_falls(self) -> None:
    wider = evaluate_allocation(profile(maximum_tolerable_loss=D("5000")), snapshot(), POLICY, NOW)
    narrower = evaluate_allocation(profile(maximum_tolerable_loss=D("4999.99")), snapshot(), POLICY, NOW)
    self.assertLessEqual(narrower.permitted_region.maximum_equity, wider.permitted_region.maximum_equity)

def test_zero_loss_budget_keeps_cash_only_feasible(self) -> None:
    result = evaluate_allocation(profile(maximum_tolerable_loss=D("0")), snapshot(), POLICY, NOW)
    self.assertEqual(result.permitted_region.maximum_equity, D("0"))
    self.assertIn("loss_amount_binding", result.binding_constraints)
```

- [ ] **Step 2: Run the new cases and confirm they fail on incomplete region logic**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_engine -v`

Expected: FAIL on missing `PermittedRegion` inequalities, ceiling calculations, or binding codes.

- [ ] **Step 3: Implement transparent intersection without an optimizer**

Add the final pure entry point `evaluate_allocation(profile, suitability, policy, assessed_at) -> AllocationResult`, consuming `build_allocation_inputs`. Return normalized inequalities for `E + B + C = 1`, non-negativity, `0.50E + 0.10B <= D`, `I(0.50E + 0.10B) <= L`, and the three equity ceilings. Calculate `maximum_equity` as the greatest feasible equity percentage under all constraints while preserving the bond/cash continuum; do not return a selected `(E, B, C)` point. Round percentage ceilings downward to whole percentage points. Mark every constraint equal to the final boundary, including inherited Phase B constraints, with stable enum values:

```python
HORIZON_BINDING = "horizon_binding"
LOSS_AMOUNT_BINDING = "loss_amount_binding"
DRAWDOWN_BINDING = "drawdown_binding"
WILLINGNESS_BINDING = "willingness_binding"
STABILITY_BINDING = "stability_binding"
```

If investable stock is zero, set `permitted_region=None` and add `no_current_investable_stock`. A zero-risk feasible result remains `range_available`, never a technical error.

- [ ] **Step 4: Run the engine suite twice to detect order dependence**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_engine -v && .venv/bin/python -m unittest tests.unit.test_allocation_engine -v`

Expected: both runs PASS with identical test counts and no randomized ordering failure.

- [ ] **Step 5: Commit feasible-region rules**

```bash
git add src/kunjin/allocation/engine.py tests/unit/test_allocation_engine.py
git commit -m "feat: calculate transparent allocation ranges"
```

### Task 4: Add Strict Serialization And Domain-Separated Encryption

**Files:**
- Create: `src/kunjin/allocation/serialization.py`
- Create: `src/kunjin/allocation/crypto.py`
- Create: `tests/unit/test_allocation_serialization_crypto.py`
- Modify: `src/kunjin/allocation/__init__.py`

- [ ] **Step 1: Write failing canonicalization and crypto tests**

Require sorted exact JSON, canonical decimal strings, timezone normalization, exact-key schemas, duplicate-key/nonfinite/float/unexpected-key rejection, known HKDF vectors, missing-key noncreation, nonce/tamper rejection, and cross-decryption rejection against profile and suitability ciphertext.

```python
def test_allocation_domains_are_fixed(self) -> None:
    self.assertEqual(ALLOCATION_ENCRYPTION_INFO, b"kunjin/allocation-assessment/encryption/v1")
    self.assertEqual(ALLOCATION_FINGERPRINT_INFO, b"kunjin/allocation-assessment/fingerprint/v1")

def test_decoder_rejects_noncanonical_decimal(self) -> None:
    payload = valid_exact_json().replace(b'"100.00"', b'"100.0"', 1)
    with self.assertRaisesRegex(ValueError, "canonical decimal"):
        decode_exact_result(payload)
```

- [ ] **Step 2: Run tests and confirm missing serializer/cipher failures**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_serialization_crypto -v`

Expected: FAIL because allocation serialization and crypto modules do not exist.

- [ ] **Step 3: Implement canonical exact payload and allocation cipher**

Expose:

```python
def encode_exact_result(value: AllocationExactResult) -> bytes: ...
def decode_exact_result(payload: bytes) -> AllocationExactResult: ...

@dataclass(frozen=True)
class EncryptedAllocationAssessment:
    algorithm: str
    key_version: str
    nonce: str
    ciphertext: str
    keyed_fingerprint: str

class AllocationCipher:
    def encrypt(self, plaintext: bytes) -> EncryptedAllocationAssessment: ...
    def decrypt(self, value: EncryptedAllocationAssessment) -> bytes: ...
    def fingerprint(self, payload: bytes) -> str: ...
```

Use the existing 32-byte master key, AES-256-GCM, a fixed allocation associated-data value, and the two approved HKDF info values. `decrypt` and `fingerprint` must call `load_existing_key`; only `encrypt` may call `load_or_create_key`.

- [ ] **Step 4: Run serializer/crypto plus existing crypto regressions**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_serialization_crypto tests.unit.test_suitability_crypto tests.unit.test_suitability_assessment_crypto -v`

Expected: PASS, including all cross-domain rejection cases.

- [ ] **Step 5: Commit serialization and crypto**

```bash
git add src/kunjin/allocation/__init__.py src/kunjin/allocation/serialization.py src/kunjin/allocation/crypto.py tests/unit/test_allocation_serialization_crypto.py
git commit -m "feat: encrypt allocation assessments"
```

### Task 5: Add Atomic Schema V9 And Immutable Tables

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v9.py`

- [ ] **Step 1: Write failing migration tests from every supported version**

Test clean creation and upgrades from V1 through V8, exact columns/check constraints, `ON DELETE RESTRICT` foreign keys, immutable update/delete triggers, invalid status rejection, and injected mid-migration failure rollback with no V9 tables, triggers, or migration marker remaining.

```python
def test_v9_failure_is_atomic(self) -> None:
    repository = repository_at_v8()
    with patch.object(repository, "_execute_schema", side_effect=RuntimeError("injected")):
        with self.assertRaisesRegex(RuntimeError, "injected"):
            repository.migrate()
    self.assertNotIn("allocation_assessments", repository.table_names())
    self.assertNotIn(9, repository.applied_versions())
```

- [ ] **Step 2: Run migration tests and confirm schema version failure**

Run: `.venv/bin/python -m unittest tests.unit.test_schema_v9 -v`

Expected: FAIL because `SCHEMA_VERSION` is 8 and V9 tables are absent.

- [ ] **Step 3: Implement V9 in one transaction**

Set `SCHEMA_VERSION = 9` and add `SCHEMA_V9` defining exactly the two design tables. Require `allocation_assessments.status = 'range_available'`; ensure unique policy versions, positive foreign-key IDs, nonempty crypto metadata, 64-character lowercase digest length checks, `valid_until > assessed_at`, and immutable update/delete triggers for both tables. Add `(profile_version_id, suitability_assessment_id, policy_version, assessed_at)` and history indexes. Register V9 in the repository migration sequence without introducing an intermediate commit.

- [ ] **Step 4: Run all schema tests**

Run: `.venv/bin/python -m unittest tests.unit.test_schema_v2 tests.unit.test_schema_v4 tests.unit.test_schema_v5 tests.unit.test_schema_v6 tests.unit.test_schema_v7 tests.unit.test_schema_v8 tests.unit.test_schema_v9 -v`

Expected: PASS for clean and incremental migration paths.

- [ ] **Step 5: Commit schema V9**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v9.py
git commit -m "feat: add immutable allocation schema"
```

### Task 6: Add Allocation Policy And Assessment Stores

**Files:**
- Create: `src/kunjin/allocation/store.py`
- Create: `tests/unit/test_allocation_store.py`
- Modify: `src/kunjin/allocation/__init__.py`

- [ ] **Step 1: Write failing policy/store tests**

Require exact-type policy insertion, idempotent identical insertion, same-version content conflict rejection, malicious-subclass rejection, canonical stored JSON validation, atomic binding assertions, immutable assessment insertion, latest-current lookup, history ordering, and rejection of malformed plaintext summaries or encrypted metadata.

```python
def test_insert_asserts_profile_and_suitability_bindings(self) -> None:
    with self.assertRaises(AllocationBindingChangedError):
        self.store.insert(
            profile_version_id=1,
            suitability_assessment_id=2,
            expected_profile_fingerprint="a" * 64,
            expected_suitability_input_fingerprint="b" * 64,
            **valid_insert_fields(),
        )
```

- [ ] **Step 2: Run store tests and confirm missing module failure**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_store -v`

Expected: FAIL because `kunjin.allocation.store` is absent.

- [ ] **Step 3: Implement strict records and stores**

Provide `AllocationPolicyStore.ensure/get` and `AllocationAssessmentStore.insert/latest_for/history`. The insert method must use `BEGIN IMMEDIATE`, re-read the active confirmed profile and referenced suitability row, compare both fingerprints and current bindings, insert the allocation row, read it back, and commit. Parse every stored row through exact schemas; do not trust SQLite text merely because constraints passed.

- [ ] **Step 4: Run store and schema tests**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_store tests.unit.test_schema_v9 -v`

Expected: PASS with binding switches rejected and no partial inserts.

- [ ] **Step 5: Commit allocation persistence**

```bash
git add src/kunjin/allocation/__init__.py src/kunjin/allocation/store.py tests/unit/test_allocation_store.py
git commit -m "feat: persist immutable allocation ranges"
```

### Task 7: Expose Authenticated Suitability Snapshot And Orchestrate Phase C

**Files:**
- Modify: `src/kunjin/suitability/service.py`
- Create: `src/kunjin/allocation/service.py`
- Create: `tests/unit/test_allocation_service.py`
- Modify: `src/kunjin/allocation/__init__.py`

- [ ] **Step 1: Write failing snapshot, gate, concurrency, and freshness tests**

Test Phase B blocked transient response with zero allocation rows, constrained and ready paths, missing/stale/tampered/mismatched/undecryptable suitability failures, protected-capital and horizon blocks without persistence, profile/suitability switches before insert, after commit, and before return/status, deterministic recalculation mismatch, policy switch, exact payload tamper, missing key, and exact `valid_until` minimum behavior.

```python
def test_blocked_suitability_returns_transient_block_without_persistence(self) -> None:
    execution = self.service.ranges()
    self.assertEqual(execution.status, AllocationStatus.BLOCKED)
    self.assertEqual(execution.blocks, (AllocationBlockCode.SUITABILITY_BLOCKED,))
    self.assertIsNone(execution.permitted_region)
    self.assertEqual(self.assessment_store.history(), ())
```

- [ ] **Step 2: Run service tests and confirm missing contract/orchestration failures**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_service -v`

Expected: FAIL because authenticated snapshots and `AllocationService` are absent.

- [ ] **Step 3: Add an internal authenticated Phase B snapshot contract**

Add a frozen `AuthenticatedSuitabilitySnapshot` carrying the exact `AssessmentResult`, profile version/fingerprint, suitability assessment ID/input fingerprint/status/constraints, assessed/valid timestamps, and the exact loaded profile. Add `SuitabilityService.load_authenticated_snapshot()` that performs the same policy, decryption, fingerprint, deterministic recalculation, active-profile, latest-assessment, and freshness checks used by `status`; it must not alter `safe_json()` or expose exact values to CLI JSON.

- [ ] **Step 4: Implement allocation orchestration and stable technical errors**

Construct:

```python
class AllocationPolicyError(RuntimeError):
    code = "allocation_policy_unavailable"

class AllocationCalculationError(RuntimeError):
    code = "allocation_calculation_failed"

class AllocationService:
    def ranges(self) -> AllocationExecution: ...
    def status(self) -> Dict[str, object]: ...
    def history(self) -> Tuple[Dict[str, object], ...]: ...
    def policy(self) -> Dict[str, object]: ...
```

`ranges()` returns a transient financial block for Phase B `blocked`, `allocation_horizon_missing`, protected-capital shortfall, and profile conflict. Persist only `range_available`. Bind the input fingerprint to profile ID/fingerprint, suitability ID/input fingerprint, policy checksum, and canonical UTC exact `assessed_at` instant. Require both Phase B and Phase C `created_at == assessed_at`, deterministic `valid_until`, and `assessed_at <= current_time < valid_until` for a fresh result. After commit, load and authenticate the same bindings again before returning. Technical failures raise stable nonzero errors and never masquerade as empty ranges.

- [ ] **Step 5: Run service, suitability, store, and crypto tests**

Run: `.venv/bin/python -m unittest tests.unit.test_allocation_service tests.unit.test_allocation_store tests.unit.test_suitability_assessment_service tests.unit.test_allocation_serialization_crypto -v`

Expected: PASS with transient blocks unpersisted and successful assessments fresh only under unchanged bindings.

- [ ] **Step 6: Commit the authenticated service layer**

```bash
git add src/kunjin/suitability/service.py src/kunjin/allocation/__init__.py src/kunjin/allocation/service.py tests/unit/test_allocation_service.py
git commit -m "feat: orchestrate authenticated allocation ranges"
```

### Task 8: Add CLI Commands And Privacy-Separated Views

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser, JSON, local, and exit-code tests**

Cover `allocation ranges/status/history/policy`, JSON blocked/range responses, amount-free fields, policy transparency, local exact synthetic output, rejection of JSON exact values, financial-block exit zero, and technical-error exit nonzero. Add sentinel scans proving exact CNY and private names are absent from JSON envelopes.

```python
def test_allocation_ranges_json_is_amount_free(self) -> None:
    payload = run_json("allocation", "ranges")
    rendered = json.dumps(payload, ensure_ascii=False)
    self.assertNotIn("123456.78", rendered)
    self.assertNotIn("private-goal-sentinel", rendered)
    self.assertEqual(payload["data"]["capability"], "research_only")
    self.assertNotIn("target_allocation", rendered)
```

- [ ] **Step 2: Run CLI tests and confirm parser failures**

Run: `.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v`

Expected: FAIL because `allocation` is not a top-level command and the context lacks an allocation service.

- [ ] **Step 3: Wire allocation context and route privacy views**

Add `allocation` to `_TOP_LEVEL_COMMANDS`, construct stores/cipher/service from the existing profile key store, and add the four subcommands. `--json allocation ranges` must call `safe_json`; non-JSON `allocation ranges` may call `local_view`. `status`, `history`, and `policy` stay amount-free in both modes. Keep command names descriptive, never `recommend`, `target`, or `buy`.

- [ ] **Step 4: Run CLI and smoke tests**

Run: `.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v`

Expected: PASS with stable envelopes and exit behavior.

- [ ] **Step 5: Commit CLI commands**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose private allocation range commands"
```

### Task 9: Clarify Profile Editing And Reject Postponement Conflicts

**Files:**
- Modify: `src/kunjin/suitability/editor.py`
- Modify: `tests/unit/test_suitability_editor.py`
- Modify: `tests/unit/test_allocation_engine.py`

- [ ] **Step 1: Write failing editor and conflict tests**

Require local wording that each asset balance belongs in exactly one field, low-risk labels do not certify a fund, goal dates remain authoritative until reconfirmed, and profile-level `can_postpone_goal_use=False` conflicts with any individual goal claiming postponement.

```python
def test_profile_level_false_rejects_postponable_goal_claim(self) -> None:
    result = evaluate_allocation(profile(can_postpone_goal_use=False, goals=(goal(use_date_can_be_postponed=True),)), snapshot(), POLICY, NOW)
    self.assertEqual(result.blocks, (AllocationBlockCode.ALLOCATION_PROFILE_CONFLICT,))
    self.assertIn("profile_disallows_goal_postponement", result.profile_conflicts)
```

- [ ] **Step 2: Run editor and engine cases and confirm wording/conflict failures**

Run: `.venv/bin/python -m unittest tests.unit.test_suitability_editor tests.unit.test_allocation_engine -v`

Expected: FAIL until the editor explanations and conflict code are present.

- [ ] **Step 3: Add concise local explanations and deterministic validation**

Print the exclusivity limitation before asset prompts and the postponement rule before goal prompts. Do not add any field that extends a horizon automatically. Add the stable conflict code `profile_disallows_goal_postponement`; the engine must return a block with no permitted region.

- [ ] **Step 4: Run editor and engine tests**

Run: `.venv/bin/python -m unittest tests.unit.test_suitability_editor tests.unit.test_allocation_engine -v`

Expected: PASS without changing stored profile serialization.

- [ ] **Step 5: Commit editor safety wording**

```bash
git add src/kunjin/suitability/editor.py tests/unit/test_suitability_editor.py tests/unit/test_allocation_engine.py
git commit -m "feat: clarify allocation profile inputs"
```

### Task 10: Align Logging, Documentation, And Codex Skill

**Files:**
- Modify: `src/kunjin/logging.py`
- Modify: `tests/unit/test_logging.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `integrations/codex/kunjin-fund/agents/openai.yaml`

- [ ] **Step 1: Write failing redaction and Skill contract tests**

Add allocation exact keys and Decimal-bearing nested structures to logging tests. Extend integration/smoke assertions so the repository Skill forbids tool execution of non-JSON `allocation ranges`, requires suitability before allocation, refuses target/bypass/optimistic-return/product-classification prompts, and preserves `research_only`.

```python
def test_allocation_amount_keys_are_redacted_recursively(self) -> None:
    value = {"investable_stock_assets": Decimal("123456.78"), "nested": {"stress_loss_amount": "9876.54"}}
    rendered = json.dumps(redact_secrets(value), default=str)
    self.assertNotIn("123456.78", rendered)
    self.assertNotIn("9876.54", rendered)
```

- [ ] **Step 2: Run logging, CLI, and smoke tests and confirm contract failures**

Run: `.venv/bin/python -m unittest tests.unit.test_logging tests.integration.test_cli tests.test_smoke -v`

Expected: FAIL until redaction keys and documentation/Skill assertions are aligned.

- [ ] **Step 3: Implement redaction and user-facing contracts**

Document the three abstract layers, fixed stress tests, ceiling-not-target meaning, strict Phase B gate, goal requirement, zero-return states, commands, freshness, encryption, and Phase D/E exclusions. The Skill flow must be exactly `--json suitability assess` then, only for constrained/ready states, `--json allocation ranges`; it must map stale/technical errors to `insufficient_data` and never infer that a real fund belongs in the fixed-income layer.

- [ ] **Step 4: Synchronize and compare the installed Skill**

Run:

```bash
cp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cp integrations/codex/kunjin-fund/agents/openai.yaml /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
cmp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp integrations/codex/kunjin-fund/agents/openai.yaml /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
```

Expected: both `cmp` commands exit 0 with no output. This write requires the already approved local install-copy permission.

- [ ] **Step 5: Run documentation-adjacent tests**

Run: `.venv/bin/python -m unittest tests.unit.test_logging tests.integration.test_cli tests.test_smoke -v`

Expected: PASS with no exact allocation sentinel in logs or JSON.

- [ ] **Step 6: Commit logging and guidance**

```bash
git add src/kunjin/logging.py tests/unit/test_logging.py README.md integrations/codex/kunjin-fund/SKILL.md integrations/codex/kunjin-fund/agents/openai.yaml tests/integration/test_cli.py tests/test_smoke.py
git commit -m "docs: add allocation range safety workflow"
```

### Task 11: Run Full Acceptance, Real Blocked Path, And Independent Financial Audit

**Files:**
- Create: `docs/audits/2026-07-12-kunjin-phase-c-independent-review.md`
- Modify only if verification finds a Phase C defect: files owned by Tasks 1-10 and their focused tests.

- [ ] **Step 1: Run focused Phase C and full regression suites**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_allocation_models_policy tests.unit.test_allocation_engine tests.unit.test_allocation_serialization_crypto tests.unit.test_schema_v9 tests.unit.test_allocation_store tests.unit.test_allocation_service -v
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: every command exits 0; the full test count is recorded verbatim in the audit evidence.

- [ ] **Step 2: Scan all plaintext surfaces using synthetic sentinels**

Run integration tests that use `ALLOCATION-PRIVATE-GOAL-SENTINEL` and `87654321.09`, then inspect the temporary test database, captured logs, JSON output, exception text, and state directory. Expected: sentinels occur only inside the decrypted local-view assertion and encrypted ciphertext cannot be searched as plaintext.

- [ ] **Step 3: Verify the real personal path only through amount-free JSON**

Run:

```bash
.venv/bin/kunjin --json suitability assess
.venv/bin/kunjin --json allocation ranges
.venv/bin/kunjin --json allocation status
```

Expected: the current profile remains Phase B `blocked`; Phase C returns `blocked` with `suitability_blocked`, no permitted region, no persisted allocation assessment, `capability=research_only`, and no exact amount or private item name. Do not run `.venv/bin/kunjin allocation ranges`.

- [ ] **Step 4: Perform fresh specification and quality/security reviews**

Review every design section against implemented code and tests, then inspect privacy, immutable persistence, transaction bindings, missing-key behavior, deterministic recalculation, rounding direction, and recommendation-language regressions. Any finding is fixed with a new failing regression test, followed by the focused and full commands from Step 1.

- [ ] **Step 5: Write the independent financial review without predetermined credit**

The audit must lead with findings, use the same 100-point beginner workflow rubric as Phase B, give Phase D and Phase E zero new credit, explicitly challenge the 50% equity/10% fixed-income stress assumptions and three-layer abstraction, assess input comprehensibility and overlap limitations, distinguish the real blocked test from a real allocation range, and answer whether KunJin now provides 90% or more help. Do not award points for test count, code volume, encryption, or documentation volume by themselves.

- [ ] **Step 6: Re-run final verification after audit text is added**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
git diff --check
cmp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp integrations/codex/kunjin-fund/agents/openai.yaml /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
```

Expected: all commands exit 0 and repository/installed Skill files are byte-identical.

- [ ] **Step 7: Commit acceptance evidence**

```bash
git add docs/audits/2026-07-12-kunjin-phase-c-independent-review.md
git commit -m "docs: audit phase c allocation ranges"
```

## Design Coverage Review

- Strict Phase B gate and `research_only`: Tasks 7, 8, 10, and 11.
- Three abstract layers and fixed stress assumptions: Tasks 1 and 3.
- Capital exclusivity, protected claims, existing stock, monthly no-double-deduction: Tasks 2 and 9.
- Per-item horizons, residual-purpose selection, postponement conflicts: Tasks 2 and 9.
- Zero-return funding states: Task 2.
- Transparent inequalities, ceilings, zero-stock and zero-risk behavior: Task 3.
- Exact local versus amount-free JSON output: Tasks 7 and 8.
- Schema V9, immutable policy/assessments, and atomic migration: Tasks 5 and 6.
- HKDF domain separation, canonical exact payload, and missing-key behavior: Task 4.
- Freshness, binding concurrency, and deterministic recalculation: Tasks 6 and 7.
- Stable technical and financial codes: Tasks 1, 3, 7, and 8.
- README, logging, repository Skill, and installed Skill parity: Task 10.
- Synthetic acceptance, real blocked acceptance, and independent score challenge: Task 11.

The plan intentionally creates no fund classification, current-holding compliance, target point, purchase amount, expected-return forecast, or directional trade label; those remain Phase D/E.
