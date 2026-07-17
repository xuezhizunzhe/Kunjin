from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Tuple

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import canonical_json_bytes, validate_exact_dataclass_state

RAPID_NAV_MAX_PAGES = 6
DEEP_NAV_MAX_PAGES = 50
MIN_CORRELATION_SAMPLES = 60
MAX_OFFICIAL_EVENTS = 20
MAX_FACTS = 128
MAX_RELATIONSHIPS = 128

HELD_FUND_BRIEF_POLICY_V1_GOLDEN_CHECKSUM = (
    "17ef267c1604f03fa9b941cd014e894766b7533d32341dd5cc7e4ec2062b12b3"
)


def _state_precedence() -> Tuple[str, ...]:
    return (
        "phase_b_hard_block_no_add",
        "liquidation_or_termination_reduce_or_exit_review",
        "identity_or_action_critical_gap_abstain",
        "supported_risk_event_watch",
        "owner_confirmed_thesis_hold",
        "sufficient_facts_without_owner_thesis_watch",
    )


def _fact_requirements() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    return (
        (
            "continue_holding",
            ("identity_active_status", "personal_position", "formal_nav", "official_events"),
        ),
        (
            "reduce_to_cash",
            ("identity_active_status", "personal_position", "redemption_terms", "official_events"),
        ),
        (
            "full_exit",
            ("identity_active_status", "personal_position", "redemption_terms", "official_events"),
        ),
        (
            "switch_reduce",
            ("identity_active_status", "personal_position", "redemption_terms", "official_events"),
        ),
        (
            "switch_buy",
            (
                "identity_active_status",
                "phase_b",
                "phase_c",
                "d1",
                "d2",
                "d3",
                "post_trade",
            ),
        ),
    )


def _official_event_rules() -> Tuple[Tuple[str, str], ...]:
    return (
        (OfficialEventCode.FUND_LIQUIDATION_NOTICE.value, "trigger_reduce_or_exit_review"),
        (OfficialEventCode.FUND_TERMINATION_NOTICE.value, "trigger_reduce_or_exit_review"),
        (OfficialEventCode.MANAGER_CHANGE_NOTICE.value, "trigger_watch"),
        (OfficialEventCode.SUBSCRIPTION_SUSPENSION_NOTICE.value, "block_add_and_trigger_watch"),
        (OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE.value, "abstain_affected_exit_action"),
        (OfficialEventCode.FEE_CHANGE_NOTICE.value, "trigger_watch"),
        (OfficialEventCode.BENCHMARK_CHANGE_NOTICE.value, "trigger_watch"),
        (OfficialEventCode.OTHER_OFFICIAL_PRODUCT_NOTICE.value, "evidence_only"),
    )


@dataclass(frozen=True)
class HeldFundBriefPolicyV1:
    version: str = "1"
    rapid_nav_max_pages: int = RAPID_NAV_MAX_PAGES
    deep_nav_max_pages: int = DEEP_NAV_MAX_PAGES
    minimum_correlation_samples: int = MIN_CORRELATION_SAMPLES
    maximum_official_events: int = MAX_OFFICIAL_EVENTS
    maximum_facts: int = MAX_FACTS
    maximum_relationships: int = MAX_RELATIONSHIPS
    state_precedence: Tuple[str, ...] = field(default_factory=_state_precedence)
    fact_requirements: Tuple[Tuple[str, Tuple[str, ...]], ...] = field(
        default_factory=_fact_requirements
    )
    official_event_rules: Tuple[Tuple[str, str], ...] = field(
        default_factory=_official_event_rules
    )
    exact_amount_available: bool = False

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "held fund brief policy V1")
        if type(self) is not HeldFundBriefPolicyV1:
            raise ValueError("held fund brief policy V1 subclasses are not accepted")
        expected = HeldFundBriefPolicyV1()
        if self != expected:
            raise ValueError("held fund brief policy V1 must be canonical")
        if type(self.version) is not str or self.version != "1":
            raise ValueError("held fund brief policy V1 version must be '1'")
        for value, expected_value, name in (
            (self.rapid_nav_max_pages, RAPID_NAV_MAX_PAGES, "rapid NAV pages"),
            (self.deep_nav_max_pages, DEEP_NAV_MAX_PAGES, "deep NAV pages"),
            (
                self.minimum_correlation_samples,
                MIN_CORRELATION_SAMPLES,
                "minimum correlation samples",
            ),
            (self.maximum_official_events, MAX_OFFICIAL_EVENTS, "maximum official events"),
            (self.maximum_facts, MAX_FACTS, "maximum facts"),
            (self.maximum_relationships, MAX_RELATIONSHIPS, "maximum relationships"),
        ):
            if type(value) is not int or value != expected_value:
                raise ValueError(f"{name} differs from held fund brief policy V1")
        if self.state_precedence != _state_precedence():
            raise ValueError("state precedence differs from held fund brief policy V1")
        if self.fact_requirements != _fact_requirements():
            raise ValueError("fact requirements differ from held fund brief policy V1")
        if self.official_event_rules != _official_event_rules():
            raise ValueError("official event rules differ from held fund brief policy V1")
        if self.exact_amount_available is not False:
            raise ValueError("held fund brief policy V1 never exposes an exact amount")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "deep_nav_max_pages": self.deep_nav_max_pages,
            "exact_amount_available": self.exact_amount_available,
            "fact_requirements": [
                {"action_id": action_id, "required_fields": list(required_fields)}
                for action_id, required_fields in self.fact_requirements
            ],
            "maximum_facts": self.maximum_facts,
            "maximum_official_events": self.maximum_official_events,
            "maximum_relationships": self.maximum_relationships,
            "minimum_correlation_samples": self.minimum_correlation_samples,
            "official_event_rules": [
                {"event_code": event_code, "rule": rule}
                for event_code, rule in self.official_event_rules
            ],
            "rapid_nav_max_pages": self.rapid_nav_max_pages,
            "state_precedence": list(self.state_precedence),
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        checksum = hashlib.sha256(self.canonical_json()).hexdigest()
        if self.version == "1" and checksum != HELD_FUND_BRIEF_POLICY_V1_GOLDEN_CHECKSUM:
            raise ValueError("HeldFundBriefPolicy V1 canonical checksum drifted")
        return checksum
