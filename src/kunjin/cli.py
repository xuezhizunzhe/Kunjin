from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kunjin import __version__
from kunjin.adapters.eastmoney import (
    EastmoneyFundClient,
    EastmoneyMarketClient,
    PublicDataError,
)
from kunjin.adapters.yangjibao import YangjibaoClient, YangjibaoError
from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.analytics.research import analyze_fund_history, analyze_sectors
from kunjin.logging import redact_secrets
from kunjin.ledger.alipay import AlipayPaymentParser, requires_confirmation
from kunjin.ledger.ocr import OcrError, VisionOcrClient
from kunjin.ledger.reconcile import reconcile_fund
from kunjin.ledger.service import LedgerImportError, LedgerService
from kunjin.ledger.store import LedgerStateError, LedgerStore
from kunjin.models import InvestmentThesis
from kunjin.paths import RuntimePaths
from kunjin.security.keychain import CredentialStoreError, KeychainTokenStore
from kunjin.services.research import ResearchSyncService
from kunjin.services.sync import PortfolioSyncService, SyncError
from kunjin.storage.repository import Repository


_FUND_CODE = re.compile(r"^\d{6}$")
_COMMAND_PART = re.compile(r"^[a-z][a-z0-9_-]*$")
_TOP_LEVEL_COMMANDS = {
    "auth",
    "fund",
    "ledger",
    "market",
    "portfolio",
    "report",
    "status",
    "sync",
    "thesis",
    "version",
}


class CliUsageError(ValueError):
    code = "invalid_arguments"


class KunjinArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


@dataclass
class ApplicationContext:
    paths: RuntimePaths
    repository: Repository
    token_store: KeychainTokenStore
    client: YangjibaoClient
    sync_service: PortfolioSyncService
    research_service: ResearchSyncService
    ledger_service: LedgerService


def build_context() -> ApplicationContext:
    paths = RuntimePaths.from_environment().ensure()
    repository = Repository(paths.database)
    repository.migrate()
    token_store = KeychainTokenStore()
    client = YangjibaoClient(token_store)
    research_service = ResearchSyncService(
        repository,
        EastmoneyFundClient(),
        EastmoneyMarketClient(),
    )
    ledger_store = LedgerStore(repository)
    return ApplicationContext(
        paths=paths,
        repository=repository,
        token_store=token_store,
        client=client,
        sync_service=PortfolioSyncService(client, repository),
        research_service=research_service,
        ledger_service=LedgerService(
            paths=paths,
            store=ledger_store,
            ocr_client=VisionOcrClient(),
            parser=AlipayPaymentParser(),
        ),
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


def parse_field_overrides(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        name, separator, field_value = value.partition("=")
        if not separator or not name.strip():
            raise ValueError("field overrides must use NAME=VALUE")
        result[name.strip()] = field_value.strip()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = KunjinArgumentParser(
        prog="kunjin", description="KunJin fund research CLI"
    )
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
    sync_fund = sync_subparsers.add_parser("fund")
    sync_fund.add_argument("fund_code")
    sync_subparsers.add_parser("market")
    sync_subparsers.add_parser("daily")

    portfolio = subparsers.add_parser("portfolio")
    portfolio_subparsers = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_subparsers.add_parser("show")
    portfolio_subparsers.add_parser("analyze")

    fund = subparsers.add_parser("fund")
    fund_subparsers = fund.add_subparsers(dest="fund_command", required=True)
    fund_research = fund_subparsers.add_parser("research")
    fund_research.add_argument("fund_code")

    market = subparsers.add_parser("market")
    market_subparsers = market.add_subparsers(dest="market_command", required=True)
    market_subparsers.add_parser("sectors")

    thesis = subparsers.add_parser("thesis")
    thesis_subparsers = thesis.add_subparsers(dest="thesis_command", required=True)
    thesis_add = thesis_subparsers.add_parser("add")
    thesis_add.add_argument("fund_code")
    thesis_add.add_argument("--reason", required=True)
    thesis_add.add_argument("--horizon", required=True)
    thesis_add.add_argument("--invalidation", required=True)
    thesis_list = thesis_subparsers.add_parser("list")
    thesis_list.add_argument("--fund-code")
    thesis_review = thesis_subparsers.add_parser("review")
    thesis_review.add_argument("fund_code")

    report = subparsers.add_parser("report")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    report_subparsers.add_parser("weekly")

    ledger = subparsers.add_parser("ledger")
    ledger_subparsers = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_import = ledger_subparsers.add_parser("import")
    ledger_import.add_argument("image")
    ledger_import.add_argument("--fund-code")
    ledger_subparsers.add_parser("drafts")
    ledger_confirm = ledger_subparsers.add_parser("confirm")
    ledger_confirm.add_argument("draft_id", type=int)
    ledger_confirm.add_argument("--field", action="append", default=[])
    ledger_add = ledger_subparsers.add_parser("add")
    ledger_add.add_argument("--type", required=True, dest="transaction_type")
    ledger_add.add_argument("--fund-code", required=True)
    ledger_add.add_argument("--fund-name")
    ledger_add.add_argument("--amount")
    ledger_add.add_argument("--shares")
    ledger_add.add_argument("--nav")
    ledger_add.add_argument("--fee")
    ledger_add.add_argument("--order-time")
    ledger_add.add_argument("--confirmation-time")
    ledger_transactions = ledger_subparsers.add_parser("transactions")
    ledger_transactions.add_argument("--fund-code")
    ledger_reconcile = ledger_subparsers.add_parser("reconcile")
    ledger_reconcile.add_argument("--fund-code", required=True)
    ledger_document = ledger_subparsers.add_parser("document")
    document_subparsers = ledger_document.add_subparsers(
        dest="document_command", required=True
    )
    document_delete = document_subparsers.add_parser("delete")
    document_delete.add_argument("document_id", type=int)
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

    if args.command == "ledger" and args.ledger_command == "import":
        draft = context.ledger_service.import_image(
            args.image, fund_code_hint=args.fund_code
        )
        fields = context.ledger_service.store.list_ocr_fields(
            draft.source_document_id
        )
        fields_by_name = {field.name: field for field in fields}
        public_fields = {
            field.name: {
                "normalized_value": field.normalized_value,
                "confidence": str(field.confidence),
                "evidence_level": field.evidence_level.value,
            }
            for field in fields
        }
        return envelope(
            "ledger.import",
            {
                "document_id": draft.source_document_id,
                "draft": serialize(draft),
                "requires_confirmation": requires_confirmation(fields_by_name),
                "fields": public_fields,
            },
        )

    if args.command == "ledger" and args.ledger_command == "drafts":
        drafts = context.ledger_service.store.list_drafts()
        return envelope("ledger.drafts", {"drafts": serialize(drafts)})

    if args.command == "ledger" and args.ledger_command == "confirm":
        transaction = context.ledger_service.confirm_draft(
            args.draft_id, parse_field_overrides(args.field)
        )
        return envelope(
            "ledger.confirm", {"transaction": serialize(transaction)}
        )

    if args.command == "ledger" and args.ledger_command == "add":
        transaction = context.ledger_service.add_manual_transaction(
            transaction_type=args.transaction_type,
            fund_code=args.fund_code,
            fund_name=args.fund_name,
            amount=args.amount,
            shares=args.shares,
            nav=args.nav,
            fee=args.fee,
            order_time=args.order_time,
            confirmation_time=args.confirmation_time,
        )
        return envelope("ledger.add", {"transaction": serialize(transaction)})

    if args.command == "ledger" and args.ledger_command == "transactions":
        transactions = context.ledger_service.store.list_transactions(
            args.fund_code
        )
        return envelope(
            "ledger.transactions", {"transactions": serialize(transactions)}
        )

    if args.command == "ledger" and args.ledger_command == "reconcile":
        if not _FUND_CODE.fullmatch(args.fund_code):
            raise LedgerImportError(
                "invalid_fund_code", "fund code must contain six digits"
            )
        positions = [
            position
            for position in context.repository.latest_positions()
            if position.fund_code == args.fund_code
        ]
        if not positions:
            return envelope(
                "ledger.reconcile",
                errors=[
                    {
                        "code": "position_not_found",
                        "message": "No synchronized position is available for this fund",
                    }
                ],
            )
        account_titles = sorted({position.account_title for position in positions})
        if len(account_titles) > 1:
            return envelope(
                "ledger.reconcile",
                {"account_titles": account_titles},
                errors=[
                    {
                        "code": "ambiguous_position_accounts",
                        "message": "Multiple accounts hold this fund; account selection is required",
                    }
                ],
            )
        position = max(
            positions,
            key=lambda item: (item.observed_at, item.account_title),
        )
        result = reconcile_fund(
            position,
            context.ledger_service.store.list_transactions(args.fund_code),
            context.ledger_service.store.list_drafts(),
        )
        return envelope(
            "ledger.reconcile",
            {"result": serialize(result)},
            warnings=list(result.warnings),
        )

    if (
        args.command == "ledger"
        and args.ledger_command == "document"
        and args.document_command == "delete"
    ):
        deleted = context.ledger_service.delete_document(args.document_id)
        warnings = [] if deleted else ["document is not active or does not exist"]
        return envelope(
            "ledger.document.delete",
            {"document_id": args.document_id, "deleted": deleted},
            warnings=warnings,
        )

    latest_sync = context.repository.latest_successful_sync("yangjibao")
    data_freshness = freshness(None if latest_sync is None else latest_sync.get("finished_at"))

    if args.command == "sync" and args.sync_command == "portfolio":
        result = context.sync_service.sync_portfolio(trigger="manual")
        return envelope("sync.portfolio", serialize(result))

    if args.command == "sync" and args.sync_command == "fund":
        result = context.research_service.sync_fund(args.fund_code)
        return envelope("sync.fund", serialize(result))

    if args.command == "sync" and args.sync_command == "market":
        result = context.research_service.sync_market()
        return envelope("sync.market", serialize(result))

    if args.command == "sync" and args.sync_command == "daily":
        results: Dict[str, Any] = {}
        errors: List[Dict[str, str]] = []
        try:
            results["portfolio"] = serialize(
                context.sync_service.sync_portfolio(trigger="scheduled")
            )
        except Exception as exc:
            errors.append(
                {
                    "code": str(getattr(exc, "code", "portfolio_sync_failed")),
                    "message": redact_secrets(str(exc)),
                }
            )
        fund_results = {}
        for fund_code in sorted(
            {position.fund_code for position in context.repository.latest_positions()}
        ):
            try:
                fund_results[fund_code] = serialize(
                    context.research_service.sync_fund(fund_code)
                )
            except Exception as exc:
                errors.append(
                    {
                        "code": str(getattr(exc, "code", "fund_sync_failed")),
                        "message": f"{fund_code}: {redact_secrets(str(exc))}",
                    }
                )
        results["funds"] = fund_results
        try:
            results["market"] = serialize(context.research_service.sync_market())
        except Exception as exc:
            errors.append(
                {
                    "code": str(getattr(exc, "code", "market_sync_failed")),
                    "message": redact_secrets(str(exc)),
                }
            )
        return envelope("sync.daily", results, errors=errors)

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

    if args.command == "fund" and args.fund_command == "research":
        history = context.repository.fund_history(args.fund_code)
        analysis = analyze_fund_history(history)
        profile = context.repository.fund_profile(args.fund_code)
        return envelope(
            "fund.research",
            {"profile": profile, "analysis": analysis},
            warnings=list(analysis.get("warnings", [])),
        )

    if args.command == "market" and args.market_command == "sectors":
        analysis = analyze_sectors(context.repository.latest_sector_snapshots())
        return envelope(
            "market.sectors",
            analysis,
            warnings=list(analysis.get("warnings", [])),
        )

    if args.command == "thesis" and args.thesis_command == "add":
        thesis = InvestmentThesis(
            fund_code=args.fund_code,
            rationale=args.reason,
            horizon=args.horizon,
            invalidation=args.invalidation,
            created_at=datetime.now(timezone.utc),
        )
        thesis_id = context.repository.add_thesis(thesis)
        return envelope("thesis.add", {"id": thesis_id, "thesis": serialize(thesis)})

    if args.command == "thesis" and args.thesis_command == "list":
        theses = context.repository.list_theses(args.fund_code)
        return envelope("thesis.list", {"theses": serialize(theses)})

    if args.command == "thesis" and args.thesis_command == "review":
        theses = context.repository.list_theses(args.fund_code)
        research = analyze_fund_history(context.repository.fund_history(args.fund_code))
        warnings = list(research.get("warnings", []))
        if not theses:
            warnings.append("no recorded thesis exists for this fund")
        return envelope(
            "thesis.review",
            {"theses": serialize(theses), "fund_research": research},
            warnings=warnings,
        )

    if args.command == "report" and args.report_command == "weekly":
        positions = context.repository.latest_positions()
        portfolio = analyze_portfolio(positions)
        funds = {
            fund_code: analyze_fund_history(context.repository.fund_history(fund_code))
            for fund_code in sorted({position.fund_code for position in positions})
        }
        sectors = analyze_sectors(context.repository.latest_sector_snapshots())
        warnings = list(portfolio.warnings) + list(sectors.get("warnings", []))
        warnings.append(
            "news and causal attribution are not persisted yet; verify relevant events from official sources"
        )
        return envelope(
            "report.weekly",
            {
                "portfolio": serialize(portfolio),
                "funds": funds,
                "sectors": sectors,
                "learning_questions": [
                    "Did the original thesis still hold this week?",
                    "Was the result driven by the intended mechanism or broad market movement?",
                    "What evidence would invalidate the position next week?",
                ],
            },
            warnings=warnings,
        )

    return envelope(
        str(args.command),
        errors=[{"code": "unknown_command", "message": "Unknown command"}],
    )


def run(
    argv: Optional[Sequence[str]] = None,
    context: Optional[ApplicationContext] = None,
) -> Tuple[Dict[str, Any], int, bool]:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args: Optional[argparse.Namespace] = None
    json_output = "--json" in raw_argv
    try:
        args = build_parser().parse_args(raw_argv)
        json_output = args.json_output
        if args.command == "version":
            payload = envelope("version", {"version": __version__})
        else:
            payload = execute(args, context or build_context())
        exit_code = 1 if payload["errors"] else 0
    except (
        CredentialStoreError,
        PublicDataError,
        YangjibaoError,
        SyncError,
        OcrError,
        LedgerImportError,
        LedgerStateError,
        CliUsageError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "operation_failed")
        payload = envelope(
            (
                _command_name(args)
                if args is not None
                else _command_name_from_argv(raw_argv)
            ),
            errors=[{"code": str(code), "message": redact_secrets(str(exc))}],
        )
        exit_code = 1
    return serialize(payload), exit_code, json_output


def _command_name(args: argparse.Namespace) -> str:
    if args.command != "ledger":
        return str(args.command)
    if args.ledger_command == "document":
        return f"ledger.document.{args.document_command}"
    return f"ledger.{args.ledger_command}"


def _command_name_from_argv(argv: Sequence[str]) -> str:
    values = [str(value) for value in argv]
    try:
        ledger_index = values.index("ledger")
    except ValueError:
        for value in values:
            if value in _TOP_LEVEL_COMMANDS:
                return value
        return "cli"

    action_index = ledger_index + 1
    if action_index >= len(values):
        return "ledger"
    action = values[action_index]
    if not _COMMAND_PART.fullmatch(action):
        return "ledger"
    if action != "document":
        return f"ledger.{action}"

    document_action_index = action_index + 1
    if document_action_index >= len(values):
        return "ledger.document"
    document_action = values[document_action_index]
    if not _COMMAND_PART.fullmatch(document_action):
        return "ledger.document"
    return f"ledger.document.{document_action}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    payload, exit_code, json_output = run(argv)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
