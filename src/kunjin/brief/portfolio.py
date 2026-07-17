from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Optional

from kunjin.brief.portfolio_worker_protocol import (
    MAX_PORTFOLIO_RESPONSE_BYTES,
    SCHEMA_VERSION,
    PortfolioWorkerRequest,
    PortfolioWorkerResponse,
    decode_portfolio_response,
    encode_portfolio_error,
    encode_portfolio_request,
    encode_portfolio_success,
)
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import (
    FreshnessContext,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldHistory,
    SourceFieldState,
    validate_aware_datetime,
    validate_exact_dataclass_state,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.worker import (
    PRIVATE_KEYCHAIN_WORKER_ENV,
    _run_framed_worker,
)
from kunjin.funds.service import SourceRequestContext
from kunjin.models import AccountObservation, PositionObservation
from kunjin.services.sync import PortfolioSyncService
from kunjin.storage.repository import Repository

_SOURCE_ID = "yangjibao_portfolio_observation"
_FIELD_ID = "personal_position_observation"


@dataclass(frozen=True)
class PortfolioObservationResult:
    fund_code: str
    status: str
    accounts: int
    positions: int
    position_present: Optional[bool]
    observed_at: Optional[str]
    error_code: Optional[str] = None


def _transport_validator(result, request, budget: RequestBudget) -> None:
    if type(result) is not PortfolioWorkerResponse:
        raise ValueError("portfolio worker result must use the exact type")
    if result.request_id != request.request_id or result.operation != request.operation:
        raise ValueError("portfolio worker response identity mismatch")
    if result.ok:
        payload = result.payload
        if payload is None:
            raise ValueError("portfolio worker success payload is missing")
        if not budget.started_at <= payload.retrieved_at <= budget.deadline_at:
            raise ValueError("portfolio worker retrieval time is outside request lifetime")


def run_portfolio_worker(
    request: PortfolioWorkerRequest,
    budget: RequestBudget,
) -> PortfolioWorkerResponse:
    if type(request) is not PortfolioWorkerRequest:
        raise ValueError("portfolio request must use the exact protocol type")
    if type(budget) is not RequestBudget or request.request_id != budget.request_id:
        raise ValueError("portfolio worker budget binding is invalid")
    return _run_framed_worker(
        request,
        budget,
        encoder=encode_portfolio_request,
        decoder=decode_portfolio_response,
        validator=_transport_validator,
        module="kunjin.brief.portfolio_worker_main",
        max_response_bytes=MAX_PORTFOLIO_RESPONSE_BYTES,
        environment_profile=PRIVATE_KEYCHAIN_WORKER_ENV,
    )


class BoundedPortfolioService:
    def __init__(
        self,
        repository: Repository,
        *,
        worker_runner: Callable[
            [PortfolioWorkerRequest, RequestBudget], PortfolioWorkerResponse
        ] = run_portfolio_worker,
        sync_service: Optional[PortfolioSyncService] = None,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if not callable(worker_runner):
            raise ValueError("portfolio worker runner must be callable")
        self.repository = repository
        self.worker_runner = worker_runner
        self.sync_service = sync_service or PortfolioSyncService(None, repository)

    def sync(
        self,
        fund_code: str,
        context: SourceRequestContext,
    ) -> PortfolioObservationResult:
        if (
            type(fund_code) is not str
            or len(fund_code) != 6
            or not fund_code.isascii()
            or not fund_code.isdigit()
        ):
            raise ValueError("fund code must be exactly six ASCII digits")
        if type(context) is not SourceRequestContext:
            raise ValueError("context must be an exact SourceRequestContext")
        if context.audit_store.repository.database.resolve() != self.repository.database.resolve():
            raise ValueError("portfolio and audit stores must share one database")
        context.budget.require_publishable()
        subject_key = f"fund:{fund_code}"
        state, history = context.health_service.source_field_state_and_history(
            _SOURCE_ID,
            _FIELD_ID,
            subject_key,
            FreshnessContext(now=context.budget.started_at),
            request_run_id=context.request_run_id,
            budget=context.budget,
        )
        if state is SourceFieldState.COOLDOWN:
            return self._record_cooldown(
                fund_code,
                subject_key,
                context,
                history,
            )
        request = PortfolioWorkerRequest(
            SCHEMA_VERSION,
            context.budget.request_id,
            "portfolio_observation",
        )
        response = self.worker_runner(request, context.budget)
        context.budget.require_publishable()
        finished_at = self._trusted_finish(context)
        response_bytes = self._validate_response(request, response, context, finished_at)
        if not response.ok:
            return self._record_failure(
                fund_code,
                subject_key,
                response,
                response_bytes,
                finished_at,
                context,
            )
        payload = response.payload
        if payload is None:
            raise ValueError("portfolio worker success payload is missing")
        accounts = tuple(
            AccountObservation(
                "yangjibao",
                item.source_account_id,
                item.title,
                item.observed_at,
            )
            for item in payload.accounts
        )
        positions = tuple(
            PositionObservation(
                item.source_account_id,
                item.fund_code,
                item.fund_name,
                Decimal(item.shares),
                item.observed_at,
                share_class=item.share_class,
                formal_nav=None if item.formal_nav is None else Decimal(item.formal_nav),
                estimated_nav=(None if item.estimated_nav is None else Decimal(item.estimated_nav)),
                observed_profit=(
                    None if item.observed_profit is None else Decimal(item.observed_profit)
                ),
            )
            for item in payload.positions
        )
        for account in accounts:
            account.validate()
        for position in positions:
            position.validate()
        attempt = self._attempt(
            subject_key,
            SourceAttemptOutcome.SUCCESS,
            context,
            finished_at,
            data_as_of=payload.retrieved_at,
            response_bytes=response_bytes,
        )
        context.budget.require_publishable()
        with self.repository.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                context.budget.require_publishable()
                context.audit_store.record_source_attempt(
                    context.request_run_id,
                    attempt,
                    connection=connection,
                )
                self.sync_service.commit_observations(
                    accounts,
                    positions,
                    payload.retrieved_at,
                    connection=connection,
                )
                context.budget.require_publishable()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PortfolioObservationResult(
            fund_code,
            "success",
            len(accounts),
            len(positions),
            any(item.fund_code == fund_code and item.shares > 0 for item in positions),
            payload.retrieved_at.isoformat(),
        )

    @staticmethod
    def _trusted_finish(context: SourceRequestContext) -> datetime:
        try:
            value = validate_aware_datetime(
                context.health_service.wall_clock(),
                "portfolio response finish",
            ).astimezone(timezone.utc)
        except Exception:
            raise ValueError("portfolio response wall clock failed") from None
        if not context.budget.started_at <= value <= context.budget.deadline_at:
            raise ValueError("portfolio response finish is outside request lifetime")
        return value

    @staticmethod
    def _validate_response(
        request: PortfolioWorkerRequest,
        response: PortfolioWorkerResponse,
        context: SourceRequestContext,
        finished_at: datetime,
    ) -> int:
        if type(response) is not PortfolioWorkerResponse:
            raise ValueError("portfolio worker returned an invalid response type")
        validate_exact_dataclass_state(response, "portfolio worker response")
        if (
            response.schema_version,
            response.request_id,
            response.operation,
        ) != (request.schema_version, request.request_id, request.operation):
            raise ValueError("portfolio worker response identity mismatch")
        if response.ok:
            if (
                type(response.ok) is not bool
                or response.payload is None
                or response.reason_code is not None
                or response.retryable is not None
                or response.message is not None
            ):
                raise ValueError("portfolio worker success response shape is invalid")
            frame = encode_portfolio_success(request, response.payload)
            if decode_portfolio_response(frame, request) != response:
                raise ValueError("portfolio worker success response is noncanonical")
            times = (
                response.payload.retrieved_at,
                *(item.observed_at for item in response.payload.accounts),
                *(item.observed_at for item in response.payload.positions),
            )
            if any(value < context.budget.started_at or value > finished_at for value in times):
                raise ValueError("portfolio observation is outside request lifetime")
            return len(frame)
        if (
            type(response.ok) is not bool
            or response.payload is not None
            or response.reason_code is None
            or response.retryable is None
            or response.message is None
        ):
            raise ValueError("portfolio worker error response shape is invalid")
        frame = encode_portfolio_error(
            request,
            response.reason_code,
            response.retryable,
        )
        if decode_portfolio_response(frame, request) != response:
            raise ValueError("portfolio worker error response is noncanonical")
        return len(frame)

    def _record_cooldown(
        self,
        fund_code: str,
        subject_key: str,
        context: SourceRequestContext,
        history: SourceFieldHistory,
    ) -> PortfolioObservationResult:
        cooldowns = tuple(
            record.attempt.cooldown_until
            for record in history.attempts
            if record.attempt.cooldown_until is not None
            and record.attempt.cooldown_until > context.budget.started_at
        )
        if not cooldowns:
            raise ValueError("portfolio cooldown state has no authenticated deadline")
        finished_at = self._trusted_finish(context)
        attempt = self._attempt(
            subject_key,
            SourceAttemptOutcome.SKIPPED_COOLDOWN,
            context,
            finished_at,
            error_code=SourceErrorCode.COOLDOWN_ACTIVE,
            cooldown_until=max(cooldowns),
            response_bytes=0,
        )
        context.budget.require_publishable()
        context.audit_store.record_source_attempt(context.request_run_id, attempt)
        return PortfolioObservationResult(
            fund_code,
            "skipped_cooldown",
            0,
            0,
            None,
            None,
            SourceErrorCode.COOLDOWN_ACTIVE.value,
        )

    def _record_failure(
        self,
        fund_code: str,
        subject_key: str,
        response: PortfolioWorkerResponse,
        response_bytes: int,
        finished_at: datetime,
        context: SourceRequestContext,
    ) -> PortfolioObservationResult:
        reason = response.reason_code
        if reason is None:
            raise ValueError("portfolio worker failure reason is missing")
        if reason == "authentication_required":
            # Audit schema V1 groups private authentication failures under this code.
            error_code = SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED
            public_reason = "authentication_required"
            outcome = SourceAttemptOutcome.UNAVAILABLE
            cooldown = None
        elif reason == "rate_limited":
            error_code = SourceErrorCode.TRANSIENT_NETWORK_FAILURE
            public_reason = reason
            outcome = SourceAttemptOutcome.TRANSIENT_FAILURE
            cooldown = context.health_service.cooldown_until(finished_at)
        else:
            error_code = (
                SourceErrorCode.VALIDATION_FAILURE
                if reason == "validation_failure"
                else SourceErrorCode.SOURCE_UNAVAILABLE
            )
            public_reason = reason
            outcome = SourceAttemptOutcome.UNAVAILABLE
            cooldown = None
        attempt = self._attempt(
            subject_key,
            outcome,
            context,
            finished_at,
            error_code=error_code,
            cooldown_until=cooldown,
            response_bytes=response_bytes,
        )
        context.budget.require_publishable()
        context.audit_store.record_source_attempt(context.request_run_id, attempt)
        return PortfolioObservationResult(
            fund_code,
            "unavailable",
            0,
            0,
            None,
            None,
            public_reason,
        )

    @staticmethod
    def _attempt(
        subject_key: str,
        outcome: SourceAttemptOutcome,
        context: SourceRequestContext,
        finished_at: datetime,
        *,
        data_as_of: Optional[datetime] = None,
        error_code: Optional[SourceErrorCode] = None,
        cooldown_until: Optional[datetime] = None,
        response_bytes: int,
    ) -> SourceAttempt:
        registry = SourceRegistryV1()
        attempt = SourceAttempt(
            _SOURCE_ID,
            _FIELD_ID,
            subject_key,
            1,
            outcome,
            context.budget.started_at,
            finished_at,
            data_as_of,
            error_code,
            cooldown_until,
            None,
            None,
            registry.version,
            registry.checksum(),
            response_bytes,
        )
        attempt.validate()
        return attempt
