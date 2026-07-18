from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    SourceTier,
    canonical_json_bytes,
    validate_identifier,
    validate_public_text,
    validate_public_text_tuple,
)
from kunjin.intelligence.models import (
    DimensionObservation,
    DimensionState,
    EntityAlias,
    EventConfidenceState,
    EventEntityLink,
    EventEntityRelationship,
    EventType,
    IntegrityState,
    LineageEdge,
    LineageKind,
    MarketDimension,
    MarketEntity,
    MarketShadowState,
    MarketStateSnapshot,
    MetricId,
    NewsEvent,
    NewsItem,
    SectorShadowState,
)
from kunjin.intelligence.parsers import ParsedItem, ParsedSectorMarketRow
from kunjin.intelligence.policy import IntelligencePolicyV1

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCE_TIERS = {
    "eastmoney_market": SourceTier.TIER_2,
    "fund_manager_official_documents": SourceTier.TIER_1,
    "gov_cn_policy": SourceTier.TIER_1,
    "stcn_fund_news": SourceTier.TIER_2,
}
_EVENT_TYPES = {item.value: item for item in EventType}


def _normalize_phrase(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _stable_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _canonical_id(prefix: str, payload: dict) -> str:
    return f"{prefix}_{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:24]}"


def _validate_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an aware UTC datetime")
    return value


def _exact_phrase_present(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    escaped = re.escape(phrase)
    if _FUND_CODE.fullmatch(phrase):
        return re.search(rf"(?<![0-9]){escaped}(?![0-9])", text, flags=re.ASCII) is not None
    if phrase.isascii() and all(character.isalnum() or character.isspace() for character in phrase):
        return (
            re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", text, flags=re.ASCII)
            is not None
        )
    return phrase in text


def _ascending_identifiers(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class PublicFundContext:
    fund_code: str
    canonical_name: str
    benchmark_terms: Tuple[str, ...]
    disclosed_security_names: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    holdings_period: Optional[date]
    holdings_coverage: str

    def validate(self) -> None:
        if type(self.fund_code) is not str or _FUND_CODE.fullmatch(self.fund_code) is None:
            raise ValueError("public fund context requires an exact six-digit fund code")
        validate_public_text(self.canonical_name, "public fund canonical name")
        validate_public_text_tuple(self.benchmark_terms, "public fund benchmark terms")
        validate_public_text_tuple(
            self.disclosed_security_names, "public fund disclosed security names"
        )
        if len(set(self.benchmark_terms)) != len(self.benchmark_terms):
            raise ValueError("public fund benchmark terms must be unique")
        if len(set(self.disclosed_security_names)) != len(self.disclosed_security_names):
            raise ValueError("public fund disclosed security names must be unique")
        if type(self.evidence_ids) is not tuple:
            raise ValueError("public fund evidence ids must be an exact tuple")
        for evidence_id in self.evidence_ids:
            validate_identifier(evidence_id, "public fund evidence id")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("public fund evidence ids must be unique")
        if self.holdings_period is not None and type(self.holdings_period) is not date:
            raise ValueError("holdings period must be an exact date or None")
        validate_public_text(self.holdings_coverage, "holdings coverage")
        if self.disclosed_security_names and self.holdings_period is None:
            raise ValueError("disclosed securities require a holdings period")


@dataclass(frozen=True)
class EntityBindingResult:
    item_id: str
    entity_ids: Tuple[str, ...]
    evidence_ids: Tuple[str, ...]
    ambiguous_aliases: Tuple[str, ...]

    def validate(self) -> None:
        validate_identifier(self.item_id, "binding item id")
        for values, name in (
            (self.entity_ids, "binding entity ids"),
            (self.evidence_ids, "binding evidence ids"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} must be an exact tuple")
            for value in values:
                validate_identifier(value, name)
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"{name} must be unique and ascending")
        validate_public_text_tuple(self.ambiguous_aliases, "ambiguous aliases")
        if tuple(sorted(set(self.ambiguous_aliases))) != self.ambiguous_aliases:
            raise ValueError("ambiguous aliases must be unique and ascending")


@dataclass(frozen=True)
class MarketBatch:
    source_attempt_id: int
    rows: Tuple[ParsedSectorMarketRow, ...]
    retrieved_at: datetime

    def validate(self) -> None:
        if type(self.source_attempt_id) is not int or self.source_attempt_id <= 0:
            raise ValueError("market batch source attempt id must be positive")
        _validate_utc(self.retrieved_at, "market batch retrieval time")
        if type(self.rows) is not tuple:
            raise ValueError("market batch rows must be an exact tuple")
        codes = []
        for row in self.rows:
            if type(row) is not ParsedSectorMarketRow:
                raise ValueError("market batch rows must be exact parsed market rows")
            if row.retrieved_at != self.retrieved_at:
                raise ValueError("market batch row retrieval times must equal the batch time")
            codes.append((row.sector_kind, row.sector_code))
        if len(codes) != len(set(codes)):
            raise ValueError("market batch rows must not repeat sector identities")


def news_item_from_parsed(
    parsed: ParsedItem,
    source_attempt_id: int,
    excerpt_expires_at: datetime,
) -> NewsItem:
    if type(parsed) is not ParsedItem:
        raise ValueError("parsed item must be an exact ParsedItem")
    source_tier = _SOURCE_TIERS.get(parsed.source_id)
    if source_tier is None:
        raise ValueError("parsed item source is not reviewed by Policy V1")
    item = NewsItem(
        item_id=_stable_id(
            "news_item",
            parsed.source_id,
            parsed.canonical_url,
            parsed.content_fingerprint,
        ),
        source_id=parsed.source_id,
        publisher=parsed.attributed_publisher,
        canonical_url=parsed.canonical_url,
        title=parsed.title,
        excerpt=parsed.excerpt,
        excerpt_truncated=parsed.excerpt_truncated,
        excerpt_original_bytes=parsed.excerpt_original_bytes,
        excerpt_expires_at=excerpt_expires_at,
        excerpt_expired_at=None,
        published_at=parsed.published_at,
        publication_precision=parsed.publication_precision,
        publication_interval_end=parsed.publication_interval_end,
        retrieved_at=parsed.retrieved_at,
        source_tier=source_tier,
        content_fingerprint=parsed.content_fingerprint,
        category=parsed.category,
        integrity_state=IntegrityState.ACTIVE,
        source_attempt_id=source_attempt_id,
    )
    item.validate()
    return item


def bind_public_entities(
    item: NewsItem,
    entities: Tuple[MarketEntity, ...],
    aliases: Tuple[EntityAlias, ...],
) -> EntityBindingResult:
    item.validate()
    if type(entities) is not tuple or type(aliases) is not tuple:
        raise ValueError("entities and aliases must be exact tuples")
    entity_by_id = {}
    for entity in entities:
        entity.validate()
        if entity.entity_id in entity_by_id:
            raise ValueError("entity registry contains duplicate ids")
        entity_by_id[entity.entity_id] = entity

    text = _normalize_phrase(" ".join(part for part in (item.title, item.excerpt) if part))
    matched_ids = set()
    evidence_ids = {item.item_id}
    ambiguous = set()

    phrase_entities = {}
    phrase_evidence = {}
    for entity in entities:
        if not (
            entity.active_from <= item.published_at
            and (entity.active_until is None or item.published_at < entity.active_until)
        ):
            continue
        phrase = _normalize_phrase(entity.canonical_name)
        phrase_entities.setdefault(phrase, set()).add(entity.entity_id)
        phrase_evidence.setdefault((phrase, entity.entity_id), set()).update(
            entity.evidence_ids
        )
    for alias in aliases:
        alias.validate()
        entity = entity_by_id.get(alias.entity_id)
        if entity is None:
            raise ValueError("entity alias does not resolve to a public entity")
        if not (
            alias.active_from <= item.published_at
            and (alias.active_until is None or item.published_at < alias.active_until)
            and entity.active_from <= item.published_at
            and (entity.active_until is None or item.published_at < entity.active_until)
        ):
            continue
        phrase = _normalize_phrase(alias.alias)
        phrase_entities.setdefault(phrase, set()).add(alias.entity_id)
        phrase_evidence.setdefault((phrase, alias.entity_id), set()).update(
            (*entity.evidence_ids, *alias.evidence_ids)
        )
    for phrase, candidate_ids in phrase_entities.items():
        if not _exact_phrase_present(text, phrase):
            continue
        if len(candidate_ids) > 1:
            ambiguous.add(phrase)
            continue
        entity_id = next(iter(candidate_ids))
        matched_ids.add(entity_id)
        evidence_ids.update(phrase_evidence[(phrase, entity_id)])

    result = EntityBindingResult(
        item_id=item.item_id,
        entity_ids=_ascending_identifiers(matched_ids),
        evidence_ids=_ascending_identifiers(evidence_ids),
        ambiguous_aliases=tuple(sorted(ambiguous)),
    )
    result.validate()
    return result


def build_lineage(items: Tuple[NewsItem, ...]) -> Tuple[LineageEdge, ...]:
    if type(items) is not tuple:
        raise ValueError("lineage items must be an exact tuple")
    item_ids = set()
    groups = {}
    for item in items:
        item.validate()
        if item.item_id in item_ids:
            raise ValueError("lineage items must have unique ids")
        item_ids.add(item.item_id)
        groups.setdefault(item.content_fingerprint, []).append(item)

    edges = []
    for fingerprint, members in groups.items():
        if len(members) < 2:
            continue
        ordered = sorted(
            members,
            key=lambda item: (
                item.published_at,
                0 if item.source_tier is SourceTier.TIER_1 else 1,
                item.item_id,
            ),
        )
        original = ordered[0]
        if original.source_tier is not SourceTier.TIER_1:
            continue
        for reprint in ordered[1:]:
            edge = LineageEdge(
                edge_id=_stable_id("lineage", fingerprint, reprint.item_id, original.item_id),
                from_item_id=reprint.item_id,
                to_item_id=original.item_id,
                kind=LineageKind.REPRINT,
                evidence_ids=tuple(sorted((original.item_id, reprint.item_id))),
            )
            edge.validate()
            edges.append(edge)
    return tuple(sorted(edges, key=lambda edge: edge.edge_id))


def _event_type(category: str) -> EventType:
    try:
        return _EVENT_TYPES[category]
    except KeyError:
        raise ValueError("news category is not a controlled event type") from None


def build_events(
    items: Tuple[NewsItem, ...],
    bindings: Tuple[EntityBindingResult, ...],
    edges: Tuple[LineageEdge, ...],
) -> Tuple[NewsEvent, ...]:
    if type(items) is not tuple or type(bindings) is not tuple or type(edges) is not tuple:
        raise ValueError("event analysis inputs must be exact tuples")
    item_by_id = {}
    for item in items:
        item.validate()
        if item.item_id in item_by_id:
            raise ValueError("event items must have unique ids")
        item_by_id[item.item_id] = item
    binding_by_id = {}
    for binding in bindings:
        binding.validate()
        if binding.item_id not in item_by_id or binding.item_id in binding_by_id:
            raise ValueError("event binding does not resolve uniquely")
        binding_by_id[binding.item_id] = binding
    reprint_ids = set()
    for edge in edges:
        edge.validate()
        if edge.from_item_id not in item_by_id or edge.to_item_id not in item_by_id:
            raise ValueError("lineage edge does not resolve to event items")
        if edge.kind is LineageKind.REPRINT:
            reprint_ids.add(edge.from_item_id)

    groups = {}
    for item in items:
        if item.integrity_state is not IntegrityState.ACTIVE:
            continue
        binding = binding_by_id.get(item.item_id)
        entity_ids = () if binding is None else binding.entity_ids
        title = _normalize_phrase(item.title)
        key = (_event_type(item.category), title, entity_ids)
        groups.setdefault(key, []).append(item)

    result = []
    for (event_type, title, entity_ids), members in groups.items():
        supporting_ids = tuple(sorted(item.item_id for item in members))
        independent_tier_one = {
            (item.publisher, item.content_fingerprint)
            for item in members
            if item.source_tier is SourceTier.TIER_1 and item.item_id not in reprint_ids
        }
        confidence = (
            EventConfidenceState.SUFFICIENT
            if len(independent_tier_one) >= 2
            else EventConfidenceState.PARTIAL
        )
        earliest = min(item.published_at for item in members)
        latest = max(item.published_at for item in members)
        invalidation_conditions = ("出现官方更正、撤稿或后续冲突证据",)
        event_id = _canonical_id(
            "event",
            {
                "confidence_state": confidence.value,
                "correction_item_ids": [],
                "earliest_published_at": earliest,
                "event_type": event_type.value,
                "integrity_state": IntegrityState.ACTIVE.value,
                "invalidation_conditions": list(invalidation_conditions),
                "latest_published_at": latest,
                "normalized_title": title,
                "opposing_item_ids": [],
                "retraction_item_ids": [],
                "supporting_item_ids": list(supporting_ids),
                "superseded_by_event_id": None,
            },
        )
        event = NewsEvent(
            event_id=event_id,
            event_type=event_type,
            normalized_title=title,
            supporting_item_ids=supporting_ids,
            opposing_item_ids=(),
            correction_item_ids=(),
            retraction_item_ids=(),
            confidence_state=confidence,
            earliest_published_at=earliest,
            latest_published_at=latest,
            integrity_state=IntegrityState.ACTIVE,
            superseded_by_event_id=None,
            invalidation_conditions=invalidation_conditions,
        )
        event.validate()
        result.append(event)
    return tuple(sorted(result, key=lambda event: event.event_id))


def build_fund_relevance(
    events: Tuple[NewsEvent, ...],
    context: PublicFundContext,
    entities: Tuple[MarketEntity, ...],
) -> Tuple[EventEntityLink, ...]:
    context.validate()
    if type(events) is not tuple or type(entities) is not tuple:
        raise ValueError("fund relevance inputs must be exact tuples")
    by_name = {}
    for entity in entities:
        entity.validate()
        by_name.setdefault(_normalize_phrase(entity.canonical_name), []).append(entity)

    fund_candidates = by_name.get(_normalize_phrase(context.canonical_name), ())
    requested = (
        *(
            (term, EventEntityRelationship.FUND_BENCHMARK_EXPOSURE)
            for term in context.benchmark_terms
        ),
        *(
            (name, EventEntityRelationship.FUND_HOLDING_EXPOSURE)
            for name in context.disclosed_security_names
        ),
    )
    links = []
    for event in events:
        event.validate()
        text = _normalize_phrase(event.normalized_title)
        event_evidence = (
            event.supporting_item_ids
            + event.opposing_item_ids
            + event.correction_item_ids
            + event.retraction_item_ids
        )
        if len(fund_candidates) == 1 and (
            _exact_phrase_present(text, _normalize_phrase(context.canonical_name))
            or _exact_phrase_present(text, context.fund_code)
        ):
            fund = fund_candidates[0]
            link = EventEntityLink(
                link_id=_stable_id(
                    "event_link",
                    event.event_id,
                    fund.entity_id,
                    EventEntityRelationship.SUBJECT.value,
                ),
                event_id=event.event_id,
                entity_id=fund.entity_id,
                relationship=EventEntityRelationship.SUBJECT,
                evidence_ids=_ascending_identifiers(event_evidence),
            )
            link.validate()
            links.append(link)
        for phrase, relationship in requested:
            normalized = _normalize_phrase(phrase)
            if not _exact_phrase_present(text, normalized):
                continue
            candidates = by_name.get(normalized, ())
            if len(candidates) != 1:
                continue
            entity = candidates[0]
            evidence_ids = _ascending_identifiers(event_evidence)
            link = EventEntityLink(
                link_id=_stable_id(
                    "event_link", event.event_id, entity.entity_id, relationship.value
                ),
                event_id=event.event_id,
                entity_id=entity.entity_id,
                relationship=relationship,
                evidence_ids=evidence_ids,
            )
            link.validate()
            links.append(link)
    return tuple(sorted(links, key=lambda link: link.link_id))


def _metric_state(value: Decimal, positive: Decimal, negative: Decimal) -> DimensionState:
    if value >= positive:
        return DimensionState.POSITIVE
    if value <= negative:
        return DimensionState.NEGATIVE
    return DimensionState.NEUTRAL


def _fresh(batch_time: datetime, as_of: datetime, policy: IntelligencePolicyV1) -> bool:
    local_batch = batch_time.astimezone(_SHANGHAI).date()
    local_as_of = as_of.astimezone(_SHANGHAI).date()
    return 0 <= (local_as_of - local_batch).days <= policy.market_max_age_days


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _ranked_codes_at_or_above(
    rows: Sequence[ParsedSectorMarketRow],
    field_name: str,
    percentile: Decimal,
) -> set[str]:
    ordered = sorted(
        rows,
        key=lambda row: (getattr(row, field_name), row.sector_code),
    )
    total = Decimal(len(ordered))
    return {
        row.sector_code
        for rank, row in enumerate(ordered, start=1)
        if Decimal(rank) / total >= percentile
    }


def _observation(
    *,
    entity_id: str,
    dimension: MarketDimension,
    metric_id: Optional[MetricId],
    state: DimensionState,
    value: Optional[Decimal],
    unit: Optional[str],
    data_as_of: datetime,
    retrieved_at: datetime,
    source_tier: SourceTier,
    source_attempt_ids: Tuple[int, ...],
    evidence_ids: Tuple[str, ...] = (),
    freshness: EvidenceFreshness = EvidenceFreshness.CURRENT,
    conflict_ids: Tuple[str, ...] = (),
) -> DimensionObservation:
    completeness = (
        EvidenceCompleteness.INSUFFICIENT
        if state is DimensionState.INSUFFICIENT_DATA
        else EvidenceCompleteness.PARTIAL
        if state is DimensionState.CONFLICTED
        else EvidenceCompleteness.COMPLETE
    )
    canonical_identity = {
        "completeness": completeness.value,
        "conflict_ids": list(_ascending_identifiers(conflict_ids)),
        "data_as_of": data_as_of,
        "dimension": dimension.value,
        "entity_id": entity_id,
        "evidence_ids": list(_ascending_identifiers(evidence_ids)),
        "freshness": freshness.value,
        "metric_id": None if metric_id is None else metric_id.value,
        "retrieved_at": retrieved_at,
        "source_attempt_ids": list(tuple(sorted(set(source_attempt_ids)))),
        "source_tier": source_tier.value,
        "state": state.value,
        "unit": unit,
        "value": value,
    }
    observation = DimensionObservation(
        observation_id=_canonical_id("observation", canonical_identity),
        entity_id=entity_id,
        dimension=dimension,
        metric_id=metric_id,
        state=state,
        value=value,
        unit=unit,
        data_as_of=data_as_of,
        retrieved_at=retrieved_at,
        source_tier=source_tier,
        source_attempt_ids=tuple(sorted(set(source_attempt_ids))),
        evidence_ids=_ascending_identifiers(evidence_ids),
        freshness=freshness,
        completeness=completeness,
        conflict_ids=_ascending_identifiers(conflict_ids),
    )
    observation.validate()
    return observation


def _insufficient(
    *,
    dimension: MarketDimension,
    metric_id: Optional[MetricId],
    latest: MarketBatch,
    freshness: EvidenceFreshness,
) -> DimensionObservation:
    return _observation(
        entity_id="market_cn",
        dimension=dimension,
        metric_id=metric_id,
        state=DimensionState.INSUFFICIENT_DATA,
        value=None,
        unit=None,
        data_as_of=latest.retrieved_at,
        retrieved_at=latest.retrieved_at,
        source_tier=SourceTier.TIER_2,
        source_attempt_ids=(latest.source_attempt_id,),
        freshness=freshness,
    )


def _coverage(eligible: int, total: int) -> Decimal:
    if total == 0:
        return Decimal(0)
    return Decimal(eligible) / Decimal(total)


def _build_trend(
    latest: MarketBatch, as_of: datetime, policy: IntelligencePolicyV1
) -> Tuple[DimensionObservation, DimensionObservation]:
    rows = tuple(row for row in latest.rows if row.sector_kind == "industry")
    freshness = (
        EvidenceFreshness.CURRENT
        if _fresh(latest.retrieved_at, as_of, policy)
        else EvidenceFreshness.STALE
    )
    base_eligible = len(rows) >= policy.minimum_sectors and freshness is EvidenceFreshness.CURRENT
    change_rows = tuple(row for row in rows if row.pct_change is not None)
    breadth_rows = tuple(
        row
        for row in rows
        if row.advancers is not None
        and row.decliners is not None
        and row.advancers + row.decliners > 0
    )
    if not base_eligible or _coverage(len(change_rows), len(rows)) < policy.minimum_field_coverage:
        change = _insufficient(
            dimension=MarketDimension.TREND_BREADTH,
            metric_id=MetricId.INDUSTRY_MEDIAN_PCT_CHANGE,
            latest=latest,
            freshness=freshness,
        )
    else:
        value = _median(tuple(row.pct_change for row in change_rows if row.pct_change is not None))
        change = _observation(
            entity_id="market_cn",
            dimension=MarketDimension.TREND_BREADTH,
            metric_id=MetricId.INDUSTRY_MEDIAN_PCT_CHANGE,
            state=_metric_state(value, policy.positive_change, policy.negative_change),
            value=value,
            unit="percentage_points",
            data_as_of=latest.retrieved_at,
            retrieved_at=latest.retrieved_at,
            source_tier=SourceTier.TIER_2,
            source_attempt_ids=(latest.source_attempt_id,),
            freshness=freshness,
        )
    if not base_eligible or _coverage(len(breadth_rows), len(rows)) < policy.minimum_field_coverage:
        breadth = _insufficient(
            dimension=MarketDimension.TREND_BREADTH,
            metric_id=MetricId.INDUSTRY_AGGREGATE_BREADTH,
            latest=latest,
            freshness=freshness,
        )
    else:
        advancing = sum(row.advancers for row in breadth_rows if row.advancers is not None)
        total = sum(
            row.advancers + row.decliners
            for row in breadth_rows
            if row.advancers is not None and row.decliners is not None
        )
        value = Decimal(advancing) / Decimal(total)
        breadth = _observation(
            entity_id="market_cn",
            dimension=MarketDimension.TREND_BREADTH,
            metric_id=MetricId.INDUSTRY_AGGREGATE_BREADTH,
            state=_metric_state(value, policy.positive_breadth, policy.negative_breadth),
            value=value,
            unit="decimal_fraction",
            data_as_of=latest.retrieved_at,
            retrieved_at=latest.retrieved_at,
            source_tier=SourceTier.TIER_2,
            source_attempt_ids=(latest.source_attempt_id,),
            freshness=freshness,
        )
    return change, breadth


def _build_crowding(
    latest: MarketBatch, as_of: datetime, policy: IntelligencePolicyV1
) -> DimensionObservation:
    freshness = (
        EvidenceFreshness.CURRENT
        if _fresh(latest.retrieved_at, as_of, policy)
        else EvidenceFreshness.STALE
    )
    return _insufficient(
        dimension=MarketDimension.CROWDING,
        metric_id=MetricId.INDUSTRY_OVERHEATING_SHARE,
        latest=latest,
        freshness=freshness,
    )


def _build_flow(
    batches: Tuple[MarketBatch, ...],
    latest: MarketBatch,
    as_of: datetime,
    policy: IntelligencePolicyV1,
) -> DimensionObservation:
    eligible_batches = []
    for batch in sorted(batches, key=lambda value: value.retrieved_at, reverse=True):
        if not _fresh(batch.retrieved_at, as_of, policy):
            continue
        rows = tuple(row for row in batch.rows if row.sector_kind == "industry")
        flow_rows = tuple(row for row in rows if row.main_net_inflow_ratio is not None)
        if (
            len(rows) >= policy.minimum_sectors
            and _coverage(len(flow_rows), len(rows)) >= policy.minimum_field_coverage
        ):
            eligible_batches.append((batch, flow_rows))
    selected = eligible_batches[: policy.flow_observations]
    if len(selected) < policy.flow_observations or len(
        {batch.retrieved_at for batch, _rows in selected}
    ) < policy.flow_observations:
        freshness = (
            EvidenceFreshness.CURRENT
            if _fresh(latest.retrieved_at, as_of, policy)
            else EvidenceFreshness.STALE
        )
        return _insufficient(
            dimension=MarketDimension.PERSISTENT_FLOW,
            metric_id=MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
            latest=latest,
            freshness=freshness,
        )
    shares = tuple(
        Decimal(sum(1 for row in rows if row.main_net_inflow_ratio > 0)) / Decimal(len(rows))
        for _batch, rows in selected
    )
    state = (
        DimensionState.POSITIVE
        if all(value >= policy.positive_breadth for value in shares)
        else DimensionState.NEGATIVE
        if all(value <= policy.negative_breadth for value in shares)
        else DimensionState.NEUTRAL
    )
    attempts = tuple(sorted(batch.source_attempt_id for batch, _rows in selected))
    return _observation(
        entity_id="market_cn",
        dimension=MarketDimension.PERSISTENT_FLOW,
        metric_id=MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
        state=state,
        value=None,
        unit=None,
        data_as_of=max(batch.retrieved_at for batch, _rows in selected),
        retrieved_at=max(batch.retrieved_at for batch, _rows in selected),
        source_tier=SourceTier.TIER_2,
        source_attempt_ids=attempts,
        freshness=EvidenceFreshness.CURRENT,
    )


def _build_catalyst(
    events: Tuple[NewsEvent, ...],
    items: Tuple[NewsItem, ...],
    bindings: Tuple[EntityBindingResult, ...],
    latest: MarketBatch,
    as_of: datetime,
    policy: IntelligencePolicyV1,
) -> DimensionObservation:
    item_by_id = {item.item_id: item for item in items}
    if len(item_by_id) != len(items):
        raise ValueError("catalyst items must have unique ids")
    for item in items:
        item.validate()
    binding_by_item_id = {}
    for binding in bindings:
        binding.validate()
        if binding.item_id not in item_by_id or binding.item_id in binding_by_item_id:
            raise ValueError("catalyst binding must resolve uniquely to a news item")
        binding_by_item_id[binding.item_id] = binding
    positive_events = []
    negative_events = []
    evidence_ids = set()
    attempts = set()
    for event in events:
        event.validate()
        if (
            event.integrity_state is not IntegrityState.ACTIVE
            or event.opposing_item_ids
            or event.correction_item_ids
            or event.retraction_item_ids
            or event.confidence_state
            not in {EventConfidenceState.PARTIAL, EventConfidenceState.SUFFICIENT}
            or event.latest_published_at > as_of
            or as_of - event.latest_published_at > timedelta(seconds=policy.recent_seconds)
        ):
            continue
        supporting = []
        unresolved = False
        for item_id in event.supporting_item_ids:
            item = item_by_id.get(item_id)
            if item is None:
                unresolved = True
                break
            supporting.append(item)
        if unresolved:
            continue
        tier_one = tuple(
            item
            for item in supporting
            if item.source_tier is SourceTier.TIER_1
            and item.integrity_state is IntegrityState.ACTIVE
            and item.published_at <= as_of
            and as_of - item.published_at <= timedelta(seconds=policy.recent_seconds)
            and item.item_id in binding_by_item_id
            and bool(binding_by_item_id[item.item_id].entity_ids)
            and not binding_by_item_id[item.item_id].ambiguous_aliases
        )
        if not tier_one:
            continue
        supports = False
        restricts = False
        for item in tier_one:
            public_text = _normalize_phrase(
                " ".join(part for part in (item.title, item.excerpt) if part)
            )
            item_supports = any(
                _exact_phrase_present(public_text, phrase) for phrase in policy.support_phrases
            )
            item_restricts = any(
                _exact_phrase_present(public_text, phrase)
                for phrase in policy.restriction_phrases
            )
            supports = supports or item_supports
            restricts = restricts or item_restricts
            evidence_ids.add(item.item_id)
            attempts.add(item.source_attempt_id)
        if supports:
            positive_events.append(event.event_id)
        if restricts:
            negative_events.append(event.event_id)
    if not attempts:
        return _insufficient(
            dimension=MarketDimension.CATALYSTS,
            metric_id=MetricId.AUTHENTICATED_EVENT_DIRECTION,
            latest=latest,
            freshness=EvidenceFreshness.UNKNOWN,
        )
    if positive_events and negative_events:
        state = DimensionState.CONFLICTED
        conflict_ids = tuple(sorted(set(positive_events + negative_events)))
    elif positive_events:
        state = DimensionState.POSITIVE
        conflict_ids = ()
    elif negative_events:
        state = DimensionState.NEGATIVE
        conflict_ids = ()
    elif evidence_ids:
        state = DimensionState.NEUTRAL
        conflict_ids = ()
    else:
        return _insufficient(
            dimension=MarketDimension.CATALYSTS,
            metric_id=MetricId.AUTHENTICATED_EVENT_DIRECTION,
            latest=latest,
            freshness=EvidenceFreshness.UNKNOWN,
        )
    evidence_items = tuple(item_by_id[item_id] for item_id in evidence_ids)
    retrieval = max(item.retrieved_at for item in evidence_items)
    return _observation(
        entity_id="market_cn",
        dimension=MarketDimension.CATALYSTS,
        metric_id=MetricId.AUTHENTICATED_EVENT_DIRECTION,
        state=state,
        value=None,
        unit=None,
        data_as_of=max(item.published_at for item in evidence_items),
        retrieved_at=retrieval,
        source_tier=SourceTier.TIER_1,
        source_attempt_ids=tuple(sorted(attempts)),
        evidence_ids=tuple(sorted(evidence_ids)),
        freshness=EvidenceFreshness.CURRENT,
        conflict_ids=conflict_ids,
    )


def _overall_market_state(dimensions: Tuple[DimensionObservation, ...]) -> MarketShadowState:
    by_metric = {item.metric_id: item for item in dimensions}
    change = by_metric[MetricId.INDUSTRY_MEDIAN_PCT_CHANGE]
    breadth = by_metric[MetricId.INDUSTRY_AGGREGATE_BREADTH]
    if DimensionState.INSUFFICIENT_DATA in {change.state, breadth.state}:
        return MarketShadowState.INSUFFICIENT_DATA
    trend = (
        DimensionState.POSITIVE
        if change.state is DimensionState.POSITIVE and breadth.state is DimensionState.POSITIVE
        else DimensionState.NEGATIVE
        if change.state is DimensionState.NEGATIVE and breadth.state is DimensionState.NEGATIVE
        else DimensionState.NEUTRAL
    )
    other = [
        by_metric[MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D],
        by_metric[MetricId.AUTHENTICATED_EVENT_DIRECTION],
        by_metric[MetricId.INDUSTRY_OVERHEATING_SHARE],
    ]
    eligible = [
        item
        for item in other
        if item.state not in {DimensionState.INSUFFICIENT_DATA, DimensionState.CONFLICTED}
    ]
    flow_or_catalyst = other[:2]
    if len(eligible) < 2 or not any(
        item.state not in {DimensionState.INSUFFICIENT_DATA, DimensionState.CONFLICTED}
        for item in flow_or_catalyst
    ):
        return MarketShadowState.INSUFFICIENT_DATA
    other_states = [item.state for item in eligible]
    if (
        trend is DimensionState.POSITIVE
        and sum(state is DimensionState.POSITIVE for state in other_states) >= 2
        and not any(state is DimensionState.NEGATIVE for state in other_states)
        and not any(state is DimensionState.RISK_FLAG for state in other_states)
    ):
        return MarketShadowState.OFFENSIVE_BIAS
    if trend is DimensionState.NEGATIVE and sum(
        state is DimensionState.NEGATIVE for state in other_states
    ) >= 2:
        return MarketShadowState.DEFENSIVE_BIAS
    return MarketShadowState.NEUTRAL


def _sector_states(
    batches: Tuple[MarketBatch, ...],
    latest: MarketBatch,
    as_of: datetime,
    policy: IntelligencePolicyV1,
) -> Tuple[Tuple[str, SectorShadowState], ...]:
    if not _fresh(latest.retrieved_at, as_of, policy):
        return ()
    history_by_code = {}
    for batch in sorted(batches, key=lambda value: value.retrieved_at, reverse=True):
        if not _fresh(batch.retrieved_at, as_of, policy):
            continue
        for row in batch.rows:
            if row.sector_kind == "industry":
                history_by_code.setdefault(row.sector_code, []).append(row)
    latest_industry = tuple(row for row in latest.rows if row.sector_kind == "industry")
    crowding_rows = tuple(
        row
        for row in latest_industry
        if row.pct_change is not None and row.turnover_rate is not None
    )
    crowding_eligible = (
        len(latest_industry) >= policy.minimum_sectors
        and _coverage(len(crowding_rows), len(latest_industry))
        >= policy.minimum_field_coverage
    )
    if crowding_eligible:
        return_codes = _ranked_codes_at_or_above(
            crowding_rows,
            "pct_change",
            policy.crowding_percentile,
        )
        turnover_codes = _ranked_codes_at_or_above(
            crowding_rows,
            "turnover_rate",
            policy.crowding_percentile,
        )
    else:
        return_codes = set()
        turnover_codes = set()
    result = []
    for row in latest.rows:
        if row.sector_kind != "industry":
            continue
        sector_id = _stable_id("sector", row.sector_code)
        if (
            row.pct_change is None
            or row.advancers is None
            or row.decliners is None
            or row.advancers + row.decliners == 0
        ):
            result.append((sector_id, SectorShadowState.INSUFFICIENT_DATA))
            continue
        breadth = Decimal(row.advancers) / Decimal(row.advancers + row.decliners)
        trend = (
            DimensionState.POSITIVE
            if row.pct_change >= policy.positive_change and breadth >= policy.positive_breadth
            else DimensionState.NEGATIVE
            if row.pct_change <= policy.negative_change and breadth <= policy.negative_breadth
            else DimensionState.NEUTRAL
        )
        history = history_by_code.get(row.sector_code, ())[: policy.flow_observations]
        flow = None
        if (
            len(history) == policy.flow_observations
            and len({item.retrieved_at for item in history}) == policy.flow_observations
            and all(
                item.main_net_inflow_ratio is not None for item in history
            )
        ):
            if all(item.main_net_inflow_ratio > 0 for item in history):
                flow = DimensionState.POSITIVE
            elif all(item.main_net_inflow_ratio < 0 for item in history):
                flow = DimensionState.NEGATIVE
            else:
                flow = DimensionState.NEUTRAL
        crowding_risk = (
            crowding_eligible
            and row.sector_code in return_codes
            and row.sector_code in turnover_codes
        )
        if crowding_risk and trend is DimensionState.POSITIVE:
            result.append((sector_id, SectorShadowState.OVERHEATING_RISK))
        elif flow is None or not crowding_eligible or row.turnover_rate is None:
            result.append((sector_id, SectorShadowState.INSUFFICIENT_DATA))
        elif trend is DimensionState.POSITIVE and flow is DimensionState.POSITIVE:
            result.append((sector_id, SectorShadowState.IMPROVING))
        elif trend is DimensionState.NEGATIVE and flow is DimensionState.NEGATIVE:
            result.append((sector_id, SectorShadowState.WEAKENING))
        else:
            result.append((sector_id, SectorShadowState.NEUTRAL))
    return tuple(sorted(result))


def build_market_state(
    batches: Tuple[MarketBatch, ...],
    events: Tuple[NewsEvent, ...],
    items: Tuple[NewsItem, ...],
    bindings: Tuple[EntityBindingResult, ...],
    as_of: datetime,
    policy: IntelligencePolicyV1,
) -> MarketStateSnapshot:
    if type(batches) is not tuple or not batches:
        raise ValueError("market state requires at least one exact market batch")
    if type(events) is not tuple or type(items) is not tuple or type(bindings) is not tuple:
        raise ValueError("market events, items, and bindings must be exact tuples")
    _validate_utc(as_of, "market state as-of time")
    policy.validate()
    for batch in batches:
        batch.validate()
        if batch.retrieved_at > as_of:
            raise ValueError("market batch cannot be retrieved after the state as-of time")
    if len({batch.source_attempt_id for batch in batches}) != len(batches):
        raise ValueError("market batches must use unique source attempt ids")
    latest = max(batches, key=lambda batch: batch.retrieved_at)

    trend = _build_trend(latest, as_of, policy)
    flow = _build_flow(batches, latest, as_of, policy)
    catalyst = _build_catalyst(events, items, bindings, latest, as_of, policy)
    crowding = _build_crowding(latest, as_of, policy)
    unsupported = tuple(
        _insufficient(
            dimension=dimension,
            metric_id=None,
            latest=latest,
            freshness=EvidenceFreshness.UNKNOWN,
        )
        for dimension in (
            MarketDimension.VALUATION,
            MarketDimension.FUNDAMENTALS_EARNINGS,
        )
    )
    dimensions = (*trend, flow, catalyst, crowding, *unsupported)
    unknown_dimensions = tuple(
        dimension
        for dimension in MarketDimension
        if any(
            item.dimension is dimension
            and item.state is DimensionState.INSUFFICIENT_DATA
            for item in dimensions
        )
    )
    supporting = tuple(
        sorted(
            item.observation_id
            for item in dimensions
            if item.state is DimensionState.POSITIVE
        )
    )
    opposing = tuple(
        sorted(
            item.observation_id
            for item in dimensions
            if item.state in {DimensionState.NEGATIVE, DimensionState.RISK_FLAG}
        )
    )
    snapshot = MarketStateSnapshot(
        market_state=_overall_market_state(dimensions),
        sector_states=_sector_states(batches, latest, as_of, policy),
        dimensions=dimensions,
        supporting_observation_ids=supporting,
        opposing_observation_ids=opposing,
        unknown_dimensions=unknown_dimensions,
        invalidation_conditions=(
            "市场批次超过五个自然日未更新",
            "market crowding aggregate is deferred under frozen Policy V1",
            "新闻事件出现官方更正、撤稿或冲突证据",
            "当前影子状态不构成交易授权",
        ),
        next_review_at=as_of + timedelta(seconds=policy.current_cache_seconds),
        policy_checksum=policy.checksum(),
    )
    snapshot.validate()
    return snapshot
