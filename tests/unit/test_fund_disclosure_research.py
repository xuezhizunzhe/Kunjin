import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

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
from kunjin.funds.research import build_disclosure_report

AS_OF = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def source(
    source_id: int,
    kind: DocumentKind,
    *,
    tier: int = 1,
    published_at: Optional[datetime] = None,
) -> SourceDocument:
    return SourceDocument(
        id=source_id,
        fund_code="519755",
        document_kind=kind,
        title=f"{kind.value}-{source_id}",
        url=f"https://example.com/{kind.value}/{source_id}",
        source_name="official" if tier == 1 else "eastmoney_f10",
        source_tier=tier,
        publisher="示例基金公司" if tier == 1 else "东方财富",
        published_at=published_at,
        retrieved_at=AS_OF,
        checksum=f"{source_id:x}".rjust(64, "0"),
    )


def complete_bundle() -> DisclosureBundle:
    sources = {
        1: source(
            1,
            DocumentKind.BASIC_PROFILE,
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        2: source(2, DocumentKind.MANAGER_HISTORY),
        3: source(3, DocumentKind.FEE_SCHEDULE),
        4: source(
            4,
            DocumentKind.SIZE_HISTORY,
            published_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
        ),
        5: source(
            5,
            DocumentKind.QUARTERLY_HOLDINGS,
            published_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
        6: source(
            6,
            DocumentKind.INDUSTRY_EXPOSURE,
            published_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
        7: source(
            7,
            DocumentKind.ANNOUNCEMENT,
            published_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        ),
    }
    statuses = {
        kind.value: {
            "state": "success",
            "current_source_document_id": str(source_id),
            "last_attempted_at": AS_OF.isoformat(),
            "last_success_at": AS_OF.isoformat(),
            "warning": None,
            "error_code": None,
            "error_message": None,
        }
        for source_id, kind in (
            (1, DocumentKind.BASIC_PROFILE),
            (2, DocumentKind.MANAGER_HISTORY),
            (3, DocumentKind.FEE_SCHEDULE),
            (4, DocumentKind.SIZE_HISTORY),
            (5, DocumentKind.QUARTERLY_HOLDINGS),
            (6, DocumentKind.INDUSTRY_EXPOSURE),
            (7, DocumentKind.ANNOUNCEMENT),
        )
    }
    return DisclosureBundle(
        fund_code="519755",
        identity=FundIdentity(
            "519755", "示例基金A", "active", "混合型", date(2020, 1, 1), "示例基金公司", 1
        ),
        share_classes=(FundShareClass("519755", "519755", "A", "示例基金A", 1),),
        manager_tenures=(FundManagerTenure("519755", "张三", date(2024, 1, 1), None, 2),),
        fee_rules=(
            FundFeeRule(
                "519755",
                FeeType.MANAGEMENT,
                3,
                share_class="A",
                rate=Decimal("1.20"),
                rule_order=1,
            ),
            FundFeeRule(
                "519755", FeeType.REDEMPTION, 3, share_class="A", rate=Decimal("1.50"),
                holding_days_min=0, holding_days_max=6, rule_order=2,
            ),
        ),
        sizes=(
            FundSizeObservation(
                "519755", date(2026, 6, 30), Decimal("100000000"), Decimal("50000000"),
                datetime(2026, 7, 5, tzinfo=timezone.utc), 4,
            ),
        ),
        benchmarks=(
            FundBenchmark(
                "519755", "沪深300指数收益率*80%+中债指数收益率*20%", None, None, 1
            ),
        ),
        holdings=(
            FundHolding(
                "519755", date(2026, 6, 30), datetime(2026, 7, 8, tzinfo=timezone.utc), 1,
                "600000", "浦发银行", AssetType.STOCK, Decimal("5.40"), "top10", 5,
            ),
        ),
        industry_exposure=(
            FundIndustryExposure(
                "519755", date(2026, 6, 30), datetime(2026, 7, 8, tzinfo=timezone.utc),
                "申万一级", "银行", Decimal("12.50"), 6,
            ),
        ),
        announcements=(
            FundAnnouncement(
                "519755", "2026年第二季度报告", "定期报告", "示例基金公司",
                datetime(2026, 7, 8, tzinfo=timezone.utc), "https://example.com/report.pdf", 1, 7,
            ),
        ),
        source_documents=sources,
        section_states={name: str(status["state"]) for name, status in statuses.items()},
        section_statuses=statuses,
    )


class FundDisclosureResearchTest(unittest.TestCase):
    def test_complete_active_fund_has_sourced_verified_report(self) -> None:
        result = build_disclosure_report(complete_bundle(), AS_OF)

        self.assertEqual(result["evidence_level"], "verified_fact")
        self.assertEqual(result["managers"]["current"][0]["manager_name"], "张三")
        self.assertEqual(result["holdings"]["report_period"], "2026-06-30")
        self.assertEqual(result["holdings"]["published_at"], "2026-07-08T00:00:00+00:00")
        self.assertEqual(result["holdings"]["disclosure_scopes"], ["top10"])
        self.assertEqual(result["holdings"]["age_days"], 11)
        self.assertEqual(result["missing_sections"], {})
        for key in (
            "sources", "publication_dates", "report_dates", "freshness", "warnings", "conflicts",
        ):
            self.assertIn(key, result)
        self.assertNotIn("score", result)
        self.assertNotIn("recommendation", result)

    def test_current_manager_not_disclosed_uses_actual_section_state(self) -> None:
        bundle = complete_bundle()
        statuses = dict(bundle.section_statuses)
        statuses[DocumentKind.MANAGER_HISTORY.value] = {
            **statuses[DocumentKind.MANAGER_HISTORY.value],
            "state": "not_disclosed",
        }
        result = build_disclosure_report(
            replace(
                bundle,
                manager_tenures=(),
                section_states={
                    **bundle.section_states,
                    DocumentKind.MANAGER_HISTORY.value: "not_disclosed",
                },
                section_statuses=statuses,
            ),
            AS_OF,
        )

        self.assertEqual(result["managers"]["evidence_level"], "insufficient_data")
        self.assertEqual(result["missing_sections"]["manager_history"], "not_disclosed")

    def test_only_former_managers_does_not_present_one_as_current(self) -> None:
        bundle = complete_bundle()
        result = build_disclosure_report(
            replace(
                bundle,
                manager_tenures=(
                    FundManagerTenure(
                        "519755", "前任经理", date(2020, 1, 1), date(2023, 12, 31), 2
                    ),
                ),
            ),
            AS_OF,
        )

        self.assertEqual(result["managers"]["current"], [])
        self.assertEqual(result["managers"]["former"][0]["manager_name"], "前任经理")
        self.assertEqual(result["missing_sections"]["current_manager"], "insufficient_data")
        self.assertIn("manager_history_contains_only_former_managers", result["warnings"])

    def test_missing_redemption_holding_period_rule_is_explicit(self) -> None:
        bundle = complete_bundle()
        result = build_disclosure_report(
            replace(bundle, fee_rules=(bundle.fee_rules[0],)),
            AS_OF,
        )

        self.assertEqual(result["missing_sections"]["redemption_fee_rules"], "insufficient_data")
        self.assertIn("redemption_holding_period_rules_are_missing", result["warnings"])

    def test_later_announcement_for_same_report_period_does_not_make_holdings_stale(self) -> None:
        bundle = complete_bundle()
        latest_report = replace(
            bundle.announcements[0],
            title="2026年半年度报告",
            published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
        result = build_disclosure_report(
            replace(bundle, announcements=(latest_report,)),
            AS_OF,
        )

        self.assertEqual(result["holdings"]["freshness"], "current")
        self.assertNotIn("holdings_are_older_than_latest_statutory_report", result["warnings"])

    def test_holdings_older_than_parsed_statutory_report_period_are_stale(self) -> None:
        bundle = complete_bundle()
        first_quarter_holding = replace(
            bundle.holdings[0],
            report_period=date(2026, 3, 31),
            published_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
        )
        half_year_report = replace(
            bundle.announcements[0],
            title="2026年半年度报告",
            published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )
        result = build_disclosure_report(
            replace(bundle, holdings=(first_quarter_holding,), announcements=(half_year_report,)),
            AS_OF,
        )

        self.assertEqual(result["holdings"]["freshness"], "stale")
        self.assertIn("holdings_are_older_than_latest_statutory_report", result["warnings"])

    def test_unparseable_statutory_title_is_not_guessed(self) -> None:
        bundle = complete_bundle()
        undated_report = replace(
            bundle.announcements[0],
            title="半年度报告",
            published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
        )

        result = build_disclosure_report(
            replace(bundle, announcements=(undated_report,)),
            AS_OF,
        )

        self.assertEqual(result["holdings"]["freshness"], "current")
        self.assertNotIn("holdings_are_older_than_latest_statutory_report", result["warnings"])

    def test_a_and_c_fee_conditions_remain_separate(self) -> None:
        bundle = complete_bundle()
        result = build_disclosure_report(
            replace(
                bundle,
                share_classes=(
                    FundShareClass("519755", "519755", "A", "示例基金A", 1),
                    FundShareClass("519755", "019755", "C", "示例基金C", 1),
                ),
                fee_rules=bundle.fee_rules
                + (
                    FundFeeRule(
                        "519755", FeeType.SALES_SERVICE, 3, share_class="C",
                        rate=Decimal("0.40"), rule_order=3,
                    ),
                ),
            ),
            AS_OF,
        )

        fee_classes = {
            (item["fee_type"], item["share_class"], item["rate"])
            for item in result["fees"]["rules"]
        }
        self.assertIn(("management", "A", "1.20"), fee_classes)
        self.assertIn(("sales_service", "C", "0.40"), fee_classes)
        self.assertIn("share_classes_have_different_fee_schedules", result["warnings"])
        self.assertEqual(result["conflicts"], [])

    def test_tier_one_manager_and_benchmark_win_while_conflicts_are_retained(self) -> None:
        bundle = complete_bundle()
        sources = dict(bundle.source_documents)
        sources[8] = source(8, DocumentKind.MANAGER_HISTORY, tier=2)
        sources[9] = source(9, DocumentKind.BASIC_PROFILE, tier=2)
        result = build_disclosure_report(
            replace(
                bundle,
                manager_tenures=(
                    FundManagerTenure("519755", "一级经理", date(2024, 1, 1), None, 2),
                    FundManagerTenure("519755", "二级经理", date(2024, 1, 1), None, 8),
                ),
                benchmarks=(
                    FundBenchmark("519755", "一级基准", None, None, 1),
                    FundBenchmark("519755", "二级基准", None, None, 9),
                ),
                source_documents=sources,
            ),
            AS_OF,
        )

        self.assertEqual(
            [item["manager_name"] for item in result["managers"]["current"]],
            ["一级经理"],
        )
        self.assertEqual(
            [item["description"] for item in result["benchmarks"]["items"]],
            ["一级基准"],
        )
        self.assertTrue(any("manager" in conflict for conflict in result["conflicts"]))
        self.assertTrue(any("benchmark" in conflict for conflict in result["conflicts"]))

    def test_empty_bundle_is_insufficient_without_claiming_unavailable_source(self) -> None:
        empty = DisclosureBundle(
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

        result = build_disclosure_report(empty, AS_OF)

        self.assertEqual(result["evidence_level"], "insufficient_data")
        self.assertEqual(result["missing_sections"]["basic_profile"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
