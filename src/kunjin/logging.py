from __future__ import annotations

import logging
import re


_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization|token|request-sign|secret|qr(?:_url)?|"
    r"order_id|card_number|phone|managed_path)\b\s*[:=]\s*([^\s,;]+)"
)


def redact_secrets(value: str) -> str:
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(str(record.getMessage()))
        record.args = ()
        return True


def configure_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("kunjin")
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger
