from __future__ import annotations

import hashlib
import sys
from decimal import Decimal
from typing import Optional

from kunjin.adapters.eastmoney import EastmoneyFundClient, PublicDataError
from kunjin.decision.models import canonical_decimal
from kunjin.decision.worker_protocol import (
    MAX_REQUEST_BYTES,
    FundNavPayload,
    FundNavRow,
    FundNavWorkerRequest,
    WorkerTextPayload,
    decode_fund_nav_request,
    decode_worker_request,
    encode_fund_nav_error,
    encode_fund_nav_success,
    encode_worker_error,
    encode_worker_success,
    worker_error_message,
)
from kunjin.funds.sources import FundSourceError, FundTextClient


def _read_request() -> bytes:
    frame = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(frame) > MAX_REQUEST_BYTES:
        raise ValueError("worker request exceeds frame limit")
    return frame


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else canonical_decimal(value)


def _nav_success(request: FundNavWorkerRequest) -> bytes:
    (
        _raw,
        fund_name,
        fund_type,
        observations,
        corporate_actions,
    ) = EastmoneyFundClient().fetch_nav_history_with_actions(
        request.arguments["fund_code"],
        page_size=20,
        max_pages=int(request.arguments["max_pages"]),
    )
    if len(observations) != len(corporate_actions):
        raise ValueError("NAV corporate action metadata is incomplete")
    ordered = tuple(
        sorted(
            zip(observations, corporate_actions),
            key=lambda item: item[0].nav_date,
            reverse=True,
        )
    )
    return encode_fund_nav_success(
        request,
        FundNavPayload(
            fund_code=request.arguments["fund_code"],
            fund_name=fund_name,
            fund_type=fund_type,
            retrieved_at=max(item.retrieved_at for item, _action in ordered),
            observation_count=len(ordered),
            rows=tuple(
                FundNavRow(
                    nav_date=item.nav_date.isoformat(),
                    unit_nav=canonical_decimal(item.unit_nav),
                    accumulated_nav=_decimal_text(item.accumulated_nav),
                    daily_growth=_decimal_text(item.daily_growth),
                    corporate_action_state=action,
                )
                for item, action in ordered
            ),
        ),
    )


def main() -> int:
    raw_request = _read_request()
    try:
        request = decode_fund_nav_request(raw_request)
    except ValueError:
        try:
            request = decode_worker_request(raw_request)
        except ValueError:
            return 2
    if type(request) is FundNavWorkerRequest:
        try:
            frame = _nav_success(request)
        except PublicDataError as exc:
            frame = encode_fund_nav_error(
                request,
                reason_code=exc.reason_code,
                retryable=exc.retryable,
                message=worker_error_message(exc.reason_code),
            )
        except (ArithmeticError, TypeError, ValueError):
            frame = encode_fund_nav_error(
                request,
                reason_code="validation_failure",
                retryable=False,
                message=worker_error_message("validation_failure"),
            )
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
        return 0
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
                text_checksum=hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
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
            message=worker_error_message(exc.reason_code),
        )
    except (TypeError, ValueError):
        frame = encode_worker_error(
            request,
            reason_code="validation_failure",
            retryable=False,
            message=worker_error_message("validation_failure"),
        )
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
