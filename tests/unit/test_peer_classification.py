from __future__ import annotations

import hashlib
import unittest
from datetime import date
from typing import Optional

from kunjin.funds.models import (
    DisclosureBundle,
    FundBenchmark,
    FundIdentity,
    FundShareClass,
)
from kunjin.funds.peers.classification import (
    DISCOVERY_VALIDATION_LIMIT,
    PEER_RULE_VERSION,
    benchmark_family,
    classify_peer,
    ordered_candidates,
)
from kunjin.funds.peers.models import DirectoryCandidate, MembershipKind

AS_OF = date(2026, 7, 11)
SOURCE_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
CHECKSUM = "a" * 64


def directory_candidate(
    fund_code: str,
    *,
    fund_name: Optional[str] = None,
    directory_type: str = "混合型-灵活",
) -> DirectoryCandidate:
    return DirectoryCandidate(
        fund_code=fund_code,
        fund_name=fund_name or f"示例基金{fund_code}",
        directory_type=directory_type,
        source_url=SOURCE_URL,
        source_checksum=CHECKSUM,
    )


def bundle(
    fund_code: str,
    *,
    status: str = "active",
    fund_type: Optional[str] = "混合型-灵活",
    benchmark: Optional[str] = "50%×沪深300指数收益率+50%×中债综合全价指数收益率",
    related: tuple[tuple[str, str], ...] = (),
    conflicts: tuple[str, ...] = (),
    identity_error: Optional[str] = None,
    identity: bool = True,
) -> DisclosureBundle:
    return DisclosureBundle(
        fund_code=fund_code,
        identity=(
            FundIdentity(
                fund_code=fund_code,
                fund_name=f"示例基金{fund_code}",
                status=status,
                fund_type=fund_type,
                established_date=date(2020, 1, 1),
                manager_name="示例基金管理有限公司",
                source_document_id=1,
            )
            if identity
            else None
        ),
        share_classes=tuple(
            FundShareClass(
                fund_code=fund_code,
                related_fund_code=related_code,
                share_class=share_class,
                fund_name=None,
                source_document_id=1,
            )
            for related_code, share_class in related
        ),
        manager_tenures=(),
        fee_rules=(),
        sizes=(),
        benchmarks=(
            ()
            if benchmark is None
            else (
                FundBenchmark(
                    fund_code=fund_code,
                    description=benchmark,
                    effective_from=None,
                    effective_to=None,
                    source_document_id=1,
                ),
            )
        ),
        holdings=(),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses=(
            {}
            if identity_error is None
            else {"basic_profile": {"error_code": identity_error}}
        ),
        conflicts=conflicts,
    )


class CandidateOrderingTests(unittest.TestCase):
    def test_orders_routes_by_precedence_and_retains_highest_precedence_kind(self) -> None:
        directory = (
            directory_candidate("519755"),
            directory_candidate("000001"),
            directory_candidate("000002"),
            directory_candidate("000003"),
            directory_candidate("000004", directory_type="债券型-混合二级"),
        )

        ordered = ordered_candidates(
            "519755",
            directory,
            user_supplied=("000003", "000001", "000003"),
            held_codes=("000002", "000001"),
        )

        self.assertEqual(
            ordered[:4],
            (
                ("519755", MembershipKind.ANCHOR),
                ("000003", MembershipKind.USER_SUPPLIED),
                ("000001", MembershipKind.USER_SUPPLIED),
                ("000002", MembershipKind.HELD),
            ),
        )
        self.assertNotIn(("000004", MembershipKind.DISCOVERED), ordered)

    def test_excludes_back_end_names_only_from_discovery(self) -> None:
        directory = (
            directory_candidate("519755"),
            directory_candidate("000001", fund_name="示例基金（后端）"),
            directory_candidate("000002", fund_name="示例基金(后端)"),
            directory_candidate("000003"),
        )

        discovered = ordered_candidates("519755", directory, (), ())
        explicit = ordered_candidates("519755", directory, ("000001",), ())

        self.assertNotIn(("000001", MembershipKind.DISCOVERED), discovered)
        self.assertNotIn(("000002", MembershipKind.DISCOVERED), discovered)
        self.assertIn(("000003", MembershipKind.DISCOVERED), discovered)
        self.assertIn(("000001", MembershipKind.USER_SUPPLIED), explicit)

    def test_discovery_order_is_anchor_seeded_sha256_and_bounded(self) -> None:
        directory = (directory_candidate("519755"),) + tuple(
            directory_candidate(f"{code:06d}") for code in range(1, 61)
        )
        expected = tuple(
            sorted(
                (f"{code:06d}" for code in range(1, 61)),
                key=lambda code: (
                    hashlib.sha256(f"519755:{code}".encode("ascii")).hexdigest(),
                    code,
                ),
            )[:DISCOVERY_VALIDATION_LIMIT]
        )

        first = ordered_candidates("519755", directory, (), ())
        repeated = ordered_candidates("519755", directory, (), ())
        other_anchor = ordered_candidates(
            "519756",
            (directory_candidate("519756"),) + directory[1:],
            (),
            (),
        )

        self.assertEqual(first, repeated)
        self.assertEqual(tuple(code for code, _ in first[1:]), expected)
        self.assertEqual(len(first[1:]), DISCOVERY_VALIDATION_LIMIT)
        self.assertNotEqual(first[1:], other_anchor[1:])


class PeerClassificationTests(unittest.TestCase):
    def test_builds_exact_mixed_active_equity_bond_key(self) -> None:
        result = classify_peer(bundle("519755"), bundle("000001"), AS_OF)

        self.assertTrue(result.accepted)
        self.assertEqual(PEER_RULE_VERSION, "1")
        self.assertEqual(result.fund_type_family, "混合型-灵活")
        self.assertEqual(result.management_style, "active_or_unspecified")
        self.assertEqual(result.benchmark_family, "equity_bond")
        self.assertEqual(
            result.classification_key,
            "混合型-灵活|active_or_unspecified|equity_bond",
        )
        self.assertEqual(result.reason, "classification_match")

    def test_explicit_index_type_is_passive(self) -> None:
        anchor = bundle("519755", fund_type="指数型-股票", benchmark="沪深300指数收益率")
        result = classify_peer(
            anchor,
            bundle("000001", fund_type="指数型-股票", benchmark="沪深300指数收益率"),
            AS_OF,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.management_style, "passive")
        self.assertEqual(result.classification_key, "指数型-股票|passive|equity")

    def test_rejects_identity_status_conflict_and_ambiguous_type_with_stable_reasons(self) -> None:
        anchor = bundle("519755")
        cases = (
            (bundle("000001", identity=False), "missing_identity", ()),
            (bundle("000001", status="terminated"), "inactive_fund", ()),
            (
                bundle("000001", identity_error="identity_conflict"),
                "identity_conflict",
                (),
            ),
            (
                bundle("000001", fund_type=None),
                "peer_classification_ambiguous",
                ("missing_fund_type",),
            ),
        )

        for candidate, reason, warnings in cases:
            with self.subTest(reason=reason):
                result = classify_peer(anchor, candidate, AS_OF)
                self.assertFalse(result.accepted)
                self.assertEqual(result.reason, reason)
                self.assertEqual(result.warnings, warnings)

    def test_rejects_type_and_benchmark_family_mismatches(self) -> None:
        anchor = bundle("519755")

        type_mismatch = classify_peer(
            anchor,
            bundle("000001", fund_type="混合型-偏股"),
            AS_OF,
        )
        benchmark_mismatch = classify_peer(
            anchor,
            bundle("000002", benchmark="沪深300指数收益率"),
            AS_OF,
        )

        self.assertFalse(type_mismatch.accepted)
        self.assertEqual(type_mismatch.reason, "type_mismatch")
        self.assertFalse(benchmark_mismatch.accepted)
        self.assertEqual(benchmark_mismatch.reason, "benchmark_mismatch")

    def test_missing_or_unrecognized_benchmark_is_ambiguous(self) -> None:
        anchor = bundle("519755")
        for candidate in (
            bundle("000001", benchmark=None),
            bundle("000002", benchmark="一年期银行定期存款利率"),
        ):
            with self.subTest(fund_code=candidate.fund_code):
                result = classify_peer(anchor, candidate, AS_OF)
                self.assertFalse(result.accepted)
                self.assertEqual(result.reason, "peer_classification_ambiguous")
                self.assertEqual(result.warnings, ("missing_benchmark_family",))

    def test_explicit_ac_relationship_is_accepted_with_sibling_warning(self) -> None:
        anchor = bundle(
            "519755",
            related=(("519755", "A"), ("519756", "C")),
        )
        candidate = bundle(
            "519756",
            related=(("519755", "A"), ("519756", "C")),
        )

        result = classify_peer(anchor, candidate, AS_OF)

        self.assertTrue(result.accepted)
        self.assertEqual(result.warnings, ("share_class_sibling",))

    def test_benchmark_family_normalizes_nfkc_and_whitespace(self) -> None:
        self.assertEqual(
            benchmark_family(" 沪深３００ 指数收益率 + 中债 综合指数收益率 "),
            "equity_bond",
        )
        self.assertIsNone(benchmark_family("一年期银行定期存款利率"))

    def test_benchmark_family_recognizes_common_formal_index_names(self) -> None:
        self.assertEqual(
            benchmark_family("沪深300指数收益率+中证全债指数收益率"),
            "equity_bond",
        )
        self.assertEqual(benchmark_family("中证800指数收益率"), "equity")
        self.assertEqual(benchmark_family("中证全债指数收益率"), "bond")


if __name__ == "__main__":
    unittest.main()
