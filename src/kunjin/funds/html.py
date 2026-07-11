from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin


IGNORED_ELEMENTS = {"script", "style", "iframe"}
HEADING_ELEMENTS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class FundParseError(RuntimeError):
    """Raised when disclosure markup cannot be parsed without guessing."""

    def __init__(self, code: str, message: str = "fund HTML parsing failed") -> None:
        super().__init__(f"{message} ({code})")
        self.code = code


@dataclass(frozen=True)
class HtmlLink:
    text: str
    url: str


@dataclass(frozen=True)
class HtmlTable:
    caption: str
    headers: Tuple[str, ...]
    rows: Tuple[Tuple[str, ...], ...]
    links: Tuple[Tuple[str, str], ...]


@dataclass
class _Cell:
    tag: str
    rowspan: int
    colspan: int
    text_parts: List[str] = field(default_factory=list)


@dataclass
class _RawTable:
    fallback_caption: str
    caption_parts: List[str] = field(default_factory=list)
    rows: List[List[_Cell]] = field(default_factory=list)
    links: List[HtmlLink] = field(default_factory=list)


@dataclass
class _OpenAnchor:
    href: str
    table: Optional[_RawTable]
    text_parts: List[str] = field(default_factory=list)


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _positive_span(attributes: Dict[str, str], name: str) -> int:
    try:
        return max(1, int(attributes.get(name, "1")))
    except (TypeError, ValueError):
        return 1


class _FundHtmlParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.tables: List[_RawTable] = []
        self.links: List[HtmlLink] = []
        self.definition_pairs: List[Tuple[str, str]] = []
        self.paragraphs: List[str] = []

        self._ignored_tag: Optional[str] = None
        self._ignored_same_tag_depth = 0
        self._pending_heading = ""
        self._heading_tag: Optional[str] = None
        self._heading_parts: List[str] = []
        self._table: Optional[_RawTable] = None
        self._table_depth = 0
        self._row: Optional[List[_Cell]] = None
        self._cell: Optional[_Cell] = None
        self._in_caption = False
        self._anchor: Optional[_OpenAnchor] = None
        self._paragraph_parts: Optional[List[str]] = None
        self._term_parts: Optional[List[str]] = None
        self._definition_parts: Optional[List[str]] = None
        self._active_term = ""

    def handle_starttag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        tag = tag.lower()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_same_tag_depth += 1
            return
        if tag in IGNORED_ELEMENTS:
            self._ignored_tag = tag
            self._ignored_same_tag_depth = 1
            return
        if self._table is not None and self._table_depth > 1:
            if tag == "table":
                self._table_depth += 1
            return

        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag in HEADING_ELEMENTS:
            self._heading_tag = tag
            self._heading_parts = []
        elif tag == "table":
            if self._table is None:
                self._table = _RawTable(fallback_caption=self._pending_heading)
                self._table_depth = 1
            else:
                self._table_depth += 1
        elif tag == "caption" and self._table is not None and self._table_depth == 1:
            self._in_caption = True
        elif tag == "tr" and self._table is not None and self._table_depth == 1:
            self._finish_cell()
            self._finish_row()
            self._row = []
        elif tag in {"th", "td"} and self._row is not None and self._table_depth == 1:
            self._finish_cell()
            self._cell = _Cell(
                tag=tag,
                rowspan=_positive_span(attributes, "rowspan"),
                colspan=_positive_span(attributes, "colspan"),
            )
        elif tag == "a" and self._anchor is None:
            self._anchor = _OpenAnchor(
                href=attributes.get("href", ""),
                table=self._table,
            )
        elif tag == "p":
            self._finish_paragraph()
            self._paragraph_parts = []
        elif tag == "dt":
            self._finish_term()
            self._term_parts = []
        elif tag == "dd":
            self._finish_definition()
            self._definition_parts = []

    def handle_startendtag(
        self, tag: str, attrs: List[Tuple[str, Optional[str]]]
    ) -> None:
        if self._ignored_tag is not None or tag.lower() in IGNORED_ELEMENTS:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_tag is not None:
            if tag == self._ignored_tag:
                self._ignored_same_tag_depth -= 1
                if self._ignored_same_tag_depth <= 0:
                    self._ignored_tag = None
                    self._ignored_same_tag_depth = 0
            return
        if self._table is not None and self._table_depth > 1:
            if tag == "table":
                self._table_depth -= 1
            return

        if tag == "a":
            self._finish_anchor()
        elif tag in {"th", "td"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_cell()
            self._finish_row()
        elif tag == "caption":
            self._in_caption = False
        elif tag == "table" and self._table is not None:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._finish_cell()
                self._finish_row()
                self.tables.append(self._table)
                self._table = None
                self._table_depth = 0
        elif tag == self._heading_tag:
            self._pending_heading = _normalize_text("".join(self._heading_parts))
            self._heading_tag = None
            self._heading_parts = []
        elif tag == "p":
            self._finish_paragraph()
        elif tag == "dt":
            self._finish_term()
        elif tag == "dd":
            self._finish_definition()

    def handle_data(self, data: str) -> None:
        if self._ignored_tag is not None or self._table_depth > 1:
            return
        if self._heading_tag is not None:
            self._heading_parts.append(data)
        if self._in_caption and self._table is not None:
            self._table.caption_parts.append(data)
        if self._cell is not None:
            self._cell.text_parts.append(data)
        if self._anchor is not None:
            self._anchor.text_parts.append(data)
        if self._paragraph_parts is not None:
            self._paragraph_parts.append(data)
        if self._term_parts is not None:
            self._term_parts.append(data)
        if self._definition_parts is not None:
            self._definition_parts.append(data)

    def finish(self) -> None:
        self.close()
        self._finish_anchor()
        self._finish_paragraph()
        self._finish_term()
        self._finish_definition()
        self._finish_cell()
        self._finish_row()
        if self._table is not None:
            self.tables.append(self._table)
            self._table = None
            self._table_depth = 0

    def _finish_anchor(self) -> None:
        if self._anchor is None:
            return
        text = _normalize_text("".join(self._anchor.text_parts))
        href = self._anchor.href.strip()
        if text and href:
            link = HtmlLink(text=text, url=urljoin(self.base_url, href))
            self.links.append(link)
            if self._anchor.table is not None:
                self._anchor.table.links.append(link)
        self._anchor = None

    def _finish_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
        self._cell = None

    def _finish_row(self) -> None:
        if self._row is not None and self._table is not None and self._row:
            self._table.rows.append(self._row)
        self._row = None

    def _finish_paragraph(self) -> None:
        if self._paragraph_parts is not None:
            value = _normalize_text("".join(self._paragraph_parts))
            if value:
                self.paragraphs.append(value)
        self._paragraph_parts = None

    def _finish_term(self) -> None:
        if self._term_parts is not None:
            self._active_term = _normalize_text("".join(self._term_parts))
        self._term_parts = None

    def _finish_definition(self) -> None:
        if self._definition_parts is not None:
            value = _normalize_text("".join(self._definition_parts))
            if self._active_term and value:
                self.definition_pairs.append((self._active_term, value))
        self._definition_parts = None


def _parse(text: str, base_url: str) -> _FundHtmlParser:
    parser = _FundHtmlParser(base_url)
    try:
        parser.feed(text)
        parser.finish()
    except Exception as exc:
        raise FundParseError("malformed_html") from exc
    return parser


def _expand_rows(raw_rows: List[List[_Cell]]) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    expanded: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = []
    carried: Dict[int, Tuple[str, str, int]] = {}

    for raw_row in raw_rows:
        values: Dict[int, str] = {}
        kinds: Dict[int, str] = {}
        next_carried: Dict[int, Tuple[str, str, int]] = {}
        for column, (value, kind, remaining) in carried.items():
            values[column] = value
            kinds[column] = kind
            if remaining > 1:
                next_carried[column] = (value, kind, remaining - 1)

        column = 0
        for cell in raw_row:
            while column in values:
                column += 1
            value = _normalize_text("".join(cell.text_parts))
            for offset in range(cell.colspan):
                target = column + offset
                values[target] = value
                kinds[target] = cell.tag
                if cell.rowspan > 1:
                    next_carried[target] = (value, cell.tag, cell.rowspan - 1)
            column += cell.colspan

        if values:
            width = max(values) + 1
            expanded.append(
                (
                    tuple(values.get(index, "") for index in range(width)),
                    tuple(kinds.get(index, "td") for index in range(width)),
                )
            )
        carried = next_carried

    return expanded


def _convert_table(raw_table: _RawTable) -> HtmlTable:
    expanded = _expand_rows(raw_table.rows)
    width = max((len(values) for values, _ in expanded), default=0)
    headers: Tuple[str, ...] = ()
    data_rows: List[Tuple[str, ...]] = []
    for values, kinds in expanded:
        padded_values = values + ("",) * (width - len(values))
        is_header_row = bool(values) and all(kind == "th" for kind in kinds)
        if not headers and is_header_row:
            headers = padded_values
        elif headers and padded_values == headers and is_header_row:
            continue
        else:
            data_rows.append(padded_values)

    caption = _normalize_text("".join(raw_table.caption_parts))
    if not caption:
        caption = raw_table.fallback_caption
    return HtmlTable(
        caption=caption,
        headers=headers,
        rows=tuple(data_rows),
        links=tuple((link.text, link.url) for link in raw_table.links),
    )


def parse_tables(text: str, base_url: str) -> List[HtmlTable]:
    """Parse disclosure tables without executing or loading referenced content."""

    return [_convert_table(table) for table in _parse(text, base_url).tables]


def extract_labeled_values(text: str, base_url: str) -> Dict[str, List[str]]:
    """Collect explicit label/value pairs from common fund disclosure markup."""

    parser = _parse(text, base_url)
    values: Dict[str, List[str]] = {}

    def add(label: str, value: str) -> None:
        normalized_label = _normalize_text(label).rstrip(":：")
        normalized_value = _normalize_text(value)
        if normalized_label and normalized_value:
            values.setdefault(normalized_label, []).append(normalized_value)

    for label, value in parser.definition_pairs:
        add(label, value)

    for raw_table in parser.tables:
        table = _convert_table(raw_table)
        for row in table.rows:
            if len(row) >= 2:
                for value in row[1:]:
                    add(row[0], value)

    for paragraph in parser.paragraphs:
        colon_positions = [
            position
            for position in (paragraph.find("："), paragraph.find(":"))
            if position >= 0
        ]
        if colon_positions:
            position = min(colon_positions)
            add(paragraph[:position], paragraph[position + 1 :])

    return values


def extract_links(text: str, base_url: str) -> List[HtmlLink]:
    """Return anchors in document order with resolved URLs; no URL is fetched."""

    return list(_parse(text, base_url).links)
