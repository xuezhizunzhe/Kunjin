from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Tuple

from kunjin.decision.models import (
    SourceFieldPolicy,
    SourcePolicy,
    SupplementationRequest,
    canonical_json_bytes,
)

SOURCE_IDS = (
    "eastmoney_f10",
    "eastmoney_nav",
    "eastmoney_market",
    "fund_manager_official_documents",
    "yangjibao_portfolio_observation",
)


def _supplementation(
    field_id: str,
    *,
    location: str,
    accepted_input: Tuple[str, ...] = ("URL", "PDF", "screenshot", "field"),
    freshness: str,
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


_SOURCES = (
    SourcePolicy(
        source_id="eastmoney_f10",
        source_kind="structured_public_tier_2",
        scope="public fund profile, manager, fees, holdings, and industry disclosures",
        fields=(
            SourceFieldPolicy(
                "identity_active_status",
                "tier_2",
                7 * 24 * 60 * 60,
                "fund code, name, share class, and active status",
                ("fund_manager_official_documents",),
                _supplementation(
                    "identity_active_status",
                    location="fund manager product page or current prospectus",
                    freshness="published or checked within 7 days",
                ),
            ),
            SourceFieldPolicy(
                "current_manager_team",
                "tier_2",
                7 * 24 * 60 * 60,
                "current manager or team and tenure start",
                ("fund_manager_official_documents",),
                _supplementation(
                    "current_manager_team",
                    location="fund manager product page or manager-change announcement",
                    freshness="published or checked within 7 days",
                ),
            ),
            SourceFieldPolicy(
                "fees_share_class_relationship",
                "tier_2",
                7 * 24 * 60 * 60,
                "published fees and share-class relationship",
                ("fund_manager_official_documents",),
                _supplementation(
                    "fees_share_class_relationship",
                    location="official fee schedule or current purchase channel page",
                    freshness="effective schedule; channel terms within 7 days",
                ),
            ),
            SourceFieldPolicy(
                "holdings_industries",
                "tier_2",
                120 * 24 * 60 * 60,
                "latest disclosed holdings and industries with disclosure period",
                ("fund_manager_official_documents",),
                _supplementation(
                    "holdings_industries",
                    location="latest official quarterly, semiannual, or annual report",
                    freshness="latest report due under the disclosure calendar",
                ),
                dated_history_fallback_seconds=540 * 24 * 60 * 60,
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
                "tier_2",
                4 * 24 * 60 * 60,
                "formal NAV with valuation and publication date",
                ("fund_manager_official_documents",),
                _supplementation(
                    "formal_nav",
                    location="official product NAV page or formal NAV announcement",
                    freshness="latest expected publication day for the fund class",
                ),
                dated_history_fallback_seconds=5 * 365 * 24 * 60 * 60,
            ),
            SourceFieldPolicy(
                "adjusted_return_series",
                "tier_2",
                4 * 24 * 60 * 60,
                "continuous cumulative-NAV or total-return series",
                ("fund_manager_official_documents",),
                _supplementation(
                    "adjusted_return_series",
                    location="official NAV history export or validated structured series",
                    accepted_input=("URL", "PDF", "field"),
                    freshness="common end date at latest expected comparable NAV day",
                    supported="dated performance history when continuity checks pass",
                    unsupported="current timing conclusion or adjusted-return correlation",
                ),
                dated_history_fallback_seconds=5 * 365 * 24 * 60 * 60,
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
                "tier_2",
                2 * 60 * 60,
                "dated market, sector, valuation, flow, and crowding observations",
                (),
                _supplementation(
                    "market_context",
                    location="dated exchange, index-provider, or market-data page",
                    freshness="within the resolved query window; at most 2 hours for current",
                    supported="fund facts and portfolio structure analysis",
                    unsupported="current market interpretation",
                ),
                dated_history_fallback_seconds=365 * 24 * 60 * 60,
            ),
        ),
    ),
    SourcePolicy(
        source_id="fund_manager_official_documents",
        source_kind="official_public_tier_1",
        scope="official product documents, reports, fee schedules, and announcements",
        fields=(
            SourceFieldPolicy(
                "identity_active_status",
                "tier_1",
                7 * 24 * 60 * 60,
                "official identity, exact share class, and product status",
                ("eastmoney_f10",),
                _supplementation(
                    "identity_active_status",
                    location="official prospectus, product page, or status announcement",
                    freshness="published or checked within 7 days",
                ),
            ),
            SourceFieldPolicy(
                "current_manager_team",
                "tier_1",
                7 * 24 * 60 * 60,
                "official manager or team and effective date",
                ("eastmoney_f10",),
                _supplementation(
                    "current_manager_team",
                    location="official product page or manager-change announcement",
                    freshness="published or checked within 7 days",
                ),
            ),
            SourceFieldPolicy(
                "fees_share_class_relationship",
                "tier_1",
                365 * 24 * 60 * 60,
                "official effective fee schedule and share-class terms",
                ("eastmoney_f10",),
                _supplementation(
                    "fees_share_class_relationship",
                    location="official prospectus, fee schedule, or product document",
                    freshness="current effective period with newer-announcement check",
                ),
            ),
            SourceFieldPolicy(
                "holdings_industries",
                "tier_1",
                120 * 24 * 60 * 60,
                "official statutory holdings and industry disclosure",
                ("eastmoney_f10",),
                _supplementation(
                    "holdings_industries",
                    location="latest official periodic report",
                    freshness="latest report due under the disclosure calendar",
                ),
                dated_history_fallback_seconds=540 * 24 * 60 * 60,
            ),
            SourceFieldPolicy(
                "fund_manager_product_announcement",
                "tier_1",
                7 * 24 * 60 * 60,
                "official announcement with correction and retraction status",
                (),
                _supplementation(
                    "fund_manager_product_announcement",
                    location="official fund manager announcements or regulator disclosure",
                    freshness="inside query window with correction and retraction check",
                    supported="other independently evidenced facts",
                    unsupported="action affected by the unresolved announcement gap",
                ),
                dated_history_fallback_seconds=5 * 365 * 24 * 60 * 60,
            ),
            SourceFieldPolicy(
                "transaction_availability_limits_cutoff",
                "tier_1",
                2 * 60 * 60,
                "current subscription, redemption, limits, and cutoff terms",
                ("yangjibao_portfolio_observation",),
                _supplementation(
                    "transaction_availability_limits_cutoff",
                    location="official product page or validated private channel screenshot",
                    freshness="within 2 hours for current or same trading day otherwise",
                    supported="non-executable product research",
                    unsupported="executable buy or redeem conclusion and exact amount",
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
                "private_observation",
                24 * 60 * 60,
                "observed holding ratios and observation time without transaction authority",
                (),
                _supplementation(
                    "personal_position_observation",
                    location="local private portfolio import workflow",
                    accepted_input=("field",),
                    freshness="same request for exact action; otherwise label observation time",
                    supported="labeled portfolio ratios and structural diagnosis",
                    unsupported="exact action amount or transaction confirmation",
                ),
                dated_history_fallback_seconds=30 * 24 * 60 * 60,
            ),
            SourceFieldPolicy(
                "transaction_channel_observation",
                "private_observation",
                2 * 60 * 60,
                "observed channel terms that require independent validation",
                ("fund_manager_official_documents",),
                _supplementation(
                    "transaction_channel_observation",
                    location="local private screenshot import workflow",
                    accepted_input=("screenshot", "field"),
                    freshness="within 2 hours for current or same trading day otherwise",
                    supported="attributed channel observation",
                    unsupported="tier_1 transaction confirmation or exact amount authorization",
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
        if type(self) is not SourceRegistryV1:
            raise ValueError("source registry V1 subclasses are not accepted")
        if type(self.version) is not str or self.version != "1":
            raise ValueError("source registry V1 version must be '1'")
        if type(self.sources) is not tuple or self.sources != _SOURCES:
            raise ValueError("source registry V1 sources must be canonical")
        source_ids = []
        identities = []
        for source in self.sources:
            if type(source) is not SourcePolicy:
                raise ValueError("registry sources must contain exact SourcePolicy records")
            source.validate()
            source_ids.append(source.source_id)
            identities.extend((source.source_id, field.field_id) for field in source.fields)
        if tuple(source_ids) != SOURCE_IDS:
            raise ValueError("source registry V1 source ids must be canonical")
        if len(identities) != len(set(identities)):
            raise ValueError("source and field identities must be unique")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "sources": [source.to_canonical_dict() for source in self.sources],
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_dict())

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()
