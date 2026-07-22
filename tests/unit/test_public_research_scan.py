from __future__ import annotations

from kunjin.public_research.scan import scan_public_research


def _payload() -> dict[str, object]:
    return {
        "request": {
            "workflow": "market_overview",
            "finished_at": "2026-07-21T08:10:00+00:00",
            "interval": {
                "start_at": "2026-07-18T08:00:00+00:00",
                "end_at": "2026-07-21T08:00:00+00:00",
                "timezone_name": "Asia/Shanghai",
            },
        },
        "items": [
            {
                "evidence_role": "source_fact",
                "publisher": "公开来源",
                "published_at": "2026-07-20T08:00:00+00:00",
                "retrieved_at": "2026-07-20T08:10:00+00:00",
                "canonical_url": "https://example.test/policy",
                "source_tier": "tier_1",
                "title": "公开政策事件",
                "excerpt": "可核验摘要",
                "integrity_state": "active",
            }
        ],
        "experimental_shadow": {
            "status": "experimental_shadow",
            "market_state": "neutral",
            "market_direction_status": "evidence_only",
            "sector_states": [
                {"sector_name": "电力设备", "state": "improving"},
                {"sector_name": "人工智能", "state": "overheating_risk"},
                {"sector_name": "汽车整车", "state": "neutral"},
            ],
        },
        "fund_relevance": {"subject_fund_code": None, "coverage_scope": None},
        "missing_evidence": [],
        "conflicts": [],
        "cross_validation": {"complete": False},
    }


def test_scan_discovers_multiple_unrequested_directions_from_sector_states() -> None:
    result = scan_public_research(_payload())

    directions = {item["domain_id"]: item for item in result["directions"]}
    assert directions["power_energy"]["matched_sectors"] == ["电力设备"]
    assert directions["ai_compute"]["signal"] == "需要谨慎"
    assert directions["autos"]["signal"] == "继续观察"
    assert directions["shipping_trade"]["evidence_state"] == "insufficient_data"
    assert directions["industrial_commodities"]["evidence_state"] == "insufficient_data"
    assert result["timeline"][0]["source"]["url"] == "https://example.test/policy"
    assert result["automatic_industry_data"]["state"] == "network_refresh_needed"


def test_scan_keeps_facts_and_analysis_separate() -> None:
    result = scan_public_research(_payload())

    assert result["timeline"][0]["label"] == "可核验事实"
    assert result["directions"][0]["label"] == "系统分析"
    assert result["conditional_guidance"]["automatic_trade"] is False
