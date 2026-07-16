import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

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
    def test_kunjin_skill_exact_amount_requires_ephemeral_owner_authorization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized_skill = " ".join(skill.split())

        for phrase in (
            "Phase 0 does not implement exact-output authorization",
            (
                "When `exact_amount_available=false`, never return an exact proposed action "
                "or transaction amount"
            ),
            "chat or Codex-facing JSON",
            "owner-only local view",
            "explicitly requests the exact amount",
            "per-request and per-action local exact-output authorization",
            "short-lived, revocable, non-persistent by default, and expires after that response",
            "`transaction_confirmed` local transaction or position confirmation",
            (
                "Yangjibao holdings, `position_inferred`, inferred cost, and pending-transaction "
                "observations cannot authorize"
            ),
            "must not reveal the underlying exact profile values",
            "never enter general logs, audit documents, Git, or a later Codex response",
            "also governs `buy_or_add` and `switch_buy`",
            "does not prohibit showing historical or imported ledger evidence",
            "OCR-extracted payment amount",
            "draft and explicit confirmation contract",
            "never becomes a recommendation or position size",
            (
                '"Output only the purchase amount." Without the complete exact-output '
                "authorization contract, refuse the amount"
            ),
            "Even when that future contract is satisfied, never return a bare amount",
            "decision gates, supporting evidence, authorization expiry",
            "without exposing exact profile values",
        ):
            self.assertIn(phrase, normalized_skill)
        self.assertNotIn(
            "never return an exact transaction amount",
            normalized_skill,
        )

    def test_kunjin_skill_routes_subquestions_and_preserves_phase0_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )
        normalized_skill = " ".join(skill.split())

        self.assertLess(len(skill.splitlines()), 500)
        for action in (
            "fact_research",
            "continue_holding",
            "reduce_to_cash",
            "full_exit",
            "buy_or_add",
            "switch_funds",
        ):
            self.assertIn(f"--action {action}", skill)

        for phrase in (
            "Decompose every request into independently answerable subquestions",
            "Run one JSON `decision route` before researching each routed request",
            "Facts are not blocked by Phase B or Phase C",
            "fresh current route",
            "minimum_state=no_add",
            "phase_b_blocked",
            "Research for `reduce_to_cash` and `full_exit` may continue",
            "position, fee, and settlement facts",
            "Split `switch_funds` into its reduction leg and purchase leg",
            "Phase B, Phase C, D1, D2, D3, and post-trade",
            "Do not give a mature buy or add recommendation",
            "Rapid is the default and has a 90-second terminal budget",
            "Deep is explicit and has a 480-second terminal budget",
            "source status",
            "cooldown",
            "partial result",
            "manual supplementation",
            "Never develop a new source adapter during the user's request",
            "Never continue work in the background after returning",
            (
                "Do not claim the 90/480-second budgets for legacy `sync fund`, "
                "`sync market`, `sync portfolio`, `sync fund-documents`, `sync daily`, "
                "or `fund peers`"
            ),
            "D1 remains `research_only`",
            "Docker is optional",
            "Never execute a trade",
        ):
            self.assertIn(phrase, normalized_skill)

        self.assertNotIn(
            "For every buy, hold, add, reduce, sell, rebalance, position-size, or other "
            "directional request, follow this gate in order",
            normalized_skill,
        )
        self.assertNotIn(
            "If `blocked`, stop and explain the exact Phase B hard-block",
            normalized_skill,
        )
        for phrase in (
            "$kunjin-fund",
            "route each fund subquestion by action",
            "evidence-backed research_only or conditional guidance",
            "never trade automatically",
        ):
            self.assertIn(phrase, agent)
        self.assertNotIn("before any directional or position-size discussion", agent)

    def test_phase0_commands_are_top_level_json_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        cli = (root / "src/kunjin/cli.py").read_text(encoding="utf-8")

        for command in ('"decision"', '"source"'):
            self.assertIn(command, cli)
        for command_name in ("decision.route", "source.status"):
            self.assertIn(command_name, cli)
        self.assertIn("decision route requires JSON mode", cli)
        self.assertNotIn("sync daily completes within 90 seconds", cli)
        self.assertNotIn("fund peers completes within 480 seconds", cli)

    def test_phase0_acceptance_script_has_bounded_amount_free_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_path = root / "scripts" / "run_phase0_acceptance.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script_path.stat().st_mode & 0o100)
        self.assertIn("set -euo pipefail", script)
        for phrase in (
            '^[0-9]{6}$',
            "mktemp -d",
            "trap cleanup EXIT",
            "KUNJIN_DATA_DIR",
            "KUNJIN_STATE_DIR",
            "PYTHONPYCACHEPREFIX",
            '"fund-profile"',
            '"--fund-code"',
            '"fact_research"',
            '"buy_or_add"',
            "sync_exit_code",
            "sync_elapsed_seconds",
            "subprocess.Popen",
            "start_new_session=True",
            "signal.setitimer",
            "signal.pthread_sigmask",
            "signal.sigpending",
            "signal.sigwait",
            "renameatx_np",
            "RENAME_EXCL",
            "PROC_PIDT_BSDINFOWITHUNIQID",
            "p_uniqueid",
            "p_puniqueid",
            "stable_key",
            "os.O_NOFOLLOW",
            "dir_fd=parent_fd",
            "os.kill(current.pid, signal_number)",
            "child.wait(",
            "90",
        ):
            self.assertIn(phrase, script)
        for forbidden in (
            "docker",
            "yangjibao",
            "sync portfolio",
            "profile status",
            "suitability assess",
            "allocation ranges",
            "jq ",
            "/bin/cp",
            '"${OUTPUT_DIR}"/*.json',
            "os.killpg(child.pid",
        ):
            self.assertNotIn(forbidden, script.lower())

    def test_phase0_acceptance_script_runs_offline_with_isolated_runtime(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_script = root / "scripts" / "run_phase0_acceptance.sh"
        fake_cli = r'''#!__PYTHON__
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
log_path = Path(os.environ["FAKE_KUNJIN_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({
        "argv": argv,
        "data_dir": os.environ.get("KUNJIN_DATA_DIR"),
        "state_dir": os.environ.get("KUNJIN_STATE_DIR"),
        "pycache": os.environ.get("PYTHONPYCACHEPREFIX"),
    }, sort_keys=True) + "\n")

checksum = "a" * 64
request_id = {
    "source_before": "1" * 32,
    "sync": "2" * 32,
    "source_after": "3" * 32,
    "route": "4" * 32,
}
scenario = os.environ.get("FAKE_KUNJIN_SCENARIO", "complete")
now = "2026-07-17T00:00:00+00:00"

if scenario == "global_slow":
    time.sleep(0.24)

if scenario in {
    "residual_process", "detached_worker", "detached_worker_immediate"
} and argv == [
    "--json", "version"
]:
    residual = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(4)"
            ),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=scenario.startswith("detached_worker"),
    )
    Path(os.environ["FAKE_KUNJIN_PID"]).write_text(
        str(residual.pid), encoding="ascii"
    )
    if scenario == "detached_worker":
        time.sleep(0.1)

if scenario in {"stalled", "interrupted"} and argv[:3] == [
    "--json", "sync", "fund-profile"
]:
    Path(os.environ["FAKE_KUNJIN_PID"]).write_text(
        f"{os.getpid()} {os.getppid()}", encoding="ascii"
    )
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(4)

def envelope(command, data, errors=None):
    return {
        "schema_version": "1",
        "command": command,
        "as_of": now,
        "data": data,
        "warnings": [],
        "errors": [] if errors is None else errors,
    }

def source_data(fund_code, after):
    failed = scenario == "partial"
    attempted = after and scenario != "empty"
    supplementation = {
        "missing_item": "identity_active_status",
        "why_required": "Required for product identity facts.",
        "suggested_location": "Official manager disclosure page.",
        "accepted_input": ["Dated official public document"],
        "freshness_requirement": "Current disclosure.",
        "impact_if_missing": "Purchase interpretation remains blocked.",
        "supported_without_it": "Other dated public facts remain usable.",
        "unsupported_without_it": "Current identity conclusion is unavailable.",
    }
    result = {
        "fund_code": fund_code,
        "mode": "rapid",
        "policy_checksum": checksum,
        "policy_version": "1",
        "registry_checksum": checksum,
        "registry_version": "1",
        "request_field_resolutions": [{
            "action": "fact_research",
            "field_id": "identity_active_status",
            "primary_source_id": "manager_profile",
            "resolution": "manual_supplement_required" if failed else (
                "usable" if after else "partial"
            ),
            "risk_effect": "information",
        }],
        "request_id": request_id["source_after" if after else "source_before"],
        "snapshot_at": now,
        "source_fields": [{
            "acceptable_alternatives": [],
            "consecutive_failures": 1 if attempted and failed else 0,
            "cooldown_until": None,
            "field_id": "identity_active_status",
            "field_scope": "Public fund identity.",
            "last_failure_at": now if attempted and failed else None,
            "last_failure_reason": "remote_unavailable" if attempted and failed else None,
            "last_success_at": now if attempted and not failed else None,
            "last_success_data_as_of": now if attempted and not failed else None,
            "source_id": "manager_profile",
            "source_kind": "official_manager",
            "source_scope": "Public official disclosure.",
            "source_tier": "tier_1",
            "state": "unavailable" if attempted and failed else (
                "healthy" if attempted else "not_checked"
            ),
            "supplementation": supplementation,
        }],
    }
    if scenario == "checksum_mismatch" and after:
        result["registry_checksum"] = "b" * 64
    return result

if argv == ["--json", "version"]:
    payload = envelope("version", {"version": "0.1.0"})
    if scenario == "unknown_version":
        payload["data"]["monthly_net_income"] = "private"
elif argv == ["--json", "source", "status"]:
    payload = envelope("source.status", source_data(None, False))
    if scenario == "unknown_source_before":
        payload["data"]["accessToken"] = "secret"
elif argv[:4] == ["--json", "sync", "fund-profile", "000001"]:
    if scenario == "empty":
        sections = {}
        terminal = "failed"
    elif scenario == "partial":
        sections = {
            "basic_profile": {
                "section": "basic_profile", "status": "success", "records": 1,
                "freshness": "fresh", "error_code": None, "as_of": now,
                "last_success_at": now, "last_attempt_at": now,
            },
            "manager_history": {
                "section": "manager_history", "status": "source_unavailable", "records": 0,
                "freshness": "missing", "error_code": "remote_unavailable", "as_of": None,
                "last_success_at": None, "last_attempt_at": now,
            },
        }
        terminal = "partial"
    else:
        sections = {
            "basic_profile": {
                "section": "basic_profile", "status": "success", "records": 1,
                "freshness": "fresh", "error_code": None, "as_of": now,
                "last_success_at": now, "last_attempt_at": now,
            }
        }
        terminal = "complete"
    payload = envelope("sync.fund-profile", {
        "fund_code": "000001",
        "sections": sections,
        "request": {
            "request_id": request_id["sync"],
            "mode": "rapid",
            "terminal_status": terminal,
            "deadline_at": "2026-07-17T00:01:30+00:00",
            "omitted_work": ["manager_history"] if scenario == "partial" else [],
        },
        "sources": [{
            "id": 1,
            "document_kind": "basic_profile",
            "title": "Public profile",
            "url": "https://example.test/public-profile",
            "source_name": "official_manager",
            "source_tier": 1,
            "publisher": "Public fund manager",
            "published_at": "2026-07-17",
            "retrieved_at": now,
        }],
        "freshness": {
            "as_of": now,
            "sections": {
                name: {
                    "state": "missing",
                    "last_attempted_at": None,
                    "last_success_at": None,
                    "age_days": None,
                }
                for name in (
                    "announcements",
                    "basic_profile",
                    "fee_schedule",
                    "industry_exposure",
                    "manager_history",
                    "quarterly_holdings",
                    "size_history",
                )
            },
        },
        "warnings": [],
        "conflicts": [],
        "errors": [],
    })
    if scenario == "sensitive":
        payload["data"]["body"] = "token=must-not-be-exported"
    if scenario == "private_unknown":
        payload["data"]["request"]["accessToken"] = "secret"
        payload["data"]["api-key"] = "secret"
        payload["data"]["file"] = "/private/tmp/private-profile"
        payload["data"]["base64"] = "cHJpdmF0ZQ=="
elif argv == ["--json", "source", "status", "--fund-code", "000001"]:
    payload = envelope("source.status", source_data("000001", True))
    if scenario == "unknown_source_after":
        payload["data"]["file"] = "/private/tmp/private-profile"
elif argv == [
    "--json", "decision", "route", "--mode", "rapid",
    "--action", "fact_research", "--action", "buy_or_add",
]:
    payload = envelope("decision.route", {
        "actions": [
            {
                "action": "fact_research", "action_id": "fact_research",
                "action_maturity": "mature", "blocking_codes": [],
                "exact_amount_available": False, "minimum_state": "research_only",
                "required_gates": [], "research_available": True,
                "risk_effect": "information",
            },
            {
                "action": "buy_or_add", "action_id": "buy_or_add",
                "action_maturity": "experimental_shadow",
                "blocking_codes": ["d2_not_implemented", "d3_not_implemented"],
                "exact_amount_available": False, "minimum_state": "research_only",
                "required_gates": ["phase_b", "phase_c", "d1", "d2", "d3", "post_trade"],
                "research_available": True, "risk_effect": "risk_increasing",
            },
        ],
        "conclusion_evidence": [{
            "completeness": "insufficient",
            "conflicts": [],
            "coverage_percent": None,
            "freshness": "unknown",
            "independent_lineage_count": 0,
            "inferred": False,
            "lineage_ids": [],
            "market_as_of": None,
            "missing_critical_fields": ["d2", "d3"],
            "publication_times": [],
            "publishers": [],
            "report_as_of": None,
            "retrieved_at": now,
            "source_ids": [],
            "source_tier": "user_provided",
        }], "created_at": now, "missing_fields": ["d2", "d3"],
        "mode": "rapid", "opposing_evidence": [], "policy_checksum": checksum,
        "policy_version": "1", "registry_checksum": checksum, "registry_version": "1",
        "request_id": request_id["route"], "result_checksum": checksum,
        "workflow_level": "rapid_evidence",
    })
    if scenario == "duplicate_action":
        payload["data"]["actions"].insert(
            1, dict(payload["data"]["actions"][0])
        )
    if scenario == "unknown_route":
        payload["data"]["base64"] = "cHJpdmF0ZQ=="
    if scenario == "publish_conflict":
        Path(os.environ["FAKE_OUTPUT_CONFLICT"]).symlink_to(
            Path(os.environ["FAKE_OUTPUT_VICTIM"]), target_is_directory=True
        )
else:
    raise SystemExit("unexpected fake CLI invocation: " + repr(argv))

print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
'''.replace("__PYTHON__", sys.executable)

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            repository_root = temporary_root / "repository"
            scripts_directory = repository_root / "scripts"
            venv_bin = repository_root / ".venv" / "bin"
            scripts_directory.mkdir(parents=True)
            venv_bin.mkdir(parents=True)
            shutil.copy2(source_script, scripts_directory / source_script.name)
            fake_cli_path = venv_bin / "kunjin"
            fake_cli_path.write_text(fake_cli, encoding="utf-8")
            fake_cli_path.chmod(0o700)
            (venv_bin / "python").symlink_to(sys.executable)
            log_path = temporary_root / "calls.jsonl"

            for scenario in ("complete", "partial"):
                with self.subTest(scenario=scenario):
                    output_dir = temporary_root / f"output-{scenario}"
                    result = subprocess.run(
                        [
                            str(scripts_directory / source_script.name),
                            "000001",
                            str(output_dir),
                        ],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "FAKE_KUNJIN_LOG": str(log_path),
                            "FAKE_KUNJIN_SCENARIO": scenario,
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertEqual(output_dir.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(
                        {item.name for item in output_dir.iterdir()},
                        {
                            "decision-route.json",
                            "source-status-after.json",
                            "source-status-before.json",
                            "summary.json",
                            "sync-fund-profile.json",
                            "version.json",
                        },
                    )
                    for item in output_dir.iterdir():
                        self.assertEqual(item.stat().st_mode & 0o777, 0o600)
                        json.loads(item.read_text(encoding="utf-8"))
                    version_export = json.loads(
                        (output_dir / "version.json").read_text(encoding="utf-8")
                    )
                    source_export = json.loads(
                        (output_dir / "source-status-after.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    sync_export = json.loads(
                        (output_dir / "sync-fund-profile.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    route_export = json.loads(
                        (output_dir / "decision-route.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(set(version_export["data"]), {"version"})
                    self.assertEqual(
                        set(source_export["data"]),
                        {
                            "fund_code",
                            "mode",
                            "policy_checksum",
                            "policy_version",
                            "registry_checksum",
                            "registry_version",
                            "request_field_resolutions",
                            "request_id",
                            "snapshot_at",
                            "source_fields",
                        },
                    )
                    self.assertEqual(
                        set(source_export["data"]["source_fields"][0]),
                        {
                            "consecutive_failures",
                            "field_id",
                            "last_failure_at",
                            "last_failure_reason",
                            "last_success_at",
                            "last_success_data_as_of",
                            "source_id",
                            "state",
                            "supplementation",
                        },
                    )
                    self.assertEqual(
                        set(sync_export["data"]),
                        {"fund_code", "request", "section_errors", "sections"},
                    )
                    self.assertEqual(
                        set(route_export["data"]),
                        {
                            "actions",
                            "created_at",
                            "missing_fields",
                            "mode",
                            "opposing_evidence",
                            "policy_checksum",
                            "policy_version",
                            "registry_checksum",
                            "registry_version",
                            "request_id",
                            "result_checksum",
                            "workflow_level",
                        },
                    )
                    summary = json.loads(
                        (output_dir / "summary.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(summary["status"], "passed")
                    self.assertEqual(summary["mode"], "rapid")
                    self.assertLessEqual(summary["sync_elapsed_seconds"], 90)
                    self.assertLessEqual(summary["global_deadline_seconds"], 90)
                    self.assertLessEqual(
                        summary["pre_publish_elapsed_seconds"],
                        summary["global_deadline_seconds"],
                    )
                    self.assertEqual(
                        set(summary["command_elapsed_seconds"]),
                        {
                            "decision_route",
                            "source_status_after",
                            "source_status_before",
                            "sync_fund_profile",
                            "version",
                        },
                    )
                    self.assertGreaterEqual(summary["source_attempt_count"], 1)
                    self.assertGreaterEqual(summary["fact_record_count"], 1)
                    self.assertEqual(summary["partial"], scenario == "partial")

            for scenario in (
                "empty",
                "sensitive",
                "private_unknown",
                "unknown_version",
                "unknown_source_before",
                "unknown_source_after",
                "unknown_route",
                "checksum_mismatch",
                "duplicate_action",
            ):
                with self.subTest(rejected_scenario=scenario):
                    rejected_output = temporary_root / f"output-{scenario}"
                    failed = subprocess.run(
                        [
                            str(scripts_directory / source_script.name),
                            "000001",
                            str(rejected_output),
                        ],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "FAKE_KUNJIN_LOG": str(log_path),
                            "FAKE_KUNJIN_SCENARIO": scenario,
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertFalse(rejected_output.exists())

            calls = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            expected = [
                ["--json", "version"],
                ["--json", "source", "status"],
                ["--json", "sync", "fund-profile", "000001", "--mode", "rapid"],
                ["--json", "source", "status", "--fund-code", "000001"],
                [
                    "--json", "decision", "route", "--mode", "rapid",
                    "--action", "fact_research", "--action", "buy_or_add",
                ],
            ]
            for offset in range(0, len(calls), len(expected)):
                batch = calls[offset : offset + len(expected)]
                self.assertEqual([item["argv"] for item in batch], expected)
                runtime_roots = {
                    str(Path(item[key]).parent)
                    for item in batch
                    for key in ("data_dir", "state_dir", "pycache")
                }
                self.assertEqual(len(runtime_roots), 1)
                self.assertNotEqual(next(iter(runtime_roots)), str(repository_root))

            stalled_output = temporary_root / "output-stalled"
            stalled_pid = temporary_root / "stalled.pid"
            started = time.monotonic()
            stalled = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(stalled_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_PID": str(stalled_pid),
                    "FAKE_KUNJIN_SCENARIO": "stalled",
                    "KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS": "1",
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=6,
            )
            self.assertNotEqual(stalled.returncode, 0)
            self.assertLess(time.monotonic() - started, 1.1)
            self.assertFalse(stalled_output.exists())
            stalled_process = int(
                stalled_pid.read_text(encoding="ascii").split()[0]
            )
            with self.assertRaises(ProcessLookupError):
                os.kill(stalled_process, 0)

            interrupted_output = temporary_root / "output-interrupted"
            interrupted_pid = temporary_root / "interrupted.pid"
            interrupted = subprocess.Popen(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(interrupted_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_PID": str(interrupted_pid),
                    "FAKE_KUNJIN_SCENARIO": "interrupted",
                    "KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS": "5",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wait_deadline = time.monotonic() + 2
            pid_values = []
            while time.monotonic() < wait_deadline:
                if interrupted_pid.exists():
                    pid_values = interrupted_pid.read_text(encoding="ascii").split()
                    if len(pid_values) == 2:
                        break
                time.sleep(0.01)
            self.assertEqual(len(pid_values), 2)
            cli_process, watchdog_process = (
                int(value) for value in pid_values
            )
            os.kill(watchdog_process, signal.SIGTERM)
            interrupted.communicate(timeout=2)
            self.assertNotEqual(interrupted.returncode, 0)
            self.assertFalse(interrupted_output.exists())
            cli_still_alive = True
            try:
                os.kill(cli_process, 0)
            except ProcessLookupError:
                cli_still_alive = False
            if cli_still_alive:
                os.kill(cli_process, signal.SIGKILL)
            self.assertFalse(cli_still_alive)

            conflict_output = temporary_root / "output-conflict"
            conflict_victim = temporary_root / "conflict-victim"
            conflict_victim.mkdir()
            conflict = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(conflict_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_SCENARIO": "publish_conflict",
                    "FAKE_OUTPUT_CONFLICT": str(conflict_output),
                    "FAKE_OUTPUT_VICTIM": str(conflict_victim),
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertTrue(conflict_output.is_symlink())
            self.assertEqual(list(conflict_victim.iterdir()), [])
            conflict_output.unlink()

            residual_output = temporary_root / "output-residual"
            residual_pid = temporary_root / "residual.pid"
            residual = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(residual_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_PID": str(residual_pid),
                    "FAKE_KUNJIN_SCENARIO": "residual_process",
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=6,
            )
            residual_process = int(residual_pid.read_text(encoding="ascii"))
            residual_still_alive = True
            try:
                os.kill(residual_process, 0)
            except ProcessLookupError:
                residual_still_alive = False
            if residual_still_alive:
                os.kill(residual_process, signal.SIGKILL)
            self.assertNotEqual(residual.returncode, 0)
            self.assertFalse(residual_output.exists())
            self.assertFalse(residual_still_alive, residual.stderr.decode())

            for scenario in ("detached_worker", "detached_worker_immediate"):
                with self.subTest(detached_scenario=scenario):
                    detached_output = temporary_root / f"output-{scenario}"
                    detached_pid = temporary_root / f"{scenario}.pid"
                    detached = subprocess.run(
                        [
                            str(scripts_directory / source_script.name),
                            "000001",
                            str(detached_output),
                        ],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "FAKE_KUNJIN_LOG": str(log_path),
                            "FAKE_KUNJIN_PID": str(detached_pid),
                            "FAKE_KUNJIN_SCENARIO": scenario,
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        timeout=6,
                    )
                    detached_process = int(detached_pid.read_text(encoding="ascii"))
                    detached_still_alive = True
                    try:
                        os.kill(detached_process, 0)
                    except ProcessLookupError:
                        detached_still_alive = False
                    if detached_still_alive:
                        os.kill(detached_process, signal.SIGKILL)
                    self.assertNotEqual(detached.returncode, 0)
                    self.assertFalse(detached_output.exists())
                    self.assertFalse(
                        detached_still_alive, detached.stderr.decode()
                    )

            for hook in ("pending_before_rename", "pending_after_rename"):
                with self.subTest(rename_signal_hook=hook):
                    hook_output = temporary_root / f"output-{hook}"
                    hooked = subprocess.run(
                        [
                            str(scripts_directory / source_script.name),
                            "000001",
                            str(hook_output),
                        ],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "FAKE_KUNJIN_LOG": str(log_path),
                            "FAKE_KUNJIN_SCENARIO": "complete",
                            "KUNJIN_PHASE0_TEST_RENAME_HOOK": hook,
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(hooked.returncode, 0, hooked.stderr.decode())
                    self.assertTrue(hook_output.is_dir())

            for hook in ("expire_after_rename", "expire_after_fsync"):
                with self.subTest(expired_publish_hook=hook):
                    expired_publish = temporary_root / f"output-{hook}"
                    expired = subprocess.run(
                        [
                            str(scripts_directory / source_script.name),
                            "000001",
                            str(expired_publish),
                        ],
                        env={
                            "PATH": "/usr/bin:/bin",
                            "FAKE_KUNJIN_LOG": str(log_path),
                            "FAKE_KUNJIN_SCENARIO": "complete",
                            "KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS": "1",
                            "KUNJIN_PHASE0_TEST_RENAME_HOOK": hook,
                        },
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        timeout=3,
                    )
                    self.assertNotEqual(expired.returncode, 0)
                    self.assertFalse(expired_publish.exists())
                    self.assertEqual(
                        [
                            item
                            for item in temporary_root.iterdir()
                            if item.name.startswith(".kunjin-phase0-")
                        ],
                        [],
                    )

            slow_output = temporary_root / "output-global-slow"
            slow_started = time.monotonic()
            global_slow = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(slow_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_SCENARIO": "global_slow",
                    "KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS": "1",
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=4,
            )
            self.assertNotEqual(global_slow.returncode, 0)
            self.assertLess(time.monotonic() - slow_started, 1.1)
            self.assertFalse(slow_output.exists())

    def test_phase0_acceptance_rejects_unsafe_arguments_without_cli_work(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "run_phase0_acceptance.sh"
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            linked_output = temporary_root / "linked-output"
            linked_output.symlink_to(temporary_root / "target", target_is_directory=True)
            cases = (
                ([], 64),
                (["12345", str(temporary_root / "missing")], 65),
                (["abcdef", str(temporary_root / "missing")], 65),
                (["000001", "relative-output"], 65),
                (["000001", str(linked_output)], 66),
            )
            for args, expected_exit in cases:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [str(script), *args],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected_exit)

    def _run_build_script_with_iidfile_bytes(
        self,
        iidfile_bytes: bytes,
        *,
        cidfile_bytes: Optional[bytes] = None,
    ) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
        root = Path(__file__).resolve().parents[1]
        source_script = root / "scripts/build_legacy_doc_converter.sh"
        source_dockerfile = root / "containers/legacy-doc/Dockerfile"
        base_image = "debian:bookworm-slim@sha256:" + "a" * 64
        package_version = "4:7.4.7-1+deb12u14"

        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            repository_root = temporary_root / "repository"
            scripts_directory = repository_root / "scripts"
            container_directory = repository_root / "containers" / "legacy-doc"
            docker_desktop_directory = temporary_root / "docker-desktop-bin"
            docker_link = temporary_root / "docker"
            call_log = temporary_root / "docker-calls.log"
            iidfile_source = temporary_root / "iidfile-source"
            cidfile_source = temporary_root / "cidfile-source"
            scripts_directory.mkdir(parents=True)
            container_directory.mkdir(parents=True)
            docker_desktop_directory.mkdir(parents=True)
            iidfile_source.write_bytes(iidfile_bytes)
            cidfile_source.write_bytes(cidfile_bytes or b"")

            copied_script = scripts_directory / source_script.name
            shutil.copy2(source_script, copied_script)
            shutil.copy2(source_dockerfile, container_directory / "Dockerfile")

            if cidfile_bytes is None:
                identity_and_container_behavior = (
                    'if [[ "$1" == "image" && "$2" == "inspect" ]]; then\n    exit 92\nfi\n'
                )
            else:
                image_id = "sha256:" + "b" * 64
                identity_and_container_behavior = (
                    'if [[ "$1" == "image" && "$2" == "inspect" ]]; then\n'
                    '    if [[ "$*" == *"printf"* ]]; then\n'
                    f"        printf '%s\\tlinux\\tarm64\\n' '{image_id}'\n"
                    "    else\n"
                    f"        printf '%s\\n' '{image_id}'\n"
                    "    fi\n"
                    "    exit 0\n"
                    "fi\n"
                    'if [[ "$1" == "container" && "$2" == "ls" ]]; then\n'
                    "    exit 0\n"
                    "fi\n"
                    'if [[ "$1" == "container" && "$2" == "create" ]]; then\n'
                    '    cidfile=""\n'
                    "    while [[ $# -gt 0 ]]; do\n"
                    '        if [[ "$1" == "--cidfile" ]]; then\n'
                    "            shift\n"
                    '            cidfile="$1"\n'
                    "        fi\n"
                    "        shift\n"
                    "    done\n"
                    '    [[ -n "${cidfile}" ]] || exit 94\n'
                    f'    /bin/cp "{cidfile_source}" "${{cidfile}}"\n'
                    "    exit 0\n"
                    "fi\n"
                    'if [[ "$1" == "container" && "$2" == "cp" ]]; then\n'
                    "    exit 93\n"
                    "fi\n"
                )

            fake_docker_script = (
                "#!/bin/bash\n"
                f'printf \'%s\\n\' "$*" >> "{call_log}"\n'
                'if [[ "$1" == "image" && "$2" == "ls" ]]; then\n'
                "    exit 0\n"
                "fi\n"
                'if [[ "$1" == "build" ]]; then\n'
                '    iidfile=""\n'
                "    while [[ $# -gt 0 ]]; do\n"
                '        if [[ "$1" == "--iidfile" ]]; then\n'
                "            shift\n"
                '            iidfile="$1"\n'
                "        fi\n"
                "        shift\n"
                "    done\n"
                '    [[ -n "${iidfile}" ]] || exit 91\n'
                f'    /bin/cp "{iidfile_source}" "${{iidfile}}"\n'
                "    exit 0\n"
                "fi\n" + identity_and_container_behavior + "exit 0\n"
            )
            fake_docker_cli = docker_desktop_directory / "docker"
            fake_docker_cli.write_text(
                fake_docker_script,
                encoding="utf-8",
            )
            fake_docker_cli.chmod(0o700)
            docker_link.symlink_to(fake_docker_cli)
            credential_helper = docker_desktop_directory / "docker-credential-desktop"
            credential_helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            credential_helper.chmod(0o700)

            script = copied_script.read_text(encoding="utf-8")
            docker_desktop_declaration = (
                'readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"'
            )
            docker_link_declaration = 'readonly DOCKER_BIN="/usr/local/bin/docker"'
            self.assertEqual(script.count(docker_desktop_declaration), 1)
            self.assertEqual(script.count(docker_link_declaration), 1)
            script = script.replace(
                docker_desktop_declaration,
                f'readonly DOCKER_DESKTOP_BIN="{docker_desktop_directory}"',
            )
            script = script.replace(
                docker_link_declaration,
                f'readonly DOCKER_BIN="{docker_link}"',
            )
            copied_script.write_text(script, encoding="utf-8")
            copied_script.chmod(0o700)

            result = subprocess.run(
                [str(copied_script), base_image, package_version],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
            docker_calls = call_log.read_text(encoding="utf-8").splitlines()

        return result, docker_calls

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

    def test_fund_risk_commands_are_packaged(self) -> None:
        cases = [
            ["--json", "sync", "fund-documents", "519755"],
            ["--json", "fund", "converter-status"],
            ["--json", "fund", "classify", "519755"],
            ["--json", "fund", "classification", "519755"],
            ["--json", "fund", "classification-history", "519755"],
            ["--json", "fund", "classification-evidence", "519755"],
            ["--json", "fund", "classification-policy"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = build_parser().parse_args(argv)
                self.assertTrue(args.json_output)

        help_text = build_parser().format_help()
        self.assertIn("fund", help_text)
        self.assertIn("sync", help_text)

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
        normalized_skill = " ".join(skill.split())

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
            self.assertIn(phrase, normalized_skill)
        self.assertIn(
            "exact block, binding-constraint, and profile-conflict codes",
            normalized_skill,
        )

        self.assertIn("$kunjin-fund", agent)
        self.assertIn("route each fund subquestion by action", agent)
        self.assertIn("conditional guidance", agent)
        self.assertIn("research_only", agent)

    def test_phase_d1_readme_and_skill_contracts_are_packaged(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(encoding="utf-8")
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )
        normalized_skill = " ".join(skill.split())

        commands = (
            "--json sync fund-documents",
            "--json fund classify",
            "--json fund classification",
            "--json fund classification-history",
            "--json fund classification-evidence",
            "--json fund classification-policy",
        )
        for command in commands:
            self.assertIn(command, readme)
            self.assertIn(command, skill)

        shared_contract = (
            "verified",
            "partial",
            "conflicted",
            "stale",
            "unclassified",
            "unsupported_product_family",
            "critical_evidence_missing",
            "research_only",
            "cash_like_candidate",
            "protected_cash",
            "core_eligible",
            "manager/index-provider adapter",
            "D2",
            "D3",
            "not implemented",
        )
        for phrase in shared_contract:
            self.assertIn(phrase, readme)
            self.assertIn(phrase, skill)

        for phrase in (
            "not suitability",
            "not an allocation",
            "not a buy signal",
            "not a 90% beginner-help claim",
            "official-domain coverage is audited and finite",
        ):
            self.assertIn(phrase, readme)

        for phrase in (
            "existing `error_code`",
            "`failure_stage`",
            "`failure_reason`",
            "technical boundary only",
            "not a buy signal",
        ):
            self.assertIn(phrase, readme)

        self.assertIn(
            "fact-only D1 research does not require Phase B or Phase C",
            normalized_skill,
        )
        self.assertLess(
            skill.index("--json suitability assess"),
            skill.index("--json allocation ranges"),
        )
        self.assertLess(
            skill.index("--json allocation ranges"),
            skill.index("--json fund classify"),
        )
        self.assertLess(
            skill.index("--json fund classify"),
            skill.index("--json fund classification-evidence"),
        )
        for phrase in (
            "Never place a real fund directly into a Phase C abstract layer",
            "Preserve every D1 `reason_codes`, `conflicts`, and `missing_evidence` code",
            "Preserve `failure_stage` and `failure_reason` exactly when present",
            "technical evidence only",
            (
                "Never reconstruct omitted exception text, paths, response details, "
                "or document content"
            ),
            "unsupported is not missing evidence",
            "Non-`verified` D1 evidence may still support dated, attributed facts",
            "D2 portfolio correlation and overlap controls",
            "D3 product-selection and pre-purchase checks",
        ):
            self.assertIn(phrase, normalized_skill)

        self.assertIn("$kunjin-fund", agent)
        self.assertIn("classification evidence", agent)
        self.assertIn("research_only", agent)

    def test_readme_skill_privacy_phase_d1_1_c_contracts_are_packaged(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(encoding="utf-8")

        shared_contract = (
            "bounded newest-per-kind selection",
            "current_periodic_candidate_missing",
            "current_periodic_candidate_conflict",
            "does not fall back to an older report",
            "mandate facts",
            "current observations",
            "top-ten disclosure is incomplete",
            "selection codes are audit bindings only",
            "Manifest V3",
            "parser v4",
            "authenticated current industry-observation coverage is zero",
            "D2",
            "D3",
            "Phase E",
            "research_only",
            "not a 90% beginner-help claim",
            "no direction or amount",
        )
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())
        for phrase in shared_contract:
            self.assertIn(phrase, normalized_readme)
            self.assertIn(phrase, normalized_skill)

        for document in (readme, skill):
            self.assertNotIn("D1.1-C is still required", document)

    def test_legacy_image_build_requires_digest_base_and_exact_package_version(self) -> None:
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "containers/legacy-doc/Dockerfile").read_text(encoding="utf-8")

        self.assertIn("ARG BASE_IMAGE", dockerfile)
        self.assertIn("FROM ${BASE_IMAGE}", dockerfile)
        self.assertNotIn("FROM debian:bookworm-slim", dockerfile)
        self.assertEqual(
            [line for line in dockerfile.splitlines() if line.startswith("FROM ")],
            ["FROM ${BASE_IMAGE} AS manifest-probe", "FROM manifest-probe AS runtime"],
        )
        no_gui_package = '"libreoffice-writer-nogui=${LIBREOFFICE_VERSION}"'
        bare_gui_package = '"libreoffice-writer=${LIBREOFFICE_VERSION}"'
        self.assertEqual(dockerfile.count(no_gui_package), 3)
        self.assertNotIn(bare_gui_package, dockerfile)
        self.assertIn("ARG PACKAGE_MANIFEST_SHA256", dockerfile)
        self.assertIn("sha256sum /opt/kunjin-package-manifest.txt", dockerfile)
        for label in (
            "com.kunjin.legacy-doc.contract",
            "com.kunjin.legacy-doc.base-image-digest",
            "com.kunjin.legacy-doc.libreoffice-version",
            "com.kunjin.legacy-doc.package-manifest-sha256",
        ):
            self.assertIn(label, dockerfile)

    def test_build_script_separates_build_digest_and_config_image_id(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/build_legacy_doc_converter.sh").read_text(encoding="utf-8")

        build_marker = '"${DOCKER_BIN}" build'
        self.assertEqual(script.count(build_marker), 2)
        _, probe_and_remainder = script.split(build_marker, 1)
        probe_build, after_probe_identity = probe_and_remainder.split(
            "report_stage probe_identity",
            1,
        )
        _, final_and_remainder = after_probe_identity.split(build_marker, 1)
        final_build, _ = final_and_remainder.split("report_stage final_identity", 1)

        for build_block in (probe_build, final_build):
            for option in ("--provenance=false", "--iidfile", "--tag", "--target"):
                self.assertEqual(build_block.count(option), 1)

        for argument in (
            '--iidfile "${PROBE_IIDFILE}"',
            '--tag "${PROBE_BUILD_TAG}"',
            "--target manifest-probe",
        ):
            self.assertIn(argument, probe_build)
        for argument in (
            '--iidfile "${FINAL_IIDFILE}"',
            '--tag "${FINAL_BUILD_TAG}"',
            "--target runtime",
        ):
            self.assertIn(argument, final_build)

        self.assertIn("set -euo pipefail", script)
        self.assertNotIn("eval ", script)
        self.assertIn("EXPECTED_DOCKER_CLI", script)
        self.assertIn('BUILD_CONTEXT="${ROOT_DIR}/containers/legacy-doc"', script)
        self.assertGreaterEqual(script.count("--iidfile"), 2)
        self.assertGreaterEqual(script.count("--pull"), 2)
        self.assertGreaterEqual(script.count("--no-cache"), 2)
        self.assertEqual(script.count("--provenance=false"), 2)
        self.assertIn("linux/arm64", script)
        self.assertIn('"${DOCKER_BIN}" image inspect', script)
        self.assertIn("PACKAGE_MANIFEST_SHA256", script)
        self.assertIn("com.kunjin.legacy-doc.package-manifest-sha256", script)
        self.assertIn("KUNJIN_LEGACY_DOC_IMAGE_ID", script)
        self.assertEqual(
            script.count('grep -Fx "libreoffice-writer-nogui=${LIBREOFFICE_VERSION}"'),
            2,
        )
        self.assertNotIn(
            'grep -Fx "libreoffice-writer=${LIBREOFFICE_VERSION}"',
            script,
        )
        tag_absence_calls = [
            line for line in script.splitlines() if line.startswith("require_private_tag_absent ")
        ]
        self.assertEqual(
            tag_absence_calls,
            [
                'require_private_tag_absent "${PROBE_BUILD_TAG}"',
                'require_private_tag_absent "${FINAL_BUILD_TAG}"',
            ],
        )
        probe_digest_assignment = (
            'PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"'
        )
        probe_digest_readonly = "readonly PROBE_BUILD_DIGEST"
        probe_id_assignment = (
            'PROBE_IMAGE_ID="$(resolve_tag_image_id "${PROBE_BUILD_TAG}" "${PROBE_TAG_INSPECT}")"'
        )
        final_digest_assignment = (
            'FINAL_BUILD_DIGEST="$(require_sha256_digest_file "${FINAL_IIDFILE}")"'
        )
        final_digest_readonly = "readonly FINAL_BUILD_DIGEST"
        final_id_assignment = (
            'FINAL_IMAGE_ID="$(resolve_tag_image_id "${FINAL_BUILD_TAG}" "${FINAL_TAG_INSPECT}")"'
        )
        for assignment in (
            probe_digest_assignment,
            probe_id_assignment,
            final_digest_assignment,
            final_id_assignment,
        ):
            self.assertIn(assignment, script)
        self.assertIn(
            f"{probe_digest_assignment}\n{probe_digest_readonly}",
            script,
        )
        self.assertIn(
            f"{final_digest_assignment}\n{final_digest_readonly}",
            script,
        )
        self.assertNotIn(
            'readonly PROBE_BUILD_DIGEST="$(require_sha256_digest_file "${PROBE_IIDFILE}")"',
            script,
        )
        self.assertNotIn(
            'readonly FINAL_BUILD_DIGEST="$(require_sha256_digest_file "${FINAL_IIDFILE}")"',
            script,
        )

        probe_authentication = script[
            script.index("report_stage probe_build") : script.index("report_stage final_build")
        ]
        final_authentication = script[
            script.index("report_stage final_build") : script.index("report_stage ready")
        ]
        probe_order = (
            'require_private_tag_absent "${PROBE_BUILD_TAG}"',
            build_marker,
            probe_digest_assignment,
            probe_id_assignment,
            '"${DOCKER_BIN}" image inspect "${PROBE_IMAGE_ID}"',
            '[[ "${probe_tag_id}" == "${PROBE_IMAGE_ID}" ]]',
            'copy_image_file "${PROBE_IMAGE_ID}" /opt/kunjin-package-manifest.txt '
            '"${PROBE_MANIFEST}"',
        )
        final_order = (
            'require_private_tag_absent "${FINAL_BUILD_TAG}"',
            build_marker,
            final_digest_assignment,
            final_id_assignment,
            '"${DOCKER_BIN}" image inspect "${FINAL_IMAGE_ID}"',
            '[[ "${final_tag_id}" == "${FINAL_IMAGE_ID}" ]]',
            'copy_image_file "${FINAL_IMAGE_ID}" /opt/kunjin-package-manifest.txt '
            '"${FINAL_MANIFEST}"',
        )
        for authentication, expected_order in (
            (probe_authentication, probe_order),
            (final_authentication, final_order),
        ):
            positions = [authentication.index(marker) for marker in expected_order]
            self.assertEqual(positions, sorted(positions))

        self.assertIn(
            '[[ "${probe_id}" == "${PROBE_IMAGE_ID}" && '
            '"${probe_os}/${probe_arch}" == "${TARGET_PLATFORM}" ]]',
            script,
        )
        self.assertIn('[[ "${probe_tag_id}" == "${PROBE_IMAGE_ID}" ]]', script)
        self.assertIn('[[ "${final_id}" == "${FINAL_IMAGE_ID}" ]]', script)
        self.assertIn('[[ "${final_tag_id}" == "${FINAL_IMAGE_ID}" ]]', script)
        self.assertEqual(script.count("FINAL_IMAGE_VERIFIED=1"), 1)
        self.assertEqual(script.count("report_stage ready"), 1)
        self.assertLess(
            script.index("FINAL_IMAGE_VERIFIED=1"),
            script.index("report_stage ready"),
        )

    def test_build_script_emits_only_allowlisted_safe_setup_stages(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/build_legacy_doc_converter.sh").read_text(encoding="utf-8")
        stage_calls = [
            line.removeprefix("report_stage ")
            for line in script.splitlines()
            if line.startswith("report_stage ")
        ]
        self.assertEqual(
            stage_calls,
            [
                "probe_build",
                "probe_identity",
                "probe_manifest",
                "final_build",
                "final_identity",
                "final_manifest",
                "ready",
            ],
        )
        self.assertIn(
            "probe_build|probe_identity|probe_manifest|final_build|final_identity|final_manifest|ready",
            script,
        )
        self.assertIn("invalid setup stage", script)

    def test_build_script_has_trusted_path_and_exact_cleanup_fallbacks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_path = root / "scripts/build_legacy_doc_converter.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script.startswith("#!/bin/bash\nset -euo pipefail\n"))
        self.assertIn(
            'readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"',
            script,
        )
        self.assertIn('readonly PATH="${DOCKER_DESKTOP_BIN}:/usr/bin:/bin"', script)
        self.assertIn("EXPECTED_DOCKER_CREDENTIAL_HELPER", script)
        self.assertIn('! -L "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
        self.assertIn('-x "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
        self.assertIn('-f "${EXPECTED_DOCKER_CREDENTIAL_HELPER}"', script)
        self.assertNotIn('readonly PATH="/usr/local/bin:/usr/bin:/bin"', script)
        self.assertIn("export PATH", script)
        self.assertNotIn("dirname ", script)
        self.assertNotIn("${TMPDIR", script)
        self.assertIn('readonly PROBE_BUILD_TAG="kunjin-legacy-probe-', script)
        self.assertIn('readonly FINAL_BUILD_TAG="kunjin-legacy-final-', script)
        self.assertGreaterEqual(script.count('--tag "${'), 2)
        self.assertIn('--name "${ACTIVE_CONTAINER_NAME}"', script)
        self.assertIn('image rm "${PROBE_BUILD_TAG}"', script)
        self.assertIn('image rm "${FINAL_BUILD_TAG}"', script)
        self.assertIn('container rm --force "${ACTIVE_CONTAINER_NAME}"', script)
        self.assertNotIn("recover_image_id", script)
        self.assertNotIn("RECOVERED_IMAGE_ID", script)
        self.assertIn("recover_container_id", script)
        self.assertIn("require_container_name_absent", script)
        self.assertIn("remove_probe_image", script)

        with tempfile.TemporaryDirectory() as directory:
            malicious = Path(directory)
            sentinel = malicious / "called"
            fake_dirname = malicious / "dirname"
            fake_dirname.write_text(
                f"#!/bin/sh\nprintf called > {sentinel}\n",
                encoding="utf-8",
            )
            fake_dirname.chmod(0o700)
            result = subprocess.run(
                [str(script_path)],
                env={"PATH": str(malicious)},
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 64)
        self.assertFalse(sentinel.exists())

    def test_build_script_rejects_untrusted_docker_credential_helper_states(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source_script = root / "scripts/build_legacy_doc_converter.sh"
        source_dockerfile = root / "containers/legacy-doc/Dockerfile"
        base_image = "debian:bookworm-slim@sha256:" + "a" * 64
        package_version = "4:7.4.7-1+deb12u14"

        for helper_state in ("missing", "directory", "non_executable", "symlink"):
            with (
                self.subTest(helper_state=helper_state),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary_root = Path(directory)
                repository_root = temporary_root / "repository"
                scripts_directory = repository_root / "scripts"
                container_directory = repository_root / "containers" / "legacy-doc"
                docker_desktop_directory = temporary_root / "docker-desktop-bin"
                docker_link = temporary_root / "docker"
                scripts_directory.mkdir(parents=True)
                container_directory.mkdir(parents=True)
                docker_desktop_directory.mkdir(parents=True)

                copied_script = scripts_directory / source_script.name
                shutil.copy2(source_script, copied_script)
                shutil.copy2(source_dockerfile, container_directory / "Dockerfile")

                fake_docker_cli = docker_desktop_directory / "docker"
                fake_docker_cli.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
                fake_docker_cli.chmod(0o700)
                docker_link.symlink_to(fake_docker_cli)

                script = copied_script.read_text(encoding="utf-8")
                docker_desktop_declaration = (
                    'readonly DOCKER_DESKTOP_BIN="/Applications/Docker.app/Contents/Resources/bin"'
                )
                docker_link_declaration = 'readonly DOCKER_BIN="/usr/local/bin/docker"'
                self.assertEqual(script.count(docker_desktop_declaration), 1)
                self.assertEqual(script.count(docker_link_declaration), 1)
                script = script.replace(
                    docker_desktop_declaration,
                    f'readonly DOCKER_DESKTOP_BIN="{docker_desktop_directory}"',
                )
                script = script.replace(
                    docker_link_declaration,
                    f'readonly DOCKER_BIN="{docker_link}"',
                )
                copied_script.write_text(script, encoding="utf-8")
                copied_script.chmod(0o700)

                credential_helper = docker_desktop_directory / "docker-credential-desktop"
                if helper_state == "directory":
                    credential_helper.mkdir()
                elif helper_state == "non_executable":
                    credential_helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    credential_helper.chmod(0o600)
                elif helper_state == "symlink":
                    real_helper = temporary_root / "real-docker-credential-helper"
                    real_helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    real_helper.chmod(0o700)
                    credential_helper.symlink_to(real_helper)

                result = subprocess.run(
                    [str(copied_script), base_image, package_version],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 69)
                self.assertIn(
                    b"trusted Docker setup prerequisites are unavailable",
                    result.stderr,
                )

    def test_build_script_accepts_buildx_iidfile_with_optional_final_newline(self) -> None:
        digest = b"sha256:" + b"a" * 64

        for iidfile_bytes in (digest, digest + b"\n"):
            with self.subTest(iidfile_bytes=iidfile_bytes):
                result, docker_calls = self._run_build_script_with_iidfile_bytes(iidfile_bytes)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"setup stage: probe_identity", result.stderr)
                self.assertTrue(any(call.startswith("image inspect ") for call in docker_calls))

    def test_build_script_stops_before_image_inspect_for_invalid_iidfile(self) -> None:
        digest = b"sha256:" + b"a" * 64
        invalid_iidfiles = (
            b"",
            b"invalid-iidfile\n",
            digest + b"\r\n",
            b"\n" + digest,
            digest + b"\n\n",
            b"sha256:" + b"A" * 64,
            b"sha256:" + b"a" * 63,
            digest + b"\0",
        )

        for iidfile_bytes in invalid_iidfiles:
            with self.subTest(iidfile_bytes=iidfile_bytes):
                result, docker_calls = self._run_build_script_with_iidfile_bytes(iidfile_bytes)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"setup stage: probe_identity", result.stderr)
                self.assertNotIn(b"setup stage: probe_manifest", result.stderr)
                self.assertFalse(any(call.startswith("image inspect ") for call in docker_calls))

    def test_build_script_accepts_docker_cidfile_with_optional_final_newline(self) -> None:
        digest = b"sha256:" + b"a" * 64
        container_id = b"c" * 64

        for cidfile_bytes in (container_id, container_id + b"\n"):
            with self.subTest(cidfile_bytes=cidfile_bytes):
                result, docker_calls = self._run_build_script_with_iidfile_bytes(
                    digest,
                    cidfile_bytes=cidfile_bytes,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"setup stage: probe_manifest", result.stderr)
                self.assertTrue(any(call.startswith("container cp ") for call in docker_calls))

    def test_build_script_stops_before_container_cp_for_invalid_cidfile(self) -> None:
        digest = b"sha256:" + b"a" * 64
        container_id = b"c" * 64
        invalid_cidfiles = (
            b"",
            container_id + b"\r\n",
            b"\n" + container_id,
            container_id + b"\n\n",
            b"C" * 64,
            b"c" * 63,
            container_id + b"\0",
        )

        for cidfile_bytes in invalid_cidfiles:
            with self.subTest(cidfile_bytes=cidfile_bytes):
                result, docker_calls = self._run_build_script_with_iidfile_bytes(
                    digest,
                    cidfile_bytes=cidfile_bytes,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"setup stage: probe_manifest", result.stderr)
                self.assertFalse(any(call.startswith("container cp ") for call in docker_calls))

    def test_build_script_rejects_symlink_invocation_and_authenticates_dockerfile(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_path = root / "scripts/build_legacy_doc_converter.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('if [[ -L "${SCRIPT_SOURCE}" ]]', script)
        self.assertIn("PHYSICAL_SCRIPT_DIRECTORY", script)
        self.assertIn(
            'readonly EXPECTED_DOCKERFILE_SHA256="'
            "1efbc4e17e65bdf39134a0031960e9de3a68a625affbc16a1b5723ce0388f25b"
            '"',
            script,
        )
        self.assertIn('! -L "${REPOSITORY_DOCKERFILE}"', script)
        self.assertIn('"dockerfile_sha256":"%s"', script)

        with tempfile.TemporaryDirectory() as directory:
            evil_root = Path(directory) / "evil"
            evil_scripts = evil_root / "scripts"
            evil_container = evil_root / "containers" / "legacy-doc"
            evil_scripts.mkdir(parents=True)
            evil_container.mkdir(parents=True)
            linked_script = evil_scripts / script_path.name
            linked_script.symlink_to(script_path)
            evil_container.joinpath("Dockerfile").write_text(
                "FROM debian:bookworm-slim\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(linked_script),
                    "debian:bookworm-slim@sha256:" + "a" * 64,
                    "25.2.3.2-1",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 66)
        self.assertIn(b"symlink", result.stderr)

        with tempfile.TemporaryDirectory() as directory:
            repository_link = Path(directory) / "repository-link"
            repository_link.symlink_to(root, target_is_directory=True)
            result = subprocess.run(
                [str(repository_link / "scripts" / script_path.name)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 64)
        self.assertNotIn(b"symlink invocation", result.stderr)

    def test_runtime_docs_require_pull_never_network_none_and_no_host_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        converter_readme = (root / "containers/legacy-doc/README.md").read_text(encoding="utf-8")
        implementation_plan = (
            root / "docs/superpowers/plans/2026-07-13-kunjin-phase-d1-1-b-isolated-legacy-doc.md"
        ).read_text(encoding="utf-8")
        combined = readme + "\n" + converter_readme

        for phrase in (
            "--pull=never",
            "--network=none",
            "never pulls or builds",
            "no host `textutil` fallback",
            "no host LibreOffice fallback",
            "--user=<host-uid>:<host-gid>",
            "conversion stdout and stderr are never captured",
            "private bounded metadata queries",
        ):
            self.assertIn(phrase, combined)
        for document in (converter_readme, implementation_plan):
            self.assertIn("libreoffice-writer-nogui", document)
            self.assertIn("no-GUI", document)
            self.assertIn("reduces GUI dependencies", document)
            self.assertIn("conversion contract remains unchanged", document)

    def test_skill_preserves_conversion_stage_and_reason_as_technical_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(encoding="utf-8")

        for phrase in (
            "fund converter-status",
            "failure_stage=conversion",
            "legacy_converter_unavailable",
            "legacy_converter_timeout",
            "legacy_converter_resource_limit",
            "legacy_converter_failed",
            "legacy_converter_output_invalid",
            "technical evidence only",
            "Conversion success is not financial evidence",
            "D1.1-C",
            "D2",
            "D3",
            "Phase E",
            "no host `textutil` fallback",
            "no host LibreOffice fallback",
        ):
            self.assertIn(phrase, skill)


if __name__ == "__main__":
    unittest.main()
