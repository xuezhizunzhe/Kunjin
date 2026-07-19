from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Tuple

from kunjin.decision.models import canonical_json_bytes, validate_exact_dataclass_state

HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM = (
    "a78f01681f5b45dcbc9a264cfbdb2ee9805c30c7dab8583903ee60a83956fc46"
)


def _core_omission_prohibition() -> Tuple[str, ...]:
    return (
        "identity_profile",
        "personal_position_observation",
        "formal_nav",
        "manager_fee_profile",
        "holdings_industries",
        "official_announcements",
    )


@dataclass(frozen=True)
class HeldFundManualReviewPolicyV1:
    version: str = "1"
    orchestration_window_seconds: int = 1800
    maximum_announcement_candidates: int = 20
    maximum_announcement_body_bytes: int = 512 * 1024
    maximum_announcement_total_bytes: int = 4 * 1024 * 1024
    core_omission_prohibition: Tuple[str, ...] = _core_omission_prohibition()
    sell_timing: str = "insufficient_data"
    action_authorized: bool = False
    exact_amount_available: bool = False
    automatic_trade: bool = False

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "held fund manual review policy V1")
        if type(self) is not HeldFundManualReviewPolicyV1:
            raise ValueError("held fund manual review policy V1 subclasses are not accepted")
        if self != HeldFundManualReviewPolicyV1():
            raise ValueError("held fund manual review policy V1 must be canonical")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "action_authorized": self.action_authorized,
            "automatic_trade": self.automatic_trade,
            "core_omission_prohibition": list(self.core_omission_prohibition),
            "exact_amount_available": self.exact_amount_available,
            "maximum_announcement_body_bytes": self.maximum_announcement_body_bytes,
            "maximum_announcement_candidates": self.maximum_announcement_candidates,
            "maximum_announcement_total_bytes": self.maximum_announcement_total_bytes,
            "orchestration_window_seconds": self.orchestration_window_seconds,
            "sell_timing": self.sell_timing,
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self)

    def checksum(self) -> str:
        checksum = hashlib.sha256(self.canonical_json()).hexdigest()
        if checksum != HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM:
            raise ValueError("HeldFundManualReviewPolicy V1 canonical checksum drifted")
        return checksum
