from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type

from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import EncryptedAssessment, EncryptedProfile
from kunjin.suitability.models import (
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
)
from kunjin.suitability.policy import SuitabilityPolicyV1


class ActiveProfileChangedError(RuntimeError):
    """The profile binding changed before an assessment could be stored."""


class ProfileInvalidationReason(str, Enum):
    INCOME_CHANGE = "income_change"
    DEBT_CHANGE = "debt_change"
    OBLIGATION_CHANGE = "obligation_change"
    GOAL_CHANGE = "goal_change"
    HOUSEHOLD_CHANGE = "household_change"
    USER_REQUESTED = "user_requested"
    KEY_ROTATION = "key_rotation"

    @classmethod
    def parse(cls, value: object) -> "ProfileInvalidationReason":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        raise ValueError("reason must be a supported invalidation code")


@dataclass(frozen=True)
class ProfileVersionMetadata:
    id: int
    version: int
    status: str
    confirmed_at: datetime
    valid_until: datetime
    invalidated_at: Optional[datetime]
    invalidation_reason: Optional[str]


@dataclass(frozen=True)
class StoredEncryptedProfile:
    metadata: ProfileVersionMetadata
    encrypted: EncryptedProfile


@dataclass(frozen=True)
class PolicyVersionRecord:
    version: str
    canonical_policy_json: str
    policy_checksum: str
    effective_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class AssessmentSafeSummary:
    debt_count: int
    goal_count: int
    obligation_count: int
    required_reserve_months: int
    risk_answers_consistent: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "debt_count": self.debt_count,
            "goal_count": self.goal_count,
            "obligation_count": self.obligation_count,
            "required_reserve_months": self.required_reserve_months,
            "risk_answers_consistent": self.risk_answers_consistent,
        }


@dataclass(frozen=True)
class AssessmentMetadata:
    id: int
    profile_version_id: int
    policy_version: str
    input_fingerprint: str
    status: AssessmentStatus
    hard_blocks: Tuple[BlockReason, ...]
    constraints: Tuple[ConstraintReason, ...]
    safe_summary: AssessmentSafeSummary
    assessed_at: datetime
    valid_until: datetime
    created_at: datetime


@dataclass(frozen=True)
class StoredEncryptedAssessment:
    metadata: AssessmentMetadata
    encrypted: EncryptedAssessment


class ProfileStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def confirm(
        self,
        encrypted: EncryptedProfile,
        confirmed_at: datetime,
        valid_until: datetime,
    ) -> ProfileVersionMetadata:
        _aware_datetime(confirmed_at, "confirmed_at")
        _aware_datetime(valid_until, "valid_until")
        if valid_until <= confirmed_at:
            raise ValueError("valid_until must be after confirmed_at")
        _encrypted_profile(encrypted)

        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM financial_profile_versions"
                ).fetchone()
                next_version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE financial_profile_versions "
                    "SET status = 'superseded' WHERE status = 'confirmed'"
                )
                cursor = connection.execute(
                    """
                    INSERT INTO financial_profile_versions(
                        version, status, encryption_algorithm,
                        encryption_key_version, nonce, encrypted_payload,
                        keyed_payload_fingerprint, confirmed_at, valid_until,
                        invalidated_at, invalidation_reason, created_at
                    ) VALUES (?, 'confirmed', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        next_version,
                        encrypted.algorithm,
                        encrypted.key_version,
                        encrypted.nonce,
                        encrypted.ciphertext,
                        encrypted.keyed_fingerprint,
                        confirmed_at.isoformat(),
                        valid_until.isoformat(),
                        confirmed_at.isoformat(),
                    ),
                )
                stored = connection.execute(
                    "SELECT * FROM financial_profile_versions WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _metadata(stored)

    def active_encrypted(self) -> Optional[StoredEncryptedProfile]:
        with self._repository.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM financial_profile_versions
                WHERE status = 'confirmed'
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else _stored_profile(row)

    def encrypted_by_id(self, profile_version_id: int) -> Optional[StoredEncryptedProfile]:
        if type(profile_version_id) is not int or profile_version_id <= 0:
            raise ValueError("profile_version_id must be a positive integer")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM financial_profile_versions WHERE id = ? LIMIT 1",
                (profile_version_id,),
            ).fetchone()
        return None if row is None else _stored_profile(row)

    def history(self) -> Tuple[ProfileVersionMetadata, ...]:
        with self._repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, version, status, confirmed_at, valid_until,
                       invalidated_at, invalidation_reason
                FROM financial_profile_versions
                ORDER BY version DESC
                """
            ).fetchall()
        return tuple(_metadata(row) for row in rows)

    def latest_metadata(self) -> Optional[ProfileVersionMetadata]:
        with self._repository.connect() as connection:
            row = connection.execute(
                """
                SELECT id, version, status, confirmed_at, valid_until,
                       invalidated_at, invalidation_reason
                FROM financial_profile_versions
                ORDER BY version DESC
                LIMIT 1
                """
            ).fetchone()
        return None if row is None else _metadata(row)

    def invalidate_active(
        self, reason: object, invalidated_at: datetime
    ) -> Optional[ProfileVersionMetadata]:
        reason_code = ProfileInvalidationReason.parse(reason)
        _aware_datetime(invalidated_at, "invalidated_at")

        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT id FROM financial_profile_versions "
                    "WHERE status = 'confirmed' ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                profile_id = int(row["id"])
                connection.execute(
                    """
                    UPDATE financial_profile_versions
                    SET status = 'invalidated', invalidated_at = ?,
                        invalidation_reason = ?
                    WHERE id = ? AND status = 'confirmed'
                    """,
                    (invalidated_at.isoformat(), reason_code.value, profile_id),
                )
                stored = connection.execute(
                    "SELECT * FROM financial_profile_versions WHERE id = ?",
                    (profile_id,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return _metadata(stored)


class SuitabilityPolicyStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def ensure(self, policy: SuitabilityPolicyV1) -> PolicyVersionRecord:
        if type(policy) is not SuitabilityPolicyV1:
            raise ValueError("policy must be the exact SuitabilityPolicyV1 type")
        SuitabilityPolicyV1.validate(policy)
        fixed_policy = SuitabilityPolicyV1()
        fixed_policy.validate()
        canonical = fixed_policy.canonical_json().decode("utf-8")
        checksum = fixed_policy.checksum()
        effective_at = fixed_policy.effective_at.isoformat()
        with self._repository.connect() as connection, connection:
            connection.execute(
                "INSERT OR IGNORE INTO suitability_policy_versions("
                "version, canonical_policy_json, policy_checksum, effective_at, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    fixed_policy.version,
                    canonical,
                    checksum,
                    effective_at,
                    effective_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM suitability_policy_versions WHERE version = ?",
                (fixed_policy.version,),
            ).fetchone()
            if (
                row is None
                or row["canonical_policy_json"] != canonical
                or row["policy_checksum"] != checksum
            ):
                raise ValueError("suitability policy version content does not match")
            record = _policy_record(row)
        return record

    def get(self, version: str) -> Optional[PolicyVersionRecord]:
        _required_string(version, "policy version")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM suitability_policy_versions WHERE version = ?",
                (version,),
            ).fetchone()
        return None if row is None else _policy_record(row)


class SuitabilityAssessmentStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = repository

    def insert(
        self,
        profile_version_id: int,
        policy_version: str,
        input_fingerprint: str,
        result: AssessmentResult,
        encrypted: EncryptedAssessment,
        assessed_at: datetime,
        valid_until: datetime,
    ) -> AssessmentMetadata:
        if type(profile_version_id) is not int or profile_version_id <= 0:
            raise ValueError("profile_version_id must be a positive integer")
        _required_string(policy_version, "policy version")
        _lower_hex_digest(input_fingerprint, "input_fingerprint")
        if not isinstance(result, AssessmentResult):
            raise ValueError("assessment result is required")
        result.validate()
        _encrypted_assessment(encrypted)
        _aware_datetime(assessed_at, "assessed_at")
        _aware_datetime(valid_until, "valid_until")
        if valid_until <= assessed_at:
            raise ValueError("valid_until must be after assessed_at")
        # Canonical UTC storage makes the bounded latest query's text ordering
        # identical to absolute-time ordering for service-created assessments.
        assessed_at = assessed_at.astimezone(timezone.utc)
        valid_until = valid_until.astimezone(timezone.utc)

        hard_blocks = _reason_json(result.hard_blocks)
        constraints = _reason_json(result.constraints)
        safe_summary_value = _safe_summary(result.safe_summary())
        safe_summary = json.dumps(
            safe_summary_value.as_dict(),
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                active_rows = connection.execute(
                    "SELECT id FROM financial_profile_versions WHERE status = 'confirmed'"
                ).fetchall()
                if len(active_rows) != 1 or int(active_rows[0]["id"]) != profile_version_id:
                    raise ActiveProfileChangedError(
                        "active profile changed before assessment persistence"
                    )
                cursor = connection.execute(
                    "INSERT INTO suitability_assessments("
                    "profile_version_id, policy_version, input_fingerprint, status, "
                    "hard_blocks_json, constraints_json, safe_summary_json, "
                    "encrypted_amount_results, encryption_algorithm, "
                    "encryption_key_version, nonce, keyed_payload_fingerprint, "
                    "assessed_at, valid_until, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        profile_version_id,
                        policy_version,
                        input_fingerprint,
                        result.status.value,
                        hard_blocks,
                        constraints,
                        safe_summary,
                        encrypted.ciphertext,
                        encrypted.algorithm,
                        encrypted.key_version,
                        encrypted.nonce,
                        encrypted.keyed_fingerprint,
                        assessed_at.isoformat(),
                        valid_until.isoformat(),
                        assessed_at.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM suitability_assessments WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if row is None:
            raise ValueError("stored suitability assessment is unavailable")
        return _assessment_metadata(row)

    def latest_for(
        self, profile_version_id: int, policy_version: str
    ) -> Optional[StoredEncryptedAssessment]:
        if type(profile_version_id) is not int or profile_version_id <= 0:
            raise ValueError("profile_version_id must be a positive integer")
        _required_string(policy_version, "policy version")
        with self._repository.connect() as connection:
            connection.create_function(
                "kunjin_epoch_microseconds",
                1,
                _epoch_microseconds,
                deterministic=True,
            )
            try:
                row = connection.execute(
                    "SELECT * FROM suitability_assessments "
                    "WHERE profile_version_id = ? AND policy_version = ? "
                    "ORDER BY kunjin_epoch_microseconds(assessed_at) DESC, "
                    "id DESC LIMIT 1",
                    (profile_version_id, policy_version),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "user-defined function raised exception" in str(exc):
                    raise ValueError("stored assessed_at must be an ISO datetime") from None
                raise
        return None if row is None else _stored_assessment(row)

    def get(self, assessment_id: int) -> Optional[StoredEncryptedAssessment]:
        if type(assessment_id) is not int or assessment_id <= 0:
            raise ValueError("assessment_id must be a positive integer")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM suitability_assessments WHERE id = ? LIMIT 1",
                (assessment_id,),
            ).fetchone()
        return None if row is None else _stored_assessment(row)

    def history(self) -> Tuple[AssessmentMetadata, ...]:
        with self._repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM suitability_assessments ORDER BY id DESC"
            ).fetchall()
        metadata = tuple(_assessment_metadata(row) for row in rows)
        return tuple(
            sorted(
                metadata,
                key=lambda item: (item.assessed_at, item.id),
                reverse=True,
            )
        )


def _aware_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _encrypted_profile(value: EncryptedProfile) -> None:
    if not isinstance(value, EncryptedProfile):
        raise ValueError("encrypted profile is required")
    for field_name in (
        "algorithm",
        "key_version",
        "nonce",
        "ciphertext",
        "keyed_fingerprint",
    ):
        field_value = getattr(value, field_name)
        if not isinstance(field_value, str) or not field_value:
            raise ValueError(f"encrypted profile {field_name} is required")


def _encrypted_assessment(value: EncryptedAssessment) -> None:
    if not isinstance(value, EncryptedAssessment):
        raise ValueError("encrypted assessment is required")
    if value.algorithm != "AES-256-GCM":
        raise ValueError("encrypted assessment algorithm is invalid")
    for field_name in ("key_version", "nonce", "ciphertext"):
        field_value = getattr(value, field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            raise ValueError(f"encrypted assessment {field_name} is required")
    _lower_hex_digest(
        value.keyed_fingerprint,
        "encrypted assessment keyed_fingerprint",
    )


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _lower_hex_digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character hex digest")
    return value


def _stored_datetime(value: object, name: str) -> datetime:
    return _parse_aware_datetime_text(value, name)


def _parse_aware_datetime_text(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"stored {name} must be an ISO datetime")
    parse_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise ValueError(f"stored {name} must be an ISO datetime") from exc
    try:
        _aware_datetime(parsed, name)
    except ValueError:
        raise ValueError(f"stored {name} must be an ISO datetime") from None
    canonical = parsed.isoformat()
    legacy_utc = value.endswith("Z") and canonical == parse_value
    if canonical != value and not legacy_utc:
        raise ValueError(f"stored {name} must be an ISO datetime")
    return parsed


def _epoch_microseconds(value: object) -> int:
    parsed = _parse_aware_datetime_text(value, "assessed_at").astimezone(timezone.utc)
    epoch_ordinal = datetime(1970, 1, 1).toordinal()
    days = parsed.toordinal() - epoch_ordinal
    seconds = parsed.hour * 3_600 + parsed.minute * 60 + parsed.second
    return days * 86_400_000_000 + seconds * 1_000_000 + parsed.microsecond


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("stored JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _stored_json(value: object, name: str) -> object:
    if not isinstance(value, str):
        raise ValueError(f"stored {name} must be valid JSON")
    try:
        return json.loads(
            value,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"stored {name} must be valid JSON: {exc}") from None


def _reason_json(reasons: Tuple[Enum, ...]) -> str:
    return json.dumps(
        [item.value for item in reasons],
        separators=(",", ":"),
    )


def _stored_reasons(
    value: object,
    enum_type: Type[Enum],
    name: str,
) -> Tuple[Enum, ...]:
    parsed = _stored_json(value, name)
    if not isinstance(parsed, list):
        raise ValueError(f"stored {name} must be a JSON array")
    reasons = []
    for item in parsed:
        if not isinstance(item, str):
            raise ValueError(f"stored {name} must contain reason strings")
        try:
            reasons.append(enum_type(item))
        except ValueError:
            label = "block" if enum_type is BlockReason else "constraint"
            raise ValueError(f"stored {name} contains an unsupported {label} reason") from None
    if len(set(reasons)) != len(reasons):
        raise ValueError(f"stored {name} cannot contain duplicates")
    return tuple(reasons)


_SAFE_SUMMARY_INTEGER_KEYS = frozenset(
    {
        "debt_count",
        "goal_count",
        "obligation_count",
        "required_reserve_months",
    }
)
_SAFE_SUMMARY_KEYS = _SAFE_SUMMARY_INTEGER_KEYS | {"risk_answers_consistent"}


def _safe_summary(value: object) -> AssessmentSafeSummary:
    if not isinstance(value, dict) or set(value) != _SAFE_SUMMARY_KEYS:
        raise ValueError("stored safe summary has an invalid amount-free schema")
    for key in _SAFE_SUMMARY_INTEGER_KEYS:
        item = value[key]
        if type(item) is not int or item < 0:
            raise ValueError("stored safe summary has an invalid amount-free schema")
    if type(value["risk_answers_consistent"]) is not bool:
        raise ValueError("stored safe summary has an invalid amount-free schema")
    return AssessmentSafeSummary(
        debt_count=value["debt_count"],
        goal_count=value["goal_count"],
        obligation_count=value["obligation_count"],
        required_reserve_months=value["required_reserve_months"],
        risk_answers_consistent=value["risk_answers_consistent"],
    )


def _stored_safe_summary(value: object) -> AssessmentSafeSummary:
    parsed = _stored_json(value, "safe_summary_json")
    return _safe_summary(parsed)


def _policy_record(row: sqlite3.Row) -> PolicyVersionRecord:
    fixed_policy = SuitabilityPolicyV1()
    fixed_policy.validate()
    version = _required_string(row["version"], "stored policy version")
    canonical = _required_string(
        row["canonical_policy_json"],
        "stored canonical policy JSON",
    )
    parsed = _stored_json(canonical, "canonical_policy_json")
    if not isinstance(parsed, dict):
        raise ValueError("stored canonical_policy_json must be a JSON object")
    expected_canonical = json.dumps(parsed, separators=(",", ":"), sort_keys=True)
    if canonical != expected_canonical:
        raise ValueError("stored canonical_policy_json must be canonical JSON")
    if parsed.get("version") != version:
        raise ValueError("stored policy JSON version does not match row version")
    if version != fixed_policy.version:
        raise ValueError("stored suitability policy version is unsupported")
    fixed_canonical = fixed_policy.canonical_json().decode("utf-8")
    if canonical != fixed_canonical:
        raise ValueError("stored suitability policy does not match fixed V1")
    checksum = _lower_hex_digest(row["policy_checksum"], "stored policy_checksum")
    if checksum != fixed_policy.checksum() or (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest() != checksum
    ):
        raise ValueError("stored policy checksum does not match policy JSON")
    effective_at = _stored_datetime(row["effective_at"], "effective_at")
    if effective_at != fixed_policy.effective_at:
        raise ValueError("stored policy effective_at does not match fixed V1")
    return PolicyVersionRecord(
        version=version,
        canonical_policy_json=canonical,
        policy_checksum=checksum,
        effective_at=effective_at,
        created_at=_stored_datetime(row["created_at"], "created_at"),
    )


def _assessment_metadata(row: sqlite3.Row) -> AssessmentMetadata:
    try:
        status = AssessmentStatus(row["status"])
    except (TypeError, ValueError):
        raise ValueError("stored assessment status is unsupported") from None
    assessed_at = _stored_datetime(row["assessed_at"], "assessed_at")
    valid_until = _stored_datetime(row["valid_until"], "valid_until")
    if valid_until <= assessed_at:
        raise ValueError("stored valid_until must be after assessed_at")
    hard_blocks = tuple(
        _stored_reasons(
            row["hard_blocks_json"],
            BlockReason,
            "hard_blocks_json",
        )
    )
    constraints = tuple(
        _stored_reasons(
            row["constraints_json"],
            ConstraintReason,
            "constraints_json",
        )
    )
    expected_status = AssessmentStatus.READY_FOR_ALLOCATION
    if hard_blocks:
        expected_status = AssessmentStatus.BLOCKED
    elif constraints:
        expected_status = AssessmentStatus.CONSTRAINED
    if status is not expected_status:
        raise ValueError("stored assessment status does not match reasons")
    return AssessmentMetadata(
        id=int(row["id"]),
        profile_version_id=int(row["profile_version_id"]),
        policy_version=_required_string(
            row["policy_version"],
            "stored policy version",
        ),
        input_fingerprint=_lower_hex_digest(
            row["input_fingerprint"],
            "stored input_fingerprint",
        ),
        status=status,
        hard_blocks=hard_blocks,
        constraints=constraints,
        safe_summary=_stored_safe_summary(row["safe_summary_json"]),
        assessed_at=assessed_at,
        valid_until=valid_until,
        created_at=_stored_datetime(row["created_at"], "created_at"),
    )


def _stored_assessment(row: sqlite3.Row) -> StoredEncryptedAssessment:
    encrypted = EncryptedAssessment(
        algorithm=str(row["encryption_algorithm"]),
        key_version=str(row["encryption_key_version"]),
        nonce=str(row["nonce"]),
        ciphertext=str(row["encrypted_amount_results"]),
        keyed_fingerprint=str(row["keyed_payload_fingerprint"]),
    )
    _encrypted_assessment(encrypted)
    return StoredEncryptedAssessment(
        metadata=_assessment_metadata(row),
        encrypted=encrypted,
    )


def _metadata(row: sqlite3.Row) -> ProfileVersionMetadata:
    invalidated_value = row["invalidated_at"]
    return ProfileVersionMetadata(
        id=int(row["id"]),
        version=int(row["version"]),
        status=str(row["status"]),
        confirmed_at=_stored_datetime(row["confirmed_at"], "confirmed_at"),
        valid_until=_stored_datetime(row["valid_until"], "valid_until"),
        invalidated_at=(
            None
            if invalidated_value is None
            else _stored_datetime(invalidated_value, "invalidated_at")
        ),
        invalidation_reason=(
            None if row["invalidation_reason"] is None else str(row["invalidation_reason"])
        ),
    )


def _stored_profile(row: sqlite3.Row) -> StoredEncryptedProfile:
    encrypted = EncryptedProfile(
        algorithm=str(row["encryption_algorithm"]),
        key_version=str(row["encryption_key_version"]),
        nonce=str(row["nonce"]),
        ciphertext=str(row["encrypted_payload"]),
        keyed_fingerprint=str(row["keyed_payload_fingerprint"]),
    )
    _encrypted_profile(encrypted)
    return StoredEncryptedProfile(metadata=_metadata(row), encrypted=encrypted)
