from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Callable, Optional, Tuple

from kunjin.decision.budget import RequestBudget
from kunjin.decision.health import SourceStatusSnapshot
from kunjin.decision.models import (
    ActionKind,
    ForceAuthorization,
    FreshnessContext,
    RequestMode,
    RiskEffect,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldRef,
    SourceFieldState,
    canonical_json_bytes,
    validate_aware_datetime,
    validate_checksum,
    validate_exact_dataclass_state,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.worker import run_fund_nav_worker
from kunjin.decision.worker_protocol import (
    SCHEMA_VERSION,
    FundNavPayload,
    FundNavWorkerRequest,
    FundNavWorkerResponse,
    encode_fund_nav_error,
    encode_fund_nav_success,
)
from kunjin.funds.service import SourceRequestContext
from kunjin.models import FundNavObservation
from kunjin.storage.repository import Repository

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_SOURCE_ID = "eastmoney_nav"
_FORMAL_FIELD = "formal_nav"
_ADJUSTED_FIELD = "adjusted_return_series"
_FIELDS = (_FORMAL_FIELD, _ADJUSTED_FIELD)
_MIN_ADJUSTED_SAMPLES = 60
_MAX_ADJUSTED_SAMPLES = 1024
_VALIDATED_SERIES_MAC_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class NavSyncResult:
    fund_code: str
    status: str
    formal_nav_status: str
    adjusted_series_status: str
    records: int
    latest_nav_date: Optional[str]
    omitted_work: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidatedAdjustedNavSeries:
    fund_code: str
    observations: Tuple[FundNavObservation, ...]
    source_attempt_id: int
    retrieved_at: datetime
    data_as_of: date
    binding_mac: str

    def binding_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "data_as_of": self.data_as_of,
                "fund_code": self.fund_code,
                "observations": tuple(
                    {
                        "accumulated_nav": item.accumulated_nav,
                        "corporate_action_state": item.corporate_action_state,
                        "daily_growth": item.daily_growth,
                        "fund_code": item.fund_code,
                        "nav_date": item.nav_date,
                        "retrieved_at": item.retrieved_at,
                        "source": item.source,
                        "source_attempt_id": item.source_attempt_id,
                        "unit_nav": item.unit_nav,
                    }
                    for item in self.observations
                ),
                "retrieved_at": self.retrieved_at,
                "source_attempt_id": self.source_attempt_id,
            }
        )

    def validation_issue(self) -> Optional[str]:
        try:
            validate_checksum(self.binding_mac, "validated adjusted NAV series binding MAC")
            authenticated = hmac.compare_digest(
                self.binding_mac,
                _validated_series_mac(self),
            )
        except (AttributeError, TypeError, ValueError):
            authenticated = False
        if not authenticated:
            return "source_binding_invalid"
        if (
            type(self.observations) is not tuple
            or not _MIN_ADJUSTED_SAMPLES <= len(self.observations) <= _MAX_ADJUSTED_SAMPLES
        ):
            return "samples_insufficient"
        if type(self.source_attempt_id) is not int or self.source_attempt_id <= 0:
            return "source_binding_invalid"
        if (
            type(self.retrieved_at) is not datetime
            or self.retrieved_at.tzinfo is not timezone.utc
            or type(self.data_as_of) is not date
        ):
            return "source_binding_invalid"

        dates = []
        for observation in self.observations:
            if type(observation) is not FundNavObservation:
                return "observation_invalid"
            try:
                observation.validate()
            except (TypeError, ValueError):
                return "observation_invalid"
            if observation.fund_code != self.fund_code:
                return "subject_mismatch"
            if (
                observation.source != "eastmoney"
                or observation.source_attempt_id != self.source_attempt_id
                or observation.retrieved_at != self.retrieved_at
                or observation.retrieved_at.tzinfo is not timezone.utc
            ):
                return "source_binding_invalid"
            if (
                type(observation.nav_date) is not date
                or type(observation.unit_nav) is not Decimal
                or not observation.unit_nav.is_finite()
                or observation.unit_nav <= 0
            ):
                return "observation_invalid"
            dates.append(observation.nav_date)

        if len(dates) != len(set(dates)):
            return "duplicate_date"
        if dates != sorted(dates):
            return "date_order_invalid"
        if self.data_as_of != dates[-1]:
            return "source_binding_invalid"
        if any(
            type(item.accumulated_nav) is not Decimal
            or not item.accumulated_nav.is_finite()
            or item.accumulated_nav <= 0
            for item in self.observations
        ):
            return "accumulated_nav_unavailable"
        if any(item.corporate_action_state != "none" for item in self.observations):
            return "corporate_action_unresolved"

        first = self.observations[0]
        if first.accumulated_nav is None:
            return "accumulated_nav_unavailable"
        distribution_spread = first.accumulated_nav - first.unit_nav
        if any(
            item.accumulated_nav is None
            or item.accumulated_nav - item.unit_nav != distribution_spread
            for item in self.observations
        ):
            return "discontinuity"
        for older, newer in zip(self.observations, self.observations[1:]):
            growth = newer.daily_growth
            if type(growth) is not Decimal or not growth.is_finite():
                return "discontinuity"
            unit_change = newer.unit_nav - older.unit_nav
            if (growth > 0 and unit_change < 0) or (growth < 0 and unit_change > 0):
                return "discontinuity"
        return None

    def validate(self) -> None:
        if type(self) is not ValidatedAdjustedNavSeries:
            raise ValueError("validated adjusted NAV series subclasses are not accepted")
        validate_exact_dataclass_state(self, "validated adjusted NAV series")
        if type(self.fund_code) is not str or _FUND_CODE.fullmatch(self.fund_code) is None:
            raise ValueError("validated adjusted NAV series fund code is invalid")
        issue = self.validation_issue()
        if issue is not None:
            raise ValueError(f"validated adjusted NAV series is invalid: {issue}")


def _validated_series_mac(value: ValidatedAdjustedNavSeries) -> str:
    return hmac.new(
        _VALIDATED_SERIES_MAC_KEY,
        value.binding_bytes(),
        hashlib.sha256,
    ).hexdigest()


def _seal_validated_adjusted_nav_series(
    *,
    fund_code: str,
    observations: Tuple[FundNavObservation, ...],
    source_attempt_id: int,
    retrieved_at: datetime,
    data_as_of: date,
) -> ValidatedAdjustedNavSeries:
    result = ValidatedAdjustedNavSeries(
        fund_code=fund_code,
        observations=observations,
        source_attempt_id=source_attempt_id,
        retrieved_at=retrieved_at,
        data_as_of=data_as_of,
        binding_mac="0" * 64,
    )
    return replace(result, binding_mac=_validated_series_mac(result))


class BoundedNavService:
    def __init__(
        self,
        repository: Repository,
        *,
        worker_runner: Callable[
            [FundNavWorkerRequest, RequestBudget], FundNavWorkerResponse
        ] = run_fund_nav_worker,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if not callable(worker_runner):
            raise ValueError("NAV worker runner must be callable")
        self.repository = repository
        self.worker_runner = worker_runner

    def sync(
        self,
        fund_code: str,
        context: SourceRequestContext,
        *,
        latest_expected_data_as_of: Optional[datetime] = None,
    ) -> NavSyncResult:
        if type(fund_code) is not str or _FUND_CODE.fullmatch(fund_code) is None:
            raise ValueError("fund code must be exactly six ASCII digits")
        if type(context) is not SourceRequestContext:
            raise ValueError("context must be an exact SourceRequestContext")
        if context.audit_store.repository.database.resolve() != self.repository.database.resolve():
            raise ValueError("NAV and audit stores must share one database")
        latest_expected_data_as_of = self._normalized_expected_date(latest_expected_data_as_of)
        context.budget.require_publishable()
        subject_key = f"fund:{fund_code}"
        snapshot = self._source_snapshot(
            subject_key,
            context,
            latest_expected_data_as_of,
        )
        states = self._group_states(snapshot)
        authorizations = self._force_authorizations(subject_key, context)
        if context.force_reason is None and SourceFieldState.COOLDOWN in states.values():
            return self._record_group_cooldown(
                fund_code,
                subject_key,
                context,
                snapshot,
                latest_expected_data_as_of,
            )

        cached = self._latest_cache_batch(
            tuple(
                item
                for item in self.repository.fund_history(fund_code)
                if item.source == "eastmoney"
            ),
            context,
            subject_key,
        )
        cached_formal_status = (
            None
            if not cached
            else self._formal_status(
                cached[-1].nav_date,
                latest_expected_data_as_of,
            )
        )
        if context.force_reason is None and (
            cached
            and (
                states[_FORMAL_FIELD] is SourceFieldState.HEALTHY
                or (
                    latest_expected_data_as_of is None
                    and states[_FORMAL_FIELD] is SourceFieldState.DEGRADED
                )
            )
            and (cached_formal_status == "success" or latest_expected_data_as_of is None)
        ):
            return self._record_cache_hit(
                fund_code,
                subject_key,
                context,
                cached,
                latest_expected_data_as_of,
            )

        max_pages = "6" if context.budget.mode is RequestMode.RAPID else "50"
        request = FundNavWorkerRequest(
            schema_version=SCHEMA_VERSION,
            request_id=context.budget.request_id,
            source_id=_SOURCE_ID,
            field_id=_FORMAL_FIELD,
            subject_key=subject_key,
            operation="fund_nav_fetch",
            arguments={"fund_code": fund_code, "max_pages": max_pages},
        )
        response = self.worker_runner(request, context.budget)
        context.budget.require_publishable()
        finished_at = self._trusted_response_time(context)
        response_bytes = self._validate_worker_response(request, response, context, finished_at)
        if not response.ok:
            return self._record_worker_failure(
                fund_code,
                subject_key,
                context,
                response,
                authorizations,
                response_bytes,
                finished_at,
            )
        payload = response.payload
        if payload is None:
            raise ValueError("NAV worker success payload is missing")
        observations = self._observations(payload)
        return self._persist_success(
            fund_code,
            subject_key,
            context,
            payload,
            observations,
            authorizations,
            response_bytes,
            latest_expected_data_as_of,
            finished_at,
        )

    @staticmethod
    def _validate_worker_response(
        request: FundNavWorkerRequest,
        response: FundNavWorkerResponse,
        context: SourceRequestContext,
        finished_at: datetime,
    ) -> int:
        if type(response) is not FundNavWorkerResponse:
            raise ValueError("NAV worker returned an invalid response type")
        expected_identity = (
            request.schema_version,
            request.request_id,
            request.source_id,
            request.field_id,
            request.subject_key,
            request.operation,
        )
        actual_identity = (
            response.schema_version,
            response.request_id,
            response.source_id,
            response.field_id,
            response.subject_key,
            response.operation,
        )
        if actual_identity != expected_identity:
            raise ValueError("NAV worker response identity does not match request")
        if response.ok:
            if (
                response.payload is None
                or response.reason_code is not None
                or response.retryable is not None
                or response.message is not None
            ):
                raise ValueError("NAV worker success response shape is invalid")
            if not (
                context.budget.started_at
                <= response.payload.retrieved_at
                <= finished_at
                <= context.budget.deadline_at
            ):
                raise ValueError("NAV worker retrieval time is outside request lifetime")
            if any(
                datetime.strptime(row.nav_date, "%Y-%m-%d").date() > finished_at.date()
                for row in response.payload.rows
            ):
                raise ValueError("NAV date is later than the trusted parent date")
            return len(encode_fund_nav_success(request, response.payload))
        if (
            response.payload is not None
            or response.reason_code is None
            or response.retryable is None
            or response.message is None
        ):
            raise ValueError("NAV worker error response shape is invalid")
        return len(
            encode_fund_nav_error(
                request,
                reason_code=response.reason_code,
                retryable=response.retryable,
                message=response.message,
            )
        )

    @staticmethod
    def _trusted_response_time(context: SourceRequestContext) -> datetime:
        context.budget.require_publishable()
        try:
            finished_at = validate_aware_datetime(
                context.health_service.wall_clock(),
                "NAV response finish",
            ).astimezone(timezone.utc)
        except Exception:
            raise ValueError("NAV response wall clock failed") from None
        if not context.budget.started_at <= finished_at <= context.budget.deadline_at:
            raise ValueError("NAV response finish is outside request lifetime")
        return finished_at

    @staticmethod
    def _normalized_expected_date(
        latest_expected_data_as_of: Optional[datetime],
    ) -> Optional[datetime]:
        if latest_expected_data_as_of is None:
            return None
        validated = validate_aware_datetime(
            latest_expected_data_as_of,
            "latest expected NAV date",
        )
        return datetime.combine(validated.date(), time.min, tzinfo=timezone.utc)

    @staticmethod
    def _source_snapshot(
        subject_key: str,
        context: SourceRequestContext,
        latest_expected_data_as_of: Optional[datetime],
    ) -> SourceStatusSnapshot:
        refs = tuple(SourceFieldRef(_SOURCE_ID, field) for field in _FIELDS)
        requirements = tuple(
            context.health_service.action_requirement(
                field,
                ActionKind.FACT_RESEARCH,
                RiskEffect.INFORMATION,
            )
            for field in _FIELDS
        )
        return context.health_service.source_status_snapshot(
            subject_key,
            FreshnessContext(
                now=context.budget.started_at,
                latest_expected_data_as_of=latest_expected_data_as_of,
            ),
            refs,
            requirements,
            request_run_id=context.request_run_id,
            budget=context.budget,
        )

    @staticmethod
    def _group_states(snapshot: SourceStatusSnapshot) -> dict[str, SourceFieldState]:
        states = {
            projection.history.reference.field_id: projection.state
            for projection in snapshot.projections
            if projection.history.reference.source_id == _SOURCE_ID
            and projection.history.reference.field_id in _FIELDS
        }
        if set(states) != set(_FIELDS):
            raise ValueError("NAV source health snapshot is incomplete")
        return states

    @staticmethod
    def _force_authorizations(
        subject_key: str,
        context: SourceRequestContext,
    ) -> dict[str, Optional[ForceAuthorization]]:
        if context.force_reason is None:
            return {field: None for field in _FIELDS}
        references = tuple(SourceFieldRef(_SOURCE_ID, field) for field in _FIELDS)
        authorizations = context.health_service.force_authorizations(
            context.budget,
            references,
            subject_key,
            context.force_reason,
            request_run_id=context.request_run_id,
            attempt_number=1,
        )
        if authorizations is None or len(authorizations) != len(_FIELDS):
            raise ValueError("NAV force authorization is unavailable for the source group")
        return dict(zip(_FIELDS, authorizations))

    @staticmethod
    def _observations(payload: FundNavPayload) -> Tuple[FundNavObservation, ...]:
        observations = tuple(
            FundNavObservation(
                fund_code=payload.fund_code,
                nav_date=datetime.strptime(row.nav_date, "%Y-%m-%d").date(),
                unit_nav=Decimal(row.unit_nav),
                accumulated_nav=(
                    None if row.accumulated_nav is None else Decimal(row.accumulated_nav)
                ),
                daily_growth=(None if row.daily_growth is None else Decimal(row.daily_growth)),
                source="eastmoney",
                retrieved_at=payload.retrieved_at,
                corporate_action_state=row.corporate_action_state,
            )
            for row in payload.rows
        )
        for item in observations:
            item.validate()
        return observations

    def _latest_cache_batch(
        self,
        observations: Tuple[FundNavObservation, ...],
        context: SourceRequestContext,
        subject_key: str,
    ) -> Tuple[FundNavObservation, ...]:
        candidates = sorted(
            {
                (item.retrieved_at, item.source_attempt_id)
                for item in observations
                if item.source_attempt_id is not None
            },
            reverse=True,
        )
        for retrieved_at, attempt_id in candidates:
            if attempt_id is None:
                continue
            retrieval_batch = tuple(
                item for item in observations if item.retrieved_at == retrieved_at
            )
            if not retrieval_batch or any(
                item.source_attempt_id != attempt_id for item in retrieval_batch
            ):
                continue
            stored = context.audit_store.authenticated_source_attempt(attempt_id)
            batch = retrieval_batch
            latest = max(batch, key=lambda item: item.nav_date)
            expected_data_as_of = datetime.combine(
                latest.nav_date,
                time.min,
                tzinfo=timezone.utc,
            )
            if (
                stored.attempt.source_id == _SOURCE_ID
                and stored.attempt.field_id == _FORMAL_FIELD
                and stored.attempt.subject_key == subject_key
                and stored.attempt.outcome is SourceAttemptOutcome.SUCCESS
                and stored.attempt.data_as_of == expected_data_as_of
                and stored.attempt.started_at <= retrieved_at <= stored.attempt.finished_at
            ):
                return tuple(sorted(batch, key=lambda item: item.nav_date))
        return ()

    def validated_adjusted_series(
        self,
        fund_code: str,
        context: SourceRequestContext,
        *,
        latest_expected_data_as_of: Optional[datetime] = None,
    ) -> Optional[ValidatedAdjustedNavSeries]:
        if type(fund_code) is not str or _FUND_CODE.fullmatch(fund_code) is None:
            raise ValueError("fund code must be exactly six ASCII digits")
        if type(context) is not SourceRequestContext:
            raise ValueError("context must be an exact SourceRequestContext")
        if context.audit_store.repository.database.resolve() != self.repository.database.resolve():
            raise ValueError("NAV and audit stores must share one database")
        latest_expected_data_as_of = self._normalized_expected_date(latest_expected_data_as_of)
        context.budget.require_publishable()
        subject_key = f"fund:{fund_code}"
        cached = self._latest_cache_batch(
            tuple(
                item
                for item in self.repository.fund_history(fund_code)
                if item.source == "eastmoney"
            ),
            context,
            subject_key,
        )
        if not cached:
            return None
        latest = cached[-1]
        formal_status = self._formal_status(
            latest.nav_date,
            latest_expected_data_as_of,
        )
        if not self._adjusted_series_complete(cached, formal_status):
            return None
        attempt_ids = {item.source_attempt_id for item in cached}
        retrieval_times = {item.retrieved_at for item in cached}
        if len(attempt_ids) != 1 or None in attempt_ids or len(retrieval_times) != 1:
            return None
        source_attempt_id = next(iter(attempt_ids))
        retrieved_at = next(iter(retrieval_times))
        if type(source_attempt_id) is not int or type(retrieved_at) is not datetime:
            return None
        result = _seal_validated_adjusted_nav_series(
            fund_code=fund_code,
            observations=cached,
            source_attempt_id=source_attempt_id,
            retrieved_at=retrieved_at,
            data_as_of=latest.nav_date,
        )
        result.validate()
        return result

    @staticmethod
    def _adjusted_series_complete(
        observations: Tuple[FundNavObservation, ...],
        formal_status: str,
    ) -> bool:
        if len(observations) < _MIN_ADJUSTED_SAMPLES or formal_status != "success":
            return False
        ordered = tuple(sorted(observations, key=lambda item: item.nav_date, reverse=True))
        if not all(
            item.accumulated_nav is not None
            and item.accumulated_nav.is_finite()
            and item.accumulated_nav > 0
            and item.corporate_action_state == "none"
            for item in ordered
        ):
            return False
        first_accumulated = ordered[0].accumulated_nav
        if first_accumulated is None:
            return False
        distribution_spread = first_accumulated - ordered[0].unit_nav
        if any(
            item.accumulated_nav is None
            or item.accumulated_nav - item.unit_nav != distribution_spread
            for item in ordered
        ):
            return False
        for newer, older in zip(ordered, ordered[1:]):
            growth = newer.daily_growth
            unit_change = newer.unit_nav - older.unit_nav
            if (
                growth is None
                or (growth > 0 and unit_change < 0)
                or (growth < 0 and unit_change > 0)
            ):
                return False
        return True

    @staticmethod
    def _formal_status(
        latest_nav_date: date,
        latest_expected_data_as_of: Optional[datetime],
    ) -> str:
        if latest_expected_data_as_of is None:
            return "unknown_current"
        expected_date = latest_expected_data_as_of.date()
        return "success" if latest_nav_date >= expected_date else "stale"

    def _persist_success(
        self,
        fund_code: str,
        subject_key: str,
        context: SourceRequestContext,
        payload: FundNavPayload,
        observations: Tuple[FundNavObservation, ...],
        authorizations: dict[str, Optional[ForceAuthorization]],
        response_bytes: int,
        latest_expected_data_as_of: Optional[datetime],
        finished_at: datetime,
    ) -> NavSyncResult:
        formal_status = self._formal_status(
            observations[0].nav_date,
            latest_expected_data_as_of,
        )
        adjusted_complete = self._adjusted_series_complete(
            observations,
            formal_status,
        )
        data_as_of = datetime.combine(
            observations[0].nav_date,
            time.min,
            tzinfo=timezone.utc,
        )
        formal_attempt = self._attempt(
            _FORMAL_FIELD,
            subject_key,
            SourceAttemptOutcome.SUCCESS,
            context,
            finished_at=finished_at,
            data_as_of=data_as_of,
            response_bytes=response_bytes,
            authorization=authorizations[_FORMAL_FIELD],
        )
        adjusted_attempt = self._attempt(
            _ADJUSTED_FIELD,
            subject_key,
            (
                SourceAttemptOutcome.SUCCESS
                if adjusted_complete
                else SourceAttemptOutcome.UNAVAILABLE
            ),
            context,
            finished_at=finished_at,
            data_as_of=data_as_of if adjusted_complete else None,
            error_code=(None if adjusted_complete else SourceErrorCode.VALIDATION_FAILURE),
            response_bytes=response_bytes,
            authorization=authorizations[_ADJUSTED_FIELD],
        )
        context.budget.require_publishable()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                context.budget.require_publishable()
                formal_attempt_id = context.audit_store.record_source_attempt(
                    context.request_run_id,
                    formal_attempt,
                    authorizations[_FORMAL_FIELD],
                    connection=connection,
                )
                context.audit_store.record_source_attempt(
                    context.request_run_id,
                    adjusted_attempt,
                    authorizations[_ADJUSTED_FIELD],
                    connection=connection,
                )
                self.repository.save_authenticated_fund_history(
                    fund_code,
                    payload.fund_name,
                    payload.fund_type,
                    "eastmoney",
                    observations,
                    source_attempt_id=formal_attempt_id,
                    connection=connection,
                )
                context.budget.require_publishable()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return NavSyncResult(
            fund_code=fund_code,
            status=("success" if adjusted_complete and formal_status == "success" else "partial"),
            formal_nav_status=formal_status,
            adjusted_series_status="success" if adjusted_complete else "insufficient",
            records=len(observations),
            latest_nav_date=observations[0].nav_date.isoformat(),
            omitted_work=() if adjusted_complete else (_ADJUSTED_FIELD,),
        )

    def _record_worker_failure(
        self,
        fund_code: str,
        subject_key: str,
        context: SourceRequestContext,
        response: FundNavWorkerResponse,
        authorizations: dict[str, Optional[ForceAuthorization]],
        response_bytes: int,
        finished_at: datetime,
    ) -> NavSyncResult:
        if response.reason_code is None:
            raise ValueError("NAV worker failure has no reason code")
        error_code = SourceErrorCode(response.reason_code)
        outcome = (
            SourceAttemptOutcome.TRANSIENT_FAILURE
            if response.retryable
            else SourceAttemptOutcome.UNSUPPORTED
            if error_code
            in {
                SourceErrorCode.FIELD_UNSUPPORTED,
                SourceErrorCode.SOURCE_CONTRACT_UNSUPPORTED,
                SourceErrorCode.HTTP_NOT_FOUND,
                SourceErrorCode.HTTP_GONE,
            }
            else SourceAttemptOutcome.UNAVAILABLE
        )
        attempts = tuple(
            self._attempt(
                field,
                subject_key,
                outcome,
                context,
                finished_at=finished_at,
                error_code=error_code,
                cooldown_until=(
                    context.health_service.cooldown_until(finished_at)
                    if outcome is SourceAttemptOutcome.TRANSIENT_FAILURE
                    else None
                ),
                response_bytes=response_bytes,
                authorization=authorizations[field],
            )
            for field in _FIELDS
        )
        self._commit_attempts(context, attempts, authorizations)
        return NavSyncResult(
            fund_code,
            "unavailable",
            "unavailable",
            "unavailable",
            0,
            None,
            _FIELDS,
        )

    def _record_group_cooldown(
        self,
        fund_code: str,
        subject_key: str,
        context: SourceRequestContext,
        snapshot: SourceStatusSnapshot,
        latest_expected_data_as_of: Optional[datetime],
    ) -> NavSyncResult:
        cooldowns = tuple(
            record.attempt.cooldown_until
            for projection in snapshot.projections
            if projection.history.reference.source_id == _SOURCE_ID
            for record in projection.history.attempts
            if record.attempt.cooldown_until is not None
            and record.attempt.cooldown_until > snapshot.evaluated_at
        )
        if not cooldowns:
            raise ValueError("NAV cooldown state has no authenticated deadline")
        cooldown_until = max(cooldowns)
        attempts = tuple(
            self._attempt(
                field,
                subject_key,
                SourceAttemptOutcome.SKIPPED_COOLDOWN,
                context,
                finished_at=snapshot.evaluated_at,
                error_code=SourceErrorCode.COOLDOWN_ACTIVE,
                cooldown_until=cooldown_until,
            )
            for field in _FIELDS
        )
        self._commit_attempts(context, attempts, {field: None for field in _FIELDS})
        cached = self._latest_cache_batch(
            tuple(
                item
                for item in self.repository.fund_history(fund_code)
                if item.source == "eastmoney"
            ),
            context,
            subject_key,
        )
        latest = None if not cached else cached[-1]
        formal_status = (
            "unavailable"
            if latest is None
            else self._formal_status(
                latest.nav_date,
                latest_expected_data_as_of,
            )
        )
        adjusted_complete = (
            False if latest is None else self._adjusted_series_complete(cached, formal_status)
        )
        return NavSyncResult(
            fund_code,
            "skipped_cooldown",
            formal_status,
            "cache" if adjusted_complete else "insufficient",
            len(cached),
            None if latest is None else latest.nav_date.isoformat(),
            _FIELDS,
        )

    def _record_cache_hit(
        self,
        fund_code: str,
        subject_key: str,
        context: SourceRequestContext,
        observations: Tuple[FundNavObservation, ...],
        latest_expected_data_as_of: Optional[datetime],
    ) -> NavSyncResult:
        latest = observations[-1]
        data_as_of = datetime.combine(latest.nav_date, time.min, tzinfo=timezone.utc)
        formal_status = self._formal_status(
            latest.nav_date,
            latest_expected_data_as_of,
        )
        adjusted_complete = self._adjusted_series_complete(
            observations,
            formal_status,
        )
        attempts = (
            self._attempt(
                _FORMAL_FIELD,
                subject_key,
                SourceAttemptOutcome.CACHE_HIT,
                context,
                finished_at=context.budget.started_at,
                data_as_of=data_as_of,
            ),
            self._attempt(
                _ADJUSTED_FIELD,
                subject_key,
                (
                    SourceAttemptOutcome.CACHE_HIT
                    if adjusted_complete
                    else SourceAttemptOutcome.UNAVAILABLE
                ),
                context,
                finished_at=context.budget.started_at,
                data_as_of=data_as_of if adjusted_complete else None,
                error_code=(None if adjusted_complete else SourceErrorCode.VALIDATION_FAILURE),
            ),
        )
        self._commit_attempts(context, attempts, {field: None for field in _FIELDS})
        return NavSyncResult(
            fund_code,
            "cache_hit",
            formal_status,
            "cache" if adjusted_complete else "insufficient",
            len(observations),
            latest.nav_date.isoformat(),
            () if adjusted_complete else (_ADJUSTED_FIELD,),
        )

    @staticmethod
    def _attempt(
        field: str,
        subject_key: str,
        outcome: SourceAttemptOutcome,
        context: SourceRequestContext,
        *,
        finished_at: datetime,
        data_as_of: Optional[datetime] = None,
        error_code: Optional[SourceErrorCode] = None,
        cooldown_until: Optional[datetime] = None,
        response_bytes: int = 0,
        authorization: Optional[ForceAuthorization] = None,
    ) -> SourceAttempt:
        registry = SourceRegistryV1()
        attempt = SourceAttempt(
            source_id=_SOURCE_ID,
            field_id=field,
            subject_key=subject_key,
            attempt_number=1,
            outcome=outcome,
            started_at=(
                context.budget.started_at
                if authorization is None
                else authorization.reservation.reserved_at
            ),
            finished_at=finished_at,
            data_as_of=data_as_of,
            error_code=error_code,
            cooldown_until=cooldown_until,
            force_actor=None if authorization is None else authorization.actor,
            force_reason=None if authorization is None else authorization.reason,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=response_bytes,
        )
        attempt.validate()
        return attempt

    def _commit_attempts(
        self,
        context: SourceRequestContext,
        attempts: Tuple[SourceAttempt, ...],
        authorizations: dict[str, Optional[ForceAuthorization]],
    ) -> None:
        context.budget.require_publishable()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                context.budget.require_publishable()
                for attempt in attempts:
                    context.audit_store.record_source_attempt(
                        context.request_run_id,
                        attempt,
                        authorizations[attempt.field_id],
                        connection=connection,
                    )
                context.budget.require_publishable()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
