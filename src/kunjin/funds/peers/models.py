from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple
from urllib.parse import urlparse


FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
CHECKSUM_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PEER_MEMBER_LIMIT = 20


class MembershipKind(str, Enum):
    ANCHOR = "anchor"
    USER_SUPPLIED = "user_supplied"
    HELD = "held"
    DISCOVERED = "discovered"


class PeerGroupStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"


class PeerSyncState(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    SOURCE_UNAVAILABLE = "source_unavailable"


def _validate_fund_code(fund_code: str) -> None:
    if not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError(f"invalid fund code: {fund_code}")


def _validate_required(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _validate_optional_text(value: Optional[str], field_name: str) -> None:
    if value is not None:
        _validate_required(value, field_name)


def _validate_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("source URL must use HTTPS")


def _validate_checksum(value: str, field_name: str) -> None:
    if not CHECKSUM_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_positive_id(value: Optional[int], field_name: str) -> None:
    if value is not None and (not isinstance(value, int) or value <= 0):
        raise ValueError(f"{field_name} must be positive")


def _validate_percentage(value: Decimal, field_name: str) -> None:
    if value < 0 or value > 100:
        raise ValueError(f"{field_name} must be between 0 and 100")


def _validate_warnings(warnings: Tuple[str, ...]) -> None:
    for warning in warnings:
        _validate_required(warning, "warning")


@dataclass(frozen=True)
class DirectoryCandidate:
    fund_code: str
    fund_name: str
    directory_type: str
    source_url: str
    source_checksum: str

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.fund_name, "fund name")
        _validate_required(self.directory_type, "directory type")
        _validate_https_url(self.source_url)
        _validate_checksum(self.source_checksum, "source checksum")


@dataclass(frozen=True)
class PeerClassification:
    fund_code: str
    accepted: bool
    classification_key: Optional[str]
    fund_type_family: Optional[str]
    management_style: Optional[str]
    benchmark_family: Optional[str]
    reason: str
    warnings: Tuple[str, ...] = ()

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        if not isinstance(self.accepted, bool):
            raise ValueError("accepted must be boolean")
        for value, field_name in (
            (self.classification_key, "classification key"),
            (self.fund_type_family, "fund type family"),
            (self.management_style, "management style"),
            (self.benchmark_family, "benchmark family"),
        ):
            _validate_optional_text(value, field_name)
        if self.accepted and any(
            value is None
            for value in (
                self.classification_key,
                self.fund_type_family,
                self.management_style,
                self.benchmark_family,
            )
        ):
            raise ValueError("accepted classification must be complete")
        _validate_required(self.reason, "classification reason")
        _validate_warnings(self.warnings)


@dataclass(frozen=True)
class PeerGroupMember:
    fund_code: str
    membership_kind: MembershipKind
    classification_key: str
    acceptance_reason: str
    warning: Optional[str]
    profile_source_document_id: Optional[int]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        if not isinstance(self.membership_kind, MembershipKind):
            raise ValueError("unknown membership kind")
        _validate_required(self.classification_key, "classification key")
        _validate_required(self.acceptance_reason, "acceptance reason")
        _validate_optional_text(self.warning, "warning")
        _validate_positive_id(self.profile_source_document_id, "profile source document id")


@dataclass(frozen=True)
class PeerGroup:
    id: Optional[int]
    anchor_fund_code: str
    rule_version: str
    rule_key: str
    rule_description: str
    candidate_source_url: str
    candidate_source_tier: int
    candidate_source_checksum: str
    input_fingerprint: str
    created_at: datetime
    status: PeerGroupStatus
    members: Tuple[PeerGroupMember, ...]
    warnings: Tuple[str, ...] = ()

    def validate(self) -> None:
        _validate_positive_id(self.id, "peer group id")
        _validate_fund_code(self.anchor_fund_code)
        _validate_required(self.rule_version, "rule version")
        _validate_required(self.rule_key, "rule key")
        _validate_required(self.rule_description, "rule description")
        _validate_https_url(self.candidate_source_url)
        if self.candidate_source_tier not in {1, 2, 3}:
            raise ValueError("candidate source tier must be between 1 and 3")
        _validate_checksum(self.candidate_source_checksum, "candidate source checksum")
        _validate_checksum(self.input_fingerprint, "input fingerprint")
        _validate_aware(self.created_at, "created_at")
        if not isinstance(self.status, PeerGroupStatus):
            raise ValueError("unknown peer group status")
        if not self.members:
            raise ValueError("peer group must contain at least one member")
        if len(self.members) > PEER_MEMBER_LIMIT:
            raise ValueError(f"peer group cannot contain more than {PEER_MEMBER_LIMIT} members")
        member_codes = []
        for member in self.members:
            member.validate()
            member_codes.append(member.fund_code)
        if len(member_codes) != len(set(member_codes)):
            raise ValueError("peer group members must have unique fund codes")
        if self.anchor_fund_code not in member_codes:
            raise ValueError("peer group must contain the anchor fund")
        _validate_warnings(self.warnings)


@dataclass(frozen=True)
class WindowMetric:
    fund_code: str
    window: str
    effective_start: date
    effective_end: date
    observations: int
    total_return: Decimal
    annualized_volatility: Optional[Decimal]
    max_drawdown: Decimal
    drawdown_peak_date: date
    trough_date: date
    recovery_date: Optional[date]

    def validate(self) -> None:
        _validate_fund_code(self.fund_code)
        _validate_required(self.window, "window")
        if self.effective_end < self.effective_start:
            raise ValueError("effective end cannot precede effective start")
        if self.observations <= 0:
            raise ValueError("observations must be positive")
        if self.total_return <= -1:
            raise ValueError("total return must be greater than -1")
        if self.annualized_volatility is not None and self.annualized_volatility < 0:
            raise ValueError("annualized volatility cannot be negative")
        if self.max_drawdown < 0 or self.max_drawdown > 1:
            raise ValueError("maximum drawdown must be between 0 and 1")
        for value, field_name in (
            (self.drawdown_peak_date, "drawdown peak date"),
            (self.trough_date, "trough date"),
        ):
            if value < self.effective_start or value > self.effective_end:
                raise ValueError(f"{field_name} must be inside the effective window")
        if self.trough_date < self.drawdown_peak_date:
            raise ValueError("trough date cannot precede drawdown peak date")
        if self.recovery_date is not None and (
            self.recovery_date < self.trough_date or self.recovery_date > self.effective_end
        ):
            raise ValueError("recovery date must follow the trough inside the effective window")


@dataclass(frozen=True)
class SharedExposure:
    exposure_type: str
    exposure_code: str
    exposure_name: str
    left_weight: Decimal
    right_weight: Decimal
    shared_weight: Decimal

    def validate(self) -> None:
        _validate_required(self.exposure_type, "exposure type")
        _validate_required(self.exposure_code, "exposure code")
        _validate_required(self.exposure_name, "exposure name")
        _validate_percentage(self.left_weight, "left weight")
        _validate_percentage(self.right_weight, "right weight")
        _validate_percentage(self.shared_weight, "shared weight")
        if self.shared_weight > min(self.left_weight, self.right_weight):
            raise ValueError("shared weight cannot exceed either exposure weight")


@dataclass(frozen=True)
class PairwiseOverlap:
    left_fund_code: str
    right_fund_code: str
    metric_name: str
    left_report_period: date
    right_report_period: date
    left_published_at: datetime
    right_published_at: datetime
    left_disclosed_weight: Decimal
    right_disclosed_weight: Decimal
    overlap: Decimal
    shared: Tuple[SharedExposure, ...]
    warnings: Tuple[str, ...] = ()

    def validate(self) -> None:
        _validate_fund_code(self.left_fund_code)
        _validate_fund_code(self.right_fund_code)
        if self.left_fund_code == self.right_fund_code:
            raise ValueError("overlap requires two different funds")
        _validate_required(self.metric_name, "metric name")
        _validate_aware(self.left_published_at, "left_published_at")
        _validate_aware(self.right_published_at, "right_published_at")
        _validate_percentage(self.left_disclosed_weight, "left disclosed weight")
        _validate_percentage(self.right_disclosed_weight, "right disclosed weight")
        _validate_percentage(self.overlap, "overlap")
        if self.overlap > min(self.left_disclosed_weight, self.right_disclosed_weight):
            raise ValueError("overlap cannot exceed either disclosed weight")
        exposure_keys = []
        shared_weight_sum = Decimal("0")
        for exposure in self.shared:
            exposure.validate()
            exposure_keys.append((exposure.exposure_type, exposure.exposure_code))
            shared_weight_sum += exposure.shared_weight
        if len(exposure_keys) != len(set(exposure_keys)):
            raise ValueError("shared exposures must have unique type and code pairs")
        if self.overlap != shared_weight_sum:
            raise ValueError("overlap must equal the sum of shared exposure weights")
        _validate_warnings(self.warnings)
