from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional, Tuple

from kunjin.funds.models import FUND_CODE_PATTERN, DocumentKind
from kunjin.funds.risk.audit import (
    ACTIVE_LEGACY_PARSER_VERSION,
    HISTORICAL_LEGACY_PARSER_VERSIONS,
    HISTORICAL_NATIVE_PARSER_VERSIONS,
    ParserProvenance,
    RefreshOutcome,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import (
    OfficialDocumentError,
    RetrievedArtifact,
    validate_safe_https_url,
)
from kunjin.funds.risk.engine import (
    ClassificationEvidence,
    classification_input_manifest,
    classify_fund,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
    unspecified_document_failure,
)
from kunjin.funds.risk.legacy_doc import (
    ConverterStatus,
    LegacyConversionResult,
    LegacyDocConversionError,
    LegacyDocConverter,
)
from kunjin.funds.risk.models import (
    EvidenceFreshness,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    MandateFact,
)
from kunjin.funds.risk.parsers import (
    ParsedArtifactResult,
    ParsedRiskDocument,
    RiskDocumentParseError,
    artifact_uses_legacy_ole_container,
    fact_fingerprint,
    parse_artifact_with_provenance,
)
from kunjin.funds.risk.policy import (
    CLASSIFICATION_CONFLICT_CODES,
    ClassificationPolicyV1,
)
from kunjin.funds.risk.selection import (
    PERIODIC_DOCUMENT_KINDS,
    SELECTION_STATES,
    DocumentSelectionPlan,
    current_evidence_projection,
    select_current_candidates,
)
from kunjin.funds.risk.selection import (
    select_current_documents as select_current_document_records,
)
from kunjin.funds.risk.store import RiskStoreError, StoredParserProvenance
from kunjin.funds.service import expected_report_period

_PUBLIC_TECHNICAL_CODES = frozenset(
    {
        "official_document_unavailable",
        "official_document_invalid",
        "official_document_resource_limit",
        "official_document_parse_failed",
        "classification_policy_unavailable",
        "classification_calculation_failed",
        "classification_storage_failed",
    }
)

_LEGAL_KINDS = frozenset(
    {
        DocumentKind.FUND_CONTRACT,
        DocumentKind.PROSPECTUS,
        DocumentKind.PROSPECTUS_UPDATE,
        DocumentKind.PRODUCT_SUMMARY,
        DocumentKind.CLASSIFICATION_ANNOUNCEMENT,
    }
)
_REPORT_KINDS = frozenset(
    {
        DocumentKind.QUARTERLY_REPORT,
        DocumentKind.SEMIANNUAL_REPORT,
        DocumentKind.ANNUAL_REPORT,
    }
)
_SYNC_DOCUMENT_KINDS = _LEGAL_KINDS | _REPORT_KINDS | {DocumentKind.INDEX_METHODOLOGY}
_SYNC_WARNING_CODES = frozenset({"source_excerpt_truncated"})
_SYNC_CONFLICT_CODES = frozenset({"duplicate_conflicting_clause"})
_SYNC_FAILURE_COMBINATIONS = frozenset(
    {
        ("official_document_unavailable", "discovery", "dns_unavailable"),
        ("official_document_unavailable", "discovery", "network_unavailable"),
        ("official_document_unavailable", "discovery", "http_unavailable"),
        ("official_document_unavailable", "landing_validation", "dns_unavailable"),
        ("official_document_unavailable", "retrieval", "dns_unavailable"),
        ("official_document_unavailable", "retrieval", "network_unavailable"),
        ("official_document_unavailable", "retrieval", "http_unavailable"),
        ("official_document_invalid", "discovery", "source_unregistered"),
        ("official_document_invalid", "discovery", "redirect_rejected"),
        ("official_document_invalid", "discovery", "discovery_format_invalid"),
        ("official_document_invalid", "discovery", "identity_mismatch"),
        ("official_document_invalid", "discovery", "publication_date_missing"),
        ("official_document_invalid", "landing_validation", "source_unregistered"),
        ("official_document_invalid", "landing_validation", "redirect_rejected"),
        ("official_document_invalid", "landing_validation", "landing_format_invalid"),
        ("official_document_invalid", "landing_validation", "landing_title_mismatch"),
        ("official_document_invalid", "landing_validation", "landing_date_mismatch"),
        ("official_document_invalid", "landing_validation", "attachment_missing"),
        ("official_document_invalid", "landing_validation", "attachment_ambiguous"),
        ("official_document_invalid", "landing_validation", "attachment_host_rejected"),
        ("official_document_invalid", "landing_validation", "authentication_shell"),
        ("official_document_invalid", "landing_validation", "empty_or_script_only_html"),
        ("official_document_invalid", "retrieval", "source_unregistered"),
        ("official_document_invalid", "retrieval", "redirect_rejected"),
        ("official_document_invalid", "retrieval", "clock_invalid"),
        ("official_document_invalid", "identity_validation", "source_unregistered"),
        ("official_document_invalid", "identity_validation", "identity_mismatch"),
        ("official_document_invalid", "container_validation", "declared_mime_unsupported"),
        ("official_document_invalid", "container_validation", "detected_container_unknown"),
        ("official_document_invalid", "container_validation", "declared_detected_mismatch"),
        ("official_document_invalid", "container_validation", "legacy_ole_container_unsupported"),
        ("official_document_invalid", "persistence", "managed_artifact_invalid"),
        ("official_document_invalid", "unspecified", "unspecified_failure"),
        ("official_document_resource_limit", "discovery", "resource_limit"),
        ("official_document_resource_limit", "landing_validation", "resource_limit"),
        ("official_document_resource_limit", "retrieval", "resource_limit"),
        ("official_document_resource_limit", "container_validation", "resource_limit"),
        ("official_document_resource_limit", "conversion", "legacy_converter_timeout"),
        (
            "official_document_resource_limit",
            "conversion",
            "legacy_converter_resource_limit",
        ),
        ("official_document_resource_limit", "parser", "resource_limit"),
        (
            "official_document_parse_failed",
            "conversion",
            "legacy_converter_unavailable",
        ),
        ("official_document_parse_failed", "conversion", "legacy_converter_failed"),
        (
            "official_document_parse_failed",
            "conversion",
            "legacy_converter_output_invalid",
        ),
        ("official_document_parse_failed", "parser", "parser_format_invalid"),
        ("official_document_parse_failed", "parser", "parser_identity_mismatch"),
        (
            "official_document_parse_failed",
            "parser",
            "parser_effective_date_invalid",
        ),
        ("official_document_parse_failed", "parser", "parser_ambiguous_fact"),
        ("classification_storage_failed", "persistence", "storage_failure"),
    }
)
_UNSAFE_PUBLIC_TEXT = re.compile(
    r"(?:<|>|docker\s+run|docker\.app|stderr|stdout|traceback|file://|"
    r"(?:^|[^a-z0-9])/(?:applications|users|private|tmp|var)(?:/|$))",
    re.IGNORECASE,
)
_ABSOLUTE_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PUBLIC_TEXT_DECODE_PASSES = 2
_LOWERCASE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RiskServiceError(RuntimeError):
    """A stable, redacted D1 technical failure."""

    def __init__(self, code: str, *, reason: Optional[str] = None) -> None:
        if code not in _PUBLIC_TECHNICAL_CODES:
            raise ValueError("risk service error code is not public")
        if reason is not None and (
            type(reason) is not str or not reason or not reason.replace("_", "").isalnum()
        ):
            raise ValueError("risk service failure reason must be a stable code")
        self.code = code
        self.reason = reason
        super().__init__(code if reason is None else f"{code}: {reason}")


class _BoundParserFailure(RiskDocumentParseError):
    def __init__(
        self,
        source: RiskDocumentParseError,
        provenance: Optional[ParserProvenance],
    ) -> None:
        super().__init__(
            source.code,
            source.failure.reason_code,
            "official fund document parsing failed",
        )
        self.provenance = provenance


class _TrackingLegacyConverter:
    def __init__(self, converter: LegacyDocConverter) -> None:
        self._converter = converter
        self.attempted = False
        self.provenance: Optional[ParserProvenance] = None
        self.parser_input_sha256: Optional[str] = None

    def convert(self, artifact: RetrievedArtifact):
        self.attempted = True
        result = self._converter.convert(artifact)
        if type(result) is LegacyConversionResult:
            try:
                result.validate()
            except ValueError:
                return result
            self.provenance = result.provenance
            self.parser_input_sha256 = result.parser_input_sha256
        return result

    def matches(self, parsed: ParsedArtifactResult) -> bool:
        return (
            self.provenance is not None
            and self.parser_input_sha256 is not None
            and parsed.provenance == self.provenance
            and parsed.parser_input_sha256 == self.parser_input_sha256
        )


@dataclass(frozen=True)
class DocumentSelectionItem:
    document_kind: str
    status: str
    selected_url: Optional[str]
    candidate_count: int
    reason_code: Optional[str]

    def validate(self) -> None:
        _require_exact_record(self, DocumentSelectionItem, "document selection item")
        try:
            document_kind = DocumentKind(self.document_kind)
        except (TypeError, ValueError):
            raise ValueError("document selection kind is invalid") from None
        if document_kind not in PERIODIC_DOCUMENT_KINDS:
            raise ValueError("document selection kind must be periodic")
        if type(self.status) is not str or self.status not in SELECTION_STATES:
            raise ValueError("document selection status is invalid")
        if type(self.candidate_count) is not int or self.candidate_count < 0:
            raise ValueError("document selection candidate count is invalid")
        if self.status == "selected":
            if self.selected_url is None:
                raise ValueError("selected document selection requires a URL")
            validate_public_risk_url(self.selected_url)
            if self.candidate_count < 1 or self.reason_code is not None:
                raise ValueError("selected document selection state is inconsistent")
            return
        if self.selected_url is not None:
            raise ValueError("unselected document selection cannot expose a URL")
        if self.status == "missing":
            if (
                self.candidate_count != 0
                or self.reason_code != "current_periodic_candidate_missing"
            ):
                raise ValueError("missing document selection state is inconsistent")
            return
        if (
            self.candidate_count < 2
            or self.reason_code != "current_periodic_candidate_conflict"
        ):
            raise ValueError("conflicted document selection state is inconsistent")


@dataclass(frozen=True)
class DocumentSyncItem:
    document_kind: str
    title: str
    url: str
    published_at: Optional[datetime]
    status: str
    artifact_id: Optional[int]
    fact_count: int
    warnings: Tuple[str, ...]
    conflicts: Tuple[str, ...]
    error_code: Optional[str]
    failure_stage: Optional[str] = None
    failure_reason: Optional[str] = None

    def validate(self) -> None:
        _require_exact_record(self, DocumentSyncItem, "document sync item")
        if type(self.document_kind) is not str:
            raise ValueError("document sync kind must be exact text")
        try:
            document_kind = DocumentKind(self.document_kind)
        except ValueError:
            raise ValueError("document sync kind is invalid") from None
        if document_kind not in _SYNC_DOCUMENT_KINDS:
            raise ValueError("document sync kind is not supported")
        _validate_sync_title(self.title)
        validate_public_risk_url(self.url)
        if self.published_at is not None:
            _validate_canonical_utc(self.published_at, "document sync published_at")
        if type(self.status) is not str or self.status not in {"success", "failed"}:
            raise ValueError("document sync item status is invalid")
        if type(self.fact_count) is not int or self.fact_count < 0:
            raise ValueError("document sync fact count must be nonnegative")
        _validate_sync_codes(self.warnings, _SYNC_WARNING_CODES, "document sync warnings")
        _validate_sync_codes(self.conflicts, _SYNC_CONFLICT_CODES, "document sync conflicts")
        if self.status == "success":
            if type(self.artifact_id) is not int or self.artifact_id <= 0:
                raise ValueError("successful document sync requires an artifact")
            if any(
                value is not None
                for value in (self.error_code, self.failure_stage, self.failure_reason)
            ):
                raise ValueError("successful document sync cannot contain a failure")
            return
        if (
            self.artifact_id is not None
            or self.fact_count != 0
            or self.warnings
            or self.conflicts
            or (
                self.error_code,
                self.failure_stage,
                self.failure_reason,
            )
            not in _SYNC_FAILURE_COMBINATIONS
        ):
            raise ValueError("failed document sync has an invalid safe failure")


@dataclass(frozen=True)
class DocumentSyncResult:
    fund_code: str
    status: str
    documents: Tuple[DocumentSyncItem, ...]
    selections: Tuple[DocumentSelectionItem, ...]
    selection_checksum: str
    attempted_at: datetime
    capability: str = "research_only"

    def validate(self) -> None:
        _require_exact_record(self, DocumentSyncResult, "document sync result")
        _validate_fund_code(self.fund_code)
        if type(self.status) is not str or self.status not in {
            "success",
            "partial",
            "failed",
            "empty",
        }:
            raise ValueError("document sync result status is invalid")
        if type(self.documents) is not tuple:
            raise ValueError("document sync documents must be an immutable tuple")
        for item in self.documents:
            if type(item) is not DocumentSyncItem:
                raise ValueError("document sync documents must use exact items")
            item.validate()
        if type(self.selections) is not tuple:
            raise ValueError("document sync selections must be an immutable tuple")
        for item in self.selections:
            if type(item) is not DocumentSelectionItem:
                raise ValueError("document sync selections must use exact items")
            item.validate()
        expected_kinds = tuple(sorted(kind.value for kind in PERIODIC_DOCUMENT_KINDS))
        if tuple(item.document_kind for item in self.selections) != expected_kinds:
            raise ValueError("document sync selections must cover every periodic kind")
        selection_by_kind = {item.document_kind: item for item in self.selections}
        periodic_documents = tuple(
            item
            for item in self.documents
            if DocumentKind(item.document_kind) in PERIODIC_DOCUMENT_KINDS
        )
        periodic_document_urls = {
            (item.document_kind, item.url) for item in periodic_documents
        }
        if len(periodic_documents) != len(periodic_document_urls):
            raise ValueError("document sync contains duplicate periodic documents")
        selected_urls = {
            (item.document_kind, item.selected_url)
            for item in self.selections
            if item.status == "selected"
        }
        if periodic_document_urls != selected_urls:
            raise ValueError("document sync periodic documents do not match selections")
        if any(
            selection_by_kind[item.document_kind].status != "selected"
            for item in periodic_documents
        ):
            raise ValueError("document sync contains an unselected periodic document")
        if (
            type(self.selection_checksum) is not str
            or _LOWERCASE_SHA256.fullmatch(self.selection_checksum) is None
        ):
            raise ValueError("document sync selection checksum is invalid")
        _validate_canonical_utc(self.attempted_at, "document sync attempted_at")
        if type(self.capability) is not str or self.capability != "research_only":
            raise ValueError("document sync capability is invalid")
        statuses = {item.status for item in self.documents}
        expected = (
            "empty"
            if not self.documents
            else "success"
            if statuses == {"success"}
            else "failed"
            if statuses == {"failed"}
            else "partial"
        )
        if self.status != expected:
            raise ValueError("document sync result status does not match its items")


class FundRiskService:
    """Fact-only D1 synchronization and classification orchestration."""

    def __init__(
        self,
        *,
        risk_store: object,
        disclosure_store: object,
        repository: object,
        discovery: object,
        document_client: object,
        parser: Optional[Callable[[object], object]] = None,
        legacy_converter: Optional[LegacyDocConverter] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        policy: ClassificationPolicyV1 = ClassificationPolicyV1(),
        nav_conflict_resolver: Callable[
            [str, tuple, tuple], tuple
        ] = lambda _code, _nav, _facts: (),
        evidence_checkpoint: Callable[[str], None] = lambda _point: None,
        failure_observer: Callable[[SafeDocumentFailure], None] = lambda _failure: None,
    ) -> None:
        self._risk_store = risk_store
        self._disclosure_store = disclosure_store
        self._repository = repository
        self._discovery = discovery
        self._document_client = document_client
        self._parser = parser
        self._legacy_converter = legacy_converter
        self._clock = clock
        self._policy = policy
        self._nav_conflict_resolver = nav_conflict_resolver
        self._evidence_checkpoint = evidence_checkpoint
        self._failure_observer = failure_observer

    def converter_status(self) -> ConverterStatus:
        unavailable = ConverterStatus(
            capability="research_only",
            status="unavailable",
            reason_code=DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE.value,
            parser_version=None,
            provenance_checksum=None,
        )
        unavailable.validate()
        converter = self._legacy_converter
        if converter is None:
            return unavailable
        try:
            status = converter.status()
            if type(status) is not ConverterStatus:
                return unavailable
            status.validate()
            return status
        except Exception:
            return unavailable

    def sync_documents(self, fund_code: str) -> DocumentSyncResult:
        _validate_fund_code(fund_code)
        attempted_at = _canonical_now(self._clock())
        try:
            refresh_id = self._risk_store.begin_document_refresh(fund_code, attempted_at)
        except Exception as exc:
            self._raise_storage_failure(exc)

        try:
            bundle = self._disclosure_store.load_bundle(fund_code)
            identity = getattr(bundle, "identity", None)
            manager_name = None if identity is None else getattr(identity, "manager_name", None)
            if type(manager_name) is not str or not manager_name.strip():
                manager_name = None
            candidates = self._discovery.discover(
                fund_code,
                manager_name=manager_name,
                announcements=getattr(bundle, "announcements", ()),
            )
            selection = select_current_candidates(
                fund_code,
                refresh_run_id=refresh_id,
                candidates=candidates,
            )
        except Exception as exc:
            failure = _safe_failure(exc)
            error = _service_error(exc)
            try:
                self._risk_store.complete_document_refresh(
                    refresh_id,
                    RefreshOutcome.FAILED,
                    _canonical_now(self._clock()),
                    failure=failure,
                )
            except Exception as storage_exc:
                self._raise_storage_failure(storage_exc)
            self._observe_failure(failure)
            if error is exc:
                raise
            raise error from exc

        try:
            self._risk_store.publish_document_selection(selection, attempted_at)
        except Exception as exc:
            self._raise_storage_failure(exc)

        items = tuple(
            self._sync_candidate(refresh_id, candidate)
            for candidate in selection.attempted_candidates
        )
        statuses = {item.status for item in items}
        if not items:
            status = "empty"
        elif statuses == {"success"}:
            status = "success"
        elif statuses == {"failed"}:
            status = "failed"
        else:
            status = "partial"
        outcome = RefreshOutcome(status)
        completion_failure = None
        if outcome is RefreshOutcome.FAILED:
            completion_failure = _item_failure(items[0])
        try:
            self._risk_store.complete_document_refresh(
                refresh_id,
                outcome,
                _canonical_now(self._clock()),
                failure=completion_failure,
            )
        except RiskServiceError:
            raise
        except Exception as exc:
            self._raise_storage_failure(exc)
        result = DocumentSyncResult(
            fund_code=fund_code,
            status=status,
            documents=items,
            selections=_public_selection_items(selection),
            selection_checksum=selection.selection_checksum,
            attempted_at=attempted_at,
        )
        result.validate()
        return result

    def _sync_candidate(self, refresh_id: int, candidate: object) -> DocumentSyncItem:
        artifact = None
        attempted_at = _canonical_now(self._clock())
        provenance = None
        try:
            artifact = self._document_client.fetch(candidate)
            try:
                parsed_result = self._parse_artifact(artifact, candidate)
                parsed = parsed_result.document
                provenance = parsed_result.provenance
            except LegacyDocConversionError:
                provenance = self._current_legacy_provenance()
                raise
            except RiskDocumentParseError as exc:
                provenance = (
                    exc.provenance
                    if type(exc) is _BoundParserFailure
                    else native_parser_provenance()
                )
                raise
            try:
                stored = self._risk_store.publish_candidate_success(
                    refresh_id=refresh_id,
                    candidate=candidate,
                    parsed=parsed,
                    provenance=provenance,
                    parser_input_sha256=parsed_result.parser_input_sha256,
                    attempted_at=attempted_at,
                )
            except Exception as exc:
                self._raise_storage_failure(exc)
            return DocumentSyncItem(
                document_kind=candidate.document_kind.value,
                title=candidate.title,
                url=candidate.url,
                published_at=_canonical_optional_utc(candidate.published_at),
                status="success",
                artifact_id=stored.artifact.id,
                fact_count=len(stored.facts),
                warnings=parsed.warnings,
                conflicts=parsed.conflicts,
                error_code=None,
            )
        except RiskServiceError:
            raise
        except Exception as exc:
            failure = _safe_failure(exc)
            error = _service_error(exc)
            try:
                if artifact is None or provenance is None:
                    self._risk_store.publish_candidate_failure(
                        refresh_id=refresh_id,
                        candidate=candidate,
                        failure=failure,
                        attempted_at=attempted_at,
                        artifact=None,
                        provenance=None,
                    )
                else:
                    self._risk_store.publish_candidate_parse_failure(
                        refresh_id=refresh_id,
                        candidate=candidate,
                        artifact=artifact,
                        provenance=provenance,
                        failure=failure,
                        attempted_at=attempted_at,
                    )
            except Exception as storage_exc:
                self._raise_storage_failure(storage_exc)
            self._observe_failure(failure)
            return DocumentSyncItem(
                document_kind=candidate.document_kind.value,
                title=candidate.title,
                url=candidate.url,
                published_at=_canonical_optional_utc(candidate.published_at),
                status="failed",
                artifact_id=None,
                fact_count=0,
                warnings=(),
                conflicts=(),
                error_code=error.code,
                failure_stage=failure.stage.value,
                failure_reason=failure.reason_code.value,
            )

    def _parse_artifact(
        self,
        artifact: RetrievedArtifact,
        candidate: object,
    ) -> ParsedArtifactResult:
        tracked_converter = (
            None
            if self._legacy_converter is None
            else _TrackingLegacyConverter(self._legacy_converter)
        )
        try:
            uses_legacy_ole = artifact_uses_legacy_ole_container(artifact)
            if self._parser is None or uses_legacy_ole:
                parsed = parse_artifact_with_provenance(
                    artifact,
                    legacy_converter=tracked_converter,
                )
            else:
                custom = self._parser(artifact)
                if type(custom) is ParsedRiskDocument:
                    parsed = ParsedArtifactResult(
                        document=custom,
                        parser_input_sha256=artifact.sha256,
                        provenance=native_parser_provenance(),
                    )
                elif type(custom) is ParsedArtifactResult:
                    parsed = custom
                    parsed.validate()
                    if (
                        parsed.provenance.converter_kind != "none"
                        or parsed.parser_input_sha256 != artifact.sha256
                    ):
                        raise ValueError("custom parser provenance must be native")
                else:
                    raise ValueError("custom parser result is invalid")
            if (
                tracked_converter is not None
                and tracked_converter.attempted
                and not tracked_converter.matches(parsed)
            ):
                raise RiskDocumentParseError(
                    "official_document_parse_failed",
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                    "official fund document parsing failed",
                )
            _validate_parser_result(parsed, artifact, candidate)
            return parsed
        except LegacyDocConversionError:
            raise
        except RiskDocumentParseError as exc:
            if tracked_converter is not None and tracked_converter.attempted:
                raise _BoundParserFailure(exc, tracked_converter.provenance) from None
            raise
        except Exception as exc:
            raise RiskDocumentParseError(
                "official_document_parse_failed",
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official fund document parsing failed",
            ) from exc

    def _current_legacy_provenance(self) -> Optional[ParserProvenance]:
        converter = self._legacy_converter
        if converter is None:
            return None
        active_provenance = getattr(converter, "active_provenance", None)
        if not callable(active_provenance):
            return None
        try:
            provenance = active_provenance()
            if type(provenance) is not ParserProvenance:
                return None
            provenance.validate()
            if provenance.converter_kind != "docker_libreoffice":
                return None
            return provenance
        except Exception:
            return None

    def _raise_storage_failure(self, error: Exception) -> None:
        failure = _storage_failure()
        self._observe_failure(failure)
        raise RiskServiceError(failure.public_code) from error

    def _observe_failure(self, failure: SafeDocumentFailure) -> None:
        try:
            self._failure_observer(failure)
        except BaseException:
            pass

    def classify(self, fund_code: str):
        _validate_fund_code(fund_code)
        classified_at = _canonical_now(self._clock())
        try:
            self._policy.validate()
            self._risk_store.ensure_policy(self._policy)
        except Exception as exc:
            raise RiskServiceError("classification_policy_unavailable") from exc

        try:
            initial = self._assemble_evidence(fund_code, classified_at)
            initial_token = _evidence_token(initial, self._policy)
            self._evidence_checkpoint("before_insert")
            current = self._assemble_evidence(fund_code, classified_at)
            if _evidence_token(current, self._policy) != initial_token:
                raise RiskServiceError(
                    "classification_calculation_failed",
                    reason="evidence_changed",
                )
            result = classify_fund(current, self._policy, classified_at)
        except RiskServiceError:
            raise
        except Exception as exc:
            raise RiskServiceError("classification_calculation_failed") from exc

        try:
            stored = self._risk_store.save_classification(result, current, self._policy)
            self._evidence_checkpoint("after_commit")
            self._require_unchanged(fund_code, classified_at, initial_token)
            record = self._risk_store.classification_evidence(
                fund_code,
                classification_id=stored.id,
            )
            if record is None:
                raise RiskServiceError("classification_storage_failed")
            authenticated = classify_fund(
                record.evidence,
                self._policy,
                record.classification.classified_at,
            )
            if authenticated != result:
                raise RiskServiceError("classification_storage_failed")
            self._evidence_checkpoint("before_return")
            self._require_unchanged(fund_code, classified_at, initial_token)
            record = self._risk_store.classification_evidence(
                fund_code,
                classification_id=stored.id,
            )
            if (
                record is None
                or record.classification.input_fingerprint != result.input_fingerprint
            ):
                raise RiskServiceError("classification_storage_failed")
            return result
        except RiskServiceError:
            raise
        except Exception as exc:
            raise RiskServiceError("classification_storage_failed") from exc

    def current_classification(self, fund_code: str):
        _validate_fund_code(fund_code)
        record = self._authenticated_current_record(fund_code)
        if record is None:
            return None
        return classify_fund(
            record.evidence,
            self._policy,
            record.classification.classified_at,
        )

    def classification_history(self, fund_code: str):
        _validate_fund_code(fund_code)
        return self._risk_store.classification_history(fund_code)

    def classification_evidence(self, fund_code: str, classification_id: Optional[int] = None):
        _validate_fund_code(fund_code)
        if classification_id is not None:
            return self._risk_store.classification_evidence(
                fund_code,
                classification_id=classification_id,
            )
        return self._authenticated_current_record(fund_code)

    def _authenticated_current_record(self, fund_code: str):
        try:
            record = self._risk_store.classification_evidence(fund_code)
        except Exception as exc:
            raise RiskServiceError("classification_storage_failed") from exc
        if record is None:
            return None

        checked_at = _canonical_now(self._clock())
        try:
            stored_token = _evidence_token(record.evidence, self._policy)
            current = self._assemble_evidence(fund_code, checked_at)
            if _evidence_token(current, self._policy) != stored_token:
                raise RiskServiceError(
                    "classification_calculation_failed",
                    reason="evidence_changed",
                )
            self._evidence_checkpoint("current_before_return")
            self._require_unchanged(fund_code, checked_at, stored_token)
            try:
                latest = self._risk_store.classification_evidence(fund_code)
            except Exception as exc:
                raise RiskServiceError("classification_storage_failed") from exc
            if latest is None or _evidence_token(latest.evidence, self._policy) != stored_token:
                raise RiskServiceError(
                    "classification_calculation_failed",
                    reason="evidence_changed",
                )
            return latest
        except RiskServiceError:
            raise
        except Exception as exc:
            raise RiskServiceError("classification_calculation_failed") from exc

    def _require_unchanged(
        self,
        fund_code: str,
        classified_at: datetime,
        expected_token: str,
    ) -> None:
        current = self._assemble_evidence(fund_code, classified_at)
        if _evidence_token(current, self._policy) != expected_token:
            raise RiskServiceError(
                "classification_calculation_failed",
                reason="evidence_changed",
            )

    def _assemble_evidence(
        self,
        fund_code: str,
        classified_at: datetime,
    ) -> ClassificationEvidence:
        try:
            snapshot = self._risk_store.current_document_selection_snapshot(fund_code)
        except RiskStoreError as exc:
            raise RiskServiceError("classification_storage_failed") from exc
        if snapshot is None:
            raise RiskServiceError("official_document_unavailable")
        successful_records = (
            snapshot.selected_periodic_records + snapshot.nonperiodic_successful_records
        )
        if not successful_records:
            raise RiskServiceError("official_document_unavailable")
        self._require_active_provenance(successful_records)

        nonperiodic_records, _ = current_evidence_projection(
            snapshot.nonperiodic_successful_records
        )
        records, report_facts = current_evidence_projection(
            snapshot.selected_periodic_records + nonperiodic_records
        )
        if not records:
            raise RiskServiceError("official_document_unavailable")
        bundle = self._disclosure_store.load_bundle(fund_code)
        validate_bundle = getattr(bundle, "validate", None)
        if callable(validate_bundle):
            validate_bundle()
        nav_history = tuple(self._repository.fund_history(fund_code))
        for observation in nav_history:
            observation.validate()
            if observation.fund_code != fund_code:
                raise ValueError("NAV observation belongs to another fund")

        legal_facts = []
        benchmark_facts = []
        existing_facts = list(_external_facts(bundle, classified_at))
        fact_ids = []
        for record in records:
            target = existing_facts
            if record.artifact.document_kind in _LEGAL_KINDS:
                target = legal_facts
            elif record.artifact.document_kind is DocumentKind.INDEX_METHODOLOGY:
                target = benchmark_facts
            elif record.artifact.document_kind in _REPORT_KINDS:
                continue
            for stored_fact in record.facts:
                target.append(_mandate_fact(stored_fact))
                fact_ids.append(stored_fact.id)

        for record, stored_fact in report_facts:
            fact_ids.append(stored_fact.id)
        report_mandate_facts = tuple(
            sorted((_mandate_fact(stored_fact) for _, stored_fact in report_facts), key=_fact_order)
        )

        external = _external_fingerprints(bundle)
        external_sources = _external_source_references(bundle)
        if nav_history:
            nav_fingerprint = _fingerprint(nav_history)
            nav_start = min(item.nav_date for item in nav_history)
            nav_end = max(item.nav_date for item in nav_history)
        else:
            nav_fingerprint = None
            nav_start = None
            nav_end = None
        nav_conflicts = self._nav_conflict_resolver(
            fund_code,
            nav_history,
            tuple(legal_facts),
        )
        if type(nav_conflicts) is not tuple or nav_conflicts != tuple(sorted(set(nav_conflicts))):
            raise ValueError("NAV conflicts must be a sorted unique tuple")
        if not set(nav_conflicts).issubset(CLASSIFICATION_CONFLICT_CODES):
            raise ValueError("NAV conflicts must use declared codes")

        evidence = ClassificationEvidence(
            fund_code=fund_code,
            legal_facts=tuple(sorted(legal_facts, key=_fact_order)),
            benchmark_facts=tuple(sorted(benchmark_facts, key=_fact_order)),
            report_facts=report_mandate_facts,
            existing_disclosure_facts=tuple(sorted(existing_facts, key=_fact_order)),
            nav_conflicts=nav_conflicts,
            external_evidence_fingerprints=external,
            external_source_references=external_sources,
            nav_evidence_fingerprint=nav_fingerprint,
            nav_observation_start=nav_start,
            nav_observation_end=nav_end,
            freshness=tuple(
                sorted(
                    (
                        evidence_freshness(record.artifact, self._policy, classified_at)
                        for record in records
                    ),
                    key=lambda item: (item.section, item.source_document_id),
                )
            ),
            document_ids=tuple(sorted(record.artifact.id for record in records)),
            fact_ids=tuple(sorted(fact_ids)),
            parse_result_ids=tuple(sorted(record.parse_result.id for record in records)),
            parser_provenance_checksums=tuple(
                sorted({record.provenance.provenance_checksum for record in records})
            ),
            document_refresh_run_id=snapshot.selection.refresh_run_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        evidence.validate()
        return evidence

    def _require_active_provenance(self, records: tuple) -> None:
        if type(records) is not tuple:
            raise RiskServiceError("classification_storage_failed")
        native = native_parser_provenance()
        legacy_checksums = set()
        historical_provenance_found = False
        for record in records:
            requirement = getattr(record, "provenance", None)
            if type(requirement) is not StoredParserProvenance:
                raise RiskServiceError("classification_storage_failed")
            try:
                provenance = ParserProvenance(
                    parser_version=requirement.parser_version,
                    converter_kind=requirement.converter_kind,
                    canonical_json=requirement.canonical_json,
                    provenance_checksum=requirement.provenance_checksum,
                )
                provenance.validate()
            except (TypeError, ValueError) as exc:
                raise RiskServiceError("classification_storage_failed") from exc
            if provenance.converter_kind == "none":
                if provenance.parser_version in HISTORICAL_NATIVE_PARSER_VERSIONS:
                    historical_provenance_found = True
                    continue
                if provenance != native:
                    raise RiskServiceError("classification_storage_failed")
            elif provenance.converter_kind == "docker_libreoffice":
                if provenance.parser_version in HISTORICAL_LEGACY_PARSER_VERSIONS:
                    historical_provenance_found = True
                    continue
                if provenance.parser_version != ACTIVE_LEGACY_PARSER_VERSION:
                    raise RiskServiceError("classification_storage_failed")
                legacy_checksums.add(provenance.provenance_checksum)
            else:
                raise RiskServiceError("classification_storage_failed")

        if historical_provenance_found:
            raise RiskServiceError("official_document_unavailable")
        if legacy_checksums:
            active = self._current_legacy_provenance()
            if active is None or legacy_checksums != {active.provenance_checksum}:
                raise RiskServiceError("official_document_unavailable")


def _validate_parser_result(
    parsed: object,
    artifact: object,
    candidate: object,
) -> None:
    if (
        type(parsed) is not ParsedArtifactResult
        or type(artifact) is not RetrievedArtifact
        or artifact.candidate != candidate
        or parsed.document.artifact != artifact
        or parsed.document.artifact.candidate != candidate
    ):
        raise RiskDocumentParseError(
            "official_document_parse_failed",
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official fund document parsing failed",
        )
    try:
        parsed.validate()
    except ValueError:
        raise RiskDocumentParseError(
            "official_document_parse_failed",
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official fund document parsing failed",
        ) from None


def select_current_documents(records: tuple) -> tuple:
    """Compatibility wrapper for newest-per-kind document selection."""

    return select_current_document_records(records)


def evidence_freshness(
    artifact: object,
    policy: ClassificationPolicyV1,
    as_of: datetime,
) -> EvidenceFreshness:
    """Calculate policy-specific freshness for one authenticated public document."""

    policy.validate()
    as_of = _canonical_now(as_of)
    observed_at = artifact.published_at or artifact.retrieved_at
    observed_at = _canonical_now(observed_at)
    kind = artifact.document_kind
    state_override = None

    if kind is DocumentKind.CLASSIFICATION_ANNOUNCEMENT:
        valid_until = observed_at + timedelta(microseconds=1)
        state_override = FreshnessState.INVALIDATED
    elif kind is DocumentKind.FUND_CONTRACT:
        valid_until = datetime.max.replace(tzinfo=timezone.utc)
    elif kind is DocumentKind.PRODUCT_SUMMARY:
        valid_until = observed_at + timedelta(days=policy.product_summary_review_days)
    elif kind is DocumentKind.INDEX_METHODOLOGY:
        valid_until = observed_at + timedelta(days=policy.index_methodology_review_days)
    elif kind in {
        DocumentKind.QUARTERLY_REPORT,
        DocumentKind.SEMIANNUAL_REPORT,
        DocumentKind.ANNUAL_REPORT,
    }:
        period_end = _report_period_end(artifact.title)
        if period_end is None:
            valid_until = observed_at + timedelta(microseconds=1)
            state_override = FreshnessState.INVALIDATED
        else:
            deadline = policy.periodic_report_deadline(_next_report_period_end(period_end))
            valid_until = datetime.combine(deadline, datetime.min.time(), tzinfo=timezone.utc)
            if valid_until <= observed_at:
                valid_until = observed_at + timedelta(microseconds=1)
                state_override = FreshnessState.INVALIDATED
    else:
        valid_until = observed_at + timedelta(days=policy.legal_document_review_days)

    state = state_override or (
        FreshnessState.CURRENT if as_of < valid_until else FreshnessState.STALE
    )
    result = EvidenceFreshness(
        section=kind.value,
        source_document_id=artifact.id,
        state=state,
        observed_at=observed_at,
        valid_until=valid_until,
        critical=True,
    )
    result.validate()
    return result


def _report_period_end(title: str) -> Optional[date]:
    normalized = title.replace("第一", "第1").replace("第二", "第2")
    normalized = normalized.replace("第三", "第3").replace("第四", "第4")
    year_match = re.search(r"(?P<year>20\d{2})(?:年|[-/])", normalized)
    if year_match is None:
        return None
    year = int(year_match.group("year"))
    if re.search(r"第?1季度|q1\b", normalized, re.IGNORECASE):
        return date(year, 3, 31)
    if re.search(r"半年度|第?2季度|q2\b", normalized, re.IGNORECASE):
        return date(year, 6, 30)
    if re.search(r"第?3季度|q3\b", normalized, re.IGNORECASE):
        return date(year, 9, 30)
    if re.search(r"年度|第?4季度|q4\b", normalized, re.IGNORECASE):
        return date(year, 12, 31)
    return None


def _next_report_period_end(period_end: date) -> date:
    if (period_end.month, period_end.day) == (3, 31):
        return date(period_end.year, 6, 30)
    if (period_end.month, period_end.day) == (6, 30):
        return date(period_end.year, 9, 30)
    if (period_end.month, period_end.day) == (9, 30):
        return date(period_end.year, 12, 31)
    if (period_end.month, period_end.day) == (12, 31):
        return date(period_end.year + 1, 3, 31)
    raise ValueError("unsupported report period end")


def _mandate_fact(stored: object) -> MandateFact:
    fact = MandateFact(
        fund_code=stored.fund_code,
        fact_kind=stored.fact_kind,
        normalized_value=stored.normalized_value,
        unit=stored.unit,
        source_document_id=stored.source_document_id,
        page_number=stored.page_number,
        section_name=stored.section_name,
        source_excerpt=stored.source_excerpt,
        effective_from=stored.effective_from,
        effective_to=stored.effective_to,
        confidence_state=stored.confidence_state,
        parser_version=stored.parser_version,
        fact_fingerprint=stored.fact_fingerprint,
    )
    fact.validate()
    return fact


def _fact_order(fact: MandateFact) -> tuple:
    return (fact.fact_kind, fact.source_document_id, fact.fact_fingerprint)


def _external_fingerprints(bundle: object) -> Tuple[Tuple[str, str], ...]:
    sections = _external_sections(bundle)
    bindings = []
    source_documents = getattr(bundle, "source_documents", {})
    for section, (records, document_kind) in sections.items():
        source_ids = sorted(
            {
                item.source_document_id
                for item in records
                if getattr(item, "source_document_id", None) is not None
            }
        )
        payload = {
            "records": records,
            "section_state": getattr(bundle, "section_states", {}).get(document_kind.value),
            "section_status": getattr(bundle, "section_statuses", {}).get(document_kind.value),
            "sources": tuple(source_documents[source_id] for source_id in source_ids),
        }
        bindings.append((section, _fingerprint(payload)))
    return tuple(sorted(bindings))


def _external_sections(bundle: object) -> dict:
    return {
        "identity": (
            () if bundle.identity is None else (bundle.identity,),
            DocumentKind.BASIC_PROFILE,
        ),
        "share_class": (tuple(bundle.share_classes), DocumentKind.BASIC_PROFILE),
        "benchmark": (tuple(bundle.benchmarks), DocumentKind.BENCHMARK),
        "holdings": (tuple(bundle.holdings), DocumentKind.QUARTERLY_HOLDINGS),
        "industry": (tuple(bundle.industry_exposure), DocumentKind.INDUSTRY_EXPOSURE),
        "size": (tuple(bundle.sizes), DocumentKind.SIZE_HISTORY),
        "fees": (tuple(bundle.fee_rules), DocumentKind.FEE_SCHEDULE),
    }


def _external_source_references(bundle: object) -> Tuple[ExternalSourceReference, ...]:
    references = []
    source_documents = getattr(bundle, "source_documents", {})
    for section, (records, _document_kind) in _external_sections(bundle).items():
        source_ids = sorted(
            {
                item.source_document_id
                for item in records
                if getattr(item, "source_document_id", None) is not None
            }
        )
        for source_id in source_ids:
            source = source_documents[source_id]
            reference = ExternalSourceReference(
                source_namespace="fund_disclosure",
                source_document_id=source_id,
                fund_code=source.fund_code,
                document_kind=source.document_kind.value,
                section=section,
                title=source.title,
                url=source.url,
                source_name=source.source_name,
                source_tier=source.source_tier,
                publisher=source.publisher,
                published_at=(
                    None
                    if source.published_at is None
                    else source.published_at.astimezone(timezone.utc)
                ),
                retrieved_at=source.retrieved_at.astimezone(timezone.utc),
                checksum=source.checksum,
            )
            reference.validate()
            references.append(reference)
    return tuple(
        sorted(
            references,
            key=lambda item: (
                item.source_namespace,
                item.source_document_id,
                item.section,
            ),
        )
    )


def _external_facts(
    bundle: object,
    classified_at: datetime,
) -> Tuple[MandateFact, ...]:
    facts = []

    identity = getattr(bundle, "identity", None)
    identities = _current_sourced_records(
        bundle,
        () if identity is None else (identity,),
        DocumentKind.BASIC_PROFILE,
        classified_at,
    )
    if identities:
        current_identity = identities[0]
        fund_name = getattr(current_identity, "fund_name", None)
        if type(fund_name) is str and fund_name.strip():
            facts.append(
                _synthetic_fact(
                    current_identity.fund_code,
                    "fund_name",
                    fund_name,
                    current_identity.source_document_id,
                    "identity",
                )
            )
        platform_category = getattr(current_identity, "fund_type", None)
        if type(platform_category) is str and platform_category.strip():
            facts.append(
                _synthetic_fact(
                    current_identity.fund_code,
                    "platform_category",
                    platform_category,
                    current_identity.source_document_id,
                    "identity",
                )
            )

    share_classes = _current_sourced_records(
        bundle,
        bundle.share_classes,
        DocumentKind.BASIC_PROFILE,
        classified_at,
    )
    if share_classes:
        facts.append(
            _synthetic_fact(
                share_classes[0].fund_code,
                "share_class_evidence_present",
                True,
                share_classes[0].source_document_id,
                "share_class",
            )
        )

    fee_rules = _current_sourced_records(
        bundle,
        bundle.fee_rules,
        DocumentKind.FEE_SCHEDULE,
        classified_at,
    )
    if fee_rules:
        facts.append(
            _synthetic_fact(
                fee_rules[0].fund_code,
                "fee_evidence_present",
                True,
                fee_rules[0].source_document_id,
                "fees",
            )
        )

    sizes = _current_sourced_records(
        bundle,
        bundle.sizes,
        DocumentKind.SIZE_HISTORY,
        classified_at,
    )
    sizes = tuple(
        item for item in sizes if item.net_assets is not None or item.total_shares is not None
    )
    if sizes:
        latest_date = max(item.report_date for item in sizes)
        current_sizes = tuple(item for item in sizes if item.report_date == latest_date)
        facts.append(
            _synthetic_fact(
                current_sizes[0].fund_code,
                "size_evidence_present",
                True,
                current_sizes[0].source_document_id,
                "size",
                effective_from=latest_date,
                effective_to=latest_date,
            )
        )

    holdings = _current_sourced_records(
        bundle,
        bundle.holdings,
        DocumentKind.QUARTERLY_HOLDINGS,
        classified_at,
    )
    if holdings:
        latest_period = max(item.report_period for item in holdings)
        current_holdings = tuple(item for item in holdings if item.report_period == latest_period)
        stock_holdings = tuple(
            item for item in current_holdings if item.asset_type.value == "stock"
        )
        source_id = current_holdings[0].source_document_id
        complete = all(item.disclosure_scope == "complete" for item in current_holdings)
        facts.append(
            _synthetic_fact(
                current_holdings[0].fund_code,
                "holdings_evidence_complete",
                complete,
                source_id,
                "holdings",
                effective_from=latest_period,
                effective_to=latest_period,
            )
        )
        if stock_holdings and min(item.rank for item in stock_holdings) == 1:
            facts.append(
                _synthetic_fact(
                    stock_holdings[0].fund_code,
                    "current_largest_security_weight_percent",
                    max(item.weight for item in stock_holdings),
                    source_id,
                    "holdings",
                    unit="percent_of_net_assets",
                    effective_from=latest_period,
                    effective_to=latest_period,
                )
            )
        top_ten = tuple(item for item in stock_holdings if 1 <= item.rank <= 10)
        if {item.rank for item in top_ten} == set(range(1, 11)):
            facts.append(
                _synthetic_fact(
                    top_ten[0].fund_code,
                    "current_top_ten_holdings_weight_percent",
                    sum((item.weight for item in top_ten), Decimal("0")),
                    source_id,
                    "holdings",
                    unit="percent_of_net_assets",
                    effective_from=latest_period,
                    effective_to=latest_period,
                )
            )
        if complete:
            stock_weight = sum(
                (item.weight for item in stock_holdings),
                Decimal("0"),
            )
            facts.append(
                _synthetic_fact(
                    current_holdings[0].fund_code,
                    "current_stock_asset_allocation_percent",
                    stock_weight,
                    source_id,
                    "holdings",
                    unit="percent_of_net_assets",
                    effective_from=latest_period,
                    effective_to=latest_period,
                )
            )

    return tuple(sorted(facts, key=_fact_order))


def _current_sourced_records(
    bundle: object,
    records: object,
    section: DocumentKind,
    classified_at: datetime,
) -> tuple:
    values = tuple(
        item for item in records if getattr(item, "source_document_id", None) is not None
    )
    if not values:
        return ()
    status = getattr(bundle, "section_statuses", {}).get(section.value)
    if status is None or status.get("state") != "success":
        return ()
    current_source_id = status.get("current_source_document_id")
    if type(current_source_id) is not int:
        return ()
    values = tuple(item for item in values if item.source_document_id == current_source_id)
    if not values:
        return ()
    source = bundle.source_documents.get(current_source_id)
    if source is None:
        return ()
    if section in {DocumentKind.QUARTERLY_HOLDINGS, DocumentKind.INDUSTRY_EXPOSURE}:
        latest_period = max(item.report_period for item in values)
        if latest_period < expected_report_period(classified_at.date()):
            return ()
    else:
        age = classified_at - source.retrieved_at.astimezone(timezone.utc)
        if age > timedelta(days=30):
            return ()
    return values


def _synthetic_fact(
    fund_code: str,
    fact_kind: str,
    value: object,
    source_document_id: int,
    section_name: str,
    *,
    unit: Optional[str] = None,
    effective_from: Optional[date] = None,
    effective_to: Optional[date] = None,
) -> MandateFact:
    fields = {
        "fact_kind": fact_kind,
        "normalized_value": value,
        "unit": unit,
        "page_number": None,
        "section_name": section_name,
        "source_excerpt": f"derived from current sourced {section_name} disclosure",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "confidence_state": FactConfidence.EXACT,
    }
    fact = MandateFact(
        fund_code=fund_code,
        source_document_id=source_document_id,
        parser_version="external_disclosure_v1",
        fact_fingerprint=fact_fingerprint(**fields),
        **fields,
    )
    fact.validate()
    return fact


def _evidence_token(
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
) -> str:
    manifest = classification_input_manifest(evidence, policy, policy.effective_at)
    return hashlib.sha256(_canonical_json(manifest).encode("ascii")).hexdigest()


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(_canonical_public(value)).encode("ascii")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _canonical_public(value: object) -> object:
    if isinstance(value, Enum):
        return {"type": "enum", "value": value.value}
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("public evidence Decimal must be finite")
        normalized = value.normalize()
        return {
            "type": "decimal",
            "value": format(normalized, "f") if normalized != 0 else "0",
        }
    if type(value) is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("public evidence datetime must be timezone-aware")
        return {"type": "datetime", "value": value.astimezone(timezone.utc).isoformat()}
    if type(value) is date:
        return {"type": "date", "value": value.isoformat()}
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) in {tuple, list}:
        return [_canonical_public(item) for item in value]
    if type(value) is dict:
        return {
            str(key): _canonical_public(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_public(getattr(value, field.name)) for field in fields(value)
        }
    raise ValueError("public evidence contains an unsupported value")


def _service_error(error: Exception) -> RiskServiceError:
    if type(error) is RiskServiceError:
        return error
    return RiskServiceError(_safe_failure(error).public_code)


def _safe_failure(error: Exception) -> SafeDocumentFailure:
    if isinstance(
        error,
        (LegacyDocConversionError, OfficialDocumentError, RiskDocumentParseError),
    ):
        error.failure.validate()
        return error.failure
    if isinstance(error, RiskStoreError):
        return _storage_failure()
    return unspecified_document_failure()


def _storage_failure() -> SafeDocumentFailure:
    failure = SafeDocumentFailure(
        "classification_storage_failed",
        DocumentFailureStage.PERSISTENCE,
        DocumentFailureReason.STORAGE_FAILURE,
    )
    failure.validate()
    return failure


def _item_failure(item: DocumentSyncItem) -> SafeDocumentFailure:
    if (
        item.status != "failed"
        or item.error_code is None
        or item.failure_stage is None
        or item.failure_reason is None
    ):
        raise ValueError("failed refresh item lacks an exact safe failure")
    failure = SafeDocumentFailure(
        item.error_code,
        DocumentFailureStage(item.failure_stage),
        DocumentFailureReason(item.failure_reason),
    )
    failure.validate()
    return failure


def _public_selection_items(
    selection: DocumentSelectionPlan,
) -> Tuple[DocumentSelectionItem, ...]:
    selection.validate()
    candidates = {
        item.candidate_fingerprint: item for item in selection.periodic_candidates
    }
    items = []
    for state in selection.periodic_states:
        selected = (
            None
            if state.selected_fingerprint is None
            else candidates[state.selected_fingerprint]
        )
        item = DocumentSelectionItem(
            document_kind=state.document_kind.value,
            status=state.state,
            selected_url=None if selected is None else selected.url,
            candidate_count=len(state.candidate_fingerprints),
            reason_code=state.reason_code,
        )
        item.validate()
        items.append(item)
    return tuple(sorted(items, key=lambda item: item.document_kind))


def _require_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    state = vars(value)
    expected_fields = {field.name for field in fields(expected_type)}
    if type(state) is not dict or set(state) != expected_fields:
        raise ValueError(f"{label} has unexpected state")


def _validate_sync_title(value: object) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > 512
        or "\x00" in value
        or "\r" in value
        or "\n" in value
    ):
        raise ValueError("document sync title is invalid")
    try:
        validate_public_risk_string(value)
    except ValueError:
        raise ValueError("document sync title is invalid") from None


def validate_public_risk_string(value: object) -> None:
    if type(value) is not str or "\x00" in value:
        raise ValueError("risk public string must be exact safe text")
    if _ABSOLUTE_URL.match(value) is not None:
        validate_public_risk_url(value)
        return
    _reject_unsafe_public_text(value)


def validate_public_risk_url(value: object) -> None:
    if type(value) is not str or len(value) > 4096 or "\x00" in value:
        raise ValueError("risk public URL must be exact bounded text")
    try:
        parsed_url = validate_safe_https_url(value)
    except ValueError:
        raise ValueError("risk public URL must use safe HTTPS") from None
    if parsed_url.fragment:
        raise ValueError("risk public URL cannot contain a fragment")
    _reject_unsafe_public_text(parsed_url.query)


def _reject_unsafe_public_text(value: str) -> None:
    decoded = value
    for _ in range(_PUBLIC_TEXT_DECODE_PASSES + 1):
        if _UNSAFE_PUBLIC_TEXT.search(decoded) is not None:
            raise ValueError("risk public string contains private implementation details")
        try:
            next_value = urllib.parse.unquote(decoded, errors="strict")
        except UnicodeDecodeError:
            raise ValueError("risk public string contains invalid encoded text") from None
        if next_value == decoded:
            return
        decoded = next_value
    if _UNSAFE_PUBLIC_TEXT.search(decoded) is not None:
        raise ValueError("risk public string contains private implementation details")
    if urllib.parse.unquote(decoded, errors="replace") != decoded:
        raise ValueError("risk public string exceeds the decoding boundary")


def _validate_sync_codes(
    values: object,
    allowed: frozenset[str],
    label: str,
) -> None:
    if type(values) is not tuple or values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be an immutable sorted unique tuple")
    if any(type(value) is not str or value not in allowed for value in values):
        raise ValueError(f"{label} contains an unknown code")


def _validate_canonical_utc(value: object, label: str) -> None:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{label} must use canonical UTC")


def _validate_fund_code(fund_code: object) -> None:
    if type(fund_code) is not str or not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError("fund code must contain six digits")


def _canonical_now(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("risk service clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_optional_utc(value: object) -> Optional[datetime]:
    if value is None:
        return None
    return _canonical_now(value)


__all__ = [
    "DocumentSelectionItem",
    "DocumentSyncItem",
    "DocumentSyncResult",
    "FundRiskService",
    "RiskServiceError",
    "evidence_freshness",
    "select_current_documents",
    "validate_public_risk_string",
    "validate_public_risk_url",
]
