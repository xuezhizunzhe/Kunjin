from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Optional, Union

from kunjin.ledger.alipay import AlipayParseError, AlipayPaymentParser
from kunjin.ledger.models import (
    EvidenceLevel,
    LedgerDraft,
    LedgerTransaction,
    TransactionType,
)
from kunjin.ledger.ocr import OcrError
from kunjin.ledger.store import LedgerStateError, LedgerStore
from kunjin.paths import RuntimePaths


ALLOWED_FIELDS = {
    "fund_code",
    "fund_name",
    "amount",
    "shares",
    "nav",
    "fee",
    "order_time",
    "confirmation_time",
    "transaction_type",
}

_ALLOWED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".heic"}
_DECIMAL_FIELDS = {"amount", "shares", "nav", "fee"}
_DATETIME_FIELDS = {"order_time", "confirmation_time"}
_FUND_CODE = re.compile(r"^\d{6}$")
_HASH_CHUNK_SIZE = 1024 * 1024


class LedgerImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LedgerService:
    def __init__(
        self,
        paths: RuntimePaths,
        store: LedgerStore,
        ocr_client,
        parser: AlipayPaymentParser,
        now: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.paths = paths
        self.store = store
        self.ocr_client = ocr_client
        self.parser = parser
        self.now = now or (lambda: datetime.now(timezone.utc))

    def import_image(
        self, source: Union[str, Path], fund_code_hint: Optional[str] = None
    ) -> LedgerDraft:
        source_path = Path(source).expanduser()
        suffix = source_path.suffix.lower()
        if suffix not in _ALLOWED_IMAGE_SUFFIXES:
            raise LedgerImportError(
                "unsupported_image", "image type is not supported"
            )
        fund_code = self._parse_fund_code(fund_code_hint, required=False)
        imported_at = self._current_time()
        self._ensure_private_imports_root()
        managed_path, digest, created_managed_file = self._stage_source(
            source_path, suffix
        )

        try:
            blocks = self.ocr_client.recognize(managed_path)
        except OcrError:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise
        except Exception:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise

        try:
            fields = self.parser.parse(blocks)
        except AlipayParseError as error:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            code = (
                "missing_required_field"
                if error.code == "missing_required_field"
                else "invalid_field"
            )
            raise LedgerImportError(code, str(error)) from None
        except (ValueError, TypeError, OSError):
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise LedgerImportError(
                "invalid_field", "payment fields could not be parsed"
            ) from None

        try:
            amount = self._parse_decimal(fields["amount"].normalized_value, "amount")
            order_time = self._parse_datetime(
                fields["order_time"].normalized_value, "order_time"
            )
            field_evidence = {
                name: field.evidence_level.value for name, field in fields.items()
            }
        except KeyError:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise LedgerImportError(
                "missing_required_field", "required payment field is unavailable"
            ) from None
        except LedgerImportError:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise
        except (AttributeError, TypeError, ValueError):
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise LedgerImportError(
                "invalid_field", "payment fields could not be parsed"
            ) from None
        if amount is None or order_time is None:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise LedgerImportError(
                "missing_required_field", "required payment field is unavailable"
            )

        if fund_code is not None:
            field_evidence["fund_code"] = EvidenceLevel.USER_CONFIRMED.value

        draft = LedgerDraft(
            id=None,
            source_document_id=None,
            transaction_type=TransactionType.SUBSCRIPTION,
            fund_code=fund_code,
            fund_name=None,
            amount=amount,
            shares=None,
            nav=None,
            fee=None,
            order_time=order_time,
            confirmation_time=None,
            evidence_level=EvidenceLevel.TRANSACTION_CONFIRMED,
            field_evidence=field_evidence,
            status="pending",
            created_at=imported_at,
        )
        try:
            return self.store.commit_import(
                sha256=digest,
                original_name=source_path.name,
                managed_path=str(managed_path),
                document_type="alipay_payment",
                imported_at=imported_at,
                fields=fields.values(),
                draft=draft,
            )
        except Exception:
            self._cleanup_new_managed_file(managed_path, created_managed_file)
            raise

    def confirm_draft(
        self, draft_id: int, overrides: Dict[str, str]
    ) -> LedgerTransaction:
        draft = self.store.get_draft(draft_id)
        if draft is None or draft.status != "pending":
            raise LedgerStateError("draft is not pending")

        unknown_fields = set(overrides) - ALLOWED_FIELDS
        if unknown_fields:
            raise LedgerImportError("invalid_field", "unsupported draft field")

        changes = {}
        field_evidence = dict(draft.field_evidence)
        for name, value in overrides.items():
            changes[name] = self._parse_override(name, value)
            field_evidence[name] = EvidenceLevel.USER_CONFIRMED.value

        if changes:
            draft = replace(draft, **changes, field_evidence=field_evidence)

        if draft.fund_code is None:
            raise LedgerImportError(
                "missing_fund_code", "fund code is required before confirmation"
            )
        if not _FUND_CODE.fullmatch(draft.fund_code):
            raise LedgerImportError(
                "invalid_fund_code", "fund code must contain six digits"
            )

        confirmed_at = self._current_time()
        draft = self.store.replace_pending_draft(draft)
        return self.store.confirm_draft(draft.id, confirmed_at)

    def add_manual_transaction(
        self,
        transaction_type,
        fund_code,
        fund_name=None,
        amount=None,
        shares=None,
        nav=None,
        fee=None,
        order_time=None,
        confirmation_time=None,
    ) -> LedgerTransaction:
        created_at = self._current_time()
        parsed_type = self._parse_transaction_type(transaction_type)
        parsed_code = self._parse_fund_code(fund_code, required=True)
        supplied = {
            "transaction_type": parsed_type,
            "fund_code": parsed_code,
            "fund_name": self._parse_fund_name(fund_name),
            "amount": self._parse_decimal(amount, "amount"),
            "shares": self._parse_decimal(shares, "shares"),
            "nav": self._parse_decimal(nav, "nav"),
            "fee": self._parse_decimal(fee, "fee"),
            "order_time": self._parse_datetime(order_time, "order_time"),
            "confirmation_time": self._parse_datetime(
                confirmation_time, "confirmation_time"
            ),
        }
        field_evidence = {
            name: EvidenceLevel.USER_CONFIRMED.value
            for name, value in supplied.items()
            if value is not None
        }
        transaction = LedgerTransaction(
            id=None,
            source_document_id=None,
            transaction_type=parsed_type,
            fund_code=parsed_code,
            fund_name=supplied["fund_name"],
            amount=supplied["amount"],
            shares=supplied["shares"],
            nav=supplied["nav"],
            fee=supplied["fee"],
            order_time=supplied["order_time"],
            confirmation_time=supplied["confirmation_time"],
            evidence_level=EvidenceLevel.USER_CONFIRMED,
            field_evidence=field_evidence,
            created_at=created_at,
        )
        return self.store.add_transaction(transaction)

    def delete_document(self, document_id: int) -> bool:
        managed_path = self.store.document_path(document_id)
        if managed_path is None:
            return False

        # The managed copy is removed first. If the database update fails, a
        # retry sees the still-active row, tolerates the missing file, and
        # finishes the tombstone update without restoring sensitive content.
        self._safe_unlink_managed(managed_path)
        self.store.mark_document_deleted(document_id, self._current_time())
        return True

    def _ensure_private_imports_root(self) -> None:
        try:
            before = self.paths.imports.lstat()
        except FileNotFoundError:
            before = None
        except OSError:
            raise LedgerImportError(
                "unsafe_document_path", "managed imports path is unavailable"
            ) from None
        if before is not None and stat.S_ISLNK(before.st_mode):
            raise LedgerImportError(
                "unsafe_document_path", "managed imports must not be a symlink"
            )
        try:
            self.paths.ensure()
            after = self.paths.imports.lstat()
        except OSError:
            raise LedgerImportError(
                "unsafe_document_path", "managed imports path is unavailable"
            ) from None
        if stat.S_ISLNK(after.st_mode) or not stat.S_ISDIR(after.st_mode):
            raise LedgerImportError(
                "unsafe_document_path", "managed imports must be a private directory"
            )

    def _stage_source(self, source: Path, suffix: str):
        source_descriptor = None
        temporary_descriptor = None
        temporary_path = None
        try:
            source_descriptor = os.open(
                str(source), os.O_RDONLY | os.O_NOFOLLOW
            )
            if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
                raise LedgerImportError(
                    "source_unavailable", "source image is not a regular file"
                )
            temporary_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".import-", suffix=".tmp", dir=str(self.paths.imports)
            )
            temporary_path = Path(temporary_name)
            os.fchmod(temporary_descriptor, 0o600)
            digest = hashlib.sha256()
            while True:
                chunk = os.read(source_descriptor, _HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                self._write_all(temporary_descriptor, chunk)
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            os.close(source_descriptor)
            source_descriptor = None

            digest_text = digest.hexdigest()
            managed_path = self._select_managed_target(digest_text, suffix)
            if managed_path is not None:
                if self._hash_managed_file(managed_path) != digest_text:
                    raise LedgerImportError(
                        "unsafe_document_path", "managed document hash is invalid"
                    )
                os.unlink(temporary_path)
                temporary_path = None
                return managed_path, digest_text, False

            managed_path = self.paths.imports / f"{digest_text}{suffix}"
            os.replace(temporary_path, managed_path)
            temporary_path = None
            if self._hash_managed_file(managed_path) != digest_text:
                self._safe_unlink_managed(managed_path)
                raise LedgerImportError(
                    "unsafe_document_path", "managed document hash is invalid"
                )
            return managed_path, digest_text, True
        except LedgerImportError:
            raise
        except OSError:
            raise LedgerImportError(
                "source_unavailable", "source image could not be staged"
            ) from None
        finally:
            if source_descriptor is not None:
                os.close(source_descriptor)
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _write_all(file_descriptor: int, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            written = os.write(file_descriptor, data[offset:])
            if written <= 0:
                raise OSError("staging write failed")
            offset += written

    def _select_managed_target(
        self, digest: str, preferred_suffix: str
    ) -> Optional[Path]:
        exact_name = f"{digest}{preferred_suffix}"
        try:
            candidates = sorted(
                entry.name
                for entry in os.scandir(self.paths.imports)
                if entry.name.startswith(f"{digest}.")
                and Path(entry.name).suffix.lower() in _ALLOWED_IMAGE_SUFFIXES
            )
        except OSError:
            raise LedgerImportError(
                "unsafe_document_path", "managed imports path is unavailable"
            ) from None
        if exact_name in candidates:
            return self.paths.imports / exact_name
        if candidates:
            return self.paths.imports / candidates[0]
        return None

    @staticmethod
    def _hash_managed_file(path: Path) -> str:
        descriptor = None
        try:
            descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise LedgerImportError(
                    "unsafe_document_path", "managed document is not a regular file"
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, _HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        except LedgerImportError:
            raise
        except OSError:
            raise LedgerImportError(
                "unsafe_document_path", "managed document is unavailable"
            ) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _cleanup_new_managed_file(self, path: Path, created: bool) -> None:
        if not created:
            return
        try:
            self._safe_unlink_managed(path)
        except LedgerImportError:
            pass

    def _safe_unlink_managed(self, path: Path) -> bool:
        expected = re.compile(
            r"^[0-9a-f]{64}(?:\.png|\.jpg|\.jpeg|\.heic)$"
        )
        imports_root = Path(os.path.abspath(str(self.paths.imports)))
        normalized_path = Path(os.path.abspath(str(path)))
        if (
            normalized_path.parent != imports_root
            or not expected.fullmatch(normalized_path.name)
        ):
            raise LedgerImportError(
                "unsafe_document_path", "document is outside managed imports"
            )
        directory_descriptor = None
        document_descriptor = None
        try:
            root_identity = os.lstat(imports_root)
            if stat.S_ISLNK(root_identity.st_mode) or not stat.S_ISDIR(
                root_identity.st_mode
            ):
                raise LedgerImportError(
                    "unsafe_document_path",
                    "managed imports must be a private directory",
                )
            directory_descriptor = os.open(
                str(imports_root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            opened_root = os.fstat(directory_descriptor)
            if (opened_root.st_dev, opened_root.st_ino) != (
                root_identity.st_dev,
                root_identity.st_ino,
            ):
                raise LedgerImportError(
                    "unsafe_document_path", "managed imports identity changed"
                )

            try:
                document_descriptor = os.open(
                    normalized_path.name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                return False
            opened_document = os.fstat(document_descriptor)
            linked_document = os.stat(
                normalized_path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(opened_document.st_mode) or (
                opened_document.st_dev,
                opened_document.st_ino,
            ) != (linked_document.st_dev, linked_document.st_ino):
                raise LedgerImportError(
                    "unsafe_document_path", "managed document identity changed"
                )
            os.unlink(normalized_path.name, dir_fd=directory_descriptor)
            return True
        except FileNotFoundError:
            return False
        except LedgerImportError:
            raise
        except OSError:
            raise LedgerImportError(
                "unsafe_document_path", "managed document could not be removed"
            ) from None
        finally:
            if document_descriptor is not None:
                os.close(document_descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)

    def _parse_override(self, name: str, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            raise LedgerImportError("invalid_field", f"{name} cannot be empty")
        if name == "fund_code":
            return self._parse_fund_code(value, required=False)
        if name == "fund_name":
            return self._parse_fund_name(value)
        if name in _DECIMAL_FIELDS:
            return self._parse_decimal(value, name)
        if name in _DATETIME_FIELDS:
            return self._parse_datetime(value, name)
        if name == "transaction_type":
            return self._parse_transaction_type(value)
        raise LedgerImportError("invalid_field", "unsupported draft field")

    @staticmethod
    def _parse_fund_code(value, required: bool) -> Optional[str]:
        normalized = "" if value is None else str(value).strip()
        if not normalized:
            if required:
                raise LedgerImportError("invalid_fund_code", "fund code is required")
            return None
        if not _FUND_CODE.fullmatch(normalized):
            raise LedgerImportError(
                "invalid_fund_code", "fund code must contain six digits"
            )
        return normalized

    @staticmethod
    def _parse_fund_name(value) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _parse_decimal(value, name: str) -> Optional[Decimal]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            raise LedgerImportError("invalid_field", f"{name} is invalid") from None
        if not parsed.is_finite() or parsed < 0:
            raise LedgerImportError("invalid_field", f"{name} is invalid")
        return parsed

    @staticmethod
    def _parse_datetime(value, name: str) -> Optional[datetime]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if isinstance(value, datetime):
            return LedgerService._require_aware_datetime(value, name)
        try:
            parsed = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            raise LedgerImportError("invalid_field", f"{name} is invalid") from None
        return LedgerService._require_aware_datetime(parsed, name)

    def _current_time(self) -> datetime:
        value = self.now()
        if not isinstance(value, datetime):
            raise LedgerImportError("invalid_field", "now is invalid")
        return self._require_aware_datetime(value, "now")

    @staticmethod
    def _require_aware_datetime(value: datetime, name: str) -> datetime:
        try:
            offset = value.utcoffset()
        except (OverflowError, TypeError, ValueError):
            offset = None
        if value.tzinfo is None or offset is None:
            raise LedgerImportError(
                "invalid_field", f"{name} must include a timezone"
            )
        return value

    @staticmethod
    def _parse_transaction_type(value) -> TransactionType:
        try:
            return (
                value
                if isinstance(value, TransactionType)
                else TransactionType(str(value))
            )
        except (TypeError, ValueError):
            raise LedgerImportError(
                "invalid_field", "transaction_type is invalid"
            ) from None
