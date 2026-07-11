import json
import unittest
from pathlib import Path

from kunjin.adapters.eastmoney import EastmoneyFundClient, EastmoneyMarketClient, PublicDataError


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload

    def request_json(self, url, referer):
        self.url = url
        self.referer = referer
        return self.payload


def fixture(name):
    path = Path(__file__).parents[1] / "fixtures" / "eastmoney" / name
    return json.loads(path.read_text())


class EastmoneyTest(unittest.TestCase):
    def test_fund_nav_is_normalized(self) -> None:
        client = EastmoneyFundClient(FakeHttp(fixture("fund_nav.json")))

        _, name, fund_type, history = client.fetch_nav_history("017811")

        self.assertEqual(name, "测试人工智能基金C")
        self.assertEqual(fund_type, "混合型")
        self.assertEqual(str(history[0].unit_nav), "1.20")

    def test_invalid_fund_code_is_rejected(self) -> None:
        client = EastmoneyFundClient(FakeHttp({}))
        with self.assertRaises(ValueError):
            client.fetch_nav_history("bad")

    def test_sector_ranking_is_normalized(self) -> None:
        client = EastmoneyMarketClient(FakeHttp(fixture("sectors.json")))

        _, sectors = client.fetch_sectors("industry")

        self.assertEqual(sectors[0].sector_name, "半导体")
        self.assertEqual(str(sectors[0].pct_change), "2.5")


if __name__ == "__main__":
    unittest.main()

