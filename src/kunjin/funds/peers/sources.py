from __future__ import annotations

import json
import re
from typing import Tuple

from kunjin.funds.html import FundParseError
from kunjin.funds.peers.models import DirectoryCandidate
from kunjin.funds.sources import TextResponse


PEER_DIRECTORY_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
PEER_DIRECTORY_REFERER = "https://fund.eastmoney.com/"
MAX_DIRECTORY_ROWS = 30_000


def _reject_constant(value: str) -> None:
    raise FundParseError(
        "malformed_peer_directory", f"invalid JSON constant: {value}"
    )


def parse_peer_directory(response: TextResponse) -> Tuple[DirectoryCandidate, ...]:
    text = response.text.lstrip("\ufeff").strip()
    match = re.fullmatch(r"var\s+r\s*=\s*(\[.*\])\s*;", text, re.DOTALL)
    if match is None:
        raise FundParseError("malformed_peer_directory")
    try:
        payload = json.loads(match.group(1), parse_constant=_reject_constant)
    except (json.JSONDecodeError, FundParseError) as exc:
        raise FundParseError("malformed_peer_directory") from exc
    if not isinstance(payload, list) or len(payload) > MAX_DIRECTORY_ROWS:
        raise FundParseError("invalid_peer_directory_size")

    candidates = []
    for row in payload:
        if (
            not isinstance(row, list)
            or len(row) != 5
            or not all(isinstance(value, str) for value in row)
        ):
            raise FundParseError("malformed_peer_directory_row")
        fund_code, _, fund_name, directory_type, _ = row
        if not directory_type.strip():
            continue
        candidate = DirectoryCandidate(
            fund_code=fund_code,
            fund_name=fund_name,
            directory_type=directory_type,
            source_url=response.final_url,
            source_checksum=response.checksum,
        )
        try:
            candidate.validate()
        except ValueError as exc:
            raise FundParseError("malformed_peer_directory_row") from exc
        candidates.append(candidate)
    return tuple(candidates)
