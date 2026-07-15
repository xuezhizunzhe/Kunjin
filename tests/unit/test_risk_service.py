from __future__ import annotations

import hashlib
import inspect
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from kunjin.funds.models import (
    AssetType,
    DisclosureBundle,
    DocumentKind,
    FeeType,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundIndustryExposure,
    FundShareClass,
    FundSizeObservation,
    SourceDocument,
)
from kunjin.funds.risk.audit import (
    ParserProvenance,
    ParseRunKind,
    ParseRunOutcome,
    RefreshOutcome,
    legacy_parser_provenance,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import (
    OfficialDocumentCandidate,
    OfficialDocumentError,
    OfficialDocumentResourceLimitError,
    OfficialDocumentUnavailableError,
    RetrievedArtifact,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.funds.risk.legacy_doc import (
    ConverterStatus,
    LegacyConversionResult,
    LegacyDocConversionError,
)
from kunjin.funds.risk.models import FactConfidence, encode_fact_value_json
from kunjin.funds.risk.parsers import (
    PARSER_VERSION,
    ParsedArtifactResult,
    ParsedMandateFact,
    ParsedRiskDocument,
    RiskDocumentParseError,
    fact_fingerprint,
    parse_artifact,
)
from kunjin.funds.risk.policy import ClassificationPolicyV1
from kunjin.funds.risk.research import build_authenticated_risk_research_report
from kunjin.funds.risk.selection import (
    PERIODIC_DOCUMENT_KINDS,
    select_current_candidates,
)
from kunjin.funds.risk.service import (
    DocumentSelectionItem,
    DocumentSyncItem,
    FundRiskService,
    RiskServiceError,
    evidence_freshness,
    select_current_documents,
)
from kunjin.funds.risk.store import (
    FundRiskStore,
    ParsedDocumentRecord,
    RiskStoreError,
    StoredDocumentArtifact,
    StoredFact,
    StoredParseResult,
    StoredParserProvenance,
    StoredParseRun,
)
from kunjin.funds.store import FundDisclosureStore
from kunjin.models import FundNavObservation
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
LEGACY_PROVENANCE = legacy_parser_provenance(
    image_id="sha256:" + "a" * 64,
    architecture="linux/arm64",
    libreoffice_version="25.2.3.2",
    package_manifest_checksum="b" * 64,
)
RISK_FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds" / "risk"


def provenance_from_payload(payload: dict[str, object]) -> ParserProvenance:
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return ParserProvenance(
        parser_version=str(payload["parser_version"]),
        converter_kind=str(payload["converter_kind"]),
        canonical_json=canonical,
        provenance_checksum=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    )


def historical_native_provenance(parser_version: str = "2") -> ParserProvenance:
    return provenance_from_payload(
        {
            "contract_version": "native-v1",
            "converter_kind": "none",
            "parser_version": parser_version,
        }
    )


def historical_legacy_provenance(parser_version: str) -> ParserProvenance:
    return provenance_from_payload(
        {
            "adapter_contract_version": "docker-libreoffice-v1",
            "architecture": "linux/arm64",
            "converter_kind": "docker_libreoffice",
            "export_filter": "html_starwriter_skip_images_v1",
            "image_id": "sha256:" + "a" * 64,
            "libreoffice_version": "4:7.4.7-1+deb12u14",
            "normalization_contract": "legacy_html_nfc_v1",
            "package_manifest_checksum": "b" * 64,
            "parser_version": parser_version,
        }
    )


def candidate(kind: DocumentKind, suffix: str) -> OfficialDocumentCandidate:
    return OfficialDocumentCandidate(
        fund_code="519755",
        document_kind=kind,
        title=f"official {suffix}",
        url=f"https://www.fund001.com/{suffix}.html",
        publisher="交银施罗德基金管理有限公司",
        published_at=NOW,
        source_tier=1,
    )


def legacy_candidate() -> OfficialDocumentCandidate:
    return OfficialDocumentCandidate(
        fund_code="519755",
        document_kind=DocumentKind.QUARTERLY_REPORT,
        title="交银示例混合型证券投资基金2026年第2季度报告",
        url="https://www.fund001.com/2026-q2.doc",
        publisher="交银施罗德基金管理有限公司",
        published_at=NOW,
        source_tier=1,
    )


def legacy_conversion_result(normalized_html: Optional[str] = None) -> LegacyConversionResult:
    if normalized_html is None:
        normalized_html = (RISK_FIXTURES / "legacy-converted-report.html").read_text(
            encoding="utf-8"
        )
    return LegacyConversionResult(
        normalized_html=normalized_html,
        parser_input_sha256=hashlib.sha256(normalized_html.encode("utf-8")).hexdigest(),
        provenance=LEGACY_PROVENANCE,
    )


def legacy_conversion_error(reason: DocumentFailureReason) -> LegacyDocConversionError:
    public_code = (
        "official_document_resource_limit"
        if reason
        in {
            DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT,
            DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT,
        }
        else "official_document_parse_failed"
    )
    return LegacyDocConversionError(
        SafeDocumentFailure(
            public_code=public_code,
            stage=DocumentFailureStage.CONVERSION,
            reason_code=reason,
        )
    )


def stored_document(
    kind: DocumentKind,
    *,
    artifact_id: int,
    title: str,
    published_at: datetime,
    facts: tuple[StoredFact, ...] = (),
    parser_provenance: object = None,
) -> ParsedDocumentRecord:
    path = Path(__file__)
    payload = path.read_bytes()
    provenance = native_parser_provenance() if parser_provenance is None else parser_provenance
    return ParsedDocumentRecord(
        artifact=StoredDocumentArtifact(
            id=artifact_id,
            fund_code="519755",
            document_kind=kind,
            url=f"https://www.fund001.com/{artifact_id}.html",
            landing_url=f"https://www.fund001.com/{artifact_id}.html",
            publisher="交银施罗德基金管理有限公司",
            title=title,
            published_at=published_at,
            retrieved_at=published_at + timedelta(hours=1),
            content_type="text/html; charset=utf-8",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            managed_path=path,
            parse_status="parsed",
            parser_version=PARSER_VERSION,
            parse_error_code=None,
        ),
        facts=facts,
        provenance=StoredParserProvenance(
            id=artifact_id,
            parser_version=provenance.parser_version,
            converter_kind=provenance.converter_kind,
            canonical_json=provenance.canonical_json,
            provenance_checksum=provenance.provenance_checksum,
            created_at=published_at,
        ),
        parse_result=StoredParseResult(
            id=artifact_id,
            source_document_id=artifact_id,
            provenance_id=artifact_id,
            parser_input_sha256=hashlib.sha256(payload).hexdigest(),
            fact_set_fingerprint=hashlib.sha256(
                "|".join(sorted(item.fact_fingerprint for item in facts)).encode("ascii")
            ).hexdigest(),
            created_at=published_at,
        ),
        parse_run=StoredParseRun(
            id=artifact_id,
            source_document_id=artifact_id,
            provenance_id=artifact_id,
            run_kind=ParseRunKind.LIVE,
            outcome=ParseRunOutcome.SUCCESS,
            parse_result_id=artifact_id,
            public_error_code=None,
            failure_stage=None,
            failure_reason=None,
            attempted_at=published_at,
        ),
    )


def stored_fact(
    *,
    fact_id: int,
    document_id: int,
    fact_kind: str,
    value: object,
    unit: Optional[str] = None,
) -> StoredFact:
    fields = {
        "fact_kind": fact_kind,
        "normalized_value": value,
        "unit": unit,
        "page_number": 1,
        "section_name": "official section",
        "source_excerpt": "official public excerpt",
        "effective_from": None,
        "effective_to": None,
        "confidence_state": FactConfidence.EXACT,
    }
    return StoredFact(
        id=fact_id,
        fund_code="519755",
        source_document_id=document_id,
        parse_result_id=document_id,
        normalized_value_json=encode_fact_value_json(value),
        parser_version=PARSER_VERSION,
        fact_fingerprint=fact_fingerprint(**fields),
        fact_kind=fact_kind,
        unit=unit,
        page_number=1,
        section_name="official section",
        source_excerpt="official public excerpt",
        effective_from=None,
        effective_to=None,
        confidence_state=FactConfidence.EXACT,
    )


def parsed_document_for(
    path: Path,
    item: OfficialDocumentCandidate,
    *,
    facts: tuple[ParsedMandateFact, ...] = (),
) -> ParsedRiskDocument:
    payload = path.read_bytes()
    artifact = RetrievedArtifact(
        candidate=item,
        final_url=item.url,
        retrieved_at=NOW,
        content_type="text/html; charset=utf-8",
        byte_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        managed_path=path,
    )
    return ParsedRiskDocument(
        artifact=artifact,
        facts=facts,
        warnings=(),
        conflicts=(),
    )


def publish_current_document(
    store: FundRiskStore,
    parsed: ParsedRiskDocument,
    *,
    started_at: datetime = NOW,
) -> ParsedDocumentRecord:
    refresh_id = store.begin_document_refresh(parsed.artifact.candidate.fund_code, started_at)
    selection = select_current_candidates(
        parsed.artifact.candidate.fund_code,
        refresh_run_id=refresh_id,
        candidates=(parsed.artifact.candidate,),
    )
    store.publish_document_selection(selection, started_at)
    record = store.publish_candidate_success(
        refresh_id=refresh_id,
        candidate=parsed.artifact.candidate,
        parsed=parsed,
        provenance=native_parser_provenance(),
        parser_input_sha256=parsed.artifact.sha256,
        attempted_at=started_at,
    )
    store.complete_document_refresh(
        refresh_id,
        RefreshOutcome.SUCCESS,
        started_at + timedelta(seconds=1),
    )
    return record


def disclosure_bundle(*, fund_type: str = "FOF") -> DisclosureBundle:
    return DisclosureBundle(
        fund_code="519755",
        identity=FundIdentity(
            fund_code="519755",
            fund_name="公开基金",
            status="active",
            fund_type=fund_type,
            established_date=None,
            manager_name="交银施罗德基金管理有限公司",
            source_document_id=None,
        ),
        share_classes=(),
        manager_tenures=(),
        fee_rules=(),
        sizes=(),
        benchmarks=(),
        holdings=(),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )


def sourced_disclosure_bundle() -> DisclosureBundle:
    source = SourceDocument(
        id=99,
        fund_code="519755",
        document_kind=DocumentKind.QUARTERLY_HOLDINGS,
        title="2026年第一季度公开披露",
        url="https://www.fund001.com/disclosure.html",
        source_name="official_fund_manager",
        source_tier=1,
        publisher="交银施罗德基金管理有限公司",
        published_at=NOW,
        retrieved_at=NOW,
        checksum="b" * 64,
    )
    report_period = date(2026, 3, 31)
    return replace(
        disclosure_bundle(),
        share_classes=(
            FundShareClass(
                fund_code="519755",
                related_fund_code="519756",
                share_class="A",
                fund_name="公开基金A",
                source_document_id=99,
            ),
        ),
        fee_rules=(
            FundFeeRule(
                fund_code="519755",
                fee_type=FeeType.MANAGEMENT,
                source_document_id=99,
                rate=Decimal("0.5"),
            ),
        ),
        sizes=(
            FundSizeObservation(
                fund_code="519755",
                report_date=report_period,
                net_assets=Decimal("1000000"),
                total_shares=None,
                published_at=NOW,
                source_document_id=99,
            ),
        ),
        holdings=(
            FundHolding(
                fund_code="519755",
                report_period=report_period,
                published_at=NOW,
                rank=1,
                security_code="600000",
                security_name="公开证券",
                asset_type=AssetType.STOCK,
                weight=Decimal("8"),
                disclosure_scope="top10",
                source_document_id=99,
            ),
        ),
        industry_exposure=(
            FundIndustryExposure(
                fund_code="519755",
                report_period=report_period,
                published_at=NOW,
                classification_standard="申万",
                industry_name="银行",
                weight=Decimal("20"),
                source_document_id=99,
                industry_code="801780",
            ),
            FundIndustryExposure(
                fund_code="519755",
                report_period=report_period,
                published_at=NOW,
                classification_standard="申万",
                industry_name="电子",
                weight=Decimal("15"),
                source_document_id=99,
                industry_code="801080",
            ),
        ),
        source_documents={99: source},
        section_states={
            DocumentKind.BASIC_PROFILE.value: "success",
            DocumentKind.FEE_SCHEDULE.value: "success",
            DocumentKind.SIZE_HISTORY.value: "success",
            DocumentKind.QUARTERLY_HOLDINGS.value: "success",
            DocumentKind.INDUSTRY_EXPOSURE.value: "success",
        },
        section_statuses={
            kind.value: {
                "state": "success",
                "current_source_document_id": 99,
                "last_attempted_at": NOW.isoformat(),
                "last_success_at": NOW.isoformat(),
                "warning": None,
                "error_code": None,
                "error_message": None,
            }
            for kind in (
                DocumentKind.BASIC_PROFILE,
                DocumentKind.FEE_SCHEDULE,
                DocumentKind.SIZE_HISTORY,
                DocumentKind.QUARTERLY_HOLDINGS,
                DocumentKind.INDUSTRY_EXPOSURE,
            )
        },
    )


def sourced_identity_bundle(
    *,
    fund_name: str = "公开混合基金",
    fund_type: str = "混合型",
    retrieved_at: datetime = NOW,
    sourced: bool = True,
) -> DisclosureBundle:
    source = SourceDocument(
        id=101,
        fund_code="519755",
        document_kind=DocumentKind.BASIC_PROFILE,
        title="公开基金基本资料",
        url="https://www.fund001.com/identity.html",
        source_name="official_fund_manager",
        source_tier=1,
        publisher="交银施罗德基金管理有限公司",
        published_at=NOW,
        retrieved_at=retrieved_at,
        checksum="c" * 64,
    )
    source_id = 101 if sourced else None
    return replace(
        disclosure_bundle(fund_type=fund_type),
        identity=FundIdentity(
            fund_code="519755",
            fund_name=fund_name,
            status="active",
            fund_type=fund_type,
            established_date=None,
            manager_name="交银施罗德基金管理有限公司",
            source_document_id=source_id,
        ),
        source_documents={101: source},
        section_states={DocumentKind.BASIC_PROFILE.value: "success"},
        section_statuses={
            DocumentKind.BASIC_PROFILE.value: {
                "state": "success",
                "current_source_document_id": 101,
                "last_attempted_at": retrieved_at.isoformat(),
                "last_success_at": retrieved_at.isoformat(),
                "warning": None,
                "error_code": None,
                "error_message": None,
            }
        },
    )


class FakeDisclosureStore:
    def __init__(self, bundle: Optional[object] = None) -> None:
        self.bundle = bundle

    def load_bundle(self, fund_code: str) -> object:
        if self.bundle is not None:
            return self.bundle
        return SimpleNamespace(
            fund_code=fund_code,
            identity=SimpleNamespace(manager_name="交银施罗德基金管理有限公司"),
            announcements=(),
        )


class FakeDiscovery:
    def __init__(self, candidates: tuple[OfficialDocumentCandidate, ...]) -> None:
        self.candidates = candidates

    def discover(self, fund_code: str, **_: object) -> tuple[OfficialDocumentCandidate, ...]:
        return self.candidates


class RaisingDiscovery:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def discover(self, fund_code: str, **_: object) -> tuple[OfficialDocumentCandidate, ...]:
        del fund_code
        raise self.error


class FakeDocumentClient:
    def __init__(self, path: Path, failed_url: str) -> None:
        self.path = path
        self.failed_url = failed_url

    def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
        if value.url == self.failed_url:
            raise OfficialDocumentUnavailableError(
                DocumentFailureStage.RETRIEVAL,
                DocumentFailureReason.NETWORK_UNAVAILABLE,
                "network detail must stay private",
            )
        payload = self.path.read_bytes()
        import hashlib

        return RetrievedArtifact(
            candidate=value,
            final_url=value.url,
            retrieved_at=NOW,
            content_type="text/html; charset=utf-8",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            managed_path=self.path,
        )


class FixedContentTypeDocumentClient:
    def __init__(self, path: Path, content_type: str) -> None:
        self.path = path
        self.content_type = content_type

    def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
        payload = self.path.read_bytes()
        return RetrievedArtifact(
            candidate=value,
            final_url=value.url,
            retrieved_at=NOW,
            content_type=self.content_type,
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            managed_path=self.path,
        )


class FakeLegacyConverter:
    def __init__(
        self,
        outcomes: tuple[object, ...],
        *,
        provenance: object = LEGACY_PROVENANCE,
    ) -> None:
        self.outcomes = list(outcomes)
        self.provenance = provenance
        self.status_calls = 0
        self.convert_calls = 0
        self.active_provenance_calls = 0

    def status(self) -> object:
        self.status_calls += 1
        raise AssertionError("service must not query public status during synchronization")

    def active_provenance(self) -> object:
        self.active_provenance_calls += 1
        if isinstance(self.provenance, BaseException):
            raise self.provenance
        return self.provenance

    def convert(self, artifact: RetrievedArtifact) -> LegacyConversionResult:
        del artifact
        self.convert_calls += 1
        if not self.outcomes:
            raise AssertionError("unexpected conversion call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if type(outcome) is not LegacyConversionResult:
            raise AssertionError("test conversion outcome is invalid")
        return outcome


class RaisingDocumentClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
        del value
        raise self.error


class FakeRiskStore:
    def __init__(
        self,
        records: Optional[tuple[ParsedDocumentRecord, ...]] = None,
        *,
        events: Optional[list[object]] = None,
    ) -> None:
        self.published: list[ParsedRiskDocument] = []
        self.published_parser_state: list[tuple[object, str]] = []
        self.failed_artifacts: list[tuple[RetrievedArtifact, str, str]] = []
        self.failed_parser_state: list[object] = []
        self.candidate_outcomes: list[tuple[str, int, OfficialDocumentCandidate]] = []
        self.published_selections: list[object] = []
        self.events = [] if events is None else events
        self.refreshes: dict[int, dict[str, object]] = {}
        self.records = (
            (
                stored_document(
                    DocumentKind.PRODUCT_SUMMARY,
                    artifact_id=900,
                    title="2026年产品资料概要",
                    published_at=NOW,
                ),
            )
            if records is None
            else records
        )
        self.saved = None
        self.saved_evidence = None
        self._next_refresh_id = 1
        self.snapshot_override = None

    def begin_document_refresh(self, fund_code: str, started_at: datetime) -> int:
        refresh_id = self._next_refresh_id
        self._next_refresh_id += 1
        self.events.append("begin_refresh")
        self.refreshes[refresh_id] = {
            "fund_code": fund_code,
            "started_at": started_at,
            "completion": None,
        }
        return refresh_id

    def publish_document_selection(self, plan: object, created_at: datetime) -> object:
        del created_at
        self.events.append("publish_selection")
        self.published_selections.append(plan)
        return plan

    def complete_document_refresh(
        self,
        refresh_id: int,
        outcome: RefreshOutcome,
        completed_at: datetime,
        *,
        failure: Optional[SafeDocumentFailure] = None,
    ) -> None:
        self.events.append("complete_refresh")
        self.refreshes[refresh_id]["completion"] = (outcome, completed_at, failure)

    def publish_candidate_success(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        parsed: ParsedRiskDocument,
        provenance: object,
        parser_input_sha256: str,
        attempted_at: datetime,
    ) -> object:
        del attempted_at
        self.published.append(parsed)
        self.published_parser_state.append((provenance, parser_input_sha256))
        self.candidate_outcomes.append(("success", refresh_id, candidate))
        return SimpleNamespace(artifact=SimpleNamespace(id=1), facts=())

    def publish_candidate_failure(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        failure: SafeDocumentFailure,
        attempted_at: datetime,
        artifact: None,
        provenance: None,
    ) -> None:
        del failure, attempted_at, artifact, provenance
        self.candidate_outcomes.append(("failure", refresh_id, candidate))

    def publish_candidate_parse_failure(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        artifact: RetrievedArtifact,
        provenance: object,
        failure: SafeDocumentFailure,
        attempted_at: datetime,
    ) -> object:
        del attempted_at
        self.failed_artifacts.append((artifact, PARSER_VERSION, failure.public_code))
        self.failed_parser_state.append(provenance)
        self.candidate_outcomes.append(("parse_failure", refresh_id, candidate))
        return SimpleNamespace(id=2)

    def current_parsed_documents(
        self,
        _: str,
        active_provenance_checksums: tuple[str, ...],
    ) -> tuple[ParsedDocumentRecord, ...]:
        self.active_provenance_checksums = active_provenance_checksums
        if self.refreshes:
            latest = self.refreshes[max(self.refreshes)]
            completion = latest["completion"]
            if completion is None or completion[0] not in {
                RefreshOutcome.SUCCESS,
                RefreshOutcome.PARTIAL,
            }:
                return ()
        return tuple(
            record
            for record in self.records
            if record.provenance.provenance_checksum in active_provenance_checksums
        )

    def current_document_selection_snapshot(self, fund_code: str) -> object:
        if self.snapshot_override is not None:
            return self.snapshot_override
        refresh_id = 1
        if self.refreshes:
            refresh_id = max(self.refreshes)
            latest = self.refreshes[refresh_id]
            completion = latest["completion"]
            if completion is None or completion[0] not in {
                RefreshOutcome.SUCCESS,
                RefreshOutcome.PARTIAL,
            }:
                return None
        periodic = tuple(
            record
            for record in self.records
            if record.artifact.document_kind in PERIODIC_DOCUMENT_KINDS
        )
        nonperiodic = tuple(
            record
            for record in self.records
            if record.artifact.document_kind not in PERIODIC_DOCUMENT_KINDS
        )
        snapshot_payload = {
            "refresh_run_id": refresh_id,
            "records": [
                {
                    "artifact_id": record.artifact.id,
                    "parse_result_id": record.parse_result.id,
                    "provenance_checksum": record.provenance.provenance_checksum,
                }
                for record in self.records
            ],
        }
        snapshot_checksum = hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        ).hexdigest()
        return SimpleNamespace(
            selection=SimpleNamespace(
                refresh_run_id=refresh_id,
                fund_code=fund_code,
                selection_policy_checksum="a" * 64,
                selection_checksum=hashlib.sha256(
                    f"selection:{fund_code}:{refresh_id}".encode("ascii")
                ).hexdigest(),
            ),
            selected_periodic_records=periodic,
            nonperiodic_successful_records=nonperiodic,
            candidate_run_snapshot_checksum=snapshot_checksum,
            selection_reason_codes=(),
        )

    def current_parser_requirements(self, _: str) -> tuple[StoredParserProvenance, ...]:
        return tuple(
            sorted(
                {
                    record.provenance.provenance_checksum: record.provenance
                    for record in self.records
                }.values(),
                key=lambda item: item.provenance_checksum,
            )
        )

    def ensure_policy(self, policy: ClassificationPolicyV1) -> object:
        return SimpleNamespace(policy_checksum=policy.checksum())

    def save_classification(self, result: object, evidence: object, _: object) -> object:
        self.saved = result
        self.saved_evidence = evidence
        return SimpleNamespace(id=1, input_fingerprint=result.input_fingerprint)

    def classification_evidence(self, _: str, classification_id: Optional[int] = None) -> object:
        del classification_id
        if self.saved is None:
            return None
        return SimpleNamespace(
            classification=SimpleNamespace(
                id=1,
                classified_at=self.saved.classified_at,
                input_fingerprint=self.saved.input_fingerprint,
            ),
            evidence=self.saved_evidence,
        )

    def classification_history(self, _: str) -> tuple:
        return () if self.saved is None else (self.saved,)


class FakeRepository:
    def __init__(self, history: tuple[FundNavObservation, ...] = ()) -> None:
        self.history = history

    def fund_history(self, _: str) -> list[FundNavObservation]:
        return list(self.history)


def selection_snapshot(
    *,
    periodic: tuple[ParsedDocumentRecord, ...] = (),
    nonperiodic: tuple[ParsedDocumentRecord, ...] = (),
    refresh_run_id: int = 1,
    reason_codes: tuple[str, ...] = (),
    selection_policy_checksum: str = "a" * 64,
    selection_checksum: str = "b" * 64,
    candidate_run_snapshot_checksum: str = "c" * 64,
) -> object:
    return SimpleNamespace(
        selection=SimpleNamespace(
            refresh_run_id=refresh_run_id,
            fund_code="519755",
            selection_policy_checksum=selection_policy_checksum,
            selection_checksum=selection_checksum,
        ),
        selected_periodic_records=periodic,
        nonperiodic_successful_records=nonperiodic,
        candidate_run_snapshot_checksum=candidate_run_snapshot_checksum,
        selection_reason_codes=reason_codes,
    )


class RiskServiceBoundaryTest(unittest.TestCase):
    def test_constructor_has_no_personal_finance_dependencies(self) -> None:
        parameters = set(inspect.signature(FundRiskService).parameters)
        forbidden = {
            "profile_service",
            "suitability_service",
            "allocation_service",
            "ledger_service",
            "yangjibao_client",
        }
        self.assertFalse(parameters & forbidden)

    def test_converter_status_returns_only_exact_safe_metadata(self) -> None:
        ready = ConverterStatus(
            capability="research_only",
            status="ready",
            reason_code=None,
            parser_version=LEGACY_PROVENANCE.parser_version,
            provenance_checksum=LEGACY_PROVENANCE.provenance_checksum,
        )

        class StatusConverter:
            def __init__(self, outcome: object) -> None:
                self.outcome = outcome

            def status(self) -> object:
                if isinstance(self.outcome, BaseException):
                    raise self.outcome
                return self.outcome

        unavailable = FundRiskService(
            risk_store=SimpleNamespace(),
            disclosure_store=SimpleNamespace(),
            repository=SimpleNamespace(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
        ).converter_status()
        returned = FundRiskService(
            risk_store=SimpleNamespace(),
            disclosure_store=SimpleNamespace(),
            repository=SimpleNamespace(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            legacy_converter=StatusConverter(ready),
        ).converter_status()

        self.assertEqual(
            unavailable,
            ConverterStatus(
                capability="research_only",
                status="unavailable",
                reason_code="legacy_converter_unavailable",
                parser_version=None,
                provenance_checksum=None,
            ),
        )
        self.assertIs(returned, ready)

        for unsafe in (
            RuntimeError("/private/tmp/docker stderr must stay private"),
            SimpleNamespace(
                capability="research_only",
                status="ready",
                image_id="sha256:" + "f" * 64,
            ),
        ):
            with self.subTest(unsafe=type(unsafe).__name__):
                status = FundRiskService(
                    risk_store=SimpleNamespace(),
                    disclosure_store=SimpleNamespace(),
                    repository=SimpleNamespace(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    legacy_converter=StatusConverter(unsafe),
                ).converter_status()
                self.assertEqual(status.status, "unavailable")
                self.assertEqual(status.reason_code, "legacy_converter_unavailable")
                self.assertIsNone(status.parser_version)
                self.assertIsNone(status.provenance_checksum)

    def test_document_sync_item_rejects_private_diagnostic_text_and_url_components(
        self,
    ) -> None:
        valid = DocumentSyncItem(
            document_kind="prospectus_update",
            title="公开基金招募说明书（更新）",
            url=("https://www.fund001.com/prospectus.html?document=2026-update&language=zh_CN"),
            published_at=NOW,
            status="success",
            artifact_id=1,
            fact_count=3,
            warnings=(),
            conflicts=(),
            error_code=None,
        )
        valid.validate()

        unsafe_titles = (
            "公开报告 stderr_private_diagnostic",
            "公开报告 stdout_private_diagnostic",
            "公开报告 traceback_private_diagnostic",
            "公开报告 <html>private diagnostic</html>",
            "公开报告 %3Chtml%3Eprivate diagnostic%3C%2Fhtml%3E",
            "公开报告 path=/private/tmp/report.doc",
            "公开报告 path=/Users/owner/report.doc",
            "公开报告 path=%2Fprivate%2Ftmp%2Freport.doc",
        )
        unsafe_urls = (
            "https://www.fund001.com/report?path=/private/tmp/report.doc",
            "https://www.fund001.com/report?path=/Users/owner/report.doc",
            "https://www.fund001.com/report?diagnostic=stderr_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=stdout_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=traceback_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=%73tderr_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=%73tdout_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=%74raceback_private_diagnostic",
            "https://www.fund001.com/report?payload=<html>private</html>",
            "https://www.fund001.com/report?path=%2Fprivate%2Ftmp%2Freport.doc",
            "https://www.fund001.com/report?path=%2FUsers%2Fowner%2Freport.doc",
            "https://www.fund001.com/report?payload=%3Chtml%3Eprivate%3C%2Fhtml%3E",
            "https://www.fund001.com/report?path=%252Fprivate%252Ftmp%252Freport.doc",
            "https://www.fund001.com/report?path=%252FUsers%252Fowner%252Freport.doc",
            "https://www.fund001.com/report?payload=%253Chtml%253Eprivate%253C%252Fhtml%253E",
            "https://www.fund001.com/report?diagnostic=%2573tderr_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=%2573tdout_private_diagnostic",
            "https://www.fund001.com/report?diagnostic=%2574raceback_private_diagnostic",
            "https://www.fund001.com/report#path=/private/tmp/report.doc",
            "https://www.fund001.com/report#diagnostic=stderr_private_diagnostic",
            "https://www.fund001.com/report#payload=%3Chtml%3Eprivate%3C%2Fhtml%3E",
        )

        for title in unsafe_titles:
            with self.subTest(title=title), self.assertRaises(ValueError):
                replace(valid, title=title).validate()
        for url in unsafe_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                replace(valid, url=url).validate()

        replace(
            valid,
            title="公开基金招募说明书（资产比例50%）",
            url=(
                "https://www.fund001.com/reports/users-guide.html?"
                "name=%E5%85%AC%E5%BC%80%E6%8A%A5%E5%91%8A&page=1"
            ),
        ).validate()

    def test_non_ole_sync_never_checks_converter_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text(
                "<html><body><p>基金类型：基金中基金</p></body></html>",
                encoding="utf-8",
            )
            converter = FakeLegacyConverter(())
            store = FakeRiskStore()
            result = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((candidate(DocumentKind.PROSPECTUS, "native"),)),
                document_client=FixedContentTypeDocumentClient(path, "text/html; charset=utf-8"),
                legacy_converter=converter,
                clock=lambda: NOW,
            ).sync_documents("519755")

        self.assertEqual(result.status, "success")
        self.assertEqual(converter.status_calls, 0)
        self.assertEqual(converter.convert_calls, 0)
        self.assertEqual(converter.active_provenance_calls, 0)
        self.assertEqual(
            store.published_parser_state,
            [(native_parser_provenance(), store.published[0].artifact.sha256)],
        )

    def test_ole_unavailable_timeout_resource_failed_and_output_invalid_propagate_exact_codes(
        self,
    ) -> None:
        cases = (
            (
                DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
                "official_document_parse_failed",
                None,
                "failure",
            ),
            (
                DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT,
                "official_document_resource_limit",
                LEGACY_PROVENANCE,
                "parse_failure",
            ),
            (
                DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT,
                "official_document_resource_limit",
                LEGACY_PROVENANCE,
                "parse_failure",
            ),
            (
                DocumentFailureReason.LEGACY_CONVERTER_FAILED,
                "official_document_parse_failed",
                LEGACY_PROVENANCE,
                "parse_failure",
            ),
            (
                DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID,
                "official_document_parse_failed",
                LEGACY_PROVENANCE,
                "parse_failure",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.doc"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            for reason, public_code, provenance, expected_outcome in cases:
                with self.subTest(reason=reason.value):
                    converter = FakeLegacyConverter(
                        (legacy_conversion_error(reason),),
                        provenance=provenance,
                    )
                    store = FakeRiskStore()
                    result = FundRiskService(
                        risk_store=store,
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((legacy_candidate(),)),
                        document_client=FixedContentTypeDocumentClient(path, "application/msword"),
                        legacy_converter=converter,
                        clock=lambda: NOW,
                    ).sync_documents("519755")

                    item = result.documents[0]
                    self.assertEqual(item.error_code, public_code)
                    self.assertEqual(item.failure_stage, "conversion")
                    self.assertEqual(item.failure_reason, reason.value)
                    self.assertEqual(store.candidate_outcomes[0][0], expected_outcome)
                    self.assertEqual(
                        store.failed_parser_state,
                        [] if provenance is None else [provenance],
                    )

    def test_conversion_failure_persists_retryable_parse_and_candidate_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy.doc"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            repository = Repository(root / "kunjin.sqlite3")
            repository.migrate()
            risk_store = FundRiskStore(repository)
            converter = FakeLegacyConverter(
                (
                    legacy_conversion_error(DocumentFailureReason.LEGACY_CONVERTER_FAILED),
                    legacy_conversion_result(),
                )
            )
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FundDisclosureStore(repository),
                repository=repository,
                discovery=FakeDiscovery((legacy_candidate(),)),
                document_client=FixedContentTypeDocumentClient(path, "application/msword"),
                legacy_converter=converter,
                clock=lambda: NOW,
            )

            first = service.sync_documents("519755")
            second = service.sync_documents("519755")
            current = risk_store.current_parsed_documents(
                "519755", (LEGACY_PROVENANCE.provenance_checksum,)
            )
            with repository.connect() as connection:
                runs = connection.execute(
                    "SELECT outcome, failure_stage, failure_reason "
                    "FROM fund_document_parse_runs ORDER BY id"
                ).fetchall()

        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "success")
        self.assertEqual(
            [(row["outcome"], row["failure_stage"], row["failure_reason"]) for row in runs],
            [
                ("failed", "conversion", "legacy_converter_failed"),
                ("success", None, None),
            ],
        )
        self.assertEqual(len(current), 1)
        self.assertEqual(
            current[0].provenance.provenance_checksum, LEGACY_PROVENANCE.provenance_checksum
        )
        self.assertGreater(len(current[0].facts), 0)
        self.assertEqual(
            current[0].parse_result.parser_input_sha256,
            legacy_conversion_result().parser_input_sha256,
        )

    def test_custom_parser_exact_result_compatibility_and_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            ole_path = root / "document.doc"
            ole_path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            item = candidate(DocumentKind.PROSPECTUS, "custom-parser")
            native_client = FixedContentTypeDocumentClient(path, "text/html; charset=utf-8")
            legacy_client = FixedContentTypeDocumentClient(ole_path, "application/msword")

            def native_parser(artifact: RetrievedArtifact) -> ParsedRiskDocument:
                return ParsedRiskDocument(artifact=artifact, facts=(), warnings=(), conflicts=())

            def native_result_parser(artifact: RetrievedArtifact) -> ParsedArtifactResult:
                return ParsedArtifactResult(
                    document=ParsedRiskDocument(
                        artifact=artifact,
                        facts=(),
                        warnings=(),
                        conflicts=(),
                    ),
                    parser_input_sha256=artifact.sha256,
                    provenance=native_parser_provenance(),
                )

            for parser in (native_parser, native_result_parser):
                with self.subTest(parser=parser.__name__):
                    store = FakeRiskStore()
                    result = FundRiskService(
                        risk_store=store,
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((item,)),
                        document_client=native_client,
                        parser=parser,
                        clock=lambda: NOW,
                    ).sync_documents("519755")
                    self.assertEqual(result.status, "success")
                    provenance, parser_input = store.published_parser_state[0]
                    self.assertEqual(provenance, native_parser_provenance())
                    self.assertEqual(parser_input, store.published[0].artifact.sha256)

            custom_parser_calls = 0

            def ole_native_parser(artifact: RetrievedArtifact) -> ParsedRiskDocument:
                nonlocal custom_parser_calls
                custom_parser_calls += 1
                return ParsedRiskDocument(artifact=artifact, facts=(), warnings=(), conflicts=())

            def ole_provenance_parser(artifact: RetrievedArtifact) -> ParsedArtifactResult:
                nonlocal custom_parser_calls
                custom_parser_calls += 1
                return ParsedArtifactResult(
                    document=ParsedRiskDocument(
                        artifact=artifact,
                        facts=(),
                        warnings=(),
                        conflicts=(),
                    ),
                    parser_input_sha256="d" * 64,
                    provenance=LEGACY_PROVENANCE,
                )

            for parser in (ole_native_parser, ole_provenance_parser):
                with self.subTest(ole_parser=parser.__name__):
                    rejected_ole = FundRiskService(
                        risk_store=FakeRiskStore(),
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((item,)),
                        document_client=legacy_client,
                        parser=parser,
                        clock=lambda: NOW,
                    ).sync_documents("519755")
                    self.assertEqual(rejected_ole.status, "failed")
                    self.assertEqual(
                        rejected_ole.documents[0].failure_reason,
                        "legacy_converter_unavailable",
                    )
            self.assertEqual(custom_parser_calls, 0)

            rejected = FundRiskService(
                risk_store=FakeRiskStore(),
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((item,)),
                document_client=native_client,
                parser=lambda artifact: SimpleNamespace(artifact=artifact),
                clock=lambda: NOW,
            ).sync_documents("519755")
        self.assertEqual(rejected.status, "failed")
        self.assertEqual(rejected.documents[0].failure_reason, "parser_format_invalid")

    def test_custom_parser_provenance_must_match_actual_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            html_path = root / "document.html"
            html_path.write_text("<html><body>public</body></html>", encoding="utf-8")
            ole_path = root / "document.doc"
            ole_path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            item = candidate(DocumentKind.PROSPECTUS, "container-binding")

            def parsed_result(
                artifact: RetrievedArtifact,
                provenance: object,
                parser_input_sha256: str,
            ) -> ParsedArtifactResult:
                return ParsedArtifactResult(
                    document=ParsedRiskDocument(
                        artifact=artifact,
                        facts=(),
                        warnings=(),
                        conflicts=(),
                    ),
                    parser_input_sha256=parser_input_sha256,
                    provenance=provenance,
                )

            cases = (
                (
                    FixedContentTypeDocumentClient(html_path, "text/html; charset=utf-8"),
                    lambda artifact: parsed_result(artifact, LEGACY_PROVENANCE, "d" * 64),
                    "parser_format_invalid",
                ),
                (
                    FixedContentTypeDocumentClient(html_path, "text/html; charset=utf-8"),
                    lambda artifact: parsed_result(
                        artifact,
                        native_parser_provenance(),
                        "d" * 64,
                    ),
                    "parser_format_invalid",
                ),
                (
                    FixedContentTypeDocumentClient(ole_path, "application/msword"),
                    lambda artifact: parsed_result(
                        artifact,
                        native_parser_provenance(),
                        artifact.sha256,
                    ),
                    "legacy_converter_unavailable",
                ),
            )
            for client, parser, expected_reason in cases:
                with self.subTest(content_type=client.content_type):
                    result = FundRiskService(
                        risk_store=FakeRiskStore(),
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((item,)),
                        document_client=client,
                        parser=parser,
                        clock=lambda: NOW,
                    ).sync_documents("519755")
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(
                        result.documents[0].failure_reason,
                        expected_reason,
                    )

    def test_ole_uses_injected_converter_without_calling_custom_parser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.doc"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            custom_parser_calls = 0

            def custom_parser(artifact: RetrievedArtifact) -> ParsedArtifactResult:
                nonlocal custom_parser_calls
                custom_parser_calls += 1
                return ParsedArtifactResult(
                    document=ParsedRiskDocument(
                        artifact=artifact,
                        facts=(),
                        warnings=(),
                        conflicts=(),
                    ),
                    parser_input_sha256="d" * 64,
                    provenance=LEGACY_PROVENANCE,
                )

            converter = FakeLegacyConverter((legacy_conversion_result(),), provenance=None)
            store = FakeRiskStore()
            result = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((legacy_candidate(),)),
                document_client=FixedContentTypeDocumentClient(path, "application/msword"),
                parser=custom_parser,
                legacy_converter=converter,
                clock=lambda: NOW,
            ).sync_documents("519755")

        self.assertEqual(result.status, "success")
        self.assertEqual(custom_parser_calls, 0)
        self.assertEqual(converter.active_provenance_calls, 0)
        self.assertEqual(
            store.published_parser_state,
            [(LEGACY_PROVENANCE, legacy_conversion_result().parser_input_sha256)],
        )

    def test_converted_parser_failure_binds_result_provenance_without_status_lookup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.doc"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            invalid_identity = (
                RISK_FIXTURES.joinpath("legacy-converted-report.html")
                .read_text(encoding="utf-8")
                .replace("519755", "000001")
                .replace("交银示例混合型证券投资基金", "其他基金")
            )
            for active_provenance in (None, RuntimeError("private status failure")):
                with self.subTest(active_provenance=type(active_provenance).__name__):
                    converter = FakeLegacyConverter(
                        (legacy_conversion_result(invalid_identity),),
                        provenance=active_provenance,
                    )
                    store = FakeRiskStore()
                    result = FundRiskService(
                        risk_store=store,
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((legacy_candidate(),)),
                        document_client=FixedContentTypeDocumentClient(
                            path,
                            "application/msword",
                        ),
                        legacy_converter=converter,
                        clock=lambda: NOW,
                    ).sync_documents("519755")

                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.documents[0].failure_stage, "parser")
                    self.assertEqual(
                        result.documents[0].failure_reason,
                        "parser_identity_mismatch",
                    )
                    self.assertEqual(store.failed_parser_state, [LEGACY_PROVENANCE])
                    self.assertEqual(converter.active_provenance_calls, 0)

    def test_invalid_conversion_result_without_provenance_never_defaults_native(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.doc"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            invalid_result = replace(
                legacy_conversion_result(),
                parser_input_sha256="d" * 64,
            )
            converter = FakeLegacyConverter((invalid_result,), provenance=None)
            store = FakeRiskStore()
            result = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((legacy_candidate(),)),
                document_client=FixedContentTypeDocumentClient(path, "application/msword"),
                legacy_converter=converter,
                clock=lambda: NOW,
            ).sync_documents("519755")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.documents[0].failure_stage, "conversion")
        self.assertEqual(
            result.documents[0].failure_reason,
            "legacy_converter_output_invalid",
        )
        self.assertEqual(store.failed_parser_state, [])
        self.assertEqual(store.candidate_outcomes[0][0], "failure")

    def test_pure_native_classify_and_current_read_never_query_converter(self) -> None:
        store = FakeRiskStore()
        converter = FakeLegacyConverter(())
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            legacy_converter=converter,
            clock=lambda: NOW,
        )

        service.classify("519755")
        service.current_classification("519755")

        self.assertEqual(
            store.saved_evidence.parser_provenance_checksums,
            (native_parser_provenance().provenance_checksum,),
        )
        self.assertEqual(converter.active_provenance_calls, 0)
        self.assertEqual(converter.status_calls, 0)

    def test_legacy_current_evidence_requires_exact_active_provenance(self) -> None:
        legacy_record = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=901,
            title="2026年产品资料概要",
            published_at=NOW,
            parser_provenance=LEGACY_PROVENANCE,
        )
        store = FakeRiskStore((legacy_record,))
        converter = FakeLegacyConverter((), provenance=LEGACY_PROVENANCE)
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            legacy_converter=converter,
            clock=lambda: NOW,
        )

        service.classify("519755")

        self.assertEqual(
            store.saved_evidence.parser_provenance_checksums,
            (LEGACY_PROVENANCE.provenance_checksum,),
        )
        self.assertGreaterEqual(converter.active_provenance_calls, 1)

    def test_legacy_current_evidence_fails_closed_on_provenance_mismatch(self) -> None:
        legacy_record = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=901,
            title="2026年产品资料概要",
            published_at=NOW,
            parser_provenance=LEGACY_PROVENANCE,
        )
        other = legacy_parser_provenance(
            image_id="sha256:" + "c" * 64,
            architecture="linux/arm64",
            libreoffice_version="25.2.3.2",
            package_manifest_checksum="d" * 64,
        )
        converter = FakeLegacyConverter((), provenance=other)
        service = FundRiskService(
            risk_store=FakeRiskStore((legacy_record,)),
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            legacy_converter=converter,
            clock=lambda: NOW,
        )

        with self.assertRaises(RiskServiceError) as caught:
            service.classify("519755")

        self.assertEqual(caught.exception.code, "official_document_unavailable")
        self.assertEqual(converter.active_provenance_calls, 1)

    def test_known_v2_and_v3_evidence_is_unavailable_until_v4_refresh(self) -> None:
        for parser_version in ("2", "3"):
            with self.subTest(parser_version=parser_version):
                historical = stored_document(
                    DocumentKind.QUARTERLY_REPORT,
                    artifact_id=901,
                    title="2026年第二季度报告",
                    published_at=NOW,
                    parser_provenance=historical_native_provenance(parser_version),
                )
                current = stored_document(
                    DocumentKind.QUARTERLY_REPORT,
                    artifact_id=902,
                    title="2026年第二季度报告",
                    published_at=NOW + timedelta(minutes=1),
                )
                store = FakeRiskStore((historical,))
                service = FundRiskService(
                    risk_store=store,
                    disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                    repository=FakeRepository(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW + timedelta(minutes=2),
                )

                with self.assertRaises(RiskServiceError) as caught:
                    service.classify("519755")
                self.assertEqual(caught.exception.code, "official_document_unavailable")

                store.records = (current,)
                service.classify("519755")

                self.assertEqual(
                    store.saved_evidence.parse_result_ids,
                    (current.parse_result.id,),
                )
                self.assertEqual(
                    store.saved_evidence.parser_provenance_checksums,
                    (current.provenance.provenance_checksum,),
                )

    def test_known_legacy_v2_and_v3_evidence_is_unavailable_until_v4_refresh(
        self,
    ) -> None:
        for parser_version in (
            "2-docker-libreoffice-v1",
            "3-docker-libreoffice-v1",
        ):
            with self.subTest(parser_version=parser_version):
                historical = stored_document(
                    DocumentKind.QUARTERLY_REPORT,
                    artifact_id=901,
                    title="2026年第二季度报告",
                    published_at=NOW,
                    parser_provenance=historical_legacy_provenance(parser_version),
                )
                service = FundRiskService(
                    risk_store=FakeRiskStore((historical,)),
                    disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                    repository=FakeRepository(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW + timedelta(minutes=1),
                )

                with self.assertRaises(RiskServiceError) as caught:
                    service.classify("519755")

                self.assertEqual(caught.exception.code, "official_document_unavailable")

    def test_unknown_current_provenance_is_storage_failure(self) -> None:
        unknown = provenance_from_payload(
            {
                "contract_version": "native-v1",
                "converter_kind": "none",
                "parser_version": "99",
            }
        )
        record = stored_document(
            DocumentKind.QUARTERLY_REPORT,
            artifact_id=901,
            title="2026年第二季度报告",
            published_at=NOW,
            parser_provenance=unknown,
        )

        with self.assertRaises(RiskServiceError) as caught:
            FundRiskService(
                risk_store=FakeRiskStore((record,)),
                disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")

        self.assertEqual(caught.exception.code, "classification_storage_failed")

    def test_selected_v4_success_uses_exact_snapshot_record(self) -> None:
        old_fact = stored_fact(
            fact_id=901,
            document_id=901,
            fact_kind="current_stock_asset_allocation_percent",
            value=Decimal("10"),
            unit="percent_of_net_assets",
        )
        current_fact = stored_fact(
            fact_id=902,
            document_id=902,
            fact_kind="current_stock_asset_allocation_percent",
            value=Decimal("20"),
            unit="percent_of_net_assets",
        )
        old = stored_document(
            DocumentKind.QUARTERLY_REPORT,
            artifact_id=901,
            title="2026年第1季度报告",
            published_at=NOW - timedelta(days=30),
            facts=(old_fact,),
        )
        current = stored_document(
            DocumentKind.SEMIANNUAL_REPORT,
            artifact_id=902,
            title="2026年半年度报告",
            published_at=NOW,
            facts=(current_fact,),
        )
        store = FakeRiskStore((old, current))
        store.snapshot_override = selection_snapshot(
            periodic=(current,),
            refresh_run_id=7,
        )

        FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        self.assertEqual(store.saved_evidence.document_ids, (902,))
        self.assertEqual(
            tuple(fact.normalized_value for fact in store.saved_evidence.report_facts),
            (Decimal("20"),),
        )
        self.assertEqual(store.saved_evidence.document_refresh_run_id, 7)
        self.assertEqual(store.saved_evidence.selection_policy_checksum, "a" * 64)
        self.assertEqual(store.saved_evidence.selection_manifest_checksum, "b" * 64)
        self.assertEqual(store.saved_evidence.candidate_run_snapshot_checksum, "c" * 64)

    def test_failed_or_missing_periodic_selection_never_uses_old_report(self) -> None:
        old = stored_document(
            DocumentKind.ANNUAL_REPORT,
            artifact_id=901,
            title="2025年年度报告",
            published_at=NOW - timedelta(days=90),
            facts=(
                stored_fact(
                    fact_id=901,
                    document_id=901,
                    fact_kind="current_stock_asset_allocation_percent",
                    value=Decimal("40"),
                    unit="percent_of_net_assets",
                ),
            ),
        )
        legal = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=902,
            title="2026年产品资料概要",
            published_at=NOW,
        )
        for reason_codes in ((), ("current_periodic_candidate_missing",)):
            with self.subTest(reason_codes=reason_codes):
                store = FakeRiskStore((old, legal))
                store.snapshot_override = selection_snapshot(
                    nonperiodic=(legal,),
                    reason_codes=reason_codes,
                )

                result = FundRiskService(
                    risk_store=store,
                    disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                    repository=FakeRepository(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW,
                ).classify("519755")

                self.assertEqual(store.saved_evidence.document_ids, (902,))
                self.assertEqual(store.saved_evidence.report_facts, ())
                self.assertEqual(
                    store.saved_evidence.selection_reason_codes,
                    reason_codes,
                )
                self.assertEqual(
                    tuple(item.source_document_id for item in store.saved_evidence.freshness),
                    (902,),
                )
                self.assertNotIn(
                    "current_periodic_candidate_missing",
                    result.reason_codes,
                )
                self.assertNotIn(
                    "current_periodic_candidate_missing",
                    result.missing_evidence,
                )

    def test_selection_reason_binding_is_audit_only_and_cannot_improve_rank(self) -> None:
        record = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=901,
            title="2026年产品资料概要",
            published_at=NOW,
        )

        def classify_with(reason_codes: tuple[str, ...]):
            store = FakeRiskStore((record,))
            store.snapshot_override = selection_snapshot(
                nonperiodic=(record,),
                reason_codes=reason_codes,
            )
            result = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")
            return result

        baseline = classify_with(())
        bound = classify_with(
            (
                "current_periodic_candidate_conflict",
                "current_periodic_candidate_missing",
            )
        )

        self.assertEqual(bound.product_family, baseline.product_family)
        self.assertEqual(bound.risk_bucket, baseline.risk_bucket)
        self.assertEqual(bound.portfolio_role, baseline.portfolio_role)
        self.assertEqual(bound.evidence_status, baseline.evidence_status)
        self.assertEqual(bound.reason_codes, baseline.reason_codes)
        self.assertEqual(bound.missing_evidence, baseline.missing_evidence)

    def test_mixed_active_and_historical_snapshot_is_entirely_unavailable(self) -> None:
        active = stored_document(
            DocumentKind.QUARTERLY_REPORT,
            artifact_id=901,
            title="2026年第1季度报告",
            published_at=NOW,
        )
        historical = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=902,
            title="2026年产品资料概要",
            published_at=NOW,
            parser_provenance=historical_native_provenance("3"),
        )
        store = FakeRiskStore((active, historical))
        store.snapshot_override = selection_snapshot(
            periodic=(active,),
            nonperiodic=(historical,),
        )

        with self.assertRaises(RiskServiceError) as caught:
            FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")

        self.assertEqual(caught.exception.code, "official_document_unavailable")

    def test_selection_snapshot_change_before_insert_is_evidence_changed(self) -> None:
        record = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=901,
            title="2026年产品资料概要",
            published_at=NOW,
        )
        changes = (
            {"refresh_run_id": 2},
            {"selection_policy_checksum": "d" * 64},
            {"selection_checksum": "d" * 64},
            {"candidate_run_snapshot_checksum": "d" * 64},
            {"reason_codes": ("current_periodic_candidate_missing",)},
        )
        for change in changes:
            with self.subTest(change=tuple(change)):
                store = FakeRiskStore((record,))
                store.snapshot_override = selection_snapshot(nonperiodic=(record,))

                def checkpoint(point: str) -> None:
                    if point == "before_insert":
                        store.snapshot_override = selection_snapshot(
                            nonperiodic=(record,),
                            **change,
                        )

                with self.assertRaises(RiskServiceError) as caught:
                    FundRiskService(
                        risk_store=store,
                        disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                        repository=FakeRepository(),
                        discovery=SimpleNamespace(),
                        document_client=SimpleNamespace(),
                        clock=lambda: NOW,
                        evidence_checkpoint=checkpoint,
                    ).classify("519755")

                self.assertEqual(
                    caught.exception.code,
                    "classification_calculation_failed",
                )
                self.assertEqual(caught.exception.reason, "evidence_changed")

    def test_report_retrieval_time_does_not_extend_freshness(self) -> None:
        report = stored_document(
            DocumentKind.ANNUAL_REPORT,
            artifact_id=901,
            title="公开基金2025年年度报告",
            published_at=datetime(2026, 3, 31, tzinfo=timezone.utc),
        ).artifact
        later_retrieval = replace(report, retrieved_at=NOW + timedelta(days=30))

        original = evidence_freshness(report, ClassificationPolicyV1(), NOW)
        retrieved_later = evidence_freshness(
            later_retrieval,
            ClassificationPolicyV1(),
            NOW,
        )

        self.assertEqual(retrieved_later.observed_at, original.observed_at)
        self.assertEqual(retrieved_later.valid_until, original.valid_until)
        self.assertEqual(retrieved_later.state, original.state)

    def test_same_refresh_nonperiodic_projection_keeps_only_current_update(self) -> None:
        prospectus = stored_document(
            DocumentKind.PROSPECTUS,
            artifact_id=901,
            title="2025年招募说明书",
            published_at=NOW - timedelta(days=365),
        )
        old_update = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=902,
            title="2026年第一次更新招募说明书",
            published_at=NOW - timedelta(days=30),
        )
        current_update = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=903,
            title="2026年第二次更新招募说明书",
            published_at=NOW,
        )
        records = (prospectus, current_update, old_update)
        store = FakeRiskStore(records)
        store.snapshot_override = selection_snapshot(nonperiodic=records)

        FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        self.assertEqual(store.saved_evidence.document_ids, (903,))

    def test_snapshot_storage_revalidation_failure_is_public_storage_failure(self) -> None:
        record = stored_document(
            DocumentKind.PRODUCT_SUMMARY,
            artifact_id=901,
            title="2026年产品资料概要",
            published_at=NOW,
        )

        class RevalidationFailureStore(FakeRiskStore):
            def save_classification(self, *_: object) -> object:
                raise RiskStoreError("private selection snapshot changed")

        store = RevalidationFailureStore((record,))
        store.snapshot_override = selection_snapshot(nonperiodic=(record,))
        with self.assertRaises(RiskServiceError) as caught:
            FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")

        self.assertEqual(caught.exception.code, "classification_storage_failed")
        self.assertNotIn("private selection snapshot changed", repr(caught.exception))

    def test_refresh_start_is_committed_before_discovery_is_called(self) -> None:
        events: list[object] = []

        class RecordingDiscovery(FakeDiscovery):
            def discover(self, fund_code: str, **kwargs: object):
                events.append("discover")
                return super().discover(fund_code, **kwargs)

        store = FakeRiskStore(events=events)
        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=RecordingDiscovery(()),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).sync_documents("519755")

        self.assertEqual(
            events,
            ["begin_refresh", "discover", "publish_selection", "complete_refresh"],
        )
        self.assertEqual(result.status, "empty")
        self.assertEqual(
            store.refreshes[1]["completion"][0],
            RefreshOutcome.EMPTY,
        )

    def test_sync_persists_selection_before_fetch_and_attempts_only_selected_candidates(
        self,
    ) -> None:
        events: list[object] = []
        old_quarter = replace(
            candidate(DocumentKind.QUARTERLY_REPORT, "old-quarter"),
            published_at=NOW - timedelta(days=2),
        )
        new_quarter = replace(
            candidate(DocumentKind.QUARTERLY_REPORT, "new-quarter"),
            published_at=NOW - timedelta(days=1),
        )
        annual = replace(
            candidate(DocumentKind.ANNUAL_REPORT, "new-annual"),
            published_at=NOW - timedelta(days=1),
        )
        summary = candidate(DocumentKind.PRODUCT_SUMMARY, "summary")

        class RecordingDiscovery(FakeDiscovery):
            def discover(self, fund_code: str, **kwargs: object):
                events.append("discover")
                return super().discover(fund_code, **kwargs)

        class RecordingClient:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
                events.append(("fetch", value.url))
                self.urls.append(value.url)
                raise OfficialDocumentUnavailableError(
                    DocumentFailureStage.RETRIEVAL,
                    DocumentFailureReason.NETWORK_UNAVAILABLE,
                    "private network detail",
                )

        store = FakeRiskStore(events=events)
        client = RecordingClient()
        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=RecordingDiscovery((old_quarter, new_quarter, annual, summary)),
            document_client=client,
            clock=lambda: NOW,
        ).sync_documents("519755")

        self.assertEqual(events[:3], ["begin_refresh", "discover", "publish_selection"])
        self.assertEqual(
            client.urls,
            [new_quarter.url, annual.url, summary.url],
        )
        self.assertNotIn(old_quarter.url, client.urls)
        self.assertEqual(
            tuple(item.document_kind for item in result.selections),
            ("annual_report", "quarterly_report", "semiannual_report"),
        )
        self.assertEqual(result.selections[0].selected_url, annual.url)
        self.assertEqual(result.selections[1].selected_url, new_quarter.url)
        self.assertEqual(
            result.selections[2].reason_code,
            "current_periodic_candidate_missing",
        )
        self.assertEqual(
            result.selection_checksum,
            store.published_selections[0].selection_checksum,
        )

    def test_newest_failure_does_not_fallback_to_an_older_periodic_report(self) -> None:
        old = replace(
            candidate(DocumentKind.ANNUAL_REPORT, "old-annual"),
            published_at=NOW - timedelta(days=2),
        )
        newest = replace(
            candidate(DocumentKind.ANNUAL_REPORT, "newest-annual"),
            published_at=NOW - timedelta(days=1),
        )

        class RecordingClient(RaisingDocumentClient):
            def __init__(self) -> None:
                super().__init__(
                    OfficialDocumentUnavailableError(
                        DocumentFailureStage.RETRIEVAL,
                        DocumentFailureReason.NETWORK_UNAVAILABLE,
                        "private network detail",
                    )
                )
                self.urls: list[str] = []

            def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
                self.urls.append(value.url)
                return super().fetch(value)

        client = RecordingClient()
        result = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((old, newest)),
            document_client=client,
            clock=lambda: NOW,
        ).sync_documents("519755")

        self.assertEqual(client.urls, [newest.url])
        self.assertEqual(result.documents[0].url, newest.url)
        self.assertEqual(result.documents[0].failure_stage, "retrieval")
        self.assertEqual(result.documents[0].failure_reason, "network_unavailable")
        with self.assertRaises(ValueError):
            replace(
                result,
                documents=(replace(result.documents[0], url=old.url),),
            ).validate()
        with self.assertRaises(ValueError):
            replace(result, selection_checksum="A" * 64).validate()

    def test_newest_time_tie_is_persisted_and_downloads_neither_candidate(self) -> None:
        first = candidate(DocumentKind.QUARTERLY_REPORT, "quarter-a")
        second = candidate(DocumentKind.QUARTERLY_REPORT, "quarter-b")

        class RejectingClient:
            def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
                raise AssertionError(f"conflicted candidate must not be fetched: {value.url}")

        store = FakeRiskStore()
        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((first, second)),
            document_client=RejectingClient(),
            clock=lambda: NOW,
        ).sync_documents("519755")

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.documents, ())
        quarterly = next(
            item for item in result.selections if item.document_kind == "quarterly_report"
        )
        self.assertEqual(quarterly.status, "conflicted")
        self.assertEqual(quarterly.candidate_count, 2)
        self.assertIsNone(quarterly.selected_url)
        self.assertEqual(
            quarterly.reason_code,
            "current_periodic_candidate_conflict",
        )
        self.assertEqual(store.candidate_outcomes, [])

    def test_published_selection_is_not_mixed_when_upstream_candidates_change(self) -> None:
        selected = replace(
            candidate(DocumentKind.ANNUAL_REPORT, "selected-annual"),
            published_at=NOW - timedelta(days=1),
        )
        later = candidate(DocumentKind.ANNUAL_REPORT, "later-annual")
        discovery = FakeDiscovery((selected,))

        class SwitchingStore(FakeRiskStore):
            def publish_document_selection(self, plan: object, created_at: datetime) -> object:
                stored = super().publish_document_selection(plan, created_at)
                discovery.candidates = (later,)
                return stored

        class RecordingClient(RaisingDocumentClient):
            def __init__(self) -> None:
                super().__init__(
                    OfficialDocumentUnavailableError(
                        DocumentFailureStage.RETRIEVAL,
                        DocumentFailureReason.NETWORK_UNAVAILABLE,
                        "private network detail",
                    )
                )
                self.urls: list[str] = []

            def fetch(self, value: OfficialDocumentCandidate) -> RetrievedArtifact:
                self.urls.append(value.url)
                return super().fetch(value)

        client = RecordingClient()
        result = FundRiskService(
            risk_store=SwitchingStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=discovery,
            document_client=client,
            clock=lambda: NOW,
        ).sync_documents("519755")

        self.assertEqual(client.urls, [selected.url])
        annual = next(
            item for item in result.selections if item.document_kind == "annual_report"
        )
        self.assertEqual(annual.selected_url, selected.url)
        self.assertNotEqual(annual.selected_url, later.url)

    def test_document_selection_item_rejects_unknown_or_private_state(self) -> None:
        valid = DocumentSelectionItem(
            document_kind="annual_report",
            status="selected",
            selected_url="https://www.fund001.com/annual.html",
            candidate_count=1,
            reason_code=None,
        )
        valid.validate()
        invalid = (
            replace(valid, status="unknown"),
            replace(valid, reason_code="current_periodic_candidate_missing"),
            replace(valid, selected_url=None),
            replace(valid, candidate_count=0),
        )
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                item.validate()
        object.__setattr__(valid, "candidate_fingerprints", ("a" * 64,))
        with self.assertRaises(ValueError):
            valid.validate()

    def test_discovery_failure_completes_refresh_failed_without_reusing_history(self) -> None:
        historical = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="历史招募说明书",
            published_at=NOW - timedelta(days=1),
        )
        store = FakeRiskStore((historical,))
        failure = OfficialDocumentUnavailableError(
            DocumentFailureStage.DISCOVERY,
            DocumentFailureReason.NETWORK_UNAVAILABLE,
            "private discovery detail",
        )
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=FakeRepository(),
            discovery=RaisingDiscovery(failure),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )

        with self.assertRaises(RiskServiceError) as caught:
            service.sync_documents("519755")

        completion = store.refreshes[1]["completion"]
        self.assertEqual(caught.exception.code, "official_document_unavailable")
        self.assertEqual(completion[0], RefreshOutcome.FAILED)
        self.assertEqual(completion[2], failure.failure)
        with self.assertRaises(RiskServiceError) as classify_error:
            FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")
        self.assertEqual(classify_error.exception.code, "official_document_unavailable")

    def test_process_like_interruption_leaves_incomplete_refresh_that_blocks_classify(
        self,
    ) -> None:
        class ProcessLikeInterruption(BaseException):
            pass

        class InterruptingDiscovery:
            def discover(self, fund_code: str, **_: object) -> tuple:
                del fund_code
                raise ProcessLikeInterruption()

        store = FakeRiskStore()
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=FakeRepository(),
            discovery=InterruptingDiscovery(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )

        with self.assertRaises(ProcessLikeInterruption):
            service.sync_documents("519755")

        self.assertIsNone(store.refreshes[1]["completion"])
        with self.assertRaises(RiskServiceError) as caught:
            FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            ).classify("519755")
        self.assertEqual(caught.exception.code, "official_document_unavailable")

    def test_current_reads_reject_stored_result_when_current_gate_or_evidence_changes(
        self,
    ) -> None:
        store = FakeRiskStore()
        disclosure_store = FakeDisclosureStore(disclosure_bundle())
        service = FundRiskService(
            risk_store=store,
            disclosure_store=disclosure_store,
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )
        service.classify("519755")

        disclosure_store.bundle = replace(
            disclosure_store.bundle,
            identity=replace(disclosure_store.bundle.identity, fund_type="mixed"),
        )
        for read in (
            lambda: service.current_classification("519755"),
            lambda: service.classification_evidence("519755"),
        ):
            with self.subTest(read=read):
                with self.assertRaises(RiskServiceError) as caught:
                    read()
                self.assertEqual(caught.exception.code, "classification_calculation_failed")
                self.assertEqual(caught.exception.reason, "evidence_changed")

        historical = service.classification_evidence("519755", classification_id=1)
        self.assertIsNotNone(historical)
        self.assertEqual(len(service.classification_history("519755")), 1)

        disclosure_store.bundle = disclosure_bundle()
        refresh_id = store.begin_document_refresh("519755", NOW + timedelta(minutes=1))
        failure = SafeDocumentFailure(
            "official_document_unavailable",
            DocumentFailureStage.DISCOVERY,
            DocumentFailureReason.NETWORK_UNAVAILABLE,
        )
        store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.FAILED,
            NOW + timedelta(minutes=1, seconds=1),
            failure=failure,
        )
        for read in (
            lambda: service.current_classification("519755"),
            lambda: service.classification_evidence("519755"),
        ):
            with self.subTest(read=read):
                with self.assertRaises(RiskServiceError) as caught:
                    read()
                self.assertEqual(caught.exception.code, "official_document_unavailable")

        incomplete_id = store.begin_document_refresh(
            "519755",
            NOW + timedelta(minutes=2),
        )
        for read in (
            lambda: service.current_classification("519755"),
            lambda: service.classification_evidence("519755"),
        ):
            with self.subTest(gate="incomplete", read=read):
                with self.assertRaises(RiskServiceError) as caught:
                    read()
                self.assertEqual(caught.exception.code, "official_document_unavailable")

        store.complete_document_refresh(
            incomplete_id,
            RefreshOutcome.EMPTY,
            NOW + timedelta(minutes=2, seconds=1),
        )
        for read in (
            lambda: service.current_classification("519755"),
            lambda: service.classification_evidence("519755"),
        ):
            with self.subTest(gate="empty", read=read):
                with self.assertRaises(RiskServiceError) as caught:
                    read()
                self.assertEqual(caught.exception.code, "official_document_unavailable")

    def test_current_read_rechecks_evidence_after_authenticated_record_load(self) -> None:
        disclosure_store = FakeDisclosureStore(disclosure_bundle())

        class SwitchingStore(FakeRiskStore):
            switch_on_read = False

            def classification_evidence(
                self,
                fund_code: str,
                classification_id: Optional[int] = None,
            ) -> object:
                record = super().classification_evidence(fund_code, classification_id)
                if self.switch_on_read and classification_id is None:
                    disclosure_store.bundle = replace(
                        disclosure_store.bundle,
                        identity=replace(disclosure_store.bundle.identity, fund_type="mixed"),
                    )
                return record

        store = SwitchingStore()
        service = FundRiskService(
            risk_store=store,
            disclosure_store=disclosure_store,
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )
        service.classify("519755")
        store.switch_on_read = True

        with self.assertRaises(RiskServiceError) as caught:
            service.classification_evidence("519755")

        self.assertEqual(caught.exception.code, "classification_calculation_failed")
        self.assertEqual(caught.exception.reason, "evidence_changed")

    def test_landing_retrieval_and_container_failures_persist_candidate_outcomes(self) -> None:
        failures = (
            OfficialDocumentError(
                DocumentFailureStage.LANDING_VALIDATION,
                DocumentFailureReason.LANDING_FORMAT_INVALID,
                "private landing detail",
            ),
            OfficialDocumentUnavailableError(
                DocumentFailureStage.RETRIEVAL,
                DocumentFailureReason.NETWORK_UNAVAILABLE,
                "private retrieval detail",
            ),
            OfficialDocumentError(
                DocumentFailureStage.CONTAINER_VALIDATION,
                DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
                "private container detail",
            ),
        )
        for index, failure in enumerate(failures):
            with self.subTest(stage=failure.failure.stage):
                store = FakeRiskStore()
                item = candidate(DocumentKind.ANNUAL_REPORT, f"failure-{index}")
                result = FundRiskService(
                    risk_store=store,
                    disclosure_store=FakeDisclosureStore(),
                    repository=SimpleNamespace(),
                    discovery=FakeDiscovery((item,)),
                    document_client=RaisingDocumentClient(failure),
                    clock=lambda: NOW,
                ).sync_documents("519755")

                self.assertEqual(store.candidate_outcomes, [("failure", 1, item)])
                self.assertEqual(result.documents[0].failure_stage, failure.failure.stage.value)
                self.assertEqual(
                    result.documents[0].failure_reason,
                    failure.failure.reason_code.value,
                )

    def test_partial_refresh_uses_only_same_refresh_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "kunjin.sqlite3"
            first_path = root / "first.html"
            second_path = root / "second.html"
            first_path.write_text("<html>first</html>", encoding="utf-8")
            second_path.write_text("<html>second</html>", encoding="utf-8")
            repository = Repository(database)
            repository.migrate()
            store = FundRiskStore(repository)
            old = parsed_document_for(
                first_path,
                candidate(DocumentKind.PROSPECTUS_UPDATE, "old"),
            )
            publish_current_document(store, old, started_at=NOW - timedelta(minutes=1))
            current = parsed_document_for(
                second_path,
                candidate(DocumentKind.ANNUAL_REPORT, "current"),
                facts=(
                    ParsedMandateFact(
                        fact_kind="current_stock_asset_allocation_percent",
                        normalized_value=Decimal("20"),
                        unit="percent_of_net_assets",
                        page_number=1,
                        section_name="资产配置",
                        source_excerpt="股票资产占基金资产净值比例为20%。",
                        effective_from=date(2025, 12, 31),
                        effective_to=date(2025, 12, 31),
                        confidence_state=FactConfidence.EXACT,
                        fact_fingerprint=fact_fingerprint(
                            fact_kind="current_stock_asset_allocation_percent",
                            normalized_value=Decimal("20"),
                            unit="percent_of_net_assets",
                            page_number=1,
                            section_name="资产配置",
                            source_excerpt="股票资产占基金资产净值比例为20%。",
                            effective_from=date(2025, 12, 31),
                            effective_to=date(2025, 12, 31),
                            confidence_state=FactConfidence.EXACT,
                        ),
                    ),
                ),
            )
            refresh_id = store.begin_document_refresh("519755", NOW)
            failed = candidate(DocumentKind.PRODUCT_SUMMARY, "failed")
            selection = select_current_candidates(
                "519755",
                refresh_run_id=refresh_id,
                candidates=(current.artifact.candidate, failed),
            )
            store.publish_document_selection(selection, NOW)
            current_record = store.publish_candidate_success(
                refresh_id=refresh_id,
                candidate=current.artifact.candidate,
                parsed=current,
                provenance=native_parser_provenance(),
                parser_input_sha256=current.artifact.sha256,
                attempted_at=NOW,
            )
            store.publish_candidate_failure(
                refresh_id=refresh_id,
                candidate=failed,
                failure=SafeDocumentFailure(
                    "official_document_unavailable",
                    DocumentFailureStage.RETRIEVAL,
                    DocumentFailureReason.NETWORK_UNAVAILABLE,
                ),
                attempted_at=NOW,
                artifact=None,
                provenance=None,
            )
            store.complete_document_refresh(
                refresh_id,
                RefreshOutcome.PARTIAL,
                NOW + timedelta(seconds=1),
            )
            service = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                repository=repository,
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            )

            service.classify("519755")
            self.assertEqual(
                store.current_parsed_documents(
                    "519755", (native_parser_provenance().provenance_checksum,)
                ),
                (current_record,),
            )

    def test_latest_parse_failure_blocks_older_success_for_independent_classify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "document.html"
            path.write_text("<html>current</html>", encoding="utf-8")
            repository = Repository(root / "kunjin.sqlite3")
            repository.migrate()
            store = FundRiskStore(repository)
            parsed = parsed_document_for(
                path,
                candidate(DocumentKind.PROSPECTUS_UPDATE, "current"),
            )
            publish_current_document(store, parsed)
            failed_refresh = store.begin_document_refresh("519755", NOW + timedelta(minutes=1))
            failure = SafeDocumentFailure(
                "official_document_parse_failed",
                DocumentFailureStage.PARSER,
                DocumentFailureReason.PARSER_FORMAT_INVALID,
            )
            store.publish_candidate_parse_failure(
                refresh_id=failed_refresh,
                candidate=parsed.artifact.candidate,
                artifact=parsed.artifact,
                provenance=native_parser_provenance(),
                failure=failure,
                attempted_at=NOW + timedelta(minutes=1),
            )
            store.complete_document_refresh(
                failed_refresh,
                RefreshOutcome.FAILED,
                NOW + timedelta(minutes=1, seconds=1),
                failure=failure,
            )

            with self.assertRaises(RiskServiceError) as caught:
                FundRiskService(
                    risk_store=FundRiskStore(repository),
                    disclosure_store=FakeDisclosureStore(disclosure_bundle()),
                    repository=repository,
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW + timedelta(minutes=2),
                ).classify("519755")

        self.assertEqual(caught.exception.code, "official_document_unavailable")

    def test_storage_failure_leaves_refresh_incomplete_and_returns_existing_public_code(
        self,
    ) -> None:
        class FailingCandidateStore(FakeRiskStore):
            def publish_candidate_failure(self, **_: object) -> None:
                raise RiskStoreError("private storage detail")

        store = FailingCandidateStore()
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "failure"),)),
            document_client=RaisingDocumentClient(
                OfficialDocumentUnavailableError(
                    DocumentFailureStage.RETRIEVAL,
                    DocumentFailureReason.NETWORK_UNAVAILABLE,
                    "private retrieval detail",
                )
            ),
            clock=lambda: NOW,
        )

        with self.assertRaises(RiskServiceError) as caught:
            service.sync_documents("519755")

        self.assertEqual(caught.exception.code, "classification_storage_failed")
        self.assertIsNone(store.refreshes[1]["completion"])
        self.assertNotIn("private storage detail", repr(caught.exception))

    def test_sync_loads_disclosure_bundle_once(self) -> None:
        class CountingDisclosureStore(FakeDisclosureStore):
            def __init__(self) -> None:
                super().__init__()
                self.loaded_fund_codes: list[str] = []

            def load_bundle(self, fund_code: str) -> object:
                self.loaded_fund_codes.append(fund_code)
                return super().load_bundle(fund_code)

        disclosure_store = CountingDisclosureStore()
        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=disclosure_store,
            repository=SimpleNamespace(),
            discovery=FakeDiscovery(()),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )

        result = service.sync_documents("519755")

        self.assertEqual(result.status, "empty")
        self.assertEqual(disclosure_store.loaded_fund_codes, ["519755"])

    def test_current_sourced_identity_creates_bound_name_and_platform_facts(self) -> None:
        store = FakeRiskStore()
        FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(
                sourced_identity_bundle(
                    fund_name="公开货币基金",
                    fund_type="混合型-偏债",
                )
            ),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        facts = {item.fact_kind: item for item in store.saved_evidence.existing_disclosure_facts}
        self.assertEqual(facts["fund_name"].normalized_value, "公开货币基金")
        self.assertEqual(facts["platform_category"].normalized_value, "混合型-偏债")
        self.assertEqual(facts["fund_name"].section_name, "identity")
        self.assertEqual(facts["platform_category"].section_name, "identity")
        self.assertEqual(facts["fund_name"].source_document_id, 101)
        self.assertIn(
            ("identity", dict(store.saved_evidence.external_evidence_fingerprints)["identity"]),
            store.saved_evidence.external_evidence_fingerprints,
        )
        self.assertIn(
            (101, "identity"),
            {
                (item.source_document_id, item.section)
                for item in store.saved_evidence.external_source_references
            },
        )

    def test_unsourced_or_stale_identity_does_not_create_scope_facts(self) -> None:
        bundles = (
            sourced_identity_bundle(sourced=False),
            sourced_identity_bundle(retrieved_at=NOW - timedelta(days=31)),
        )
        for bundle in bundles:
            with self.subTest(bundle=bundle):
                store = FakeRiskStore()
                FundRiskService(
                    risk_store=store,
                    disclosure_store=FakeDisclosureStore(bundle),
                    repository=FakeRepository(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW,
                ).classify("519755")
                kinds = {item.fact_kind for item in store.saved_evidence.existing_disclosure_facts}
                self.assertNotIn("fund_name", kinds)
                self.assertNotIn("platform_category", kinds)

    def test_sync_isolates_candidates_and_reports_partial_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>基金类型：债券型基金</body></html>", encoding="utf-8")
            successful = candidate(DocumentKind.PROSPECTUS_UPDATE, "success")
            failed = candidate(DocumentKind.ANNUAL_REPORT, "failure")
            risk_store = FakeRiskStore()
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((successful, failed)),
                document_client=FakeDocumentClient(path, failed.url),
                parser=lambda artifact: ParsedRiskDocument(
                    artifact=artifact,
                    facts=(),
                    warnings=(),
                    conflicts=(),
                ),
                clock=lambda: NOW,
            )

            result = service.sync_documents("519755")

        self.assertEqual(result.status, "partial")
        self.assertEqual([item.status for item in result.documents], ["success", "failed"])
        self.assertIsNone(result.documents[0].failure_stage)
        self.assertIsNone(result.documents[0].failure_reason)
        self.assertEqual(result.documents[1].error_code, "official_document_unavailable")
        self.assertEqual(result.documents[1].failure_stage, "retrieval")
        self.assertEqual(result.documents[1].failure_reason, "network_unavailable")
        self.assertEqual(len(risk_store.published), 1)
        self.assertEqual(risk_store.failed_artifacts, [])
        self.assertEqual(result.capability, "research_only")
        self.assertNotIn(str(path), repr(result))
        self.assertNotIn("network detail", repr(result))

    def test_parse_failure_audits_artifact_without_publishing_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            item = candidate(DocumentKind.PROSPECTUS, "parse-failure")
            risk_store = FakeRiskStore()

            def fail_parse(_: RetrievedArtifact) -> ParsedRiskDocument:
                raise RiskDocumentParseError(
                    "official_document_parse_failed",
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                    "private parser exception detail",
                )

            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((item,)),
                document_client=FakeDocumentClient(path, ""),
                parser=fail_parse,
                clock=lambda: NOW,
            )
            result = service.sync_documents("519755")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.documents[0].error_code, "official_document_parse_failed")
        self.assertEqual(result.documents[0].failure_stage, "parser")
        self.assertEqual(result.documents[0].failure_reason, "parser_format_invalid")
        self.assertEqual(risk_store.published, [])
        self.assertEqual(len(risk_store.failed_artifacts), 1)
        self.assertEqual(
            risk_store.failed_artifacts[0][1:],
            (PARSER_VERSION, "official_document_parse_failed"),
        )
        self.assertNotIn("private parser exception detail", repr(result))

    def test_parser_boundary_rejects_non_exact_or_rebound_parsed_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            item = candidate(DocumentKind.PROSPECTUS, "parser-boundary")

            def non_exact(artifact: RetrievedArtifact) -> object:
                return SimpleNamespace(
                    artifact=artifact,
                    warnings=(),
                    conflicts=(),
                    validate=lambda: None,
                )

            def rebound(artifact: RetrievedArtifact) -> ParsedRiskDocument:
                other = replace(
                    artifact,
                    candidate=candidate(DocumentKind.PROSPECTUS, "other-candidate"),
                )
                return ParsedRiskDocument(
                    artifact=other,
                    facts=(),
                    warnings=(),
                    conflicts=(),
                )

            for parser in (non_exact, rebound):
                with self.subTest(parser=parser.__name__):
                    store = FakeRiskStore()
                    result = FundRiskService(
                        risk_store=store,
                        disclosure_store=FakeDisclosureStore(),
                        repository=SimpleNamespace(),
                        discovery=FakeDiscovery((item,)),
                        document_client=FakeDocumentClient(path, ""),
                        parser=parser,
                        clock=lambda: NOW,
                    ).sync_documents("519755")

                    self.assertEqual(result.status, "failed")
                    self.assertEqual(
                        result.documents[0].error_code,
                        "official_document_parse_failed",
                    )
                    self.assertEqual(result.documents[0].failure_stage, "parser")
                    self.assertEqual(
                        result.documents[0].failure_reason,
                        "parser_format_invalid",
                    )
                    self.assertEqual(store.candidate_outcomes, [("parse_failure", 1, item)])
                    self.assertEqual(store.failed_artifacts[0][0].candidate, item)
                    self.assertEqual(
                        store.refreshes[1]["completion"][0],
                        RefreshOutcome.FAILED,
                    )

    def test_sync_propagates_safe_diagnostics_and_observes_each_failure(self) -> None:
        observed: list[SafeDocumentFailure] = []
        failure = SafeDocumentFailure(
            "official_document_invalid",
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
        )
        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "legacy"),)),
            document_client=RaisingDocumentClient(
                OfficialDocumentError(
                    failure.stage,
                    failure.reason_code,
                    "private URL path and response detail",
                )
            ),
            clock=lambda: NOW,
            failure_observer=observed.append,
        )

        result = service.sync_documents("519755")

        item = result.documents[0]
        self.assertEqual(item.error_code, "official_document_invalid")
        self.assertEqual(item.failure_stage, "container_validation")
        self.assertEqual(item.failure_reason, "legacy_ole_container_unsupported")
        self.assertEqual(observed, [failure])
        self.assertNotIn("private URL", repr(result))
        self.assertNotIn("response detail", repr(result))

    def test_unknown_and_observer_failures_remain_fail_closed(self) -> None:
        observed: list[SafeDocumentFailure] = []

        def broken_observer(failure: SafeDocumentFailure) -> None:
            observed.append(failure)
            raise RuntimeError("private observer sentinel")

        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "unknown"),)),
            document_client=RaisingDocumentClient(
                RuntimeError("official_document_unavailable private client sentinel")
            ),
            clock=lambda: NOW,
            failure_observer=broken_observer,
        )

        result = service.sync_documents("519755")

        item = result.documents[0]
        self.assertEqual(item.error_code, "official_document_invalid")
        self.assertEqual(item.failure_stage, "unspecified")
        self.assertEqual(item.failure_reason, "unspecified_failure")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].public_code, "official_document_invalid")
        self.assertNotIn("private client sentinel", repr(result))
        self.assertNotIn("private observer sentinel", repr(result))

    def test_observer_base_exception_is_isolated_but_main_interruption_is_not(self) -> None:
        class ProcessLikeInterruption(BaseException):
            pass

        observed: list[SafeDocumentFailure] = []

        def interrupting_observer(failure: SafeDocumentFailure) -> None:
            observed.append(failure)
            raise ProcessLikeInterruption("private observer interruption")

        store = FakeRiskStore()
        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "observer"),)),
            document_client=RaisingDocumentClient(
                OfficialDocumentUnavailableError(
                    DocumentFailureStage.RETRIEVAL,
                    DocumentFailureReason.NETWORK_UNAVAILABLE,
                    "private retrieval detail",
                )
            ),
            clock=lambda: NOW,
            failure_observer=interrupting_observer,
        ).sync_documents("519755")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.documents[0].error_code, "official_document_unavailable")
        self.assertEqual(len(observed), 1)

        interrupted_store = FakeRiskStore()

        def interrupting_parser(_: RetrievedArtifact) -> ParsedRiskDocument:
            raise ProcessLikeInterruption("main parser interruption")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html>public</html>", encoding="utf-8")
            with self.assertRaises(ProcessLikeInterruption):
                FundRiskService(
                    risk_store=interrupted_store,
                    disclosure_store=FakeDisclosureStore(),
                    repository=SimpleNamespace(),
                    discovery=FakeDiscovery((candidate(DocumentKind.PROSPECTUS, "interruption"),)),
                    document_client=FakeDocumentClient(path, ""),
                    parser=interrupting_parser,
                    clock=lambda: NOW,
                ).sync_documents("519755")

        self.assertIsNone(interrupted_store.refreshes[1]["completion"])

    def test_discovery_failure_is_observed_and_existing_service_error_is_preserved(
        self,
    ) -> None:
        observed: list[SafeDocumentFailure] = []

        def broken_observer(failure: SafeDocumentFailure) -> None:
            observed.append(failure)
            raise RuntimeError("private discovery observer sentinel")

        failure = SafeDocumentFailure(
            "official_document_unavailable",
            DocumentFailureStage.DISCOVERY,
            DocumentFailureReason.NETWORK_UNAVAILABLE,
        )
        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=RaisingDiscovery(
                OfficialDocumentUnavailableError(
                    failure.stage,
                    failure.reason_code,
                    "private discovery sentinel",
                )
            ),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
            failure_observer=broken_observer,
        )

        with self.assertRaises(RiskServiceError) as caught:
            service.sync_documents("519755")

        self.assertEqual(caught.exception.code, "official_document_unavailable")
        self.assertIsNone(caught.exception.reason)
        self.assertEqual(str(caught.exception), "official_document_unavailable")
        self.assertEqual(observed, [failure])
        self.assertNotIn("private discovery sentinel", repr(caught.exception))
        self.assertNotIn("private discovery observer sentinel", repr(caught.exception))

        existing = RiskServiceError(
            "classification_calculation_failed",
            reason="evidence_changed",
        )
        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=RaisingDiscovery(existing),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
            failure_observer=observed.append,
        )
        with self.assertRaises(RiskServiceError) as preserved:
            service.sync_documents("519755")
        self.assertIs(preserved.exception, existing)
        self.assertEqual(
            str(preserved.exception),
            "classification_calculation_failed: evidence_changed",
        )
        self.assertEqual(
            observed,
            [
                failure,
                SafeDocumentFailure(
                    "official_document_invalid",
                    DocumentFailureStage.UNSPECIFIED,
                    DocumentFailureReason.UNSPECIFIED_FAILURE,
                ),
            ],
        )

    def test_unexpected_parser_failure_uses_safe_parser_format_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            observed: list[SafeDocumentFailure] = []

            def fail_parse(_: RetrievedArtifact) -> ParsedRiskDocument:
                raise RuntimeError("official_document_resource_limit private parser sentinel")

            service = FundRiskService(
                risk_store=FakeRiskStore(),
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery(
                    (candidate(DocumentKind.PROSPECTUS, "unexpected-parser-failure"),)
                ),
                document_client=FakeDocumentClient(path, ""),
                parser=fail_parse,
                clock=lambda: NOW,
                failure_observer=observed.append,
            )

            result = service.sync_documents("519755")

        item = result.documents[0]
        self.assertEqual(item.error_code, "official_document_parse_failed")
        self.assertEqual(item.failure_stage, "parser")
        self.assertEqual(item.failure_reason, "parser_format_invalid")
        self.assertEqual(
            observed,
            [
                SafeDocumentFailure(
                    "official_document_parse_failed",
                    DocumentFailureStage.PARSER,
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                )
            ],
        )
        self.assertNotIn("private parser sentinel", repr(result))

    def test_parser_resource_failure_propagates_safe_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            observed: list[SafeDocumentFailure] = []

            def fail_parse(_: RetrievedArtifact) -> ParsedRiskDocument:
                raise RiskDocumentParseError(
                    "official_document_resource_limit",
                    DocumentFailureReason.RESOURCE_LIMIT,
                    "private resource detail",
                )

            risk_store = FakeRiskStore()
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "resource-limit"),)),
                document_client=FakeDocumentClient(path, ""),
                parser=fail_parse,
                clock=lambda: NOW,
                failure_observer=observed.append,
            )

            result = service.sync_documents("519755")

        item = result.documents[0]
        self.assertEqual(item.error_code, "official_document_resource_limit")
        self.assertEqual(item.failure_stage, "parser")
        self.assertEqual(item.failure_reason, "resource_limit")
        self.assertEqual(
            observed,
            [
                SafeDocumentFailure(
                    "official_document_resource_limit",
                    DocumentFailureStage.PARSER,
                    DocumentFailureReason.RESOURCE_LIMIT,
                )
            ],
        )
        self.assertEqual(len(risk_store.failed_artifacts), 1)
        self.assertEqual(
            risk_store.failed_artifacts[0][2],
            "official_document_resource_limit",
        )
        self.assertNotIn("private resource detail", repr(result))

    def test_failed_artifact_storage_failure_overrides_candidate_failure(self) -> None:
        class FailingArtifactStore(FakeRiskStore):
            def publish_candidate_parse_failure(self, **_: object) -> object:
                raise RuntimeError("private storage sentinel")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.html"
            path.write_text("<html><body>public document</body></html>", encoding="utf-8")
            observed: list[SafeDocumentFailure] = []

            def fail_parse(_: RetrievedArtifact) -> ParsedRiskDocument:
                raise RiskDocumentParseError(
                    "official_document_parse_failed",
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                    "private parser sentinel",
                )

            risk_store = FailingArtifactStore()
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FakeDisclosureStore(),
                repository=SimpleNamespace(),
                discovery=FakeDiscovery((candidate(DocumentKind.PROSPECTUS, "storage-failure"),)),
                document_client=FakeDocumentClient(path, ""),
                parser=fail_parse,
                clock=lambda: NOW,
                failure_observer=observed.append,
            )

            with self.assertRaises(RiskServiceError) as caught:
                service.sync_documents("519755")

        self.assertEqual(caught.exception.code, "classification_storage_failed")
        self.assertIsNone(risk_store.refreshes[1]["completion"])
        self.assertEqual(
            observed,
            [
                SafeDocumentFailure(
                    "classification_storage_failed",
                    DocumentFailureStage.PERSISTENCE,
                    DocumentFailureReason.STORAGE_FAILURE,
                )
            ],
        )
        self.assertNotIn("private parser sentinel", repr(caught.exception))
        self.assertNotIn("private storage sentinel", repr(caught.exception))

    def test_official_resource_failure_is_not_collapsed_to_unknown(self) -> None:
        failure = OfficialDocumentResourceLimitError(
            DocumentFailureStage.RETRIEVAL,
            DocumentFailureReason.RESOURCE_LIMIT,
            "private retrieval limit detail",
        )
        observed: list[SafeDocumentFailure] = []
        service = FundRiskService(
            risk_store=FakeRiskStore(),
            disclosure_store=FakeDisclosureStore(),
            repository=SimpleNamespace(),
            discovery=FakeDiscovery((candidate(DocumentKind.ANNUAL_REPORT, "limit"),)),
            document_client=RaisingDocumentClient(failure),
            clock=lambda: NOW,
            failure_observer=observed.append,
        )

        result = service.sync_documents("519755")

        item = result.documents[0]
        self.assertEqual(item.error_code, "official_document_resource_limit")
        self.assertEqual(item.failure_stage, "retrieval")
        self.assertEqual(item.failure_reason, "resource_limit")
        self.assertEqual(observed, [failure.failure])
        self.assertNotIn("private retrieval limit detail", repr(result))

    def test_freshness_uses_report_deadlines_and_one_year_reviews(self) -> None:
        policy = ClassificationPolicyV1()
        annual = stored_document(
            DocumentKind.ANNUAL_REPORT,
            artifact_id=1,
            title="交银某基金2025年年度报告",
            published_at=datetime(2026, 3, 31, tzinfo=timezone.utc),
        ).artifact
        q1 = stored_document(
            DocumentKind.QUARTERLY_REPORT,
            artifact_id=2,
            title="交银某基金2026年第1季度报告",
            published_at=datetime(2026, 4, 20, tzinfo=timezone.utc),
        ).artifact
        methodology = stored_document(
            DocumentKind.INDEX_METHODOLOGY,
            artifact_id=3,
            title="指数编制方案",
            published_at=datetime(2025, 7, 14, tzinfo=timezone.utc),
        ).artifact
        contract = stored_document(
            DocumentKind.FUND_CONTRACT,
            artifact_id=4,
            title="基金合同",
            published_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ).artifact

        annual_state = evidence_freshness(annual, policy, NOW)
        q1_state = evidence_freshness(q1, policy, NOW)
        methodology_state = evidence_freshness(methodology, policy, NOW)
        contract_state = evidence_freshness(contract, policy, NOW)

        self.assertEqual(annual_state.state.value, "stale")
        self.assertEqual(annual_state.valid_until.date().isoformat(), "2026-04-30")
        self.assertEqual(q1_state.state.value, "current")
        self.assertEqual(q1_state.valid_until.date().isoformat(), "2026-09-13")
        self.assertEqual(methodology_state.state.value, "current")
        self.assertEqual(methodology_state.valid_until.date().isoformat(), "2026-07-14")
        self.assertEqual(contract_state.state.value, "current")
        self.assertEqual(contract_state.valid_until.year, datetime.max.year)

    def test_new_legal_document_supersedes_old_version(self) -> None:
        old_prospectus = stored_document(
            DocumentKind.PROSPECTUS,
            artifact_id=1,
            title="old prospectus",
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        old_update = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=2,
            title="old update",
            published_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        )
        current_update = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=3,
            title="current update",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        )

        selected = select_current_documents((old_prospectus, current_update, old_update))

        self.assertEqual([item.artifact.id for item in selected], [3])

    def test_newer_semiannual_fact_supersedes_annual_value_without_conflict(self) -> None:
        legal = stored_fact(
            fact_id=1,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        annual_value = stored_fact(
            fact_id=2,
            document_id=2,
            fact_kind="current_stock_asset_allocation_percent",
            value=Decimal("10"),
            unit="percent_of_net_assets",
        )
        semiannual_value = stored_fact(
            fact_id=3,
            document_id=3,
            fact_kind="current_stock_asset_allocation_percent",
            value=Decimal("20"),
            unit="percent_of_net_assets",
        )
        records = (
            stored_document(
                DocumentKind.PROSPECTUS_UPDATE,
                artifact_id=1,
                title="2026年更新招募说明书",
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                facts=(legal,),
            ),
            stored_document(
                DocumentKind.ANNUAL_REPORT,
                artifact_id=2,
                title="2025年年度报告",
                published_at=datetime(2026, 3, 31, tzinfo=timezone.utc),
                facts=(annual_value,),
            ),
            stored_document(
                DocumentKind.SEMIANNUAL_REPORT,
                artifact_id=3,
                title="2026年半年度报告",
                published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
                facts=(semiannual_value,),
            ),
        )
        self.assertEqual(
            {record.artifact.id for record in select_current_documents(records)},
            {1, 2, 3},
        )
        store = FakeRiskStore(records)
        service = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )

        result = service.classify("519755")

        self.assertNotIn("source_version_conflict", result.conflicts)
        report_facts = store.saved_evidence.report_facts
        self.assertEqual(len(report_facts), 1)
        self.assertEqual(report_facts[0].normalized_value, Decimal("20"))
        self.assertEqual(store.saved_evidence.document_ids, (1, 3))

    def test_classify_binds_external_sections_and_formal_nav_window(self) -> None:
        fact = stored_fact(
            fact_id=10,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(fact,),
        )
        nav = FundNavObservation(
            fund_code="519755",
            nav_date=date(2026, 7, 10),
            unit_nav=Decimal("1.2345"),
            accumulated_nav=Decimal("1.5678"),
            daily_growth=Decimal("0.10"),
            source="eastmoney_formal_nav",
            retrieved_at=NOW,
        )
        risk_store = FakeRiskStore((record,))
        service = FundRiskService(
            risk_store=risk_store,
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository((nav,)),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        )

        result = service.classify("519755")

        self.assertEqual(result.capability, "research_only")
        self.assertIn("unsupported_product_family", result.reason_codes)
        evidence = risk_store.saved_evidence
        self.assertEqual(
            [name for name, _ in evidence.external_evidence_fingerprints],
            ["benchmark", "fees", "holdings", "identity", "industry", "share_class", "size"],
        )
        self.assertIsNotNone(evidence.nav_evidence_fingerprint)
        self.assertEqual(evidence.nav_observation_start, nav.nav_date)
        self.assertEqual(evidence.nav_observation_end, nav.nav_date)
        self.assertEqual(evidence.parse_result_ids, (record.parse_result.id,))
        self.assertEqual(
            evidence.parser_provenance_checksums,
            (record.provenance.provenance_checksum,),
        )
        report = build_authenticated_risk_research_report(
            SimpleNamespace(
                evidence=evidence,
                classification=SimpleNamespace(classified_at=result.classified_at),
                documents=(record.artifact,),
            )
        )
        self.assertEqual(report["capability"], "research_only")
        self.assertNotIn("managed_path", repr(report))
        self.assertEqual(report["sources"][0]["document_id"], 1)

    def test_evidence_change_before_return_fails_closed(self) -> None:
        fact = stored_fact(
            fact_id=10,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(fact,),
        )
        disclosure_store = FakeDisclosureStore(disclosure_bundle())

        def switch(point: str) -> None:
            if point == "before_return":
                disclosure_store.bundle = replace(
                    disclosure_store.bundle,
                    identity=replace(disclosure_store.bundle.identity, fund_type="mixed"),
                )

        service = FundRiskService(
            risk_store=FakeRiskStore((record,)),
            disclosure_store=disclosure_store,
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
            evidence_checkpoint=switch,
        )

        with self.assertRaisesRegex(RiskServiceError, "evidence_changed"):
            service.classify("519755")

    def test_evidence_switch_before_insert_and_after_commit_fails_closed(self) -> None:
        fact = stored_fact(
            fact_id=10,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(fact,),
        )
        for switch_point in ("before_insert", "after_commit"):
            with self.subTest(switch_point=switch_point):
                disclosure_store = FakeDisclosureStore(disclosure_bundle())

                def switch(point: str, expected: str = switch_point) -> None:
                    if point == expected:
                        disclosure_store.bundle = replace(
                            disclosure_store.bundle,
                            identity=replace(
                                disclosure_store.bundle.identity,
                                fund_type="mixed",
                            ),
                        )

                service = FundRiskService(
                    risk_store=FakeRiskStore((record,)),
                    disclosure_store=disclosure_store,
                    repository=FakeRepository(),
                    discovery=SimpleNamespace(),
                    document_client=SimpleNamespace(),
                    clock=lambda: NOW,
                    evidence_checkpoint=switch,
                )
                with self.assertRaisesRegex(RiskServiceError, "evidence_changed"):
                    service.classify("519755")

    def test_share_class_and_nav_conflict_are_bound_without_promotion(self) -> None:
        fact = stored_fact(
            fact_id=10,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(fact,),
        )
        base_bundle = disclosure_bundle()
        sibling_bundle = replace(
            base_bundle,
            share_classes=(
                FundShareClass(
                    fund_code="519755",
                    related_fund_code="519756",
                    share_class="A",
                    fund_name="公开基金A",
                    source_document_id=None,
                ),
            ),
        )

        def classify_with(bundle: DisclosureBundle):
            store = FakeRiskStore((record,))
            result = FundRiskService(
                risk_store=store,
                disclosure_store=FakeDisclosureStore(bundle),
                repository=FakeRepository(),
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
                nav_conflict_resolver=lambda _code, _nav, _facts: (
                    "nav_behavior_conflicts_with_declared_scope",
                ),
            ).classify("519755")
            return result, store.saved_evidence

        base_result, base_evidence = classify_with(base_bundle)
        sibling_result, sibling_evidence = classify_with(sibling_bundle)

        self.assertNotEqual(base_result.input_fingerprint, sibling_result.input_fingerprint)
        self.assertNotEqual(
            dict(base_evidence.external_evidence_fingerprints)["share_class"],
            dict(sibling_evidence.external_evidence_fingerprints)["share_class"],
        )
        self.assertIn("nav_behavior_conflicts_with_declared_scope", base_result.conflicts)
        self.assertNotEqual(base_result.risk_bucket.value, "high_quality_fixed_income")

    def test_external_industry_records_are_bound_but_not_promoted_to_current_facts(
        self,
    ) -> None:
        legal = stored_fact(
            fact_id=1,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(legal,),
        )
        bundle = sourced_disclosure_bundle()
        store = FakeRiskStore((record,))
        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(bundle),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        facts = {
            fact.fact_kind: fact.normalized_value
            for fact in store.saved_evidence.existing_disclosure_facts
        }
        units = {
            fact.fact_kind: fact.unit
            for fact in store.saved_evidence.existing_disclosure_facts
        }
        self.assertEqual(facts["share_class_evidence_present"], True)
        self.assertEqual(facts["fee_evidence_present"], True)
        self.assertEqual(facts["size_evidence_present"], True)
        self.assertEqual(facts["holdings_evidence_complete"], False)
        self.assertEqual(facts["current_largest_security_weight_percent"], Decimal("8"))
        self.assertTrue(
            {
                "current_largest_industry_name",
                "current_largest_industry_weight_percent",
                "current_industry_count",
            }.isdisjoint(facts)
        )
        self.assertEqual(
            units["current_largest_security_weight_percent"],
            "percent_of_net_assets",
        )
        self.assertNotIn("source_version_conflict", result.conflicts)
        self.assertNotIn("current_top_ten_holdings_weight_percent", facts)
        self.assertNotIn("current_stock_asset_allocation_percent", facts)
        self.assertEqual(len(bundle.industry_exposure), 2)
        self.assertIn(
            "industry",
            dict(store.saved_evidence.external_evidence_fingerprints),
        )
        self.assertIn(
            (99, "industry"),
            {
                (item.source_document_id, item.section)
                for item in store.saved_evidence.external_source_references
            },
        )
        report = build_authenticated_risk_research_report(
            SimpleNamespace(
                evidence=store.saved_evidence,
                classification=SimpleNamespace(classified_at=result.classified_at),
                documents=(record.artifact,),
            )
        )
        source_keys = {
            (
                source["source_namespace"],
                source["document_id"],
                source["section"],
            )
            for source in report["sources"]
        }
        self.assertIn(("d1_artifact", 1, None), source_keys)
        self.assertIn(("fund_disclosure", 99, "holdings"), source_keys)
        self.assertIn(("fund_disclosure", 99, "industry"), source_keys)

    def test_external_industry_absence_fails_active_equity_gates_as_missing(self) -> None:
        report_period = date(2026, 3, 31)
        bundle = sourced_disclosure_bundle()
        holdings = tuple(
            FundHolding(
                fund_code="519755",
                report_period=report_period,
                published_at=NOW,
                rank=rank,
                security_code=f"600{rank:03d}",
                security_name=f"公开证券{rank}",
                asset_type=AssetType.STOCK,
                weight=Decimal("5"),
                disclosure_scope="complete",
                source_document_id=99,
            )
            for rank in range(1, 11)
        )
        industries = tuple(
            FundIndustryExposure(
                fund_code="519755",
                report_period=report_period,
                published_at=NOW,
                classification_standard="申万",
                industry_name=f"公开行业{rank}",
                weight=Decimal(str(21 - rank)),
                source_document_id=99,
                industry_code=f"801{rank:03d}",
            )
            for rank in range(1, 6)
        )
        legal = stored_fact(
            fact_id=1,
            document_id=1,
            fact_kind="legal_product_family",
            value="active_equity",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(legal,),
        )
        nav = FundNavObservation(
            fund_code="519755",
            nav_date=date(2026, 7, 10),
            unit_nav=Decimal("1.1"),
            accumulated_nav=Decimal("1.2"),
            daily_growth=Decimal("0.1"),
            source="eastmoney_formal_nav",
            retrieved_at=NOW,
        )
        store = FakeRiskStore((record,))

        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(
                replace(bundle, holdings=holdings, industry_exposure=industries)
            ),
            repository=FakeRepository((nav,)),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        facts = {
            item.fact_kind: item.normalized_value
            for item in store.saved_evidence.existing_disclosure_facts
        }
        self.assertEqual(facts["current_largest_security_weight_percent"], Decimal("5"))
        self.assertEqual(facts["current_top_ten_holdings_weight_percent"], Decimal("50"))
        self.assertEqual(facts["current_stock_asset_allocation_percent"], Decimal("50"))
        self.assertEqual(facts["fee_evidence_present"], True)
        self.assertEqual(facts["size_evidence_present"], True)
        self.assertEqual(facts["share_class_evidence_present"], True)
        self.assertIsNotNone(store.saved_evidence.nav_evidence_fingerprint)
        self.assertEqual(
            result.missing_evidence,
            (
                "industry_concentration_evidence_missing",
                "industry_count_evidence_missing",
            ),
        )
        self.assertEqual(
            result.reason_codes,
            (
                "classification_partial",
                "critical_evidence_missing",
                "industry_evidence_missing",
            ),
        )
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.portfolio_role.value, "not_eligible")

    def test_complete_sourced_holdings_use_explicit_net_asset_denominators(self) -> None:
        bundle = sourced_disclosure_bundle()
        holdings = tuple(
            FundHolding(
                fund_code="519755",
                report_period=date(2026, 3, 31),
                published_at=NOW,
                rank=rank,
                security_code=f"600{rank:03d}",
                security_name=f"公开证券{rank}",
                asset_type=AssetType.STOCK,
                weight=Decimal(str(11 - rank)),
                disclosure_scope="complete",
                source_document_id=99,
            )
            for rank in range(1, 11)
        )
        legal = stored_fact(
            fact_id=1,
            document_id=1,
            fact_kind="legal_product_type",
            value="fof",
        )
        record = stored_document(
            DocumentKind.PROSPECTUS_UPDATE,
            artifact_id=1,
            title="2026年更新招募说明书",
            published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            facts=(legal,),
        )
        store = FakeRiskStore((record,))

        result = FundRiskService(
            risk_store=store,
            disclosure_store=FakeDisclosureStore(replace(bundle, holdings=holdings)),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")
        units = {
            fact.fact_kind: fact.unit
            for fact in store.saved_evidence.existing_disclosure_facts
        }

        for fact_kind in (
            "current_largest_security_weight_percent",
            "current_top_ten_holdings_weight_percent",
            "current_stock_asset_allocation_percent",
        ):
            with self.subTest(fact_kind=fact_kind):
                self.assertEqual(units[fact_kind], "percent_of_net_assets")
        self.assertNotIn("source_version_conflict", result.conflicts)

    def test_legacy_generic_percent_current_fact_fails_closed_on_recompute(self) -> None:
        legal = stored_fact(
            fact_id=1,
            document_id=1,
            fact_kind="legal_product_family",
            value="ordinary_bond",
        )
        legacy_current = stored_fact(
            fact_id=2,
            document_id=2,
            fact_kind="current_stock_asset_allocation_percent",
            value=Decimal("0"),
            unit="percent",
        )
        records = (
            stored_document(
                DocumentKind.PROSPECTUS_UPDATE,
                artifact_id=1,
                title="2026年更新招募说明书",
                published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                facts=(legal,),
            ),
            stored_document(
                DocumentKind.SEMIANNUAL_REPORT,
                artifact_id=2,
                title="2026年半年度报告",
                published_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
                facts=(legacy_current,),
            ),
        )

        result = FundRiskService(
            risk_store=FakeRiskStore(records),
            disclosure_store=FakeDisclosureStore(disclosure_bundle()),
            repository=FakeRepository(),
            discovery=SimpleNamespace(),
            document_client=SimpleNamespace(),
            clock=lambda: NOW,
        ).classify("519755")

        self.assertIn("source_version_conflict", result.conflicts)
        self.assertNotEqual(result.evidence_status.value, "verified")

    def test_real_store_parser_and_authenticated_readback_integration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "kunjin.sqlite3"
            document_path = root / "prospectus.html"
            payload = "<html><body><p>基金类型：基金中基金</p></body></html>".encode()
            document_path.write_bytes(payload)
            repository = Repository(database)
            repository.migrate()
            risk_store = FundRiskStore(repository)
            artifact = RetrievedArtifact(
                candidate=OfficialDocumentCandidate(
                    fund_code="519755",
                    document_kind=DocumentKind.PROSPECTUS_UPDATE,
                    title="2026年更新招募说明书",
                    url="https://www.fund001.com/prospectus.html",
                    publisher="交银施罗德基金管理有限公司",
                    published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    source_tier=1,
                ),
                final_url="https://www.fund001.com/prospectus.html",
                retrieved_at=NOW,
                content_type="text/html; charset=utf-8",
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                managed_path=document_path,
            )
            publish_current_document(risk_store, parse_artifact(artifact))
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FundDisclosureStore(repository),
                repository=repository,
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            )

            result = service.classify("519755")
            authenticated = service.current_classification("519755")

        self.assertEqual(result, authenticated)
        self.assertIn("classification_unclassified", result.reason_codes)

    def test_real_store_round_trip_preserves_identity_scope_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document_path = root / "prospectus.html"
            payload = b"<html><body>public prospectus</body></html>"
            document_path.write_bytes(payload)
            repository = Repository(root / "kunjin.sqlite3")
            repository.migrate()
            risk_store = FundRiskStore(repository)
            artifact = RetrievedArtifact(
                candidate=OfficialDocumentCandidate(
                    fund_code="519755",
                    document_kind=DocumentKind.PROSPECTUS_UPDATE,
                    title="2026年更新招募说明书",
                    url="https://www.fund001.com/prospectus.html",
                    publisher="交银施罗德基金管理有限公司",
                    published_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    source_tier=1,
                ),
                final_url="https://www.fund001.com/prospectus.html",
                retrieved_at=NOW,
                content_type="text/html; charset=utf-8",
                byte_size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                managed_path=document_path,
            )
            fields = {
                "fact_kind": "legal_product_family",
                "normalized_value": "sector_theme",
                "unit": None,
                "page_number": 1,
                "section_name": "投资范围",
                "source_excerpt": "本基金投资于行业主题证券。",
                "effective_from": None,
                "effective_to": None,
                "confidence_state": FactConfidence.EXACT,
            }
            parsed = ParsedRiskDocument(
                artifact=artifact,
                facts=(
                    ParsedMandateFact(
                        **fields,
                        fact_fingerprint=fact_fingerprint(**fields),
                    ),
                ),
                warnings=(),
                conflicts=(),
            )
            publish_current_document(risk_store, parsed)
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=FakeDisclosureStore(
                    sourced_identity_bundle(
                        fund_name="公开货币基金",
                        fund_type="债券型",
                    )
                ),
                repository=repository,
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            )

            result = service.classify("519755")
            authenticated = service.current_classification("519755")
            record = service.classification_evidence("519755")

        self.assertEqual(result, authenticated)
        self.assertEqual(result.evidence_status.value, "verified")
        self.assertEqual(
            result.conflicts,
            (
                "name_conflicts_with_formal_scope",
                "platform_category_conflicts_with_formal_scope",
            ),
        )
        self.assertIsNotNone(record)
        self.assertEqual(
            tuple(json.loads(record.classification.conflicts_json)),
            result.conflicts,
        )
        manifest = json.loads(record.classification.input_manifest_json)
        identity_facts = {
            item["fact_kind"]: item
            for item in manifest["existing_disclosure_facts"]
            if item["fact_kind"] in {"fund_name", "platform_category"}
        }
        self.assertEqual(set(identity_facts), {"fund_name", "platform_category"})
        self.assertEqual(
            {item["source_document_id"] for item in identity_facts.values()},
            {101},
        )
        self.assertTrue(
            any(
                item["source_document_id"] == 101 and item["section"] == "identity"
                for item in manifest["external_source_references"]
            )
        )

    def test_historical_research_retains_bound_external_source_after_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document_path = root / "prospectus.html"
            document_path.write_text("<html>public prospectus</html>", encoding="utf-8")
            repository = Repository(root / "kunjin.sqlite3")
            repository.migrate()
            risk_store = FundRiskStore(repository)
            item = candidate(DocumentKind.PROSPECTUS_UPDATE, "historical-source")
            fields = {
                "fact_kind": "legal_product_family",
                "normalized_value": "sector_theme",
                "unit": None,
                "page_number": 1,
                "section_name": "投资范围",
                "source_excerpt": "本基金投资于行业主题证券。",
                "effective_from": None,
                "effective_to": None,
                "confidence_state": FactConfidence.EXACT,
            }
            publish_current_document(
                risk_store,
                parsed_document_for(
                    document_path,
                    item,
                    facts=(
                        ParsedMandateFact(
                            **fields,
                            fact_fingerprint=fact_fingerprint(**fields),
                        ),
                    ),
                ),
            )
            disclosure_store = FakeDisclosureStore(sourced_disclosure_bundle())
            service = FundRiskService(
                risk_store=risk_store,
                disclosure_store=disclosure_store,
                repository=repository,
                discovery=SimpleNamespace(),
                document_client=SimpleNamespace(),
                clock=lambda: NOW,
            )
            service.classify("519755")
            classification_id = service.classification_history("519755")[0].id
            disclosure_store.bundle = disclosure_bundle(fund_type="mixed")

            historical = service.classification_evidence(
                "519755",
                classification_id=classification_id,
            )
            report = build_authenticated_risk_research_report(historical)

        external_sources = [
            source
            for source in report["sources"]
            if source["source_namespace"] == "fund_disclosure"
        ]
        self.assertTrue(external_sources)
        self.assertEqual(
            {source["url"] for source in external_sources},
            {"https://www.fund001.com/disclosure.html"},
        )
        self.assertEqual(
            {source["published_at"] for source in external_sources},
            {NOW.isoformat()},
        )
        self.assertGreater(len({source["section"] for source in external_sources}), 1)


if __name__ == "__main__":
    unittest.main()
