from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Optional, Tuple
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RecognizedIndustryTaxonomy:
    taxonomy_id: str
    version: str
    source_aliases: Tuple[str, ...]
    expected_code_pattern: str


@dataclass(frozen=True)
class IndustryTaxonomyMapping:
    metadata: RecognizedIndustryTaxonomy
    source_url: str
    published_at: date
    entries: Tuple[Tuple[str, str, Tuple[str, ...]], ...]
    canonical_json: str
    checksum: str


@dataclass(frozen=True)
class IndustryDistributionRow:
    classification_standard: str
    industry_code: str
    industry_name: str
    rank: int
    weight: Decimal
    unit: str


@dataclass(frozen=True)
class ValidatedIndustryDistribution:
    taxonomy_id: str
    mapping_checksum: str
    rows: Tuple[IndustryDistributionRow, ...]


SW_LEVEL1_2021 = RecognizedIndustryTaxonomy(
    taxonomy_id="sw_level1_2021",
    version="2021",
    source_aliases=("申万一级行业分类(2021)", "申万一级行业分类（2021）"),
    expected_code_pattern=r"801[0-9]{3}",
)

RECOGNIZED_INDUSTRY_TAXONOMIES: Tuple[RecognizedIndustryTaxonomy, ...] = (
    SW_LEVEL1_2021,
)

# Recognized metadata is not sufficient evidence. Production remains disabled until
# a complete official mapping asset is separately reviewed and pinned.
PRODUCTION_TAXONOMY_MAPPINGS: Tuple[IndustryTaxonomyMapping, ...] = ()

_SUPPORTED_UNITS = ("percent",)
_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}")
_DNS_LABEL_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def validate_industry_distribution(
    *,
    rows: Tuple[IndustryDistributionRow, ...],
    complete_scope: bool,
    mappings: Tuple[IndustryTaxonomyMapping, ...] = PRODUCTION_TAXONOMY_MAPPINGS,
) -> Optional[ValidatedIndustryDistribution]:
    _validate_registry(mappings)
    _validate_caller_records(rows, complete_scope)
    if not complete_scope or not rows or not mappings:
        return None

    if any(
        not value.strip()
        for row in rows
        for value in (
            row.classification_standard,
            row.industry_code,
            row.industry_name,
            row.unit,
        )
    ):
        return None

    bounded_standards = tuple(
        _normalize_whitespace(row.classification_standard)
        for row in rows
    )
    if len(set(bounded_standards)) != 1:
        return None

    mapping = _mapping_for_standard(bounded_standards[0], mappings)
    if mapping is None:
        return None

    if any(row.unit not in _SUPPORTED_UNITS for row in rows):
        return None
    if len({row.unit for row in rows}) != 1:
        return None
    if tuple(row.rank for row in rows) != tuple(range(1, len(rows) + 1)):
        return None
    if any(left.weight < right.weight for left, right in zip(rows, rows[1:])):
        return None
    if len(rows) > 1 and rows[0].weight <= rows[1].weight:
        return None

    entry_by_code: dict[str, tuple[str, frozenset[str]]] = {}
    for code, name, aliases in mapping.entries:
        normalized_code = _normalize_safe_text(code, "mapping code")
        normalized_names = frozenset(
            _comparison_key(value, "mapping name") for value in (name, *aliases)
        )
        entry_by_code[normalized_code] = (name, normalized_names)

    seen_codes: set[str] = set()
    seen_names: set[str] = set()
    for row in rows:
        code = _normalize_safe_text(row.industry_code, "industry code")
        name_key = _comparison_key(row.industry_name, "industry name")
        if not re.fullmatch(mapping.metadata.expected_code_pattern, code):
            return None
        mapped = entry_by_code.get(code)
        if mapped is None or name_key not in mapped[1]:
            return None
        if code in seen_codes or name_key in seen_names:
            return None
        seen_codes.add(code)
        seen_names.add(name_key)

    validated = ValidatedIndustryDistribution(
        taxonomy_id=mapping.metadata.taxonomy_id,
        mapping_checksum=mapping.checksum,
        rows=rows,
    )
    _validate_validated_distribution(validated)
    return validated


def _validate_caller_records(
    rows: object,
    complete_scope: object,
) -> None:
    if type(rows) is not tuple:
        raise ValueError("industry rows must be an exact tuple")
    if type(complete_scope) is not bool:
        raise ValueError("complete scope must be an exact bool")
    for row in rows:
        _require_exact_record_state(row, IndustryDistributionRow, "industry row")
        for field_name, value in (
            ("classification standard", row.classification_standard),
            ("industry code", row.industry_code),
            ("industry name", row.industry_name),
            ("unit", row.unit),
        ):
            if type(value) is not str:
                raise ValueError(f"{field_name} must be an exact string")
            _require_safe_unicode(value, field_name)
        if type(row.rank) is not int or row.rank < 1:
            raise ValueError("industry rank must be a positive exact integer")
        if (
            type(row.weight) is not Decimal
            or not row.weight.is_finite()
            or row.weight < 0
            or row.weight > 100
        ):
            raise ValueError("industry weight must be a finite Decimal percent")


def _validate_registry(mappings: object) -> None:
    if type(mappings) is not tuple:
        raise ValueError("taxonomy mappings must be an exact tuple")
    seen_taxonomies: set[tuple[str, str]] = set()
    seen_source_aliases: set[str] = set()
    for mapping in mappings:
        _require_exact_record_state(
            mapping,
            IndustryTaxonomyMapping,
            "taxonomy mapping",
        )
        _validate_mapping(mapping)
        identity = (mapping.metadata.taxonomy_id, mapping.metadata.version)
        if identity in seen_taxonomies:
            raise ValueError("taxonomy registry contains duplicate taxonomy mappings")
        seen_taxonomies.add(identity)
        normalized_aliases = {
            _comparison_key(alias, "taxonomy source alias")
            for alias in mapping.metadata.source_aliases
        }
        if seen_source_aliases.intersection(normalized_aliases):
            raise ValueError("taxonomy registry contains conflicting source aliases")
        seen_source_aliases.update(normalized_aliases)


def _validate_mapping(mapping: IndustryTaxonomyMapping) -> None:
    _require_exact_record_state(
        mapping,
        IndustryTaxonomyMapping,
        "taxonomy mapping",
    )
    _validate_metadata(mapping.metadata)
    if mapping.metadata not in RECOGNIZED_INDUSTRY_TAXONOMIES:
        raise ValueError("taxonomy mapping metadata is not recognized")
    if type(mapping.source_url) is not str:
        raise ValueError("taxonomy source URL must be an exact string")
    normalized_source_url = _normalize_safe_text(
        mapping.source_url,
        "taxonomy source URL",
    )
    if normalized_source_url != mapping.source_url:
        raise ValueError("taxonomy source URL must be canonical")
    if not mapping.source_url.isascii() or any(
        character.isspace() or character == "\\"
        for character in mapping.source_url
    ):
        raise ValueError("taxonomy source URL must use canonical ASCII URL syntax")
    try:
        parsed_url = urlsplit(mapping.source_url)
        parsed_url.port
    except ValueError as exc:
        raise ValueError("taxonomy source URL is invalid") from exc
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.fragment
        or not _is_valid_source_hostname(parsed_url.hostname, parsed_url.netloc)
    ):
        raise ValueError("taxonomy source URL must be an authenticated HTTPS URL")
    if type(mapping.published_at) is not date:
        raise ValueError("taxonomy publication date must be an exact date")
    if type(mapping.entries) is not tuple or not mapping.entries:
        raise ValueError("taxonomy entries must be a non-empty exact tuple")
    if type(mapping.canonical_json) is not str:
        raise ValueError("taxonomy canonical JSON must be an exact string")
    if type(mapping.checksum) is not str or not _CHECKSUM_PATTERN.fullmatch(
        mapping.checksum
    ):
        raise ValueError("taxonomy checksum must be a lowercase SHA-256 digest")

    canonical_entries: list[dict[str, object]] = []
    seen_codes: set[str] = set()
    seen_names: set[str] = set()
    for entry in mapping.entries:
        if type(entry) is not tuple or len(entry) != 3:
            raise ValueError("taxonomy entry must be an exact three-item tuple")
        code, name, aliases = entry
        if type(code) is not str or type(name) is not str or type(aliases) is not tuple:
            raise ValueError("taxonomy entry fields must use exact immutable types")
        normalized_code = _normalize_safe_text(code, "mapping code")
        if normalized_code != code or not re.fullmatch(
            mapping.metadata.expected_code_pattern, code
        ):
            raise ValueError("taxonomy entry code is not canonical")
        canonical_name = _normalize_safe_text(name, "mapping name")
        if canonical_name != name:
            raise ValueError("taxonomy entry name is not canonical")
        name_key = _comparison_key(name, "mapping name")
        if normalized_code in seen_codes or name_key in seen_names:
            raise ValueError("taxonomy entries contain duplicate codes or names")
        seen_codes.add(normalized_code)
        seen_names.add(name_key)

        canonical_aliases: list[str] = []
        for alias in aliases:
            if type(alias) is not str:
                raise ValueError("taxonomy aliases must be exact strings")
            canonical_alias = _normalize_safe_text(alias, "mapping alias")
            alias_key = _comparison_key(alias, "mapping alias")
            if canonical_alias != alias or alias_key in seen_names:
                raise ValueError("taxonomy aliases must be canonical and globally unique")
            seen_names.add(alias_key)
            canonical_aliases.append(alias)
        if canonical_aliases != sorted(canonical_aliases, key=_sort_key):
            raise ValueError("taxonomy aliases must be canonically sorted")
        canonical_entries.append(
            {"aliases": canonical_aliases, "code": code, "name": name}
        )

    if mapping.entries != tuple(sorted(mapping.entries, key=lambda item: item[0])):
        raise ValueError("taxonomy entries must be canonically sorted by code")
    payload = {
        "entries": canonical_entries,
        "published_at": mapping.published_at.isoformat(),
        "source_url": mapping.source_url,
        "taxonomy_id": mapping.metadata.taxonomy_id,
        "version": mapping.metadata.version,
    }
    expected_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if mapping.canonical_json != expected_json:
        raise ValueError("taxonomy mapping JSON is not canonical")
    try:
        canonical_bytes = mapping.canonical_json.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("taxonomy mapping JSON must be ASCII escaped") from exc
    if hashlib.sha256(canonical_bytes).hexdigest() != mapping.checksum:
        raise ValueError("taxonomy mapping checksum does not match canonical JSON")


def _validate_metadata(metadata: object) -> None:
    _require_exact_record_state(
        metadata,
        RecognizedIndustryTaxonomy,
        "taxonomy metadata",
    )
    for field_name, value in (
        ("taxonomy id", metadata.taxonomy_id),
        ("taxonomy version", metadata.version),
        ("taxonomy code pattern", metadata.expected_code_pattern),
    ):
        if type(value) is not str:
            raise ValueError(f"{field_name} must be an exact string")
        normalized = _normalize_safe_text(value, field_name)
        if normalized != value:
            raise ValueError(f"{field_name} must be canonical")
    if type(metadata.source_aliases) is not tuple or not metadata.source_aliases:
        raise ValueError("taxonomy source aliases must be a non-empty exact tuple")
    normalized_aliases: list[str] = []
    raw_aliases: set[str] = set()
    for alias in metadata.source_aliases:
        if type(alias) is not str:
            raise ValueError("taxonomy source aliases must be exact strings")
        _require_safe_unicode(alias, "taxonomy source alias")
        if _normalize_whitespace(alias) != alias or alias in raw_aliases:
            raise ValueError("taxonomy source aliases must be canonical and unique")
        normalized_aliases.append(alias)
        raw_aliases.add(alias)
    if normalized_aliases != sorted(normalized_aliases, key=_sort_key):
        raise ValueError("taxonomy source aliases must be canonically sorted")
    try:
        re.compile(metadata.expected_code_pattern)
    except re.error as exc:
        raise ValueError("taxonomy code pattern is invalid") from exc


def _validate_validated_distribution(distribution: object) -> None:
    _require_exact_record_state(
        distribution,
        ValidatedIndustryDistribution,
        "validated industry distribution",
    )


def _require_exact_record_state(
    value: object,
    expected_type: type[object],
    name: str,
) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{name} must be an exact record")
    if type(vars(value)) is not dict:
        raise ValueError(f"{name} must have exact record state")
    expected_fields = {field.name for field in fields(expected_type)}
    if set(vars(value)) != expected_fields:
        raise ValueError(f"{name} must have exact record state")


def _is_valid_source_hostname(hostname: str, netloc: str) -> bool:
    bracketed = netloc.startswith("[")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if bracketed:
            return False
        if ":" in hostname or re.fullmatch(r"[0-9.]+", hostname):
            return False
        if len(hostname) > 253 or hostname.startswith(".") or hostname.endswith("."):
            return False
        return all(_DNS_LABEL_PATTERN.fullmatch(label) for label in hostname.split("."))
    if address.version == 6:
        return bracketed and "]" in netloc
    return not bracketed and ":" not in hostname


def _mapping_for_standard(
    normalized_standard: str,
    mappings: Tuple[IndustryTaxonomyMapping, ...],
) -> Optional[IndustryTaxonomyMapping]:
    standard_key = _comparison_key(normalized_standard, "classification standard")
    matches = tuple(
        mapping
        for mapping in mappings
        if standard_key
        in {
            _comparison_key(alias, "taxonomy source alias")
            for alias in mapping.metadata.source_aliases
        }
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _comparison_key(value: str, name: str) -> str:
    return _normalize_safe_text(value, name).casefold()


def _normalize_safe_text(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty")
    _require_safe_unicode(value, name)
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _normalize_whitespace(normalized)
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    _require_safe_unicode(normalized, name)
    return normalized


def _normalize_whitespace(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value).strip()


def _require_safe_unicode(value: str, name: str) -> None:
    if any(_is_unsafe_character(character) for character in value):
        raise ValueError(f"{name} contains unsafe Unicode")


def _is_unsafe_character(character: str) -> bool:
    codepoint = ord(character)
    category = unicodedata.category(character)
    return category in {"Cc", "Cf", "Cs"} or any(
        start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES
    )


def _sort_key(value: str) -> tuple[str, str]:
    return (_comparison_key(value, "taxonomy text"), value)
