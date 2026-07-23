import hashlib
import json
import os
import re
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from kunjin.adapters.eastmoney import EastmoneyFundClient, EastmoneyMarketClient
from kunjin.adapters.yangjibao import YangjibaoClient
from kunjin.allocation.crypto import AllocationCipher
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.allocation.service import (
    AllocationCalculationError,
    AllocationPolicyError,
    AllocationService,
    EncryptedProfileUnavailableError,
)
from kunjin.allocation.store import AllocationAssessmentStore, AllocationPolicyStore
from kunjin.cli import (
    ApplicationContext,
    _conclusion_evidence_from_public,
    _validate_decision_route_data,
    _validate_source_status_data,
    build_context,
    build_parser,
    run,
)
from kunjin.decision.budget import RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    ActionKind,
    ForceReasonCode,
    RequestFieldResolution,
    RequestMode,
    RequestTerminalStatus,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    SourceFieldState,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.service import DecisionRoutingService
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore, DecisionAuditStoreError
from kunjin.funds.models import DisclosureBundle, DocumentKind
from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
    PeerSyncState,
)
from kunjin.funds.peers.service import PeerSyncResult
from kunjin.funds.risk.audit import RefreshOutcome, native_parser_provenance
from kunjin.funds.risk.documents import OfficialDocumentCandidate, RetrievedArtifact
from kunjin.funds.risk.engine import classification_input_manifest_v1
from kunjin.funds.risk.legacy_doc import ConverterStatus
from kunjin.funds.risk.parsers import ParsedRiskDocument
from kunjin.funds.risk.policy import ClassificationPolicyV1
from kunjin.funds.risk.selection import select_current_candidates
from kunjin.funds.risk.service import (
    DocumentSelectionItem,
    DocumentSyncItem,
    DocumentSyncResult,
    FundRiskService,
    RiskServiceError,
)
from kunjin.funds.risk.store import FundRiskStore
from kunjin.funds.service import (
    FundDisclosureSyncInterrupted,
    FundDisclosureSyncResult,
    SectionSyncResult,
)
from kunjin.funds.store import FundDisclosureStore
from kunjin.intelligence.models import IntelligenceWorkflow, QueryInterval
from kunjin.intelligence.service import (
    IntelligenceRequestSubject,
    IntelligenceServiceError,
    PragmaticIntelligenceResult,
)
from kunjin.intelligence.store import AuthenticatedTerminalRequest
from kunjin.ledger.alipay import AlipayPaymentParser
from kunjin.ledger.models import OcrBlock
from kunjin.ledger.service import LedgerService
from kunjin.ledger.store import LedgerStore
from kunjin.models import (
    AccountObservation,
    FundNavObservation,
    PositionObservation,
    SectorObservation,
    SyncResult,
)
from kunjin.paths import RuntimePaths
from kunjin.selection.models import PersonalGateEvidence, ShortlistResult
from kunjin.selection.readiness import (
    CandidateReadinessEvidence,
    ShortlistReadinessResult,
)
from kunjin.selection.scope import ResearchScopeService
from kunjin.services.research import ResearchSyncResult, ResearchSyncService
from kunjin.services.sync import PortfolioSyncService
from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import AssessmentCipher, ProfileCipher, ProfileCryptoError
from kunjin.suitability.models import PlannedObligation
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
from tests.unit.test_selection_models import shortlist_result_fixture
from tests.unit.test_suitability_models import valid_profile


class FakeTokenStore:
    def __init__(self, token="never-print-this") -> None:
        self.token = token

    def load(self):
        return self.token

    def save(self, token):
        self.token = token

    def delete(self):
        self.token = None


def document_selections(
    *,
    annual_url="https://www.fund001.com/report.html",
) -> tuple[DocumentSelectionItem, ...]:
    annual = (
        DocumentSelectionItem(
            document_kind="annual_report",
            status="missing",
            selected_url=None,
            candidate_count=0,
            reason_code="current_periodic_candidate_missing",
        )
        if annual_url is None
        else DocumentSelectionItem(
            document_kind="annual_report",
            status="selected",
            selected_url=annual_url,
            candidate_count=1,
            reason_code=None,
        )
    )
    return (
        annual,
        DocumentSelectionItem(
            document_kind="quarterly_report",
            status="missing",
            selected_url=None,
            candidate_count=0,
            reason_code="current_periodic_candidate_missing",
        ),
        DocumentSelectionItem(
            document_kind="semiannual_report",
            status="missing",
            selected_url=None,
            candidate_count=0,
            reason_code="current_periodic_candidate_missing",
        ),
    )


class FakeProfileKeyStore:
    def __init__(self) -> None:
        self.key = b"fake-profile-key-secret-value!!!"

    def load_existing_key(self):
        return self.key

    def load_or_create_key(self):
        return self.key


class FakeOcrClient:
    def __init__(self, amount_confidence="0.99"):
        self.amount_confidence = amount_confidence

    def recognize(self, image_path):
        return [
            OcrBlock(
                text="订单金额 ￥20.00",
                confidence=Decimal(self.amount_confidence),
                x=Decimal("0.1"),
                y=Decimal("0.2"),
                width=Decimal("0.5"),
                height=Decimal("0.05"),
            ),
            OcrBlock(
                text="订单时间 2026-07-04 23:11:51",
                confidence=Decimal("0.98"),
                x=Decimal("0.1"),
                y=Decimal("0.4"),
                width=Decimal("0.7"),
                height=Decimal("0.05"),
            ),
            OcrBlock(
                text="商家订单号 202607040001",
                confidence=Decimal("0.97"),
                x=Decimal("0.1"),
                y=Decimal("0.6"),
                width=Decimal("0.7"),
                height=Decimal("0.05"),
            ),
        ]


class FakeDisclosureService:
    def __init__(self, profile=None, holdings=None, snapshots=None) -> None:
        self.profile_result = profile
        self.holdings_result = holdings
        self.snapshots = snapshots or {}
        self.calls = []

    def sync_profile(self, fund_code, *, request_context=None):
        self.calls.append(("profile", fund_code))
        self.request_context = request_context
        if isinstance(self.profile_result, BaseException):
            raise self.profile_result
        return self.profile_result

    def sync_holdings(self, fund_code, *, request_context=None):
        self.calls.append(("holdings", fund_code))
        self.request_context = request_context
        if isinstance(self.holdings_result, BaseException):
            raise self.holdings_result
        return self.holdings_result

    def section_snapshot(self, fund_code, section):
        return self.snapshots.get(
            section,
            SectionSyncResult(section, "missing", 0, "missing"),
        )


class FakeDisclosureStore:
    def __init__(self, bundle) -> None:
        self.bundle = bundle

    def load_bundle(self, fund_code):
        return self.bundle


class FakePeerService:
    def __init__(self, result, refresh_result=None) -> None:
        self.result = result
        self.refresh_result = refresh_result
        self.calls = []
        self.refresh_calls = 0

    def sync_peers(self, fund_code, user_candidates=()):
        self.calls.append((fund_code, tuple(user_candidates)))
        return self.result

    def refresh_existing_groups(self):
        self.refresh_calls += 1
        return self.refresh_result or {}


class FakePeerStore:
    def __init__(self, group=None) -> None:
        self.group = group
        self.saved = []

    def load_current_group(self, fund_code):
        return self.group

    def save_comparison(self, *args, **kwargs):
        self.saved.append((args, kwargs))
        return 7


class FakeRiskService:
    def __init__(self) -> None:
        self.sync_result = None
        self.classification_result = SimpleNamespace(input_fingerprint="a" * 64)
        self.history = (
            SimpleNamespace(id=11, input_fingerprint="a" * 64),
            SimpleNamespace(id=10, input_fingerprint="b" * 64),
        )
        self.evidence_by_id = {
            11: SimpleNamespace(report_status="partial", fingerprint="a" * 64),
            10: SimpleNamespace(report_status="stale", fingerprint="b" * 64),
        }
        self.calls = []
        self.error = None
        self.converter_status_result = ConverterStatus(
            capability="research_only",
            status="unavailable",
            reason_code="legacy_converter_unavailable",
            parser_version=None,
            provenance_checksum=None,
        )

    def converter_status(self):
        self.calls.append(("converter_status",))
        return self.converter_status_result

    def sync_documents(self, fund_code):
        self.calls.append(("sync_documents", fund_code))
        if self.error is not None:
            raise self.error
        return self.sync_result

    def classify(self, fund_code):
        self.calls.append(("classify", fund_code))
        if self.error is not None:
            raise self.error
        return self.classification_result

    def classification_history(self, fund_code):
        self.calls.append(("classification_history", fund_code))
        if self.error is not None:
            raise self.error
        return self.history

    def classification_evidence(self, fund_code, classification_id=None):
        self.calls.append(("classification_evidence", fund_code, classification_id))
        if self.error is not None:
            raise self.error
        if classification_id is None:
            return self.evidence_by_id.get(11)
        return self.evidence_by_id.get(classification_id)


def fake_risk_report(record):
    status = record.report_status
    reason_code = {
        "partial": "classification_partial",
        "stale": "classification_stale",
        "conflicted": "classification_conflicted",
        "unsupported": "unsupported_product_family",
        "unclassified": "classification_unclassified",
    }.get(status, "classification_verified")
    evidence_status = "unclassified" if status == "unsupported" else status
    return {
        "capability": "research_only",
        "fund_code": "519755",
        "verified_facts": [],
        "non_verified_evidence": [],
        "classification": {
            "policy_version": "1",
            "input_fingerprint": record.fingerprint,
            "product_family": "unsupported" if status == "unsupported" else "unclassified",
            "risk_bucket": "unclassified",
            "portfolio_role": "not_eligible",
            "reason_codes": [reason_code],
            "classified_at": "2026-07-13T08:00:00+00:00",
            "valid_until": "2026-07-14T08:00:00+00:00",
        },
        "evidence_status": evidence_status,
        "evidence_tags": [],
        "missing_evidence": ["critical_evidence_missing"],
        "conflicts": [] if status != "conflicted" else ["source_version_conflict"],
        "freshness": [],
        "sources": [
            {
                "url": "https://www.fund001.com/public.pdf",
                "title": "official public title",
                "checksum": "c" * 64,
                "published_at": "2026-07-13T00:00:00+00:00",
            }
        ],
        "limitations": [
            "cash_like_is_not_protected_cash",
            "classification_is_not_recommendation",
            "d2_d3_not_evaluated",
        ],
    }


def peer_group(*codes):
    return PeerGroup(
        id=3,
        anchor_fund_code=codes[0],
        rule_version="1",
        rule_key="混合型-灵活|active_or_unspecified|equity_bond",
        rule_description="同类型、管理方式与基准族",
        candidate_source_url="https://fund.eastmoney.com/js/fundcode_search.js",
        candidate_source_tier=2,
        candidate_source_checksum="a" * 64,
        input_fingerprint="b" * 64,
        created_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        status=PeerGroupStatus.SUCCESS,
        members=tuple(
            PeerGroupMember(
                code,
                MembershipKind.ANCHOR if index == 0 else MembershipKind.DISCOVERED,
                "混合型-灵活|active_or_unspecified|equity_bond",
                "classification_match",
                None,
                None,
            )
            for index, code in enumerate(codes)
        ),
    )


def empty_disclosure_bundle(fund_code="519755"):
    return DisclosureBundle(
        fund_code=fund_code,
        identity=None,
        share_classes=(),
        manager_tenures=(),
        fee_rules=(),
        sizes=(),
        benchmarks=(),
        holdings=(),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )


def disclosure_sync_result(fund_code="519755", profile=True, total_failure=False):
    names = (
        ("basic_profile", "manager_history", "fee_schedule", "size_history", "announcements")
        if profile
        else ("quarterly_holdings", "industry_exposure")
    )
    sections = {}
    for index, name in enumerate(names):
        failed = total_failure or index == len(names) - 1
        sections[name] = SectionSyncResult(
            name,
            "source_unavailable" if failed else "success",
            0 if failed else 1,
            "missing" if failed else "fresh",
            error_code="remote_unavailable" if failed else None,
        )
    return FundDisclosureSyncResult(fund_code, sections, ())


class FakeShortlistService:
    def __init__(self, result: ShortlistResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def review(self, fund_codes) -> ShortlistResult:
        codes = tuple(fund_codes)
        self.calls.append(codes)
        return self.result


class FakeResearchScopeService:
    def __init__(self, *, blocked: bool = False) -> None:
        suitability = {
            "state": "fresh",
            "freshness": "fresh",
            "status": "blocked" if blocked else "ready_for_allocation",
            "hard_blocks": ["emergency_reserve_shortfall"] if blocked else [],
            "constraints": [],
        }
        allocation = {
            "state": "missing" if blocked else "fresh",
            "freshness": "missing" if blocked else "fresh",
            "status": None if blocked else "range_available",
            "binding_constraints": [],
        }
        self._delegate = ResearchScopeService(
            suitability_status_loader=lambda: suitability,
            allocation_status_loader=lambda: allocation,
        )
        self.calls: list[dict[str, str | None]] = []

    def form(self, *, objective, horizon, product_category):
        self.calls.append(
            {
                "objective": objective,
                "horizon": horizon,
                "product_category": product_category,
            }
        )
        return self._delegate.form(
            objective=objective,
            horizon=horizon,
            product_category=product_category,
        )


class FakeShortlistReadinessService:
    def __init__(self, result: ShortlistReadinessResult) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    def review(self, fund_codes) -> ShortlistReadinessResult:
        codes = tuple(fund_codes)
        self.calls.append(codes)
        return self.result


def readiness_result_fixture(*, ready: bool = True) -> ShortlistReadinessResult:
    codes = ("000002", "000001")
    personal_gate = PersonalGateEvidence(
        suitability_state="fresh",
        suitability_freshness="fresh",
        suitability_status="ready_for_allocation",
        allocation_state="fresh",
        allocation_freshness="fresh",
        allocation_status="range_available",
        blocking_codes=(),
        constraint_codes=(),
    )
    evidence = tuple(
        CandidateReadinessEvidence(
            fund_code=code,
            source_health=(),
            profile=(),
            formal_nav=(),
            holdings=(),
            d1=(),
            portfolio_binding=(("position_state", "not_held"),),
            shortlist_entry=(),
        )
        for code in codes
    )
    result = ShortlistReadinessResult(
        as_of=datetime(2026, 7, 19, tzinfo=timezone.utc),
        candidate_codes=codes,
        personal_gate=personal_gate,
        candidate_evidence=evidence,
        comparison_evidence_ready=ready,
        conditional_shortlist_gate_ready=True,
        blocking_codes=() if ready else ("formal_nav_missing",),
        bounded_refresh_actions=()
        if ready
        else (("000002", "sync fund 000002"),),
        manual_supplementation=(),
    )
    result.validate()
    return result


class CliIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
        repository = Repository(paths.database)
        repository.migrate()
        now = datetime.now(timezone.utc)
        repository.replace_snapshot(
            AccountObservation("yangjibao", "account-1", "学习账户", now),
            [
                PositionObservation(
                    "account-1",
                    "016067",
                    "新能源混合A",
                    Decimal("10"),
                    now,
                    share_class="A",
                    formal_nav=Decimal("1.1"),
                    observed_profit=Decimal("0.1"),
                ),
                PositionObservation(
                    "account-1",
                    "519755",
                    "成长基金",
                    Decimal("11.32"),
                    now,
                    formal_nav=Decimal("1.7467"),
                    observed_profit=Decimal("-0.23"),
                ),
            ],
        )
        token_store = FakeTokenStore()
        client = YangjibaoClient(token_store)
        repository.save_fund_history(
            "017811",
            "人工智能混合C",
            "混合型",
            "eastmoney",
            [
                FundNavObservation(
                    "017811", date(2026, 6, 1), Decimal("1.0"), None, None, "eastmoney", now
                ),
                FundNavObservation(
                    "017811", date(2026, 7, 1), Decimal("1.1"), None, None, "eastmoney", now
                ),
            ],
        )
        repository.save_sector_snapshots(
            [
                SectorObservation(
                    "BK1", "半导体", "industry", Decimal("2"), Decimal("3"), 8, 2, "eastmoney", now
                )
            ]
        )
        self.profile_key_store = FakeProfileKeyStore()
        self.suitability_now = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
        profile_service = ProfileService(
            ProfileStore(repository),
            ProfileCipher(self.profile_key_store),
            now=lambda: self.suitability_now,
        )
        suitability_service = SuitabilityService(
            profile_service,
            SuitabilityPolicyStore(repository),
            SuitabilityAssessmentStore(repository),
            AssessmentCipher(self.profile_key_store),
            SuitabilityPolicyV1(),
            now=lambda: self.suitability_now,
        )
        self.context = ApplicationContext(
            paths=paths,
            repository=repository,
            token_store=token_store,
            client=client,
            sync_service=PortfolioSyncService(client, repository),
            research_service=ResearchSyncService(
                repository, EastmoneyFundClient(), EastmoneyMarketClient()
            ),
            ledger_service=LedgerService(
                paths,
                LedgerStore(repository),
                FakeOcrClient(),
                AlipayPaymentParser(),
                now=lambda: datetime(2026, 7, 11, tzinfo=timezone.utc),
            ),
            fund_disclosure_store=FundDisclosureStore(repository),
            profile_service=profile_service,
            suitability_service=suitability_service,
            allocation_service=AllocationService(
                suitability_service,
                AllocationPolicyStore(repository),
                AllocationAssessmentStore(repository),
                AllocationCipher(self.profile_key_store),
                AllocationPolicyV1(),
                now=lambda: self.suitability_now,
            ),
        )
        audit_store = DecisionAuditStore(repository)
        evidence_policy = EvidencePolicyV1()
        source_registry = SourceRegistryV1()
        self.context.decision_service = DecisionRoutingService(
            suitability_service,
            audit_store,
            policy=evidence_policy,
            registry=source_registry,
        )
        self.context.source_health_service = SourceHealthService(
            audit_store,
            registry=source_registry,
            policy=evidence_policy,
        )
        self.payment_image = root / "payment.jpg"
        self.payment_image.write_bytes(b"synthetic payment screenshot")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_status_json_contract(self) -> None:
        payload, exit_code, json_output = run(["--json", "status"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(
            set(payload),
            {"schema_version", "command", "as_of", "data", "warnings", "errors"},
        )

    def test_holding_review_preview_commands_require_json_and_positive_ids(self) -> None:
        parsed = build_parser().parse_args(
            [
                "--json",
                "fund",
                "holding-review",
                "123456",
                "--action",
                "full_exit",
                "--brief-request-run-id",
                "11",
                "--intelligence-request-run-id",
                "12",
                "--exit-reason",
                "risk_reduction",
                "--use-of-proceeds",
                "cash_reserve",
            ]
        )
        self.assertEqual(parsed.fund_command, "holding-review")
        self.assertEqual(parsed.brief_request_run_id, 11)

        for argv in (
            ["thesis", "match-project", "123456", "--intelligence-request-run-id", "12"],
            [
                "thesis",
                "adjudicate",
                "123456",
                "--thesis-match-projection-id",
                "1",
                "--decision",
                "uncertain",
            ],
            [
                "fund",
                "holding-review",
                "123456",
                "--action",
                "continue_holding",
                "--brief-request-run-id",
                "11",
                "--intelligence-request-run-id",
                "12",
            ],
        ):
            payload, exit_code, _ = run(argv, self.context)
            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

        payload, exit_code, _ = run(
            [
                "--json",
                "thesis",
                "match-project",
                "123456",
                "--intelligence-request-run-id",
                "0",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_holding_review_preview_services_are_explicit_and_fail_closed(self) -> None:
        payload, exit_code, _ = run(
            [
                "--json",
                "thesis",
                "match-project",
                "123456",
                "--intelligence-request-run-id",
                "12",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_holding_review_cli_passes_explicit_action_context(self) -> None:
        from types import SimpleNamespace

        from kunjin.holding_review.models import FlowStatus, TransientHoldingReviewOutcome

        calls = []

        class FakeHoldingReviewService:
            def review(self, fund_code, **kwargs):
                calls.append((fund_code, kwargs))
                return TransientHoldingReviewOutcome(
                    flow_status=FlowStatus.PARTIAL,
                    review_snapshot=None,
                    missing_snapshot_codes=("brief_snapshot_missing",),
                )

        self.context.holding_review_service = FakeHoldingReviewService()
        self.context.thesis_review_service = SimpleNamespace()
        payload, exit_code, json_output = run(
            [
                "--json",
                "fund",
                "holding-review",
                "123456",
                "--action",
                "reduce_to_cash",
                "--brief-request-run-id",
                "11",
                "--intelligence-request-run-id",
                "12",
                "--remainder-intent",
                "retain_some",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assertEqual(payload["command"], "fund.holding-review")
        self.assertFalse(payload["data"]["review_boundary"]["action_authorized"])
        self.assertEqual(calls[0][0], "123456")
        self.assertEqual(calls[0][1]["brief_request_run_id"], 11)
        self.assertEqual(calls[0][1]["remainder_intent"].value, "retain_some")

    def test_thesis_preview_cli_projects_then_adjudicates_only_explicitly(self) -> None:
        from types import SimpleNamespace

        calls = []

        class PublicRecord:
            def __init__(self, state):
                self.state = state

            def to_canonical_dict(self):
                return {"state": self.state}

        class FakeThesisReviewService:
            def match_project(self, fund_code, request_run_id):
                calls.append(("match", fund_code, request_run_id))
                return SimpleNamespace(
                    id=31,
                    value=PublicRecord("possible_invalidation_match"),
                )

            def adjudicate(self, fund_code, projection_id, decision, supersedes_id=None):
                calls.append(
                    (
                        "adjudicate",
                        fund_code,
                        projection_id,
                        decision.value,
                        supersedes_id,
                    )
                )
                return SimpleNamespace(
                    id=41,
                    value=PublicRecord(decision.value),
                )

        self.context.thesis_review_service = FakeThesisReviewService()
        matched, match_exit, _ = run(
            [
                "--json",
                "thesis",
                "match-project",
                "123456",
                "--intelligence-request-run-id",
                "12",
            ],
            self.context,
        )

        self.assertEqual(match_exit, 0)
        self.assertEqual(matched["data"]["id"], 31)
        self.assertEqual(calls, [("match", "123456", 12)])

        adjudicated, adjudication_exit, _ = run(
            [
                "--json",
                "thesis",
                "adjudicate",
                "123456",
                "--thesis-match-projection-id",
                "31",
                "--decision",
                "presented_match_rejected",
                "--supersedes",
                "40",
            ],
            self.context,
        )

        self.assertEqual(adjudication_exit, 0)
        self.assertEqual(adjudicated["data"]["id"], 41)
        self.assertEqual(
            calls[-1],
            ("adjudicate", "123456", 31, "presented_match_rejected", 40),
        )

    def test_pragmatic_intelligence_parser_and_thin_json_dispatch(self) -> None:
        self.assertEqual(
            build_parser().parse_args(["--json", "news", "recent"]).news_command,
            "recent",
        )
        self.assertEqual(
            build_parser().parse_args(["--json", "market", "overview"]).market_command,
            "overview",
        )
        parsed = build_parser().parse_args(
            ["--json", "fund", "intelligence", "519755"]
        )
        self.assertEqual(parsed.fund_command, "intelligence")
        self.assertEqual(parsed.fund_code, "519755")

        terminal = AuthenticatedTerminalRequest(
            id=1,
            request_id="a" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=self.suitability_now,
            deadline_at=self.suitability_now + timedelta(seconds=90),
            finished_at=self.suitability_now + timedelta(seconds=1),
            omitted_work=("source_unavailable",),
        )
        class FakeIntelligenceService:
            def __init__(self) -> None:
                self.calls = []

            def _result(self, workflow, fund_code=None):
                return PragmaticIntelligenceResult(
                    report=None,
                    terminal_request=terminal,
                    subject=IntelligenceRequestSubject(
                        workflow=workflow,
                        interval=QueryInterval(
                            self_now - timedelta(hours=72),
                            self_now,
                            "Asia/Shanghai",
                        ),
                        subject_scope=(
                            "global_public"
                            if fund_code is None
                            else "named_public_fund"
                        ),
                        fund_code=fund_code,
                    ),
                    items=(),
                    item_uses=(),
                    lineage_edges=(),
                    events=(),
                    source_summaries=(),
                    sector_labels=(),
                    fund_context=None,
                    thesis_review=None,
                )

            def news_recent(self, **kwargs):
                self.calls.append(("news", kwargs))
                return self._result(IntelligenceWorkflow.NEWS_RECENT)

            def market_overview(self, **kwargs):
                self.calls.append(("market", kwargs))
                return self._result(IntelligenceWorkflow.MARKET_OVERVIEW)

            def fund_intelligence(self, fund_code, **kwargs):
                self.calls.append(("fund", fund_code, kwargs))
                return self._result(IntelligenceWorkflow.FUND_INTELLIGENCE, fund_code)

        self_now = self.suitability_now
        service = FakeIntelligenceService()
        self.context.intelligence_service = service
        cases = (
            (["--json", "news", "recent"], "news.recent"),
            (["--json", "market", "overview", "--mode", "deep"], "market.overview"),
            (
                ["--json", "fund", "intelligence", "519755"],
                "fund.intelligence",
            ),
        )
        for argv, command in cases:
            with self.subTest(command=command):
                payload, exit_code, json_output = run(argv, self.context)
                self.assertEqual(exit_code, 0)
                self.assertTrue(json_output)
                self.assert_envelope(payload, command)
                self.assertFalse(payload["data"]["exact_amount_available"])
                self.assertNotIn("fund:000000", json.dumps(payload, ensure_ascii=False))

        self.assertEqual(service.calls[0][1]["window"], "recent")
        self.assertEqual(service.calls[1][1]["mode"], "deep")
        self.assertEqual(service.calls[2][1], "519755")

    def test_pragmatic_intelligence_service_error_is_stable_and_sanitized(self) -> None:
        class FailingIntelligenceService:
            def news_recent(self, **_kwargs):
                raise IntelligenceServiceError(7)

        self.context.intelligence_service = FailingIntelligenceService()
        payload, exit_code, json_output = run(
            ["--json", "news", "recent"], self.context
        )

        self.assertEqual(exit_code, 1)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "news.recent")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "intelligence_service_failed",
                    "message": "intelligence service failed",
                }
            ],
        )

    def test_pragmatic_intelligence_rejects_mixed_window_and_explicit_dates(self) -> None:
        payload, exit_code, json_output = run(
            [
                "--json",
                "news",
                "recent",
                "--window",
                "recent",
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-02",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "news.recent")
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_research_summary_routes_one_public_workflow(self) -> None:
        now = self.suitability_now
        terminal = AuthenticatedTerminalRequest(
            id=19,
            request_id="a" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=now,
            deadline_at=now + timedelta(seconds=90),
            finished_at=now + timedelta(seconds=1),
            omitted_work=("source_unavailable",),
        )

        class FakeIntelligenceService:
            def __init__(self) -> None:
                self.calls = []

            def _result(self, workflow, fund_code=None):
                return PragmaticIntelligenceResult(
                    report=None,
                    terminal_request=terminal,
                    subject=IntelligenceRequestSubject(
                        workflow=workflow,
                        interval=QueryInterval(
                            now - timedelta(hours=72),
                            now,
                            "Asia/Shanghai",
                        ),
                        subject_scope=(
                            "global_public"
                            if fund_code is None
                            else "named_public_fund"
                        ),
                        fund_code=fund_code,
                    ),
                    items=(),
                    item_uses=(),
                    lineage_edges=(),
                    events=(),
                    source_summaries=(),
                    sector_labels=(),
                    fund_context=None,
                    thesis_review=None,
                )

            def news_recent(inner_self, **kwargs):
                inner_self.calls.append(("news", kwargs))
                return inner_self._result(IntelligenceWorkflow.NEWS_RECENT)

            def market_overview(inner_self, **kwargs):
                inner_self.calls.append(("market", kwargs))
                return inner_self._result(IntelligenceWorkflow.MARKET_OVERVIEW)

            def fund_intelligence(inner_self, fund_code, **kwargs):
                inner_self.calls.append(("fund", fund_code, kwargs))
                return inner_self._result(
                    IntelligenceWorkflow.FUND_INTELLIGENCE, fund_code
                )

        service = FakeIntelligenceService()
        self.context.intelligence_service = service
        cases = (
            (["--json", "research", "summary", "news"], "news_recent"),
            (["--json", "research", "summary", "market"], "market_overview"),
            (
                ["--json", "research", "summary", "fund", "519755"],
                "fund_intelligence",
            ),
        )
        for argv, workflow in cases:
            with self.subTest(argv=argv):
                payload, exit_code, json_output = run(argv, self.context)
                self.assertEqual(exit_code, 0, payload)
                self.assertTrue(json_output)
                self.assert_envelope(payload, "research.summary")
                self.assertEqual(payload["data"]["retrieval"]["workflow"], workflow)
                self.assertFalse(
                    payload["data"]["conditional_guidance"]["action_authorized"]
                )
                self.assertFalse(
                    payload["data"]["conditional_guidance"]["automatic_trade"]
                )

        self.assertEqual([item[0] for item in service.calls], ["news", "market", "fund"])

    def test_research_summary_requires_json_and_a_fund_code_for_fund_scope(self) -> None:
        payload, exit_code, json_output = run(
            ["research", "summary", "news"], self.context
        )
        self.assertEqual(exit_code, 1)
        self.assertFalse(json_output)
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

        payload, exit_code, json_output = run(
            ["--json", "research", "summary", "fund"], self.context
        )
        self.assertEqual(exit_code, 1)
        self.assertTrue(json_output)
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_research_scan_uses_market_overview_once(self) -> None:
        terminal = AuthenticatedTerminalRequest(
            id=20,
            request_id="b" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=self.suitability_now,
            deadline_at=self.suitability_now + timedelta(seconds=90),
            finished_at=self.suitability_now + timedelta(seconds=1),
            omitted_work=("source_unavailable",),
        )

        class FakeIntelligenceService:
            calls = 0

            def market_overview(inner_self, **_kwargs):
                inner_self.calls += 1
                return PragmaticIntelligenceResult(
                    report=None,
                    terminal_request=terminal,
                    subject=IntelligenceRequestSubject(
                        workflow=IntelligenceWorkflow.MARKET_OVERVIEW,
                        interval=QueryInterval(
                            self.suitability_now - timedelta(hours=72),
                            self.suitability_now,
                            "Asia/Shanghai",
                        ),
                        subject_scope="global_public",
                        fund_code=None,
                    ),
                    items=(),
                    item_uses=(),
                    lineage_edges=(),
                    events=(),
                    source_summaries=(),
                    sector_labels=(),
                    fund_context=None,
                    thesis_review=None,
                )

        service = FakeIntelligenceService()
        self.context.intelligence_service = service
        payload, exit_code, json_output = run(
            ["--json", "research", "scan", "--window", "recent"], self.context
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "research.scan")
        self.assertEqual(payload["data"]["retrieval"]["workflow"], "market_overview")
        self.assertEqual(service.calls, 1)

    def test_research_supplement_is_local_and_preliminary_only(self) -> None:
        payload, exit_code, json_output = run(
            [
                "--json",
                "research",
                "supplement",
                "--source-name",
                "公开行业统计",
                "--source-kind",
                "industry_data",
                "--title",
                "月度行业统计",
                "--published-at",
                "2026-07-20T08:00:00+00:00",
                "--source-url",
                "https://example.test/monthly",
                "--statistics-period",
                "2026年6月",
                "--indicator-name",
                "样本产量",
                "--indicator-value",
                "100",
                "--unit",
                "万台",
                "--methodology",
                "来源页面标明月度统计口径",
                "--domain",
                "autos",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "research.supplement")
        self.assertEqual(
            payload["data"]["fact"]["source"]["research_source_level"],
            "provisional_tier_2",
        )
        self.assertEqual(
            payload["data"]["current_research_use"]["state"],
            "prepared_for_next_research",
        )
        self.assertFalse(payload["data"]["current_research_use"]["strong_direction_eligible"])
        self.assertFalse(payload["data"]["conditional_guidance"]["automatic_trade"])

    def test_research_supplement_timeline_uses_only_supplied_materials(self) -> None:
        def material(period, published_at, value):
            return json.dumps(
                {
                    "source_name": "中国政府网",
                    "source_kind": "official",
                    "title": f"{period}全社会用电量",
                    "published_at": published_at,
                    "original_url": "https://www.gov.cn/example.htm",
                    "statistics_period": period,
                    "indicator_name": "全社会用电量",
                    "indicator_value": value,
                    "unit": "亿千瓦时",
                    "methodology": "用户提供的同口径月度材料",
                    "domain_id": "power_energy",
                }
            )

        payload, exit_code, json_output = run(
            [
                "--json",
                "research",
                "supplement-timeline",
                "--material-json",
                material("2026年3月", "2026-04-20T00:00:00+08:00", "8595"),
                "--material-json",
                material("2026年4月", "2026-05-19T00:00:00+08:00", "8205"),
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "research.supplement_timeline")
        self.assertEqual(payload["data"]["conclusion"]["state"], "timeline_available")
        self.assertEqual(len(payload["data"]["timeline"]), 2)
        self.assertFalse(payload["data"]["current_research_use"]["strong_direction_eligible"])

    def test_research_evidence_store_reuses_history_and_plans_only_new_months(self) -> None:
        for month, value in ((3, "100"), (4, "101"), (5, "102"), (6, "103"), (7, "104")):
            payload, exit_code, json_output = run(
                [
                    "--json",
                    "research",
                    "evidence-store",
                    "--source-name",
                    "测试行业协会",
                    "--publisher",
                    "测试行业协会",
                    "--source-kind",
                    "industry_data",
                    "--title",
                    f"{month}月测试产量",
                    "--published-at",
                    "2026-08-01T08:00:00+08:00",
                    "--source-url",
                    f"https://example.test/{month}",
                    "--statistics-period",
                    f"2026年{month}月",
                    "--indicator-name",
                    "测试产量",
                    "--indicator-value",
                    value,
                    "--unit",
                    "万台",
                    "--methodology",
                    "单月口径",
                    "--verification-state",
                    "outer_page_verified",
                    "--domain",
                    "autos",
                ],
                self.context,
            )
            self.assertEqual(exit_code, 0, payload)
            self.assertTrue(json_output)
            self.assert_envelope(payload, "research.evidence_store")

        timeline, exit_code, json_output = run(
            [
                "--json",
                "research",
                "evidence-timeline",
                "--domain",
                "autos",
                "--indicator-name",
                "测试产量",
                "--unit",
                "万台",
            ],
            self.context,
        )
        plan, plan_exit_code, plan_json_output = run(
            [
                "--json",
                "research",
                "evidence-refresh-plan",
                "--domain",
                "autos",
                "--indicator-name",
                "测试产量",
                "--unit",
                "万台",
                "--through-period",
                "2026年10月",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, timeline)
        self.assertTrue(json_output)
        self.assert_envelope(timeline, "research.evidence_timeline")
        self.assertEqual(
            timeline["data"]["current_research_use"]["state"],
            "used_in_persisted_timeline",
        )
        self.assertEqual(plan_exit_code, 0, plan)
        self.assertTrue(plan_json_output)
        self.assert_envelope(plan, "research.evidence_refresh_plan")
        self.assertEqual(
            plan["data"]["new_periods_to_fetch"],
            ["2026年8月", "2026年9月", "2026年10月"],
        )
        self.assertEqual(plan["data"]["revision_check_periods"], [
            "2026年3月",
            "2026年4月",
            "2026年5月",
            "2026年6月",
            "2026年7月",
        ])

    def test_research_event_store_clusters_multiple_verified_sources(self) -> None:
        base = [
            "--json",
            "research",
            "event-store",
            "--source-name",
            "行情平台",
            "--publisher",
            "行情平台",
            "--source-kind",
            "platform_data",
            "--title",
            "电力板块异动",
            "--source-url",
            "https://example.test/market-event",
            "--published-at",
            "2026-07-22T10:30:00+08:00",
            "--event-key",
            "power-event-2026-07-22",
            "--fact-summary",
            "行情页面记录电力板块异动。",
            "--claim-boundary",
            "报道归因不作为市场原因事实。",
            "--verification-state",
            "outer_page_verified",
            "--domain",
            "power_energy",
        ]
        first, first_exit, _ = run(base, self.context)
        second, second_exit, _ = run(
            [
                "--json",
                "research",
                "event-store",
                "--source-name",
                "财经媒体",
                "--publisher",
                "财经媒体",
                "--source-kind",
                "media",
                "--title",
                "不同标题的盘后报道",
                "--source-url",
                "https://media.example.test/event",
                "--published-at",
                "2026-07-22T11:00:00+08:00",
                "--event-key",
                "power-event-2026-07-22",
                "--fact-summary",
                "媒体报道同日电力板块异动。",
                "--claim-boundary",
                "媒体归因不作为市场原因事实。",
                "--verification-state",
                "outer_page_verified",
                "--domain",
                "power_energy",
            ],
            self.context,
        )
        timeline, timeline_exit, timeline_json = run(
            ["--json", "research", "event-timeline", "--domain", "power_energy"],
            self.context,
        )

        self.assertEqual(first_exit, 0, first)
        self.assertEqual(second_exit, 0, second)
        self.assertEqual(timeline_exit, 0, timeline)
        self.assertTrue(timeline_json)
        self.assert_envelope(timeline, "research.event_timeline")
        self.assertEqual(len(timeline["data"]["events"]), 1)
        self.assertEqual(len(timeline["data"]["events"][0]["sources"]), 2)
        self.assertEqual(timeline["data"]["events"][0]["reported_fact_source_count"], 1)

    def test_research_local_overview_requires_no_domain_or_indicator(self) -> None:
        indicator, indicator_exit, _ = run(
            [
                "--json",
                "research",
                "evidence-store",
                "--source-name",
                "公开统计",
                "--source-kind",
                "industry_data",
                "--title",
                "6月用电量",
                "--published-at",
                "2026-07-19T08:00:00+08:00",
                "--source-url",
                "https://example.test/power-june",
                "--statistics-period",
                "2026年6月",
                "--indicator-name",
                "全社会用电量",
                "--indicator-value",
                "8981",
                "--unit",
                "亿千瓦时",
                "--verification-state",
                "outer_page_verified",
                "--domain",
                "power_energy",
            ],
            self.context,
        )
        event, event_exit, _ = run(
            [
                "--json",
                "research",
                "event-store",
                "--source-name",
                "行情数据",
                "--source-kind",
                "platform_data",
                "--title",
                "电力个股日线",
                "--source-url",
                "https://example.test/power-event",
                "--published-at",
                "2026-07-20T15:00:00+08:00",
                "--event-key",
                "power-energy-2026-07-20-market-move",
                "--fact-summary",
                "行情记录电力相关个股当日上涨。",
                "--claim-boundary",
                "该行情事实不确认媒体归因。",
                "--verification-state",
                "outer_page_verified",
                "--domain",
                "power_energy",
            ],
            self.context,
        )
        overview, overview_exit, overview_json = run(
            ["--json", "research", "local-overview"], self.context
        )

        self.assertEqual(indicator_exit, 0, indicator)
        self.assertEqual(event_exit, 0, event)
        self.assertEqual(overview_exit, 0, overview)
        self.assertTrue(overview_json)
        self.assert_envelope(overview, "research.local_overview")
        self.assertEqual(overview["data"]["coverage"]["persisted_indicator_record_count"], 1)
        self.assertEqual(overview["data"]["domains"][0]["event_counts"]["fact"], 1)
        self.assertTrue(overview["data"]["outer_discovery"]["outer_discovery_required"])
        self.assertEqual(
            overview["data"]["outer_discovery"]["current_news_refresh_state"], "pending"
        )

    def test_research_discovery_plan_uses_persisted_candidate_and_starts_pending(self) -> None:
        stored, stored_exit, _ = run(
            [
                "--json",
                "research",
                "evidence-store",
                "--source-name",
                "公开统计",
                "--source-kind",
                "industry_data",
                "--title",
                "6月用电量",
                "--published-at",
                "2026-07-19T08:00:00+08:00",
                "--source-url",
                "https://example.test/power-june-plan",
                "--statistics-period",
                "2026年6月",
                "--indicator-name",
                "全社会用电量",
                "--indicator-value",
                "8981",
                "--unit",
                "亿千瓦时",
                "--verification-state",
                "outer_page_verified",
                "--domain",
                "power_energy",
            ],
            self.context,
        )
        terminal = AuthenticatedTerminalRequest(
            id=23,
            request_id="d" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=self.suitability_now,
            deadline_at=self.suitability_now + timedelta(seconds=90),
            finished_at=self.suitability_now + timedelta(seconds=1),
            omitted_work=("source_unavailable",),
        )

        class FakeIntelligenceService:
            def market_overview(inner_self, **_kwargs):
                return PragmaticIntelligenceResult(
                    report=None,
                    terminal_request=terminal,
                    subject=IntelligenceRequestSubject(
                        workflow=IntelligenceWorkflow.MARKET_OVERVIEW,
                        interval=QueryInterval(
                            self.suitability_now - timedelta(hours=72),
                            self.suitability_now,
                            "Asia/Shanghai",
                        ),
                        subject_scope="global_public",
                        fund_code=None,
                    ),
                    items=(),
                    item_uses=(),
                    lineage_edges=(),
                    events=(),
                    source_summaries=(),
                    sector_labels=(),
                    fund_context=None,
                    thesis_review=None,
                )

        self.context.intelligence_service = FakeIntelligenceService()
        payload, exit_code, json_output = run(
            ["--json", "research", "discovery-plan", "--window", "recent"], self.context
        )

        self.assertEqual(stored_exit, 0, stored)
        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "research.discovery_plan")
        plans = payload["data"]["candidate_plans"]
        self.assertEqual([item["domain_id"] for item in plans], ["power_energy"])
        self.assertEqual(plans[0]["current_news_refresh_state"], "pending")
        self.assertTrue(payload["data"]["scan"]["outer_discovery"]["outer_discovery_required"])

    def test_research_panorama_uses_month_and_quarter_market_windows(self) -> None:
        terminal = AuthenticatedTerminalRequest(
            id=21,
            request_id="c" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=self.suitability_now,
            deadline_at=self.suitability_now + timedelta(seconds=90),
            finished_at=self.suitability_now + timedelta(seconds=1),
            omitted_work=("source_unavailable",),
        )

        class FakeIntelligenceService:
            calls = []

            def market_overview(inner_self, **kwargs):
                inner_self.calls.append(kwargs)
                return PragmaticIntelligenceResult(
                    report=None, terminal_request=terminal,
                    subject=IntelligenceRequestSubject(
                        workflow=IntelligenceWorkflow.MARKET_OVERVIEW,
                        interval=QueryInterval(
                            self.suitability_now - timedelta(hours=72),
                            self.suitability_now,
                            "Asia/Shanghai",
                        ),
                        subject_scope="global_public", fund_code=None,
                    ), items=(), item_uses=(), lineage_edges=(), events=(),
                    source_summaries=(), sector_labels=(), fund_context=None,
                    thesis_review=None,
                )

        service = FakeIntelligenceService()
        self.context.intelligence_service = service
        payload, exit_code, json_output = run(["--json", "research", "panorama"], self.context)

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "research.panorama")
        self.assertEqual(len(service.calls), 2)
        self.assertTrue(all(item["mode"] == "rapid" for item in service.calls))
        self.assertEqual(
            [(item["end"] - item["start"]).days for item in service.calls],
            [29, 89],
        )

    def test_portfolio_review_manual_mode_does_not_synchronize(self) -> None:
        class MustNotSync:
            def sync_portfolio(self, **_kwargs):
                raise AssertionError("manual portfolio review must not synchronize")

        self.context.sync_service = MustNotSync()
        payload, exit_code, json_output = run(
            [
                "--json",
                "portfolio",
                "review",
                "--manual-position",
                "123456=60",
                "--manual-position",
                "654321=40",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "portfolio.review")
        self.assertEqual(payload["data"]["input_source"], "manual_temporary")
        self.assertFalse(
            payload["data"]["conditional_guidance"]["automatic_trade"]
        )

    def test_fund_review_composes_public_context_conservatively(self) -> None:
        self.context.brief_service = SimpleNamespace(
            brief_outcome=lambda *_args, **_kwargs: object()
        )
        self.context.intelligence_service = SimpleNamespace(
            fund_intelligence=lambda *_args, **_kwargs: object(),
            market_overview=lambda *_args, **_kwargs: object(),
        )
        with (
            patch("kunjin.brief.research.public_outcome_payload", return_value={}),
            patch(
                "kunjin.intelligence.research.public_intelligence_payload",
                return_value={},
            ),
            patch(
                "kunjin.cli.summarize_public_research",
                return_value={"sources": [], "what_happened": [], "retrieval": {}},
            ),
            patch("kunjin.cli.scan_public_research", return_value={"directions": []}),
        ):
            payload, exit_code, json_output = run(
                [
                    "--json",
                    "fund",
                    "review",
                    "123456",
                    "--portfolio-context",
                    "cached",
                ],
                self.context,
            )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.review")
        self.assertEqual(payload["data"]["conclusion"]["disposition"], "需补充信息")
        self.assertFalse(
            payload["data"]["conditional_guidance"]["automatic_trade"]
        )
        self.assertEqual(
            payload["data"]["portfolio_snapshot"]["stability_state"], "stable"
        )

    def test_fund_review_cached_public_context_skips_brief_and_market_refresh(self) -> None:
        class MustNotRefresh:
            def __getattr__(self, _name):
                raise AssertionError("cached fund review must not refresh public sources")

        self.context.brief_service = MustNotRefresh()
        self.context.intelligence_service = MustNotRefresh()
        payload, exit_code, json_output = run(
            [
                "--json",
                "fund",
                "review",
                "016067",
                "--portfolio-context",
                "cached",
                "--cached-public-context",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.review")
        self.assertEqual(payload["data"]["public_context_refresh"]["state"], "not_refreshed")
        self.assertEqual(
            payload["data"]["portfolio_snapshot"]["stability_state"], "stable"
        )

    def test_fund_review_related_group_is_explicit_and_non_transactional(self) -> None:
        self.context.brief_service = SimpleNamespace(
            brief_outcome=lambda *_args, **_kwargs: object()
        )
        self.context.intelligence_service = SimpleNamespace(
            fund_intelligence=lambda *_args, **_kwargs: object(),
            market_overview=lambda *_args, **_kwargs: object(),
        )
        comparison = {
            "coverage": {"members_total": 2, "members_with_disclosures": 2},
            "pairwise_overlap": [],
            "sources": [],
            "warnings": [],
            "data_gaps": [],
        }
        with (
            patch("kunjin.brief.research.public_outcome_payload", return_value={}),
            patch("kunjin.intelligence.research.public_intelligence_payload", return_value={}),
            patch(
                "kunjin.cli.summarize_public_research",
                return_value={"sources": [], "what_happened": [], "retrieval": {}},
            ),
            patch("kunjin.cli.scan_public_research", return_value={"directions": []}),
            patch("kunjin.cli.build_explicit_compare_report", return_value=comparison),
        ):
            payload, exit_code, json_output = run(
                [
                    "--json",
                    "fund",
                    "review",
                    "123456",
                    "--related-fund",
                    "654321",
                ],
                self.context,
            )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.review")
        self.assertEqual(
            payload["data"]["related_fund_context"]["fund_codes"],
            ["123456", "654321"],
        )
        self.assertFalse(payload["data"]["conditional_guidance"]["automatic_trade"])

    def test_investor_guardrails_returns_category_boundaries(self) -> None:
        payload, exit_code, json_output = run(
            [
                "--json",
                "investor",
                "guardrails",
                "--emergency-fund",
                "yes",
                "--near-term-use",
                "no",
                "--horizon",
                "long",
                "--volatility",
                "medium",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "investor.guardrails")
        self.assertEqual(payload["data"]["readiness"], "可以继续研究")
        self.assertFalse(payload["data"]["action_boundary"]["automatic_trade"])
        self.assertEqual(
            payload["data"]["portfolio_research"]["state"],
            "portfolio_unavailable",
        )

    def test_fund_candidates_requires_profile_before_research_candidates(self) -> None:
        with patch(
            "kunjin.cli.build_explicit_compare_report",
            return_value={
                "coverage": {"members_with_disclosures": 2, "members_total": 2},
                "metric_orderings": {},
                "sources": [],
                "candidate_portfolio_overlap": {},
                "pairwise_overlap": [],
                "warnings": [],
                "data_gaps": [],
            },
        ):
            payload, exit_code, json_output = run(
                ["--json", "fund", "candidates", "123456", "654321"], self.context
            )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.candidates")
        self.assertEqual(payload["data"]["conclusion"]["disposition"], "需补充个人信息")
        self.assertFalse(payload["data"]["conditional_guidance"]["automatic_trade"])
        self.assertEqual(
            payload["data"]["portfolio_snapshot"]["stability_state"], "stable"
        )

    def test_portfolio_show_and_diagnose_share_one_snapshot_binding(self) -> None:
        shown, shown_exit, _ = run(["--json", "portfolio", "show"], self.context)
        diagnosed, diagnosed_exit, _ = run(
            ["--json", "portfolio", "diagnose"], self.context
        )

        self.assertEqual(shown_exit, 0, shown)
        self.assertEqual(diagnosed_exit, 1, diagnosed)
        self.assertEqual(diagnosed["errors"][0]["code"], "insufficient_data")
        snapshot = shown["data"]["portfolio_snapshot"]
        diagnosis_snapshot = diagnosed["data"]["portfolio_snapshot"]
        for key in (
            "sync_run_id",
            "observation_version",
            "observation_at",
            "positive_position_count",
            "positive_position_fingerprint",
        ):
            self.assertEqual(snapshot[key], diagnosis_snapshot[key])
        self.assertEqual(diagnosis_snapshot["stability_state"], "stable")

    def test_fund_review_triggers_are_on_demand_only(self) -> None:
        payload, exit_code, json_output = run(
            ["--json", "fund", "review-triggers", "123456"], self.context
        )

        self.assertEqual(exit_code, 0, payload)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.review-triggers")
        self.assertFalse(payload["data"]["action_boundary"]["automatic_monitoring"])
        self.assertFalse(payload["data"]["action_boundary"]["automatic_trade"])

    def test_phase0_decision_route_parser_and_json_contract(self) -> None:
        payload, exit_code, json_output = run(
            [
                "--json",
                "decision",
                "route",
                "--action",
                "fact_research",
                "--action",
                "continue_holding",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "decision.route")
        self.assertEqual(
            set(payload["data"]),
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
            },
        )
        self.assertEqual(payload["data"]["mode"], "rapid")
        self.assertEqual(payload["data"]["workflow_level"], "rapid_evidence")
        self.assertEqual(
            [item["action"] for item in payload["data"]["actions"]],
            ["fact_research", "continue_holding"],
        )
        self.assertRegex(payload["data"]["request_id"], r"^[0-9a-f]{32}$")
        for key in ("policy_checksum", "registry_checksum", "result_checksum"):
            self.assertRegex(payload["data"][key], r"^[0-9a-f]{64}$")
        self.assertEqual(
            datetime.fromisoformat(payload["data"]["created_at"]).utcoffset(),
            timedelta(0),
        )

    def test_phase0_decision_route_validator_rejects_deep_shape_and_private_values(
        self,
    ) -> None:
        payload, exit_code, _ = run(
            ["--json", "decision", "route", "--action", "fact_research"],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        valid = payload["data"]
        _validate_decision_route_data(valid)

        mutations = (
            ("action_maturity", 7),
            ("minimum_state", "target_amount"),
            ("exact_amount_available", 1),
            ("research_available", "true"),
            ("required_gates", [918273645001]),
            ("blocking_codes", {"monthly_net_income": 918273645001}),
        )
        for key, private_value in mutations:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(valid))
                changed["actions"][0][key] = private_value
                with self.assertRaises(ValueError):
                    _validate_decision_route_data(changed)

        top_level_mutations = (
            ("opposing_evidence", [918273645001]),
            ("missing_fields", {"target_amount": 918273645001}),
            ("conclusion_evidence", [{"monthly_net_income": 918273645001}]),
        )
        for key, private_value in top_level_mutations:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(valid))
                changed[key] = private_value
                with self.assertRaises(ValueError):
                    _validate_decision_route_data(changed)

        changed = json.loads(json.dumps(valid))
        changed["created_at"] = changed["created_at"].replace("+00:00", "Z")
        with self.assertRaises(ValueError):
            _validate_decision_route_data(changed)

        conclusion = {
            "completeness": "partial",
            "conflicts": [],
            "coverage_percent": None,
            "freshness": "dated_history",
            "independent_lineage_count": 0,
            "inferred": False,
            "lineage_ids": [],
            "market_as_of": None,
            "missing_critical_fields": ["identity_active_status"],
            "publication_times": [],
            "publishers": ["owner supplied public document"],
            "report_as_of": None,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_ids": [],
            "source_tier": "user_provided",
        }
        _conclusion_evidence_from_public(conclusion)
        for private_publisher in (
            "target_amount=918273645001",
            f"managed_path={self.context.paths.database}",
            "failed at /private/tmp/output.log",
            "runtime traceback: line 7",
            "monthly%20net%20income=918273645001",
            "monthly%2520net%2520income=918273645001",
            "918273645001",
            "owner value 918273645001",
            "income 918273645001",
        ):
            with self.subTest(private_publisher=private_publisher):
                changed = json.loads(json.dumps(conclusion))
                changed["publishers"] = [private_publisher]
                with self.assertRaises(ValueError):
                    _conclusion_evidence_from_public(changed)
        changed = json.loads(json.dumps(conclusion))
        changed["independent_lineage_count"] = 1
        changed["lineage_ids"] = ["target_amount"]
        with self.assertRaises(ValueError):
            _conclusion_evidence_from_public(changed)
        changed["lineage_ids"] = ["target_amount_918273645001"]
        with self.assertRaises(ValueError):
            _conclusion_evidence_from_public(changed)
        for private_lineage in (
            "income_value_918273645001",
            "private_918273645001",
        ):
            changed["lineage_ids"] = [private_lineage]
            with self.assertRaises(ValueError):
                _conclusion_evidence_from_public(changed)

        for legitimate_publisher in (
            "7 Fund Management",
            "Amount Asset Management",
        ):
            with self.subTest(legitimate_publisher=legitimate_publisher):
                changed = json.loads(json.dumps(conclusion))
                changed["publishers"] = [legitimate_publisher]
                _conclusion_evidence_from_public(changed)
        changed = json.loads(json.dumps(conclusion))
        changed["independent_lineage_count"] = 1
        changed["lineage_ids"] = ["market_context_v2"]
        _conclusion_evidence_from_public(changed)

    def test_phase0_decision_route_supports_deep_and_switch_legs(self) -> None:
        payload, exit_code, _ = run(
            [
                "--json",
                "decision",
                "route",
                "--mode",
                "deep",
                "--action",
                "switch_funds",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["mode"], "deep")
        self.assertEqual(payload["data"]["workflow_level"], "decision_evidence")
        self.assertEqual(
            [item["action_id"] for item in payload["data"]["actions"]],
            ["switch_reduce", "switch_buy"],
        )
        self.assertEqual(
            [item["risk_effect"] for item in payload["data"]["actions"]],
            ["risk_reducing", "risk_increasing"],
        )

    def test_phase0_facts_survive_blocked_phase_b(self) -> None:
        blocked_profile = replace(valid_profile(), emergency_reserve=Decimal("0.00"))
        self.context.profile_service.confirm_profile(blocked_profile)
        assessed, assessed_exit, _ = run(
            ["--json", "suitability", "assess"], self.context
        )
        self.assertEqual(assessed_exit, 0)
        self.assertEqual(assessed["data"]["status"], "blocked")

        payload, exit_code, _ = run(
            [
                "--json",
                "decision",
                "route",
                "--action",
                "fact_research",
                "--action",
                "continue_holding",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        facts, holding = payload["data"]["actions"]
        self.assertTrue(facts["research_available"])
        self.assertEqual(facts["blocking_codes"], [])
        self.assertEqual(holding["minimum_state"], "no_add")
        self.assertIn("phase_b_blocked", holding["blocking_codes"])

    def test_phase0_decision_route_rejects_non_json_and_invalid_actions(self) -> None:
        cases = (
            (["decision", "route", "--action", "fact_research"], "decision.route"),
            (["--json", "decision", "route"], "decision.route"),
            (
                [
                    "--json",
                    "decision",
                    "route",
                    "--action",
                    "fact_research",
                    "--action",
                    "fact_research",
                ],
                "decision.route",
            ),
            (
                [
                    "--json",
                    "decision",
                    "route",
                    "--mode",
                    "invalid",
                    "--action",
                    "fact_research",
                ],
                "decision.route",
            ),
        )
        for argv, expected_command in cases:
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, expected_command)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_phase0_source_status_is_read_only_and_separates_resolutions(self) -> None:
        class SuitabilityMustNotRun:
            def status(inner_self):
                raise AssertionError("source status must not read Phase B")

        original_suitability = self.context.suitability_service
        self.context.suitability_service = SuitabilityMustNotRun()
        try:
            payload, exit_code, json_output = run(
                ["--json", "source", "status", "--fund-code", "000000"],
                self.context,
            )
        finally:
            self.context.suitability_service = original_suitability

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "source.status")
        self.assertEqual(
            set(payload["data"]),
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
            },
        )
        self.assertEqual(payload["data"]["fund_code"], "000000")
        self.assertTrue(payload["data"]["source_fields"])
        self.assertTrue(payload["data"]["request_field_resolutions"])
        source_states = {item["state"] for item in payload["data"]["source_fields"]}
        self.assertTrue(source_states <= {item.value for item in SourceFieldState})
        resolutions = {
            item["resolution"]
            for item in payload["data"]["request_field_resolutions"]
        }
        self.assertTrue(
            resolutions <= {item.value for item in RequestFieldResolution}
        )
        for item in payload["data"]["source_fields"]:
            self.assertEqual(
                set(item),
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
                },
            )

    def test_phase0_source_status_validator_rejects_nested_private_values(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "source", "status", "--fund-code", "000000"],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        valid = payload["data"]
        _validate_source_status_data(valid)

        snapshot_at = datetime.fromisoformat(valid["snapshot_at"])
        boundary = json.loads(json.dumps(valid))
        row = boundary["source_fields"][0]
        row["state"] = "cooldown"
        row["last_failure_at"] = (snapshot_at - timedelta(seconds=1)).isoformat()
        row["last_failure_reason"] = "network_timeout"
        row["consecutive_failures"] = 1
        row["cooldown_until"] = (snapshot_at + timedelta(seconds=1)).isoformat()
        with patch(
            "kunjin.cli._request_finish_now",
            return_value=snapshot_at + timedelta(seconds=2),
        ):
            _validate_source_status_data(boundary)
        expired = json.loads(json.dumps(boundary))
        expired["source_fields"][0]["cooldown_until"] = valid["snapshot_at"]
        with self.assertRaises(ValueError):
            _validate_source_status_data(expired)
        noncanonical = json.loads(json.dumps(valid))
        noncanonical["snapshot_at"] = noncanonical["snapshot_at"].replace(
            "+00:00", "Z"
        )
        with self.assertRaises(ValueError):
            _validate_source_status_data(noncanonical)

        mutations = (
            ("consecutive_failures", True),
            ("field_scope", "monthly_net_income=918273645001"),
            ("last_failure_reason", "target_amount"),
            ("acceptable_alternatives", [{"monthly_net_income": 918273645001}]),
            ("supplementation", {"target_amount": 918273645001}),
        )
        for key, private_value in mutations:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(valid))
                changed["source_fields"][0][key] = private_value
                with self.assertRaises(ValueError):
                    _validate_source_status_data(changed)

        changed = json.loads(json.dumps(valid))
        changed["source_fields"][0]["state"] = "healthy"
        with self.assertRaises(ValueError):
            _validate_source_status_data(changed)

        now_text = datetime.now(timezone.utc).isoformat()
        future_text = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        state_mutations = (
            ("unsupported", "network_timeout", 1, None),
            ("cooldown", "field_unsupported", 1, future_text),
            ("unsupported", "field_unsupported", 0, None),
            ("cooldown", "network_timeout", 0, future_text),
        )
        for state, reason, count, cooldown in state_mutations:
            with self.subTest(state=state, reason=reason, count=count):
                changed = json.loads(json.dumps(valid))
                row = changed["source_fields"][0]
                row["state"] = state
                row["last_failure_at"] = now_text
                row["last_failure_reason"] = reason
                row["consecutive_failures"] = count
                row["cooldown_until"] = cooldown
                with self.assertRaises(ValueError):
                    _validate_source_status_data(changed)

        resolution_mutations = (
            ("field_id", 918273645001),
            ("resolution", "target_amount"),
            ("primary_source_id", {"monthly_net_income": 918273645001}),
        )
        for key, private_value in resolution_mutations:
            with self.subTest(key=key):
                changed = json.loads(json.dumps(valid))
                changed["request_field_resolutions"][0][key] = private_value
                with self.assertRaises(ValueError):
                    _validate_source_status_data(changed)

    def test_phase0_source_status_optional_code_and_strict_validation(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "source", "status"], self.context
        )
        self.assertEqual(exit_code, 0)
        self.assertIsNone(payload["data"]["fund_code"])

        for invalid in ("12345", "1234567", "abcdef", "12345a"):
            with self.subTest(invalid=invalid):
                payload, exit_code, _ = run(
                    ["--json", "source", "status", "--fund-code", invalid],
                    self.context,
                )
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "source.status")
                self.assertEqual(payload["errors"][0]["code"], "invalid_fund_code")

    def test_phase0_source_status_without_code_never_inherits_fund_000000(self) -> None:
        now = datetime.now(timezone.utc)
        budget = RequestBudget.create(
            RequestMode.RAPID,
            wall_clock=lambda: now - timedelta(seconds=3),
        )
        store = self.context.source_health_service.audit_store
        request_run_id = store.begin_request(budget)
        attempt = SourceAttempt(
            source_id="eastmoney_nav",
            field_id="formal_nav",
            subject_key="fund:000000",
            attempt_number=1,
            outcome=SourceAttemptOutcome.SUCCESS,
            started_at=now - timedelta(seconds=2),
            finished_at=now - timedelta(seconds=1),
            data_as_of=now - timedelta(days=1),
            error_code=None,
            cooldown_until=None,
            force_actor=None,
            force_reason=None,
            registry_version=self.context.source_health_service.registry.version,
            registry_checksum=self.context.source_health_service.registry.checksum(),
            response_bytes=10,
        )
        store.record_source_attempt(request_run_id, attempt)
        store.finalize_request(
            request_run_id,
            RequestTerminalStatus.COMPLETE,
            attempt.finished_at,
            (),
        )

        unscoped, unscoped_exit, _ = run(
            ["--json", "source", "status"], self.context
        )
        scoped, scoped_exit, _ = run(
            ["--json", "source", "status", "--fund-code", "000000"],
            self.context,
        )

        self.assertEqual((unscoped_exit, scoped_exit), (0, 0))
        unscoped_nav = next(
            item
            for item in unscoped["data"]["source_fields"]
            if item["source_id"] == "eastmoney_nav"
            and item["field_id"] == "formal_nav"
        )
        scoped_nav = next(
            item
            for item in scoped["data"]["source_fields"]
            if item["source_id"] == "eastmoney_nav"
            and item["field_id"] == "formal_nav"
        )
        self.assertEqual(unscoped_nav["state"], "not_checked")
        self.assertIsNone(unscoped_nav["last_success_at"])
        self.assertIsNotNone(scoped_nav["last_success_at"])

    def test_phase0_source_status_expiry_is_audited_as_expired(self) -> None:
        class Clock:
            value = 0.0

            def __call__(inner_self):
                return inner_self.value

        clock = Clock()
        now = datetime.now(timezone.utc)
        budget = RequestBudget.create(
            RequestMode.RAPID,
            monotonic=clock,
            wall_clock=lambda: now,
        )
        clock.value = budget.total_seconds

        with patch("kunjin.cli.RequestBudget.create", return_value=budget):
            payload, exit_code, _ = run(
                ["--json", "source", "status", "--fund-code", "000000"],
                self.context,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errors"][0]["code"], "source_status_failed")
        with self.context.repository.connect() as connection:
            row = connection.execute(
                "SELECT status, finished_at, deadline_at FROM request_runs "
                "WHERE request_id = ?",
                (budget.request_id,),
            ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["finished_at"], row["deadline_at"])

    def test_phase0_source_status_does_not_count_scheduler_skip_as_failure(self) -> None:
        now = datetime.now(timezone.utc)
        store = self.context.source_health_service.audit_store
        registry = self.context.source_health_service.registry

        def record(attempt, request_started_at):
            budget = RequestBudget.create(
                RequestMode.RAPID,
                wall_clock=lambda: request_started_at,
            )
            request_run_id = store.begin_request(budget)
            store.record_source_attempt(request_run_id, attempt)
            store.finalize_request(
                request_run_id,
                RequestTerminalStatus.COMPLETE,
                attempt.finished_at,
                (),
            )

        cooldown_until = now + timedelta(minutes=25)
        failure = SourceAttempt(
            source_id="eastmoney_nav",
            field_id="formal_nav",
            subject_key="fund:000000",
            attempt_number=1,
            outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
            started_at=now - timedelta(seconds=5),
            finished_at=now - timedelta(seconds=4),
            data_as_of=None,
            error_code=SourceErrorCode.NETWORK_TIMEOUT,
            cooldown_until=cooldown_until,
            force_actor=None,
            force_reason=None,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=0,
        )
        skipped = replace(
            failure,
            outcome=SourceAttemptOutcome.SKIPPED_COOLDOWN,
            started_at=now - timedelta(seconds=3),
            finished_at=now - timedelta(seconds=2),
            error_code=SourceErrorCode.COOLDOWN_ACTIVE,
        )
        record(failure, now - timedelta(seconds=6))
        record(skipped, now - timedelta(seconds=4))

        payload, exit_code, _ = run(
            ["--json", "source", "status", "--fund-code", "000000"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        nav = next(
            item
            for item in payload["data"]["source_fields"]
            if item["source_id"] == "eastmoney_nav"
            and item["field_id"] == "formal_nav"
        )
        self.assertEqual(nav["state"], "cooldown")
        self.assertEqual(nav["last_failure_reason"], "network_timeout")
        self.assertEqual(nav["consecutive_failures"], 1)

    def test_phase0_source_status_allows_current_success_with_newer_failure(self) -> None:
        now = datetime.now(timezone.utc)
        store = self.context.source_health_service.audit_store
        registry = self.context.source_health_service.registry

        def record(attempt, request_started_at):
            budget = RequestBudget.create(
                RequestMode.RAPID,
                wall_clock=lambda: request_started_at,
            )
            request_run_id = store.begin_request(budget)
            store.record_source_attempt(request_run_id, attempt)
            store.finalize_request(
                request_run_id,
                RequestTerminalStatus.COMPLETE,
                attempt.finished_at,
                (),
            )

        success = SourceAttempt(
            source_id="eastmoney_nav",
            field_id="formal_nav",
            subject_key="fund:000000",
            attempt_number=1,
            outcome=SourceAttemptOutcome.SUCCESS,
            started_at=now - timedelta(seconds=10),
            finished_at=now - timedelta(seconds=9),
            data_as_of=now - timedelta(days=1),
            error_code=None,
            cooldown_until=None,
            force_actor=None,
            force_reason=None,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=10,
        )
        failure = replace(
            success,
            outcome=SourceAttemptOutcome.UNAVAILABLE,
            started_at=now - timedelta(seconds=4),
            finished_at=now - timedelta(seconds=3),
            data_as_of=None,
            error_code=SourceErrorCode.PARSE_FAILURE,
            response_bytes=0,
        )
        record(success, now - timedelta(seconds=11))
        record(failure, now - timedelta(seconds=5))

        payload, exit_code, _ = run(
            ["--json", "source", "status", "--fund-code", "000000"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        nav = next(
            item
            for item in payload["data"]["source_fields"]
            if item["source_id"] == "eastmoney_nav"
            and item["field_id"] == "formal_nav"
        )
        self.assertIn(nav["state"], {"healthy", "degraded"})
        self.assertEqual(nav["consecutive_failures"], 1)
        self.assertEqual(nav["last_failure_reason"], "parse_failure")
        self.assertIsNotNone(nav["last_success_at"])

    def test_phase0_command_errors_never_publish_private_exception_details(self) -> None:
        private_details = (
            "access_token=never-print-this monthly_net_income=918273645001 "
            f"managed_path={self.context.paths.database} worker_pid=42424242424242424242"
        )

        class FailingDecisionService:
            def route(inner_self, budget, actions):
                raise RuntimeError(private_details)

        class FailingSourceHealthService:
            @property
            def audit_store(inner_self):
                raise RuntimeError(private_details)

        self.context.decision_service = FailingDecisionService()
        decision, decision_exit, _ = run(
            ["--json", "decision", "route", "--action", "fact_research"],
            self.context,
        )
        self.context.source_health_service = FailingSourceHealthService()
        source, source_exit, _ = run(
            ["--json", "source", "status"], self.context
        )

        self.assertEqual((decision_exit, source_exit), (1, 1))
        self.assertEqual(decision["errors"][0]["code"], "decision_command_failed")
        self.assertEqual(source["errors"][0]["code"], "source_status_failed")
        self.assertEqual(decision["errors"][0]["message"], "decision routing failed")
        self.assertEqual(source["errors"][0]["message"], "source status failed")
        rendered = json.dumps({"decision": decision, "source": source})
        for private in (
            "never-print-this",
            "918273645001",
            str(self.context.paths.database),
            "42424242424242424242",
        ):
            self.assertNotIn(private, rendered)

    def test_profile_edit_rejects_json_with_stable_error(self) -> None:
        payload, exit_code, json_output = run(["--json", "profile", "edit"], self.context)

        self.assertEqual(exit_code, 1)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "profile.edit")
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")
        self.assertEqual(
            payload["errors"][0]["message"],
            "profile edit is interactive and does not support JSON mode",
        )

    def test_profile_status_reports_missing_without_sensitive_values(self) -> None:
        payload, exit_code, _ = run(["--json", "profile", "status"], self.context)

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "profile.status")
        self.assertEqual(payload["data"], {"state": "missing", "freshness": "missing"})

    def test_profile_status_and_history_expose_metadata_only(self) -> None:
        self.context.profile_service.confirm_profile(valid_profile())
        active = self.context.profile_service._store.active_encrypted()
        self.assertIsNotNone(active)

        status, status_exit_code, _ = run(["--json", "profile", "status"], self.context)
        history, history_exit_code, _ = run(["--json", "profile", "history"], self.context)

        self.assertEqual(status_exit_code, 0)
        self.assertEqual(history_exit_code, 0)
        self.assert_envelope(status, "profile.status")
        self.assert_envelope(history, "profile.history")
        self.assertEqual(status["data"]["state"], "confirmed")
        self.assertEqual(len(history["data"]["profiles"]), 1)
        rendered = json.dumps({"status": status, "history": history}, ensure_ascii=False)
        for sensitive in (
            "12000",
            "500000",
            "40000",
            "fake-profile-key-secret-value",
            active.encrypted.ciphertext,
            active.encrypted.nonce,
            active.encrypted.keyed_fingerprint,
            str(self.context.paths.database),
        ):
            self.assertNotIn(sensitive, rendered)

    def test_profile_crypto_error_has_stable_command_and_error_code(self) -> None:
        class UnavailableProfileService:
            def status(inner_self):
                raise ProfileCryptoError("profile encryption key is unavailable")

        self.context.profile_service = UnavailableProfileService()

        payload, exit_code, _ = run(["--json", "profile", "status"], self.context)

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "profile.status")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "encrypted_profile_unavailable",
                    "message": "profile encryption key is unavailable",
                }
            ],
        )

    def test_profile_crypto_error_redacts_financial_and_encryption_details(self) -> None:
        database_path = str(self.context.paths.database)

        class LeakingProfileService:
            def status(inner_self):
                raise ProfileCryptoError(
                    "monthly_net_income=918273645001 "
                    "emergency_reserve=918273645002 "
                    "ciphertext=ciphertext-secret "
                    "nonce=nonce-secret "
                    "keyed_payload_fingerprint=fingerprint-secret "
                    "profile_key=fake-profile-key-secret-value "
                    f"managed_path={database_path}"
                )

        self.context.profile_service = LeakingProfileService()

        payload, exit_code, _ = run(["--json", "profile", "status"], self.context)

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "profile.status")
        rendered = json.dumps(payload, ensure_ascii=False)
        for sensitive in (
            "918273645001",
            "918273645002",
            "ciphertext-secret",
            "nonce-secret",
            "fingerprint-secret",
            "fake-profile-key-secret-value",
            database_path,
        ):
            self.assertNotIn(sensitive, rendered)

    def test_profile_decryption_failure_envelope_contains_no_private_material(self) -> None:
        original_service = self.context.profile_service
        original_service.confirm_profile(valid_profile())
        active = original_service._store.active_encrypted()
        self.assertIsNotNone(active)
        with self.context.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER financial_profile_payload_no_update")
            connection.execute(
                "UPDATE financial_profile_versions SET encrypted_payload = ? "
                "WHERE status = 'confirmed'",
                ("AAAA",),
            )

        class DecryptingProfileService:
            def status(inner_self):
                original_service.load_active_profile()
                raise AssertionError("decryption failure was expected")

        self.context.profile_service = DecryptingProfileService()

        payload, exit_code, _ = run(["--json", "profile", "status"], self.context)

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "profile.status")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "encrypted_profile_unavailable",
                    "message": "profile decryption failed",
                }
            ],
        )
        rendered = json.dumps(payload, ensure_ascii=False)
        for sensitive in (
            "12000",
            "500000",
            "40000",
            active.encrypted.ciphertext,
            active.encrypted.nonce,
            active.encrypted.keyed_fingerprint,
            "fake-profile-key-secret-value",
            str(self.context.paths.database),
        ):
            self.assertNotIn(sensitive, rendered)

    def test_suitability_assess_returns_all_financial_states_with_exit_zero(self) -> None:
        profiles = (
            (
                replace(
                    valid_profile(),
                    debts=(replace(valid_profile().debts[0], delinquent=True),),
                ),
                "blocked",
            ),
            (
                replace(
                    valid_profile(),
                    immediately_available_cash=Decimal("1000000"),
                    cash_like_assets=Decimal("0"),
                    emergency_reserve=Decimal("1000000"),
                ),
                "constrained",
            ),
            (
                replace(
                    valid_profile(),
                    immediately_available_cash=Decimal("1000000"),
                    cash_like_assets=Decimal("0"),
                    emergency_reserve=Decimal("1000000"),
                    obligations=(),
                    goals=(),
                ),
                "ready_for_allocation",
            ),
        )

        for profile, expected_status in profiles:
            with self.subTest(expected_status=expected_status):
                self.context.profile_service.confirm_profile(profile)
                payload, exit_code, json_output = run(
                    ["--json", "suitability", "assess"], self.context
                )

                self.assertEqual(exit_code, 0)
                self.assertTrue(json_output)
                self.assert_envelope(payload, "suitability.assess")
                self.assertEqual(payload["data"]["status"], expected_status)
                self.assertEqual(payload["data"]["capability"], "research_only")

    def test_suitability_local_assess_is_explicit_exact_amount_view(self) -> None:
        synthetic = replace(
            valid_profile(),
            monthly_net_income=Decimal("200000"),
            monthly_essential_expenses=Decimal("1000"),
            monthly_required_debt_service=Decimal("0"),
            monthly_investment_ceiling=Decimal("95311"),
            minimum_monthly_cash_buffer=Decimal("0"),
            immediately_available_cash=Decimal("73129"),
            cash_like_assets=Decimal("0"),
            emergency_reserve=Decimal("73129"),
            debts=(),
            obligations=(
                PlannedObligation(
                    "private-synthetic-obligation",
                    Decimal("72217"),
                    date(2027, 1, 1),
                    Decimal("0"),
                ),
            ),
            goals=(),
        )
        self.context.profile_service.confirm_profile(synthetic)

        payload, exit_code, json_output = run(["suitability", "assess"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertFalse(json_output)
        self.assert_envelope(payload, "suitability.assess")
        self.assertEqual(payload["data"]["capability"], "research_only")
        amounts = payload["data"]["amounts"]
        self.assertEqual(amounts["verified_emergency_reserve"], "73129.00")
        self.assertEqual(amounts["required_emergency_reserve"], "84217.00")
        self.assertEqual(amounts["safe_monthly_ceiling"], "95311.00")
        self.assertNotIn("private-synthetic-obligation", json.dumps(payload))

    def test_suitability_json_views_are_amount_free_and_hide_private_material(self) -> None:
        synthetic = replace(
            valid_profile(),
            monthly_net_income=Decimal("200000"),
            monthly_essential_expenses=Decimal("1000"),
            monthly_required_debt_service=Decimal("0"),
            monthly_investment_ceiling=Decimal("95311"),
            minimum_monthly_cash_buffer=Decimal("0"),
            immediately_available_cash=Decimal("73129"),
            cash_like_assets=Decimal("0"),
            emergency_reserve=Decimal("73129"),
            debts=valid_profile().debts,
            obligations=(
                PlannedObligation(
                    "private-synthetic-obligation",
                    Decimal("72217"),
                    date(2027, 1, 1),
                    Decimal("0"),
                ),
            ),
            goals=(),
        )
        self.context.profile_service.confirm_profile(synthetic)
        assess, assess_exit, _ = run(["--json", "suitability", "assess"], self.context)
        status, status_exit, _ = run(["--json", "suitability", "status"], self.context)
        history, history_exit, _ = run(["--json", "suitability", "history"], self.context)

        self.assertEqual((assess_exit, status_exit, history_exit), (0, 0, 0))
        self.assert_envelope(assess, "suitability.assess")
        self.assert_envelope(status, "suitability.status")
        self.assert_envelope(history, "suitability.history")
        self.assertEqual(
            assess["data"]["profile_conflicts"],
            ["monthly_required_debt_service_vs_debts"],
        )
        self.assertNotIn("profile_conflicts", status["data"])
        self.assertNotIn("profile_conflicts", history["data"]["assessments"][0])
        self.assertEqual(history["data"]["assessments"][0]["capability"], "research_only")
        stored = self.context.suitability_service._assessment_store.history()[0]
        rendered = json.dumps(
            {"assess": assess, "status": status, "history": history},
            ensure_ascii=False,
        )
        for sensitive in (
            "73129",
            "84217",
            "95311",
            "private-synthetic-obligation",
            stored.input_fingerprint,
            self.context.suitability_service._policy.canonical_json().decode("utf-8"),
            "canonical_policy_json",
            "policy_checksum",
            "high_interest_annual_rate",
            '"verified_emergency_reserve":',
            '"required_emergency_reserve":',
            '"emergency_reserve_shortfall":',
            '"required_monthly_obligation_saving":',
            '"required_monthly_goal_saving":',
            '"monthly_safety_residual":',
            '"safe_monthly_ceiling":',
            '"amounts"',
        ):
            self.assertNotIn(sensitive, rendered)
        encrypted = self.context.suitability_service._assessment_store.latest_for(
            stored.profile_version_id,
            stored.policy_version,
        ).encrypted
        for sensitive in (
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.keyed_fingerprint,
        ):
            self.assertNotIn(sensitive, rendered)

    def test_suitability_status_missing_and_history_empty_are_amount_free(self) -> None:
        status, status_exit_code, _ = run(["--json", "suitability", "status"], self.context)
        history, history_exit_code, _ = run(["--json", "suitability", "history"], self.context)

        self.assertEqual((status_exit_code, history_exit_code), (0, 0))
        self.assertEqual(
            status["data"],
            {"state": "missing", "freshness": "missing", "capability": "research_only"},
        )
        self.assertEqual(history["data"], {"assessments": []})

    def test_suitability_service_unavailable_is_stable_usage_error(self) -> None:
        self.context.suitability_service = None

        payload, exit_code, _ = run(["--json", "suitability", "assess"], self.context)

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "suitability.assess")
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")
        self.assertEqual(payload["errors"][0]["message"], "suitability service is unavailable")

    def test_suitability_technical_errors_have_stable_nonzero_envelopes(self) -> None:
        cases = (
            (
                ProfileCryptoError("profile encryption key is unavailable"),
                "encrypted_profile_unavailable",
            ),
            (SuitabilityPolicyError("suitability policy is unavailable"), "policy_unavailable"),
            (
                SuitabilityAssessmentError("suitability assessment could not be calculated"),
                "assessment_calculation_failed",
            ),
        )
        for error, expected_code in cases:
            with self.subTest(expected_code=expected_code):

                class FailingSuitabilityService:
                    def assess(inner_self):
                        raise error

                self.context.suitability_service = FailingSuitabilityService()
                payload, exit_code, _ = run(["--json", "suitability", "assess"], self.context)

                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "suitability.assess")
                self.assertEqual(payload["errors"][0]["code"], expected_code)

    def test_build_context_shares_one_profile_key_store_for_both_ciphers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
            key_store = FakeProfileKeyStore()
            with (
                patch("kunjin.cli.RuntimePaths.from_environment", return_value=paths),
                patch("kunjin.cli.ProfileKeyStore", return_value=key_store) as key_store_factory,
            ):
                context = build_context()

        self.assertEqual(key_store_factory.call_count, 1)
        self.assertIs(context.profile_service._cipher._key_store, key_store)
        self.assertIs(context.suitability_service._assessment_cipher._key_store, key_store)
        self.assertIs(context.allocation_service._cipher._key_store, key_store)
        self.assertIsNotNone(context.fund_risk_store)
        self.assertIs(context.fund_risk_service._risk_store, context.fund_risk_store)
        self.assertIs(
            context.fund_risk_service._disclosure_store,
            context.fund_disclosure_store,
        )
        self.assertIs(context.fund_risk_service._repository, context.repository)
        self.assertIs(
            context.decision_service._store,
            context.source_health_service.audit_store,
        )
        self.assertIs(
            context.decision_service._policy,
            context.source_health_service.policy,
        )
        self.assertIs(
            context.decision_service._registry,
            context.source_health_service.registry,
        )
        self.assertIsNotNone(context.brief_service)
        self.assertIs(context.brief_service._repository, context.repository)
        self.assertIs(context.brief_service._suitability_service, context.suitability_service)
        self.assertIs(
            context.brief_service._disclosure_service,
            context.fund_disclosure_service,
        )
        self.assertIs(
            context.brief_service._portfolio_service.sync_service,
            context.sync_service,
        )
        self.assertIs(
            context.brief_service._portfolio_service.repository,
            context.repository,
        )
        self.assertIs(context.brief_service._nav_service.repository, context.repository)
        self.assertIs(
            context.brief_service._audit_store,
            context.source_health_service.audit_store,
        )
        self.assertIs(
            context.brief_service._health_service,
            context.source_health_service,
        )
        self.assertIs(context.brief_service._risk_store, context.fund_risk_store)
        self.assertIsNone(context.brief_service._announcement_content_loader)
        deep_official = context.brief_service._deep_official_confirmation_service
        self.assertIsNotNone(deep_official)
        self.assertIs(deep_official.disclosure_store, context.fund_disclosure_store)
        self.assertIs(
            deep_official.audit_store,
            context.source_health_service.audit_store,
        )
        self.assertIs(
            deep_official.review_store,
            context.holding_review_service.holding_review_store,
        )
        self.assertIsNotNone(context.selection_service)
        self.assertIs(context.selection_service._repository, context.repository)
        self.assertIs(
            context.selection_service._disclosure_store,
            context.fund_disclosure_store,
        )
        self.assertIs(
            context.selection_service._classification_loader.__self__,
            context.fund_risk_service,
        )
        self.assertIs(
            context.selection_service._suitability_status_loader.__self__,
            context.suitability_service,
        )
        self.assertIs(
            context.selection_service._allocation_status_loader.__self__,
            context.allocation_service,
        )
        self.assertIsNotNone(context.research_scope_service)
        self.assertIs(
            context.research_scope_service._suitability_status_loader.__self__,
            context.suitability_service,
        )
        self.assertIs(
            context.research_scope_service._allocation_status_loader.__self__,
            context.allocation_service,
        )
        self.assertIsNotNone(context.shortlist_readiness_service)
        self.assertIs(context.shortlist_readiness_service.repository, context.repository)
        self.assertIs(
            context.shortlist_readiness_service.disclosure_store,
            context.fund_disclosure_store,
        )
        self.assertIs(
            context.shortlist_readiness_service.source_health_service,
            context.source_health_service,
        )
        self.assertIs(
            context.shortlist_readiness_service.classification_loader.__self__,
            context.fund_risk_service,
        )
        self.assertIs(
            context.shortlist_readiness_service.suitability_status_loader.__self__,
            context.suitability_service,
        )
        self.assertIs(
            context.shortlist_readiness_service.allocation_status_loader.__self__,
            context.allocation_service,
        )
        for forbidden_dependency in (
            "_profile_service",
            "_suitability_service",
            "_allocation_service",
        ):
            self.assertFalse(hasattr(context.fund_risk_service, forbidden_dependency))

    def test_build_context_lazily_injects_converter_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
            converter = SimpleNamespace(status=lambda: None)
            image_id = "sha256:" + "d" * 64
            with (
                patch("kunjin.cli.RuntimePaths.from_environment", return_value=paths),
                patch(
                    "kunjin.cli.DockerLegacyDocConverter",
                    return_value=converter,
                ) as converter_factory,
                patch.object(converter, "status", wraps=converter.status) as status,
                patch.dict(os.environ, {"KUNJIN_LEGACY_DOC_IMAGE_ID": image_id}),
            ):
                context = build_context()

        converter_factory.assert_called_once_with(
            image_id=image_id,
            runtime_paths=paths,
        )
        status.assert_not_called()
        self.assertIs(context.fund_risk_service._legacy_converter, converter)

    def test_build_context_keeps_official_confirmation_deep_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
            with patch("kunjin.cli.RuntimePaths.from_environment", return_value=paths):
                context = build_context()

        brief = context.brief_service
        deep_official = brief._deep_official_confirmation_service
        request_context = SimpleNamespace()
        omitted: list[str] = []
        with patch.object(deep_official, "confirm") as confirm:
            brief._confirm_deep_official_events(
                "123456",
                request_context,
                RequestMode.RAPID,
                omitted,
            )
            confirm.assert_not_called()

            brief._confirm_deep_official_events(
                "123456",
                request_context,
                RequestMode.DEEP,
                omitted,
            )
            confirm.assert_called_once_with("123456", request_context)

    def test_build_context_unset_or_invalid_converter_id_does_not_query_docker(self) -> None:
        for image_id in (None, "latest-or-private-host-path"):
            with self.subTest(image_id=image_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = RuntimePaths(root / "kunjin.db", root / "snapshots", root / "logs")
                environment = dict(os.environ)
                if image_id is None:
                    environment.pop("KUNJIN_LEGACY_DOC_IMAGE_ID", None)
                else:
                    environment["KUNJIN_LEGACY_DOC_IMAGE_ID"] = image_id
                with (
                    patch("kunjin.cli.RuntimePaths.from_environment", return_value=paths),
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "kunjin.funds.risk.legacy_doc.SubprocessDockerCommandRunner.run"
                    ) as docker_run,
                ):
                    context = build_context()
                    payload, exit_code, _ = run(["--json", "version"], context)

                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["data"]["version"], "0.1.0")
                docker_run.assert_not_called()

    def test_allocation_ranges_returns_financial_blocks_with_exit_zero(self) -> None:
        blocked_profile = replace(valid_profile(), emergency_reserve=Decimal("0.00"))
        self.context.profile_service.confirm_profile(blocked_profile)
        suitability, suitability_exit, _ = run(["--json", "suitability", "assess"], self.context)

        payload, exit_code, json_output = run(["--json", "allocation", "ranges"], self.context)

        self.assertEqual(suitability_exit, 0)
        self.assertEqual(suitability["data"]["status"], "blocked")
        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "allocation.ranges")
        self.assertEqual(payload["data"]["status"], "blocked")
        self.assertEqual(payload["data"]["blocks"], ["suitability_blocked"])
        self.assertEqual(payload["data"]["capability"], "research_only")

        self.context.profile_service.confirm_profile(
            replace(valid_profile(), obligations=(), goals=())
        )
        suitability, suitability_exit, _ = run(["--json", "suitability", "assess"], self.context)
        payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

        self.assertEqual(suitability_exit, 0)
        self.assertEqual(suitability["data"]["status"], "ready_for_allocation")
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["status"], "blocked")
        self.assertEqual(payload["data"]["blocks"], ["allocation_horizon_missing"])

    def test_allocation_profile_conflict_is_amount_free_financial_block(self) -> None:
        profile = replace(
            valid_profile(),
            can_postpone_goal_use=False,
            goals=(
                replace(
                    valid_profile().goals[0],
                    name="private-postponement-conflict-goal",
                    use_date_can_be_postponed=True,
                ),
            ),
        )
        self.context.profile_service.confirm_profile(profile)
        suitability, suitability_exit, _ = run(["--json", "suitability", "assess"], self.context)

        payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

        self.assertEqual(suitability_exit, 0)
        self.assertIn(
            suitability["data"]["status"],
            {"constrained", "ready_for_allocation"},
        )
        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "allocation.ranges")
        self.assertEqual(payload["data"]["status"], "blocked")
        self.assertEqual(payload["data"]["blocks"], ["allocation_profile_conflict"])
        self.assertEqual(
            payload["data"]["profile_conflicts"],
            ["profile_disallows_goal_postponement"],
        )
        self.assertEqual(self.context.allocation_service._assessment_store.history(), ())
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("private-postponement-conflict-goal", rendered)
        self.assertNotIn('"exact"', rendered)

    def test_allocation_json_views_are_amount_free_and_policy_is_transparent(self) -> None:
        synthetic = replace(
            valid_profile(),
            monthly_net_income=Decimal("87654321.09"),
            goals=(
                replace(
                    valid_profile().goals[0],
                    name="private-allocation-goal-sentinel",
                ),
            ),
        )
        self.context.profile_service.confirm_profile(synthetic)
        suitability, suitability_exit, _ = run(["--json", "suitability", "assess"], self.context)
        ranges, ranges_exit, _ = run(["--json", "allocation", "ranges"], self.context)
        status, status_exit, _ = run(["--json", "allocation", "status"], self.context)
        history, history_exit, _ = run(["--json", "allocation", "history"], self.context)
        policy, policy_exit, _ = run(["--json", "allocation", "policy"], self.context)

        self.assertEqual(
            (suitability_exit, ranges_exit, status_exit, history_exit, policy_exit),
            (0, 0, 0, 0, 0),
        )
        self.assertEqual(ranges["data"]["status"], "range_available")
        self.assertEqual(status["data"]["state"], "fresh")
        self.assertEqual(len(history["data"]["assessments"]), 1)
        self.assertEqual(policy["data"]["version"], "1")
        self.assertIn("checksum", policy["data"])
        self.assertIn("stress_loss_by_layer", policy["data"])
        rendered = json.dumps(
            {
                "suitability": suitability,
                "ranges": ranges,
                "status": status,
                "history": history,
                "policy": policy,
            },
            ensure_ascii=False,
        )
        stored = self.context.allocation_service._assessment_store.history()[0]
        encrypted = self.context.allocation_service._assessment_store.get(stored.id).encrypted
        for sensitive in (
            "87654321.09",
            "private-allocation-goal-sentinel",
            stored.input_fingerprint,
            encrypted.ciphertext,
            encrypted.nonce,
            encrypted.keyed_fingerprint,
            '"exact"',
            "target_allocation",
            "purchase_amount",
            "protected_capital_amount",
            "monthly_discretionary_ceiling",
        ):
            self.assertNotIn(sensitive, rendered)

    def test_allocation_local_ranges_is_the_only_exact_view(self) -> None:
        synthetic = replace(
            valid_profile(),
            monthly_net_income=Decimal("87654321.09"),
            goals=(
                replace(
                    valid_profile().goals[0],
                    name="private-local-allocation-goal",
                ),
            ),
        )
        self.context.profile_service.confirm_profile(synthetic)
        run(["--json", "suitability", "assess"], self.context)

        ranges, ranges_exit, ranges_json = run(["allocation", "ranges"], self.context)
        status, status_exit, _ = run(["allocation", "status"], self.context)
        history, history_exit, _ = run(["allocation", "history"], self.context)
        policy, policy_exit, _ = run(["allocation", "policy"], self.context)

        self.assertEqual(
            (ranges_exit, status_exit, history_exit, policy_exit),
            (0, 0, 0, 0),
        )
        self.assertFalse(ranges_json)
        self.assertIn("exact", ranges["data"])
        self.assertIn("private-local-allocation-goal", json.dumps(ranges))
        for amount_free in (status, history, policy):
            rendered = json.dumps(amount_free, ensure_ascii=False)
            self.assertNotIn("private-local-allocation-goal", rendered)
            self.assertNotIn('"exact"', rendered)

    def test_allocation_service_unavailable_is_stable_usage_error(self) -> None:
        self.context.allocation_service = None

        payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "allocation.ranges")
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")
        self.assertEqual(payload["errors"][0]["message"], "allocation service is unavailable")

    def test_allocation_technical_errors_have_stable_nonzero_envelopes(self) -> None:
        cases = (
            (
                AllocationPolicyError("allocation policy is unavailable"),
                "allocation_policy_unavailable",
            ),
            (
                AllocationCalculationError("allocation range could not be calculated"),
                "allocation_calculation_failed",
            ),
            (
                EncryptedProfileUnavailableError("encrypted profile is unavailable"),
                "encrypted_profile_unavailable",
            ),
        )
        for error, expected_code in cases:
            with self.subTest(expected_code=expected_code):

                class FailingAllocationService:
                    def ranges(inner_self):
                        raise error

                self.context.allocation_service = FailingAllocationService()
                payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "allocation.ranges")
                self.assertEqual(payload["errors"][0]["code"], expected_code)

    def test_allocation_errors_never_echo_private_exception_text(self) -> None:
        sentinel = "private-goal=918273645001 fingerprint=secret-fingerprint"
        cases = (
            (
                AllocationPolicyError(sentinel),
                "allocation_policy_unavailable",
                "allocation policy is unavailable",
            ),
            (
                AllocationCalculationError(sentinel),
                "allocation_calculation_failed",
                "allocation calculation failed",
            ),
            (
                EncryptedProfileUnavailableError(sentinel),
                "encrypted_profile_unavailable",
                "encrypted profile is unavailable",
            ),
            (
                ProfileCryptoError(sentinel),
                "encrypted_profile_unavailable",
                "encrypted profile is unavailable",
            ),
        )
        for error, expected_code, expected_message in cases:
            with self.subTest(expected_code=expected_code, error_type=type(error)):

                class FailingAllocationService:
                    def ranges(inner_self):
                        raise error

                self.context.allocation_service = FailingAllocationService()
                payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["errors"][0]["code"], expected_code)
                self.assertEqual(payload["errors"][0]["message"], expected_message)
                self.assertNotIn(sentinel, json.dumps(payload))

    def test_allocation_unknown_service_errors_are_fixed_for_every_command(self) -> None:
        sentinel = "private-goal=918273645001 traceback=secret-traceback"
        for error_type in (ValueError, RuntimeError):
            for action in ("ranges", "status", "history", "policy"):
                with self.subTest(error_type=error_type, action=action):

                    class FailingAllocationService:
                        def ranges(inner_self):
                            raise error_type(sentinel)

                        def status(inner_self):
                            raise error_type(sentinel)

                        def history(inner_self):
                            raise error_type(sentinel)

                        def policy(inner_self):
                            raise error_type(sentinel)

                    self.context.allocation_service = FailingAllocationService()
                    payload, exit_code, _ = run(["--json", "allocation", action], self.context)

                    self.assertEqual(exit_code, 1)
                    self.assert_envelope(payload, f"allocation.{action}")
                    self.assertEqual(
                        payload["errors"],
                        [
                            {
                                "code": "allocation_calculation_failed",
                                "message": "allocation calculation failed",
                            }
                        ],
                    )
                    rendered = json.dumps(payload, ensure_ascii=False)
                    self.assertNotIn(sentinel, rendered)
                    self.assertNotIn("918273645001", rendered)
                    self.assertNotIn("secret-traceback", rendered)

    def test_fund_brief_exact_json_invocation_is_thin_and_amount_free(self) -> None:
        report = object()

        class BriefService:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def brief_outcome(inner_self, fund_code, *, action, mode):
                inner_self.calls.append((fund_code, action, mode))
                return report

        service = BriefService()
        self.context.brief_service = service
        projected = {
            "request": {
                "action_ids": ["fact_research", "continue_holding"],
                "omitted_work": ["official_announcements"],
                "terminal_status": "partial",
            },
            "subject": {"fund_code": "519755"},
            "sync_status": {"state": "partial"},
            "decision_evidence_status": {"state": "insufficient"},
        }

        with patch(
            "kunjin.brief.research.public_outcome_payload",
            return_value=projected,
        ) as projector:
            payload, exit_code, json_output = run(
                [
                    "--json",
                    "fund",
                    "brief",
                    "519755",
                    "--action",
                    "continue_holding",
                    "--mode",
                    "rapid",
                ],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.brief")
        self.assertEqual(payload["data"], projected)
        self.assertEqual(payload["data"]["request"]["terminal_status"], "partial")
        self.assertEqual(
            payload["data"]["request"]["omitted_work"],
            ["official_announcements"],
        )
        self.assertEqual(payload["data"]["sync_status"]["state"], "partial")
        self.assertEqual(
            payload["data"]["decision_evidence_status"]["state"],
            "insufficient",
        )
        self.assertEqual(
            service.calls,
            [("519755", ActionKind.CONTINUE_HOLDING, RequestMode.RAPID)],
        )
        projector.assert_called_once_with(report)

    def test_fund_brief_supports_all_owner_actions_and_switch(self) -> None:
        class BriefService:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def brief_outcome(inner_self, fund_code, *, action, mode):
                inner_self.calls.append((fund_code, action, mode))
                return object()

        service = BriefService()
        self.context.brief_service = service
        expected_actions = {
            "continue_holding": ["fact_research", "continue_holding"],
            "reduce_to_cash": ["fact_research", "reduce_to_cash"],
            "full_exit": ["fact_research", "full_exit"],
            "switch_funds": ["fact_research", "switch_reduce", "switch_buy"],
        }
        for action, action_ids in expected_actions.items():
            with (
                self.subTest(action=action),
                patch(
                    "kunjin.brief.research.public_outcome_payload",
                    return_value={
                        "request": {
                            "action_ids": action_ids,
                            "omitted_work": [],
                            "terminal_status": "complete",
                        }
                    },
                ),
            ):
                payload, exit_code, _ = run(
                    ["--json", "fund", "brief", "519755", "--action", action],
                    self.context,
                )
                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["data"]["request"]["action_ids"], action_ids)

        self.assertEqual(
            [item[1] for item in service.calls],
            [
                ActionKind.CONTINUE_HOLDING,
                ActionKind.REDUCE_TO_CASH,
                ActionKind.FULL_EXIT,
                ActionKind.SWITCH_FUNDS,
            ],
        )

    def test_fund_brief_deep_mode_is_explicitly_forwarded(self) -> None:
        class BriefService:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def brief_outcome(inner_self, fund_code, *, action, mode):
                inner_self.calls.append((fund_code, action, mode))
                return object()

        service = BriefService()
        self.context.brief_service = service
        with patch(
            "kunjin.brief.research.public_outcome_payload",
            return_value={
                "request": {
                    "action_ids": ["fact_research", "full_exit"],
                    "mode": "deep",
                    "omitted_work": [],
                    "terminal_status": "complete",
                }
            },
        ):
            payload, exit_code, _ = run(
                [
                    "--json",
                    "fund",
                    "brief",
                    "519755",
                    "--action",
                    "full_exit",
                    "--mode",
                    "deep",
                ],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["request"]["mode"], "deep")
        self.assertEqual(
            service.calls,
            [("519755", ActionKind.FULL_EXIT, RequestMode.DEEP)],
        )

    def test_fund_brief_rejects_non_json_invalid_code_action_and_mode(self) -> None:
        self.context.brief_service = SimpleNamespace(
            brief_outcome=lambda *_args, **_kwargs: object()
        )
        cases = (
            (
                ["fund", "brief", "519755", "--action", "continue_holding"],
                "invalid_arguments",
            ),
            (
                ["--json", "fund", "brief", "51975", "--action", "continue_holding"],
                "invalid_fund_code",
            ),
            (
                [
                    "--json",
                    "fund",
                    "brief",
                    "\uff11\uff12\uff13\uff14\uff15\uff16",
                    "--action",
                    "continue_holding",
                ],
                "invalid_fund_code",
            ),
            (
                ["--json", "fund", "brief", "519755", "--action", "buy_or_add"],
                "invalid_arguments",
            ),
            (
                [
                    "--json",
                    "fund",
                    "brief",
                    "519755",
                    "--action",
                    "continue_holding",
                    "--mode",
                    "overnight",
                ],
                "invalid_arguments",
            ),
        )
        for argv, expected_code in cases:
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "fund.brief")
                self.assertEqual(payload["errors"][0]["code"], expected_code)

    def test_fund_brief_technical_failure_is_nonzero_and_sanitized(self) -> None:
        private = (
            "access_token=never-print-this amount=918273645001 "
            f"managed_path={self.context.paths.database}"
        )

        class FailingBriefService:
            def brief_outcome(inner_self, *_args, **_kwargs):
                raise RuntimeError(private)

        self.context.brief_service = FailingBriefService()
        payload, exit_code, _ = run(
            [
                "--json",
                "fund",
                "brief",
                "519755",
                "--action",
                "continue_holding",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "fund.brief")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "fund_brief_failed",
                    "message": "held fund brief failed",
                }
            ],
        )
        rendered = json.dumps(payload)
        for value in ("never-print-this", "918273645001", str(self.context.paths.database)):
            self.assertNotIn(value, rendered)

        self.context.brief_service = SimpleNamespace(
            brief_outcome=lambda *_args, **_kwargs: object()
        )
        with patch(
            "kunjin.brief.research.public_outcome_payload",
            side_effect=ValueError(private),
        ):
            projection, projection_exit, _ = run(
                [
                    "--json",
                    "fund",
                    "brief",
                    "519755",
                    "--action",
                    "continue_holding",
                ],
                self.context,
            )
        self.assertEqual(projection_exit, 1)
        self.assertEqual(projection["errors"], payload["errors"])
        self.assertNotIn(private, json.dumps(projection))

    def test_fund_brief_system_exit_is_nonzero_json_and_sanitized(self) -> None:
        private = "access_token=never-print-this managed_path=/private/tmp/secret"
        for exit_value in (0, 2, private):
            with self.subTest(exit_value=exit_value):

                class ExitingBriefService:
                    def brief_outcome(inner_self, *_args, **_kwargs):
                        raise SystemExit(exit_value)

                self.context.brief_service = ExitingBriefService()
                payload, exit_code, json_output = run(
                    [
                        "--json",
                        "fund",
                        "brief",
                        "519755",
                        "--action",
                        "continue_holding",
                    ],
                    self.context,
                )

                self.assertTrue(json_output)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "fund.brief")
                self.assertEqual(
                    payload["errors"],
                    [
                        {
                            "code": "fund_brief_failed",
                            "message": "held fund brief failed",
                        }
                    ],
                )
                self.assertNotIn("never-print-this", json.dumps(payload))
                self.assertNotIn("/private/tmp/secret", json.dumps(payload))

    def test_fund_brief_keyboard_interrupt_is_not_disguised_as_json(self) -> None:
        class InterruptedBriefService:
            def brief_outcome(inner_self, *_args, **_kwargs):
                raise KeyboardInterrupt

        self.context.brief_service = InterruptedBriefService()
        with self.assertRaises(KeyboardInterrupt):
            run(
                [
                    "--json",
                    "fund",
                    "brief",
                    "519755",
                    "--action",
                    "continue_holding",
                ],
                self.context,
            )

    def test_fund_brief_has_no_private_or_expansive_options(self) -> None:
        self.context.brief_service = SimpleNamespace(
            brief_outcome=lambda *_args, **_kwargs: object()
        )
        forbidden = (
            "--amount",
            "--shares",
            "--date",
            "--url",
            "--path",
            "--token",
            "--adapter",
            "--docker",
            "--background",
        )
        for option in forbidden:
            private = "never-print-this"
            with self.subTest(option=option):
                payload, exit_code, _ = run(
                    [
                        "--json",
                        "fund",
                        "brief",
                        "519755",
                        "--action",
                        "continue_holding",
                        option,
                        private,
                    ],
                    self.context,
                )
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "fund.brief")
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")
                self.assertNotIn(private, json.dumps(payload))

    def test_fund_brief_valid_business_states_remain_successful(self) -> None:
        cases = (
            ("current_holding", "continue_holding", "complete", (), "complete", "partial", "watch"),
            (
                "auth_missing",
                "continue_holding",
                "partial",
                ("personal_position_observation",),
                "partial",
                "insufficient",
                "abstain",
            ),
            (
                "phase_b_blocked",
                "continue_holding",
                "complete",
                (),
                "complete",
                "partial",
                "no_add",
            ),
            (
                "partial_profile",
                "continue_holding",
                "partial",
                ("identity_profile",),
                "partial",
                "insufficient",
                "abstain",
            ),
            (
                "unsupported_holdings",
                "continue_holding",
                "partial",
                ("holdings_industries",),
                "partial",
                "insufficient",
                "watch",
            ),
            (
                "liquidation",
                "continue_holding",
                "complete",
                (),
                "complete",
                "partial",
                "reduce_or_exit_review",
            ),
            ("no_thesis", "continue_holding", "complete", (), "complete", "partial", "watch"),
            (
                "active_thesis_fail_closed",
                "continue_holding",
                "partial",
                ("thesis_review",),
                "complete",
                "insufficient",
                "watch",
            ),
            (
                "reduce",
                "reduce_to_cash",
                "complete",
                (),
                "complete",
                "partial",
                "reduce_or_exit_review",
            ),
            (
                "exit",
                "full_exit",
                "complete",
                (),
                "complete",
                "partial",
                "reduce_or_exit_review",
            ),
            (
                "switch",
                "switch_funds",
                "partial",
                ("switch_buy_evidence",),
                "partial",
                "insufficient",
                "abstain",
            ),
        )
        for (
            scenario,
            action,
            terminal_status,
            omitted_work,
            sync_state,
            decision_state,
            primary_state,
        ) in cases:
            with self.subTest(scenario=scenario):
                self.context.brief_service = SimpleNamespace(
                    brief_outcome=lambda *_args, **_kwargs: object()
                )
                projected = {
                    "request": {
                        "terminal_status": terminal_status,
                        "omitted_work": list(omitted_work),
                    },
                    "sync_status": {"state": sync_state},
                    "decision_evidence_status": {"state": decision_state},
                    "action_interpretation": {"primary_state": primary_state},
                }
                with patch(
                    "kunjin.brief.research.public_outcome_payload",
                    return_value=projected,
                ):
                    payload, exit_code, _ = run(
                        ["--json", "fund", "brief", "519755", "--action", action],
                        self.context,
                    )

                self.assertEqual(exit_code, 0)
                self.assertEqual(payload["errors"], [])
                self.assertEqual(payload["data"], projected)

    def test_fund_brief_missing_service_is_a_fixed_usage_error(self) -> None:
        self.context.brief_service = None

        payload, exit_code, _ = run(
            [
                "--json",
                "fund",
                "brief",
                "519755",
                "--action",
                "continue_holding",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "fund.brief")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "invalid_arguments",
                    "message": "held fund brief service is unavailable",
                }
            ],
        )

    def test_allocation_amount_free_boundary_rejects_malicious_service_values(self) -> None:
        sentinel = "private-goal-918273645001"

        @dataclass
        class PrivateDataclass:
            name: str = sentinel

        class PrivateObject:
            def __str__(self):
                return sentinel

        malicious = {
            "private_name": sentinel,
            "amount": Decimal("918273645001.25"),
            "nested": [PrivateDataclass(), {"custom": PrivateObject()}],
        }

        class MaliciousExecution:
            def safe_json(self):
                return malicious

            def local_view(self):
                return malicious

        class MaliciousAllocationService:
            def ranges(self):
                return MaliciousExecution()

            def status(self):
                return malicious

            def history(self):
                return (malicious,)

            def policy(self):
                return malicious

        self.context.allocation_service = MaliciousAllocationService()
        for action in ("ranges", "status", "history", "policy"):
            with self.subTest(action=action):
                payload, exit_code, _ = run(["--json", "allocation", action], self.context)

                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, f"allocation.{action}")
                self.assertEqual(
                    payload["errors"],
                    [
                        {
                            "code": "allocation_calculation_failed",
                            "message": "allocation calculation failed",
                        }
                    ],
                )
                rendered = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(sentinel, rendered)
                self.assertNotIn("918273645001", rendered)

    def test_legacy_phase_b_date_fingerprint_requires_reassessment_stably(self) -> None:
        profile = self.context.profile_service.confirm_profile(valid_profile())
        suitability, suitability_exit, _ = run(["--json", "suitability", "assess"], self.context)
        loaded = self.context.profile_service.load_active()
        legacy_payload = "|".join(
            (
                str(profile.id),
                loaded.encrypted_keyed_fingerprint,
                SuitabilityPolicyV1().checksum(),
                self.suitability_now.date().isoformat(),
            )
        ).encode("ascii")
        legacy_fingerprint = AssessmentCipher(self.profile_key_store).fingerprint(legacy_payload)
        with self.context.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute(
                "UPDATE suitability_assessments SET input_fingerprint = ? WHERE id = ?",
                (legacy_fingerprint, suitability["data"]["assessment_id"]),
            )

        payload, exit_code, _ = run(["--json", "allocation", "ranges"], self.context)

        self.assertEqual(suitability_exit, 0)
        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "allocation.ranges")
        self.assertEqual(payload["errors"][0]["code"], "allocation_calculation_failed")
        self.assertEqual(
            payload["errors"][0]["message"],
            "allocation calculation failed",
        )

    def test_portfolio_output_never_contains_token(self) -> None:
        payload, exit_code, _ = run(["--json", "portfolio", "show"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertNotIn("never-print-this", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["data"]["positions"][0]["fund_code"], "016067")

    def test_analysis_is_structured(self) -> None:
        payload, exit_code, _ = run(["--json", "portfolio", "analyze"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["analysis"]["evidence_level"], "deterministic_calculation")

    def test_fund_research_is_structured(self) -> None:
        payload, exit_code, _ = run(["--json", "fund", "research", "017811"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["profile"]["fund_code"], "017811")
        self.assertEqual(payload["data"]["analysis"]["evidence_level"], "deterministic_calculation")

    def test_fund_risk_financial_states_use_authenticated_reports_and_exit_zero(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service

        for status in ("partial", "stale", "conflicted", "unsupported", "unclassified"):
            with self.subTest(status=status):
                service.evidence_by_id[11] = SimpleNamespace(
                    report_status=status,
                    fingerprint="a" * 64,
                )
                with patch(
                    "kunjin.cli.build_authenticated_risk_research_report",
                    side_effect=fake_risk_report,
                ):
                    payload, exit_code, json_output = run(
                        ["--json", "fund", "classify", "519755"],
                        self.context,
                    )

                self.assertEqual(exit_code, 0)
                self.assertTrue(json_output)
                self.assert_envelope(payload, "fund.classify")
                self.assertEqual(payload["errors"], [])
                self.assertEqual(payload["data"]["capability"], "research_only")
                self.assertEqual(
                    payload["data"]["classification"]["input_fingerprint"],
                    "a" * 64,
                )
                self.assertIn(
                    payload["data"]["classification"]["reason_codes"][0],
                    {
                        "classification_partial",
                        "classification_stale",
                        "classification_conflicted",
                        "unsupported_product_family",
                        "classification_unclassified",
                    },
                )

        self.assertIn(("classification_evidence", "519755", 11), service.calls)

    def test_v1_history_and_v2_current_evidence_both_authenticate_through_cli(self) -> None:
        risk_store = FundRiskStore(self.context.repository)
        self.context.fund_risk_store = risk_store
        now = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)
        document_path = self.context.paths.fund_documents / "public-summary.html"
        document_path.parent.mkdir(parents=True, exist_ok=True)
        document_path.write_text(
            "<html><body><p>公开产品资料概要</p></body></html>",
            encoding="utf-8",
        )
        payload = document_path.read_bytes()
        candidate = OfficialDocumentCandidate(
            fund_code="519755",
            document_kind=DocumentKind.PRODUCT_SUMMARY,
            title="公开产品资料概要",
            url="https://www.fund001.com/public-summary.html",
            publisher="交银施罗德基金管理有限公司",
            published_at=now,
            source_tier=1,
        )
        artifact = RetrievedArtifact(
            candidate=candidate,
            final_url=candidate.url,
            retrieved_at=now,
            content_type="text/html; charset=utf-8",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            managed_path=document_path,
        )
        parsed = ParsedRiskDocument(artifact=artifact, facts=(), warnings=(), conflicts=())
        refresh_id = risk_store.begin_document_refresh("519755", now)
        risk_store.publish_document_selection(
            select_current_candidates(
                "519755",
                refresh_run_id=refresh_id,
                candidates=(candidate,),
            ),
            now,
        )
        risk_store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=artifact.sha256,
            attempted_at=now,
        )
        risk_store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            now,
        )
        self.context.fund_risk_service = FundRiskService(
            risk_store=risk_store,
            disclosure_store=self.context.fund_disclosure_store,
            repository=self.context.repository,
            discovery=object(),
            document_client=object(),
            clock=lambda: now,
        )

        historical, historical_exit, _ = run(
            ["--json", "fund", "classify", "519755"],
            self.context,
        )
        self.assertEqual(historical_exit, 0)
        historical_id = risk_store.classification_history("519755")[0].id
        bound = risk_store.classification_evidence("519755", historical_id)
        self.assertIsNotNone(bound)
        manifest_v1 = classification_input_manifest_v1(
            bound.evidence,
            ClassificationPolicyV1(),
            bound.classification.classified_at,
        )
        manifest_json = json.dumps(
            manifest_v1,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
        with self.context.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
            connection.execute(
                "UPDATE fund_risk_classifications "
                "SET input_manifest_json = ?, input_fingerprint = ? WHERE id = ?",
                (manifest_json, fingerprint, historical_id),
            )

        current_time = datetime(2026, 7, 13, 8, 1, tzinfo=timezone.utc)
        self.context.fund_risk_service = FundRiskService(
            risk_store=risk_store,
            disclosure_store=self.context.fund_disclosure_store,
            repository=self.context.repository,
            discovery=object(),
            document_client=object(),
            clock=lambda: current_time,
        )
        classified, classify_exit, _ = run(
            ["--json", "fund", "classify", "519755"],
            self.context,
        )
        current, current_exit, _ = run(
            ["--json", "fund", "classification-evidence", "519755"],
            self.context,
        )
        history, history_exit, _ = run(
            ["--json", "fund", "classification-history", "519755"],
            self.context,
        )

        self.assertEqual((classify_exit, current_exit, history_exit), (0, 0, 0))
        self.assertEqual(classified["errors"], [])
        self.assertEqual(classified["data"]["evidence_status"], "unclassified")
        self.assertEqual(
            classified["data"]["classification"]["input_fingerprint"],
            current["data"]["classification"]["input_fingerprint"],
        )
        self.assertEqual(len(history["data"]["classifications"]), 2)
        self.assertEqual(
            history["data"]["classifications"][1]["classification"]["input_fingerprint"],
            fingerprint,
        )
        self.assertEqual(
            history["data"]["classifications"][0]["classification"]["input_fingerprint"],
            classified["data"]["classification"]["input_fingerprint"],
        )

    def test_converter_status_json_exposes_only_safe_metadata(self) -> None:
        service = FakeRiskService()
        service.converter_status_result = ConverterStatus(
            capability="research_only",
            status="ready",
            reason_code=None,
            parser_version="4-docker-libreoffice-v1",
            provenance_checksum="e" * 64,
        )
        self.context.fund_risk_service = service

        payload, exit_code, _ = run(
            ["--json", "fund", "converter-status"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "fund.converter-status")
        self.assertEqual(
            payload["data"],
            {
                "capability": "research_only",
                "status": "ready",
                "reason_code": None,
                "parser_version": "4-docker-libreoffice-v1",
                "provenance_checksum": "e" * 64,
            },
        )
        rendered = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "sha256:",
            "container",
            "/Users/",
            "/private/",
            "argv",
            "package",
            "stderr",
            "<html",
        ):
            self.assertNotIn(forbidden, rendered.casefold())

    def test_sync_json_never_contains_converter_internal_fields(self) -> None:
        service = FakeRiskService()
        item = DocumentSyncItem(
            document_kind="annual_report",
            title="公开年度报告",
            url="https://www.fund001.com/report.doc",
            published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            status="failed",
            artifact_id=None,
            fact_count=0,
            warnings=(),
            conflicts=(),
            error_code="official_document_parse_failed",
            failure_stage="conversion",
            failure_reason="legacy_converter_failed",
        )
        object.__setattr__(item, "docker_path", "/Applications/Docker.app/private-cli")
        object.__setattr__(item, "argv", ("docker", "run", "private-container-name"))
        object.__setattr__(item, "stderr", "private daemon stderr")
        object.__setattr__(item, "normalized_html", "<html>private converted output</html>")
        service.sync_result = DocumentSyncResult(
            fund_code="519755",
            status="failed",
            documents=(item,),
            selections=document_selections(annual_url="https://www.fund001.com/report.doc"),
            selection_checksum="a" * 64,
            attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        self.context.fund_risk_service = service

        payload, exit_code, _ = run(
            ["--json", "sync", "fund-documents", "519755"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["data"], {})
        rendered = json.dumps(payload, ensure_ascii=False).casefold()
        for forbidden in (
            "/applications/docker.app",
            "private-container-name",
            "private daemon stderr",
            "private converted output",
            "sha256:" + "f" * 64,
        ):
            self.assertNotIn(forbidden, rendered)

    def test_fund_risk_current_and_evidence_are_authenticated_read_only_views(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service

        with patch(
            "kunjin.cli.build_authenticated_risk_research_report",
            side_effect=fake_risk_report,
        ) as builder:
            current, current_exit, _ = run(
                ["--json", "fund", "classification", "519755"],
                self.context,
            )
            evidence, evidence_exit, _ = run(
                ["--json", "fund", "classification-evidence", "519755"],
                self.context,
            )

        self.assertEqual(current_exit, 0)
        self.assertEqual(evidence_exit, 0)
        self.assert_envelope(current, "fund.classification")
        self.assert_envelope(evidence, "fund.classification-evidence")
        self.assertEqual(current["data"], evidence["data"])
        self.assertEqual(builder.call_count, 2)
        self.assertEqual(
            service.calls,
            [
                ("classification_evidence", "519755", None),
                ("classification_evidence", "519755", None),
            ],
        )

    def test_fund_risk_history_authenticates_every_record(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service

        with patch(
            "kunjin.cli.build_authenticated_risk_research_report",
            side_effect=fake_risk_report,
        ):
            payload, exit_code, _ = run(
                ["--json", "fund", "classification-history", "519755"],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "fund.classification-history")
        self.assertEqual(payload["data"]["fund_code"], "519755")
        self.assertEqual(
            [item["classification_id"] for item in payload["data"]["classifications"]],
            [11, 10],
        )
        self.assertEqual(
            service.calls,
            [
                ("classification_history", "519755"),
                ("classification_evidence", "519755", 11),
                ("classification_evidence", "519755", 10),
            ],
        )

    def test_fund_risk_missing_current_and_empty_history_are_financially_empty(self) -> None:
        service = FakeRiskService()
        service.history = ()
        service.evidence_by_id = {}
        self.context.fund_risk_service = service

        cases = (
            (["--json", "fund", "classification", "519755"], "fund.classification"),
            (
                ["--json", "fund", "classification-evidence", "519755"],
                "fund.classification-evidence",
            ),
            (
                ["--json", "fund", "classification-history", "519755"],
                "fund.classification-history",
            ),
        )
        for argv, command in cases:
            with self.subTest(command=command):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 0)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"], [])
                self.assertEqual(payload["data"]["capability"], "research_only")

    def test_fund_risk_document_sync_partial_and_empty_exit_zero(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        successful = DocumentSyncItem(
            document_kind="prospectus_update",
            title="official prospectus",
            url="https://www.fund001.com/prospectus.html",
            published_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            status="success",
            artifact_id=1,
            fact_count=3,
            warnings=(),
            conflicts=(),
            error_code=None,
        )
        failed = DocumentSyncItem(
            document_kind="annual_report",
            title="official annual report",
            url="https://www.fund001.com/report.html",
            published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            status="failed",
            artifact_id=None,
            fact_count=0,
            warnings=(),
            conflicts=(),
            error_code="official_document_parse_failed",
            failure_stage="parser",
            failure_reason="parser_format_invalid",
        )

        for status, documents in (("partial", (successful, failed)), ("empty", ())):
            with self.subTest(status=status):
                service.sync_result = DocumentSyncResult(
                    fund_code="519755",
                    status=status,
                    documents=documents,
                    selections=document_selections(
                        annual_url=None if status == "empty" else failed.url
                    ),
                    selection_checksum="a" * 64,
                    attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                    capability="research_only",
                )
                payload, exit_code, _ = run(
                    ["--json", "sync", "fund-documents", "519755"],
                    self.context,
                )
                self.assertEqual(exit_code, 0)
                self.assert_envelope(payload, "sync.fund-documents")
                self.assertEqual(payload["errors"], [])
                self.assertEqual(payload["data"]["status"], status)
                self.assertEqual(payload["data"]["selection_checksum"], "a" * 64)
                if status == "partial":
                    self.assertEqual(
                        payload["data"]["selections"],
                        [
                            {
                                "document_kind": "annual_report",
                                "status": "selected",
                                "selected_url": "https://www.fund001.com/report.html",
                                "candidate_count": 1,
                                "reason_code": None,
                            },
                            {
                                "document_kind": "quarterly_report",
                                "status": "missing",
                                "selected_url": None,
                                "candidate_count": 0,
                                "reason_code": "current_periodic_candidate_missing",
                            },
                            {
                                "document_kind": "semiannual_report",
                                "status": "missing",
                                "selected_url": None,
                                "candidate_count": 0,
                                "reason_code": "current_periodic_candidate_missing",
                            },
                        ],
                    )
                    self.assertEqual(
                        payload["data"]["documents"],
                        [
                            {
                                "document_kind": "prospectus_update",
                                "title": "official prospectus",
                                "url": "https://www.fund001.com/prospectus.html",
                                "published_at": "2026-07-13T00:00:00+00:00",
                                "status": "success",
                                "artifact_id": 1,
                                "fact_count": 3,
                                "warnings": [],
                                "conflicts": [],
                                "error_code": None,
                                "failure_stage": None,
                                "failure_reason": None,
                            },
                            {
                                "document_kind": "annual_report",
                                "title": "official annual report",
                                "url": "https://www.fund001.com/report.html",
                                "published_at": "2026-07-12T00:00:00+00:00",
                                "status": "failed",
                                "artifact_id": None,
                                "fact_count": 0,
                                "warnings": [],
                                "conflicts": [],
                                "error_code": "official_document_parse_failed",
                                "failure_stage": "parser",
                                "failure_reason": "parser_format_invalid",
                            },
                        ],
                    )
                    rendered = json.dumps(payload, ensure_ascii=False)
                    self.assertEqual(
                        set(re.findall(r"https?://[^\"\s]+", rendered)),
                        {
                            "https://www.fund001.com/prospectus.html",
                            "https://www.fund001.com/report.html",
                        },
                    )
                    for private_value in (
                        "private-exception-sentinel",
                        "/private/tmp/managed-document.doc",
                        "https://private.invalid/document?token=secret",
                        "raw response body sentinel",
                        "monthly_net_income",
                        "private_name",
                    ):
                        self.assertNotIn(private_value, rendered)
                else:
                    self.assertEqual(payload["data"]["documents"], [])
                    self.assertTrue(
                        all(
                            item["status"] == "missing" and item["selected_url"] is None
                            for item in payload["data"]["selections"]
                        )
                    )

    def test_fund_risk_document_sync_all_technical_failures_exit_nonzero(self) -> None:
        service = FakeRiskService()
        service.sync_result = DocumentSyncResult(
            fund_code="519755",
            status="failed",
            documents=(
                DocumentSyncItem(
                    document_kind="annual_report",
                    title="official annual report",
                    url="https://www.fund001.com/report.html",
                    published_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
                    status="failed",
                    artifact_id=None,
                    fact_count=0,
                    warnings=(),
                    conflicts=(),
                    error_code="official_document_parse_failed",
                    failure_stage="parser",
                    failure_reason="parser_format_invalid",
                ),
            ),
            selections=document_selections(),
            selection_checksum="a" * 64,
            attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            capability="research_only",
        )
        self.context.fund_risk_service = service

        payload, exit_code, _ = run(
            ["--json", "sync", "fund-documents", "519755"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "sync.fund-documents")
        self.assertEqual(
            payload["errors"],
            [
                {
                    "code": "official_document_parse_failed",
                    "message": "official fund document parsing failed",
                }
            ],
        )
        self.assertEqual(
            payload["data"]["documents"][0]["failure_stage"],
            "parser",
        )
        self.assertEqual(
            payload["data"]["documents"][0]["failure_reason"],
            "parser_format_invalid",
        )

    def test_fund_risk_document_sync_rejects_exact_dataclass_private_diagnostics(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        valid = DocumentSyncItem(
            document_kind="prospectus_update",
            title="official prospectus",
            url="https://www.fund001.com/prospectus.html",
            published_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            status="success",
            artifact_id=1,
            fact_count=3,
            warnings=(),
            conflicts=(),
            error_code=None,
        )
        unsafe_items = (
            replace(valid, title="/Applications/Docker.app/private-sentinel"),
            replace(valid, title="<html>private-sentinel</html>"),
            replace(valid, title="docker run private-sentinel"),
            replace(valid, url="https://www.fund001.com/#private-sentinel"),
            replace(valid, warnings=("stderr_private_sentinel",)),
            replace(valid, conflicts=("raw_diagnostic_private_sentinel",)),
        )

        for item in unsafe_items:
            with self.subTest(item=item):
                service.sync_result = DocumentSyncResult(
                    fund_code="519755",
                    status="success",
                    documents=(item,),
                    selections=document_selections(annual_url=None),
                    selection_checksum="a" * 64,
                    attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                )

                payload, exit_code, _ = run(
                    ["--json", "sync", "fund-documents", "519755"],
                    self.context,
                )

                self.assertEqual(exit_code, 1)
                self.assertNotIn("private-sentinel", json.dumps(payload, ensure_ascii=False))

    def test_fund_risk_public_payload_scan_rejects_decoded_private_strings(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        valid = DocumentSyncItem(
            document_kind="prospectus_update",
            title="公开基金招募说明书（更新）",
            url="https://www.fund001.com/prospectus.html",
            published_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
            status="success",
            artifact_id=1,
            fact_count=3,
            warnings=(),
            conflicts=(),
            error_code=None,
        )
        unsafe_items = (
            replace(valid, title="公开报告 stderr_private_diagnostic-private-sentinel"),
            replace(
                valid,
                url=("https://www.fund001.com/report?path=%2Fprivate%2Ftmp%2Fprivate-sentinel.doc"),
            ),
            replace(
                valid,
                url=(
                    "https://www.fund001.com/report?payload=%3Chtml%3Eprivate-sentinel%3C%2Fhtml%3E"
                ),
            ),
            replace(
                valid,
                url=(
                    "https://www.fund001.com/report?"
                    "path=%252Fprivate%252Ftmp%252Fprivate-sentinel.doc"
                ),
            ),
            replace(
                valid,
                url=(
                    "https://www.fund001.com/report?"
                    "path=%252FUsers%252Fowner%252Fprivate-sentinel.doc"
                ),
            ),
            replace(
                valid,
                url=(
                    "https://www.fund001.com/report?"
                    "payload=%253Chtml%253Eprivate-sentinel%253C%252Fhtml%253E"
                ),
            ),
            replace(
                valid,
                url=("https://www.fund001.com/report?diagnostic=%2573tderr_private-sentinel"),
            ),
            replace(
                valid,
                url=("https://www.fund001.com/report?diagnostic=%2573tdout_private-sentinel"),
            ),
            replace(
                valid,
                url=("https://www.fund001.com/report?diagnostic=%2574raceback_private-sentinel"),
            ),
        )

        for item in unsafe_items:
            with self.subTest(item=item):
                service.sync_result = DocumentSyncResult(
                    fund_code="519755",
                    status="success",
                    documents=(item,),
                    selections=document_selections(annual_url=None),
                    selection_checksum="a" * 64,
                    attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
                )
                with (
                    patch.object(DocumentSyncResult, "validate"),
                    patch.object(
                        DocumentSyncItem,
                        "validate",
                    ),
                ):
                    payload, exit_code, _ = run(
                        ["--json", "sync", "fund-documents", "519755"],
                        self.context,
                    )

                self.assertEqual(exit_code, 1)
                self.assertNotIn("private-sentinel", json.dumps(payload, ensure_ascii=False))
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("classification_unclassified", rendered)
        self.assertNotIn("private-exception-sentinel", rendered)
        self.assertNotIn("/private/tmp/managed-document.doc", rendered)
        self.assertNotIn("raw response body sentinel", rendered)

    def test_fund_risk_selection_json_uses_an_explicit_public_allowlist(self) -> None:
        service = FakeRiskService()
        selections = document_selections(annual_url=None)
        object.__setattr__(selections[0], "candidate_fingerprints", ("b" * 64,))
        object.__setattr__(
            selections[0],
            "unselected_urls",
            ("https://private.invalid/report?token=private-selection-sentinel",),
        )
        object.__setattr__(
            selections[0],
            "canonical_json",
            "private-selection-manifest-sentinel",
        )
        result = DocumentSyncResult(
            fund_code="519755",
            status="empty",
            documents=(),
            selections=selections,
            selection_checksum="a" * 64,
            attempted_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        )
        object.__setattr__(
            result,
            "raw_selection_json",
            '{"managed_path":"/private/tmp/private-selection.doc"}',
        )
        object.__setattr__(result, "candidate_fingerprints", ("c" * 64,))
        object.__setattr__(
            result,
            "unselected_urls",
            ("https://private.invalid/unselected?token=private-result-sentinel",),
        )
        object.__setattr__(result, "managed_path", "/private/tmp/private-selection.doc")
        object.__setattr__(result, "normalized_html", "<html>private-result-sentinel</html>")
        object.__setattr__(result, "database_path", str(self.context.paths.database))
        object.__setattr__(result, "exception_text", "traceback private-result-sentinel")
        service.sync_result = result
        self.context.fund_risk_service = service

        with (
            patch.object(DocumentSyncResult, "validate"),
            patch.object(DocumentSelectionItem, "validate"),
        ):
            payload, exit_code, _ = run(
                ["--json", "sync", "fund-documents", "519755"],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("candidate_fingerprints", rendered)
        self.assertNotIn("unselected_urls", rendered)
        self.assertNotIn("canonical_json", rendered)
        self.assertNotIn("private-selection-sentinel", rendered)
        self.assertNotIn("private-selection-manifest-sentinel", rendered)
        self.assertNotIn("raw_selection_json", rendered)
        self.assertNotIn("private-result-sentinel", rendered)
        self.assertNotIn("/private/tmp/private-selection.doc", rendered)
        self.assertNotIn(str(self.context.paths.database), rendered)
        self.assertNotIn("<html>", rendered)
        self.assertNotIn("traceback", rendered.casefold())

    def test_fund_risk_technical_errors_use_only_public_messages(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        cases = {
            "official_document_unavailable": "official fund document is unavailable",
            "official_document_invalid": "official fund document is invalid",
            "official_document_resource_limit": ("official fund document exceeded resource limits"),
            "official_document_parse_failed": ("official fund document parsing failed"),
            "classification_policy_unavailable": "classification policy is unavailable",
            "classification_calculation_failed": ("fund classification calculation failed"),
            "classification_storage_failed": "fund classification storage failed",
        }
        for code, public_message in cases.items():
            with self.subTest(code=code):
                service.error = RiskServiceError(code, reason="public_reason")
                payload, exit_code, _ = run(
                    ["--json", "fund", "classify", "519755"],
                    self.context,
                )
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "fund.classify")
                self.assertEqual(payload["errors"][0]["code"], code)
                self.assertEqual(
                    payload["errors"][0]["message"],
                    f"{public_message}: public_reason",
                )
                self.assertNotIn("Traceback", json.dumps(payload))
        service.error = None

    def test_fund_risk_policy_is_fixed_public_and_amount_free(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "fund", "classification-policy"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "fund.classification-policy")
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["data"]["capability"], "research_only")
        self.assertEqual(payload["data"]["policy"]["version"], "1")
        self.assertEqual(len(payload["data"]["policy_checksum"]), 64)
        self.assertEqual(
            payload["data"]["policy"]["effective_at"],
            "2026-07-13T00:00:00+00:00",
        )
        rendered = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("monthly_net_income", "target_amount", "private_name"):
            self.assertNotIn(forbidden, rendered)

    def test_fund_risk_commands_reuse_six_digit_code_validation(self) -> None:
        self.context.fund_risk_service = FakeRiskService()
        for argv in (
            ["--json", "sync", "fund-documents", "123"],
            ["--json", "fund", "classify", "abc123"],
            ["--json", "fund", "classification", "123"],
            ["--json", "fund", "classification-history", "123"],
            ["--json", "fund", "classification-evidence", "123"],
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["errors"][0]["code"], "invalid_fund_code")

        for argv, command in (
            (["--json", "sync", "fund-documents"], "sync.fund-documents"),
            (["--json", "fund", "classify"], "fund.classify"),
            (["--json", "fund", "classification"], "fund.classification"),
            (
                ["--json", "fund", "classification-history"],
                "fund.classification-history",
            ),
            (
                ["--json", "fund", "classification-evidence"],
                "fund.classification-evidence",
            ),
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_fund_risk_output_contains_no_personal_or_local_document_fields(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        with patch(
            "kunjin.cli.build_authenticated_risk_research_report",
            side_effect=fake_risk_report,
        ):
            payload, exit_code, _ = run(
                ["--json", "fund", "classification-evidence", "519755"],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertIn("https://www.fund001.com/public.pdf", rendered)
        self.assertIn("official public title", rendered)
        self.assertIn("a" * 64, rendered)
        for forbidden in (
            "managed_path",
            str(self.context.paths.database),
            "/Users/",
            "raw_body",
            "response_body",
            "parser_exception",
            "monthly_net_income",
            "target_amount",
            "学习账户",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_fund_risk_report_rejects_direction_amount_target_and_score_keys(self) -> None:
        service = FakeRiskService()
        self.context.fund_risk_service = service
        forbidden_keys = (
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
        )

        for index, key in enumerate(forbidden_keys):
            sentinel = f"forbidden-d1-decision-{index}"

            def malicious_report(record, key=key, sentinel=sentinel):
                return {**fake_risk_report(record), key: sentinel}

            with (
                self.subTest(key=key),
                patch(
                    "kunjin.cli.build_authenticated_risk_research_report",
                    side_effect=malicious_report,
                ),
            ):
                payload, exit_code, _ = run(
                    ["--json", "fund", "classification-evidence", "519755"],
                    self.context,
                )

            self.assertEqual(exit_code, 1)
            self.assert_envelope(payload, "fund.classification-evidence")
            self.assertEqual(
                payload["errors"],
                [
                    {
                        "code": "classification_storage_failed",
                        "message": "fund classification storage failed",
                    }
                ],
            )
            self.assertNotIn(sentinel, json.dumps(payload, ensure_ascii=False))

    def test_market_sectors_state_scope(self) -> None:
        payload, exit_code, _ = run(["--json", "market", "sectors"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["scope"], "recent_strength_and_breadth_only")

    def test_thesis_add_and_review(self) -> None:
        payload, exit_code, _ = run(
            [
                "--json",
                "thesis",
                "add",
                "017811",
                "--reason",
                "AI盈利改善",
                "--horizon",
                "12个月",
                "--invalidation",
                "持续落后且风格漂移",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["thesis"]["fund_code"], "017811")

        review, exit_code, _ = run(["--json", "thesis", "review", "017811"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(review["data"]["theses"]), 1)

    def test_weekly_report_preserves_missing_evidence_warning(self) -> None:
        payload, exit_code, _ = run(["--json", "report", "weekly"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertTrue(any("news" in warning for warning in payload["warnings"]))

    def test_daily_sync_combines_sources(self) -> None:
        class PortfolioService:
            def sync_portfolio(inner_self, trigger):
                return SyncResult(1, 1, 1, datetime.now(timezone.utc))

        class ResearchService:
            def sync_fund(inner_self, fund_code):
                return ResearchSyncResult(fund_code, 10)

            def sync_market(inner_self):
                return ResearchSyncResult("market_sectors", 2)

        self.context.sync_service = PortfolioService()
        self.context.research_service = ResearchService()
        payload, exit_code, _ = run(["--json", "sync", "daily"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertIn("016067", payload["data"]["funds"])
        self.assertEqual(payload["data"]["market"]["observations"], 2)

    def test_fund_disclosure_commands_have_stable_contracts(self) -> None:
        bundle = empty_disclosure_bundle()
        self.context.fund_disclosure_store = FakeDisclosureStore(bundle)
        self.context.fund_disclosure_service = FakeDisclosureService(
            profile=disclosure_sync_result(),
            holdings=disclosure_sync_result(profile=False),
        )

        cases = [
            (["--json", "sync", "fund-profile", "519755"], "sync.fund-profile"),
            (["--json", "sync", "fund-holdings", "519755"], "sync.fund-holdings"),
            (["--json", "fund", "profile", "519755"], "fund.profile"),
            (["--json", "fund", "fees", "519755"], "fund.fees"),
            (["--json", "fund", "holdings", "519755"], "fund.holdings"),
            (["--json", "fund", "announcements", "519755"], "fund.announcements"),
        ]
        for argv, command in cases:
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 0)
                self.assert_envelope(payload, command)
                for key in ("sources", "freshness", "warnings", "conflicts", "errors"):
                    self.assertIn(key, payload["data"])

    def test_peer_commands_have_stable_contracts_and_do_not_leak_tokens(self) -> None:
        result = PeerSyncResult(
            "519755",
            PeerSyncState.PARTIAL,
            3,
            2,
            1,
            0,
            ("candidate_discovery_unavailable",),
            ({"code": "candidate_failed", "message": "000001"},),
        )
        service = FakePeerService(result)
        store = FakePeerStore(peer_group("519755", "000001"))
        self.context.peer_service = service
        self.context.peer_store = store
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())

        cases = (
            (
                [
                    "--json",
                    "sync",
                    "fund-peers",
                    "519755",
                    "--candidate",
                    "000001",
                    "--candidate",
                    "000002",
                ],
                "sync.fund-peers",
                0,
            ),
            (["--json", "fund", "peers", "519755"], "fund.peers", 0),
            (
                ["--json", "fund", "compare", "519755", "000001"],
                "fund.compare",
                0,
            ),
            (["--json", "portfolio", "overlap"], "portfolio.overlap", 1),
        )
        reports = {}
        for argv, command, expected_exit_code in cases:
            with self.subTest(command=command):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, expected_exit_code)
                self.assert_envelope(payload, command)
                self.assertNotIn("never-print-this", json.dumps(payload, ensure_ascii=False))
                reports[command] = payload

        self.assertEqual(reports["fund.peers"]["data"]["status"], "partial")
        self.assertEqual(reports["fund.compare"]["data"]["status"], "partial")
        self.assertEqual(
            reports["portfolio.overlap"]["data"]["status"],
            "insufficient_data",
        )
        self.assertEqual(
            reports["portfolio.overlap"]["errors"][0]["code"],
            "insufficient_data",
        )

        self.assertEqual(service.calls, [("519755", ("000001", "000002"))])
        self.assertEqual(len(store.saved), 3)

        first_compare, exit_code, _ = run(
            ["--json", "fund", "compare", "519755", "000001"], self.context
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(first_compare["data"]["comparison_run_id"], 7)
        explicit_calls = [call for call in store.saved if call[0][0] == "explicit"]
        self.assertEqual(explicit_calls[0][0][5], explicit_calls[1][0][5])

    def test_portfolio_diagnose_cli_is_additive_and_non_authorizing(self) -> None:
        parsed = build_parser().parse_args(
            ["--json", "portfolio", "diagnose", "--candidate", "000001"]
        )
        self.assertEqual(parsed.portfolio_command, "diagnose")
        self.assertEqual(parsed.candidate, "000001")

        payload, exit_code, _ = run(
            ["--json", "portfolio", "diagnose"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "portfolio.diagnose")
        self.assertFalse(payload["data"]["action_boundary"]["action_authorized"])
        self.assertFalse(payload["data"]["action_boundary"]["exact_amount_available"])
        self.assertEqual(payload["errors"][0]["code"], "insufficient_data")

    def test_fund_shortlist_parser_and_success_payload_are_bounded(self) -> None:
        parsed = build_parser().parse_args(
            ["--json", "fund", "shortlist", "000002", "000001"]
        )
        self.assertEqual(parsed.fund_command, "shortlist")
        self.assertEqual(parsed.fund_codes, ["000002", "000001"])

        service = FakeShortlistService(shortlist_result_fixture())
        self.context.selection_service = service
        payload, exit_code, _ = run(
            ["--json", "fund", "shortlist", "000002", "000001"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "fund.shortlist")
        self.assertEqual(service.calls, [("000002", "000001")])
        self.assertEqual(payload["data"]["comparison_state"], "conditional_shortlist")
        self.assertFalse(payload["data"]["conditional_shortlist"]["merit_ordered"])
        self.assertFalse(payload["data"]["action_boundary"]["action_authorized"])

    def test_fund_shortlist_insufficient_data_is_a_structured_error(self) -> None:
        source = shortlist_result_fixture()
        result = replace(
            source,
            comparison_state="insufficient_data",
            candidate_reviews=tuple(
                replace(item, evidence_state="insufficient_data")
                for item in source.candidate_reviews
            ),
            shortlist_codes=(),
            missing_evidence=("formal_nav_unavailable",),
        )
        result.validate()
        service = FakeShortlistService(result)
        self.context.selection_service = service

        payload, exit_code, _ = run(
            ["--json", "fund", "shortlist", "000002", "000001"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "fund.shortlist")
        self.assertEqual(payload["errors"], [
            {
                "code": "insufficient_data",
                "message": "Candidate shortlist has insufficient authenticated evidence",
            }
        ])
        self.assertEqual(payload["data"]["missing_evidence"], ["formal_nav_unavailable"])

    def test_fund_shortlist_validates_before_service_work(self) -> None:
        service = FakeShortlistService(shortlist_result_fixture())
        self.context.selection_service = service
        invalid_commands = (
            ["--json", "fund", "shortlist", "000001"],
            [
                "--json",
                "fund",
                "shortlist",
                "000001",
                "000002",
                "000003",
                "000004",
                "000005",
                "000006",
            ],
            ["--json", "fund", "shortlist", "000001", "000001"],
            ["--json", "fund", "shortlist", "000000", "000001"],
            ["--json", "fund", "shortlist", "000001", "abc123"],
        )

        for argv in invalid_commands:
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, "fund.shortlist")
                self.assertIn(
                    payload["errors"][0]["code"],
                    {"invalid_arguments", "invalid_fund_code"},
                )
        self.assertEqual(service.calls, [])

        accepted = ["000001", "000002", "000003", "000004", "000005"]
        parsed = build_parser().parse_args(["--json", "fund", "shortlist", *accepted])
        self.assertEqual(parsed.fund_codes, accepted)

    def test_fund_shortlist_unavailable_and_legacy_dispatch_are_isolated(self) -> None:
        self.context.selection_service = None
        unavailable, exit_code, _ = run(
            ["--json", "fund", "shortlist", "000001", "000002"],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assert_envelope(unavailable, "fund.shortlist")
        self.assertEqual(unavailable["errors"][0]["code"], "invalid_arguments")

        class MustNotRun:
            def review(self, fund_codes):
                raise AssertionError("legacy commands must not invoke shortlist")

        self.context.selection_service = MustNotRun()
        diagnosis, diagnosis_exit, _ = run(
            ["--json", "portfolio", "diagnose"], self.context
        )
        self.assertEqual(diagnosis_exit, 1)
        self.assert_envelope(diagnosis, "portfolio.diagnose")
        self.assertNotIn("conditional_shortlist", diagnosis["data"])

        self.context.peer_store = FakePeerStore(peer_group("519755", "000001"))
        self.context.fund_disclosure_store = FakeDisclosureStore(
            empty_disclosure_bundle()
        )
        comparison, comparison_exit, _ = run(
            ["--json", "fund", "compare", "519755", "000001"], self.context
        )
        self.assertEqual(comparison_exit, 0)
        self.assert_envelope(comparison, "fund.compare")
        self.assertNotIn("conditional_shortlist", comparison["data"])

    def test_research_scope_parser_accepts_only_closed_values(self) -> None:
        args = build_parser().parse_args([
            "--json",
            "fund",
            "research-scope",
            "--objective",
            "learning",
            "--horizon",
            "long_term",
            "--product-category",
            "broad_index",
        ])
        self.assertEqual(args.fund_command, "research-scope")
        self.assertEqual(args.objective, "learning")
        self.assertEqual(args.horizon, "long_term")
        self.assertEqual(args.product_category, "broad_index")

        payload, exit_code, _ = run(
            [
                "--json",
                "fund",
                "research-scope",
                "--objective",
                "unknown",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "fund.research-scope")
        self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_research_scope_success_and_missing_choices_are_public(self) -> None:
        service = FakeResearchScopeService()
        self.context.research_scope_service = service

        payload, exit_code, json_output = run(
            [
                "--json",
                "fund",
                "research-scope",
                "--objective",
                "learning",
                "--horizon",
                "long_term",
                "--product-category",
                "broad_index",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.research-scope")
        self.assertEqual(payload["data"]["request"]["objective"], "learning")
        self.assertEqual(
            payload["data"]["candidate_formation"],
            {
                "status": "research_scope_only",
                "candidate_code_discovery": "not_implemented",
            },
        )

        missing, missing_exit, _ = run(
            ["--json", "fund", "research-scope"], self.context
        )
        self.assertEqual(missing_exit, 0)
        self.assertEqual(
            missing["data"]["missing_inputs"],
            [
                "objective_required",
                "horizon_required",
                "product_category_required",
            ],
        )
        self.assertEqual(len(missing["data"]["product_category_context"]["choices"]), 6)

    def test_research_scope_blocked_gate_does_not_erase_fact_scope(self) -> None:
        service = FakeResearchScopeService(blocked=True)
        self.context.research_scope_service = service

        payload, exit_code, _ = run(
            [
                "--json",
                "fund",
                "research-scope",
                "--objective",
                "learning",
                "--horizon",
                "short_term",
                "--product-category",
                "pure_bond",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["research_scope"]["product_category"], "pure_bond")
        self.assertFalse(
            payload["data"]["research_scope"]["risk_increase_conclusion_allowed"]
        )
        self.assertIn(
            "emergency_reserve_shortfall",
            payload["data"]["personal_gate"]["blocking_codes"],
        )

    def test_shortlist_readiness_parser_and_success_preserve_order(self) -> None:
        args = build_parser().parse_args(
            ["--json", "fund", "shortlist-readiness", "000002", "000001"]
        )
        self.assertEqual(args.fund_command, "shortlist-readiness")
        self.assertEqual(args.fund_codes, ["000002", "000001"])

        service = FakeShortlistReadinessService(readiness_result_fixture())
        self.context.shortlist_readiness_service = service
        payload, exit_code, json_output = run(
            ["--json", "fund", "shortlist-readiness", "000002", "000001"],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(json_output)
        self.assert_envelope(payload, "fund.shortlist-readiness")
        self.assertEqual(service.calls, [("000002", "000001")])
        self.assertTrue(payload["data"]["comparison_evidence_ready"])
        self.assertEqual(
            payload["data"]["request"]["candidate_codes"],
            ["000002", "000001"],
        )

    def test_shortlist_readiness_insufficient_data_keeps_full_payload(self) -> None:
        service = FakeShortlistReadinessService(readiness_result_fixture(ready=False))
        self.context.shortlist_readiness_service = service
        payload, exit_code, _ = run(
            ["--json", "fund", "shortlist-readiness", "000002", "000001"],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "fund.shortlist-readiness")
        self.assertEqual(payload["errors"], [
            {
                "code": "insufficient_data",
                "message": "Candidate comparison evidence is not ready",
            }
        ])
        self.assertFalse(payload["data"]["comparison_evidence_ready"])
        self.assertEqual(payload["data"]["blocking_codes"], ["formal_nav_missing"])
        self.assertEqual(
            payload["data"]["bounded_refresh_actions"],
            [{"fund_code": "000002", "command": "sync fund 000002"}],
        )

    def test_owner_readiness_commands_validate_mode_and_codes_before_services(self) -> None:
        scope = FakeResearchScopeService()
        readiness = FakeShortlistReadinessService(readiness_result_fixture())
        self.context.research_scope_service = scope
        self.context.shortlist_readiness_service = readiness
        invalid_commands = (
            ["fund", "research-scope"],
            ["fund", "shortlist-readiness", "000002", "000001"],
            ["--json", "fund", "shortlist-readiness", "000001"],
            ["--json", "fund", "shortlist-readiness", "000001", "000001"],
            ["--json", "fund", "shortlist-readiness", "000000", "000001"],
            ["--json", "fund", "shortlist-readiness", "000001", "abc123"],
        )
        for argv in invalid_commands:
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertIn(
                    payload["errors"][0]["code"],
                    {"invalid_arguments", "invalid_fund_code"},
                )
        self.assertEqual(scope.calls, [])
        self.assertEqual(readiness.calls, [])

    def test_owner_readiness_static_failures_do_not_build_or_write_context(self) -> None:
        cases = (
            (
                ["fund", "research-scope"],
                "fund.research-scope",
                "invalid_arguments",
                "fund research-scope requires JSON mode",
            ),
            (
                ["fund", "shortlist-readiness", "000002", "000001"],
                "fund.shortlist-readiness",
                "invalid_arguments",
                "fund shortlist-readiness requires JSON mode",
            ),
            (
                ["--json", "fund", "shortlist-readiness", "000001"],
                "fund.shortlist-readiness",
                "invalid_arguments",
                "fund shortlist requires 2 to 5 unique fund codes",
            ),
            (
                ["--json", "fund", "shortlist-readiness", "000001", "000001"],
                "fund.shortlist-readiness",
                "invalid_arguments",
                "fund shortlist requires 2 to 5 unique fund codes",
            ),
            (
                ["--json", "fund", "research-scope", "--objective", "unknown"],
                "fund.research-scope",
                "invalid_arguments",
                None,
            ),
        )
        for argv, command, error_code, error_message in cases:
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                data_dir = root / "data"
                state_dir = root / "state"
                environment = {
                    "KUNJIN_DATA_DIR": str(data_dir),
                    "KUNJIN_STATE_DIR": str(state_dir),
                }
                with (
                    patch.dict(os.environ, environment),
                    patch("kunjin.cli.build_context") as context_builder,
                ):
                    payload, exit_code, _ = run(argv)

                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"][0]["code"], error_code)
                if error_message is not None:
                    self.assertEqual(payload["errors"][0]["message"], error_message)
                context_builder.assert_not_called()
                self.assertFalse(data_dir.exists())
                self.assertFalse(state_dir.exists())
                for suffix in ("", "-wal", "-shm"):
                    self.assertFalse((data_dir / f"kunjin.db{suffix}").exists())

    def test_valid_owner_readiness_command_still_builds_context_once(self) -> None:
        context = self.context
        context.research_scope_service = FakeResearchScopeService()
        with patch("kunjin.cli.build_context", return_value=context) as context_builder:
            payload, exit_code, _ = run(["--json", "fund", "research-scope"])

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "fund.research-scope")
        context_builder.assert_called_once_with(public_acceptance_subject=None)

    def test_owner_readiness_unavailable_and_technical_errors_are_sanitized(self) -> None:
        self.context.research_scope_service = None
        self.context.shortlist_readiness_service = None
        cases = (
            (["--json", "fund", "research-scope"], "fund.research-scope"),
            (
                ["--json", "fund", "shortlist-readiness", "000002", "000001"],
                "fund.shortlist-readiness",
            ),
        )
        for argv, command in cases:
            with self.subTest(command=command):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

        class TechnicalFailure:
            def form(self, **_kwargs):
                raise RuntimeError("private-owner-secret")

            def review(self, _codes):
                raise RuntimeError("private-owner-secret")

        failure = TechnicalFailure()
        self.context.research_scope_service = failure
        self.context.shortlist_readiness_service = failure
        technical_cases = (
            (
                ["--json", "fund", "research-scope"],
                "fund_research_scope_failed",
            ),
            (
                ["--json", "fund", "shortlist-readiness", "000002", "000001"],
                "fund_shortlist_readiness_failed",
            ),
        )
        for argv, code in technical_cases:
            with self.subTest(code=code):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["errors"][0]["code"], code)
                self.assertNotIn("private-owner-secret", json.dumps(payload))

    def test_owner_readiness_preserves_process_interrupts(self) -> None:
        class InterruptingService:
            def form(self, **_kwargs):
                raise SystemExit(19)

            def review(self, _codes):
                raise KeyboardInterrupt()

        service = InterruptingService()
        self.context.research_scope_service = service
        self.context.shortlist_readiness_service = service
        with self.assertRaisesRegex(SystemExit, "19"):
            run(["--json", "fund", "research-scope"], self.context)
        with self.assertRaises(KeyboardInterrupt):
            run(
                ["--json", "fund", "shortlist-readiness", "000002", "000001"],
                self.context,
            )

    def test_owner_readiness_dispatch_is_isolated_from_legacy_services(self) -> None:
        class MustNotRun:
            def review(self, _codes):
                raise AssertionError("legacy shortlist service must not run")

        self.context.selection_service = MustNotRun()
        self.context.research_scope_service = FakeResearchScopeService()
        self.context.shortlist_readiness_service = FakeShortlistReadinessService(
            readiness_result_fixture()
        )
        scope, scope_exit, _ = run(
            ["--json", "fund", "research-scope"], self.context
        )
        readiness, readiness_exit, _ = run(
            ["--json", "fund", "shortlist-readiness", "000002", "000001"],
            self.context,
        )
        self.assertEqual(scope_exit, 0)
        self.assertEqual(readiness_exit, 0)
        self.assert_envelope(scope, "fund.research-scope")
        self.assert_envelope(readiness, "fund.shortlist-readiness")

        class NewServicesMustNotRun:
            def form(self, **_kwargs):
                raise AssertionError("legacy command invoked research scope")

            def review(self, _codes):
                raise AssertionError("legacy command invoked readiness")

        self.context.research_scope_service = NewServicesMustNotRun()
        self.context.shortlist_readiness_service = NewServicesMustNotRun()
        source, source_exit, _ = run(
            ["--json", "source", "status", "--fund-code", "000001"],
            self.context,
        )
        self.assertEqual(source_exit, 0)
        self.assert_envelope(source, "source.status")

        self.context.selection_service = FakeShortlistService(shortlist_result_fixture())
        shortlist, shortlist_exit, _ = run(
            ["--json", "fund", "shortlist", "000002", "000001"],
            self.context,
        )
        self.assertEqual(shortlist_exit, 0)
        self.assert_envelope(shortlist, "fund.shortlist")

        diagnosis, diagnosis_exit, _ = run(
            ["--json", "portfolio", "diagnose"], self.context
        )
        self.assertEqual(diagnosis_exit, 1)
        self.assert_envelope(diagnosis, "portfolio.diagnose")

        self.context.peer_store = FakePeerStore(peer_group("519755", "000001"))
        self.context.fund_disclosure_store = FakeDisclosureStore(
            empty_disclosure_bundle()
        )
        comparison, comparison_exit, _ = run(
            ["--json", "fund", "compare", "519755", "000001"], self.context
        )
        self.assertEqual(comparison_exit, 0)
        self.assert_envelope(comparison, "fund.compare")

        suitability, suitability_exit, _ = run(
            ["--json", "suitability", "status"], self.context
        )
        self.assertEqual(suitability_exit, 0)
        self.assert_envelope(suitability, "suitability.status")
        allocation, allocation_exit, _ = run(
            ["--json", "allocation", "status"], self.context
        )
        self.assertEqual(allocation_exit, 0)
        self.assert_envelope(allocation, "allocation.status")

    def test_peer_sync_partial_success_keeps_member_errors_in_data(self) -> None:
        result = PeerSyncResult(
            "519755",
            PeerSyncState.PARTIAL,
            3,
            2,
            1,
            1,
            (),
            ({"code": "candidate_sync_failed", "message": "000001"},),
        )
        self.context.peer_service = FakePeerService(result)

        payload, exit_code, _ = run(["--json", "sync", "fund-peers", "519755"], self.context)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["data"]["errors"][0]["code"], "candidate_sync_failed")

    def test_peer_group_too_small_exits_one_and_missing_group_is_insufficient(self) -> None:
        self.context.peer_store = FakePeerStore(peer_group("519755"))
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())

        payload, exit_code, _ = run(["--json", "fund", "peers", "519755"], self.context)
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errors"][0]["code"], "peer_group_too_small")

        self.context.peer_store = FakePeerStore()
        missing, exit_code, _ = run(["--json", "fund", "peers", "519755"], self.context)
        self.assertEqual(exit_code, 1)
        self.assertEqual(missing["data"]["status"], "insufficient_data")
        self.assertEqual(missing["errors"][0]["code"], "insufficient_data")

    def test_peer_read_commands_validate_inputs_without_syncing(self) -> None:
        class SyncMustNotRun:
            def sync_peers(self, fund_code, user_candidates=()):
                raise AssertionError("read commands must not synchronize")

        self.context.peer_service = SyncMustNotRun()
        self.context.peer_store = FakePeerStore(peer_group("519755", "000001"))
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())

        for argv in (
            ["--json", "sync", "fund-peers", "123"],
            ["--json", "sync", "fund-peers", "519755", "--candidate", "abc"],
            ["--json", "fund", "peers", "123"],
            ["--json", "fund", "compare", "519755"],
            ["--json", "fund", "compare", "519755", "519755"],
            ["--json", "fund", "compare", "519755", "abc123"],
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertIn(
                    payload["errors"][0]["code"],
                    {"invalid_fund_code", "invalid_arguments"},
                )

        for argv, command in (
            (["--json", "sync", "fund-peers"], "sync.fund-peers"),
            (["--json", "fund", "compare"], "fund.compare"),
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def test_disclosure_sync_partial_success_exits_zero_and_total_failure_exits_one(self) -> None:
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())
        service = FakeDisclosureService(
            profile=disclosure_sync_result(),
            holdings=disclosure_sync_result(profile=False, total_failure=True),
        )
        self.context.fund_disclosure_service = service

        partial, exit_code, _ = run(["--json", "sync", "fund-profile", "519755"], self.context)
        self.assertEqual(exit_code, 0)
        self.assertEqual(partial["errors"], [])
        self.assertTrue(partial["data"]["errors"])
        self.assertEqual(
            set(partial["data"]["request"]),
            {
                "request_id",
                "mode",
                "terminal_status",
                "deadline_at",
                "omitted_work",
            },
        )
        self.assertRegex(partial["data"]["request"]["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(partial["data"]["request"]["mode"], "rapid")
        self.assertEqual(partial["data"]["request"]["terminal_status"], "partial")
        self.assertEqual(
            datetime.fromisoformat(partial["data"]["request"]["deadline_at"])
            - service.request_context.budget.started_at,
            timedelta(seconds=90),
        )

        failed, exit_code, _ = run(["--json", "sync", "fund-holdings", "519755"], self.context)
        self.assertEqual(exit_code, 1)
        self.assertEqual(failed["errors"][0]["code"], "fund_disclosure_sync_failed")
        self.assertEqual(len(failed["data"]["errors"]), 2)
        self.assertEqual(failed["data"]["request"]["terminal_status"], "failed")

    def test_disclosure_sync_deep_force_is_allowlisted_and_bound_to_context(self) -> None:
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())
        service = FakeDisclosureService(profile=disclosure_sync_result())
        self.context.fund_disclosure_service = service

        payload, exit_code, _ = run(
            [
                "--json",
                "sync",
                "fund-profile",
                "519755",
                "--mode",
                "deep",
                "--force",
                "--force-reason",
                "verify_source_recovery",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["request"]["mode"], "deep")
        self.assertEqual(service.request_context.budget.total_seconds, 480.0)
        self.assertEqual(
            datetime.fromisoformat(payload["data"]["request"]["deadline_at"])
            - service.request_context.budget.started_at,
            timedelta(seconds=480),
        )
        self.assertIs(
            service.request_context.force_reason,
            ForceReasonCode.VERIFY_SOURCE_RECOVERY,
        )

    def test_disclosure_sync_rejects_invalid_force_combinations_before_fetch(self) -> None:
        service = FakeDisclosureService(profile=disclosure_sync_result())
        self.context.fund_disclosure_service = service
        for argv in (
            [
                "--json",
                "sync",
                "fund-profile",
                "519755",
                "--force",
                "--force-reason",
                "verify_source_recovery",
            ],
            [
                "--json",
                "sync",
                "fund-profile",
                "519755",
                "--mode",
                "deep",
                "--force",
            ],
            [
                "--json",
                "sync",
                "fund-profile",
                "519755",
                "--mode",
                "deep",
                "--force-reason",
                "verify_source_recovery",
            ],
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")
        self.assertEqual(service.calls, [])

    def test_disclosure_sync_interrupt_finalizes_cancelled_request(self) -> None:
        service = FakeDisclosureService(profile=KeyboardInterrupt())
        self.context.fund_disclosure_service = service

        with self.assertRaises(KeyboardInterrupt):
            run(["--json", "sync", "fund-profile", "519755"], self.context)

        with self.context.repository.connect() as connection:
            row = connection.execute(
                "SELECT status, omitted_work_json FROM request_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("basic_profile", json.loads(row["omitted_work_json"]))

    def test_disclosure_sync_interrupt_preserves_completed_section_metadata(self) -> None:
        service = FakeDisclosureService(
            profile=FundDisclosureSyncInterrupted(
                (
                    "manager_history",
                    "current_manager_team",
                    "fee_schedule",
                    "fees_share_class_relationship",
                    "announcements",
                    "fund_manager_product_announcement",
                )
            )
        )
        self.context.fund_disclosure_service = service

        with self.assertRaises(KeyboardInterrupt):
            run(["--json", "sync", "fund-profile", "519755"], self.context)

        with self.context.repository.connect() as connection:
            row = connection.execute(
                "SELECT omitted_work_json FROM request_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        omitted = json.loads(row["omitted_work_json"])
        self.assertNotIn("basic_profile", omitted)
        self.assertNotIn("size_history", omitted)
        self.assertIn("manager_history", omitted)

    def test_disclosure_sync_exception_keeps_public_request_metadata(self) -> None:
        secret = f"private failure at {self.context.paths.database} token=never-print"
        service = FakeDisclosureService(profile=ValueError(secret))
        self.context.fund_disclosure_service = service

        payload, exit_code, _ = run(
            ["--json", "sync", "fund-profile", "519755"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            set(payload["data"]["request"]),
            {
                "request_id",
                "mode",
                "terminal_status",
                "deadline_at",
                "omitted_work",
            },
        )
        self.assertEqual(payload["data"]["request"]["terminal_status"], "failed")
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(str(self.context.paths.database), rendered)
        self.assertNotIn("never-print", rendered)

    def test_disclosure_sync_primary_and_finalize_failures_stay_safe(self) -> None:
        service = FakeDisclosureService(
            profile=RuntimeError("PRIMARY_PRIVATE_DETAIL token=primary-secret")
        )
        self.context.fund_disclosure_service = service

        with patch(
            "kunjin.cli.DecisionAuditStore.finalize_request",
            side_effect=DecisionAuditStoreError(
                "FINALIZE_PRIVATE_DETAIL token=final-secret"
            ),
        ):
            payload, exit_code, _ = run(
                ["--json", "sync", "fund-profile", "519755"],
                self.context,
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            payload["errors"][0],
            {
                "code": "fund_disclosure_sync_failed",
                "message": "bounded fund disclosure synchronization failed",
            },
        )
        self.assertEqual(payload["data"]["request"]["terminal_status"], "failed")
        rendered = json.dumps(payload, ensure_ascii=False)
        for private in (
            "PRIMARY_PRIVATE_DETAIL",
            "primary-secret",
            "FINALIZE_PRIVATE_DETAIL",
            "final-secret",
        ):
            self.assertNotIn(private, rendered)

    def test_disclosure_sync_expiry_returns_terminal_metadata_and_finalizes(self) -> None:
        class ExpiringDisclosureService(FakeDisclosureService):
            def sync_profile(inner_self, fund_code, *, request_context=None):
                inner_self.calls.append(("profile", fund_code))
                inner_self.request_context = request_context
                request_context.budget.cancel("worker_timeout")
                return inner_self.profile_result

        service = ExpiringDisclosureService(profile=disclosure_sync_result())
        self.context.fund_disclosure_service = service
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())

        payload, exit_code, _ = run(
            ["--json", "sync", "fund-profile", "519755"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["request"]["terminal_status"], "expired")
        with self.context.repository.connect() as connection:
            row = connection.execute(
                "SELECT status, finished_at, deadline_at "
                "FROM request_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertLessEqual(
            datetime.fromisoformat(row["finished_at"]),
            datetime.fromisoformat(row["deadline_at"]),
        )

    def test_disclosure_sync_late_finish_is_expired_without_backdating_audit(self) -> None:
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())
        service = FakeDisclosureService(profile=disclosure_sync_result())
        self.context.fund_disclosure_service = service

        def late_finish() -> datetime:
            return service.request_context.budget.deadline_at + timedelta(seconds=1)

        with patch("kunjin.cli._request_finish_now", side_effect=late_finish):
            payload, exit_code, _ = run(
                ["--json", "sync", "fund-profile", "519755"],
                self.context,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["request"]["terminal_status"], "expired")
        with self.context.repository.connect() as connection:
            row = connection.execute(
                "SELECT status, finished_at, deadline_at "
                "FROM request_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "expired")
        self.assertGreater(
            datetime.fromisoformat(row["finished_at"]),
            datetime.fromisoformat(row["deadline_at"]),
        )

    def test_disclosure_commands_reject_invalid_fund_code(self) -> None:
        for argv in (
            ["--json", "sync", "fund-profile", "123"],
            ["--json", "sync", "fund-holdings", "abc123"],
            ["--json", "fund", "profile", "123"],
            ["--json", "fund", "fees", "123"],
            ["--json", "fund", "holdings", "123"],
            ["--json", "fund", "announcements", "123"],
        ):
            with self.subTest(argv=argv):
                payload, exit_code, _ = run(argv, self.context)
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["errors"][0]["code"], "invalid_fund_code")

    def test_fund_holdings_rejects_invalid_period(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "fund", "holdings", "519755", "--period", "2026-02-30"],
            self.context,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["errors"][0]["code"], "invalid_report_period")

    def test_fund_holdings_marks_a_missing_requested_period(self) -> None:
        self.context.fund_disclosure_store = FakeDisclosureStore(empty_disclosure_bundle())

        payload, exit_code, _ = run(
            ["--json", "fund", "holdings", "519755", "--period", "2026-06-30"],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["data"]["holdings"]["evidence_level"], "insufficient_data")
        self.assertIn("requested_report_period_not_found", payload["data"]["warnings"])

    def test_daily_sync_isolates_nav_profile_and_holdings_failures(self) -> None:
        class PortfolioService:
            def sync_portfolio(inner_self, trigger):
                return SyncResult(1, 1, 1, datetime.now(timezone.utc))

        class ResearchService:
            def sync_fund(inner_self, fund_code):
                if fund_code == "016067":
                    raise RuntimeError("NAV unavailable")
                return ResearchSyncResult(fund_code, 10)

            def sync_market(inner_self):
                return ResearchSyncResult("market_sectors", 2)

        stale = {
            name: SectionSyncResult(name, "success", 1, "stale")
            for name in (
                "basic_profile",
                "manager_history",
                "fee_schedule",
                "size_history",
                "announcements",
                "quarterly_holdings",
                "industry_exposure",
            )
        }
        disclosures = FakeDisclosureService(
            profile=RuntimeError("profile unavailable"),
            holdings=disclosure_sync_result(profile=False),
            snapshots=stale,
        )
        self.context.sync_service = PortfolioService()
        self.context.research_service = ResearchService()
        self.context.fund_disclosure_service = disclosures

        payload, exit_code, _ = run(["--json", "sync", "daily"], self.context)

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["data"]["market"]["observations"], 2)
        self.assertIn("519755", payload["data"]["funds"])
        self.assertIn(("holdings", "016067"), disclosures.calls)
        self.assertIn(("holdings", "519755"), disclosures.calls)
        self.assertGreaterEqual(len(payload["errors"]), 3)

    def test_daily_sync_isolates_peer_refresh_failures(self) -> None:
        class PortfolioService:
            def sync_portfolio(inner_self, trigger):
                return SyncResult(1, 1, 1, datetime.now(timezone.utc))

        class ResearchService:
            def sync_fund(inner_self, fund_code):
                return ResearchSyncResult(fund_code, 10)

            def sync_market(inner_self):
                return ResearchSyncResult("market_sectors", 2)

        fresh = {
            name: SectionSyncResult(name, "success", 1, "fresh")
            for name in (
                "basic_profile",
                "manager_history",
                "fee_schedule",
                "size_history",
                "announcements",
                "quarterly_holdings",
                "industry_exposure",
            )
        }
        peer_result = PeerSyncResult(
            "519755",
            PeerSyncState.SOURCE_UNAVAILABLE,
            3,
            2,
            1,
            0,
            ("peer_group_refresh_failed",),
            (
                {
                    "fund_code": "519755",
                    "stage": "refresh",
                    "error_code": "network_unavailable",
                    "message": "directory unavailable",
                },
            ),
        )
        peer_service = FakePeerService(peer_result, refresh_result={"519755": peer_result})
        self.context.sync_service = PortfolioService()
        self.context.research_service = ResearchService()
        self.context.fund_disclosure_service = FakeDisclosureService(
            profile=disclosure_sync_result(),
            holdings=disclosure_sync_result(profile=False),
            snapshots=fresh,
        )
        self.context.peer_service = peer_service

        payload, exit_code, _ = run(["--json", "sync", "daily"], self.context)

        self.assertEqual(exit_code, 1)
        self.assertEqual(tuple(payload["data"]["peer_groups"]), ("519755",))
        self.assertEqual(
            payload["data"]["peer_groups"]["519755"]["status"],
            "source_unavailable",
        )
        self.assertEqual(payload["data"]["market"]["observations"], 2)
        self.assertEqual(set(payload["data"]["funds"]), {"016067", "519755"})
        for fund_result in payload["data"]["funds"].values():
            self.assertIn("nav", fund_result)
            self.assertEqual(fund_result["profile"], {"status": "fresh"})
            self.assertEqual(fund_result["holdings"], {"status": "fresh"})
        self.assertEqual(peer_service.refresh_calls, 1)
        self.assertEqual(peer_service.calls, [])
        self.assertEqual(payload["errors"][0]["code"], "network_unavailable")
        self.assertIn("519755", payload["errors"][0]["message"])
        self.assertIn("directory unavailable", payload["errors"][0]["message"])

    def test_ledger_import_has_stable_private_json_contract(self) -> None:
        payload, exit_code, _ = run(
            [
                "--json",
                "ledger",
                "import",
                str(self.payment_image),
                "--fund-code",
                "519755",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "ledger.import")
        self.assertEqual(payload["data"]["document_id"], 1)
        self.assertFalse(payload["data"]["requires_confirmation"])
        self.assertEqual(
            payload["data"]["draft"]["field_evidence"]["amount"],
            "transaction_confirmed",
        )
        self.assertEqual(payload["data"]["fields"]["amount"]["normalized_value"], "20.00")
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("managed_path", rendered)
        self.assertNotIn(str(self.context.paths.imports), rendered)
        self.assertNotIn("商家订单号", rendered)
        self.assertNotIn("synthetic payment screenshot", rendered)

    def test_ledger_import_uses_parser_confirmation_rule(self) -> None:
        self.context.ledger_service.ocr_client = FakeOcrClient("0.79")

        payload, exit_code, _ = run(
            ["--json", "ledger", "import", str(self.payment_image)], self.context
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["data"]["requires_confirmation"])

    def test_ledger_drafts_confirm_and_transactions(self) -> None:
        imported, _, _ = run(["--json", "ledger", "import", str(self.payment_image)], self.context)
        draft_id = imported["data"]["draft"]["id"]

        drafts, exit_code, _ = run(["--json", "ledger", "drafts"], self.context)
        self.assertEqual(exit_code, 0)
        self.assert_envelope(drafts, "ledger.drafts")
        self.assertEqual([item["id"] for item in drafts["data"]["drafts"]], [draft_id])

        confirmed, exit_code, _ = run(
            [
                "--json",
                "ledger",
                "confirm",
                str(draft_id),
                "--field",
                "fund_code=519755",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assert_envelope(confirmed, "ledger.confirm")
        self.assertEqual(
            confirmed["data"]["transaction"]["field_evidence"]["fund_code"],
            "user_confirmed",
        )

        transactions, exit_code, _ = run(
            ["--json", "ledger", "transactions", "--fund-code", "519755"],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assert_envelope(transactions, "ledger.transactions")
        self.assertEqual(len(transactions["data"]["transactions"]), 1)

    def test_ledger_add_and_reconcile_do_not_sync(self) -> None:
        class SyncMustNotRun:
            def sync_portfolio(inner_self, trigger):
                raise AssertionError("ledger reconcile must not synchronize")

        self.context.sync_service = SyncMustNotRun()
        added, exit_code, _ = run(
            [
                "--json",
                "ledger",
                "add",
                "--type",
                "subscription",
                "--fund-code",
                "519755",
                "--amount",
                "20.00",
                "--order-time",
                "2026-07-04T23:11:51+08:00",
            ],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assert_envelope(added, "ledger.add")
        self.assertEqual(added["data"]["transaction"]["evidence_level"], "user_confirmed")

        reconciled, exit_code, _ = run(
            ["--json", "ledger", "reconcile", "--fund-code", "519755"],
            self.context,
        )
        self.assertEqual(exit_code, 0)
        self.assert_envelope(reconciled, "ledger.reconcile")
        self.assertEqual(reconciled["data"]["result"]["status"], "consistent")
        self.assertEqual(reconciled["data"]["result"]["evidence_level"], "position_inferred")

    def test_ledger_reconcile_missing_position_has_stable_error(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "ledger", "reconcile", "--fund-code", "000001"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "ledger.reconcile")
        self.assertEqual(payload["errors"][0]["code"], "position_not_found")

    def test_ledger_reconcile_rejects_invalid_fund_code(self) -> None:
        payload, exit_code, _ = run(
            ["--json", "ledger", "reconcile", "--fund-code", "123"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "ledger.reconcile")
        self.assertEqual(payload["errors"][0]["code"], "invalid_fund_code")

    def test_ledger_reconcile_rejects_ambiguous_position_accounts(self) -> None:
        now = datetime.now(timezone.utc)
        account_one = AccountObservation("yangjibao", "account-1", "学习账户", now)
        account_two = AccountObservation("yangjibao", "account-2", "另一个账户", now)
        sync_run_id = self.context.repository.begin_sync("yangjibao", "test")
        self.context.repository.commit_sync(
            sync_run_id,
            [],
            [
                (
                    account_one,
                    [
                        PositionObservation(
                            "account-1",
                            "519755",
                            "成长基金",
                            Decimal("11.32"),
                            now,
                            formal_nav=Decimal("1.7467"),
                            observed_profit=Decimal("-0.23"),
                        )
                    ],
                ),
                (
                    account_two,
                    [
                        PositionObservation(
                            "account-2",
                            "519755",
                            "成长基金",
                            Decimal("1"),
                            now,
                            formal_nav=Decimal("1.7467"),
                            observed_profit=Decimal("0"),
                        )
                    ],
                ),
            ],
            observed_at=now,
        )

        payload, exit_code, _ = run(
            ["--json", "ledger", "reconcile", "--fund-code", "519755"],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "ledger.reconcile")
        self.assertEqual(payload["errors"][0]["code"], "ambiguous_position_accounts")
        self.assertEqual(payload["data"]["account_titles"], ["另一个账户", "学习账户"])

    def test_ledger_document_delete_removes_only_managed_copy(self) -> None:
        imported, _, _ = run(
            [
                "--json",
                "ledger",
                "import",
                str(self.payment_image),
                "--fund-code",
                "519755",
            ],
            self.context,
        )
        document_id = imported["data"]["document_id"]

        payload, exit_code, _ = run(
            ["--json", "ledger", "document", "delete", str(document_id)],
            self.context,
        )

        self.assertEqual(exit_code, 0)
        self.assert_envelope(payload, "ledger.document.delete")
        self.assertTrue(payload["data"]["deleted"])
        self.assertTrue(self.payment_image.is_file())

    def test_invalid_ledger_field_override_is_a_stable_error(self) -> None:
        imported, _, _ = run(["--json", "ledger", "import", str(self.payment_image)], self.context)
        draft_id = imported["data"]["draft"]["id"]

        payload, exit_code, _ = run(
            [
                "--json",
                "ledger",
                "confirm",
                str(draft_id),
                "--field",
                "amount-without-equals",
            ],
            self.context,
        )

        self.assertEqual(exit_code, 1)
        self.assert_envelope(payload, "ledger.confirm")
        self.assertEqual(payload["errors"][0]["code"], "operation_failed")

    def test_json_argument_errors_return_stable_envelopes(self) -> None:
        cases = [
            (
                ["--json", "ledger", "confirm", "abc"],
                "ledger.confirm",
            ),
            (
                ["--json", "ledger", "add", "--type", "subscription"],
                "ledger.add",
            ),
            (
                ["--json", "ledger", "unknown"],
                "ledger.unknown",
            ),
            (
                ["--json", "ledger", "document", "delete", "abc"],
                "ledger.document.delete",
            ),
        ]
        for argv, command in cases:
            with self.subTest(argv=argv):
                payload, exit_code, json_output = run(argv, self.context)

                self.assertEqual(exit_code, 1)
                self.assertTrue(json_output)
                self.assert_envelope(payload, command)
                self.assertEqual(payload["errors"][0]["code"], "invalid_arguments")

    def assert_envelope(self, payload, command) -> None:
        self.assertEqual(
            set(payload),
            {"schema_version", "command", "as_of", "data", "warnings", "errors"},
        )
        self.assertEqual(payload["command"], command)


if __name__ == "__main__":
    unittest.main()
