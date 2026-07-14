from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Dict, Optional, Tuple

from kunjin.funds.risk.engine import classify_fund
from kunjin.funds.risk.models import FactConfidence, FundRiskClassification, MandateFact
from kunjin.funds.risk.policy import ClassificationPolicyV1


@dataclass(frozen=True)
class RiskResearchSource:
    document_id: int
    document_kind: str
    title: str
    url: str
    publisher: str
    published_at: Optional[datetime]
    retrieved_at: datetime
    source_namespace: str = "d1_artifact"
    section: Optional[str] = None
    source_name: str = "official_document"
    source_tier: int = 1
    checksum: Optional[str] = None
    landing_url: Optional[str] = None

    def validate(self) -> None:
        if type(self) is not RiskResearchSource:
            raise ValueError("research source subclasses are not accepted")
        if set(vars(self)) != {field.name for field in fields(type(self))}:
            raise ValueError("research source has unexpected state")
        if type(self.document_id) is not int or self.document_id <= 0:
            raise ValueError("research source document id must be positive")
        if self.source_namespace not in {"d1_artifact", "fund_disclosure"}:
            raise ValueError("research source namespace is unsupported")
        for value, label in (
            (self.document_kind, "document kind"),
            (self.title, "title"),
            (self.url, "URL"),
            (self.publisher, "publisher"),
            (self.source_name, "source name"),
        ):
            if type(value) is not str or not value.strip() or "\x00" in value:
                raise ValueError(f"research source {label} is required")
        if self.section is not None and (type(self.section) is not str or not self.section.strip()):
            raise ValueError("research source section must be non-empty text")
        if self.landing_url is not None and (
            type(self.landing_url) is not str
            or not self.landing_url.strip()
            or "\x00" in self.landing_url
        ):
            raise ValueError("research source landing URL must be non-empty text")
        if type(self.source_tier) is not int or self.source_tier not in {1, 2, 3}:
            raise ValueError("research source tier must be between one and three")
        if self.checksum is not None and (
            type(self.checksum) is not str
            or len(self.checksum) != 64
            or any(character not in "0123456789abcdef" for character in self.checksum)
        ):
            raise ValueError("research source checksum must be lowercase SHA-256")
        for value, label in (
            (self.published_at, "published_at"),
            (self.retrieved_at, "retrieved_at"),
        ):
            if value is not None and (
                type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"research source {label} must be timezone-aware")


def build_risk_research_report(
    classification: FundRiskClassification,
    *,
    verified_facts: Tuple[MandateFact, ...],
    sources: Tuple[RiskResearchSource, ...],
) -> Dict[str, object]:
    """Return a public, amount-free explanation of one D1 classification."""

    if type(classification) is not FundRiskClassification:
        raise ValueError("research report requires an exact classification")
    classification.validate()
    if type(verified_facts) is not tuple:
        raise ValueError("verified facts must be an immutable tuple")
    if type(sources) is not tuple:
        raise ValueError("research sources must be an immutable tuple")
    for fact in verified_facts:
        if type(fact) is not MandateFact:
            raise ValueError("verified facts must use exact MandateFact records")
        fact.validate()
        if fact.fund_code != classification.fund_code:
            raise ValueError("verified facts must match the classification fund")
    for source in sources:
        source.validate()

    ordered_facts = sorted(
        (item for item in verified_facts if item.confidence_state is not FactConfidence.AMBIGUOUS),
        key=lambda item: (
            item.fact_kind,
            item.source_document_id,
            item.fact_fingerprint,
        ),
    )
    ambiguous_facts = sorted(
        (item for item in verified_facts if item.confidence_state is FactConfidence.AMBIGUOUS),
        key=lambda item: (
            item.fact_kind,
            item.source_document_id,
            item.fact_fingerprint,
        ),
    )
    ordered_sources = sorted(
        sources,
        key=lambda item: (
            item.source_namespace,
            item.document_id,
            item.section or "",
        ),
    )
    return {
        "capability": "research_only",
        "fund_code": classification.fund_code,
        "verified_facts": [_fact_payload(item) for item in ordered_facts],
        "non_verified_evidence": [_fact_payload(item) for item in ambiguous_facts],
        "classification": {
            "policy_version": classification.policy_version,
            "input_fingerprint": classification.input_fingerprint,
            "product_family": classification.product_family.value,
            "risk_bucket": classification.risk_bucket.value,
            "portfolio_role": classification.portfolio_role.value,
            "reason_codes": list(classification.reason_codes),
            "classified_at": classification.classified_at.isoformat(),
            "valid_until": classification.valid_until.isoformat(),
        },
        "evidence_status": classification.evidence_status.value,
        "evidence_tags": list(classification.evidence_tags),
        "missing_evidence": list(classification.missing_evidence),
        "conflicts": list(classification.conflicts),
        "freshness": [
            {
                "section": item.section,
                "source_document_id": item.source_document_id,
                "state": item.state.value,
                "observed_at": item.observed_at.isoformat(),
                "valid_until": item.valid_until.isoformat(),
                "critical": item.critical,
            }
            for item in classification.freshness
        ],
        "sources": [_source_payload(item) for item in ordered_sources],
        "limitations": [
            "cash_like_is_not_protected_cash",
            "classification_is_not_recommendation",
            "d2_d3_not_evaluated",
        ],
    }


def build_authenticated_risk_research_report(record: object) -> Dict[str, object]:
    """Build a public report from a store-authenticated classification record."""

    if record is None:
        raise ValueError("authenticated classification evidence is required")
    policy = ClassificationPolicyV1()
    classification = classify_fund(
        record.evidence,
        policy,
        record.classification.classified_at,
    )
    classification = replace(
        classification,
        input_fingerprint=getattr(
            record.classification,
            "input_fingerprint",
            classification.input_fingerprint,
        ),
    )
    classification.validate()
    facts = (
        record.evidence.legal_facts
        + record.evidence.benchmark_facts
        + record.evidence.report_facts
        + record.evidence.existing_disclosure_facts
    )
    sources = tuple(
        RiskResearchSource(
            document_id=document.id,
            document_kind=document.document_kind.value,
            title=document.title,
            url=document.url,
            publisher=document.publisher,
            published_at=document.published_at,
            retrieved_at=document.retrieved_at,
            source_namespace="d1_artifact",
            source_name="official_document",
            source_tier=1,
            checksum=document.sha256,
            landing_url=document.landing_url,
        )
        for document in record.documents
    ) + tuple(
        RiskResearchSource(
            document_id=reference.source_document_id,
            document_kind=reference.document_kind,
            title=reference.title,
            url=reference.url,
            publisher=reference.publisher,
            published_at=reference.published_at,
            retrieved_at=reference.retrieved_at,
            source_namespace=reference.source_namespace,
            section=reference.section,
            source_name=reference.source_name,
            source_tier=reference.source_tier,
            checksum=reference.checksum,
        )
        for reference in record.evidence.external_source_references
    )
    return build_risk_research_report(
        classification,
        verified_facts=facts,
        sources=sources,
    )


def _fact_payload(fact: MandateFact) -> Dict[str, object]:
    return {
        "fact_kind": fact.fact_kind,
        "normalized_value": _public_value(fact.normalized_value),
        "unit": fact.unit,
        "source_document_id": fact.source_document_id,
        "page_number": fact.page_number,
        "section_name": fact.section_name,
        "effective_from": _date(fact.effective_from),
        "effective_to": _date(fact.effective_to),
        "confidence_state": fact.confidence_state.value,
        "fact_fingerprint": fact.fact_fingerprint,
    }


def _source_payload(source: RiskResearchSource) -> Dict[str, object]:
    return {
        "source_namespace": source.source_namespace,
        "document_id": source.document_id,
        "document_kind": source.document_kind,
        "section": source.section,
        "title": source.title,
        "url": source.url,
        "landing_url": source.landing_url,
        "publisher": source.publisher,
        "source_name": source.source_name,
        "source_tier": source.source_tier,
        "checksum": source.checksum,
        "published_at": _datetime(source.published_at),
        "retrieved_at": _datetime(source.retrieved_at),
    }


def _public_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) in {str, bool, int, type(None)}:
        return value
    if type(value) is date:
        return value.isoformat()
    if type(value) is datetime:
        return _datetime(value)
    if type(value) is tuple:
        if value and all(
            type(item) is tuple and len(item) == 2 and type(item[0]) is str for item in value
        ):
            return {item[0]: _public_value(item[1]) for item in value}
        return [_public_value(item) for item in value]
    raise ValueError("research fact contains unsupported public data")


def _date(value: Optional[date]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "RiskResearchSource",
    "build_authenticated_risk_research_report",
    "build_risk_research_report",
]
