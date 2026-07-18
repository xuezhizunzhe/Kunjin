from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Tuple

from kunjin.brief.d2 import (
    AdjustedReturnSeriesEvidence,
    PortfolioEvidenceBinding,
    build_d2_relationships,
    project_adjusted_return_series_evidence,
)
from kunjin.brief.engine import (
    BriefSourceResolution,
    HeldFundBriefEngine,
    load_brief_source_resolution,
)
from kunjin.brief.facts import (
    AuthenticatedAnnouncementContent,
    SourceLinkedFactSet,
    build_source_linked_facts,
)
from kunjin.brief.models import HeldFundBriefOutcome, HeldFundBriefReport
from kunjin.brief.policy import HeldFundBriefPolicyV1
from kunjin.brief.portfolio import PortfolioObservationResult
from kunjin.brief.public_acceptance_portfolio import (
    SYNTHETIC_OBSERVATION_VERSION,
    PublicAcceptancePortfolioService,
)
from kunjin.brief.research import build_owner_report, build_snapshot
from kunjin.brief.store import (
    HISTORICAL_BRIEF_COMPARISON_UNAVAILABLE,
    BriefStore,
)
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    ActionKind,
    RequestMode,
    RequestTerminalStatus,
    SourceAttemptOutcome,
    SourceFieldRef,
    validate_aware_datetime,
    validate_identifier,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.routing import ActionRouter
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.service import SourceRequestContext
from kunjin.storage.repository import Repository

_OWNER_ACTIONS = frozenset(
    {
        ActionKind.CONTINUE_HOLDING,
        ActionKind.REDUCE_TO_CASH,
        ActionKind.FULL_EXIT,
        ActionKind.SWITCH_FUNDS,
    }
)
_SOURCE_STAGES = (
    "identity_profile",
    "personal_position_observation",
    "formal_nav",
    "manager_fee_profile",
    "holdings_industries",
    "official_announcements",
)
_FINAL_STAGES = (
    "fact_projection",
    "d2_relationships",
    "action_evaluation",
    "brief_publication",
)
_SUCCESSFUL_DISCLOSURE_STATES = frozenset({"success", "not_disclosed"})
_EXPIRY_CANCEL_REASONS = frozenset(
    {"request_deadline_reached", "request_expired", "worker_timeout"}
)


class HeldFundBriefServiceError(RuntimeError):
    """A sanitized failure to produce a held-fund brief."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HeldFundBriefService:
    """Orchestrate one held-fund brief under one request budget."""

    def __init__(
        self,
        *,
        repository: Repository,
        suitability_service: object,
        disclosure_service: object,
        portfolio_service: object,
        nav_service: object,
        audit_store: Optional[DecisionAuditStore] = None,
        brief_store: Optional[BriefStore] = None,
        health_service: Optional[SourceHealthService] = None,
        router: Optional[ActionRouter] = None,
        engine: Optional[HeldFundBriefEngine] = None,
        evidence_policy: Optional[EvidencePolicyV1] = None,
        source_registry: Optional[SourceRegistryV1] = None,
        brief_policy: Optional[HeldFundBriefPolicyV1] = None,
        risk_store: object = None,
        announcement_content_loader: Optional[
            Callable[[object, SourceRequestContext], Tuple[AuthenticatedAnnouncementContent, ...]]
        ] = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if not callable(getattr(suitability_service, "status", None)):
            raise ValueError("suitability service must expose status")
        for dependency, methods, name in (
            (
                disclosure_service,
                ("sync_sections", "sync_holdings"),
                "disclosure service",
            ),
            (portfolio_service, ("sync",), "portfolio service"),
            (
                nav_service,
                ("sync", "validated_adjusted_series"),
                "NAV service",
            ),
        ):
            if any(not callable(getattr(dependency, method, None)) for method in methods):
                raise ValueError(f"{name} does not expose its required methods")
        disclosure_store = getattr(disclosure_service, "store", None)
        if not callable(getattr(disclosure_store, "load_bundle", None)):
            raise ValueError("disclosure service must expose its disclosure store")
        self._validate_dependency_repository(
            repository,
            disclosure_store,
            "disclosure store",
            required=True,
        )
        self._validate_dependency_repository(
            repository,
            portfolio_service,
            "portfolio service",
            required=True,
        )
        self._validate_dependency_repository(
            repository,
            nav_service,
            "NAV service",
            required=True,
        )
        if risk_store is not None:
            self._validate_dependency_repository(
                repository,
                risk_store,
                "risk store",
                attribute="_repository",
                required=True,
            )
        if audit_store is None:
            audit_store = DecisionAuditStore(repository)
        if type(audit_store) is not DecisionAuditStore:
            raise ValueError("audit store must be an exact DecisionAuditStore")
        if audit_store.repository is not repository:
            raise ValueError("audit store must share the brief repository")
        if brief_store is None:
            brief_store = BriefStore(repository, audit_store)
        if type(brief_store) is not BriefStore:
            raise ValueError("brief store must be an exact BriefStore")
        if (
            brief_store.repository is not repository
            or brief_store.decision_store is not audit_store
        ):
            raise ValueError("brief store must share the brief audit store")
        if source_registry is None:
            source_registry = SourceRegistryV1()
        if evidence_policy is None:
            evidence_policy = EvidencePolicyV1()
        if health_service is None:
            health_service = SourceHealthService(
                audit_store,
                source_registry,
                evidence_policy,
                wall_clock=now,
            )
        if type(health_service) is not SourceHealthService:
            raise ValueError("health service must be an exact SourceHealthService")
        if health_service.audit_store is not audit_store:
            raise ValueError("health service must share the brief audit store")
        if router is None:
            router = ActionRouter()
        if type(router) is not ActionRouter:
            raise ValueError("router must be an exact ActionRouter")
        if brief_policy is None:
            brief_policy = HeldFundBriefPolicyV1()
        if engine is None:
            engine = HeldFundBriefEngine(brief_policy)
        if type(engine) is not HeldFundBriefEngine:
            raise ValueError("brief engine must be an exact HeldFundBriefEngine")
        if type(evidence_policy) is not EvidencePolicyV1:
            raise ValueError("evidence policy must be an exact EvidencePolicyV1")
        if type(source_registry) is not SourceRegistryV1:
            raise ValueError("source registry must be an exact SourceRegistryV1")
        if type(brief_policy) is not HeldFundBriefPolicyV1:
            raise ValueError("brief policy must be an exact HeldFundBriefPolicyV1")
        if announcement_content_loader is not None and not callable(announcement_content_loader):
            raise ValueError("announcement content loader must be callable or None")
        if not callable(now) or not callable(monotonic):
            raise ValueError("brief clocks must be callable")
        evidence_policy.validate()
        source_registry.validate()
        brief_policy.validate()
        self._repository = repository
        self._suitability_service = suitability_service
        self._disclosure_service = disclosure_service
        self._disclosure_store = disclosure_store
        self._portfolio_service = portfolio_service
        self._nav_service = nav_service
        self._audit_store = audit_store
        self._brief_store = brief_store
        self._health_service = health_service
        self._router = router
        self._engine = engine
        self._evidence_policy = evidence_policy
        self._source_registry = source_registry
        self._brief_policy = brief_policy
        self._risk_store = risk_store
        self._announcement_content_loader = announcement_content_loader
        self._now = now
        self._monotonic = monotonic

    @staticmethod
    def _validate_dependency_repository(
        repository: Repository,
        dependency: object,
        name: str,
        *,
        attribute: str = "repository",
        required: bool = False,
    ) -> None:
        dependency_repository = getattr(dependency, attribute, None)
        if dependency_repository is None:
            if required:
                raise ValueError(f"{name} must expose repository")
            return
        if (
            not isinstance(dependency_repository, Repository)
            or dependency_repository.database.resolve() != repository.database.resolve()
        ):
            raise ValueError(f"{name} must share the brief repository")

    def brief(
        self,
        fund_code: str,
        *,
        action: ActionKind,
        mode: RequestMode = RequestMode.RAPID,
        latest_expected_data_as_of: Optional[datetime] = None,
    ) -> HeldFundBriefReport:
        return self.brief_outcome(
            fund_code,
            action=action,
            mode=mode,
            latest_expected_data_as_of=latest_expected_data_as_of,
        ).report

    def brief_outcome(
        self,
        fund_code: str,
        *,
        action: ActionKind,
        mode: RequestMode = RequestMode.RAPID,
        latest_expected_data_as_of: Optional[datetime] = None,
    ) -> HeldFundBriefOutcome:
        self._validate_request(fund_code, action, mode, latest_expected_data_as_of)
        budget = RequestBudget.create(
            mode,
            monotonic=self._monotonic,
            wall_clock=self._now,
        )
        request_run_id = self._audit_store.begin_request(budget)
        omitted: list[str] = []
        completed: set[str] = set()
        try:
            suitability_status = self._suitability_status(action, omitted)
            budget.require_publishable()
            route = self._router.route(
                request_id=budget.request_id,
                mode=mode,
                actions=(ActionKind.FACT_RESEARCH, action),
                suitability_status=suitability_status,
            )
            context = SourceRequestContext(
                request_run_id,
                budget,
                self._audit_store,
                self._health_service,
            )

            self._run_source(
                "identity_profile",
                lambda: self._disclosure_service.sync_sections(
                    fund_code,
                    ("basic_profile",),
                    request_context=context,
                ),
                budget,
                completed,
                omitted,
            )
            portfolio_result = self._run_source(
                "personal_position_observation",
                lambda: self._portfolio_service.sync(fund_code, context),
                budget,
                completed,
                omitted,
            )
            portfolio_binding = self._portfolio_binding(
                portfolio_result,
                fund_code,
                context,
                omitted,
            )
            self._run_source(
                "formal_nav",
                lambda: self._nav_service.sync(
                    fund_code,
                    context,
                    latest_expected_data_as_of=latest_expected_data_as_of,
                ),
                budget,
                completed,
                omitted,
            )
            self._run_source(
                "manager_fee_profile",
                lambda: self._disclosure_service.sync_sections(
                    fund_code,
                    ("manager_history", "fee_schedule"),
                    request_context=context,
                ),
                budget,
                completed,
                omitted,
            )
            self._run_source(
                "holdings_industries",
                lambda: self._disclosure_service.sync_holdings(
                    fund_code,
                    request_context=context,
                ),
                budget,
                completed,
                omitted,
            )
            self._run_source(
                "official_announcements",
                lambda: self._disclosure_service.sync_sections(
                    fund_code,
                    ("announcements",),
                    request_context=context,
                ),
                budget,
                completed,
                omitted,
            )

            as_of = self._current_time(budget)
            action_ids = tuple(item.action_id for item in route.actions)
            target_bundle = self._disclosure_store.load_bundle(fund_code)
            announcement_contents = self._announcement_contents(
                target_bundle,
                context,
                omitted,
            )
            fact_sets = self._build_fact_sets(
                fund_code=fund_code,
                portfolio=portfolio_binding,
                target_bundle=target_bundle,
                target_announcement_contents=announcement_contents,
                action_ids=action_ids,
                as_of=as_of,
                omitted=omitted,
            )
            fact_set = fact_sets[fund_code]
            completed.add("fact_projection")
            budget.require_publishable()

            adjusted = self._adjusted_evidence(
                tuple(sorted(fact_sets)),
                context,
                latest_expected_data_as_of,
                omitted,
            )
            d2 = build_d2_relationships(
                fund_code,
                portfolio_binding,
                fact_sets,
                as_of,
                request_id=budget.request_id,
                request_mode=mode,
                adjusted_series_by_fund=(adjusted if adjusted else None),
                decision_audit_store=(self._audit_store if adjusted else None),
            )
            completed.add("d2_relationships")
            budget.require_publishable()

            resolutions = self._source_resolutions(
                request_run_id,
                budget,
                route,
                fact_set,
                d2,
                as_of,
            )
            thesis = self._confirmed_thesis(fund_code, route, omitted)
            evaluation = self._engine.evaluate(
                route=route,
                fact_set=fact_set,
                d2=d2,
                source_resolutions=resolutions,
                confirmed_thesis=thesis,
            )
            completed.add("action_evaluation")
            budget.require_publishable()

            try:
                history_comparable = self._brief_store.latest_history_comparable(fund_code)
            except Exception:
                history_comparable = False
            if not history_comparable:
                self._add_omitted(omitted, HISTORICAL_BRIEF_COMPARISON_UNAVAILABLE)

            terminal_omitted = tuple(dict.fromkeys(omitted))
            terminal_status = (
                RequestTerminalStatus.PARTIAL
                if terminal_omitted
                else RequestTerminalStatus.COMPLETE
            )
            finished_at = self._current_time(budget, not_before=as_of)
            outcomes: list[HeldFundBriefOutcome] = []

            def snapshot_factory(run_id: int, decision_id: int):
                snapshot = build_snapshot(
                    request_run_id=run_id,
                    decision_snapshot_id=decision_id,
                    route=route,
                    fact_set=fact_set,
                    d2=d2,
                    evaluation=evaluation,
                )
                outcome = HeldFundBriefOutcome(
                    build_owner_report(snapshot, d2),
                    terminal_status,
                    terminal_omitted,
                )
                outcome.validate()
                outcomes.append(outcome)
                return snapshot

            self._brief_store.publish(
                request_run_id=request_run_id,
                route=route,
                evidence_policy=self._evidence_policy,
                source_registry=self._source_registry,
                brief_policy=self._brief_policy,
                snapshot_factory=snapshot_factory,
                created_at=as_of,
                finished_at=finished_at,
                status=terminal_status,
                omitted_work=terminal_omitted,
                budget=budget,
            )
            completed.add("brief_publication")
            if len(outcomes) != 1 or type(outcomes[0]) is not HeldFundBriefOutcome:
                raise HeldFundBriefServiceError("brief outcome publication binding failed")
            return outcomes[0]
        except BudgetExpired:
            status = self._budget_terminal_status(budget)
            self._finalize_unpublished(
                request_run_id,
                budget,
                status,
                self._remaining_omissions(completed, omitted),
            )
            raise
        except (KeyboardInterrupt, SystemExit):
            if not budget.cancelled:
                budget.cancel("owner_cancelled")
            self._finalize_unpublished(
                request_run_id,
                budget,
                self._budget_terminal_status(budget),
                self._remaining_omissions(completed, omitted),
            )
            raise
        except Exception:
            self._finalize_unpublished(
                request_run_id,
                budget,
                RequestTerminalStatus.FAILED,
                self._remaining_omissions(completed, omitted),
            )
            raise HeldFundBriefServiceError("held fund brief failed") from None

    @staticmethod
    def _validate_request(
        fund_code: object,
        action: object,
        mode: object,
        latest_expected_data_as_of: object,
    ) -> None:
        if (
            type(fund_code) is not str
            or len(fund_code) != 6
            or not fund_code.isascii()
            or not fund_code.isdigit()
        ):
            raise ValueError("fund code must be exactly six ASCII digits")
        if type(action) is not ActionKind or action not in _OWNER_ACTIONS:
            raise ValueError("brief action is unsupported")
        if type(mode) is not RequestMode:
            raise ValueError("brief mode must be an exact RequestMode")
        if latest_expected_data_as_of is not None:
            validate_aware_datetime(
                latest_expected_data_as_of,
                "latest expected NAV time",
            )

    def _suitability_status(self, action: ActionKind, omitted: list[str]) -> object:
        if action not in {ActionKind.CONTINUE_HOLDING, ActionKind.SWITCH_FUNDS}:
            return None
        try:
            return self._suitability_service.status()
        except Exception:
            self._add_omitted(omitted, "phase_b_status")
            return None

    def _run_source(
        self,
        stage: str,
        operation: Callable[[], object],
        budget: RequestBudget,
        completed: set[str],
        omitted: list[str],
    ) -> object:
        validate_identifier(stage, "brief source stage")
        budget.require_publishable()
        try:
            result = operation()
        except BudgetExpired:
            raise
        except Exception:
            self._add_omitted(omitted, stage)
            budget.require_publishable()
            return None
        budget.require_publishable()
        completed.add(stage)
        self._collect_result_omissions(result, stage, omitted)
        return result

    @classmethod
    def _collect_result_omissions(
        cls,
        result: object,
        stage: str,
        omitted: list[str],
    ) -> None:
        if result is None:
            return
        returned = getattr(result, "omitted_work", ())
        if type(returned) is tuple:
            for item in returned:
                if type(item) is str:
                    cls._add_omitted(omitted, item)
        sections = getattr(result, "sections", None)
        if (
            type(sections) is dict
            and sections
            and any(
                getattr(item, "status", None) not in _SUCCESSFUL_DISCLOSURE_STATES
                for item in sections.values()
            )
        ):
            cls._add_omitted(omitted, stage)
        status = getattr(result, "status", None)
        if stage == "personal_position_observation" and status != "success":
            cls._add_omitted(omitted, stage)

    def _portfolio_binding(
        self,
        result: object,
        fund_code: str,
        context: SourceRequestContext,
        omitted: list[str],
    ) -> PortfolioEvidenceBinding:
        binding = getattr(result, "portfolio_binding", None)
        try:
            if (
                type(result) is not PortfolioObservationResult
                or result.fund_code != fund_code
                or result.status != "success"
            ):
                raise ValueError("portfolio result identity is not a success")
            if type(binding) is not PortfolioEvidenceBinding:
                raise ValueError("portfolio result has no exact evidence binding")
            binding.validate()
            if binding.source_state != "same_request_success":
                raise ValueError("portfolio success is not bound to the current request")
            source_attempt_id = getattr(result, "source_attempt_id", None)
            if type(source_attempt_id) is not int or source_attempt_id <= 0:
                raise ValueError("portfolio result has no exact source attempt")
            stored = context.audit_store.authenticated_source_attempt(source_attempt_id)
            attempt = stored.attempt
            expected_observation_version = (
                SYNTHETIC_OBSERVATION_VERSION
                if type(self._portfolio_service) is PublicAcceptancePortfolioService
                else f"source_attempt_{source_attempt_id}"
            )
            if (
                stored.id != source_attempt_id
                or stored.request_run_id != context.request_run_id
                or stored.request_id != context.budget.request_id
                or attempt.source_id != "yangjibao_portfolio_observation"
                or attempt.field_id != "personal_position_observation"
                or attempt.subject_key != f"fund:{fund_code}"
                or attempt.outcome is not SourceAttemptOutcome.SUCCESS
                or attempt.data_as_of != binding.observed_at
                or binding.observation_version != expected_observation_version
                or not binding.snapshot_complete
                or binding.request_id != context.budget.request_id
                or binding.request_mode is not context.budget.mode
                or binding.request_started_at != context.budget.started_at
                or binding.request_deadline_at != context.budget.deadline_at
            ):
                raise ValueError("portfolio source attempt binding is not authenticated")
            return binding
        except Exception:
            self._add_omitted(omitted, "personal_position_observation")
        budget = context.budget
        fallback = PortfolioEvidenceBinding(
            positions=(),
            snapshot_complete=False,
            observation_version="portfolio_unavailable",
            observed_at=budget.started_at,
            source_state="unbound",
            request_id=None,
            request_mode=None,
            request_started_at=None,
            request_deadline_at=None,
        )
        fallback.validate()
        return fallback

    def _announcement_contents(
        self,
        bundle: object,
        context: SourceRequestContext,
        omitted: list[str],
    ) -> Tuple[AuthenticatedAnnouncementContent, ...]:
        if self._announcement_content_loader is None:
            self._add_omitted(omitted, "official_announcement_content")
            return ()
        try:
            contents = self._announcement_content_loader(bundle, context)
            if type(contents) is not tuple:
                raise ValueError("announcement content loader returned an invalid type")
            for item in contents:
                if type(item) is not AuthenticatedAnnouncementContent:
                    raise ValueError("announcement content loader returned an invalid record")
                item.validate()
            return contents
        except BudgetExpired:
            raise
        except Exception:
            self._add_omitted(omitted, "official_announcement_content")
            context.budget.require_publishable()
            return ()

    def _build_fact_sets(
        self,
        *,
        fund_code: str,
        portfolio: PortfolioEvidenceBinding,
        target_bundle: object,
        target_announcement_contents: Tuple[AuthenticatedAnnouncementContent, ...],
        action_ids: Tuple[str, ...],
        as_of: datetime,
        omitted: list[str],
    ) -> Dict[str, SourceLinkedFactSet]:
        held_codes = tuple(
            sorted({item.fund_code for item in portfolio.positions if item.shares > 0})
        )
        result: Dict[str, SourceLinkedFactSet] = {}
        for code in tuple(sorted(set(held_codes) | {fund_code})):
            try:
                bundle = (
                    target_bundle if code == fund_code else self._disclosure_store.load_bundle(code)
                )
                result[code] = build_source_linked_facts(
                    bundle,
                    as_of,
                    announcement_contents=(
                        target_announcement_contents if code == fund_code else ()
                    ),
                    repository=self._repository,
                    decision_audit_store=self._audit_store,
                    risk_store=self._risk_store,
                    action_ids=action_ids,
                )
            except Exception:
                if code == fund_code:
                    raise
                self._add_omitted(omitted, "peer_fact_projection")
        return result

    def _adjusted_evidence(
        self,
        fund_codes: Tuple[str, ...],
        context: SourceRequestContext,
        latest_expected_data_as_of: Optional[datetime],
        omitted: list[str],
    ) -> Dict[str, AdjustedReturnSeriesEvidence]:
        result: Dict[str, AdjustedReturnSeriesEvidence] = {}
        for code in fund_codes:
            context.budget.require_publishable()
            try:
                series = self._nav_service.validated_adjusted_series(
                    code,
                    context,
                    latest_expected_data_as_of=latest_expected_data_as_of,
                )
                if series is not None:
                    evidence = project_adjusted_return_series_evidence(series)
                    evidence.validate()
                    result[code] = evidence
            except BudgetExpired:
                raise
            except Exception:
                self._add_omitted(omitted, "adjusted_return_series")
                context.budget.require_publishable()
        return result

    def _source_resolutions(
        self,
        request_run_id: int,
        budget: RequestBudget,
        route: object,
        fact_set: SourceLinkedFactSet,
        d2: object,
        as_of: datetime,
    ) -> Tuple[BriefSourceResolution, ...]:
        loader = getattr(self._audit_store, "authenticated_request_source_attempts", None)
        if not callable(loader):
            return ()
        stored_attempts = loader(request_run_id, budget, as_of)
        subject_key = f"fund:{fact_set.fund_code}"
        all_facts = (*fact_set.facts, *d2.evidence_facts)

        def evidence_ids_for(stored, projected_field: str) -> Tuple[str, ...]:
            lineage = f"source_attempt_{stored.id}"
            if projected_field == "official_events":
                return tuple(
                    item.event_id
                    for item in fact_set.official_events
                    if lineage in {item.original_source_id, item.quoted_source_id}
                )
            return tuple(
                fact.fact_id
                for fact in all_facts
                if fact.field_id == projected_field and fact.source_lineage_id == lineage
            )

        def related_source_refs(source_id: str, field_id: str) -> set[SourceFieldRef]:
            return {
                SourceFieldRef(source_id, field_id),
                *self._registry_alternative_refs(source_id, field_id),
            }

        def manual_supplement_ready(source_id: str, field_id: str) -> bool:
            expected_refs = related_source_refs(source_id, field_id)
            latest_by_ref = {}
            for candidate in reversed(stored_attempts):
                candidate_attempt = candidate.attempt
                candidate_ref = SourceFieldRef(
                    candidate_attempt.source_id,
                    candidate_attempt.field_id,
                )
                if (
                    candidate_attempt.subject_key == subject_key
                    and candidate_ref in expected_refs
                    and candidate_ref not in latest_by_ref
                ):
                    latest_by_ref[candidate_ref] = candidate_attempt
            exhausted = {
                SourceAttemptOutcome.UNAVAILABLE,
                SourceAttemptOutcome.UNSUPPORTED,
            }
            return expected_refs == set(latest_by_ref) and all(
                attempt.outcome in exhausted for attempt in latest_by_ref.values()
            )

        requirements = dict(self._brief_policy.fact_requirements)
        resolutions = []
        seen = set()
        for stored in reversed(stored_attempts):
            attempt = stored.attempt
            if attempt.subject_key != subject_key:
                continue
            field_id = attempt.field_id
            projected_field = (
                "official_events" if field_id == "fund_manager_product_announcement" else field_id
            )
            evidence_ids = evidence_ids_for(stored, projected_field)
            if projected_field == "official_events":
                if attempt.source_id != "fund_manager_official_documents":
                    continue
            elif attempt.outcome not in {
                SourceAttemptOutcome.SUCCESS,
                SourceAttemptOutcome.CACHE_HIT,
            }:
                alternatives = related_source_refs(attempt.source_id, attempt.field_id)
                if any(
                    candidate.attempt.subject_key == subject_key
                    and SourceFieldRef(
                        candidate.attempt.source_id,
                        candidate.attempt.field_id,
                    )
                    in alternatives
                    and candidate.attempt.outcome
                    in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
                    and evidence_ids_for(candidate, projected_field)
                    for candidate in stored_attempts
                ):
                    continue
            for action_route in route.actions:
                action_id = action_route.action_id
                if action_id != "fact_research" and projected_field not in requirements.get(
                    action_id, ()
                ):
                    continue
                if (action_id, projected_field) in seen:
                    continue
                if (
                    attempt.outcome
                    in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
                    and projected_field != "official_events"
                    and not evidence_ids
                ):
                    continue
                acceptable_alternative_ids: Tuple[str, ...] = ()
                if attempt.outcome in {
                    SourceAttemptOutcome.UNAVAILABLE,
                    SourceAttemptOutcome.UNSUPPORTED,
                }:
                    acceptable_alternative_ids = tuple(
                        sorted(
                            {
                                reference.source_id
                                for reference in self._registry_alternative_refs(
                                    attempt.source_id,
                                    attempt.field_id,
                                )
                            }
                        )
                    )
                try:
                    resolution = load_brief_source_resolution(
                        self._audit_store,
                        stored.id,
                        action_id=action_id,
                        field_id=projected_field,
                        evidence_ids=evidence_ids,
                        acceptable_alternative_ids=acceptable_alternative_ids,
                        manual_supplement_ready=manual_supplement_ready(
                            attempt.source_id,
                            attempt.field_id,
                        ),
                    )
                except ValueError:
                    continue
                resolutions.append(resolution)
                seen.add((action_id, projected_field))
        return tuple(resolutions)

    def _registry_alternative_refs(
        self,
        source_id: str,
        field_id: str,
    ) -> Tuple[SourceFieldRef, ...]:
        for source in self._source_registry.sources:
            if source.source_id != source_id:
                continue
            for field in source.fields:
                if field.field_id == field_id:
                    return field.acceptable_alternatives
        return ()

    @staticmethod
    def _budget_terminal_status(budget: RequestBudget) -> RequestTerminalStatus:
        if budget.cancel_reason in _EXPIRY_CANCEL_REASONS or not budget.cancelled:
            return RequestTerminalStatus.EXPIRED
        return RequestTerminalStatus.CANCELLED

    def _confirmed_thesis(
        self,
        fund_code: str,
        route: object,
        omitted: list[str],
    ) -> None:
        if not any(item.action_id == "continue_holding" for item in route.actions):
            return None
        loader = getattr(self._repository, "latest_active_thesis", None)
        if not callable(loader):
            return None
        active = loader(fund_code)
        if active is None:
            return None
        self._add_omitted(omitted, "thesis_review")
        return None

    def _current_time(
        self,
        budget: RequestBudget,
        *,
        not_before: Optional[datetime] = None,
    ) -> datetime:
        budget.require_publishable()
        try:
            current = validate_aware_datetime(self._now(), "brief clock").astimezone(timezone.utc)
        except Exception:
            raise HeldFundBriefServiceError("brief clock failed") from None
        if not_before is not None:
            current = max(current, not_before)
        if not budget.started_at <= current <= budget.deadline_at:
            raise BudgetExpired("brief wall clock is outside the request lifetime")
        return current

    @classmethod
    def _remaining_omissions(
        cls,
        completed: set[str],
        omitted: list[str],
    ) -> Tuple[str, ...]:
        values = list(omitted)
        for stage in (*_SOURCE_STAGES, *_FINAL_STAGES):
            if stage not in completed:
                cls._add_omitted(values, stage)
        return tuple(values)

    def _finalize_unpublished(
        self,
        request_run_id: int,
        budget: RequestBudget,
        status: RequestTerminalStatus,
        omitted: Tuple[str, ...],
    ) -> None:
        if status is RequestTerminalStatus.EXPIRED:
            finished_at = budget.deadline_at
        else:
            try:
                finished_at = validate_aware_datetime(
                    self._now(),
                    "brief terminal clock",
                ).astimezone(timezone.utc)
            except Exception:
                finished_at = budget.started_at
            finished_at = min(
                max(finished_at, budget.started_at),
                budget.deadline_at,
            )
        try:
            self._audit_store.finalize_request(
                request_run_id,
                status,
                finished_at,
                omitted,
            )
        except Exception:
            raise HeldFundBriefServiceError("brief request finalization failed") from None

    @staticmethod
    def _add_omitted(target: list[str], value: str) -> None:
        validate_identifier(value, "brief omitted work")
        if value not in target:
            target.append(value)
