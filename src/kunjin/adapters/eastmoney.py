from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from kunjin.models import FundNavObservation, SectorObservation


FUND_CODE = re.compile(r"^\d{6}$")


class PublicDataError(RuntimeError):
    code = "public_data_error"


def _decimal(value: Any) -> Optional[Decimal]:
    if value in (None, "", "-"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PublicDataError(f"invalid numeric public-data value: {value!r}") from exc


class HttpsJsonClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def request_json(self, url: str, referer: str) -> Dict[str, Any]:
        if urllib.parse.urlparse(url).scheme != "https":
            raise PublicDataError("public data requires HTTPS")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": referer,
                "User-Agent": "KunJin/0.1 read-only research client",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PublicDataError(f"public data HTTP error: {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PublicDataError("public data network request failed") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicDataError("public data returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise PublicDataError("public data response is not an object")
        return payload


class EastmoneyFundClient:
    def __init__(self, http: Optional[HttpsJsonClient] = None) -> None:
        self.http = http or HttpsJsonClient()

    def fetch_nav_history(
        self, fund_code: str, page_size: int = 1000
    ) -> Tuple[Dict[str, Any], Optional[str], Optional[str], List[FundNavObservation]]:
        if not FUND_CODE.fullmatch(fund_code):
            raise ValueError(f"invalid fund code: {fund_code}")
        query = urllib.parse.urlencode(
            {
                "fundCode": fund_code,
                "pageIndex": "1",
                "pageSize": str(page_size),
                "startDate": "",
                "endDate": "",
            }
        )
        url = f"https://api.fund.eastmoney.com/f10/lsjz?{query}"
        payload = self.http.request_json(url, f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html")
        if payload.get("ErrCode") not in (None, 0):
            raise PublicDataError(str(payload.get("ErrMsg") or "fund NAV business error"))
        data = payload.get("Data")
        if not isinstance(data, dict) or not isinstance(data.get("LSJZList"), list):
            raise PublicDataError("fund NAV response is incomplete")
        retrieved_at = datetime.now(timezone.utc)
        observations: List[FundNavObservation] = []
        for item in data["LSJZList"]:
            if not isinstance(item, dict) or not item.get("FSRQ") or not item.get("DWJZ"):
                continue
            observation = FundNavObservation(
                fund_code=fund_code,
                nav_date=date.fromisoformat(str(item["FSRQ"])),
                unit_nav=_decimal(item["DWJZ"]) or Decimal("0"),
                accumulated_nav=_decimal(item.get("LJJZ")),
                daily_growth=_decimal(item.get("JZZZL")),
                source="eastmoney",
                retrieved_at=retrieved_at,
            )
            observation.validate()
            observations.append(observation)
        if not observations:
            raise PublicDataError("fund NAV history is empty")
        name = data.get("FundName") or payload.get("FundName")
        fund_type = data.get("FundType")
        return payload, None if name is None else str(name), None if fund_type is None else str(fund_type), observations


class EastmoneyMarketClient:
    def __init__(self, http: Optional[HttpsJsonClient] = None) -> None:
        self.http = http or HttpsJsonClient()

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
        url = f"https://push2.eastmoney.com/api/qt/clist/get?{query}"
        payload = self.http.request_json(url, "https://quote.eastmoney.com/center/boardlist.html")
        data = payload.get("data")
        items = None if not isinstance(data, dict) else data.get("diff")
        if not isinstance(items, list):
            raise PublicDataError("sector ranking response is incomplete")
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

