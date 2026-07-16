import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kunjin.decision.models import (
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ConclusionEvidence,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    FreshnessContext,
    FreshnessKind,
    RequestFieldResolution,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceFieldRef,
    SourceFieldState,
    SourceTier,
    StoredSourceAttempt,
    WorkflowLevel,
    canonical_json_bytes,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SOURCE_IDS, SourceRegistryV1

UTC_NOW = datetime(2026, 7, 16, 6, 0, tzinfo=timezone.utc)


def _field(registry: SourceRegistryV1, source_id: str, field_id: str):
    return next(
        field
        for source in registry.sources
        if source.source_id == source_id
        for field in source.fields
        if field.field_id == field_id
    )


def _route(evidence: tuple = ()) -> DecisionRoute:
    return DecisionRoute(
        request_id="0123456789abcdef0123456789abcdef",
        mode=RequestMode.RAPID,
        workflow_level=WorkflowLevel.RAPID_EVIDENCE,
        actions=(
            ActionRoute(
                action_id="fact_research",
                action=ActionKind.FACT_RESEARCH,
                risk_effect=RiskEffect.INFORMATION,
                required_gates=(),
                blocking_codes=(),
                research_available=True,
                exact_amount_available=False,
                minimum_state="research_only",
                action_maturity=ActionMaturity.MATURE,
            ),
        ),
        conclusion_evidence=evidence,
        opposing_evidence=(),
        missing_fields=(),
        policy_version="1",
        policy_checksum="a" * 64,
        registry_version="1",
        registry_checksum="b" * 64,
    )


def _complete_current_tier1(**overrides) -> ConclusionEvidence:
    values = {
        "source_tier": SourceTier.TIER_1,
        "publishers": ("example_fund_manager",),
        "source_ids": ("fund_manager_official_documents",),
        "publication_times": (UTC_NOW - timedelta(hours=1),),
        "market_as_of": None,
        "report_as_of": None,
        "retrieved_at": UTC_NOW,
        "independent_lineage_count": 1,
        "lineage_ids": ("official_document_1",),
        "completeness": EvidenceCompleteness.COMPLETE,
        "coverage_percent": Decimal("100"),
        "freshness": EvidenceFreshness.CURRENT,
        "conflicts": (),
        "inferred": False,
        "missing_critical_fields": (),
    }
    values.update(overrides)
    return ConclusionEvidence(**values)


def test_phase0_enums_are_exact() -> None:
    assert [item.value for item in RequestMode] == ["rapid", "deep"]
    assert [item.value for item in ActionKind] == [
        "fact_research",
        "continue_holding",
        "reduce_to_cash",
        "full_exit",
        "buy_or_add",
        "switch_funds",
    ]
    assert [item.value for item in RiskEffect] == [
        "information",
        "risk_maintaining",
        "risk_reducing",
        "risk_increasing",
    ]
    assert [item.value for item in SourceFieldState] == [
        "not_checked",
        "healthy",
        "degraded",
        "cooldown",
        "unavailable",
        "unsupported",
    ]
    assert [item.value for item in RequestFieldResolution] == [
        "usable",
        "partial",
        "manual_supplement_required",
    ]
    assert [item.value for item in WorkflowLevel] == [
        "rapid_evidence",
        "decision_evidence",
    ]
    assert [item.value for item in ActionMaturity] == [
        "mature",
        "experimental_shadow",
    ]
    assert [item.value for item in RequestTerminalStatus] == [
        "complete",
        "partial",
        "failed",
        "cancelled",
        "expired",
    ]
    assert [item.value for item in SourceAttemptOutcome] == [
        "success",
        "transient_failure",
        "unavailable",
        "unsupported",
        "cancelled",
        "expired",
        "cache_hit",
        "skipped_cooldown",
    ]
    assert [item.value for item in SourceTier] == [
        "tier_1",
        "tier_2",
        "private_observation",
        "user_provided",
    ]
    assert [item.value for item in EvidenceCompleteness] == [
        "complete",
        "partial",
        "insufficient",
    ]
    assert [item.value for item in EvidenceFreshness] == [
        "current",
        "dated_history",
        "stale",
        "unknown",
    ]
    assert [item.value for item in FreshnessKind] == [
        "fixed_age",
        "formal_nav_calendar",
        "effective_period",
        "disclosure_calendar",
        "query_window",
        "same_trading_day",
        "same_request",
    ]
    assert ActionKind.SWITCH_FUNDS.value == "switch_funds"


def test_policy_and_registry_have_pinned_canonical_bytes() -> None:
    policy = EvidencePolicyV1()
    registry = SourceRegistryV1()
    assert policy.checksum() == "bafaf188c31ce4912485856369397c423dece2dcc48ac3e19273845763dd1428"
    assert registry.checksum() == "2aa479937c46d94e8b8dbc11695900bbebe9aa08765b3e09792d9428724085af"
    assert len(policy.canonical_json()) == 5_862
    assert len(registry.canonical_json()) == 18_961
    for item in (policy, registry):
        canonical = item.canonical_json()
        assert (
            json.dumps(
                json.loads(canonical.decode("ascii")),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            == canonical
        )
        assert re.fullmatch(r"[0-9a-f]{64}", item.checksum())
        lowered = canonical.decode("ascii").casefold()
        for forbidden in ("ciphertext", "nonce", "access_token", "private_value"):
            assert forbidden not in lowered


def test_source_registry_references_are_finite_resolvable_and_complete() -> None:
    registry = SourceRegistryV1()
    assert tuple(source.source_id for source in registry.sources) == SOURCE_IDS
    assert 1 <= len(registry.sources) <= 8
    identities = {
        SourceFieldRef(source.source_id, field.field_id)
        for source in registry.sources
        for field in source.fields
    }
    assert len(identities) == sum(len(source.fields) for source in registry.sources)
    assert all(
        alternative in identities
        for source in registry.sources
        for field in source.fields
        for alternative in field.acceptable_alternatives
    )
    assert all(
        field.supplementation.accepted_input
        for source in registry.sources
        for field in source.fields
    )
    assert SourceFieldRef("eastmoney_f10", "fund_manager_product_announcement") in identities
    assert SourceFieldRef("fund_manager_official_documents", "formal_nav") in identities
    assert SourceFieldRef(
        "fund_manager_official_documents", "adjusted_return_series"
    ) in identities
    assert _field(
        registry,
        "fund_manager_official_documents",
        "transaction_availability_limits_cutoff",
    ).acceptable_alternatives == (
        SourceFieldRef("yangjibao_portfolio_observation", "transaction_channel_observation"),
    )


def test_dynamic_freshness_rules_fail_closed_without_exact_context() -> None:
    registry = SourceRegistryV1()
    cases = (
        ("eastmoney_nav", "formal_nav"),
        ("fund_manager_official_documents", "fees_share_class_relationship"),
        ("eastmoney_f10", "holdings_industries"),
        ("eastmoney_market", "market_context"),
        (
            "fund_manager_official_documents",
            "transaction_availability_limits_cutoff",
        ),
        ("yangjibao_portfolio_observation", "personal_position_observation"),
    )
    empty = FreshnessContext(now=UTC_NOW)
    for source_id, field_id in cases:
        field = _field(registry, source_id, field_id)
        assert not field.is_current(UTC_NOW - timedelta(minutes=1), empty)


def test_each_dynamic_freshness_rule_accepts_only_matching_context() -> None:
    registry = SourceRegistryV1()
    data_as_of = UTC_NOW - timedelta(hours=1)
    nav = _field(registry, "eastmoney_nav", "formal_nav")
    assert nav.is_current(
        data_as_of,
        FreshnessContext(now=UTC_NOW, latest_expected_data_as_of=data_as_of),
    )
    fees = _field(
        registry,
        "fund_manager_official_documents",
        "fees_share_class_relationship",
    )
    assert fees.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            effective_period_start=UTC_NOW - timedelta(days=365),
            effective_period_end=None,
            effective_period_open_ended=True,
            newer_announcement_check_complete=True,
            newer_announcement_found=False,
            newer_announcement_checked_at=UTC_NOW,
        ),
    )
    holdings = _field(registry, "eastmoney_f10", "holdings_industries")
    assert holdings.is_current(
        data_as_of,
        FreshnessContext(now=UTC_NOW, next_disclosure_due_at=UTC_NOW + timedelta(days=1)),
    )
    market = _field(registry, "eastmoney_market", "market_context")
    assert market.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            query_window_start=UTC_NOW - timedelta(days=1),
            query_window_end=UTC_NOW,
        ),
    )
    announcement = _field(
        registry,
        "fund_manager_official_documents",
        "fund_manager_product_announcement",
    )
    query_context = FreshnessContext(
        now=UTC_NOW,
        query_window_start=UTC_NOW - timedelta(days=1),
        query_window_end=UTC_NOW,
    )
    assert not announcement.is_current(data_as_of, query_context)
    assert announcement.is_current(
        data_as_of,
        replace(
            query_context,
            correction_retraction_check_complete=True,
            correction_retraction_checked_at=UTC_NOW,
        ),
    )
    transaction = _field(
        registry,
        "fund_manager_official_documents",
        "transaction_availability_limits_cutoff",
    )
    assert transaction.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            trading_day=date(2026, 7, 16),
            data_trading_day=date(2026, 7, 16),
        ),
    )
    position = _field(
        registry,
        "yangjibao_portfolio_observation",
        "personal_position_observation",
    )
    assert position.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            request_id="0123456789abcdef0123456789abcdef",
            data_request_id="0123456789abcdef0123456789abcdef",
        ),
    )
    assert not position.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            request_id="0123456789abcdef0123456789abcdef",
            data_request_id="abcdef0123456789abcdef0123456789",
        ),
    )


def test_fixed_freshness_with_announcement_invalidation_fails_closed() -> None:
    identity = _field(SourceRegistryV1(), "eastmoney_f10", "identity_active_status")
    data_as_of = UTC_NOW - timedelta(days=1)
    assert not identity.is_current(data_as_of, FreshnessContext(now=UTC_NOW))
    assert identity.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            newer_announcement_check_complete=True,
            newer_announcement_found=False,
            newer_announcement_checked_at=UTC_NOW,
        ),
    )
    assert not identity.is_current(
        data_as_of,
        FreshnessContext(
            now=UTC_NOW,
            newer_announcement_check_complete=True,
            newer_announcement_found=True,
            newer_announcement_checked_at=UTC_NOW,
        ),
    )


def test_dated_history_fallback_never_upgrades_to_current() -> None:
    market = _field(SourceRegistryV1(), "eastmoney_market", "market_context")
    data_as_of = UTC_NOW - timedelta(days=10)
    context = FreshnessContext(
        now=UTC_NOW,
        query_window_start=UTC_NOW - timedelta(hours=2),
        query_window_end=UTC_NOW,
    )
    assert not market.is_current(data_as_of, context)
    assert market.is_usable(data_as_of, context)


def test_effective_period_requires_explicit_open_or_closed_mode() -> None:
    fees = _field(
        SourceRegistryV1(),
        "fund_manager_official_documents",
        "fees_share_class_relationship",
    )
    incomplete = FreshnessContext(
        now=UTC_NOW,
        effective_period_start=UTC_NOW - timedelta(days=1),
        newer_announcement_check_complete=True,
        newer_announcement_found=False,
        newer_announcement_checked_at=UTC_NOW,
    )
    with pytest.raises(ValueError, match="end mode"):
        fees.is_current(UTC_NOW - timedelta(hours=1), incomplete)


def test_policy_encodes_structured_d2_and_post_trade_fail_closed_rules() -> None:
    policy = EvidencePolicyV1()
    policy.validate()
    assert policy.d2.classification_coverage.formula_id == "classification_coverage"
    assert policy.d2.classification_coverage.minimum_percent == Decimal("90")
    assert policy.d2.sector_candidate_asset_coverage.minimum_percent == Decimal("80")
    assert policy.d2.broad_index_candidate_asset_coverage.minimum_percent == Decimal("90")
    assert policy.d2.transaction_after_lookthrough_coverage.minimum_percent == Decimal("70")
    assert policy.d2.cash_excluded_from_denominators
    assert policy.d2.derivatives_leverage_shorts_residual_reported_separately
    assert policy.d2.unresolved_exposure_cannot_increase_coverage
    assert policy.d2.fund_of_funds_lookthrough_requires_verified_inputs
    assert policy.d2.test_every_applicable_limit
    assert policy.d2.allocate_all_unknown_to_each_limit
    assert policy.post_trade.cap_scope == "all_linked_accounts_current_and_pending"
    assert policy.post_trade.requires_unlinked_account_affirmation
    assert policy.post_trade.requires_material_holding_completeness
    assert policy.post_trade.valuation_date_tolerance_days == 0
    assert policy.post_trade.block_exact_amount_on_failure


def test_policy_and_registry_tampering_fail_closed() -> None:
    policy = EvidencePolicyV1()
    tampered_gate = replace(
        policy.d2.classification_coverage,
        minimum_percent=Decimal("89"),
    )
    tampered_d2 = replace(policy.d2, classification_coverage=tampered_gate)
    with pytest.raises(ValueError):
        replace(policy, d2=tampered_d2).validate()

    registry = SourceRegistryV1()
    source = registry.sources[0]
    field = source.fields[0]
    missing_reference = SourceFieldRef("eastmoney_market", "undeclared_field")
    tampered_field = replace(field, acceptable_alternatives=(missing_reference,))
    tampered_source = replace(source, fields=(tampered_field, *source.fields[1:]))
    with pytest.raises(ValueError):
        replace(registry, sources=(tampered_source, *registry.sources[1:])).validate()


@pytest.mark.parametrize(
    "request_id",
    ("ABCDEF0123456789ABCDEF0123456789", "short", "g" * 32),
)
def test_request_id_is_exact_lowercase_uuid_hex(request_id: str) -> None:
    route = _route()
    object.__setattr__(route, "request_id", request_id)
    with pytest.raises(ValueError, match="request id"):
        route.validate()


@pytest.mark.parametrize(
    "subject_key,force_reason",
    (
        ("account:123456", "manual retry"),
        ("fund:123456", "access_token=secret"),
        ("fund:123456", "account balance 1000 CNY"),
        ("fund:123456", "Bearer aaa.bbb.ccc"),
    ),
)
def test_source_attempt_rejects_non_fund_subject_and_private_force_reason(
    subject_key: str,
    force_reason: str,
) -> None:
    attempt = SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key=subject_key,
        attempt_number=1,
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        started_at=UTC_NOW - timedelta(seconds=1),
        finished_at=UTC_NOW,
        data_as_of=None,
        error_code="network_timeout",
        cooldown_until=UTC_NOW + timedelta(minutes=30),
        force_actor="local_owner",
        force_reason=force_reason,
        registry_version="1",
        registry_checksum="a" * 64,
        response_bytes=0,
    )
    with pytest.raises(ValueError):
        attempt.validate()


def test_source_attempt_accepts_safe_exact_identifiers() -> None:
    attempt = SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key="fund:123456",
        attempt_number=2,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=UTC_NOW - timedelta(seconds=1),
        finished_at=UTC_NOW,
        data_as_of=UTC_NOW - timedelta(minutes=1),
        error_code=None,
        cooldown_until=None,
        force_actor=None,
        force_reason=None,
        registry_version="1",
        registry_checksum="a" * 64,
        response_bytes=100,
    )
    attempt.validate()


@pytest.mark.parametrize(
    "field_name,value",
    (
        ("source_id", "Eastmoney"),
        ("field_id", "../identity"),
        ("error_code", "NETWORK_TIMEOUT"),
    ),
)
def test_source_attempt_rejects_invalid_public_identifiers(
    field_name: str,
    value: str,
) -> None:
    attempt = SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key="fund:123456",
        attempt_number=1,
        outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
        started_at=UTC_NOW - timedelta(seconds=1),
        finished_at=UTC_NOW,
        data_as_of=None,
        error_code="network_timeout",
        cooldown_until=UTC_NOW + timedelta(minutes=30),
        force_actor=None,
        force_reason=None,
        registry_version="1",
        registry_checksum="a" * 64,
        response_bytes=0,
    )
    invalid = replace(attempt, **{field_name: value})
    with pytest.raises(ValueError):
        invalid.validate()


@pytest.mark.parametrize(
    "field_name,value",
    (
        ("source_tier", "tier_1"),
        ("completeness", "complete"),
        ("freshness", "current"),
    ),
)
def test_conclusion_evidence_rejects_untyped_quality_values(
    field_name: str,
    value: str,
) -> None:
    evidence = replace(_complete_current_tier1(), **{field_name: value})
    with pytest.raises(ValueError):
        evidence.validate()


@pytest.mark.parametrize(
    "overrides",
    (
        {"publishers": ()},
        {"source_ids": ()},
        {"publication_times": ()},
        {"independent_lineage_count": 0, "lineage_ids": ()},
    ),
)
def test_current_complete_tier1_requires_identity_date_and_lineage(overrides: dict) -> None:
    evidence = _complete_current_tier1(**overrides)
    with pytest.raises(ValueError):
        evidence.validate()


def test_conclusion_evidence_is_bounded() -> None:
    evidence = _complete_current_tier1()
    route = _route((evidence,) * 129)
    with pytest.raises(ValueError, match="bounded"):
        route.validate()


def test_equal_datetime_instants_have_equal_utc_canonical_bytes() -> None:
    utc_evidence = _complete_current_tier1()
    plus_eight = timezone(timedelta(hours=8))
    local_evidence = _complete_current_tier1(
        publication_times=(
            (UTC_NOW - timedelta(hours=1)).astimezone(plus_eight),
        ),
        retrieved_at=UTC_NOW.astimezone(plus_eight),
    )
    assert canonical_json_bytes(utc_evidence) == canonical_json_bytes(local_evidence)
    assert b"+00:00" in canonical_json_bytes(utc_evidence)


def test_v1_and_nested_records_reject_injected_state() -> None:
    registry = SourceRegistryV1()
    field = registry.sources[0].fields[0]
    context = FreshnessContext(now=UTC_NOW)
    evidence = _complete_current_tier1()
    route = _route((evidence,))
    attempt = SourceAttempt(
        source_id="eastmoney_f10",
        field_id="identity_active_status",
        subject_key="fund:123456",
        attempt_number=1,
        outcome=SourceAttemptOutcome.SUCCESS,
        started_at=UTC_NOW - timedelta(seconds=1),
        finished_at=UTC_NOW,
        data_as_of=UTC_NOW - timedelta(minutes=1),
        error_code=None,
        cooldown_until=None,
        force_actor=None,
        force_reason=None,
        registry_version="1",
        registry_checksum="a" * 64,
        response_bytes=100,
    )
    records = (
        replace(EvidencePolicyV1()),
        replace(EvidencePolicyV1().requirements[0]),
        replace(EvidencePolicyV1().d2),
        replace(EvidencePolicyV1().d2.classification_coverage),
        replace(EvidencePolicyV1().post_trade),
        replace(registry),
        replace(registry.sources[0]),
        replace(field),
        replace(field.freshness),
        replace(field.acceptable_alternatives[0]),
        replace(field.supplementation),
        replace(context),
        replace(evidence),
        replace(route),
        replace(route.actions[0]),
        replace(attempt),
        StoredSourceAttempt(id=1, request_run_id=1, attempt=attempt),
    )
    for record in records:
        object.__setattr__(record, "unexpected", "state")
        with pytest.raises(ValueError, match="unexpected"):
            record.validate()
