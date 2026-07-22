from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from urllib.parse import urlparse

_MAX_FACTS = 64
_WORKFLOWS = frozenset({"news_recent", "market_overview", "fund_intelligence"})
_PRIVATE_KEYS = frozenset(
    {
        "amount",
        "authorization",
        "cookie",
        "cookies",
        "local_path",
        "managed_path",
        "private_path",
        "raw_body",
        "raw_response_body",
        "response_body",
        "token",
    }
)
_MARKET_STATE_LABELS = {
    "offensive_bias": "偏积极",
    "neutral": "中性",
    "defensive_bias": "偏谨慎",
    "insufficient_data": "证据不足",
}


def summarize_public_research(payload: Mapping[str, object]) -> dict[str, object]:
    """Create a bounded beginner-facing view from an existing public payload."""

    _reject_private_keys(payload)
    request = _request(payload.get("request"))
    facts = _facts(payload.get("items"))
    gaps = _string_list(payload.get("missing_evidence"), "missing_evidence")
    if not facts and "no_active_public_facts" not in gaps:
        gaps.append("no_active_public_facts")
    gaps.sort()
    shadow = _mapping(payload.get("experimental_shadow"), "experimental_shadow")
    fund_relevance = _mapping(payload.get("fund_relevance"), "fund_relevance")
    return {
        "conclusion": _conclusion(facts),
        "what_happened": facts,
        "why_it_may_matter": _analysis(shadow, fund_relevance),
        "conditional_guidance": _guidance(facts),
        "risks_and_unknowns": _risks(payload, gaps),
        "sources": _sources(facts),
        "retrieval": {
            "workflow": request["workflow"],
            "interval": request["interval"],
            "retrieved_at": request["finished_at"],
        },
    }


def _request(value: object) -> dict[str, object]:
    request = _mapping(value, "request")
    workflow = request.get("workflow")
    if workflow not in _WORKFLOWS:
        _invalid("workflow is unsupported")
    finished_at = _timestamp(request.get("finished_at"), "request finished_at")
    interval = _mapping(request.get("interval"), "request interval")
    for key in ("start_at", "end_at"):
        _timestamp(interval.get(key), f"request interval {key}")
    if not isinstance(interval.get("timezone_name"), str) or not interval["timezone_name"]:
        _invalid("request interval timezone is invalid")
    return {
        "workflow": workflow,
        "finished_at": finished_at,
        "interval": {
            "start_at": interval["start_at"],
            "end_at": interval["end_at"],
            "timezone_name": interval["timezone_name"],
        },
    }


def _facts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _invalid("items is invalid")
    if len(value) > _MAX_FACTS:
        _invalid("items exceeds its limit")
    facts = []
    for item in value:
        record = _mapping(item, "item")
        if record.get("integrity_state") != "active":
            continue
        if record.get("evidence_role") != "source_fact":
            _invalid("item evidence role is invalid")
        source = {
            "source_name": _text(record.get("publisher"), "item publisher"),
            "url": _http_url(record.get("canonical_url")),
            "published_at": _timestamp(record.get("published_at"), "item published_at"),
            "source_tier": _text(record.get("source_tier"), "item source_tier"),
            "retrieved_at": _timestamp(record.get("retrieved_at"), "item retrieved_at"),
        }
        facts.append(
            {
                "label": "可核验事实",
                "title": _text(record.get("title"), "item title"),
                "excerpt": _text(record.get("excerpt"), "item excerpt"),
                "source": source,
            }
        )
    return facts


def _analysis(
    shadow: Mapping[str, object], fund_relevance: Mapping[str, object]
) -> dict[str, str]:
    market_state = shadow.get("market_state")
    market_text = _MARKET_STATE_LABELS.get(market_state, "证据不足")
    text = f"本次公开信息中的市场状态仅作为分析线索，当前为{market_text}。"
    if fund_relevance.get("coverage_scope") is not None:
        text += "基金关联仅依据带日期的披露持仓、基准或指数关系，不代表实时完整持仓。"
    return {"label": "系统分析", "text": text}


def _conclusion(facts: Sequence[Mapping[str, object]]) -> dict[str, str]:
    if not facts:
        return {
            "state": "insufficient_data",
            "text": "本次公开信息不足，暂不形成方向判断。",
        }
    return {
        "state": "evidence_backed_research",
        "text": "已整理可核验的公开信息，适合继续研究，不构成买卖结论。",
    }


def _guidance(facts: Sequence[Mapping[str, object]]) -> dict[str, object]:
    text = (
        "可继续关注相关公开信息，但不构成买卖指令。"
        if facts
        else "建议先补充公开资料后再做人工复核，不构成买卖指令。"
    )
    return {
        "label": "条件性建议",
        "text": text,
        "action_authorized": False,
        "automatic_trade": False,
    }


def _risks(payload: Mapping[str, object], gaps: list[str]) -> dict[str, object]:
    cross_validation = _mapping(payload.get("cross_validation"), "cross_validation")
    risks = ["公开信息可能不完整、滞后或缺少独立交叉验证。"]
    if cross_validation.get("complete") is not True:
        risks.append("本次结果不代表已完成跨来源交叉验证。")
    if _sequence(payload.get("conflicts"), "conflicts"):
        risks.append("现有来源中存在待人工复核的冲突信息。")
    return {
        "label": "风险与证据缺口",
        "major_risks": risks,
        "evidence_gaps": gaps,
    }


def _sources(facts: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    values = []
    seen = set()
    for fact in facts:
        source = fact["source"]
        if not isinstance(source, Mapping):
            _invalid("fact source is invalid")
        identity = tuple(source.items())
        if identity not in seen:
            seen.add(identity)
            values.append(dict(source))
    return values


def _reject_private_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _invalid("mapping key is invalid")
            if key.lower() in _PRIVATE_KEYS:
                _invalid("private field is not allowed")
            _reject_private_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            _reject_private_keys(child)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _invalid(f"{label} is invalid")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        _invalid(f"{label} is invalid")
    return value


def _string_list(value: object, label: str) -> list[str]:
    values = _sequence(value, label)
    result = []
    for item in values:
        result.append(_text(item, label))
    return sorted(set(result))


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid(f"{label} is invalid")
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _invalid(f"{label} is invalid")
    if parsed.tzinfo is None:
        _invalid(f"{label} is invalid")
    return text


def _http_url(value: object) -> str:
    url = _text(value, "item canonical_url")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _invalid("item canonical_url is invalid")
    return url


def _invalid(detail: str) -> None:
    raise ValueError(f"public research payload {detail}")
