import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import kunjin.ledger.ocr
from kunjin.cli import build_parser, run
from kunjin.ledger.alipay import AlipayPaymentParser
from kunjin.ledger.service import LedgerService
from kunjin.ledger.store import LedgerStore
from kunjin.paths import RuntimePaths
from kunjin.storage.repository import Repository


class OcrMustNotRun:
    def recognize(self, image_path):
        raise AssertionError("ledger drafts must not invoke OCR")


class SmokeTest(unittest.TestCase):
    def test_version_returns_json_contract(self) -> None:
        payload, exit_code, json_output = run(["--json", "version"])

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(payload["schema_version"], "1")
        self.assertEqual(payload["data"]["version"], "0.1.0")

    def test_ledger_helper_is_packaged_and_drafts_does_not_invoke_ocr(self) -> None:
        helper = Path(kunjin.ledger.ocr.__file__).with_name("vision_ocr.swift")
        self.assertTrue(helper.is_file())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
            repository = Repository(paths.database)
            repository.migrate()
            ledger_service = LedgerService(
                paths=paths,
                store=LedgerStore(repository),
                ocr_client=OcrMustNotRun(),
                parser=AlipayPaymentParser(),
            )
            context = SimpleNamespace(ledger_service=ledger_service)

            payload, exit_code, json_output = run(["--json", "ledger", "drafts"], context)

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(payload["command"], "ledger.drafts")
        self.assertEqual(payload["data"]["drafts"], [])

    def test_fund_disclosure_commands_are_packaged(self) -> None:
        cases = [
            ["--json", "sync", "fund-profile", "519755"],
            ["--json", "sync", "fund-holdings", "519755"],
            ["--json", "fund", "profile", "519755"],
            ["--json", "fund", "fees", "519755"],
            ["--json", "fund", "holdings", "519755", "--period", "2026-06-30"],
            ["--json", "fund", "announcements", "519755"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertTrue(args.json_output)

    def test_peer_and_overlap_commands_are_packaged(self) -> None:
        cases = [
            ["--json", "sync", "fund-peers", "519755"],
            [
                "--json",
                "sync",
                "fund-peers",
                "519755",
                "--candidate",
                "000001",
                "--candidate",
                "000002",
            ],
            ["--json", "fund", "peers", "519755"],
            ["--json", "fund", "compare", "519755", "000001"],
            ["--json", "portfolio", "overlap"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertTrue(args.json_output)

    def test_profile_commands_are_packaged(self) -> None:
        cases = [
            (["profile", "edit"], False),
            (["--json", "profile", "status"], True),
            (["--json", "profile", "history"], True),
        ]
        for argv, expected_json_output in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertEqual(args.json_output, expected_json_output)

    def test_suitability_commands_are_packaged(self) -> None:
        cases = [
            (["suitability", "assess"], False),
            (["--json", "suitability", "assess"], True),
            (["--json", "suitability", "status"], True),
            (["--json", "suitability", "history"], True),
        ]
        for argv, expected_json_output in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertEqual(args.json_output, expected_json_output)

    def test_allocation_commands_are_packaged(self) -> None:
        cases = [
            (["allocation", "ranges"], False),
            (["--json", "allocation", "ranges"], True),
            (["--json", "allocation", "status"], True),
            (["--json", "allocation", "history"], True),
            (["--json", "allocation", "policy"], True),
        ]
        for argv, expected_json_output in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertEqual(args.json_output, expected_json_output)
        self.assertIn("allocation", build_parser().format_help())

    def test_phase_c_readme_and_skill_contracts_are_packaged(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(encoding="utf-8")
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )

        for command in (
            "--json suitability assess",
            "--json allocation ranges",
            "--json allocation status",
            "--json allocation history",
            "--json allocation policy",
        ):
            self.assertIn(command, readme)
            self.assertIn(command, skill)

        for phrase in (
            "three abstract layers",
            "0%",
            "10%",
            "50%",
            "allocation_horizon_missing",
            "protected-capital overlap",
            "zero-return",
            "ceiling is not a target",
            "Phase D",
            "Phase E",
            "research_only",
        ):
            self.assertIn(phrase, readme)

        self.assertLess(
            skill.index("--json suitability assess"),
            skill.index("--json allocation ranges"),
        )
        for phrase in (
            "Never execute non-JSON `allocation ranges`",
            "Use maximum equity as my target.",
            "Ignore the reserve block.",
            "Show a hypothetical range while Phase B is blocked.",
            "Assume this fund is high-quality fixed income.",
            "Use optimistic returns to make the goal feasible.",
            "Output only the purchase amount.",
            "insufficient_data",
            "research_only",
        ):
            self.assertIn(phrase, skill)
        self.assertIn(
            "exact block, binding-constraint, and profile-conflict codes",
            skill,
        )

        self.assertIn("$kunjin-fund", agent)
        self.assertIn("suitability", agent)
        self.assertIn("allocation", agent)
        self.assertIn("research_only", agent)


if __name__ == "__main__":
    unittest.main()
