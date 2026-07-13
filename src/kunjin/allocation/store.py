from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kunjin.allocation.crypto import EncryptedAllocationAssessment
from kunjin.allocation.models import (
    AllocationConstraintCode,
    AllocationResult,
    AllocationSafeSummary,
    AllocationStatus,
    PermittedRegion,
)
from kunjin.allocation.policy import (
    ALLOCATION_POLICY_V1_CHECKSUM,
    AllocationPolicyV1,
)
from kunjin.allocation.serialization import (
    MAX_COLLECTION_ITEMS,
    MAX_EXACT_PAYLOAD_BYTES,
    MAX_INTEGER_DIGITS,
)
from kunjin.storage.repository import Repository

MAX_HISTORY_PAGE_SIZE = 1_000
MAX_HISTORY_OFFSET = 2**63 - 1


class AllocationBindingChangedError(RuntimeError):
    """The authenticated profile or suitability binding changed before storage."""


class AllocationAssessmentConflictError(RuntimeError):
    """An allocation input fingerprint is already bound to different content."""


@dataclass(frozen=True)
class AllocationPolicyRecord:
    version: str
    canonical_policy_json: str
    policy_checksum: str
    effective_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class AllocationAssessmentMetadata:
    id: int
    profile_version_id: int
    suitability_assessment_id: int
    policy_version: str
    input_fingerprint: str
    status: AllocationStatus
    permitted_region: Optional[PermittedRegion]
    binding_constraints: Tuple[AllocationConstraintCode, ...]
    safe_summary: AllocationSafeSummary
    assessed_at: datetime
    valid_until: datetime
    created_at: datetime


@dataclass(frozen=True)
class StoredEncryptedAllocationAssessment:
    metadata: AllocationAssessmentMetadata
    encrypted: EncryptedAllocationAssessment


class AllocationPolicyStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = _owned_repository(repository)

    def ensure(self, policy: AllocationPolicyV1) -> AllocationPolicyRecord:
        if type(policy) is not AllocationPolicyV1:
            raise ValueError("policy must be the exact AllocationPolicyV1 type")
        policy.validate()
        fixed = AllocationPolicyV1()
        fixed.validate()
        canonical = fixed.canonical_json().decode("utf-8")
        checksum = fixed.checksum()
        if checksum != ALLOCATION_POLICY_V1_CHECKSUM:
            raise ValueError("allocation policy V1 checksum does not match the fixed checksum")
        effective_at = _canonical_utc_text(fixed.effective_at, "effective_at")

        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM allocation_policy_versions WHERE version = ?",
                    (fixed.version,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO allocation_policy_versions("
                        "version, canonical_policy_json, policy_checksum, effective_at, created_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (fixed.version, canonical, checksum, effective_at, effective_at),
                    )
                    row = connection.execute(
                        "SELECT * FROM allocation_policy_versions WHERE version = ?",
                        (fixed.version,),
                    ).fetchone()
                record = _policy_record(row)
                if (
                    record.version != fixed.version
                    or record.canonical_policy_json != canonical
                    or record.policy_checksum != checksum
                    or record.effective_at != fixed.effective_at
                    or record.created_at != fixed.effective_at
                ):
                    raise ValueError("allocation policy version content does not match fixed V1")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return record

    def get(self, version: str) -> Optional[AllocationPolicyRecord]:
        _required_text(version, "policy version")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM allocation_policy_versions WHERE version = ?",
                (version,),
            ).fetchone()
        return None if row is None else _policy_record(row)


class AllocationAssessmentStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = _owned_repository(repository)

    def insert(
        self,
        *,
        profile_version_id: int,
        suitability_assessment_id: int,
        expected_profile_fingerprint: str,
        expected_suitability_input_fingerprint: str,
        suitability_policy_version: str,
        policy_version: str,
        input_fingerprint: str,
        result: AllocationResult,
        encrypted: EncryptedAllocationAssessment,
        assessed_at: datetime,
        valid_until: datetime,
    ) -> AllocationAssessmentMetadata:
        _positive_integer(profile_version_id, "profile_version_id")
        _positive_integer(suitability_assessment_id, "suitability_assessment_id")
        _lower_hex_digest(expected_profile_fingerprint, "expected_profile_fingerprint")
        _lower_hex_digest(
            expected_suitability_input_fingerprint,
            "expected_suitability_input_fingerprint",
        )
        _required_text(suitability_policy_version, "suitability policy version")
        _required_text(policy_version, "allocation policy version")
        _lower_hex_digest(input_fingerprint, "input_fingerprint")
        if type(result) is not AllocationResult:
            raise ValueError("result must be the exact AllocationResult type")
        result.validate()
        if result.status is not AllocationStatus.RANGE_AVAILABLE:
            raise ValueError("only range_available allocation results may be persisted")
        _validate_encrypted(encrypted, require_fixed_metadata=False)
        assessed_at_utc = _canonical_utc_datetime(assessed_at, "assessed_at")
        valid_until_utc = _canonical_utc_datetime(valid_until, "valid_until")
        if valid_until_utc <= assessed_at_utc:
            raise ValueError("valid_until must be after assessed_at")

        permitted_region_json = _encode_permitted_region(result.permitted_region)
        constraints_json = _encode_constraints(result.binding_constraints)
        safe_summary_json = _encode_safe_summary(result.safe_summary)
        assessed_text = assessed_at_utc.isoformat()
        valid_until_text = valid_until_utc.isoformat()

        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._assert_bindings(
                    connection,
                    profile_version_id=profile_version_id,
                    suitability_assessment_id=suitability_assessment_id,
                    expected_profile_fingerprint=expected_profile_fingerprint,
                    expected_suitability_input_fingerprint=(expected_suitability_input_fingerprint),
                    suitability_policy_version=suitability_policy_version,
                    allocation_policy_version=policy_version,
                    assessed_at=assessed_at_utc,
                    valid_until=valid_until_utc,
                )
                existing_rows = connection.execute(
                    "SELECT * FROM allocation_assessments "
                    "WHERE input_fingerprint = ? ORDER BY id LIMIT 2",
                    (input_fingerprint,),
                ).fetchall()
                if existing_rows:
                    if len(existing_rows) != 1:
                        raise AllocationAssessmentConflictError(
                            "allocation input fingerprint has duplicate stored rows"
                        )
                    stored = self._row_to_stored(existing_rows[0])
                    if (
                        encrypted.algorithm != stored.encrypted.algorithm
                        or encrypted.key_version != stored.encrypted.key_version
                        or encrypted.keyed_fingerprint != stored.encrypted.keyed_fingerprint
                    ):
                        raise AllocationAssessmentConflictError(
                            "allocation input fingerprint encryption metadata conflicts"
                        )
                    if not _metadata_matches_idempotent_existing(
                        stored.metadata,
                        profile_version_id=profile_version_id,
                        suitability_assessment_id=suitability_assessment_id,
                        policy_version=policy_version,
                        input_fingerprint=input_fingerprint,
                        result=result,
                        assessed_at=assessed_at_utc,
                        valid_until=valid_until_utc,
                    ):
                        raise AllocationAssessmentConflictError(
                            "allocation input fingerprint is bound to different content"
                        )
                    connection.commit()
                    return stored.metadata
                _validate_encrypted(encrypted)
                cursor = connection.execute(
                    "INSERT INTO allocation_assessments("
                    "profile_version_id, suitability_assessment_id, policy_version, "
                    "input_fingerprint, status, permitted_region_json, "
                    "binding_constraints_json, safe_summary_json, encrypted_amount_results, "
                    "encryption_algorithm, encryption_key_version, nonce, "
                    "keyed_payload_fingerprint, assessed_at, valid_until, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        profile_version_id,
                        suitability_assessment_id,
                        policy_version,
                        input_fingerprint,
                        AllocationStatus.RANGE_AVAILABLE.value,
                        permitted_region_json,
                        constraints_json,
                        safe_summary_json,
                        encrypted.ciphertext,
                        encrypted.algorithm,
                        encrypted.key_version,
                        encrypted.nonce,
                        encrypted.keyed_fingerprint,
                        assessed_text,
                        valid_until_text,
                        assessed_text,
                    ),
                )
                inserted_id = cursor.lastrowid
                row = connection.execute(
                    "SELECT * FROM allocation_assessments WHERE id = ?",
                    (inserted_id,),
                ).fetchone()
                stored = self._row_to_stored(row)
                if (
                    stored.metadata.id != inserted_id
                    or not _metadata_matches_expected(
                        stored.metadata,
                        profile_version_id=profile_version_id,
                        suitability_assessment_id=suitability_assessment_id,
                        policy_version=policy_version,
                        input_fingerprint=input_fingerprint,
                        result=result,
                        assessed_at=assessed_at_utc,
                        valid_until=valid_until_utc,
                    )
                    or stored.encrypted != encrypted
                ):
                    raise ValueError("stored allocation assessment read-back does not match")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return stored.metadata

    def latest_for(
        self,
        profile_version_id: int,
        suitability_assessment_id: int,
        policy_version: str,
    ) -> Optional[StoredEncryptedAllocationAssessment]:
        _positive_integer(profile_version_id, "profile_version_id")
        _positive_integer(suitability_assessment_id, "suitability_assessment_id")
        _required_text(policy_version, "policy version")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM allocation_assessments "
                "WHERE profile_version_id = ? AND suitability_assessment_id = ? "
                "AND policy_version = ? "
                "ORDER BY assessed_at DESC, id DESC LIMIT 1",
                (profile_version_id, suitability_assessment_id, policy_version),
            ).fetchone()
        return None if row is None else self._row_to_stored(row)

    def get(self, assessment_id: int) -> Optional[StoredEncryptedAllocationAssessment]:
        _positive_integer(assessment_id, "assessment_id")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM allocation_assessments WHERE id = ?",
                (assessment_id,),
            ).fetchone()
        return None if row is None else self._row_to_stored(row)

    def history(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[AllocationAssessmentMetadata, ...]:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE_SIZE:
            raise ValueError(f"limit must be an integer from 1 through {MAX_HISTORY_PAGE_SIZE}")
        if type(offset) is not int or not 0 <= offset <= MAX_HISTORY_OFFSET:
            raise ValueError(f"offset must be an integer from zero through {MAX_HISTORY_OFFSET}")
        with self._repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM allocation_assessments "
                "ORDER BY assessed_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return tuple(self._row_to_stored(row).metadata for row in rows)

    def _row_to_stored(self, row: Any) -> StoredEncryptedAllocationAssessment:
        return _stored_assessment(row)

    @staticmethod
    def _assert_bindings(
        connection: Any,
        *,
        profile_version_id: int,
        suitability_assessment_id: int,
        expected_profile_fingerprint: str,
        expected_suitability_input_fingerprint: str,
        suitability_policy_version: str,
        allocation_policy_version: str,
        assessed_at: datetime,
        valid_until: datetime,
    ) -> None:
        active_rows = connection.execute(
            "SELECT * FROM financial_profile_versions WHERE status = 'confirmed'"
        ).fetchall()
        if len(active_rows) != 1:
            raise AllocationBindingChangedError("active profile binding changed")
        active = active_rows[0]
        if (
            _strict_integer(active["id"], "active profile id") != profile_version_id
            or active["keyed_payload_fingerprint"] != expected_profile_fingerprint
        ):
            raise AllocationBindingChangedError("active profile binding changed")
        confirmed_at = _stored_aware_datetime(active["confirmed_at"], "profile confirmed_at")
        profile_valid_until = _stored_aware_datetime(active["valid_until"], "profile valid_until")
        if not (confirmed_at <= assessed_at < profile_valid_until):
            raise AllocationBindingChangedError("active profile is not current")

        suitability = connection.execute(
            "SELECT * FROM suitability_assessments WHERE id = ?",
            (suitability_assessment_id,),
        ).fetchone()
        if suitability is None:
            raise AllocationBindingChangedError("suitability binding changed")
        if (
            _strict_integer(suitability["profile_version_id"], "suitability profile id")
            != profile_version_id
            or suitability["input_fingerprint"] != expected_suitability_input_fingerprint
            or suitability["policy_version"] != suitability_policy_version
            or suitability["status"] not in ("constrained", "ready_for_allocation")
        ):
            raise AllocationBindingChangedError("suitability binding changed")

        candidates = connection.execute(
            "SELECT id, assessed_at FROM suitability_assessments "
            "WHERE profile_version_id = ? AND policy_version = ?",
            (profile_version_id, suitability_policy_version),
        ).fetchall()
        if not candidates:
            raise AllocationBindingChangedError("suitability binding changed")
        latest = max(
            candidates,
            key=lambda row: (
                _stored_aware_datetime(row["assessed_at"], "suitability assessed_at"),
                _strict_integer(row["id"], "suitability id"),
            ),
        )
        if _strict_integer(latest["id"], "latest suitability id") != suitability_assessment_id:
            raise AllocationBindingChangedError("referenced suitability is no longer latest")
        suitability_assessed_at = _stored_aware_datetime(
            suitability["assessed_at"], "suitability assessed_at"
        )
        suitability_valid_until = _stored_aware_datetime(
            suitability["valid_until"], "suitability valid_until"
        )
        if not (suitability_assessed_at <= assessed_at < suitability_valid_until):
            raise AllocationBindingChangedError("referenced suitability is not current")
        policy_row = connection.execute(
            "SELECT * FROM allocation_policy_versions WHERE version = ?",
            (allocation_policy_version,),
        ).fetchone()
        if policy_row is None:
            raise AllocationBindingChangedError("allocation policy binding changed")
        policy = _policy_record(policy_row)
        if policy.effective_at > assessed_at:
            raise AllocationBindingChangedError("allocation policy is not yet effective")
        expected_valid_until = min(
            assessed_at + timedelta(hours=24),
            profile_valid_until,
            suitability_valid_until,
        )
        if valid_until != expected_valid_until:
            raise AllocationBindingChangedError(
                "allocation valid_until does not match authenticated bindings"
            )


def _policy_record(row: Any) -> AllocationPolicyRecord:
    if row is None:
        raise ValueError("stored allocation policy is unavailable")
    version = _required_text(row["version"], "stored policy version")
    if version != "1":
        raise ValueError("stored allocation policy version is unsupported")
    canonical = _required_text(row["canonical_policy_json"], "stored canonical policy JSON")
    parsed = _stored_json(canonical, "canonical policy JSON")
    if type(parsed) is not dict:
        raise ValueError("stored canonical policy JSON must be an object")
    fixed = AllocationPolicyV1()
    expected = fixed.canonical_json().decode("utf-8")
    if canonical != _canonical_json(parsed):
        raise ValueError("stored canonical policy JSON is not canonical")
    checksum = _lower_hex_digest(row["policy_checksum"], "stored policy checksum")
    actual_checksum = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if checksum != actual_checksum:
        raise ValueError("stored allocation policy checksum does not match content")
    if canonical != expected or checksum != ALLOCATION_POLICY_V1_CHECKSUM:
        raise ValueError("stored allocation policy content does not match fixed V1")
    effective_at = _stored_utc_datetime(row["effective_at"], "policy effective_at")
    created_at = _stored_utc_datetime(row["created_at"], "policy created_at")
    if effective_at != fixed.effective_at:
        raise ValueError("stored allocation policy effective_at does not match fixed V1")
    if created_at != fixed.effective_at:
        raise ValueError("stored allocation policy created_at does not match fixed V1")
    return AllocationPolicyRecord(version, canonical, checksum, effective_at, created_at)


def _stored_assessment(row: Any) -> StoredEncryptedAllocationAssessment:
    if row is None:
        raise ValueError("stored allocation assessment is unavailable")
    assessment_id = _strict_positive_integer(row["id"], "assessment id")
    profile_id = _strict_positive_integer(row["profile_version_id"], "profile version id")
    suitability_id = _strict_positive_integer(
        row["suitability_assessment_id"], "suitability assessment id"
    )
    policy_version = _required_text(row["policy_version"], "stored policy version")
    input_fingerprint = _lower_hex_digest(row["input_fingerprint"], "stored input fingerprint")
    try:
        status = AllocationStatus(row["status"])
    except (TypeError, ValueError):
        raise ValueError("stored allocation status is invalid") from None
    if status is not AllocationStatus.RANGE_AVAILABLE:
        raise ValueError("stored allocation status must be range_available")
    permitted_region = _decode_permitted_region(row["permitted_region_json"])
    constraints = _decode_constraints(row["binding_constraints_json"])
    safe_summary = _decode_safe_summary(row["safe_summary_json"])
    _validate_plaintext_semantics(permitted_region, constraints, safe_summary)
    encrypted = EncryptedAllocationAssessment(
        algorithm=row["encryption_algorithm"],
        key_version=row["encryption_key_version"],
        nonce=row["nonce"],
        ciphertext=row["encrypted_amount_results"],
        keyed_fingerprint=row["keyed_payload_fingerprint"],
    )
    _validate_encrypted(encrypted)
    assessed_at = _stored_utc_datetime(row["assessed_at"], "assessment assessed_at")
    valid_until = _stored_utc_datetime(row["valid_until"], "assessment valid_until")
    created_at = _stored_utc_datetime(row["created_at"], "assessment created_at")
    if valid_until <= assessed_at:
        raise ValueError("stored valid_until must be after assessed_at")
    if created_at != assessed_at:
        raise ValueError("stored created_at must equal assessed_at")
    metadata = AllocationAssessmentMetadata(
        id=assessment_id,
        profile_version_id=profile_id,
        suitability_assessment_id=suitability_id,
        policy_version=policy_version,
        input_fingerprint=input_fingerprint,
        status=status,
        permitted_region=permitted_region,
        binding_constraints=constraints,
        safe_summary=safe_summary,
        assessed_at=assessed_at,
        valid_until=valid_until,
        created_at=created_at,
    )
    return StoredEncryptedAllocationAssessment(metadata, encrypted)


def _metadata_matches_expected(
    metadata: AllocationAssessmentMetadata,
    *,
    profile_version_id: int,
    suitability_assessment_id: int,
    policy_version: str,
    input_fingerprint: str,
    result: AllocationResult,
    assessed_at: datetime,
    valid_until: datetime,
) -> bool:
    return (
        metadata.profile_version_id == profile_version_id
        and metadata.suitability_assessment_id == suitability_assessment_id
        and metadata.policy_version == policy_version
        and metadata.input_fingerprint == input_fingerprint
        and metadata.status is AllocationStatus.RANGE_AVAILABLE
        and metadata.permitted_region == result.permitted_region
        and metadata.binding_constraints == result.binding_constraints
        and metadata.safe_summary == result.safe_summary
        and metadata.assessed_at == assessed_at
        and metadata.valid_until == valid_until
        and metadata.created_at == assessed_at
    )


def _metadata_matches_idempotent_existing(
    metadata: AllocationAssessmentMetadata,
    *,
    profile_version_id: int,
    suitability_assessment_id: int,
    policy_version: str,
    input_fingerprint: str,
    result: AllocationResult,
    assessed_at: datetime,
    valid_until: datetime,
) -> bool:
    return _metadata_matches_expected(
        metadata,
        profile_version_id=profile_version_id,
        suitability_assessment_id=suitability_assessment_id,
        policy_version=policy_version,
        input_fingerprint=input_fingerprint,
        result=result,
        assessed_at=assessed_at,
        valid_until=valid_until,
    )


def _encode_permitted_region(value: Optional[PermittedRegion]) -> str:
    if value is None:
        payload: Dict[str, object] = {"available": False}
    else:
        if type(value) is not PermittedRegion:
            raise ValueError("permitted region must be an exact PermittedRegion or None")
        value.validate()
        payload = {
            "available": True,
            "drawdown_equity_ceiling": _decimal_text(value.drawdown_equity_ceiling),
            "horizon_equity_ceiling": _decimal_text(value.horizon_equity_ceiling),
            "inequalities": list(value.inequalities),
            "loss_amount_equity_ceiling": _decimal_text(value.loss_amount_equity_ceiling),
            "maximum_equity": _decimal_text(value.maximum_equity),
            "stability_equity_ceiling": _decimal_text(value.stability_equity_ceiling),
            "willingness_equity_ceiling": _decimal_text(value.willingness_equity_ceiling),
        }
    return _canonical_json(payload)


def _decode_permitted_region(value: object) -> Optional[PermittedRegion]:
    parsed = _stored_json(value, "permitted region")
    if type(parsed) is not dict:
        raise ValueError("stored permitted region must be a JSON object")
    if parsed == {"available": False}:
        expected = _canonical_json(parsed)
        if value != expected:
            raise ValueError("stored permitted region JSON is not canonical")
        return None
    expected_keys = {
        "available",
        "drawdown_equity_ceiling",
        "horizon_equity_ceiling",
        "inequalities",
        "loss_amount_equity_ceiling",
        "maximum_equity",
        "stability_equity_ceiling",
        "willingness_equity_ceiling",
    }
    if set(parsed) != expected_keys or parsed["available"] is not True:
        raise ValueError("stored permitted region has unexpected fields")
    inequalities = parsed["inequalities"]
    if type(inequalities) is not list or any(type(item) is not str for item in inequalities):
        raise ValueError("stored permitted region inequalities are invalid")
    region = PermittedRegion(
        inequalities=tuple(inequalities),
        maximum_equity=_stored_decimal(parsed["maximum_equity"], "maximum equity"),
        horizon_equity_ceiling=_stored_decimal(
            parsed["horizon_equity_ceiling"], "horizon equity ceiling"
        ),
        loss_amount_equity_ceiling=_stored_decimal(
            parsed["loss_amount_equity_ceiling"], "loss amount equity ceiling"
        ),
        drawdown_equity_ceiling=_stored_decimal(
            parsed["drawdown_equity_ceiling"], "drawdown equity ceiling"
        ),
        willingness_equity_ceiling=_stored_decimal(
            parsed["willingness_equity_ceiling"], "willingness equity ceiling"
        ),
        stability_equity_ceiling=_stored_decimal(
            parsed["stability_equity_ceiling"], "stability equity ceiling"
        ),
    )
    region.validate()
    if value != _encode_permitted_region(region):
        raise ValueError("stored permitted region JSON is not canonical")
    return region


def _encode_constraints(value: Tuple[AllocationConstraintCode, ...]) -> str:
    if type(value) is not tuple or any(
        type(item) is not AllocationConstraintCode for item in value
    ):
        raise ValueError("binding constraints must contain exact AllocationConstraintCode values")
    if len(value) != len(set(value)):
        raise ValueError("binding constraints must not contain duplicates")
    if value != _canonical_constraint_order(value):
        raise ValueError("binding constraints must use canonical enum order")
    return _canonical_json([item.value for item in value])


def _decode_constraints(value: object) -> Tuple[AllocationConstraintCode, ...]:
    parsed = _stored_json(value, "binding constraints")
    if type(parsed) is not list:
        raise ValueError("stored binding constraints must be a JSON array")
    if len(parsed) > len(AllocationConstraintCode):
        raise ValueError("stored binding constraints exceed the supported item limit")
    try:
        constraints = tuple(AllocationConstraintCode(item) for item in parsed if type(item) is str)
    except ValueError:
        raise ValueError("stored binding constraints contain an unsupported code") from None
    if len(constraints) != len(parsed):
        raise ValueError("stored binding constraints must contain strings")
    if len(constraints) != len(set(constraints)):
        raise ValueError("stored binding constraints contain duplicates")
    if constraints != _canonical_constraint_order(constraints):
        raise ValueError("stored binding constraints must use canonical enum order")
    if value != _encode_constraints(constraints):
        raise ValueError("stored binding constraints JSON is not canonical")
    return constraints


def _encode_safe_summary(value: AllocationSafeSummary) -> str:
    if type(value) is not AllocationSafeSummary:
        raise ValueError("safe summary must be an exact AllocationSafeSummary")
    value.validate()
    return _canonical_json(
        {
            "fully_funded_now_count": value.fully_funded_now_count,
            "fundable_without_return_count": value.fundable_without_return_count,
            "funding_gap_without_return_count": value.funding_gap_without_return_count,
            "goal_count": value.goal_count,
            "horizon_equity_ceilings": [
                _decimal_text(item) for item in value.horizon_equity_ceilings
            ],
            "obligation_count": value.obligation_count,
        }
    )


def _decode_safe_summary(value: object) -> AllocationSafeSummary:
    parsed = _stored_json(value, "safe summary")
    expected_keys = {
        "fully_funded_now_count",
        "fundable_without_return_count",
        "funding_gap_without_return_count",
        "goal_count",
        "horizon_equity_ceilings",
        "obligation_count",
    }
    if type(parsed) is not dict or set(parsed) != expected_keys:
        raise ValueError("stored safe summary has unexpected fields")
    horizons = parsed["horizon_equity_ceilings"]
    if type(horizons) is not list:
        raise ValueError("stored safe summary horizons must be an array")
    if len(horizons) > MAX_COLLECTION_ITEMS:
        raise ValueError("stored safe summary horizons exceed the item limit")
    summary = AllocationSafeSummary(
        goal_count=_stored_non_negative_integer(parsed["goal_count"], "goal count"),
        obligation_count=_stored_non_negative_integer(
            parsed["obligation_count"], "obligation count"
        ),
        fully_funded_now_count=_stored_non_negative_integer(
            parsed["fully_funded_now_count"], "fully funded count"
        ),
        fundable_without_return_count=_stored_non_negative_integer(
            parsed["fundable_without_return_count"], "fundable count"
        ),
        funding_gap_without_return_count=_stored_non_negative_integer(
            parsed["funding_gap_without_return_count"], "funding gap count"
        ),
        horizon_equity_ceilings=tuple(
            _stored_decimal(item, "horizon equity ceiling") for item in horizons
        ),
    )
    summary.validate()
    if value != _encode_safe_summary(summary):
        raise ValueError("stored safe summary JSON is not canonical")
    return summary


def _validate_plaintext_semantics(
    permitted_region: Optional[PermittedRegion],
    constraints: Tuple[AllocationConstraintCode, ...],
    safe_summary: AllocationSafeSummary,
) -> None:
    no_stock = AllocationConstraintCode.NO_CURRENT_INVESTABLE_STOCK in constraints
    if (permitted_region is None) != no_stock:
        raise ValueError("stored permitted region must match no_current_investable_stock")
    has_funding_gap = AllocationConstraintCode.FUNDING_GAP_WITHOUT_RETURN in constraints
    if has_funding_gap != bool(safe_summary.funding_gap_without_return_count):
        raise ValueError("stored funding-gap constraint must match safe summary")
    local_codes = {
        AllocationConstraintCode.HORIZON_BINDING,
        AllocationConstraintCode.LOSS_AMOUNT_BINDING,
        AllocationConstraintCode.DRAWDOWN_BINDING,
        AllocationConstraintCode.WILLINGNESS_BINDING,
        AllocationConstraintCode.STABILITY_BINDING,
    }
    if permitted_region is None:
        if local_codes.intersection(constraints):
            raise ValueError("stored no-stock allocation cannot contain local bindings")
        return
    binding_by_ceiling = {
        AllocationConstraintCode.HORIZON_BINDING: (permitted_region.horizon_equity_ceiling),
        AllocationConstraintCode.LOSS_AMOUNT_BINDING: (permitted_region.loss_amount_equity_ceiling),
        AllocationConstraintCode.DRAWDOWN_BINDING: (permitted_region.drawdown_equity_ceiling),
        AllocationConstraintCode.WILLINGNESS_BINDING: (permitted_region.willingness_equity_ceiling),
        AllocationConstraintCode.STABILITY_BINDING: (permitted_region.stability_equity_ceiling),
    }
    expected = {
        code
        for code, ceiling in binding_by_ceiling.items()
        if ceiling == permitted_region.maximum_equity
    }
    if set(constraints).intersection(local_codes) != expected:
        raise ValueError("stored local binding codes do not match permitted region")


def _validate_encrypted(
    value: EncryptedAllocationAssessment,
    *,
    require_fixed_metadata: bool = True,
) -> None:
    expected_state = {field.name for field in fields(EncryptedAllocationAssessment)}
    if (
        type(value) is not EncryptedAllocationAssessment
        or type(vars(value)) is not dict
        or set(vars(value)) != expected_state
    ):
        raise ValueError("encrypted allocation assessment must use its exact declared type")
    if type(value.algorithm) is not str or not value.algorithm or "\x00" in value.algorithm:
        raise ValueError("encrypted allocation assessment algorithm is invalid")
    if type(value.key_version) is not str or not value.key_version or "\x00" in value.key_version:
        raise ValueError("encrypted allocation assessment key version is invalid")
    if require_fixed_metadata and value.algorithm != "AES-256-GCM":
        raise ValueError("encrypted allocation assessment algorithm is invalid")
    if require_fixed_metadata and value.key_version != "1":
        raise ValueError("encrypted allocation assessment key version is invalid")
    _decode_base64(value.nonce, "nonce", expected_length=12)
    ciphertext = _decode_base64(
        value.ciphertext,
        "ciphertext",
        maximum_length=MAX_EXACT_PAYLOAD_BYTES + 16,
    )
    if len(ciphertext) < 16:
        raise ValueError("encrypted allocation assessment ciphertext is too short")
    _lower_hex_digest(value.keyed_fingerprint, "encrypted allocation fingerprint")


def _decode_base64(
    value: object,
    name: str,
    *,
    expected_length: Optional[int] = None,
    maximum_length: Optional[int] = None,
) -> bytes:
    if type(value) is not str or not value or len(value) % 4:
        raise ValueError(f"encrypted allocation assessment {name} is invalid")
    if expected_length is not None and len(value) != _base64_encoded_length(expected_length):
        raise ValueError(f"encrypted allocation assessment {name} has an invalid length")
    if maximum_length is not None and len(value) > _base64_encoded_length(maximum_length):
        raise ValueError(f"encrypted allocation assessment {name} exceeds the size limit")
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeError, ValueError, binascii.Error):
        raise ValueError(f"encrypted allocation assessment {name} is invalid") from None
    if base64.urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ValueError(f"encrypted allocation assessment {name} is not canonical base64")
    if expected_length is not None and len(decoded) != expected_length:
        raise ValueError(f"encrypted allocation assessment {name} has an invalid length")
    if maximum_length is not None and len(decoded) > maximum_length:
        raise ValueError(f"encrypted allocation assessment {name} exceeds the size limit")
    return decoded


def _stored_json(value: object, name: str) -> object:
    if type(value) is not str:
        raise ValueError(f"stored {name} must be valid JSON")
    if len(value) > MAX_EXACT_PAYLOAD_BYTES:
        raise ValueError(f"stored {name} exceeds the size limit")
    try:
        return json.loads(
            value,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=lambda _: _raise_invalid_json(name),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"stored {name} must be valid JSON: {exc}") from None


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("stored JSON contains a duplicate key")
        result[key] = value
    return result


def _raise_invalid_json(name: str) -> None:
    raise ValueError(f"stored {name} contains an unsupported JSON constant")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("allocation percentage must be an exact finite Decimal")
    if value.is_zero():
        return "0"
    return format(value.normalize(), "f")


def _stored_decimal(value: object, name: str) -> Decimal:
    if type(value) is not str or not value or value.startswith(("+", "-")):
        raise ValueError(f"stored {name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"stored {name} must be a canonical decimal string") from None
    if not parsed.is_finite() or _decimal_text(parsed) != value:
        raise ValueError(f"stored {name} must be a canonical decimal string")
    return parsed


def _canonical_utc_datetime(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an exact timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _canonical_utc_text(value: datetime, name: str) -> str:
    return _canonical_utc_datetime(value, name).isoformat()


def _stored_utc_datetime(value: object, name: str) -> datetime:
    parsed = _stored_aware_datetime(value, name)
    if type(value) is not str or not value.endswith("+00:00") or parsed.tzinfo is not timezone.utc:
        raise ValueError(f"stored {name} must use canonical UTC")
    if parsed.isoformat() != value:
        raise ValueError(f"stored {name} must use canonical UTC")
    return parsed


def _stored_aware_datetime(value: object, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"stored {name} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"stored {name} must be an ISO datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError(f"stored {name} must be an ISO datetime")
    return parsed


def _required_text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    return value


def _lower_hex_digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase 64-character hex digest")
    return value


def _positive_integer(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _strict_integer(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"stored {name} must be an integer")
    return value


def _strict_positive_integer(value: object, name: str) -> int:
    parsed = _strict_integer(value, name)
    if parsed <= 0:
        raise ValueError(f"stored {name} must be positive")
    return parsed


def _stored_non_negative_integer(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"stored {name} must be a non-negative integer")
    if len(str(value)) > MAX_INTEGER_DIGITS or value > MAX_COLLECTION_ITEMS:
        raise ValueError(f"stored {name} exceeds the supported limit")
    return value


def _base64_encoded_length(decoded_length: int) -> int:
    return 4 * ((decoded_length + 2) // 3)


def _canonical_constraint_order(
    value: Tuple[AllocationConstraintCode, ...],
) -> Tuple[AllocationConstraintCode, ...]:
    selected = set(value)
    return tuple(item for item in AllocationConstraintCode if item in selected)


def _owned_repository(repository: object) -> Repository:
    if (
        type(repository) is not Repository
        or type(vars(repository)) is not dict
        or set(vars(repository)) != {"database"}
    ):
        raise ValueError("repository must be an exact declared Repository")
    database = repository.database
    if type(database) is not type(Path()) or "\x00" in str(database):
        raise ValueError("repository database path is unsupported")
    try:
        resolved = database.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("repository database path is unsupported") from None
    if type(resolved) is not type(Path()) or "\x00" in str(resolved):
        raise ValueError("repository database path is unsupported")
    return Repository(resolved)
