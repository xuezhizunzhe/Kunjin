from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional
from urllib.parse import urlsplit

from kunjin.brief.models import OfficialEventCode, thesis_record_fingerprint
from kunjin.brief.store import BriefStore, BriefStoreError, StoredBriefSnapshot
from kunjin.decision.models import (
    ActionKind,
    RequestMode,
    SourceTier,
    canonical_json_bytes,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.funds.official_domains import (
    OFFICIAL_SOURCE_REGISTRATIONS,
    OFFICIAL_SOURCE_REGISTRY_VERSION,
    official_source_registry_checksum,
)
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    AdjudicationDecision,
    BindingState,
    EvidenceDelta,
    EvidenceReadiness,
    ExitReason,
    FlowStatus,
    HeldReviewOfficialEventProjection,
    HistoryComparability,
    HoldingReviewResult,
    HoldingReviewSnapshot,
    OfficialAnnouncementContent,
    OfficialCheckClosure,
    OfficialEventEvidenceReference,
    OfficialListingPageEvidence,
    OfficialListingTerminalState,
    OfficialManagerIdentityState,
    RedemptionComponentState,
    RedemptionEvidence,
    RedemptionFeasibility,
    RemainderIntent,
    ReviewBoundary,
    ReviewDisposition,
    ReviewEvidenceItem,
    ThesisEvidenceAdjudication,
    ThesisMatchProjection,
    ThesisMatchProjectionState,
    ThesisMatchState,
    TriggeredReviewCode,
    UseOfProceeds,
)
from kunjin.holding_review.official import (
    OfficialListingItem,
    classify_official_listing_title,
)
from kunjin.holding_review.policy import (
    OFFICIAL_MAXIMUM_CANDIDATES,
    HeldFundManualReviewPolicyV1,
    OfficialCheckPolicyV1,
)
from kunjin.intelligence.models import (
    DimensionState,
    EventConfidenceState,
    EventEntityRelationship,
    IntegrityState,
    IntelligenceWorkflow,
    LineageEdge,
    LineageKind,
    NewsItem,
)
from kunjin.intelligence.store import (
    IntelligenceStore,
    IntelligenceStoreError,
    StoredIntelligenceSnapshot,
)
from kunjin.models import InvestmentThesis
from kunjin.storage.repository import Repository

MAX_REVIEW_JSON_BYTES = 4 * 1024 * 1024
_ORIGIN_PROVING_LINEAGE_KINDS = frozenset(
    (LineageKind.DIRECT_QUOTE, LineageKind.REPRINT)
)
_ADJUDICATION_REVIEW_STATES = {
    AdjudicationDecision.PRESENTED_MATCH_CONFIRMED: (
        ThesisMatchState.PRESENTED_MATCH_CONFIRMED
    ),
    AdjudicationDecision.PRESENTED_MATCH_REJECTED: (
        ThesisMatchState.PRESENTED_MATCH_REJECTED
    ),
    AdjudicationDecision.UNCERTAIN: ThesisMatchState.MANUAL_REVIEW_UNCERTAIN,
}
_PROJECTION_REVIEW_STATES = {
    ThesisMatchProjectionState.THESIS_MISSING: ThesisMatchState.THESIS_MISSING,
    ThesisMatchProjectionState.NO_MATCHING_EVIDENCE: (
        ThesisMatchState.NO_MATCHING_EVIDENCE
    ),
    ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH: (
        ThesisMatchState.MANUAL_REVIEW_PENDING
    ),
}


class HoldingReviewStoreError(RuntimeError):
    """A sanitized authenticated held-review persistence failure."""


@dataclass(frozen=True)
class StoredThesisMatchProjection:
    id: int
    value: ThesisMatchProjection


@dataclass(frozen=True)
class StoredThesisEvidenceAdjudication:
    id: int
    value: ThesisEvidenceAdjudication


@dataclass(frozen=True)
class AuthenticatedThesisProjectionInputs:
    intelligence: StoredIntelligenceSnapshot
    items: tuple[NewsItem, ...]
    evidence_descriptors: tuple[ReviewEvidenceItem, ...]


@dataclass(frozen=True)
class StoredHoldingReviewSnapshot:
    id: int
    value: HoldingReviewSnapshot


@dataclass(frozen=True)
class StoredOfficialCheckClosure:
    id: int
    value: OfficialCheckClosure


@dataclass(frozen=True)
class AuthenticatedOfficialManagerIdentity:
    fund_code: str
    state: OfficialManagerIdentityState
    row_id: Optional[int]
    source_document_id: Optional[int]
    source_document_checksum: Optional[str]
    normalized_name: Optional[str]
    fingerprint: Optional[str]
    fund_name: Optional[str]

    def validate(self) -> None:
        _fund_code(self.fund_code)
        if type(self.state) is not OfficialManagerIdentityState:
            raise ValueError("official manager identity state must be exact")
        values = (
            self.row_id,
            self.source_document_id,
            self.source_document_checksum,
            self.normalized_name,
            self.fingerprint,
            self.fund_name,
        )
        present = all(item is not None for item in values)
        if present != (self.state is OfficialManagerIdentityState.PRESENT):
            raise ValueError("official manager identity fields must be all-or-none")
        if present:
            _positive_id(self.row_id, "manager identity row id")
            _positive_id(self.source_document_id, "manager identity source document id")
            _checksum(
                self.source_document_checksum,
                "manager identity source document checksum",
            )
            _checksum(self.fingerprint, "manager identity fingerprint")
            if self.normalized_name != _normalized_identity(self.normalized_name):
                raise ValueError("official manager identity name is not normalized")
            _normalized_identity(self.fund_name)


class HoldingReviewStore:
    def __init__(self, repository: Repository) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        self.repository = repository
        self.brief_store = BriefStore(repository)
        self.intelligence_store = IntelligenceStore(repository)
        self.policy = HeldFundManualReviewPolicyV1()
        self.policy.validate()
        self.official_policy = OfficialCheckPolicyV1()
        self.official_policy.validate()
        self.source_registry = SourceRegistryV1()
        self.source_registry.validate()

    def publish_announcement_content(self, value: OfficialAnnouncementContent) -> int:
        if type(value) is not OfficialAnnouncementContent:
            raise ValueError("official announcement content must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_announcement_content_references(
                        connection, value, require_running=True
                    )
                    existing = connection.execute(
                        """
                        SELECT * FROM fund_official_announcement_contents
                        WHERE listing_source_document_id=?
                          AND canonical_announcement_url=?
                          AND normalized_content_sha256=?
                          AND integrity_checked_at=?
                        """,
                        (
                            value.listing_source_document_id,
                            value.canonical_announcement_url,
                            value.normalized_content_sha256,
                            _utc_text(value.integrity_checked_at),
                        ),
                    ).fetchone()
                    if existing is not None:
                        content_id, stored = self._stored_announcement_content(existing)
                        self._authenticate_announcement_content_references(
                            connection, stored, require_running=True
                        )
                        if stored != value:
                            raise HoldingReviewStoreError(
                                "official announcement content authentication failed"
                            )
                        connection.commit()
                        return content_id
                    cursor = connection.execute(
                        """
                        INSERT INTO fund_official_announcement_contents(
                            brief_request_run_id, source_attempt_id, fund_code,
                            listing_source_document_id, canonical_announcement_url,
                            announcement_title, announcement_published_at, publisher,
                            normalized_content, normalized_content_bytes,
                            normalized_content_sha256, original_source_id,
                            quoted_source_id, integrity_status, integrity_checked_at,
                            retrieved_at, record_checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            value.brief_request_run_id,
                            value.source_attempt_id,
                            value.fund_code,
                            value.listing_source_document_id,
                            value.canonical_announcement_url,
                            value.announcement_title,
                            _utc_text(value.announcement_published_at),
                            value.publisher,
                            value.normalized_content,
                            value.normalized_content_bytes,
                            value.normalized_content_sha256,
                            value.original_source_id,
                            value.quoted_source_id,
                            value.integrity_status,
                            _utc_text(value.integrity_checked_at),
                            _utc_text(value.retrieved_at),
                            value.record_checksum,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM fund_official_announcement_contents WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError(
                            "official announcement content reload failed"
                        )
                    content_id, stored = self._stored_announcement_content(row)
                    self._authenticate_announcement_content_references(
                        connection, stored, require_running=True
                    )
                    if stored != value:
                        raise HoldingReviewStoreError(
                            "official announcement content byte comparison failed"
                        )
                    connection.commit()
                    return content_id
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "official announcement content binding failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official announcement content authentication failed"
            ) from None

    def publish_official_event(self, value: HeldReviewOfficialEventProjection) -> int:
        if type(value) is not HeldReviewOfficialEventProjection:
            raise ValueError("official event projection must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_official_event_references(
                        connection, value, require_running=True
                    )
                    existing = connection.execute(
                        """
                        SELECT * FROM held_review_official_event_projections
                        WHERE brief_request_run_id=? AND announcement_content_id=?
                          AND event_code=?
                        """,
                        (
                            value.brief_request_run_id,
                            value.announcement_content_id,
                            value.event_code.value,
                        ),
                    ).fetchone()
                    if existing is not None:
                        projection_id, stored = self._stored_official_event(existing)
                        self._authenticate_official_event_references(
                            connection, stored, require_running=True
                        )
                        if stored != value:
                            raise HoldingReviewStoreError(
                                "official event projection authentication failed"
                            )
                        connection.commit()
                        return projection_id
                    cursor = connection.execute(
                        """
                        INSERT INTO held_review_official_event_projections(
                            brief_request_run_id, fund_code, announcement_row_id,
                            announcement_content_id, event_code, triggered_review_code,
                            policy_version, policy_checksum, record_checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            value.brief_request_run_id,
                            value.fund_code,
                            value.announcement_row_id,
                            value.announcement_content_id,
                            value.event_code.value,
                            value.triggered_review_code.value,
                            value.policy_version,
                            value.policy_checksum,
                            value.record_checksum,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM held_review_official_event_projections WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError(
                            "official event projection reload failed"
                        )
                    projection_id, stored = self._stored_official_event(row)
                    self._authenticate_official_event_references(
                        connection, stored, require_running=True
                    )
                    if stored != value:
                        raise HoldingReviewStoreError(
                            "official event projection byte comparison failed"
                        )
                    connection.commit()
                    return projection_id
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError("official event projection binding failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official event projection authentication failed"
            ) from None

    def authenticated_official_event_references(
        self,
        brief_request_run_id: int,
        fund_code: str,
    ) -> tuple[OfficialEventEvidenceReference, ...]:
        _positive_id(brief_request_run_id, "brief request run id")
        _fund_code(fund_code)
        try:
            with self.repository.connect() as connection:
                self._authenticate_deep_request(
                    connection, brief_request_run_id, require_running=False
                )
                attempt = connection.execute(
                    """
                    SELECT 1 FROM source_attempts
                    WHERE request_run_id=?
                      AND source_id='fund_manager_official_documents'
                      AND field_id='fund_manager_product_announcement'
                      AND subject_key=?
                    LIMIT 1
                    """,
                    (brief_request_run_id, f"fund:{fund_code}"),
                ).fetchone()
                if attempt is None:
                    raise HoldingReviewStoreError(
                        "official event reference fund binding failed"
                    )
                rows = connection.execute(
                    """
                    SELECT * FROM held_review_official_event_projections
                    WHERE brief_request_run_id=? AND fund_code=? ORDER BY id
                    """,
                    (brief_request_run_id, fund_code),
                ).fetchall()
                if len(rows) > self.policy.maximum_announcement_candidates:
                    raise HoldingReviewStoreError(
                        "official event reference candidate bound failed"
                    )
                references = []
                for row in rows:
                    projection_id, value = self._stored_official_event(row)
                    self._authenticate_official_event_references(
                        connection, value, require_running=False
                    )
                    references.append(
                        OfficialEventEvidenceReference(
                            projection_id=projection_id,
                            projection_checksum=value.record_checksum,
                            event_code=value.event_code,
                            triggered_review_code=value.triggered_review_code,
                        )
                    )
                result = tuple(references)
                for reference in result:
                    reference.validate()
                return result
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "official event reference binding failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official event reference authentication failed"
            ) from None

    def publish_official_check_closure(
        self,
        value: OfficialCheckClosure,
    ) -> StoredOfficialCheckClosure:
        if type(value) is not OfficialCheckClosure:
            raise ValueError("official check closure must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    existing = connection.execute(
                        """
                        SELECT * FROM held_review_official_check_closures
                        WHERE brief_request_run_id=? AND fund_code=?
                        """,
                        (value.brief_request_run_id, value.fund_code),
                    ).fetchone()
                    if existing is not None:
                        stored = self._stored_official_check_closure(existing)
                        self._authenticate_official_check_closure(
                            connection,
                            stored,
                            require_running=True,
                        )
                        if stored.value != value:
                            raise HoldingReviewStoreError(
                                "official check closure authentication failed"
                            )
                        connection.commit()
                        return stored
                    cursor = connection.execute(
                        """
                        INSERT INTO held_review_official_check_closures(
                            brief_request_run_id, fund_code, listing_source_attempt_id,
                            official_registry_version, official_registry_checksum,
                            source_registration_ids_json, manager_identity_state,
                            manager_identity_row_id,
                            manager_identity_source_document_id,
                            manager_identity_source_document_checksum,
                            manager_identity_normalized_name,
                            manager_identity_fingerprint,
                            listing_page_evidence_json, window_start, window_end,
                            listing_count, candidate_count, authenticated_body_count,
                            projected_event_count, listing_truncated,
                            candidate_cap_reached, body_cap_reached, gap_codes_json,
                            official_negative_check_complete, policy_version,
                            policy_checksum, official_check_policy_version,
                            official_check_policy_checksum, created_at, record_checksum
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        _official_closure_sql_values(value),
                    )
                    row = connection.execute(
                        "SELECT * FROM held_review_official_check_closures WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError(
                            "official check closure reload failed"
                        )
                    stored = self._stored_official_check_closure(row)
                    if stored.value != value:
                        raise HoldingReviewStoreError(
                            "official check closure byte comparison failed"
                        )
                    self._authenticate_official_check_closure(
                        connection,
                        stored,
                        require_running=True,
                    )
                    connection.commit()
                    return stored
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "official check closure binding failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official check closure authentication failed"
            ) from None

    def authenticated_official_manager_identity(
        self,
        fund_code: str,
        as_of: datetime,
    ) -> AuthenticatedOfficialManagerIdentity:
        _fund_code(fund_code)
        _utc_text(as_of)
        try:
            with self.repository.connect() as connection:
                return self._resolve_official_manager_identity(
                    connection,
                    fund_code,
                    as_of,
                )
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "official manager identity binding failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official manager identity authentication failed"
            ) from None

    def authenticated_official_check_closure(
        self,
        brief_request_run_id: int,
        fund_code: str,
    ) -> StoredOfficialCheckClosure:
        _positive_id(brief_request_run_id, "brief request run id")
        _fund_code(fund_code)
        try:
            with self.repository.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM held_review_official_check_closures
                    WHERE brief_request_run_id=? AND fund_code=? ORDER BY id
                    """,
                    (brief_request_run_id, fund_code),
                ).fetchall()
                if len(rows) != 1:
                    raise HoldingReviewStoreError(
                        "official check closure is missing or ambiguous"
                    )
                stored = self._stored_official_check_closure(rows[0])
                self._authenticate_official_check_closure(
                    connection,
                    stored,
                    require_running=False,
                )
                return stored
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "official check closure binding failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "official check closure authentication failed"
            ) from None

    def publish_thesis_match(
        self,
        value: ThesisMatchProjection,
    ) -> StoredThesisMatchProjection:
        if type(value) is not ThesisMatchProjection:
            raise ValueError("thesis match projection must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_projection_references(connection, value)
                    existing = connection.execute(
                        """
                        SELECT * FROM thesis_match_projections
                        WHERE fund_code=? AND thesis_fingerprint IS ?
                          AND intelligence_request_run_id=?
                          AND matcher_policy_checksum=? AND evidence_set_checksum=?
                        """,
                        (
                            value.fund_code,
                            value.thesis_fingerprint,
                            value.intelligence_request_run_id,
                            value.matcher_policy_checksum,
                            value.evidence_set_checksum,
                        ),
                    ).fetchone()
                    if existing is not None:
                        stored = self._stored_projection(existing)
                        if _without_times_and_checksum(stored.value) != (
                            _without_times_and_checksum(value)
                        ):
                            raise HoldingReviewStoreError(
                                "thesis match projection authentication failed"
                            )
                        connection.commit()
                        return stored
                    cursor = connection.execute(
                        """
                        INSERT INTO thesis_match_projections(
                            fund_code, thesis_id, thesis_fingerprint,
                            intelligence_request_run_id, intelligence_snapshot_id,
                            intelligence_snapshot_checksum, matcher_policy_version,
                            matcher_policy_checksum, projection_state, evidence_ids_json,
                            evidence_descriptors_json, evidence_set_checksum, created_at,
                            record_checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            value.fund_code,
                            value.thesis_id,
                            value.thesis_fingerprint,
                            value.intelligence_request_run_id,
                            value.intelligence_snapshot_id,
                            value.intelligence_snapshot_checksum,
                            value.matcher_policy_version,
                            value.matcher_policy_checksum,
                            value.projection_state.value,
                            _json_text(value.evidence_ids),
                            _json_text(
                                tuple(
                                    item.to_canonical_dict()
                                    for item in value.evidence_descriptors
                                )
                            ),
                            value.evidence_set_checksum,
                            _utc_text(value.created_at),
                            value.record_checksum,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM thesis_match_projections WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError("thesis match projection reload failed")
                    stored = self._stored_projection(row)
                    if stored.value != value:
                        raise HoldingReviewStoreError(
                            "thesis match projection byte comparison failed"
                        )
                    connection.commit()
                    return stored
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError("thesis match projection binding failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "thesis match projection authentication failed"
            ) from None

    def latest_thesis_match(
        self,
        fund_code: str,
        request_run_id: int,
    ) -> Optional[StoredThesisMatchProjection]:
        _fund_code(fund_code)
        _positive_id(request_run_id, "request run id")
        try:
            with self.repository.connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM thesis_match_projections
                    WHERE fund_code=? AND intelligence_request_run_id=?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (fund_code, request_run_id),
                ).fetchone()
                if row is None:
                    return None
                stored = self._stored_projection(row)
                self._authenticate_projection_references(connection, stored.value)
                return stored
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError("thesis match projection authentication failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError("thesis match projection authentication failed") from None

    def authenticated_thesis_match(
        self,
        projection_id: int,
    ) -> StoredThesisMatchProjection:
        _positive_id(projection_id, "projection id")
        try:
            with self.repository.connect() as connection:
                stored = self._projection_by_id(connection, projection_id)
                self._authenticate_projection_references(connection, stored.value)
                return stored
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError(
                "thesis match projection authentication failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "thesis match projection authentication failed"
            ) from None

    def authenticated_thesis_projection_inputs(
        self,
        request_run_id: int,
    ) -> AuthenticatedThesisProjectionInputs:
        _positive_id(request_run_id, "request run id")
        try:
            with self.repository.connect() as connection:
                intelligence = self._intelligence_by_request(connection, request_run_id)
                descriptors = self._derived_evidence_descriptors(connection, intelligence)
                items = []
                for item_id in intelligence.snapshot.item_ids:
                    row = connection.execute(
                        "SELECT id FROM intelligence_news_items WHERE item_key=?",
                        (item_id,),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError(
                            "thesis projection input authentication failed"
                        )
                    items.append(
                        self.intelligence_store._authenticated_item(
                            connection, int(row["id"])
                        )
                    )
                ordered_descriptors = tuple(
                    descriptors[item_id] for item_id in intelligence.snapshot.item_ids
                )
                return AuthenticatedThesisProjectionInputs(
                    intelligence=intelligence,
                    items=tuple(items),
                    evidence_descriptors=ordered_descriptors,
                )
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError(
                "thesis projection input authentication failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "thesis projection input authentication failed"
            ) from None

    def publish_adjudication(
        self,
        value: ThesisEvidenceAdjudication,
    ) -> StoredThesisEvidenceAdjudication:
        if type(value) is not ThesisEvidenceAdjudication:
            raise ValueError("thesis evidence adjudication must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    projection = self._projection_by_id(
                        connection, value.thesis_match_projection_id
                    )
                    self._authenticate_projection_references(connection, projection.value)
                    self._authenticate_adjudication_projection(value, projection)
                    current = self._current_adjudication_row(
                        connection, value.thesis_match_projection_id
                    )
                    if current is not None:
                        current_stored = self._stored_adjudication(current)
                        if (
                            current_stored.value.decision is value.decision
                            and value.superseded_adjudication_id
                            in (None, current_stored.id)
                        ):
                            connection.commit()
                            return current_stored
                    existing = self._adjudication_identity_row(connection, value)
                    if existing is not None:
                        stored = self._stored_adjudication(existing)
                        if _without_times_and_checksum(stored.value) != (
                            _without_times_and_checksum(value)
                        ):
                            raise HoldingReviewStoreError(
                                "thesis evidence adjudication authentication failed"
                            )
                        current = self._current_adjudication_row(
                            connection, value.thesis_match_projection_id
                        )
                        if current is None or int(current["id"]) != stored.id:
                            raise HoldingReviewStoreError(
                                "thesis evidence adjudication supersession failed"
                            )
                        connection.commit()
                        return stored
                    if value.superseded_adjudication_id is None:
                        if current is not None:
                            raise HoldingReviewStoreError(
                                "thesis evidence adjudication supersession failed"
                            )
                    elif current is None or int(current["id"]) != value.superseded_adjudication_id:
                        raise HoldingReviewStoreError(
                            "thesis evidence adjudication supersession failed"
                        )
                    cursor = connection.execute(
                        """
                        INSERT INTO thesis_evidence_adjudications(
                            fund_code, thesis_id, thesis_fingerprint,
                            thesis_match_projection_id, thesis_match_projection_checksum,
                            intelligence_request_run_id, intelligence_snapshot_checksum,
                            evidence_ids_json, evidence_set_checksum, decision,
                            superseded_adjudication_id, created_at, record_checksum
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            value.fund_code,
                            value.thesis_id,
                            value.thesis_fingerprint,
                            value.thesis_match_projection_id,
                            value.thesis_match_projection_checksum,
                            value.intelligence_request_run_id,
                            value.intelligence_snapshot_checksum,
                            _json_text(value.evidence_ids),
                            value.evidence_set_checksum,
                            value.decision.value,
                            value.superseded_adjudication_id,
                            _utc_text(value.created_at),
                            value.record_checksum,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError("thesis evidence adjudication reload failed")
                    stored = self._stored_adjudication(row)
                    if stored.value != value:
                        raise HoldingReviewStoreError(
                            "thesis evidence adjudication byte comparison failed"
                        )
                    connection.commit()
                    return stored
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError("thesis evidence adjudication binding failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "thesis evidence adjudication authentication failed"
            ) from None

    def current_adjudication(
        self,
        projection_id: int,
    ) -> Optional[StoredThesisEvidenceAdjudication]:
        _positive_id(projection_id, "projection id")
        try:
            with self.repository.connect() as connection:
                row = self._current_adjudication_row(connection, projection_id)
                if row is None:
                    return None
                stored = self._stored_adjudication(row)
                projection = self._projection_by_id(connection, projection_id)
                self._authenticate_projection_references(connection, projection.value)
                self._authenticate_adjudication_projection(stored.value, projection)
                return stored
        except HoldingReviewStoreError:
            raise
        except sqlite3.DatabaseError:
            raise HoldingReviewStoreError(
                "thesis evidence adjudication authentication failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "thesis evidence adjudication authentication failed"
            ) from None

    def publish_review(self, value: HoldingReviewSnapshot) -> StoredHoldingReviewSnapshot:
        if type(value) is not HoldingReviewSnapshot:
            raise ValueError("holding review snapshot must be exact")
        value.validate()
        try:
            with self.repository.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._authenticate_review_references(connection, value)
                    existing = connection.execute(
                        "SELECT * FROM holding_review_snapshots WHERE semantic_identity_checksum=?",
                        (value.semantic_identity_checksum,),
                    ).fetchone()
                    if existing is not None:
                        stored = self._stored_review(existing)
                        if stored.value.previous_review_id != value.previous_review_id:
                            raise HoldingReviewStoreError(
                                "holding review semantic identity has a different previous pointer"
                            )
                        connection.commit()
                        return stored
                    self._authenticate_latest_comparable_previous(connection, value)
                    cursor = connection.execute(
                        """
                        INSERT INTO holding_review_snapshots(
                            fund_code, action, brief_request_run_id, brief_snapshot_id,
                            brief_snapshot_checksum, intelligence_request_run_id,
                            intelligence_snapshot_id, intelligence_snapshot_checksum,
                            thesis_match_projection_id, thesis_match_projection_checksum,
                            active_thesis_state, active_thesis_id, active_thesis_fingerprint,
                            adjudication_state, adjudication_id, adjudication_checksum,
                            previous_review_id, result_json, result_fingerprint,
                            policy_version, policy_checksum, created_at,
                            semantic_identity_checksum, record_checksum
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            value.fund_code,
                            value.action.value,
                            value.brief_request_run_id,
                            value.brief_snapshot_id,
                            value.brief_snapshot_checksum,
                            value.intelligence_request_run_id,
                            value.intelligence_snapshot_id,
                            value.intelligence_snapshot_checksum,
                            value.thesis_match_projection_id,
                            value.thesis_match_projection_checksum,
                            value.active_thesis_state.value,
                            value.active_thesis_id,
                            value.active_thesis_fingerprint,
                            value.adjudication_state.value,
                            value.adjudication_id,
                            value.adjudication_checksum,
                            value.previous_review_id,
                            value.result.canonical_json().decode("ascii"),
                            value.result_fingerprint,
                            value.policy_version,
                            value.policy_checksum,
                            _utc_text(value.created_at),
                            value.semantic_identity_checksum,
                            value.record_checksum,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM holding_review_snapshots WHERE id=?",
                        (int(cursor.lastrowid),),
                    ).fetchone()
                    if row is None:
                        raise HoldingReviewStoreError("holding review snapshot reload failed")
                    stored = self._stored_review(row)
                    if stored.value != value:
                        raise HoldingReviewStoreError(
                            "holding review snapshot byte comparison failed"
                        )
                    connection.commit()
                    return stored
                except BaseException:
                    connection.rollback()
                    raise
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError("holding review snapshot binding failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError("holding review snapshot authentication failed") from None

    def latest_comparable_review(
        self,
        fund_code: str,
        action: ActionKind,
        thesis_fingerprint: Optional[str],
        policy_checksum: str,
    ) -> Optional[StoredHoldingReviewSnapshot]:
        _fund_code(fund_code)
        if type(action) is not ActionKind:
            raise ValueError("action must be an exact ActionKind")
        if thesis_fingerprint is not None:
            _checksum(thesis_fingerprint, "thesis fingerprint")
        _checksum(policy_checksum, "policy checksum")
        try:
            with self.repository.connect() as connection:
                if thesis_fingerprint is None:
                    row = connection.execute(
                        """
                        SELECT * FROM holding_review_snapshots
                        WHERE fund_code=? AND action=? AND active_thesis_state='missing'
                          AND active_thesis_fingerprint IS NULL AND policy_checksum=?
                        ORDER BY created_at DESC, id DESC LIMIT 1
                        """,
                        (fund_code, action.value, policy_checksum),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM holding_review_snapshots
                        WHERE fund_code=? AND action=? AND active_thesis_state='present'
                          AND active_thesis_fingerprint=? AND policy_checksum=?
                        ORDER BY created_at DESC, id DESC LIMIT 1
                        """,
                        (fund_code, action.value, thesis_fingerprint, policy_checksum),
                    ).fetchone()
                if row is None:
                    return None
                stored = self._stored_review(row)
                self._authenticate_review_references(
                    connection,
                    stored.value,
                    require_current_adjudication=False,
                )
                return stored
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError("holding review snapshot authentication failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError("holding review snapshot authentication failed") from None

    def authenticated_review_evidence_descriptors(
        self,
        review_id: int,
    ) -> tuple[ReviewEvidenceItem, ...]:
        _positive_id(review_id, "review id")
        try:
            with self.repository.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM holding_review_snapshots WHERE id=?",
                    (review_id,),
                ).fetchone()
                if row is None:
                    raise HoldingReviewStoreError(
                        "holding review snapshot authentication failed"
                    )
                stored = self._stored_review(row)
                self._authenticate_review_references(
                    connection,
                    stored.value,
                    require_current_adjudication=False,
                )
                intelligence = self._intelligence_by_request(
                    connection, stored.value.intelligence_request_run_id
                )
                descriptors = self._derived_evidence_descriptors(
                    connection, intelligence
                )
                official_ids = {
                    reference.support_evidence_id
                    for reference in stored.value.result.official_event_evidence
                }
                evidence_ids = tuple(
                    evidence_id
                    for evidence_id in stored.value.result.evidence_ids
                    if evidence_id not in official_ids
                )
                if any(evidence_id not in descriptors for evidence_id in evidence_ids):
                    raise HoldingReviewStoreError(
                        "holding review evidence descriptor authentication failed"
                    )
                return tuple(descriptors[evidence_id] for evidence_id in evidence_ids)
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError(
                "holding review evidence descriptor authentication failed"
            ) from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError(
                "holding review evidence descriptor authentication failed"
            ) from None

    @staticmethod
    def _authenticate_deep_request(
        connection: sqlite3.Connection,
        request_run_id: int,
        *,
        require_running: bool,
    ) -> None:
        row = connection.execute(
            "SELECT mode, status FROM request_runs WHERE id=?", (request_run_id,)
        ).fetchone()
        if row is None or row["mode"] != "deep":
            raise HoldingReviewStoreError("Deep official request binding failed")
        allowed_statuses = {"running"} if require_running else {"running", "complete", "partial"}
        if row["status"] not in allowed_statuses:
            raise HoldingReviewStoreError("official request terminal binding failed")

    def _authenticate_official_check_closure(
        self,
        connection: sqlite3.Connection,
        stored: StoredOfficialCheckClosure,
        *,
        require_running: bool,
    ) -> None:
        _positive_id(stored.id, "official check closure id")
        value = stored.value
        value.validate()
        self._authenticate_deep_request(
            connection,
            value.brief_request_run_id,
            require_running=require_running,
        )
        if (
            value.policy_version != self.policy.version
            or value.policy_checksum != self.policy.checksum()
            or value.official_check_policy_version != self.official_policy.version
            or value.official_check_policy_checksum != self.official_policy.checksum()
            or value.official_registry_version != OFFICIAL_SOURCE_REGISTRY_VERSION
            or value.official_registry_checksum != official_source_registry_checksum()
        ):
            raise HoldingReviewStoreError(
                "official check closure policy authentication failed"
            )
        attempt = connection.execute(
            "SELECT * FROM source_attempts WHERE id=?",
            (value.listing_source_attempt_id,),
        ).fetchone()
        run = connection.execute(
            "SELECT * FROM request_runs WHERE id=?",
            (value.brief_request_run_id,),
        ).fetchone()
        same_attempt_count = connection.execute(
            """
            SELECT count(*) FROM source_attempts
            WHERE request_run_id=?
              AND source_id='fund_manager_official_documents'
              AND field_id='fund_manager_product_announcement'
              AND subject_key=?
            """,
            (value.brief_request_run_id, f"fund:{value.fund_code}"),
        ).fetchone()[0]
        if (
            attempt is None
            or run is None
            or attempt["request_run_id"] != value.brief_request_run_id
            or attempt["source_id"] != "fund_manager_official_documents"
            or attempt["field_id"] != "fund_manager_product_announcement"
            or attempt["subject_key"] != f"fund:{value.fund_code}"
            or attempt["attempt_number"] != 1
            or attempt["force_actor"] is not None
            or attempt["force_reason"] is not None
            or attempt["authorization_id"] is not None
            or attempt["registry_version"] != self.source_registry.version
            or attempt["registry_checksum"] != self.source_registry.checksum()
            or same_attempt_count != 1
            or _stored_utc(attempt["started_at"]) < _stored_utc(run["started_at"])
            or _stored_utc(attempt["finished_at"]) > _stored_utc(run["deadline_at"])
            or value.created_at > _stored_utc(run["deadline_at"])
            or _stored_utc(attempt["finished_at"]) > value.created_at
            or (
                value.official_negative_check_complete
                and attempt["outcome"] not in {"success", "cache_hit"}
            )
        ):
            raise HoldingReviewStoreError(
                "official check closure source attempt binding failed"
            )
        manager_identity = self._authenticate_official_manager_identity(connection, value)
        normalized_manager = manager_identity.normalized_name
        registrations = tuple(
            item
            for item in OFFICIAL_SOURCE_REGISTRATIONS
            if normalized_manager is not None and item.matches_identity(normalized_manager)
        )
        expected_registration_ids = tuple(item.registration_id for item in registrations)
        if value.source_registration_ids != expected_registration_ids:
            raise HoldingReviewStoreError(
                "official check closure source registration binding failed"
            )
        registration_by_id = {item.registration_id: item for item in registrations}
        candidate_rows, manifest_complete = self._authenticate_official_listing_manifest(
            connection,
            value,
            attempt,
            registration_by_id,
            manager_identity.fund_name,
        )
        self._authenticate_official_candidate_closure(
            connection,
            value,
            candidate_rows,
            require_running=require_running,
        )
        if value.official_negative_check_complete and not manifest_complete:
            raise HoldingReviewStoreError(
                "official check closure page manifest authentication failed"
            )

    def _authenticate_official_manager_identity(
        self,
        connection: sqlite3.Connection,
        value: OfficialCheckClosure,
    ) -> AuthenticatedOfficialManagerIdentity:
        observed = self._resolve_official_manager_identity(
            connection,
            value.fund_code,
            value.created_at,
        )
        if observed.state is not value.manager_identity_state:
            raise HoldingReviewStoreError(
                "official check closure manager identity state authentication failed"
            )
        if observed.state is not OfficialManagerIdentityState.PRESENT:
            if value.source_registration_ids or value.listing_page_evidence:
                raise HoldingReviewStoreError(
                    "official check closure non-present identity has listing evidence"
                )
            return observed
        if (
            value.manager_identity_row_id != observed.row_id
            or value.manager_identity_source_document_id != observed.source_document_id
            or value.manager_identity_source_document_checksum
            != observed.source_document_checksum
            or value.manager_identity_normalized_name != observed.normalized_name
            or value.manager_identity_fingerprint != observed.fingerprint
        ):
            raise HoldingReviewStoreError(
                "official check closure manager identity binding failed"
            )
        return observed

    def _resolve_official_manager_identity(
        self,
        connection: sqlite3.Connection,
        fund_code: str,
        as_of: datetime,
    ) -> AuthenticatedOfficialManagerIdentity:
        rows = connection.execute(
            """
            SELECT identity.*, document.checksum AS source_document_checksum,
                   document.retrieved_at AS source_document_retrieved_at,
                   document.document_kind AS source_document_kind,
                   document.source_tier AS source_document_tier
            FROM fund_section_syncs AS sync
            JOIN fund_source_documents AS document
              ON document.id=sync.current_source_document_id
            JOIN fund_identities AS identity
              ON identity.fund_code=sync.fund_code
             AND identity.source_document_id=document.id
            WHERE sync.fund_code=? AND sync.section='basic_profile'
              AND sync.state='success'
            ORDER BY identity.id
            """,
            (fund_code,),
        ).fetchall()
        row = None if len(rows) != 1 else rows[0]
        if not rows or (row is not None and row["manager_name"] is None):
            state = OfficialManagerIdentityState.MISSING
        elif len(rows) != 1:
            state = OfficialManagerIdentityState.CONFLICTED
        else:
            retrieved_at = _stored_utc(row["source_document_retrieved_at"])
            if (
                retrieved_at > as_of
                or row["status"] != "active"
                or row["source_document_kind"] != "basic_profile"
                or row["source_document_tier"] not in {1, 2}
            ):
                state = OfficialManagerIdentityState.CONFLICTED
            elif retrieved_at < as_of - timedelta(
                days=self.official_policy.manager_identity_maximum_age_days
            ):
                state = OfficialManagerIdentityState.STALE
            else:
                state = OfficialManagerIdentityState.PRESENT
        if state is not OfficialManagerIdentityState.PRESENT:
            result = AuthenticatedOfficialManagerIdentity(
                fund_code=fund_code,
                state=state,
                row_id=None,
                source_document_id=None,
                source_document_checksum=None,
                normalized_name=None,
                fingerprint=None,
                fund_name=None,
            )
            result.validate()
            return result
        assert row is not None
        normalized_name = _normalized_identity(str(row["manager_name"]))
        fingerprint = _manager_identity_fingerprint(
            fund_code=fund_code,
            identity_row_id=int(row["id"]),
            fund_name=str(row["fund_name"]),
            manager_name=normalized_name,
            source_document_id=int(row["source_document_id"]),
            source_document_checksum=str(row["source_document_checksum"]),
        )
        result = AuthenticatedOfficialManagerIdentity(
            fund_code=fund_code,
            state=state,
            row_id=int(row["id"]),
            source_document_id=int(row["source_document_id"]),
            source_document_checksum=str(row["source_document_checksum"]),
            normalized_name=normalized_name,
            fingerprint=fingerprint,
            fund_name=str(row["fund_name"]),
        )
        result.validate()
        return result

    def _authenticate_official_listing_manifest(
        self,
        connection: sqlite3.Connection,
        value: OfficialCheckClosure,
        attempt: Mapping[str, object],
        registrations: Mapping[str, object],
        product_name: Optional[str],
    ) -> tuple[tuple[tuple[int, OfficialListingItem], ...], bool]:
        attempt_started = _stored_utc(attempt["started_at"])
        attempt_finished = _stored_utc(attempt["finished_at"])
        if sum(page.raw_byte_count for page in value.listing_page_evidence) != int(
            attempt["response_byte_count"]
        ):
            raise HoldingReviewStoreError(
                "official check closure page byte binding failed"
            )
        item_rows: list[tuple[int, OfficialListingItem]] = []
        seen_item_urls: set[str] = set()
        manifest_complete = bool(registrations)
        prior_oldest_by_registration: dict[str, datetime] = {}
        for page in value.listing_page_evidence:
            registration = registrations.get(page.registration_id)
            if registration is None:
                raise HoldingReviewStoreError(
                    "official check closure page registration binding failed"
                )
            if not attempt_started <= page.retrieved_at <= attempt_finished:
                raise HoldingReviewStoreError(
                    "official check closure page capture binding failed"
                )
            document = connection.execute(
                "SELECT * FROM fund_source_documents WHERE id=?",
                (page.source_document_id,),
            ).fetchone()
            host = (urlsplit(page.canonical_page_url).hostname or "").lower().rstrip(".")
            if (
                document is None
                or document["fund_code"] != value.fund_code
                or document["document_kind"] != "announcement"
                or document["source_name"] != "fund_manager_official_documents"
                or document["source_tier"] != 1
                or document["url"] != page.canonical_page_url
                or document["checksum"] != page.raw_sha256
                or host not in registration.accepted_hosts
                or not registration.matches_identity(str(document["publisher"]))
            ):
                raise HoldingReviewStoreError(
                    "official check closure page document binding failed"
                )
            rows = connection.execute(
                """
                SELECT * FROM fund_announcements
                WHERE fund_code=? AND source_document_id=?
                ORDER BY id
                """,
                (value.fund_code, page.source_document_id),
            ).fetchall()
            page_items: list[OfficialListingItem] = []
            page_pairs: list[tuple[int, OfficialListingItem]] = []
            for index, row in enumerate(rows, start=1):
                item = OfficialListingItem(
                    registration_id=page.registration_id,
                    page_number=page.page_number,
                    page_local_index=index,
                    title=str(row["title"]),
                    canonical_url=str(row["url"]),
                    publisher=str(row["publisher"]),
                    published_at=_stored_utc(row["published_at"]),
                )
                item.validate()
                if (
                    row["source_tier"] != 1
                    or item.publisher != document["publisher"]
                    or item.canonical_url in seen_item_urls
                ):
                    raise HoldingReviewStoreError(
                        "official check closure listing row binding failed"
                    )
                seen_item_urls.add(item.canonical_url)
                page_items.append(item)
                page_pairs.append((int(row["id"]), item))
            page_checksum = hashlib.sha256(
                canonical_json_bytes([item.to_canonical_dict() for item in page_items])
            ).hexdigest()
            if len(page_items) != page.parsed_item_count:
                missing_date_gap = "official_listing_publication_date_missing" in set(
                    value.gap_codes
                )
                if (
                    value.official_negative_check_complete
                    or len(page_items) > page.parsed_item_count
                    or not missing_date_gap
                ):
                    raise HoldingReviewStoreError(
                        "official check closure parsed page authentication failed"
                    )
                manifest_complete = False
            elif page_checksum != page.parsed_items_sha256:
                raise HoldingReviewStoreError(
                    "official check closure parsed page authentication failed"
                )
            dates = tuple(item.published_at for item in page_items)
            if any(left < right for left, right in zip(dates, dates[1:])):
                manifest_complete = False
            prior_oldest = prior_oldest_by_registration.get(page.registration_id)
            if dates and prior_oldest is not None and dates[0] > prior_oldest:
                manifest_complete = False
            if dates:
                prior_oldest_by_registration[page.registration_id] = dates[-1]
            if (
                page.terminal_state is OfficialListingTerminalState.WINDOW_BOUNDARY_REACHED
                and (not dates or dates[-1] >= value.window_start)
            ):
                manifest_complete = False
            for pair in page_pairs:
                published_at = pair[1].published_at
                if (
                    published_at is not None
                    and value.window_start <= published_at < value.window_end
                ):
                    item_rows.append(pair)
                elif published_at is not None and published_at >= value.window_end:
                    manifest_complete = False
        for registration_id in registrations:
            pages = tuple(
                page
                for page in value.listing_page_evidence
                if page.registration_id == registration_id
            )
            if (
                not pages
                or tuple(page.page_number for page in pages)
                != tuple(range(1, len(pages) + 1))
                or len({page.reported_total_pages for page in pages}) != 1
                or pages[-1].terminal_state is None
                or any(page.terminal_state is not None for page in pages[:-1])
                or (
                    pages[-1].terminal_state
                    is OfficialListingTerminalState.SOURCE_FINAL_PAGE
                    and pages[-1].page_number != pages[-1].reported_total_pages
                )
            ):
                manifest_complete = False
        if len(item_rows) != value.listing_count:
            raise HoldingReviewStoreError(
                "official check closure listing count authentication failed"
            )
        if product_name is None and item_rows:
            raise HoldingReviewStoreError(
                "official check closure product identity authentication failed"
            )
        all_candidates = tuple(
            pair
            for pair in item_rows
            if product_name is not None
            and classify_official_listing_title(
                pair[1].title,
                product_name,
                self.official_policy,
            )
            != "ordinary"
        )
        candidates = all_candidates[:OFFICIAL_MAXIMUM_CANDIDATES]
        cap_reached = len(all_candidates) > OFFICIAL_MAXIMUM_CANDIDATES
        body_limit_gap = bool(
            {"announcement_body_limit", "official_announcement_total_limit"}
            .intersection(value.gap_codes)
        )
        expected_body_cap = cap_reached or body_limit_gap
        if (
            len(candidates) != value.candidate_count
            or value.candidate_cap_reached != cap_reached
            or value.body_cap_reached != expected_body_cap
            or (
                body_limit_gap
                and value.authenticated_body_count >= value.candidate_count
            )
        ):
            raise HoldingReviewStoreError(
                "official check closure candidate authentication failed"
            )
        return candidates, manifest_complete

    def _authenticate_official_candidate_closure(
        self,
        connection: sqlite3.Connection,
        value: OfficialCheckClosure,
        candidate_rows: tuple[tuple[int, OfficialListingItem], ...],
        *,
        require_running: bool,
    ) -> None:
        candidates_by_id = {row_id: item for row_id, item in candidate_rows}
        contents_by_candidate: dict[int, tuple[int, OfficialAnnouncementContent]] = {}
        content_rows = connection.execute(
            """
            SELECT * FROM fund_official_announcement_contents
            WHERE brief_request_run_id=? AND fund_code=? ORDER BY id
            """,
            (value.brief_request_run_id, value.fund_code),
        ).fetchall()
        for row in content_rows:
            content_id, content = self._stored_announcement_content(row)
            self._authenticate_announcement_content_references(
                connection,
                content,
                require_running=require_running,
            )
            matches = tuple(
                candidate_id
                for candidate_id, candidate in candidate_rows
                if content.listing_source_document_id
                == _announcement_source_document_id(connection, candidate_id)
                and content.canonical_announcement_url == candidate.canonical_url
                and content.announcement_title == candidate.title
                and content.announcement_published_at == candidate.published_at
                and content.publisher == candidate.publisher
            )
            if len(matches) != 1 or matches[0] in contents_by_candidate:
                raise HoldingReviewStoreError(
                    "official check closure duplicate or extra body binding failed"
                )
            contents_by_candidate[matches[0]] = (content_id, content)
        active_contents = {
            candidate_id: stored
            for candidate_id, stored in contents_by_candidate.items()
            if stored[1].integrity_status == "active"
        }
        event_rows = connection.execute(
            """
            SELECT * FROM held_review_official_event_projections
            WHERE brief_request_run_id=? AND fund_code=? ORDER BY id
            """,
            (value.brief_request_run_id, value.fund_code),
        ).fetchall()
        events_by_candidate: dict[int, int] = {}
        for row in event_rows:
            projection_id, event = self._stored_official_event(row)
            self._authenticate_official_event_references(
                connection,
                event,
                require_running=require_running,
            )
            active = active_contents.get(event.announcement_row_id)
            if (
                event.announcement_row_id not in candidates_by_id
                or active is None
                or event.announcement_content_id != active[0]
                or event.announcement_row_id in events_by_candidate
            ):
                raise HoldingReviewStoreError(
                    "official check closure extra or cross-event binding failed"
                )
            events_by_candidate[event.announcement_row_id] = projection_id
        if (
            value.authenticated_body_count != len(active_contents)
            or value.projected_event_count != len(events_by_candidate)
        ):
            raise HoldingReviewStoreError(
                "official check closure body/event count authentication failed"
            )
        expected_candidate_ids = set(candidates_by_id)
        if value.official_negative_check_complete and (
            set(active_contents) != expected_candidate_ids
            or set(events_by_candidate) != expected_candidate_ids
        ):
            raise HoldingReviewStoreError(
                "official check closure candidate mapping authentication failed"
            )

    def _authenticate_announcement_content_references(
        self,
        connection: sqlite3.Connection,
        value: OfficialAnnouncementContent,
        *,
        require_running: bool,
    ) -> None:
        self._authenticate_deep_request(
            connection,
            value.brief_request_run_id,
            require_running=require_running,
        )
        attempt = connection.execute(
            "SELECT * FROM source_attempts WHERE id=?", (value.source_attempt_id,)
        ).fetchone()
        if (
            attempt is None
            or attempt["request_run_id"] != value.brief_request_run_id
            or attempt["source_id"] != value.original_source_id
            or attempt["field_id"] != "fund_manager_product_announcement"
            or attempt["subject_key"] != f"fund:{value.fund_code}"
            or attempt["outcome"] not in {"success", "cache_hit"}
        ):
            raise HoldingReviewStoreError("official announcement content binding failed")
        document = connection.execute(
            "SELECT * FROM fund_source_documents WHERE id=?",
            (value.listing_source_document_id,),
        ).fetchone()
        if (
            document is None
            or document["fund_code"] != value.fund_code
            or document["document_kind"] != "announcement"
            or document["source_name"] != value.original_source_id
            or document["source_tier"] != 1
            or document["publisher"] != value.publisher
        ):
            raise HoldingReviewStoreError("official announcement content binding failed")
        announcements = connection.execute(
            """
            SELECT id FROM fund_announcements
            WHERE source_document_id=? AND fund_code=? AND url=? AND title=?
              AND published_at=? AND publisher=? AND source_tier=1
            ORDER BY id LIMIT 2
            """,
            (
                value.listing_source_document_id,
                value.fund_code,
                value.canonical_announcement_url,
                value.announcement_title,
                _utc_text(value.announcement_published_at),
                value.publisher,
            ),
        ).fetchall()
        if len(announcements) != 1:
            raise HoldingReviewStoreError("official announcement content binding failed")

    def _authenticate_official_event_references(
        self,
        connection: sqlite3.Connection,
        value: HeldReviewOfficialEventProjection,
        *,
        require_running: bool,
    ) -> None:
        self._authenticate_deep_request(
            connection,
            value.brief_request_run_id,
            require_running=require_running,
        )
        if (
            value.policy_version != self.policy.version
            or value.policy_checksum != self.policy.checksum()
        ):
            raise HoldingReviewStoreError("official event policy authentication failed")
        content_row = connection.execute(
            "SELECT * FROM fund_official_announcement_contents WHERE id=?",
            (value.announcement_content_id,),
        ).fetchone()
        if content_row is None:
            raise HoldingReviewStoreError("official event projection binding failed")
        content_id, content = self._stored_announcement_content(content_row)
        self._authenticate_announcement_content_references(
            connection, content, require_running=require_running
        )
        if (
            content_id != value.announcement_content_id
            or content.brief_request_run_id != value.brief_request_run_id
            or content.fund_code != value.fund_code
            or content.integrity_status != "active"
        ):
            raise HoldingReviewStoreError("official event projection binding failed")
        announcement = connection.execute(
            "SELECT * FROM fund_announcements WHERE id=?",
            (value.announcement_row_id,),
        ).fetchone()
        if (
            announcement is None
            or announcement["fund_code"] != value.fund_code
            or announcement["source_document_id"] != content.listing_source_document_id
            or announcement["url"] != content.canonical_announcement_url
            or announcement["title"] != content.announcement_title
            or _stored_utc(announcement["published_at"])
            != content.announcement_published_at
            or announcement["publisher"] != content.publisher
            or announcement["source_tier"] != 1
        ):
            raise HoldingReviewStoreError("official event projection binding failed")

    @staticmethod
    def _stored_announcement_content(
        row: Mapping[str, object],
    ) -> tuple[int, OfficialAnnouncementContent]:
        value = OfficialAnnouncementContent(
            brief_request_run_id=_positive_id(
                row["brief_request_run_id"], "brief request run id"
            ),
            source_attempt_id=_positive_id(row["source_attempt_id"], "source attempt id"),
            fund_code=_fund_code(row["fund_code"]),
            listing_source_document_id=_positive_id(
                row["listing_source_document_id"], "listing source document id"
            ),
            canonical_announcement_url=str(row["canonical_announcement_url"]),
            announcement_title=str(row["announcement_title"]),
            announcement_published_at=_stored_utc(row["announcement_published_at"]),
            publisher=str(row["publisher"]),
            normalized_content=str(row["normalized_content"]),
            normalized_content_bytes=_positive_id(
                row["normalized_content_bytes"], "normalized content byte count"
            ),
            normalized_content_sha256=_checksum(
                row["normalized_content_sha256"], "normalized content checksum"
            ),
            original_source_id=str(row["original_source_id"]),
            quoted_source_id=_optional_text(row["quoted_source_id"]),
            integrity_status=str(row["integrity_status"]),
            integrity_checked_at=_stored_utc(row["integrity_checked_at"]),
            retrieved_at=_stored_utc(row["retrieved_at"]),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        return _positive_id(row["id"], "announcement content id"), value

    @staticmethod
    def _stored_official_event(
        row: Mapping[str, object],
    ) -> tuple[int, HeldReviewOfficialEventProjection]:
        value = HeldReviewOfficialEventProjection(
            brief_request_run_id=_positive_id(
                row["brief_request_run_id"], "brief request run id"
            ),
            fund_code=_fund_code(row["fund_code"]),
            announcement_row_id=_positive_id(
                row["announcement_row_id"], "announcement row id"
            ),
            announcement_content_id=_positive_id(
                row["announcement_content_id"], "announcement content id"
            ),
            event_code=OfficialEventCode(str(row["event_code"])),
            triggered_review_code=TriggeredReviewCode(str(row["triggered_review_code"])),
            policy_version=str(row["policy_version"]),
            policy_checksum=_checksum(row["policy_checksum"], "policy checksum"),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        return _positive_id(row["id"], "official event projection id"), value

    @staticmethod
    def _stored_official_check_closure(
        row: Mapping[str, object],
    ) -> StoredOfficialCheckClosure:
        page_values = _json_value(row["listing_page_evidence_json"], list)
        pages = tuple(
            OfficialListingPageEvidence(
                registration_id=str(item["registration_id"]),
                page_number=int(item["page_number"]),
                reported_total_pages=int(item["reported_total_pages"]),
                canonical_page_url=str(item["canonical_page_url"]),
                raw_byte_count=int(item["raw_byte_count"]),
                raw_sha256=_checksum(item["raw_sha256"], "raw page checksum"),
                retrieved_at=_stored_utc(item["retrieved_at"]),
                parsed_item_count=int(item["parsed_item_count"]),
                parsed_items_sha256=_checksum(
                    item["parsed_items_sha256"], "parsed item checksum"
                ),
                terminal_state=(
                    None
                    if item["terminal_state"] is None
                    else OfficialListingTerminalState(str(item["terminal_state"]))
                ),
                source_document_id=_positive_id(
                    item["source_document_id"], "listing source document id"
                ),
            )
            for item in page_values
        )
        value = OfficialCheckClosure(
            brief_request_run_id=_positive_id(
                row["brief_request_run_id"], "brief request run id"
            ),
            fund_code=_fund_code(row["fund_code"]),
            listing_source_attempt_id=_positive_id(
                row["listing_source_attempt_id"], "listing source attempt id"
            ),
            official_registry_version=str(row["official_registry_version"]),
            official_registry_checksum=_checksum(
                row["official_registry_checksum"], "official registry checksum"
            ),
            source_registration_ids=tuple(
                str(item)
                for item in _json_value(row["source_registration_ids_json"], list)
            ),
            manager_identity_state=OfficialManagerIdentityState(
                str(row["manager_identity_state"])
            ),
            manager_identity_row_id=_optional_id(row["manager_identity_row_id"]),
            manager_identity_source_document_id=_optional_id(
                row["manager_identity_source_document_id"]
            ),
            manager_identity_source_document_checksum=(
                None
                if row["manager_identity_source_document_checksum"] is None
                else _checksum(
                    row["manager_identity_source_document_checksum"],
                    "manager identity source document checksum",
                )
            ),
            manager_identity_normalized_name=_optional_text(
                row["manager_identity_normalized_name"]
            ),
            manager_identity_fingerprint=(
                None
                if row["manager_identity_fingerprint"] is None
                else _checksum(
                    row["manager_identity_fingerprint"],
                    "manager identity fingerprint",
                )
            ),
            listing_page_evidence=pages,
            window_start=_stored_utc(row["window_start"]),
            window_end=_stored_utc(row["window_end"]),
            listing_count=int(row["listing_count"]),
            candidate_count=int(row["candidate_count"]),
            authenticated_body_count=int(row["authenticated_body_count"]),
            projected_event_count=int(row["projected_event_count"]),
            listing_truncated=bool(row["listing_truncated"]),
            candidate_cap_reached=bool(row["candidate_cap_reached"]),
            body_cap_reached=bool(row["body_cap_reached"]),
            gap_codes=tuple(
                str(item) for item in _json_value(row["gap_codes_json"], list)
            ),
            official_negative_check_complete=bool(
                row["official_negative_check_complete"]
            ),
            policy_version=str(row["policy_version"]),
            policy_checksum=_checksum(row["policy_checksum"], "policy checksum"),
            official_check_policy_version=str(row["official_check_policy_version"]),
            official_check_policy_checksum=_checksum(
                row["official_check_policy_checksum"],
                "official check policy checksum",
            ),
            created_at=_stored_utc(row["created_at"]),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        return StoredOfficialCheckClosure(
            id=_positive_id(row["id"], "official check closure id"),
            value=value,
        )

    def _authenticate_projection_references(
        self,
        connection: sqlite3.Connection,
        value: ThesisMatchProjection,
        *,
        require_active_thesis: bool = True,
    ) -> None:
        intelligence = self._intelligence_by_request(
            connection, value.intelligence_request_run_id
        )
        if (
            intelligence.id != value.intelligence_snapshot_id
            or intelligence.result_checksum != value.intelligence_snapshot_checksum
            or intelligence.snapshot.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE
            or intelligence.snapshot.subject_fund_code != value.fund_code
        ):
            raise HoldingReviewStoreError("thesis match projection binding failed")
        thesis = None
        if value.thesis_id is not None:
            thesis = _authenticated_thesis(
                connection,
                value.thesis_id,
                require_active=require_active_thesis,
            )
            fingerprint_thesis = (
                thesis if require_active_thesis else replace(thesis, active=True)
            )
            if (
                thesis.fund_code != value.fund_code
                or thesis_record_fingerprint(value.thesis_id, fingerprint_thesis)
                != value.thesis_fingerprint
            ):
                raise HoldingReviewStoreError("thesis match projection binding failed")
        elif require_active_thesis:
            _authenticate_no_active_thesis(connection, value.fund_code)
        derived = self._derived_evidence_descriptors(connection, intelligence)
        items = self._authenticated_projection_items(connection, intelligence)
        expected_policy_checksum = _thesis_matcher_policy_v1_checksum()
        if (
            value.matcher_policy_version != "1"
            or value.matcher_policy_checksum != expected_policy_checksum
        ):
            raise HoldingReviewStoreError("thesis matcher policy authentication failed")
        matched_ids = () if thesis is None else _v1_thesis_match_item_ids(thesis, items)
        expected_state = (
            ThesisMatchProjectionState.THESIS_MISSING
            if thesis is None
            else (
                ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
                if matched_ids
                else ThesisMatchProjectionState.NO_MATCHING_EVIDENCE
            )
        )
        try:
            expected = tuple(derived[item_id] for item_id in matched_ids)
        except KeyError:
            raise HoldingReviewStoreError(
                "thesis match projection evidence descriptor authentication failed"
            ) from None
        if expected_state is not value.projection_state or expected != value.evidence_descriptors:
            raise HoldingReviewStoreError(
                "thesis match projection evidence descriptor authentication failed"
            )

    def _authenticated_projection_items(
        self,
        connection: sqlite3.Connection,
        intelligence: StoredIntelligenceSnapshot,
    ) -> tuple[NewsItem, ...]:
        items = []
        for item_id in intelligence.snapshot.item_ids:
            row = connection.execute(
                "SELECT id FROM intelligence_news_items WHERE item_key=?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError(
                    "thesis projection input authentication failed"
                )
            items.append(
                self.intelligence_store._authenticated_item(connection, int(row["id"]))
            )
        return tuple(items)

    def _authenticate_review_references(
        self,
        connection: sqlite3.Connection,
        value: HoldingReviewSnapshot,
        *,
        require_current_adjudication: bool = True,
    ) -> None:
        brief = self._brief_by_request(connection, value.brief_request_run_id)
        if (
            brief.id != value.brief_snapshot_id
            or brief.result_checksum != value.brief_snapshot_checksum
            or brief.snapshot.fund_code != value.fund_code
            or value.action.value not in brief.snapshot.action_ids
        ):
            raise HoldingReviewStoreError("holding review snapshot binding failed")
        closure_rows = connection.execute(
            "SELECT * FROM held_review_official_check_closures "
            "WHERE brief_request_run_id=? AND fund_code=? ORDER BY id",
            (value.brief_request_run_id, value.fund_code),
        ).fetchall()
        if brief.snapshot.mode is RequestMode.RAPID:
            if (
                closure_rows
                or value.result.official_negative_check_complete
                or value.result.official_event_evidence
            ):
                raise HoldingReviewStoreError("holding review preview boundary failed")
        elif brief.snapshot.mode is RequestMode.DEEP:
            if len(closure_rows) > 1:
                raise HoldingReviewStoreError(
                    "holding review official closure binding failed"
                )
            if not closure_rows:
                if (
                    value.result.official_negative_check_complete
                    or value.result.official_event_evidence
                ):
                    raise HoldingReviewStoreError(
                        "holding review official closure binding failed"
                    )
            else:
                closure = self._stored_official_check_closure(closure_rows[0])
                self._authenticate_official_check_closure(
                    connection,
                    closure,
                    require_running=False,
                )
                expected_references = self.authenticated_official_event_references(
                    value.brief_request_run_id,
                    value.fund_code,
                )
                if (
                    value.result.official_negative_check_complete
                    is not closure.value.official_negative_check_complete
                    or value.result.official_event_evidence != expected_references
                ):
                    raise HoldingReviewStoreError(
                        "holding review official evidence binding failed"
                    )
        else:
            raise HoldingReviewStoreError("holding review request mode binding failed")
        intelligence = self._intelligence_by_request(
            connection, value.intelligence_request_run_id
        )
        if (
            intelligence.id != value.intelligence_snapshot_id
            or intelligence.result_checksum != value.intelligence_snapshot_checksum
            or intelligence.snapshot.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE
            or intelligence.snapshot.subject_fund_code != value.fund_code
        ):
            raise HoldingReviewStoreError("holding review snapshot binding failed")
        projection = self._projection_by_id(connection, value.thesis_match_projection_id)
        self._authenticate_projection_references(
            connection,
            projection.value,
            require_active_thesis=require_current_adjudication,
        )
        if (
            projection.value.record_checksum != value.thesis_match_projection_checksum
            or projection.value.fund_code != value.fund_code
            or projection.value.intelligence_request_run_id != value.intelligence_request_run_id
            or projection.value.intelligence_snapshot_id != value.intelligence_snapshot_id
        ):
            raise HoldingReviewStoreError("holding review snapshot binding failed")
        if value.active_thesis_id is not None:
            thesis = _authenticated_thesis(
                connection,
                value.active_thesis_id,
                require_active=require_current_adjudication,
            )
            fingerprint_thesis = (
                thesis
                if require_current_adjudication
                else replace(thesis, active=True)
            )
            if (
                thesis.fund_code != value.fund_code
                or thesis_record_fingerprint(value.active_thesis_id, fingerprint_thesis)
                != value.active_thesis_fingerprint
            ):
                raise HoldingReviewStoreError("holding review snapshot binding failed")
        elif require_current_adjudication:
            _authenticate_no_active_thesis(connection, value.fund_code)
        adjudication_value = None
        if value.adjudication_id is not None:
            row = connection.execute(
                "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
                (value.adjudication_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError("holding review snapshot binding failed")
            adjudication = self._stored_adjudication(row)
            adjudication_value = adjudication.value
            if (
                adjudication.value.record_checksum != value.adjudication_checksum
                or adjudication.value.thesis_match_projection_id != projection.id
            ):
                raise HoldingReviewStoreError("holding review snapshot binding failed")
            self._authenticate_adjudication_projection(adjudication.value, projection)
            if require_current_adjudication:
                current = self._current_adjudication_row(connection, projection.id)
                if current is None or int(current["id"]) != adjudication.id:
                    raise HoldingReviewStoreError(
                        "holding review requires the unique current adjudication"
                    )
        elif require_current_adjudication and (
            self._current_adjudication_row(connection, projection.id) is not None
        ):
            raise HoldingReviewStoreError(
                "holding review omitted the current adjudication"
            )
        expected_thesis_state = (
            _PROJECTION_REVIEW_STATES[projection.value.projection_state]
            if adjudication_value is None
            else _ADJUDICATION_REVIEW_STATES[adjudication_value.decision]
        )
        if value.result.thesis_review_state is not expected_thesis_state:
            raise HoldingReviewStoreError(
                "holding review thesis review state authentication failed"
            )
        authenticated_evidence = self._derived_evidence_descriptors(
            connection,
            intelligence,
        )
        bound_evidence_ids = set(projection.value.evidence_ids)
        if adjudication_value is not None:
            bound_evidence_ids.update(adjudication_value.evidence_ids)
        result_evidence_ids = set(value.result.evidence_ids)
        if not bound_evidence_ids.issubset(result_evidence_ids):
            raise HoldingReviewStoreError(
                "holding review result omits bound evidence"
            )
        official_support_ids = {
            reference.support_evidence_id
            for reference in value.result.official_event_evidence
        }
        non_official_ids = result_evidence_ids.difference(official_support_ids)
        if not non_official_ids.issubset(authenticated_evidence):
            raise HoldingReviewStoreError(
                "holding review result contains unauthenticated evidence"
            )
        if value.previous_review_id is not None:
            row = connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?",
                (value.previous_review_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError("holding review snapshot binding failed")
            previous = self._stored_review(row)
            if (
                not _reviews_comparable(previous.value, value)
                or previous.value.created_at > value.created_at
            ):
                raise HoldingReviewStoreError(
                    "holding review previous row is not exactly comparable"
                )
            self._authenticate_review_references(
                connection,
                previous.value,
                require_current_adjudication=False,
            )
        if (
            value.policy_version != self.policy.version
            or value.policy_checksum != self.policy.checksum()
        ):
            raise HoldingReviewStoreError("holding review snapshot policy authentication failed")

    def _authenticate_latest_comparable_previous(
        self,
        connection: sqlite3.Connection,
        value: HoldingReviewSnapshot,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM holding_review_snapshots
            WHERE fund_code=? AND action=?
              AND active_thesis_state=? AND active_thesis_id IS ?
              AND active_thesis_fingerprint IS ?
              AND policy_version=? AND policy_checksum=?
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (
                value.fund_code,
                value.action.value,
                value.active_thesis_state.value,
                value.active_thesis_id,
                value.active_thesis_fingerprint,
                value.policy_version,
                value.policy_checksum,
            ),
        ).fetchone()
        latest_id = None if row is None else _positive_id(row["id"], "previous review id")
        if latest_id != value.previous_review_id:
            raise HoldingReviewStoreError(
                "holding review must bind the latest comparable previous row"
            )
        if row is not None:
            stored = self._stored_review(row)
            if not _reviews_comparable(stored.value, value):
                raise HoldingReviewStoreError(
                    "holding review latest comparable authentication failed"
                )

    def _derived_evidence_descriptors(
        self,
        connection: sqlite3.Connection,
        intelligence: StoredIntelligenceSnapshot,
    ) -> dict[str, ReviewEvidenceItem]:
        snapshot = intelligence.snapshot
        run = connection.execute(
            "SELECT status FROM request_runs WHERE id=?",
            (snapshot.request_run_id,),
        ).fetchone()
        graph_complete = run is not None and run["status"] == "complete"
        edges = []
        for edge_id in snapshot.lineage_edge_ids:
            row = connection.execute(
                "SELECT * FROM intelligence_lineage_edges WHERE edge_key=?",
                (edge_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError(
                    "thesis match projection evidence descriptor authentication failed"
                )
            edge = self.intelligence_store._lineage_from_authenticated_row(connection, row)
            edges.append(edge)
        lineage_states = _derive_lineage_states(
            snapshot.item_ids,
            tuple(edges),
            graph_complete=graph_complete,
        )

        conflicted_ids = set(snapshot.conflicts)
        for event_id in snapshot.event_ids:
            event = self.intelligence_store._authenticate_event_row(connection, event_id)
            event_items = {
                *event.supporting_item_ids,
                *event.opposing_item_ids,
                *event.correction_item_ids,
                *event.retraction_item_ids,
            }
            if (
                event.confidence_state is EventConfidenceState.CONFLICTED
                or event.event_id in snapshot.conflicts
            ):
                conflicted_ids.update(event_items)
        for observation in snapshot.market_state.dimensions:
            if (
                observation.state is DimensionState.CONFLICTED
                or observation.conflict_ids
                or observation.observation_id in snapshot.conflicts
            ):
                conflicted_ids.update(observation.evidence_ids)

        relevant_links = set(snapshot.fund_relevance_link_ids)
        entities = {item.entity_id: item for item in snapshot.entities}
        subject_entity_id = f"fund_{snapshot.subject_fund_code}"
        direct_ids = {
            evidence_id
            for link in snapshot.event_entity_links
            if link.link_id in relevant_links
            and link.relationship is EventEntityRelationship.SUBJECT
            and link.entity_id == subject_entity_id
            and entities[link.entity_id].entity_type == "fund"
            for evidence_id in link.evidence_ids
        }
        result = {}
        for item_id in snapshot.item_ids:
            row = connection.execute(
                "SELECT id FROM intelligence_news_items WHERE item_key=?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError(
                    "thesis match projection evidence descriptor authentication failed"
                )
            item = self.intelligence_store._authenticated_item(connection, int(row["id"]))
            lineage_kind, closed = lineage_states[item_id]
            result[item_id] = ReviewEvidenceItem(
                evidence_id=item_id,
                source_tier=(1 if item.source_tier is SourceTier.TIER_1 else 2),
                lineage_kind=lineage_kind,
                current=item.integrity_state is IntegrityState.ACTIVE,
                graph_closed=closed,
                original_lineage=lineage_kind is LineageKind.ORIGINAL,
                retracted=item.integrity_state is IntegrityState.RETRACTED,
                conflicted=item_id in conflicted_ids,
                direct_subject_binding=item_id in direct_ids,
            )
        return result

    def _brief_by_request(
        self, connection: sqlite3.Connection, request_run_id: int
    ) -> StoredBriefSnapshot:
        row = connection.execute(
            "SELECT * FROM fund_brief_snapshots WHERE request_run_id=?", (request_run_id,)
        ).fetchone()
        if row is None:
            raise HoldingReviewStoreError("brief request snapshot authentication failed")
        policy = self.brief_store._load_policy(connection)
        return self.brief_store._stored_snapshot(row, policy, connection)

    def _intelligence_by_request(
        self, connection: sqlite3.Connection, request_run_id: int
    ) -> StoredIntelligenceSnapshot:
        row = connection.execute(
            "SELECT * FROM intelligence_snapshots WHERE request_run_id=?", (request_run_id,)
        ).fetchone()
        if row is None:
            raise HoldingReviewStoreError("intelligence request snapshot authentication failed")
        return self.intelligence_store._authenticated_snapshot_row(connection, row)

    def _projection_by_id(
        self, connection: sqlite3.Connection, projection_id: int
    ) -> StoredThesisMatchProjection:
        row = connection.execute(
            "SELECT * FROM thesis_match_projections WHERE id=?", (projection_id,)
        ).fetchone()
        if row is None:
            raise HoldingReviewStoreError("thesis match projection authentication failed")
        return self._stored_projection(row)

    @staticmethod
    def _authenticate_adjudication_projection(
        value: ThesisEvidenceAdjudication,
        projection: StoredThesisMatchProjection,
    ) -> None:
        projected = projection.value
        if (
            projected.projection_state is not ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
            or value.fund_code != projected.fund_code
            or value.thesis_id != projected.thesis_id
            or value.thesis_fingerprint != projected.thesis_fingerprint
            or value.thesis_match_projection_checksum != projected.record_checksum
            or value.intelligence_request_run_id != projected.intelligence_request_run_id
            or value.intelligence_snapshot_checksum != projected.intelligence_snapshot_checksum
            or value.evidence_ids != projected.evidence_ids
        ):
            raise HoldingReviewStoreError("thesis evidence adjudication binding failed")

    @staticmethod
    def _adjudication_identity_row(connection, value):
        return connection.execute(
            """
            SELECT * FROM thesis_evidence_adjudications
            WHERE fund_code=? AND thesis_id=? AND thesis_fingerprint=?
              AND thesis_match_projection_id=? AND thesis_match_projection_checksum=?
              AND intelligence_request_run_id=? AND intelligence_snapshot_checksum=?
              AND evidence_set_checksum=? AND decision=?
              AND COALESCE(superseded_adjudication_id,0)=COALESCE(?,0)
            """,
            (
                value.fund_code,
                value.thesis_id,
                value.thesis_fingerprint,
                value.thesis_match_projection_id,
                value.thesis_match_projection_checksum,
                value.intelligence_request_run_id,
                value.intelligence_snapshot_checksum,
                value.evidence_set_checksum,
                value.decision.value,
                value.superseded_adjudication_id,
            ),
        ).fetchone()

    @staticmethod
    def _current_adjudication_row(connection, projection_id):
        rows = connection.execute(
            """
            SELECT current.* FROM thesis_evidence_adjudications AS current
            WHERE current.thesis_match_projection_id=?
              AND NOT EXISTS(
                SELECT 1 FROM thesis_evidence_adjudications AS child
                WHERE child.superseded_adjudication_id=current.id
              )
            ORDER BY current.created_at DESC, current.id DESC LIMIT 2
            """,
            (projection_id,),
        ).fetchall()
        if len(rows) > 1:
            raise HoldingReviewStoreError("thesis evidence adjudication supersession failed")
        return None if not rows else rows[0]

    @staticmethod
    def _stored_projection(row: Mapping[str, object]) -> StoredThesisMatchProjection:
        descriptors = tuple(
            _review_evidence_item(item)
            for item in _json_value(row["evidence_descriptors_json"], list)
        )
        value = ThesisMatchProjection(
            fund_code=str(row["fund_code"]),
            thesis_id=_optional_id(row["thesis_id"]),
            thesis_fingerprint=_optional_text(row["thesis_fingerprint"]),
            intelligence_request_run_id=_positive_id(
                row["intelligence_request_run_id"], "intelligence request run id"
            ),
            intelligence_snapshot_id=_positive_id(
                row["intelligence_snapshot_id"], "intelligence snapshot id"
            ),
            intelligence_snapshot_checksum=_checksum(
                row["intelligence_snapshot_checksum"], "intelligence snapshot checksum"
            ),
            matcher_policy_version=str(row["matcher_policy_version"]),
            matcher_policy_checksum=_checksum(
                row["matcher_policy_checksum"], "matcher policy checksum"
            ),
            projection_state=ThesisMatchProjectionState(str(row["projection_state"])),
            evidence_descriptors=descriptors,
            evidence_set_checksum=_checksum(
                row["evidence_set_checksum"], "evidence set checksum"
            ),
            created_at=_stored_utc(row["created_at"]),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        if tuple(_json_value(row["evidence_ids_json"], list)) != value.evidence_ids:
            raise HoldingReviewStoreError("thesis match projection authentication failed")
        return StoredThesisMatchProjection(_positive_id(row["id"], "projection id"), value)

    @staticmethod
    def _stored_adjudication(row: Mapping[str, object]) -> StoredThesisEvidenceAdjudication:
        value = ThesisEvidenceAdjudication(
            fund_code=str(row["fund_code"]),
            thesis_id=_positive_id(row["thesis_id"], "thesis id"),
            thesis_fingerprint=_checksum(row["thesis_fingerprint"], "thesis fingerprint"),
            thesis_match_projection_id=_positive_id(
                row["thesis_match_projection_id"], "projection id"
            ),
            thesis_match_projection_checksum=_checksum(
                row["thesis_match_projection_checksum"], "projection checksum"
            ),
            intelligence_request_run_id=_positive_id(
                row["intelligence_request_run_id"], "intelligence request run id"
            ),
            intelligence_snapshot_checksum=_checksum(
                row["intelligence_snapshot_checksum"], "intelligence snapshot checksum"
            ),
            evidence_ids=tuple(_json_value(row["evidence_ids_json"], list)),
            evidence_set_checksum=_checksum(row["evidence_set_checksum"], "evidence checksum"),
            decision=AdjudicationDecision(str(row["decision"])),
            superseded_adjudication_id=_optional_id(row["superseded_adjudication_id"]),
            created_at=_stored_utc(row["created_at"]),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        return StoredThesisEvidenceAdjudication(
            _positive_id(row["id"], "adjudication id"), value
        )

    @staticmethod
    def _stored_review(row: Mapping[str, object]) -> StoredHoldingReviewSnapshot:
        result_bytes = _ascii_bytes(row["result_json"], "review result", MAX_REVIEW_JSON_BYTES)
        data = _json_bytes(result_bytes, dict)
        result = _review_result(data)
        if result.canonical_json() != result_bytes:
            raise HoldingReviewStoreError("holding review result authentication failed")
        value = HoldingReviewSnapshot(
            fund_code=str(row["fund_code"]),
            action=ActionKind(str(row["action"])),
            brief_request_run_id=_positive_id(row["brief_request_run_id"], "brief request id"),
            brief_snapshot_id=_positive_id(row["brief_snapshot_id"], "brief snapshot id"),
            brief_snapshot_checksum=_checksum(row["brief_snapshot_checksum"], "brief checksum"),
            intelligence_request_run_id=_positive_id(
                row["intelligence_request_run_id"], "intelligence request id"
            ),
            intelligence_snapshot_id=_positive_id(
                row["intelligence_snapshot_id"], "intelligence snapshot id"
            ),
            intelligence_snapshot_checksum=_checksum(
                row["intelligence_snapshot_checksum"], "intelligence checksum"
            ),
            thesis_match_projection_id=_positive_id(
                row["thesis_match_projection_id"], "projection id"
            ),
            thesis_match_projection_checksum=_checksum(
                row["thesis_match_projection_checksum"], "projection checksum"
            ),
            active_thesis_state=BindingState(str(row["active_thesis_state"])),
            active_thesis_id=_optional_id(row["active_thesis_id"]),
            active_thesis_fingerprint=_optional_text(row["active_thesis_fingerprint"]),
            adjudication_state=BindingState(str(row["adjudication_state"])),
            adjudication_id=_optional_id(row["adjudication_id"]),
            adjudication_checksum=_optional_text(row["adjudication_checksum"]),
            previous_review_id=_optional_id(row["previous_review_id"]),
            result=result,
            result_fingerprint=_checksum(row["result_fingerprint"], "result fingerprint"),
            policy_version=str(row["policy_version"]),
            policy_checksum=_checksum(row["policy_checksum"], "policy checksum"),
            created_at=_stored_utc(row["created_at"]),
            semantic_identity_checksum=_checksum(
                row["semantic_identity_checksum"], "semantic identity checksum"
            ),
            record_checksum=_checksum(row["record_checksum"], "record checksum"),
        )
        value.validate()
        return StoredHoldingReviewSnapshot(_positive_id(row["id"], "review id"), value)


def _review_result(value: dict) -> HoldingReviewResult:
    redemption = value["redemption_evidence"]
    delta = value["evidence_delta"]
    boundary = value["boundary"]
    return HoldingReviewResult(
        fund_code=value["fund_code"],
        action=ActionKind(value["action"]),
        flow_status=FlowStatus(value["flow_status"]),
        evidence_readiness=EvidenceReadiness(value["evidence_readiness"]),
        history_comparability=HistoryComparability(value["history_comparability"]),
        thesis_review_state=ThesisMatchState(value["thesis_review_state"]),
        review_disposition=ReviewDisposition(value["review_disposition"]),
        triggered_reviews=tuple(TriggeredReviewCode(item) for item in value["triggered_reviews"]),
        official_event_evidence=tuple(
            OfficialEventEvidenceReference(
                projection_id=item["projection_id"],
                projection_checksum=item["projection_checksum"],
                event_code=OfficialEventCode(item["event_code"]),
                triggered_review_code=TriggeredReviewCode(item["triggered_review_code"]),
            )
            for item in value["official_event_evidence"]
        ),
        redemption_feasibility=RedemptionFeasibility(value["redemption_feasibility"]),
        redemption_evidence=RedemptionEvidence(
            **{key: RedemptionComponentState(item) for key, item in redemption.items()}
        ),
        sell_timing=value["sell_timing"],
        upstream_action_boundary=tuple(value["upstream_action_boundary"]),
        boundary=ReviewBoundary(**boundary),
        omitted_work=tuple(value["omitted_work"]),
        official_negative_check_complete=value["official_negative_check_complete"],
        intelligence_schedule_complete=value["intelligence_schedule_complete"],
        intelligence_omitted_work=tuple(value["intelligence_omitted_work"]),
        intelligence_degraded_sources=tuple(value["intelligence_degraded_sources"]),
        action_review_source_sufficiency=ActionReviewSourceSufficiency(
            value["action_review_source_sufficiency"]
        ),
        hard_event_review=value["hard_event_review"],
        evidence_ids=tuple(value["evidence_ids"]),
        evidence_delta=EvidenceDelta(
            history_comparability=HistoryComparability(delta["history_comparability"]),
            evidence_unchanged=delta["evidence_unchanged"],
            added_evidence_ids=tuple(delta["added_evidence_ids"]),
            removed_evidence_ids=tuple(delta["removed_evidence_ids"]),
            corrected_evidence_ids=tuple(delta["corrected_evidence_ids"]),
            retracted_evidence_ids=tuple(delta["retracted_evidence_ids"]),
            expired_evidence_ids=tuple(delta["expired_evidence_ids"]),
            conflicted_evidence_ids=tuple(delta["conflicted_evidence_ids"]),
            reason_codes=tuple(delta["reason_codes"]),
        ),
        remainder_intent=RemainderIntent(value["remainder_intent"]),
        exit_reason=ExitReason(value["exit_reason"]),
        use_of_proceeds=UseOfProceeds(value["use_of_proceeds"]),
        policy_version=value["policy_version"],
        policy_checksum=value["policy_checksum"],
        created_at=_stored_utc(value["created_at"]),
    )


def _review_evidence_item(value: dict) -> ReviewEvidenceItem:
    return ReviewEvidenceItem(
        evidence_id=value["evidence_id"],
        source_tier=value["source_tier"],
        lineage_kind=LineageKind(value["lineage_kind"]),
        current=value["current"],
        graph_closed=value["graph_closed"],
        original_lineage=value["original_lineage"],
        retracted=value["retracted"],
        conflicted=value["conflicted"],
        direct_subject_binding=value["direct_subject_binding"],
    )


def _authenticated_thesis(
    connection: sqlite3.Connection,
    thesis_id: int,
    *,
    require_active: bool,
) -> InvestmentThesis:
    if type(require_active) is not bool:
        raise ValueError("active thesis requirement must be an exact boolean")
    row = connection.execute(
        "SELECT * FROM investment_theses WHERE id=?",
        (thesis_id,),
    ).fetchone()
    if row is None:
        raise HoldingReviewStoreError("thesis identity authentication failed")
    value = InvestmentThesis(
        fund_code=str(row["fund_code"]),
        rationale=str(row["rationale"]),
        horizon=str(row["horizon"]),
        invalidation=str(row["invalidation"]),
        created_at=_stored_utc(row["created_at"]),
        active=bool(row["active"]),
    )
    value.validate()
    if require_active and not value.active:
        raise HoldingReviewStoreError("active thesis authentication failed")
    if require_active:
        latest = connection.execute(
            """
            SELECT id FROM investment_theses
            WHERE fund_code=? AND active=1
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (value.fund_code,),
        ).fetchone()
        if latest is None or int(latest["id"]) != thesis_id:
            raise HoldingReviewStoreError("latest active thesis authentication failed")
    return value


def _authenticate_no_active_thesis(
    connection: sqlite3.Connection,
    fund_code: str,
) -> None:
    row = connection.execute(
        "SELECT count(*) AS count FROM investment_theses WHERE fund_code=? AND active=1",
        (fund_code,),
    ).fetchone()
    if row is None or type(row["count"]) is not int or row["count"] != 0:
        raise HoldingReviewStoreError("active thesis absence authentication failed")


def _thesis_matcher_policy_v1_checksum() -> str:
    return hashlib.sha256(canonical_json_bytes({"version": "1"})).hexdigest()


def _v1_thesis_match_item_ids(
    thesis: InvestmentThesis,
    items: tuple[NewsItem, ...],
) -> tuple[str, ...]:
    needle = _normalized_match_text(thesis.invalidation)
    return tuple(
        sorted(
            item.item_id
            for item in items
            if needle
            in _normalized_match_text(f"{item.title} {item.excerpt or ''}")
        )
    )


def _normalized_match_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _derive_lineage_states(
    item_ids: tuple[str, ...],
    edges: tuple[LineageEdge, ...],
    *,
    graph_complete: bool,
) -> dict[str, tuple[LineageKind, bool]]:
    if type(item_ids) is not tuple or type(edges) is not tuple:
        raise ValueError("lineage derivation inputs must be exact tuples")
    if type(graph_complete) is not bool:
        raise ValueError("lineage graph completeness must be an exact boolean")
    item_set = set(item_ids)
    if len(item_set) != len(item_ids):
        raise ValueError("lineage derivation item ids must be unique")
    outgoing: dict[str, list[LineageKind]] = {item_id: [] for item_id in item_ids}
    targets_by_source: dict[str, list[str]] = {item_id: [] for item_id in item_ids}
    for edge in edges:
        if type(edge) is not LineageEdge:
            raise ValueError("lineage derivation edges must be exact LineageEdge records")
        edge.validate()
        if edge.from_item_id not in item_set or edge.to_item_id not in item_set:
            raise ValueError("lineage derivation edge must resolve inside the item set")
        outgoing[edge.from_item_id].append(edge.kind)
        targets_by_source[edge.from_item_id].append(edge.to_item_id)
    proven_originals = {
        targets[0]
        for source_id, targets in targets_by_source.items()
        if len(outgoing[source_id]) == 1
        and outgoing[source_id][0] in _ORIGIN_PROVING_LINEAGE_KINDS
        and len(targets) == 1
    }
    result = {}
    for item_id in item_ids:
        kinds = outgoing[item_id]
        if not graph_complete:
            result[item_id] = (LineageKind.UNKNOWN, False)
        elif len(kinds) == 1 and kinds[0] in _ORIGIN_PROVING_LINEAGE_KINDS:
            result[item_id] = (kinds[0], True)
        elif not kinds and item_id in proven_originals:
            result[item_id] = (LineageKind.ORIGINAL, True)
        else:
            result[item_id] = (LineageKind.UNKNOWN, False)
    return result


def _without_times_and_checksum(value):
    return replace(
        value,
        created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        record_checksum="0" * 64,
    )


def _reviews_comparable(
    previous: HoldingReviewSnapshot,
    current: HoldingReviewSnapshot,
) -> bool:
    return (
        previous.fund_code == current.fund_code
        and previous.action is current.action
        and previous.active_thesis_state is current.active_thesis_state
        and previous.active_thesis_id == current.active_thesis_id
        and previous.active_thesis_fingerprint == current.active_thesis_fingerprint
        and previous.policy_version == current.policy_version
        and previous.policy_checksum == current.policy_checksum
    )


def _official_closure_sql_values(value: OfficialCheckClosure) -> tuple[object, ...]:
    return (
        value.brief_request_run_id,
        value.fund_code,
        value.listing_source_attempt_id,
        value.official_registry_version,
        value.official_registry_checksum,
        _json_text(list(value.source_registration_ids)),
        value.manager_identity_state.value,
        value.manager_identity_row_id,
        value.manager_identity_source_document_id,
        value.manager_identity_source_document_checksum,
        value.manager_identity_normalized_name,
        value.manager_identity_fingerprint,
        _json_text([page.to_canonical_dict() for page in value.listing_page_evidence]),
        _utc_text(value.window_start),
        _utc_text(value.window_end),
        value.listing_count,
        value.candidate_count,
        value.authenticated_body_count,
        value.projected_event_count,
        int(value.listing_truncated),
        int(value.candidate_cap_reached),
        int(value.body_cap_reached),
        _json_text(list(value.gap_codes)),
        int(value.official_negative_check_complete),
        value.policy_version,
        value.policy_checksum,
        value.official_check_policy_version,
        value.official_check_policy_checksum,
        _utc_text(value.created_at),
        value.record_checksum,
    )


def _normalized_identity(value: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("manager identity must be non-empty text")
    return "".join(unicodedata.normalize("NFKC", value).split())


def _manager_identity_fingerprint(
    *,
    fund_code: str,
    identity_row_id: int,
    fund_name: str,
    manager_name: str,
    source_document_id: int,
    source_document_checksum: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "fund_code": _fund_code(fund_code),
                "fund_name": _normalized_identity(fund_name),
                "identity_row_id": _positive_id(identity_row_id, "manager identity row id"),
                "manager_name": _normalized_identity(manager_name),
                "source_document_checksum": _checksum(
                    source_document_checksum,
                    "manager identity source document checksum",
                ),
                "source_document_id": _positive_id(
                    source_document_id,
                    "manager identity source document id",
                ),
            }
        )
    ).hexdigest()


def _announcement_source_document_id(
    connection: sqlite3.Connection,
    announcement_row_id: int,
) -> int:
    row = connection.execute(
        "SELECT source_document_id FROM fund_announcements WHERE id=?",
        (announcement_row_id,),
    ).fetchone()
    if row is None:
        raise HoldingReviewStoreError("official announcement row binding failed")
    return _positive_id(row["source_document_id"], "announcement source document id")


def _fund_code(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 6
        or not value.isascii()
        or not value.isdigit()
        or value == "000000"
    ):
        raise ValueError("fund code must be a non-reserved six-digit ASCII code")
    return value


def _positive_id(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive exact integer")
    return value


def _optional_id(value: object) -> Optional[int]:
    if value is None:
        return None
    return _positive_id(value, "optional id")


def _checksum(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(item not in "0123456789abcdef" for item in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 checksum")
    return value


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError("optional text must be exact")
    return value


def _utc_text(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError("datetime must be UTC")
    return value.isoformat()


def _stored_utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("stored datetime must be text")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("stored datetime must be UTC")
    return result


def _ascii_bytes(value: object, name: str, maximum: int) -> bytes:
    if type(value) is not str:
        raise ValueError(f"{name} must be text")
    encoded = value.encode("ascii")
    if len(encoded) > maximum:
        raise ValueError(f"{name} exceeds byte limit")
    return encoded


def _json_bytes(value: bytes, expected: type):
    decoded = json.loads(value)
    if type(decoded) is not expected or canonical_json_bytes(decoded) != value:
        raise ValueError("stored JSON is not canonical")
    return decoded


def _json_value(value: object, expected: type):
    return _json_bytes(_ascii_bytes(value, "stored JSON", MAX_REVIEW_JSON_BYTES), expected)


def _json_text(value: object) -> str:
    return canonical_json_bytes(value).decode("ascii")
