from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import (
    ParserProvenance,
    ParseRunOutcome,
    RefreshOutcome,
    legacy_parser_provenance,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import (
    OfficialDocumentCandidate,
    OfficialDocumentError,
    RetrievedArtifact,
)
from kunjin.funds.risk.engine import (
    ClassificationEvidence,
    classification_input_manifest_v1,
    classification_input_manifest_v2,
    classify_fund,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.funds.risk.models import (
    EvidenceFreshness,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    MandateFact,
    ProductFamily,
)
from kunjin.funds.risk.parsers import (
    PARSER_VERSION,
    ParsedMandateFact,
    ParsedRiskDocument,
    fact_fingerprint,
)
from kunjin.funds.risk.policy import ClassificationPolicyV1
from kunjin.funds.risk.selection import select_current_candidates
from kunjin.funds.risk.store import (
    CurrentDocumentSelectionSnapshot,
    FundRiskStore,
    RiskStoreError,
    StoredDocumentSelectionManifest,
)
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 13, 8, tzinfo=timezone.utc)


def historical_native_provenance(parser_version: str = "2") -> ParserProvenance:
    payload = {
        "contract_version": "native-v1",
        "converter_kind": "none",
        "parser_version": parser_version,
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return ParserProvenance(
        parser_version=parser_version,
        converter_kind="none",
        canonical_json=canonical,
        provenance_checksum=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
    )


class FundRiskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.repository = Repository(root / "kunjin.db")
        self.repository.migrate()
        self.store = FundRiskStore(self.repository)
        self.document_path = root / "document.html"
        self.document_path.write_bytes(b"<html>synthetic public evidence</html>")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _artifact(self, fund_code: str = "000001") -> RetrievedArtifact:
        raw = self.document_path.read_bytes()
        candidate = OfficialDocumentCandidate(
            fund_code=fund_code,
            document_kind=DocumentKind.PROSPECTUS,
            title="Synthetic public prospectus",
            url=f"https://www.fund001.com/synthetic/{fund_code}/prospectus",
            publisher="交银施罗德基金管理有限公司",
            published_at=NOW,
            source_tier=1,
        )
        return RetrievedArtifact(
            candidate=candidate,
            final_url=candidate.url,
            retrieved_at=NOW,
            content_type="text/html; charset=utf-8",
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            managed_path=self.document_path,
        )

    def _parsed(self, fund_code: str = "000001") -> ParsedRiskDocument:
        fields = {
            "fact_kind": "legal_product_type",
            "normalized_value": "ordinary_bond",
            "unit": None,
            "page_number": None,
            "section_name": "Investment scope",
            "source_excerpt": "Synthetic public investment scope",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        fact = ParsedMandateFact(
            **fields,
            fact_fingerprint=fact_fingerprint(**fields),
        )
        return ParsedRiskDocument(
            artifact=self._artifact(fund_code),
            facts=(fact,),
            warnings=(),
            conflicts=(),
        )

    def _second_fact(self) -> ParsedMandateFact:
        fields = {
            "fact_kind": "stock_exposure_max_percent",
            "normalized_value": Decimal("0"),
            "unit": "percent",
            "page_number": 1,
            "section_name": "Investment scope",
            "source_excerpt": "Synthetic public stock exposure ceiling",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        return ParsedMandateFact(
            **fields,
            fact_fingerprint=fact_fingerprint(**fields),
        )

    def _document_variant(
        self,
        *,
        kind: DocumentKind,
        marker: str,
        published_at: Optional[datetime],
        retrieved_at: datetime,
    ) -> ParsedRiskDocument:
        parsed = self._parsed()
        url = f"https://www.fund001.com/synthetic/000001/{marker}"
        candidate = replace(
            parsed.artifact.candidate,
            document_kind=kind,
            title=f"Synthetic {marker}",
            url=url,
            published_at=published_at,
        )
        return replace(
            parsed,
            artifact=replace(
                parsed.artifact,
                candidate=candidate,
                final_url=url,
                retrieved_at=retrieved_at,
            ),
        )

    def _classification_inputs(
        self,
        *,
        classified_at: datetime = NOW,
        external_marker: str = "a",
    ):
        parsed = self._parsed()
        refresh_id = self.store.begin_document_refresh("000001", classified_at)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(parsed.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, classified_at)
        record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=classified_at,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            classified_at + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        artifact = record.artifact
        stored = record.facts[0]
        normalized = stored.normalized_value
        mandate = MandateFact(
            fund_code=stored.fund_code,
            fact_kind=stored.fact_kind,
            normalized_value=normalized,
            unit=stored.unit,
            source_document_id=stored.source_document_id,
            page_number=stored.page_number,
            section_name=stored.section_name,
            source_excerpt=stored.source_excerpt,
            effective_from=stored.effective_from,
            effective_to=stored.effective_to,
            confidence_state=stored.confidence_state,
            parser_version=stored.parser_version,
            fact_fingerprint=stored.fact_fingerprint,
        )
        freshness = EvidenceFreshness(
            section="legal_scope",
            source_document_id=artifact.id,
            state=FreshnessState.CURRENT,
            observed_at=classified_at - timedelta(days=1),
            valid_until=classified_at + timedelta(days=30),
            critical=True,
        )
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=(mandate,),
            benchmark_facts=(),
            report_facts=(),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(("identity", external_marker * 64),),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(freshness,),
            document_ids=(artifact.id,),
            fact_ids=(stored.id,),
            parse_result_ids=(stored.parse_result_id,),
            parser_provenance_checksums=(native_parser_provenance().provenance_checksum,),
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        policy = ClassificationPolicyV1()
        classification = classify_fund(evidence, policy, classified_at)
        return classification, evidence, policy

    def _failure(self) -> SafeDocumentFailure:
        return SafeDocumentFailure(
            public_code="official_document_parse_failed",
            stage=DocumentFailureStage.PARSER,
            reason_code=DocumentFailureReason.PARSER_FORMAT_INVALID,
        )

    def _selection_plan(self, refresh_id: int, fund_code: str = "000001"):
        annual = replace(
            self._parsed(fund_code).artifact.candidate,
            document_kind=DocumentKind.ANNUAL_REPORT,
            title="Synthetic annual report",
            url=f"https://www.fund001.com/synthetic/{fund_code}/annual",
            published_at=NOW - timedelta(days=2),
        )
        quarter = replace(
            annual,
            document_kind=DocumentKind.QUARTERLY_REPORT,
            title="Synthetic quarterly report",
            url=f"https://www.fund001.com/synthetic/{fund_code}/quarter",
            published_at=NOW - timedelta(days=1),
        )
        return select_current_candidates(
            fund_code,
            refresh_run_id=refresh_id,
            candidates=(annual, quarter),
        )

    def _publish_current(
        self,
        parsed: Optional[ParsedRiskDocument] = None,
        *,
        outcome: RefreshOutcome = RefreshOutcome.SUCCESS,
        started_at: datetime = NOW,
    ):
        parsed = self._parsed() if parsed is None else parsed
        refresh_id = self.store.begin_document_refresh(
            parsed.artifact.candidate.fund_code,
            started_at,
        )
        record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=started_at,
        )
        self.store.complete_document_refresh(
            refresh_id,
            outcome,
            started_at + timedelta(seconds=1),
        )
        return refresh_id, record

    def _selection_bound_inputs(
        self,
        *,
        classified_at: datetime = NOW,
        outcome: RefreshOutcome = RefreshOutcome.SUCCESS,
    ):
        parsed = self._parsed()
        refresh_id = self.store.begin_document_refresh("000001", classified_at)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(parsed.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, classified_at)
        record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=classified_at,
        )
        self.store.complete_document_refresh(
            refresh_id,
            outcome,
            classified_at + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        stored = record.facts[0]
        mandate = MandateFact(
            fund_code=stored.fund_code,
            fact_kind=stored.fact_kind,
            normalized_value=stored.normalized_value,
            unit=stored.unit,
            source_document_id=stored.source_document_id,
            page_number=stored.page_number,
            section_name=stored.section_name,
            source_excerpt=stored.source_excerpt,
            effective_from=stored.effective_from,
            effective_to=stored.effective_to,
            confidence_state=stored.confidence_state,
            parser_version=stored.parser_version,
            fact_fingerprint=stored.fact_fingerprint,
        )
        freshness = EvidenceFreshness(
            section="legal_scope",
            source_document_id=record.artifact.id,
            state=FreshnessState.CURRENT,
            observed_at=classified_at - timedelta(days=1),
            valid_until=classified_at + timedelta(days=30),
            critical=True,
        )
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=(mandate,),
            benchmark_facts=(),
            report_facts=(),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(freshness,),
            document_ids=(record.artifact.id,),
            fact_ids=(stored.id,),
            parse_result_ids=(record.parse_result.id,),
            parser_provenance_checksums=(record.provenance.provenance_checksum,),
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        policy = ClassificationPolicyV1()
        return classify_fund(evidence, policy, classified_at), evidence, policy, snapshot

    def _insert_historical_classification(
        self,
        classification,
        evidence: ClassificationEvidence,
        policy: ClassificationPolicyV1,
        *,
        version: int,
    ) -> int:
        self.store.ensure_policy(policy)
        manifest = (
            classification_input_manifest_v1(evidence, policy, classification.classified_at)
            if version == 1
            else classification_input_manifest_v2(evidence, policy, classification.classified_at)
        )
        manifest_json = json.dumps(
            manifest,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
        historical = replace(classification, input_fingerprint=fingerprint)
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                "INSERT INTO fund_risk_classifications("
                "fund_code, policy_version, input_fingerprint, input_manifest_json, "
                "product_family, risk_bucket, portfolio_role, evidence_status, "
                "evidence_tags_json, reason_codes_json, missing_evidence_json, conflicts_json, "
                "evidence_document_ids_json, evidence_fact_ids_json, freshness_json, "
                "classified_at, valid_until, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    historical.fund_code,
                    historical.policy_version,
                    historical.input_fingerprint,
                    manifest_json,
                    historical.product_family.value,
                    historical.risk_bucket.value,
                    historical.portfolio_role.value,
                    historical.evidence_status.value,
                    json.dumps(list(historical.evidence_tags), separators=(",", ":")),
                    json.dumps(list(historical.reason_codes), separators=(",", ":")),
                    json.dumps(list(historical.missing_evidence), separators=(",", ":")),
                    json.dumps(list(historical.conflicts), separators=(",", ":")),
                    json.dumps(list(historical.evidence_document_ids), separators=(",", ":")),
                    json.dumps(list(historical.evidence_fact_ids), separators=(",", ":")),
                    json.dumps(manifest["freshness"], separators=(",", ":"), sort_keys=True),
                    historical.classified_at.isoformat(),
                    historical.valid_until.isoformat(),
                    historical.classified_at.isoformat(),
                ),
            )
        return cursor.lastrowid

    def _bind_evidence_to_selection(
        self,
        evidence: ClassificationEvidence,
        parsed: ParsedRiskDocument,
        *,
        at: datetime = NOW,
    ) -> ClassificationEvidence:
        refresh_id = self.store.begin_document_refresh(evidence.fund_code, at)
        plan = select_current_candidates(
            evidence.fund_code,
            refresh_run_id=refresh_id,
            candidates=(parsed.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, at)
        record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=at,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            at + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot(evidence.fund_code)
        assert snapshot is not None
        self.assertEqual(evidence.document_ids, (record.artifact.id,))
        self.assertEqual(evidence.parse_result_ids, (record.parse_result.id,))
        return replace(
            evidence,
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )

    def test_refresh_start_and_completion_are_independent_exact_events(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        with self.repository.connect() as connection:
            started = connection.execute(
                "SELECT * FROM fund_document_refresh_runs WHERE id = ?",
                (refresh_id,),
            ).fetchone()
            completion = connection.execute(
                "SELECT * FROM fund_document_refresh_completions WHERE refresh_run_id = ?",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(started["fund_code"], "000001")
        self.assertIsNone(completion)

        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.EMPTY,
            NOW + timedelta(seconds=1),
        )
        with self.repository.connect() as connection:
            completion = connection.execute(
                "SELECT * FROM fund_document_refresh_completions WHERE refresh_run_id = ?",
                (refresh_id,),
            ).fetchone()
        self.assertEqual(completion["outcome"], "empty")
        with self.assertRaises(RiskStoreError):
            self.store.complete_document_refresh(
                refresh_id,
                RefreshOutcome.EMPTY,
                NOW + timedelta(seconds=2),
            )

    def test_current_documents_fail_closed_on_latest_refresh_state(self) -> None:
        _, published = self._publish_current()
        checksum = native_parser_provenance().provenance_checksum
        self.assertEqual(
            self.store.current_parsed_documents("000001", (checksum,)),
            (published,),
        )

        self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        self.assertEqual(self.store.current_parsed_documents("000001", (checksum,)), ())

    def test_selection_snapshot_authenticates_manifest_runs_and_reason_projection(self) -> None:
        _, evidence, _, snapshot = self._selection_bound_inputs()

        self.assertIs(type(snapshot), CurrentDocumentSelectionSnapshot)
        self.assertEqual(snapshot.selection.refresh_run_id, evidence.document_refresh_run_id)
        self.assertEqual(
            snapshot.selection.selection_policy_checksum,
            evidence.selection_policy_checksum,
        )
        self.assertEqual(
            snapshot.selection.selection_checksum,
            evidence.selection_manifest_checksum,
        )
        self.assertEqual(
            snapshot.candidate_run_snapshot_checksum,
            evidence.candidate_run_snapshot_checksum,
        )
        self.assertEqual(
            snapshot.selection_reason_codes,
            ("current_periodic_candidate_missing",),
        )
        self.assertEqual(snapshot.selected_periodic_records, ())
        self.assertEqual(len(snapshot.nonperiodic_successful_records), 1)
        self.assertEqual(len(snapshot.candidate_runs), 1)

        exact = self.store.document_selection_snapshot_for_refresh(
            "000001",
            snapshot.selection.refresh_run_id,
        )
        self.assertEqual(exact, snapshot)

    def test_current_selection_snapshot_never_falls_back_past_absolute_latest_refresh(self) -> None:
        _, _, _, first = self._selection_bound_inputs()
        self.assertEqual(
            self.store.current_document_selection_snapshot("000001"),
            first,
        )

        states = (
            ("running", None),
            ("failed", RefreshOutcome.FAILED),
            ("empty", RefreshOutcome.EMPTY),
        )
        for index, (label, outcome) in enumerate(states, start=1):
            with self.subTest(label=label):
                refresh_id = self.store.begin_document_refresh(
                    "000001",
                    NOW + timedelta(minutes=index),
                )
                if outcome is RefreshOutcome.FAILED:
                    self.store.complete_document_refresh(
                        refresh_id,
                        outcome,
                        NOW + timedelta(minutes=index, seconds=1),
                        failure=self._failure(),
                    )
                elif outcome is not None:
                    self.store.complete_document_refresh(
                        refresh_id,
                        outcome,
                        NOW + timedelta(minutes=index, seconds=1),
                    )
                self.assertIsNone(
                    self.store.current_document_selection_snapshot("000001")
                )

    def test_selection_snapshot_binds_selected_failure_without_old_fallback(self) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="selected-failure",
            published_at=NOW,
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(annual.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, NOW)
        self.store.publish_candidate_failure(
            refresh_id=refresh_id,
            candidate=annual.artifact.candidate,
            failure=self._failure(),
            attempted_at=NOW,
            artifact=None,
            provenance=None,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(seconds=1),
        )

        snapshot = self.store.current_document_selection_snapshot("000001")

        assert snapshot is not None
        self.assertEqual(snapshot.selected_periodic_records, ())
        self.assertEqual(snapshot.nonperiodic_successful_records, ())
        self.assertEqual(snapshot.candidate_runs[0].outcome.value, "failed")
        self.assertEqual(snapshot.candidate_runs[0].failure, self._failure())
        self.assertEqual(
            snapshot.selection_reason_codes,
            ("current_periodic_candidate_missing",),
        )

    def test_selection_snapshot_uses_exact_periodic_success_and_allows_same_refresh_nonperiodic(
        self,
    ) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="annual-success",
            published_at=NOW,
            retrieved_at=NOW,
        )
        first = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="prospectus-first",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW,
        )
        second = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="prospectus-second",
            published_at=NOW - timedelta(days=1),
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(
                annual.artifact.candidate,
                first.artifact.candidate,
                second.artifact.candidate,
            ),
        )
        self.store.publish_document_selection(plan, NOW)
        records = tuple(
            self.store.publish_candidate_success(
                refresh_id=refresh_id,
                candidate=parsed.artifact.candidate,
                parsed=parsed,
                provenance=native_parser_provenance(),
                parser_input_sha256=parsed.artifact.sha256,
                attempted_at=NOW,
            )
            for parsed in (annual, first, second)
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )

        snapshot = self.store.current_document_selection_snapshot("000001")

        assert snapshot is not None
        self.assertEqual(snapshot.selected_periodic_records, (records[0],))
        self.assertEqual(snapshot.nonperiodic_successful_records, records[1:])

    def test_selection_snapshot_rejects_missing_or_unselected_periodic_runs(self) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="annual-missing-run",
            published_at=NOW,
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(annual.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, NOW)
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(RiskStoreError, "selected periodic"):
            self.store.current_document_selection_snapshot("000001")

        conflicted_a = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="annual-conflict-a",
            published_at=NOW + timedelta(minutes=1),
            retrieved_at=NOW,
        )
        conflicted_b = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="annual-conflict-b",
            published_at=NOW + timedelta(minutes=1),
            retrieved_at=NOW,
        )
        conflict_refresh = self.store.begin_document_refresh(
            "000001", NOW + timedelta(minutes=1)
        )
        conflict_plan = select_current_candidates(
            "000001",
            refresh_run_id=conflict_refresh,
            candidates=(conflicted_a.artifact.candidate, conflicted_b.artifact.candidate),
        )
        self.store.publish_document_selection(conflict_plan, NOW + timedelta(minutes=1))
        self.store.publish_candidate_failure(
            refresh_id=conflict_refresh,
            candidate=conflicted_a.artifact.candidate,
            failure=self._failure(),
            attempted_at=NOW + timedelta(minutes=1),
            artifact=None,
            provenance=None,
        )
        self.store.complete_document_refresh(
            conflict_refresh,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(minutes=1, seconds=1),
        )
        with self.assertRaisesRegex(RiskStoreError, "unselected periodic"):
            self.store.current_document_selection_snapshot("000001")

    def test_selection_snapshot_conflict_has_no_run_and_exact_reason_projection(self) -> None:
        first = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="conflict-a",
            published_at=NOW,
            retrieved_at=NOW,
        )
        second = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="conflict-b",
            published_at=NOW,
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(first.artifact.candidate, second.artifact.candidate),
        )
        self.store.publish_document_selection(plan, NOW)
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )

        snapshot = self.store.current_document_selection_snapshot("000001")

        assert snapshot is not None
        self.assertEqual(snapshot.candidate_runs, ())
        self.assertEqual(
            snapshot.selection_reason_codes,
            (
                "current_periodic_candidate_conflict",
                "current_periodic_candidate_missing",
            ),
        )

    def test_selection_snapshot_rejects_failed_candidate_parse_run_mismatch(self) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="parse-failure",
            published_at=NOW,
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(annual.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, NOW)
        run = self.store.publish_candidate_parse_failure(
            refresh_id=refresh_id,
            candidate=annual.artifact.candidate,
            artifact=annual.artifact,
            provenance=native_parser_provenance(),
            failure=self._failure(),
            attempted_at=NOW,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(seconds=1),
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_parse_run_no_update")
            connection.execute(
                "UPDATE fund_document_parse_runs SET public_error_code = ? WHERE id = ?",
                ("official_document_invalid", run.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "candidate and parse failure"):
            self.store.current_document_selection_snapshot("000001")

    def test_new_classification_save_requires_v3_and_historical_v1_v2_remain_readable(self) -> None:
        _, evidence, policy = self._classification_inputs()
        historical_evidence = replace(
            evidence,
            document_refresh_run_id=None,
            selection_policy_checksum=None,
            selection_manifest_checksum=None,
            candidate_run_snapshot_checksum=None,
            selection_reason_codes=(),
        )
        historical_classification = classify_fund(historical_evidence, policy, NOW)
        with self.assertRaisesRegex(RiskStoreError, "manifest v3"):
            self.store.save_classification(
                historical_classification,
                historical_evidence,
                policy,
            )

        for version in (1, 2):
            with self.subTest(version=version):
                classification_id = self._insert_historical_classification(
                    historical_classification,
                    historical_evidence,
                    policy,
                    version=version,
                )
                bound = self.store.classification_evidence("000001", classification_id)
                assert bound is not None
                self.assertEqual(
                    self.store.classification_history("000001")[0].id,
                    classification_id,
                )
                current = self.store.current_classification("000001")
                assert current is not None
                self.assertEqual(current.id, classification_id)
                self.assertIsNone(bound.evidence.document_refresh_run_id)
                self.assertIsNone(bound.evidence.selection_policy_checksum)
                self.assertIsNone(bound.evidence.selection_manifest_checksum)
                self.assertIsNone(bound.evidence.candidate_run_snapshot_checksum)
                self.assertEqual(bound.evidence.selection_reason_codes, ())

    def test_v3_save_and_historical_readback_authenticate_exact_refresh(self) -> None:
        classification, evidence, policy, snapshot = self._selection_bound_inputs()

        stored = self.store.save_classification(classification, evidence, policy)
        later = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))

        bound = self.store.classification_evidence("000001", stored.id)

        assert bound is not None
        self.assertEqual(bound.evidence, evidence)
        self.assertEqual(
            self.store.document_selection_snapshot_for_refresh(
                "000001", snapshot.selection.refresh_run_id
            ),
            snapshot,
        )
        self.assertNotEqual(later, snapshot.selection.refresh_run_id)

    def test_v3_save_revalidates_absolute_latest_refresh_inside_write_transaction(self) -> None:
        classification, evidence, policy, _ = self._selection_bound_inputs()
        self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))

        with self.assertRaisesRegex(RiskStoreError, "current selection"):
            self.store.save_classification(classification, evidence, policy)

        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM fund_risk_classifications"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_v3_save_rejects_every_newer_refresh_terminal_state(self) -> None:
        states = (
            None,
            RefreshOutcome.FAILED,
            RefreshOutcome.EMPTY,
            RefreshOutcome.PARTIAL,
            RefreshOutcome.SUCCESS,
        )
        for index, outcome in enumerate(states, start=1):
            with self.subTest(outcome=None if outcome is None else outcome.value):
                temporary_directory = tempfile.TemporaryDirectory()
                try:
                    repository = Repository(Path(temporary_directory.name) / "kunjin.db")
                    repository.migrate()
                    original_store, original_repository = self.store, self.repository
                    original_path = self.document_path
                    self.store, self.repository = FundRiskStore(repository), repository
                    self.document_path = Path(temporary_directory.name) / "document.html"
                    self.document_path.write_bytes(b"<html>synthetic public evidence</html>")
                    classification, evidence, policy, _ = self._selection_bound_inputs()
                    newer = self.store.begin_document_refresh(
                        "000001", NOW + timedelta(minutes=index)
                    )
                    if outcome is RefreshOutcome.FAILED:
                        self.store.complete_document_refresh(
                            newer,
                            outcome,
                            NOW + timedelta(minutes=index, seconds=1),
                            failure=self._failure(),
                        )
                    elif outcome is not None:
                        self.store.complete_document_refresh(
                            newer,
                            outcome,
                            NOW + timedelta(minutes=index, seconds=1),
                        )
                    with self.assertRaisesRegex(RiskStoreError, "current selection"):
                        self.store.save_classification(classification, evidence, policy)
                    with repository.connect() as connection:
                        count = connection.execute(
                            "SELECT COUNT(*) FROM fund_risk_classifications"
                        ).fetchone()[0]
                    self.assertEqual(count, 0)
                finally:
                    self.store, self.repository = original_store, original_repository
                    self.document_path = original_path
                    temporary_directory.cleanup()

    def test_v3_save_rejects_candidate_run_appended_after_assembly(self) -> None:
        classification, evidence, policy, snapshot = self._selection_bound_inputs()
        with self.repository.connect() as connection, connection:
            connection.execute(
                "INSERT INTO fund_document_candidate_runs("
                "refresh_run_id, candidate_fingerprint, fund_code, document_kind, url, "
                "published_at, outcome, source_document_id, parse_run_id, public_error_code, "
                "failure_stage, failure_reason, created_at"
                ") VALUES (?, ?, ?, ?, ?, NULL, 'failed', NULL, NULL, ?, ?, ?, ?)",
                (
                    snapshot.selection.refresh_run_id,
                    "f" * 64,
                    "000001",
                    DocumentKind.PRODUCT_SUMMARY.value,
                    "https://www.fund001.com/synthetic/000001/appended",
                    self._failure().public_code,
                    self._failure().stage.value,
                    self._failure().reason_code.value,
                    (NOW + timedelta(minutes=1)).isoformat(),
                ),
            )

        with self.assertRaisesRegex(RiskStoreError, "selection snapshot"):
            self.store.save_classification(classification, evidence, policy)
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM fund_risk_classifications"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_v3_save_rejects_each_selection_binding_change(self) -> None:
        _, evidence, policy, _ = self._selection_bound_inputs()
        changes = {
            "refresh": {"document_refresh_run_id": evidence.document_refresh_run_id + 1},
            "policy": {"selection_policy_checksum": "a" * 64},
            "manifest": {"selection_manifest_checksum": "b" * 64},
            "candidate_runs": {"candidate_run_snapshot_checksum": "c" * 64},
            "reasons": {"selection_reason_codes": ()},
        }
        for label, values in changes.items():
            with self.subTest(label=label):
                changed = replace(evidence, **values)
                classification = classify_fund(changed, policy, NOW)
                with self.assertRaisesRegex(
                    RiskStoreError,
                    "current selection|selection snapshot",
                ):
                    self.store.save_classification(classification, changed, policy)

    def test_v3_save_rejects_empty_official_evidence_when_projection_is_nonempty(self) -> None:
        _, evidence, policy, _ = self._selection_bound_inputs()
        emptied = replace(
            evidence,
            legal_facts=(),
            benchmark_facts=(),
            report_facts=(),
            freshness=(),
            document_ids=(),
            fact_ids=(),
            parse_result_ids=(),
            parser_provenance_checksums=(),
        )
        classification = classify_fund(emptied, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "evidence projection"):
            self.store.save_classification(classification, emptied, policy)
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM fund_risk_classifications"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_v3_save_rejects_misgrouped_official_facts(self) -> None:
        _, evidence, policy, _ = self._selection_bound_inputs()
        misgrouped = replace(
            evidence,
            legal_facts=(),
            report_facts=evidence.legal_facts,
        )
        classification = classify_fund(misgrouped, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "evidence projection"):
            self.store.save_classification(classification, misgrouped, policy)
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM fund_risk_classifications"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_v3_save_rejects_an_authenticated_empty_projection(self) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="failed-only-annual",
            published_at=NOW - timedelta(days=1),
            retrieved_at=NOW,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(annual.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, NOW)
        self.store.publish_candidate_failure(
            refresh_id=refresh_id,
            candidate=annual.artifact.candidate,
            failure=self._failure(),
            attempted_at=NOW,
            artifact=None,
            provenance=None,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=(),
            benchmark_facts=(),
            report_facts=(),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(),
            document_ids=(),
            fact_ids=(),
            parse_result_ids=(),
            parser_provenance_checksums=(),
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        policy = ClassificationPolicyV1()

        with self.assertRaisesRegex(RiskStoreError, "projection is unavailable"):
            self.store.save_classification(classify_fund(evidence, policy, NOW), evidence, policy)

    def test_selected_parse_failure_can_reuse_an_artifact_from_an_older_success(self) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="reused-annual",
            published_at=NOW - timedelta(days=1),
            retrieved_at=NOW,
        )
        first_refresh = self.store.begin_document_refresh("000001", NOW)
        self.store.publish_document_selection(
            select_current_candidates(
                "000001",
                refresh_run_id=first_refresh,
                candidates=(annual.artifact.candidate,),
            ),
            NOW,
        )
        first = self.store.publish_candidate_success(
            refresh_id=first_refresh,
            candidate=annual.artifact.candidate,
            parsed=annual,
            provenance=native_parser_provenance(),
            parser_input_sha256=annual.artifact.sha256,
            attempted_at=NOW,
        )
        self.store.complete_document_refresh(
            first_refresh,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )

        second_refresh = self.store.begin_document_refresh(
            "000001", NOW + timedelta(minutes=1)
        )
        self.store.publish_document_selection(
            select_current_candidates(
                "000001",
                refresh_run_id=second_refresh,
                candidates=(annual.artifact.candidate,),
            ),
            NOW + timedelta(minutes=1),
        )
        self.store.publish_candidate_parse_failure(
            refresh_id=second_refresh,
            candidate=annual.artifact.candidate,
            artifact=annual.artifact,
            provenance=native_parser_provenance(),
            failure=self._failure(),
            attempted_at=NOW + timedelta(minutes=1),
        )
        self.store.complete_document_refresh(
            second_refresh,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(minutes=1, seconds=1),
        )

        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        self.assertEqual(snapshot.selection.refresh_run_id, second_refresh)
        self.assertEqual(snapshot.selected_periodic_records, ())
        self.assertEqual(len(snapshot.candidate_runs), 1)
        self.assertIs(snapshot.candidate_runs[0].outcome, ParseRunOutcome.FAILED)
        self.assertEqual(snapshot.candidate_runs[0].source_document_id, first.artifact.id)

    def test_v3_save_rejects_older_periodic_report_instead_of_current_fact_projection(
        self,
    ) -> None:
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="2025-annual",
            published_at=NOW - timedelta(days=30),
            retrieved_at=NOW,
        )
        annual = replace(
            annual,
            artifact=replace(
                annual.artifact,
                candidate=replace(annual.artifact.candidate, title="2025年年度报告"),
            ),
        )
        quarter = self._document_variant(
            kind=DocumentKind.QUARTERLY_REPORT,
            marker="2026-q1",
            published_at=NOW - timedelta(days=1),
            retrieved_at=NOW,
        )
        quarter = replace(
            quarter,
            artifact=replace(
                quarter.artifact,
                candidate=replace(quarter.artifact.candidate, title="2026年第1季度报告"),
            ),
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(annual.artifact.candidate, quarter.artifact.candidate),
        )
        self.store.publish_document_selection(plan, NOW)
        annual_record, _ = tuple(
            self.store.publish_candidate_success(
                refresh_id=refresh_id,
                candidate=parsed.artifact.candidate,
                parsed=parsed,
                provenance=native_parser_provenance(),
                parser_input_sha256=parsed.artifact.sha256,
                attempted_at=NOW,
            )
            for parsed in (annual, quarter)
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        stored = annual_record.facts[0]
        report_fact = MandateFact(
            fund_code=stored.fund_code,
            fact_kind=stored.fact_kind,
            normalized_value=stored.normalized_value,
            unit=stored.unit,
            source_document_id=stored.source_document_id,
            page_number=stored.page_number,
            section_name=stored.section_name,
            source_excerpt=stored.source_excerpt,
            effective_from=stored.effective_from,
            effective_to=stored.effective_to,
            confidence_state=stored.confidence_state,
            parser_version=stored.parser_version,
            fact_fingerprint=stored.fact_fingerprint,
        )
        freshness = EvidenceFreshness(
            section=DocumentKind.ANNUAL_REPORT.value,
            source_document_id=annual_record.artifact.id,
            state=FreshnessState.CURRENT,
            observed_at=annual_record.artifact.published_at,
            valid_until=NOW + timedelta(days=30),
            critical=True,
        )
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=(),
            benchmark_facts=(),
            report_facts=(report_fact,),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(freshness,),
            document_ids=(annual_record.artifact.id,),
            fact_ids=(stored.id,),
            parse_result_ids=(annual_record.parse_result.id,),
            parser_provenance_checksums=(annual_record.provenance.provenance_checksum,),
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        policy = ClassificationPolicyV1()
        classification = classify_fund(evidence, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "evidence projection"):
            self.store.save_classification(classification, evidence, policy)

    def test_v3_readback_rejects_selection_and_candidate_run_tampering(self) -> None:
        classification, evidence, policy, _ = self._selection_bound_inputs()
        stored = self.store.save_classification(classification, evidence, policy)

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_candidate_run_no_update")
            connection.execute(
                "UPDATE fund_document_candidate_runs SET created_at = ?",
                ((NOW + timedelta(minutes=2)).isoformat(),),
            )

        with self.assertRaisesRegex(RiskStoreError, "selection snapshot"):
            self.store.classification_history("000001")
        with self.assertRaisesRegex(RiskStoreError, "selection snapshot"):
            self.store.classification_evidence("000001", stored.id)

    def test_v3_readback_rejects_each_manifest_selection_field_change(self) -> None:
        classification, evidence, policy, _ = self._selection_bound_inputs()
        stored = self.store.save_classification(classification, evidence, policy)
        original_manifest = stored.input_manifest_json
        original_fingerprint = stored.input_fingerprint
        changes = {
            "document_refresh_run_id": evidence.document_refresh_run_id + 1,
            "selection_policy_checksum": "a" * 64,
            "selection_manifest_checksum": "b" * 64,
            "candidate_run_snapshot_checksum": "c" * 64,
            "selection_reason_codes": [],
        }
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
        for field, value in changes.items():
            with self.subTest(field=field):
                manifest = json.loads(original_manifest)
                manifest[field] = value
                manifest_json = json.dumps(
                    manifest,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                fingerprint = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        "UPDATE fund_risk_classifications SET input_manifest_json = ?, "
                        "input_fingerprint = ? WHERE id = ?",
                        (manifest_json, fingerprint, stored.id),
                    )
                with self.assertRaises(RiskStoreError):
                    self.store.classification_history("000001")
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        "UPDATE fund_risk_classifications SET input_manifest_json = ?, "
                        "input_fingerprint = ? WHERE id = ?",
                        (original_manifest, original_fingerprint, stored.id),
                    )

    def test_v3_readback_rejects_changed_safe_candidate_failure(self) -> None:
        _, evidence, policy, snapshot = self._selection_bound_inputs()
        with self.repository.connect() as connection, connection:
            connection.execute(
                "INSERT INTO fund_document_candidate_runs("
                "refresh_run_id, candidate_fingerprint, fund_code, document_kind, url, "
                "published_at, outcome, source_document_id, parse_run_id, public_error_code, "
                "failure_stage, failure_reason, created_at"
                ") VALUES (?, ?, ?, ?, ?, NULL, 'failed', NULL, NULL, ?, ?, ?, ?)",
                (
                    snapshot.selection.refresh_run_id,
                    "e" * 64,
                    "000001",
                    DocumentKind.PRODUCT_SUMMARY.value,
                    "https://www.fund001.com/synthetic/000001/failed-summary",
                    self._failure().public_code,
                    self._failure().stage.value,
                    self._failure().reason_code.value,
                    NOW.isoformat(),
                ),
            )
        rebound = self.store.current_document_selection_snapshot("000001")
        assert rebound is not None
        evidence = replace(
            evidence,
            candidate_run_snapshot_checksum=rebound.candidate_run_snapshot_checksum,
        )
        classification = classify_fund(evidence, policy, NOW)
        self.store.save_classification(classification, evidence, policy)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_candidate_run_no_update")
            connection.execute(
                "UPDATE fund_document_candidate_runs SET public_error_code = ?, "
                "failure_stage = ?, failure_reason = ? WHERE candidate_fingerprint = ?",
                (
                    "official_document_invalid",
                    DocumentFailureStage.UNSPECIFIED.value,
                    DocumentFailureReason.UNSPECIFIED_FAILURE.value,
                    "e" * 64,
                ),
            )

        with self.assertRaisesRegex(RiskStoreError, "selection snapshot"):
            self.store.classification_history("000001")

    def test_latest_refresh_id_blocks_old_success_when_clock_moves_backward(self) -> None:
        self._publish_current(started_at=NOW)
        checksum = native_parser_provenance().provenance_checksum

        self.store.begin_document_refresh("000001", NOW - timedelta(days=1))
        self.assertEqual(self.store.current_parsed_documents("000001", (checksum,)), ())

        failed_refresh = self.store.begin_document_refresh("000001", NOW - timedelta(days=2))
        self.store.complete_document_refresh(
            failed_refresh,
            RefreshOutcome.FAILED,
            NOW - timedelta(days=2) + timedelta(seconds=1),
            failure=self._failure(),
        )
        self.assertEqual(self.store.current_parsed_documents("000001", (checksum,)), ())

        failed_refresh = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=2))
        self.store.complete_document_refresh(
            failed_refresh,
            RefreshOutcome.FAILED,
            NOW + timedelta(minutes=2, seconds=1),
            failure=self._failure(),
        )
        self.assertEqual(self.store.current_parsed_documents("000001", (checksum,)), ())

        empty_refresh = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=3))
        self.store.complete_document_refresh(
            empty_refresh,
            RefreshOutcome.EMPTY,
            NOW + timedelta(minutes=3, seconds=1),
        )
        self.assertEqual(self.store.current_parsed_documents("000001", (checksum,)), ())

    def test_current_blocks_candidate_after_later_live_parse_failure(self) -> None:
        parsed = self._parsed()
        _, current = self._publish_current(parsed)
        failure = self._failure()

        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                "INSERT INTO fund_document_parse_runs("
                "source_document_id, provenance_id, run_kind, outcome, parse_result_id, "
                "public_error_code, failure_stage, failure_reason, attempted_at"
                ") VALUES (?, ?, 'live', 'failed', NULL, ?, ?, ?, ?)",
                (
                    current.artifact.id,
                    current.provenance.id,
                    failure.public_code,
                    failure.stage.value,
                    failure.reason_code.value,
                    (NOW - timedelta(days=1)).isoformat(),
                ),
            )
            latest_run_id = cursor.lastrowid

        self.assertGreater(latest_run_id, current.parse_run.id)
        self.assertEqual(
            self.store.current_parsed_documents(
                "000001",
                (native_parser_provenance().provenance_checksum,),
            ),
            (),
        )

    def test_current_blocks_candidate_when_later_success_run_is_not_candidate_bound(self) -> None:
        parsed = self._parsed()
        _, current = self._publish_current(parsed)
        candidate_run_id = current.parse_run.id

        self.store.publish_parsed_document(parsed)
        with self.repository.connect() as connection:
            latest_run = connection.execute(
                "SELECT * FROM fund_document_parse_runs "
                "WHERE source_document_id = ? AND provenance_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (current.artifact.id, current.provenance.id),
            ).fetchone()
            candidate_run = connection.execute(
                "SELECT parse_run_id FROM fund_document_candidate_runs "
                "WHERE source_document_id = ?",
                (current.artifact.id,),
            ).fetchone()
        latest_run_id = latest_run["id"]
        self.assertGreater(latest_run_id, candidate_run_id)
        self.assertEqual(latest_run["outcome"], "success")
        self.assertEqual(latest_run["parse_result_id"], current.parse_result.id)
        self.assertEqual(candidate_run["parse_run_id"], candidate_run_id)

        records = self.store.current_parsed_documents(
            "000001",
            (native_parser_provenance().provenance_checksum,),
        )

        self.assertEqual(records, ())

    def test_active_provenance_checksums_require_exact_sorted_unique_digests(self) -> None:
        invalid = (
            [native_parser_provenance().provenance_checksum],
            ("b" * 64, "a" * 64),
            ("a" * 64, "a" * 64),
            ("A" * 64,),
            ("short",),
            (True,),
        )
        for checksums in invalid:
            with self.subTest(checksums=checksums), self.assertRaises(ValueError):
                self.store.current_parsed_documents("000001", checksums)  # type: ignore[arg-type]

    def test_current_parser_requirements_use_authenticated_current_refresh_only(self) -> None:
        _, current = self._publish_current()

        self.assertEqual(
            self.store.current_parser_requirements("000001"),
            (current.provenance,),
        )

        failed_refresh = self.store.begin_document_refresh(
            "000001",
            NOW + timedelta(minutes=1),
        )
        self.store.complete_document_refresh(
            failed_refresh,
            RefreshOutcome.FAILED,
            NOW + timedelta(minutes=1, seconds=1),
            failure=self._failure(),
        )

        self.assertEqual(self.store.current_parser_requirements("000001"), ())

    def test_partial_refresh_uses_only_same_refresh_successful_candidates(self) -> None:
        self._publish_current()
        partial = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        parsed = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="current-annual",
            published_at=NOW,
            retrieved_at=NOW + timedelta(minutes=1),
        )
        current = self.store.publish_candidate_success(
            refresh_id=partial,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=NOW + timedelta(minutes=1),
        )
        failed_candidate = replace(
            self._parsed().artifact.candidate,
            document_kind=DocumentKind.PRODUCT_SUMMARY,
            url="https://www.fund001.com/synthetic/000001/failed-summary",
        )
        self.store.publish_candidate_failure(
            refresh_id=partial,
            candidate=failed_candidate,
            failure=self._failure(),
            attempted_at=NOW + timedelta(minutes=1),
            artifact=None,
            provenance=None,
        )
        self.store.complete_document_refresh(
            partial,
            RefreshOutcome.PARTIAL,
            NOW + timedelta(minutes=1, seconds=1),
        )

        self.assertEqual(
            self.store.current_parsed_documents(
                "000001", (native_parser_provenance().provenance_checksum,)
            ),
            (current,),
        )

    def test_parse_failure_then_same_provenance_success_is_allowed(self) -> None:
        parsed = self._parsed()
        provenance = native_parser_provenance()
        failed_refresh = self.store.begin_document_refresh("000001", NOW)
        self.store.publish_candidate_parse_failure(
            refresh_id=failed_refresh,
            candidate=parsed.artifact.candidate,
            artifact=parsed.artifact,
            provenance=provenance,
            failure=self._failure(),
            attempted_at=NOW,
        )
        self.store.complete_document_refresh(
            failed_refresh,
            RefreshOutcome.FAILED,
            NOW + timedelta(seconds=1),
            failure=self._failure(),
        )

        success_refresh = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        stored = self.store.publish_candidate_success(
            refresh_id=success_refresh,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=provenance,
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=NOW + timedelta(minutes=1),
        )
        self.store.complete_document_refresh(
            success_refresh,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(minutes=1, seconds=1),
        )

        self.assertEqual(stored.facts[0].parse_result_id, stored.parse_result.id)
        self.assertEqual(
            self.store.current_parsed_documents(
                "000001",
                (provenance.provenance_checksum,),
            ),
            (stored,),
        )
        with self.repository.connect() as connection:
            outcomes = tuple(
                row["outcome"]
                for row in connection.execute(
                    "SELECT outcome FROM fund_document_parse_runs ORDER BY id"
                ).fetchall()
            )
        self.assertEqual(outcomes, ("failed", "success"))

    def test_native_parser_input_must_match_artifact_on_publish(self) -> None:
        parsed = self._parsed()
        refresh_id = self.store.begin_document_refresh("000001", NOW)

        with self.assertRaisesRegex(ValueError, "native parser input"):
            self.store.publish_candidate_success(
                refresh_id=refresh_id,
                candidate=parsed.artifact.candidate,
                parsed=parsed,
                provenance=native_parser_provenance(),
                parser_input_sha256="f" * 64,
                attempted_at=NOW,
            )

        with self.repository.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fund_document_parse_results").fetchone()[
                    0
                ],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM fund_document_candidate_runs").fetchone()[
                    0
                ],
                0,
            )

    def test_legacy_parser_input_can_differ_from_original_artifact(self) -> None:
        parsed = self._parsed()
        provenance = legacy_parser_provenance(
            image_id="sha256:" + "1" * 64,
            architecture="linux/arm64",
            libreoffice_version="24.2.0",
            package_manifest_checksum="2" * 64,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        stored = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=provenance,
            parser_input_sha256="e" * 64,
            attempted_at=NOW,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )

        self.assertEqual(stored.parse_result.parser_input_sha256, "e" * 64)
        self.assertEqual(
            self.store.current_parsed_documents("000001", (provenance.provenance_checksum,)),
            (stored,),
        )

    def test_same_artifact_and_facts_can_publish_under_distinct_legacy_provenance(self) -> None:
        parsed = self._parsed()
        provenances = (
            legacy_parser_provenance(
                image_id="sha256:" + "1" * 64,
                architecture="linux/arm64",
                libreoffice_version="24.2.0",
                package_manifest_checksum="2" * 64,
            ),
            legacy_parser_provenance(
                image_id="sha256:" + "3" * 64,
                architecture="linux/arm64",
                libreoffice_version="24.2.0",
                package_manifest_checksum="4" * 64,
            ),
        )
        records = []
        for offset, provenance in enumerate(provenances):
            started_at = NOW + timedelta(minutes=offset)
            refresh_id = self.store.begin_document_refresh("000001", started_at)
            records.append(
                self.store.publish_candidate_success(
                    refresh_id=refresh_id,
                    candidate=parsed.artifact.candidate,
                    parsed=parsed,
                    provenance=provenance,
                    parser_input_sha256=str(5 + offset) * 64,
                    attempted_at=started_at,
                )
            )
            self.store.complete_document_refresh(
                refresh_id,
                RefreshOutcome.SUCCESS,
                started_at + timedelta(seconds=1),
            )

        self.assertEqual(records[0].artifact.id, records[1].artifact.id)
        self.assertNotEqual(records[0].parse_result.id, records[1].parse_result.id)
        self.assertEqual(
            records[0].facts[0].fact_fingerprint,
            records[1].facts[0].fact_fingerprint,
        )
        self.assertNotEqual(records[0].facts[0].id, records[1].facts[0].id)

    def test_same_artifact_has_distinct_immutable_v3_and_v4_parse_results(self) -> None:
        parsed = self._parsed()
        historical = historical_native_provenance("3")
        historical_canonical_json = historical.canonical_json
        historical_checksum = historical.provenance_checksum
        provenances = (historical, native_parser_provenance())
        records = []

        for offset, provenance in enumerate(provenances):
            started_at = NOW + timedelta(minutes=offset)
            refresh_id = self.store.begin_document_refresh("000001", started_at)
            records.append(
                self.store.publish_candidate_success(
                    refresh_id=refresh_id,
                    candidate=parsed.artifact.candidate,
                    parsed=parsed,
                    provenance=provenance,
                    parser_input_sha256=parsed.artifact.sha256,
                    attempted_at=started_at,
                )
            )
            self.store.complete_document_refresh(
                refresh_id,
                RefreshOutcome.SUCCESS,
                started_at + timedelta(seconds=1),
            )

        self.assertEqual(records[0].artifact.id, records[1].artifact.id)
        self.assertNotEqual(records[0].provenance.id, records[1].provenance.id)
        self.assertNotEqual(records[0].parse_result.id, records[1].parse_result.id)
        self.assertNotEqual(records[0].facts[0].id, records[1].facts[0].id)
        self.assertEqual(records[0].provenance.parser_version, "3")
        self.assertEqual(records[1].provenance.parser_version, "4")
        self.assertEqual(records[0].provenance.canonical_json, historical_canonical_json)
        self.assertEqual(records[0].provenance.provenance_checksum, historical_checksum)

    def test_document_selection_round_trips_and_current_uses_latest_refresh(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = self._selection_plan(refresh_id)
        stored = self.store.publish_document_selection(plan, NOW)

        self.assertIs(type(stored), StoredDocumentSelectionManifest)
        self.assertEqual(stored.refresh_run_id, refresh_id)
        self.assertEqual(stored.fund_code, "000001")
        self.assertEqual(stored.manifest_version, 1)
        self.assertEqual(stored.periodic_candidates, plan.periodic_candidates)
        self.assertEqual(stored.periodic_states, plan.periodic_states)
        self.assertEqual(stored.canonical_json, plan.canonical_json)
        self.assertEqual(stored.selection_checksum, plan.selection_checksum)
        self.assertEqual(self.store.document_selection_for_refresh(refresh_id), stored)
        self.assertIsNone(self.store.current_document_selection("000001"))
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.EMPTY,
            NOW + timedelta(seconds=1),
        )
        self.assertEqual(self.store.current_document_selection("000001"), stored)

        later = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        self.assertIsNone(self.store.current_document_selection("000001"))
        later_plan = self._selection_plan(later)
        later_stored = self.store.publish_document_selection(
            later_plan,
            NOW + timedelta(minutes=1),
        )
        self.assertIsNone(self.store.current_document_selection("000001"))
        self.store.complete_document_refresh(
            later,
            RefreshOutcome.EMPTY,
            NOW + timedelta(minutes=1, seconds=1),
        )
        self.assertEqual(self.store.current_document_selection("000001"), later_stored)
        self.assertIsNone(self.store.document_selection_for_refresh(999999))

    def test_document_selection_publish_rejects_rebinding_and_duplicate_refresh(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        plan = self._selection_plan(refresh_id)
        self.store.publish_document_selection(plan, NOW)

        with self.assertRaises(RiskStoreError):
            self.store.publish_document_selection(plan, NOW)

        foreign_refresh = self.store.begin_document_refresh("000002", NOW)
        with self.assertRaises(RiskStoreError):
            self.store.publish_document_selection(
                self._selection_plan(foreign_refresh),
                NOW,
            )

    def test_document_selection_read_authenticates_every_stored_binding(self) -> None:
        mutations = (
            ("canonical_json", '{ "fund_code": "000001" }'),
            ("selection_checksum", "f" * 64),
            ("selection_policy_checksum", "f" * 64),
            ("fund_code", "000002"),
            ("manifest_version", 2),
            ("created_at", "not-a-time"),
        )
        for offset, (column, value) in enumerate(mutations):
            with self.subTest(column=column):
                refresh_id = self.store.begin_document_refresh(
                    "000001", NOW + timedelta(minutes=offset)
                )
                self.store.publish_document_selection(
                    self._selection_plan(refresh_id),
                    NOW + timedelta(minutes=offset),
                )
                with self.repository.connect() as connection, connection:
                    connection.execute("DROP TRIGGER fund_document_selection_manifest_no_update")
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f"UPDATE fund_document_selection_manifests SET {column} = ? "
                        "WHERE refresh_run_id = ?",
                        (value, refresh_id),
                    )
                    connection.execute("PRAGMA ignore_check_constraints = OFF")
                with self.assertRaises(RiskStoreError):
                    self.store.document_selection_for_refresh(refresh_id)
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        "CREATE TRIGGER fund_document_selection_manifest_no_update "
                        "BEFORE UPDATE ON fund_document_selection_manifests BEGIN "
                        "SELECT RAISE(ABORT, 'fund document selection manifests are immutable'); "
                        "END"
                    )

    def test_document_selection_read_rejects_refresh_id_rebinding(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        self.store.publish_document_selection(self._selection_plan(refresh_id), NOW)
        other_refresh = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_selection_manifest_no_update")
            connection.execute(
                "UPDATE fund_document_selection_manifests SET refresh_run_id = ? "
                "WHERE refresh_run_id = ?",
                (other_refresh, refresh_id),
            )
        with self.assertRaises(RiskStoreError):
            self.store.document_selection_for_refresh(other_refresh)

    def test_document_selection_read_rejects_semantic_manifest_tampering(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        stored = self.store.publish_document_selection(
            self._selection_plan(refresh_id),
            NOW,
        )
        payload = json.loads(stored.canonical_json)
        annual = next(
            item
            for item in payload["periodic_states"]
            if item["document_kind"] == DocumentKind.ANNUAL_REPORT.value
        )
        annual["state"] = "missing"
        annual["selected_fingerprint"] = None
        annual["reason_code"] = "current_periodic_candidate_missing"
        tampered = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_selection_manifest_no_update")
            connection.execute(
                "UPDATE fund_document_selection_manifests "
                "SET canonical_json = ?, selection_checksum = ? WHERE refresh_run_id = ?",
                (
                    tampered,
                    hashlib.sha256(tampered.encode("ascii")).hexdigest(),
                    refresh_id,
                ),
            )

        with self.assertRaises(RiskStoreError):
            self.store.document_selection_for_refresh(refresh_id)

    def test_native_parser_input_tampering_fails_closed_on_current_read(self) -> None:
        _, stored = self._publish_current()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_parse_result_no_update")
            connection.execute(
                "UPDATE fund_document_parse_results SET parser_input_sha256 = ? WHERE id = ?",
                ("f" * 64, stored.parse_result.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "parser input"):
            self.store.current_parsed_documents(
                "000001", (native_parser_provenance().provenance_checksum,)
            )

    def test_current_candidate_fingerprint_tampering_fails_closed(self) -> None:
        self._publish_current()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_candidate_run_no_update")
            connection.execute(
                "UPDATE fund_document_candidate_runs SET candidate_fingerprint = ?",
                ("f" * 64,),
            )

        with self.assertRaisesRegex(RiskStoreError, "candidate"):
            self.store.current_parsed_documents(
                "000001", (native_parser_provenance().provenance_checksum,)
            )

    def test_current_candidate_parse_run_binding_tampering_fails_closed(self) -> None:
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        first = self._parsed()
        second = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="second-current-binding",
            published_at=NOW,
            retrieved_at=NOW,
        )
        first_record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=first.artifact.candidate,
            parsed=first,
            provenance=native_parser_provenance(),
            parser_input_sha256=first.artifact.sha256,
            attempted_at=NOW,
        )
        second_record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=second.artifact.candidate,
            parsed=second,
            provenance=native_parser_provenance(),
            parser_input_sha256=second.artifact.sha256,
            attempted_at=NOW,
        )
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_candidate_run_no_update")
            connection.execute(
                "UPDATE fund_document_candidate_runs SET parse_run_id = ? "
                "WHERE source_document_id = ?",
                (second_record.parse_run.id, first_record.artifact.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "candidate"):
            self.store.current_parsed_documents(
                "000001", (native_parser_provenance().provenance_checksum,)
            )

    def test_current_candidate_outcome_tampering_does_not_revive_old_success(self) -> None:
        self._publish_current(started_at=NOW - timedelta(minutes=1))
        _, latest = self._publish_current(started_at=NOW)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_document_candidate_run_no_update")
            connection.execute(
                "UPDATE fund_document_candidate_runs "
                "SET outcome = 'failed', public_error_code = ?, failure_stage = ?, "
                "failure_reason = ? WHERE parse_run_id = ?",
                (
                    self._failure().public_code,
                    self._failure().stage.value,
                    self._failure().reason_code.value,
                    latest.parse_run.id,
                ),
            )

        self.assertEqual(
            self.store.current_parsed_documents(
                "000001", (native_parser_provenance().provenance_checksum,)
            ),
            (),
        )

    def test_current_success_allows_registered_redirect_final_url(self) -> None:
        parsed = self._parsed()
        redirected = replace(
            parsed,
            artifact=replace(
                parsed.artifact,
                final_url="https://www.fund001.com/redirected/prospectus",
            ),
        )

        _, stored = self._publish_current(redirected)

        self.assertEqual(stored.artifact.url, redirected.artifact.final_url)
        self.assertEqual(stored.artifact.landing_url, redirected.artifact.candidate.url)

    def test_second_different_success_for_same_artifact_and_provenance_fails_closed(self) -> None:
        parsed = self._parsed()
        self._publish_current(parsed)
        changed = replace(parsed, facts=(*parsed.facts, self._second_fact()))
        refresh_id = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))

        with self.assertRaisesRegex(RiskStoreError, "parse result conflict"):
            self.store.publish_candidate_success(
                refresh_id=refresh_id,
                candidate=changed.artifact.candidate,
                parsed=changed,
                provenance=native_parser_provenance(),
                parser_input_sha256=changed.artifact.sha256,
                attempted_at=NOW + timedelta(minutes=1),
            )
        with self.repository.connect() as connection:
            candidate_count = connection.execute(
                "SELECT COUNT(*) FROM fund_document_candidate_runs WHERE refresh_run_id = ?",
                (refresh_id,),
            ).fetchone()[0]
        self.assertEqual(candidate_count, 0)

    def test_policy_ensure_is_exact_and_idempotent(self) -> None:
        first = self.store.ensure_policy(ClassificationPolicyV1())
        second = self.store.ensure_policy(ClassificationPolicyV1())

        self.assertEqual(first, second)
        self.assertEqual(first.policy_checksum, ClassificationPolicyV1().checksum())

    def test_policy_ensure_rejects_stored_content_conflict(self) -> None:
        self.store.ensure_policy(ClassificationPolicyV1())
        canonical = '{"version":"1"}'
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_classification_policy_no_update")
            connection.execute(
                "UPDATE fund_classification_policy_versions "
                "SET canonical_policy_json = ?, policy_checksum = ?",
                (canonical, hashlib.sha256(canonical.encode()).hexdigest()),
            )

        with self.assertRaisesRegex(RiskStoreError, "policy authentication"):
            self.store.ensure_policy(ClassificationPolicyV1())

    def test_publish_parsed_document_is_atomic_and_idempotent(self) -> None:
        first_artifact, first_facts = self.store.publish_parsed_document(self._parsed())
        second_artifact, second_facts = self.store.publish_parsed_document(self._parsed())

        self.assertEqual(first_artifact, second_artifact)
        self.assertEqual(first_facts, second_facts)
        self.assertGreater(first_artifact.id, 0)
        self.assertEqual(len(first_facts), 1)
        self.assertEqual(first_facts[0].source_document_id, first_artifact.id)

        with self.repository.connect() as connection:
            artifact_count = connection.execute(
                "SELECT COUNT(*) FROM fund_document_artifacts"
            ).fetchone()[0]
            fact_count = connection.execute("SELECT COUNT(*) FROM fund_mandate_facts").fetchone()[0]
        self.assertEqual((artifact_count, fact_count), (1, 1))

    def test_typed_fact_values_round_trip_without_decimal_string_collision(self) -> None:
        base = {
            "fact_kind": "high_quality_fixed_income_min_percent",
            "unit": "percent",
            "page_number": 1,
            "section_name": "Investment scope",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        decimal_fields = {
            **base,
            "normalized_value": Decimal("80"),
            "source_excerpt": "Synthetic numeric eighty",
        }
        string_fields = {
            **base,
            "normalized_value": "80",
            "source_excerpt": "Synthetic text eighty",
        }
        nested_value = (
            ("alpha", Decimal("1.20")),
            ("beta", (None, True, 7, "7")),
        )
        nested_fields = {
            **base,
            "fact_kind": "synthetic_nested_fact",
            "normalized_value": nested_value,
            "unit": None,
            "source_excerpt": "Synthetic nested public value",
        }
        facts = tuple(
            ParsedMandateFact(**fields, fact_fingerprint=fact_fingerprint(**fields))
            for fields in (decimal_fields, string_fields, nested_fields)
        )
        self.assertNotEqual(facts[0].fact_fingerprint, facts[1].fact_fingerprint)
        parsed = replace(self._parsed(), facts=facts)

        _, stored = self.store.publish_parsed_document(parsed)
        values = tuple(item.normalized_value for item in stored)

        self.assertIn(Decimal("80"), values)
        self.assertIn("80", values)
        self.assertIn(nested_value, values)
        self.assertEqual(len({item.normalized_value_json for item in stored}), 3)

        mandate_facts = tuple(
            MandateFact(
                fund_code=item.fund_code,
                fact_kind=item.fact_kind,
                normalized_value=item.normalized_value,
                unit=item.unit,
                source_document_id=item.source_document_id,
                page_number=item.page_number,
                section_name=item.section_name,
                source_excerpt=item.source_excerpt,
                effective_from=item.effective_from,
                effective_to=item.effective_to,
                confidence_state=item.confidence_state,
                parser_version=item.parser_version,
                fact_fingerprint=item.fact_fingerprint,
            )
            for item in stored
        )
        freshness = EvidenceFreshness(
            section="legal_scope",
            source_document_id=stored[0].source_document_id,
            state=FreshnessState.CURRENT,
            observed_at=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
            critical=True,
        )
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=tuple(
                reversed(
                    sorted(
                        mandate_facts,
                        key=lambda fact: (
                            fact.fact_kind,
                            fact.source_document_id,
                            fact.fact_fingerprint,
                        ),
                    )
                )
            ),
            benchmark_facts=(),
            report_facts=(),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(freshness,),
            document_ids=(stored[0].source_document_id,),
            fact_ids=tuple(item.id for item in stored),
            parse_result_ids=tuple(sorted({item.parse_result_id for item in stored})),
            parser_provenance_checksums=(native_parser_provenance().provenance_checksum,),
        )
        evidence = self._bind_evidence_to_selection(evidence, parsed)
        policy = ClassificationPolicyV1()
        classification = classify_fund(evidence, policy, NOW)
        self.store.save_classification(classification, evidence, policy)
        bound = self.store.classification_evidence("000001")
        assert bound is not None
        rebound_values = {
            item.fact_fingerprint: item.normalized_value for item in bound.evidence.legal_facts
        }
        self.assertEqual(
            rebound_values,
            {item.fact_fingerprint: item.normalized_value for item in stored},
        )

    def test_typed_fact_json_tampering_fails_closed(self) -> None:
        artifact, facts = self.store.publish_parsed_document(
            replace(self._parsed(), facts=(self._second_fact(),))
        )
        malformed_values = (
            '{"type":"unknown","value":"0"}',
            '{"extra":true,"type":"decimal","value":"0"}',
            '{"type":"decimal","value":"0.0"}',
            '[{"type":"int","value":1}]',
            '{"type":"decimal","value":"NaN"}',
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
        for malformed in malformed_values:
            with self.subTest(malformed=malformed):
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        "UPDATE fund_mandate_facts SET normalized_value_json = ? WHERE id = ?",
                        (malformed, facts[0].id),
                    )
                with self.assertRaisesRegex(RiskStoreError, "fact authentication"):
                    self.store.parsed_document_history(artifact.fund_code)

    def test_typed_fact_value_change_is_detected_by_fingerprint(self) -> None:
        artifact, facts = self.store.publish_parsed_document(
            replace(self._parsed(), facts=(self._second_fact(),))
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
            connection.execute(
                "UPDATE fund_mandate_facts SET normalized_value_json = ? WHERE id = ?",
                ('{"type":"str","value":"0"}', facts[0].id),
            )

        with self.assertRaisesRegex(RiskStoreError, "fact fingerprint"):
            self.store.parsed_document_history(artifact.fund_code)

    def test_same_artifact_key_with_different_metadata_is_a_conflict(self) -> None:
        self.store.publish_parsed_document(self._parsed())
        changed = replace(
            self._parsed(),
            artifact=replace(self._artifact(), content_type="application/xhtml+xml"),
        )

        with self.assertRaisesRegex(RiskStoreError, "artifact fingerprint conflict"):
            self.store.publish_parsed_document(changed)

    def test_publish_rejects_forged_tier_one_publisher_or_unregistered_host(self) -> None:
        base = self._parsed()
        attacks = (
            replace(
                base,
                artifact=replace(
                    base.artifact,
                    candidate=replace(
                        base.artifact.candidate,
                        publisher="evil publisher",
                    ),
                ),
            ),
            replace(
                base,
                artifact=replace(
                    base.artifact,
                    candidate=replace(
                        base.artifact.candidate,
                        url="https://www.efunds.com.cn/forged/prospectus",
                    ),
                    final_url="https://www.efunds.com.cn/forged/prospectus",
                ),
            ),
        )
        for attack in attacks:
            with self.subTest(publisher=attack.artifact.candidate.publisher):
                with self.assertRaisesRegex(OfficialDocumentError, "not registered"):
                    self.store.publish_parsed_document(attack)

    def test_publish_allows_registered_redirect_host_from_source_allowlist(self) -> None:
        parsed = self._parsed()
        redirected = replace(
            parsed,
            artifact=replace(
                parsed.artifact,
                final_url="https://www.fund001.com/redirected/prospectus",
            ),
        )

        artifact, facts = self.store.publish_parsed_document(redirected)

        self.assertEqual(artifact.url, redirected.artifact.final_url)
        self.assertEqual(artifact.landing_url, redirected.artifact.candidate.url)
        self.assertEqual(len(facts), 1)

    def test_published_fact_set_cannot_be_extended(self) -> None:
        original = self._parsed()
        self.store.publish_parsed_document(original)
        changed = replace(original, facts=(*original.facts, self._second_fact()))

        with self.assertRaisesRegex(RiskStoreError, "parse result conflict"):
            self.store.publish_parsed_document(changed)

        with self.repository.connect() as connection:
            facts = connection.execute("SELECT * FROM fund_mandate_facts").fetchall()
        self.assertEqual(len(facts), 1)

    def test_publish_rolls_back_artifact_and_facts_on_readback_failure(self) -> None:
        original = self.store._row_to_fact

        def fail_readback(row):
            raise RiskStoreError("synthetic fact readback failure")

        self.store._row_to_fact = fail_readback
        try:
            with self.assertRaisesRegex(RiskStoreError, "synthetic fact readback"):
                self.store.publish_parsed_document(self._parsed())
        finally:
            self.store._row_to_fact = original

        with self.repository.connect() as connection:
            artifacts = connection.execute(
                "SELECT COUNT(*) FROM fund_document_artifacts"
            ).fetchone()[0]
            facts = connection.execute("SELECT COUNT(*) FROM fund_mandate_facts").fetchone()[0]
        self.assertEqual((artifacts, facts), (0, 0))

    def test_concurrent_publish_serializes_to_one_artifact_and_fact_set(self) -> None:
        def publish(_):
            local = FundRiskStore(self.repository)
            return local.publish_parsed_document(self._parsed())

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(publish, range(2)))

        self.assertEqual(results[0], results[1])
        with self.repository.connect() as connection:
            artifacts = connection.execute(
                "SELECT COUNT(*) FROM fund_document_artifacts"
            ).fetchone()[0]
            facts = connection.execute("SELECT COUNT(*) FROM fund_mandate_facts").fetchone()[0]
        self.assertEqual((artifacts, facts), (1, 1))

    def test_failed_artifact_never_publishes_facts(self) -> None:
        stored = self.store.save_failed_artifact(
            self._artifact(),
            parser_version=PARSER_VERSION,
            parse_error_code="official_document_parse_failed",
        )

        self.assertEqual(stored.parse_status, "failed")
        with self.repository.connect() as connection:
            fact_count = connection.execute("SELECT COUNT(*) FROM fund_mandate_facts").fetchone()[0]
        self.assertEqual(fact_count, 0)

    def test_failed_artifact_legacy_parse_fields_are_not_authoritative(self) -> None:
        first = self.store.save_failed_artifact(
            self._artifact(),
            parser_version=PARSER_VERSION,
            parse_error_code="official_document_parse_failed",
        )
        second = self.store.save_failed_artifact(
            self._artifact(),
            parser_version=PARSER_VERSION,
            parse_error_code="official_document_resource_limit",
        )

        self.assertEqual(first, second)
        self.assertEqual(self.store.current_parsed_documents("000001", ()), ())

    def test_parsed_document_history_returns_authenticated_bundles_in_stable_order(self) -> None:
        older = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="older",
            published_at=NOW,
            retrieved_at=NOW,
        )
        newer_first = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="newer-first",
            published_at=NOW + timedelta(days=1),
            retrieved_at=NOW + timedelta(hours=1),
        )
        newer_second = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="newer-second",
            published_at=NOW + timedelta(days=1),
            retrieved_at=NOW + timedelta(hours=1),
        )
        annual = self._document_variant(
            kind=DocumentKind.ANNUAL_REPORT,
            marker="annual",
            published_at=NOW - timedelta(days=1),
            retrieved_at=NOW + timedelta(days=2),
        )
        no_publication_time = self._document_variant(
            kind=DocumentKind.PROSPECTUS,
            marker="no-publication-time",
            published_at=None,
            retrieved_at=NOW + timedelta(days=3),
        )
        for parsed in (older, newer_first, newer_second, annual, no_publication_time):
            self.store.publish_parsed_document(parsed)

        history = self.store.parsed_document_history("000001")

        self.assertEqual(
            tuple(item.artifact.title for item in history),
            (
                "Synthetic annual",
                "Synthetic newer-second",
                "Synthetic newer-first",
                "Synthetic older",
                "Synthetic no-publication-time",
            ),
        )
        self.assertTrue(all(item.artifact.parse_status == "parsed" for item in history))
        self.assertTrue(all(len(item.facts) == 1 for item in history))
        self.assertTrue(
            all(
                fact.source_document_id == item.artifact.id
                for item in history
                for fact in item.facts
            )
        )

    def test_parsed_document_history_excludes_failed_artifacts(self) -> None:
        self.store.publish_parsed_document(self._parsed())
        failed = self._document_variant(
            kind=DocumentKind.PRODUCT_SUMMARY,
            marker="failed-summary",
            published_at=NOW,
            retrieved_at=NOW + timedelta(hours=1),
        )
        self.store.save_failed_artifact(
            failed.artifact,
            parser_version=PARSER_VERSION,
            parse_error_code="official_document_parse_failed",
        )

        history = self.store.parsed_document_history("000001")

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].artifact.document_kind, DocumentKind.PROSPECTUS)

    def test_parsed_document_history_rejects_artifact_or_fact_tampering(self) -> None:
        artifact, _ = self.store.publish_parsed_document(self._parsed())
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
            connection.execute(
                "UPDATE fund_mandate_facts SET source_excerpt = ? WHERE source_document_id = ?",
                ("Tampered public excerpt", artifact.id),
            )
        with self.assertRaisesRegex(RiskStoreError, "fact fingerprint"):
            self.store.parsed_document_history("000001")

        with self.repository.connect() as connection, connection:
            connection.execute(
                "UPDATE fund_mandate_facts SET source_excerpt = ? WHERE source_document_id = ?",
                ("Synthetic public investment scope", artifact.id),
            )
        raw = self.document_path.read_bytes()
        self.document_path.write_bytes(b"z" * len(raw))
        with self.assertRaisesRegex(RiskStoreError, "artifact authentication"):
            self.store.parsed_document_history("000001")

    def test_classification_is_idempotent_and_authenticates_evidence(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        first = self.store.save_classification(classification, evidence, policy)
        second = self.store.save_classification(classification, evidence, policy)

        self.assertEqual(first, second)
        self.assertEqual(self.store.current_classification("000001"), first)
        self.assertEqual(self.store.classification_history("000001"), (first,))
        bound = self.store.classification_evidence("000001")
        self.assertIsNotNone(bound)
        assert bound is not None
        self.assertEqual(bound.classification, first)
        self.assertEqual(tuple(item.id for item in bound.documents), evidence.document_ids)
        self.assertEqual(tuple(item.id for item in bound.facts), evidence.fact_ids)

    def test_v3_manifest_has_exact_result_provenance_and_selection_bindings(self) -> None:
        classification, evidence, policy = self._classification_inputs()

        stored = self.store.save_classification(classification, evidence, policy)
        manifest = json.loads(stored.input_manifest_json)

        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(manifest["parse_result_ids"], list(evidence.parse_result_ids))
        self.assertEqual(
            manifest["parser_provenance_checksums"],
            list(evidence.parser_provenance_checksums),
        )
        self.assertEqual(manifest["document_refresh_run_id"], evidence.document_refresh_run_id)
        self.assertEqual(
            manifest["candidate_run_snapshot_checksum"],
            evidence.candidate_run_snapshot_checksum,
        )
        self.assertEqual(
            stored.input_manifest_json,
            json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )

    def test_v2_zero_fact_document_round_trips_authenticated_result_binding(self) -> None:
        parsed = replace(self._parsed(), facts=())
        refresh_id = self.store.begin_document_refresh("000001", NOW)
        record = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=NOW,
        )
        freshness = EvidenceFreshness(
            section="legal_scope",
            source_document_id=record.artifact.id,
            state=FreshnessState.CURRENT,
            observed_at=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
            critical=True,
        )
        evidence = ClassificationEvidence(
            fund_code="000001",
            legal_facts=(),
            benchmark_facts=(),
            report_facts=(),
            existing_disclosure_facts=(),
            nav_conflicts=(),
            external_evidence_fingerprints=(),
            external_source_references=(),
            nav_evidence_fingerprint=None,
            nav_observation_start=None,
            nav_observation_end=None,
            freshness=(freshness,),
            document_ids=(record.artifact.id,),
            fact_ids=(),
            parse_result_ids=(record.parse_result.id,),
            parser_provenance_checksums=(record.provenance.provenance_checksum,),
        )
        plan = select_current_candidates(
            "000001",
            refresh_run_id=refresh_id,
            candidates=(parsed.artifact.candidate,),
        )
        self.store.publish_document_selection(plan, NOW)
        self.store.complete_document_refresh(
            refresh_id,
            RefreshOutcome.SUCCESS,
            NOW + timedelta(seconds=1),
        )
        snapshot = self.store.current_document_selection_snapshot("000001")
        assert snapshot is not None
        evidence = replace(
            evidence,
            document_refresh_run_id=refresh_id,
            selection_policy_checksum=snapshot.selection.selection_policy_checksum,
            selection_manifest_checksum=snapshot.selection.selection_checksum,
            candidate_run_snapshot_checksum=snapshot.candidate_run_snapshot_checksum,
            selection_reason_codes=snapshot.selection_reason_codes,
        )
        policy = ClassificationPolicyV1()
        classification = classify_fund(evidence, policy, NOW)

        stored = self.store.save_classification(classification, evidence, policy)
        bound = self.store.classification_evidence("000001", stored.id)

        assert bound is not None
        self.assertEqual(tuple(item.id for item in bound.documents), evidence.document_ids)
        self.assertEqual(bound.facts, ())
        self.assertEqual(bound.evidence.parse_result_ids, evidence.parse_result_ids)
        self.assertEqual(
            bound.evidence.parser_provenance_checksums,
            evidence.parser_provenance_checksums,
        )

    def test_v1_manifest_bytes_and_fingerprint_read_back_with_derived_bindings(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        stored = self.store.save_classification(classification, evidence, policy)
        manifest_v1 = classification_input_manifest_v1(evidence, policy, NOW)
        manifest_json = json.dumps(
            manifest_v1,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
            connection.execute(
                "UPDATE fund_risk_classifications SET input_manifest_json = ?, "
                "input_fingerprint = ? WHERE id = ?",
                (manifest_json, fingerprint, stored.id),
            )

        bound = self.store.classification_evidence("000001")

        assert bound is not None
        self.assertEqual(bound.classification.input_manifest_json, manifest_json)
        self.assertEqual(bound.classification.input_fingerprint, fingerprint)
        self.assertEqual(bound.evidence.parse_result_ids, evidence.parse_result_ids)
        self.assertEqual(
            bound.evidence.parser_provenance_checksums,
            evidence.parser_provenance_checksums,
        )

    def test_manifest_shape_and_canonical_bytes_fail_closed(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        stored = self.store.save_classification(classification, evidence, policy)
        v1 = classification_input_manifest_v1(evidence, policy, NOW)
        v2 = json.loads(stored.input_manifest_json)
        invalid_manifests = {
            "mixed": {
                **v1,
                "parse_result_ids": list(evidence.parse_result_ids),
                "parser_provenance_checksums": list(evidence.parser_provenance_checksums),
            },
            "partial_v2": {
                **v1,
                "manifest_version": 2,
                "parse_result_ids": list(evidence.parse_result_ids),
            },
            "unknown": {**v2, "unknown_binding": True},
        }
        encoded = {
            label: json.dumps(
                manifest,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            for label, manifest in invalid_manifests.items()
        }
        encoded["noncanonical"] = json.dumps(v2, ensure_ascii=True, indent=2, sort_keys=True)
        encoded["reordered"] = json.dumps(
            dict(reversed(tuple(v2.items()))),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
        for label, manifest_json in encoded.items():
            with self.subTest(label=label):
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        "UPDATE fund_risk_classifications SET input_manifest_json = ? WHERE id = ?",
                        (manifest_json, stored.id),
                    )
                with self.assertRaisesRegex(RiskStoreError, "classification storage"):
                    self.store.classification_history("000001")
                with self.assertRaisesRegex(RiskStoreError, "classification storage"):
                    self.store.classification_evidence("000001")

    def test_v2_explicit_result_ids_cannot_include_second_result_for_same_document(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        legacy = legacy_parser_provenance(
            image_id="sha256:" + "1" * 64,
            architecture="linux/arm64",
            libreoffice_version="24.2.0",
            package_manifest_checksum="2" * 64,
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        parsed = self._parsed()
        extra = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=legacy,
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=NOW + timedelta(minutes=1),
        )
        expanded = replace(
            evidence,
            parse_result_ids=tuple(sorted((*evidence.parse_result_ids, extra.parse_result.id))),
            parser_provenance_checksums=tuple(
                sorted((*evidence.parser_provenance_checksums, legacy.provenance_checksum))
            ),
        )
        expanded_classification = classify_fund(expanded, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "current selection"):
            self.store.save_classification(expanded_classification, expanded, policy)

    def test_v2_document_cannot_omit_its_parse_result(self) -> None:
        _, evidence, policy = self._classification_inputs()
        missing = replace(
            evidence,
            parse_result_ids=(),
            parser_provenance_checksums=(),
        )
        classification = classify_fund(missing, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "parse result binding"):
            self.store.save_classification(classification, missing, policy)

    def test_v2_document_cannot_bind_result_from_another_document(self) -> None:
        _, evidence, policy = self._classification_inputs()
        parsed = self._document_variant(
            kind=DocumentKind.PRODUCT_SUMMARY,
            marker="foreign-result",
            published_at=NOW,
            retrieved_at=NOW + timedelta(minutes=1),
        )
        refresh_id = self.store.begin_document_refresh("000001", NOW + timedelta(minutes=1))
        foreign = self.store.publish_candidate_success(
            refresh_id=refresh_id,
            candidate=parsed.artifact.candidate,
            parsed=parsed,
            provenance=native_parser_provenance(),
            parser_input_sha256=parsed.artifact.sha256,
            attempted_at=NOW + timedelta(minutes=1),
        )
        rebound = replace(
            evidence,
            parse_result_ids=(foreign.parse_result.id,),
        )
        classification = classify_fund(rebound, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "current selection"):
            self.store.save_classification(classification, rebound, policy)

    def test_same_classification_fingerprint_with_different_content_is_a_conflict(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        self.store.save_classification(classification, evidence, policy)
        changed = replace(classification, product_family=ProductFamily.UNSUPPORTED)

        with self.assertRaisesRegex(RiskStoreError, "classification fingerprint conflict"):
            self.store.save_classification(changed, evidence, policy)

    def test_classification_rejects_fact_from_another_fund(self) -> None:
        _, evidence, policy = self._classification_inputs()
        _, foreign_facts = self.store.publish_parsed_document(self._parsed("000002"))
        foreign_evidence = replace(evidence, fact_ids=(foreign_facts[0].id,))
        classification = classify_fund(foreign_evidence, policy, NOW)

        with self.assertRaisesRegex(RiskStoreError, "fact fund binding"):
            self.store.save_classification(classification, foreign_evidence, policy)

    def test_concurrent_classification_save_serializes_to_one_row(self) -> None:
        inputs = self._classification_inputs()

        def save(_):
            return FundRiskStore(self.repository).save_classification(*inputs)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(save, range(2)))

        self.assertEqual(results[0], results[1])
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM fund_risk_classifications").fetchone()[
                0
            ]
        self.assertEqual(count, 1)

    def test_manifest_tampering_fails_closed_on_history_read(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        stored = self.store.save_classification(classification, evidence, policy)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
            connection.execute(
                "UPDATE fund_risk_classifications SET input_manifest_json = ? WHERE id = ?",
                ('{"fund_code":"000001"}', stored.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "classification storage"):
            self.store.classification_history("000001")

    def test_external_disclosure_facts_round_trip_without_d1_fact_ids(self) -> None:
        _, evidence, policy = self._classification_inputs()
        fields = {
            "fact_kind": "platform_category",
            "normalized_value": "bond_fund",
            "unit": None,
            "page_number": None,
            "section_name": "identity",
            "source_excerpt": "derived from current sourced identity disclosure",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        external_fact = MandateFact(
            fund_code="000001",
            source_document_id=999999,
            parser_version="external_disclosure_v1",
            fact_fingerprint=fact_fingerprint(**fields),
            **fields,
        )
        external_source = ExternalSourceReference(
            source_namespace="fund_disclosure",
            source_document_id=999999,
            fund_code="000001",
            document_kind="basic_profile",
            section="identity",
            title="public identity disclosure",
            url="https://www.fund001.com/identity.html",
            source_name="public_source",
            source_tier=2,
            publisher="public publisher",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(days=1),
            checksum="e" * 64,
        )
        evidence = replace(
            evidence,
            existing_disclosure_facts=(external_fact,),
            external_source_references=(external_source,),
        )
        classification = classify_fund(evidence, policy, NOW)

        self.store.save_classification(classification, evidence, policy)
        bound = self.store.classification_evidence("000001")

        assert bound is not None
        self.assertEqual(bound.evidence.existing_disclosure_facts, (external_fact,))
        self.assertEqual(bound.evidence.external_source_references, (external_source,))
        self.assertNotIn(external_fact.source_document_id, evidence.document_ids)
        self.assertEqual(len(bound.facts), len(evidence.fact_ids))

    def test_external_source_manifest_tampering_fails_closed(self) -> None:
        _, evidence, policy = self._classification_inputs()
        fields = {
            "fact_kind": "platform_category",
            "normalized_value": "bond_fund",
            "unit": None,
            "page_number": None,
            "section_name": "identity",
            "source_excerpt": "derived from current sourced identity disclosure",
            "effective_from": None,
            "effective_to": None,
            "confidence_state": FactConfidence.EXACT,
        }
        external_fact = MandateFact(
            fund_code="000001",
            source_document_id=999999,
            parser_version="external_disclosure_v1",
            fact_fingerprint=fact_fingerprint(**fields),
            **fields,
        )
        external_source = ExternalSourceReference(
            source_namespace="fund_disclosure",
            source_document_id=999999,
            fund_code="000001",
            document_kind="basic_profile",
            section="identity",
            title="public identity disclosure",
            url="https://www.fund001.com/identity.html",
            source_name="public_source",
            source_tier=2,
            publisher="public publisher",
            published_at=NOW - timedelta(days=2),
            retrieved_at=NOW - timedelta(days=1),
            checksum="e" * 64,
        )
        evidence = replace(
            evidence,
            existing_disclosure_facts=(external_fact,),
            external_source_references=(external_source,),
        )
        classification = classify_fund(evidence, policy, NOW)
        stored = self.store.save_classification(classification, evidence, policy)
        manifest = json.loads(stored.input_manifest_json)
        manifest["external_source_references"][0]["checksum"] = "f" * 64
        tampered = json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
            connection.execute(
                "UPDATE fund_risk_classifications SET input_manifest_json = ? WHERE id = ?",
                (tampered, stored.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "classification storage"):
            self.store.classification_history("000001")

    def test_classification_output_tampering_is_recomputed_and_rejected(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        stored = self.store.save_classification(classification, evidence, policy)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_risk_classification_no_update")
            connection.execute(
                "UPDATE fund_risk_classifications SET product_family = ? WHERE id = ?",
                (ProductFamily.UNSUPPORTED.value, stored.id),
            )

        with self.assertRaisesRegex(RiskStoreError, "classification storage"):
            self.store.classification_history("000001")

    def test_bound_fact_tampering_fails_closed(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        self.store.save_classification(classification, evidence, policy)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
            connection.execute(
                "UPDATE fund_mandate_facts SET source_excerpt = ?",
                ("Different synthetic public excerpt",),
            )

        with self.assertRaisesRegex(RiskStoreError, "fact fingerprint"):
            self.store.classification_history("000001")

    def test_bound_artifact_file_tampering_fails_closed(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        self.store.save_classification(classification, evidence, policy)
        raw = self.document_path.read_bytes()
        self.document_path.write_bytes(b"x" * len(raw))

        with self.assertRaisesRegex(RiskStoreError, "artifact authentication"):
            self.store.classification_history("000001")

    def test_classification_readback_failure_rolls_back_insert(self) -> None:
        classification, evidence, policy = self._classification_inputs()
        original = self.store._row_to_classification

        def fail_readback(connection, row):
            raise RiskStoreError("synthetic classification readback failure")

        self.store._row_to_classification = fail_readback
        try:
            with self.assertRaisesRegex(RiskStoreError, "synthetic classification readback"):
                self.store.save_classification(classification, evidence, policy)
        finally:
            self.store._row_to_classification = original
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM fund_risk_classifications").fetchone()[
                0
            ]
        self.assertEqual(count, 0)

    def test_current_and_history_order_by_absolute_time_then_id(self) -> None:
        first_inputs = self._classification_inputs(classified_at=NOW, external_marker="a")
        first = self.store.save_classification(*first_inputs)
        same_time_inputs = self._classification_inputs(classified_at=NOW, external_marker="b")
        second = self.store.save_classification(*same_time_inputs)
        later = (NOW + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8)))
        later_utc = later.astimezone(timezone.utc)
        later_inputs = self._classification_inputs(classified_at=later_utc, external_marker="c")
        third = self.store.save_classification(*later_inputs)

        self.assertEqual(
            self.store.classification_history("000001"),
            (third, second, first),
        )
        self.assertEqual(self.store.current_classification("000001"), third)


if __name__ == "__main__":
    unittest.main()
