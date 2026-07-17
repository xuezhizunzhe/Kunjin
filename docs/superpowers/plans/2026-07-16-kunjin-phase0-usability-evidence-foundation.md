# KunJin Phase 0 Usability And Evidence Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make fact research independent from financial-safety gates while adding bounded request execution, auditable evidence/source policy, deterministic source health, and explicit partial/supplementation results without yet claiming mature buy, hold, or sell advice.

**Architecture:** Add a focused `kunjin.decision` package for immutable policies, action routing, request budgets, worker isolation, source health, and minimal audit storage. Existing fund disclosure parsing and SQLite publication remain in the parent process; only public network fetching moves into deadline-bound subprocess workers. Expose the foundation through structured CLI commands and update the Codex Skill so ordinary research no longer waits for unsupported official adapters or incorrectly stops at Phase B.

**Tech Stack:** Python 3.9, standard-library `argparse`/`subprocess`/`selectors`, immutable dataclasses and enums, canonical JSON/SHA-256, SQLite Schema V15, pytest/unittest, Ruff, Bash acceptance scripts.

---

## Scope Boundary

This plan implements only design Phase 0: `RequestBudget`, per-action routing,
workflow/evidence separation, EvidencePolicy V1 and source-registry bindings,
partial/supplementation contracts, source health, Skill updates, and real
latency/failure acceptance.

The 90/480-second SLA in this plan applies to `decision route`, `source status`,
and the newly bounded `sync fund-profile`/`sync fund-holdings` paths. Legacy NAV,
market, portfolio, and official-document commands keep their existing contracts
until a later bounded orchestrator owns them; the updated Skill must not claim
the Phase 0 SLA for those commands.

It does **not** implement news ingestion, the Phase 1 one-fund decision vertical
slice, full D2, D3, exact transaction amounts, Phase E monitoring, or new
official-domain adapters. Each receives a separate plan after Phase 0 passes
live and independent review.

## File Map

- Create `src/kunjin/decision/{models,policy,source_registry,budget}.py` for
  public contracts, canonical policies, and monotonic budgets.
- Create `src/kunjin/decision/{worker_protocol,worker_main,worker}.py` for
  bounded public-source subprocess isolation.
- Create `src/kunjin/decision/{store,health,routing,service}.py` for minimal
  audit storage, source state, gate routing, and orchestration.
- Modify `src/kunjin/funds/{sources,service}.py` so public disclosure fetches
  use the worker while parsing and SQLite writes stay in the parent.
- Modify `src/kunjin/storage/{schema,repository}.py` for additive Schema V14.
- Modify `src/kunjin/cli.py` for `decision route`, `source status`, request
  modes, and bounded disclosure synchronization.
- Modify `integrations/codex/kunjin-fund/SKILL.md`, `README.md`, and
  `tests/test_smoke.py` for the new user and Skill contract.
- Create focused unit/integration tests and
  `scripts/run_phase0_acceptance.sh` as specified below.

## Task 1: Define Immutable Decision Policies And Contracts

**Files:**

- Create: `src/kunjin/decision/__init__.py`
- Create: `src/kunjin/decision/models.py`
- Create: `src/kunjin/decision/policy.py`
- Create: `src/kunjin/decision/source_registry.py`
- Test: `tests/unit/test_decision_policy.py`

- [ ] **Step 1: Write failing enum, policy, and registry tests**

```python
import json
import re

from kunjin.decision.models import (
    ActionKind,
    RequestFieldResolution,
    RequestMode,
    RiskEffect,
    SourceFieldState,
    WorkflowLevel,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1


def test_phase0_enums_are_exact() -> None:
    assert [item.value for item in RequestMode] == ["rapid", "deep"]
    assert [item.value for item in RiskEffect] == [
        "information", "risk_maintaining", "risk_reducing", "risk_increasing"
    ]
    assert [item.value for item in SourceFieldState] == [
        "not_checked", "healthy", "degraded", "cooldown", "unavailable", "unsupported"
    ]
    assert [item.value for item in RequestFieldResolution] == [
        "usable", "partial", "manual_supplement_required"
    ]
    assert [item.value for item in WorkflowLevel] == [
        "rapid_evidence", "decision_evidence"
    ]
    assert ActionKind.SWITCH_FUNDS.value == "switch_funds"


def test_policy_and_registry_are_canonical_and_public() -> None:
    for item in (EvidencePolicyV1(), SourceRegistryV1()):
        canonical = item.canonical_json()
        assert json.dumps(
            json.loads(canonical.decode("ascii")),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii") == canonical
        assert re.fullmatch(r"[0-9a-f]{64}", item.checksum())
        lowered = canonical.decode("ascii").casefold()
        for forbidden in ("ciphertext", "nonce", "access_token", "private_value"):
            assert forbidden not in lowered


def test_source_registry_is_finite_and_has_supplementation() -> None:
    registry = SourceRegistryV1()
    assert 1 <= len(registry.sources) <= 8
    identities = {
        (source.source_id, field.field_id)
        for source in registry.sources
        for field in source.fields
    }
    assert len(identities) == sum(len(source.fields) for source in registry.sources)
    assert all(
        field.supplementation.accepted_input
        for source in registry.sources
        for field in source.fields
    )
```

- [ ] **Step 2: Run the tests and verify the import failure**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_policy.py
```

Expected: collection fails because `kunjin.decision` does not exist.

- [ ] **Step 3: Add exact enums and immutable records**

```python
class RequestMode(str, Enum):
    RAPID = "rapid"
    DEEP = "deep"


class ActionKind(str, Enum):
    FACT_RESEARCH = "fact_research"
    CONTINUE_HOLDING = "continue_holding"
    REDUCE_TO_CASH = "reduce_to_cash"
    FULL_EXIT = "full_exit"
    BUY_OR_ADD = "buy_or_add"
    SWITCH_FUNDS = "switch_funds"


class RiskEffect(str, Enum):
    INFORMATION = "information"
    RISK_MAINTAINING = "risk_maintaining"
    RISK_REDUCING = "risk_reducing"
    RISK_INCREASING = "risk_increasing"


class SourceFieldState(str, Enum):
    NOT_CHECKED = "not_checked"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class RequestFieldResolution(str, Enum):
    USABLE = "usable"
    PARTIAL = "partial"
    MANUAL_SUPPLEMENT_REQUIRED = "manual_supplement_required"


class ActionMaturity(str, Enum):
    MATURE = "mature"
    EXPERIMENTAL_SHADOW = "experimental_shadow"


class WorkflowLevel(str, Enum):
    RAPID_EVIDENCE = "rapid_evidence"
    DECISION_EVIDENCE = "decision_evidence"


class RequestTerminalStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SourceAttemptOutcome(str, Enum):
    SUCCESS = "success"
    TRANSIENT_FAILURE = "transient_failure"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CACHE_HIT = "cache_hit"
    SKIPPED_COOLDOWN = "skipped_cooldown"
```

All records use `@dataclass(frozen=True)` and validate non-empty identifiers,
timezone-aware timestamps, exact enum instances, bounded tuples, and public
strings. Add `SupplementationRequest`, `SourceFieldPolicy`, `SourcePolicy`,
`ConclusionEvidence`, `ActionRoute`, and `DecisionRoute`.
`ConclusionEvidence` carries source tier, publishers, publication/retrieval
times, independent lineage count/IDs, completeness, freshness, conflicts,
inference flag, and missing critical fields. Workflow level is never one of
those quality fields. `ActionRoute` carries `action_id`, `action`,
`risk_effect`, `required_gates`, `blocking_codes`, `research_available`,
`exact_amount_available`, `minimum_state`, and `action_maturity`.
Also add `SourceAttempt` and `StoredSourceAttempt` with the Task 4 column types;
attempt number is exactly 1 or 2 and every timestamp is timezone-aware.
`SourceFieldPolicy.is_current(data_as_of, now)` enforces its decision freshness;
`is_usable(data_as_of, now)` permits only the explicitly configured dated-
history fallback and never upgrades it to current evidence.

- [ ] **Step 4: Add canonical EvidencePolicy V1 and SourceRegistry V1**

Use sorted compact ASCII JSON and SHA-256 over those exact bytes.
EvidencePolicy V1 encodes every design section 10.3 row, the conservative D2
coverage thresholds and unknown-exposure rule from section 12.5, and the
target/cap approval requirement from section 14. Phase 0 stores those future
gates but does not claim to satisfy them. SourceRegistry V1 is limited to:

```python
SOURCE_IDS = (
    "eastmoney_f10",
    "eastmoney_nav",
    "eastmoney_market",
    "fund_manager_official_documents",
    "yangjibao_portfolio_observation",
)
```

Each field stores source tier, maximum age, scope, acceptable alternatives, and
all eight supplementation fields from design section 18. Yangjibao is a private
observation source, never Tier 1 transaction confirmation.

- [ ] **Step 5: Verify and commit Task 1**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_policy.py
.venv/bin/ruff check src/kunjin/decision tests/unit/test_decision_policy.py
git diff --check
git add src/kunjin/decision tests/unit/test_decision_policy.py
git commit -m "feat: define decision evidence contracts"
```

## Task 2: Implement Monotonic RequestBudget

**Files:**

- Create: `src/kunjin/decision/budget.py`
- Test: `tests/unit/test_decision_budget.py`

- [ ] **Step 1: Write failing deadline and cancellation tests**

```python
from datetime import datetime, timezone

import pytest

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import RequestMode


def test_rapid_budget_reserves_cleanup_inside_ninety_seconds() -> None:
    ticks = [100.0]
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id="a" * 32,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert budget.total_seconds == 90.0
    assert budget.worker_seconds() == 88.0
    ticks[0] = 187.5
    assert budget.worker_seconds() == 0.5
    ticks[0] = 190.0
    with pytest.raises(BudgetExpired):
        budget.require_publishable()


def test_cancelled_budget_never_becomes_publishable_again() -> None:
    budget = RequestBudget.create(RequestMode.DEEP, request_id="b" * 32)
    budget.cancel("owner_cancelled")
    with pytest.raises(BudgetExpired):
        budget.require_publishable()
```

- [ ] **Step 2: Run the red tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_budget.py
```

Expected: import failure for `kunjin.decision.budget`.

- [ ] **Step 3: Implement the budget exactly once**

```python
TOTAL_SECONDS = {
    RequestMode.RAPID: 90.0,
    RequestMode.DEEP: 480.0,
}
CLEANUP_RESERVE_SECONDS = 2.0


def worker_seconds(self) -> float:
    if self.cancelled:
        return 0.0
    return max(
        0.0,
        self.monotonic_deadline - self.monotonic() - self.cleanup_reserve_seconds,
    )
```

`create()` accepts only exact `RequestMode`, generates lowercase UUID hex when
needed, stores monotonic start/deadline and wall-clock audit timestamps.
`cancel()` is one-way. `require_publishable()` fails at/after deadline or after
cancellation. Never derive execution expiry from wall-clock time.

- [ ] **Step 4: Run focused verification**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_budget.py
.venv/bin/ruff check src/kunjin/decision/budget.py tests/unit/test_decision_budget.py
```

- [ ] **Step 5: Commit Task 2**

```bash
git add src/kunjin/decision/budget.py tests/unit/test_decision_budget.py
git commit -m "feat: add bounded request budget"
```

## Task 3: Add Bounded Worker Protocol And Process Isolation

**Files:**

- Create: `src/kunjin/decision/worker_protocol.py`
- Create: `src/kunjin/decision/worker_main.py`
- Create: `src/kunjin/decision/worker.py`
- Create: `tests/fixtures/decision/worker_fixture.py`
- Test: `tests/unit/test_decision_worker.py`
- Modify: `src/kunjin/funds/sources.py`
- Test: `tests/unit/test_fund_sources.py`

- [ ] **Step 1: Write failing protocol and lifecycle tests**

Test exact request/source/field/schema binding, maximum request/response bytes,
malformed JSON, wrong request ID, sleeping worker, continuous slow output,
ignored SIGTERM, oversized output, nonzero exit, and late output. A 0.4-second
test budget must return below 0.8 seconds with no child PID alive.

- [ ] **Step 2: Run worker tests red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_worker.py \
  tests/unit/test_fund_sources.py -k 'worker or failure_reason'
```

Expected: missing worker modules and stable source failure reasons.

- [ ] **Step 3: Implement a bounded anonymous-pipe protocol**

Use schema version 1, request frame limit 16 KiB, response frame limit 12 MiB,
and this exact public request shape:

```json
{
  "schema_version": 1,
  "request_id": "32 lowercase hex characters",
  "source_id": "eastmoney_f10",
  "field_id": "basic_profile",
  "subject_key": "fund:000000",
  "operation": "fund_text_fetch",
  "arguments": {"url": "https://fundf10.eastmoney.com/", "referer": "https://fundf10.eastmoney.com/"}
}
```

The result repeats all identities and returns either a validated public payload
or exact `reason_code`, `retryable`, and safe message. Encode response text as
base64 UTF-8. Never include tracebacks, environment, paths, headers, tokens, or
raw exception text.

- [ ] **Step 4: Implement parent lifecycle and production worker**

Launch with `stdin=PIPE`, `stdout=PIPE`, `stderr=DEVNULL`, `close_fds=True`, and
`start_new_session=True`. Use `selectors` and bounded `os.read`; do not use
unbounded `communicate()`. Pass an allowlisted environment only.

On timeout, `KeyboardInterrupt`, cancellation, or oversize: cancel the budget,
SIGTERM the exact process group, wait at most
one second inside the cleanup reserve, SIGKILL if needed, then reap. Revalidate
request ID, source/field/subject, schema, size, and parent receive monotonic time
before returning. `worker_main.py` supports only `fund_text_fetch` and imports no
storage, paths, keychain, Yangjibao, or Docker module.

Give `FundSourceError` stable `reason_code` and `retryable` values for DNS,
transient network, timeout, HTTP 4xx, unsafe URL/redirect, oversized response,
and decode failure while preserving public code `fund_source_error`.

- [ ] **Step 5: Verify and commit Task 3**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_worker.py tests/unit/test_fund_sources.py
.venv/bin/ruff check src/kunjin/decision src/kunjin/funds/sources.py \
  tests/unit/test_decision_worker.py tests/unit/test_fund_sources.py
git diff --check
git add src/kunjin/decision src/kunjin/funds/sources.py \
  tests/fixtures/decision tests/unit/test_decision_worker.py tests/unit/test_fund_sources.py
git commit -m "feat: isolate public source workers"
```

## Task 4: Add Minimal Schema V14

**Files:**

- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Create: `tests/unit/test_schema_v14.py`

- [ ] **Step 1: Write failing additive migration tests**

Start databases at V10, V11, V12, and V13; migrate to V14; assert versions
1 through 14, existing classification bytes unchanged, and exactly three new
tables: `request_runs`, `source_attempts`, `decision_snapshots`. Test JSON,
lowercase checksums, foreign keys, exact states, terminal lifecycle, and
snapshot/attempt no-update/no-delete triggers.

- [ ] **Step 2: Run V14 tests red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_schema_v14.py
```

Expected: `SCHEMA_V14` is absent and `SCHEMA_VERSION` remains 13.

- [ ] **Step 3: Add the exact three-table ownership model**

`request_runs` stores request ID, mode, running/terminal status, start,
deadline, finish, and `omitted_work_json`. `source_attempts` stores request FK,
source/field/subject, attempt 1 or 2, exact outcome, times, data date, error,
cooldown, `force_actor`, force reason, registry version/checksum, and response
byte count. The single-owner Phase 0 actor is the public constant `local_owner`.
`decision_snapshots` stores request FK, complete canonical EvidencePolicy and
registry JSON/version/checksum, canonical route JSON, result checksum, and UTC
creation time.

Exact allowed attempt outcomes are:

```sql
CHECK(outcome IN (
  'success', 'transient_failure', 'unavailable', 'unsupported',
  'cancelled', 'expired', 'cache_hit', 'skipped_cooldown'
))
```

Allow `request_runs` one transition from `running` to
`complete|partial|failed|cancelled|expired` while preserving identity, mode,
start, and deadline. Reject updates to terminal runs and every update/delete of
attempts and snapshots. All canonical JSON uses `json_valid`; checksums are
exact lowercase 64-byte text.

- [ ] **Step 4: Register migration and verify history**

Set `SCHEMA_VERSION = 14`, export `SCHEMA_V14`, import it in repository, and
append `(14, SCHEMA_V14)` to `_migration_definitions()`. No destructive rebuild
or backfill is permitted.

```bash
.venv/bin/python -m pytest -q tests/unit/test_schema_v13.py tests/unit/test_schema_v14.py
```

- [ ] **Step 5: Commit Task 4**

```bash
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py \
  tests/unit/test_schema_v14.py
git commit -m "feat: add decision audit schema"
```

## Task 5: Persist Request, Attempt, And Policy-Bound Snapshots

**Files:**

- Create: `src/kunjin/decision/store.py`
- Test: `tests/unit/test_decision_store.py`

- [ ] **Step 1: Write failing lifecycle and tamper tests**

Cover begin/finalize, two-attempt maximum, terminal immutability, canonical
policy/registry round-trip, checksum verification, wrong request binding,
invalid JSON, and attempt history ordered newest first. Change one byte of
policy, registry, and route JSON; every read must fail closed.

- [ ] **Step 2: Run red store tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_store.py
```

Expected: missing `DecisionAuditStore`.

- [ ] **Step 3: Implement exact parent-side APIs**

```python
class DecisionAuditStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def begin_request(self, budget: RequestBudget) -> int:
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO request_runs(
                    request_id, mode, status, started_at, deadline_at,
                    finished_at, omitted_work_json
                ) VALUES (?, ?, 'running', ?, ?, NULL, '[]')
                """,
                (
                    budget.request_id,
                    budget.mode.value,
                    budget.started_at.isoformat(),
                    budget.deadline_at.isoformat(),
                ),
            )
            return int(cursor.lastrowid)
```

Add `record_source_attempt(request_run_id, attempt)`,
`finalize_request(request_run_id, status, finished_at, omitted_work)`,
`save_decision_snapshot(request_run_id, route, policy, registry, created_at)`,
and `source_attempt_history(source_id, field_id, subject_key)` with the exact
typed arguments named here. Accept immutable records, not free-form
dictionaries. Each write uses a parent transaction. Snapshot save computes the
route checksum over canonical JSON and stores complete policy/registry bytes;
reads recompute all checksums before constructing records. Finalization uses a
single guarded `UPDATE request_runs SET status = ?, finished_at = ?,
omitted_work_json = ? WHERE id = ? AND status = 'running'` and requires exactly
one affected row. Attempt insertion validates request ownership and attempt
number before its single `INSERT`.

- [ ] **Step 4: Prove worker/storage separation**

Add an AST/import test asserting worker modules do not import
`kunjin.storage`, `kunjin.paths`, `kunjin.security`, or
`kunjin.adapters.yangjibao`.

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_store.py \
  tests/unit/test_decision_worker.py
```

- [ ] **Step 5: Commit Task 5**

```bash
git add src/kunjin/decision/store.py tests/unit/test_decision_store.py \
  tests/unit/test_decision_worker.py
git commit -m "feat: persist bounded decision audits"
```

## Task 6: Implement Source Health, Cooldown, And Supplementation

**Files:**

- Create: `src/kunjin/decision/health.py`
- Modify: `src/kunjin/decision/models.py`
- Modify: `src/kunjin/decision/store.py`
- Modify: `src/kunjin/storage/schema.py`
- Modify: `src/kunjin/storage/repository.py`
- Test: `tests/unit/test_decision_health.py`
- Test: `tests/unit/test_decision_policy.py`
- Test: `tests/unit/test_decision_store.py`
- Create: `tests/unit/test_schema_v15.py`

Task 6 may add Schema V15 with the narrow append-only
`source_work_authorizations` table and `source_attempts.authorization_id`.
This revision is required because process-local retry or force consumption
cannot enforce one reservation across independent service/store instances.
The table is limited to request/source/field/subject/attempt authorization and
must not become a task queue or general evidence store.

- [ ] **Step 1: Write the complete state matrix as failing tests**

Test `not_checked`, current success, stale success, active transient cooldown,
expired cooldown without success, permanent 404/410, explicit unsupported
field, successful alternative, partial dated fallback, and all alternatives
exhausted. Prove `manual_supplement_required` is never a
`SourceFieldState`. Force requires deep mode and a non-empty owner reason, is
recorded once, and cannot be inherited by ordinary requests.

- [ ] **Step 2: Run red health tests**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_health.py
```

Expected: missing `SourceHealthService`.

- [ ] **Step 3: Implement deterministic source-field projection**

Use this precedence:

```python
def source_field_state(history, field_policy, now):
    if not history:
        return SourceFieldState.NOT_CHECKED
    latest = history[0]
    if latest.outcome == SourceAttemptOutcome.UNSUPPORTED:
        return SourceFieldState.UNSUPPORTED
    if latest.cooldown_until is not None and now < latest.cooldown_until:
        return SourceFieldState.COOLDOWN
    successful = tuple(
        item
        for item in history
        if item.outcome in {
            SourceAttemptOutcome.SUCCESS,
            SourceAttemptOutcome.CACHE_HIT,
        }
    )
    if successful and field_policy.is_current(successful[0].data_as_of, now):
        return SourceFieldState.HEALTHY
    if successful and field_policy.is_usable(successful[0].data_as_of, now):
        return SourceFieldState.DEGRADED
    return SourceFieldState.UNAVAILABLE
```

Derive usability from stored dates plus field policy; do not store convenience
booleans. Permanent 404/410 and audited unsupported contracts are unsupported,
not recurring cooldown failures.

- [ ] **Step 4: Resolve alternatives and retry policy**

Return `usable` only when action evidence is met, `partial` when dated facts
remain but action evidence is incomplete, and `manual_supplement_required`
only after all acceptable alternatives fail. Allow one retry only for a
retryable transient network failure with sufficient budget. HTTP 4xx,
paywall/auth shell, identity conflict, validation, and parse failures receive no
retry. Initial cooldown is 30 minutes.

- [ ] **Step 5: Verify and commit Task 6**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_health.py
.venv/bin/ruff check src/kunjin/decision/health.py tests/unit/test_decision_health.py
git diff --check
git add src/kunjin/decision/health.py tests/unit/test_decision_health.py
git commit -m "feat: add deterministic source health"
```

## Task 7: Route Existing Disclosure Fetches Through The Worker

**Files:**

- Modify: `src/kunjin/funds/sources.py`
- Modify: `src/kunjin/funds/service.py`
- Modify: `src/kunjin/cli.py`
- Test: `tests/unit/test_fund_disclosure_service.py`
- Test: `tests/integration/test_cli.py`

- [ ] **Step 1: Write failing bounded-sync tests**

Cover one queried fund, per-section source identity, parsing in the parent,
partial publication when another worker fails, no publication after deadline,
no scheduling after expiry, one transient retry, no 4xx retry, cooldown skip,
force-deep bypass, Ctrl-C cleanup/finalization, and terminal response by
deadline. Assert worker code cannot write SQLite.

- [ ] **Step 2: Run disclosure tests red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_fund_disclosure_service.py \
  tests/integration/test_cli.py -k 'bounded or budget or cooldown or fund_disclosure'
```

Expected: sync APIs lack request context and request metadata.

- [ ] **Step 3: Add explicit optional request context**

```python
@dataclass(frozen=True)
class SourceRequestContext:
    request_run_id: int
    budget: RequestBudget
    audit_store: DecisionAuditStore
    health_service: SourceHealthService
    force_reason: Optional[str] = None


def sync_profile(
    self,
    fund_code: str,
    *,
    request_context: Optional[SourceRequestContext] = None,
) -> FundDisclosureSyncResult:
    return self._sync(
        fund_code,
        PROFILE_SECTIONS,
        request_context=request_context,
    )
```

Apply the keyword to `sync_holdings`, `sync_classification`, and `sync_all`.
No context preserves current direct-client behavior. With context, use the
bounded worker for each section and parse/publish only after
`budget.require_publishable()` in the parent.

- [ ] **Step 4: Create one audited request per CLI sync**

Add `--mode {rapid,deep}`, `--force`, and `--force-reason` to
`sync fund-profile` and `sync fund-holdings`. Default is rapid. Reject force
unless mode is deep and reason is non-empty. Public output adds:

```json
{
  "request": {
    "request_id": "lowercase UUID hex",
    "mode": "rapid",
    "terminal_status": "complete",
    "deadline_at": "2026-07-16T00:00:00+00:00",
    "omitted_work": []
  }
}
```

Allow `partial` as the other successful terminal status. Never expose local
paths, bodies, tokens, profile values, PIDs, or exception text. Finalize
expired/cancelled requests even when nothing publishes. Partial success remains
exit 0; zero usable sections remains exit 1.

- [ ] **Step 5: Verify and commit Task 7**

```bash
.venv/bin/python -m pytest -q tests/unit/test_fund_disclosure_service.py \
  tests/integration/test_cli.py tests/unit/test_fund_sources.py
.venv/bin/ruff check src/kunjin/funds src/kunjin/cli.py \
  tests/unit/test_fund_disclosure_service.py tests/integration/test_cli.py
git diff --check
git add src/kunjin/funds/sources.py src/kunjin/funds/service.py src/kunjin/cli.py \
  tests/unit/test_fund_disclosure_service.py tests/integration/test_cli.py
git commit -m "feat: bound disclosure synchronization"
```

## Task 8: Implement Per-Action Routing And Decision Snapshots

**Files:**

- Create: `src/kunjin/decision/routing.py`
- Create: `src/kunjin/decision/service.py`
- Test: `tests/unit/test_decision_routing.py`

- [ ] **Step 1: Write failing routing matrix tests**

Test fact research under blocked B, hold under fresh block, hold with missing B,
reduce/full exit under block, buy/add under all B states, and switch
decomposition. Facts remain available; blocked-B hold is at least mature
`no_add`; reductions remain analyzable; purchase legs remain blocked by
incomplete D2/D3/post-trade.

- [ ] **Step 2: Run routing tests red**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_routing.py
```

Expected: router and decision service are absent.

- [ ] **Step 3: Implement one immutable action table**

```python
ACTION_RULES = {
    ActionKind.FACT_RESEARCH: (
        RiskEffect.INFORMATION,
        (),
    ),
    ActionKind.CONTINUE_HOLDING: (
        RiskEffect.RISK_MAINTAINING,
        ("phase_b_context", "phase_e_policy"),
    ),
    ActionKind.REDUCE_TO_CASH: (
        RiskEffect.RISK_REDUCING,
        ("position", "fees", "settlement", "minimum_remainder"),
    ),
    ActionKind.FULL_EXIT: (
        RiskEffect.RISK_REDUCING,
        ("exit_reason", "position", "fees", "settlement", "use_of_proceeds"),
    ),
    ActionKind.BUY_OR_ADD: (
        RiskEffect.RISK_INCREASING,
        ("phase_b", "phase_c", "d1", "d2", "d3", "post_trade"),
    ),
}
```

Expand `switch_funds` into deterministic `switch_reduce` and `switch_buy`
routes; the buy leg gets the full risk-increasing list. Routing never executes
a transaction.

- [ ] **Step 4: Bind safe Phase B metadata and persist**

Read only `SuitabilityService.status()` safe metadata. Fresh blocked B yields
mature `no_add` for holding and blocks risk increase. Missing/stale B yields
`financial_safety_not_current` and never unqualified hold. Facts and reductions
remain research-available. Every result includes policy/registry identities,
`workflow_level`, separate `conclusion_evidence`, opposing evidence, missing
fields, and `action_maturity`; persist its canonical route in one snapshot.
Rapid mode sets workflow level to `rapid_evidence`, never a confidence value.
A non-blocked Phase B still cannot mature `hold` because Phase E is absent;
hold/reduce/exit interpretations remain `experimental_shadow` except the
audited deterministic blocked-B `no_add` rule.

- [ ] **Step 5: Verify and commit Task 8**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_routing.py \
  tests/unit/test_decision_store.py
.venv/bin/ruff check src/kunjin/decision tests/unit/test_decision_routing.py
git diff --check
git add src/kunjin/decision/routing.py src/kunjin/decision/service.py \
  tests/unit/test_decision_routing.py
git commit -m "feat: route fund actions independently"
```

## Task 9: Expose `decision route` And `source status`

**Files:**

- Modify: `src/kunjin/cli.py`
- Test: `tests/integration/test_cli.py`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing parser and JSON contract tests**

Require:

```bash
kunjin --json decision route --action fact_research --action continue_holding
kunjin --json decision route --action switch_funds
kunjin --json decision route --mode deep --action fact_research
kunjin --json source status
kunjin --json source status --fund-code 000000
```

Reject duplicate/zero actions, invalid modes/codes, non-JSON decision route,
private output keys, and unknown states. Switch returns two routes and facts
survive blocked B.

- [ ] **Step 2: Run CLI tests red**

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py tests/test_smoke.py \
  -k 'decision_route or source_status or phase0_commands'
```

Expected: parser rejects both new top-level commands.

- [ ] **Step 3: Extend parser and dependency injection**

Add `decision` and `source` to `_TOP_LEVEL_COMMANDS`. Add optional
`decision_service` and `source_health_service` to `ApplicationContext`, built
from the repository, suitability service, policy, registry, and audit store.
Parser choices come from enum values, not copied string lists.

Decision route is JSON-only and defaults to rapid. Source status accepts an
optional public fund code and returns one row per source/field plus separate
request-field resolutions.

- [ ] **Step 4: Validate before envelope publication**

Validate exact keys, enums, checksums, UTC dates, supplementation fields, and
route invariants. Forbid bodies, local paths, profile inputs, exact amounts,
worker IDs, and exception details. Extend `_command_name_from_argv()` for both
families.

- [ ] **Step 5: Verify and commit Task 9**

```bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py tests/test_smoke.py
.venv/bin/ruff check src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git diff --check
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose decision routing and source health"
```

## Task 10: Update And Synchronize The KunJin Skill

**Files:**

- Modify: `integrations/codex/kunjin-fund/SKILL.md`
- Modify: `tests/test_smoke.py`
- Sync after tests: `/Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md`

- [ ] **Step 1: Write failing Skill behavior tests**

Require per-subquestion routing, facts not gated by B/C, blocked hold plus
`no_add`, reduction analysis under blocked B, separate switch legs, rapid
90-second terminal result, explicit deep, cooldown, supplementation, no
interactive adapter development, no implicit background work, and no mature buy
while D2/D3/post-trade remain incomplete.

- [ ] **Step 2: Run Skill tests red**

```bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k kunjin_skill
```

Expected: old blanket directional gate text fails assertions.

- [ ] **Step 3: Rewrite workflow without weakening safety**

The Skill contract becomes:

```text
decompose into fact, risk-maintaining, risk-reducing, and risk-increasing
-> call decision route with every action
-> answer independently supported facts even when Phase B is blocked
-> disclose safety conflict and at least no_add for blocked hold
-> allow reduction/exit research but require transaction facts for exact action
-> keep buy/add/switch-buy blocked until B/C/D1/D2/D3/post-trade gates exist
-> use rapid by default and explicit deep only when the owner asks
-> return partial facts plus supplementation at deadline/source failure
```

Remove the rule that every hold/reduce/sell request stops immediately at Phase
B. Preserve exact-amount privacy, no automatic trading, D1 `research_only`,
Docker optionality, and every ledger safeguard.

- [ ] **Step 4: Verify repository Skill and synchronize exact bytes**

```bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k kunjin_skill
cp integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
cmp -s integrations/codex/kunjin-fund/SKILL.md \
  /Users/yanzihao/.codex/skills/kunjin-fund/SKILL.md
```

Expected: `cmp` exits 0. Request approval only for the external Skill write.

- [ ] **Step 5: Commit repository-owned Task 10 files**

```bash
git add integrations/codex/kunjin-fund/SKILL.md tests/test_smoke.py
git commit -m "docs: route KunJin fund requests by action"
```

## Task 11: Add Amount-Free Phase 0 Live Acceptance

**Files:**

- Create: `scripts/run_phase0_acceptance.sh`
- Modify: `README.md`
- Modify: `tests/test_smoke.py`

- [ ] **Step 1: Write failing script contract tests**

Assert Bash syntax, repository-root resolution, required six-digit public code,
fresh isolated data/state, rapid mode, pre/post source status, request IDs,
elapsed seconds, no Docker, and no personal profile/amount exports.

- [ ] **Step 2: Run smoke tests red**

```bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k phase0_acceptance
```

Expected: script is absent.

- [ ] **Step 3: Implement the script with exact sequence**

1. Validate `CODE` and `OUTPUT_DIR` arguments.
2. Create a private `mktemp -d` runtime and cleanup trap.
3. Set isolated `KUNJIN_DATA_DIR`, `KUNJIN_STATE_DIR`, and
   `PYTHONPYCACHEPREFIX` below it.
4. Capture version and empty pre-run `source status`.
5. Run rapid `sync fund-profile CODE`, saving JSON, exit, and elapsed seconds.
6. Capture post-run `source status --fund-code CODE`.
7. Run `decision route --action fact_research --action buy_or_add`.
8. Assert terminal elapsed at most 90 seconds, public checksums, at least one
   source attempt, fact availability, and blocked mature purchase direction.
9. Copy only amount-free JSON and summary into `OUTPUT_DIR`.

The script never builds/pulls Docker, synchronizes Yangjibao, opens the personal
database, or repeats polling. A partial source failure passes only when obtained
facts, missing impact, and supplementation are present; an empty envelope fails.

- [ ] **Step 4: Document the Phase 0 boundary and command**

README includes:

```text
Phase 0 proves bounded routing, source health, and graceful partial results.
It does not prove news intelligence, D2/D3, mature hold/sell timing, or an exact
buy amount. Full one-fund usefulness acceptance belongs to Phase 1.
```

Document `scripts/run_phase0_acceptance.sh 000000 OUTPUT_DIR`, explaining that
execution replaces `000000` with an approved public code and no holding is
hard-coded.

- [ ] **Step 5: Verify and commit Task 11**

```bash
/bin/bash -n scripts/run_phase0_acceptance.sh
.venv/bin/python -m pytest -q tests/test_smoke.py -k phase0_acceptance
.venv/bin/ruff check tests/test_smoke.py
git diff --check
git add scripts/run_phase0_acceptance.sh README.md tests/test_smoke.py
git commit -m "test: add phase0 live acceptance"
```

## Task 12: Full Verification And Independent Phase Review

**Files:**

- Create after verification:
  `docs/audits/2026-07-16-kunjin-phase0-independent-review.md`
- Modify only for verified scoped defects: files owned by Tasks 1-11

- [ ] **Step 1: Run the complete local matrix**

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
/bin/bash -n scripts/run_phase0_acceptance.sh
git diff --check
```

Expected: tests, Ruff, Bash, and whitespace checks all pass.

- [ ] **Step 2: Run real amount-free acceptance with approved network**

```bash
scripts/run_phase0_acceptance.sh 000001 \
  /private/tmp/kunjin-phase0-live-$(date +%Y%m%d-%H%M%S)
```

`000001` is an amount-free public acceptance identifier, not a personal holding
assertion. Expected: terminal at most 90 seconds; useful success or bounded
partial output with exact impact/supplementation; no worker remains; no Docker;
no personal amount.

- [ ] **Step 3: Run real process-failure acceptance**

```bash
.venv/bin/python -m pytest -q tests/unit/test_decision_worker.py \
  -k 'timeout or slow or ignore_term or oversized or late'
```

Expected: stuck/sleep, continuous slow output, ignored SIGTERM, oversized IPC,
and late output all terminate with no live fixture PID; only parent writes exist
in SQLite.

- [ ] **Step 4: Perform independent financial and product reviews**

Use two fresh read-only reviewers. Financial review checks Phase B bypass,
blocked-hold `no_add`, reduction availability, and accidental mature buy/sell
timing. Product review checks terminal deadlines, useful partial output,
source-state determinism, cooldown, worker cleanup, privacy, and absence of
adapter/Docker work.

Write observed evidence, unresolved P0/P1/P2, and a fresh beginner-workflow
score. Use 54/100 only as pre-Phase-0 baseline; do not preassign improvement or
claim 90% help.

- [ ] **Step 5: Fix blockers, rerun, commit audit, and stop**

Any P0/P1 is fixed in scope and the full matrix rerun. P2 may remain only with
explicit residual risk and a later task binding.

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
/bin/bash -n scripts/run_phase0_acceptance.sh
git diff --check
git add docs/audits/2026-07-16-kunjin-phase0-independent-review.md
git commit -m "docs: review phase0 usability foundation"
```

Stop for owner confirmation before writing or executing Phase 1.

## Final Acceptance Checklist

- Fact research is not stopped by Phase B/C.
- Blocked-B holding is never unqualified `hold` and is at least `no_add`.
- Risk-reducing research remains available; exact reduction stays gated.
- Buy/add and switch-buy remain blocked without D2/D3/post-trade.
- Phase 0 bounded commands have 90/480-second terminal budgets with cleanup
  inside deadline; legacy commands are not mislabeled with that SLA.
- Killed/expired workers cannot publish or write SQLite.
- Source state and aggregate field resolution are separate enums.
- One transient retry is bounded; deterministic failures are not retried.
- Policy and registry bytes/version/checksum bind snapshots and attempts.
- Partial output carries facts, missing impact, alternatives, and supplementation.
- Normal queries never build adapters/Docker or continue in background.
- Repository and installed Skills are byte-identical.
- No exact profile value, amount, token, body, local path, or raw exception
  appears in JSON, logs, tests, audits, or Git.
- Full tests, Ruff, Bash, live latency, and both independent reviews pass before
  Phase 0 is called complete.
