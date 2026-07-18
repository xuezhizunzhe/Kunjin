from __future__ import annotations

from typing import Dict

from kunjin.intelligence.service import PragmaticIntelligenceResult


def _source_payload(result: PragmaticIntelligenceResult) -> list[dict[str, object]]:
    return [
        {
            "source_attempt_id": item.source_attempt_id,
            "source_id": item.source_id,
            "field_id": item.field_id,
            "source_tier": item.source_tier.value,
            "outcome": item.outcome.value,
            "endpoint": item.endpoint,
            "data_as_of": (
                None if item.data_as_of is None else item.data_as_of.isoformat()
            ),
            "data_as_of_semantics": (
                "http_retrieval_not_market_session"
                if item.source_id == "eastmoney_market"
                else "unavailable"
                if item.data_as_of is None
                else "source_declared_data_time"
            ),
            "retrieved_at": (
                None if item.retrieved_at is None else item.retrieved_at.isoformat()
            ),
            "retrieved_at_semantics": "http_retrieval_time",
            "completeness": item.completeness,
            "coverage_gap_codes": list(item.coverage_gap_codes),
            "reason_code": item.reason_code,
            "retryable": item.retryable,
            "cooldown_until": (
                None
                if item.cooldown_until is None
                else item.cooldown_until.isoformat()
            ),
            "manual_supplementation": item.supplementation,
        }
        for item in result.source_summaries
    ]


def _item_payload(result: PragmaticIntelligenceResult) -> list[dict[str, object]]:
    uses = {item.item_id: item.source_attempt_id for item in result.item_uses}
    lineage_by_item: dict[str, list[dict[str, str]]] = {}
    for edge in result.lineage_edges:
        payload = {
            "edge_id": edge.edge_id,
            "from_item_id": edge.from_item_id,
            "to_item_id": edge.to_item_id,
            "kind": edge.kind.value,
        }
        lineage_by_item.setdefault(edge.from_item_id, []).append(payload)
        lineage_by_item.setdefault(edge.to_item_id, []).append(payload)
    return [
        {
            "item_id": item.item_id,
            "evidence_role": "source_fact",
            "source_id": item.source_id,
            "publisher": item.publisher,
            "published_at": item.published_at.isoformat(),
            "publication_precision": item.publication_precision,
            "publication_interval_end": (
                None
                if item.publication_interval_end is None
                else item.publication_interval_end.isoformat()
            ),
            "retrieved_at": item.retrieved_at.isoformat(),
            "retrieved_at_semantics": "http_retrieval_time",
            "origin_source_attempt_id": item.source_attempt_id,
            "current_request_source_attempt_id": uses[item.item_id],
            "canonical_url": item.canonical_url,
            "source_tier": item.source_tier.value,
            "content_fingerprint": item.content_fingerprint,
            "title": item.title,
            "excerpt": item.excerpt,
            "excerpt_truncated": item.excerpt_truncated,
            "integrity_state": item.integrity_state.value,
            "lineage": lineage_by_item.get(item.item_id, []),
        }
        for item in result.items
    ]


def _event_payload(result: PragmaticIntelligenceResult) -> list[dict[str, object]]:
    values = []
    for event in result.events:
        payload = event.to_canonical_dict()
        payload["evidence_role"] = "reasoned_inference"
        payload["opposing_evidence_assessment"] = "not_systematically_detected"
        values.append(payload)
    return values


def _dimension_payload(result: PragmaticIntelligenceResult) -> list[dict[str, object]]:
    if result.report is None:
        return []
    summaries = {
        item.source_attempt_id: {
            "source_attempt_id": item.source_attempt_id,
            "source_id": item.source_id,
            "endpoint": item.endpoint,
            "data_as_of": (
                None if item.data_as_of is None else item.data_as_of.isoformat()
            ),
            "retrieved_at": (
                None if item.retrieved_at is None else item.retrieved_at.isoformat()
            ),
        }
        for item in result.source_summaries
    }
    values = []
    for observation in result.report.snapshot.market_state.dimensions:
        payload = observation.to_canonical_dict()
        payload["data_as_of"] = None
        payload["freshness"] = "unknown"
        payload["http_retrieved_at"] = observation.retrieved_at.isoformat()
        payload["data_as_of_semantics"] = "http_retrieval_not_market_session"
        payload["market_session_as_of"] = None
        payload["authenticated_sources"] = [
            summaries[attempt_id] for attempt_id in observation.source_attempt_ids
        ]
        values.append(payload)
    return values


def _shadow_payload(result: PragmaticIntelligenceResult) -> dict[str, object]:
    if result.report is None:
        return {
            "status": "insufficient_data",
            "market_state": "insufficient_data",
            "market_direction_status": "insufficient_data",
            "market_session_as_of": None,
            "sector_states": [],
            "invalidation_conditions": [],
            "action_authorized": False,
        }
    state = result.report.snapshot.market_state
    labels = {item.entity_id: item for item in result.sector_labels}
    return {
        "status": "experimental_shadow",
        "market_state": state.market_state.value,
        "market_direction_status": (
            "insufficient_data"
            if state.market_state.value == "insufficient_data"
            else "evidence_only"
        ),
        "market_session_as_of": None,
        "sector_states": [
            {
                "sector_id": sector_id,
                "sector_code": labels[sector_id].sector_code,
                "sector_name": labels[sector_id].sector_name,
                "state": sector_state.value,
                "freshness": "unknown",
                "basis": "http_retrieval_without_market_session_time",
            }
            for sector_id, sector_state in state.sector_states
        ],
        "supporting_observation_ids": list(state.supporting_observation_ids),
        "opposing_observation_ids": list(state.opposing_observation_ids),
        "unknown_dimensions": [item.value for item in state.unknown_dimensions],
        "invalidation_conditions": list(state.invalidation_conditions),
        "next_review_at": state.next_review_at.isoformat(),
        "action_authorized": False,
    }


def _fund_payload(result: PragmaticIntelligenceResult) -> dict[str, object]:
    context = result.fund_context
    if context is None:
        return {
            "subject_fund_code": result.subject.fund_code,
            "coverage_scope": None,
            "context": None,
            "companion_workflows_required": [
                "decision_route",
                "fund_brief",
                "portfolio",
            ]
            if result.subject.fund_code is not None
            else [],
            "links": [],
        }
    links = []
    if result.report is not None:
        link_ids = set(result.report.snapshot.fund_relevance_link_ids)
        for item in result.report.snapshot.event_entity_links:
            if item.link_id not in link_ids:
                continue
            payload = item.to_canonical_dict()
            payload["relationship_semantics"] = (
                "disclosed_context_text_match_not_current_exposure"
            )
            payload["holdings_period"] = (
                None
                if context.relevance_context.holdings_period is None
                else context.relevance_context.holdings_period.isoformat()
            )
            payload["holdings_freshness"] = context.holdings_freshness
            links.append(payload)
    relevance = context.relevance_context
    return {
        "subject_fund_code": relevance.fund_code,
        "coverage_scope": context.coverage_scope,
        "covered_fields": list(context.covered_fields),
        "not_covered_fields": list(context.not_covered_fields),
        "context": {
            "canonical_name": relevance.canonical_name,
            "benchmark_terms": list(relevance.benchmark_terms),
            "disclosed_security_names": list(relevance.disclosed_security_names),
            "holdings_period": (
                None
                if relevance.holdings_period is None
                else relevance.holdings_period.isoformat()
            ),
            "holdings_coverage": relevance.holdings_coverage,
            "holdings_section_state": context.holdings_section_state,
            "holdings_freshness": context.holdings_freshness,
            "holdings_published_at": (
                None
                if context.holdings_published_at is None
                else context.holdings_published_at.isoformat()
            ),
            "holdings_last_success_at": (
                None
                if context.holdings_last_success_at is None
                else context.holdings_last_success_at.isoformat()
            ),
            "holdings_retrieved_at": (
                None
                if context.holdings_retrieved_at is None
                else context.holdings_retrieved_at.isoformat()
            ),
            "source_boundary": context.source_boundary,
        },
        "companion_workflows_required": list(context.companion_workflows),
        "links": links,
    }


def _thesis_payload(result: PragmaticIntelligenceResult) -> object:
    review = result.thesis_review
    if review is None:
        return None
    return {
        "reason": review.reason,
        "horizon": review.horizon,
        "invalidation": review.invalidation,
        "evidence_check": review.evidence_check,
        "evidence_ids": list(review.evidence_ids),
        "semantic_review_required": True,
        "can_trigger_sale": False,
        "action_instruction": None,
    }


def public_intelligence_payload(
    result: PragmaticIntelligenceResult,
) -> Dict[str, object]:
    result.validate()
    report = result.report
    snapshot = None if report is None else report.snapshot
    request = {
        "request_id": result.terminal_request.request_id,
        "request_run_id": result.terminal_request.id,
        "mode": result.terminal_request.mode.value,
        "terminal_status": result.terminal_request.status.value,
        "started_at": result.terminal_request.started_at.isoformat(),
        "deadline_at": result.terminal_request.deadline_at.isoformat(),
        "finished_at": result.terminal_request.finished_at.isoformat(),
        "subject_scope": result.subject.subject_scope,
        "subject_fund_code": result.subject.fund_code,
        "workflow": result.subject.workflow.value,
        "interval": result.subject.interval.to_canonical_dict(),
        "omitted_work": list(result.terminal_request.omitted_work),
        "sources": _source_payload(result),
    }
    beginner = (
        {
            "evidence_boundary": "本次没有来源形成可发布快照，结论为证据不足。",
            "action_boundary": "证据不足不等于可以买、卖出或继续持有。",
        }
        if report is None
        else dict(report.beginner_explanation_zh)
    )
    missing = set(result.terminal_request.omitted_work)
    if snapshot is not None:
        missing.update(snapshot.missing_evidence)
    conflicts = (
        []
        if snapshot is None
        else [
            {
                "code": code,
                "scope": "bounded_authenticated_sources",
                "complete_cross_validation": False,
            }
            for code in snapshot.conflicts
        ]
    )
    return {
        "request": request,
        "items": _item_payload(result),
        "events": _event_payload(result),
        "dimensions": _dimension_payload(result),
        "experimental_shadow": _shadow_payload(result),
        "fund_relevance": _fund_payload(result),
        "thesis_review": _thesis_payload(result),
        "conflicts": conflicts,
        "cross_validation": {
            "scope": "bounded_authenticated_sources",
            "complete": False,
            "opposing_evidence_detection": "not_systematically_implemented",
            "empty_conflicts_means_no_conflict_detected_in_scope_only": True,
        },
        "missing_evidence": sorted(missing),
        "beginner_explanation_zh": beginner,
        "exact_amount_available": False,
        "action_maturity": "evidence_only",
        "action_authorized": False,
    }
