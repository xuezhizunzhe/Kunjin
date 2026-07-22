from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from kunjin.decision.models import (
    canonical_decimal,
    validate_exact_dataclass_state,
    validate_request_id,
)
from kunjin.suitability.models import validate_private_name

SCHEMA_VERSION = 2
MAX_PORTFOLIO_REQUEST_BYTES = 4 * 1024
MAX_PORTFOLIO_RESPONSE_BYTES = 1024 * 1024
MAX_PORTFOLIO_ACCOUNTS = 64
MAX_PORTFOLIO_POSITIONS = 4096
MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT = MAX_PORTFOLIO_POSITIONS
_REQUEST_KEYS = frozenset({"schema_version", "request_id", "operation"})
_RESPONSE_IDENTITY_KEYS = _REQUEST_KEYS
_ATTESTATION_KEYS = frozenset(
    {"keychain_read_count", "keychain_mutation_attempt_count"}
)
_PAYLOAD_KEYS = frozenset({"retrieved_at", "accounts", "positions"})
_ACCOUNT_KEYS = frozenset({"source_account_id", "title", "observed_at"})
_POSITION_KEYS = frozenset(
    {
        "source_account_id",
        "fund_code",
        "fund_name",
        "share_class",
        "shares",
        "formal_nav",
        "estimated_nav",
        "observed_profit",
        "observed_at",
    }
)
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_FUND_CODE = re.compile(r"^[0-9]{6}$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,18})?$")
_ERROR_RETRYABILITY = {
    "authentication_required": False,
    "rate_limited": True,
    "source_unavailable": False,
    "validation_failure": False,
}


def _duplicate_guard(pairs: list) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("portfolio worker frame contains duplicate keys")
        result[key] = value
    return result


def _load(frame: bytes, maximum: int, label: str) -> Dict[str, Any]:
    if type(frame) is not bytes or not frame or len(frame) > maximum:
        raise ValueError(f"portfolio worker {label} frame is invalid")
    try:
        value = json.loads(
            frame.decode("utf-8"),
            object_pairs_hook=_duplicate_guard,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError(f"portfolio worker {label} frame is invalid") from None
    if type(value) is not dict:
        raise ValueError(f"portfolio worker {label} frame is invalid")
    return value


def _dump(value: object, maximum: int, label: str) -> bytes:
    frame = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not frame or len(frame) > maximum:
        raise ValueError(f"portfolio worker {label} frame exceeds its limit")
    return frame


def _utc(value: object, label: str) -> datetime:
    if type(value) is datetime:
        parsed = value
    elif type(value) is str:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"portfolio {label} is invalid") from None
    else:
        raise ValueError(f"portfolio {label} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"portfolio {label} must be UTC")
    return parsed


def _decimal_text(
    value: object,
    label: str,
    *,
    optional: bool,
    nonnegative: bool = False,
    positive: bool = False,
) -> Optional[str]:
    if value is None:
        if optional:
            return None
        raise ValueError(f"portfolio {label} is required")
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        raise ValueError(f"portfolio {label} is invalid")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"portfolio {label} is invalid") from None
    if (
        not number.is_finite()
        or (nonnegative and number < 0)
        or (positive and number <= 0)
        or canonical_decimal(number) != value
    ):
        raise ValueError(f"portfolio {label} is invalid")
    return value


def portfolio_error_message(reason_code: str) -> str:
    if type(reason_code) is not str or reason_code not in _ERROR_RETRYABILITY:
        raise ValueError("portfolio worker reason code is unsupported")
    return f"portfolio source error: {reason_code}"


def _credential_count(value: object, label: str, *, maximum: int) -> int:
    if type(value) is not int or value < 0 or value > maximum:
        raise ValueError(f"portfolio worker {label} credential attestation is invalid")
    return value


@dataclass(frozen=True)
class PortfolioWorkerRequest:
    schema_version: int
    request_id: str
    operation: str

    def validate(self) -> None:
        if type(self) is not PortfolioWorkerRequest or set(vars(self)) != _REQUEST_KEYS:
            raise ValueError("portfolio worker request shape is invalid")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("portfolio worker schema version is invalid")
        validate_request_id(self.request_id)
        if self.operation != "portfolio_observation":
            raise ValueError("portfolio worker operation is invalid")

    def to_dict(self) -> Dict[str, Any]:
        self.validate()
        return {
            "operation": self.operation,
            "request_id": self.request_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class PortfolioAccount:
    source_account_id: str
    title: str
    observed_at: datetime


@dataclass(frozen=True)
class PortfolioPosition:
    source_account_id: str
    fund_code: str
    fund_name: str
    share_class: Optional[str]
    shares: str
    formal_nav: Optional[str]
    estimated_nav: Optional[str]
    observed_profit: Optional[str]
    observed_at: datetime


@dataclass(frozen=True)
class PortfolioObservationPayload:
    retrieved_at: datetime
    accounts: Tuple[PortfolioAccount, ...]
    positions: Tuple[PortfolioPosition, ...]


@dataclass(frozen=True)
class PortfolioWorkerResponse:
    schema_version: int
    request_id: str
    operation: str
    ok: bool
    payload: Optional[PortfolioObservationPayload]
    reason_code: Optional[str]
    retryable: Optional[bool]
    message: Optional[str]
    keychain_read_count: int
    keychain_mutation_attempt_count: int


def _validate_payload(payload: PortfolioObservationPayload) -> PortfolioObservationPayload:
    if type(payload) is not PortfolioObservationPayload:
        raise ValueError("portfolio payload must use the exact type")
    validate_exact_dataclass_state(payload, "portfolio payload")
    retrieved_at = _utc(payload.retrieved_at, "retrieval time")
    if (
        type(payload.accounts) is not tuple
        or len(payload.accounts) > MAX_PORTFOLIO_ACCOUNTS
        or type(payload.positions) is not tuple
        or len(payload.positions) > MAX_PORTFOLIO_POSITIONS
    ):
        raise ValueError("portfolio payload exceeds record limits")
    account_ids = []
    observed_by_account = {}
    for account in payload.accounts:
        if type(account) is not PortfolioAccount:
            raise ValueError("portfolio accounts must use the exact type")
        validate_exact_dataclass_state(account, "portfolio account")
        if (
            type(account.source_account_id) is not str
            or _ACCOUNT_ID.fullmatch(account.source_account_id) is None
        ):
            raise ValueError("portfolio account id is invalid")
        validate_private_name(account.title, "portfolio account title")
        observed_at = _utc(account.observed_at, "account observation time")
        if observed_at > retrieved_at:
            raise ValueError("portfolio account observation follows retrieval")
        account_ids.append(account.source_account_id)
        observed_by_account[account.source_account_id] = observed_at
    if len(account_ids) != len(set(account_ids)):
        raise ValueError("portfolio accounts contain duplicates")
    position_ids = []
    position_counts = {account_id: 0 for account_id in account_ids}
    for position in payload.positions:
        if type(position) is not PortfolioPosition:
            raise ValueError("portfolio positions must use the exact type")
        validate_exact_dataclass_state(position, "portfolio position")
        account_observed = observed_by_account.get(position.source_account_id)
        if account_observed is None:
            raise ValueError("portfolio position references an unknown account")
        if type(position.fund_code) is not str or _FUND_CODE.fullmatch(position.fund_code) is None:
            raise ValueError("portfolio fund code is invalid")
        validate_private_name(position.fund_name, "portfolio fund name")
        if position.share_class not in {None, "A", "C"}:
            raise ValueError("portfolio share class is invalid")
        _decimal_text(position.shares, "shares", optional=False, nonnegative=True)
        _decimal_text(position.formal_nav, "formal NAV", optional=True, positive=True)
        _decimal_text(position.estimated_nav, "estimated NAV", optional=True, positive=True)
        _decimal_text(position.observed_profit, "observed profit", optional=True)
        if _utc(position.observed_at, "position observation time") != account_observed:
            raise ValueError("portfolio position observation does not match its account")
        position_counts[position.source_account_id] += 1
        if position_counts[position.source_account_id] > MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT:
            raise ValueError("portfolio account exceeds its position limit")
        position_ids.append((position.source_account_id, position.fund_code))
    if len(position_ids) != len(set(position_ids)):
        raise ValueError("portfolio positions contain duplicates")
    return payload


def encode_portfolio_request(request: PortfolioWorkerRequest) -> bytes:
    return _dump(request.to_dict(), MAX_PORTFOLIO_REQUEST_BYTES, "request")


def decode_portfolio_request(frame: bytes) -> PortfolioWorkerRequest:
    value = _load(frame, MAX_PORTFOLIO_REQUEST_BYTES, "request")
    if set(value) != _REQUEST_KEYS:
        raise ValueError("portfolio worker request shape is invalid")
    request = PortfolioWorkerRequest(**value)
    request.validate()
    return request


def _identity(request: PortfolioWorkerRequest) -> Dict[str, Any]:
    request.validate()
    return {
        "operation": request.operation,
        "request_id": request.request_id,
        "schema_version": request.schema_version,
    }


def encode_portfolio_success(
    request: PortfolioWorkerRequest,
    payload: PortfolioObservationPayload,
    *,
    keychain_read_count: int,
    keychain_mutation_attempt_count: int,
) -> bytes:
    _validate_payload(payload)
    _credential_count(keychain_read_count, "read count", maximum=1)
    _credential_count(keychain_mutation_attempt_count, "mutation count", maximum=2)
    value = _identity(request)
    value.update(
        {
            "keychain_mutation_attempt_count": keychain_mutation_attempt_count,
            "keychain_read_count": keychain_read_count,
            "ok": True,
            "payload": {
                "accounts": [
                    {
                        "observed_at": item.observed_at.isoformat(),
                        "source_account_id": item.source_account_id,
                        "title": item.title,
                    }
                    for item in payload.accounts
                ],
                "positions": [
                    {
                        "estimated_nav": item.estimated_nav,
                        "formal_nav": item.formal_nav,
                        "fund_code": item.fund_code,
                        "fund_name": item.fund_name,
                        "observed_at": item.observed_at.isoformat(),
                        "observed_profit": item.observed_profit,
                        "share_class": item.share_class,
                        "shares": item.shares,
                        "source_account_id": item.source_account_id,
                    }
                    for item in payload.positions
                ],
                "retrieved_at": payload.retrieved_at.isoformat(),
            },
        }
    )
    return _dump(value, MAX_PORTFOLIO_RESPONSE_BYTES, "response")


def encode_portfolio_error(
    request: PortfolioWorkerRequest,
    reason_code: str,
    retryable: bool,
    *,
    keychain_read_count: int,
    keychain_mutation_attempt_count: int,
) -> bytes:
    if (
        reason_code not in _ERROR_RETRYABILITY
        or type(retryable) is not bool
        or retryable is not _ERROR_RETRYABILITY[reason_code]
    ):
        raise ValueError("portfolio worker error is invalid")
    _credential_count(keychain_read_count, "read count", maximum=1)
    _credential_count(keychain_mutation_attempt_count, "mutation count", maximum=2)
    value = _identity(request)
    value.update(
        {
            "keychain_mutation_attempt_count": keychain_mutation_attempt_count,
            "keychain_read_count": keychain_read_count,
            "message": portfolio_error_message(reason_code),
            "ok": False,
            "reason_code": reason_code,
            "retryable": retryable,
        }
    )
    return _dump(value, MAX_PORTFOLIO_RESPONSE_BYTES, "response")


def decode_portfolio_response(
    frame: bytes,
    request: PortfolioWorkerRequest,
) -> PortfolioWorkerResponse:
    value = _load(frame, MAX_PORTFOLIO_RESPONSE_BYTES, "response")
    identity = _identity(request)
    if any(value.get(key) != expected for key, expected in identity.items()):
        raise ValueError("portfolio worker response identity mismatch")
    if value.get("ok") is True:
        if set(value) != _RESPONSE_IDENTITY_KEYS | _ATTESTATION_KEYS | {"ok", "payload"}:
            raise ValueError("portfolio worker success shape is invalid")
        keychain_read_count = _credential_count(
            value["keychain_read_count"], "read count", maximum=1
        )
        keychain_mutation_attempt_count = _credential_count(
            value["keychain_mutation_attempt_count"], "mutation count", maximum=2
        )
        raw = value["payload"]
        if type(raw) is not dict or set(raw) != _PAYLOAD_KEYS:
            raise ValueError("portfolio worker payload shape is invalid")
        if type(raw["accounts"]) is not list or type(raw["positions"]) is not list:
            raise ValueError("portfolio worker record lists are invalid")
        accounts = []
        for item in raw["accounts"]:
            if type(item) is not dict or set(item) != _ACCOUNT_KEYS:
                raise ValueError("portfolio worker account shape is invalid")
            accounts.append(
                PortfolioAccount(
                    item["source_account_id"],
                    item["title"],
                    _utc(item["observed_at"], "account observation time"),
                )
            )
        positions = []
        for item in raw["positions"]:
            if type(item) is not dict or set(item) != _POSITION_KEYS:
                raise ValueError("portfolio worker position shape is invalid")
            positions.append(
                PortfolioPosition(
                    item["source_account_id"],
                    item["fund_code"],
                    item["fund_name"],
                    item["share_class"],
                    item["shares"],
                    item["formal_nav"],
                    item["estimated_nav"],
                    item["observed_profit"],
                    _utc(item["observed_at"], "position observation time"),
                )
            )
        payload = PortfolioObservationPayload(
            _utc(raw["retrieved_at"], "retrieval time"),
            tuple(accounts),
            tuple(positions),
        )
        _validate_payload(payload)
        return PortfolioWorkerResponse(
            **identity,
            ok=True,
            payload=payload,
            reason_code=None,
            retryable=None,
            message=None,
            keychain_read_count=keychain_read_count,
            keychain_mutation_attempt_count=keychain_mutation_attempt_count,
        )
    if value.get("ok") is not False or set(value) != _RESPONSE_IDENTITY_KEYS | {
        "ok",
        "reason_code",
        "retryable",
        "message",
    } | _ATTESTATION_KEYS:
        raise ValueError("portfolio worker error shape is invalid")
    keychain_read_count = _credential_count(
        value["keychain_read_count"], "read count", maximum=1
    )
    keychain_mutation_attempt_count = _credential_count(
        value["keychain_mutation_attempt_count"], "mutation count", maximum=2
    )
    reason = value["reason_code"]
    retryable = value["retryable"]
    if (
        reason not in _ERROR_RETRYABILITY
        or type(retryable) is not bool
        or retryable is not _ERROR_RETRYABILITY[reason]
        or value["message"] != portfolio_error_message(reason)
    ):
        raise ValueError("portfolio worker error is invalid")
    return PortfolioWorkerResponse(
        **identity,
        ok=False,
        payload=None,
        reason_code=reason,
        retryable=retryable,
        message=value["message"],
        keychain_read_count=keychain_read_count,
        keychain_mutation_attempt_count=keychain_mutation_attempt_count,
    )
