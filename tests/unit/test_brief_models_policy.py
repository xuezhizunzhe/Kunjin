import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

import pytest

from kunjin.brief.models import (
    BriefActionInterpretation,
    BriefCoverage,
    BriefEvidenceState,
    BriefEvidenceStatus,
    BriefFact,
    BriefResolutionBinding,
    BriefSnapshot,
    BriefState,
    HeldFundBriefReport,
    OfficialEvent,
    OfficialEventCode,
    RelationshipEvidence,
)
from kunjin.brief.policy import (
    DEEP_NAV_MAX_PAGES,
    HELD_FUND_BRIEF_POLICY_V1_GOLDEN_CHECKSUM,
    MAX_FACTS,
    MAX_OFFICIAL_EVENTS,
    MAX_RELATIONSHIPS,
    MIN_CORRELATION_SAMPLES,
    RAPID_NAV_MAX_PAGES,
    HeldFundBriefPolicyV1,
)
from kunjin.decision.models import (
    ActionMaturity,
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestFieldResolution,
    RequestMode,
    SourceFieldState,
    SourceTier,
)

NOW = datetime(2026, 7, 17, 6, 0, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def _fact(**overrides: object) -> BriefFact:
    values = {
        "fact_id": "formal_nav_1",
        "field_id": "formal_nav",
        "value": "1.2345",
        "unit": "cny_per_share",
        "data_as_of": NOW - timedelta(days=1),
        "published_at": NOW - timedelta(hours=1),
        "retrieved_at": NOW,
        "source_id": "eastmoney_nav",
        "source_tier": SourceTier.TIER_2,
        "publisher": "Eastmoney",
        "canonical_url": "https://example.test/fund/123456",
        "freshness": EvidenceFreshness.CURRENT,
        "completeness": EvidenceCompleteness.COMPLETE,
        "conflict_ids": (),
        "calculated": False,
        "source_lineage_id": "eastmoney_nav_123456",
    }
    values.update(overrides)
    return BriefFact(**values)


def _manager_fact(**overrides: object) -> BriefFact:
    values = {
        "fact_id": "manager_fact_1",
        "field_id": "current_manager_team",
        "value": "Example Manager",
        "unit": None,
        "data_as_of": NOW - timedelta(days=1),
        "published_at": NOW - timedelta(hours=1),
        "retrieved_at": NOW,
        "source_id": "eastmoney_f10",
        "source_tier": SourceTier.TIER_2,
        "publisher": "Eastmoney",
        "canonical_url": "https://example.test/fund/123456/manager",
        "freshness": EvidenceFreshness.CURRENT,
        "completeness": EvidenceCompleteness.COMPLETE,
        "conflict_ids": (),
        "calculated": False,
        "source_lineage_id": "eastmoney_manager_123456",
    }
    values.update(overrides)
    return BriefFact(**values)


def _event(**overrides: object) -> OfficialEvent:
    values = {
        "event_id": "manager_change_1",
        "event_code": OfficialEventCode.MANAGER_CHANGE_NOTICE,
        "title": "Fund manager change notice",
        "summary": "The official manager roster changed.",
        "publisher": "Example Fund Manager",
        "canonical_url": "https://example.test/announcement/1",
        "published_at": NOW - timedelta(hours=2),
        "retrieved_at": NOW,
        "source_tier": SourceTier.TIER_1,
        "original_source_id": "official_announcement_1",
        "quoted_source_id": None,
        "content_fingerprint": CHECKSUM,
        "integrity_status": "active",
        "affected_action_ids": ("fact_research", "continue_holding"),
    }
    values.update(overrides)
    return OfficialEvent(**values)


def _relationship(**overrides: object) -> RelationshipEvidence:
    values = {
        "relationship_id": "same_manager_1",
        "relationship_type": "same_manager",
        "fund_codes": ("123456", "654321"),
        "evidence_state": BriefEvidenceState.COMPLETE,
        "metrics": {"matched": True},
        "evidence_ids": ("manager_fact_1",),
        "report_periods": (),
        "publication_times": (NOW - timedelta(hours=1),),
        "warnings": (),
    }
    values.update(overrides)
    return RelationshipEvidence(**values)


def _coverage(**overrides: object) -> BriefCoverage:
    values = {
        "coverage_id": "portfolio_relationship_coverage",
        "scope": "current_fund_portfolio",
        "evidence_state": BriefEvidenceState.PARTIAL,
        "included_fund_codes": ("123456",),
        "omitted_fund_codes": ("654321",),
        "known_percent": "50",
        "unknown_fields": ("industry_exposure",),
        "evidence_ids": ("same_manager_1",),
    }
    values.update(overrides)
    return BriefCoverage(**values)


def _evidence_status(**overrides: object) -> BriefEvidenceStatus:
    values = {
        "state": BriefEvidenceState.PARTIAL,
        "required_fields": ("identity_active_status",),
        "obtained_fields": ("identity_active_status",),
        "missing_fields": (),
        "stale_fields": (),
        "conflicted_fields": (),
        "unsupported_fields": (),
        "cooldown_fields": (),
        "supported_interpretations": ("continue_holding",),
        "unsupported_interpretations": (),
        "acceptable_alternative_ids": (),
        "manual_supplementation_codes": (),
    }
    values.update(overrides)
    return BriefEvidenceStatus(**values)


def _interpretation(**overrides: object) -> BriefActionInterpretation:
    values = {
        "action_id": "continue_holding",
        "state": BriefState.WATCH,
        "action_maturity": ActionMaturity.EXPERIMENTAL_SHADOW,
        "supporting_evidence_ids": ("formal_nav_1",),
        "opposing_evidence_ids": (),
        "blocking_codes": (),
        "missing_fields": ("owner_confirmed_thesis",),
        "invalidation_conditions": ("A verified liquidation notice is published.",),
        "unavailable_actions": ("exact_amount",),
        "exact_amount_available": False,
        "state_inputs": {"owner_confirmed_thesis": False},
    }
    values.update(overrides)
    return BriefActionInterpretation(**values)


def _resolution_binding(**overrides: object) -> BriefResolutionBinding:
    values = {
        "action_id": "continue_holding",
        "field_id": "official_events",
        "resolution": RequestFieldResolution.USABLE,
        "source_states": (SourceFieldState.HEALTHY,),
        "source_attempt_id": 7,
        "source_id": "fund_manager_official_documents",
        "source_field_id": "fund_manager_product_announcement",
        "evaluated_at": NOW - timedelta(minutes=1),
    }
    values.update(overrides)
    return BriefResolutionBinding(**values)


def _snapshot(**overrides: object) -> BriefSnapshot:
    values = {
        "request_run_id": 1,
        "decision_snapshot_id": 2,
        "fund_code": "123456",
        "action_ids": ("fact_research", "continue_holding"),
        "mode": RequestMode.RAPID,
        "facts": (_fact(), _manager_fact()),
        "official_events": (_event(),),
        "relationships": (_relationship(),),
        "coverage": _coverage(),
        "holdings_coverage": _coverage(
            coverage_id="disclosed_holdings_coverage",
        ),
        "sync_status": _evidence_status(),
        "decision_evidence_status": _evidence_status(),
        "interpretations": (_interpretation(),),
        "primary_state": BriefState.WATCH,
        "action_maturity": ActionMaturity.EXPERIMENTAL_SHADOW,
        "constraints": (),
        "triggered_reviews": (),
        "affected_action_abstentions": (),
        "blocking_codes": (),
        "evidence_state": BriefEvidenceState.PARTIAL,
        "missing_fields": ("owner_confirmed_thesis", "industry_exposure"),
        "conflicts": (),
        "source_lineage_ids": (
            "eastmoney_nav_123456",
            "eastmoney_manager_123456",
            "official_announcement_1",
        ),
        "evidence_fingerprint": CHECKSUM,
        "created_at": NOW,
        "portfolio_evidence_state": "current",
        "position_present": True,
        "observation_version": "portfolio_observation_1",
        "observed_at": NOW,
    }
    values.update(overrides)
    return BriefSnapshot(**values)


def test_brief_enums_are_exact() -> None:
    assert tuple(item.value for item in BriefState) == (
        "no_add",
        "hold",
        "watch",
        "reduce_or_exit_review",
        "abstain",
    )
    assert tuple(item.value for item in BriefEvidenceState) == (
        "complete",
        "partial",
        "insufficient",
    )
    assert tuple(item.value for item in OfficialEventCode) == (
        "fund_liquidation_notice",
        "fund_termination_notice",
        "manager_change_notice",
        "subscription_suspension_notice",
        "redemption_restriction_notice",
        "fee_change_notice",
        "benchmark_change_notice",
        "other_official_product_notice",
    )


def test_switch_primary_preserves_reduce_leg_and_phase_b_no_add() -> None:
    reduce_leg = _interpretation(
        action_id="switch_reduce",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
        blocking_codes=("fees_missing",),
        unavailable_actions=("exact_amount", "automatic_trade"),
    )
    buy_leg = _interpretation(
        action_id="switch_buy",
        state=BriefState.ABSTAIN,
        blocking_codes=("phase_b_blocked", "d3_missing"),
        missing_fields=("d3",),
    )
    snapshot = _snapshot(
        action_ids=("fact_research", "switch_reduce", "switch_buy"),
        official_events=(),
        interpretations=(reduce_leg, buy_leg),
        primary_state=BriefState.NO_ADD,
        action_maturity=ActionMaturity.MATURE,
        affected_action_abstentions=("switch_buy",),
        blocking_codes=("fees_missing", "phase_b_blocked", "d3_missing"),
        missing_fields=("owner_confirmed_thesis", "d3", "industry_exposure"),
        source_lineage_ids=("eastmoney_nav_123456", "eastmoney_manager_123456"),
    )

    snapshot.validate()
    assert snapshot.interpretations[0].state is BriefState.REDUCE_OR_EXIT_REVIEW
    assert snapshot.primary_state is BriefState.NO_ADD


def test_snapshot_persists_authenticated_resolution_lineage() -> None:
    snapshot = _snapshot(
        source_lineage_ids=(
            "eastmoney_nav_123456",
            "eastmoney_manager_123456",
            "official_announcement_1",
            "source_attempt_7",
        ),
        resolution_lineage_ids=("source_attempt_7",),
        resolution_bindings=(_resolution_binding(),),
    )

    snapshot.validate()
    assert snapshot.to_canonical_dict()["resolution_lineage_ids"] == ["source_attempt_7"]
    assert snapshot.to_canonical_dict()["resolution_bindings"][0]["action_id"] == (
        "continue_holding"
    )


def test_hold_requires_thesis_and_official_resolution_binding() -> None:
    hold = _interpretation(
        state=BriefState.HOLD,
        missing_fields=(),
        state_inputs={"owner_confirmed_thesis": False},
    )
    snapshot = _snapshot(
        official_events=(),
        interpretations=(hold,),
        primary_state=BriefState.HOLD,
        missing_fields=("industry_exposure",),
        source_lineage_ids=("eastmoney_nav_123456", "eastmoney_manager_123456"),
    )

    with pytest.raises(ValueError, match="hold requires"):
        snapshot.validate()


@pytest.mark.parametrize(
    "state",
    (BriefState.HOLD, BriefState.WATCH, BriefState.ABSTAIN),
)
def test_experimental_states_reject_mature_label(state: BriefState) -> None:
    interpretation = _interpretation(
        state=state,
        action_maturity=ActionMaturity.MATURE,
    )

    with pytest.raises(ValueError, match="maturity"):
        interpretation.validate()


def test_no_add_rejects_experimental_label() -> None:
    interpretation = _interpretation(
        state=BriefState.NO_ADD,
        action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
    )

    with pytest.raises(ValueError, match="maturity"):
        interpretation.validate()


def test_mature_exit_review_requires_active_hard_official_event() -> None:
    interpretation = _interpretation(
        action_id="full_exit",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
        action_maturity=ActionMaturity.MATURE,
        supporting_evidence_ids=("formal_nav_1",),
        missing_fields=(),
    )
    snapshot = _snapshot(
        action_ids=("fact_research", "full_exit"),
        official_events=(),
        interpretations=(interpretation,),
        primary_state=BriefState.REDUCE_OR_EXIT_REVIEW,
        action_maturity=ActionMaturity.MATURE,
        missing_fields=("industry_exposure",),
        source_lineage_ids=("eastmoney_nav_123456", "eastmoney_manager_123456"),
    )

    with pytest.raises(ValueError, match="hard official event"):
        snapshot.validate()


def test_holding_action_rejects_experimental_exit_review() -> None:
    interpretation = _interpretation(
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
        action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
    )
    snapshot = _snapshot(
        interpretations=(interpretation,),
        primary_state=BriefState.REDUCE_OR_EXIT_REVIEW,
    )

    with pytest.raises(ValueError, match="action state"):
        snapshot.validate()


def test_switch_buy_rejects_watch_state() -> None:
    reduce_leg = _interpretation(
        action_id="switch_reduce",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
        action_maturity=ActionMaturity.EXPERIMENTAL_SHADOW,
    )
    buy_leg = _interpretation(
        action_id="switch_buy",
        state=BriefState.WATCH,
    )
    snapshot = _snapshot(
        action_ids=("fact_research", "switch_reduce", "switch_buy"),
        official_events=(),
        interpretations=(reduce_leg, buy_leg),
        primary_state=BriefState.REDUCE_OR_EXIT_REVIEW,
        source_lineage_ids=("eastmoney_nav_123456", "eastmoney_manager_123456"),
    )

    with pytest.raises(ValueError, match="action state"):
        snapshot.validate()


def test_active_liquidation_cannot_be_persisted_as_hold() -> None:
    liquidation = _event(
        event_id="liquidation_1",
        event_code=OfficialEventCode.FUND_LIQUIDATION_NOTICE,
    )
    binding = _resolution_binding()
    hold = _interpretation(
        state=BriefState.HOLD,
        supporting_evidence_ids=(liquidation.event_id,),
        missing_fields=(),
        invalidation_conditions=("基金进入清盘程序",),
        state_inputs={
            "owner_confirmed_thesis": True,
            "thesis_fingerprint": CHECKSUM,
            "thesis_record_id": "1",
            "thesis_review_source_lineage_id": binding.lineage_id,
            "thesis_review_state": "intact",
            "thesis_reviewed_at": binding.evaluated_at,
        },
    )
    snapshot = _snapshot(
        official_events=(liquidation,),
        interpretations=(hold,),
        primary_state=BriefState.HOLD,
        triggered_reviews=(OfficialEventCode.FUND_LIQUIDATION_NOTICE.value,),
        resolution_bindings=(binding,),
        resolution_lineage_ids=(binding.lineage_id,),
        source_lineage_ids=(
            "eastmoney_nav_123456",
            "eastmoney_manager_123456",
            liquidation.original_source_id,
            binding.lineage_id,
        ),
    )

    with pytest.raises(ValueError, match="hard official event"):
        snapshot.validate()


def test_triggered_reviews_exactly_match_active_hard_events() -> None:
    snapshot = _snapshot(
        triggered_reviews=(OfficialEventCode.FUND_TERMINATION_NOTICE.value,),
    )

    with pytest.raises(ValueError, match="triggered reviews"):
        snapshot.validate()


def test_event_cannot_narrow_its_canonical_affected_actions() -> None:
    liquidation = _event(
        event_code=OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        affected_action_ids=("fact_research",),
    )
    snapshot = _snapshot(
        official_events=(liquidation,),
        triggered_reviews=(OfficialEventCode.FUND_LIQUIDATION_NOTICE.value,),
    )

    with pytest.raises(ValueError, match="affected actions"):
        snapshot.validate()


def test_redemption_restriction_cannot_be_ignored_by_exit_review() -> None:
    restriction = _event(
        event_id="redemption_restriction_1",
        event_code=OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        affected_action_ids=("fact_research", "full_exit"),
    )
    interpretation = _interpretation(
        action_id="full_exit",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
        supporting_evidence_ids=(restriction.event_id,),
        missing_fields=(),
        unavailable_actions=("exact_amount", "automatic_trade"),
    )
    snapshot = _snapshot(
        action_ids=("fact_research", "full_exit"),
        official_events=(restriction,),
        interpretations=(interpretation,),
        primary_state=BriefState.REDUCE_OR_EXIT_REVIEW,
        missing_fields=("industry_exposure",),
        source_lineage_ids=(
            "eastmoney_nav_123456",
            "eastmoney_manager_123456",
            restriction.original_source_id,
        ),
    )

    with pytest.raises(ValueError, match="redemption restriction"):
        snapshot.validate()


@pytest.mark.parametrize(
    "event_code",
    (
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        OfficialEventCode.FEE_CHANGE_NOTICE,
        OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
        OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE,
    ),
)
def test_active_watch_event_cannot_be_persisted_as_hold(event_code) -> None:
    event = _event(event_code=event_code)
    binding = _resolution_binding()
    hold = _interpretation(
        state=BriefState.HOLD,
        supporting_evidence_ids=(event.event_id,),
        missing_fields=(),
        state_inputs={
            "owner_confirmed_thesis": True,
            "thesis_fingerprint": CHECKSUM,
            "thesis_record_id": "1",
            "thesis_review_source_lineage_id": binding.lineage_id,
            "thesis_review_state": "intact",
            "thesis_reviewed_at": binding.evaluated_at,
        },
    )
    snapshot = _snapshot(
        official_events=(event,),
        interpretations=(hold,),
        primary_state=BriefState.HOLD,
        resolution_bindings=(binding,),
        resolution_lineage_ids=(binding.lineage_id,),
        source_lineage_ids=(
            "eastmoney_nav_123456",
            "eastmoney_manager_123456",
            event.original_source_id,
            binding.lineage_id,
        ),
    )

    with pytest.raises(ValueError, match="risk event"):
        snapshot.validate()


def test_subscription_suspension_must_keep_buy_or_add_unavailable() -> None:
    suspension = _event(event_code=OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE)
    watch = _interpretation(
        state=BriefState.WATCH,
        supporting_evidence_ids=(suspension.event_id,),
        unavailable_actions=("exact_amount",),
    )
    snapshot = _snapshot(
        official_events=(suspension,),
        interpretations=(watch,),
    )

    with pytest.raises(ValueError, match="subscription suspension"):
        snapshot.validate()


@pytest.mark.parametrize("integrity_status", ("corrected", "retracted"))
def test_inactive_event_requires_affected_action_abstention(integrity_status) -> None:
    inactive = _event(integrity_status=integrity_status)
    snapshot = _snapshot(
        official_events=(inactive,),
    )

    with pytest.raises(ValueError, match="inactive official event"):
        snapshot.validate()


def test_snapshot_is_canonical_ascii_and_owner_overlay_is_ephemeral() -> None:
    snapshot = _snapshot()
    payload = json.loads(snapshot.canonical_json())
    assert payload["fund_code"] == "123456"
    assert payload["primary_state"] == "watch"
    assert snapshot.checksum() == snapshot.checksum()
    assert snapshot.canonical_json().isascii()
    assert "portfolio_weight" not in payload

    report = HeldFundBriefReport(
        snapshot=snapshot,
        owner_overlay={
            "position_present": True,
            "portfolio_weight": "0.125",
            "observed_at": NOW,
            "observation_version": "portfolio_observation_1",
        },
    )
    report.validate()
    assert report.to_canonical_dict()["owner_overlay"]["portfolio_weight"] == "0.125"
    assert report.persisted_checksum() == snapshot.checksum()


def test_nested_public_maps_are_defensively_frozen() -> None:
    metrics = {"nested": {"matched": True}}
    relationship = _relationship(metrics=metrics)
    metrics["nested"]["matched"] = False
    assert relationship.to_canonical_dict()["metrics"] == {"nested": {"matched": True}}
    with pytest.raises(TypeError):
        relationship.metrics["new_key"] = True


def test_mapping_proxy_backing_maps_are_defensively_copied_at_every_depth() -> None:
    nested_backing = {"matched": True}
    metrics_backing = {"nested": MappingProxyType(nested_backing)}
    relationship = _relationship(metrics=MappingProxyType(metrics_backing))
    snapshot = _snapshot(relationships=(relationship,))
    before = snapshot.canonical_json(), snapshot.checksum()

    nested_backing["matched"] = False
    metrics_backing["new_key"] = "changed"
    assert (snapshot.canonical_json(), snapshot.checksum()) == before

    overlay_backing = {
        "position_present": True,
        "portfolio_weight": "0.125",
        "observed_at": NOW,
        "observation_version": "portfolio_observation_1",
    }
    report = HeldFundBriefReport(
        snapshot=snapshot,
        owner_overlay=MappingProxyType(overlay_backing),
    )
    rendered = report.to_canonical_dict()
    overlay_backing["portfolio_weight"] = "0.875"
    assert report.to_canonical_dict() == rendered


def test_dynamic_public_trees_reject_int_enum_dataclass_and_custom_canonical_object() -> None:
    class RogueEnum(str, Enum):
        VALUE = "rogue"

    @dataclass(frozen=True)
    class RogueRecord:
        value: str

    class RogueCanonical:
        def to_canonical_dict(self) -> dict:
            return {"value": "rogue"}

    invalid_values = (1, RogueEnum.VALUE, RogueRecord("rogue"), RogueCanonical())
    for invalid in invalid_values:
        with pytest.raises(ValueError, match="unsupported"):
            replace(_relationship(), metrics={"value": invalid}).validate()

    replace(_relationship(), metrics={"matched": True}).validate()


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(facts=(replace(_fact(), value=Decimal("73129.17")),)),
        _snapshot(
            relationships=(
                replace(_relationship(), metrics={"nested": {"value": Decimal("73129.17")}}),
            )
        ),
        _snapshot(
            interpretations=(
                replace(
                    _interpretation(),
                    state_inputs={"nested": {"value": Decimal("73129.17")}},
                ),
            )
        ),
        _snapshot(coverage=replace(_coverage(), known_percent=Decimal("73129.17"))),
    ),
)
def test_persisted_snapshot_rejects_decimal_at_every_nested_path(
    snapshot: BriefSnapshot,
) -> None:
    with pytest.raises(ValueError, match="Decimal"):
        snapshot.canonical_json()


@pytest.mark.parametrize(
    "snapshot",
    (
        _snapshot(facts=(replace(_fact(), value=1.25),)),
        _snapshot(relationships=(replace(_relationship(), metrics={"nested": {"ratio": 0.5}}),)),
        _snapshot(interpretations=(replace(_interpretation(), state_inputs={"confidence": 0.5}),)),
    ),
)
def test_persisted_snapshot_rejects_float(snapshot: BriefSnapshot) -> None:
    with pytest.raises(ValueError, match="float"):
        snapshot.canonical_json()


@pytest.mark.parametrize(
    "private_key",
    (
        "proposed_amount",
        "shares",
        "purchase_cost",
        "observed_profit",
        "monthly_income",
        "debt_value",
        "emergency_reserve",
        "total_asset",
        "asset",
        "assets",
        "total_assets",
        "liquid_assets",
        "financial_assets",
        "loss_budget",
        "access_token",
        "api_credential",
        "ciphertext",
        "nonce",
        "private_path",
        "position_value",
        "current_value",
        "local_path",
        "managed_path",
        "response_body",
        "raw_body",
        "purchase_lots",
        "current_market_value",
        "position_market_value",
        "portfolio_weight",
        "owner_weight",
    ),
)
def test_persisted_snapshot_rejects_private_mapping_paths(private_key: str) -> None:
    snapshot = _snapshot(
        interpretations=(
            replace(_interpretation(), state_inputs={"nested": {private_key: "redacted"}}),
        )
    )
    with pytest.raises(ValueError, match="private path"):
        snapshot.canonical_json()


@pytest.mark.parametrize("public_key", ("asset_class", "candidate_asset_coverage"))
def test_public_asset_paths_are_not_false_positive_private_paths(public_key: str) -> None:
    snapshot = _snapshot(
        interpretations=(replace(_interpretation(), state_inputs={public_key: "public_value"}),)
    )
    snapshot.validate()


@pytest.mark.parametrize(
    "url",
    (
        "https://example.test/fund/123456?token=redacted",
        "https://example.test:443/fund/123456",
        "https://EXAMPLE.test/fund/123456",
        "HTTPS://example.test/fund/123456",
        "https://ex\N{LATIN SMALL LETTER A WITH ACUTE}mple.test/fund/123456",
        "https://user@example.test/fund/123456",
        "https://example.test/fund/123456#fragment",
    ),
)
def test_canonical_public_urls_reject_ambiguous_or_secret_bearing_forms(url: str) -> None:
    with pytest.raises(ValueError, match="canonical public HTTPS URL"):
        replace(_fact(), canonical_url=url).validate()


def test_canonical_public_url_allows_non_ascii_path_with_ascii_host() -> None:
    replace(
        _fact(),
        canonical_url=(
            "https://example.test/\N{CJK UNIFIED IDEOGRAPH-516C}\N{CJK UNIFIED IDEOGRAPH-544A}"
        ),
    ).validate()


@pytest.mark.parametrize(
    "source_tier",
    (SourceTier.TIER_2, SourceTier.PRIVATE_OBSERVATION, SourceTier.USER_PROVIDED),
)
def test_official_events_require_exact_tier_1(source_tier: SourceTier) -> None:
    with pytest.raises(ValueError, match="TIER_1"):
        replace(_event(), source_tier=source_tier).validate()


def test_records_reject_subclasses_unknown_state_duplicates_and_unbounded_text() -> None:
    class FactSubclass(BriefFact):
        pass

    with pytest.raises(ValueError):
        FactSubclass(**vars(_fact())).validate()

    fact = _fact()
    object.__setattr__(fact, "unexpected", "public")
    with pytest.raises(ValueError, match="unexpected instance state"):
        fact.validate()

    with pytest.raises(ValueError, match="duplicates"):
        replace(_snapshot(), action_ids=("fact_research", "fact_research")).validate()
    with pytest.raises(ValueError, match="too long"):
        replace(_fact(), publisher="x" * 4097).validate()


def test_records_reject_non_utc_times_and_invalid_owner_overlay_keys() -> None:
    non_utc = NOW.astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(ValueError, match="UTC"):
        replace(_fact(), retrieved_at=non_utc).validate()
    with pytest.raises(ValueError, match="unknown owner overlay"):
        HeldFundBriefReport(snapshot=_snapshot(), owner_overlay={"shares": "1"}).validate()


@pytest.mark.parametrize("field_name", ("request_run_id", "decision_snapshot_id"))
@pytest.mark.parametrize("invalid", (0, -1, True, False))
def test_snapshot_rejects_non_positive_or_boolean_database_ids(
    field_name: str,
    invalid: object,
) -> None:
    with pytest.raises(ValueError, match="positive exact integer"):
        replace(_snapshot(), **{field_name: invalid}).validate()


def test_snapshot_rejects_subclasses() -> None:
    class SnapshotSubclass(BriefSnapshot):
        pass

    with pytest.raises(ValueError, match="exact BriefSnapshot"):
        SnapshotSubclass(**vars(_snapshot())).validate()


def test_snapshot_rejects_duplicate_nested_ids_and_cross_record_mismatch() -> None:
    with pytest.raises(ValueError, match="fact ids"):
        replace(_snapshot(), facts=(_fact(), _fact())).validate()
    with pytest.raises(ValueError, match="event ids"):
        replace(_snapshot(), official_events=(_event(), _event())).validate()
    with pytest.raises(ValueError, match="relationship ids"):
        replace(_snapshot(), relationships=(_relationship(), _relationship())).validate()
    with pytest.raises(ValueError, match="interpretation action ids"):
        replace(_snapshot(), interpretations=(_interpretation(), _interpretation())).validate()
    with pytest.raises(ValueError, match="primary"):
        replace(_snapshot(), primary_state=BriefState.NO_ADD).validate()


@pytest.mark.parametrize(
    "action_ids",
    (
        ("continue_holding", "fact_research"),
        ("fact_research",),
        ("fact_research", "buy_or_add"),
        ("fact_research", "switch_reduce"),
        ("fact_research", "switch_buy"),
    ),
)
def test_snapshot_rejects_noncanonical_action_shapes(action_ids: tuple) -> None:
    with pytest.raises(ValueError, match="canonical action shape"):
        replace(_snapshot(), action_ids=action_ids).validate()


def test_switch_requires_both_exact_interpretations() -> None:
    switch_reduce = replace(
        _interpretation(),
        action_id="switch_reduce",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
    )
    switch_buy = replace(
        _interpretation(),
        action_id="switch_buy",
        state=BriefState.ABSTAIN,
        blocking_codes=("phase_b_blocked",),
    )
    switch_event = replace(
        _event(),
        affected_action_ids=("fact_research", "switch_buy"),
    )
    switch = _snapshot(
        action_ids=("fact_research", "switch_reduce", "switch_buy"),
        official_events=(switch_event,),
        interpretations=(switch_reduce, switch_buy),
        primary_state=BriefState.NO_ADD,
        action_maturity=ActionMaturity.MATURE,
        blocking_codes=("phase_b_blocked",),
    )
    switch.validate()
    with pytest.raises(ValueError, match="exactly match"):
        replace(switch, interpretations=(switch_reduce,)).validate()


def test_snapshot_requires_unambiguous_resolved_evidence_references() -> None:
    invalid_snapshots = (
        _snapshot(
            interpretations=(
                replace(_interpretation(), supporting_evidence_ids=("missing_evidence",)),
            )
        ),
        _snapshot(relationships=(replace(_relationship(), evidence_ids=("missing_evidence",)),)),
        _snapshot(coverage=replace(_coverage(), evidence_ids=("missing_evidence",))),
        _snapshot(official_events=(replace(_event(), affected_action_ids=("full_exit",)),)),
        _snapshot(
            source_lineage_ids=("eastmoney_nav_123456", "official_announcement_1"),
        ),
        _snapshot(
            facts=(
                _fact(),
                replace(_manager_fact(), fact_id="manager_change_1"),
            )
        ),
    )
    for snapshot in invalid_snapshots:
        with pytest.raises(ValueError, match="evidence|action|lineage|namespace"):
            snapshot.validate()


def test_interpretation_may_cite_coverage_as_noncolliding_evidence() -> None:
    snapshot = _snapshot(
        interpretations=(
            replace(
                _interpretation(),
                supporting_evidence_ids=("portfolio_relationship_coverage",),
            ),
        )
    )
    snapshot.validate()


def test_switch_top_level_cannot_hide_action_gaps_or_fact_conflicts() -> None:
    conflicted_fact = replace(_fact(), conflict_ids=("identity_conflict",))
    switch_reduce = replace(
        _interpretation(),
        action_id="switch_reduce",
        state=BriefState.REDUCE_OR_EXIT_REVIEW,
    )
    switch_buy = replace(
        _interpretation(),
        action_id="switch_buy",
        state=BriefState.ABSTAIN,
        blocking_codes=("phase_b_blocked",),
        missing_fields=("phase_b",),
    )
    switch = _snapshot(
        action_ids=("fact_research", "switch_reduce", "switch_buy"),
        facts=(conflicted_fact, _manager_fact()),
        official_events=(
            replace(
                _event(),
                affected_action_ids=("fact_research", "switch_buy"),
            ),
        ),
        interpretations=(switch_reduce, switch_buy),
        primary_state=BriefState.NO_ADD,
        action_maturity=ActionMaturity.MATURE,
        blocking_codes=("phase_b_blocked",),
        missing_fields=("owner_confirmed_thesis", "phase_b", "industry_exposure"),
        conflicts=("identity_conflict",),
    )
    switch.validate()

    hidden = (
        replace(switch, blocking_codes=()),
        replace(
            switch,
            missing_fields=("owner_confirmed_thesis", "industry_exposure"),
        ),
        replace(switch, missing_fields=("owner_confirmed_thesis", "phase_b")),
        replace(switch, conflicts=()),
    )
    for snapshot in hidden:
        with pytest.raises(ValueError, match="must include"):
            snapshot.validate()


def test_action_interpretation_never_allows_exact_amount() -> None:
    with pytest.raises(ValueError, match="exact amount"):
        replace(_interpretation(), exact_amount_available=True).validate()


def test_policy_v1_is_exact_canonical_and_pinned() -> None:
    policy = HeldFundBriefPolicyV1()
    assert RAPID_NAV_MAX_PAGES == 6
    assert DEEP_NAV_MAX_PAGES == 50
    assert MIN_CORRELATION_SAMPLES == 60
    assert MAX_OFFICIAL_EVENTS == 20
    assert MAX_FACTS == 128
    assert MAX_RELATIONSHIPS == 128
    assert policy.exact_amount_available is False
    assert policy.checksum() == HELD_FUND_BRIEF_POLICY_V1_GOLDEN_CHECKSUM
    assert policy.canonical_json().isascii()
    assert json.loads(policy.canonical_json())["state_precedence"][0] == (
        "phase_b_hard_block_no_add"
    )

    class PolicySubclass(HeldFundBriefPolicyV1):
        pass

    with pytest.raises(ValueError, match="subclasses"):
        PolicySubclass().validate()
    with pytest.raises(ValueError, match="canonical"):
        replace(policy, rapid_nav_max_pages=7).validate()
