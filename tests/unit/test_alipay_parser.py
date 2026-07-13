import json
import unittest
from decimal import Decimal
from pathlib import Path

from kunjin.ledger.alipay import (
    AlipayParseError,
    AlipayPaymentParser,
    requires_confirmation,
)
from kunjin.ledger.models import EvidenceLevel, OcrBlock

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ledger"
    / "alipay_payment_blocks.json"
)


def block(
    text: str,
    *,
    confidence: str = "0.99",
    x: str = "0.10",
    y: str = "0.50",
    width: str = "0.20",
    height: str = "0.04",
) -> OcrBlock:
    return OcrBlock(
        text=text,
        confidence=Decimal(confidence),
        x=Decimal(x),
        y=Decimal(y),
        width=Decimal(width),
        height=Decimal(height),
    )


class AlipayPaymentParserTest(unittest.TestCase):
    def test_parses_only_directly_visible_payment_evidence(self) -> None:
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        blocks = [
            OcrBlock(
                text=row["text"],
                confidence=Decimal(row["confidence"]),
                x=Decimal(row["x"]),
                y=Decimal(row["y"]),
                width=Decimal(row["width"]),
                height=Decimal(row["height"]),
            )
            for row in payload["blocks"]
        ]

        fields = AlipayPaymentParser().parse(blocks)

        self.assertEqual(fields["amount"].normalized_value, "20.00")
        self.assertEqual(
            fields["order_time"].normalized_value,
            "2026-07-04T23:11:51+08:00",
        )
        self.assertEqual(
            fields["amount"].evidence_level,
            EvidenceLevel.TRANSACTION_CONFIRMED,
        )
        self.assertEqual(
            fields["order_time"].evidence_level,
            EvidenceLevel.TRANSACTION_CONFIRMED,
        )
        self.assertEqual(set(fields), {"amount", "order_time"})
        self.assertFalse(requires_confirmation(fields))

    def test_nfkc_normalizes_full_width_payment_text(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("支付成功", x="0.40", y="0.90"),
                block("－２０．００ 元", x="0.40", y="0.78"),
                block("订单时间", x="0.08", y="0.42"),
                block("２０２６－０７－０４ ２３：１１：５１", x="0.50", y="0.42"),
            ]
        )

        self.assertEqual(fields["amount"].normalized_value, "20.00")
        self.assertEqual(
            fields["order_time"].normalized_value,
            "2026-07-04T23:11:51+08:00",
        )

    def test_missing_amount_has_stable_error_code(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("订单时间", x="0.08", y="0.42"),
                    block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_invalid_order_time_has_stable_error_code(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("订单金额", x="0.08", y="0.60"),
                    block("20.00", x="0.50", y="0.60"),
                    block("订单时间", x="0.08", y="0.42"),
                    block("2026-02-30 23:11:51", x="0.50", y="0.42"),
                ]
            )

        self.assertEqual(raised.exception.code, "invalid_field_value")

    def test_low_confidence_field_is_retained_for_confirmation(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("支付金额", confidence="0.79", x="0.08", y="0.60"),
                block("20.00", x="0.50", y="0.60"),
                block("订单时间", x="0.08", y="0.42"),
                block("2026-07-04 23:11:51", x="0.50", y="0.42"),
            ]
        )

        self.assertEqual(fields["amount"].confidence, Decimal("0.79"))
        self.assertTrue(requires_confirmation(fields))

    def test_layout_anchors_ignore_unrelated_amounts_and_dates(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("账户余额", x="0.08", y="0.68"),
                block("999.00", x="0.50", y="0.68"),
                block("2025-01-01 08:00:00", x="0.50", y="0.55"),
                block("订单金额", x="0.08", y="0.60"),
                block("20.00", x="0.50", y="0.60"),
                block("订单时间", x="0.08", y="0.42"),
                block("2026-07-04 23:11:51", x="0.50", y="0.42"),
            ]
        )

        self.assertEqual(fields["amount"].normalized_value, "20.00")
        self.assertEqual(
            fields["order_time"].normalized_value,
            "2026-07-04T23:11:51+08:00",
        )

    def test_fund_like_text_never_creates_unsupported_fields(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("支付成功", x="0.40", y="0.90"),
                block("-20.00", x="0.40", y="0.78"),
                block("订单时间", x="0.08", y="0.42"),
                block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                block("基金代码 519755", x="0.08", y="0.34"),
                block("份额 11.32", x="0.08", y="0.28"),
                block("净值 1.7467", x="0.08", y="0.22"),
                block("手续费 0.00", x="0.08", y="0.16"),
            ]
        )

        self.assertEqual(set(fields), {"amount", "order_time"})

    def test_nearer_order_number_does_not_override_negative_payment_amount(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("支付成功", x="0.40", y="0.90"),
                block("123456", x="0.40", y="0.84"),
                block("-20.00", x="0.40", y="0.78"),
                block("订单时间", x="0.08", y="0.42"),
                block("2026-07-04 23:11:51", x="0.50", y="0.42"),
            ]
        )

        self.assertEqual(fields["amount"].normalized_value, "20.00")

    def test_non_payment_amount_labels_are_not_substring_matches(self) -> None:
        for misleading_label in ("待支付金额 999.00", "非支付金额:999.00"):
            with self.subTest(label=misleading_label):
                fields = AlipayPaymentParser().parse(
                    [
                        block(misleading_label, x="0.08", y="0.70"),
                        block("支付成功", x="0.40", y="0.90"),
                        block("-20.00", x="0.40", y="0.78"),
                        block("订单时间", x="0.08", y="0.42"),
                        block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                    ]
                )

                self.assertEqual(fields["amount"].normalized_value, "20.00")

    def test_negative_amount_without_payment_status_is_rejected(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("-20.00", x="0.40", y="0.78"),
                    block("订单时间", x="0.08", y="0.42"),
                    block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_explicit_currency_amount_near_payment_status_is_accepted(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("支付成功", confidence="0.79", x="0.40", y="0.90"),
                block("￥20.00", x="0.40", y="0.78"),
                block("订单时间", x="0.08", y="0.42"),
                block("2026-07-04 23:11:51", x="0.50", y="0.42"),
            ]
        )

        self.assertEqual(fields["amount"].normalized_value, "20.00")
        self.assertEqual(fields["amount"].confidence, Decimal("0.79"))
        self.assertEqual(fields["amount"].raw_text, "支付成功 | ￥20.00")

    def test_plain_number_near_payment_status_is_rejected(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("支付成功", x="0.40", y="0.90"),
                    block("123456", x="0.40", y="0.84"),
                    block("订单时间", x="0.08", y="0.42"),
                    block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_exact_label_with_delimiter_and_inline_amount_is_accepted(self) -> None:
        for labeled_amount in ("订单金额:20.00", "支付金额 20.00"):
            with self.subTest(labeled_amount=labeled_amount):
                fields = AlipayPaymentParser().parse(
                    [
                        block(labeled_amount, x="0.08", y="0.60"),
                        block("订单时间", x="0.08", y="0.42"),
                        block("2026-07-04 23:11:51", x="0.50", y="0.42"),
                    ]
                )

                self.assertEqual(fields["amount"].normalized_value, "20.00")

    def test_parses_strict_buy_transaction_row_template(self) -> None:
        fields = AlipayPaymentParser().parse(
            [
                block("某公募基金名称", x="0.08", y="0.58", width="0.50"),
                block("买入", confidence="0.98", x="0.08", y="0.50"),
                block("20.00元", confidence="0.97", x="0.68", y="0.50"),
                block(
                    "2026-07-04 23:11:51",
                    confidence="0.96",
                    x="0.42",
                    y="0.44",
                    width="0.42",
                ),
            ]
        )

        self.assertEqual(fields["amount"].normalized_value, "20.00")
        self.assertEqual(
            fields["order_time"].normalized_value,
            "2026-07-04T23:11:51+08:00",
        )
        self.assertEqual(fields["amount"].raw_text, "买入 | 20.00元")
        self.assertEqual(
            fields["order_time"].raw_text,
            "买入 | 2026-07-04 23:11:51",
        )
        self.assertEqual(fields["amount"].confidence, Decimal("0.97"))
        self.assertEqual(fields["order_time"].confidence, Decimal("0.96"))
        self.assertEqual(set(fields), {"amount", "order_time"})

    def test_sell_transaction_row_is_not_treated_as_buy(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("卖出", x="0.08", y="0.50"),
                    block("20.00元", x="0.68", y="0.50"),
                    block("2026-07-04 23:11:51", x="0.42", y="0.44"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_unanchored_currency_amount_and_date_are_rejected(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("20.00元", x="0.68", y="0.50"),
                    block("2026-07-04 23:11:51", x="0.42", y="0.44"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_buy_row_rejects_distant_unlabelled_date(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("买入", x="0.08", y="0.50"),
                    block("20.00元", x="0.68", y="0.50"),
                    block("2026-07-04 23:11:51", x="0.42", y="0.30"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")

    def test_buy_row_requires_explicit_currency_marker(self) -> None:
        with self.assertRaises(AlipayParseError) as raised:
            AlipayPaymentParser().parse(
                [
                    block("买入", x="0.08", y="0.50"),
                    block("20.00", x="0.68", y="0.50"),
                    block("2026-07-04 23:11:51", x="0.42", y="0.44"),
                ]
            )

        self.assertEqual(raised.exception.code, "missing_required_field")


if __name__ == "__main__":
    unittest.main()
