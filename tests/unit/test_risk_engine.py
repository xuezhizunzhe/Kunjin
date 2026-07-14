from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.documents import OfficialDocumentCandidate, RetrievedArtifact
from kunjin.funds.risk.engine import (
    ClassificationEvidence,
    classification_input_manifest,
    classification_input_manifest_v1,
    classification_input_manifest_v2,
    classify_fund,
    conservative_classification_rank,
)
from kunjin.funds.risk.models import (
    EvidenceFreshness,
    EvidenceStatus,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    MandateFact,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
)
from kunjin.funds.risk.parsers import parse_artifact
from kunjin.funds.risk.policy import ClassificationPolicyV1

NOW = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
POLICY = ClassificationPolicyV1()
FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds" / "risk"


def D(value: object) -> Decimal:
    return Decimal(str(value))


def fact(kind: str, value: object, *, document_id: int = 1) -> MandateFact:
    canonical = json.dumps(
        [kind, str(value), document_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return MandateFact(
        fund_code="000001",
        fact_kind=kind,
        normalized_value=value,
        unit=None,
        source_document_id=document_id,
        page_number=1,
        section_name="synthetic_public_evidence",
        source_excerpt=f"synthetic public fact: {kind}",
        effective_from=None,
        effective_to=None,
        confidence_state=FactConfidence.EXACT,
        parser_version="test",
        fact_fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def parsed_facts(
    path: Path,
    *,
    document_kind: DocumentKind,
    document_id: int,
) -> Tuple[MandateFact, ...]:
    raw = path.read_bytes()
    artifact = RetrievedArtifact(
        candidate=OfficialDocumentCandidate(
            fund_code="000001",
            document_kind=document_kind,
            title="Public synthetic official document",
            url="https://www.fund001.com/synthetic/document",
            publisher="Public synthetic publisher",
            published_at=NOW - timedelta(days=1),
            source_tier=1,
        ),
        final_url="https://www.fund001.com/synthetic/document",
        retrieved_at=NOW,
        content_type="text/html",
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        managed_path=path,
    )
    parsed = parse_artifact(artifact)
    return tuple(
        MandateFact(
            fund_code="000001",
            fact_kind=item.fact_kind,
            normalized_value=item.normalized_value,
            unit=item.unit,
            source_document_id=document_id,
            page_number=item.page_number,
            section_name=item.section_name,
            source_excerpt=item.source_excerpt,
            effective_from=item.effective_from,
            effective_to=item.effective_to,
            confidence_state=item.confidence_state,
            parser_version="risk_parser_v1",
            fact_fingerprint=item.fact_fingerprint,
        )
        for item in parsed.facts
    )


def parsed_evidence(
    *,
    legal_facts: Tuple[MandateFact, ...] = (),
    benchmark_facts: Tuple[MandateFact, ...] = (),
) -> ClassificationEvidence:
    d1_facts = legal_facts + benchmark_facts
    document_ids = tuple(sorted({item.source_document_id for item in d1_facts}))
    return ClassificationEvidence(
        fund_code="000001",
        legal_facts=legal_facts,
        benchmark_facts=benchmark_facts,
        report_facts=(),
        existing_disclosure_facts=(),
        nav_conflicts=(),
        external_evidence_fingerprints=(),
        external_source_references=(),
        nav_evidence_fingerprint=None,
        nav_observation_start=None,
        nav_observation_end=None,
        freshness=tuple(
            EvidenceFreshness(
                section=f"section_{document_id}",
                source_document_id=document_id,
                state=FreshnessState.CURRENT,
                observed_at=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=30),
                critical=True,
            )
            for document_id in document_ids
        ),
        document_ids=document_ids,
        fact_ids=tuple(range(1, len(d1_facts) + 1)),
        parse_result_ids=tuple(range(1, len(document_ids) + 1)),
        parser_provenance_checksums=tuple(
            chr(ord("a") + index) * 64 for index in range(len(document_ids))
        ),
    )


def facts(values: Dict[str, object], *, document_id: int) -> Tuple[MandateFact, ...]:
    return tuple(fact(kind, value, document_id=document_id) for kind, value in values.items())


def external_reference(**changes: object) -> ExternalSourceReference:
    values = {
        "source_namespace": "fund_disclosure",
        "source_document_id": 4,
        "fund_code": "000001",
        "document_kind": "basic_profile",
        "section": "synthetic_public_evidence",
        "title": "public disclosure",
        "url": "https://www.fund001.com/public.html",
        "source_name": "test_public_source",
        "source_tier": 2,
        "publisher": "public publisher",
        "published_at": NOW - timedelta(days=2),
        "retrieved_at": NOW - timedelta(days=1),
        "checksum": "e" * 64,
    }
    values.update(changes)
    return ExternalSourceReference(**values)


def evidence(
    *,
    legal: Optional[Dict[str, object]] = None,
    benchmark: Optional[Dict[str, object]] = None,
    report: Optional[Dict[str, object]] = None,
    disclosures: Optional[Dict[str, object]] = None,
    nav_conflicts: Iterable[str] = (),
    external_evidence_fingerprints: Tuple[Tuple[str, str], ...] = (),
    external_source_references: Tuple[ExternalSourceReference, ...] = (),
    nav_evidence_fingerprint: Optional[str] = None,
    nav_observation_start: Optional[date] = None,
    nav_observation_end: Optional[date] = None,
) -> ClassificationEvidence:
    legal_facts = facts(legal or {}, document_id=1)
    benchmark_facts = facts(benchmark or {}, document_id=2)
    report_facts = facts(report or {}, document_id=3)
    disclosure_facts = facts(disclosures or {}, document_id=4)
    d1_facts = legal_facts + benchmark_facts + report_facts
    document_ids = tuple(sorted({item.source_document_id for item in d1_facts}))
    if disclosure_facts and not external_evidence_fingerprints:
        external_evidence_fingerprints = (("synthetic_public_evidence", "f" * 64),)
    if disclosure_facts and not external_source_references:
        external_source_references = (external_reference(),)
    freshness = tuple(
        EvidenceFreshness(
            section=f"section_{document_id}",
            source_document_id=document_id,
            state=FreshnessState.CURRENT,
            observed_at=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
            critical=True,
        )
        for document_id in document_ids
    )
    return ClassificationEvidence(
        fund_code="000001",
        legal_facts=legal_facts,
        benchmark_facts=benchmark_facts,
        report_facts=report_facts,
        existing_disclosure_facts=disclosure_facts,
        nav_conflicts=tuple(sorted(nav_conflicts)),
        external_evidence_fingerprints=external_evidence_fingerprints,
        external_source_references=external_source_references,
        nav_evidence_fingerprint=nav_evidence_fingerprint,
        nav_observation_start=nav_observation_start,
        nav_observation_end=nav_observation_end,
        freshness=freshness,
        document_ids=document_ids,
        fact_ids=tuple(range(1, len(d1_facts) + 1)),
        parse_result_ids=tuple(range(1, len(document_ids) + 1)),
        parser_provenance_checksums=tuple(
            chr(ord("a") + index) * 64 for index in range(len(document_ids))
        ),
    )


def strict_bond_legal() -> Dict[str, object]:
    return {
        "legal_product_family": "ordinary_bond",
        "stock_exposure_max_percent": D("0"),
        "convertible_bond_exposure_max_percent": D("0"),
        "exchangeable_bond_exposure_max_percent": D("0"),
        "effective_duration_max": D("5"),
        "high_quality_fixed_income_min_percent": D("80"),
        "below_aa_plus_exposure_max_percent": D("0"),
        "unrated_non_sovereign_exposure_max_percent": D("0"),
        "gross_leverage_max_percent": D("120"),
        "single_non_sovereign_issuer_max_percent": D("10"),
        "derivatives_use": "absent",
        "overseas_exposure_max_percent": D("0"),
        "hong_kong_exposure_max_percent": D("0"),
    }


def strict_bond_report() -> Dict[str, object]:
    return {
        "current_stock_asset_allocation_percent": D("0"),
        "current_convertible_bond_asset_allocation_percent": D("0"),
        "current_exchangeable_bond_asset_allocation_percent": D("0"),
        "current_effective_duration": D("5"),
        "current_high_quality_fixed_income_percent": D("80"),
        "current_below_aa_plus_exposure_percent": D("0"),
        "current_unrated_non_sovereign_exposure_percent": D("0"),
        "current_gross_leverage_percent": D("120"),
        "current_largest_non_sovereign_issuer_percent": D("10"),
    }


def broad_benchmark() -> Dict[str, object]:
    return {
        "tracked_index_name": "synthetic broad index",
        "index_methodology_present": True,
        "constituent_count": 300,
        "largest_constituent_weight_max_percent": D("10"),
        "top_ten_constituent_weight_max_percent": D("40"),
        "largest_industry_weight_max_percent": D("35"),
        "industry_count_min": 5,
    }


def complete_equity_report() -> Dict[str, object]:
    return {
        "current_stock_asset_allocation_percent": D("90"),
        "current_largest_security_weight_percent": D("10"),
        "current_top_ten_holdings_weight_percent": D("50"),
        "current_largest_industry_weight_percent": D("40"),
        "current_industry_count": 5,
        "holdings_evidence_complete": True,
        "fee_evidence_present": True,
        "size_evidence_present": True,
        "share_class_evidence_present": True,
    }


def classify(value: ClassificationEvidence):  # type: ignore[no-untyped-def]
    return classify_fund(value, POLICY, NOW)


class ClassificationEvidenceContractTest(unittest.TestCase):
    def test_input_is_immutable(self) -> None:
        value = evidence(legal={"legal_product_type": "fof"})
        with self.assertRaises(FrozenInstanceError):
            value.fund_code = "000002"  # type: ignore[misc]

    def test_external_fingerprints_are_strict_sorted_unique_bindings(self) -> None:
        valid = evidence(
            legal={"legal_product_type": "fof"},
            external_evidence_fingerprints=(
                ("fees", "a" * 64),
                ("holdings", "b" * 64),
            ),
        )
        valid.validate()
        for bindings in (
            (("holdings", "b" * 64), ("fees", "a" * 64)),
            (("fees", "a" * 64), ("fees", "b" * 64)),
            (("Fees", "a" * 64),),
            (("fees", "A" * 64),),
            (("fees", "short"),),
        ):
            with self.subTest(bindings=bindings), self.assertRaises(ValueError):
                replace(valid, external_evidence_fingerprints=bindings).validate()
        with self.assertRaises(ValueError):
            replace(valid, external_evidence_fingerprints=[]).validate()  # type: ignore[arg-type]

    def test_existing_disclosures_are_bound_externally_not_by_d1_ids(self) -> None:
        value = evidence(disclosures={"platform_category": "bond_fund"})
        value.validate()
        self.assertEqual(value.document_ids, ())
        self.assertEqual(value.fact_ids, ())
        self.assertEqual(value.existing_disclosure_facts[0].source_document_id, 4)
        self.assertTrue(value.external_evidence_fingerprints)

    def test_existing_disclosures_require_an_external_fingerprint(self) -> None:
        value = evidence(disclosures={"platform_category": "bond_fund"})
        with self.assertRaises(ValueError):
            replace(value, external_evidence_fingerprints=()).validate()

    def test_external_source_references_are_strict_and_bind_every_disclosure(self) -> None:
        valid = evidence(disclosures={"platform_category": "bond_fund"})
        valid.validate()
        invalid_references = (
            (),
            (external_reference(fund_code="000002"),),
            (external_reference(checksum="E" * 64),),
            (external_reference(url="http://www.fund001.com/public.html"),),
            (external_reference(url="https://user@www.fund001.com/public.html"),),
            (external_reference(url="https://www.fund001.com:444/public.html"),),
            (external_reference(url="https://www.fund001.com/public.html#fragment"),),
            (external_reference(source_namespace="d1_artifact"),),
            (external_reference(), external_reference()),
        )
        for references in invalid_references:
            with self.subTest(references=references), self.assertRaises(ValueError):
                replace(valid, external_source_references=references).validate()

        multiple_sections = (
            external_reference(section="identity"),
            external_reference(),
        )
        replace(
            valid,
            external_evidence_fingerprints=(
                ("identity", "d" * 64),
                ("synthetic_public_evidence", "f" * 64),
            ),
            external_source_references=multiple_sections,
        ).validate()

    def test_d1_facts_still_require_document_and_fact_id_bindings(self) -> None:
        value = evidence(legal={"legal_product_family": "sector_theme"})
        for changes in ({"document_ids": ()}, {"fact_ids": ()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(value, **changes).validate()

    def test_parse_result_and_provenance_bindings_are_strict_sorted_unique_values(self) -> None:
        valid = evidence(legal={"legal_product_family": "sector_theme"})
        valid.validate()
        invalid_changes = (
            {"parse_result_ids": [1]},
            {"parse_result_ids": (2, 1)},
            {"parse_result_ids": (1, 1)},
            {"parse_result_ids": (0,)},
            {"parse_result_ids": (True,)},
            {"parser_provenance_checksums": ["a" * 64]},
            {"parser_provenance_checksums": ("b" * 64, "a" * 64)},
            {"parser_provenance_checksums": ("a" * 64, "a" * 64)},
            {"parser_provenance_checksums": ("A" * 64,)},
            {"parser_provenance_checksums": ("short",)},
            {"parser_provenance_checksums": (True,)},
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes).validate()

    def test_nav_binding_is_all_or_none_and_has_an_ordered_window(self) -> None:
        valid = evidence(
            legal={"legal_product_type": "fof"},
            nav_evidence_fingerprint="c" * 64,
            nav_observation_start=NOW.date() - timedelta(days=30),
            nav_observation_end=NOW.date(),
        )
        valid.validate()
        invalid_changes = (
            {"nav_evidence_fingerprint": None},
            {"nav_observation_start": None},
            {"nav_observation_end": None},
            {"nav_evidence_fingerprint": "C" * 64},
            {"nav_evidence_fingerprint": True},
            {
                "nav_observation_start": NOW.date(),
                "nav_observation_end": NOW.date() - timedelta(days=1),
            },
        )
        for changes in invalid_changes:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(valid, **changes).validate()

    def test_manifest_is_public_canonical_and_fact_order_independent(self) -> None:
        first_fact = fact("legal_product_family", "ordinary_bond")
        second_fact = fact("stock_exposure_max_percent", D("0"))
        base = evidence(
            legal={"legal_product_family": "ordinary_bond"},
            external_evidence_fingerprints=(("identity", "d" * 64),),
            nav_evidence_fingerprint="e" * 64,
            nav_observation_start=NOW.date() - timedelta(days=7),
            nav_observation_end=NOW.date(),
        )
        forward = replace(base, legal_facts=(first_fact, second_fact), fact_ids=(1, 2))
        reverse = replace(base, legal_facts=(second_fact, first_fact), fact_ids=(1, 2))
        first = classification_input_manifest(forward, POLICY, NOW)
        second = classification_input_manifest(reverse, POLICY, NOW)
        self.assertEqual(first, second)
        self.assertEqual(
            first["external_evidence_fingerprints"],
            [["identity", "d" * 64]],
        )
        self.assertEqual(
            first["nav_observation_start"],
            (NOW.date() - timedelta(days=7)).isoformat(),
        )
        self.assertEqual(first["nav_observation_end"], NOW.date().isoformat())
        self.assertNotIn("managed_path", json.dumps(first, sort_keys=True))

    def test_manifest_v1_preserves_the_exact_legacy_bytes_and_fingerprint(self) -> None:
        value = evidence(legal={"legal_product_type": "fof"})

        manifest = classification_input_manifest_v1(value, POLICY, NOW)
        encoded = json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

        self.assertEqual(
            set(manifest),
            {
                "benchmark_facts",
                "classified_at",
                "document_ids",
                "existing_disclosure_facts",
                "external_evidence_fingerprints",
                "external_source_references",
                "fact_ids",
                "freshness",
                "fund_code",
                "legal_facts",
                "nav_conflicts",
                "nav_evidence_fingerprint",
                "nav_observation_end",
                "nav_observation_start",
                "policy_checksum",
                "policy_version",
                "report_facts",
            },
        )
        self.assertEqual(len(encoded), 1084)
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            "6397a3388370b9b9b3bf45529462616040788c647a211cd289f27d3513ce210c",
        )

    def test_manifest_v2_has_exact_result_and_provenance_bindings(self) -> None:
        value = evidence(legal={"legal_product_type": "fof"})

        legacy = classification_input_manifest_v1(value, POLICY, NOW)
        current = classification_input_manifest_v2(value, POLICY, NOW)

        self.assertEqual(classification_input_manifest(value, POLICY, NOW), current)
        self.assertEqual(
            set(current),
            set(legacy)
            | {
                "manifest_version",
                "parse_result_ids",
                "parser_provenance_checksums",
            },
        )
        self.assertEqual(current["manifest_version"], 2)
        self.assertEqual(current["parse_result_ids"], [1])
        self.assertEqual(current["parser_provenance_checksums"], ["a" * 64])
        self.assertEqual(
            {key: value for key, value in current.items() if key in legacy},
            legacy,
        )

    def test_v2_result_and_provenance_bindings_change_the_input_fingerprint(self) -> None:
        base = evidence(legal={"legal_product_family": "sector_theme"})
        changed_result = replace(base, parse_result_ids=(2,))
        changed_provenance = replace(base, parser_provenance_checksums=("b" * 64,))

        baseline = classify(base)
        self.assertNotEqual(classify(changed_result).input_fingerprint, baseline.input_fingerprint)
        self.assertNotEqual(
            classify(changed_provenance).input_fingerprint,
            baseline.input_fingerprint,
        )

    def test_manifest_binds_canonical_external_source_payload(self) -> None:
        value = evidence(disclosures={"platform_category": "bond_fund"})

        manifest = classification_input_manifest(value, POLICY, NOW)

        self.assertEqual(len(manifest["external_source_references"]), 1)
        source = manifest["external_source_references"][0]
        self.assertEqual(source["source_namespace"], "fund_disclosure")
        self.assertEqual(source["source_document_id"], 4)
        self.assertEqual(source["published_at"], (NOW - timedelta(days=2)).isoformat())
        self.assertEqual(source["checksum"], "e" * 64)

    def test_external_or_nav_binding_changes_fingerprint_not_classification_rank(self) -> None:
        base = evidence(
            legal={"legal_product_family": "sector_theme"},
            external_evidence_fingerprints=(("identity", "1" * 64),),
            nav_evidence_fingerprint="2" * 64,
            nav_observation_start=NOW.date() - timedelta(days=7),
            nav_observation_end=NOW.date(),
        )
        changed_external = replace(
            base,
            external_evidence_fingerprints=(("identity", "3" * 64),),
        )
        changed_nav = replace(base, nav_evidence_fingerprint="4" * 64)
        changed_nav_window = replace(
            base,
            nav_observation_start=NOW.date() - timedelta(days=8),
        )
        baseline = classify(base)
        for changed in (changed_external, changed_nav, changed_nav_window):
            result = classify(changed)
            self.assertNotEqual(result.input_fingerprint, baseline.input_fingerprint)
            self.assertEqual(
                conservative_classification_rank(result),
                conservative_classification_rank(baseline),
            )

    def test_existing_disclosure_payload_is_in_manifest_and_fingerprint(self) -> None:
        base = evidence(disclosures={"platform_category": "bond_fund"})
        changed = replace(
            base,
            existing_disclosure_facts=(fact("platform_category", "equity_fund", document_id=4),),
        )
        base_manifest = classification_input_manifest(base, POLICY, NOW)
        changed_manifest = classification_input_manifest(changed, POLICY, NOW)
        self.assertNotEqual(
            base_manifest["existing_disclosure_facts"],
            changed_manifest["existing_disclosure_facts"],
        )
        self.assertNotEqual(classify(base).input_fingerprint, classify(changed).input_fingerprint)


class FailClosedClassificationTest(unittest.TestCase):
    def test_explicit_unsupported_family_is_a_successful_factual_result(self) -> None:
        result = classify_fund(
            evidence(legal={"legal_product_type": "fof"}),
            POLICY,
            NOW,
        )
        self.assertEqual(result.product_family, ProductFamily.UNSUPPORTED)
        self.assertEqual(result.risk_bucket, RiskBucket.UNCLASSIFIED)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertEqual(result.evidence_status, EvidenceStatus.UNCLASSIFIED)
        self.assertIn("unsupported_product_family", result.reason_codes)

    def test_missing_formal_scope_is_not_mislabeled_as_unsupported(self) -> None:
        result = classify_fund(
            evidence(disclosures={"platform_category": "bond_fund"}),
            POLICY,
            NOW,
        )
        self.assertEqual(result.product_family, ProductFamily.UNCLASSIFIED)
        self.assertNotIn("unsupported_product_family", result.reason_codes)
        self.assertIn("official_scope_missing", result.reason_codes)
        self.assertIn("legal_product_family_evidence_missing", result.missing_evidence)

    def test_all_strict_bond_gates_pass_at_the_policy_boundaries(self) -> None:
        result = classify_fund(
            evidence(legal=strict_bond_legal(), report=strict_bond_report()),
            POLICY,
            NOW,
        )
        self.assertEqual(result.product_family, ProductFamily.ORDINARY_BOND)
        self.assertEqual(result.risk_bucket, RiskBucket.HIGH_QUALITY_FIXED_INCOME)
        self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)


class ProductFamilyMatrixTest(unittest.TestCase):
    def test_parsed_sector_summary_reaches_verified_sector_classification(self) -> None:
        legal_facts = parsed_facts(
            FIXTURES / "sector-fund-summary.html",
            document_kind=DocumentKind.PRODUCT_SUMMARY,
            document_id=1,
        )
        result = classify(parsed_evidence(legal_facts=legal_facts))
        self.assertEqual(result.product_family, ProductFamily.SECTOR_THEME)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)
        self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)
        self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)

    def test_parsed_broad_methodology_can_identify_broad_family_but_not_eligibility(self) -> None:
        benchmark_facts = parsed_facts(
            FIXTURES / "broad-index-methodology.html",
            document_kind=DocumentKind.INDEX_METHODOLOGY,
            document_id=2,
        )
        result = classify(parsed_evidence(benchmark_facts=benchmark_facts))
        self.assertEqual(result.product_family, ProductFamily.BROAD_INDEX)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertEqual(result.evidence_status, EvidenceStatus.PARTIAL)
        self.assertIn("holdings_evidence_missing", result.missing_evidence)

    def test_unclassified_family_requires_explicit_broad_methodology_identity(self) -> None:
        complete = {**broad_benchmark(), "sector_theme_mandate": "absent"}
        cases = (
            ("missing_methodology", {**complete, "index_methodology_present": False}),
            (
                "broad_scope_not_explicit",
                {kind: value for kind, value in complete.items() if kind != "sector_theme_mandate"},
            ),
            (
                "tracked_index_missing",
                {kind: value for kind, value in complete.items() if kind != "tracked_index_name"},
            ),
        )
        for label, benchmark in cases:
            with self.subTest(label=label):
                result = classify(evidence(benchmark=benchmark, report=complete_equity_report()))
                self.assertEqual(result.product_family, ProductFamily.UNCLASSIFIED)
                self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)

        inferred = classify(evidence(benchmark=complete, report=complete_equity_report()))
        self.assertEqual(inferred.product_family, ProductFamily.BROAD_INDEX)
        self.assertEqual(inferred.portfolio_role, PortfolioRole.CORE_ELIGIBLE)

    def test_formal_theme_methodology_never_infers_broad_family(self) -> None:
        result = classify(
            evidence(
                benchmark={
                    **broad_benchmark(),
                    "sector_theme_mandate": "present",
                }
            )
        )
        self.assertEqual(result.product_family, ProductFamily.SECTOR_THEME)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)
        self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)

    def test_index_legal_types_still_require_all_index_gates(self) -> None:
        broad = classify(
            evidence(
                legal={"legal_product_type": "index_fund"},
                benchmark={**broad_benchmark(), "sector_theme_mandate": "absent"},
                report=complete_equity_report(),
            )
        )
        enhanced = classify(
            evidence(
                legal={
                    "legal_product_type": "index_enhanced_fund",
                    "enhancement_limits_present": True,
                },
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
            )
        )
        missing_methodology = classify(
            evidence(
                legal={"legal_product_type": "index_fund"},
                benchmark={
                    kind: value
                    for kind, value in {
                        **broad_benchmark(),
                        "sector_theme_mandate": "absent",
                    }.items()
                    if kind != "index_methodology_present"
                },
                report=complete_equity_report(),
            )
        )
        missing_scope = classify(
            evidence(
                legal={"legal_product_type": "index_fund"},
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
            )
        )
        theme = classify(
            evidence(
                legal={"legal_product_type": "index_fund"},
                benchmark={**broad_benchmark(), "sector_theme_mandate": "present"},
            )
        )
        self.assertEqual(broad.product_family, ProductFamily.BROAD_INDEX)
        self.assertEqual(broad.portfolio_role, PortfolioRole.CORE_ELIGIBLE)
        self.assertEqual(enhanced.product_family, ProductFamily.INDEX_ENHANCED)
        self.assertEqual(enhanced.portfolio_role, PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE)
        self.assertEqual(missing_methodology.product_family, ProductFamily.UNCLASSIFIED)
        self.assertEqual(missing_methodology.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertEqual(missing_scope.product_family, ProductFamily.UNCLASSIFIED)
        self.assertEqual(missing_scope.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertEqual(theme.product_family, ProductFamily.SECTOR_THEME)
        self.assertEqual(theme.portfolio_role, PortfolioRole.SATELLITE_ONLY)

    def test_supported_product_family_matrix(self) -> None:
        strict_legal = strict_bond_legal()
        strict_report = strict_bond_report()
        broad_report = complete_equity_report()
        cases = (
            (
                "money_market",
                evidence(
                    legal={
                        "legal_product_family": "money_market",
                        "stock_exposure_max_percent": D("0"),
                        "convertible_bond_exposure_max_percent": D("0"),
                        "exchangeable_bond_exposure_max_percent": D("0"),
                        "derivatives_use": "absent",
                        "redemption_restriction": "daily_open",
                        "lockup_restriction": "absent",
                    }
                ),
                ProductFamily.MONEY_MARKET,
                RiskBucket.CASH_LIKE_CANDIDATE,
                PortfolioRole.CASH_MANAGEMENT_CANDIDATE,
            ),
            *(
                (
                    family.value,
                    evidence(
                        legal={**strict_legal, "legal_product_family": family.value},
                        report=strict_report,
                    ),
                    family,
                    RiskBucket.HIGH_QUALITY_FIXED_INCOME,
                    PortfolioRole.NOT_ELIGIBLE,
                )
                for family in (
                    ProductFamily.SHORT_BOND,
                    ProductFamily.INTERMEDIATE_BOND,
                    ProductFamily.ORDINARY_BOND,
                )
            ),
            *(
                (
                    family.value,
                    evidence(legal={"legal_product_family": family.value}),
                    family,
                    RiskBucket.HYBRID_RISK,
                    PortfolioRole.NOT_ELIGIBLE,
                )
                for family in (
                    ProductFamily.LONG_BOND,
                    ProductFamily.CREDIT_BOND,
                    ProductFamily.CONVERTIBLE_BOND,
                    ProductFamily.FIXED_INCOME_PLUS,
                    ProductFamily.BOND_MIXED,
                )
            ),
            (
                "broad_index",
                evidence(
                    legal={"legal_product_family": "broad_index"},
                    benchmark=broad_benchmark(),
                    report=broad_report,
                ),
                ProductFamily.BROAD_INDEX,
                RiskBucket.DIVERSIFIED_EQUITY,
                PortfolioRole.CORE_ELIGIBLE,
            ),
            (
                "index_enhanced",
                evidence(
                    legal={
                        "legal_product_family": "index_enhanced",
                        "enhancement_limits_present": True,
                    },
                    benchmark=broad_benchmark(),
                    report=broad_report,
                ),
                ProductFamily.INDEX_ENHANCED,
                RiskBucket.DIVERSIFIED_EQUITY,
                PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE,
            ),
            (
                "sector_theme",
                evidence(legal={"legal_product_family": "sector_theme"}),
                ProductFamily.SECTOR_THEME,
                RiskBucket.CONCENTRATED_EQUITY,
                PortfolioRole.SATELLITE_ONLY,
            ),
            *(
                (
                    family.value,
                    evidence(
                        legal={"legal_product_family": family.value},
                        report=complete_equity_report(),
                    ),
                    family,
                    RiskBucket.DIVERSIFIED_EQUITY,
                    PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE,
                )
                for family in (ProductFamily.ACTIVE_EQUITY, ProductFamily.EQUITY_MIXED)
            ),
            (
                "qdii_broad_equity",
                evidence(
                    legal={"legal_product_family": "qdii_broad_equity"},
                    benchmark=broad_benchmark(),
                    report=broad_report,
                ),
                ProductFamily.QDII_BROAD_EQUITY,
                RiskBucket.DIVERSIFIED_EQUITY,
                PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE,
            ),
            (
                "qdii_sector_theme",
                evidence(legal={"legal_product_family": "qdii_sector_theme"}),
                ProductFamily.QDII_SECTOR_THEME,
                RiskBucket.CONCENTRATED_EQUITY,
                PortfolioRole.SATELLITE_ONLY,
            ),
        )
        for label, input_evidence, family, bucket, role in cases:
            with self.subTest(label=label):
                result = classify(input_evidence)
                self.assertEqual(result.product_family, family)
                self.assertEqual(result.risk_bucket, bucket)
                self.assertEqual(result.portfolio_role, role)
                self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)

    def test_all_explicitly_unsupported_legal_types_stay_factual(self) -> None:
        for legal_type in (
            "fof",
            "commodity_fund",
            "public_reit",
            "leveraged_fund",
            "structured_fund",
            "unsupported_qdii",
        ):
            with self.subTest(legal_type=legal_type):
                result = classify(evidence(legal={"legal_product_type": legal_type}))
                self.assertEqual(result.product_family, ProductFamily.UNSUPPORTED)
                self.assertEqual(result.evidence_status, EvidenceStatus.UNCLASSIFIED)
                self.assertIn("unsupported_product_family", result.reason_codes)

    def test_index_enhanced_never_receives_core_eligibility(self) -> None:
        result = classify(
            evidence(
                legal={
                    "legal_product_family": "index_enhanced",
                    "enhancement_limits_present": True,
                },
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
            )
        )
        self.assertEqual(result.risk_bucket, RiskBucket.DIVERSIFIED_EQUITY)
        self.assertEqual(result.portfolio_role, PortfolioRole.ACTIVE_DIVERSIFIER_ELIGIBLE)

    def test_formal_theme_paths_override_generic_equity_identity(self) -> None:
        paths = (
            {"sector_theme_mandate": "present"},
            {"theme_exposure_min_percent": D("80")},
        )
        for theme_facts in paths:
            with self.subTest(theme_facts=theme_facts):
                result = classify(
                    evidence(
                        legal={"legal_product_type": "equity_fund", **theme_facts},
                        report=complete_equity_report(),
                    )
                )
                self.assertEqual(result.product_family, ProductFamily.SECTOR_THEME)
                self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)

    def test_complete_current_industry_concentration_can_identify_theme(self) -> None:
        result = classify(
            evidence(
                legal={"legal_product_type": "equity_fund"},
                report={
                    **complete_equity_report(),
                    "industry_evidence_complete": True,
                    "industry_concentration_consistent_with_mandate": True,
                    "current_largest_industry_weight_percent": D("50"),
                },
            )
        )
        self.assertEqual(result.product_family, ProductFamily.SECTOR_THEME)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)

    def test_industry_concentration_without_mandate_consistency_does_not_define_theme(self) -> None:
        result = classify(
            evidence(
                legal={"legal_product_type": "equity_fund"},
                report={
                    **complete_equity_report(),
                    "industry_evidence_complete": True,
                    "current_largest_industry_weight_percent": D("50"),
                },
            )
        )
        self.assertEqual(result.product_family, ProductFamily.ACTIVE_EQUITY)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)

    def test_formal_sector_index_scope_cannot_be_promoted_to_broad(self) -> None:
        benchmark = broad_benchmark()
        benchmark["index_scope"] = "sector_theme"
        result = classify(
            evidence(
                legal={"legal_product_family": "broad_index"},
                benchmark=benchmark,
                report=complete_equity_report(),
            )
        )
        self.assertEqual(result.product_family, ProductFamily.SECTOR_THEME)
        self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)


class DowngradeAndConflictTest(unittest.TestCase):
    def test_missing_active_concentration_evidence_is_partial_and_not_eligible(self) -> None:
        report = complete_equity_report()
        del report["current_industry_count"]
        result = classify(evidence(legal={"legal_product_family": "active_equity"}, report=report))
        self.assertEqual(result.evidence_status, EvidenceStatus.PARTIAL)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertIn("industry_count_evidence_missing", result.missing_evidence)

    def test_critical_stale_evidence_closes_verified_roles(self) -> None:
        current = evidence(
            legal={"legal_product_family": "broad_index"},
            benchmark=broad_benchmark(),
            report=complete_equity_report(),
        )
        stale = replace(
            current,
            freshness=tuple(
                replace(item, state=FreshnessState.STALE) if item.critical else item
                for item in current.freshness
            ),
        )
        result = classify(stale)
        self.assertEqual(result.evidence_status, EvidenceStatus.STALE)
        self.assertEqual(result.risk_bucket, RiskBucket.UNCLASSIFIED)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertIn("critical_evidence_stale", result.reason_codes)

    def test_current_state_with_expired_deadline_is_stale(self) -> None:
        current = evidence(
            legal={"legal_product_family": "sector_theme"},
        )
        expired = replace(
            current,
            freshness=tuple(
                replace(
                    item,
                    observed_at=NOW - timedelta(days=3),
                    valid_until=NOW - timedelta(days=1),
                )
                for item in current.freshness
            ),
        )
        self.assertEqual(classify(expired).evidence_status, EvidenceStatus.STALE)

    def test_invalidated_critical_evidence_is_stale(self) -> None:
        current = evidence(legal={"legal_product_family": "sector_theme"})
        invalidated = replace(
            current,
            freshness=tuple(
                replace(item, state=FreshnessState.INVALIDATED) for item in current.freshness
            ),
        )
        self.assertEqual(classify(invalidated).evidence_status, EvidenceStatus.STALE)

    def test_missing_freshness_is_partial_and_not_eligible(self) -> None:
        current = evidence(legal={"legal_product_family": "sector_theme"})
        result = classify(replace(current, freshness=()))
        self.assertEqual(result.evidence_status, EvidenceStatus.PARTIAL)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertIn("freshness_evidence_missing", result.missing_evidence)

    def test_freshness_must_cover_every_bound_document(self) -> None:
        current = evidence(
            legal={"legal_product_family": "broad_index"},
            benchmark=broad_benchmark(),
            report=complete_equity_report(),
        )
        incomplete = replace(current, freshness=current.freshness[:-1])
        result = classify(incomplete)
        self.assertEqual(result.evidence_status, EvidenceStatus.PARTIAL)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)
        self.assertIn("freshness_evidence_missing", result.missing_evidence)

    def test_tier_one_conflict_blocks_verified_role(self) -> None:
        result = classify(
            evidence(
                legal={"legal_product_family": "broad_index"},
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
                nav_conflicts=("benchmark_conflicts_with_mandate",),
            )
        )
        self.assertEqual(result.evidence_status, EvidenceStatus.CONFLICTED)
        self.assertEqual(result.portfolio_role, PortfolioRole.NOT_ELIGIBLE)

    def test_name_and_platform_conflicts_are_warnings_only(self) -> None:
        result = classify(
            evidence(
                legal={"legal_product_family": "sector_theme"},
                nav_conflicts=(
                    "name_conflicts_with_formal_scope",
                    "platform_category_conflicts_with_formal_scope",
                ),
            )
        )
        self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)
        self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)

    def test_identity_scope_warnings_are_derived_from_strong_external_signals(self) -> None:
        baseline = classify(evidence(legal={"legal_product_family": "sector_theme"}))
        result = classify(
            evidence(
                legal={"legal_product_family": "sector_theme"},
                disclosures={
                    "fund_name": "公开货币基金",
                    "platform_category": "债券型",
                },
            )
        )
        self.assertEqual(
            result.conflicts,
            (
                "name_conflicts_with_formal_scope",
                "platform_category_conflicts_with_formal_scope",
            ),
        )
        self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)
        self.assertEqual(result.product_family, baseline.product_family)
        self.assertEqual(result.risk_bucket, baseline.risk_bucket)
        self.assertEqual(result.portfolio_role, baseline.portfolio_role)
        self.assertEqual(
            conservative_classification_rank(result),
            conservative_classification_rank(baseline),
        )

    def test_compatible_chinese_and_english_identity_labels_do_not_warn(self) -> None:
        cases = (
            ({"legal_product_family": "money_market"}, "现金宝货币", "Money Market"),
            ({"legal_product_family": "ordinary_bond"}, "稳健纯债", "Bond Fund"),
            ({"legal_product_family": "fixed_income_plus"}, "固收增强债券", "混合型-偏债"),
            ({"legal_product_family": "equity_mixed"}, "价值混合", "Mixed Fund"),
            ({"legal_product_family": "sector_theme"}, "医药主题混合", "混合型"),
            ({"legal_product_family": "broad_index"}, "债券ETF", "债券型-指数型"),
            ({"legal_product_family": "qdii_broad_equity"}, "Global QDII Index", "QDII"),
            ({"legal_product_type": "fof"}, "基金中基金（FOF）", "Fund of Funds"),
        )
        for legal, fund_name, platform_category in cases:
            with self.subTest(legal=legal):
                result = classify(
                    evidence(
                        legal=legal,
                        disclosures={
                            "fund_name": fund_name,
                            "platform_category": platform_category,
                        },
                    )
                )
                self.assertNotIn("name_conflicts_with_formal_scope", result.conflicts)
                self.assertNotIn(
                    "platform_category_conflicts_with_formal_scope",
                    result.conflicts,
                )

    def test_unknown_or_formally_unscoped_identity_labels_do_not_warn(self) -> None:
        cases = (
            evidence(
                legal={"legal_product_family": "sector_theme"},
                disclosures={
                    "fund_name": "稳健成长精选",
                    "platform_category": "其他",
                },
            ),
            evidence(
                disclosures={
                    "fund_name": "现金宝货币",
                    "platform_category": "债券型",
                }
            ),
        )
        for value in cases:
            with self.subTest(value=value):
                result = classify(value)
                self.assertNotIn("name_conflicts_with_formal_scope", result.conflicts)
                self.assertNotIn(
                    "platform_category_conflicts_with_formal_scope",
                    result.conflicts,
                )

    def test_nav_behavior_can_downgrade_but_never_promote(self) -> None:
        unknown = evidence(disclosures={"platform_category": "bond_fund"})
        stable = evidence(
            disclosures={"platform_category": "bond_fund", "stable_nav_behavior": True}
        )
        self.assertEqual(classify(stable).risk_bucket, classify(unknown).risk_bucket)

        conflicted = classify(
            evidence(
                legal=strict_bond_legal(),
                report=strict_bond_report(),
                nav_conflicts=("nav_behavior_conflicts_with_declared_scope",),
            )
        )
        self.assertEqual(conflicted.evidence_status, EvidenceStatus.CONFLICTED)
        self.assertEqual(conflicted.risk_bucket, RiskBucket.HYBRID_RISK)

    def test_lower_tier_scope_cannot_replace_formal_scope(self) -> None:
        formal = classify(evidence(legal={"legal_product_family": "sector_theme"}))
        lower_tier = classify(evidence(disclosures={"legal_product_family": "sector_theme"}))
        self.assertLessEqual(
            conservative_classification_rank(lower_tier),
            conservative_classification_rank(formal),
        )
        self.assertEqual(lower_tier.product_family, ProductFamily.UNCLASSIFIED)

    def test_conflicting_fact_order_cannot_change_the_financial_result(self) -> None:
        ordinary = fact("legal_product_family", "ordinary_bond")
        money = fact("legal_product_family", "money_market")
        base = evidence(legal={"legal_product_family": "ordinary_bond"})
        forward = replace(base, legal_facts=(ordinary, money), fact_ids=(1, 2))
        reversed_input = replace(base, legal_facts=(money, ordinary), fact_ids=(1, 2))

        first = classify(forward)
        second = classify(reversed_input)
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.product_family, second.product_family)
        self.assertEqual(first.risk_bucket, second.risk_bucket)
        self.assertEqual(first.portfolio_role, second.portfolio_role)
        self.assertEqual(first.evidence_status, second.evidence_status)
        self.assertEqual(first.reason_codes, second.reason_codes)
        self.assertEqual(first.conflicts, second.conflicts)
        self.assertIn("source_version_conflict", first.conflicts)
        self.assertEqual(first.evidence_status, EvidenceStatus.CONFLICTED)

    def test_observed_mandate_breaches_add_stable_conflict_codes(self) -> None:
        cases = (
            (
                "current_stock_asset_allocation_percent",
                D("0.01"),
                "equity_exposure_conflict",
            ),
            (
                "current_convertible_bond_asset_allocation_percent",
                D("0.01"),
                "convertible_exposure_conflict",
            ),
            ("current_effective_duration", D("5.01"), "duration_conflict"),
            (
                "current_high_quality_fixed_income_percent",
                D("79.99"),
                "credit_quality_conflict",
            ),
            ("current_gross_leverage_percent", D("120.01"), "leverage_conflict"),
        )
        for kind, value, expected_conflict in cases:
            with self.subTest(kind=kind):
                report = strict_bond_report()
                report[kind] = value
                result = classify(evidence(legal=strict_bond_legal(), report=report))
                self.assertIn(expected_conflict, result.conflicts)
                self.assertEqual(result.evidence_status, EvidenceStatus.CONFLICTED)


class EvidenceTagTest(unittest.TestCase):
    def qdii(self, **legal_changes: object):  # type: ignore[no-untyped-def]
        return classify(
            evidence(
                legal={
                    "legal_product_family": "qdii_broad_equity",
                    **legal_changes,
                },
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
            )
        )

    def test_qdii_and_hong_kong_tags_are_sorted_unique_public_facts(self) -> None:
        result = self.qdii(hong_kong_exposure_max_percent=D("80"))
        self.assertEqual(result.evidence_tags, ("foreign_currency", "hong_kong_equity"))

    def test_credit_convertible_and_bond_scope_tags_require_explicit_facts(self) -> None:
        cases = (
            (
                evidence(legal={"legal_product_family": "credit_bond"}),
                ("credit_exposure",),
            ),
            (
                evidence(legal={"legal_product_family": "convertible_bond"}),
                ("convertible_exposure",),
            ),
            (
                evidence(
                    legal={
                        "legal_product_family": "sector_theme",
                        "interest_rate_bond_mandate": "present",
                        "policy_bank_bond_mandate": True,
                    }
                ),
                ("interest_rate_bond", "policy_bank_bond"),
            ),
        )
        for input_evidence, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify(input_evidence).evidence_tags, expected)

    def test_unrelated_facts_do_not_create_evidence_tags(self) -> None:
        result = classify(
            evidence(
                legal={"legal_product_family": "sector_theme"},
                disclosures={"stable_nav_behavior": True},
            )
        )
        self.assertEqual(result.evidence_tags, ())

    def test_tags_do_not_change_the_conservative_classification_rank(self) -> None:
        baseline = self.qdii()
        tagged = self.qdii(hong_kong_exposure_max_percent=D("80"))
        self.assertNotEqual(baseline.evidence_tags, tagged.evidence_tags)
        self.assertEqual(
            conservative_classification_rank(baseline),
            conservative_classification_rank(tagged),
        )


class ConservativeMonotonicityTest(unittest.TestCase):
    def assert_not_improved(self, risky, baseline) -> None:  # type: ignore[no-untyped-def]
        self.assertLessEqual(
            conservative_classification_rank(risky),
            conservative_classification_rank(baseline),
        )

    def test_strict_bond_risk_grid_never_improves_the_result(self) -> None:
        baseline = classify(evidence(legal=strict_bond_legal(), report=strict_bond_report()))
        cases = (
            ("stock_exposure_max_percent", "legal", D("0.01")),
            ("current_stock_asset_allocation_percent", "report", D("0.01")),
            ("convertible_bond_exposure_max_percent", "legal", D("0.01")),
            ("current_convertible_bond_asset_allocation_percent", "report", D("0.01")),
            ("current_effective_duration", "report", D("5.01")),
            ("current_high_quality_fixed_income_percent", "report", D("79.99")),
            ("current_below_aa_plus_exposure_percent", "report", D("0.01")),
            ("current_unrated_non_sovereign_exposure_percent", "report", D("0.01")),
            ("current_gross_leverage_percent", "report", D("120.01")),
            ("current_largest_non_sovereign_issuer_percent", "report", D("10.01")),
        )
        for kind, group, value in cases:
            with self.subTest(kind=kind):
                legal = strict_bond_legal()
                report = strict_bond_report()
                (legal if group == "legal" else report)[kind] = value
                self.assert_not_improved(classify(evidence(legal=legal, report=report)), baseline)

    def test_broad_index_boundary_grid_never_improves_the_result(self) -> None:
        baseline = classify(
            evidence(
                legal={"legal_product_family": "broad_index"},
                benchmark=broad_benchmark(),
                report=complete_equity_report(),
            )
        )
        cases = (
            ("constituent_count", 99),
            ("largest_constituent_weight_max_percent", D("10.01")),
            ("top_ten_constituent_weight_max_percent", D("40.01")),
            ("largest_industry_weight_max_percent", D("35.01")),
            ("industry_count_min", 4),
        )
        for kind, value in cases:
            with self.subTest(kind=kind):
                benchmark = broad_benchmark()
                benchmark[kind] = value
                risky = classify(
                    evidence(
                        legal={"legal_product_family": "broad_index"},
                        benchmark=benchmark,
                        report=complete_equity_report(),
                    )
                )
                self.assert_not_improved(risky, baseline)

    def test_active_concentration_grid_never_improves_the_result(self) -> None:
        baseline = classify(
            evidence(
                legal={"legal_product_family": "active_equity"},
                report=complete_equity_report(),
            )
        )
        cases = (
            ("current_largest_security_weight_percent", D("10.01")),
            ("current_top_ten_holdings_weight_percent", D("50.01")),
            ("current_largest_industry_weight_percent", D("40.01")),
            ("current_industry_count", 4),
        )
        for kind, value in cases:
            with self.subTest(kind=kind):
                report = complete_equity_report()
                report[kind] = value
                risky = classify(
                    evidence(
                        legal={"legal_product_family": "active_equity"},
                        report=report,
                    )
                )
                self.assert_not_improved(risky, baseline)

    def test_removing_each_critical_bond_fact_never_improves_the_result(self) -> None:
        baseline = classify(evidence(legal=strict_bond_legal(), report=strict_bond_report()))
        for group_name, original in (
            ("legal", strict_bond_legal()),
            ("report", strict_bond_report()),
        ):
            for kind in original:
                if kind == "legal_product_family":
                    continue
                with self.subTest(group=group_name, kind=kind):
                    legal = strict_bond_legal()
                    report = strict_bond_report()
                    del (legal if group_name == "legal" else report)[kind]
                    self.assert_not_improved(
                        classify(evidence(legal=legal, report=report)),
                        baseline,
                    )

    def test_missing_one_strict_bond_gate_never_passes_high_quality(self) -> None:
        report = strict_bond_report()
        del report["current_largest_non_sovereign_issuer_percent"]
        result = classify_fund(evidence(legal=strict_bond_legal(), report=report), POLICY, NOW)
        self.assertNotEqual(result.risk_bucket, RiskBucket.HIGH_QUALITY_FIXED_INCOME)
        self.assertIn("issuer_concentration_evidence_missing", result.missing_evidence)

    def test_verified_broad_index_can_fail_core_eligibility(self) -> None:
        result = classify_fund(
            evidence(
                legal={"legal_product_family": "broad_index"},
                benchmark={
                    "tracked_index_name": "synthetic broad index",
                    "index_methodology_present": True,
                    "constituent_count": 300,
                    "largest_constituent_weight_max_percent": D("10"),
                    "top_ten_constituent_weight_max_percent": D("40"),
                    "largest_industry_weight_max_percent": D("35.01"),
                    "industry_count_min": 5,
                },
                report={
                    "holdings_evidence_complete": True,
                    "fee_evidence_present": True,
                    "size_evidence_present": True,
                    "share_class_evidence_present": True,
                },
            ),
            POLICY,
            NOW,
        )
        self.assertEqual(result.product_family, ProductFamily.BROAD_INDEX)
        self.assertEqual(result.risk_bucket, RiskBucket.CONCENTRATED_EQUITY)
        self.assertEqual(result.portfolio_role, PortfolioRole.SATELLITE_ONLY)
        self.assertEqual(result.evidence_status, EvidenceStatus.VERIFIED)


if __name__ == "__main__":
    unittest.main()
