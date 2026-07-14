from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import os
import re
import socket
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Tuple

from kunjin.funds.models import FUND_CODE_PATTERN, DocumentKind, FundAnnouncement
from kunjin.funds.official_domains import (
    FUND_COMPANY_DOMAINS,
    INDEX_PROVIDER_DOMAINS,
    OFFICIAL_SOURCE_REGISTRATIONS,
    REGULATOR_AND_EXCHANGE_DOMAINS,
    OfficialSourceRegistration,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.funds.sources import classify_source
from kunjin.paths import RuntimePaths

MAX_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 1500
MAX_EXTRACTED_CHARACTERS = 20_000_000
MAX_FACTS = 10_000
MAX_EXCERPT_CHARACTERS = 4096
MAX_DISCOVERY_PAGES = 25
MAX_DISCOVERY_ITEMS = 2000
MAX_REDIRECTS = 5
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_DOCX_ENTRIES = 1024
MAX_DOCX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024

_PDF_CONTENT_TYPES = frozenset({"application/pdf"})
_HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_GENERIC_WORD_CONTENT_TYPE = "application/msword"
_DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_OLE_COMPOUND_FILE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")


class OfficialDocumentError(RuntimeError):
    code = "official_document_invalid"

    def __init__(
        self,
        stage: DocumentFailureStage,
        reason_code: DocumentFailureReason,
        message: str,
    ) -> None:
        failure = SafeDocumentFailure(self.code, stage, reason_code)
        failure.validate()
        self.failure = failure
        super().__init__(message)


class OfficialDocumentUnavailableError(OfficialDocumentError):
    code = "official_document_unavailable"


class OfficialDocumentResourceLimitError(OfficialDocumentError):
    code = "official_document_resource_limit"


def _validate_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


@dataclass(frozen=True)
class OfficialDocumentCandidate:
    fund_code: str
    document_kind: DocumentKind
    title: str
    url: str
    publisher: str
    published_at: Optional[datetime]
    source_tier: int

    def validate(self) -> None:
        _validate_exact_record(self, OfficialDocumentCandidate, "official document candidate")
        if type(self.fund_code) is not str or not FUND_CODE_PATTERN.fullmatch(self.fund_code):
            raise ValueError("official document requires a six-digit fund code")
        if type(self.document_kind) is not DocumentKind:
            raise ValueError("official document kind is invalid")
        if (
            type(self.title) is not str
            or type(self.publisher) is not str
            or not self.title.strip()
            or not self.publisher.strip()
        ):
            raise ValueError("official document title and publisher are required")
        if self.published_at is not None and (
            type(self.published_at) is not datetime
            or self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("official document publication time must be timezone-aware")
        if type(self.source_tier) is not int or self.source_tier != 1:
            raise ValueError("official document candidate must be tier one")
        if type(self.url) is not str:
            raise ValueError("official document URL must be exact text")
        validate_safe_https_url(self.url)


@dataclass(frozen=True)
class RetrievedArtifact:
    candidate: OfficialDocumentCandidate
    final_url: str
    retrieved_at: datetime
    content_type: str
    byte_size: int
    sha256: str
    managed_path: Path


@dataclass(frozen=True)
class OfficialDocumentIndexItem:
    title: str
    url: str
    publisher: str
    published_at: Optional[datetime]

    def validate(self) -> None:
        _validate_exact_record(self, OfficialDocumentIndexItem, "official index item")
        if (
            type(self.title) is not str
            or type(self.publisher) is not str
            or not self.title.strip()
            or not self.publisher.strip()
        ):
            raise ValueError("official document index item is incomplete")
        if self.published_at is not None and (
            type(self.published_at) is not datetime
            or self.published_at.tzinfo is None
            or self.published_at.utcoffset() is None
        ):
            raise ValueError("official index publication time must be timezone-aware")
        if type(self.url) is not str:
            raise ValueError("official index URL must be exact text")
        validate_safe_https_url(self.url)


@dataclass(frozen=True)
class OfficialDocumentIndexPage:
    page_number: int
    total_pages: int
    items: Tuple[OfficialDocumentIndexItem, ...]

    def validate(self) -> None:
        _validate_exact_record(self, OfficialDocumentIndexPage, "official index page")
        if (
            type(self.page_number) is not int
            or type(self.total_pages) is not int
            or self.page_number <= 0
            or self.total_pages <= 0
        ):
            raise ValueError("official index page numbers must be positive")
        if self.page_number > self.total_pages:
            raise ValueError("official index page exceeds the reported final page")
        if type(self.items) is not tuple:
            raise ValueError("official index items must be an immutable tuple")
        for item in self.items:
            item.validate()


class OfficialIndexClient(Protocol):
    def fetch_page(
        self,
        source: OfficialSourceRegistration,
        fund_code: str,
        page: int,
    ) -> OfficialDocumentIndexPage: ...


class _IndexHtmlParser(HTMLParser):
    def __init__(self, base_url: str, publisher: str, fund_code: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.publisher = publisher
        self.fund_code = fund_code
        self.reported_total_pages: Optional[int] = None
        self.has_next = False
        self.items = []
        self._anchor: Optional[dict] = None
        self._text = []
        self._fund_code_seen = False
        self._heading_parts: Optional[list[str]] = None
        self._product_identity: Optional[str] = None
        self._span_parts: Optional[list[str]] = None
        self._pending_published_at: Optional[datetime] = None
        self._list_item_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        folded_tag = tag.casefold()
        if folded_tag == "input" and attributes.get("id", "").casefold() == "fundcode":
            page_code = attributes.get("value", "").strip()
            if page_code and page_code != self.fund_code:
                raise OfficialDocumentError(
                    DocumentFailureStage.IDENTITY_VALIDATION,
                    DocumentFailureReason.IDENTITY_MISMATCH,
                    "official document index fund identity mismatch",
                )
            self._fund_code_seen = page_code == self.fund_code
        if folded_tag == "h2" and self._fund_code_seen and self._product_identity is None:
            self._heading_parts = []
        if folded_tag == "li":
            self._list_item_depth += 1
            self._pending_published_at = None
        if folded_tag == "span" and self._list_item_depth > 0 and self._span_parts is None:
            self._span_parts = []
        total_pages = attributes.get("data-total-pages")
        if total_pages is not None:
            try:
                parsed_total = int(total_pages)
            except ValueError as exc:
                raise OfficialDocumentError(
                    DocumentFailureStage.DISCOVERY,
                    DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                    "official document index returned invalid pagination",
                ) from exc
            if parsed_total <= 0:
                raise OfficialDocumentError(
                    DocumentFailureStage.DISCOVERY,
                    DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                    "official document index returned invalid pagination",
                )
            if self.reported_total_pages is not None and self.reported_total_pages != parsed_total:
                raise OfficialDocumentError(
                    DocumentFailureStage.DISCOVERY,
                    DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                    "official document index returned conflicting pagination",
                )
            self.reported_total_pages = parsed_total
        if folded_tag != "a" or self._anchor is not None:
            return
        href = attributes.get("href")
        if not href:
            return
        rel = attributes.get("rel", "").casefold().split()
        if "next" in rel:
            self.has_next = True
        self._anchor = {
            "href": href,
            "published_at": attributes.get("data-published-at"),
            "title": attributes.get("title"),
        }
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)
        elif self._heading_parts is not None:
            self._heading_parts.append(data)
        elif self._span_parts is not None:
            self._span_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded_tag = tag.casefold()
        if folded_tag == "h2" and self._heading_parts is not None:
            identity = " ".join("".join(self._heading_parts).split())
            self._product_identity = identity or None
            self._heading_parts = None
        if folded_tag == "span" and self._span_parts is not None:
            value = " ".join("".join(self._span_parts).split())
            match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
            if match is not None:
                try:
                    self._pending_published_at = datetime(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        tzinfo=timezone.utc,
                    )
                except ValueError as exc:
                    raise OfficialDocumentError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                        "official document index returned invalid publication time",
                    ) from exc
            self._span_parts = None
        if folded_tag == "li" and self._list_item_depth > 0:
            self._list_item_depth -= 1
            self._pending_published_at = None
        if folded_tag != "a" or self._anchor is None:
            return
        title = " ".join((self._anchor.get("title") or "".join(self._text)).split())
        if (
            title
            and _document_kind_from_title(title) is not None
            and _title_matches_product(title, self._product_identity)
        ):
            published_at = _parse_index_publication_time(self._anchor["published_at"])
            self.items.append(
                OfficialDocumentIndexItem(
                    title=title,
                    url=_canonical_index_document_url(
                        self.base_url,
                        self._anchor["href"],
                    ),
                    publisher=self.publisher,
                    published_at=published_at or self._pending_published_at,
                )
            )
        self._anchor = None
        self._text = []
        self._pending_published_at = None


class _AttachmentHtmlParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.urls: list[str] = []
        self.title: Optional[str] = None
        self._title_parts: Optional[list[str]] = None
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.casefold() == "h1" and self.title is None:
            self._title_parts = []
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        resolved = urllib.parse.urljoin(self.base_url, href)
        path = urllib.parse.urlparse(resolved).path.casefold()
        if path.endswith((".pdf", ".doc", ".docx")):
            self.urls.append(resolved)

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        if self._title_parts is not None:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "h1" and self._title_parts is not None:
            title = " ".join("".join(self._title_parts).split())
            self.title = title or None
            self._title_parts = None


def _parse_index_publication_time(value: Optional[str]) -> Optional[datetime]:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfficialDocumentError(
            DocumentFailureStage.DISCOVERY,
            DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
            "official document index returned invalid publication time",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_index_document_url(base_url: str, href: str) -> str:
    resolved = urllib.parse.urljoin(base_url, href)
    parsed = urllib.parse.urlparse(resolved)
    if (parsed.hostname or "").lower().rstrip(".") != "www.fund001.com":
        return resolved
    match = re.fullmatch(
        r"/news/(\d{4})-(\d{2})-(\d{2})/(\d+)_([1-9]\d*)\.shtml",
        parsed.path,
    )
    if match is None:
        return resolved
    canonical_path = "/news/{}/{}/{}/{}/{}.shtml".format(*match.groups())
    return urllib.parse.urlunparse(parsed._replace(path=canonical_path))


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(normalized.split()).casefold()


def validate_safe_https_url(url: str) -> urllib.parse.ParseResult:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("official document URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("official document URL must be safe HTTPS")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("official document URL host is invalid")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return parsed
    raise ValueError("official document URL cannot use an IP literal")


def _is_disallowed_address(address: ipaddress._BaseAddress) -> bool:
    return any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _validate_public_dns(
    host: str,
    port: int,
    *,
    stage: DocumentFailureStage,
) -> None:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OfficialDocumentUnavailableError(
            stage,
            DocumentFailureReason.DNS_UNAVAILABLE,
            "official document DNS lookup failed",
        ) from exc
    if not results:
        raise OfficialDocumentUnavailableError(
            stage,
            DocumentFailureReason.DNS_UNAVAILABLE,
            "official document DNS lookup returned no addresses",
        )
    for result in results:
        try:
            address = ipaddress.ip_address(result[4][0])
        except (ValueError, IndexError, TypeError) as exc:
            raise OfficialDocumentError(
                stage,
                DocumentFailureReason.SOURCE_UNREGISTERED,
                "official document DNS returned an invalid address",
            ) from exc
        if _is_disallowed_address(address):
            raise OfficialDocumentError(
                stage,
                DocumentFailureReason.SOURCE_UNREGISTERED,
                "official document DNS resolved to a non-public address",
            )


def validate_official_source(publisher: str, url: str) -> Tuple[str, ...]:
    try:
        parsed = validate_safe_https_url(url)
    except ValueError as exc:
        raise OfficialDocumentError(
            DocumentFailureStage.IDENTITY_VALIDATION,
            DocumentFailureReason.SOURCE_UNREGISTERED,
            "official document source is not registered",
        ) from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if type(publisher) is not str or not publisher:
        raise OfficialDocumentError(
            DocumentFailureStage.IDENTITY_VALIDATION,
            DocumentFailureReason.SOURCE_UNREGISTERED,
            "official document source is not registered",
        )

    for registration in OFFICIAL_SOURCE_REGISTRATIONS:
        if publisher == registration.identity and host in registration.accepted_hosts:
            return registration.accepted_hosts

    regulator_publishers = REGULATOR_AND_EXCHANGE_DOMAINS.get(host, ())
    if publisher in regulator_publishers:
        return (host,)

    manager = FUND_COMPANY_DOMAINS.get(host)
    if manager is not None and publisher == manager:
        return tuple(
            sorted(
                registered_host
                for registered_host, identity in FUND_COMPANY_DOMAINS.items()
                if identity == publisher
            )
        )

    provider = INDEX_PROVIDER_DOMAINS.get(host)
    if provider is not None and publisher == provider:
        return tuple(
            sorted(
                registered_host
                for registered_host, identity in INDEX_PROVIDER_DOMAINS.items()
                if identity == publisher
            )
        )

    raise OfficialDocumentError(
        DocumentFailureStage.IDENTITY_VALIDATION,
        DocumentFailureReason.SOURCE_UNREGISTERED,
        "official document source is not registered",
    )


def _validate_registered_url(
    url: str,
    allowed_hosts: Tuple[str, ...],
    *,
    redirect: bool,
    stage: DocumentFailureStage,
) -> None:
    if redirect:
        failure_stage = stage
        failure_reason = DocumentFailureReason.REDIRECT_REJECTED
    elif stage is DocumentFailureStage.DISCOVERY:
        failure_stage = stage
        failure_reason = DocumentFailureReason.DISCOVERY_FORMAT_INVALID
    elif stage is DocumentFailureStage.LANDING_VALIDATION:
        failure_stage = stage
        failure_reason = DocumentFailureReason.ATTACHMENT_HOST_REJECTED
    else:
        failure_stage = DocumentFailureStage.IDENTITY_VALIDATION
        failure_reason = DocumentFailureReason.SOURCE_UNREGISTERED
    try:
        parsed = validate_safe_https_url(url)
    except ValueError as exc:
        label = "redirect" if redirect else "source"
        raise OfficialDocumentError(
            failure_stage,
            failure_reason,
            f"official document {label} is invalid",
        ) from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in allowed_hosts:
        label = "redirect" if redirect else "source"
        raise OfficialDocumentError(
            failure_stage,
            failure_reason,
            f"official document {label} host is not registered",
        )
    _validate_public_dns(host, parsed.port or 443, stage=stage)


_TITLE_PATTERNS = (
    (
        DocumentKind.PRODUCT_SUMMARY,
        re.compile(r"基金产品资料概要"),
    ),
    (
        DocumentKind.PROSPECTUS_UPDATE,
        re.compile(r"(?:更新.*招募说明书|招募说明书.*更新)"),
    ),
    (
        DocumentKind.PROSPECTUS,
        re.compile(r"^(?!.*(?:更新.*招募说明书|招募说明书.*更新)).*招募说明书"),
    ),
    (DocumentKind.FUND_CONTRACT, re.compile(r"基金合同")),
    (DocumentKind.SEMIANNUAL_REPORT, re.compile(r"(?:半年度报告|中期报告)")),
    (DocumentKind.ANNUAL_REPORT, re.compile(r"(?<!半)年度报告")),
    (DocumentKind.QUARTERLY_REPORT, re.compile(r"(?:第?[一二三四1-4]季度报告|季度报告)")),
    (
        DocumentKind.INDEX_METHODOLOGY,
        re.compile(r"(?:指数编制方案|指数方法论|指数编制细则|指数规则)"),
    ),
    (
        DocumentKind.CLASSIFICATION_ANNOUNCEMENT,
        re.compile(r"(?:基金转型|基金合并|投资范围变更|业绩比较基准变更|基金类型变更)"),
    ),
)


def _document_kind_from_title(title: str) -> Optional[DocumentKind]:
    normalized = unicodedata.normalize("NFKC", title)
    matches = {kind for kind, pattern in _TITLE_PATTERNS if pattern.search(normalized)}
    if len(matches) != 1:
        return None
    return next(iter(matches))


def _share_class_letters(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[A-Z]", value.upper()))


def _product_identity_parts(value: str) -> Tuple[str, frozenset[str]]:
    normalized = "".join(unicodedata.normalize("NFKC", value).split())
    normalized = normalized.replace("(", "（").replace(")", "）")
    share_classes = set()
    for match in re.finditer(
        r"（([A-Z](?:[/、][A-Z])*)类份额）|([A-Z](?:[/、][A-Z])*)类份额",
        normalized,
        flags=re.IGNORECASE,
    ):
        share_classes.update(
            _share_class_letters(next(group for group in match.groups() if group is not None))
        )
    normalized = re.sub(
        r"（[A-Z](?:[/、][A-Z])*类份额）|[A-Z](?:[/、][A-Z])*类份额",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    trailing = re.search(r"([A-Z](?:[/、][A-Z])*)$", normalized, flags=re.IGNORECASE)
    if trailing is not None:
        share_classes.update(_share_class_letters(trailing.group(1)))
        normalized = normalized[: trailing.start()]
    return normalized.casefold(), frozenset(share_classes)


def _title_matches_product(title: str, product_identity: Optional[str]) -> bool:
    if product_identity is None:
        return True
    product_core, product_share_classes = _product_identity_parts(product_identity)
    title_core, title_share_classes = _product_identity_parts(title)
    if product_core not in title_core:
        return False
    if not title_share_classes:
        return True
    return bool(product_share_classes) and title_share_classes.issubset(product_share_classes)


def discover_candidate(
    announcement: FundAnnouncement,
    *,
    manager_name: str,
) -> Optional[OfficialDocumentCandidate]:
    announcement.validate()
    kind = _document_kind_from_title(announcement.title)
    if kind is None:
        return None
    if (
        classify_source(
            announcement.url,
            announcement.publisher,
            manager_name,
        )
        != 1
    ):
        return None
    candidate = OfficialDocumentCandidate(
        fund_code=announcement.fund_code,
        document_kind=kind,
        title=announcement.title,
        url=announcement.url,
        publisher=announcement.publisher,
        published_at=announcement.published_at,
        source_tier=1,
    )
    candidate.validate()
    return candidate


def discover_index_candidate(
    item: OfficialDocumentIndexItem,
    *,
    fund_code: str,
    provider_name: str,
) -> Optional[OfficialDocumentCandidate]:
    if type(fund_code) is not str or not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError("index methodology requires a six-digit fund code")
    if type(provider_name) is not str or not provider_name.strip():
        raise ValueError("index methodology provider identity is required")
    try:
        item.validate()
        parsed = validate_safe_https_url(item.url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    registered_provider = INDEX_PROVIDER_DOMAINS.get(host)
    expected = _normalized_text(provider_name)
    if (
        registered_provider is None
        or _normalized_text(registered_provider) != expected
        or _normalized_text(item.publisher) != expected
        or _document_kind_from_title(item.title) is not DocumentKind.INDEX_METHODOLOGY
    ):
        return None
    candidate = OfficialDocumentCandidate(
        fund_code=fund_code,
        document_kind=DocumentKind.INDEX_METHODOLOGY,
        title=item.title,
        url=item.url,
        publisher=item.publisher,
        published_at=item.published_at,
        source_tier=1,
    )
    candidate.validate()
    return candidate


class OfficialDocumentDiscovery:
    def __init__(
        self,
        *,
        client: OfficialIndexClient,
        registrations: Tuple[OfficialSourceRegistration, ...] = OFFICIAL_SOURCE_REGISTRATIONS,
        max_pages: int = MAX_DISCOVERY_PAGES,
        max_items: int = MAX_DISCOVERY_ITEMS,
    ) -> None:
        if type(registrations) is not tuple:
            raise ValueError("official discovery registrations must be an immutable tuple")
        for registration in registrations:
            try:
                registration.validate()
            except (AttributeError, ValueError) as exc:
                raise ValueError("official discovery registration is invalid") from exc
        if type(max_pages) is not int or max_pages <= 0 or max_pages > MAX_DISCOVERY_PAGES:
            raise ValueError("official discovery page limit is invalid")
        if type(max_items) is not int or max_items <= 0 or max_items > MAX_DISCOVERY_ITEMS:
            raise ValueError("official discovery item limit is invalid")
        self.client = client
        self.registrations = registrations
        self.max_pages = max_pages
        self.max_items = max_items

    def discover(
        self,
        fund_code: str,
        *,
        manager_name: Optional[str],
        announcements: Iterable[FundAnnouncement] = (),
    ) -> Tuple[OfficialDocumentCandidate, ...]:
        if not FUND_CODE_PATTERN.fullmatch(fund_code):
            raise ValueError("official discovery requires a six-digit fund code")
        candidates = []
        item_count = 0

        manager_sources = tuple(
            source for source in self.registrations if source.source_kind == "fund_manager"
        )
        matched_sources = tuple(
            source
            for source in manager_sources
            if manager_name is not None and source.matches_identity(manager_name)
        )
        selected_sources = matched_sources or tuple(
            source for source in manager_sources if source.binds_fund_identity
        )

        for source in selected_sources:
            page_number = 1
            while True:
                if page_number > self.max_pages:
                    raise OfficialDocumentResourceLimitError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.RESOURCE_LIMIT,
                        "official document discovery exceeded page limit",
                    )
                try:
                    source.index_url(fund_code, page_number)
                except ValueError as exc:
                    raise OfficialDocumentError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                        "official document index reported pagination for a single-page source",
                    ) from exc
                page = self.client.fetch_page(source, fund_code, page_number)
                try:
                    page.validate()
                except ValueError as exc:
                    raise OfficialDocumentError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                        "official document index page is invalid",
                    ) from exc
                if page.page_number != page_number:
                    raise OfficialDocumentError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                        "official document index page sequence is invalid",
                    )
                if page.total_pages > self.max_pages:
                    raise OfficialDocumentResourceLimitError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.RESOURCE_LIMIT,
                        "official document discovery exceeded page limit",
                    )
                item_count += len(page.items)
                if item_count > self.max_items:
                    raise OfficialDocumentResourceLimitError(
                        DocumentFailureStage.DISCOVERY,
                        DocumentFailureReason.RESOURCE_LIMIT,
                        "official document discovery exceeded item limit",
                    )
                for item in page.items:
                    candidate = self._candidate_from_index_item(
                        source,
                        fund_code,
                        item,
                    )
                    if candidate is not None:
                        candidates.append(candidate)
                if page_number >= page.total_pages:
                    break
                page_number += 1

        for item in announcements:
            if manager_name is None:
                continue
            if item.fund_code != fund_code:
                continue
            candidate = discover_candidate(item, manager_name=manager_name)
            if candidate is not None:
                candidates.append(candidate)

        unique = {(item.document_kind.value, item.url, item.title): item for item in candidates}
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    item.document_kind.value,
                    item.published_at or datetime.min.replace(tzinfo=timezone.utc),
                    item.url,
                ),
            )
        )

    @staticmethod
    def _candidate_from_index_item(
        source: OfficialSourceRegistration,
        fund_code: str,
        item: OfficialDocumentIndexItem,
    ) -> Optional[OfficialDocumentCandidate]:
        if _normalized_text(item.publisher) != _normalized_text(source.identity):
            return None
        try:
            parsed = validate_safe_https_url(item.url)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in source.accepted_hosts:
            return None
        kind = _document_kind_from_title(item.title)
        if kind is None:
            return None
        candidate = OfficialDocumentCandidate(
            fund_code=fund_code,
            document_kind=kind,
            title=item.title,
            url=item.url,
            publisher=item.publisher,
            published_at=item.published_at,
            source_tier=1,
        )
        candidate.validate()
        return candidate


class _AllowedHostsRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS
    max_repeats = 2

    def __init__(
        self,
        allowed_hosts: Tuple[str, ...],
        *,
        stage: DocumentFailureStage,
    ) -> None:
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.stage = stage

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        target = urllib.parse.urljoin(req.full_url, newurl)
        _validate_registered_url(
            target,
            self.allowed_hosts,
            redirect=True,
            stage=self.stage,
        )
        return super().redirect_request(req, fp, code, msg, headers, target)


class OfficialRedirectHandler(_AllowedHostsRedirectHandler):
    def __init__(self, candidate: OfficialDocumentCandidate) -> None:
        candidate.validate()
        super().__init__(
            validate_official_source(candidate.publisher, candidate.url),
            stage=DocumentFailureStage.RETRIEVAL,
        )


class OfficialHtmlIndexClient:
    def __init__(
        self,
        *,
        opener: Optional[object] = None,
        timeout_seconds: int = 20,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("official index timeout must be positive")
        self.opener = opener
        self.timeout_seconds = timeout_seconds

    def fetch_page(
        self,
        source: OfficialSourceRegistration,
        fund_code: str,
        page: int,
    ) -> OfficialDocumentIndexPage:
        try:
            url = source.index_url(fund_code, page)
        except ValueError as exc:
            raise OfficialDocumentError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                "official document index URL is invalid",
            ) from exc
        _validate_registered_url(
            url,
            source.accepted_hosts,
            redirect=False,
            stage=DocumentFailureStage.DISCOVERY,
        )
        opener = self.opener or urllib.request.build_opener(
            _AllowedHostsRedirectHandler(
                source.accepted_hosts,
                stage=DocumentFailureStage.RETRIEVAL,
            )
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "KunJin/0.1 read-only official fund index client",
            },
            method="GET",
        )
        try:
            response = opener.open(request, timeout=self.timeout_seconds)
            with response:
                final_url = response.geturl()
                _validate_registered_url(
                    final_url,
                    source.accepted_hosts,
                    redirect=True,
                    stage=DocumentFailureStage.RETRIEVAL,
                )
                content_type = response.headers.get("Content-Type", "")
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        parsed_size = int(declared_size)
                    except ValueError as exc:
                        raise OfficialDocumentError(
                            DocumentFailureStage.DISCOVERY,
                            DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                            "official document index returned invalid content length",
                        ) from exc
                    if parsed_size < 0 or parsed_size > MAX_INDEX_BYTES:
                        raise OfficialDocumentResourceLimitError(
                            DocumentFailureStage.DISCOVERY,
                            DocumentFailureReason.RESOURCE_LIMIT,
                            "official document index exceeds size limit",
                        )
                chunks = []
                byte_size = 0
                while True:
                    chunk = response.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > MAX_INDEX_BYTES:
                        raise OfficialDocumentResourceLimitError(
                            DocumentFailureStage.DISCOVERY,
                            DocumentFailureReason.RESOURCE_LIMIT,
                            "official document index exceeds size limit",
                        )
                    chunks.append(chunk)
        except OfficialDocumentError:
            raise
        except urllib.error.HTTPError as exc:
            raise OfficialDocumentUnavailableError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.HTTP_UNAVAILABLE,
                f"official document index HTTP request failed with status {exc.code}",
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            OSError,
        ) as exc:
            raise OfficialDocumentUnavailableError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.NETWORK_UNAVAILABLE,
                "official document index network request failed",
            ) from exc

        payload = b"".join(chunks)
        if _declared_family(content_type) != "html" or _detected_family(payload) != "html":
            raise OfficialDocumentError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                "official document index content type does not match payload",
            )
        _validate_html_document(payload, stage=DocumentFailureStage.DISCOVERY)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = payload.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise OfficialDocumentError(
                    DocumentFailureStage.DISCOVERY,
                    DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                    "official document index encoding is invalid",
                ) from exc
        parser = _IndexHtmlParser(final_url, source.identity, fund_code)
        try:
            parser.feed(text)
            parser.close()
        except OfficialDocumentError:
            raise
        except Exception as exc:
            raise OfficialDocumentError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                "official document index HTML is invalid",
            ) from exc
        if source.binds_fund_identity and (
            not parser._fund_code_seen or parser._product_identity is None
        ):
            raise OfficialDocumentError(
                DocumentFailureStage.IDENTITY_VALIDATION,
                DocumentFailureReason.IDENTITY_MISMATCH,
                "official document index fund identity is missing",
            )
        if source.requires_publication_date and any(
            item.published_at is None for item in parser.items
        ):
            raise OfficialDocumentError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.PUBLICATION_DATE_MISSING,
                "official document index publication date is missing",
            )
        if len(parser.items) > MAX_DISCOVERY_ITEMS:
            raise OfficialDocumentResourceLimitError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.RESOURCE_LIMIT,
                "official document discovery exceeded item limit",
            )
        total_pages = parser.reported_total_pages
        if total_pages is None:
            total_pages = page + 1 if parser.has_next else page
        result = OfficialDocumentIndexPage(
            page_number=page,
            total_pages=total_pages,
            items=tuple(parser.items),
        )
        try:
            result.validate()
        except ValueError as exc:
            raise OfficialDocumentError(
                DocumentFailureStage.DISCOVERY,
                DocumentFailureReason.DISCOVERY_FORMAT_INVALID,
                "official document index page is invalid",
            ) from exc
        return result


def _detected_family(payload: bytes) -> Optional[str]:
    if payload.startswith(_OLE_COMPOUND_FILE_SIGNATURE):
        return "legacy_ole_doc"
    if payload.startswith(b"%PDF-"):
        return "pdf"
    prefix = payload[:4096].lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20").lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html") or b"<html" in prefix:
        return "html"
    if payload.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = set(archive.namelist())
        except (OSError, ValueError, zipfile.BadZipFile):
            return None
        if {"[Content_Types].xml", "word/document.xml"}.issubset(names):
            return "docx"
    return None


def _declared_family(content_type: str) -> Optional[str]:
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type in _PDF_CONTENT_TYPES:
        return "pdf"
    if media_type in _HTML_CONTENT_TYPES:
        return "html"
    if media_type == _GENERIC_WORD_CONTENT_TYPE:
        return "generic_word"
    if media_type == _DOCX_CONTENT_TYPE:
        return "docx"
    return None


def _families_match(detected: Optional[str], declared: Optional[str]) -> bool:
    if detected is None or declared is None:
        return False
    if detected == "docx":
        return declared in {"generic_word", "docx"}
    if detected == "legacy_ole_doc":
        return declared == "generic_word"
    return detected == declared


def _validated_container_family(payload: bytes, content_type: str) -> str:
    detected = _detected_family(payload)
    declared = _declared_family(content_type)
    if declared is None:
        raise OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DECLARED_MIME_UNSUPPORTED,
            "official document declared content type is not supported",
        )
    if detected is None:
        raise OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DETECTED_CONTAINER_UNKNOWN,
            "official document payload container is not recognized",
        )
    if not _families_match(detected, declared):
        raise OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
            "official document content type does not match payload",
        )
    return detected


def _validate_html_document(
    payload: bytes,
    *,
    stage: DocumentFailureStage,
) -> None:
    format_reason = (
        DocumentFailureReason.DISCOVERY_FORMAT_INVALID
        if stage is DocumentFailureStage.DISCOVERY
        else DocumentFailureReason.LANDING_FORMAT_INVALID
    )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = payload.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise OfficialDocumentError(
                stage,
                format_reason,
                "official HTML document encoding is invalid",
            ) from exc
    normalized = unicodedata.normalize("NFKC", text).casefold()
    password_input = re.search(r"<input\b[^>]*\btype\s*=\s*['\"]?password", normalized)
    login_form = "<form" in normalized and any(
        marker in normalized for marker in ("登录", "认证", "captcha", "login", "sign in")
    )
    if password_input or login_form:
        raise OfficialDocumentError(
            stage,
            DocumentFailureReason.AUTHENTICATION_SHELL,
            "official document returned an authentication shell",
        )
    without_scripts = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        "",
        normalized,
        flags=re.S,
    )
    visible = re.sub(r"<[^>]+>", "", without_scripts)
    if not "".join(visible.split()):
        raise OfficialDocumentError(
            stage,
            DocumentFailureReason.EMPTY_OR_SCRIPT_ONLY_HTML,
            "official document returned a script-only HTML shell",
        )


def _resolve_attachment_url(
    payload: bytes,
    landing_url: str,
    allowed_hosts: Tuple[str, ...],
    candidate: OfficialDocumentCandidate,
) -> Optional[str]:
    if "/news/" not in urllib.parse.urlparse(landing_url).path.casefold():
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = payload.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise OfficialDocumentError(
                DocumentFailureStage.LANDING_VALIDATION,
                DocumentFailureReason.LANDING_FORMAT_INVALID,
                "official document landing page encoding is invalid",
            ) from exc
    parser = _AttachmentHtmlParser(landing_url)
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise OfficialDocumentError(
            DocumentFailureStage.LANDING_VALIDATION,
            DocumentFailureReason.LANDING_FORMAT_INVALID,
            "official document landing page is invalid",
        ) from exc
    if parser.title is None or _normalized_text(parser.title) != _normalized_text(candidate.title):
        raise OfficialDocumentError(
            DocumentFailureStage.LANDING_VALIDATION,
            DocumentFailureReason.LANDING_TITLE_MISMATCH,
            "official document landing page title mismatch",
        )
    if candidate.published_at is not None:
        expected_date = candidate.published_at.astimezone(timezone.utc).date()
        page_dates = set()
        for match in re.finditer(
            r"时间\s*[:：]\s*(\d{4})[-/](\d{2})[-/](\d{2})(?!\d)",
            " ".join(parser.text_parts),
        ):
            try:
                page_dates.add(
                    datetime(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        tzinfo=timezone.utc,
                    ).date()
                )
            except ValueError as exc:
                raise OfficialDocumentError(
                    DocumentFailureStage.LANDING_VALIDATION,
                    DocumentFailureReason.LANDING_DATE_MISMATCH,
                    "official document landing page date is invalid",
                ) from exc
        if page_dates != {expected_date}:
            raise OfficialDocumentError(
                DocumentFailureStage.LANDING_VALIDATION,
                DocumentFailureReason.LANDING_DATE_MISMATCH,
                "official document landing page date mismatch",
            )
    urls = tuple(sorted(set(parser.urls)))
    if not urls:
        raise OfficialDocumentError(
            DocumentFailureStage.LANDING_VALIDATION,
            DocumentFailureReason.ATTACHMENT_MISSING,
            "official document landing page attachment is missing",
        )
    for url in urls:
        try:
            parsed = validate_safe_https_url(url)
        except ValueError as exc:
            raise OfficialDocumentError(
                DocumentFailureStage.LANDING_VALIDATION,
                DocumentFailureReason.ATTACHMENT_HOST_REJECTED,
                "official document attachment is invalid",
            ) from exc
        if (parsed.hostname or "").lower().rstrip(".") not in allowed_hosts:
            raise OfficialDocumentError(
                DocumentFailureStage.LANDING_VALIDATION,
                DocumentFailureReason.ATTACHMENT_HOST_REJECTED,
                "official document attachment host is not registered",
            )
    if len(urls) != 1:
        raise OfficialDocumentError(
            DocumentFailureStage.LANDING_VALIDATION,
            DocumentFailureReason.ATTACHMENT_AMBIGUOUS,
            "official document landing page attachment is ambiguous",
        )
    _validate_registered_url(
        urls[0],
        allowed_hosts,
        redirect=False,
        stage=DocumentFailureStage.LANDING_VALIDATION,
    )
    return urls[0]


def _verify_existing_artifact(path: Path, digest: str, byte_size: int) -> None:
    if path.is_symlink():
        raise OfficialDocumentError(
            DocumentFailureStage.PERSISTENCE,
            DocumentFailureReason.MANAGED_ARTIFACT_INVALID,
            "managed official document artifact cannot be a symbolic link",
        )
    if not path.is_file() or path.stat().st_size != byte_size:
        raise OfficialDocumentError(
            DocumentFailureStage.PERSISTENCE,
            DocumentFailureReason.MANAGED_ARTIFACT_INVALID,
            "managed official document artifact is inconsistent",
        )
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            checksum.update(chunk)
    if checksum.hexdigest() != digest:
        raise OfficialDocumentError(
            DocumentFailureStage.PERSISTENCE,
            DocumentFailureReason.MANAGED_ARTIFACT_INVALID,
            "managed official document artifact is inconsistent",
        )
    path.chmod(0o600)


class OfficialDocumentClient:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        opener: Optional[object] = None,
        timeout_seconds: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("official document timeout must be positive")
        self.paths = paths.ensure()
        self.opener = opener
        self.timeout_seconds = timeout_seconds
        self.clock = clock

    def _download_to_temp(
        self,
        url: str,
        allowed_hosts: Tuple[str, ...],
    ) -> Tuple[Path, str, str, int, str, bytes]:
        opener = self.opener or urllib.request.build_opener(
            _AllowedHostsRedirectHandler(
                allowed_hosts,
                stage=DocumentFailureStage.RETRIEVAL,
            )
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": (
                    "application/pdf,application/msword,"
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document,text/html,application/xhtml+xml;q=0.9"
                ),
                "User-Agent": "KunJin/0.1 read-only official fund document client",
            },
            method="GET",
        )
        temp_path: Optional[Path] = None
        try:
            response = opener.open(request, timeout=self.timeout_seconds)
            with response:
                final_url = response.geturl()
                _validate_registered_url(
                    final_url,
                    allowed_hosts,
                    redirect=True,
                    stage=DocumentFailureStage.RETRIEVAL,
                )
                content_type = response.headers.get("Content-Type", "")
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        parsed_size = int(declared_size)
                    except ValueError as exc:
                        raise OfficialDocumentError(
                            DocumentFailureStage.RETRIEVAL,
                            DocumentFailureReason.NETWORK_UNAVAILABLE,
                            "official document returned invalid content length",
                        ) from exc
                    if parsed_size < 0:
                        raise OfficialDocumentError(
                            DocumentFailureStage.RETRIEVAL,
                            DocumentFailureReason.NETWORK_UNAVAILABLE,
                            "official document returned invalid content length",
                        )
                    if parsed_size > MAX_DOCUMENT_BYTES:
                        raise OfficialDocumentResourceLimitError(
                            DocumentFailureStage.RETRIEVAL,
                            DocumentFailureReason.RESOURCE_LIMIT,
                            "official document response exceeds size limit",
                        )

                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".partial-",
                    dir=self.paths.fund_documents,
                )
                temp_path = Path(temporary_name)
                os.fchmod(descriptor, 0o600)
                checksum = hashlib.sha256()
                byte_size = 0
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = response.read(STREAM_CHUNK_BYTES)
                        if not chunk:
                            break
                        byte_size += len(chunk)
                        if byte_size > MAX_DOCUMENT_BYTES:
                            raise OfficialDocumentResourceLimitError(
                                DocumentFailureStage.RETRIEVAL,
                                DocumentFailureReason.RESOURCE_LIMIT,
                                "official document response exceeds size limit",
                            )
                        checksum.update(chunk)
                        output.write(chunk)
            payload = temp_path.read_bytes()
            return (
                temp_path,
                final_url,
                content_type,
                byte_size,
                checksum.hexdigest(),
                payload,
            )
        except Exception:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    def fetch(self, candidate: OfficialDocumentCandidate) -> RetrievedArtifact:
        candidate.validate()
        allowed_hosts = validate_official_source(candidate.publisher, candidate.url)
        _validate_registered_url(
            candidate.url,
            allowed_hosts,
            redirect=False,
            stage=DocumentFailureStage.RETRIEVAL,
        )
        temp_path: Optional[Path] = None
        try:
            temp_path, final_url, content_type, byte_size, digest, payload = self._download_to_temp(
                candidate.url, allowed_hosts
            )
            detected = _validated_container_family(payload, content_type)
            if detected == "html":
                _validate_html_document(
                    payload,
                    stage=DocumentFailureStage.LANDING_VALIDATION,
                )
                attachment_url = _resolve_attachment_url(
                    payload,
                    final_url,
                    allowed_hosts,
                    candidate,
                )
                if attachment_url is not None:
                    temp_path.unlink()
                    temp_path = None
                    temp_path, final_url, content_type, byte_size, digest, payload = (
                        self._download_to_temp(attachment_url, allowed_hosts)
                    )
                    detected = _validated_container_family(payload, content_type)
                    if detected == "html":
                        raise OfficialDocumentError(
                            DocumentFailureStage.LANDING_VALIDATION,
                            DocumentFailureReason.ATTACHMENT_MISSING,
                            "official document attachment did not return a document",
                        )
            retrieved_at = self.clock()
            if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
                raise OfficialDocumentError(
                    DocumentFailureStage.RETRIEVAL,
                    DocumentFailureReason.CLOCK_INVALID,
                    "official document clock must be timezone-aware",
                )
            suffix = {
                "pdf": ".pdf",
                "html": ".html",
                "docx": ".docx",
                "legacy_ole_doc": ".doc",
            }[detected]
            managed_path = self.paths.fund_documents / (digest + suffix)
            try:
                os.link(temp_path, managed_path)
            except FileExistsError:
                _verify_existing_artifact(managed_path, digest, byte_size)
            else:
                managed_path.chmod(0o600)
            return RetrievedArtifact(
                candidate=candidate,
                final_url=final_url,
                retrieved_at=retrieved_at.astimezone(timezone.utc),
                content_type=content_type,
                byte_size=byte_size,
                sha256=digest,
                managed_path=managed_path,
            )
        except OfficialDocumentError:
            raise
        except urllib.error.HTTPError as exc:
            raise OfficialDocumentUnavailableError(
                DocumentFailureStage.RETRIEVAL,
                DocumentFailureReason.HTTP_UNAVAILABLE,
                f"official document HTTP request failed with status {exc.code}",
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            OSError,
        ) as exc:
            raise OfficialDocumentUnavailableError(
                DocumentFailureStage.RETRIEVAL,
                DocumentFailureReason.NETWORK_UNAVAILABLE,
                "official document network request failed",
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
