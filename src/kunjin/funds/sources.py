from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from kunjin.funds.models import DocumentKind
from kunjin.funds.official_domains import (
    FUND_COMPANY_DOMAINS,
    REGULATOR_AND_EXCHANGE_DOMAINS,
)


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
FETCHABLE_HOSTS = frozenset({"fundf10.eastmoney.com", "api.fund.eastmoney.com"})
FUND_CODE_PATTERN = re.compile(r"^\d{6}$")

F10_PAGE_PATHS: Dict[DocumentKind, str] = {
    DocumentKind.BASIC_PROFILE: "jbgk_{code}.html",
    DocumentKind.MANAGER_HISTORY: "jjjl_{code}.html",
    DocumentKind.FEE_SCHEDULE: "jjfl_{code}.html",
    DocumentKind.SIZE_HISTORY: "gmbd_{code}.html",
    DocumentKind.QUARTERLY_HOLDINGS: "ccmx_{code}.html",
    DocumentKind.INDUSTRY_EXPOSURE: "hytz_{code}.html",
    DocumentKind.ANNOUNCEMENT: "jjgg_{code}.html",
}


class FundSourceError(RuntimeError):
    code = "fund_source_error"


@dataclass(frozen=True)
class TextResponse:
    requested_url: str
    final_url: str
    text: str
    retrieved_at: datetime
    checksum: str
    content_type: str


def build_f10_url(document_kind: DocumentKind, fund_code: str) -> str:
    if not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError(f"invalid fund code: {fund_code}")
    try:
        path = F10_PAGE_PATHS[document_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported F10 document kind: {document_kind}") from exc
    return "https://fundf10.eastmoney.com/" + path.format(code=fund_code)


def build_disclosure_url(
    document_kind: DocumentKind, fund_code: str, *, year: Optional[int] = None
) -> str:
    if not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError(f"invalid fund code: {fund_code}")
    if document_kind is DocumentKind.SIZE_HISTORY:
        return (
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            f"?type=gmbd&mode=0&code={fund_code}"
        )
    if document_kind is DocumentKind.QUARTERLY_HOLDINGS:
        return (
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            f"?type=jjcc&code={fund_code}&topline=10&year=&month="
        )
    if document_kind is DocumentKind.INDUSTRY_EXPOSURE:
        if year is None or year < 1900 or year > 9999:
            raise ValueError("industry disclosure year is required")
        return (
            "https://api.fund.eastmoney.com/f10/HYPZ/"
            f"?fundCode={fund_code}&year={year}"
        )
    if document_kind is DocumentKind.ANNOUNCEMENT:
        return (
            "https://api.fund.eastmoney.com/f10/JJGG"
            f"?fundcode={fund_code}&pageIndex=1&pageSize=20&type=0"
        )
    return build_f10_url(document_kind, fund_code)


def _normalized_name(value: Optional[str]) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(normalized.split()).casefold()


def _parsed_https_url(url: str) -> Optional[urllib.parse.ParseResult]:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return parsed
    return None


def classify_source(url: str, publisher: str, manager_name: str) -> int:
    parsed = _parsed_https_url(url)
    if parsed is None:
        return 2
    host = (parsed.hostname or "").lower().rstrip(".")
    normalized_publisher = _normalized_name(publisher)

    accepted_publishers = REGULATOR_AND_EXCHANGE_DOMAINS.get(host)
    if accepted_publishers is not None and normalized_publisher in {
        _normalized_name(name) for name in accepted_publishers
    }:
        return 1

    registered_manager = FUND_COMPANY_DOMAINS.get(host)
    if registered_manager is None:
        return 2
    expected = _normalized_name(registered_manager)
    if normalized_publisher == expected and _normalized_name(manager_name) == expected:
        return 1
    return 2


IpAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _is_disallowed_address(address: IpAddress) -> bool:
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


def _validate_public_dns(host: str, port: int) -> None:
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FundSourceError("fund source DNS lookup failed") from exc
    if not results:
        raise FundSourceError("fund source DNS lookup returned no addresses")
    for result in results:
        sockaddr = result[4]
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise FundSourceError("fund source DNS returned an invalid address") from exc
        if _is_disallowed_address(address):
            raise FundSourceError("fund source DNS resolved to a non-public address")


def _validate_fetch_url(url: str) -> urllib.parse.ParseResult:
    parsed = _parsed_https_url(url)
    if parsed is None:
        raise FundSourceError("fund source URL must be a safe HTTPS URL")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in FETCHABLE_HOSTS:
        raise FundSourceError("fund source host is not fetchable")
    return parsed


def _decode_text(payload: bytes, declared_charset: Optional[str]) -> str:
    charset = (declared_charset or "").strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "gb18030": "gb18030",
        "gbk": "gbk",
    }
    candidates = [aliases[charset]] if charset in aliases else ["utf-8", "gb18030"]
    for encoding in candidates:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise FundSourceError("fund source response text could not be decoded")


class FundTextClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    def fetch(self, url: str, referer: str) -> TextResponse:
        requested = _validate_fetch_url(url)
        referer_url = _parsed_https_url(referer)
        if referer_url is None:
            raise FundSourceError("fund source referer must use safe HTTPS")
        host = (requested.hostname or "").lower().rstrip(".")
        _validate_public_dns(host, requested.port or 443)

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
                "Referer": referer,
                "User-Agent": "KunJin/0.1 read-only fund disclosure client",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                final = _validate_fetch_url(final_url)
                final_host = (final.hostname or "").lower().rstrip(".")
                if final_host != host:
                    raise FundSourceError("fund source redirect changed host")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        parsed_length = int(content_length)
                        if parsed_length < 0:
                            raise ValueError
                        if parsed_length > MAX_RESPONSE_BYTES:
                            raise FundSourceError("fund source response exceeds size limit")
                    except ValueError as exc:
                        raise FundSourceError("fund source returned invalid content length") from exc
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise FundSourceError("fund source response exceeds size limit")
                content_type = response.headers.get("Content-Type", "")
                declared_charset = response.headers.get_content_charset()
        except FundSourceError:
            raise
        except urllib.error.HTTPError as exc:
            raise FundSourceError(f"fund source HTTP error: {exc.code}") from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            OSError,
        ) as exc:
            raise FundSourceError("fund source network request failed") from exc

        return TextResponse(
            requested_url=url,
            final_url=final_url,
            text=_decode_text(payload, declared_charset),
            retrieved_at=datetime.now(timezone.utc),
            checksum=hashlib.sha256(payload).hexdigest(),
            content_type=content_type,
        )
