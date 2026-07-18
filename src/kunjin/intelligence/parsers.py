from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from kunjin.intelligence.models import LineageKind

EASTMONEY_MARKET_FIELDS = "f12,f14,f3,f8,f62,f184,f104,f105"

_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_COLLECTION_ITEMS = 512
_MAX_JSON_MAPPING_ITEMS = 128
_MAX_TEXT_CHARS = 4_096
_MAX_BREADTH_TOTAL = 10_000
_EXCERPT_MAX_BYTES = 2_048
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UTC = timezone.utc
_GOV_POLICY_URL = re.compile(
    r"https://www\.gov\.cn/zhengce/"
    r"(?:content/\d{6}/content_\d+\.htm|zhengceku/\d{6}/content_\d+\.htm)"
)
_STCN_DETAIL_PATH = re.compile(r"/article/detail/(\d+)\.html")
_SECTOR_CODE = re.compile(r"[A-Za-z0-9._-]{1,32}")
_ORIGINAL_STCN_PUBLISHERS = frozenset({"证券时报网", "证券时报", "券商中国", "人民财讯"})
_IGNORED_HTML_TAGS = frozenset({"iframe", "noscript", "script", "style"})
_CONTENT_BLOCK_TAGS = frozenset(
    {"article", "blockquote", "br", "div", "h1", "h2", "h3", "li", "p", "section"}
)


class IntelligenceParseError(ValueError):
    """A public source payload did not match its reviewed deterministic shape."""


@dataclass(frozen=True)
class ArticleCandidate:
    detail_id: str
    canonical_url: str
    listed_title: str


@dataclass(frozen=True)
class ParsedItem:
    source_id: str
    hosting_publisher: str
    attributed_publisher: str
    canonical_url: str
    title: str
    normalized_public_content: str
    published_at: datetime
    retrieved_at: datetime
    category: str
    lineage_hint: LineageKind
    author: Optional[str]
    publication_precision: str
    publication_interval_end: Optional[datetime]
    excerpt: str
    excerpt_truncated: bool
    excerpt_original_bytes: int
    content_fingerprint: str


@dataclass(frozen=True)
class ParsedSectorMarketRow:
    sector_code: str
    sector_name: str
    sector_kind: str
    pct_change: Optional[Decimal]
    turnover_rate: Optional[Decimal]
    main_net_inflow: Optional[Decimal]
    main_net_inflow_ratio: Optional[Decimal]
    advancers: Optional[int]
    decliners: Optional[int]
    retrieved_at: datetime


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _bounded_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise IntelligenceParseError(f"{name} must be text")
    normalized = _normalize_text(value)
    if (not normalized and not allow_empty) or len(normalized) > _MAX_TEXT_CHARS:
        raise IntelligenceParseError(f"{name} must be bounded non-empty text")
    return normalized


def _validate_retrieved_at(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise IntelligenceParseError("retrieved_at must be an aware UTC datetime")
    return value


def _reject_json_constant(value: str) -> object:
    raise IntelligenceParseError(f"JSON contains non-finite constant {value}")


def _mapping_without_duplicate_keys(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise IntelligenceParseError("JSON contains duplicate mapping keys")
        result[key] = value
    return result


def _decode_json(payload_utf8: object) -> object:
    if type(payload_utf8) is not str:
        raise IntelligenceParseError("payload must be exact UTF-8 text")
    payload_bytes = payload_utf8.encode("utf-8")
    if not payload_bytes or len(payload_bytes) > _MAX_PAYLOAD_BYTES:
        raise IntelligenceParseError("payload must be non-empty and at most 5 MiB")
    try:
        value = json.loads(
            payload_utf8,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_mapping_without_duplicate_keys,
        )
    except IntelligenceParseError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise IntelligenceParseError("payload contains malformed JSON") from exc
    _validate_json_tree(value)
    return value


def _validate_json_tree(value: object, *, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise IntelligenceParseError("JSON exceeds the public tree depth limit")
    if type(value) is dict:
        if len(value) > _MAX_JSON_MAPPING_ITEMS:
            raise IntelligenceParseError("JSON mapping has too many items")
        for key, item in value.items():
            if type(key) is not str or len(key) > _MAX_TEXT_CHARS:
                raise IntelligenceParseError("JSON mapping key is invalid")
            _validate_json_tree(item, depth=depth + 1)
        return
    if type(value) is list:
        if len(value) > _MAX_JSON_COLLECTION_ITEMS:
            raise IntelligenceParseError("JSON list has too many items")
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
        return
    if type(value) is str:
        if len(value) > _MAX_PAYLOAD_BYTES:
            raise IntelligenceParseError("JSON text value is too large")
        return
    if value is None or type(value) in {bool, int, Decimal}:
        return
    raise IntelligenceParseError("JSON contains an unsupported value")


def _excerpt_and_fingerprint(content: str) -> Tuple[str, bool, int, str]:
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > _MAX_PAYLOAD_BYTES:
        raise IntelligenceParseError("normalized public content must be non-empty and bounded")
    truncated = len(encoded) > _EXCERPT_MAX_BYTES
    excerpt_bytes = encoded[:_EXCERPT_MAX_BYTES]
    while True:
        try:
            excerpt = excerpt_bytes.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            excerpt_bytes = excerpt_bytes[: exc.start]
    return excerpt, truncated, len(encoded), hashlib.sha256(encoded).hexdigest()


def _parsed_item(
    *,
    source_id: str,
    hosting_publisher: str,
    attributed_publisher: str,
    canonical_url: str,
    title: str,
    content: str,
    published_at: datetime,
    retrieved_at: datetime,
    category: str,
    lineage_hint: LineageKind,
    author: Optional[str],
    publication_precision: str,
    publication_interval_end: Optional[datetime],
) -> ParsedItem:
    if published_at.utcoffset() != timedelta(0) or retrieved_at < published_at:
        raise IntelligenceParseError("publication time must be UTC and not after retrieval")
    excerpt, truncated, original_bytes, fingerprint = _excerpt_and_fingerprint(content)
    return ParsedItem(
        source_id=source_id,
        hosting_publisher=hosting_publisher,
        attributed_publisher=attributed_publisher,
        canonical_url=canonical_url,
        title=title,
        normalized_public_content=content,
        published_at=published_at,
        retrieved_at=retrieved_at,
        category=category,
        lineage_hint=lineage_hint,
        author=author,
        publication_precision=publication_precision,
        publication_interval_end=publication_interval_end,
        excerpt=excerpt,
        excerpt_truncated=truncated,
        excerpt_original_bytes=original_bytes,
        content_fingerprint=fingerprint,
    )


def parse_gov_policy_list(payload_utf8: str, retrieved_at: datetime) -> Tuple[ParsedItem, ...]:
    retrieved = _validate_retrieved_at(retrieved_at)
    payload = _decode_json(payload_utf8)
    if type(payload) is not list:
        raise IntelligenceParseError("government policy payload must be an exact list")

    items: List[ParsedItem] = []
    required_keys = ("TITLE", "SUB_TITLE", "URL", "DOCRELPUBTIME")
    for row in payload:
        if type(row) is not dict or any(key not in row for key in required_keys):
            raise IntelligenceParseError("government policy row is incomplete")
        title = _bounded_text(row["TITLE"], "government policy title")
        subtitle = _bounded_text(
            row["SUB_TITLE"], "government policy subtitle", allow_empty=True
        )
        canonical_url = _bounded_text(row["URL"], "government policy URL")
        if _GOV_POLICY_URL.fullmatch(canonical_url) is None:
            raise IntelligenceParseError("government policy URL is not an exact gov.cn policy path")
        publication_date_text = _bounded_text(
            row["DOCRELPUBTIME"], "government policy publication date"
        )
        try:
            publication_date = date.fromisoformat(publication_date_text)
        except ValueError:
            raise IntelligenceParseError("government policy publication date is invalid") from None
        if publication_date.isoformat() != publication_date_text:
            raise IntelligenceParseError("government policy publication date is not canonical")
        published_at = datetime.combine(
            publication_date, time.min, tzinfo=_SHANGHAI
        ).astimezone(_UTC)
        interval_end = datetime.combine(
            publication_date + timedelta(days=1), time.min, tzinfo=_SHANGHAI
        ).astimezone(_UTC)
        content = " ".join(part for part in (title, subtitle) if part)
        items.append(
            _parsed_item(
                source_id="gov_cn_policy",
                hosting_publisher="中国政府网",
                attributed_publisher="中国政府网",
                canonical_url=canonical_url,
                title=title,
                content=content,
                published_at=published_at,
                retrieved_at=retrieved,
                category="policy",
                lineage_hint=LineageKind.ORIGINAL,
                author=None,
                publication_precision="date",
                publication_interval_end=interval_end,
            )
        )
    return tuple(items)


class _StcnListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: List[Tuple[str, str]] = []
        self._href: Optional[str] = None
        self._parts: List[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if tag in _IGNORED_HTML_TAGS:
            self._ignored_depth = 1
            return
        if tag == "a":
            self._finish_anchor()
            attributes = dict(attrs)
            self._href = attributes.get("href")
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "a":
            self._finish_anchor()

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and self._href is not None:
            self._parts.append(data)

    def finish(self) -> None:
        self.close()
        self._finish_anchor()

    def _finish_anchor(self) -> None:
        if self._href is not None:
            self.anchors.append((self._href, _normalize_text("".join(self._parts))))
        self._href = None
        self._parts = []


def _feed_html(parser: HTMLParser, payload_utf8: object) -> None:
    if type(payload_utf8) is not str:
        raise IntelligenceParseError("HTML payload must be exact UTF-8 text")
    size = len(payload_utf8.encode("utf-8"))
    if size == 0 or size > _MAX_PAYLOAD_BYTES:
        raise IntelligenceParseError("HTML payload must be non-empty and at most 5 MiB")
    try:
        parser.feed(payload_utf8)
    except (AssertionError, UnicodeError) as exc:
        raise IntelligenceParseError("HTML payload is malformed") from exc


def _canonical_stcn_detail_url(value: str) -> Optional[Tuple[str, str]]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    match = _STCN_DETAIL_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.stcn.com"
        or parsed.hostname != "www.stcn.com"
        or port is not None
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    detail_id = match.group(1)
    return detail_id, f"https://www.stcn.com/article/detail/{detail_id}.html"


def parse_stcn_fund_list(
    payload_utf8: str, retrieved_at: datetime
) -> Tuple[ArticleCandidate, ...]:
    _validate_retrieved_at(retrieved_at)
    parser = _StcnListParser()
    _feed_html(parser, payload_utf8)
    parser.finish()

    candidates: List[ArticleCandidate] = []
    seen: Dict[str, ArticleCandidate] = {}
    for href, text in parser.anchors:
        parsed_detail = _canonical_stcn_detail_url(
            urljoin("https://www.stcn.com/article/list/fund.html", href)
        )
        if parsed_detail is None or not text:
            continue
        detail_id, canonical_url = parsed_detail
        title = _bounded_text(text, "STCN listed title")
        candidate = ArticleCandidate(detail_id, canonical_url, title)
        previous = seen.get(detail_id)
        if previous is not None and previous != candidate:
            raise IntelligenceParseError("STCN list contains conflicting duplicate detail IDs")
        if previous is None:
            seen[detail_id] = candidate
            candidates.append(candidate)
    return tuple(candidates)


class _StcnDetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_urls: List[str] = []
        self.titles: List[str] = []
        self.visible_parts: List[str] = []
        self.content_parts: List[str] = []
        self._title_parts: Optional[List[str]] = None
        self._content_depth = 0
        self._content_containers = 0
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            self._ignored_depth += 1
            return
        if tag in _IGNORED_HTML_TAGS:
            self._ignored_depth = 1
            return
        attributes = dict(attrs)
        class_names = frozenset((attributes.get("class") or "").split())
        if tag == "link" and "canonical" in (attributes.get("rel") or "").split():
            href = attributes.get("href")
            if href:
                self.canonical_urls.append(href)
        if tag == "h1":
            if self._title_parts is not None:
                self.titles.append(_normalize_text("".join(self._title_parts)))
            self._title_parts = []
        is_content = (
            attributes.get("id") == "article-content" or "article-content" in class_names
        )
        if is_content:
            self._content_containers += 1
            self._content_depth = 1
        elif self._content_depth:
            self._content_depth += 1
        if self._content_depth and tag in _CONTENT_BLOCK_TAGS:
            self.content_parts.append(" ")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if tag == "h1" and self._title_parts is not None:
            self.titles.append(_normalize_text("".join(self._title_parts)))
            self._title_parts = None
        if self._content_depth:
            if tag in _CONTENT_BLOCK_TAGS:
                self.content_parts.append(" ")
            self._content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.visible_parts.append(data)
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._content_depth:
            self.content_parts.append(data)

    def finish(self) -> None:
        self.close()
        if self._title_parts is not None:
            self.titles.append(_normalize_text("".join(self._title_parts)))
            self._title_parts = None


def _one_match(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text)
    if len(matches) != 1:
        raise IntelligenceParseError(f"STCN detail requires exactly one {label}")
    return _bounded_text(matches[0], f"STCN detail {label}")


def parse_stcn_detail(payload_utf8: str, retrieved_at: datetime) -> ParsedItem:
    retrieved = _validate_retrieved_at(retrieved_at)
    parser = _StcnDetailParser()
    _feed_html(parser, payload_utf8)
    parser.finish()

    canonical_details = [
        parsed
        for value in parser.canonical_urls
        if (parsed := _canonical_stcn_detail_url(value)) is not None
    ]
    titles = [title for title in parser.titles if title]
    if len(canonical_details) != 1 or len(titles) != 1 or parser._content_containers != 1:
        raise IntelligenceParseError(
            "STCN detail requires one canonical detail ID, title, and article container"
        )
    _detail_id, canonical_url = canonical_details[0]
    visible_text = _normalize_text(" ".join(parser.visible_parts))
    source = _one_match(r"来源\s*[:：]\s*([^\s]+)", visible_text, "source label")
    author = _one_match(r"作者\s*[:：]\s*([^\s]+)", visible_text, "author label")
    publication_text = _one_match(
        r"(?<!\d)(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2})(?!\d)",
        visible_text,
        "minute publication time",
    )
    try:
        local_published_at = datetime.strptime(publication_text, "%Y-%m-%d %H:%M").replace(
            tzinfo=_SHANGHAI
        )
    except ValueError:
        raise IntelligenceParseError("STCN detail publication time is invalid") from None
    content = _normalize_text("".join(parser.content_parts))
    if not content:
        raise IntelligenceParseError("STCN detail article container is empty")
    lineage = (
        LineageKind.ORIGINAL if source in _ORIGINAL_STCN_PUBLISHERS else LineageKind.REPRINT
    )
    return _parsed_item(
        source_id="stcn_fund_news",
        hosting_publisher="证券时报网",
        attributed_publisher=source,
        canonical_url=canonical_url,
        title=titles[0],
        content=content,
        published_at=local_published_at.astimezone(_UTC),
        retrieved_at=retrieved,
        category="fund_media",
        lineage_hint=lineage,
        author=author,
        publication_precision="minute",
        publication_interval_end=None,
    )


def _optional_decimal(value: object, field: str) -> Optional[Decimal]:
    if value in (None, "", "-"):
        return None
    if type(value) is bool or type(value) not in {str, int, Decimal}:
        raise IntelligenceParseError(f"Eastmoney {field} must be a decimal value")
    try:
        result = Decimal(value) if type(value) is not Decimal else value
    except (InvalidOperation, ValueError):
        raise IntelligenceParseError(f"Eastmoney {field} must be a finite decimal value") from None
    if not result.is_finite():
        raise IntelligenceParseError(f"Eastmoney {field} must be a finite decimal value")
    return result


def _optional_count(value: object, field: str) -> Optional[int]:
    if value in (None, "", "-"):
        return None
    if type(value) is int:
        result = value
    elif type(value) is str and re.fullmatch(r"\d+", value):
        result = int(value)
    else:
        raise IntelligenceParseError(f"Eastmoney {field} must be a non-negative integer")
    if result < 0:
        raise IntelligenceParseError(f"Eastmoney {field} must be non-negative")
    if result > _MAX_BREADTH_TOTAL:
        raise IntelligenceParseError("Eastmoney breadth total is impossible")
    return result


def parse_eastmoney_market(
    payload_utf8: str, sector_kind: str, retrieved_at: datetime
) -> Tuple[ParsedSectorMarketRow, ...]:
    retrieved = _validate_retrieved_at(retrieved_at)
    if type(sector_kind) is not str or sector_kind not in {"industry", "concept"}:
        raise IntelligenceParseError("sector kind must be exactly industry or concept")
    payload = _decode_json(payload_utf8)
    data = payload.get("data") if type(payload) is dict else None
    rows = data.get("diff") if type(data) is dict else None
    if type(rows) is not list:
        raise IntelligenceParseError("Eastmoney market payload is incomplete")

    parsed_rows: List[ParsedSectorMarketRow] = []
    seen_codes = set()
    for row in rows:
        if type(row) is not dict:
            raise IntelligenceParseError("Eastmoney market row must be a mapping")
        code = _bounded_text(row.get("f12"), "Eastmoney sector code")
        name = _bounded_text(row.get("f14"), "Eastmoney sector name")
        if _SECTOR_CODE.fullmatch(code) is None:
            raise IntelligenceParseError("Eastmoney sector code is invalid")
        if code in seen_codes:
            raise IntelligenceParseError("Eastmoney market contains a duplicate sector code")
        seen_codes.add(code)
        advancers = _optional_count(row.get("f104"), "f104")
        decliners = _optional_count(row.get("f105"), "f105")
        if (
            advancers is not None
            and decliners is not None
            and advancers + decliners > _MAX_BREADTH_TOTAL
        ):
            raise IntelligenceParseError("Eastmoney breadth total is impossible")
        parsed_rows.append(
            ParsedSectorMarketRow(
                sector_code=code,
                sector_name=name,
                sector_kind=sector_kind,
                pct_change=_optional_decimal(row.get("f3"), "f3"),
                turnover_rate=_optional_decimal(row.get("f8"), "f8"),
                main_net_inflow=_optional_decimal(row.get("f62"), "f62"),
                main_net_inflow_ratio=_optional_decimal(row.get("f184"), "f184"),
                advancers=advancers,
                decliners=decliners,
                retrieved_at=retrieved,
            )
        )
    return tuple(parsed_rows)
