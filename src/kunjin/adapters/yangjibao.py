from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from kunjin.models import AccountObservation, PositionObservation
from kunjin.security.keychain import KeychainTokenStore

DEFAULT_BASE_URL = "https://browser-plug-api.yangjibao.com"
YANGJIBAO_BROWSER_PLUGIN_SIGNING_SECRET = "YxmKSrQR4uoJ5lOoWIhcbd7SlUEh9OOc"
EXACT_GET_PATHS = {
    "/qr_code",
    "/user_account",
    "/account_collect",
    "/fund_hold",
    "/income_data",
    "/income_line_data",
}
PREFIX_GET_PATHS = {"/qr_code_state/"}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")


class YangjibaoError(RuntimeError):
    code = "yangjibao_error"


class InsecureTransportError(YangjibaoError):
    code = "insecure_transport"


class DisallowedEndpointError(YangjibaoError):
    code = "disallowed_endpoint"


class AuthenticationRequiredError(YangjibaoError):
    code = "authentication_required"


class RateLimitedError(YangjibaoError):
    code = "rate_limited"


class RemoteResponseError(YangjibaoError):
    code = "remote_response_error"


@dataclass(frozen=True)
class QrLoginChallenge:
    challenge_id: str
    qr_content: str


def generate_signature(path: str, token: str, timestamp: int, signing_secret: str) -> str:
    sign_path = path.split("?", 1)[0]
    value = f"{sign_path}{token or ''}{timestamp}{signing_secret}"
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RemoteResponseError(f"invalid numeric value: {value!r}") from exc


def _share_class(name: str) -> Optional[str]:
    match = re.search(r"(?:混合|联接|指数|债券|股票|基金)?([AC])$", name.strip())
    return None if match is None else match.group(1)


class YangjibaoClient:
    def __init__(
        self,
        token_store: KeychainTokenStore,
        base_url: str = DEFAULT_BASE_URL,
        signing_secret: str = YANGJIBAO_BROWSER_PLUGIN_SIGNING_SECRET,
        timeout_seconds: int = 20,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https":
            raise InsecureTransportError("Yangjibao requires HTTPS")
        self.token_store = token_store
        self.base_url = base_url.rstrip("/")
        self.signing_secret = signing_secret
        self.timeout_seconds = timeout_seconds

    def _validate_path(self, path: str) -> None:
        path_only = path.split("?", 1)[0]
        if path_only in EXACT_GET_PATHS:
            return
        for prefix in PREFIX_GET_PATHS:
            if path_only.startswith(prefix):
                identifier = path_only[len(prefix) :]
                if identifier and SAFE_IDENTIFIER.fullmatch(identifier):
                    return
        raise DisallowedEndpointError(f"GET endpoint is not allowlisted: {path_only}")

    def _request_json(
        self,
        path: str,
        query: Optional[Dict[str, str]] = None,
        token_required: bool = True,
    ) -> Any:
        self._validate_path(path)
        token = self.token_store.load() or ""
        if token_required and not token:
            raise AuthenticationRequiredError("Yangjibao authorization is required")
        timestamp = int(time.time())
        signature = generate_signature(path, token, timestamp, self.signing_secret)
        encoded_query = "" if not query else "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            self.base_url + path + encoded_query,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Request-Time": str(timestamp),
                "Request-Sign": signature,
                "Authorization": token,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise AuthenticationRequiredError("Yangjibao authorization expired") from exc
            if exc.code == 429:
                raise RateLimitedError("Yangjibao request rate limited") from exc
            raise RemoteResponseError(f"Yangjibao HTTP error: {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RemoteResponseError("Yangjibao network request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteResponseError("Yangjibao returned malformed JSON") from exc

        if not isinstance(payload, dict):
            raise RemoteResponseError("Yangjibao response is not an object")
        if payload.get("code") != 200:
            raise RemoteResponseError(str(payload.get("message") or "Yangjibao business error"))
        return payload.get("data")

    def start_qr_login(self) -> QrLoginChallenge:
        data = self._request_json("/qr_code", token_required=False)
        if not isinstance(data, dict) or not data.get("id") or not data.get("url"):
            raise RemoteResponseError("Yangjibao QR response is incomplete")
        return QrLoginChallenge(str(data["id"]), str(data["url"]))

    def poll_qr_login(self, challenge_id: str, timeout_seconds: int = 120) -> None:
        if not SAFE_IDENTIFIER.fullmatch(challenge_id):
            raise RemoteResponseError("invalid QR challenge id")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self._request_json(
                f"/qr_code_state/{challenge_id}", token_required=False
            )
            if isinstance(data, dict) and str(data.get("state")) == "2":
                token = str(data.get("token") or "")
                if not token:
                    raise RemoteResponseError("QR login succeeded without a token")
                self.token_store.save(token)
                return
            time.sleep(3)
        raise RemoteResponseError("Yangjibao QR login timed out")

    def render_qr(self, qr_content: str) -> bool:
        try:
            import qrcode  # type: ignore
        except ImportError:
            return False
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        return True


    def list_accounts(self) -> Tuple[Any, List[AccountObservation]]:
        data = self._request_json("/user_account")
        items = data.get("list", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            raise RemoteResponseError("Yangjibao account list is malformed")
        observed_at = datetime.now(timezone.utc)
        accounts = [
            AccountObservation(
                source="yangjibao",
                source_account_id=str(item.get("id") or ""),
                title=str(item.get("title") or "未命名账户"),
                observed_at=observed_at,
            )
            for item in items
            if isinstance(item, dict)
        ]
        for account in accounts:
            account.validate()
        return data, accounts

    def list_holdings(
        self, account_id: str, observed_at: Optional[datetime] = None
    ) -> Tuple[Any, List[PositionObservation]]:
        if not SAFE_IDENTIFIER.fullmatch(account_id):
            raise RemoteResponseError("invalid account id")
        data = self._request_json("/fund_hold", query={"account_id": account_id})
        if not isinstance(data, list):
            raise RemoteResponseError("Yangjibao holdings are malformed")
        timestamp = observed_at or datetime.now(timezone.utc)
        positions: List[PositionObservation] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            nav_info = item.get("nv_info") if isinstance(item.get("nv_info"), dict) else {}
            name = str(item.get("short_name") or item.get("name") or "")
            estimated_nav = (
                nav_info.get("gsz") or nav_info.get("vgsz") or nav_info.get("zsgz")
            )
            position = PositionObservation(
                source_account_id=account_id,
                fund_code=str(item.get("code") or ""),
                fund_name=name,
                share_class=_share_class(name),
                shares=_decimal(item.get("hold_share")) or Decimal("0"),
                formal_nav=_decimal(item.get("last_net")),
                estimated_nav=_decimal(estimated_nav),
                observed_profit=_decimal(item.get("hold_earn")),
                observed_at=timestamp,
            )
            position.validate()
            positions.append(position)
        return data, positions
