import json
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
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
from kunjin.cli import ApplicationContext, build_context, run
from kunjin.funds.models import DisclosureBundle
from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
    PeerSyncState,
)
from kunjin.funds.peers.service import PeerSyncResult
from kunjin.funds.service import FundDisclosureSyncResult, SectionSyncResult
from kunjin.funds.store import FundDisclosureStore
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

    def sync_profile(self, fund_code):
        self.calls.append(("profile", fund_code))
        if isinstance(self.profile_result, Exception):
            raise self.profile_result
        return self.profile_result

    def sync_holdings(self, fund_code):
        self.calls.append(("holdings", fund_code))
        if isinstance(self.holdings_result, Exception):
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

        failed, exit_code, _ = run(["--json", "sync", "fund-holdings", "519755"], self.context)
        self.assertEqual(exit_code, 1)
        self.assertEqual(failed["errors"][0]["code"], "fund_disclosure_sync_failed")
        self.assertEqual(len(failed["data"]["errors"]), 2)

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
        self.context.repository.replace_snapshot(
            AccountObservation("yangjibao", "account-2", "另一个账户", now),
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
