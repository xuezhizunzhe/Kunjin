from __future__ import annotations

import ipaddress
import os
import selectors
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import timedelta
from types import MappingProxyType
from typing import Mapping, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.worker_protocol import (
    MAX_RESPONSE_BYTES,
    WorkerRequest,
    WorkerResponse,
    decode_worker_response,
    encode_worker_request,
)
from kunjin.funds.sources import FETCHABLE_HOSTS

_READ_CHUNK_BYTES = 64 * 1024
_TERM_GRACE_SECONDS = 0.25
_KILL_GRACE_SECONDS = 0.25
_SELECT_SLICE_SECONDS = 0.05
_GROUP_POLL_SECONDS = 0.01
_AUDIT_CLOCK_SKEW = timedelta(seconds=1)
_WORKER_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
)


class WorkerExecutionError(RuntimeError):
    code = "worker_execution_error"

    def __init__(self, reason_code: str, message: str, *, retryable: bool = False) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(message)


def _default_worker_argv() -> Tuple[str, ...]:
    return (sys.executable, "-I", "-m", "kunjin.decision.worker_main")


def _validate_worker_argv(value: Tuple[str, ...]) -> Tuple[str, ...]:
    if (
        type(value) is not tuple
        or not value
        or len(value) > 8
        or any(type(item) is not str or not item or len(item) > 4_096 for item in value)
    ):
        raise ValueError("worker command is invalid")
    return value


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group_exit(
    process: subprocess.Popen,
    pgid: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(pgid):
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(_GROUP_POLL_SECONDS, remaining))
    process.poll()
    return True


def _signal_process_group(process: subprocess.Popen, pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except OSError:
        try:
            if sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except OSError:
            return


def _terminate_and_reap(process: subprocess.Popen, pgid: int) -> None:
    if _process_group_exists(pgid):
        _signal_process_group(process, pgid, signal.SIGTERM)
        group_gone = _wait_for_process_group_exit(
            process,
            pgid,
            _TERM_GRACE_SECONDS,
        )
        if not group_gone:
            _signal_process_group(process, pgid, signal.SIGKILL)
            group_gone = _wait_for_process_group_exit(
                process,
                pgid,
                _KILL_GRACE_SECONDS,
            )
    else:
        group_gone = True
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        raise WorkerExecutionError(
            "worker_cleanup_failed",
            "public source worker could not be reaped",
        ) from None
    if not group_gone or _process_group_exists(pgid):
        raise WorkerExecutionError(
            "worker_cleanup_failed",
            "public source worker process group could not be removed",
        )


def _cancel_and_terminate(
    budget: RequestBudget,
    process: subprocess.Popen,
    pgid: int,
    reason: str,
) -> None:
    budget.cancel(reason)
    try:
        _terminate_and_reap(process, pgid)
    except BaseException:
        return


def _worker_error(reason_code: str, message: str) -> WorkerExecutionError:
    return WorkerExecutionError(reason_code, message)


def _remaining_worker_seconds(budget: RequestBudget) -> float:
    if budget.cancelled:
        raise _worker_error("request_cancelled", "request was cancelled")
    try:
        remaining = budget.worker_seconds()
    except BudgetExpired:
        raise _worker_error("worker_timeout", "public source worker deadline reached") from None
    if remaining <= 0.0:
        raise _worker_error("worker_timeout", "public source worker deadline reached")
    return remaining


def _safe_https_host(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("worker payload URL is unsafe") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("worker payload URL is unsafe")
    host = parsed.hostname.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("worker payload URL is unsafe")
    if host not in FETCHABLE_HOSTS:
        raise ValueError("worker payload URL host is not fetchable")
    return host


def _validate_parent_payload(
    result: WorkerResponse,
    request: WorkerRequest,
    budget: RequestBudget,
) -> None:
    if not result.ok:
        return
    payload = result.payload
    if payload is None:
        raise ValueError("worker success payload is missing")
    requested_host = _safe_https_host(payload.requested_url)
    final_host = _safe_https_host(payload.final_url)
    if payload.requested_url != request.arguments["url"] or final_host != requested_host:
        raise ValueError("worker payload URL does not match request")
    if not (
        budget.started_at - _AUDIT_CLOCK_SKEW
        <= payload.retrieved_at
        <= budget.deadline_at + _AUDIT_CLOCK_SKEW
    ):
        raise ValueError("worker retrieval time is outside request audit window")


def _close_worker_pipes(process: subprocess.Popen) -> None:
    for pipe in (process.stdin, process.stdout):
        if pipe is None or pipe.closed:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _exchange_frames(
    process: subprocess.Popen,
    frame: bytes,
    budget: RequestBudget,
) -> bytes:
    if process.stdin is None or process.stdout is None:
        raise _worker_error("worker_launch_failed", "public source worker pipes are unavailable")
    selector = selectors.DefaultSelector()
    response = bytearray()
    try:
        stdin_fd = process.stdin.fileno()
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
        selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
        sent = 0
        stdout_open = True
        while stdout_open:
            remaining = _remaining_worker_seconds(budget)
            events = selector.select(min(_SELECT_SLICE_SECONDS, remaining))
            for key, _mask in events:
                if key.data == "stdin":
                    try:
                        written = os.write(stdin_fd, frame[sent:])
                    except BrokenPipeError:
                        selector.unregister(stdin_fd)
                        process.stdin.close()
                        continue
                    sent += written
                    if sent == len(frame):
                        selector.unregister(stdin_fd)
                        process.stdin.close()
                    continue
                chunk = os.read(
                    stdout_fd,
                    min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(response)),
                )
                if not chunk:
                    selector.unregister(stdout_fd)
                    process.stdout.close()
                    stdout_open = False
                    break
                response.extend(chunk)
                if len(response) > MAX_RESPONSE_BYTES:
                    raise _worker_error(
                        "worker_response_oversized",
                        "public source worker response exceeded its limit",
                    )
    except (WorkerExecutionError, KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except (OSError, ValueError):
        raise _worker_error("worker_io_failed", "public source worker I/O failed") from None
    finally:
        selector.close()
    return bytes(response)


def run_public_worker(
    request: WorkerRequest,
    budget: RequestBudget,
) -> WorkerResponse:
    if type(request) is not WorkerRequest:
        raise ValueError("request must use the exact worker protocol type")
    if type(budget) is not RequestBudget:
        raise ValueError("budget must use the exact request budget type")
    if request.request_id != budget.request_id:
        raise ValueError("worker and budget request identities differ")
    _remaining_worker_seconds(budget)
    frame = encode_worker_request(request)
    argv = _validate_worker_argv(_default_worker_argv())
    _remaining_worker_seconds(budget)
    try:
        process = subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            restore_signals=True,
            start_new_session=True,
            env=dict(_WORKER_ENVIRONMENT),
        )
    except (OSError, ValueError):
        raise _worker_error(
            "worker_launch_failed", "public source worker could not start"
        ) from None
    pgid = process.pid
    try:
        _remaining_worker_seconds(budget)
        response = _exchange_frames(process, frame, budget)
        remaining = _remaining_worker_seconds(budget)
        return_code = process.wait(timeout=remaining)
        if return_code != 0:
            raise _worker_error("worker_nonzero_exit", "public source worker failed")
        result = decode_worker_response(bytes(response), request)
        _validate_parent_payload(result, request, budget)
        _remaining_worker_seconds(budget)
        budget.require_publishable()
    except subprocess.TimeoutExpired:
        error = _worker_error("worker_timeout", "public source worker did not exit")
        _cancel_and_terminate(budget, process, pgid, error.reason_code)
        _close_worker_pipes(process)
        raise error from None
    except ValueError as exc:
        reason = (
            "worker_identity_mismatch"
            if "identity" in str(exc) or "schema version" in str(exc)
            else "worker_protocol_error"
        )
        error = _worker_error(reason, "public source worker returned an invalid response")
        _cancel_and_terminate(budget, process, pgid, error.reason_code)
        _close_worker_pipes(process)
        raise error from None
    except BudgetExpired:
        error = _worker_error("request_expired", "request result is no longer publishable")
        _cancel_and_terminate(budget, process, pgid, error.reason_code)
        _close_worker_pipes(process)
        raise error from None
    except BaseException as exc:
        reason = exc.reason_code if isinstance(exc, WorkerExecutionError) else "worker_aborted"
        _cancel_and_terminate(budget, process, pgid, reason)
        _close_worker_pipes(process)
        raise
    _close_worker_pipes(process)
    _terminate_and_reap(process, pgid)
    return result
