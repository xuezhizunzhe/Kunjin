from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from kunjin.models import AccountObservation, PositionObservation, StoredPosition
from kunjin.storage.schema import SCHEMA_V1, SCHEMA_VERSION


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _as_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


class Repository:
    def __init__(self, database: Path) -> None:
        self.database = Path(database)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(str(self.database))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            with connection:
                connection.executescript(SCHEMA_V1)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, _utc_now().isoformat()),
                )
        self.database.chmod(0o600)

    def table_names(self) -> set:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def begin_sync(self, source: str, trigger: str) -> int:
        with self.connect() as connection, connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(source, trigger, started_at, status) VALUES (?, ?, ?, 'running')",
                (source, trigger, _utc_now().isoformat()),
            )
            return int(cursor.lastrowid)

    def commit_sync(
        self,
        sync_run_id: int,
        raw_snapshots: Sequence[Tuple[str, str, str, datetime]],
        observations: Sequence[Tuple[AccountObservation, Sequence[PositionObservation]]],
    ) -> None:
        for account, positions in observations:
            account.validate()
            for position in positions:
                position.validate()
                if position.source_account_id != account.source_account_id:
                    raise ValueError("position account id does not match account")

        finished_at = _utc_now().isoformat()
        with self.connect() as connection, connection:
            for endpoint, payload_json, checksum, retrieved_at in raw_snapshots:
                connection.execute(
                    """
                    INSERT INTO raw_snapshots(
                        sync_run_id, endpoint, retrieved_at, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (sync_run_id, endpoint, retrieved_at.isoformat(), payload_json, checksum),
                )

            for account, positions in observations:
                connection.execute(
                    """
                    INSERT INTO accounts(source, source_account_id, title, observed_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, source_account_id) DO UPDATE SET
                        title = excluded.title,
                        observed_at = excluded.observed_at
                    """,
                    (
                        account.source,
                        account.source_account_id,
                        account.title,
                        account.observed_at.isoformat(),
                    ),
                )
                account_row = connection.execute(
                    "SELECT id FROM accounts WHERE source = ? AND source_account_id = ?",
                    (account.source, account.source_account_id),
                ).fetchone()
                account_id = int(account_row["id"])
                for position in positions:
                    connection.execute(
                        """
                        INSERT INTO positions(
                            account_id, fund_code, fund_name, share_class, shares,
                            formal_nav, estimated_nav, observed_profit, observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            position.fund_code,
                            position.fund_name,
                            position.share_class,
                            str(position.shares),
                            _as_text(position.formal_nav),
                            _as_text(position.estimated_nav),
                            _as_text(position.observed_profit),
                            position.observed_at.isoformat(),
                        ),
                    )

            connection.execute(
                """
                UPDATE sync_runs
                SET status = 'success', finished_at = ?, error_code = NULL, error_message = NULL
                WHERE id = ?
                """,
                (finished_at, sync_run_id),
            )

    def fail_sync(self, sync_run_id: int, error_code: str, error_message: str) -> None:
        with self.connect() as connection, connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET status = 'failed', finished_at = ?, error_code = ?, error_message = ?
                WHERE id = ?
                """,
                (_utc_now().isoformat(), error_code, error_message, sync_run_id),
            )

    def latest_positions(self) -> List[StoredPosition]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT a.title AS account_title, p.*
                FROM positions p
                JOIN accounts a ON a.id = p.account_id
                WHERE p.observed_at = (
                    SELECT MAX(p2.observed_at) FROM positions p2 WHERE p2.account_id = p.account_id
                )
                ORDER BY a.title, p.fund_code
                """
            ).fetchall()
        return [
            StoredPosition(
                account_title=str(row["account_title"]),
                fund_code=str(row["fund_code"]),
                fund_name=str(row["fund_name"]),
                share_class=row["share_class"],
                shares=Decimal(str(row["shares"])),
                formal_nav=_as_decimal(row["formal_nav"]),
                estimated_nav=_as_decimal(row["estimated_nav"]),
                observed_profit=_as_decimal(row["observed_profit"]),
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
            )
            for row in rows
        ]

    def latest_successful_sync(self, source: str) -> Optional[Dict[str, str]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sync_runs
                WHERE source = ? AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
        return None if row is None else dict(row)

    def latest_raw_snapshot(self) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM raw_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["payload_json"])

    def replace_snapshot(
        self,
        account: AccountObservation,
        positions: Iterable[PositionObservation],
    ) -> None:
        sync_run_id = self.begin_sync(account.source, "test")
        try:
            self.commit_sync(sync_run_id, [], [(account, list(positions))])
        except Exception:
            self.fail_sync(sync_run_id, "validation_error", "snapshot validation failed")
            raise

