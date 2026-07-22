"""Persistent, bounded public-indicator evidence for outer browser research."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from kunjin.public_research.supplement import (
    _timeline_period,
    build_supplement_timeline,
    summarize_user_supplied_evidence,
)
from kunjin.storage.repository import Repository

_VERIFIED_STATE = "outer_page_verified"
_MAX_EXCERPT = 1_000


def persist_verified_evidence(
    repository: Repository, material: Mapping[str, object]
) -> dict[str, object]:
    """Store one externally read public indicator without retaining page content."""

    if material.get("source_verification_state") != _VERIFIED_STATE:
        raise ValueError("outer page verification is required before persistence")
    projection = summarize_user_supplied_evidence(material)
    fact = projection.get("fact")
    if not isinstance(fact, Mapping) or fact.get("fact_kind") != "indicator":
        raise ValueError("only verified traceable indicators may be persisted")
    source = fact.get("source")
    indicator = fact.get("indicator")
    use = projection.get("current_research_use")
    if (
        not isinstance(source, Mapping)
        or not isinstance(indicator, Mapping)
        or not isinstance(use, Mapping)
    ):
        raise ValueError("verified indicator structure is invalid")

    domain_id = _required_text(use.get("domain_id"), "domain")
    source_name = _required_text(source.get("source_name"), "source name")
    publisher = _optional_text(material.get("publisher"), "publisher") or source_name
    source_kind = _required_text(source.get("source_kind_claimed"), "source kind")
    source_tier = _required_text(source.get("research_source_level"), "source tier")
    if source_tier not in {"tier_1", "tier_2"}:
        raise ValueError("verified indicator source tier is invalid")
    title = _required_text(fact.get("title"), "title")
    original_url = _required_text(source.get("url"), "original URL")
    published_at = _required_text(source.get("published_at"), "publication time")
    statistics_period = _required_text(indicator.get("statistics_period"), "statistics period")
    indicator_name = _required_text(indicator.get("name"), "indicator name")
    indicator_value = _required_text(indicator.get("value"), "indicator value")
    unit = _required_text(indicator.get("unit"), "unit")
    methodology = _optional_text(indicator.get("methodology"), "methodology")
    short_excerpt = _optional_excerpt(material.get("short_excerpt"))
    if _timeline_period(statistics_period) is None:
        raise ValueError("verified indicator statistics period is not comparable")

    record = {
        "domain_id": domain_id,
        "source_name": source_name,
        "publisher": publisher,
        "source_kind": source_kind,
        "source_tier": source_tier,
        "title": title,
        "original_url": original_url,
        "published_at": published_at,
        "statistics_period": statistics_period,
        "indicator_name": indicator_name,
        "indicator_value": indicator_value,
        "unit": unit,
        "methodology": methodology,
        "short_excerpt": short_excerpt,
    }
    record_sha256 = _sha256(record)
    excerpt_sha256 = hashlib.sha256((short_excerpt or "").encode("utf-8")).hexdigest()
    retrieved_at = datetime.now(timezone.utc).isoformat()

    with repository.connect() as connection, connection:
        duplicate = connection.execute(
            "SELECT id, revision_of_evidence_id FROM public_research_evidence "
            "WHERE record_sha256=?",
            (record_sha256,),
        ).fetchone()
        if duplicate is not None:
            return {
                "storage_state": "duplicate_unchanged",
                "evidence_id": int(duplicate["id"]),
                "revision_of_evidence_id": duplicate["revision_of_evidence_id"],
                "record_sha256": record_sha256,
            }

        revision = connection.execute(
            """
            SELECT id FROM public_research_evidence
            WHERE domain_id=? AND indicator_name=? AND unit=? AND statistics_period=?
              AND original_url=? AND source_name=?
              AND id NOT IN (
                  SELECT revision_of_evidence_id FROM public_research_evidence
                  WHERE revision_of_evidence_id IS NOT NULL
              )
            ORDER BY published_at DESC, id DESC LIMIT 1
            """,
            (
                domain_id,
                indicator_name,
                unit,
                statistics_period,
                original_url,
                source_name,
            ),
        ).fetchone()
        cursor = connection.execute(
            """
            INSERT INTO public_research_evidence(
                domain_id, source_name, publisher, source_kind, source_tier, title,
                original_url, published_at, statistics_period, indicator_name,
                indicator_value, unit, methodology, short_excerpt, excerpt_sha256,
                verification_state, revision_of_evidence_id, retrieved_at, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                source_name,
                publisher,
                source_kind,
                source_tier,
                title,
                original_url,
                published_at,
                statistics_period,
                indicator_name,
                indicator_value,
                unit,
                methodology,
                short_excerpt,
                excerpt_sha256,
                _VERIFIED_STATE,
                None if revision is None else int(revision["id"]),
                retrieved_at,
                record_sha256,
            ),
        )
        evidence_id = int(cursor.lastrowid)
    return {
        "storage_state": "stored",
        "evidence_id": evidence_id,
        "revision_of_evidence_id": None if revision is None else int(revision["id"]),
        "record_sha256": record_sha256,
        "retrieved_at": retrieved_at,
    }


def build_persisted_timeline(
    repository: Repository, domain_id: str, indicator_name: str, unit: str
) -> dict[str, object]:
    """Rebuild one comparable timeline from currently effective verified evidence."""

    rows = _current_rows(repository, domain_id, indicator_name, unit)
    materials = tuple(_row_material(row) for row in rows)
    if len(materials) > 12:
        materials = materials[-12:]
    if len(materials) < 2:
        return _insufficient_persisted_timeline(rows, domain_id, indicator_name, unit)
    timeline = build_supplement_timeline(materials)
    evidence_by_key = {
        (str(row["statistics_period"]), str(row["indicator_value"]), str(row["original_url"])): row
        for row in rows
    }
    for item in timeline["timeline"]:
        if isinstance(item, dict):
            item["label"] = "经外层核验的结构化公开事实"
            evidence = evidence_by_key.get(
                (str(item["statistics_period"]), str(item["value"]), str(item["url"]))
            )
            if evidence is not None:
                item.update(
                    {
                        "evidence_id": int(evidence["id"]),
                        "publisher": str(evidence["publisher"]),
                        "source_tier": str(evidence["source_tier"]),
                        "retrieved_at": str(evidence["retrieved_at"]),
                    }
                )
    timeline["as_of"] = datetime.now(timezone.utc).isoformat()
    timeline["conclusion"] = {
        "state": timeline["conclusion"]["state"],
        "text": "已从持久化、经外层核验的同口径公开事实重建时间线，仅用于继续研究。",
    }
    timeline["current_research_use"] = {
        "state": "used_in_persisted_timeline",
        "strong_direction_eligible": False,
        "fund_mapping_boundary": "基金关联仍只可使用带日期的基准、指数或披露持仓。",
    }
    timeline["storage"] = {
        "state": "verified_structured_evidence",
        "evidence_count": len(rows),
        "fulltext_stored": False,
    }
    timeline["automatic_industry_data"] = {
        "state": "outer_browser_evidence_available",
        "text": "本时间线来自外层按需读取并核验后保存的公开字段；KunJin 未自行抓取网页。",
    }
    return timeline


def build_refresh_plan(
    repository: Repository,
    domain_id: str,
    indicator_name: str,
    unit: str,
    through_period: str,
    from_period: str | None = None,
) -> dict[str, object]:
    """Plan bounded outer research without making a network request."""

    requested = _timeline_period(through_period)
    if requested is None:
        raise ValueError("refresh through period is not comparable")
    requested_start = requested if from_period is None else _timeline_period(from_period)
    if requested_start is None or requested_start[:2] != requested[:2]:
        raise ValueError("refresh start period does not match requested timeline")
    if requested_start[2] > requested[2]:
        raise ValueError("refresh start period is after requested timeline")
    rows = _current_rows(repository, domain_id, indicator_name, unit)
    periods = sorted({str(row["statistics_period"]) for row in rows}, key=_period_sort_key)
    parsed = [_timeline_period(period) for period in periods]
    if any(period is None for period in parsed):
        raise ValueError("stored evidence statistics period is not comparable")
    comparable = [period for period in parsed if period is not None]
    if comparable and any(period[:2] != requested[:2] for period in comparable):
        raise ValueError("refresh period does not match stored timeline granularity")

    if comparable:
        first_index = min(requested_start[2], min(period[2] for period in comparable))
        covered_indexes = {period[2] for period in comparable}
        upper_index = requested[2]
        new_periods = [
            _format_period(requested[0], requested[1], index)
            for index in range(first_index, upper_index + 1)
            if index not in covered_indexes
        ]
        revision_periods = [
            period for period in periods if _period_sort_key(period) < requested
        ]
    else:
        new_periods = [
            _format_period(requested[0], requested[1], index)
            for index in range(requested_start[2], requested[2] + 1)
        ]
        revision_periods = []

    conflict_periods = _conflicting_periods(rows)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "domain_id": domain_id,
        "indicator_name": indicator_name,
        "unit": unit,
        "requested_through_period": through_period,
        "requested_from_period": from_period,
        "covered_periods": periods,
        "new_periods_to_fetch": new_periods,
        "revision_check_periods": revision_periods,
        "conflicting_periods": conflict_periods,
        "historical_evidence_state": (
            "usable_pending_lightweight_revision_check"
            if revision_periods
            else "no_prior_verified_coverage"
        ),
        "network_action": "outer_research_only",
        "failure_fallback": "retain_dated_history_and_report_unrefreshed_periods",
    }


def _current_rows(
    repository: Repository, domain_id: str, indicator_name: str, unit: str
) -> list[Any]:
    for value, name in ((domain_id, "domain"), (indicator_name, "indicator name"), (unit, "unit")):
        _required_text(value, name)
    with repository.connect() as connection:
        return connection.execute(
            """
            SELECT * FROM public_research_evidence AS evidence
            WHERE evidence.domain_id=? AND evidence.indicator_name=? AND evidence.unit=?
              AND evidence.verification_state=?
              AND NOT EXISTS (
                  SELECT 1 FROM public_research_evidence AS revision
                  WHERE revision.revision_of_evidence_id=evidence.id
              )
            ORDER BY evidence.statistics_period, evidence.published_at, evidence.id
            """,
            (domain_id, indicator_name, unit, _VERIFIED_STATE),
        ).fetchall()


def _row_material(row: Any) -> dict[str, object]:
    return {
        "source_name": str(row["source_name"]),
        "source_kind": str(row["source_kind"]),
        "title": str(row["title"]),
        "published_at": str(row["published_at"]),
        "original_url": str(row["original_url"]),
        "statistics_period": str(row["statistics_period"]),
        "indicator_name": str(row["indicator_name"]),
        "indicator_value": str(row["indicator_value"]),
        "unit": str(row["unit"]),
        "methodology": row["methodology"],
        "domain_id": str(row["domain_id"]),
        "source_verification_state": _VERIFIED_STATE,
    }


def _insufficient_persisted_timeline(
    rows: list[Any], domain_id: str, indicator_name: str, unit: str
) -> dict[str, object]:
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "conclusion": {
            "state": "insufficient_data",
            "text": "已持久化的同口径可追溯指标不足两期，暂不能重建时间线。",
        },
        "timeline": [],
        "coverage": {
            "domains": [domain_id],
            "indicator_name": indicator_name,
            "unit": unit,
            "covered_periods": [str(row["statistics_period"]) for row in rows],
            "missing_periods": [],
            "duplicate_periods": [],
            "conflicting_periods": _conflicting_periods(rows),
        },
        "evidence_gaps": ["persisted_timeline_requires_two_comparable_periods"],
        "current_research_use": {
            "state": "not_used_in_timeline",
            "strong_direction_eligible": False,
            "fund_mapping_boundary": "基金关联仍只可使用带日期的基准、指数或披露持仓。",
        },
        "storage": {
            "state": "verified_structured_evidence",
            "evidence_count": len(rows),
            "fulltext_stored": False,
        },
    }


def _conflicting_periods(rows: list[Any]) -> list[str]:
    values_by_period: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        values_by_period.setdefault(str(row["statistics_period"]), set()).add(
            (str(row["indicator_value"]), str(row["unit"]))
        )
    return sorted(
        (period for period, values in values_by_period.items() if len(values) > 1),
        key=_period_sort_key,
    )


def _period_sort_key(value: str) -> tuple[int, int, int]:
    parsed = _timeline_period(value)
    if parsed is None:
        raise ValueError("statistics period is not comparable")
    return parsed


def _format_period(year: int, kind: int, index: int) -> str:
    return f"{year}年{index}月" if kind == 1 else f"{year}年第{index}季度"


def _sha256(value: Mapping[str, object]) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value, name)
    if text is None:
        raise ValueError(f"verified indicator {name} is required")
    return text


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not (text := " ".join(value.split())):
        raise ValueError(f"verified indicator {name} is invalid")
    return text


def _optional_excerpt(value: object) -> str | None:
    excerpt = _optional_text(value, "short excerpt")
    if excerpt is not None and len(excerpt) > _MAX_EXCERPT:
        raise ValueError("verified indicator short excerpt is invalid")
    return excerpt
