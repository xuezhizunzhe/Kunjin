from __future__ import annotations

import json
import os
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from kunjin.ledger.models import OcrBlock


BLOCK_KEYS = frozenset({"text", "confidence", "x", "y", "width", "height"})


def _reject_json_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def _bounded_decimal(
    row: object,
    name: str,
    *,
    lower_inclusive: bool,
) -> Decimal:
    if not isinstance(row, dict):
        raise TypeError("block must be an object")
    value = row[name]
    if type(value) is not Decimal or not value.is_finite():
        raise TypeError("block coordinate must be a decimal")
    if lower_inclusive:
        valid = Decimal("0") <= value <= Decimal("1")
    else:
        valid = Decimal("0") < value <= Decimal("1")
    if not valid:
        raise ValueError("block coordinate is out of range")
    return value


class OcrError(RuntimeError):
    code = "ocr_error"


class OcrUnavailableError(OcrError):
    code = "ocr_unavailable"


class OcrResponseError(OcrError):
    code = "ocr_response_error"


class VisionOcrClient:
    def __init__(
        self,
        swift_path: Path = Path("/usr/bin/swift"),
        helper_path: Optional[Path] = None,
        timeout_seconds: int = 60,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.swift_path = swift_path
        self.helper_path = helper_path or Path(__file__).with_name("vision_ocr.swift")
        self.timeout_seconds = timeout_seconds
        self.cache_dir = cache_dir or (
            Path(tempfile.gettempdir()) / f"kunjin-swift-cache-{os.getuid()}"
        )

    def recognize(self, image_path: Path) -> List[OcrBlock]:
        if not self.swift_path.is_file() or not self.helper_path.is_file():
            raise OcrUnavailableError("local Apple Vision OCR is unavailable")
        if not image_path.is_file():
            raise OcrResponseError("image file is unavailable")

        try:
            clang_cache = self.cache_dir / "clang"
            swift_cache = self.cache_dir / "swift"
            for directory in (self.cache_dir, clang_cache, swift_cache):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                directory.chmod(0o700)
            environment = os.environ.copy()
            environment["CLANG_MODULE_CACHE_PATH"] = str(clang_cache)
            environment["SWIFT_MODULECACHE_PATH"] = str(swift_cache)
            completed = subprocess.run(
                [str(self.swift_path), str(self.helper_path), str(image_path)],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (subprocess.TimeoutExpired, OSError, UnicodeError):
            raise OcrResponseError("local Apple Vision OCR failed") from None

        if completed.returncode != 0:
            raise OcrResponseError("local Apple Vision OCR failed")

        try:
            payload = json.loads(
                completed.stdout,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, dict) or set(payload) != {"blocks"}:
                raise TypeError("OCR payload has invalid keys")
            rows = payload["blocks"]
            if not isinstance(rows, list):
                raise TypeError("blocks must be a list")
            blocks = []
            for row in rows:
                if not isinstance(row, dict) or set(row) != BLOCK_KEYS:
                    raise TypeError("OCR block has invalid keys")
                text = row["text"]
                if not isinstance(text, str) or not text.strip():
                    raise TypeError("OCR block text is invalid")
                blocks.append(
                    OcrBlock(
                        text=text,
                        confidence=_bounded_decimal(
                            row, "confidence", lower_inclusive=True
                        ),
                        x=_bounded_decimal(row, "x", lower_inclusive=True),
                        y=_bounded_decimal(row, "y", lower_inclusive=True),
                        width=_bounded_decimal(row, "width", lower_inclusive=False),
                        height=_bounded_decimal(row, "height", lower_inclusive=False),
                    )
                )
            return blocks
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise OcrResponseError(
                "local Apple Vision OCR returned invalid data"
            ) from None
