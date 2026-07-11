import logging
import unittest

from kunjin.logging import SecretRedactionFilter, redact_secrets


class LoggingTest(unittest.TestCase):
    def test_redact_secrets_removes_auth_values(self) -> None:
        value = "Authorization: abc token=secret Request-Sign: deadbeef qr_url=https://x/y"
        redacted = redact_secrets(value)

        for secret in ("abc", "secret", "deadbeef", "https://x/y"):
            self.assertNotIn(secret, redacted)

    def test_filter_redacts_formatted_message(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", ("hidden",), None)
        SecretRedactionFilter().filter(record)

        self.assertEqual(record.getMessage(), "token=[REDACTED]")


if __name__ == "__main__":
    unittest.main()

