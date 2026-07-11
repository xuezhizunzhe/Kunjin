from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from kunjin.funds.models import (
    AssetType,
    FundHolding,
    FundIndustryExposure,
    FundManagerTenure,
    FundSizeObservation,
)
from kunjin.funds.peers.analytics import (
    PEER_CALCULATION_VERSION,
    START_TOLERANCE_DAYS,
    calculate_size_stability,
    calculate_window_metric,
    common_end_date,
    current_manager_team_start,
    pairwise_industry_overlap,
    pairwise_overlap,
    portfolio_overlap,
    select_overlap_periods,
)
from kunjin.models import FundNavObservation


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def nav(day: date, value: str, fund_code: str = "519755") -> FundNavObservation:
    return FundNavObservation(
        fund_code=fund_code,
        nav_date=day,
        unit_nav=Decimal(value),
        accumulated_nav=None,
        daily_growth=None,
        source="eastmoney",
        retrieved_at=NOW,
    )


def size(day: date, value: Optional[str]) -> FundSizeObservation:
    return FundSizeObservation(
        fund_code="519755",
        report_date=day,
        net_assets=None if value is None else Decimal(value),
        total_shares=None,
        published_at=NOW,
        source_document_id=1,
    )


def holding(
    fund_code: str,
    security_code: str,
    security_name: str,
    weight: str,
    *,
    report_period: date = date(2026, 3, 31),
    published_at: Optional[datetime] = NOW,
    asset_type: AssetType = AssetType.STOCK,
    disclosure_scope: str = "top10",
) -> FundHolding:
    return FundHolding(
        fund_code=fund_code,
        report_period=report_period,
        published_at=published_at,
        rank=1,
        security_code=security_code,
        security_name=security_name,
        asset_type=asset_type,
        weight=Decimal(weight),
        disclosure_scope=disclosure_scope,
        source_document_id=1,
    )


def industry(
    fund_code: str,
    industry_name: str,
    weight: str,
    *,
    industry_code: Optional[str] = None,
    standard: str = "证监会行业分类",
    report_period: date = date(2026, 3, 31),
    published_at: Optional[datetime] = NOW,
) -> FundIndustryExposure:
    return FundIndustryExposure(
        fund_code=fund_code,
        report_period=report_period,
        published_at=published_at,
        classification_standard=standard,
        industry_name=industry_name,
        weight=Decimal(weight),
        source_document_id=1,
        industry_code=industry_code,
    )


class AlignedNavMetricTest(unittest.TestCase):
    def test_exports_stable_calculation_contract(self) -> None:
        self.assertEqual(PEER_CALCULATION_VERSION, "1")
        self.assertEqual(START_TOLERANCE_DAYS, 7)

    def test_common_end_date_is_latest_date_shared_by_every_history(self) -> None:
        histories = {
            "519755": [
                nav(date(2026, 7, 8), "1.00"),
                nav(date(2026, 7, 10), "1.02"),
                nav(date(2026, 7, 11), "1.03"),
            ],
            "000001": [
                nav(date(2026, 7, 9), "1.00", "000001"),
                nav(date(2026, 7, 10), "1.01", "000001"),
            ],
        }

        self.assertEqual(common_end_date(histories), date(2026, 7, 10))
        self.assertIsNone(common_end_date({}))
        self.assertIsNone(common_end_date({"519755": []}))
        self.assertIsNone(
            common_end_date(
                {
                    "519755": [nav(date(2026, 7, 10), "1.0")],
                    "000001": [nav(date(2026, 7, 9), "1.0", "000001")],
                }
            )
        )

    def test_window_uses_latest_baseline_within_seven_calendar_days(self) -> None:
        metric, warnings = calculate_window_metric(
            fund_code="519755",
            history=[
                nav(date(2026, 4, 7), "1.00"),
                nav(date(2026, 4, 9), "1.01"),
                nav(date(2026, 7, 9), "1.10"),
                nav(date(2026, 7, 10), "1.20"),
            ],
            window="90d",
            target_start=date(2026, 4, 10),
            effective_end=date(2026, 7, 9),
        )

        self.assertEqual(warnings, ())
        self.assertIsNotNone(metric)
        assert metric is not None
        self.assertEqual(metric.effective_start, date(2026, 4, 9))
        self.assertEqual(metric.effective_end, date(2026, 7, 9))
        self.assertEqual(metric.observations, 2)
        self.assertEqual(metric.total_return, Decimal("1.10") / Decimal("1.01") - 1)

    def test_missing_baseline_or_effective_end_is_explicit(self) -> None:
        for history in (
            [nav(date(2026, 4, 2), "1.00"), nav(date(2026, 7, 9), "1.10")],
            [nav(date(2026, 4, 10), "1.00"), nav(date(2026, 7, 8), "1.10")],
        ):
            with self.subTest(history=history):
                metric, warnings = calculate_window_metric(
                    "519755",
                    history,
                    "90d",
                    date(2026, 4, 10),
                    date(2026, 7, 9),
                )
                self.assertIsNone(metric)
                self.assertEqual(warnings, ("aligned_nav_window_unavailable",))

    def test_volatility_drawdown_trough_and_recovery_match_known_vector(self) -> None:
        metric, warnings = calculate_window_metric(
            "519755",
            [
                nav(date(2026, 1, 1), "1.0"),
                nav(date(2026, 1, 2), "1.2"),
                nav(date(2026, 1, 3), "0.9"),
                nav(date(2026, 1, 4), "1.2"),
            ],
            "known_vector",
            date(2026, 1, 1),
            date(2026, 1, 4),
        )

        self.assertEqual(warnings, ())
        assert metric is not None
        daily_returns = (
            Decimal("1.2") / Decimal("1.0") - 1,
            Decimal("0.9") / Decimal("1.2") - 1,
            Decimal("1.2") / Decimal("0.9") - 1,
        )
        mean = sum(daily_returns, Decimal("0")) / Decimal(len(daily_returns))
        expected_volatility = (
            sum((item - mean) ** 2 for item in daily_returns)
            / Decimal(len(daily_returns))
        ).sqrt() * Decimal(252).sqrt()
        self.assertEqual(metric.annualized_volatility, expected_volatility)
        self.assertEqual(metric.max_drawdown, Decimal("0.25"))
        self.assertEqual(metric.drawdown_peak_date, date(2026, 1, 2))
        self.assertEqual(metric.trough_date, date(2026, 1, 3))
        self.assertEqual(metric.recovery_date, date(2026, 1, 4))

    def test_one_observation_has_zero_return_and_drawdown_without_volatility(self) -> None:
        metric, warnings = calculate_window_metric(
            "519755",
            [nav(date(2026, 7, 9), "1.2")],
            "manager_tenure",
            date(2026, 7, 9),
            date(2026, 7, 9),
        )

        self.assertEqual(warnings, ())
        assert metric is not None
        self.assertEqual(metric.total_return, Decimal("0"))
        self.assertIsNone(metric.annualized_volatility)
        self.assertEqual(metric.max_drawdown, Decimal("0"))
        self.assertEqual(metric.drawdown_peak_date, date(2026, 7, 9))
        self.assertEqual(metric.trough_date, date(2026, 7, 9))
        self.assertIsNone(metric.recovery_date)

    def test_current_manager_team_start_uses_latest_active_start(self) -> None:
        tenures = (
            FundManagerTenure("519755", "前任", date(2020, 1, 1), date(2024, 1, 1), 1),
            FundManagerTenure("519755", "现任甲", date(2024, 2, 1), None, 2),
            FundManagerTenure("519755", "现任乙", date(2025, 3, 1), None, 3),
            FundManagerTenure("519755", "未来经理", date(2026, 8, 1), None, 4),
        )

        self.assertEqual(
            current_manager_team_start(tenures, date(2026, 7, 11)),
            date(2025, 3, 1),
        )
        self.assertIsNone(
            current_manager_team_start(
                (FundManagerTenure("519755", "前任", date(2020, 1, 1), date(2024, 1, 1), 1),),
                date(2026, 7, 11),
            )
        )

    def test_manager_tenure_metrics_preserve_each_funds_actual_start(self) -> None:
        left, left_warnings = calculate_window_metric(
            "519755",
            [
                nav(date(2025, 1, 1), "1.0"),
                nav(date(2026, 7, 10), "1.2"),
            ],
            "manager_tenure",
            date(2025, 1, 1),
            date(2026, 7, 10),
        )
        right, right_warnings = calculate_window_metric(
            "000001",
            [
                nav(date(2025, 6, 1), "1.0", "000001"),
                nav(date(2026, 7, 10), "1.1", "000001"),
            ],
            "manager_tenure",
            date(2025, 6, 1),
            date(2026, 7, 10),
        )

        self.assertEqual(left_warnings, ())
        self.assertEqual(right_warnings, ())
        assert left is not None and right is not None
        self.assertEqual(left.effective_start, date(2025, 1, 1))
        self.assertEqual(right.effective_start, date(2025, 6, 1))
        self.assertNotEqual(left.effective_start, right.effective_start)

    def test_size_stability_uses_latest_five_valid_observations(self) -> None:
        result = calculate_size_stability(
            (
                size(date(2024, 12, 31), "50"),
                size(date(2025, 3, 31), "100"),
                size(date(2025, 6, 30), None),
                size(date(2025, 9, 30), "110"),
                size(date(2025, 12, 31), "99"),
                size(date(2026, 3, 31), "108.9"),
                size(date(2026, 6, 30), "119.79"),
            )
        )

        self.assertEqual(result["evidence_level"], "deterministic_calculation")
        self.assertEqual(result["observations"], 5)
        self.assertEqual(result["earliest_report_date"], date(2025, 3, 31))
        self.assertEqual(result["latest_report_date"], date(2026, 6, 30))
        self.assertEqual(result["net_asset_change"], Decimal("0.1979"))
        changes = (Decimal("0.1"), Decimal("-0.1"), Decimal("0.1"), Decimal("0.1"))
        mean = sum(changes, Decimal("0")) / Decimal(len(changes))
        expected_pstdev = (
            sum((item - mean) ** 2 for item in changes) / Decimal(len(changes))
        ).sqrt()
        self.assertEqual(result["quarterly_change_pstdev"], expected_pstdev)

    def test_size_stability_requires_three_non_missing_positive_observations(self) -> None:
        result = calculate_size_stability(
            (
                size(date(2025, 12, 31), "100"),
                size(date(2026, 3, 31), None),
                size(date(2026, 6, 30), "110"),
            )
        )

        self.assertEqual(result, {"evidence_level": "insufficient_data", "observations": 2})


class OverlapTest(unittest.TestCase):
    def test_pairwise_overlap_uses_asset_type_and_code_without_normalizing(self) -> None:
        result = pairwise_overlap(
            "519755",
            "000001",
            (
                holding("519755", "600000", "浦发银行", "5.0"),
                holding("519755", "600519", "贵州茅台", "3.0"),
                holding("519755", "600000", "浦发转债", "1.0", asset_type=AssetType.BOND),
            ),
            (
                holding("000001", "600000", "浦发银行", "2.0"),
                holding("000001", "000001", "平安银行", "4.0"),
                holding("000001", "600000", "浦发转债", "0.5", asset_type=AssetType.BOND),
            ),
        )

        self.assertEqual(result.metric_name, "top10_disclosed_overlap")
        self.assertEqual(result.left_disclosed_weight, Decimal("9.0"))
        self.assertEqual(result.right_disclosed_weight, Decimal("6.5"))
        self.assertEqual(result.overlap, Decimal("2.5"))
        self.assertEqual(
            [
                (item.exposure_type, item.exposure_code, item.shared_weight)
                for item in result.shared
            ],
            [("bond", "600000", Decimal("0.5")), ("stock", "600000", Decimal("2.0"))],
        )

    def test_zero_overlap_is_a_valid_deterministic_result(self) -> None:
        result = pairwise_overlap(
            "519755",
            "000001",
            (holding("519755", "600000", "浦发银行", "5"),),
            (holding("000001", "000001", "平安银行", "4"),),
        )
        self.assertEqual(result.overlap, Decimal("0"))
        self.assertEqual(result.shared, ())

    def test_period_selection_prefers_latest_common_period(self) -> None:
        old = date(2025, 12, 31)
        latest = date(2026, 3, 31)
        left_period, right_period, warnings = select_overlap_periods(
            (
                holding("519755", "600000", "浦发银行", "5", report_period=old),
                holding("519755", "600519", "贵州茅台", "5", report_period=latest),
            ),
            (
                holding("000001", "600000", "浦发银行", "5", report_period=old),
                holding("000001", "000001", "平安银行", "5", report_period=latest),
            ),
        )
        self.assertEqual((left_period, right_period), (latest, latest))
        self.assertEqual(warnings, ())

    def test_one_quarter_mismatch_is_allowed_but_larger_gap_is_rejected(self) -> None:
        left = (holding("519755", "600000", "浦发银行", "5", report_period=date(2026, 3, 31)),)
        adjacent = (
            holding(
                "000001", "600000", "浦发银行", "5", report_period=date(2025, 12, 31)
            ),
        )
        self.assertEqual(
            select_overlap_periods(left, adjacent),
            (date(2026, 3, 31), date(2025, 12, 31), ("report_period_mismatch",)),
        )
        too_old = (
            holding(
                "000001", "600000", "浦发银行", "5", report_period=date(2025, 9, 30)
            ),
        )
        with self.assertRaisesRegex(ValueError, "within one quarter"):
            select_overlap_periods(left, too_old)

    def test_missing_publication_date_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "publication date"):
            pairwise_overlap(
                "519755",
                "000001",
                (holding("519755", "600000", "浦发银行", "5", published_at=None),),
                (holding("000001", "600000", "浦发银行", "5"),),
            )

    def test_join_uses_codes_and_warns_when_names_differ(self) -> None:
        result = pairwise_overlap(
            "519755",
            "000001",
            (holding("519755", "600000", "浦发银行", "5"),),
            (
                holding("000001", "600000", "上海浦东发展银行", "2"),
                holding("000001", "000001", "浦发银行", "3"),
            ),
        )
        self.assertEqual(result.overlap, Decimal("2"))
        self.assertEqual(result.shared[0].exposure_code, "600000")
        self.assertIn("exposure_name_mismatch:stock:600000", result.warnings)

    def test_industry_overlap_requires_same_classification_standard(self) -> None:
        result, warnings = pairwise_industry_overlap(
            "519755",
            "000001",
            (industry("519755", "银行", "10", industry_code="J66"),),
            (industry("000001", "银行业", "6", industry_code="J66"),),
        )
        self.assertEqual(warnings, ("exposure_name_mismatch:industry:J66",))
        assert result is not None
        self.assertEqual(result.overlap, Decimal("6"))
        self.assertEqual(result.shared[0].exposure_type, "industry")

        result, warnings = pairwise_industry_overlap(
            "519755",
            "000001",
            (industry("519755", "银行", "10", standard="证监会行业分类"),),
            (industry("000001", "银行", "6", standard="申万一级"),),
        )
        self.assertIsNone(result)
        self.assertEqual(warnings, ("industry_classification_mismatch",))

        result, warnings = pairwise_industry_overlap(
            "519755",
            "000001",
            (
                industry(
                    "519755",
                    "银行",
                    "10",
                    standard="证监会行业分类",
                    report_period=date(2026, 3, 31),
                ),
            ),
            (
                industry(
                    "000001",
                    "银行",
                    "6",
                    standard="申万一级",
                    report_period=date(2025, 12, 31),
                ),
            ),
        )
        self.assertIsNone(result)
        self.assertEqual(
            warnings,
            ("report_period_mismatch", "industry_classification_mismatch"),
        )

    def test_industry_name_fallback_is_normalized(self) -> None:
        result, warnings = pairwise_industry_overlap(
            "519755",
            "000001",
            (industry("519755", "食品 饮料", "10"),),
            (industry("000001", "食品饮料", "4"),),
        )
        self.assertEqual(warnings, ())
        assert result is not None
        self.assertEqual(result.overlap, Decimal("4"))

    def test_portfolio_overlap_calculates_lookthrough_and_duplicate_contribution(self) -> None:
        result = portfolio_overlap(
            {"519755": Decimal("0.60"), "000001": Decimal("0.40")},
            {
                "519755": (holding("519755", "600000", "浦发银行", "10"),),
                "000001": (holding("000001", "600000", "浦发银行", "5"),),
            },
        )
        self.assertEqual(result["total_disclosed_security_exposure"], Decimal("0.08"))
        self.assertEqual(result["duplicated_contribution"], Decimal("0.02"))
        self.assertEqual(result["portfolio_weight_coverage"], Decimal("1.00"))
        self.assertEqual(result["disclosure_coverage"], Decimal("0.08"))
        exposure = result["securities"][0]
        self.assertEqual(exposure["total_weight"], Decimal("0.08"))
        self.assertEqual(exposure["duplicated_contribution"], Decimal("0.02"))
        self.assertEqual(len(exposure["contributors"]), 2)

    def test_portfolio_overlap_omits_missing_and_stale_inputs_without_zero_filling(self) -> None:
        result = portfolio_overlap(
            {"519755": Decimal("0.50"), "000001": Decimal("0.30")},
            {
                "519755": (holding("519755", "600000", "浦发银行", "10"),),
                "000001": (),
                "999999": (holding("999999", "600519", "贵州茅台", "10"),),
            },
            stale_codes=frozenset({"519755"}),
        )
        self.assertEqual(result["included_fund_codes"], ())
        self.assertEqual(result["omitted_fund_codes"], ("000001", "519755", "999999"))
        self.assertEqual(result["portfolio_weight_coverage"], Decimal("0"))
        self.assertEqual(result["disclosure_coverage"], Decimal("0"))
        self.assertIn("stale_holdings:519755", result["warnings"])
        self.assertIn("missing_holdings:000001", result["warnings"])
        self.assertIn("missing_portfolio_weight:999999", result["warnings"])

    def test_duplicate_accounts_use_one_aggregated_fund_weight(self) -> None:
        result = portfolio_overlap(
            {"519755": Decimal("0.75")},
            {"519755": (holding("519755", "600000", "浦发银行", "20"),)},
        )
        self.assertEqual(result["included_fund_codes"], ("519755",))
        self.assertEqual(result["portfolio_weight_coverage"], Decimal("0.75"))
        self.assertEqual(result["total_disclosed_security_exposure"], Decimal("0.15"))

    def test_portfolio_weight_total_cannot_exceed_one(self) -> None:
        with self.assertRaisesRegex(ValueError, "total.*cannot exceed one"):
            portfolio_overlap(
                {"519755": Decimal("0.60"), "000001": Decimal("0.50")},
                {
                    "519755": (holding("519755", "600000", "浦发银行", "10"),),
                    "000001": (holding("000001", "000001", "平安银行", "10"),),
                },
            )


if __name__ == "__main__":
    unittest.main()
