from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kunjin.decision.budget import RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    ForceReasonCode,
    RequestMode,
    RequestTerminalStatus,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.decision.worker_protocol import WorkerResponse, WorkerTextPayload
from kunjin.funds.models import DocumentKind
from kunjin.funds.service import (
    SECTION_SPECS,
    FundDisclosureService,
    SourceRequestContext,
    announcement_report_period,
    expected_report_period,
)
from kunjin.funds.sources import FundSourceError, TextResponse
from kunjin.funds.store import FundDisclosureStore
from kunjin.storage.repository import Repository

FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds"
WORKER_FIXTURE = Path(__file__).parents[1] / "fixtures" / "decision" / "worker_fixture.py"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeTextClient:
    def __init__(self, failures: set[DocumentKind] | None = None) -> None:
        self.failures = failures or set()
        self.overrides: dict[DocumentKind, str] = {}
        self.retrieved_at = datetime(2026, 7, 11, 9, tzinfo=SHANGHAI)
        self.requested_urls: list[str] = []

    def fetch(self, url: str, referer: str) -> TextResponse:
        del referer
        self.requested_urls.append(url)
        parsed_url = urlparse(url)
        query = parsed_url.query
        filename = Path(urlparse(url).path).name
        if "type=gmbd" in query:
            kind = DocumentKind.SIZE_HISTORY
        elif "type=jjcc" in query:
            kind = DocumentKind.QUARTERLY_HOLDINGS
        elif filename == "HYPZ":
            kind = DocumentKind.INDUSTRY_EXPOSURE
        elif filename == "JJGG":
            kind = DocumentKind.ANNOUNCEMENT
        else:
            kind = next(
                kind
                for kind, prefix in {
                DocumentKind.BASIC_PROFILE: "jbgk_",
                DocumentKind.MANAGER_HISTORY: "jjjl_",
                DocumentKind.FEE_SCHEDULE: "jjfl_",
                }.items()
                if filename.startswith(prefix)
            )
        if kind in self.failures:
            raise FundSourceError(f"synthetic failure for {kind.value}")
        fixture_name = {
            DocumentKind.BASIC_PROFILE: "basic_profile.html",
            DocumentKind.MANAGER_HISTORY: "manager_history.html",
            DocumentKind.FEE_SCHEDULE: "fee_schedule.html",
            DocumentKind.SIZE_HISTORY: "size_history.html",
            DocumentKind.QUARTERLY_HOLDINGS: "quarterly_holdings.html",
            DocumentKind.INDUSTRY_EXPOSURE: "industry_exposure.html",
            DocumentKind.ANNOUNCEMENT: "announcements.html",
        }[kind]
        text = self.overrides.get(kind, (FIXTURES / fixture_name).read_text("utf-8"))
        return TextResponse(
            requested_url=url,
            final_url=url,
            text=text,
            retrieved_at=self.retrieved_at,
            checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_type="text/html; charset=utf-8",
        )


class FakeWorkerRunner:
    def __init__(self) -> None:
        self.calls = []
        self.failures: dict[str, list[SourceErrorCode]] = {}
        self.text_overrides: dict[str, str] = {}
        self.after_call = None

    def __call__(self, request, budget) -> WorkerResponse:
        self.calls.append(request)
        failures = self.failures.get(request.field_id, [])
        if failures:
            code = failures.pop(0)
            response = WorkerResponse(
                schema_version=request.schema_version,
                request_id=request.request_id,
                source_id=request.source_id,
                field_id=request.field_id,
                subject_key=request.subject_key,
                operation=request.operation,
                ok=False,
                payload=None,
                reason_code=code.value,
                retryable=code
                in {
                    SourceErrorCode.DNS_FAILURE,
                    SourceErrorCode.TRANSIENT_NETWORK_FAILURE,
                    SourceErrorCode.NETWORK_TIMEOUT,
                },
                message=f"public source error: {code.value}",
            )
        else:
            url = request.arguments["url"]
            kind = self._kind(url)
            text = self.text_overrides.get(
                request.field_id,
                (FIXTURES / self._fixture(kind)).read_text("utf-8"),
            )
            retrieved_at = budget.started_at
            response = WorkerResponse(
                schema_version=request.schema_version,
                request_id=request.request_id,
                source_id=request.source_id,
                field_id=request.field_id,
                subject_key=request.subject_key,
                operation=request.operation,
                ok=True,
                payload=WorkerTextPayload(
                    requested_url=url,
                    final_url=url,
                    text=text,
                    text_checksum=hashlib.sha256(text.encode()).hexdigest(),
                    retrieved_at=retrieved_at,
                    checksum=hashlib.sha256(text.encode()).hexdigest(),
                    content_type="text/html; charset=utf-8",
                ),
                reason_code=None,
                retryable=None,
                message=None,
            )
        if self.after_call is not None:
            self.after_call(request)
        return response

    @staticmethod
    def _kind(url: str) -> DocumentKind:
        parsed_url = urlparse(url)
        filename = Path(parsed_url.path).name
        if "type=gmbd" in parsed_url.query:
            return DocumentKind.SIZE_HISTORY
        if "type=jjcc" in parsed_url.query:
            return DocumentKind.QUARTERLY_HOLDINGS
        if filename == "HYPZ":
            return DocumentKind.INDUSTRY_EXPOSURE
        if filename == "JJGG":
            return DocumentKind.ANNOUNCEMENT
        return next(
            kind
            for kind, prefix in {
                DocumentKind.BASIC_PROFILE: "jbgk_",
                DocumentKind.MANAGER_HISTORY: "jjjl_",
                DocumentKind.FEE_SCHEDULE: "jjfl_",
            }.items()
            if filename.startswith(prefix)
        )

    @staticmethod
    def _fixture(kind: DocumentKind) -> str:
        return {
            DocumentKind.BASIC_PROFILE: "basic_profile.html",
            DocumentKind.MANAGER_HISTORY: "manager_history.html",
            DocumentKind.FEE_SCHEDULE: "fee_schedule.html",
            DocumentKind.SIZE_HISTORY: "size_history.html",
            DocumentKind.QUARTERLY_HOLDINGS: "quarterly_holdings.html",
            DocumentKind.INDUSTRY_EXPOSURE: "industry_exposure.html",
            DocumentKind.ANNOUNCEMENT: "announcements.html",
        }[kind]


class FundDisclosureServiceTest(unittest.TestCase):
    def test_bounded_sections_bind_worker_and_registry_fields_explicitly(self) -> None:
        expected = {
            "basic_profile": ("basic_profile", "identity_active_status"),
            "manager_history": ("manager_history", "current_manager_team"),
            "fee_schedule": ("fee_schedule", "fees_share_class_relationship"),
            "size_history": ("size_history", "identity_active_status"),
            "quarterly_holdings": ("quarterly_holdings", "holdings_industries"),
            "industry_exposure": ("industry_exposure", "holdings_industries"),
            "announcements": (
                "announcement",
                "fund_manager_product_announcement",
            ),
        }

        self.assertEqual(
            {
                section: (spec.worker_field_id, spec.audit_field_id)
                for section, spec in SECTION_SPECS.items()
            },
            expected,
        )
        self.assertTrue(hasattr(SourceRequestContext, "__dataclass_fields__"))

    def test_real_dynamic_contract_uses_announcements_to_fill_publication_date(self) -> None:
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = json.dumps(
            {"Data": [{
                "TITLE": "交银多策略回报灵活配置混合型证券投资基金2026年第2季度报告",
                "NEWCATEGORY": "3", "PUBLISHDATEDesc": "2026-07-20",
                "ATTACHTYPE": ".pdf", "ID": "AN202607200001",
            }]}, ensure_ascii=False,
        )
        content = """<h4>2026年2季度股票投资明细</h4><table>
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr>
          <tr><td>1</td><td>000001</td><td>平安银行</td><td>6.25%</td></tr></table>"""
        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = (
            "var apidata=" + json.dumps({"content": content}, ensure_ascii=False) + ";"
        )

        result = self.service.sync_all("519755")

        self.assertEqual(result.sections["announcements"].status, "success")
        self.assertEqual(result.sections["quarterly_holdings"].status, "success")
        holding = self.store.load_bundle("519755").holdings[0]
        self.assertEqual(holding.published_at, datetime(2026, 7, 20, tzinfo=SHANGHAI))
        self.assertTrue(
            any(
                "FundArchivesDatas.aspx?type=jjcc" in url
                for url in self.client.requested_urls
            )
        )
        self.assertTrue(
            any(
                "api.fund.eastmoney.com/f10/JJGG" in url
                for url in self.client.requested_urls
            )
        )

    def test_dynamic_holdings_without_matching_announcement_fail_explicitly(self) -> None:
        content = """<h4>2025年4季度股票投资明细</h4><table>
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th></tr>
          <tr><td>1</td><td>000001</td><td>平安银行</td><td>6.25%</td></tr></table>"""
        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = (
            "var apidata=" + json.dumps({"content": content}, ensure_ascii=False) + ";"
        )

        result = self.service.sync_holdings("519755")

        self.assertEqual(result.sections["quarterly_holdings"].status, "source_unavailable")
        self.assertEqual(
            result.sections["quarterly_holdings"].error_code,
            "missing_publication_date",
        )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.store = FundDisclosureStore(self.repository)
        self.client = FakeTextClient()
        self.as_of = datetime(2026, 7, 11, 12, tzinfo=SHANGHAI)
        self.audit_now = self.as_of.astimezone(timezone.utc)
        self.worker = FakeWorkerRunner()
        self.service = FundDisclosureService(
            self.client,
            self.store,
            now=lambda: self.as_of,
            worker_runner=self.worker,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _bounded_context(
        self,
        request_id: str,
        *,
        mode: RequestMode = RequestMode.RAPID,
        force_reason: ForceReasonCode | None = None,
        ticks: list[float] | None = None,
    ) -> SourceRequestContext:
        monotonic_ticks = ticks or [100.0]
        budget = RequestBudget.create(
            mode,
            request_id=request_id,
            monotonic=lambda: monotonic_ticks[0],
            wall_clock=lambda: self.audit_now,
        )
        audit_store = DecisionAuditStore(self.repository)
        request_run_id = audit_store.begin_request(budget)
        health = SourceHealthService(
            audit_store,
            wall_clock=lambda: self.audit_now,
        )
        return SourceRequestContext(
            request_run_id,
            budget,
            audit_store,
            health,
            force_reason,
        )

    def _finalize(self, context: SourceRequestContext, result) -> None:
        successful = any(
            section.status in {"success", "not_disclosed"}
            and section.error_code is None
            for section in result.sections.values()
        )
        complete = successful and not result.omitted_work and all(
            section.status in {"success", "not_disclosed"}
            and section.error_code is None
            for section in result.sections.values()
        )
        status = (
            RequestTerminalStatus.COMPLETE
            if complete
            else RequestTerminalStatus.PARTIAL
            if successful
            else RequestTerminalStatus.FAILED
        )
        context.audit_store.finalize_request(
            context.request_run_id,
            status,
            self.audit_now,
            result.omitted_work,
        )

    def test_bounded_profile_uses_one_fund_workers_and_parent_publication(self) -> None:
        context = self._bounded_context("1" * 32)

        result = self.service.sync_profile("519755", request_context=context)
        self._finalize(context, result)

        self.assertEqual(self.client.requested_urls, [])
        self.assertEqual(
            [request.field_id for request in self.worker.calls],
            [
                "basic_profile",
                "size_history",
                "manager_history",
                "fee_schedule",
                "announcement",
            ],
        )
        self.assertTrue(
            all(request.subject_key == "fund:519755" for request in self.worker.calls)
        )
        self.assertTrue(
            all(
                item.status in {"success", "not_disclosed"}
                for item in result.sections.values()
            )
        )
        self.assertEqual(result.omitted_work, ())
        histories = {
            field_id: context.audit_store.source_attempt_history(
                "eastmoney_f10", field_id, "fund:519755"
            )
            for field_id in {
                "identity_active_status",
                "current_manager_team",
                "fees_share_class_relationship",
                "fund_manager_product_announcement",
            }
        }
        self.assertTrue(
            all(
                len(history) == 1
                and history[0].attempt.outcome is SourceAttemptOutcome.SUCCESS
                for history in histories.values()
            )
        )

    def test_bounded_sync_all_groups_seven_sections_into_five_audit_fields(self) -> None:
        context = self._bounded_context("ab" * 16)

        result = self.service.sync_all("519755", request_context=context)

        self.assertEqual(len(self.worker.calls), 7)
        self.assertEqual(len(result.sections), 7)
        self.assertEqual(result.omitted_work, ())
        for field_id in (
            "identity_active_status",
            "current_manager_team",
            "fees_share_class_relationship",
            "fund_manager_product_announcement",
            "holdings_industries",
        ):
            history = context.audit_store.source_attempt_history(
                "eastmoney_f10", field_id, "fund:519755"
            )
            self.assertEqual(len(history), 1)
            self.assertIs(
                history[0].attempt.outcome,
                SourceAttemptOutcome.SUCCESS,
            )

    def test_real_worker_fixture_returns_bytes_for_parent_parse_and_publication(self) -> None:
        context = self._bounded_context("ac" * 16)
        service = FundDisclosureService(
            self.client,
            self.store,
            now=lambda: self.as_of,
        )
        argv = (
            sys.executable,
            str(WORKER_FIXTURE),
            "success_file",
            str(FIXTURES / "basic_profile.html"),
            context.budget.started_at.isoformat(),
        )
        self.assertEqual(self.store.section_status("519755"), {})

        with patch(
            "kunjin.decision.worker._default_worker_argv",
            return_value=argv,
        ):
            result = service.sync_classification(
                "519755", request_context=context
            )

        self.assertEqual(result.sections["basic_profile"].status, "success")
        self.assertIsNotNone(self.store.load_bundle("519755").identity)
        self.assertEqual(self.client.requested_urls, [])
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(history[0].attempt.outcome, SourceAttemptOutcome.SUCCESS)

    def test_bounded_group_failure_keeps_other_sections_and_is_not_audit_success(self) -> None:
        self.worker.failures["size_history"] = [SourceErrorCode.HTTP_4XX]
        context = self._bounded_context("2" * 32)

        result = self.service.sync_profile("519755", request_context=context)

        self.assertEqual(result.sections["basic_profile"].status, "success")
        self.assertEqual(result.sections["size_history"].error_code, "http_4xx")
        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertIn("size_history", result.omitted_work)
        self.assertIn("identity_active_status", result.omitted_work)
        identity = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertEqual(len(identity), 1)
        self.assertIs(identity[0].attempt.outcome, SourceAttemptOutcome.UNAVAILABLE)
        self.assertEqual(
            [call.field_id for call in self.worker.calls].count("size_history"),
            1,
        )

    def test_bounded_transient_group_failure_consumes_one_retry_authorization(self) -> None:
        self.worker.failures["manager_history"] = [SourceErrorCode.DNS_FAILURE]
        context = self._bounded_context("3" * 32)

        result = self.service.sync_profile("519755", request_context=context)

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(
            [call.field_id for call in self.worker.calls].count("manager_history"),
            2,
        )
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "current_manager_team", "fund:519755"
        )
        self.assertEqual(
            [item.attempt.outcome for item in history],
            [SourceAttemptOutcome.SUCCESS, SourceAttemptOutcome.TRANSIENT_FAILURE],
        )
        self.assertIsNotNone(history[0].authorization_id)
        self.assertIsNone(history[1].authorization_id)

    def test_bounded_retry_uses_exact_current_parent_despite_future_attempt(self) -> None:
        self.worker.failures["manager_history"] = [SourceErrorCode.DNS_FAILURE]
        context = self._bounded_context("31" * 16)
        inserted = [False]

        def insert_future_attempt(request) -> None:
            if request.field_id != "manager_history" or inserted[0]:
                return
            inserted[0] = True
            future_now = self.audit_now + timedelta(days=1)
            future_budget = RequestBudget.create(
                RequestMode.RAPID,
                request_id="32" * 16,
                monotonic=lambda: 500.0,
                wall_clock=lambda: future_now,
            )
            future_store = DecisionAuditStore(self.repository)
            future_run_id = future_store.begin_request(future_budget)
            registry = SourceRegistryV1()
            future_store.record_source_attempt(
                future_run_id,
                SourceAttempt(
                    source_id="eastmoney_f10",
                    field_id="current_manager_team",
                    subject_key="fund:519755",
                    attempt_number=1,
                    outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
                    started_at=future_now,
                    finished_at=future_now,
                    data_as_of=None,
                    error_code=SourceErrorCode.DNS_FAILURE,
                    cooldown_until=future_now + timedelta(minutes=30),
                    force_actor=None,
                    force_reason=None,
                    registry_version=registry.version,
                    registry_checksum=registry.checksum(),
                    response_bytes=0,
                ),
            )
            future_store.finalize_request(
                future_run_id,
                RequestTerminalStatus.PARTIAL,
                future_now,
                (),
            )

        self.worker.after_call = insert_future_attempt
        result = self.service.sync_profile("519755", request_context=context)

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(
            [call.field_id for call in self.worker.calls].count("manager_history"),
            2,
        )
        current_attempts = tuple(
            record
            for record in context.audit_store.source_attempt_history(
                "eastmoney_f10", "current_manager_team", "fund:519755"
            )
            if record.request_id == "31" * 16
        )
        self.assertEqual(len(current_attempts), 2)
        self.assertIsNotNone(current_attempts[0].authorization_id)

    def test_bounded_deadline_discards_result_and_stops_new_scheduling(self) -> None:
        ticks = [100.0]
        context = self._bounded_context("4" * 32, ticks=ticks)

        def expire_after_first(_request) -> None:
            ticks[0] = context.budget.monotonic_deadline

        self.worker.after_call = expire_after_first
        result = self.service.sync_profile("519755", request_context=context)

        self.assertEqual([call.field_id for call in self.worker.calls], ["basic_profile"])
        self.assertEqual(context.budget.cancel_reason, "request_deadline_reached")
        self.assertIsNone(self.store.load_bundle("519755").identity)
        self.assertEqual(self.store.section_status("519755"), {})
        self.assertIn("basic_profile", result.omitted_work)
        self.assertIn("current_manager_team", result.omitted_work)
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(history[0].attempt.outcome, SourceAttemptOutcome.EXPIRED)

    def test_bounded_publish_rolls_back_when_budget_expires_inside_store(self) -> None:
        ticks = [100.0]
        context = self._bounded_context("41" * 16, ticks=ticks)
        original_insert = self.store._insert_record

        def expire_after_insert(connection, record) -> None:
            original_insert(connection, record)
            ticks[0] = context.budget.monotonic_deadline - 1.0

        with patch.object(self.store, "_insert_record", side_effect=expire_after_insert):
            result = self.service.sync_classification(
                "519755", request_context=context
            )

        self.assertEqual(result.sections["basic_profile"].status, "missing")
        self.assertEqual(self.store.section_status("519755"), {})
        self.assertIsNone(self.store.load_bundle("519755").identity)
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(history[0].attempt.outcome, SourceAttemptOutcome.EXPIRED)

    def test_bounded_failure_mark_rolls_back_when_budget_expires_before_commit(self) -> None:
        ticks = [100.0]
        context = self._bounded_context("42" * 16, ticks=ticks)
        self.worker.failures["basic_profile"] = [SourceErrorCode.HTTP_4XX]
        original_require = self.store._require_budget
        checks = [0]

        def expire_on_final_check(budget) -> None:
            checks[0] += 1
            if checks[0] == 3:
                ticks[0] = context.budget.monotonic_deadline - 1.0
            original_require(budget)

        with patch.object(
            self.store,
            "_require_budget",
            side_effect=expire_on_final_check,
        ):
            result = self.service.sync_classification(
                "519755", request_context=context
            )

        self.assertEqual(result.sections["basic_profile"].status, "missing")
        self.assertEqual(self.store.section_status("519755"), {})
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(history[0].attempt.outcome, SourceAttemptOutcome.EXPIRED)

    def test_bounded_retry_failure_reports_section_and_group_as_omitted(self) -> None:
        self.worker.failures["manager_history"] = [
            SourceErrorCode.DNS_FAILURE,
            SourceErrorCode.NETWORK_TIMEOUT,
        ]
        context = self._bounded_context("9" * 32)

        result = self.service.sync_profile("519755", request_context=context)

        self.assertIn("manager_history", result.omitted_work)
        self.assertIn("current_manager_team", result.omitted_work)
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "current_manager_team", "fund:519755"
        )
        self.assertEqual(len(history), 2)
        self.assertTrue(
            all(
                item.attempt.outcome is SourceAttemptOutcome.TRANSIENT_FAILURE
                for item in history
            )
        )

    def test_bounded_parse_and_identity_failures_are_not_retried(self) -> None:
        cases = (
            ("f" * 32, "<html><body>missing fields</body></html>", "parse_failure"),
            (
                "0" * 32,
                (FIXTURES / "basic_profile.html")
                .read_text("utf-8")
                .replace("<dd>519755</dd>", "<dd>000001</dd>", 1),
                "identity_conflict",
            ),
        )
        for request_id, text, expected in cases:
            with self.subTest(expected=expected):
                self.worker.calls.clear()
                self.worker.text_overrides["basic_profile"] = text
                context = self._bounded_context(request_id)

                result = self.service.sync_classification(
                    "519755", request_context=context
                )

                self.assertEqual(len(self.worker.calls), 1)
                self.assertIn("basic_profile", result.omitted_work)
                history = context.audit_store.source_attempt_history(
                    "eastmoney_f10", "identity_active_status", "fund:519755"
                )
                self.assertEqual(history[0].request_id, request_id)
                self.assertIs(
                    history[0].attempt.outcome,
                    SourceAttemptOutcome.UNAVAILABLE,
                )
                self.assertEqual(history[0].attempt.error_code.value, expected)

    def test_bounded_transient_without_retry_budget_reports_omitted(self) -> None:
        ticks = [100.0]
        context = self._bounded_context("a" * 32, ticks=ticks)
        self.worker.failures["basic_profile"] = [SourceErrorCode.DNS_FAILURE]

        def leave_too_little_worker_time(request) -> None:
            if request.field_id == "basic_profile":
                ticks[0] = context.budget.monotonic_deadline - 2.1

        self.worker.after_call = leave_too_little_worker_time
        result = self.service.sync_classification(
            "519755", request_context=context
        )

        self.assertEqual(len(self.worker.calls), 1)
        self.assertIn("basic_profile", result.omitted_work)
        self.assertIn("identity_active_status", result.omitted_work)
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertEqual(len(history), 1)
        self.assertIs(
            history[0].attempt.outcome,
            SourceAttemptOutcome.TRANSIENT_FAILURE,
        )

    def test_bounded_cooldown_skips_and_deep_force_consumes_authorization(self) -> None:
        self.worker.failures["basic_profile"] = [
            SourceErrorCode.DNS_FAILURE,
            SourceErrorCode.DNS_FAILURE,
        ]
        failed_context = self._bounded_context("5" * 32)
        failed = self.service.sync_classification(
            "519755", request_context=failed_context
        )
        self._finalize(failed_context, failed)
        calls_after_failure = len(self.worker.calls)

        cooldown_context = self._bounded_context("6" * 32)
        cooldown = self.service.sync_classification(
            "519755", request_context=cooldown_context
        )

        self.assertEqual(len(self.worker.calls), calls_after_failure)
        self.assertIn("basic_profile", cooldown.omitted_work)
        cooldown_history = cooldown_context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(
            cooldown_history[0].attempt.outcome,
            SourceAttemptOutcome.SKIPPED_COOLDOWN,
        )

        force_context = self._bounded_context(
            "7" * 32,
            mode=RequestMode.DEEP,
            force_reason=ForceReasonCode.VERIFY_SOURCE_RECOVERY,
        )
        forced = self.service.sync_classification(
            "519755", request_context=force_context
        )

        self.assertEqual(forced.sections["basic_profile"].status, "success")
        force_history = force_context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertEqual(force_history[0].request_id, "7" * 32)
        self.assertEqual(force_history[0].attempt.force_actor, "local_owner")
        self.assertIs(
            force_history[0].attempt.force_reason,
            ForceReasonCode.VERIFY_SOURCE_RECOVERY,
        )
        self.assertIsNotNone(force_history[0].authorization_id)

    def test_bounded_keyboard_interrupt_records_cancel_and_parent_can_finalize(self) -> None:
        context = self._bounded_context("8" * 32)

        def interrupt(_request, _budget):
            raise KeyboardInterrupt

        self.service.worker_runner = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.service.sync_classification("519755", request_context=context)

        self.assertTrue(context.budget.cancelled)
        self.assertEqual(self.store.section_status("519755"), {})
        history = context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )
        self.assertIs(history[0].attempt.outcome, SourceAttemptOutcome.CANCELLED)
        context.audit_store.finalize_request(
            context.request_run_id,
            RequestTerminalStatus.CANCELLED,
            self.audit_now,
            ("basic_profile",),
        )

    def test_bounded_healthy_holdings_cache_records_cache_hit_without_workers(self) -> None:
        self.service.sync_profile("519755")
        first_context = self._bounded_context("b" * 32)
        first = self.service.sync_holdings("519755", request_context=first_context)
        self._finalize(first_context, first)
        self.worker.calls.clear()

        second_context = self._bounded_context("c" * 32)
        second = self.service.sync_holdings("519755", request_context=second_context)

        self.assertEqual(self.worker.calls, [])
        self.assertEqual(second.omitted_work, ())
        history = second_context.audit_store.source_attempt_history(
            "eastmoney_f10", "holdings_industries", "fund:519755"
        )
        self.assertEqual(history[0].request_id, "c" * 32)
        self.assertIs(
            history[0].attempt.outcome,
            SourceAttemptOutcome.CACHE_HIT,
        )
        self.assertEqual(
            history[0].attempt.data_as_of,
            history[1].attempt.data_as_of,
        )

    def test_bounded_cache_ignores_future_attempt_lineage(self) -> None:
        self.service.sync_profile("519755")
        first_context = self._bounded_context("51" * 16)
        first = self.service.sync_holdings("519755", request_context=first_context)
        self._finalize(first_context, first)
        older = first_context.audit_store.source_attempt_history(
            "eastmoney_f10", "holdings_industries", "fund:519755"
        )[0]

        future_now = self.audit_now + timedelta(days=1)
        future_budget = RequestBudget.create(
            RequestMode.RAPID,
            request_id="52" * 16,
            monotonic=lambda: 200.0,
            wall_clock=lambda: future_now,
        )
        future_store = DecisionAuditStore(self.repository)
        future_run_id = future_store.begin_request(future_budget)
        registry = SourceRegistryV1()
        future_store.record_source_attempt(
            future_run_id,
            SourceAttempt(
                source_id="eastmoney_f10",
                field_id="holdings_industries",
                subject_key="fund:519755",
                attempt_number=1,
                outcome=SourceAttemptOutcome.SUCCESS,
                started_at=future_now,
                finished_at=future_now,
                data_as_of=future_now,
                error_code=None,
                cooldown_until=None,
                force_actor=None,
                force_reason=None,
                registry_version=registry.version,
                registry_checksum=registry.checksum(),
                response_bytes=1,
            ),
        )
        future_store.finalize_request(
            future_run_id,
            RequestTerminalStatus.COMPLETE,
            future_now,
            (),
        )
        self.worker.calls.clear()

        current_context = self._bounded_context("53" * 16)
        result = self.service.sync_holdings(
            "519755", request_context=current_context
        )

        self.assertEqual(self.worker.calls, [])
        self.assertEqual(result.omitted_work, ())
        current = next(
            record
            for record in current_context.audit_store.source_attempt_history(
                "eastmoney_f10", "holdings_industries", "fund:519755"
            )
            if record.request_id == "53" * 16
        )
        self.assertIs(current.attempt.outcome, SourceAttemptOutcome.CACHE_HIT)
        self.assertEqual(current.attempt.data_as_of, older.attempt.data_as_of)

    def test_bounded_cooldown_skip_ignores_future_attempt_deadline(self) -> None:
        self.worker.failures["basic_profile"] = [
            SourceErrorCode.DNS_FAILURE,
            SourceErrorCode.DNS_FAILURE,
        ]
        failed_context = self._bounded_context("61" * 16)
        failed = self.service.sync_classification(
            "519755", request_context=failed_context
        )
        self._finalize(failed_context, failed)
        older = failed_context.audit_store.source_attempt_history(
            "eastmoney_f10", "identity_active_status", "fund:519755"
        )[0]

        future_now = self.audit_now + timedelta(days=1)
        future_budget = RequestBudget.create(
            RequestMode.RAPID,
            request_id="62" * 16,
            monotonic=lambda: 300.0,
            wall_clock=lambda: future_now,
        )
        future_store = DecisionAuditStore(self.repository)
        future_run_id = future_store.begin_request(future_budget)
        registry = SourceRegistryV1()
        future_store.record_source_attempt(
            future_run_id,
            SourceAttempt(
                source_id="eastmoney_f10",
                field_id="identity_active_status",
                subject_key="fund:519755",
                attempt_number=1,
                outcome=SourceAttemptOutcome.TRANSIENT_FAILURE,
                started_at=future_now,
                finished_at=future_now,
                data_as_of=None,
                error_code=SourceErrorCode.DNS_FAILURE,
                cooldown_until=future_now + timedelta(minutes=30),
                force_actor=None,
                force_reason=None,
                registry_version=registry.version,
                registry_checksum=registry.checksum(),
                response_bytes=0,
            ),
        )
        future_store.finalize_request(
            future_run_id,
            RequestTerminalStatus.PARTIAL,
            future_now,
            (),
        )
        calls_before = len(self.worker.calls)

        current_context = self._bounded_context("63" * 16)
        result = self.service.sync_classification(
            "519755", request_context=current_context
        )

        self.assertEqual(len(self.worker.calls), calls_before)
        self.assertIn("basic_profile", result.omitted_work)
        current = next(
            record
            for record in current_context.audit_store.source_attempt_history(
                "eastmoney_f10", "identity_active_status", "fund:519755"
            )
            if record.request_id == "63" * 16
        )
        self.assertIs(
            current.attempt.outcome,
            SourceAttemptOutcome.SKIPPED_COOLDOWN,
        )
        self.assertEqual(
            current.attempt.cooldown_until,
            older.attempt.cooldown_until,
        )

    def test_bounded_health_alone_cannot_hide_a_missing_business_section(self) -> None:
        self.service.sync_profile("519755")
        first_context = self._bounded_context("d" * 32)
        first = self.service.sync_holdings("519755", request_context=first_context)
        self._finalize(first_context, first)
        self.store.mark_section_failure(
            "519755",
            DocumentKind.INDUSTRY_EXPOSURE,
            "source_unavailable",
            "source_unavailable",
            self.audit_now,
        )
        self.worker.calls.clear()

        second_context = self._bounded_context("e" * 32)
        self.service.sync_holdings("519755", request_context=second_context)

        self.assertEqual(
            [request.field_id for request in self.worker.calls],
            ["quarterly_holdings", "industry_exposure"],
        )

    def test_bounded_profile_cache_does_not_invent_integrity_checks(self) -> None:
        first_context = self._bounded_context("1a" * 16)
        first = self.service.sync_profile("519755", request_context=first_context)
        self._finalize(first_context, first)
        self.worker.calls.clear()

        second_context = self._bounded_context("2a" * 16)
        self.service.sync_profile("519755", request_context=second_context)

        self.assertEqual(
            [request.field_id for request in self.worker.calls],
            [
                "basic_profile",
                "size_history",
                "manager_history",
                "fee_schedule",
                "announcement",
            ],
        )

    def test_profile_sync_isolates_failures_and_maps_announcement_result_key(self) -> None:
        self.client.failures.add(DocumentKind.SIZE_HISTORY)
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = "<p>暂无基金公告</p>"

        result = self.service.sync_profile("519755")

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertEqual(result.sections["size_history"].status, "source_unavailable")
        self.assertEqual(result.sections["announcements"].status, "not_disclosed")
        self.assertNotIn("announcement", result.sections)
        self.assertEqual(
            self.store.section_status("519755")[DocumentKind.ANNOUNCEMENT.value]["state"],
            "not_disclosed",
        )

    def test_classification_sync_fetches_only_basic_profile(self) -> None:
        result = self.service.sync_classification("519755")

        self.assertEqual(tuple(result.sections), ("basic_profile",))
        self.assertEqual(len(self.client.requested_urls), 1)
        self.assertIn("jbgk_519755.html", self.client.requested_urls[0])

    def test_sync_all_keeps_profile_successes_when_holdings_fetch_fails(self) -> None:
        self.client.failures.add(DocumentKind.QUARTERLY_HOLDINGS)
        self.client.overrides[DocumentKind.ANNOUNCEMENT] = "<p>暂无基金公告</p>"

        result = self.service.sync_all("519755")

        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertEqual(
            result.sections["quarterly_holdings"].status,
            "source_unavailable",
        )
        self.assertEqual(result.sections["announcements"].status, "not_disclosed")

    def test_failed_second_holdings_sync_retains_records_and_last_success(self) -> None:
        first = self.service.sync_holdings("519755")
        first_success = first.sections["quarterly_holdings"].last_success_at
        self.assertEqual(first.sections["quarterly_holdings"].records, 3)

        self.client.retrieved_at = datetime(2026, 7, 12, 9, tzinfo=SHANGHAI)
        self.client.failures.add(DocumentKind.QUARTERLY_HOLDINGS)
        self.as_of = datetime(2026, 7, 12, 12, tzinfo=SHANGHAI)
        second = self.service.sync_holdings("519755")

        section = second.sections["quarterly_holdings"]
        self.assertEqual(section.status, "source_unavailable")
        self.assertEqual(section.records, 3)
        self.assertEqual(section.last_success_at, first_success)
        self.assertEqual(
            section.last_attempt_at,
            datetime(2026, 7, 12, 12, tzinfo=SHANGHAI).isoformat(),
        )
        self.assertEqual(len(self.store.load_bundle("519755").holdings), 3)

    def test_identity_conflict_only_blocks_basic_profile_and_is_reported(self) -> None:
        profile = (FIXTURES / "basic_profile.html").read_text("utf-8")
        self.client.overrides[DocumentKind.BASIC_PROFILE] = profile.replace(
            "519755", "519754"
        )

        result = self.service.sync_profile("519755")

        self.assertEqual(result.sections["basic_profile"].status, "source_unavailable")
        self.assertIn("basic_profile:identity_conflict", result.conflicts)
        self.assertEqual(result.sections["manager_history"].status, "success")
        self.assertEqual(result.sections["fee_schedule"].status, "success")
        self.assertIsNone(self.store.load_bundle("519755").identity)

    def test_freshness_has_only_supported_values(self) -> None:
        missing = self.service.section_snapshot("519755", "manager_history")
        self.assertEqual(missing.freshness, "missing")

        result = self.service.sync_all("519755")
        self.assertEqual(result.sections["basic_profile"].freshness, "fresh")
        self.assertEqual(result.sections["quarterly_holdings"].freshness, "fresh")
        self.assertTrue(
            {item.freshness for item in result.sections.values()}
            <= {"fresh", "stale", "missing", "unknown"}
        )

        self.client.overrides[DocumentKind.QUARTERLY_HOLDINGS] = "<p>暂无持仓数据</p>"
        empty = self.service.sync_holdings("519755")
        self.assertEqual(empty.sections["quarterly_holdings"].freshness, "unknown")

    def test_quarterly_freshness_uses_expected_report_period(self) -> None:
        self.service.sync_holdings("519755")

        self.as_of = datetime(2026, 8, 6, 12, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "fresh",
        )
        self.as_of = datetime(2026, 8, 7, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "fresh",
        )

        self.as_of = datetime(2026, 11, 7, 0, tzinfo=SHANGHAI)
        self.assertEqual(
            self.service.section_snapshot("519755", "quarterly_holdings").freshness,
            "stale",
        )


class ExpectedReportPeriodTest(unittest.TestCase):
    def test_announcement_titles_map_quarter_four_and_annual_to_same_period(self) -> None:
        expected = date(2025, 12, 31)
        titles = (
            "示例基金2025年第4季度报告",
            "示例基金2025年第四季度报告",
            "示例基金2025年年度报告",
        )
        self.assertEqual([announcement_report_period(title) for title in titles], [expected] * 3)

    def test_four_operational_deadlines_and_pre_april_window(self) -> None:
        cases = {
            date(2026, 4, 6): date(2025, 9, 30),
            date(2026, 4, 7): date(2025, 12, 31),
            date(2026, 5, 6): date(2025, 12, 31),
            date(2026, 5, 7): date(2026, 3, 31),
            date(2026, 8, 6): date(2026, 3, 31),
            date(2026, 8, 7): date(2026, 6, 30),
            date(2026, 11, 6): date(2026, 6, 30),
            date(2026, 11, 7): date(2026, 9, 30),
        }
        for as_of, expected in cases.items():
            with self.subTest(as_of=as_of):
                self.assertEqual(expected_report_period(as_of), expected)


if __name__ == "__main__":
    unittest.main()
