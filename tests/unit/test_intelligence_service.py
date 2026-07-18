from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    RequestTerminalStatus,
    SourceAttemptOutcome,
    SourceErrorCode,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.store import FundDisclosureStore
from kunjin.intelligence.acquisition import IntelligenceAcquisitionError
from kunjin.intelligence.models import MarketDimension
from kunjin.intelligence.research import public_intelligence_payload
from kunjin.intelligence.service import (
    IntelligenceService,
    IntelligenceServiceError,
    PragmaticIntelligenceResult,
)
from kunjin.intelligence.store import IntelligenceStore
from kunjin.intelligence.worker_protocol import (
    IntelligenceSourceKind,
    IntelligenceWorkerResponse,
)
from kunjin.models import InvestmentThesis
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parents[1] / "fixtures" / "intelligence"


def _service(tmp_path: Path, acquire) -> IntelligenceService:
    repository = Repository(tmp_path / "intelligence-service.db")
    repository.migrate()
    audit = DecisionAuditStore(repository)
    return IntelligenceService(
        repository,
        audit,
        IntelligenceStore(repository, audit),
        SourceHealthService(audit, wall_clock=lambda: NOW),
        FundDisclosureStore(repository),
        clock=lambda: NOW,
        acquire=acquire,
        monotonic=lambda: 10.0,
    )


def _response(request, payload: str) -> IntelligenceWorkerResponse:
    import hashlib

    return IntelligenceWorkerResponse(
        requested_url=request.requested_url,
        final_url=request.requested_url,
        retrieved_at=NOW,
        content_type="application/json; charset=utf-8",
        payload_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        payload_utf8=payload,
    )


def test_service_has_no_transient_browser_input() -> None:
    parameters = inspect.signature(IntelligenceService.news_recent).parameters

    assert "external_context" not in parameters
    assert "browser_results" not in parameters


def test_result_wrapper_requires_authenticated_graph_closure() -> None:
    parameters = inspect.signature(PragmaticIntelligenceResult).parameters

    assert tuple(parameters) == (
        "report",
        "terminal_request",
        "subject",
        "items",
        "item_uses",
        "lineage_edges",
        "events",
        "source_summaries",
        "sector_labels",
        "fund_context",
        "thesis_review",
    )


def test_all_sources_failed_returns_authenticated_partial_without_synthetic_evidence(
    tmp_path: Path,
) -> None:
    def fail(_request, _budget):
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    result = _service(tmp_path, fail).news_recent()

    assert result.report is None
    assert result.terminal_request.status is RequestTerminalStatus.PARTIAL
    assert result.items == ()
    assert result.events == ()
    assert tuple(summary.outcome.value for summary in result.source_summaries) == (
        "unavailable",
        "unavailable",
    )
    assert result.terminal_request.omitted_work == (
        "gov_cn_policy_source_unavailable",
        "stcn_fund_news_source_unavailable",
    )


def test_one_source_success_publishes_authenticated_news_with_empty_market_facts(
    tmp_path: Path,
) -> None:
    gov = (FIXTURES / "gov_policy.json").read_text(encoding="utf-8")

    def acquire(request, _budget):
        if request.source_kind is IntelligenceSourceKind.GOV_POLICY:
            return _response(request, gov)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    result = _service(tmp_path, acquire).news_recent(window="near_term")

    assert result.report is not None
    assert result.report.terminal_status is RequestTerminalStatus.PARTIAL
    assert len(result.items) == 2
    assert result.report.snapshot.market_state.dimensions == ()
    assert set(result.report.snapshot.market_state.unknown_dimensions) == set(MarketDimension)
    assert result.report.snapshot.exact_amount_available is False
    encoded = result.report.snapshot.canonical_json()
    for forbidden in (b"portfolio_weight", b'"shares":', b'"cost":', b'"profit":'):
        assert forbidden not in encoded


def test_fund_intelligence_rejects_internal_global_sentinel(tmp_path: Path) -> None:
    service = _service(tmp_path, lambda _request, _budget: pytest.fail("not called"))

    with pytest.raises(ValueError, match="sentinel"):
        service.fund_intelligence("000000")


def test_authenticated_cooldown_stops_a_second_source_call(tmp_path: Path) -> None:
    calls = []

    def fail(request, _budget):
        calls.append(request.source_kind)
        code = (
            SourceErrorCode.DNS_FAILURE
            if request.source_kind is IntelligenceSourceKind.GOV_POLICY
            else SourceErrorCode.SOURCE_UNAVAILABLE
        )
        raise IntelligenceAcquisitionError(code, retryable=code is SourceErrorCode.DNS_FAILURE)

    service = _service(tmp_path, fail)
    service.news_recent()
    second = service.news_recent()

    assert calls.count(IntelligenceSourceKind.GOV_POLICY) == 1
    assert "gov_cn_policy_cooldown_active" in second.terminal_request.omitted_work


def test_unexpected_exception_after_begin_is_sanitized_and_terminalized(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path, lambda _request, _budget: pytest.fail("not called"))

    with patch.object(
        service._store,
        "expire_excerpts",
        side_effect=RuntimeError("secret /tmp/private-owner-path"),
    ), pytest.raises(IntelligenceServiceError, match="intelligence service failed") as error:
        service.news_recent()

    terminal = service._store.authenticated_terminal_request(error.value.request_run_id)
    assert terminal.status is RequestTerminalStatus.PARTIAL
    assert terminal.omitted_work == ("unexpected_service_failure",)
    assert "private-owner-path" not in str(error.value)


def test_cached_item_exposes_current_request_item_use_not_old_canonical_attempt(
    tmp_path: Path,
) -> None:
    gov = (FIXTURES / "gov_policy.json").read_text(encoding="utf-8")

    def acquire(request, _budget):
        if request.source_kind is IntelligenceSourceKind.GOV_POLICY:
            return _response(request, gov)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    service = _service(tmp_path, acquire)
    first = service.news_recent(window="near_term")
    second = service.news_recent(window="near_term")

    assert first.items[0].source_attempt_id != second.item_uses[0].source_attempt_id
    assert second.items[0].source_attempt_id == first.items[0].source_attempt_id
    assert second.item_uses[0].item_id == second.items[0].item_id
    assert second.item_uses[0].source_attempt_id in {
        summary.source_attempt_id for summary in second.source_summaries
    }


def test_partial_stcn_group_keeps_later_success_and_is_not_cached(
    tmp_path: Path,
) -> None:
    list_payload = (FIXTURES / "stcn_fund_list.html").read_text(encoding="utf-8").replace(
        "</main>",
        '<a href="/article/detail/3359703.html">第三篇基金新闻</a></main>',
    )
    first_detail = (FIXTURES / "stcn_fund_detail.html").read_text(encoding="utf-8")
    third_detail = first_detail.replace("3359541", "3359703").replace(
        "公募基金积极布局长期资金", "第三篇基金新闻"
    )
    list_calls = 0

    def acquire(request, _budget):
        nonlocal list_calls
        if request.source_kind is IntelligenceSourceKind.GOV_POLICY:
            raise IntelligenceAcquisitionError(
                SourceErrorCode.SOURCE_UNAVAILABLE,
                retryable=False,
            )
        if request.source_kind is IntelligenceSourceKind.STCN_FUND_LIST:
            list_calls += 1
            return _response(request, list_payload)
        if "3359602" in request.requested_url:
            raise IntelligenceAcquisitionError(
                SourceErrorCode.SOURCE_UNAVAILABLE,
                retryable=False,
            )
        payload = third_detail if "3359703" in request.requested_url else first_detail
        return _response(request, payload)

    service = _service(tmp_path, acquire)
    first = service.news_recent()
    second = service.news_recent()

    assert {item.title for item in first.items} == {
        "公募基金积极布局长期资金",
        "第三篇基金新闻",
    }
    assert "stcn_fund_news_source_unavailable" in first.terminal_request.omitted_work
    assert list_calls == 2
    assert "stcn_fund_news_source_unavailable" in second.terminal_request.omitted_work
    stcn = next(
        summary
        for summary in second.source_summaries
        if summary.source_id == "stcn_fund_news"
    )
    assert stcn.completeness == "partial"
    assert stcn.coverage_gap_codes == ("stcn_fund_news_source_unavailable",)
    assert stcn.supplementation is not None


def test_partial_stcn_cache_is_not_reused_across_request_subjects(
    tmp_path: Path,
) -> None:
    list_payload = (FIXTURES / "stcn_fund_list.html").read_text(encoding="utf-8")
    detail_payload = (FIXTURES / "stcn_fund_detail.html").read_text(encoding="utf-8")
    list_calls = 0

    def acquire(request, _budget):
        nonlocal list_calls
        if request.source_kind is IntelligenceSourceKind.STCN_FUND_LIST:
            list_calls += 1
            return _response(request, list_payload)
        if request.source_kind is IntelligenceSourceKind.STCN_FUND_DETAIL:
            if "3359602" in request.requested_url:
                raise IntelligenceAcquisitionError(
                    SourceErrorCode.SOURCE_UNAVAILABLE,
                    retryable=False,
                )
            return _response(request, detail_payload)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    service = _service(tmp_path, acquire)
    first = service.news_recent()
    second = service.fund_intelligence("519755")

    assert list_calls == 2
    assert "stcn_fund_news_source_unavailable" in first.terminal_request.omitted_work
    assert "stcn_fund_news_source_unavailable" in second.terminal_request.omitted_work


def test_stcn_list_without_any_usable_detail_does_not_publish_zero_evidence_snapshot(
    tmp_path: Path,
) -> None:
    list_payload = (FIXTURES / "stcn_fund_list.html").read_text(encoding="utf-8")

    def acquire(request, _budget):
        if request.source_kind is IntelligenceSourceKind.STCN_FUND_LIST:
            return _response(request, list_payload)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    result = _service(tmp_path, acquire).news_recent()

    assert result.report is None
    assert result.items == ()
    assert not any(
        summary.source_id == "stcn_fund_news"
        and summary.outcome is SourceAttemptOutcome.SUCCESS
        for summary in result.source_summaries
    )


def test_market_never_uses_news_item_cache_and_refreshes_each_request(
    tmp_path: Path,
) -> None:
    market = (FIXTURES / "eastmoney_market.json").read_text(encoding="utf-8")
    market_calls = 0

    def acquire(request, _budget):
        nonlocal market_calls
        if request.source_kind is IntelligenceSourceKind.EASTMONEY_MARKET:
            market_calls += 1
            return _response(request, market)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    service = _service(tmp_path, acquire)
    service.market_overview()
    service.market_overview()

    assert market_calls == 2


def test_null_snapshot_keeps_request_subject_but_no_fund_facts(tmp_path: Path) -> None:
    def fail(_request, _budget):
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    result = _service(tmp_path, fail).fund_intelligence("519755")

    assert result.report is None
    assert result.subject.subject_scope == "named_public_fund"
    assert result.subject.fund_code == "519755"
    assert result.fund_context is None
    assert result.thesis_review is None
    assert result.items == ()
    assert result.item_uses == ()

    payload = public_intelligence_payload(result)
    assert payload["request"]["workflow"] == "fund_intelligence"
    assert payload["request"]["subject_scope"] == "named_public_fund"
    assert payload["request"]["subject_fund_code"] == "519755"
    assert payload["fund_relevance"]["context"] is None
    assert {item["outcome"] for item in payload["request"]["sources"]} == {
        "unavailable"
    }
    assert {item["source_tier"] for item in payload["request"]["sources"]} == {
        "tier_1",
        "tier_2",
    }
    assert set(result.terminal_request.omitted_work).issubset(payload["missing_evidence"])


def test_http_retrieval_time_is_preserved_and_not_promoted_to_market_session(
    tmp_path: Path,
) -> None:
    market = (FIXTURES / "eastmoney_market.json").read_text(encoding="utf-8")
    retrieved_at = NOW - timedelta(milliseconds=500)

    def acquire(request, _budget):
        if request.source_kind is IntelligenceSourceKind.EASTMONEY_MARKET:
            response = _response(request, market)
            return IntelligenceWorkerResponse(
                **{**response.__dict__, "retrieved_at": retrieved_at}
            )
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    result = _service(tmp_path, acquire).market_overview()
    payload = public_intelligence_payload(result)

    eastmoney = next(
        item for item in payload["request"]["sources"]
        if item["source_id"] == "eastmoney_market"
    )
    assert eastmoney["retrieved_at"] == retrieved_at.isoformat()
    assert eastmoney["data_as_of"] is None
    assert eastmoney["data_as_of_semantics"] == (
        "http_retrieval_not_market_session"
    )
    assert eastmoney["retrieved_at_semantics"] == "http_retrieval_time"
    assert payload["experimental_shadow"]["market_session_as_of"] is None
    assert payload["experimental_shadow"]["market_direction_status"] == (
        "insufficient_data"
    )
    assert all(item["data_as_of"] is None for item in payload["dimensions"])
    assert all(item["freshness"] == "unknown" for item in payload["dimensions"])
    assert "market_data_time_unavailable" in payload["missing_evidence"]
    assert all(
        {"sector_code", "sector_name", "freshness", "basis"}.issubset(item)
        for item in payload["experimental_shadow"]["sector_states"]
    )


def test_fund_context_is_disclosed_only_and_thesis_match_requires_manual_review(
    tmp_path: Path,
) -> None:
    gov = (FIXTURES / "gov_policy.json").read_text(encoding="utf-8")

    def acquire(request, _budget):
        if request.source_kind is IntelligenceSourceKind.GOV_POLICY:
            return _response(request, gov)
        raise IntelligenceAcquisitionError(
            SourceErrorCode.SOURCE_UNAVAILABLE,
            retryable=False,
        )

    service = _service(tmp_path, acquire)
    service._repository.add_thesis(
        InvestmentThesis(
            fund_code="519755",
            rationale="用于学习长期资金政策影响",
            horizon="长期观察",
            invalidation="支持长期资金入市",
            created_at=NOW - timedelta(days=1),
        )
    )
    result = service.fund_intelligence("519755", window="near_term")
    payload = public_intelligence_payload(result)

    assert payload["fund_relevance"]["coverage_scope"] == "disclosed_context"
    assert payload["fund_relevance"]["covered_fields"] == []
    assert set(payload["fund_relevance"]["not_covered_fields"]) >= {
        "identity",
        "active_benchmark",
        "disclosed_holdings",
    }
    assert set(payload["fund_relevance"]["not_covered_fields"]) >= {
        "formal_nav",
        "manager",
        "fees",
    }
    assert payload["fund_relevance"]["companion_workflows_required"] == [
        "decision_route",
        "fund_brief",
        "portfolio",
    ]
    assert payload["thesis_review"]["evidence_check"] == (
        "possible_invalidation_match"
    )
    assert payload["thesis_review"]["semantic_review_required"] is True
    assert payload["thesis_review"]["can_trigger_sale"] is False
    assert payload["action_maturity"] == "evidence_only"
    assert payload["action_authorized"] is False
    assert all(item["evidence_role"] == "source_fact" for item in payload["items"])
    assert all(
        item["evidence_role"] == "reasoned_inference" for item in payload["events"]
    )
    assert all(
        item["opposing_evidence_assessment"] == "not_systematically_detected"
        for item in payload["events"]
    )
    assert payload["cross_validation"]["complete"] is False
