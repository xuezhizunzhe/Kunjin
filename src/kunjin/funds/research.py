from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from kunjin.funds.models import (
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundManagerTenure,
    SourceDocument,
)

_REQUIRED_SECTIONS: Tuple[DocumentKind, ...] = (
    DocumentKind.BASIC_PROFILE,
    DocumentKind.MANAGER_HISTORY,
    DocumentKind.FEE_SCHEDULE,
    DocumentKind.SIZE_HISTORY,
    DocumentKind.QUARTERLY_HOLDINGS,
    DocumentKind.INDUSTRY_EXPOSURE,
    DocumentKind.ANNOUNCEMENT,
)


def _iso(value: Optional[date]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _decimal(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def _source(bundle: DisclosureBundle, source_id: Optional[int]) -> Optional[SourceDocument]:
    if source_id is None:
        return None
    return bundle.source_documents.get(source_id)


def _tier(bundle: DisclosureBundle, record: Any) -> int:
    source = _source(bundle, record.source_document_id)
    return 99 if source is None else source.source_tier


def _source_ids(records: Iterable[Any]) -> List[int]:
    return sorted(
        {
            int(record.source_document_id)
            for record in records
            if record.source_document_id is not None
        }
    )


def _source_payload(source: SourceDocument) -> Dict[str, object]:
    return {
        "id": source.id,
        "document_kind": source.document_kind.value,
        "title": source.title,
        "url": source.url,
        "source_name": source.source_name,
        "source_tier": source.source_tier,
        "publisher": source.publisher,
        "published_at": _iso(source.published_at),
        "retrieved_at": source.retrieved_at.isoformat(),
    }


def _state(bundle: DisclosureBundle, section: DocumentKind) -> str:
    state = bundle.section_states.get(section.value)
    if state:
        return state
    status = bundle.section_statuses.get(section.value, {})
    return str(status.get("state") or "insufficient_data")


def _section_freshness(
    bundle: DisclosureBundle,
    section: DocumentKind,
    as_of: datetime,
) -> Dict[str, object]:
    status = bundle.section_statuses.get(section.value, {})
    last_success_at = status.get("last_success_at")
    age_days: Optional[int] = None
    if last_success_at:
        parsed = datetime.fromisoformat(str(last_success_at))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            age_days = max(0, (as_of.date() - parsed.date()).days)
    return {
        "state": _state(bundle, section),
        "last_attempted_at": status.get("last_attempted_at"),
        "last_success_at": last_success_at,
        "age_days": age_days,
    }


def _manager_payload(record: FundManagerTenure) -> Dict[str, object]:
    return {
        "manager_name": record.manager_name,
        "start_date": record.start_date.isoformat(),
        "end_date": _iso(record.end_date),
        "source_document_id": record.source_document_id,
    }


def _active_manager(record: FundManagerTenure, as_of: date) -> bool:
    return record.start_date <= as_of and (
        record.end_date is None or record.end_date >= as_of
    )


def _select_managers(
    bundle: DisclosureBundle,
    as_of: date,
) -> Tuple[List[FundManagerTenure], List[FundManagerTenure], List[str]]:
    current = [record for record in bundle.manager_tenures if _active_manager(record, as_of)]
    former = [record for record in bundle.manager_tenures if not _active_manager(record, as_of)]
    if not current:
        return (
            [],
            sorted(
                former,
                key=lambda item: (item.start_date, item.manager_name),
                reverse=True,
            ),
            [],
        )

    primary_tier = min(_tier(bundle, record) for record in current)
    primary = [record for record in current if _tier(bundle, record) == primary_tier]
    secondary = [record for record in current if _tier(bundle, record) != primary_tier]
    primary_names = {record.manager_name for record in primary}
    secondary_names = {record.manager_name for record in secondary}
    conflicts = []
    if secondary_names and secondary_names != primary_names:
        conflicts.append(
            "manager_conflict: tier "
            f"{primary_tier} reports {', '.join(sorted(primary_names))}; "
            f"lower-tier sources report {', '.join(sorted(secondary_names))}"
        )
    return (
        sorted(primary, key=lambda item: (item.start_date, item.manager_name)),
        sorted(former, key=lambda item: (item.start_date, item.manager_name), reverse=True),
        conflicts,
    )


def _active_benchmark(record: FundBenchmark, as_of: date) -> bool:
    return (record.effective_from is None or record.effective_from <= as_of) and (
        record.effective_to is None or record.effective_to >= as_of
    )


def _select_benchmarks(
    bundle: DisclosureBundle,
    as_of: date,
) -> Tuple[List[FundBenchmark], List[str]]:
    active = [record for record in bundle.benchmarks if _active_benchmark(record, as_of)]
    if not active:
        return [], []
    primary_tier = min(_tier(bundle, record) for record in active)
    primary = [record for record in active if _tier(bundle, record) == primary_tier]
    secondary = [record for record in active if _tier(bundle, record) != primary_tier]
    primary_values = {record.description for record in primary}
    secondary_values = {record.description for record in secondary}
    conflicts = []
    if secondary_values and secondary_values != primary_values:
        conflicts.append(
            "benchmark_conflict: tier "
            f"{primary_tier} reports {' | '.join(sorted(primary_values))}; "
            f"lower-tier sources report {' | '.join(sorted(secondary_values))}"
        )
    return sorted(primary, key=lambda item: item.description), conflicts


def _fee_payload(rule: FundFeeRule) -> Dict[str, object]:
    return {
        "fee_type": rule.fee_type.value,
        "share_class": rule.share_class,
        "rate": _decimal(rule.rate),
        "fixed_amount": _decimal(rule.fixed_amount),
        "amount_min": _decimal(rule.amount_min),
        "amount_max": _decimal(rule.amount_max),
        "holding_days_min": rule.holding_days_min,
        "holding_days_max": rule.holding_days_max,
        "rule_order": rule.rule_order,
        "effective_from": _iso(rule.effective_from),
        "effective_to": _iso(rule.effective_to),
        "raw_rule_text": rule.raw_rule_text,
        "source_document_id": rule.source_document_id,
    }


def _fee_signature(rule: FundFeeRule) -> Tuple[object, ...]:
    return (
        rule.fee_type.value,
        rule.rate,
        rule.fixed_amount,
        rule.amount_min,
        rule.amount_max,
        rule.holding_days_min,
        rule.holding_days_max,
        rule.effective_from,
        rule.effective_to,
        rule.raw_rule_text,
    )


def _share_class_fee_difference(rules: Sequence[FundFeeRule]) -> bool:
    schedules: Dict[str, set] = {}
    for rule in rules:
        if rule.share_class is not None:
            schedules.setdefault(rule.share_class, set()).add(_fee_signature(rule))
    return len(schedules) > 1 and len({frozenset(value) for value in schedules.values()}) > 1


def _holding_payload(record: FundHolding) -> Dict[str, object]:
    return {
        "rank": record.rank,
        "security_code": record.security_code,
        "security_name": record.security_name,
        "asset_type": record.asset_type.value,
        "weight": _decimal(record.weight),
        "shares": _decimal(record.shares),
        "market_value": _decimal(record.market_value),
        "disclosure_scope": record.disclosure_scope,
        "source_document_id": record.source_document_id,
    }


def _statutory_report_period(title: str) -> Optional[date]:
    year_match = re.search(r"(?P<year>20\d{2})年", title)
    if year_match is None:
        return None
    year = int(year_match.group("year"))
    if "第1季度" in title or "第一季度" in title:
        return date(year, 3, 31)
    if "半年度" in title or "第2季度" in title or "第二季度" in title:
        return date(year, 6, 30)
    if "第3季度" in title or "第三季度" in title:
        return date(year, 9, 30)
    if "第4季度" in title or "第四季度" in title:
        return date(year, 12, 31)
    if "年度" in title:
        return date(year, 12, 31)
    return None


def _holdings_report(
    bundle: DisclosureBundle,
    as_of: datetime,
) -> Tuple[Dict[str, object], List[str]]:
    if not bundle.holdings:
        return {
            "evidence_level": "insufficient_data",
            "report_period": None,
            "published_at": None,
            "disclosure_scopes": [],
            "age_days": None,
            "freshness": _state(bundle, DocumentKind.QUARTERLY_HOLDINGS),
            "items": [],
            "source_document_ids": [],
        }, []

    report_period = max(record.report_period for record in bundle.holdings)
    period_records = [record for record in bundle.holdings if record.report_period == report_period]
    primary_tier = min(_tier(bundle, record) for record in period_records)
    primary = [record for record in period_records if _tier(bundle, record) == primary_tier]
    seen_ranks = set()
    unique_rank_primary = []
    duplicate_rank_group = False
    for record in primary:
        if record.rank in seen_ranks:
            duplicate_rank_group = True
            break
        seen_ranks.add(record.rank)
        unique_rank_primary.append(record)
    if duplicate_rank_group:
        primary = unique_rank_primary
    section_status = bundle.section_statuses.get(DocumentKind.QUARTERLY_HOLDINGS.value, {})
    section_warning = str(section_status.get("warning") or "")
    parser_group_ambiguous = "top10_table_group_display_only" in section_warning
    selection = {
        "rule": (
            "first_complete_rank_group_for_display_only"
            if duplicate_rank_group
            else (
                "parser_selected_display_group"
                if parser_group_ambiguous
                else "single_bound_table_group"
            )
        ),
        "report_period_binding": (
            "unresolved" if duplicate_rank_group or parser_group_ambiguous else "verified"
        ),
        "uncertainty": (
            "Repeated ranks cannot reconstruct historical table groups."
            if duplicate_rank_group
            else (
                "The source page did not bind the displayed table group to one report period."
                if parser_group_ambiguous
                else None
            )
        ),
    }
    holding_conflicts = []
    if duplicate_rank_group:
        holding_conflicts.append("historical_top10_group_unbound")
    if parser_group_ambiguous:
        holding_conflicts.append("multiple_top10_table_groups_unbound")
    publication_dates = [
        record.published_at for record in primary if record.published_at is not None
    ]
    published_at = max(publication_dates) if publication_dates else None
    statutory_periods = [
        period
        for period in (
            _statutory_report_period(item.title) for item in bundle.announcements
        )
        if period is not None
    ]
    is_stale = bool(statutory_periods and max(statutory_periods) > report_period)
    warnings = ["holdings_are_older_than_latest_statutory_report"] if is_stale else []
    if duplicate_rank_group:
        warnings.append("multiple_top10_table_groups")
    return {
        "evidence_level": (
            "partial" if holding_conflicts else "verified_fact"
        ),
        "report_period": report_period.isoformat(),
        "published_at": _iso(published_at),
        "disclosure_scopes": sorted({record.disclosure_scope for record in primary}),
        "age_days": max(0, (as_of.date() - report_period).days),
        "freshness": "stale" if is_stale else "current",
        "items": [
            _holding_payload(record)
            for record in sorted(primary, key=lambda item: (item.rank, item.security_code))
        ],
        "source_document_ids": _source_ids(primary),
        "selection": selection,
        "conflicts": holding_conflicts,
    }, warnings


def _section_has_data(bundle: DisclosureBundle, section: DocumentKind) -> bool:
    if section == DocumentKind.BASIC_PROFILE:
        return bool(bundle.identity or bundle.share_classes or bundle.benchmarks)
    if section == DocumentKind.MANAGER_HISTORY:
        return bool(bundle.manager_tenures)
    if section == DocumentKind.FEE_SCHEDULE:
        return bool(bundle.fee_rules)
    if section == DocumentKind.SIZE_HISTORY:
        return bool(bundle.sizes)
    if section == DocumentKind.QUARTERLY_HOLDINGS:
        return bool(bundle.holdings)
    if section == DocumentKind.INDUSTRY_EXPOSURE:
        return bool(bundle.industry_exposure)
    if section == DocumentKind.ANNOUNCEMENT:
        return bool(bundle.announcements)
    return False


def build_disclosure_report(
    bundle: DisclosureBundle,
    as_of: datetime,
) -> Dict[str, object]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    bundle.validate()

    current_managers, former_managers, manager_conflicts = _select_managers(
        bundle, as_of.date()
    )
    benchmarks, benchmark_conflicts = _select_benchmarks(bundle, as_of.date())
    holdings, holdings_warnings = _holdings_report(bundle, as_of)

    missing_sections: Dict[str, str] = {}
    for section in _REQUIRED_SECTIONS:
        if not _section_has_data(bundle, section):
            missing_sections[section.value] = _state(bundle, section)
    if not current_managers:
        manager_state = _state(bundle, DocumentKind.MANAGER_HISTORY)
        missing_sections["current_manager"] = (
            manager_state if not bundle.manager_tenures else "insufficient_data"
        )
    redemption_rules = [rule for rule in bundle.fee_rules if rule.fee_type == FeeType.REDEMPTION]
    has_redemption_period = any(
        rule.holding_days_min is not None or rule.holding_days_max is not None
        for rule in redemption_rules
    )
    if not has_redemption_period:
        fee_state = _state(bundle, DocumentKind.FEE_SCHEDULE)
        missing_sections["redemption_fee_rules"] = (
            fee_state if not bundle.fee_rules else "insufficient_data"
        )

    status_warnings = [
        str(status["warning"])
        for status in bundle.section_statuses.values()
        if status.get("warning")
    ]
    generated_warnings = list(holdings_warnings)
    if bundle.manager_tenures and not current_managers:
        generated_warnings.append("manager_history_contains_only_former_managers")
    if not has_redemption_period:
        generated_warnings.append("redemption_holding_period_rules_are_missing")
    if _share_class_fee_difference(bundle.fee_rules):
        generated_warnings.append("share_classes_have_different_fee_schedules")
    for section in _REQUIRED_SECTIONS:
        if _state(bundle, section) == "source_unavailable":
            generated_warnings.append(f"{section.value}_source_unavailable")

    all_facts = (
        ((bundle.identity,) if bundle.identity is not None else ())
        + bundle.share_classes
        + bundle.manager_tenures
        + bundle.fee_rules
        + bundle.sizes
        + bundle.benchmarks
        + bundle.holdings
        + bundle.industry_exposure
        + bundle.announcements
    )
    verified_facts = [
        fact for fact in all_facts if _source(bundle, fact.source_document_id) is not None
    ]

    publication_dates = sorted(
        {
            value.isoformat()
            for value in (
                [source.published_at for source in bundle.source_documents.values()]
                + [size.published_at for size in bundle.sizes]
                + [holding.published_at for holding in bundle.holdings]
                + [item.published_at for item in bundle.industry_exposure]
                + [item.published_at for item in bundle.announcements]
            )
            if value is not None
        }
    )
    report_dates = sorted(
        {
            value.isoformat()
            for value in (
                [size.report_date for size in bundle.sizes]
                + [holding.report_period for holding in bundle.holdings]
                + [item.report_period for item in bundle.industry_exposure]
            )
        }
    )

    identity = None
    if bundle.identity is not None:
        identity = {
            "fund_code": bundle.identity.fund_code,
            "fund_name": bundle.identity.fund_name,
            "status": bundle.identity.status,
            "fund_type": bundle.identity.fund_type,
            "established_date": _iso(bundle.identity.established_date),
            "manager_name": bundle.identity.manager_name,
            "source_document_id": bundle.identity.source_document_id,
        }

    return {
        "fund_code": bundle.fund_code,
        "as_of": as_of.isoformat(),
        "evidence_level": "verified_fact" if verified_facts else "insufficient_data",
        "identity": identity,
        "share_classes": [
            {
                "related_fund_code": item.related_fund_code,
                "share_class": item.share_class,
                "fund_name": item.fund_name,
                "source_document_id": item.source_document_id,
            }
            for item in sorted(
                bundle.share_classes,
                key=lambda item: (item.share_class, item.related_fund_code),
            )
        ],
        "managers": {
            "evidence_level": "verified_fact" if current_managers else "insufficient_data",
            "current": [_manager_payload(item) for item in current_managers],
            "former": [_manager_payload(item) for item in former_managers],
            "source_document_ids": _source_ids(bundle.manager_tenures),
        },
        "fees": {
            "evidence_level": "verified_fact" if bundle.fee_rules else "insufficient_data",
            "rules": [
                _fee_payload(item)
                for item in sorted(
                    bundle.fee_rules,
                    key=lambda item: (item.share_class or "", item.fee_type.value, item.rule_order),
                )
            ],
            "source_document_ids": _source_ids(bundle.fee_rules),
        },
        "sizes": {
            "evidence_level": "verified_fact" if bundle.sizes else "insufficient_data",
            "items": [
                {
                    "report_date": item.report_date.isoformat(),
                    "net_assets": _decimal(item.net_assets),
                    "total_shares": _decimal(item.total_shares),
                    "published_at": _iso(item.published_at),
                    "source_document_id": item.source_document_id,
                }
                for item in sorted(bundle.sizes, key=lambda item: item.report_date, reverse=True)
            ],
            "source_document_ids": _source_ids(bundle.sizes),
        },
        "benchmarks": {
            "evidence_level": "verified_fact" if benchmarks else "insufficient_data",
            "items": [
                {
                    "description": item.description,
                    "effective_from": _iso(item.effective_from),
                    "effective_to": _iso(item.effective_to),
                    "source_document_id": item.source_document_id,
                }
                for item in benchmarks
            ],
            "source_document_ids": _source_ids(benchmarks),
        },
        "holdings": holdings,
        "industry_exposure": {
            "evidence_level": "verified_fact" if bundle.industry_exposure else "insufficient_data",
            "items": [
                {
                    "report_period": item.report_period.isoformat(),
                    "published_at": _iso(item.published_at),
                    "classification_standard": item.classification_standard,
                    "industry_code": item.industry_code,
                    "industry_name": item.industry_name,
                    "weight": _decimal(item.weight),
                    "market_value": _decimal(item.market_value),
                    "source_document_id": item.source_document_id,
                }
                for item in sorted(
                    bundle.industry_exposure,
                    key=lambda item: (item.report_period, item.weight),
                    reverse=True,
                )
            ],
            "source_document_ids": _source_ids(bundle.industry_exposure),
        },
        "announcements": {
            "evidence_level": "verified_fact" if bundle.announcements else "insufficient_data",
            "items": [
                {
                    "title": item.title,
                    "category": item.category,
                    "publisher": item.publisher,
                    "published_at": item.published_at.isoformat(),
                    "url": item.url,
                    "source_tier": item.source_tier,
                    "source_document_id": item.source_document_id,
                }
                for item in sorted(
                    bundle.announcements,
                    key=lambda item: item.published_at,
                    reverse=True,
                )
            ],
            "source_document_ids": _source_ids(bundle.announcements),
        },
        "sources": [
            _source_payload(item)
            for item in sorted(
                bundle.source_documents.values(),
                key=lambda item: (item.source_tier, item.id or 0),
            )
        ],
        "publication_dates": publication_dates,
        "report_dates": report_dates,
        "freshness": {
            "as_of": as_of.isoformat(),
            "sections": {
                section.value: _section_freshness(bundle, section, as_of)
                for section in _REQUIRED_SECTIONS
            },
        },
        "warnings": _unique(
            tuple(bundle.warnings) + tuple(status_warnings) + tuple(generated_warnings)
        ),
        "conflicts": _unique(
            tuple(bundle.conflicts) + tuple(manager_conflicts) + tuple(benchmark_conflicts)
        ),
        "missing_sections": missing_sections,
    }
