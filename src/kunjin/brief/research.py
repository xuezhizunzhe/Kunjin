from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Optional, Tuple

from kunjin.brief.d2 import D2RelationshipSet
from kunjin.brief.engine import HeldFundBriefEvaluation
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import (
    BriefFact,
    BriefSnapshot,
    BriefState,
    HeldFundBriefReport,
)
from kunjin.decision.models import (
    DecisionRoute,
    canonical_decimal,
    canonical_json_bytes,
    canonical_value,
)

_TOP_LEVEL_KEYS = (
    "request",
    "subject",
    "facts",
    "official_events",
    "portfolio_relationship",
    "sync_status",
    "decision_evidence_status",
    "action_interpretation",
    "missing_evidence",
    "beginner_explanation_zh",
)
_BEGINNER_KEYS = (
    "headline",
    "fund_identity",
    "portfolio_relationship",
    "recent_official_events",
    "why_this_state",
    "evidence_gaps",
    "change_conditions",
)
_GAP_FIELDS = (
    ("missing", "missing_fields"),
    ("stale", "stale_fields"),
    ("conflicted", "conflicted_fields"),
    ("unsupported", "unsupported_fields"),
    ("cooldown", "cooldown_fields"),
)


def _merge_facts(
    public_facts: Tuple[BriefFact, ...],
    relationship_facts: Tuple[BriefFact, ...],
) -> Tuple[BriefFact, ...]:
    merged: dict[str, BriefFact] = {}
    encoded: dict[str, bytes] = {}
    for fact in (*public_facts, *relationship_facts):
        if type(fact) is not BriefFact:
            raise ValueError("brief snapshot facts must be exact BriefFact records")
        fact.validate()
        payload = canonical_json_bytes(fact)
        if fact.fact_id in encoded and encoded[fact.fact_id] != payload:
            raise ValueError("brief snapshot facts contain a conflicting identifier")
        merged[fact.fact_id] = fact
        encoded[fact.fact_id] = payload
    return tuple(merged[key] for key in sorted(merged))


def _source_lineages(facts, events, resolution_lineages) -> Tuple[str, ...]:
    values = []
    for lineage_id in (
        *(fact.source_lineage_id for fact in facts),
        *(
            source_id
            for event in events
            for source_id in (event.original_source_id, event.quoted_source_id)
            if source_id is not None
        ),
        *resolution_lineages,
    ):
        if lineage_id not in values:
            values.append(lineage_id)
    return tuple(values)


def _evidence_fingerprint(
    *,
    facts,
    events,
    relationships,
    coverage,
    holdings_coverage,
    evaluation: HeldFundBriefEvaluation,
    d2: D2RelationshipSet,
    resolution_bindings,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "constraints": evaluation.constraints,
                "coverage": coverage,
                "decision_evidence_status": evaluation.decision_evidence_status,
                "events": events,
                "facts": facts,
                "holdings_coverage": holdings_coverage,
                "interpretations": evaluation.interpretations,
                "observation_version": d2.portfolio_provenance.observation_version,
                "observed_at": d2.observed_at,
                "portfolio_evidence_state": d2.portfolio_evidence_state,
                "position_present": d2.position_present,
                "relationships": relationships,
                "resolution_bindings": resolution_bindings,
                "sync_status": evaluation.sync_status,
            }
        )
    ).hexdigest()


def build_snapshot(
    *,
    request_run_id: int,
    decision_snapshot_id: int,
    route: DecisionRoute,
    fact_set: SourceLinkedFactSet,
    d2: D2RelationshipSet,
    evaluation: HeldFundBriefEvaluation,
) -> BriefSnapshot:
    if type(request_run_id) is not int or request_run_id <= 0:
        raise ValueError("request run id must be a positive integer")
    if type(decision_snapshot_id) is not int or decision_snapshot_id <= 0:
        raise ValueError("decision snapshot id must be a positive integer")
    if type(route) is not DecisionRoute:
        raise ValueError("brief snapshot route must be exact")
    if type(fact_set) is not SourceLinkedFactSet:
        raise ValueError("brief snapshot fact set must be exact")
    if type(d2) is not D2RelationshipSet:
        raise ValueError("brief snapshot D2 set must be exact")
    if type(evaluation) is not HeldFundBriefEvaluation:
        raise ValueError("brief snapshot evaluation must be exact")
    route.validate()
    fact_set.validate()
    d2.validate()
    evaluation.validate()
    provenance = d2.portfolio_provenance
    if (
        route.request_id != provenance.current_request_id
        or route.mode is not provenance.current_request_mode
        or fact_set.fund_code != d2.target_fund_code
        or tuple(item.action_id for item in evaluation.interpretations)
        != tuple(item.action_id for item in route.actions[1:])
    ):
        raise ValueError("brief snapshot inputs do not share one request and subject")

    facts = _merge_facts(fact_set.facts, d2.evidence_facts)
    events = tuple(
        sorted(fact_set.official_events, key=lambda item: (item.published_at, item.event_id))
    )
    relationships = tuple(sorted(d2.relationships, key=lambda item: item.relationship_id))
    action_order = {item.action_id: index for index, item in enumerate(route.actions)}
    if any(binding.action_id not in action_order for binding in evaluation.resolution_bindings):
        raise ValueError("brief resolution binding action is outside the request")
    resolution_bindings = tuple(
        sorted(
            evaluation.resolution_bindings,
            key=lambda item: (
                action_order[item.action_id],
                item.field_id,
                item.source_attempt_id,
            ),
        )
    )
    resolution_lineages = tuple(
        dict.fromkeys(binding.lineage_id for binding in resolution_bindings)
    )
    missing_fields = tuple(
        sorted(
            {
                *evaluation.missing_fields,
                *d2.coverage.unknown_fields,
                *d2.holdings_coverage.unknown_fields,
                *evaluation.sync_status.missing_fields,
                *evaluation.decision_evidence_status.missing_fields,
                *(
                    field_id
                    for interpretation in evaluation.interpretations
                    for field_id in interpretation.missing_fields
                ),
            }
        )
    )
    snapshot = BriefSnapshot(
        request_run_id=request_run_id,
        decision_snapshot_id=decision_snapshot_id,
        fund_code=fact_set.fund_code,
        action_ids=tuple(item.action_id for item in route.actions),
        mode=route.mode,
        facts=facts,
        official_events=events,
        relationships=relationships,
        coverage=d2.coverage,
        holdings_coverage=d2.holdings_coverage,
        sync_status=evaluation.sync_status,
        decision_evidence_status=evaluation.decision_evidence_status,
        interpretations=evaluation.interpretations,
        primary_state=evaluation.primary_state,
        action_maturity=evaluation.action_maturity,
        constraints=evaluation.constraints,
        triggered_reviews=evaluation.triggered_reviews,
        affected_action_abstentions=evaluation.affected_action_abstentions,
        blocking_codes=evaluation.blocking_codes,
        evidence_state=evaluation.decision_evidence_status.state,
        missing_fields=missing_fields,
        conflicts=evaluation.conflicts,
        source_lineage_ids=_source_lineages(facts, events, resolution_lineages),
        evidence_fingerprint=_evidence_fingerprint(
            facts=facts,
            events=events,
            relationships=relationships,
            coverage=d2.coverage,
            holdings_coverage=d2.holdings_coverage,
            evaluation=evaluation,
            d2=d2,
            resolution_bindings=resolution_bindings,
        ),
        created_at=provenance.as_of,
        portfolio_evidence_state=d2.portfolio_evidence_state,
        position_present=d2.position_present,
        observation_version=provenance.observation_version,
        observed_at=d2.observed_at,
        resolution_lineage_ids=resolution_lineages,
        resolution_bindings=resolution_bindings,
    )
    snapshot.validate()
    return snapshot


def build_owner_report(
    snapshot: BriefSnapshot,
    portfolio_weight: Optional[str],
) -> HeldFundBriefReport:
    if type(snapshot) is not BriefSnapshot:
        raise ValueError("owner report snapshot must be exact")
    snapshot.validate()
    position_present = snapshot.position_present
    if position_present is None:
        if portfolio_weight is not None:
            raise ValueError("unknown position cannot claim a portfolio weight")
        normalized_weight = None
    elif position_present is False:
        if portfolio_weight not in {None, "0"}:
            raise ValueError("absent position cannot claim a nonzero weight")
        normalized_weight = "0"
    else:
        normalized_weight = portfolio_weight
        if normalized_weight is not None:
            if type(normalized_weight) is not str:
                raise ValueError("portfolio weight must be a canonical string or None")
            try:
                value = Decimal(normalized_weight)
            except Exception:
                raise ValueError("portfolio weight must be canonical") from None
            if canonical_decimal(value) != normalized_weight or not (
                Decimal("0") <= value <= Decimal("1")
            ):
                raise ValueError("portfolio weight must be canonical and in [0, 1]")
    report = HeldFundBriefReport(
        snapshot=snapshot,
        owner_overlay={
            "observation_version": snapshot.observation_version,
            "observed_at": snapshot.observed_at,
            "portfolio_weight": normalized_weight,
            "position_present": position_present,
        },
    )
    report.validate()
    return report


def _affected_actions(snapshot: BriefSnapshot, field_id: str, fallback) -> Tuple[str, ...]:
    direct = tuple(
        item.action_id for item in snapshot.interpretations if field_id in item.missing_fields
    )
    return direct or fallback


def _missing_evidence(snapshot: BriefSnapshot) -> list[dict[str, object]]:
    items = []
    seen_fields = set()
    for scope, status, use_fallback in (
        ("decision_evidence_status", snapshot.decision_evidence_status, True),
        ("sync_status", snapshot.sync_status, False),
    ):
        for condition, attribute in _GAP_FIELDS:
            for field_id in getattr(status, attribute):
                seen_fields.add(field_id)
                fallback = status.unsupported_interpretations if use_fallback else ()
                items.append(
                    {
                        "affected_action_ids": list(
                            _affected_actions(snapshot, field_id, fallback)
                        ),
                        "condition": condition,
                        "field_id": field_id,
                        "scope": scope,
                    }
                )
    for scope, coverage in (
        ("minimum_relationship_coverage", snapshot.coverage),
        ("disclosed_holdings_coverage", snapshot.holdings_coverage),
    ):
        for field_id in coverage.unknown_fields:
            seen_fields.add(field_id)
            items.append(
                {
                    "affected_action_ids": list(_affected_actions(snapshot, field_id, ())),
                    "condition": "missing",
                    "field_id": field_id,
                    "scope": scope,
                }
            )
    for field_id in snapshot.missing_fields:
        if field_id in seen_fields:
            continue
        items.append(
            {
                "affected_action_ids": list(_affected_actions(snapshot, field_id, ())),
                "condition": "missing",
                "field_id": field_id,
                "scope": "snapshot",
            }
        )
    return items


def _state_text(state: BriefState) -> str:
    return {
        BriefState.NO_ADD: (
            "当前仅支持暂不新增风险（no_add）。这是财务安全闸门限制，不代表应持有或卖出。"
        ),
        BriefState.HOLD: (
            "本次已核验信息未触发已确认的持有理由失效条件（hold）；这是实验性观察，不是确定持有建议。"
        ),
        BriefState.WATCH: (
            "本次规则结果为继续观察（watch）；现有证据不足以形成确定的持有、减仓或退出结论。"
        ),
        BriefState.REDUCE_OR_EXIT_REVIEW: (
            "已触发减仓或退出复核（reduce_or_exit_review）；这不是立即赎回指令。"
        ),
        BriefState.ABSTAIN: (
            "本次暂不形成行动倾向（abstain）；请先处理列示的证据缺口、冲突或交易限制。"
        ),
    }[state]


def _maturity_text(mode: str) -> str:
    if mode == "mature":
        return "mature 仅表示规则可稳定复现，不表示基金判断确定或交易已获授权。"
    return "experimental_shadow 是实验性影子状态，仅供观察，不授权交易。"


def _beginner_explanation(snapshot: BriefSnapshot, missing_evidence) -> dict[str, object]:
    headline_items = []
    for interpretation in snapshot.interpretations:
        text = _state_text(interpretation.state)
        if interpretation.action_id == "switch_reduce":
            text = f"转出腿：{text}"
        elif interpretation.action_id == "switch_buy":
            text = f"转入腿：{text} 不得从转出腿继承许可。"
        headline_items.append(
            {
                "action_id": interpretation.action_id,
                "action_maturity": interpretation.action_maturity.value,
                "state": interpretation.state.value,
                "text": text,
            }
        )
    headline_text = _state_text(snapshot.primary_state)
    if snapshot.triggered_reviews:
        headline_text += " 同时存在正式公告触发的退出复核，但不等于立即卖出。"
    identity_facts = tuple(
        fact
        for fact in snapshot.facts
        if fact.field_id in {"identity_active_status", "share_class_identity"}
    )
    identity_ids = [fact.fact_id for fact in identity_facts]
    identity_dates = sorted(
        {
            canonical_value(value)
            for fact in identity_facts
            for value in (fact.data_as_of, fact.published_at)
            if value is not None
        }
    )
    active_event_ids = [
        event.event_id for event in snapshot.official_events if event.integrity_status == "active"
    ]
    supporting_ids = list(
        dict.fromkeys(
            evidence_id
            for item in snapshot.interpretations
            for evidence_id in item.supporting_evidence_ids
        )
    )
    opposing_ids = list(
        dict.fromkeys(
            evidence_id
            for item in snapshot.interpretations
            for evidence_id in item.opposing_evidence_ids
        )
    )
    gap_text = (
        "以下缺口会限制对应动作结论。"
        if missing_evidence
        else "本次规则字段未记录缺口，但不表示全部风险已覆盖。"
    )
    event_text = (
        "已列出本次范围内仍为 active 的正式公告。"
        if active_event_ids
        else "本次没有可展示的 active 正式公告；这不自动证明不存在相关事件。"
    )
    result = {
        "headline": {
            "action_maturity": snapshot.action_maturity.value,
            "items": headline_items,
            "maturity_text": _maturity_text(snapshot.action_maturity.value),
            "primary_state": snapshot.primary_state.value,
            "text": headline_text,
        },
        "fund_identity": {
            "data_dates": identity_dates,
            "evidence_ids": identity_ids,
            "text": "基金身份、份额类别、日期和来源等级以所列结构化证据为准。",
        },
        "portfolio_relationship": {
            "coverage_ids": [
                snapshot.coverage.coverage_id,
                snapshot.holdings_coverage.coverage_id,
            ],
            "relationship_ids": [item.relationship_id for item in snapshot.relationships],
            "unknown_fields": {
                "disclosed_holdings_coverage": list(snapshot.holdings_coverage.unknown_fields),
                "minimum_relationship_coverage": list(snapshot.coverage.unknown_fields),
            },
            "text": ("这里只展示已验证的组合关系与覆盖缺口；未知持仓不按零重叠处理。"),
        },
        "recent_official_events": {
            "event_ids": active_event_ids,
            "text": event_text,
        },
        "why_this_state": {
            "blocking_codes": list(snapshot.blocking_codes),
            "opposing_evidence_ids": opposing_ids,
            "supporting_evidence_ids": supporting_ids,
            "text": ("支持证据、反方证据和阻断码分别列示；没有反方记录不等于没有风险。"),
        },
        "evidence_gaps": {
            "items": missing_evidence,
            "text": gap_text,
        },
        "change_conditions": {
            "items": [
                {
                    "action_id": item.action_id,
                    "blocking_codes": list(item.blocking_codes),
                    "evidence_change_conditions": list(item.missing_fields),
                    "invalidation_conditions": list(item.invalidation_conditions),
                    "unavailable_actions": list(item.unavailable_actions),
                }
                for item in snapshot.interpretations
            ],
            "text": ("证据补齐、阻断解除或失效条件触发时需要重新评估；不会自动执行交易。"),
        },
    }
    if tuple(result) != _BEGINNER_KEYS:
        raise ValueError("beginner explanation schema drifted")
    return result


def public_payload(report: HeldFundBriefReport) -> dict[str, object]:
    """Return the strict owner-local payload; this is not a public audit projection."""

    if type(report) is not HeldFundBriefReport:
        raise ValueError("public payload requires an exact HeldFundBriefReport")
    report.validate()
    snapshot = report.snapshot
    overlay = report.owner_overlay
    if overlay is None:
        raise ValueError("owner-local payload requires an owner overlay")
    missing = _missing_evidence(snapshot)
    payload = {
        "request": {
            "action_ids": list(snapshot.action_ids),
            "created_at": canonical_value(snapshot.created_at),
            "decision_snapshot_id": snapshot.decision_snapshot_id,
            "evidence_fingerprint": snapshot.evidence_fingerprint,
            "mode": snapshot.mode.value,
            "request_run_id": snapshot.request_run_id,
            "result_checksum": snapshot.checksum(),
        },
        "subject": {
            "fund_code": snapshot.fund_code,
            "observation_version": overlay["observation_version"],
            "observed_at": (
                None if overlay["observed_at"] is None else canonical_value(overlay["observed_at"])
            ),
            "portfolio_evidence_state": snapshot.portfolio_evidence_state,
            "portfolio_weight": overlay["portfolio_weight"],
            "position_present": overlay["position_present"],
        },
        "facts": [item.to_canonical_dict() for item in snapshot.facts],
        "official_events": [item.to_canonical_dict() for item in snapshot.official_events],
        "portfolio_relationship": {
            "disclosed_holdings_coverage": snapshot.holdings_coverage.to_canonical_dict(),
            "minimum_relationship_coverage": snapshot.coverage.to_canonical_dict(),
            "relationships": [item.to_canonical_dict() for item in snapshot.relationships],
        },
        "sync_status": snapshot.sync_status.to_canonical_dict(),
        "decision_evidence_status": snapshot.decision_evidence_status.to_canonical_dict(),
        "action_interpretation": {
            "action_maturity": snapshot.action_maturity.value,
            "affected_action_abstentions": list(snapshot.affected_action_abstentions),
            "blocking_codes": list(snapshot.blocking_codes),
            "conflicts": list(snapshot.conflicts),
            "constraints": list(snapshot.constraints),
            "interpretations": [item.to_canonical_dict() for item in snapshot.interpretations],
            "primary_state": snapshot.primary_state.value,
            "triggered_reviews": list(snapshot.triggered_reviews),
        },
        "missing_evidence": missing,
        "beginner_explanation_zh": _beginner_explanation(snapshot, missing),
    }
    if tuple(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("owner payload schema drifted")
    return payload
