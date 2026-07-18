from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional, Union

from kunjin.decision.models import (
    TRANSIENT_SOURCE_ERRORS,
    SourceErrorCode,
    validate_checksum,
    validate_public_text,
    validate_request_id,
)

SCHEMA_VERSION = 1
MAX_INTELLIGENCE_REQUEST_BYTES = 16 * 1024
MAX_INTELLIGENCE_PAYLOAD_BYTES = 5 * 1024 * 1024
MAX_INTELLIGENCE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_INTELLIGENCE_URL_CHARS = 4 * 1024

_REQUEST_KEYS = frozenset(
    {"deadline_utc", "maximum_bytes", "request_id", "requested_url", "source_kind"}
)
_IDENTITY_KEYS = frozenset({"request_id", "schema_version", "source_kind"})
_SUCCESS_PAYLOAD_KEYS = frozenset(
    {
        "content_type",
        "final_url",
        "payload_base64",
        "payload_sha256",
        "requested_url",
        "retrieved_at",
    }
)
_REDIRECT_KEYS = frozenset(
    {"http_status", "location", "requested_url", "retrieved_at"}
)
_FAILURE_KEYS = frozenset(
    {"http_status", "reason_code", "requested_url", "retrieved_at", "retryable"}
)
_STCN_DETAIL_URL = re.compile(
    r"https://www\.stcn\.com/article/detail/[1-9][0-9]{0,15}\.html"
)
_GOV_POLICY_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
_STCN_FUND_LIST_URL = "https://www.stcn.com/article/list/fund.html"
_EASTMONEY_HOSTS = ("push2.eastmoney.com", "push2delay.eastmoney.com")
_EASTMONEY_FIELDS = "f12,f14,f3,f8,f62,f184,f104,f105"
_EASTMONEY_URLS = frozenset(
    f"https://{host}/api/qt/clist/get?"
    + urllib.parse.urlencode(
        {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": market_filter,
            "fields": _EASTMONEY_FIELDS,
        }
    )
    for host in _EASTMONEY_HOSTS
    for market_filter in ("m:90+t:2", "m:90+t:3")
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class IntelligenceSourceKind(str, Enum):
    GOV_POLICY = "gov_policy"
    STCN_FUND_LIST = "stcn_fund_list"
    STCN_FUND_DETAIL = "stcn_fund_detail"
    EASTMONEY_MARKET = "eastmoney_market"


@dataclass(frozen=True)
class IntelligenceWorkerRequest:
    source_kind: IntelligenceSourceKind
    requested_url: str
    request_id: str
    deadline_utc: datetime
    maximum_bytes: int = MAX_INTELLIGENCE_PAYLOAD_BYTES

    def validate(self) -> None:
        if type(self) is not IntelligenceWorkerRequest or set(vars(self)) != _REQUEST_KEYS:
            raise ValueError("intelligence worker request shape is invalid")
        if type(self.source_kind) is not IntelligenceSourceKind:
            raise ValueError("intelligence worker source kind is invalid")
        validate_intelligence_source_url(self.source_kind, self.requested_url)
        validate_request_id(self.request_id)
        _validate_utc(self.deadline_utc, "worker deadline")
        if (
            type(self.maximum_bytes) is not int
            or not 1 <= self.maximum_bytes <= MAX_INTELLIGENCE_PAYLOAD_BYTES
        ):
            raise ValueError("intelligence worker payload limit is invalid")


@dataclass(frozen=True)
class IntelligenceWorkerResponse:
    requested_url: str
    final_url: str
    retrieved_at: datetime
    content_type: str
    payload_sha256: str
    payload_utf8: str


@dataclass(frozen=True)
class IntelligenceWorkerRedirect:
    requested_url: str
    retrieved_at: datetime
    location: str
    http_status: int


@dataclass(frozen=True)
class IntelligenceWorkerFailure:
    requested_url: str
    retrieved_at: datetime
    reason_code: SourceErrorCode
    retryable: bool
    http_status: Optional[int]


IntelligenceWorkerResult = Union[
    IntelligenceWorkerResponse,
    IntelligenceWorkerRedirect,
    IntelligenceWorkerFailure,
]


def _reject_duplicate_pairs(pairs: list) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("intelligence worker frame contains duplicate keys")
        result[key] = value
    return result


def _canonical_json_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _load_frame(frame: bytes, *, maximum: int, name: str) -> Dict[str, Any]:
    if type(frame) is not bytes or not frame or len(frame) > maximum:
        raise ValueError(f"intelligence worker {name} is not canonical JSON")
    try:
        value = json.loads(
            frame.decode("ascii"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        raise ValueError(f"intelligence worker {name} is not canonical JSON") from None
    if type(value) is not dict or _canonical_json_bytes(value) != frame:
        raise ValueError(f"intelligence worker {name} is not canonical JSON")
    return value


def _validate_utc(value: object, name: str) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"intelligence {name} must be an aware UTC datetime")
    return value


def _parse_utc(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"intelligence {name} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"intelligence {name} is invalid") from None
    return _validate_utc(parsed, name)


def validate_intelligence_source_url(
    source_kind: IntelligenceSourceKind,
    url: str,
) -> str:
    if type(source_kind) is not IntelligenceSourceKind:
        raise ValueError("intelligence source URL kind is invalid")
    if type(url) is not str or not url or len(url) > MAX_INTELLIGENCE_URL_CHARS:
        raise ValueError("intelligence source URL is invalid")
    validate_public_text(url, "intelligence source URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("intelligence source URL is invalid") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
        or parsed.hostname.endswith(".")
    ):
        raise ValueError("intelligence source URL is invalid")
    valid = False
    if source_kind is IntelligenceSourceKind.GOV_POLICY:
        valid = url == _GOV_POLICY_URL
    elif source_kind is IntelligenceSourceKind.STCN_FUND_LIST:
        valid = url == _STCN_FUND_LIST_URL
    elif source_kind is IntelligenceSourceKind.STCN_FUND_DETAIL:
        valid = _STCN_DETAIL_URL.fullmatch(url) is not None
    elif source_kind is IntelligenceSourceKind.EASTMONEY_MARKET:
        valid = url in _EASTMONEY_URLS
    if not valid:
        raise ValueError("intelligence source URL is not allowlisted")
    return url


def encode_intelligence_worker_request(request: IntelligenceWorkerRequest) -> bytes:
    if type(request) is not IntelligenceWorkerRequest:
        raise ValueError("intelligence worker request type is invalid")
    request.validate()
    frame = _canonical_json_bytes(
        {
            "deadline_utc": request.deadline_utc.astimezone(timezone.utc).isoformat(),
            "maximum_bytes": request.maximum_bytes,
            "request_id": request.request_id,
            "requested_url": request.requested_url,
            "source_kind": request.source_kind.value,
        }
    )
    if len(frame) > MAX_INTELLIGENCE_REQUEST_BYTES:
        raise ValueError("intelligence worker request exceeds frame limit")
    return frame


def decode_intelligence_worker_request(frame: bytes) -> IntelligenceWorkerRequest:
    value = _load_frame(
        frame,
        maximum=MAX_INTELLIGENCE_REQUEST_BYTES,
        name="request frame",
    )
    if set(value) != _REQUEST_KEYS:
        raise ValueError("intelligence worker request shape is invalid")
    try:
        source_kind = IntelligenceSourceKind(value["source_kind"])
    except (TypeError, ValueError):
        raise ValueError("intelligence worker source kind is invalid") from None
    request = IntelligenceWorkerRequest(
        source_kind=source_kind,
        requested_url=value["requested_url"],
        request_id=value["request_id"],
        deadline_utc=_parse_utc(value["deadline_utc"], "worker deadline"),
        maximum_bytes=value["maximum_bytes"],
    )
    request.validate()
    return request


def _identity(request: IntelligenceWorkerRequest) -> Dict[str, Any]:
    request.validate()
    return {
        "request_id": request.request_id,
        "schema_version": SCHEMA_VERSION,
        "source_kind": request.source_kind.value,
    }


def _validate_response(
    request: IntelligenceWorkerRequest,
    response: IntelligenceWorkerResponse,
) -> IntelligenceWorkerResponse:
    request.validate()
    if type(response) is not IntelligenceWorkerResponse:
        raise ValueError("intelligence worker success response type is invalid")
    if response.requested_url != request.requested_url:
        raise ValueError("intelligence worker response identity does not match request")
    validate_intelligence_source_url(request.source_kind, response.requested_url)
    validate_intelligence_source_url(request.source_kind, response.final_url)
    if response.final_url != response.requested_url:
        raise ValueError("intelligence worker child must not follow redirects")
    _validate_utc(response.retrieved_at, "retrieval time")
    validate_public_text(response.content_type, "intelligence content type")
    media_type = response.content_type.split(";", 1)[0].strip().casefold()
    expected_types = (
        {"application/json", "text/json"}
        if request.source_kind
        in {IntelligenceSourceKind.GOV_POLICY, IntelligenceSourceKind.EASTMONEY_MARKET}
        else {"text/html", "application/xhtml+xml"}
    )
    if media_type not in expected_types:
        raise ValueError("intelligence content type does not match source kind")
    validate_checksum(response.payload_sha256, "intelligence payload checksum")
    if type(response.payload_utf8) is not str:
        raise ValueError("intelligence payload must be exact UTF-8 text")
    try:
        payload = response.payload_utf8.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("intelligence payload must be exact UTF-8 text") from None
    if not payload or len(payload) > request.maximum_bytes:
        raise ValueError("intelligence payload exceeds its request limit")
    if hashlib.sha256(payload).hexdigest() != response.payload_sha256:
        raise ValueError("intelligence payload checksum does not match payload")
    return response


def encode_intelligence_worker_success(
    request: IntelligenceWorkerRequest,
    response: IntelligenceWorkerResponse,
) -> bytes:
    response = _validate_response(request, response)
    value = _identity(request)
    value.update(
        {
            "ok": True,
            "payload": {
                "content_type": response.content_type,
                "final_url": response.final_url,
                "payload_base64": base64.b64encode(
                    response.payload_utf8.encode("utf-8")
                ).decode("ascii"),
                "payload_sha256": response.payload_sha256,
                "requested_url": response.requested_url,
                "retrieved_at": response.retrieved_at.astimezone(timezone.utc).isoformat(),
            },
        }
    )
    frame = _canonical_json_bytes(value)
    if len(frame) > MAX_INTELLIGENCE_RESPONSE_BYTES:
        raise ValueError("intelligence worker response exceeds frame limit")
    return frame


def encode_intelligence_worker_redirect(
    request: IntelligenceWorkerRequest,
    redirect: IntelligenceWorkerRedirect,
) -> bytes:
    request.validate()
    if (
        type(redirect) is not IntelligenceWorkerRedirect
        or redirect.requested_url != request.requested_url
        or type(redirect.http_status) is not int
        or redirect.http_status not in _REDIRECT_STATUSES
    ):
        raise ValueError("intelligence worker redirect is invalid")
    _validate_utc(redirect.retrieved_at, "redirect retrieval time")
    if (
        type(redirect.location) is not str
        or not redirect.location
        or len(redirect.location) > MAX_INTELLIGENCE_URL_CHARS
    ):
        raise ValueError("intelligence worker redirect location is invalid")
    validate_public_text(redirect.location, "intelligence redirect location")
    value = _identity(request)
    value.update(
        {
            "ok": False,
            "redirect": {
                "http_status": redirect.http_status,
                "location": redirect.location,
                "requested_url": redirect.requested_url,
                "retrieved_at": redirect.retrieved_at.astimezone(timezone.utc).isoformat(),
            },
        }
    )
    return _canonical_json_bytes(value)


def _validate_failure(
    request: IntelligenceWorkerRequest,
    failure: IntelligenceWorkerFailure,
) -> IntelligenceWorkerFailure:
    request.validate()
    if (
        type(failure) is not IntelligenceWorkerFailure
        or failure.requested_url != request.requested_url
        or type(failure.reason_code) is not SourceErrorCode
        or type(failure.retryable) is not bool
        or failure.retryable != (failure.reason_code in TRANSIENT_SOURCE_ERRORS)
    ):
        raise ValueError("intelligence worker failure is invalid")
    _validate_utc(failure.retrieved_at, "failure retrieval time")
    if failure.http_status is not None and (
        type(failure.http_status) is not int or not 100 <= failure.http_status <= 599
    ):
        raise ValueError("intelligence worker HTTP status is invalid")
    expected_status = {
        SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED: {401, 403},
        SourceErrorCode.HTTP_NOT_FOUND: {404},
        SourceErrorCode.HTTP_GONE: {410},
        SourceErrorCode.HTTP_RATE_LIMITED: {429},
    }.get(failure.reason_code)
    if expected_status is not None and failure.http_status not in expected_status:
        raise ValueError("intelligence worker HTTP status does not match failure")
    if failure.reason_code is SourceErrorCode.HTTP_5XX and (
        failure.http_status is None or not 500 <= failure.http_status <= 599
    ):
        raise ValueError("intelligence worker HTTP status does not match failure")
    if failure.reason_code is SourceErrorCode.HTTP_4XX and (
        failure.http_status is None
        or not 400 <= failure.http_status <= 499
        or failure.http_status in {401, 403, 404, 410, 429}
    ):
        raise ValueError("intelligence worker HTTP status does not match failure")
    if failure.reason_code is SourceErrorCode.UNSAFE_REDIRECT:
        if (
            failure.http_status is not None
            and not 300 <= failure.http_status <= 399
        ):
            raise ValueError("intelligence worker HTTP status does not match failure")
    elif failure.reason_code not in {
        SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED,
        SourceErrorCode.HTTP_NOT_FOUND,
        SourceErrorCode.HTTP_GONE,
        SourceErrorCode.HTTP_RATE_LIMITED,
        SourceErrorCode.HTTP_5XX,
        SourceErrorCode.HTTP_4XX,
    } and failure.http_status is not None:
        raise ValueError("non-HTTP intelligence failure cannot contain an HTTP status")
    return failure


def encode_intelligence_worker_failure(
    request: IntelligenceWorkerRequest,
    failure: IntelligenceWorkerFailure,
) -> bytes:
    failure = _validate_failure(request, failure)
    value = _identity(request)
    value.update(
        {
            "failure": {
                "http_status": failure.http_status,
                "reason_code": failure.reason_code.value,
                "requested_url": failure.requested_url,
                "retrieved_at": failure.retrieved_at.astimezone(timezone.utc).isoformat(),
                "retryable": failure.retryable,
            },
            "ok": False,
        }
    )
    return _canonical_json_bytes(value)


def decode_intelligence_worker_result(
    frame: bytes,
    request: IntelligenceWorkerRequest,
) -> IntelligenceWorkerResult:
    request.validate()
    value = _load_frame(
        frame,
        maximum=MAX_INTELLIGENCE_RESPONSE_BYTES,
        name="response frame",
    )
    expected = _identity(request)
    if any(value.get(key) != expected[key] for key in _IDENTITY_KEYS):
        raise ValueError("intelligence worker response identity does not match request")
    if type(value.get("ok")) is not bool:
        raise ValueError("intelligence worker response status is invalid")
    if value["ok"]:
        if set(value) != _IDENTITY_KEYS | {"ok", "payload"}:
            raise ValueError("intelligence worker success response shape is invalid")
        payload = value["payload"]
        if type(payload) is not dict or set(payload) != _SUCCESS_PAYLOAD_KEYS:
            raise ValueError("intelligence worker success payload shape is invalid")
        try:
            payload_bytes = base64.b64decode(payload["payload_base64"], validate=True)
            payload_utf8 = payload_bytes.decode("utf-8")
        except (TypeError, ValueError, binascii.Error, UnicodeDecodeError):
            raise ValueError("intelligence worker payload is not valid UTF-8") from None
        return _validate_response(
            request,
            IntelligenceWorkerResponse(
                requested_url=payload["requested_url"],
                final_url=payload["final_url"],
                retrieved_at=_parse_utc(payload["retrieved_at"], "retrieval time"),
                content_type=payload["content_type"],
                payload_sha256=payload["payload_sha256"],
                payload_utf8=payload_utf8,
            ),
        )
    if set(value) == _IDENTITY_KEYS | {"ok", "redirect"}:
        redirect = value["redirect"]
        if type(redirect) is not dict or set(redirect) != _REDIRECT_KEYS:
            raise ValueError("intelligence worker redirect shape is invalid")
        result = IntelligenceWorkerRedirect(
            requested_url=redirect["requested_url"],
            retrieved_at=_parse_utc(redirect["retrieved_at"], "redirect retrieval time"),
            location=redirect["location"],
            http_status=redirect["http_status"],
        )
        encode_intelligence_worker_redirect(request, result)
        return result
    if set(value) == _IDENTITY_KEYS | {"failure", "ok"}:
        failure = value["failure"]
        if type(failure) is not dict or set(failure) != _FAILURE_KEYS:
            raise ValueError("intelligence worker failure shape is invalid")
        try:
            reason = SourceErrorCode(failure["reason_code"])
        except (TypeError, ValueError):
            raise ValueError("intelligence worker failure reason is invalid") from None
        return _validate_failure(
            request,
            IntelligenceWorkerFailure(
                requested_url=failure["requested_url"],
                retrieved_at=_parse_utc(
                    failure["retrieved_at"], "failure retrieval time"
                ),
                reason_code=reason,
                retryable=failure["retryable"],
                http_status=failure["http_status"],
            ),
        )
    raise ValueError("intelligence worker failure response shape is invalid")
