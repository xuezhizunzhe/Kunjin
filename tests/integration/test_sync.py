import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.models import AccountObservation, PositionObservation
from kunjin.services.sync import PortfolioSyncService, SyncError
from kunjin.storage.repository import Repository


class FakeClient:
    def __init__(self, fail=False) -> None:
        self.fail = fail
        self.now = datetime.now(timezone.utc)

    def list_accounts(self):
        if self.fail:
            raise RuntimeError("synthetic outage token=must-not-persist")
        account = AccountObservation("yangjibao", "account-1", "学习账户", self.now)
        return {"list": [{"id": "account-1", "token": "hidden"}]}, [account]

    def list_holdings(self, account_id, observed_at=None):
        positions = [
            PositionObservation(
                account_id,
                "016067",
                "新能源汽车混合A",
                Decimal("10"),
                observed_at or self.now,
                share_class="A",
                formal_nav=Decimal("1.1"),
                observed_profit=Decimal("0.1"),
            ),
            PositionObservation(
                account_id,
                "017811",
                "人工智能混合C",
                Decimal("8"),
                observed_at or self.now,
                share_class="C",
                formal_nav=Decimal("1.2"),
                observed_profit=Decimal("-0.2"),
            ),
        ]
        return [{"code": item.fund_code} for item in positions], positions


class SyncIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_sync_stores_redacted_raw_and_normalized_data(self) -> None:
        service = PortfolioSyncService(FakeClient(), self.repository)

        result = service.sync_portfolio()

        self.assertEqual(result.accounts, 1)
        self.assertEqual(result.positions, 2)
        self.assertEqual(len(self.repository.latest_positions()), 2)
        self.assertNotIn("hidden", self.repository.latest_raw_snapshot())

    def test_failed_refresh_preserves_previous_snapshot(self) -> None:
        service = PortfolioSyncService(FakeClient(), self.repository)
        service.sync_portfolio()
        before = self.repository.latest_positions()
        service.client = FakeClient(fail=True)

        with self.assertRaises(SyncError):
            service.sync_portfolio()

        self.assertEqual(self.repository.latest_positions(), before)


if __name__ == "__main__":
    unittest.main()

