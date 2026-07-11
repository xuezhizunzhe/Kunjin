import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.ledger.models import (
    EvidenceLevel,
    ExtractedField,
    LedgerDraft,
    LedgerTransaction,
    TransactionType,
)
from kunjin.ledger.store import LedgerStateError, LedgerStore
from kunjin.storage.repository import Repository


class LedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.store = LedgerStore(self.repository)
        self.now = datetime(2026, 7, 11, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def draft(self, document_id=None, draft_id=None, status="pending") -> LedgerDraft:
        return LedgerDraft(
            id=draft_id,
            source_document_id=document_id,
            transaction_type=TransactionType.SUBSCRIPTION,
            fund_code="519755",
            fund_name=None,
            amount=Decimal("20.00"),
            shares=None,
            nav=None,
            fee=None,
            order_time=datetime(2026, 7, 4, 23, 11, 51, tzinfo=timezone.utc),
            confirmation_time=None,
            evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
            field_evidence={"amount": EvidenceLevel.TRANSACTION_CONFIRMED.value},
            status=status,
            created_at=self.now,
        )

    def add_document(self, sha256="a" * 64, managed_path="/private/payment.jpg") -> int:
        return self.store.add_document(
            sha256=sha256,
            original_name="payment.jpg",
            managed_path=managed_path,
            document_type="alipay_payment",
            imported_at=self.now,
        )

    def test_draft_confirmation_is_atomic_and_single_use(self) -> None:
        document_id = self.add_document()
        draft_id = self.store.add_draft(self.draft(document_id=document_id))

        transaction = self.store.confirm_draft(draft_id, self.now)

        self.assertEqual(transaction.fund_code, "519755")
        self.assertEqual(transaction.amount, Decimal("20.00"))
        self.assertEqual(self.store.get_draft(draft_id).status, "confirmed")
        with self.assertRaises(LedgerStateError):
            self.store.confirm_draft(draft_id, self.now)
        self.assertEqual(len(self.store.list_transactions()), 1)

    def test_get_and_replace_pending_draft_round_trip(self) -> None:
        draft_id = self.store.add_draft(self.draft())
        stored = self.store.get_draft(draft_id)
        replacement = LedgerDraft(
            **{
                **stored.__dict__,
                "fund_name": "测试基金",
                "amount": Decimal("25.50"),
                "evidence_level": EvidenceLevel.USER_CONFIRMED,
            }
        )

        updated = self.store.replace_pending_draft(replacement)

        self.assertEqual(updated.id, draft_id)
        self.assertEqual(updated.fund_name, "测试基金")
        self.assertEqual(updated.amount, Decimal("25.50"))
        self.store.confirm_draft(draft_id, self.now)
        with self.assertRaisesRegex(LedgerStateError, "draft is not pending"):
            self.store.replace_pending_draft(replacement)

    def test_document_hash_is_reused_and_deleted_document_is_restored(self) -> None:
        document_id = self.add_document()
        self.assertEqual(self.add_document(), document_id)
        self.assertEqual(self.store.document_path(document_id), Path("/private/payment.jpg"))

        old_path = self.store.mark_document_deleted(document_id, self.now)

        self.assertEqual(old_path, Path("/private/payment.jpg"))
        self.assertIsNone(self.store.document_path(document_id))
        restored_id = self.store.add_document(
            sha256="a" * 64,
            original_name="restored.jpg",
            managed_path="/private/restored.jpg",
            document_type="alipay_payment",
            imported_at=self.now + timedelta(seconds=1),
        )
        self.assertEqual(restored_id, document_id)
        self.assertEqual(self.store.document_path(document_id), Path("/private/restored.jpg"))

    def test_list_drafts_filters_by_status(self) -> None:
        first_id = self.store.add_draft(self.draft())
        second_id = self.store.add_draft(self.draft())
        self.store.confirm_draft(second_id, self.now)

        self.assertEqual([draft.id for draft in self.store.list_drafts("pending")], [first_id])
        self.assertEqual([draft.id for draft in self.store.list_drafts("confirmed")], [second_id])

    def test_add_and_list_transactions_round_trip_and_filter(self) -> None:
        later = LedgerTransaction(
            id=None,
            source_document_id=None,
            transaction_type=TransactionType.SUBSCRIPTION,
            fund_code="519755",
            fund_name="测试基金",
            amount=Decimal("20.00"),
            shares=Decimal("11.32"),
            nav=Decimal("1.7668"),
            fee=Decimal("0"),
            order_time=self.now + timedelta(days=1),
            confirmation_time=None,
            evidence_level=EvidenceLevel.USER_CONFIRMED,
            field_evidence={"amount": EvidenceLevel.USER_CONFIRMED.value},
            created_at=self.now,
        )
        earlier = LedgerTransaction(
            **{
                **later.__dict__,
                "fund_code": "000001",
                "order_time": self.now,
            }
        )

        later_stored = self.store.add_transaction(later)
        earlier_stored = self.store.add_transaction(earlier)

        self.assertIsNotNone(later_stored.id)
        self.assertEqual(later_stored.amount, Decimal("20.00"))
        self.assertEqual(
            [item.id for item in self.store.list_transactions()],
            [earlier_stored.id, later_stored.id],
        )
        filtered = self.store.list_transactions("519755")
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].shares, Decimal("11.32"))
        self.assertEqual(
            filtered[0].field_evidence,
            {"amount": EvidenceLevel.USER_CONFIRMED.value},
        )

    def test_invalid_write_values_are_rejected(self) -> None:
        invalid = LedgerDraft(**{**self.draft().__dict__, "amount": Decimal("-1")})
        with self.assertRaisesRegex(ValueError, "amount cannot be negative"):
            self.store.add_draft(invalid)

        missing_code_id = self.store.add_draft(
            LedgerDraft(**{**self.draft().__dict__, "fund_code": None})
        )
        with self.assertRaisesRegex(LedgerStateError, "fund code is required"):
            self.store.confirm_draft(missing_code_id, self.now)

    def test_invalid_field_evidence_is_rejected_for_drafts_and_transactions(self) -> None:
        invalid_draft = LedgerDraft(
            **{**self.draft().__dict__, "field_evidence": {"amount": "unverified"}}
        )
        with self.assertRaisesRegex(ValueError, "invalid field evidence level"):
            self.store.add_draft(invalid_draft)

        invalid_transaction = LedgerTransaction(
            id=None,
            source_document_id=None,
            transaction_type=TransactionType.SUBSCRIPTION,
            fund_code="519755",
            fund_name=None,
            amount=Decimal("20.00"),
            shares=None,
            nav=None,
            fee=None,
            order_time=self.now,
            confirmation_time=None,
            evidence_level=EvidenceLevel.USER_CONFIRMED,
            field_evidence={"amount": "unverified"},
            created_at=self.now,
        )
        with self.assertRaisesRegex(ValueError, "invalid field evidence level"):
            self.store.add_transaction(invalid_transaction)

    def test_invalid_stored_field_evidence_is_rejected_on_read(self) -> None:
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO transaction_drafts(
                    transaction_type, fund_code, evidence_level,
                    field_evidence_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    TransactionType.SUBSCRIPTION.value,
                    "519755",
                    EvidenceLevel.USER_CONFIRMED.value,
                    '{"amount":"unverified"}',
                    "pending",
                    self.now.isoformat(),
                ),
            )
            draft_id = int(cursor.lastrowid)

        with self.assertRaisesRegex(ValueError, "invalid field evidence level"):
            self.store.get_draft(draft_id)

    def test_ocr_fields_upsert_and_validate_confidence_and_evidence(self) -> None:
        document_id = self.add_document()
        self.store.upsert_ocr_fields(
            document_id,
            [
                ExtractedField(
                    name="amount",
                    raw_text="订单金额 20.00元",
                    normalized_value="20.00",
                    confidence=Decimal("0.98"),
                    evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
                )
            ],
        )
        self.store.upsert_ocr_fields(
            document_id,
            [
                ExtractedField(
                    name="amount",
                    raw_text="订单金额 20元",
                    normalized_value="20",
                    confidence=Decimal("0.99"),
                    evidence_level=EvidenceLevel.USER_CONFIRMED,
                )
            ],
        )

        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ocr_fields WHERE document_id = ?", (document_id,)
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_text"], "订单金额 20元")
        self.assertEqual(rows[0]["confidence"], "0.99")
        self.assertEqual(rows[0]["evidence_level"], EvidenceLevel.USER_CONFIRMED.value)

        with self.assertRaisesRegex(ValueError, "confidence"):
            self.store.upsert_ocr_fields(
                document_id,
                [
                    ExtractedField(
                        name="amount",
                        raw_text="20",
                        normalized_value="20",
                        confidence=Decimal("1.01"),
                        evidence_level=EvidenceLevel.USER_CONFIRMED,
                    )
                ],
            )
        with self.assertRaisesRegex(ValueError, "invalid evidence level"):
            self.store.upsert_ocr_fields(
                document_id,
                [
                    ExtractedField(
                        name="amount",
                        raw_text="20",
                        normalized_value="20",
                        confidence=Decimal("0.9"),
                        evidence_level="unverified",
                    )
                ],
            )

    def test_list_ocr_fields_round_trips_in_stable_name_order(self) -> None:
        document_id = self.add_document()
        fields = [
            ExtractedField(
                name="order_time",
                raw_text="订单时间 2026-07-04 23:11:51",
                normalized_value="2026-07-04T23:11:51+08:00",
                confidence=Decimal("0.98"),
                evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
            ),
            ExtractedField(
                name="amount",
                raw_text="订单金额 20.00元",
                normalized_value="20.00",
                confidence=Decimal("0.99"),
                evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
            ),
        ]
        self.store.upsert_ocr_fields(document_id, fields)

        stored = self.store.list_ocr_fields(document_id)

        self.assertEqual([item.name for item in stored], ["amount", "order_time"])
        self.assertEqual(stored[0].normalized_value, "20.00")
        self.assertEqual(stored[0].confidence, Decimal("0.99"))
        self.assertEqual(
            stored[0].evidence_level, EvidenceLevel.TRANSACTION_CONFIRMED
        )

    def test_list_ocr_fields_rejects_invalid_stored_values(self) -> None:
        document_id = self.add_document()
        self.store.upsert_ocr_fields(
            document_id,
            [
                ExtractedField(
                    name="amount",
                    raw_text="订单金额 20.00元",
                    normalized_value="20.00",
                    confidence=Decimal("0.99"),
                    evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
                )
            ],
        )
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE ocr_fields SET confidence = 'not-a-number' WHERE document_id = ?",
                (document_id,),
            )

        with self.assertRaisesRegex(ValueError, "stored OCR field"):
            self.store.list_ocr_fields(document_id)

    def test_list_ocr_fields_rejects_invalid_stored_evidence(self) -> None:
        document_id = self.add_document()
        self.store.upsert_ocr_fields(
            document_id,
            [
                ExtractedField(
                    name="amount",
                    raw_text="订单金额 20.00元",
                    normalized_value="20.00",
                    confidence=Decimal("0.99"),
                    evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
                )
            ],
        )
        with self.repository.connect() as connection, connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE ocr_fields SET evidence_level = 'unverified' WHERE document_id = ?",
                (document_id,),
            )

        with self.assertRaisesRegex(ValueError, "stored OCR field"):
            self.store.list_ocr_fields(document_id)

    def test_commit_import_rolls_back_document_fields_and_draft_together(self) -> None:
        fields = [
            ExtractedField(
                name="amount",
                raw_text="订单金额 20.00元",
                normalized_value="20.00",
                confidence=Decimal("0.98"),
                evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
            )
        ]
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                CREATE TRIGGER reject_import_draft
                BEFORE INSERT ON transaction_drafts
                BEGIN
                    SELECT RAISE(ABORT, 'reject import draft');
                END
                """
            )

        with self.assertRaisesRegex(Exception, "reject import draft"):
            self.store.commit_import(
                sha256="b" * 64,
                original_name="payment.jpg",
                managed_path="/private/payment.jpg",
                document_type="alipay_payment",
                imported_at=self.now,
                fields=fields,
                draft=self.draft(document_id=None),
            )

        with self.repository.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM imported_documents").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM ocr_fields").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transaction_drafts").fetchone()[0],
                0,
            )

    def test_missing_draft_and_document_return_none(self) -> None:
        self.assertIsNone(self.store.get_draft(999))
        self.assertIsNone(self.store.document_path(999))
        self.assertIsNone(self.store.mark_document_deleted(999, self.now))


if __name__ == "__main__":
    unittest.main()
