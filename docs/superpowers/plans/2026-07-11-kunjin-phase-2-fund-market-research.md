# KunJin Phase 2 Fund and Market Research Implementation Plan

> **For agentic workers:** Execute task-by-task with tests before implementation.

**Goal:** Add public fund NAV research and A-share sector-strength observations to KunJin while preserving explicit evidence and missing-data boundaries.

**Architecture:** HTTPS-only Eastmoney adapters normalize public JSON into SQLite schema version 2. Deterministic research functions compute returns, volatility, drawdown, recovery, sector breadth, and ranking. The CLI exposes `sync fund`, `fund research`, `sync market`, and `market sectors` with the existing JSON envelope.

**Tech Stack:** Python 3.9 standard library, SQLite, unittest, synthetic fixtures.

---

### Task 1: Schema Version 2

- [ ] Add `funds`, `fund_nav`, and `sector_snapshots` tables.
- [ ] Add repository upsert/query methods and migration tests.
- [ ] Preserve schema version 1 data during migration.

### Task 2: Public Fund NAV Adapter

- [ ] Add an HTTPS-only Eastmoney NAV client with strict six-digit fund codes.
- [ ] Parse formal NAV dates, unit NAV, accumulated NAV, and daily growth from JSON.
- [ ] Test malformed data, HTTP errors, and synthetic fund history.

### Task 3: Fund Risk Research

- [ ] Compute 30/90/365-day returns from available formal NAV.
- [ ] Compute daily volatility, maximum drawdown, trough date, and recovery date.
- [ ] Return explicit warnings for missing benchmark, manager, fee, and holdings data.
- [ ] Test rising, falling, recovered, and insufficient histories.

### Task 4: Sector Ranking Adapter and Analysis

- [ ] Add HTTPS-only industry and concept ranking reads.
- [ ] Normalize sector code, name, daily change, turnover, advancers, and decliners.
- [ ] Calculate breadth and rank recent strength without treating it as investment merit.
- [ ] Warn that valuation, earnings, flows, and catalysts are absent in phase two.

### Task 5: CLI and Skill Integration

- [ ] Add `sync fund CODE`, `fund research CODE`, `sync market`, and `market sectors`.
- [ ] Update `kunjin-fund` to use implemented commands and retain evidence rules.
- [ ] Run complete regression, credential scan, and destination verification.
