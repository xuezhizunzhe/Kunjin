from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from kunjin.models import (
    AccountObservation,
    FundNavObservation,
    InvestmentThesis,
    PositionObservation,
    SectorObservation,
    StoredPosition,
)
from kunjin.storage.schema import SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_VERSION


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
                    (1, _utc_now().isoformat()),
                )
                connection.executescript(SCHEMA_V2)
                connection.execute(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _utc_now().isoformat()),
                )
                connection.executescript(SCHEMA_V3)
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

    def save_fund_history(
        self,
        fund_code: str,
        fund_name: Optional[str],
        fund_type: Optional[str],
        source: str,
        observations: Sequence[FundNavObservation],
    ) -> None:
        for observation in observations:
            observation.validate()
            if observation.fund_code != fund_code:
                raise ValueError("NAV fund code does not match requested fund")
        observed_at = max(
            (item.retrieved_at for item in observations),
            default=_utc_now(),
        ).isoformat()
        with self.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO funds(fund_code, fund_name, fund_type, source, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fund_code) DO UPDATE SET
                    fund_name = COALESCE(excluded.fund_name, funds.fund_name),
                    fund_type = COALESCE(excluded.fund_type, funds.fund_type),
                    source = excluded.source,
                    observed_at = excluded.observed_at
                """,
                (fund_code, fund_name, fund_type, source, observed_at),
            )
            for item in observations:
                connection.execute(
                    """
                    INSERT INTO fund_nav(
                        fund_code, nav_date, unit_nav, accumulated_nav,
                        daily_growth, source, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fund_code, nav_date, source) DO UPDATE SET
                        unit_nav = excluded.unit_nav,
                        accumulated_nav = excluded.accumulated_nav,
                        daily_growth = excluded.daily_growth,
                        retrieved_at = excluded.retrieved_at
                    """,
                    (
                        item.fund_code,
                        item.nav_date.isoformat(),
                        str(item.unit_nav),
                        _as_text(item.accumulated_nav),
                        _as_text(item.daily_growth),
                        item.source,
                        item.retrieved_at.isoformat(),
                    ),
                )

    def fund_history(self, fund_code: str) -> List[FundNavObservation]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fund_nav WHERE fund_code = ? ORDER BY nav_date",
                (fund_code,),
            ).fetchall()
        from datetime import date

        return [
            FundNavObservation(
                fund_code=str(row["fund_code"]),
                nav_date=date.fromisoformat(str(row["nav_date"])),
                unit_nav=Decimal(str(row["unit_nav"])),
                accumulated_nav=_as_decimal(row["accumulated_nav"]),
                daily_growth=_as_decimal(row["daily_growth"]),
                source=str(row["source"]),
                retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            )
            for row in rows
        ]

    def fund_profile(self, fund_code: str) -> Optional[Dict[str, Optional[str]]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM funds WHERE fund_code = ?",
                (fund_code,),
            ).fetchone()
        return None if row is None else dict(row)

    def save_sector_snapshots(self, observations: Sequence[SectorObservation]) -> None:
        for observation in observations:
            observation.validate()
        with self.connect() as connection, connection:
            for item in observations:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sector_snapshots(
                        sector_code, sector_name, sector_kind, pct_change,
                        turnover_rate, advancers, decliners, source, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.sector_code,
                        item.sector_name,
                        item.sector_kind,
                        _as_text(item.pct_change),
                        _as_text(item.turnover_rate),
                        item.advancers,
                        item.decliners,
                        item.source,
                        item.retrieved_at.isoformat(),
                    ),
                )

    def latest_sector_snapshots(self) -> List[SectorObservation]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sector_snapshots
                WHERE retrieved_at = (SELECT MAX(retrieved_at) FROM sector_snapshots)
                ORDER BY CAST(pct_change AS REAL) DESC, sector_name
                """
            ).fetchall()
        return [
            SectorObservation(
                sector_code=str(row["sector_code"]),
                sector_name=str(row["sector_name"]),
                sector_kind=str(row["sector_kind"]),
                pct_change=_as_decimal(row["pct_change"]),
                turnover_rate=_as_decimal(row["turnover_rate"]),
                advancers=row["advancers"],
                decliners=row["decliners"],
                source=str(row["source"]),
                retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            )
            for row in rows
        ]

    def add_thesis(self, thesis: InvestmentThesis) -> int:
        thesis.validate()
        with self.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO investment_theses(
                    fund_code, rationale, horizon, invalidation, created_at, active
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    thesis.fund_code,
                    thesis.rationale,
                    thesis.horizon,
                    thesis.invalidation,
                    thesis.created_at.isoformat(),
                    1 if thesis.active else 0,
                ),
            )
            return int(cursor.lastrowid)

    def list_theses(self, fund_code: Optional[str] = None) -> List[InvestmentThesis]:
        query = "SELECT * FROM investment_theses"
        parameters: Tuple[str, ...] = ()
        if fund_code is not None:
            query += " WHERE fund_code = ?"
            parameters = (fund_code,)
        query += " ORDER BY created_at DESC, id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            InvestmentThesis(
                fund_code=str(row["fund_code"]),
                rationale=str(row["rationale"]),
                horizon=str(row["horizon"]),
                invalidation=str(row["invalidation"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                active=bool(row["active"]),
            )
            for row in rows
        ]
