from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone


def _response(
    request: dict,
    *,
    text: str = "fixture result",
    final_url: str | None = None,
    retrieved_at: datetime | None = None,
    text_checksum: str | None = None,
) -> bytes:
    text_bytes = text.encode("utf-8")
    payload = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "source_id": request["source_id"],
        "field_id": request["field_id"],
        "subject_key": request["subject_key"],
        "operation": request["operation"],
        "ok": True,
        "payload": {
            "requested_url": request["arguments"]["url"],
            "final_url": final_url or request["arguments"]["url"],
            "text_base64": base64.b64encode(text_bytes).decode("ascii"),
            "text_checksum": text_checksum or hashlib.sha256(text_bytes).hexdigest(),
            "retrieved_at": (retrieved_at or datetime.now(timezone.utc)).isoformat(),
            "checksum": "a" * 64,
            "content_type": "text/plain; charset=utf-8",
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def main() -> int:
    mode = sys.argv[1]
    if mode == "grandchild":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(10)
        return 0
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if mode == "malformed":
        sys.stdout.buffer.write(b"not-json")
    elif mode == "wrong_id":
        request["request_id"] = "f" * 32
        sys.stdout.buffer.write(_response(request))
    elif mode == "wrong_schema":
        response = json.loads(_response(request).decode("utf-8"))
        response["schema_version"] = 2
        sys.stdout.buffer.write(
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
    elif mode in {"wrong_source", "wrong_field", "wrong_subject", "wrong_operation"}:
        field = mode.removeprefix("wrong_")
        replacements = {
            "source": ("source_id", "eastmoney_nav"),
            "field": ("field_id", "fee_schedule"),
            "subject": ("subject_key", "fund:000001"),
            "operation": ("operation", "other_operation"),
        }
        key, value = replacements[field]
        response = json.loads(_response(request).decode("utf-8"))
        response[key] = value
        sys.stdout.buffer.write(
            json.dumps(response, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
    elif mode == "sleep":
        time.sleep(10)
    elif mode == "slow_output":
        while True:
            os.write(sys.stdout.fileno(), b"x" * 1024)
            time.sleep(0.01)
    elif mode == "ignore_sigterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(10)
    elif mode == "oversize":
        chunk = b"x" * (64 * 1024)
        for _ in range(193):
            os.write(sys.stdout.fileno(), chunk)
    elif mode == "nonzero":
        return 7
    elif mode == "late_output":
        time.sleep(0.45)
        sys.stdout.buffer.write(_response(request))
    elif mode == "orphan_grandchild":
        pid_path = sys.argv[2]
        child = subprocess.Popen(
            (sys.executable, __file__, "grandchild"),
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        with open(pid_path, "w", encoding="ascii") as handle:
            handle.write(str(child.pid))
    elif mode == "fast_orphan_grandchild":
        pid_path = sys.argv[2]
        child = subprocess.Popen(
            (sys.executable, __file__, "grandchild"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        with open(pid_path, "w", encoding="ascii") as handle:
            handle.write(str(child.pid))
    elif mode == "bad_text_checksum":
        sys.stdout.buffer.write(_response(request, text_checksum="f" * 64))
    elif mode == "unsafe_final":
        sys.stdout.buffer.write(_response(request, final_url="https://example.com/stolen"))
    elif mode == "wrong_final_code":
        sys.stdout.buffer.write(
            _response(
                request,
                final_url="https://fundf10.eastmoney.com/jbgk_000001.html",
            )
        )
    elif mode == "wrong_final_field":
        sys.stdout.buffer.write(
            _response(
                request,
                final_url="https://fundf10.eastmoney.com/jjjl_519755.html",
            )
        )
    elif mode == "wrong_final_dynamic_code":
        sys.stdout.buffer.write(
            _response(
                request,
                final_url=(
                    "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
                    "?type=gmbd&mode=0&code=000001"
                ),
            )
        )
    elif mode == "wrong_final_dynamic_query":
        sys.stdout.buffer.write(
            _response(
                request,
                final_url=(
                    "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
                    "?type=gmbd&mode=1&code=519755"
                ),
            )
        )
    elif mode == "future_time":
        sys.stdout.buffer.write(
            _response(request, retrieved_at=datetime.now(timezone.utc) + timedelta(days=3650))
        )
    elif mode == "inspect_env":
        sys.stdout.buffer.write(_response(request, text="\n".join(sorted(os.environ))))
    elif mode == "success":
        sys.stdout.buffer.write(_response(request))
    else:
        return 9
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
