from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import patch

import pytest

from kunjin.brief.facts import (
    AuthenticatedAnnouncementContent,
    build_source_linked_facts,
)
from kunjin.brief.models import OfficialEventCode
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    SOURCE_REGISTRY_V1_GOLDEN_CHECKSUM,
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.models import (
    DocumentKind,
    FeeType,
    FundAnnouncement,
    FundBenchmark,
    FundFeeRule,
    FundManagerTenure,
    FundShareClass,
    SourceDocument,
)
from kunjin.funds.risk.store import ClassificationEvidenceRecord, FundRiskStore
from kunjin.models import FundNavObservation
from kunjin.storage.repository import Repository
from tests.unit.test_fund_disclosure_research import AS_OF, complete_bundle

OFFICIAL_PUBLISHER = "交银施罗德基金管理有限公司"
PRODUCT_NAME = "交银多策略回报灵活配置混合型证券投资基金"


def _rapid_bundle():
    bundle = complete_bundle()
    sources = {
        source_id: replace(
            document,
            url=f"https://fundf10.eastmoney.com/{document.document_kind.value}/{source_id}",
            source_name="eastmoney_f10",
            source_tier=2,
            publisher="东方财富",
        )
        for source_id, document in bundle.source_documents.items()
    }
    announcement_source = sources[7]
    announcement = replace(
        bundle.announcements[0],
        publisher=announcement_source.publisher,
        published_at=announcement_source.published_at,
        url=announcement_source.url,
        source_tier=announcement_source.source_tier,
    )
    return replace(
        bundle,
        announcements=(announcement,),
        source_documents=sources,
    )


def _source_document(
    source_id: int,
    *,
    tier: int,
    publisher: str,
    url: str,
    title: str,
    published_at: datetime = AS_OF - timedelta(days=1),
) -> SourceDocument:
    return SourceDocument(
        source_id,
        "519755",
        DocumentKind.ANNOUNCEMENT,
        title,
        url,
        "official" if tier == 1 else "eastmoney_f10",
        tier,
        publisher,
        published_at,
        AS_OF,
        f"{source_id:x}".rjust(64, "0"),
    )


def _announcement(
    source_id: int,
    *,
    tier: int,
    publisher: str,
    url: str,
    title: str,
    published_at: datetime = AS_OF - timedelta(days=1),
) -> FundAnnouncement:
    return FundAnnouncement(
        "519755",
        title,
        "基金公告",
        publisher,
        published_at,
        url,
        tier,
        source_id,
    )


def _official_announcement_bundle(title: str):
    bundle = _rapid_bundle()
    url = "https://www.fund001.com/fund/519755/notice.pdf"
    document = _source_document(
        7,
        tier=1,
        publisher=OFFICIAL_PUBLISHER,
        url=url,
        title=title,
    )
    announcement = _announcement(
        7,
        tier=1,
        publisher=OFFICIAL_PUBLISHER,
        url=url,
        title=title,
    )
    return replace(
        bundle,
        identity=replace(
            bundle.identity,
            fund_name=PRODUCT_NAME,
            manager_name=OFFICIAL_PUBLISHER,
        ),
        announcements=(announcement,),
        source_documents={**bundle.source_documents, 7: document},
    )


def _content(
    source_id: int = 7,
    *,
    fingerprint: Optional[str] = None,
    checked_at: datetime = AS_OF,
) -> AuthenticatedAnnouncementContent:
    return AuthenticatedAnnouncementContent(
        source_document_id=source_id,
        content_fingerprint=(
            f"{source_id:x}".rjust(64, "0") if fingerprint is None else fingerprint
        ),
        original_source_id=f"disclosure_document_{source_id}",
        quoted_source_id=None,
        integrity_status="active",
        integrity_check_complete=True,
        integrity_checked_at=checked_at,
    )


def _store_nav_batch(
    repository: Repository,
    audit: DecisionAuditStore,
    *,
    request_id: str,
    retrieved_at: datetime,
    nav_date: date,
    unit_nav: str,
    authenticated: bool,
) -> Optional[int]:
    observation = FundNavObservation(
        "519755",
        nav_date,
        unit_nav=Decimal(unit_nav),
        accumulated_nav=Decimal(unit_nav),
        daily_growth=Decimal("0.12"),
        source="eastmoney",
        retrieved_at=retrieved_at,
        corporate_action_state="none",
    )
    if not authenticated:
        repository.save_fund_history("519755", "测试基金", "混合型", "eastmoney", (observation,))
        return None
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=request_id,
        monotonic=lambda: 1.0,
        wall_clock=lambda: retrieved_at - timedelta(seconds=1),
    )
    run_id = audit.begin_request(budget)
    attempt_id = audit.record_source_attempt(
        run_id,
        SourceAttempt(
            "eastmoney_nav",
            "formal_nav",
            "fund:519755",
            1,
            SourceAttemptOutcome.SUCCESS,
            retrieved_at - timedelta(seconds=1),
            retrieved_at,
            datetime.combine(nav_date, datetime.min.time(), tzinfo=timezone.utc),
            None,
            None,
            None,
            None,
            "1",
            SOURCE_REGISTRY_V1_GOLDEN_CHECKSUM,
            100,
        ),
    )
    with repository.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        repository.save_authenticated_fund_history(
            "519755",
            "测试基金",
            "混合型",
            "eastmoney",
            (observation,),
            source_attempt_id=attempt_id,
            connection=connection,
        )
        connection.commit()
    return attempt_id


def test_disclosure_projection_preserves_selection_dates_and_fee_classes() -> None:
    bundle = _rapid_bundle()
    former = FundManagerTenure("519755", "前任经理", date(2020, 1, 1), date(2023, 12, 31), 2)
    c_class = FundShareClass("519755", "019755", "C", "示例基金C", 1)
    c_fee = FundFeeRule(
        "519755",
        FeeType.SALES_SERVICE,
        3,
        share_class="C",
        rate=Decimal("0.40"),
        rule_order=3,
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            share_classes=bundle.share_classes + (c_class,),
            manager_tenures=bundle.manager_tenures + (former,),
            fee_rules=bundle.fee_rules + (c_fee,),
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    assert result.fund_code == "519755"
    current = [fact for fact in result.facts if fact.field_id == "current_manager_team"]
    former_facts = [fact for fact in result.facts if fact.field_id == "former_manager_history"]
    fees = [fact for fact in result.facts if fact.field_id == "fees_share_class_relationship"]
    holdings = [fact for fact in result.facts if fact.field_id == "holdings_industries"]
    assert [fact.value["manager_name"] for fact in current] == ["张三"]
    assert [fact.value["manager_name"] for fact in former_facts] == ["前任经理"]
    assert {fact.value["share_class"] for fact in fees} == {"A", "C"}
    assert holdings[0].value["disclosure_scope"] == ("top10",)
    assert holdings[0].data_as_of == datetime(2026, 6, 30, tzinfo=timezone.utc)
    assert all(fact.canonical_url.startswith("https://") for fact in result.facts)
    assert all(fact.source_lineage_id for fact in result.facts)


def test_stale_top10_holdings_and_missing_redemption_period_fail_closed() -> None:
    bundle = _rapid_bundle()
    old = replace(
        bundle.holdings[0],
        report_period=date(2026, 3, 31),
        published_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    statutory = replace(
        bundle.announcements[0],
        title="2026年半年度报告",
        published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            fee_rules=(bundle.fee_rules[0],),
            holdings=(old,),
            announcements=(statutory,),
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    holding = next(fact for fact in result.facts if fact.field_id == "holdings_industries")
    assert holding.freshness.value == "stale"
    assert holding.completeness.value == "partial"
    assert holding.value["disclosure_scope"] == ("top10",)
    assert "redemption_fee_rules" in result.missing_fields
    assert "redemption_holding_period_rules_are_missing" in result.warnings


def test_tier1_current_manager_wins_and_lower_tier_conflict_is_retained() -> None:
    bundle = _rapid_bundle()
    tier1_source = replace(
        bundle.source_documents[2],
        id=8,
        url="https://www.fund001.com/fund/519755/manager.html",
        source_name="official",
        source_tier=1,
        publisher=OFFICIAL_PUBLISHER,
        checksum="8" * 64,
    )
    higher_tier = FundManagerTenure("519755", "一级来源经理", date(2024, 1, 1), None, 8)

    result = build_source_linked_facts(
        replace(
            bundle,
            identity=replace(bundle.identity, manager_name=OFFICIAL_PUBLISHER),
            manager_tenures=bundle.manager_tenures + (higher_tier,),
            source_documents={**bundle.source_documents, 8: tier1_source},
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    current = [fact for fact in result.facts if fact.field_id == "current_manager_team"]
    assert [fact.value["manager_name"] for fact in current] == ["一级来源经理"]
    assert result.conflicts


def test_query_url_is_rejected_without_silent_canonicalization() -> None:
    bundle = _rapid_bundle()
    queried = replace(
        bundle.source_documents[1],
        url="https://example.com/basic_profile/1?fund=519755",
    )

    result = build_source_linked_facts(
        replace(bundle, source_documents={**bundle.source_documents, 1: queried}),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    assert not any(fact.field_id == "identity_active_status" for fact in result.facts)
    assert "identity_active_status" in result.missing_fields
    assert "source_projection_invalid" in result.warnings


def test_unregistered_tier1_source_cannot_project_official_complete_fact() -> None:
    result = build_source_linked_facts(
        complete_bundle(),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    assert not any(fact.field_id == "identity_active_status" for fact in result.facts)
    assert "identity_active_status" in result.missing_fields


def test_future_disclosure_source_is_not_projected() -> None:
    bundle = _rapid_bundle()
    future = replace(
        bundle.source_documents[1],
        retrieved_at=AS_OF + timedelta(seconds=1),
    )

    result = build_source_linked_facts(
        replace(bundle, source_documents={**bundle.source_documents, 1: future}),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    assert not any(fact.field_id == "identity_active_status" for fact in result.facts)
    assert "identity_active_status" in result.missing_fields


def test_holdings_retain_each_source_specific_publication_time() -> None:
    bundle = _rapid_bundle()
    second_source = replace(
        bundle.source_documents[5],
        id=8,
        url="https://fundf10.eastmoney.com/quarterly_holdings/8",
        published_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        checksum="8" * 64,
    )
    second_holding = replace(
        bundle.holdings[0],
        rank=2,
        security_code="600001",
        security_name="邯郸钢铁",
        published_at=datetime(2026, 7, 6, tzinfo=timezone.utc),
        source_document_id=8,
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            holdings=bundle.holdings + (second_holding,),
            source_documents={**bundle.source_documents, 8: second_source},
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    publications = {
        fact.source_lineage_id: fact.published_at
        for fact in result.facts
        if fact.field_id == "holdings_industries"
    }
    assert publications == {
        "disclosure_document_5": datetime(2026, 7, 8, tzinfo=timezone.utc),
        "disclosure_document_8": datetime(2026, 7, 6, tzinfo=timezone.utc),
    }


def test_tier2_liquidation_reprint_remains_fact_and_never_becomes_event() -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _rapid_bundle()
    url = "https://fundf10.eastmoney.com/notice/519755.pdf"
    document = _source_document(
        8,
        tier=2,
        publisher="东方财富",
        url=url,
        title=title,
    )
    announcement = _announcement(
        8,
        tier=2,
        publisher="东方财富",
        url=url,
        title=title,
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            announcements=(announcement,),
            source_documents={**bundle.source_documents, 8: document},
        ),
        AS_OF,
        announcement_contents=(_content(8),),
        action_ids=("fact_research", "full_exit"),
    )

    assert any(fact.field_id == "fund_manager_product_announcement" for fact in result.facts)
    assert result.official_events == ()


def test_only_cross_authenticated_tier1_body_creates_mature_liquidation_event() -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _official_announcement_bundle(title)

    without_body = build_source_linked_facts(
        bundle,
        AS_OF,
        action_ids=("fact_research", "full_exit"),
    )
    with_body = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "full_exit"),
    )

    assert without_body.official_events == ()
    assert "official_events" in without_body.missing_fields
    assert len(with_body.official_events) == 1
    assert "official_events" not in with_body.missing_fields
    event = with_body.official_events[0]
    assert event.event_code is OfficialEventCode.FUND_LIQUIDATION_NOTICE
    assert event.content_fingerprint == bundle.source_documents[7].checksum
    assert event.integrity_status == "active"
    assert event.quoted_source_id is None
    assert set(event.affected_action_ids) <= {
        "fact_research",
        "full_exit",
    }


def test_content_fingerprint_must_match_same_url_document_checksum() -> None:
    bundle = _official_announcement_bundle(f"{PRODUCT_NAME}清算报告")

    result = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(_content(fingerprint="f" * 64),),
        action_ids=("fact_research", "full_exit"),
    )

    assert result.official_events == ()
    assert "official_events" in result.missing_fields
    assert "announcement_content_binding_invalid" in result.conflicts


@pytest.mark.parametrize(
    "checked_at",
    (AS_OF + timedelta(seconds=1), AS_OF - timedelta(hours=2, seconds=1)),
)
def test_integrity_check_time_must_be_current_and_not_future(checked_at: datetime) -> None:
    bundle = _official_announcement_bundle(f"{PRODUCT_NAME}清算报告")

    result = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(_content(checked_at=checked_at),),
        action_ids=("fact_research", "full_exit"),
    )

    assert result.official_events == ()
    assert "official_events" in result.missing_fields


def test_same_title_tier2_reprint_keeps_distinct_lineage_and_no_second_event() -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _official_announcement_bundle(title)
    tier2_url = "https://fundf10.eastmoney.com/notice/519755.pdf"
    tier2_document = _source_document(
        8,
        tier=2,
        publisher="东方财富",
        url=tier2_url,
        title=title,
    )
    tier2_announcement = _announcement(
        8,
        tier=2,
        publisher="东方财富",
        url=tier2_url,
        title=title,
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            announcements=bundle.announcements + (tier2_announcement,),
            source_documents={**bundle.source_documents, 8: tier2_document},
        ),
        AS_OF,
        announcement_contents=(_content(), _content(8)),
        action_ids=("fact_research", "full_exit"),
    )

    announcements = [
        fact for fact in result.facts if fact.field_id == "fund_manager_product_announcement"
    ]
    assert len(announcements) == 2
    assert len({fact.source_lineage_id for fact in announcements}) == 2
    assert len(result.official_events) == 1
    assert result.official_events[0].quoted_source_id is None


def test_incomplete_integrity_check_keeps_fact_but_blocks_action_event() -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _official_announcement_bundle(title)
    incomplete = replace(_content(), integrity_check_complete=False)

    result = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(incomplete,),
        action_ids=("fact_research", "full_exit"),
    )

    assert any(fact.field_id == "fund_manager_product_announcement" for fact in result.facts)
    assert result.official_events == ()
    assert "official_events" in result.missing_fields
    assert "official_event_integrity_incomplete" in result.warnings


@pytest.mark.parametrize("integrity_status", ("corrected", "retracted"))
def test_nonactive_content_without_replacement_never_triggers_event(
    integrity_status: str,
) -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _official_announcement_bundle(title)

    result = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(replace(_content(), integrity_status=integrity_status),),
        action_ids=("fact_research", "full_exit"),
    )

    assert any(fact.field_id == "fund_manager_product_announcement" for fact in result.facts)
    assert result.official_events == ()
    assert "official_events" in result.missing_fields
    assert "official_event_integrity_nonactive" in result.warnings


@pytest.mark.parametrize(
    "change",
    (
        lambda item: replace(item, source_tier=2),
        lambda item: replace(item, publisher="不一致发布方"),
        lambda item: replace(item, url="https://www.fund001.com/fund/519755/other.pdf"),
        lambda item: replace(item, published_at=AS_OF - timedelta(days=2)),
        lambda item: replace(item, title=f"{PRODUCT_NAME}基金经理变更公告"),
    ),
)
def test_announcement_and_document_metadata_conflict_blocks_event(change) -> None:
    title = "交银多策略回报灵活配置混合型证券投资基金清算报告"
    bundle = _official_announcement_bundle(title)
    broken = change(bundle.announcements[0])

    result = build_source_linked_facts(
        replace(bundle, announcements=(broken,)),
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "full_exit"),
    )

    assert result.official_events == ()
    assert "announcement_source_conflict" in result.conflicts


def test_title_without_exact_product_name_creates_no_event() -> None:
    bundle = _official_announcement_bundle("解读：基金清算报告意味着什么")

    result = build_source_linked_facts(
        bundle,
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "continue_holding"),
    )

    assert result.official_events == ()


def test_company_level_notice_without_exact_product_name_creates_no_event() -> None:
    result = build_source_linked_facts(
        _official_announcement_bundle("关于公司调整业务安排的提示公告"),
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "continue_holding"),
    )

    assert result.official_events == ()


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        (
            f"{PRODUCT_NAME}清算报告",
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        ),
        (
            f"关于{PRODUCT_NAME}可能触发清算的提示性公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}清算报告更正公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}恢复大额申购业务的公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}调整大额申购限额的公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}暂停大额申购业务的公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}基金财产清算报告",
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}增聘基金经理公告",
            OfficialEventCode.MANAGER_CHANGE_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}调整业绩比较基准公告",
            OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
        ),
        (
            f"{PRODUCT_NAME}调整申购费率优惠活动公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
        (
            "其他基金清算报告",
            None,
        ),
        (
            f"{PRODUCT_NAME}召开基金份额持有人大会公告",
            OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE,
        ),
    ),
)
def test_event_title_fullmatch_matrix(
    title: str,
    expected: Optional[OfficialEventCode],
) -> None:
    result = build_source_linked_facts(
        _official_announcement_bundle(title),
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=(
            "fact_research",
            "switch_reduce",
            "switch_buy",
        ),
    )

    if expected is None:
        assert result.official_events == ()
    else:
        assert result.official_events[0].event_code is expected


def test_nfkc_product_binding_and_c_sibling_isolation() -> None:
    a_name = f"{PRODUCT_NAME}A"
    nfkc_bundle = _official_announcement_bundle(f"{PRODUCT_NAME}Ａ清算报告")
    nfkc_bundle = replace(
        nfkc_bundle,
        identity=replace(nfkc_bundle.identity, fund_name=a_name),
    )
    matched = build_source_linked_facts(
        nfkc_bundle,
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "full_exit"),
    )
    assert matched.official_events[0].event_code is OfficialEventCode.FUND_LIQUIDATION_NOTICE

    c_name = f"{PRODUCT_NAME}C"
    c_bundle = _official_announcement_bundle(f"{c_name}清算报告")
    c_bundle = replace(
        c_bundle,
        identity=replace(c_bundle.identity, fund_name=a_name),
        share_classes=(
            replace(
                c_bundle.share_classes[0],
                fund_name=c_name,
                share_class="C",
                related_fund_code="019755",
            ),
        ),
    )
    isolated = build_source_linked_facts(
        c_bundle,
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "full_exit"),
    )
    assert isolated.official_events == ()


def test_action_bearing_event_is_not_crowded_out_by_twenty_routine_notices() -> None:
    bundle = _official_announcement_bundle(f"{PRODUCT_NAME}清算报告")
    documents = dict(bundle.source_documents)
    announcements = list(bundle.announcements)
    contents = [_content()]
    for index in range(8, 29):
        title = f"{PRODUCT_NAME}基金经理变更公告"
        url = f"https://www.fund001.com/fund/519755/notice-{index}.pdf"
        documents[index] = _source_document(
            index,
            tier=1,
            publisher=OFFICIAL_PUBLISHER,
            url=url,
            title=title,
            published_at=AS_OF,
        )
        announcements.append(
            _announcement(
                index,
                tier=1,
                publisher=OFFICIAL_PUBLISHER,
                url=url,
                title=title,
                published_at=AS_OF,
            )
        )
        contents.append(_content(index))

    result = build_source_linked_facts(
        replace(
            bundle,
            announcements=tuple(announcements),
            source_documents=documents,
        ),
        AS_OF,
        announcement_contents=tuple(contents),
        action_ids=("fact_research", "full_exit"),
    )

    assert len(result.official_events) == 20
    assert any(
        event.event_code is OfficialEventCode.FUND_LIQUIDATION_NOTICE
        for event in result.official_events
    )
    assert "official_event_limit_reached" in result.warnings


def test_announcement_fact_ids_are_stable_and_core_facts_survive_truncation() -> None:
    bundle = _rapid_bundle()
    documents = dict(bundle.source_documents)
    announcements = []
    for source_id in range(8, 138):
        title = f"示例基金第{source_id}次运作公告"
        url = f"https://fundf10.eastmoney.com/notice/{source_id}"
        documents[source_id] = _source_document(
            source_id,
            tier=2,
            publisher="东方财富",
            url=url,
            title=title,
            published_at=AS_OF,
        )
        announcements.append(
            _announcement(
                source_id,
                tier=2,
                publisher="东方财富",
                url=url,
                title=title,
                published_at=AS_OF,
            )
        )

    first = build_source_linked_facts(
        replace(
            bundle,
            announcements=tuple(announcements),
            source_documents=documents,
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )
    second = build_source_linked_facts(
        replace(
            bundle,
            announcements=tuple(reversed(announcements)),
            source_documents=documents,
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    def announcement_bindings(result):
        return {
            fact.fact_id: fact.source_lineage_id
            for fact in result.facts
            if fact.field_id == "fund_manager_product_announcement"
        }

    assert announcement_bindings(first) == announcement_bindings(second)
    assert any(fact.field_id == "identity_active_status" for fact in first.facts)
    assert any(fact.field_id == "current_benchmark" for fact in first.facts)
    first_announcement = next(
        index
        for index, fact in enumerate(first.facts)
        if fact.field_id == "fund_manager_product_announcement"
    )
    core_fields = {
        "identity_active_status",
        "current_manager_team",
        "share_class_identity",
        "fees_share_class_relationship",
        "holdings_industries",
        "current_benchmark",
        "formal_nav",
        "d1_classification",
    }
    assert all(
        index < first_announcement
        for index, fact in enumerate(first.facts)
        if fact.field_id in core_fields
    )
    assert "fact_limit_reached" in first.warnings


def test_tier2_source_requires_registered_eastmoney_identity() -> None:
    bundle = _rapid_bundle()
    unknown = replace(
        bundle.source_documents[1],
        url="https://example.com/basic_profile/1",
    )
    wrong_kind = replace(bundle.source_documents[1], source_name="mirror")

    for source in (unknown, wrong_kind):
        result = build_source_linked_facts(
            replace(bundle, source_documents={**bundle.source_documents, 1: source}),
            AS_OF,
            action_ids=("fact_research", "continue_holding"),
        )
        assert not any(fact.field_id == "identity_active_status" for fact in result.facts)


def test_tier1_disclosure_requires_same_registered_identity_manager() -> None:
    bundle = _official_announcement_bundle(f"{PRODUCT_NAME}清算报告")
    mismatched = replace(
        bundle,
        identity=replace(bundle.identity, manager_name="其他基金管理有限公司"),
    )

    result = build_source_linked_facts(
        mismatched,
        AS_OF,
        announcement_contents=(_content(),),
        action_ids=("fact_research", "full_exit"),
    )

    assert not any(fact.source_tier.value == "tier_1" for fact in result.facts)
    assert result.official_events == ()


def test_real_eastmoney_announcement_index_projects_multiple_record_facts() -> None:
    bundle = _rapid_bundle()
    index = replace(
        bundle.source_documents[7],
        title="基金公告索引",
        url=(
            "https://api.fund.eastmoney.com/f10/JJGG?fundcode=519755&pageIndex=1&pageSize=20&type=0"
        ),
        source_name="eastmoney_api",
        publisher="东方财富公告索引",
        published_at=None,
    )
    first = _announcement(
        7,
        tier=2,
        publisher="东方财富公告索引",
        url="https://fund.eastmoney.com/gonggao/519755,first.html",
        title="示例基金第一次运作公告",
    )
    second = replace(
        first,
        title="示例基金第二次运作公告",
        url="https://pdf.dfcfw.com/pdf/H2_second_1.pdf",
        published_at=AS_OF,
    )

    result = build_source_linked_facts(
        replace(
            bundle,
            announcements=(first, second),
            source_documents={**bundle.source_documents, 7: index},
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    announcements = [
        fact for fact in result.facts if fact.field_id == "fund_manager_product_announcement"
    ]
    assert len(announcements) == 2
    assert {fact.canonical_url for fact in announcements} == {
        "https://fundf10.eastmoney.com/jjgg_519755.html"
    }
    assert {fact.value["record_url"] for fact in announcements} == {
        first.url,
        second.url,
    }
    assert {fact.value["record_published_at"] for fact in announcements} == {
        first.published_at.isoformat(),
        second.published_at.isoformat(),
    }


def test_current_benchmark_is_sourced_and_conflict_is_field_scoped() -> None:
    bundle = _rapid_bundle()
    tier1_source = replace(
        bundle.source_documents[1],
        id=8,
        document_kind=DocumentKind.BENCHMARK,
        title="业绩比较基准",
        url="https://www.fund001.com/fund/519755/benchmark.html",
        source_name="official",
        source_tier=1,
        publisher=OFFICIAL_PUBLISHER,
        published_at=AS_OF - timedelta(days=2),
        checksum="8" * 64,
    )
    tier2_source = replace(
        bundle.source_documents[1],
        id=9,
        document_kind=DocumentKind.BENCHMARK,
        title="业绩比较基准",
        url="https://fundf10.eastmoney.com/benchmark/9",
        checksum="9" * 64,
    )
    primary = FundBenchmark(
        "519755",
        "中证800指数收益率*60%+中债综合指数收益率*40%",
        date(2026, 1, 1),
        None,
        8,
    )
    lower_tier = FundBenchmark("519755", "其他基准", None, None, 9)

    result = build_source_linked_facts(
        replace(
            bundle,
            identity=replace(bundle.identity, manager_name=OFFICIAL_PUBLISHER),
            benchmarks=(primary, lower_tier),
            source_documents={
                **bundle.source_documents,
                8: tier1_source,
                9: tier2_source,
            },
        ),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    benchmark = next(fact for fact in result.facts if fact.field_id == "current_benchmark")
    assert benchmark.value == {
        "description": primary.description,
        "effective_from": "2026-01-01",
        "effective_to": None,
    }
    assert benchmark.data_as_of == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert benchmark.published_at == tier1_source.published_at
    assert benchmark.conflict_ids
    assert all(
        not fact.conflict_ids
        for fact in result.facts
        if fact.field_id
        in {
            "identity_active_status",
            "fees_share_class_relationship",
            "holdings_industries",
        }
    )


def test_fee_projection_retains_safe_threshold_and_rule_order_keys() -> None:
    bundle = _rapid_bundle()
    threshold_rule = replace(
        bundle.fee_rules[0],
        amount_min=Decimal("1000"),
        amount_max=Decimal("9999.99"),
        rule_order=7,
    )

    result = build_source_linked_facts(
        replace(bundle, fee_rules=(threshold_rule,)),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    fee = next(fact for fact in result.facts if fact.field_id == "fees_share_class_relationship")
    assert fee.value["threshold_minimum"] == "1000"
    assert fee.value["threshold_maximum"] == "9999.99"
    assert fee.value["rule_order"] == "7"
    assert "amount_min" not in fee.value
    assert "amount_max" not in fee.value


def test_formal_nav_is_selected_from_reauthenticated_task4_batch(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    audit = DecisionAuditStore(repository)
    attempt_id = _store_nav_batch(
        repository,
        audit,
        request_id="3" * 32,
        retrieved_at=AS_OF - timedelta(minutes=1),
        nav_date=AS_OF.date(),
        unit_nav="1.2345",
        authenticated=True,
    )

    result = build_source_linked_facts(
        _rapid_bundle(),
        AS_OF,
        repository=repository,
        decision_audit_store=audit,
        action_ids=("fact_research", "continue_holding"),
    )

    nav_fact = next(fact for fact in result.facts if fact.field_id == "formal_nav")
    assert nav_fact.value == "1.2345"
    assert nav_fact.source_lineage_id == f"source_attempt_{attempt_id}"


def test_newer_unbound_nav_cannot_displace_older_authenticated_batch(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    audit = DecisionAuditStore(repository)
    _store_nav_batch(
        repository,
        audit,
        request_id="4" * 32,
        retrieved_at=AS_OF - timedelta(minutes=2),
        nav_date=AS_OF.date() - timedelta(days=1),
        unit_nav="1.1111",
        authenticated=True,
    )
    _store_nav_batch(
        repository,
        audit,
        request_id="5" * 32,
        retrieved_at=AS_OF - timedelta(minutes=1),
        nav_date=AS_OF.date(),
        unit_nav="9.9999",
        authenticated=False,
    )

    result = build_source_linked_facts(
        _rapid_bundle(),
        AS_OF,
        repository=repository,
        decision_audit_store=audit,
        action_ids=("fact_research", "continue_holding"),
    )

    nav_fact = next(fact for fact in result.facts if fact.field_id == "formal_nav")
    assert nav_fact.value == "1.1111"
    assert nav_fact.freshness.value == "dated_history"


def test_nav_stores_must_reference_the_same_database(tmp_path) -> None:
    repository = Repository(tmp_path / "facts.db")
    other_repository = Repository(tmp_path / "audit.db")
    repository.migrate()
    other_repository.migrate()

    with pytest.raises(ValueError, match="same database"):
        build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            repository=repository,
            decision_audit_store=DecisionAuditStore(other_repository),
            action_ids=("fact_research", "continue_holding"),
        )


def test_d1_projection_reads_only_current_authenticated_store_record(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    store = FundRiskStore(repository)
    record = ClassificationEvidenceRecord(None, None, (), (), None)
    report = {
        "fund_code": "519755",
        "classification": {
            "product_family": "equity_mixed",
            "risk_bucket": "hybrid_risk",
            "portfolio_role": "satellite_only",
            "classified_at": (AS_OF - timedelta(days=1)).isoformat(),
            "valid_until": (AS_OF + timedelta(days=1)).isoformat(),
        },
        "evidence_status": "verified",
        "evidence_tags": ["equity_exposure_present"],
        "missing_evidence": [],
        "conflicts": [],
        "sources": [
            {
                "source_namespace": "d1_artifact",
                "document_id": 21,
                "url": "https://www.fund001.com/fund/519755/prospectus.pdf",
                "publisher": OFFICIAL_PUBLISHER,
                "published_at": (AS_OF - timedelta(days=2)).isoformat(),
                "retrieved_at": (AS_OF - timedelta(days=1)).isoformat(),
                "source_tier": 1,
            }
        ],
    }

    with (
        patch.object(FundRiskStore, "classification_evidence", return_value=record) as current,
        patch("kunjin.brief.facts.build_authenticated_risk_research_report", return_value=report),
    ):
        result = build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            risk_store=store,
            action_ids=("fact_research", "continue_holding"),
        )

    current.assert_called_once_with("519755")
    fact = next(fact for fact in result.facts if fact.field_id == "d1_classification")
    assert fact.value["evidence_status"] == "verified"
    assert fact.value["classified_at"] == report["classification"]["classified_at"]
    assert fact.data_as_of == datetime.fromisoformat(report["sources"][0]["retrieved_at"])
    assert fact.calculated is True

    queried_report = {
        **report,
        "sources": [
            report["sources"][0],
            {
                **report["sources"][0],
                "document_id": 22,
                "url": "https://www.fund001.com/fund/519755/prospectus.pdf?download=1",
            },
        ],
    }
    with (
        patch.object(FundRiskStore, "classification_evidence", return_value=record),
        patch(
            "kunjin.brief.facts.build_authenticated_risk_research_report",
            return_value=queried_report,
        ),
    ):
        rejected = build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            risk_store=store,
            action_ids=("fact_research", "continue_holding"),
        )
    assert not any(fact.field_id == "d1_classification" for fact in rejected.facts)
    assert "d1_classification" in rejected.missing_fields


@pytest.mark.parametrize("future_field", ("classified_at", "published_at", "retrieved_at"))
def test_future_d1_times_are_not_projected(tmp_path, future_field: str) -> None:
    repository = Repository(tmp_path / f"{future_field}.db")
    repository.migrate()
    store = FundRiskStore(repository)
    record = ClassificationEvidenceRecord(None, None, (), (), None)
    classification = {
        "product_family": "equity_mixed",
        "risk_bucket": "hybrid_risk",
        "portfolio_role": "satellite_only",
        "classified_at": (AS_OF - timedelta(days=1)).isoformat(),
        "valid_until": (AS_OF + timedelta(days=1)).isoformat(),
    }
    source = {
        "source_namespace": "d1_artifact",
        "document_id": 21,
        "url": "https://www.fund001.com/fund/519755/prospectus.pdf",
        "publisher": OFFICIAL_PUBLISHER,
        "published_at": (AS_OF - timedelta(days=2)).isoformat(),
        "retrieved_at": (AS_OF - timedelta(days=1)).isoformat(),
        "source_tier": 1,
    }
    if future_field == "classified_at":
        classification[future_field] = (AS_OF + timedelta(seconds=1)).isoformat()
    else:
        source[future_field] = (AS_OF + timedelta(seconds=1)).isoformat()
    report = {
        "fund_code": "519755",
        "classification": classification,
        "evidence_status": "verified",
        "evidence_tags": ["equity_exposure_present"],
        "missing_evidence": [],
        "conflicts": [],
        "sources": [source],
    }

    with (
        patch.object(FundRiskStore, "classification_evidence", return_value=record),
        patch(
            "kunjin.brief.facts.build_authenticated_risk_research_report",
            return_value=report,
        ),
    ):
        result = build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            risk_store=store,
            action_ids=("fact_research", "continue_holding"),
        )

    assert not any(fact.field_id == "d1_classification" for fact in result.facts)
    assert "d1_classification" in result.missing_fields
    assert "d1_classification_binding_invalid" in result.conflicts


def test_fact_values_never_contain_private_or_noncanonical_scalars() -> None:
    result = build_source_linked_facts(
        _rapid_bundle(),
        AS_OF,
        action_ids=("fact_research", "continue_holding"),
    )

    for fact in result.facts:
        fact.validate()
        rendered = repr(fact.to_canonical_dict())
        assert "Decimal(" not in rendered
        assert "portfolio_weight" not in rendered


def test_empty_current_d1_store_is_missing_without_false_conflict(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()

    result = build_source_linked_facts(
        _rapid_bundle(),
        AS_OF,
        risk_store=FundRiskStore(repository),
        action_ids=("fact_research", "continue_holding"),
    )

    assert "d1_classification" in result.missing_fields
    assert "d1_classification_binding_invalid" not in result.conflicts


def test_unbound_content_or_unknown_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            announcement_contents=(_content(999),),
            action_ids=("fact_research", "continue_holding"),
        )
    with pytest.raises(ValueError, match="unsupported"):
        build_source_linked_facts(
            _rapid_bundle(),
            AS_OF,
            action_ids=("fact_research", "invented_action"),
        )
