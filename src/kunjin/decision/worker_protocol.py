from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from kunjin.decision.models import (
    TRANSIENT_SOURCE_ERRORS,
    SourceErrorCode,
    validate_checksum,
    validate_identifier,
    validate_public_text,
    validate_request_id,
)

SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_URL_CHARS = 4 * 1024
MAX_ERROR_MESSAGE_CHARS = 512
_SUBJECT_KEY_PATTERN = re.compile(r"^fund:[0-9]{6}$")
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "source_id",
        "field_id",
        "subject_key",
        "operation",
        "arguments",
    }
)
_IDENTITY_KEYS = frozenset(_REQUEST_KEYS - {"arguments"})
_PAYLOAD_KEYS = frozenset(
    {
        "requested_url",
        "final_url",
        "text_base64",
        "text_checksum",
        "retrieved_at",
        "checksum",
        "content_type",
    }
)
_SOURCE_ERROR_CODES = frozenset(item.value for item in SourceErrorCode)
_TRANSIENT_ERROR_CODES = frozenset(item.value for item in TRANSIENT_SOURCE_ERRORS)
_F10_FIELD_HOSTS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "announcement": frozenset(
            {"api.fund.eastmoney.com", "fundf10.eastmoney.com"}
        ),
        "basic_profile": frozenset({"fundf10.eastmoney.com"}),
        "fee_schedule": frozenset({"fundf10.eastmoney.com"}),
        "industry_exposure": frozenset(
            {"api.fund.eastmoney.com", "fundf10.eastmoney.com"}
        ),
        "manager_history": frozenset({"fundf10.eastmoney.com"}),
        "quarterly_holdings": frozenset({"fundf10.eastmoney.com"}),
        "size_history": frozenset({"fundf10.eastmoney.com"}),
    }
)
_WORKER_BINDINGS: Mapping[str, Mapping[str, Mapping[str, frozenset[str]]]] = (
    MappingProxyType(
        {
            "fund_text_fetch": MappingProxyType(
                {"eastmoney_f10": _F10_FIELD_HOSTS}
            )
        }
    )
)


def _reject_duplicate_pairs(pairs: list) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("worker frame contains duplicate keys")
        result[key] = value
    return result


def _load_json_frame(frame: bytes, *, maximum: int, name: str) -> Dict[str, Any]:
    if type(frame) is not bytes or not frame:
        raise ValueError(f"worker {name} frame must be non-empty bytes")
    if len(frame) > maximum:
        raise ValueError(f"worker {name} exceeds frame limit")
    try:
        text = frame.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("worker frame contains a non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        raise ValueError(f"worker {name} is not canonical JSON") from None
    if type(value) is not dict:
        raise ValueError(f"worker {name} must be a JSON object")
    if _canonical_json_bytes(value) != frame:
        raise ValueError(f"worker {name} is not canonical JSON")
    return value


def _canonical_json_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validate_subject_key(value: object) -> str:
    if type(value) is not str or _SUBJECT_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("worker subject key must identify one public fund")
    return value


def _validate_url_argument(value: object, name: str) -> str:
    if type(value) is not str or not value or len(value) > MAX_URL_CHARS:
        raise ValueError(f"worker {name} must be bounded text")
    return validate_public_text(value, f"worker {name}")


def _validate_content_type(value: object) -> str:
    if value == "":
        return value
    return validate_public_text(value, "worker content type")


def _validate_schema(value: object) -> int:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise ValueError("worker schema version is not supported")
    return value


def _binding_host(url: object) -> str:
    if type(url) is not str:
        raise ValueError("worker request binding URL must be exact text")
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        raise ValueError("worker request binding URL is invalid") from None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("worker request binding requires safe HTTPS")
    return parsed.hostname.lower().rstrip(".")


def _validate_worker_binding(
    operation: object,
    source_id: object,
    field_id: object,
    arguments: object,
) -> None:
    if type(operation) is not str or type(source_id) is not str or type(field_id) is not str:
        raise ValueError("worker request binding is invalid")
    source_bindings = _WORKER_BINDINGS.get(operation)
    field_bindings = None if source_bindings is None else source_bindings.get(source_id)
    allowed_hosts = None if field_bindings is None else field_bindings.get(field_id)
    if allowed_hosts is None or type(arguments) is not dict:
        raise ValueError("worker request binding is not allowed")
    if _binding_host(arguments.get("url")) not in allowed_hosts:
        raise ValueError("worker request binding host is not allowed")
    if _binding_host(arguments.get("referer")) != "fundf10.eastmoney.com":
        raise ValueError("worker request binding referer is not allowed")


@dataclass(frozen=True)
class WorkerRequest:
    schema_version: int
    request_id: str
    source_id: str
    field_id: str
    subject_key: str
    operation: str
    arguments: Dict[str, str]

    def validate(self) -> None:
        if type(self) is not WorkerRequest or set(vars(self)) != _REQUEST_KEYS:
            raise ValueError("worker request shape is invalid")
        _validate_schema(self.schema_version)
        validate_request_id(self.request_id)
        validate_identifier(self.source_id, "worker source id")
        validate_identifier(self.field_id, "worker field id")
        _validate_subject_key(self.subject_key)
        if type(self.arguments) is not dict or set(self.arguments) != {"url", "referer"}:
            raise ValueError("worker arguments must contain only url and referer")
        _validate_url_argument(self.arguments["url"], "url")
        _validate_url_argument(self.arguments["referer"], "referer")
        _validate_worker_binding(
            self.operation,
            self.source_id,
            self.field_id,
            self.arguments,
        )

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "arguments": dict(self.arguments),
            "field_id": self.field_id,
            "operation": self.operation,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "subject_key": self.subject_key,
        }


@dataclass(frozen=True)
class WorkerTextPayload:
    requested_url: str
    final_url: str
    text: str
    text_checksum: str
    retrieved_at: datetime
    checksum: str
    content_type: str


@dataclass(frozen=True)
class WorkerResponse:
    schema_version: int
    request_id: str
    source_id: str
    field_id: str
    subject_key: str
    operation: str
    ok: bool
    payload: Optional[WorkerTextPayload]
    reason_code: Optional[str]
    retryable: Optional[bool]
    message: Optional[str]


def encode_worker_request(request: WorkerRequest) -> bytes:
    if type(request) is not WorkerRequest:
        raise ValueError("worker request must use the exact protocol type")
    frame = _canonical_json_bytes(request.to_dict())
    if len(frame) > MAX_REQUEST_BYTES:
        raise ValueError("worker request exceeds frame limit")
    return frame


def decode_worker_request(frame: bytes) -> WorkerRequest:
    value = _load_json_frame(frame, maximum=MAX_REQUEST_BYTES, name="request")
    if set(value) != _REQUEST_KEYS:
        raise ValueError("worker request shape is invalid")
    request = WorkerRequest(
        schema_version=value["schema_version"],
        request_id=value["request_id"],
        source_id=value["source_id"],
        field_id=value["field_id"],
        subject_key=value["subject_key"],
        operation=value["operation"],
        arguments=value["arguments"],
    )
    request.validate()
    return request


def _identity_dict(request: WorkerRequest) -> Dict[str, Any]:
    return {
        "field_id": request.field_id,
        "operation": request.operation,
        "request_id": request.request_id,
        "schema_version": request.schema_version,
        "source_id": request.source_id,
        "subject_key": request.subject_key,
    }


def encode_worker_success(request: WorkerRequest, payload: WorkerTextPayload) -> bytes:
    request.validate()
    if type(payload) is not WorkerTextPayload:
        raise ValueError("worker payload must use the exact protocol type")
    requested_url = _validate_url_argument(payload.requested_url, "requested url")
    final_url = _validate_url_argument(payload.final_url, "final url")
    if type(payload.text) is not str:
        raise ValueError("worker response text must be text")
    try:
        text_bytes = payload.text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("worker response text is not valid UTF-8") from None
    if type(payload.retrieved_at) is not datetime or payload.retrieved_at.tzinfo is None:
        raise ValueError("worker retrieval time must be timezone aware")
    if payload.retrieved_at.utcoffset() != timedelta(0):
        raise ValueError("worker retrieval time must be UTC")
    checksum = validate_checksum(payload.checksum, "worker payload checksum")
    text_checksum = validate_checksum(
        payload.text_checksum,
        "worker payload text checksum",
    )
    if text_checksum != hashlib.sha256(text_bytes).hexdigest():
        raise ValueError("worker payload text checksum does not match text")
    content_type = _validate_content_type(payload.content_type)
    value = _identity_dict(request)
    value.update(
        {
            "ok": True,
            "payload": {
                "checksum": checksum,
                "content_type": content_type,
                "final_url": final_url,
                "requested_url": requested_url,
                "retrieved_at": payload.retrieved_at.astimezone(timezone.utc).isoformat(),
                "text_base64": base64.b64encode(text_bytes).decode("ascii"),
                "text_checksum": text_checksum,
            },
        }
    )
    frame = _canonical_json_bytes(value)
    if len(frame) > MAX_RESPONSE_BYTES:
        raise ValueError("worker response exceeds frame limit")
    return frame


def encode_worker_error(
    request: WorkerRequest,
    *,
    reason_code: str,
    retryable: bool,
    message: str,
) -> bytes:
    request.validate()
    validate_identifier(reason_code, "worker reason code")
    if reason_code not in _SOURCE_ERROR_CODES:
        raise ValueError("worker reason code is not supported")
    if type(retryable) is not bool:
        raise ValueError("worker retryable must be an exact boolean")
    if retryable != (reason_code in _TRANSIENT_ERROR_CODES):
        raise ValueError("worker retryable does not match its reason code")
    validate_public_text(message, "worker error message")
    if len(message) > MAX_ERROR_MESSAGE_CHARS:
        raise ValueError("worker error message exceeds limit")
    value = _identity_dict(request)
    value.update(
        {
            "message": message,
            "ok": False,
            "reason_code": reason_code,
            "retryable": retryable,
        }
    )
    return _canonical_json_bytes(value)


def _validate_response_identity(value: Dict[str, Any], request: WorkerRequest) -> None:
    expected = _identity_dict(request)
    if any(value.get(key) != expected[key] for key in _IDENTITY_KEYS):
        raise ValueError("worker response identity does not match request")


def decode_worker_response(frame: bytes, request: WorkerRequest) -> WorkerResponse:
    request.validate()
    value = _load_json_frame(frame, maximum=MAX_RESPONSE_BYTES, name="response")
    _validate_response_identity(value, request)
    ok = value.get("ok")
    if type(ok) is not bool:
        raise ValueError("worker response status is invalid")
    if ok:
        if set(value) != _IDENTITY_KEYS | {"ok", "payload"}:
            raise ValueError("worker success response shape is invalid")
        raw_payload = value["payload"]
        if type(raw_payload) is not dict or set(raw_payload) != _PAYLOAD_KEYS:
            raise ValueError("worker success payload shape is invalid")
        try:
            text_bytes = base64.b64decode(raw_payload["text_base64"], validate=True)
            text = text_bytes.decode("utf-8")
        except (TypeError, ValueError, binascii.Error, UnicodeDecodeError):
            raise ValueError("worker response text encoding is invalid") from None
        text_checksum = validate_checksum(
            raw_payload["text_checksum"],
            "worker payload text checksum",
        )
        if text_checksum != hashlib.sha256(text_bytes).hexdigest():
            raise ValueError("worker payload text checksum does not match text")
        try:
            retrieved_at = datetime.fromisoformat(raw_payload["retrieved_at"])
        except (TypeError, ValueError):
            raise ValueError("worker retrieval time is invalid") from None
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() != timedelta(0):
            raise ValueError("worker retrieval time must be UTC")
        payload = WorkerTextPayload(
            requested_url=_validate_url_argument(raw_payload["requested_url"], "requested url"),
            final_url=_validate_url_argument(raw_payload["final_url"], "final url"),
            text=text,
            text_checksum=text_checksum,
            retrieved_at=retrieved_at.astimezone(timezone.utc),
            checksum=validate_checksum(raw_payload["checksum"], "worker payload checksum"),
            content_type=_validate_content_type(raw_payload["content_type"]),
        )
        if payload.requested_url != request.arguments["url"]:
            raise ValueError("worker response identity does not match request URL")
        return WorkerResponse(
            **expected_response_identity(request),
            ok=True,
            payload=payload,
            reason_code=None,
            retryable=None,
            message=None,
        )
    if set(value) != _IDENTITY_KEYS | {"ok", "reason_code", "retryable", "message"}:
        raise ValueError("worker error response shape is invalid")
    reason_code = validate_identifier(value["reason_code"], "worker reason code")
    if reason_code not in _SOURCE_ERROR_CODES:
        raise ValueError("worker reason code is not supported")
    retryable = value["retryable"]
    if type(retryable) is not bool:
        raise ValueError("worker retryable must be an exact boolean")
    if retryable != (reason_code in _TRANSIENT_ERROR_CODES):
        raise ValueError("worker retryable does not match its reason code")
    message = validate_public_text(value["message"], "worker error message")
    if len(message) > MAX_ERROR_MESSAGE_CHARS:
        raise ValueError("worker error message exceeds limit")
    return WorkerResponse(
        **expected_response_identity(request),
        ok=False,
        payload=None,
        reason_code=reason_code,
        retryable=retryable,
        message=message,
    )


def expected_response_identity(request: WorkerRequest) -> Dict[str, Any]:
    return {
        "field_id": request.field_id,
        "operation": request.operation,
        "request_id": request.request_id,
        "schema_version": request.schema_version,
        "source_id": request.source_id,
        "subject_key": request.subject_key,
    }
