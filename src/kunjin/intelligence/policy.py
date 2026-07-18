from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Tuple

from kunjin.decision.models import canonical_json_bytes, validate_exact_dataclass_state

RAPID_SECONDS = 90
DEEP_SECONDS = 480
RECENT_SECONDS = 72 * 60 * 60
CURRENT_CACHE_SECONDS = 2 * 60 * 60
EXCERPT_MAX_BYTES = 2_048
EXCERPT_RETENTION_DAYS = 365
MIN_SECTORS = 20
MIN_FIELD_COVERAGE = Decimal("0.90")
POSITIVE_CHANGE = Decimal("0.50")
NEGATIVE_CHANGE = Decimal("-0.50")
POSITIVE_BREADTH = Decimal("0.60")
NEGATIVE_BREADTH = Decimal("0.40")
CROWDING_PERCENTILE = Decimal("0.90")
MARKET_CROWDING_SHARE = Decimal("0.20")
FLOW_OBSERVATIONS = 3
FLOW_WINDOW_DAYS = 5
MARKET_MAX_AGE_DAYS = 5

SUPPORT_PHRASES = ("支持", "促进", "推动", "加快", "扩大消费")
RESTRICTION_PHRASES = ("暂停", "禁止", "限制", "风险警示", "行政处罚")

INTELLIGENCE_POLICY_V1_GOLDEN_CHECKSUM = (
    "4699db062f1a1e1cc91b410b3b06fe1969aa99efe33b07e778119058f94e94e6"
)


def _source_registry() -> Tuple[Tuple[str, str, str, str, str], ...]:
    return (
        (
            "cs_com_cn",
            "tier_2",
            "https://www.cs.com.cn/xwzx/hg/",
            "disabled",
            "http_403_at_preflight",
        ),
        (
            "csrc_public_news",
            "tier_1",
            "c100028/common_list.shtml",
            "disabled",
            "stale_at_2021_preflight",
        ),
        (
            "eastmoney_market",
            "tier_2",
            "existing_push2_allowlist",
            "enabled",
            "structured_market_adapter",
        ),
        (
            "fund_manager_official_documents",
            "tier_1",
            "existing_audited_manager_domain_registry",
            "enabled",
            "authenticated_existing_registry",
        ),
        (
            "gov_cn_policy",
            "tier_1",
            "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json",
            "enabled",
            "bounded_public_json",
        ),
        (
            "stcn_fund_news",
            "tier_2",
            "https://www.stcn.com/article/list/fund.html",
            "enabled",
            "server_rendered_public_html",
        ),
    )


def _metric_rules() -> Tuple[Tuple[str, Tuple[str, ...], str, str, str], ...]:
    return (
        (
            "catalysts",
            ("authenticated_event_direction",),
            "gov_cn_policy_stcn_and_official_fund_events",
            "query_window",
            "tier_1_exact_entity_phrase_only_tier_2_context_only",
        ),
        (
            "crowding_market",
            ("industry_overheating_share",),
            "eastmoney_market",
            "maximum_age_5_calendar_days",
            "risk_at_eligible_share_gte_0_20",
        ),
        (
            "crowding_sector",
            ("sector_return_turnover_percentiles",),
            "eastmoney_market_f3_f8_same_kind_batch",
            "maximum_age_5_calendar_days",
            "risk_when_both_percentiles_gte_0_90",
        ),
        (
            "flow_market",
            ("industry_positive_flow_share_3d",),
            "eastmoney_market_f184",
            "newest_3_observations_in_5_calendar_days",
            "positive_all_gte_0_60_negative_all_lte_0_40",
        ),
        (
            "flow_sector",
            ("sector_main_flow_ratio_3d",),
            "eastmoney_market_f184",
            "newest_3_observations_in_5_calendar_days",
            "positive_all_gt_0_negative_all_lt_0",
        ),
        (
            "fundamentals_earnings",
            (),
            "unsupported",
            "unsupported",
            "insufficient_data",
        ),
        (
            "trend_breadth_market",
            ("industry_median_pct_change", "industry_aggregate_breadth"),
            "eastmoney_market_industry_batch",
            "maximum_age_5_calendar_days",
            "positive_change_gte_0_50_breadth_gte_0_60_negative_change_lte_minus_0_50_breadth_lte_0_40",
        ),
        (
            "trend_breadth_sector",
            ("sector_pct_change", "sector_breadth"),
            "eastmoney_market_same_batch",
            "maximum_age_5_calendar_days",
            "positive_change_gte_0_50_breadth_gte_0_60_negative_change_lte_minus_0_50_breadth_lte_0_40",
        ),
        (
            "valuation",
            (),
            "unsupported",
            "unsupported",
            "insufficient_data",
        ),
    )


def _privacy_fields() -> Tuple[str, ...]:
    return (
        "amount",
        "authorization_headers",
        "browser_cookies",
        "cost",
        "local_path",
        "portfolio_weight",
        "profit",
        "raw_body",
        "shares",
    )


def _market_state_rules() -> Tuple[str, ...]:
    return (
        "requires_eligible_trend_breadth_plus_two_other_dimensions",
        "requires_at_least_one_non_price_dimension",
        "offensive_requires_positive_trend_two_other_positive_no_negative_no_crowding_risk",
        "defensive_requires_negative_trend_and_at_least_two_negative_eligible_dimensions",
        "other_sufficient_combinations_are_neutral",
        "insufficient_coverage_is_insufficient_data",
    )


def _sector_state_precedence() -> Tuple[str, ...]:
    return (
        "overheating_risk",
        "improving",
        "weakening",
        "neutral",
        "insufficient_data",
    )


def _sector_state_rules() -> Tuple[str, ...]:
    return (
        "overheating_risk_has_first_precedence",
        "improving_requires_positive_trend_and_positive_fundamentals_flow_or_catalyst",
        "improving_requires_no_negative_fundamentals_flow_or_catalyst",
        "weakening_requires_negative_trend_and_negative_fundamentals_flow_or_catalyst",
        "other_sufficient_combinations_are_neutral",
        "missing_coverage_is_insufficient_data",
    )


@dataclass(frozen=True)
class IntelligencePolicyV1:
    version: str = "1"
    rapid_seconds: int = RAPID_SECONDS
    deep_seconds: int = DEEP_SECONDS
    recent_seconds: int = RECENT_SECONDS
    current_cache_seconds: int = CURRENT_CACHE_SECONDS
    excerpt_max_bytes: int = EXCERPT_MAX_BYTES
    excerpt_retention_days: int = EXCERPT_RETENTION_DAYS
    minimum_sectors: int = MIN_SECTORS
    minimum_field_coverage: Decimal = MIN_FIELD_COVERAGE
    positive_change: Decimal = POSITIVE_CHANGE
    negative_change: Decimal = NEGATIVE_CHANGE
    positive_breadth: Decimal = POSITIVE_BREADTH
    negative_breadth: Decimal = NEGATIVE_BREADTH
    crowding_percentile: Decimal = CROWDING_PERCENTILE
    market_crowding_share: Decimal = MARKET_CROWDING_SHARE
    flow_observations: int = FLOW_OBSERVATIONS
    flow_window_days: int = FLOW_WINDOW_DAYS
    market_max_age_days: int = MARKET_MAX_AGE_DAYS
    support_phrases: Tuple[str, ...] = SUPPORT_PHRASES
    restriction_phrases: Tuple[str, ...] = RESTRICTION_PHRASES
    source_registry: Tuple[Tuple[str, str, str, str, str], ...] = field(
        default_factory=_source_registry
    )
    metric_rules: Tuple[Tuple[str, Tuple[str, ...], str, str, str], ...] = field(
        default_factory=_metric_rules
    )
    privacy_fields: Tuple[str, ...] = field(default_factory=_privacy_fields)
    market_state_rules: Tuple[str, ...] = field(default_factory=_market_state_rules)
    sector_state_precedence: Tuple[str, ...] = field(default_factory=_sector_state_precedence)
    sector_state_rules: Tuple[str, ...] = field(default_factory=_sector_state_rules)
    transaction_output_available: bool = False

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "intelligence policy V1")
        if type(self) is not IntelligencePolicyV1:
            raise ValueError("intelligence policy V1 must be an exact IntelligencePolicyV1")
        if self != IntelligencePolicyV1():
            raise ValueError("intelligence policy V1 must be canonical")
        if self.version != "1" or type(self.version) is not str:
            raise ValueError("intelligence policy V1 version must be exactly '1'")
        for value, expected, name in (
            (self.rapid_seconds, RAPID_SECONDS, "rapid seconds"),
            (self.deep_seconds, DEEP_SECONDS, "deep seconds"),
            (self.recent_seconds, RECENT_SECONDS, "recent seconds"),
            (self.current_cache_seconds, CURRENT_CACHE_SECONDS, "current cache seconds"),
            (self.excerpt_max_bytes, EXCERPT_MAX_BYTES, "excerpt maximum bytes"),
            (
                self.excerpt_retention_days,
                EXCERPT_RETENTION_DAYS,
                "excerpt retention days",
            ),
            (self.minimum_sectors, MIN_SECTORS, "minimum sectors"),
            (self.flow_observations, FLOW_OBSERVATIONS, "flow observations"),
            (self.flow_window_days, FLOW_WINDOW_DAYS, "flow window days"),
            (self.market_max_age_days, MARKET_MAX_AGE_DAYS, "market maximum age days"),
        ):
            if type(value) is not int or value != expected:
                raise ValueError(f"{name} differs from intelligence policy V1")
        for value, expected, name in (
            (self.minimum_field_coverage, MIN_FIELD_COVERAGE, "minimum field coverage"),
            (self.positive_change, POSITIVE_CHANGE, "positive change"),
            (self.negative_change, NEGATIVE_CHANGE, "negative change"),
            (self.positive_breadth, POSITIVE_BREADTH, "positive breadth"),
            (self.negative_breadth, NEGATIVE_BREADTH, "negative breadth"),
            (self.crowding_percentile, CROWDING_PERCENTILE, "crowding percentile"),
            (self.market_crowding_share, MARKET_CROWDING_SHARE, "market crowding share"),
        ):
            if type(value) is not Decimal or value != expected:
                raise ValueError(f"{name} differs from intelligence policy V1")
        if self.support_phrases != SUPPORT_PHRASES:
            raise ValueError("support phrases differ from intelligence policy V1")
        if self.restriction_phrases != RESTRICTION_PHRASES:
            raise ValueError("restriction phrases differ from intelligence policy V1")
        if self.source_registry != _source_registry():
            raise ValueError("source registry differs from intelligence policy V1")
        if self.metric_rules != _metric_rules():
            raise ValueError("metric rules differ from intelligence policy V1")
        if self.privacy_fields != _privacy_fields():
            raise ValueError("privacy fields differ from intelligence policy V1")
        if self.market_state_rules != _market_state_rules():
            raise ValueError("market state rules differ from intelligence policy V1")
        if self.sector_state_precedence != _sector_state_precedence():
            raise ValueError("sector precedence differs from intelligence policy V1")
        if self.sector_state_rules != _sector_state_rules():
            raise ValueError("sector state rules differ from intelligence policy V1")
        if self.transaction_output_available is not False:
            raise ValueError("intelligence policy V1 never enables transaction output")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "budgets": {
                "deep_seconds": self.deep_seconds,
                "rapid_seconds": self.rapid_seconds,
            },
            "cache": {"current_seconds": self.current_cache_seconds},
            "catalyst_phrases": {
                "restriction": list(self.restriction_phrases),
                "support": list(self.support_phrases),
                "tier_1_exact_entity_only": True,
                "tier_2_directional_authority": False,
            },
            "excerpt": {
                "maximum_utf8_bytes": self.excerpt_max_bytes,
                "metadata_fingerprint_lineage_retained": True,
                "retention_days": self.excerpt_retention_days,
            },
            "market_state_rules": list(self.market_state_rules),
            "metric_rules": [
                {
                    "dimension": dimension,
                    "direction_rule": direction_rule,
                    "freshness": freshness,
                    "metric_ids": list(metric_ids),
                    "source": source,
                }
                for dimension, metric_ids, source, freshness, direction_rule in self.metric_rules
            ],
            "privacy": {
                "dynamic_field_denylist_checksum": hashlib.sha256(
                    canonical_json_bytes(self.privacy_fields)
                ).hexdigest(),
                "dynamic_public_trees_only": True,
            },
            "query": {"recent_seconds": self.recent_seconds},
            "sector_state_precedence": list(self.sector_state_precedence),
            "sector_state_rules": list(self.sector_state_rules),
            "source_registry": [
                {
                    "entry": entry,
                    "reason": reason,
                    "source_id": source_id,
                    "state": state,
                    "tier": tier,
                }
                for source_id, tier, entry, state, reason in self.source_registry
            ],
            "thresholds": {
                "crowding_percentile": self.crowding_percentile,
                "flow_observations": self.flow_observations,
                "flow_window_days": self.flow_window_days,
                "market_crowding_share": self.market_crowding_share,
                "market_max_age_days": self.market_max_age_days,
                "minimum_field_coverage": self.minimum_field_coverage,
                "minimum_sectors": self.minimum_sectors,
                "negative_breadth": self.negative_breadth,
                "negative_change": self.negative_change,
                "positive_breadth": self.positive_breadth,
                "positive_change": self.positive_change,
            },
            "transaction_output_available": self.transaction_output_available,
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        checksum = hashlib.sha256(self.canonical_json()).hexdigest()
        if self.version == "1" and checksum != INTELLIGENCE_POLICY_V1_GOLDEN_CHECKSUM:
            raise ValueError("IntelligencePolicy V1 canonical checksum drifted")
        return checksum
