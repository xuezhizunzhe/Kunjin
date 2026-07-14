from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Optional, Tuple
from unittest.mock import Mock, patch

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.documents import OfficialDocumentCandidate, RetrievedArtifact
from kunjin.funds.risk.failures import DocumentFailureReason, DocumentFailureStage
from kunjin.funds.risk.legacy_doc import (
    CommandResult,
    DockerLegacyDocConverter,
    LegacyDocConversionError,
    SubprocessDockerCommandRunner,
)
from kunjin.paths import RuntimePaths

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
IMAGE_ID = "sha256:" + "a" * 64
PACKAGE_CHECKSUM = "b" * 64
BASE_DIGEST = "sha256:" + "c" * 64
LIBREOFFICE_VERSION = "4:7.4.7-1+deb12u14"


class FakeRunner:
    def __init__(self) -> None:
        self.run_calls: list[Tuple[str, ...]] = []
        self.query_calls: list[Tuple[str, ...]] = []
        self.run_results: Deque[CommandResult] = deque()
        self.on_run: Optional[Callable[[Tuple[str, ...]], None]] = None
        self.on_query: Optional[
            Callable[[Tuple[str, ...]], Optional[Tuple[bytes, CommandResult]]]
        ] = None
        self.stale_containers: Tuple[Tuple[str, str], ...] = ()
        self.container_states: dict[str, Tuple[str, str, bool, int]] = {}
        self.image_output = (
            f"{IMAGE_ID}\tlinux\tarm64\tdocker-libreoffice-v1\t{BASE_DIGEST}\t"
            f"{LIBREOFFICE_VERSION}\t{PACKAGE_CHECKSUM}\n"
        ).encode("ascii")

    def run(self, argv: Tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        del timeout_seconds
        self.run_calls.append(argv)
        if self.on_run is not None:
            self.on_run(argv)
        if self.run_results:
            return self.run_results.popleft()
        if len(argv) >= 3 and argv[1:3] == ("container", "inspect"):
            return CommandResult(return_code=1)
        return CommandResult(return_code=0)

    def query(
        self,
        argv: Tuple[str, ...],
        *,
        timeout_seconds: int,
        output_path: Path,
        max_output_bytes: int,
    ) -> CommandResult:
        del timeout_seconds
        self.query_calls.append(argv)
        if self.on_query is not None:
            override = self.on_query(argv)
            if override is not None:
                payload, result = override
                if result.return_code == 0:
                    output_path.write_bytes(payload)
                    output_path.chmod(0o600)
                return result
        if argv[1:3] == ("container", "ls") and any(
            token == "label=com.kunjin.legacy-doc=1" for token in argv
        ):
            payload = "".join(
                f"{identifier}\t{state}\n" for identifier, state in self.stale_containers
            ).encode("ascii")
        elif argv[1:3] == ("container", "ls"):
            payload = b""
        elif argv[1:3] == ("container", "inspect"):
            target = argv[3]
            state = self.container_states.get(target)
            if state is None:
                return CommandResult(return_code=1)
            identifier, name, oom_killed, exit_code = state
            payload = (
                f"{identifier}\t/{name}\texited\t{str(oom_killed).lower()}\t{exit_code}\n"
            ).encode("ascii")
        else:
            payload = self.image_output
        if len(payload) > max_output_bytes:
            return CommandResult(return_code=None, output_limit_exceeded=True)
        output_path.write_bytes(payload)
        output_path.chmod(0o600)
        return CommandResult(return_code=0)


class LegacyDocConverterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.paths = RuntimePaths(
            database=self.root / "data" / "kunjin.db",
            snapshots=self.root / "data" / "snapshots",
            logs=self.root / "state" / "logs",
        ).ensure()
        self.runner = FakeRunner()
        self.source = self.root / "managed.doc"
        self.source.write_bytes(b"authenticated legacy document")
        self.artifact = self._artifact()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _artifact(self) -> RetrievedArtifact:
        body = self.source.read_bytes()
        return RetrievedArtifact(
            candidate=OfficialDocumentCandidate(
                fund_code="519755",
                document_kind=DocumentKind.QUARTERLY_REPORT,
                title="example fund quarterly report",
                url="https://www.fund001.com/report.doc",
                publisher="example fund manager",
                published_at=NOW,
                source_tier=1,
            ),
            final_url="https://www.fund001.com/report.doc",
            retrieved_at=NOW,
            content_type="application/msword",
            byte_size=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            managed_path=self.source,
        )

    def _converter(self, **changes: object) -> DockerLegacyDocConverter:
        values = {
            "image_id": IMAGE_ID,
            "libreoffice_version": LIBREOFFICE_VERSION,
            "package_manifest_checksum": PACKAGE_CHECKSUM,
            "runtime_paths": self.paths,
            "runner": self.runner,
        }
        values.update(changes)
        converter = DockerLegacyDocConverter(**values)
        converter._docker_cli_is_trusted = Mock(return_value=True)
        return converter

    @staticmethod
    def _mount_source(argv: Tuple[str, ...], destination: str) -> Path:
        for index, token in enumerate(argv):
            if token == "--mount":
                specification = argv[index + 1]
                fields = dict(
                    item.split("=", 1) for item in specification.split(",") if "=" in item
                )
                if fields.get("dst") == destination:
                    return Path(fields["src"])
        raise AssertionError(f"missing mount for {destination}")

    def _successful_run(self, html: bytes) -> Callable[[Tuple[str, ...]], None]:
        def create_output(argv: Tuple[str, ...]) -> None:
            if len(argv) < 2 or argv[1] != "run":
                return
            output_dir = self._mount_source(argv, "/output")
            output_dir.joinpath("input.html").write_bytes(html)
            cidfile = Path(argv[argv.index("--cidfile") + 1])
            cidfile.write_text("d" * 64 + "\n", encoding="ascii")

        return create_output

    def test_status_requires_exact_local_sha256_image_id_and_matching_inspect(self) -> None:
        ready = self._converter().status()
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.reason_code, None)
        self.assertEqual(ready.parser_version, "2-docker-libreoffice-v1")
        self.assertEqual(len(ready.provenance_checksum or ""), 64)
        self.assertIn(
            (str(DockerLegacyDocConverter.DOCKER_PATH), "image", "inspect", IMAGE_ID),
            [call[:4] for call in self.runner.query_calls],
        )

        for invalid in (None, "legacy:latest", "sha256:" + "A" * 64):
            with self.subTest(image_id=invalid):
                status = self._converter(image_id=invalid).status()
                self.assertNotEqual(status.status, "ready")
                self.assertEqual(status.reason_code, "legacy_converter_unavailable")

        self.runner.image_output = self.runner.image_output.replace(b"arm64", b"amd64")
        mismatch = self._converter().status()
        self.assertEqual(mismatch.status, "invalid")

        for uid, gid in ((0, os.getgid()), (os.getuid(), -1), (True, os.getgid())):
            with (
                self.subTest(uid=uid, gid=gid),
                patch("kunjin.funds.risk.legacy_doc.os.getuid", return_value=uid),
                patch("kunjin.funds.risk.legacy_doc.os.getgid", return_value=gid),
            ):
                self.assertEqual(self._converter().status().status, "unavailable")

    def test_image_id_only_derives_and_caches_verified_label_provenance(self) -> None:
        converter = self._converter(
            libreoffice_version=None,
            package_manifest_checksum=None,
        )

        status = converter.status()
        provenance = converter.active_provenance()
        image_inspections = [
            call for call in self.runner.query_calls if call[1:3] == ("image", "inspect")
        ]

        self.assertEqual(status.status, "ready")
        self.assertIsNotNone(provenance)
        self.assertEqual(provenance.parser_version, "2-docker-libreoffice-v1")
        self.assertEqual(provenance.provenance_checksum, status.provenance_checksum)
        self.assertIn(LIBREOFFICE_VERSION, provenance.canonical_json)
        self.assertIn(PACKAGE_CHECKSUM, provenance.canonical_json)
        self.assertEqual(len(image_inspections), 1)

    def test_explicit_test_metadata_must_match_verified_image_labels(self) -> None:
        mismatches = (
            {"libreoffice_version": "25.2.3.3"},
            {"package_manifest_checksum": "c" * 64},
            {"libreoffice_version": None},
        )
        for changes in mismatches:
            with self.subTest(changes=changes):
                self.assertEqual(self._converter(**changes).status().status, "invalid")

    def test_unset_or_invalid_image_id_never_queries_docker(self) -> None:
        for image_id in (None, "legacy:latest", "sha256:" + "A" * 64):
            with self.subTest(image_id=image_id):
                runner = FakeRunner()
                converter = DockerLegacyDocConverter(
                    image_id=image_id,
                    libreoffice_version=None,
                    package_manifest_checksum=None,
                    runtime_paths=self.paths,
                    runner=runner,
                )
                converter._docker_cli_is_trusted = Mock(return_value=True)
                status = converter.status()
                self.assertNotEqual(status.status, "ready")
                self.assertEqual(runner.query_calls, [])
                self.assertEqual(runner.run_calls, [])

    def test_run_uses_every_required_isolation_flag_and_no_shell(self) -> None:
        self.runner.on_run = self._successful_run(b"<html><body>valid</body></html>")
        self._converter().convert(self.artifact)
        argv = next(call for call in self.runner.run_calls if call[1] == "run")
        required = (
            "--pull=never",
            "--label",
            "com.kunjin.legacy-doc=1",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--ipc=none",
            "--pids-limit=64",
            "--memory=768m",
            "--memory-swap=768m",
            "--cpus=1.0",
            "--init",
            f"--user={os.getuid()}:{os.getgid()}",
            "--log-driver=none",
            "--tmpfs",
            (
                "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,"
                f"uid={os.getuid()},gid={os.getgid()}"
            ),
            "--workdir=/tmp",
            IMAGE_ID,
            "/usr/bin/libreoffice",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--norestore",
            "--safe-mode",
            "-env:UserInstallation=file:///tmp/lo-profile",
            "--convert-to",
            "html:HTML (StarWriter):SkipImages",
            "--outdir",
            "/output",
            "/input/input.doc",
        )
        for token in required:
            self.assertIn(token, argv)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("--device", argv)
        self.assertFalse(any("docker.sock" in token for token in argv))

    def test_input_is_private_copy_with_original_sha256(self) -> None:
        observed: dict[str, object] = {}

        def inspect_copy(argv: Tuple[str, ...]) -> None:
            if argv[1] != "run":
                return
            input_dir = self._mount_source(argv, "/input")
            copied = input_dir / "input.doc"
            observed["same_path"] = copied == self.source
            observed["mode"] = stat.S_IMODE(copied.stat().st_mode)
            observed["sha256"] = hashlib.sha256(copied.read_bytes()).hexdigest()
            self._successful_run(b"<html><body>valid</body></html>")(argv)

        self.runner.on_run = inspect_copy
        self._converter().convert(self.artifact)
        self.assertFalse(observed["same_path"])
        self.assertEqual(observed["mode"], 0o600)
        self.assertEqual(observed["sha256"], self.artifact.sha256)

    def test_stdout_stderr_and_stdin_are_never_captured_or_inherited(self) -> None:
        process = Mock()
        process.wait.return_value = 0
        process.returncode = 0
        with patch("kunjin.funds.risk.legacy_doc.subprocess.Popen", return_value=process) as popen:
            SubprocessDockerCommandRunner().run(
                ("/usr/local/bin/docker", "version"), timeout_seconds=1
            )
        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertFalse(kwargs["shell"])
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["restore_signals"])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(
            kwargs["env"],
            {"HOME": "/var/empty", "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )

    def test_timeout_kills_client_then_force_removes_cid_and_verifies_absence(self) -> None:
        self.runner.on_run = self._successful_run(b"<html><body>discarded</body></html>")
        self.runner.run_results.append(CommandResult(return_code=None, timed_out=True))
        with self.assertRaises(LegacyDocConversionError) as caught:
            self._converter().convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code, DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
        )
        self.assertTrue(
            any(
                call[1:3] == ("container", "rm") and "d" * 64 in call
                for call in self.runner.run_calls
            )
        )
        self.assertTrue(
            any(call[1:3] == ("container", "inspect") for call in self.runner.run_calls)
        )

        process = Mock(pid=4321)
        process.wait.side_effect = (
            subprocess.TimeoutExpired(cmd="docker", timeout=1),
            subprocess.TimeoutExpired(cmd="docker", timeout=2),
            0,
        )
        with (
            patch("kunjin.funds.risk.legacy_doc.subprocess.Popen", return_value=process),
            patch("kunjin.funds.risk.legacy_doc.os.killpg") as killpg,
        ):
            result = SubprocessDockerCommandRunner().run(
                ("/usr/local/bin/docker", "version"), timeout_seconds=1
            )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.termination_failed)
        self.assertEqual(
            killpg.call_args_list,
            [unittest.mock.call(4321, signal.SIGTERM), unittest.mock.call(4321, signal.SIGKILL)],
        )

    def test_name_fallback_cleans_container_when_cidfile_is_missing(self) -> None:
        def output_without_cid(argv: Tuple[str, ...]) -> None:
            if argv[1] == "run":
                self._mount_source(argv, "/output").joinpath("input.html").write_bytes(
                    b"<html><body>valid</body></html>"
                )

        self.runner.on_run = output_without_cid
        self._converter().convert(self.artifact)
        run_argv = next(call for call in self.runner.run_calls if call[1] == "run")
        name = run_argv[run_argv.index("--name") + 1]
        self.assertRegex(name, r"^kunjin-legacy-doc-[0-9a-f]{32}$")
        self.assertTrue(
            any(call[1:3] == ("container", "rm") and name in call for call in self.runner.run_calls)
        )

    def test_startup_reconciliation_removes_only_kunjin_labeled_stale_containers(self) -> None:
        stale = "e" * 64
        self.runner.stale_containers = ((stale, "exited"),)
        self._converter().status()
        query = next(call for call in self.runner.query_calls if call[1:3] == ("container", "ls"))
        self.assertIn("label=com.kunjin.legacy-doc=1", query)
        removed = [call[-1] for call in self.runner.run_calls if call[1:3] == ("container", "rm")]
        self.assertEqual(removed, [stale])

    def test_cleanup_absence_requires_successful_exact_private_query(self) -> None:
        target = "d" * 64
        self.runner.run_results.extend(
            (CommandResult(return_code=0), CommandResult(return_code=125))
        )

        def fail_absence_query(
            argv: Tuple[str, ...],
        ) -> Optional[Tuple[bytes, CommandResult]]:
            if argv[1:3] == ("container", "ls"):
                return b"", CommandResult(return_code=125)
            return None

        self.runner.on_query = fail_absence_query
        self.assertFalse(self._converter()._remove_and_verify_absent(target))

        self.runner.run_results.append(CommandResult(return_code=None))
        self.assertFalse(self._converter()._remove_and_verify_absent(target))

    def test_running_labeled_container_is_not_removed_during_reconciliation(self) -> None:
        running = "e" * 64
        self.runner.stale_containers = ((running, "running"),)
        converter = self._converter()
        status = converter.status()
        self.assertEqual(status.status, "unavailable")
        self.assertFalse(
            any(
                call[1:3] == ("container", "rm") and call[-1] == running
                for call in self.runner.run_calls
            )
        )
        self.assertEqual(converter.status().status, "unavailable")

    def test_unknown_labeled_container_state_fails_closed_without_removal(self) -> None:
        unknown = "f" * 64
        self.runner.stale_containers = ((unknown, "future-state"),)
        converter = self._converter()
        self.assertEqual(converter.status().status, "unavailable")
        self.assertFalse(any(call[1:3] == ("container", "rm") for call in self.runner.run_calls))

    def test_workspace_not_a_directory_is_redacted_and_status_never_raises(self) -> None:
        shutil.rmtree(self.paths.legacy_doc_runtime)
        self.paths.legacy_doc_runtime.write_text("PRIVATE-PATH-SENTINEL", encoding="ascii")
        converter = self._converter()
        status = converter.status()
        self.assertEqual(status.status, "unavailable")
        with self.assertRaises(LegacyDocConversionError) as caught:
            converter.convert(self.artifact)
        rendered = repr(caught.exception) + str(caught.exception)
        self.assertNotIn(str(self.paths.legacy_doc_runtime), rendered)
        self.assertNotIn("PRIVATE-PATH-SENTINEL", rendered)

    def test_launch_failure_maps_to_converter_unavailable(self) -> None:
        self.runner.run_results.append(CommandResult(return_code=None, launch_failed=True))
        with self.assertRaises(LegacyDocConversionError) as caught:
            self._converter().convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
        )

    def test_launch_failure_does_not_require_cleanup_proof_or_become_timeout(self) -> None:
        self.runner.run_results.extend(
            (
                CommandResult(return_code=None, launch_failed=True),
                CommandResult(return_code=None, launch_failed=True),
            )
        )
        converter = self._converter()

        with self.assertRaises(LegacyDocConversionError) as caught:
            converter.convert(self.artifact)

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
        )
        self.assertFalse(any(call[1:3] == ("container", "rm") for call in self.runner.run_calls))
        self.assertEqual(converter.status().status, "unavailable")

    def test_nonzero_oom_state_maps_to_resource_limit(self) -> None:
        identifier = "d" * 64
        name_seen: list[str] = []

        def failed_oom_run(argv: Tuple[str, ...]) -> None:
            if argv[1] != "run":
                return
            name = argv[argv.index("--name") + 1]
            name_seen.append(name)
            Path(argv[argv.index("--cidfile") + 1]).write_text(identifier + "\n", encoding="ascii")
            self.runner.container_states[identifier] = (identifier, name, True, 137)

        self.runner.on_run = failed_oom_run
        self.runner.run_results.append(CommandResult(return_code=137))
        with self.assertRaises(LegacyDocConversionError) as caught:
            self._converter().convert(self.artifact)
        self.assertTrue(name_seen)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT,
        )

    def test_nonzero_authenticated_non_oom_state_maps_to_failed(self) -> None:
        identifier = "d" * 64

        def failed_run(argv: Tuple[str, ...]) -> None:
            if argv[1] != "run":
                return
            name = argv[argv.index("--name") + 1]
            Path(argv[argv.index("--cidfile") + 1]).write_text(identifier + "\n", encoding="ascii")
            self.runner.container_states[identifier] = (identifier, name, False, 1)

        self.runner.on_run = failed_run
        self.runner.run_results.append(CommandResult(return_code=1))
        with self.assertRaises(LegacyDocConversionError) as caught:
            self._converter().convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_FAILED,
        )

    def test_nonzero_without_authenticated_state_maps_to_unavailable(self) -> None:
        self.runner.on_run = self._successful_run(b"<html><body>discarded</body></html>")
        self.runner.run_results.append(CommandResult(return_code=1))
        with self.assertRaises(LegacyDocConversionError) as caught:
            converter = self._converter()
            converter.convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
        )
        self.assertEqual(converter.status().status, "unavailable")

    def test_termination_failure_maps_to_timeout_and_disables(self) -> None:
        self.runner.on_run = self._successful_run(b"<html><body>discarded</body></html>")
        self.runner.run_results.append(
            CommandResult(return_code=None, timed_out=True, termination_failed=True)
        )
        converter = self._converter()
        with self.assertRaises(LegacyDocConversionError) as caught:
            converter.convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT,
        )
        self.assertEqual(converter.status().status, "unavailable")

    def test_cleanup_failure_disables_converter_and_discards_output(self) -> None:
        self.runner.on_run = self._successful_run(b"<html><body>must disappear</body></html>")
        self.runner.run_results.extend(
            (
                CommandResult(return_code=0),
                CommandResult(return_code=0),
                CommandResult(return_code=0),
                CommandResult(return_code=0),
                CommandResult(return_code=0),
            )
        )
        converter = self._converter()
        with self.assertRaises(LegacyDocConversionError) as caught:
            converter.convert(self.artifact)
        self.assertEqual(
            caught.exception.failure.reason_code, DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
        )
        self.assertEqual(converter.status().status, "unavailable")
        self.assertFalse(any(self.paths.legacy_doc_runtime.glob("run-*")))

    def test_invalid_utf8_nul_replacement_script_external_or_oversized_output_fails(self) -> None:
        invalid_outputs = (
            b"\xff",
            b"<html>nul\x00</html>",
            "<html>bad\ufffd</html>".encode(),
            b"<html><script>alert(1)</script></html>",
            b'<html><img src="https://example.test/x"></html>',
            b"<html>" + b"x" * (DockerLegacyDocConverter.MAX_OUTPUT_BYTES + 1) + b"</html>",
        )
        for payload in invalid_outputs:
            with self.subTest(prefix=payload[:20]):
                self.runner.run_calls.clear()
                self.runner.on_run = self._successful_run(payload)
                with self.assertRaises(LegacyDocConversionError) as caught:
                    self._converter().convert(self.artifact)
                self.assertIn(
                    caught.exception.failure.reason_code,
                    {
                        DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID,
                        DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT,
                    },
                )

    def test_html_rejects_external_doctype_processing_instruction_refresh_and_unclosed_tags(
        self,
    ) -> None:
        invalid_outputs = (
            b'<!DOCTYPE html SYSTEM "private.dtd"><html></html>',
            b'<!DOCTYPE html PUBLIC "private" "private.dtd"><html></html>',
            b"<?xml version='1.0'?><html></html>",
            b'<html><head><meta http-equiv="  ReFrEsH  " content="0"></head></html>',
            b'<html><body onclick="">value</body></html>',
            b"<html><body><table><tr><td>value</tr></table></body></html>",
            b"<html><body><div>value</body></html>",
        )
        for payload in invalid_outputs:
            with self.subTest(payload=payload):
                self.runner.on_run = self._successful_run(payload)
                with self.assertRaises(LegacyDocConversionError) as caught:
                    self._converter().convert(self.artifact)
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID,
                )

    def test_html_accepts_exact_doctype_and_common_libreoffice_void_tags(self) -> None:
        self.runner.on_run = self._successful_run(
            b" \n<!DOCTYPE HTML><html><head><meta charset='utf-8'></head>"
            b"<body><p>value<br><img alt='x'></p></body></html>\n\t"
        )
        result = self._converter().convert(self.artifact)
        self.assertIn("<!DOCTYPE HTML>", result.normalized_html)

    def test_html_accepts_bounded_libreoffice_formatting_and_list_recovery(self) -> None:
        self.runner.on_run = self._successful_run(
            b"<!DOCTYPE html><html><body><p>"
            b"<span><font><span><font>value</span></font></font>"
            b"</p><ol><li>item</ol></body></html>"
        )
        result = self._converter().convert(self.artifact)
        self.assertIn("<span><font><span><font>value</span>", result.normalized_html)

    def test_html_rejects_multiple_roots_or_content_after_root(self) -> None:
        invalid_outputs = (
            b"<html><body>one</body></html><html><body>two</body></html>",
            b"<html><body>value</body></html><!DOCTYPE html>",
            b"<html><body>value</body></html>PRIVATE-BANNER",
            b"<html><body>value</body></html><!-- PRIVATE-BANNER -->",
            b"PRIVATE-BANNER<html><body>value</body></html>",
            b"<!-- PRIVATE-BANNER --><html><body>value</body></html>",
            b"<!DOCTYPE html><!DOCTYPE html><html><body>value</body></html>",
        )
        for payload in invalid_outputs:
            with self.subTest(payload=payload):
                self.runner.on_run = self._successful_run(payload)
                with self.assertRaises(LegacyDocConversionError) as caught:
                    self._converter().convert(self.artifact)
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID,
                )

    def test_workspace_validation_failure_removes_created_workspace(self) -> None:
        converter = self._converter()
        validation_calls = 0

        def reject_workspace(path: Path) -> None:
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == 2:
                raise ValueError("PRIVATE-WORKSPACE-SENTINEL")

        with patch(
            "kunjin.funds.risk.legacy_doc._validate_private_directory",
            side_effect=reject_workspace,
        ):
            with self.assertRaises(ValueError) as caught:
                converter._create_private_workspace()
        self.assertNotIn(str(self.paths.legacy_doc_runtime), str(caught.exception))
        self.assertFalse(any(self.paths.legacy_doc_runtime.glob("run-*")))

    def test_subdirectory_creation_failure_is_redacted_and_workspace_is_removed(self) -> None:
        converter = self._converter()
        self.assertEqual(converter.status().status, "ready")
        original_mkdir = Path.mkdir

        def reject_input_directory(path: Path, *args: object, **kwargs: object) -> None:
            if path.name == "input":
                raise OSError("PRIVATE-SUBDIRECTORY-SENTINEL")
            original_mkdir(path, *args, **kwargs)

        with patch("kunjin.funds.risk.legacy_doc.Path.mkdir", new=reject_input_directory):
            with self.assertRaises(LegacyDocConversionError) as caught:
                converter.convert(self.artifact)
        rendered = repr(caught.exception) + str(caught.exception)
        self.assertNotIn("PRIVATE-SUBDIRECTORY-SENTINEL", rendered)
        self.assertNotIn(str(self.paths.legacy_doc_runtime), rendered)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
        )
        self.assertFalse(any(self.paths.legacy_doc_runtime.glob("run-*")))

    def test_output_rejects_symlink_wrong_owner_and_extra_file(self) -> None:
        cases = ("symlink", "owner", "extra")
        for case in cases:
            with self.subTest(case=case):
                converter = self._converter()
                output = self.root / f"output-{case}"
                output.mkdir(mode=0o700)
                expected = output / "input.html"
                if case == "symlink":
                    target = output / "target.html"
                    target.write_text("<html></html>", encoding="ascii")
                    expected.symlink_to(target)
                else:
                    expected.write_text("<html></html>", encoding="ascii")
                if case == "extra":
                    (output / "extra.txt").write_text("extra", encoding="ascii")
                context = (
                    patch("kunjin.funds.risk.legacy_doc.os.getuid", return_value=os.getuid() + 1)
                    if case == "owner"
                    else patch("kunjin.funds.risk.legacy_doc.os.getuid", wraps=os.getuid)
                )
                with context, self.assertRaises(LegacyDocConversionError) as caught:
                    converter._read_and_validate_output(output)
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID,
                )

    def test_valid_output_is_nfc_normalized_and_has_stable_checksum(self) -> None:
        self.runner.on_run = self._successful_run(
            b"\xef\xbb\xbf<html>\r\n<body>cafe\xcc\x81</body>\r\n</html>"
        )
        first = self._converter().convert(self.artifact)
        self.runner.on_run = self._successful_run(
            b"\xef\xbb\xbf<html>\r\n<body>cafe\xcc\x81</body>\r\n</html>"
        )
        second = self._converter().convert(self.artifact)
        self.assertEqual(first.normalized_html, "<html>\n<body>caf\u00e9</body>\n</html>")
        self.assertEqual(first.parser_input_sha256, second.parser_input_sha256)
        self.assertEqual(
            first.parser_input_sha256, hashlib.sha256(first.normalized_html.encode()).hexdigest()
        )

    def test_failure_repr_and_public_result_do_not_contain_path_argv_stderr_or_html(self) -> None:
        sentinel = "PRIVATE-SENTINEL"
        self.runner.on_run = self._successful_run(
            f"<html><script>{sentinel}</script></html>".encode()
        )
        with self.assertRaises(LegacyDocConversionError) as caught:
            self._converter().convert(self.artifact)
        rendered = repr(caught.exception) + str(caught.exception) + repr(caught.exception.failure)
        self.assertNotIn(sentinel, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("docker run", rendered)
        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.CONVERSION)


if __name__ == "__main__":
    unittest.main()
