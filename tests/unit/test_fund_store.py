import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.funds.models import (
    AssetType,
    DocumentKind,
    FeeType,
    FundAnnouncement,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundIndustryExposure,
    FundManagerTenure,
    FundShareClass,
    FundSizeObservation,
    SourceDocument,
)
from kunjin.funds.store import FundDisclosureStore, make_record_key
from kunjin.storage.repository import Repository


class FundDisclosureStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.store = FundDisclosureStore(self.repository)
        self.now = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def source(self, checksum: str, retrieved_at=None) -> SourceDocument:
        return SourceDocument(
            id=None,
            fund_code="519755",
            document_kind=DocumentKind.MANAGER_HISTORY,
            title="基金经理变更记录",
            url="https://fundf10.eastmoney.com/jjjl_519755.html",
            source_name="eastmoney_f10",
            source_tier=2,
            publisher="东方财富",
            published_at=None,
            retrieved_at=retrieved_at or self.now,
            checksum=checksum,
        )

    def manager(self, name: str, start_date: date) -> FundManagerTenure:
        return FundManagerTenure(
            fund_code="519755",
            manager_name=name,
            start_date=start_date,
            end_date=None,
            source_document_id=None,
        )

    def section_source(self, kind: DocumentKind, checksum_character: str) -> SourceDocument:
        return SourceDocument(
            id=None,
            fund_code="519755",
            document_kind=kind,
            title=kind.value,
            url=f"https://fundf10.eastmoney.com/{kind.value}_519755.html",
            source_name="eastmoney_f10",
            source_tier=2,
            publisher="东方财富",
            published_at=None,
            retrieved_at=self.now,
            checksum=checksum_character * 64,
        )

    def test_record_key_is_stable_and_excludes_source_document_id(self) -> None:
        first = self.manager("张三", date(2025, 1, 1))
        second = FundManagerTenure(**{**first.__dict__, "source_document_id": 99})
        self.assertEqual(make_record_key(first), make_record_key(second))
        self.assertEqual(len(make_record_key(first)), 64)

    def test_publish_selects_only_current_source_version(self) -> None:
        old_id = self.store.publish_section(
            "519755",
            DocumentKind.MANAGER_HISTORY,
            self.source("a" * 64),
            [self.manager("旧经理", date(2024, 1, 1))],
            "success",
        )
        new_id = self.store.publish_section(
            "519755",
            DocumentKind.MANAGER_HISTORY.value,
            self.source("b" * 64, self.now + timedelta(days=1)),
            [self.manager("新经理", date(2025, 1, 1))],
            "success",
        )

        bundle = self.store.load_bundle("519755")

        self.assertNotEqual(old_id, new_id)
        self.assertEqual([item.manager_name for item in bundle.manager_tenures], ["新经理"])
        self.assertEqual(set(bundle.source_documents), {new_id})
        with self.repository.connect() as connection:
            history_count = connection.execute(
                "SELECT COUNT(*) AS count FROM fund_manager_tenures WHERE fund_code = '519755'"
            ).fetchone()["count"]
        self.assertEqual(history_count, 2)

    def test_repeated_announcement_url_remains_visible_in_new_source_version(self) -> None:
        old_source = self.section_source(DocumentKind.ANNOUNCEMENT, "a")
        new_source = SourceDocument(
            **{
                **old_source.__dict__,
                "retrieved_at": self.now + timedelta(days=1),
                "checksum": "b" * 64,
            }
        )
        announcement = FundAnnouncement(
            fund_code="519755",
            title="季度报告",
            category="定期报告",
            publisher="示例基金公司",
            published_at=self.now,
            url="https://example.com/report.pdf",
            source_tier=2,
            source_document_id=None,
        )
        self.store.publish_section(
            "519755", DocumentKind.ANNOUNCEMENT, old_source, [announcement], "success"
        )
        new_id = self.store.publish_section(
            "519755", DocumentKind.ANNOUNCEMENT, new_source, [announcement], "success"
        )

        bundle = self.store.load_bundle("519755")

        self.assertEqual(len(bundle.announcements), 1)
        self.assertEqual(bundle.announcements[0].source_document_id, new_id)

    def test_publish_rejects_undated_holdings_before_sqlite_write(self) -> None:
        holding = FundHolding(
            "519755", date(2026, 6, 30), None, 1, "000001", "平安银行",
            AssetType.STOCK, Decimal("6.25"), "top10", None,
        )

        with self.assertRaisesRegex(ValueError, "publication date"):
            self.store.publish_section(
                "519755",
                DocumentKind.QUARTERLY_HOLDINGS,
                self.section_source(DocumentKind.QUARTERLY_HOLDINGS, "d"),
                [holding],
                "success",
            )

    def test_basic_profile_publishes_identity_share_classes_and_benchmark(self) -> None:
        source = self.section_source(DocumentKind.BASIC_PROFILE, "c")
        records = [
            FundIdentity("519755", "示例基金A", "active", "混合型", None, "示例公司", None),
            FundShareClass("519755", "519756", "C", "示例基金C", None),
            FundBenchmark("519755", "沪深300指数收益率", None, None, None),
        ]

        self.store.publish_section(
            "519755", DocumentKind.BASIC_PROFILE, source, records, "success"
        )

        bundle = self.store.load_bundle("519755")
        self.assertIsNotNone(bundle.identity)
        self.assertEqual(bundle.identity.fund_name, "示例基金A")
        self.assertEqual([item.related_fund_code for item in bundle.share_classes], ["519756"])
        self.assertEqual([item.description for item in bundle.benchmarks], ["沪深300指数收益率"])

    def test_failed_publication_rolls_back_and_preserves_previous_pointer(self) -> None:
        good_id = self.store.publish_section(
            "519755",
            DocumentKind.MANAGER_HISTORY,
            self.source("a" * 64),
            [self.manager("有效经理", date(2024, 1, 1))],
            "success",
        )
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER reject_broken_manager
                BEFORE INSERT ON fund_manager_tenures
                WHEN NEW.manager_name = '触发回滚'
                BEGIN
                    SELECT RAISE(ABORT, 'forced publication failure');
                END
                """
            )

        with self.assertRaisesRegex(Exception, "forced publication failure"):
            self.store.publish_section(
                "519755",
                DocumentKind.MANAGER_HISTORY,
                self.source("b" * 64, self.now + timedelta(days=1)),
                [self.manager("触发回滚", date(2025, 1, 1))],
                "success",
            )

        bundle = self.store.load_bundle("519755")
        self.assertEqual([item.manager_name for item in bundle.manager_tenures], ["有效经理"])
        self.assertEqual(set(bundle.source_documents), {good_id})
        with self.repository.connect() as connection:
            documents = connection.execute(
                "SELECT id FROM fund_source_documents ORDER BY id"
            ).fetchall()
            pointer = connection.execute(
                "SELECT current_source_document_id FROM fund_section_syncs"
            ).fetchone()["current_source_document_id"]
        self.assertEqual([int(row["id"]) for row in documents], [good_id])
        self.assertEqual(pointer, good_id)

    def test_failure_status_retains_last_successful_evidence(self) -> None:
        source_id = self.store.publish_section(
            "519755",
            DocumentKind.MANAGER_HISTORY,
            self.source("a" * 64),
            [self.manager("有效经理", date(2024, 1, 1))],
            "success",
        )
        attempted_at = self.now + timedelta(days=1)

        self.store.mark_section_failure(
            "519755",
            DocumentKind.MANAGER_HISTORY.value,
            "connection_refused",
            "接口临时拒绝连接",
            attempted_at,
        )

        bundle = self.store.load_bundle("519755")
        status = self.store.section_status("519755")[DocumentKind.MANAGER_HISTORY.value]
        self.assertEqual(
            bundle.section_states[DocumentKind.MANAGER_HISTORY.value],
            "source_unavailable",
        )
        self.assertEqual([item.manager_name for item in bundle.manager_tenures], ["有效经理"])
        self.assertEqual(set(bundle.source_documents), {source_id})
        self.assertEqual(status["current_source_document_id"], str(source_id))
        self.assertEqual(status["last_attempted_at"], attempted_at.isoformat())
        self.assertEqual(status["error_code"], "connection_refused")
        self.assertIsNotNone(status["last_success_at"])

    def test_all_fact_types_round_trip_through_current_section_pointers(self) -> None:
        published_at = self.now - timedelta(days=1)
        publications = [
            (
                DocumentKind.BASIC_PROFILE,
                "1",
                [
                    FundIdentity(
                        "519755", "交银多策略回报灵活配置混合A", "active",
                        "混合型", date(2015, 6, 2), "张三", None,
                    ),
                    FundShareClass(
                        "519755", "519755", "A",
                        "交银多策略回报灵活配置混合A", None,
                    ),
                ],
            ),
            (
                DocumentKind.FEE_SCHEDULE,
                "2",
                [FundFeeRule(
                    "519755", FeeType.MANAGEMENT, None,
                    rate=Decimal("1.20"), raw_rule_text="年费率1.20%",
                )],
            ),
            (
                DocumentKind.SIZE_HISTORY,
                "3",
                [FundSizeObservation(
                    "519755", date(2026, 6, 30), Decimal("123456789.01"),
                    Decimal("100000000"), published_at, None,
                )],
            ),
            (
                DocumentKind.BENCHMARK,
                "4",
                [FundBenchmark(
                    "519755", "沪深300指数收益率*50%+中债综合指数收益率*50%",
                    None, None, None,
                )],
            ),
            (
                DocumentKind.QUARTERLY_HOLDINGS,
                "5",
                [FundHolding(
                    "519755", date(2026, 6, 30), published_at, 1,
                    "600000", "浦发银行", AssetType.STOCK, Decimal("5.25"),
                    "top_ten", None, Decimal("1000"), Decimal("12345.67"),
                )],
            ),
            (
                DocumentKind.INDUSTRY_EXPOSURE,
                "6",
                [FundIndustryExposure(
                    "519755", date(2026, 6, 30), published_at, "证监会行业",
                    "金融业", Decimal("12.30"), None, "J", Decimal("3000000"),
                )],
            ),
            (
                DocumentKind.ANNOUNCEMENT,
                "7",
                [FundAnnouncement(
                    "519755", "基金季度报告", "定期报告", "基金管理人",
                    published_at, "https://example.com/fund-report.pdf", 1, None,
                )],
            ),
        ]
        for kind, checksum_character, records in publications:
            self.store.publish_section(
                "519755", kind, self.section_source(kind, checksum_character), records, "success"
            )

        bundle = self.store.load_bundle("519755")

        self.assertEqual(bundle.identity.fund_name, "交银多策略回报灵活配置混合A")
        self.assertEqual(bundle.share_classes[0].share_class, "A")
        self.assertEqual(bundle.fee_rules[0].rate, Decimal("1.20"))
        self.assertEqual(bundle.sizes[0].net_assets, Decimal("123456789.01"))
        self.assertEqual(len(bundle.benchmarks), 1)
        self.assertEqual(bundle.holdings[0].asset_type, AssetType.STOCK)
        self.assertEqual(bundle.industry_exposure[0].industry_code, "J")
        self.assertEqual(bundle.announcements[0].source_tier, 1)
        self.assertEqual(len(bundle.source_documents), len(publications))

    def test_not_disclosed_is_a_successful_empty_publication(self) -> None:
        source_id = self.store.publish_section(
            "519755",
            DocumentKind.QUARTERLY_HOLDINGS,
            self.section_source(DocumentKind.QUARTERLY_HOLDINGS, "8"),
            [],
            "not_disclosed",
            warning="基金尚未披露季度持仓",
        )

        bundle = self.store.load_bundle("519755")

        self.assertEqual(bundle.holdings, ())
        self.assertEqual(
            bundle.section_states[DocumentKind.QUARTERLY_HOLDINGS.value],
            "not_disclosed",
        )
        self.assertEqual(set(bundle.source_documents), {source_id})
        self.assertEqual(bundle.warnings, ("基金尚未披露季度持仓",))


if __name__ == "__main__":
    unittest.main()
