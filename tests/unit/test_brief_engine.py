from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from kunjin.brief.d2 import PortfolioEvidenceBinding, build_d2_relationships
from kunjin.brief.engine import (
    BriefSourceResolution,
    ConfirmedThesisState,
    EvidenceStatus,
    HeldFundBriefEngine,
    HeldFundBriefEvaluation,
    ThesisReviewState,
    load_brief_source_resolution,
    load_confirmed_thesis_state,
)
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefEvidenceState,
    BriefFact,
    BriefState,
    OfficialEvent,
    OfficialEventCode,
    canonical_event_affected_actions,
)
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionState,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestFieldResolution,
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldState,
    SourceTier,
    canonical_json_bytes,
)
from kunjin.decision.routing import ActionRouter
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.models import InvestmentThesis, StoredPosition
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
REQUEST_ID = "1234567890abcdef1234567890abcdef"
FUND_CODE = "519755"


def _status(*, blocked: bool = False) -> dict[str, object]:
    return {
        "state": "fresh",
        "freshness": "fresh",
        "assessment_id": 7,
        "profile_version_id": 3,
        "policy_version": "1",
        "status": "blocked" if blocked else "ready_for_allocation",
        "hard_blocks": ["emergency_reserve_shortfall"] if blocked else [],
        "constraints": ["near_term_obligation_gap"] if blocked else [],
        "assessed_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(days=1)).isoformat(),
        "capability": "research_only",
    }


def _route(action: ActionKind, *, blocked: bool = False):
    return ActionRouter().route(
        request_id=REQUEST_ID,
        mode=RequestMode.RAPID,
        actions=(ActionKind.FACT_RESEARCH, action),
        suitability_status=_status(blocked=blocked),
    )


def _fact(
    field_id: str,
    value: object,
    *,
    fact_id: str | None = None,
    conflict_ids: tuple[str, ...] = (),
) -> BriefFact:
    return BriefFact(
        fact_id=field_id if fact_id is None else fact_id,
        field_id=field_id,
        value=value,
        unit=None,
        data_as_of=NOW - timedelta(days=1),
        published_at=NOW - timedelta(days=1),
        retrieved_at=NOW,
        source_id="eastmoney_f10",
        source_tier=SourceTier.TIER_2,
        publisher="东方财富",
        canonical_url=f"https://fundf10.eastmoney.com/{field_id}/{FUND_CODE}",
        freshness=EvidenceFreshness.CURRENT,
        completeness=EvidenceCompleteness.COMPLETE,
        conflict_ids=conflict_ids,
        calculated=False,
        source_lineage_id=f"document_{field_id}",
    )


def _base_facts() -> tuple[BriefFact, ...]:
    return (
        _fact(
            "identity_active_status",
            {"fund_code": FUND_CODE, "fund_company": "测试基金公司"},
        ),
        _fact(
            "current_manager_team",
            {"manager_name": "测试经理", "tenure_start": "2024-01-01"},
        ),
        _fact(
            "current_benchmark",
            {
                "description": "测试指数收益率",
                "effective_from": "2024-01-01",
                "effective_to": None,
            },
        ),
        _fact(
            "share_class_identity",
            {
                "related_fund_code": FUND_CODE,
                "share_class": "A",
                "fund_name": "测试基金A",
            },
        ),
        _fact(
            "formal_nav",
            {"fund_code": FUND_CODE, "nav": "1.2345", "nav_date": "2026-07-16"},
        ),
        _fact(
            "fees_share_class_relationship",
            {"share_class": "A", "fee_state": "published_conditions_available"},
        ),
    )


def _event(
    code: OfficialEventCode,
    action_id: str,
    *,
    event_id: str | None = None,
    integrity_status: str = "active",
) -> OfficialEvent:
    event_key = code.value if event_id is None else event_id
    action_shape = (
        ("fact_research", "switch_reduce", "switch_buy")
        if action_id.startswith("switch_")
        else ("fact_research", action_id)
    )
    return OfficialEvent(
        event_id=event_key,
        event_code=code,
        title=f"测试{code.value}",
        summary="经认证的管理人正式公告。",
        publisher="测试基金公司",
        canonical_url=f"https://www.fund.example/{event_key}.pdf",
        published_at=NOW - timedelta(hours=2),
        retrieved_at=NOW - timedelta(hours=1),
        source_tier=SourceTier.TIER_1,
        original_source_id=f"official_document_{event_key}",
        quoted_source_id=None,
        content_fingerprint=("1" if event_id is None else "2") * 64,
        integrity_status=integrity_status,
        affected_action_ids=canonical_event_affected_actions(code, action_shape),
    )


def _fact_set(
    *,
    events: tuple[OfficialEvent, ...] = (),
    missing_fields: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    extra_facts: tuple[BriefFact, ...] = (),
    identity_conflict: bool = False,
) -> SourceLinkedFactSet:
    facts = _base_facts()
    if identity_conflict:
        facts = (
            replace(facts[0], conflict_ids=("identity_value_conflict",)),
            *facts[1:],
        )
        conflicts = (*conflicts, "identity_value_conflict")
    result = SourceLinkedFactSet(
        fund_code=FUND_CODE,
        facts=(*facts, *extra_facts),
        official_events=events,
        missing_fields=missing_fields,
        conflicts=conflicts,
        warnings=(),
    )
    result.validate()
    return result


def _d2(fact_set: SourceLinkedFactSet):
    position = StoredPosition(
        account_title="synthetic-account",
        fund_code=FUND_CODE,
        fund_name="测试基金A",
        shares=Decimal("1"),
        observed_at=NOW,
        share_class="A",
        formal_nav=Decimal("1.2345"),
        estimated_nav=None,
        observed_profit=None,
    )
    binding = PortfolioEvidenceBinding(
        positions=(position,),
        snapshot_complete=True,
        observation_version="synthetic_portfolio_v1",
        observed_at=NOW,
        source_state="same_request_success",
        request_id=REQUEST_ID,
        request_mode=RequestMode.RAPID,
        request_started_at=NOW - timedelta(seconds=30),
        request_deadline_at=NOW + timedelta(seconds=60),
    )
    return build_d2_relationships(
        FUND_CODE,
        binding,
        {FUND_CODE: fact_set},
        NOW,
        request_id=REQUEST_ID,
        request_mode=RequestMode.RAPID,
    )


def _official_resolution(
    action_id: str,
    *,
    field_id: str = "official_events",
    evidence_ids: tuple[str, ...] = (),
    outcome: SourceAttemptOutcome = SourceAttemptOutcome.SUCCESS,
    data_as_of: datetime | None = None,
    manual_supplement_ready: bool = True,
    acceptable_alternative_ids: tuple[str, ...] = (),
) -> BriefSourceResolution:
    with TemporaryDirectory() as directory:
        successful = {
            SourceAttemptOutcome.SUCCESS,
            SourceAttemptOutcome.CACHE_HIT,
        }
        repository = Repository(Path(directory) / "resolution.db")
        repository.migrate()
        store = DecisionAuditStore(repository)
        budget = RequestBudget.create(
            RequestMode.RAPID,
            request_id=REQUEST_ID,
            monotonic=lambda: 10.0,
            wall_clock=lambda: NOW - timedelta(seconds=10),
        )
        request_run_id = store.begin_request(budget)
        registry = SourceRegistryV1()
        attempt_id = store.record_source_attempt(
            request_run_id,
            SourceAttempt(
                source_id="fund_manager_official_documents",
                field_id=(
                    "fund_manager_product_announcement"
                    if field_id == "official_events"
                    else field_id
                ),
                subject_key=f"fund:{FUND_CODE}",
                attempt_number=1,
                outcome=outcome,
                started_at=NOW - timedelta(seconds=5),
                finished_at=NOW - timedelta(seconds=4),
                data_as_of=(
                    (NOW - timedelta(seconds=5) if data_as_of is None else data_as_of)
                    if outcome in successful
                    else None
                ),
                error_code=(
                    None
                    if outcome in successful
                    else SourceErrorCode.SOURCE_UNAVAILABLE
                ),
                cooldown_until=None,
                force_actor=None,
                force_reason=None,
                registry_version=registry.version,
                registry_checksum=registry.checksum(),
                response_bytes=100 if outcome in successful else 0,
            ),
        )
        return load_brief_source_resolution(
            store,
            attempt_id,
            action_id=action_id,
            field_id=field_id,
            evidence_ids=evidence_ids,
            manual_supplement_ready=manual_supplement_ready,
            acceptable_alternative_ids=acceptable_alternative_ids,
        )


def _thesis(
    tmp_path,
    *,
    review_resolution: BriefSourceResolution,
    review_state: ThesisReviewState = ThesisReviewState.INTACT,
    evidence_ids: tuple[str, ...] = (),
) -> ConfirmedThesisState:
    repository = Repository(tmp_path / f"thesis-{review_state.value}.db")
    repository.migrate()
    created_at = (
        review_resolution.evaluated_at + timedelta(seconds=1)
        if review_state is ThesisReviewState.UNKNOWN
        else review_resolution.evaluated_at - timedelta(seconds=1)
    )
    thesis_id = repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="基金角色与已确认的持有目的相符",
            horizon="长期观察",
            invalidation="基金角色或核心管理发生实质变化",
            created_at=created_at,
        )
    )
    return load_confirmed_thesis_state(
        repository,
        thesis_id,
        review_resolution=review_resolution,
        evidence_ids=evidence_ids,
    )


def _evaluate(
    action: ActionKind,
    *,
    blocked: bool = False,
    fact_set: SourceLinkedFactSet | None = None,
    confirmed_thesis: ConfirmedThesisState | None = None,
    source_resolutions: tuple[BriefSourceResolution, ...] | None = None,
) -> HeldFundBriefEvaluation:
    selected = _fact_set() if fact_set is None else fact_set
    route = _route(action, blocked=blocked)
    if source_resolutions is None:
        source_resolutions = tuple(
            _official_resolution(item.action_id)
            for item in route.actions
            if item.action_id not in {"fact_research", "switch_buy"}
        )
    result = HeldFundBriefEngine().evaluate(
        route=route,
        fact_set=selected,
        d2=_d2(selected),
        source_resolutions=source_resolutions,
        confirmed_thesis=confirmed_thesis,
    )
    assert type(result) is HeldFundBriefEvaluation
    assert type(result.sync_status) is EvidenceStatus
    assert type(result.decision_evidence_status) is EvidenceStatus
    assert all(type(item) is BriefActionInterpretation for item in result.interpretations)
    assert all(item.exact_amount_available is False for item in result.interpretations)
    return result


def _interpretation(result: HeldFundBriefEvaluation, action_id: str):
    return next(item for item in result.interpretations if item.action_id == action_id)


def test_multiple_field_resolutions_from_one_attempt_share_one_lineage() -> None:
    resolutions = (
        _official_resolution("continue_holding"),
        _official_resolution(
            "continue_holding",
            field_id="formal_nav",
            data_as_of=NOW - timedelta(days=30),
        ),
    )

    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        source_resolutions=resolutions,
    )

    assert result.resolution_lineage_ids == ("source_attempt_1",)
    assert tuple(binding.lineage_id for binding in result.resolution_bindings) == (
        "source_attempt_1",
        "source_attempt_1",
    )


def test_unchecked_alternative_suppresses_aggregate_manual_code() -> None:
    resolution = _official_resolution(
        "continue_holding",
        outcome=SourceAttemptOutcome.UNAVAILABLE,
        manual_supplement_ready=False,
        acceptable_alternative_ids=("eastmoney_f10",),
    )

    assert resolution.resolution is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED
    assert resolution.manual_supplementation_codes == ()


def test_phase_b_block_is_mature_no_add_without_suppressing_research() -> None:
    result = _evaluate(ActionKind.CONTINUE_HOLDING, blocked=True)
    interpretation = _interpretation(result, "continue_holding")

    assert result.primary_state is BriefState.NO_ADD
    assert interpretation.state is BriefState.NO_ADD
    assert interpretation.action_maturity is ActionMaturity.MATURE
    assert {"phase_b_blocked", "emergency_reserve_shortfall"}.issubset(
        interpretation.blocking_codes
    )
    assert interpretation.supporting_evidence_ids == ()
    assert BriefState.HOLD not in {item.state for item in result.interpretations}
    assert BriefState.REDUCE_OR_EXIT_REVIEW not in {item.state for item in result.interpretations}


@pytest.mark.parametrize(
    "event_code",
    (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        OfficialEventCode.FUND_TERMINATION_NOTICE,
    ),
)
def test_block_and_hard_event_preserve_no_add_and_exit_review(event_code) -> None:
    event = _event(event_code, "continue_holding")
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        blocked=True,
        fact_set=_fact_set(events=(event,)),
    )

    assert result.primary_state is BriefState.NO_ADD
    assert event_code.value in result.triggered_reviews
    assert event.event_id in _interpretation(result, "continue_holding").supporting_evidence_ids
    assert "immediate_sale" in _interpretation(result, "continue_holding").unavailable_actions


def test_liquidation_and_termination_reviews_coexist_without_trade_instruction() -> None:
    liquidation = _event(
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        "continue_holding",
        event_id="liquidation_event",
    )
    termination = _event(
        OfficialEventCode.FUND_TERMINATION_NOTICE,
        "continue_holding",
        event_id="termination_event",
    )
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(events=(liquidation, termination)),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert result.primary_state is BriefState.ABSTAIN
    assert result.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert result.triggered_reviews == (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE.value,
        OfficialEventCode.FUND_TERMINATION_NOTICE.value,
    )
    assert {liquidation.event_id, termination.event_id}.issubset(
        interpretation.supporting_evidence_ids
    )
    assert "continue_holding" in result.affected_action_abstentions
    assert "immediate_sale" in interpretation.unavailable_actions


def test_identity_conflict_abstains_only_the_affected_interpretation() -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(identity_conflict=True),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.ABSTAIN
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert "continue_holding" in result.affected_action_abstentions
    assert "identity_value_conflict" in result.conflicts
    assert "identity_value_conflict" in interpretation.blocking_codes


def test_block_does_not_hide_identity_abstention() -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        blocked=True,
        fact_set=_fact_set(identity_conflict=True),
    )

    assert result.primary_state is BriefState.NO_ADD
    assert "continue_holding" in result.affected_action_abstentions
    assert "identity_value_conflict" in result.conflicts


def test_missing_fee_conditions_leave_watch_but_block_fee_and_execution() -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(missing_fields=("fees_share_class_relationship",)),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.WATCH
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert "fees_share_class_relationship" in result.missing_fields
    assert "exact_fee" in interpretation.unavailable_actions
    assert "executable_redemption" in interpretation.unavailable_actions
    assert result.sync_status.state is BriefEvidenceState.PARTIAL


@pytest.mark.parametrize(
    ("event_code", "action_id"),
    (
        (OfficialEventCode.MANAGER_CHANGE_NOTICE, "continue_holding"),
        (OfficialEventCode.FEE_CHANGE_NOTICE, "continue_holding"),
        (OfficialEventCode.BENCHMARK_CHANGE_NOTICE, "continue_holding"),
    ),
)
def test_supported_non_exit_risk_events_trigger_watch_only(event_code, action_id) -> None:
    event = _event(event_code, action_id)
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(events=(event,)),
    )
    interpretation = _interpretation(result, action_id)

    assert interpretation.state is BriefState.WATCH
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert result.triggered_reviews == ()
    assert event.event_id in interpretation.supporting_evidence_ids


def test_confirmed_thesis_can_only_create_experimental_hold(tmp_path) -> None:
    resolution = _official_resolution("continue_holding")
    thesis = _thesis(tmp_path, review_resolution=resolution)
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        confirmed_thesis=thesis,
        source_resolutions=(resolution,),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.HOLD
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert interpretation.invalidation_conditions == ("基金角色或核心管理发生实质变化",)
    assert interpretation.state_inputs["thesis_record_id"] == str(thesis.thesis_id)
    assert interpretation.state_inputs["thesis_fingerprint"] == thesis.thesis_fingerprint


def test_confirmed_thesis_binds_exact_record_and_content_fingerprint(tmp_path) -> None:
    resolution = _official_resolution("continue_holding")
    repository = Repository(tmp_path / "exact-thesis.db")
    repository.migrate()
    first_id = repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="第一条持有理由",
            horizon="一年",
            invalidation="经理离任",
            created_at=resolution.evaluated_at + timedelta(seconds=1),
        )
    )
    second_id = repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="第二条持有理由",
            horizon="三年",
            invalidation="基金清盘",
            created_at=resolution.evaluated_at + timedelta(seconds=2),
        )
    )
    duplicate_id = repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="第一条持有理由",
            horizon="一年",
            invalidation="经理离任",
            created_at=resolution.evaluated_at + timedelta(seconds=1),
        )
    )

    first = load_confirmed_thesis_state(
        repository,
        first_id,
        review_resolution=resolution,
        evidence_ids=(),
    )
    second = load_confirmed_thesis_state(
        repository,
        second_id,
        review_resolution=resolution,
        evidence_ids=(),
    )
    duplicate = load_confirmed_thesis_state(
        repository,
        duplicate_id,
        review_resolution=resolution,
        evidence_ids=(),
    )

    assert first.thesis_id == first_id
    assert first.reason == "第一条持有理由"
    assert second.thesis_id == second_id
    assert second.reason == "第二条持有理由"
    assert first.thesis_fingerprint != second.thesis_fingerprint
    assert first.thesis_fingerprint != duplicate.thesis_fingerprint
    assert len(first.thesis_fingerprint) == 64
    assert (
        first.thesis_fingerprint
        == hashlib.sha256(
            canonical_json_bytes(
                {
                    "active": True,
                    "created_at": resolution.evaluated_at + timedelta(seconds=1),
                    "fund_code": FUND_CODE,
                    "horizon": "一年",
                    "invalidation": "经理离任",
                    "rationale": "第一条持有理由",
                    "thesis_id": first_id,
                }
            )
        ).hexdigest()
    )


@pytest.mark.parametrize("invalid_id", (True, "1", 0, -1))
def test_confirmed_thesis_rejects_invalid_record_id(tmp_path, invalid_id) -> None:
    resolution = _official_resolution("continue_holding")
    repository = Repository(tmp_path / f"invalid-thesis-{invalid_id}.db")
    repository.migrate()

    with pytest.raises(ValueError, match="positive integer"):
        load_confirmed_thesis_state(
            repository,
            invalid_id,
            review_resolution=resolution,
            evidence_ids=(),
        )


def test_confirmed_thesis_rejects_missing_inactive_and_wrong_fund_records(tmp_path) -> None:
    resolution = _official_resolution("continue_holding")
    repository = Repository(tmp_path / "rejected-theses.db")
    repository.migrate()
    inactive_id = repository.add_thesis(
        InvestmentThesis(
            FUND_CODE,
            "已停用理由",
            "一年",
            "经理离任",
            NOW - timedelta(days=1),
            active=False,
        )
    )
    wrong_fund_id = repository.add_thesis(
        InvestmentThesis(
            "000001",
            "其他基金理由",
            "一年",
            "经理离任",
            NOW - timedelta(days=1),
        )
    )

    with pytest.raises(ValueError, match="no active"):
        load_confirmed_thesis_state(
            repository,
            inactive_id,
            review_resolution=resolution,
            evidence_ids=(),
        )
    with pytest.raises(ValueError, match="not usable for the fund"):
        load_confirmed_thesis_state(
            repository,
            wrong_fund_id,
            review_resolution=resolution,
            evidence_ids=(),
        )
    with pytest.raises(ValueError, match="no active"):
        load_confirmed_thesis_state(
            repository,
            wrong_fund_id + 1,
            review_resolution=resolution,
            evidence_ids=(),
        )


def test_confirmed_thesis_id_and_fingerprint_are_mac_bound(tmp_path) -> None:
    resolution = _official_resolution("continue_holding")
    thesis = _thesis(tmp_path, review_resolution=resolution)

    with pytest.raises(ValueError, match="not authenticated"):
        replace(thesis, thesis_id=thesis.thesis_id + 1).validate()
    with pytest.raises(ValueError, match="not authenticated"):
        replace(thesis, thesis_fingerprint="f" * 64).validate()


def test_stale_cache_hit_is_degraded_and_cannot_authorize_hold(tmp_path) -> None:
    resolution = _official_resolution(
        "continue_holding",
        outcome=SourceAttemptOutcome.CACHE_HIT,
        data_as_of=NOW - timedelta(days=365),
    )
    repository = Repository(tmp_path / "stale-thesis.db")
    repository.migrate()
    thesis_id = repository.add_thesis(
        InvestmentThesis(
            fund_code=FUND_CODE,
            rationale="长期持有",
            horizon="三年",
            invalidation="经理离任",
            created_at=NOW - timedelta(days=30),
        )
    )

    assert resolution.resolution is RequestFieldResolution.PARTIAL
    assert resolution.source_states == (SourceFieldState.DEGRADED,)
    with pytest.raises(ValueError, match="not usable"):
        load_confirmed_thesis_state(
            repository,
            thesis_id,
            review_resolution=resolution,
            evidence_ids=(),
        )


def test_no_confirmed_thesis_is_watch_not_inferred_hold() -> None:
    result = _evaluate(ActionKind.CONTINUE_HOLDING)
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.WATCH
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW


def test_invalidated_thesis_cannot_support_hold(tmp_path) -> None:
    event = _event(OfficialEventCode.MANAGER_CHANGE_NOTICE, "continue_holding")
    resolution = _official_resolution("continue_holding")
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(events=(event,)),
        confirmed_thesis=_thesis(
            tmp_path,
            review_resolution=resolution,
            review_state=ThesisReviewState.TRIGGERED,
            evidence_ids=(event.event_id,),
        ),
        source_resolutions=(resolution,),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is not BriefState.HOLD
    assert "thesis_invalidation_triggered" in interpretation.blocking_codes


@pytest.mark.parametrize(
    ("field_id", "value"),
    (
        ("one_day_return", {"return_percent": "-9.9", "date": "2026-07-16"}),
        ("short_term_rank", {"rank": "1", "window": "one_week"}),
        ("media_claim", {"publisher": "财经媒体", "claim": "建议立即卖出"}),
    ),
)
def test_one_day_ranking_and_media_claims_never_trigger_exit_review(field_id, value) -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(extra_facts=(_fact(field_id, value),)),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.WATCH
    assert result.triggered_reviews == ()
    assert BriefState.REDUCE_OR_EXIT_REVIEW not in {item.state for item in result.interpretations}


@pytest.mark.parametrize("action", (ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT))
def test_reduce_and_exit_research_remain_available_under_phase_b_block(action) -> None:
    redemption_terms = _fact(
        "redemption_terms",
        {
            "fee_condition": "holding_period_required",
            "settlement_condition": "published_rule_available",
        },
    )
    result = _evaluate(
        action,
        blocked=True,
        fact_set=_fact_set(extra_facts=(redemption_terms,)),
    )
    interpretation = _interpretation(result, action.value)

    assert interpretation.state is BriefState.REDUCE_OR_EXIT_REVIEW
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert "phase_b_blocked" not in interpretation.blocking_codes
    assert "automatic_trade" in interpretation.unavailable_actions


def test_switch_reduce_and_buy_legs_remain_independent() -> None:
    redemption_terms = _fact(
        "redemption_terms",
        {"fee_condition": "published", "settlement_condition": "published"},
    )
    result = _evaluate(
        ActionKind.SWITCH_FUNDS,
        blocked=True,
        fact_set=_fact_set(extra_facts=(redemption_terms,)),
    )
    reduce_leg = _interpretation(result, "switch_reduce")
    buy_leg = _interpretation(result, "switch_buy")

    assert tuple(item.action_id for item in result.interpretations) == (
        "switch_reduce",
        "switch_buy",
    )
    assert reduce_leg.state is BriefState.REDUCE_OR_EXIT_REVIEW
    assert reduce_leg.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert "phase_b_blocked" not in reduce_leg.blocking_codes
    assert buy_leg.state is BriefState.ABSTAIN
    assert {
        "phase_b_blocked",
        "phase_c_missing",
        "d1_missing",
        "d2_missing",
        "d3_missing",
        "post_trade_missing",
    }.issubset(buy_leg.blocking_codes)
    assert "d2" not in result.decision_evidence_status.obtained_fields
    assert "d2" in result.decision_evidence_status.missing_fields
    assert "switch_buy" in result.affected_action_abstentions


def test_arbitrary_source_resolution_is_rejected() -> None:
    fact_set = _fact_set()
    with pytest.raises(ValueError, match="source resolution"):
        HeldFundBriefEngine().evaluate(
            route=_route(ActionKind.CONTINUE_HOLDING),
            fact_set=fact_set,
            d2=_d2(fact_set),
            source_resolutions=(object(),),
            confirmed_thesis=None,
        )


def test_phase1_rejects_an_upstream_route_that_claims_exact_amount() -> None:
    fact_set = _fact_set()
    route = _route(ActionKind.SWITCH_FUNDS, blocked=True)
    buy_leg = route.actions[-1]
    forged_buy = replace(
        buy_leg,
        blocking_codes=(),
        research_available=True,
        exact_amount_available=True,
        minimum_state=ActionState.ACTIONABLE,
        action_maturity=ActionMaturity.MATURE,
    )
    forged_route = replace(route, actions=(*route.actions[:-1], forged_buy), missing_fields=())
    forged_route.validate()

    with pytest.raises(ValueError, match="exact amount"):
        HeldFundBriefEngine().evaluate(
            route=forged_route,
            fact_set=fact_set,
            d2=_d2(fact_set),
            source_resolutions=(),
            confirmed_thesis=None,
        )


def test_research_unavailable_route_abstains_even_with_intact_thesis(tmp_path) -> None:
    fact_set = _fact_set()
    route = _route(ActionKind.CONTINUE_HOLDING)
    unavailable_action = replace(route.actions[-1], research_available=False)
    unavailable_route = replace(route, actions=(*route.actions[:-1], unavailable_action))
    unavailable_route.validate()
    resolution = _official_resolution("continue_holding")
    thesis = _thesis(tmp_path, review_resolution=resolution)

    result = HeldFundBriefEngine().evaluate(
        route=unavailable_route,
        fact_set=fact_set,
        d2=_d2(fact_set),
        source_resolutions=(resolution,),
        confirmed_thesis=thesis,
    )

    interpretation = _interpretation(result, "continue_holding")
    assert interpretation.state is BriefState.ABSTAIN
    assert "research_unavailable" in interpretation.blocking_codes


def test_source_resolution_evidence_must_match_the_claimed_field() -> None:
    fact_set = _fact_set()
    mismatched = _official_resolution(
        "continue_holding",
        evidence_ids=("formal_nav",),
    )

    with pytest.raises(ValueError, match="wrong type"):
        HeldFundBriefEngine().evaluate(
            route=_route(ActionKind.CONTINUE_HOLDING),
            fact_set=fact_set,
            d2=_d2(fact_set),
            source_resolutions=(mismatched,),
            confirmed_thesis=None,
        )


def test_fact_research_resolution_cannot_authenticate_intact_thesis(tmp_path) -> None:
    fact_resolution = _official_resolution("fact_research")

    with pytest.raises(ValueError, match="not usable for the fund"):
        _thesis(
            tmp_path,
            review_resolution=fact_resolution,
            review_state=ThesisReviewState.INTACT,
        )


def test_engine_contract_exports_are_exact_types() -> None:
    assert all(
        isinstance(value, type)
        for value in (
            BriefSourceResolution,
            ConfirmedThesisState,
            EvidenceStatus,
            HeldFundBriefEvaluation,
            HeldFundBriefEngine,
        )
    )


def test_empty_announcement_result_requires_action_bound_negative_check() -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        source_resolutions=(),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.ABSTAIN
    assert "official_events" in interpretation.missing_fields
    assert "continue_holding" in result.affected_action_abstentions


def test_fact_research_resolution_cannot_authorize_holding_negative_check() -> None:
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        source_resolutions=(_official_resolution("fact_research"),),
    )

    assert _interpretation(result, "continue_holding").state is BriefState.ABSTAIN


@pytest.mark.parametrize("integrity_status", ("corrected", "retracted"))
def test_inactive_official_event_forces_affected_action_abstention(
    integrity_status: str,
) -> None:
    event = _event(
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        "continue_holding",
        integrity_status=integrity_status,
    )
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(events=(event,)),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.ABSTAIN
    assert event.event_id in interpretation.opposing_evidence_ids
    assert f"official_event_{integrity_status}_{event.event_id}" in (interpretation.blocking_codes)


def test_weak_market_or_media_fact_never_supports_thesis_hold(tmp_path) -> None:
    weak = (
        _fact("one_day_return", {"return_percent": "-9.9", "date": "2026-07-16"}),
        _fact("short_term_rank", {"rank": "1", "window": "one_week"}),
        _fact("media_claim", {"publisher": "财经媒体", "claim": "建议立即卖出"}),
    )
    resolution = _official_resolution("continue_holding")
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        fact_set=_fact_set(extra_facts=weak),
        confirmed_thesis=_thesis(tmp_path, review_resolution=resolution),
        source_resolutions=(resolution,),
    )
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.HOLD
    assert not {item.fact_id for item in weak}.intersection(interpretation.supporting_evidence_ids)


def test_liquidation_review_preserves_redemption_restriction_abstention() -> None:
    liquidation = _event(
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        "full_exit",
        event_id="liquidation_for_exit",
    )
    restriction = _event(
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        "full_exit",
        event_id="redemption_restricted",
    )
    terms = _fact(
        "redemption_terms",
        {"fee_condition": "published", "settlement_condition": "published"},
    )
    result = _evaluate(
        ActionKind.FULL_EXIT,
        fact_set=_fact_set(events=(liquidation, restriction), extra_facts=(terms,)),
    )
    interpretation = _interpretation(result, "full_exit")

    assert interpretation.state is BriefState.ABSTAIN
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE.value in (interpretation.blocking_codes)
    assert "executable_redemption" in interpretation.unavailable_actions
    assert "full_exit" in result.affected_action_abstentions
    assert "full_exit" not in result.decision_evidence_status.supported_interpretations
    assert "full_exit" in result.decision_evidence_status.unsupported_interpretations


@pytest.mark.parametrize("action", (ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT))
def test_transaction_research_without_redemption_terms_abstains(action) -> None:
    result = _evaluate(action)
    interpretation = _interpretation(result, action.value)

    assert interpretation.state is BriefState.ABSTAIN
    assert "redemption_terms_missing" in interpretation.blocking_codes
    assert "exact_fee" in interpretation.unavailable_actions
    assert "executable_redemption" in interpretation.unavailable_actions


def test_partial_formal_nav_cannot_support_continue_holding() -> None:
    facts = _fact_set().facts
    partial_nav = replace(
        next(item for item in facts if item.field_id == "formal_nav"),
        completeness=EvidenceCompleteness.PARTIAL,
    )
    fact_set = replace(
        _fact_set(),
        facts=tuple(
            partial_nav if item.field_id == "formal_nav" else item for item in facts
        ),
    )

    result = _evaluate(ActionKind.CONTINUE_HOLDING, fact_set=fact_set)
    interpretation = _interpretation(result, "continue_holding")

    assert interpretation.state is BriefState.ABSTAIN
    assert "formal_nav" in interpretation.missing_fields
    assert "continue_holding" in result.affected_action_abstentions


@pytest.mark.parametrize("action", (ActionKind.REDUCE_TO_CASH, ActionKind.FULL_EXIT))
def test_partial_redemption_terms_cannot_support_transaction_research(action) -> None:
    partial_terms = replace(
        _fact(
            "redemption_terms",
            {"fee_condition": "published", "settlement_condition": "published"},
        ),
        completeness=EvidenceCompleteness.PARTIAL,
    )

    result = _evaluate(action, fact_set=_fact_set(extra_facts=(partial_terms,)))
    interpretation = _interpretation(result, action.value)

    assert interpretation.state is BriefState.ABSTAIN
    assert "redemption_terms" in interpretation.missing_fields
    assert "redemption_terms_missing" in interpretation.blocking_codes


@pytest.mark.parametrize(
    "event_code",
    (OfficialEventCode.FUND_LIQUIDATION_NOTICE, OfficialEventCode.FUND_TERMINATION_NOTICE),
)
def test_hard_event_without_redemption_terms_triggers_review_but_abstains(event_code) -> None:
    event = _event(event_code, "full_exit")

    result = _evaluate(
        ActionKind.FULL_EXIT,
        fact_set=_fact_set(events=(event,)),
    )
    interpretation = _interpretation(result, "full_exit")

    assert event_code.value in result.triggered_reviews
    assert event.event_id in interpretation.supporting_evidence_ids
    assert interpretation.state is BriefState.ABSTAIN
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert "redemption_terms_missing" in interpretation.blocking_codes
    assert "immediate_sale" in interpretation.unavailable_actions


def test_hard_event_with_terms_still_requires_every_route_gate() -> None:
    liquidation = _event(OfficialEventCode.FUND_LIQUIDATION_NOTICE, "full_exit")
    terms = _fact(
        "redemption_terms",
        {"fee_condition": "published", "settlement_condition": "published"},
    )

    result = _evaluate(
        ActionKind.FULL_EXIT,
        fact_set=_fact_set(events=(liquidation,), extra_facts=(terms,)),
    )
    interpretation = _interpretation(result, "full_exit")

    assert interpretation.state is BriefState.ABSTAIN
    assert interpretation.action_maturity is ActionMaturity.EXPERIMENTAL_SHADOW
    assert {"exit_reason", "use_of_proceeds"}.issubset(interpretation.missing_fields)
    assert set(result.decision_evidence_status.obtained_fields).isdisjoint(
        result.decision_evidence_status.missing_fields
    )


def test_plain_fact_cannot_satisfy_reserved_complete_d2_gate() -> None:
    forged_d2 = _fact("d2", {"evidence_state": "complete"})

    result = _evaluate(
        ActionKind.SWITCH_FUNDS,
        fact_set=_fact_set(extra_facts=(forged_d2,)),
    )
    switch_buy = _interpretation(result, "switch_buy")

    assert switch_buy.state is BriefState.ABSTAIN
    assert "d2" in switch_buy.missing_fields
    assert "d2" not in result.decision_evidence_status.obtained_fields


def test_transaction_leg_with_terms_but_no_announcement_check_abstains() -> None:
    terms = _fact(
        "redemption_terms",
        {"fee_condition": "published", "settlement_condition": "published"},
    )
    result = _evaluate(
        ActionKind.REDUCE_TO_CASH,
        fact_set=_fact_set(extra_facts=(terms,)),
        source_resolutions=(),
    )

    interpretation = _interpretation(result, "reduce_to_cash")
    assert interpretation.state is BriefState.ABSTAIN
    assert "official_events" in interpretation.missing_fields


def test_unknown_thesis_review_is_watch_and_integrity_bound(tmp_path) -> None:
    resolution = _official_resolution("continue_holding")
    thesis = _thesis(
        tmp_path,
        review_resolution=resolution,
        review_state=ThesisReviewState.UNKNOWN,
    )
    result = _evaluate(
        ActionKind.CONTINUE_HOLDING,
        confirmed_thesis=thesis,
        source_resolutions=(resolution,),
    )

    assert _interpretation(result, "continue_holding").state is BriefState.WATCH
    assert thesis.reviewed_at is None
    assert _interpretation(result, "continue_holding").state_inputs["thesis_reviewed_at"] is None
    with pytest.raises(ValueError):
        replace(thesis, review_state=ThesisReviewState.INTACT).validate()


def test_decision_status_separates_stale_and_conflicted_from_missing() -> None:
    facts = _base_facts()
    stale_nav = replace(facts[4], freshness=EvidenceFreshness.STALE)
    conflicted_identity = replace(facts[0], conflict_ids=("identity_value_conflict",))
    fact_set = SourceLinkedFactSet(
        fund_code=FUND_CODE,
        facts=(conflicted_identity, *facts[1:4], stale_nav, facts[5]),
        official_events=(),
        missing_fields=(),
        conflicts=("identity_value_conflict",),
        warnings=(),
    )
    fact_set.validate()
    result = _evaluate(ActionKind.CONTINUE_HOLDING, fact_set=fact_set)

    assert "formal_nav" in result.decision_evidence_status.stale_fields
    assert "identity_active_status" in result.decision_evidence_status.conflicted_fields
    assert "formal_nav" not in result.decision_evidence_status.missing_fields
    assert "identity_active_status" not in result.decision_evidence_status.missing_fields


def test_holding_decision_status_excludes_unrelated_d2_sync_gaps() -> None:
    result = _evaluate(ActionKind.CONTINUE_HOLDING)

    assert "authenticated_index_identity_519755" in result.sync_status.missing_fields
    assert "authenticated_index_identity_519755" not in (
        result.decision_evidence_status.missing_fields
    )
    assert "holdings_evidence_missing_519755" not in (
        result.decision_evidence_status.missing_fields
    )


def test_future_official_event_is_rejected_before_action_interpretation() -> None:
    future = replace(
        _event(OfficialEventCode.FUND_LIQUIDATION_NOTICE, "continue_holding"),
        published_at=NOW + timedelta(days=1),
        retrieved_at=NOW + timedelta(days=2),
    )
    fact_set = _fact_set(events=(future,))

    with pytest.raises(ValueError, match="later than the brief"):
        _evaluate(ActionKind.CONTINUE_HOLDING, fact_set=fact_set)


def test_engine_rejects_cross_type_evidence_identifier_collision() -> None:
    original = _fact_set()
    fact_set = replace(
        original,
        facts=tuple(
            replace(fact, fact_id="d2_minimum_relationship_coverage")
            if fact.field_id == "formal_nav"
            else fact
            for fact in original.facts
        ),
    )
    fact_set.validate()
    d2 = _d2(fact_set)
    d2.validate()

    with pytest.raises(ValueError, match="conflicting identifier"):
        HeldFundBriefEngine().evaluate(
            route=_route(ActionKind.CONTINUE_HOLDING),
            fact_set=fact_set,
            d2=d2,
            source_resolutions=(),
            confirmed_thesis=None,
        )


def test_engine_rejects_narrowed_official_event_action_scope() -> None:
    liquidation = replace(
        _event(OfficialEventCode.FUND_LIQUIDATION_NOTICE, "continue_holding"),
        affected_action_ids=("fact_research",),
    )
    fact_set = _fact_set(events=(liquidation,))
    route = _route(ActionKind.CONTINUE_HOLDING)

    with pytest.raises(ValueError, match="action binding"):
        HeldFundBriefEngine().evaluate(
            route=route,
            fact_set=fact_set,
            d2=_d2(fact_set),
            source_resolutions=(_official_resolution("continue_holding"),),
            confirmed_thesis=None,
        )


def test_holdings_only_coverage_id_is_not_a_persistable_evidence_id() -> None:
    fact_set = _fact_set()
    d2 = _d2(fact_set)

    with pytest.raises(ValueError, match="does not close"):
        HeldFundBriefEngine().evaluate(
            route=_route(ActionKind.CONTINUE_HOLDING),
            fact_set=fact_set,
            d2=d2,
            source_resolutions=(
                _official_resolution(
                    "fact_research",
                    evidence_ids=(d2.holdings_coverage.coverage_id,),
                ),
            ),
            confirmed_thesis=None,
        )
