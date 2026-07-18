from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from typing import Tuple

from kunjin.brief.d2 import D2RelationshipSet
from kunjin.brief.engine import HeldFundBriefEvaluation
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefCoverage,
    BriefEvidenceStatus,
    BriefFact,
    BriefSnapshot,
    BriefState,
    HeldFundBriefOutcome,
    HeldFundBriefReport,
    OfficialEventCode,
    RelationshipEvidence,
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
_OUTCOME_REQUEST_KEYS = (
    "action_ids",
    "created_at",
    "decision_snapshot_id",
    "evidence_fingerprint",
    "mode",
    "request_run_id",
    "result_checksum",
    "terminal_status",
    "omitted_work",
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
_MAX_BEGINNER_EXPLANATION_BYTES = 64 * 1024


def _sorted_unique(values) -> Tuple[str, ...]:
    return tuple(sorted(set(values)))


def _action_sorted(values, action_order) -> Tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda item: (action_order.get(item, 999), item)))


def _canonical_fact(fact: BriefFact) -> BriefFact:
    return replace(fact, conflict_ids=_sorted_unique(fact.conflict_ids))


def _canonical_relationship(item: RelationshipEvidence) -> RelationshipEvidence:
    return replace(
        item,
        evidence_ids=_sorted_unique(item.evidence_ids),
        warnings=tuple(sorted(set(item.warnings))),
    )


def _canonical_coverage(coverage: BriefCoverage) -> BriefCoverage:
    return replace(
        coverage,
        included_fund_codes=tuple(sorted(coverage.included_fund_codes)),
        omitted_fund_codes=tuple(sorted(coverage.omitted_fund_codes)),
        unknown_fields=_sorted_unique(coverage.unknown_fields),
        evidence_ids=_sorted_unique(coverage.evidence_ids),
    )


def _canonical_status(status: BriefEvidenceStatus, action_order) -> BriefEvidenceStatus:
    return replace(
        status,
        required_fields=_sorted_unique(status.required_fields),
        obtained_fields=_sorted_unique(status.obtained_fields),
        missing_fields=_sorted_unique(status.missing_fields),
        stale_fields=_sorted_unique(status.stale_fields),
        conflicted_fields=_sorted_unique(status.conflicted_fields),
        unsupported_fields=_sorted_unique(status.unsupported_fields),
        cooldown_fields=_sorted_unique(status.cooldown_fields),
        supported_interpretations=_action_sorted(
            status.supported_interpretations,
            action_order,
        ),
        unsupported_interpretations=_action_sorted(
            status.unsupported_interpretations,
            action_order,
        ),
        acceptable_alternative_ids=_sorted_unique(status.acceptable_alternative_ids),
        manual_supplementation_codes=_sorted_unique(status.manual_supplementation_codes),
    )


def _canonical_interpretation(
    item: BriefActionInterpretation,
) -> BriefActionInterpretation:
    return replace(
        item,
        supporting_evidence_ids=_sorted_unique(item.supporting_evidence_ids),
        opposing_evidence_ids=_sorted_unique(item.opposing_evidence_ids),
        blocking_codes=_sorted_unique(item.blocking_codes),
        missing_fields=_sorted_unique(item.missing_fields),
        invalidation_conditions=tuple(sorted(set(item.invalidation_conditions))),
        unavailable_actions=_sorted_unique(item.unavailable_actions),
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
        fact = _canonical_fact(fact)
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
    d2: D2RelationshipSet,
    resolution_bindings,
    conflicts,
    interpretations,
    fact_missing_fields,
    fact_warnings,
    d2_missing_fields,
    d2_warnings,
) -> str:
    thesis_bindings = []
    for interpretation in interpretations:
        state_inputs = interpretation.state_inputs
        if state_inputs.get("thesis_fingerprint") is None:
            continue
        thesis_bindings.append(
            {
                "action_id": interpretation.action_id,
                "thesis_fingerprint": state_inputs.get("thesis_fingerprint"),
                "thesis_record_id": state_inputs.get("thesis_record_id"),
                "thesis_review_source_lineage_id": state_inputs.get(
                    "thesis_review_source_lineage_id"
                ),
                "thesis_review_state": state_inputs.get("thesis_review_state"),
                "thesis_reviewed_at": state_inputs.get("thesis_reviewed_at"),
            }
        )
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "conflicts": conflicts,
                "coverage": coverage,
                "d2_missing_fields": d2_missing_fields,
                "d2_warnings": d2_warnings,
                "events": events,
                "fact_missing_fields": fact_missing_fields,
                "fact_warnings": fact_warnings,
                "facts": facts,
                "holdings_coverage": holdings_coverage,
                "observation_version": d2.portfolio_provenance.observation_version,
                "observed_at": d2.observed_at,
                "portfolio_evidence_state": d2.portfolio_evidence_state,
                "position_present": d2.position_present,
                "relationships": relationships,
                "resolution_bindings": resolution_bindings,
                "thesis_bindings": thesis_bindings,
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
    relationships = tuple(
        sorted(
            (_canonical_relationship(item) for item in d2.relationships),
            key=lambda item: item.relationship_id,
        )
    )
    snapshot_observed_at = None if d2.portfolio_evidence_state == "unknown" else d2.observed_at
    action_order = {item.action_id: index for index, item in enumerate(route.actions)}
    coverage = _canonical_coverage(d2.coverage)
    holdings_coverage = _canonical_coverage(d2.holdings_coverage)
    sync_status = _canonical_status(evaluation.sync_status, action_order)
    decision_status = _canonical_status(
        evaluation.decision_evidence_status,
        action_order,
    )
    interpretations = tuple(_canonical_interpretation(item) for item in evaluation.interpretations)
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
                *coverage.unknown_fields,
                *holdings_coverage.unknown_fields,
                *sync_status.missing_fields,
                *decision_status.missing_fields,
                *(
                    field_id
                    for interpretation in interpretations
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
        coverage=coverage,
        holdings_coverage=holdings_coverage,
        sync_status=sync_status,
        decision_evidence_status=decision_status,
        interpretations=interpretations,
        primary_state=evaluation.primary_state,
        action_maturity=evaluation.action_maturity,
        constraints=_sorted_unique(evaluation.constraints),
        triggered_reviews=evaluation.triggered_reviews,
        affected_action_abstentions=_action_sorted(
            evaluation.affected_action_abstentions,
            action_order,
        ),
        blocking_codes=_sorted_unique(evaluation.blocking_codes),
        evidence_state=decision_status.state,
        missing_fields=missing_fields,
        conflicts=_sorted_unique(evaluation.conflicts),
        source_lineage_ids=_source_lineages(facts, events, resolution_lineages),
        evidence_fingerprint=_evidence_fingerprint(
            facts=facts,
            events=events,
            relationships=relationships,
            coverage=coverage,
            holdings_coverage=holdings_coverage,
            d2=d2,
            resolution_bindings=resolution_bindings,
            conflicts=_sorted_unique(evaluation.conflicts),
            interpretations=interpretations,
            fact_missing_fields=_sorted_unique(fact_set.missing_fields),
            fact_warnings=_sorted_unique(fact_set.warnings),
            d2_missing_fields=_sorted_unique(d2.missing_fields),
            d2_warnings=_sorted_unique(d2.warnings),
        ),
        created_at=provenance.as_of,
        portfolio_evidence_state=d2.portfolio_evidence_state,
        position_present=d2.position_present,
        observation_version=provenance.observation_version,
        observed_at=snapshot_observed_at,
        resolution_lineage_ids=resolution_lineages,
        resolution_bindings=resolution_bindings,
    )
    snapshot.validate()
    return snapshot


def build_owner_report(
    snapshot: BriefSnapshot,
    d2: D2RelationshipSet,
) -> HeldFundBriefReport:
    if type(snapshot) is not BriefSnapshot:
        raise ValueError("owner report snapshot must be exact")
    if type(d2) is not D2RelationshipSet:
        raise ValueError("owner report D2 evidence must be exact")
    snapshot.validate()
    d2.validate()
    provenance = d2.portfolio_provenance
    expected_observed_at = None if d2.portfolio_evidence_state == "unknown" else d2.observed_at
    if (
        snapshot.fund_code != d2.target_fund_code
        or snapshot.portfolio_evidence_state != d2.portfolio_evidence_state
        or snapshot.position_present is not d2.position_present
        or snapshot.observation_version != provenance.observation_version
        or snapshot.observed_at != expected_observed_at
        or snapshot.coverage != _canonical_coverage(d2.coverage)
        or snapshot.holdings_coverage != _canonical_coverage(d2.holdings_coverage)
        or snapshot.relationships
        != tuple(
            sorted(
                (_canonical_relationship(item) for item in d2.relationships),
                key=lambda item: item.relationship_id,
            )
        )
    ):
        raise ValueError("owner report D2 evidence does not match the snapshot")
    position_present = snapshot.position_present
    if position_present is None:
        normalized_weight = None
    elif position_present is False:
        normalized_weight = "0"
    else:
        normalized_weight = d2.target_portfolio_weight
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
        d2_fallback = _affected_actions(
            snapshot,
            "d2",
            (),
        )
        for field_id in coverage.unknown_fields:
            seen_fields.add(field_id)
            items.append(
                {
                    "affected_action_ids": list(_affected_actions(snapshot, field_id, d2_fallback)),
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


def _state_text(state: BriefState, *, hard_event: bool = False) -> str:
    if state is BriefState.REDUCE_OR_EXIT_REVIEW:
        if hard_event:
            return (
                "active 清盘或终止正式公告触发减仓或退出复核"
                "（reduce_or_exit_review）；这不是立即赎回指令。"
            )
        return (
            "本次规则结果进入减仓或退出复核流程（reduce_or_exit_review）；"
            "不表示系统发现了确定卖出信号，也不是立即赎回指令。"
        )
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
        hard_event = any(
            event.integrity_status == "active"
            and event.event_code
            in {
                OfficialEventCode.FUND_LIQUIDATION_NOTICE,
                OfficialEventCode.FUND_TERMINATION_NOTICE,
            }
            and interpretation.action_id in event.affected_action_ids
            for event in snapshot.official_events
        )
        text = _state_text(interpretation.state, hard_event=hard_event)
        if OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE.value in (interpretation.blocking_codes):
            text += (
                " 因当前存在赎回限制，当前不能形成可执行赎回安排；"
                "这不表示永久无法赎回，需以限制解除后的正式信息重新评估。"
            )
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
    primary_hard_event = any(
        item.state is snapshot.primary_state
        and any(
            event.integrity_status == "active"
            and event.event_code
            in {
                OfficialEventCode.FUND_LIQUIDATION_NOTICE,
                OfficialEventCode.FUND_TERMINATION_NOTICE,
            }
            and item.action_id in event.affected_action_ids
            for event in snapshot.official_events
        )
        for item in snapshot.interpretations
    )
    headline_text = _state_text(snapshot.primary_state, hard_event=primary_hard_event)
    if snapshot.triggered_reviews:
        headline_text += " 同时存在正式公告触发的退出复核，但不等于立即卖出。"
    restricted_actions = tuple(
        item.action_id
        for item in snapshot.interpretations
        if OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE.value in item.blocking_codes
    )
    if len(snapshot.interpretations) == 1 and restricted_actions:
        headline_text += " 因当前存在赎回限制，当前不能形成可执行赎回安排；这不表示永久无法赎回。"
    elif restricted_actions:
        headline_text += " 一个或多个动作当前存在执行限制，必须查看分腿结论。"
    is_switch = snapshot.action_ids == ("fact_research", "switch_reduce", "switch_buy")
    maturity_scope = "primary_state_only" if is_switch else "all_actions"
    maturity_text = _maturity_text(snapshot.action_maturity.value)
    if is_switch:
        maturity_text += (
            " 顶层成熟度仅描述主状态；转入腿仍以自己的 experimental_shadow 和 abstain 为准，"
            "不得继承转出腿许可。"
        )
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
    inactive_event_items = [
        {
            "event_code": event.event_code.value,
            "event_id": event.event_id,
            "integrity_status": event.integrity_status,
        }
        for event in snapshot.official_events
        if event.integrity_status != "active"
    ]
    gap_text = (
        "以下缺口会限制对应动作结论。"
        if missing_evidence
        else "本次规则字段未记录缺口，但不表示全部风险已覆盖。"
    )
    if active_event_ids and inactive_event_items:
        event_text = (
            "已列出仍为 active 的正式公告；corrected 或 retracted 公告仅作反方证据，"
            "不作为当前行动依据。"
        )
    elif active_event_ids:
        event_text = "已列出本次范围内仍为 active 的正式公告。"
    elif inactive_event_items:
        event_text = (
            "本次没有可展示的 active 正式公告；corrected 或 retracted 公告仅作反方证据，"
            "不作为当前行动依据。"
        )
    else:
        event_text = "本次没有可展示的 active 正式公告；这不自动证明不存在相关事件。"
    result = {
        "headline": {
            "action_maturity": snapshot.action_maturity.value,
            "items": headline_items,
            "maturity_scope": maturity_scope,
            "maturity_text": maturity_text,
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
            "inactive_items": inactive_event_items,
            "text": event_text,
        },
        "why_this_state": {
            "items": [
                {
                    "action_id": item.action_id,
                    "action_maturity": item.action_maturity.value,
                    "blocking_codes": list(item.blocking_codes),
                    "opposing_evidence_ids": list(item.opposing_evidence_ids),
                    "supporting_evidence_ids": list(item.supporting_evidence_ids),
                }
                for item in snapshot.interpretations
            ],
            "text": (
                "每个动作的支持证据、反方证据和阻断码分别列示；"
                "一个动作的证据不能授权另一个动作，没有反方记录也不等于没有风险。"
            ),
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
    if len(canonical_json_bytes(result)) > _MAX_BEGINNER_EXPLANATION_BYTES:
        raise ValueError("beginner explanation exceeds its bounded output size")
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


def public_outcome_payload(outcome: HeldFundBriefOutcome) -> dict[str, object]:
    """Project an authenticated terminal outcome into the owner-local payload."""

    if type(outcome) is not HeldFundBriefOutcome:
        raise ValueError("public outcome payload requires an exact HeldFundBriefOutcome")
    outcome.validate()
    payload = public_payload(outcome.report)
    payload["request"] = {
        **payload["request"],
        "terminal_status": outcome.terminal_status.value,
        "omitted_work": list(outcome.omitted_work),
    }
    if tuple(payload["request"]) != _OUTCOME_REQUEST_KEYS:
        raise ValueError("owner outcome request schema drifted")
    if tuple(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("owner outcome payload schema drifted")
    return payload
