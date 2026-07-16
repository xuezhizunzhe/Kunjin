from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
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

_READ_CHUNK_BYTES = 64 * 1024
_TERM_GRACE_SECONDS = 0.25
_KILL_GRACE_SECONDS = 0.25
_SELECT_SLICE_SECONDS = 0.05
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


def _terminate_and_reap(process: subprocess.Popen) -> None:
    pid = process.pid
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=_TERM_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        raise WorkerExecutionError(
            "worker_cleanup_failed",
            "public source worker could not be reaped",
        ) from None


def _cancel_and_terminate(
    budget: RequestBudget,
    process: subprocess.Popen,
    reason: str,
) -> None:
    budget.cancel(reason)
    _terminate_and_reap(process)


def _worker_error(reason_code: str, message: str) -> WorkerExecutionError:
    return WorkerExecutionError(reason_code, message)


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
    if budget.cancelled:
        raise _worker_error("request_cancelled", "request was cancelled before worker launch")
    duration = budget.worker_seconds()
    if duration <= 0.0:
        budget.cancel("worker_deadline_reached")
        raise _worker_error("worker_timeout", "public source worker deadline reached")
    frame = encode_worker_request(request)
    argv = _validate_worker_argv(_default_worker_argv())
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
    if process.stdin is None or process.stdout is None:
        _cancel_and_terminate(budget, process, "worker_pipe_invalid")
        raise _worker_error("worker_launch_failed", "public source worker pipes are unavailable")

    deadline = time.monotonic() + duration
    response = bytearray()
    selector = selectors.DefaultSelector()
    try:
        stdin_fd = process.stdin.fileno()
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdin_fd, False)
        os.set_blocking(stdout_fd, False)
        selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
        selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
    except (OSError, ValueError):
        selector.close()
        _cancel_and_terminate(budget, process, "worker_pipe_invalid")
        process.stdin.close()
        process.stdout.close()
        raise _worker_error("worker_launch_failed", "public source worker pipes are invalid")
    sent = 0
    stdout_open = True
    try:
        while stdout_open:
            if budget.cancelled:
                _cancel_and_terminate(budget, process, "request_cancelled")
                raise _worker_error(
                    "request_cancelled", "request cancelled during worker execution"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                _cancel_and_terminate(budget, process, "worker_timeout")
                raise _worker_error("worker_timeout", "public source worker deadline reached")
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
                else:
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
                        _cancel_and_terminate(budget, process, "worker_response_oversized")
                        raise _worker_error(
                            "worker_response_oversized",
                            "public source worker response exceeded its limit",
                        )
    except KeyboardInterrupt:
        _cancel_and_terminate(budget, process, "worker_interrupted")
        raise
    except (OSError, ValueError):
        _cancel_and_terminate(budget, process, "worker_io_failed")
        raise _worker_error("worker_io_failed", "public source worker I/O failed") from None
    finally:
        selector.close()
        if not process.stdin.closed:
            process.stdin.close()
        if not process.stdout.closed:
            process.stdout.close()

    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        _cancel_and_terminate(budget, process, "worker_timeout")
        raise _worker_error("worker_timeout", "public source worker result arrived too late")
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _cancel_and_terminate(budget, process, "worker_timeout")
        raise _worker_error("worker_timeout", "public source worker did not exit") from None
    if return_code != 0:
        raise _worker_error("worker_nonzero_exit", "public source worker failed")
    try:
        result = decode_worker_response(bytes(response), request)
    except ValueError as exc:
        reason = (
            "worker_identity_mismatch"
            if "identity" in str(exc) or "schema version" in str(exc)
            else "worker_protocol_error"
        )
        raise _worker_error(reason, "public source worker returned an invalid response") from None
    if time.monotonic() >= deadline:
        budget.cancel("worker_timeout")
        raise _worker_error("worker_timeout", "public source worker result arrived too late")
    try:
        budget.require_publishable()
    except BudgetExpired:
        raise _worker_error("request_expired", "request result is no longer publishable") from None
    return result
