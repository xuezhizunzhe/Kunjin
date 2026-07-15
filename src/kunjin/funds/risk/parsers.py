from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple
from xml.etree import ElementTree

from pypdf import PdfReader

from kunjin.funds.html import HEADING_ELEMENTS, IGNORED_ELEMENTS
from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import (
    ACTIVE_NATIVE_PARSER_VERSION,
    ParserProvenance,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCX_ENTRIES,
    MAX_DOCX_UNCOMPRESSED_BYTES,
    MAX_EXCERPT_CHARACTERS,
    MAX_EXTRACTED_CHARACTERS,
    MAX_FACTS,
    MAX_PDF_PAGES,
    OfficialDocumentCandidate,
    RetrievedArtifact,
    validate_safe_https_url,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.funds.risk.legacy_doc import (
    LegacyConversionResult,
    LegacyDocConversionError,
    LegacyDocConverter,
    _validate_normalized_html,
)
from kunjin.funds.risk.models import FactConfidence, canonical_fact_value
from kunjin.funds.risk.report_facts import (
    MAX_REPORT_CELL_CHARACTERS,
    MAX_REPORT_CELLS_PER_ROW,
    MAX_REPORT_ROWS,
    MAX_REPORT_TABLES,
    CurrentReportObservation,
    ReportCell,
    ReportRow,
    ReportTable,
    extract_common_report_observations,
)
from kunjin.funds.risk.reports import (
    ConvertedDocumentContractError,
    TextBlockView,
    has_exact_leading_cover_title,
    report_period_end,
    validate_converted_document_contract,
)

PARSER_VERSION = ACTIVE_NATIVE_PARSER_VERSION
MAX_SECTION_CHARACTERS = 256

_FACT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DATE_ISO_PATTERN = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_DATE_CN_PATTERN = re.compile(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日")
_EFFECTIVE_FROM_LABEL_PATTERN = re.compile(
    r"^(?:生效日期\s*[:：]\s*.+|"
    r"(?:effective from|effective date)(?:\s*[:：]\s*|\s+).+)[.。]?$",
    flags=re.IGNORECASE,
)
_EFFECTIVE_FROM_SENTENCE_PATTERN = re.compile(r"^自\s*.+\s*起生效[.。]?$")
_EFFECTIVE_TO_LABEL_PATTERN = re.compile(
    r"^(?:失效日期|有效期至|effective until|effective to)\s*[:：]\s*.+[.。]?$",
    flags=re.IGNORECASE,
)
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_DOCX_MEDIA_TYPES = frozenset(
    {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_HTML_CHARSETS = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "gb18030": "gb18030",
    "gbk": "gb18030",
    "gb2312": "gb18030",
}
_NON_CONFLICTING_FACT_KINDS = frozenset({"investment_objective"})
_WORDPROCESSINGML_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PACKAGE_RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")


class RiskDocumentParseError(RuntimeError):
    """A redacted deterministic parser failure."""

    def __init__(
        self,
        code: str,
        reason_code: DocumentFailureReason,
        message: str,
    ) -> None:
        if code not in {
            "official_document_parse_failed",
            "official_document_resource_limit",
        }:
            raise ValueError("parser error code is invalid")
        failure = SafeDocumentFailure(code, DocumentFailureStage.PARSER, reason_code)
        failure.validate()
        self.code = code
        self.failure = failure
        super().__init__(message)


def _fail(
    reason_code: DocumentFailureReason,
    message: str = "official fund document parsing failed",
) -> RiskDocumentParseError:
    return RiskDocumentParseError(
        "official_document_parse_failed",
        reason_code,
        message,
    )


def _resource_limit() -> RiskDocumentParseError:
    return RiskDocumentParseError(
        "official_document_resource_limit",
        DocumentFailureReason.RESOURCE_LIMIT,
        "official fund document exceeded a parser resource limit",
    )


def _require_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if type(vars(value)) is not dict or set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


def _validate_public_value(value: object) -> None:
    if value is None or type(value) in {str, bool, int, Decimal}:
        if type(value) is Decimal and not value.is_finite():
            raise ValueError("normalized value Decimal must be finite")
        if type(value) is str and "\x00" in value:
            raise ValueError("normalized value cannot contain NUL")
        return
    if type(value) is tuple:
        for item in value:
            _validate_public_value(item)
        return
    raise ValueError("normalized value must be deeply immutable")


def fact_fingerprint(
    *,
    fact_kind: str,
    normalized_value: object,
    unit: Optional[str],
    page_number: Optional[int],
    section_name: Optional[str],
    source_excerpt: str,
    effective_from: Optional[date],
    effective_to: Optional[date],
    confidence_state: FactConfidence,
) -> str:
    payload = {
        "confidence_state": confidence_state.value,
        "effective_from": effective_from.isoformat() if effective_from else None,
        "effective_to": effective_to.isoformat() if effective_to else None,
        "fact_kind": fact_kind,
        "normalized_value": canonical_fact_value(normalized_value),
        "page_number": page_number,
        "section_name": section_name,
        "source_excerpt": source_excerpt,
        "unit": unit,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parsed_fact_from_current_observation(
    observation: CurrentReportObservation,
    *,
    effective_from: Optional[date],
    effective_to: Optional[date],
) -> ParsedMandateFact:
    """Convert one exact current observation into the persisted fact contract."""

    if type(observation) is not CurrentReportObservation:
        raise ValueError("current report observation must be exact")
    observation.validate()
    if type(effective_from) not in {date, type(None)} or type(effective_to) not in {
        date,
        type(None),
    }:
        raise ValueError("current report effective dates must be exact dates")
    if (
        effective_from is not None
        and effective_to is not None
        and effective_to < effective_from
    ):
        raise ValueError("current report effective end date cannot precede its start")
    fingerprint = fact_fingerprint(
        fact_kind=observation.fact_kind,
        normalized_value=observation.normalized_value,
        unit=observation.unit,
        page_number=observation.page_number,
        section_name=observation.section_name,
        source_excerpt=observation.source_excerpt,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence_state=observation.confidence_state,
    )
    fact = ParsedMandateFact(
        fact_kind=observation.fact_kind,
        normalized_value=observation.normalized_value,
        unit=observation.unit,
        page_number=observation.page_number,
        section_name=observation.section_name,
        source_excerpt=observation.source_excerpt,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence_state=observation.confidence_state,
        fact_fingerprint=fingerprint,
    )
    fact.validate()
    return fact


@dataclass(frozen=True)
class ParsedMandateFact:
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    confidence_state: FactConfidence
    fact_fingerprint: str

    def validate(self) -> None:
        _require_exact_record(self, ParsedMandateFact, "parsed mandate fact")
        if type(self.fact_kind) is not str or not _FACT_KIND_PATTERN.fullmatch(self.fact_kind):
            raise ValueError("fact kind must be a lowercase stable code")
        _validate_public_value(self.normalized_value)
        if self.unit is not None and (
            type(self.unit) is not str
            or not self.unit.strip()
            or len(self.unit) > 64
            or "\x00" in self.unit
        ):
            raise ValueError("unit must be bounded text")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("page number must be positive")
        if self.section_name is not None and (
            type(self.section_name) is not str
            or not self.section_name.strip()
            or len(self.section_name) > MAX_SECTION_CHARACTERS
            or "\x00" in self.section_name
        ):
            raise ValueError("section name must be bounded text")
        if (
            type(self.source_excerpt) is not str
            or not self.source_excerpt.strip()
            or len(self.source_excerpt) > MAX_EXCERPT_CHARACTERS
            or "\x00" in self.source_excerpt
        ):
            raise ValueError("source excerpt must be bounded text")
        if type(self.effective_from) not in {date, type(None)} or type(self.effective_to) not in {
            date,
            type(None),
        }:
            raise ValueError("effective dates must be exact dates")
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_to < self.effective_from
        ):
            raise ValueError("effective end date cannot precede effective start date")
        if type(self.confidence_state) is not FactConfidence:
            raise ValueError("unknown fact confidence")
        if type(self.fact_fingerprint) is not str or not _SHA256_PATTERN.fullmatch(
            self.fact_fingerprint
        ):
            raise ValueError("fact fingerprint must be a lowercase SHA-256 digest")
        expected = fact_fingerprint(
            fact_kind=self.fact_kind,
            normalized_value=self.normalized_value,
            unit=self.unit,
            page_number=self.page_number,
            section_name=self.section_name,
            source_excerpt=self.source_excerpt,
            effective_from=self.effective_from,
            effective_to=self.effective_to,
            confidence_state=self.confidence_state,
        )
        if self.fact_fingerprint != expected:
            raise ValueError("fact fingerprint does not authenticate the parsed fact")


@dataclass(frozen=True)
class ParsedRiskDocument:
    artifact: RetrievedArtifact
    facts: Tuple[ParsedMandateFact, ...]
    warnings: Tuple[str, ...]
    conflicts: Tuple[str, ...]

    def validate(self) -> None:
        _require_exact_record(self, ParsedRiskDocument, "parsed risk document")
        if type(self.artifact) is not RetrievedArtifact:
            raise ValueError("artifact must be an exact RetrievedArtifact")
        if type(self.facts) is not tuple:
            raise ValueError("parsed facts must be an immutable tuple")
        for fact in self.facts:
            if type(fact) is not ParsedMandateFact:
                raise ValueError("parsed facts must use exact ParsedMandateFact records")
            fact.validate()
        for values, label in ((self.warnings, "warnings"), (self.conflicts, "conflicts")):
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be unique and sorted")
            if any(
                type(value) is not str or not _FACT_KIND_PATTERN.fullmatch(value)
                for value in values
            ):
                raise ValueError(f"{label} must contain stable codes")


@dataclass(frozen=True)
class ParsedArtifactResult:
    document: ParsedRiskDocument
    parser_input_sha256: str
    provenance: ParserProvenance

    def validate(self) -> None:
        _require_exact_record(self, ParsedArtifactResult, "parsed artifact result")
        if type(self.document) is not ParsedRiskDocument:
            raise ValueError("parsed artifact document must be exact")
        self.document.validate()
        if type(self.parser_input_sha256) is not str or not _SHA256_PATTERN.fullmatch(
            self.parser_input_sha256
        ):
            raise ValueError("parser input checksum must be a lowercase SHA-256 digest")
        if type(self.provenance) is not ParserProvenance:
            raise ValueError("parser provenance must be exact")
        self.provenance.validate()
        if (
            self.provenance.converter_kind == "none"
            and self.parser_input_sha256 != self.document.artifact.sha256
        ):
            raise ValueError("native parser input must authenticate the original artifact")


@dataclass(frozen=True)
class _TextBlock:
    text: str
    page_number: Optional[int]
    section_name: Optional[str]
    current_observation_eligible: bool = True
    nfc_only: bool = False


class _EvidenceHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[_TextBlock] = []
        self._ignored_tag: Optional[str] = None
        self._ignored_depth = 0
        self._heading_tag: Optional[str] = None
        self._heading_parts: List[str] = []
        self._block_tag: Optional[str] = None
        self._block_parts: List[str] = []
        self._section: Optional[str] = None
        self._section_current_observation_eligible = True

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        del attrs
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth += 1
            return
        if tag in IGNORED_ELEMENTS:
            self._ignored_tag = tag
            self._ignored_depth = 1
            return
        if tag in HEADING_ELEMENTS:
            self._finish_block()
            self._heading_tag = tag
            self._heading_parts = []
            return
        if tag in {"p", "li", "dd", "td", "th"}:
            self._finish_block()
            self._block_tag = tag
            self._block_parts = []
        elif tag == "br" and self._block_tag is not None:
            self._block_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth -= 1
                if self._ignored_depth == 0:
                    self._ignored_tag = None
            return
        if self._heading_tag == tag:
            raw_section = "".join(self._heading_parts)
            section = _normalized_text(raw_section)
            (
                self._section,
                self._section_current_observation_eligible,
            ) = _section_context(raw_section, section)
            self._heading_tag = None
            self._heading_parts = []
        if self._block_tag == tag:
            self._finish_block()

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None:
            return
        if self._heading_tag is not None:
            self._heading_parts.append(data)
        elif self._block_tag is not None:
            self._block_parts.append(data)

    def finish(self) -> None:
        self.close()
        self._finish_block()

    def _finish_block(self) -> None:
        if self._block_tag is not None:
            text = _normalized_text("".join(self._block_parts))
            if text:
                self.blocks.append(
                    _TextBlock(
                        text,
                        None,
                        self._section,
                        self._section_current_observation_eligible,
                    )
                )
        self._block_tag = None
        self._block_parts = []


class _ConvertedEvidenceHtmlParser(HTMLParser):
    _TABLE_HEADER = ("指标", "单位", "数值")
    _TABLE_HEADER_ALIASES = frozenset({"指标", "项目"})
    _CELL_TEXT_WRAPPERS = frozenset(
        {
            "b",
            "em",
            "font",
            "i",
            "p",
            "s",
            "small",
            "span",
            "strong",
            "sub",
            "sup",
            "u",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[_TextBlock] = []
        self.views: List[TextBlockView] = []
        self._ignored_tag: Optional[str] = None
        self._ignored_depth = 0
        self._heading_tag: Optional[str] = None
        self._heading_parts: List[str] = []
        self._title_tag: Optional[str] = None
        self._title_parts: List[str] = []
        self._block_tag: Optional[str] = None
        self._block_parts: List[str] = []
        self._section: Optional[str] = None
        self._section_current_observation_eligible = True
        self._table_depth = 0
        self._table_columns: Optional[Tuple[str, str, str]] = None
        self._row_cells: Optional[List[Tuple[str, str]]] = None
        self._row_ambiguous = False
        self._cell_tag: Optional[str] = None
        self._cell_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth += 1
            return
        if tag in IGNORED_ELEMENTS:
            self._ignored_tag = tag
            self._ignored_depth = 1
            return
        if tag == "title":
            self._finish_block()
            self._title_tag = tag
            self._title_parts = []
            return
        if tag == "table":
            self._finish_block()
            self._table_depth += 1
            if self._table_depth == 1:
                self._table_columns = None
            else:
                self._row_ambiguous = True
            return
        if self._table_depth:
            self._handle_table_start(tag, attrs)
            return
        if tag in HEADING_ELEMENTS:
            self._finish_block()
            self._heading_tag = tag
            self._heading_parts = []
            return
        if tag in {"p", "li", "dd"}:
            self._finish_block()
            self._block_tag = tag
            self._block_parts = []
        elif tag == "br" and self._block_tag is not None:
            self._block_parts.append(" ")

    def _handle_table_start(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        if self._table_depth != 1:
            return
        if self._cell_tag is not None:
            if tag == "br":
                self._cell_parts.append(" ")
            elif tag not in self._CELL_TEXT_WRAPPERS:
                self._row_ambiguous = True
            return
        if tag == "tr":
            if self._row_cells is not None:
                self._row_ambiguous = True
            else:
                self._row_cells = []
                self._row_ambiguous = False
            return
        if tag not in {"td", "th"} or self._row_cells is None:
            return
        if self._cell_tag is not None:
            self._row_ambiguous = True
            return
        attribute_names = [name.casefold() for name, _ in attrs]
        if len(attribute_names) != len(set(attribute_names)):
            self._row_ambiguous = True
        for name, value in attrs:
            if name.casefold() in {"rowspan", "colspan"} and (value or "").strip() != "1":
                self._row_ambiguous = True
        self._cell_tag = tag
        self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth -= 1
                if self._ignored_depth == 0:
                    self._ignored_tag = None
            return
        if self._table_depth:
            self._handle_table_end(tag)
            return
        if self._title_tag == tag:
            title = _normalized_converted_text("".join(self._title_parts))
            if title:
                self.views.append(TextBlockView(title, None, None, False, True))
            self._title_tag = None
            self._title_parts = []
            return
        if self._heading_tag == tag:
            raw_heading = "".join(self._heading_parts)
            heading = _normalized_converted_text(raw_heading)
            if heading:
                (
                    self._section,
                    self._section_current_observation_eligible,
                ) = _section_context(raw_heading, heading)
                self.views.append(TextBlockView(heading, None, self._section, True, False))
            elif _has_unsafe_time_context_character(raw_heading):
                self._section = None
                self._section_current_observation_eligible = False
            self._heading_tag = None
            self._heading_parts = []
        if self._block_tag == tag:
            self._finish_block()

    def _handle_table_end(self, tag: str) -> None:
        if self._table_depth != 1:
            if tag == "table":
                self._table_depth -= 1
            return
        if self._cell_tag == tag:
            text = _normalized_converted_text("".join(self._cell_parts))
            if self._row_cells is not None:
                self._row_cells.append((tag, text))
            self._cell_tag = None
            self._cell_parts = []
            return
        if tag == "tr":
            self._finish_row()
            return
        if tag == "table":
            if self._row_cells is not None:
                self._row_ambiguous = True
                self._finish_row()
            self._table_depth = 0
            self._table_columns = None

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None:
            return
        if self._cell_tag is not None:
            self._cell_parts.append(data)
        elif self._title_tag is not None:
            self._title_parts.append(data)
        elif self._heading_tag is not None:
            self._heading_parts.append(data)
        elif self._block_tag is not None:
            self._block_parts.append(data)

    def finish(self) -> None:
        self.close()
        self._finish_block()

    def _finish_block(self) -> None:
        if self._block_tag is not None:
            text = _normalized_converted_text("".join(self._block_parts))
            if text:
                block = _TextBlock(text, None, self._section, False, True)
                self.blocks.append(block)
                self.views.append(TextBlockView(text, None, self._section, False, False))
        self._block_tag = None
        self._block_parts = []

    def _finish_row(self) -> None:
        cells = self._row_cells or []
        values = tuple(text for _, text in cells)
        tags = tuple(tag for tag, _ in cells)
        if not self._row_ambiguous and len(values) == 2 and all(values):
            if values[0] in {"基金代码", "基金名称"}:
                self._append_bound_block(values[0] + "：" + values[1], False)
        if (
            not self._row_ambiguous
            and len(values) == 3
            and all(values)
            and all(tag == "th" for tag in tags)
            and values[0] in self._TABLE_HEADER_ALIASES
            and values[1:] == self._TABLE_HEADER[1:]
        ):
            self._table_columns = self._TABLE_HEADER
        elif (
            not self._row_ambiguous
            and self._table_columns == self._TABLE_HEADER
            and len(values) == 3
            and all(values)
        ):
            label, unit, value = values
            self._append_bound_block(label + value + unit, True)
        self._row_cells = None
        self._row_ambiguous = False
        self._cell_tag = None
        self._cell_parts = []

    def _append_bound_block(self, text: str, current_observation_eligible: bool) -> None:
        block = _TextBlock(
            text,
            None,
            self._section,
            current_observation_eligible and self._section_current_observation_eligible,
            True,
        )
        self.blocks.append(block)
        self.views.append(TextBlockView(text, None, self._section, False, False))


_TABLE_CELL_WRAPPERS = frozenset(
    {
        "a",
        "b",
        "em",
        "font",
        "i",
        "p",
        "s",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "u",
    }
)
_KNOWN_TABLE_HEADERS = frozenset(
    {
        "代码",
        "占比",
        "名称",
        "单位",
        "序号",
        "指标",
        "数值",
        "比例",
        "项目",
        "证券代码",
        "证券名称",
        "asset",
        "code",
        "indicator",
        "item",
        "name",
        "rank",
        "unit",
        "value",
        "weight",
    }
)


def _row_looks_like_header(values: Tuple[str, ...]) -> bool:
    known_headers = sum(value.casefold() in _KNOWN_TABLE_HEADERS for value in values)
    return len(values) >= 2 and known_headers >= 2


def _report_table_excerpt(rows: Tuple[ReportRow, ...]) -> str:
    excerpt = "；".join(" | ".join(cell.text for cell in row.cells) for row in rows)
    if len(excerpt) <= MAX_EXCERPT_CHARACTERS:
        return excerpt
    return excerpt[: MAX_EXCERPT_CHARACTERS - 3] + "..."


class _StructuredHtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: List[ReportTable] = []
        self._ignored_tag: Optional[str] = None
        self._ignored_depth = 0
        self._heading_tag: Optional[str] = None
        self._heading_parts: List[str] = []
        self._section: Optional[str] = None
        self._section_current_observation_eligible = True
        self._table_depth = 0
        self._table_count = 0
        self._total_rows = 0
        self._table_invalid = False
        self._rows: List[Tuple[Tuple[str, bool], ...]] = []
        self._row: Optional[List[Tuple[str, bool]]] = None
        self._cell_tag: Optional[str] = None
        self._cell_parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth += 1
            return
        if tag in IGNORED_ELEMENTS:
            self._ignored_tag = tag
            self._ignored_depth = 1
            return
        if tag == "table":
            if self._table_depth == 0:
                self._table_count += 1
                if self._table_count > MAX_REPORT_TABLES:
                    raise _resource_limit()
                self._table_invalid = False
                self._rows = []
                self._row = None
                self._cell_tag = None
                self._cell_parts = []
            else:
                self._table_invalid = True
            self._table_depth += 1
            return
        if self._table_depth:
            self._handle_table_start(tag, attrs)
            return
        if tag in HEADING_ELEMENTS:
            self._heading_tag = tag
            self._heading_parts = []

    def _handle_table_start(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        if self._table_depth != 1:
            return
        if self._cell_tag is not None:
            if tag == "br":
                self._cell_parts.append(" ")
            elif tag not in _TABLE_CELL_WRAPPERS:
                self._table_invalid = True
            return
        if tag == "tr":
            if self._row is not None:
                self._table_invalid = True
            else:
                self._row = []
            return
        if tag not in {"td", "th"}:
            return
        if self._row is None or self._cell_tag is not None:
            self._table_invalid = True
            return
        attribute_names = tuple(name.casefold() for name, _ in attrs)
        if len(attribute_names) != len(set(attribute_names)):
            self._table_invalid = True
        for name, value in attrs:
            if name.casefold() in {"rowspan", "colspan"} and (value or "").strip() != "1":
                self._table_invalid = True
        self._cell_tag = tag
        self._cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_depth -= 1
                if self._ignored_depth == 0:
                    self._ignored_tag = None
            return
        if self._table_depth:
            self._handle_table_end(tag)
            return
        if self._heading_tag == tag:
            raw_heading = "".join(self._heading_parts)
            heading = _normalized_text(raw_heading)
            (
                self._section,
                self._section_current_observation_eligible,
            ) = _section_context(raw_heading, heading)
            self._heading_tag = None
            self._heading_parts = []

    def _handle_table_end(self, tag: str) -> None:
        if self._table_depth > 1:
            if tag == "table":
                self._table_depth -= 1
            return
        if self._cell_tag == tag:
            text = _normalized_text("".join(self._cell_parts))
            if len(text) > MAX_REPORT_CELL_CHARACTERS:
                raise _resource_limit()
            if not text or self._row is None:
                self._table_invalid = True
            elif len(self._row) >= MAX_REPORT_CELLS_PER_ROW:
                raise _resource_limit()
            else:
                self._row.append((text, tag == "th"))
            self._cell_tag = None
            self._cell_parts = []
            return
        if tag == "tr":
            self._finish_row()
            return
        if tag == "table":
            if self._cell_tag is not None or self._row is not None:
                self._table_invalid = True
            self._finish_table()
            self._table_depth = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None:
            return
        if self._cell_tag is not None:
            self._cell_parts.append(data)
        elif self._heading_tag is not None and self._table_depth == 0:
            self._heading_parts.append(data)

    def finish(self) -> None:
        self.close()
        if self._table_depth:
            self._table_invalid = True
            self._finish_table()
            self._table_depth = 0

    def _finish_row(self) -> None:
        if self._cell_tag is not None or self._row is None or not self._row:
            self._table_invalid = True
        else:
            self._total_rows += 1
            if self._total_rows > MAX_REPORT_ROWS:
                raise _resource_limit()
            self._rows.append(tuple(self._row))
        self._row = None
        self._cell_tag = None
        self._cell_parts = []

    def _finish_table(self) -> None:
        if (
            not self._table_invalid
            and self._rows
            and self._section_current_observation_eligible
        ):
            rows = []
            for index, raw_row in enumerate(self._rows):
                values = tuple(text for text, _ in raw_row)
                inferred_header = index == 0 and _row_looks_like_header(values)
                rows.append(
                    ReportRow(
                        tuple(
                            ReportCell(text, is_header or inferred_header)
                            for text, is_header in raw_row
                        )
                    )
                )
            table = ReportTable(
                rows=tuple(rows),
                page_number=None,
                section_name=self._section,
                source_excerpt=_report_table_excerpt(tuple(rows)),
            )
            try:
                table.validate()
            except ValueError:
                pass
            else:
                self.tables.append(table)
        self._table_invalid = False
        self._rows = []
        self._row = None
        self._cell_tag = None
        self._cell_parts = []


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalized_converted_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _section_context(raw: str, normalized: str) -> Tuple[Optional[str], bool]:
    unsafe = _has_unsafe_time_context_character(raw)
    if unsafe:
        normalized = " ".join(
            "".join(
                " " if _has_unsafe_time_context_character(character) else character
                for character in normalized
            ).split()
        )
    section = normalized[:MAX_SECTION_CHARACTERS] if normalized else None
    return section, not unsafe


def _normalized_fact_text(block: _TextBlock, value: str) -> str:
    if block.nfc_only:
        return _normalized_converted_text(value)
    return _normalized_text(value)


def _parse_date_match(match: re.Match[str]) -> date:
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise _fail(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID) from exc


def _explicit_date(value: str, *patterns: re.Pattern[str]) -> Optional[date]:
    if not any(pattern.fullmatch(value) for pattern in patterns):
        return None
    matches = tuple(
        match
        for date_pattern in (_DATE_ISO_PATTERN, _DATE_CN_PATTERN)
        for match in date_pattern.finditer(value)
    )
    if len(matches) != 1:
        raise _fail(
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
            "official fund document contains an invalid effective date",
        )
    return _parse_date_match(matches[0])


def _effective_dates(blocks: Tuple[_TextBlock, ...]) -> Tuple[Optional[date], Optional[date]]:
    starts = set()
    ends = set()
    for block in blocks:
        start = _explicit_date(
            block.text,
            _EFFECTIVE_FROM_LABEL_PATTERN,
            _EFFECTIVE_FROM_SENTENCE_PATTERN,
        )
        end = _explicit_date(block.text, _EFFECTIVE_TO_LABEL_PATTERN)
        if start is not None:
            starts.add(start)
        if end is not None:
            ends.add(end)
    if len(starts) > 1 or len(ends) > 1:
        raise _fail(
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
            "official fund document contains conflicting effective dates",
        )
    start = next(iter(starts), None)
    end = next(iter(ends), None)
    if start is not None and end is not None and end < start:
        raise _fail(
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
            "official fund document contains invalid effective dates",
        )
    return start, end


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise _fail(DocumentFailureReason.PARSER_AMBIGUOUS_FACT) from exc
    if not parsed.is_finite() or parsed < 0:
        raise _fail(DocumentFailureReason.PARSER_AMBIGUOUS_FACT)
    return parsed


def _bounded_excerpt(text: str, match: re.Match[str]) -> Tuple[str, bool]:
    if len(text) <= MAX_EXCERPT_CHARACTERS:
        return text, False
    if MAX_EXCERPT_CHARACTERS < 7:
        return text[match.start() : match.start() + MAX_EXCERPT_CHARACTERS], True
    available = MAX_EXCERPT_CHARACTERS - 6
    start = max(0, match.start() - available // 2)
    end = min(len(text), start + available)
    start = max(0, end - available)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else ""), True


def _new_fact(
    block: _TextBlock,
    match: re.Match[str],
    fact_kind: str,
    normalized_value: object,
    unit: Optional[str],
    confidence: FactConfidence,
    effective_from: Optional[date],
    effective_to: Optional[date],
) -> Tuple[ParsedMandateFact, bool]:
    excerpt, truncated = _bounded_excerpt(block.text, match)
    fingerprint = fact_fingerprint(
        fact_kind=fact_kind,
        normalized_value=normalized_value,
        unit=unit,
        page_number=block.page_number,
        section_name=block.section_name,
        source_excerpt=excerpt,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence_state=confidence,
    )
    fact = ParsedMandateFact(
        fact_kind=fact_kind,
        normalized_value=normalized_value,
        unit=unit,
        page_number=block.page_number,
        section_name=block.section_name,
        source_excerpt=excerpt,
        effective_from=effective_from,
        effective_to=effective_to,
        confidence_state=confidence,
        fact_fingerprint=fingerprint,
    )
    fact.validate()
    return fact, truncated


def _literal_patterns() -> Tuple[Tuple[str, str, object, Optional[str], FactConfidence], ...]:
    return (
        (
            r"(?:本基金)?不投资于股票|(?:the fund )?does not invest in stocks?",
            "stock_exposure_max_percent",
            Decimal("0"),
            "percent",
            FactConfidence.EXACT,
        ),
        (
            r"(?:本基金)?不投资于可转换债券|(?:the fund )?does not invest in convertible bonds?",
            "convertible_bond_exposure_max_percent",
            Decimal("0"),
            "percent",
            FactConfidence.EXACT,
        ),
        (
            r"(?:本基金)?不投资于可交换债券|(?:the fund )?does not invest in exchangeable bonds?",
            "exchangeable_bond_exposure_max_percent",
            Decimal("0"),
            "percent",
            FactConfidence.EXACT,
        ),
        (
            r"(?:本基金)?不使用衍生品|(?:the fund )?does not use derivatives?",
            "derivatives_use",
            "absent",
            None,
            FactConfidence.ABSENT,
        ),
        (
            r"不跟踪单一行业或主题|does not track (?:a )?single (?:industry|sector) or theme",
            "sector_theme_mandate",
            "absent",
            None,
            FactConfidence.ABSENT,
        ),
    )


def _extract_facts(
    blocks: Tuple[_TextBlock, ...],
    effective_from: Optional[date],
    effective_to: Optional[date],
    document_kind: DocumentKind,
) -> Tuple[Tuple[ParsedMandateFact, ...], Tuple[str, ...], Tuple[str, ...]]:
    facts: List[ParsedMandateFact] = []
    fact_fingerprints = set()
    warnings = set()
    conflicts = set()

    def add(
        block: _TextBlock,
        match: re.Match[str],
        kind: str,
        value: object,
        unit: Optional[str],
        confidence: FactConfidence = FactConfidence.EXACT,
    ) -> None:
        fact, truncated = _new_fact(
            block,
            match,
            kind,
            value,
            unit,
            confidence,
            effective_from,
            effective_to,
        )
        if truncated:
            warnings.add("source_excerpt_truncated")
        if fact.fact_fingerprint not in fact_fingerprints:
            facts.append(fact)
            fact_fingerprints.add(fact.fact_fingerprint)
        if len(facts) > MAX_FACTS:
            raise _resource_limit()

    if document_kind is DocumentKind.INDEX_METHODOLOGY:
        methodology_block = next(
            (
                block
                for block in blocks
                if re.search(r"跟踪指数|tracked index", block.text, flags=re.IGNORECASE)
            ),
            next(iter(blocks), None),
        )
        if methodology_block is not None:
            methodology_match = re.search(
                r"跟踪指数|tracked index|\S+",
                methodology_block.text,
                flags=re.IGNORECASE,
            )
            if methodology_match is not None:
                add(
                    methodology_block,
                    methodology_match,
                    "index_methodology_present",
                    True,
                    None,
                    FactConfidence.PRESENT,
                )

    for block in blocks:
        legal_type = re.search(
            r"基金类型\s*[:：]\s*(货币市场型|债券型|混合型|股票型|指数增强型|指数型|QDII|基金中基金|商品型|REITs?)(?:基金)?"
            r"|本基金(?:是(?:一只)?|为|属于)(?:契约型开放式)?\s*(货币市场型|债券型|混合型|股票型|指数增强型|指数型|QDII|基金中基金|商品型|REITs?)基金"
            r"|fund type\s*[:：]\s*(money market|bond|mixed|equity|"
            r"index(?:[- ]+enhanced)?|QDII|FOF|commodity|REITs?) fund",
            block.text,
            flags=re.IGNORECASE,
        )
        if legal_type is not None:
            raw_type = re.sub(
                r"[-\s]+",
                " ",
                (legal_type.group(1) or legal_type.group(2) or legal_type.group(3)).casefold(),
            )
            type_map = {
                "货币市场型": "money_market_fund",
                "债券型": "bond_fund",
                "混合型": "mixed_fund",
                "股票型": "equity_fund",
                "指数型": "index_fund",
                "指数增强型": "index_enhanced_fund",
                "qdii": "qdii_fund",
                "基金中基金": "fof",
                "商品型": "commodity_fund",
                "reits": "public_reit",
                "reit": "public_reit",
                "money market": "money_market_fund",
                "bond": "bond_fund",
                "mixed": "mixed_fund",
                "equity": "equity_fund",
                "index": "index_fund",
                "index enhanced": "index_enhanced_fund",
                "fof": "fof",
                "commodity": "commodity_fund",
            }
            normalized_type = type_map.get(raw_type)
            if normalized_type is not None:
                add(block, legal_type, "legal_product_type", normalized_type, None)

        if block.section_name in {"投资目标", "INVESTMENT OBJECTIVE"}:
            objective = re.search(r"\S.*", block.text)
            if objective is not None:
                add(block, objective, "investment_objective", block.text, None)
        else:
            objective = re.search(
                r"投资目标\s*[:：]\s*(.+)|investment objective\s*[:：]\s*(.+)",
                block.text,
                flags=re.IGNORECASE,
            )
            if objective is not None:
                add(
                    block,
                    objective,
                    "investment_objective",
                    _normalized_fact_text(block, objective.group(1) or objective.group(2)),
                    None,
                )

        benchmark = re.search(
            r"业绩比较基准\s*[:：]\s*([^。;；]+)|performance benchmark\s*[:：]\s*([^.;]+)",
            block.text,
            flags=re.IGNORECASE,
        )
        if benchmark is not None:
            add(
                block,
                benchmark,
                "benchmark_name",
                _normalized_fact_text(block, benchmark.group(1) or benchmark.group(2)),
                None,
            )

        tracking_objective = re.search(
            r"跟踪目标\s*[:：]\s*([^。;；]+)|tracking objective\s*[:：]\s*([^.;]+)",
            block.text,
            flags=re.IGNORECASE,
        )
        if tracking_objective is not None:
            add(
                block,
                tracking_objective,
                "tracking_objective",
                _normalized_fact_text(
                    block,
                    tracking_objective.group(1) or tracking_objective.group(2),
                ),
                None,
            )

        for expression, kind, value, unit, confidence in _literal_patterns():
            match = re.search(expression, block.text, flags=re.IGNORECASE)
            if match is not None:
                add(block, match, kind, value, unit, confidence)

        range_match = re.search(
            r"股票资产(?:[（(][^）)]{1,64}[）)])?占基金资产的(?:比例)?(?:为|是|:)?\s*(\d+(?:\.\d+)?)%\s*[-~至]\s*(\d+(?:\.\d+)?)%"
            r"|stocks? (?:represent|account for)\s*(\d+(?:\.\d+)?)%\s*[-~to]+\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if range_match is not None:
            lower = _decimal(range_match.group(1) or range_match.group(3))
            upper = _decimal(range_match.group(2) or range_match.group(4))
            if lower > upper or upper > 100:
                raise _fail(
                    DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                    "official fund document contains an invalid exposure range",
                )
            add(
                block,
                range_match,
                "stock_exposure_min_percent",
                lower,
                "percent",
                FactConfidence.BOUNDED_RANGE,
            )
            add(
                block,
                range_match,
                "stock_exposure_max_percent",
                upper,
                "percent",
                FactConfidence.BOUNDED_RANGE,
            )

        stock_max = re.search(
            r"股票资产(?:[（(][^）)]{1,64}[）)])?占基金资产的(?:比例)?(?:不超过|最高为?)\s*(\d+(?:\.\d+)?)%"
            r"|stock exposure (?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if stock_max is not None:
            add(
                block,
                stock_max,
                "stock_exposure_max_percent",
                _decimal(stock_max.group(1) or stock_max.group(2)),
                "percent",
            )

        exposure_patterns = (
            ("bond", r"债券资产占基金资产的比例"),
            ("cash", r"现金资产占基金资产的比例"),
            ("fund", r"投资于其他基金的比例"),
            ("derivative", r"衍生品投资比例"),
            ("domestic", r"境内证券投资比例"),
            ("hong_kong", r"港股通标的股票投资比例"),
            ("overseas", r"境外证券投资比例"),
        )
        for prefix, expression in exposure_patterns:
            range_value = re.search(
                expression + r"(?:为|是|:)?\s*(\d+(?:\.\d+)?)%\s*[-~至]\s*(\d+(?:\.\d+)?)%",
                block.text,
                flags=re.IGNORECASE,
            )
            if range_value is not None:
                lower = _decimal(range_value.group(1))
                upper = _decimal(range_value.group(2))
                if lower > upper or upper > 100:
                    raise _fail(
                        DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                        "official fund document contains an invalid exposure range",
                    )
                add(
                    block,
                    range_value,
                    prefix + "_exposure_min_percent",
                    lower,
                    "percent",
                    FactConfidence.BOUNDED_RANGE,
                )
                add(
                    block,
                    range_value,
                    prefix + "_exposure_max_percent",
                    upper,
                    "percent",
                    FactConfidence.BOUNDED_RANGE,
                )
                continue
            minimum = re.search(
                expression + r"不低于(?:基金资产的)?\s*(\d+(?:\.\d+)?)%",
                block.text,
                flags=re.IGNORECASE,
            )
            if minimum is not None:
                value = _decimal(minimum.group(1))
                if value > 100:
                    raise _fail(
                        DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                        "official fund document contains an invalid exposure bound",
                    )
                add(block, minimum, prefix + "_exposure_min_percent", value, "percent")
                continue
            maximum = re.search(
                expression + r"不超过(?:基金资产的)?\s*(\d+(?:\.\d+)?)%",
                block.text,
                flags=re.IGNORECASE,
            )
            if maximum is not None:
                value = _decimal(maximum.group(1))
                if value > 100:
                    raise _fail(
                        DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                        "official fund document contains an invalid exposure bound",
                    )
                add(block, maximum, prefix + "_exposure_max_percent", value, "percent")
                continue
            exact = re.search(
                expression + r"为\s*(\d+(?:\.\d+)?)%",
                block.text,
                flags=re.IGNORECASE,
            )
            if exact is not None:
                value = _decimal(exact.group(1))
                if value > 100:
                    raise _fail(
                        DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                        "official fund document contains an invalid exposure bound",
                    )
                add(block, exact, prefix + "_exposure_min_percent", value, "percent")
                add(block, exact, prefix + "_exposure_max_percent", value, "percent")

        duration = re.search(
            r"(?:组合)?有效久期不超过\s*(\d+(?:\.\d+)?)\s*年"
            r"|effective duration (?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)\s*years?",
            block.text,
            flags=re.IGNORECASE,
        )
        if duration is not None:
            add(
                block,
                duration,
                "effective_duration_max",
                _decimal(duration.group(1) or duration.group(2)),
                "years",
            )

        maturity = re.search(
            r"(?:投资)?组合平均剩余期限不超过\s*(\d+(?:\.\d+)?)\s*天"
            r"|weighted average maturity (?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)\s*days?",
            block.text,
            flags=re.IGNORECASE,
        )
        if maturity is not None:
            add(
                block,
                maturity,
                "weighted_average_maturity_max",
                _decimal(maturity.group(1) or maturity.group(2)),
                "days",
            )

        high_grade = re.search(
            r"(?:国债、政策性金融债、现金、银行存款及AAA级债券合计|"
            r"sovereign, policy bank, cash, deposits? and AAA bonds? combined)"
            r"(?:不低于|(?:are )?at least)\s*(?:基金资产的)?\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if high_grade is not None:
            add(
                block,
                high_grade,
                "high_quality_fixed_income_min_percent",
                _decimal(high_grade.group(1)),
                "percent",
            )

        below_rating = re.search(
            r"AA\+级以下债券投资比例(?:为|不超过)\s*(\d+(?:\.\d+)?)%"
            r"|below AA\+ bond exposure (?:is|does not exceed)\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if below_rating is not None:
            add(
                block,
                below_rating,
                "below_aa_plus_exposure_max_percent",
                _decimal(below_rating.group(1) or below_rating.group(2)),
                "percent",
            )

        unrated = re.search(
            r"非主权无评级债券投资比例(?:为|不超过)\s*(\d+(?:\.\d+)?)%"
            r"|unrated non-sovereign bond exposure (?:is|does not exceed)\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if unrated is not None:
            add(
                block,
                unrated,
                "unrated_non_sovereign_exposure_max_percent",
                _decimal(unrated.group(1) or unrated.group(2)),
                "percent",
            )

        leverage = re.search(
            r"基金总资产不得超过基金净资产的\s*(\d+(?:\.\d+)?)%"
            r"|gross assets? (?:shall|must) not exceed\s*(\d+(?:\.\d+)?)% of net assets?",
            block.text,
            flags=re.IGNORECASE,
        )
        if leverage is not None:
            add(
                block,
                leverage,
                "gross_leverage_max_percent",
                _decimal(leverage.group(1) or leverage.group(2)),
                "percent",
            )

        repo = re.search(
            r"债券逆回购投资比例不超过(?:基金资产的)?\s*(\d+(?:\.\d+)?)%"
            r"|reverse repo exposure (?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if repo is not None:
            add(
                block,
                repo,
                "repo_exposure_max_percent",
                _decimal(repo.group(1) or repo.group(2)),
                "percent",
            )

        issuer = re.search(
            r"单一非主权发行人证券不超过基金资产的\s*(\d+(?:\.\d+)?)%"
            r"|single non-sovereign issuer (?:exposure )?(?:shall|must) not exceed"
            r"\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if issuer is not None:
            add(
                block,
                issuer,
                "single_non_sovereign_issuer_max_percent",
                _decimal(issuer.group(1) or issuer.group(2)),
                "percent",
            )

        tracked_index = re.search(
            r"跟踪指数\s*[:：]\s*([^。;；]+)|tracked index\s*[:：]\s*([^.;]+)",
            block.text,
            flags=re.IGNORECASE,
        )
        if tracked_index is not None:
            add(
                block,
                tracked_index,
                "tracked_index_name",
                _normalized_fact_text(block, tracked_index.group(1) or tracked_index.group(2)),
                None,
            )

        count = re.search(
            r"指数样本数量为\s*(\d+)\s*只|index (?:has|contains)\s*(\d+)\s*constituents?",
            block.text,
            flags=re.IGNORECASE,
        )
        if count is not None:
            add(block, count, "constituent_count", int(count.group(1) or count.group(2)), "count")

        for expression, kind in (
            (
                r"单一最大样本权重不超过\s*(\d+(?:\.\d+)?)%|largest constituent weight "
                r"(?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)%",
                "largest_constituent_weight_max_percent",
            ),
            (
                r"前十大样本权重合计不超过\s*(\d+(?:\.\d+)?)%|top ten constituent weight "
                r"(?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)%",
                "top_ten_constituent_weight_max_percent",
            ),
            (
                r"单一最大行业权重不超过\s*(\d+(?:\.\d+)?)%|largest industry weight "
                r"(?:does not exceed|is at most)\s*(\d+(?:\.\d+)?)%",
                "largest_industry_weight_max_percent",
            ),
        ):
            match = re.search(expression, block.text, flags=re.IGNORECASE)
            if match is not None:
                add(
                    block,
                    match,
                    kind,
                    _decimal(match.group(1) or match.group(2)),
                    "percent",
                )

        industries = re.search(
            r"指数覆盖行业数量不少于\s*(\d+)\s*个|index covers at least\s*(\d+)\s*industries",
            block.text,
            flags=re.IGNORECASE,
        )
        if industries is not None:
            add(
                block,
                industries,
                "industry_count_min",
                int(industries.group(1) or industries.group(2)),
                "count",
            )

        theme = re.search(
            r"投资于[^。;；]*主题证券的比例不低于(?:非现金基金资产的)?\s*(\d+(?:\.\d+)?)%"
            r"|theme securities (?:represent|account for) at least\s*(\d+(?:\.\d+)?)%",
            block.text,
            flags=re.IGNORECASE,
        )
        if theme is not None:
            add(
                block,
                theme,
                "theme_exposure_min_percent",
                _decimal(theme.group(1) or theme.group(2)),
                "percent_of_non_cash_assets",
            )

        explicit_industry_scope = re.search(
            r"(?:公司所处行业|主营业务)属于[^。；;]{1,120}(?:行业|产业)",
            block.text,
            flags=re.IGNORECASE,
        )
        if explicit_industry_scope is not None:
            add(
                block,
                explicit_industry_scope,
                "sector_theme_mandate",
                "present",
                None,
                FactConfidence.PRESENT,
            )

        liquid_assets = re.search(
            r"保持不低于基金资产净值\s*(\d+(?:\.\d+)?)%的现金或者到期日在一年以内的政府债券"
            r"|maintains? at least\s*(\d+(?:\.\d+)?)% in cash or government bonds "
            r"maturing within one year",
            block.text,
            flags=re.IGNORECASE,
        )
        if liquid_assets is not None:
            add(
                block,
                liquid_assets,
                "minimum_liquid_assets_percent",
                _decimal(liquid_assets.group(1) or liquid_assets.group(2)),
                "percent_of_net_assets",
            )

        daily_redemption = re.search(
            r"每日开放赎回|open for redemption daily",
            block.text,
            flags=re.IGNORECASE,
        )
        if daily_redemption is not None:
            add(
                block,
                daily_redemption,
                "redemption_restriction",
                "daily_open",
                None,
                FactConfidence.PRESENT,
            )
        no_lockup = re.search(r"无锁定期|no lock-?up period", block.text, flags=re.IGNORECASE)
        if no_lockup is not None:
            add(
                block,
                no_lockup,
                "lockup_restriction",
                "absent",
                None,
                FactConfidence.ABSENT,
            )

    legal_types = {
        fact.normalized_value for fact in facts if fact.fact_kind == "legal_product_type"
    }
    specific_index_types = legal_types & {"index_fund", "index_enhanced_fund"}
    if len(specific_index_types) == 1 and legal_types <= specific_index_types | {"equity_fund"}:
        for index, fact in enumerate(facts):
            if fact.fact_kind != "legal_product_type" or fact.normalized_value != "equity_fund":
                continue
            asset_class = replace(fact, fact_kind="legal_asset_class")
            asset_class = replace(
                asset_class,
                fact_fingerprint=fact_fingerprint(
                    fact_kind=asset_class.fact_kind,
                    normalized_value=asset_class.normalized_value,
                    unit=asset_class.unit,
                    page_number=asset_class.page_number,
                    section_name=asset_class.section_name,
                    source_excerpt=asset_class.source_excerpt,
                    effective_from=asset_class.effective_from,
                    effective_to=asset_class.effective_to,
                    confidence_state=asset_class.confidence_state,
                ),
            )
            asset_class.validate()
            facts[index] = asset_class

    grouped = {}
    for fact in facts:
        grouped.setdefault(fact.fact_kind, []).append(fact)
    for same_kind in grouped.values():
        if same_kind[0].fact_kind in _NON_CONFLICTING_FACT_KINDS:
            continue
        values = {
            json.dumps(
                canonical_fact_value(fact.normalized_value),
                ensure_ascii=False,
                sort_keys=True,
            )
            for fact in same_kind
        }
        if len(values) <= 1:
            continue
        conflicts.add("duplicate_conflicting_clause")
        for index, fact in enumerate(facts):
            if fact.fact_kind != same_kind[0].fact_kind:
                continue
            ambiguous = replace(fact, confidence_state=FactConfidence.AMBIGUOUS)
            ambiguous = replace(
                ambiguous,
                fact_fingerprint=fact_fingerprint(
                    fact_kind=ambiguous.fact_kind,
                    normalized_value=ambiguous.normalized_value,
                    unit=ambiguous.unit,
                    page_number=ambiguous.page_number,
                    section_name=ambiguous.section_name,
                    source_excerpt=ambiguous.source_excerpt,
                    effective_from=ambiguous.effective_from,
                    effective_to=ambiguous.effective_to,
                    confidence_state=ambiguous.confidence_state,
                ),
            )
            ambiguous.validate()
            facts[index] = ambiguous

    return tuple(facts), tuple(sorted(warnings)), tuple(sorted(conflicts))


def _read_artifact(artifact: RetrievedArtifact) -> bytes:
    try:
        _require_exact_record(artifact, RetrievedArtifact, "retrieved artifact")
    except (TypeError, ValueError) as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    if type(artifact.candidate) is not OfficialDocumentCandidate:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    try:
        artifact.candidate.validate()
    except ValueError as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    if not isinstance(artifact.managed_path, Path):
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    if (
        type(artifact.final_url) is not str
        or type(artifact.content_type) is not str
        or not artifact.content_type.strip()
        or type(artifact.byte_size) is not int
        or artifact.byte_size <= 0
        or type(artifact.sha256) is not str
        or not _SHA256_PATTERN.fullmatch(artifact.sha256)
        or len(artifact.content_type) > 256
        or "\x00" in artifact.content_type
    ):
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    try:
        validate_safe_https_url(artifact.final_url)
    except ValueError as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    if type(artifact.retrieved_at) is not datetime or (
        artifact.retrieved_at.tzinfo is None or artifact.retrieved_at.utcoffset() is None
    ):
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    if artifact.byte_size > MAX_DOCUMENT_BYTES:
        raise _resource_limit()
    try:
        descriptor = os.open(artifact.managed_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != artifact.byte_size:
                raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
            raw = stream.read(artifact.byte_size + 1)
    except (OSError, ValueError) as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    if len(raw) != artifact.byte_size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official fund document failed integrity verification",
        )
    return raw


def artifact_uses_legacy_ole_container(artifact: RetrievedArtifact) -> bool:
    """Authenticate an artifact and report whether its exact container is legacy OLE."""

    return _read_artifact(artifact).startswith(_OLE_COMPOUND_FILE_SIGNATURE)


def _parse_content_type(content_type: str) -> Tuple[str, Optional[str]]:
    message = Message()
    message["content-type"] = content_type
    media_type = message.get_content_type().casefold()
    charset = message.get_content_charset()
    return media_type, charset.casefold() if charset else None


def _html_content(
    raw: bytes,
    declared_charset: Optional[str],
) -> Tuple[Tuple[_TextBlock, ...], Tuple[ReportTable, ...]]:
    charset = _HTML_CHARSETS.get(declared_charset or "utf-8")
    if charset is None:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official HTML declares an unsupported charset",
        )
    try:
        text = raw.decode(charset, errors="strict")
        parser = _EvidenceHtmlParser()
        parser.feed(text)
        parser.finish()
        table_parser = _StructuredHtmlTableParser()
        table_parser.feed(text)
        table_parser.finish()
    except (UnicodeError, ValueError) as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    blocks = tuple(parser.blocks)
    if sum(len(block.text) for block in blocks) > MAX_EXTRACTED_CHARACTERS:
        raise _resource_limit()
    return blocks, tuple(table_parser.tables)


def _html_blocks(raw: bytes, declared_charset: Optional[str]) -> Tuple[_TextBlock, ...]:
    return _html_content(raw, declared_charset)[0]


def _converted_html_content(
    raw: bytes,
) -> Tuple[Tuple[_TextBlock, ...], Tuple[TextBlockView, ...], Tuple[ReportTable, ...]]:
    try:
        text = raw.decode("utf-8", errors="strict")
        parser = _ConvertedEvidenceHtmlParser()
        parser.feed(text)
        parser.finish()
        table_parser = _StructuredHtmlTableParser()
        table_parser.feed(text)
        table_parser.finish()
    except (UnicodeError, ValueError) as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    blocks = tuple(parser.blocks)
    views = tuple(parser.views)
    if (
        not blocks
        or not views
        or sum(len(block.text) for block in blocks) > MAX_EXTRACTED_CHARACTERS
        or sum(len(block.text) for block in views) > MAX_EXTRACTED_CHARACTERS
    ):
        if blocks and views:
            raise _resource_limit()
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    return blocks, views, tuple(table_parser.tables)


def _converted_html_blocks(raw: bytes) -> Tuple[Tuple[_TextBlock, ...], Tuple[TextBlockView, ...]]:
    blocks, views, _ = _converted_html_content(raw)
    return blocks, views


def _safe_xml_root(payload: bytes) -> ElementTree.Element:
    folded = payload.lower()
    if b"<!doctype" in folded or b"<!entity" in folded or b"\x00" in payload:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX contains unsupported XML declarations",
        )
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX contains malformed XML",
        ) from exc


def _docx_member_names(archive: zipfile.ZipFile) -> Tuple[str, ...]:
    infos = archive.infolist()
    if len(infos) > MAX_DOCX_ENTRIES:
        raise _resource_limit()
    names = tuple(info.filename for info in infos)
    if len(names) != len(set(names)):
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX contains duplicate members",
        )
    total_uncompressed = 0
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or name.startswith("/")
            or ".." in path.parts
            or info.flag_bits & 0x1
        ):
            raise _fail(
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official DOCX contains an unsafe member",
            )
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise _fail(
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official DOCX uses an unsupported compression method",
            )
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
            raise _resource_limit()
        folded = name.casefold()
        if (
            folded.endswith("vbaproject.bin")
            or "/embeddings/" in "/" + folded
            or "/activex/" in "/" + folded
        ):
            raise _fail(
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official DOCX contains active or embedded content",
            )
    return names


def _docx_read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX member could not be read",
        ) from exc


def _docx_validate_content_types(archive: zipfile.ZipFile) -> None:
    root = _safe_xml_root(_docx_read_member(archive, "[Content_Types].xml"))
    main_types = {
        element.attrib.get("ContentType")
        for element in root
        if element.tag.rsplit("}", 1)[-1] == "Override"
        and element.attrib.get("PartName") == "/word/document.xml"
    }
    if main_types != {_WORDPROCESSINGML_MAIN_CONTENT_TYPE}:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX content type is invalid",
        )
    if any("macroenabled" in (value or "").casefold() for value in main_types):
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX contains macros",
        )


def _docx_reject_external_relationships(
    archive: zipfile.ZipFile,
    names: Tuple[str, ...],
) -> None:
    relationship_tag = "{" + _PACKAGE_RELATIONSHIP_NAMESPACE + "}Relationship"
    for name in names:
        if not name.casefold().endswith(".rels"):
            continue
        root = _safe_xml_root(_docx_read_member(archive, name))
        for relationship in root.iter(relationship_tag):
            if relationship.attrib.get("TargetMode", "").casefold() == "external":
                raise _fail(
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                    "official DOCX contains external relationships",
                )


def _docx_paragraph_raw_text(paragraph: ElementTree.Element) -> str:
    parts = []
    for element in paragraph.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name == "t" and element.text:
            parts.append(element.text)
        elif local_name in {"tab", "br", "cr"}:
            parts.append(" ")
    return "".join(parts)


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    return _normalized_text(_docx_paragraph_raw_text(paragraph))


def _docx_is_heading(paragraph: ElementTree.Element, text: str) -> bool:
    style = paragraph.find("./{" + _WORD_NAMESPACE + "}pPr/{" + _WORD_NAMESPACE + "}pStyle")
    style_name = "" if style is None else style.attrib.get("{" + _WORD_NAMESPACE + "}val", "")
    folded_style = unicodedata.normalize("NFKC", style_name).casefold()
    return (
        folded_style.startswith("heading")
        or folded_style.startswith("标题")
        or _looks_like_heading(text)
    )


def _docx_report_table(
    table_element: ElementTree.Element,
    section: Optional[str],
) -> Optional[ReportTable]:
    word = "{" + _WORD_NAMESPACE + "}"
    if table_element.findall(".//" + word + "tbl"):
        return None
    raw_rows = []
    for row_index, row in enumerate(table_element.findall("./" + word + "tr")):
        cells = []
        for cell in row.findall("./" + word + "tc"):
            grid_span = cell.find("./" + word + "tcPr/" + word + "gridSpan")
            horizontal_merge = cell.find("./" + word + "tcPr/" + word + "hMerge")
            vertical_merge = cell.find("./" + word + "tcPr/" + word + "vMerge")
            if horizontal_merge is not None or vertical_merge is not None or (
                grid_span is not None
                and grid_span.attrib.get(word + "val", "").strip() != "1"
            ):
                return None
            text = _normalized_text(
                " ".join(
                    _docx_paragraph_text(paragraph)
                    for paragraph in cell.findall(".//" + word + "p")
                )
            )
            if not text:
                return None
            if len(text) > MAX_REPORT_CELL_CHARACTERS:
                raise _resource_limit()
            if len(cells) >= MAX_REPORT_CELLS_PER_ROW:
                raise _resource_limit()
            cells.append(text)
        if not cells:
            return None
        explicit_header = row.find("./" + word + "trPr/" + word + "tblHeader") is not None
        inferred_header = row_index == 0 and _row_looks_like_header(tuple(cells))
        raw_rows.append(
            ReportRow(
                tuple(
                    ReportCell(text, explicit_header or inferred_header)
                    for text in cells
                )
            )
        )
    if not raw_rows:
        return None
    rows = tuple(raw_rows)
    table = ReportTable(
        rows=rows,
        page_number=None,
        section_name=section,
        source_excerpt=_report_table_excerpt(rows),
    )
    try:
        table.validate()
    except ValueError:
        return None
    return table


def _docx_content(raw: bytes) -> Tuple[Tuple[_TextBlock, ...], Tuple[ReportTable, ...]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = _docx_member_names(archive)
            required = {"[Content_Types].xml", "word/document.xml"}
            if not required.issubset(names):
                raise _fail(
                    DocumentFailureReason.PARSER_FORMAT_INVALID,
                    "official DOCX is missing required members",
                )
            _docx_validate_content_types(archive)
            _docx_reject_external_relationships(archive, names)
            document = _safe_xml_root(_docx_read_member(archive, "word/document.xml"))
    except RiskDocumentParseError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX is invalid",
        ) from exc

    word = "{" + _WORD_NAMESPACE + "}"
    body = document.find(word + "body")
    if body is None:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX has no document body",
        )
    blocks = []
    tables = []
    section = None
    section_current_observation_eligible = True
    total_characters = 0
    total_table_rows = 0
    table_count = 0
    for child in body:
        local_name = child.tag.rsplit("}", 1)[-1]
        texts = []
        if local_name == "p":
            raw_text = _docx_paragraph_raw_text(child)
            text = _normalized_text(raw_text)
            if text and _docx_is_heading(child, text):
                section, section_current_observation_eligible = _section_context(
                    raw_text, text
                )
                continue
            if text:
                texts.append(text)
        elif local_name == "tbl":
            table_count += 1
            if table_count > MAX_REPORT_TABLES:
                raise _resource_limit()
            candidate_row_count = len(child.findall("./" + word + "tr"))
            if candidate_row_count > MAX_REPORT_ROWS - total_table_rows:
                raise _resource_limit()
            total_table_rows += candidate_row_count
            table = (
                _docx_report_table(child, section)
                if section_current_observation_eligible
                else None
            )
            if table is not None:
                tables.append(table)
            for row in child.findall(".//" + word + "tr"):
                cells = []
                for cell in row.findall("./" + word + "tc"):
                    cell_text = _normalized_text(
                        " ".join(
                            _docx_paragraph_text(paragraph)
                            for paragraph in cell.findall(".//" + word + "p")
                        )
                    )
                    if cell_text:
                        cells.append(cell_text)
                if cells:
                    if len(cells) % 2 == 0:
                        texts.extend(
                            key + "：" + value for key, value in zip(cells[::2], cells[1::2])
                        )
                    else:
                        texts.append("；".join(cells))
        for text in texts:
            total_characters += len(text)
            if total_characters > MAX_EXTRACTED_CHARACTERS:
                raise _resource_limit()
            blocks.append(
                _TextBlock(
                    text,
                    None,
                    section,
                    section_current_observation_eligible,
                )
            )
    if not blocks:
        raise _fail(
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "official DOCX has no reliable extractable text",
        )
    return tuple(blocks), tuple(tables)


def _docx_blocks(raw: bytes) -> Tuple[_TextBlock, ...]:
    return _docx_content(raw)[0]


def _legal_name_from_document_title(title: str) -> Optional[str]:
    normalized = _normalized_text(title)
    return _legal_name_from_normalized_document_title(normalized)


def _converted_legal_name_from_document_title(title: str) -> Optional[str]:
    normalized = _normalized_converted_text(title)
    return _legal_name_from_normalized_document_title(normalized)


def _legal_name_from_normalized_document_title(normalized: str) -> Optional[str]:
    suffix_patterns = (
        r"基金产品资料概要.*$",
        r"产品资料概要.*$",
        r"招募说明书.*$",
        r"基金合同.*$",
        r"\d{4}年(?:半年度|中期|年度)报告.*$",
        r"\d{4}年第?[一二三四1-4]季度报告.*$",
    )
    for pattern in suffix_patterns:
        match = re.search(pattern, normalized)
        if match is None or match.start() <= 0:
            continue
        legal_name = normalized[: match.start()].strip()
        if len(legal_name) >= 8 and re.search(r"基金(?:\(LOF\))?$", legal_name, re.IGNORECASE):
            return legal_name
    return None


def _validate_document_fund_identity(
    blocks: Tuple[_TextBlock, ...],
    candidate: OfficialDocumentCandidate,
) -> None:
    codes = {
        match.group(1)
        for block in blocks
        for match in re.finditer(r"基金代码\s*[:：]\s*(\d{6})(?!\d)", block.text)
    }
    if codes:
        if candidate.fund_code in codes:
            return
        raise _fail(
            DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
            "official DOCX fund identity does not match the requested fund",
        )
    legal_name = _legal_name_from_document_title(candidate.title)
    if legal_name is not None and any(block.text == legal_name for block in blocks):
        return
    raise _fail(
        DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
        "official DOCX fund identity does not match the requested fund",
    )


def _validate_converted_fund_identity(
    blocks: Tuple[TextBlockView, ...],
    candidate: OfficialDocumentCandidate,
) -> None:
    has_trusted_fund_contract_cover = (
        candidate.document_kind is DocumentKind.FUND_CONTRACT
        and has_exact_leading_cover_title(blocks, candidate.title)
    )
    codes = {
        match.group(1)
        for block in blocks
        if not _is_target_fund_identity_context(block)
        for match in re.finditer(r"基金代码\s*[:：]\s*([0-9]{6})(?![0-9])", block.text)
    }
    candidate_name = _converted_legal_name_from_document_title(candidate.title)
    explicit_names = set()
    for block in blocks:
        if _is_target_fund_identity_context(block):
            continue
        if block.is_title and has_trusted_fund_contract_cover:
            continue
        name_match = re.fullmatch(r"基金名称\s*[:：]\s*(.+)", block.text)
        if name_match is not None:
            explicit_names.add(_normalized_converted_text(name_match.group(1)))
        if block.is_heading or block.is_title:
            heading_name = _converted_legal_name_from_document_title(block.text)
            if heading_name is not None:
                explicit_names.add(heading_name)

    if codes and codes != {candidate.fund_code}:
        raise _fail(
            DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
            "converted official document fund identity does not match",
        )
    if explicit_names and (candidate_name is None or explicit_names != {candidate_name}):
        raise _fail(
            DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
            "converted official document fund identity does not match",
        )
    if codes == {candidate.fund_code} or (
        candidate_name is not None and explicit_names == {candidate_name}
    ) or has_exact_leading_cover_title(blocks, candidate.title):
        return
    raise _fail(
        DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
        "converted official document fund identity does not match",
    )


def _is_target_fund_identity_context(block: TextBlockView) -> bool:
    if block.section_name is None:
        return False
    section = _normalized_converted_text(block.section_name)
    return (
        "目标基金" in section
        or re.search(r"\btarget fund\b", section, re.IGNORECASE) is not None
    )


def _pdf_has_active_or_embedded_content(reader: PdfReader) -> bool:
    try:
        root = reader.trailer["/Root"].get_object()
        if any(root.get(key) is not None for key in ("/OpenAction", "/AA", "/AcroForm")):
            return True
        names = root.get("/Names")
        if names is not None:
            names = names.get_object()
            if any(names.get(key) is not None for key in ("/EmbeddedFiles", "/JavaScript")):
                return True
        if root.get("/Collection") is not None:
            return True
        for page in reader.pages:
            if page.get("/AA") is not None:
                return True
            annotations = page.get("/Annots")
            if annotations is None:
                continue
            for reference in annotations.get_object():
                annotation = reference.get_object()
                if annotation.get("/A") is not None or annotation.get("/AA") is not None:
                    return True
                if annotation.get("/Subtype") in {
                    "/FileAttachment",
                    "/RichMedia",
                    "/Screen",
                    "/Widget",
                }:
                    return True
        return False
    except Exception as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc


def _looks_like_heading(value: str) -> bool:
    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > MAX_SECTION_CHARACTERS
        or any(character.isdigit() for character in stripped)
    ):
        return False
    if stripped in {
        "投资范围",
        "投资目标",
        "基金类型",
        "指数目标",
        "样本与权重限制",
    }:
        return True
    letters = [character for character in stripped if character.isalpha()]
    return bool(letters) and all(character.isupper() for character in letters)


_PDF_LEGAL_SECTION_HEADINGS = frozenset(
    {
        "投资范围",
        "投资目标",
        "基金类型",
        "指数目标",
        "样本与权重限制",
        "INVESTMENT SCOPE",
    }
)
_PDF_CURRENT_SECTION_HEADINGS = frozenset({"CURRENT ASSET ALLOCATION"})


def _pdf_blocks(
    raw: bytes,
    *,
    current_observation_eligible_by_default: bool = True,
) -> Tuple[_TextBlock, ...]:
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
        if len(reader.pages) == 0:
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
        if len(reader.pages) > MAX_PDF_PAGES:
            raise _resource_limit()
        if _pdf_has_active_or_embedded_content(reader):
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
        blocks = []
        total_characters = 0
        extracted_any_text = False
        section = None
        section_current_observation_eligible = current_observation_eligible_by_default
        for page_number, page in enumerate(reader.pages, start=1):
            extracted = page.extract_text()
            if extracted is None:
                extracted = ""
            text = unicodedata.normalize("NFKC", extracted)
            if "\ufffd" in text or "\x00" in text:
                raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
            total_characters += len(text)
            if total_characters > MAX_EXTRACTED_CHARACTERS:
                raise _resource_limit()
            normalized_newlines = text.replace("\r\n", "\n").replace("\r", "\n")
            for raw_line in normalized_newlines.split("\n"):
                line = _normalized_text(raw_line)
                if not line:
                    continue
                extracted_any_text = True
                if _has_unsafe_time_context_character(raw_line):
                    section_current_observation_eligible = False
                    continue
                if _HISTORICAL_CONTEXT_PATTERN.search(line):
                    section, _ = _section_context(raw_line, line)
                    section_current_observation_eligible = False
                    continue
                if line in _PDF_CURRENT_SECTION_HEADINGS:
                    section, section_current_observation_eligible = _section_context(
                        raw_line, line
                    )
                    continue
                if line in _PDF_LEGAL_SECTION_HEADINGS:
                    section, _ = _section_context(raw_line, line)
                    section_current_observation_eligible = False
                    continue
                periods, residual = _context_periods_and_residual(line)
                if periods or _TEMPORAL_CONTEXT_CUE_PATTERN.search(residual):
                    section_current_observation_eligible = False
                blocks.append(
                    _TextBlock(
                        line,
                        page_number,
                        section,
                        section_current_observation_eligible,
                    )
                )
        if not extracted_any_text:
            raise _fail(
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official PDF has no reliable extractable text",
            )
        return tuple(blocks)
    except RiskDocumentParseError:
        raise
    except Exception as exc:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc


def _pdf_content(
    raw: bytes,
    *,
    periodic_report: bool = False,
) -> Tuple[Tuple[_TextBlock, ...], Tuple[ReportTable, ...]]:
    return (
        _pdf_blocks(
            raw,
            current_observation_eligible_by_default=not periodic_report,
        ),
        (),
    )


def _legacy_conversion_failure(reason: DocumentFailureReason) -> LegacyDocConversionError:
    failure = SafeDocumentFailure(
        public_code="official_document_parse_failed",
        stage=DocumentFailureStage.CONVERSION,
        reason_code=reason,
    )
    failure.validate()
    return LegacyDocConversionError(failure)


_PERIODIC_DOCUMENT_KINDS = frozenset(
    {
        DocumentKind.ANNUAL_REPORT,
        DocumentKind.SEMIANNUAL_REPORT,
        DocumentKind.QUARTERLY_REPORT,
    }
)
_HISTORICAL_CONTEXT_PATTERN = re.compile(
    r"历史|上期|上一期|前期|去年|上年度|上季度|上年末|往期|同期|期初|"
    r"historical|prior period|previous period",
    flags=re.IGNORECASE,
)
_TEMPORAL_CONTEXT_CUE_PATTERN = re.compile(
    r"(?<![0-9])[0-9]{4}(?![0-9])|[〇零一二三四五六七八九]{4}年|"
    r"(?<![0-9])[0-9]{1,2}月(?:[0-9]{1,2}日)?|"
    r"(?:上|下|本)?半年|(?:上)?(?:年|月|季)(?:初|末)|年度|半年度|中期|"
    r"第?[一二三四1-4]季度|"
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|q[1-4]|quarter|annual|semiannual|semi-annual|"
    r"half-year|year[ -]?end|month[ -]?end|quarter[ -]?end)\b|"
    r"截至|截止|as of|as at",
    flags=re.IGNORECASE,
)
_GENERIC_CURRENT_CONTEXT_PATTERN = re.compile(
    r"(?:(?:截至|截止)\s*)?(?:(?:本)?报告期末|本期末|期末)|"
    r"(?:(?:as of|as at)\s+)?(?:the\s+)?end of (?:the\s+)?reporting period",
    flags=re.IGNORECASE,
)
_CONTEXT_QUARTERS = {
    "1": (3, 31),
    "一": (3, 31),
    "2": (6, 30),
    "二": (6, 30),
    "3": (9, 30),
    "三": (9, 30),
    "4": (12, 31),
    "四": (12, 31),
}
_CONTEXT_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

# Unicode Default_Ignorable_Code_Point ranges; Cf is also checked by category.
_DEFAULT_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD),
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


def _has_unsafe_time_context_character(value: str) -> bool:
    for character in value:
        if unicodedata.category(character) in {"Cc", "Cf"}:
            return True
        codepoint = ord(character)
        if any(start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES):
            return True
    return False


def _context_periods_and_residual(normalized: str) -> Tuple[set[date], str]:
    periods: set[date] = set()
    spans: List[Tuple[int, int]] = []

    def add_span(match: re.Match[str], value: date) -> None:
        periods.add(value)
        spans.append(match.span())

    chinese_prefix = r"(?:(?:截至|截止)\s*)?"
    english_prefix = r"(?:(?:as of|as at)\s+)?"
    try:
        for match in re.finditer(
            chinese_prefix
            + r"(?<![0-9])([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日",
            normalized,
        ):
            add_span(match, date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        for match in re.finditer(
            chinese_prefix
            + r"(?<![0-9])([0-9]{4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,2})(?![0-9])",
            normalized,
        ):
            add_span(match, date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        for match in re.finditer(
            chinese_prefix + r"(?<![0-9])([0-9]{4})年(?:末|年度)",
            normalized,
        ):
            add_span(match, date(int(match.group(1)), 12, 31))
        for match in re.finditer(
            chinese_prefix + r"(?<![0-9])([0-9]{4})年(?:半年度|中期)",
            normalized,
        ):
            add_span(match, date(int(match.group(1)), 6, 30))
        for match in re.finditer(
            chinese_prefix + r"(?<![0-9])([0-9]{4})年第?([一二三四1-4])季度",
            normalized,
        ):
            month, day = _CONTEXT_QUARTERS[match.group(2)]
            add_span(match, date(int(match.group(1)), month, day))

        for match in re.finditer(
            english_prefix
            + r"(?<![0-9])([0-9]{4})\s+(annual|semi-annual|semiannual|half-year)\b",
            normalized,
            flags=re.IGNORECASE,
        ):
            month, day = (12, 31) if match.group(2).casefold() == "annual" else (6, 30)
            add_span(match, date(int(match.group(1)), month, day))
        english_quarters = {
            "q1": (3, 31),
            "first quarter": (3, 31),
            "q2": (6, 30),
            "second quarter": (6, 30),
            "q3": (9, 30),
            "third quarter": (9, 30),
            "q4": (12, 31),
            "fourth quarter": (12, 31),
        }
        for match in re.finditer(
            english_prefix
            + r"(?<![0-9])([0-9]{4})\s*"
            + r"(q[1-4]|first quarter|second quarter|third quarter|fourth quarter)\b",
            normalized,
            flags=re.IGNORECASE,
        ):
            month, day = english_quarters[match.group(2).casefold()]
            add_span(match, date(int(match.group(1)), month, day))
        for match in re.finditer(
            english_prefix
            + r"\b("
            + "|".join(_CONTEXT_MONTHS)
            + r")\s+([0-9]{1,2}),?\s+([0-9]{4})\b",
            normalized,
            flags=re.IGNORECASE,
        ):
            add_span(
                match,
                date(
                    int(match.group(3)),
                    _CONTEXT_MONTHS[match.group(1).casefold()],
                    int(match.group(2)),
                ),
            )
        for match in re.finditer(
            english_prefix
            + r"\b([0-9]{1,2})\s+("
            + "|".join(_CONTEXT_MONTHS)
            + r")\s+([0-9]{4})\b",
            normalized,
            flags=re.IGNORECASE,
        ):
            add_span(
                match,
                date(
                    int(match.group(3)),
                    _CONTEXT_MONTHS[match.group(2).casefold()],
                    int(match.group(1)),
                ),
            )
    except ValueError:
        return set(), normalized

    spans.extend(match.span() for match in _GENERIC_CURRENT_CONTEXT_PATTERN.finditer(normalized))
    residual = list(normalized)
    for start, end in spans:
        residual[start:end] = " " * (end - start)
    return periods, "".join(residual)


def _observation_context_matches_period(
    observation: CurrentReportObservation,
    period_end: date,
) -> bool:
    section = observation.section_name
    if section is None:
        return True
    if _has_unsafe_time_context_character(section):
        return False
    normalized = " ".join(unicodedata.normalize("NFKC", section).split())
    if _HISTORICAL_CONTEXT_PATTERN.search(normalized):
        return False
    periods, residual = _context_periods_and_residual(normalized)
    period_matches = not periods or periods == {period_end}
    return period_matches and _TEMPORAL_CONTEXT_CUE_PATTERN.search(residual) is None


def _ambiguous_current_fact(fact: ParsedMandateFact) -> ParsedMandateFact:
    ambiguous = replace(fact, confidence_state=FactConfidence.AMBIGUOUS)
    ambiguous = replace(
        ambiguous,
        fact_fingerprint=fact_fingerprint(
            fact_kind=ambiguous.fact_kind,
            normalized_value=ambiguous.normalized_value,
            unit=ambiguous.unit,
            page_number=ambiguous.page_number,
            section_name=ambiguous.section_name,
            source_excerpt=ambiguous.source_excerpt,
            effective_from=ambiguous.effective_from,
            effective_to=ambiguous.effective_to,
            confidence_state=ambiguous.confidence_state,
        ),
    )
    ambiguous.validate()
    return ambiguous


def _current_common_facts(
    artifact: RetrievedArtifact,
    blocks: Tuple[_TextBlock, ...],
    tables: Tuple[ReportTable, ...],
) -> Tuple[Tuple[ParsedMandateFact, ...], Tuple[str, ...]]:
    if artifact.candidate.document_kind not in _PERIODIC_DOCUMENT_KINDS:
        return (), ()
    observations = list(extract_common_report_observations(text_blocks=(), tables=tables))
    for block in blocks:
        if not block.current_observation_eligible or block.nfc_only:
            continue
        if block.section_name is not None and _has_unsafe_time_context_character(
            block.section_name
        ):
            continue
        block_observations = extract_common_report_observations(
            text_blocks=(block.text,),
            tables=(),
        )
        for observation in block_observations:
            bound = replace(
                observation,
                page_number=block.page_number,
                section_name=block.section_name,
            )
            bound.validate()
            observations.append(bound)
    if not observations:
        return (), ()

    period_end = report_period_end(artifact.candidate.title)
    publication_date = artifact.candidate.published_at.astimezone(timezone.utc).date()
    if period_end is None or period_end > publication_date:
        raise _fail(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    observations = [
        observation
        for observation in observations
        if _observation_context_matches_period(observation, period_end)
    ]
    if not observations:
        return (), ()

    facts = []
    fingerprints = set()
    for observation in observations:
        fact = parsed_fact_from_current_observation(
            observation,
            effective_from=period_end,
            effective_to=period_end,
        )
        if fact.fact_fingerprint not in fingerprints:
            facts.append(fact)
            fingerprints.add(fact.fact_fingerprint)
        if len(facts) > MAX_FACTS:
            raise _resource_limit()

    conflicts = set()
    grouped = {}
    for fact in facts:
        grouped.setdefault(fact.fact_kind, []).append(fact)
    conflicting_kinds = {
        kind
        for kind, same_kind in grouped.items()
        if len(
            {
                json.dumps(
                    {
                        "unit": fact.unit,
                        "value": canonical_fact_value(fact.normalized_value),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for fact in same_kind
            }
        )
        > 1
    }
    if conflicting_kinds:
        conflicts.add("duplicate_conflicting_clause")
        facts = [
            _ambiguous_current_fact(fact) if fact.fact_kind in conflicting_kinds else fact
            for fact in facts
        ]
    return tuple(facts), tuple(sorted(conflicts))


def _parsed_document(
    artifact: RetrievedArtifact,
    blocks: Tuple[_TextBlock, ...],
    tables: Tuple[ReportTable, ...],
) -> ParsedRiskDocument:
    if type(tables) is not tuple or len(tables) > MAX_REPORT_TABLES:
        raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
    for table in tables:
        if type(table) is not ReportTable:
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
        try:
            table.validate()
        except ValueError as exc:
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID) from exc
    effective_from, effective_to = _effective_dates(blocks)
    facts, warnings, conflicts = _extract_facts(
        blocks,
        effective_from,
        effective_to,
        artifact.candidate.document_kind,
    )
    current_facts, current_conflicts = _current_common_facts(artifact, blocks, tables)
    facts = facts + current_facts
    if len(facts) > MAX_FACTS:
        raise _resource_limit()
    result = ParsedRiskDocument(
        artifact=artifact,
        facts=facts,
        warnings=warnings,
        conflicts=tuple(sorted(set(conflicts) | set(current_conflicts))),
    )
    result.validate()
    return result


def parse_artifact_with_provenance(
    artifact: RetrievedArtifact,
    *,
    legacy_converter: Optional[LegacyDocConverter] = None,
) -> ParsedArtifactResult:
    """Parse one authenticated artifact and retain its exact parser provenance."""

    raw = _read_artifact(artifact)
    media_type, charset = _parse_content_type(artifact.content_type)
    if raw.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
        if media_type != "application/msword" or charset is not None:
            raise _fail(DocumentFailureReason.PARSER_FORMAT_INVALID)
        if legacy_converter is None:
            raise _legacy_conversion_failure(DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE)
        try:
            conversion = legacy_converter.convert(artifact)
        except LegacyDocConversionError:
            raise
        except Exception:
            raise _legacy_conversion_failure(
                DocumentFailureReason.LEGACY_CONVERTER_FAILED
            ) from None
        if type(conversion) is not LegacyConversionResult:
            raise _legacy_conversion_failure(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
        try:
            conversion.validate()
        except ValueError:
            raise _legacy_conversion_failure(
                DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID
            ) from None
        _validate_normalized_html(conversion.normalized_html)
        blocks, block_views, tables = _converted_html_content(
            conversion.normalized_html.encode("utf-8")
        )
        try:
            validate_converted_document_contract(block_views, artifact.candidate)
        except ConvertedDocumentContractError as exc:
            raise _fail(exc.reason_code) from None
        _validate_converted_fund_identity(block_views, artifact.candidate)
        document = _parsed_document(artifact, blocks, tables)
        parser_input_sha256 = conversion.parser_input_sha256
        provenance = conversion.provenance
    else:
        if media_type in _HTML_MEDIA_TYPES:
            blocks, tables = _html_content(raw, charset)
        elif media_type == "application/pdf" and charset is None:
            blocks, tables = _pdf_content(
                raw,
                periodic_report=(
                    artifact.candidate.document_kind in _PERIODIC_DOCUMENT_KINDS
                ),
            )
        elif media_type in _DOCX_MEDIA_TYPES and charset is None:
            blocks, tables = _docx_content(raw)
            _validate_document_fund_identity(blocks, artifact.candidate)
        else:
            raise _fail(
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "official fund document has an unsupported content type",
            )
        document = _parsed_document(artifact, blocks, tables)
        parser_input_sha256 = artifact.sha256
        provenance = native_parser_provenance()

    parsed = ParsedArtifactResult(
        document=document,
        parser_input_sha256=parser_input_sha256,
        provenance=provenance,
    )
    parsed.validate()
    return parsed


def parse_artifact(artifact: RetrievedArtifact) -> ParsedRiskDocument:
    """Compatibility wrapper for native parsing and fail-closed legacy routing."""

    return parse_artifact_with_provenance(artifact).document
