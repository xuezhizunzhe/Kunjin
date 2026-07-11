import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kunjin.adapters.yangjibao import (
    DisallowedEndpointError,
    InsecureTransportError,
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


if __name__ == "__main__":
    unittest.main()
