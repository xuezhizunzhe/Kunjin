from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from kunjin.ledger.models import (
    EvidenceLevel,
    ExtractedField,
    LedgerDraft,
    LedgerTransaction,
    TransactionType,
)
from kunjin.storage.repository import Repository


class LedgerStateError(ValueError):
    code = "ledger_state_error"


DOCUMENT_SELECT_SQL = "SELECT * FROM imported_documents WHERE sha256 = ?"
DOCUMENT_INSERT_SQL = """
INSERT INTO imported_documents(
    sha256, original_name, managed_path, document_type, imported_at
) VALUES (?, ?, ?, ?, ?)
"""
DOCUMENT_RESTORE_SQL = """
UPDATE imported_documents
SET original_name = ?, managed_path = ?, document_type = ?, imported_at = ?,
    status = 'active', deleted_at = NULL
WHERE id = ?
"""
DOCUMENT_REUSE_SQL = """
UPDATE imported_documents
SET original_name = ?, managed_path = ?, document_type = ?, imported_at = ?
WHERE id = ? AND status = 'active'
"""
OCR_FIELD_UPSERT_SQL = """
INSERT INTO ocr_fields(
    document_id, field_name, raw_text, normalized_value, confidence, evidence_level
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(document_id, field_name) DO UPDATE SET
    raw_text = excluded.raw_text,
    normalized_value = excluded.normalized_value,
    confidence = excluded.confidence,
    evidence_level = excluded.evidence_level
"""
DRAFT_SELECT_SQL = "SELECT * FROM transaction_drafts WHERE id = ?"
DRAFT_UPDATE_SQL = """
UPDATE transaction_drafts
SET transaction_type = ?, fund_code = ?, fund_name = ?, amount = ?, shares = ?,
    nav = ?, fee = ?, order_time = ?, confirmation_time = ?,
    evidence_level = ?, field_evidence_json = ?
WHERE id = ? AND status = 'pending'
"""
TRANSACTION_INSERT_SQL = """
INSERT INTO transactions(
    source_document_id, transaction_type, fund_code, fund_name,
    amount, shares, nav, fee, order_time, confirmation_time,
    evidence_level, field_evidence_json, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
DRAFT_CONFIRM_SQL = """
UPDATE transaction_drafts
SET status = 'confirmed', confirmed_at = ?
WHERE id = ? AND status = 'pending'
"""
DOCUMENT_DELETE_SQL = """
UPDATE imported_documents
SET managed_path = NULL, status = 'deleted', deleted_at = ?
WHERE id = ? AND status = 'active'
"""

_FUND_CODE = re.compile(r"^\d{6}$")
_DRAFT_STATUSES = {"pending", "confirmed", "rejected"}
_DOCUMENT_TYPES = {"alipay_payment", "unknown"}


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _datetime_text(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _optional_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _optional_datetime(value: Optional[str]) -> Optional[datetime]:
    return None if value is None else datetime.fromisoformat(str(value))


def _evidence_json(evidence: Dict[str, str]) -> str:
    normalized = _normalize_field_evidence(evidence, "field evidence")
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _decode_evidence(value: str) -> Dict[str, str]:
    decoded = json.loads(value)
    return _normalize_field_evidence(decoded, "stored field evidence")


def _normalize_field_evidence(evidence, label: str) -> Dict[str, str]:
    if not isinstance(evidence, dict):
        raise ValueError(f"{label} must be an object")
    normalized = {}
    for key, value in evidence.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{label} keys and values must be strings")
        try:
            normalized[key] = EvidenceLevel(value).value
        except ValueError as error:
            raise ValueError("invalid field evidence level") from error
    return normalized


class LedgerStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def add_document(
        self,
        sha256: str,
        original_name: str,
        managed_path: str,
        document_type: str,
        imported_at: datetime,
    ) -> int:
        if document_type not in _DOCUMENT_TYPES:
            raise ValueError("invalid document type")
        if not sha256 or not original_name or not managed_path:
            raise ValueError("document hash, name, and managed path are required")
        with self.repository.connect() as connection, connection:
            row = connection.execute(DOCUMENT_SELECT_SQL, (sha256,)).fetchone()
            if row is None:
                cursor = connection.execute(
                    DOCUMENT_INSERT_SQL,
                    (sha256, original_name, managed_path, document_type, imported_at.isoformat()),
                )
                return int(cursor.lastrowid)
            document_id = int(row["id"])
            if row["status"] == "deleted":
                connection.execute(
                    DOCUMENT_RESTORE_SQL,
                    (
                        original_name,
                        managed_path,
                        document_type,
                        imported_at.isoformat(),
                        document_id,
                    ),
                )
            return document_id

    def upsert_ocr_fields(
        self, document_id: int, fields: Iterable[ExtractedField]
    ) -> None:
        values = [
            (document_id,) + value for value in self._ocr_field_values(fields)
        ]
        with self.repository.connect() as connection, connection:
            connection.executemany(OCR_FIELD_UPSERT_SQL, values)

    def list_ocr_fields(self, document_id: int) -> List[ExtractedField]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ocr_fields WHERE document_id = ? ORDER BY field_name, id",
                (document_id,),
            ).fetchall()

        fields = []
        for row in rows:
            try:
                name = str(row["field_name"])
                raw_text = str(row["raw_text"])
                confidence = Decimal(str(row["confidence"]))
                evidence_level = EvidenceLevel(str(row["evidence_level"]))
                if (
                    not name
                    or not raw_text
                    or not confidence.is_finite()
                    or confidence < 0
                    or confidence > 1
                ):
                    raise ValueError
                normalized_value = row["normalized_value"]
                fields.append(
                    ExtractedField(
                        name=name,
                        raw_text=raw_text,
                        normalized_value=(
                            None
                            if normalized_value is None
                            else str(normalized_value)
                        ),
                        confidence=confidence,
                        evidence_level=evidence_level,
                    )
                )
            except (InvalidOperation, TypeError, ValueError):
                raise ValueError("stored OCR field is invalid") from None
        return fields

    def commit_import(
        self,
        sha256: str,
        original_name: str,
        managed_path: str,
        document_type: str,
        imported_at: datetime,
        fields: Iterable[ExtractedField],
        draft: LedgerDraft,
    ) -> LedgerDraft:
        if document_type not in _DOCUMENT_TYPES:
            raise ValueError("invalid document type")
        if not sha256 or not original_name or not managed_path:
            raise ValueError("document hash, name, and managed path are required")
        if draft.id is not None or draft.source_document_id is not None:
            raise ValueError("import draft must not already be stored")
        self._validate_draft(draft, require_pending=True)
        ocr_values = self._ocr_field_values(fields)

        with self.repository.connect() as connection, connection:
            row = connection.execute(DOCUMENT_SELECT_SQL, (sha256,)).fetchone()
            if row is None:
                cursor = connection.execute(
                    DOCUMENT_INSERT_SQL,
                    (
                        sha256,
                        original_name,
                        managed_path,
                        document_type,
                        imported_at.isoformat(),
                    ),
                )
                document_id = int(cursor.lastrowid)
            else:
                document_id = int(row["id"])
                if row["status"] == "deleted":
                    connection.execute(
                        DOCUMENT_RESTORE_SQL,
                        (
                            original_name,
                            managed_path,
                            document_type,
                            imported_at.isoformat(),
                            document_id,
                        ),
                    )
                else:
                    connection.execute(
                        DOCUMENT_REUSE_SQL,
                        (
                            original_name,
                            managed_path,
                            document_type,
                            imported_at.isoformat(),
                            document_id,
                        ),
                    )

            connection.executemany(
                OCR_FIELD_UPSERT_SQL,
                [(document_id,) + value for value in ocr_values],
            )
            stored_draft = replace(draft, source_document_id=document_id)
            cursor = connection.execute(
                """
                INSERT INTO transaction_drafts(
                    source_document_id, transaction_type, fund_code, fund_name,
                    amount, shares, nav, fee, order_time, confirmation_time,
                    evidence_level, field_evidence_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._draft_values(stored_draft)
                + (stored_draft.status, stored_draft.created_at.isoformat()),
            )
            row = connection.execute(
                DRAFT_SELECT_SQL, (int(cursor.lastrowid),)
            ).fetchone()
            return self._row_to_draft(row)

    def add_draft(self, draft: LedgerDraft) -> int:
        self._validate_draft(draft)
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO transaction_drafts(
                    source_document_id, transaction_type, fund_code, fund_name,
                    amount, shares, nav, fee, order_time, confirmation_time,
                    evidence_level, field_evidence_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._draft_values(draft) + (draft.status, draft.created_at.isoformat()),
            )
            return int(cursor.lastrowid)

    def get_draft(self, draft_id: int) -> Optional[LedgerDraft]:
        with self.repository.connect() as connection:
            row = connection.execute(DRAFT_SELECT_SQL, (draft_id,)).fetchone()
        return None if row is None else self._row_to_draft(row)

    def list_drafts(self, status: str = "pending") -> List[LedgerDraft]:
        if status not in _DRAFT_STATUSES:
            raise ValueError("invalid draft status")
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transaction_drafts WHERE status = ? ORDER BY created_at, id",
                (status,),
            ).fetchall()
        return [self._row_to_draft(row) for row in rows]

    def replace_pending_draft(self, draft: LedgerDraft) -> LedgerDraft:
        if draft.id is None:
            raise ValueError("draft id is required")
        self._validate_draft(draft, require_pending=True)
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                DRAFT_UPDATE_SQL, self._draft_update_values(draft) + (draft.id,)
            )
            if cursor.rowcount != 1:
                raise LedgerStateError("draft is not pending")
            row = connection.execute(DRAFT_SELECT_SQL, (draft.id,)).fetchone()
        return self._row_to_draft(row)

    def confirm_draft(self, draft_id: int, confirmed_at: datetime) -> LedgerTransaction:
        with self.repository.connect() as connection, connection:
            row = connection.execute(DRAFT_SELECT_SQL, (draft_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise LedgerStateError("draft is not pending")
            draft = self._row_to_draft(row)
            if draft.fund_code is None:
                raise LedgerStateError("fund code is required before confirmation")
            transaction = LedgerTransaction(
                id=None,
                source_document_id=draft.source_document_id,
                transaction_type=draft.transaction_type,
                fund_code=draft.fund_code,
                fund_name=draft.fund_name,
                amount=draft.amount,
                shares=draft.shares,
                nav=draft.nav,
                fee=draft.fee,
                order_time=draft.order_time,
                confirmation_time=draft.confirmation_time,
                evidence_level=draft.evidence_level,
                field_evidence=draft.field_evidence,
                created_at=confirmed_at,
            )
            self._validate_transaction(transaction)
            cursor = connection.execute(
                TRANSACTION_INSERT_SQL, self._transaction_values(transaction)
            )
            update = connection.execute(DRAFT_CONFIRM_SQL, (confirmed_at.isoformat(), draft_id))
            if update.rowcount != 1:
                raise LedgerStateError("draft is not pending")
            inserted = connection.execute(
                "SELECT * FROM transactions WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
            return self._row_to_transaction(inserted)

    def add_transaction(self, transaction: LedgerTransaction) -> LedgerTransaction:
        self._validate_transaction(transaction)
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                TRANSACTION_INSERT_SQL, self._transaction_values(transaction)
            )
            return replace(transaction, id=int(cursor.lastrowid))

    def list_transactions(self, fund_code: Optional[str] = None) -> List[LedgerTransaction]:
        if fund_code is not None and not _FUND_CODE.fullmatch(fund_code):
            raise ValueError("invalid fund code")
        sql = "SELECT * FROM transactions"
        parameters = ()
        if fund_code is not None:
            sql += " WHERE fund_code = ?"
            parameters = (fund_code,)
        sql += " ORDER BY COALESCE(confirmation_time, order_time, created_at), id"
        with self.repository.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._row_to_transaction(row) for row in rows]

    def document_path(self, document_id: int) -> Optional[Path]:
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT managed_path FROM imported_documents WHERE id = ? AND status = 'active'",
                (document_id,),
            ).fetchone()
        if row is None or row["managed_path"] is None:
            return None
        return Path(str(row["managed_path"]))

    def mark_document_deleted(
        self, document_id: int, deleted_at: datetime
    ) -> Optional[Path]:
        with self.repository.connect() as connection, connection:
            row = connection.execute(
                "SELECT managed_path FROM imported_documents WHERE id = ? AND status = 'active'",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            old_path = None if row["managed_path"] is None else Path(str(row["managed_path"]))
            cursor = connection.execute(
                DOCUMENT_DELETE_SQL, (deleted_at.isoformat(), document_id)
            )
            if cursor.rowcount != 1:
                raise LedgerStateError("document is not active")
            return old_path

    @staticmethod
    def _evidence_value(value: EvidenceLevel) -> str:
        try:
            return EvidenceLevel(value).value
        except (TypeError, ValueError) as error:
            raise ValueError("invalid evidence level") from error

    @staticmethod
    def _transaction_type_value(value: TransactionType) -> str:
        try:
            return TransactionType(value).value
        except (TypeError, ValueError) as error:
            raise ValueError("invalid transaction type") from error

    def _validate_draft(self, draft: LedgerDraft, require_pending: bool = False) -> None:
        self._transaction_type_value(draft.transaction_type)
        self._evidence_value(draft.evidence_level)
        if draft.status not in _DRAFT_STATUSES:
            raise ValueError("invalid draft status")
        if require_pending and draft.status != "pending":
            raise LedgerStateError("draft is not pending")
        self._validate_fund_code(draft.fund_code, required=False)
        self._validate_numbers(draft.amount, draft.shares, draft.nav, draft.fee)
        _evidence_json(draft.field_evidence)

    def _validate_transaction(self, transaction: LedgerTransaction) -> None:
        self._transaction_type_value(transaction.transaction_type)
        self._evidence_value(transaction.evidence_level)
        self._validate_fund_code(transaction.fund_code, required=True)
        self._validate_numbers(
            transaction.amount, transaction.shares, transaction.nav, transaction.fee
        )
        _evidence_json(transaction.field_evidence)

    @staticmethod
    def _validate_fund_code(fund_code: Optional[str], required: bool) -> None:
        if fund_code is None:
            if required:
                raise ValueError("fund code is required")
            return
        if not _FUND_CODE.fullmatch(fund_code):
            raise ValueError("invalid fund code")

    @staticmethod
    def _validate_numbers(*values: Optional[Decimal]) -> None:
        names = ("amount", "shares", "nav", "fee")
        for name, value in zip(names, values):
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")

    def _ocr_field_values(self, fields: Iterable[ExtractedField]) -> List[tuple]:
        values = []
        for item in fields:
            if not item.name or not item.raw_text:
                raise ValueError("OCR field name and raw text are required")
            evidence_level = self._evidence_value(item.evidence_level)
            if item.confidence < 0 or item.confidence > 1:
                raise ValueError("OCR confidence must be between zero and one")
            values.append(
                (
                    item.name,
                    item.raw_text,
                    item.normalized_value,
                    str(item.confidence),
                    evidence_level,
                )
            )
        return values

    def _draft_values(self, draft: LedgerDraft) -> tuple:
        return (
            draft.source_document_id,
            self._transaction_type_value(draft.transaction_type),
            draft.fund_code,
            draft.fund_name,
            _decimal_text(draft.amount),
            _decimal_text(draft.shares),
            _decimal_text(draft.nav),
            _decimal_text(draft.fee),
            _datetime_text(draft.order_time),
            _datetime_text(draft.confirmation_time),
            self._evidence_value(draft.evidence_level),
            _evidence_json(draft.field_evidence),
        )

    def _transaction_values(self, transaction: LedgerTransaction) -> tuple:
        return (
            transaction.source_document_id,
            self._transaction_type_value(transaction.transaction_type),
            transaction.fund_code,
            transaction.fund_name,
            _decimal_text(transaction.amount),
            _decimal_text(transaction.shares),
            _decimal_text(transaction.nav),
            _decimal_text(transaction.fee),
            _datetime_text(transaction.order_time),
            _datetime_text(transaction.confirmation_time),
            self._evidence_value(transaction.evidence_level),
            _evidence_json(transaction.field_evidence),
            transaction.created_at.isoformat(),
        )

    def _draft_update_values(self, draft: LedgerDraft) -> tuple:
        return self._draft_values(draft)[1:]

    @staticmethod
    def _row_to_draft(row) -> LedgerDraft:
        return LedgerDraft(
            id=int(row["id"]),
            source_document_id=(
                None if row["source_document_id"] is None else int(row["source_document_id"])
            ),
            transaction_type=TransactionType(str(row["transaction_type"])),
            fund_code=row["fund_code"],
            fund_name=row["fund_name"],
            amount=_optional_decimal(row["amount"]),
            shares=_optional_decimal(row["shares"]),
            nav=_optional_decimal(row["nav"]),
            fee=_optional_decimal(row["fee"]),
            order_time=_optional_datetime(row["order_time"]),
            confirmation_time=_optional_datetime(row["confirmation_time"]),
            evidence_level=EvidenceLevel(str(row["evidence_level"])),
            field_evidence=_decode_evidence(str(row["field_evidence_json"])),
            status=str(row["status"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _row_to_transaction(row) -> LedgerTransaction:
        return LedgerTransaction(
            id=int(row["id"]),
            source_document_id=(
                None if row["source_document_id"] is None else int(row["source_document_id"])
            ),
            transaction_type=TransactionType(str(row["transaction_type"])),
            fund_code=str(row["fund_code"]),
            fund_name=row["fund_name"],
            amount=_optional_decimal(row["amount"]),
            shares=_optional_decimal(row["shares"]),
            nav=_optional_decimal(row["nav"]),
            fee=_optional_decimal(row["fee"]),
            order_time=_optional_datetime(row["order_time"]),
            confirmation_time=_optional_datetime(row["confirmation_time"]),
            evidence_level=EvidenceLevel(str(row["evidence_level"])),
            field_evidence=_decode_evidence(str(row["field_evidence_json"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
