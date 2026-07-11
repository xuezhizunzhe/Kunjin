from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.funds.models import (
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundFeeRule,
    FundIndustryExposure,
    SourceDocument,
)
from kunjin.funds.research import build_disclosure_report
from kunjin.funds.peers.analytics import (
    PEER_CALCULATION_VERSION,
    calculate_size_stability,
    calculate_window_metric,
    common_end_date,
    current_manager_team_start,
    pairwise_industry_overlap,
    pairwise_overlap,
    portfolio_overlap,
)
from kunjin.funds.peers.classification import PEER_RULE_VERSION, classify_peer
from kunjin.funds.peers.models import PairwiseOverlap, PeerGroup, WindowMetric
from kunjin.models import FundNavObservation, StoredPosition


WINDOW_DAYS = {"90d": 90, "365d": 365}
ONGOING_FEE_TYPES = {
    FeeType.MANAGEMENT,
    FeeType.CUSTODY,
    FeeType.SALES_SERVICE,
}
FORBIDDEN_REPORT_KEYS = {"score", "overall_score", "recommendation", "buy", "sell"}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal values are not valid report data")
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return [_json_value(item) for item in sorted(value)]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def comparison_fingerprint(payload: Mapping[str, object]) -> str:
    """Return a stable identity for the exact deterministic comparison inputs."""

    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def _require_aware(as_of: datetime) -> None:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")


def _fee_payload(rule: FundFeeRule) -> Dict[str, object]:
    return {
        "fee_type": rule.fee_type.value,
        "share_class": rule.share_class,
        "rate": rule.rate,
        "fixed_amount": rule.fixed_amount,
        "amount_min": rule.amount_min,
        "amount_max": rule.amount_max,
        "holding_days_min": rule.holding_days_min,
        "holding_days_max": rule.holding_days_max,
        "rule_order": rule.rule_order,
        "effective_from": rule.effective_from,
        "effective_to": rule.effective_to,
        "raw_rule_text": rule.raw_rule_text,
        "source_document_id": rule.source_document_id,
    }


def _current_rule(rule: FundFeeRule, as_of: date) -> bool:
    return (rule.effective_from is None or rule.effective_from <= as_of) and (
        rule.effective_to is None or rule.effective_to >= as_of
    )


def _position_share_classes(positions: Sequence[StoredPosition]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for position in positions:
        if position.share_class is not None:
            result[position.fund_code] = position.share_class
    return result


def _applicable_share_class(
    code: str,
    bundle: DisclosureBundle,
    position_classes: Mapping[str, str],
) -> Optional[str]:
    known = position_classes.get(code)
    if known is not None:
        return known
    if bundle.identity is not None:
        name = bundle.identity.fund_name.strip().upper()
        if name.endswith("A") or name.endswith("C"):
            return name[-1]
    classes = {rule.share_class for rule in bundle.fee_rules if rule.share_class}
    return next(iter(classes)) if len(classes) == 1 else None


def _fee_sections(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    positions: Sequence[StoredPosition],
    as_of: date,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    raw: Dict[str, object] = {}
    ongoing: Dict[str, object] = {}
    position_classes = _position_share_classes(positions)
    for code in codes:
        bundle = bundles.get(code)
        if bundle is None:
            raw[code] = []
            ongoing[code] = None
            continue
        ordered = sorted(
            bundle.fee_rules,
            key=lambda rule: (
                rule.fee_type.value,
                rule.share_class or "",
                rule.rule_order,
                rule.amount_min if rule.amount_min is not None else Decimal("-1"),
                rule.holding_days_min if rule.holding_days_min is not None else -1,
            ),
        )
        raw[code] = [_fee_payload(rule) for rule in ordered]
        share_class = _applicable_share_class(code, bundle, position_classes)
        applicable = [
            rule
            for rule in bundle.fee_rules
            if rule.fee_type in ONGOING_FEE_TYPES
            and rule.rate is not None
            and rule.fixed_amount is None
            and _current_rule(rule, as_of)
            and (rule.share_class is None or rule.share_class == share_class)
        ]
        ongoing[code] = (
            sum((rule.rate for rule in applicable if rule.rate is not None), Decimal("0"))
            if applicable
            else None
        )
    return raw, ongoing


def _metric_payload(metric: WindowMetric) -> Dict[str, object]:
    return {
        "fund_code": metric.fund_code,
        "window": metric.window,
        "effective_start": metric.effective_start,
        "effective_end": metric.effective_end,
        "observations": metric.observations,
        "total_return": metric.total_return,
        "annualized_volatility": metric.annualized_volatility,
        "max_drawdown": metric.max_drawdown,
        "drawdown_peak_date": metric.drawdown_peak_date,
        "trough_date": metric.trough_date,
        "recovery_date": metric.recovery_date,
    }


def _metric_orderings(metrics: Sequence[WindowMetric]) -> Dict[str, object]:
    definitions = (
        ("total_return", "higher", True),
        ("annualized_volatility", "lower", False),
        ("max_drawdown", "lower", False),
    )
    result: Dict[str, object] = {}
    for metric_name, direction, reverse in definitions:
        known = [metric for metric in metrics if getattr(metric, metric_name) is not None]
        if not known:
            continue
        ordered = sorted(known, key=lambda metric: metric.fund_code)
        ordered = sorted(
            ordered, key=lambda metric: getattr(metric, metric_name), reverse=reverse
        )
        result[metric_name] = {
            "metric": metric_name,
            "window": metrics[0].window,
            "direction": direction,
            "fund_codes": [metric.fund_code for metric in ordered],
            "values": {metric.fund_code: getattr(metric, metric_name) for metric in ordered},
        }
    return result


def _window_sections(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    as_of: date,
) -> Tuple[Dict[str, object], Dict[str, object], Dict[str, int], List[str], Dict[str, object]]:
    windows: Dict[str, object] = {"90d": [], "365d": [], "manager_tenure": []}
    orderings: Dict[str, object] = {"90d": {}, "365d": {}, "manager_tenure": {}}
    coverage = {
        "members_with_90d_nav": 0,
        "members_with_365d_nav": 0,
        "members_with_manager_tenure_nav": 0,
    }
    warnings: List[str] = []
    data_dates: Dict[str, object] = {}
    available = {code: histories[code] for code in codes if histories.get(code)}
    effective_end = common_end_date(available)
    if effective_end is None:
        if available:
            warnings.append("aligned_nav_window_unavailable")
        return windows, orderings, coverage, warnings, data_dates
    data_dates["common_nav_end"] = effective_end

    for window, days in WINDOW_DAYS.items():
        metrics: List[WindowMetric] = []
        for code in codes:
            metric, metric_warnings = calculate_window_metric(
                code,
                histories.get(code, ()),
                window,
                effective_end - timedelta(days=days),
                effective_end,
            )
            if metric is not None:
                metrics.append(metric)
            elif metric_warnings:
                warnings.append(f"{metric_warnings[0]}:{window}:{code}")
        windows[window] = [_metric_payload(metric) for metric in metrics]
        orderings[window] = _metric_orderings(metrics)
        coverage[f"members_with_{window}_nav"] = len(metrics)

    manager_metrics: List[WindowMetric] = []
    starts: Dict[str, date] = {}
    for code in codes:
        bundle = bundles.get(code)
        start = (
            None
            if bundle is None
            else current_manager_team_start(bundle.manager_tenures, as_of)
        )
        if start is None:
            warnings.append(f"manager_tenure_history_insufficient:{code}")
            continue
        starts[code] = start
        metric, metric_warnings = calculate_window_metric(
            code,
            histories.get(code, ()),
            "manager_tenure",
            start,
            effective_end,
        )
        if metric is not None:
            manager_metrics.append(metric)
        elif metric_warnings:
            warnings.append(f"{metric_warnings[0]}:manager_tenure:{code}")
    windows["manager_tenure"] = [_metric_payload(metric) for metric in manager_metrics]
    coverage["members_with_manager_tenure_nav"] = len(manager_metrics)
    data_dates["manager_team_starts"] = starts
    metric_starts = {metric.effective_start for metric in manager_metrics}
    if len(metric_starts) == 1:
        orderings["manager_tenure"] = _metric_orderings(manager_metrics)
    elif len(metric_starts) > 1:
        warnings.append("manager_tenure_start_dates_differ")
    return windows, orderings, coverage, warnings, data_dates


def _overlap_payload(result: PairwiseOverlap) -> Dict[str, object]:
    return {
        "left_fund_code": result.left_fund_code,
        "right_fund_code": result.right_fund_code,
        "metric_name": result.metric_name,
        "left_report_period": result.left_report_period,
        "right_report_period": result.right_report_period,
        "left_published_at": result.left_published_at,
        "right_published_at": result.right_published_at,
        "left_disclosed_weight": result.left_disclosed_weight,
        "right_disclosed_weight": result.right_disclosed_weight,
        "overlap": result.overlap,
        "shared": [
            {
                "exposure_type": item.exposure_type,
                "exposure_code": item.exposure_code,
                "exposure_name": item.exposure_name,
                "left_weight": item.left_weight,
                "right_weight": item.right_weight,
                "shared_weight": item.shared_weight,
            }
            for item in result.shared
        ],
        "warnings": list(result.warnings),
    }


def _pairwise_sections(
    codes: Sequence[str], bundles: Mapping[str, DisclosureBundle]
) -> Tuple[List[object], List[str]]:
    reports: List[object] = []
    warnings: List[str] = []
    for left_code, right_code in combinations(codes, 2):
        left = bundles.get(left_code)
        right = bundles.get(right_code)
        if left is None or right is None:
            warnings.append(f"holdings_unavailable:{left_code}:{right_code}")
            continue
        item: Dict[str, object] = {
            "left_fund_code": left_code,
            "right_fund_code": right_code,
            "security": None,
            "industry": None,
        }
        try:
            security = pairwise_overlap(left_code, right_code, left.holdings, right.holdings)
            item["security"] = _overlap_payload(security)
            warnings.extend(security.warnings)
        except ValueError:
            warnings.append(f"holdings_unavailable:{left_code}:{right_code}")
        try:
            industry, industry_warnings = pairwise_industry_overlap(
                left_code, right_code, left.industry_exposure, right.industry_exposure
            )
            item["industry"] = None if industry is None else _overlap_payload(industry)
            warnings.extend(industry_warnings)
        except ValueError:
            warnings.append(f"industry_holdings_unavailable:{left_code}:{right_code}")
        if item["security"] is not None or item["industry"] is not None:
            reports.append(item)
    return reports, warnings


def _holdings_stale(bundle: DisclosureBundle, as_of: datetime) -> bool:
    section = DocumentKind.QUARTERLY_HOLDINGS.value
    state = bundle.section_states.get(section)
    if state == "stale":
        return True
    if bundle.section_statuses.get(section, {}).get("state") == "stale":
        return True
    try:
        return build_disclosure_report(bundle, as_of)["holdings"]["freshness"] == "stale"
    except (KeyError, TypeError, ValueError):
        return False


def _industry_portfolio_overlap(
    weights: Mapping[str, Decimal], bundles: Mapping[str, DisclosureBundle], stale: Iterable[str]
) -> Dict[str, object]:
    stale_codes = set(stale)
    exposures: Dict[Tuple[str, str], Dict[str, object]] = {}
    omitted: List[str] = []
    included_weight = Decimal("0")
    for code in sorted(weights):
        bundle = bundles.get(code)
        if bundle is None or code in stale_codes or not bundle.industry_exposure:
            omitted.append(code)
            continue
        latest = max(item.report_period for item in bundle.industry_exposure)
        records = [item for item in bundle.industry_exposure if item.report_period == latest]
        standards = {item.classification_standard for item in records}
        if len(standards) != 1:
            omitted.append(code)
            continue
        included_weight += weights[code]
        for record in records:
            key = (record.classification_standard, record.industry_code or record.industry_name)
            exposure = exposures.setdefault(
                key,
                {
                    "classification_standard": record.classification_standard,
                    "industry_code": record.industry_code,
                    "industry_name": record.industry_name,
                    "contributors": [],
                },
            )
            exposure["contributors"].append(
                {
                    "fund_code": code,
                    "portfolio_weight": weights[code],
                    "disclosed_weight": record.weight,
                    "lookthrough_weight": weights[code] * record.weight / Decimal("100"),
                }
            )
    items = []
    for key in sorted(exposures):
        item = exposures[key]
        contributors = item["contributors"]
        total = sum((entry["lookthrough_weight"] for entry in contributors), Decimal("0"))
        duplicated = total - max(
            (entry["lookthrough_weight"] for entry in contributors),
            default=Decimal("0"),
        )
        items.append({**item, "total_weight": total, "duplicated_contribution": duplicated})
    return {
        "industries": items,
        "portfolio_weight_coverage": included_weight,
        "omitted_fund_codes": omitted,
    }


def _portfolio_section(
    bundles: Mapping[str, DisclosureBundle],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Tuple[Dict[str, object], List[str]]:
    if not positions:
        return {
            "evidence_level": "insufficient_data",
            "portfolio_weight_coverage": Decimal("0"),
            "securities": [],
            "industries": [],
        }, []
    analysis = analyze_portfolio(positions)
    if not analysis.weights:
        return {
            "evidence_level": "insufficient_data",
            "portfolio_weight_coverage": Decimal("0"),
            "securities": [],
            "industries": [],
        }, list(analysis.warnings) + ["portfolio_coverage_partial"]
    holdings_by_fund = {
        code: bundles[code].holdings for code in analysis.weights if code in bundles
    }
    stale = frozenset(
        code
        for code in analysis.weights
        if code in bundles and _holdings_stale(bundles[code], as_of)
    )
    security = portfolio_overlap(analysis.weights, holdings_by_fund, stale_codes=stale)
    industry = _industry_portfolio_overlap(analysis.weights, bundles, stale)
    result = {
        "evidence_level": "deterministic_calculation",
        **security,
        "industries": industry["industries"],
        "industry_portfolio_weight_coverage": industry["portfolio_weight_coverage"],
        "industry_omitted_fund_codes": industry["omitted_fund_codes"],
        "portfolio_value_kind": analysis.value_kind,
        "portfolio_observed_at": max(position.observed_at for position in positions),
    }
    warnings = list(analysis.warnings) + list(security["warnings"])
    if security["portfolio_weight_coverage"] < Decimal("1"):
        warnings.append("portfolio_coverage_partial")
    return result, warnings


def _candidate_portfolio_overlap(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    portfolio: Mapping[str, object],
) -> Dict[str, object]:
    current_exposure = {
        (str(item["exposure_type"]), str(item["security_code"])): Decimal(
            str(item["total_weight"])
        )
        for item in portfolio.get("securities", [])
    }
    result: Dict[str, object] = {}
    for code in codes:
        bundle = bundles.get(code)
        if bundle is None or not bundle.holdings or not current_exposure:
            result[code] = {"evidence_level": "insufficient_data"}
            continue
        latest = max(item.report_period for item in bundle.holdings)
        records = [item for item in bundle.holdings if item.report_period == latest]
        shared = []
        for record in records:
            key = (record.asset_type.value, record.security_code)
            if key not in current_exposure:
                continue
            candidate_weight = record.weight / Decimal("100")
            shared.append(
                {
                    "exposure_type": record.asset_type.value,
                    "security_code": record.security_code,
                    "security_name": record.security_name,
                    "candidate_disclosed_weight": candidate_weight,
                    "shared_weight": min(candidate_weight, current_exposure[key]),
                }
            )
        result[code] = {
            "evidence_level": "deterministic_calculation",
            "metric_name": (
                "top10_disclosed_portfolio_overlap"
                if any(item.disclosure_scope == "top10" for item in records)
                else "disclosed_portfolio_overlap"
            ),
            "report_period": latest,
            "candidate_disclosed_weight": sum(
                (item.weight for item in records), Decimal("0")
            )
            / Decimal("100"),
            "overlap": sum((item["shared_weight"] for item in shared), Decimal("0")),
            "shared": shared,
        }
    return result


def _simple_ordering(
    metric: str, values: Mapping[str, object], direction: str
) -> Dict[str, object]:
    known = [(code, value) for code, value in values.items() if isinstance(value, Decimal)]
    known.sort(key=lambda item: item[0])
    known.sort(key=lambda item: item[1], reverse=direction == "higher")
    return {
        "metric": metric,
        "direction": direction,
        "fund_codes": [code for code, _ in known],
        "values": dict(known),
    } if known else {}


def _source_payload(source: SourceDocument) -> Dict[str, object]:
    return {
        "id": source.id,
        "fund_code": source.fund_code,
        "document_kind": source.document_kind.value,
        "title": source.title,
        "url": source.url,
        "source_name": source.source_name,
        "source_tier": source.source_tier,
        "publisher": source.publisher,
        "published_at": source.published_at,
        "retrieved_at": source.retrieved_at,
    }


def _candidate_source(group: PeerGroup) -> Dict[str, object]:
    return {
        "type": "peer_directory",
        "url": group.candidate_source_url,
        "source_tier": group.candidate_source_tier,
        "checksum": group.candidate_source_checksum,
    }


def _sources(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    group: Optional[PeerGroup] = None,
) -> List[object]:
    result: List[object] = []
    seen_ids = set()
    seen_urls = set()
    if group is not None:
        candidate = _candidate_source(group)
        result.append(candidate)
        seen_urls.add(candidate["url"])
    for code in codes:
        bundle = bundles.get(code)
        if bundle is None:
            continue
        for source_id in sorted(bundle.source_documents):
            source = _source_payload(bundle.source_documents[source_id])
            identity = source.get("id")
            url = source.get("url")
            if (identity is not None and identity in seen_ids) or url in seen_urls:
                continue
            result.append(source)
            if identity is not None:
                seen_ids.add(identity)
            seen_urls.add(url)
    return result


def _manager_payloads(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    as_of: date,
) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for code in codes:
        bundle = bundles.get(code)
        current = [] if bundle is None else [
            item for item in bundle.manager_tenures
            if item.start_date <= as_of and (item.end_date is None or item.end_date >= as_of)
        ]
        result[code] = [
            {
                "manager_name": item.manager_name,
                "start_date": item.start_date,
                "end_date": item.end_date,
                "source_document_id": item.source_document_id,
            }
            for item in sorted(current, key=lambda item: (item.start_date, item.manager_name))
        ]
    return result


def _summary_from_orderings(orderings: Mapping[str, object]) -> Tuple[List[str], List[str]]:
    advantages: List[str] = []
    tradeoffs: List[str] = []
    for window in ("90d", "365d"):
        section = orderings.get(window, {})
        if not isinstance(section, Mapping):
            continue
        for metric_name in ("total_return", "annualized_volatility", "max_drawdown"):
            ordering = section.get(metric_name)
            if not isinstance(ordering, Mapping):
                continue
            codes = ordering.get("fund_codes", [])
            if len(codes) > 1:
                advantages.append(f"{window}:{metric_name}:{codes[0]}")
                tradeoffs.append(f"{window}:{metric_name}:{codes[-1]}")
    for metric_name in (
        "ongoing_annual_fee_rate",
        "size_stability",
        "portfolio_overlap",
    ):
        ordering = orderings.get(metric_name)
        if not isinstance(ordering, Mapping):
            continue
        codes = ordering.get("fund_codes", [])
        if len(codes) > 1:
            advantages.append(f"{metric_name}:{codes[0]}")
            tradeoffs.append(f"{metric_name}:{codes[-1]}")
    return _unique(advantages), _unique(tradeoffs)


def _build_report(
    codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    positions: Sequence[StoredPosition],
    as_of: datetime,
    *,
    group: Optional[PeerGroup],
    comparison_kind: str,
) -> Dict[str, object]:
    _require_aware(as_of)
    windows, orderings, nav_coverage, nav_warnings, data_dates = _window_sections(
        codes, bundles, histories, as_of.date()
    )
    fees, ongoing = _fee_sections(codes, bundles, positions, as_of.date())
    sizes = {}
    for code in codes:
        stability = (
            calculate_size_stability(bundles[code].sizes)
            if code in bundles
            else {"evidence_level": "insufficient_data", "observations": 0}
        )
        sizes[code] = None if stability.get("observations") == 0 else stability
    pairwise, overlap_warnings = _pairwise_sections(codes, bundles)
    portfolio, portfolio_warnings = _portfolio_section(bundles, positions, as_of)
    candidate_overlap = _candidate_portfolio_overlap(codes, bundles, portfolio)
    orderings["ongoing_annual_fee_rate"] = _simple_ordering(
        "ongoing_annual_fee_rate", ongoing, "lower"
    )
    orderings["size_stability"] = _simple_ordering(
        "quarterly_change_pstdev",
        {
            code: None if value is None else value.get("quarterly_change_pstdev")
            for code, value in sizes.items()
        },
        "lower",
    )
    orderings["portfolio_overlap"] = _simple_ordering(
        "disclosed_portfolio_overlap",
        {
            code: value.get("overlap")
            if value.get("evidence_level") == "deterministic_calculation"
            else None
            for code, value in candidate_overlap.items()
        },
        "lower",
    )
    warnings = list(nav_warnings) + overlap_warnings + portfolio_warnings
    data_gaps: List[str] = []
    if group is not None and len(codes) < 2:
        data_gaps.append("peer_group_too_small")
        warnings.append("peer_group_too_small")
    for code in codes:
        if code not in bundles:
            data_gaps.append(f"missing_disclosure_bundle:{code}")
    advantages, tradeoffs = _summary_from_orderings(orderings)
    members = (
        [
            {
                "fund_code": member.fund_code,
                "membership_kind": member.membership_kind.value,
                "classification_key": member.classification_key,
                "acceptance_reason": member.acceptance_reason,
                "warning": member.warning,
                "profile_source_document_id": member.profile_source_document_id,
            }
            for member in group.members
        ]
        if group is not None
        else [{"fund_code": code, "membership_kind": "explicit"} for code in codes]
    )
    if group is not None:
        warnings.extend(group.warnings)
        warnings.extend(member.warning for member in group.members if member.warning)
        data_dates["peer_group_created_at"] = group.created_at
    report: Dict[str, object] = {
        "comparison_kind": comparison_kind,
        "as_of": as_of,
        "rule_version": group.rule_version if group is not None else PEER_RULE_VERSION,
        "calculation_version": PEER_CALCULATION_VERSION,
        "members": members,
        "windows": windows,
        "metric_orderings": orderings,
        "managers": _manager_payloads(codes, bundles, as_of.date()),
        "fees": fees,
        "ongoing_annual_fee_rates": ongoing,
        "sizes": sizes,
        "pairwise_overlap": pairwise,
        "portfolio_overlap": portfolio,
        "candidate_portfolio_overlap": candidate_overlap,
        "advantages": advantages,
        "tradeoffs": tradeoffs,
        "data_gaps": _unique(data_gaps),
        "watch_reasons": [],
        "coverage": {
            "members_total": len(codes),
            "members_with_disclosures": sum(code in bundles for code in codes),
            **nav_coverage,
            "portfolio_weight_coverage": portfolio.get("portfolio_weight_coverage", Decimal("0")),
        },
        "data_dates": data_dates,
        "sources": _sources(codes, bundles, group),
        "warnings": _unique(warnings),
        "errors": [],
    }
    if group is not None:
        report["anchor_fund_code"] = group.anchor_fund_code
        report["rule_key"] = group.rule_key
        report["rule_description"] = group.rule_description
        report["group_status"] = group.status.value
        report["candidate_source"] = _candidate_source(group)
    normalized = _json_value(report)
    _assert_no_forbidden_keys(normalized)
    return normalized


def _walk_keys(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def _assert_no_forbidden_keys(report: object) -> None:
    if any(key in FORBIDDEN_REPORT_KEYS for key in _walk_keys(report)):
        raise AssertionError("deterministic report contains a forbidden interpretation key")


def build_peer_report(
    group: PeerGroup,
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]:
    codes = tuple(member.fund_code for member in group.members)
    return _build_report(
        codes, bundles, histories, positions, as_of, group=group, comparison_kind="peer"
    )


def build_explicit_compare_report(
    fund_codes: Sequence[str],
    bundles: Mapping[str, DisclosureBundle],
    histories: Mapping[str, Sequence[FundNavObservation]],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]:
    codes = tuple(fund_codes)
    if len(codes) < 2 or len(codes) > 10 or len(set(codes)) != len(codes):
        raise ValueError("explicit comparison requires 2 to 10 unique fund codes")
    report = _build_report(
        codes, bundles, histories, positions, as_of, group=None, comparison_kind="explicit"
    )
    warnings = list(report["warnings"])
    anchor = bundles.get(codes[0])
    if anchor is not None:
        for code in codes[1:]:
            candidate = bundles.get(code)
            if candidate is None:
                continue
            classification = classify_peer(anchor, candidate, as_of.date())
            if not classification.accepted:
                warnings.append(f"comparability_warning:{code}:{classification.reason}")
            if "share_class_sibling" in classification.warnings:
                warnings.append(f"share_class_sibling:{codes[0]}:{code}")
    report["warnings"] = _unique(warnings)
    return report


def build_portfolio_overlap_report(
    bundles: Mapping[str, DisclosureBundle],
    positions: Sequence[StoredPosition],
    as_of: datetime,
) -> Dict[str, object]:
    _require_aware(as_of)
    portfolio, warnings = _portfolio_section(bundles, positions, as_of)
    codes = sorted({position.fund_code for position in positions})
    report = {
        "comparison_kind": "portfolio_overlap",
        "as_of": as_of,
        "calculation_version": PEER_CALCULATION_VERSION,
        "data_dates": {
            "portfolio_observed_at": max((item.observed_at for item in positions), default=None),
            "holdings_report_periods": portfolio.get("report_periods", {}),
        },
        "portfolio_overlap": portfolio,
        "coverage": {
            "held_funds_total": len(codes),
            "portfolio_weight_coverage": portfolio.get("portfolio_weight_coverage", Decimal("0")),
        },
        "sources": _sources(codes, bundles),
        "advantages": [],
        "tradeoffs": [],
        "data_gaps": [
            warning
            for warning in warnings
            if "missing" in warning or "partial" in warning
        ],
        "watch_reasons": [],
        "warnings": _unique(warnings),
        "errors": [],
    }
    normalized = _json_value(report)
    _assert_no_forbidden_keys(normalized)
    return normalized
