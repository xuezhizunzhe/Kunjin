import http.client
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kunjin.adapters.eastmoney import (
    EastmoneyFundClient,
    EastmoneyMarketClient,
    HttpsJsonClient,
    PublicDataError,
)


class FakeHttp:
    def __init__(self, payload):
        self.payload = payload

    def request_json(self, url, referer):
        self.url = url
        self.referer = referer
        return self.payload


class PaginatedHttp:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.urls = []

    def request_json(self, url, referer):
        self.urls.append(url)
        return next(self.payloads)


def fixture(name):
    path = Path(__file__).parents[1] / "fixtures" / "eastmoney" / name
    return json.loads(path.read_text())


class EastmoneyTest(unittest.TestCase):
    def test_http_client_retries_remote_disconnect(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": {}}'
        with patch(
            "kunjin.adapters.eastmoney.urllib.request.urlopen",
            side_effect=[http.client.RemoteDisconnected(), response],
        ) as urlopen:
            payload = HttpsJsonClient(retries=1).request_json(
                "https://example.com/data", "https://example.com/"
            )

        self.assertEqual(payload, {"data": {}})
        self.assertEqual(urlopen.call_count, 2)

    def test_fund_nav_is_normalized(self) -> None:
        http = FakeHttp(fixture("fund_nav.json"))
        client = EastmoneyFundClient(http)

        _, name, fund_type, history = client.fetch_nav_history("017811")

        self.assertIn("pageSize=20", http.url)
        self.assertEqual(name, "测试人工智能基金C")
        self.assertEqual(fund_type, "混合型")
        self.assertEqual(str(history[0].unit_nav), "1.20")

    def test_fund_nav_reads_additional_pages(self) -> None:
        first_page = fixture("fund_nav.json")
        first_page["Data"]["LSJZList"] = first_page["Data"]["LSJZList"] * 5
        first_page["TotalCount"] = 21
        second_page = fixture("fund_nav.json")
        second_page["Data"]["LSJZList"] = second_page["Data"]["LSJZList"][:1]
        second_page["TotalCount"] = 21
        http = PaginatedHttp([first_page, second_page])

        _, _, _, history = EastmoneyFundClient(http).fetch_nav_history("017811")

        self.assertEqual(len(history), 21)
        self.assertIn("pageIndex=1", http.urls[0])
        self.assertIn("pageIndex=2", http.urls[1])

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
