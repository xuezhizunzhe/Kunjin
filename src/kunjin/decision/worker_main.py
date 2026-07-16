from __future__ import annotations

import sys

from kunjin.decision.worker_protocol import (
    MAX_REQUEST_BYTES,
    WorkerTextPayload,
    decode_worker_request,
    encode_worker_error,
    encode_worker_success,
)
from kunjin.funds.sources import FundSourceError, FundTextClient


def _read_request() -> bytes:
    frame = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(frame) > MAX_REQUEST_BYTES:
        raise ValueError("worker request exceeds frame limit")
    return frame


def main() -> int:
    try:
        request = decode_worker_request(_read_request())
    except ValueError:
        return 2
    try:
        response = FundTextClient().fetch(
            request.arguments["url"],
            request.arguments["referer"],
        )
        frame = encode_worker_success(
            request,
            WorkerTextPayload(
                requested_url=response.requested_url,
                final_url=response.final_url,
                text=response.text,
                retrieved_at=response.retrieved_at,
                checksum=response.checksum,
                content_type=response.content_type,
            ),
        )
    except FundSourceError as exc:
        frame = encode_worker_error(
            request,
            reason_code=exc.reason_code,
            retryable=exc.retryable,
            message=str(exc),
        )
    except (TypeError, ValueError):
        frame = encode_worker_error(
            request,
            reason_code="validation_failure",
            retryable=False,
            message="fund source result failed validation",
        )
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
