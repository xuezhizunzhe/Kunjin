import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
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


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def source_document(**overrides) -> SourceDocument:
    values = {
        "id": None,
        "fund_code": "519755",
        "document_kind": DocumentKind.MANAGER_HISTORY,
        "title": "基金经理变更记录",
        "url": "https://fundf10.eastmoney.com/jjjl_519755.html",
        "source_name": "eastmoney_f10",
        "source_tier": 2,
        "publisher": "东方财富",
        "published_at": None,
        "retrieved_at": NOW,
        "checksum": "a" * 64,
    }
    values.update(overrides)
    return SourceDocument(**values)


class SourceDocumentTest(unittest.TestCase):
    def test_valid_source_document_is_accepted(self) -> None:
        source_document().validate()

    def test_invalid_fund_code_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid fund code"):
            source_document(fund_code="51975").validate()

    def test_non_https_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            source_document(url="http://fundf10.eastmoney.com/a").validate()

    def test_source_tier_must_be_between_one_and_three(self) -> None:
        for tier in (0, 4):
            with self.subTest(tier=tier):
                with self.assertRaisesRegex(ValueError, "source tier"):
                    source_document(source_tier=tier).validate()

    def test_retrieval_time_must_be_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "retrieved_at"):
            source_document(retrieved_at=datetime(2026, 7, 11)).validate()

    def test_publication_time_must_be_aware_when_present(self) -> None:
        with self.assertRaisesRegex(ValueError, "published_at"):
            source_document(published_at=datetime(2026, 7, 10)).validate()


class NormalizedFactValidationTest(unittest.TestCase):
    def test_all_fact_models_accept_valid_values(self) -> None:
        records = (
            FundIdentity("519755", "示例基金", "active", "混合型", date(2020, 1, 1), "示例公司", 1),
            FundShareClass("519755", "519755", "A", "示例基金A", 1),
            FundManagerTenure("519755", "张三", date(2024, 1, 1), None, 1),
            FundFeeRule("519755", FeeType.MANAGEMENT, 1, rate=Decimal("1.20"), rule_order=1),
            FundSizeObservation("519755", date(2026, 6, 30), Decimal("100000000"), Decimal("50000000"), NOW, 1),
            FundBenchmark("519755", "沪深300指数收益率*80%+中债指数收益率*20%", None, None, 1),
            FundHolding("519755", date(2026, 6, 30), NOW, 1, "600000", "浦发银行", AssetType.STOCK, Decimal("5.4"), "top10", 1),
            FundIndustryExposure("519755", date(2026, 6, 30), NOW, "申万一级", "银行", Decimal("12.5"), 1),
            FundAnnouncement("519755", "季度报告", "定期报告", "示例公司", NOW, "https://example.com/report.pdf", 2, 1),
        )

        for record in records:
            with self.subTest(record=type(record).__name__):
                record.validate()

    def test_every_fact_exposes_source_document_id(self) -> None:
        identity = FundIdentity("519755", "示例基金", "active", None, None, None, None)
        self.assertIsNone(identity.source_document_id)

    def test_negative_fee_values_are_rejected(self) -> None:
        base = FundFeeRule("519755", FeeType.REDEMPTION, 1, rate=Decimal("1"), rule_order=1)
        for field_name in ("rate", "fixed_amount", "amount_min", "amount_max"):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(ValueError, "cannot be negative"):
                    replace(base, **{field_name: Decimal("-0.01")}).validate()

    def test_negative_holding_day_bounds_are_rejected(self) -> None:
        rule = FundFeeRule(
            "519755",
            FeeType.REDEMPTION,
            1,
            holding_days_min=-1,
            rule_order=1,
        )
        with self.assertRaisesRegex(ValueError, "holding day"):
            rule.validate()

    def test_inverted_fee_intervals_are_rejected(self) -> None:
        amount_rule = FundFeeRule(
            "519755",
            FeeType.SUBSCRIPTION,
            1,
            amount_min=Decimal("100"),
            amount_max=Decimal("10"),
            rule_order=1,
        )
        day_rule = FundFeeRule(
            "519755",
            FeeType.REDEMPTION,
            1,
            holding_days_min=30,
            holding_days_max=7,
            rule_order=1,
        )
        with self.assertRaisesRegex(ValueError, "amount interval"):
            amount_rule.validate()
        with self.assertRaisesRegex(ValueError, "holding day interval"):
            day_rule.validate()

    def test_holding_weight_must_be_between_zero_and_one_hundred(self) -> None:
        base = FundHolding(
            "519755", date(2026, 6, 30), NOW, 1, "600000", "浦发银行",
            AssetType.STOCK, Decimal("5"), "top10", 1,
        )
        for weight in (Decimal("-0.01"), Decimal("100.01")):
            with self.subTest(weight=weight):
                with self.assertRaisesRegex(ValueError, "weight"):
                    replace(base, weight=weight).validate()

    def test_industry_weight_must_be_between_zero_and_one_hundred(self) -> None:
        record = FundIndustryExposure(
            "519755", date(2026, 6, 30), NOW, "申万一级", "银行", Decimal("101"), 1
        )
        with self.assertRaisesRegex(ValueError, "weight"):
            record.validate()

    def test_manager_end_date_cannot_precede_start_date(self) -> None:
        tenure = FundManagerTenure(
            "519755", "张三", date(2026, 1, 2), date(2026, 1, 1), 1
        )
        with self.assertRaisesRegex(ValueError, "end date"):
            tenure.validate()

    def test_fact_publication_times_must_be_aware(self) -> None:
        holding = FundHolding(
            "519755", date(2026, 6, 30), datetime(2026, 7, 1), 1,
            "600000", "浦发银行", AssetType.STOCK, Decimal("5"), "top10", 1,
        )
        with self.assertRaisesRegex(ValueError, "published_at"):
            holding.validate()

    def test_bundle_uses_immutable_fact_collections(self) -> None:
        bundle = DisclosureBundle(
            fund_code="519755",
            identity=None,
            share_classes=(),
            manager_tenures=(),
            fee_rules=(),
            sizes=(),
            benchmarks=(),
            holdings=(),
            industry_exposure=(),
            announcements=(),
            source_documents={},
            section_states={},
            section_statuses={},
        )
        self.assertEqual(bundle.warnings, ())
        self.assertEqual(bundle.conflicts, ())


if __name__ == "__main__":
    unittest.main()
