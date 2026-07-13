from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal

from kunjin.funds.peers.models import (
    DirectoryCandidate,
    MembershipKind,
    PairwiseOverlap,
    PeerClassification,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
    SharedExposure,
    WindowMetric,
)

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
SOURCE_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
CHECKSUM = "a" * 64


def make_member(fund_code: str = "519755") -> PeerGroupMember:
    return PeerGroupMember(
        fund_code=fund_code,
        membership_kind=MembershipKind.ANCHOR,
        classification_key="mixed_flexible|active_or_unspecified|equity_bond",
        acceptance_reason="anchor_classification_match",
        warning=None,
        profile_source_document_id=1,
    )


def make_group(*members: PeerGroupMember, created_at: datetime = NOW) -> PeerGroup:
    return PeerGroup(
        id=None,
        anchor_fund_code="519755",
        rule_version="1",
        rule_key="mixed_flexible|active_or_unspecified|equity_bond",
        rule_description="Same normalized fund type, management style, and benchmark family.",
        candidate_source_url=SOURCE_URL,
        candidate_source_tier=2,
        candidate_source_checksum=CHECKSUM,
        input_fingerprint="b" * 64,
        created_at=created_at,
        status=PeerGroupStatus.SUCCESS,
        members=tuple(members) or (make_member(),),
    )


def make_overlap(**changes: object) -> PairwiseOverlap:
    values = {
        "left_fund_code": "519755",
        "right_fund_code": "000001",
        "metric_name": "top10_disclosed_overlap",
        "left_report_period": date(2026, 3, 31),
        "right_report_period": date(2026, 3, 31),
        "left_published_at": NOW,
        "right_published_at": NOW,
        "left_disclosed_weight": Decimal("42.50"),
        "right_disclosed_weight": Decimal("48.25"),
        "overlap": Decimal("5.10"),
        "shared": (
            SharedExposure(
                exposure_type="security",
                exposure_code="600000",
                exposure_name="浦发银行",
                left_weight=Decimal("7.20"),
                right_weight=Decimal("5.10"),
                shared_weight=Decimal("5.10"),
            ),
        ),
    }
    values.update(changes)
    return PairwiseOverlap(**values)


class DirectoryCandidateTests(unittest.TestCase):
    def test_valid_candidate(self) -> None:
        candidate = DirectoryCandidate(
            fund_code="519755",
            fund_name="交银多策略回报灵活配置混合A",
            directory_type="混合型-灵活",
            source_url=SOURCE_URL,
            source_checksum=CHECKSUM,
        )

        candidate.validate()
        with self.assertRaises(FrozenInstanceError):
            candidate.fund_name = "changed"  # type: ignore[misc]

    def test_rejects_invalid_fields(self) -> None:
        valid = {
            "fund_code": "519755",
            "fund_name": "交银多策略回报灵活配置混合A",
            "directory_type": "混合型-灵活",
            "source_url": SOURCE_URL,
            "source_checksum": CHECKSUM,
        }
        for field, value in (
            ("fund_code", "51975"),
            ("fund_name", " "),
            ("directory_type", ""),
            ("source_url", "http://fund.eastmoney.com/js/fundcode_search.js"),
            ("source_checksum", "A" * 64),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                DirectoryCandidate(**{**valid, field: value}).validate()


class PeerClassificationTests(unittest.TestCase):
    def test_validates_accepted_and_rejected_classifications(self) -> None:
        PeerClassification(
            fund_code="519755",
            accepted=True,
            classification_key="mixed_flexible|active_or_unspecified|equity_bond",
            fund_type_family="mixed_flexible",
            management_style="active_or_unspecified",
            benchmark_family="equity_bond",
            reason="classification_match",
        ).validate()
        PeerClassification(
            fund_code="000001",
            accepted=False,
            classification_key=None,
            fund_type_family=None,
            management_style=None,
            benchmark_family=None,
            reason="peer_classification_ambiguous",
            warnings=("missing_benchmark",),
        ).validate()

    def test_rejects_invalid_code_and_empty_reason(self) -> None:
        for changes in (
            {"fund_code": "abc123"},
            {"reason": ""},
        ):
            values = {
                "fund_code": "519755",
                "accepted": False,
                "classification_key": None,
                "fund_type_family": None,
                "management_style": None,
                "benchmark_family": None,
                "reason": "type_mismatch",
            }
            with self.assertRaises(ValueError):
                PeerClassification(**{**values, **changes}).validate()


class PeerGroupTests(unittest.TestCase):
    def test_valid_member_and_group(self) -> None:
        member = make_member()
        member.validate()
        make_group(member).validate()

    def test_rejects_invalid_member_fields(self) -> None:
        valid = {
            "fund_code": "519755",
            "membership_kind": MembershipKind.ANCHOR,
            "classification_key": "mixed_flexible|active_or_unspecified|equity_bond",
            "acceptance_reason": "anchor_classification_match",
            "warning": None,
            "profile_source_document_id": 1,
        }
        for field, value in (
            ("fund_code", "5197550"),
            ("membership_kind", "anchor"),
            ("classification_key", ""),
            ("acceptance_reason", " "),
            ("profile_source_document_id", 0),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                PeerGroupMember(**{**valid, field: value}).validate()

    def test_rejects_duplicate_members_and_more_than_twenty(self) -> None:
        with self.assertRaises(ValueError):
            make_group(make_member(), make_member()).validate()

        members = tuple(make_member(f"{code:06d}") for code in range(1, 22))
        with self.assertRaises(ValueError):
            make_group(*members).validate()

    def test_rejects_naive_created_at_and_invalid_group_fields(self) -> None:
        with self.assertRaises(ValueError):
            make_group(created_at=datetime(2026, 7, 11, 12, 0)).validate()

        valid = make_group()
        for field, value in (
            ("id", 0),
            ("rule_version", ""),
            ("rule_key", " "),
            ("rule_description", ""),
            ("candidate_source_url", "http://fund.eastmoney.com/x"),
            ("candidate_source_tier", 4),
            ("candidate_source_checksum", "x" * 64),
            ("input_fingerprint", "b" * 63),
            ("status", "success"),
        ):
            values = {name: getattr(valid, name) for name in valid.__dataclass_fields__}
            with self.subTest(field=field), self.assertRaises(ValueError):
                PeerGroup(**{**values, field: value}).validate()


class MetricAndOverlapTests(unittest.TestCase):
    def test_valid_window_metric(self) -> None:
        WindowMetric(
            fund_code="519755",
            window="90d",
            effective_start=date(2026, 4, 10),
            effective_end=date(2026, 7, 9),
            observations=61,
            total_return=Decimal("0.0325"),
            annualized_volatility=Decimal("0.125"),
            max_drawdown=Decimal("0.042"),
            drawdown_peak_date=date(2026, 5, 7),
            trough_date=date(2026, 5, 26),
            recovery_date=date(2026, 6, 18),
        ).validate()

    def test_rejects_invalid_window_metric_ratios(self) -> None:
        valid = {
            "fund_code": "519755",
            "window": "90d",
            "effective_start": date(2026, 4, 10),
            "effective_end": date(2026, 7, 9),
            "observations": 61,
            "total_return": Decimal("0.0325"),
            "annualized_volatility": Decimal("0.125"),
            "max_drawdown": Decimal("0.042"),
            "drawdown_peak_date": date(2026, 5, 7),
            "trough_date": date(2026, 5, 26),
            "recovery_date": date(2026, 6, 18),
        }
        for field, value in (
            ("total_return", Decimal("-1")),
            ("annualized_volatility", Decimal("-0.001")),
            ("max_drawdown", Decimal("-0.001")),
            ("max_drawdown", Decimal("1.001")),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                WindowMetric(**{**valid, field: value}).validate()

    def test_valid_overlap_and_percentage_bounds(self) -> None:
        make_overlap().validate()
        for field in ("left_disclosed_weight", "right_disclosed_weight", "overlap"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_overlap(**{field: Decimal("100.01")}).validate()
            with self.subTest(field=field), self.assertRaises(ValueError):
                make_overlap(**{field: Decimal("-0.01")}).validate()

        for field in ("left_weight", "right_weight", "shared_weight"):
            values = {
                "exposure_type": "security",
                "exposure_code": "600000",
                "exposure_name": "浦发银行",
                "left_weight": Decimal("1"),
                "right_weight": Decimal("1"),
                "shared_weight": Decimal("1"),
            }
            with self.subTest(field=field), self.assertRaises(ValueError):
                SharedExposure(**{**values, field: Decimal("101")}).validate()

        with self.assertRaises(ValueError):
            SharedExposure(
                exposure_type="",
                exposure_code="600000",
                exposure_name="浦发银行",
                left_weight=Decimal("1"),
                right_weight=Decimal("1"),
                shared_weight=Decimal("1"),
            ).validate()

    def test_overlap_must_equal_shared_weight_sum(self) -> None:
        with self.assertRaises(ValueError):
            make_overlap(overlap=Decimal("5.11")).validate()

    def test_shared_exposure_identity_includes_type(self) -> None:
        security = make_overlap().shared[0]
        industry = SharedExposure(
            exposure_type="industry",
            exposure_code=security.exposure_code,
            exposure_name="银行",
            left_weight=Decimal("3"),
            right_weight=Decimal("2"),
            shared_weight=Decimal("2"),
        )
        make_overlap(overlap=Decimal("7.10"), shared=(security, industry)).validate()
        with self.assertRaises(ValueError):
            make_overlap(overlap=Decimal("10.20"), shared=(security, security)).validate()

    def test_rejects_naive_publication_datetimes(self) -> None:
        with self.assertRaises(ValueError):
            make_overlap(left_published_at=datetime(2026, 7, 11, 12, 0)).validate()


if __name__ == "__main__":
    unittest.main()
