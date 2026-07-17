from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from kunjin.decision.models import TRANSIENT_SOURCE_ERRORS, SourceErrorCode
from kunjin.funds.sources import FundSourceError
from kunjin.models import FundNavObservation, SectorObservation

FUND_CODE = re.compile(r"^\d{6}$")
FUND_NAV_PAGE_SIZE = 20
FUND_NAV_MAX_PAGES = 50


class PublicDataError(FundSourceError):
    code = "public_data_error"


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PublicDataError(
            "public data contains an invalid numeric value",
            reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
        ) from exc


def _public_data_error(exception: BaseException) -> PublicDataError:
    cause = exception.reason if isinstance(exception, urllib.error.URLError) else exception
    if isinstance(cause, socket.gaierror):
        reason = SourceErrorCode.DNS_FAILURE
    elif isinstance(cause, (TimeoutError, socket.timeout)):
        reason = SourceErrorCode.NETWORK_TIMEOUT
    elif isinstance(cause, ssl.SSLCertVerificationError):
        reason = SourceErrorCode.VALIDATION_FAILURE
    else:
        reason = SourceErrorCode.TRANSIENT_NETWORK_FAILURE
    return PublicDataError(
        f"public source error: {reason.value}",
        reason_code=reason.value,
        retryable=reason in TRANSIENT_SOURCE_ERRORS,
    )


def _http_error(exception: urllib.error.HTTPError) -> PublicDataError:
    if exception.code == 404:
        reason = SourceErrorCode.HTTP_NOT_FOUND
    elif exception.code == 410:
        reason = SourceErrorCode.HTTP_GONE
    elif exception.code in {408, 429} or 500 <= exception.code <= 599:
        reason = SourceErrorCode.TRANSIENT_NETWORK_FAILURE
    else:
        reason = SourceErrorCode.HTTP_4XX
    return PublicDataError(
        f"public source error: {reason.value}",
        reason_code=reason.value,
        retryable=reason in TRANSIENT_SOURCE_ERRORS,
    )


class HttpsJsonClient:
    def __init__(self, timeout_seconds: int = 20, retries: int = 1) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def request_json(self, url: str, referer: str) -> Dict[str, Any]:
        if urllib.parse.urlparse(url).scheme != "https":
            raise PublicDataError(
                "public data requires HTTPS",
                reason_code=SourceErrorCode.UNSAFE_URL.value,
            )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": referer,
                "User-Agent": "KunJin/0.1 read-only research client",
            },
            method="GET",
        )
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                error = _http_error(exc)
                if error.retryable and attempt < self.retries:
                    continue
                raise error from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                http.client.RemoteDisconnected,
                ConnectionResetError,
                http.client.IncompleteRead,
                http.client.BadStatusLine,
                socket.gaierror,
                ssl.SSLCertVerificationError,
            ) as exc:
                error = _public_data_error(exc)
                if error.retryable and attempt < self.retries:
                    continue
                raise error from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PublicDataError(
                    "public data returned malformed JSON",
                    reason_code=SourceErrorCode.DECODE_FAILURE.value,
                ) from exc
        if not isinstance(payload, dict):
            raise PublicDataError(
                "public data response is not an object",
                reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
            )
        return payload


class EastmoneyFundClient:
    def __init__(self, http: Optional[HttpsJsonClient] = None) -> None:
        self.http = http or HttpsJsonClient()

    def fetch_nav_history(
        self,
        fund_code: str,
        page_size: int = FUND_NAV_PAGE_SIZE,
        max_pages: int = FUND_NAV_MAX_PAGES,
    ) -> Tuple[Dict[str, Any], Optional[str], Optional[str], List[FundNavObservation]]:
        payload, name, fund_type, observations, _actions = (
            self.fetch_nav_history_with_actions(fund_code, page_size, max_pages)
        )
        return payload, name, fund_type, observations

    def fetch_nav_history_with_actions(
        self,
        fund_code: str,
        page_size: int = FUND_NAV_PAGE_SIZE,
        max_pages: int = FUND_NAV_MAX_PAGES,
    ) -> Tuple[
        Dict[str, Any],
        Optional[str],
        Optional[str],
        List[FundNavObservation],
        Tuple[str, ...],
    ]:
        if not FUND_CODE.fullmatch(fund_code):
            raise ValueError(f"invalid fund code: {fund_code}")
        if not 1 <= page_size <= FUND_NAV_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {FUND_NAV_PAGE_SIZE}")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")

        retrieved_at = datetime.now(timezone.utc)
        observations: List[FundNavObservation] = []
        corporate_actions: List[str] = []
        first_payload: Optional[Dict[str, Any]] = None
        name: Optional[Any] = None
        fund_type: Optional[Any] = None
        total_count: Optional[int] = None
        previous_nav_date: Optional[date] = None

        for page_index in range(1, max_pages + 1):
            query = urllib.parse.urlencode(
                {
                    "fundCode": fund_code,
                    "pageIndex": str(page_index),
                    "pageSize": str(page_size),
                    "startDate": "",
                    "endDate": "",
                }
            )
            url = f"https://api.fund.eastmoney.com/f10/lsjz?{query}"
            payload = self.http.request_json(
                url, f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html"
            )
            if first_payload is None:
                first_payload = payload
            if payload.get("ErrCode") not in (None, 0):
                raise PublicDataError("fund NAV business error")
            data = payload.get("Data")
            if not isinstance(data, dict) or not isinstance(data.get("LSJZList"), list):
                raise PublicDataError(
                    "fund NAV response is incomplete",
                    reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
                )
            items = data["LSJZList"]
            name = name or data.get("FundName") or payload.get("FundName")
            fund_type = fund_type or data.get("FundType")
            if total_count is None and payload.get("TotalCount") is not None:
                total_count = int(payload["TotalCount"])

            for item in items:
                if not isinstance(item, dict) or not item.get("FSRQ") or not item.get("DWJZ"):
                    raise PublicDataError(
                        "fund NAV row is incomplete",
                        reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
                    )
                action_value = item.get("FHFCZ")
                if "FHFCZ" not in item or not isinstance(action_value, str):
                    action_state = "unknown"
                elif action_value.strip():
                    action_state = "present"
                else:
                    action_state = "none"
                try:
                    nav_date = date.fromisoformat(str(item["FSRQ"]))
                except ValueError:
                    raise PublicDataError(
                        "fund NAV date is invalid",
                        reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
                    ) from None
                if previous_nav_date is not None and nav_date >= previous_nav_date:
                    raise PublicDataError(
                        "fund NAV dates are not strictly descending",
                        reason_code=SourceErrorCode.VALIDATION_FAILURE.value,
                    )
                previous_nav_date = nav_date
                observation = FundNavObservation(
                    fund_code=fund_code,
                    nav_date=nav_date,
                    unit_nav=_decimal(item["DWJZ"]) or Decimal("0"),
                    accumulated_nav=_decimal(item.get("LJJZ")),
                    daily_growth=_decimal(item.get("JZZZL")),
                    source="eastmoney",
                    retrieved_at=retrieved_at,
                    corporate_action_state=action_state,
                )
                observation.validate()
                observations.append(observation)
                corporate_actions.append(action_state)

            if len(items) < page_size or (
                total_count is not None and len(observations) >= total_count
            ):
                break
        if not observations:
            raise PublicDataError("fund NAV history is empty")
        assert first_payload is not None
        return (
            first_payload,
            None if name is None else str(name),
            None if fund_type is None else str(fund_type),
            observations,
            tuple(corporate_actions),
        )


class EastmoneyMarketClient:
    def __init__(self, http: Optional[HttpsJsonClient] = None) -> None:
        self.http = http or HttpsJsonClient()

    def _fetch_sector_payload(
        self, url: str, referer: str
    ) -> Tuple[Dict[str, Any], List[Any]]:
        payload = self.http.request_json(url, referer)
        data = payload.get("data")
        items = None if not isinstance(data, dict) else data.get("diff")
        if not isinstance(items, list):
            raise PublicDataError("sector ranking response is incomplete")
        return payload, items

    def fetch_sectors(self, sector_kind: str) -> Tuple[Dict[str, Any], List[SectorObservation]]:
        if sector_kind not in {"industry", "concept"}:
            raise ValueError("sector kind must be industry or concept")
        market_filter = "m:90+t:2" if sector_kind == "industry" else "m:90+t:3"
        query = urllib.parse.urlencode(
            {
                "pn": "1",
                "pz": "500",
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": market_filter,
                "fields": "f12,f14,f3,f8,f104,f105",
            }
        )
        referer = "https://quote.eastmoney.com/center/boardlist.html"
        primary_url = f"https://push2.eastmoney.com/api/qt/clist/get?{query}"
        fallback_url = f"https://push2delay.eastmoney.com/api/qt/clist/get?{query}"
        try:
            payload, items = self._fetch_sector_payload(primary_url, referer)
        except PublicDataError as primary_error:
            try:
                payload, items = self._fetch_sector_payload(fallback_url, referer)
            except PublicDataError as fallback_error:
                raise PublicDataError(
                    "sector ranking endpoints failed: "
                    f"primary: {primary_error}; fallback: {fallback_error}"
                ) from fallback_error
        retrieved_at = datetime.now(timezone.utc)
        observations: List[SectorObservation] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            observation = SectorObservation(
                sector_code=str(item.get("f12") or ""),
                sector_name=str(item.get("f14") or ""),
                sector_kind=sector_kind,
                pct_change=_decimal(item.get("f3")),
                turnover_rate=_decimal(item.get("f8")),
                advancers=None if item.get("f104") in (None, "-") else int(item["f104"]),
                decliners=None if item.get("f105") in (None, "-") else int(item["f105"]),
                source="eastmoney",
                retrieved_at=retrieved_at,
            )
            observation.validate()
            observations.append(observation)
        return payload, observations
