from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Tuple

from kunjin.decision.models import (
    FreshnessKind,
    FreshnessRule,
    SourceFieldPolicy,
    SourceFieldRef,
    SourcePolicy,
    SourceTier,
    SupplementationRequest,
    canonical_json_bytes,
    validate_exact_dataclass_state,
)

SOURCE_IDS = (
    "eastmoney_f10",
    "eastmoney_nav",
    "eastmoney_market",
    "fund_manager_official_documents",
    "yangjibao_portfolio_observation",
)

_DAY = 24 * 60 * 60


def _ref(source_id: str, field_id: str) -> SourceFieldRef:
    return SourceFieldRef(source_id=source_id, field_id=field_id)


def _supplementation(
    field_id: str,
    *,
    location: str,
    freshness: str,
    accepted_input: Tuple[str, ...] = ("URL", "PDF", "screenshot", "field"),
    supported: str = "dated factual research with explicit missing-field label",
    unsupported: str = "current action conclusion and exact amount",
) -> SupplementationRequest:
    return SupplementationRequest(
        missing_item=field_id,
        why_required=f"{field_id} is required by EvidencePolicy V1",
        suggested_location=location,
        accepted_input=accepted_input,
        freshness_requirement=freshness,
        impact_if_missing=(
            "evidence completeness is reduced and applicable action gates remain blocked"
        ),
        supported_without_it=supported,
        unsupported_without_it=unsupported,
    )


def _fixed(days: int, *, announcement_check: bool = False) -> FreshnessRule:
    return FreshnessRule(
        kind=FreshnessKind.FIXED_AGE,
        maximum_age_seconds=days * _DAY,
        requires_newer_announcement_check=announcement_check,
    )


def _formal_series() -> FreshnessRule:
    return FreshnessRule(
        kind=FreshnessKind.FORMAL_NAV_CALENDAR,
        dated_history_fallback_seconds=5 * 365 * _DAY,
    )


def _holdings() -> FreshnessRule:
    return FreshnessRule(
        kind=FreshnessKind.DISCLOSURE_CALENDAR,
        dated_history_fallback_seconds=540 * _DAY,
    )


def _announcement() -> FreshnessRule:
    return FreshnessRule(
        kind=FreshnessKind.QUERY_WINDOW,
        maximum_age_seconds=7 * _DAY,
        dated_history_fallback_seconds=5 * 365 * _DAY,
        requires_correction_retraction_check=True,
    )


_SOURCES = (
    SourcePolicy(
        source_id="eastmoney_f10",
        source_kind="structured_public_tier_2",
        scope="public fund profile, manager, fees, holdings, and announcements",
        fields=(
            SourceFieldPolicy(
                "identity_active_status",
                SourceTier.TIER_2,
                _fixed(7, announcement_check=True),
                "fund code, name, share class, and active status",
                (_ref("fund_manager_official_documents", "identity_active_status"),),
                _supplementation(
                    "identity_active_status",
                    location="fund manager product page or current prospectus",
                    freshness="within 7 days with a completed newer-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "current_manager_team",
                SourceTier.TIER_2,
                _fixed(7, announcement_check=True),
                "current manager or team and tenure start",
                (_ref("fund_manager_official_documents", "current_manager_team"),),
                _supplementation(
                    "current_manager_team",
                    location="fund manager product page or manager-change announcement",
                    freshness="within 7 days with a completed manager-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "fees_share_class_relationship",
                SourceTier.TIER_2,
                FreshnessRule(
                    FreshnessKind.EFFECTIVE_PERIOD,
                    requires_newer_announcement_check=True,
                ),
                "published fees and share-class relationship",
                (
                    _ref(
                        "fund_manager_official_documents",
                        "fees_share_class_relationship",
                    ),
                ),
                _supplementation(
                    "fees_share_class_relationship",
                    location="official fee schedule or current purchase channel page",
                    freshness="effective period with a completed newer-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "holdings_industries",
                SourceTier.TIER_2,
                _holdings(),
                "latest disclosed holdings and industries with disclosure period",
                (_ref("fund_manager_official_documents", "holdings_industries"),),
                _supplementation(
                    "holdings_industries",
                    location="latest official quarterly, semiannual, or annual report",
                    freshness="current only before the next disclosure-calendar due time",
                ),
            ),
            SourceFieldPolicy(
                "fund_manager_product_announcement",
                SourceTier.TIER_2,
                _announcement(),
                "attributed product or manager announcements and their publication dates",
                (
                    _ref(
                        "fund_manager_official_documents",
                        "fund_manager_product_announcement",
                    ),
                ),
                _supplementation(
                    "fund_manager_product_announcement",
                    location="official manager announcement index or regulator disclosure",
                    freshness="inside query window with correction and retraction check",
                    supported="other independently evidenced facts",
                    unsupported="action affected by the unresolved announcement gap",
                ),
            ),
        ),
    ),
    SourcePolicy(
        source_id="eastmoney_nav",
        source_kind="structured_public_tier_2",
        scope="formal NAV and validated cumulative-NAV history",
        fields=(
            SourceFieldPolicy(
                "formal_nav",
                SourceTier.TIER_2,
                _formal_series(),
                "formal NAV with valuation and publication date",
                (_ref("fund_manager_official_documents", "formal_nav"),),
                _supplementation(
                    "formal_nav",
                    location="official product NAV page or formal NAV announcement",
                    freshness="latest expected publication under the applicable calendar",
                ),
            ),
            SourceFieldPolicy(
                "adjusted_return_series",
                SourceTier.TIER_2,
                _formal_series(),
                "continuous cumulative-NAV or total-return series",
                (_ref("fund_manager_official_documents", "adjusted_return_series"),),
                _supplementation(
                    "adjusted_return_series",
                    location="official NAV history export or validated structured series",
                    freshness="common end date at latest expected comparable NAV day",
                    accepted_input=("URL", "PDF", "field"),
                    supported="dated performance history when continuity checks pass",
                    unsupported="current timing conclusion or adjusted-return correlation",
                ),
            ),
        ),
    ),
    SourcePolicy(
        source_id="eastmoney_market",
        source_kind="structured_public_tier_2",
        scope="A-share market and sector context",
        fields=(
            SourceFieldPolicy(
                "market_context",
                SourceTier.TIER_2,
                FreshnessRule(
                    FreshnessKind.QUERY_WINDOW,
                    maximum_age_seconds=2 * 60 * 60,
                    dated_history_fallback_seconds=365 * _DAY,
                ),
                "dated market, sector, valuation, flow, and crowding observations",
                (),
                _supplementation(
                    "market_context",
                    location="dated exchange, index-provider, or market-data page",
                    freshness="inside query window and within 2 hours for current",
                    supported="fund facts and portfolio structure analysis",
                    unsupported="current market interpretation",
                ),
            ),
        ),
    ),
    SourcePolicy(
        source_id="fund_manager_official_documents",
        source_kind="official_public_tier_1",
        scope="official documents, reports, fee schedules, NAV, and announcements",
        fields=(
            SourceFieldPolicy(
                "identity_active_status",
                SourceTier.TIER_1,
                _fixed(7, announcement_check=True),
                "official identity, exact share class, and product status",
                (_ref("eastmoney_f10", "identity_active_status"),),
                _supplementation(
                    "identity_active_status",
                    location="official prospectus, product page, or status announcement",
                    freshness="within 7 days with a completed newer-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "current_manager_team",
                SourceTier.TIER_1,
                _fixed(7, announcement_check=True),
                "official manager or team and effective date",
                (_ref("eastmoney_f10", "current_manager_team"),),
                _supplementation(
                    "current_manager_team",
                    location="official product page or manager-change announcement",
                    freshness="within 7 days with a completed manager-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "fees_share_class_relationship",
                SourceTier.TIER_1,
                FreshnessRule(
                    FreshnessKind.EFFECTIVE_PERIOD,
                    requires_newer_announcement_check=True,
                ),
                "official effective fee schedule and share-class terms",
                (_ref("eastmoney_f10", "fees_share_class_relationship"),),
                _supplementation(
                    "fees_share_class_relationship",
                    location="official prospectus, fee schedule, or product document",
                    freshness="effective period with a completed newer-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "holdings_industries",
                SourceTier.TIER_1,
                _holdings(),
                "official statutory holdings and industry disclosure",
                (_ref("eastmoney_f10", "holdings_industries"),),
                _supplementation(
                    "holdings_industries",
                    location="latest official periodic report",
                    freshness="current only before the next disclosure-calendar due time",
                ),
            ),
            SourceFieldPolicy(
                "fund_manager_product_announcement",
                SourceTier.TIER_1,
                _announcement(),
                "official announcement with correction and retraction status",
                (_ref("eastmoney_f10", "fund_manager_product_announcement"),),
                _supplementation(
                    "fund_manager_product_announcement",
                    location="official fund manager announcements or regulator disclosure",
                    freshness="inside query window with correction and retraction check",
                    supported="other independently evidenced facts",
                    unsupported="action affected by the unresolved announcement gap",
                ),
            ),
            SourceFieldPolicy(
                "transaction_availability_limits_cutoff",
                SourceTier.TIER_1,
                FreshnessRule(
                    FreshnessKind.SAME_TRADING_DAY,
                    maximum_age_seconds=2 * 60 * 60,
                ),
                "current subscription, redemption, limits, and cutoff terms",
                (
                    _ref(
                        "yangjibao_portfolio_observation",
                        "transaction_channel_observation",
                    ),
                ),
                _supplementation(
                    "transaction_availability_limits_cutoff",
                    location="official product page or validated private channel screenshot",
                    freshness="within 2 hours and on the resolved trading day",
                    supported="non-executable product research",
                    unsupported="executable buy or redeem conclusion and exact amount",
                ),
            ),
            SourceFieldPolicy(
                "formal_nav",
                SourceTier.TIER_1,
                _formal_series(),
                "official formal NAV with valuation and publication date",
                (_ref("eastmoney_nav", "formal_nav"),),
                _supplementation(
                    "formal_nav",
                    location="official product NAV page or formal NAV announcement",
                    freshness="latest expected publication under the applicable calendar",
                ),
            ),
            SourceFieldPolicy(
                "adjusted_return_series",
                SourceTier.TIER_1,
                _formal_series(),
                "official continuous cumulative-NAV or total-return series",
                (_ref("eastmoney_nav", "adjusted_return_series"),),
                _supplementation(
                    "adjusted_return_series",
                    location="official NAV history export or product report",
                    freshness="common end date at latest expected comparable NAV day",
                    accepted_input=("URL", "PDF", "field"),
                    supported="dated performance history when continuity checks pass",
                    unsupported="current timing conclusion or adjusted-return correlation",
                ),
            ),
        ),
    ),
    SourcePolicy(
        source_id="yangjibao_portfolio_observation",
        source_kind="private_observation_not_transaction_confirmation",
        scope="read-only portfolio and channel observation for the local owner",
        fields=(
            SourceFieldPolicy(
                "personal_position_observation",
                SourceTier.PRIVATE_OBSERVATION,
                FreshnessRule(
                    FreshnessKind.SAME_REQUEST,
                    dated_history_fallback_seconds=30 * _DAY,
                ),
                "observed holding ratios and time without transaction authority",
                (),
                _supplementation(
                    "personal_position_observation",
                    location="local private portfolio import workflow",
                    accepted_input=("field",),
                    freshness="same request for exact action; otherwise dated observation",
                    supported="labeled portfolio ratios and structural diagnosis",
                    unsupported="exact action amount or transaction confirmation",
                ),
            ),
            SourceFieldPolicy(
                "transaction_channel_observation",
                SourceTier.PRIVATE_OBSERVATION,
                FreshnessRule(
                    FreshnessKind.SAME_TRADING_DAY,
                    maximum_age_seconds=2 * 60 * 60,
                ),
                "observed channel terms that require official validation",
                (
                    _ref(
                        "fund_manager_official_documents",
                        "transaction_availability_limits_cutoff",
                    ),
                ),
                _supplementation(
                    "transaction_channel_observation",
                    location="local private screenshot import workflow",
                    accepted_input=("screenshot", "field"),
                    freshness="within 2 hours and on the resolved trading day",
                    supported="attributed private channel observation",
                    unsupported="tier_1 confirmation or exact amount authorization",
                ),
            ),
        ),
    ),
)


@dataclass(frozen=True)
class SourceRegistryV1:
    version: str = "1"
    sources: Tuple[SourcePolicy, ...] = _SOURCES

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "source registry V1")
        if type(self) is not SourceRegistryV1:
            raise ValueError("source registry V1 subclasses are not accepted")
        if type(self.version) is not str or self.version != "1":
            raise ValueError("source registry V1 version must be '1'")
        if type(self.sources) is not tuple or self.sources != _SOURCES:
            raise ValueError("source registry V1 sources must be canonical")
        source_ids = []
        identities = set()
        alternatives = []
        for source in self.sources:
            if type(source) is not SourcePolicy:
                raise ValueError("registry sources must contain exact SourcePolicy records")
            source.validate()
            source_ids.append(source.source_id)
            for field in source.fields:
                identities.add(SourceFieldRef(source.source_id, field.field_id))
                alternatives.extend(field.acceptable_alternatives)
        if tuple(source_ids) != SOURCE_IDS:
            raise ValueError("source registry V1 source ids must be canonical")
        expected_count = sum(len(source.fields) for source in self.sources)
        if len(identities) != expected_count:
            raise ValueError("source and field identities must be unique")
        missing_targets = [reference for reference in alternatives if reference not in identities]
        if missing_targets:
            raise ValueError("acceptable alternatives must reference declared source fields")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "sources": [source.to_canonical_dict() for source in self.sources],
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


SOURCE_REGISTRY_V1_CHECKSUM = (
    "2aa479937c46d94e8b8dbc11695900bbebe9aa08765b3e09792d9428724085af"
)
