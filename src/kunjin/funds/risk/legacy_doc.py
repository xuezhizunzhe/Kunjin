from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, fields
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Protocol, Tuple

from kunjin.funds.risk.audit import (
    ACTIVE_LEGACY_PARSER_VERSION,
    ParserProvenance,
    legacy_parser_provenance,
)
from kunjin.funds.risk.documents import RetrievedArtifact
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.paths import RuntimePaths

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_NAME_PATTERN = re.compile(r"^kunjin-legacy-doc-[0-9a-f]{32}$")
_BASE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ABNORMAL_CONTROL_PATTERN = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DANGEROUS_CSS_PATTERN = re.compile(r"(?:url\s*\(|@import|expression\s*\()", re.IGNORECASE)
_DANGEROUS_TAGS = frozenset({"script", "object", "embed", "iframe", "applet"})
_RESOURCE_ATTRIBUTES = frozenset(
    {
        "action",
        "background",
        "cite",
        "data",
        "formaction",
        "href",
        "longdesc",
        "poster",
        "src",
        "srcset",
        "xlink:href",
    }
)
_DOCKER_PATH = Path("/usr/local/bin/docker")
_DOCKER_DESKTOP_CLI = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
_CONVERTER_LABEL = "com.kunjin.legacy-doc=1"
_IMAGE_CONTRACT_LABEL = "com.kunjin.legacy-doc.contract"
_BASE_DIGEST_LABEL = "com.kunjin.legacy-doc.base-image-digest"
_LIBREOFFICE_VERSION_LABEL = "com.kunjin.legacy-doc.libreoffice-version"
_PACKAGE_CHECKSUM_LABEL = "com.kunjin.legacy-doc.package-manifest-sha256"
_CONTAINER_LIST_FORMAT = "{{.ID}}\t{{.Names}}\t{{.State}}"
_CONTAINER_STATE_FORMAT = (
    "{{.Id}}\t{{.Name}}\t{{.State.Status}}\t{{.State.OOMKilled}}\t{{.State.ExitCode}}"
)
_KNOWN_CONTAINER_STATES = frozenset(
    {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
)
_REMOVABLE_STALE_CONTAINER_STATES = frozenset({"exited", "dead"})
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_LIBREOFFICE_RECOVERABLE_INLINE_TAGS = frozenset({"a", "b", "font", "span"})
_LIBREOFFICE_LIST_CONTAINERS = frozenset({"ol", "ul"})
_CLEAN_DOCKER_ENVIRONMENT = {
    "HOME": "/var/empty",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
}
_IMAGE_INSPECT_FORMAT = "\t".join(
    (
        "{{.Id}}",
        "{{.Os}}",
        "{{.Architecture}}",
        '{{index .Config.Labels "' + _IMAGE_CONTRACT_LABEL + '"}}',
        '{{index .Config.Labels "' + _BASE_DIGEST_LABEL + '"}}',
        '{{index .Config.Labels "' + _LIBREOFFICE_VERSION_LABEL + '"}}',
        '{{index .Config.Labels "' + _PACKAGE_CHECKSUM_LABEL + '"}}',
    )
)
_STALE_CONTAINER_QUERY = (
    str(_DOCKER_PATH),
    "container",
    "ls",
    "--all",
    "--no-trunc",
    "--filter",
    "label=" + _CONVERTER_LABEL,
    "--format",
    "{{.ID}}\t{{.State}}",
)


@dataclass(frozen=True)
class CommandResult:
    return_code: Optional[int]
    timed_out: bool = False
    launch_failed: bool = False
    termination_failed: bool = False
    output_limit_exceeded: bool = False

    def validate(self) -> None:
        _require_exact_record(self, CommandResult, "Docker command result")
        if self.return_code is not None and type(self.return_code) is not int:
            raise ValueError("Docker command return code must be an exact integer")
        for value in (
            self.timed_out,
            self.launch_failed,
            self.termination_failed,
            self.output_limit_exceeded,
        ):
            if type(value) is not bool:
                raise ValueError("Docker command state flags must be exact booleans")
        if self.return_code is not None and (
            self.timed_out
            or self.launch_failed
            or self.termination_failed
            or self.output_limit_exceeded
        ):
            raise ValueError("Docker command terminal state is contradictory")


class DockerCommandRunner(Protocol):
    def run(self, argv: Tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        raise NotImplementedError

    def query(
        self,
        argv: Tuple[str, ...],
        *,
        timeout_seconds: int,
        output_path: Path,
        max_output_bytes: int,
    ) -> CommandResult:
        raise NotImplementedError


@dataclass(frozen=True)
class ConverterStatus:
    capability: str
    status: str
    reason_code: Optional[str]
    parser_version: Optional[str]
    provenance_checksum: Optional[str]

    def validate(self) -> None:
        _require_exact_record(self, ConverterStatus, "legacy converter status")
        if self.capability != "research_only":
            raise ValueError("legacy converter capability is invalid")
        if self.status not in {"ready", "unavailable", "invalid"}:
            raise ValueError("legacy converter status is invalid")
        if self.status == "ready":
            if self.reason_code is not None:
                raise ValueError("ready converter cannot have a failure reason")
            if self.parser_version != ACTIVE_LEGACY_PARSER_VERSION:
                raise ValueError("ready converter parser version is invalid")
            if type(self.provenance_checksum) is not str or not _SHA256_PATTERN.fullmatch(
                self.provenance_checksum
            ):
                raise ValueError("ready converter provenance checksum is invalid")
        elif (
            self.reason_code != DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE.value
            or self.parser_version is not None
            or self.provenance_checksum is not None
        ):
            raise ValueError("unready converter status exposes invalid metadata")


@dataclass(frozen=True)
class LegacyConversionResult:
    normalized_html: str
    parser_input_sha256: str
    provenance: ParserProvenance

    def validate(self) -> None:
        _require_exact_record(self, LegacyConversionResult, "legacy conversion result")
        if type(self.normalized_html) is not str or not self.normalized_html:
            raise ValueError("legacy conversion HTML is invalid")
        if type(self.parser_input_sha256) is not str or not _SHA256_PATTERN.fullmatch(
            self.parser_input_sha256
        ):
            raise ValueError("legacy conversion checksum is invalid")
        if hashlib.sha256(self.normalized_html.encode("utf-8")).hexdigest() != (
            self.parser_input_sha256
        ):
            raise ValueError("legacy conversion checksum does not match HTML")
        if type(self.provenance) is not ParserProvenance:
            raise ValueError("legacy conversion provenance must be exact")
        self.provenance.validate()
        if self.provenance.converter_kind != "docker_libreoffice":
            raise ValueError("legacy conversion provenance must use the Docker converter")


class LegacyDocConverter(Protocol):
    def status(self) -> ConverterStatus:
        raise NotImplementedError

    def active_provenance(self) -> Optional[ParserProvenance]:
        raise NotImplementedError

    def convert(self, artifact: RetrievedArtifact) -> LegacyConversionResult:
        raise NotImplementedError


class LegacyDocConversionError(RuntimeError):
    """A redacted deterministic conversion failure."""

    def __init__(self, failure: SafeDocumentFailure) -> None:
        if type(failure) is not SafeDocumentFailure:
            raise ValueError("legacy conversion failure must be exact")
        failure.validate()
        if failure.stage is not DocumentFailureStage.CONVERSION:
            raise ValueError("legacy conversion failure stage is invalid")
        self.code = failure.public_code
        self.failure = failure
        super().__init__("official legacy document conversion failed")

    def __repr__(self) -> str:
        return (
            "LegacyDocConversionError("
            f"public_code={self.failure.public_code!r}, "
            f"stage={self.failure.stage.value!r}, "
            f"reason_code={self.failure.reason_code.value!r})"
        )


class SubprocessDockerCommandRunner:
    _KILL_WAIT_SECONDS = 2

    def run(self, argv: Tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        _validate_argv(argv)
        _validate_timeout(timeout_seconds)
        try:
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                restore_signals=True,
                start_new_session=True,
                env=_CLEAN_DOCKER_ENVIRONMENT,
            )
        except (OSError, ValueError):
            return _checked_result(CommandResult(return_code=None, launch_failed=True))
        return self._wait(process, timeout_seconds=timeout_seconds)

    def query(
        self,
        argv: Tuple[str, ...],
        *,
        timeout_seconds: int,
        output_path: Path,
        max_output_bytes: int,
    ) -> CommandResult:
        _validate_metadata_query(argv)
        _validate_timeout(timeout_seconds)
        _validate_private_query_destination(output_path, max_output_bytes)
        descriptor: Optional[int] = None
        output = None
        try:
            descriptor = os.open(
                output_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            output = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = None
            process = subprocess.Popen(
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                restore_signals=True,
                start_new_session=True,
                env=_CLEAN_DOCKER_ENVIRONMENT,
            )
            result = self._wait(process, timeout_seconds=timeout_seconds)
        except (OSError, ValueError):
            result = _checked_result(CommandResult(return_code=None, launch_failed=True))
        finally:
            if output is not None:
                output.close()
            if descriptor is not None:
                os.close(descriptor)
        if result.return_code == 0:
            try:
                if output_path.lstat().st_size > max_output_bytes:
                    return _checked_result(
                        CommandResult(return_code=None, output_limit_exceeded=True)
                    )
            except OSError:
                return _checked_result(CommandResult(return_code=None, launch_failed=True))
        return result

    def _wait(self, process: subprocess.Popen, *, timeout_seconds: int) -> CommandResult:
        try:
            return_code = process.wait(timeout=timeout_seconds)
            return _checked_result(CommandResult(return_code=return_code))
        except subprocess.TimeoutExpired:
            termination_failed = not self._terminate_process_group(process)
            return _checked_result(
                CommandResult(
                    return_code=None,
                    timed_out=True,
                    termination_failed=termination_failed,
                )
            )

    def _terminate_process_group(self, process: subprocess.Popen) -> bool:
        pid = getattr(process, "pid", None)
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            process.wait(timeout=self._KILL_WAIT_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        try:
            process.wait(timeout=self._KILL_WAIT_SECONDS)
            return True
        except subprocess.TimeoutExpired:
            return False


class DockerLegacyDocConverter:
    DOCKER_PATH = _DOCKER_PATH
    MAX_INPUT_BYTES = 64 * 1024 * 1024
    MAX_OUTPUT_BYTES = 4 * 1024 * 1024
    MAX_OUTPUT_CHARACTERS = 3 * 1024 * 1024
    RUN_TIMEOUT_SECONDS = 45
    CLEANUP_TIMEOUT_SECONDS = 10
    QUERY_TIMEOUT_SECONDS = 10
    MAX_STALE_CONTAINERS = 128

    def __init__(
        self,
        *,
        image_id: Optional[str],
        runtime_paths: RuntimePaths,
        libreoffice_version: Optional[str] = None,
        package_manifest_checksum: Optional[str] = None,
        runner: Optional[DockerCommandRunner] = None,
    ) -> None:
        if type(runtime_paths) is not RuntimePaths:
            raise ValueError("runtime paths must be exact")
        self._image_id = image_id
        self._libreoffice_version = libreoffice_version
        self._package_manifest_checksum = package_manifest_checksum
        self._runtime_paths = runtime_paths
        self._runner = runner if runner is not None else SubprocessDockerCommandRunner()
        self._disabled = False
        self._cleanup_uncertain = False
        self._reconciled = False
        self._verified_provenance: Optional[ParserProvenance] = None
        self._lock = threading.RLock()

    def status(self) -> ConverterStatus:
        with self._lock:
            try:
                if self._disabled:
                    return _unready_status("unavailable")
                if type(self._image_id) is not str or not _IMAGE_ID_PATTERN.fullmatch(
                    self._image_id
                ):
                    return _unready_status(
                        "invalid" if self._image_id is not None else "unavailable"
                    )
                if (self._libreoffice_version is None) != (self._package_manifest_checksum is None):
                    return _unready_status("invalid")
                if self._runtime_identity() is None:
                    return _unready_status("unavailable")
                if not self._docker_cli_is_trusted():
                    return _unready_status("unavailable")
                if not self._reconciled:
                    if not self._reconcile_stale_containers():
                        self._disabled = True
                        return _unready_status("unavailable")
                    self._reconciled = True
                inspection = self._query_image_inspection()
                if inspection is None:
                    return _unready_status("unavailable")
                provenance = self._verified_image_provenance(inspection)
                if provenance is None:
                    return _unready_status("invalid")
                self._verified_provenance = provenance
                result = ConverterStatus(
                    capability="research_only",
                    status="ready",
                    reason_code=None,
                    parser_version=provenance.parser_version,
                    provenance_checksum=provenance.provenance_checksum,
                )
                result.validate()
                return result
            except (OSError, ValueError):
                self._disabled = True
                return _unready_status("unavailable")

    def active_provenance(self) -> Optional[ParserProvenance]:
        with self._lock:
            if self._verified_provenance is not None:
                return self._verified_provenance
            status = self.status()
            if status.status != "ready":
                return None
            return self._verified_provenance

    def convert(self, artifact: RetrievedArtifact) -> LegacyConversionResult:
        with self._lock:
            provenance = self._require_ready_provenance()
            try:
                _validate_artifact_record(artifact)
            except LegacyDocConversionError:
                raise
            except (OSError, ValueError):
                raise _conversion_error(
                    DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID
                ) from None
            workspace: Optional[Path] = None
            try:
                workspace = self._create_private_workspace()
            except (OSError, ValueError):
                self._disabled = True
                reason = (
                    DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
                    if self._cleanup_uncertain
                    else DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE
                )
                raise _conversion_error(reason) from None
            container_name = "kunjin-legacy-doc-" + secrets.token_hex(16)
            if not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
                self._disabled = True
                if not _remove_private_workspace(workspace):
                    self._cleanup_uncertain = True
                    raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT)
                raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE)
            cidfile = workspace / "container.cid"
            input_directory = workspace / "input"
            output_directory = workspace / "output"
            run_result: Optional[CommandResult] = None
            cleanup_proven = False
            try:
                input_directory.mkdir(mode=0o700)
                output_directory.mkdir(mode=0o700)
            except (OSError, ValueError):
                self._disabled = True
                if not _remove_private_workspace(workspace):
                    self._cleanup_uncertain = True
                    raise _conversion_error(
                        DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
                    ) from None
                raise _conversion_error(
                    DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE
                ) from None
            try:
                self._copy_authenticated_artifact(artifact, input_directory / "input.doc")
                argv = self._run_argv(
                    container_name=container_name,
                    cidfile=cidfile,
                    input_directory=input_directory,
                    output_directory=output_directory,
                )
                run_result = self._checked_run(argv, timeout_seconds=self.RUN_TIMEOUT_SECONDS)
                if run_result.launch_failed:
                    self._disabled = True
                    raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE)
                cid = self._read_container_id(cidfile)
                run_failure = self._classify_run_failure(
                    run_result,
                    target=cid if cid is not None else container_name,
                )
                cleanup_proven = self._force_cleanup(cid=cid, container_name=container_name)
                if self._cleanup_uncertain or not cleanup_proven or run_result.termination_failed:
                    self._disabled = True
                    raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT)
                if run_failure is not None:
                    if run_failure is DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE:
                        self._disabled = True
                    raise _conversion_error(run_failure)
                normalized_html = self._read_and_validate_output(output_directory)
                result = LegacyConversionResult(
                    normalized_html=normalized_html,
                    parser_input_sha256=hashlib.sha256(normalized_html.encode("utf-8")).hexdigest(),
                    provenance=provenance,
                )
                result.validate()
                return result
            except LegacyDocConversionError:
                raise
            except (OSError, ValueError):
                if run_result is not None and not cleanup_proven:
                    self._disabled = True
                    raise _conversion_error(
                        DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
                    ) from None
                raise _conversion_error(
                    DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID
                ) from None
            finally:
                if not _remove_private_workspace(workspace):
                    self._disabled = True
                    self._cleanup_uncertain = True
                    raise _conversion_error(
                        DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
                    ) from None

    def _verified_image_provenance(
        self,
        inspection: Tuple[str, ...],
    ) -> Optional[ParserProvenance]:
        try:
            if (
                inspection[0] != self._image_id
                or inspection[1:4] != ("linux", "arm64", "docker-libreoffice-v1")
                or not _BASE_DIGEST_PATTERN.fullmatch(inspection[4])
            ):
                return None
            libreoffice_version = inspection[5]
            package_manifest_checksum = inspection[6]
            if self._libreoffice_version is not None and (
                self._libreoffice_version != libreoffice_version
                or self._package_manifest_checksum != package_manifest_checksum
            ):
                return None
            return legacy_parser_provenance(
                image_id=inspection[0],
                architecture="linux/arm64",
                libreoffice_version=libreoffice_version,
                package_manifest_checksum=package_manifest_checksum,
            )
        except (IndexError, ValueError):
            return None

    def _require_ready_provenance(self) -> ParserProvenance:
        status = self.status()
        if status.status != "ready":
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE)
        provenance = self._verified_provenance
        if provenance is None or provenance.provenance_checksum != status.provenance_checksum:
            self._disabled = True
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE)
        return provenance

    def _docker_cli_is_trusted(self) -> bool:
        try:
            if not self.DOCKER_PATH.is_symlink():
                return False
            resolved = self.DOCKER_PATH.resolve(strict=True)
            expected = _DOCKER_DESKTOP_CLI.resolve(strict=True)
            return resolved == expected and resolved.is_file()
        except OSError:
            return False

    def _create_private_workspace(self) -> Path:
        root = self._runtime_paths.legacy_doc_runtime
        workspace: Optional[Path] = None
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _validate_private_directory(root)
            workspace = Path(tempfile.mkdtemp(prefix="run-", dir=root))
            _validate_private_directory(workspace)
            return workspace
        except (OSError, ValueError):
            if workspace is not None and not _remove_private_workspace(workspace):
                self._disabled = True
                self._cleanup_uncertain = True
            raise

    def _private_query(self, argv: Tuple[str, ...], *, max_output_bytes: int) -> Optional[bytes]:
        workspace: Optional[Path] = None
        output_path: Optional[Path] = None
        payload: Optional[bytes] = None
        try:
            workspace = self._create_private_workspace()
            output_path = workspace / "metadata.out"
            result = self._runner.query(
                argv,
                timeout_seconds=self.QUERY_TIMEOUT_SECONDS,
                output_path=output_path,
                max_output_bytes=max_output_bytes,
            )
            result.validate()
            if (
                result.return_code != 0
                or result.timed_out
                or result.launch_failed
                or result.termination_failed
                or result.output_limit_exceeded
            ):
                payload = None
            else:
                payload = _read_stable_private_file(output_path, max_bytes=max_output_bytes)
        except (OSError, ValueError):
            payload = None
        finally:
            if output_path is not None:
                try:
                    output_path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    payload = None
            if workspace is not None and not _remove_private_workspace(workspace):
                self._disabled = True
                self._cleanup_uncertain = True
                payload = None
        return payload

    def _query_image_inspection(self) -> Optional[Tuple[str, ...]]:
        if type(self._image_id) is not str or not _IMAGE_ID_PATTERN.fullmatch(self._image_id):
            return None
        argv = (
            str(self.DOCKER_PATH),
            "image",
            "inspect",
            self._image_id,
            "--format",
            _IMAGE_INSPECT_FORMAT,
        )
        payload = self._private_query(argv, max_output_bytes=2048)
        if payload is None:
            return None
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            return None
        if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text:
            return None
        fields_value = tuple(text[:-1].split("\t"))
        if len(fields_value) != 7 or any(not value for value in fields_value):
            return None
        return fields_value

    def _reconcile_stale_containers(self) -> bool:
        payload = self._private_query(_STALE_CONTAINER_QUERY, max_output_bytes=8192)
        if payload is None:
            return False
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            return False
        if "\r" in text or (text and not text.endswith("\n")):
            return False
        records = tuple(value.split("\t") for value in text.splitlines() if value)
        if len(records) > self.MAX_STALE_CONTAINERS or any(len(record) != 2 for record in records):
            return False
        identifiers = tuple(record[0] for record in records)
        if len(set(identifiers)) != len(identifiers):
            return False
        for identifier, state in records:
            if (
                not _CONTAINER_ID_PATTERN.fullmatch(identifier)
                or state not in _KNOWN_CONTAINER_STATES
                or state not in _REMOVABLE_STALE_CONTAINER_STATES
            ):
                return False
        for identifier, _state in records:
            if not self._remove_and_verify_absent(identifier):
                return False
        return True

    def _classify_run_failure(
        self, result: CommandResult, *, target: str
    ) -> Optional[DocumentFailureReason]:
        if result.termination_failed or result.timed_out:
            return DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT
        if result.output_limit_exceeded:
            return DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT
        if result.launch_failed:
            return DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE
        if result.return_code == 0:
            return None
        state = self._query_container_state(target)
        if state is None:
            return DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE
        return (
            DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT
            if state[3]
            else DocumentFailureReason.LEGACY_CONVERTER_FAILED
        )

    def _copy_authenticated_artifact(self, artifact: RetrievedArtifact, destination: Path) -> None:
        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(artifact.managed_path, source_flags)
        destination_descriptor: Optional[int] = None
        digest = hashlib.sha256()
        byte_count = 0
        try:
            before = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or before.st_size != artifact.byte_size
            ):
                raise ValueError("managed legacy artifact is invalid")
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            while True:
                chunk = os.read(source_descriptor, 64 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > self.MAX_INPUT_BYTES or byte_count > artifact.byte_size:
                    raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT)
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise OSError("private legacy artifact copy failed")
                    view = view[written:]
            os.fsync(destination_descriptor)
            after = os.fstat(source_descriptor)
            if (before.st_dev, before.st_ino, before.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ValueError("managed legacy artifact changed during copy")
        finally:
            os.close(source_descriptor)
            if destination_descriptor is not None:
                os.close(destination_descriptor)
        if byte_count != artifact.byte_size or digest.hexdigest() != artifact.sha256:
            raise ValueError("managed legacy artifact checksum mismatch")
        copied = destination.lstat()
        if (
            not stat.S_ISREG(copied.st_mode)
            or copied.st_nlink != 1
            or copied.st_uid != os.getuid()
            or stat.S_IMODE(copied.st_mode) != 0o600
        ):
            raise ValueError("private legacy artifact copy is invalid")

    def _run_argv(
        self,
        *,
        container_name: str,
        cidfile: Path,
        input_directory: Path,
        output_directory: Path,
    ) -> Tuple[str, ...]:
        runtime_identity = self._runtime_identity()
        if (
            not _CONTAINER_NAME_PATTERN.fullmatch(container_name)
            or type(self._image_id) is not str
            or not _IMAGE_ID_PATTERN.fullmatch(self._image_id)
            or runtime_identity is None
        ):
            raise ValueError("legacy converter runtime identity is invalid")
        runtime_user = f"{runtime_identity[0]}:{runtime_identity[1]}"
        return (
            str(self.DOCKER_PATH),
            "run",
            "--pull=never",
            "--name",
            container_name,
            "--cidfile",
            str(cidfile),
            "--label",
            _CONVERTER_LABEL,
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
            "--user=" + runtime_user,
            "--log-driver=none",
            "--mount",
            f"type=bind,src={input_directory},dst=/input,readonly",
            "--mount",
            f"type=bind,src={output_directory},dst=/output",
            "--tmpfs",
            (
                "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,"
                f"uid={runtime_identity[0]},gid={runtime_identity[1]}"
            ),
            "--workdir=/tmp",
            self._image_id,
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

    @staticmethod
    def _runtime_identity() -> Optional[Tuple[int, int]]:
        uid = os.getuid()
        gid = os.getgid()
        if type(uid) is not int or type(gid) is not int or uid <= 0 or gid < 0:
            return None
        return uid, gid

    def _checked_run(self, argv: Tuple[str, ...], *, timeout_seconds: int) -> CommandResult:
        try:
            result = self._runner.run(argv, timeout_seconds=timeout_seconds)
            result.validate()
            return result
        except (OSError, ValueError):
            return _checked_result(CommandResult(return_code=None, launch_failed=True))

    def _read_container_id(self, cidfile: Path) -> Optional[str]:
        try:
            payload = _read_stable_private_file(cidfile, max_bytes=128, require_mode=None)
            text = payload.decode("ascii")
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        value = text.strip("\n")
        if text not in {value, value + "\n"} or not _CONTAINER_ID_PATTERN.fullmatch(value):
            return None
        return value

    def _force_cleanup(self, *, cid: Optional[str], container_name: str) -> bool:
        target = cid if cid is not None else container_name
        if cid is not None and not _CONTAINER_ID_PATTERN.fullmatch(cid):
            return False
        if cid is None and not _CONTAINER_NAME_PATTERN.fullmatch(container_name):
            return False
        if self._remove_and_verify_absent(target):
            return True
        if cid is not None:
            return self._remove_and_verify_absent(container_name)
        return False

    def _remove_and_verify_absent(self, target: str) -> bool:
        removal = self._checked_run(
            (str(self.DOCKER_PATH), "container", "rm", "--force", target),
            timeout_seconds=self.CLEANUP_TIMEOUT_SECONDS,
        )
        if (
            removal.return_code is None
            or removal.timed_out
            or removal.launch_failed
            or removal.termination_failed
            or removal.output_limit_exceeded
        ):
            return False
        inspection = self._checked_run(
            (str(self.DOCKER_PATH), "container", "inspect", target),
            timeout_seconds=self.CLEANUP_TIMEOUT_SECONDS,
        )
        if (
            inspection.return_code is None
            or inspection.timed_out
            or inspection.launch_failed
            or inspection.termination_failed
            or inspection.output_limit_exceeded
        ):
            return False
        if inspection.return_code == 0:
            return False
        payload = self._private_query(
            _container_listing_query(target),
            max_output_bytes=1024,
        )
        return payload == b""

    def _query_container_state(self, target: str) -> Optional[Tuple[str, str, str, bool, int]]:
        payload = self._private_query(
            _container_state_query(target),
            max_output_bytes=1024,
        )
        if payload is None:
            return None
        try:
            text = payload.decode("ascii")
        except UnicodeDecodeError:
            return None
        if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text:
            return None
        values = text[:-1].split("\t")
        if len(values) != 5:
            return None
        identifier, name, state, oom_text, exit_code_text = values
        if (
            not _CONTAINER_ID_PATTERN.fullmatch(identifier)
            or not name.startswith("/")
            or not _CONTAINER_NAME_PATTERN.fullmatch(name[1:])
            or state not in _KNOWN_CONTAINER_STATES
            or oom_text not in {"true", "false"}
            or not re.fullmatch(r"0|[1-9][0-9]{0,9}", exit_code_text)
        ):
            return None
        if _CONTAINER_ID_PATTERN.fullmatch(target):
            if identifier != target:
                return None
        elif _CONTAINER_NAME_PATTERN.fullmatch(target):
            if name != "/" + target:
                return None
        else:
            return None
        exit_code = int(exit_code_text)
        if exit_code > 2**31 - 1:
            return None
        return identifier, name[1:], state, oom_text == "true", exit_code

    def _read_and_validate_output(self, output_directory: Path) -> str:
        try:
            entries = tuple(output_directory.iterdir())
        except OSError:
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID) from None
        expected = output_directory / "input.html"
        if entries != (expected,):
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
        try:
            payload = _read_stable_private_file(
                expected,
                max_bytes=self.MAX_OUTPUT_BYTES,
                require_mode=None,
            )
        except _PrivateFileLimitError:
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT) from None
        except (OSError, ValueError):
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID) from None
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID) from None
        if text.startswith("\ufeff"):
            text = text[1:]
        normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
        if len(normalized) > self.MAX_OUTPUT_CHARACTERS:
            raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT)
        _validate_normalized_html(normalized)
        return normalized


class _PrivateFileLimitError(ValueError):
    pass


class _StrictConvertedHTMLValidator(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.failed = False
        self._open_tags: list[str] = []
        self._doctype_seen = False
        self._root_started = False
        self._root_closed = False

    def handle_starttag(self, tag: str, attrs: list[Tuple[str, Optional[str]]]) -> None:
        lowered_tag = tag.lower()
        if not self._root_started:
            if lowered_tag == "html":
                self._root_started = True
            else:
                self.failed = True
        elif self._root_closed or lowered_tag == "html":
            self.failed = True
        self._validate_attributes(attrs)
        if lowered_tag in _DANGEROUS_TAGS:
            self.failed = True
        if lowered_tag not in _VOID_HTML_TAGS:
            self._open_tags.append(lowered_tag)

    def _validate_attributes(self, attrs: list[Tuple[str, Optional[str]]]) -> None:
        for name, value in attrs:
            lowered_name = name.lower()
            normalized_value = value or ""
            if lowered_name.startswith("on"):
                self.failed = True
            elif lowered_name in _RESOURCE_ATTRIBUTES:
                if normalized_value and not (
                    lowered_name == "href" and normalized_value.startswith("#")
                ):
                    self.failed = True
            if lowered_name == "http-equiv" and normalized_value.strip().lower() == "refresh":
                self.failed = True
            if lowered_name == "style" and _DANGEROUS_CSS_PATTERN.search(normalized_value):
                self.failed = True

    def handle_startendtag(self, tag: str, attrs: list[Tuple[str, Optional[str]]]) -> None:
        lowered_tag = tag.lower()
        if not self._root_started or self._root_closed or lowered_tag == "html":
            self.failed = True
        self._validate_attributes(attrs)
        if lowered_tag in _DANGEROUS_TAGS:
            self.failed = True

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.lower()
        if not self._root_started or self._root_closed:
            self.failed = True
            return
        if lowered_tag in _VOID_HTML_TAGS:
            self.failed = True
            return
        if lowered_tag not in _LIBREOFFICE_RECOVERABLE_INLINE_TAGS:
            while (
                self._open_tags
                and self._open_tags[-1] in _LIBREOFFICE_RECOVERABLE_INLINE_TAGS
            ):
                self._open_tags.pop()
        if not self._open_tags:
            self.failed = True
            return
        if self._open_tags[-1] == lowered_tag:
            self._open_tags.pop()
        elif lowered_tag in _LIBREOFFICE_RECOVERABLE_INLINE_TAGS:
            if lowered_tag not in self._open_tags:
                return
            matching_index = len(self._open_tags) - 1 - self._open_tags[::-1].index(lowered_tag)
            if any(
                open_tag not in _LIBREOFFICE_RECOVERABLE_INLINE_TAGS
                for open_tag in self._open_tags[matching_index + 1 :]
            ):
                self.failed = True
                return
            del self._open_tags[matching_index:]
        elif lowered_tag in _LIBREOFFICE_LIST_CONTAINERS:
            if lowered_tag not in self._open_tags:
                self.failed = True
                return
            matching_index = len(self._open_tags) - 1 - self._open_tags[::-1].index(lowered_tag)
            if any(open_tag != "li" for open_tag in self._open_tags[matching_index + 1 :]):
                self.failed = True
                return
            del self._open_tags[matching_index:]
        else:
            self.failed = True
            return
        if lowered_tag == "html":
            self._root_closed = True

    def handle_decl(self, decl: str) -> None:
        if (
            self._doctype_seen
            or self._root_started
            or self._root_closed
            or decl.strip().lower() != "doctype html"
        ):
            self.failed = True
            return
        self._doctype_seen = True

    def unknown_decl(self, data: str) -> None:
        del data
        self.failed = True

    def handle_pi(self, data: str) -> None:
        del data
        self.failed = True

    def handle_data(self, data: str) -> None:
        if (not self._root_started or self._root_closed) and data.strip():
            self.failed = True

    def handle_comment(self, data: str) -> None:
        del data
        if not self._root_started or self._root_closed:
            self.failed = True

    @property
    def has_unclosed_tags(self) -> bool:
        return bool(self._open_tags)

    @property
    def has_complete_document(self) -> bool:
        return self._root_started and self._root_closed

    def handle_entityref(self, name: str) -> None:
        del name
        if not self._root_started or self._root_closed:
            self.failed = True

    def handle_charref(self, name: str) -> None:
        del name
        if not self._root_started or self._root_closed:
            self.failed = True


def _validate_normalized_html(value: str) -> None:
    if (
        not value.strip()
        or "\x00" in value
        or "\ufffd" in value
        or _ABNORMAL_CONTROL_PATTERN.search(value)
        or value.startswith(("%PDF-", "\ud0cf\u11e0", "PK\x03\x04"))
        or _DANGEROUS_CSS_PATTERN.search(value)
    ):
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
    lowered_prefix = value.lstrip()[:128].lower()
    if lowered_prefix.startswith(("libreoffice", "soffice", "usage:")):
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
    parser = _StrictConvertedHTMLValidator()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID) from None
    if parser.failed or parser.has_unclosed_tags or not parser.has_complete_document:
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
    stripped = value.lstrip().lower()
    if not stripped.startswith(("<!doctype html", "<html")):
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)
    if "</html>" not in stripped:
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_OUTPUT_INVALID)


def _validate_artifact_record(artifact: object) -> RetrievedArtifact:
    _require_exact_record(artifact, RetrievedArtifact, "retrieved legacy artifact")
    if not isinstance(artifact.managed_path, Path):
        raise ValueError("managed legacy artifact path must be exact")
    if (
        type(artifact.byte_size) is not int
        or artifact.byte_size <= 0
        or artifact.byte_size > DockerLegacyDocConverter.MAX_INPUT_BYTES
    ):
        raise _conversion_error(DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT)
    if type(artifact.sha256) is not str or not _SHA256_PATTERN.fullmatch(artifact.sha256):
        raise ValueError("managed legacy artifact checksum is invalid")
    return artifact


def _validate_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ValueError("private legacy runtime directory is invalid")


def _remove_private_workspace(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return not os.path.lexists(path)
    except (OSError, ValueError):
        return False


def _read_stable_private_file(
    path: Path,
    *,
    max_bytes: int,
    require_mode: Optional[int] = 0o600,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
            or (require_mode is not None and stat.S_IMODE(before.st_mode) != require_mode)
        ):
            raise ValueError("private converter output is invalid")
        if before.st_size > max_bytes:
            raise _PrivateFileLimitError("private converter output exceeded its limit")
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise _PrivateFileLimitError("private converter output exceeded its limit")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise ValueError("private converter output changed during read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _validate_private_query_destination(output_path: Path, max_output_bytes: int) -> None:
    if (
        not isinstance(output_path, Path)
        or type(max_output_bytes) is not int
        or max_output_bytes <= 0
    ):
        raise ValueError("private metadata output contract is invalid")
    if output_path.exists() or output_path.is_symlink():
        raise ValueError("private metadata output must be new")
    _validate_private_directory(output_path.parent)


def _validate_metadata_query(argv: Tuple[str, ...]) -> None:
    _validate_argv(argv)
    image_query = (
        len(argv) == 6
        and argv[0] == str(_DOCKER_PATH)
        and argv[1:3] == ("image", "inspect")
        and _IMAGE_ID_PATTERN.fullmatch(argv[3]) is not None
        and argv[4:] == ("--format", _IMAGE_INSPECT_FORMAT)
    )
    container_state_query = (
        len(argv) == 6
        and argv[0] == str(_DOCKER_PATH)
        and argv[1:3] == ("container", "inspect")
        and _is_valid_container_target(argv[3])
        and argv[4:] == ("--format", _CONTAINER_STATE_FORMAT)
    )
    container_listing_query = (
        len(argv) == 9
        and argv[0] == str(_DOCKER_PATH)
        and argv[1:4] == ("container", "ls", "--all")
        and argv[4] == "--no-trunc"
        and argv[5] == "--filter"
        and _is_valid_container_filter(argv[6])
        and argv[7:] == ("--format", _CONTAINER_LIST_FORMAT)
    )
    if (
        argv != _STALE_CONTAINER_QUERY
        and not image_query
        and not container_state_query
        and not container_listing_query
    ):
        raise ValueError("Docker metadata query is not allowlisted")


def _container_state_query(target: str) -> Tuple[str, ...]:
    if not _is_valid_container_target(target):
        raise ValueError("container state query target is invalid")
    return (
        str(_DOCKER_PATH),
        "container",
        "inspect",
        target,
        "--format",
        _CONTAINER_STATE_FORMAT,
    )


def _container_listing_query(target: str) -> Tuple[str, ...]:
    if _CONTAINER_ID_PATTERN.fullmatch(target):
        filter_value = "id=" + target
    elif _CONTAINER_NAME_PATTERN.fullmatch(target):
        filter_value = "name=^/" + target + "$"
    else:
        raise ValueError("container listing query target is invalid")
    return (
        str(_DOCKER_PATH),
        "container",
        "ls",
        "--all",
        "--no-trunc",
        "--filter",
        filter_value,
        "--format",
        _CONTAINER_LIST_FORMAT,
    )


def _is_valid_container_target(value: str) -> bool:
    return bool(_CONTAINER_ID_PATTERN.fullmatch(value) or _CONTAINER_NAME_PATTERN.fullmatch(value))


def _is_valid_container_filter(value: str) -> bool:
    if value.startswith("id="):
        return _CONTAINER_ID_PATTERN.fullmatch(value[3:]) is not None
    prefix = "name=^/"
    return (
        value.startswith(prefix)
        and value.endswith("$")
        and _CONTAINER_NAME_PATTERN.fullmatch(value[len(prefix) : -1]) is not None
    )


def _validate_argv(argv: object) -> Tuple[str, ...]:
    if (
        type(argv) is not tuple
        or not argv
        or any(type(token) is not str or not token for token in argv)
    ):
        raise ValueError("Docker command argument vector is invalid")
    if "\x00" in "".join(argv):
        raise ValueError("Docker command argument vector is invalid")
    return argv


def _validate_timeout(value: object) -> int:
    if type(value) is not int or value <= 0 or value > 300:
        raise ValueError("Docker command timeout is invalid")
    return value


def _checked_result(result: CommandResult) -> CommandResult:
    result.validate()
    return result


def _unready_status(status: str) -> ConverterStatus:
    result = ConverterStatus(
        capability="research_only",
        status=status,
        reason_code=DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE.value,
        parser_version=None,
        provenance_checksum=None,
    )
    result.validate()
    return result


def _conversion_error(reason: DocumentFailureReason) -> LegacyDocConversionError:
    if reason in {
        DocumentFailureReason.LEGACY_CONVERTER_TIMEOUT,
        DocumentFailureReason.LEGACY_CONVERTER_RESOURCE_LIMIT,
    }:
        public_code = "official_document_resource_limit"
    else:
        public_code = "official_document_parse_failed"
    failure = SafeDocumentFailure(
        public_code=public_code,
        stage=DocumentFailureStage.CONVERSION,
        reason_code=reason,
    )
    failure.validate()
    return LegacyDocConversionError(failure)


def _require_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected state")
