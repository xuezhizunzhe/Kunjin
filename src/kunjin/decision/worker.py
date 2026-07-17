from __future__ import annotations

import ipaddress
import os
import platform
import pwd
import selectors
import signal
import subprocess
import sys
import time
import urllib.parse
from datetime import timedelta
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.models import RequestMode
from kunjin.decision.worker_protocol import (
    MAX_NAV_RESPONSE_BYTES,
    MAX_RESPONSE_BYTES,
    FundNavWorkerRequest,
    FundNavWorkerResponse,
    WorkerRequest,
    WorkerResponse,
    decode_fund_nav_response,
    decode_worker_response,
    encode_fund_nav_request,
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
_PHASE0_RUN_ID_ENV = "KUNJIN_PHASE0_RUN_ID"
PUBLIC_WORKER_ENV = "public"
PRIVATE_KEYCHAIN_WORKER_ENV = "private_keychain"
_PUBLIC_WORKER_MODULE = "kunjin.decision.worker_main"
_PRIVATE_KEYCHAIN_WORKER_MODULE = "kunjin.brief.portfolio_worker_main"
_WORKER_TARGETS = frozenset(
    {
        (_PUBLIC_WORKER_MODULE, PUBLIC_WORKER_ENV),
        (_PRIVATE_KEYCHAIN_WORKER_MODULE, PRIVATE_KEYCHAIN_WORKER_ENV),
    }
)
_MAX_WORKER_HOME_CHARS = 4_096
_WORKER_ENVIRONMENT: Mapping[str, str] = MappingProxyType(
    {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
)


def _private_worker_home() -> str:
    if platform.system() != "Darwin":
        raise ValueError("private worker environment is unavailable")
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        raise ValueError("private worker HOME is unavailable") from None
    if (
        type(home) is not str
        or not home
        or len(home) > _MAX_WORKER_HOME_CHARS
        or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in home)
        or not os.path.isabs(home)
        or home == os.path.sep
        or os.path.normpath(home) != home
    ):
        raise ValueError("private worker HOME is invalid")
    return home


def _worker_environment(profile: str = PUBLIC_WORKER_ENV) -> Mapping[str, str]:
    if type(profile) is not str or profile not in {
        PUBLIC_WORKER_ENV,
        PRIVATE_KEYCHAIN_WORKER_ENV,
    }:
        raise ValueError("worker environment profile is invalid")
    environment = dict(_WORKER_ENVIRONMENT)
    if profile == PUBLIC_WORKER_ENV:
        run_id = os.environ.get(_PHASE0_RUN_ID_ENV)
        if run_id is not None:
            if len(run_id) != 32 or any(
                character not in "0123456789abcdef" for character in run_id
            ):
                raise ValueError("Phase 0 run identity is invalid")
            environment[_PHASE0_RUN_ID_ENV] = run_id
    else:
        environment["HOME"] = _private_worker_home()
    return environment


class WorkerExecutionError(RuntimeError):
    code = "worker_execution_error"

    def __init__(self, reason_code: str, message: str, *, retryable: bool = False) -> None:
        self.reason_code = reason_code
        self.retryable = retryable
        super().__init__(message)


def _validate_worker_module(value: str) -> str:
    if type(value) is not str or value not in {
        _PUBLIC_WORKER_MODULE,
        _PRIVATE_KEYCHAIN_WORKER_MODULE,
    }:
        raise ValueError("worker module is invalid")
    return value


def _validate_worker_target(module: str, environment_profile: str) -> None:
    if (
        type(module) is not str
        or type(environment_profile) is not str
        or (module, environment_profile) not in _WORKER_TARGETS
    ):
        raise ValueError("worker module and environment profile are invalid")


def _default_worker_argv(module: str = _PUBLIC_WORKER_MODULE) -> Tuple[str, ...]:
    return (sys.executable, "-I", "-m", _validate_worker_module(module))


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


def _validate_parent_nav_payload(
    result: FundNavWorkerResponse,
    request: FundNavWorkerRequest,
    budget: RequestBudget,
) -> None:
    if type(result) is not FundNavWorkerResponse:
        raise ValueError("NAV worker result must use the exact response type")
    if not result.ok:
        return
    payload = result.payload
    if payload is None:
        raise ValueError("NAV worker success payload is missing")
    if payload.fund_code != request.arguments["fund_code"]:
        raise ValueError("NAV worker response identity does not match request")
    if not (
        budget.started_at - _AUDIT_CLOCK_SKEW
        <= payload.retrieved_at
        <= budget.deadline_at + _AUDIT_CLOCK_SKEW
    ):
        raise ValueError("NAV worker retrieval time is outside request audit window")


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
    max_response_bytes: int,
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
                    min(_READ_CHUNK_BYTES, max_response_bytes + 1 - len(response)),
                )
                if not chunk:
                    selector.unregister(stdout_fd)
                    process.stdout.close()
                    stdout_open = False
                    break
                response.extend(chunk)
                if len(response) > max_response_bytes:
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


def _run_framed_worker(
    request: object,
    budget: RequestBudget,
    *,
    encoder: Callable[[object], bytes],
    decoder: Callable[[bytes, object], object],
    validator: Callable[[object, object, RequestBudget], None],
    module: str,
    max_response_bytes: int,
    environment_profile: str,
) -> object:
    if type(budget) is not RequestBudget:
        raise ValueError("budget must use the exact request budget type")
    if not callable(encoder) or not callable(decoder) or not callable(validator):
        raise ValueError("worker transport callables are invalid")
    _validate_worker_target(module, environment_profile)
    if (
        type(max_response_bytes) is not int
        or max_response_bytes <= 0
        or max_response_bytes > MAX_RESPONSE_BYTES
    ):
        raise ValueError("worker response limit is invalid")
    request_id = getattr(request, "request_id", None)
    if (
        type(request_id) is not str
        or len(request_id) != 32
        or any(character not in "0123456789abcdef" for character in request_id)
        or request_id != budget.request_id
    ):
        raise ValueError("worker request identity is invalid")
    _remaining_worker_seconds(budget)
    frame = encoder(request)
    if type(frame) is not bytes or not frame:
        raise ValueError("worker request frame is invalid")
    argv = _validate_worker_argv(_default_worker_argv(module))
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
            env=dict(_worker_environment(environment_profile)),
        )
    except (OSError, ValueError):
        raise _worker_error(
            "worker_launch_failed", "public source worker could not start"
        ) from None
    pgid = process.pid
    primary_error: Optional[BaseException] = None
    result: Optional[object] = None
    try:
        _remaining_worker_seconds(budget)
        response = _exchange_frames(process, frame, budget, max_response_bytes)
        result = decoder(bytes(response), request)
        validator(result, request, budget)
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
    return _run_framed_worker(
        request,
        budget,
        encoder=encode_worker_request,
        decoder=decode_worker_response,
        validator=_validate_parent_payload,
        module=_PUBLIC_WORKER_MODULE,
        max_response_bytes=MAX_RESPONSE_BYTES,
        environment_profile=PUBLIC_WORKER_ENV,
    )


def run_fund_nav_worker(
    request: FundNavWorkerRequest,
    budget: RequestBudget,
) -> FundNavWorkerResponse:
    if type(request) is not FundNavWorkerRequest:
        raise ValueError("request must use the exact NAV worker protocol type")
    if type(budget) is not RequestBudget:
        raise ValueError("budget must use the exact request budget type")
    if request.request_id != budget.request_id:
        raise ValueError("NAV worker and budget request identities differ")
    expected_pages = "6" if budget.mode is RequestMode.RAPID else "50"
    if request.arguments.get("max_pages") != expected_pages:
        raise ValueError("NAV worker page limit does not match request mode")
    return _run_framed_worker(
        request,
        budget,
        encoder=encode_fund_nav_request,
        decoder=decode_fund_nav_response,
        validator=_validate_parent_nav_payload,
        module=_PUBLIC_WORKER_MODULE,
        max_response_bytes=MAX_NAV_RESPONSE_BYTES,
        environment_profile=PUBLIC_WORKER_ENV,
    )
