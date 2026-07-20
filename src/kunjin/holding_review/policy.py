from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Tuple

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import canonical_json_bytes, validate_exact_dataclass_state
from kunjin.funds.official_domains import (
    OFFICIAL_SOURCE_REGISTRY_VERSION,
    official_source_registry_checksum,
)

HELD_FUND_MANUAL_REVIEW_POLICY_V1_GOLDEN_CHECKSUM = (
    "a78f01681f5b45dcbc9a264cfbdb2ee9805c30c7dab8583903ee60a83956fc46"
)
OFFICIAL_CANDIDATE_DETECTOR_V1_GOLDEN_CHECKSUM = (
    "fd12330998e128b7e011871e56f0a263f7f4f53269e675fdd88fc5fdfa8927af"
)
OFFICIAL_CHECK_POLICY_V1_GOLDEN_CHECKSUM = (
    "93722946c100518229531c79cabf606c23cf169536bd5c1d3213e3bf5836cb1b"
)
OFFICIAL_QUERY_WINDOW_DAYS = 180
OFFICIAL_MANAGER_IDENTITY_MAXIMUM_AGE_DAYS = 30
OFFICIAL_MAXIMUM_LISTING_PAGES = 10
OFFICIAL_MAXIMUM_LISTING_ITEMS = 1_000
OFFICIAL_MAXIMUM_LISTING_PAGE_BYTES = 2 * 1024 * 1024
OFFICIAL_MAXIMUM_CANDIDATES = 20
OFFICIAL_MAXIMUM_BODIES = 20


def _core_omission_prohibition() -> Tuple[str, ...]:
    return (
        "identity_profile",
        "personal_position_observation",
        "formal_nav",
        "manager_fee_profile",
        "holdings_industries",
        "official_announcements",
    )


def _official_event_codes() -> Tuple[OfficialEventCode, ...]:
    return (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        OfficialEventCode.FUND_TERMINATION_NOTICE,
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        OfficialEventCode.FEE_CHANGE_NOTICE,
        OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
    )


def _candidate_lexemes() -> Tuple[str, ...]:
    return (
        "业绩比较基准",
        "作废",
        "停止赎回",
        "旗下基金",
        "基金合同终止",
        "基金经理",
        "基金财产清算",
        "增聘",
        "恢复",
        "恢复大额赎回",
        "恢复赎回",
        "托管费",
        "撤回",
        "撤销",
        "旗下部分基金",
        "多只基金",
        "暂停赎回",
        "暂停大额赎回",
        "更正",
        "清算",
        "离任",
        "聘任",
        "相关基金",
        "管理费",
        "终止",
        "终止基金合同",
        "补充",
        "解聘",
        "费率",
        "赎回限制",
        "销售服务费",
        "限制赎回",
        "重大事项",
        "重要事项",
        "有关事项",
        "部分基金",
    )


def _candidate_detector_checksum(
    version: str,
    normalization_rule: str,
    lexemes: Tuple[str, ...],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "lexemes": list(lexemes),
                "normalization": normalization_rule,
                "version": version,
            }
        )
    ).hexdigest()


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


@dataclass(frozen=True)
class OfficialCheckPolicyV1:
    version: str = "1"
    query_window_days: int = OFFICIAL_QUERY_WINDOW_DAYS
    manager_identity_maximum_age_days: int = OFFICIAL_MANAGER_IDENTITY_MAXIMUM_AGE_DAYS
    maximum_listing_pages: int = OFFICIAL_MAXIMUM_LISTING_PAGES
    maximum_listing_items: int = OFFICIAL_MAXIMUM_LISTING_ITEMS
    maximum_listing_page_bytes: int = OFFICIAL_MAXIMUM_LISTING_PAGE_BYTES
    maximum_candidates: int = OFFICIAL_MAXIMUM_CANDIDATES
    maximum_bodies: int = OFFICIAL_MAXIMUM_BODIES
    automatic_retry: bool = False
    supported_event_codes: Tuple[OfficialEventCode, ...] = _official_event_codes()
    candidate_detector_version: str = "1"
    candidate_detector_checksum: str = OFFICIAL_CANDIDATE_DETECTOR_V1_GOLDEN_CHECKSUM
    normalization_rule: str = "nfkc_whitespace_v1"
    candidate_lexemes: Tuple[str, ...] = _candidate_lexemes()
    positive_projector_version: str = "1"
    page_manifest_version: str = "1"
    official_registry_version: str = OFFICIAL_SOURCE_REGISTRY_VERSION
    official_registry_checksum: str = official_source_registry_checksum()

    def validate(self) -> None:
        validate_exact_dataclass_state(self, "official check policy V1")
        if type(self) is not OfficialCheckPolicyV1:
            raise ValueError("official check policy V1 subclasses are not accepted")
        if self != OfficialCheckPolicyV1():
            raise ValueError("official check policy V1 must be canonical")
        detector_checksum = _candidate_detector_checksum(
            self.candidate_detector_version,
            self.normalization_rule,
            self.candidate_lexemes,
        )
        if detector_checksum != self.candidate_detector_checksum:
            raise ValueError("official candidate detector V1 checksum drifted")

    def to_canonical_dict(self) -> dict:
        self.validate()
        return {
            "automatic_retry": self.automatic_retry,
            "candidate_detector_checksum": self.candidate_detector_checksum,
            "candidate_detector_version": self.candidate_detector_version,
            "candidate_lexemes": list(self.candidate_lexemes),
            "manager_identity_maximum_age_days": self.manager_identity_maximum_age_days,
            "maximum_listing_items": self.maximum_listing_items,
            "maximum_listing_page_bytes": self.maximum_listing_page_bytes,
            "maximum_listing_pages": self.maximum_listing_pages,
            "maximum_bodies": self.maximum_bodies,
            "maximum_candidates": self.maximum_candidates,
            "normalization_rule": self.normalization_rule,
            "official_registry_checksum": self.official_registry_checksum,
            "official_registry_version": self.official_registry_version,
            "page_manifest_version": self.page_manifest_version,
            "positive_projector_version": self.positive_projector_version,
            "query_window_days": self.query_window_days,
            "supported_event_codes": [item.value for item in self.supported_event_codes],
            "version": self.version,
        }

    def canonical_json(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_dict())

    def checksum(self) -> str:
        checksum = hashlib.sha256(self.canonical_json()).hexdigest()
        if checksum != OFFICIAL_CHECK_POLICY_V1_GOLDEN_CHECKSUM:
            raise ValueError("OfficialCheckPolicy V1 canonical checksum drifted")
        return checksum
