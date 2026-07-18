from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    TRANSIENT_SOURCE_ERRORS,
    UNAVAILABLE_SOURCE_ERRORS,
    UNSUPPORTED_SOURCE_ERRORS,
    FreshnessContext,
    RequestMode,
    RequestTerminalStatus,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldState,
    validate_identifier,
    validate_identifier_tuple,
    validate_public_text,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.service import expected_report_period
from kunjin.funds.store import FundDisclosureStore
from kunjin.intelligence.acquisition import (
    IntelligenceAcquisitionError,
    acquire_intelligence_source,
    source_binding,
)
from kunjin.intelligence.analysis import (
    MarketBatch,
    PublicFundContext,
    bind_public_entities,
    build_events,
    build_fund_relevance,
    build_lineage,
    build_market_state,
    news_item_from_parsed,
)
from kunjin.intelligence.models import (
    EntityAlias,
    IntelligenceReport,
    IntelligenceSnapshot,
    IntelligenceWorkflow,
    LineageEdge,
    MarketDimension,
    MarketEntity,
    MarketShadowState,
    MarketStateSnapshot,
    NewsEvent,
    NewsItem,
    QueryInterval,
    QueryWindow,
)
from kunjin.intelligence.parsers import (
    parse_eastmoney_market,
    parse_gov_policy_list,
    parse_stcn_detail,
    parse_stcn_fund_list,
)
from kunjin.intelligence.policy import IntelligencePolicyV1
from kunjin.intelligence.store import (
    AuthenticatedSnapshotItemUse,
    AuthenticatedTerminalRequest,
    IntelligenceStore,
    IntelligenceStoreError,
)
from kunjin.intelligence.worker_protocol import (
    IntelligenceSourceKind,
    IntelligenceWorkerRequest,
    IntelligenceWorkerResponse,
)
from kunjin.storage.repository import Repository

GLOBAL_PUBLIC_SUBJECT_KEY = "fund:000000"
_FUND_CODE = re.compile(r"^[0-9]{6}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_GOV_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
_STCN_LIST_URL = "https://www.stcn.com/article/list/fund.html"
_EXCERPT_RETENTION = timedelta(days=365)
_CURRENT_CACHE = timedelta(hours=2)


class IntelligenceServiceError(ValueError):
    code = "intelligence_service_failed"

    def __init__(self, request_run_id: int) -> None:
        if type(request_run_id) is not int or request_run_id <= 0:
            raise ValueError("intelligence service error requires a positive request id")
        self.request_run_id = request_run_id
        super().__init__("intelligence service failed")


@dataclass(frozen=True)
class IntelligenceRequestSubject:
    workflow: IntelligenceWorkflow
    interval: QueryInterval
    subject_scope: str
    fund_code: Optional[str]

    def validate(self) -> None:
        if type(self.workflow) is not IntelligenceWorkflow:
            raise ValueError("request subject workflow must be exact")
        if type(self.interval) is not QueryInterval:
            raise ValueError("request subject interval must be exact")
        self.interval.validate()
        if self.subject_scope not in {"global_public", "named_public_fund"}:
            raise ValueError("request subject scope is invalid")
        if self.subject_scope == "global_public":
            if (
                self.fund_code is not None
                or self.workflow is IntelligenceWorkflow.FUND_INTELLIGENCE
            ):
                raise ValueError("global request subject cannot declare a fund")
        elif (
            self.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE
            or self.fund_code is None
            or _FUND_CODE.fullmatch(self.fund_code) is None
            or self.fund_code == "000000"
        ):
            raise ValueError("named request subject requires a public fund code")


def _eastmoney_industry_url() -> str:
    return "https://push2.eastmoney.com/api/qt/clist/get?" + urllib.parse.urlencode(
        {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:90+t:2",
            "fields": "f12,f14,f3,f8,f62,f184,f104,f105",
        }
    )


@dataclass(frozen=True)
class IntelligenceSourceSummary:
    source_attempt_id: int
    source_id: str
    field_id: str
    outcome: SourceAttemptOutcome
    data_as_of: Optional[datetime]
    retrieved_at: Optional[datetime]
    endpoint: str
    completeness: str
    coverage_gap_codes: Tuple[str, ...]
    reason_code: Optional[str]
    retryable: Optional[bool]
    cooldown_until: Optional[datetime]
    supplementation: Optional[str]

    def validate(self) -> None:
        if type(self.source_attempt_id) is not int or self.source_attempt_id <= 0:
            raise ValueError("source summary attempt id must be positive")
        validate_identifier(self.source_id, "source summary source id")
        validate_identifier(self.field_id, "source summary field id")
        for value, name in (
            (self.data_as_of, "source summary data time"),
            (self.retrieved_at, "source summary retrieval time"),
            (self.cooldown_until, "source summary cooldown"),
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{name} must be UTC")
        validate_public_text(self.endpoint, "source summary endpoint")
        if self.completeness not in {"complete", "partial", "insufficient"}:
            raise ValueError("source summary completeness is invalid")
        if type(self.coverage_gap_codes) is not tuple:
            raise ValueError("source summary coverage gaps must be an exact tuple")
        validate_identifier_tuple(
            self.coverage_gap_codes,
            "source summary coverage gaps",
        )
        if tuple(sorted(set(self.coverage_gap_codes))) != self.coverage_gap_codes:
            raise ValueError("source summary coverage gaps must be unique and ascending")
        if (self.completeness == "complete") == bool(self.coverage_gap_codes):
            raise ValueError("source summary completeness and coverage gaps are inconsistent")
        if self.reason_code is not None:
            validate_identifier(self.reason_code, "source summary reason code")
        if self.retryable is not None and type(self.retryable) is not bool:
            raise ValueError("source summary retryable must be boolean or None")
        if self.supplementation is not None:
            validate_public_text(self.supplementation, "source summary supplementation")
        usable = self.outcome in {
            SourceAttemptOutcome.SUCCESS,
            SourceAttemptOutcome.CACHE_HIT,
        }
        if usable != (self.reason_code is None):
            raise ValueError("source summary outcome and reason code are inconsistent")


@dataclass(frozen=True)
class PublicThesisReview:
    reason: str
    horizon: str
    invalidation: str
    evidence_check: str
    evidence_ids: Tuple[str, ...]

    def validate(self) -> None:
        validate_public_text(self.reason, "thesis reason")
        validate_public_text(self.horizon, "thesis horizon")
        validate_public_text(self.invalidation, "thesis invalidation")
        if self.evidence_check not in {
            "possible_invalidation_match",
            "no_matching_evidence",
        }:
            raise ValueError("thesis evidence check is invalid")
        if type(self.evidence_ids) is not tuple:
            raise ValueError("thesis evidence ids must be an exact tuple")
        for evidence_id in self.evidence_ids:
            validate_identifier(evidence_id, "thesis evidence id")
        if tuple(sorted(set(self.evidence_ids))) != self.evidence_ids:
            raise ValueError("thesis evidence ids must be unique and ascending")


@dataclass(frozen=True)
class PublicSectorLabel:
    entity_id: str
    sector_code: str
    sector_name: str

    def validate(self) -> None:
        validate_identifier(self.entity_id, "sector label entity id")
        validate_public_text(self.sector_code, "sector label code")
        validate_public_text(self.sector_name, "sector label name")


@dataclass(frozen=True)
class PublicFundDisclosureContext:
    relevance_context: PublicFundContext
    coverage_scope: str
    covered_fields: Tuple[str, ...]
    not_covered_fields: Tuple[str, ...]
    holdings_section_state: str
    holdings_freshness: str
    holdings_published_at: Optional[datetime]
    holdings_last_success_at: Optional[datetime]
    holdings_retrieved_at: Optional[datetime]
    source_boundary: str
    companion_workflows: Tuple[str, ...]

    def validate(self) -> None:
        if type(self.relevance_context) is not PublicFundContext:
            raise ValueError("fund disclosure relevance context must be exact")
        self.relevance_context.validate()
        if self.coverage_scope != "disclosed_context":
            raise ValueError("fund disclosure scope must be disclosed_context")
        for values, name in (
            (self.covered_fields, "fund covered fields"),
            (self.not_covered_fields, "fund uncovered fields"),
            (self.companion_workflows, "fund companion workflows"),
        ):
            if type(values) is not tuple:
                raise ValueError(f"{name} must be an exact tuple")
            for value in values:
                validate_identifier(value, name)
        validate_identifier(self.holdings_section_state, "holdings section state")
        if self.holdings_freshness not in {"fresh", "stale", "missing", "unknown"}:
            raise ValueError("holdings freshness is invalid")
        for value, name in (
            (self.holdings_published_at, "holdings publication time"),
            (self.holdings_last_success_at, "holdings last success time"),
            (self.holdings_retrieved_at, "holdings retrieval time"),
        ):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() != timedelta(0)
            ):
                raise ValueError(f"{name} must be UTC")
        validate_public_text(self.source_boundary, "fund source boundary")


@dataclass(frozen=True)
class PragmaticIntelligenceResult:
    report: Optional[IntelligenceReport]
    terminal_request: AuthenticatedTerminalRequest
    subject: IntelligenceRequestSubject
    items: Tuple[NewsItem, ...]
    item_uses: Tuple[AuthenticatedSnapshotItemUse, ...]
    lineage_edges: Tuple[LineageEdge, ...]
    events: Tuple[NewsEvent, ...]
    source_summaries: Tuple[IntelligenceSourceSummary, ...]
    sector_labels: Tuple[PublicSectorLabel, ...]
    fund_context: Optional[PublicFundDisclosureContext]
    thesis_review: Optional[PublicThesisReview]

    def validate(self) -> None:
        if type(self.terminal_request) is not AuthenticatedTerminalRequest:
            raise ValueError("terminal request must be authenticated")
        self.terminal_request.validate()
        if type(self.subject) is not IntelligenceRequestSubject:
            raise ValueError("request subject must be exact")
        self.subject.validate()
        for values, record_type, name in (
            (self.items, NewsItem, "result items"),
            (self.item_uses, AuthenticatedSnapshotItemUse, "result item uses"),
            (self.lineage_edges, LineageEdge, "result lineage"),
            (self.events, NewsEvent, "result events"),
            (self.source_summaries, IntelligenceSourceSummary, "result sources"),
            (self.sector_labels, PublicSectorLabel, "result sector labels"),
        ):
            if type(values) is not tuple or any(type(item) is not record_type for item in values):
                raise ValueError(f"{name} must contain exact records")
            for item in values:
                item.validate()
        if self.fund_context is not None:
            if type(self.fund_context) is not PublicFundDisclosureContext:
                raise ValueError("fund context must be exact")
            self.fund_context.validate()
        if self.thesis_review is not None:
            if type(self.thesis_review) is not PublicThesisReview:
                raise ValueError("thesis review must be exact")
            self.thesis_review.validate()

        if self.report is None:
            if (
                self.items
                or self.item_uses
                or self.lineage_edges
                or self.events
                or self.sector_labels
                or self.fund_context is not None
                or self.thesis_review is not None
            ):
                raise ValueError("a result without a snapshot cannot publish evidence records")
            if self.terminal_request.status is not RequestTerminalStatus.PARTIAL:
                raise ValueError("a result without a snapshot must be partial")
            return
        if type(self.report) is not IntelligenceReport:
            raise ValueError("result report must be exact or None")
        self.report.validate()
        snapshot = self.report.snapshot
        if (
            snapshot.request_run_id != self.terminal_request.id
            or snapshot.request_id != self.terminal_request.request_id
            or self.report.terminal_status is not self.terminal_request.status
            or self.report.omitted_work != self.terminal_request.omitted_work
        ):
            raise ValueError("result request and snapshot do not match")
        if (
            snapshot.workflow is not self.subject.workflow
            or snapshot.subject_fund_code != self.subject.fund_code
        ):
            raise ValueError("result request subject and snapshot do not match")
        sector_entities = {
            entity.entity_id: entity.canonical_name
            for entity in snapshot.entities
            if entity.entity_type == "sector"
        }
        for label in self.sector_labels:
            if sector_entities.get(label.entity_id) != label.sector_name:
                raise ValueError("sector labels do not resolve to snapshot entities")
        if self.fund_context is not None and (
            self.subject.fund_code
            != self.fund_context.relevance_context.fund_code
        ):
            raise ValueError("fund disclosure context does not match request subject")
        if tuple(item.item_id for item in self.items) != snapshot.item_ids:
            raise ValueError("result items do not close over the snapshot")
        if tuple(item.item_id for item in self.item_uses) != tuple(sorted(snapshot.item_ids)):
            raise ValueError("result item uses do not close over the snapshot")
        if tuple(edge.edge_id for edge in self.lineage_edges) != snapshot.lineage_edge_ids:
            raise ValueError("result lineage does not close over the snapshot")
        if tuple(event.event_id for event in self.events) != snapshot.event_ids:
            raise ValueError("result events do not close over the snapshot")
        usable_summary_ids = {
            summary.source_attempt_id
            for summary in self.source_summaries
            if summary.outcome
            in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
        }
        if not set(snapshot.source_attempt_ids).issubset(usable_summary_ids):
            raise ValueError("snapshot sources do not resolve to current source coverage")
        if not {item.source_attempt_id for item in self.item_uses}.issubset(
            usable_summary_ids
        ):
            raise ValueError("item uses do not resolve to current source coverage")
        item_ids = set(snapshot.item_ids)
        if self.thesis_review is not None and not set(
            self.thesis_review.evidence_ids
        ).issubset(item_ids):
            raise ValueError("thesis evidence does not close over the snapshot")


class IntelligenceService:
    def __init__(
        self,
        repository: Repository,
        audit_store: DecisionAuditStore,
        intelligence_store: IntelligenceStore,
        health_service: SourceHealthService,
        fund_store: FundDisclosureStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        acquire: Callable[
            [IntelligenceWorkerRequest, RequestBudget], IntelligenceWorkerResponse
        ] = acquire_intelligence_source,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if type(audit_store) is not DecisionAuditStore:
            raise ValueError("audit store must be exact")
        if type(intelligence_store) is not IntelligenceStore:
            raise ValueError("intelligence store must be exact")
        if type(health_service) is not SourceHealthService:
            raise ValueError("health service must be exact")
        if type(fund_store) is not FundDisclosureStore:
            raise ValueError("fund store must be exact")
        if any(
            store.repository is not repository
            for store in (audit_store, intelligence_store, fund_store)
        ) or health_service.audit_store is not audit_store:
            raise ValueError("intelligence services must share one repository and audit store")
        if not callable(clock) or not callable(acquire) or not callable(monotonic):
            raise ValueError("service clocks and acquisition must be callable")
        self._repository = repository
        self._audit = audit_store
        self._store = intelligence_store
        self._health = health_service
        self._fund_store = fund_store
        self._clock = clock
        self._acquire = acquire
        self._monotonic = monotonic
        self._policy = IntelligencePolicyV1()

    def news_recent(
        self,
        window: str = "recent",
        mode: str = "rapid",
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> PragmaticIntelligenceResult:
        return self._run(
            IntelligenceWorkflow.NEWS_RECENT,
            None,
            window,
            mode,
            start,
            end,
        )

    def market_overview(
        self,
        window: str = "recent",
        mode: str = "rapid",
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> PragmaticIntelligenceResult:
        return self._run(
            IntelligenceWorkflow.MARKET_OVERVIEW,
            None,
            window,
            mode,
            start,
            end,
        )

    def fund_intelligence(
        self,
        fund_code: str,
        window: str = "recent",
        mode: str = "rapid",
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> PragmaticIntelligenceResult:
        if _FUND_CODE.fullmatch(fund_code) is None or fund_code == "000000":
            raise ValueError("fund code must be six digits and cannot be the global sentinel")
        return self._run(
            IntelligenceWorkflow.FUND_INTELLIGENCE,
            fund_code,
            window,
            mode,
            start,
            end,
        )

    def _run(
        self,
        workflow: IntelligenceWorkflow,
        fund_code: Optional[str],
        window: str,
        mode: str,
        start: Optional[date],
        end: Optional[date],
    ) -> PragmaticIntelligenceResult:
        request_mode = RequestMode(mode)
        now = self._now()
        interval = self._interval(window, start, end, now)
        subject = IntelligenceRequestSubject(
            workflow=workflow,
            interval=interval,
            subject_scope=(
                "global_public" if fund_code is None else "named_public_fund"
            ),
            fund_code=fund_code,
        )
        subject.validate()
        budget = RequestBudget.create(
            request_mode,
            monotonic=self._monotonic,
            wall_clock=lambda: now,
        )
        request_run_id = self._audit.begin_request(budget)
        try:
            return self._run_started(
                subject,
                interval,
                budget,
                request_run_id,
                now,
            )
        except IntelligenceServiceError:
            raise
        except Exception:
            try:
                self._store.authenticated_terminal_request(request_run_id)
            except IntelligenceStoreError:
                self._audit.finalize_request(
                    request_run_id,
                    RequestTerminalStatus.PARTIAL,
                    min(self._now(), budget.deadline_at),
                    ("unexpected_service_failure",),
                )
                self._store.authenticated_terminal_request(request_run_id)
            raise IntelligenceServiceError(request_run_id) from None

    def _run_started(
        self,
        subject: IntelligenceRequestSubject,
        interval: QueryInterval,
        budget: RequestBudget,
        request_run_id: int,
        now: datetime,
    ) -> PragmaticIntelligenceResult:
        workflow = subject.workflow
        fund_code = subject.fund_code
        subject_key = GLOBAL_PUBLIC_SUBJECT_KEY if fund_code is None else f"fund:{fund_code}"
        self._store.expire_excerpts(now)
        fund_context = None if fund_code is None else self._public_fund_context(fund_code)

        items: list[NewsItem] = []
        market_batches: list[MarketBatch] = []
        usable_attempt_ids: list[int] = []
        omitted: list[str] = []
        try:
            if workflow in {
                IntelligenceWorkflow.MARKET_OVERVIEW,
                IntelligenceWorkflow.FUND_INTELLIGENCE,
            }:
                batch, attempt_id = self._market_source(
                    subject_key, budget, request_run_id, interval, omitted
                )
                if batch is not None and attempt_id is not None:
                    market_batches.append(batch)
                    usable_attempt_ids.append(attempt_id)
            for loader in (self._gov_source, self._stcn_source):
                loaded, attempt_id = loader(
                    subject_key, budget, request_run_id, interval, omitted
                )
                items.extend(loaded)
                if attempt_id is not None:
                    usable_attempt_ids.append(attempt_id)
        except BudgetExpired:
            omitted.append("request_budget_expired")

        omitted_values = tuple(sorted(set(omitted)))
        if not usable_attempt_ids:
            if not omitted_values:
                omitted_values = ("all_sources_without_usable_evidence",)
            finish = min(self._now(), budget.deadline_at)
            self._audit.finalize_request(
                request_run_id,
                RequestTerminalStatus.PARTIAL,
                finish,
                omitted_values,
            )
            result = PragmaticIntelligenceResult(
                report=None,
                terminal_request=self._store.authenticated_terminal_request(request_run_id),
                subject=subject,
                items=(),
                item_uses=(),
                lineage_edges=(),
                events=(),
                source_summaries=self._source_summaries(request_run_id, omitted_values),
                sector_labels=(),
                fund_context=None,
                thesis_review=None,
            )
            result.validate()
            return result

        unique_items = tuple(
            sorted({item.item_id: item for item in items}.values(), key=lambda item: item.item_id)
        )
        persisted_items = self._persist_items(unique_items)
        current_item_uses = self._current_item_uses(
            persisted_items,
            tuple(sorted(set(usable_attempt_ids))),
        )
        use_by_item = {
            item.item_id: item.source_attempt_id for item in current_item_uses
        }
        analysis_items = tuple(
            replace(item, source_attempt_id=use_by_item[item.item_id])
            for item in persisted_items
        )
        entities, aliases = self._entities(market_batches, fund_context, interval)
        bindings = tuple(
            bind_public_entities(item, entities, aliases) for item in analysis_items
        )
        lineage = build_lineage(analysis_items)
        events = build_events(analysis_items, bindings, lineage)
        links = (
            ()
            if fund_context is None
            else build_fund_relevance(
                events,
                fund_context.relevance_context,
                entities,
            )
        )
        with self._repository.connect() as connection, connection:
            self._store.save_lineage_and_events(lineage, events, connection)

        created_at = self._now()
        market_state = (
            build_market_state(
                tuple(market_batches),
                events,
                analysis_items,
                bindings,
                created_at,
                self._policy,
            )
            if market_batches
            else self._empty_market_state(created_at)
        )
        missing = {dimension.value for dimension in market_state.unknown_dimensions}
        missing.update(omitted_values)
        if workflow in {
            IntelligenceWorkflow.MARKET_OVERVIEW,
            IntelligenceWorkflow.FUND_INTELLIGENCE,
        }:
            missing.add("market_data_time_unavailable")
        if not persisted_items:
            missing.add("news_items")
        conflicts = (
            ("entity_alias_conflict",)
            if any(binding.ambiguous_aliases for binding in bindings)
            else ()
        )
        terminal_status = (
            RequestTerminalStatus.PARTIAL
            if omitted_values
            else RequestTerminalStatus.COMPLETE
        )
        thesis_review = self._thesis_review(fund_code, persisted_items)

        def snapshot_factory(active_request_run_id: int) -> IntelligenceSnapshot:
            snapshot = IntelligenceSnapshot(
                workflow=workflow,
                request_id=budget.request_id,
                request_run_id=active_request_run_id,
                interval=interval,
                subject_fund_code=fund_code,
                entities=entities,
                item_ids=tuple(item.item_id for item in persisted_items),
                source_attempt_ids=tuple(sorted(set(usable_attempt_ids))),
                lineage_edge_ids=tuple(edge.edge_id for edge in lineage),
                event_ids=tuple(event.event_id for event in events),
                event_entity_links=links,
                market_state=market_state,
                fund_relevance_link_ids=tuple(link.link_id for link in links),
                conflicts=conflicts,
                missing_evidence=tuple(sorted(missing)),
                created_at=created_at,
                exact_amount_available=False,
            )
            snapshot.validate()
            return snapshot

        try:
            stored = self._store.publish_snapshot(
                request_run_id,
                snapshot_factory,
                created_at,
                terminal_status,
                omitted_values,
                budget,
            )
        except (BudgetExpired, IntelligenceStoreError):
            final_omitted = tuple(sorted(set((*omitted_values, "final_publication_failed"))))
            self._audit.finalize_request(
                request_run_id,
                RequestTerminalStatus.PARTIAL,
                min(self._now(), budget.deadline_at),
                final_omitted,
            )
            result = PragmaticIntelligenceResult(
                report=None,
                terminal_request=self._store.authenticated_terminal_request(request_run_id),
                subject=subject,
                items=(),
                item_uses=(),
                lineage_edges=(),
                events=(),
                source_summaries=self._source_summaries(request_run_id, final_omitted),
                sector_labels=(),
                fund_context=None,
                thesis_review=None,
            )
            result.validate()
            return result

        authenticated_items = self._store.authenticated_items_by_keys(stored.snapshot.item_ids)
        authenticated_item_uses = self._store.authenticated_snapshot_item_uses(stored.id)
        if authenticated_item_uses != current_item_uses:
            raise IntelligenceStoreError("current item use authentication failed")
        source_summaries = self._source_summaries(request_run_id, omitted_values)
        sector_labels = self._sector_labels(market_batches)
        report = IntelligenceReport(
            snapshot=stored.snapshot,
            terminal_status=terminal_status,
            omitted_work=omitted_values,
            beginner_explanation_zh={
                "evidence_boundary": "仅包含已认证公开来源；事实、推断与行动边界分开。",
                "action_boundary": "这是实验性研究结果，不是买卖指令，也不提供精确金额。",
                "coverage_boundary": "来源失败、时间窗口和遗漏工作均按稳定代码公开。",
            },
        )
        result = PragmaticIntelligenceResult(
            report=report,
            terminal_request=self._store.authenticated_terminal_request(request_run_id),
            subject=subject,
            items=authenticated_items,
            item_uses=authenticated_item_uses,
            lineage_edges=lineage,
            events=events,
            source_summaries=source_summaries,
            sector_labels=sector_labels,
            fund_context=fund_context,
            thesis_review=thesis_review,
        )
        result.validate()
        return result

    def _market_source(self, subject_key, budget, run_id, interval, omitted):
        if self._skip_cooldown(
            IntelligenceSourceKind.EASTMONEY_MARKET,
            subject_key,
            budget,
            run_id,
            interval,
            omitted,
        ):
            return None, None
        response = self._fetch(
            IntelligenceSourceKind.EASTMONEY_MARKET,
            _eastmoney_industry_url(),
            subject_key,
            run_id,
            budget,
            omitted,
        )
        if response is None:
            return None, None
        try:
            rows = parse_eastmoney_market(
                response.payload_utf8, "industry", response.retrieved_at
            )
        except ValueError:
            self._record_failure(
                run_id,
                subject_key,
                IntelligenceSourceKind.EASTMONEY_MARKET,
                SourceErrorCode.PARSE_FAILURE,
                budget,
            )
            omitted.append("eastmoney_market_parse_failure")
            return None, None
        attempt_id = self._record_success(
            run_id,
            subject_key,
            IntelligenceSourceKind.EASTMONEY_MARKET,
            SourceAttemptOutcome.SUCCESS,
            response.retrieved_at,
            len(response.payload_utf8.encode("utf-8")),
            budget,
        )
        return MarketBatch(attempt_id, rows, response.retrieved_at), attempt_id

    def _gov_source(self, subject_key, budget, run_id, interval, omitted):
        if self._skip_cooldown(
            IntelligenceSourceKind.GOV_POLICY,
            subject_key,
            budget,
            run_id,
            interval,
            omitted,
        ):
            return (), None
        cached = self._store.authenticated_cached_items(
            "gov_cn_policy", interval.end_at - _CURRENT_CACHE, interval.end_at
        )
        if cached:
            attempt_id = self._record_success(
                run_id,
                subject_key,
                IntelligenceSourceKind.GOV_POLICY,
                SourceAttemptOutcome.CACHE_HIT,
                max(item.retrieved_at for item in cached),
                0,
                budget,
            )
            return self._inside_interval(cached, interval), attempt_id
        response = self._fetch(
            IntelligenceSourceKind.GOV_POLICY,
            _GOV_URL,
            subject_key,
            run_id,
            budget,
            omitted,
        )
        if response is None:
            return (), None
        try:
            parsed = parse_gov_policy_list(response.payload_utf8, response.retrieved_at)
        except ValueError:
            self._record_failure(
                run_id,
                subject_key,
                IntelligenceSourceKind.GOV_POLICY,
                SourceErrorCode.PARSE_FAILURE,
                budget,
            )
            omitted.append("gov_cn_policy_parse_failure")
            return (), None
        attempt_id = self._record_success(
            run_id,
            subject_key,
            IntelligenceSourceKind.GOV_POLICY,
            SourceAttemptOutcome.SUCCESS,
            response.retrieved_at,
            len(response.payload_utf8.encode("utf-8")),
            budget,
        )
        converted = tuple(
            news_item_from_parsed(
                item,
                attempt_id,
                item.retrieved_at + _EXCERPT_RETENTION,
            )
            for item in parsed
        )
        return self._inside_interval(converted, interval), attempt_id

    def _stcn_source(self, subject_key, budget, run_id, interval, omitted):
        if self._skip_cooldown(
            IntelligenceSourceKind.STCN_FUND_LIST,
            subject_key,
            budget,
            run_id,
            interval,
            omitted,
        ):
            return (), None
        started = self._now()
        responses = []
        try:
            list_response = self._acquire(
                self._request(IntelligenceSourceKind.STCN_FUND_LIST, _STCN_LIST_URL, budget),
                budget,
            )
            candidates = parse_stcn_fund_list(
                list_response.payload_utf8, list_response.retrieved_at
            )
            responses.append(list_response)
            cap = 12 if budget.mode is RequestMode.RAPID else 36
            parsed_items = []
            stopped_old = False
            for candidate in candidates[:cap]:
                budget.require_publishable()
                try:
                    detail = self._acquire(
                        self._request(
                            IntelligenceSourceKind.STCN_FUND_DETAIL,
                            candidate.canonical_url,
                            budget,
                        ),
                        budget,
                    )
                    parsed = parse_stcn_detail(detail.payload_utf8, detail.retrieved_at)
                    if parsed.canonical_url != candidate.canonical_url:
                        raise ValueError("STCN detail canonical URL differs from its list entry")
                except BudgetExpired:
                    omitted.append("stcn_fund_news_deadline")
                    break
                except IntelligenceAcquisitionError as exc:
                    omitted.append(f"stcn_fund_news_{exc.reason_code.value}")
                    continue
                except ValueError:
                    omitted.append("stcn_fund_news_parse_failure")
                    continue
                responses.append(detail)
                if parsed.publication_interval_end is not None:
                    old = parsed.publication_interval_end <= interval.start_at
                else:
                    old = parsed.published_at < interval.start_at
                if old:
                    stopped_old = True
                    break
                if parsed.published_at <= interval.end_at:
                    parsed_items.append(parsed)
            if len(candidates) > cap and not stopped_old:
                omitted.append("stcn_detail_cap_reached")
        except BudgetExpired:
            omitted.append("stcn_fund_news_deadline")
            return (), None
        except IntelligenceAcquisitionError as exc:
            self._record_failure(
                run_id, subject_key, IntelligenceSourceKind.STCN_FUND_LIST, exc.reason_code, budget
            )
            omitted.append(f"stcn_fund_news_{exc.reason_code.value}")
            return (), None
        except ValueError:
            self._record_failure(
                run_id,
                subject_key,
                IntelligenceSourceKind.STCN_FUND_LIST,
                SourceErrorCode.PARSE_FAILURE,
                budget,
            )
            omitted.append("stcn_fund_news_parse_failure")
            return (), None
        if not parsed_items:
            error_code = (
                SourceErrorCode.PARSE_FAILURE
                if "stcn_fund_news_parse_failure" in omitted
                else SourceErrorCode.SOURCE_UNAVAILABLE
            )
            self._record_failure(
                run_id,
                subject_key,
                IntelligenceSourceKind.STCN_FUND_LIST,
                error_code,
                budget,
            )
            if not any(code.startswith("stcn_fund_news_") for code in omitted):
                omitted.append("stcn_fund_news_no_usable_items")
            return (), None
        finished = max((item.retrieved_at for item in responses), default=started)
        attempt_id = self._record_attempt(
            run_id,
            subject_key,
            IntelligenceSourceKind.STCN_FUND_LIST,
            SourceAttemptOutcome.SUCCESS,
            started,
            finished,
            finished,
            None,
            None,
            sum(len(item.payload_utf8.encode("utf-8")) for item in responses),
        )
        converted = tuple(
            news_item_from_parsed(
                item,
                attempt_id,
                item.retrieved_at + _EXCERPT_RETENTION,
            )
            for item in parsed_items
        )
        return converted, attempt_id

    def _fetch(self, kind, url, subject_key, run_id, budget, omitted):
        try:
            return self._acquire(self._request(kind, url, budget), budget)
        except BudgetExpired:
            raise
        except IntelligenceAcquisitionError as exc:
            self._record_failure(run_id, subject_key, kind, exc.reason_code, budget)
            source_id, _field = source_binding(kind)
            omitted.append(f"{source_id}_{exc.reason_code.value}")
            return None

    def _skip_cooldown(
        self,
        kind,
        subject_key,
        budget,
        run_id,
        interval,
        omitted,
    ) -> bool:
        source_id, field_id = source_binding(kind)
        now = min(self._now(), budget.deadline_at)
        context = FreshnessContext(
            now=now,
            request_id=budget.request_id,
            query_window_start=interval.start_at,
            query_window_end=interval.end_at,
            correction_retraction_check_complete=True,
            correction_retraction_found=False,
            correction_retraction_checked_at=now,
        )
        state, history = self._health.source_field_state_and_history(
            source_id,
            field_id,
            subject_key,
            context,
            request_run_id=run_id,
            budget=budget,
        )
        if state is not SourceFieldState.COOLDOWN:
            return False
        cooldown = next(
            (
                stored.attempt.cooldown_until
                for stored in history.attempts
                if stored.attempt.cooldown_until is not None
                and stored.attempt.cooldown_until > now
            ),
            None,
        )
        if cooldown is None:
            raise ValueError("cooldown state has no authenticated deadline")
        self._record_attempt(
            run_id,
            subject_key,
            kind,
            SourceAttemptOutcome.SKIPPED_COOLDOWN,
            now,
            now,
            None,
            SourceErrorCode.COOLDOWN_ACTIVE,
            cooldown,
            0,
        )
        omitted.append(f"{source_id}_cooldown_active")
        return True

    def _request(self, kind, url, budget):
        worker_seconds = budget.worker_seconds()
        if worker_seconds <= 0:
            raise BudgetExpired("no worker budget remains")
        deadline = min(
            budget.deadline_at,
            self._now() + timedelta(seconds=worker_seconds),
        )
        return IntelligenceWorkerRequest(kind, url, budget.request_id, deadline)

    def _record_success(self, run_id, subject_key, kind, outcome, data_as_of, size, budget):
        now = self._now()
        return self._record_attempt(
            run_id,
            subject_key,
            kind,
            outcome,
            max(budget.started_at, now),
            max(budget.started_at, now),
            data_as_of,
            None,
            None,
            size,
        )

    def _record_failure(self, run_id, subject_key, kind, error_code, budget):
        now = min(self._now(), budget.deadline_at)
        if error_code in TRANSIENT_SOURCE_ERRORS:
            outcome = SourceAttemptOutcome.TRANSIENT_FAILURE
            cooldown = now + timedelta(minutes=30)
        elif error_code in UNSUPPORTED_SOURCE_ERRORS:
            outcome = SourceAttemptOutcome.UNSUPPORTED
            cooldown = None
        elif error_code in UNAVAILABLE_SOURCE_ERRORS:
            outcome = SourceAttemptOutcome.UNAVAILABLE
            cooldown = None
        else:
            outcome = SourceAttemptOutcome.UNAVAILABLE
            error_code = SourceErrorCode.SOURCE_UNAVAILABLE
            cooldown = None
        return self._record_attempt(
            run_id,
            subject_key,
            kind,
            outcome,
            max(budget.started_at, now),
            max(budget.started_at, now),
            None,
            error_code,
            cooldown,
            0,
        )

    def _record_attempt(
        self,
        run_id,
        subject_key,
        kind,
        outcome,
        started,
        finished,
        data_as_of,
        error_code,
        cooldown,
        response_bytes,
    ):
        source_id, field_id = source_binding(kind)
        registry = SourceRegistryV1()
        attempt = SourceAttempt(
            source_id=source_id,
            field_id=field_id,
            subject_key=subject_key,
            attempt_number=1,
            outcome=outcome,
            started_at=started,
            finished_at=finished,
            data_as_of=data_as_of,
            error_code=error_code,
            cooldown_until=cooldown,
            force_actor=None,
            force_reason=None,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=response_bytes,
        )
        return self._audit.record_source_attempt(run_id, attempt)

    def _persist_items(self, items: Tuple[NewsItem, ...]) -> Tuple[NewsItem, ...]:
        if not items:
            return ()
        existing = {
            item.item_id: item
            for item in self._store.authenticated_items_by_keys(
                tuple(item.item_id for item in items)
            )
        }
        new_items = tuple(item for item in items if item.item_id not in existing)
        if new_items:
            with self._repository.connect() as connection, connection:
                self._store.save_items(new_items, connection)
        return self._store.authenticated_items_by_keys(
            tuple(item.item_id for item in items)
        )

    def _public_fund_context(self, fund_code: str) -> PublicFundDisclosureContext:
        bundle = self._fund_store.load_bundle(fund_code)
        name = fund_code if bundle.identity is None else bundle.identity.fund_name
        local_date = self._now().astimezone(_SHANGHAI).date()
        benchmark_terms = tuple(
            sorted(
                {
                    item.description
                    for item in bundle.benchmarks
                    if (item.effective_from is None or item.effective_from <= local_date)
                    and (item.effective_to is None or local_date <= item.effective_to)
                }
            )
        )
        periods = tuple(item.report_period for item in bundle.holdings)
        period = max(periods) if periods else None
        security_names = tuple(
            sorted(
                {
                    item.security_name
                    for item in bundle.holdings
                    if period is not None and item.report_period == period
                }
            )
        )
        coverage = (
            "当前没有可用的季度持仓披露"
            if period is None
            else f"仅覆盖{period.isoformat()}报告期披露持仓，不代表实时或完整组合"
        )
        relevance = PublicFundContext(
            fund_code,
            name,
            benchmark_terms,
            security_names,
            (),
            period,
            coverage,
        )
        relevance.validate()
        holdings = tuple(
            item
            for item in bundle.holdings
            if period is not None and item.report_period == period
        )
        published_values = tuple(
            item.published_at.astimezone(timezone.utc)
            for item in holdings
            if item.published_at is not None
        )
        source_ids = {
            item.source_document_id
            for item in holdings
            if item.source_document_id is not None
        }
        retrieved_values = tuple(
            document.retrieved_at.astimezone(timezone.utc)
            for source_id, document in bundle.source_documents.items()
            if source_id in source_ids
        )
        status = bundle.section_statuses.get("quarterly_holdings", {})
        last_success_text = status.get("last_success_at")
        last_success = (
            None
            if last_success_text is None
            else datetime.fromisoformat(str(last_success_text)).astimezone(timezone.utc)
        )
        section_state = str(
            bundle.section_states.get("quarterly_holdings", "missing")
        )
        expected = expected_report_period(local_date)
        freshness = (
            "missing"
            if period is None
            else "fresh"
            if period >= expected and section_state == "success"
            else "stale"
            if period < expected
            else "unknown"
        )
        covered_fields = tuple(
            field
            for field, available in (
                ("identity", bundle.identity is not None),
                ("active_benchmark", bool(benchmark_terms)),
                ("disclosed_holdings", bool(holdings)),
            )
            if available
        )
        missing_context_fields = tuple(
            field
            for field in ("identity", "active_benchmark", "disclosed_holdings")
            if field not in covered_fields
        )
        result = PublicFundDisclosureContext(
            relevance_context=relevance,
            coverage_scope="disclosed_context",
            covered_fields=covered_fields,
            not_covered_fields=(
                *missing_context_fields,
                "decision",
                "fees",
                "formal_nav",
                "manager",
                "portfolio",
            ),
            holdings_section_state=(
                section_state
                if re.fullmatch(r"[a-z][a-z0-9_]*", section_state)
                else "unknown"
            ),
            holdings_freshness=freshness,
            holdings_published_at=max(published_values) if published_values else None,
            holdings_last_success_at=last_success,
            holdings_retrieved_at=max(retrieved_values) if retrieved_values else None,
            source_boundary=(
                "仅声明当前本地披露存储中的身份、有效基准和季度披露持仓；"
                "不覆盖正式净值、经理、费用、个人组合或行动判断"
            ),
            companion_workflows=("decision_route", "fund_brief", "portfolio"),
        )
        result.validate()
        return result

    def _entities(self, batches, fund_context, interval):
        active_from = datetime(2001, 1, 1, tzinfo=timezone.utc)
        values = [MarketEntity("market_cn", "market", "中国公募基金市场", active_from, None, ())]
        if fund_context is not None:
            relevance = fund_context.relevance_context
            values.append(
                MarketEntity(
                    f"fund_{relevance.fund_code}",
                    "fund",
                    relevance.canonical_name,
                    active_from,
                    None,
                    (),
                )
            )
            for kind, names in (
                ("benchmark", relevance.benchmark_terms),
                ("security", relevance.disclosed_security_names),
            ):
                for name in names:
                    values.append(
                        MarketEntity(
                            self._entity_id(kind, name), kind, name, active_from, None, ()
                        )
                    )
        for batch in batches:
            for row in batch.rows:
                values.append(
                    MarketEntity(
                        self._entity_id("sector", row.sector_code),
                        "sector",
                        row.sector_name,
                        active_from,
                        None,
                        (),
                    )
                )
        by_id = {item.entity_id: item for item in values}
        entities = tuple(sorted(by_id.values(), key=lambda item: item.entity_id))
        aliases = ()
        if fund_context is not None:
            relevance = fund_context.relevance_context
            aliases = (
                EntityAlias(
                    f"fund_{relevance.fund_code}",
                    relevance.fund_code,
                    "fund_code",
                    active_from,
                    None,
                    (),
                ),
            )
        return entities, aliases

    def _sector_labels(self, batches) -> Tuple[PublicSectorLabel, ...]:
        labels = {
            self._entity_id("sector", row.sector_code): PublicSectorLabel(
                entity_id=self._entity_id("sector", row.sector_code),
                sector_code=row.sector_code,
                sector_name=row.sector_name,
            )
            for batch in batches
            for row in batch.rows
        }
        values = tuple(sorted(labels.values(), key=lambda item: item.entity_id))
        for value in values:
            value.validate()
        return values

    @staticmethod
    def _entity_id(kind: str, value: str) -> str:
        return f"{kind}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"

    def _empty_market_state(self, as_of: datetime) -> MarketStateSnapshot:
        state = MarketStateSnapshot(
            market_state=MarketShadowState.INSUFFICIENT_DATA,
            sector_states=(),
            dimensions=(),
            supporting_observation_ids=(),
            opposing_observation_ids=(),
            unknown_dimensions=tuple(MarketDimension),
            invalidation_conditions=(
                "该工作流没有发布市场指标事实",
                "空市场状态不构成交易授权",
            ),
            next_review_at=as_of + _CURRENT_CACHE,
            policy_checksum=self._policy.checksum(),
        )
        state.validate()
        return state

    def _thesis_review(self, fund_code, items):
        if fund_code is None:
            return None
        stored = self._repository.latest_active_thesis(fund_code)
        if stored is None:
            return None
        _thesis_id, thesis = stored
        matches = tuple(
            sorted(
                item.item_id
                for item in items
                if thesis.invalidation in f"{item.title} {item.excerpt or ''}"
            )
        )
        result = PublicThesisReview(
            thesis.rationale,
            thesis.horizon,
            thesis.invalidation,
            "possible_invalidation_match" if matches else "no_matching_evidence",
            matches,
        )
        result.validate()
        return result

    def _current_item_uses(self, items, attempt_ids):
        attempts_by_source = {}
        for attempt_id in attempt_ids:
            stored = self._audit.authenticated_source_attempt(attempt_id)
            if stored.attempt.outcome not in {
                SourceAttemptOutcome.SUCCESS,
                SourceAttemptOutcome.CACHE_HIT,
            }:
                continue
            attempts_by_source.setdefault(stored.attempt.source_id, []).append(attempt_id)
        values = []
        for item in items:
            candidates = attempts_by_source.get(item.source_id, ())
            if len(candidates) != 1:
                raise IntelligenceStoreError("current item use source is ambiguous")
            value = AuthenticatedSnapshotItemUse(item.item_id, candidates[0])
            value.validate()
            values.append(value)
        return tuple(sorted(values, key=lambda value: value.item_id))

    def _source_summaries(self, request_run_id, omitted_work):
        endpoints = {
            "eastmoney_market": _eastmoney_industry_url(),
            "gov_cn_policy": _GOV_URL,
            "stcn_fund_news": _STCN_LIST_URL,
        }
        summaries = []
        attempts = self._store.authenticated_terminal_source_attempts(request_run_id)
        for stored in attempts:
            attempt = stored.attempt
            usable = attempt.outcome in {
                SourceAttemptOutcome.SUCCESS,
                SourceAttemptOutcome.CACHE_HIT,
            }
            coverage_gap_codes = tuple(
                sorted(
                    code
                    for code in omitted_work
                    if code == "stcn_detail_cap_reached"
                    and attempt.source_id == "stcn_fund_news"
                    or code.startswith(f"{attempt.source_id}_")
                )
            )
            source_partial = bool(
                coverage_gap_codes
            )
            completeness = (
                "partial" if usable and source_partial else "complete" if usable else "insufficient"
            )
            reason_code = None if attempt.error_code is None else attempt.error_code.value
            retryable = (
                None
                if usable
                else attempt.error_code in TRANSIENT_SOURCE_ERRORS
            )
            supplementation = (
                None
                if usable and not source_partial
                else "请提供带日期的公开来源URL、官方文件或公开截图进行人工补证"
            )
            summary = IntelligenceSourceSummary(
                source_attempt_id=stored.id,
                source_id=attempt.source_id,
                field_id=attempt.field_id,
                outcome=attempt.outcome,
                data_as_of=None,
                retrieved_at=(
                    None
                    if attempt.data_as_of is None
                    else attempt.data_as_of.astimezone(timezone.utc)
                ),
                endpoint=endpoints[attempt.source_id],
                completeness=completeness,
                coverage_gap_codes=coverage_gap_codes,
                reason_code=reason_code,
                retryable=retryable,
                cooldown_until=(
                    None
                    if attempt.cooldown_until is None
                    else attempt.cooldown_until.astimezone(timezone.utc)
                ),
                supplementation=supplementation,
            )
            summary.validate()
            summaries.append(summary)
        return tuple(summaries)

    @staticmethod
    def _inside_interval(items, interval):
        return tuple(
            item
            for item in items
            if item.published_at <= interval.end_at
            and (
                item.publication_interval_end is None
                and item.published_at >= interval.start_at
                or item.publication_interval_end is not None
                and item.publication_interval_end > interval.start_at
            )
        )

    def _interval(self, window, start, end, now):
        if (start is None) != (end is None):
            raise ValueError("explicit intelligence interval requires start and end")
        if start is not None:
            if window != "recent":
                raise ValueError("named and explicit intelligence intervals cannot be mixed")
            if type(start) is not date or type(end) is not date or end < start:
                raise ValueError("explicit intelligence interval is invalid")
            start_at = datetime.combine(start, datetime_time.min, _SHANGHAI).astimezone(
                timezone.utc
            )
            end_at = datetime.combine(
                end + timedelta(days=1), datetime_time.min, _SHANGHAI
            ).astimezone(timezone.utc)
            end_at = min(end_at, now)
        else:
            selected = QueryWindow(window)
            if selected is QueryWindow.TODAY:
                local_now = now.astimezone(_SHANGHAI)
                start_at = datetime.combine(
                    local_now.date(), datetime_time.min, _SHANGHAI
                ).astimezone(timezone.utc)
            elif selected is QueryWindow.RECENT:
                start_at = now - timedelta(hours=72)
            else:
                start_at = now - timedelta(days=7)
            end_at = now
        result = QueryInterval(start_at, end_at, "Asia/Shanghai")
        result.validate()
        return result

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise ValueError("intelligence service clock must return an aware datetime")
        value = value.astimezone(timezone.utc)
        if value.utcoffset() != timedelta(0):
            raise ValueError("intelligence service clock must resolve to UTC")
        return value
