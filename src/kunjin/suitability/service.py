from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional, Tuple

from kunjin.suitability.assessment_serialization import (
    decode_assessment_amounts,
    encode_assessment_amounts,
)
from kunjin.suitability.crypto import (
    AssessmentCipher,
    ProfileCipher,
    ProfileCryptoError,
)
from kunjin.suitability.engine import evaluate
from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    FinancialProfile,
)
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.serialization import decode_profile, encode_profile
from kunjin.suitability.store import (
    ActiveProfileChangedError,
    AssessmentMetadata,
    ProfileInvalidationReason,
    ProfileStore,
    ProfileVersionMetadata,
    StoredEncryptedProfile,
    SuitabilityAssessmentStore,
    SuitabilityPolicyStore,
)


class SuitabilityPolicyError(RuntimeError):
    code = "policy_unavailable"


class SuitabilityAssessmentError(RuntimeError):
    code = "assessment_calculation_failed"


class SuitabilitySnapshotUnavailableError(SuitabilityAssessmentError):
    """No current authenticated Phase B assessment exists for this profile."""


@dataclass(frozen=True)
class LoadedProfile:
    metadata: ProfileVersionMetadata
    profile: FinancialProfile
    encrypted_keyed_fingerprint: str


@dataclass(frozen=True)
class AuthenticatedSuitabilitySnapshot:
    profile: FinancialProfile
    profile_version_id: int
    profile_version: int
    profile_keyed_fingerprint: str
    profile_valid_until: datetime
    result: AssessmentResult
    assessment_id: int
    input_fingerprint: str
    policy_version: str
    policy_checksum: str
    assessed_at: datetime
    valid_until: datetime

    @property
    def status(self) -> AssessmentStatus:
        return self.result.status

    @property
    def hard_blocks(self):
        return self.result.hard_blocks

    @property
    def constraints(self):
        return self.result.constraints

    @property
    def profile_conflicts(self):
        return self.result.profile_conflicts

    @property
    def debt_count(self) -> int:
        return self.result.debt_count

    @property
    def obligation_count(self) -> int:
        return self.result.obligation_count

    @property
    def goal_count(self) -> int:
        return self.result.goal_count


@dataclass(frozen=True)
class SuitabilityExecution:
    result: AssessmentResult
    assessment_id: Optional[int]
    profile_version_id: Optional[int]
    profile_version: Optional[int]
    policy_version: str
    assessed_at: datetime
    valid_until: Optional[datetime]
    freshness: str

    def safe_json(self) -> Dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "profile_version": self.profile_version,
            "policy_version": self.policy_version,
            "status": self.result.status.value,
            "hard_blocks": [item.value for item in self.result.hard_blocks],
            "constraints": [item.value for item in self.result.constraints],
            "profile_conflicts": [item.value for item in self.result.profile_conflicts],
            "required_reserve_months": self.result.required_reserve_months,
            "risk_answers_consistent": self.result.risk_answers_consistent,
            "debt_count": self.result.debt_count,
            "obligation_count": self.result.obligation_count,
            "goal_count": self.result.goal_count,
            "assessed_at": self.assessed_at.isoformat(),
            "valid_until": (None if self.valid_until is None else self.valid_until.isoformat()),
            "freshness": self.freshness,
            "capability": "research_only",
        }

    def local_view(self) -> Dict[str, object]:
        view = self.safe_json()
        if self.assessment_id is not None:
            view["amounts"] = _local_amounts(self.result.amounts)
        return view


class ProfileService:
    VALIDITY_DAYS = 90

    def __init__(
        self,
        store: ProfileStore,
        cipher: ProfileCipher,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._now = now

    def confirm_profile(self, profile: FinancialProfile) -> ProfileVersionMetadata:
        confirmed_at = self._current_time()
        canonical_profile = replace(profile, confirmed_at=confirmed_at)
        canonical_profile.validate()
        plaintext = encode_profile(canonical_profile)
        encrypted = self._cipher.encrypt(plaintext)
        return self._store.confirm(
            encrypted,
            confirmed_at,
            confirmed_at + timedelta(days=self.VALIDITY_DAYS),
        )

    def load_active_profile(self) -> Optional[FinancialProfile]:
        loaded = self.load_active()
        return None if loaded is None else loaded.profile

    def load_active(self) -> Optional[LoadedProfile]:
        active = self._store.active_encrypted()
        if active is None:
            return None
        return LoadedProfile(
            metadata=active.metadata,
            profile=self._decode_stored(active),
            encrypted_keyed_fingerprint=active.encrypted.keyed_fingerprint,
        )

    def load_by_id(self, profile_version_id: int) -> Optional[LoadedProfile]:
        stored = self._store.encrypted_by_id(profile_version_id)
        if stored is None:
            return None
        return LoadedProfile(
            metadata=stored.metadata,
            profile=self._decode_stored(stored),
            encrypted_keyed_fingerprint=stored.encrypted.keyed_fingerprint,
        )

    def latest_metadata(self) -> Optional[ProfileVersionMetadata]:
        return self._store.latest_metadata()

    def status(self) -> Dict[str, object]:
        active = self._store.active_encrypted()
        if active is None:
            return {"state": "missing", "freshness": "missing"}
        self._decode_stored(active)
        metadata = active.metadata
        current = self._current_time()
        return {
            "state": metadata.status,
            "version": metadata.version,
            "confirmed_at": metadata.confirmed_at.isoformat(),
            "valid_until": metadata.valid_until.isoformat(),
            "freshness": "fresh" if current < metadata.valid_until else "stale",
        }

    def history(self) -> Tuple[Dict[str, object], ...]:
        return tuple(self._history_item(item) for item in self._store.history())

    def invalidate(self, reason: object) -> Optional[ProfileVersionMetadata]:
        reason_code = ProfileInvalidationReason.parse(reason)
        return self._store.invalidate_active(reason_code, self._current_time())

    def _decode_stored(self, active: StoredEncryptedProfile) -> FinancialProfile:
        try:
            plaintext = self._cipher.decrypt(active.encrypted)
            profile = decode_profile(plaintext)
            if profile.confirmed_at != active.metadata.confirmed_at:
                raise ValueError("profile confirmation metadata does not match payload")
            if active.metadata.valid_until != profile.confirmed_at + timedelta(
                days=self.VALIDITY_DAYS
            ):
                raise ValueError("profile validity metadata does not match payload")
            return profile
        except ProfileCryptoError:
            raise
        except ValueError:
            raise ProfileCryptoError("profile decryption failed") from None

    def _current_time(self) -> datetime:
        current = self._now()
        if not isinstance(current, datetime):
            raise ValueError("current time must be a datetime")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("current time must be timezone-aware")
        return current

    @staticmethod
    def _history_item(metadata: ProfileVersionMetadata) -> Dict[str, object]:
        return {
            "id": metadata.id,
            "version": metadata.version,
            "status": metadata.status,
            "confirmed_at": metadata.confirmed_at.isoformat(),
            "valid_until": metadata.valid_until.isoformat(),
            "invalidated_at": (
                None if metadata.invalidated_at is None else metadata.invalidated_at.isoformat()
            ),
            "invalidation_reason": metadata.invalidation_reason,
        }


class SuitabilityService:
    def __init__(
        self,
        profile_service: ProfileService,
        policy_store: SuitabilityPolicyStore,
        assessment_store: SuitabilityAssessmentStore,
        assessment_cipher: AssessmentCipher,
        policy: SuitabilityPolicyV1,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._profile_service = profile_service
        self._policy_store = policy_store
        self._assessment_store = assessment_store
        self._assessment_cipher = assessment_cipher
        self._policy = policy
        self._now = now

    def assess(self) -> SuitabilityExecution:
        try:
            return self._assess()
        except (
            ProfileCryptoError,
            SuitabilityPolicyError,
            SuitabilityAssessmentError,
        ):
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be calculated"
            ) from None

    def _assess(self) -> SuitabilityExecution:
        assessed_at = self._current_time()
        loaded = self._profile_service.load_active()
        if loaded is None:
            latest = self._profile_service.latest_metadata()
            reason = (
                BlockReason.PROFILE_INVALIDATED
                if latest is not None and latest.status == "invalidated"
                else BlockReason.PROFILE_MISSING
            )
            return self._readiness_execution(reason, assessed_at, latest)
        if assessed_at >= loaded.metadata.valid_until:
            return self._readiness_execution(
                BlockReason.PROFILE_STALE,
                assessed_at,
                loaded.metadata,
            )

        policy_checksum = self._ensure_policy()
        try:
            result = evaluate(loaded.profile, self._policy, assessed_at)
            encrypted = self._assessment_cipher.encrypt(encode_assessment_amounts(result.amounts))
            input_fingerprint = self._assessment_input_fingerprint(
                loaded,
                policy_checksum,
                assessed_at,
            )
            valid_until = min(
                assessed_at.astimezone(timezone.utc) + timedelta(hours=24),
                loaded.metadata.valid_until,
            )
            metadata = self._assessment_store.insert(
                profile_version_id=loaded.metadata.id,
                policy_version=self._policy.version,
                input_fingerprint=input_fingerprint,
                result=result,
                encrypted=encrypted,
                assessed_at=assessed_at,
                valid_until=valid_until,
            )
            final_loaded = self._profile_service.load_active()
            if not self._same_profile_binding(loaded, final_loaded):
                return self._readiness_execution(
                    BlockReason.PROFILE_STALE,
                    assessed_at,
                    loaded.metadata,
                )
        except ActiveProfileChangedError:
            return self._readiness_execution(
                BlockReason.PROFILE_STALE,
                assessed_at,
                loaded.metadata,
            )
        except ProfileCryptoError:
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be calculated"
            ) from None
        return self._execution(result, metadata, loaded.metadata.version, "fresh")

    def status(self) -> Dict[str, object]:
        try:
            return self._status()
        except (
            ProfileCryptoError,
            SuitabilityPolicyError,
            SuitabilityAssessmentError,
        ):
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be validated"
            ) from None

    def load_authenticated_snapshot(self) -> AuthenticatedSuitabilitySnapshot:
        """Load the current exact Phase B result for trusted internal consumers."""
        try:
            return self._load_authenticated_snapshot()
        except (
            ProfileCryptoError,
            SuitabilityPolicyError,
            SuitabilityAssessmentError,
        ):
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be authenticated"
            ) from None

    def load_authenticated_snapshot_by_ids(
        self,
        profile_version_id: int,
        suitability_assessment_id: int,
    ) -> AuthenticatedSuitabilitySnapshot:
        """Authenticate an immutable historical Phase B provenance chain."""
        try:
            if type(profile_version_id) is not int or profile_version_id <= 0:
                raise ValueError("profile_version_id must be a positive integer")
            if type(suitability_assessment_id) is not int or suitability_assessment_id <= 0:
                raise ValueError("suitability_assessment_id must be a positive integer")
            loaded = self._profile_service.load_by_id(profile_version_id)
            if loaded is None:
                raise SuitabilitySnapshotUnavailableError(
                    "historical suitability profile is unavailable"
                )
            policy_checksum = self._ensure_policy()
            stored = self._assessment_store.get(suitability_assessment_id)
            if stored is None:
                raise SuitabilitySnapshotUnavailableError(
                    "historical suitability assessment is unavailable"
                )
            metadata = stored.metadata
            if (
                metadata.profile_version_id != profile_version_id
                or metadata.policy_version != self._policy.version
            ):
                raise SuitabilityAssessmentError(
                    "historical suitability binding could not be authenticated"
                )
            self._validate_assessment_timing(loaded, metadata)
            expected_fingerprint = self._assessment_input_fingerprint(
                loaded,
                policy_checksum,
                metadata.assessed_at,
            )
            if not hmac.compare_digest(
                metadata.input_fingerprint,
                expected_fingerprint,
            ):
                raise SuitabilityAssessmentError(
                    "historical suitability binding could not be authenticated"
                )
            try:
                amounts = decode_assessment_amounts(
                    self._assessment_cipher.decrypt(stored.encrypted)
                )
            except ProfileCryptoError:
                raise SuitabilityAssessmentError(
                    "historical suitability assessment could not be authenticated"
                ) from None
            expected_result = evaluate(
                loaded.profile,
                self._policy,
                metadata.assessed_at,
            )
            if not self._matches(metadata, amounts, expected_result):
                raise SuitabilityAssessmentError(
                    "historical suitability assessment could not be authenticated"
                )
            return AuthenticatedSuitabilitySnapshot(
                profile=loaded.profile,
                profile_version_id=loaded.metadata.id,
                profile_version=loaded.metadata.version,
                profile_keyed_fingerprint=loaded.encrypted_keyed_fingerprint,
                profile_valid_until=loaded.metadata.valid_until,
                result=expected_result,
                assessment_id=metadata.id,
                input_fingerprint=metadata.input_fingerprint,
                policy_version=metadata.policy_version,
                policy_checksum=policy_checksum,
                assessed_at=metadata.assessed_at,
                valid_until=metadata.valid_until,
            )
        except (
            ProfileCryptoError,
            SuitabilityPolicyError,
            SuitabilityAssessmentError,
        ):
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "historical suitability assessment could not be authenticated"
            ) from None

    def _load_authenticated_snapshot(self) -> AuthenticatedSuitabilitySnapshot:
        current = self._current_time()
        loaded = self._profile_service.load_active()
        if loaded is None or current >= loaded.metadata.valid_until:
            raise SuitabilitySnapshotUnavailableError(
                "current suitability assessment is unavailable"
            )

        policy_checksum = self._ensure_policy()
        try:
            stored = self._assessment_store.latest_for(
                loaded.metadata.id,
                self._policy.version,
            )
            if stored is None or current >= stored.metadata.valid_until:
                raise SuitabilitySnapshotUnavailableError(
                    "current suitability assessment is unavailable"
                )
            metadata = stored.metadata
            self._validate_assessment_timing(loaded, metadata, current)
            expected_fingerprint = self._assessment_input_fingerprint(
                loaded,
                policy_checksum,
                metadata.assessed_at,
            )
            if not hmac.compare_digest(
                metadata.input_fingerprint,
                expected_fingerprint,
            ):
                raise SuitabilityAssessmentError(
                    "current suitability assessment could not be authenticated"
                )
            try:
                amounts = decode_assessment_amounts(
                    self._assessment_cipher.decrypt(stored.encrypted)
                )
            except ProfileCryptoError:
                raise SuitabilityAssessmentError(
                    "current suitability assessment could not be authenticated"
                ) from None
            expected_result = evaluate(
                loaded.profile,
                self._policy,
                metadata.assessed_at,
            )
            if not self._matches(metadata, amounts, expected_result):
                raise SuitabilityAssessmentError(
                    "current suitability assessment could not be authenticated"
                )
            final_loaded = self._profile_service.load_active()
            if not self._same_profile_binding(loaded, final_loaded):
                raise SuitabilityAssessmentError("current suitability assessment binding changed")
        except (ProfileCryptoError, SuitabilityAssessmentError):
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be authenticated"
            ) from None
        return AuthenticatedSuitabilitySnapshot(
            profile=loaded.profile,
            profile_version_id=loaded.metadata.id,
            profile_version=loaded.metadata.version,
            profile_keyed_fingerprint=loaded.encrypted_keyed_fingerprint,
            profile_valid_until=loaded.metadata.valid_until,
            result=expected_result,
            assessment_id=metadata.id,
            input_fingerprint=metadata.input_fingerprint,
            policy_version=metadata.policy_version,
            policy_checksum=policy_checksum,
            assessed_at=metadata.assessed_at,
            valid_until=metadata.valid_until,
        )

    def _status(self) -> Dict[str, object]:
        current = self._current_time()
        loaded = self._profile_service.load_active()
        if loaded is None:
            return self._stale_history_or_missing()
        if current >= loaded.metadata.valid_until:
            return self._stale_history_or_missing(force_stale=True)

        policy_checksum = self._ensure_policy()
        try:
            stored = self._assessment_store.latest_for(
                loaded.metadata.id,
                self._policy.version,
            )
            if stored is None:
                return self._stale_history_or_missing()
            metadata = stored.metadata
            self._validate_assessment_timing(loaded, metadata)
            if metadata.assessed_at > current:
                raise SuitabilityAssessmentError(
                    "suitability assessment timing could not be authenticated"
                )
            if current >= metadata.valid_until:
                return self._metadata_status(metadata, "stale")
            expected_fingerprint = self._assessment_input_fingerprint(
                loaded,
                policy_checksum,
                metadata.assessed_at,
            )
            if not hmac.compare_digest(
                metadata.input_fingerprint,
                expected_fingerprint,
            ):
                return self._metadata_status(metadata, "stale")
            amounts = decode_assessment_amounts(self._assessment_cipher.decrypt(stored.encrypted))
            expected_result = evaluate(
                loaded.profile,
                self._policy,
                metadata.assessed_at,
            )
            if not self._matches(metadata, amounts, expected_result):
                return self._metadata_status(metadata, "stale")
            final_loaded = self._profile_service.load_active()
            if not self._same_profile_binding(loaded, final_loaded):
                return self._metadata_status(metadata, "stale")
        except ProfileCryptoError:
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment could not be validated"
            ) from None
        return self._metadata_status(metadata, "fresh")

    def history(self) -> Tuple[Dict[str, object], ...]:
        try:
            history = self._assessment_store.history()
            for metadata in history:
                self.load_authenticated_snapshot_by_ids(
                    metadata.profile_version_id,
                    metadata.id,
                )
            return tuple(self._history_item(metadata) for metadata in history)
        except SuitabilityAssessmentError:
            raise
        except Exception:
            raise SuitabilityAssessmentError(
                "suitability assessment history is unavailable"
            ) from None

    def _ensure_policy(self) -> str:
        try:
            record = self._policy_store.ensure(self._policy)
            if record.policy_checksum != self._policy.checksum():
                raise ValueError("policy checksum does not match")
            return record.policy_checksum
        except Exception:
            raise SuitabilityPolicyError("suitability policy is unavailable") from None

    def _assessment_input_fingerprint(
        self,
        loaded: LoadedProfile,
        policy_checksum: str,
        assessed_at: datetime,
    ) -> str:
        fingerprint_input = "|".join(
            (
                str(loaded.metadata.id),
                loaded.encrypted_keyed_fingerprint,
                policy_checksum,
                assessed_at.astimezone(timezone.utc).isoformat(),
            )
        ).encode("ascii")
        return self._assessment_cipher.fingerprint(fingerprint_input)

    def _validate_assessment_timing(
        self,
        loaded: LoadedProfile,
        metadata: AssessmentMetadata,
        current: Optional[datetime] = None,
    ) -> None:
        expected_valid_until = min(
            metadata.assessed_at.astimezone(timezone.utc)
            + timedelta(hours=self._policy.assessment_freshness_hours),
            loaded.metadata.valid_until.astimezone(timezone.utc),
        )
        valid = (
            metadata.created_at == metadata.assessed_at
            and loaded.metadata.confirmed_at <= metadata.assessed_at < loaded.metadata.valid_until
            and metadata.valid_until == expected_valid_until
        )
        if current is not None:
            current_utc = current.astimezone(timezone.utc)
            valid = valid and metadata.assessed_at <= current_utc < metadata.valid_until
        if not valid:
            raise SuitabilityAssessmentError(
                "suitability assessment timing could not be authenticated"
            )

    def _readiness_execution(
        self,
        reason: BlockReason,
        assessed_at: datetime,
        metadata: Optional[ProfileVersionMetadata],
    ) -> SuitabilityExecution:
        result = AssessmentResult(
            status=AssessmentStatus.BLOCKED,
            hard_blocks=(reason,),
            constraints=(),
            required_reserve_months=0,
            risk_answers_consistent=True,
            profile_conflicts=(),
            debt_count=0,
            obligation_count=0,
            goal_count=0,
            amounts=AssessmentAmounts.zero(),
        )
        result.validate()
        return SuitabilityExecution(
            result=result,
            assessment_id=None,
            profile_version_id=None if metadata is None else metadata.id,
            profile_version=None if metadata is None else metadata.version,
            policy_version=self._policy.version,
            assessed_at=assessed_at,
            valid_until=None,
            freshness="transient",
        )

    @staticmethod
    def _execution(
        result: AssessmentResult,
        metadata: AssessmentMetadata,
        profile_version: int,
        freshness: str,
    ) -> SuitabilityExecution:
        return SuitabilityExecution(
            result=result,
            assessment_id=metadata.id,
            profile_version_id=metadata.profile_version_id,
            profile_version=profile_version,
            policy_version=metadata.policy_version,
            assessed_at=metadata.assessed_at,
            valid_until=metadata.valid_until,
            freshness=freshness,
        )

    @staticmethod
    def _matches(
        metadata: AssessmentMetadata,
        amounts: AssessmentAmounts,
        result: AssessmentResult,
    ) -> bool:
        return (
            metadata.status is result.status
            and metadata.hard_blocks == result.hard_blocks
            and metadata.constraints == result.constraints
            and metadata.safe_summary.as_dict() == result.safe_summary()
            and amounts == result.amounts
        )

    @staticmethod
    def _same_profile_binding(
        expected: LoadedProfile,
        current: Optional[LoadedProfile],
    ) -> bool:
        return (
            current is not None
            and current.metadata.id == expected.metadata.id
            and hmac.compare_digest(
                current.encrypted_keyed_fingerprint,
                expected.encrypted_keyed_fingerprint,
            )
        )

    @staticmethod
    def _nonfresh_status(state: str) -> Dict[str, object]:
        return {
            "state": state,
            "freshness": state,
            "capability": "research_only",
        }

    def _stale_history_or_missing(
        self,
        force_stale: bool = False,
    ) -> Dict[str, object]:
        history = self._assessment_store.history()
        if history:
            return self._metadata_status(history[0], "stale")
        return self._nonfresh_status("stale" if force_stale else "missing")

    @staticmethod
    def _metadata_status(
        metadata: AssessmentMetadata,
        freshness: str,
    ) -> Dict[str, object]:
        return {
            "state": freshness,
            "freshness": freshness,
            "assessment_id": metadata.id,
            "profile_version_id": metadata.profile_version_id,
            "policy_version": metadata.policy_version,
            "status": metadata.status.value,
            "hard_blocks": [item.value for item in metadata.hard_blocks],
            "constraints": [item.value for item in metadata.constraints],
            "assessed_at": metadata.assessed_at.isoformat(),
            "valid_until": metadata.valid_until.isoformat(),
            "capability": "research_only",
        }

    @staticmethod
    def _history_item(metadata: AssessmentMetadata) -> Dict[str, object]:
        return {
            "assessment_id": metadata.id,
            "profile_version_id": metadata.profile_version_id,
            "policy_version": metadata.policy_version,
            "status": metadata.status.value,
            "hard_blocks": [item.value for item in metadata.hard_blocks],
            "constraints": [item.value for item in metadata.constraints],
            "assessed_at": metadata.assessed_at.isoformat(),
            "valid_until": metadata.valid_until.isoformat(),
            "capability": "research_only",
        }

    def _current_time(self) -> datetime:
        current = self._now()
        if type(current) is not datetime:
            raise SuitabilityAssessmentError("current time must be a datetime")
        if current.tzinfo is None or current.utcoffset() is None:
            raise SuitabilityAssessmentError("current time must be timezone-aware")
        return current.astimezone(timezone.utc)


def _local_amounts(amounts: AssessmentAmounts) -> Dict[str, str]:
    encoded = encode_assessment_amounts(amounts)
    return json.loads(encoded.decode("utf-8"))
