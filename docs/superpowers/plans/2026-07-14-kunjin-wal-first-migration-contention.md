# KunJin WAL First-Migration Contention Implementation Plan

> **For agentic workers:** Implement task-by-task with fresh subagents, test-first evidence, and independent review.

**Goal:** Make concurrent first migrations reliably enable SQLite WAL while retrying only the exact transient lock error.

**Architecture:** Move the existing WAL pragma into a private bounded-retry helper. Keep migration transaction semantics, schema validation, rollback, commit, and permissions unchanged.

**Tech Stack:** Python 3.9, sqlite3, unittest/pytest.

---

### Task 1: Add WAL Retry Contract Tests

**Files:**
- Modify: `tests/unit/test_repository.py`
- Test: `tests/unit/test_schema_v9.py`
- Test: `tests/unit/test_schema_v10.py`

- [ ] **Step 1: Add imports for helper-level tests**

```python
import sqlite3
from unittest.mock import patch

import kunjin.storage.repository as repository_module
```

- [ ] **Step 2: Prove one transient lock is retried**

Add a fake connection whose first `execute` raises
`sqlite3.OperationalError("database is locked")` and whose second call returns.
Patch `kunjin.storage.repository.time.sleep`, call
`repository_module._enable_wal(connection)`, and assert two execute calls plus
one sleep with `0.01`.

- [ ] **Step 3: Prove unrelated errors are immediate**

Use a fake connection that raises `sqlite3.OperationalError("disk I/O error")`.
Assert `_enable_wal` re-raises that error and patched `time.sleep` is never
called.

- [ ] **Step 4: Run red tests**

```bash
.venv/bin/python -m pytest tests/unit/test_repository.py -q
.venv/bin/python -m pytest \
  tests/unit/test_schema_v10.py::SchemaV10Test::test_two_connections_can_contend_for_first_migration \
  -q
```

Expected: helper tests fail because `_enable_wal` does not exist; the V10
contention test reproduces `database is locked`.

### Task 2: Implement The Bounded WAL Retry

**Files:**
- Modify: `src/kunjin/storage/repository.py`
- Test: `tests/unit/test_repository.py`
- Test: `tests/unit/test_schema_v9.py`
- Test: `tests/unit/test_schema_v10.py`

- [ ] **Step 1: Import monotonic timing support**

```python
import time
```

- [ ] **Step 2: Add fixed retry constants and helper**

```python
_WAL_RETRY_TIMEOUT_SECONDS = 5.0
_WAL_RETRY_INTERVAL_SECONDS = 0.01


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _WAL_RETRY_TIMEOUT_SECONDS
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if exc.args != ("database is locked",):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_INTERVAL_SECONDS)
```

- [ ] **Step 3: Replace only the existing pragma call**

```python
with self.connect() as connection:
    _enable_wal(connection)
```

Do not alter the following `BEGIN IMMEDIATE` transaction or any migration code.

- [ ] **Step 4: Run focused green tests**

```bash
.venv/bin/python -m pytest tests/unit/test_repository.py -q
.venv/bin/python -m pytest \
  tests/unit/test_schema_v9.py::SchemaV9Test::test_two_connections_can_contend_for_first_migration \
  tests/unit/test_schema_v10.py::SchemaV10Test::test_two_connections_can_contend_for_first_migration \
  -q
```

Expected: PASS.

### Task 3: Stress And Full Verification

**Files:**
- Verify: `src/kunjin/storage/repository.py`
- Verify: `tests/unit/test_repository.py`
- Verify: `tests/unit/test_schema_v9.py`
- Verify: `tests/unit/test_schema_v10.py`

- [ ] **Step 1: Repeat the contention tests**

Run both contention tests 20 times. Expected: 40 test executions with zero
failures.

- [ ] **Step 2: Run full verification**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check src/kunjin/storage/repository.py tests/unit/test_repository.py tests/test_smoke.py
PYTHONPYCACHEPREFIX=/private/tmp/kunjin-pycache \
  .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
/bin/bash -n scripts/build_legacy_doc_converter.sh
git diff --check
shasum -a 256 containers/legacy-doc/Dockerfile
```

Expected: all commands pass and the Dockerfile checksum remains unchanged.

- [ ] **Step 3: Independent review**

Require no P0/P1/P2 for retry scope, error preservation, deadline behavior,
migration transaction semantics, or the already completed Docker identity fix.

### Task 4: Lock The Deadline Behavior

**Files:**
- Modify: `tests/unit/test_repository.py`
- Test: `tests/unit/test_repository.py`

- [ ] **Step 1: Add a deterministic mocked-clock deadline test**

Use one persistent `sqlite3.OperationalError("database is locked")` instance and
a fake connection that always raises it. Patch `time.monotonic` with
`[10.0, 14.0, 15.0]` and patch `time.sleep`. Assert:

- the exact same exception object is re-raised;
- `execute` is called exactly twice;
- `sleep(0.01)` is called exactly once;
- `monotonic` is called exactly three times.

- [ ] **Step 2: Run focused verification and independent re-review**

```bash
.venv/bin/python -m pytest tests/unit/test_repository.py -q
.venv/bin/ruff check tests/unit/test_repository.py
.venv/bin/ruff format --check tests/unit/test_repository.py
git diff --check -- tests/unit/test_repository.py
```

Expected: PASS and no remaining P0/P1/P2 after independent re-review.
