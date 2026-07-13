from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import patch

from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundManagerTenure,
    FundShareClass,
    FundSizeObservation,
    SourceDocument,
)
from kunjin.funds.peers import research as peer_research
from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
)
from kunjin.funds.peers.research import (
    build_explicit_compare_report,
    build_peer_report,
    build_portfolio_overlap_report,
    comparison_fingerprint,
)
from kunjin.models import FundNavObservation, StoredPosition

NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def make_bundle(
    code: str,
    *,
    fund_type: str = "混合型-灵活",
    benchmark: str = "50%沪深300指数收益率+50%中债综合全价指数收益率",
    manager_start: date = date(2025, 1, 1),
    share_class: Optional[str] = None,
    related_code: Optional[str] = None,
    holdings: tuple[FundHolding, ...] = (),
    fees: tuple[FundFeeRule, ...] = (),
    sizes: tuple[FundSizeObservation, ...] = (),
) -> DisclosureBundle:
    source_id = int(code) + 1
    return DisclosureBundle(
        fund_code=code,
        identity=FundIdentity(
            code,
            f"示例基金{code}{share_class or ''}",
            "active",
            fund_type,
            date(2020, 1, 1),
            "示例基金公司",
            source_id,
        ),
        share_classes=(
            ()
            if related_code is None or share_class is None
            else (FundShareClass(code, related_code, share_class, None, source_id),)
        ),
        manager_tenures=(
            FundManagerTenure(code, f"经理{code}", manager_start, None, source_id),
        ),
        fee_rules=fees,
        sizes=sizes,
        benchmarks=(FundBenchmark(code, benchmark, None, None, source_id),),
        holdings=holdings,
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )


def make_group(*codes: str) -> PeerGroup:
    return PeerGroup(
        id=1,
        anchor_fund_code=codes[0],
        rule_version="1",
        rule_key="混合型-灵活|active_or_unspecified|equity_bond",
        rule_description="同类型、管理方式与基准族",
        candidate_source_url="https://fund.eastmoney.com/js/fundcode_search.js",
        candidate_source_tier=2,
        candidate_source_checksum="a" * 64,
        input_fingerprint="b" * 64,
        created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        status=PeerGroupStatus.SUCCESS,
        members=tuple(
            PeerGroupMember(
                code,
                MembershipKind.ANCHOR if index == 0 else MembershipKind.DISCOVERED,
                "混合型-灵活|active_or_unspecified|equity_bond",
                "classification_match",
                None,
                int(code) + 1,
            )
            for index, code in enumerate(codes)
        ),
    )


def navs(code: str, start_value: str, end_value: str) -> tuple[FundNavObservation, ...]:
    start = date(2025, 7, 10)
    values = (start_value, "1.05", "1.02", end_value)
    days = (start, date(2026, 4, 11), date(2026, 7, 1), date(2026, 7, 10))
    return tuple(
        FundNavObservation(code, day, Decimal(value), None, None, "formal", NOW)
        for day, value in zip(days, values)
    )


def holding(code: str, security: str, weight: str) -> FundHolding:
    return FundHolding(
        code,
        date(2026, 3, 31),
        datetime(2026, 4, 21, tzinfo=timezone.utc),
        1,
        security,
        f"证券{security}",
        AssetType.STOCK,
        Decimal(weight),
        "top10",
        int(code) + 1,
    )


def position(code: str, value: str = "100") -> StoredPosition:
    return StoredPosition(
        "养基宝", code, f"基金{code}", Decimal(value), NOW, formal_nav=Decimal("1")
    )


def with_source(bundle: DisclosureBundle, source_id: int, url: str) -> DisclosureBundle:
    source = SourceDocument(
        source_id,
        bundle.fund_code,
        DocumentKind.BASIC_PROFILE,
        "基金资料",
        url,
        "测试来源",
        2,
        "测试发布方",
        NOW,
        NOW,
        "c" * 64,
    )
    return replace(bundle, source_documents={source_id: source})


class PeerResearchTest(unittest.TestCase):
    def test_single_member_report_has_stable_contract_and_no_trading_keys(self) -> None:
        report = build_peer_report(
            make_group("519755"), {"519755": make_bundle("519755")}, {}, (), NOW
        )

        self.assertEqual(report["anchor_fund_code"], "519755")
        self.assertEqual(report["rule_version"], "1")
        self.assertEqual(report["rule_key"], "混合型-灵活|active_or_unspecified|equity_bond")
        self.assertEqual(report["rule_description"], "同类型、管理方式与基准族")
        self.assertEqual(report["group_status"], "success")
        self.assertEqual(report["calculation_version"], "1")
        self.assertEqual(report["windows"], {"90d": [], "365d": [], "manager_tenure": []})
        self.assertEqual(report["fees"], {"519755": []})
        self.assertEqual(report["sizes"], {"519755": None})
        self.assertEqual(report["data_gaps"], ["peer_group_too_small"])
        self.assertIn("peer_group_too_small", report["warnings"])
        self.assertEqual(report["coverage"]["members_total"], 1)
        self.assertEqual(
            report["data_dates"]["peer_group_created_at"],
            "2026-07-11T00:00:00+00:00",
        )
        self.assertEqual(report["warnings"], ["peer_group_too_small"])
        self.assertEqual(report["candidate_source"]["type"], "peer_directory")
        self.assertEqual(report["sources"][0]["url"], report["candidate_source"]["url"])
        encoded = json.dumps(report, ensure_ascii=False)
        for forbidden in ('"score"', '"overall_score"', '"recommendation"', '"buy"', '"sell"'):
            self.assertNotIn(forbidden, encoded)

    def test_metric_orderings_are_independent_and_manager_start_is_not_ranked(self) -> None:
        bundles = {
            "519755": make_bundle("519755", manager_start=date(2025, 1, 1)),
            "000001": make_bundle("000001", manager_start=date(2025, 6, 1)),
        }
        report = build_peer_report(
            make_group("519755", "000001"),
            bundles,
            {
                "519755": (
                    FundNavObservation(
                        "519755", date(2025, 1, 1), Decimal("0.9"), None, None, "formal", NOW
                    ),
                    *navs("519755", "1", "1.10"),
                ),
                "000001": (
                    FundNavObservation(
                        "000001", date(2025, 6, 1), Decimal("0.95"), None, None, "formal", NOW
                    ),
                    *navs("000001", "1", "1.20"),
                ),
            },
            (),
            NOW,
        )

        ordering = report["metric_orderings"]["90d"]
        self.assertEqual(ordering["total_return"]["direction"], "higher")
        self.assertEqual(ordering["max_drawdown"]["direction"], "lower")
        self.assertEqual(ordering["total_return"]["fund_codes"][0], "000001")
        self.assertEqual(report["metric_orderings"]["manager_tenure"], {})
        self.assertIn("manager_tenure_start_dates_differ", report["warnings"])

    def test_fees_preserve_conditions_and_sum_only_current_ongoing_rates(self) -> None:
        fees = (
            FundFeeRule("519755", FeeType.MANAGEMENT, 1, share_class="A", rate=Decimal("1.2")),
            FundFeeRule("519755", FeeType.CUSTODY, 1, share_class="A", rate=Decimal("0.2")),
            FundFeeRule("519755", FeeType.SALES_SERVICE, 1, share_class="C", rate=Decimal("0.4")),
            FundFeeRule(
                "519755", FeeType.SUBSCRIPTION, 1, share_class="A",
                rate=Decimal("1.5"), amount_max=Decimal("100000"),
            ),
            FundFeeRule(
                "519755", FeeType.REDEMPTION, 1, share_class="A",
                rate=Decimal("0.5"), holding_days_max=6,
            ),
        )
        report = build_explicit_compare_report(
            ("519755", "000001"),
            {
                "519755": make_bundle("519755", share_class="A", fees=fees),
                "000001": make_bundle("000001"),
            },
            {},
            (
                StoredPosition(
                    "养基宝", "519755", "基金A", Decimal("1"), NOW,
                    share_class="A", formal_nav=Decimal("1"),
                ),
            ),
            NOW,
        )

        self.assertEqual(report["ongoing_annual_fee_rates"]["519755"], "1.4")
        raw = report["fees"]["519755"]
        self.assertEqual(len(raw), 5)
        subscription = next(rule for rule in raw if rule["fee_type"] == "subscription")
        redemption = next(rule for rule in raw if rule["fee_type"] == "redemption")
        self.assertEqual(subscription["amount_max"], "100000")
        self.assertEqual(redemption["holding_days_max"], 6)

    def test_share_class_sibling_and_explicit_non_peer_warnings_are_visible(self) -> None:
        sibling = build_explicit_compare_report(
            ("519755", "000001"),
            {
                "519755": make_bundle("519755", share_class="A", related_code="000001"),
                "000001": make_bundle("000001", share_class="C", related_code="519755"),
            },
            {},
            (),
            NOW,
        )
        self.assertIn("share_class_sibling:519755:000001", sibling["warnings"])

        non_peer = build_explicit_compare_report(
            ("519755", "000002"),
            {
                "519755": make_bundle("519755"),
                "000002": make_bundle("000002", fund_type="债券型"),
            },
            {},
            (),
            NOW,
        )
        self.assertIn("comparability_warning:000002:type_mismatch", non_peer["warnings"])

    def test_overlap_is_top10_and_portfolio_missing_holdings_reduces_coverage(self) -> None:
        bundles = {
            "519755": make_bundle("519755", holdings=(holding("519755", "600000", "10"),)),
            "000001": make_bundle("000001", holdings=(holding("000001", "600000", "5"),)),
            "000002": make_bundle("000002"),
        }
        compare = build_explicit_compare_report(
            ("519755", "000001"), bundles, {}, (), NOW
        )
        metric_name = compare["pairwise_overlap"][0]["security"]["metric_name"]
        self.assertEqual(metric_name, "top10_disclosed_overlap")
        self.assertNotEqual(metric_name, "total_overlap")

        portfolio = build_portfolio_overlap_report(
            bundles,
            (position("519755"), position("000002")),
            NOW,
        )
        self.assertEqual(portfolio["portfolio_overlap"]["portfolio_weight_coverage"], "0.5")
        self.assertIn("portfolio_coverage_partial", portfolio["warnings"])

    def test_size_stability_and_fingerprint_are_json_stable(self) -> None:
        sizes = tuple(
            FundSizeObservation("519755", date(2025, month, day), Decimal(value), None, NOW, 1)
            for month, day, value in ((3, 31, "100"), (6, 30, "110"), (9, 30, "99"))
        )
        report = build_peer_report(
            make_group("519755"), {"519755": make_bundle("519755", sizes=sizes)}, {}, (), NOW
        )
        self.assertEqual(report["sizes"]["519755"]["observations"], 3)
        payload = {"when": NOW, "day": date(2026, 7, 10), "amount": Decimal("1.20")}
        self.assertEqual(
            comparison_fingerprint(payload),
            comparison_fingerprint(dict(reversed(list(payload.items())))),
        )
        self.assertEqual(len(comparison_fingerprint(payload)), 64)
        for invalid in (Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    comparison_fingerprint({"invalid": invalid})

    def test_portfolio_warnings_and_forbidden_key_guard_are_preserved(self) -> None:
        bundles = {
            "519755": make_bundle(
                "519755", holdings=(holding("519755", "600000", "10"),)
            )
        }
        estimated = StoredPosition(
            "养基宝",
            "519755",
            "基金519755",
            Decimal("100"),
            NOW,
            estimated_nav=Decimal("1"),
        )
        report = build_portfolio_overlap_report(bundles, (estimated,), NOW)
        self.assertIn("portfolio value includes intraday estimated NAV", report["warnings"])

        with patch.object(peer_research, "FORBIDDEN_REPORT_KEYS", {"errors"}):
            with self.assertRaises(AssertionError):
                build_portfolio_overlap_report(bundles, (estimated,), NOW)

    def test_non_nav_orderings_feed_advantages_and_tradeoffs(self) -> None:
        left_sizes = tuple(
            FundSizeObservation("519755", date(2025, month, day), Decimal(value), None, NOW, 1)
            for month, day, value in ((3, 31, "100"), (6, 30, "101"), (9, 30, "102"))
        )
        right_sizes = tuple(
            FundSizeObservation("000001", date(2025, month, day), Decimal(value), None, NOW, 1)
            for month, day, value in ((3, 31, "100"), (6, 30, "130"), (9, 30, "90"))
        )
        bundles = {
            "519755": make_bundle(
                "519755",
                fees=(FundFeeRule("519755", FeeType.MANAGEMENT, 1, rate=Decimal("0.5")),),
                sizes=left_sizes,
                holdings=(holding("519755", "600000", "10"),),
            ),
            "000001": make_bundle(
                "000001",
                fees=(FundFeeRule("000001", FeeType.MANAGEMENT, 1, rate=Decimal("1.0")),),
                sizes=right_sizes,
                holdings=(holding("000001", "600000", "5"),),
            ),
        }
        report = build_explicit_compare_report(
            ("519755", "000001"), bundles, {}, (position("519755"),), NOW
        )
        for metric in (
            "ongoing_annual_fee_rate",
            "size_stability",
            "portfolio_overlap",
        ):
            self.assertTrue(any(item.startswith(f"{metric}:") for item in report["advantages"]))
            self.assertTrue(any(item.startswith(f"{metric}:") for item in report["tradeoffs"]))

    def test_sources_are_deduplicated_by_url(self) -> None:
        shared_url = "https://fundf10.eastmoney.com/example.html"
        bundles = {
            "519755": with_source(make_bundle("519755"), 10, shared_url),
            "000001": with_source(make_bundle("000001"), 11, shared_url),
        }
        report = build_explicit_compare_report(
            ("519755", "000001"), bundles, {}, (), NOW
        )
        matching = [source for source in report["sources"] if source["url"] == shared_url]
        self.assertEqual(len(matching), 1)


if __name__ == "__main__":
    unittest.main()
