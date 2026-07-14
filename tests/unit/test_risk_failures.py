import dataclasses
import unittest
from enum import Enum

from kunjin.funds.risk.failures import (
    DOCUMENT_FAILURE_PUBLIC_CODES,
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
    unspecified_document_failure,
)


class DocumentFailureModelTest(unittest.TestCase):
    def test_enums_and_public_codes_are_fixed(self) -> None:
        self.assertTrue(issubclass(DocumentFailureStage, str))
        self.assertTrue(issubclass(DocumentFailureStage, Enum))
        self.assertEqual(
            {item.value for item in DocumentFailureStage},
            {
                "discovery",
                "landing_validation",
                "retrieval",
                "identity_validation",
                "container_validation",
                "conversion",
                "parser",
                "persistence",
                "unspecified",
            },
        )
        self.assertIn(
            DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
            tuple(DocumentFailureReason),
        )
        self.assertEqual(
            {item.value for item in DocumentFailureReason},
            {
                "dns_unavailable",
                "network_unavailable",
                "http_unavailable",
                "source_unregistered",
                "redirect_rejected",
                "discovery_format_invalid",
                "identity_mismatch",
                "publication_date_missing",
                "landing_format_invalid",
                "landing_title_mismatch",
                "landing_date_mismatch",
                "attachment_missing",
                "attachment_ambiguous",
                "attachment_host_rejected",
                "authentication_shell",
                "empty_or_script_only_html",
                "declared_mime_unsupported",
                "detected_container_unknown",
                "declared_detected_mismatch",
                "legacy_ole_container_unsupported",
                "legacy_converter_unavailable",
                "legacy_converter_timeout",
                "legacy_converter_resource_limit",
                "legacy_converter_failed",
                "legacy_converter_output_invalid",
                "resource_limit",
                "parser_format_invalid",
                "parser_identity_mismatch",
                "parser_effective_date_invalid",
                "parser_ambiguous_fact",
                "clock_invalid",
                "managed_artifact_invalid",
                "storage_failure",
                "unspecified_failure",
            },
        )
        self.assertEqual(
            DOCUMENT_FAILURE_PUBLIC_CODES,
            frozenset(
                {
                    "official_document_unavailable",
                    "official_document_invalid",
                    "official_document_resource_limit",
                    "official_document_parse_failed",
                    "classification_storage_failed",
                }
            ),
        )

    def test_record_is_exact_frozen_and_allowlisted(self) -> None:
        failure = SafeDocumentFailure(
            public_code="official_document_invalid",
            stage=DocumentFailureStage.CONTAINER_VALIDATION,
            reason_code=DocumentFailureReason.LEGACY_OLE_CONTAINER_UNSUPPORTED,
        )
        failure.validate()
        self.assertEqual(
            {field.name for field in dataclasses.fields(SafeDocumentFailure)},
            {"public_code", "stage", "reason_code"},
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            failure.public_code = "official_document_unavailable"

    def test_subclasses_hidden_state_and_unknown_values_are_rejected(self) -> None:
        class HiddenFailure(SafeDocumentFailure):
            pass

        with self.assertRaises(ValueError):
            HiddenFailure(
                "official_document_invalid",
                DocumentFailureStage.UNSPECIFIED,
                DocumentFailureReason.UNSPECIFIED_FAILURE,
            ).validate()
        failure = unspecified_document_failure()
        object.__setattr__(failure, "private_detail", "sentinel")
        with self.assertRaises(ValueError):
            failure.validate()
        with self.assertRaises(ValueError):
            SafeDocumentFailure(
                "private_code",
                DocumentFailureStage.UNSPECIFIED,
                DocumentFailureReason.UNSPECIFIED_FAILURE,
            ).validate()

    def test_unknown_fallback_contains_no_free_form_detail(self) -> None:
        failure = unspecified_document_failure()
        self.assertEqual(failure.public_code, "official_document_invalid")
        self.assertEqual(failure.stage, DocumentFailureStage.UNSPECIFIED)
        self.assertEqual(
            failure.reason_code,
            DocumentFailureReason.UNSPECIFIED_FAILURE,
        )
        self.assertNotIn("exception", repr(failure).lower())
