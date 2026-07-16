from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
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


def _argv(mode: str, *arguments: str) -> tuple[str, ...]:
    return (sys.executable, str(FIXTURE), mode, *arguments)


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


def _process_group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill_process_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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


def test_worker_contract_rejects_source_field_and_host_impersonation() -> None:
    request = _request()
    invalid_requests = (
        replace(request, source_id="fund_manager_official_documents"),
        replace(request, field_id="net_asset_value"),
        replace(
            request,
            arguments={
                "url": "https://api.fund.eastmoney.com/f10/JBGK/",
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
    )
    for invalid in invalid_requests:
        with pytest.raises(ValueError, match="worker.*binding"):
            encode_worker_request(invalid)


def test_worker_contract_allows_controlled_api_disclosures() -> None:
    request = replace(
        _request(),
        field_id="announcement",
        arguments={
            "url": "https://api.fund.eastmoney.com/f10/JJGG?fundcode=000000",
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    assert decode_worker_request(encode_worker_request(request)) == request


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


def test_protocol_requires_canonical_json_bytes() -> None:
    request = _request()
    noncanonical = json.dumps(request.to_dict(), sort_keys=False).encode("utf-8")
    with pytest.raises(ValueError, match="canonical JSON"):
        decode_worker_request(noncanonical)


def test_transport_text_checksum_is_bound_to_utf8_text() -> None:
    with pytest.raises(WorkerExecutionError) as error:
        _run_fixture("bad_text_checksum", _budget())
    assert error.value.reason_code == "worker_protocol_error"


@pytest.mark.parametrize("mode", ("unsafe_final", "future_time"))
def test_parent_rejects_untrusted_payload_metadata(mode: str) -> None:
    with pytest.raises(WorkerExecutionError) as error:
        _run_fixture(mode, _budget())
    assert error.value.reason_code == "worker_protocol_error"


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


def test_slow_popen_cannot_recreate_worker_deadline() -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def slow_capture(*args, **kwargs):
        time.sleep(0.55)
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    started = time.monotonic()
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=slow_capture),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("success")),
        ):
            with pytest.raises(WorkerExecutionError) as error:
                run_public_worker(_request(), _budget(0.4))
        assert error.value.reason_code == "worker_timeout"
        assert time.monotonic() - started < 0.8
        assert processes and not _process_group_is_alive(processes[0].pid)
    finally:
        for process in processes:
            _kill_process_group(process.pid)
            process.wait(timeout=1)


def test_leader_exit_still_reaps_ignored_term_grandchild(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
            patch(
                "kunjin.decision.worker._default_worker_argv",
                return_value=_argv("orphan_grandchild", str(pid_path)),
            ),
        ):
            with pytest.raises(WorkerExecutionError) as error:
                run_public_worker(_request(), _budget(0.4))
        assert error.value.reason_code == "worker_timeout"
        child_pid = int(pid_path.read_text(encoding="ascii"))
        assert processes[0].poll() is not None
        assert not _pid_is_alive(child_pid)
        assert not _process_group_is_alive(processes[0].pid)
    finally:
        for process in processes:
            _kill_process_group(process.pid)
            process.wait(timeout=1)


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


def test_prelaunch_worker_cutoff_cancels_budget_and_never_launches() -> None:
    budget = _budget(0.0)
    with patch("kunjin.decision.worker.subprocess.Popen") as popen:
        with pytest.raises(WorkerExecutionError) as error:
            run_public_worker(_request(), budget)
    assert error.value.reason_code == "worker_timeout"
    assert budget.cancelled
    assert budget.cancel_reason == "worker_timeout"
    assert budget.worker_seconds() == 0.0
    popen.assert_not_called()


def test_cleanup_failure_overrides_and_chains_original_timeout() -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    cleanup_error = WorkerExecutionError(
        "worker_cleanup_failed",
        "public source worker process group could not be removed",
    )
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("sleep")),
            patch(
                "kunjin.decision.worker._terminate_and_reap",
                side_effect=cleanup_error,
            ),
        ):
            with pytest.raises(WorkerExecutionError) as raised:
                run_public_worker(_request(), _budget(0.1))
        assert raised.value is cleanup_error
        assert isinstance(raised.value.__cause__, WorkerExecutionError)
        assert raised.value.__cause__.reason_code == "worker_timeout"
    finally:
        for process in processes:
            _kill_process_group(process.pid)
            process.wait(timeout=1)


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


def test_launch_fails_closed_when_process_group_identity_is_unexpected() -> None:
    process = subprocess.Popen(
        _argv("sleep"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    budget = _budget()
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", return_value=process),
            patch("kunjin.decision.worker.os.getpgid", return_value=process.pid + 1),
        ):
            with pytest.raises(WorkerExecutionError) as raised:
                run_public_worker(_request(), budget)
        assert raised.value.reason_code == "worker_launch_failed"
        assert budget.cancelled
        assert process.poll() is not None
    finally:
        _kill_process_group(process.pid)
        process.wait(timeout=1)


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


def test_system_exit_cancels_terminates_group_and_is_reraised() -> None:
    process = subprocess.Popen(
        _argv("sleep"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    budget = _budget()
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", return_value=process),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("sleep")),
            patch(
                "kunjin.decision.worker.selectors.DefaultSelector.select",
                side_effect=SystemExit(17),
            ),
        ):
            with pytest.raises(SystemExit) as raised:
                run_public_worker(_request(), budget)
        assert raised.value.code == 17
        assert budget.cancelled
        assert process.poll() is not None
        assert not _process_group_is_alive(process.pid)
    finally:
        _kill_process_group(process.pid)
        process.wait(timeout=1)


def test_worker_module_import_boundary_excludes_private_and_storage_modules() -> None:
    worker_main = Path(__file__).parents[2] / "src" / "kunjin" / "decision" / "worker_main.py"
    source = worker_main.read_text(encoding="utf-8")
    forbidden = ("storage", "paths", "keychain", "yangjibao", "docker", "legacy_doc")
    assert all(name not in source.casefold() for name in forbidden)


def test_production_worker_entrypoint_rejects_unbound_url_before_launch() -> None:
    request = replace(
        _request(),
        arguments={"url": "https://example.com/", "referer": "https://example.com/"},
    )
    with patch("kunjin.decision.worker.subprocess.Popen") as popen:
        with pytest.raises(ValueError, match="worker.*binding"):
            run_public_worker(request, _budget())
    popen.assert_not_called()
