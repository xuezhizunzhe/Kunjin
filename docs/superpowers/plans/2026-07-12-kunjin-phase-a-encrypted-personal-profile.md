# KunJin Phase A Encrypted Personal Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, encrypted, versioned personal financial profile with interactive confirmation, privacy-preserving status/history commands, and a temporary research-only Skill gate until suitability assessment exists in Phase B.

**Architecture:** Add a focused `kunjin.suitability` package containing immutable profile models, canonical serialization, Keychain-backed AES-GCM encryption, a profile store, a lifecycle service, and an injected-I/O interactive editor. Extend SQLite to schema version 7 and wire only `profile edit`, `profile status`, and `profile history` into the existing CLI; no suitability score, allocation range, or purchase allowance is implemented in this phase.

**Tech Stack:** Python 3.9+, standard-library `dataclasses`/`Decimal`/`json`/`sqlite3`, `cryptography` AES-GCM and HKDF, macOS `/usr/bin/security`, `unittest`, existing KunJin JSON envelope and SQLite repository patterns.

---

## Scope And File Map

Create:

- `src/kunjin/suitability/__init__.py`: public Phase A exports.
- `src/kunjin/suitability/models.py`: validated immutable financial-profile types.
- `src/kunjin/suitability/serialization.py`: canonical JSON encode/decode without floats.
- `src/kunjin/suitability/crypto.py`: Keychain key lifecycle, AES-GCM encryption, keyed fingerprints.
- `src/kunjin/suitability/store.py`: version storage and lifecycle metadata updates.
- `src/kunjin/suitability/service.py`: profile confirmation, status, history, and invalidation orchestration.
- `src/kunjin/suitability/editor.py`: local interactive prompt and explicit confirmation flow.
- `tests/unit/test_suitability_models.py`: validation and serialization tests.
- `tests/unit/test_suitability_crypto.py`: encryption, tamper, missing-key, and fingerprint tests.
- `tests/unit/test_schema_v7.py`: migration and immutable-payload tests.
- `tests/unit/test_suitability_store.py`: profile lifecycle persistence tests.
- `tests/unit/test_suitability_service.py`: service behavior and sensitive-output tests.
- `tests/unit/test_suitability_editor.py`: local prompt, cancellation, and confirmation tests.
- `docs/audits/2026-07-12-kunjin-phase-a-independent-review.md`: evidence-backed Phase A financial audit written only after implementation verification.

Modify:

- `pyproject.toml`: add the audited runtime encryption dependency.
- `setup.py`: keep the verified `setup.py develop` installation path aligned.
- `src/kunjin/storage/schema.py`: add schema version 7 and profile table.
- `src/kunjin/storage/repository.py`: execute schema version 7 migration.
- `src/kunjin/cli.py`: add profile commands and `ProfileService` to `ApplicationContext`.
- `src/kunjin/logging.py`: redact new profile-specific key/value names.
- `tests/unit/test_logging.py`: assert financial values do not survive redaction.
- `tests/integration/test_cli.py`: add stable profile JSON contracts and no-sensitive-value assertions.
- `tests/test_smoke.py`: assert profile commands are packaged and parseable.
- `README.md`: document encrypted profile commands and Phase A limitations.
- `integrations/codex/kunjin-fund/SKILL.md`: add profile workflow and temporary research-only direction gate.
- `integrations/codex/kunjin-fund/agents/openai.yaml`: mention personal suitability readiness in the default prompt without claiming it is implemented.

Do not modify the user's existing untracked files:

- `docs/superpowers/plans/2026-07-11-kunjin-phase-4-personal-ledger.md`
- `docs/superpowers/specs/2026-07-11-kunjin-a-share-intelligence-and-personal-ledger-design.md`

## Task 1: Add The Encryption Dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `setup.py`

- [ ] **Step 1: Record the current dependency state**

Run:

```bash
.venv/bin/python -c "import importlib.util; print(importlib.util.find_spec('cryptography'))"
```

Expected: either `None` or an installed module specification. Record the result in the implementation notes; do not infer that the package is available.

- [ ] **Step 2: Add the same bounded runtime dependency to both packaging paths**

Update `pyproject.toml`:

```toml
dependencies = ["cryptography>=43,<46"]

[project.scripts]
kunjin = "kunjin.cli:main"
```

The existing `kunjin.cli:app` declaration is inconsistent with the actual CLI
entry point and would break a PEP 517 editable install. Correct it in the same
packaging change.

Update `setup.py`:

```python
setup(
    name="kunjin",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages("src"),
    package_data={"kunjin": ["ledger/*.swift"]},
    include_package_data=True,
    install_requires=["cryptography>=43,<46"],
    entry_points={"console_scripts": ["kunjin=kunjin.cli:main"]},
)
```

- [ ] **Step 3: Install the project into the existing virtual environment**

Run:

```bash
.venv/bin/pip install -e .
```

Expected: exit 0 and an installed `cryptography` version inside the declared range. If dependency download is blocked by network sandboxing, rerun with the required approval rather than changing the design to homemade cryptography.

- [ ] **Step 4: Verify the required primitive is importable**

Run:

```bash
.venv/bin/python -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; print(len(AESGCM.generate_key(bit_length=256)))"
```

Expected:

```text
32
```

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml setup.py
git commit -m "build: add profile encryption dependency"
```

## Task 2: Define Immutable Profile Models And Canonical Serialization

**Files:**
- Create: `src/kunjin/suitability/__init__.py`
- Create: `src/kunjin/suitability/models.py`
- Create: `src/kunjin/suitability/serialization.py`
- Create: `tests/unit/test_suitability_models.py`

- [ ] **Step 1: Write failing model validation tests**

Create `tests/unit/test_suitability_models.py` with tests covering a valid complete profile and invalid negative, naive-datetime, percentage, and inconsistent debt values:

```python
from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.suitability.models import (
    Debt,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    RiskReaction,
)
from kunjin.suitability.serialization import decode_profile, encode_profile


NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def valid_profile() -> FinancialProfile:
    return FinancialProfile(
        currency="CNY",
        monthly_net_income=Decimal("12000"),
        monthly_essential_expenses=Decimal("5000"),
        monthly_required_debt_service=Decimal("1500"),
        monthly_investment_ceiling=Decimal("1000"),
        minimum_operating_cash=Decimal("3000"),
        minimum_monthly_cash_buffer=Decimal("1000"),
        income_stability=IncomeStability.STABLE,
        income_interruption_risk=False,
        immediately_available_cash=Decimal("50000"),
        cash_like_assets=Decimal("10000"),
        emergency_reserve=Decimal("40000"),
        low_risk_fixed_income_assets=Decimal("5000"),
        manual_equity_fund_assets=Decimal("0"),
        manual_bond_fund_assets=Decimal("0"),
        manual_sector_fund_assets=Decimal("0"),
        dependents=0,
        other_volatile_assets=Decimal("0"),
        maximum_tolerable_loss=Decimal("20000"),
        maximum_tolerable_drawdown=Decimal("0.20"),
        reaction_10=RiskReaction.HOLD,
        reaction_20=RiskReaction.HOLD,
        reaction_30=RiskReaction.REDUCE,
        experienced_material_loss=False,
        understands_multi_year_recovery=True,
        can_postpone_goal_use=True,
        debts=(
            Debt(
                debt_type="mortgage",
                outstanding_principal=Decimal("500000"),
                effective_annual_rate=Decimal("0.035"),
                monthly_payment=Decimal("1500"),
                maturity_date=date(2045, 1, 1),
                delinquent=False,
                revolving_interest=False,
            ),
        ),
        obligations=(
            PlannedObligation(
                name="education",
                amount=Decimal("10000"),
                due_date=date(2027, 9, 1),
                amount_already_reserved=Decimal("3000"),
            ),
        ),
        goals=(
            FinancialGoal(
                name="long-term growth",
                target_amount=Decimal("200000"),
                target_date=date(2034, 1, 1),
                priority=1,
                amount_already_reserved=Decimal("20000"),
                temporary_principal_loss_acceptable=True,
                use_date_can_be_postponed=True,
            ),
        ),
        confirmed_at=NOW,
    )


class SuitabilityModelsTest(unittest.TestCase):
    def test_complete_profile_round_trips_without_float_values(self) -> None:
        profile = valid_profile()
        profile.validate()
        encoded = encode_profile(profile)
        self.assertNotIn(b"12000.0", encoded)
        self.assertEqual(decode_profile(encoded), profile)

    def test_negative_amount_is_rejected(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(**{**profile.__dict__, "emergency_reserve": Decimal("-1")})
        with self.assertRaisesRegex(ValueError, "emergency reserve cannot be negative"):
            invalid.validate()

    def test_drawdown_must_be_a_fraction(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(
            **{**profile.__dict__, "maximum_tolerable_drawdown": Decimal("20")}
        )
        with self.assertRaisesRegex(ValueError, "drawdown must be between zero and one"):
            invalid.validate()

    def test_confirmed_at_must_be_timezone_aware(self) -> None:
        profile = valid_profile()
        invalid = FinancialProfile(
            **{**profile.__dict__, "confirmed_at": datetime(2026, 7, 12, 12)}
        )
        with self.assertRaisesRegex(ValueError, "confirmed_at must be timezone-aware"):
            invalid.validate()

    def test_reserved_obligation_cannot_exceed_amount(self) -> None:
        obligation = PlannedObligation(
            "education", Decimal("100"), date(2027, 1, 1), Decimal("101")
        )
        with self.assertRaisesRegex(ValueError, "reserved obligation amount"):
            obligation.validate()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_models -v
```

Expected: import failure for `kunjin.suitability`.

- [ ] **Step 3: Implement the immutable models**

Create `src/kunjin/suitability/models.py` with frozen dataclasses and explicit validation. Use these exact enums and public fields:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple


class IncomeStability(str, Enum):
    STABLE = "stable"
    VARIABLE = "variable"
    UNSTABLE = "unstable"


class RiskReaction(str, Enum):
    HOLD = "hold"
    REDUCE = "reduce"
    REDEEM = "redeem"


def _non_negative(value: Decimal, name: str) -> None:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class Debt:
    debt_type: str
    outstanding_principal: Decimal
    effective_annual_rate: Decimal
    monthly_payment: Decimal
    maturity_date: Optional[date]
    delinquent: bool
    revolving_interest: bool

    def validate(self) -> None:
        if not self.debt_type.strip():
            raise ValueError("debt type is required")
        _non_negative(self.outstanding_principal, "outstanding principal")
        _non_negative(self.effective_annual_rate, "effective annual rate")
        _non_negative(self.monthly_payment, "monthly payment")


@dataclass(frozen=True)
class PlannedObligation:
    name: str
    amount: Decimal
    due_date: date
    amount_already_reserved: Decimal

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("obligation name is required")
        _non_negative(self.amount, "obligation amount")
        _non_negative(self.amount_already_reserved, "reserved obligation amount")
        if self.amount_already_reserved > self.amount:
            raise ValueError("reserved obligation amount cannot exceed obligation amount")


@dataclass(frozen=True)
class FinancialGoal:
    name: str
    target_amount: Decimal
    target_date: date
    priority: int
    amount_already_reserved: Decimal
    temporary_principal_loss_acceptable: bool
    use_date_can_be_postponed: bool

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("goal name is required")
        _non_negative(self.target_amount, "goal target amount")
        _non_negative(self.amount_already_reserved, "goal reserved amount")
        if self.amount_already_reserved > self.target_amount:
            raise ValueError("goal reserved amount cannot exceed target amount")
        if self.priority < 1:
            raise ValueError("goal priority must be positive")


@dataclass(frozen=True)
class FinancialProfile:
    currency: str
    monthly_net_income: Decimal
    monthly_essential_expenses: Decimal
    monthly_required_debt_service: Decimal
    monthly_investment_ceiling: Decimal
    minimum_operating_cash: Decimal
    minimum_monthly_cash_buffer: Decimal
    income_stability: IncomeStability
    income_interruption_risk: bool
    immediately_available_cash: Decimal
    cash_like_assets: Decimal
    emergency_reserve: Decimal
    low_risk_fixed_income_assets: Decimal
    manual_equity_fund_assets: Decimal
    manual_bond_fund_assets: Decimal
    manual_sector_fund_assets: Decimal
    dependents: int
    other_volatile_assets: Decimal
    maximum_tolerable_loss: Decimal
    maximum_tolerable_drawdown: Decimal
    reaction_10: RiskReaction
    reaction_20: RiskReaction
    reaction_30: RiskReaction
    experienced_material_loss: bool
    understands_multi_year_recovery: bool
    can_postpone_goal_use: bool
    debts: Tuple[Debt, ...]
    obligations: Tuple[PlannedObligation, ...]
    goals: Tuple[FinancialGoal, ...]
    confirmed_at: datetime

    def validate(self) -> None:
        if self.currency != "CNY":
            raise ValueError("profile currency must be CNY")
        for value, name in (
            (self.monthly_net_income, "monthly net income"),
            (self.monthly_essential_expenses, "monthly essential expenses"),
            (self.monthly_required_debt_service, "monthly required debt service"),
            (self.monthly_investment_ceiling, "monthly investment ceiling"),
            (self.minimum_operating_cash, "minimum operating cash"),
            (self.minimum_monthly_cash_buffer, "minimum monthly cash buffer"),
            (self.immediately_available_cash, "immediately available cash"),
            (self.cash_like_assets, "cash-like assets"),
            (self.emergency_reserve, "emergency reserve"),
            (self.low_risk_fixed_income_assets, "low-risk fixed-income assets"),
            (self.manual_equity_fund_assets, "manual equity-fund assets"),
            (self.manual_bond_fund_assets, "manual bond-fund assets"),
            (self.manual_sector_fund_assets, "manual sector-fund assets"),
            (self.other_volatile_assets, "other volatile assets"),
            (self.maximum_tolerable_loss, "maximum tolerable loss"),
        ):
            _non_negative(value, name)
        if not self.maximum_tolerable_drawdown.is_finite() or not (
            Decimal("0") <= self.maximum_tolerable_drawdown <= Decimal("1")
        ):
            raise ValueError("drawdown must be between zero and one")
        if self.dependents < 0:
            raise ValueError("dependents cannot be negative")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("confirmed_at must be timezone-aware")
        for item in self.debts:
            item.validate()
        for item in self.obligations:
            item.validate()
        for item in self.goals:
            item.validate()
```

- [ ] **Step 4: Implement canonical serialization**

Create `src/kunjin/suitability/serialization.py`. Encode every `Decimal`, `date`, `datetime`, enum, tuple, and bool explicitly; reject non-finite decimal values and unexpected keys. The canonical encoder must use:

```python
json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

The decoder must construct `Debt`, `PlannedObligation`, `FinancialGoal`, and `FinancialProfile`, call `validate()`, and return the validated profile. Do not use `pickle`, `eval`, dataclass `asdict()` without type tags, or JSON float parsing for money.

- [ ] **Step 5: Export the public types**

Create `src/kunjin/suitability/__init__.py`:

```python
from kunjin.suitability.models import (
    Debt,
    FinancialGoal,
    FinancialProfile,
    IncomeStability,
    PlannedObligation,
    RiskReaction,
)

__all__ = [
    "Debt",
    "FinancialGoal",
    "FinancialProfile",
    "IncomeStability",
    "PlannedObligation",
    "RiskReaction",
]
```

- [ ] **Step 6: Run the model tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_models -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/kunjin/suitability tests/unit/test_suitability_models.py
git commit -m "feat: add validated financial profile models"
```

## Task 3: Add Keychain-Backed AES-GCM Profile Encryption

**Files:**
- Create: `src/kunjin/suitability/crypto.py`
- Create: `tests/unit/test_suitability_crypto.py`

- [ ] **Step 1: Write failing encryption tests**

Create tests with an in-memory fake secret store. Cover round trip, random nonce,
stable keyed fingerprint, tamper rejection, missing key on decrypt, and no
automatic replacement of a missing key:

```python
class MemorySecretStore:
    def __init__(self) -> None:
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def delete(self):
        self.value = None


class ProfileCipherTest(unittest.TestCase):
    def test_round_trip_uses_random_nonce_and_stable_keyed_fingerprint(self) -> None:
        store = MemorySecretStore()
        cipher = ProfileCipher(store)
        first = cipher.encrypt(b'{"amount":"12000"}')
        second = cipher.encrypt(b'{"amount":"12000"}')
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.keyed_fingerprint, second.keyed_fingerprint)
        self.assertEqual(cipher.decrypt(first), b'{"amount":"12000"}')

    def test_tampered_ciphertext_is_rejected_without_plaintext(self) -> None:
        store = MemorySecretStore()
        cipher = ProfileCipher(store)
        encrypted = cipher.encrypt(b"private-profile")
        tampered = EncryptedProfile(
            encrypted.algorithm,
            encrypted.key_version,
            encrypted.nonce,
            encrypted.ciphertext[:-2] + "AA",
            encrypted.keyed_fingerprint,
        )
        with self.assertRaisesRegex(ProfileCryptoError, "profile decryption failed"):
            cipher.decrypt(tampered)

    def test_missing_key_does_not_generate_a_replacement_during_decrypt(self) -> None:
        store = MemorySecretStore()
        cipher = ProfileCipher(store)
        encrypted = cipher.encrypt(b"private-profile")
        store.delete()
        with self.assertRaisesRegex(ProfileCryptoError, "profile encryption key is unavailable"):
            cipher.decrypt(encrypted)
        self.assertIsNone(store.value)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_crypto -v
```

Expected: import failure for `kunjin.suitability.crypto`.

- [ ] **Step 3: Implement the encryption contract**

Create these public types in `crypto.py`:

```python
@dataclass(frozen=True)
class EncryptedProfile:
    algorithm: str
    key_version: str
    nonce: str
    ciphertext: str
    keyed_fingerprint: str


class ProfileCryptoError(RuntimeError):
    code = "encrypted_profile_unavailable"
```

Implement `ProfileKeyStore` as a narrow wrapper around `KeychainTokenStore` with
service `com.kunjin.profile-encryption` and account `v1`. Store a URL-safe base64
encoding of exactly 32 random bytes. `load_existing_key()` must return `None`
without creating a key. `load_or_create_key()` is used only while confirming a
new profile.

Implement `ProfileCipher` using:

```python
ALGORITHM = "AES-256-GCM"
KEY_VERSION = "1"
ASSOCIATED_DATA = b"kunjin:financial-profile:v1"
```

- Generate a fresh 12-byte nonce for every encryption.
- Encrypt with `AESGCM(key).encrypt(nonce, plaintext, ASSOCIATED_DATA)`.
- Derive a separate 32-byte fingerprint key with HKDF-SHA256 using info
  `b"kunjin:financial-profile:fingerprint:v1"`.
- Calculate `hmac.new(fingerprint_key, plaintext, hashlib.sha256).hexdigest()`.
- Base64-encode nonce and ciphertext for SQLite storage.
- Convert all `InvalidTag`, base64, key-length, and Keychain errors into a
  redacted `ProfileCryptoError` without returning ciphertext or plaintext.

- [ ] **Step 4: Add a real Keychain command-construction test**

Patch `KeychainTokenStore._run`, create `ProfileKeyStore`, call `save_key()` and
`load_existing_key()`, and assert the service/account are exactly
`com.kunjin.profile-encryption` and `v1`, `shell=False` remains inherited, and the
raw key never appears in an exception string.

- [ ] **Step 5: Run the encryption and existing keychain tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_crypto tests.unit.test_keychain -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/suitability/crypto.py tests/unit/test_suitability_crypto.py
git commit -m "feat: encrypt financial profiles with keychain keys"
```

## Task 4: Add Schema Version 7 With Immutable Sensitive Payloads

**Files:**
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v7.py`

- [ ] **Step 1: Write the failing migration tests**

Create tests that migrate a fresh database and a populated version-6 database.
Assert the new table exists, versions are `[1, 2, 3, 4, 5, 6, 7]`, and existing
transactions, NAV, identities, holdings, and peer groups remain unchanged.

Also add this immutability test:

```python
with self.assertRaisesRegex(sqlite3.IntegrityError, "profile payload is immutable"):
    with repository.connect() as connection, connection:
        connection.execute(
            "UPDATE financial_profile_versions SET encrypted_payload = ? WHERE id = 1",
            ("changed",),
        )
```

- [ ] **Step 2: Run the migration test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_schema_v7 -v
```

Expected: failure because schema version 7 and `financial_profile_versions` do not exist.

- [ ] **Step 3: Add schema version 7**

Set:

```python
SCHEMA_VERSION = 7
```

Add:

```sql
CREATE TABLE IF NOT EXISTS financial_profile_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE CHECK(version > 0),
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'confirmed', 'superseded', 'invalidated'
    )),
    encryption_algorithm TEXT NOT NULL CHECK(encryption_algorithm = 'AES-256-GCM'),
    encryption_key_version TEXT NOT NULL,
    nonce TEXT NOT NULL,
    encrypted_payload TEXT NOT NULL,
    keyed_payload_fingerprint TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_financial_profile
ON financial_profile_versions(status)
WHERE status = 'confirmed';

CREATE TRIGGER IF NOT EXISTS financial_profile_payload_no_update
BEFORE UPDATE ON financial_profile_versions
WHEN OLD.encryption_algorithm != NEW.encryption_algorithm
  OR OLD.encryption_key_version != NEW.encryption_key_version
  OR OLD.nonce != NEW.nonce
  OR OLD.encrypted_payload != NEW.encrypted_payload
  OR OLD.keyed_payload_fingerprint != NEW.keyed_payload_fingerprint
  OR OLD.confirmed_at != NEW.confirmed_at
  OR OLD.created_at != NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'profile payload is immutable');
END;

CREATE TRIGGER IF NOT EXISTS financial_profile_no_delete
BEFORE DELETE ON financial_profile_versions
BEGIN
    SELECT RAISE(ABORT, 'profile versions are immutable');
END;
```

Draft data remains in process memory and is never written before explicit local
confirmation. Lifecycle metadata may transition a confirmed record to
`superseded` or `invalidated`; encrypted content and confirmation metadata never
change.

- [ ] **Step 4: Wire schema version 7 into `Repository.migrate()`**

Import `SCHEMA_V7`, execute it after `SCHEMA_V6`, insert migration version 6
explicitly, then insert version 7 with `SCHEMA_VERSION`. Do not reuse
`SCHEMA_VERSION` for the version-6 row.

- [ ] **Step 5: Run all schema tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_schema_v2 \
  tests.unit.test_schema_v4 \
  tests.unit.test_schema_v5 \
  tests.unit.test_schema_v6 \
  tests.unit.test_schema_v7 -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_schema_v7.py
git commit -m "feat: add encrypted profile schema"
```

## Task 5: Implement Profile Storage And Lifecycle Service

**Files:**
- Create: `src/kunjin/suitability/store.py`
- Create: `src/kunjin/suitability/service.py`
- Create: `tests/unit/test_suitability_store.py`
- Create: `tests/unit/test_suitability_service.py`

- [ ] **Step 1: Write failing store lifecycle tests**

Test these exact behaviors:

- First confirmation creates version 1 with status `confirmed`.
- Second confirmation atomically changes version 1 to `superseded` and inserts
  version 2 as the only `confirmed` row.
- A database failure rolls back both superseding and insertion.
- Invalidation changes only lifecycle fields.
- History is newest-first and exposes no encrypted payload by default.
- Loading the active encrypted row returns all encryption metadata.

Use a fixed aware clock and a fake `EncryptedProfile`; never put recognizable
salary values in SQL fixtures.

- [ ] **Step 2: Run the store tests to verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_store -v
```

Expected: import failure for `ProfileStore`.

- [ ] **Step 3: Implement `ProfileStore`**

Define:

```python
@dataclass(frozen=True)
class ProfileVersionMetadata:
    id: int
    version: int
    status: str
    confirmed_at: datetime
    valid_until: datetime
    invalidated_at: Optional[datetime]
    invalidation_reason: Optional[str]


@dataclass(frozen=True)
class StoredEncryptedProfile:
    metadata: ProfileVersionMetadata
    encrypted: EncryptedProfile
```

Implement:

```python
confirm(encrypted, confirmed_at, valid_until) -> ProfileVersionMetadata
active_encrypted() -> Optional[StoredEncryptedProfile]
history() -> Tuple[ProfileVersionMetadata, ...]
invalidate_active(reason, invalidated_at) -> Optional[ProfileVersionMetadata]
```

`confirm()` must use one SQLite transaction, select `MAX(version)`, update the
active row to `superseded`, and insert the next version. It must never accept a
naive datetime or empty invalidation reason.

- [ ] **Step 4: Write failing service tests**

Test:

- `confirm_profile()` validates, canonicalizes, encrypts, and stores a 90-day
  validity window.
- `load_active_profile()` decrypts and verifies the keyed fingerprint.
- Missing key returns `encrypted_profile_unavailable` and does not create a key.
- `status()` returns only `state`, `version`, `confirmed_at`, `valid_until`, and
  `freshness`.
- `history()` contains lifecycle metadata only.
- An expired profile reports `stale` without mutating the database.

- [ ] **Step 5: Implement `ProfileService`**

Constructor:

```python
def __init__(
    self,
    store: ProfileStore,
    cipher: ProfileCipher,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
```

Public methods:

```python
confirm_profile(profile: FinancialProfile) -> ProfileVersionMetadata
load_active_profile() -> Optional[FinancialProfile]
status() -> Dict[str, object]
history() -> Tuple[Dict[str, object], ...]
invalidate(reason: str) -> Optional[ProfileVersionMetadata]
```

Use a fixed 90-day validity window in Phase A. Do not calculate suitability,
investable assets, or allocation ranges.

- [ ] **Step 6: Run store and service tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_suitability_store \
  tests.unit.test_suitability_service -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/kunjin/suitability/store.py src/kunjin/suitability/service.py \
  tests/unit/test_suitability_store.py tests/unit/test_suitability_service.py
git commit -m "feat: add financial profile lifecycle"
```

## Task 6: Build The Local Interactive Profile Editor

**Files:**
- Create: `src/kunjin/suitability/editor.py`
- Create: `tests/unit/test_suitability_editor.py`

- [ ] **Step 1: Write failing editor tests with injected I/O**

Use an iterator-backed input function and a list-backed writer. Test:

- Invalid decimal input is re-prompted without echoing a traceback.
- `cancel` exits without calling `confirm_profile()`.
- Answering `no` to the final confirmation stores nothing.
- Answering `yes` stores exactly one profile.
- The final local summary contains exact values, but the returned command payload
  contains only version/freshness metadata.
- No prompt asks for account passwords, card numbers, verification codes, or
  Yangjibao tokens.

- [ ] **Step 2: Run the editor test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_editor -v
```

Expected: import failure for `ProfileEditor`.

- [ ] **Step 3: Implement prompt helpers**

Implement focused helpers:

```python
_required_text(label: str) -> str
_decimal(label: str) -> Decimal
_integer(label: str) -> int
_boolean(label: str) -> bool
_date(label: str) -> date
_choice(label: str, enum_type: Type[Enum]) -> Enum
_debts() -> Tuple[Debt, ...]
_obligations() -> Tuple[PlannedObligation, ...]
_goals() -> Tuple[FinancialGoal, ...]
```

Every helper recognizes the exact input `cancel` and raises a private
`ProfileEditCancelled`. Dates use `YYYY-MM-DD`; drawdown accepts a percentage
such as `20` and stores `Decimal("0.20")`. Money input rejects commas, scientific
notation, non-finite values, and negative values.

- [ ] **Step 4: Implement explicit local confirmation**

`ProfileEditor.edit()` gathers every Phase A field, builds a profile with an
aware UTC confirmation time, validates it, prints a structured local summary,
and asks:

```text
Confirm and encrypt this profile? [yes/no]
```

Only the exact answer `yes` calls `ProfileService.confirm_profile()`. Return:

```python
{"status": "confirmed", "version": metadata.version}
```

or:

```python
{"status": "cancelled"}
```

Do not return the profile object or exact values.

- [ ] **Step 5: Run the editor tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_suitability_editor -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/suitability/editor.py tests/unit/test_suitability_editor.py
git commit -m "feat: add interactive financial profile editor"
```

## Task 7: Wire Profile Commands Into The CLI

**Files:**
- Modify: `src/kunjin/cli.py`
- Modify: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser and integration tests**

Add parser cases:

```python
["profile", "edit"]
["--json", "profile", "status"]
["--json", "profile", "history"]
```

Assert `profile edit` with `--json` returns a stable `invalid_arguments` error.
Assert status without a profile returns:

```json
{
  "state": "missing",
  "freshness": "missing"
}
```

After confirming a synthetic encrypted profile through the service, assert
status/history expose no values such as `12000`, `500000`, `40000`, or the fake
Keychain secret.

- [ ] **Step 2: Run the focused CLI tests to verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_smoke tests.integration.test_cli -v
```

Expected: profile parser or context failures.

- [ ] **Step 3: Extend `ApplicationContext` and `build_context()`**

Add:

```python
profile_service: Optional[ProfileService] = None
```

Construct:

```python
profile_store = ProfileStore(repository)
profile_service = ProfileService(
    profile_store,
    ProfileCipher(ProfileKeyStore()),
)
```

Use keyword arguments in `tests/integration/test_cli.py` when creating
`ApplicationContext` so future optional services do not break positional test
construction.

- [ ] **Step 4: Add parser and command routing**

Add `profile` to `_TOP_LEVEL_COMMANDS` and `_command_name_from_argv()` nested
commands. Add subcommands `edit`, `status`, and `history`.

Routing behavior:

```python
if args.command == "profile" and args.profile_command == "edit":
    if args.json_output:
        raise CliUsageError("profile edit is interactive and does not support JSON mode")
    if context.profile_service is None:
        raise CliUsageError("profile service is unavailable")
    result = ProfileEditor(context.profile_service).edit()
    return envelope("profile.edit", result)

if args.command == "profile" and args.profile_command == "status":
    return envelope("profile.status", context.profile_service.status())

if args.command == "profile" and args.profile_command == "history":
    return envelope("profile.history", {"profiles": context.profile_service.history()})
```

Add `ProfileCryptoError` to the stable exception tuple in `run()`.

- [ ] **Step 5: Run CLI and smoke tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_smoke tests.integration.test_cli -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose encrypted profile commands"
```

## Task 8: Strengthen Financial-Value Redaction And Storage Privacy Tests

**Files:**
- Modify: `src/kunjin/logging.py`
- Modify: `tests/unit/test_logging.py`
- Modify: `tests/unit/test_suitability_service.py`
- Modify: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing redaction tests**

Add a test containing labeled fields:

```text
monthly_net_income=12000 emergency_reserve=40000 debt_principal=500000
maximum_tolerable_loss=20000 profile_key=secret
```

Assert none of the values survive `redact_secrets()` and every key remains as
`key=[REDACTED]`.

- [ ] **Step 2: Run the redaction test to verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_logging -v
```

Expected: the new financial values remain visible.

- [ ] **Step 3: Add narrow financial redaction patterns**

Extend the existing redaction system for these exact normalized keys:

```text
monthly_net_income
monthly_essential_expenses
monthly_required_debt_service
monthly_investment_ceiling
immediately_available_cash
cash_like_assets
emergency_reserve
low_risk_fixed_income_assets
manual_equity_fund_assets
manual_bond_fund_assets
manual_sector_fund_assets
debt_principal
goal_amount
obligation_amount
maximum_tolerable_loss
profile_key
```

Do not add a broad number-redaction expression that would destroy fund codes,
dates, NAV values, or non-sensitive analytics.

- [ ] **Step 4: Add a database plaintext scan test**

After confirming a profile with distinctive exact values, read the SQLite file
as bytes and assert those UTF-8 strings do not appear. Also query
`financial_profile_versions` and assert its ciphertext and fingerprint do not
contain them.

- [ ] **Step 5: Add a JSON and exception leakage test**

Serialize profile status/history and trigger a decryption failure. Assert the
distinctive values, ciphertext, nonce, Keychain secret, and database path are
absent from both successful and error envelopes.

- [ ] **Step 6: Run privacy tests**

Run:

```bash
.venv/bin/python -m unittest \
  tests.unit.test_logging \
  tests.unit.test_suitability_crypto \
  tests.unit.test_suitability_service \
  tests.integration.test_cli -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/kunjin/logging.py tests/unit/test_logging.py \
  tests/unit/test_suitability_service.py tests/integration/test_cli.py
git commit -m "test: prevent financial profile disclosure"
```

## Task 9: Align README And Codex Skill With Phase A's Transitional Safety State

**Files:**
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `integrations/codex/kunjin-fund/agents/openai.yaml`

- [ ] **Step 1: Update README installation and privacy requirements**

Replace the claim that the runtime uses only the standard library. Document the
bounded `cryptography` dependency, Keychain service name, encrypted SQLite
payload, and the commands:

```bash
.venv/bin/kunjin profile edit
.venv/bin/kunjin --json profile status
.venv/bin/kunjin --json profile history
```

State explicitly:

- Phase A stores and versions the profile.
- Phase A does not calculate suitability, allocation, or purchase amounts.
- Exact financial values should be entered in the local interactive terminal,
  not pasted into Codex chat.
- Losing the Keychain key makes the encrypted profile unavailable; it does not
  reveal or reset the old profile.

- [ ] **Step 2: Update the Skill command and privacy workflow**

Add profile commands and these mandatory Phase A rules:

```text
- Never request exact income, debt, reserve, asset, goal, or loss-budget values in chat.
- Direct the user to `kunjin profile edit` for exact local entry.
- `profile status` and `profile history` may be read through JSON because they are metadata-only.
- Until `suitability assess` exists and returns a non-blocked state in Phase B,
  buy/add/reduce/sell and position-size questions are `research_only`.
- Phase A profile presence is not suitability approval.
```

Replace the current workflow item that permits buy/hold/add/reduce/sell
interpretations on explicit request. During Phase A, allow factual research and
opposing evidence only; do not give a position size or directional trade label.

- [ ] **Step 3: Update the Skill default prompt**

Set the default prompt to:

```yaml
default_prompt: "Use $kunjin-fund to check my local profile readiness and explain my fund portfolio with evidence; do not provide trade direction before suitability gates are implemented and passed."
```

- [ ] **Step 4: Verify repository Skill consistency and wording**

Run:

```bash
rg -n "profile edit|research_only|exact income|suitability" \
  README.md integrations/codex/kunjin-fund/SKILL.md \
  integrations/codex/kunjin-fund/agents/openai.yaml
```

Expected: all four Phase A limitations are present and no text claims that
allocation or purchase checking exists.

- [ ] **Step 5: Install the repository Skill and verify byte identity**

Copy only the reviewed repository Skill files into the existing personal Skill
directory:

```bash
cp integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cp integrations/codex/kunjin-fund/agents/openai.yaml \
  /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
diff -u integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
diff -u integrations/codex/kunjin-fund/agents/openai.yaml \
  /Users/yanzihao/.codex/skills/kunjin-fund/agents/openai.yaml
```

Expected: both `diff` commands produce no output. Do not replace unrelated
skills or modify global AGENTS instructions.

- [ ] **Step 6: Commit**

```bash
git add README.md integrations/codex/kunjin-fund
git commit -m "docs: document encrypted profile safety boundary"
```

## Task 10: Run Full Verification And Write The Phase A Independent Financial Audit

**Files:**
- Create: `docs/audits/2026-07-12-kunjin-phase-a-independent-review.md`
- Modify only if verification finds a real defect: files already listed above

- [ ] **Step 1: Run the complete automated test suite**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Expected: all existing 338 tests plus the new Phase A tests pass with zero
failures and zero errors.

- [ ] **Step 2: Run bytecode compilation**

Run:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-phase-a-pycache \
  .venv/bin/python -m compileall -q src tests
```

Expected: exit 0 and no output.

- [ ] **Step 3: Run the configured linter**

If `.venv/bin/ruff` is unavailable, install the declared development dependency
with approval rather than skipping silently:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
```

Expected: `All checks passed!`.

- [ ] **Step 4: Run packaging and CLI smoke checks**

Run:

```bash
.venv/bin/kunjin --json version
.venv/bin/kunjin --json profile status
.venv/bin/kunjin --json profile history
```

Expected: version succeeds; profile commands return stable envelopes containing
metadata only. Do not run `profile edit` with real values during automated
verification.

- [ ] **Step 5: Run a synthetic sensitive-data scan**

Use an isolated temporary `KUNJIN_DATA_DIR` and `KUNJIN_STATE_DIR`, confirm a
synthetic profile through the service, then run:

```bash
rg -a -n "73129|84217|95311" "$KUNJIN_DATA_DIR" "$KUNJIN_STATE_DIR"
```

Expected: no matches. These sentinel numbers must be used only in the isolated
test profile and must not be real personal values.

- [ ] **Step 6: Perform the independent financial review**

Create `docs/audits/2026-07-12-kunjin-phase-a-independent-review.md` using actual
command and test evidence. The review must state, at minimum:

- Useful capability: exact financial facts can now be stored locally with
  authenticated encryption and version metadata.
- Material limitation: no suitability, emergency-reserve assessment, risk
  capacity, allocation range, or purchase check exists yet.
- False-confidence risk: profile status must not be interpreted as investment
  readiness.
- Skill review: directional fund questions are temporarily research-only.
- Privacy result: exact values are absent from normal JSON, logs, and plaintext
  SQLite scans.
- Coverage rubric: only verified Phase A capability receives credit; all
  designed-only Phase B-F capabilities receive zero.
- 90% conclusion: not reached, with the exact scored areas and remaining gaps.
- Next priority: Phase B suitability safety gates.

Do not copy the design's expected conclusion without inspecting the implemented
commands and outputs.

- [ ] **Step 7: Review the final diff and user-owned files**

Run:

```bash
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors; the two pre-existing untracked user documents
remain untouched; only Phase A implementation, documentation, tests, and audit
files are changed.

- [ ] **Step 8: Commit the verified audit**

```bash
git add docs/audits/2026-07-12-kunjin-phase-a-independent-review.md
git commit -m "docs: audit encrypted personal profile phase"
```

## Phase A Exit Gate

Do not begin the Phase B implementation plan until all of the following are true:

- The user has reviewed the Phase A implementation and independent audit.
- The complete test suite, bytecode compilation, linter, and CLI smoke checks
  have fresh passing evidence.
- Exact synthetic financial values are absent from plaintext database, logs, and
  normal JSON responses.
- The repository Skill and installed Skill are byte-identical after installation.
- The Skill treats all directional trade and position-size questions as
  `research_only` during the Phase A transition.
- The independent audit explicitly concludes that 90% workflow coverage has not
  been reached and identifies Phase B as the next priority.
