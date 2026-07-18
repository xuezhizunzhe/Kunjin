from __future__ import annotations

import hashlib
import json
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Dict, Optional

from kunjin.decision.models import SourceErrorCode
from kunjin.intelligence.worker_protocol import (
    MAX_INTELLIGENCE_REQUEST_BYTES,
    IntelligenceSourceKind,
    IntelligenceWorkerFailure,
    IntelligenceWorkerRedirect,
    IntelligenceWorkerRequest,
    IntelligenceWorkerResponse,
    decode_intelligence_worker_request,
    encode_intelligence_worker_failure,
    encode_intelligence_worker_redirect,
    encode_intelligence_worker_success,
)

_USER_AGENT = "KunJin/0.1 public-research"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _read_request() -> bytes:
    frame = sys.stdin.buffer.read(MAX_INTELLIGENCE_REQUEST_BYTES + 1)
    if len(frame) > MAX_INTELLIGENCE_REQUEST_BYTES:
        raise ValueError("intelligence worker request exceeds frame limit")
    return frame


def _reject_duplicate_pairs(pairs: list) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("public JSON contains duplicate keys")
        result[key] = value
    return result


def _validate_source_payload(
    source_kind: IntelligenceSourceKind,
    payload_utf8: str,
) -> None:
    if source_kind in {
        IntelligenceSourceKind.GOV_POLICY,
        IntelligenceSourceKind.EASTMONEY_MARKET,
    }:
        try:
            json.loads(
                payload_utf8,
                object_pairs_hook=_reject_duplicate_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
            raise ValueError("public JSON payload is malformed") from None
        return
    lowered = payload_utf8.casefold()
    if "<html" not in lowered or "</html>" not in lowered:
        raise ValueError("public HTML payload is malformed")
    parser = HTMLParser(convert_charrefs=True)
    try:
        parser.feed(payload_utf8)
        parser.close()
    except (AssertionError, TypeError, ValueError):
        raise ValueError("public HTML payload is malformed") from None


def _content_type(headers: object) -> str:
    raw = headers.get("Content-Type", "")
    if type(raw) is not str or not raw or len(raw) > 512:
        raise ValueError("public response content type is invalid")
    return raw


def _read_success(
    request: IntelligenceWorkerRequest,
    response: object,
    retrieved_at: datetime,
) -> IntelligenceWorkerResponse:
    content_type = _content_type(response.headers)
    media_type = content_type.split(";", 1)[0].strip().casefold()
    expected_types = (
        {"application/json", "text/json"}
        if request.source_kind
        in {IntelligenceSourceKind.GOV_POLICY, IntelligenceSourceKind.EASTMONEY_MARKET}
        else {"text/html", "application/xhtml+xml"}
    )
    if media_type not in expected_types:
        raise ValueError("public response content type does not match source")
    content_length = response.headers.get("Content-Length")
    if content_length not in (None, ""):
        try:
            declared = int(content_length)
        except (TypeError, ValueError):
            raise ValueError("public response content length is invalid") from None
        if declared < 0:
            raise ValueError("public response content length is invalid")
        if declared > request.maximum_bytes:
            raise OverflowError("public response exceeds request limit")
    payload = response.read(request.maximum_bytes + 1)
    if type(payload) is not bytes:
        raise ValueError("public response body must be bytes")
    if not payload or len(payload) > request.maximum_bytes:
        raise OverflowError("public response exceeds request limit")
    try:
        payload_utf8 = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("public response body is not UTF-8") from None
    _validate_source_payload(request.source_kind, payload_utf8)
    return IntelligenceWorkerResponse(
        requested_url=request.requested_url,
        final_url=request.requested_url,
        retrieved_at=retrieved_at,
        content_type=content_type,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        payload_utf8=payload_utf8,
    )


def _http_failure(status: int) -> SourceErrorCode:
    if status in {401, 403}:
        return SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED
    if status == 404:
        return SourceErrorCode.HTTP_NOT_FOUND
    if status == 410:
        return SourceErrorCode.HTTP_GONE
    if status == 429:
        return SourceErrorCode.HTTP_RATE_LIMITED
    if 500 <= status <= 599:
        return SourceErrorCode.HTTP_5XX
    return SourceErrorCode.HTTP_4XX


def _network_failure(error: BaseException) -> SourceErrorCode:
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, socket.gaierror):
        return SourceErrorCode.DNS_FAILURE
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return SourceErrorCode.NETWORK_TIMEOUT
    return SourceErrorCode.TRANSIENT_NETWORK_FAILURE


def _failure(
    request: IntelligenceWorkerRequest,
    reason_code: SourceErrorCode,
    retrieved_at: datetime,
    *,
    http_status: Optional[int] = None,
) -> bytes:
    from kunjin.decision.models import TRANSIENT_SOURCE_ERRORS

    return encode_intelligence_worker_failure(
        request,
        IntelligenceWorkerFailure(
            requested_url=request.requested_url,
            retrieved_at=retrieved_at,
            reason_code=reason_code,
            retryable=reason_code in TRANSIENT_SOURCE_ERRORS,
            http_status=http_status,
        ),
    )


def _execute(request: IntelligenceWorkerRequest) -> bytes:
    started_at = _utc_now()
    remaining = (request.deadline_utc - started_at).total_seconds()
    if remaining <= 0:
        return _failure(request, SourceErrorCode.REQUEST_EXPIRED, started_at)
    http_request = urllib.request.Request(
        request.requested_url,
        headers={
            "Accept": "application/json,text/html;q=0.9",
            "User-Agent": _USER_AGENT,
        },
        method="GET",
    )
    try:
        with _build_opener().open(http_request, timeout=remaining) as response:
            success = _read_success(request, response, _utc_now())
        return encode_intelligence_worker_success(request, success)
    except urllib.error.HTTPError as exc:
        retrieved_at = _utc_now()
        if exc.code in _REDIRECT_STATUSES:
            location = exc.headers.get("Location")
            if type(location) is not str or not location:
                return _failure(
                    request,
                    SourceErrorCode.UNSAFE_REDIRECT,
                    retrieved_at,
                    http_status=exc.code,
                )
            try:
                return encode_intelligence_worker_redirect(
                    request,
                    IntelligenceWorkerRedirect(
                        requested_url=request.requested_url,
                        retrieved_at=retrieved_at,
                        location=location,
                        http_status=exc.code,
                    ),
                )
            except ValueError:
                return _failure(
                    request,
                    SourceErrorCode.UNSAFE_REDIRECT,
                    retrieved_at,
                    http_status=exc.code,
                )
        if 300 <= exc.code <= 399:
            return _failure(
                request,
                SourceErrorCode.UNSAFE_REDIRECT,
                retrieved_at,
                http_status=exc.code,
            )
        if not 400 <= exc.code <= 599:
            return _failure(request, SourceErrorCode.SOURCE_UNAVAILABLE, retrieved_at)
        reason = _http_failure(exc.code)
        return _failure(request, reason, retrieved_at, http_status=exc.code)
    except OverflowError:
        return _failure(request, SourceErrorCode.OVERSIZED_RESPONSE, _utc_now())
    except (TimeoutError, socket.timeout, socket.gaierror, urllib.error.URLError) as exc:
        return _failure(request, _network_failure(exc), _utc_now())
    except (TypeError, UnicodeError, ValueError):
        return _failure(request, SourceErrorCode.DECODE_FAILURE, _utc_now())


def main() -> int:
    try:
        request = decode_intelligence_worker_request(_read_request())
    except ValueError:
        return 2
    frame = _execute(request)
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
