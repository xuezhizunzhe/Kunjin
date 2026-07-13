from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from kunjin.funds import parsers
from kunjin.funds.html import FundParseError
from kunjin.funds.models import DisclosureBundle, DocumentKind
from kunjin.funds.parsers import ParsedSection
from kunjin.funds.sources import FundTextClient, build_disclosure_url, build_f10_url
from kunjin.funds.store import FundDisclosureStore

SHANGHAI = ZoneInfo("Asia/Shanghai")
REFERER = "https://fundf10.eastmoney.com/"
FRESHNESS_VALUES = frozenset({"fresh", "stale", "missing", "unknown"})


@dataclass(frozen=True)
class SectionSpec:
    document_kind: DocumentKind
    parser_name: str


@dataclass(frozen=True)
class SectionSyncResult:
    section: str
    status: str
    records: int
    freshness: str
    error_code: Optional[str] = None
    as_of: Optional[str] = None
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None


@dataclass(frozen=True)
class FundDisclosureSyncResult:
    fund_code: str
    sections: Dict[str, SectionSyncResult]
    conflicts: Tuple[str, ...]


SECTION_SPECS = {
    "basic_profile": SectionSpec(DocumentKind.BASIC_PROFILE, "parse_basic_profile"),
    "manager_history": SectionSpec(
        DocumentKind.MANAGER_HISTORY, "parse_manager_history"
    ),
    "fee_schedule": SectionSpec(DocumentKind.FEE_SCHEDULE, "parse_fee_schedule"),
    "size_history": SectionSpec(DocumentKind.SIZE_HISTORY, "parse_size_history"),
    "quarterly_holdings": SectionSpec(
        DocumentKind.QUARTERLY_HOLDINGS, "parse_quarterly_holdings"
    ),
    "industry_exposure": SectionSpec(
        DocumentKind.INDUSTRY_EXPOSURE, "parse_industry_exposure"
    ),
    "announcements": SectionSpec(DocumentKind.ANNOUNCEMENT, "parse_announcements"),
}

PROFILE_SECTIONS = (
    "basic_profile",
    "manager_history",
    "fee_schedule",
    "size_history",
    "announcements",
)
CLASSIFICATION_SECTIONS = ("basic_profile",)
HOLDING_SECTIONS = ("quarterly_holdings", "industry_exposure")
AGE_LIMITS = {
    DocumentKind.BASIC_PROFILE: timedelta(days=30),
    DocumentKind.MANAGER_HISTORY: timedelta(days=7),
    DocumentKind.FEE_SCHEDULE: timedelta(days=30),
    DocumentKind.SIZE_HISTORY: timedelta(days=30),
    DocumentKind.ANNOUNCEMENT: timedelta(hours=24),
}


def expected_report_period(as_of: date) -> date:
    if as_of >= date(as_of.year, 11, 7):
        return date(as_of.year, 9, 30)
    if as_of >= date(as_of.year, 8, 7):
        return date(as_of.year, 6, 30)
    if as_of >= date(as_of.year, 5, 7):
        return date(as_of.year, 3, 31)
    if as_of >= date(as_of.year, 4, 7):
        return date(as_of.year - 1, 12, 31)
    return date(as_of.year - 1, 9, 30)


def _error_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    return str(code) if code else error.__class__.__name__.casefold()


def announcement_report_period(title: str) -> Optional[date]:
    normalized = "".join(title.split())
    quarter_match = re.search(
        r"(?<!\d)(\d{4})年(?:第)?([一二三四1234])季度报告", normalized
    )
    if quarter_match is not None:
        quarter_values = {"一": 1, "二": 2, "三": 3, "四": 4}
        raw_quarter = quarter_match.group(2)
        quarter = int(raw_quarter) if raw_quarter.isdigit() else quarter_values[raw_quarter]
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
        return date(int(quarter_match.group(1)), *month_day)
    half_year_match = re.search(r"(?<!\d)(\d{4})年(?:半年度|中期)报告", normalized)
    if half_year_match is not None:
        return date(int(half_year_match.group(1)), 6, 30)
    annual_match = re.search(r"(?<!\d)(\d{4})年年度报告", normalized)
    if annual_match is not None:
        return date(int(annual_match.group(1)), 12, 31)
    return None


class FundDisclosureService:
    def __init__(
        self,
        client: FundTextClient,
        store: FundDisclosureStore,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.store = store
        self.now = now

    def sync_profile(self, fund_code: str) -> FundDisclosureSyncResult:
        return self._sync(fund_code, PROFILE_SECTIONS)

    def sync_classification(self, fund_code: str) -> FundDisclosureSyncResult:
        return self._sync(fund_code, CLASSIFICATION_SECTIONS)

    def sync_holdings(self, fund_code: str) -> FundDisclosureSyncResult:
        return self._sync(fund_code, HOLDING_SECTIONS)

    def sync_all(self, fund_code: str) -> FundDisclosureSyncResult:
        return self._sync(fund_code, PROFILE_SECTIONS + HOLDING_SECTIONS)

    def section_snapshot(self, fund_code: str, section: str) -> SectionSyncResult:
        spec = self._spec(section)
        as_of = self._aware_now()
        bundle = self.store.load_bundle(fund_code)
        return self._result(section, spec, bundle, as_of)

    def _sync(
        self, fund_code: str, section_names: Tuple[str, ...]
    ) -> FundDisclosureSyncResult:
        # Validate before entering the isolated loop; an invalid identifier is a
        # request error, not a remote section failure.
        build_f10_url(DocumentKind.BASIC_PROFILE, fund_code)
        conflicts = []
        for section_name in section_names:
            spec = self._spec(section_name)
            try:
                parsed = self._fetch_and_parse(fund_code, spec)
                self.store.publish_section(
                    fund_code,
                    spec.document_kind,
                    parsed.source,
                    parsed.records,
                    parsed.state,
                    warning="; ".join(parsed.warnings) or None,
                )
                conflicts.extend(
                    f"{section_name}:{conflict}" for conflict in parsed.conflicts
                )
            except Exception as error:
                attempted_at = self._aware_now()
                code = _error_code(error)
                self.store.mark_section_failure(
                    fund_code,
                    spec.document_kind,
                    code,
                    str(error),
                    attempted_at,
                )
                if code == "identity_conflict":
                    conflicts.append(f"{section_name}:{code}")

        as_of = self._aware_now()
        bundle = self.store.load_bundle(fund_code)
        return FundDisclosureSyncResult(
            fund_code=fund_code,
            sections={
                section_name: self._result(
                    section_name, self._spec(section_name), bundle, as_of
                )
                for section_name in section_names
            },
            conflicts=tuple(dict.fromkeys(conflicts)),
        )

    def _fetch_and_parse(
        self, fund_code: str, spec: SectionSpec
    ) -> ParsedSection:
        year = (
            self._aware_now().astimezone(SHANGHAI).year
            if spec.document_kind is DocumentKind.INDUSTRY_EXPOSURE
            else None
        )
        response = self.client.fetch(
            build_disclosure_url(spec.document_kind, fund_code, year=year), REFERER
        )
        parser = getattr(parsers, spec.parser_name)
        if spec.document_kind is DocumentKind.ANNOUNCEMENT:
            identity = self.store.load_bundle(fund_code).identity
            manager_name = "" if identity is None else (identity.manager_name or "")
            return parser(response, fund_code, manager_name)
        parsed = parser(response, fund_code)
        if spec.document_kind in {
            DocumentKind.QUARTERLY_HOLDINGS,
            DocumentKind.INDUSTRY_EXPOSURE,
        }:
            return self._attach_publication_dates(parsed, fund_code)
        return parsed

    def _attach_publication_dates(
        self, parsed: ParsedSection, fund_code: str
    ) -> ParsedSection:
        if parsed.state != "success" or not parsed.records:
            return parsed
        announcements = self.store.load_bundle(fund_code).announcements
        publication_dates: Dict[date, datetime] = {}
        for announcement in announcements:
            report_period = announcement_report_period(announcement.title)
            if report_period is not None and report_period not in publication_dates:
                publication_dates[report_period] = announcement.published_at
        enriched = []
        for record in parsed.records:
            if record.published_at is not None:
                enriched.append(record)
                continue
            published_at = publication_dates.get(record.report_period)
            if published_at is None:
                raise FundParseError(
                    "missing_publication_date",
                    "no announcement exactly matches the disclosure report period",
                )
            enriched.append(replace(record, published_at=published_at))
        warnings = tuple(
            warning
            for warning in parsed.warnings
            if warning != "publication_date_requires_announcement_match"
        )
        return replace(parsed, records=tuple(enriched), warnings=warnings)

    def _aware_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("disclosure service clock must be timezone-aware")
        return value

    def _result(
        self,
        section_name: str,
        spec: SectionSpec,
        bundle: DisclosureBundle,
        as_of: datetime,
    ) -> SectionSyncResult:
        status = bundle.section_statuses.get(spec.document_kind.value)
        state = "missing" if status is None else str(status["state"])
        last_success_at = None if status is None else status["last_success_at"]
        last_attempt_at = None if status is None else status["last_attempted_at"]
        freshness = self._freshness(spec.document_kind, bundle, as_of, last_success_at)
        if freshness not in FRESHNESS_VALUES:
            raise ValueError(f"unsupported freshness value: {freshness}")
        return SectionSyncResult(
            section=section_name,
            status=state,
            records=self._record_count(spec.document_kind, bundle),
            freshness=freshness,
            error_code=None if status is None else status["error_code"],
            as_of=as_of.isoformat(),
            last_success_at=last_success_at,
            last_attempt_at=last_attempt_at,
        )

    @staticmethod
    def _freshness(
        kind: DocumentKind,
        bundle: DisclosureBundle,
        as_of: datetime,
        last_success_at: Optional[str],
    ) -> str:
        if last_success_at is None:
            return "missing"
        if kind in {DocumentKind.QUARTERLY_HOLDINGS, DocumentKind.INDUSTRY_EXPOSURE}:
            records = (
                bundle.holdings
                if kind is DocumentKind.QUARTERLY_HOLDINGS
                else bundle.industry_exposure
            )
            if not records:
                return "unknown"
            latest_period = max(record.report_period for record in records)
            expected = expected_report_period(as_of.astimezone(SHANGHAI).date())
            return "fresh" if latest_period >= expected else "stale"
        try:
            successful_at = datetime.fromisoformat(last_success_at)
            if successful_at.tzinfo is None or successful_at.utcoffset() is None:
                return "unknown"
        except (TypeError, ValueError):
            return "unknown"
        age = as_of - successful_at
        return "fresh" if age <= AGE_LIMITS[kind] else "stale"

    @staticmethod
    def _record_count(kind: DocumentKind, bundle: DisclosureBundle) -> int:
        if kind is DocumentKind.BASIC_PROFILE:
            return (
                (1 if bundle.identity is not None else 0)
                + len(bundle.share_classes)
                + len(bundle.benchmarks)
            )
        records = {
            DocumentKind.MANAGER_HISTORY: bundle.manager_tenures,
            DocumentKind.FEE_SCHEDULE: bundle.fee_rules,
            DocumentKind.SIZE_HISTORY: bundle.sizes,
            DocumentKind.QUARTERLY_HOLDINGS: bundle.holdings,
            DocumentKind.INDUSTRY_EXPOSURE: bundle.industry_exposure,
            DocumentKind.ANNOUNCEMENT: bundle.announcements,
        }
        return len(records[kind])

    @staticmethod
    def _spec(section: str) -> SectionSpec:
        try:
            return SECTION_SPECS[section]
        except KeyError:
            raise ValueError(f"unsupported disclosure section: {section}") from None
