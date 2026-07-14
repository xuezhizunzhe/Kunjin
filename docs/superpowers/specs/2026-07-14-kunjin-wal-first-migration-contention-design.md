# KunJin WAL First-Migration Contention Design

## Goal

Make two simultaneous first-time `Repository.migrate()` calls reliably enable
WAL and serialize schema migration without hiding unrelated SQLite failures.

## Root Cause

Both connections currently execute `PRAGMA journal_mode = WAL` before
`BEGIN IMMEDIATE`. During first database creation, SQLite can return
`database is locked` immediately to one connection even though the connection's
normal timeout is configured. A 30-run isolated experiment reproduced 6 failures
without a retry and 0 failures with a bounded retry.

## Design

Add a private `_enable_wal(connection)` helper. It attempts the existing pragma
and retries only when `sqlite3.OperationalError.args` is exactly
`("database is locked",)`. The retry uses a monotonic five-second deadline and a
10ms interval. If the deadline expires, the original SQLite error is re-raised.
Every other error is re-raised immediately.

`Repository.migrate()` calls `_enable_wal(connection)` in the same location as
the existing pragma. `BEGIN IMMEDIATE`, rollback, migrations, validation,
commit, and file permissions remain unchanged.

## Rejected Alternatives

- A separate cross-process lock file adds lifecycle, stale-lock, and platform
  concerns beyond this narrow SQLite boundary.
- Retrying every operational error could conceal corruption, invalid pragmas,
  permissions, or unsupported database behavior.
- Removing the concurrency test would hide a real startup race.

## Verification

- Preserve the existing two-connection migration tests.
- Add a unit test proving a non-lock operational error is raised immediately
  without sleeping.
- Run the V9 and V10 contention tests repeatedly.
- Run the full repository suite and all existing static checks before returning
  to the Docker live build.
