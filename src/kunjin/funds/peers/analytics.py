from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import AbstractSet, Dict, Mapping, Optional, Sequence, Tuple, cast

from kunjin.brief.nav import ValidatedAdjustedNavSeries
from kunjin.decision.models import (
    canonical_json_bytes,
    validate_checksum,
    validate_exact_dataclass_state,
)
from kunjin.funds.models import (
    FundHolding,
    FundIndustryExposure,
    FundManagerTenure,
    FundSizeObservation,
)
from kunjin.funds.peers.models import PairwiseOverlap, SharedExposure, WindowMetric
from kunjin.models import FundNavObservation

PEER_CALCULATION_VERSION = "1"
ADJUSTED_CORRELATION_VERSION = "1"
START_TOLERANCE_DAYS = 7
_ADJUSTED_CORRELATION_MAC_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class AdjustedReturnCorrelationResult:
    left_fund_code: str
    right_fund_code: str
    left_source_attempt_id: int
    right_source_attempt_id: int
    left_series_binding_mac: str
    right_series_binding_mac: str
    status: str
    correlation: Optional[Decimal]
    samples: int
    aligned_observations: int
    left_observations: int
    right_observations: int
    effective_start: Optional[date]
    effective_end: Optional[date]
    calculation_version: str
    insufficiency_codes: Tuple[str, ...]
    binding_mac: str

    def binding_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "aligned_observations": self.aligned_observations,
                "calculation_version": self.calculation_version,
                "correlation": self.correlation,
                "effective_end": self.effective_end,
                "effective_start": self.effective_start,
                "insufficiency_codes": self.insufficiency_codes,
                "left_fund_code": self.left_fund_code,
                "left_observations": self.left_observations,
                "left_series_binding_mac": self.left_series_binding_mac,
                "left_source_attempt_id": self.left_source_attempt_id,
                "right_fund_code": self.right_fund_code,
                "right_observations": self.right_observations,
                "right_series_binding_mac": self.right_series_binding_mac,
                "right_source_attempt_id": self.right_source_attempt_id,
                "samples": self.samples,
                "status": self.status,
            }
        )

    def validate(self) -> None:
        if type(self) is not AdjustedReturnCorrelationResult:
            raise ValueError("adjusted correlation result subclasses are not accepted")
        validate_exact_dataclass_state(self, "adjusted correlation result")
        validate_checksum(self.binding_mac, "adjusted correlation result binding MAC")
        if not hmac.compare_digest(self.binding_mac, _adjusted_correlation_mac(self)):
            raise ValueError("adjusted correlation result binding MAC does not match")
        if (
            type(self.left_fund_code) is not str
            or re.fullmatch(r"[0-9]{6}", self.left_fund_code) is None
            or type(self.right_fund_code) is not str
            or re.fullmatch(r"[0-9]{6}", self.right_fund_code) is None
            or self.left_fund_code == self.right_fund_code
        ):
            raise ValueError("adjusted correlation requires two distinct fund codes")
        for value, name in (
            (self.left_source_attempt_id, "left source attempt id"),
            (self.right_source_attempt_id, "right source attempt id"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"adjusted correlation {name} must be positive")
        validate_checksum(self.left_series_binding_mac, "left series binding MAC")
        validate_checksum(self.right_series_binding_mac, "right series binding MAC")
        if self.status not in {"success", "insufficient_data"}:
            raise ValueError("adjusted correlation status is invalid")
        for value, name in (
            (self.samples, "samples"),
            (self.aligned_observations, "aligned observations"),
            (self.left_observations, "left observations"),
            (self.right_observations, "right observations"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"adjusted correlation {name} must be non-negative")
        if self.aligned_observations and self.samples != self.aligned_observations - 1:
            raise ValueError("adjusted correlation sample and alignment counts differ")
        if (self.effective_start is None) != (self.effective_end is None):
            raise ValueError("adjusted correlation dates must be present together")
        if self.effective_start is not None and (
            type(self.effective_start) is not date
            or type(self.effective_end) is not date
            or self.effective_end < self.effective_start
        ):
            raise ValueError("adjusted correlation dates are invalid")
        if self.calculation_version != ADJUSTED_CORRELATION_VERSION:
            raise ValueError("adjusted correlation calculation version is invalid")
        if (
            type(self.insufficiency_codes) is not tuple
            or len(self.insufficiency_codes) != len(set(self.insufficiency_codes))
            or any(type(item) is not str or not item for item in self.insufficiency_codes)
        ):
            raise ValueError("adjusted correlation insufficiency codes are invalid")
        if self.status == "success":
            if (
                type(self.correlation) is not Decimal
                or not self.correlation.is_finite()
                or not Decimal("-1") <= self.correlation <= Decimal("1")
                or self.samples <= 0
                or self.effective_start is None
                or self.insufficiency_codes
            ):
                raise ValueError("successful adjusted correlation is incomplete")
        elif self.correlation is not None or not self.insufficiency_codes:
            raise ValueError("insufficient adjusted correlation has invalid conclusions")


def _adjusted_correlation_result(
    left: ValidatedAdjustedNavSeries,
    right: ValidatedAdjustedNavSeries,
    *,
    status: str,
    correlation: Optional[Decimal],
    samples: int,
    aligned_dates: Sequence[date],
    insufficiency_codes: Tuple[str, ...],
) -> AdjustedReturnCorrelationResult:
    result = AdjustedReturnCorrelationResult(
        left_fund_code=left.fund_code,
        right_fund_code=right.fund_code,
        left_source_attempt_id=left.source_attempt_id,
        right_source_attempt_id=right.source_attempt_id,
        left_series_binding_mac=left.binding_mac,
        right_series_binding_mac=right.binding_mac,
        status=status,
        correlation=correlation,
        samples=samples,
        aligned_observations=len(aligned_dates),
        left_observations=len(left.observations),
        right_observations=len(right.observations),
        effective_start=None if not aligned_dates else aligned_dates[0],
        effective_end=None if not aligned_dates else aligned_dates[-1],
        calculation_version=ADJUSTED_CORRELATION_VERSION,
        insufficiency_codes=insufficiency_codes,
        binding_mac="0" * 64,
    )
    result = replace(result, binding_mac=_adjusted_correlation_mac(result))
    result.validate()
    return result


def _adjusted_correlation_mac(value: AdjustedReturnCorrelationResult) -> str:
    return hmac.new(
        _ADJUSTED_CORRELATION_MAC_KEY,
        value.binding_bytes(),
        hashlib.sha256,
    ).hexdigest()


def decimal_adjusted_return_correlation(
    left: ValidatedAdjustedNavSeries,
    right: ValidatedAdjustedNavSeries,
    *,
    minimum_samples: int,
) -> AdjustedReturnCorrelationResult:
    if (
        type(left) is not ValidatedAdjustedNavSeries
        or type(right) is not ValidatedAdjustedNavSeries
    ):
        raise ValueError("adjusted correlation requires exact validated NAV series")
    if type(minimum_samples) is not int or minimum_samples <= 0:
        raise ValueError("minimum correlation samples must be a positive exact integer")
    if left.fund_code == right.fund_code:
        raise ValueError("adjusted correlation requires two distinct funds")

    issue_codes = {
        "duplicate_date": "adjusted_return_duplicate_date",
        "subject_mismatch": "adjusted_return_subject_mismatch",
        "accumulated_nav_unavailable": "adjusted_return_accumulated_nav_unavailable",
        "corporate_action_unresolved": "adjusted_return_corporate_action_unresolved",
        "discontinuity": "adjusted_return_discontinuity",
        "samples_insufficient": "adjusted_return_series_samples_insufficient",
        "source_binding_invalid": "adjusted_return_source_binding_invalid",
        "observation_invalid": "adjusted_return_observation_invalid",
        "date_order_invalid": "adjusted_return_date_order_invalid",
    }
    issues = []
    for side, series in (("left", left), ("right", right)):
        issue = series.validation_issue()
        if issue is not None:
            issues.append(f"{issue_codes.get(issue, 'adjusted_return_series_invalid')}_{side}")
    if issues:
        return _adjusted_correlation_result(
            left,
            right,
            status="insufficient_data",
            correlation=None,
            samples=0,
            aligned_dates=(),
            insufficiency_codes=tuple(issues),
        )
    left.validate()
    right.validate()

    left_by_date = {item.nav_date: item for item in left.observations}
    right_by_date = {item.nav_date: item for item in right.observations}
    common_start = max(min(left_by_date), min(right_by_date))
    common_end = min(max(left_by_date), max(right_by_date))
    if common_start <= common_end:
        left_window_dates = {item for item in left_by_date if common_start <= item <= common_end}
        right_window_dates = {item for item in right_by_date if common_start <= item <= common_end}
        if left_window_dates != right_window_dates:
            aligned_dates = tuple(sorted(left_window_dates & right_window_dates))
            return _adjusted_correlation_result(
                left,
                right,
                status="insufficient_data",
                correlation=None,
                samples=max(0, len(aligned_dates) - 1),
                aligned_dates=aligned_dates,
                insufficiency_codes=("adjusted_return_asymmetric_dates",),
            )
    aligned_dates = tuple(sorted(left_by_date.keys() & right_by_date.keys()))
    samples = max(0, len(aligned_dates) - 1)
    if samples < minimum_samples:
        return _adjusted_correlation_result(
            left,
            right,
            status="insufficient_data",
            correlation=None,
            samples=samples,
            aligned_dates=aligned_dates,
            insufficiency_codes=("adjusted_return_samples_insufficient",),
        )

    with localcontext() as context:
        context.prec = 28
        left_returns = tuple(
            left_by_date[newer].unit_nav / left_by_date[older].unit_nav - Decimal("1")
            for older, newer in zip(aligned_dates, aligned_dates[1:])
        )
        right_returns = tuple(
            right_by_date[newer].unit_nav / right_by_date[older].unit_nav - Decimal("1")
            for older, newer in zip(aligned_dates, aligned_dates[1:])
        )
        sample_count = Decimal(samples)
        left_mean = sum(left_returns, Decimal("0")) / sample_count
        right_mean = sum(right_returns, Decimal("0")) / sample_count
        left_variance_sum = sum(((item - left_mean) ** 2 for item in left_returns), Decimal("0"))
        right_variance_sum = sum(((item - right_mean) ** 2 for item in right_returns), Decimal("0"))
        zero_variance_codes = []
        if left_variance_sum == 0:
            zero_variance_codes.append("adjusted_return_zero_variance_left")
        if right_variance_sum == 0:
            zero_variance_codes.append("adjusted_return_zero_variance_right")
        if zero_variance_codes:
            return _adjusted_correlation_result(
                left,
                right,
                status="insufficient_data",
                correlation=None,
                samples=samples,
                aligned_dates=aligned_dates,
                insufficiency_codes=tuple(zero_variance_codes),
            )
        covariance_sum = sum(
            (
                (left_value - left_mean) * (right_value - right_mean)
                for left_value, right_value in zip(left_returns, right_returns)
            ),
            Decimal("0"),
        )
        denominator = (left_variance_sum * right_variance_sum).sqrt()
        correlation = covariance_sum / denominator
        if not correlation.is_finite() or correlation < Decimal("-1") or correlation > Decimal("1"):
            return _adjusted_correlation_result(
                left,
                right,
                status="insufficient_data",
                correlation=None,
                samples=samples,
                aligned_dates=aligned_dates,
                insufficiency_codes=("adjusted_return_correlation_invalid",),
            )

    return _adjusted_correlation_result(
        left,
        right,
        status="success",
        correlation=correlation,
        samples=samples,
        aligned_dates=aligned_dates,
        insufficiency_codes=(),
    )


def _quarter_ordinal(value: date) -> int:
    return value.year * 4 + (value.month - 1) // 3


def _selected_period_records(records: Sequence[object], report_period: date) -> list[object]:
    return [
        record
        for record in records
        if cast(date, getattr(record, "report_period")) == report_period
    ]


def _publication_date(records: Sequence[object]) -> datetime:
    published = {getattr(record, "published_at") for record in records}
    if None in published or not published:
        raise ValueError("selected disclosure is missing a publication date")
    if len(published) != 1:
        raise ValueError("selected disclosure has conflicting publication dates")
    return cast(datetime, next(iter(published)))


def select_overlap_periods(
    left: Sequence[object],
    right: Sequence[object],
) -> Tuple[date, date, Tuple[str, ...]]:
    left_periods = {cast(date, getattr(record, "report_period")) for record in left}
    right_periods = {cast(date, getattr(record, "report_period")) for record in right}
    if not left_periods or not right_periods:
        raise ValueError("overlap requires disclosures for both funds")

    common_periods = left_periods & right_periods
    if common_periods:
        selected = max(common_periods)
        return selected, selected, ()

    left_period = max(left_periods)
    right_period = max(right_periods)
    if abs(_quarter_ordinal(left_period) - _quarter_ordinal(right_period)) > 1:
        raise ValueError("latest disclosure periods must be within one quarter")
    return left_period, right_period, ("report_period_mismatch",)


def _holding_map(records: Sequence[FundHolding]) -> Dict[Tuple[str, str], FundHolding]:
    result: Dict[Tuple[str, str], FundHolding] = {}
    for record in records:
        key = (record.asset_type.value, record.security_code)
        if key in result:
            raise ValueError(f"duplicate holding exposure: {key[0]}:{key[1]}")
        result[key] = record
    return result


def pairwise_overlap(
    left_fund_code: str,
    right_fund_code: str,
    left: Sequence[FundHolding],
    right: Sequence[FundHolding],
) -> PairwiseOverlap:
    left_period, right_period, period_warnings = select_overlap_periods(left, right)
    selected_left = cast(Sequence[FundHolding], _selected_period_records(left, left_period))
    selected_right = cast(Sequence[FundHolding], _selected_period_records(right, right_period))
    left_published_at = _publication_date(selected_left)
    right_published_at = _publication_date(selected_right)
    left_by_key = _holding_map(selected_left)
    right_by_key = _holding_map(selected_right)

    warnings = list(period_warnings)
    shared = []
    for exposure_type, exposure_code in sorted(left_by_key.keys() & right_by_key.keys()):
        left_record = left_by_key[(exposure_type, exposure_code)]
        right_record = right_by_key[(exposure_type, exposure_code)]
        if left_record.security_name != right_record.security_name:
            warnings.append(f"exposure_name_mismatch:{exposure_type}:{exposure_code}")
        shared.append(
            SharedExposure(
                exposure_type=exposure_type,
                exposure_code=exposure_code,
                exposure_name=left_record.security_name,
                left_weight=left_record.weight,
                right_weight=right_record.weight,
                shared_weight=min(left_record.weight, right_record.weight),
            )
        )

    result = PairwiseOverlap(
        left_fund_code=left_fund_code,
        right_fund_code=right_fund_code,
        metric_name=(
            "top10_disclosed_overlap"
            if any(
                record.disclosure_scope == "top10" for record in (*selected_left, *selected_right)
            )
            else "disclosed_overlap"
        ),
        left_report_period=left_period,
        right_report_period=right_period,
        left_published_at=left_published_at,
        right_published_at=right_published_at,
        left_disclosed_weight=sum((record.weight for record in selected_left), Decimal("0")),
        right_disclosed_weight=sum((record.weight for record in selected_right), Decimal("0")),
        overlap=sum((exposure.shared_weight for exposure in shared), Decimal("0")),
        shared=tuple(shared),
        warnings=tuple(warnings),
    )
    result.validate()
    return result


def _normalize_industry_name(value: str) -> str:
    return re.sub(r"[\s\-_/（）()]+", "", value).casefold()


def _industry_key(record: FundIndustryExposure) -> str:
    if record.industry_code:
        return record.industry_code.strip()
    return _normalize_industry_name(record.industry_name)


def pairwise_industry_overlap(
    left_fund_code: str,
    right_fund_code: str,
    left: Sequence[FundIndustryExposure],
    right: Sequence[FundIndustryExposure],
) -> Tuple[Optional[PairwiseOverlap], Tuple[str, ...]]:
    left_period, right_period, period_warnings = select_overlap_periods(left, right)
    selected_left = cast(
        Sequence[FundIndustryExposure], _selected_period_records(left, left_period)
    )
    selected_right = cast(
        Sequence[FundIndustryExposure], _selected_period_records(right, right_period)
    )
    left_standards = {record.classification_standard for record in selected_left}
    right_standards = {record.classification_standard for record in selected_right}
    if len(left_standards) != 1 or len(right_standards) != 1:
        raise ValueError("selected industry disclosures require one classification standard")
    if left_standards != right_standards:
        return None, period_warnings + ("industry_classification_mismatch",)

    left_published_at = _publication_date(selected_left)
    right_published_at = _publication_date(selected_right)
    left_by_key = {_industry_key(record): record for record in selected_left}
    right_by_key = {_industry_key(record): record for record in selected_right}
    if len(left_by_key) != len(selected_left) or len(right_by_key) != len(selected_right):
        raise ValueError("duplicate industry exposure")

    warnings = list(period_warnings)
    shared = []
    for exposure_code in sorted(left_by_key.keys() & right_by_key.keys()):
        left_record = left_by_key[exposure_code]
        right_record = right_by_key[exposure_code]
        if _normalize_industry_name(left_record.industry_name) != _normalize_industry_name(
            right_record.industry_name
        ):
            warnings.append(f"exposure_name_mismatch:industry:{exposure_code}")
        shared.append(
            SharedExposure(
                exposure_type="industry",
                exposure_code=exposure_code,
                exposure_name=left_record.industry_name,
                left_weight=left_record.weight,
                right_weight=right_record.weight,
                shared_weight=min(left_record.weight, right_record.weight),
            )
        )

    result = PairwiseOverlap(
        left_fund_code=left_fund_code,
        right_fund_code=right_fund_code,
        metric_name="industry_disclosed_overlap",
        left_report_period=left_period,
        right_report_period=right_period,
        left_published_at=left_published_at,
        right_published_at=right_published_at,
        left_disclosed_weight=sum((record.weight for record in selected_left), Decimal("0")),
        right_disclosed_weight=sum((record.weight for record in selected_right), Decimal("0")),
        overlap=sum((exposure.shared_weight for exposure in shared), Decimal("0")),
        shared=tuple(shared),
        warnings=tuple(warnings),
    )
    result.validate()
    return result, result.warnings


def portfolio_overlap(
    portfolio_weights: Mapping[str, Decimal],
    holdings_by_fund: Mapping[str, Sequence[FundHolding]],
    stale_codes: AbstractSet[str] = frozenset(),
) -> Dict[str, object]:
    total_portfolio_weight = sum(portfolio_weights.values(), Decimal("0"))
    if total_portfolio_weight > 1:
        raise ValueError("portfolio weight total cannot exceed one")

    warnings = []
    omitted = set()
    included = []
    report_periods: Dict[str, date] = {}
    exposures: Dict[Tuple[str, str], Dict[str, object]] = {}
    portfolio_weight_coverage = Decimal("0")

    for fund_code in sorted(set(portfolio_weights) | set(holdings_by_fund)):
        portfolio_weight = portfolio_weights.get(fund_code)
        if portfolio_weight is None:
            omitted.add(fund_code)
            warnings.append(f"missing_portfolio_weight:{fund_code}")
            continue
        if portfolio_weight < 0 or portfolio_weight > 1:
            raise ValueError(f"portfolio weight must be a fraction: {fund_code}")
        if fund_code in stale_codes:
            omitted.add(fund_code)
            warnings.append(f"stale_holdings:{fund_code}")
            continue
        holdings = holdings_by_fund.get(fund_code, ())
        if not holdings:
            omitted.add(fund_code)
            warnings.append(f"missing_holdings:{fund_code}")
            continue

        latest_period = max(record.report_period for record in holdings)
        selected = [record for record in holdings if record.report_period == latest_period]
        included.append(fund_code)
        portfolio_weight_coverage += portfolio_weight
        report_periods[fund_code] = latest_period
        for record in selected:
            key = (record.asset_type.value, record.security_code)
            lookthrough_weight = portfolio_weight * record.weight / Decimal("100")
            exposure = exposures.setdefault(
                key,
                {
                    "exposure_type": record.asset_type.value,
                    "security_code": record.security_code,
                    "security_name": record.security_name,
                    "contributors": [],
                },
            )
            if exposure["security_name"] != record.security_name:
                warnings.append(
                    f"exposure_name_mismatch:{record.asset_type.value}:{record.security_code}"
                )
            cast(list, exposure["contributors"]).append(
                {
                    "fund_code": fund_code,
                    "portfolio_weight": portfolio_weight,
                    "disclosed_weight": record.weight,
                    "lookthrough_weight": lookthrough_weight,
                    "report_period": latest_period,
                }
            )

    securities = []
    total_exposure = Decimal("0")
    duplicated_contribution = Decimal("0")
    for key in sorted(exposures):
        exposure = exposures[key]
        contributors = cast(list, exposure["contributors"])
        weights = [cast(Decimal, contributor["lookthrough_weight"]) for contributor in contributors]
        total_weight = sum(weights, Decimal("0"))
        duplicated = total_weight - max(weights)
        total_exposure += total_weight
        duplicated_contribution += duplicated
        securities.append(
            {
                **exposure,
                "contributors": tuple(contributors),
                "total_weight": total_weight,
                "duplicated_contribution": duplicated,
            }
        )

    return {
        "securities": tuple(securities),
        "total_disclosed_security_exposure": total_exposure,
        "duplicated_contribution": duplicated_contribution,
        "included_fund_codes": tuple(included),
        "omitted_fund_codes": tuple(sorted(omitted)),
        "portfolio_weight_coverage": portfolio_weight_coverage,
        "disclosure_coverage": total_exposure,
        "report_periods": report_periods,
        "warnings": tuple(warnings),
    }


def common_end_date(
    histories: Mapping[str, Sequence[FundNavObservation]],
) -> Optional[date]:
    if not histories:
        return None

    shared_dates: Optional[set[date]] = None
    for history in histories.values():
        if not history:
            return None
        dates = {observation.nav_date for observation in history}
        shared_dates = dates if shared_dates is None else shared_dates & dates
        if not shared_dates:
            return None
    return max(shared_dates) if shared_dates else None


def calculate_window_metric(
    fund_code: str,
    history: Sequence[FundNavObservation],
    window: str,
    target_start: date,
    effective_end: date,
) -> Tuple[Optional[WindowMetric], Tuple[str, ...]]:
    unavailable = ("aligned_nav_window_unavailable",)
    if target_start > effective_end:
        return None, unavailable

    by_date = {
        observation.nav_date: observation
        for observation in history
        if observation.fund_code == fund_code and observation.nav_date <= effective_end
    }
    end_observation = by_date.get(effective_end)
    if end_observation is None:
        return None, unavailable

    earliest_baseline = target_start - timedelta(days=START_TOLERANCE_DAYS)
    baseline_dates = [
        nav_date for nav_date in by_date if earliest_baseline <= nav_date <= target_start
    ]
    if not baseline_dates:
        return None, unavailable

    effective_start = max(baseline_dates)
    ordered = [
        by_date[nav_date]
        for nav_date in sorted(by_date)
        if effective_start <= nav_date <= effective_end
    ]
    if not ordered:
        return None, unavailable

    daily_returns = [
        ordered[index].unit_nav / ordered[index - 1].unit_nav - Decimal("1")
        for index in range(1, len(ordered))
    ]
    annualized_volatility: Optional[Decimal] = None
    if daily_returns:
        mean = sum(daily_returns, Decimal("0")) / Decimal(len(daily_returns))
        variance = sum((daily_return - mean) ** 2 for daily_return in daily_returns) / Decimal(
            len(daily_returns)
        )
        annualized_volatility = variance.sqrt() * Decimal(252).sqrt()

    peak_nav = ordered[0].unit_nav
    peak_date = ordered[0].nav_date
    drawdown_peak_nav = peak_nav
    drawdown_peak_date = peak_date
    max_drawdown = Decimal("0")
    trough_date = ordered[0].nav_date
    for observation in ordered:
        if observation.unit_nav > peak_nav:
            peak_nav = observation.unit_nav
            peak_date = observation.nav_date
        drawdown = Decimal("1") - observation.unit_nav / peak_nav
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            drawdown_peak_nav = peak_nav
            drawdown_peak_date = peak_date
            trough_date = observation.nav_date

    recovery_date = None
    if max_drawdown > 0:
        recovery_date = next(
            (
                observation.nav_date
                for observation in ordered
                if observation.nav_date > trough_date and observation.unit_nav >= drawdown_peak_nav
            ),
            None,
        )

    metric = WindowMetric(
        fund_code=fund_code,
        window=window,
        effective_start=effective_start,
        effective_end=effective_end,
        observations=len(ordered),
        total_return=end_observation.unit_nav / ordered[0].unit_nav - Decimal("1"),
        annualized_volatility=annualized_volatility,
        max_drawdown=max_drawdown,
        drawdown_peak_date=drawdown_peak_date,
        trough_date=trough_date,
        recovery_date=recovery_date,
    )
    metric.validate()
    return metric, ()


def current_manager_team_start(
    tenures: Sequence[FundManagerTenure],
    as_of: date,
) -> Optional[date]:
    active_starts = [
        tenure.start_date
        for tenure in tenures
        if tenure.start_date <= as_of and (tenure.end_date is None or tenure.end_date >= as_of)
    ]
    return max(active_starts) if active_starts else None


def calculate_size_stability(
    observations: Sequence[FundSizeObservation],
) -> Dict[str, object]:
    by_date = {
        observation.report_date: observation
        for observation in observations
        if observation.net_assets is not None and observation.net_assets > 0
    }
    ordered = [by_date[report_date] for report_date in sorted(by_date)][-5:]
    if len(ordered) < 3:
        return {
            "evidence_level": "insufficient_data",
            "observations": len(ordered),
        }

    known_assets = [cast(Decimal, observation.net_assets) for observation in ordered]
    quarterly_changes = [
        known_assets[index] / known_assets[index - 1] - Decimal("1")
        for index in range(1, len(known_assets))
    ]
    mean = sum(quarterly_changes, Decimal("0")) / Decimal(len(quarterly_changes))
    variance = sum(
        (quarterly_change - mean) ** 2 for quarterly_change in quarterly_changes
    ) / Decimal(len(quarterly_changes))

    return {
        "evidence_level": "deterministic_calculation",
        "observations": len(ordered),
        "earliest_report_date": ordered[0].report_date,
        "latest_report_date": ordered[-1].report_date,
        "earliest_net_assets": known_assets[0],
        "latest_net_assets": known_assets[-1],
        "net_asset_change": known_assets[-1] / known_assets[0] - Decimal("1"),
        "quarterly_change_pstdev": variance.sqrt(),
    }
