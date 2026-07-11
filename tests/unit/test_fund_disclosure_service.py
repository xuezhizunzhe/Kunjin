from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kunjin.funds.models import DocumentKind
from kunjin.funds.service import (
    FundDisclosureService,
    announcement_report_period,
    expected_report_period,
)
from kunjin.funds.sources import FundSourceError, TextResponse
from kunjin.funds.store import FundDisclosureStore
from kunjin.storage.repository import Repository


FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeTextClient:
    def __init__(self, failures: set[DocumentKind] | None = None) -> None:
        self.failures = failures or set()
        self.overrides: dict[DocumentKind, str] = {}
        self.retrieved_at = datetime(2026, 7, 11, 9, tzinfo=SHANGHAI)
        self.requested_urls: list[str] = []

    def fetch(self, url: str, referer: str) -> TextResponse:
        del referer
        self.requested_urls.append(url)
        parsed_url = urlparse(url)
        query = parsed_url.query
        filename = Path(urlparse(url).path).name
        if "type=gmbd" in query:
            kind = DocumentKind.SIZE_HISTORY
        elif "type=jjcc" in query:
            kind = DocumentKind.QUARTERLY_HOLDINGS
        elif filename == "HYPZ":
            kind = DocumentKind.INDUSTRY_EXPOSURE
        elif filename == "JJGG":
            kind = DocumentKind.ANNOUNCEMENT
        else:
            kind = next(
                kind
                for kind, prefix in {
                DocumentKind.BASIC_PROFILE: "jbgk_",
                DocumentKind.MANAGER_HISTORY: "jjjl_",
                DocumentKind.FEE_SCHEDULE: "jjfl_",
                }.items()
                if filename.startswith(prefix)
            )
        if kind in self.failures:
            raise FundSourceError(f"synthetic failure for {kind.value}")
        fixture_name = {
            DocumentKind.BASIC_PROFILE: "basic_profile.html",
            DocumentKind.MANAGER_HISTORY: "manager_history.html",
            DocumentKind.FEE_SCHEDULE: "fee_schedule.html",
            DocumentKind.SIZE_HISTORY: "size_history.html",
            DocumentKind.QUARTERLY_HOLDINGS: "quarterly_holdings.html",
            DocumentKind.INDUSTRY_EXPOSURE: "industry_exposure.html",
            DocumentKind.ANNOUNCEMENT: "announcements.html",
        }[kind]
        text = self.overrides.get(kind, (FIXTURES / fixture_name).read_text("utf-8"))
        return TextResponse(
            requested_url=url,
            final_url=url,
            text=text,
            retrieved_at=self.retrieved_at,
            checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_type="text/html; charset=utf-8",
        )


class FundDisclosureServiceTest(unittest.TestCase):
    def test_real_dynamic_contract_uses_announcements_to_fill_publication_date(self) -> None:
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = json.dumps(
            {"Data": [{
                "TITLE": "交银多策略回报灵活配置混合型证券投资基金2026年第2季度报告",
                "NEWCATEGORY": "3", "PUBLISHDATEDesc": "2026-07-20",
                "ATTACHTYPE": ".pdf", "ID": "AN202607200001",
            }]}, ensure_ascii=False,
        )
        content = """<h4>2026年2季度股票投资明细</h4><table>
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr>
          <tr><td>1</td><td>000001</td><td>平安银行</td><td>6.25%</td></tr></table>"""
        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = (
            "var apidata=" + json.dumps({"content": content}, ensure_ascii=False) + ";"
        )

        result = self.service.sync_all("519755")

        self.assertEqual(result.sections["announcements"].status, "success")
        self.assertEqual(result.sections["quarterly_holdings"].status, "success")
        holding = self.store.load_bundle("519755").holdings[0]
        self.assertEqual(holding.published_at, datetime(2026, 7, 20, tzinfo=SHANGHAI))
        self.assertTrue(any("FundArchivesDatas.aspx?type=jjcc" in url for url in self.client.requested_urls))
        self.assertTrue(any("api.fund.eastmoney.com/f10/JJGG" in url for url in self.client.requested_urls))

    def test_dynamic_holdings_without_matching_announcement_fail_explicitly(self) -> None:
        content = """<h4>2025年4季度股票投资明细</h4><table>
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr>
          <tr><td>1</td><td>000001</td><td>平安银行</td><td>6.25%</td></tr></table>"""
        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = (
            "var apidata=" + json.dumps({"content": content}, ensure_ascii=False) + ";"
        )

        result = self.service.sync_holdings("519755")

        self.assertEqual(result.sections["quarterly_holdings"].status, "source_unavailable")
        self.assertEqual(result.sections["quarterly_holdings"].error_code, "missing_publication_date")

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        repository.migrate()
        self.store = FundDisclosureStore(repository)
        self.client = FakeTextClient()
        self.as_of = datetime(2026, 7, 11, 12, tzinfo=SHANGHAI)
        self.service = FundDisclosureService(
            self.client,
            self.store,
            now=lambda: self.as_of,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_profile_sync_isolates_failures_and_maps_announcement_result_key(self) -> None:
        self.client.failures.add(DocumentKind.SIZE_HISTORY)
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = "<p>暂无基金公告</p>"

        result = self.service.sync_profile("519755")

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertEqual(result.sections["size_history"].status, "source_unavailable")
        self.assertEqual(result.sections["announcements"].status, "not_disclosed")
        self.assertNotIn("announcement", result.sections)
        self.assertEqual(
            self.store.section_status("519755")[DocumentKind.ANNOUNCEMENT.value]["state"],
            "not_disclosed",
        )

    def test_classification_sync_fetches_only_basic_profile(self) -> None:
        result = self.service.sync_classification("519755")

        self.assertEqual(tuple(result.sections), ("basic_profile",))
        self.assertEqual(len(self.client.requested_urls), 1)
        self.assertIn("jbgk_519755.html", self.client.requested_urls[0])

    def test_sync_all_keeps_profile_successes_when_holdings_fetch_fails(self) -> None:
        self.client.failures.add(DocumentKind.QUARTERLY_HOLDINGS)
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = "<p>暂无基金公告</p>"

        result = self.service.sync_all("519755")

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertEqual(
            result.sections["quarterly_holdings"].status,
            "source_unavailable",
        )
        self.assertEqual(result.sections["announcements"].status, "not_disclosed")

    def test_failed_second_holdings_sync_retains_records_and_last_success(self) -> None:
        first = self.service.sync_holdings("519755")
        first_success = first.sections["quarterly_holdings"].last_success_at
        self.assertEqual(first.sections["quarterly_holdings"].records, 3)

        self.client.retrieved_at = datetime(2026, 7, 12, 9, tzinfo=SHANGHAI)
        self.client.failures.add(DocumentKind.QUARTERLY_HOLDINGS)
        self.as_of = datetime(2026, 7, 12, 12, tzinfo=SHANGHAI)
        second = self.service.sync_holdings("519755")

        section = second.sections["quarterly_holdings"]
        self.assertEqual(section.status, "source_unavailable")
        self.assertEqual(section.records, 3)
        self.assertEqual(section.last_success_at, first_success)
        self.assertEqual(
            section.last_attempt_at,
            datetime(2026, 7, 12, 12, tzinfo=SHANGHAI).isoformat(),
        )
        self.assertEqual(len(self.store.load_bundle("519755").holdings), 3)

    def test_identity_conflict_only_blocks_basic_profile_and_is_reported(self) -> None:
        profile = (FIXTURES / "basic_profile.html").read_text("utf-8")
        self.client.overrides[DocumentKind.BASIC_PROFILE] = profile.replace(
            "519755", "519754"
        )

        result = self.service.sync_profile("519755")

        self.assertEqual(result.sections["basic_profile"].status, "source_unavailable")
        self.assertIn("basic_profile:identity_conflict", result.conflicts)
        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertIsNone(self.store.load_bundle("519755").identity)

    def test_freshness_has_only_supported_values(self) -> None:
        missing = self.service.section_snapshot("519755", "manager_history")
        self.assertEqual(missing.freshness, "missing")

        result = self.service.sync_all("519755")
        self.assertEqual(result.sections["basic_profile"].freshness, "fresh")
        self.assertEqual(result.sections["quarterly_holdings"].freshness, "fresh")
        self.assertTrue(
            {item.freshness for item in result.sections.values()}
            <= {"fresh", "stale", "missing", "unknown"}
        )

        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = "<p>暂无持仓数据</p>"
        empty = self.service.sync_holdings("519755")
        self.assertEqual(empty.sections["quarterly_holdings"].freshness, "unknown")

    def test_quarterly_freshness_uses_expected_report_period(self) -> None:
        self.service.sync_holdings("519755")

        self.as_of = datetime(2026, 8, 6, 12, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "fresh",
        )
        self.as_of = datetime(2026, 8, 7, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "fresh",
        )

        self.as_of = datetime(2026, 11, 7, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "stale",
        )


class ExpectedReportPeriodTest(unittest.TestCase):
    def test_announcement_titles_map_quarter_four_and_annual_to_same_period(self) -> None:
        expected = date(2025, 12, 31)
        titles = (
            "示例基金2025年第4季度报告",
            "示例基金2025年第四季度报告",
            "示例基金2025年年度报告",
        )
        self.assertEqual([announcement_report_period(title) for title in titles], [expected] * 3)

    def test_four_operational_deadlines_and_pre_april_window(self) -> None:
        cases = {
            date(2026, 4, 6): date(2025, 9, 30),
            date(2026, 4, 7): date(2025, 12, 31),
            date(2026, 5, 6): date(2025, 12, 31),
            date(2026, 5, 7): date(2026, 3, 31),
            date(2026, 8, 6): date(2026, 3, 31),
            date(2026, 8, 7): date(2026, 6, 30),
            date(2026, 11, 6): date(2026, 6, 30),
            date(2026, 11, 7): date(2026, 9, 30),
        }
        for as_of, expected in cases.items():
            with self.subTest(as_of=as_of):
                self.assertEqual(expected_report_period(as_of), expected)


if __name__ == "__main__":
    unittest.main()
