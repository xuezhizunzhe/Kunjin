from __future__ import annotations

import socket
import unittest
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import MagicMock, patch

from kunjin.funds.html import FundParseError
from kunjin.funds.peers.sources import (
    MAX_DIRECTORY_ROWS,
    PEER_DIRECTORY_REFERER,
    PEER_DIRECTORY_URL,
    parse_peer_directory,
)
from kunjin.funds.sources import FundSourceError, FundTextClient, TextResponse

NOW = datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "funds" / "fundcode_search.js"
).read_text(encoding="utf-8")
PUBLIC_DNS_RESULT = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
]


def make_text_response(text: str) -> TextResponse:
    return TextResponse(
        requested_url=PEER_DIRECTORY_URL,
        final_url=PEER_DIRECTORY_URL,
        text=text,
        retrieved_at=NOW,
        checksum="b" * 64,
        content_type="application/javascript; charset=utf-8",
    )


def make_http_response(body: bytes, final_url: str):
    headers = Message()
    headers["Content-Type"] = "application/javascript; charset=utf-8"
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.side_effect = lambda size=-1: body if size < 0 else body[:size]
    response.geturl.return_value = final_url
    response.headers = headers
    return response


class PeerDirectoryParserTest(unittest.TestCase):
    def test_parses_only_the_static_var_r_json_directory(self) -> None:
        items = parse_peer_directory(make_text_response("\ufeff" + FIXTURE))

        self.assertEqual(
            [item.fund_code for item in items],
            ["519755", "000001", "000002", "000003"],
        )
        self.assertTrue(all(item.source_checksum == "b" * 64 for item in items))
        self.assertTrue(all(item.source_url == PEER_DIRECTORY_URL for item in items))

    def test_rejects_wrong_assignment_and_trailing_executable_text(self) -> None:
        invalid_documents = (
            'var x = [["519755","A","基金","混合型-灵活","B"]];',
            'var r = [["519755","A","基金","混合型-灵活","B"]];alert(1);',
            'window.r = [["519755","A","基金","混合型-灵活","B"]];',
            'var r = {"code":"519755"};',
            'var r = [NaN];',
        )
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaises(FundParseError) as context:
                parse_peer_directory(make_text_response(document))
            self.assertEqual(context.exception.code, "malformed_peer_directory")

    def test_rejects_rows_with_wrong_shape_or_non_string_values(self) -> None:
        invalid_rows = (
            '["519755","A","基金","混合型-灵活"]',
            '["519755","A","基金","混合型-灵活","B","extra"]',
            '["519755","A","基金",1,"B"]',
            '{"code":"519755"}',
        )
        for row in invalid_rows:
            document = f"var r = [{row}];"
            with self.subTest(row=row), self.assertRaises(FundParseError) as context:
                parse_peer_directory(make_text_response(document))
            self.assertEqual(context.exception.code, "malformed_peer_directory_row")

    def test_rejects_invalid_codes_and_empty_names(self) -> None:
        invalid_rows = (
            '["51975","A","基金","混合型-灵活","B"]',
            '["519755","A","","混合型-灵活","B"]',
        )
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(FundParseError) as context:
                parse_peer_directory(make_text_response(f"var r = [{row}];"))
            self.assertEqual(context.exception.code, "malformed_peer_directory_row")

    def test_skips_rows_with_empty_directory_types(self) -> None:
        document = (
            'var r = [['
            '"519755","A","正常基金","混合型-灵活","B"],['
            '"000001","C","未分类基金","","D"]];'
        )

        items = parse_peer_directory(make_text_response(document))

        self.assertEqual([item.fund_code for item in items], ["519755"])
        self.assertTrue(all(item.directory_type for item in items))

    def test_rejects_directory_above_the_row_limit(self) -> None:
        row = '["519755","A","基金","混合型-灵活","B"]'
        document = "var r = [" + ",".join([row] * (MAX_DIRECTORY_ROWS + 1)) + "];"

        with self.assertRaises(FundParseError) as context:
            parse_peer_directory(make_text_response(document))

        self.assertEqual(context.exception.code, "invalid_peer_directory_size")


class PeerDirectoryClientSecurityTest(unittest.TestCase):
    def test_allows_the_exact_audited_directory_host(self) -> None:
        response = make_http_response(FIXTURE.encode("utf-8"), PEER_DIRECTORY_URL)
        with patch(
            "kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT
        ), patch(
            "kunjin.funds.sources.urllib.request.urlopen", return_value=response
        ):
            fetched = FundTextClient().fetch(PEER_DIRECTORY_URL, PEER_DIRECTORY_REFERER)

        self.assertEqual(fetched.final_url, PEER_DIRECTORY_URL)
        self.assertEqual(len(parse_peer_directory(fetched)), 4)

    def test_rejects_cross_host_redirect_from_directory(self) -> None:
        response = make_http_response(
            FIXTURE.encode("utf-8"),
            "https://fundf10.eastmoney.com/js/fundcode_search.js",
        )
        with patch(
            "kunjin.funds.sources.socket.getaddrinfo", return_value=PUBLIC_DNS_RESULT
        ), patch(
            "kunjin.funds.sources.urllib.request.urlopen", return_value=response
        ):
            with self.assertRaises(FundSourceError):
                FundTextClient().fetch(PEER_DIRECTORY_URL, PEER_DIRECTORY_REFERER)

    def test_rejects_directory_host_when_dns_resolves_to_private_address(self) -> None:
        private_dns = [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.1", 443))
        ]
        with patch(
            "kunjin.funds.sources.socket.getaddrinfo", return_value=private_dns
        ), patch("kunjin.funds.sources.urllib.request.urlopen") as urlopen:
            with self.assertRaises(FundSourceError):
                FundTextClient().fetch(PEER_DIRECTORY_URL, PEER_DIRECTORY_REFERER)
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
