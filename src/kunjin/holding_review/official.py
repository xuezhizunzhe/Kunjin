from __future__ import annotations

import hashlib
import http.client
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from html.parser import HTMLParser
from typing import Callable, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from kunjin.brief.models import OfficialEventCode
from kunjin.decision.models import (
    canonical_json_bytes,
    validate_aware_datetime,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
)
from kunjin.funds.official_domains import (
    FUND_COMPANY_DOMAINS,
    OFFICIAL_SOURCE_REGISTRATIONS,
    OfficialSourceRegistration,
)

# Existing public clients either filter index titles or persist documents. The
# holding-review path needs the same audited transport checks while retaining
# bounded raw HTML in memory for later atomic persistence.
from kunjin.funds.risk.documents import (
    STREAM_CHUNK_BYTES,
    OfficialDocumentError,
    OfficialDocumentResourceLimitError,
    OfficialDocumentUnavailableError,
    _AllowedHostsRedirectHandler,
    _validate_html_document,
    _validate_registered_url,
    _validated_container_family,
    validate_official_source,
)
from kunjin.funds.risk.failures import DocumentFailureReason, DocumentFailureStage
from kunjin.holding_review.models import (
    HeldReviewOfficialEventProjection,
    OfficialAnnouncementContent,
    OfficialListingPageEvidence,
    OfficialListingTerminalState,
    TriggeredReviewCode,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1, OfficialCheckPolicyV1
from kunjin.intelligence.models import _validate_public_https_url

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_META_CHARSET = re.compile(
    br"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9_-]+)",
    flags=re.IGNORECASE,
)
_HTML_MEDIA_TYPES = frozenset(("text/html", "application/xhtml+xml"))
_ENCODING_ALIASES = {
    "utf-8": "utf-8",
    "utf8": "utf-8",
    "gb18030": "gb18030",
}
_BLOCK_TAGS = frozenset(
    (
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    )
)
_IGNORED_TAGS = frozenset(("head", "script", "style", "noscript", "template"))
_TITLE_EXCLUSION_MARKERS = (
    "可能",
    "提示",
    "更正",
    "补充",
    "撤销",
    "撤回",
    "恢复",
    "解读",
    "优惠",
    "作废",
)
_CORRECTION_TITLE_MARKERS = ("更正", "补充")
_RETRACTION_TITLE_MARKERS = ("撤销", "撤回", "作废")
_BODY_NEGATION_MARKERS = (
    "不",
    "未",
    "无",
    "并非",
    "取消",
    "撤销",
    "撤回",
    "恢复",
    "更正",
    "作废",
)
_LISTING_GENERIC_MARKERS = (
    "旗下部分基金",
    "旗下基金",
    "部分基金",
    "多只基金",
    "相关基金",
)
_LISTING_INTEGRITY_MARKERS = (
    "更正",
    "补充",
    "撤销",
    "撤回",
    "作废",
)
_LISTING_NEAR_MATCH_MARKERS = (
    "重大事项",
    "重要事项",
    "有关事项",
)
_LISTING_TERMINAL_STATES = frozenset(
    (
        OfficialListingTerminalState.SOURCE_FINAL_PAGE,
        OfficialListingTerminalState.WINDOW_BOUNDARY_REACHED,
    )
)
_EVENT_TRIGGER_MAP = {
    OfficialEventCode.FUND_LIQUIDATION_NOTICE: (
        TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW
    ),
    OfficialEventCode.FUND_TERMINATION_NOTICE: (
        TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW
    ),
    OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE: (
        TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW
    ),
    OfficialEventCode.MANAGER_CHANGE_NOTICE: TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
    OfficialEventCode.FEE_CHANGE_NOTICE: TriggeredReviewCode.FEE_CHANGE_REVIEW,
    OfficialEventCode.BENCHMARK_CHANGE_NOTICE: TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW,
}
_TITLE_PATTERNS = (
    (
        OfficialEventCode.FUND_TERMINATION_NOTICE,
        re.compile(r"^基金合同终止(?:及基金财产清算结果)?(?:的)?公告$"),
    ),
    (
        OfficialEventCode.FUND_LIQUIDATION_NOTICE,
        re.compile(r"^(?:清算(?:报告|公告|的公告)|基金财产清算报告)$"),
    ),
    (
        OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
        re.compile(r"^暂停(?:赎回|大额赎回)(?:业务)?(?:的)?公告$"),
    ),
    (
        OfficialEventCode.MANAGER_CHANGE_NOTICE,
        re.compile(r"^(?:基金经理变更|增聘基金经理)公告$"),
    ),
    (
        OfficialEventCode.FEE_CHANGE_NOTICE,
        re.compile(r"^(?:调整|变更).*(?:费率|管理费|托管费|销售服务费).*公告$"),
    ),
    (
        OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
        re.compile(r"^(?:变更|调整)业绩比较基准.*公告$"),
    ),
)
_BODY_PATTERNS = {
    OfficialEventCode.FUND_LIQUIDATION_NOTICE: re.compile(
        r"(?:基金财产(?:进入|开始)?清算(?:程序)?|进入清算程序)"
    ),
    OfficialEventCode.FUND_TERMINATION_NOTICE: re.compile(r"基金合同(?:将)?终止"),
    OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE: re.compile(
        r"(?:暂停(?:办理)?(?:赎回|大额赎回)(?:业务)?|限制赎回)"
    ),
    OfficialEventCode.MANAGER_CHANGE_NOTICE: re.compile(
        r"(?:基金经理(?:发生)?变更|增聘基金经理|解聘基金经理)"
    ),
    OfficialEventCode.FEE_CHANGE_NOTICE: re.compile(
        r"(?:调整|变更)(?:本基金)?(?:管理费率|托管费率|销售服务费率|费率)"
    ),
    OfficialEventCode.BENCHMARK_CHANGE_NOTICE: re.compile(
        r"(?:变更|调整)(?:本基金)?业绩比较基准"
    ),
}


class AnnouncementContentError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _exact_dataclass(value: object, expected: type, name: str) -> None:
    if type(value) is not expected or set(vars(value)) != {
        item.name for item in fields(expected)
    }:
        raise ValueError(f"{name} must be an exact {expected.__name__}")


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _fund_code(value: object, name: str = "fund code") -> str:
    if type(value) is not str or _FUND_CODE.fullmatch(value) is None or value == "000000":
        raise ValueError(f"{name} must be a non-reserved six-digit code")
    return value


def _utc(value: object, name: str) -> datetime:
    result = validate_aware_datetime(value, name)
    if result.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC")
    return result


@dataclass(frozen=True)
class OfficialAnnouncementRow:
    announcement_row_id: int
    fund_code: str
    product_name: str
    listing_source_document_id: int
    canonical_announcement_url: str
    announcement_title: str
    publisher: str
    published_at: datetime
    source_tier: int
    integrity_status: str
    integrity_checked_at: datetime

    def validate(self) -> None:
        _exact_dataclass(self, OfficialAnnouncementRow, "official announcement row")
        _positive_int(self.announcement_row_id, "announcement row id")
        _fund_code(self.fund_code)
        validate_public_text(self.product_name, "announcement product name")
        _positive_int(self.listing_source_document_id, "listing source document id")
        _validate_public_https_url(self.canonical_announcement_url, "announcement URL")
        validate_public_text(self.announcement_title, "announcement title")
        validate_public_text(self.publisher, "announcement publisher")
        parsed = urlsplit(self.canonical_announcement_url)
        if FUND_COMPANY_DOMAINS.get(parsed.hostname or "") != self.publisher:
            raise ValueError("announcement publisher does not match the registered manager")
        _utc(self.published_at, "announcement publication time")
        _utc(self.integrity_checked_at, "announcement integrity check time")
        if self.integrity_checked_at < self.published_at:
            raise ValueError("announcement integrity check cannot precede publication")
        if type(self.source_tier) is not int or self.source_tier != 1:
            raise ValueError("announcement source tier must be exact tier 1")
        if self.integrity_status not in {"active", "corrected", "retracted"}:
            raise ValueError("announcement integrity status is unsupported")


@dataclass(frozen=True)
class OfficialFetchResult:
    requested_url: str
    final_url: str
    content_type: str
    payload: bytes
    retrieved_at: datetime

    def validate(self) -> None:
        _exact_dataclass(self, OfficialFetchResult, "official fetch result")
        _validate_public_https_url(self.requested_url, "requested announcement URL")
        _validate_public_https_url(self.final_url, "final announcement URL")
        validate_public_text(self.content_type, "announcement content type")
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("announcement payload must be non-empty exact bytes")
        _utc(self.retrieved_at, "announcement retrieval time")


@dataclass(frozen=True)
class OfficialListingFetchResult:
    requested_url: str
    final_url: str
    content_type: str
    payload: bytes
    retrieved_at: datetime

    def validate(self) -> None:
        _exact_dataclass(self, OfficialListingFetchResult, "official listing fetch result")
        _validate_public_https_url(self.requested_url, "requested listing URL")
        _validate_public_https_url(self.final_url, "final listing URL")
        validate_public_text(self.content_type, "listing content type")
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("official listing payload must be non-empty exact bytes")
        _utc(self.retrieved_at, "official listing retrieval time")


@dataclass(frozen=True)
class _RawOfficialHttpCapture:
    requested_url: str
    final_url: str
    content_type: str
    payload: bytes
    retrieved_at: datetime


def _clock_utc(clock: Callable[[], datetime]) -> datetime:
    value = validate_aware_datetime(clock(), "official HTTP clock")
    return value.astimezone(timezone.utc)


def _read_registered_official_html(
    *,
    requested_url: str,
    allowed_hosts: Tuple[str, ...],
    maximum_bytes: int,
    deadline_at: datetime,
    timeout_seconds: int,
    opener: Optional[object],
    clock: Callable[[], datetime],
    accept_header: str,
    user_agent: str,
    html_validation_stage: DocumentFailureStage,
) -> _RawOfficialHttpCapture:
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("official HTTP byte limit must be a positive exact integer")
    deadline = _utc(deadline_at, "official HTTP deadline")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValueError("official HTTP timeout must be a positive exact integer")
    started_at = _clock_utc(clock)
    remaining_seconds = (deadline - started_at).total_seconds()
    if remaining_seconds <= 0:
        raise TimeoutError("official HTTP deadline expired")
    _validate_registered_url(
        requested_url,
        allowed_hosts,
        redirect=False,
        stage=DocumentFailureStage.RETRIEVAL,
    )
    request_timeout = min(float(timeout_seconds), remaining_seconds)
    request_opener = opener or urllib.request.build_opener(
        _AllowedHostsRedirectHandler(
            allowed_hosts,
            stage=DocumentFailureStage.RETRIEVAL,
        )
    )
    request = urllib.request.Request(
        requested_url,
        headers={"Accept": accept_header, "User-Agent": user_agent},
        method="GET",
    )
    try:
        response = request_opener.open(request, timeout=request_timeout)
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
                        "official HTTP response returned invalid content length",
                    ) from exc
                if parsed_size < 0:
                    raise OfficialDocumentError(
                        DocumentFailureStage.RETRIEVAL,
                        DocumentFailureReason.NETWORK_UNAVAILABLE,
                        "official HTTP response returned invalid content length",
                    )
                if parsed_size > maximum_bytes:
                    raise OfficialDocumentResourceLimitError(
                        DocumentFailureStage.RETRIEVAL,
                        DocumentFailureReason.RESOURCE_LIMIT,
                        "official HTTP response exceeds byte limit",
                    )
            chunks: list[bytes] = []
            byte_count = 0
            while True:
                chunk = response.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > maximum_bytes:
                    raise OfficialDocumentResourceLimitError(
                        DocumentFailureStage.RETRIEVAL,
                        DocumentFailureReason.RESOURCE_LIMIT,
                        "official HTTP response exceeds byte limit",
                    )
                chunks.append(chunk)
    except OfficialDocumentError:
        raise
    except urllib.error.HTTPError as exc:
        raise OfficialDocumentUnavailableError(
            DocumentFailureStage.RETRIEVAL,
            DocumentFailureReason.HTTP_UNAVAILABLE,
            f"official HTTP request failed with status {exc.code}",
        ) from exc
    except TimeoutError:
        raise
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("official HTTP request timed out") from exc
        raise OfficialDocumentUnavailableError(
            DocumentFailureStage.RETRIEVAL,
            DocumentFailureReason.NETWORK_UNAVAILABLE,
            "official HTTP request failed",
        ) from exc
    except (http.client.RemoteDisconnected, ConnectionResetError, OSError) as exc:
        raise OfficialDocumentUnavailableError(
            DocumentFailureStage.RETRIEVAL,
            DocumentFailureReason.NETWORK_UNAVAILABLE,
            "official HTTP request failed",
        ) from exc
    retrieved_at = _clock_utc(clock)
    if retrieved_at >= deadline:
        raise TimeoutError("official HTTP deadline expired")
    payload = b"".join(chunks)
    detected = _validated_container_family(payload, content_type)
    if detected != "html":
        raise OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DECLARED_MIME_UNSUPPORTED,
            "official HTTP endpoint did not return HTML",
        )
    _validate_html_document(payload, stage=html_validation_stage)
    return _RawOfficialHttpCapture(
        requested_url=requested_url,
        final_url=final_url,
        content_type=content_type,
        payload=payload,
        retrieved_at=retrieved_at,
    )


class OfficialListingHttpFetcher:
    def __init__(
        self,
        *,
        deadline_at: datetime,
        opener: Optional[object] = None,
        timeout_seconds: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        policy: OfficialCheckPolicyV1 = OfficialCheckPolicyV1(),
    ) -> None:
        self._deadline_at = _utc(deadline_at, "official listing HTTP deadline")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("official listing HTTP timeout must be positive")
        if not callable(clock):
            raise ValueError("official listing HTTP clock must be callable")
        if type(policy) is not OfficialCheckPolicyV1:
            raise ValueError("official listing HTTP fetch requires exact policy V1")
        policy.validate()
        self._opener = opener
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._policy = policy

    def __call__(
        self,
        source: OfficialSourceRegistration,
        fund_code: str,
        page: int,
        maximum_bytes: int,
    ) -> OfficialListingFetchResult:
        if type(source) is not OfficialSourceRegistration:
            raise ValueError("official listing HTTP source must be exact")
        source.validate()
        _fund_code(fund_code)
        _positive_int(page, "official listing page")
        if (
            type(maximum_bytes) is not int
            or not 1 <= maximum_bytes <= self._policy.maximum_listing_page_bytes
        ):
            raise ValueError("official listing HTTP byte limit is invalid")
        if any(
            FUND_COMPANY_DOMAINS.get(host) != source.identity
            for host in source.accepted_hosts
        ):
            raise ValueError("official listing HTTP publisher binding is invalid")
        requested_url = source.index_url(fund_code, page)
        raw = _read_registered_official_html(
            requested_url=requested_url,
            allowed_hosts=source.accepted_hosts,
            maximum_bytes=maximum_bytes,
            deadline_at=self._deadline_at,
            timeout_seconds=self._timeout_seconds,
            opener=self._opener,
            clock=self._clock,
            accept_header="text/html,application/xhtml+xml",
            user_agent="KunJin/0.1 read-only official holding-review listing client",
            html_validation_stage=DocumentFailureStage.DISCOVERY,
        )
        result = OfficialListingFetchResult(
            requested_url=raw.requested_url,
            final_url=raw.final_url,
            content_type=raw.content_type,
            payload=raw.payload,
            retrieved_at=raw.retrieved_at,
        )
        result.validate()
        return result


class OfficialAnnouncementHttpFetcher:
    def __init__(
        self,
        *,
        rows: Tuple[OfficialAnnouncementRow, ...],
        deadline_at: datetime,
        opener: Optional[object] = None,
        timeout_seconds: int = 20,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        policy: HeldFundManualReviewPolicyV1 = HeldFundManualReviewPolicyV1(),
    ) -> None:
        if type(rows) is not tuple or not rows:
            raise ValueError("official announcement HTTP rows must be a non-empty tuple")
        if type(policy) is not HeldFundManualReviewPolicyV1:
            raise ValueError("official announcement HTTP fetch requires exact policy V1")
        policy.validate()
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise ValueError("official announcement HTTP timeout must be positive")
        if not callable(clock):
            raise ValueError("official announcement HTTP clock must be callable")
        first = rows[0]
        allowed_by_url: dict[str, Tuple[str, ...]] = {}
        for row in rows:
            if type(row) is not OfficialAnnouncementRow:
                raise ValueError("official announcement HTTP rows must be exact")
            try:
                row.validate()
            except ValueError as exc:
                if "publisher" in str(exc):
                    raise ValueError(
                        "official announcement HTTP publisher binding is invalid"
                    ) from exc
                raise
            if row.fund_code != first.fund_code or row.product_name != first.product_name:
                raise ValueError("official announcement HTTP rows must bind the same fund")
            if row.publisher != first.publisher:
                raise ValueError("official announcement HTTP rows must bind the same publisher")
            if row.canonical_announcement_url in allowed_by_url:
                raise ValueError("official announcement HTTP URLs must be unique")
            allowed_by_url[row.canonical_announcement_url] = validate_official_source(
                row.publisher,
                row.canonical_announcement_url,
            )
        self._deadline_at = _utc(deadline_at, "official announcement HTTP deadline")
        self._opener = opener
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._policy = policy
        self._allowed_by_url = allowed_by_url

    def __call__(self, url: str, maximum_bytes: int) -> OfficialFetchResult:
        if type(url) is not str or url not in self._allowed_by_url:
            raise ValueError("official announcement HTTP URL is not bound to the fund")
        if (
            type(maximum_bytes) is not int
            or not 1 <= maximum_bytes <= self._policy.maximum_announcement_body_bytes
        ):
            raise ValueError("official announcement HTTP byte limit is invalid")
        raw = _read_registered_official_html(
            requested_url=url,
            allowed_hosts=self._allowed_by_url[url],
            maximum_bytes=maximum_bytes,
            deadline_at=self._deadline_at,
            timeout_seconds=self._timeout_seconds,
            opener=self._opener,
            clock=self._clock,
            accept_header="text/html,application/xhtml+xml",
            user_agent="KunJin/0.1 read-only official holding-review announcement client",
            html_validation_stage=DocumentFailureStage.LANDING_VALIDATION,
        )
        result = OfficialFetchResult(
            requested_url=raw.requested_url,
            final_url=raw.final_url,
            content_type=raw.content_type,
            payload=raw.payload,
            retrieved_at=raw.retrieved_at,
        )
        result.validate()
        return result


@dataclass(frozen=True)
class OfficialListingItem:
    registration_id: str
    page_number: int
    page_local_index: int
    title: str
    canonical_url: str
    publisher: str
    published_at: Optional[datetime]

    def validate(self) -> None:
        _exact_dataclass(self, OfficialListingItem, "official listing item")
        validate_public_text(self.registration_id, "official registration id")
        _positive_int(self.page_number, "official listing page number")
        _positive_int(self.page_local_index, "official listing page-local index")
        validate_public_text(self.title, "official listing title")
        _validate_public_https_url(self.canonical_url, "official listing item URL")
        validate_public_text(self.publisher, "official listing publisher")
        if self.published_at is not None:
            _utc(self.published_at, "official listing publication time")

    def to_canonical_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "canonical_url": self.canonical_url,
            "page_local_index": self.page_local_index,
            "published_at": (
                None if self.published_at is None else self.published_at.isoformat()
            ),
            "publisher": self.publisher,
            "registration_id": self.registration_id,
            "title": self.title,
        }


@dataclass(frozen=True)
class OfficialListingPageCapture:
    registration_id: str
    page_number: int
    reported_total_pages: int
    canonical_page_url: str
    content_type: str
    raw_payload: bytes
    raw_byte_count: int
    raw_sha256: str
    retrieved_at: datetime
    parsed_items: Tuple[OfficialListingItem, ...]
    parsed_item_count: int
    parsed_items_sha256: str
    terminal_state: Optional[OfficialListingTerminalState]

    def validate(self) -> None:
        _exact_dataclass(self, OfficialListingPageCapture, "official listing page capture")
        validate_identifier(self.registration_id, "official registration id")
        if type(self.page_number) is not int or not 1 <= self.page_number <= 10:
            raise ValueError("official listing page number must be between 1 and 10")
        if (
            type(self.reported_total_pages) is not int
            or self.reported_total_pages < self.page_number
        ):
            raise ValueError("official listing reported total pages are invalid")
        _validate_public_https_url(self.canonical_page_url, "official listing page URL")
        validate_public_text(self.content_type, "official listing content type")
        if type(self.raw_payload) is not bytes or not self.raw_payload:
            raise ValueError("official listing raw payload must be non-empty exact bytes")
        if (
            type(self.raw_byte_count) is not int
            or not 1 <= self.raw_byte_count <= 2 * 1024 * 1024
        ):
            raise ValueError("official listing raw byte count is invalid")
        if self.raw_byte_count != len(self.raw_payload):
            raise ValueError("official listing raw byte count does not match payload")
        for value, name in (
            (self.raw_sha256, "official listing raw checksum"),
            (self.parsed_items_sha256, "official listing item-set checksum"),
        ):
            if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"{name} is invalid")
        if self.raw_sha256 != hashlib.sha256(self.raw_payload).hexdigest():
            raise ValueError("official listing raw checksum does not match payload")
        _utc(self.retrieved_at, "official listing retrieval time")
        if type(self.parsed_items) is not tuple:
            raise ValueError("official listing parsed items must be an exact tuple")
        for index, item in enumerate(self.parsed_items, start=1):
            if type(item) is not OfficialListingItem:
                raise ValueError("official listing parsed items must be exact")
            item.validate()
            if (
                item.registration_id != self.registration_id
                or item.page_number != self.page_number
                or item.page_local_index != index
            ):
                raise ValueError("official listing parsed item page binding is invalid")
        if (
            type(self.parsed_item_count) is not int
            or not 0 <= self.parsed_item_count <= 1000
        ):
            raise ValueError("official listing parsed item count is invalid")
        if self.parsed_item_count != len(self.parsed_items):
            raise ValueError("official listing parsed item count does not match items")
        expected_items_sha256 = hashlib.sha256(
            canonical_json_bytes(
                [item.to_canonical_dict() for item in self.parsed_items]
            )
        ).hexdigest()
        if self.parsed_items_sha256 != expected_items_sha256:
            raise ValueError("official listing parsed item checksum does not match items")
        if self.terminal_state is not None and type(self.terminal_state) is not (
            OfficialListingTerminalState
        ):
            raise ValueError("official listing terminal state is invalid")


def persistable_official_listing_items(
    capture: OfficialListingPageCapture,
) -> Tuple[OfficialListingItem, ...]:
    if type(capture) is not OfficialListingPageCapture:
        raise ValueError("persistable listing items require an exact page capture")
    capture.validate()
    return tuple(item for item in capture.parsed_items if item.published_at is not None)


def materialize_official_listing_page_evidence(
    capture: OfficialListingPageCapture,
    *,
    source_document_id: int,
) -> OfficialListingPageEvidence:
    if type(capture) is not OfficialListingPageCapture:
        raise ValueError("official listing evidence requires an exact page capture")
    capture.validate()
    _positive_int(source_document_id, "listing source document id")
    evidence = OfficialListingPageEvidence(
        registration_id=capture.registration_id,
        page_number=capture.page_number,
        reported_total_pages=capture.reported_total_pages,
        canonical_page_url=capture.canonical_page_url,
        raw_byte_count=capture.raw_byte_count,
        raw_sha256=capture.raw_sha256,
        retrieved_at=capture.retrieved_at,
        parsed_item_count=capture.parsed_item_count,
        parsed_items_sha256=capture.parsed_items_sha256,
        terminal_state=capture.terminal_state,
        source_document_id=source_document_id,
    )
    evidence.validate()
    return evidence


@dataclass(frozen=True)
class OfficialListingResult:
    matched_registration_ids: Tuple[str, ...]
    items: Tuple[OfficialListingItem, ...]
    candidate_items: Tuple[OfficialListingItem, ...]
    page_captures: Tuple[OfficialListingPageCapture, ...]
    listing_count: int
    candidate_count: int
    listing_truncated: bool
    listing_closure_complete: bool
    gap_codes: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, OfficialListingResult, "official listing result")
        if type(self.matched_registration_ids) is not tuple or any(
            type(item) is not str or not item or not item.isascii()
            for item in self.matched_registration_ids
        ):
            raise ValueError("matched official registration ids must be immutable ASCII")
        if self.matched_registration_ids != tuple(
            sorted(set(self.matched_registration_ids))
        ):
            raise ValueError("matched official registration ids must be sorted and unique")
        for values, expected, name in (
            (self.items, OfficialListingItem, "official listing items"),
            (self.candidate_items, OfficialListingItem, "official listing candidates"),
            (self.page_captures, OfficialListingPageCapture, "official listing captures"),
        ):
            if type(values) is not tuple or any(type(item) is not expected for item in values):
                raise ValueError(f"{name} must be an exact immutable tuple")
            for item in values:
                item.validate()
        matched_set = set(self.matched_registration_ids)
        if any(item.registration_id not in matched_set for item in self.items):
            raise ValueError("official listing item registration is not matched")
        if any(item.registration_id not in matched_set for item in self.page_captures):
            raise ValueError("official listing capture registration is not matched")
        item_urls = tuple(item.canonical_url for item in self.items)
        if len(item_urls) != len(set(item_urls)):
            raise ValueError("official listing result item URLs must be unique")
        candidate_urls = tuple(item.canonical_url for item in self.candidate_items)
        if len(candidate_urls) != len(set(candidate_urls)) or any(
            item not in self.items for item in self.candidate_items
        ):
            raise ValueError("official listing candidates must be unique listing items")
        if self.candidate_items != tuple(
            item for item in self.items if item.canonical_url in set(candidate_urls)
        ):
            raise ValueError("official listing candidates must preserve listing order")
        for registration_id in self.matched_registration_ids:
            pages = tuple(
                item
                for item in self.page_captures
                if item.registration_id == registration_id
            )
            page_numbers = tuple(item.page_number for item in pages)
            if page_numbers and page_numbers != tuple(range(1, len(pages) + 1)):
                raise ValueError("official listing evidence pages must be contiguous")
            terminals = tuple(
                item for item in pages if item.terminal_state in _LISTING_TERMINAL_STATES
            )
            if len(terminals) > 1 or (terminals and terminals[0] is not pages[-1]):
                raise ValueError("official listing evidence terminal must be unique and final")
        if type(self.listing_count) is not int or self.listing_count != len(self.items):
            raise ValueError("official listing count does not match items")
        if type(self.candidate_count) is not int or self.candidate_count != len(
            self.candidate_items
        ):
            raise ValueError("official listing candidate count does not match candidates")
        if type(self.listing_truncated) is not bool:
            raise ValueError("official listing truncation flag must be exact bool")
        if type(self.listing_closure_complete) is not bool:
            raise ValueError("official listing closure flag must be exact bool")
        gaps = validate_identifier_tuple(
            self.gap_codes, "official listing gap codes", allow_empty=True
        )
        if gaps != tuple(sorted(set(gaps))):
            raise ValueError("official listing gap codes must be sorted and unique")
        terminal_registrations = {
            item.registration_id
            for item in self.page_captures
            if item.terminal_state in _LISTING_TERMINAL_STATES
        }
        expected_complete = (
            bool(self.matched_registration_ids)
            and not self.gap_codes
            and not self.listing_truncated
            and terminal_registrations == set(self.matched_registration_ids)
        )
        if self.listing_closure_complete != expected_complete:
            raise ValueError("official listing closure does not match its evidence")
        if self.listing_closure_complete:
            page_urls = tuple(item.canonical_page_url for item in self.page_captures)
            raw_checksums = tuple(item.raw_sha256 for item in self.page_captures)
            if len(page_urls) != len(set(page_urls)) or len(raw_checksums) != len(
                set(raw_checksums)
            ):
                raise ValueError("complete official listing evidence must be unique")


class OfficialListingError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _listing_publication_time(value: Optional[str]) -> Optional[datetime]:
    if value is None or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise OfficialListingError("official_listing_publication_date_invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed


def _canonical_listing_url(
    base_url: str,
    href: str,
    accepted_hosts: Tuple[str, ...],
) -> str:
    resolved = urljoin(base_url, href)
    try:
        _validate_public_https_url(resolved, "official listing item URL")
    except ValueError:
        raise OfficialListingError("official_listing_item_url_invalid") from None
    host = (urlsplit(resolved).hostname or "").lower().rstrip(".")
    if host not in accepted_hosts:
        raise OfficialListingError("official_listing_item_host_mismatch")
    return resolved


class _OfficialListingHtmlParser(HTMLParser):
    def __init__(
        self,
        *,
        base_url: str,
        source: OfficialSourceRegistration,
        fund_code: str,
        product_name: str,
        page_number: int,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.source = source
        self.fund_code = fund_code
        self.product_name = product_name
        self.page_number = page_number
        self.reported_total_pages: Optional[int] = None
        self.has_next = False
        self.items: list[OfficialListingItem] = []
        self.saw_body = False
        self.saw_password_input = False
        self.page_text: list[str] = []
        self._fund_code_seen = False
        self._heading_parts: Optional[list[str]] = None
        self._product_identity: Optional[str] = None
        self._list_item_depth = 0
        self._span_parts: Optional[list[str]] = None
        self._pending_published_at: Optional[datetime] = None
        self._anchor: Optional[dict[str, Optional[str]]] = None
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        folded = tag.casefold()
        attributes = {name.casefold(): value for name, value in attrs}
        if folded == "body":
            self.saw_body = True
        if folded == "input":
            if (attributes.get("type") or "").casefold() == "password":
                self.saw_password_input = True
            if (attributes.get("id") or "").casefold() == "fundcode":
                code = (attributes.get("value") or "").strip()
                if code and code != self.fund_code:
                    raise OfficialListingError("official_listing_fund_binding_ambiguous")
                self._fund_code_seen = code == self.fund_code
        if folded == "h2" and self._product_identity is None:
            self._heading_parts = []
        if folded == "li":
            self._list_item_depth += 1
            self._pending_published_at = None
        if folded == "span" and self._list_item_depth and self._span_parts is None:
            self._span_parts = []
        total_pages = attributes.get("data-total-pages")
        if total_pages is not None:
            try:
                parsed_total = int(total_pages)
            except (TypeError, ValueError):
                raise OfficialListingError("official_listing_pagination_invalid") from None
            if parsed_total <= 0:
                raise OfficialListingError("official_listing_pagination_invalid")
            if (
                self.reported_total_pages is not None
                and self.reported_total_pages != parsed_total
            ):
                raise OfficialListingError("official_listing_pagination_conflict")
            self.reported_total_pages = parsed_total
        if folded != "a" or self._anchor is not None:
            return
        rel = (attributes.get("rel") or "").casefold().split()
        if "next" in rel:
            self.has_next = True
            return
        href = attributes.get("href")
        if not href or not self._list_item_depth:
            return
        if self.source.binds_fund_identity and (
            not self._fund_code_seen or self._product_identity is None
        ):
            return
        self._anchor = {
            "href": href,
            "published_at": attributes.get("data-published-at"),
            "title": attributes.get("title"),
        }
        self._anchor_parts = []

    def handle_data(self, data: str) -> None:
        self.page_text.append(data)
        if self._anchor is not None:
            self._anchor_parts.append(data)
        elif self._heading_parts is not None:
            self._heading_parts.append(data)
        elif self._span_parts is not None:
            self._span_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded == "h2" and self._heading_parts is not None:
            value = unicodedata.normalize("NFKC", "".join(self._heading_parts))
            self._product_identity = " ".join(value.split()) or None
            self._heading_parts = None
        if folded == "span" and self._span_parts is not None:
            value = " ".join("".join(self._span_parts).split())
            self._pending_published_at = _listing_publication_time(value)
            self._span_parts = None
        if folded == "a" and self._anchor is not None:
            raw_title = self._anchor.get("title") or "".join(self._anchor_parts)
            title = " ".join(unicodedata.normalize("NFKC", raw_title).split())
            if title:
                published = _listing_publication_time(self._anchor.get("published_at"))
                item = OfficialListingItem(
                    registration_id=self.source.registration_id,
                    page_number=self.page_number,
                    page_local_index=len(self.items) + 1,
                    title=title,
                    canonical_url=_canonical_listing_url(
                        self.base_url,
                        self._anchor["href"] or "",
                        self.source.accepted_hosts,
                    ),
                    publisher=self.source.identity,
                    published_at=published or self._pending_published_at,
                )
                item.validate()
                self.items.append(item)
            self._anchor = None
            self._anchor_parts = []
        if folded == "li" and self._list_item_depth:
            self._list_item_depth -= 1
            self._pending_published_at = None

    def validate_identity(self) -> None:
        if not self.saw_body:
            raise OfficialListingError("official_listing_container_invalid")
        if self.source.binds_fund_identity:
            if not self._fund_code_seen or self._product_identity is None:
                raise OfficialListingError("official_listing_fund_binding_ambiguous")
            expected = _compact(self.product_name)
            actual = _compact(self._product_identity)
            if expected not in actual and actual not in expected:
                raise OfficialListingError("official_listing_fund_binding_ambiguous")
        compact_text = _compact(" ".join(self.page_text)).casefold()
        if self.saw_password_input or ("登录" in compact_text and "密码" in compact_text):
            raise OfficialListingError("official_listing_login_page")
        if any(marker in compact_text for marker in ("付费后阅读", "订阅后查看")):
            raise OfficialListingError("official_listing_paywall")


@dataclass(frozen=True)
class OfficialCollectionContext:
    brief_request_run_id: int
    source_attempt_id: int
    fund_code: str
    product_name: str
    source_set_complete: bool
    window_complete: bool
    terminal_query_complete: bool
    upstream_gap_codes: Tuple[str, ...]
    deadline_at: datetime

    def validate(self) -> None:
        _exact_dataclass(self, OfficialCollectionContext, "official collection context")
        _positive_int(self.brief_request_run_id, "brief request run id")
        _positive_int(self.source_attempt_id, "source attempt id")
        _fund_code(self.fund_code)
        validate_public_text(self.product_name, "collection product name")
        for value, name in (
            (self.source_set_complete, "source set complete"),
            (self.window_complete, "window complete"),
            (self.terminal_query_complete, "terminal query complete"),
        ):
            if type(value) is not bool:
                raise ValueError(f"{name} must be an exact boolean")
        gaps = validate_identifier_tuple(
            self.upstream_gap_codes,
            "upstream official gap codes",
            allow_empty=True,
        )
        if gaps != tuple(sorted(set(gaps))):
            raise ValueError("upstream official gap codes must be sorted and unique")
        _utc(self.deadline_at, "official collection deadline")


@dataclass(frozen=True)
class OfficialEventCandidate:
    brief_request_run_id: int
    fund_code: str
    announcement_row_id: int
    normalized_content_sha256: str
    event_code: OfficialEventCode
    triggered_review_code: TriggeredReviewCode

    def validate(self) -> None:
        _exact_dataclass(self, OfficialEventCandidate, "official event candidate")
        _positive_int(self.brief_request_run_id, "brief request run id")
        _fund_code(self.fund_code)
        _positive_int(self.announcement_row_id, "announcement row id")
        if (
            type(self.normalized_content_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.normalized_content_sha256) is None
        ):
            raise ValueError("official event content checksum is invalid")
        if type(self.event_code) is not OfficialEventCode or self.event_code not in (
            _EVENT_TRIGGER_MAP
        ):
            raise ValueError("official event code is unsupported")
        if (
            type(self.triggered_review_code) is not TriggeredReviewCode
            or _EVENT_TRIGGER_MAP[self.event_code] is not self.triggered_review_code
        ):
            raise ValueError("official event trigger does not match")


@dataclass(frozen=True)
class OfficialCollectionResult:
    contents: Tuple[OfficialAnnouncementContent, ...]
    event_candidates: Tuple[OfficialEventCandidate, ...]
    official_negative_check_complete: bool
    gap_codes: Tuple[str, ...]

    def validate(self) -> None:
        _exact_dataclass(self, OfficialCollectionResult, "official collection result")
        if type(self.contents) is not tuple or type(self.event_candidates) is not tuple:
            raise ValueError("official collection records must be exact tuples")
        for item in self.contents:
            if type(item) is not OfficialAnnouncementContent:
                raise ValueError("official contents must use exact authenticated records")
            item.validate()
        for item in self.event_candidates:
            if type(item) is not OfficialEventCandidate:
                raise ValueError("official events must use exact candidate records")
            item.validate()
        if type(self.official_negative_check_complete) is not bool:
            raise ValueError("official negative check flag must be an exact boolean")
        gaps = validate_identifier_tuple(self.gap_codes, "official gap codes", allow_empty=True)
        if gaps != tuple(sorted(set(gaps))):
            raise ValueError("official gap codes must be sorted and unique")
        if self.official_negative_check_complete != (not self.gap_codes):
            raise ValueError("official negative check completeness does not match gaps")


class _AnnouncementTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._parts: list[str] = []
        self._ignored_depth = 0
        self._body_depth = 0
        self.saw_body = False
        self.saw_password_input = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag == "body":
            self.saw_body = True
            self._body_depth += 1
            return
        if normalized_tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if normalized_tag == "input" and any(
            name.casefold() == "type" and (value or "").casefold() == "password"
            for name, value in attrs
        ):
            self.saw_password_input = True
        if self._body_depth and not self._ignored_depth and normalized_tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _IGNORED_TAGS:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if normalized_tag == "body":
            self._flush()
            if self._body_depth:
                self._body_depth -= 1
            return
        if self._body_depth and not self._ignored_depth and normalized_tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._body_depth and not self._ignored_depth:
            self._parts.append(data)

    def _flush(self) -> None:
        line = " ".join("".join(self._parts).split())
        self._parts.clear()
        if line:
            self.lines.append(line)


def _content_type_parts(content_type: str) -> tuple[str, Optional[str]]:
    message = Message()
    message["content-type"] = content_type
    media_type = message.get_content_type().casefold()
    charset = message.get_content_charset()
    return media_type, None if charset is None else charset.casefold().replace("_", "-")


def _validated_encoding(payload: bytes, declared: Optional[str]) -> str:
    declared_encoding = _ENCODING_ALIASES.get(declared or "") if declared else None
    if declared is not None and declared_encoding is None:
        raise AnnouncementContentError("announcement_charset_unsupported")
    match = _META_CHARSET.search(payload[:8192])
    detected = None
    if match is not None:
        meta = match.group(1).decode("ascii").casefold().replace("_", "-")
        detected = _ENCODING_ALIASES.get(meta)
        if detected is None:
            raise AnnouncementContentError("announcement_charset_unsupported")
    if declared_encoding is not None and detected is not None and declared_encoding != detected:
        raise AnnouncementContentError("announcement_charset_conflict")
    return declared_encoding or detected or "utf-8"


def normalize_announcement_html(
    payload: bytes,
    content_type: str,
    *,
    maximum_bytes: int = 512 * 1024,
) -> str:
    if type(payload) is not bytes or not payload:
        raise AnnouncementContentError("announcement_container_invalid")
    if type(content_type) is not str or not content_type.strip():
        raise AnnouncementContentError("announcement_container_invalid")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise ValueError("announcement byte limit must be a positive exact integer")
    media_type, declared = _content_type_parts(content_type)
    lowered = payload[:8192].lstrip().lower()
    if (
        media_type not in _HTML_MEDIA_TYPES
        or b"\x00" in payload
        or lowered.startswith((b"%pdf-", b"pk\x03\x04", b"mz"))
        or b"<html" not in lowered
        or b"<body" not in payload.lower()
    ):
        raise AnnouncementContentError("announcement_container_invalid")
    encoding = _validated_encoding(payload, declared)
    try:
        text = payload.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        raise AnnouncementContentError("announcement_charset_invalid") from None
    parser = _AnnouncementTextParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise AnnouncementContentError("announcement_html_invalid") from exc
    if not parser.saw_body:
        raise AnnouncementContentError("announcement_container_invalid")
    parser._flush()
    normalized = unicodedata.normalize("NFKC", " ".join(parser.lines))
    normalized = " ".join(normalized.split())
    compact = "".join(normalized.split()).casefold()
    if parser.saw_password_input or ("登录" in compact and "密码" in compact):
        raise AnnouncementContentError("announcement_login_page")
    if any(marker in compact for marker in ("付费后阅读", "订阅后查看")):
        raise AnnouncementContentError("announcement_paywall")
    encoded = normalized.encode("utf-8")
    if not encoded:
        raise AnnouncementContentError("announcement_body_empty")
    if len(encoded) > maximum_bytes:
        raise AnnouncementContentError("announcement_body_limit")
    return normalized


def _compact(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def classify_official_listing_title(
    title: str,
    product_name: str,
    policy: OfficialCheckPolicyV1 = OfficialCheckPolicyV1(),
) -> str:
    validate_public_text(title, "official listing title")
    validate_public_text(product_name, "official listing product name")
    if type(policy) is not OfficialCheckPolicyV1:
        raise ValueError("official listing title classification requires exact policy V1")
    policy.validate()
    compact = _compact(title)
    product = _compact(product_name)
    matched = tuple(marker for marker in policy.candidate_lexemes if marker in compact)
    if not matched:
        return "ordinary"
    if any(marker in compact for marker in _LISTING_GENERIC_MARKERS):
        return "ambiguous"
    if any(marker in compact for marker in _LISTING_INTEGRITY_MARKERS):
        return "ambiguous"
    if product in compact and any(marker in compact for marker in _LISTING_NEAR_MATCH_MARKERS):
        return "ambiguous"
    return "candidate" if product in compact else "ambiguous"


def _decoded_listing_page(fetch: OfficialListingFetchResult, maximum_bytes: int) -> str:
    if len(fetch.payload) > maximum_bytes:
        raise OfficialListingError("official_listing_page_byte_limit")
    media_type, declared = _content_type_parts(fetch.content_type)
    if media_type not in _HTML_MEDIA_TYPES:
        raise OfficialListingError("official_listing_container_invalid")
    lowered = fetch.payload[:8192].lstrip().lower()
    if b"\x00" in fetch.payload or b"<html" not in lowered or b"<body" not in (
        fetch.payload.lower()
    ):
        raise OfficialListingError("official_listing_container_invalid")
    try:
        encoding = _validated_encoding(fetch.payload, declared)
    except AnnouncementContentError as exc:
        raise OfficialListingError(
            exc.reason_code.replace("announcement_", "official_listing_", 1)
        ) from None
    try:
        return fetch.payload.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        raise OfficialListingError("official_listing_charset_invalid") from None


FetchOfficialListing = Callable[
    [OfficialSourceRegistration, str, int, int], Optional[OfficialListingFetchResult]
]


class OfficialListingAcquirer:
    def __init__(
        self,
        *,
        fetch: FetchOfficialListing,
        registrations: Tuple[
            OfficialSourceRegistration, ...
        ] = OFFICIAL_SOURCE_REGISTRATIONS,
        policy: OfficialCheckPolicyV1 = OfficialCheckPolicyV1(),
        maximum_pages: Optional[int] = None,
        maximum_items: Optional[int] = None,
        maximum_page_bytes: Optional[int] = None,
    ) -> None:
        if not callable(fetch):
            raise ValueError("official listing fetch must be callable")
        if type(registrations) is not tuple:
            raise ValueError("official listing registrations must be an exact tuple")
        registration_ids: list[str] = []
        for source in registrations:
            if type(source) is not OfficialSourceRegistration:
                raise ValueError("official listing registration must be exact")
            source.validate()
            registration_ids.append(source.registration_id)
        if registration_ids != sorted(set(registration_ids)):
            raise ValueError("official listing registrations must be sorted and unique")
        if type(policy) is not OfficialCheckPolicyV1:
            raise ValueError("official listing acquisition requires exact policy V1")
        policy.validate()
        page_limit = policy.maximum_listing_pages if maximum_pages is None else maximum_pages
        item_limit = policy.maximum_listing_items if maximum_items is None else maximum_items
        byte_limit = (
            policy.maximum_listing_page_bytes
            if maximum_page_bytes is None
            else maximum_page_bytes
        )
        if (
            type(page_limit) is not int
            or not 1 <= page_limit <= policy.maximum_listing_pages
        ):
            raise ValueError("official listing page limit is invalid")
        if (
            type(item_limit) is not int
            or not 1 <= item_limit <= policy.maximum_listing_items
        ):
            raise ValueError("official listing item limit is invalid")
        if (
            type(byte_limit) is not int
            or not 1 <= byte_limit <= policy.maximum_listing_page_bytes
        ):
            raise ValueError("official listing byte limit is invalid")
        self._fetch = fetch
        self._registrations = registrations
        self._policy = policy
        self._maximum_pages = page_limit
        self._maximum_items = item_limit
        self._maximum_page_bytes = byte_limit

    @staticmethod
    def _result(
        *,
        matched: Tuple[str, ...],
        items: list[OfficialListingItem],
        captures: list[OfficialListingPageCapture],
        truncated: bool,
        gaps: set[str],
    ) -> OfficialListingResult:
        if items:
            raise ValueError("early official listing results cannot contain items")
        candidates: Tuple[OfficialListingItem, ...] = ()
        terminal_registrations = {
            item.registration_id
            for item in captures
            if item.terminal_state in _LISTING_TERMINAL_STATES
        }
        result = OfficialListingResult(
            matched_registration_ids=matched,
            items=tuple(items),
            candidate_items=candidates,
            page_captures=tuple(captures),
            listing_count=len(items),
            candidate_count=len(candidates),
            listing_truncated=truncated,
            listing_closure_complete=(
                bool(matched)
                and not gaps
                and not truncated
                and terminal_registrations == set(matched)
            ),
            gap_codes=tuple(sorted(gaps)),
        )
        result.validate()
        return result

    def collect_registered_listing(
        self,
        fund_code: str,
        manager_name: str,
        product_name: str,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> OfficialListingResult:
        _fund_code(fund_code)
        validate_public_text(manager_name, "official listing manager name")
        validate_public_text(product_name, "official listing product name")
        start = _utc(window_start, "official listing window start")
        end = _utc(window_end, "official listing window end")
        if start >= end:
            raise ValueError("official listing window must be non-empty")
        if end - start != timedelta(days=self._policy.query_window_days):
            raise ValueError("official listing window must match official check policy V1")
        matched_sources = tuple(
            source
            for source in self._registrations
            if source.source_kind == "fund_manager" and source.matches_identity(manager_name)
        )
        matched = tuple(source.registration_id for source in matched_sources)
        if not matched_sources:
            return self._result(
                matched=(),
                items=[],
                captures=[],
                truncated=False,
                gaps={"official_source_set_unsupported"},
            )

        items: list[OfficialListingItem] = []
        captures: list[OfficialListingPageCapture] = []
        gaps: set[str] = set()
        truncated = False
        seen_raw: set[str] = set()
        seen_page_urls: set[str] = set()
        seen_item_urls: set[str] = set()
        parsed_item_total = 0

        for source in matched_sources:
            if any(
                FUND_COMPANY_DOMAINS.get(host) != source.identity
                for host in source.accepted_hosts
            ):
                gaps.add("official_listing_publisher_mismatch")
                truncated = True
                continue
            page_number = 1
            reported_total_pages: Optional[int] = None
            prior_oldest: Optional[datetime] = None
            ordering_valid = True
            source_terminal = False
            while page_number <= self._maximum_pages:
                try:
                    expected_url = source.index_url(fund_code, page_number)
                except ValueError:
                    gaps.add("official_listing_pagination_invalid")
                    truncated = True
                    break
                try:
                    fetched = self._fetch(
                        source,
                        fund_code,
                        page_number,
                        self._maximum_page_bytes,
                    )
                except TimeoutError:
                    gaps.add("official_listing_timeout")
                    truncated = True
                    break
                except Exception:
                    gaps.add("official_listing_source_failed")
                    truncated = True
                    break
                if fetched is None:
                    gaps.add("official_listing_content_missing")
                    truncated = True
                    break
                try:
                    fetched.validate()
                except (TypeError, ValueError):
                    gaps.add("official_listing_fetch_invalid")
                    truncated = True
                    break
                if fetched.requested_url != expected_url:
                    gaps.add("official_listing_request_binding_invalid")
                    truncated = True
                    break
                final_host = (urlsplit(fetched.final_url).hostname or "").lower().rstrip(".")
                if final_host not in source.accepted_hosts:
                    gaps.add("official_listing_redirect_rejected")
                    truncated = True
                    break
                if fetched.final_url in seen_page_urls:
                    gaps.add("official_listing_page_duplicate")
                    truncated = True
                    break
                try:
                    text = _decoded_listing_page(fetched, self._maximum_page_bytes)
                    parser = _OfficialListingHtmlParser(
                        base_url=fetched.final_url,
                        source=source,
                        fund_code=fund_code,
                        product_name=product_name,
                        page_number=page_number,
                    )
                    parser.feed(text)
                    parser.close()
                    parser.validate_identity()
                except OfficialListingError as exc:
                    gaps.add(exc.reason_code)
                    truncated = True
                    break
                except Exception:
                    gaps.add("official_listing_html_invalid")
                    truncated = True
                    break

                page_total = parser.reported_total_pages
                if page_total is None:
                    page_total = page_number + 1 if parser.has_next else page_number
                if page_total < page_number or (parser.has_next and page_total <= page_number):
                    gaps.add("official_listing_pagination_invalid")
                    truncated = True
                    break
                if reported_total_pages is None:
                    reported_total_pages = page_total
                elif reported_total_pages != page_total:
                    gaps.add("official_listing_pagination_conflict")
                    truncated = True
                    break

                raw_sha256 = hashlib.sha256(fetched.payload).hexdigest()
                page_items = tuple(parser.items)
                parsed_items_sha256 = hashlib.sha256(
                    canonical_json_bytes([item.to_canonical_dict() for item in page_items])
                ).hexdigest()
                page_capture = OfficialListingPageCapture(
                    registration_id=source.registration_id,
                    page_number=page_number,
                    reported_total_pages=page_total,
                    canonical_page_url=fetched.final_url,
                    content_type=fetched.content_type,
                    raw_payload=fetched.payload,
                    raw_byte_count=len(fetched.payload),
                    raw_sha256=raw_sha256,
                    retrieved_at=fetched.retrieved_at,
                    parsed_items=page_items,
                    parsed_item_count=len(page_items),
                    parsed_items_sha256=parsed_items_sha256,
                    terminal_state=None,
                )
                page_capture.validate()
                captures.append(page_capture)
                seen_page_urls.add(fetched.final_url)
                if raw_sha256 in seen_raw:
                    gaps.add("official_listing_page_duplicate")
                    truncated = True
                    break
                seen_raw.add(raw_sha256)

                parsed_item_total += len(page_items)
                if parsed_item_total > self._maximum_items:
                    gaps.add("official_listing_item_cap_reached")
                    truncated = True
                    break
                dated_items = [item for item in page_items if item.published_at is not None]
                if len(dated_items) != len(page_items):
                    gaps.add("official_listing_publication_date_missing")
                page_dates = [item.published_at for item in dated_items]
                if any(left < right for left, right in zip(page_dates, page_dates[1:])):
                    gaps.add("official_listing_order_conflict")
                    ordering_valid = False
                if page_dates and prior_oldest is not None and page_dates[0] > prior_oldest:
                    gaps.add("official_listing_order_conflict")
                    ordering_valid = False
                if page_dates:
                    prior_oldest = page_dates[-1]

                for item in page_items:
                    if item.canonical_url in seen_item_urls:
                        gaps.add("official_listing_item_duplicate")
                        continue
                    seen_item_urls.add(item.canonical_url)
                    if item.published_at is None:
                        continue
                    if item.published_at >= end:
                        gaps.add("official_listing_window_future_item")
                        continue
                    if item.published_at >= start:
                        items.append(item)

                has_all_dates = len(dated_items) == len(page_items)
                crossed_boundary = (
                    bool(page_dates)
                    and has_all_dates
                    and ordering_valid
                    and page_dates[-1] < start
                )
                source_final = page_number >= page_total
                terminal_state = None
                if crossed_boundary:
                    terminal_state = OfficialListingTerminalState.WINDOW_BOUNDARY_REACHED
                elif source_final:
                    terminal_state = OfficialListingTerminalState.SOURCE_FINAL_PAGE
                if terminal_state is not None:
                    captures[-1] = replace(captures[-1], terminal_state=terminal_state)
                    source_terminal = True
                    break
                page_number += 1

            if not source_terminal:
                if page_number > self._maximum_pages:
                    gaps.add("official_listing_page_cap_reached")
                    truncated = True
                elif not truncated:
                    gaps.add("official_listing_query_incomplete")
                    truncated = True

        candidates = [
            item
            for item in items
            if classify_official_listing_title(item.title, product_name, self._policy)
            != "ordinary"
        ]
        result = OfficialListingResult(
            matched_registration_ids=matched,
            items=tuple(items),
            candidate_items=tuple(candidates),
            page_captures=tuple(captures),
            listing_count=len(items),
            candidate_count=len(candidates),
            listing_truncated=truncated,
            listing_closure_complete=(
                bool(matched)
                and not gaps
                and not truncated
                and {
                    item.registration_id
                    for item in captures
                    if item.terminal_state in _LISTING_TERMINAL_STATES
                }
                == set(matched)
            ),
            gap_codes=tuple(sorted(gaps)),
        )
        result.validate()
        return result


def _title_event(row: OfficialAnnouncementRow) -> Optional[OfficialEventCode]:
    title = _compact(row.announcement_title)
    product_name = _compact(row.product_name)
    suffix = None
    for prefix in (f"关于{product_name}", product_name):
        if title.startswith(prefix):
            suffix = title[len(prefix) :]
            break
    if suffix is None or any(marker in suffix for marker in _TITLE_EXCLUSION_MARKERS):
        return None
    for code, pattern in _TITLE_PATTERNS:
        if pattern.fullmatch(suffix):
            return code
    return None


def _title_integrity_status(row: OfficialAnnouncementRow) -> Optional[str]:
    title = _compact(row.announcement_title)
    if any(marker in title for marker in _RETRACTION_TITLE_MARKERS):
        return "retracted"
    if any(marker in title for marker in _CORRECTION_TITLE_MARKERS):
        return "corrected"
    return None


def _body_supports(
    row: OfficialAnnouncementRow,
    content: OfficialAnnouncementContent,
    event_code: OfficialEventCode,
) -> bool:
    product_name = _compact(row.product_name)
    pattern = _BODY_PATTERNS[event_code]
    for statement in re.split(r"[。！？；;]+", content.normalized_content):
        compact = _compact(statement)
        if product_name not in compact:
            continue
        for match in pattern.finditer(compact):
            window = compact[
                max(0, match.start() - 16) : min(len(compact), match.end() + 16)
            ]
            if product_name in compact[: match.start() + 1] and not any(
                marker in window for marker in _BODY_NEGATION_MARKERS
            ):
                return True
    return False


def _event_candidate(
    row: OfficialAnnouncementRow,
    content: OfficialAnnouncementContent,
) -> tuple[Optional[OfficialEventCandidate], Optional[str]]:
    event_code = _title_event(row)
    if event_code is None:
        return None, None
    if row.integrity_status != "active" or content.integrity_status != "active":
        return None, "official_event_integrity_unresolved"
    if not _body_supports(row, content, event_code):
        return None, "official_event_body_conflict"
    candidate = OfficialEventCandidate(
        brief_request_run_id=content.brief_request_run_id,
        fund_code=row.fund_code,
        announcement_row_id=row.announcement_row_id,
        normalized_content_sha256=content.normalized_content_sha256,
        event_code=event_code,
        triggered_review_code=_EVENT_TRIGGER_MAP[event_code],
    )
    candidate.validate()
    return candidate, None


def materialize_official_event_projection(
    candidate: OfficialEventCandidate,
    *,
    content: OfficialAnnouncementContent,
    announcement_content_id: int,
    policy: HeldFundManualReviewPolicyV1,
) -> HeldReviewOfficialEventProjection:
    if type(candidate) is not OfficialEventCandidate:
        raise ValueError("official event materialization requires an exact candidate")
    candidate.validate()
    if type(content) is not OfficialAnnouncementContent:
        raise ValueError("official event materialization requires exact announcement content")
    content.validate()
    if (
        content.brief_request_run_id != candidate.brief_request_run_id
        or content.fund_code != candidate.fund_code
        or content.normalized_content_sha256 != candidate.normalized_content_sha256
    ):
        raise ValueError("official event candidate does not bind the announcement content")
    _positive_int(announcement_content_id, "announcement content id")
    if type(policy) is not HeldFundManualReviewPolicyV1:
        raise ValueError("official event materialization requires exact policy V1")
    policy.validate()
    projection = HeldReviewOfficialEventProjection(
        brief_request_run_id=candidate.brief_request_run_id,
        fund_code=candidate.fund_code,
        announcement_row_id=candidate.announcement_row_id,
        announcement_content_id=announcement_content_id,
        event_code=candidate.event_code,
        triggered_review_code=candidate.triggered_review_code,
        policy_version=policy.version,
        policy_checksum=policy.checksum(),
        record_checksum="0" * 64,
    )
    projection = replace(projection, record_checksum=projection.expected_record_checksum())
    projection.validate()
    return projection


FetchOfficialAnnouncement = Callable[[str, int], Optional[OfficialFetchResult]]


class OfficialAnnouncementCollector:
    def __init__(
        self,
        fetch: FetchOfficialAnnouncement,
        policy: HeldFundManualReviewPolicyV1 = HeldFundManualReviewPolicyV1(),
        check_policy: OfficialCheckPolicyV1 = OfficialCheckPolicyV1(),
    ) -> None:
        if not callable(fetch):
            raise ValueError("official announcement fetch must be callable")
        if type(policy) is not HeldFundManualReviewPolicyV1:
            raise ValueError("official announcement collector requires exact policy V1")
        policy.validate()
        if type(check_policy) is not OfficialCheckPolicyV1:
            raise ValueError("official announcement collector requires exact check policy V1")
        check_policy.validate()
        if (
            check_policy.maximum_candidates != policy.maximum_announcement_candidates
            or check_policy.maximum_bodies != policy.maximum_announcement_candidates
        ):
            raise ValueError("official announcement candidate limits must match")
        self._fetch = fetch
        self._policy = policy
        self._check_policy = check_policy

    def collect(
        self,
        rows: Tuple[OfficialAnnouncementRow, ...],
        context: OfficialCollectionContext,
    ) -> OfficialCollectionResult:
        if type(rows) is not tuple:
            raise ValueError("official announcement rows must be an exact tuple")
        if type(context) is not OfficialCollectionContext:
            raise ValueError("official collection context must be exact")
        context.validate()
        gaps = set(context.upstream_gap_codes)
        if not context.source_set_complete:
            gaps.add("official_source_set_incomplete")
        if not context.window_complete:
            gaps.add("official_window_incomplete")
        if not context.terminal_query_complete:
            gaps.add("official_query_incomplete")

        ordered = sorted(
            rows,
            key=lambda item: (
                -item.published_at.timestamp()
                if type(item) is OfficialAnnouncementRow
                and type(item.published_at) is datetime
                else float("inf"),
                item.announcement_row_id
                if type(item) is OfficialAnnouncementRow
                and type(item.announcement_row_id) is int
                else 0,
            ),
        )
        contents: list[OfficialAnnouncementContent] = []
        events: list[OfficialEventCandidate] = []
        seen_rows: set[int] = set()
        seen_urls: set[str] = set()
        total_bytes = 0
        body_candidate_count = 0
        for row in ordered:
            if total_bytes >= self._policy.maximum_announcement_total_bytes:
                gaps.add("official_announcement_total_limit")
                break
            if type(row) is not OfficialAnnouncementRow:
                gaps.add("official_announcement_row_invalid")
                continue
            parsed = urlsplit(row.canonical_announcement_url)
            if FUND_COMPANY_DOMAINS.get(parsed.hostname or "") != row.publisher:
                gaps.add("official_announcement_publisher_mismatch")
                continue
            try:
                row.validate()
            except (TypeError, ValueError):
                gaps.add("official_announcement_row_invalid")
                continue
            if row.fund_code != context.fund_code or row.product_name != context.product_name:
                gaps.add("official_announcement_fund_binding_ambiguous")
                continue
            if row.announcement_row_id in seen_rows or row.canonical_announcement_url in seen_urls:
                gaps.add("official_announcement_candidate_conflicted")
                continue
            seen_rows.add(row.announcement_row_id)
            seen_urls.add(row.canonical_announcement_url)
            event_code = _title_event(row)
            title_classification = classify_official_listing_title(
                row.announcement_title, row.product_name, self._check_policy
            )
            high_impact_title = event_code is not None or title_classification != "ordinary"
            title_integrity_status = _title_integrity_status(row)
            if (
                high_impact_title
                and title_integrity_status is not None
                and row.integrity_status != title_integrity_status
            ):
                gaps.add("official_candidate_classification_ambiguous")
                gaps.add("official_event_integrity_unresolved")
                continue
            if event_code is None and title_classification == "ordinary":
                continue
            if body_candidate_count >= self._check_policy.maximum_candidates:
                gaps.add("official_announcement_candidate_cap_reached")
                break
            body_candidate_count += 1
            try:
                fetched = self._fetch(
                    row.canonical_announcement_url,
                    self._policy.maximum_announcement_body_bytes,
                )
            except TimeoutError:
                gaps.add("official_announcement_timeout")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            except Exception:
                gaps.add("official_announcement_source_failed")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            if fetched is None:
                gaps.add("official_announcement_content_missing")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            try:
                fetched.validate()
            except (TypeError, ValueError):
                gaps.add("official_announcement_fetch_invalid")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            if fetched.requested_url != row.canonical_announcement_url:
                gaps.add("official_announcement_request_binding_invalid")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            if fetched.final_url != row.canonical_announcement_url:
                gaps.add("official_announcement_redirect_rejected")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            if fetched.retrieved_at > context.deadline_at:
                gaps.add("official_announcement_late_result")
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                continue
            try:
                normalized = normalize_announcement_html(
                    fetched.payload,
                    fetched.content_type,
                    maximum_bytes=self._policy.maximum_announcement_body_bytes,
                )
            except AnnouncementContentError as exc:
                gaps.add(exc.reason_code)
                if high_impact_title:
                    gaps.add("official_event_body_incomplete")
                if exc.reason_code == "announcement_body_limit":
                    break
                continue
            encoded = normalized.encode("utf-8")
            if total_bytes + len(encoded) > self._policy.maximum_announcement_total_bytes:
                gaps.add("official_announcement_total_limit")
                break
            content = OfficialAnnouncementContent(
                brief_request_run_id=context.brief_request_run_id,
                source_attempt_id=context.source_attempt_id,
                fund_code=row.fund_code,
                listing_source_document_id=row.listing_source_document_id,
                canonical_announcement_url=row.canonical_announcement_url,
                announcement_title=row.announcement_title,
                announcement_published_at=row.published_at,
                publisher=row.publisher,
                normalized_content=normalized,
                normalized_content_bytes=len(encoded),
                normalized_content_sha256=hashlib.sha256(encoded).hexdigest(),
                original_source_id="fund_manager_official_documents",
                quoted_source_id=None,
                integrity_status=row.integrity_status,
                integrity_checked_at=row.integrity_checked_at,
                retrieved_at=fetched.retrieved_at,
                record_checksum="0" * 64,
            )
            content = replace(content, record_checksum=content.expected_record_checksum())
            try:
                content.validate()
            except (TypeError, ValueError):
                gaps.add("official_announcement_content_invalid")
                if event_code is not None:
                    gaps.add("official_event_body_incomplete")
                continue
            contents.append(content)
            total_bytes += content.normalized_content_bytes
            if row.integrity_status != "active" and high_impact_title:
                gaps.add("official_event_integrity_unresolved")
                continue
            if event_code is None:
                gaps.add("official_candidate_classification_ambiguous")
                continue
            candidate, event_gap = _event_candidate(row, content)
            if candidate is not None:
                events.append(candidate)
            if event_gap is not None:
                gaps.add(event_gap)

        result = OfficialCollectionResult(
            contents=tuple(contents),
            event_candidates=tuple(events),
            official_negative_check_complete=not gaps,
            gap_codes=tuple(sorted(gaps)),
        )
        result.validate()
        return result


__all__ = [
    "AnnouncementContentError",
    "OfficialAnnouncementHttpFetcher",
    "OfficialAnnouncementCollector",
    "OfficialAnnouncementRow",
    "OfficialCollectionContext",
    "OfficialCollectionResult",
    "OfficialEventCandidate",
    "OfficialFetchResult",
    "OfficialListingAcquirer",
    "OfficialListingError",
    "OfficialListingFetchResult",
    "OfficialListingHttpFetcher",
    "OfficialListingItem",
    "OfficialListingPageCapture",
    "OfficialListingResult",
    "classify_official_listing_title",
    "materialize_official_event_projection",
    "materialize_official_listing_page_evidence",
    "normalize_announcement_html",
    "persistable_official_listing_items",
]
