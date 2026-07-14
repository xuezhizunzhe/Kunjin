from __future__ import annotations

import hashlib
import io
import socket
import stat
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from kunjin.funds.models import DocumentKind, FundAnnouncement
from kunjin.funds.official_domains import OfficialSourceRegistration
from kunjin.funds.risk.documents import (
    MAX_DISCOVERY_ITEMS,
    MAX_DISCOVERY_PAGES,
    MAX_DOCUMENT_BYTES,
    OfficialDocumentCandidate,
    OfficialDocumentClient,
    OfficialDocumentDiscovery,
    OfficialDocumentError,
    OfficialDocumentIndexItem,
    OfficialDocumentIndexPage,
    OfficialDocumentResourceLimitError,
    OfficialDocumentUnavailableError,
    OfficialHtmlIndexClient,
    OfficialRedirectHandler,
    discover_candidate,
    discover_index_candidate,
    validate_official_source,
)
from kunjin.funds.risk.failures import DocumentFailureReason, DocumentFailureStage
from kunjin.paths import RuntimePaths

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
MANAGER = "交银施罗德基金管理有限公司"
PUBLIC_DNS_RESULT = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]
FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds" / "risk"


def registration() -> OfficialSourceRegistration:
    return OfficialSourceRegistration(
        registration_id="test-manager",
        identity=MANAGER,
        source_kind="fund_manager",
        accepted_hosts=("www.fund001.com",),
        document_index_url_template=(
            "https://www.fund001.com/fund/{fund_code}/documents?page={page}"
        ),
    )


def docx_bytes(text: str = "基金类型：混合型基金") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                '  <Default Extension="rels" ContentType="'
                'application/vnd.openxmlformats-package.relationships+xml"/>\n'
                '  <Default Extension="xml" ContentType="application/xml"/>\n'
                '  <Override PartName="/word/document.xml" ContentType="'
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document."
                'main+xml"/>\n'
                "</Types>"
            ),
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>"""
            + text
            + """</w:t></w:r></w:p></w:body>
</w:document>""",
        )
    return output.getvalue()


def announcement(**changes: object) -> FundAnnouncement:
    values = {
        "fund_code": "519755",
        "title": "交银示例混合型证券投资基金2026年第2季度报告",
        "category": "定期报告",
        "publisher": MANAGER,
        "published_at": NOW,
        "url": "https://www.fund001.com/web/notice/519755-q2.pdf",
        "source_tier": 2,
        "source_document_id": 1,
    }
    values.update(changes)
    return FundAnnouncement(**values)


def candidate(**changes: object) -> OfficialDocumentCandidate:
    values = {
        "fund_code": "519755",
        "document_kind": DocumentKind.QUARTERLY_REPORT,
        "title": "交银示例混合型证券投资基金2026年第2季度报告",
        "url": "https://www.fund001.com/web/notice/519755-q2.pdf",
        "publisher": MANAGER,
        "published_at": NOW,
        "source_tier": 1,
    }
    values.update(changes)
    return OfficialDocumentCandidate(**values)


class FakeIndexClient:
    def __init__(self, pages: tuple[OfficialDocumentIndexPage, ...]) -> None:
        self.pages = {page.page_number: page for page in pages}
        self.requested_pages: list[int] = []

    def fetch_page(
        self,
        source: OfficialSourceRegistration,
        fund_code: str,
        page: int,
    ) -> OfficialDocumentIndexPage:
        self.requested_pages.append(page)
        return self.pages[page]


class BytesResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://www.fund001.com/web/notice/519755-q2.pdf",
        content_type: str = "application/pdf",
        content_length: Optional[int] = None,
    ) -> None:
        self.body = body
        self.offset = 0
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "BytesResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result = self.body[self.offset :]
            self.offset = len(self.body)
            return result
        result = self.body[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def geturl(self) -> str:
        return self.url


class FakeOpener:
    def __init__(self, responses: tuple[BytesResponse, ...]) -> None:
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: int) -> BytesResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class OfficialDocumentDiscoveryTest(unittest.TestCase):
    def test_audited_manager_alias_selects_canonical_registration(self) -> None:
        source = OfficialSourceRegistration(
            registration_id="alias-manager",
            identity=MANAGER,
            identity_aliases=("交银施罗德基金",),
            source_kind="fund_manager",
            accepted_hosts=("www.fund001.com",),
            document_index_url_template=(
                "https://www.fund001.com/fund/{fund_code}/documents?page={page}"
            ),
            binds_fund_identity=True,
        )
        page = OfficialDocumentIndexPage(
            page_number=1,
            total_pages=1,
            items=(
                OfficialDocumentIndexItem(
                    title="交银示例混合型证券投资基金基金合同",
                    url="https://www.fund001.com/web/notice/contract.pdf",
                    publisher=MANAGER,
                    published_at=NOW,
                ),
            ),
        )

        result = OfficialDocumentDiscovery(
            client=FakeIndexClient((page,)),
            registrations=(source,),
        ).discover("519755", manager_name="交银施罗德基金")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].publisher, MANAGER)

        mismatch_result = OfficialDocumentDiscovery(
            client=FakeIndexClient((page,)),
            registrations=(source,),
        ).discover("519755", manager_name="错误的平台管理人")
        self.assertEqual(len(mismatch_result), 1)

    def test_strict_records_reject_bool_subclasses_and_hidden_state(self) -> None:
        with self.assertRaises(ValueError):
            candidate(source_tier=True).validate()
        with self.assertRaises(ValueError):
            OfficialDocumentIndexPage(page_number=True, total_pages=1, items=()).validate()

        class DerivedCandidate(OfficialDocumentCandidate):
            pass

        with self.assertRaisesRegex(ValueError, "subclasses"):
            DerivedCandidate(**vars(candidate())).validate()

        item = OfficialDocumentIndexItem(
            title="示例基金基金合同",
            url="https://www.fund001.com/web/notice/contract.pdf",
            publisher=MANAGER,
            published_at=NOW,
        )
        object.__setattr__(item, "hidden", "state")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            item.validate()

    def test_tier_one_announcement_discovers_only_validated_official_document(self) -> None:
        result = discover_candidate(announcement(), manager_name=MANAGER)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.document_kind, DocumentKind.QUARTERLY_REPORT)
        self.assertEqual(result.source_tier, 1)

        mirror = announcement(
            url="https://pdf.dfcfw.com/pdf/H2_AN.pdf",
            publisher=MANAGER,
            source_tier=1,
        )
        self.assertIsNone(discover_candidate(mirror, manager_name=MANAGER))

    def test_index_methodology_requires_exact_provider_and_registered_host(self) -> None:
        official = OfficialDocumentIndexItem(
            title="沪深300指数编制方案",
            url="https://www.csindex.com.cn/uploads/indices/000300-methodology.pdf",
            publisher="中证指数有限公司",
            published_at=NOW,
        )
        result = discover_index_candidate(
            official,
            fund_code="519755",
            provider_name="中证指数有限公司",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.document_kind, DocumentKind.INDEX_METHODOLOGY)
        self.assertEqual(result.source_tier, 1)

        wrong_publisher = OfficialDocumentIndexItem(
            title=official.title,
            url=official.url,
            publisher="其他指数公司",
            published_at=NOW,
        )
        mirror = OfficialDocumentIndexItem(
            title=official.title,
            url="https://pdf.dfcfw.com/pdf/index-methodology.pdf",
            publisher="中证指数有限公司",
            published_at=NOW,
        )
        non_methodology = OfficialDocumentIndexItem(
            title="沪深300指数行情说明",
            url=official.url,
            publisher=official.publisher,
            published_at=NOW,
        )
        for item in (wrong_publisher, mirror, non_methodology):
            with self.subTest(item=item):
                self.assertIsNone(
                    discover_index_candidate(
                        item,
                        fund_code="519755",
                        provider_name="中证指数有限公司",
                    )
                )

    def test_discovery_constructor_rejects_bool_and_mutable_registrations(self) -> None:
        client = FakeIndexClient(())
        for changes in (
            {"max_pages": True},
            {"max_items": True},
            {"registrations": [registration()]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                OfficialDocumentDiscovery(client=client, **changes)

    def test_ambiguous_or_mismatched_title_is_not_promoted(self) -> None:
        self.assertIsNone(
            discover_candidate(
                announcement(title="基金年度报告及招募说明书更新"),
                manager_name=MANAGER,
            )
        )
        self.assertIsNone(discover_candidate(announcement(), manager_name="其他基金公司"))

    def test_official_index_paginates_to_the_reported_final_page(self) -> None:
        pages = (
            OfficialDocumentIndexPage(
                page_number=1,
                total_pages=3,
                items=(
                    OfficialDocumentIndexItem(
                        title="交银示例混合型证券投资基金基金合同",
                        url="https://www.fund001.com/web/notice/contract.pdf",
                        publisher=MANAGER,
                        published_at=NOW,
                    ),
                ),
            ),
            OfficialDocumentIndexPage(
                page_number=2,
                total_pages=3,
                items=(
                    OfficialDocumentIndexItem(
                        title="交银示例混合型证券投资基金招募说明书更新",
                        url="https://www.fund001.com/web/notice/prospectus.pdf",
                        publisher=MANAGER,
                        published_at=NOW,
                    ),
                ),
            ),
            OfficialDocumentIndexPage(
                page_number=3,
                total_pages=3,
                items=(
                    OfficialDocumentIndexItem(
                        title="交银示例混合型证券投资基金2026年第2季度报告",
                        url="https://www.fund001.com/web/notice/q2.pdf",
                        publisher=MANAGER,
                        published_at=NOW,
                    ),
                ),
            ),
        )
        index_client = FakeIndexClient(pages)
        discovery = OfficialDocumentDiscovery(
            client=index_client,
            registrations=(registration(),),
        )

        result = discovery.discover("519755", manager_name=MANAGER)

        self.assertEqual(index_client.requested_pages, [1, 2, 3])
        self.assertEqual(
            {item.document_kind for item in result},
            {
                DocumentKind.FUND_CONTRACT,
                DocumentKind.PROSPECTUS_UPDATE,
                DocumentKind.QUARTERLY_REPORT,
            },
        )
        self.assertTrue(all(item.source_tier == 1 for item in result))

    def test_discovery_caps_pages_items_and_rejects_page_number_mismatch(self) -> None:
        too_many_pages = FakeIndexClient(
            (
                OfficialDocumentIndexPage(
                    page_number=1,
                    total_pages=MAX_DISCOVERY_PAGES + 1,
                    items=(),
                ),
            )
        )
        with self.assertRaisesRegex(OfficialDocumentError, "page limit"):
            OfficialDocumentDiscovery(
                client=too_many_pages,
                registrations=(registration(),),
            ).discover("519755", manager_name=MANAGER)

        too_many_items = FakeIndexClient(
            (
                OfficialDocumentIndexPage(
                    page_number=1,
                    total_pages=1,
                    items=tuple(
                        OfficialDocumentIndexItem(
                            title=f"示例基金2026年第2季度报告 {index}",
                            url=f"https://www.fund001.com/web/notice/{index}.pdf",
                            publisher=MANAGER,
                            published_at=NOW,
                        )
                        for index in range(MAX_DISCOVERY_ITEMS + 1)
                    ),
                ),
            )
        )
        with self.assertRaisesRegex(OfficialDocumentError, "item limit"):
            OfficialDocumentDiscovery(
                client=too_many_items,
                registrations=(registration(),),
            ).discover("519755", manager_name=MANAGER)

        mismatch = FakeIndexClient(
            (OfficialDocumentIndexPage(page_number=2, total_pages=2, items=()),)
        )
        mismatch.pages[1] = mismatch.pages.pop(2)
        with self.assertRaisesRegex(OfficialDocumentError, "page sequence"):
            OfficialDocumentDiscovery(
                client=mismatch,
                registrations=(registration(),),
            ).discover("519755", manager_name=MANAGER)

    def test_fixture_is_a_bounded_public_official_index(self) -> None:
        text = (FIXTURES / "official-index.html").read_text(encoding="utf-8")
        self.assertIn('data-total-pages="3"', text)
        self.assertIn("基金合同", text)
        self.assertNotIn("amount", text.casefold())

    def test_html_index_client_parses_reported_pagination_and_official_links(self) -> None:
        source = registration()
        response = BytesResponse(
            (FIXTURES / "official-index.html").read_bytes(),
            url=source.index_url("519755", 1),
            content_type="text/html; charset=utf-8",
        )
        opener = FakeOpener((response,))
        client = OfficialHtmlIndexClient(opener=opener, timeout_seconds=3)
        with patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            page = client.fetch_page(source, "519755", 1)

        self.assertEqual(page.page_number, 1)
        self.assertEqual(page.total_pages, 3)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(page.items[0].publisher, MANAGER)
        self.assertEqual(
            page.items[0].url,
            "https://www.fund001.com/web/notice/contract.pdf",
        )

    def test_index_html_shell_failures_retain_discovery_stage(self) -> None:
        source = registration()
        cases = (
            (
                (FIXTURES / "authentication-shell.html").read_bytes(),
                DocumentFailureReason.AUTHENTICATION_SHELL,
            ),
            (
                b"<!doctype html><html><script>render()</script></html>",
                DocumentFailureReason.EMPTY_OR_SCRIPT_ONLY_HTML,
            ),
        )

        for body, expected_reason in cases:
            response = BytesResponse(
                body,
                url=source.index_url("519755", 1),
                content_type="text/html; charset=utf-8",
            )
            client = OfficialHtmlIndexClient(opener=FakeOpener((response,)))
            with (
                self.subTest(expected_reason=expected_reason),
                patch(
                    "kunjin.funds.risk.documents.socket.getaddrinfo",
                    return_value=PUBLIC_DNS_RESULT,
                ),
                self.assertRaises(OfficialDocumentError) as caught,
            ):
                client.fetch_page(source, "519755", 1)

            self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.DISCOVERY)
            self.assertEqual(caught.exception.failure.reason_code, expected_reason)

    def test_live_shaped_index_scopes_product_and_keeps_adjacent_date(self) -> None:
        source = OfficialSourceRegistration(
            registration_id="live-shaped",
            identity=MANAGER,
            source_kind="fund_manager",
            accepted_hosts=("www.fund001.com",),
            document_index_url_template=("https://www.fund001.com/fund/{fund_code}/sxxpl.shtml"),
            binds_fund_identity=True,
        )
        body = (
            '<!doctype html><html><body><a href="http://www.jyamc.com">交银施罗德资管</a>'
            '<input type="hidden" id="fundcode" value="519706"/>'
            "<h2>交银施罗德深证300价值交易型开放式指数证券投资基金联接基金</h2>"
            '<li><span>2026-04-21</span><a title="交银施罗德深证300价值交易型开放式指数'
            '证券投资基金联接基金2026年第1季度报告" href="/news/2026-04-21/70484_1.shtml">'
            "目标基金季报</a></li>"
            '<li><span>2026-04-21</span><a title="深证300价值交易型开放式指数证券投资基金'
            '2026年第1季度报告" href="/news/2026-04-21/70461_1.shtml">关联ETF季报</a></li>'
            "</body></html>"
        ).encode()
        client = OfficialHtmlIndexClient(
            opener=FakeOpener(
                (
                    BytesResponse(
                        body,
                        url=source.index_url("519706", 1),
                        content_type="text/html; charset=utf-8",
                    ),
                )
            )
        )
        with patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            page = client.fetch_page(source, "519706", 1)

        self.assertEqual(len(page.items), 1)
        self.assertIn("联接基金", page.items[0].title)
        self.assertEqual(page.items[0].published_at, datetime(2026, 4, 21, tzinfo=timezone.utc))
        self.assertEqual(
            page.items[0].url,
            "https://www.fund001.com/news/2026/04/21/70484/1.shtml",
        )

    def test_identity_bound_index_rejects_soft_404_without_code_and_product_heading(self) -> None:
        source = OfficialSourceRegistration(
            registration_id="identity-bound",
            identity=MANAGER,
            source_kind="fund_manager",
            accepted_hosts=("www.fund001.com",),
            document_index_url_template=("https://www.fund001.com/fund/{fund_code}/sxxpl.shtml"),
            binds_fund_identity=True,
        )
        response = BytesResponse(
            b'<!doctype html><a title="public fund contract" href="/contract.pdf">contract</a>',
            url=source.index_url("519755", 1),
            content_type="text/html",
        )
        with (
            patch(
                "kunjin.funds.risk.documents.socket.getaddrinfo",
                return_value=PUBLIC_DNS_RESULT,
            ),
            self.assertRaisesRegex(OfficialDocumentError, "identity"),
        ):
            OfficialHtmlIndexClient(opener=FakeOpener((response,))).fetch_page(source, "519755", 1)

    def test_identity_bound_index_filters_share_classes_and_accepts_common_documents(self) -> None:
        source = OfficialSourceRegistration(
            registration_id="share-bound",
            identity=MANAGER,
            source_kind="fund_manager",
            accepted_hosts=("www.fund001.com",),
            document_index_url_template=("https://www.fund001.com/fund/{fund_code}/sxxpl.shtml"),
            binds_fund_identity=True,
        )
        body = (
            '<!doctype html><input id="fundcode" value="519755"/>'
            "<h2>交银示例混合型证券投资基金A</h2>"
            '<li><span>2026-06-12</span><a title="交银示例混合型证券投资基金招募说明书更新" '
            'href="/news/2026-06-12/1_1.shtml">common</a></li>'
            '<li><span>2026-06-12</span><a title="交银示例混合型证券投资基金（A类份额）'
            '基金产品资料概要更新" href="/news/2026-06-12/2_1.shtml">A</a></li>'
            '<li><span>2026-06-12</span><a title="交银示例混合型证券投资基金（C类份额）'
            '基金产品资料概要更新" href="/news/2026-06-12/3_1.shtml">C</a></li>'
        ).encode()
        response = BytesResponse(
            body,
            url=source.index_url("519755", 1),
            content_type="text/html; charset=utf-8",
        )
        with patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            page = OfficialHtmlIndexClient(opener=FakeOpener((response,))).fetch_page(
                source, "519755", 1
            )

        self.assertEqual(
            [item.title for item in page.items],
            [
                "交银示例混合型证券投资基金招募说明书更新",
                "交银示例混合型证券投资基金（A类份额）基金产品资料概要更新",
            ],
        )

    def test_single_page_registration_never_requests_a_second_page(self) -> None:
        source = OfficialSourceRegistration(
            registration_id="single-page",
            identity=MANAGER,
            source_kind="fund_manager",
            accepted_hosts=("www.fund001.com",),
            document_index_url_template="https://www.fund001.com/fund/{fund_code}/index.shtml",
        )
        index_client = FakeIndexClient(
            (OfficialDocumentIndexPage(page_number=1, total_pages=2, items=()),)
        )
        with self.assertRaisesRegex(OfficialDocumentError, "single-page"):
            OfficialDocumentDiscovery(
                client=index_client,
                registrations=(source,),
            ).discover("519755", manager_name=MANAGER)
        self.assertEqual(index_client.requested_pages, [1])


class OfficialDocumentClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.paths = RuntimePaths(
            database=root / "data" / "kunjin.db",
            snapshots=root / "data" / "snapshots",
            logs=root / "state" / "logs",
        ).ensure()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_official_source_validation_binds_exact_publisher_to_host_allowlist(self) -> None:
        self.assertEqual(
            validate_official_source(MANAGER, candidate().url),
            ("www.fund001.com",),
        )
        for publisher, url in (
            (MANAGER + " ", candidate().url),
            ("evil publisher", candidate().url),
            (MANAGER, "https://www.efunds.com.cn/document.pdf"),
        ):
            with self.subTest(publisher=publisher, url=url):
                with self.assertRaisesRegex(OfficialDocumentError, "not registered"):
                    validate_official_source(publisher, url)

    def fetch(self, response: BytesResponse):
        client = OfficialDocumentClient(
            paths=self.paths,
            opener=FakeOpener((response,)),
            timeout_seconds=3,
            clock=lambda: NOW,
        )
        with patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            return client.fetch(candidate())

    def test_document_errors_carry_allowlisted_failure_without_changing_public_code(self) -> None:
        error = OfficialDocumentError(
            DocumentFailureStage.CONTAINER_VALIDATION,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
            "private mismatch detail",
        )

        self.assertEqual(error.code, "official_document_invalid")
        self.assertEqual(error.failure.public_code, error.code)
        self.assertEqual(error.failure.stage, DocumentFailureStage.CONTAINER_VALIDATION)
        self.assertEqual(
            error.failure.reason_code,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
        )
        error.failure.validate()

    def test_redirect_to_unregistered_host_is_rejected_before_following(self) -> None:
        handler = OfficialRedirectHandler(candidate())
        request = urllib.request.Request(candidate().url)
        with (
            patch(
                "kunjin.funds.risk.documents.socket.getaddrinfo",
                return_value=PUBLIC_DNS_RESULT,
            ),
            patch.object(
                urllib.request.HTTPRedirectHandler,
                "redirect_request",
                wraps=urllib.request.HTTPRedirectHandler.redirect_request,
            ) as parent,
        ):
            with self.assertRaisesRegex(OfficialDocumentError, "redirect"):
                handler.redirect_request(
                    request,
                    MagicMock(),
                    302,
                    "Found",
                    Message(),
                    "https://evil.example/a.pdf",
                )
            parent.assert_not_called()

    def test_final_url_is_revalidated_for_injected_openers(self) -> None:
        response = BytesResponse(b"%PDF-1.7\npublic", url="https://evil.example/a.pdf")
        client = OfficialDocumentClient(
            paths=self.paths,
            opener=FakeOpener((response,)),
            clock=lambda: NOW,
        )
        with (
            patch(
                "kunjin.funds.risk.documents.socket.getaddrinfo",
                return_value=PUBLIC_DNS_RESULT,
            ),
            self.assertRaisesRegex(OfficialDocumentError, "redirect"),
        ):
            client.fetch(candidate())

    def test_managed_artifact_is_private_checksum_named_and_idempotent(self) -> None:
        body = b"%PDF-1.7\nsynthetic public document\n%%EOF"
        first = self.fetch(BytesResponse(body))
        second = self.fetch(BytesResponse(body))

        expected = hashlib.sha256(body).hexdigest()
        self.assertEqual(first.sha256, expected)
        self.assertEqual(first.managed_path.name, expected + ".pdf")
        self.assertEqual(first.managed_path, second.managed_path)
        self.assertEqual(stat.S_IMODE(first.managed_path.stat().st_mode), 0o600)
        self.assertEqual(first.managed_path.read_bytes(), body)
        self.assertEqual(tuple(self.paths.fund_documents.glob(".partial-*")), ())

    def test_existing_checksum_path_rejects_symbolic_link(self) -> None:
        body = b"%PDF-1.7\nsynthetic public document\n%%EOF"
        digest = hashlib.sha256(body).hexdigest()
        target = self.paths.database.parent / "outside-artifact.pdf"
        target.write_bytes(body)
        managed_path = self.paths.fund_documents / (digest + ".pdf")
        managed_path.symlink_to(target)

        with self.assertRaisesRegex(OfficialDocumentError, "symbolic link") as raised:
            self.fetch(BytesResponse(body))

        self.assertEqual(raised.exception.code, "official_document_invalid")
        self.assertTrue(managed_path.is_symlink())
        self.assertEqual(tuple(self.paths.fund_documents.glob(".partial-*")), ())

    def test_accepts_matching_html_and_uses_html_extension(self) -> None:
        body = b"<!doctype html><html><body><h1>Public fund contract</h1></body></html>"
        artifact = self.fetch(BytesResponse(body, content_type="text/html; charset=utf-8"))
        self.assertEqual(artifact.managed_path.suffix, ".html")
        self.assertEqual(artifact.content_type, "text/html; charset=utf-8")

    def test_resolves_one_same_host_landing_attachment_and_accepts_docx_magic(self) -> None:
        landing_url = "https://www.fund001.com/news/2026/06/12/70995/1.shtml"
        attachment_url = "https://www.fund001.com/upload/2026/06/12/current.doc"
        published_at = datetime(2026, 6, 12, tzinfo=timezone.utc)
        title = "交银示例混合型证券投资基金招募说明书更新"
        landing = BytesResponse(
            (
                "<!doctype html><html><body><h1>"
                + title
                + '</h1><p>时间：2026-06-12</p><a href="/upload/2026/06/12/'
                'current.doc">click</a></body></html>'
            ).encode(),
            url=landing_url,
            content_type="text/html; charset=utf-8",
        )
        attachment = BytesResponse(
            docx_bytes(),
            url=attachment_url,
            content_type="application/msword",
        )
        opener = FakeOpener((landing, attachment))
        client = OfficialDocumentClient(paths=self.paths, opener=opener, clock=lambda: NOW)
        with patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ):
            artifact = client.fetch(
                candidate(
                    url=landing_url,
                    document_kind=DocumentKind.PROSPECTUS_UPDATE,
                    title=title,
                    published_at=published_at,
                )
            )

        self.assertEqual(artifact.final_url, attachment_url)
        self.assertEqual(artifact.managed_path.suffix, ".docx")
        self.assertEqual(artifact.content_type, "application/msword")
        self.assertEqual(len(opener.requests), 2)

    def test_landing_page_rejects_multiple_or_cross_host_attachments(self) -> None:
        landing_url = "https://www.fund001.com/news/2026/06/12/70995/1.shtml"
        title = "交银示例混合型证券投资基金2026年第2季度报告"
        prefix = ("<!doctype html><h1>" + title + "</h1><p>时间：2026-07-13</p>").encode()
        cases = (
            prefix + b'<a href="/a.doc">a</a><a href="/b.docx">b</a>',
            prefix + b'<a href="https://evil.example/a.docx">a</a>',
        )
        expected_reasons = (
            DocumentFailureReason.ATTACHMENT_AMBIGUOUS,
            DocumentFailureReason.ATTACHMENT_HOST_REJECTED,
        )
        for body, expected_reason in zip(cases, expected_reasons):
            opener = FakeOpener((BytesResponse(body, url=landing_url, content_type="text/html"),))
            client = OfficialDocumentClient(paths=self.paths, opener=opener, clock=lambda: NOW)
            with (
                self.subTest(body=body),
                patch(
                    "kunjin.funds.risk.documents.socket.getaddrinfo",
                    return_value=PUBLIC_DNS_RESULT,
                ),
                self.assertRaises(OfficialDocumentError) as caught,
            ):
                client.fetch(candidate(url=landing_url))
            self.assertEqual(
                caught.exception.failure.stage,
                DocumentFailureStage.LANDING_VALIDATION,
            )
            self.assertEqual(caught.exception.failure.reason_code, expected_reason)

    def test_landing_title_mismatch_has_safe_failure(self) -> None:
        landing_url = "https://www.fund001.com/news/2026/06/12/70995/1.shtml"
        response = BytesResponse(
            b'<!doctype html><h1>wrong title</h1><a href="/document.pdf">document</a>',
            url=landing_url,
            content_type="text/html",
        )

        with self.assertRaises(OfficialDocumentError) as caught:
            self.fetch(response)

        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.LANDING_VALIDATION)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LANDING_TITLE_MISMATCH,
        )

    def test_msword_ole_is_authenticated_and_persisted_as_retrieved_artifact(self) -> None:
        legacy_doc = bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic legacy document"
        artifact = self.fetch(BytesResponse(legacy_doc, content_type="application/msword"))

        expected = hashlib.sha256(legacy_doc).hexdigest()
        self.assertEqual(artifact.sha256, expected)
        self.assertEqual(artifact.managed_path.name, expected + ".doc")
        self.assertEqual(artifact.managed_path.read_bytes(), legacy_doc)
        self.assertEqual(stat.S_IMODE(artifact.managed_path.stat().st_mode), 0o600)

    def test_ooxml_only_mime_with_ole_payload_is_rejected_as_mismatch(self) -> None:
        legacy_doc = bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic legacy document"
        with self.assertRaises(OfficialDocumentError) as caught:
            self.fetch(
                BytesResponse(
                    legacy_doc,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    ),
                )
            )

        self.assertEqual(caught.exception.code, "official_document_invalid")
        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.CONTAINER_VALIDATION)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
        )
        self.assertEqual(tuple(self.paths.fund_documents.iterdir()), ())

    def test_rejects_mime_mismatch_authentication_shell_and_binary(self) -> None:
        cases = (
            BytesResponse(
                b"<!doctype html><html><body>document</body></html>",
                content_type="application/pdf",
            ),
            BytesResponse(
                (FIXTURES / "authentication-shell.html").read_bytes(),
                content_type="text/html",
            ),
            BytesResponse(
                (FIXTURES / "mime-mismatch.bin").read_bytes(),
                content_type="application/pdf",
            ),
        )
        for response in cases:
            with self.subTest(content_type=response.headers["Content-Type"]):
                with self.assertRaises(OfficialDocumentError):
                    self.fetch(response)
        self.assertEqual(tuple(self.paths.fund_documents.iterdir()), ())

    def test_rejects_declared_and_streamed_oversize_without_artifact(self) -> None:
        declared = BytesResponse(
            b"%PDF-1.7\nsmall",
            content_length=MAX_DOCUMENT_BYTES + 1,
        )
        streamed = BytesResponse(b"%PDF-1.7\n" + b"x" * MAX_DOCUMENT_BYTES)
        for response in (declared, streamed):
            with self.subTest(declared=response.headers.get("Content-Length")):
                with self.assertRaisesRegex(OfficialDocumentError, "size limit") as raised:
                    self.fetch(response)
                self.assertEqual(raised.exception.code, "official_document_resource_limit")
                self.assertEqual(raised.exception.failure.stage, DocumentFailureStage.RETRIEVAL)
                self.assertEqual(
                    raised.exception.failure.reason_code,
                    DocumentFailureReason.RESOURCE_LIMIT,
                )
        self.assertEqual(tuple(self.paths.fund_documents.iterdir()), ())

    def test_error_codes_distinguish_invalid_unavailable_and_resource_limit(self) -> None:
        with self.assertRaises(OfficialDocumentError) as invalid:
            self.fetch(
                BytesResponse(
                    b"<!doctype html><html><body>document</body></html>",
                    content_type="application/pdf",
                )
            )
        self.assertEqual(invalid.exception.code, "official_document_invalid")
        self.assertEqual(invalid.exception.failure.stage, DocumentFailureStage.CONTAINER_VALIDATION)
        self.assertEqual(
            invalid.exception.failure.reason_code,
            DocumentFailureReason.DECLARED_DETECTED_MISMATCH,
        )

        opener = FakeOpener((BytesResponse(b"%PDF-1.7\npublic"),))
        client = OfficialDocumentClient(paths=self.paths, opener=opener, clock=lambda: NOW)
        with (
            patch(
                "kunjin.funds.risk.documents.socket.getaddrinfo",
                side_effect=socket.gaierror("synthetic lookup failure"),
            ),
            self.assertRaises(OfficialDocumentUnavailableError) as unavailable,
        ):
            client.fetch(candidate())
        self.assertEqual(unavailable.exception.code, "official_document_unavailable")
        self.assertEqual(unavailable.exception.failure.stage, DocumentFailureStage.RETRIEVAL)
        self.assertEqual(
            unavailable.exception.failure.reason_code,
            DocumentFailureReason.DNS_UNAVAILABLE,
        )

        self.assertTrue(issubclass(OfficialDocumentResourceLimitError, OfficialDocumentError))

    def test_http_failure_has_safe_failure(self) -> None:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.HTTPError(
            candidate().url,
            503,
            "synthetic upstream detail",
            Message(),
            None,
        )
        client = OfficialDocumentClient(paths=self.paths, opener=opener, clock=lambda: NOW)
        with (
            patch(
                "kunjin.funds.risk.documents.socket.getaddrinfo",
                return_value=PUBLIC_DNS_RESULT,
            ),
            self.assertRaises(OfficialDocumentUnavailableError) as caught,
        ):
            client.fetch(candidate())

        self.assertEqual(caught.exception.code, "official_document_unavailable")
        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.RETRIEVAL)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.HTTP_UNAVAILABLE,
        )

    def test_rejects_non_public_dns_before_open(self) -> None:
        opener = FakeOpener((BytesResponse(b"%PDF-1.7\npublic"),))
        client = OfficialDocumentClient(paths=self.paths, opener=opener, clock=lambda: NOW)
        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        ]
        with (
            patch("kunjin.funds.risk.documents.socket.getaddrinfo", return_value=private_dns),
            self.assertRaisesRegex(OfficialDocumentError, "non-public"),
        ):
            client.fetch(candidate())
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
