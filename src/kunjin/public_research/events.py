"""Bounded persistence and grouping for externally verified public events."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from kunjin.public_research.supplement import _source_level
from kunjin.storage.repository import Repository

_VERIFIED_STATE = "outer_page_verified"
_FACT_SOURCE_KINDS = frozenset({"official", "industry_data", "platform_data"})
_SOURCE_KINDS = _FACT_SOURCE_KINDS | frozenset({"media", "community"})


def persist_verified_event(
    repository: Repository, material: Mapping[str, object]
) -> dict[str, object]:
    """Store one verified public event with fact, reported-fact, or lead state."""

    if material.get("source_verification_state") != _VERIFIED_STATE:
        raise ValueError("outer page verification is required before persistence")
    source_kind = _required_text(material.get("source_kind"), "source kind")
    if source_kind not in _SOURCE_KINDS:
        raise ValueError("event source kind is invalid")
    original_url = _https_url(material.get("original_url"))
    published_at = _timestamp(material.get("published_at"), "publication time")
    occurred_at = _optional_timestamp(material.get("event_occurred_at"), "event time")
    domain_id = _required_text(material.get("domain_id"), "domain")
    source_name = _required_text(material.get("source_name"), "source name")
    publisher = _optional_text(material.get("publisher"), "publisher") or source_name
    title = _required_text(material.get("title"), "title")
    fact_summary = _required_text(material.get("fact_summary"), "fact summary")
    claim_boundary = _required_text(material.get("claim_boundary"), "claim boundary")
    event_key = _required_text(material.get("event_key"), "event key")
    if not 16 <= len(event_key) <= 64:
        raise ValueError("event key is invalid")
    fact_key = _optional_text(material.get("event_fact_key"), "event fact key")
    fact_value = _optional_text(material.get("event_fact_value"), "event fact value")
    fact_unit = _optional_text(material.get("event_fact_unit"), "event fact unit")
    if any(value is not None for value in (fact_key, fact_value, fact_unit)) and not all(
        value is not None for value in (fact_key, fact_value, fact_unit)
    ):
        raise ValueError("event comparable fact is incomplete")
    short_excerpt = _optional_text(material.get("short_excerpt"), "short excerpt")
    if short_excerpt is not None and len(short_excerpt) > 1_000:
        raise ValueError("event short excerpt is invalid")
    source_tier = (
        _source_level(
            {
                "source_kind": source_kind,
                "original_url": original_url,
                "source_verification_state": _VERIFIED_STATE,
            }
        )
        if source_kind != "community"
        else "lead"
    )
    evidence_state = _evidence_state(source_kind)
    record = {
        "event_key": event_key,
        "domain_id": domain_id,
        "source_name": source_name,
        "publisher": publisher,
        "source_kind": source_kind,
        "source_tier": source_tier,
        "title": title,
        "original_url": original_url,
        "event_occurred_at": occurred_at,
        "published_at": published_at,
        "fact_summary": fact_summary,
        "claim_boundary": claim_boundary,
        "event_fact_key": fact_key,
        "event_fact_value": fact_value,
        "event_fact_unit": fact_unit,
        "short_excerpt": short_excerpt,
    }
    record_sha256 = _sha256(record)
    excerpt_sha256 = hashlib.sha256((short_excerpt or "").encode("utf-8")).hexdigest()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    with repository.connect() as connection, connection:
        existing = connection.execute(
            "SELECT id FROM public_research_events WHERE record_sha256=?", (record_sha256,)
        ).fetchone()
        if existing is not None:
            return {"storage_state": "duplicate_unchanged", "event_id": int(existing["id"])}
        cursor = connection.execute(
            """
            INSERT INTO public_research_events(
                event_key, domain_id, source_name, publisher, source_kind, source_tier,
                title, original_url, event_occurred_at, published_at, fact_summary,
                claim_boundary, event_fact_key, event_fact_value, event_fact_unit,
                short_excerpt, excerpt_sha256, verification_state, evidence_state, retrieved_at,
                record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                domain_id,
                source_name,
                publisher,
                source_kind,
                source_tier,
                title,
                original_url,
                occurred_at,
                published_at,
                fact_summary,
                claim_boundary,
                fact_key,
                fact_value,
                fact_unit,
                short_excerpt,
                excerpt_sha256,
                _VERIFIED_STATE,
                evidence_state,
                retrieved_at,
                record_sha256,
            ),
        )
    return {"storage_state": "stored", "event_id": int(cursor.lastrowid), "event_key": event_key}


def build_persisted_event_timeline(
    repository: Repository, domain_id: str, *, recent_days: int = 30
) -> dict[str, object]:
    """Group stored sources for events without converting claimed causes into facts."""

    if recent_days not in {7, 30, 90, 180}:
        raise ValueError("event recent window is invalid")
    with repository.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM public_research_events WHERE domain_id=?
            ORDER BY COALESCE(event_occurred_at, published_at), published_at, id
            """,
            (domain_id,),
        ).fetchall()
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["event_key"]), []).append(row)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=recent_days)
    events = []
    for event_key, sources in grouped.items():
        comparable = {
            (str(row["event_fact_key"]), str(row["event_fact_value"]), str(row["event_fact_unit"]))
            for row in sources
            if row["event_fact_key"] is not None
        }
        facts = sorted(
            {
                str(row["fact_summary"])
                for row in sources
                if row["evidence_state"] == "fact"
            }
        )
        reported_facts = sorted(
            {
                str(row["fact_summary"])
                for row in sources
                if row["evidence_state"] == "reported_fact"
            }
        )
        events.append(
            {
                "event_key": event_key,
                "event_dates": sorted(
                    {str(row["event_occurred_at"]) for row in sources if row["event_occurred_at"]}
                ),
                "published_at": [str(row["published_at"]) for row in sources],
                "fact_summaries": facts,
                "reported_facts": reported_facts,
                "claim_boundaries": sorted({str(row["claim_boundary"]) for row in sources}),
                "sources": [
                    {
                        "event_id": int(row["id"]),
                        "publisher": str(row["publisher"]),
                        "source_tier": str(row["source_tier"]),
                        "source_kind": str(row["source_kind"]),
                        "evidence_state": str(row["evidence_state"]),
                        "url": str(row["original_url"]),
                        "published_at": str(row["published_at"]),
                        "retrieved_at": str(row["retrieved_at"]),
                    }
                    for row in sources
                ],
                "comparable_fact_conflict": len(comparable) > 1,
                "direct_fact_source_count": sum(
                    1 for row in sources if row["evidence_state"] == "fact"
                ),
                "reported_fact_source_count": sum(
                    1 for row in sources if row["evidence_state"] == "reported_fact"
                ),
                "recent_window_state": (
                    "recent"
                    if any(
                        _parse_timestamp(row["published_at"]) >= cutoff for row in sources
                    )
                    else "historical"
                ),
            }
        )
    return {
        "as_of": now.isoformat(),
        "domain_id": domain_id,
        "recent_window_days": recent_days,
        "events": events,
        "analysis_boundary": "事实摘要与来源声明分开；文章声称的市场原因未因保存而成为事实。",
        "network_action": "outer_research_only",
    }


def _evidence_state(source_kind: str) -> str:
    if source_kind == "community":
        return "lead"
    if source_kind == "media":
        return "reported_fact"
    return "fact"


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _https_url(value: object) -> str:
    url = _required_text(value, "original URL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("event original URL is invalid")
    return url


def _timestamp(value: object, name: str) -> str:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"event {name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"event {name} is invalid")
    return text


def _optional_timestamp(value: object, name: str) -> str | None:
    return None if value is None else _timestamp(value, name)


def _parse_timestamp(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value, name)
    if text is None:
        raise ValueError(f"event {name} is required")
    return text


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (text := " ".join(value.split())) or len(text) > 1_000:
        raise ValueError(f"event {name} is invalid")
    return text
