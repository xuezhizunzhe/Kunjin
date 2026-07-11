import ast
import json
import os
import stat
import subprocess
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kunjin.ledger.ocr import (
    OcrResponseError,
    OcrUnavailableError,
    VisionOcrClient,
)


class VisionOcrClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.swift_path = root / "swift"
        self.helper_path = root / "vision_ocr.swift"
        self.image_path = root / "private-payment.jpg"
        self.cache_dir = root / "swift-cache"
        self.swift_path.touch()
        self.helper_path.touch()
        self.image_path.write_bytes(b"private-image-bytes")
        self.client = VisionOcrClient(
            swift_path=self.swift_path,
            helper_path=self.helper_path,
            cache_dir=self.cache_dir,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_cache_directory_is_private_process_user_cache(self) -> None:
        client = VisionOcrClient(
            swift_path=self.swift_path,
            helper_path=self.helper_path,
        )

        self.assertEqual(
            client.cache_dir,
            Path(tempfile.gettempdir()) / f"kunjin-swift-cache-{os.getuid()}",
        )

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_recognize_converts_json_blocks(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "blocks": [
                        {
                            "text": "订单金额 20.00元",
                            "confidence": 0.98,
                            "x": 0.1,
                            "y": 0.2,
                            "width": 0.8,
                            "height": 0.05,
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

        blocks = self.client.recognize(self.image_path)

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "订单金额 20.00元")
        self.assertEqual(blocks[0].confidence, Decimal("0.98"))
        self.assertEqual(blocks[0].x, Decimal("0.1"))
        self.assertEqual(blocks[0].y, Decimal("0.2"))
        self.assertEqual(blocks[0].width, Decimal("0.8"))
        self.assertEqual(blocks[0].height, Decimal("0.05"))
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [str(self.swift_path), str(self.helper_path), str(self.image_path)],
        )
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertEqual(kwargs["timeout"], 60)
        self.assertEqual(
            kwargs["env"]["CLANG_MODULE_CACHE_PATH"],
            str(self.cache_dir / "clang"),
        )
        self.assertEqual(
            kwargs["env"]["SWIFT_MODULECACHE_PATH"],
            str(self.cache_dir / "swift"),
        )
        for directory in (
            self.cache_dir,
            self.cache_dir / "clang",
            self.cache_dir / "swift",
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_missing_swift_raises_redacted_unavailable_error(self) -> None:
        missing_swift = self.swift_path.with_name("missing-swift")
        client = VisionOcrClient(
            swift_path=missing_swift,
            helper_path=self.helper_path,
        )

        with self.assertRaises(OcrUnavailableError) as raised:
            client.recognize(self.image_path)

        self.assertEqual(raised.exception.code, "ocr_unavailable")
        self.assertNotIn(str(missing_swift), str(raised.exception))
        self.assertNotIn(str(self.image_path), str(raised.exception))

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_malformed_json_raises_redacted_response_error(self, run) -> None:
        secret_ocr_text = "订单金额 20.00元"
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"not-json {secret_ocr_text}",
            stderr="",
        )

        with self.assertRaises(OcrResponseError) as raised:
            self.client.recognize(self.image_path)

        message = str(raised.exception)
        self.assertEqual(raised.exception.code, "ocr_response_error")
        self.assertNotIn(secret_ocr_text, message)
        self.assertNotIn(str(self.image_path), message)
        self.assertNotIn("private-image-bytes", message)

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_strict_json_contract_rejects_invalid_payloads(self, run) -> None:
        valid_row = {
            "text": "订单金额 20.00元",
            "confidence": 0.98,
            "x": 0.1,
            "y": 0.2,
            "width": 0.8,
            "height": 0.05,
        }
        invalid_payloads = {
            "non-object root": [],
            "blocks is not a list": {"blocks": {}},
            "root extra key": {"blocks": [], "extra": True},
            "row extra key": {"blocks": [{**valid_row, "extra": 1}]},
            "missing row key": {
                "blocks": [{key: value for key, value in valid_row.items() if key != "x"}]
            },
            "empty text": {"blocks": [{**valid_row, "text": ""}]},
            "whitespace text": {"blocks": [{**valid_row, "text": "   "}]},
            "non-string text": {"blocks": [{**valid_row, "text": 20}]},
            "boolean numeric": {"blocks": [{**valid_row, "confidence": True}]},
            "string numeric": {"blocks": [{**valid_row, "x": "0.1"}]},
            "null numeric": {"blocks": [{**valid_row, "y": None}]},
            "negative confidence": {"blocks": [{**valid_row, "confidence": -0.01}]},
            "confidence above one": {"blocks": [{**valid_row, "confidence": 1.01}]},
            "negative x": {"blocks": [{**valid_row, "x": -0.01}]},
            "x above one": {"blocks": [{**valid_row, "x": 1.01}]},
            "negative y": {"blocks": [{**valid_row, "y": -0.01}]},
            "y above one": {"blocks": [{**valid_row, "y": 1.01}]},
            "zero width": {"blocks": [{**valid_row, "width": 0}]},
            "width above one": {"blocks": [{**valid_row, "width": 1.01}]},
            "zero height": {"blocks": [{**valid_row, "height": 0}]},
            "height above one": {"blocks": [{**valid_row, "height": 1.01}]},
        }

        for name, payload in invalid_payloads.items():
            with self.subTest(name=name):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload, ensure_ascii=False),
                    stderr="",
                )
                with self.assertRaises(OcrResponseError):
                    self.client.recognize(self.image_path)

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_non_finite_json_constants_are_rejected(self, run) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=(
                        '{"blocks":[{"text":"private OCR text",'
                        f'"confidence":{constant},"x":0.1,"y":0.2,'
                        '"width":0.8,"height":0.05}]}'
                    ),
                    stderr="",
                )
                with self.assertRaises(OcrResponseError) as raised:
                    self.client.recognize(self.image_path)
                self.assertNotIn("private OCR text", str(raised.exception))

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_subprocess_failure_does_not_expose_stderr_or_image_path(self, run) -> None:
        secret_ocr_text = "基金交易隐私文字"
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=secret_ocr_text,
        )

        with self.assertRaises(OcrResponseError) as raised:
            self.client.recognize(self.image_path)

        message = str(raised.exception)
        self.assertNotIn(secret_ocr_text, message)
        self.assertNotIn(str(self.image_path), message)
        self.assertNotIn("private-image-bytes", message)

    @patch("kunjin.ledger.ocr.subprocess.run")
    def test_runtime_errors_are_redacted(self, run) -> None:
        failures = (
            subprocess.TimeoutExpired(cmd=[str(self.image_path)], timeout=60),
            OSError(f"cannot open {self.image_path}: private-image-bytes"),
            UnicodeDecodeError(
                "utf-8",
                b"private-image-bytes",
                0,
                1,
                "invalid OCR output",
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                run.side_effect = failure
                with self.assertRaises(OcrResponseError) as raised:
                    self.client.recognize(self.image_path)
                message = str(raised.exception)
                self.assertNotIn(str(self.image_path), message)
                self.assertNotIn("private-image-bytes", message)

    def test_setup_package_data_includes_swift_helper(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        setup_tree = ast.parse((project_root / "setup.py").read_text())
        setup_call = next(
            node
            for node in ast.walk(setup_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setup"
        )
        setup_arguments = {
            keyword.arg: ast.literal_eval(keyword.value)
            for keyword in setup_call.keywords
            if keyword.arg in {"package_data", "include_package_data"}
        }

        self.assertEqual(
            setup_arguments["package_data"],
            {"kunjin": ["ledger/*.swift"]},
        )
        self.assertIs(setup_arguments["include_package_data"], True)


if __name__ == "__main__":
    unittest.main()
