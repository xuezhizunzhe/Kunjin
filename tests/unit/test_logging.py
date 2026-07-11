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

    def test_redact_secrets_removes_ledger_sensitive_values(self) -> None:
        value = (
            "order_id=202607041234 card_number:6222021234567890 "
            "phone=13800138000 managed_path=/Users/person/private/import.jpg"
        )

        redacted = redact_secrets(value)

        for secret in (
            "202607041234",
            "6222021234567890",
            "13800138000",
            "/Users/person/private/import.jpg",
        ):
            self.assertNotIn(secret, redacted)
        for key in ("order_id", "card_number", "phone", "managed_path"):
            self.assertIn(f"{key}=[REDACTED]", redacted)


if __name__ == "__main__":
    unittest.main()
