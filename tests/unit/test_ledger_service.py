import hashlib
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from kunjin.ledger.alipay import AlipayPaymentParser
from kunjin.ledger.models import OcrBlock, TransactionType
from kunjin.ledger.ocr import OcrResponseError
from kunjin.ledger.service import LedgerImportError, LedgerService
from kunjin.ledger.store import LedgerStore
from kunjin.paths import RuntimePaths
from kunjin.storage.repository import Repository


class FakeOcrClient:
    def __init__(self, blocks):
        self.blocks = blocks
        self.paths = []

    def recognize(self, image_path: Path):
        self.paths.append(image_path)
        return self.blocks


class FailingOcrClient:
    def recognize(self, image_path: Path):
        raise OcrResponseError("local OCR failed")


class FailingParser:
    def __init__(self, error):
        self.error = error

    def parse(self, blocks):
        raise self.error


class LedgerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.paths = RuntimePaths(
            database=root / "data" / "kunjin.db",
            snapshots=root / "data" / "snapshots",
            logs=root / "state" / "logs",
        ).ensure()
        self.repository = Repository(self.paths.database)
        self.repository.migrate()
        self.store = LedgerStore(self.repository)
        self.now = datetime(2026, 7, 11, 4, 0, tzinfo=timezone.utc)
        self.ocr = FakeOcrClient(
            [
                OcrBlock(
                    text="订单金额 ￥20.00",
                    confidence=Decimal("0.99"),
                    x=Decimal("0.1"),
                    y=Decimal("0.2"),
                    width=Decimal("0.5"),
                    height=Decimal("0.05"),
                ),
                OcrBlock(
                    text="订单时间 2026-07-04 23:11:51",
                    confidence=Decimal("0.98"),
                    x=Decimal("0.1"),
                    y=Decimal("0.4"),
                    width=Decimal("0.7"),
                    height=Decimal("0.05"),
                ),
            ]
        )
        self.service = LedgerService(
            paths=self.paths,
            store=self.store,
            ocr_client=self.ocr,
            parser=AlipayPaymentParser(),
            now=lambda: self.now,
        )
        self.source = root / "payment.JPG"
        self.source.write_bytes(b"synthetic payment screenshot")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_import_copies_private_hash_named_file_and_creates_draft(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")

        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        managed = self.paths.imports / f"{digest}.jpg"
        self.assertTrue(managed.is_file())
        self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o600)
        self.assertEqual(self.ocr.paths, [managed])
        self.assertEqual(draft.source_document_id, 1)
        self.assertEqual(draft.fund_code, "519755")
        self.assertEqual(draft.amount, Decimal("20.00"))
        self.assertFalse(hasattr(draft, "managed_path"))
        self.assertEqual(
            draft.order_time.isoformat(), "2026-07-04T23:11:51+08:00"
        )
        self.assertEqual(
            draft.field_evidence,
            {
                "amount": "transaction_confirmed",
                "fund_code": "user_confirmed",
                "order_time": "transaction_confirmed",
            },
        )

    def test_reimport_reuses_document_and_managed_file(self) -> None:
        first = self.service.import_image(self.source, fund_code_hint="519755")
        second = self.service.import_image(self.source, fund_code_hint="519755")

        self.assertEqual(first.source_document_id, second.source_document_id)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(list(self.paths.imports.iterdir())), 1)
        with self.repository.connect() as connection:
            document_count = connection.execute(
                "SELECT COUNT(*) FROM imported_documents"
            ).fetchone()[0]
        self.assertEqual(document_count, 1)

    def test_import_rejects_unavailable_unsupported_and_invalid_hint(self) -> None:
        with self.assertRaises(LedgerImportError) as unavailable:
            self.service.import_image(self.source.with_name("missing.jpg"))
        self.assertEqual(unavailable.exception.code, "source_unavailable")

        unsupported = self.source.with_suffix(".gif")
        unsupported.write_bytes(b"gif")
        with self.assertRaises(LedgerImportError) as rejected:
            self.service.import_image(unsupported)
        self.assertEqual(rejected.exception.code, "unsupported_image")

        with self.assertRaises(LedgerImportError) as invalid_hint:
            self.service.import_image(self.source, fund_code_hint="123")
        self.assertEqual(invalid_hint.exception.code, "invalid_fund_code")

    def test_import_rejects_managed_symlink_outside_private_directory(self) -> None:
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        outside = self.paths.database.parent.parent / "outside.jpg"
        outside.write_bytes(b"do not touch")
        outside.chmod(0o644)
        (self.paths.imports / f"{digest}.jpg").symlink_to(outside)

        with self.assertRaises(LedgerImportError) as raised:
            self.service.import_image(self.source)

        self.assertEqual(raised.exception.code, "unsafe_document_path")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)

    def test_import_rejects_source_symlink(self) -> None:
        linked_source = self.source.with_name("linked.jpg")
        linked_source.symlink_to(self.source)

        with self.assertRaises(LedgerImportError) as raised:
            self.service.import_image(linked_source)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertEqual(list(self.paths.imports.iterdir()), [])

    def test_import_rejects_symlinked_imports_root(self) -> None:
        self.paths.imports.rmdir()
        outside = self.paths.database.parent.parent / "outside-imports"
        outside.mkdir()
        self.paths.imports.symlink_to(outside, target_is_directory=True)

        with self.assertRaises(LedgerImportError) as raised:
            self.service.import_image(self.source)

        self.assertEqual(raised.exception.code, "unsafe_document_path")
        self.assertEqual(list(outside.iterdir()), [])

    def test_import_rejects_existing_managed_file_with_wrong_hash(self) -> None:
        digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        managed = self.paths.imports / f"{digest}.jpg"
        managed.write_bytes(b"different content")

        with self.assertRaises(LedgerImportError) as raised:
            self.service.import_image(self.source)

        self.assertEqual(raised.exception.code, "unsafe_document_path")
        self.assertEqual(managed.read_bytes(), b"different content")
        self.assert_no_import_rows()

    def test_partial_copy_failure_removes_staging_file(self) -> None:
        original_write = __import__("os").write
        calls = 0

        def fail_after_partial_write(file_descriptor, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(file_descriptor, data[:5])
            raise OSError("injected write failure")

        with mock.patch("kunjin.ledger.service.os.write", fail_after_partial_write):
            with self.assertRaises(LedgerImportError) as raised:
                self.service.import_image(self.source)

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertEqual(list(self.paths.imports.iterdir()), [])
        self.assert_no_import_rows()

    def test_ocr_parser_and_store_failures_leave_no_import_state(self) -> None:
        cases = [
            (
                LedgerService(
                    self.paths,
                    self.store,
                    FailingOcrClient(),
                    AlipayPaymentParser(),
                    now=lambda: self.now,
                ),
                OcrResponseError,
            ),
            (
                LedgerService(
                    self.paths,
                    self.store,
                    self.ocr,
                    FailingParser(ValueError("contains private path")),
                    now=lambda: self.now,
                ),
                LedgerImportError,
            ),
        ]
        for service, error_type in cases:
            with self.subTest(error_type=error_type):
                with self.assertRaises(error_type) as raised:
                    service.import_image(self.source)
                if isinstance(raised.exception, LedgerImportError):
                    self.assertEqual(raised.exception.code, "invalid_field")
                    self.assertNotIn(str(self.source), str(raised.exception))
                self.assertEqual(list(self.paths.imports.iterdir()), [])
                self.assert_no_import_rows()

        with mock.patch.object(
            self.store, "commit_import", side_effect=RuntimeError("database failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "database failed"):
                self.service.import_image(self.source)
        self.assertEqual(list(self.paths.imports.iterdir()), [])
        self.assert_no_import_rows()

    def test_confirmation_requires_fund_code(self) -> None:
        draft = self.service.import_image(self.source)

        with self.assertRaises(LedgerImportError) as raised:
            self.service.confirm_draft(draft.id, {})

        self.assertEqual(raised.exception.code, "missing_fund_code")
        self.assertEqual(self.store.get_draft(draft.id).status, "pending")

    def test_confirmation_override_changes_only_that_field_evidence(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")

        transaction = self.service.confirm_draft(draft.id, {"amount": "21.00"})

        self.assertEqual(transaction.amount, Decimal("21.00"))
        self.assertEqual(transaction.fund_code, "519755")
        self.assertEqual(
            transaction.field_evidence,
            {
                "amount": "user_confirmed",
                "fund_code": "user_confirmed",
                "order_time": "transaction_confirmed",
            },
        )

    def test_confirmation_without_overrides_preserves_imported_evidence(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")

        transaction = self.service.confirm_draft(draft.id, {})

        self.assertEqual(transaction.fund_code, "519755")
        self.assertEqual(transaction.amount, Decimal("20.00"))
        self.assertEqual(
            transaction.field_evidence,
            {
                "amount": "transaction_confirmed",
                "fund_code": "user_confirmed",
                "order_time": "transaction_confirmed",
            },
        )

    def test_delete_document_removes_only_managed_copy_and_preserves_evidence(
        self,
    ) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")
        transaction = self.service.confirm_draft(draft.id, {})
        managed_path = self.store.document_path(draft.source_document_id)

        self.assertTrue(self.service.delete_document(draft.source_document_id))

        self.assertFalse(managed_path.exists())
        self.assertIsNone(self.store.document_path(draft.source_document_id))
        stored = self.store.list_transactions("519755")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].id, transaction.id)
        self.assertEqual(stored[0].field_evidence, transaction.field_evidence)
        with self.repository.connect() as connection:
            document = connection.execute(
                "SELECT status, managed_path FROM imported_documents WHERE id = ?",
                (draft.source_document_id,),
            ).fetchone()
            self.assertEqual(document["status"], "deleted")
            self.assertIsNone(document["managed_path"])
            self.assertGreater(
                connection.execute(
                    "SELECT COUNT(*) FROM ocr_fields WHERE document_id = ?",
                    (draft.source_document_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM transaction_drafts WHERE source_document_id = ?",
                    (draft.source_document_id,),
                ).fetchone()[0],
                1,
            )

    def test_delete_document_rejects_external_path_and_target_symlink(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")
        managed_path = self.store.document_path(draft.source_document_id)
        outside = self.paths.database.parent.parent / "outside.jpg"
        outside.write_bytes(b"private external file")
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE imported_documents SET managed_path = ? WHERE id = ?",
                (str(outside), draft.source_document_id),
            )

        with self.assertRaises(LedgerImportError) as external:
            self.service.delete_document(draft.source_document_id)
        self.assertEqual(external.exception.code, "unsafe_document_path")
        self.assertEqual(outside.read_bytes(), b"private external file")

        managed_path.unlink()
        managed_path.symlink_to(outside)
        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE imported_documents SET managed_path = ? WHERE id = ?",
                (str(managed_path), draft.source_document_id),
            )
        with self.assertRaises(LedgerImportError) as symlink:
            self.service.delete_document(draft.source_document_id)
        self.assertEqual(symlink.exception.code, "unsafe_document_path")
        self.assertTrue(managed_path.is_symlink())
        self.assertEqual(outside.read_bytes(), b"private external file")

    def test_delete_document_rejects_symlinked_imports_root(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")
        original_imports = self.paths.imports
        moved_imports = original_imports.with_name("real-imports")
        original_imports.rename(moved_imports)
        original_imports.symlink_to(moved_imports, target_is_directory=True)

        with self.assertRaises(LedgerImportError) as raised:
            self.service.delete_document(draft.source_document_id)

        self.assertEqual(raised.exception.code, "unsafe_document_path")
        stored_name = self.store.document_path(draft.source_document_id).name
        self.assertTrue((moved_imports / stored_name).exists())

    def test_delete_document_can_retry_after_database_failure(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")
        managed_path = self.store.document_path(draft.source_document_id)

        with mock.patch.object(
            self.store,
            "mark_document_deleted",
            side_effect=RuntimeError("database failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "database failed"):
                self.service.delete_document(draft.source_document_id)

        self.assertFalse(managed_path.exists())
        self.assertEqual(
            self.store.document_path(draft.source_document_id), managed_path
        )
        self.assertTrue(self.service.delete_document(draft.source_document_id))
        self.assertIsNone(self.store.document_path(draft.source_document_id))

    def test_delete_document_returns_false_for_unknown_or_deleted_document(
        self,
    ) -> None:
        self.assertFalse(self.service.delete_document(999))
        draft = self.service.import_image(self.source, fund_code_hint="519755")
        self.assertTrue(self.service.delete_document(draft.source_document_id))
        self.assertFalse(self.service.delete_document(draft.source_document_id))

    def test_confirmation_rejects_unknown_or_invalid_override(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")

        for overrides in ({"unknown": "value"}, {"amount": "not-a-number"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(LedgerImportError) as raised:
                    self.service.confirm_draft(draft.id, overrides)
                self.assertEqual(raised.exception.code, "invalid_field")

    def test_confirmation_rejects_empty_or_naive_datetime_override(self) -> None:
        draft = self.service.import_image(self.source, fund_code_hint="519755")

        for overrides in ({"fee": ""}, {"confirmation_time": "2026-07-11T12:00:00"}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(LedgerImportError) as raised:
                    self.service.confirm_draft(draft.id, overrides)
                self.assertEqual(raised.exception.code, "invalid_field")
                self.assertNotIn("fee", self.store.get_draft(draft.id).field_evidence)

    def test_manual_entry_is_user_confirmed_without_source_document(self) -> None:
        transaction = self.service.add_manual_transaction(
            transaction_type="subscription",
            fund_code="519755",
            fund_name="测试基金",
            amount="20.00",
            order_time="2026-07-04T23:11:51+08:00",
        )

        self.assertIsNotNone(transaction.id)
        self.assertIsNone(transaction.source_document_id)
        self.assertEqual(transaction.transaction_type, TransactionType.SUBSCRIPTION)
        self.assertEqual(transaction.amount, Decimal("20.00"))
        self.assertEqual(transaction.evidence_level.value, "user_confirmed")
        self.assertEqual(
            transaction.field_evidence,
            {
                "amount": "user_confirmed",
                "fund_code": "user_confirmed",
                "fund_name": "user_confirmed",
                "order_time": "user_confirmed",
                "transaction_type": "user_confirmed",
            },
        )

    def test_naive_manual_datetime_and_now_are_rejected(self) -> None:
        with self.assertRaises(LedgerImportError) as naive_manual:
            self.service.add_manual_transaction(
                transaction_type="subscription",
                fund_code="519755",
                order_time="2026-07-04T23:11:51",
            )
        self.assertEqual(naive_manual.exception.code, "invalid_field")

        naive_service = LedgerService(
            self.paths,
            self.store,
            self.ocr,
            AlipayPaymentParser(),
            now=lambda: datetime(2026, 7, 11, 4, 0),
        )
        with self.assertRaises(LedgerImportError) as naive_now:
            naive_service.import_image(self.source)
        self.assertEqual(naive_now.exception.code, "invalid_field")
        self.assertEqual(list(self.paths.imports.iterdir()), [])
        self.assert_no_import_rows()

    def assert_no_import_rows(self) -> None:
        with self.repository.connect() as connection:
            for table in ("imported_documents", "ocr_fields", "transaction_drafts"):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
