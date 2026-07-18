from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from kunjin.intelligence.models import LineageKind
from kunjin.intelligence.parsers import (
    EASTMONEY_MARKET_FIELDS,
    IntelligenceParseError,
    parse_eastmoney_market,
    parse_gov_policy_list,
    parse_stcn_detail,
    parse_stcn_fund_list,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 18, 4, 30, tzinfo=UTC)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "intelligence"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_gov_policy_list_uses_exact_canonical_paths_and_local_date_interval() -> None:
    items = parse_gov_policy_list(fixture("gov_policy.json"), retrieved_at=NOW)

    assert tuple(item.canonical_url for item in items) == (
        "https://www.gov.cn/zhengce/content/202607/content_710001.htm",
        "https://www.gov.cn/zhengce/zhengceku/202607/content_710002.htm",
    )
    assert items[0].source_id == "gov_cn_policy"
    assert items[0].hosting_publisher == "中国政府网"
    assert items[0].attributed_publisher == "中国政府网"
    assert items[0].published_at.isoformat() == "2026-07-13T16:00:00+00:00"
    assert items[0].publication_interval_end.isoformat() == "2026-07-14T16:00:00+00:00"
    assert items[0].publication_precision == "date"
    assert items[0].lineage_hint is LineageKind.ORIGINAL
    assert items[0].normalized_public_content == (
        "关于促进资本市场长期稳定发展的通知 支持长期资金入市"
    )


def test_gov_policy_rejects_non_list_and_noncanonical_policy_urls() -> None:
    with pytest.raises(IntelligenceParseError, match="list"):
        parse_gov_policy_list('{"items": []}', retrieved_at=NOW)

    payload = json.loads(fixture("gov_policy.json"))
    payload[0]["URL"] = "https://www.gov.cn.evil.example/zhengce/content/1.htm"
    with pytest.raises(IntelligenceParseError, match="government policy URL"):
        parse_gov_policy_list(json.dumps(payload), retrieved_at=NOW)


def test_gov_policy_validates_unknown_values_before_ignoring_them() -> None:
    payload = json.loads(fixture("gov_policy.json"))
    nested: object = "leaf"
    for _ in range(14):
        nested = {"nested": nested}
    payload[0]["unknown"] = nested

    with pytest.raises(IntelligenceParseError, match="depth"):
        parse_gov_policy_list(json.dumps(payload), retrieved_at=NOW)


def test_stcn_fund_list_extracts_only_exact_detail_ids() -> None:
    candidates = parse_stcn_fund_list(fixture("stcn_fund_list.html"), retrieved_at=NOW)

    assert tuple(candidate.detail_id for candidate in candidates) == ("3359541", "3359602")
    assert candidates[0].canonical_url == "https://www.stcn.com/article/detail/3359541.html"
    assert candidates[0].listed_title == "公募基金积极布局长期资金"
    with pytest.raises(FrozenInstanceError):
        candidates[0].detail_id = "1"  # type: ignore[misc]


def test_stcn_hosted_reprint_is_not_independent() -> None:
    item = parse_stcn_detail(fixture("stcn_fund_detail.html"), retrieved_at=NOW)

    assert item.hosting_publisher == "证券时报网"
    assert item.attributed_publisher == "中国基金报"
    assert item.author == "测试记者"
    assert item.lineage_hint is LineageKind.REPRINT
    assert item.published_at.isoformat() == "2026-07-17T07:51:00+00:00"
    assert item.publication_precision == "minute"
    assert item.publication_interval_end is None
    assert item.canonical_url == "https://www.stcn.com/article/detail/3359541.html"


def test_stcn_enabled_publisher_on_canonical_detail_may_be_original() -> None:
    html = fixture("stcn_fund_detail.html").replace("来源：中国基金报", "来源：证券时报")

    assert parse_stcn_detail(html, retrieved_at=NOW).lineage_hint is LineageKind.ORIGINAL


def test_stcn_fingerprints_full_content_but_bounds_utf8_excerpt() -> None:
    full_text = "基" * 700 + " 基金行业持续关注长期资金配置。"
    html = fixture("stcn_fund_detail.html").replace("政策内容。", "基" * 700)

    item = parse_stcn_detail(html, retrieved_at=NOW)

    assert item.normalized_public_content == full_text
    assert item.content_fingerprint == hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    assert item.excerpt_original_bytes == len(full_text.encode("utf-8"))
    assert len(item.excerpt.encode("utf-8")) <= 2048
    assert item.excerpt_truncated is True
    assert item.excerpt == "基" * 682


@pytest.mark.parametrize(
    "missing_label", ("来源：中国基金报", "作者：测试记者", "2026-07-17 15:51")
)
def test_stcn_detail_requires_exactly_one_source_author_and_minute(missing_label: str) -> None:
    html = fixture("stcn_fund_detail.html").replace(missing_label, "")

    with pytest.raises(IntelligenceParseError, match="STCN detail"):
        parse_stcn_detail(html, retrieved_at=NOW)


def test_eastmoney_market_parses_extended_fields_without_float_conversion() -> None:
    assert EASTMONEY_MARKET_FIELDS == "f12,f14,f3,f8,f62,f184,f104,f105"

    rows = parse_eastmoney_market(
        fixture("eastmoney_market.json"), sector_kind="industry", retrieved_at=NOW
    )

    assert rows[0].sector_code == "BK1036"
    assert rows[0].sector_name == "半导体"
    assert rows[0].pct_change == Decimal("2.5")
    assert rows[0].turnover_rate == Decimal("3.20")
    assert rows[0].main_net_inflow == Decimal("73129.17")
    assert rows[0].main_net_inflow_ratio == Decimal("1.25")
    assert rows[0].advancers == 80
    assert rows[0].decliners == 20
    assert rows[1].turnover_rate is None
    assert rows[1].main_net_inflow is None
    assert rows[1].main_net_inflow_ratio is None
    assert rows[0].retrieved_at is NOW


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload["data"]["diff"].append(payload["data"]["diff"][0]), "duplicate"),
        (lambda payload: payload["data"]["diff"][0].__setitem__("f3", "NaN"), "finite"),
        (lambda payload: payload["data"]["diff"][0].__setitem__("f104", -1), "non-negative"),
        (lambda payload: payload["data"]["diff"][0].__setitem__("f105", 10001), "breadth"),
    ),
)
def test_eastmoney_market_rejects_invalid_batch(mutation, message: str) -> None:
    payload = json.loads(fixture("eastmoney_market.json"))
    mutation(payload)

    with pytest.raises(IntelligenceParseError, match=message):
        parse_eastmoney_market(json.dumps(payload), sector_kind="industry", retrieved_at=NOW)


def test_parsers_require_utc_retrieval_times_and_exact_sector_kind() -> None:
    local_time = datetime.fromisoformat("2026-07-18T12:30:00+08:00")
    with pytest.raises(IntelligenceParseError, match="UTC"):
        parse_gov_policy_list(fixture("gov_policy.json"), retrieved_at=local_time)
    with pytest.raises(IntelligenceParseError, match="sector kind"):
        parse_eastmoney_market(fixture("eastmoney_market.json"), "theme", NOW)
