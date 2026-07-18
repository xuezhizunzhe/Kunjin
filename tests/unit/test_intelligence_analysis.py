from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.decision.models import EvidenceCompleteness, EvidenceFreshness, SourceTier
from kunjin.intelligence.analysis import (
    EntityBindingResult,
    MarketBatch,
    PublicFundContext,
    bind_public_entities,
    build_events,
    build_fund_relevance,
    build_lineage,
    build_market_state,
    news_item_from_parsed,
)
from kunjin.intelligence.models import (
    DimensionState,
    EntityAlias,
    EventConfidenceState,
    EventEntityRelationship,
    EventType,
    IntegrityState,
    IntelligenceSnapshot,
    IntelligenceWorkflow,
    LineageKind,
    MarketDimension,
    MarketEntity,
    MarketShadowState,
    MetricId,
    NewsEvent,
    NewsItem,
    QueryInterval,
    SectorShadowState,
)
from kunjin.intelligence.parsers import ParsedItem, ParsedSectorMarketRow
from kunjin.intelligence.policy import IntelligencePolicyV1

UTC = timezone.utc
AS_OF = datetime(2026, 7, 18, 6, 0, tzinfo=UTC)


def _parsed(
    *,
    source_id: str = "gov_cn_policy",
    publisher: str = "中国政府网",
    url: str = "https://www.gov.cn/zhengce/content/202607/content_1.htm",
    title: str = "支持长期资金入市",
    content: str = "支持长期资金入市",
    published_at: datetime = AS_OF - timedelta(hours=2),
    lineage_hint: LineageKind = LineageKind.ORIGINAL,
) -> ParsedItem:
    encoded = content.encode("utf-8")
    import hashlib

    return ParsedItem(
        source_id=source_id,
        hosting_publisher=publisher,
        attributed_publisher=publisher,
        canonical_url=url,
        title=title,
        normalized_public_content=content,
        published_at=published_at,
        retrieved_at=AS_OF,
        category="policy",
        lineage_hint=lineage_hint,
        author=None,
        publication_precision="minute",
        publication_interval_end=None,
        excerpt=content,
        excerpt_truncated=False,
        excerpt_original_bytes=len(encoded),
        content_fingerprint=hashlib.sha256(encoded).hexdigest(),
    )


def _item(parsed: ParsedItem, source_attempt_id: int) -> NewsItem:
    return news_item_from_parsed(
        parsed,
        source_attempt_id,
        parsed.retrieved_at + timedelta(days=365),
    )


def _entity(entity_id: str, entity_type: str, name: str) -> MarketEntity:
    return MarketEntity(
        entity_id=entity_id,
        entity_type=entity_type,
        canonical_name=name,
        active_from=datetime(2000, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=(f"{entity_id}_evidence",),
    )


def _alias(entity_id: str, alias: str) -> EntityAlias:
    return EntityAlias(
        entity_id=entity_id,
        alias=alias,
        alias_type="controlled_name",
        active_from=datetime(2000, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=(f"{entity_id}_alias_evidence",),
    )


def _binding(
    item: NewsItem,
    entity_ids: tuple[str, ...] = ("market_cn",),
    ambiguous_aliases: tuple[str, ...] = (),
) -> EntityBindingResult:
    return EntityBindingResult(
        item_id=item.item_id,
        entity_ids=entity_ids,
        evidence_ids=(item.item_id,),
        ambiguous_aliases=ambiguous_aliases,
    )


def _row(
    index: int,
    *,
    pct_change: Decimal | None = Decimal("0.50"),
    turnover_rate: Decimal | None = None,
    main_flow_ratio: Decimal | None = Decimal("1"),
    advancers: int | None = 6,
    decliners: int | None = 4,
    retrieved_at: datetime = AS_OF,
    missing_turnover: bool = False,
) -> ParsedSectorMarketRow:
    return ParsedSectorMarketRow(
        sector_code=f"bk{index:04d}",
        sector_name=f"行业{index:02d}",
        sector_kind="industry",
        pct_change=pct_change,
        turnover_rate=(
            None
            if missing_turnover
            else Decimal(index)
            if turnover_rate is None
            else turnover_rate
        ),
        main_net_inflow=Decimal("100"),
        main_net_inflow_ratio=main_flow_ratio,
        advancers=advancers,
        decliners=decliners,
        retrieved_at=retrieved_at,
    )


def _batch(
    *,
    attempt: int = 11,
    retrieved_at: datetime = AS_OF,
    count: int = 20,
    pct_change: Decimal | None = Decimal("0.50"),
    advancers: int | None = 6,
    decliners: int | None = 4,
    missing: int = 0,
    flow_ratio: Decimal | None = Decimal("1"),
) -> MarketBatch:
    rows = []
    for index in range(1, count + 1):
        is_missing = index <= missing
        rows.append(
            _row(
                index,
                pct_change=None if is_missing else pct_change,
                main_flow_ratio=None if is_missing else flow_ratio,
                advancers=None if is_missing else advancers,
                decliners=None if is_missing else decliners,
                retrieved_at=retrieved_at,
            )
        )
    return MarketBatch(attempt, tuple(rows), retrieved_at)


def _dimension(state, dimension: MarketDimension, metric: MetricId):
    return next(
        item
        for item in state.dimensions
        if item.dimension is dimension and item.metric_id is metric
    )


def test_parsed_item_conversion_is_exact_deterministic_and_validated() -> None:
    parsed = _parsed()
    left = _item(parsed, 7)
    right = _item(parsed, 7)

    assert left == right
    assert left.publisher == parsed.attributed_publisher
    assert left.source_tier is SourceTier.TIER_1
    assert left.excerpt == parsed.excerpt
    assert left.content_fingerprint == parsed.content_fingerprint
    left.validate()

    with pytest.raises(ValueError, match="365"):
        news_item_from_parsed(parsed, 7, AS_OF + timedelta(days=364))
    with pytest.raises(ValueError, match="source"):
        news_item_from_parsed(
            replace(parsed, source_id="unreviewed"),
            7,
            AS_OF + timedelta(days=365),
        )


def test_news_item_identity_changes_when_public_content_changes() -> None:
    left = _item(_parsed(content="支持长期资金入市"), 7)
    right = _item(_parsed(content="限制短期投机资金入市"), 8)
    assert left.canonical_url == right.canonical_url
    assert left.content_fingerprint != right.content_fingerprint
    assert left.item_id != right.item_id


def test_exact_code_name_and_alias_binding_rejects_digit_substrings() -> None:
    fund = _entity("fund_000001", "fund", "华夏成长混合")
    aliases = (_alias(fund.entity_id, "000001"),)
    exact = _item(_parsed(title="000001 华夏成长混合发布季报"), 7)
    embedded = _item(
        _parsed(
            url="https://www.gov.cn/zhengce/content/202607/content_2.htm",
            title="1000001 发布季报",
        ),
        8,
    )

    binding = bind_public_entities(exact, (fund,), aliases)
    assert binding.entity_ids == (fund.entity_id,)
    assert set(binding.evidence_ids) >= {
        exact.item_id,
        "fund_000001_evidence",
        "fund_000001_alias_evidence",
    }
    assert bind_public_entities(embedded, (fund,), aliases).entity_ids == ()


def test_ambiguous_alias_is_reported_and_not_linked() -> None:
    technology = _entity("sector_technology", "sector", "科技行业")
    fund = _entity("fund_000002", "fund", "人工智能主题基金")
    item = _item(_parsed(title="AI政策"), 7)

    result = bind_public_entities(
        item,
        (technology, fund),
        (_alias(technology.entity_id, "AI"), _alias(fund.entity_id, "AI")),
    )

    assert result.entity_ids == ()
    assert result.ambiguous_aliases == ("AI",)


def test_canonical_and_alias_paths_share_one_global_ambiguity_check() -> None:
    canonical = _entity("sector_ai", "sector", "AI")
    aliased = _entity("fund_000003", "fund", "人工智能基金")
    item = _item(_parsed(title="AI政策"), 7)

    result = bind_public_entities(
        item,
        (canonical, aliased),
        (_alias(aliased.entity_id, "AI"),),
    )

    assert result.entity_ids == ()
    assert result.ambiguous_aliases == ("AI",)


def test_reprint_never_counts_as_independent_confirmation() -> None:
    original = _item(_parsed(), 7)
    reprint = _item(
        _parsed(
            source_id="stcn_fund_news",
            publisher="证券时报网",
            url="https://www.stcn.com/article/detail/100.html",
            lineage_hint=LineageKind.REPRINT,
        ),
        8,
    )
    edges = build_lineage((original, reprint))
    events = build_events((original, reprint), (), edges)

    assert len(edges) == 1
    assert edges[0].kind is LineageKind.REPRINT
    assert events[0].confidence_state is not EventConfidenceState.SUFFICIENT
    assert events[0].supporting_item_ids == tuple(sorted((original.item_id, reprint.item_id)))


def test_event_identity_covers_role_items_and_is_stable_for_identical_input() -> None:
    first = _item(_parsed(), 7)
    second = _item(
        _parsed(
            url="https://www.gov.cn/zhengce/content/202607/content_9.htm",
            content="支持长期资金入市的另一份原始文件",
        ),
        8,
    )
    first_binding = _binding(first)
    second_binding = _binding(second)
    first_event = build_events((first,), (first_binding,), ())[0]
    identical = build_events((first,), (first_binding,), ())[0]
    second_event = build_events((second,), (second_binding,), ())[0]

    assert first_event == identical
    assert first_event.event_id == identical.event_id
    assert first_event.supporting_item_ids != second_event.supporting_item_ids
    assert first_event.event_id != second_event.event_id


def test_distinct_fingerprints_do_not_create_independent_lineage() -> None:
    left = _item(_parsed(), 7)
    right = _item(
        _parsed(
            source_id="stcn_fund_news",
            publisher="证券时报网",
            url="https://www.stcn.com/article/detail/101.html",
            content="另一篇独立正文",
        ),
        8,
    )
    assert build_lineage((left, right)) == ()


def test_lineage_never_points_an_earlier_tier_two_item_to_a_future_tier_one_item() -> None:
    early_tier_two = _item(
        _parsed(
            source_id="stcn_fund_news",
            publisher="证券时报网",
            url="https://www.stcn.com/article/detail/103.html",
            published_at=AS_OF - timedelta(hours=3),
        ),
        8,
    )
    later_tier_one = _item(
        _parsed(published_at=AS_OF - timedelta(hours=1)),
        7,
    )
    assert early_tier_two.content_fingerprint == later_tier_one.content_fingerprint
    assert build_lineage((early_tier_two, later_tier_one)) == ()


def test_benchmark_and_top_ten_relevance_stay_distinct_and_evidenced() -> None:
    event = NewsEvent(
        event_id="event_market_update",
        event_type=EventType.MARKET,
        normalized_title="华夏成长混合跟踪沪深300指数并披露贵州茅台",
        supporting_item_ids=("news_item_one",),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.PARTIAL,
        earliest_published_at=AS_OF,
        latest_published_at=AS_OF,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    entities = (
        _entity("fund_000001", "fund", "华夏成长混合"),
        _entity("benchmark_csi300", "benchmark", "沪深300指数"),
        _entity("security_moutai", "security", "贵州茅台"),
    )
    context = PublicFundContext(
        fund_code="000001",
        canonical_name="华夏成长混合",
        benchmark_terms=("沪深300指数",),
        disclosed_security_names=("贵州茅台",),
        evidence_ids=("fund_identity_evidence", "holdings_report_evidence"),
        holdings_period=date(2026, 6, 30),
        holdings_coverage="仅覆盖2026年二季度披露的前十大持仓",
    )

    links = build_fund_relevance((event,), context, entities)
    relationships = {link.relationship for link in links}

    assert relationships == {
        EventEntityRelationship.SUBJECT,
        EventEntityRelationship.FUND_BENCHMARK_EXPOSURE,
        EventEntityRelationship.FUND_HOLDING_EXPOSURE,
    }
    assert context.holdings_period == date(2026, 6, 30)
    assert "前十大持仓" in context.holdings_coverage
    assert all(link.evidence_ids == ("news_item_one",) for link in links)


def test_fund_relevance_closes_inside_authenticated_snapshot_item_ids() -> None:
    parsed = _parsed(title="000001发布季度报告", content="000001发布季度报告")
    item = replace(_item(parsed, 7), category="fund_official")
    item.validate()
    event = NewsEvent(
        event_id="event_fund_snapshot",
        event_type=EventType.FUND_OFFICIAL,
        normalized_title=item.title,
        supporting_item_ids=(item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.PARTIAL,
        earliest_published_at=item.published_at,
        latest_published_at=item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    market = MarketEntity(
        entity_id="market_cn",
        entity_type="market",
        canonical_name="中国公募基金市场",
        active_from=datetime(2001, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=(),
    )
    fund = MarketEntity(
        entity_id="fund_000001",
        entity_type="fund",
        canonical_name="华夏成长混合",
        active_from=datetime(2001, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=(),
    )
    context = PublicFundContext(
        fund_code="000001",
        canonical_name=fund.canonical_name,
        benchmark_terms=(),
        disclosed_security_names=(),
        evidence_ids=("fund_identity_evidence",),
        holdings_period=None,
        holdings_coverage="当前没有可用的季度持仓覆盖",
    )
    links = build_fund_relevance((event,), context, (fund,))
    state = build_market_state(
        (_batch(),), (), (), (), AS_OF, IntelligencePolicyV1()
    )
    snapshot = IntelligenceSnapshot(
        workflow=IntelligenceWorkflow.FUND_INTELLIGENCE,
        request_id="b" * 32,
        request_run_id=2,
        interval=QueryInterval(
            start_at=AS_OF - timedelta(hours=72),
            end_at=AS_OF,
            timezone_name="Asia/Shanghai",
        ),
        subject_fund_code="000001",
        entities=(fund, market),
        item_ids=(item.item_id,),
        source_attempt_ids=(7, 11),
        lineage_edge_ids=(),
        event_ids=(event.event_id,),
        event_entity_links=links,
        market_state=state,
        fund_relevance_link_ids=tuple(link.link_id for link in links),
        conflicts=(),
        missing_evidence=("valuation", "fundamentals_earnings"),
        created_at=AS_OF,
        exact_amount_available=False,
    )
    snapshot.validate()


def test_exact_fund_code_can_create_subject_relevance_without_name_in_title() -> None:
    event = NewsEvent(
        event_id="event_fund_code",
        event_type=EventType.FUND_OFFICIAL,
        normalized_title="000001发布季度报告",
        supporting_item_ids=("news_item_code",),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.PARTIAL,
        earliest_published_at=AS_OF,
        latest_published_at=AS_OF,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    context = PublicFundContext(
        fund_code="000001",
        canonical_name="华夏成长混合",
        benchmark_terms=(),
        disclosed_security_names=(),
        evidence_ids=("fund_identity_evidence",),
        holdings_period=None,
        holdings_coverage="当前没有可用的季度持仓覆盖",
    )
    links = build_fund_relevance(
        (event,), context, (_entity("fund_000001", "fund", "华夏成长混合"),)
    )
    assert len(links) == 1
    assert links[0].relationship is EventEntityRelationship.SUBJECT


@pytest.mark.parametrize(
    ("change", "expected"),
    (
        (Decimal("0.49"), DimensionState.NEUTRAL),
        (Decimal("0.50"), DimensionState.POSITIVE),
        (Decimal("0.51"), DimensionState.POSITIVE),
        (Decimal("-0.49"), DimensionState.NEUTRAL),
        (Decimal("-0.50"), DimensionState.NEGATIVE),
        (Decimal("-0.51"), DimensionState.NEGATIVE),
    ),
)
def test_trend_thresholds_use_exact_decimal(change: Decimal, expected: DimensionState) -> None:
    state = build_market_state(
        (_batch(pct_change=change),), (), (), (), AS_OF, IntelligencePolicyV1()
    )
    assert _dimension(
        state, MarketDimension.TREND_BREADTH, MetricId.INDUSTRY_MEDIAN_PCT_CHANGE
    ).state is expected


@pytest.mark.parametrize(
    ("advancers", "decliners", "expected"),
    (
        (59, 41, DimensionState.NEUTRAL),
        (60, 40, DimensionState.POSITIVE),
        (61, 39, DimensionState.POSITIVE),
        (41, 59, DimensionState.NEUTRAL),
        (40, 60, DimensionState.NEGATIVE),
        (39, 61, DimensionState.NEGATIVE),
    ),
)
def test_breadth_thresholds_use_exact_decimal(
    advancers: int, decliners: int, expected: DimensionState
) -> None:
    state = build_market_state(
        (_batch(advancers=advancers, decliners=decliners),),
        (),
        (),
        (),
        AS_OF,
        IntelligencePolicyV1(),
    )
    assert _dimension(
        state, MarketDimension.TREND_BREADTH, MetricId.INDUSTRY_AGGREGATE_BREADTH
    ).state is expected


def test_market_coverage_and_freshness_fail_closed() -> None:
    policy = IntelligencePolicyV1()
    too_small = build_market_state((_batch(count=19),), (), (), (), AS_OF, policy)
    incomplete = build_market_state((_batch(missing=3),), (), (), (), AS_OF, policy)
    stale_time = AS_OF - timedelta(days=6)
    stale = build_market_state(
        (_batch(retrieved_at=stale_time),), (), (), (), AS_OF, policy
    )

    for state in (too_small, incomplete, stale):
        assert _dimension(
            state,
            MarketDimension.TREND_BREADTH,
            MetricId.INDUSTRY_MEDIAN_PCT_CHANGE,
        ).state is DimensionState.INSUFFICIENT_DATA
        assert _dimension(
            state,
            MarketDimension.TREND_BREADTH,
            MetricId.INDUSTRY_MEDIAN_PCT_CHANGE,
        ).completeness is EvidenceCompleteness.INSUFFICIENT


def test_current_batch_never_fabricates_flow_history() -> None:
    state = build_market_state((_batch(),), (), (), (), AS_OF, IntelligencePolicyV1())
    flow = _dimension(
        state,
        MarketDimension.PERSISTENT_FLOW,
        MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
    )
    assert flow.state is DimensionState.INSUFFICIENT_DATA
    assert flow.value is None


def test_observation_identity_covers_attempt_and_time_and_is_stable() -> None:
    policy = IntelligencePolicyV1()
    first = build_market_state((_batch(attempt=11),), (), (), (), AS_OF, policy)
    identical = build_market_state((_batch(attempt=11),), (), (), (), AS_OF, policy)
    different_attempt = build_market_state((_batch(attempt=12),), (), (), (), AS_OF, policy)

    first_ids = tuple(item.observation_id for item in first.dimensions)
    assert first_ids == tuple(item.observation_id for item in identical.dimensions)
    assert set(first_ids).isdisjoint(
        item.observation_id for item in different_attempt.dimensions
    )


def test_three_current_flow_observations_are_required() -> None:
    batches = tuple(
        _batch(attempt=11 + index, retrieved_at=AS_OF - timedelta(days=index))
        for index in range(3)
    )
    state = build_market_state(batches, (), (), (), AS_OF, IntelligencePolicyV1())
    flow = _dimension(
        state,
        MarketDimension.PERSISTENT_FLOW,
        MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
    )
    assert flow.state is DimensionState.POSITIVE
    assert flow.source_attempt_ids == (11, 12, 13)


def test_insufficient_flow_reports_latest_batch_freshness() -> None:
    stale_time = AS_OF - timedelta(days=6)
    state = build_market_state(
        (_batch(retrieved_at=stale_time),),
        (),
        (),
        (),
        AS_OF,
        IntelligencePolicyV1(),
    )
    flow = _dimension(
        state,
        MarketDimension.PERSISTENT_FLOW,
        MetricId.INDUSTRY_POSITIVE_FLOW_SHARE_3D,
    )
    assert flow.state is DimensionState.INSUFFICIENT_DATA
    assert flow.freshness is EvidenceFreshness.STALE


def test_tier_two_only_event_cannot_set_catalyst_even_if_marked_sufficient() -> None:
    tier_two_item = _item(
        _parsed(
            source_id="stcn_fund_news",
            publisher="证券时报网",
            url="https://www.stcn.com/article/detail/102.html",
        ),
        20,
    )
    forged = NewsEvent(
        event_id="event_forged_tier_two",
        event_type=EventType.POLICY,
        normalized_title="支持长期资金入市",
        supporting_item_ids=(tier_two_item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=tier_two_item.published_at,
        latest_published_at=tier_two_item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )

    state = build_market_state(
        (_batch(),),
        (forged,),
        (tier_two_item,),
        (_binding(tier_two_item),),
        AS_OF,
        IntelligencePolicyV1(),
    )
    catalyst = _dimension(
        state, MarketDimension.CATALYSTS, MetricId.AUTHENTICATED_EVENT_DIRECTION
    )
    assert catalyst.state is DimensionState.INSUFFICIENT_DATA


def test_tier_one_active_event_can_set_policy_catalyst() -> None:
    tier_one_item = _item(_parsed(), 7)
    event = NewsEvent(
        event_id="event_tier_one_policy",
        event_type=EventType.POLICY,
        normalized_title="支持长期资金入市",
        supporting_item_ids=(tier_one_item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=tier_one_item.published_at,
        latest_published_at=tier_one_item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    state = build_market_state(
        (_batch(),),
        (event,),
        (tier_one_item,),
        (_binding(tier_one_item),),
        AS_OF,
        IntelligencePolicyV1(),
    )
    assert _dimension(
        state, MarketDimension.CATALYSTS, MetricId.AUTHENTICATED_EVENT_DIRECTION
    ).state is DimensionState.POSITIVE


def test_checked_bound_tier_one_item_without_direction_is_neutral_and_evidenced() -> None:
    item = _item(_parsed(title="资本市场常规公告", content="资本市场常规公告"), 7)
    event = NewsEvent(
        event_id="event_neutral_policy",
        event_type=EventType.POLICY,
        normalized_title="不能信任的推动措辞",
        supporting_item_ids=(item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=item.published_at,
        latest_published_at=item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    state = build_market_state(
        (_batch(),),
        (event,),
        (item,),
        (_binding(item),),
        AS_OF,
        IntelligencePolicyV1(),
    )
    catalyst = _dimension(
        state, MarketDimension.CATALYSTS, MetricId.AUTHENTICATED_EVENT_DIRECTION
    )
    assert catalyst.state is DimensionState.NEUTRAL
    assert catalyst.evidence_ids == (item.item_id,)
    assert catalyst.source_attempt_ids == (item.source_attempt_id,)


def test_catalyst_direction_comes_from_bound_tier_one_item_not_event_title() -> None:
    item = _item(_parsed(title="风险警示长期资金入市", content="风险警示"), 7)
    event = NewsEvent(
        event_id="event_misleading_title",
        event_type=EventType.POLICY,
        normalized_title="支持长期资金入市",
        supporting_item_ids=(item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=item.published_at,
        latest_published_at=item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    state = build_market_state(
        (_batch(),),
        (event,),
        (item,),
        (_binding(item),),
        AS_OF,
        IntelligencePolicyV1(),
    )
    catalyst = _dimension(
        state, MarketDimension.CATALYSTS, MetricId.AUTHENTICATED_EVENT_DIRECTION
    )
    assert catalyst.state is DimensionState.NEGATIVE


@pytest.mark.parametrize(
    "bindings",
    ((), "ambiguous"),
)
def test_unbound_or_ambiguous_tier_one_item_cannot_set_catalyst(bindings: object) -> None:
    item = _item(_parsed(), 7)
    event = NewsEvent(
        event_id="event_unbound_policy",
        event_type=EventType.POLICY,
        normalized_title=item.title,
        supporting_item_ids=(item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=item.published_at,
        latest_published_at=item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    binding_tuple = (
        ()
        if bindings == ()
        else (_binding(item, ambiguous_aliases=("长期资金",)),)
    )
    state = build_market_state(
        (_batch(),),
        (event,),
        (item,),
        binding_tuple,
        AS_OF,
        IntelligencePolicyV1(),
    )
    assert _dimension(
        state, MarketDimension.CATALYSTS, MetricId.AUTHENTICATED_EVENT_DIRECTION
    ).state is DimensionState.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    ("change", "advancers", "decliners", "phrase"),
    (
        (Decimal("0.50"), 60, 40, "支持长期资金入市"),
        (Decimal("-0.50"), 40, 60, "限制长期资金入市"),
    ),
)
def test_one_other_directional_dimension_is_insufficient_for_market_state(
    change: Decimal, advancers: int, decliners: int, phrase: str
) -> None:
    item = _item(_parsed(title=phrase, content=phrase), 7)
    event = NewsEvent(
        event_id="event_single_other_direction",
        event_type=EventType.POLICY,
        normalized_title=phrase,
        supporting_item_ids=(item.item_id,),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=item.published_at,
        latest_published_at=item.published_at,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("出现官方更正或撤稿",),
    )
    state = build_market_state(
        (_batch(pct_change=change, advancers=advancers, decliners=decliners),),
        (event,),
        (item,),
        (_binding(item),),
        AS_OF,
        IntelligencePolicyV1(),
    )
    assert state.market_state is MarketShadowState.INSUFFICIENT_DATA


def test_unsupported_dimensions_are_explicit_and_shadow_is_non_authorizing() -> None:
    state = build_market_state((_batch(),), (), (), (), AS_OF, IntelligencePolicyV1())
    unsupported = {
        item.dimension
        for item in state.dimensions
        if item.metric_id is None
    }

    assert unsupported == {
        MarketDimension.VALUATION,
        MarketDimension.FUNDAMENTALS_EARNINGS,
    }
    assert state.unknown_dimensions == (
        MarketDimension.PERSISTENT_FLOW,
        MarketDimension.CATALYSTS,
        MarketDimension.CROWDING,
        MarketDimension.VALUATION,
        MarketDimension.FUNDAMENTALS_EARNINGS,
    )
    assert not hasattr(state, "action_authorized")
    assert not hasattr(state, "exact_amount_available")
    assert "amount" not in inspect.signature(build_market_state).parameters
    assert "action" not in inspect.signature(build_market_state).parameters


def test_market_dimensions_use_attempt_lineage_without_fake_news_item_ids() -> None:
    state = build_market_state((_batch(),), (), (), (), AS_OF, IntelligencePolicyV1())
    assert all(item.evidence_ids == () for item in state.dimensions)

    market = MarketEntity(
        entity_id="market_cn",
        entity_type="market",
        canonical_name="中国公募基金市场",
        active_from=datetime(2001, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=(),
    )
    snapshot = IntelligenceSnapshot(
        workflow=IntelligenceWorkflow.MARKET_OVERVIEW,
        request_id="a" * 32,
        request_run_id=1,
        interval=QueryInterval(
            start_at=AS_OF - timedelta(hours=72),
            end_at=AS_OF,
            timezone_name="Asia/Shanghai",
        ),
        subject_fund_code=None,
        entities=(market,),
        item_ids=(),
        source_attempt_ids=(11,),
        lineage_edge_ids=(),
        event_ids=(),
        event_entity_links=(),
        market_state=state,
        fund_relevance_link_ids=(),
        conflicts=(),
        missing_evidence=("valuation", "fundamentals_earnings"),
        created_at=AS_OF,
        exact_amount_available=False,
    )
    snapshot.validate()


def test_market_crowding_is_explicitly_unavailable_under_frozen_policy_v1() -> None:
    rows = tuple(
        _row(
            index,
            pct_change=Decimal("10") if index >= 18 else Decimal("0.50"),
            turnover_rate=Decimal("10") if index >= 18 else Decimal("1"),
        )
        for index in range(1, 21)
    )
    state = build_market_state(
        (MarketBatch(11, rows, AS_OF),), (), (), (), AS_OF, IntelligencePolicyV1()
    )
    crowding = _dimension(
        state,
        MarketDimension.CROWDING,
        MetricId.INDUSTRY_OVERHEATING_SHARE,
    )
    assert crowding.value is None
    assert crowding.state is DimensionState.INSUFFICIENT_DATA
    assert MarketDimension.CROWDING in state.unknown_dimensions
    assert any("crowding" in condition for condition in state.invalidation_conditions)


def test_stable_percentile_rank_limits_twenty_equal_rows_to_three_overheating_sectors() -> None:
    rows = tuple(
        _row(index, pct_change=Decimal("0.50"), turnover_rate=Decimal("1"))
        for index in range(1, 21)
    )
    state = build_market_state(
        (MarketBatch(11, rows, AS_OF),), (), (), (), AS_OF, IntelligencePolicyV1()
    )
    crowding = _dimension(
        state,
        MarketDimension.CROWDING,
        MetricId.INDUSTRY_OVERHEATING_SHARE,
    )
    sector_values = tuple(value for _sector_id, value in state.sector_states)
    assert crowding.state is DimensionState.INSUFFICIENT_DATA
    assert crowding.value is None
    assert sector_values.count(SectorShadowState.OVERHEATING_RISK) == 3
    assert sector_values.count(SectorShadowState.INSUFFICIENT_DATA) == 17


def test_sector_missing_its_crowding_field_is_insufficient_even_with_flow_history() -> None:
    batches = []
    for offset in range(3):
        retrieved_at = AS_OF - timedelta(days=offset)
        rows = tuple(
            _row(
                index,
                turnover_rate=Decimal(index),
                retrieved_at=retrieved_at,
                missing_turnover=offset == 0 and index == 1,
            )
            for index in range(1, 21)
        )
        batches.append(MarketBatch(11 + offset, rows, retrieved_at))
    state = build_market_state(
        tuple(batches), (), (), (), AS_OF, IntelligencePolicyV1()
    )
    sector_values = tuple(value for _sector_id, value in state.sector_states)
    assert sector_values.count(SectorShadowState.INSUFFICIENT_DATA) == 1
