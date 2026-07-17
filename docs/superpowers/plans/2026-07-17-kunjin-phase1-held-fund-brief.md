# KunJin Phase 1 Held-Fund Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build one bounded fund-brief vertical slice that turns current public fund facts, one read-only personal holding observation, a deterministic D2 subset, and official events into an evidence-linked conditional Chinese explanation without an exact amount or automatic trade.

**Architecture:** Add a focused kunjin.brief package above existing decision, disclosure, NAV, portfolio, and peer components. Put NAV and current portfolio refresh under the existing process deadline through strict framed workers, keep completed source attempts and validated cache writes independently auditable, then atomically publish the final decision route, sanitized brief snapshot, and request terminal state. The owner-local response adds an ephemeral amount-free portfolio-weight overlay excluded from persistence and audit checksums.

**Tech Stack:** Python 3.9+, dataclasses, Decimal, SQLite schema v17, existing RequestBudget/worker/source-health code, unittest/pytest, Ruff, Bash acceptance, macOS Keychain, read-only HTTPS.

---

## File Map

Create:

- src/kunjin/brief/__init__.py
- src/kunjin/brief/models.py
- src/kunjin/brief/policy.py
- src/kunjin/brief/store.py
- src/kunjin/brief/nav.py
- src/kunjin/brief/portfolio_worker_protocol.py
- src/kunjin/brief/portfolio_worker_main.py
- src/kunjin/brief/portfolio.py
- src/kunjin/brief/facts.py
- src/kunjin/brief/d2.py
- src/kunjin/brief/engine.py
- src/kunjin/brief/research.py
- src/kunjin/brief/service.py
- tests/unit/test_brief_models_policy.py
- tests/unit/test_brief_store.py
- tests/unit/test_brief_nav.py
- tests/unit/test_brief_portfolio_worker.py
- tests/unit/test_brief_facts.py
- tests/unit/test_brief_d2.py
- tests/unit/test_brief_engine.py
- tests/unit/test_brief_research.py
- tests/unit/test_brief_service.py
- tests/unit/test_schema_v16.py
- scripts/run_phase1_acceptance.sh
- docs/audits/2026-07-17-kunjin-phase1-independent-review.md after verification.

Modify:

- src/kunjin/storage/schema.py
- src/kunjin/storage/repository.py
- src/kunjin/decision/models.py
- src/kunjin/decision/worker.py
- src/kunjin/decision/worker_protocol.py
- src/kunjin/decision/worker_main.py
- src/kunjin/decision/store.py
- src/kunjin/services/sync.py
- src/kunjin/cli.py
- relevant decision, sync, CLI, smoke, README, and Skill tests/docs.

Do not modify or stage the six pre-existing untracked Phase D/Phase 0 plan/spec files unless the owner separately requests it.

---

### Task 1: Exact Brief Models And Policy V1

**Files:**

- Create: src/kunjin/brief/__init__.py
- Create: src/kunjin/brief/models.py
- Create: src/kunjin/brief/policy.py
- Create: tests/unit/test_brief_models_policy.py

- [x] **Step 1: Write failing enum, shape, privacy, and checksum tests**

Require these exact enums:

~~~python
assert tuple(item.value for item in BriefState) == (
    "no_add", "hold", "watch", "reduce_or_exit_review", "abstain"
)
assert tuple(item.value for item in BriefEvidenceState) == (
    "complete", "partial", "insufficient"
)
assert HeldFundBriefPolicyV1().checksum() == (
    HELD_FUND_BRIEF_POLICY_V1_GOLDEN_CHECKSUM
)
~~~

Reject subclasses, unknown keys, floats, non-UTC times, duplicate IDs, unbounded text, and private keys. Inject Decimal("73129.17") through facts, relationships, state inputs, and nested maps; BriefSnapshot.canonical_json() must reject every path.

Persisted brief records represent public numeric facts with bounded canonical
strings. Reject every Decimal found anywhere in the BriefSnapshot tree; apply
this snapshot privacy scanner only to persisted brief records, not to the
separately validated public thresholds inside HeldFundBriefPolicyV1.

Dynamic persisted fact and relationship trees accept only exact bounded public
scalars, tuples, and defensively copied mappings. Reject arbitrary Enum,
dataclass, integer numeric, mutable backing-map, URL query, and unresolved
cross-record reference paths. Private-path matching must reject personal asset
values and managed paths without rejecting public taxonomy fields such as
asset_class.

- [x] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_models_policy.py
~~~

Expected: import failure because kunjin.brief does not exist.

- [x] **Step 3: Implement exact immutable records**

Define BriefState, BriefEvidenceState, OfficialEventCode, BriefFact, OfficialEvent, RelationshipEvidence, BriefCoverage, BriefActionInterpretation, BriefSnapshot, and HeldFundBriefReport. BriefSnapshot excludes owner weight; HeldFundBriefReport holds it only in an optional ephemeral overlay.

BriefSnapshot.request_run_id and BriefSnapshot.decision_snapshot_id are exact
positive integers bound to the existing SQLite request_runs.id and
decision_snapshots.id primary keys. They are not the external 32-hex request
identifier or a checksum.

Require one exact owner-action shape: fact_research plus continue_holding,
reduce_to_cash, or full_exit; or fact_research plus both switch_reduce and
switch_buy. Every non-fact action has one interpretation, every evidence and
source-lineage reference resolves inside the snapshot, and OfficialEvent accepts
only authenticated Tier 1 evidence.

Reuse decision validators for identifiers, checksums, request IDs, text, tuples, and aware datetimes. Recursively reject amount, shares, cost, profit, income, debt, reserve, asset, loss_budget, token, credential, ciphertext, nonce, and private paths from persisted records.

- [x] **Step 4: Implement canonical policy**

Fix these V1 values:

~~~python
RAPID_NAV_MAX_PAGES = 6
DEEP_NAV_MAX_PAGES = 50
MIN_CORRELATION_SAMPLES = 60
MAX_OFFICIAL_EVENTS = 20
MAX_FACTS = 128
MAX_RELATIONSHIPS = 128
~~~

Serialize state precedence, fact requirements per action, exact official-event rules, and exact false amount availability to canonical ASCII JSON. Compute and pin its lowercase SHA-256.

- [x] **Step 5: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_models_policy.py
.venv/bin/ruff check src/kunjin/brief tests/unit/test_brief_models_policy.py
git add src/kunjin/brief/__init__.py src/kunjin/brief/models.py \
  src/kunjin/brief/policy.py tests/unit/test_brief_models_policy.py
git commit -m "feat: define held fund brief contract"
~~~

---

### Task 2: Schema V16 And Atomic Brief Store

**Files:**

- Modify: src/kunjin/storage/schema.py
- Modify: src/kunjin/storage/repository.py
- Modify: src/kunjin/decision/store.py
- Create: src/kunjin/brief/store.py
- Create: tests/unit/test_schema_v16.py
- Create: tests/unit/test_brief_store.py
- Modify: tests/unit/test_decision_store.py

- [x] **Step 1: Write failing migration and store tests**

Require versions 1 through 16, exact schema objects, immutable policy/snapshot rows, canonical JSON, lowercase SHA-256, UTC timestamps, one brief per request, valid request/decision bindings, and byte preservation while migrating v13/v14/v15.

Attempt snapshot JSON containing portfolio_weight, shares, or observed_profit and require rejection.

- [x] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_schema_v16.py \
  tests/unit/test_brief_store.py tests/unit/test_decision_store.py
~~~

- [x] **Step 3: Add strict schema v16**

Create brief_policy_versions and fund_brief_snapshots. The snapshot stores:

~~~text
request_run_id, decision_snapshot_id, fund_code, action_ids_json,
primary_state, action_maturity, triggered_reviews_json,
affected_action_abstentions_json, blocking_codes_json, evidence_state,
missing_fields_json, conflicts_json, source_lineage_ids_json,
evidence_fingerprint, canonical_snapshot_json, result_checksum,
conclusion_changed, created_at
~~~

Use strict JSON/enums/fund-code/checksum/UTC constraints, request and decision foreign keys, a running-request insert guard, and no-replace/no-update/no-delete triggers.

- [x] **Step 4: Support caller-owned transactions in DecisionAuditStore**

Add optional exact sqlite3.Connection parameters to decision-snapshot save and request finalization, following record_source_attempt ownership. A supplied connection is never begun, committed, rolled back, or closed by DecisionAuditStore. Preserve all current deadline, pending-authorization, and exactly-once checks.

- [x] **Step 5: Implement BriefStore.publish**

BriefStore.publish accepts an internal exact snapshot factory rather than a
BriefSnapshot with a fake database ID. After DecisionAuditStore inserts and
reloads the decision snapshot on the caller-owned transaction, pass the real
positive request_runs.id and decision_snapshots.id to the factory, require an
exact validated BriefSnapshot with those bindings, and only then insert it.
Never predict an AUTOINCREMENT value or publish the decision row early.

~~~text
BEGIN IMMEDIATE
-> authenticate Brief Policy V1
-> save final DecisionRoute without completing request
-> insert sanitized BriefSnapshot
-> reload and byte-compare row
-> finalize request complete/partial
-> recheck RequestBudget
-> COMMIT
~~~

Rollback all final artifacts on failure. Completed source attempts and validated cache writes survive. History returns at most 64 authenticated snapshots and derives conclusion_changed from sanitized snapshots only.

- [x] **Step 6: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_schema_v16.py \
  tests/unit/test_brief_store.py tests/unit/test_decision_store.py
.venv/bin/ruff check src/kunjin/storage src/kunjin/decision/store.py \
  src/kunjin/brief/store.py tests/unit/test_schema_v16.py \
  tests/unit/test_brief_store.py tests/unit/test_decision_store.py
git add src/kunjin/storage/schema.py src/kunjin/storage/repository.py \
  src/kunjin/decision/store.py src/kunjin/brief/store.py \
  tests/unit/test_schema_v16.py tests/unit/test_brief_store.py \
  tests/unit/test_decision_store.py
git commit -m "feat: persist held fund brief snapshots"
~~~

---

### Task 3: Reusable Strict Worker Transport

**Files:**

- Modify: src/kunjin/decision/worker.py
- Modify: tests/unit/test_decision_worker.py

- [x] **Step 1: Add failing transport-preservation tests**

Exercise an internal framed runner with injected exact encoder/decoder/validator/module/max-size values. Preserve clean environment, no shell, new session, nonblocking I/O, total deadline, TERM/KILL/reap, oversized rejection, child cleanup, late-output rejection, and public URL validation.

- [x] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_decision_worker.py
~~~

- [x] **Step 3: Extract the internal transport**

Keep run_public_worker observable behavior unchanged. Permit only internal environment profiles:

~~~python
PUBLIC_WORKER_ENV = "public"
PRIVATE_KEYCHAIN_WORKER_ENV = "private_keychain"
~~~

Neither profile accepts caller environment or includes token, cookie, authorization, proxy, Python path, temp directory, or credentials. The private profile may add only a tested macOS login-session value needed by /usr/bin/security.

- [x] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_decision_worker.py \
  tests/test_smoke.py -k "decision_worker or phase0_acceptance"
.venv/bin/ruff check src/kunjin/decision/worker.py \
  tests/unit/test_decision_worker.py
git add src/kunjin/decision/worker.py tests/unit/test_decision_worker.py
git commit -m "refactor: share bounded worker transport"
~~~

---

### Task 4: Formal NAV Under RequestBudget

**Files:**

- Modify: src/kunjin/adapters/eastmoney.py
- Modify: src/kunjin/models.py
- Modify: src/kunjin/storage/schema.py
- Modify: src/kunjin/storage/repository.py
- Modify: src/kunjin/decision/health.py
- Modify: src/kunjin/decision/store.py
- Modify: src/kunjin/decision/worker_protocol.py
- Modify: src/kunjin/decision/worker_main.py
- Modify: src/kunjin/decision/worker.py
- Create: src/kunjin/brief/nav.py
- Create: tests/unit/test_brief_nav.py
- Modify: tests/unit/test_decision_worker.py
- Modify: tests/unit/test_eastmoney.py
- Modify: tests/unit/test_repository.py
- Modify: tests/unit/test_schema_v4.py through tests/unit/test_schema_v16.py

- [x] **Step 1: Write failing NAV protocol/service tests**

Cover exact fund binding, Rapid six-page and Deep 50-page limits, malformed numeric values, duplicate dates, wrong code, noncanonical/oversized output, continuity ambiguity, cooldown, retry authorization, deadline, child cleanup, parent-only persistence, and late-write rejection.

- [x] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_nav.py \
  tests/unit/test_decision_worker.py
~~~

- [x] **Step 3: Add fund_nav_fetch**

The exact child request arguments are:

~~~json
{"fund_code":"000001","max_pages":"6"}
~~~

The child uses EastmoneyFundClient and returns canonical normalized public fund identity, retrieval time, count, bounded NAV rows, and only the bounded corporate-action state `none|present|unknown`. It returns no raw body, corporate-action text, or arbitrary URL. The parent revalidates every value, chronology, duplicate, size, identity, date, canonical decimal, and request lifetime.

- [x] **Step 4: Implement BoundedNavService**

Check source health for eastmoney_nav/formal_nav and adjusted_return_series, run at most one worker job, record authenticated attempts for both registry fields, check publishability, and then persist. Normalize the trusted expected NAV input to the caller's calendar date and store that trading date as UTC midnight for freshness comparison. Keep latest formal NAV usable when adjusted-series continuity/sample evidence is insufficient.

Schema v17 adds only bounded `corporate_action_state` and a nullable `source_attempt_id` foreign key to cached NAV rows, defaulting legacy rows to `unknown` and an unauthenticated null binding. In the parent transaction, insert the formal and adjusted attempts first, then bind every live NAV row to the authenticated formal success attempt before commit. Generic repository writes remain unbound and cannot become brief cache evidence. Rebuild adjusted-series quality from the latest authenticated `retrieved_at` batch using the same rules as live data; older overlapping or newer unbound rows never enter that selected window. Require 60 samples, complete positive accumulated NAV, corporate-action state `none`, constant `accumulated_nav - unit_nav` across the selected window, and no sign conflict between each published daily growth and adjacent unit-NAV change. These are exact continuity invariants, not magnitude thresholds. Missing or conflicting evidence leaves adjusted_return_series unavailable while the shared endpoint acquisition remains evidenced by formal_nav success; only a current formal-NAV cache, or a dated cache when no trusted expected trading date exists, prevents repeated networking.

- [x] **Step 5: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_nav.py \
  tests/unit/test_decision_worker.py tests/unit/test_decision_health.py
.venv/bin/ruff check src/kunjin/decision src/kunjin/brief/nav.py \
  tests/unit/test_brief_nav.py tests/unit/test_decision_worker.py
git add src/kunjin/decision/worker_protocol.py \
  src/kunjin/decision/worker_main.py src/kunjin/decision/worker.py \
  src/kunjin/brief/nav.py tests/unit/test_brief_nav.py \
  tests/unit/test_decision_worker.py
git commit -m "feat: bound formal nav synchronization"
~~~

---

### Task 5: Credential-Isolated Read-Only Portfolio Worker

**Files:**

- Modify: src/kunjin/decision/models.py
- Create: src/kunjin/brief/portfolio_worker_protocol.py
- Create: src/kunjin/brief/portfolio_worker_main.py
- Create: src/kunjin/brief/portfolio.py
- Modify: src/kunjin/services/sync.py
- Create: tests/unit/test_brief_portfolio_worker.py
- Modify: tests/unit/test_sync.py
- Modify: tests/unit/test_decision_health.py

- [ ] **Step 1: Write failing credential/lifecycle tests**

With token and numeric sentinels, prove the token is absent from argv, environment, temp files, request frame, response frame, stderr, logs, exceptions, and audit rows. Cover missing/expired auth, rate limit, malformed records, size, timeout, ignored termination, child process, late output, parent-only writes, and no external mutation.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_portfolio_worker.py \
  tests/unit/test_sync.py tests/unit/test_decision_health.py
~~~

- [ ] **Step 3: Define the no-secret request**

~~~json
{
  "schema_version":1,
  "request_id":"1234567890abcdef1234567890abcdef",
  "operation":"portfolio_observation"
}
~~~

The child loads KeychainTokenStore itself, calls only list_accounts/list_holdings, validates records, and returns typed observations without token, signing secret, headers, signature, or raw API bodies. Add stable authentication_required as an unavailable, non-transient source error without changing EvidencePolicy/SourceRegistry checksums.

- [ ] **Step 4: Implement parent validation and commit**

BoundedPortfolioService uses the private environment, validates all observations again, records one yangjibao_portfolio_observation/personal_position_observation attempt, checks budget, then calls parent-only PortfolioSyncService.commit_observations(). Raw snapshots are omitted on this path.

- [ ] **Step 5: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_portfolio_worker.py \
  tests/unit/test_sync.py tests/unit/test_decision_health.py \
  tests/unit/test_decision_worker.py
.venv/bin/ruff check src/kunjin/brief src/kunjin/services/sync.py \
  src/kunjin/decision/models.py tests/unit/test_brief_portfolio_worker.py \
  tests/unit/test_sync.py
git add src/kunjin/decision/models.py \
  src/kunjin/brief/portfolio_worker_protocol.py \
  src/kunjin/brief/portfolio_worker_main.py src/kunjin/brief/portfolio.py \
  src/kunjin/services/sync.py tests/unit/test_brief_portfolio_worker.py \
  tests/unit/test_sync.py tests/unit/test_decision_health.py
git commit -m "feat: bound private portfolio observation"
~~~

---

### Task 6: Source-Linked Facts And Official Events

**Files:**

- Create: src/kunjin/brief/facts.py
- Create: tests/unit/test_brief_facts.py

- [ ] **Step 1: Write failing selection/event tests**

Use Tier 1/Tier 2 conflicts, former/current managers, A/C fees, stale/top-ten holdings, missing redemption periods, and reprints. Every fact must resolve to a projected source and retain data/publication/retrieval dates.

Tier 2 announcement indexes may remain attributed facts but cannot create mature official events. Only a validated Tier 1 manager-domain liquidation/termination source can trigger mature review.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_facts.py
~~~

- [ ] **Step 3: Build facts and official events**

Reuse build_disclosure_report selection semantics, then project bounded BriefFact records. Add latest formal NAV and current D1 classification with exact status/tags. Classify official events with anchored normalized-title patterns and source metadata. Preserve unmatched Tier 1 as other_official_product_notice and Tier 2 outside official_events. Never infer correction/retraction or independence from title similarity.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_facts.py
.venv/bin/ruff check src/kunjin/brief/facts.py tests/unit/test_brief_facts.py
git add src/kunjin/brief/facts.py tests/unit/test_brief_facts.py
git commit -m "feat: build sourced held fund facts"
~~~

---

### Task 7: Position, Economic, Manager, Company, And Index D2

**Files:**

- Create: src/kunjin/brief/d2.py
- Create: tests/unit/test_brief_d2.py

- [ ] **Step 1: Write failing relationship tests**

Cover multi-account duplicates, A/C siblings, exact same index, similar family/different index, same manager/different company, same company/different manager, effective-date conflicts, missing NAV, stale portfolio, and private amount sentinels.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_d2.py \
  -k "position or sibling or manager or company or index"
~~~

- [ ] **Step 3: Implement amount-free relationships**

Reuse analyze_portfolio for weights/HHI, then discard total value and profit. Aggregate authenticated sibling codes into one economic exposure. Compare exact current benchmarks, not family similarity. Keep manager and company records separate. Return unknown with exact missing/conflict codes.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_d2.py \
  -k "position or sibling or manager or company or index"
.venv/bin/ruff check src/kunjin/brief/d2.py tests/unit/test_brief_d2.py
git add src/kunjin/brief/d2.py tests/unit/test_brief_d2.py
git commit -m "feat: calculate held fund relationships"
~~~

---

### Task 8: Coverage-Aware Overlap And Adjusted Correlation

**Files:**

- Modify: src/kunjin/brief/d2.py
- Modify: tests/unit/test_brief_d2.py
- Modify only when shared behavior changes: src/kunjin/funds/peers/analytics.py
- Modify only when shared behavior changes: tests/unit/test_peer_analytics.py

- [ ] **Step 1: Write failing overlap/correlation tests**

Cover report-period alignment, top-ten/complete scopes, missing/stale holdings, omitted weights, partial disclosed weight, identity conflicts, and unknown exposure. Correlation requires accumulated NAV or validated total return, aligned dates, common end, 60 samples, nonzero variance, and no discontinuity. Unit-NAV-only, 59 samples, duplicates, or ambiguous corporate actions return insufficient_data.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_d2.py \
  tests/unit/test_peer_analytics.py -k "overlap or correlation"
~~~

- [ ] **Step 3: Reuse overlap and add Decimal Pearson correlation**

Preserve top10_disclosed_overlap, both periods/publication times, disclosed coverage, included/omitted codes, and warnings. Calculate Pearson correlation over aligned adjusted-return changes, never NAV levels. Output samples, dates, coverage, calculation version, and insufficiency codes. Do not create a combined score.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_d2.py \
  tests/unit/test_peer_analytics.py
.venv/bin/ruff check src/kunjin/brief/d2.py \
  src/kunjin/funds/peers/analytics.py tests/unit/test_brief_d2.py \
  tests/unit/test_peer_analytics.py
git add src/kunjin/brief/d2.py tests/unit/test_brief_d2.py
git diff --quiet -- src/kunjin/funds/peers/analytics.py || \
  git add src/kunjin/funds/peers/analytics.py tests/unit/test_peer_analytics.py
git commit -m "feat: add coverage aware d2 evidence"
~~~

---

### Task 9: Evidence Sufficiency And Action Interpretation

**Files:**

- Create: src/kunjin/brief/engine.py
- Create: tests/unit/test_brief_engine.py

- [ ] **Step 1: Write the confirmed state matrix**

Cover blocked no_add; blocked plus liquidation retaining both states; identity-conflict abstention; missing fee while watch remains; risk-event watch; thesis-backed experimental hold; no-thesis watch; one-day/ranking/media non-triggers; reduce/exit under block; independent switch legs; and exact false everywhere.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_engine.py
~~~

- [ ] **Step 3: Implement HeldFundBriefEngine**

Consume typed DecisionRoute, facts, events, D2, source resolutions, and confirmed theses. Produce separate sync_status and decision_evidence_status. Preserve simultaneous constraints, triggered reviews, and affected abstentions; primary state is presentation only. Do not parse free-form text here.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_engine.py \
  tests/unit/test_decision_routing.py
.venv/bin/ruff check src/kunjin/brief/engine.py \
  tests/unit/test_brief_engine.py
git add src/kunjin/brief/engine.py tests/unit/test_brief_engine.py
git commit -m "feat: interpret held fund evidence"
~~~

---

### Task 10: Strict JSON And Chinese Projection

**Files:**

- Create: src/kunjin/brief/research.py
- Create: tests/unit/test_brief_research.py

- [ ] **Step 1: Write failing exact-output tests**

Require the nine confirmed sections, exact nested keys, stable ordering, canonical sanitized snapshot, valid source references, bounded Chinese text, and no unknown fields. Test all evidence/states/switch output. Search for private sentinels. Forbid translating mature into financial certainty and forbid bare unconditional buy/sell/hold headlines.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_research.py
~~~

- [ ] **Step 3: Implement the three projections**

~~~python
def build_snapshot(...) -> BriefSnapshot: ...
def build_owner_report(snapshot, portfolio_weight) -> HeldFundBriefReport: ...
def public_payload(report) -> dict[str, object]: ...
~~~

Use fixed code-driven Chinese templates for headline, fund identity, portfolio relationship, official events, supporting/opposing evidence, gaps, and change conditions. Stable English codes remain adjacent.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_research.py
.venv/bin/ruff check src/kunjin/brief/research.py \
  tests/unit/test_brief_research.py
git add src/kunjin/brief/research.py tests/unit/test_brief_research.py
git commit -m "feat: project beginner held fund brief"
~~~

---

### Task 11: Single-Budget Orchestration

**Files:**

- Create: src/kunjin/brief/service.py
- Create: tests/unit/test_brief_service.py
- Modify: src/kunjin/brief/__init__.py

- [ ] **Step 1: Write failing orchestration tests**

Prove one request ID/budget binds route, source work, facts, D2, state, snapshot, and output. Cover Rapid/Deep, priority, public partials, portfolio failure, cooldown, expiry at every boundary, cancellation, late result, final rollback, prior snapshot preservation, and no background work.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_service.py
~~~

- [ ] **Step 3: Implement HeldFundBriefService.brief**

~~~text
create RequestBudget
-> begin request
-> route fact_research plus owner action
-> create one SourceRequestContext
-> bounded portfolio
-> bounded profile/holdings/announcement
-> bounded NAV
-> load authenticated bundle/history/classification/thesis
-> build facts/events/D2/evidence/action
-> build sanitized snapshot plus owner overlay
-> atomically publish route + snapshot + terminal state
-> return report
~~~

Every exception finalizes failed/cancelled/expired with exact omitted work. Do not delete successful source/cache evidence.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/unit/test_brief_service.py \
  tests/unit/test_brief_store.py tests/unit/test_fund_disclosure_service.py \
  tests/unit/test_brief_nav.py tests/unit/test_brief_portfolio_worker.py
.venv/bin/ruff check src/kunjin/brief tests/unit/test_brief_service.py
git add src/kunjin/brief/__init__.py src/kunjin/brief/service.py \
  tests/unit/test_brief_service.py
git commit -m "feat: orchestrate held fund brief"
~~~

---

### Task 12: JSON-Only CLI

**Files:**

- Modify: src/kunjin/cli.py
- Modify: tests/integration/test_cli.py
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing CLI tests**

Cover exact invocation, missing JSON, invalid code/action/mode, current holding, auth missing, blocked B, partial profile, unsupported holdings, liquidation, no thesis, thesis, reduce, exit, switch, exact schema, and privacy.

~~~bash
kunjin --json fund brief 519755 --action continue_holding --mode rapid
~~~

No amount/shares/date/URL/adapter/background/Docker option exists.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py \
  tests/test_smoke.py -k "fund_brief or phase0"
~~~

- [ ] **Step 3: Wire thin context and dispatch**

Instantiate brief dependencies once, validate arguments/JSON, call service, and envelope strict output. Keep business rules out of cli.py. Expected partial/insufficient outcomes exit zero; invalid usage or inability to produce a valid terminal envelope exits nonzero.

- [ ] **Step 4: Verify and commit**

~~~bash
.venv/bin/python -m pytest -q tests/integration/test_cli.py \
  tests/test_smoke.py -k "fund_brief or phase0"
.venv/bin/ruff check src/kunjin/cli.py tests/integration/test_cli.py \
  tests/test_smoke.py
git add src/kunjin/cli.py tests/integration/test_cli.py tests/test_smoke.py
git commit -m "feat: expose held fund brief command"
~~~

---

### Task 13: Safe Live Acceptance

**Files:**

- Create: scripts/run_phase1_acceptance.sh
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing offline acceptance tests**

Use fake CLI healthy/unsupported/owner projections and all action routes. Cover timeout, interrupt, ignored TERM, detached descendants, oversized output, unknown fields, output conflict/inode replacement, and private sentinel rejection.

Exact public interface:

~~~bash
scripts/run_phase1_acceptance.sh \
  HEALTHY_PUBLIC_CODE UNSUPPORTED_PUBLIC_CODE OUTPUT_DIR
~~~

Codes differ; output must not exist. Real-owner acceptance remains separate and private.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k phase1_acceptance
~~~

- [ ] **Step 3: Implement strict public acceptance**

Reuse reviewed Phase 0 process ownership, deadline, strict projection, private staging, exclusive rename, inode verification, and cleanup. Never copy raw JSON. Healthy output must contain useful sourced facts, not counts. Unsupported output must contain gaps and supplementation. Validate continue, reduce, exit, and both switch legs with exact false.

- [ ] **Step 4: Add private owner projection**

Use a random subject ID and retain only position-present, state, maturity, coverage class, elapsed time, and stable codes. Destroy the private ID mapping. Never retain the fund code, amount, shares, cost, profit, weight, account title, or full holdings.

- [ ] **Step 5: Verify and commit**

~~~bash
/bin/bash -n scripts/run_phase1_acceptance.sh
.venv/bin/python -m pytest -q tests/test_smoke.py -k phase1_acceptance
.venv/bin/ruff check tests/test_smoke.py
git diff --check
git add scripts/run_phase1_acceptance.sh tests/test_smoke.py
git commit -m "test: add held fund brief acceptance"
~~~

---

### Task 14: README And Codex Skill

**Files:**

- Modify: README.md
- Modify: integrations/codex/kunjin-fund/SKILL.md
- Modify: integrations/codex/kunjin-fund/agents/openai.yaml
- Modify after validation/approval: /Users/yanzihao/.codex/skills/kunjin-fund/
- Modify: tests/test_smoke.py

- [ ] **Step 1: Write failing contract tests**

Require fund brief for one held-fund question, combined fact/action routing, split sync/decision evidence, Tier 2/date labels, D2 coverage, official-event limits, exact false, and conditional wording. Require explicit statements that broad news, complete D2, D3, and Phase E remain absent.

- [ ] **Step 2: Confirm red**

~~~bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k kunjin_skill
~~~

- [ ] **Step 3: Update repository docs/Skill**

Add command, state/evidence explanation, limitations, privacy, Rapid/Deep, no background work, and supplementation. Replace only the obsolete claim that D2 is entirely absent with the precise minimum-subset boundary.

- [ ] **Step 4: Validate and sync installed Skill**

~~~bash
.venv/bin/python -m pytest -q tests/test_smoke.py -k kunjin_skill
python3 /Users/yanzihao/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  integrations/codex/kunjin-fund
git diff --check
~~~

After explicit external-write approval, copy the validated repository directory, compare byte-for-byte, validate installed files, and print SHA-256. Then commit repository files only:

~~~bash
git add README.md integrations/codex/kunjin-fund/SKILL.md \
  integrations/codex/kunjin-fund/agents/openai.yaml tests/test_smoke.py
git commit -m "docs: use held fund brief workflow"
~~~

---

### Task 15: Full Verification, Live Runs, Independent Review, Stop

**Files:**

- Create after evidence: docs/audits/2026-07-17-kunjin-phase1-independent-review.md
- Modify only for verified P0/P1 defects: Task 1-14 owned files.

- [ ] **Step 1: Run complete local matrix**

~~~bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/python -m compileall -q src tests
/bin/bash -n scripts/run_phase0_acceptance.sh
/bin/bash -n scripts/run_phase1_acceptance.sh
git diff --check
~~~

- [ ] **Step 2: Run exact failure nodes**

Run worker, NAV, private portfolio, store rollback, cooldown, late-write, subprocess, and acceptance cases by exact node ID. Confirm all fixture PIDs/groups are gone and only authenticated parent writes exist.

- [ ] **Step 3: Run approved public live acceptance**

Predeclare healthy and unsupported public codes, use a fresh /private/tmp output, and verify 90-second termination, useful facts, dates/tiers, concrete supplementation, all action boundaries, no Docker/adapter work, and no worker residue.

- [ ] **Step 4: Run private owner acceptance**

Use existing read-only auth. Do not print or retain the held code or private values. Ask the owner only to confirm privately that position presence and relationship interpretation match.

- [ ] **Step 5: Dispatch two fresh read-only reviewers**

Financial review: B bypass, no-add, known/unknown D2, events, no inferred hold, reduce/exit, switch independence, no timing/amount.

Product review: 90/480 boundaries, useful partial, Chinese clarity, source/cooldown, cleanup, privacy, atomic current snapshot, no interactive infrastructure.

- [ ] **Step 6: Fix all P0/P1 and bind retained P2**

For each defect: reproduce, add failing test, apply one root-cause fix, run focused tests, obtain independent spec/quality review, and rerun full matrix. Do not increase score for code/test/docs/latency alone.

- [ ] **Step 7: Write and commit audit**

Record evidence, P2, product reliability, fresh beginner score, and explicit 90-percent conclusion.

~~~bash
git add docs/audits/2026-07-17-kunjin-phase1-independent-review.md
git commit -m "docs: review held fund brief usability"
~~~

- [ ] **Step 8: Stop at Phase 2 owner gate**

Report commits, evidence directories, tests, scores, and limits. Do not design or execute Phase 2 before confirmation.

---

## Plan Self-Review Checklist

- [x] Every approved design requirement maps to a task.
- [x] Facts remain independent from Phase B/C.
- [x] Current NAV and position refresh are inside RequestBudget.
- [x] Credentials never enter argv, environment, temp files, IPC output, logs, audit, tests, Git, or retained acceptance.
- [x] Source attempts/cache survive report failure; route + sanitized snapshot + terminal state publish atomically.
- [x] Persisted snapshots exclude portfolio weight and all amount-derived values.
- [x] Minimum D2 never turns missing exposure into diversification.
- [x] Tier 2 announcement indexes cannot create mature official events.
- [x] No state exposes exact amount or automatic trade.
- [x] Phase 0 acceptance and installed Skill equality remain gates.
- [x] Six pre-existing untracked plan/spec files remain untouched.
