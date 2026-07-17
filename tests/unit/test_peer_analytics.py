from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from kunjin.brief.nav import (
    ValidatedAdjustedNavSeries,
    _seal_validated_adjusted_nav_series,
)
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
    decimal_adjusted_return_correlation,
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


def adjusted_series(
    fund_code: str,
    returns: tuple[Decimal, ...],
    *,
    start: date = date(2026, 1, 1),
    source_attempt_id: int = 1,
    accumulated_offset: Decimal = Decimal("0"),
) -> ValidatedAdjustedNavSeries:
    value = Decimal("1")
    observations = [
        FundNavObservation(
            fund_code=fund_code,
            nav_date=start,
            unit_nav=value,
            accumulated_nav=value + accumulated_offset,
            daily_growth=Decimal("0"),
            source="eastmoney",
            retrieved_at=NOW,
            corporate_action_state="none",
            source_attempt_id=source_attempt_id,
        )
    ]
    for offset, daily_return in enumerate(returns, start=1):
        value *= Decimal("1") + daily_return
        observations.append(
            FundNavObservation(
                fund_code=fund_code,
                nav_date=start + timedelta(days=offset),
                unit_nav=value,
                accumulated_nav=value + accumulated_offset,
                daily_growth=daily_return * Decimal("100"),
                source="eastmoney",
                retrieved_at=NOW,
                corporate_action_state="none",
                source_attempt_id=source_attempt_id,
            )
        )
    return _seal_validated_adjusted_nav_series(
        fund_code=fund_code,
        observations=tuple(observations),
        source_attempt_id=source_attempt_id,
        retrieved_at=NOW,
        data_as_of=observations[-1].nav_date,
    )


def reseal_series(
    series: ValidatedAdjustedNavSeries,
    observations: tuple[FundNavObservation, ...],
    *,
    data_as_of: date | None = None,
) -> ValidatedAdjustedNavSeries:
    return _seal_validated_adjusted_nav_series(
        fund_code=series.fund_code,
        observations=observations,
        source_attempt_id=series.source_attempt_id,
        retrieved_at=series.retrieved_at,
        data_as_of=observations[-1].nav_date if data_as_of is None else data_as_of,
    )


class AdjustedReturnCorrelationTest(unittest.TestCase):
    def test_sixty_aligned_return_samples_are_decimal_and_preserve_common_window(self) -> None:
        left_returns = tuple(
            Decimal("0.001") if index % 2 == 0 else Decimal("0.003") for index in range(60)
        )
        left = adjusted_series("519755", left_returns)
        right = adjusted_series("000001", left_returns, source_attempt_id=2)

        result = decimal_adjusted_return_correlation(left, right, minimum_samples=60)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.correlation, Decimal("1"))
        self.assertEqual(result.samples, 60)
        self.assertEqual(result.aligned_observations, 61)
        self.assertEqual(result.left_observations, 61)
        self.assertEqual(result.right_observations, 61)
        self.assertEqual(result.effective_start, date(2026, 1, 1))
        self.assertEqual(result.effective_end, date(2026, 3, 2))
        self.assertEqual(result.calculation_version, "1")
        self.assertEqual(result.insufficiency_codes, ())
        self.assertIsInstance(result.correlation, Decimal)

    def test_fifty_nine_returns_from_sixty_levels_are_insufficient(self) -> None:
        returns = tuple(
            Decimal("0.001") if index % 2 == 0 else Decimal("0.002") for index in range(59)
        )
        result = decimal_adjusted_return_correlation(
            adjusted_series("519755", returns),
            adjusted_series("000001", returns, source_attempt_id=2),
            minimum_samples=60,
        )

        self.assertEqual(result.status, "insufficient_data")
        self.assertIsNone(result.correlation)
        self.assertEqual(result.samples, 59)
        self.assertIn("adjusted_return_samples_insufficient", result.insufficiency_codes)

    def test_perfect_negative_and_nontrivial_decimal_vectors(self) -> None:
        left_returns = tuple(
            Decimal("0.01") if index % 2 == 0 else Decimal("0.02") for index in range(60)
        )
        negative_returns = tuple(Decimal("0.03") - item for item in left_returns)
        negative = decimal_adjusted_return_correlation(
            adjusted_series("519755", left_returns),
            adjusted_series("000001", negative_returns, source_attempt_id=2),
            minimum_samples=60,
        )
        self.assertEqual(negative.status, "success")
        self.assertEqual(negative.correlation, Decimal("-1"))

        left_cycle = (Decimal("0.01"), Decimal("0.02"), Decimal("0.03")) * 20
        right_cycle = (Decimal("0.03"), Decimal("0.01"), Decimal("0.02")) * 20
        nontrivial = decimal_adjusted_return_correlation(
            adjusted_series("519755", left_cycle),
            adjusted_series("000001", right_cycle, source_attempt_id=2),
            minimum_samples=60,
        )
        self.assertEqual(nontrivial.status, "success")
        self.assertEqual(nontrivial.correlation, Decimal("-0.5"))

    def test_constant_accumulated_nav_offset_does_not_distort_validated_unit_returns(self) -> None:
        returns = tuple(
            Decimal("0.001") if index % 2 == 0 else Decimal("0.003") for index in range(60)
        )
        result = decimal_adjusted_return_correlation(
            adjusted_series("519755", returns),
            adjusted_series(
                "000001",
                returns,
                source_attempt_id=2,
                accumulated_offset=Decimal("5"),
            ),
            minimum_samples=60,
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.correlation, Decimal("1"))

    def test_unsealed_or_tampered_series_cannot_support_correlation(self) -> None:
        returns = (Decimal("0.01"), Decimal("0.02")) * 30
        valid_left = adjusted_series("519755", returns)
        valid_right = adjusted_series("000001", returns, source_attempt_id=2)
        tampered = replace(
            valid_right,
            observations=(
                replace(valid_right.observations[0], unit_nav=Decimal("9")),
                *valid_right.observations[1:],
            ),
        )

        result = decimal_adjusted_return_correlation(
            valid_left,
            tampered,
            minimum_samples=60,
        )

        self.assertEqual(result.status, "insufficient_data")
        self.assertIn(
            "adjusted_return_source_binding_invalid_right",
            result.insufficiency_codes,
        )

    def test_correlation_result_rejects_hidden_state(self) -> None:
        returns = (Decimal("0.01"), Decimal("0.02")) * 30
        result = decimal_adjusted_return_correlation(
            adjusted_series("519755", returns),
            adjusted_series("000001", returns, source_attempt_id=2),
            minimum_samples=60,
        )
        object.__setattr__(result, "hidden_override", "forged")

        with self.assertRaisesRegex(ValueError, "unexpected instance state"):
            result.validate()

        clean = decimal_adjusted_return_correlation(
            adjusted_series("519755", returns),
            adjusted_series("000001", returns, source_attempt_id=2),
            minimum_samples=60,
        )
        with self.assertRaisesRegex(ValueError, "binding MAC"):
            replace(clean, correlation=Decimal("-1")).validate()

    def test_alignment_uses_shared_levels_and_latest_common_end(self) -> None:
        returns = tuple(
            Decimal("0.001") if index % 2 == 0 else Decimal("0.003") for index in range(62)
        )
        left = adjusted_series("519755", returns)
        right_full = adjusted_series("000001", returns, source_attempt_id=2)
        right = reseal_series(
            right_full,
            tuple(
                item for index, item in enumerate(right_full.observations) if index not in {10, 62}
            ),
            data_as_of=right_full.observations[-2].nav_date,
        )

        result = decimal_adjusted_return_correlation(left, right, minimum_samples=60)

        self.assertEqual(result.status, "insufficient_data")
        self.assertIn("adjusted_return_asymmetric_dates", result.insufficiency_codes)

        bounded_right = reseal_series(
            right_full,
            right_full.observations[1:-1],
        )
        bounded = decimal_adjusted_return_correlation(
            left,
            bounded_right,
            minimum_samples=60,
        )
        self.assertEqual(bounded.status, "success")
        self.assertEqual(bounded.samples, 60)
        self.assertEqual(bounded.aligned_observations, 61)
        self.assertEqual(bounded.effective_start, date(2026, 1, 2))
        self.assertEqual(bounded.effective_end, date(2026, 3, 3))

    def test_duplicate_date_and_subject_mismatch_fail_closed(self) -> None:
        returns = (Decimal("0.01"), Decimal("0.02")) * 30
        valid_left = adjusted_series("519755", returns)
        valid_right = adjusted_series("000001", returns, source_attempt_id=2)
        duplicate = reseal_series(
            valid_left,
            valid_left.observations + (valid_left.observations[-1],),
        )
        duplicate_result = decimal_adjusted_return_correlation(
            duplicate,
            valid_right,
            minimum_samples=60,
        )
        self.assertEqual(duplicate_result.status, "insufficient_data")
        self.assertIn("adjusted_return_duplicate_date_left", duplicate_result.insufficiency_codes)

        wrong_subject = reseal_series(
            valid_left,
            (
                replace(valid_left.observations[0], fund_code="999999"),
                *valid_left.observations[1:],
            ),
        )
        subject_result = decimal_adjusted_return_correlation(
            wrong_subject,
            valid_right,
            minimum_samples=60,
        )
        self.assertEqual(subject_result.status, "insufficient_data")
        self.assertIn("adjusted_return_subject_mismatch_left", subject_result.insufficiency_codes)

    def test_missing_accumulated_nav_or_corporate_action_fails_closed(self) -> None:
        returns = (Decimal("0.01"), Decimal("0.02")) * 30
        valid_left = adjusted_series("519755", returns)
        valid_right = adjusted_series("000001", returns, source_attempt_id=2)
        missing_accumulated = reseal_series(
            valid_left,
            (
                *valid_left.observations[:30],
                replace(valid_left.observations[30], accumulated_nav=None),
                *valid_left.observations[31:],
            ),
        )
        accumulated_result = decimal_adjusted_return_correlation(
            missing_accumulated,
            valid_right,
            minimum_samples=60,
        )
        self.assertEqual(accumulated_result.status, "insufficient_data")
        self.assertIn(
            "adjusted_return_accumulated_nav_unavailable_left",
            accumulated_result.insufficiency_codes,
        )

        for action_state in ("present", "unknown"):
            with self.subTest(action_state=action_state):
                action_series = reseal_series(
                    valid_left,
                    (
                        *valid_left.observations[:30],
                        replace(
                            valid_left.observations[30],
                            corporate_action_state=action_state,
                        ),
                        *valid_left.observations[31:],
                    ),
                )
                action_result = decimal_adjusted_return_correlation(
                    action_series,
                    valid_right,
                    minimum_samples=60,
                )
                self.assertEqual(action_result.status, "insufficient_data")
                self.assertIn(
                    "adjusted_return_corporate_action_unresolved_left",
                    action_result.insufficiency_codes,
                )

    def test_accumulated_nav_breakpoint_or_growth_sign_conflict_fails_closed(self) -> None:
        returns = (Decimal("0.01"), Decimal("0.02")) * 30
        valid_left = adjusted_series("519755", returns)
        valid_right = adjusted_series("000001", returns, source_attempt_id=2)
        discontinuous_rows = (
            (
                *valid_left.observations[:30],
                replace(
                    valid_left.observations[30],
                    accumulated_nav=valid_left.observations[30].accumulated_nav + Decimal("0.01"),
                ),
                *valid_left.observations[31:],
            ),
            (
                *valid_left.observations[:30],
                replace(valid_left.observations[30], daily_growth=Decimal("-1")),
                *valid_left.observations[31:],
            ),
        )
        for observations in discontinuous_rows:
            with self.subTest(observations=observations[30]):
                result = decimal_adjusted_return_correlation(
                    reseal_series(valid_left, observations),
                    valid_right,
                    minimum_samples=60,
                )
                self.assertEqual(result.status, "insufficient_data")
                self.assertIsNone(result.correlation)
                self.assertIn(
                    "adjusted_return_discontinuity_left",
                    result.insufficiency_codes,
                )

    def test_zero_variance_on_either_side_is_insufficient(self) -> None:
        variable = (Decimal("0.01"), Decimal("0.02")) * 30
        constant = (Decimal("0.01"),) * 60
        for left_returns, right_returns, expected_code in (
            (constant, variable, "adjusted_return_zero_variance_left"),
            (variable, constant, "adjusted_return_zero_variance_right"),
        ):
            with self.subTest(expected_code=expected_code):
                result = decimal_adjusted_return_correlation(
                    adjusted_series("519755", left_returns),
                    adjusted_series("000001", right_returns, source_attempt_id=2),
                    minimum_samples=60,
                )
                self.assertEqual(result.status, "insufficient_data")
                self.assertIsNone(result.correlation)
                self.assertIn(expected_code, result.insufficiency_codes)


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
            sum((item - mean) ** 2 for item in daily_returns) / Decimal(len(daily_returns))
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
        adjacent = (holding("000001", "600000", "浦发银行", "5", report_period=date(2025, 12, 31)),)
        self.assertEqual(
            select_overlap_periods(left, adjacent),
            (date(2026, 3, 31), date(2025, 12, 31), ("report_period_mismatch",)),
        )
        too_old = (holding("000001", "600000", "浦发银行", "5", report_period=date(2025, 9, 30)),)
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
