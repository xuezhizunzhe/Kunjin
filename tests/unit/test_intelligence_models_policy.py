from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestTerminalStatus,
    SourceTier,
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
    IntelligenceReport,
    IntelligenceSnapshot,
    IntelligenceWorkflow,
    LineageEdge,
    LineageKind,
    MarketDimension,
    MarketEntity,
    MarketShadowState,
    MarketStateSnapshot,
    MetricId,
    NewsEvent,
    NewsItem,
    QueryInterval,
    QueryWindow,
    SectorShadowState,
    truncate_excerpt_utf8,
)
from kunjin.intelligence.policy import (
    INTELLIGENCE_POLICY_V1_GOLDEN_CHECKSUM,
    IntelligencePolicyV1,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 18, 4, 30, tzinfo=UTC)
DAY_START_UTC = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
DAY_END_UTC = datetime(2026, 7, 18, 16, 0, tzinfo=UTC)
CHECKSUM = "a" * 64


def _news_item(**changes: object) -> NewsItem:
    values = {
        "item_id": "news_item_1",
        "source_id": "gov_cn_policy",
        "publisher": "中国政府网",
        "canonical_url": "https://www.gov.cn/zhengce/content/202607/content_1.htm",
        "title": "关于促进资本市场长期稳定发展的通知",
        "excerpt": "支持长期资金入市。",
        "excerpt_truncated": False,
        "excerpt_original_bytes": len("支持长期资金入市。".encode()),
        "excerpt_expires_at": NOW + timedelta(days=365),
        "excerpt_expired_at": None,
        "published_at": DAY_START_UTC,
        "publication_precision": "date",
        "publication_interval_end": DAY_END_UTC,
        "retrieved_at": NOW,
        "source_tier": SourceTier.TIER_1,
        "content_fingerprint": CHECKSUM,
        "category": "policy",
        "integrity_state": IntegrityState.ACTIVE,
        "source_attempt_id": 7,
    }
    values.update(changes)
    return NewsItem(**values)


def _dimension(**changes: object) -> DimensionObservation:
    values = {
        "observation_id": "observation_1",
        "entity_id": "market_cn",
        "dimension": MarketDimension.TREND_BREADTH,
        "metric_id": MetricId.INDUSTRY_MEDIAN_PCT_CHANGE,
        "state": DimensionState.POSITIVE,
        "value": Decimal("0.50"),
        "unit": "percentage_points",
        "data_as_of": NOW,
        "retrieved_at": NOW,
        "source_tier": SourceTier.TIER_2,
        "source_attempt_ids": (11,),
        "evidence_ids": ("news_item_1",),
        "freshness": EvidenceFreshness.CURRENT,
        "completeness": EvidenceCompleteness.COMPLETE,
        "conflict_ids": (),
    }
    values.update(changes)
    return DimensionObservation(**values)


def _market_state(**changes: object) -> MarketStateSnapshot:
    values = {
        "market_state": MarketShadowState.NEUTRAL,
        "sector_states": (("technology", SectorShadowState.IMPROVING),),
        "dimensions": (_dimension(),),
        "supporting_observation_ids": ("observation_1",),
        "opposing_observation_ids": (),
        "unknown_dimensions": (
            MarketDimension.VALUATION,
            MarketDimension.FUNDAMENTALS_EARNINGS,
        ),
        "invalidation_conditions": ("市场数据超过五个自然日未更新",),
        "next_review_at": NOW + timedelta(hours=2),
        "policy_checksum": INTELLIGENCE_POLICY_V1_GOLDEN_CHECKSUM,
    }
    values.update(changes)
    return MarketStateSnapshot(**values)


def _snapshot(**changes: object) -> IntelligenceSnapshot:
    entity = MarketEntity(
        entity_id="market_cn",
        entity_type="market",
        canonical_name="中国公募基金市场",
        active_from=datetime(2001, 1, 1, tzinfo=UTC),
        active_until=None,
        evidence_ids=("news_item_1",),
    )
    link = EventEntityLink(
        link_id="event_entity_link_1",
        event_id="event_1",
        entity_id="market_cn",
        relationship=EventEntityRelationship.POLICY_CATALYST,
        evidence_ids=("news_item_1",),
    )
    values = {
        "workflow": IntelligenceWorkflow.MARKET_OVERVIEW,
        "request_id": "a" * 32,
        "request_run_id": 9,
        "interval": QueryInterval(
            start_at=NOW - timedelta(hours=72),
            end_at=NOW,
            timezone_name="Asia/Shanghai",
        ),
        "subject_fund_code": None,
        "entities": (entity,),
        "item_ids": ("news_item_1",),
        "source_attempt_ids": (7, 11),
        "lineage_edge_ids": (),
        "event_ids": ("event_1",),
        "event_entity_links": (link,),
        "market_state": _market_state(),
        "fund_relevance_link_ids": (),
        "conflicts": (),
        "missing_evidence": ("valuation", "fundamentals_earnings"),
        "created_at": NOW,
        "exact_amount_available": False,
    }
    values.update(changes)
    return IntelligenceSnapshot(**values)


def test_policy_v1_contract_is_exact_and_amount_free() -> None:
    assert tuple(item.value for item in IntelligenceWorkflow) == (
        "news_recent",
        "market_overview",
        "fund_intelligence",
    )
    assert tuple(item.value for item in QueryWindow) == ("today", "recent", "near_term")
    assert tuple(item.value for item in DimensionState) == (
        "positive",
        "neutral",
        "negative",
        "risk_flag",
        "conflicted",
        "insufficient_data",
    )
    assert tuple(item.value for item in MarketShadowState) == (
        "offensive_bias",
        "neutral",
        "defensive_bias",
        "insufficient_data",
    )
    assert tuple(item.value for item in SectorShadowState) == (
        "improving",
        "neutral",
        "weakening",
        "overheating_risk",
        "insufficient_data",
    )
    assert tuple(item.value for item in MarketDimension) == (
        "trend_breadth",
        "persistent_flow",
        "catalysts",
        "crowding",
        "valuation",
        "fundamentals_earnings",
    )
    assert tuple(item.value for item in EventConfidenceState) == (
        "sufficient",
        "partial",
        "conflicted",
        "insufficient",
    )
    assert tuple(item.value for item in MetricId) == (
        "industry_median_pct_change",
        "industry_aggregate_breadth",
        "sector_pct_change",
        "sector_breadth",
        "industry_positive_flow_share_3d",
        "sector_main_flow_ratio_3d",
        "authenticated_event_direction",
        "industry_overheating_share",
        "sector_return_turnover_percentiles",
    )
    assert tuple(item.value for item in EventType) == (
        "policy",
        "fund_official",
        "fund_media",
        "market",
        "sector",
    )
    assert tuple(item.value for item in EventEntityRelationship) == (
        "subject",
        "affects",
        "policy_catalyst",
        "fund_holding_exposure",
        "fund_benchmark_exposure",
    )
    policy = IntelligencePolicyV1()
    assert policy.rapid_seconds == 90
    assert policy.deep_seconds == 480
    assert policy.recent_seconds == 72 * 60 * 60
    assert policy.excerpt_max_bytes == 2048
    assert policy.excerpt_retention_days == 365
    assert dict((row[0], row[1]) for row in policy.metric_rules)["trend_breadth_market"] == (
        "industry_median_pct_change",
        "industry_aggregate_breadth",
    )
    assert tuple(row[0] for row in policy.source_registry) == (
        "cs_com_cn",
        "csrc_public_news",
        "eastmoney_market",
        "fund_manager_official_documents",
        "gov_cn_policy",
        "stcn_fund_news",
    )
    assert policy.checksum() == INTELLIGENCE_POLICY_V1_GOLDEN_CHECKSUM
    canonical = policy.canonical_json()
    assert canonical == json.dumps(
        json.loads(canonical), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    assert b"amount" not in canonical
    assert b"exact_amount_available" not in canonical


def test_policy_v1_is_frozen_and_rejects_subclasses_or_drift() -> None:
    policy = IntelligencePolicyV1()
    with pytest.raises(FrozenInstanceError):
        policy.rapid_seconds = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="canonical"):
        replace(policy, recent_seconds=1).validate()

    class DerivedPolicy(IntelligencePolicyV1):
        pass

    with pytest.raises(ValueError, match="exact"):
        DerivedPolicy().validate()


def test_utf8_excerpt_truncation_preserves_code_points_and_exact_flag() -> None:
    exact = "基" * 682 + "ab"
    assert len(exact.encode("utf-8")) == 2048
    unchanged, unchanged_flag = truncate_excerpt_utf8(exact)
    assert unchanged == exact
    assert unchanged_flag is False

    truncated, truncated_flag = truncate_excerpt_utf8(exact + "金")
    assert len(truncated.encode("utf-8")) <= 2048
    assert truncated.encode("utf-8").decode("utf-8") == truncated
    assert truncated == exact
    assert truncated_flag is True


def test_news_item_requires_bounded_excerpt_and_exact_expiry_shape() -> None:
    item = _news_item()
    item.validate()
    assert item.to_canonical_dict()["published_at"] == "2026-07-17T16:00:00+00:00"
    with pytest.raises(FrozenInstanceError):
        item.title = "changed"  # type: ignore[misc]

    with pytest.raises(ValueError, match="2048"):
        _news_item(excerpt="基" * 683).validate()
    with pytest.raises(ValueError, match="non-empty"):
        _news_item(excerpt="").validate()
    with pytest.raises(ValueError, match="expired"):
        _news_item(excerpt=None, excerpt_expired_at=None).validate()
    with pytest.raises(ValueError, match="expired"):
        _news_item(
            excerpt=None,
            excerpt_expired_at=NOW + timedelta(days=364),
        ).validate()
    expired = _news_item(
        excerpt=None,
        excerpt_expired_at=NOW + timedelta(days=365),
    )
    expired.validate()

    truncated_text = "基" * 682
    truncated = _news_item(
        excerpt=truncated_text,
        excerpt_truncated=True,
        excerpt_original_bytes=len(truncated_text.encode("utf-8")) + 3,
    )
    truncated.validate()
    replace(truncated, excerpt=None, excerpt_expired_at=truncated.excerpt_expires_at).validate()
    with pytest.raises(ValueError, match="truncated"):
        replace(truncated, excerpt_truncated=False).validate()
    with pytest.raises(ValueError, match="truncated"):
        _news_item(excerpt_truncated=True).validate()
    for short_excerpt, original_bytes in (("基", 4), ("😀", 5)):
        with pytest.raises(ValueError, match="truncated"):
            _news_item(
                excerpt=short_excerpt,
                excerpt_truncated=True,
                excerpt_original_bytes=original_bytes,
            ).validate()


def test_news_item_publication_precision_is_exact_and_all_datetimes_are_utc() -> None:
    _news_item().validate()
    minute = _news_item(
        published_at=datetime(2026, 7, 18, 3, 51, tzinfo=UTC),
        publication_precision="minute",
        publication_interval_end=None,
    )
    minute.validate()

    shanghai = timezone(timedelta(hours=8))
    with pytest.raises(ValueError, match="UTC"):
        _news_item(retrieved_at=NOW.astimezone(shanghai)).validate()
    with pytest.raises(ValueError, match="date"):
        _news_item(publication_interval_end=None).validate()
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        _news_item(published_at=datetime(2026, 7, 18, 0, 0, tzinfo=UTC)).validate()
    with pytest.raises(ValueError, match="minute"):
        replace(minute, published_at=minute.published_at.replace(second=3)).validate()
    with pytest.raises(ValueError, match="minute"):
        replace(
            minute,
            publication_interval_end=minute.published_at + timedelta(minutes=1),
        ).validate()
    with pytest.raises(ValueError, match="precision"):
        replace(minute, publication_precision="second").validate()


def test_exact_evidence_records_validate_relationships_and_utc() -> None:
    alias = EntityAlias(
        entity_id="sector_technology",
        alias="科技",
        alias_type="controlled_name",
        active_from=NOW,
        active_until=None,
        evidence_ids=("news_item_1",),
    )
    alias.validate()
    LineageEdge(
        edge_id="lineage_1",
        from_item_id="news_item_1",
        to_item_id="news_item_2",
        kind=LineageKind.REPRINT,
        evidence_ids=("news_item_1", "news_item_2"),
    ).validate()
    event = NewsEvent(
        event_id="event_1",
        event_type=EventType.POLICY,
        normalized_title="促进长期资金入市",
        supporting_item_ids=("news_item_1",),
        opposing_item_ids=(),
        correction_item_ids=(),
        retraction_item_ids=(),
        confidence_state=EventConfidenceState.SUFFICIENT,
        earliest_published_at=DAY_START_UTC,
        latest_published_at=DAY_END_UTC,
        integrity_state=IntegrityState.ACTIVE,
        superseded_by_event_id=None,
        invalidation_conditions=("官方发布更正或撤回",),
    )
    event.validate()
    with pytest.raises(ValueError, match="sufficient"):
        replace(event, opposing_item_ids=("news_item_2",)).validate()
    with pytest.raises(ValueError, match="conflicted"):
        replace(event, confidence_state=EventConfidenceState.CONFLICTED).validate()
    retracted = replace(
        event,
        supporting_item_ids=(),
        retraction_item_ids=("news_item_2",),
        confidence_state=EventConfidenceState.INSUFFICIENT,
        integrity_state=IntegrityState.RETRACTED,
    )
    retracted.validate()
    superseded = replace(
        event,
        confidence_state=EventConfidenceState.INSUFFICIENT,
        integrity_state=IntegrityState.SUPERSEDED,
        superseded_by_event_id="event_2",
    )
    superseded.validate()
    with pytest.raises(ValueError, match="superseded"):
        replace(superseded, confidence_state=EventConfidenceState.SUFFICIENT).validate()
    with pytest.raises(ValueError, match="replacement"):
        replace(superseded, superseded_by_event_id="event_1").validate()
    with pytest.raises(ValueError, match="non-superseded"):
        replace(event, superseded_by_event_id="event_2").validate()
    with pytest.raises(ValueError, match="UTC"):
        replace(alias, active_from=NOW.astimezone(timezone(timedelta(hours=8)))).validate()


def test_dimension_observations_accept_only_bounded_public_ratios() -> None:
    observation = _dimension()
    observation.validate()
    assert observation.to_canonical_dict()["value"] == "0.5"
    _dimension(
        dimension=MarketDimension.VALUATION,
        metric_id=None,
        value=None,
        unit=None,
        state=DimensionState.INSUFFICIENT_DATA,
        freshness=EvidenceFreshness.UNKNOWN,
        completeness=EvidenceCompleteness.INSUFFICIENT,
    ).validate()
    with pytest.raises(ValueError, match="public market ratio"):
        _dimension(value=Decimal("73129.17")).validate()
    with pytest.raises(ValueError, match="unit"):
        _dimension(unit="cny").validate()
    with pytest.raises(ValueError, match="exact MarketDimension"):
        _dimension(dimension="trend_breadth").validate()
    with pytest.raises(ValueError, match="metric"):
        _dimension(metric_id=MetricId.SECTOR_BREADTH).validate()
    with pytest.raises(ValueError, match="unsupported"):
        _dimension(metric_id=None).validate()
    with pytest.raises(ValueError, match="source attempt"):
        _dimension(source_attempt_ids=()).validate()
    with pytest.raises(ValueError, match="ascending"):
        _dimension(source_attempt_ids=(11, 11)).validate()
    with pytest.raises(ValueError, match="state"):
        _dimension(value=Decimal("0.49"), state=DimensionState.POSITIVE).validate()
    with pytest.raises(ValueError, match="conflict"):
        _dimension(state=DimensionState.CONFLICTED, conflict_ids=()).validate()


def test_market_observation_namespace_and_source_attempts_fail_closed() -> None:
    state = _market_state()
    state.validate()
    with pytest.raises(ValueError, match="observation"):
        replace(state, supporting_observation_ids=("news_item_1",)).validate()
    with pytest.raises(ValueError, match="source attempt"):
        _snapshot(source_attempt_ids=(7,)).validate()


def test_event_links_are_exact_resolved_and_fund_relevance_is_scoped() -> None:
    snapshot = _snapshot()
    snapshot.validate()
    link = snapshot.event_entity_links[0]
    with pytest.raises(ValueError, match="resolve"):
        replace(snapshot, event_ids=()).validate()
    with pytest.raises(ValueError, match="resolve"):
        replace(snapshot, entities=()).validate()
    with pytest.raises(ValueError, match="subset"):
        replace(snapshot, fund_relevance_link_ids=("missing_link",)).validate()
    with pytest.raises(ValueError, match="fund intelligence"):
        replace(snapshot, fund_relevance_link_ids=(link.link_id,)).validate()

    fund_snapshot = replace(
        snapshot,
        workflow=IntelligenceWorkflow.FUND_INTELLIGENCE,
        subject_fund_code="000001",
        fund_relevance_link_ids=(link.link_id,),
    )
    fund_snapshot.validate()


@pytest.mark.parametrize(
    "url",
    (
        "https://localhost/news",
        "https://internal/news",
        "https://service.local/news",
        "https://127.0.0.1/news",
        "https://[::1]/news",
        "https://user:pass@www.gov.cn/news",
        "https://www.gov.cn:443/news",
        "https://www.gov.cn/news#private",
        "https://www.gov.cn/news?authorization=secret",
        "https://www.gov.cn/news?access_token=secret",
        "https://www.gov.cn/news?cookie=secret",
        "https://www.gov.cn/news?credential=secret",
        "https://www.gov.cn/news?apiKey=secret",
        "https://www.gov.cn/news?authorizationToken=secret",
        "https://%31%32%37.0.0.1/news",
    ),
)
def test_news_item_rejects_nonpublic_or_sensitive_urls(url: str) -> None:
    with pytest.raises(ValueError, match="public HTTPS"):
        _news_item(canonical_url=url).validate()


@pytest.mark.parametrize(
    "private_name",
    (
        "amount",
        "shares",
        "cost",
        "profit",
        "portfolio_weight",
        "browser_cookies",
        "authorization_headers",
        "raw_body",
        "local_path",
    ),
)
def test_dynamic_snapshot_and_report_paths_reject_private_fields(private_name: str) -> None:
    with pytest.raises(ValueError, match="private"):
        _snapshot(conflicts=(private_name,)).validate()

    report = IntelligenceReport(
        snapshot=_snapshot(),
        terminal_status=RequestTerminalStatus.COMPLETE,
        omitted_work=(),
        beginner_explanation_zh={"summary": "当前证据只支持观察。", private_name: "sentinel"},
    )
    with pytest.raises(ValueError, match="private"):
        report.validate()


def test_dynamic_report_tree_rejects_amount_like_values_and_is_immutable() -> None:
    report = IntelligenceReport(
        snapshot=_snapshot(),
        terminal_status=RequestTerminalStatus.COMPLETE,
        omitted_work=(),
        beginner_explanation_zh={
            "summary": "当前证据只支持观察。",
            "evidence": ("政策事件一",),
        },
    )
    report.validate()
    with pytest.raises(TypeError):
        report.beginner_explanation_zh["summary"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="Decimal"):
        IntelligenceReport(
            snapshot=_snapshot(),
            terminal_status=RequestTerminalStatus.COMPLETE,
            omitted_work=(),
            beginner_explanation_zh={"summary": Decimal("73129.17")},
        ).validate()


def test_snapshot_is_amount_free_and_references_resolve() -> None:
    snapshot = _snapshot()
    snapshot.validate()
    canonical = snapshot.canonical_json()
    assert b"73129.17" not in canonical
    assert b"portfolio_weight" not in canonical
    with pytest.raises(ValueError, match="false"):
        replace(snapshot, exact_amount_available=True).validate()
    with pytest.raises(ValueError, match="resolve"):
        replace(snapshot, item_ids=()).validate()
    with pytest.raises(ValueError, match="fund code"):
        replace(snapshot, subject_fund_code="ABC").validate()


def test_report_terminal_status_and_omitted_work_are_consistent() -> None:
    complete = IntelligenceReport(
        snapshot=_snapshot(),
        terminal_status=RequestTerminalStatus.COMPLETE,
        omitted_work=(),
        beginner_explanation_zh={"summary": "当前证据只支持观察。"},
    )
    complete.validate()
    with pytest.raises(ValueError, match="inconsistent"):
        replace(complete, terminal_status=RequestTerminalStatus.PARTIAL).validate()
    partial = replace(
        complete,
        terminal_status=RequestTerminalStatus.PARTIAL,
        omitted_work=("valuation",),
    )
    partial.validate()
