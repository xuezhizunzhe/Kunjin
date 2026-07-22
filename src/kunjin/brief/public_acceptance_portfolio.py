from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from kunjin.brief.portfolio import (
    BoundedPortfolioService,
    PortfolioObservationResult,
)
from kunjin.brief.portfolio_worker_protocol import (
    PortfolioAccount,
    PortfolioObservationPayload,
    PortfolioPosition,
    PortfolioWorkerRequest,
    decode_portfolio_response,
    encode_portfolio_success,
)
from kunjin.decision.budget import RequestBudget
from kunjin.paths import RuntimePaths
from kunjin.services.sync import PortfolioSyncService
from kunjin.storage.repository import Repository

ACCEPTANCE_ATTESTATION_ENV = "KUNJIN_PHASE1_PUBLIC_FIXTURE_ATTESTATION"
ACCEPTANCE_FIXTURE_FD_ENV = "KUNJIN_PHASE1_PUBLIC_FIXTURE_FD"
ACCEPTANCE_MARKER_FD_ENV = "KUNJIN_PHASE1_PUBLIC_MARKER_FD"
ACCEPTANCE_RUN_ID_ENV = "KUNJIN_PHASE1_RUN_ID"
ACCEPTANCE_ATTESTATION = "synthetic_non_personal"
SYNTHETIC_OBSERVATION_VERSION = "synthetic_non_personal_v1"

_FIXTURE_CONTRACT = "kunjin_phase1_public_portfolio_v1"
_MARKER_CONTRACT = "kunjin_phase1_public_portfolio_used_v1"
_FIXTURE_KEYS = {"contract", "fund_code", "run_id", "schema_version"}
_FUND_CODE = re.compile(r"^[0-9]{6}$", re.ASCII)
_RUN_ID = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_MAX_FIXTURE_BYTES = 512
_CAPABILITY_TOKEN = object()


@dataclass(frozen=True)
class PublicAcceptanceCapability:
    fund_code: str
    run_id: str
    marker_fd: int

    def __post_init__(self) -> None:
        if type(self) is not PublicAcceptanceCapability:
            raise ValueError("public acceptance capability subclasses are not accepted")
        if _FUND_CODE.fullmatch(self.fund_code) is None:
            raise ValueError("public acceptance capability fund code is invalid")
        if _RUN_ID.fullmatch(self.run_id) is None:
            raise ValueError("public acceptance capability run id is invalid")
        if type(self.marker_fd) is not int or self.marker_fd < 3:
            raise ValueError("public acceptance marker descriptor is invalid")


def _descriptor(value: str, label: str) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"public acceptance {label} descriptor is invalid")
    descriptor = int(value)
    if descriptor < 3 or str(descriptor) != value:
        raise ValueError(f"public acceptance {label} descriptor is invalid")
    return descriptor


def _validate_ephemeral_paths(paths: RuntimePaths) -> None:
    if type(paths) is not RuntimePaths:
        raise ValueError("public acceptance runtime paths are invalid")
    data_dir = paths.database.parent
    state_dir = paths.logs.parent
    for directory, label in ((data_dir, "data"), (state_dir, "state")):
        metadata = directory.lstat()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ValueError(f"public acceptance {label} directory is unsafe")
    data_resolved = data_dir.resolve(strict=True)
    state_resolved = state_dir.resolve(strict=True)
    if data_resolved.parent != state_resolved.parent:
        raise ValueError("public acceptance directories do not share one case root")
    case_root = data_resolved.parent
    runtime_root = case_root.parent
    runtime_metadata = runtime_root.lstat()
    case_metadata = case_root.lstat()
    if (
        runtime_root.parent != Path("/private/tmp")
        or not runtime_root.name.startswith("kunjin-phase1-acceptance.")
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or not stat.S_ISDIR(case_metadata.st_mode)
        or runtime_metadata.st_uid != os.getuid()
        or case_metadata.st_uid != os.getuid()
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        or stat.S_IMODE(case_metadata.st_mode) != 0o700
    ):
        raise ValueError("public acceptance case root is not ephemeral")
    if paths.database.exists() or any(data_resolved.iterdir()) or any(state_resolved.iterdir()):
        raise ValueError("public acceptance directories must be new and empty")


def _read_anonymous_fixture(descriptor: int) -> bytes:
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 0
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or flags & os.O_ACCMODE != os.O_RDONLY
    ):
        raise ValueError("public acceptance fixture must be anonymous, owned, and read-only")
    chunks = []
    remaining = _MAX_FIXTURE_BYTES + 1
    while remaining > 0:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if not raw or len(raw) > _MAX_FIXTURE_BYTES or os.read(descriptor, 1):
        raise ValueError("public acceptance fixture exceeds its exact byte limit")
    return raw


def _validate_marker_descriptor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    if (
        not stat.S_ISFIFO(metadata.st_mode)
        or metadata.st_nlink != 0
        or flags & os.O_ACCMODE != os.O_WRONLY
    ):
        raise ValueError("public acceptance marker must be an anonymous write-only pipe")
    os.set_inheritable(descriptor, False)


def load_public_acceptance_capability(
    paths: RuntimePaths,
    expected_fund_code: Optional[str],
) -> Optional[PublicAcceptanceCapability]:
    values = {
        ACCEPTANCE_ATTESTATION_ENV: os.environ.get(ACCEPTANCE_ATTESTATION_ENV),
        ACCEPTANCE_FIXTURE_FD_ENV: os.environ.get(ACCEPTANCE_FIXTURE_FD_ENV),
        ACCEPTANCE_MARKER_FD_ENV: os.environ.get(ACCEPTANCE_MARKER_FD_ENV),
        ACCEPTANCE_RUN_ID_ENV: os.environ.get(ACCEPTANCE_RUN_ID_ENV),
    }
    activation_values = (
        values[ACCEPTANCE_ATTESTATION_ENV],
        values[ACCEPTANCE_FIXTURE_FD_ENV],
        values[ACCEPTANCE_MARKER_FD_ENV],
    )
    if all(value is None for value in activation_values):
        return None
    if any(value is None for value in values.values()):
        raise ValueError("public acceptance capability markers are incomplete")
    if (
        type(expected_fund_code) is not str
        or _FUND_CODE.fullmatch(expected_fund_code) is None
        or values[ACCEPTANCE_ATTESTATION_ENV] != ACCEPTANCE_ATTESTATION
    ):
        raise ValueError("public acceptance capability subject is invalid")

    fixture_fd = _descriptor(values[ACCEPTANCE_FIXTURE_FD_ENV], "fixture")
    marker_fd = _descriptor(values[ACCEPTANCE_MARKER_FD_ENV], "marker")
    if fixture_fd == marker_fd:
        raise ValueError("public acceptance capability descriptors must differ")
    _validate_ephemeral_paths(paths)
    try:
        raw = _read_anonymous_fixture(fixture_fd)
    finally:
        os.close(fixture_fd)
    _validate_marker_descriptor(marker_fd)
    try:
        fixture = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("public acceptance fixture is not canonical JSON") from None
    if type(fixture) is not dict or set(fixture) != _FIXTURE_KEYS:
        raise ValueError("public acceptance fixture keys are invalid")
    canonical = json.dumps(
        fixture,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if canonical != raw:
        raise ValueError("public acceptance fixture is not canonical")
    run_id = values[ACCEPTANCE_RUN_ID_ENV]
    if (
        fixture["schema_version"] != 1
        or fixture["contract"] != _FIXTURE_CONTRACT
        or fixture["fund_code"] != expected_fund_code
        or fixture["run_id"] != run_id
        or _RUN_ID.fullmatch(run_id) is None
    ):
        raise ValueError("public acceptance fixture identity is invalid")
    for key in (
        ACCEPTANCE_ATTESTATION_ENV,
        ACCEPTANCE_FIXTURE_FD_ENV,
        ACCEPTANCE_MARKER_FD_ENV,
    ):
        os.environ.pop(key, None)
    return PublicAcceptanceCapability(expected_fund_code, run_id, marker_fd)


class _SyntheticWorker:
    def __init__(self, capability: PublicAcceptanceCapability) -> None:
        self._capability = capability
        self._used = False
        self._payload_sha256: Optional[str] = None
        self._request_id: Optional[str] = None

    def __call__(
        self,
        request: PortfolioWorkerRequest,
        budget: RequestBudget,
    ):
        if self._used:
            raise ValueError("public acceptance capability is single-use")
        if type(request) is not PortfolioWorkerRequest or type(budget) is not RequestBudget:
            raise ValueError("public acceptance worker binding is invalid")
        request.validate()
        if request.request_id != budget.request_id:
            raise ValueError("public acceptance worker request does not match its budget")
        self._used = True
        observed_at = budget.started_at
        accounts = (
            PortfolioAccount("synthetic-account-1", "SYNTHETIC_NON_PERSONAL_1", observed_at),
            PortfolioAccount("synthetic-account-2", "SYNTHETIC_NON_PERSONAL_2", observed_at),
        )
        positions = tuple(
            PortfolioPosition(
                account.source_account_id,
                self._capability.fund_code,
                "SYNTHETIC_NON_PERSONAL_FUND",
                None,
                str(index),
                "1",
                None,
                None,
                observed_at,
            )
            for index, account in enumerate(accounts, start=1)
        )
        payload = PortfolioObservationPayload(observed_at, accounts, positions)
        encoded = encode_portfolio_success(
            request,
            payload,
            keychain_read_count=0,
            keychain_mutation_attempt_count=0,
        )
        response = decode_portfolio_response(encoded, request)
        self._payload_sha256 = hashlib.sha256(encoded).hexdigest()
        self._request_id = request.request_id
        return response

    def publish_marker(self, result: PortfolioObservationResult) -> None:
        if (
            type(result) is not PortfolioObservationResult
            or type(result.source_attempt_id) is not int
            or result.source_attempt_id <= 0
            or self._payload_sha256 is None
            or self._request_id is None
        ):
            raise ValueError("public acceptance result cannot publish a marker")
        marker = json.dumps(
            {
                "contract": _MARKER_CONTRACT,
                "fund_code": self._capability.fund_code,
                "observation_version": SYNTHETIC_OBSERVATION_VERSION,
                "payload_sha256": self._payload_sha256,
                "request_id": self._request_id,
                "run_id": self._capability.run_id,
                "schema_version": 1,
                "source_attempt_id": result.source_attempt_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        try:
            if os.write(self._capability.marker_fd, marker) != len(marker):
                raise ValueError("public acceptance marker write was incomplete")
        finally:
            os.close(self._capability.marker_fd)


class PublicAcceptancePortfolioService(BoundedPortfolioService):
    def __init__(
        self,
        repository: Repository,
        sync_service: PortfolioSyncService,
        capability: PublicAcceptanceCapability,
        *,
        _token: object,
    ) -> None:
        if _token is not _CAPABILITY_TOKEN or type(capability) is not PublicAcceptanceCapability:
            raise ValueError("public acceptance portfolio service requires an exact capability")
        self._acceptance_fund_code = capability.fund_code
        worker = _SyntheticWorker(capability)
        self._acceptance_worker = worker
        super().__init__(
            repository,
            worker_runner=worker,
            sync_service=sync_service,
        )

    def sync(self, fund_code, context) -> PortfolioObservationResult:
        if fund_code != self._acceptance_fund_code:
            raise ValueError("public acceptance portfolio subject changed")
        result = super().sync(fund_code, context)
        if type(result) is not PortfolioObservationResult or result.status != "success":
            raise ValueError("public acceptance portfolio did not produce a current result")
        binding = replace(
            result.portfolio_binding,
            observation_version=SYNTHETIC_OBSERVATION_VERSION,
        )
        binding.validate()
        projected = replace(result, portfolio_binding=binding)
        self._acceptance_worker.publish_marker(projected)
        return projected


def build_public_acceptance_portfolio_service(
    repository: Repository,
    sync_service: PortfolioSyncService,
    capability: PublicAcceptanceCapability,
) -> PublicAcceptancePortfolioService:
    return PublicAcceptancePortfolioService(
        repository,
        sync_service,
        capability,
        _token=_CAPABILITY_TOKEN,
    )
