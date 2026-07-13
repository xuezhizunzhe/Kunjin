# KunJin Phase B Suitability Safety Gates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, versioned, privacy-preserving suitability assessment that blocks unsafe financial foundations, records constraints, and exposes no exact financial amounts through JSON or the Codex Skill.

**Architecture:** Extend `kunjin.suitability` with immutable assessment models, a canonical policy V1, a pure calculation engine, domain-separated assessment encryption, schema V8 persistence, and a service that binds every result to the active encrypted profile and policy version. Add local exact and amount-free JSON CLI views; directional fund requests remain `research_only` until later phases.

**Tech Stack:** Python 3.9+, frozen dataclasses, `Decimal`, `datetime`, canonical JSON, SQLite triggers, `cryptography` AES-256-GCM/HKDF/HMAC, macOS Keychain, `unittest`, Ruff, existing KunJin CLI envelopes.

---

## Scope And File Map

Create:

- `src/kunjin/suitability/policy.py`: policy V1 parameters, canonical JSON, checksum, and policy validation.
- `src/kunjin/suitability/engine.py`: pure debt, reserve, obligation, goal, cash-flow, risk-conflict, and state aggregation rules.
- `src/kunjin/suitability/assessment_serialization.py`: canonical exact-result serialization without floats.
- `tests/unit/test_suitability_policy.py`: policy version/checksum/validation tests.
- `tests/unit/test_suitability_engine.py`: rule boundaries, aggregation, and safety-monotonicity tests.
- `tests/unit/test_suitability_assessment_crypto.py`: assessment cipher domain-separation and tamper tests.
- `tests/unit/test_schema_v8.py`: migration, constraints, and immutability tests.
- `tests/unit/test_suitability_assessment_store.py`: policy and assessment persistence tests.
- `tests/unit/test_suitability_assessment_service.py`: orchestration, freshness, privacy, and technical-error tests.
- `docs/audits/2026-07-12-kunjin-phase-b-independent-review.md`: evidence-backed independent review created only after verification.

Modify:

- `src/kunjin/suitability/models.py`: debt-type and assessment result types.
- `src/kunjin/suitability/__init__.py`: public exports.
- `src/kunjin/suitability/editor.py`: normalized debt choices and local warning for unsupported categories.
- `src/kunjin/suitability/crypto.py`: assessment ciphertext and domain-separated cipher.
- `src/kunjin/suitability/store.py`: policy and immutable assessment stores.
- `src/kunjin/suitability/service.py`: loaded-profile contract and suitability orchestration.
- `src/kunjin/storage/schema.py`: schema version 8.
- `src/kunjin/storage/repository.py`: schema V8 migration.
- `src/kunjin/cli.py`: suitability parser, context wiring, routing, and privacy views.
- `src/kunjin/logging.py`: exact derived-value redaction keys.
- `tests/unit/test_suitability_editor.py`: normalized debt prompt coverage.
- `tests/unit/test_suitability_crypto.py`: shared-key compatibility regression coverage.
- `tests/unit/test_suitability_service.py`: loaded-profile metadata coverage.
- `tests/unit/test_logging.py`: new amount-key redaction coverage.
- `tests/integration/test_cli.py`: local/JSON contracts and stable error behavior.
- `tests/test_smoke.py`: suitability command packaging.
- `README.md`: Phase B commands, states, privacy, and limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: Phase B decision flow and continued `research_only` rule.
- `integrations/codex/kunjin-fund/agents/openai.yaml`: default prompt alignment without recommendation claims.

Do not modify or remove the user's unrelated untracked design and ledger files. Do not put real personal amounts in tests, logs, docs, commit messages, or agent prompts.

## Task 1: Add Assessment Models And Canonical Policy V1

**Files:**
- Modify: `src/kunjin/suitability/models.py`
- Create: `src/kunjin/suitability/policy.py`
- Modify: `src/kunjin/suitability/__init__.py`
- Create: `tests/unit/test_suitability_policy.py`

- [ ] **Step 1: Write failing policy and model tests**

Create tests that require stable enums, immutable results, canonical Decimal strings, checksum stability, and parameter validation:

```python
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal

from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
    DebtType,
)
from kunjin.suitability.policy import SuitabilityPolicyV1


class SuitabilityPolicyTest(unittest.TestCase):
    def test_policy_v1_has_stable_canonical_checksum(self) -> None:
        first = SuitabilityPolicyV1()
        second = SuitabilityPolicyV1()
        self.assertEqual(first.version, "1")
        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.checksum(), second.checksum())
        self.assertIn(b'"high_interest_annual_rate":"0.08"', first.canonical_json())

    def test_policy_rejects_invalid_thresholds(self) -> None:
        with self.assertRaisesRegex(ValueError, "high-interest rate"):
            SuitabilityPolicyV1(high_interest_annual_rate=Decimal("-0.01")).validate()

    def test_assessment_models_are_immutable(self) -> None:
        result = AssessmentResult(
            status=AssessmentStatus.BLOCKED,
            hard_blocks=(BlockReason.PROFILE_MISSING,),
            constraints=(),
            required_reserve_months=0,
            risk_answers_consistent=True,
            debt_count=0,
            obligation_count=0,
            goal_count=0,
            amounts=AssessmentAmounts.zero(),
        )
        with self.assertRaises(FrozenInstanceError):
            result.status = AssessmentStatus.CONSTRAINED

    def test_debt_types_and_reason_codes_are_stable(self) -> None:
        self.assertEqual(DebtType.CONSUMER_LOAN.value, "consumer_loan")
        self.assertEqual(BlockReason.HIGH_INTEREST_DEBT.value, "high_interest_debt")
        self.assertEqual(
            ConstraintReason.MONTHLY_CEILING_CONSTRAINED.value,
            "monthly_ceiling_constrained",
        )
```

- [ ] **Step 2: Run the focused tests and verify red**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_policy -v
```

Expected: import errors for the new policy and assessment types.

- [ ] **Step 3: Implement stable public types**

Add string enums for:

```python
class DebtType(str, Enum):
    MORTGAGE = "mortgage"
    AUTO_LOAN = "auto_loan"
    CREDIT_CARD = "credit_card"
    CONSUMER_LOAN = "consumer_loan"
    PERSONAL_LOAN = "personal_loan"
    STUDENT_LOAN = "student_loan"
    BUSINESS_LOAN = "business_loan"
    OTHER = "other"


class AssessmentStatus(str, Enum):
    BLOCKED = "blocked"
    CONSTRAINED = "constrained"
    READY_FOR_ALLOCATION = "ready_for_allocation"
```

Define `BlockReason` with exactly these values:

```text
profile_missing
profile_invalidated
profile_stale
debt_type_unknown
debt_delinquent
revolving_credit
high_interest_debt
emergency_reserve_shortfall
obligation_overdue
goal_overdue
critical_goal_shortfall
no_monthly_investable_cash_flow
profile_conflict
```

Define `ConstraintReason` with exactly:

```text
near_term_obligation_gap
near_term_goal_gap
monthly_ceiling_constrained
```

Add frozen `AssessmentAmounts` fields for verified reserve, required reserve, reserve shortfall, required monthly obligation saving, required monthly goal saving, monthly safety residual, and safe monthly ceiling. Add `zero()` and strict finite-Decimal validation. Add frozen `AssessmentResult` with status, reason tuples, amount-free counts, reserve months, consistency boolean, and `AssessmentAmounts`; validate that aggregation matches reasons. Add `safe_summary()` returning only `required_reserve_months`, `risk_answers_consistent`, `debt_count`, `obligation_count`, and `goal_count`.

- [ ] **Step 4: Implement canonical policy V1**

Define a frozen `SuitabilityPolicyV1` with `Decimal("0.08")`, 6/9/12 reserve months, 1/3-year horizons, 24-hour freshness, supported/consumer debt sets, cent quantization, and an aware fixed `effective_at=datetime(2026, 7, 12, tzinfo=timezone.utc)`. Implement `validate()`, `canonical_json()`, and `checksum()` using sorted compact JSON and SHA-256. Decimal values must be encoded as strings and sets as sorted arrays.

- [ ] **Step 5: Export the new public types and run green tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_policy tests.unit.test_suitability_models -v
.venv/bin/ruff check src/kunjin/suitability/models.py src/kunjin/suitability/policy.py tests/unit/test_suitability_policy.py
```

Expected: all focused tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 6: Commit the policy boundary**

```bash
git add src/kunjin/suitability/models.py src/kunjin/suitability/policy.py src/kunjin/suitability/__init__.py tests/unit/test_suitability_policy.py
git commit -m "feat: define suitability policy and assessment models"
```

## Task 2: Implement Debt And Emergency-Reserve Gates

**Files:**
- Create: `src/kunjin/suitability/engine.py`
- Create: `tests/unit/test_suitability_engine.py`

- [ ] **Step 1: Write failing debt boundary tests**

Use `dataclasses.replace()` and `valid_profile()` from `tests.unit.test_suitability_models`. Cover exact debt names, unknown names, 7.99% versus 8%, mortgage exclusion, delinquency, revolving interest, and zero-principal unknown debt.

```python
def test_unsecured_consumer_debt_blocks_at_eight_percent(self) -> None:
    profile = replace(
        valid_profile(),
        debts=(Debt("consumer_loan", D("1000"), D("0.08"), D("100"), None, False, False),),
    )
    result = evaluate(profile, SuitabilityPolicyV1(), NOW)
    self.assertIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)


def test_mortgage_is_not_rate_only_blocked(self) -> None:
    profile = replace(
        valid_profile(),
        debts=(Debt("mortgage", D("1000"), D("0.12"), D("100"), None, False, False),),
    )
    result = evaluate(profile, SuitabilityPolicyV1(), NOW)
    self.assertNotIn(BlockReason.HIGH_INTEREST_DEBT, result.hard_blocks)


def test_unknown_nonzero_debt_type_blocks(self) -> None:
    profile = replace(
        valid_profile(),
        debts=(Debt("住房借款", D("1000"), D("0.03"), D("100"), None, False, False),),
    )
    result = evaluate(profile, SuitabilityPolicyV1(), NOW)
    self.assertIn(BlockReason.DEBT_TYPE_UNKNOWN, result.hard_blocks)
```

- [ ] **Step 2: Write failing reserve tests**

Cover `min(designated, liquid)`, debt service in monthly safety cost, 6/9/12 month selection, one-month material-obligation boundary, and strict reserve shortfall.

```python
def test_verified_reserve_uses_smaller_supported_amount(self) -> None:
    profile = replace(
        valid_profile(),
        immediately_available_cash=D("40000"),
        cash_like_assets=D("10000"),
        emergency_reserve=D("80000"),
    )
    result = evaluate(profile, SuitabilityPolicyV1(), NOW)
    self.assertEqual(result.amounts.verified_emergency_reserve, D("50000.00"))


def test_variable_income_requires_nine_months(self) -> None:
    profile = replace(valid_profile(), income_stability=IncomeStability.VARIABLE)
    result = evaluate(profile, SuitabilityPolicyV1(), NOW)
    self.assertEqual(result.required_reserve_months, 9)
```

- [ ] **Step 3: Run the tests and verify red**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_engine -v
```

Expected: import failure for `kunjin.suitability.engine`.

- [ ] **Step 4: Implement the pure engine debt and reserve helpers**

Implement `evaluate(profile, policy, assessed_at)` with no I/O. Add internal helpers `_debt_reasons()`, `_required_reserve_months()`, `_unfunded_obligations()`, `_money_up()`, and `_money_down()`. Evaluate every debt and retain every applicable reason. Use exact string matching against policy debt values; never lowercase, translate, or substring-match profile input.

Compute:

```python
liquid = profile.immediately_available_cash + profile.cash_like_assets
verified = min(profile.emergency_reserve, liquid)
monthly_safety_cost = (
    profile.monthly_essential_expenses + profile.monthly_required_debt_service
)
required = monthly_safety_cost * Decimal(required_months) + obligations_within_one_year
shortfall = max(Decimal("0"), required - verified)
```

Quantize required values upward and available values downward to `Decimal("0.01")`. Add `EMERGENCY_RESERVE_SHORTFALL` only when the quantized shortfall is positive.

- [ ] **Step 5: Run debt/reserve tests and lint**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_engine -v
.venv/bin/ruff check src/kunjin/suitability/engine.py tests/unit/test_suitability_engine.py
```

Expected: all implemented debt and reserve tests pass.

- [ ] **Step 6: Commit the first engine slice**

```bash
git add src/kunjin/suitability/engine.py tests/unit/test_suitability_engine.py
git commit -m "feat: add debt and reserve suitability gates"
```

## Task 3: Complete Obligation, Goal, Cash-Flow, Risk, And Monotonic Rules

**Files:**
- Modify: `src/kunjin/suitability/engine.py`
- Modify: `tests/unit/test_suitability_engine.py`

- [ ] **Step 1: Add failing horizon and cash-flow tests**

Add tests for past-due gaps, exactly one year, exactly three years, fully funded short-term items, priority-1 monthly saving, lower-priority exclusion, residual zero, constrained ceiling, and aggregation priority.

```python
def test_unpostponable_priority_one_goal_within_one_year_blocks(self) -> None:
    goal = FinancialGoal(
        "home deposit", D("12000"), date(2027, 1, 1), 1, D("0"), False, False
    )
    result = evaluate(replace(valid_profile(), goals=(goal,)), POLICY, NOW)
    self.assertIn(BlockReason.CRITICAL_GOAL_SHORTFALL, result.hard_blocks)


def test_positive_residual_below_personal_ceiling_is_constrained(self) -> None:
    profile = replace(
        valid_profile(),
        monthly_net_income=D("10000"),
        monthly_essential_expenses=D("7000"),
        monthly_required_debt_service=D("1000"),
        minimum_monthly_cash_buffer=D("1000"),
        monthly_investment_ceiling=D("2000"),
        goals=(),
        obligations=(),
        debts=(),
    )
    result = evaluate(profile, POLICY, NOW)
    self.assertEqual(result.status, AssessmentStatus.CONSTRAINED)
    self.assertIn(
        ConstraintReason.MONTHLY_CEILING_CONSTRAINED,
        result.constraints,
    )
```

- [ ] **Step 2: Add failing risk-conflict matrix tests**

Cover non-monotonic reactions, redeem threshold versus declared tolerance, sub-10% tolerance with hold at 10%, zero CNY loss with hold, and a consistent defensive profile.

- [ ] **Step 3: Add failing safety-monotonicity tests**

Create a severity mapping `ready_for_allocation=0`, `constrained=1`, `blocked=2`. For otherwise identical profiles assert that less reserve, more debt, higher supported consumer rate, shorter goal date, larger obligation gap, lower income, and higher essential expenses never reduce severity.

- [ ] **Step 4: Run the new tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_engine -v
```

Expected: failures for unimplemented goal, obligation, cash-flow, risk-conflict, and monotonic rules.

- [ ] **Step 5: Implement the remaining pure rules**

Use calendar contribution periods:

```python
def _contribution_periods(as_of: date, due: date) -> int:
    return max(1, 12 * (due.year - as_of.year) + due.month - as_of.month + 1)
```

Sum required monthly savings for all positive obligation gaps within three years and all positive priority-1 goal gaps. Round required monthly savings up to cents. Compute monthly residual and safe ceiling exactly as the spec. Add all block and constraint codes, deduplicate them while preserving policy order, then derive the three-state result.

Risk action severity must be non-decreasing from 10% to 30%. Implement the three additional conflict rules from the spec without inventing a numerical score or changing profile answers.

- [ ] **Step 6: Run the full engine suite and static checks**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_engine tests.unit.test_suitability_policy -v
.venv/bin/ruff check src/kunjin/suitability/engine.py src/kunjin/suitability/policy.py src/kunjin/suitability/models.py tests/unit/test_suitability_engine.py
```

Expected: all boundary and monotonic tests pass; Ruff is clean.

- [ ] **Step 7: Commit the complete engine**

```bash
git add src/kunjin/suitability/engine.py tests/unit/test_suitability_engine.py
git commit -m "feat: complete suitability safety engine"
```

## Task 4: Restrict Local Debt Entry To Normalized Types

**Files:**
- Modify: `src/kunjin/suitability/editor.py`
- Modify: `tests/unit/test_suitability_editor.py`

- [ ] **Step 1: Add failing normalized-choice tests**

Test that debt type uses the exact enum list, rejects arbitrary text, stores the normalized string, and warns that `business_loan` and `other` cannot pass Policy V1.

- [ ] **Step 2: Run the editor tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_editor -v
```

Expected: current editor accepts arbitrary debt text, so the new test fails.

- [ ] **Step 3: Replace free-text debt entry**

Change debt construction to:

```python
debt_type = self._choice("Debt type", DebtType)
if debt_type in (DebtType.BUSINESS_LOAN, DebtType.OTHER):
    self._writer(
        "This debt type cannot pass suitability policy v1 until its risk type is clarified."
    )
debt = Debt(
    debt_type=debt_type.value,
    outstanding_principal=self._decimal("Debt outstanding principal"),
    effective_annual_rate=self._percentage("Debt effective annual rate (%)"),
    monthly_payment=self._decimal("Debt monthly payment"),
    maturity_date=(
        self._date("Debt maturity date")
        if self._boolean("Debt has a maturity date?")
        else None
    ),
    delinquent=self._boolean("Debt is delinquent?"),
    revolving_interest=self._boolean("Debt charges revolving interest?"),
)
```

Do not change the encrypted profile wire format; legacy exact normalized strings remain decodable.

- [ ] **Step 4: Run editor, model, and serialization tests**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_editor tests.unit.test_suitability_models -v
.venv/bin/ruff check src/kunjin/suitability/editor.py tests/unit/test_suitability_editor.py
```

- [ ] **Step 5: Commit the input normalization**

```bash
git add src/kunjin/suitability/editor.py tests/unit/test_suitability_editor.py
git commit -m "feat: normalize suitability debt input"
```

## Task 5: Add Domain-Separated Assessment Encryption

**Files:**
- Modify: `src/kunjin/suitability/crypto.py`
- Create: `src/kunjin/suitability/assessment_serialization.py`
- Create: `tests/unit/test_suitability_assessment_crypto.py`
- Modify: `tests/unit/test_suitability_crypto.py`

- [ ] **Step 1: Write failing serialization and cipher tests**

Cover canonical Decimal strings, float rejection, exact-key rejection, round trip, random nonce, tamper failure, missing key, wrong metadata, and profile/assessment domain separation.

```python
def test_assessment_round_trip_uses_distinct_domain(self) -> None:
    key_store = FakeProfileKeyStore()
    profile_cipher = ProfileCipher(key_store)
    assessment_cipher = AssessmentCipher(key_store)
    encoded = encode_assessment_amounts(AssessmentAmounts.zero())
    encrypted = assessment_cipher.encrypt(encoded)
    self.assertEqual(assessment_cipher.decrypt(encrypted), encoded)
    self.assertNotEqual(encrypted.keyed_fingerprint, profile_cipher.encrypt(encoded).keyed_fingerprint)
```

- [ ] **Step 2: Run the focused tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_assessment_crypto -v
```

- [ ] **Step 3: Implement canonical exact-result serialization**

Encode exactly the seven `AssessmentAmounts` fields as sorted compact JSON with Decimal strings. Decode with `parse_float` and `parse_constant` rejection, exact-key validation, finite Decimal validation, and `AssessmentAmounts.validate()`.

- [ ] **Step 4: Implement `EncryptedAssessment` and `AssessmentCipher`**

Use:

```python
ALGORITHM = "AES-256-GCM"
KEY_VERSION = "1"
KEY_INFO = b"kunjin/suitability-assessment/encryption/v1"
ASSOCIATED_DATA = b"kunjin/suitability-assessment/v1"
FINGERPRINT_INFO = b"kunjin/suitability-assessment/fingerprint/v1"
```

Derive the assessment AES key from the existing 32-byte Keychain master key with HKDF-SHA256. Derive a separate fingerprint key. Keep the existing profile cipher contract byte-compatible. Decryption must never call `load_or_create_key()`.

Expose `fingerprint(payload: bytes) -> str` on `AssessmentCipher`. It loads an existing key only, derives `FINGERPRINT_INFO`, and returns a lowercase 64-character HMAC-SHA256 digest.

- [ ] **Step 5: Run all crypto tests and lint**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_crypto tests.unit.test_suitability_assessment_crypto -v
.venv/bin/ruff check src/kunjin/suitability/crypto.py src/kunjin/suitability/assessment_serialization.py tests/unit/test_suitability_assessment_crypto.py
```

- [ ] **Step 6: Commit assessment encryption**

```bash
git add src/kunjin/suitability/crypto.py src/kunjin/suitability/assessment_serialization.py tests/unit/test_suitability_crypto.py tests/unit/test_suitability_assessment_crypto.py
git commit -m "feat: encrypt suitability assessment amounts"
```

## Task 6: Add Schema Version 8

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v8.py`
- Modify: `tests/unit/test_schema_v7.py`

- [ ] **Step 1: Write failing V8 migration and immutability tests**

Require both new tables, schema versions 1 through 8, V7 data preservation, policy uniqueness, valid assessment status, foreign keys, update rejection, and delete rejection.

- [ ] **Step 2: Run V8 tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_schema_v8 -v
```

Expected: `SCHEMA_V8` import failure.

- [ ] **Step 3: Add `SCHEMA_V8`**

Create `suitability_policy_versions` and `suitability_assessments` exactly as specified. Store encrypted values in `encrypted_amount_results`; `safe_summary_json`, `hard_blocks_json`, and `constraints_json` are plaintext amount-free JSON. Add triggers:

```sql
CREATE TRIGGER suitability_policy_no_update
BEFORE UPDATE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER suitability_policy_no_delete
BEFORE DELETE ON suitability_policy_versions
BEGIN
    SELECT RAISE(ABORT, 'suitability policies are immutable');
END;

CREATE TRIGGER suitability_assessment_no_update
BEFORE UPDATE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;

CREATE TRIGGER suitability_assessment_no_delete
BEFORE DELETE ON suitability_assessments
BEGIN
    SELECT RAISE(ABORT, 'suitability assessments are immutable');
END;
```

Set `SCHEMA_VERSION = 8`, import `SCHEMA_V8` in the repository, execute it after V7, and record migration version 8 explicitly.

- [ ] **Step 4: Run every schema migration test**

```bash
.venv/bin/python -m unittest tests.unit.test_schema_v2 tests.unit.test_schema_v4 tests.unit.test_schema_v5 tests.unit.test_schema_v6 tests.unit.test_schema_v7 tests.unit.test_schema_v8 -v
.venv/bin/ruff check src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v8.py
```

- [ ] **Step 5: Commit schema V8**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v7.py tests/unit/test_schema_v8.py
git commit -m "feat: add immutable suitability assessment schema"
```

## Task 7: Implement Policy And Assessment Stores

**Files:**
- Modify: `src/kunjin/suitability/store.py`
- Create: `tests/unit/test_suitability_assessment_store.py`

- [ ] **Step 1: Write failing store tests**

Cover policy insert, idempotent same-content ensure, different-content version rejection, assessment insert, latest matching lookup, metadata-only history, strict ISO datetimes, invalid JSON rejection, and encrypted field round trip.

- [ ] **Step 2: Run the store tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_assessment_store -v
```

- [ ] **Step 3: Implement immutable store records**

Add frozen records `PolicyVersionRecord`, `AssessmentMetadata`, and `StoredEncryptedAssessment`. Implement:

```python
class SuitabilityPolicyStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def ensure(self, policy: SuitabilityPolicyV1) -> PolicyVersionRecord:
        policy.validate()
        canonical = policy.canonical_json().decode("utf-8")
        checksum = policy.checksum()
        with self._repository.connect() as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO suitability_policy_versions("
                "version, canonical_policy_json, policy_checksum, effective_at, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    policy.version,
                    canonical,
                    checksum,
                    policy.effective_at.isoformat(),
                    policy.effective_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM suitability_policy_versions WHERE version = ?",
                (policy.version,),
            ).fetchone()
        if (
            row is None
            or row["canonical_policy_json"] != canonical
            or row["policy_checksum"] != checksum
        ):
            raise ValueError("suitability policy version content does not match")
        return _policy_record(row)

    def get(self, version: str) -> Optional[PolicyVersionRecord]:
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM suitability_policy_versions WHERE version = ?",
                (version,),
            ).fetchone()
        return None if row is None else _policy_record(row)


class SuitabilityAssessmentStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def insert(
        self,
        profile_version_id: int,
        policy_version: str,
        input_fingerprint: str,
        result: AssessmentResult,
        encrypted: EncryptedAssessment,
        assessed_at: datetime,
        valid_until: datetime,
    ) -> AssessmentMetadata:
        hard_blocks = json.dumps(
            [item.value for item in result.hard_blocks], separators=(",", ":")
        )
        constraints = json.dumps(
            [item.value for item in result.constraints], separators=(",", ":")
        )
        safe_summary = json.dumps(
            result.safe_summary(), separators=(",", ":"), sort_keys=True
        )
        with self._repository.connect() as connection, connection:
            cursor = connection.execute(
                "INSERT INTO suitability_assessments("
                "profile_version_id, policy_version, input_fingerprint, status, "
                "hard_blocks_json, constraints_json, safe_summary_json, "
                "encrypted_amount_results, encryption_algorithm, encryption_key_version, "
                "nonce, keyed_payload_fingerprint, assessed_at, valid_until, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile_version_id,
                    policy_version,
                    input_fingerprint,
                    result.status.value,
                    hard_blocks,
                    constraints,
                    safe_summary,
                    encrypted.ciphertext,
                    encrypted.algorithm,
                    encrypted.key_version,
                    encrypted.nonce,
                    encrypted.keyed_fingerprint,
                    assessed_at.isoformat(),
                    valid_until.isoformat(),
                    assessed_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM suitability_assessments WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _assessment_metadata(row)

    def latest_for(
        self, profile_version_id: int, policy_version: str
    ) -> Optional[StoredEncryptedAssessment]:
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM suitability_assessments "
                "WHERE profile_version_id = ? AND policy_version = ? "
                "ORDER BY assessed_at DESC, id DESC LIMIT 1",
                (profile_version_id, policy_version),
            ).fetchone()
        return None if row is None else _stored_assessment(row)

    def history(self) -> Tuple[AssessmentMetadata, ...]:
        with self._repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM suitability_assessments "
                "ORDER BY assessed_at DESC, id DESC"
            ).fetchall()
        return tuple(_assessment_metadata(row) for row in rows)
```

Implement `_policy_record`, `_assessment_metadata`, and `_stored_assessment` with the existing strict ISO-datetime helpers and strict JSON-array/object parsing. Serialize reason tuples as compact JSON arrays and safe summary as amount-free compact JSON. Validate that exact amounts never enter those three plaintext JSON fields.

- [ ] **Step 4: Run store and schema tests**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_assessment_store tests.unit.test_schema_v8 -v
.venv/bin/ruff check src/kunjin/suitability/store.py tests/unit/test_suitability_assessment_store.py
```

- [ ] **Step 5: Commit the persistence layer**

```bash
git add src/kunjin/suitability/store.py tests/unit/test_suitability_assessment_store.py
git commit -m "feat: persist suitability policies and assessments"
```

## Task 8: Implement Suitability Service Orchestration

**Files:**
- Modify: `src/kunjin/suitability/service.py`
- Modify: `src/kunjin/suitability/store.py`
- Modify: `tests/unit/test_suitability_service.py`
- Create: `tests/unit/test_suitability_assessment_service.py`

- [ ] **Step 1: Add failing loaded-profile contract tests**

Add `LoadedProfile` with metadata, decoded profile, and encrypted keyed fingerprint. Require `ProfileService.load_active()` to return it while preserving `load_active_profile()` compatibility. Add `ProfileStore.latest_metadata()` so a missing active profile can distinguish invalidated from never-created state without decrypting terminal history.

- [ ] **Step 2: Add failing suitability service tests**

Cover:

- Missing profile returns transient `blocked/profile_missing`, exit-safe view, and no assessment row.
- Invalidated and stale profiles return blocked readiness results.
- Fresh valid profile persists an assessment bound to profile/policy.
- `valid_until` is the earlier of 24 hours and profile expiry.
- Profile version changes make old status stale.
- Policy checksum mismatch raises `policy_unavailable`.
- Missing key and tampered assessment raise `encrypted_profile_unavailable`.
- Safe JSON contains no amount strings or private names.
- Local view contains derived amounts but not keys, nonce, or ciphertext.

- [ ] **Step 3: Run the service tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_service tests.unit.test_suitability_assessment_service -v
```

- [ ] **Step 4: Implement stable service errors and views**

Add:

```python
class SuitabilityPolicyError(RuntimeError):
    code = "policy_unavailable"


class SuitabilityAssessmentError(RuntimeError):
    code = "assessment_calculation_failed"
```

Add frozen execution/view objects so exact values and safe JSON are separate methods, not a dictionary later redacted by key deletion.

- [ ] **Step 5: Implement `SuitabilityService`**

Constructor dependencies are explicit: `ProfileService`, policy store, assessment store, assessment cipher, policy, and clock. `assess()` authenticates the profile before policy/engine use, ensures the policy row, computes the pure result, encrypts canonical exact amounts, derives a keyed input fingerprint, persists when a profile version exists, and returns the execution object.

Build the fingerprint input exactly as:

```python
fingerprint_input = "|".join(
    (
        str(loaded.metadata.id),
        loaded.encrypted_keyed_fingerprint,
        policy.checksum(),
        assessed_at.date().isoformat(),
    )
).encode("ascii")
input_fingerprint = assessment_cipher.fingerprint(fingerprint_input)
```

`status()` authenticates the current profile and latest matching assessment, validates freshness and fingerprint binding, and decrypts/validates exact results before reporting fresh. `history()` is metadata-only.

- [ ] **Step 6: Run all suitability service, store, and crypto tests**

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_service tests.unit.test_suitability_assessment_service tests.unit.test_suitability_assessment_store tests.unit.test_suitability_assessment_crypto -v
.venv/bin/ruff check src/kunjin/suitability/service.py src/kunjin/suitability/store.py tests/unit/test_suitability_assessment_service.py
```

- [ ] **Step 7: Commit orchestration**

```bash
git add src/kunjin/suitability/service.py src/kunjin/suitability/store.py tests/unit/test_suitability_service.py tests/unit/test_suitability_assessment_service.py
git commit -m "feat: orchestrate suitability assessments"
```

## Task 9: Add Suitability CLI Commands And Privacy Views

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser and CLI contract tests**

Require these commands:

```text
kunjin suitability assess
kunjin --json suitability assess
kunjin --json suitability status
kunjin --json suitability history
```

Test all three financial states, metadata-only status/history, local exact output, JSON amount absence, technical error envelopes, and exit zero for a completed `blocked` financial assessment.

- [ ] **Step 2: Run focused CLI tests and verify red**

```bash
.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v
```

Expected: parser rejects the new top-level command.

- [ ] **Step 3: Wire services into `ApplicationContext`**

Add optional `suitability_service`. In `build_context()`, share one `ProfileKeyStore` between `ProfileCipher` and `AssessmentCipher`, create policy and assessment stores, and inject `SuitabilityPolicyV1()`.

- [ ] **Step 4: Add parser and routing**

Add `suitability` to `_TOP_LEVEL_COMMANDS`, then parsers for `assess`, `status`, and `history`. Route:

```python
if args.command == "suitability":
    if context.suitability_service is None:
        raise CliUsageError("suitability service is unavailable")
    if args.suitability_command == "assess":
        execution = context.suitability_service.assess()
        data = execution.safe_json() if args.json_output else execution.local_view()
        return envelope("suitability.assess", data)
    if args.suitability_command == "status":
        return envelope("suitability.status", context.suitability_service.status())
    return envelope(
        "suitability.history",
        {"assessments": context.suitability_service.history()},
    )
```

Add the new stable technical exceptions to `run()` handling. Do not convert financial block reasons into envelope errors.

- [ ] **Step 5: Verify amount-free JSON with synthetic sentinels**

Use synthetic values `73129`, `84217`, and `95311` only in temporary test profiles. Assert they appear in the explicit local `assess` data and do not appear in JSON `assess`, `status`, or `history` payload serialization.

- [ ] **Step 6: Run CLI and smoke suites**

```bash
.venv/bin/python -m unittest tests.integration.test_cli tests.test_smoke -v
.venv/bin/ruff check src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
```

- [ ] **Step 7: Commit the CLI contract**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose suitability safety commands"
```

## Task 10: Strengthen Privacy, README, And Codex Skill Boundaries

**Files:**
- Modify: `src/kunjin/logging.py`
- Modify: `tests/unit/test_logging.py`
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `integrations/codex/kunjin-fund/agents/openai.yaml`

- [ ] **Step 1: Add failing redaction tests**

Require redaction for:

```text
verified_emergency_reserve
required_emergency_reserve
emergency_reserve_shortfall
required_monthly_obligation_saving
required_monthly_goal_saving
monthly_safety_residual
safe_monthly_ceiling
encrypted_amount_results
```

Assert sentinel values do not survive exception and log redaction.

- [ ] **Step 2: Run logging tests and verify red**

```bash
.venv/bin/python -m unittest tests.unit.test_logging -v
```

- [ ] **Step 3: Add narrow redaction keys and rerun tests**

Append only the exact Phase B derived-value keys to `_SECRET_KEYS`; do not redact generic words such as `status`, `amount`, `goal`, or `debt` because that would destroy useful non-sensitive diagnostics.

- [ ] **Step 4: Update README**

Document the four commands, three states, 24-hour assessment freshness, exact local versus amount-free JSON behavior, and these explicit limitations:

```text
ready_for_allocation is not a buy recommendation.
Phase B does not calculate an allocation, classify a fund, or approve an amount.
Directional and position-size requests remain research_only until later phases pass.
```

- [ ] **Step 5: Update the repository Skill and default prompt**

Change the profile workflow to call `--json suitability assess`. Preserve exact reason codes and forbid asking for local amounts. The decision table must keep all three states `research_only` until Phase C exists. Add adversarial examples from the spec.

- [ ] **Step 6: Verify wording and install byte-identical Skill files**

Run:

```bash
rg -n "ready_for_allocation|constrained|blocked|research_only|90%|exact" README.md integrations/codex/kunjin-fund
cp integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cp integrations/codex/kunjin-fund/agents/openai.yaml /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
shasum -a 256 integrations/codex/kunjin-fund/SKILL.md /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
shasum -a 256 integrations/codex/kunjin-fund/agents/openai.yaml /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
```

Expected: each repository/installed pair has an identical digest. Copying outside the workspace requires the normal approval path.

- [ ] **Step 7: Run privacy and integration tests**

```bash
.venv/bin/python -m unittest tests.unit.test_logging tests.integration.test_cli -v
.venv/bin/ruff check src/kunjin/logging.py tests/unit/test_logging.py
```

- [ ] **Step 8: Commit documentation and Skill alignment**

```bash
git add src/kunjin/logging.py tests/unit/test_logging.py README.md integrations/codex/kunjin-fund/SKILL.md integrations/codex/kunjin-fund/agents/openai.yaml
git commit -m "docs: align suitability privacy and skill rules"
```

## Task 11: Full Verification, Real Acceptance, And Independent Review

**Files:**
- Create: `docs/audits/2026-07-12-kunjin-phase-b-independent-review.md`
- Review: all Phase B files and current worktree

- [ ] **Step 1: Run the complete automated suite**

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Expected: exit zero with the final test count and `OK`. Record the actual count; do not reuse Phase A's 408 count.

- [ ] **Step 2: Run compilation, full Ruff, and patch checks**

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-phase-b-pycache .venv/bin/python -m compileall -q src tests
.venv/bin/ruff check .
git diff --check
```

Expected: all exit zero; Ruff prints `All checks passed!`.

- [ ] **Step 3: Run isolated synthetic CLI acceptance**

Use temporary `KUNJIN_DATA_DIR` and `KUNJIN_STATE_DIR`, a fake key store inside a test harness, and synthetic sentinel amounts. Run local and JSON assessment paths, then scan the temporary database/state directories:

```bash
rg -a -n '73129|84217|95311' /private/tmp/kunjin-phase-b-data /private/tmp/kunjin-phase-b-state
```

Expected: exit 1 and no matches. Do not use the owner's real values as search terms.

- [ ] **Step 4: Perform real personal metadata acceptance**

The owner manually runs `kunjin suitability assess` and reviews the exact local calculations. Agents must not run or capture this non-JSON command because doing so would expose exact values to the conversation/tool transcript.

After the owner confirms the local view, agents may run only:

```bash
.venv/bin/kunjin --json suitability assess
.venv/bin/kunjin --json suitability status
.venv/bin/kunjin --json suitability history
```

Record only status, profile/policy versions, freshness, reason codes, assessment count, and privacy outcome. Never record exact values or goal names.

- [ ] **Step 5: Perform the independent financial review**

Write findings first. Re-score the same 100-point beginner workflow used in Phase A. Give zero credit to Phase C-E capabilities. Explicitly assess:

- Whether the rules are financially defensible and transparent.
- Whether a stricter input can ever improve the result.
- Whether block reasons are complete and actionable without becoming advice.
- Whether `ready_for_allocation` can be misread as a buy signal.
- Whether JSON, logs, SQLite, tests, docs, or Skill leak exact amounts.
- Whether the Skill can be prompt-injected into bypassing the state.
- Whether KunJin reaches 90% of reasonably automatable beginner purchase help.

Do not increase the score because tests pass; tests only support the capabilities they exercise.

- [ ] **Step 6: Review all diffs and preserve user-owned files**

```bash
git status --short
git diff --stat
git diff -- src/kunjin/suitability src/kunjin/storage/schema.py src/kunjin/storage/repository.py src/kunjin/cli.py src/kunjin/logging.py tests README.md integrations/codex/kunjin-fund docs/audits/2026-07-12-kunjin-phase-b-independent-review.md
git diff --check
```

Confirm that unrelated user-owned untracked documents remain unchanged.

- [ ] **Step 7: Commit the verified audit**

```bash
git add docs/audits/2026-07-12-kunjin-phase-b-independent-review.md
git commit -m "docs: add phase b independent suitability review"
```

If the environment cannot write `.git/index.lock`, do not claim commits were created. Preserve the worktree and report the exact sandbox limitation.
