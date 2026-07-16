from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import TRANSIENT_SOURCE_ERRORS, RequestMode, SourceErrorCode
from kunjin.decision.worker import (
    WorkerExecutionError,
    _close_worker_pipes,
    _finalize_process_group,
    run_public_worker,
)
from kunjin.decision.worker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    WorkerRequest,
    decode_worker_request,
    decode_worker_response,
    encode_worker_error,
    encode_worker_request,
    validate_worker_result_url,
    worker_error_message,
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


def _run_fixture_request(
    mode: str,
    request: WorkerRequest,
    budget: RequestBudget,
):
    with patch("kunjin.decision.worker._default_worker_argv", return_value=_argv(mode)):
        return run_public_worker(request, budget)


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
    except (PermissionError, ProcessLookupError):
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
        replace(
            request,
            arguments={
                "url": "https://fundf10.eastmoney.com/jbgk_000001.html",
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
        replace(
            request,
            arguments={
                "url": "https://fundf10.eastmoney.com/jjjl_000000.html",
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
        replace(
            request,
            field_id="announcement",
            arguments={
                "url": (
                    "https://api.fund.eastmoney.com/f10/JJGG"
                    "?fundcode=000000&fundcode=000001"
                ),
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
        replace(
            request,
            arguments={
                "url": "https://fundf10.eastmoney.com/jbgk_000000.html#private",
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
        replace(
            request,
            arguments={
                "url": "https://fundf10.eastmoney.com./jbgk_000000.html",
                "referer": "https://fundf10.eastmoney.com/",
            },
        ),
        replace(request, subject_key="fund:000001"),
    )
    for invalid in invalid_requests:
        with pytest.raises(ValueError, match="worker.*binding"):
            encode_worker_request(invalid)


def test_worker_contract_allows_controlled_api_disclosures() -> None:
    request = replace(
        _request(),
        field_id="announcement",
        arguments={
            "url": (
                "https://api.fund.eastmoney.com/f10/JJGG"
                "?fundcode=000000&pageIndex=1&pageSize=20&type=0"
            ),
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    assert decode_worker_request(encode_worker_request(request)) == request


@pytest.mark.parametrize(
    ("field_id", "url"),
    (
        ("basic_profile", "https://fundf10.eastmoney.com/jbgk_519755.html"),
        ("manager_history", "https://fundf10.eastmoney.com/jjjl_519755.html"),
        ("fee_schedule", "https://fundf10.eastmoney.com/jjfl_519755.html"),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&mode=0&code=519755",
        ),
        (
            "quarterly_holdings",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=jjcc&code=519755&topline=10&year=&month=",
        ),
        (
            "industry_exposure",
            "https://api.fund.eastmoney.com/f10/HYPZ/"
            "?fundCode=519755&year=2026",
        ),
        (
            "announcement",
            "https://api.fund.eastmoney.com/f10/JJGG"
            "?fundcode=519755&pageIndex=1&pageSize=20&type=0",
        ),
    ),
)
def test_worker_contract_accepts_exact_fund_field_templates(
    field_id: str,
    url: str,
) -> None:
    request = replace(
        _request(),
        subject_key="fund:519755",
        field_id=field_id,
        arguments={
            "url": url,
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    assert decode_worker_request(encode_worker_request(request)) == request


@pytest.mark.parametrize(
    ("field_id", "url"),
    (
        (
            "basic_profile",
            "https://fundf10.eastmoney.com/jbgk_519755.html?unknown=1",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&mode=0&code=519755&unknown=1",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&mode=0&code=519755&unknown=1&unknown=2",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&code=519755",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&mode=1&code=519755",
        ),
        (
            "quarterly_holdings",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=jjcc&code=519755&topline=20&year=&month=",
        ),
        (
            "quarterly_holdings",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=jjcc&code=519755&topline=10&year=2026&month=",
        ),
        (
            "industry_exposure",
            "https://api.fund.eastmoney.com/f10/HYPZ/?fundCode=519755",
        ),
        (
            "industry_exposure",
            "https://api.fund.eastmoney.com/f10/HYPZ/"
            "?fundCode=519755&year=1899",
        ),
        (
            "industry_exposure",
            "https://api.fund.eastmoney.com/f10/HYPZ/"
            "?fundCode=519755&year=02026",
        ),
        (
            "announcement",
            "https://api.fund.eastmoney.com/f10/JJGG"
            "?fundcode=519755&pageIndex=1&pageindex=1&pageSize=20&type=0",
        ),
        (
            "announcement",
            "https://api.fund.eastmoney.com/f10/JJGG"
            "?fundcode=519755&pageIndex=1&type=0",
        ),
        (
            "announcement",
            "https://api.fund.eastmoney.com/f10/JJGG"
            "?fundcode=519755&pageIndex=2&pageSize=20&type=0",
        ),
        (
            "basic_profile",
            "https://fundf10.eastmoney.com/jbgk_519755.html?",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?code=519755&type=gmbd&mode=0",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?type=gmbd&&mode=0&code=519755",
        ),
        (
            "size_history",
            "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
            "?&type=gmbd&mode=0&code=519755&",
        ),
        (
            "industry_exposure",
            "https://api.fund.eastmoney.com/f10/HYPZ/"
            "?%66undCode=519755&year=2026",
        ),
    ),
)
def test_worker_contract_rejects_nonexact_query_templates(
    field_id: str,
    url: str,
) -> None:
    request = replace(
        _request(),
        subject_key="fund:519755",
        field_id=field_id,
        arguments={
            "url": url,
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    with pytest.raises(ValueError, match="worker.*binding"):
        encode_worker_request(request)


def test_worker_contract_requires_exact_referer() -> None:
    request = replace(
        _request(),
        arguments={
            "url": "https://fundf10.eastmoney.com/",
            "referer": "https://fundf10.eastmoney.com/?from=worker",
        },
    )
    with pytest.raises(ValueError, match="worker.*binding"):
        encode_worker_request(request)


def test_worker_result_url_allows_only_same_subject_field_templates() -> None:
    static_request = replace(
        _request(),
        subject_key="fund:519755",
        field_id="size_history",
        arguments={
            "url": "https://fundf10.eastmoney.com/gmbd_519755.html",
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    dynamic_url = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        "?type=gmbd&mode=0&code=519755"
    )
    assert validate_worker_result_url(static_request, dynamic_url) == dynamic_url
    dynamic_request = replace(
        static_request,
        arguments={
            "url": dynamic_url,
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    static_url = "https://fundf10.eastmoney.com/gmbd_519755.html"
    assert validate_worker_result_url(dynamic_request, static_url) == static_url
    industry_request = replace(
        static_request,
        field_id="industry_exposure",
        arguments={
            "url": "https://fundf10.eastmoney.com/hytz_519755.html",
            "referer": "https://fundf10.eastmoney.com/",
        },
    )
    api_url = (
        "https://api.fund.eastmoney.com/f10/HYPZ/"
        "?fundCode=519755&year=2026"
    )
    assert validate_worker_result_url(industry_request, api_url) == api_url


@pytest.mark.parametrize(
    ("mode", "dynamic"),
    (
        ("wrong_final_code", False),
        ("wrong_final_field", False),
        ("wrong_final_dynamic_code", True),
        ("wrong_final_dynamic_query", True),
    ),
)
def test_parent_rejects_bound_host_with_wrong_final_url_and_reaps(
    mode: str,
    dynamic: bool,
) -> None:
    request = _request()
    if dynamic:
        request = replace(
            request,
            subject_key="fund:519755",
            field_id="size_history",
            arguments={
                "url": (
                    "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
                    "?type=gmbd&mode=0&code=519755"
                ),
                "referer": "https://fundf10.eastmoney.com/",
            },
        )
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    try:
        with patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture):
            with pytest.raises(WorkerExecutionError) as raised:
                _run_fixture_request(mode, request, _budget())
        assert raised.value.reason_code == "worker_protocol_error"
        assert processes[0].poll() is not None
        assert not _process_group_is_alive(processes[0].pid)
    finally:
        for process in processes:
            _kill_process_group(process.pid)
            process.wait(timeout=1)


def test_all_source_error_codes_roundtrip_only_their_safe_message() -> None:
    transient = frozenset(item.value for item in TRANSIENT_SOURCE_ERRORS)
    for error_code in SourceErrorCode:
        reason_code = error_code.value
        message = worker_error_message(reason_code)
        frame = encode_worker_error(
            _request(),
            reason_code=reason_code,
            retryable=reason_code in transient,
            message=message,
        )
        result = decode_worker_response(frame, _request())
        assert result.reason_code == reason_code
        assert result.message == message
        assert len(message) <= 512


@pytest.mark.parametrize(
    "unsafe_message",
    (
        "/Users/alice/private/token.txt",
        "Authorization: Bearer secret-token",
        "Traceback: raw exception from upstream",
    ),
)
def test_error_encoder_and_decoder_reject_noncanonical_messages(
    unsafe_message: str,
) -> None:
    reason_code = "source_unavailable"
    with pytest.raises(ValueError, match="message"):
        encode_worker_error(
            _request(),
            reason_code=reason_code,
            retryable=False,
            message=unsafe_message,
        )
    value = json.loads(
        encode_worker_error(
            _request(),
            reason_code=reason_code,
            retryable=False,
            message=worker_error_message(reason_code),
        ).decode("utf-8")
    )
    value["message"] = unsafe_message
    tampered = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    with pytest.raises(ValueError, match="message"):
        decode_worker_response(tampered, _request())


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


def test_fast_exited_leader_still_reaps_detached_stdout_grandchild(tmp_path: Path) -> None:
    pid_path = tmp_path / "fast-grandchild.pid"
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
                return_value=_argv("fast_orphan_grandchild", str(pid_path)),
            ),
        ):
            with pytest.raises(WorkerExecutionError) as raised:
                run_public_worker(_request(), _budget())
        assert raised.value.reason_code == "worker_protocol_error"
        child_pid = int(pid_path.read_text(encoding="ascii"))
        assert not _pid_is_alive(child_pid)
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
                "kunjin.decision.worker._finalize_process_group",
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


def test_finalization_signals_group_before_the_only_wait() -> None:
    events = []
    process = MagicMock()
    process.wait.side_effect = lambda **_kwargs: events.append("wait") or 0

    def record_signal(_pgid, sent_signal):
        events.append(sent_signal)

    with (
        patch("kunjin.decision.worker.os.killpg", side_effect=record_signal),
        patch("kunjin.decision.worker.time.sleep"),
    ):
        assert _finalize_process_group(process, 12345) == 0
    assert events == [signal.SIGTERM, signal.SIGKILL, "wait"]
    process.wait.assert_called_once()


def test_finalization_wait_failure_is_a_cleanup_error() -> None:
    process = MagicMock()
    process.wait.side_effect = subprocess.TimeoutExpired(("worker",), 0.1)
    with (
        patch("kunjin.decision.worker.os.killpg"),
        patch("kunjin.decision.worker.time.sleep"),
    ):
        with pytest.raises(WorkerExecutionError) as raised:
            _finalize_process_group(process, 12345)
    assert raised.value.reason_code == "worker_cleanup_failed"
    process.wait.assert_called_once()


def test_finalization_continues_to_kill_and_wait_after_grace_interruption() -> None:
    events = []
    process = MagicMock()
    process.wait.side_effect = lambda **_kwargs: events.append("wait") or 0

    def record_signal(_pgid, sent_signal):
        events.append(sent_signal)

    with (
        patch("kunjin.decision.worker.os.killpg", side_effect=record_signal),
        patch(
            "kunjin.decision.worker.time.sleep",
            side_effect=(SystemExit(23), None),
        ),
    ):
        with pytest.raises(WorkerExecutionError) as raised:
            _finalize_process_group(process, 12345)
    assert raised.value.reason_code == "worker_cleanup_failed"
    assert isinstance(raised.value.__cause__, SystemExit)
    assert events == [signal.SIGTERM, signal.SIGKILL, "wait"]


def test_pipe_closer_records_first_base_exception_and_continues() -> None:
    process = MagicMock()
    first_error = SystemExit(31)
    process.stdin.closed = False
    process.stdin.close.side_effect = first_error
    process.stdout.closed = False
    process.stdout.close.side_effect = MemoryError("second close failure")

    assert _close_worker_pipes(process) is first_error
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()


@pytest.mark.parametrize(
    ("mode", "close_error", "expected_cause"),
    (
        ("success", SystemExit(41), None),
        ("malformed", MemoryError("close interrupted"), "worker_protocol_error"),
    ),
)
def test_close_interrupt_still_finalizes_group_before_reraising(
    mode: str,
    close_error: BaseException,
    expected_cause: str | None,
) -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    budget = _budget()
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv(mode)),
            patch(
                "kunjin.decision.worker._close_worker_pipes",
                side_effect=close_error,
            ),
            patch(
                "kunjin.decision.worker._finalize_process_group",
                wraps=_finalize_process_group,
            ) as finalize,
        ):
            with pytest.raises(type(close_error)) as raised:
                run_public_worker(_request(), budget)
        assert raised.value is close_error
        if expected_cause is None:
            assert raised.value.__cause__ is None
        else:
            assert isinstance(raised.value.__cause__, WorkerExecutionError)
            assert raised.value.__cause__.reason_code == expected_cause
        assert budget.cancelled
        assert budget.cancel_reason == "worker_aborted"
        finalize.assert_called_once()
        assert processes[0].poll() is not None
        assert not _process_group_is_alive(processes[0].pid)
    finally:
        for process in processes:
            _kill_process_group(process.pid)
            process.wait(timeout=1)


def test_cancel_interrupt_after_close_still_finalizes_and_reaps() -> None:
    processes: list[subprocess.Popen] = []
    original_waits = []
    events = []
    real_popen = subprocess.Popen
    real_killpg = os.killpg

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        original_wait = process.wait
        process.wait = MagicMock(wraps=original_wait)
        processes.append(process)
        original_waits.append(original_wait)
        return process

    def record_signal(pgid: int, sent_signal: int):
        events.append(sent_signal)
        return real_killpg(pgid, sent_signal)

    budget = _budget(0.1)
    close_error = SystemExit(51)
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("sleep")),
            patch(
                "kunjin.decision.worker._close_worker_pipes",
                return_value=close_error,
            ),
            patch.object(RequestBudget, "cancel", side_effect=KeyboardInterrupt),
            patch("kunjin.decision.worker.os.killpg", side_effect=record_signal),
            patch(
                "kunjin.decision.worker._finalize_process_group",
                wraps=_finalize_process_group,
            ) as finalize,
        ):
            with pytest.raises(KeyboardInterrupt):
                run_public_worker(_request(), budget)
        finalize.assert_called_once()
        assert events == [signal.SIGTERM, signal.SIGKILL]
        processes[0].wait.assert_called_once()
        assert processes[0].returncode is not None
        assert not _process_group_is_alive(processes[0].pid)
    finally:
        for process, original_wait in zip(processes, original_waits):
            _kill_process_group(process.pid)
            original_wait(timeout=1)


def test_cleanup_failure_overrides_cancel_interrupt_after_close() -> None:
    processes: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    cleanup_error = WorkerExecutionError(
        "worker_cleanup_failed",
        "public source worker could not be reaped",
    )
    budget = _budget(0.1)
    try:
        with (
            patch("kunjin.decision.worker.subprocess.Popen", side_effect=capture),
            patch("kunjin.decision.worker._default_worker_argv", return_value=_argv("sleep")),
            patch(
                "kunjin.decision.worker._close_worker_pipes",
                return_value=SystemExit(52),
            ),
            patch.object(RequestBudget, "cancel", side_effect=KeyboardInterrupt),
            patch(
                "kunjin.decision.worker._finalize_process_group",
                side_effect=cleanup_error,
            ) as finalize,
        ):
            with pytest.raises(WorkerExecutionError) as raised:
                run_public_worker(_request(), budget)
        assert raised.value is cleanup_error
        assert isinstance(raised.value.__cause__, KeyboardInterrupt)
        finalize.assert_called_once()
    finally:
        for process in processes:
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
    assert "str(exc)" not in source


_DYNAMIC_IMPORT = "__dynamic_import__"


def _resolve_import_from(
    module_name: str,
    node: ast.ImportFrom,
    *,
    is_package: bool,
) -> set[str]:
    if node.level:
        module_parts = module_name.split(".")
        package = module_parts if is_package else module_parts[:-1]
        parent_count = node.level - 1
        if parent_count > len(package):
            return {_DYNAMIC_IMPORT}
        base_parts = package[: len(package) - parent_count]
        if node.module:
            base_parts.extend(node.module.split("."))
        base = ".".join(base_parts)
    else:
        base = node.module or ""
    names = {base} if base else set()
    for alias in node.names:
        if alias.name != "*":
            names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def _ast_import_names(
    source: str,
    module_name: str,
    *,
    is_package: bool = False,
) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in {"__import__", "import_module"}:
            names.add(_DYNAMIC_IMPORT)
        elif isinstance(node, ast.Attribute) and node.attr in {
            "__import__",
            "import_module",
        }:
            names.add(_DYNAMIC_IMPORT)
        elif isinstance(node, ast.Constant) and node.value in {
            "__import__",
            "import_module",
        }:
            names.add(_DYNAMIC_IMPORT)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            if any(alias.name in {"builtins", "importlib"} for alias in node.names):
                names.add(_DYNAMIC_IMPORT)
        elif isinstance(node, ast.ImportFrom):
            names.update(
                _resolve_import_from(
                    module_name,
                    node,
                    is_package=is_package,
                )
            )
            if node.module in {"builtins", "importlib"}:
                names.add(_DYNAMIC_IMPORT)
        elif isinstance(node, ast.Call):
            is_dynamic_import = (
                isinstance(node.func, ast.Name)
                and node.func.id in {"__import__", "import_module"}
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"__import__", "import_module"}
            ) or (
                isinstance(node.func, ast.Call)
                and isinstance(node.func.func, ast.Name)
                and node.func.func.id == "getattr"
            )
            if is_dynamic_import:
                names.add(_DYNAMIC_IMPORT)
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, str
                ):
                    names.add(node.args[0].value)
    return names


def _local_module_path(source_root: Path, module_name: str):
    if not module_name.startswith("kunjin"):
        return None
    relative = Path(*module_name.split("."))
    module_file = source_root / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = source_root / relative / "__init__.py"
    return package_file if package_file.is_file() else None


def _worker_local_dependency_closure(
    source_root: Path, worker_modules: list[Path]
) -> tuple[set[str], set[str]]:
    pending = []

    def enqueue_module_and_parents(module_name: str) -> None:
        parts = module_name.split(".")
        for count in range(1, len(parts) + 1):
            candidate = ".".join(parts[:count])
            if _local_module_path(source_root, candidate) is not None:
                pending.append(candidate)

    for module in worker_modules:
        enqueue_module_and_parents(
            ".".join(module.relative_to(source_root).with_suffix("").parts)
        )
    reachable = set()
    all_imports = set()
    while pending:
        module_name = pending.pop()
        if module_name in reachable:
            continue
        module_path = _local_module_path(source_root, module_name)
        assert module_path is not None
        reachable.add(module_name)
        imported = _ast_import_names(
            module_path.read_text(encoding="utf-8"),
            module_name,
            is_package=module_path.name == "__init__.py",
        )
        all_imports.update(imported)
        for imported_name in imported:
            imported_path = _local_module_path(source_root, imported_name)
            if imported_path is not None and imported_name not in reachable:
                enqueue_module_and_parents(imported_name)
    return reachable, all_imports


@pytest.mark.parametrize(
    "source",
    (
        "from .. import storage",
        "from kunjin import storage",
        'import importlib\nimportlib.import_module("kunjin.storage")',
        '__import__("kunjin.storage")',
    ),
)
def test_worker_ast_import_detector_catches_boundary_bypasses(source: str) -> None:
    imports = _ast_import_names(source, "kunjin.decision.worker_probe")
    assert any(
        imported == "kunjin.storage" or imported.startswith("kunjin.storage.")
        for imported in imports
    )


@pytest.mark.parametrize(
    ("module_name", "source", "expected"),
    (
        ("kunjin.funds", "from .. import storage", "kunjin.storage"),
        ("kunjin.decision", "from . import policy", "kunjin.decision.policy"),
        ("kunjin.decision", "from .. import storage", "kunjin.storage"),
    ),
)
def test_worker_ast_import_detector_resolves_package_relative_imports(
    module_name: str, source: str, expected: str
) -> None:
    imports = _ast_import_names(source, module_name, is_package=True)
    assert expected in imports


@pytest.mark.parametrize(
    "source",
    (
        'import importlib as loader\nloader.import_module("kunjin.storage")',
        'from importlib import import_module as load\nload("kunjin.storage")',
        'import builtins\nbuiltins.__import__("kunjin.storage")',
        'from builtins import __import__ as load\nload("kunjin.storage")',
        'import importlib\ngetattr(importlib, "import_module")("kunjin.storage")',
        'getattr(target, dynamic_name)("kunjin.storage")',
    ),
)
def test_worker_ast_import_detector_rejects_alias_and_getattr_bypasses(
    source: str,
) -> None:
    imports = _ast_import_names(source, "kunjin.decision.worker_probe")
    assert _DYNAMIC_IMPORT in imports


def test_all_worker_reachable_local_modules_use_strict_allowlist() -> None:
    source_root = Path(__file__).parents[2] / "src"
    worker_modules = sorted((source_root / "kunjin" / "decision").glob("worker*.py"))
    assert worker_modules
    reachable, imported = _worker_local_dependency_closure(source_root, worker_modules)
    forbidden = (
        "kunjin.storage",
        "kunjin.paths",
        "kunjin.security",
        "kunjin.adapters.yangjibao",
    )
    allowed = {
        "kunjin",
        "kunjin.decision",
        "kunjin.decision.budget",
        "kunjin.decision.models",
        "kunjin.decision.policy",
        "kunjin.decision.source_registry",
        "kunjin.decision.worker",
        "kunjin.decision.worker_main",
        "kunjin.decision.worker_protocol",
        "kunjin.funds",
        "kunjin.funds.models",
        "kunjin.funds.official_domains",
        "kunjin.funds.sources",
    }
    assert _DYNAMIC_IMPORT not in imported
    assert not any(
        name == blocked or name.startswith(f"{blocked}.")
        for name in imported
        for blocked in forbidden
    )
    assert reachable == allowed


def test_production_worker_entrypoint_rejects_unbound_url_before_launch() -> None:
    request = replace(
        _request(),
        arguments={"url": "https://example.com/", "referer": "https://example.com/"},
    )
    with patch("kunjin.decision.worker.subprocess.Popen") as popen:
        with pytest.raises(ValueError, match="worker.*binding"):
            run_public_worker(request, _budget())
    popen.assert_not_called()
