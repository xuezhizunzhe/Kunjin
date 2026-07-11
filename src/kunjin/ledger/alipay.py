from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kunjin.ledger.models import EvidenceLevel, ExtractedField, OcrBlock


MIN_CONFIDENCE = Decimal("0.80")

_AMOUNT_LABELS = ("支付金额", "订单金额")
_PAYMENT_STATUS = "支付成功"
_ORDER_TIME_LABEL = "订单时间"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_AMOUNT_VALUE = re.compile(
    r"^[\s]*(?P<sign>[-+]?)\s*(?P<currency>CNY|RMB|人民币|[¥￥])?\s*"
    r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
    r"\s*(?P<unit>元)?\s*$",
    re.IGNORECASE,
)
_DATE_TIME_VALUE = re.compile(
    r"(?<!\d)(?P<value>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})(?!\d)"
)


class AlipayParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return " ".join(normalized.split())


def _center_y(block: OcrBlock) -> Decimal:
    return block.y + block.height / Decimal("2")


def _center_x(block: OcrBlock) -> Decimal:
    return block.x + block.width / Decimal("2")


def _same_row(left: OcrBlock, right: OcrBlock) -> bool:
    tolerance = max(
        Decimal("0.025"),
        max(left.height, right.height) * Decimal("0.75"),
    )
    return abs(_center_y(left) - _center_y(right)) <= tolerance


def _raw_text(blocks: Sequence[OcrBlock]) -> str:
    return " | ".join(block.text for block in blocks)


def _minimum_confidence(blocks: Sequence[OcrBlock]) -> Decimal:
    return min(block.confidence for block in blocks)


def _parse_amount_value(text: str) -> Optional[Tuple[Decimal, bool, bool]]:
    matched = _AMOUNT_VALUE.fullmatch(text)
    if matched is None:
        return None
    try:
        value = Decimal(matched.group("amount").replace(",", ""))
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return (
        value,
        matched.group("sign") == "-",
        matched.group("currency") is not None or matched.group("unit") is not None,
    )


def _parse_amount_label(text: str) -> Optional[Tuple[str, Optional[str]]]:
    for label in _AMOUNT_LABELS:
        if text == label:
            return label, None
        if not text.startswith(label):
            continue
        remainder = text[len(label) :]
        if not remainder:
            return label, None
        if remainder.startswith(":"):
            return label, remainder[1:].strip()
        if remainder[0].isspace():
            inline_text = remainder.lstrip()
            if inline_text.startswith(":"):
                inline_text = inline_text[1:].strip()
            return label, inline_text
    return None


class AlipayPaymentParser:
    def parse(self, blocks: Iterable[OcrBlock]) -> Dict[str, ExtractedField]:
        source_blocks = list(blocks)
        normalized = [(block, _normalize_text(block.text)) for block in source_blocks]

        amount = self._extract_amount(normalized)
        order_time = self._extract_order_time(normalized)
        if amount is None or order_time is None:
            buy_record = self._extract_buy_record(normalized)
            if buy_record is not None:
                if amount is None:
                    amount = buy_record["amount"]
                if order_time is None:
                    order_time = buy_record["order_time"]

        if amount is None:
            raise AlipayParseError(
                "missing_required_field", "payment amount is unavailable"
            )

        if order_time is None:
            raise AlipayParseError(
                "missing_required_field", "order time is unavailable"
            )

        return {"amount": amount, "order_time": order_time}

    def _extract_amount(
        self, normalized: Sequence[Tuple[OcrBlock, str]]
    ) -> Optional[ExtractedField]:
        labeled = self._labeled_amount_candidates(normalized)
        if labeled:
            return self._amount_field(min(labeled, key=lambda candidate: candidate[0]))

        status_blocks = [
            block for block, text in normalized if text == _PAYMENT_STATUS
        ]
        status_candidates = []
        for status in status_blocks:
            for block, text in normalized:
                parsed = _parse_amount_value(text)
                if parsed is None or block is status:
                    continue
                value, has_negative_sign, has_currency_marker = parsed
                if not has_negative_sign and not has_currency_marker:
                    continue
                vertical_distance = abs(_center_y(status) - _center_y(block))
                horizontal_distance = abs(_center_x(status) - _center_x(block))
                if vertical_distance <= Decimal("0.20") and horizontal_distance <= Decimal(
                    "0.35"
                ):
                    status_candidates.append(
                        (
                            vertical_distance,
                            horizontal_distance,
                            value,
                            (status, block),
                        )
                    )
        if status_candidates:
            _, _, value, contributors = min(
                status_candidates,
                key=lambda candidate: (candidate[0], candidate[1]),
            )
            return self._build_amount_field(value, contributors)
        return None

    def _extract_buy_record(
        self, normalized: Sequence[Tuple[OcrBlock, str]]
    ) -> Optional[Dict[str, ExtractedField]]:
        candidates = []
        action_blocks = [block for block, text in normalized if text == "买入"]
        for action in action_blocks:
            amount_candidates = []
            date_candidates = []
            for block, text in normalized:
                if block is action:
                    continue

                parsed_amount = _parse_amount_value(text)
                if parsed_amount is not None:
                    value, _, has_currency_marker = parsed_amount
                    horizontal_distance = _center_x(block) - _center_x(action)
                    if (
                        has_currency_marker
                        and _same_row(action, block)
                        and horizontal_distance > 0
                        and horizontal_distance <= Decimal("0.75")
                    ):
                        amount_candidates.append(
                            (horizontal_distance, value, block)
                        )

                matched_time = _DATE_TIME_VALUE.fullmatch(text)
                if matched_time is not None:
                    vertical_distance = _center_y(action) - _center_y(block)
                    horizontal_distance = abs(_center_x(action) - _center_x(block))
                    if (
                        Decimal("0") < vertical_distance <= Decimal("0.08")
                        and horizontal_distance <= Decimal("0.80")
                        and block.x >= action.x - Decimal("0.03")
                    ):
                        date_candidates.append(
                            (
                                vertical_distance,
                                horizontal_distance,
                                matched_time.group("value"),
                                block,
                            )
                        )

            for amount_distance, value, amount_block in amount_candidates:
                for (
                    date_vertical_distance,
                    date_horizontal_distance,
                    raw_time,
                    date_block,
                ) in date_candidates:
                    candidates.append(
                        (
                            date_vertical_distance,
                            amount_distance,
                            date_horizontal_distance,
                            action,
                            amount_block,
                            value,
                            date_block,
                            raw_time,
                        )
                    )

        if not candidates:
            return None
        (
            _,
            _,
            _,
            action,
            amount_block,
            amount_value,
            date_block,
            raw_time,
        ) = min(candidates, key=lambda candidate: candidate[:3])
        return {
            "amount": self._build_amount_field(
                amount_value, (action, amount_block)
            ),
            "order_time": self._build_order_time_field(
                raw_time, (action, date_block)
            ),
        }

    def _labeled_amount_candidates(
        self, normalized: Sequence[Tuple[OcrBlock, str]]
    ) -> List[Tuple[Decimal, Decimal, Sequence[OcrBlock]]]:
        candidates = []
        for label_block, label_text in normalized:
            parsed_label = _parse_amount_label(label_text)
            if parsed_label is None:
                continue
            _, inline_text = parsed_label

            inline_amount = (
                _parse_amount_value(inline_text) if inline_text is not None else None
            )
            if inline_amount is not None:
                candidates.append(
                    (Decimal("0"), inline_amount[0], (label_block,))
                )

            for value_block, value_text in normalized:
                if value_block is label_block or not _same_row(label_block, value_block):
                    continue
                if _center_x(value_block) < _center_x(label_block):
                    continue
                parsed = _parse_amount_value(value_text)
                if parsed is None:
                    continue
                candidates.append(
                    (
                        abs(_center_x(value_block) - _center_x(label_block)),
                        parsed[0],
                        (label_block, value_block),
                    )
                )
        return candidates

    def _amount_field(
        self, candidate: Tuple[Decimal, Decimal, Sequence[OcrBlock]]
    ) -> ExtractedField:
        _, value, contributors = candidate
        return self._build_amount_field(value, contributors)

    def _build_amount_field(
        self, value: Decimal, contributors: Sequence[OcrBlock]
    ) -> ExtractedField:
        value = abs(value)
        if value <= 0:
            raise AlipayParseError(
                "invalid_field_value", "payment amount is invalid"
            )
        return ExtractedField(
            name="amount",
            raw_text=_raw_text(contributors),
            normalized_value=format(value, "f"),
            confidence=_minimum_confidence(contributors),
            evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
        )

    def _extract_order_time(
        self, normalized: Sequence[Tuple[OcrBlock, str]]
    ) -> Optional[ExtractedField]:
        candidates = []
        for label_block, label_text in normalized:
            if _ORDER_TIME_LABEL not in label_text:
                continue

            inline_text = label_text.split(_ORDER_TIME_LABEL, 1)[1].strip(" :")
            inline_match = _DATE_TIME_VALUE.search(inline_text)
            if inline_match is not None:
                candidates.append(
                    (Decimal("0"), inline_match.group("value"), (label_block,))
                )

            for value_block, value_text in normalized:
                if value_block is label_block or not _same_row(label_block, value_block):
                    continue
                if _center_x(value_block) < _center_x(label_block):
                    continue
                matched = _DATE_TIME_VALUE.search(value_text)
                if matched is None:
                    continue
                candidates.append(
                    (
                        abs(_center_x(value_block) - _center_x(label_block)),
                        matched.group("value"),
                        (label_block, value_block),
                    )
                )

        if not candidates:
            return None
        _, raw_value, contributors = min(candidates, key=lambda candidate: candidate[0])
        return self._build_order_time_field(raw_value, contributors)

    def _build_order_time_field(
        self, raw_value: str, contributors: Sequence[OcrBlock]
    ) -> ExtractedField:
        try:
            parsed = datetime.strptime(raw_value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=_SHANGHAI
            )
        except ValueError:
            raise AlipayParseError(
                "invalid_field_value", "order time is invalid"
            ) from None
        return ExtractedField(
            name="order_time",
            raw_text=_raw_text(contributors),
            normalized_value=parsed.isoformat(),
            confidence=_minimum_confidence(contributors),
            evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
        )


def requires_confirmation(fields: Dict[str, ExtractedField]) -> bool:
    required = ("amount", "order_time")
    return any(
        name not in fields or fields[name].confidence < MIN_CONFIDENCE
        for name in required
    )
