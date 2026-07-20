from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Sequence

from kunjin.decision.models import ActionKind

MAX_SUMMARY_BYTES = 32_768
MAX_PROBE_OUTPUT_BYTES = 1_048_576
PREVIEW_COUNTS = {
    "brief_calls": 1,
    "intelligence_calls": 1,
    "match_projection_calls": 1,
    "adjudication_calls": 0,
    "holding_review_calls": 1,
    "network_retries": 0,
}

FAULT_CASES = (
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
)

_PROBE_NODES = {
    "fund_binding_mismatch": (
        "tests/unit/test_holding_review_service.py::"
        "test_wrong_fund_or_action_fails_closed"
    ),
    "snapshot_corruption": (
        "tests/unit/test_holding_review_store.py::"
        "test_legacy_drifted_projection_fails_on_every_decision_read_path"
    ),
    "brief_snapshot_missing": (
        "tests/unit/test_phase5_acceptance.py::"
        "test_core_brief_snapshot_omission_is_transient_and_not_persisted"
    ),
    "intelligence_snapshot_missing": (
        "tests/unit/test_holding_review_service.py::"
        "test_missing_exact_snapshot_is_transient_and_not_persisted"
    ),
    "thesis_missing": (
        "tests/unit/test_phase5_acceptance.py::"
        "test_core_thesis_omission_runs_authenticated_abstaining_chain"
    ),
    "official_confirmation_missing": (
        "tests/unit/test_holding_review_research.py::"
        "test_preview_explains_official_gap_without_absence_claim"
    ),
    "redemption_evidence_missing": (
        "tests/unit/test_holding_review_engine.py::"
        "test_redemption_restriction_with_incomplete_component_is_stable_insufficient_data"
    ),
    "tier_two_only": (
        "tests/unit/test_phase5_acceptance.py::test_tier_two_only_probe_is_insufficient"
    ),
    "same_lineage_reprint": (
        "tests/unit/test_phase5_acceptance.py::"
        "test_same_lineage_reprint_probe_is_insufficient"
    ),
    "source_failed": (
        "tests/unit/test_holding_review_engine.py::"
        "test_source_failure_cannot_be_described_as_unchanged"
    ),
    "coverage_reduced": (
        "tests/unit/test_holding_review_engine.py::test_coverage_loss_never_claims_unchanged"
    ),
    "stale_adjudication": (
        "tests/unit/test_holding_review_store.py::"
        "test_review_rejects_superseded_adjudication"
    ),
    "history_corruption": (
        "tests/unit/test_holding_review_engine.py::"
        "test_requested_untrusted_history_blocks_continue_observing"
    ),
    "repeated_request": (
        "tests/unit/test_holding_review_store.py::"
        "test_review_round_trip_previous_binding_and_privacy"
    ),
    "privacy_shape": (
        "tests/unit/test_phase5_acceptance.py::"
        "test_strict_local_schema_rejects_shape_type_and_fixed_value_drift"
    ),
    "interrupt_cleanup": (
        "tests/test_smoke.py::SmokeTest::"
        "test_phase5_acceptance_interrupt_cleans_process_group"
    ),
    "unexpected_exit": (
        "tests/test_smoke.py::SmokeTest::"
        "test_phase5_acceptance_rejects_unexpected_child_exit"
    ),
}
_LOCAL_PROBE_NODE = (
    "tests/unit/test_phase5_acceptance.py::"
    "test_local_preview_runs_authenticated_chain_once_without_network"
)
_CORE_GAPS = frozenset(
    {
        "brief_snapshot_missing",
        "intelligence_snapshot_missing",
        "thesis_missing",
        "official_confirmation_missing",
        "redemption_evidence_missing",
    }
)
_LOCAL_KEYS = frozenset(
    {
        "acceptance_scope",
        "action_authorized",
        "automatic_trade",
        "counts",
        "exact_amount_available",
        "gap_codes",
        "mode",
        "network_retries",
        "observation",
        "observed_faults",
        "official_negative_check_complete",
        "outcome",
        "review_disposition",
        "review_maturity",
        "sell_timing",
    }
)
_FAULT_KEYS = frozenset(
    {
        "acceptance_scope",
        "action_authorized",
        "automatic_trade",
        "case_count",
        "exact_amount_available",
        "fault_cases",
        "mode",
        "network_retries",
        "observations",
        "official_negative_check_complete",
        "outcome",
        "review_disposition",
        "review_maturity",
        "sell_timing",
    }
)
_PRIVATE_MODE_KEYS = frozenset(
    {
        "action_authorized",
        "automatic_trade",
        "conditional_review_usability",
        "counts",
        "engineering_flow",
        "evidence_readiness",
        "exact_amount_available",
        "history_comparability",
        "mode",
        "redemption_feasibility",
        "sell_timing",
        "thesis_review_readiness",
    }
)
_OBSERVATION_KEYS = frozenset(
    {"case", "evidence_checksum", "probe_kind", "status"}
)
_PRIVATE_KEYS = re.compile(
    r'"(?:amount|cost|current_value|email|fund_code|income|nav|path|profile|shares|token)"\s*:',
    re.IGNORECASE,
)
_SIX_DIGIT_CODE = re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")
_PRIVATE_PATH = re.compile(r"/(?:Users|home|private|var/folders)/", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PRIVATE_SENTINEL = re.compile(r"(?:OWNER|ENGINEERING)_PRIVATE_SENTINEL", re.IGNORECASE)
_LONG_SECRET = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{42,}={0,2}(?![A-Za-z0-9_-])")
_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FUND_CODE = re.compile(r"[0-9]{6}")
_PRIVATE_ACTIONS = frozenset(
    {
        ActionKind.CONTINUE_HOLDING,
        ActionKind.REDUCE_TO_CASH,
        ActionKind.FULL_EXIT,
    }
)
_PRIVATE_CALL_SEQUENCE = (
    "brief_calls",
    "intelligence_calls",
    "match_projection_calls",
    "holding_review_calls",
)


@dataclass(frozen=True)
class AcceptanceFixture:
    mode: str
    observed_faults: tuple[str, ...]
    gap_codes: tuple[str, ...]
    review_disposition: str
    outcome: str


@dataclass(frozen=True)
class PrivateSubject:
    fund_code: str
    action: ActionKind


@dataclass(frozen=True)
class PrivateAcceptanceFixture:
    mode: str
    evidence_readiness: str
    history_comparability: str
    thesis_review_readiness: str
    conditional_review_usability: str
    redemption_feasibility: str
    review_disposition: str


@dataclass
class PrivateCallLedger:
    calls: list[str] = field(default_factory=list)

    def record(self, call: str) -> None:
        if (
            type(call) is not str
            or len(self.calls) >= len(_PRIVATE_CALL_SEQUENCE)
            or call != _PRIVATE_CALL_SEQUENCE[len(self.calls)]
        ):
            raise ValueError("private acceptance call sequence invalid")
        self.calls.append(call)

    def counts(self) -> dict[str, int]:
        if tuple(self.calls) != _PRIVATE_CALL_SEQUENCE:
            raise ValueError("private acceptance call sequence incomplete")
        return {
            "brief_calls": self.calls.count("brief_calls"),
            "intelligence_calls": self.calls.count("intelligence_calls"),
            "match_projection_calls": self.calls.count("match_projection_calls"),
            "adjudication_calls": 0,
            "holding_review_calls": self.calls.count("holding_review_calls"),
            "network_retries": 0,
        }


@dataclass(frozen=True)
class PrivateChainResult:
    review: Mapping[str, object]
    counts: Mapping[str, int]
    projection_id: int


def build_acceptance_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="phase5_acceptance.py")
    parser.add_argument("mode", choices=("local", "fault", "engineering", "owner"))
    return parser


def parse_mode(parser: argparse.ArgumentParser, value: str) -> str:
    if not isinstance(parser, argparse.ArgumentParser):
        raise ValueError("acceptance parser invalid")
    return str(parser.parse_args([value]).mode)


def local_fixture() -> AcceptanceFixture:
    return AcceptanceFixture(
        mode="local",
        observed_faults=(),
        gap_codes=("insufficient_data", "official_confirmation_required"),
        review_disposition="abstain",
        outcome="accepted_preview",
    )


def fault_fixture(case: str) -> AcceptanceFixture:
    if type(case) is not str or case not in FAULT_CASES:
        raise ValueError("acceptance fault case invalid")
    gaps = {"insufficient_data", "official_confirmation_required", case}
    if case in _CORE_GAPS:
        gaps.add(case)
    outcomes = {
        "repeated_request": "history_bound_preview",
        "interrupt_cleanup": "interrupted_cleanly",
        "unexpected_exit": "child_failure_rejected",
    }
    return AcceptanceFixture(
        mode="fault",
        observed_faults=(case,),
        gap_codes=tuple(sorted(gaps)),
        review_disposition=(
            "manual_thesis_review_required"
            if case == "stale_adjudication"
            else "abstain"
        ),
        outcome=outcomes.get(case, "fail_closed"),
    )


def possible_match_fixture(*, mode: str = "owner") -> PrivateAcceptanceFixture:
    return PrivateAcceptanceFixture(
        mode=mode,
        evidence_readiness="partial",
        history_comparability="not_available",
        thesis_review_readiness="manual_review_required",
        conditional_review_usability="partial",
        redemption_feasibility="not_requested",
        review_disposition="manual_thesis_review_required",
    )


def owner_acceptance(value: PrivateAcceptanceFixture) -> dict[str, object]:
    if type(value) is not PrivateAcceptanceFixture or value.mode != "owner":
        raise ValueError("owner acceptance fixture invalid")
    ledger = PrivateCallLedger()
    for call in _PRIVATE_CALL_SEQUENCE:
        ledger.record(call)
    return _private_summary(value, ledger.counts(), adjudication_unchanged=True)


def _private_summary(
    value: PrivateAcceptanceFixture,
    counts: Mapping[str, int],
    *,
    adjudication_unchanged: bool,
) -> dict[str, object]:
    if dict(counts) != PREVIEW_COUNTS or adjudication_unchanged is not True:
        raise ValueError("private acceptance flow incomplete")
    summary = {
        "action_authorized": False,
        "automatic_trade": False,
        "conditional_review_usability": value.conditional_review_usability,
        "counts": dict(counts),
        "engineering_flow": "pass",
        "evidence_readiness": value.evidence_readiness,
        "exact_amount_available": False,
        "history_comparability": value.history_comparability,
        "mode": value.mode,
        "redemption_feasibility": value.redemption_feasibility,
        "sell_timing": "insufficient_data",
        "thesis_review_readiness": value.thesis_review_readiness,
    }
    validate_summary(summary, expected_mode=value.mode)
    return summary


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def secure_read_private_subject(
    path: Path,
    *,
    excluded_roots: Sequence[Path],
) -> PrivateSubject:
    failure = "private subject file invalid"
    if type(path) is not type(Path()) or not path.is_absolute():
        raise ValueError(failure)
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = os.lstat(parent)
    except (OSError, RuntimeError):
        raise ValueError(failure) from None
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or stat.S_IMODE(parent_stat.st_mode) != 0o700
        or parent_stat.st_uid != os.getuid()
    ):
        raise ValueError(failure)
    try:
        initial = os.lstat(path)
    except OSError:
        raise ValueError(failure) from None
    if not stat.S_ISREG(initial.st_mode) or stat.S_ISLNK(initial.st_mode):
        raise ValueError(failure)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError(failure)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise ValueError(failure) from None
    try:
        metadata = os.fstat(descriptor)
        current = os.lstat(path)
        resolved = path.resolve(strict=True)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or stat.S_ISLNK(current.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (current.st_dev, current.st_ino)
            or any(_under(resolved, root.resolve(strict=False)) for root in excluded_roots)
        ):
            raise ValueError(failure)
        raw = os.read(descriptor, 16_385)
    finally:
        os.close(descriptor)
    if len(raw) > 16_384:
        raise ValueError(failure)
    try:
        def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(failure)
                result[key] = value
            return result

        payload = json.loads(raw.decode("ascii"), object_pairs_hook=strict_object)
    except (UnicodeError, ValueError, TypeError):
        raise ValueError(failure) from None
    if (
        type(payload) is not dict
        or set(payload) != {"fund_code", "action"}
        or any((ancestor / ".git").exists() for ancestor in (resolved.parent, *resolved.parents))
    ):
        raise ValueError(failure)
    code = payload["fund_code"]
    action_value = payload["action"]
    try:
        action = ActionKind(action_value)
    except (TypeError, ValueError):
        raise ValueError(failure) from None
    if (
        type(code) is not str
        or _FUND_CODE.fullmatch(code) is None
        or code == "000000"
        or action not in _PRIVATE_ACTIONS
    ):
        raise ValueError(failure)
    return PrivateSubject(code, action)


def private_subject_path(mode: str, environ: Mapping[str, str]) -> Path:
    if mode not in {"engineering", "owner"}:
        raise ValueError("private mode invalid")
    engineering = environ.get("KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE")
    owner = environ.get("KUNJIN_PHASE5_OWNER_SUBJECT_FILE")
    if engineering and owner:
        raise ValueError("engineering and owner subject files must be separate")
    selected = engineering if mode == "engineering" else owner
    if not selected or (mode == "engineering" and owner) or (mode == "owner" and engineering):
        raise ValueError("private subject file invalid")
    return Path(selected)


class NoExternalOperations(AbstractContextManager):
    def __init__(self) -> None:
        self._originals: list[tuple[object, str, object]] = []

    @staticmethod
    def _deny(*_args, **_kwargs):
        raise OSError("private acceptance external operation prohibited")

    def _patch(self, owner: object, name: str) -> None:
        if hasattr(owner, name):
            original = getattr(owner, name)
            self._originals.append((owner, name, original))
            setattr(owner, name, self._deny)

    def __enter__(self):
        for name in ("create_connection", "getaddrinfo"):
            self._patch(socket, name)
        for name in ("connect", "connect_ex"):
            self._patch(socket.socket, name)
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            self._patch(subprocess, name)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        while self._originals:
            owner, name, original = self._originals.pop()
            setattr(owner, name, original)
        return False


def _placeholder_observation(case: str) -> dict[str, object]:
    return {
        "case": case,
        "evidence_checksum": hashlib.sha256(case.encode("ascii")).hexdigest(),
        "probe_kind": "pytest",
        "status": "verified",
    }


def project_acceptance(
    value: AcceptanceFixture,
    *,
    observation: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    if type(value) is not AcceptanceFixture:
        raise ValueError("acceptance fixture invalid")
    if value.mode not in {"local", "fault"}:
        raise ValueError("acceptance mode invalid")
    if value.review_disposition not in {
        "abstain",
        "manual_thesis_review_required",
    }:
        raise ValueError("acceptance disposition invalid")
    if value.mode == "local" and value.observed_faults:
        raise ValueError("local acceptance faults invalid")
    if value.mode == "fault" and (
        len(value.observed_faults) != 1 or value.observed_faults[0] not in FAULT_CASES
    ):
        raise ValueError("fault acceptance inventory invalid")
    projected = {
        "acceptance_scope": "synthetic_local_preview_only",
        "action_authorized": False,
        "automatic_trade": False,
        "counts": dict(PREVIEW_COUNTS),
        "exact_amount_available": False,
        "gap_codes": list(value.gap_codes),
        "mode": value.mode,
        "network_retries": 0,
        "observation": dict(
            observation or _placeholder_observation("local_authenticated_chain")
        ),
        "observed_faults": list(value.observed_faults),
        "official_negative_check_complete": False,
        "outcome": value.outcome,
        "review_disposition": value.review_disposition,
        "review_maturity": "evidence_only",
        "sell_timing": "insufficient_data",
    }
    if value.mode == "local":
        validate_summary(projected, expected_mode="local")
    return projected


def _validate_exact_bool(value: object) -> None:
    if type(value) is not bool:
        raise ValueError("acceptance output invalid")


def _validate_exact_int(value: object, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise ValueError("acceptance output invalid")


def _validate_identifier_list(value: object, expected: Sequence[str]) -> None:
    if type(value) is not list or value != list(expected):
        raise ValueError("acceptance output invalid")
    if any(type(item) is not str or _IDENTIFIER.fullmatch(item) is None for item in value):
        raise ValueError("acceptance output invalid")


def _validate_observation(value: object, expected_case: str) -> None:
    if type(value) is not dict or set(value) != _OBSERVATION_KEYS:
        raise ValueError("acceptance output invalid")
    if value.get("case") != expected_case:
        raise ValueError("acceptance output invalid")
    if value.get("probe_kind") != "pytest" or value.get("status") != "verified":
        raise ValueError("acceptance output invalid")
    checksum = value.get("evidence_checksum")
    if type(checksum) is not str or _CHECKSUM.fullmatch(checksum) is None:
        raise ValueError("acceptance output invalid")


def validate_summary(value: object, *, expected_mode: str) -> dict[str, object]:
    if expected_mode not in {"local", "fault", "engineering", "owner"} or type(
        value
    ) is not dict:
        raise ValueError("acceptance output invalid")
    expected_keys = (
        _LOCAL_KEYS
        if expected_mode == "local"
        else _FAULT_KEYS
        if expected_mode == "fault"
        else _PRIVATE_MODE_KEYS
    )
    if set(value) != expected_keys:
        raise ValueError("acceptance output invalid")
    for key in (
        "action_authorized",
        "automatic_trade",
        "exact_amount_available",
    ):
        _validate_exact_bool(value[key])
        if value[key] is not False:
            raise ValueError("acceptance output invalid")
    if expected_mode in {"local", "fault"}:
        _validate_exact_bool(value["official_negative_check_complete"])
        if value["official_negative_check_complete"] is not False:
            raise ValueError("acceptance output invalid")
        _validate_exact_int(value["network_retries"], 0)
    fixed = {"mode": expected_mode, "sell_timing": "insufficient_data"}
    if expected_mode in {"local", "fault"}:
        fixed["review_maturity"] = "evidence_only"
    if any(value[key] != expected for key, expected in fixed.items()):
        raise ValueError("acceptance output invalid")

    if expected_mode in {"engineering", "owner"}:
        if (
            value["engineering_flow"] not in {"pass", "failed"}
            or value["evidence_readiness"]
            not in {"ready", "partial", "insufficient_data"}
            or value["history_comparability"]
            not in {"comparable", "not_comparable", "not_available"}
            or value["thesis_review_readiness"]
            not in {"ready", "manual_review_required", "missing", "insufficient_data"}
            or value["conditional_review_usability"]
            not in {"observed_for_request", "partial", "not_testable"}
            or value["redemption_feasibility"]
            not in {
                "not_requested",
                "insufficient_data",
                "restricted",
                "evidence_complete_non_authorizing",
            }
        ):
            raise ValueError("acceptance output invalid")
        counts = value["counts"]
        if type(counts) is not dict or set(counts) != set(PREVIEW_COUNTS):
            raise ValueError("acceptance output invalid")
        for key, expected in PREVIEW_COUNTS.items():
            _validate_exact_int(counts[key], expected)
    elif expected_mode == "local":
        if value["review_disposition"] != "abstain":
            raise ValueError("acceptance output invalid")
        if (
            value["acceptance_scope"] != "synthetic_local_preview_only"
            or value["outcome"] != "accepted_preview"
        ):
            raise ValueError("acceptance output invalid")
        counts = value["counts"]
        if type(counts) is not dict or set(counts) != set(PREVIEW_COUNTS):
            raise ValueError("acceptance output invalid")
        for key, expected in PREVIEW_COUNTS.items():
            _validate_exact_int(counts[key], expected)
        _validate_identifier_list(
            value["gap_codes"],
            ("insufficient_data", "official_confirmation_required"),
        )
        _validate_identifier_list(value["observed_faults"], ())
        _validate_observation(value["observation"], "local_authenticated_chain")
    else:
        if value["review_disposition"] != "abstain":
            raise ValueError("acceptance output invalid")
        if (
            value["acceptance_scope"] != "synthetic_local_faults_only"
            or value["outcome"] != "fault_contract_verified"
        ):
            raise ValueError("acceptance output invalid")
        _validate_exact_int(value["case_count"], len(FAULT_CASES))
        _validate_identifier_list(value["fault_cases"], FAULT_CASES)
        observations = value["observations"]
        if type(observations) is not list or len(observations) != len(FAULT_CASES):
            raise ValueError("acceptance output invalid")
        for observation, case in zip(observations, FAULT_CASES):
            _validate_observation(observation, case)
    return value


def _privacy_scan(encoded: str) -> None:
    if any(
        pattern.search(encoded)
        for pattern in (
            _PRIVATE_KEYS,
            _SIX_DIGIT_CODE,
            _PRIVATE_PATH,
            _EMAIL,
            _PRIVATE_SENTINEL,
            _LONG_SECRET,
        )
    ):
        raise ValueError("acceptance output invalid")


def _strict_json(encoded: str) -> dict[str, object]:
    def object_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("acceptance output invalid")
            value[key] = item
        return value

    def reject_constant(_value: str) -> None:
        raise ValueError("acceptance output invalid")

    try:
        payload = json.loads(
            encoded,
            object_pairs_hook=object_hook,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError):
        raise ValueError("acceptance output invalid") from None
    if type(payload) is not dict:
        raise ValueError("acceptance output invalid")
    return payload


def _privacy_projection(payload: dict[str, object]) -> str:
    projected = json.loads(json.dumps(payload, ensure_ascii=True))
    if payload.get("mode") == "local":
        projected["observation"]["evidence_checksum"] = "validated_checksum"
    elif payload.get("mode") == "fault":
        for observation in projected["observations"]:
            observation["evidence_checksum"] = "validated_checksum"
    return json.dumps(projected, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sanitize_encoded_output(encoded: str, *, expected_mode: Optional[str] = None) -> str:
    if type(encoded) is not str or not encoded or "Traceback" in encoded:
        raise ValueError("acceptance output invalid")
    if len(encoded.encode("utf-8")) > MAX_SUMMARY_BYTES:
        raise ValueError("acceptance output invalid")
    payload = _strict_json(encoded)
    mode = payload.get("mode")
    if expected_mode is not None and mode != expected_mode:
        raise ValueError("acceptance output invalid")
    validate_summary(payload, expected_mode=str(mode))
    _privacy_scan(_privacy_projection(payload))
    return encoded


def encode_summary(summary: Mapping[str, object]) -> str:
    if type(summary) is not dict:
        raise ValueError("acceptance summary invalid")
    encoded = json.dumps(
        summary,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sanitize_encoded_output(encoded, expected_mode=str(summary.get("mode")))


def _run_pytest_probe(
    case: str,
    node: str,
    runtime_dir: Path,
    *,
    repository_root: Path,
    python: Path,
) -> dict[str, object]:
    case_dir = runtime_dir / case
    case_dir.mkdir(mode=0o700)
    environment = {
        **os.environ,
        "KUNJIN_DATA_DIR": str(case_dir / "data"),
        "KUNJIN_STATE_DIR": str(case_dir / "state"),
        "PYTHONPYCACHEPREFIX": str(case_dir / "pycache"),
    }
    completed = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            "-q",
            "--basetemp",
            str(case_dir / "pytest"),
            node,
        ],
        cwd=repository_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        check=False,
    )
    captured = completed.stdout + completed.stderr
    if completed.returncode != 0 or len(captured) > MAX_PROBE_OUTPUT_BYTES:
        raise ValueError("acceptance probe failed")
    digest = hashlib.sha256(node.encode("ascii") + b"\0" + captured).hexdigest()
    return {
        "case": case,
        "evidence_checksum": digest,
        "probe_kind": "pytest",
        "status": "verified",
    }


def run_fault_probe(
    case: str,
    runtime_dir: Path,
    *,
    repository_root: Optional[Path] = None,
    python: Optional[Path] = None,
) -> dict[str, object]:
    if type(case) is not str or case not in FAULT_CASES:
        raise ValueError("acceptance fault case invalid")
    if type(runtime_dir) is not type(Path()) or not runtime_dir.is_dir():
        raise ValueError("acceptance runtime invalid")
    root = repository_root or Path(__file__).resolve().parents[1]
    interpreter = python or Path(sys.executable)
    return _run_pytest_probe(
        case,
        _PROBE_NODES[case],
        runtime_dir,
        repository_root=root,
        python=interpreter,
    )


def _run_local_probe(runtime_dir: Path) -> dict[str, object]:
    return _run_pytest_probe(
        "local_authenticated_chain",
        _LOCAL_PROBE_NODE,
        runtime_dir,
        repository_root=Path(__file__).resolve().parents[1],
        python=Path(sys.executable),
    )


def _private_cli_call(
    cli,
    context,
    argv: list[str],
    expected: str,
    *,
    ledger: PrivateCallLedger,
    call: str,
) -> dict[str, object]:
    ledger.record(call)
    payload, exit_code, json_output = cli.run(["--json", *argv], context)
    if (
        not json_output
        or exit_code != 0
        or type(payload) is not dict
        or payload.get("command") != expected
        or type(payload.get("data")) is not dict
    ):
        raise ValueError("private acceptance command failed")
    return payload["data"]


def _private_thesis_readiness(state: object) -> str:
    if state in {"manual_review_pending", "manual_review_uncertain"}:
        return "manual_review_required"
    if state == "thesis_missing":
        return "missing"
    if state in {
        "no_matching_evidence",
        "presented_match_confirmed",
        "presented_match_rejected",
    }:
        return "ready"
    return "insufficient_data"


def _private_summary_from_review(
    mode: str,
    subject: PrivateSubject,
    chain: PrivateChainResult,
    *,
    adjudication_unchanged: bool,
) -> dict[str, object]:
    review = chain.review
    interpretation = review.get("interpretation")
    boundary = review.get("review_boundary")
    evidence_delta = review.get("evidence_delta")
    redemption = review.get("redemption")
    if (
        type(interpretation) is not dict
        or review.get("flow_status") not in {"complete", "partial"}
        or review.get("fund_code") != subject.fund_code
        or review.get("action") != subject.action.value
        or boundary
        != {
            "action_authorized": False,
            "automatic_trade": False,
            "exact_amount_available": False,
            "review_maturity": "evidence_only",
        }
    ):
        raise ValueError("private acceptance review invalid")
    candidate_match = review.get("candidate_thesis_match")
    if (
        type(candidate_match) is not dict
        or candidate_match.get("projection_id") != chain.projection_id
    ):
        raise ValueError("private acceptance projection binding failed")
    evidence_readiness = review.get("evidence_readiness", "insufficient_data")
    history = (
        evidence_delta.get("history_comparability", "not_available")
        if type(evidence_delta) is dict
        else "not_available"
    )
    redemption_state = (
        redemption.get("feasibility", "insufficient_data")
        if type(redemption) is dict
        else "insufficient_data"
    )
    disposition = interpretation.get("review_disposition")
    thesis_readiness = _private_thesis_readiness(
        interpretation.get("thesis_review_state")
    )
    conditional = (
        "observed_for_request"
        if evidence_readiness == "ready"
        and disposition in {"continue_observing", "reduce_review", "exit_review"}
        else "partial"
        if review.get("flow_status") in {"complete", "partial"}
        else "not_testable"
    )
    summary = {
        "action_authorized": False,
        "automatic_trade": False,
        "conditional_review_usability": conditional,
        "counts": dict(chain.counts),
        "engineering_flow": (
            "pass"
            if dict(chain.counts) == PREVIEW_COUNTS and adjudication_unchanged is True
            else "failed"
        ),
        "evidence_readiness": evidence_readiness,
        "exact_amount_available": False,
        "history_comparability": history,
        "mode": mode,
        "redemption_feasibility": redemption_state,
        "sell_timing": review.get("sell_timing"),
        "thesis_review_readiness": thesis_readiness,
    }
    if summary["engineering_flow"] != "pass":
        raise ValueError("private acceptance flow incomplete")
    validate_summary(summary, expected_mode=mode)
    return summary


def _local_snapshot_holds(context, fund_code: str) -> bool:
    return any(
        item.fund_code == fund_code and item.shares > 0
        for item in context.repository.latest_positions()
    )


def _run_private_chain(
    mode: str, subject: PrivateSubject, key: bytes
) -> PrivateChainResult:
    from scripts import phase41_acceptance as phase41

    cli, context = phase41._build_context_with_key(key)
    if mode == "owner" and not _local_snapshot_holds(context, subject.fund_code):
        raise ValueError("owner subject is not held in the latest local snapshot")

    def offline_portfolio(*_args, **_kwargs):
        raise OSError("private acceptance portfolio refresh prohibited")

    context.brief_service._portfolio_service.sync = offline_portfolio
    ledger = PrivateCallLedger()
    brief = _private_cli_call(
        cli,
        context,
        [
            "fund",
            "brief",
            subject.fund_code,
            "--action",
            subject.action.value,
            "--mode",
            "rapid",
        ],
        "fund.brief",
        ledger=ledger,
        call="brief_calls",
    )
    intelligence = _private_cli_call(
        cli,
        context,
        ["fund", "intelligence", subject.fund_code, "--mode", "rapid"],
        "fund.intelligence",
        ledger=ledger,
        call="intelligence_calls",
    )
    brief_request = brief.get("request")
    intelligence_request = intelligence.get("request")
    if type(brief_request) is not dict or type(intelligence_request) is not dict:
        raise ValueError("private acceptance request binding failed")
    brief_id = brief_request.get("request_run_id")
    intelligence_id = intelligence_request.get("request_run_id")
    if (
        type(brief_id) is not int
        or brief_id <= 0
        or type(intelligence_id) is not int
        or intelligence_id <= 0
    ):
        raise ValueError("private acceptance request binding failed")
    projection = _private_cli_call(
        cli,
        context,
        [
            "thesis",
            "match-project",
            subject.fund_code,
            "--intelligence-request-run-id",
            str(intelligence_id),
        ],
        "thesis.match-project",
        ledger=ledger,
        call="match_projection_calls",
    )
    projection_id = projection.get("id")
    if type(projection_id) is not int or projection_id <= 0:
        raise ValueError("private acceptance projection binding failed")
    review = _private_cli_call(
        cli,
        context,
        [
            "fund",
            "holding-review",
            subject.fund_code,
            "--action",
            subject.action.value,
            "--brief-request-run-id",
            str(brief_id),
            "--intelligence-request-run-id",
            str(intelligence_id),
        ],
        "fund.holding-review",
        ledger=ledger,
        call="holding_review_calls",
    )
    return PrivateChainResult(review, ledger.counts(), projection_id)


def _adjudication_digest(database: Path) -> tuple[int, str]:
    import sqlite3

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT id, record_checksum FROM thesis_evidence_adjudications ORDER BY id"
        ).fetchall()
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":"))
    return len(rows), hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _load_owner_key_without_sensitive_environment(phase41) -> bytes:
    calls = 0

    def run_exact(command: list[str]):
        nonlocal calls
        calls += 1
        expected = [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "com.kunjin.profile-encryption",
            "-a",
            "v1",
            "-w",
        ]
        if command != expected or calls != 1:
            raise ValueError("owner Keychain access invalid")
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            shell=False,
            env={"HOME": str(phase41._canonical_home()), "PATH": "/usr/bin:/bin"},
            timeout=15,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr

    key = phase41.load_owner_key_once(run_exact)
    if calls != 1:
        raise ValueError("owner Keychain access count invalid")
    return key


def run_private_acceptance(mode: str, runtime_dir: Path) -> dict[str, object]:
    from scripts import phase41_acceptance as phase41

    if "KUNJIN_DATA_DIR" in os.environ or "KUNJIN_STATE_DIR" in os.environ:
        raise ValueError("private runtime override prohibited")
    if mode == "owner" and os.environ.get("KUNJIN_PHASE5_OWNER_APPROVED") != (
        "explicit_private_read_only_review"
    ):
        raise ValueError("owner approval required")
    subject_path = private_subject_path(mode, os.environ)
    subject = secure_read_private_subject(
        subject_path,
        excluded_roots=(Path(__file__).resolve().parents[1],),
    )
    data_dir = runtime_dir / "data"
    state_dir = runtime_dir / "state"
    data_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    target = data_dir / "kunjin.db"
    source = phase41._canonical_owner_database()
    key = b"\0" * 32
    sensitive_names = (
        "KUNJIN_PHASE5_ENGINEERING_SUBJECT_FILE",
        "KUNJIN_PHASE5_OWNER_SUBJECT_FILE",
        "KUNJIN_PHASE5_OWNER_APPROVED",
    )
    sensitive = {name: os.environ.pop(name) for name in sensitive_names if name in os.environ}
    try:
        if mode == "owner":
            key = _load_owner_key_without_sensitive_environment(phase41)
        with phase41.ReadOnlyDatabaseGuard(source, target):
            before = _adjudication_digest(target)
            os.environ["KUNJIN_DATA_DIR"] = str(data_dir)
            os.environ["KUNJIN_STATE_DIR"] = str(state_dir)
            try:
                with NoExternalOperations():
                    chain = _run_private_chain(mode, subject, key)
            finally:
                os.environ.pop("KUNJIN_DATA_DIR", None)
                os.environ.pop("KUNJIN_STATE_DIR", None)
                key = b""
            adjudication_unchanged = _adjudication_digest(target) == before
            if not adjudication_unchanged:
                raise ValueError("private acceptance adjudication changed")
            summary = _private_summary_from_review(
                mode,
                subject,
                chain,
                adjudication_unchanged=adjudication_unchanged,
            )
    finally:
        os.environ.update(sensitive)
    phase41.check_runtime_permissions(runtime_dir)
    return summary


def project_mode(mode: str, runtime_dir: Path) -> dict[str, object]:
    if mode == "local":
        return project_acceptance(
            local_fixture(), observation=_run_local_probe(runtime_dir)
        )
    if mode in {"engineering", "owner"}:
        return run_private_acceptance(mode, runtime_dir)
    if mode != "fault":
        raise ValueError("acceptance mode invalid")
    observations = [run_fault_probe(case, runtime_dir) for case in FAULT_CASES]
    result = {
        "acceptance_scope": "synthetic_local_faults_only",
        "action_authorized": False,
        "automatic_trade": False,
        "case_count": len(observations),
        "exact_amount_available": False,
        "fault_cases": list(FAULT_CASES),
        "mode": "fault",
        "network_retries": 0,
        "observations": observations,
        "official_negative_check_complete": False,
        "outcome": "fault_contract_verified",
        "review_disposition": "abstain",
        "review_maturity": "evidence_only",
        "sell_timing": "insufficient_data",
    }
    validate_summary(result, expected_mode="fault")
    return result


def _runtime_dir() -> Path:
    value = os.environ.get("KUNJIN_PHASE5_RUNTIME_DIR")
    if not value:
        raise ValueError("acceptance runtime invalid")
    path = Path(value)
    metadata = os.lstat(path)
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
    ):
        raise ValueError("acceptance runtime invalid")
    return path


def _read_private_summary(path: Path, runtime_dir: Path) -> str:
    resolved_parent = path.parent.resolve(strict=True)
    if resolved_parent != runtime_dir.resolve(strict=True) or path.is_symlink():
        raise ValueError("acceptance output invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_size > MAX_SUMMARY_BYTES
        ):
            raise ValueError("acceptance output invalid")
        raw = os.read(descriptor, MAX_SUMMARY_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_SUMMARY_BYTES:
        raise ValueError("acceptance output invalid")
    try:
        return raw.decode("ascii").strip()
    except UnicodeError:
        raise ValueError("acceptance output invalid") from None


def _produce(mode: str) -> int:
    encoded = encode_summary(project_mode(mode, _runtime_dir()))
    print(encoded)
    return 0


def _validate_file(mode: str, path_value: str) -> int:
    encoded = _read_private_summary(Path(path_value), _runtime_dir())
    print(sanitize_encoded_output(encoded, expected_mode=mode))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "produce" and args[1] in {
            "local",
            "fault",
            "engineering",
            "owner",
        }:
            return _produce(args[1])
        if (
            len(args) == 3
            and args[0] == "validate"
            and args[1] in {"local", "fault", "engineering", "owner"}
        ):
            return _validate_file(args[1], args[2])
        if len(args) == 1 and args[0] in {"local", "fault"}:
            with tempfile.TemporaryDirectory(prefix="kunjin-phase5-") as temporary:
                path = Path(temporary)
                path.chmod(0o700)
                os.environ["KUNJIN_PHASE5_RUNTIME_DIR"] = str(path)
                return _produce(args[0])
    except (OSError, RuntimeError, subprocess.SubprocessError, TypeError, ValueError):
        print('{"error_code":"phase5_acceptance_failed","ok":false}')
        return 70
    print('{"error_code":"phase5_arguments_invalid","ok":false}')
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
