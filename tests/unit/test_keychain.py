import subprocess
import unittest
from unittest.mock import patch

from kunjin.security.keychain import KeychainTokenStore


class KeychainTest(unittest.TestCase):
    @patch("kunjin.security.keychain.subprocess.run")
    def test_save_uses_non_shell_update_command(self, run_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        store = KeychainTokenStore()

        store.save("synthetic-token")

        command = run_mock.call_args.args[0]
        self.assertEqual(
            command[:6],
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                "com.kunjin.yangjibao",
                "-a",
            ],
        )
        self.assertFalse(run_mock.call_args.kwargs["shell"])

    @patch("kunjin.security.keychain.subprocess.run")
    def test_missing_token_returns_none(self, run_mock) -> None:
        run_mock.side_effect = subprocess.CalledProcessError(44, ["security"], stderr="missing")

        self.assertIsNone(KeychainTokenStore().load())

    @patch("kunjin.security.keychain.subprocess.run")
    def test_delete_is_idempotent(self, run_mock) -> None:
        run_mock.side_effect = subprocess.CalledProcessError(44, ["security"], stderr="missing")

        KeychainTokenStore().delete()


if __name__ == "__main__":
    unittest.main()
