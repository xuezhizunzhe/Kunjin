from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import (
    ParserProvenance,
    ParseRunKind,
    ParseRunOutcome,
    RefreshOutcome,
    candidate_fingerprint,
    canonical_fact_set_fingerprint,
    native_parser_provenance,
)
from kunjin.funds.risk.documents import (
    MAX_DOCUMENT_BYTES,
    OfficialDocumentCandidate,
    RetrievedArtifact,
    validate_official_source,
    validate_safe_https_url,
)
from kunjin.funds.risk.engine import (
    ClassificationEvidence,
    classification_input_manifest,
    classification_input_manifest_v1,
    classification_input_manifest_v2,
    classification_input_manifest_v3,
    classify_fund,
)
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
    SafeDocumentFailure,
)
from kunjin.funds.risk.models import (
    EvidenceFreshness,
    EvidenceStatus,
    ExternalSourceReference,
    FactConfidence,
    FreshnessState,
    FundRiskClassification,
    MandateFact,
    PortfolioRole,
    ProductFamily,
    RiskBucket,
    decode_fact_value_json,
    encode_fact_value_json,
    fact_value_from_canonical,
)
from kunjin.funds.risk.parsers import (
    PARSER_VERSION,
    ParsedMandateFact,
    ParsedRiskDocument,
    fact_fingerprint,
)
from kunjin.funds.risk.policy import (
    CLASSIFICATION_POLICY_V1_CHECKSUM,
    ClassificationPolicyV1,
)
from kunjin.funds.risk.selection import (
    PERIODIC_DOCUMENT_KINDS,
    SELECTION_POLICY_V1_CHECKSUM,
    SELECTION_REASON_CODES,
    DocumentSelectionPlan,
    PeriodicSelectionState,
    SelectionCandidate,
    current_evidence_projection,
)
from kunjin.storage.repository import Repository


class RiskStoreError(RuntimeError):
    """A deterministic, redacted D1 persistence failure."""

    code = "classification_storage_failed"


_MANIFEST_V1_KEYS = frozenset(
    {
        "benchmark_facts",
        "classified_at",
        "document_ids",
        "existing_disclosure_facts",
        "external_evidence_fingerprints",
        "external_source_references",
        "fact_ids",
        "freshness",
        "fund_code",
        "legal_facts",
        "nav_conflicts",
        "nav_evidence_fingerprint",
        "nav_observation_end",
        "nav_observation_start",
        "policy_checksum",
        "policy_version",
        "report_facts",
    }
)
_MANIFEST_V2_KEYS = _MANIFEST_V1_KEYS | frozenset(
    {
        "manifest_version",
        "parse_result_ids",
        "parser_provenance_checksums",
    }
)
_MANIFEST_V3_KEYS = _MANIFEST_V2_KEYS | frozenset(
    {
        "document_refresh_run_id",
        "selection_policy_checksum",
        "selection_manifest_checksum",
        "candidate_run_snapshot_checksum",
        "selection_reason_codes",
    }
)
_SELECTION_MANIFEST_KEYS = frozenset(
    {
        "fund_code",
        "manifest_version",
        "periodic_candidates",
        "periodic_states",
        "refresh_run_id",
        "selection_policy_checksum",
    }
)
_SELECTION_CANDIDATE_KEYS = frozenset(
    {"candidate_fingerprint", "document_kind", "published_at", "url"}
)
_SELECTION_STATE_KEYS = frozenset(
    {
        "candidate_fingerprints",
        "document_kind",
        "reason_code",
        "selected_fingerprint",
        "state",
    }
)
_LEGAL_EVIDENCE_DOCUMENT_KINDS = frozenset(
    {
        DocumentKind.FUND_CONTRACT,
        DocumentKind.PROSPECTUS,
        DocumentKind.PROSPECTUS_UPDATE,
        DocumentKind.PRODUCT_SUMMARY,
        DocumentKind.CLASSIFICATION_ANNOUNCEMENT,
    }
)


@dataclass(frozen=True)
class StoredDocumentArtifact:
    id: int
    fund_code: str
    document_kind: DocumentKind
    url: str
    landing_url: str
    publisher: str
    title: str
    published_at: Optional[datetime]
    retrieved_at: datetime
    content_type: str
    byte_size: int
    sha256: str
    managed_path: Path
    parse_status: str
    parser_version: str
    parse_error_code: Optional[str]


@dataclass(frozen=True)
class StoredFact:
    id: int
    fund_code: str
    source_document_id: int
    parse_result_id: int
    fact_kind: str
    normalized_value_json: str
    unit: Optional[str]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    effective_from: Optional[date]
    effective_to: Optional[date]
    confidence_state: FactConfidence
    parser_version: str
    fact_fingerprint: str

    @property
    def normalized_value(self) -> object:
        return decode_fact_value_json(self.normalized_value_json)


@dataclass(frozen=True)
class ParsedDocumentRecord:
    artifact: StoredDocumentArtifact
    facts: Tuple[StoredFact, ...]
    provenance: StoredParserProvenance
    parse_result: StoredParseResult
    parse_run: StoredParseRun


@dataclass(frozen=True)
class StoredParserProvenance:
    id: int
    parser_version: str
    converter_kind: str
    canonical_json: str
    provenance_checksum: str
    created_at: datetime


@dataclass(frozen=True)
class StoredParseResult:
    id: int
    source_document_id: int
    provenance_id: int
    parser_input_sha256: str
    fact_set_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class StoredParseRun:
    id: int
    source_document_id: int
    provenance_id: int
    run_kind: ParseRunKind
    outcome: ParseRunOutcome
    parse_result_id: Optional[int]
    public_error_code: Optional[str]
    failure_stage: Optional[DocumentFailureStage]
    failure_reason: Optional[DocumentFailureReason]
    attempted_at: datetime


@dataclass(frozen=True)
class StoredClassificationPolicy:
    version: str
    canonical_policy_json: str
    policy_checksum: str
    effective_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class StoredDocumentSelectionManifest:
    refresh_run_id: int
    fund_code: str
    manifest_version: int
    selection_policy_checksum: str
    canonical_json: str
    selection_checksum: str
    created_at: datetime
    periodic_candidates: Tuple[SelectionCandidate, ...]
    periodic_states: Tuple[PeriodicSelectionState, ...]


@dataclass(frozen=True)
class StoredSelectionCandidateRun:
    id: int
    candidate_fingerprint: str
    fund_code: str
    document_kind: DocumentKind
    url: str
    published_at: Optional[datetime]
    outcome: ParseRunOutcome
    source_document_id: Optional[int]
    parse_run_id: Optional[int]
    parsed_record: Optional[ParsedDocumentRecord]
    failure: Optional[SafeDocumentFailure]
    created_at: datetime


@dataclass(frozen=True)
class CurrentDocumentSelectionSnapshot:
    selection: StoredDocumentSelectionManifest
    candidate_runs: Tuple[StoredSelectionCandidateRun, ...]
    selected_periodic_records: Tuple[ParsedDocumentRecord, ...]
    nonperiodic_successful_records: Tuple[ParsedDocumentRecord, ...]
    candidate_run_snapshot_checksum: str
    selection_reason_codes: Tuple[str, ...]


@dataclass(frozen=True)
class StoredClassification:
    id: int
    fund_code: str
    policy_version: str
    input_fingerprint: str
    input_manifest_json: str
    product_family: ProductFamily
    risk_bucket: RiskBucket
    portfolio_role: PortfolioRole
    evidence_status: EvidenceStatus
    evidence_tags_json: str
    reason_codes_json: str
    missing_evidence_json: str
    conflicts_json: str
    evidence_document_ids_json: str
    evidence_fact_ids_json: str
    freshness_json: str
    classified_at: datetime
    valid_until: datetime
    created_at: datetime


@dataclass(frozen=True)
class ClassificationEvidenceRecord:
    classification: StoredClassification
    policy: StoredClassificationPolicy
    documents: Tuple[StoredDocumentArtifact, ...]
    facts: Tuple[StoredFact, ...]
    evidence: ClassificationEvidence


class FundRiskStore:
    def __init__(self, repository: Repository) -> None:
        self._repository = _owned_repository(repository)

    def ensure_policy(self, policy: ClassificationPolicyV1) -> StoredClassificationPolicy:
        if type(policy) is not ClassificationPolicyV1:
            raise ValueError("policy must be the exact ClassificationPolicyV1 type")
        policy.validate()
        fixed = ClassificationPolicyV1()
        fixed.validate()
        canonical = fixed.canonical_json().decode("ascii")
        checksum = fixed.checksum()
        if checksum != CLASSIFICATION_POLICY_V1_CHECKSUM:
            raise ValueError("classification policy V1 checksum does not match the fixed checksum")
        effective_at = _canonical_utc_text(fixed.effective_at, "policy effective_at")

        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM fund_classification_policy_versions WHERE version = ?",
                    (fixed.version,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO fund_classification_policy_versions("
                        "version, canonical_policy_json, policy_checksum, effective_at, created_at"
                        ") VALUES (?, ?, ?, ?, ?)",
                        (fixed.version, canonical, checksum, effective_at, effective_at),
                    )
                    row = connection.execute(
                        "SELECT * FROM fund_classification_policy_versions WHERE version = ?",
                        (fixed.version,),
                    ).fetchone()
                record = self._row_to_policy(row)
                expected = StoredClassificationPolicy(
                    version=fixed.version,
                    canonical_policy_json=canonical,
                    policy_checksum=checksum,
                    effective_at=fixed.effective_at,
                    created_at=fixed.effective_at,
                )
                if record != expected:
                    raise RiskStoreError("classification policy version content conflict")
                connection.commit()
                return record
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def begin_document_refresh(self, fund_code: str, started_at: datetime) -> int:
        code = _fund_code(fund_code)
        started = _canonical_utc_text(started_at, "refresh started_at")
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO fund_document_refresh_runs(fund_code, started_at) VALUES (?, ?)",
                    (code, started),
                )
                refresh_id = _positive_integer(cursor.lastrowid, "refresh id")
                connection.commit()
                return refresh_id
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc

    def publish_document_selection(
        self,
        plan: DocumentSelectionPlan,
        created_at: datetime,
    ) -> StoredDocumentSelectionManifest:
        if type(plan) is not DocumentSelectionPlan:
            raise ValueError("document selection plan must be exact")
        plan.validate()
        created = _canonical_utc_text(created_at, "selection created_at")
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _authenticate_refresh(connection, plan.refresh_run_id, plan.fund_code)
                connection.execute(
                    "INSERT INTO fund_document_selection_manifests("
                    "refresh_run_id, fund_code, manifest_version, selection_policy_checksum, "
                    "canonical_json, selection_checksum, created_at"
                    ") VALUES (?, ?, 1, ?, ?, ?, ?)",
                    (
                        plan.refresh_run_id,
                        plan.fund_code,
                        plan.selection_policy_checksum,
                        plan.canonical_json,
                        plan.selection_checksum,
                        created,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM fund_document_selection_manifests WHERE refresh_run_id = ?",
                    (plan.refresh_run_id,),
                ).fetchone()
                stored = self._row_to_document_selection(connection, row)
                connection.commit()
                return stored
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def document_selection_for_refresh(
        self,
        refresh_run_id: int,
    ) -> Optional[StoredDocumentSelectionManifest]:
        refresh = _positive_integer(refresh_run_id, "refresh id")
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fund_document_selection_manifests WHERE refresh_run_id = ?",
                (refresh,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_document_selection(connection, row)

    def current_document_selection(
        self,
        fund_code: str,
    ) -> Optional[StoredDocumentSelectionManifest]:
        code = _fund_code(fund_code)
        with self._repository.connect() as connection:
            row = connection.execute(
                "SELECT manifest.* FROM fund_document_refresh_runs AS refresh "
                "JOIN fund_document_selection_manifests AS manifest "
                "ON manifest.refresh_run_id = refresh.id "
                "JOIN fund_document_refresh_completions AS completion "
                "ON completion.refresh_run_id = refresh.id "
                "WHERE refresh.fund_code = ? AND refresh.id = ("
                "SELECT MAX(latest.id) FROM fund_document_refresh_runs AS latest "
                "WHERE latest.fund_code = ?)",
                (code, code),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_document_selection(connection, row)

    def current_document_selection_snapshot(
        self,
        fund_code: str,
    ) -> Optional[CurrentDocumentSelectionSnapshot]:
        code = _fund_code(fund_code)
        try:
            with self._repository.connect() as connection:
                connection.execute("BEGIN")
                latest = connection.execute(
                    "SELECT refresh.id, completion.outcome "
                    "FROM fund_document_refresh_runs AS refresh "
                    "LEFT JOIN fund_document_refresh_completions AS completion "
                    "ON completion.refresh_run_id = refresh.id "
                    "WHERE refresh.fund_code = ? ORDER BY refresh.id DESC LIMIT 1",
                    (code,),
                ).fetchone()
                if latest is None or latest["outcome"] not in {"success", "partial"}:
                    connection.commit()
                    return None
                snapshot = self._document_selection_snapshot(
                    connection,
                    code,
                    _positive_integer(latest["id"], "refresh id"),
                )
                connection.commit()
                return snapshot
        except sqlite3.DatabaseError as exc:
            raise RiskStoreError("classification storage failed") from exc

    def document_selection_snapshot_for_refresh(
        self,
        fund_code: str,
        refresh_run_id: int,
    ) -> CurrentDocumentSelectionSnapshot:
        code = _fund_code(fund_code)
        refresh = _positive_integer(refresh_run_id, "refresh id")
        try:
            with self._repository.connect() as connection:
                connection.execute("BEGIN")
                snapshot = self._document_selection_snapshot(connection, code, refresh)
                connection.commit()
                return snapshot
        except sqlite3.DatabaseError as exc:
            raise RiskStoreError("classification storage failed") from exc

    def complete_document_refresh(
        self,
        refresh_id: int,
        outcome: RefreshOutcome,
        completed_at: datetime,
        *,
        failure: Optional[SafeDocumentFailure] = None,
    ) -> None:
        refresh = _positive_integer(refresh_id, "refresh id")
        if type(outcome) is not RefreshOutcome:
            raise ValueError("refresh outcome must be exact")
        completed = _canonical_utc_text(completed_at, "refresh completed_at")
        if outcome is RefreshOutcome.FAILED:
            if type(failure) is not SafeDocumentFailure:
                raise ValueError("failed refresh requires a safe document failure")
            failure.validate()
            failure_values = (
                failure.public_code,
                failure.stage.value,
                failure.reason_code.value,
            )
        else:
            if failure is not None:
                raise ValueError("successful refresh outcome cannot carry a failure")
            failure_values = (None, None, None)
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT started_at FROM fund_document_refresh_runs WHERE id = ?",
                    (refresh,),
                ).fetchone()
                if row is None:
                    raise RiskStoreError("document refresh is unavailable")
                started = _stored_datetime(row["started_at"], "refresh started_at")
                if completed_at < started:
                    raise ValueError("refresh completion cannot precede its start")
                connection.execute(
                    "INSERT INTO fund_document_refresh_completions("
                    "refresh_run_id, outcome, public_error_code, failure_stage, "
                    "failure_reason, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (refresh, outcome.value, *failure_values, completed),
                )
                connection.commit()
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def publish_candidate_success(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        parsed: ParsedRiskDocument,
        provenance: ParserProvenance,
        parser_input_sha256: str,
        attempted_at: datetime,
    ) -> ParsedDocumentRecord:
        refresh = _positive_integer(refresh_id, "refresh id")
        _validate_candidate(candidate)
        if type(parsed) is not ParsedRiskDocument or parsed.artifact.candidate != candidate:
            raise ValueError("parsed document must bind the exact candidate")
        _validate_provenance(provenance)
        parser_input = _digest(parser_input_sha256, "parser input checksum")
        attempted = _canonical_utc_text(attempted_at, "parse attempted_at")
        artifact_values, fact_values = _validated_parsed_document(
            parsed,
            parser_version=provenance.parser_version,
        )
        candidate_checksum = candidate_fingerprint(candidate)
        fact_set_checksum = canonical_fact_set_fingerprint(
            tuple(str(item[-1]) for item in fact_values)
        )
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _authenticate_refresh(connection, refresh, candidate.fund_code)
                artifact, _ = self._ensure_artifact(connection, artifact_values)
                stored_provenance = self._ensure_provenance(
                    connection,
                    provenance,
                    attempted_at,
                )
                result = self._ensure_parse_result(
                    connection,
                    artifact,
                    stored_provenance,
                    parser_input,
                    fact_set_checksum,
                    attempted_at,
                )
                facts = self._ensure_complete_fact_set(
                    connection,
                    artifact,
                    result,
                    fact_values,
                )
                run = self._append_parse_run(
                    connection,
                    artifact,
                    stored_provenance,
                    ParseRunKind.LIVE,
                    ParseRunOutcome.SUCCESS,
                    attempted_at,
                    parse_result=result,
                    failure=None,
                )
                self._append_candidate_run(
                    connection,
                    refresh,
                    candidate,
                    candidate_checksum,
                    ParseRunOutcome.SUCCESS,
                    attempted,
                    artifact=artifact,
                    parse_run=run,
                    failure=None,
                )
                connection.commit()
                return ParsedDocumentRecord(
                    artifact=artifact,
                    facts=facts,
                    provenance=stored_provenance,
                    parse_result=result,
                    parse_run=run,
                )
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def publish_candidate_failure(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        failure: SafeDocumentFailure,
        attempted_at: datetime,
        artifact: None,
        provenance: None,
    ) -> None:
        if artifact is not None or provenance is not None:
            raise ValueError("pre-parser candidate failure cannot bind parser state")
        refresh = _positive_integer(refresh_id, "refresh id")
        _validate_candidate(candidate)
        _validate_failure(failure)
        attempted = _canonical_utc_text(attempted_at, "candidate attempted_at")
        checksum = candidate_fingerprint(candidate)
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _authenticate_refresh(connection, refresh, candidate.fund_code)
                self._append_candidate_run(
                    connection,
                    refresh,
                    candidate,
                    checksum,
                    ParseRunOutcome.FAILED,
                    attempted,
                    artifact=None,
                    parse_run=None,
                    failure=failure,
                )
                connection.commit()
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def publish_candidate_parse_failure(
        self,
        *,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        artifact: RetrievedArtifact,
        provenance: ParserProvenance,
        failure: SafeDocumentFailure,
        attempted_at: datetime,
    ) -> StoredParseRun:
        refresh = _positive_integer(refresh_id, "refresh id")
        _validate_candidate(candidate)
        if type(artifact) is not RetrievedArtifact or artifact.candidate != candidate:
            raise ValueError("failed artifact must bind the exact candidate")
        _validate_provenance(provenance)
        _validate_failure(failure)
        attempted = _canonical_utc_text(attempted_at, "parse attempted_at")
        artifact_values = _validated_artifact_values(
            artifact,
            parse_status="failed",
            parser_version=provenance.parser_version,
            parse_error_code=failure.public_code,
        )
        checksum = candidate_fingerprint(candidate)
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _authenticate_refresh(connection, refresh, candidate.fund_code)
                stored_artifact, _ = self._ensure_artifact(connection, artifact_values)
                stored_provenance = self._ensure_provenance(
                    connection,
                    provenance,
                    attempted_at,
                )
                run = self._append_parse_run(
                    connection,
                    stored_artifact,
                    stored_provenance,
                    ParseRunKind.LIVE,
                    ParseRunOutcome.FAILED,
                    attempted_at,
                    parse_result=None,
                    failure=failure,
                )
                self._append_candidate_run(
                    connection,
                    refresh,
                    candidate,
                    checksum,
                    ParseRunOutcome.FAILED,
                    attempted,
                    artifact=stored_artifact,
                    parse_run=run,
                    failure=failure,
                )
                connection.commit()
                return run
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def publish_parsed_document(
        self, parsed: ParsedRiskDocument
    ) -> Tuple[StoredDocumentArtifact, Tuple[StoredFact, ...]]:
        provenance = native_parser_provenance()
        artifact_values, fact_values = _validated_parsed_document(
            parsed,
            parser_version=provenance.parser_version,
        )
        fact_set_checksum = canonical_fact_set_fingerprint(
            tuple(str(item[-1]) for item in fact_values)
        )
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                artifact, _ = self._ensure_artifact(connection, artifact_values)
                stored_provenance = self._ensure_provenance(
                    connection,
                    provenance,
                    parsed.artifact.retrieved_at,
                )
                result = self._ensure_parse_result(
                    connection,
                    artifact,
                    stored_provenance,
                    parsed.artifact.sha256,
                    fact_set_checksum,
                    parsed.artifact.retrieved_at,
                )
                facts = self._ensure_complete_fact_set(
                    connection,
                    artifact,
                    result,
                    fact_values,
                )
                self._append_parse_run(
                    connection,
                    artifact,
                    stored_provenance,
                    ParseRunKind.LIVE,
                    ParseRunOutcome.SUCCESS,
                    parsed.artifact.retrieved_at,
                    parse_result=result,
                    failure=None,
                )
                connection.commit()
                return artifact, facts
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def save_failed_artifact(
        self,
        artifact: RetrievedArtifact,
        *,
        parser_version: str,
        parse_error_code: str,
    ) -> StoredDocumentArtifact:
        artifact_values = _validated_artifact_values(
            artifact,
            parse_status="failed",
            parser_version=parser_version,
            parse_error_code=parse_error_code,
        )
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                stored, _ = self._ensure_artifact(connection, artifact_values)
                connection.commit()
                return stored
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def _document_selection_snapshot(
        self,
        connection: Any,
        fund_code: str,
        refresh_run_id: int,
    ) -> CurrentDocumentSelectionSnapshot:
        refresh = connection.execute(
            "SELECT refresh.fund_code, completion.outcome "
            "FROM fund_document_refresh_runs AS refresh "
            "LEFT JOIN fund_document_refresh_completions AS completion "
            "ON completion.refresh_run_id = refresh.id WHERE refresh.id = ?",
            (refresh_run_id,),
        ).fetchone()
        if refresh is None or refresh["fund_code"] != fund_code:
            raise RiskStoreError("document selection refresh binding changed")
        if refresh["outcome"] not in {"success", "partial"}:
            raise RiskStoreError("document selection refresh is not completed")
        selection_row = connection.execute(
            "SELECT * FROM fund_document_selection_manifests WHERE refresh_run_id = ?",
            (refresh_run_id,),
        ).fetchone()
        selection = self._row_to_document_selection(connection, selection_row)
        if selection.fund_code != fund_code:
            raise RiskStoreError("document selection fund binding changed")

        rows = connection.execute(
            "SELECT * FROM fund_document_candidate_runs "
            "WHERE refresh_run_id = ? ORDER BY id",
            (refresh_run_id,),
        ).fetchall()
        candidate_runs = tuple(
            self._row_to_selection_candidate_run(
                connection,
                row,
                fund_code=fund_code,
                refresh_run_id=refresh_run_id,
            )
            for row in rows
        )
        self._authenticate_selection_candidate_runs(selection, candidate_runs)
        selected_fingerprints = {
            state.selected_fingerprint
            for state in selection.periodic_states
            if state.state == "selected"
        }
        selected_periodic_records = tuple(
            run.parsed_record
            for run in candidate_runs
            if run.candidate_fingerprint in selected_fingerprints
            and run.outcome is ParseRunOutcome.SUCCESS
            and run.parsed_record is not None
        )
        nonperiodic_successful_records = tuple(
            run.parsed_record
            for run in candidate_runs
            if run.document_kind not in PERIODIC_DOCUMENT_KINDS
            and run.outcome is ParseRunOutcome.SUCCESS
            and run.parsed_record is not None
        )
        reason_codes = tuple(
            sorted(
                {
                    state.reason_code
                    for state in selection.periodic_states
                    if state.reason_code is not None
                }
            )
        )
        if any(code not in SELECTION_REASON_CODES for code in reason_codes):
            raise RiskStoreError("document selection reason projection changed")
        payload = {
            "candidate_runs": [
                {
                    "candidate_fingerprint": run.candidate_fingerprint,
                    "created_at": run.created_at.isoformat(),
                    "document_kind": run.document_kind.value,
                    "failure_reason": (
                        None if run.failure is None else run.failure.reason_code.value
                    ),
                    "failure_stage": None if run.failure is None else run.failure.stage.value,
                    "fund_code": run.fund_code,
                    "id": run.id,
                    "outcome": run.outcome.value,
                    "parse_run_id": run.parse_run_id,
                    "public_error_code": (
                        None if run.failure is None else run.failure.public_code
                    ),
                    "published_at": (
                        None if run.published_at is None else run.published_at.isoformat()
                    ),
                    "source_document_id": run.source_document_id,
                    "url": run.url,
                }
                for run in candidate_runs
            ],
            "fund_code": fund_code,
            "refresh_run_id": refresh_run_id,
        }
        checksum = hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()
        return CurrentDocumentSelectionSnapshot(
            selection=selection,
            candidate_runs=candidate_runs,
            selected_periodic_records=selected_periodic_records,
            nonperiodic_successful_records=nonperiodic_successful_records,
            candidate_run_snapshot_checksum=checksum,
            selection_reason_codes=reason_codes,
        )

    def _row_to_selection_candidate_run(
        self,
        connection: Any,
        row: Any,
        *,
        fund_code: str,
        refresh_run_id: int,
    ) -> StoredSelectionCandidateRun:
        try:
            run_id = _positive_integer(row["id"], "candidate run id")
            if _positive_integer(row["refresh_run_id"], "refresh id") != refresh_run_id:
                raise ValueError("candidate refresh binding changed")
            candidate_fund = _fund_code(row["fund_code"])
            if candidate_fund != fund_code:
                raise ValueError("candidate fund binding changed")
            fingerprint = _digest(row["candidate_fingerprint"], "candidate fingerprint")
            document_kind = DocumentKind(row["document_kind"])
            url = _required_text(row["url"], "candidate URL")
            validate_safe_https_url(url)
            published_at = _stored_optional_datetime(
                row["published_at"], "candidate published_at"
            )
            outcome = ParseRunOutcome(row["outcome"])
            source_document_id = _optional_positive_integer(
                row["source_document_id"], "source document id"
            )
            parse_run_id = _optional_positive_integer(row["parse_run_id"], "parse run id")
            created_at = _stored_datetime(row["created_at"], "candidate created_at")
            parsed_record = None
            failure = None
            if outcome is ParseRunOutcome.SUCCESS:
                if source_document_id is None or parse_run_id is None:
                    raise ValueError("successful candidate lacks parser bindings")
                if any(
                    row[key] is not None
                    for key in ("public_error_code", "failure_stage", "failure_reason")
                ):
                    raise ValueError("successful candidate carries a failure")
                parse_row = connection.execute(
                    "SELECT parse_result_id FROM fund_document_parse_runs WHERE id = ?",
                    (parse_run_id,),
                ).fetchone()
                if parse_row is None:
                    raise ValueError("candidate parse run is unavailable")
                parsed_record = self._load_parsed_record(
                    connection,
                    parse_row["parse_result_id"],
                    parse_run_id=parse_run_id,
                )
                if parsed_record.artifact.id != source_document_id:
                    raise ValueError("candidate artifact binding changed")
                self._authenticate_candidate_identity(
                    parsed_record.artifact,
                    fingerprint=fingerprint,
                    fund_code=fund_code,
                    document_kind=document_kind,
                    url=url,
                    published_at=published_at,
                )
            else:
                failure = SafeDocumentFailure(
                    public_code=_required_text(
                        row["public_error_code"], "candidate failure public code"
                    ),
                    stage=DocumentFailureStage(row["failure_stage"]),
                    reason_code=DocumentFailureReason(row["failure_reason"]),
                )
                failure.validate()
                if (source_document_id is None) != (parse_run_id is None):
                    raise ValueError("failed candidate parser bindings are partial")
                if source_document_id is not None and parse_run_id is not None:
                    artifact = self._row_to_artifact(
                        connection.execute(
                            "SELECT * FROM fund_document_artifacts WHERE id = ?",
                            (source_document_id,),
                        ).fetchone()
                    )
                    parse_run = self._row_to_parse_run(
                        connection.execute(
                            "SELECT * FROM fund_document_parse_runs WHERE id = ?",
                            (parse_run_id,),
                        ).fetchone()
                    )
                    self._row_to_provenance(
                        connection.execute(
                            "SELECT * FROM fund_document_parser_provenance WHERE id = ?",
                            (parse_run.provenance_id,),
                        ).fetchone()
                    )
                    if (
                        parse_run.source_document_id != source_document_id
                        or parse_run.outcome is not ParseRunOutcome.FAILED
                        or parse_run.public_error_code != failure.public_code
                        or parse_run.failure_stage is not failure.stage
                        or parse_run.failure_reason is not failure.reason_code
                    ):
                        raise RiskStoreError("candidate and parse failure binding changed")
                    self._authenticate_candidate_identity(
                        artifact,
                        fingerprint=fingerprint,
                        fund_code=fund_code,
                        document_kind=document_kind,
                        url=url,
                        published_at=published_at,
                    )
            return StoredSelectionCandidateRun(
                id=run_id,
                candidate_fingerprint=fingerprint,
                fund_code=candidate_fund,
                document_kind=document_kind,
                url=url,
                published_at=published_at,
                outcome=outcome,
                source_document_id=source_document_id,
                parse_run_id=parse_run_id,
                parsed_record=parsed_record,
                failure=failure,
                created_at=created_at,
            )
        except RiskStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStoreError("stored candidate run authentication failed") from exc

    def _authenticate_candidate_identity(
        self,
        artifact: StoredDocumentArtifact,
        *,
        fingerprint: str,
        fund_code: str,
        document_kind: DocumentKind,
        url: str,
        published_at: Optional[datetime],
    ) -> None:
        candidate = OfficialDocumentCandidate(
            fund_code=fund_code,
            document_kind=document_kind,
            title=artifact.title,
            url=url,
            publisher=artifact.publisher,
            published_at=published_at,
            source_tier=1,
        )
        _validate_candidate(candidate)
        if (
            artifact.fund_code != fund_code
            or artifact.document_kind is not document_kind
            or artifact.landing_url != url
            or artifact.published_at != published_at
            or candidate_fingerprint(candidate) != fingerprint
        ):
            raise RiskStoreError("stored candidate identity binding changed")

    def _authenticate_selection_candidate_runs(
        self,
        selection: StoredDocumentSelectionManifest,
        candidate_runs: Tuple[StoredSelectionCandidateRun, ...],
    ) -> None:
        periodic_runs = tuple(
            run for run in candidate_runs if run.document_kind in PERIODIC_DOCUMENT_KINDS
        )
        candidates_by_fingerprint = {
            candidate.candidate_fingerprint: candidate
            for candidate in selection.periodic_candidates
        }
        for run in periodic_runs:
            candidate = candidates_by_fingerprint.get(run.candidate_fingerprint)
            if (
                candidate is None
                or candidate.document_kind is not run.document_kind
                or candidate.url != run.url
                or candidate.published_at != run.published_at
            ):
                raise RiskStoreError("periodic candidate manifest binding changed")
        for state in selection.periodic_states:
            matching = tuple(
                run for run in periodic_runs if run.document_kind is state.document_kind
            )
            if state.state == "selected":
                if (
                    len(matching) != 1
                    or matching[0].candidate_fingerprint != state.selected_fingerprint
                ):
                    raise RiskStoreError("selected periodic candidate run binding changed")
            elif matching:
                raise RiskStoreError("unselected periodic candidate run is present")
        selected = {
            state.selected_fingerprint
            for state in selection.periodic_states
            if state.state == "selected"
        }
        if any(run.candidate_fingerprint not in selected for run in periodic_runs):
            raise RiskStoreError("extra periodic candidate run is present")

    def save_classification(
        self,
        classification: FundRiskClassification,
        evidence: ClassificationEvidence,
        policy: ClassificationPolicyV1,
    ) -> StoredClassification:
        values = _validated_classification_values(classification, evidence, policy)
        self.ensure_policy(policy)
        with self._repository.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                policy_row = connection.execute(
                    "SELECT * FROM fund_classification_policy_versions WHERE version = ?",
                    (classification.policy_version,),
                ).fetchone()
                self._row_to_policy(policy_row)
                latest = connection.execute(
                    "SELECT refresh.id, completion.outcome "
                    "FROM fund_document_refresh_runs AS refresh "
                    "LEFT JOIN fund_document_refresh_completions AS completion "
                    "ON completion.refresh_run_id = refresh.id "
                    "WHERE refresh.fund_code = ? ORDER BY refresh.id DESC LIMIT 1",
                    (classification.fund_code,),
                ).fetchone()
                if (
                    latest is None
                    or latest["outcome"] not in {"success", "partial"}
                    or latest["id"] != evidence.document_refresh_run_id
                ):
                    raise RiskStoreError("classification current selection binding changed")
                snapshot = self._document_selection_snapshot(
                    connection,
                    classification.fund_code,
                    evidence.document_refresh_run_id,
                )
                _, _, result_ids, provenance_checksums = _load_bound_evidence(
                    self,
                    connection,
                    classification.fund_code,
                    classification.evidence_document_ids,
                    classification.evidence_fact_ids,
                    evidence.parse_result_ids,
                )
                if result_ids != evidence.parse_result_ids:
                    raise RiskStoreError("classification parse result binding changed")
                if provenance_checksums != evidence.parser_provenance_checksums:
                    raise RiskStoreError("classification parser provenance binding changed")
                _authenticate_v3_evidence_snapshot(evidence, snapshot)
                row = connection.execute(
                    "SELECT * FROM fund_risk_classifications "
                    "WHERE fund_code = ? AND policy_version = ? AND input_fingerprint = ?",
                    (
                        classification.fund_code,
                        classification.policy_version,
                        classification.input_fingerprint,
                    ),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO fund_risk_classifications("
                        "fund_code, policy_version, input_fingerprint, input_manifest_json, "
                        "product_family, risk_bucket, portfolio_role, evidence_status, "
                        "evidence_tags_json, reason_codes_json, missing_evidence_json, "
                        "conflicts_json, evidence_document_ids_json, evidence_fact_ids_json, "
                        "freshness_json, classified_at, valid_until, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        values,
                    )
                    row = connection.execute(
                        "SELECT * FROM fund_risk_classifications "
                        "WHERE fund_code = ? AND policy_version = ? AND input_fingerprint = ?",
                        (
                            classification.fund_code,
                            classification.policy_version,
                            classification.input_fingerprint,
                        ),
                    ).fetchone()
                stored = self._row_to_classification(connection, row)
                if _classification_record_values(stored) != values:
                    raise RiskStoreError("classification fingerprint conflict")
                connection.commit()
                return stored
            except sqlite3.DatabaseError as exc:
                connection.rollback()
                raise RiskStoreError("classification storage failed") from exc
            except Exception:
                connection.rollback()
                raise

    def current_classification(self, fund_code: str) -> Optional[StoredClassification]:
        _fund_code(fund_code)
        history = self.classification_history(fund_code)
        return history[0] if history else None

    def parsed_document_history(self, fund_code: str) -> Tuple[ParsedDocumentRecord, ...]:
        _fund_code(fund_code)
        try:
            with self._repository.connect() as connection:
                rows = connection.execute(
                    "SELECT result.id AS result_id FROM fund_document_parse_results AS result "
                    "JOIN fund_document_artifacts AS artifact "
                    "ON artifact.id = result.source_document_id WHERE artifact.fund_code = ?",
                    (fund_code,),
                ).fetchall()
                records: List[ParsedDocumentRecord] = []
                for row in rows:
                    records.append(self._load_parsed_record(connection, row["result_id"]))
        except sqlite3.DatabaseError as exc:
            raise RiskStoreError("classification storage failed") from exc

        records.sort(key=lambda item: item.artifact.id, reverse=True)
        records.sort(key=lambda item: item.artifact.retrieved_at, reverse=True)
        records.sort(
            key=lambda item: (
                item.artifact.published_at is not None,
                item.artifact.published_at
                if item.artifact.published_at is not None
                else datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )
        records.sort(key=lambda item: item.artifact.document_kind.value)
        return tuple(records)

    def current_parsed_documents(
        self,
        fund_code: str,
        active_provenance_checksums: Tuple[str, ...],
    ) -> Tuple[ParsedDocumentRecord, ...]:
        code = _fund_code(fund_code)
        checksums = _digest_tuple(active_provenance_checksums, "active provenance checksums")
        if not checksums:
            return ()
        records = self._current_authenticated_records(code)
        return _sort_parsed_records(
            tuple(
                record for record in records if record.provenance.provenance_checksum in checksums
            )
        )

    def current_parser_requirements(
        self,
        fund_code: str,
    ) -> Tuple[StoredParserProvenance, ...]:
        code = _fund_code(fund_code)
        records = self._current_authenticated_records(code)
        requirements: Dict[str, StoredParserProvenance] = {}
        for record in records:
            checksum = record.provenance.provenance_checksum
            existing = requirements.get(checksum)
            if existing is not None and existing != record.provenance:
                raise RiskStoreError("current parser provenance binding changed")
            requirements[checksum] = record.provenance
        return tuple(requirements[key] for key in sorted(requirements))

    def _current_authenticated_records(
        self,
        fund_code: str,
    ) -> Tuple[ParsedDocumentRecord, ...]:
        code = _fund_code(fund_code)
        try:
            with self._repository.connect() as connection:
                refresh = connection.execute(
                    "SELECT refresh.id, completion.outcome "
                    "FROM fund_document_refresh_runs AS refresh "
                    "LEFT JOIN fund_document_refresh_completions AS completion "
                    "ON completion.refresh_run_id = refresh.id "
                    "WHERE refresh.fund_code = ? "
                    "ORDER BY refresh.id DESC LIMIT 1",
                    (code,),
                ).fetchone()
                if refresh is None or refresh["outcome"] not in {"success", "partial"}:
                    return ()
                rows = connection.execute(
                    "SELECT candidate.*, result.id AS result_id, "
                    "latest.latest_parse_run_id AS latest_parse_run_id, "
                    "provenance.provenance_checksum AS provenance_checksum "
                    "FROM fund_document_candidate_runs AS candidate "
                    "JOIN fund_document_parse_runs AS run ON run.id = candidate.parse_run_id "
                    "JOIN (SELECT source_document_id, provenance_id, MAX(id) "
                    "AS latest_parse_run_id FROM fund_document_parse_runs "
                    "GROUP BY source_document_id, provenance_id) AS latest "
                    "ON latest.source_document_id = run.source_document_id "
                    "AND latest.provenance_id = run.provenance_id "
                    "JOIN fund_document_parse_results AS result ON result.id = run.parse_result_id "
                    "JOIN fund_document_parser_provenance AS provenance "
                    "ON provenance.id = result.provenance_id "
                    "WHERE candidate.refresh_run_id = ? AND candidate.outcome = 'success' "
                    "AND run.outcome = 'success' AND run.id = latest.latest_parse_run_id "
                    "ORDER BY candidate.id",
                    (refresh["id"],),
                ).fetchall()
                records = []
                for row in rows:
                    record = self._load_parsed_record(
                        connection,
                        row["result_id"],
                        parse_run_id=row["parse_run_id"],
                    )
                    self._authenticate_current_candidate(
                        row,
                        record,
                        refresh_id=refresh["id"],
                        fund_code=code,
                    )
                    records.append(record)
        except sqlite3.DatabaseError as exc:
            raise RiskStoreError("classification storage failed") from exc
        return tuple(records)

    def classification_history(self, fund_code: str) -> Tuple[StoredClassification, ...]:
        _fund_code(fund_code)
        with self._repository.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fund_risk_classifications WHERE fund_code = ?",
                (fund_code,),
            ).fetchall()
            records = tuple(self._row_to_classification(connection, row) for row in rows)
        return tuple(
            sorted(
                records,
                key=lambda item: (item.classified_at, item.id),
                reverse=True,
            )
        )

    def classification_evidence(
        self,
        fund_code: str,
        classification_id: Optional[int] = None,
    ) -> Optional[ClassificationEvidenceRecord]:
        _fund_code(fund_code)
        if classification_id is not None:
            _positive_integer(classification_id, "classification id")
        with self._repository.connect() as connection:
            if classification_id is None:
                rows = connection.execute(
                    "SELECT * FROM fund_risk_classifications WHERE fund_code = ?",
                    (fund_code,),
                ).fetchall()
                if not rows:
                    return None
                authenticated = tuple(self._row_to_classification(connection, row) for row in rows)
                classification = max(
                    authenticated,
                    key=lambda item: (item.classified_at, item.id),
                )
            else:
                row = connection.execute(
                    "SELECT * FROM fund_risk_classifications WHERE fund_code = ? AND id = ?",
                    (fund_code, classification_id),
                ).fetchone()
                if row is None:
                    return None
                classification = self._row_to_classification(connection, row)
            policy_row = connection.execute(
                "SELECT * FROM fund_classification_policy_versions WHERE version = ?",
                (classification.policy_version,),
            ).fetchone()
            policy = self._row_to_policy(policy_row)
            manifest, version = _decode_manifest_envelope(classification.input_manifest_json)
            explicit_result_ids = (
                _manifest_id_tuple(manifest["parse_result_ids"], "parse result IDs")
                if version in {2, 3}
                else None
            )
            documents, facts, result_ids, provenance_checksums = _load_bound_evidence(
                self,
                connection,
                classification.fund_code,
                _decode_id_array(
                    classification.evidence_document_ids_json,
                    "evidence document IDs",
                ),
                _decode_id_array(
                    classification.evidence_fact_ids_json,
                    "evidence fact IDs",
                ),
                explicit_result_ids,
            )
            evidence = _evidence_from_manifest(
                classification.input_manifest_json,
                facts,
                result_ids,
                provenance_checksums,
            )
            if version == 3:
                snapshot = self._document_selection_snapshot(
                    connection,
                    classification.fund_code,
                    evidence.document_refresh_run_id,
                )
                _authenticate_v3_evidence_snapshot(evidence, snapshot)
            return ClassificationEvidenceRecord(
                classification=classification,
                policy=policy,
                documents=documents,
                facts=facts,
                evidence=evidence,
            )

    def _ensure_artifact(
        self, connection: Any, values: Tuple[object, ...]
    ) -> Tuple[StoredDocumentArtifact, bool]:
        fund_code, document_kind, url, sha256 = values[0], values[1], values[2], values[10]
        row = connection.execute(
            "SELECT * FROM fund_document_artifacts "
            "WHERE fund_code = ? AND document_kind = ? AND url = ? AND sha256 = ?",
            (fund_code, document_kind, url, sha256),
        ).fetchone()
        created = row is None
        if created:
            connection.execute(
                "INSERT INTO fund_document_artifacts("
                "fund_code, document_kind, url, landing_url, publisher, title, published_at, "
                "retrieved_at, content_type, byte_size, sha256, managed_path, "
                "parse_status, parser_version, parse_error_code"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            row = connection.execute(
                "SELECT * FROM fund_document_artifacts "
                "WHERE fund_code = ? AND document_kind = ? AND url = ? AND sha256 = ?",
                (fund_code, document_kind, url, sha256),
            ).fetchone()
        stored = self._row_to_artifact(row)
        if _artifact_record_values(stored)[:12] != values[:12]:
            raise RiskStoreError("artifact fingerprint conflict")
        return stored, created

    def _ensure_provenance(
        self,
        connection: Any,
        provenance: ParserProvenance,
        created_at: datetime,
    ) -> StoredParserProvenance:
        _validate_provenance(provenance)
        created = _canonical_utc_text(created_at, "provenance created_at")
        row = connection.execute(
            "SELECT * FROM fund_document_parser_provenance WHERE provenance_checksum = ?",
            (provenance.provenance_checksum,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO fund_document_parser_provenance("
                "parser_version, converter_kind, canonical_json, provenance_checksum, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    provenance.parser_version,
                    provenance.converter_kind,
                    provenance.canonical_json,
                    provenance.provenance_checksum,
                    created,
                ),
            )
            row = connection.execute(
                "SELECT * FROM fund_document_parser_provenance WHERE provenance_checksum = ?",
                (provenance.provenance_checksum,),
            ).fetchone()
        stored = self._row_to_provenance(row)
        if (
            stored.parser_version,
            stored.converter_kind,
            stored.canonical_json,
            stored.provenance_checksum,
        ) != (
            provenance.parser_version,
            provenance.converter_kind,
            provenance.canonical_json,
            provenance.provenance_checksum,
        ):
            raise RiskStoreError("parser provenance conflict")
        return stored

    def _ensure_parse_result(
        self,
        connection: Any,
        artifact: StoredDocumentArtifact,
        provenance: StoredParserProvenance,
        parser_input_sha256: str,
        fact_set_fingerprint: str,
        created_at: datetime,
    ) -> StoredParseResult:
        parser_input = _digest(parser_input_sha256, "parser input checksum")
        fact_set = _digest(fact_set_fingerprint, "fact set fingerprint")
        created = _canonical_utc_text(created_at, "parse result created_at")
        if provenance.converter_kind == "none" and parser_input != artifact.sha256:
            raise ValueError("native parser input must match the authenticated artifact")
        row = connection.execute(
            "SELECT * FROM fund_document_parse_results "
            "WHERE source_document_id = ? AND provenance_id = ?",
            (artifact.id, provenance.id),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO fund_document_parse_results("
                "source_document_id, provenance_id, parser_input_sha256, "
                "fact_set_fingerprint, created_at) VALUES (?, ?, ?, ?, ?)",
                (artifact.id, provenance.id, parser_input, fact_set, created),
            )
            row = connection.execute(
                "SELECT * FROM fund_document_parse_results "
                "WHERE source_document_id = ? AND provenance_id = ?",
                (artifact.id, provenance.id),
            ).fetchone()
        stored = self._row_to_parse_result(row)
        if (
            stored.source_document_id,
            stored.provenance_id,
            stored.parser_input_sha256,
            stored.fact_set_fingerprint,
        ) != (artifact.id, provenance.id, parser_input, fact_set):
            raise RiskStoreError("parse result conflict")
        return stored

    def _ensure_complete_fact_set(
        self,
        connection: Any,
        artifact: StoredDocumentArtifact,
        parse_result: StoredParseResult,
        values: Tuple[Tuple[object, ...], ...],
    ) -> Tuple[StoredFact, ...]:
        expected_by_fingerprint = {str(item[-1]): item for item in values}
        if len(expected_by_fingerprint) != len(values):
            raise ValueError("parsed facts must have unique fingerprints")

        for item in values:
            bound = (artifact.fund_code, artifact.id, parse_result.id, *item)
            row = connection.execute(
                "SELECT * FROM fund_mandate_facts "
                "WHERE parse_result_id = ? AND fact_fingerprint = ?",
                (parse_result.id, item[-1]),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO fund_mandate_facts("
                    "fund_code, source_document_id, parse_result_id, fact_kind, "
                    "normalized_value_json, "
                    "unit, page_number, section_name, source_excerpt, effective_from, "
                    "effective_to, confidence_state, parser_version, fact_fingerprint"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    bound,
                )
                row = connection.execute(
                    "SELECT * FROM fund_mandate_facts "
                    "WHERE parse_result_id = ? AND fact_fingerprint = ?",
                    (parse_result.id, item[-1]),
                ).fetchone()
            stored = self._row_to_fact(row)
            if _fact_record_values(stored) != bound:
                raise RiskStoreError("fact fingerprint conflict")

        rows = connection.execute(
            "SELECT * FROM fund_mandate_facts WHERE parse_result_id = ? ORDER BY id",
            (parse_result.id,),
        ).fetchall()
        stored_facts = tuple(self._row_to_fact(row) for row in rows)
        actual_by_fingerprint = {
            fact.fact_fingerprint: _fact_record_values(fact)[3:] for fact in stored_facts
        }
        if actual_by_fingerprint != expected_by_fingerprint:
            raise RiskStoreError("published fact set conflict")
        if canonical_fact_set_fingerprint(tuple(actual_by_fingerprint)) != (
            parse_result.fact_set_fingerprint
        ):
            raise RiskStoreError("stored parse result fact set changed")
        return stored_facts

    def _append_parse_run(
        self,
        connection: Any,
        artifact: StoredDocumentArtifact,
        provenance: StoredParserProvenance,
        run_kind: ParseRunKind,
        outcome: ParseRunOutcome,
        attempted_at: object,
        *,
        parse_result: Optional[StoredParseResult],
        failure: Optional[SafeDocumentFailure],
    ) -> StoredParseRun:
        if type(run_kind) is not ParseRunKind or type(outcome) is not ParseRunOutcome:
            raise ValueError("parse run kind and outcome must be exact")
        attempted = _canonical_utc_text(attempted_at, "parse attempted_at")
        if outcome is ParseRunOutcome.SUCCESS:
            if type(parse_result) is not StoredParseResult or failure is not None:
                raise ValueError("successful parse run requires only a parse result")
            values = (parse_result.id, None, None, None)
        else:
            if parse_result is not None or type(failure) is not SafeDocumentFailure:
                raise ValueError("failed parse run requires only a safe failure")
            failure.validate()
            values = (
                None,
                failure.public_code,
                failure.stage.value,
                failure.reason_code.value,
            )
        cursor = connection.execute(
            "INSERT INTO fund_document_parse_runs("
            "source_document_id, provenance_id, run_kind, outcome, parse_result_id, "
            "public_error_code, failure_stage, failure_reason, attempted_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact.id, provenance.id, run_kind.value, outcome.value, *values, attempted),
        )
        return self._row_to_parse_run(
            connection.execute(
                "SELECT * FROM fund_document_parse_runs WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        )

    def _append_candidate_run(
        self,
        connection: Any,
        refresh_id: int,
        candidate: OfficialDocumentCandidate,
        fingerprint: str,
        outcome: ParseRunOutcome,
        created_at: str,
        *,
        artifact: Optional[StoredDocumentArtifact],
        parse_run: Optional[StoredParseRun],
        failure: Optional[SafeDocumentFailure],
    ) -> None:
        if outcome is ParseRunOutcome.SUCCESS:
            if artifact is None or parse_run is None or failure is not None:
                raise ValueError("successful candidate requires parser bindings")
            failure_values = (None, None, None)
        else:
            if type(failure) is not SafeDocumentFailure:
                raise ValueError("failed candidate requires a safe failure")
            failure.validate()
            if (artifact is None) != (parse_run is None):
                raise ValueError("failed candidate parser bindings are all-or-none")
            failure_values = (
                failure.public_code,
                failure.stage.value,
                failure.reason_code.value,
            )
        connection.execute(
            "INSERT INTO fund_document_candidate_runs("
            "refresh_run_id, candidate_fingerprint, fund_code, document_kind, url, "
            "published_at, outcome, source_document_id, parse_run_id, public_error_code, "
            "failure_stage, failure_reason, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                refresh_id,
                fingerprint,
                candidate.fund_code,
                candidate.document_kind.value,
                candidate.url,
                None if candidate.published_at is None else candidate.published_at.isoformat(),
                outcome.value,
                None if artifact is None else artifact.id,
                None if parse_run is None else parse_run.id,
                *failure_values,
                created_at,
            ),
        )

    def _load_parsed_record(
        self,
        connection: Any,
        parse_result_id: object,
        *,
        parse_run_id: Optional[object] = None,
    ) -> ParsedDocumentRecord:
        result = self._row_to_parse_result(
            connection.execute(
                "SELECT * FROM fund_document_parse_results WHERE id = ?",
                (_positive_integer(parse_result_id, "parse result id"),),
            ).fetchone()
        )
        provenance = self._row_to_provenance(
            connection.execute(
                "SELECT * FROM fund_document_parser_provenance WHERE id = ?",
                (result.provenance_id,),
            ).fetchone()
        )
        artifact = self._row_to_artifact(
            connection.execute(
                "SELECT * FROM fund_document_artifacts WHERE id = ?",
                (result.source_document_id,),
            ).fetchone()
        )
        if provenance.converter_kind == "none" and result.parser_input_sha256 != artifact.sha256:
            raise RiskStoreError("native parser input binding changed")
        facts = tuple(
            self._row_to_fact(row)
            for row in connection.execute(
                "SELECT * FROM fund_mandate_facts WHERE parse_result_id = ? ORDER BY id",
                (result.id,),
            ).fetchall()
        )
        if provenance.parser_version != next(
            (fact.parser_version for fact in facts), provenance.parser_version
        ) or any(
            fact.parse_result_id != result.id
            or fact.source_document_id != artifact.id
            or fact.fund_code != artifact.fund_code
            or fact.parser_version != provenance.parser_version
            for fact in facts
        ):
            raise RiskStoreError("parsed result fact binding changed")
        if canonical_fact_set_fingerprint(tuple(fact.fact_fingerprint for fact in facts)) != (
            result.fact_set_fingerprint
        ):
            raise RiskStoreError("stored parse result fact set changed")
        if parse_run_id is None:
            run_row = connection.execute(
                "SELECT * FROM fund_document_parse_runs "
                "WHERE parse_result_id = ? AND outcome = 'success' ORDER BY id DESC LIMIT 1",
                (result.id,),
            ).fetchone()
        else:
            run_row = connection.execute(
                "SELECT * FROM fund_document_parse_runs WHERE id = ?",
                (_positive_integer(parse_run_id, "parse run id"),),
            ).fetchone()
        run = self._row_to_parse_run(run_row)
        if (
            run.source_document_id != artifact.id
            or run.provenance_id != provenance.id
            or run.parse_result_id != result.id
        ):
            raise RiskStoreError("parsed result run binding changed")
        return ParsedDocumentRecord(
            artifact=artifact,
            facts=facts,
            provenance=provenance,
            parse_result=result,
            parse_run=run,
        )

    def _authenticate_current_candidate(
        self,
        row: Any,
        record: ParsedDocumentRecord,
        *,
        refresh_id: object,
        fund_code: str,
    ) -> None:
        try:
            candidate = OfficialDocumentCandidate(
                fund_code=_fund_code(row["fund_code"]),
                document_kind=DocumentKind(row["document_kind"]),
                title=record.artifact.title,
                url=_required_text(row["url"], "candidate URL"),
                publisher=record.artifact.publisher,
                published_at=_stored_optional_datetime(
                    row["published_at"], "candidate published_at"
                ),
                source_tier=1,
            )
            _validate_candidate(candidate)
            if (
                _positive_integer(row["refresh_run_id"], "refresh id")
                != _positive_integer(refresh_id, "refresh id")
                or candidate.fund_code != fund_code
                or row["outcome"] != ParseRunOutcome.SUCCESS.value
                or row["public_error_code"] is not None
                or row["failure_stage"] is not None
                or row["failure_reason"] is not None
                or _positive_integer(row["source_document_id"], "source document id")
                != record.artifact.id
                or _positive_integer(row["parse_run_id"], "parse run id") != record.parse_run.id
                or _positive_integer(row["latest_parse_run_id"], "latest parse run id")
                != record.parse_run.id
                or _positive_integer(row["result_id"], "parse result id") != record.parse_result.id
                or _digest(row["provenance_checksum"], "parser provenance checksum")
                != record.provenance.provenance_checksum
                or record.artifact.fund_code != candidate.fund_code
                or record.artifact.document_kind is not candidate.document_kind
                or record.artifact.landing_url != candidate.url
                or record.artifact.published_at != candidate.published_at
                or _digest(row["candidate_fingerprint"], "candidate fingerprint")
                != candidate_fingerprint(candidate)
            ):
                raise RiskStoreError("current candidate binding changed")
            _stored_datetime(row["created_at"], "candidate created_at")
        except RiskStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStoreError("current candidate authentication failed") from exc

    def _row_to_artifact(self, row: Any) -> StoredDocumentArtifact:
        if row is None:
            raise RiskStoreError("stored artifact is unavailable")
        try:
            stored = StoredDocumentArtifact(
                id=_positive_integer(row["id"], "artifact id"),
                fund_code=_fund_code(row["fund_code"]),
                document_kind=DocumentKind(row["document_kind"]),
                url=_required_text(row["url"], "artifact URL"),
                landing_url=_required_text(row["landing_url"], "artifact landing URL"),
                publisher=_required_text(row["publisher"], "artifact publisher"),
                title=_required_text(row["title"], "artifact title"),
                published_at=_stored_optional_datetime(row["published_at"], "published_at"),
                retrieved_at=_stored_datetime(row["retrieved_at"], "retrieved_at"),
                content_type=_required_text(row["content_type"], "artifact content type"),
                byte_size=_positive_integer(row["byte_size"], "artifact byte size"),
                sha256=_digest(row["sha256"], "artifact checksum"),
                managed_path=_stored_path(row["managed_path"]),
                parse_status=_parse_status(row["parse_status"]),
                parser_version=_stable_code(row["parser_version"], "parser version", version=True),
                parse_error_code=_optional_stable_code(row["parse_error_code"]),
            )
            _authenticate_stored_artifact(stored)
            return stored
        except RiskStoreError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise RiskStoreError("stored artifact authentication failed") from exc

    def _row_to_document_selection(
        self,
        connection: Any,
        row: Any,
    ) -> StoredDocumentSelectionManifest:
        if row is None:
            raise RiskStoreError("stored document selection is unavailable")
        try:
            refresh_run_id = _positive_integer(row["refresh_run_id"], "selection refresh id")
            fund_code = _fund_code(row["fund_code"])
            manifest_version = row["manifest_version"]
            if type(manifest_version) is not int or manifest_version != 1:
                raise ValueError("stored document selection version is unknown")
            selection_policy_checksum = _digest(
                row["selection_policy_checksum"],
                "selection policy checksum",
            )
            if selection_policy_checksum != SELECTION_POLICY_V1_CHECKSUM:
                raise ValueError("stored document selection policy is unknown")
            canonical_json = _required_text(row["canonical_json"], "selection canonical JSON")
            payload = _decode_canonical_object(canonical_json, "document selection manifest")
            selection_checksum = _digest(row["selection_checksum"], "selection checksum")
            if hashlib.sha256(canonical_json.encode("ascii")).hexdigest() != selection_checksum:
                raise ValueError("stored document selection checksum does not match content")
            created_at = _stored_datetime(row["created_at"], "selection created_at")
            candidates, states = _decode_document_selection_payload(
                payload,
                refresh_run_id=refresh_run_id,
                fund_code=fund_code,
                selection_policy_checksum=selection_policy_checksum,
            )
            refresh_row = connection.execute(
                "SELECT fund_code FROM fund_document_refresh_runs WHERE id = ?",
                (refresh_run_id,),
            ).fetchone()
            if refresh_row is None or refresh_row["fund_code"] != fund_code:
                raise ValueError("stored document selection refresh binding changed")
            return StoredDocumentSelectionManifest(
                refresh_run_id=refresh_run_id,
                fund_code=fund_code,
                manifest_version=manifest_version,
                selection_policy_checksum=selection_policy_checksum,
                canonical_json=canonical_json,
                selection_checksum=selection_checksum,
                created_at=created_at,
                periodic_candidates=candidates,
                periodic_states=states,
            )
        except RiskStoreError:
            raise
        except (KeyError, UnicodeError, TypeError, ValueError) as exc:
            raise RiskStoreError("stored document selection authentication failed") from exc

    def _row_to_fact(self, row: Any) -> StoredFact:
        if row is None:
            raise RiskStoreError("stored fact is unavailable")
        try:
            normalized_json = row["normalized_value_json"]
            normalized = decode_fact_value_json(normalized_json)
            confidence = FactConfidence(row["confidence_state"])
            stored = StoredFact(
                id=_positive_integer(row["id"], "fact id"),
                fund_code=_fund_code(row["fund_code"]),
                source_document_id=_positive_integer(
                    row["source_document_id"], "source document id"
                ),
                parse_result_id=_positive_integer(row["parse_result_id"], "parse result id"),
                fact_kind=_stable_code(row["fact_kind"], "fact kind"),
                normalized_value_json=normalized_json,
                unit=_optional_text(row["unit"], "fact unit"),
                page_number=_optional_positive_integer(row["page_number"], "page number"),
                section_name=_optional_text(row["section_name"], "section name"),
                source_excerpt=_required_text(row["source_excerpt"], "source excerpt"),
                effective_from=_stored_optional_date(row["effective_from"], "effective_from"),
                effective_to=_stored_optional_date(row["effective_to"], "effective_to"),
                confidence_state=confidence,
                parser_version=_stable_code(row["parser_version"], "parser version", version=True),
                fact_fingerprint=_digest(row["fact_fingerprint"], "fact fingerprint"),
            )
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
            mandate.validate()
            expected_fingerprint = fact_fingerprint(
                fact_kind=stored.fact_kind,
                normalized_value=normalized,
                unit=stored.unit,
                page_number=stored.page_number,
                section_name=stored.section_name,
                source_excerpt=stored.source_excerpt,
                effective_from=stored.effective_from,
                effective_to=stored.effective_to,
                confidence_state=stored.confidence_state,
            )
            if stored.fact_fingerprint != expected_fingerprint:
                raise RiskStoreError("stored fact fingerprint does not match content")
            return stored
        except RiskStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise RiskStoreError("stored fact authentication failed") from exc

    def _row_to_provenance(self, row: Any) -> StoredParserProvenance:
        if row is None:
            raise RiskStoreError("stored parser provenance is unavailable")
        try:
            record = StoredParserProvenance(
                id=_positive_integer(row["id"], "provenance id"),
                parser_version=_stable_code(row["parser_version"], "parser version", version=True),
                converter_kind=_required_text(row["converter_kind"], "converter kind"),
                canonical_json=_required_text(row["canonical_json"], "provenance JSON"),
                provenance_checksum=_digest(row["provenance_checksum"], "provenance checksum"),
                created_at=_stored_datetime(row["created_at"], "provenance created_at"),
            )
            parsed = ParserProvenance(
                parser_version=record.parser_version,
                converter_kind=record.converter_kind,
                canonical_json=record.canonical_json,
                provenance_checksum=record.provenance_checksum,
            )
            parsed.validate()
            return record
        except (TypeError, ValueError) as exc:
            raise RiskStoreError("stored parser provenance authentication failed") from exc

    def _row_to_parse_result(self, row: Any) -> StoredParseResult:
        if row is None:
            raise RiskStoreError("stored parse result is unavailable")
        try:
            return StoredParseResult(
                id=_positive_integer(row["id"], "parse result id"),
                source_document_id=_positive_integer(
                    row["source_document_id"], "source document id"
                ),
                provenance_id=_positive_integer(row["provenance_id"], "provenance id"),
                parser_input_sha256=_digest(row["parser_input_sha256"], "parser input checksum"),
                fact_set_fingerprint=_digest(row["fact_set_fingerprint"], "fact set fingerprint"),
                created_at=_stored_datetime(row["created_at"], "parse result created_at"),
            )
        except (TypeError, ValueError) as exc:
            raise RiskStoreError("stored parse result authentication failed") from exc

    def _row_to_parse_run(self, row: Any) -> StoredParseRun:
        if row is None:
            raise RiskStoreError("stored parse run is unavailable")
        try:
            record = StoredParseRun(
                id=_positive_integer(row["id"], "parse run id"),
                source_document_id=_positive_integer(
                    row["source_document_id"], "source document id"
                ),
                provenance_id=_positive_integer(row["provenance_id"], "provenance id"),
                run_kind=ParseRunKind(row["run_kind"]),
                outcome=ParseRunOutcome(row["outcome"]),
                parse_result_id=_optional_positive_integer(
                    row["parse_result_id"], "parse result id"
                ),
                public_error_code=_optional_stable_code(row["public_error_code"]),
                failure_stage=(
                    None
                    if row["failure_stage"] is None
                    else DocumentFailureStage(row["failure_stage"])
                ),
                failure_reason=(
                    None
                    if row["failure_reason"] is None
                    else DocumentFailureReason(row["failure_reason"])
                ),
                attempted_at=_stored_datetime(row["attempted_at"], "parse attempted_at"),
            )
            if record.outcome is ParseRunOutcome.SUCCESS:
                if (
                    record.parse_result_id is None
                    or record.public_error_code is not None
                    or record.failure_stage is not None
                    or record.failure_reason is not None
                ):
                    raise ValueError("successful parse run binding is invalid")
            elif record.run_kind is ParseRunKind.LIVE:
                failure = SafeDocumentFailure(
                    public_code=_required_text(
                        record.public_error_code,
                        "parse failure public code",
                    ),
                    stage=record.failure_stage,
                    reason_code=record.failure_reason,
                )
                failure.validate()
            elif record.parse_result_id is not None or record.public_error_code is None:
                raise ValueError("legacy failed parse run binding is invalid")
            return record
        except (TypeError, ValueError) as exc:
            raise RiskStoreError("stored parse run authentication failed") from exc

    def _row_to_policy(self, row: Any) -> StoredClassificationPolicy:
        if row is None:
            raise RiskStoreError("stored classification policy is unavailable")
        try:
            canonical = _required_text(row["canonical_policy_json"], "canonical policy JSON")
            parsed = json.loads(canonical)
            if type(parsed) is not dict or _canonical_json(parsed) != canonical:
                raise ValueError("stored policy JSON is not canonical")
            checksum = _digest(row["policy_checksum"], "policy checksum")
            if hashlib.sha256(canonical.encode("ascii")).hexdigest() != checksum:
                raise ValueError("stored policy checksum does not match content")
            fixed = ClassificationPolicyV1()
            if (
                row["version"] != fixed.version
                or canonical != fixed.canonical_json().decode("ascii")
                or checksum != CLASSIFICATION_POLICY_V1_CHECKSUM
            ):
                raise ValueError("stored policy does not match fixed V1")
            record = StoredClassificationPolicy(
                version=fixed.version,
                canonical_policy_json=canonical,
                policy_checksum=checksum,
                effective_at=_stored_datetime(row["effective_at"], "policy effective_at"),
                created_at=_stored_datetime(row["created_at"], "policy created_at"),
            )
            if record.effective_at != fixed.effective_at or record.created_at != fixed.effective_at:
                raise ValueError("stored policy times do not match fixed V1")
            return record
        except (UnicodeError, TypeError, ValueError) as exc:
            raise RiskStoreError("stored classification policy authentication failed") from exc

    def _row_to_classification(self, connection: Any, row: Any) -> StoredClassification:
        if row is None:
            raise RiskStoreError("stored classification is unavailable")
        try:
            record = StoredClassification(
                id=_positive_integer(row["id"], "classification id"),
                fund_code=_fund_code(row["fund_code"]),
                policy_version=_stable_code(
                    row["policy_version"], "classification policy version", version=True
                ),
                input_fingerprint=_digest(
                    row["input_fingerprint"], "classification input fingerprint"
                ),
                input_manifest_json=_required_text(
                    row["input_manifest_json"], "classification input manifest"
                ),
                product_family=ProductFamily(row["product_family"]),
                risk_bucket=RiskBucket(row["risk_bucket"]),
                portfolio_role=PortfolioRole(row["portfolio_role"]),
                evidence_status=EvidenceStatus(row["evidence_status"]),
                evidence_tags_json=_required_text(row["evidence_tags_json"], "evidence tags JSON"),
                reason_codes_json=_required_text(row["reason_codes_json"], "reason codes JSON"),
                missing_evidence_json=_required_text(
                    row["missing_evidence_json"], "missing evidence JSON"
                ),
                conflicts_json=_required_text(row["conflicts_json"], "conflicts JSON"),
                evidence_document_ids_json=_required_text(
                    row["evidence_document_ids_json"], "evidence document IDs JSON"
                ),
                evidence_fact_ids_json=_required_text(
                    row["evidence_fact_ids_json"], "evidence fact IDs JSON"
                ),
                freshness_json=_required_text(row["freshness_json"], "freshness JSON"),
                classified_at=_stored_datetime(row["classified_at"], "classified_at"),
                valid_until=_stored_datetime(row["valid_until"], "valid_until"),
                created_at=_stored_datetime(row["created_at"], "created_at"),
            )
            policy_row = connection.execute(
                "SELECT * FROM fund_classification_policy_versions WHERE version = ?",
                (record.policy_version,),
            ).fetchone()
            policy_record = self._row_to_policy(policy_row)
            manifest_payload, manifest_version = _decode_manifest_envelope(
                record.input_manifest_json
            )
            explicit_result_ids = (
                _manifest_id_tuple(
                    manifest_payload["parse_result_ids"],
                    "parse result IDs",
                )
                if manifest_version in {2, 3}
                else None
            )
            documents, facts, result_ids, provenance_checksums = _load_bound_evidence(
                self,
                connection,
                record.fund_code,
                _decode_id_array(record.evidence_document_ids_json, "evidence document IDs"),
                _decode_id_array(record.evidence_fact_ids_json, "evidence fact IDs"),
                explicit_result_ids,
            )
            del documents
            evidence = _evidence_from_manifest(
                record.input_manifest_json,
                facts,
                result_ids,
                provenance_checksums,
            )
            if manifest_version == 3:
                snapshot = self._document_selection_snapshot(
                    connection,
                    record.fund_code,
                    evidence.document_refresh_run_id,
                )
                _authenticate_v3_evidence_snapshot(evidence, snapshot)
            policy = ClassificationPolicyV1()
            if policy_record.policy_checksum != policy.checksum():
                raise ValueError("classification policy checksum binding changed")
            if manifest_version == 1:
                manifest = classification_input_manifest_v1(
                    evidence, policy, record.classified_at
                )
            elif manifest_version == 2:
                manifest = classification_input_manifest_v2(
                    evidence, policy, record.classified_at
                )
            else:
                manifest = classification_input_manifest_v3(
                    evidence, policy, record.classified_at
                )
            canonical_manifest = _canonical_json(manifest)
            if canonical_manifest != record.input_manifest_json:
                raise ValueError("classification input manifest is not canonical")
            if hashlib.sha256(canonical_manifest.encode("ascii")).hexdigest() != (
                record.input_fingerprint
            ):
                raise ValueError("classification input fingerprint does not match manifest")
            result = _classification_from_record(record)
            expected_result = classify_fund(evidence, policy, record.classified_at)
            if manifest_version == 1:
                expected_result = replace(
                    expected_result,
                    input_fingerprint=record.input_fingerprint,
                )
            if result != expected_result:
                raise ValueError("stored classification does not match deterministic engine output")
            if result.fund_code != evidence.fund_code:
                raise ValueError("classification evidence fund binding changed")
            if result.policy_version != policy.version:
                raise ValueError("classification policy version binding changed")
            if result.evidence_document_ids != evidence.document_ids:
                raise ValueError("classification document binding changed")
            if result.evidence_fact_ids != evidence.fact_ids:
                raise ValueError("classification fact binding changed")
            if result.freshness != evidence.freshness:
                raise ValueError("classification freshness binding changed")
            if record.created_at != record.classified_at:
                raise ValueError("classification created_at binding changed")
            return record
        except RiskStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise RiskStoreError("classification storage authentication failed") from exc


def _validated_classification_values(
    classification: FundRiskClassification,
    evidence: ClassificationEvidence,
    policy: ClassificationPolicyV1,
) -> Tuple[object, ...]:
    if type(classification) is not FundRiskClassification:
        raise ValueError("classification must be the exact FundRiskClassification type")
    if type(evidence) is not ClassificationEvidence:
        raise ValueError("evidence must be the exact ClassificationEvidence type")
    if type(policy) is not ClassificationPolicyV1:
        raise ValueError("policy must be the exact ClassificationPolicyV1 type")
    classification.validate()
    evidence.validate()
    policy.validate()
    if evidence.document_refresh_run_id is None:
        raise RiskStoreError("new classification save requires manifest v3 evidence")
    manifest = classification_input_manifest(evidence, policy, classification.classified_at)
    manifest_json = _canonical_json(manifest)
    fingerprint = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
    if fingerprint != classification.input_fingerprint:
        raise ValueError("classification input fingerprint does not authenticate its manifest")
    if classification.fund_code != evidence.fund_code:
        raise ValueError("classification and evidence fund code must match")
    if classification.policy_version != policy.version:
        raise ValueError("classification and policy version must match")
    if classification.evidence_document_ids != evidence.document_ids:
        raise ValueError("classification document IDs must match evidence")
    if classification.evidence_fact_ids != evidence.fact_ids:
        raise ValueError("classification fact IDs must match evidence")
    if classification.freshness != evidence.freshness:
        raise ValueError("classification freshness must match evidence")
    expected = classify_fund(evidence, policy, classification.classified_at)
    if classification != expected:
        raise RiskStoreError("classification fingerprint conflict")
    return (
        classification.fund_code,
        classification.policy_version,
        classification.input_fingerprint,
        manifest_json,
        classification.product_family.value,
        classification.risk_bucket.value,
        classification.portfolio_role.value,
        classification.evidence_status.value,
        _canonical_json(list(classification.evidence_tags)),
        _canonical_json(list(classification.reason_codes)),
        _canonical_json(list(classification.missing_evidence)),
        _canonical_json(list(classification.conflicts)),
        _canonical_json(list(classification.evidence_document_ids)),
        _canonical_json(list(classification.evidence_fact_ids)),
        _canonical_json(manifest["freshness"]),
        classification.classified_at.isoformat(),
        classification.valid_until.isoformat(),
        classification.classified_at.isoformat(),
    )


def _classification_record_values(record: StoredClassification) -> Tuple[object, ...]:
    return (
        record.fund_code,
        record.policy_version,
        record.input_fingerprint,
        record.input_manifest_json,
        record.product_family.value,
        record.risk_bucket.value,
        record.portfolio_role.value,
        record.evidence_status.value,
        record.evidence_tags_json,
        record.reason_codes_json,
        record.missing_evidence_json,
        record.conflicts_json,
        record.evidence_document_ids_json,
        record.evidence_fact_ids_json,
        record.freshness_json,
        record.classified_at.isoformat(),
        record.valid_until.isoformat(),
        record.created_at.isoformat(),
    )


def _classification_from_record(record: StoredClassification) -> FundRiskClassification:
    result = FundRiskClassification(
        fund_code=record.fund_code,
        policy_version=record.policy_version,
        input_fingerprint=record.input_fingerprint,
        product_family=record.product_family,
        risk_bucket=record.risk_bucket,
        portfolio_role=record.portfolio_role,
        evidence_status=record.evidence_status,
        evidence_tags=_decode_code_array(record.evidence_tags_json, "evidence tags"),
        reason_codes=_decode_code_array(record.reason_codes_json, "reason codes"),
        missing_evidence=_decode_code_array(record.missing_evidence_json, "missing evidence"),
        conflicts=_decode_code_array(record.conflicts_json, "conflicts"),
        evidence_document_ids=_decode_id_array(
            record.evidence_document_ids_json, "evidence document IDs"
        ),
        evidence_fact_ids=_decode_id_array(record.evidence_fact_ids_json, "evidence fact IDs"),
        freshness=_decode_freshness(record.freshness_json),
        classified_at=record.classified_at,
        valid_until=record.valid_until,
    )
    result.validate()
    return result


def _load_bound_evidence(
    store: FundRiskStore,
    connection: Any,
    fund_code: str,
    document_ids: Tuple[int, ...],
    fact_ids: Tuple[int, ...],
    explicit_parse_result_ids: Optional[Tuple[int, ...]] = None,
) -> Tuple[
    Tuple[StoredDocumentArtifact, ...],
    Tuple[StoredFact, ...],
    Tuple[int, ...],
    Tuple[str, ...],
]:
    if document_ids != tuple(sorted(set(document_ids))) or any(
        type(item) is not int or item <= 0 for item in document_ids
    ):
        raise ValueError("evidence document IDs must be positive, sorted, and unique")
    if fact_ids != tuple(sorted(set(fact_ids))) or any(
        type(item) is not int or item <= 0 for item in fact_ids
    ):
        raise ValueError("evidence fact IDs must be positive, sorted, and unique")
    if explicit_parse_result_ids is not None and (
        explicit_parse_result_ids != tuple(sorted(set(explicit_parse_result_ids)))
        or any(type(item) is not int or item <= 0 for item in explicit_parse_result_ids)
    ):
        raise ValueError("parse result IDs must be positive, sorted, and unique")
    documents: List[StoredDocumentArtifact] = []
    for document_id in document_ids:
        row = connection.execute(
            "SELECT * FROM fund_document_artifacts WHERE id = ?", (document_id,)
        ).fetchone()
        document = store._row_to_artifact(row)
        if document.fund_code != fund_code:
            raise RiskStoreError("classification evidence document fund binding changed")
        documents.append(document)
    document_id_set = set(document_ids)
    facts: List[StoredFact] = []
    for fact_id in fact_ids:
        row = connection.execute(
            "SELECT * FROM fund_mandate_facts WHERE id = ?", (fact_id,)
        ).fetchone()
        fact = store._row_to_fact(row)
        if fact.fund_code != fund_code or fact.source_document_id not in document_id_set:
            raise RiskStoreError("classification evidence fact fund binding changed")
        facts.append(fact)
    fact_result_ids = tuple(sorted({fact.parse_result_id for fact in facts}))
    result_ids = fact_result_ids if explicit_parse_result_ids is None else explicit_parse_result_ids
    provenances = set()
    result_document_ids = []
    for result_id in sorted(result_ids):
        record = store._load_parsed_record(connection, result_id)
        if record.artifact.fund_code != fund_code or record.artifact.id not in document_id_set:
            raise RiskStoreError("classification parse result document binding changed")
        result_document_ids.append(record.artifact.id)
        provenances.add(record.provenance.provenance_checksum)
    if explicit_parse_result_ids is not None:
        if len(result_document_ids) != len(set(result_document_ids)):
            raise RiskStoreError("classification parse result binding changed")
        if set(result_document_ids) != document_id_set:
            raise RiskStoreError("classification parse result binding changed")
        if not set(fact_result_ids).issubset(result_ids):
            raise RiskStoreError("classification fact parse result binding changed")
    return (
        tuple(documents),
        tuple(facts),
        tuple(sorted(result_ids)),
        tuple(sorted(provenances)),
    )


def _authenticate_v3_evidence_snapshot(
    evidence: ClassificationEvidence,
    snapshot: CurrentDocumentSelectionSnapshot,
) -> None:
    refresh_id = _positive_integer(
        evidence.document_refresh_run_id,
        "classification document refresh run id",
    )
    if (
        evidence.fund_code != snapshot.selection.fund_code
        or refresh_id != snapshot.selection.refresh_run_id
        or evidence.selection_policy_checksum
        != snapshot.selection.selection_policy_checksum
        or evidence.selection_manifest_checksum != snapshot.selection.selection_checksum
        or evidence.candidate_run_snapshot_checksum
        != snapshot.candidate_run_snapshot_checksum
        or evidence.selection_reason_codes != snapshot.selection_reason_codes
    ):
        raise RiskStoreError("classification selection snapshot binding changed")
    successful = tuple(
        record
        for record in (
            *snapshot.selected_periodic_records,
            *snapshot.nonperiodic_successful_records,
        )
    )
    records, report_facts = current_evidence_projection(successful)
    if not records:
        raise RiskStoreError("classification evidence projection is unavailable")
    expected_document_ids = tuple(sorted(record.artifact.id for record in records))
    expected_parse_result_ids = tuple(sorted(record.parse_result.id for record in records))
    expected_provenance_checksums = tuple(
        sorted({record.provenance.provenance_checksum for record in records})
    )
    expected_fact_ids = tuple(
        sorted(
            {
                fact.id
                for record in records
                if record.artifact.document_kind not in PERIODIC_DOCUMENT_KINDS
                for fact in record.facts
            }
            | {fact.id for _, fact in report_facts}
        )
    )
    expected_legal_facts = tuple(
        sorted(
            (
                _stored_fact_as_mandate(fact)
                for record in records
                if record.artifact.document_kind in _LEGAL_EVIDENCE_DOCUMENT_KINDS
                for fact in record.facts
            ),
            key=_mandate_fact_order,
        )
    )
    expected_benchmark_facts = tuple(
        sorted(
            (
                _stored_fact_as_mandate(fact)
                for record in records
                if record.artifact.document_kind is DocumentKind.INDEX_METHODOLOGY
                for fact in record.facts
            ),
            key=_mandate_fact_order,
        )
    )
    expected_report_facts = tuple(
        sorted(
            (_stored_fact_as_mandate(fact) for _, fact in report_facts),
            key=_mandate_fact_order,
        )
    )
    if (
        evidence.document_ids != expected_document_ids
        or evidence.fact_ids != expected_fact_ids
        or evidence.parse_result_ids != expected_parse_result_ids
        or evidence.parser_provenance_checksums != expected_provenance_checksums
        or evidence.legal_facts != expected_legal_facts
        or evidence.benchmark_facts != expected_benchmark_facts
        or evidence.report_facts != expected_report_facts
    ):
        raise RiskStoreError("classification evidence projection changed")


def _mandate_fact_order(fact: MandateFact) -> tuple:
    return (fact.fact_kind, fact.source_document_id, fact.fact_fingerprint)


def _evidence_from_manifest(
    manifest_json: str,
    stored_facts: Tuple[StoredFact, ...],
    derived_parse_result_ids: Tuple[int, ...],
    derived_provenance_checksums: Tuple[str, ...],
) -> ClassificationEvidence:
    manifest, version = _decode_manifest_envelope(manifest_json)
    db_facts = tuple(_stored_fact_as_mandate(item) for item in stored_facts)
    available = list(db_facts)

    def group(name: str, *, stored_binding_required: bool) -> Tuple[MandateFact, ...]:
        payloads = manifest[name]
        if type(payloads) is not list:
            raise ValueError(f"manifest {name} must be an array")
        result = []
        for payload in payloads:
            fact = _manifest_fact(payload)
            if not stored_binding_required:
                result.append(fact)
                continue
            try:
                index = available.index(fact)
            except ValueError:
                raise ValueError("manifest fact does not match bound stored fact") from None
            result.append(available.pop(index))
        return tuple(result)

    legal = group("legal_facts", stored_binding_required=True)
    benchmark = group("benchmark_facts", stored_binding_required=True)
    report = group("report_facts", stored_binding_required=True)
    disclosures = group("existing_disclosure_facts", stored_binding_required=False)
    if available:
        raise ValueError("bound stored facts are missing from classification manifest")
    external_raw = manifest["external_evidence_fingerprints"]
    if type(external_raw) is not list:
        raise ValueError("external evidence fingerprints must be an array")
    external = tuple(_manifest_binding(item) for item in external_raw)
    source_references = _manifest_external_sources(manifest["external_source_references"])
    nav_fingerprint = manifest["nav_evidence_fingerprint"]
    if nav_fingerprint is not None:
        nav_fingerprint = _digest(nav_fingerprint, "NAV evidence fingerprint")
    evidence = ClassificationEvidence(
        fund_code=_fund_code(manifest["fund_code"]),
        legal_facts=legal,
        benchmark_facts=benchmark,
        report_facts=report,
        existing_disclosure_facts=disclosures,
        nav_conflicts=_manifest_code_tuple(manifest["nav_conflicts"], "NAV conflicts"),
        external_evidence_fingerprints=external,
        external_source_references=source_references,
        nav_evidence_fingerprint=nav_fingerprint,
        nav_observation_start=_manifest_optional_date(
            manifest["nav_observation_start"], "NAV observation start"
        ),
        nav_observation_end=_manifest_optional_date(
            manifest["nav_observation_end"], "NAV observation end"
        ),
        freshness=_manifest_freshness(manifest["freshness"]),
        document_ids=_manifest_id_tuple(manifest["document_ids"], "document IDs"),
        fact_ids=_manifest_id_tuple(manifest["fact_ids"], "fact IDs"),
        parse_result_ids=derived_parse_result_ids,
        parser_provenance_checksums=derived_provenance_checksums,
        document_refresh_run_id=(
            None
            if version < 3
            else _positive_integer(
                manifest["document_refresh_run_id"],
                "manifest document refresh run id",
            )
        ),
        selection_policy_checksum=(
            None
            if version < 3
            else _digest(
                manifest["selection_policy_checksum"],
                "manifest selection policy checksum",
            )
        ),
        selection_manifest_checksum=(
            None
            if version < 3
            else _digest(
                manifest["selection_manifest_checksum"],
                "manifest selection checksum",
            )
        ),
        candidate_run_snapshot_checksum=(
            None
            if version < 3
            else _digest(
                manifest["candidate_run_snapshot_checksum"],
                "manifest candidate run snapshot checksum",
            )
        ),
        selection_reason_codes=(
            ()
            if version < 3
            else _manifest_code_tuple(
                manifest["selection_reason_codes"],
                "selection reason codes",
            )
        ),
    )
    if version in {2, 3}:
        manifest_result_ids = _manifest_id_tuple(
            manifest["parse_result_ids"],
            "parse result IDs",
        )
        manifest_checksums = _manifest_digest_tuple(
            manifest["parser_provenance_checksums"],
            "parser provenance checksums",
        )
        if manifest_result_ids != derived_parse_result_ids:
            raise ValueError("manifest parse result binding changed")
        if manifest_checksums != derived_provenance_checksums:
            raise ValueError("manifest parser provenance binding changed")
    evidence.validate()
    _digest(manifest["policy_checksum"], "manifest policy checksum")
    _stable_code(manifest["policy_version"], "manifest policy version", version=True)
    _stored_datetime(manifest["classified_at"], "manifest classified_at")
    return evidence


def _stored_fact_as_mandate(value: StoredFact) -> MandateFact:
    fact = MandateFact(
        fund_code=value.fund_code,
        fact_kind=value.fact_kind,
        normalized_value=decode_fact_value_json(value.normalized_value_json),
        unit=value.unit,
        source_document_id=value.source_document_id,
        page_number=value.page_number,
        section_name=value.section_name,
        source_excerpt=value.source_excerpt,
        effective_from=value.effective_from,
        effective_to=value.effective_to,
        confidence_state=value.confidence_state,
        parser_version=value.parser_version,
        fact_fingerprint=value.fact_fingerprint,
    )
    fact.validate()
    return fact


def _manifest_fact(value: object) -> MandateFact:
    if type(value) is not dict or set(value) != {field.name for field in fields(MandateFact)}:
        raise ValueError("manifest fact has unexpected fields")
    fact = MandateFact(
        fund_code=_fund_code(value["fund_code"]),
        fact_kind=_stable_code(value["fact_kind"], "manifest fact kind"),
        normalized_value=fact_value_from_canonical(value["normalized_value"]),
        unit=_optional_text(value["unit"], "manifest fact unit"),
        source_document_id=_positive_integer(
            value["source_document_id"], "manifest source document id"
        ),
        page_number=_optional_positive_integer(value["page_number"], "manifest page number"),
        section_name=_optional_text(value["section_name"], "manifest section name"),
        source_excerpt=_required_text(value["source_excerpt"], "manifest source excerpt"),
        effective_from=_manifest_optional_date(value["effective_from"], "effective_from"),
        effective_to=_manifest_optional_date(value["effective_to"], "effective_to"),
        confidence_state=FactConfidence(value["confidence_state"]),
        parser_version=_stable_code(
            value["parser_version"], "manifest parser version", version=True
        ),
        fact_fingerprint=_digest(value["fact_fingerprint"], "manifest fact fingerprint"),
    )
    fact.validate()
    return fact


def _manifest_freshness(value: object) -> Tuple[EvidenceFreshness, ...]:
    if type(value) is not list:
        raise ValueError("manifest freshness must be an array")
    items = []
    expected = {
        "critical",
        "observed_at",
        "section",
        "source_document_id",
        "state",
        "valid_until",
    }
    for raw in value:
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("manifest freshness entry has unexpected fields")
        item = EvidenceFreshness(
            section=_stable_code(raw["section"], "freshness section"),
            source_document_id=_positive_integer(
                raw["source_document_id"], "freshness source document id"
            ),
            state=FreshnessState(raw["state"]),
            observed_at=_stored_datetime(raw["observed_at"], "freshness observed_at"),
            valid_until=_stored_datetime(raw["valid_until"], "freshness valid_until"),
            critical=raw["critical"],
        )
        item.validate()
        items.append(item)
    return tuple(items)


def _decode_freshness(value: str) -> Tuple[EvidenceFreshness, ...]:
    parsed = _decode_canonical_json(value, "freshness JSON")
    return _manifest_freshness(parsed)


def _manifest_binding(value: object) -> Tuple[str, str]:
    if type(value) is not list or len(value) != 2:
        raise ValueError("external evidence binding must be a pair")
    return (
        _stable_code(value[0], "external evidence section"),
        _digest(value[1], "external evidence fingerprint"),
    )


def _manifest_external_sources(value: object) -> Tuple[ExternalSourceReference, ...]:
    if type(value) is not list:
        raise ValueError("external source references must be an array")
    expected = {
        "checksum",
        "document_kind",
        "fund_code",
        "published_at",
        "publisher",
        "retrieved_at",
        "section",
        "source_document_id",
        "source_name",
        "source_namespace",
        "source_tier",
        "title",
        "url",
    }
    references = []
    for raw in value:
        if type(raw) is not dict or set(raw) != expected:
            raise ValueError("external source reference has unexpected fields")
        published_at = raw["published_at"]
        if published_at is not None:
            published_at = _stored_datetime(
                published_at,
                "external source published_at",
            )
        reference = ExternalSourceReference(
            source_namespace=_required_text(
                raw["source_namespace"],
                "external source namespace",
            ),
            source_document_id=_positive_integer(
                raw["source_document_id"],
                "external source document id",
            ),
            fund_code=_fund_code(raw["fund_code"]),
            document_kind=_stable_code(
                raw["document_kind"],
                "external document kind",
            ),
            section=_stable_code(raw["section"], "external source section"),
            title=_required_text(raw["title"], "external source title"),
            url=_required_text(raw["url"], "external source URL"),
            source_name=_required_text(raw["source_name"], "external source name"),
            source_tier=_positive_integer(raw["source_tier"], "external source tier"),
            publisher=_required_text(raw["publisher"], "external source publisher"),
            published_at=published_at,
            retrieved_at=_stored_datetime(
                raw["retrieved_at"],
                "external source retrieved_at",
            ),
            checksum=_digest(raw["checksum"], "external source checksum"),
        )
        reference.validate()
        references.append(reference)
    return tuple(references)


def _manifest_optional_date(value: object, label: str) -> Optional[date]:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{label} must be date text")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be canonical")
    return parsed


def _manifest_id_tuple(value: object, label: str) -> Tuple[int, ...]:
    if type(value) is not list:
        raise ValueError(f"manifest {label} must be an array")
    result = tuple(value)
    if result != tuple(sorted(set(result))) or any(
        type(item) is not int or item <= 0 for item in result
    ):
        raise ValueError(f"manifest {label} must be positive, sorted, and unique")
    return result


def _manifest_code_tuple(value: object, label: str) -> Tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"manifest {label} must be an array")
    result = tuple(_stable_code(item, label) for item in value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"manifest {label} must be sorted and unique")
    return result


def _manifest_digest_tuple(value: object, label: str) -> Tuple[str, ...]:
    if type(value) is not list:
        raise ValueError(f"manifest {label} must be an array")
    result = tuple(_digest(item, label) for item in value)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"manifest {label} must be sorted and unique")
    return result


def _decode_manifest_envelope(value: str) -> Tuple[Dict[str, object], int]:
    manifest = _decode_canonical_object(value, "classification input manifest")
    keys = frozenset(manifest)
    if keys == _MANIFEST_V1_KEYS:
        return manifest, 1
    if keys == _MANIFEST_V2_KEYS and manifest["manifest_version"] == 2:
        return manifest, 2
    if keys == _MANIFEST_V3_KEYS and manifest["manifest_version"] == 3:
        return manifest, 3
    raise ValueError("classification input manifest has unexpected fields")


def _decode_canonical_json(value: str, label: str) -> object:
    if type(value) is not str:
        raise ValueError(f"{label} must be text")
    parsed = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if _canonical_json(parsed) != value:
        raise ValueError(f"{label} is not canonical")
    return parsed


def _decode_canonical_object(value: str, label: str) -> Dict[str, object]:
    parsed = _decode_canonical_json(value, label)
    if type(parsed) is not dict:
        raise ValueError(f"{label} must be an object")
    return parsed


def _decode_document_selection_payload(
    payload: Dict[str, object],
    *,
    refresh_run_id: int,
    fund_code: str,
    selection_policy_checksum: str,
) -> Tuple[Tuple[SelectionCandidate, ...], Tuple[PeriodicSelectionState, ...]]:
    if frozenset(payload) != _SELECTION_MANIFEST_KEYS:
        raise ValueError("document selection manifest has unexpected fields")
    if (
        payload["manifest_version"] != 1
        or type(payload["manifest_version"]) is not int
        or payload["refresh_run_id"] != refresh_run_id
        or type(payload["refresh_run_id"]) is not int
        or payload["fund_code"] != fund_code
        or type(payload["fund_code"]) is not str
        or payload["selection_policy_checksum"] != selection_policy_checksum
        or type(payload["selection_policy_checksum"]) is not str
    ):
        raise ValueError("document selection manifest binding changed")

    raw_candidates = payload["periodic_candidates"]
    if type(raw_candidates) is not list:
        raise ValueError("document selection candidates must be an array")
    candidates = tuple(_decode_selection_candidate(item) for item in raw_candidates)
    periodic_order = {kind: index for index, kind in enumerate(PERIODIC_DOCUMENT_KINDS)}
    expected_candidates = tuple(
        sorted(
            candidates,
            key=lambda item: (
                periodic_order[item.document_kind],
                item.published_at,
                item.url,
                item.candidate_fingerprint,
            ),
        )
    )
    if candidates != expected_candidates:
        raise ValueError("document selection candidates are not canonically ordered")
    fingerprints = tuple(item.candidate_fingerprint for item in candidates)
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("document selection candidates contain duplicate fingerprints")

    raw_states = payload["periodic_states"]
    if type(raw_states) is not list:
        raise ValueError("document selection states must be an array")
    states = tuple(_decode_selection_state(item) for item in raw_states)
    if tuple(item.document_kind for item in states) != PERIODIC_DOCUMENT_KINDS:
        raise ValueError("document selection states do not cover every periodic kind")
    for state in states:
        _authenticate_selection_state(state, candidates)
    return candidates, states


def _decode_selection_candidate(value: object) -> SelectionCandidate:
    if type(value) is not dict or frozenset(value) != _SELECTION_CANDIDATE_KEYS:
        raise ValueError("document selection candidate has unexpected fields")
    record = SelectionCandidate(
        candidate_fingerprint=_digest(
            value["candidate_fingerprint"],
            "selection candidate fingerprint",
        ),
        document_kind=DocumentKind(value["document_kind"]),
        url=_required_text(value["url"], "selection candidate URL"),
        published_at=_stored_datetime(
            value["published_at"],
            "selection candidate published_at",
        ),
    )
    record.validate()
    return record


def _decode_selection_state(value: object) -> PeriodicSelectionState:
    if type(value) is not dict or frozenset(value) != _SELECTION_STATE_KEYS:
        raise ValueError("document selection state has unexpected fields")
    raw_fingerprints = value["candidate_fingerprints"]
    if type(raw_fingerprints) is not list:
        raise ValueError("document selection state fingerprints must be an array")
    selected = value["selected_fingerprint"]
    reason = value["reason_code"]
    record = PeriodicSelectionState(
        document_kind=DocumentKind(value["document_kind"]),
        state=_required_text(value["state"], "selection state"),
        candidate_fingerprints=tuple(
            _digest(item, "selection state candidate fingerprint")
            for item in raw_fingerprints
        ),
        selected_fingerprint=(
            None if selected is None else _digest(selected, "selected candidate fingerprint")
        ),
        reason_code=None if reason is None else _required_text(reason, "selection reason code"),
    )
    record.validate()
    return record


def _authenticate_selection_state(
    state: PeriodicSelectionState,
    candidates: Tuple[SelectionCandidate, ...],
) -> None:
    matching = tuple(item for item in candidates if item.document_kind is state.document_kind)
    fingerprints = tuple(sorted(item.candidate_fingerprint for item in matching))
    if state.candidate_fingerprints != fingerprints:
        raise ValueError("document selection state candidate binding changed")
    if not matching:
        if state.state != "missing":
            raise ValueError("document selection state must be missing without candidates")
        return
    newest_time = max(item.published_at for item in matching)
    newest = tuple(item for item in matching if item.published_at == newest_time)
    if len(newest) == 1:
        if (
            state.state != "selected"
            or state.selected_fingerprint != newest[0].candidate_fingerprint
        ):
            raise ValueError("document selection state does not bind the newest candidate")
        return
    if state.state != "conflicted":
        raise ValueError("document selection state must fail closed on a newest-time tie")


def _decode_code_array(value: str, label: str) -> Tuple[str, ...]:
    parsed = _decode_canonical_json(value, f"{label} JSON")
    return _manifest_code_tuple(parsed, label)


def _decode_id_array(value: str, label: str) -> Tuple[int, ...]:
    parsed = _decode_canonical_json(value, f"{label} JSON")
    return _manifest_id_tuple(parsed, label)


def _validated_parsed_document(
    parsed: ParsedRiskDocument,
    *,
    parser_version: str = PARSER_VERSION,
) -> Tuple[Tuple[object, ...], Tuple[Tuple[object, ...], ...]]:
    if type(parsed) is not ParsedRiskDocument:
        raise ValueError("parsed document must be the exact ParsedRiskDocument type")
    parsed.validate()
    artifact_values = _validated_artifact_values(
        parsed.artifact,
        parse_status="parsed",
        parser_version=parser_version,
        parse_error_code=None,
    )
    fact_values = tuple(
        _parsed_fact_values(
            fact,
            parsed.artifact.candidate.fund_code,
            parser_version=parser_version,
        )
        for fact in parsed.facts
    )
    return artifact_values, fact_values


def _validated_artifact_values(
    artifact: RetrievedArtifact,
    *,
    parse_status: str,
    parser_version: str,
    parse_error_code: Optional[str],
) -> Tuple[object, ...]:
    if type(artifact) is not RetrievedArtifact or set(vars(artifact)) != {
        field.name for field in fields(RetrievedArtifact)
    }:
        raise ValueError("artifact must be the exact RetrievedArtifact type")
    if type(artifact.candidate) is not OfficialDocumentCandidate:
        raise ValueError("artifact candidate must be exact")
    artifact.candidate.validate()
    allowed_hosts = validate_official_source(
        artifact.candidate.publisher,
        artifact.candidate.url,
    )
    landing_url = validate_safe_https_url(artifact.candidate.url)
    landing_host = (landing_url.hostname or "").lower().rstrip(".")
    if landing_host not in allowed_hosts:
        raise ValueError("artifact landing URL host is not registered for its publisher")
    final_url = validate_safe_https_url(artifact.final_url)
    final_host = (final_url.hostname or "").lower().rstrip(".")
    if final_host not in allowed_hosts:
        raise ValueError("artifact final URL host is not registered for its publisher")
    retrieved_at = _canonical_utc_text(artifact.retrieved_at, "retrieved_at")
    published_at = (
        None
        if artifact.candidate.published_at is None
        else _canonical_utc_text(artifact.candidate.published_at, "published_at")
    )
    if type(artifact.content_type) is not str or not artifact.content_type.strip():
        raise ValueError("artifact content type is required")
    if type(artifact.byte_size) is not int or not 0 < artifact.byte_size <= MAX_DOCUMENT_BYTES:
        raise ValueError("artifact byte size is invalid")
    sha256 = _digest(artifact.sha256, "artifact checksum")
    if not isinstance(artifact.managed_path, Path) or "\x00" in str(artifact.managed_path):
        raise ValueError("artifact managed path is invalid")
    _stable_code(parser_version, "parser version", version=True)
    if parse_status not in {"parsed", "failed"}:
        raise ValueError("artifact parse status is invalid")
    if parse_status == "parsed" and parse_error_code is not None:
        raise ValueError("parsed artifact cannot have a parse error")
    if parse_status == "failed":
        _stable_code(parse_error_code, "parse error code")
    _authenticate_file(artifact.managed_path, artifact.byte_size, sha256)
    return (
        artifact.candidate.fund_code,
        artifact.candidate.document_kind.value,
        artifact.final_url,
        artifact.candidate.url,
        artifact.candidate.publisher,
        artifact.candidate.title,
        published_at,
        retrieved_at,
        artifact.content_type,
        artifact.byte_size,
        sha256,
        str(artifact.managed_path),
        parse_status,
        parser_version,
        parse_error_code,
    )


def _parsed_fact_values(
    fact: ParsedMandateFact,
    fund_code: str,
    *,
    parser_version: str = PARSER_VERSION,
) -> Tuple[object, ...]:
    if type(fact) is not ParsedMandateFact:
        raise ValueError("parsed fact must be exact")
    fact.validate()
    mandate = MandateFact(
        fund_code=fund_code,
        fact_kind=fact.fact_kind,
        normalized_value=fact.normalized_value,
        unit=fact.unit,
        source_document_id=1,
        page_number=fact.page_number,
        section_name=fact.section_name,
        source_excerpt=fact.source_excerpt,
        effective_from=fact.effective_from,
        effective_to=fact.effective_to,
        confidence_state=fact.confidence_state,
        parser_version=parser_version,
        fact_fingerprint=fact.fact_fingerprint,
    )
    mandate.validate()
    return (
        fact.fact_kind,
        encode_fact_value_json(fact.normalized_value),
        fact.unit,
        fact.page_number,
        fact.section_name,
        fact.source_excerpt,
        None if fact.effective_from is None else fact.effective_from.isoformat(),
        None if fact.effective_to is None else fact.effective_to.isoformat(),
        fact.confidence_state.value,
        parser_version,
        fact.fact_fingerprint,
    )


def _artifact_record_values(record: StoredDocumentArtifact) -> Tuple[object, ...]:
    return (
        record.fund_code,
        record.document_kind.value,
        record.url,
        record.landing_url,
        record.publisher,
        record.title,
        None if record.published_at is None else record.published_at.isoformat(),
        record.retrieved_at.isoformat(),
        record.content_type,
        record.byte_size,
        record.sha256,
        str(record.managed_path),
        record.parse_status,
        record.parser_version,
        record.parse_error_code,
    )


def _fact_record_values(record: StoredFact) -> Tuple[object, ...]:
    return (
        record.fund_code,
        record.source_document_id,
        record.parse_result_id,
        record.fact_kind,
        record.normalized_value_json,
        record.unit,
        record.page_number,
        record.section_name,
        record.source_excerpt,
        None if record.effective_from is None else record.effective_from.isoformat(),
        None if record.effective_to is None else record.effective_to.isoformat(),
        record.confidence_state.value,
        record.parser_version,
        record.fact_fingerprint,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _validate_candidate(candidate: object) -> OfficialDocumentCandidate:
    if type(candidate) is not OfficialDocumentCandidate:
        raise ValueError("candidate must be exact")
    candidate.validate()
    if candidate.published_at is not None and candidate.published_at.tzinfo is not timezone.utc:
        raise ValueError("candidate published_at must use canonical UTC")
    return candidate


def _validate_provenance(provenance: object) -> ParserProvenance:
    if type(provenance) is not ParserProvenance:
        raise ValueError("parser provenance must be exact")
    provenance.validate()
    return provenance


def _validate_failure(failure: object) -> SafeDocumentFailure:
    if type(failure) is not SafeDocumentFailure:
        raise ValueError("safe document failure must be exact")
    failure.validate()
    return failure


def _authenticate_refresh(connection: Any, refresh_id: int, fund_code: str) -> None:
    row = connection.execute(
        "SELECT refresh.fund_code, completion.refresh_run_id AS completed "
        "FROM fund_document_refresh_runs AS refresh "
        "LEFT JOIN fund_document_refresh_completions AS completion "
        "ON completion.refresh_run_id = refresh.id WHERE refresh.id = ?",
        (refresh_id,),
    ).fetchone()
    if row is None or row["fund_code"] != fund_code:
        raise RiskStoreError("document refresh fund binding changed")
    if row["completed"] is not None:
        raise RiskStoreError("document refresh is already complete")


def _digest_tuple(values: object, label: str) -> Tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{label} must be an immutable tuple")
    result = tuple(_digest(value, label) for value in values)
    if result != tuple(sorted(set(result))):
        raise ValueError(f"{label} must be unique and sorted")
    return result


def _sort_parsed_records(
    records: Tuple[ParsedDocumentRecord, ...],
) -> Tuple[ParsedDocumentRecord, ...]:
    ordered = list(records)
    ordered.sort(key=lambda item: item.parse_run.id, reverse=True)
    ordered.sort(key=lambda item: item.artifact.retrieved_at, reverse=True)
    ordered.sort(
        key=lambda item: (
            item.artifact.published_at is not None,
            item.artifact.published_at
            if item.artifact.published_at is not None
            else datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    ordered.sort(key=lambda item: item.artifact.document_kind.value)
    return tuple(ordered)


def _authenticate_stored_artifact(record: StoredDocumentArtifact) -> None:
    if record.parse_status == "parsed" and record.parse_error_code is not None:
        raise RiskStoreError("stored parsed artifact has a parse error")
    if record.parse_status == "failed" and record.parse_error_code is None:
        raise RiskStoreError("stored failed artifact lacks a parse error")
    allowed_hosts = validate_official_source(record.publisher, record.landing_url)
    final_url = validate_safe_https_url(record.url)
    if (final_url.hostname or "").lower().rstrip(".") not in allowed_hosts:
        raise RiskStoreError("stored artifact final URL host is not registered")
    _authenticate_file(record.managed_path, record.byte_size, record.sha256)


def _authenticate_file(path: Path, byte_size: int, expected_sha256: str) -> None:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != byte_size:
            raise ValueError("artifact file metadata does not match")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(byte_size + 1)
    except OSError as exc:
        raise ValueError("artifact file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) != byte_size or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("artifact file checksum does not match")


def _owned_repository(repository: object) -> Repository:
    if (
        type(repository) is not Repository
        or type(vars(repository)) is not dict
        or set(vars(repository)) != {"database"}
    ):
        raise ValueError("repository must be an exact declared Repository")
    database = repository.database
    if type(database) is not type(Path()) or "\x00" in str(database):
        raise ValueError("repository database path is unsupported")
    try:
        resolved = database.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("repository database path is unsupported") from None
    return Repository(resolved)


def _canonical_utc_text(value: object, label: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _stored_datetime(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"stored {label} must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not timezone.utc or parsed.isoformat() != value:
        raise ValueError(f"stored {label} must use canonical UTC")
    return parsed


def _stored_optional_datetime(value: object, label: str) -> Optional[datetime]:
    return None if value is None else _stored_datetime(value, label)


def _stored_optional_date(value: object, label: str) -> Optional[date]:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"stored {label} must be date text")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"stored {label} is not canonical")
    return parsed


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be positive")
    return value


def _optional_positive_integer(value: object, label: str) -> Optional[int]:
    return None if value is None else _positive_integer(value, label)


def _fund_code(value: object) -> str:
    if type(value) is not str or len(value) != 6 or not value.isascii() or not value.isdigit():
        raise ValueError("stored fund code is invalid")
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> Optional[str]:
    return None if value is None else _required_text(value, label)


def _digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _stable_code(value: object, label: str, *, version: bool = False) -> str:
    text = _required_text(value, label)
    allowed = (
        "abcdefghijklmnopqrstuvwxyz0123456789._-"
        if version
        else "abcdefghijklmnopqrstuvwxyz0123456789_"
    )
    if (
        text[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
        or (not version and text[0] not in "abcdefghijklmnopqrstuvwxyz")
        or any(character not in allowed for character in text)
    ):
        raise ValueError(f"{label} must be a stable code")
    return text


def _optional_stable_code(value: object) -> Optional[str]:
    return None if value is None else _stable_code(value, "parse error code")


def _parse_status(value: object) -> str:
    if value not in {"parsed", "failed"} or type(value) is not str:
        raise ValueError("stored parse status is invalid")
    return value


def _stored_path(value: object) -> Path:
    text = _required_text(value, "managed path")
    path = Path(text)
    if "\x00" in text:
        raise ValueError("stored managed path is invalid")
    return path
