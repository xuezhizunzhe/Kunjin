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
from typing import Mapping, Optional, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.worker_protocol import (
    MAX_RESPONSE_BYTES,
    WorkerRequest,
    WorkerResponse,
    decode_worker_response,
    encode_worker_request,
    validate_worker_result_url,
)
from kunjin.funds.sources import FETCHABLE_HOSTS

_READ_CHUNK_BYTES = 64 * 1024
_TERM_GRACE_SECONDS = 0.05
_KILL_GRACE_SECONDS = 0.05
_REAP_GRACE_SECONDS = 0.10
_SELECT_SLICE_SECONDS = 0.05
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


def _signal_cleanup_group(pgid: int, sent_signal: int) -> Optional[WorkerExecutionError]:
    try:
        os.killpg(pgid, sent_signal)
    except ProcessLookupError:
        return None
    except PermissionError:
        # Darwin reports EPERM when the unreaped group contains only zombies.
        return None
    except OSError:
        return _worker_error(
            "worker_cleanup_failed",
            "public source worker process group could not be signalled",
        )
    return None


def _finalize_process_group(process: subprocess.Popen, pgid: int) -> int:
    cleanup_error: Optional[WorkerExecutionError] = None
    cleanup_cause: Optional[BaseException] = None

    def record_cleanup_failure(cause: BaseException) -> None:
        nonlocal cleanup_error, cleanup_cause
        if cleanup_error is not None:
            return
        cleanup_error = _worker_error(
            "worker_cleanup_failed",
            "public source worker finalization was interrupted",
        )
        cleanup_cause = cause

    try:
        cleanup_error = _signal_cleanup_group(pgid, signal.SIGTERM)
    except BaseException as exc:
        record_cleanup_failure(exc)
    try:
        time.sleep(_TERM_GRACE_SECONDS)
    except BaseException as exc:
        record_cleanup_failure(exc)
    try:
        kill_error = _signal_cleanup_group(pgid, signal.SIGKILL)
    except BaseException as exc:
        record_cleanup_failure(exc)
    else:
        if cleanup_error is None:
            cleanup_error = kill_error
    try:
        time.sleep(_KILL_GRACE_SECONDS)
    except BaseException as exc:
        record_cleanup_failure(exc)
    try:
        return_code = process.wait(timeout=_REAP_GRACE_SECONDS)
    except BaseException as exc:
        wait_error = WorkerExecutionError(
            "worker_cleanup_failed",
            "public source worker could not be reaped",
        )
        raise wait_error from exc
    if cleanup_error is not None:
        if cleanup_cause is not None:
            raise cleanup_error from cleanup_cause
        raise cleanup_error
    return return_code


def _worker_error(reason_code: str, message: str) -> WorkerExecutionError:
    return WorkerExecutionError(reason_code, message)


def _remaining_worker_seconds(budget: RequestBudget) -> float:
    if budget.cancelled:
        raise _worker_error("request_cancelled", "request was cancelled")
    try:
        remaining = budget.worker_seconds()
    except BudgetExpired:
        if not budget.cancelled:
            budget.cancel("worker_timeout")
        raise _worker_error("worker_timeout", "public source worker deadline reached") from None
    if remaining <= 0.0:
        budget.cancel("worker_timeout")
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
    _safe_https_host(payload.requested_url)
    if payload.requested_url != request.arguments["url"]:
        raise ValueError("worker payload URL does not match request")
    validate_worker_result_url(request, payload.final_url)
    if not (
        budget.started_at - _AUDIT_CLOCK_SKEW
        <= payload.retrieved_at
        <= budget.deadline_at + _AUDIT_CLOCK_SKEW
    ):
        raise ValueError("worker retrieval time is outside request audit window")


def _close_worker_pipes(process: subprocess.Popen) -> Optional[BaseException]:
    first_error: Optional[BaseException] = None
    for pipe in (process.stdin, process.stdout):
        try:
            if pipe is None or pipe.closed:
                continue
            pipe.close()
        except OSError:
            pass
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


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
    primary_error: Optional[BaseException] = None
    result: Optional[WorkerResponse] = None
    try:
        _remaining_worker_seconds(budget)
        response = _exchange_frames(process, frame, budget)
        result = decode_worker_response(bytes(response), request)
        _validate_parent_payload(result, request, budget)
        _remaining_worker_seconds(budget)
        budget.require_publishable()
    except ValueError as exc:
        reason = (
            "worker_identity_mismatch"
            if "identity" in str(exc) or "schema version" in str(exc)
            else "worker_protocol_error"
        )
        primary_error = _worker_error(
            reason,
            "public source worker returned an invalid response",
        )
    except BudgetExpired:
        primary_error = _worker_error(
            "request_expired",
            "request result is no longer publishable",
        )
    except BaseException as exc:
        primary_error = exc
    close_error: Optional[BaseException] = None
    pending_error: Optional[BaseException] = None
    cleanup_error: Optional[WorkerExecutionError] = None
    return_code: Optional[int] = None
    try:
        try:
            close_error = _close_worker_pipes(process)
        except BaseException as exc:
            close_error = exc
        if close_error is not None:
            budget.cancel("worker_aborted")
        elif primary_error is not None:
            reason = (
                primary_error.reason_code
                if isinstance(primary_error, WorkerExecutionError)
                else "worker_aborted"
            )
            budget.cancel(reason)
    except BaseException as exc:
        pending_error = exc
    finally:
        try:
            return_code = _finalize_process_group(process, pgid)
        except WorkerExecutionError as exc:
            cleanup_error = exc
    if cleanup_error is not None:
        cause = pending_error or close_error or primary_error
        if cause is not None:
            raise cleanup_error from cause
        raise cleanup_error
    if pending_error is not None:
        raise pending_error
    if close_error is not None:
        if primary_error is not None:
            raise close_error from primary_error
        raise close_error from None
    if return_code is None:
        raise _worker_error(
            "worker_cleanup_failed",
            "public source worker finalization returned no status",
        )
    if return_code != 0 and (
        primary_error is None
        or (
            isinstance(primary_error, WorkerExecutionError)
            and primary_error.reason_code == "worker_protocol_error"
            and return_code > 0
        )
    ):
        primary_error = _worker_error("worker_nonzero_exit", "public source worker failed")
        budget.cancel("worker_nonzero_exit")
    if primary_error is not None:
        raise primary_error
    try:
        budget.require_publishable()
    except BudgetExpired:
        budget.cancel("request_expired")
        raise _worker_error(
            "request_expired",
            "request result is no longer publishable",
        ) from None
    if result is None:
        budget.cancel("worker_protocol_error")
        raise _worker_error(
            "worker_protocol_error",
            "public source worker returned no result",
        )
    return result
