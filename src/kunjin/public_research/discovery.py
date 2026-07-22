"""Bounded outer-discovery plans and completion semantics.

KunJin does not fetch pages here. Codex's browser layer records what it actually
read so a search result page cannot be presented as a completed news refresh.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_MAX_CANDIDATES = 3
_MAX_DIRECT_PAGE_ATTEMPTS = 2
_TRUSTED_SOURCE_CLASSES = frozenset(
    {
        "official_or_regulator",
        "exchange_or_company_announcement",
        "industry_association_or_structured_data",
        "credible_financial_media",
    }
)
_ATTEMPT_ROLES = ("primary", "trusted_alternative")
_READ_STATES = frozenset({"read", "blocked"})


def build_candidate_discovery_plan(scan_payload: Mapping[str, object]) -> dict[str, object]:
    """Plan one bounded, domain-specific outer search for each selected direction."""

    candidates = scan_payload.get("candidate_directions")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("public research candidate directions are invalid")
    plans = []
    seen = set()
    for candidate in candidates:
        if len(plans) >= _MAX_CANDIDATES:
            break
        if not isinstance(candidate, Mapping):
            raise ValueError("public research candidate direction is invalid")
        domain_id = _text(candidate.get("domain_id"), "candidate domain")
        domain_name = _text(candidate.get("domain_name"), "candidate domain name")
        if domain_id in seen:
            continue
        seen.add(domain_id)
        plans.append(
            {
                "domain_id": domain_id,
                "domain_name": domain_name,
                "query": f"{domain_name} 最近一周 行业数据 市场变化",
                "discovery_query_executed": False,
                "direct_page_read_count": 0,
                "independent_source_count": 0,
                "newly_persisted_evidence_count": 0,
                "current_news_refresh_state": "pending",
                "attempt_limits": {
                    "search_queries": 1,
                    "direct_page_attempts": _MAX_DIRECT_PAGE_ATTEMPTS,
                    "primary_direct_page_reads": 1,
                    "trusted_alternative_direct_page_reads": 1,
                },
                "source_order": [
                    "official_or_regulator",
                    "exchange_or_company_announcement",
                    "industry_association_or_structured_data",
                    "credible_financial_media",
                ],
                "completion_rule": (
                    "完成要求本方向已执行发现查询，且至少读取一个可信直接页面并核验当前窗口材料；"
                    "搜索结果页、媒体线索或转载本身不能构成完成。"
                ),
                "failure_fallback": (
                    "主页面受阻后最多读取一个可信替代页；仍不足则记录 partial 或 blocked，"
                    "不继续轮询。"
                ),
            }
        )
    return {
        "candidate_plans": plans,
        "outer_discovery_required": bool(plans),
        "network_action": "outer_browser_research_only",
        "analysis_boundary": (
            "搜索页只用于发现；事实是否保存仍取决于外层对直接页面字段、来源和日期的核验。"
        ),
    }


def assess_candidate_discovery_outcome(
    plan: Mapping[str, object],
    *,
    discovery_query_executed: bool,
    direct_page_attempts: Sequence[Mapping[str, object]] = (),
    newly_persisted_evidence_count: int = 0,
    discovery_blocked: bool = False,
) -> dict[str, object]:
    """Describe a bounded browser-research outcome without performing network I/O."""

    domain_id = _text(plan.get("domain_id"), "candidate domain")
    attempt_limits = plan.get("attempt_limits")
    if not isinstance(attempt_limits, Mapping):
        raise ValueError("candidate discovery plan is invalid")
    limit = attempt_limits.get("direct_page_attempts")
    if type(limit) is not int or limit != _MAX_DIRECT_PAGE_ATTEMPTS:
        raise ValueError("candidate discovery plan is invalid")
    if type(discovery_query_executed) is not bool or type(discovery_blocked) is not bool:
        raise ValueError("discovery execution state is invalid")
    if type(newly_persisted_evidence_count) is not int or newly_persisted_evidence_count < 0:
        raise ValueError("persisted evidence count is invalid")
    if len(direct_page_attempts) > limit:
        raise ValueError("candidate discovery direct-page attempt limit exceeded")

    parsed_attempts = [
        _parse_attempt(item, index=index) for index, item in enumerate(direct_page_attempts)
    ]
    direct_reads = [item for item in parsed_attempts if item["read_state"] == "read"]
    validated_reads = [
        item
        for item in direct_reads
        if item["source_class"] in _TRUSTED_SOURCE_CLASSES
        and item["current_window_validated"]
    ]
    identities = {
        _source_identity(item)
        for item in direct_reads
        if item["source_class"] in _TRUSTED_SOURCE_CLASSES
    }
    state = _refresh_state(
        discovery_query_executed=discovery_query_executed,
        direct_attempt_count=len(parsed_attempts),
        validated_reads=validated_reads,
        discovery_blocked=discovery_blocked,
    )
    return {
        "domain_id": domain_id,
        "discovery_query_executed": discovery_query_executed,
        "direct_page_attempt_count": len(parsed_attempts),
        "direct_page_read_count": len(direct_reads),
        "independent_source_count": len(identities),
        "newly_persisted_evidence_count": newly_persisted_evidence_count,
        "current_news_refresh_state": state,
        "direct_page_attempts": parsed_attempts,
        "state_boundary": _state_boundary(state),
    }


def _parse_attempt(value: Mapping[str, object], *, index: int) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("candidate discovery attempt is invalid")
    expected_role = _ATTEMPT_ROLES[index]
    role = value.get("attempt_role")
    source_class = value.get("source_class")
    read_state = value.get("read_state")
    validated = value.get("current_window_validated")
    if (
        role != expected_role
        or source_class not in _TRUSTED_SOURCE_CLASSES
        or read_state not in _READ_STATES
        or type(validated) is not bool
    ):
        raise ValueError("candidate discovery attempt is invalid")
    publisher = _optional_text(value.get("original_publisher"), "original publisher")
    attribution = _optional_text(value.get("data_attribution"), "data attribution")
    host = _optional_text(value.get("source_host"), "source host")
    is_repost = value.get("is_repost", False)
    if type(is_repost) is not bool:
        raise ValueError("candidate discovery repost state is invalid")
    if read_state == "blocked" and validated:
        raise ValueError("blocked candidate page cannot validate current material")
    return {
        "attempt_role": role,
        "source_class": source_class,
        "read_state": read_state,
        "current_window_validated": validated,
        "original_publisher": publisher,
        "data_attribution": attribution,
        "source_host": host,
        "is_repost": is_repost,
    }


def _refresh_state(
    *,
    discovery_query_executed: bool,
    direct_attempt_count: int,
    validated_reads: Sequence[Mapping[str, object]],
    discovery_blocked: bool,
) -> str:
    if discovery_query_executed and validated_reads:
        return "completed"
    if discovery_blocked or direct_attempt_count == _MAX_DIRECT_PAGE_ATTEMPTS:
        return "blocked"
    if discovery_query_executed or direct_attempt_count:
        return "partial"
    return "pending"


def _source_identity(attempt: Mapping[str, object]) -> tuple[str, str]:
    for key in ("original_publisher", "data_attribution", "source_host"):
        value = attempt.get(key)
        if isinstance(value, str) and value:
            return (key, value.casefold())
    return ("unattributed", str(attempt["source_class"]))


def _state_boundary(state: str) -> str:
    return {
        "completed": "已直读可信页面并核验当前窗口材料；仍需保留来源和字段边界。",
        "partial": "已进行发现但未完成可信直接页面核验，不能声称已完成互联网近期刷新。",
        "blocked": "受限尝试已耗尽或环境阻塞，保留历史证据并明确本次未刷新部分。",
        "pending": "尚未执行本方向外层发现，不能声称已主动查询。",
    }[state]


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not (text := " ".join(value.split())):
        raise ValueError(f"{name} is invalid")
    return text


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name)
