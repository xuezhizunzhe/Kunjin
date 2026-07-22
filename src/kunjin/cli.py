from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from kunjin import __version__
from kunjin.adapters.eastmoney import (
    EastmoneyFundClient,
    EastmoneyMarketClient,
    PublicDataError,
)
from kunjin.adapters.yangjibao import YangjibaoClient, YangjibaoError
from kunjin.allocation.crypto import AllocationCipher
from kunjin.allocation.models import (
    AllocationBlockCode,
    AllocationConstraintCode,
    AllocationProfileConflictCode,
)
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.allocation.service import (
    AllocationCalculationError,
    AllocationPolicyError,
    AllocationService,
    EncryptedProfileUnavailableError,
)
from kunjin.allocation.store import AllocationAssessmentStore, AllocationPolicyStore
from kunjin.analytics.portfolio import analyze_portfolio
from kunjin.analytics.research import analyze_fund_history, analyze_sectors
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    TRANSIENT_SOURCE_ERRORS,
    UNSUPPORTED_SOURCE_ERRORS,
    ActionKind,
    ActionMaturity,
    ActionRoute,
    ActionState,
    ConclusionEvidence,
    DecisionRoute,
    EvidenceCompleteness,
    EvidenceFreshness,
    ForceReasonCode,
    FreshnessContext,
    RequestFieldResolution,
    RequestMode,
    RequestTerminalStatus,
    RiskEffect,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldRef,
    SourceFieldState,
    SourceTier,
    WorkflowLevel,
    validate_identifier,
    validate_public_text,
    validate_version,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.service import DecisionRoutingService
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.diagnosis.research import public_diagnosis_payload
from kunjin.diagnosis.service import DiagnosisService
from kunjin.fund_candidates import build_fund_candidate_review
from kunjin.fund_review import (
    build_fund_review,
    build_portfolio_weight_context,
    build_related_fund_context,
)
from kunjin.funds.peers.analytics import PEER_CALCULATION_VERSION
from kunjin.funds.peers.research import (
    build_explicit_compare_report,
    build_peer_report,
    build_portfolio_overlap_report,
    comparison_fingerprint,
)
from kunjin.funds.peers.service import PeerResearchService
from kunjin.funds.peers.store import PeerStore
from kunjin.funds.research import build_disclosure_report
from kunjin.funds.risk.documents import (
    OfficialDocumentClient,
    OfficialDocumentDiscovery,
    OfficialHtmlIndexClient,
)
from kunjin.funds.risk.legacy_doc import ConverterStatus, DockerLegacyDocConverter
from kunjin.funds.risk.policy import ClassificationPolicyV1
from kunjin.funds.risk.research import build_authenticated_risk_research_report
from kunjin.funds.risk.service import (
    DocumentSelectionItem,
    DocumentSyncItem,
    DocumentSyncResult,
    FundRiskService,
    RiskServiceError,
    validate_public_risk_string,
)
from kunjin.funds.risk.store import FundRiskStore
from kunjin.funds.service import (
    HOLDING_SECTIONS,
    PROFILE_SECTIONS,
    SECTION_SPECS,
    FundDisclosureService,
    FundDisclosureSyncInterrupted,
    SourceRequestContext,
)
from kunjin.funds.sources import FundTextClient
from kunjin.funds.store import FundDisclosureStore
from kunjin.holding_review.models import (
    AdjudicationDecision,
    ExitReason,
    RemainderIntent,
    UseOfProceeds,
)
from kunjin.holding_review.research import public_holding_review_payload
from kunjin.holding_review.service import HoldingReviewServiceError
from kunjin.holding_review.thesis import ThesisReviewError
from kunjin.investor_guardrails import build_investor_guardrails
from kunjin.ledger.alipay import AlipayPaymentParser, requires_confirmation
from kunjin.ledger.ocr import OcrError, VisionOcrClient
from kunjin.ledger.reconcile import reconcile_fund
from kunjin.ledger.service import LedgerImportError, LedgerService
from kunjin.ledger.store import LedgerStateError, LedgerStore
from kunjin.logging import redact_secrets
from kunjin.models import InvestmentThesis
from kunjin.paths import RuntimePaths
from kunjin.portfolio_review import ManualPortfolioPosition, PortfolioReviewService
from kunjin.public_research.events import (
    build_persisted_event_timeline,
    persist_verified_event,
)
from kunjin.public_research.evidence import (
    build_persisted_timeline,
    build_refresh_plan,
    persist_verified_evidence,
)
from kunjin.public_research.panorama import build_cross_domain_panorama
from kunjin.public_research.scan import scan_public_research
from kunjin.public_research.summary import summarize_public_research
from kunjin.public_research.supplement import (
    build_supplement_timeline,
    summarize_user_supplied_evidence,
)
from kunjin.review_triggers import build_review_triggers
from kunjin.security.keychain import CredentialStoreError, KeychainTokenStore
from kunjin.selection.readiness import (
    ShortlistReadinessService,
    public_shortlist_readiness_payload,
)
from kunjin.selection.research import public_shortlist_payload
from kunjin.selection.scope import (
    PRODUCT_CATEGORIES,
    RESEARCH_HORIZONS,
    RESEARCH_OBJECTIVES,
    ResearchScopeService,
    public_research_scope_payload,
)
from kunjin.selection.service import ShortlistService
from kunjin.services.research import ResearchSyncService
from kunjin.services.sync import PortfolioSyncService, SyncError
from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import (
    AssessmentCipher,
    ProfileCipher,
    ProfileCryptoError,
    ProfileKeyStore,
)
from kunjin.suitability.editor import ProfileEditor
from kunjin.suitability.models import BlockReason, ConstraintReason
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.service import (
    ProfileService,
    SuitabilityAssessmentError,
    SuitabilityPolicyError,
    SuitabilityService,
)
from kunjin.suitability.store import (
    ProfileStore,
    SuitabilityAssessmentStore,
    SuitabilityPolicyStore,
)

if TYPE_CHECKING:
    from kunjin.brief.service import HeldFundBriefService
    from kunjin.holding_review.service import HoldingReviewService
    from kunjin.holding_review.thesis import ThesisReviewService
    from kunjin.intelligence.service import IntelligenceService

_FUND_CODE = re.compile(r"^[0-9]{6}$")
_COMMAND_PART = re.compile(r"^[a-z][a-z0-9_-]*$")
_TOP_LEVEL_COMMANDS = {
    "allocation",
    "auth",
    "decision",
    "fund",
    "investor",
    "ledger",
    "market",
    "news",
    "portfolio",
    "profile",
    "research",
    "report",
    "status",
    "source",
    "suitability",
    "sync",
    "thesis",
    "version",
}
_ALLOCATION_FORBIDDEN_TEXT = (
    "exact",
    "amount",
    "cny",
    "name",
    "private",
    "encrypted",
    "nonce",
    "ciphertext",
    "fingerprint",
    "target",
    "recommended",
    "purchase",
    "selected",
)
_ALLOCATION_APPROVED_SENSITIVE_KEYS = {"loss_amount_equity_ceiling"}
_ALLOCATION_INEQUALITIES = (
    "E+B+C=1",
    "E>=0",
    "B>=0",
    "C>=0",
    "0.50E+0.10B<=D",
    "I(0.50E+0.10B)<=L",
    "E<=weighted_horizon_ceiling",
    "E<=behavioral_willingness_ceiling",
    "E<=financial_stability_ceiling",
)
_ALLOCATION_PUBLIC_ERROR_MESSAGES = {
    "allocation_policy_unavailable": "allocation policy is unavailable",
    "allocation_calculation_failed": "allocation calculation failed",
    "encrypted_profile_unavailable": "encrypted profile is unavailable",
}
_RISK_TECHNICAL_CODES = frozenset(
    {
        "official_document_unavailable",
        "official_document_invalid",
        "official_document_resource_limit",
        "official_document_parse_failed",
        "classification_policy_unavailable",
        "classification_calculation_failed",
        "classification_storage_failed",
    }
)
_RISK_PUBLIC_ERROR_MESSAGES = {
    "official_document_unavailable": "official fund document is unavailable",
    "official_document_invalid": "official fund document is invalid",
    "official_document_resource_limit": "official fund document exceeded resource limits",
    "official_document_parse_failed": "official fund document parsing failed",
    "classification_policy_unavailable": "classification policy is unavailable",
    "classification_calculation_failed": "fund classification calculation failed",
    "classification_storage_failed": "fund classification storage failed",
}
_RISK_PRIVATE_OUTPUT_KEYS = frozenset(
    {
        "managed_path",
        "managed_artifact_path",
        "artifact_path",
        "local_path",
        "raw_body",
        "raw_response_body",
        "response_body",
        "parser_exception",
        "parser_exception_chain",
        "exception_chain",
        "embedded_file_metadata",
        "embedded_metadata",
        "monthly_net_income",
        "monthly_essential_expenses",
        "monthly_required_debt_service",
        "emergency_reserve",
        "maximum_tolerable_loss",
        "goal_amount",
        "target_amount",
        "amount",
        "purchase_amount",
        "target",
        "target_weight",
        "direction",
        "trade_direction",
        "recommendation",
        "buy",
        "sell",
        "score",
        "universal_score",
        "obligation_amount",
        "profile_key",
        "ciphertext",
        "nonce",
    }
)


class CliUsageError(ValueError):
    code = "invalid_arguments"


class InvalidFundCodeError(ValueError):
    code = "invalid_fund_code"


class InvalidReportPeriodError(ValueError):
    code = "invalid_report_period"


class BoundedDisclosureSyncError(ValueError):
    code = "fund_disclosure_sync_failed"

    def __init__(self, request_metadata: Dict[str, Any]) -> None:
        self.request_metadata = request_metadata
        super().__init__("bounded fund disclosure synchronization failed")


class DecisionCliError(ValueError):
    code = "decision_command_failed"


class SourceStatusCliError(ValueError):
    code = "source_status_failed"


class FundBriefCliError(ValueError):
    code = "fund_brief_failed"


class FundResearchScopeCliError(ValueError):
    code = "fund_research_scope_failed"


class FundShortlistReadinessCliError(ValueError):
    code = "fund_shortlist_readiness_failed"


class KunjinArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _positive_cli_id(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ID must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("ID must be a positive integer")
    return parsed


def _request_finish_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ApplicationContext:
    paths: RuntimePaths
    repository: Repository
    token_store: KeychainTokenStore
    client: YangjibaoClient
    sync_service: PortfolioSyncService
    research_service: ResearchSyncService
    ledger_service: LedgerService
    fund_disclosure_store: Optional[FundDisclosureStore] = None
    fund_disclosure_service: Optional[FundDisclosureService] = None
    peer_store: Optional[PeerStore] = None
    peer_service: Optional[PeerResearchService] = None
    profile_service: Optional[ProfileService] = None
    suitability_service: Optional[SuitabilityService] = None
    allocation_service: Optional[AllocationService] = None
    fund_risk_store: Optional[FundRiskStore] = None
    fund_risk_service: Optional[FundRiskService] = None
    decision_service: Optional[DecisionRoutingService] = None
    source_health_service: Optional[SourceHealthService] = None
    brief_service: Optional["HeldFundBriefService"] = None
    intelligence_service: Optional["IntelligenceService"] = None
    diagnosis_service: Optional[DiagnosisService] = None
    selection_service: Optional[ShortlistService] = None
    research_scope_service: Optional[ResearchScopeService] = None
    shortlist_readiness_service: Optional[ShortlistReadinessService] = None
    thesis_review_service: Optional["ThesisReviewService"] = None
    holding_review_service: Optional["HoldingReviewService"] = None


def build_context(*, public_acceptance_subject: Optional[str] = None) -> ApplicationContext:
    from kunjin.brief.nav import BoundedNavService
    from kunjin.brief.portfolio import BoundedPortfolioService
    from kunjin.brief.public_acceptance_portfolio import (
        build_public_acceptance_portfolio_service,
        load_public_acceptance_capability,
    )
    from kunjin.brief.service import HeldFundBriefService
    from kunjin.holding_review.deep import DeepOfficialConfirmationService
    from kunjin.holding_review.official import (
        OfficialAnnouncementHttpFetcher,
        OfficialListingHttpFetcher,
    )
    from kunjin.holding_review.service import HoldingReviewService
    from kunjin.holding_review.store import HoldingReviewStore
    from kunjin.holding_review.thesis import ThesisReviewService
    from kunjin.intelligence.service import IntelligenceService
    from kunjin.intelligence.store import IntelligenceStore

    paths = RuntimePaths.from_environment()
    public_acceptance = load_public_acceptance_capability(
        paths,
        public_acceptance_subject,
    )
    paths.ensure()
    repository = Repository(paths.database)
    repository.migrate()
    token_store = KeychainTokenStore()
    client = YangjibaoClient(token_store)
    research_service = ResearchSyncService(
        repository,
        EastmoneyFundClient(),
        EastmoneyMarketClient(),
    )
    ledger_store = LedgerStore(repository)
    fund_disclosure_store = FundDisclosureStore(repository)
    fund_text_client = FundTextClient()
    fund_disclosure_service = FundDisclosureService(fund_text_client, fund_disclosure_store)
    fund_risk_store = FundRiskStore(repository)
    legacy_converter = DockerLegacyDocConverter(
        image_id=os.environ.get("KUNJIN_LEGACY_DOC_IMAGE_ID"),
        runtime_paths=paths,
    )
    official_index_client = OfficialHtmlIndexClient()
    peer_store = PeerStore(repository)
    profile_store = ProfileStore(repository)
    profile_key_store = ProfileKeyStore()
    profile_service = ProfileService(
        profile_store,
        ProfileCipher(profile_key_store),
    )
    suitability_service = SuitabilityService(
        profile_service,
        SuitabilityPolicyStore(repository),
        SuitabilityAssessmentStore(repository),
        AssessmentCipher(profile_key_store),
        SuitabilityPolicyV1(),
    )
    allocation_service = AllocationService(
        suitability_service,
        AllocationPolicyStore(repository),
        AllocationAssessmentStore(repository),
        AllocationCipher(profile_key_store),
        AllocationPolicyV1(),
    )
    fund_risk_service = FundRiskService(
        risk_store=fund_risk_store,
        disclosure_store=fund_disclosure_store,
        repository=repository,
        discovery=OfficialDocumentDiscovery(client=official_index_client),
        document_client=OfficialDocumentClient(paths=paths),
        legacy_converter=legacy_converter,
        policy=ClassificationPolicyV1(),
    )
    decision_audit_store = DecisionAuditStore(repository)
    evidence_policy = EvidencePolicyV1()
    source_registry = SourceRegistryV1()
    sync_service = PortfolioSyncService(client, repository)
    source_health_service = SourceHealthService(
        decision_audit_store,
        registry=source_registry,
        policy=evidence_policy,
    )
    portfolio_service = (
        BoundedPortfolioService(
            repository,
            sync_service=sync_service,
        )
        if public_acceptance is None
        else build_public_acceptance_portfolio_service(
            repository,
            sync_service,
            public_acceptance,
        )
    )
    holding_review_store = HoldingReviewStore(repository)
    deep_official_confirmation_service = DeepOfficialConfirmationService(
        disclosure_store=fund_disclosure_store,
        audit_store=decision_audit_store,
        review_store=holding_review_store,
        listing_fetch_factory=lambda deadline_at: OfficialListingHttpFetcher(
            deadline_at=deadline_at,
        ),
        announcement_fetch_factory=lambda rows, deadline_at: (
            OfficialAnnouncementHttpFetcher(
                rows=rows,
                deadline_at=deadline_at,
            )
        ),
    )
    brief_service = HeldFundBriefService(
        repository=repository,
        suitability_service=suitability_service,
        disclosure_service=fund_disclosure_service,
        portfolio_service=portfolio_service,
        nav_service=BoundedNavService(repository),
        audit_store=decision_audit_store,
        health_service=source_health_service,
        evidence_policy=evidence_policy,
        source_registry=source_registry,
        risk_store=fund_risk_store,
        deep_official_confirmation_service=deep_official_confirmation_service,
    )
    intelligence_store = IntelligenceStore(repository, decision_audit_store)
    return ApplicationContext(
        paths=paths,
        repository=repository,
        token_store=token_store,
        client=client,
        sync_service=sync_service,
        research_service=research_service,
        ledger_service=LedgerService(
            paths=paths,
            store=ledger_store,
            ocr_client=VisionOcrClient(),
            parser=AlipayPaymentParser(),
        ),
        fund_disclosure_store=fund_disclosure_store,
        fund_disclosure_service=fund_disclosure_service,
        peer_store=peer_store,
        peer_service=PeerResearchService(
            fund_text_client,
            fund_disclosure_service,
            fund_disclosure_store,
            research_service,
            repository,
            peer_store,
        ),
        profile_service=profile_service,
        suitability_service=suitability_service,
        allocation_service=allocation_service,
        fund_risk_store=fund_risk_store,
        fund_risk_service=fund_risk_service,
        decision_service=DecisionRoutingService(
            suitability_service,
            decision_audit_store,
            policy=evidence_policy,
            registry=source_registry,
        ),
        source_health_service=source_health_service,
        brief_service=brief_service,
        intelligence_service=IntelligenceService(
            repository,
            decision_audit_store,
            intelligence_store,
            source_health_service,
            fund_disclosure_store,
        ),
        diagnosis_service=DiagnosisService(repository, fund_disclosure_store),
        selection_service=ShortlistService(
            repository,
            fund_disclosure_store,
            classification_loader=fund_risk_service.current_classification,
            suitability_status_loader=suitability_service.status,
            allocation_status_loader=allocation_service.status,
        ),
        research_scope_service=ResearchScopeService(
            suitability_status_loader=suitability_service.status,
            allocation_status_loader=allocation_service.status,
        ),
        shortlist_readiness_service=ShortlistReadinessService(
            repository,
            fund_disclosure_store,
            source_health_service=source_health_service,
            classification_loader=fund_risk_service.current_classification,
            suitability_status_loader=suitability_service.status,
            allocation_status_loader=allocation_service.status,
        ),
        thesis_review_service=ThesisReviewService(
            repository,
            holding_review_store=holding_review_store,
        ),
        holding_review_service=HoldingReviewService(
            repository,
            holding_review_store=holding_review_store,
            intelligence_store=intelligence_store,
        ),
    )


def envelope(
    command: str,
    data: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[str]] = None,
    errors: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": "1",
        "command": command,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
    }


def serialize(value: Any) -> Any:
    if is_dataclass(value):
        return serialize(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    return value


def freshness(finished_at: Optional[str], now: Optional[datetime] = None) -> str:
    if not finished_at:
        return "missing"
    shanghai = ZoneInfo("Asia/Shanghai")
    current = (now or datetime.now(timezone.utc)).astimezone(shanghai)
    synced = datetime.fromisoformat(finished_at).astimezone(shanghai)
    deadline_date = synced.date() + timedelta(days=1)
    while deadline_date.weekday() >= 5:
        deadline_date += timedelta(days=1)
    deadline = datetime.combine(deadline_date, time(16, 30), tzinfo=shanghai)
    return "fresh" if current <= deadline else "stale"


def parse_field_overrides(values: Sequence[str]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for value in values:
        name, separator, field_value = value.partition("=")
        if not separator or not name.strip():
            raise ValueError("field overrides must use NAME=VALUE")
        result[name.strip()] = field_value.strip()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = KunjinArgumentParser(prog="kunjin", description="KunJin fund research CLI")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version")
    subparsers.add_parser("status")

    decision = subparsers.add_parser("decision")
    decision_subparsers = decision.add_subparsers(
        dest="decision_command", required=True
    )
    decision_route = decision_subparsers.add_parser("route")
    decision_route.add_argument(
        "--mode",
        choices=[item.value for item in RequestMode],
        default=RequestMode.RAPID.value,
    )
    decision_route.add_argument(
        "--action",
        action="append",
        choices=[item.value for item in ActionKind],
        required=True,
    )

    source = subparsers.add_parser("source")
    source_subparsers = source.add_subparsers(dest="source_command", required=True)
    source_status = source_subparsers.add_parser("status")
    source_status.add_argument("--fund-code")

    profile = subparsers.add_parser("profile")
    profile_subparsers = profile.add_subparsers(dest="profile_command", required=True)
    profile_subparsers.add_parser("edit")
    profile_subparsers.add_parser("status")
    profile_subparsers.add_parser("history")

    suitability = subparsers.add_parser("suitability")
    suitability_subparsers = suitability.add_subparsers(dest="suitability_command", required=True)
    suitability_subparsers.add_parser("assess")
    suitability_subparsers.add_parser("status")
    suitability_subparsers.add_parser("history")

    allocation = subparsers.add_parser("allocation")
    allocation_subparsers = allocation.add_subparsers(dest="allocation_command", required=True)
    allocation_subparsers.add_parser("ranges")
    allocation_subparsers.add_parser("status")
    allocation_subparsers.add_parser("history")
    allocation_subparsers.add_parser("policy")

    auth = subparsers.add_parser("auth")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_subparsers.add_parser("status")
    login = auth_subparsers.add_parser("login")
    login.add_argument("provider", choices=["yangjibao"])
    revoke = auth_subparsers.add_parser("revoke")
    revoke.add_argument("provider", choices=["yangjibao"])

    sync = subparsers.add_parser("sync")
    sync_subparsers = sync.add_subparsers(dest="sync_command", required=True)
    sync_subparsers.add_parser("portfolio")
    sync_fund = sync_subparsers.add_parser("fund")
    sync_fund.add_argument("fund_code")
    sync_fund_profile = sync_subparsers.add_parser("fund-profile")
    sync_fund_profile.add_argument("fund_code")
    sync_fund_holdings = sync_subparsers.add_parser("fund-holdings")
    sync_fund_holdings.add_argument("fund_code")
    for bounded_sync in (sync_fund_profile, sync_fund_holdings):
        bounded_sync.add_argument(
            "--mode",
            choices=[item.value for item in RequestMode],
            default=RequestMode.RAPID.value,
        )
        bounded_sync.add_argument("--force", action="store_true")
        bounded_sync.add_argument(
            "--force-reason",
            choices=[item.value for item in ForceReasonCode],
        )
    sync_fund_peers = sync_subparsers.add_parser("fund-peers")
    sync_fund_peers.add_argument("fund_code")
    sync_fund_peers.add_argument("--candidate", action="append", default=[])
    sync_fund_documents = sync_subparsers.add_parser("fund-documents")
    sync_fund_documents.add_argument("fund_code")
    sync_subparsers.add_parser("market")
    sync_subparsers.add_parser("daily")

    portfolio = subparsers.add_parser("portfolio")
    portfolio_subparsers = portfolio.add_subparsers(dest="portfolio_command", required=True)
    portfolio_subparsers.add_parser("show")
    portfolio_subparsers.add_parser("analyze")
    portfolio_subparsers.add_parser("overlap")
    portfolio_diagnose = portfolio_subparsers.add_parser("diagnose")
    portfolio_diagnose.add_argument("--candidate")
    portfolio_review = portfolio_subparsers.add_parser("review")
    portfolio_review.add_argument("--manual-position", action="append", default=[])

    fund = subparsers.add_parser("fund")
    fund_subparsers = fund.add_subparsers(dest="fund_command", required=True)
    fund_brief = fund_subparsers.add_parser("brief")
    fund_brief.add_argument("fund_code")
    fund_brief.add_argument(
        "--action",
        choices=[
            ActionKind.CONTINUE_HOLDING.value,
            ActionKind.REDUCE_TO_CASH.value,
            ActionKind.FULL_EXIT.value,
            ActionKind.SWITCH_FUNDS.value,
        ],
        required=True,
    )
    fund_brief.add_argument(
        "--mode",
        choices=[item.value for item in RequestMode],
        default=RequestMode.RAPID.value,
    )
    fund_research = fund_subparsers.add_parser("research")
    fund_research.add_argument("fund_code")
    fund_profile = fund_subparsers.add_parser("profile")
    fund_profile.add_argument("fund_code")
    fund_fees = fund_subparsers.add_parser("fees")
    fund_fees.add_argument("fund_code")
    fund_holdings = fund_subparsers.add_parser("holdings")
    fund_holdings.add_argument("fund_code")
    fund_holdings.add_argument("--period")
    fund_announcements = fund_subparsers.add_parser("announcements")
    fund_announcements.add_argument("fund_code")
    fund_peers = fund_subparsers.add_parser("peers")
    fund_peers.add_argument("fund_code")
    fund_compare = fund_subparsers.add_parser("compare")
    fund_compare.add_argument("fund_codes", nargs="+")
    fund_candidates = fund_subparsers.add_parser("candidates")
    fund_candidates.add_argument("fund_codes", nargs="+")
    fund_candidates.add_argument("--emergency-fund", choices=["yes", "no"])
    fund_candidates.add_argument("--near-term-use", choices=["yes", "no"])
    fund_candidates.add_argument("--horizon", choices=["short", "medium", "long"])
    fund_candidates.add_argument("--volatility", choices=["low", "medium", "high"])
    fund_shortlist = fund_subparsers.add_parser("shortlist")
    fund_shortlist.add_argument("fund_codes", nargs="+")
    fund_research_scope = fund_subparsers.add_parser("research-scope")
    fund_research_scope.add_argument("--objective", choices=RESEARCH_OBJECTIVES)
    fund_research_scope.add_argument("--horizon", choices=RESEARCH_HORIZONS)
    fund_research_scope.add_argument(
        "--product-category",
        choices=PRODUCT_CATEGORIES,
    )
    fund_readiness = fund_subparsers.add_parser("shortlist-readiness")
    fund_readiness.add_argument("fund_codes", nargs="+")
    fund_classify = fund_subparsers.add_parser("classify")
    fund_classify.add_argument("fund_code")
    fund_classification = fund_subparsers.add_parser("classification")
    fund_classification.add_argument("fund_code")
    fund_classification_history = fund_subparsers.add_parser("classification-history")
    fund_classification_history.add_argument("fund_code")
    fund_classification_evidence = fund_subparsers.add_parser("classification-evidence")
    fund_classification_evidence.add_argument("fund_code")
    fund_subparsers.add_parser("classification-policy")
    fund_subparsers.add_parser("converter-status")
    fund_intelligence = fund_subparsers.add_parser("intelligence")
    fund_intelligence.add_argument("fund_code")
    _add_intelligence_arguments(fund_intelligence)
    fund_review = fund_subparsers.add_parser("review")
    fund_review.add_argument("fund_code")
    fund_review.add_argument(
        "--action",
        choices=[
            ActionKind.CONTINUE_HOLDING.value,
            ActionKind.REDUCE_TO_CASH.value,
            ActionKind.FULL_EXIT.value,
        ],
        default=ActionKind.CONTINUE_HOLDING.value,
    )
    fund_review.add_argument("--horizon", choices=["short", "medium", "long"])
    fund_review.add_argument("--risk-tolerance", choices=["low", "medium", "high"])
    fund_review.add_argument("--near-term-use", choices=["yes", "no"])
    fund_review.add_argument("--emergency-fund", choices=["yes", "no"])
    fund_review.add_argument("--portfolio-context", choices=["none", "cached"], default="none")
    fund_review.add_argument("--manual-position", action="append", default=[])
    fund_review.add_argument("--related-fund", action="append", default=[])
    fund_review_triggers = fund_subparsers.add_parser("review-triggers")
    fund_review_triggers.add_argument("fund_code")

    investor = subparsers.add_parser("investor")
    investor_subparsers = investor.add_subparsers(dest="investor_command", required=True)
    guardrails = investor_subparsers.add_parser("guardrails")
    guardrails.add_argument("--emergency-fund", choices=["yes", "no"])
    guardrails.add_argument("--near-term-use", choices=["yes", "no"])
    guardrails.add_argument("--horizon", choices=["short", "medium", "long"])
    guardrails.add_argument("--volatility", choices=["low", "medium", "high"])
    guardrails.add_argument("--portfolio-context", choices=["none", "cached"], default="none")
    holding_review = fund_subparsers.add_parser("holding-review")
    holding_review.add_argument("fund_code")
    holding_review.add_argument(
        "--action",
        choices=[
            ActionKind.CONTINUE_HOLDING.value,
            ActionKind.REDUCE_TO_CASH.value,
            ActionKind.FULL_EXIT.value,
        ],
        required=True,
    )
    holding_review.add_argument(
        "--brief-request-run-id", type=_positive_cli_id, required=True
    )
    holding_review.add_argument(
        "--intelligence-request-run-id", type=_positive_cli_id, required=True
    )
    holding_review.add_argument(
        "--remainder-intent",
        choices=[item.value for item in RemainderIntent],
        default=RemainderIntent.UNKNOWN.value,
    )
    holding_review.add_argument(
        "--exit-reason",
        choices=[item.value for item in ExitReason],
        default=ExitReason.UNKNOWN.value,
    )
    holding_review.add_argument(
        "--use-of-proceeds",
        choices=[item.value for item in UseOfProceeds],
        default=UseOfProceeds.UNKNOWN.value,
    )

    market = subparsers.add_parser("market")
    market_subparsers = market.add_subparsers(dest="market_command", required=True)
    market_subparsers.add_parser("sectors")
    market_overview = market_subparsers.add_parser("overview")
    _add_intelligence_arguments(market_overview)

    news = subparsers.add_parser("news")
    news_subparsers = news.add_subparsers(dest="news_command", required=True)
    news_recent = news_subparsers.add_parser("recent")
    _add_intelligence_arguments(news_recent)

    research = subparsers.add_parser("research")
    research_subparsers = research.add_subparsers(dest="research_command", required=True)
    research_summary = research_subparsers.add_parser("summary")
    research_summary.add_argument("scope", choices=["news", "market", "fund"])
    research_summary.add_argument("fund_code", nargs="?")
    _add_intelligence_arguments(research_summary)
    research_scan = research_subparsers.add_parser("scan")
    _add_intelligence_arguments(research_scan)
    research_subparsers.add_parser("panorama")
    research_supplement = research_subparsers.add_parser("supplement")
    research_supplement.add_argument("--source-name", required=True)
    research_supplement.add_argument(
        "--source-kind",
        choices=["official", "platform_data", "industry_data", "media", "community"],
        required=True,
    )
    research_timeline = research_subparsers.add_parser("supplement-timeline")
    research_timeline.add_argument("--material-json", action="append", required=True)
    research_evidence_store = research_subparsers.add_parser("evidence-store")
    research_evidence_store.add_argument("--source-name", required=True)
    research_evidence_store.add_argument(
        "--source-kind",
        choices=["official", "platform_data", "industry_data", "media", "community"],
        required=True,
    )
    research_evidence_store.add_argument("--publisher")
    research_evidence_store.add_argument("--title", required=True)
    research_evidence_store.add_argument("--published-at", required=True)
    research_evidence_store.add_argument("--source-url", required=True)
    research_evidence_store.add_argument("--statistics-period", required=True)
    research_evidence_store.add_argument("--indicator-name", required=True)
    research_evidence_store.add_argument("--indicator-value", required=True)
    research_evidence_store.add_argument("--unit", required=True)
    research_evidence_store.add_argument("--methodology")
    research_evidence_store.add_argument("--short-excerpt")
    research_evidence_store.add_argument(
        "--verification-state", choices=["outer_page_verified"], required=True
    )
    research_evidence_store.add_argument(
        "--domain",
        choices=[
            "power_energy",
            "coal_oil_gas",
            "real_estate_materials",
            "industrial_commodities",
            "autos",
            "shipping_trade",
            "ai_compute",
            "consumer",
            "policy",
            "weather",
        ],
        required=True,
    )
    research_evidence_timeline = research_subparsers.add_parser("evidence-timeline")
    research_evidence_timeline.add_argument("--domain", required=True)
    research_evidence_timeline.add_argument("--indicator-name", required=True)
    research_evidence_timeline.add_argument("--unit", required=True)
    research_evidence_plan = research_subparsers.add_parser("evidence-refresh-plan")
    research_evidence_plan.add_argument("--domain", required=True)
    research_evidence_plan.add_argument("--indicator-name", required=True)
    research_evidence_plan.add_argument("--unit", required=True)
    research_evidence_plan.add_argument("--from-period")
    research_evidence_plan.add_argument("--through-period", required=True)
    research_event_store = research_subparsers.add_parser("event-store")
    research_event_store.add_argument("--source-name", required=True)
    research_event_store.add_argument("--publisher")
    research_event_store.add_argument(
        "--source-kind",
        choices=["official", "platform_data", "industry_data", "media", "community"],
        required=True,
    )
    research_event_store.add_argument("--title", required=True)
    research_event_store.add_argument("--source-url", required=True)
    research_event_store.add_argument("--published-at", required=True)
    research_event_store.add_argument("--event-occurred-at")
    research_event_store.add_argument("--event-key")
    research_event_store.add_argument("--fact-summary", required=True)
    research_event_store.add_argument("--claim-boundary", required=True)
    research_event_store.add_argument("--event-fact-key")
    research_event_store.add_argument("--event-fact-value")
    research_event_store.add_argument("--event-fact-unit")
    research_event_store.add_argument("--short-excerpt")
    research_event_store.add_argument(
        "--verification-state", choices=["outer_page_verified"], required=True
    )
    research_event_store.add_argument("--domain", required=True)
    research_event_timeline = research_subparsers.add_parser("event-timeline")
    research_event_timeline.add_argument("--domain", required=True)
    research_event_timeline.add_argument("--recent-days", type=int, default=30)
    research_supplement.add_argument("--title", required=True)
    research_supplement.add_argument("--published-at", required=True)
    research_supplement.add_argument("--source-url")
    research_supplement.add_argument("--statistics-period")
    research_supplement.add_argument("--indicator-name")
    research_supplement.add_argument("--indicator-value")
    research_supplement.add_argument("--unit")
    research_supplement.add_argument("--methodology")
    research_supplement.add_argument(
        "--domain",
        choices=[
            "power_energy",
            "coal_oil_gas",
            "real_estate_materials",
            "industrial_commodities",
            "autos",
            "shipping_trade",
            "ai_compute",
            "consumer",
            "policy",
            "weather",
        ],
        required=True,
    )

    thesis = subparsers.add_parser("thesis")
    thesis_subparsers = thesis.add_subparsers(dest="thesis_command", required=True)
    thesis_add = thesis_subparsers.add_parser("add")
    thesis_add.add_argument("fund_code")
    thesis_add.add_argument("--reason", required=True)
    thesis_add.add_argument("--horizon", required=True)
    thesis_add.add_argument("--invalidation", required=True)
    thesis_list = thesis_subparsers.add_parser("list")
    thesis_list.add_argument("--fund-code")
    thesis_review = thesis_subparsers.add_parser("review")
    thesis_review.add_argument("fund_code")
    thesis_match_project = thesis_subparsers.add_parser("match-project")
    thesis_match_project.add_argument("fund_code")
    thesis_match_project.add_argument(
        "--intelligence-request-run-id", type=_positive_cli_id, required=True
    )
    thesis_adjudicate = thesis_subparsers.add_parser("adjudicate")
    thesis_adjudicate.add_argument("fund_code")
    thesis_adjudicate.add_argument(
        "--thesis-match-projection-id", type=_positive_cli_id, required=True
    )
    thesis_adjudicate.add_argument(
        "--decision",
        choices=[item.value for item in AdjudicationDecision],
        required=True,
    )
    thesis_adjudicate.add_argument("--supersedes", type=_positive_cli_id)

    report = subparsers.add_parser("report")
    report_subparsers = report.add_subparsers(dest="report_command", required=True)
    report_subparsers.add_parser("weekly")

    ledger = subparsers.add_parser("ledger")
    ledger_subparsers = ledger.add_subparsers(dest="ledger_command", required=True)
    ledger_import = ledger_subparsers.add_parser("import")
    ledger_import.add_argument("image")
    ledger_import.add_argument("--fund-code")
    ledger_subparsers.add_parser("drafts")
    ledger_confirm = ledger_subparsers.add_parser("confirm")
    ledger_confirm.add_argument("draft_id", type=int)
    ledger_confirm.add_argument("--field", action="append", default=[])
    ledger_add = ledger_subparsers.add_parser("add")
    ledger_add.add_argument("--type", required=True, dest="transaction_type")
    ledger_add.add_argument("--fund-code", required=True)
    ledger_add.add_argument("--fund-name")
    ledger_add.add_argument("--amount")
    ledger_add.add_argument("--shares")
    ledger_add.add_argument("--nav")
    ledger_add.add_argument("--fee")
    ledger_add.add_argument("--order-time")
    ledger_add.add_argument("--confirmation-time")
    ledger_transactions = ledger_subparsers.add_parser("transactions")
    ledger_transactions.add_argument("--fund-code")
    ledger_reconcile = ledger_subparsers.add_parser("reconcile")
    ledger_reconcile.add_argument("--fund-code", required=True)
    ledger_document = ledger_subparsers.add_parser("document")
    document_subparsers = ledger_document.add_subparsers(dest="document_command", required=True)
    document_delete = document_subparsers.add_parser("delete")
    document_delete.add_argument("document_id", type=int)
    return parser


def _add_intelligence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--window",
        choices=["today", "recent", "near_term"],
    )
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument(
        "--mode",
        choices=[item.value for item in RequestMode],
        default=RequestMode.RAPID.value,
    )


def _positions_payload(context: ApplicationContext) -> List[Dict[str, Any]]:
    return [serialize(position) for position in context.repository.latest_positions()]


def _validate_fund_code(fund_code: str) -> None:
    if not _FUND_CODE.fullmatch(fund_code):
        raise InvalidFundCodeError("fund code must contain six digits")


def _manual_portfolio_position(value: str) -> ManualPortfolioPosition:
    code, separator, percent = value.partition("=")
    if separator != "=" or not _FUND_CODE.fullmatch(code):
        raise CliUsageError("manual position must use CODE=PERCENT")
    try:
        parsed = Decimal(percent)
    except Exception:
        raise CliUsageError("manual position percentage is invalid") from None
    if not parsed.is_finite() or not Decimal("0") < parsed <= Decimal("100"):
        raise CliUsageError("manual position percentage is invalid")
    return ManualPortfolioPosition(code, parsed / Decimal("100"))


def _intelligence_interval_arguments(
    args: argparse.Namespace,
) -> Tuple[str, Optional[date], Optional[date]]:
    if (args.start is None) != (args.end is None):
        raise CliUsageError("intelligence interval requires both --start and --end")
    if args.start is not None and args.window is not None:
        raise CliUsageError("--window cannot be combined with --start and --end")
    if args.start is None:
        return args.window or "recent", None, None
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    except (TypeError, ValueError):
        raise CliUsageError("intelligence interval dates must use YYYY-MM-DD") from None
    if start.isoformat() != args.start or end.isoformat() != args.end or end < start:
        raise CliUsageError("intelligence interval dates are invalid")
    return "recent", start, end


def _intelligence_response(
    context: ApplicationContext,
    args: argparse.Namespace,
) -> Dict[str, object]:
    if context.intelligence_service is None:
        raise CliUsageError("intelligence service is unavailable")
    window, start, end = _intelligence_interval_arguments(args)
    mode = args.mode
    if args.command == "news":
        result = context.intelligence_service.news_recent(
            window=window,
            mode=mode,
            start=start,
            end=end,
        )
    elif args.command == "market":
        result = context.intelligence_service.market_overview(
            window=window,
            mode=mode,
            start=start,
            end=end,
        )
    else:
        _validate_fund_code(args.fund_code)
        result = context.intelligence_service.fund_intelligence(
            args.fund_code,
            window=window,
            mode=mode,
            start=start,
            end=end,
        )
    from kunjin.intelligence.research import public_intelligence_payload

    return public_intelligence_payload(result)


def _research_summary_response(
    context: ApplicationContext,
    args: argparse.Namespace,
) -> Dict[str, object]:
    from kunjin.intelligence.research import public_intelligence_payload

    if context.intelligence_service is None:
        raise CliUsageError("intelligence service is unavailable")
    window, start, end = _intelligence_interval_arguments(args)
    if args.scope == "news":
        if args.fund_code is not None:
            raise CliUsageError("fund code is only allowed for fund research")
        result = context.intelligence_service.news_recent(
            window=window,
            mode=args.mode,
            start=start,
            end=end,
        )
    elif args.scope == "market":
        if args.fund_code is not None:
            raise CliUsageError("fund code is only allowed for fund research")
        result = context.intelligence_service.market_overview(
            window=window,
            mode=args.mode,
            start=start,
            end=end,
        )
    else:
        if args.fund_code is None:
            raise CliUsageError("fund research requires a fund code")
        _validate_fund_code(args.fund_code)
        result = context.intelligence_service.fund_intelligence(
            args.fund_code,
            window=window,
            mode=args.mode,
            start=start,
            end=end,
        )
    return summarize_public_research(public_intelligence_payload(result))


def _research_scan_response(
    context: ApplicationContext,
    args: argparse.Namespace,
) -> Dict[str, object]:
    from kunjin.intelligence.research import public_intelligence_payload

    if context.intelligence_service is None:
        raise CliUsageError("intelligence service is unavailable")
    window, start, end = _intelligence_interval_arguments(args)
    result = context.intelligence_service.market_overview(
        window=window,
        mode=args.mode,
        start=start,
        end=end,
    )
    return scan_public_research(public_intelligence_payload(result))


def _research_panorama_response(context: ApplicationContext) -> Dict[str, object]:
    from kunjin.intelligence.research import public_intelligence_payload

    if context.intelligence_service is None:
        raise CliUsageError("intelligence service is unavailable")
    end = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    windows = (
        ("近一月", end - timedelta(days=29)),
        ("近三月", end - timedelta(days=89)),
    )
    payloads = []
    for label, start in windows:
        result = context.intelligence_service.market_overview(
            window="recent",
            mode=RequestMode.RAPID.value,
            start=start,
            end=end,
        )
        payloads.append((label, public_intelligence_payload(result)))
    return build_cross_domain_panorama(tuple(payloads))


def _research_supplement_response(args: argparse.Namespace) -> Dict[str, object]:
    return summarize_user_supplied_evidence(
        {
            "source_name": args.source_name,
            "source_kind": args.source_kind,
            "title": args.title,
            "published_at": args.published_at,
            "original_url": args.source_url,
            "statistics_period": args.statistics_period,
            "indicator_name": args.indicator_name,
            "indicator_value": args.indicator_value,
            "unit": args.unit,
            "methodology": args.methodology,
            "domain_id": args.domain,
        }
    )


def _research_supplement_timeline_response(args: argparse.Namespace) -> Dict[str, object]:
    materials = []
    for encoded in args.material_json:
        try:
            material = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise CliUsageError("supplement timeline material is invalid") from exc
        if not isinstance(material, dict):
            raise CliUsageError("supplement timeline material is invalid")
        materials.append(material)
    return build_supplement_timeline(tuple(materials))


def _research_evidence_store_response(
    context: ApplicationContext, args: argparse.Namespace
) -> Dict[str, object]:
    return persist_verified_evidence(
        context.repository,
        {
            "source_name": args.source_name,
            "publisher": args.publisher,
            "source_kind": args.source_kind,
            "title": args.title,
            "published_at": args.published_at,
            "original_url": args.source_url,
            "statistics_period": args.statistics_period,
            "indicator_name": args.indicator_name,
            "indicator_value": args.indicator_value,
            "unit": args.unit,
            "methodology": args.methodology,
            "domain_id": args.domain,
            "source_verification_state": args.verification_state,
            "short_excerpt": args.short_excerpt,
        },
    )


def _research_evidence_timeline_response(
    context: ApplicationContext, args: argparse.Namespace
) -> Dict[str, object]:
    return build_persisted_timeline(
        context.repository, args.domain, args.indicator_name, args.unit
    )


def _research_evidence_refresh_plan_response(
    context: ApplicationContext, args: argparse.Namespace
) -> Dict[str, object]:
    return build_refresh_plan(
        context.repository,
        args.domain,
        args.indicator_name,
        args.unit,
        args.through_period,
        args.from_period,
    )


def _research_event_store_response(
    context: ApplicationContext, args: argparse.Namespace
) -> Dict[str, object]:
    return persist_verified_event(
        context.repository,
        {
            "source_name": args.source_name,
            "publisher": args.publisher,
            "source_kind": args.source_kind,
            "title": args.title,
            "original_url": args.source_url,
            "published_at": args.published_at,
            "event_occurred_at": args.event_occurred_at,
            "event_key": args.event_key,
            "fact_summary": args.fact_summary,
            "claim_boundary": args.claim_boundary,
            "event_fact_key": args.event_fact_key,
            "event_fact_value": args.event_fact_value,
            "event_fact_unit": args.event_fact_unit,
            "short_excerpt": args.short_excerpt,
            "source_verification_state": args.verification_state,
            "domain_id": args.domain,
        },
    )


def _research_event_timeline_response(
    context: ApplicationContext, args: argparse.Namespace
) -> Dict[str, object]:
    return build_persisted_event_timeline(
        context.repository, args.domain, recent_days=args.recent_days
    )


def _fund_brief_response(
    context: ApplicationContext,
    fund_code: str,
    action: ActionKind,
    mode: RequestMode,
) -> Dict[str, Any]:
    from kunjin.brief.research import public_outcome_payload

    _validate_fund_code(fund_code)
    if context.brief_service is None:
        raise CliUsageError("held fund brief service is unavailable")
    try:
        outcome = context.brief_service.brief_outcome(
            fund_code,
            action=action,
            mode=mode,
        )
        return public_outcome_payload(outcome)
    except KeyboardInterrupt:
        raise
    except SystemExit:
        raise FundBriefCliError("held fund brief failed") from None
    except Exception:
        raise FundBriefCliError("held fund brief failed") from None


def _validate_compare_codes(fund_codes: Sequence[str]) -> Tuple[str, ...]:
    codes = tuple(fund_codes)
    for fund_code in codes:
        _validate_fund_code(fund_code)
    if len(codes) < 2 or len(codes) > 10 or len(set(codes)) != len(codes):
        raise CliUsageError("fund compare requires 2 to 10 unique fund codes")
    return codes


def _validate_shortlist_codes(fund_codes: Sequence[str]) -> Tuple[str, ...]:
    codes = tuple(fund_codes)
    for fund_code in codes:
        _validate_fund_code(fund_code)
    if (
        len(codes) < 2
        or len(codes) > 5
        or len(set(codes)) != len(codes)
        or "000000" in codes
    ):
        raise CliUsageError("fund shortlist requires 2 to 5 unique fund codes")
    return codes


def _validate_candidate_codes(fund_codes: Sequence[str]) -> Tuple[str, ...]:
    codes = tuple(fund_codes)
    for fund_code in codes:
        _validate_fund_code(fund_code)
    if len(codes) < 2 or len(codes) > 5 or len(set(codes)) != len(codes):
        raise CliUsageError("fund candidates requires 2 to 5 unique fund codes")
    return codes


def _related_fund_codes(fund_code: str, related_funds: Sequence[str]) -> Tuple[str, ...]:
    codes = (fund_code, *related_funds)
    for code in codes:
        _validate_fund_code(code)
    if len(codes) > 5 or len(set(codes)) != len(codes):
        raise CliUsageError("fund review related funds must be 1 to 5 unique fund codes")
    return codes


def _peer_components(context: ApplicationContext) -> Tuple[PeerStore, FundDisclosureStore]:
    if context.peer_store is None:
        raise ValueError("peer store is unavailable")
    if context.fund_disclosure_store is None:
        raise ValueError("fund disclosure store is unavailable")
    return context.peer_store, context.fund_disclosure_store


def _comparison_inputs(
    context: ApplicationContext, fund_codes: Sequence[str]
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Any]]:
    if context.fund_disclosure_store is None:
        raise ValueError("fund disclosure store is unavailable")
    codes = tuple(fund_codes)
    bundles = {code: context.fund_disclosure_store.load_bundle(code) for code in codes}
    histories = {code: context.repository.fund_history(code) for code in codes}
    positions = context.repository.latest_positions()
    return bundles, histories, positions


def _comparison_status(report: Dict[str, Any]) -> str:
    warnings = set(report.get("warnings", ()))
    data_gaps = tuple(report.get("data_gaps", ()))
    if "peer_group_too_small" in warnings:
        return "insufficient_data"
    coverage = report.get("coverage", {})
    if report.get("comparison_kind") == "portfolio_overlap":
        usable_weight = Decimal(str(coverage.get("portfolio_weight_coverage") or "0"))
        if coverage.get("held_funds_total", 0) == 0 or usable_weight == 0:
            return "insufficient_data"
        return "partial" if warnings or data_gaps or report.get("errors") else "success"
    if coverage.get("members_with_disclosures", 0) == 0:
        return "insufficient_data"
    return "partial" if warnings or data_gaps or report.get("errors") else "success"


def _save_comparison_report(
    peer_store: PeerStore,
    comparison_kind: str,
    report: Dict[str, Any],
    as_of: datetime,
    anchor_fund_code: Optional[str] = None,
    peer_group_id: Optional[int] = None,
) -> Dict[str, Any]:
    status = _comparison_status(report)
    fingerprint_payload = dict(report)
    fingerprint_payload.pop("as_of", None)
    fingerprint = comparison_fingerprint(
        {"calculation_date": as_of.date(), "report": fingerprint_payload}
    )
    warnings = list(report.get("warnings", ()))
    run_id = peer_store.save_comparison(
        comparison_kind,
        anchor_fund_code,
        peer_group_id,
        as_of,
        status,
        fingerprint,
        report,
        None if not warnings else ";".join(warnings),
    )
    return {**report, "status": status, "comparison_run_id": run_id}


def _parse_report_period(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InvalidReportPeriodError(
            "report period must be a valid date in YYYY-MM-DD format"
        ) from None


def _disclosure_report(
    context: ApplicationContext,
    fund_code: str,
    period: Optional[date] = None,
) -> Dict[str, Any]:
    _validate_fund_code(fund_code)
    if context.fund_disclosure_store is None:
        raise ValueError("fund disclosure store is unavailable")
    bundle = context.fund_disclosure_store.load_bundle(fund_code)
    if period is not None:
        period_holdings = tuple(item for item in bundle.holdings if item.report_period == period)
        period_industry = tuple(
            item for item in bundle.industry_exposure if item.report_period == period
        )
        warnings = bundle.warnings
        if not period_holdings and not period_industry:
            warnings += ("requested_report_period_not_found",)
        bundle = replace(
            bundle,
            holdings=period_holdings,
            industry_exposure=period_industry,
            warnings=warnings,
        )
    return build_disclosure_report(bundle, datetime.now(timezone.utc))


def _disclosure_errors(context: ApplicationContext, fund_code: str) -> List[Dict[str, str]]:
    if context.fund_disclosure_store is None:
        return []
    statuses = context.fund_disclosure_store.load_bundle(fund_code).section_statuses
    return [
        {
            "section": section,
            "code": str(status.get("error_code") or "source_unavailable"),
            "message": str(status.get("error_message") or "source is unavailable"),
        }
        for section, status in sorted(statuses.items())
        if status.get("error_code")
    ]


def _report_metadata(
    report: Dict[str, Any], errors: Optional[List[Dict[str, str]]] = None
) -> Dict[str, Any]:
    return {
        "sources": report["sources"],
        "freshness": report["freshness"],
        "warnings": report["warnings"],
        "conflicts": report["conflicts"],
        "errors": errors or [],
    }


def _sync_disclosure(
    context: ApplicationContext,
    fund_code: str,
    profile: bool,
    *,
    mode: RequestMode,
    force_reason: Optional[ForceReasonCode],
) -> Dict[str, Any]:
    _validate_fund_code(fund_code)
    if context.fund_disclosure_service is None:
        raise ValueError("fund disclosure service is unavailable")
    budget = RequestBudget.create(mode)
    audit_store = DecisionAuditStore(context.repository)
    request_run_id = audit_store.begin_request(budget)
    health_service = SourceHealthService(audit_store)
    request_context = SourceRequestContext(
        request_run_id,
        budget,
        audit_store,
        health_service,
        force_reason,
    )
    requested_sections = PROFILE_SECTIONS if profile else HOLDING_SECTIONS
    terminal_status = RequestTerminalStatus.FAILED
    omitted_work: Tuple[str, ...] = ()
    result = None

    def request_metadata() -> Dict[str, Any]:
        return {
            "request_id": budget.request_id,
            "mode": budget.mode.value,
            "terminal_status": terminal_status.value,
            "deadline_at": budget.deadline_at.isoformat(),
            "omitted_work": list(omitted_work),
        }

    def finalize() -> None:
        nonlocal terminal_status
        finished_at = _request_finish_now()
        if finished_at < budget.started_at:
            terminal_status = RequestTerminalStatus.FAILED
            finished_at = budget.started_at
        elif finished_at >= budget.deadline_at:
            budget.cancel("request_deadline_reached")
            terminal_status = RequestTerminalStatus.EXPIRED
        try:
            audit_store.finalize_request(
                request_run_id,
                terminal_status,
                finished_at,
                omitted_work,
            )
        except BaseException as finalization_error:
            raise BoundedDisclosureSyncError(request_metadata()) from finalization_error

    try:
        result = (
            context.fund_disclosure_service.sync_profile(
                fund_code,
                request_context=request_context,
            )
            if profile
            else context.fund_disclosure_service.sync_holdings(
                fund_code,
                request_context=request_context,
            )
        )
        omitted_work = result.omitted_work
        usable = tuple(
            item
            for item in result.sections.values()
            if item.error_code is None and item.status in {"success", "not_disclosed"}
        )
        if budget.cancelled:
            terminal_status = (
                RequestTerminalStatus.EXPIRED
                if budget.cancel_reason
                in {"request_expired", "worker_timeout", "request_deadline_reached"}
                else RequestTerminalStatus.CANCELLED
            )
        elif not usable:
            terminal_status = RequestTerminalStatus.FAILED
        elif omitted_work or len(usable) != len(result.sections):
            terminal_status = RequestTerminalStatus.PARTIAL
        else:
            terminal_status = RequestTerminalStatus.COMPLETE
    except (KeyboardInterrupt, SystemExit) as control_flow_error:
        budget.cancel("request_cancelled")
        terminal_status = RequestTerminalStatus.CANCELLED
        if isinstance(control_flow_error, FundDisclosureSyncInterrupted):
            omitted_work = control_flow_error.omitted_work
        else:
            omitted_work = tuple(
                dict.fromkeys(
                    (
                        *requested_sections,
                        *(
                            SECTION_SPECS[item].audit_field_id
                            for item in requested_sections
                        ),
                    )
                )
            )
        try:
            finalize()
        except BoundedDisclosureSyncError:
            pass
        raise
    except Exception as error:
        terminal_status = (
            RequestTerminalStatus.EXPIRED
            if budget.cancelled
            and budget.cancel_reason
            in {"request_expired", "worker_timeout", "request_deadline_reached"}
            else RequestTerminalStatus.FAILED
        )
        omitted_work = tuple(
            dict.fromkeys(
                (
                    *requested_sections,
                    *(
                        SECTION_SPECS[item].audit_field_id
                        for item in requested_sections
                    ),
                )
            )
        )
        try:
            finalize()
        except BoundedDisclosureSyncError as finalization_error:
            raise BoundedDisclosureSyncError(request_metadata()) from finalization_error
        raise BoundedDisclosureSyncError(request_metadata()) from error
    finalize()
    if result is None:
        raise BoundedDisclosureSyncError(request_metadata())
    report = _disclosure_report(context, fund_code)
    failed_sections = [
        {
            "section": section,
            "code": item.error_code or item.status,
            "message": item.status,
        }
        for section, item in result.sections.items()
        if item.error_code is not None or item.status == "source_unavailable"
    ]
    data = {
        "fund_code": fund_code,
        "sections": serialize(result.sections),
        "request": request_metadata(),
        **_report_metadata(report, failed_sections),
    }
    successful = any(
        item.error_code is None and item.status in {"success", "not_disclosed"}
        for item in result.sections.values()
    )
    return {"data": data, "successful": successful}


def _sections_due(service: FundDisclosureService, fund_code: str, sections: Sequence[str]) -> bool:
    return any(
        service.section_snapshot(fund_code, section).freshness != "fresh" for section in sections
    )


def _append_section_sync_errors(errors: List[Dict[str, str]], fund_code: str, result: Any) -> None:
    for section, item in result.sections.items():
        if item.error_code is not None or item.status == "source_unavailable":
            errors.append(
                {
                    "code": item.error_code or "source_unavailable",
                    "message": f"{fund_code}/{section}: {item.status}",
                }
            )


def _append_peer_refresh_errors(errors: List[Dict[str, str]], peer_results: Dict[str, Any]) -> None:
    for anchor_code, result in peer_results.items():
        status = getattr(result.status, "value", result.status)
        if status != "source_unavailable":
            continue
        details = result.errors or ({},)
        for detail in details:
            code = str(
                detail.get("error_code")
                or detail.get("code")
                or next(reversed(result.warnings), "peer_group_refresh_failed")
            )
            message = str(detail.get("message") or "peer group refresh failed")
            errors.append(
                {
                    "code": code,
                    "message": f"{anchor_code}: {redact_secrets(message)}",
                }
            )


def _validate_allocation_json_response(command: str, value: object) -> Dict[str, Any]:
    try:
        _validate_amount_free_primitive(value)
        if command == "ranges":
            _validate_allocation_ranges(value)
            return value
        if command == "status":
            _validate_allocation_status(value)
            return value
        if command == "history":
            if type(value) is not tuple:
                raise ValueError("history must be a tuple")
            assessments = list(value)
            for item in assessments:
                _validate_allocation_history_item(item)
            return {"assessments": assessments}
        if command == "policy":
            _validate_allocation_policy(value)
            return value
        raise ValueError("unsupported allocation command")
    except (TypeError, ValueError):
        raise AllocationCalculationError(
            "allocation service returned an invalid response"
        ) from None


def _validate_amount_free_primitive(value: object, path: str = "root") -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is str:
        lowered = value.lower()
        if any(forbidden in lowered for forbidden in _ALLOCATION_FORBIDDEN_TEXT):
            raise ValueError(f"forbidden allocation text at {path}")
        return
    if type(value) is list or type(value) is tuple:
        for index, item in enumerate(value):
            _validate_amount_free_primitive(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"non-string allocation key at {path}")
            lowered = key.lower()
            if lowered not in _ALLOCATION_APPROVED_SENSITIVE_KEYS and any(
                forbidden in lowered for forbidden in _ALLOCATION_FORBIDDEN_TEXT
            ):
                raise ValueError(f"forbidden allocation key at {path}")
            _validate_amount_free_primitive(item, f"{path}.{key}")
        return
    raise ValueError(f"unsupported allocation value at {path}")


def _require_keys(value: object, expected: set, name: str) -> Dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"invalid {name} schema")
    return value


def _positive_int(value: object, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {name}")


def _non_negative_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {name}")


def _percentage(value: object, name: str) -> None:
    if type(value) is not str or not re.fullmatch(r"(?:0|1|0\.[0-9]+)", value):
        raise ValueError(f"invalid {name}")
    decimal = Decimal(value)
    if not Decimal("0") <= decimal <= Decimal("1"):
        raise ValueError(f"invalid {name}")


def _percentage_text(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _timestamp(value: object, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str:
        raise ValueError(f"invalid {name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid {name}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError(f"invalid {name}")


def _code_list(value: object, allowed: set, name: str) -> None:
    if type(value) is not list or len(value) != len(set(value)):
        raise ValueError(f"invalid {name}")
    if any(type(item) is not str or item not in allowed for item in value):
        raise ValueError(f"invalid {name}")


def _validate_safe_summary(value: object) -> None:
    summary = _require_keys(
        value,
        {
            "goal_count",
            "obligation_count",
            "fully_funded_now_count",
            "fundable_without_return_count",
            "funding_gap_without_return_count",
            "horizon_equity_ceilings",
        },
        "safe summary",
    )
    for key in (
        "goal_count",
        "obligation_count",
        "fully_funded_now_count",
        "fundable_without_return_count",
        "funding_gap_without_return_count",
    ):
        _non_negative_int(summary[key], key)
    if type(summary["horizon_equity_ceilings"]) is not list:
        raise ValueError("invalid horizon ceilings")
    for item in summary["horizon_equity_ceilings"]:
        _percentage(item, "horizon ceiling")


def _validate_permitted_region(value: object) -> None:
    if value is None:
        return
    region = _require_keys(
        value,
        {
            "inequalities",
            "maximum_equity",
            "horizon_equity_ceiling",
            "loss_amount_equity_ceiling",
            "drawdown_equity_ceiling",
            "willingness_equity_ceiling",
            "stability_equity_ceiling",
        },
        "permitted region",
    )
    inequalities = region["inequalities"]
    if type(inequalities) is not list or tuple(inequalities) != _ALLOCATION_INEQUALITIES:
        raise ValueError("invalid inequalities")
    for key in set(region) - {"inequalities"}:
        _percentage(region[key], key)


def _validate_allocation_ranges(value: object) -> None:
    data = _require_keys(
        value,
        {
            "assessment_id",
            "profile_version",
            "suitability_assessment_id",
            "policy_version",
            "status",
            "blocks",
            "binding_constraints",
            "profile_conflicts",
            "safe_summary",
            "permitted_region",
            "assessed_at",
            "valid_until",
            "freshness",
            "capability",
        },
        "allocation ranges",
    )
    _positive_int(data["assessment_id"], "assessment id", optional=True)
    _positive_int(data["profile_version"], "profile version")
    _positive_int(data["suitability_assessment_id"], "suitability assessment id")
    if data["policy_version"] != "1" or data["capability"] != "research_only":
        raise ValueError("invalid allocation binding")
    if data["status"] not in {"blocked", "range_available"}:
        raise ValueError("invalid allocation status")
    _code_list(data["blocks"], {item.value for item in AllocationBlockCode}, "blocks")
    _code_list(
        data["binding_constraints"],
        {item.value for item in AllocationConstraintCode},
        "constraints",
    )
    _code_list(
        data["profile_conflicts"],
        {item.value for item in AllocationProfileConflictCode},
        "profile conflicts",
    )
    _validate_safe_summary(data["safe_summary"])
    _validate_permitted_region(data["permitted_region"])
    _timestamp(data["assessed_at"], "assessed_at")
    _timestamp(data["valid_until"], "valid_until", optional=True)
    if data["freshness"] not in {"fresh", "transient"}:
        raise ValueError("invalid allocation freshness")
    if data["status"] == "range_available" and (
        data["assessment_id"] is None
        or data["permitted_region"] is None
        or data["valid_until"] is None
        or data["freshness"] != "fresh"
    ):
        raise ValueError("invalid available allocation range")
    if data["status"] == "blocked" and (
        data["assessment_id"] is not None
        or data["permitted_region"] is not None
        or data["valid_until"] is not None
        or data["freshness"] != "transient"
    ):
        raise ValueError("invalid blocked allocation range")


def _validate_allocation_history_item(value: object, *, status: bool = False) -> None:
    expected = {
        "assessment_id",
        "profile_version_id",
        "suitability_assessment_id",
        "policy_version",
        "status",
        "binding_constraints",
        "safe_summary",
        "permitted_region",
        "assessed_at",
        "valid_until",
        "capability",
    }
    if status:
        expected |= {"state", "freshness"}
    data = _require_keys(value, expected, "allocation history item")
    for key in ("assessment_id", "profile_version_id", "suitability_assessment_id"):
        _positive_int(data[key], key)
    if (
        data["policy_version"] != "1"
        or data["status"] != "range_available"
        or data["capability"] != "research_only"
    ):
        raise ValueError("invalid allocation history binding")
    _code_list(
        data["binding_constraints"],
        {item.value for item in AllocationConstraintCode},
        "constraints",
    )
    _validate_safe_summary(data["safe_summary"])
    _validate_permitted_region(data["permitted_region"])
    _timestamp(data["assessed_at"], "assessed_at")
    _timestamp(data["valid_until"], "valid_until")
    if status and (data["state"] not in {"fresh", "stale"} or data["freshness"] != data["state"]):
        raise ValueError("invalid allocation status freshness")


def _validate_allocation_status(value: object) -> None:
    if value == {
        "state": "missing",
        "freshness": "missing",
        "capability": "research_only",
    }:
        return
    _validate_allocation_history_item(value, status=True)


def _validate_allocation_policy(value: object) -> None:
    data = _require_keys(
        value,
        {
            "version",
            "checksum",
            "effective_at",
            "stress_loss_by_layer",
            "horizon_equity_ceilings",
            "willingness_equity_ceilings",
            "stability_equity_ceilings",
            "capability",
        },
        "allocation policy",
    )
    fixed = AllocationPolicyV1()
    if data["version"] != fixed.version or data["capability"] != "research_only":
        raise ValueError("invalid allocation policy identity")
    if data["checksum"] != fixed.checksum():
        raise ValueError("invalid allocation policy checksum")
    _timestamp(data["effective_at"], "effective_at")
    if data["effective_at"] != fixed.effective_at.isoformat():
        raise ValueError("invalid allocation policy effective time")
    stress = _require_keys(
        data["stress_loss_by_layer"],
        {"protected_cash", "high_quality_fixed_income", "diversified_equity"},
        "stress policy",
    )
    expected_stress = {
        layer.value: _percentage_text(item) for layer, item in fixed.stress_loss_by_layer
    }
    if stress != expected_stress:
        raise ValueError("invalid stress policy")
    horizons = data["horizon_equity_ceilings"]
    if type(horizons) is not list or len(horizons) != 5:
        raise ValueError("invalid horizon policy")
    expected_horizons = [
        {"maximum_years": years, "equity_ceiling": _percentage_text(ceiling)}
        for years, ceiling in fixed.horizon_equity_ceilings
    ]
    if horizons != expected_horizons:
        raise ValueError("invalid horizon policy")
    for item in horizons:
        row = _require_keys(item, {"maximum_years", "equity_ceiling"}, "horizon row")
        if row["maximum_years"] is not None:
            _positive_int(row["maximum_years"], "maximum years")
        _percentage(row["equity_ceiling"], "equity ceiling")
    expected_ceilings = {
        "willingness_equity_ceilings": {
            label: _percentage_text(item) for label, item in fixed.willingness_equity_ceilings
        },
        "stability_equity_ceilings": {
            label: _percentage_text(item) for label, item in fixed.stability_equity_ceilings
        },
    }
    for key in ("willingness_equity_ceilings", "stability_equity_ceilings"):
        ceilings = data[key]
        if type(ceilings) is not dict or ceilings != expected_ceilings[key]:
            raise ValueError("invalid allocation policy ceilings")
        for label, item in ceilings.items():
            if type(label) is not str or not label:
                raise ValueError("invalid allocation policy label")
            _percentage(item, label)


def _allocation_call(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except (
        AllocationPolicyError,
        AllocationCalculationError,
        EncryptedProfileUnavailableError,
        ProfileCryptoError,
    ):
        raise
    except Exception:
        raise AllocationCalculationError("allocation operation failed") from None


def _allocation_ranges_response(service: AllocationService, json_output: bool) -> Dict[str, Any]:
    def operation() -> Dict[str, Any]:
        execution = service.ranges()
        if json_output:
            return _validate_allocation_json_response("ranges", execution.safe_json())
        return execution.local_view()

    return _allocation_call(operation)


def _allocation_safe_response(operation: Callable[[], object], command: str) -> Dict[str, Any]:
    return _allocation_call(lambda: _validate_allocation_json_response(command, operation()))


def _fund_risk_service(context: ApplicationContext) -> FundRiskService:
    service = context.fund_risk_service
    if service is None:
        raise CliUsageError("fund risk service is unavailable")
    return service


def _risk_call(operation: Callable[[], Any], fallback_code: str) -> Any:
    try:
        return operation()
    except RiskServiceError:
        raise
    except Exception:
        raise RiskServiceError(fallback_code) from None


def _risk_authenticated_report(record: object) -> Dict[str, Any]:
    try:
        report = build_authenticated_risk_research_report(record)
        _validate_public_risk_payload(report)
    except RiskServiceError:
        raise
    except Exception:
        raise RiskServiceError("classification_storage_failed") from None
    if report.get("capability") != "research_only":
        raise RiskServiceError("classification_storage_failed")
    return report


def _risk_classify_report(service: FundRiskService, fund_code: str) -> Dict[str, Any]:
    classification = _risk_call(
        lambda: service.classify(fund_code),
        "classification_calculation_failed",
    )
    history = _risk_call(
        lambda: service.classification_history(fund_code),
        "classification_storage_failed",
    )
    fingerprint = getattr(classification, "input_fingerprint", None)
    match = next(
        (item for item in history if getattr(item, "input_fingerprint", None) == fingerprint),
        None,
    )
    if match is None:
        raise RiskServiceError("classification_storage_failed")
    record = _risk_call(
        lambda: service.classification_evidence(
            fund_code,
            classification_id=getattr(match, "id", None),
        ),
        "classification_storage_failed",
    )
    if record is None:
        raise RiskServiceError("classification_storage_failed")
    report = _risk_authenticated_report(record)
    if report.get("classification", {}).get("input_fingerprint") != fingerprint:
        raise RiskServiceError("classification_storage_failed")
    return report


def _risk_current_report(
    service: FundRiskService,
    fund_code: str,
) -> Optional[Dict[str, Any]]:
    record = _risk_call(
        lambda: service.classification_evidence(fund_code),
        "classification_storage_failed",
    )
    if record is None:
        return None
    return _risk_authenticated_report(record)


def _risk_history_report(service: FundRiskService, fund_code: str) -> Dict[str, Any]:
    history = _risk_call(
        lambda: service.classification_history(fund_code),
        "classification_storage_failed",
    )
    reports = []
    for item in history:
        classification_id = getattr(item, "id", None)
        if type(classification_id) is not int or classification_id <= 0:
            raise RiskServiceError("classification_storage_failed")
        record = _risk_call(
            lambda classification_id=classification_id: service.classification_evidence(
                fund_code,
                classification_id=classification_id,
            ),
            "classification_storage_failed",
        )
        if record is None:
            raise RiskServiceError("classification_storage_failed")
        report = _risk_authenticated_report(record)
        if report.get("classification", {}).get("input_fingerprint") != getattr(
            item,
            "input_fingerprint",
            None,
        ):
            raise RiskServiceError("classification_storage_failed")
        reports.append({"classification_id": classification_id, **report})
    return {
        "capability": "research_only",
        "fund_code": fund_code,
        "classifications": reports,
    }


def _missing_risk_report(fund_code: str) -> Dict[str, Any]:
    return {
        "capability": "research_only",
        "fund_code": fund_code,
        "status": "missing",
        "classification": None,
    }


def _risk_policy_report() -> Dict[str, Any]:
    try:
        policy = ClassificationPolicyV1()
        policy.validate()
        canonical = json.loads(policy.canonical_json().decode("ascii"))
        checksum = policy.checksum()
    except Exception:
        raise RiskServiceError("classification_policy_unavailable") from None
    return {
        "capability": "research_only",
        "policy": canonical,
        "policy_checksum": checksum,
    }


def _risk_converter_status_report(service: FundRiskService) -> Dict[str, Any]:
    try:
        status = service.converter_status()
        if type(status) is not ConverterStatus:
            raise ValueError("converter status must be exact")
        status.validate()
        report = {
            "capability": status.capability,
            "status": status.status,
            "reason_code": status.reason_code,
            "parser_version": status.parser_version,
            "provenance_checksum": status.provenance_checksum,
        }
        _validate_public_risk_payload(report)
        return report
    except Exception:
        return {
            "capability": "research_only",
            "status": "unavailable",
            "reason_code": "legacy_converter_unavailable",
            "parser_version": None,
            "provenance_checksum": None,
        }


def _risk_sync_report(result: object) -> Dict[str, Any]:
    if type(result) is not DocumentSyncResult:
        raise ValueError("fund document synchronization returned an invalid result")
    result.validate()
    selections = []
    for item in result.selections:
        if type(item) is not DocumentSelectionItem:
            raise ValueError("fund document synchronization returned an invalid selection")
        item.validate()
        selections.append(
            {
                "document_kind": item.document_kind,
                "status": item.status,
                "selected_url": item.selected_url,
                "candidate_count": item.candidate_count,
                "reason_code": item.reason_code,
            }
        )
    documents = []
    for item in result.documents:
        if type(item) is not DocumentSyncItem:
            raise ValueError("fund document synchronization returned an invalid item")
        item.validate()
        documents.append(
            {
                "document_kind": item.document_kind,
                "title": item.title,
                "url": item.url,
                "published_at": item.published_at,
                "status": item.status,
                "artifact_id": item.artifact_id,
                "fact_count": item.fact_count,
                "warnings": item.warnings,
                "conflicts": item.conflicts,
                "error_code": item.error_code,
                "failure_stage": item.failure_stage,
                "failure_reason": item.failure_reason,
            }
        )
    report = serialize(
        {
            "fund_code": result.fund_code,
            "status": result.status,
            "selections": selections,
            "selection_checksum": result.selection_checksum,
            "documents": documents,
            "attempted_at": result.attempted_at,
            "capability": result.capability,
        }
    )
    _validate_public_risk_payload(report)
    return report


def _risk_sync_errors(result: object) -> List[Dict[str, str]]:
    if type(result) is not DocumentSyncResult:
        raise ValueError("fund document synchronization returned an invalid result")
    result.validate()
    successful = any(item.status == "success" for item in result.documents)
    if result.status != "failed" or successful:
        return []
    codes = sorted(
        {
            str(item.error_code)
            for item in result.documents
            if item.error_code in _RISK_TECHNICAL_CODES
        }
    )
    if not codes:
        codes = ["official_document_unavailable"]
    return [{"code": code, "message": _RISK_PUBLIC_ERROR_MESSAGES[code]} for code in codes]


def _validate_public_risk_payload(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _RISK_PRIVATE_OUTPUT_KEYS:
                raise ValueError("risk report contains a private field")
            _validate_public_risk_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_public_risk_payload(item)
        return
    if isinstance(value, str):
        try:
            validate_public_risk_string(value)
        except ValueError:
            raise ValueError("risk report contains local implementation details") from None


_DECISION_ROUTE_DATA_KEYS = frozenset(
    {
        "actions",
        "conclusion_evidence",
        "created_at",
        "missing_fields",
        "mode",
        "opposing_evidence",
        "policy_checksum",
        "policy_version",
        "registry_checksum",
        "registry_version",
        "request_id",
        "result_checksum",
        "workflow_level",
    }
)
_ACTION_ROUTE_DATA_KEYS = frozenset(
    {
        "action",
        "action_id",
        "action_maturity",
        "blocking_codes",
        "exact_amount_available",
        "minimum_state",
        "required_gates",
        "research_available",
        "risk_effect",
    }
)
_CONCLUSION_EVIDENCE_DATA_KEYS = frozenset(
    {
        "completeness",
        "conflicts",
        "coverage_percent",
        "freshness",
        "independent_lineage_count",
        "inferred",
        "lineage_ids",
        "market_as_of",
        "missing_critical_fields",
        "publication_times",
        "publishers",
        "report_as_of",
        "retrieved_at",
        "source_ids",
        "source_tier",
    }
)
_SOURCE_STATUS_DATA_KEYS = frozenset(
    {
        "fund_code",
        "mode",
        "policy_checksum",
        "policy_version",
        "registry_checksum",
        "registry_version",
        "request_field_resolutions",
        "request_id",
        "snapshot_at",
        "source_fields",
    }
)
_SOURCE_FIELD_DATA_KEYS = frozenset(
    {
        "acceptable_alternatives",
        "consecutive_failures",
        "cooldown_until",
        "field_id",
        "field_scope",
        "last_failure_at",
        "last_failure_reason",
        "last_success_at",
        "last_success_data_as_of",
        "source_id",
        "source_kind",
        "source_scope",
        "source_tier",
        "state",
        "supplementation",
    }
)
_REQUEST_FIELD_RESOLUTION_KEYS = frozenset(
    {"action", "field_id", "primary_source_id", "resolution", "risk_effect"}
)
_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
_ACTION_REQUIRED_GATES = {
    ActionKind.FACT_RESEARCH: (),
    ActionKind.CONTINUE_HOLDING: ("phase_b_context", "phase_e_policy"),
    ActionKind.REDUCE_TO_CASH: ("position", "fees", "settlement", "minimum_remainder"),
    ActionKind.FULL_EXIT: (
        "exit_reason",
        "position",
        "fees",
        "settlement",
        "use_of_proceeds",
    ),
    ActionKind.BUY_OR_ADD: ("phase_b", "phase_c", "d1", "d2", "d3", "post_trade"),
}
_ROUTE_FIELDS = frozenset(
    {
        "phase_b",
        "phase_c",
        "d1",
        "d2",
        "d3",
        "post_trade",
        "phase_e_policy",
        "position",
        "fees",
        "settlement",
        "minimum_remainder",
        "exit_reason",
        "use_of_proceeds",
    }
)
_ROUTE_GATE_CODES = _ROUTE_FIELDS | frozenset({"phase_b_context"})
_ROUTE_BLOCKING_CODES = frozenset(
    {
        "phase_b_blocked",
        "phase_e_policy_missing",
        "financial_safety_not_current",
        *(f"{field}_missing" for field in _ROUTE_FIELDS),
        *(item.value for item in BlockReason),
    }
)
_ROUTE_OPPOSING_CODES = frozenset(
    {
        "continued_exposure_is_not_risk_free",
        "financial_safety_conflicts_with_continued_exposure",
        "reduction_may_create_transaction_costs",
        "full_exit_may_change_portfolio_balance",
        "new_money_increases_risk",
        *(item.value for item in ConstraintReason),
    }
)
_SOURCE_FAILURE_OUTCOMES = frozenset(
    {
        SourceAttemptOutcome.TRANSIENT_FAILURE,
        SourceAttemptOutcome.UNAVAILABLE,
        SourceAttemptOutcome.UNSUPPORTED,
    }
)
_PHASE0_FORBIDDEN_IDENTIFIERS = _RISK_PRIVATE_OUTPUT_KEYS | frozenset(
    {
        "access_token",
        "account_balance",
        "account_number",
        "api_key",
        "cookie",
        "managed_path",
        "monthly_net_income",
        "order_id",
        "password",
        "private_value",
        "raw_exception",
        "session_token",
        "transaction_id",
        "worker_id",
        "worker_pid",
    }
)
_PHASE0_PRIVATE_TEXT_MARKERS = (
    "access token",
    "account balance",
    "account number",
    "api key",
    "emergency reserve",
    "goal amount",
    "managed path",
    "maximum tolerable loss",
    "monthly essential expenses",
    "monthly net income",
    "monthly required debt service",
    "obligation amount",
    "private value",
    "target amount",
    "transaction identifier",
    "worker pid",
)
_PHASE0_TEXT_DECODE_PASSES = 3
_PHASE0_NUMERIC_PAYLOAD = re.compile(
    r"^[+\-]?\s*(?:[¥￥$€£]\s*)?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:cny|rmb|usd|eur|元|万元))?$",
    re.IGNORECASE,
)
_PHASE0_AMOUNT_ASSIGNMENT = re.compile(
    r"(?:^|[^a-z0-9])(?:amount|balance|debt|goal|income|reserve|target)"
    r"\s*[:=]\s*[¥￥$€£]?\s*\d",
    re.IGNORECASE,
)
_PHASE0_LARGE_NUMBER_TOKEN = re.compile(
    r"(?<![\d,])(?:\d{8,}|\d{1,3}(?:,\d{3}){2,})(?![\d,])"
)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("public timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_utc_iso(
    value: object,
    name: str,
    *,
    optional: bool = False,
) -> Optional[datetime]:
    if optional and value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{name} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be an exact UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be an exact UTC timestamp")
    if _utc_iso(parsed) != value:
        raise ValueError(f"{name} must use canonical UTC representation")
    return parsed


def _validate_utc_iso(value: object, name: str, *, optional: bool = False) -> None:
    _parse_utc_iso(value, name, optional=optional)


def _identifier_list(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
    allowed_identifiers: Optional[frozenset[str]] = None,
) -> Tuple[str, ...]:
    if type(value) is not list or len(value) > 128 or (not allow_empty and not value):
        raise ValueError(f"{name} must be a bounded exact list")
    result = tuple(
        _public_identifier(
            item,
            name,
            allowed_identifiers=allowed_identifiers,
        )
        for item in value
    )
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _public_text_list(value: object, name: str) -> Tuple[str, ...]:
    if type(value) is not list or len(value) > 128:
        raise ValueError(f"{name} must be a bounded exact list")
    result = tuple(_phase0_public_text(item, name) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _public_identifier(
    value: object,
    name: str,
    *,
    allowed_identifiers: Optional[frozenset[str]] = None,
) -> str:
    result = validate_identifier(value, name)
    if allowed_identifiers is not None and result in allowed_identifiers:
        return result
    if result in _PHASE0_FORBIDDEN_IDENTIFIERS:
        raise ValueError(f"{name} is a private identifier")
    normalized = " ".join(result.casefold().split("_"))
    padded = f" {normalized} "
    if any(f" {marker} " in padded for marker in _PHASE0_PRIVATE_TEXT_MARKERS):
        raise ValueError(f"{name} contains a private identifier")
    if _PHASE0_LARGE_NUMBER_TOKEN.search(result) is not None:
        raise ValueError(f"{name} contains a private numeric token")
    return result


def _phase0_public_text(value: object, name: str) -> str:
    result = validate_public_text(value, name)
    normalized_text = _normalize_phase0_public_text(result, name)
    validate_public_text(normalized_text, name)
    normalized = " ".join(
        part for part in re.split(r"[\W_]+", normalized_text.casefold()) if part
    )
    if any(marker in normalized for marker in _PHASE0_PRIVATE_TEXT_MARKERS):
        raise ValueError(f"{name} contains a private value")
    if _PHASE0_NUMERIC_PAYLOAD.fullmatch(normalized_text.strip()) is not None or (
        _PHASE0_AMOUNT_ASSIGNMENT.search(normalized_text) is not None
        or _PHASE0_LARGE_NUMBER_TOKEN.search(normalized_text) is not None
    ):
        raise ValueError(f"{name} contains an amount-like private value")
    try:
        validate_public_risk_string(normalized_text)
    except ValueError:
        raise ValueError(f"{name} contains local implementation details")
    return result


def _normalize_phase0_public_text(value: str, name: str) -> str:
    current = unicodedata.normalize("NFKC", value)
    for _ in range(_PHASE0_TEXT_DECODE_PASSES):
        try:
            decoded = urllib.parse.unquote(current, errors="strict")
        except UnicodeDecodeError:
            raise ValueError(f"{name} contains invalid encoded text") from None
        normalized = unicodedata.normalize("NFKC", decoded)
        if normalized == current:
            return normalized
        current = normalized
    try:
        remaining = urllib.parse.unquote(current, errors="strict")
    except UnicodeDecodeError:
        raise ValueError(f"{name} contains invalid encoded text") from None
    if unicodedata.normalize("NFKC", remaining) != current:
        raise ValueError(f"{name} exceeds the decoding boundary")
    return current


def _decision_route_response(
    context: ApplicationContext,
    mode: RequestMode,
    actions: Tuple[ActionKind, ...],
) -> Dict[str, Any]:
    if context.decision_service is None:
        raise CliUsageError("decision routing service is unavailable")
    budget = RequestBudget.create(mode)
    try:
        snapshot = context.decision_service.route(budget, actions)
        route = snapshot.route
        route.validate()
        data = route.to_canonical_dict()
        data["created_at"] = _utc_iso(snapshot.created_at)
        data["result_checksum"] = route.checksum()
        _validate_decision_route_data(data)
        return data
    except KeyboardInterrupt:
        raise
    except Exception:
        raise DecisionCliError("decision routing failed") from None


def _validate_decision_route_data(data: object) -> None:
    if type(data) is not dict or set(data) != _DECISION_ROUTE_DATA_KEYS:
        raise ValueError("decision route output keys are invalid")
    if type(data["actions"]) is not list or not data["actions"]:
        raise ValueError("decision route actions are invalid")
    if type(data["conclusion_evidence"]) is not list:
        raise ValueError("decision conclusion evidence is invalid")
    actions = tuple(_action_route_from_public(item) for item in data["actions"])
    action_ids = tuple(action.action_id for action in actions)
    has_switch_reduce = "switch_reduce" in action_ids
    has_switch_buy = "switch_buy" in action_ids
    if has_switch_reduce != has_switch_buy:
        raise ValueError("decision switch route must contain both legs")
    if has_switch_reduce:
        reduce_index = action_ids.index("switch_reduce")
        if (
            reduce_index + 1 >= len(action_ids)
            or action_ids[reduce_index + 1] != "switch_buy"
        ):
            raise ValueError("decision switch route legs are not adjacent and ordered")
    conclusion_evidence = tuple(
        _conclusion_evidence_from_public(item) for item in data["conclusion_evidence"]
    )
    route = DecisionRoute(
        request_id=data["request_id"],
        mode=RequestMode(data["mode"]),
        workflow_level=WorkflowLevel(data["workflow_level"]),
        actions=actions,
        conclusion_evidence=conclusion_evidence,
        opposing_evidence=_identifier_list(
            data["opposing_evidence"],
            "opposing evidence",
            allowed_identifiers=_ROUTE_OPPOSING_CODES,
        ),
        missing_fields=_identifier_list(
            data["missing_fields"],
            "missing fields",
            allowed_identifiers=_ROUTE_FIELDS,
        ),
        policy_version=validate_version(data["policy_version"], "policy version"),
        policy_checksum=data["policy_checksum"],
        registry_version=validate_version(
            data["registry_version"], "registry version"
        ),
        registry_checksum=data["registry_checksum"],
    )
    route.validate()
    if not set(route.opposing_evidence).issubset(_ROUTE_OPPOSING_CODES):
        raise ValueError("decision route opposing evidence is invalid")
    if not set(route.missing_fields).issubset(_ROUTE_FIELDS):
        raise ValueError("decision route missing fields are invalid")
    canonical = route.to_canonical_dict()
    published_route = {key: data[key] for key in canonical}
    if published_route != canonical:
        raise ValueError("decision route output is not canonical")
    _validate_utc_iso(data["created_at"], "decision route creation")
    if type(data["result_checksum"]) is not str or (
        data["result_checksum"] != route.checksum()
    ):
        raise ValueError("decision route result checksum is invalid")


def _action_route_from_public(value: object) -> ActionRoute:
    if type(value) is not dict or set(value) != _ACTION_ROUTE_DATA_KEYS:
        raise ValueError("decision action output keys are invalid")
    action = ActionKind(value["action"])
    required_gates = _identifier_list(
        value["required_gates"],
        "required gates",
        allowed_identifiers=_ROUTE_GATE_CODES,
    )
    blocking_codes = _identifier_list(
        value["blocking_codes"],
        "blocking codes",
        allowed_identifiers=_ROUTE_BLOCKING_CODES,
    )
    if required_gates != _ACTION_REQUIRED_GATES[action]:
        raise ValueError("decision action required gates are invalid")
    if not set(blocking_codes).issubset(_ROUTE_BLOCKING_CODES):
        raise ValueError("decision action blocking codes are invalid")
    action_id = _public_identifier(value["action_id"], "action id")
    allowed_action_ids = {
        ActionKind.REDUCE_TO_CASH: {"reduce_to_cash", "switch_reduce"},
        ActionKind.BUY_OR_ADD: {"buy_or_add", "switch_buy"},
    }.get(action, {action.value})
    if action_id not in allowed_action_ids:
        raise ValueError("decision action id is invalid")
    route = ActionRoute(
        action_id=action_id,
        action=action,
        risk_effect=RiskEffect(value["risk_effect"]),
        required_gates=required_gates,
        blocking_codes=blocking_codes,
        research_available=value["research_available"],
        exact_amount_available=value["exact_amount_available"],
        minimum_state=ActionState(value["minimum_state"]),
        action_maturity=ActionMaturity(value["action_maturity"]),
    )
    route.validate()
    if route.to_canonical_dict() != value:
        raise ValueError("decision action output is not canonical")
    return route


def _conclusion_evidence_from_public(value: object) -> ConclusionEvidence:
    if type(value) is not dict or set(value) != _CONCLUSION_EVIDENCE_DATA_KEYS:
        raise ValueError("decision conclusion evidence keys are invalid")
    if type(value["publication_times"]) is not list or len(
        value["publication_times"]
    ) > 128:
        raise ValueError("publication times must be a bounded exact list")
    coverage = value["coverage_percent"]
    if coverage is not None:
        if type(coverage) is not str:
            raise ValueError("coverage percent must be a canonical decimal string")
        try:
            coverage = Decimal(coverage)
        except Exception:
            raise ValueError("coverage percent must be a canonical decimal string") from None
    evidence = ConclusionEvidence(
        source_tier=SourceTier(value["source_tier"]),
        publishers=_public_text_list(value["publishers"], "publishers"),
        source_ids=_identifier_list(value["source_ids"], "source ids"),
        publication_times=tuple(
            _parse_utc_iso(item, "publication time")
            for item in value["publication_times"]
        ),
        market_as_of=_parse_utc_iso(
            value["market_as_of"], "market as of", optional=True
        ),
        report_as_of=_parse_utc_iso(
            value["report_as_of"], "report as of", optional=True
        ),
        retrieved_at=_parse_utc_iso(value["retrieved_at"], "retrieved at"),
        independent_lineage_count=value["independent_lineage_count"],
        lineage_ids=_identifier_list(value["lineage_ids"], "lineage ids"),
        completeness=EvidenceCompleteness(value["completeness"]),
        coverage_percent=coverage,
        freshness=EvidenceFreshness(value["freshness"]),
        conflicts=_identifier_list(value["conflicts"], "conflicts"),
        inferred=value["inferred"],
        missing_critical_fields=_identifier_list(
            value["missing_critical_fields"], "missing critical fields"
        ),
    )
    evidence.validate()
    if evidence.to_canonical_dict() != value:
        raise ValueError("decision conclusion evidence is not canonical")
    return evidence


def _source_status_response(
    context: ApplicationContext,
    fund_code: Optional[str],
) -> Dict[str, Any]:
    if fund_code is not None:
        _validate_fund_code(fund_code)
    if context.source_health_service is None:
        raise CliUsageError("source health service is unavailable")
    try:
        service = context.source_health_service
        store = service.audit_store
        budget = RequestBudget.create(RequestMode.RAPID)
        request_run_id = store.begin_request(budget)
    except Exception:
        raise SourceStatusCliError("source status failed") from None
    finalized = False
    subject_key = None if fund_code is None else f"fund:{fund_code}"
    try:
        context_value = FreshnessContext(now=budget.started_at)
        source_fields = []
        primary_by_field: Dict[str, Tuple[int, str]] = {}
        tier_priority = {
            SourceTier.TIER_1: 0,
            SourceTier.TIER_2: 1,
            SourceTier.PRIVATE_OBSERVATION: 2,
            SourceTier.USER_PROVIDED: 3,
        }
        for source in service.registry.sources:
            for field in source.fields:
                candidate = (tier_priority[field.source_tier], source.source_id)
                if field.field_id not in primary_by_field or (
                    candidate < primary_by_field[field.field_id]
                ):
                    primary_by_field[field.field_id] = candidate

        ordered_field_ids = tuple(sorted(primary_by_field))
        primary_references = tuple(
            SourceFieldRef(primary_by_field[field_id][1], field_id)
            for field_id in ordered_field_ids
        )
        requirements = tuple(
            service.action_requirement(
                field_id,
                ActionKind.FACT_RESEARCH,
                RiskEffect.INFORMATION,
            )
            for field_id in ordered_field_ids
        )
        snapshot = (
            None
            if subject_key is None
            else service.source_status_snapshot(
                subject_key,
                context_value,
                primary_references,
                requirements,
                request_run_id=request_run_id,
                budget=budget,
            )
        )
        projected_by_reference = (
            {}
            if snapshot is None
            else {
                projection.history.reference: projection
                for projection in snapshot.projections
            }
        )

        for source in service.registry.sources:
            for field in source.fields:
                if subject_key is None:
                    state = SourceFieldState.NOT_CHECKED
                    attempts = ()
                else:
                    projection = projected_by_reference[
                        SourceFieldRef(source.source_id, field.field_id)
                    ]
                    state = projection.state
                    attempts = tuple(
                        record.attempt for record in projection.history.attempts
                    )
                successful = tuple(
                    attempt
                    for attempt in attempts
                    if attempt.outcome
                    in {SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.CACHE_HIT}
                )
                failed = tuple(
                    attempt
                    for attempt in attempts
                    if attempt.outcome in _SOURCE_FAILURE_OUTCOMES
                )
                consecutive_failures = 0
                for attempt in attempts:
                    if attempt.outcome in {
                        SourceAttemptOutcome.SUCCESS,
                        SourceAttemptOutcome.CACHE_HIT,
                    }:
                        break
                    if attempt.outcome in _SOURCE_FAILURE_OUTCOMES:
                        consecutive_failures += 1
                latest = attempts[0] if attempts else None
                last_success = successful[0] if successful else None
                last_failure = failed[0] if failed else None
                source_fields.append(
                    {
                        "acceptable_alternatives": [
                            item.to_canonical_dict()
                            for item in field.acceptable_alternatives
                        ],
                        "consecutive_failures": consecutive_failures,
                        "cooldown_until": (
                            _utc_iso(latest.cooldown_until)
                            if state is SourceFieldState.COOLDOWN
                            and latest is not None
                            and latest.cooldown_until is not None
                            else None
                        ),
                        "field_id": field.field_id,
                        "field_scope": field.scope,
                        "last_failure_at": (
                            None
                            if last_failure is None
                            else _utc_iso(last_failure.finished_at)
                        ),
                        "last_failure_reason": (
                            None
                            if last_failure is None or last_failure.error_code is None
                            else last_failure.error_code.value
                        ),
                        "last_success_at": (
                            None
                            if last_success is None
                            else _utc_iso(last_success.finished_at)
                        ),
                        "last_success_data_as_of": (
                            None
                            if last_success is None or last_success.data_as_of is None
                            else _utc_iso(last_success.data_as_of)
                        ),
                        "source_id": source.source_id,
                        "source_kind": source.source_kind,
                        "source_scope": source.scope,
                        "source_tier": field.source_tier.value,
                        "state": state.value,
                        "supplementation": field.supplementation.to_canonical_dict(),
                    }
                )
        request_field_resolutions = []
        snapshot_resolutions = (
            (RequestFieldResolution.PARTIAL,) * len(ordered_field_ids)
            if snapshot is None
            else snapshot.resolutions
        )
        for field_id, resolution in zip(ordered_field_ids, snapshot_resolutions):
            source_id = primary_by_field[field_id][1]
            request_field_resolutions.append(
                {
                    "action": ActionKind.FACT_RESEARCH.value,
                    "field_id": field_id,
                    "primary_source_id": source_id,
                    "resolution": resolution.value,
                    "risk_effect": RiskEffect.INFORMATION.value,
                }
            )

        data = {
            "fund_code": fund_code,
            "mode": RequestMode.RAPID.value,
            "policy_checksum": service.policy.checksum(),
            "policy_version": service.policy.version,
            "registry_checksum": service.registry.checksum(),
            "registry_version": service.registry.version,
            "request_field_resolutions": request_field_resolutions,
            "request_id": budget.request_id,
            "snapshot_at": _utc_iso(
                budget.started_at if snapshot is None else snapshot.evaluated_at
            ),
            "source_fields": source_fields,
        }
        _validate_source_status_data(data)
        budget.require_publishable()
        finished_at = _request_finish_now()
        if finished_at < budget.started_at or finished_at > budget.deadline_at:
            raise ValueError("source status request time is invalid")
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.COMPLETE,
            finished_at,
            (),
            budget=budget,
        )
        finalized = True
        return data
    except KeyboardInterrupt:
        _finalize_source_status_failure(
            store,
            request_run_id,
            budget,
            RequestTerminalStatus.CANCELLED,
        )
        raise
    except BudgetExpired:
        terminal_status = (
            RequestTerminalStatus.CANCELLED
            if budget.cancelled
            else RequestTerminalStatus.EXPIRED
        )
        _finalize_source_status_failure(
            store,
            request_run_id,
            budget,
            terminal_status,
        )
        raise SourceStatusCliError("source status failed") from None
    except Exception:
        if not finalized:
            _finalize_source_status_failure(
                store,
                request_run_id,
                budget,
                RequestTerminalStatus.FAILED,
            )
        raise SourceStatusCliError("source status failed") from None


def _finalize_source_status_failure(
    store: DecisionAuditStore,
    request_run_id: int,
    budget: RequestBudget,
    status: RequestTerminalStatus,
) -> None:
    finished_at = (
        budget.deadline_at
        if status is RequestTerminalStatus.EXPIRED
        else min(max(_request_finish_now(), budget.started_at), budget.deadline_at)
    )
    try:
        store.finalize_request(request_run_id, status, finished_at, ())
    except Exception:
        pass


def _validate_source_status_data(data: object) -> None:
    if type(data) is not dict or set(data) != _SOURCE_STATUS_DATA_KEYS:
        raise ValueError("source status output keys are invalid")
    if data["mode"] != RequestMode.RAPID.value:
        raise ValueError("source status mode is invalid")
    if data["fund_code"] is not None:
        if type(data["fund_code"]) is not str:
            raise ValueError("source status fund code is invalid")
        _validate_fund_code(data["fund_code"])
    if type(data["request_id"]) is not str or not _REQUEST_ID.fullmatch(
        data["request_id"]
    ):
        raise ValueError("source status request id is invalid")
    snapshot_at = _parse_utc_iso(data["snapshot_at"], "source status snapshot")
    if snapshot_at is None:
        raise ValueError("source status snapshot is required")
    policy = EvidencePolicyV1()
    registry = SourceRegistryV1()
    if (
        data["policy_version"] != policy.version
        or data["policy_checksum"] != policy.checksum()
        or data["registry_version"] != registry.version
        or data["registry_checksum"] != registry.checksum()
    ):
        raise ValueError("source status policy or registry binding is invalid")
    if type(data["source_fields"]) is not list or not data["source_fields"]:
        raise ValueError("source status fields are invalid")
    registry_fields = {
        (source.source_id, field.field_id): (source, field)
        for source in registry.sources
        for field in source.fields
    }
    identities = []
    for item in data["source_fields"]:
        if type(item) is not dict or set(item) != _SOURCE_FIELD_DATA_KEYS:
            raise ValueError("source field output keys are invalid")
        source_id = _public_identifier(item["source_id"], "source id")
        field_id = _public_identifier(item["field_id"], "field id")
        identity = (source_id, field_id)
        if identity not in registry_fields:
            raise ValueError("source field is not declared by the active registry")
        source, field = registry_fields[identity]
        state = SourceFieldState(item["state"])
        if SourceTier(item["source_tier"]) is not field.source_tier:
            raise ValueError("source field tier differs from the active registry")
        if (
            item["source_kind"] != source.source_kind
            or item["source_scope"] != source.scope
            or item["field_scope"] != field.scope
            or item["acceptable_alternatives"]
            != [reference.to_canonical_dict() for reference in field.acceptable_alternatives]
            or item["supplementation"] != field.supplementation.to_canonical_dict()
        ):
            raise ValueError("source field static contract differs from the registry")
        if (
            type(item["consecutive_failures"]) is not int
            or not 0 <= item["consecutive_failures"] <= 64
        ):
            raise ValueError("source consecutive failures are invalid")
        cooldown_until = _parse_utc_iso(
            item["cooldown_until"], "cooldown until", optional=True
        )
        last_failure_at = _parse_utc_iso(
            item["last_failure_at"], "last failure at", optional=True
        )
        last_success_at = _parse_utc_iso(
            item["last_success_at"], "last success at", optional=True
        )
        last_success_data_as_of = _parse_utc_iso(
            item["last_success_data_as_of"],
            "last success data as of",
            optional=True,
        )
        failure_reason = item["last_failure_reason"]
        if failure_reason is not None:
            failure_reason = SourceErrorCode(failure_reason)
            if failure_reason in {
                SourceErrorCode.COOLDOWN_ACTIVE,
                SourceErrorCode.REQUEST_CANCELLED,
                SourceErrorCode.REQUEST_EXPIRED,
            }:
                raise ValueError("source failure reason is not a true source failure")
        if (last_failure_at is None) != (failure_reason is None):
            raise ValueError("source failure time and reason must be paired")
        if item["consecutive_failures"] > 0 and last_failure_at is None:
            raise ValueError("source consecutive failures require a failure record")
        if (last_success_at is None) != (last_success_data_as_of is None):
            raise ValueError("source success time and data date must be paired")
        if (
            last_success_at is not None
            and last_success_data_as_of is not None
            and last_success_data_as_of > last_success_at
        ):
            raise ValueError("source data date cannot follow its successful retrieval")
        if any(
            timestamp is not None and timestamp > snapshot_at
            for timestamp in (last_failure_at, last_success_at)
        ):
            raise ValueError("source attempt time cannot follow its snapshot")
        if (state is SourceFieldState.COOLDOWN) != (cooldown_until is not None):
            raise ValueError("source cooldown state and expiry must be paired")
        if state is SourceFieldState.NOT_CHECKED and any(
            value is not None
            for value in (
                last_failure_at,
                last_success_at,
                cooldown_until,
            )
        ):
            raise ValueError("not checked source cannot expose attempt history")
        if state is SourceFieldState.NOT_CHECKED and item["consecutive_failures"] != 0:
            raise ValueError("not checked source cannot have consecutive failures")
        if state in {SourceFieldState.HEALTHY, SourceFieldState.DEGRADED} and (
            last_success_at is None
        ):
            raise ValueError("usable source state requires a successful retrieval")
        if state in {SourceFieldState.COOLDOWN, SourceFieldState.UNSUPPORTED} and (
            last_failure_at is None
        ):
            raise ValueError("failed source state requires a true source failure")
        if state is SourceFieldState.COOLDOWN and (
            failure_reason not in TRANSIENT_SOURCE_ERRORS
            or item["consecutive_failures"] <= 0
            or cooldown_until is None
            or cooldown_until <= snapshot_at
        ):
            raise ValueError("cooldown state requires an active transient failure")
        if state is SourceFieldState.UNSUPPORTED and (
            failure_reason not in UNSUPPORTED_SOURCE_ERRORS
            or item["consecutive_failures"] <= 0
            or cooldown_until is not None
        ):
            raise ValueError("unsupported state requires a permanent source failure")
        if state is SourceFieldState.UNAVAILABLE and (
            last_failure_at is None and last_success_at is None
        ):
            raise ValueError("unavailable source requires failure or stale evidence")
        identities.append(identity)
    if len(identities) != len(set(identities)) or set(identities) != set(
        registry_fields
    ):
        raise ValueError("source status identities are invalid")
    if (
        type(data["request_field_resolutions"]) is not list
        or not data["request_field_resolutions"]
    ):
        raise ValueError("request field resolutions are invalid")
    fields = []
    tier_priority = {
        SourceTier.TIER_1: 0,
        SourceTier.TIER_2: 1,
        SourceTier.PRIVATE_OBSERVATION: 2,
        SourceTier.USER_PROVIDED: 3,
    }
    expected_primary = {}
    for identity, (_source, field) in registry_fields.items():
        candidate = (tier_priority[field.source_tier], identity[0])
        if field.field_id not in expected_primary or candidate < expected_primary[
            field.field_id
        ]:
            expected_primary[field.field_id] = candidate
    for item in data["request_field_resolutions"]:
        if type(item) is not dict or set(item) != _REQUEST_FIELD_RESOLUTION_KEYS:
            raise ValueError("request field resolution output keys are invalid")
        field_id = _public_identifier(item["field_id"], "resolution field id")
        primary_source_id = _public_identifier(
            item["primary_source_id"], "resolution primary source id"
        )
        RequestFieldResolution(item["resolution"])
        if item["action"] != ActionKind.FACT_RESEARCH.value or (
            item["risk_effect"] != RiskEffect.INFORMATION.value
        ):
            raise ValueError("request field resolution action contract is invalid")
        if (
            field_id not in expected_primary
            or primary_source_id != expected_primary[field_id][1]
        ):
            raise ValueError("request field resolution primary is invalid")
        fields.append(field_id)
    if fields != sorted(set(fields)):
        raise ValueError("request field resolutions are not canonical")
    if set(fields) != set(expected_primary):
        raise ValueError("request field resolutions are incomplete")


def execute(args: argparse.Namespace, context: ApplicationContext) -> Dict[str, Any]:
    if args.command == "version":
        return envelope("version", {"version": __version__})

    if args.command == "decision" and args.decision_command == "route":
        if not args.json_output:
            raise CliUsageError("decision route requires JSON mode")
        action_values = tuple(args.action)
        if len(action_values) != len(set(action_values)):
            raise CliUsageError("decision route actions must be unique")
        actions = tuple(ActionKind(value) for value in action_values)
        return envelope(
            "decision.route",
            _decision_route_response(
                context,
                RequestMode(args.mode),
                actions,
            ),
        )

    if args.command == "source" and args.source_command == "status":
        if not args.json_output:
            raise CliUsageError("source status requires JSON mode")
        return envelope(
            "source.status",
            _source_status_response(context, args.fund_code),
        )

    if args.command == "fund" and args.fund_command == "brief":
        if not args.json_output:
            raise CliUsageError("fund brief requires JSON mode")
        return envelope(
            "fund.brief",
            _fund_brief_response(
                context,
                args.fund_code,
                ActionKind(args.action),
                RequestMode(args.mode),
            ),
        )

    if args.command == "fund" and args.fund_command == "holding-review":
        if not args.json_output:
            raise CliUsageError("fund holding-review requires JSON mode")
        if context.holding_review_service is None:
            raise CliUsageError("fund holding-review service is unavailable")
        _validate_fund_code(args.fund_code)
        outcome = context.holding_review_service.review(
            args.fund_code,
            action=ActionKind(args.action),
            brief_request_run_id=args.brief_request_run_id,
            intelligence_request_run_id=args.intelligence_request_run_id,
            remainder_intent=RemainderIntent(args.remainder_intent),
            exit_reason=ExitReason(args.exit_reason),
            use_of_proceeds=UseOfProceeds(args.use_of_proceeds),
        )
        return envelope(
            "fund.holding-review",
            public_holding_review_payload(outcome),
        )

    if args.command == "fund" and args.fund_command == "review":
        if not args.json_output:
            raise CliUsageError("fund review requires JSON mode")
        _validate_fund_code(args.fund_code)
        if context.brief_service is None or context.intelligence_service is None:
            raise CliUsageError("fund review services are unavailable")
        from kunjin.brief.research import public_outcome_payload
        from kunjin.intelligence.research import public_intelligence_payload

        brief = public_outcome_payload(
            context.brief_service.brief_outcome(
                args.fund_code,
                action=ActionKind(args.action),
                mode=RequestMode.RAPID,
            )
        )
        intelligence = summarize_public_research(
            public_intelligence_payload(
                context.intelligence_service.fund_intelligence(
                    args.fund_code, window="recent", mode="rapid"
                )
            )
        )
        market_scan = scan_public_research(
            public_intelligence_payload(
                context.intelligence_service.market_overview(window="recent", mode="rapid")
            )
        )
        portfolio = None
        if args.manual_position or args.portfolio_context == "cached":
            diagnosis_service = context.diagnosis_service or DiagnosisService(
                context.repository,
                context.fund_disclosure_store or FundDisclosureStore(context.repository),
            )
            review = PortfolioReviewService(
                sync_service=context.sync_service,
                diagnosis_service=diagnosis_service,
                disclosure_store=context.fund_disclosure_store
                or FundDisclosureStore(context.repository),
            )
            portfolio = (
                review.manual(
                    tuple(
                        _manual_portfolio_position(item)
                        for item in args.manual_position
                    )
                )
                if args.manual_position
                else {
                    "input_source": "cached",
                    "diagnosis": public_diagnosis_payload(
                        diagnosis_service.diagnose()
                    ),
                }
            )
        guardrails = build_investor_guardrails(
            emergency_fund=args.emergency_fund,
            near_term_use=args.near_term_use,
            horizon=args.horizon,
            volatility=args.risk_tolerance,
            portfolio=portfolio,
        )
        positions = context.repository.latest_positions()
        analysis = analyze_portfolio(positions)
        weights = {
            code: format(weight, "f") for code, weight in analysis.weights.items()
        }
        portfolio_weight_context = (
            build_portfolio_weight_context(
                fund_code=args.fund_code,
                weights=weights,
                value_basis=analysis.value_kind,
            )
            if args.portfolio_context == "cached" or args.related_fund
            else None
        )
        related_fund_context = None
        if args.related_fund:
            related_codes = _related_fund_codes(args.fund_code, args.related_fund)
            bundles, histories, related_positions = _comparison_inputs(context, related_codes)
            comparison = build_explicit_compare_report(
                related_codes,
                bundles,
                histories,
                related_positions,
                datetime.now(timezone.utc),
            )
            related_fund_context = build_related_fund_context(
                fund_codes=related_codes,
                weights=weights,
                comparison=comparison,
            )
        return envelope(
            "fund.review",
            build_fund_review(
                fund_code=args.fund_code,
                action=args.action,
                brief=brief,
                intelligence=intelligence,
                market_scan=market_scan,
                portfolio=portfolio,
                horizon=args.horizon,
                risk_tolerance=args.risk_tolerance,
                near_term_use=args.near_term_use,
                guardrails=guardrails,
                portfolio_weight_context=portfolio_weight_context,
                related_fund_context=related_fund_context,
            ),
        )

    if args.command == "fund" and args.fund_command == "review-triggers":
        if not args.json_output:
            raise CliUsageError("fund review-triggers requires JSON mode")
        _validate_fund_code(args.fund_code)
        return envelope("fund.review-triggers", build_review_triggers(args.fund_code))

    if args.command == "investor" and args.investor_command == "guardrails":
        if not args.json_output:
            raise CliUsageError("investor guardrails requires JSON mode")
        portfolio = None
        if args.portfolio_context == "cached":
            diagnosis_service = context.diagnosis_service or DiagnosisService(
                context.repository,
                context.fund_disclosure_store or FundDisclosureStore(context.repository),
            )
            diagnosis = public_diagnosis_payload(diagnosis_service.diagnose())
            portfolio = {
                "portfolio_overview": diagnosis["concentration"],
                "observed_exposures": diagnosis["relationships"],
                "coverage": diagnosis["coverage"],
                "missing_evidence": diagnosis["missing_evidence"],
            }
        return envelope(
            "investor.guardrails",
            build_investor_guardrails(
                emergency_fund=args.emergency_fund,
                near_term_use=args.near_term_use,
                horizon=args.horizon,
                volatility=args.volatility,
                portfolio=portfolio,
            ),
        )

    if args.command == "thesis" and args.thesis_command == "match-project":
        if not args.json_output:
            raise CliUsageError("thesis match-project requires JSON mode")
        if context.thesis_review_service is None:
            raise CliUsageError("thesis review service is unavailable")
        _validate_fund_code(args.fund_code)
        stored = context.thesis_review_service.match_project(
            args.fund_code,
            args.intelligence_request_run_id,
        )
        return envelope(
            "thesis.match-project",
            {"id": stored.id, "projection": stored.value.to_canonical_dict()},
        )

    if args.command == "thesis" and args.thesis_command == "adjudicate":
        if not args.json_output:
            raise CliUsageError("thesis adjudicate requires JSON mode")
        if context.thesis_review_service is None:
            raise CliUsageError("thesis review service is unavailable")
        _validate_fund_code(args.fund_code)
        stored = context.thesis_review_service.adjudicate(
            args.fund_code,
            args.thesis_match_projection_id,
            AdjudicationDecision(args.decision),
            supersedes_id=args.supersedes,
        )
        return envelope(
            "thesis.adjudicate",
            {"id": stored.id, "adjudication": stored.value.to_canonical_dict()},
        )

    if (
        (args.command == "news" and args.news_command == "recent")
        or (args.command == "market" and args.market_command == "overview")
        or (args.command == "fund" and args.fund_command == "intelligence")
    ):
        if not args.json_output:
            raise CliUsageError("intelligence research commands require JSON mode")
        nested = getattr(args, f"{args.command}_command")
        return envelope(f"{args.command}.{nested}", _intelligence_response(context, args))

    if args.command == "research" and args.research_command == "summary":
        if not args.json_output:
            raise CliUsageError("research summary requires JSON mode")
        return envelope("research.summary", _research_summary_response(context, args))

    if args.command == "research" and args.research_command == "scan":
        if not args.json_output:
            raise CliUsageError("research scan requires JSON mode")
        return envelope("research.scan", _research_scan_response(context, args))

    if args.command == "research" and args.research_command == "panorama":
        if not args.json_output:
            raise CliUsageError("research panorama requires JSON mode")
        return envelope("research.panorama", _research_panorama_response(context))

    if args.command == "research" and args.research_command == "supplement":
        if not args.json_output:
            raise CliUsageError("research supplement requires JSON mode")
        return envelope("research.supplement", _research_supplement_response(args))

    if args.command == "research" and args.research_command == "supplement-timeline":
        if not args.json_output:
            raise CliUsageError("research supplement timeline requires JSON mode")
        return envelope(
            "research.supplement_timeline",
            _research_supplement_timeline_response(args),
        )

    if args.command == "research" and args.research_command == "evidence-store":
        if not args.json_output:
            raise CliUsageError("research evidence store requires JSON mode")
        return envelope(
            "research.evidence_store", _research_evidence_store_response(context, args)
        )

    if args.command == "research" and args.research_command == "evidence-timeline":
        if not args.json_output:
            raise CliUsageError("research evidence timeline requires JSON mode")
        return envelope(
            "research.evidence_timeline",
            _research_evidence_timeline_response(context, args),
        )

    if args.command == "research" and args.research_command == "evidence-refresh-plan":
        if not args.json_output:
            raise CliUsageError("research evidence refresh plan requires JSON mode")
        return envelope(
            "research.evidence_refresh_plan",
            _research_evidence_refresh_plan_response(context, args),
        )

    if args.command == "research" and args.research_command == "event-store":
        if not args.json_output:
            raise CliUsageError("research event store requires JSON mode")
        return envelope("research.event_store", _research_event_store_response(context, args))

    if args.command == "research" and args.research_command == "event-timeline":
        if not args.json_output:
            raise CliUsageError("research event timeline requires JSON mode")
        return envelope(
            "research.event_timeline", _research_event_timeline_response(context, args)
        )

    if args.command == "profile":
        if args.profile_command == "edit" and args.json_output:
            raise CliUsageError("profile edit is interactive and does not support JSON mode")
        if context.profile_service is None:
            raise CliUsageError("profile service is unavailable")
        if args.profile_command == "edit":
            result = ProfileEditor(context.profile_service).edit()
            return envelope("profile.edit", result)
        if args.profile_command == "status":
            return envelope("profile.status", context.profile_service.status())
        if args.profile_command == "history":
            return envelope(
                "profile.history",
                {"profiles": context.profile_service.history()},
            )

    if args.command == "suitability":
        if context.suitability_service is None:
            raise CliUsageError("suitability service is unavailable")
        if args.suitability_command == "assess":
            execution = context.suitability_service.assess()
            data = execution.safe_json() if args.json_output else execution.local_view()
            return envelope("suitability.assess", data)
        if args.suitability_command == "status":
            return envelope(
                "suitability.status",
                context.suitability_service.status(),
            )
        return envelope(
            "suitability.history",
            {"assessments": context.suitability_service.history()},
        )

    if args.command == "allocation":
        if context.allocation_service is None:
            raise CliUsageError("allocation service is unavailable")
        if args.allocation_command == "ranges":
            return envelope(
                "allocation.ranges",
                _allocation_ranges_response(context.allocation_service, args.json_output),
            )
        if args.allocation_command == "status":
            return envelope(
                "allocation.status",
                _allocation_safe_response(context.allocation_service.status, "status"),
            )
        if args.allocation_command == "history":
            return envelope(
                "allocation.history",
                _allocation_safe_response(context.allocation_service.history, "history"),
            )
        return envelope(
            "allocation.policy",
            _allocation_safe_response(context.allocation_service.policy, "policy"),
        )

    if args.command == "auth" and args.auth_command == "status":
        return envelope(
            "auth.status",
            {"yangjibao_authorized": context.token_store.load() is not None},
        )

    if args.command == "auth" and args.auth_command == "revoke":
        context.token_store.delete()
        return envelope("auth.revoke", {"provider": args.provider, "revoked": True})

    if args.command == "auth" and args.auth_command == "login":
        if args.json_output:
            return envelope(
                "auth.login",
                errors=[
                    {
                        "code": "interactive_required",
                        "message": "Run QR authorization without --json",
                    }
                ],
            )
        challenge = context.client.start_qr_login()
        rendered = context.client.render_qr(challenge.qr_content)
        if not rendered:
            print("QR renderer is unavailable. Install the optional 'qr' extra.", file=sys.stderr)
            print(f"First-party QR content: {challenge.qr_content}", file=sys.stderr)
        context.client.poll_qr_login(challenge.challenge_id)
        return envelope("auth.login", {"provider": args.provider, "authorized": True})

    if args.command == "ledger" and args.ledger_command == "import":
        draft = context.ledger_service.import_image(args.image, fund_code_hint=args.fund_code)
        fields = context.ledger_service.store.list_ocr_fields(draft.source_document_id)
        fields_by_name = {field.name: field for field in fields}
        public_fields = {
            field.name: {
                "normalized_value": field.normalized_value,
                "confidence": str(field.confidence),
                "evidence_level": field.evidence_level.value,
            }
            for field in fields
        }
        return envelope(
            "ledger.import",
            {
                "document_id": draft.source_document_id,
                "draft": serialize(draft),
                "requires_confirmation": requires_confirmation(fields_by_name),
                "fields": public_fields,
            },
        )

    if args.command == "ledger" and args.ledger_command == "drafts":
        drafts = context.ledger_service.store.list_drafts()
        return envelope("ledger.drafts", {"drafts": serialize(drafts)})

    if args.command == "ledger" and args.ledger_command == "confirm":
        transaction = context.ledger_service.confirm_draft(
            args.draft_id, parse_field_overrides(args.field)
        )
        return envelope("ledger.confirm", {"transaction": serialize(transaction)})

    if args.command == "ledger" and args.ledger_command == "add":
        transaction = context.ledger_service.add_manual_transaction(
            transaction_type=args.transaction_type,
            fund_code=args.fund_code,
            fund_name=args.fund_name,
            amount=args.amount,
            shares=args.shares,
            nav=args.nav,
            fee=args.fee,
            order_time=args.order_time,
            confirmation_time=args.confirmation_time,
        )
        return envelope("ledger.add", {"transaction": serialize(transaction)})

    if args.command == "ledger" and args.ledger_command == "transactions":
        transactions = context.ledger_service.store.list_transactions(args.fund_code)
        return envelope("ledger.transactions", {"transactions": serialize(transactions)})

    if args.command == "ledger" and args.ledger_command == "reconcile":
        if not _FUND_CODE.fullmatch(args.fund_code):
            raise LedgerImportError("invalid_fund_code", "fund code must contain six digits")
        positions = [
            position
            for position in context.repository.latest_positions()
            if position.fund_code == args.fund_code
        ]
        if not positions:
            return envelope(
                "ledger.reconcile",
                errors=[
                    {
                        "code": "position_not_found",
                        "message": "No synchronized position is available for this fund",
                    }
                ],
            )
        account_titles = sorted({position.account_title for position in positions})
        if len(account_titles) > 1:
            return envelope(
                "ledger.reconcile",
                {"account_titles": account_titles},
                errors=[
                    {
                        "code": "ambiguous_position_accounts",
                        "message": (
                            "Multiple accounts hold this fund; account selection is required"
                        ),
                    }
                ],
            )
        position = max(
            positions,
            key=lambda item: (item.observed_at, item.account_title),
        )
        result = reconcile_fund(
            position,
            context.ledger_service.store.list_transactions(args.fund_code),
            context.ledger_service.store.list_drafts(),
        )
        return envelope(
            "ledger.reconcile",
            {"result": serialize(result)},
            warnings=list(result.warnings),
        )

    if (
        args.command == "ledger"
        and args.ledger_command == "document"
        and args.document_command == "delete"
    ):
        deleted = context.ledger_service.delete_document(args.document_id)
        warnings = [] if deleted else ["document is not active or does not exist"]
        return envelope(
            "ledger.document.delete",
            {"document_id": args.document_id, "deleted": deleted},
            warnings=warnings,
        )

    if args.command == "sync" and args.sync_command == "fund-documents":
        _validate_fund_code(args.fund_code)
        service = _fund_risk_service(context)
        result = _risk_call(
            lambda: service.sync_documents(args.fund_code),
            "official_document_invalid",
        )
        return envelope(
            "sync.fund-documents",
            _risk_sync_report(result),
            errors=_risk_sync_errors(result),
        )

    if args.command == "fund" and args.fund_command == "classify":
        _validate_fund_code(args.fund_code)
        service = _fund_risk_service(context)
        return envelope(
            "fund.classify",
            _risk_classify_report(service, args.fund_code),
        )

    if args.command == "fund" and args.fund_command in {
        "classification",
        "classification-evidence",
    }:
        _validate_fund_code(args.fund_code)
        service = _fund_risk_service(context)
        report = _risk_current_report(service, args.fund_code)
        return envelope(
            f"fund.{args.fund_command}",
            _missing_risk_report(args.fund_code) if report is None else report,
        )

    if args.command == "fund" and args.fund_command == "classification-history":
        _validate_fund_code(args.fund_code)
        service = _fund_risk_service(context)
        return envelope(
            "fund.classification-history",
            _risk_history_report(service, args.fund_code),
        )

    if args.command == "fund" and args.fund_command == "classification-policy":
        return envelope(
            "fund.classification-policy",
            _risk_policy_report(),
        )

    if args.command == "fund" and args.fund_command == "converter-status":
        service = _fund_risk_service(context)
        return envelope(
            "fund.converter-status",
            _risk_converter_status_report(service),
        )

    latest_sync = context.repository.latest_successful_sync("yangjibao")
    data_freshness = freshness(None if latest_sync is None else latest_sync.get("finished_at"))

    if args.command == "sync" and args.sync_command == "portfolio":
        result = context.sync_service.sync_portfolio(trigger="manual")
        return envelope("sync.portfolio", serialize(result))

    if args.command == "sync" and args.sync_command == "fund":
        _validate_fund_code(args.fund_code)
        result = context.research_service.sync_fund(args.fund_code)
        return envelope("sync.fund", serialize(result))

    if args.command == "sync" and args.sync_command in {
        "fund-profile",
        "fund-holdings",
    }:
        profile = args.sync_command == "fund-profile"
        mode = RequestMode(args.mode)
        if args.force:
            if mode is not RequestMode.DEEP:
                raise CliUsageError("force requires deep mode")
            if args.force_reason is None:
                raise CliUsageError("force requires an allowlisted non-empty reason")
            force_reason = ForceReasonCode(args.force_reason)
        else:
            if args.force_reason is not None:
                raise CliUsageError("force reason requires --force")
            force_reason = None
        result = _sync_disclosure(
            context,
            args.fund_code,
            profile,
            mode=mode,
            force_reason=force_reason,
        )
        command = f"sync.{args.sync_command}"
        errors = []
        if not result["successful"]:
            errors.append(
                {
                    "code": "fund_disclosure_sync_failed",
                    "message": "No requested disclosure section synchronized successfully",
                }
            )
        return envelope(command, result["data"], errors=errors)

    if args.command == "sync" and args.sync_command == "fund-peers":
        _validate_fund_code(args.fund_code)
        candidates = tuple(args.candidate)
        for candidate in candidates:
            _validate_fund_code(candidate)
        if len(candidates) != len(set(candidates)):
            raise CliUsageError("candidate fund codes must be unique")
        if context.peer_service is None:
            raise ValueError("peer research service is unavailable")
        result = context.peer_service.sync_peers(args.fund_code, user_candidates=candidates)
        data = serialize(result)
        data["errors"] = serialize(result.errors)
        errors: List[Dict[str, str]] = []
        if result.status.value == "source_unavailable":
            error_code = next(reversed(result.warnings), "peer_sync_failed")
            errors.append(
                {
                    "code": error_code,
                    "message": "Peer group synchronization did not produce a new usable group",
                }
            )
        return envelope(
            "sync.fund-peers",
            data,
            warnings=list(result.warnings),
            errors=errors,
        )

    if args.command == "sync" and args.sync_command == "market":
        result = context.research_service.sync_market()
        return envelope("sync.market", serialize(result))

    if args.command == "sync" and args.sync_command == "daily":
        results: Dict[str, Any] = {}
        errors: List[Dict[str, str]] = []
        try:
            results["portfolio"] = serialize(
                context.sync_service.sync_portfolio(trigger="scheduled")
            )
        except Exception as exc:
            errors.append(
                {
                    "code": str(getattr(exc, "code", "portfolio_sync_failed")),
                    "message": redact_secrets(str(exc)),
                }
            )
        fund_results: Dict[str, Any] = {}
        for fund_code in sorted(
            {position.fund_code for position in context.repository.latest_positions()}
        ):
            fund_result: Dict[str, Any] = {}
            try:
                fund_result["nav"] = serialize(context.research_service.sync_fund(fund_code))
            except Exception as exc:
                errors.append(
                    {
                        "code": str(getattr(exc, "code", "fund_sync_failed")),
                        "message": f"{fund_code}: {redact_secrets(str(exc))}",
                    }
                )
            if context.fund_disclosure_service is not None:
                try:
                    if _sections_due(
                        context.fund_disclosure_service,
                        fund_code,
                        PROFILE_SECTIONS,
                    ):
                        disclosure_result = context.fund_disclosure_service.sync_profile(fund_code)
                        fund_result["profile"] = serialize(disclosure_result)
                        _append_section_sync_errors(errors, fund_code, disclosure_result)
                    else:
                        fund_result["profile"] = {"status": "fresh"}
                except Exception as exc:
                    errors.append(
                        {
                            "code": str(getattr(exc, "code", "fund_profile_sync_failed")),
                            "message": f"{fund_code}: {redact_secrets(str(exc))}",
                        }
                    )
                try:
                    if _sections_due(
                        context.fund_disclosure_service,
                        fund_code,
                        HOLDING_SECTIONS,
                    ):
                        disclosure_result = context.fund_disclosure_service.sync_holdings(fund_code)
                        fund_result["holdings"] = serialize(disclosure_result)
                        _append_section_sync_errors(errors, fund_code, disclosure_result)
                    else:
                        fund_result["holdings"] = {"status": "fresh"}
                except Exception as exc:
                    errors.append(
                        {
                            "code": str(getattr(exc, "code", "fund_holdings_sync_failed")),
                            "message": f"{fund_code}: {redact_secrets(str(exc))}",
                        }
                    )
            fund_results[fund_code] = fund_result
        results["funds"] = fund_results
        if context.peer_service is not None:
            try:
                peer_results = context.peer_service.refresh_existing_groups()
                results["peer_groups"] = serialize(peer_results)
                _append_peer_refresh_errors(errors, peer_results)
            except Exception as exc:
                results["peer_groups"] = {}
                errors.append(
                    {
                        "code": str(getattr(exc, "code", "peer_group_refresh_failed")),
                        "message": redact_secrets(str(exc)),
                    }
                )
        try:
            results["market"] = serialize(context.research_service.sync_market())
        except Exception as exc:
            errors.append(
                {
                    "code": str(getattr(exc, "code", "market_sync_failed")),
                    "message": redact_secrets(str(exc)),
                }
            )
        return envelope("sync.daily", results, errors=errors)

    if args.command == "status":
        return envelope(
            "status",
            {
                "version": __version__,
                "yangjibao_authorized": context.token_store.load() is not None,
                "portfolio_freshness": data_freshness,
                "latest_successful_sync": latest_sync,
            },
            warnings=[
                "freshness uses weekday boundaries and does not yet include exchange holidays"
            ],
        )

    if args.command == "portfolio" and args.portfolio_command == "show":
        positions = _positions_payload(context)
        warnings = [] if positions else ["no synchronized portfolio is available"]
        return envelope(
            "portfolio.show",
            {"freshness": data_freshness, "positions": positions},
            warnings=warnings,
        )

    if args.command == "portfolio" and args.portfolio_command == "analyze":
        analysis = analyze_portfolio(context.repository.latest_positions())
        return envelope(
            "portfolio.analyze",
            {"freshness": data_freshness, "analysis": serialize(analysis)},
            warnings=list(analysis.warnings),
        )

    if args.command == "portfolio" and args.portfolio_command == "overlap":
        peer_store, disclosure_store = _peer_components(context)
        positions = context.repository.latest_positions()
        fund_codes = tuple(sorted({position.fund_code for position in positions}))
        bundles = {code: disclosure_store.load_bundle(code) for code in fund_codes}
        as_of = datetime.now(timezone.utc)
        report = build_portfolio_overlap_report(bundles, positions, as_of)
        data = _save_comparison_report(peer_store, "portfolio_overlap", report, as_of)
        errors = []
        if data["status"] == "insufficient_data":
            errors.append(
                {
                    "code": "insufficient_data",
                    "message": "No usable portfolio weight is available for overlap analysis",
                }
            )
        return envelope("portfolio.overlap", data, errors=errors)

    if args.command == "portfolio" and args.portfolio_command == "diagnose":
        diagnosis_service = context.diagnosis_service
        if diagnosis_service is None:
            disclosure_store = context.fund_disclosure_store or FundDisclosureStore(
                context.repository
            )
            diagnosis_service = DiagnosisService(context.repository, disclosure_store)
        result = diagnosis_service.diagnose(args.candidate)
        data = public_diagnosis_payload(result)
        errors = []
        if (
            result.relationship_coverage.evidence_state == "insufficient_data"
            or result.holdings_coverage.evidence_state == "insufficient_data"
        ):
            errors.append(
                {
                    "code": "insufficient_data",
                    "message": "Portfolio diagnosis has insufficient authenticated coverage",
                }
            )
        return envelope("portfolio.diagnose", data, errors=errors)

    if args.command == "portfolio" and args.portfolio_command == "review":
        if not args.json_output:
            raise CliUsageError("portfolio review requires JSON mode")
        diagnosis_service = context.diagnosis_service or DiagnosisService(
            context.repository,
            context.fund_disclosure_store or FundDisclosureStore(context.repository),
        )
        review = PortfolioReviewService(
            sync_service=context.sync_service,
            diagnosis_service=diagnosis_service,
            disclosure_store=context.fund_disclosure_store
            or FundDisclosureStore(context.repository),
        )
        data = (
            review.manual(tuple(_manual_portfolio_position(item) for item in args.manual_position))
            if args.manual_position
            else review.synced()
        )
        return envelope("portfolio.review", data)

    if args.command == "fund" and args.fund_command == "research":
        _validate_fund_code(args.fund_code)
        history = context.repository.fund_history(args.fund_code)
        analysis = analyze_fund_history(history)
        profile = context.repository.fund_profile(args.fund_code)
        return envelope(
            "fund.research",
            {"profile": profile, "analysis": analysis},
            warnings=list(analysis.get("warnings", [])),
        )

    if args.command == "fund" and args.fund_command == "peers":
        _validate_fund_code(args.fund_code)
        peer_store, _ = _peer_components(context)
        group = peer_store.load_current_group(args.fund_code)
        if group is None:
            return envelope(
                "fund.peers",
                {
                    "status": "insufficient_data",
                    "anchor_fund_code": args.fund_code,
                    "rule_version": None,
                    "calculation_version": PEER_CALCULATION_VERSION,
                    "data_dates": {},
                    "coverage": {},
                    "sources": [],
                    "advantages": [],
                    "tradeoffs": [],
                    "data_gaps": ["current_peer_group_missing"],
                    "watch_reasons": [],
                    "warnings": ["current_peer_group_missing"],
                    "errors": [],
                },
                errors=[
                    {
                        "code": "insufficient_data",
                        "message": "No current peer group is available for this fund",
                    }
                ],
            )
        fund_codes = tuple(member.fund_code for member in group.members)
        bundles, histories, positions = _comparison_inputs(context, fund_codes)
        as_of = datetime.now(timezone.utc)
        report = build_peer_report(group, bundles, histories, positions, as_of)
        data = _save_comparison_report(
            peer_store,
            "peer",
            report,
            as_of,
            anchor_fund_code=args.fund_code,
            peer_group_id=group.id,
        )
        errors = []
        if data["status"] == "insufficient_data":
            errors.append(
                {
                    "code": "peer_group_too_small",
                    "message": "The current peer group has fewer than two usable members",
                }
            )
        return envelope("fund.peers", data, errors=errors)

    if args.command == "fund" and args.fund_command == "compare":
        fund_codes = _validate_compare_codes(args.fund_codes)
        peer_store, _ = _peer_components(context)
        bundles, histories, positions = _comparison_inputs(context, fund_codes)
        as_of = datetime.now(timezone.utc)
        report = build_explicit_compare_report(fund_codes, bundles, histories, positions, as_of)
        data = _save_comparison_report(
            peer_store,
            "explicit",
            report,
            as_of,
            anchor_fund_code=fund_codes[0],
        )
        return envelope("fund.compare", data)

    if args.command == "fund" and args.fund_command == "candidates":
        if not args.json_output:
            raise CliUsageError("fund candidates requires JSON mode")
        fund_codes = _validate_candidate_codes(args.fund_codes)
        bundles, histories, positions = _comparison_inputs(context, fund_codes)
        comparison = build_explicit_compare_report(
            fund_codes, bundles, histories, positions, datetime.now(timezone.utc)
        )
        guardrails = build_investor_guardrails(
            emergency_fund=args.emergency_fund,
            near_term_use=args.near_term_use,
            horizon=args.horizon,
            volatility=args.volatility,
            portfolio=None,
        )
        return envelope(
            "fund.candidates",
            build_fund_candidate_review(
                fund_codes=fund_codes,
                comparison=comparison,
                guardrails=guardrails,
            ),
        )

    if args.command == "fund" and args.fund_command == "shortlist":
        fund_codes = _validate_shortlist_codes(args.fund_codes)
        if context.selection_service is None:
            raise CliUsageError("fund shortlist service is unavailable")
        result = context.selection_service.review(fund_codes)
        data = public_shortlist_payload(result)
        errors = []
        if result.comparison_state == "insufficient_data":
            errors.append(
                {
                    "code": "insufficient_data",
                    "message": (
                        "Candidate shortlist has insufficient authenticated evidence"
                    ),
                }
            )
        return envelope("fund.shortlist", data, errors=errors)

    if args.command == "fund" and args.fund_command == "research-scope":
        if not args.json_output:
            raise CliUsageError("fund research-scope requires JSON mode")
        if context.research_scope_service is None:
            raise CliUsageError("fund research-scope service is unavailable")
        try:
            result = context.research_scope_service.form(
                objective=args.objective,
                horizon=args.horizon,
                product_category=args.product_category,
            )
            data = public_research_scope_payload(result)
        except Exception:
            raise FundResearchScopeCliError("fund research-scope failed") from None
        return envelope("fund.research-scope", data)

    if args.command == "fund" and args.fund_command == "shortlist-readiness":
        if not args.json_output:
            raise CliUsageError("fund shortlist-readiness requires JSON mode")
        fund_codes = _validate_shortlist_codes(args.fund_codes)
        if context.shortlist_readiness_service is None:
            raise CliUsageError("fund shortlist-readiness service is unavailable")
        try:
            result = context.shortlist_readiness_service.review(fund_codes)
            data = public_shortlist_readiness_payload(result)
        except Exception:
            raise FundShortlistReadinessCliError(
                "fund shortlist-readiness failed"
            ) from None
        errors = []
        if not result.comparison_evidence_ready:
            errors.append(
                {
                    "code": "insufficient_data",
                    "message": "Candidate comparison evidence is not ready",
                }
            )
        return envelope("fund.shortlist-readiness", data, errors=errors)

    if args.command == "fund" and args.fund_command in {
        "profile",
        "fees",
        "holdings",
        "announcements",
    }:
        period = _parse_report_period(args.period) if args.fund_command == "holdings" else None
        report = _disclosure_report(context, args.fund_code, period)
        errors = _disclosure_errors(context, args.fund_code)
        metadata = _report_metadata(report, errors)
        if args.fund_command == "profile":
            data = {
                "fund_code": report["fund_code"],
                "identity": report["identity"],
                "share_classes": report["share_classes"],
                "managers": report["managers"],
                "sizes": report["sizes"],
                "benchmarks": report["benchmarks"],
                **metadata,
            }
        elif args.fund_command == "fees":
            data = {
                "fund_code": report["fund_code"],
                "fees": report["fees"],
                **metadata,
            }
        elif args.fund_command == "holdings":
            data = {
                "fund_code": report["fund_code"],
                "requested_period": None if period is None else period.isoformat(),
                "holdings": report["holdings"],
                "industry_exposure": report["industry_exposure"],
                **metadata,
            }
        else:
            data = {
                "fund_code": report["fund_code"],
                "announcements": report["announcements"],
                **metadata,
            }
        return envelope(f"fund.{args.fund_command}", data)

    if args.command == "market" and args.market_command == "sectors":
        analysis = analyze_sectors(context.repository.latest_sector_snapshots())
        return envelope(
            "market.sectors",
            analysis,
            warnings=list(analysis.get("warnings", [])),
        )

    if args.command == "thesis" and args.thesis_command == "add":
        thesis = InvestmentThesis(
            fund_code=args.fund_code,
            rationale=args.reason,
            horizon=args.horizon,
            invalidation=args.invalidation,
            created_at=datetime.now(timezone.utc),
        )
        thesis_id = context.repository.add_thesis(thesis)
        return envelope("thesis.add", {"id": thesis_id, "thesis": serialize(thesis)})

    if args.command == "thesis" and args.thesis_command == "list":
        theses = context.repository.list_theses(args.fund_code)
        return envelope("thesis.list", {"theses": serialize(theses)})

    if args.command == "thesis" and args.thesis_command == "review":
        theses = context.repository.list_theses(args.fund_code)
        research = analyze_fund_history(context.repository.fund_history(args.fund_code))
        warnings = list(research.get("warnings", []))
        if not theses:
            warnings.append("no recorded thesis exists for this fund")
        return envelope(
            "thesis.review",
            {"theses": serialize(theses), "fund_research": research},
            warnings=warnings,
        )

    if args.command == "report" and args.report_command == "weekly":
        positions = context.repository.latest_positions()
        portfolio = analyze_portfolio(positions)
        funds = {
            fund_code: analyze_fund_history(context.repository.fund_history(fund_code))
            for fund_code in sorted({position.fund_code for position in positions})
        }
        sectors = analyze_sectors(context.repository.latest_sector_snapshots())
        warnings = list(portfolio.warnings) + list(sectors.get("warnings", []))
        warnings.append(
            "news and causal attribution are not persisted yet; verify relevant events "
            "from official sources"
        )
        return envelope(
            "report.weekly",
            {
                "portfolio": serialize(portfolio),
                "funds": funds,
                "sectors": sectors,
                "learning_questions": [
                    "Did the original thesis still hold this week?",
                    "Was the result driven by the intended mechanism or broad market movement?",
                    "What evidence would invalidate the position next week?",
                ],
            },
            warnings=warnings,
        )

    return envelope(
        str(args.command),
        errors=[{"code": "unknown_command", "message": "Unknown command"}],
    )


def run(
    argv: Optional[Sequence[str]] = None,
    context: Optional[ApplicationContext] = None,
) -> Tuple[Dict[str, Any], int, bool]:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args: Optional[argparse.Namespace] = None
    json_output = "--json" in raw_argv
    try:
        args = build_parser().parse_args(raw_argv)
        json_output = args.json_output
        _preflight_before_context(args)
        if args.command == "version":
            payload = envelope("version", {"version": __version__})
        else:
            public_acceptance_subject = (
                args.fund_code
                if args.command == "fund" and args.fund_command == "brief"
                else None
            )
            payload = execute(
                args,
                context
                or build_context(public_acceptance_subject=public_acceptance_subject),
            )
        exit_code = 1 if payload["errors"] else 0
    except (
        CredentialStoreError,
        PublicDataError,
        YangjibaoError,
        SyncError,
        OcrError,
        LedgerImportError,
        LedgerStateError,
        ProfileCryptoError,
        SuitabilityPolicyError,
        SuitabilityAssessmentError,
        AllocationPolicyError,
        AllocationCalculationError,
        EncryptedProfileUnavailableError,
        RiskServiceError,
        HoldingReviewServiceError,
        ThesisReviewError,
        CliUsageError,
        ValueError,
    ) as exc:
        code = getattr(exc, "code", "operation_failed")
        message = redact_secrets(str(exc))
        command_name = (
            _command_name(args) if args is not None else _command_name_from_argv(raw_argv)
        )
        if isinstance(exc, CliUsageError) and args is None and command_name == "fund.brief":
            message = "invalid fund brief arguments"
        if isinstance(
            exc,
            (
                AllocationPolicyError,
                AllocationCalculationError,
                EncryptedProfileUnavailableError,
            ),
        ) or (
            args is not None
            and args.command == "allocation"
            and isinstance(exc, ProfileCryptoError)
        ):
            code = (
                "encrypted_profile_unavailable"
                if isinstance(exc, (EncryptedProfileUnavailableError, ProfileCryptoError))
                else str(code)
            )
            message = _ALLOCATION_PUBLIC_ERROR_MESSAGES.get(
                str(code), "allocation calculation failed"
            )
        if isinstance(exc, RiskServiceError):
            code = exc.code
            message = _RISK_PUBLIC_ERROR_MESSAGES[code]
            if exc.reason is not None:
                message = f"{message}: {exc.reason}"
        error_data = (
            {"request": exc.request_metadata}
            if isinstance(exc, BoundedDisclosureSyncError)
            else None
        )
        payload = envelope(
            command_name,
            error_data,
            errors=[{"code": str(code), "message": message}],
        )
        exit_code = 1
    return serialize(payload), exit_code, json_output


def _preflight_before_context(args: argparse.Namespace) -> None:
    if args.command == "thesis" and args.thesis_command in {
        "match-project",
        "adjudicate",
    }:
        if not args.json_output:
            raise CliUsageError(f"thesis {args.thesis_command} requires JSON mode")
        return
    if args.command != "fund":
        return
    if args.fund_command == "holding-review":
        if not args.json_output:
            raise CliUsageError("fund holding-review requires JSON mode")
        return
    if args.fund_command == "research-scope":
        if not args.json_output:
            raise CliUsageError("fund research-scope requires JSON mode")
        return
    if args.fund_command == "shortlist-readiness":
        if not args.json_output:
            raise CliUsageError("fund shortlist-readiness requires JSON mode")
        _validate_shortlist_codes(args.fund_codes)


def _command_name(args: argparse.Namespace) -> str:
    if args.command != "ledger":
        nested = getattr(args, f"{args.command}_command", None)
        return str(args.command) if nested is None else f"{args.command}.{nested}"
    if args.ledger_command == "document":
        return f"ledger.document.{args.document_command}"
    return f"ledger.{args.ledger_command}"


def _command_name_from_argv(argv: Sequence[str]) -> str:
    values = [str(value) for value in argv]
    command_index = next(
        (index for index, value in enumerate(values) if value in _TOP_LEVEL_COMMANDS),
        None,
    )
    if command_index is None:
        return "cli"
    command = values[command_index]
    if command not in {
        "auth",
        "allocation",
        "decision",
        "fund",
        "ledger",
        "market",
        "news",
        "portfolio",
        "profile",
        "report",
        "suitability",
        "source",
        "sync",
        "thesis",
    }:
        return command

    action_index = command_index + 1
    if action_index >= len(values):
        return command
    action = values[action_index]
    if not _COMMAND_PART.fullmatch(action):
        return command
    if command != "ledger" or action != "document":
        return f"{command}.{action}"

    document_action_index = action_index + 1
    if document_action_index >= len(values):
        return "ledger.document"
    document_action = values[document_action_index]
    if not _COMMAND_PART.fullmatch(document_action):
        return "ledger.document"
    return f"ledger.document.{document_action}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    payload, exit_code, json_output = run(argv)
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
