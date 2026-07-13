from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Dict, Optional, Tuple

from kunjin.allocation.crypto import AllocationCipher
from kunjin.allocation.engine import evaluate_allocation
from kunjin.allocation.models import (
    AllocationBlockCode,
    AllocationConstraintCode,
    AllocationResult,
    AllocationSafeSummary,
    AllocationStatus,
)
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.allocation.serialization import decode_exact_result, encode_exact_result
from kunjin.allocation.store import (
    AllocationAssessmentMetadata,
    AllocationAssessmentStore,
    AllocationBindingChangedError,
    AllocationPolicyStore,
    StoredEncryptedAllocationAssessment,
)
from kunjin.suitability.crypto import ProfileCryptoError
from kunjin.suitability.models import AssessmentStatus
from kunjin.suitability.service import (
    AuthenticatedSuitabilitySnapshot,
    SuitabilityAssessmentError,
    SuitabilityPolicyError,
    SuitabilityService,
    SuitabilitySnapshotUnavailableError,
)


class AllocationPolicyError(RuntimeError):
    code = "allocation_policy_unavailable"


class AllocationCalculationError(RuntimeError):
    code = "allocation_calculation_failed"


class EncryptedProfileUnavailableError(RuntimeError):
    code = "encrypted_profile_unavailable"


@dataclass(frozen=True)
class AllocationExecution:
    result: AllocationResult
    assessment_id: Optional[int]
    profile_version_id: int
    profile_version: int
    suitability_assessment_id: int
    policy_version: str
    assessed_at: datetime
    valid_until: Optional[datetime]
    freshness: str

    @property
    def status(self) -> AllocationStatus:
        return self.result.status

    @property
    def blocks(self) -> Tuple[AllocationBlockCode, ...]:
        return self.result.blocks

    @property
    def permitted_region(self):
        return self.result.permitted_region

    def safe_json(self) -> Dict[str, object]:
        return {
            "assessment_id": self.assessment_id,
            "profile_version": self.profile_version,
            "suitability_assessment_id": self.suitability_assessment_id,
            "policy_version": self.policy_version,
            "status": self.result.status.value,
            "blocks": [item.value for item in self.result.blocks],
            "binding_constraints": [item.value for item in self.result.binding_constraints],
            "profile_conflicts": [item.value for item in self.result.profile_conflicts],
            "safe_summary": _safe_summary_view(self.result.safe_summary),
            "permitted_region": _permitted_region_view(self.result.permitted_region),
            "assessed_at": self.assessed_at.isoformat(),
            "valid_until": (None if self.valid_until is None else self.valid_until.isoformat()),
            "freshness": self.freshness,
            "capability": "research_only",
        }

    def local_view(self) -> Dict[str, object]:
        view = self.safe_json()
        if self.result.exact is not None:
            view["exact"] = json.loads(encode_exact_result(self.result.exact).decode("utf-8"))
        return view


class AllocationService:
    def __init__(
        self,
        suitability_service: SuitabilityService,
        policy_store: AllocationPolicyStore,
        assessment_store: AllocationAssessmentStore,
        cipher: AllocationCipher,
        policy: AllocationPolicyV1,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._suitability_service = suitability_service
        self._policy_store = policy_store
        self._assessment_store = assessment_store
        self._cipher = cipher
        self._policy = policy
        self._now = now

    def ranges(self) -> AllocationExecution:
        try:
            return self._ranges()
        except EncryptedProfileUnavailableError:
            raise
        except ProfileCryptoError:
            raise EncryptedProfileUnavailableError("encrypted profile is unavailable") from None
        except AllocationPolicyError:
            raise
        except (SuitabilityPolicyError, SuitabilityAssessmentError):
            raise AllocationCalculationError(
                "allocation inputs could not be authenticated"
            ) from None
        except AllocationCalculationError:
            raise
        except Exception:
            raise AllocationCalculationError("allocation range could not be calculated") from None

    def _ranges(self) -> AllocationExecution:
        assessed_at = self._current_time()
        snapshot = self._suitability_service.load_authenticated_snapshot()
        policy_checksum = self._ensure_policy()
        if snapshot.result.status is AssessmentStatus.BLOCKED:
            result = _suitability_blocked_result(snapshot)
            return self._execution(result, None, snapshot, assessed_at, None, "transient")

        result = evaluate_allocation(
            snapshot.profile,
            snapshot.result,
            self._policy,
            assessed_at,
        )
        if result.status is AllocationStatus.BLOCKED:
            return self._execution(result, None, snapshot, assessed_at, None, "transient")
        if result.exact is None:
            raise AllocationCalculationError(
                "allocation range did not contain authenticated exact results"
            )

        input_fingerprint = self._input_fingerprint(
            snapshot,
            policy_checksum,
            assessed_at,
        )
        encrypted = self._cipher.encrypt(encode_exact_result(result.exact))
        valid_until = min(
            assessed_at + timedelta(hours=self._policy.assessment_freshness_hours),
            snapshot.profile_valid_until.astimezone(timezone.utc),
            snapshot.valid_until.astimezone(timezone.utc),
        )
        existing = self._assessment_store.latest_for(
            snapshot.profile_version_id,
            snapshot.assessment_id,
            self._policy.version,
        )
        persisted_assessed_at = assessed_at
        persisted_valid_until = valid_until
        if existing is not None and hmac.compare_digest(
            existing.metadata.input_fingerprint,
            input_fingerprint,
        ):
            persisted_assessed_at = existing.metadata.assessed_at
            persisted_valid_until = existing.metadata.valid_until
        try:
            metadata = self._assessment_store.insert(
                profile_version_id=snapshot.profile_version_id,
                suitability_assessment_id=snapshot.assessment_id,
                expected_profile_fingerprint=snapshot.profile_keyed_fingerprint,
                expected_suitability_input_fingerprint=snapshot.input_fingerprint,
                suitability_policy_version=snapshot.policy_version,
                policy_version=self._policy.version,
                input_fingerprint=input_fingerprint,
                result=result,
                encrypted=encrypted,
                assessed_at=persisted_assessed_at,
                valid_until=persisted_valid_until,
            )
        except AllocationBindingChangedError:
            raise AllocationCalculationError(
                "allocation input binding changed before persistence"
            ) from None

        final_snapshot = self._suitability_service.load_authenticated_snapshot()
        if not _same_snapshot_binding(snapshot, final_snapshot):
            raise AllocationCalculationError("allocation input binding changed after persistence")
        stored = self._assessment_store.get(metadata.id)
        if stored is None:
            raise AllocationCalculationError("persisted allocation assessment is unavailable")
        authenticated = self._authenticate_stored_current(
            stored,
            final_snapshot,
            policy_checksum,
            assessed_at,
        )
        return self._execution(
            authenticated,
            metadata,
            final_snapshot,
            metadata.assessed_at,
            metadata.valid_until,
            "fresh",
        )

    def status(self) -> Dict[str, object]:
        try:
            return self._status()
        except ProfileCryptoError:
            raise EncryptedProfileUnavailableError("encrypted profile is unavailable") from None
        except AllocationPolicyError:
            raise
        except (SuitabilityPolicyError, SuitabilityAssessmentError):
            raise AllocationCalculationError(
                "allocation inputs could not be authenticated"
            ) from None
        except (EncryptedProfileUnavailableError, AllocationCalculationError):
            raise
        except Exception:
            raise AllocationCalculationError("allocation status could not be validated") from None

    def _status(self) -> Dict[str, object]:
        current = self._current_time()
        try:
            snapshot = self._suitability_service.load_authenticated_snapshot()
        except SuitabilitySnapshotUnavailableError:
            return self._stale_history_or_missing()
        policy_checksum = self._ensure_policy()
        if snapshot.result.status is AssessmentStatus.BLOCKED:
            return self._stale_history_or_missing()
        stored = self._assessment_store.latest_for(
            snapshot.profile_version_id,
            snapshot.assessment_id,
            self._policy.version,
        )
        if stored is None:
            return self._stale_history_or_missing()
        self._authenticate_historical(stored)
        if current >= stored.metadata.valid_until:
            return _metadata_status(stored.metadata, "stale")
        self._authenticate_stored_current(stored, snapshot, policy_checksum, current)
        try:
            final_snapshot = self._suitability_service.load_authenticated_snapshot()
        except SuitabilitySnapshotUnavailableError:
            return _metadata_status(stored.metadata, "stale")
        if not _same_snapshot_binding(snapshot, final_snapshot):
            return _metadata_status(stored.metadata, "stale")
        return _metadata_status(stored.metadata, "fresh")

    def history(self) -> Tuple[Dict[str, object], ...]:
        try:
            views = []
            for metadata in self._assessment_store.history():
                stored = self._assessment_store.get(metadata.id)
                if stored is None:
                    raise ValueError("stored allocation assessment is unavailable")
                self._authenticate_historical(stored)
                views.append(_history_item(stored.metadata))
            return tuple(views)
        except Exception:
            raise AllocationCalculationError("allocation history is unavailable") from None

    def policy(self) -> Dict[str, object]:
        record = self._ensure_policy_record()
        fixed = self._policy
        return {
            "version": record.version,
            "checksum": record.policy_checksum,
            "effective_at": record.effective_at.isoformat(),
            "stress_loss_by_layer": {
                layer.value: _decimal_text(value) for layer, value in fixed.stress_loss_by_layer
            },
            "horizon_equity_ceilings": [
                {
                    "maximum_years": years,
                    "equity_ceiling": _decimal_text(value),
                }
                for years, value in fixed.horizon_equity_ceilings
            ],
            "willingness_equity_ceilings": {
                name: _decimal_text(value) for name, value in fixed.willingness_equity_ceilings
            },
            "stability_equity_ceilings": {
                name: _decimal_text(value) for name, value in fixed.stability_equity_ceilings
            },
            "capability": "research_only",
        }

    def _authenticate_payload(
        self,
        stored: StoredEncryptedAllocationAssessment,
    ) -> AllocationResult:
        try:
            exact = decode_exact_result(self._cipher.decrypt(stored.encrypted))
        except ProfileCryptoError:
            raise AllocationCalculationError(
                "encrypted allocation assessment could not be authenticated"
            ) from None
        result = AllocationResult(
            status=stored.metadata.status,
            capability="research_only",
            blocks=(),
            binding_constraints=stored.metadata.binding_constraints,
            profile_conflicts=(),
            safe_summary=stored.metadata.safe_summary,
            permitted_region=stored.metadata.permitted_region,
            exact=exact,
        )
        try:
            result.validate()
        except ValueError:
            raise AllocationCalculationError(
                "stored allocation assessment is internally inconsistent"
            ) from None
        return result

    def _authenticate_stored_current(
        self,
        stored: StoredEncryptedAllocationAssessment,
        snapshot: AuthenticatedSuitabilitySnapshot,
        policy_checksum: str,
        current: datetime,
    ) -> AllocationResult:
        metadata = stored.metadata
        if not metadata.assessed_at <= current < metadata.valid_until:
            raise AllocationCalculationError("allocation assessment is stale")
        authenticated, historical = self._authenticate_historical_with_snapshot(stored)
        if not _same_snapshot_binding(historical, snapshot):
            raise AllocationCalculationError(
                "allocation assessment is not bound to the current suitability result"
            )
        expected_fingerprint = self._input_fingerprint(
            snapshot,
            policy_checksum,
            metadata.assessed_at,
        )
        if not hmac.compare_digest(metadata.input_fingerprint, expected_fingerprint):
            raise AllocationCalculationError(
                "allocation input fingerprint could not be authenticated"
            )
        return authenticated

    def _authenticate_historical(
        self,
        stored: StoredEncryptedAllocationAssessment,
    ) -> AllocationResult:
        authenticated, _ = self._authenticate_historical_with_snapshot(stored)
        return authenticated

    def _authenticate_historical_with_snapshot(
        self,
        stored: StoredEncryptedAllocationAssessment,
    ) -> Tuple[AllocationResult, AuthenticatedSuitabilitySnapshot]:
        authenticated = self._authenticate_payload(stored)
        metadata = stored.metadata
        policy_checksum = self._ensure_policy()
        if metadata.policy_version != self._policy.version:
            raise AllocationCalculationError(
                "historical allocation policy binding could not be authenticated"
            )
        try:
            snapshot = self._suitability_service.load_authenticated_snapshot_by_ids(
                metadata.profile_version_id,
                metadata.suitability_assessment_id,
            )
        except (SuitabilityPolicyError, SuitabilityAssessmentError):
            raise AllocationCalculationError(
                "historical suitability provenance could not be authenticated"
            ) from None
        if (
            snapshot.status is AssessmentStatus.BLOCKED
            or snapshot.profile_version_id != metadata.profile_version_id
            or snapshot.assessment_id != metadata.suitability_assessment_id
            or snapshot.policy_version != "1"
        ):
            raise AllocationCalculationError(
                "historical suitability provenance could not be authenticated"
            )
        if not (
            snapshot.profile.confirmed_at <= metadata.assessed_at < snapshot.profile_valid_until
            and snapshot.assessed_at <= metadata.assessed_at < snapshot.valid_until
        ):
            raise AllocationCalculationError(
                "historical allocation timing could not be authenticated"
            )
        expected_fingerprint = self._input_fingerprint(
            snapshot,
            policy_checksum,
            metadata.assessed_at,
        )
        if not hmac.compare_digest(metadata.input_fingerprint, expected_fingerprint):
            raise AllocationCalculationError(
                "historical allocation input fingerprint could not be authenticated"
            )
        expected_valid_until = min(
            metadata.assessed_at + timedelta(hours=24),
            snapshot.profile_valid_until.astimezone(timezone.utc),
            snapshot.valid_until.astimezone(timezone.utc),
        )
        if metadata.valid_until != expected_valid_until:
            raise AllocationCalculationError(
                "historical allocation validity could not be authenticated"
            )
        expected = evaluate_allocation(
            snapshot.profile,
            snapshot.result,
            self._policy,
            metadata.assessed_at,
        )
        if expected.status is not AllocationStatus.RANGE_AVAILABLE or expected != authenticated:
            raise AllocationCalculationError(
                "historical allocation did not match deterministic recalculation"
            )
        return authenticated, snapshot

    def _input_fingerprint(
        self,
        snapshot: AuthenticatedSuitabilitySnapshot,
        policy_checksum: str,
        assessed_at: datetime,
    ) -> str:
        payload = json.dumps(
            {
                "allocation_policy_checksum": policy_checksum,
                "assessment_instant": assessed_at.astimezone(timezone.utc).isoformat(),
                "profile_keyed_fingerprint": snapshot.profile_keyed_fingerprint,
                "profile_version_id": snapshot.profile_version_id,
                "suitability_assessment_id": snapshot.assessment_id,
                "suitability_input_fingerprint": snapshot.input_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return self._cipher.fingerprint(payload)

    def _ensure_policy(self) -> str:
        return self._ensure_policy_record().policy_checksum

    def _ensure_policy_record(self):
        try:
            record = self._policy_store.ensure(self._policy)
            if record.policy_checksum != self._policy.checksum():
                raise ValueError("allocation policy checksum does not match")
            return record
        except Exception:
            raise AllocationPolicyError("allocation policy is unavailable") from None

    def _current_time(self) -> datetime:
        current = self._now()
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AllocationCalculationError("current time is unavailable")
        return current.astimezone(timezone.utc)

    def _stale_history_or_missing(self) -> Dict[str, object]:
        history = self._assessment_store.history(limit=1)
        if history:
            stored = self._assessment_store.get(history[0].id)
            if stored is None:
                raise ValueError("stored allocation assessment is unavailable")
            self._authenticate_historical(stored)
            return _metadata_status(stored.metadata, "stale")
        return {
            "state": "missing",
            "freshness": "missing",
            "capability": "research_only",
        }

    @staticmethod
    def _execution(
        result: AllocationResult,
        metadata: Optional[AllocationAssessmentMetadata],
        snapshot: AuthenticatedSuitabilitySnapshot,
        assessed_at: datetime,
        valid_until: Optional[datetime],
        freshness: str,
    ) -> AllocationExecution:
        return AllocationExecution(
            result=result,
            assessment_id=None if metadata is None else metadata.id,
            profile_version_id=snapshot.profile_version_id,
            profile_version=snapshot.profile_version,
            suitability_assessment_id=snapshot.assessment_id,
            policy_version="1" if metadata is None else metadata.policy_version,
            assessed_at=assessed_at,
            valid_until=valid_until,
            freshness=freshness,
        )


def _suitability_blocked_result(
    snapshot: AuthenticatedSuitabilitySnapshot,
) -> AllocationResult:
    result = AllocationResult(
        status=AllocationStatus.BLOCKED,
        capability="research_only",
        blocks=(AllocationBlockCode.SUITABILITY_BLOCKED,),
        binding_constraints=tuple(
            item
            for item in AllocationConstraintCode
            if item.value in {constraint.value for constraint in snapshot.result.constraints}
        ),
        profile_conflicts=(),
        safe_summary=AllocationSafeSummary(
            goal_count=snapshot.result.goal_count,
            obligation_count=snapshot.result.obligation_count,
            fully_funded_now_count=0,
            fundable_without_return_count=0,
            funding_gap_without_return_count=0,
            horizon_equity_ceilings=(),
        ),
        permitted_region=None,
        exact=None,
    )
    result.validate()
    return result


def _same_snapshot_binding(
    expected: AuthenticatedSuitabilitySnapshot,
    current: AuthenticatedSuitabilitySnapshot,
) -> bool:
    return (
        current.profile_version_id == expected.profile_version_id
        and current.assessment_id == expected.assessment_id
        and current.policy_version == expected.policy_version
        and hmac.compare_digest(
            current.profile_keyed_fingerprint,
            expected.profile_keyed_fingerprint,
        )
        and hmac.compare_digest(
            current.input_fingerprint,
            expected.input_fingerprint,
        )
    )


def _metadata_status(
    metadata: AllocationAssessmentMetadata,
    freshness: str,
) -> Dict[str, object]:
    view = _history_item(metadata)
    view.update({"state": freshness, "freshness": freshness})
    return view


def _history_item(metadata: AllocationAssessmentMetadata) -> Dict[str, object]:
    return {
        "assessment_id": metadata.id,
        "profile_version_id": metadata.profile_version_id,
        "suitability_assessment_id": metadata.suitability_assessment_id,
        "policy_version": metadata.policy_version,
        "status": metadata.status.value,
        "binding_constraints": [item.value for item in metadata.binding_constraints],
        "safe_summary": _safe_summary_view(metadata.safe_summary),
        "permitted_region": _permitted_region_view(metadata.permitted_region),
        "assessed_at": metadata.assessed_at.isoformat(),
        "valid_until": metadata.valid_until.isoformat(),
        "capability": "research_only",
    }


def _safe_summary_view(summary: AllocationSafeSummary) -> Dict[str, object]:
    return {
        "goal_count": summary.goal_count,
        "obligation_count": summary.obligation_count,
        "fully_funded_now_count": summary.fully_funded_now_count,
        "fundable_without_return_count": summary.fundable_without_return_count,
        "funding_gap_without_return_count": summary.funding_gap_without_return_count,
        "horizon_equity_ceilings": [
            _decimal_text(value) for value in summary.horizon_equity_ceilings
        ],
    }


def _permitted_region_view(region) -> Optional[Dict[str, object]]:
    if region is None:
        return None
    return {
        "inequalities": list(region.inequalities),
        "maximum_equity": _decimal_text(region.maximum_equity),
        "horizon_equity_ceiling": _decimal_text(region.horizon_equity_ceiling),
        "loss_amount_equity_ceiling": _decimal_text(region.loss_amount_equity_ceiling),
        "drawdown_equity_ceiling": _decimal_text(region.drawdown_equity_ceiling),
        "willingness_equity_ceiling": _decimal_text(region.willingness_equity_ceiling),
        "stability_equity_ceiling": _decimal_text(region.stability_equity_ceiling),
    }


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")
