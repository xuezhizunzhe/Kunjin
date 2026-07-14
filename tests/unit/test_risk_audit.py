from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import (
    CandidateRunOutcome,
    ParserProvenance,
    ParseRunKind,
    ParseRunOutcome,
    RefreshOutcome,
    candidate_fingerprint,
    canonical_fact_set_fingerprint,
    legacy_parser_provenance,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import OfficialDocumentCandidate

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def candidate(**changes: object) -> OfficialDocumentCandidate:
    values = {
        "fund_code": "519755",
        "document_kind": DocumentKind.QUARTERLY_REPORT,
        "title": "example fund 2026 second-quarter report",
        "url": "https://www.fund001.com/reports/519755-q2.doc",
        "publisher": "example fund manager",
        "published_at": NOW,
        "source_tier": 1,
    }
    values.update(changes)
    return OfficialDocumentCandidate(**values)


def provenance_from_payload(payload: dict[str, object]) -> ParserProvenance:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return ParserProvenance(
        parser_version=str(payload["parser_version"]),
        converter_kind=str(payload["converter_kind"]),
        canonical_json=canonical,
        provenance_checksum=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    )


class RiskAuditTests(unittest.TestCase):
    def test_native_provenance_is_fixed_and_canonical(self) -> None:
        value = native_parser_provenance()

        value.validate()

        self.assertEqual(value.parser_version, "2")
        self.assertEqual(value.converter_kind, "none")
        self.assertEqual(
            value.canonical_json,
            '{"contract_version":"native-v1","converter_kind":"none","parser_version":"2"}',
        )
        self.assertEqual(len(value.provenance_checksum), 64)
        self.assertEqual(value.canonical_json.encode("ascii").decode("ascii"), value.canonical_json)

    def test_legacy_provenance_is_fixed_and_canonical(self) -> None:
        image_id = "sha256:" + "a" * 64
        package_checksum = "b" * 64
        libreoffice_version = "4:7.4.7-1+deb12u14"

        value = legacy_parser_provenance(
            image_id=image_id,
            architecture="linux/arm64",
            libreoffice_version=libreoffice_version,
            package_manifest_checksum=package_checksum,
        )

        value.validate()
        self.assertEqual(value.parser_version, "2-docker-libreoffice-v1")
        self.assertEqual(value.converter_kind, "docker_libreoffice")
        self.assertEqual(
            json.loads(value.canonical_json),
            {
                "adapter_contract_version": "docker-libreoffice-v1",
                "architecture": "linux/arm64",
                "converter_kind": "docker_libreoffice",
                "export_filter": "html_starwriter_skip_images_v1",
                "image_id": image_id,
                "libreoffice_version": libreoffice_version,
                "normalization_contract": "legacy_html_nfc_v1",
                "package_manifest_checksum": package_checksum,
                "parser_version": "2-docker-libreoffice-v1",
            },
        )

    def test_retryable_failure_and_success_outcomes_are_distinct(self) -> None:
        self.assertEqual(ParseRunOutcome.FAILED.value, "failed")
        self.assertEqual(ParseRunOutcome.SUCCESS.value, "success")
        self.assertEqual(ParseRunKind.LIVE.value, "live")
        self.assertEqual(ParseRunKind.LEGACY_BACKFILL.value, "legacy_backfill")
        self.assertEqual(RefreshOutcome.SUCCESS.value, "success")
        self.assertEqual(RefreshOutcome.PARTIAL.value, "partial")
        self.assertEqual(RefreshOutcome.FAILED.value, "failed")
        self.assertEqual(RefreshOutcome.EMPTY.value, "empty")
        self.assertEqual(CandidateRunOutcome.SUCCESS.value, "success")
        self.assertEqual(CandidateRunOutcome.FAILED.value, "failed")

    def test_enums_reject_unknown_codes_and_provenance_is_frozen(self) -> None:
        for enum_type in (
            RefreshOutcome,
            CandidateRunOutcome,
            ParseRunKind,
            ParseRunOutcome,
        ):
            with self.subTest(enum_type=enum_type), self.assertRaises(ValueError):
                enum_type("unknown")

        with self.assertRaises(FrozenInstanceError):
            native_parser_provenance().parser_version = "changed"

    def test_provenance_rejects_subclasses_hidden_state_and_unknown_contracts(self) -> None:
        fixed = native_parser_provenance()

        class ProvenanceSubclass(ParserProvenance):
            pass

        subclass = ProvenanceSubclass(**vars(fixed))
        with self.assertRaisesRegex(ValueError, "subclasses"):
            subclass.validate()

        hidden = native_parser_provenance()
        object.__setattr__(hidden, "hidden", "state")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            hidden.validate()

        unknown = provenance_from_payload(
            {
                "contract_version": "unknown-v1",
                "converter_kind": "unknown_converter",
                "parser_version": "2",
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            unknown.validate()

    def test_provenance_rejects_noncanonical_json_and_invalid_checksum(self) -> None:
        fixed = native_parser_provenance()
        noncanonical = replace(
            fixed,
            canonical_json=json.dumps(json.loads(fixed.canonical_json), indent=2),
        )
        with self.assertRaisesRegex(ValueError, "canonical"):
            noncanonical.validate()

        invalid_checksum = replace(fixed, provenance_checksum="A" * 64)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            invalid_checksum.validate()

        mismatched_checksum = replace(fixed, provenance_checksum="0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum"):
            mismatched_checksum.validate()

    def test_legacy_provenance_rejects_unpinned_or_unknown_runtime_fields(self) -> None:
        valid = {
            "image_id": "sha256:" + "a" * 64,
            "architecture": "linux/arm64",
            "libreoffice_version": "4:7.4.7-1+deb12u14",
            "package_manifest_checksum": "b" * 64,
        }
        for changes in (
            {"image_id": "legacy-doc:latest"},
            {"image_id": "sha256:" + "A" * 64},
            {"architecture": "linux/amd64"},
            {"libreoffice_version": ""},
            {"libreoffice_version": "25.2 3"},
            {"libreoffice_version": "v7.4.7"},
            {"libreoffice_version": "1" * 129},
            {"package_manifest_checksum": "B" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                legacy_parser_provenance(**(valid | changes))

    def test_candidate_fingerprint_binds_every_variable_public_field(self) -> None:
        original = candidate()
        baseline = candidate_fingerprint(original)
        changes = (
            {"fund_code": "519706"},
            {"document_kind": DocumentKind.ANNUAL_REPORT},
            {"title": "example fund 2026 annual report"},
            {"url": "https://www.fund001.com/reports/519755-annual.doc"},
            {"publisher": "another official fund manager"},
            {"published_at": NOW + timedelta(days=1)},
            {"published_at": None},
        )

        self.assertEqual(candidate_fingerprint(original), baseline)
        for changed_fields in changes:
            with self.subTest(changed_fields=changed_fields):
                self.assertNotEqual(
                    candidate_fingerprint(replace(original, **changed_fields)),
                    baseline,
                )

        with self.assertRaises(ValueError):
            candidate_fingerprint(replace(original, source_tier=2))

    def test_candidate_fingerprint_rejects_non_utc_time_and_subclasses(self) -> None:
        non_utc = candidate(published_at=NOW.astimezone(timezone(timedelta(hours=8))))
        with self.assertRaisesRegex(ValueError, "UTC"):
            candidate_fingerprint(non_utc)

        class CandidateSubclass(OfficialDocumentCandidate):
            pass

        subclass = CandidateSubclass(**vars(candidate()))
        with self.assertRaisesRegex(ValueError, "subclasses"):
            candidate_fingerprint(subclass)

    def test_fact_set_fingerprint_is_order_independent_and_duplicate_rejecting(self) -> None:
        first = "1" * 64
        second = "2" * 64

        self.assertEqual(
            canonical_fact_set_fingerprint((first, second)),
            canonical_fact_set_fingerprint((second, first)),
        )
        self.assertNotEqual(
            canonical_fact_set_fingerprint((first,)),
            canonical_fact_set_fingerprint((first, second)),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            canonical_fact_set_fingerprint((first, first))

    def test_fact_set_fingerprint_rejects_mutable_or_invalid_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "tuple"):
            canonical_fact_set_fingerprint(["1" * 64])  # type: ignore[arg-type]
        for value in ("A" * 64, "1" * 63, 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SHA-256"):
                canonical_fact_set_fingerprint((value,))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
