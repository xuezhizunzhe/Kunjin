from __future__ import annotations

import urllib.parse
from datetime import timedelta
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import SourceErrorCode
from kunjin.decision.worker import PUBLIC_WORKER_ENV, _run_framed_worker
from kunjin.intelligence.worker_protocol import (
    MAX_INTELLIGENCE_RESPONSE_BYTES,
    IntelligenceSourceKind,
    IntelligenceWorkerFailure,
    IntelligenceWorkerRedirect,
    IntelligenceWorkerRequest,
    IntelligenceWorkerResponse,
    IntelligenceWorkerResult,
    decode_intelligence_worker_result,
    encode_intelligence_worker_failure,
    encode_intelligence_worker_redirect,
    encode_intelligence_worker_request,
    encode_intelligence_worker_success,
    validate_intelligence_source_url,
)

_INTELLIGENCE_WORKER_MODULE = "kunjin.intelligence.worker_main"
_AUDIT_CLOCK_SKEW = timedelta(seconds=1)
_MAX_REDIRECTS = 3
_SOURCE_BINDINGS: Mapping[IntelligenceSourceKind, Tuple[str, str]] = MappingProxyType(
    {
        IntelligenceSourceKind.GOV_POLICY: ("gov_cn_policy", "policy_events"),
        IntelligenceSourceKind.STCN_FUND_LIST: (
            "stcn_fund_news",
            "fund_media_events",
        ),
        IntelligenceSourceKind.STCN_FUND_DETAIL: (
            "stcn_fund_news",
            "fund_media_events",
        ),
        IntelligenceSourceKind.EASTMONEY_MARKET: (
            "eastmoney_market",
            "market_dimensions",
        ),
    }
)


class IntelligenceAcquisitionError(RuntimeError):
    def __init__(
        self,
        reason_code: SourceErrorCode,
        *,
        retryable: bool,
        http_status: Optional[int] = None,
    ) -> None:
        if type(reason_code) is not SourceErrorCode or type(retryable) is not bool:
            raise ValueError("intelligence acquisition error identity is invalid")
        self.reason_code = reason_code
        self.retryable = retryable
        self.http_status = http_status
        super().__init__(f"public intelligence acquisition failed: {reason_code.value}")


def source_binding(source_kind: IntelligenceSourceKind) -> Tuple[str, str]:
    if type(source_kind) is not IntelligenceSourceKind:
        raise ValueError("intelligence source kind is invalid")
    return _SOURCE_BINDINGS[source_kind]


def _validate_parent_result(
    result: IntelligenceWorkerResult,
    request: IntelligenceWorkerRequest,
    budget: RequestBudget,
) -> None:
    if type(result) not in {
        IntelligenceWorkerResponse,
        IntelligenceWorkerRedirect,
        IntelligenceWorkerFailure,
    }:
        raise ValueError("intelligence worker result uses an invalid exact type")
    if type(result) is IntelligenceWorkerResponse:
        encode_intelligence_worker_success(request, result)
    elif type(result) is IntelligenceWorkerRedirect:
        encode_intelligence_worker_redirect(request, result)
    else:
        assert type(result) is IntelligenceWorkerFailure
        encode_intelligence_worker_failure(request, result)
    if result.requested_url != request.requested_url:
        raise ValueError("intelligence worker result identity does not match request")
    validate_intelligence_source_url(request.source_kind, result.requested_url)
    if not (
        budget.started_at - _AUDIT_CLOCK_SKEW
        <= result.retrieved_at
        <= budget.deadline_at + _AUDIT_CLOCK_SKEW
    ):
        raise ValueError("intelligence worker retrieval time is outside request audit window")
    if type(result) is IntelligenceWorkerResponse:
        validate_intelligence_source_url(request.source_kind, result.final_url)


def run_intelligence_worker(
    request: IntelligenceWorkerRequest,
    budget: RequestBudget,
) -> IntelligenceWorkerResult:
    if type(request) is not IntelligenceWorkerRequest:
        raise ValueError("request must use the exact intelligence worker type")
    if type(budget) is not RequestBudget:
        raise ValueError("budget must use the exact request budget type")
    request.validate()
    if request.request_id != budget.request_id:
        raise ValueError("intelligence worker and budget request identities differ")
    if not budget.started_at <= request.deadline_utc <= budget.deadline_at:
        raise ValueError("intelligence worker deadline exceeds request budget")
    return _run_framed_worker(
        request,
        budget,
        encoder=encode_intelligence_worker_request,
        decoder=decode_intelligence_worker_result,
        validator=_validate_parent_result,
        module=_INTELLIGENCE_WORKER_MODULE,
        max_response_bytes=MAX_INTELLIGENCE_RESPONSE_BYTES,
        environment_profile=PUBLIC_WORKER_ENV,
    )


def acquire_intelligence_source(
    request: IntelligenceWorkerRequest,
    budget: RequestBudget,
    *,
    runner: Callable[
        [IntelligenceWorkerRequest, RequestBudget], IntelligenceWorkerResult
    ] = run_intelligence_worker,
) -> IntelligenceWorkerResponse:
    if type(request) is not IntelligenceWorkerRequest:
        raise ValueError("request must use the exact intelligence worker type")
    if type(budget) is not RequestBudget:
        raise ValueError("budget must use the exact request budget type")
    if not callable(runner):
        raise ValueError("intelligence worker runner must be callable")
    request.validate()
    source_binding(request.source_kind)
    if request.request_id != budget.request_id:
        raise ValueError("intelligence worker and budget request identities differ")
    if not budget.started_at <= request.deadline_utc <= budget.deadline_at:
        raise ValueError("intelligence worker deadline exceeds request budget")

    current = request
    visited = {request.requested_url}
    for _redirect_count in range(_MAX_REDIRECTS + 1):
        budget.require_publishable()
        result = runner(current, budget)
        _validate_parent_result(result, current, budget)
        budget.require_publishable()
        if type(result) is IntelligenceWorkerResponse:
            return result
        if type(result) is IntelligenceWorkerFailure:
            raise IntelligenceAcquisitionError(
                result.reason_code,
                retryable=result.retryable,
                http_status=result.http_status,
            )
        assert type(result) is IntelligenceWorkerRedirect
        redirected = urllib.parse.urljoin(current.requested_url, result.location)
        try:
            validate_intelligence_source_url(current.source_kind, redirected)
            current_host = urllib.parse.urlsplit(current.requested_url).hostname
            redirect_host = urllib.parse.urlsplit(redirected).hostname
            if current_host != redirect_host or redirected in visited:
                raise ValueError("unsafe redirect")
        except ValueError:
            raise IntelligenceAcquisitionError(
                SourceErrorCode.UNSAFE_REDIRECT,
                retryable=False,
                http_status=result.http_status,
            ) from None
        visited.add(redirected)
        current = IntelligenceWorkerRequest(
            source_kind=request.source_kind,
            requested_url=redirected,
            request_id=request.request_id,
            deadline_utc=request.deadline_utc,
            maximum_bytes=request.maximum_bytes,
        )
    raise IntelligenceAcquisitionError(
        SourceErrorCode.UNSAFE_REDIRECT,
        retryable=False,
    )
