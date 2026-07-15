import shutil
import subprocess
import tempfile
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

    def test_phase_d1_readme_and_skill_contracts_are_packaged(self) -> None:
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")
        skill = (root / "integrations/codex/kunjin-fund/SKILL.md").read_text(encoding="utf-8")
        agent = (root / "integrations/codex/kunjin-fund/agents/openai.yaml").read_text(
            encoding="utf-8"
        )

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

        self.assertIn("fact-only D1 research does not require Phase B or Phase C", skill)
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
            "Stop on every non-`verified` D1 result",
            "D2 portfolio correlation and overlap controls",
            "D3 product-selection and pre-purchase checks",
        ):
            self.assertIn(phrase, skill)

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
