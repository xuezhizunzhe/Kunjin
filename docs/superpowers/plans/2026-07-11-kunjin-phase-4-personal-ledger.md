# KunJin Personal Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import local Alipay payment screenshots, create user-confirmed immutable fund transactions, and reconcile them against Yangjibao position observations with field-level evidence.

**Architecture:** Add a focused `kunjin.ledger` package beside the existing portfolio and research code. Apple Vision performs local OCR through a bundled Swift helper; Python normalizes Alipay fields, persists drafts and confirmed transactions in SQLite schema version 4, and calculates reconciliation without inventing unavailable lots, fees, or confirmation values. Existing Yangjibao sync remains read-only and unchanged.

**Tech Stack:** Python 3.9 standard library, SQLite, Apple Vision/AppKit through `/usr/bin/swift`, `unittest`, existing KunJin CLI JSON envelope.

---

## Scope And File Map

This is the first delivery phase from `docs/superpowers/specs/2026-07-11-kunjin-a-share-intelligence-and-personal-ledger-design.md`. Fund metadata, peer comparison, A-share intelligence, automatic news ingestion, and layered candidate research remain separate later plans.

Create these focused units:

- `src/kunjin/ledger/models.py`: ledger enums, OCR blocks, drafts, transactions, and reconciliation result types.
- `src/kunjin/ledger/store.py`: SQLite reads and writes for documents, OCR fields, drafts, and immutable transactions.
- `src/kunjin/ledger/ocr.py`: OCR adapter protocol, Apple Vision subprocess client, and stable OCR errors.
- `src/kunjin/ledger/vision_ocr.swift`: local macOS Vision text recognition and JSON output only.
- `src/kunjin/ledger/alipay.py`: deterministic parsing of Alipay payment-detail OCR blocks.
- `src/kunjin/ledger/service.py`: private file import, draft creation, confirmation, manual entry, and managed-copy deletion.
- `src/kunjin/ledger/reconcile.py`: deterministic comparison between confirmed cash flow and inferred Yangjibao position cost.
- `src/kunjin/ledger/__init__.py`: public ledger exports only.

Modify these existing files:

- `src/kunjin/paths.py:8-33`: add the private imports directory without changing constructor compatibility.
- `src/kunjin/storage/schema.py:1-98`: add schema version 4.
- `src/kunjin/storage/repository.py:14-48`: execute schema version 4 during migration.
- `src/kunjin/cli.py:12-58,104-156,163-353,356-373`: wire the ledger service and commands into the existing context and JSON contract.
- `pyproject.toml`: package the Swift helper.
- `README.md`: document ledger commands, evidence limits, and local OCR prerequisite.
- `integrations/codex/kunjin-fund/SKILL.md`: teach Codex the import-confirm-reconcile workflow.

Do not add a cloud OCR dependency, browser automation, Alipay login, mobile control, MySQL, Redis, or transaction mutation commands.

### Task 1: Private Import Path And Schema Version 4

**Files:**
- Modify: `src/kunjin/paths.py:8-33`
- Modify: `src/kunjin/storage/schema.py:1-98`
- Modify: `src/kunjin/storage/repository.py:14-48`
- Create: `tests/unit/test_schema_v4.py`
- Modify: `tests/unit/test_paths.py:10-31`

- [ ] **Step 1: Write failing path and migration tests**

Add to `tests/unit/test_paths.py`:

```python
self.assertEqual(paths.imports, root / "data" / "imports")
self.assertEqual(stat.S_IMODE(paths.imports.stat().st_mode), 0o700)
```

Create `tests/unit/test_schema_v4.py`:

```python
import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository


class SchemaV4Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_adds_ledger_tables(self) -> None:
        expected = {
            "imported_documents",
            "ocr_fields",
            "transaction_drafts",
            "transactions",
        }
        self.assertTrue(expected <= self.repository.table_names())

    def test_transactions_are_immutable_at_database_level(self) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_type, fund_code, evidence_level,
                    field_evidence_json, created_at
                ) VALUES ('subscription', '519755', 'user_confirmed', '{}', '2026-07-11T00:00:00+00:00')
                """
            )
            with self.assertRaisesRegex(Exception, "transactions are immutable"):
                connection.execute("UPDATE transactions SET fund_code = '000001' WHERE id = 1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and confirm the missing path/schema failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_paths tests.unit.test_schema_v4 -v
```

Expected: `test_runtime_paths_use_overrides_and_private_permissions` fails because `RuntimePaths.imports` does not exist, and schema tests fail because version 4 tables do not exist.

- [ ] **Step 3: Add the imports property and schema**

Add to `RuntimePaths` and include it in `ensure()`:

```python
@property
def imports(self) -> Path:
    return self.database.parent / "imports"

def ensure(self) -> "RuntimePaths":
    for directory in (self.database.parent, self.snapshots, self.imports, self.logs):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    return self
```

Set `SCHEMA_VERSION = 4` and append this exact `SCHEMA_V4` definition:

```python
SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS imported_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    managed_path TEXT,
    document_type TEXT NOT NULL CHECK(document_type IN ('alipay_payment', 'unknown')),
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'deleted')) DEFAULT 'active',
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS ocr_fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES imported_documents(id),
    field_name TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    normalized_value TEXT,
    confidence TEXT NOT NULL,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    UNIQUE(document_id, field_name)
);

CREATE TABLE IF NOT EXISTS transaction_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER REFERENCES imported_documents(id),
    transaction_type TEXT NOT NULL,
    fund_code TEXT CHECK(fund_code IS NULL OR length(fund_code) = 6),
    fund_name TEXT,
    amount TEXT,
    shares TEXT,
    nav TEXT,
    fee TEXT,
    order_time TEXT,
    confirmation_time TEXT,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    field_evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'rejected')) DEFAULT 'pending',
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER REFERENCES imported_documents(id),
    transaction_type TEXT NOT NULL,
    fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
    fund_name TEXT,
    amount TEXT,
    shares TEXT,
    nav TEXT,
    fee TEXT,
    order_time TEXT,
    confirmation_time TEXT,
    evidence_level TEXT NOT NULL CHECK(evidence_level IN (
        'transaction_confirmed', 'user_confirmed', 'position_inferred'
    )),
    field_evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS transactions_no_update
BEFORE UPDATE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transactions are immutable');
END;
"""
```

Import `SCHEMA_V4` in `repository.py`. Change the migration record immediately after `SCHEMA_V3` to literal version `3`; then execute `SCHEMA_V4` and insert version `SCHEMA_VERSION` (`4`). This prevents changing `SCHEMA_VERSION` from incorrectly recording version 4 before the version 4 SQL runs.

- [ ] **Step 4: Run the focused tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_paths tests.unit.test_schema_v4 -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit schema version 4**

```bash
git add src/kunjin/paths.py src/kunjin/storage/schema.py src/kunjin/storage/repository.py tests/unit/test_paths.py tests/unit/test_schema_v4.py
git commit -m "feat: add personal ledger schema"
```

### Task 2: Ledger Types And SQLite Store

**Files:**
- Create: `src/kunjin/ledger/__init__.py`
- Create: `src/kunjin/ledger/models.py`
- Create: `src/kunjin/ledger/store.py`
- Create: `tests/unit/test_ledger_store.py`

- [ ] **Step 1: Write failing store round-trip and immutability tests**

Create `tests/unit/test_ledger_store.py` with tests that construct a temporary `Repository`, migrate it, create `LedgerStore`, save one document and draft, confirm it, and assert a second confirmation raises `LedgerStateError`:

```python
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.ledger.models import EvidenceLevel, LedgerDraft, TransactionType
from kunjin.ledger.store import LedgerStateError, LedgerStore
from kunjin.storage.repository import Repository


class LedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        repository.migrate()
        self.store = LedgerStore(repository)
        self.now = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_draft_confirmation_is_atomic_and_single_use(self) -> None:
        document_id = self.store.add_document(
            sha256="a" * 64,
            original_name="payment.jpg",
            managed_path="/private/payment.jpg",
            document_type="alipay_payment",
            imported_at=self.now,
        )
        draft_id = self.store.add_draft(
            LedgerDraft(
                id=None,
                source_document_id=document_id,
                transaction_type=TransactionType.SUBSCRIPTION,
                fund_code="519755",
                fund_name=None,
                amount=Decimal("20.00"),
                shares=None,
                nav=None,
                fee=None,
                order_time=datetime(2026, 7, 4, 23, 11, 51, tzinfo=timezone.utc),
                confirmation_time=None,
                evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
                field_evidence={"amount": EvidenceLevel.TRANSACTION_CONFIRMED.value},
                status="pending",
                created_at=self.now,
            )
        )

        transaction = self.store.confirm_draft(draft_id, self.now)

        self.assertEqual(transaction.fund_code, "519755")
        self.assertEqual(transaction.amount, Decimal("20.00"))
        with self.assertRaises(LedgerStateError):
            self.store.confirm_draft(draft_id, self.now)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and confirm imports fail**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_store -v
```

Expected: FAIL because `kunjin.ledger.models` and `kunjin.ledger.store` do not exist.

- [ ] **Step 3: Define ledger enums and dataclasses**

In `src/kunjin/ledger/models.py`, define:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional


class EvidenceLevel(str, Enum):
    TRANSACTION_CONFIRMED = "transaction_confirmed"
    USER_CONFIRMED = "user_confirmed"
    POSITION_INFERRED = "position_inferred"


class TransactionType(str, Enum):
    SUBSCRIPTION = "subscription"
    RECURRING_SUBSCRIPTION = "recurring_subscription"
    REDEMPTION = "redemption"
    CASH_DIVIDEND = "cash_dividend"
    REINVESTED_DIVIDEND = "reinvested_dividend"
    CONVERSION_IN = "conversion_in"
    CONVERSION_OUT = "conversion_out"


@dataclass(frozen=True)
class OcrBlock:
    text: str
    confidence: Decimal
    x: Decimal
    y: Decimal
    width: Decimal
    height: Decimal


@dataclass(frozen=True)
class ExtractedField:
    name: str
    raw_text: str
    normalized_value: Optional[str]
    confidence: Decimal
    evidence_level: EvidenceLevel


@dataclass(frozen=True)
class LedgerDraft:
    id: Optional[int]
    source_document_id: Optional[int]
    transaction_type: TransactionType
    fund_code: Optional[str]
    fund_name: Optional[str]
    amount: Optional[Decimal]
    shares: Optional[Decimal]
    nav: Optional[Decimal]
    fee: Optional[Decimal]
    order_time: Optional[datetime]
    confirmation_time: Optional[datetime]
    evidence_level: EvidenceLevel
    field_evidence: Dict[str, str]
    status: str
    created_at: datetime


@dataclass(frozen=True)
class LedgerTransaction:
    id: Optional[int]
    source_document_id: Optional[int]
    transaction_type: TransactionType
    fund_code: str
    fund_name: Optional[str]
    amount: Optional[Decimal]
    shares: Optional[Decimal]
    nav: Optional[Decimal]
    fee: Optional[Decimal]
    order_time: Optional[datetime]
    confirmation_time: Optional[datetime]
    evidence_level: EvidenceLevel
    field_evidence: Dict[str, str]
    created_at: datetime


@dataclass(frozen=True)
class ReconciliationResult:
    fund_code: str
    status: str
    confirmed_cash_flow: Optional[Decimal]
    inferred_position_cost: Optional[Decimal]
    difference: Optional[Decimal]
    tolerance: Optional[Decimal]
    evidence_level: EvidenceLevel
    warnings: List[str] = field(default_factory=list)
```

Export these names from `src/kunjin/ledger/__init__.py`.

- [ ] **Step 4: Implement the focused ledger store**

Implement `LedgerStore` using the existing `Repository.connect()` context. Required public methods and behavior:

```python
class LedgerStateError(ValueError):
    code = "ledger_state_error"


DOCUMENT_SELECT_SQL = "SELECT * FROM imported_documents WHERE sha256 = ?"
DOCUMENT_INSERT_SQL = """
INSERT INTO imported_documents(
    sha256, original_name, managed_path, document_type, imported_at
) VALUES (?, ?, ?, ?, ?)
"""
DOCUMENT_RESTORE_SQL = """
UPDATE imported_documents
SET original_name = ?, managed_path = ?, document_type = ?, imported_at = ?,
    status = 'active', deleted_at = NULL
WHERE id = ?
"""
OCR_FIELD_UPSERT_SQL = """
INSERT INTO ocr_fields(
    document_id, field_name, raw_text, normalized_value, confidence, evidence_level
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(document_id, field_name) DO UPDATE SET
    raw_text = excluded.raw_text,
    normalized_value = excluded.normalized_value,
    confidence = excluded.confidence,
    evidence_level = excluded.evidence_level
"""
DRAFT_SELECT_SQL = "SELECT * FROM transaction_drafts WHERE id = ?"
DRAFT_UPDATE_SQL = """
UPDATE transaction_drafts
SET transaction_type = ?, fund_code = ?, fund_name = ?, amount = ?, shares = ?,
    nav = ?, fee = ?, order_time = ?, confirmation_time = ?,
    evidence_level = ?, field_evidence_json = ?
WHERE id = ? AND status = 'pending'
"""
TRANSACTION_INSERT_SQL = """
INSERT INTO transactions(
    source_document_id, transaction_type, fund_code, fund_name,
    amount, shares, nav, fee, order_time, confirmation_time,
    evidence_level, field_evidence_json, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
DRAFT_CONFIRM_SQL = """
UPDATE transaction_drafts
SET status = 'confirmed', confirmed_at = ?
WHERE id = ? AND status = 'pending'
"""
DOCUMENT_DELETE_SQL = """
UPDATE imported_documents
SET managed_path = NULL, status = 'deleted', deleted_at = ?
WHERE id = ? AND status = 'active'
"""
```

`add_document()` first executes `DOCUMENT_SELECT_SQL`. It inserts when no row exists, returns the existing ID when the row is active, and executes `DOCUMENT_RESTORE_SQL` when the same hash was previously deleted and is now re-imported. This resolves the unique SHA-256 constraint without creating duplicate evidence rows.

`add_draft()` inserts every dataclass field into `transaction_drafts`, using `str()` for decimals, `isoformat()` for times, enum `.value`, and `json.dumps(field_evidence, sort_keys=True, separators=(",", ":"))`. `get_draft(draft_id)` uses `DRAFT_SELECT_SQL`. `list_drafts()` filters with `WHERE status = ? ORDER BY created_at, id`. `replace_pending_draft(draft)` requires a non-null ID, executes `DRAFT_UPDATE_SQL`, and raises `LedgerStateError("draft is not pending")` unless exactly one row changes.

`confirm_draft()` opens one `with self.repository.connect() as connection, connection:` transaction, loads `DRAFT_SELECT_SQL`, rejects missing/non-pending drafts, requires `fund_code`, executes `TRANSACTION_INSERT_SQL`, executes `DRAFT_CONFIRM_SQL`, and returns the inserted row through `_row_to_transaction()`. If the update row count is not one, raise `LedgerStateError("draft is not pending")` so concurrent confirmation cannot create two transactions.

`add_transaction()` uses `TRANSACTION_INSERT_SQL` directly after validation and returns a copy whose optional ID is the inserted integer. `list_transactions()` uses `ORDER BY COALESCE(confirmation_time, order_time, created_at), id`, adding `WHERE fund_code = ?` only when supplied. `document_path(document_id)` returns the active row's managed path or `None`. `mark_document_deleted()` reads and returns the old path before executing `DOCUMENT_DELETE_SQL` in the same transaction.

Do not expose transaction update or delete methods. Validate `status`, enum values, fund code, non-negative amount/shares/nav/fee, and decoded JSON object shape before writes. Use private `_row_to_draft()` and `_row_to_transaction()` helpers that restore optional decimals with `Decimal`, optional times with `datetime.fromisoformat`, enums from stored strings, and evidence with `json.loads`.

- [ ] **Step 5: Run store and migration tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_store tests.unit.test_schema_v4 -v
```

Expected: all tests pass, including the second-confirmation rejection.

- [ ] **Step 6: Commit ledger storage**

```bash
git add src/kunjin/ledger tests/unit/test_ledger_store.py
git commit -m "feat: add immutable ledger storage"
```

### Task 3: Local Apple Vision OCR Adapter

**Files:**
- Create: `src/kunjin/ledger/ocr.py`
- Create: `src/kunjin/ledger/vision_ocr.swift`
- Modify: `pyproject.toml`
- Create: `tests/unit/test_ledger_ocr.py`

- [ ] **Step 1: Write failing OCR protocol tests**

Create `tests/unit/test_ledger_ocr.py` using a fake `subprocess.run` result. Assert JSON output becomes `OcrBlock` instances, a missing `/usr/bin/swift` raises `OcrUnavailableError`, malformed JSON raises `OcrResponseError`, and no image bytes or OCR text appear in exception messages.

The successful fixture returned by the fake process must be:

```json
{"blocks":[{"text":"订单金额 20.00元","confidence":0.98,"x":0.1,"y":0.2,"width":0.8,"height":0.05}]}
```

- [ ] **Step 2: Run the OCR test and confirm it fails**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_ocr -v
```

Expected: FAIL because `kunjin.ledger.ocr` does not exist.

- [ ] **Step 3: Implement the Python OCR adapter**

Define these stable errors and client in `ocr.py`:

```python
class OcrError(RuntimeError):
    code = "ocr_error"


class OcrUnavailableError(OcrError):
    code = "ocr_unavailable"


class OcrResponseError(OcrError):
    code = "ocr_response_error"


class VisionOcrClient:
    def __init__(
        self,
        swift_path: Path = Path("/usr/bin/swift"),
        helper_path: Optional[Path] = None,
        timeout_seconds: int = 60,
    ) -> None:
        self.swift_path = swift_path
        self.helper_path = helper_path or Path(__file__).with_name("vision_ocr.swift")
        self.timeout_seconds = timeout_seconds

    def recognize(self, image_path: Path) -> List[OcrBlock]:
        if not self.swift_path.is_file() or not self.helper_path.is_file():
            raise OcrUnavailableError("local Apple Vision OCR is unavailable")
        if not image_path.is_file():
            raise OcrResponseError("image file is unavailable")
        completed = subprocess.run(
            [str(self.swift_path), str(self.helper_path), str(image_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise OcrResponseError("local Apple Vision OCR failed")
        try:
            payload = json.loads(completed.stdout)
            rows = payload["blocks"]
            return [
                OcrBlock(
                    text=str(row["text"]),
                    confidence=Decimal(str(row["confidence"])),
                    x=Decimal(str(row["x"])),
                    y=Decimal(str(row["y"])),
                    width=Decimal(str(row["width"])),
                    height=Decimal(str(row["height"])),
                )
                for row in rows
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OcrResponseError("local Apple Vision OCR returned invalid data") from exc
```

Import `json`, `subprocess`, `Decimal`, `Path`, and typing names explicitly. Catch `subprocess.TimeoutExpired` and `OSError` and convert them to the same redacted error classes.

- [ ] **Step 4: Add the Swift Vision helper**

`vision_ocr.swift` must import `AppKit`, `Foundation`, and `Vision`; load only the path in `CommandLine.arguments[1]`; run `VNRecognizeTextRequest` with `.accurate`, `usesLanguageCorrection = true`, and `recognitionLanguages = ["zh-Hans", "en-US"]`; serialize only this shape to stdout:

```swift
struct Block: Codable {
    let text: String
    let confidence: Float
    let x: CGFloat
    let y: CGFloat
    let width: CGFloat
    let height: CGFloat
}

struct Output: Codable {
    let blocks: [Block]
}
```

For each `VNRecognizedTextObservation`, use `topCandidates(1).first`. Sort blocks top-to-bottom and left-to-right using Vision bounding boxes. Write diagnostics only to stderr without including recognized text or image bytes. Exit non-zero for missing arguments, unreadable image, Vision failure, or JSON serialization failure.

Add package data:

```toml
[tool.setuptools.package-data]
kunjin = ["ledger/*.swift"]
```

- [ ] **Step 5: Run tests and Swift syntax smoke check**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_ocr -v
/usr/bin/swift src/kunjin/ledger/vision_ocr.swift
```

Expected: Python tests pass. The Swift command exits non-zero with a concise “image path is required” error, proving the helper parses and starts without emitting recognized content.

- [ ] **Step 6: Commit local OCR adapter**

```bash
git add pyproject.toml src/kunjin/ledger/ocr.py src/kunjin/ledger/vision_ocr.swift tests/unit/test_ledger_ocr.py
git commit -m "feat: add local Apple Vision OCR"
```

### Task 4: Alipay Payment Parser And Field Evidence

**Files:**
- Create: `src/kunjin/ledger/alipay.py`
- Create: `tests/fixtures/ledger/alipay_payment_blocks.json`
- Create: `tests/unit/test_alipay_parser.py`

- [ ] **Step 1: Add a synthetic OCR fixture and failing parser tests**

The fixture must contain OCR blocks for these visible strings and confidences:

```json
{
  "blocks": [
    {"text":"支付成功","confidence":"0.99","x":"0.40","y":"0.90","width":"0.20","height":"0.04"},
    {"text":"-20.00","confidence":"0.99","x":"0.40","y":"0.78","width":"0.20","height":"0.06"},
    {"text":"订单时间","confidence":"0.98","x":"0.08","y":"0.42","width":"0.18","height":"0.04"},
    {"text":"2026-07-04 23:11:51","confidence":"0.97","x":"0.50","y":"0.42","width":"0.42","height":"0.04"}
  ]
}
```

Tests must assert:

```python
fields = AlipayPaymentParser().parse(blocks)
self.assertEqual(fields["amount"].normalized_value, "20.00")
self.assertEqual(fields["order_time"].normalized_value, "2026-07-04T23:11:51+08:00")
self.assertEqual(fields["amount"].evidence_level, EvidenceLevel.TRANSACTION_CONFIRMED)
self.assertNotIn("fund_code", fields)
```

Also test that missing amount raises `AlipayParseError(code="missing_required_field")`, an invalid date is rejected, and confidence below `0.80` is retained but makes `requires_confirmation(fields)` return `True`.

- [ ] **Step 2: Run tests and confirm parser import failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_alipay_parser -v
```

Expected: FAIL because `kunjin.ledger.alipay` does not exist.

- [ ] **Step 3: Implement deterministic payment-detail parsing**

Define `AlipayParseError` with stable codes, `MIN_CONFIDENCE = Decimal("0.80")`, and `AlipayPaymentParser.parse(blocks) -> Dict[str, ExtractedField]`.

The parser must:

- Normalize full-width punctuation and whitespace with `unicodedata.normalize("NFKC", text)`.
- Recognize an amount only when `支付成功`, `支付金额`, `订单金额`, or a negative currency amount is present.
- Normalize a leading minus sign away because transaction direction is stored separately as `subscription`.
- Parse `YYYY-MM-DD HH:MM:SS` as `Asia/Shanghai` and emit an ISO 8601 value with `+08:00`.
- Never derive fund code, shares, NAV, or fee from the payment screenshot when those labels are absent.
- Use the minimum confidence among blocks contributing to a field.
- Assign `transaction_confirmed` only to fields directly visible on the payment page.

Provide:

```python
def requires_confirmation(fields: Dict[str, ExtractedField]) -> bool:
    required = ("amount", "order_time")
    return any(
        name not in fields or fields[name].confidence < MIN_CONFIDENCE
        for name in required
    )
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_alipay_parser -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit the Alipay parser**

```bash
git add src/kunjin/ledger/alipay.py tests/fixtures/ledger/alipay_payment_blocks.json tests/unit/test_alipay_parser.py
git commit -m "feat: parse Alipay payment evidence"
```

### Task 5: Import, Draft Confirmation, And Manual Entry Service

**Files:**
- Create: `src/kunjin/ledger/service.py`
- Create: `tests/unit/test_ledger_service.py`

- [ ] **Step 1: Write failing import and confirmation tests**

Use a fake OCR client returning the synthetic blocks and a temporary `RuntimePaths`. Cover these cases:

1. Import copies the source into `paths.imports` with mode `0600` and a SHA-256 filename.
2. Re-importing the same bytes reuses the document instead of creating a second file.
3. `fund_code_hint="519755"` is stored with `user_confirmed` field evidence while amount and order time remain `transaction_confirmed`.
4. Confirm without a six-digit fund code fails with `missing_fund_code`.
5. `--field amount=21.00` changes amount and marks only that field `user_confirmed`.
6. Manual entry creates a `user_confirmed` transaction with no source document.

The main assertion for the approved sample is:

```python
draft = service.import_image(source, fund_code_hint="519755")
transaction = service.confirm_draft(draft.id, {})

self.assertEqual(transaction.fund_code, "519755")
self.assertEqual(transaction.amount, Decimal("20.00"))
self.assertEqual(
    transaction.field_evidence,
    {
        "amount": "transaction_confirmed",
        "fund_code": "user_confirmed",
        "order_time": "transaction_confirmed",
    },
)
```

- [ ] **Step 2: Run tests and confirm service import failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_service -v
```

Expected: FAIL because `kunjin.ledger.service` does not exist.

- [ ] **Step 3: Implement private import and draft creation**

Define `LedgerService(paths, store, ocr_client, parser, now=None)`. `import_image()` must:

1. Accept only `.png`, `.jpg`, `.jpeg`, and `.heic` regular files.
2. Stream SHA-256 in 1 MiB chunks.
3. Copy to `paths.imports / f"{sha256}{suffix.lower()}"` using `shutil.copy2` only when absent.
4. Set the managed file mode to `0600`.
5. Add or reuse the document row.
6. Run OCR only against the managed copy.
7. Parse and save extracted fields.
8. Create a `subscription` draft, adding a valid fund-code hint as `user_confirmed` evidence.
9. Never return the full managed path in a public result.

Define the service error with per-instance stable codes:

```python
class LedgerImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
```

Use only these initial codes: `unsupported_image`, `source_unavailable`, `invalid_fund_code`, `missing_required_field`, `invalid_field`, and `unsafe_document_path`.

- [ ] **Step 4: Implement confirmation overrides and manual entry**

`confirm_draft(draft_id, overrides)` must accept only:

```python
ALLOWED_FIELDS = {
    "fund_code", "fund_name", "amount", "shares", "nav", "fee",
    "order_time", "confirmation_time", "transaction_type",
}
```

Load the pending draft with `store.get_draft()`, apply validated values with `dataclasses.replace`, update the changed field evidence to `user_confirmed`, persist it through `store.replace_pending_draft()`, then call the store's atomic confirmation. Parse decimal fields with `Decimal`, datetime fields with `datetime.fromisoformat`, and transaction type through `TransactionType`. Require a six-digit fund code before confirmation.

`add_manual_transaction()` must require transaction type and fund code, accept the same optional fields, set overall evidence to `user_confirmed`, and set each supplied field's evidence to `user_confirmed`.

- [ ] **Step 5: Run service tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_service -v
```

Expected: all tests pass; no test fixture contains a real Alipay screenshot.

- [ ] **Step 6: Commit ledger service**

```bash
git add src/kunjin/ledger/service.py tests/unit/test_ledger_service.py
git commit -m "feat: import and confirm fund transactions"
```

### Task 6: Managed Document Deletion And Privacy Redaction

**Files:**
- Modify: `src/kunjin/ledger/service.py`
- Modify: `src/kunjin/logging.py:7-13`
- Modify: `tests/unit/test_ledger_service.py`
- Modify: `tests/unit/test_logging.py:7-23`

- [ ] **Step 1: Write failing privacy tests**

Add tests asserting:

- `delete_document(document_id)` removes only a file whose resolved parent is `paths.imports.resolve()`.
- A database row pointing outside the managed imports directory raises `unsafe_document_path` and does not unlink the file.
- Deleting a managed document preserves confirmed transactions and their field evidence.
- `redact_secrets()` removes `order_id=...`, `card_number=...`, `phone=...`, and an absolute `managed_path=...`.

- [ ] **Step 2: Run tests and confirm failures**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_service tests.unit.test_logging -v
```

Expected: deletion tests fail because the method is absent; redaction tests fail because ledger-sensitive keys are not recognized.

- [ ] **Step 3: Implement guarded managed-copy deletion**

Add:

```python
def delete_document(self, document_id: int) -> bool:
    managed_path_text = self.store.document_path(document_id)
    if managed_path_text is None:
        return False
    managed_path = Path(managed_path_text).resolve()
    imports_root = self.paths.imports.resolve()
    if managed_path.parent != imports_root:
        raise LedgerImportError("unsafe_document_path", "document is outside managed imports")
    managed_path.unlink(missing_ok=True)
    self.store.mark_document_deleted(document_id, self._now())
    return True
```

Add `document_path(document_id)` to `LedgerStore`. Ensure the database is marked deleted only after the guarded unlink succeeds. Do not delete the row and do not cascade to OCR, drafts, or transactions.

- [ ] **Step 4: Extend redaction keys**

Extend `_SECRET_PATTERN` to include `order_id`, `card_number`, `phone`, and `managed_path`. Preserve existing token and QR redaction tests.

- [ ] **Step 5: Run privacy tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_service tests.unit.test_logging -v
```

Expected: all tests pass and external files remain untouched.

- [ ] **Step 6: Commit privacy handling**

```bash
git add src/kunjin/ledger/service.py src/kunjin/ledger/store.py src/kunjin/logging.py tests/unit/test_ledger_service.py tests/unit/test_logging.py
git commit -m "fix: protect imported transaction documents"
```

### Task 7: Position-To-Ledger Reconciliation

**Files:**
- Create: `src/kunjin/ledger/reconcile.py`
- Create: `tests/unit/test_ledger_reconcile.py`

- [ ] **Step 1: Write failing reconciliation tests**

Create the approved `519755` scenario using a `StoredPosition` with shares `11.32`, formal NAV `1.7467`, and observed profit `-0.23`, plus one confirmed subscription of `20.00`.

Assert:

```python
result = reconcile_fund(position, transactions, pending_drafts=[])
self.assertEqual(result.status, "consistent")
self.assertEqual(result.confirmed_cash_flow, Decimal("20.00"))
self.assertEqual(result.inferred_position_cost, Decimal("20.002644"))
self.assertEqual(result.difference, Decimal("-0.002644"))
self.assertEqual(result.evidence_level, EvidenceLevel.POSITION_INFERRED)
```

Also cover:

- no observed profit -> `insufficient_data`;
- no confirmed transaction -> `insufficient_data`;
- a difference covered by pending draft amounts -> `explainable_difference`;
- an uncovered difference -> `needs_investigation`;
- formal NAV missing but estimated NAV present -> result includes a warning that estimated NAV was used;
- redemptions reduce confirmed cash flow, while cash dividends do not silently change acquisition cost.

- [ ] **Step 2: Run tests and confirm reconciliation import failure**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_reconcile -v
```

Expected: FAIL because `kunjin.ledger.reconcile` does not exist.

- [ ] **Step 3: Implement deterministic reconciliation**

Use these exact calculations:

```python
current_nav = position.formal_nav or position.estimated_nav
current_value = position.shares * current_nav
inferred_position_cost = current_value - position.observed_profit
confirmed_cash_flow = subscriptions - redemptions
difference = confirmed_cash_flow - inferred_position_cost
tolerance = max(Decimal("0.02"), abs(inferred_position_cost) * Decimal("0.002"))
```

Where subscriptions include `subscription`, `recurring_subscription`, and `conversion_in`; redemptions include `redemption` and `conversion_out`. A missing amount is excluded with a warning. `cash_dividend` and `reinvested_dividend` generate explicit warnings until their cost-basis rules have confirmed inputs.

Status rules, in order:

1. Missing NAV, observed profit, or confirmed cash flow -> `insufficient_data`.
2. `abs(difference) <= tolerance` -> `consistent`.
3. `abs(difference) <= pending_amount_total + tolerance` -> `explainable_difference`.
4. Otherwise -> `needs_investigation`.

Always label inferred position cost as `position_inferred`; never call it an Alipay-confirmed cost basis.

- [ ] **Step 4: Run reconciliation tests**

Run:

```bash
.venv/bin/python -m unittest tests.unit.test_ledger_reconcile -v
```

Expected: all tests pass, including the exact `20.002644` sample.

- [ ] **Step 5: Commit reconciliation**

```bash
git add src/kunjin/ledger/reconcile.py tests/unit/test_ledger_reconcile.py
git commit -m "feat: reconcile transactions with positions"
```

### Task 8: Ledger CLI And Stable JSON Output

**Files:**
- Modify: `src/kunjin/cli.py:12-58,104-156,163-353,356-373`
- Modify: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing CLI integration tests**

Extend the test `ApplicationContext` with a fake or real temporary `LedgerService`. Add tests for:

```text
kunjin --json ledger import IMAGE --fund-code 519755
kunjin --json ledger drafts
kunjin --json ledger confirm DRAFT_ID --field fund_code=519755
kunjin --json ledger add --type subscription --fund-code 519755 --amount 20.00 --order-time 2026-07-04T23:11:51+08:00
kunjin --json ledger transactions --fund-code 519755
kunjin --json ledger reconcile --fund-code 519755
kunjin --json ledger document delete DOCUMENT_ID
```

Every response must retain exactly the current top-level envelope keys:

```python
{"schema_version", "command", "as_of", "data", "warnings", "errors"}
```

Assert `ledger.import` returns document ID, draft, `requires_confirmation`, and field evidence, but not `managed_path`, OCR text outside parsed fields, or screenshot bytes. Assert invalid overrides return a stable error code and exit code 1.

- [ ] **Step 2: Run CLI tests and confirm parser failures**

Run:

```bash
.venv/bin/python -m unittest tests.integration.test_cli -v
```

Expected: new ledger command tests fail because argparse does not know `ledger`.

- [ ] **Step 3: Wire the ledger context**

Add `ledger_service: LedgerService` to `ApplicationContext`. In `build_context()` construct:

```python
ledger_store = LedgerStore(repository)
ledger_service = LedgerService(
    paths=paths,
    store=ledger_store,
    ocr_client=VisionOcrClient(),
    parser=AlipayPaymentParser(),
)
```

Update every test context constructor. Do not let ledger construction access an image or start OCR.

- [ ] **Step 4: Add argparse command definitions**

Create a `ledger` parser with subcommands `import`, `drafts`, `confirm`, `add`, `transactions`, `reconcile`, and nested `document delete`. Use `action="append"`, default `[]`, for repeatable `--field NAME=VALUE` overrides. Validate six-digit codes in the service rather than duplicating validation in argparse.

- [ ] **Step 5: Add execute branches and error handling**

Each branch calls one service/store function and returns `envelope("ledger.<action>", serialize(data), warnings=warnings)`. For reconciliation, sync is not automatic inside the command; Codex workflow performs `sync portfolio` first when current data is requested.

Add `OcrError`, `LedgerImportError`, and `LedgerStateError` to the caught exception tuple at `cli.py:367`. Keep messages redacted through `redact_secrets()`.

Use this override parser:

```python
def parse_field_overrides(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        name, separator, field_value = value.partition("=")
        if not separator or not name.strip():
            raise ValueError("field overrides must use NAME=VALUE")
        result[name.strip()] = field_value.strip()
    return result
```

- [ ] **Step 6: Run CLI and regression tests**

Run:

```bash
.venv/bin/python -m unittest tests.integration.test_cli -v
.venv/bin/python -m unittest discover -s tests -v
```

Expected: ledger CLI tests pass and all pre-existing tests remain green.

- [ ] **Step 7: Commit CLI integration**

```bash
git add src/kunjin/cli.py tests/integration/test_cli.py
git commit -m "feat: expose personal ledger commands"
```

### Task 9: Documentation, Skill Workflow, And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Add a smoke test for package data and command discovery**

Extend `tests/test_smoke.py` to assert `Path(kunjin.ledger.ocr.__file__).with_name("vision_ocr.swift").is_file()` and that `run(["--json", "ledger", "drafts"], context)` returns command `ledger.drafts` without invoking OCR.

- [ ] **Step 2: Update README with the exact beginner workflow**

Document:

```bash
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json ledger import /absolute/path/to/alipay.jpg --fund-code 519755
.venv/bin/kunjin --json ledger drafts
.venv/bin/kunjin --json ledger confirm 1
.venv/bin/kunjin --json ledger transactions --fund-code 519755
.venv/bin/kunjin --json ledger reconcile --fund-code 519755
```

State that payment screenshots confirm only visible fields, Apple Vision is local, screenshots are copied to a private directory, deletion affects only the managed copy, and inferred position cost is not a reconstructed transaction lot.

- [ ] **Step 3: Update the repository Skill source of truth**

Add this workflow before portfolio analysis in `integrations/codex/kunjin-fund/SKILL.md`:

1. When the user provides an Alipay screenshot, run `ledger import` with a fund-code hint only if the user supplied or confirmed the code.
2. Show extracted amount, time, code, confidence, and field evidence; never expose the managed path.
3. Do not run `ledger confirm` until the user explicitly confirms the draft values.
4. Run `sync portfolio` before current reconciliation.
5. Explain `transaction_confirmed`, `user_confirmed`, and `position_inferred` separately.
6. Never call a payment screenshot a fund confirmation when shares, NAV, or fees are absent.

Add all ledger commands to the command reference. Keep the unsupported manager/fee/holding/news limitations until their later phases are implemented.

- [ ] **Step 4: Run complete automated verification**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pip check
git diff --check
```

Expected: all tests pass, compileall is silent with exit code 0, `pip check` reports no broken requirements, and `git diff --check` is silent.

- [ ] **Step 5: Run credential and private-data scans**

Run:

```bash
rg -n "Authorization:|Request-Sign:|never-print-this|card_number=|order_id=" src tests README.md integrations
rg -n "4573aa766844e356b42ec8d039a69478|9beade02aafd8281f31d4e607c20c1d7|codex-clipboard" . --glob '!.git/**'
```

Expected: the first command finds only intentional synthetic redaction assertions, never a live value. The second command returns no matches because real screenshot names and files are not committed.

- [ ] **Step 6: Perform the approved local live smoke test**

Set `ALIPAY_SCREENSHOT` at execution time to the user-approved Alipay payment screenshot outside the repository. Do not write its original absolute path into tracked files:

```bash
.venv/bin/kunjin --json ledger import "$ALIPAY_SCREENSHOT" --fund-code 519755
.venv/bin/kunjin --json ledger drafts
```

Verify the draft reports amount `20.00`, order time `2026-07-04T23:11:51+08:00`, code `519755`, and no invented shares, NAV, or fee. Present the draft to the user and wait for explicit confirmation before running:

```bash
.venv/bin/kunjin --json ledger confirm DRAFT_ID
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json ledger reconcile --fund-code 519755
```

Expected reconciliation: status `consistent`, confirmed cash flow `20.00`, inferred position cost approximately `20.002644`, and evidence level `position_inferred`. If live OCR differs, do not force the expected value; preserve the draft, report confidence and request field confirmation.

- [ ] **Step 7: Commit documentation and smoke coverage**

```bash
git add README.md integrations/codex/kunjin-fund/SKILL.md tests/test_smoke.py
git commit -m "docs: add personal ledger workflow"
```

- [ ] **Step 8: Review final history and push**

Run:

```bash
git status --short
git log --oneline --decorate -10
git push origin main
```

Expected: the worktree is clean before the push, the phase contains focused commits from this plan, and `origin/main` advances successfully. If GitHub DNS or credentials fail, report the exact push failure while preserving the local commits.

## Phase Acceptance Checklist

- [ ] A real Alipay payment screenshot is recognized locally without cloud OCR.
- [ ] Only visible payment amount and time receive `transaction_confirmed` evidence.
- [ ] Fund code hints and user corrections receive `user_confirmed` evidence.
- [ ] Missing shares, NAV, fees, and confirmation details remain missing.
- [ ] Confirmed transactions cannot be silently updated or deleted.
- [ ] Managed screenshots use private permissions and deletion cannot affect user originals.
- [ ] `519755` reconciles `20.00` payment evidence with the current Yangjibao position as an explicitly inferred cost relationship.
- [ ] Existing portfolio, fund, market, thesis, report, authorization, and scheduling behavior remains unchanged.
- [ ] All automated tests, compile checks, dependency checks, redaction scans, and live smoke checks pass or have an explicitly reported external blocker.
