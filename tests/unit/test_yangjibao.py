import hashlib
import json
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from kunjin.adapters.yangjibao import (
    MAX_RESPONSE_BYTES,
    DisallowedEndpointError,
    InsecureTransportError,
    RemoteResponseError,
    YangjibaoClient,
    generate_signature,
)


class FakeTokenStore:
    def __init__(self, token="token") -> None:
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = token


class YangjibaoClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = YangjibaoClient(FakeTokenStore(), signing_secret="secret")

    def fixture_data(self, name):
        path = Path(__file__).parents[1] / "fixtures" / "yangjibao" / name
        return json.loads(path.read_text())["data"]

    def test_signature_matches_known_vector(self) -> None:
        expected = hashlib.md5(b"/user_accounttoken1secret").hexdigest()
        self.assertEqual(generate_signature("/user_account", "token", 1, "secret"), expected)

    def test_plaintext_base_url_is_rejected(self) -> None:
        with self.assertRaises(InsecureTransportError):
            YangjibaoClient(FakeTokenStore(), base_url="http://example.com")

    def test_nonofficial_or_malformed_https_base_url_is_rejected(self) -> None:
        for base_url in (
            "https://example.com",
            "https://browser-plug-api.yangjibao.com:444",
            "https://browser-plug-api.yangjibao.com/path",
            "https://browser-plug-api.yangjibao.com:invalid",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(InsecureTransportError):
                    YangjibaoClient(FakeTokenStore(), base_url=base_url)

    def test_write_path_is_rejected(self) -> None:
        with self.assertRaises(DisallowedEndpointError):
            self.client._validate_path("/write_account")

    @patch.object(YangjibaoClient, "_request_json")
    def test_accounts_are_normalized(self, request_mock) -> None:
        request_mock.return_value = self.fixture_data("accounts.json")

        _, accounts = self.client.list_accounts()

        self.assertEqual(accounts[0].source_account_id, "account-1")
        self.assertEqual(accounts[0].title, "学习账户")

    @patch.object(YangjibaoClient, "_request_json")
    def test_holdings_keep_formal_and_estimated_nav_separate(self, request_mock) -> None:
        request_mock.return_value = self.fixture_data("holdings.json")

        _, positions = self.client.list_holdings("account-1")

        self.assertEqual(positions[0].fund_code, "016067")
        self.assertEqual(str(positions[0].formal_nav), "1.1000")
        self.assertEqual(str(positions[0].estimated_nav), "1.1050")
        self.assertEqual(positions[0].share_class, "A")

    @patch.object(YangjibaoClient, "_request_json")
    def test_account_and_holding_records_fail_closed(self, request_mock) -> None:
        request_mock.return_value = {"list": [{"id": "account-1"}, "malformed"]}
        with self.assertRaises(RemoteResponseError):
            self.client.list_accounts()

        request_mock.return_value = [{"code": "123456"}, "malformed"]
        with self.assertRaises(RemoteResponseError):
            self.client.list_holdings("account-1")

    @patch.object(YangjibaoClient, "_request_json")
    def test_missing_account_list_is_not_an_empty_portfolio(self, request_mock) -> None:
        request_mock.return_value = {}
        with self.assertRaisesRegex(RemoteResponseError, "list is missing"):
            self.client.list_accounts()

        request_mock.return_value = {"list": []}
        _, accounts = self.client.list_accounts()
        self.assertEqual(accounts, [])

    @patch.object(YangjibaoClient, "_request_json")
    def test_missing_holding_shares_are_not_zero(self, request_mock) -> None:
        holding = dict(self.fixture_data("holdings.json")[0])
        for missing_value in (None, ""):
            with self.subTest(missing_value=missing_value):
                holding["hold_share"] = missing_value
                request_mock.return_value = [holding]
                with self.assertRaisesRegex(RemoteResponseError, "shares are missing"):
                    self.client.list_holdings("account-1")

    def test_redirect_is_rejected_without_following_destination(self) -> None:
        error = urllib.error.HTTPError(
            self.client.base_url + "/user_account",
            302,
            "Found",
            {"Location": "https://attacker.example/steal"},
            BytesIO(),
        )
        opener = Mock()
        opener.open.side_effect = error
        self.client._opener = opener

        with self.assertRaisesRegex(RemoteResponseError, "redirect"):
            self.client._request_json("/user_account")

        opener.open.assert_called_once()
        self.assertEqual(
            opener.open.call_args.args[0].full_url,
            self.client.base_url + "/user_account",
        )

    def test_oversized_response_is_rejected_before_json_decode(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = b"x" * (MAX_RESPONSE_BYTES + 1)
        opener = Mock()
        opener.open.return_value = response
        self.client._opener = opener

        with self.assertRaisesRegex(RemoteResponseError, "exceeded"):
            self.client._request_json("/user_account")


if __name__ == "__main__":
    unittest.main()
