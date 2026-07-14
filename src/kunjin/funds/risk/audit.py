from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import timezone
from enum import Enum
from typing import Iterable

from kunjin.funds.risk.documents import OfficialDocumentCandidate

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_LIBREOFFICE_VERSION_PATTERN = re.compile(r"^[0-9][0-9A-Za-z.+:~_-]{0,127}$")
_MAX_LIBREOFFICE_VERSION_LENGTH = 128

_NATIVE_PAYLOAD = {
    "contract_version": "native-v1",
    "converter_kind": "none",
    "parser_version": "2",
}
_LEGACY_PAYLOAD_KEYS = frozenset(
    {
        "adapter_contract_version",
        "architecture",
        "converter_kind",
        "export_filter",
        "image_id",
        "libreoffice_version",
        "normalization_contract",
        "package_manifest_checksum",
        "parser_version",
    }
)


class RefreshOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    EMPTY = "empty"


class CandidateRunOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class ParseRunKind(str, Enum):
    LIVE = "live"
    LEGACY_BACKFILL = "legacy_backfill"


class ParseRunOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class ParserProvenance:
    parser_version: str
    converter_kind: str
    canonical_json: str
    provenance_checksum: str

    def validate(self) -> None:
        _validate_exact_record(self, ParserProvenance, "parser provenance")
        if type(self.parser_version) is not str or not _VERSION_PATTERN.fullmatch(
            self.parser_version
        ):
            raise ValueError("parser provenance version must be a stable version")
        if type(self.converter_kind) is not str or self.converter_kind not in {
            "none",
            "docker_libreoffice",
        }:
            raise ValueError("parser provenance converter kind is unknown")
        if type(self.canonical_json) is not str:
            raise ValueError("parser provenance canonical JSON must be exact text")
        try:
            self.canonical_json.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("parser provenance canonical JSON must be ASCII") from None
        try:
            payload = json.loads(
                self.canonical_json,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (TypeError, ValueError):
            raise ValueError("parser provenance canonical JSON is invalid") from None
        if type(payload) is not dict or _canonical_json(payload) != self.canonical_json:
            raise ValueError("parser provenance JSON must be canonical")
        if payload.get("parser_version") != self.parser_version:
            raise ValueError("parser provenance version does not match its payload")
        if payload.get("converter_kind") != self.converter_kind:
            raise ValueError("parser provenance converter kind does not match its payload")
        if self.converter_kind == "none":
            if payload != _NATIVE_PAYLOAD:
                raise ValueError("native parser provenance contract is unknown")
        else:
            _validate_legacy_payload(payload)
        _validate_sha256(self.provenance_checksum, "parser provenance checksum")
        expected_checksum = hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()
        if self.provenance_checksum != expected_checksum:
            raise ValueError("parser provenance checksum does not match canonical JSON")


def native_parser_provenance() -> ParserProvenance:
    return _provenance_from_payload(_NATIVE_PAYLOAD)


def legacy_parser_provenance(
    *,
    image_id: str,
    architecture: str,
    libreoffice_version: str,
    package_manifest_checksum: str,
) -> ParserProvenance:
    _validate_legacy_parameters(
        image_id=image_id,
        architecture=architecture,
        libreoffice_version=libreoffice_version,
        package_manifest_checksum=package_manifest_checksum,
    )
    payload = {
        "adapter_contract_version": "docker-libreoffice-v1",
        "architecture": architecture,
        "converter_kind": "docker_libreoffice",
        "export_filter": "html_starwriter_skip_images_v1",
        "image_id": image_id,
        "libreoffice_version": libreoffice_version,
        "normalization_contract": "legacy_html_nfc_v1",
        "package_manifest_checksum": package_manifest_checksum,
        "parser_version": "2-docker-libreoffice-v1",
    }
    return _provenance_from_payload(payload)


def candidate_fingerprint(candidate: OfficialDocumentCandidate) -> str:
    candidate.validate()
    if candidate.published_at is not None and candidate.published_at.tzinfo is not timezone.utc:
        raise ValueError("official document candidate published_at must use canonical UTC")
    payload = {
        "document_kind": candidate.document_kind.value,
        "fund_code": candidate.fund_code,
        "published_at": (
            candidate.published_at.isoformat() if candidate.published_at is not None else None
        ),
        "publisher": candidate.publisher,
        "source_tier": candidate.source_tier,
        "title": candidate.title,
        "url": candidate.url,
    }
    return _canonical_fingerprint(payload)


def canonical_fact_set_fingerprint(fact_fingerprints: Iterable[str]) -> str:
    if type(fact_fingerprints) is not tuple:
        raise ValueError("fact fingerprints must be an immutable tuple")
    for fingerprint in fact_fingerprints:
        _validate_sha256(fingerprint, "fact fingerprint")
    if len(set(fact_fingerprints)) != len(fact_fingerprints):
        raise ValueError("fact fingerprints cannot contain duplicates")
    return _canonical_fingerprint(
        {"fact_fingerprints": list(sorted(fact_fingerprints))},
    )


def _provenance_from_payload(payload: dict[str, object]) -> ParserProvenance:
    canonical = _canonical_json(payload)
    result = ParserProvenance(
        parser_version=payload["parser_version"],
        converter_kind=payload["converter_kind"],
        canonical_json=canonical,
        provenance_checksum=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    )
    result.validate()
    return result


def _validate_legacy_payload(payload: dict[str, object]) -> None:
    if set(payload) != _LEGACY_PAYLOAD_KEYS:
        raise ValueError("legacy parser provenance payload has unknown fields")
    fixed_values = {
        "adapter_contract_version": "docker-libreoffice-v1",
        "architecture": "linux/arm64",
        "converter_kind": "docker_libreoffice",
        "export_filter": "html_starwriter_skip_images_v1",
        "normalization_contract": "legacy_html_nfc_v1",
        "parser_version": "2-docker-libreoffice-v1",
    }
    for key, expected in fixed_values.items():
        if type(payload[key]) is not str or payload[key] != expected:
            raise ValueError(f"legacy parser provenance {key} is unknown")
    _validate_legacy_parameters(
        image_id=payload["image_id"],
        architecture=payload["architecture"],
        libreoffice_version=payload["libreoffice_version"],
        package_manifest_checksum=payload["package_manifest_checksum"],
    )


def _validate_legacy_parameters(
    *,
    image_id: object,
    architecture: object,
    libreoffice_version: object,
    package_manifest_checksum: object,
) -> None:
    if type(image_id) is not str or not _IMAGE_ID_PATTERN.fullmatch(image_id):
        raise ValueError("legacy parser provenance image ID must be an exact SHA-256 image ID")
    if type(architecture) is not str or architecture != "linux/arm64":
        raise ValueError("legacy parser provenance architecture is unknown")
    if (
        type(libreoffice_version) is not str
        or len(libreoffice_version) > _MAX_LIBREOFFICE_VERSION_LENGTH
        or not _LIBREOFFICE_VERSION_PATTERN.fullmatch(libreoffice_version)
    ):
        raise ValueError("legacy parser provenance LibreOffice version must be stable and bounded")
    _validate_sha256(
        package_manifest_checksum,
        "legacy parser provenance package manifest checksum",
    )


def _validate_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    state = vars(value)
    expected_fields = {field.name for field in fields(expected_type)}
    if type(state) is not dict or set(state) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


def _validate_sha256(value: object, label: str) -> None:
    if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()
