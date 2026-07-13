from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence, Tuple

from kunjin.adapters.yangjibao import YangjibaoClient, YangjibaoError
from kunjin.models import AccountObservation, PositionObservation, SyncResult
from kunjin.storage.repository import Repository


class SyncError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_SENSITIVE_KEYS = {"authorization", "token", "sign", "secret", "request-sign"}


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_KEYS:
                redacted[str(key)] = "[REDACTED]"
            elif lowered in {"url", "qr_url"} and isinstance(item, str):
                redacted[str(key)] = "[REDACTED_QR_CONTENT]"
            else:
                redacted[str(key)] = redact_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    return value


def snapshot_record(
    endpoint: str, payload: Any, retrieved_at: datetime
) -> Tuple[str, str, str, datetime]:
    serialized = json.dumps(
        redact_payload(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    checksum = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return endpoint, serialized, checksum, retrieved_at


class PortfolioSyncService:
    def __init__(self, client: YangjibaoClient, repository: Repository) -> None:
        self.client = client
        self.repository = repository

    def sync_portfolio(self, trigger: str = "manual") -> SyncResult:
        sync_run_id = self.repository.begin_sync("yangjibao", trigger)
        retrieved_at = datetime.now(timezone.utc)
        raw_snapshots: List[Tuple[str, str, str, datetime]] = []
        normalized: List[Tuple[AccountObservation, Sequence[PositionObservation]]] = []
        try:
            accounts_payload, accounts = self.client.list_accounts()
            raw_snapshots.append(snapshot_record("/user_account", accounts_payload, retrieved_at))
            total_positions = 0
            for account in accounts:
                holdings_payload, positions = self.client.list_holdings(
                    account.source_account_id,
                    observed_at=account.observed_at,
                )
                raw_snapshots.append(
                    snapshot_record(
                        f"/fund_hold?account_id={account.source_account_id}",
                        holdings_payload,
                        retrieved_at,
                    )
                )
                normalized.append((account, positions))
                total_positions += len(positions)
            self.repository.commit_sync(sync_run_id, raw_snapshots, normalized)
            return SyncResult(sync_run_id, len(accounts), total_positions, retrieved_at)
        except Exception as exc:
            code = getattr(exc, "code", "sync_error")
            self.repository.fail_sync(sync_run_id, str(code), str(exc))
            if isinstance(exc, SyncError):
                raise
            if isinstance(exc, YangjibaoError):
                raise SyncError(exc.code, str(exc)) from exc
            raise SyncError(str(code), str(exc)) from exc
