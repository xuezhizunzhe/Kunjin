import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.adapters.yangjibao import YangjibaoClient
from kunjin.adapters.eastmoney import EastmoneyFundClient, EastmoneyMarketClient
from kunjin.cli import ApplicationContext, run
from kunjin.models import AccountObservation, FundNavObservation, PositionObservation, SectorObservation
from kunjin.paths import RuntimePaths
from kunjin.services.sync import PortfolioSyncService
from kunjin.services.research import ResearchSyncService
from kunjin.models import SyncResult
from kunjin.services.research import ResearchSyncResult
from kunjin.storage.repository import Repository


class FakeTokenStore:
    def __init__(self, token="never-print-this") -> None:
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = token

    def delete(self):
        self.token = None


class CliIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
        repository = Repository(paths.database)
        repository.migrate()
        now = datetime.now(timezone.utc)
        repository.replace_snapshot(
            AccountObservation("yangjibao", "account-1", "学习账户", now),
            [
                PositionObservation(
                    "account-1",
                    "016067",
                    "新能源混合A",
                    Decimal("10"),
                    now,
                    share_class="A",
                    formal_nav=Decimal("1.1"),
                    observed_profit=Decimal("0.1"),
                )
            ],
        )
        token_store = FakeTokenStore()
        client = YangjibaoClient(token_store)
        repository.save_fund_history(
            "017811",
            "人工智能混合C",
            "混合型",
            "eastmoney",
            [
                FundNavObservation("017811", date(2026, 6, 1), Decimal("1.0"), None, None, "eastmoney", now),
                FundNavObservation("017811", date(2026, 7, 1), Decimal("1.1"), None, None, "eastmoney", now),
            ],
        )
        repository.save_sector_snapshots(
            [SectorObservation("BK1", "半导体", "industry", Decimal("2"), Decimal("3"), 8, 2, "eastmoney", now)]
        )
        self.context = ApplicationContext(
            paths,
            repository,
            token_store,
            client,
            PortfolioSyncService(client, repository),
            ResearchSyncService(repository, EastmoneyFundClient(), EastmoneyMarketClient()),
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_status_json_contract(self) -> None:
        payload, exit_code, json_output = run(["--json", "status"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(
            set(payload),
            {"schema_version", "command", "as_of", "data", "warnings", "errors"},
        )

    def test_portfolio_output_never_contains_token(self) -> None:
        payload, exit_code, _ = run(["--json", "portfolio", "show"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertNotIn("never-print-this", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["data"]["positions"][0]["fund_code"], "016067")

    def test_analysis_is_structured(self) -> None:
        payload, exit_code, _ = run(["--json", "portfolio", "analyze"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["analysis"]["evidence_level"], "deterministic_calculation")

    def test_fund_research_is_structured(self) -> None:
        payload, exit_code, _ = run(["--json", "fund", "research", "017811"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["profile"]["fund_code"], "017811")
        self.assertEqual(payload["data"]["analysis"]["evidence_level"], "deterministic_calculation")

    def test_market_sectors_state_scope(self) -> None:
        payload, exit_code, _ = run(["--json", "market", "sectors"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["scope"], "recent_strength_and_breadth_only")

    def test_thesis_add_and_review(self) -> None:
        payload, exit_code, _ = run(
            [
                "--json",
                "thesis",
                "add",
                "017811",
                "--reason",
                "AI盈利改善",
                "--horizon",
                "12个月",
                "--invalidation",
                "持续落后且风格漂移",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["thesis"]["fund_code"], "017811")

        review, exit_code, _ = run(["--json", "thesis", "review", "017811"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(review["data"]["theses"]), 1)

    def test_weekly_report_preserves_missing_evidence_warning(self) -> None:
        payload, exit_code, _ = run(["--json", "report", "weekly"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertTrue(any("news" in warning for warning in payload["warnings"]))

    def test_daily_sync_combines_sources(self) -> None:
        class PortfolioService:
            def sync_portfolio(inner_self, trigger):
                return SyncResult(1, 1, 1, datetime.now(timezone.utc))

        class ResearchService:
            def sync_fund(inner_self, fund_code):
                return ResearchSyncResult(fund_code, 10)

            def sync_market(inner_self):
                return ResearchSyncResult("market_sectors", 2)

        self.context.sync_service = PortfolioService()
        self.context.research_service = ResearchService()
        payload, exit_code, _ = run(["--json", "sync", "daily"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertIn("016067", payload["data"]["funds"])
        self.assertEqual(payload["data"]["market"]["observations"], 2)


if __name__ == "__main__":
    unittest.main()
