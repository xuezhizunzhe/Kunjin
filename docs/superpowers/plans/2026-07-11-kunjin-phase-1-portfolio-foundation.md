# KunJin Phase 1 Portfolio Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tested local CLI that securely authorizes Yangjibao, synchronizes read-only account and holding snapshots into SQLite, and returns structured portfolio summaries for Codex.

**Architecture:** A Python 3.9 package exposes a Typer CLI with JSON output. An audited HTTPS-only Yangjibao adapter writes redacted raw snapshots and normalized account/position rows through a transactional SQLite repository; deterministic analytics consume normalized models. Authentication tokens live in macOS Keychain.

**Tech Stack:** Python 3.9, Typer, HTTPX, Pydantic 2, platformdirs, qrcode, SQLite, pytest, respx, Ruff.

**Execution variance (2026-07-11):** PyPI DNS resolution was unavailable and the
system had no cached Typer/Pydantic/platformdirs/qrcode/pytest packages. Phase one
therefore uses the Python standard library (`argparse`, `urllib`, `dataclasses`,
`sqlite3`, and `unittest`) while preserving the same CLI, storage, security, and
JSON contracts. `qrcode` remains an optional local-only renderer. This variance
was selected only after reproducing the dependency failure and testing the old
packaging toolchain; no business scope changed.

---

## Scope

This is the first independently usable slice of the approved design. It implements personal portfolio access and foundational contracts. Later plans add public-fund research, market/news analysis, evidence reports, the installed `kunjin-fund` Skill, and `launchd` scheduling.

No existing file under `/Users/yanzihao/.codex/skills/` or `/Users/yanzihao/.codex/AGENTS.md` is changed by this plan.

## File Map

```text
pyproject.toml
README.md
.gitignore
src/kunjin/__init__.py
src/kunjin/cli.py
src/kunjin/models.py
src/kunjin/paths.py
src/kunjin/logging.py
src/kunjin/security/keychain.py
src/kunjin/storage/schema.py
src/kunjin/storage/repository.py
src/kunjin/adapters/yangjibao.py
src/kunjin/services/sync.py
src/kunjin/analytics/portfolio.py
tests/unit/*.py
tests/integration/*.py
tests/fixtures/yangjibao/*.json
```

## Locked Implementation Contracts

Use these names and signatures throughout phase one so later tasks do not drift:

```python
@dataclass(frozen=True)
class RuntimePaths:
    database: Path
    snapshots: Path
    logs: Path

    @classmethod
    def from_environment(cls) -> "RuntimePaths": ...
    def ensure(self) -> "RuntimePaths": ...


class KeychainTokenStore:
    def __init__(self, service: str = "com.kunjin.yangjibao", account: str = "default"): ...
    def save(self, token: str) -> None: ...
    def load(self) -> Optional[str]: ...
    def delete(self) -> None: ...


class YangjibaoClient:
    def __init__(
        self,
        token_store: KeychainTokenStore,
        base_url: str = "https://browser-plug-api.yangjibao.com",
        signing_secret: str = YANGJIBAO_BROWSER_PLUGIN_SIGNING_SECRET,
    ): ...
    def start_qr_login(self) -> QrLoginChallenge: ...
    def poll_qr_login(self, challenge_id: str, timeout_seconds: int = 120) -> None: ...
    def list_accounts(self) -> Tuple[dict, List[AccountObservation]]: ...
    def list_holdings(self, account_id: str) -> Tuple[dict, List[PositionObservation]]: ...


class PortfolioSyncService:
    def sync_portfolio(self, trigger: str) -> SyncResult: ...


def analyze_portfolio(positions: Sequence[PositionSnapshot]) -> PortfolioAnalysis: ...
```

The JSON envelope is exact:

```python
class CommandEnvelope(BaseModel):
    schema_version: Literal["1"] = "1"
    command: str
    as_of: datetime
    data: Dict[str, Any]
    warnings: List[str] = Field(default_factory=list)
    errors: List[CommandError] = Field(default_factory=list)
```

Schema version 1 uses these tables and columns:

```sql
CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  trigger TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','success','failed')),
  error_code TEXT,
  error_message TEXT
);
CREATE TABLE raw_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
  endpoint TEXT NOT NULL,
  retrieved_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL
);
CREATE TABLE accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_account_id TEXT NOT NULL,
  title TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(source, source_account_id)
);
CREATE TABLE positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL REFERENCES accounts(id),
  fund_code TEXT NOT NULL CHECK(length(fund_code) = 6),
  fund_name TEXT NOT NULL,
  share_class TEXT,
  shares TEXT NOT NULL,
  formal_nav TEXT,
  estimated_nav TEXT,
  observed_profit TEXT,
  observed_at TEXT NOT NULL,
  UNIQUE(account_id, fund_code, observed_at)
);
```

### Task 1: Initialize the Repository and CLI Scaffold

**Files:**
- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `src/kunjin/__init__.py`
- Create: `src/kunjin/cli.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: Initialize Git locally**

Run:

```bash
cd /Users/yanzihao/KunJin
git init
git branch -M main
```

Expected: empty repository on `main`; no remote is added yet.

- [ ] **Step 2: Write the failing CLI test**

```python
from typer.testing import CliRunner
from kunjin.cli import app


def test_version_returns_json() -> None:
    result = CliRunner().invoke(app, ["--json", "version"])
    assert result.exit_code == 0
    compact = result.stdout.replace(" ", "")
    assert '"schema_version":"1"' in compact
    assert '"version":"0.1.0"' in compact
```

- [ ] **Step 3: Create a test environment and confirm the test fails**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install pytest typer
.venv/bin/pytest tests/test_smoke.py -v
```

Expected: import failure because the package does not exist.

- [ ] **Step 4: Create package metadata and minimal CLI**

Use `setuptools`, Python `>=3.9`, and dependencies `httpx`, `platformdirs`, `pydantic`, `qrcode`, and `typer`. Add dev dependencies `pytest`, `respx`, and `ruff`. Register `kunjin = "kunjin.cli:app"`.

`src/kunjin/__init__.py`:

```python
__version__ = "0.1.0"
```

The CLI response envelope contains `schema_version`, `command`, `as_of`, `data`, `warnings`, and `errors`.

- [ ] **Step 5: Install and pass the smoke test**

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/test_smoke.py -v
```

Expected: `1 passed`.

- [ ] **Step 6: Add exclusions and commit**

Exclude `.venv/`, caches, `*.db`, snapshots, logs, `.env`, and token files. README states the read-only boundary and that KunJin does not operate Alipay or produce automatic trades.

```bash
git add .gitignore pyproject.toml README.md src tests docs
git commit -m "chore: scaffold kunjin portfolio foundation"
```

### Task 2: Runtime Paths and Secret-Redacting Logging

**Files:**
- Create: `src/kunjin/paths.py`
- Create: `src/kunjin/logging.py`
- Create: `tests/unit/test_paths.py`
- Create: `tests/unit/test_logging.py`

- [ ] **Step 1: Write path tests**

```python
def test_runtime_paths_use_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("KUNJIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUNJIN_STATE_DIR", str(tmp_path / "state"))
    paths = RuntimePaths.from_environment()
    assert paths.database == tmp_path / "data" / "kunjin.db"
    assert paths.snapshots == tmp_path / "data" / "snapshots"
    assert paths.logs == tmp_path / "state" / "logs"
```

- [ ] **Step 2: Write redaction tests**

```python
def test_redact_secrets_removes_auth_values():
    value = "Authorization: abc token=secret Request-Sign: deadbeef"
    redacted = redact_secrets(value)
    assert "abc" not in redacted
    assert "secret" not in redacted
    assert "deadbeef" not in redacted
```

- [ ] **Step 3: Implement focused path and logging modules**

`RuntimePaths` uses environment overrides in tests and `platformdirs` otherwise. `ensure()` creates directories with mode `0700`. A `logging.Filter` redacts values following `Authorization`, `token`, `Request-Sign`, `qr`, and `secret`; HTTP headers and bodies are not logged by default.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/unit/test_paths.py tests/unit/test_logging.py -v
git add src/kunjin/paths.py src/kunjin/logging.py tests/unit
git commit -m "feat: add secure runtime paths and logging"
```

### Task 3: Versioned SQLite Storage

**Files:**
- Create: `src/kunjin/models.py`
- Create: `src/kunjin/storage/__init__.py`
- Create: `src/kunjin/storage/schema.py`
- Create: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema.py`
- Create: `tests/unit/test_repository.py`

- [ ] **Step 1: Write the schema test**

```python
def test_migrate_creates_phase_one_tables(repository):
    repository.migrate()
    expected = {"schema_migrations", "sync_runs", "raw_snapshots", "accounts", "positions"}
    assert expected <= repository.table_names()
```

- [ ] **Step 2: Define normalized Pydantic contracts**

```python
class AccountObservation(BaseModel):
    source: Literal["yangjibao"]
    source_account_id: str
    title: str
    observed_at: datetime


class PositionObservation(BaseModel):
    source_account_id: str
    fund_code: str = Field(pattern=r"^\d{6}$")
    fund_name: str
    share_class: Optional[str] = None
    shares: Decimal
    formal_nav: Optional[Decimal] = None
    estimated_nav: Optional[Decimal] = None
    observed_profit: Optional[Decimal] = None
    observed_at: datetime
```

- [ ] **Step 3: Implement schema version 1**

Enable foreign keys and WAL. Store UTC ISO-8601 timestamps. Add uniqueness on `(source, source_account_id)` and `(account_id, fund_code, observed_at)`. Keep redacted JSON and SHA-256 checksums in `raw_snapshots`.

- [ ] **Step 4: Implement transactional repository methods**

Provide `migrate`, `begin_sync`, `save_raw_snapshot`, `upsert_account`, `insert_positions`, `finish_sync`, `fail_sync`, `latest_positions`, `latest_successful_sync`, and `latest_raw_snapshot`. Invalid normalized batches roll back without deleting previous history.

- [ ] **Step 5: Prove rollback behavior**

```python
def test_invalid_batch_rolls_back(repository, account, valid_position, invalid_position):
    repository.migrate()
    with pytest.raises(ValueError):
        repository.replace_snapshot(account, [valid_position, invalid_position])
    assert repository.latest_positions() == []
```

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/pytest tests/unit/test_schema.py tests/unit/test_repository.py -v
git add src/kunjin/models.py src/kunjin/storage tests/unit
git commit -m "feat: add transactional portfolio storage"
```

### Task 4: macOS Keychain Token Store

**Files:**
- Create: `src/kunjin/security/__init__.py`
- Create: `src/kunjin/security/keychain.py`
- Create: `tests/unit/test_keychain.py`

- [ ] **Step 1: Write exact command tests**

```python
def test_save_token_uses_non_shell_update_command(run_mock):
    store = KeychainTokenStore("com.kunjin.yangjibao", "default")
    store.save("synthetic-token")
    command = run_mock.call_args.args[0]
    assert command[:4] == ["/usr/bin/security", "add-generic-password", "-U", "-s"]
    assert run_mock.call_args.kwargs["shell"] is False
```

- [ ] **Step 2: Implement `save`, `load`, and `delete`**

Call `/usr/bin/security` with `shell=False`. Save uses `add-generic-password -U`; load uses `find-generic-password -w`; delete is idempotent. Missing entries return `None`; all exception messages pass through redaction.

- [ ] **Step 3: Verify and commit**

```bash
.venv/bin/pytest tests/unit/test_keychain.py -v
git add src/kunjin/security tests/unit/test_keychain.py
git commit -m "feat: store yangjibao token in keychain"
```

### Task 5: Audited Yangjibao Client

**Files:**
- Create: `src/kunjin/adapters/__init__.py`
- Create: `src/kunjin/adapters/yangjibao.py`
- Create: `tests/unit/test_yangjibao.py`
- Create: `tests/fixtures/yangjibao/accounts.json`
- Create: `tests/fixtures/yangjibao/holdings.json`

- [ ] **Step 1: Write signature, HTTPS, and allowlist tests**

```python
def test_signature_matches_known_vector():
    expected = hashlib.md5(b"/user_accounttoken1secret").hexdigest()
    assert generate_signature("/user_account", "token", 1, "secret") == expected


def test_plaintext_base_url_is_rejected():
    with pytest.raises(InsecureTransportError):
        YangjibaoClient(
            token_store=FakeTokenStore(),
            base_url="http://browser-plug-api.yangjibao.com",
        )


def test_write_path_is_rejected(client):
    with pytest.raises(DisallowedEndpointError):
        client.get("/write_account")
```

- [ ] **Step 2: Implement request boundaries**

Use `https://browser-plug-api.yangjibao.com`. Allow only QR creation/state, account list/summary, fund holdings, and income reads. Validate dynamic identifiers strictly. Send signed authentication headers without logging them. Map HTTP 401, 408, 429, malformed JSON, and business errors to typed exceptions.

- [ ] **Step 3: Implement normalization**

Parse numeric values with `Decimal(str(value))`, validate six-digit fund codes, preserve unavailable values as `None`, and keep formal NAV separate from estimated NAV. Synthetic fixtures contain no personal values.

- [ ] **Step 4: Implement QR login safely**

Retrieve the first-party QR URL, render it locally with `qrcode`, poll for at most 120 seconds, and save the token directly to Keychain. Do not print, return, persist, or send the token or QR contents to third parties.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/unit/test_yangjibao.py -v
git add src/kunjin/adapters tests/unit/test_yangjibao.py tests/fixtures/yangjibao
git commit -m "feat: add read-only yangjibao client"
```

### Task 6: Transactional Portfolio Synchronization

**Files:**
- Create: `src/kunjin/services/__init__.py`
- Create: `src/kunjin/services/sync.py`
- Create: `tests/integration/test_sync.py`

- [ ] **Step 1: Write the end-to-end mocked sync test**

```python
def test_sync_stores_redacted_raw_and_normalized_data(sync_service, repository):
    result = sync_service.sync_portfolio(trigger="manual")
    assert result.accounts == 1
    assert result.positions == 2
    assert len(repository.latest_positions()) == 2
    assert "Authorization" not in repository.latest_raw_snapshot()
```

- [ ] **Step 2: Implement recursive redaction**

Replace values for `authorization`, `token`, `sign`, `secret`, and QR-bearing URLs before serialization. Store sorted JSON plus a SHA-256 checksum.

- [ ] **Step 3: Implement the sync transaction**

Create a sync run, fetch accounts and holdings, persist redacted raw data, validate all normalized observations, and commit them together. On failure, roll back normalized changes and mark the run failed in a separate transaction.

- [ ] **Step 4: Prove previous data survives refresh failure**

```python
def test_failed_refresh_preserves_previous_snapshot(sync_service, repository, failing_client):
    sync_service.sync_portfolio(trigger="manual")
    before = repository.latest_positions()
    sync_service.client = failing_client
    with pytest.raises(SyncError):
        sync_service.sync_portfolio(trigger="manual")
    assert repository.latest_positions() == before
```

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/pytest tests/integration/test_sync.py -v
git add src/kunjin/services tests/integration/test_sync.py
git commit -m "feat: synchronize yangjibao portfolio"
```

### Task 7: Deterministic Portfolio Analytics

**Files:**
- Create: `src/kunjin/analytics/__init__.py`
- Create: `src/kunjin/analytics/portfolio.py`
- Create: `tests/unit/test_portfolio.py`

- [ ] **Step 1: Write totals, weights, and HHI tests**

```python
def test_analysis_calculates_weights_and_hhi():
    result = analyze_portfolio([position("000001", "60"), position("000002", "40")])
    assert result.total_value == Decimal("100")
    assert result.weights["000001"] == Decimal("0.6")
    assert result.hhi == Decimal("0.52")
```

- [ ] **Step 2: Implement explicit calculation rules**

Use shares times formal NAV; use estimated NAV only for a clearly labeled current estimate. Return total observed profit only with complete coverage, otherwise return coverage plus a warning. Calculate weights, HHI, largest-position share, and observed-profit contribution. Never infer transaction cost.

- [ ] **Step 3: Test missing-data behavior**

```python
def test_missing_nav_returns_insufficient_data():
    result = analyze_portfolio([position_without_nav("000001")])
    assert result.total_value is None
    assert result.evidence_level == "insufficient_data"
    assert any("missing NAV" in item for item in result.warnings)
```

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/unit/test_portfolio.py -v
git add src/kunjin/analytics tests/unit/test_portfolio.py
git commit -m "feat: add portfolio concentration analytics"
```

### Task 8: Stable Workflow CLI

**Files:**
- Modify: `src/kunjin/cli.py`
- Create: `tests/integration/test_cli.py`

- [ ] **Step 1: Write response-contract and secret tests**

```python
def test_status_json_contract(cli_runner):
    result = cli_runner.invoke(app, ["--json", "status"])
    payload = json.loads(result.stdout)
    assert set(payload) == {"schema_version", "command", "as_of", "data", "warnings", "errors"}


def test_portfolio_output_never_contains_token(cli_runner, token_store):
    token_store.save("never-print-this")
    result = cli_runner.invoke(app, ["--json", "portfolio", "show"])
    assert "never-print-this" not in result.stdout
```

- [ ] **Step 2: Implement command groups**

Expose `auth login/status/revoke`, `sync portfolio`, `status`, `portfolio show`, and `portfolio analyze`. JSON mode always returns the versioned envelope. Operational errors are structured and non-zero; warnings remain non-fatal.

- [ ] **Step 3: Implement centralized freshness**

Mark portfolio data `fresh` through the next completed mainland-China trading day, `stale` afterward, and `missing` when no successful sync exists. Test the boundary in one focused function.

- [ ] **Step 4: Verify and commit**

```bash
.venv/bin/pytest tests/integration/test_cli.py -v
git add src/kunjin/cli.py tests/integration/test_cli.py
git commit -m "feat: expose portfolio workflow through cli"
```

### Task 9: Documentation and Automated Verification

**Files:**
- Modify: `README.md`
- Create: `docs/phase-1-security-review.md`

- [ ] **Step 1: Document setup and limitations**

Document Python 3.9 setup, QR authorization, JSON commands, runtime paths, revocation, and phase-one limitations: no authoritative Alipay trades, exact fee lots, public-fund research, or market-sector analysis.

- [ ] **Step 2: Record the security review**

Document the HTTPS host, endpoint allowlist, signing provenance, token-lifetime uncertainty, local QR rendering, redacted persistence, and unofficial-interface instability.

- [ ] **Step 3: Run full verification**

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -v
.venv/bin/python -m kunjin.cli --json version
```

Expected: lint exits 0, all tests pass, and CLI schema/version are `1`/`0.1.0`.

- [ ] **Step 4: Scan tracked non-test files for credential strings**

```bash
git grep -n -E 'Authorization:|Request-Sign:|\.yjb_token' -- ':!docs/superpowers/plans/*' ':!tests/*'
```

Expected: no output.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/phase-1-security-review.md
git commit -m "docs: document phase one portfolio workflow"
```

### Task 10: Opt-in Live Read-only Smoke Test

**Files:**
- Runtime state only

- [ ] **Step 1: Show the endpoint list before authorization**

Display that only QR login, accounts, account summaries, holdings, and income reads can run. Verify no write method exists.

- [ ] **Step 2: Authorize and synchronize**

```bash
.venv/bin/kunjin auth login yangjibao
.venv/bin/kunjin --json sync portfolio
.venv/bin/kunjin --json portfolio show
.venv/bin/kunjin --json portfolio analyze
```

Expected: token is not printed; holdings include as-of, freshness, and source metadata.

- [ ] **Step 3: Compare two holdings with the app**

Verify account count, fund code, shares, and observed profit for at least two holdings. Record only pass/fail and non-sensitive discrepancies.

- [ ] **Step 4: Run final regression**

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -v
git status --short
```

Expected: tests pass and no runtime database, snapshot, log, or credential is tracked.

### Task 11: Configure GitHub After Local Success

**Files:**
- Git metadata only

- [ ] **Step 1: Check remote reachability**

Run: `git ls-remote https://github.com/xuezhizunzhe/Kunjin.git`

Expected: success; an empty response is valid for an empty repository.

- [ ] **Step 2: Add the intended remote without overwriting another**

```bash
git remote get-url origin 2>/dev/null || git remote add origin https://github.com/xuezhizunzhe/Kunjin.git
git remote -v
```

Expected: fetch and push URLs target `xuezhizunzhe/Kunjin`.

- [ ] **Step 3: Review and push**

```bash
git status --short
git log --oneline --decorate -10
git push -u origin main
```

Expected: clean worktree before push. If network or authentication is unavailable, preserve the verified local repository and report the blocker.
