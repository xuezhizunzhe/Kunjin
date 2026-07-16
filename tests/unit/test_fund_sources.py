from __future__ import annotations

import http.client
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from datetime import timezone
from email.message import Message
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kunjin.funds.models import DocumentKind
from kunjin.funds.official_domains import (
    INDEX_PROVIDER_DOMAINS,
    OFFICIAL_SOURCE_REGISTRATIONS,
    OfficialSourceRegistration,
)
from kunjin.funds.sources import (
    MAX_RESPONSE_BYTES,
    FundSourceError,
    FundTextClient,
    _build_secure_opener,
    _PinnedHTTPSConnection,
    _PinnedHTTPSHandler,
    _SameHostRedirectHandler,
    _validate_public_dns,
    build_disclosure_url,
    build_f10_url,
    classify_source,
)

PUBLIC_DNS_RESULT = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]


def make_response(
    body: bytes,
    final_url: str = "https://fundf10.eastmoney.com/jbgk_519755.html",
    content_type: str = "text/html; charset=utf-8",
):
    headers = Message()
    headers["Content-Type"] = content_type
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = lambda size=-1: body if size < 0 else body[:size]
    response.geturl.return_value = final_url
    response.headers = headers
    return response


def patch_secure_open(*, response=None, side_effect=None):
    opener = MagicMock()
    opener.open.return_value = response
    opener.open.side_effect = side_effect
    return patch("kunjin.funds.sources._build_secure_opener", return_value=opener), opener


class FundSourceUrlTest(unittest.TestCase):
    def test_builds_real_dynamic_disclosure_urls(self) -> None:
        self.assertEqual(
            build_disclosure_url(DocumentKind.SIZE_HISTORY, "519755"),
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&mode=0&code=519755",
        )
        self.assertEqual(
            build_disclosure_url(DocumentKind.QUARTERLY_HOLDINGS, "519755"),
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=519755&topline=10&year=&month=",
        )
        self.assertEqual(
            build_disclosure_url(DocumentKind.INDUSTRY_EXPOSURE, "519755", year=2026),
            "https://api.fund.eastmoney.com/f10/HYPZ/?fundCode=519755&year=2026",
        )
        self.assertEqual(
            build_disclosure_url(DocumentKind.ANNOUNCEMENT, "519755"),
            "https://api.fund.eastmoney.com/f10/JJGG?fundcode=519755&pageIndex=1&pageSize=20&type=0",
        )

    def test_builds_all_audited_f10_urls(self) -> None:
        expected_paths = {
            DocumentKind.BASIC_PROFILE: "jbgk_519755.html",
            DocumentKind.MANAGER_HISTORY: "jjjl_519755.html",
            DocumentKind.FEE_SCHEDULE: "jjfl_519755.html",
            DocumentKind.SIZE_HISTORY: "gmbd_519755.html",
            DocumentKind.QUARTERLY_HOLDINGS: "ccmx_519755.html",
            DocumentKind.INDUSTRY_EXPOSURE: "hytz_519755.html",
            DocumentKind.ANNOUNCEMENT: "jjgg_519755.html",
        }

        for kind, path in expected_paths.items():
            with self.subTest(kind=kind):
                self.assertEqual(
                    build_f10_url(kind, "519755"),
                    "https://fundf10.eastmoney.com/" + path,
                )

    def test_rejects_invalid_fund_code_and_unmapped_document_kind(self) -> None:
        with self.assertRaises(ValueError):
            build_f10_url(DocumentKind.BASIC_PROFILE, "51975")
        with self.assertRaises(ValueError):
            build_f10_url(DocumentKind.BENCHMARK, "519755")


class FundTextClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FundTextClient(timeout_seconds=3)

    def test_fetches_bounded_https_text_and_records_metadata(self) -> None:
        response = make_response(
            "基金资料".encode("gb18030"), content_type="text/html; charset=gbk"
        )
        opener_patch, opener = patch_secure_open(response=response)
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            opener_patch,
        ):
            result = self.client.fetch(
                "https://fundf10.eastmoney.com/jbgk_519755.html",
                "https://fundf10.eastmoney.com/",
            )

        self.assertEqual(result.text, "基金资料")
        self.assertEqual(result.final_url, "https://fundf10.eastmoney.com/jbgk_519755.html")
        self.assertEqual(len(result.checksum), 64)
        self.assertIs(result.retrieved_at.tzinfo, timezone.utc)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        self.assertEqual(response.read.call_args.args, (MAX_RESPONSE_BYTES + 1,))

    def test_allows_audited_api_host_without_cross_host_redirects(self) -> None:
        final_url = (
            "https://api.fund.eastmoney.com/f10/JJGG?fundcode=519755&pageIndex=1&pageSize=20&type=0"
        )
        response = make_response(
            b'{"Data":[]}', final_url=final_url, content_type="application/json"
        )
        opener_patch, _opener = patch_secure_open(response=response)
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            opener_patch,
        ):
            result = self.client.fetch(final_url, "https://fundf10.eastmoney.com/")

        self.assertEqual(result.final_url, final_url)

    def test_uses_fallback_decoding_for_unknown_charset(self) -> None:
        response = make_response(
            "基金资料".encode("gb18030"), content_type="text/html; charset=big5"
        )
        opener_patch, _opener = patch_secure_open(response=response)
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            opener_patch,
        ):
            result = self.client.fetch(
                "https://fundf10.eastmoney.com/jbgk_519755.html",
                "https://fundf10.eastmoney.com/",
            )

        self.assertEqual(result.text, "基金资料")

    def test_rejects_non_fetchable_and_unsafe_urls_before_network(self) -> None:
        urls = (
            "http://fundf10.eastmoney.com/jbgk_519755.html",
            "https://127.0.0.1/jbgk_519755.html",
            "https://localhost/jbgk_519755.html",
            "https://user:pass@fundf10.eastmoney.com/jbgk_519755.html",
            "https://example.com/jbgk_519755.html",
        )
        for url in urls:
            with (
                self.subTest(url=url),
                patch("kunjin.funds.sources._build_secure_opener") as builder,
            ):
                with self.assertRaises(FundSourceError):
                    self.client.fetch(url, "https://fundf10.eastmoney.com/")
                builder.assert_not_called()

    def test_rejects_host_when_dns_resolves_to_non_public_address(self) -> None:
        unsafe_addresses = (
            "10.0.0.1",
            "127.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "240.0.0.1",
            "0.0.0.0",
            "::1",
        )
        for address in unsafe_addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
            dns_result = [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]
            with (
                self.subTest(address=address),
                patch("kunjin.funds.sources.socket.getaddrinfo", return_value=dns_result),
                patch("kunjin.funds.sources._build_secure_opener") as builder,
            ):
                with self.assertRaises(FundSourceError):
                    self.client.fetch(
                        "https://fundf10.eastmoney.com/jbgk_519755.html",
                        "https://fundf10.eastmoney.com/",
                    )
                builder.assert_not_called()

    def test_rejects_oversized_response(self) -> None:
        response = make_response(b"x" * (MAX_RESPONSE_BYTES + 1))
        opener_patch, _opener = patch_secure_open(response=response)
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            opener_patch,
        ):
            with self.assertRaises(FundSourceError):
                self.client.fetch(
                    "https://fundf10.eastmoney.com/jbgk_519755.html",
                    "https://fundf10.eastmoney.com/",
                )

    def test_accepts_only_same_host_https_redirect(self) -> None:
        accepted = make_response(b"ok", "https://fundf10.eastmoney.com/jbgk_519755_v2.html")
        rejected_urls = (
            "http://fundf10.eastmoney.com/jbgk_519755.html",
            "https://example.com/jbgk_519755.html",
        )
        opener_patch, _opener = patch_secure_open(response=accepted)
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            opener_patch,
        ):
            self.client.fetch(
                "https://fundf10.eastmoney.com/jbgk_519755.html",
                "https://fundf10.eastmoney.com/",
            )

        for final_url in rejected_urls:
            response = make_response(b"blocked", final_url)
            opener_patch, _opener = patch_secure_open(response=response)
            with (
                self.subTest(final_url=final_url),
                patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
                opener_patch,
            ):
                with self.assertRaises(FundSourceError):
                    self.client.fetch(
                        "https://fundf10.eastmoney.com/jbgk_519755.html",
                        "https://fundf10.eastmoney.com/",
                    )

    def test_redirect_handler_rejects_malicious_second_hop_before_open(self) -> None:
        handler = _SameHostRedirectHandler("fundf10.eastmoney.com")
        request = urllib.request.Request("https://fundf10.eastmoney.com/start")
        with patch.object(
            urllib.request.HTTPRedirectHandler,
            "redirect_request",
        ) as delegated:
            with self.assertRaises(FundSourceError) as raised:
                handler.redirect_request(
                    request,
                    None,
                    302,
                    "redirect",
                    {},
                    "https://example.com/private",
                )
        self.assertEqual(raised.exception.reason_code, "unsafe_redirect")
        delegated.assert_not_called()

    def test_redirect_handler_allows_same_host_https_target(self) -> None:
        handler = _SameHostRedirectHandler("fundf10.eastmoney.com")
        request = urllib.request.Request("https://fundf10.eastmoney.com/start")
        with patch.object(
            urllib.request.HTTPRedirectHandler,
            "redirect_request",
            return_value="allowed",
        ) as delegated:
            result = handler.redirect_request(
                request,
                None,
                302,
                "redirect",
                {},
                "/next",
            )
        self.assertEqual(result, "allowed")
        delegated.assert_called_once()

    def test_secure_opener_disables_proxies_and_installs_pinned_handlers(self) -> None:
        with patch("kunjin.funds.sources.urllib.request.build_opener") as build_opener:
            _build_secure_opener("fundf10.eastmoney.com")
        handlers = build_opener.call_args.args
        proxy = next(item for item in handlers if isinstance(item, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        self.assertTrue(any(isinstance(item, _SameHostRedirectHandler) for item in handlers))
        self.assertTrue(any(isinstance(item, _PinnedHTTPSHandler) for item in handlers))

    def test_pinned_connection_connects_validated_ip_and_uses_origin_for_sni(self) -> None:
        raw_socket = MagicMock()
        raw_socket.getpeername.return_value = ("1.1.1.1", 443)
        context = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            check_hostname=True,
            post_handshake_auth=None,
            wrap_socket=MagicMock(return_value=MagicMock()),
        )
        connection = _PinnedHTTPSConnection(
            "fundf10.eastmoney.com",
            context=context,
        )
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            patch("kunjin.funds.sources.socket.socket", return_value=raw_socket),
        ):
            connection.connect()
        raw_socket.connect.assert_called_once_with(("1.1.1.1", 443))
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="fundf10.eastmoney.com",
        )

    def test_second_dns_result_cannot_connect_private_address(self) -> None:
        private_result = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))
        ]
        context = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            check_hostname=True,
            post_handshake_auth=None,
            wrap_socket=MagicMock(),
        )
        connection = _PinnedHTTPSConnection(
            "fundf10.eastmoney.com",
            context=context,
        )
        with (
            patch(
                "kunjin.funds.sources.socket.getaddrinfo",
                side_effect=(PUBLIC_DNS_RESULT, private_result),
            ) as getaddrinfo,
            patch("kunjin.funds.sources.socket.socket") as socket_factory,
        ):
            _validate_public_dns("fundf10.eastmoney.com", 443)
            with self.assertRaises(FundSourceError):
                connection.connect()
        self.assertEqual(getaddrinfo.call_count, 2)
        socket_factory.assert_not_called()

    def test_pinned_connection_closes_socket_on_peer_mismatch(self) -> None:
        raw_socket = MagicMock()
        raw_socket.getpeername.return_value = ("8.8.8.8", 443)
        context = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            check_hostname=True,
            post_handshake_auth=None,
            wrap_socket=MagicMock(),
        )
        connection = _PinnedHTTPSConnection(
            "fundf10.eastmoney.com",
            context=context,
        )
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            patch("kunjin.funds.sources.socket.socket", return_value=raw_socket),
        ):
            with self.assertRaises(FundSourceError) as raised:
                connection.connect()
        self.assertEqual(raised.exception.reason_code, "unsafe_url")
        self.assertIs(raised.exception.retryable, False)
        raw_socket.close.assert_called_once()

    def test_pinned_connection_retries_only_prevalidated_public_addresses(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
        ]
        first = MagicMock()
        first.connect.side_effect = ConnectionResetError("private detail")
        second = MagicMock()
        second.getpeername.return_value = ("8.8.8.8", 443)
        context = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            check_hostname=True,
            post_handshake_auth=None,
            wrap_socket=MagicMock(return_value=MagicMock()),
        )
        connection = _PinnedHTTPSConnection(
            "fundf10.eastmoney.com",
            context=context,
        )
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=addresses),
            patch("kunjin.funds.sources.socket.socket", side_effect=(first, second)),
        ):
            connection.connect()
        first.close.assert_called_once()
        first.connect.assert_called_once_with(("1.1.1.1", 443))
        second.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_pinned_connection_closes_socket_on_certificate_failure(self) -> None:
        raw_socket = MagicMock()
        raw_socket.getpeername.return_value = ("1.1.1.1", 443)
        certificate_error = ssl.SSLCertVerificationError("private certificate detail")
        context = SimpleNamespace(
            verify_mode=ssl.CERT_REQUIRED,
            check_hostname=True,
            post_handshake_auth=None,
            wrap_socket=MagicMock(side_effect=certificate_error),
        )
        connection = _PinnedHTTPSConnection(
            "fundf10.eastmoney.com",
            context=context,
        )
        with (
            patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
            patch("kunjin.funds.sources.socket.socket", return_value=raw_socket),
        ):
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.connect()
        raw_socket.close.assert_called_once()

    def test_failure_reasons_are_stable_and_retryable_only_when_transient(self) -> None:
        url = "https://fundf10.eastmoney.com/jbgk_519755.html"
        cases = (
            (socket.gaierror("private host detail"), "dns_failure", True),
            (TimeoutError("private timeout detail"), "network_timeout", True),
            (ConnectionResetError("private reset detail"), "transient_network_failure", True),
            (http.client.IncompleteRead(b"partial"), "transient_network_failure", True),
            (http.client.BadStatusLine("private status"), "transient_network_failure", True),
            (
                ssl.SSLCertVerificationError("private certificate detail"),
                "validation_failure",
                False,
            ),
            (
                urllib.error.URLError(
                    ssl.SSLCertVerificationError("private wrapped certificate detail")
                ),
                "validation_failure",
                False,
            ),
            (urllib.error.HTTPError(url, 404, "private header", {}, None), "http_4xx", False),
            (
                urllib.error.HTTPError(url, 503, "private server error", {}, None),
                "transient_network_failure",
                True,
            ),
            (
                urllib.error.HTTPError(url, 302, "private redirect", {}, None),
                "unsafe_redirect",
                False,
            ),
        )
        for exception, reason_code, retryable in cases:
            opener_patch, _opener = patch_secure_open(side_effect=exception)
            with (
                self.subTest(reason_code=reason_code),
                patch("kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT),
                opener_patch,
            ):
                with self.assertRaises(FundSourceError) as raised:
                    self.client.fetch(url, "https://fundf10.eastmoney.com/")
                self.assertEqual(raised.exception.code, "fund_source_error")
                self.assertEqual(raised.exception.reason_code, reason_code)
                self.assertIs(raised.exception.retryable, retryable)
                self.assertNotIn("private", str(raised.exception))

    def test_deterministic_failures_have_specific_nonretryable_reason_codes(self) -> None:
        url = "https://fundf10.eastmoney.com/jbgk_519755.html"
        cases = (
            ("unsafe_url", lambda: self.client.fetch("http://example.com", url)),
            (
                "unsafe_redirect",
                lambda: self.client.fetch(url, "https://fundf10.eastmoney.com/"),
            ),
            (
                "oversized_response",
                lambda: self.client.fetch(url, "https://fundf10.eastmoney.com/"),
            ),
            (
                "decode_failure",
                lambda: self.client.fetch(url, "https://fundf10.eastmoney.com/"),
            ),
        )
        responses = (
            None,
            make_response(b"ok", "https://example.com/redirect"),
            make_response(b"x" * (MAX_RESPONSE_BYTES + 1)),
            make_response(b"\xff\xfe\xfd", content_type="text/plain; charset=utf-8"),
        )
        for (reason_code, action), response in zip(cases, responses):
            with self.subTest(reason_code=reason_code):
                opener_patch, _opener = patch_secure_open(response=response)
                with (
                    patch(
                        "kunjin.funds.sources.socket.getaddrinfo",
                        return_value=PUBLIC_DNS_RESULT,
                    ),
                    opener_patch,
                ):
                    with self.assertRaises(FundSourceError) as raised:
                        action()
                self.assertEqual(raised.exception.reason_code, reason_code)
                self.assertIs(raised.exception.retryable, False)


class OfficialSourceClassificationTest(unittest.TestCase):
    def test_fixed_registry_separates_manager_and_index_provider_identities(self) -> None:
        manager = next(
            item for item in OFFICIAL_SOURCE_REGISTRATIONS if item.registration_id == "fund001"
        )
        self.assertEqual(manager.source_kind, "fund_manager")
        self.assertEqual(manager.identity, "交银施罗德基金管理有限公司")
        self.assertEqual(manager.identity_aliases, ("交银施罗德基金",))
        self.assertTrue(manager.binds_fund_identity)
        self.assertEqual(manager.accepted_hosts, ("www.fund001.com",))
        self.assertNotIn("eastmoney.com", manager.document_index_url_template)
        self.assertEqual(INDEX_PROVIDER_DOMAINS["www.csindex.com.cn"], "中证指数有限公司")

    def test_official_registration_is_immutable_and_has_bounded_index_shape(self) -> None:
        manager = OFFICIAL_SOURCE_REGISTRATIONS[0]
        with self.assertRaises((AttributeError, TypeError)):
            manager.accepted_hosts = ("evil.example",)  # type: ignore[misc]
        self.assertEqual(
            manager.index_url("519755", 1),
            "https://www.fund001.com/fund/519755/sxxpl.shtml",
        )
        with self.assertRaises(ValueError):
            manager.index_url("51975", 1)
        with self.assertRaises(ValueError):
            manager.index_url("519755", 0)
        with self.assertRaises(ValueError):
            manager.index_url("519755", True)

    def test_official_registration_rejects_subclasses_and_hidden_state(self) -> None:
        manager = OFFICIAL_SOURCE_REGISTRATIONS[0]

        class DerivedRegistration(OfficialSourceRegistration):
            pass

        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedRegistration(**vars(manager))
        object.__setattr__(manager, "hidden", "state")
        try:
            with self.assertRaisesRegex(ValueError, "unexpected"):
                manager.index_url("519755", 1)
        finally:
            object.__delattr__(manager, "hidden")

    def test_known_regulator_and_exchange_publishers_are_tier_one(self) -> None:
        cases = (
            ("https://www.csrc.gov.cn/csrc/c100028/doc.html", "中国证券监督管理委员会"),
            ("https://www.sse.com.cn/disclosure/doc.html", "上海证券交易所"),
            ("https://www.szse.cn/disclosure/doc.html", "深圳证券交易所"),
            ("https://www.cninfo.com.cn/new/disclosure/doc.html", "巨潮资讯网"),
        )
        for url, publisher in cases:
            with self.subTest(url=url):
                self.assertEqual(classify_source(url, publisher, "任意基金管理人"), 1)

    def test_unknown_or_mismatched_publishers_remain_tier_two(self) -> None:
        self.assertEqual(
            classify_source("https://fund.example.com/a.pdf", "示例基金", "示例基金"),
            2,
        )
        self.assertEqual(
            classify_source("https://www.sse.com.cn/a.pdf", "某基金公司", "某基金公司"),
            2,
        )

    def test_registered_fund_company_requires_exact_normalized_manager_name(self) -> None:
        official_url = "https://www.fund001.com/web/notice/519755.pdf"
        self.assertEqual(
            classify_source(
                official_url,
                " 交银施罗德基金管理有限公司 ",
                "交银施罗德基金管理有限公司",
            ),
            1,
        )
        self.assertEqual(
            classify_source(official_url, "交银施罗德基金", "交银施罗德基金管理有限公司"),
            2,
        )
        self.assertEqual(
            classify_source(official_url, "交银施罗德基金管理有限公司", "其他基金管理有限公司"),
            2,
        )

    def test_unsafe_official_links_remain_tier_two(self) -> None:
        urls = (
            "http://www.sse.com.cn/a.pdf",
            "https://user@www.sse.com.cn/a.pdf",
            "https://127.0.0.1/a.pdf",
            "https://localhost/a.pdf",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(classify_source(url, "上海证券交易所", "任意"), 2)


if __name__ == "__main__":
    unittest.main()
