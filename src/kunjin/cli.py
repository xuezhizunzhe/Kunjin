from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kunjin import __version__
from kunjin.adapters.yangjibao import YangjibaoClient, YangjibaoError
from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.logging import redact_secrets
from kunjin.paths import RuntimePaths
from kunjin.security.keychain import CredentialStoreError, KeychainTokenStore
from kunjin.services.sync import PortfolioSyncService, SyncError
from kunjin.storage.repository import Repository


@dataclass
class ApplicationContext:
    paths: RuntimePaths
    repository: Repository
    token_store: KeychainTokenStore
    client: YangjibaoClient
    sync_service: PortfolioSyncService


def build_context() -> ApplicationContext:
    paths = RuntimePaths.from_environment().ensure()
    repository = Repository(paths.database)
    repository.migrate()
    token_store = KeychainTokenStore()
    client = YangjibaoClient(token_store)
    return ApplicationContext(
        paths=paths,
        repository=repository,
        token_store=token_store,
        client=client,
        sync_service=PortfolioSyncService(client, repository),
    )


def envelope(
    command: str,
    data: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[str]] = None,
    errors: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "1",
        "command": command,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
    }


def serialize(value: Any) -> Any:
    if is_dataclass(value):
        return serialize(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def freshness(finished_at: Optional[str], now: Optional[datetime] = None) -> str:
    if not finished_at:
        return "missing"
    shanghai = ZoneInfo("Asia/Shanghai")
    current = (now or datetime.now(timezone.utc)).astimezone(shanghai)
    synced = datetime.fromisoformat(finished_at).astimezone(shanghai)
    deadline_date = synced.date() + timedelta(days=1)
    while deadline_date.weekday() >= 5:
        deadline_date += timedelta(days=1)
    deadline = datetime.combine(deadline_date, time(16, 30), tzinfo=shanghai)
    return "fresh" if current <= deadline else "stale"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kunjin", description="KunJin fund research CLI")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version")
    subparsers.add_parser("status")

    auth = subparsers.add_parser("auth")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_subparsers.add_parser("status")
    login = auth_subparsers.add_parser("login")
    login.add_argument("provider", choices=["yangjibao"])
    revoke = auth_subparsers.add_parser("revoke")
    revoke.add_argument("provider", choices=["yangjibao"])

    sync = subparsers.add_parser("sync")
    sync_subparsers = sync.add_subparsers(dest="sync_command", required=True)
    sync_subparsers.add_parser("portfolio")

    portfolio = subparsers.add_parser("portfolio")
    portfolio_subparsers = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_subparsers.add_parser("show")
    portfolio_subparsers.add_parser("analyze")
    return parser


def _positions_payload(context: ApplicationContext) -> List[Dict[str, Any]]:
    return [serialize(position) for position in context.repository.latest_positions()]


def execute(args: argparse.Namespace, context: ApplicationContext) -> Dict[str, Any]:
    if args.command == "version":
        return envelope("version", {"version": __version__})

    if args.command == "auth" and args.auth_command == "status":
        return envelope("auth.status", {"yangjibao_authorized": context.token_store.load() is not None})

    if args.command == "auth" and args.auth_command == "revoke":
        context.token_store.delete()
        return envelope("auth.revoke", {"provider": args.provider, "revoked": True})

    if args.command == "auth" and args.auth_command == "login":
        if args.json_output:
            return envelope(
                "auth.login",
                errors=[
                    {
                        "code": "interactive_required",
                        "message": "Run QR authorization without --json",
                    }
                ],
            )
        challenge = context.client.start_qr_login()
        rendered = context.client.render_qr(challenge.qr_content)
        if not rendered:
            print("QR renderer is unavailable. Install the optional 'qr' extra.", file=sys.stderr)
            print(f"First-party QR content: {challenge.qr_content}", file=sys.stderr)
        context.client.poll_qr_login(challenge.challenge_id)
        return envelope("auth.login", {"provider": args.provider, "authorized": True})

    latest_sync = context.repository.latest_successful_sync("yangjibao")
    data_freshness = freshness(None if latest_sync is None else latest_sync.get("finished_at"))

    if args.command == "sync" and args.sync_command == "portfolio":
        result = context.sync_service.sync_portfolio(trigger="manual")
        return envelope("sync.portfolio", serialize(result))

    if args.command == "status":
        return envelope(
            "status",
            {
                "version": __version__,
                "yangjibao_authorized": context.token_store.load() is not None,
                "portfolio_freshness": data_freshness,
                "latest_successful_sync": latest_sync,
            },
            warnings=["freshness uses weekday boundaries and does not yet include exchange holidays"],
        )

    if args.command == "portfolio" and args.portfolio_command == "show":
        positions = _positions_payload(context)
        warnings = [] if positions else ["no synchronized portfolio is available"]
        return envelope(
            "portfolio.show",
            {"freshness": data_freshness, "positions": positions},
            warnings=warnings,
        )

    if args.command == "portfolio" and args.portfolio_command == "analyze":
        analysis = analyze_portfolio(context.repository.latest_positions())
        return envelope(
            "portfolio.analyze",
            {"freshness": data_freshness, "analysis": serialize(analysis)},
            warnings=list(analysis.warnings),
        )

    return envelope(
        str(args.command),
        errors=[{"code": "unknown_command", "message": "Unknown command"}],
    )


def run(
    argv: Optional[Sequence[str]] = None,
    context: Optional[ApplicationContext] = None,
) -> Tuple[Dict[str, Any], int, bool]:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "version":
            payload = envelope("version", {"version": __version__})
        else:
            payload = execute(args, context or build_context())
        exit_code = 1 if payload["errors"] else 0
    except (CredentialStoreError, YangjibaoError, SyncError, ValueError) as exc:
        code = getattr(exc, "code", "operation_failed")
        payload = envelope(
            str(args.command),
            errors=[{"code": str(code), "message": redact_secrets(str(exc))}],
        )
        exit_code = 1
    return serialize(payload), exit_code, args.json_output


def main(argv: Optional[Sequence[str]] = None) -> int:
    payload, exit_code, json_output = run(argv)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
