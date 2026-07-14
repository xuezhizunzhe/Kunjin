from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Optional, Tuple

from kunjin.funds.risk.documents import MAX_EXCERPT_CHARACTERS
from kunjin.funds.risk.models import FactConfidence

MAX_REPORT_TABLES = 256
MAX_REPORT_ROWS = 20_000
MAX_REPORT_CELLS_PER_ROW = 32
MAX_REPORT_CELL_CHARACTERS = 4_096
MAX_REPORT_SECTION_CHARACTERS = 256

_FACT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_UNIT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TOTAL_ASSET_DENOMINATOR = re.compile(
    r"基金总资产|(?:^|\b)(?:fund\s+)?total\s+assets?(?:\b|$)",
    flags=re.IGNORECASE,
)
_NET_ASSET_DENOMINATOR = re.compile(
    r"基金资产净值|基金净资产|(?:^|\b)(?:fund\s+)?net\s+assets?(?:\b|$)",
    flags=re.IGNORECASE,
)


def _require_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if type(vars(value)) is not dict or set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_bounded_text(
    value: object,
    label: str,
    *,
    maximum: int,
) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or _has_control_characters(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty text")


def _validate_public_value(value: object) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("normalized value Decimal must be finite")
        return
    if type(value) is str:
        _validate_bounded_text(
            value,
            "normalized value",
            maximum=MAX_REPORT_CELL_CHARACTERS,
        )
        return
    if type(value) is tuple:
        for item in value:
            _validate_public_value(item)
        return
    raise ValueError("normalized value must be deeply immutable")


@dataclass(frozen=True)
class ReportCell:
    text: str
    is_header: bool

    def validate(self) -> None:
        _require_exact_record(self, ReportCell, "report cell")
        _validate_bounded_text(
            self.text,
            "report cell text",
            maximum=MAX_REPORT_CELL_CHARACTERS,
        )
        if type(self.is_header) is not bool:
            raise ValueError("report cell header state must be boolean")


@dataclass(frozen=True)
class ReportRow:
    cells: Tuple[ReportCell, ...]

    def validate(self) -> None:
        _require_exact_record(self, ReportRow, "report row")
        if type(self.cells) is not tuple:
            raise ValueError("report row cells must be an immutable tuple")
        if not self.cells or len(self.cells) > MAX_REPORT_CELLS_PER_ROW:
            raise ValueError("report row cell count is outside the configured limit")
        for cell in self.cells:
            if type(cell) is not ReportCell:
                raise ValueError("report row cells must use exact ReportCell records")
            cell.validate()
        headers = [cell.text for cell in self.cells if cell.is_header]
        if len(headers) != len(set(headers)):
            raise ValueError("report row contains duplicate headers")


@dataclass(frozen=True)
class ReportTable:
    rows: Tuple[ReportRow, ...]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str

    def validate(self) -> None:
        _require_exact_record(self, ReportTable, "report table")
        if type(self.rows) is not tuple:
            raise ValueError("report table rows must be an immutable tuple")
        if not self.rows or len(self.rows) > MAX_REPORT_ROWS:
            raise ValueError("report table exceeds the configured row limit")
        for row in self.rows:
            if type(row) is not ReportRow:
                raise ValueError("report table rows must use exact ReportRow records")
            row.validate()
        widths = {len(row.cells) for row in self.rows}
        if len(widths) != 1:
            raise ValueError("report table rows must have the same number of cells")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("report table page number must be positive")
        if self.section_name is not None:
            _validate_bounded_text(
                self.section_name,
                "report table section name",
                maximum=MAX_REPORT_SECTION_CHARACTERS,
            )
        _validate_bounded_text(
            self.source_excerpt,
            "report table source excerpt",
            maximum=MAX_EXCERPT_CHARACTERS,
        )
        joined = " ".join(cell.text for row in self.rows for cell in row.cells)
        denominators = {
            name
            for name, pattern in (
                ("total_assets", _TOTAL_ASSET_DENOMINATOR),
                ("net_assets", _NET_ASSET_DENOMINATOR),
            )
            if pattern.search(joined)
        }
        if len(denominators) > 1:
            raise ValueError("report table contains mixed denominators")


@dataclass(frozen=True)
class CurrentReportObservation:
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    confidence_state: FactConfidence

    def validate(self) -> None:
        _require_exact_record(self, CurrentReportObservation, "current report observation")
        if type(self.fact_kind) is not str or not _FACT_KIND_PATTERN.fullmatch(self.fact_kind):
            raise ValueError("current report fact kind must be a lowercase stable code")
        _validate_public_value(self.normalized_value)
        if self.unit is not None and (
            type(self.unit) is not str or not _UNIT_PATTERN.fullmatch(self.unit)
        ):
            raise ValueError("current report unit must be one stable denominator")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("current report page number must be positive")
        if self.section_name is not None:
            _validate_bounded_text(
                self.section_name,
                "current report section name",
                maximum=MAX_REPORT_SECTION_CHARACTERS,
            )
        _validate_bounded_text(
            self.source_excerpt,
            "current report source excerpt",
            maximum=MAX_EXCERPT_CHARACTERS,
        )
        if type(self.confidence_state) is not FactConfidence:
            raise ValueError("current report confidence state is invalid")
