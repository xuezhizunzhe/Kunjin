from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlparse

from kunjin.brief.models import (
    BriefFact,
    OfficialEvent,
    OfficialEventCode,
    canonical_event_affected_actions,
)
from kunjin.brief.policy import MAX_FACTS, MAX_OFFICIAL_EVENTS
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    SourceAttemptOutcome,
    SourceTier,
    StoredSourceAttempt,
    canonical_decimal,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.funds.models import DisclosureBundle, FundAnnouncement, SourceDocument
from kunjin.funds.official_domains import FUND_COMPANY_DOMAINS
from kunjin.funds.research import build_disclosure_report
from kunjin.funds.risk.research import build_authenticated_risk_research_report
from kunjin.funds.risk.store import (
    ClassificationEvidenceRecord,
    FundRiskStore,
    RiskStoreError,
)
from kunjin.models import FundNavObservation
from kunjin.storage.repository import Repository

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SOURCE_TIERS = {
    "eastmoney_f10": SourceTier.TIER_2,
    "eastmoney_nav": SourceTier.TIER_2,
    "fund_manager_official_documents": SourceTier.TIER_1,
}
_ACTION_SHAPES = frozenset(
    {
        ("fact_research", "continue_holding"),
        ("fact_research", "reduce_to_cash"),
        ("fact_research", "full_exit"),
        ("fact_research", "switch_reduce", "switch_buy"),
    }
)


def _utc(value: datetime, name: str) -> datetime:
    return validate_aware_datetime(value, name).astimezone(timezone.utc)


def _utc_date(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _canonical_url(value: object, name: str) -> str:
    error = f"{name} must be a canonical public HTTPS URL"
    if type(value) is not str or not value:
        raise ValueError(error)
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        raise ValueError(error) from None
    host = parsed.hostname
    if (
        not value.startswith("https://")
        or parsed.scheme != "https"
        or not host
        or host != host.lower()
        or not host.isascii()
        or parsed.netloc != host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(error)
    return value


def _registered_manager_source(publisher: str, url: str) -> bool:
    try:
        host = (urlparse(_canonical_url(url, "official source URL")).hostname or "").lower()
    except ValueError:
        return False
    return FUND_COMPANY_DOMAINS.get(host) == publisher


def _registered_tier2_source(document: SourceDocument) -> bool:
    parsed = urlparse(document.url)
    if document.source_tier != 2:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        host == "fundf10.eastmoney.com"
        and document.source_name == "eastmoney_f10"
        and document.publisher == "东方财富"
    ) or (
        host == "api.fund.eastmoney.com"
        and document.document_kind.value == "announcement"
        and document.source_name == "eastmoney_api"
        and document.publisher == "东方财富公告索引"
    )


def _announcement_integrity_window_seconds() -> int:
    registry = SourceRegistryV1()
    for source in registry.sources:
        if source.source_id != "fund_manager_official_documents":
            continue
        for field in source.fields:
            if field.field_id == "fund_manager_product_announcement":
                maximum = field.freshness.integrity_check_max_age_seconds
                if type(maximum) is int and maximum > 0:
                    return maximum
    raise ValueError("announcement integrity window is unavailable")


def _stable_code(prefix: str, value: str) -> str:
    if _IDENTIFIER.fullmatch(value):
        return value
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}"


def _public_value(value: object) -> object:
    if type(value) is Decimal:
        return canonical_decimal(value)
    if type(value) is int:
        return str(value)
    if type(value) is list:
        return tuple(_public_value(item) for item in value)
    if type(value) is tuple:
        return tuple(_public_value(item) for item in value)
    if type(value) is dict:
        return {str(key): _public_value(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class ProjectedFactSource:
    source_id: str
    source_tier: SourceTier
    publisher: str
    canonical_url: str
    published_at: Optional[datetime]
    retrieved_at: datetime
    source_lineage_id: str

    def validate(self) -> None:
        if type(self) is not ProjectedFactSource:
            raise ValueError("projected fact source subclasses are not accepted")
        validate_exact_dataclass_state(self, "projected fact source")
        validate_identifier(self.source_id, "projected source id")
        if _SOURCE_TIERS.get(self.source_id) is not self.source_tier:
            raise ValueError("projected source tier does not match its source id")
        validate_public_text(self.publisher, "projected source publisher")
        _canonical_url(self.canonical_url, "projected source URL")
        if self.source_tier is SourceTier.TIER_1 and not _registered_manager_source(
            self.publisher,
            self.canonical_url,
        ):
            raise ValueError("Tier 1 projected source is not a registered manager source")
        if self.published_at is not None:
            published_at = _utc(self.published_at, "projected source publication time")
            if published_at > _utc(self.retrieved_at, "projected source retrieval time"):
                raise ValueError("projected source publication follows retrieval")
        else:
            _utc(self.retrieved_at, "projected source retrieval time")
        validate_identifier(self.source_lineage_id, "projected source lineage id")


@dataclass(frozen=True)
class AuthenticatedAnnouncementContent:
    source_document_id: int
    content_fingerprint: str
    original_source_id: str
    quoted_source_id: Optional[str]
    integrity_status: str
    integrity_check_complete: bool
    integrity_checked_at: datetime

    def validate(self) -> None:
        if type(self) is not AuthenticatedAnnouncementContent:
            raise ValueError("announcement content subclasses are not accepted")
        validate_exact_dataclass_state(self, "authenticated announcement content")
        if type(self.source_document_id) is not int or self.source_document_id <= 0:
            raise ValueError("announcement content source document id must be positive")
        validate_checksum(self.content_fingerprint, "announcement content fingerprint")
        validate_identifier(self.original_source_id, "announcement original source id")
        if self.quoted_source_id is not None:
            validate_identifier(self.quoted_source_id, "announcement quoted source id")
            if self.quoted_source_id == self.original_source_id:
                raise ValueError("announcement quote cannot refer to its own original source")
        if self.integrity_status not in {"active", "corrected", "retracted"}:
            raise ValueError("announcement integrity status is unsupported")
        if type(self.integrity_check_complete) is not bool:
            raise ValueError("announcement integrity check flag must be an exact boolean")
        if (
            type(self.integrity_checked_at) is not datetime
            or self.integrity_checked_at.tzinfo is not timezone.utc
        ):
            raise ValueError("announcement integrity check time must use canonical UTC")


@dataclass(frozen=True)
class SourceLinkedFactSet:
    fund_code: str
    facts: Tuple[BriefFact, ...]
    official_events: Tuple[OfficialEvent, ...]
    missing_fields: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    warnings: Tuple[str, ...]

    def validate(self) -> None:
        if type(self) is not SourceLinkedFactSet:
            raise ValueError("source-linked fact set subclasses are not accepted")
        validate_exact_dataclass_state(self, "source-linked fact set")
        if type(self.fund_code) is not str or _FUND_CODE.fullmatch(self.fund_code) is None:
            raise ValueError("source-linked fact set fund code must be six ASCII digits")
        if type(self.facts) is not tuple or len(self.facts) > MAX_FACTS:
            raise ValueError("source-linked facts exceed their bound")
        fact_ids = []
        for fact in self.facts:
            if type(fact) is not BriefFact:
                raise ValueError("source-linked facts require exact BriefFact records")
            fact.validate()
            fact_ids.append(fact.fact_id)
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("source-linked fact ids must be unique")
        if (
            type(self.official_events) is not tuple
            or len(self.official_events) > MAX_OFFICIAL_EVENTS
        ):
            raise ValueError("official events exceed their bound")
        event_ids = []
        for event in self.official_events:
            if type(event) is not OfficialEvent:
                raise ValueError("official events require exact OfficialEvent records")
            event.validate()
            event_ids.append(event.event_id)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("official event ids must be unique")
        for values, name in (
            (self.missing_fields, "missing fields"),
            (self.conflicts, "fact conflicts"),
            (self.warnings, "fact warnings"),
        ):
            validate_identifier_tuple(values, name)


def _source_from_document(
    document: SourceDocument,
    as_of: datetime,
    manager_name: Optional[str],
) -> ProjectedFactSource:
    document.validate()
    if document.id is None:
        raise ValueError("source document must have a stored id")
    if document.source_tier == 1:
        if (
            manager_name is None
            or document.publisher != manager_name
            or not _registered_manager_source(document.publisher, document.url)
        ):
            raise ValueError("Tier 1 source does not match the registered fund manager")
        source_id = "fund_manager_official_documents"
        tier = SourceTier.TIER_1
    elif document.source_tier == 2:
        if not _registered_tier2_source(document):
            raise ValueError("Tier 2 source is not a registered Eastmoney source")
        source_id = "eastmoney_f10"
        tier = SourceTier.TIER_2
    else:
        raise ValueError("Tier 3 disclosure records are not projected as brief facts")
    retrieved_at = _utc(document.retrieved_at, "retrieval")
    published_at = (
        None if document.published_at is None else _utc(document.published_at, "publication")
    )
    if retrieved_at > as_of or (published_at is not None and published_at > as_of):
        raise ValueError("source document is later than the projection as-of time")
    source = ProjectedFactSource(
        source_id,
        tier,
        document.publisher,
        document.url,
        published_at,
        retrieved_at,
        f"disclosure_document_{document.id}",
    )
    source.validate()
    return source


def _freshness(state: object, tier: SourceTier) -> EvidenceFreshness:
    if state == "stale":
        return EvidenceFreshness.STALE
    if tier is SourceTier.TIER_2:
        return EvidenceFreshness.DATED_HISTORY
    if state in {"success", "current"}:
        return EvidenceFreshness.CURRENT
    return EvidenceFreshness.UNKNOWN


def _fact(
    fact_id: str,
    field_id: str,
    value: object,
    source: ProjectedFactSource,
    *,
    unit: Optional[str] = None,
    data_as_of: Optional[datetime] = None,
    published_at: Optional[datetime] = None,
    freshness: EvidenceFreshness,
    completeness: Optional[EvidenceCompleteness] = None,
    conflict_ids: Tuple[str, ...] = (),
    calculated: bool = False,
) -> BriefFact:
    source.validate()
    result = BriefFact(
        fact_id=fact_id,
        field_id=field_id,
        value=_public_value(value),
        unit=unit,
        data_as_of=data_as_of,
        published_at=source.published_at if published_at is None else published_at,
        retrieved_at=source.retrieved_at,
        source_id=source.source_id,
        source_tier=source.source_tier,
        publisher=source.publisher,
        canonical_url=source.canonical_url,
        freshness=freshness,
        completeness=(
            EvidenceCompleteness.COMPLETE
            if completeness is None and source.source_tier is SourceTier.TIER_1
            else EvidenceCompleteness.PARTIAL
            if completeness is None
            else completeness
        ),
        conflict_ids=conflict_ids,
        calculated=calculated,
        source_lineage_id=source.source_lineage_id,
    )
    result.validate()
    return result


def _document_for(
    bundle: DisclosureBundle,
    source_document_id: object,
) -> Optional[SourceDocument]:
    if type(source_document_id) is not int:
        return None
    return bundle.source_documents.get(source_document_id)


def _add_disclosure_fact(
    facts: List[BriefFact],
    missing: set,
    warnings: set,
    bundle: DisclosureBundle,
    as_of: datetime,
    *,
    fact_id: str,
    field_id: str,
    value: object,
    source_document_id: object,
    state: object,
    data_as_of: Optional[datetime] = None,
    published_at: Optional[datetime] = None,
    conflicts: Tuple[str, ...] = (),
    completeness: Optional[EvidenceCompleteness] = None,
) -> None:
    document = _document_for(bundle, source_document_id)
    if document is None:
        missing.add(field_id)
        warnings.add("source_projection_missing")
        return
    try:
        manager_name = None if bundle.identity is None else bundle.identity.manager_name
        source = _source_from_document(document, as_of, manager_name)
        facts.append(
            _fact(
                fact_id,
                field_id,
                value,
                source,
                data_as_of=data_as_of,
                published_at=published_at,
                freshness=_freshness(state, source.source_tier),
                completeness=completeness,
                conflict_ids=conflicts,
            )
        )
    except ValueError:
        missing.add(field_id)
        warnings.add("source_projection_invalid")


def _announcement_binding(
    bundle: DisclosureBundle,
    announcement: FundAnnouncement,
    as_of: datetime,
) -> Optional[Tuple[SourceDocument, ProjectedFactSource]]:
    document = _document_for(bundle, announcement.source_document_id)
    if document is None:
        return None
    if (
        document.document_kind.value != "announcement"
        or document.fund_code != bundle.fund_code
        or announcement.fund_code != bundle.fund_code
        or announcement.source_tier != document.source_tier
    ):
        return None
    try:
        manager_name = None if bundle.identity is None else bundle.identity.manager_name
        if document.source_tier == 1:
            if (
                document.title != announcement.title
                or announcement.publisher != document.publisher
                or announcement.url != document.url
                or document.published_at is None
                or _utc(announcement.published_at, "announcement publication")
                != _utc(document.published_at, "document publication")
            ):
                return None
            return document, _source_from_document(document, as_of, manager_name)
        if document.source_tier != 2 or not _registered_tier2_source(document):
            return None
        parsed = urlparse(document.url)
        canonical_url = document.url
        if parsed.query:
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            values = dict(pairs)
            if (
                parsed.scheme != "https"
                or (parsed.hostname or "").lower() != "api.fund.eastmoney.com"
                or parsed.path != "/f10/JJGG"
                or len(pairs) != 4
                or len(values) != 4
                or values
                != {
                    "fundcode": bundle.fund_code,
                    "pageIndex": "1",
                    "pageSize": "20",
                    "type": "0",
                }
                or parsed.params
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
            ):
                return None
            canonical_url = f"https://fundf10.eastmoney.com/jjgg_{bundle.fund_code}.html"
        retrieved_at = _utc(document.retrieved_at, "announcement index retrieval")
        if retrieved_at > as_of:
            return None
        source = ProjectedFactSource(
            "eastmoney_f10",
            SourceTier.TIER_2,
            document.publisher,
            canonical_url,
            None,
            retrieved_at,
            f"disclosure_document_{document.id}",
        )
        source.validate()
        return document, source
    except ValueError:
        return None


def _official_manager_source(
    bundle: DisclosureBundle,
    announcement: FundAnnouncement,
) -> bool:
    parsed = urlparse(announcement.url)
    host = (parsed.hostname or "").lower().rstrip(".")
    registered = FUND_COMPANY_DOMAINS.get(host)
    manager_identity = None if bundle.identity is None else bundle.identity.manager_name
    return (
        announcement.source_tier == 1
        and registered is not None
        and announcement.publisher == registered
        and manager_identity == registered
    )


def _event_code(
    title: str,
    product_name: Optional[str],
) -> Optional[OfficialEventCode]:
    normalized = re.sub(r"[\s　]+", "", unicodedata.normalize("NFKC", title))
    if product_name is None:
        return None
    normalized_name = re.sub(
        r"[\s　]+",
        "",
        unicodedata.normalize("NFKC", product_name),
    )
    suffix = None
    for prefix in (f"关于{normalized_name}", normalized_name):
        if normalized.startswith(prefix):
            suffix = normalized[len(prefix) :]
            break
    if suffix is None:
        return None
    if any(
        marker in suffix
        for marker in (
            "可能",
            "提示",
            "更正",
            "补充",
            "撤销",
            "恢复",
            "解读",
            "优惠",
        )
    ):
        return OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE
    patterns = (
        (
            OfficialEventCode.FUND_TERMINATION_NOTICE,
            re.compile(r"^基金合同终止(?:及基金财产清算结果)?(?:的)?公告$"),
        ),
        (
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
            re.compile(r"^(?:清算(?:报告|公告|的公告)|基金财产清算报告)$"),
        ),
        (
            OfficialEventCode.MANAGER_CHANGE_NOTICE,
            re.compile(r"^(?:基金经理变更|增聘基金经理)公告$"),
        ),
        (
            OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE,
            re.compile(r"^暂停申购(?:业务)?(?:的)?公告$"),
        ),
        (
            OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
            re.compile(r"^暂停(?:赎回|大额赎回)(?:业务)?(?:的)?公告$"),
        ),
        (
            OfficialEventCode.FEE_CHANGE_NOTICE,
            re.compile(r"^(?:调整|变更).*(?:费率|管理费|托管费|销售服务费).*公告$"),
        ),
        (
            OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
            re.compile(r"^(?:变更|调整)业绩比较基准.*公告$"),
        ),
    )
    for code, pattern in patterns:
        if pattern.fullmatch(suffix):
            return code
    return OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE


def _affected_actions(
    event_code: OfficialEventCode,
    action_ids: Tuple[str, ...],
) -> Tuple[str, ...]:
    return canonical_event_affected_actions(event_code, action_ids)


def _project_disclosure(
    bundle: DisclosureBundle,
    as_of: datetime,
    action_ids: Tuple[str, ...],
    contents: Mapping[int, AuthenticatedAnnouncementContent],
) -> Tuple[List[BriefFact], List[OfficialEvent], set, set, set]:
    report = build_disclosure_report(bundle, as_of)
    facts: List[BriefFact] = []
    events: List[OfficialEvent] = []
    missing = {str(key) for key in report["missing_sections"]}
    warnings = {_stable_code("warning", str(item)) for item in report["warnings"]}
    raw_conflicts = tuple(str(item) for item in report["conflicts"])
    conflicts = {_stable_code("conflict", item) for item in raw_conflicts}
    manager_conflict_ids = tuple(
        sorted(
            _stable_code("conflict", item) for item in raw_conflicts if "manager" in item.casefold()
        )
    )
    benchmark_conflict_ids = tuple(
        sorted(
            _stable_code("conflict", item)
            for item in raw_conflicts
            if "benchmark" in item.casefold()
        )
    )

    identity = report["identity"]
    if type(identity) is dict:
        _add_disclosure_fact(
            facts,
            missing,
            warnings,
            bundle,
            as_of,
            fact_id="identity_active_status",
            field_id="identity_active_status",
            value={
                "fund_code": identity["fund_code"],
                "fund_name": identity["fund_name"],
                "status": identity["status"],
                "fund_type": identity["fund_type"],
                "established_date": identity["established_date"],
                "fund_company": identity["manager_name"],
            },
            source_document_id=identity["source_document_id"],
            state=report["freshness"]["sections"]["basic_profile"]["state"],
            conflicts=(),
        )

    for index, item in enumerate(report["share_classes"], start=1):
        _add_disclosure_fact(
            facts,
            missing,
            warnings,
            bundle,
            as_of,
            fact_id=f"share_class_{index}",
            field_id="share_class_identity",
            value={
                "related_fund_code": item["related_fund_code"],
                "share_class": item["share_class"],
                "fund_name": item["fund_name"],
            },
            source_document_id=item["source_document_id"],
            state=report["freshness"]["sections"]["basic_profile"]["state"],
            conflicts=(),
        )

    manager_state = report["freshness"]["sections"]["manager_history"]["state"]
    for label, field_id in (
        ("current", "current_manager_team"),
        ("former", "former_manager_history"),
    ):
        for index, item in enumerate(report["managers"][label], start=1):
            _add_disclosure_fact(
                facts,
                missing,
                warnings,
                bundle,
                as_of,
                fact_id=f"{field_id}_{index}",
                field_id=field_id,
                value={
                    "manager_name": item["manager_name"],
                    "tenure_start": item["start_date"],
                    "tenure_end": item["end_date"],
                },
                source_document_id=item["source_document_id"],
                state=manager_state,
                data_as_of=_utc_date(date.fromisoformat(item["start_date"])),
                conflicts=manager_conflict_ids,
            )

    benchmark_state = report["freshness"]["sections"]["basic_profile"]["state"]
    for index, item in enumerate(report["benchmarks"]["items"], start=1):
        effective_from = item["effective_from"]
        _add_disclosure_fact(
            facts,
            missing,
            warnings,
            bundle,
            as_of,
            fact_id=f"current_benchmark_{index}",
            field_id="current_benchmark",
            value={
                "description": item["description"],
                "effective_from": effective_from,
                "effective_to": item["effective_to"],
            },
            source_document_id=item["source_document_id"],
            state=benchmark_state,
            data_as_of=(
                None if effective_from is None else _utc_date(date.fromisoformat(effective_from))
            ),
            conflicts=benchmark_conflict_ids,
        )

    fee_state = report["freshness"]["sections"]["fee_schedule"]["state"]
    for index, item in enumerate(report["fees"]["rules"], start=1):
        _add_disclosure_fact(
            facts,
            missing,
            warnings,
            bundle,
            as_of,
            fact_id=f"fee_schedule_{index}",
            field_id="fees_share_class_relationship",
            value={
                "fee_type": item["fee_type"],
                "share_class": item["share_class"],
                "rate": item["rate"],
                "fixed_fee": item["fixed_amount"],
                "threshold_minimum": item["amount_min"],
                "threshold_maximum": item["amount_max"],
                "holding_days_minimum": item["holding_days_min"],
                "holding_days_maximum": item["holding_days_max"],
                "rule_order": item["rule_order"],
                "effective_from": item["effective_from"],
                "effective_to": item["effective_to"],
            },
            source_document_id=item["source_document_id"],
            state=fee_state,
            conflicts=(),
        )

    holding_report = report["holdings"]
    holding_state = holding_report["freshness"]
    grouped: Dict[int, List[dict]] = {}
    for item in holding_report["items"]:
        source_document_id = item["source_document_id"]
        if type(source_document_id) is int:
            grouped.setdefault(source_document_id, []).append(item)
    for index, (source_document_id, items) in enumerate(sorted(grouped.items()), start=1):
        projected_items = tuple(
            {
                "rank": item["rank"],
                "security_code": item["security_code"],
                "security_name": item["security_name"],
                "asset_class": item["asset_type"],
                "disclosed_weight": item["weight"],
            }
            for item in items
        )
        report_period = holding_report["report_period"]
        source_publications = tuple(
            item.published_at
            for item in bundle.holdings
            if item.source_document_id == source_document_id
            and item.report_period.isoformat() == report_period
            and item.published_at is not None
        )
        published_at = None if not source_publications else max(source_publications)
        _add_disclosure_fact(
            facts,
            missing,
            warnings,
            bundle,
            as_of,
            fact_id=f"disclosed_holdings_{index}",
            field_id="holdings_industries",
            value={
                "report_period": report_period,
                "disclosure_scope": tuple(holding_report["disclosure_scopes"]),
                "items": projected_items,
            },
            source_document_id=source_document_id,
            state=holding_state,
            data_as_of=(
                None if report_period is None else _utc_date(date.fromisoformat(report_period))
            ),
            published_at=(
                None if published_at is None else _utc(published_at, "holdings publication")
            ),
            conflicts=(),
            completeness=(
                EvidenceCompleteness.PARTIAL
                if "top10" in holding_report["disclosure_scopes"]
                else None
            ),
        )

    announcement_state = report["freshness"]["sections"]["announcement"]["state"]
    official_sources_seen = False
    official_event_check_complete = announcement_state in {"success", "current"}
    product_name = None if bundle.identity is None else bundle.identity.fund_name
    for announcement in sorted(
        bundle.announcements,
        key=lambda item: (
            item.published_at,
            item.source_document_id or 0,
            item.title,
            item.url,
        ),
        reverse=True,
    ):
        binding = _announcement_binding(bundle, announcement, as_of)
        if binding is None:
            conflicts.add("announcement_source_conflict")
            official_event_check_complete = False
            continue
        document, source = binding
        announcement_key = hashlib.sha256(
            f"{announcement.title}\0{announcement.url}".encode()
        ).hexdigest()[:12]
        stable_suffix = f"{document.id}_{announcement_key}"
        facts.append(
            _fact(
                f"announcement_{stable_suffix}",
                "fund_manager_product_announcement",
                {
                    "title": announcement.title,
                    "category": announcement.category,
                    "record_publisher": announcement.publisher,
                    "record_url": announcement.url,
                    "record_published_at": _utc(
                        announcement.published_at,
                        "announcement publication",
                    ).isoformat(),
                },
                source,
                data_as_of=_utc(announcement.published_at, "announcement publication"),
                published_at=_utc(announcement.published_at, "announcement publication"),
                freshness=_freshness(announcement_state, source.source_tier),
                conflict_ids=(),
            )
        )
        content = contents.get(document.id or 0)
        if source.source_tier is not SourceTier.TIER_1 or not _official_manager_source(
            bundle, announcement
        ):
            continue
        official_sources_seen = True
        if content is None:
            official_event_check_complete = False
            continue
        if content.original_source_id != source.source_lineage_id:
            warnings.add("announcement_lineage_invalid")
            official_event_check_complete = False
            continue
        if content.quoted_source_id is not None and content.quoted_source_id not in {
            item.original_source_id for item in contents.values()
        }:
            warnings.add("announcement_lineage_invalid")
            official_event_check_complete = False
            continue
        if not content.integrity_check_complete:
            warnings.add("official_event_integrity_incomplete")
            official_event_check_complete = False
            continue
        checked_at = content.integrity_checked_at
        integrity_window = _announcement_integrity_window_seconds()
        if (
            content.content_fingerprint != document.checksum
            or not source.retrieved_at <= checked_at <= as_of
            or (as_of - checked_at).total_seconds() > integrity_window
        ):
            conflicts.add("announcement_content_binding_invalid")
            official_event_check_complete = False
            continue
        if content.integrity_status != "active":
            warnings.add("official_event_integrity_nonactive")
            official_event_check_complete = False
            continue
        event_code = _event_code(announcement.title, product_name)
        if event_code is None:
            continue
        affected = _affected_actions(event_code, action_ids)
        if not affected:
            continue
        event = OfficialEvent(
            event_id=f"event_{stable_suffix}_{event_code.value}",
            event_code=event_code,
            title=announcement.title,
            summary=announcement.title,
            publisher=announcement.publisher,
            canonical_url=announcement.url,
            published_at=_utc(announcement.published_at, "announcement publication"),
            retrieved_at=source.retrieved_at,
            source_tier=SourceTier.TIER_1,
            original_source_id=content.original_source_id,
            quoted_source_id=content.quoted_source_id,
            content_fingerprint=content.content_fingerprint,
            integrity_status=content.integrity_status,
            affected_action_ids=affected,
        )
        event.validate()
        events.append(event)
    if not official_sources_seen or not official_event_check_complete:
        missing.add("official_events")
    return facts, events, missing, conflicts, warnings


def _project_nav(
    facts: List[BriefFact],
    missing: set,
    conflicts: set,
    fund_code: str,
    as_of: datetime,
    repository: Optional[Repository],
    decision_audit_store: Optional[DecisionAuditStore],
) -> None:
    if repository is None and decision_audit_store is None:
        missing.add("formal_nav")
        return
    try:
        if (
            type(repository) is not Repository
            or type(decision_audit_store) is not DecisionAuditStore
            or repository.database.resolve() != decision_audit_store.repository.database.resolve()
        ):
            raise ValueError("formal NAV stores do not share one database")
        history = repository.fund_history(fund_code)
        batches: Dict[datetime, List[FundNavObservation]] = {}
        for observation in history:
            if type(observation) is not FundNavObservation:
                raise ValueError("formal NAV history contains a noncanonical row")
            observation.validate()
            retrieved_at = _utc(observation.retrieved_at, "NAV retrieval")
            nav_as_of = _utc_date(observation.nav_date)
            if (
                observation.fund_code != fund_code
                or observation.source != "eastmoney"
                or retrieved_at > as_of
                or nav_as_of > as_of
            ):
                continue
            batches.setdefault(retrieved_at, []).append(observation)

        for retrieved_at in sorted(batches, reverse=True):
            batch = batches[retrieved_at]
            attempt_ids = {item.source_attempt_id for item in batch}
            if (
                len(attempt_ids) != 1
                or None in attempt_ids
                or any(type(item.source_attempt_id) is not int for item in batch)
            ):
                continue
            attempt_id = next(iter(attempt_ids))
            try:
                attempt = decision_audit_store.authenticated_source_attempt(attempt_id)
            except DecisionAuditStoreError:
                continue
            if type(attempt) is not StoredSourceAttempt:
                continue
            attempt.validate()
            observation = max(batch, key=lambda item: item.nav_date)
            expected_as_of = _utc_date(observation.nav_date)
            started_at = _utc(attempt.attempt.started_at, "NAV attempt start")
            finished_at = _utc(attempt.attempt.finished_at, "NAV attempt finish")
            attempt_data_as_of = (
                None
                if attempt.attempt.data_as_of is None
                else _utc(attempt.attempt.data_as_of, "NAV attempt data as-of")
            )
            if (
                attempt.id != attempt_id
                or attempt.attempt.source_id != "eastmoney_nav"
                or attempt.attempt.field_id != "formal_nav"
                or attempt.attempt.subject_key != f"fund:{fund_code}"
                or attempt.attempt.outcome is not SourceAttemptOutcome.SUCCESS
                or attempt_data_as_of != expected_as_of
                or not started_at <= retrieved_at <= finished_at <= as_of
                or any(item.source_attempt_id != attempt.id for item in batch)
            ):
                continue
            source = ProjectedFactSource(
                "eastmoney_nav",
                SourceTier.TIER_2,
                "东方财富",
                f"https://fund.eastmoney.com/{fund_code}.html",
                None,
                retrieved_at,
                f"source_attempt_{attempt.id}",
            )
            facts.append(
                _fact(
                    "formal_nav",
                    "formal_nav",
                    canonical_decimal(observation.unit_nav),
                    source,
                    unit="cny_per_share",
                    data_as_of=expected_as_of,
                    freshness=(
                        EvidenceFreshness.CURRENT
                        if observation.nav_date == as_of.date()
                        else EvidenceFreshness.DATED_HISTORY
                    ),
                )
            )
            return
        missing.add("formal_nav")
        if history:
            conflicts.add("formal_nav_binding_invalid")
    except (DecisionAuditStoreError, TypeError, ValueError):
        missing.add("formal_nav")
        conflicts.add("formal_nav_binding_invalid")


def _classification_source(
    item: Mapping[str, object],
    as_of: datetime,
) -> ProjectedFactSource:
    namespace = item.get("source_namespace")
    if namespace not in {"d1_artifact", "fund_disclosure"}:
        raise ValueError("D1 source namespace is not projectable")
    tier_value = item.get("source_tier")
    if tier_value == 1:
        source_id = "fund_manager_official_documents"
        tier = SourceTier.TIER_1
    elif tier_value == 2:
        if namespace == "d1_artifact":
            raise ValueError("D1 artifacts must remain Tier 1")
        source_id = "eastmoney_f10"
        tier = SourceTier.TIER_2
    else:
        raise ValueError("D1 source tier is not projectable")
    document_id = item.get("document_id")
    if type(document_id) is not int or document_id <= 0:
        raise ValueError("D1 source document id is invalid")
    published = item.get("published_at")
    retrieved = item.get("retrieved_at")
    if type(retrieved) is not str:
        raise ValueError("D1 source retrieval is invalid")
    source = ProjectedFactSource(
        source_id,
        tier,
        str(item.get("publisher") or ""),
        str(item.get("url") or ""),
        (
            None
            if published is None
            else _utc(
                datetime.fromisoformat(str(published)),
                "D1 publication",
            )
        ),
        _utc(datetime.fromisoformat(retrieved), "D1 retrieval"),
        f"d1_document_{document_id}",
    )
    source.validate()
    if source.retrieved_at > as_of or (
        source.published_at is not None and source.published_at > as_of
    ):
        raise ValueError("D1 source is later than the projection as-of time")
    return source


def _project_classification(
    facts: List[BriefFact],
    missing: set,
    conflicts: set,
    fund_code: str,
    as_of: datetime,
    risk_store: Optional[FundRiskStore],
) -> None:
    if risk_store is None:
        missing.add("d1_classification")
        return
    if type(risk_store) is not FundRiskStore:
        raise ValueError("risk store must be an exact FundRiskStore or None")
    try:
        record = risk_store.classification_evidence(fund_code)
        if record is None:
            missing.add("d1_classification")
            return
        if type(record) is not ClassificationEvidenceRecord:
            raise ValueError("current authenticated D1 classification is unavailable")
        report = build_authenticated_risk_research_report(record)
        if report.get("fund_code") != fund_code:
            raise ValueError("D1 classification fund binding does not match")
        classification = report.get("classification")
        sources = report.get("sources")
        if type(classification) is not dict or type(sources) is not list or not sources:
            raise ValueError("D1 classification projection is incomplete")
        classified_at = _utc(
            datetime.fromisoformat(str(classification["classified_at"])),
            "D1 classification time",
        )
        valid_until = _utc(
            datetime.fromisoformat(str(classification["valid_until"])),
            "D1 validity",
        )
        if classified_at > as_of:
            raise ValueError("D1 classification is later than the projection as-of time")
        evidence_status = str(report.get("evidence_status") or "unclassified")
        if evidence_status not in {
            "verified",
            "partial",
            "conflicted",
            "stale",
            "unclassified",
        }:
            raise ValueError("D1 evidence status is unsupported")
        value = {
            "product_family": classification["product_family"],
            "risk_bucket": classification["risk_bucket"],
            "portfolio_role": classification["portfolio_role"],
            "classified_at": classified_at.isoformat(),
            "evidence_status": evidence_status,
            "evidence_tags": tuple(report.get("evidence_tags") or ()),
            "capability": "research_only",
        }
        projected = []
        for item in sources:
            if type(item) is not dict:
                raise ValueError("D1 classification source is not canonical")
            projected.append(_classification_source(item, as_of))
        projected.sort(key=lambda item: item.source_lineage_id)
        freshness = (
            EvidenceFreshness.CURRENT
            if classified_at <= as_of <= valid_until and evidence_status != "stale"
            else EvidenceFreshness.STALE
        )
        d1_conflicts = tuple(
            sorted(_stable_code("d1_conflict", str(item)) for item in report.get("conflicts") or ())
        )
        conflicts.update(d1_conflicts)
        for index, source in enumerate(projected, start=1):
            completeness = (
                EvidenceCompleteness.COMPLETE
                if evidence_status == "verified" and source.source_tier is SourceTier.TIER_1
                else EvidenceCompleteness.PARTIAL
            )
            facts.append(
                _fact(
                    f"d1_classification_{index}",
                    "d1_classification",
                    value,
                    source,
                    data_as_of=source.retrieved_at,
                    freshness=freshness,
                    completeness=completeness,
                    conflict_ids=d1_conflicts,
                    calculated=True,
                )
            )
        if freshness is EvidenceFreshness.STALE:
            missing.add("d1_current_classification")
        missing.update(str(item) for item in report.get("missing_evidence") or ())
    except (KeyError, RiskStoreError, TypeError, ValueError):
        missing.add("d1_classification")
        conflicts.add("d1_classification_binding_invalid")


def build_source_linked_facts(
    bundle: DisclosureBundle,
    as_of: datetime,
    *,
    announcement_contents: Tuple[AuthenticatedAnnouncementContent, ...] = (),
    repository: Optional[Repository] = None,
    decision_audit_store: Optional[DecisionAuditStore] = None,
    risk_store: Optional[FundRiskStore] = None,
    action_ids: Tuple[str, ...],
) -> SourceLinkedFactSet:
    if type(bundle) is not DisclosureBundle:
        raise ValueError("fact projection requires an exact DisclosureBundle")
    bundle.validate()
    if type(as_of) is not datetime:
        raise ValueError("fact projection as-of time must be an exact datetime")
    as_of = _utc(as_of, "fact projection as-of")
    if type(action_ids) is not tuple or not action_ids:
        raise ValueError("fact projection action ids must be a non-empty exact tuple")
    validate_identifier_tuple(action_ids, "fact projection action ids", allow_empty=False)
    if action_ids not in _ACTION_SHAPES:
        raise ValueError("fact projection action shape is unsupported")
    if type(announcement_contents) is not tuple:
        raise ValueError("announcement contents must be an exact tuple")
    content_by_source: Dict[int, AuthenticatedAnnouncementContent] = {}
    for content in announcement_contents:
        if type(content) is not AuthenticatedAnnouncementContent:
            raise ValueError("announcement contents require exact authenticated records")
        content.validate()
        if content.source_document_id in content_by_source:
            raise ValueError("announcement contents contain duplicate source bindings")
        content_by_source[content.source_document_id] = content
    if not set(content_by_source).issubset(bundle.source_documents):
        raise ValueError("announcement content references an unknown source document")
    if (repository is None) != (decision_audit_store is None):
        raise ValueError("formal NAV projection requires both stores or neither")
    if repository is not None:
        if (
            type(repository) is not Repository
            or type(decision_audit_store) is not DecisionAuditStore
        ):
            raise ValueError("formal NAV projection requires exact store types")
        if repository.database.resolve() != decision_audit_store.repository.database.resolve():
            raise ValueError("formal NAV stores must reference the same database")

    facts, events, missing, conflicts, warnings = _project_disclosure(
        bundle,
        as_of,
        action_ids,
        content_by_source,
    )
    _project_nav(
        facts,
        missing,
        conflicts,
        bundle.fund_code,
        as_of,
        repository,
        decision_audit_store,
    )
    _project_classification(
        facts,
        missing,
        conflicts,
        bundle.fund_code,
        as_of,
        risk_store,
    )

    def fact_priority(item: BriefFact) -> Tuple[int, str]:
        if item.field_id == "identity_active_status":
            priority = 0
        elif item.field_id == "current_manager_team":
            priority = 1
        elif item.field_id == "share_class_identity":
            priority = 2
        elif item.field_id == "fees_share_class_relationship":
            priority = 3
        elif item.field_id == "holdings_industries":
            priority = 4
        elif item.field_id == "current_benchmark":
            priority = 5
        elif item.field_id == "formal_nav":
            priority = 6
        elif item.field_id == "d1_classification":
            priority = 7
        elif item.field_id == "former_manager_history":
            priority = 8
        elif item.field_id == "fund_manager_product_announcement":
            priority = 10
        else:
            priority = 9
        return priority, item.fact_id

    facts.sort(key=fact_priority)
    event_priority = {
        OfficialEventCode.FUND_LIQUIDATION_NOTICE: 0,
        OfficialEventCode.FUND_TERMINATION_NOTICE: 0,
        OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE: 2,
    }
    events.sort(
        key=lambda item: (
            event_priority.get(item.event_code, 1),
            -item.published_at.timestamp(),
            item.event_id,
        ),
    )
    if len(facts) > MAX_FACTS:
        facts = facts[:MAX_FACTS]
        warnings.add("fact_limit_reached")
    if len(events) > MAX_OFFICIAL_EVENTS:
        events = events[:MAX_OFFICIAL_EVENTS]
        warnings.add("official_event_limit_reached")
    result = SourceLinkedFactSet(
        bundle.fund_code,
        tuple(facts),
        tuple(events),
        tuple(sorted(_stable_code("missing", item) for item in missing)),
        tuple(sorted(_stable_code("conflict", item) for item in conflicts)),
        tuple(sorted(_stable_code("warning", item) for item in warnings)),
    )
    result.validate()
    return result
