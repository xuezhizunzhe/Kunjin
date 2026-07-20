from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Mapping, Optional

from kunjin.brief.models import OfficialEventCode, thesis_record_fingerprint
from kunjin.brief.store import BriefStore, BriefStoreError, StoredBriefSnapshot
from kunjin.decision.models import ActionKind, canonical_json_bytes
from kunjin.holding_review.models import (
    ActionReviewSourceSufficiency,
    AdjudicationDecision,
    BindingState,
    EvidenceDelta,
    EvidenceReadiness,
    ExitReason,
    FlowStatus,
    HistoryComparability,
    HoldingReviewResult,
    HoldingReviewSnapshot,
    OfficialEventEvidenceReference,
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
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1
from kunjin.intelligence.models import IntelligenceWorkflow, LineageKind
from kunjin.intelligence.store import (
    IntelligenceStore,
    IntelligenceStoreError,
    StoredIntelligenceSnapshot,
)
from kunjin.models import InvestmentThesis
from kunjin.storage.repository import Repository

MAX_REVIEW_JSON_BYTES = 4 * 1024 * 1024


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
class StoredHoldingReviewSnapshot:
    id: int
    value: HoldingReviewSnapshot


class HoldingReviewStore:
    def __init__(self, repository: Repository) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        self.repository = repository
        self.brief_store = BriefStore(repository)
        self.intelligence_store = IntelligenceStore(repository)
        self.policy = HeldFundManualReviewPolicyV1()
        self.policy.validate()

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
                    existing = self._adjudication_identity_row(connection, value)
                    if existing is not None:
                        stored = self._stored_adjudication(existing)
                        if _without_times_and_checksum(stored.value) != (
                            _without_times_and_checksum(value)
                        ):
                            raise HoldingReviewStoreError(
                                "thesis evidence adjudication authentication failed"
                            )
                        connection.commit()
                        return stored
                    current = self._current_adjudication_row(
                        connection, value.thesis_match_projection_id
                    )
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
                        connection.commit()
                        return stored
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
        thesis_fingerprint: str,
        policy_checksum: str,
    ) -> Optional[StoredHoldingReviewSnapshot]:
        _fund_code(fund_code)
        if type(action) is not ActionKind:
            raise ValueError("action must be an exact ActionKind")
        _checksum(thesis_fingerprint, "thesis fingerprint")
        _checksum(policy_checksum, "policy checksum")
        try:
            with self.repository.connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM holding_review_snapshots
                    WHERE fund_code=? AND action=? AND active_thesis_fingerprint=?
                      AND policy_checksum=?
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (fund_code, action.value, thesis_fingerprint, policy_checksum),
                ).fetchone()
                if row is None:
                    return None
                stored = self._stored_review(row)
                self._authenticate_review_references(connection, stored.value)
                return stored
        except HoldingReviewStoreError:
            raise
        except (BriefStoreError, IntelligenceStoreError, sqlite3.DatabaseError):
            raise HoldingReviewStoreError("holding review snapshot authentication failed") from None
        except (TypeError, ValueError, OverflowError, UnicodeError, KeyError):
            raise HoldingReviewStoreError("holding review snapshot authentication failed") from None

    def _authenticate_projection_references(
        self, connection: sqlite3.Connection, value: ThesisMatchProjection
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
        if value.thesis_id is not None:
            thesis = _active_thesis(connection, value.thesis_id)
            if (
                thesis.fund_code != value.fund_code
                or thesis_record_fingerprint(value.thesis_id, thesis) != value.thesis_fingerprint
            ):
                raise HoldingReviewStoreError("thesis match projection binding failed")

    def _authenticate_review_references(
        self, connection: sqlite3.Connection, value: HoldingReviewSnapshot
    ) -> None:
        if (
            value.result.official_negative_check_complete
            or value.result.official_event_evidence
        ):
            raise HoldingReviewStoreError("holding review preview boundary failed")
        brief = self._brief_by_request(connection, value.brief_request_run_id)
        if (
            brief.id != value.brief_snapshot_id
            or brief.result_checksum != value.brief_snapshot_checksum
            or brief.snapshot.fund_code != value.fund_code
            or value.action.value not in brief.snapshot.action_ids
        ):
            raise HoldingReviewStoreError("holding review snapshot binding failed")
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
        if (
            projection.value.record_checksum != value.thesis_match_projection_checksum
            or projection.value.fund_code != value.fund_code
            or projection.value.intelligence_request_run_id != value.intelligence_request_run_id
            or projection.value.intelligence_snapshot_id != value.intelligence_snapshot_id
        ):
            raise HoldingReviewStoreError("holding review snapshot binding failed")
        if value.active_thesis_id is not None:
            thesis = _active_thesis(connection, value.active_thesis_id)
            if (
                thesis.fund_code != value.fund_code
                or thesis_record_fingerprint(value.active_thesis_id, thesis)
                != value.active_thesis_fingerprint
            ):
                raise HoldingReviewStoreError("holding review snapshot binding failed")
        if value.adjudication_id is not None:
            row = connection.execute(
                "SELECT * FROM thesis_evidence_adjudications WHERE id=?",
                (value.adjudication_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError("holding review snapshot binding failed")
            adjudication = self._stored_adjudication(row)
            if (
                adjudication.value.record_checksum != value.adjudication_checksum
                or adjudication.value.thesis_match_projection_id != projection.id
            ):
                raise HoldingReviewStoreError("holding review snapshot binding failed")
        if value.previous_review_id is not None:
            row = connection.execute(
                "SELECT * FROM holding_review_snapshots WHERE id=?",
                (value.previous_review_id,),
            ).fetchone()
            if row is None:
                raise HoldingReviewStoreError("holding review snapshot binding failed")
            previous = self._stored_review(row)
            if (
                previous.value.fund_code != value.fund_code
                or previous.value.action is not value.action
                or previous.value.created_at > value.created_at
            ):
                raise HoldingReviewStoreError("holding review snapshot binding failed")
        if (
            value.policy_version != self.policy.version
            or value.policy_checksum != self.policy.checksum()
        ):
            raise HoldingReviewStoreError("holding review snapshot policy authentication failed")

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


def _active_thesis(connection: sqlite3.Connection, thesis_id: int) -> InvestmentThesis:
    row = connection.execute(
        "SELECT * FROM investment_theses WHERE id=? AND active=1", (thesis_id,)
    ).fetchone()
    if row is None:
        raise HoldingReviewStoreError("active thesis authentication failed")
    value = InvestmentThesis(
        fund_code=str(row["fund_code"]),
        rationale=str(row["rationale"]),
        horizon=str(row["horizon"]),
        invalidation=str(row["invalidation"]),
        created_at=_stored_utc(row["created_at"]),
        active=bool(row["active"]),
    )
    value.validate()
    return value


def _without_times_and_checksum(value):
    return replace(
        value,
        created_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
        record_checksum="0" * 64,
    )


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
