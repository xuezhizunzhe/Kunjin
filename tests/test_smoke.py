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
            (
                "Phase B, Phase C, D1, complete D2 (the Phase 1 minimum subset "
                "never satisfies this gate), D3, and post-trade"
            ),
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

    def test_kunjin_skill_uses_phase1_held_fund_brief_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())

        shared_contract = (
            "one currently held fund",
            "--json fund brief 519755 --action continue_holding --mode rapid",
            (
                "when the request includes `continue_holding`, `reduce_to_cash`, "
                "`full_exit`, or `switch_funds`"
            ),
            "`fact_research` is always added internally",
            "Fact-only questions stay on the standalone `fact_research` route",
            (
                "Any buy or add request, including an already-held fund, stays on "
                "standalone `buy_or_add`"
            ),
            "never satisfies the complete D2 gate",
            "`terminal_status=complete`",
            "no scheduled work was omitted",
            "not a financial conclusion",
            "`sync_status`",
            "`decision_evidence_status`",
            "Tier 2",
            "data date",
            "minimum D2 subset",
            "`minimum_relationship_coverage`",
            "`disclosed_holdings_coverage`",
            "`exact_amount_available=false`",
            "conditional",
            "Broad financial-media ingestion",
            "complete D2",
            "D3",
            "Phase E",
            "not implemented",
        )
        for phrase in shared_contract:
            self.assertIn(phrase, normalized_readme)
            self.assertIn(phrase, normalized_skill)

        self.assertIn("`fund brief` owns the 90/480-second budget", normalized_skill)
        self.assertIn("Never orchestrate legacy commands in its place", normalized_skill)
        self.assertIn("unknown relationships as unknown", normalized_skill)
        self.assertNotIn(
            "D2 portfolio correlation and overlap controls and D3 product-selection",
            normalized_skill,
        )
        self.assertIn("$kunjin-fund", agent)
        self.assertIn("held-fund brief", agent)
        self.assertIn("conditional evidence", agent)

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

    def test_fund_brief_cli_surface_is_json_only_and_bounded(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(
            [
                "--json",
                "fund",
                "brief",
                "519755",
                "--action",
                "continue_holding",
                "--mode",
                "rapid",
            ]
        )

        self.assertTrue(parsed.json_output)
        self.assertEqual(parsed.command, "fund")
        self.assertEqual(parsed.fund_command, "brief")
        self.assertEqual(parsed.fund_code, "519755")
        self.assertEqual(parsed.action, "continue_holding")
        self.assertEqual(parsed.mode, "rapid")
        for forbidden in (
            "amount",
            "shares",
            "date",
            "url",
            "path",
            "token",
            "adapter",
            "docker",
            "background",
        ):
            self.assertFalse(hasattr(parsed, forbidden))

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
            "target_fd = os.open(",
            "quarantine_name",
            "PROC_PIDT_BSDINFOWITHUNIQID",
            "KERN_PROCARGS2",
            "KUNJIN_PHASE0_RUN_ID",
            "p_uniqueid",
            "p_puniqueid",
            "ctypes.sizeof(ctypes.c_int)",
            "stabilize_descendants",
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
    "residual_process", "detached_worker", "detached_worker_immediate",
    "detached_worker_double_fork"
} and argv == [
    "--json", "version"
]:
    worker_code = (
        "import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(4)"
    )
    if scenario == "detached_worker_double_fork":
        launcher_code = (
            "import os,subprocess,sys;from pathlib import Path;"
            f"worker=subprocess.Popen([sys.executable,'-c',{worker_code!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,start_new_session=True);"
            "Path(os.environ['FAKE_KUNJIN_PID']).write_text("
            "str(worker.pid),encoding='ascii');"
            "Path(os.environ['FAKE_KUNJIN_WORKER_READY']).write_text("
            "'ready',encoding='ascii')"
        )
        launcher = subprocess.Popen(
            [sys.executable, "-c", launcher_code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        launcher.wait(timeout=1)
    else:
        residual = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os;from pathlib import Path;"
                    "ready=os.environ.get('FAKE_KUNJIN_WORKER_READY');"
                    "ready and Path(ready).write_text('ready',encoding='ascii');"
                    + worker_code
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
    if scenario in {"detached_worker_immediate", "detached_worker_double_fork"}:
        ready = Path(os.environ["FAKE_KUNJIN_WORKER_READY"])
        deadline = time.monotonic() + 1
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.001)
        if not ready.exists():
            raise SystemExit("detached worker did not reach exec-ready state")

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
                    "announcement",
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

            for scenario in (
                "detached_worker",
                "detached_worker_immediate",
                "detached_worker_double_fork",
            ):
                with self.subTest(detached_scenario=scenario):
                    detached_output = temporary_root / f"output-{scenario}"
                    detached_pid = temporary_root / f"{scenario}.pid"
                    detached_ready = temporary_root / f"{scenario}.ready"
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
                            "FAKE_KUNJIN_WORKER_READY": str(detached_ready),
                            "FAKE_KUNJIN_SCENARIO": scenario,
                            "KUNJIN_PHASE0_TEST_SKIP_LIVE_DESCENDANT_SCAN": (
                                "1" if scenario == "detached_worker_double_fork" else "0"
                            ),
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

            replacement_source = temporary_root / "replacement-source"
            replacement_source.mkdir()
            replacement_sentinel = replacement_source / "must-remain.txt"
            replacement_sentinel.write_text("unrelated", encoding="ascii")
            replaced_output = temporary_root / "output-replaced-after-rename"
            replaced = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(replaced_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_SCENARIO": "complete",
                    "KUNJIN_PHASE0_TEST_RENAME_HOOK": "replace_after_rename",
                    "KUNJIN_PHASE0_TEST_REPLACEMENT_SOURCE": str(
                        replacement_source
                    ),
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(replaced.returncode, 0)
            self.assertTrue(replaced_output.is_dir())
            self.assertEqual(
                (replaced_output / replacement_sentinel.name).read_text(
                    encoding="ascii"
                ),
                "unrelated",
            )
            self.assertIn(
                "concurrent publication residue may remain",
                replaced.stderr.decode(),
            )
            self.assertEqual(
                [
                    item
                    for item in temporary_root.iterdir()
                    if item.name.startswith(".kunjin-phase0-quarantine-")
                ],
                [],
            )
            displaced = [
                item
                for item in temporary_root.iterdir()
                if item.name.startswith(".kunjin-phase0-displaced-")
            ]
            self.assertEqual(len(displaced), 1)
            for child in displaced[0].iterdir():
                child.unlink()
            displaced[0].rmdir()

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

            post_quarantine_source = temporary_root / "post-quarantine-source"
            post_quarantine_source.mkdir()
            post_quarantine_sentinel = post_quarantine_source / "must-remain.txt"
            post_quarantine_sentinel.write_text("unrelated", encoding="ascii")
            post_quarantine_output = temporary_root / "output-post-quarantine"
            post_quarantine = subprocess.run(
                [
                    str(scripts_directory / source_script.name),
                    "000001",
                    str(post_quarantine_output),
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(log_path),
                    "FAKE_KUNJIN_SCENARIO": "complete",
                    "KUNJIN_PHASE0_ACCEPTANCE_TIMEOUT_SECONDS": "1",
                    "KUNJIN_PHASE0_TEST_RENAME_HOOK": (
                        "replace_after_quarantine_removal"
                    ),
                    "KUNJIN_PHASE0_TEST_REPLACEMENT_SOURCE": str(
                        post_quarantine_source
                    ),
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=3,
            )
            self.assertNotEqual(post_quarantine.returncode, 0)
            self.assertEqual(
                (post_quarantine_output / post_quarantine_sentinel.name).read_text(
                    encoding="ascii"
                ),
                "unrelated",
            )
            self.assertIn(
                "concurrent publication residue may remain",
                post_quarantine.stderr.decode(),
            )
            self.assertEqual(
                [
                    item
                    for item in temporary_root.iterdir()
                    if item.name.startswith(".kunjin-phase0-quarantine-")
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

    def _install_phase1_acceptance_fixture(self, temporary_root: Path) -> tuple[Path, Path]:
        root = Path(__file__).resolve().parents[1]
        repository = temporary_root / "repository"
        scripts = repository / "scripts"
        cli_directory = repository / ".venv" / "bin"
        scripts.mkdir(parents=True)
        cli_directory.mkdir(parents=True)
        acceptance = scripts / "run_phase1_acceptance.sh"
        shutil.copy2(root / "scripts" / acceptance.name, acceptance)
        cli = cli_directory / "kunjin"
        cli.write_text(
            r'''#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
log = Path(os.environ["FAKE_KUNJIN_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(argv, separators=(",", ":")) + "\n")

scenario = os.environ.get("FAKE_KUNJIN_SCENARIO", "healthy")
if argv[:3] == ["--json", "source", "status"]:
    code = argv[argv.index("--fund-code") + 1]
    now = "2026-07-17T00:00:00+00:00"
    payload = {
        "schema_version": "1",
        "command": "source.status",
        "as_of": now,
        "data": {
            "fund_code": code,
            "mode": "rapid",
            "policy_checksum": "c" * 64,
            "policy_version": "1",
            "registry_checksum": "d" * 64,
            "registry_version": "1",
            "request_field_resolutions": [{
                "action": "fact_research",
                "field_id": "fund_manager_product_announcement",
                "primary_source_id": (
                    "unrelated_source"
                    if scenario == "unbound_supplementation"
                    else "fund_manager_official_documents"
                ),
                "resolution": "manual_supplement_required",
                "risk_effect": "information",
            }],
            "request_id": "e" * 32,
            "snapshot_at": now,
            "source_fields": [{
                "acceptable_alternatives": [],
                "consecutive_failures": 1,
                "cooldown_until": None,
                "field_id": "fund_manager_product_announcement",
                "field_scope": "official product announcement",
                "last_failure_at": now,
                "last_failure_reason": "unsupported_source_family",
                "last_success_at": None,
                "last_success_data_as_of": None,
                "source_id": "fund_manager_official_documents",
                "source_kind": "official_document",
                "source_scope": "fund manager official website",
                "source_tier": "tier_1",
                "state": "unsupported",
                "supplementation": {
                    "accepted_input": ["公开官方公告 URL", "带日期的官方公告截图"],
                    "freshness_requirement": "当前有效版本",
                    "impact_if_missing": "不能排除影响持有或退出判断的正式公告",
                    "missing_item": "fund_manager_product_announcement",
                    "suggested_location": "基金管理人官网产品公告页",
                    "supported_without_it": "仍可展示已取得的基金事实",
                    "unsupported_without_it": "不能形成公告驱动的行动判断",
                    "why_required": "正式公告可能改变申购、赎回或存续状态",
                },
            }],
        },
        "warnings": [],
        "errors": [],
    }
    if scenario == "spliced_supplementation":
        announcement_field = payload["data"]["source_fields"][0]
        announcement_resolution = payload["data"]["request_field_resolutions"][0]
        announcement_resolution["resolution"] = "usable"
        fee_field = json.loads(json.dumps(announcement_field))
        fee_field["field_id"] = "fees_share_class_relationship"
        fee_field["source_id"] = "fund_manager_official_fees"
        fee_field["supplementation"]["missing_item"] = "fees_share_class_relationship"
        fee_resolution = json.loads(json.dumps(announcement_resolution))
        fee_resolution["field_id"] = "fees_share_class_relationship"
        fee_resolution["primary_source_id"] = "fund_manager_official_fees"
        fee_resolution["resolution"] = "manual_supplement_required"
        payload["data"]["source_fields"] = [fee_field, announcement_field]
        payload["data"]["request_field_resolutions"] = [
            fee_resolution,
            announcement_resolution,
        ]
    if scenario == "partial_supplementation":
        payload["data"]["request_field_resolutions"][0]["resolution"] = "partial"
        source_field = payload["data"]["source_fields"][0]
        source_field["consecutive_failures"] = 0
        source_field["last_failure_at"] = None
        source_field["last_failure_reason"] = None
        source_field["state"] = "not_checked"
        market_field = json.loads(json.dumps(source_field))
        market_field["field_id"] = "market_context"
        market_field["source_id"] = "eastmoney_market"
        market_field["source_tier"] = "tier_2"
        market_field["supplementation"]["missing_item"] = "market_context"
        market_resolution = json.loads(
            json.dumps(payload["data"]["request_field_resolutions"][0])
        )
        market_resolution["field_id"] = "market_context"
        market_resolution["primary_source_id"] = "eastmoney_market"
        payload["data"]["source_fields"].append(market_field)
        payload["data"]["request_field_resolutions"].append(market_resolution)
        manager_field = json.loads(json.dumps(source_field))
        manager_field["field_id"] = "current_manager_team"
        manager_field["supplementation"]["missing_item"] = "current_manager_team"
        manager_resolution = json.loads(
            json.dumps(payload["data"]["request_field_resolutions"][0])
        )
        manager_resolution["field_id"] = "current_manager_team"
        payload["data"]["source_fields"].append(manager_field)
        payload["data"]["request_field_resolutions"].append(manager_resolution)
    json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    raise SystemExit(0)

action = argv[argv.index("--action") + 1]
code = argv[3]
if scenario == "timeout" and action == "continue_holding":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(os.environ["FAKE_KUNJIN_PID"]).write_text(str(os.getpid()), encoding="ascii")
    time.sleep(5)
if scenario == "spawn_on_term" and action == "continue_holding":
    Path(os.environ["FAKE_KUNJIN_PID"]).write_text(str(os.getpid()), encoding="ascii")
    def spawn_descendant(_signal_number, _frame):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(5)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        Path(os.environ["FAKE_KUNJIN_SPAWNED_PID"]).write_text(
            str(child.pid), encoding="ascii"
        )
    signal.signal(signal.SIGTERM, spawn_descendant)
    time.sleep(5)
if scenario == "detached" and action == "continue_holding":
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(5)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    Path(os.environ["FAKE_KUNJIN_PID"]).write_text(str(child.pid), encoding="ascii")
if scenario == "oversized" and action == "continue_holding":
    sys.stdout.write("x" * (4 * 1024 * 1024 + 1))
    raise SystemExit(0)
if scenario == "oversized_stderr" and action == "continue_holding":
    sys.stderr.write("x" * (4 * 1024 * 1024 + 1))
    raise SystemExit(0)

now = "2026-07-17T00:00:00+00:00"
source_tier = "tier_2" if code == "000002" else "tier_1"

def fact(index, field_id, value):
    return {
        "calculated": False,
        "canonical_url": f"https://example.invalid/{code}/{field_id}",
        "completeness": "complete",
        "conflict_ids": [],
        "data_as_of": now,
        "fact_id": f"fact_{index}",
        "field_id": field_id,
        "freshness": "current",
        "published_at": now,
        "publisher": "公开验收来源",
        "retrieved_at": now,
        "source_id": f"source_{index}",
        "source_lineage_id": f"lineage_{index}",
        "source_tier": ("tier_2" if field_id == "formal_nav" else source_tier),
        "unit": None,
        "value": value,
    }

fact_fields = (
    ("share_class_identity", {
        "fund_name": "公开验收基金A",
        "related_fund_code": code,
        "share_class": "A",
    }),
    ("current_manager_team", {
        "manager_name": "公开基金经理",
        "tenure_end": None,
        "tenure_start": "2024-01-01",
    }),
    ("formal_nav", "1.2345"),
    ("fees_share_class_relationship", {
        "effective_from": "2024-01-01",
        "effective_to": None,
        "fee_type": "management",
        "fixed_fee": None,
        "holding_days_maximum": None,
        "holding_days_minimum": None,
        "rate": "0.5%",
        "rule_order": 1,
        "share_class": "A",
        "threshold_maximum": None,
        "threshold_minimum": None,
    }),
    ("holdings_industries", {
        "disclosure_scope": ["top10"],
        "items": [{
            "asset_class": "equity",
            "disclosed_weight": "5.2",
            "rank": 1,
            "security_code": "600000",
            "security_name": "公开持仓",
        }],
        "report_period": "2026-06-30",
    }),
    ("fund_manager_product_announcement", {
        "category": "periodic",
        "record_published_at": now,
        "record_publisher": "公开验收来源",
        "record_url": f"https://example.invalid/{code}/announcement",
        "title": "公开验收公告",
    }),
    ("redemption_terms", {
        "fee_condition": "holding_period_required",
        "settlement_condition": "published_rule_available",
    }),
)
facts = [fact(index, field_id, value) for index, (field_id, value) in enumerate(fact_fields)]
unsupported = code == "000002"
action_ids = {
    "continue_holding": ["fact_research", "continue_holding"],
    "reduce_to_cash": ["fact_research", "reduce_to_cash"],
    "full_exit": ["fact_research", "full_exit"],
    "switch_funds": ["fact_research", "switch_reduce", "switch_buy"],
}[action]
owner_actions = action_ids[1:]

def interpretation(action_id):
    switch_buy = action_id == "switch_buy"
    transaction_action = action_id in {
        "reduce_to_cash", "full_exit", "switch_reduce", "switch_buy"
    }
    unavailable = ["exact_amount"]
    if transaction_action:
        unavailable.append("automatic_trade")
    if switch_buy:
        unavailable.append("switch_buy")
    state = (
        "abstain"
        if switch_buy
        else (
            "reduce_or_exit_review"
            if transaction_action
            else "watch"
        )
    )
    return {
        "action_id": action_id,
        "action_maturity": "experimental_shadow",
        "blocking_codes": (["d3_missing", "post_trade_missing"] if switch_buy else []),
        "exact_amount_available": False,
        "invalidation_conditions": ["证据变化时重新评估"],
        "missing_fields": (["d3", "post_trade"] if switch_buy else []),
        "opposing_evidence_ids": [],
        "state": state,
        "state_inputs": {"phase_b_blocked": False},
        "supporting_evidence_ids": ["fact_0"],
        "unavailable_actions": unavailable,
    }

interpretations = [interpretation(item) for item in owner_actions]
primary_interpretation = next(
    (
        item
        for item in interpretations
        if item["state"] == "reduce_or_exit_review"
    ),
    interpretations[0],
)
primary_state = primary_interpretation["state"]
top_blocking_codes = sorted({
    code for item in interpretations for code in item["blocking_codes"]
})

def state_text(state):
    return {
        "reduce_or_exit_review": (
            "本次规则结果进入减仓或退出复核流程（reduce_or_exit_review）；"
            "不表示系统发现了确定卖出信号，也不是立即赎回指令。"
        ),
        "watch": (
            "本次规则结果为继续观察（watch）；"
            "现有证据不足以形成确定的持有、减仓或退出结论。"
        ),
        "abstain": (
            "本次暂不形成行动倾向（abstain）；"
            "请先处理列示的证据缺口、冲突或交易限制。"
        ),
    }[state]

def headline_item_text(item):
    text = state_text(item["state"])
    if item["action_id"] == "switch_reduce":
        return "转出腿：" + text
    if item["action_id"] == "switch_buy":
        return "转入腿：" + text + " 不得从转出腿继承许可。"
    return text

coverage = {
    "coverage_id": "minimum_relationship_coverage",
    "evidence_ids": ["relationship_1"],
    "evidence_state": "complete",
    "included_fund_codes": [code, "000003"],
    "known_percent": None,
    "omitted_fund_codes": [],
    "scope": "current_portfolio",
    "unknown_fields": [],
}
holdings_coverage = dict(coverage)
holdings_coverage["coverage_id"] = "disclosed_holdings_coverage"
relationship = {
    "evidence_ids": ["fact_4"],
    "evidence_state": "complete",
    "fund_codes": [code],
    "metrics": {"multiple_observations": True},
    "publication_times": [now],
    "relationship_id": "relationship_1",
    "relationship_type": "duplicate_holding_identity",
    "report_periods": [],
    "warnings": [],
}
status = {
    "acceptable_alternative_ids": (["user_official_document"] if unsupported else []),
    "conflicted_fields": [],
    "cooldown_fields": [],
    "manual_supplementation_codes": (
        ["official_events_manual_supplement_required"] if unsupported else []
    ),
    "missing_fields": (["official_events"] if unsupported else []),
    "obtained_fields": [item[0] for item in fact_fields],
    "required_fields": [item[0] for item in fact_fields],
    "stale_fields": [],
    "state": ("partial" if unsupported else "complete"),
    "supported_interpretations": owner_actions,
    "unsupported_fields": (["official_events"] if unsupported else []),
    "unsupported_interpretations": [],
}
missing = []
if unsupported:
    missing = [{
        "affected_action_ids": owner_actions,
        "condition": "unsupported",
        "field_id": "official_events",
        "scope": "decision_evidence_status",
    }]
payload = {
    "schema_version": "1",
    "command": "fund.brief",
    "as_of": now,
    "data": {
        "request": {
            "action_ids": action_ids,
            "created_at": now,
            "decision_snapshot_id": 1,
            "evidence_fingerprint": "a" * 64,
            "mode": "rapid",
            "omitted_work": (["official_events"] if unsupported else []),
            "request_run_id": 1,
            "result_checksum": "b" * 64,
            "terminal_status": ("partial" if unsupported else "complete"),
        },
        "subject": {
            "fund_code": code,
            "observation_version": "synthetic_non_personal_v1",
            "observed_at": now,
            "portfolio_evidence_state": "current",
            "portfolio_weight": "0.25",
            "position_present": True,
        },
        "facts": facts,
        "official_events": [],
        "portfolio_relationship": {
            "disclosed_holdings_coverage": holdings_coverage,
            "minimum_relationship_coverage": coverage,
            "relationships": [relationship],
        },
        "sync_status": status,
        "decision_evidence_status": status,
        "action_interpretation": {
            "action_maturity": "experimental_shadow",
            "affected_action_abstentions": (["switch_buy"] if action == "switch_funds" else []),
            "blocking_codes": top_blocking_codes,
            "conflicts": [],
            "constraints": [],
            "interpretations": interpretations,
            "primary_state": primary_state,
            "triggered_reviews": [],
        },
        "missing_evidence": missing,
        "beginner_explanation_zh": {
            "headline": {
                "action_maturity": "experimental_shadow",
                "items": [{
                    "action_id": item["action_id"],
                    "action_maturity": item["action_maturity"],
                    "state": item["state"],
                    "text": headline_item_text(item),
                } for item in interpretations],
                "maturity_scope": (
                    "primary_state_only" if action == "switch_funds" else "all_actions"
                ),
                "maturity_text": "实验性状态不授权交易。",
                "primary_state": primary_state,
                "text": state_text(primary_state),
            },
            "fund_identity": {"data_dates": [], "evidence_ids": [], "text": "身份事实"},
            "portfolio_relationship": {
                "coverage_ids": [],
                "relationship_ids": [],
                "text": "组合关系",
                "unknown_fields": {},
            },
            "recent_official_events": {
                "event_ids": [],
                "inactive_items": [],
                "text": "正式公告范围",
            },
            "why_this_state": {"items": [], "text": "条件判断"},
            "evidence_gaps": {"items": missing, "text": "证据缺口"},
            "change_conditions": {"items": [], "text": "重新评估条件"},
        },
    },
    "warnings": [],
    "errors": [],
}
if scenario == "unknown_field" and action == "continue_holding":
    payload["data"]["unknown_field"] = "must fail closed"
if scenario == "private_sentinel" and action == "continue_holding":
    payload["data"]["facts"][0]["value"]["fund_name"] = (
        "PRIVATE_ACCEPTANCE_SENTINEL_918273645"
    )
if scenario == "stale_identity" and action == "continue_holding":
    payload["data"]["facts"][0]["freshness"] = "stale"
if scenario == "unsafe_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = "今天买入100元"
if scenario == "disguised_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "建议全额赎回，但不构成交易指令"
    )
if scenario == "exact_share_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "赎回100份，但不构成交易指令"
    )
if scenario == "bare_liquidation_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "清仓，但不构成交易指令"
    )
if scenario == "half_shares_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "赎回一半份额，但不构成交易指令"
    )
if scenario == "half_position_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "卖掉半仓，但不构成交易指令"
    )
if scenario == "chinese_exact_shares_headline" and action == "continue_holding":
    payload["data"]["beginner_explanation_zh"]["headline"]["text"] = (
        "赎回一百份，但不构成交易指令"
    )
if scenario == "invalid_reduce_state" and action == "reduce_to_cash":
    payload["data"]["action_interpretation"]["interpretations"][0]["state"] = "watch"
    payload["data"]["beginner_explanation_zh"]["headline"]["items"][0]["state"] = "watch"
    payload["data"]["action_interpretation"]["primary_state"] = "watch"
    payload["data"]["beginner_explanation_zh"]["headline"]["primary_state"] = "watch"
if scenario == "switch_buy_permission" and action == "switch_funds":
    buy = payload["data"]["action_interpretation"]["interpretations"][1]
    buy["state"] = "hold"
    buy["blocking_codes"] = []
    buy["missing_fields"] = []
    buy["unavailable_actions"] = []
if scenario == "switch_primary_mismatch" and action == "switch_funds":
    payload["data"]["action_interpretation"]["primary_state"] = "abstain"
    payload["data"]["beginner_explanation_zh"]["headline"]["primary_state"] = "abstain"
if scenario == "private_metric" and action == "continue_holding":
    payload["data"]["portfolio_relationship"]["relationships"][0]["metrics"] = {
        "owner_weight": "0.25"
    }
if scenario == "owner_coverage_unknown" and action == "continue_holding":
    disclosed = payload["data"]["portfolio_relationship"][
        "disclosed_holdings_coverage"
    ]
    disclosed["evidence_state"] = "partial"
    disclosed["known_percent"] = None
    disclosed["unknown_fields"] = ["top10_overlap_unknown"]
    payload["data"]["decision_evidence_status"]["state"] = "partial"
    payload["data"]["decision_evidence_status"]["missing_fields"] = [
        "manager_evidence_missing_000999"
    ]
if scenario in {"labeled_partial", "inconsistent_labeled_partial"} and code == "000001":
    payload["data"]["facts"] = [
        fact_item
        for fact_item in payload["data"]["facts"]
        if fact_item["field_id"] != "holdings_industries"
    ]
    payload["data"]["missing_evidence"].append({
        "affected_action_ids": owner_actions,
        "condition": "missing",
        "field_id": f"holdings_industries_{code}",
        "scope": "disclosed_holdings_coverage",
    })
    for fact_item in payload["data"]["facts"]:
        if fact_item["field_id"] in {
            "share_class_identity",
            "current_manager_team",
            "formal_nav",
        }:
            fact_item["source_tier"] = "tier_2"
            fact_item["freshness"] = "dated_history"
            fact_item["completeness"] = "partial"
    disclosed = payload["data"]["portfolio_relationship"][
        "disclosed_holdings_coverage"
    ]
    disclosed["evidence_state"] = "insufficient"
    disclosed["included_fund_codes"] = []
    disclosed["omitted_fund_codes"] = [code]
    disclosed["unknown_fields"] = [f"holdings_industries_{code}"]
    disclosed["evidence_ids"] = []
if scenario == "labeled_partial" and code == "000001":
    sync_status = payload["data"]["sync_status"]
    sync_status["state"] = "partial"
    sync_status["missing_fields"] = ["identity_active_status"]
    sync_status["obtained_fields"] = [
        field_id
        for field_id in sync_status["obtained_fields"]
        if field_id != "holdings_industries"
    ]
    decision_status = json.loads(json.dumps(sync_status))
    decision_status["state"] = "insufficient"
    decision_status["obtained_fields"] = ["formal_nav"]
    decision_status["supported_interpretations"] = []
    decision_status["unsupported_interpretations"] = owner_actions
    payload["data"]["decision_evidence_status"] = decision_status
    payload["data"]["missing_evidence"].append({
        "affected_action_ids": owner_actions,
        "condition": "missing",
        "field_id": "identity_active_status",
        "scope": "decision_evidence_status",
    })
    for item in payload["data"]["action_interpretation"]["interpretations"]:
        item["state"] = "abstain"
        if "identity_active_status_missing" not in item["blocking_codes"]:
            item["blocking_codes"].append("identity_active_status_missing")
            item["blocking_codes"].sort()
        if "identity_active_status" not in item["missing_fields"]:
            item["missing_fields"].append("identity_active_status")
            item["missing_fields"].sort()
    action = payload["data"]["action_interpretation"]
    action["primary_state"] = "abstain"
    action["affected_action_abstentions"] = owner_actions
    action["blocking_codes"] = sorted({
        code
        for item in action["interpretations"]
        for code in item["blocking_codes"]
    })
    headline = payload["data"]["beginner_explanation_zh"]["headline"]
    headline["primary_state"] = "abstain"
    headline["text"] = state_text("abstain")
    for headline_item, item in zip(headline["items"], action["interpretations"]):
        headline_item["state"] = "abstain"
        headline_item["text"] = headline_item_text(item)
if scenario == "inconsistent_redemption_terms" and code == "000001":
    for fact_item in payload["data"]["facts"]:
        if fact_item["field_id"] == "redemption_terms":
            fact_item["source_tier"] = "tier_2"
            fact_item["freshness"] = "dated_history"
            fact_item["completeness"] = "partial"
beginner = payload["data"]["beginner_explanation_zh"]
facts_by_field = {
    field_id: next(
        (item for item in payload["data"]["facts"] if item["field_id"] == field_id),
        None,
    )
    for field_id in {
        "share_class_identity", "identity_active_status", "current_manager_team",
        "formal_nav", "fees_share_class_relationship", "holdings_industries",
    }
}

def evidence_marker(item):
    data_date = item["data_as_of"] or item["published_at"] or "日期未知"
    return f"{data_date}，{item['source_tier'].replace('tier_', 'Tier ')}"

identity = facts_by_field["share_class_identity"] or facts_by_field["identity_active_status"]
if identity is None:
    beginner["fund_identity"]["text"] = "基金身份与份额类别未取得。"
else:
    identity_value = identity["value"]
    beginner["fund_identity"]["text"] = (
        f"基金：{identity_value.get('fund_name', '名称未取得')}；"
        f"份额类别：{identity_value.get('share_class', '未取得')}。"
        f"身份依据：{evidence_marker(identity)}。"
    )
manager = facts_by_field["current_manager_team"]
nav = facts_by_field["formal_nav"]
fee = facts_by_field["fees_share_class_relationship"]
holdings = facts_by_field["holdings_industries"]
beginner["why_this_state"]["text"] = (
    (
        "当前经理未取得"
        if manager is None
        else f"当前经理：{manager['value']['manager_name']}（{evidence_marker(manager)}）"
    )
    + "；"
    + (
        "正式净值未取得"
        if nav is None
        else f"正式净值：{nav['value']}（{evidence_marker(nav)}）"
    )
    + "；"
    + (
        "费用与份额规则未取得"
        if fee is None
        else f"费用与份额规则：已取得（{evidence_marker(fee)}）"
    )
    + "；"
    + (
        "披露持仓及报告期未取得"
        if holdings is None
        else (
            f"披露持仓：已取得，报告期：{holdings['value']['report_period']}"
            f"（{evidence_marker(holdings)}）"
        )
    )
    + "。"
)
beginner["portfolio_relationship"]["text"] = (
    "已知关系：同一基金存在多条持仓观察；组合覆盖明确列示；"
    "这里只是最小关系子集，不是完整 D2；"
    "未知持仓不按零重叠处理，也不表示分散充分。"
)
if payload["data"]["portfolio_relationship"]["disclosed_holdings_coverage"][
    "evidence_state"
] == "insufficient":
    beginner["portfolio_relationship"]["text"] = (
        "已知关系：同一基金存在多条持仓观察；披露持仓覆盖不足；"
        "这里只是最小关系子集，不是完整 D2；"
        "未知持仓不按零重叠处理，也不表示分散充分。"
    )
labels = {
    "holdings_industries_000001": "披露持仓",
    "identity_active_status": "基金身份",
    "official_events": "基金正式公告事件",
}
beginner_gaps = []
for gap in payload["data"]["missing_evidence"]:
    manual = gap["field_id"] == "official_events" and unsupported
    beginner_gaps.append({
        **gap,
        "label_zh": labels.get(gap["field_id"], "待补充证据"),
        "source_resolution": ({
            "acceptable_alternative_ids": ["eastmoney_f10"],
            "primary_source_id": "fund_manager_official_documents",
            "resolution": "manual_supplement_required",
            "source_field_id": "fund_manager_product_announcement",
            "source_states": ["unsupported"],
        } if manual else None),
        "supplementation": ({
            "accepted_input": ["URL", "PDF", "screenshot", "field"],
            "freshness_requirement": "current applicable version",
            "impact_if_missing": "action gates remain blocked",
            "missing_item": "fund_manager_product_announcement",
            "suggested_location": "official fund manager announcements",
            "supported_without_it": "other independently evidenced facts",
            "unsupported_without_it": "announcement-driven action conclusion",
            "why_required": "official events may change action availability",
        } if manual else None),
        "next_step": ({
            "action": "按同一缺口列出的受控补证要求提供材料；补证前保持相关动作 abstain。",
            "status": "manual_supplement_required",
        } if manual else {
            "action": "后续新请求按登记来源进行一次有边界检查，本次保持相关动作 abstain。",
            "status": "not_checked",
        }),
    })
beginner["evidence_gaps"]["items"] = beginner_gaps
if scenario == "brief_manual_unbound" and code == "000002":
    for item in beginner["evidence_gaps"]["items"]:
        if item["field_id"] == "official_events":
            item["source_resolution"] = None
            item["supplementation"] = None
if scenario == "wrong_beginner_fact" and code == "000001":
    beginner["why_this_state"]["text"] = beginner["why_this_state"]["text"].replace(
        "公开基金经理",
        "错误基金经理",
    )
fixture_fd_text = os.environ.get("KUNJIN_PHASE1_PUBLIC_FIXTURE_FD")
marker_fd_text = os.environ.get("KUNJIN_PHASE1_PUBLIC_MARKER_FD")
if fixture_fd_text is not None or marker_fd_text is not None:
    fixture_fd = int(fixture_fd_text)
    marker_fd = int(marker_fd_text)
    fixture = json.loads(os.read(fixture_fd, 512).decode("ascii"))
    os.close(fixture_fd)
    if fixture["fund_code"] != code or fixture["run_id"] != os.environ[
        "KUNJIN_PHASE1_RUN_ID"
    ]:
        raise SystemExit(93)
    marker = json.dumps(
        {
            "contract": "kunjin_phase1_public_portfolio_used_v1",
            "fund_code": code,
            "observation_version": "synthetic_non_personal_v1",
            "payload_sha256": "f" * 64,
            "request_id": "9" * 32,
            "run_id": fixture["run_id"],
            "schema_version": 1,
            "source_attempt_id": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    os.write(marker_fd, marker)
    os.close(marker_fd)
json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
''',
            encoding="utf-8",
        )
        cli.chmod(0o700)
        return acceptance, cli

    def test_phase1_acceptance_script_contract_is_strict_and_amount_free(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script_path = root / "scripts" / "run_phase1_acceptance.sh"
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script_path.stat().st_mode & 0o100)
        for phrase in (
            "set -euo pipefail",
            '^[0-9]{6}$',
            "USEFUL_PARTIAL_CODE UNSUPPORTED_PUBLIC_CODE OUTPUT_DIR",
            "--owner OUTPUT_DIR",
            "KUNJIN_PHASE1_RUN_ID",
            "start_new_session=True",
            "SIGTERM",
            "SIGKILL",
            "MAX_RAW_BYTES",
            "renameatx_np",
            "RENAME_EXCL",
            "os.O_NOFOLLOW",
            "terminal_status",
            "decision_evidence_status",
            "exact_amount_available",
            "opaque_subject_id",
            "relationship_coverage_class",
        ):
            self.assertIn(phrase, script)
        for forbidden in (
            "docker build",
            "docker pull",
            "pip install",
            "brew install",
            "sync fund-documents",
        ):
            self.assertNotIn(forbidden, script.casefold())

    def test_phase1_acceptance_runs_offline_public_and_private_projections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            log = temporary_root / "calls.log"
            public_output = temporary_root / "public-output"
            env = {
                "PATH": "/usr/bin:/bin",
                "FAKE_KUNJIN_LOG": str(log),
                "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
            }
            public = subprocess.run(
                [str(acceptance), "000001", "000002", str(public_output)],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(public.returncode, 0, public.stderr.decode())
            summary = json.loads((public_output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["acceptance"], "phase1_public_live")
            self.assertEqual(summary["status"], "passed")
            self.assertEqual(summary["portfolio_fixture"], "synthetic_non_personal")
            self.assertEqual(summary["fund_fact_scope"], "live_public_sources")
            self.assertEqual(summary["useful_partial"]["useful_fact_fields"][:3], [
                "share_class_identity",
                "current_manager_team",
                "formal_nav",
            ])
            useful_partial_brief = json.loads(
                (public_output / "useful-partial-continue_holding.json").read_text(
                    encoding="utf-8"
                )
            )
            formal_nav = next(
                item
                for item in useful_partial_brief["facts"]
                if item["field_id"] == "formal_nav"
            )
            self.assertEqual(formal_nav["source_tier"], "tier_2")
            self.assertEqual(
                useful_partial_brief["subject"]["portfolio_fixture"],
                "synthetic_non_personal",
            )
            projected_files = sorted(path.name for path in public_output.iterdir())
            self.assertEqual(
                projected_files,
                [
                    "summary.json",
                    "unsupported-continue_holding.json",
                    "unsupported-source-status.json",
                    "useful-partial-continue_holding.json",
                    "useful-partial-full_exit.json",
                    "useful-partial-reduce_to_cash.json",
                    "useful-partial-switch_funds.json",
                ],
            )
            rendered = "".join(
                path.read_text(encoding="utf-8") for path in public_output.iterdir()
            )
            for forbidden in (
                "portfolio_weight",
                '"fund_codes"',
                "current_value",
                "shares",
                "cost",
                "profit",
                "PRIVATE_ACCEPTANCE_SENTINEL",
            ):
                self.assertNotIn(forbidden, rendered)
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            action_calls = [call for call in calls if "--action" in call]
            self.assertEqual(
                [call[call.index("--action") + 1] for call in action_calls],
                [
                    "continue_holding",
                    "reduce_to_cash",
                    "full_exit",
                    "switch_funds",
                    "continue_holding",
                ],
            )
            self.assertIn(
                ["--json", "source", "status", "--fund-code", "000002"],
                calls,
            )

            owner_output = temporary_root / "owner-output"
            owner = subprocess.run(
                [str(acceptance), "--owner", str(owner_output)],
                env=env,
                input=b"000001\n",
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(owner.returncode, 0, owner.stderr.decode())
            owner_summary = json.loads(
                (owner_output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertRegex(owner_summary["opaque_subject_id"], r"^[0-9a-f]{32}$")
            self.assertTrue(owner_summary["position_present"])
            self.assertEqual(owner_summary["relationship_coverage_class"], "complete")
            owner_rendered = json.dumps(owner_summary, ensure_ascii=False)
            self.assertNotIn("000001", owner_rendered)
            self.assertNotIn("0.25", owner_rendered)
            self.assertNotIn("portfolio_weight", owner_rendered)

            conservative_output = temporary_root / "owner-conservative-output"
            conservative_env = dict(env)
            conservative_env["FAKE_KUNJIN_SCENARIO"] = "owner_coverage_unknown"
            conservative = subprocess.run(
                [str(acceptance), "--owner", str(conservative_output)],
                env=conservative_env,
                input=b"000001\n",
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(conservative.returncode, 0, conservative.stderr.decode())
            conservative_summary = json.loads(
                (conservative_output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                conservative_summary["relationship_coverage_class"],
                "partial",
            )
            self.assertEqual(
                conservative_summary["decision_evidence_state"],
                "partial",
            )
            self.assertEqual(
                conservative_summary["acceptance_scope"],
                "technical_safety_not_financial_sufficiency",
            )
            self.assertNotIn("000999", json.dumps(conservative_summary))

    def test_phase1_acceptance_fails_closed_for_faults_and_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            log = temporary_root / "calls.log"
            for scenario in (
                "oversized",
                "oversized_stderr",
                "unknown_field",
                "private_sentinel",
                "stale_identity",
                "unsafe_headline",
                "disguised_headline",
                "exact_share_headline",
                "bare_liquidation_headline",
                "half_shares_headline",
                "half_position_headline",
                "chinese_exact_shares_headline",
                "invalid_reduce_state",
                "switch_buy_permission",
                "switch_primary_mismatch",
                "private_metric",
                "wrong_beginner_fact",
                "inconsistent_redemption_terms",
                "inconsistent_labeled_partial",
                "brief_manual_unbound",
                "unbound_supplementation",
                "spliced_supplementation",
                "detached",
                "spawn_on_term",
                "timeout",
            ):
                with self.subTest(scenario=scenario):
                    output = temporary_root / f"output-{scenario}"
                    pid_file = temporary_root / f"{scenario}.pid"
                    spawned_pid_file = temporary_root / f"{scenario}-spawned.pid"
                    env = {
                        "PATH": "/usr/bin:/bin",
                        "FAKE_KUNJIN_LOG": str(log),
                        "FAKE_KUNJIN_PID": str(pid_file),
                        "FAKE_KUNJIN_SCENARIO": scenario,
                        "FAKE_KUNJIN_SPAWNED_PID": str(spawned_pid_file),
                        "KUNJIN_PHASE1_ACCEPTANCE_TIMEOUT_SECONDS": "1",
                        "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
                    }
                    result = subprocess.run(
                        [str(acceptance), "000001", "000002", str(output)],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )
                    self.assertNotEqual(
                        result.returncode,
                        0,
                        f"scenario unexpectedly passed: {scenario}",
                    )
                    self.assertFalse(output.exists())
                    if pid_file.exists():
                        pid = int(pid_file.read_text(encoding="ascii"))
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.01)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(pid, 0)
                    if spawned_pid_file.exists():
                        spawned_pid = int(spawned_pid_file.read_text(encoding="ascii"))
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            try:
                                os.kill(spawned_pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.01)
                        with self.assertRaises(ProcessLookupError):
                            os.kill(spawned_pid, 0)

    def test_phase1_acceptance_accepts_labeled_partial_and_unknown_holdings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            output = temporary_root / "labeled-partial-output"
            result = subprocess.run(
                [str(acceptance), "000001", "000002", str(output)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(temporary_root / "calls.log"),
                    "FAKE_KUNJIN_SCENARIO": "labeled_partial",
                    "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            useful_partial = json.loads(
                (output / "useful-partial-continue_holding.json").read_text(
                    encoding="utf-8"
                )
            )
            identity = next(
                item
                for item in useful_partial["facts"]
                if item["field_id"] == "share_class_identity"
            )
            self.assertEqual(identity["source_tier"], "tier_2")
            self.assertEqual(identity["freshness"], "dated_history")
            self.assertEqual(identity["completeness"], "partial")
            holdings = useful_partial["portfolio_relationship"][
                "disclosed_holdings_coverage"
            ]
            self.assertEqual(holdings["evidence_state"], "insufficient")
            self.assertEqual(holdings["unknown_fields"], ["holdings_industries_000001"])
            self.assertNotIn("included_fund_codes", holdings)
            self.assertNotIn("omitted_fund_codes", holdings)

    def test_phase1_acceptance_accepts_controlled_rapid_partial_supplementation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            output = temporary_root / "partial-supplementation-output"
            result = subprocess.run(
                [str(acceptance), "000001", "000002", str(output)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(temporary_root / "calls.log"),
                    "FAKE_KUNJIN_SCENARIO": "partial_supplementation",
                    "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            unsupported = json.loads(
                (output / "unsupported-source-status.json").read_text(encoding="utf-8")
            )
            resolution = unsupported["request_field_resolutions"][0]
            self.assertEqual(resolution["resolution"], "partial")
            supplementation = unsupported["source_fields"][0]["supplementation"]
            self.assertTrue(supplementation["accepted_input"])
            self.assertTrue(supplementation["suggested_location"])
            self.assertTrue(supplementation["impact_if_missing"])

    def test_phase1_acceptance_interrupt_cleans_ignored_term_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            output = temporary_root / "interrupted-output"
            pid_file = temporary_root / "interrupted.pid"
            process = subprocess.Popen(
                [str(acceptance), "000001", "000002", str(output)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(temporary_root / "interrupt.log"),
                    "FAKE_KUNJIN_PID": str(pid_file),
                    "FAKE_KUNJIN_SCENARIO": "timeout",
                    "KUNJIN_PHASE1_ACCEPTANCE_TIMEOUT_SECONDS": "10",
                    "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            deadline = time.monotonic() + 3
            child_pid_text = ""
            while time.monotonic() < deadline:
                if pid_file.exists():
                    child_pid_text = pid_file.read_text(encoding="ascii")
                    if child_pid_text.isascii() and child_pid_text.isdigit():
                        break
                time.sleep(0.01)
            self.assertTrue(child_pid_text.isascii() and child_pid_text.isdigit())
            child_pid = int(child_pid_text)
            os.killpg(process.pid, signal.SIGINT)
            _stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0, stderr.decode())
            self.assertFalse(output.exists())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_phase1_acceptance_rejects_arguments_conflicts_and_inode_replacement(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "run_phase1_acceptance.sh"
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory)
            existing = temporary_root / "existing"
            existing.mkdir()
            cases = (
                ([], 64),
                (["000001", "000001", str(temporary_root / "same")], 65),
                (["12345", "000002", str(temporary_root / "bad")], 65),
                (["000001", "000002", "relative-output"], 65),
                (["000001", "000002", str(existing)], 66),
            )
            for args, expected in cases:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [str(script), *args],
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected)

            acceptance, _cli = self._install_phase1_acceptance_fixture(temporary_root)
            replacement = temporary_root / "replacement-source"
            replacement.mkdir()
            replacement_inode = replacement.stat().st_ino
            output = temporary_root / "replaced-output"
            result = subprocess.run(
                [str(acceptance), "000001", "000002", str(output)],
                env={
                    "PATH": "/usr/bin:/bin",
                    "FAKE_KUNJIN_LOG": str(temporary_root / "replace.log"),
                    "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION": "synthetic_non_personal",
                    "KUNJIN_PHASE1_TEST_RENAME_HOOK": "replace_after_rename",
                    "KUNJIN_PHASE1_TEST_REPLACEMENT_SOURCE": str(replacement),
                },
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.stat().st_ino, replacement_inode)
            self.assertIn(
                "concurrent publication residue may remain",
                result.stderr.decode(),
            )

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
            "minimum D2 subset",
            "complete D2 and D3 product-selection and pre-purchase checks",
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

    def test_pragmatic_mvp_acceptance_declares_bounded_modes_and_core_cases(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_pragmatic_mvp_acceptance.sh").read_text(
            encoding="utf-8"
        )

        for mode in ("local", "fault", "live", "owner"):
            self.assertIn(mode, script)
        for case in (
            "official_policy",
            "media_reprint",
            "partial_market",
            "malformed_payload",
            "unsafe_redirect",
            "source_timeout",
            "source_cooldown",
            "source_cap_reached",
            "manual_supplement_required",
            "named_fund_public",
            "decision_routing",
            "held_fund_review",
            "thesis_review",
            "portfolio_diagnosis",
            "candidate_gate_abstention",
            "anonymous_owner",
            "no_process_residue",
        ):
            self.assertIn(case, script)
        for command in (
            "news recent --window recent --mode rapid",
            "market overview --window recent --mode rapid",
            "fund intelligence",
        ):
            self.assertIn(command, script)
        for source in ("gov_cn_policy", "stcn_fund_news", "eastmoney_market"):
            self.assertIn(source, script)

        self.assertIn("KUNJIN_PRAGMATIC_LIVE_APPROVED", script)
        self.assertIn("KUNJIN_PRAGMATIC_OWNER_APPROVED", script)
        self.assertIn("explicit_public_read_only", script)
        self.assertIn("explicit_private_read_only", script)
        owner_body = script.split("run_owner() {", 1)[1].split('case "${MODE}"', 1)[0]
        self.assertIn(
            'KUNJIN_PRAGMATIC_LIVE_APPROVED:-}" != "explicit_public_read_only"',
            owner_body,
        )
        self.assertIn("KUNJIN_PRAGMATIC_OWNER_APPROVED", owner_body)
        for owner_command in (
            '["status"]',
            '["portfolio", "analyze"]',
            '["portfolio", "overlap"]',
            '["fund", "brief"',
            '["fund", "intelligence"',
            '["thesis", "review"',
        ):
            self.assertIn(owner_command, owner_body)
        self.assertIn("source.backup(target)", owner_body)
        self.assertIn("public intelligence leaked a forbidden private field", owner_body)
        self.assertIn("never places trades", script)
        self.assertIn('source["outcome"]', script)
        for evidence_gate in (
            "live_source_set_mismatch",
            "live_news_requires_published_items",
            "live_market_requires_eastmoney_evidence",
            "live_fund_subject_mismatch",
            "live_fund_requires_usable_evidence",
            "live_fund_requires_named_context",
            "live_action_boundary_violation",
            "owner_brief_core_sources_incomplete",
        ):
            self.assertIn(evidence_gate, script)
        self.assertIn('or not data["fund_relevance"]["links"]', script)
        self.assertIn(
            "core_brief_evidence_complete = "
            "brief_core_stages.isdisjoint(brief_omitted)",
            owner_body,
        )
        self.assertIn('"financial_action_usability_assessed": False', owner_body)
        self.assertNotIn("financial_usability_passed", owner_body)
        self.assertIn('source["source_tier"]', script)
        self.assertNotIn('source["status"]', script)

    def test_pragmatic_mvp_readme_and_skill_preserve_useful_evidence_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )
        combined = " ".join((readme + "\n" + skill).split())

        for command in (
            "kunjin --json news recent --window recent --mode rapid",
            "kunjin --json market overview --window recent --mode rapid",
            "kunjin --json fund intelligence 519755 --window recent --mode rapid",
        ):
            self.assertIn(command, combined)
        for phrase in (
            "source tier",
            "publication date",
            "fact",
            "reasoned_inference",
            "lineage",
            "reprint",
            "partial",
            "cooldown",
            "manual supplementation",
            "market_session=unknown",
            "direction=insufficient_data",
            "disclosed_context",
            "possible_invalidation_match",
            "no_matching_evidence",
            "manual semantic review",
            "action_maturity=evidence_only",
            "action_authorized=false",
            "exact_amount_available=false",
            "external_context",
            "cannot strengthen KunJin's persisted evidence state",
            "complete D2",
            "D3 exact amount",
            "mature Phase E",
            "broad official adapters",
        ):
            self.assertIn(phrase, combined)

        for route in (
            "latest news",
            "market context or a direction-to-buy question",
            "named candidate",
            "held-fund daily review",
            "portfolio diagnosis",
        ):
            self.assertIn(route, skill)
        for existing_command in (
            "fund brief",
            "fund profile",
            "fund fees",
            "fund research",
            "portfolio analyze",
            "portfolio overlap",
        ):
            self.assertIn(existing_command, skill)

        self.assertIn("$kunjin-fund", agent)
        self.assertIn("news, market, named-fund, portfolio, and daily-review", agent)
        self.assertNotIn("Do not persist news in KunJin until", skill)
        self.assertNotIn("automated news ingestion are not implemented", skill)

    def test_phase3_acceptance_declares_bounded_modes_privacy_and_action_boundary(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase3_acceptance.sh").read_text(
            encoding="utf-8"
        )

        for mode in ("local", "fault", "owner"):
            self.assertIn(mode, script)
        for case in (
            "complete_relationships",
            "partial_holdings",
            "missing_nav",
            "benchmark_text_limitation",
            "candidate_duplication",
            "candidate_insufficient_data",
            "privacy_scan",
            "no_process_residue",
        ):
            self.assertIn(case, script)

        self.assertIn("KUNJIN_PHASE3_OWNER_APPROVED", script)
        self.assertIn("explicit_private_read_only", script)
        self.assertIn('mode=ro', script)
        self.assertIn("source.backup(target)", script)
        self.assertIn('[cli, "--json", "portfolio", "diagnose"]', script)
        self.assertIn('"action_maturity"', script)
        self.assertIn('"evidence_only"', script)
        self.assertIn('"action_authorized"', script)
        self.assertIn('"exact_amount_available"', script)
        self.assertIn("owner fund code leaked", script)
        self.assertIn("owner private key leaked", script)
        self.assertIn("never_places_trades", script)

    def test_phase3_readme_and_skill_route_portfolio_diagnosis_without_authorization(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        combined = " ".join((readme + "\n" + skill).split())

        for command in (
            "kunjin --json portfolio diagnose",
            "kunjin --json portfolio diagnose --candidate 519755",
        ):
            self.assertIn(command, combined)
        self.assertIn(
            "status -> sync portfolio -> portfolio diagnose",
            combined,
        )
        for phrase in (
            "one user-supplied candidate",
            "complete D2",
            "D3",
            "buy or add",
            "hold, reduce, or exit",
            "exact amount",
            "action_maturity=evidence_only",
            "action_authorized=false",
            "exact_amount_available=false",
        ):
            self.assertIn(phrase, combined)
        self.assertLessEqual(len(skill.splitlines()), 500)

    def test_phase4_acceptance_declares_private_bounded_shortlist_modes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase4_acceptance.sh").read_text(
            encoding="utf-8"
        )

        for mode in ("local", "fault", "owner"):
            self.assertIn(mode, script)
        for case in (
            "two_candidate_tradeoffs",
            "five_candidate_boundary",
            "conditional_shortlist",
            "not_comparable",
            "partial_candidate_isolation",
            "held_candidate_amount_boundary",
            "cash_like_not_protected_cash",
            "privacy_scan",
            "no_network_dependency",
            "no_process_residue",
        ):
            self.assertIn(case, script)

        self.assertIn("KUNJIN_PHASE4_OWNER_APPROVED", script)
        self.assertIn("explicit_private_read_only", script)
        self.assertIn("mode=ro", script)
        self.assertIn("source.backup(target)", script)
        self.assertIn('cli, "--json", "fund", "shortlist"', script)
        self.assertIn("owner_candidates_unavailable", script)
        self.assertIn('"action_maturity": "evidence_only"', script)
        self.assertIn('"action_authorized": False', script)
        self.assertIn('"exact_amount_available": False', script)
        self.assertIn('"automatic_trade": False', script)
        self.assertIn("owner fund code leaked", script)
        self.assertIn("owner private key leaked", script)
        self.assertIn("owner private path leaked", script)
        self.assertIn('"shortlist_ran_on_private_copy": True', script)
        self.assertIn("never_places_trades", script)

    def test_phase41_acceptance_declares_private_finite_modes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase41_acceptance.sh").read_text(
            encoding="utf-8"
        )
        helper = (root / "scripts/phase41_acceptance.py").read_text(encoding="utf-8")
        combined = script + "\n" + helper

        for mode in ("local", "fault", "engineering", "owner"):
            self.assertIn(mode, script)
        for phrase in (
            "KUNJIN_PHASE41_OWNER_APPROVED",
            "explicit_private_keychain_read_only",
            "KUNJIN_PHASE41_ENGINEERING_SUBJECTS_FILE",
            "MAX_PUBLIC_SOURCE_STATUS_CALLS = 5",
            "MAX_PUBLIC_ACTION_CALLS = 25",
            "fund.shortlist-readiness",
            "source.status",
            "sync fund-profile",
            "sync fund-holdings",
            "sync fund-documents",
            "fund classify",
            "owner_candidates_unavailable",
            "not_yet_testable",
            "research_scope_only",
            "not_implemented",
            "stopped_by_source_state",
            "dependency_stopped",
        ):
            self.assertIn(phrase, combined)
        self.assertNotIn("<<'PY'", script)
        self.assertLessEqual(len(script.splitlines()), 260)
        self.assertIn("run_tracked", script)
        self.assertIn("emit_scanned", script)
        self.assertIn("check_private_residue", script)
        self.assertNotIn("pgrep", script)
        self.assertIn('/bin/kill -0 "-${pgid}"', script)
        self.assertIn("unset PYTHONHOME PYTHONPATH", script)
        self.assertIn("phase41_private_stderr", script)
        self.assertIn("tests/unit/test_selection_service.py", script)
        self.assertIn("tests/unit/test_selection_research.py", script)

    def test_phase5_acceptance_declares_hardened_private_modes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase5_acceptance.sh").read_text(
            encoding="utf-8"
        )
        helper = (root / "scripts/phase5_acceptance.py").read_text(encoding="utf-8")
        combined = script + "\n" + helper

        self.assertIn("local|fault|engineering|owner", script)
        self.assertIn("KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE", combined)
        self.assertIn("KUNJIN_PHASE5_OWNER_SUBJECT_FILE", combined)
        self.assertIn("explicit_private_read_only_review", combined)
        self.assertIn("secure_read_private_subject", helper)
        self.assertIn("ReadOnlyDatabaseGuard", helper)
        self.assertIn("load_owner_key_once", helper)
        self.assertIn("thesis_evidence_adjudications", helper)
        self.assertIn("NoExternalOperations", helper)
        self.assertIn("latest local snapshot", helper)
        self.assertIn('readonly PATH="/usr/bin:/bin"', script)
        self.assertIn("unset PYTHONHOME PYTHONPATH", script)
        self.assertIn("umask 077", script)
        self.assertIn("mktemp -d /private/tmp/kunjin-phase5-acceptance", script)
        self.assertIn("run_tracked", script)
        self.assertIn("kill_process_group", script)
        self.assertIn("sanitize_encoded_output", helper)
        self.assertIn('"${PYTHON}" "${HELPER}" produce "${MODE}"', script)
        self.assertIn('"${PYTHON}" "${HELPER}" validate "${MODE}"', script)
        self.assertIn("MAX_CAPTURE_BYTES", script)
        for phrase in (
            '"official_negative_check_complete": False',
            '"action_authorized": False',
            '"exact_amount_available": False',
            '"automatic_trade": False',
            '"sell_timing": "insufficient_data"',
            '"network_retries": 0',
        ):
            self.assertIn(phrase, combined)

    def test_phase5_acceptance_declares_complete_fault_inventory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        helper = (root / "scripts/phase5_acceptance.py").read_text(encoding="utf-8")

        for fault in (
            "fund_binding_mismatch",
            "snapshot_corruption",
            "brief_snapshot_missing",
            "intelligence_snapshot_missing",
            "thesis_missing",
            "official_confirmation_missing",
            "redemption_evidence_missing",
            "tier_two_only",
            "same_lineage_reprint",
            "source_failed",
            "coverage_reduced",
            "stale_adjudication",
            "history_corruption",
            "repeated_request",
            "privacy_shape",
            "interrupt_cleanup",
            "unexpected_exit",
        ):
            self.assertIn(fault, helper)

    def test_phase5_acceptance_rejects_unapproved_private_modes_before_python(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            scripts = repository / "scripts"
            venv = repository / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv.mkdir(parents=True)
            wrapper = scripts / "run_phase5_acceptance.sh"
            shutil.copy2(root / "scripts/run_phase5_acceptance.sh", wrapper)
            shutil.copy2(root / "scripts/phase5_acceptance.py", scripts)
            wrapper.chmod(0o755)
            marker = repository / "python-started"
            fake_python = venv / "python"
            fake_python.write_text(
                "#!/bin/bash\nprintf started > \"${PHASE5_MARKER}\"\nexit 70\n",
                encoding="ascii",
            )
            fake_python.chmod(0o755)
            env = {**os.environ, "PHASE5_MARKER": str(marker)}

            for mode in ("engineering", "owner"):
                completed = subprocess.run(
                    [str(wrapper), mode],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=5,
                )
                self.assertEqual(completed.returncode, 77)
                self.assertFalse(marker.exists())
            completed = subprocess.run(
                [str(wrapper), "deep"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn(b"local|fault|engineering|owner", completed.stderr)

    def test_phase5_acceptance_clears_private_environment_before_validation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            scripts = repository / "scripts"
            venv = repository / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv.mkdir(parents=True)
            wrapper = scripts / "run_phase5_acceptance.sh"
            shutil.copy2(root / "scripts/run_phase5_acceptance.sh", wrapper)
            shutil.copy2(root / "scripts/phase5_acceptance.py", scripts)
            wrapper.chmod(0o755)
            marker = repository / "validated-clean"
            summary = json.dumps(
                {
                    "action_authorized": False,
                    "automatic_trade": False,
                    "conditional_review_usability": "partial",
                    "counts": {
                        "brief_calls": 1,
                        "intelligence_calls": 1,
                        "match_projection_calls": 1,
                        "adjudication_calls": 0,
                        "holding_review_calls": 1,
                        "network_retries": 0,
                    },
                    "engineering_flow": "pass",
                    "evidence_readiness": "partial",
                    "exact_amount_available": False,
                    "history_comparability": "not_available",
                    "mode": "engineering",
                    "redemption_feasibility": "not_requested",
                    "sell_timing": "insufficient_data",
                    "thesis_review_readiness": "ready",
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            fake_python = venv / "python"
            fake_python.write_text(
                "#!/bin/bash\n"
                "if [[ \"$2\" == produce ]]; then\n"
                "  [[ -n \"${KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE:-}\" ]] || exit 21\n"
                f"  printf '%s\\n' '{summary}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$2\" == validate ]]; then\n"
                "  [[ -z \"${KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE:-}\" ]] || exit 22\n"
                "  [[ -z \"${KUNJIN_PHASE5_OWNER_SUBJECT_FILE:-}\" ]] || exit 22\n"
                "  [[ -z \"${KUNJIN_PHASE5_OWNER_APPROVED:-}\" ]] || exit 22\n"
                "  printf clean > \"${PHASE5_MARKER}\"\n"
                "  /bin/cat \"$4\"\n"
                "  exit 0\n"
                "fi\n"
                "exit 23\n",
                encoding="ascii",
            )
            fake_python.chmod(0o755)
            env = {
                **os.environ,
                "KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE": str(
                    Path(temporary) / "subject.json"
                ),
                "PHASE5_MARKER": str(marker),
            }
            env.pop("KUNJIN_DATA_DIR", None)
            env.pop("KUNJIN_STATE_DIR", None)

            completed = subprocess.run(
                [str(wrapper), "engineering"],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(marker.read_text(encoding="ascii"), "clean")

    def test_phase5_acceptance_rejects_unexpected_child_exit(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            scripts = repository / "scripts"
            venv = repository / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv.mkdir(parents=True)
            wrapper = scripts / "run_phase5_acceptance.sh"
            shutil.copy2(root / "scripts/run_phase5_acceptance.sh", wrapper)
            shutil.copy2(root / "scripts/phase5_acceptance.py", scripts)
            wrapper.chmod(0o755)
            fake_python = venv / "python"
            fake_python.write_text("#!/bin/bash\nexit 23\n", encoding="ascii")
            fake_python.chmod(0o755)

            completed = subprocess.run(
                [str(wrapper), "fault"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=5,
            )

            self.assertEqual(completed.returncode, 70)
            self.assertEqual(completed.stdout, b"")
            self.assertIn(b"phase5_acceptance_tests_failed", completed.stderr)

    def test_phase5_acceptance_interrupt_cleans_process_group(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "repository"
            scripts = repository / "scripts"
            venv = repository / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv.mkdir(parents=True)
            wrapper = scripts / "run_phase5_acceptance.sh"
            shutil.copy2(root / "scripts/run_phase5_acceptance.sh", wrapper)
            shutil.copy2(root / "scripts/phase5_acceptance.py", scripts)
            wrapper.chmod(0o755)
            marker = repository / "children.txt"
            fake_python = venv / "python"
            fake_python.write_text(
                "#!/bin/bash\n"
                "trap '' TERM INT HUP\n"
                "printf '%s\\n' \"$$\" > \"${PHASE5_SIGNAL_MARKER}\"\n"
                "/bin/bash -c 'trap \"\" TERM INT HUP; printf \"%s\\n\" \"$$\" >> "
                "\"${PHASE5_SIGNAL_MARKER}\"; while :; do /bin/sleep 1; done' &\n"
                "while :; do /bin/sleep 1; done\n",
                encoding="ascii",
            )
            fake_python.chmod(0o755)
            env = {**os.environ, "PHASE5_SIGNAL_MARKER": str(marker)}
            process = subprocess.Popen(
                [str(wrapper), "fault"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130)
            child_pids = [int(value) for value in marker.read_text().splitlines()]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if all(not self._process_exists(pid) for pid in child_pids):
                    break
                time.sleep(0.02)
            self.assertTrue(all(not self._process_exists(pid) for pid in child_pids))

    @staticmethod
    def _process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_phase41_acceptance_owner_and_private_file_contract_fail_closed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase41_acceptance.sh").read_text(
            encoding="utf-8"
        )
        helper = (root / "scripts/phase41_acceptance.py").read_text(encoding="utf-8")

        for phrase in (
            'pwd.getpwuid(os.getuid()).pw_dir',
            '_canonical_home() / ".local" / "share" / "kunjin" / "kunjin.db"',
            "_validate_cli_origin",
            '"?mode=ro"',
            "self.connection.backup(target)",
            "O_NOFOLLOW",
            "assert_same_inode",
            "load_owner_key_once",
            "InMemoryKeyStore",
            "OwnerRuntimeGuards",
            "single_context",
            "single_keychain_child",
            "validate_owner_statuses",
            "validate_engineering_flow",
            "project_engineering_evidence",
            "financial_interpretation",
            "prohibited",
        ):
            self.assertIn(phrase, helper)
        self.assertIn("KUNJIN_DATA_DIR", script)
        self.assertIn("owner_runtime_override_prohibited", script)
        self.assertIn(
            'readonly OWNER_ENTRYPOINT="/Users/yanzihao/KunJin/scripts/run_phase41_acceptance.sh"',
            script,
        )
        self.assertIn('readonly OWNER_REPOSITORY_ROOT="/Users/yanzihao/KunJin"', script)
        self.assertIn("owner_entrypoint_invalid", script)
        self.assertNotIn('/bin/cat "${private_error}"', script)
        self.assertNotIn('/bin/cat "${private_output}"', script)

    def test_phase41_committed_contract_never_associates_private_subject_codes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts/run_phase41_acceptance.sh").read_text(
            encoding="utf-8"
        )
        helper = (root / "scripts/phase41_acceptance.py").read_text(encoding="utf-8")
        six_digit_literals = set(
            __import__("re").findall(
                r'(?<![0-9])[0-9]{6}(?![0-9])', script + "\n" + helper
            )
        )

        self.assertEqual(six_digit_literals, set())
        self.assertNotIn("ENGINEERING_SUBJECT_CODES", script + helper)
        self.assertNotIn("engineering_subject_codes", script + helper)

    def test_phase41_readme_and_skill_route_bounded_owner_readiness(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized_readme = " ".join(readme.split())
        normalized_skill = " ".join(skill.split())

        for document in (normalized_readme, normalized_skill):
            for phrase in (
                "kunjin --json fund research-scope",
                "--objective learning --horizon long_term --product-category broad_index",
                "kunjin --json fund shortlist-readiness 000001 000002",
                "candidate_formation.status=research_scope_only",
                "candidate_formation.candidate_code_discovery=not_implemented",
                "owner_candidate_state=owner_candidates_unavailable",
                "financial_usability=not_yet_testable",
                "action_maturity=evidence_only",
                "action_authorized=false",
                "exact_amount_available=false",
                "automatic_trade=false",
                "Phase 4.1 adds neither market direction nor candidate-code discovery",
                "readiness is a local snapshot, not a refresh engine or recommendation",
                "engineering_flow=pass",
                "evidence_readiness=ready|partial|insufficient_data",
                "comparison_evidence_readiness=ready|insufficient_data",
                "structural_comparability=observed|not_testable",
                "does not mean comparable, diversified, safe, or recommended",
            ):
                self.assertIn(phrase, document)

        for phrase in (
            "initial `fund shortlist-readiness` exactly once",
            "`source status --fund-code CODE` exactly once per code",
            "only actions returned by the initial readiness result",
            "each action at most once per code",
            "dependency order",
            "final `fund shortlist-readiness` exactly once",
            (
                "Never add `--force`, automatically retry, continue in the background, "
                "or develop an adapter during the request"
            ),
            "Use aggregate `request_field_resolutions` as authoritative",
            (
                "With `resolution=usable`, continue the single planned action even when "
                "the primary or an unused alternative is terminal"
            ),
            "`resolution=manual_supplement_required` stops the affected field",
            (
                "`resolution=partial` stops the affected field only when its corresponding "
                "primary is `cooldown`, `unavailable`, or `unsupported`"
            ),
            "A terminal command failure stops only dependent actions",
            "Each legacy command keeps its own independent runtime boundary",
            "`sync fund` and `sync fund-documents` are outside the Phase 0 90/480-second budget",
            "Return the final result as partial when any gap remains",
        ):
            self.assertIn(phrase, normalized_skill)
        self.assertNotIn(
            "For `cooldown`, `unavailable`, `unsupported`, or "
            "`manual_supplement_required`, stop the affected field",
            normalized_skill,
        )
        self.assertLessEqual(len(skill.splitlines()), 500)

    def test_phase4_readme_and_skill_route_exact_unordered_candidates(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        combined = " ".join((readme + "\n" + skill).split())

        self.assertIn("kunjin --json fund shortlist 000001 000002", combined)
        for phrase in (
            "exactly 2-5 owner-supplied codes",
            "resolve names to one unique confirmed code first",
            "unordered",
            "not a buy signal",
            "amount-free",
            "action_maturity=evidence_only",
            "action_authorized=false",
            "exact_amount_available=false",
            "automatic_trade=false",
            "outside the shortlist command",
            "Never develop a source adapter during the query",
        ):
            self.assertIn(phrase, combined)
        self.assertLess(len(skill.splitlines()), 500)

    def test_phase5_readme_and_skill_route_authenticated_preview_once(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(
            encoding="utf-8"
        )
        normalized_documents = tuple(
            " ".join(document.split()) for document in (readme, skill)
        )

        required_phrases = (
            "one owner-selected held fund",
            "fund brief exactly once",
            "fund intelligence exactly once",
            "thesis match-project exactly once",
            "thesis adjudicate at most once",
            "fund holding-review exactly once",
            "local and network-free",
            "Choose exactly one brief mode for one held-fund workflow",
            "Default to Rapid",
            "Never run both Rapid and Deep briefs in the same workflow",
            "Rapid performs ordinary context and title-level candidate discovery only",
            "Run explicit Deep only when the owner requests same-fund official-body confirmation",
            "kunjin --json fund brief CODE --action ACTION --mode deep",
            "fund liquidation",
            "fund termination",
            "redemption restriction",
            "manager change",
            "fee change",
            "benchmark change",
            "authenticated official negative-check closure",
            "registered manager official sources",
            "bounded window",
            "pagination terminal state",
            "authenticated closure",
            "official_negative_check_complete=true",
            "official_negative_check_complete=false",
            "official_confirmation_required",
            "does not mean no major risk and does not mean zero candidates",
            "本次有界官方检查未发现需要升级复核的候选；这不能排除其他重大风险。",
            "Never fall back to Tier 2",
            "Any source, window, binding, body, conflict, truncation, or cap gap forces",
            (
                "review_disposition=continue_observing|reduce_review|exit_review "
                "only when its evidence contract is complete"
            ),
            "sell_timing=insufficient_data",
            "action_authorized=false",
            "exact_amount_available=false",
            "automatic_trade=false",
            "Each command keeps its own independent budget",
            "A Rapid brief owns 90 seconds",
            "an explicit Deep brief owns 480 seconds",
            "`fund intelligence` owns its own Rapid 90-second budget",
            (
                "`match-project`, optional `adjudicate`, and `holding-review` "
                "are local and share no network budget"
            ),
            "Never retry automatically",
            "Never continue in the background",
            "Never develop an adapter during the request",
            "Never run Deep automatically",
            "An acceptance token is not owner adjudication",
            "projection-specific owner decision",
            (
                "exact intelligence request ID -> thesis match-project -> exact projection "
                "owner confirmation -> optional adjudicate -> holding-review with exact brief "
                "and intelligence request IDs"
            ),
            "Stop after the review and present every gap",
            "事实、分析、条件建议、风险、失效条件、证据缺口",
            "Chinese conclusion by default and hides internal codes",
            "不承诺收益",
            "不提供万能赢家",
            "不声称未经验证的命中率或帮助率",
        )
        for document in normalized_documents:
            for phrase in required_phrases:
                self.assertIn(phrase, document)

        for command in (
            "kunjin --json fund brief CODE --action ACTION --mode rapid",
            "kunjin --json fund intelligence CODE --window recent --mode rapid",
            (
                "kunjin --json thesis match-project CODE "
                "--intelligence-request-run-id INTELLIGENCE_REQUEST_RUN_ID"
            ),
            (
                "kunjin --json thesis adjudicate CODE "
                "--thesis-match-projection-id PROJECTION_ID --decision DECISION"
            ),
            (
                "kunjin --json fund holding-review CODE --action ACTION "
                "--brief-request-run-id BRIEF_REQUEST_RUN_ID "
                "--intelligence-request-run-id INTELLIGENCE_REQUEST_RUN_ID"
            ),
        ):
            self.assertIn(command, skill)

        normalized_skill = normalized_documents[1]
        self.assertIn("only after the owner explicitly confirms", normalized_skill)
        for required in (
            "suitability status exactly once before fund brief",
            "suitability assess exactly once",
            "Do not rerun suitability status",
            "reduce_to_cash and full_exit skip this Phase B preflight",
            (
                "If suitability status or assessment fails, do not retry; "
                "continue to the single brief"
            ),
        ):
            self.assertIn(required, normalized_skill)
        for unsupported_claim in (
            "未发现公告就表示没有重大风险",
            "provides 90% beginner help",
            "guarantees 90% beginner help",
            "guaranteed return",
            "universal winner",
            "60%-80% hit rate",
            "then `fund intelligence` and `thesis review`",
            "Deep official confirmation is deferred",
            "This bounded official check found no candidate",
        ):
            for document in normalized_documents:
                self.assertNotIn(unsupported_claim, document)
        self.assertLess(len(skill.splitlines()), 500)


if __name__ == "__main__":
    unittest.main()
