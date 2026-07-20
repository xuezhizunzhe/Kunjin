from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from kunjin.brief.models import thesis_record_fingerprint
from kunjin.decision.models import canonical_json_bytes
from kunjin.holding_review.models import (
    AdjudicationDecision,
    ReviewEvidenceItem,
    ThesisEvidenceAdjudication,
    ThesisMatchProjection,
    ThesisMatchProjectionState,
)
from kunjin.holding_review.store import (
    HoldingReviewStore,
    HoldingReviewStoreError,
    StoredThesisEvidenceAdjudication,
    StoredThesisMatchProjection,
    _thesis_matcher_policy_v1_checksum,
    _v1_thesis_match_item_ids,
)
from kunjin.intelligence.models import IntelligenceWorkflow
from kunjin.storage.repository import Repository

_FUND_CODE = re.compile(r"[0-9]{6}")


class ThesisReviewError(RuntimeError):
    """A sanitized thesis projection or adjudication failure."""


@dataclass(frozen=True)
class ThesisMatcherPolicyV1:
    version: str = "1"

    def checksum(self) -> str:
        if type(self.version) is not str or self.version != "1":
            raise ValueError("unsupported thesis matcher policy version")
        return _thesis_matcher_policy_v1_checksum()


class ThesisReviewService:
    def __init__(
        self,
        repository: Repository,
        *,
        holding_review_store: Optional[HoldingReviewStore] = None,
        clock: Optional[Callable[[], datetime]] = None,
        policy: Optional[ThesisMatcherPolicyV1] = None,
    ) -> None:
        if not isinstance(repository, Repository):
            raise ValueError("repository must be a Repository")
        if holding_review_store is not None and not isinstance(
            holding_review_store, HoldingReviewStore
        ):
            raise ValueError("holding review store must be a HoldingReviewStore")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        if policy is not None and type(policy) is not ThesisMatcherPolicyV1:
            raise ValueError("matcher policy must be exact")
        self.repository = repository
        self.holding_review_store = holding_review_store or HoldingReviewStore(repository)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.policy = policy or ThesisMatcherPolicyV1()
        self.policy.checksum()

    def match_project(
        self,
        fund_code: str,
        intelligence_request_run_id: int,
    ) -> StoredThesisMatchProjection:
        _validate_fund_code(fund_code)
        _positive_int(intelligence_request_run_id, "intelligence request run id")
        try:
            inputs = self.holding_review_store.authenticated_thesis_projection_inputs(
                intelligence_request_run_id
            )
            snapshot = inputs.intelligence.snapshot
            if (
                snapshot.workflow is not IntelligenceWorkflow.FUND_INTELLIGENCE
                or snapshot.subject_fund_code != fund_code
            ):
                raise ThesisReviewError("thesis projection snapshot binding failed")

            active = self.repository.latest_active_thesis(fund_code)
            if active is None:
                thesis_id = None
                thesis = None
                thesis_fingerprint = None
                matched_descriptors: tuple[ReviewEvidenceItem, ...] = ()
                state = ThesisMatchProjectionState.THESIS_MISSING
            else:
                thesis_id, thesis = active
                thesis_fingerprint = thesis_record_fingerprint(thesis_id, thesis)
                ordered_ids = _v1_thesis_match_item_ids(thesis, inputs.items)
                descriptor_by_id = {
                    descriptor.evidence_id: descriptor
                    for descriptor in inputs.evidence_descriptors
                }
                if any(item_id not in descriptor_by_id for item_id in ordered_ids):
                    raise ThesisReviewError("thesis projection evidence graph binding failed")
                matched_descriptors = tuple(
                    descriptor_by_id[item_id] for item_id in ordered_ids
                )
                state = (
                    ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
                    if matched_descriptors
                    else ThesisMatchProjectionState.NO_MATCHING_EVIDENCE
                )

            created_at = self.clock()
            lower_bound = inputs.intelligence.snapshot.created_at
            if thesis is not None and thesis.created_at > lower_bound:
                lower_bound = thesis.created_at
            if created_at < lower_bound:
                created_at = lower_bound
            value = ThesisMatchProjection(
                fund_code=fund_code,
                thesis_id=thesis_id,
                thesis_fingerprint=thesis_fingerprint,
                intelligence_request_run_id=intelligence_request_run_id,
                intelligence_snapshot_id=inputs.intelligence.id,
                intelligence_snapshot_checksum=inputs.intelligence.result_checksum,
                matcher_policy_version=self.policy.version,
                matcher_policy_checksum=self.policy.checksum(),
                projection_state=state,
                evidence_descriptors=matched_descriptors,
                evidence_set_checksum="0" * 64,
                created_at=created_at,
                record_checksum="0" * 64,
            )
            value = replace(
                value,
                evidence_set_checksum=value.expected_evidence_set_checksum(),
            )
            value = replace(value, record_checksum=value.expected_record_checksum())
            return self.holding_review_store.publish_thesis_match(value)
        except ThesisReviewError:
            raise
        except HoldingReviewStoreError as exc:
            raise ThesisReviewError("thesis projection snapshot authentication failed") from exc
        except Exception:
            raise ThesisReviewError("thesis projection authentication failed") from None

    def adjudicate(
        self,
        fund_code: str,
        projection_id: int,
        decision: AdjudicationDecision,
        supersedes_id: Optional[int] = None,
    ) -> StoredThesisEvidenceAdjudication:
        _validate_fund_code(fund_code)
        _positive_int(projection_id, "projection id")
        if type(decision) is not AdjudicationDecision:
            raise ThesisReviewError("thesis adjudication requires an explicit decision")
        if supersedes_id is not None:
            _positive_int(supersedes_id, "superseded adjudication id")
        try:
            projection = self.holding_review_store.authenticated_thesis_match(projection_id)
            projected = projection.value
            if projected.fund_code != fund_code:
                raise ThesisReviewError("thesis adjudication projection binding failed")
            if (
                projected.projection_state
                is not ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
                or projected.thesis_id is None
                or projected.thesis_fingerprint is None
            ):
                raise ThesisReviewError("thesis adjudication requires a possible match")

            current = self.holding_review_store.current_adjudication(projection_id)
            if current is not None:
                if current.value.decision is decision:
                    if supersedes_id not in (None, current.id):
                        raise ThesisReviewError("thesis adjudication supersession failed")
                elif supersedes_id != current.id:
                    raise ThesisReviewError("thesis adjudication supersession failed")
            elif supersedes_id is not None:
                raise ThesisReviewError("thesis adjudication supersession failed")

            evidence_ids = projected.evidence_ids
            created_at = self.clock()
            minimum_created_at = projected.created_at + timedelta(microseconds=1)
            if current is not None and created_at <= current.value.created_at:
                created_at = current.value.created_at + timedelta(microseconds=1)
            elif created_at < minimum_created_at:
                created_at = minimum_created_at
            value = ThesisEvidenceAdjudication(
                fund_code=fund_code,
                thesis_id=projected.thesis_id,
                thesis_fingerprint=projected.thesis_fingerprint,
                thesis_match_projection_id=projection.id,
                thesis_match_projection_checksum=projected.record_checksum,
                intelligence_request_run_id=projected.intelligence_request_run_id,
                intelligence_snapshot_checksum=projected.intelligence_snapshot_checksum,
                evidence_ids=evidence_ids,
                evidence_set_checksum=hashlib.sha256(
                    canonical_json_bytes(evidence_ids)
                ).hexdigest(),
                decision=decision,
                superseded_adjudication_id=supersedes_id,
                created_at=created_at,
                record_checksum="0" * 64,
            )
            value = replace(value, record_checksum=value.expected_record_checksum())
            return self.holding_review_store.publish_adjudication(value)
        except ThesisReviewError:
            raise
        except HoldingReviewStoreError as exc:
            raise ThesisReviewError("thesis projection authentication failed") from exc
        except Exception:
            raise ThesisReviewError("thesis adjudication authentication failed") from None


def _validate_fund_code(value: str) -> None:
    if type(value) is not str or _FUND_CODE.fullmatch(value) is None:
        raise ValueError("fund code must be exactly six ASCII digits")


def _positive_int(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
