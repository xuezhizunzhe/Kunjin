from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kunjin.funds.risk.models import (
    EvidenceStatus,
    FactConfidence,
    FundRiskClassification,
    MandateFact,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
)
from kunjin.funds.risk.parsers import fact_fingerprint
from kunjin.funds.risk.research import RiskResearchSource, build_risk_research_report

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def classification() -> FundRiskClassification:
    return FundRiskClassification(
        fund_code="519755",
        policy_version="1",
        input_fingerprint="a" * 64,
        product_family=ProductFamily.UNCLASSIFIED,
        risk_bucket=RiskBucket.UNCLASSIFIED,
        portfolio_role=PortfolioRole.NOT_ELIGIBLE,
        evidence_status=EvidenceStatus.UNCLASSIFIED,
        evidence_tags=("foreign_currency",),
        reason_codes=("classification_unclassified",),
        missing_evidence=("legal_product_family_evidence_missing",),
        conflicts=(),
        evidence_document_ids=(),
        evidence_fact_ids=(),
        freshness=(),
        classified_at=NOW,
        valid_until=NOW + timedelta(microseconds=1),
    )


class RiskResearchPrivacyTest(unittest.TestCase):
    def test_report_is_fact_only_and_never_exposes_managed_paths_or_direction(self) -> None:
        report = build_risk_research_report(
            classification(),
            verified_facts=(),
            sources=(
                RiskResearchSource(
                    document_id=1,
                    document_kind="prospectus_update",
                    title="official prospectus",
                    url="https://www.fund001.com/prospectus.html",
                    publisher="交银施罗德基金管理有限公司",
                    published_at=NOW,
                    retrieved_at=NOW,
                ),
            ),
        )
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)

        self.assertEqual(report["capability"], "research_only")
        self.assertEqual(
            report["limitations"],
            [
                "cash_like_is_not_protected_cash",
                "classification_is_not_recommendation",
                "d2_d3_not_evaluated",
            ],
        )
        self.assertEqual(report["evidence_tags"], ["foreign_currency"])
        for forbidden in (
            "managed_path",
            "/Users/",
            "amount",
            "target",
            "buy",
            "sell",
            "direction",
            "score",
        ):
            self.assertNotIn(forbidden, encoded.casefold())

    def test_report_preserves_public_fact_values_and_source_dates(self) -> None:
        fact_fields = {
            "fact_kind": "stock_exposure_max_percent",
            "normalized_value": Decimal("20.00"),
            "unit": "percent",
            "page_number": 12,
            "section_name": "投资范围",
            "source_excerpt": "股票资产不超过基金资产的20%",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        fact = MandateFact(
            fund_code="519755",
            source_document_id=1,
            parser_version="risk_parser_v1",
            fact_fingerprint=fact_fingerprint(**fact_fields),
            **fact_fields,
        )
        report = build_risk_research_report(
            classification(),
            verified_facts=(fact,),
            sources=(
                RiskResearchSource(
                    document_id=1,
                    document_kind="prospectus_update",
                    title="official prospectus",
                    url="https://www.fund001.com/prospectus.html",
                    publisher="交银施罗德基金管理有限公司",
                    published_at=NOW,
                    retrieved_at=NOW,
                ),
            ),
        )

        self.assertEqual(report["verified_facts"][0]["normalized_value"], "20.00")
        self.assertEqual(report["sources"][0]["published_at"], NOW.isoformat())
        self.assertNotIn("source_excerpt", report["verified_facts"][0])

    def test_ambiguous_fact_is_not_reported_as_verified(self) -> None:
        fields = {
            "fact_kind": "legal_product_type",
            "normalized_value": "bond_fund",
            "unit": None,
            "page_number": 3,
            "section_name": "基金概况",
            "source_excerpt": "conflicting public clause",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.AMBIGUOUS,
        }
        fact = MandateFact(
            fund_code="519755",
            source_document_id=1,
            parser_version="risk_parser_v1",
            fact_fingerprint=fact_fingerprint(**fields),
            **fields,
        )

        report = build_risk_research_report(
            classification(),
            verified_facts=(fact,),
            sources=(),
        )

        self.assertEqual(report["verified_facts"], [])
        self.assertEqual(len(report["non_verified_evidence"]), 1)
        self.assertEqual(
            report["non_verified_evidence"][0]["confidence_state"],
            "ambiguous",
        )


if __name__ == "__main__":
    unittest.main()
