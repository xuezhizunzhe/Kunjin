from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import RequestMode
from kunjin.decision.worker import WorkerExecutionError, run_public_worker
from kunjin.decision.worker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    WorkerRequest,
    decode_worker_request,
    decode_worker_response,
    encode_worker_request,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "decision" / "worker_fixture.py"


def _request() -> WorkerRequest:
    return WorkerRequest(
        schema_version=1,
        request_id="a" * 32,
        source_id="eastmoney_f10",
        field_id="basic_profile",
        subject_key="fund:000000",
        operation="fund_text_fetch",
        arguments={
            "url": "https://fundf10.eastmoney.com/",
            "referer": "https://fundf10.eastmoney.com/",
        },
    )


def _budget(worker_seconds: float = 2.0) -> RequestBudget:
    offset = [0.0]

    def clock() -> float:
        return time.monotonic() + offset[0]

    budget = RequestBudget.create(RequestMode.RAPID, request_id="a" * 32, monotonic=clock)
    offset[0] = 88.0 - worker_seconds
    return budget


def _argv(mode: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode)


def _run_fixture(
    mode: str,
    budget: RequestBudget,
):
    with patch("kunjin.decision.worker._default_worker_argv", return_value=_argv(mode)):
        return run_public_worker(_request(), budget)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_protocol_binds_exact_identity_schema_and_sizes() -> None:
    request = _request()
    encoded = encode_worker_request(request)
    assert len(encoded) <= MAX_REQUEST_BYTES
    with patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("success")):
        result = run_public_worker(request, _budget())
    assert result.request_id == request.request_id
    assert result.source_id == request.source_id
    assert result.field_id == request.field_id
    assert result.subject_key == request.subject_key
    assert result.schema_version == 1
    assert result.payload is not None
    assert result.payload.text == "fixture result"


def test_request_frame_limit_is_enforced_before_launch() -> None:
    with pytest.raises(ValueError, match="request.*limit"):
        decode_worker_request(b"x" * (MAX_REQUEST_BYTES + 1))


@pytest.mark.parametrize(
    ("mode", "reason_code"),
    (
        ("malformed", "worker_protocol_error"),
        ("wrong_id", "worker_identity_mismatch"),
        ("wrong_schema", "worker_identity_mismatch"),
        ("wrong_source", "worker_identity_mismatch"),
        ("wrong_field", "worker_identity_mismatch"),
        ("wrong_subject", "worker_identity_mismatch"),
        ("wrong_operation", "worker_identity_mismatch"),
        ("nonzero", "worker_nonzero_exit"),
    ),
)
def test_invalid_worker_results_fail_closed(mode: str, reason_code: str) -> None:
    with pytest.raises(WorkerExecutionError) as error:
        _run_fixture(mode, _budget())
    assert error.value.reason_code == reason_code
    assert "Traceback" not in str(error.value)
    assert str(FIXTURE) not in str(error.value)


def test_response_decoder_rejects_trailing_or_oversized_bytes() -> None:
    with pytest.raises(ValueError):
        decode_worker_response(b"{}junk", _request())
    with pytest.raises(ValueError, match="response.*limit"):
        decode_worker_response(b"x" * (MAX_RESPONSE_BYTES + 1), _request())
    with pytest.raises(ValueError, match="canonical JSON"):
        decode_worker_response(b"[" * 2_000 + b"]" * 2_000, _request())


@pytest.mark.parametrize("mode", ("sleep", "slow_output", "late_output"))
def test_deadline_returns_bounded_and_reaps_worker(mode: str) -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    started = time.monotonic()
    with (
        patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
        patch("kunjin.decision.worker._default_worker_argv", return_value=_argv(mode)),
    ):
        with pytest.raises(WorkerExecutionError) as error:
            run_public_worker(_request(), _budget(0.4))
    assert error.value.reason_code == "worker_timeout"
    assert time.monotonic() - started < 0.8
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert not _pid_is_alive(processes[0].pid)


def test_ignored_sigterm_is_killed_and_reaped_inside_cleanup_reserve() -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    started = time.monotonic()
    with (
        patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
        patch(
            "kunjin.decision.worker._default_worker_argv",
            return_value=_argv("ignore_sigterm"),
        ),
    ):
        with pytest.raises(WorkerExecutionError) as error:
            run_public_worker(_request(), _budget(0.4))
    assert error.value.reason_code == "worker_timeout"
    assert time.monotonic() - started < 0.8
    assert processes[0].poll() is not None
    assert not _pid_is_alive(processes[0].pid)


def test_oversized_output_cancels_kills_and_reaps_worker() -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    with (
        patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
        patch(
            "kunjin.decision.worker._default_worker_argv", return_value=_argv("oversize")
        ),
    ):
        with pytest.raises(WorkerExecutionError) as error:
            run_public_worker(_request(), _budget())
    assert error.value.reason_code == "worker_response_oversized"
    assert processes[0].poll() is not None
    assert not _pid_is_alive(processes[0].pid)


def test_cancelled_budget_does_not_launch_worker() -> None:
    budget = _budget()
    budget.cancel("owner_cancelled")
    with patch("kunjin.decision.worker.subprocess.Popen") as popen:
        with pytest.raises(WorkerExecutionError) as error:
            run_public_worker(_request(), budget)
    assert error.value.reason_code == "request_cancelled"
    popen.assert_not_called()


def test_launch_isolated_with_anonymous_pipes_and_allowlisted_environment(monkeypatch) -> None:
    monkeypatch.setenv("KUNJIN_PRIVATE_TOKEN", "must-not-cross")
    calls = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    with (
        patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
        patch(
            "kunjin.decision.worker._default_worker_argv", return_value=_argv("inspect_env")
        ),
    ):
        result = run_public_worker(_request(), _budget())
    kwargs = calls[0]
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert result.payload is not None
    assert "KUNJIN_PRIVATE_TOKEN" not in result.payload.text


def test_keyboard_interrupt_cancels_terminates_and_reaps() -> None:
    process = subprocess.Popen(
        _argv("sleep"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    budget = _budget()
    with (
        patch("kunjin.decision.worker.subprocess.Popen", return_value=process),
        patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("sleep")),
        patch(
            "kunjin.decision.worker.selectors.DefaultSelector.select",
            side_effect=KeyboardInterrupt,
        ),
    ):
        with pytest.raises(KeyboardInterrupt):
            run_public_worker(_request(), budget)
    assert budget.cancelled
    assert process.poll() is not None
    assert not _pid_is_alive(process.pid)


def test_worker_module_import_boundary_excludes_private_and_storage_modules() -> None:
    worker_main = Path(__file__).parents[2] / "src" / "kunjin" / "decision" / "worker_main.py"
    source = worker_main.read_text(encoding="utf-8")
    forbidden = ("storage", "paths", "keychain", "yangjibao", "docker", "legacy_doc")
    assert all(name not in source.casefold() for name in forbidden)


def test_production_worker_entrypoint_returns_a_structured_safe_error() -> None:
    request = _request()
    object.__setattr__(
        request,
        "arguments",
        {"url": "https://example.com/", "referer": "https://example.com/"},
    )
    result = run_public_worker(request, _budget())
    assert result.ok is False
    assert result.reason_code == "unsafe_url"
    assert result.retryable is False
    assert result.payload is None
