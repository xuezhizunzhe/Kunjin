from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class DocumentFailureStage(str, Enum):
    DISCOVERY = "discovery"
    LANDING_VALIDATION = "landing_validation"
    RETRIEVAL = "retrieval"
    IDENTITY_VALIDATION = "identity_validation"
    CONTAINER_VALIDATION = "container_validation"
    CONVERSION = "conversion"
    PARSER = "parser"
    PERSISTENCE = "persistence"
    UNSPECIFIED = "unspecified"


class DocumentFailureReason(str, Enum):
    DNS_UNAVAILABLE = "dns_unavailable"
    NETWORK_UNAVAILABLE = "network_unavailable"
    HTTP_UNAVAILABLE = "http_unavailable"
    SOURCE_UNREGISTERED = "source_unregistered"
    REDIRECT_REJECTED = "redirect_rejected"
    DISCOVERY_FORMAT_INVALID = "discovery_format_invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    PUBLICATION_DATE_MISSING = "publication_date_missing"
    LANDING_FORMAT_INVALID = "landing_format_invalid"
    LANDING_TITLE_MISMATCH = "landing_title_mismatch"
    LANDING_DATE_MISMATCH = "landing_date_mismatch"
    ATTACHMENT_MISSING = "attachment_missing"
    ATTACHMENT_AMBIGUOUS = "attachment_ambiguous"
    ATTACHMENT_HOST_REJECTED = "attachment_host_rejected"
    AUTHENTICATION_SHELL = "authentication_shell"
    EMPTY_OR_SCRIPT_ONLY_HTML = "empty_or_script_only_html"
    DECLARED_MIME_UNSUPPORTED = "declared_mime_unsupported"
    DETECTED_CONTAINER_UNKNOWN = "detected_container_unknown"
    DECLARED_DETECTED_MISMATCH = "declared_detected_mismatch"
    LEGACY_OLE_CONTAINER_UNSUPPORTED = "legacy_ole_container_unsupported"
    LEGACY_CONVERTER_UNAVAILABLE = "legacy_converter_unavailable"
    LEGACY_CONVERTER_TIMEOUT = "legacy_converter_timeout"
    LEGACY_CONVERTER_RESOURCE_LIMIT = "legacy_converter_resource_limit"
    LEGACY_CONVERTER_FAILED = "legacy_converter_failed"
    LEGACY_CONVERTER_OUTPUT_INVALID = "legacy_converter_output_invalid"
    RESOURCE_LIMIT = "resource_limit"
    PARSER_FORMAT_INVALID = "parser_format_invalid"
    PARSER_IDENTITY_MISMATCH = "parser_identity_mismatch"
    PARSER_EFFECTIVE_DATE_INVALID = "parser_effective_date_invalid"
    PARSER_AMBIGUOUS_FACT = "parser_ambiguous_fact"
    CLOCK_INVALID = "clock_invalid"
    MANAGED_ARTIFACT_INVALID = "managed_artifact_invalid"
    STORAGE_FAILURE = "storage_failure"
    UNSPECIFIED_FAILURE = "unspecified_failure"


DOCUMENT_FAILURE_PUBLIC_CODES = frozenset(
    {
        "official_document_unavailable",
        "official_document_invalid",
        "official_document_resource_limit",
        "official_document_parse_failed",
        "classification_storage_failed",
    }
)


@dataclass(frozen=True)
class SafeDocumentFailure:
    public_code: str
    stage: DocumentFailureStage
    reason_code: DocumentFailureReason

    def validate(self) -> None:
        if type(self) is not SafeDocumentFailure:
            raise ValueError("safe document failure subclasses are not accepted")
        if set(vars(self)) != {field.name for field in fields(SafeDocumentFailure)}:
            raise ValueError("safe document failure has unexpected state")
        if (
            type(self.public_code) is not str
            or self.public_code not in DOCUMENT_FAILURE_PUBLIC_CODES
        ):
            raise ValueError("safe document failure public code is invalid")
        if type(self.stage) is not DocumentFailureStage:
            raise ValueError("safe document failure stage is invalid")
        if type(self.reason_code) is not DocumentFailureReason:
            raise ValueError("safe document failure reason is invalid")


def unspecified_document_failure() -> SafeDocumentFailure:
    result = SafeDocumentFailure(
        public_code="official_document_invalid",
        stage=DocumentFailureStage.UNSPECIFIED,
        reason_code=DocumentFailureReason.UNSPECIFIED_FAILURE,
    )
    result.validate()
    return result
