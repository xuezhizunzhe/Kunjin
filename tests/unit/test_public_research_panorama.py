from __future__ import annotations

from kunjin.public_research.panorama import build_cross_domain_panorama


def _payload(state: str) -> dict[str, object]:
    return {
        "request": {
            "workflow": "market_overview",
            "finished_at": "2026-07-21T08:10:00+00:00",
            "interval": {
                "start_at": "2026-07-01T08:00:00+00:00",
                "end_at": "2026-07-21T08:00:00+00:00",
                "timezone_name": "Asia/Shanghai",
            },
        },
        "items": [
            _fact("电力设备公开事件", "https://example.test/power"),
            _fact("人工智能公开事件", "https://example.test/ai"),
        ],
        "experimental_shadow": {
            "market_state": "neutral",
            "sector_states": [
                {"sector_name": "电力设备", "state": state},
                {"sector_name": "人工智能", "state": "overheating_risk"},
            ],
        },
        "fund_relevance": {"subject_fund_code": None, "coverage_scope": None},
        "missing_evidence": [], "conflicts": [], "cross_validation": {"complete": False},
    }


def _fact(title: str, url: str, publisher: str = "公开来源") -> dict[str, object]:
    return {
        "evidence_role": "source_fact",
        "publisher": publisher,
        "published_at": "2026-07-20T08:00:00+00:00",
        "retrieved_at": "2026-07-20T08:10:00+00:00",
        "canonical_url": url,
        "source_tier": "tier_1",
        "title": title,
        "excerpt": "可核验摘要",
        "integrity_state": "active",
    }


def test_panorama_selects_at_most_three_candidates_and_keeps_windows() -> None:
    power_states = ("improving", "neutral", "weakening")
    windows = []
    for index, state in enumerate(power_states):
        payload = _payload(state)
        payload["items"] = [
            _fact("电力设备公开事件", f"https://example.test/power-{index}"),
            _fact("人工智能公开事件", f"https://example.test/ai-{index}"),
        ]
        windows.append((("近一周", "近一月", "近六个月")[index], payload))

    result = build_cross_domain_panorama(
        tuple(windows)
    )

    assert len(result["candidate_directions"]) == 2
    assert result["candidate_directions"][0]["domain_id"] == "ai_compute"
    assert result["candidate_directions"][0]["evidence_basis"] == "multiple_windows"
    assert [item["label"] for item in result["time_windows"]] == ["近一周", "近一月", "近六个月"]
    assert result["conditional_guidance"]["automatic_trade"] is False


def test_panorama_skips_observed_facts_without_a_direction_signal() -> None:
    payload = _payload("insufficient_data")
    payload["experimental_shadow"]["sector_states"] = [
        {"sector_name": "电力设备", "state": "insufficient_data"}
    ]
    payload["items"] = [_fact("人工智能公开事件", "https://example.test/ai")]

    result = build_cross_domain_panorama((("近一周", payload),))

    assert result["candidate_directions"] == []
    assert result["conclusion"]["state"] == "preliminary_research_available"
    assert result["preliminary_directions"] == [
        {
            "label": "预备研究方向",
            "domain_id": "ai_compute",
            "domain_name": "AI 与算力",
            "evidence_level": "preliminary",
            "observed_in": ["近一周"],
            "facts": [
                {
                    "window": "近一周",
                    "label": "可核验事实",
                    "title": "人工智能公开事件",
                    "what_happened": "可核验摘要",
                    "source": {
                        "source_name": "example.test（待核验）",
                        "reported_source_name": "公开来源",
                        "source_kind": "media_report",
                        "url": "https://example.test/ai",
                        "published_at": "2026-07-20T08:00:00+00:00",
                        "retrieved_at": "2026-07-20T08:10:00+00:00",
                        "source_tier": "tier_1",
                    },
                    "statistics_period": None,
                }
            ],
            "signal": "证据不足",
            "why_matched": "带日期的公开事实标题匹配人工智能公开事件",
            "alternative_explanation": "单条媒体或公开事实也可能反映短期情绪、流动性或未覆盖变量。",
            "evidence_needed": (
                "需要官方或行业协会的连续产量、订单、价格、指数或基金披露数据进一步查证。"
            ),
        }
    ]


def test_panorama_upgrades_only_independent_source_facts_in_one_window() -> None:
    payload = _payload("insufficient_data")
    payload["experimental_shadow"]["sector_states"] = [
        {"sector_name": "人工智能", "state": "improving"}
    ]
    payload["items"] = [
        _fact("人工智能公开事件一", "https://example.test/ai-one"),
        _fact("人工智能公开事件二", "https://example.org/ai-two"),
    ]

    result = build_cross_domain_panorama((("近一月", payload),))

    assert result["candidate_directions"][0]["domain_id"] == "ai_compute"
    assert result["candidate_directions"][0]["evidence_basis"] == "independent_sources"
    assert result["candidate_directions"][0]["signal"] == "值得继续研究"
    assert result["conditional_guidance"]["automatic_trade"] is False


def test_panorama_does_not_treat_one_repeated_fact_as_multi_window_confirmation() -> None:
    first = _payload("improving")
    second = _payload("improving")
    first["experimental_shadow"]["sector_states"] = [
        {"sector_name": "人工智能", "state": "improving"}
    ]
    second["experimental_shadow"]["sector_states"] = [
        {"sector_name": "人工智能", "state": "improving"}
    ]
    first["items"] = [_fact("人工智能公开事件", "https://example.test/ai")]
    second["items"] = [_fact("人工智能公开事件", "https://example.test/ai")]

    result = build_cross_domain_panorama((("近一月", first), ("近三月", second)))

    assert result["candidate_directions"] == []
    assert result["preliminary_directions"][0]["observed_in"] == ["近一月", "近三月"]


def test_panorama_downgrades_conflicting_stcn_source_attribution() -> None:
    payload = _payload("insufficient_data")
    payload["experimental_shadow"]["sector_states"] = [
        {"sector_name": "人工智能", "state": "insufficient_data"}
    ]
    payload["items"] = [
        _fact(
            "人工智能公开事件",
            "https://www.stcn.com/article/detail/example.html",
            publisher="中国证券报",
        )
    ]

    result = build_cross_domain_panorama((("近一月", payload),))

    source = result["preliminary_directions"][0]["facts"][0]["source"]
    assert source["source_name"] == "证券时报网/公开媒体（待核验）"
    assert source["reported_source_name"] == "中国证券报"
