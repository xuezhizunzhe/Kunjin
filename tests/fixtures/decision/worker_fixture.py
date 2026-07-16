from __future__ import annotations

import base64
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone


def _response(request: dict, *, text: str = "fixture result") -> bytes:
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
            "final_url": request["arguments"]["url"],
            "text_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "checksum": "a" * 64,
            "content_type": "text/plain; charset=utf-8",
        },
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def main() -> int:
    mode = sys.argv[1]
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if mode == "malformed":
        sys.stdout.buffer.write(b"not-json")
    elif mode == "wrong_id":
        request["request_id"] = "f" * 32
        sys.stdout.buffer.write(_response(request))
    elif mode == "wrong_schema":
        response = json.loads(_response(request).decode("utf-8"))
        response["schema_version"] = 2
        sys.stdout.buffer.write(json.dumps(response).encode("utf-8"))
    elif mode.startswith("wrong_"):
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
        sys.stdout.buffer.write(json.dumps(response).encode("utf-8"))
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
