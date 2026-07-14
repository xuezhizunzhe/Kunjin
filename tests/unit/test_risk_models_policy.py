from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from kunjin.funds.risk.models import (
    EvidenceFreshness,
    EvidenceStatus,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    FundRiskClassification,
    MandateFact,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
)
from kunjin.funds.risk.policy import (
    CLASSIFICATION_CONFLICT_CODES,
    CLASSIFICATION_FINANCIAL_CODES,
    CLASSIFICATION_POLICY_V1_CHECKSUM,
    ClassificationPolicyV1,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def mandate_fact(**changes: object) -> MandateFact:
    values = {
        "fund_code": "519755",
        "fact_kind": "stock_exposure_max_percent",
        "normalized_value": "0",
        "unit": "percent",
        "source_document_id": 1,
        "page_number": 8,
        "section_name": "investment_scope",
        "source_excerpt": "The fund does not invest in stocks.",
        "effective_from": date(2026, 1, 1),
        "effective_to": None,
        "confidence_state": FactConfidence.EXACT,
        "parser_version": "1",
        "fact_fingerprint": "a" * 64,
    }
    values.update(changes)
    return MandateFact(**values)


def classification(**changes: object) -> FundRiskClassification:
    freshness = EvidenceFreshness(
        section="legal_scope",
        source_document_id=1,
        state=FreshnessState.CURRENT,
        observed_at=NOW,
        valid_until=datetime(2027, 7, 13, 12, 0, tzinfo=timezone.utc),
        critical=True,
    )
    values = {
        "fund_code": "519755",
        "policy_version": "1",
        "input_fingerprint": "b" * 64,
        "product_family": ProductFamily.BROAD_INDEX,
        "risk_bucket": RiskBucket.DIVERSIFIED_EQUITY,
        "portfolio_role": PortfolioRole.CORE_ELIGIBLE,
        "evidence_status": EvidenceStatus.VERIFIED,
        "evidence_tags": ("credit_exposure", "foreign_currency", "hong_kong_equity"),
        "reason_codes": ("classification_verified",),
        "missing_evidence": (),
        "conflicts": (),
        "evidence_document_ids": (1, 2),
        "evidence_fact_ids": (4, 7),
        "freshness": (freshness,),
        "classified_at": NOW,
        "valid_until": datetime(2027, 7, 13, 12, 0, tzinfo=timezone.utc),
    }
    values.update(changes)
    return FundRiskClassification(**values)


def external_source(**changes: object) -> ExternalSourceReference:
    values = {
        "source_namespace": "fund_disclosure",
        "source_document_id": 9,
        "fund_code": "519755",
        "document_kind": "fee_schedule",
        "section": "fees",
        "title": "public fee schedule",
        "url": "https://www.fund001.com/fees.html",
        "source_name": "official_public_source",
        "source_tier": 1,
        "publisher": "交银施罗德基金管理有限公司",
        "published_at": NOW - timedelta(days=2),
        "retrieved_at": NOW - timedelta(days=1),
        "checksum": "c" * 64,
    }
    values.update(changes)
    return ExternalSourceReference(**values)


class RiskEnumTest(unittest.TestCase):
    def test_public_enum_values_are_stable(self) -> None:
        self.assertEqual(
            tuple(item.value for item in ProductFamily),
            (
                "money_market",
                "short_bond",
                "intermediate_bond",
                "ordinary_bond",
                "long_bond",
                "credit_bond",
                "convertible_bond",
                "fixed_income_plus",
                "bond_mixed",
                "broad_index",
                "index_enhanced",
                "sector_theme",
                "active_equity",
                "equity_mixed",
                "qdii_broad_equity",
                "qdii_sector_theme",
                "unsupported",
                "unclassified",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in RiskBucket),
            (
                "cash_like_candidate",
                "high_quality_fixed_income",
                "diversified_equity",
                "concentrated_equity",
                "hybrid_risk",
                "unclassified",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in PortfolioRole),
            (
                "cash_management_candidate",
                "core_eligible",
                "active_diversifier_eligible",
                "satellite_only",
                "not_eligible",
            ),
        )
        self.assertEqual(
            tuple(item.value for item in EvidenceStatus),
            ("verified", "partial", "conflicted", "stale", "unclassified"),
        )
        self.assertEqual(
            tuple(item.value for item in FactConfidence),
            ("exact", "bounded_range", "present", "absent", "ambiguous"),
        )


class MandateFactTest(unittest.TestCase):
    def test_valid_fact_is_immutable(self) -> None:
        fact = mandate_fact()
        fact.validate()
        with self.assertRaises(FrozenInstanceError):
            fact.fact_kind = "changed"  # type: ignore[misc]

    def test_rejects_invalid_code_ids_digest_and_excerpt(self) -> None:
        for changes in (
            {"fund_code": "51975"},
            {"source_document_id": 0},
            {"page_number": 0},
            {"fact_fingerprint": "A" * 64},
            {"source_excerpt": "x" * 4097},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                mandate_fact(**changes).validate()

    def test_source_excerpt_is_required_exact_bounded_text(self) -> None:
        class Excerpt(str):
            pass

        for value in (None, "", "   ", "contains\x00nul", Excerpt("valid-looking")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "source excerpt"):
                mandate_fact(source_excerpt=value).validate()

    def test_rejects_inverted_effective_dates_and_personal_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "effective"):
            mandate_fact(effective_from=date(2026, 2, 1), effective_to=date(2026, 1, 1)).validate()
        for key in ("amount", "target", "recommended", "buy", "sell", "goal_name"):
            for value in (
                (("public", ((key, "redacted"),)),),
                (key, "redacted"),
            ):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(ValueError, "personal"),
                ):
                    mandate_fact(normalized_value=value).validate()

    def test_normalized_value_is_deeply_immutable_and_canonical(self) -> None:
        mandate_fact(normalized_value=(("lower", D("0")), ("upper", D("10")))).validate()
        for value in (
            ["mutable"],
            {"mutable": "mapping"},
            (("upper", D("10")), ("lower", D("0"))),
            (("lower", D("0")), ("lower", D("10"))),
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mandate_fact(normalized_value=value).validate()

    def test_rejects_subclasses_and_hidden_state(self) -> None:
        class DerivedMandateFact(MandateFact):
            pass

        valid = mandate_fact()
        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedMandateFact(**vars(valid)).validate()
        object.__setattr__(valid, "hidden", "not-declared")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            valid.validate()


class ExternalSourceReferenceTest(unittest.TestCase):
    def test_valid_reference_is_exact_frozen_and_canonical(self) -> None:
        reference = external_source()
        reference.validate()
        with self.assertRaises(FrozenInstanceError):
            reference.section = "size"  # type: ignore[misc]

        class DerivedReference(ExternalSourceReference):
            pass

        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedReference(**vars(reference)).validate()

    def test_rejects_noncanonical_security_and_identity_fields(self) -> None:
        invalid = (
            {"source_namespace": "d1_artifact"},
            {"source_document_id": 0},
            {"fund_code": "51975"},
            {"document_kind": "FeeSchedule"},
            {"section": "Fees"},
            {"url": "http://www.fund001.com/fees.html"},
            {"url": "https://user@www.fund001.com/fees.html"},
            {"url": "https://www.fund001.com:444/fees.html"},
            {"url": "https://www.fund001.com/fees.html#fragment"},
            {"source_tier": 0},
            {"published_at": NOW.astimezone(timezone(timedelta(hours=8)))},
            {"retrieved_at": NOW.astimezone(timezone(timedelta(hours=8)))},
            {"checksum": "C" * 64},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                external_source(**changes).validate()


class ClassificationModelTest(unittest.TestCase):
    def test_valid_result_is_immutable_and_research_only(self) -> None:
        result = classification()
        result.validate()
        self.assertEqual(result.capability, "research_only")
        with self.assertRaises(FrozenInstanceError):
            result.policy_version = "2"  # type: ignore[misc]

    def test_result_has_no_recommendation_or_amount_fields(self) -> None:
        names = {field.name for field in dataclasses.fields(FundRiskClassification)}
        for forbidden in ("amount", "target", "recommended", "buy", "sell"):
            self.assertNotIn(forbidden, names)

    def test_evidence_tags_are_fact_only_sorted_unique_stable_codes(self) -> None:
        result = classification()
        result.validate()
        self.assertEqual(
            result.evidence_tags,
            ("credit_exposure", "foreign_currency", "hong_kong_equity"),
        )
        for tags in (
            ("hong_kong_equity", "foreign_currency"),
            ("foreign_currency", "foreign_currency"),
            ("Foreign_Currency",),
            ("foreign-currency",),
            ("personal_goal",),
            ("recommended_buy",),
        ):
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                classification(evidence_tags=tags).validate()

    def test_rejects_noncanonical_or_duplicate_codes_and_ids(self) -> None:
        for changes in (
            {"reason_codes": ("z", "a")},
            {"reason_codes": ("a", "a")},
            {"reason_codes": ("unknown_financial_code",)},
            {"evidence_document_ids": (2, 1)},
            {"evidence_fact_ids": (4, 4)},
            {"conflicts": ("z", "a")},
            {"conflicts": ("unknown_conflict",)},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                classification(**changes).validate()

    def test_rejects_naive_or_non_utc_timestamps_and_invalid_interval(self) -> None:
        for changes in (
            {"classified_at": datetime(2026, 7, 13, 12, 0)},
            {"classified_at": NOW.astimezone(timezone(timedelta(hours=8)))},
            {"valid_until": NOW},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                classification(**changes).validate()

    def test_freshness_requires_positive_id_utc_times_and_valid_interval(self) -> None:
        base = classification().freshness[0]
        base.validate()
        for changes in (
            {"source_document_id": 0},
            {"observed_at": datetime(2026, 7, 13, 12, 0)},
            {"valid_until": NOW},
            {"critical": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(base, **changes).validate()

    def test_classification_and_freshness_reject_subclasses_and_hidden_state(self) -> None:
        class DerivedClassification(FundRiskClassification):
            pass

        class DerivedFreshness(EvidenceFreshness):
            pass

        result = classification()
        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedClassification(**vars(result)).validate()
        freshness = result.freshness[0]
        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedFreshness(**vars(freshness)).validate()
        freshness_with_hidden_state = replace(freshness)
        object.__setattr__(freshness_with_hidden_state, "hidden", "not-declared")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            freshness_with_hidden_state.validate()
        object.__setattr__(result, "hidden", "not-declared")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            result.validate()

    def test_capability_requires_exact_string(self) -> None:
        class ResearchOnly(str):
            pass

        with self.assertRaisesRegex(ValueError, "research_only"):
            classification(capability=ResearchOnly("research_only")).validate()


class RiskPolicyTest(unittest.TestCase):
    def test_policy_v1_is_fixed_and_canonical(self) -> None:
        policy = ClassificationPolicyV1()
        policy.validate()
        self.assertEqual(policy.version, "1")
        self.assertEqual(policy.high_quality_duration_years_max, D("5"))
        self.assertEqual(policy.high_quality_credit_floor_percent, D("80"))
        self.assertEqual(policy.broad_index_constituents_min, 100)
        self.assertEqual(policy.broad_index_top_ten_percent_max, D("40"))
        self.assertEqual(policy.active_top_ten_percent_max, D("50"))
        self.assertEqual(
            hashlib.sha256(policy.canonical_json()).hexdigest(),
            CLASSIFICATION_POLICY_V1_CHECKSUM,
        )
        self.assertEqual(
            CLASSIFICATION_POLICY_V1_CHECKSUM,
            "d3bf0765af2f230a7f332b14324c7e28780093f1d539ac22d84c665454f6ad55",
        )

    def test_policy_canonical_json_is_sorted_ascii_and_clock_free(self) -> None:
        first = ClassificationPolicyV1().canonical_json()
        second = ClassificationPolicyV1().canonical_json()
        self.assertEqual(first, second)
        self.assertEqual(first, first.decode("ascii").encode("ascii"))
        decoded = json.loads(first)
        expected = json.dumps(
            decoded,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        self.assertEqual(first, expected)
        self.assertEqual(decoded["high_quality_stock_percent_max"], "0")
        self.assertNotEqual(decoded["high_quality_stock_percent_max"], "-0")

    def test_policy_rejects_mutation_and_subclasses(self) -> None:
        class DerivedPolicy(ClassificationPolicyV1):
            pass

        with self.assertRaises(FrozenInstanceError):
            ClassificationPolicyV1().version = "2"  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedPolicy().validate()
        policy_with_hidden_state = ClassificationPolicyV1()
        object.__setattr__(policy_with_hidden_state, "hidden", "not-declared")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            policy_with_hidden_state.validate()
        with self.assertRaises(ValueError):
            replace(ClassificationPolicyV1(), broad_index_constituents_min=99).validate()

    def test_report_deadlines_are_calendar_exact(self) -> None:
        policy = ClassificationPolicyV1()
        expected = {
            date(2026, 3, 31): date(2026, 4, 30),
            date(2026, 6, 30): date(2026, 9, 13),
            date(2026, 9, 30): date(2026, 10, 30),
            date(2026, 12, 31): date(2027, 4, 15),
        }
        for period_end, deadline in expected.items():
            with self.subTest(period_end=period_end):
                self.assertEqual(policy.periodic_report_deadline(period_end), deadline)
        with self.assertRaisesRegex(ValueError, "period end"):
            policy.periodic_report_deadline(date(2026, 5, 31))

    def test_stable_financial_and_conflict_codes_are_complete(self) -> None:
        self.assertEqual(len(CLASSIFICATION_FINANCIAL_CODES), 16)
        self.assertEqual(len(CLASSIFICATION_CONFLICT_CODES), 12)
        self.assertEqual(
            tuple(sorted(CLASSIFICATION_FINANCIAL_CODES)),
            CLASSIFICATION_FINANCIAL_CODES,
        )
        self.assertEqual(
            tuple(sorted(CLASSIFICATION_CONFLICT_CODES)),
            CLASSIFICATION_CONFLICT_CODES,
        )
        self.assertIn("classification_verified", CLASSIFICATION_FINANCIAL_CODES)
        self.assertIn("source_version_conflict", CLASSIFICATION_CONFLICT_CODES)


if __name__ == "__main__":
    unittest.main()
