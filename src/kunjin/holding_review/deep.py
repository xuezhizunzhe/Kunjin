from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from kunjin.decision.budget import BudgetExpired
from kunjin.decision.models import (
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
    validate_aware_datetime,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.models import DocumentKind, FundAnnouncement, SourceDocument
from kunjin.funds.official_domains import OFFICIAL_SOURCE_REGISTRATIONS
from kunjin.funds.service import SourceRequestContext
from kunjin.funds.store import (
    FundDisclosureStore,
    OfficialListingRequestContext,
    OfficialListingRows,
)
from kunjin.holding_review.models import (
    OfficialCheckClosure,
    OfficialListingPageEvidence,
    OfficialManagerIdentityState,
)
from kunjin.holding_review.official import (
    FetchOfficialAnnouncement,
    FetchOfficialListing,
    OfficialAnnouncementCollector,
    OfficialAnnouncementRow,
    OfficialCollectionContext,
    OfficialListingAcquirer,
    OfficialListingResult,
    materialize_official_event_projection,
    materialize_official_listing_page_evidence,
    persistable_official_listing_items,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1, OfficialCheckPolicyV1
from kunjin.holding_review.store import (
    AuthenticatedOfficialManagerIdentity,
    HoldingReviewStore,
    StoredOfficialCheckClosure,
)

ListingFetchFactory = Callable[[datetime], FetchOfficialListing]
AnnouncementFetchFactory = Callable[
    [Tuple[OfficialAnnouncementRow, ...], datetime], FetchOfficialAnnouncement
]


class DeepOfficialConfirmationError(RuntimeError):
    pass


class DeepOfficialConfirmationService:
    def __init__(
        self,
        *,
        disclosure_store: FundDisclosureStore,
        audit_store: DecisionAuditStore,
        review_store: HoldingReviewStore,
        listing_fetch_factory: ListingFetchFactory,
        announcement_fetch_factory: AnnouncementFetchFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(disclosure_store) is not FundDisclosureStore:
            raise ValueError("Deep official confirmation requires exact disclosure store")
        if type(audit_store) is not DecisionAuditStore:
            raise ValueError("Deep official confirmation requires exact audit store")
        if type(review_store) is not HoldingReviewStore:
            raise ValueError("Deep official confirmation requires exact review store")
        repositories = {
            id(disclosure_store.repository),
            id(audit_store.repository),
            id(review_store.repository),
        }
        if len(repositories) != 1:
            raise ValueError("Deep official confirmation stores must share one repository")
        if not callable(listing_fetch_factory) or not callable(announcement_fetch_factory):
            raise ValueError("Deep official confirmation fetch factories must be callable")
        if not callable(clock):
            raise ValueError("Deep official confirmation clock must be callable")
        self.disclosure_store = disclosure_store
        self.audit_store = audit_store
        self.review_store = review_store
        self.listing_fetch_factory = listing_fetch_factory
        self.announcement_fetch_factory = announcement_fetch_factory
        self.clock = clock
        self.manual_policy = HeldFundManualReviewPolicyV1()
        self.official_policy = OfficialCheckPolicyV1()
        self.source_registry = SourceRegistryV1()
        self.manual_policy.validate()
        self.official_policy.validate()
        self.source_registry.validate()

    def confirm(
        self,
        fund_code: str,
        request_context: SourceRequestContext,
    ) -> StoredOfficialCheckClosure:
        self._validate_request_context(request_context)
        request_context.budget.require_publishable()
        as_of = self._now()
        existing_attempts = self.audit_store.authenticated_request_source_attempts(
            request_context.request_run_id,
            request_context.budget,
            as_of,
        )
        official_attempt_identity = (
            "fund_manager_official_documents",
            "fund_manager_product_announcement",
            f"fund:{fund_code}",
        )
        if any(
            (
                item.attempt.source_id,
                item.attempt.field_id,
                item.attempt.subject_key,
            )
            == official_attempt_identity
            for item in existing_attempts
        ):
            raise ValueError("official confirmation request already has a source attempt")
        window_start = as_of - timedelta(days=self.official_policy.query_window_days)
        identity = self.review_store.authenticated_official_manager_identity(
            fund_code,
            as_of,
        )
        identity.validate()
        registrations = tuple(
            source
            for source in OFFICIAL_SOURCE_REGISTRATIONS
            if identity.normalized_name is not None
            and source.matches_identity(identity.normalized_name)
        )
        registration_ids = tuple(source.registration_id for source in registrations)
        if identity.state is not OfficialManagerIdentityState.PRESENT:
            identity_error_codes = {
                OfficialManagerIdentityState.MISSING: SourceErrorCode.SOURCE_UNAVAILABLE,
                OfficialManagerIdentityState.STALE: SourceErrorCode.VALIDATION_FAILURE,
                OfficialManagerIdentityState.CONFLICTED: SourceErrorCode.IDENTITY_CONFLICT,
            }
            return self._terminal_without_listing(
                fund_code=fund_code,
                request_context=request_context,
                identity=identity,
                registration_ids=(),
                window_start=window_start,
                window_end=as_of,
                gap_code=f"official_manager_identity_{identity.state.value}",
                outcome=SourceAttemptOutcome.UNAVAILABLE,
                error_code=identity_error_codes[identity.state],
            )
        if not registrations:
            return self._terminal_without_listing(
                fund_code=fund_code,
                request_context=request_context,
                identity=identity,
                registration_ids=(),
                window_start=window_start,
                window_end=as_of,
                gap_code="official_source_set_unsupported",
                outcome=SourceAttemptOutcome.UNSUPPORTED,
                error_code=SourceErrorCode.SOURCE_CONTRACT_UNSUPPORTED,
            )

        started_at = self._now()
        try:
            listing_fetch = self.listing_fetch_factory(request_context.budget.deadline_at)
            listing = OfficialListingAcquirer(
                fetch=listing_fetch,
                registrations=registrations,
                policy=self.official_policy,
            ).collect_registered_listing(
                fund_code,
                identity.normalized_name,
                identity.fund_name,
                window_start=window_start,
                window_end=as_of,
            )
        except BudgetExpired:
            raise
        except TimeoutError:
            return self._terminal_listing_failure(
                fund_code,
                request_context,
                identity,
                registration_ids,
                window_start,
                as_of,
                started_at,
                "official_listing_timeout",
                SourceAttemptOutcome.TRANSIENT_FAILURE,
                SourceErrorCode.NETWORK_TIMEOUT,
            )
        except Exception:
            return self._terminal_listing_failure(
                fund_code,
                request_context,
                identity,
                registration_ids,
                window_start,
                as_of,
                started_at,
                "official_listing_source_failed",
                SourceAttemptOutcome.UNAVAILABLE,
                SourceErrorCode.SOURCE_UNAVAILABLE,
            )
        if not listing.page_captures:
            timeout = "official_listing_timeout" in listing.gap_codes
            return self._terminal_listing_failure(
                fund_code,
                request_context,
                identity,
                registration_ids,
                window_start,
                as_of,
                started_at,
                "official_listing_timeout" if timeout else "official_listing_source_failed",
                (
                    SourceAttemptOutcome.TRANSIENT_FAILURE
                    if timeout
                    else SourceAttemptOutcome.UNAVAILABLE
                ),
                (
                    SourceErrorCode.NETWORK_TIMEOUT
                    if timeout
                    else SourceErrorCode.SOURCE_UNAVAILABLE
                ),
            )

        finished_at = self._now()
        try:
            stored_listing = self._publish_listing(
                fund_code,
                request_context,
                identity,
                listing,
                started_at,
                finished_at,
            )
        except BudgetExpired:
            raise
        except Exception:
            return self._terminal_listing_failure(
                fund_code,
                request_context,
                identity,
                registration_ids,
                window_start,
                as_of,
                started_at,
                "official_listing_persistence_failed",
                SourceAttemptOutcome.UNAVAILABLE,
                SourceErrorCode.VALIDATION_FAILURE,
            )
        return self._publish_bodies_and_closure(
            fund_code=fund_code,
            request_context=request_context,
            identity=identity,
            listing=listing,
            stored_listing=stored_listing,
            window_start=window_start,
            window_end=as_of,
            finished_at=finished_at,
        )

    def _publish_listing(
        self,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        listing: OfficialListingResult,
        started_at: datetime,
        finished_at: datetime,
    ) -> OfficialListingRows:
        registrations = {
            source.registration_id: source for source in OFFICIAL_SOURCE_REGISTRATIONS
        }
        pages = []
        announcements_by_page = []
        for capture in listing.page_captures:
            registration = registrations[capture.registration_id]
            if any(
                item.published_at is not None
                and item.published_at > capture.retrieved_at
                for item in capture.parsed_items
            ):
                raise ValueError("official listing contains a future publication time")
            pages.append(
                SourceDocument(
                    id=None,
                    fund_code=fund_code,
                    document_kind=DocumentKind.ANNOUNCEMENT,
                    title=f"官方公告列表第{capture.page_number}页",
                    url=capture.canonical_page_url,
                    source_name="fund_manager_official_documents",
                    source_tier=1,
                    publisher=registration.identity,
                    published_at=None,
                    retrieved_at=capture.retrieved_at,
                    checksum=capture.raw_sha256,
                )
            )
            announcements_by_page.append(
                tuple(
                    FundAnnouncement(
                        fund_code=fund_code,
                        title=item.title,
                        category="官方公告",
                        publisher=item.publisher,
                        published_at=item.published_at,
                        url=item.canonical_url,
                        source_tier=1,
                        source_document_id=None,
                    )
                    for item in persistable_official_listing_items(capture)
                    if item.published_at is not None
                    and item.published_at <= capture.retrieved_at
                )
            )
        attempt = self._attempt(
            fund_code=fund_code,
            started_at=started_at,
            finished_at=finished_at,
            outcome=SourceAttemptOutcome.SUCCESS,
            error_code=None,
            response_bytes=sum(item.raw_byte_count for item in listing.page_captures),
            data_as_of=finished_at,
        )
        terminal_registrations = {
            item.registration_id
            for item in listing.page_captures
            if item.terminal_state is not None
        }
        storage_context = OfficialListingRequestContext(
            request_run_id=request_context.request_run_id,
            request_id=request_context.budget.request_id,
            fund_code=fund_code,
            source_set_complete=bool(listing.matched_registration_ids),
            window_complete=listing.listing_closure_complete,
            terminal_query_complete=(
                terminal_registrations == set(listing.matched_registration_ids)
            ),
            gap_codes=listing.gap_codes,
            deadline_at=request_context.budget.deadline_at,
        )
        request_context.budget.require_publishable()
        return self.disclosure_store.publish_official_announcement_listing(
            fund_code,
            tuple(pages),
            tuple(announcements_by_page),
            attempt,
            storage_context,
        )

    def _publish_bodies_and_closure(
        self,
        *,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        listing: OfficialListingResult,
        stored_listing: OfficialListingRows,
        window_start: datetime,
        window_end: datetime,
        finished_at: datetime,
    ) -> StoredOfficialCheckClosure:
        try:
            closure = self._build_bodies_and_closure(
                fund_code=fund_code,
                request_context=request_context,
                identity=identity,
                listing=listing,
                stored_listing=stored_listing,
                window_start=window_start,
                window_end=window_end,
                finished_at=finished_at,
            )
        except BudgetExpired:
            raise
        except Exception:
            request_context.budget.require_publishable()
            closure = self._post_listing_failure_closure(
                fund_code=fund_code,
                request_context=request_context,
                identity=identity,
                listing=listing,
                stored_listing=stored_listing,
                window_start=window_start,
                window_end=window_end,
            )
        request_context.budget.require_publishable()
        return self.review_store.publish_official_check_closure(closure)

    def _build_bodies_and_closure(
        self,
        *,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        listing: OfficialListingResult,
        stored_listing: OfficialListingRows,
        window_start: datetime,
        window_end: datetime,
        finished_at: datetime,
    ) -> OfficialCheckClosure:
        if stored_listing.source_attempt_id is None:
            raise DeepOfficialConfirmationError("stored official listing attempt is missing")
        page_evidence = self._materialize_page_evidence(listing, stored_listing)
        stored_by_url = {item.value.url: item for item in stored_listing.rows}
        candidate_items = listing.candidate_items[: self.official_policy.maximum_candidates]
        candidate_rows = tuple(
            self._announcement_row(
                stored_by_url[item.canonical_url],
                identity.fund_name,
                finished_at,
            )
            for item in candidate_items
        )
        if candidate_rows:
            try:
                body_fetch = self.announcement_fetch_factory(
                    candidate_rows,
                    request_context.budget.deadline_at,
                )
            except BudgetExpired:
                raise
            except Exception as exc:
                def body_fetch(
                    url: str,
                    maximum_bytes: int,
                    error: Exception = exc,
                ):
                    raise error
        else:
            def body_fetch(url: str, maximum_bytes: int):
                raise AssertionError("ordinary official listings must not fetch bodies")
        collection = OfficialAnnouncementCollector(
            body_fetch,
            self.manual_policy,
            self.official_policy,
        ).collect(
            candidate_rows,
            OfficialCollectionContext(
                brief_request_run_id=request_context.request_run_id,
                source_attempt_id=stored_listing.source_attempt_id,
                fund_code=fund_code,
                product_name=identity.fund_name,
                source_set_complete=bool(listing.matched_registration_ids),
                window_complete=listing.listing_closure_complete,
                terminal_query_complete=(
                    {
                        item.registration_id
                        for item in listing.page_captures
                        if item.terminal_state is not None
                    }
                    == set(listing.matched_registration_ids)
                ),
                upstream_gap_codes=listing.gap_codes,
                deadline_at=request_context.budget.deadline_at,
            ),
        )
        gaps = set(collection.gap_codes)
        published_content = {}
        for content in collection.contents:
            request_context.budget.require_publishable()
            try:
                content_id = self.review_store.publish_announcement_content(content)
            except BudgetExpired:
                raise
            except Exception:
                gaps.add("official_announcement_persistence_failed")
                continue
            published_content[content.canonical_announcement_url] = (content_id, content)
        projected_event_count = 0
        rows_by_id = {item.announcement_row_id: item for item in candidate_rows}
        for event in collection.event_candidates:
            row = rows_by_id[event.announcement_row_id]
            stored_content = published_content.get(row.canonical_announcement_url)
            if stored_content is None:
                gaps.add("official_event_persistence_failed")
                continue
            try:
                projection = materialize_official_event_projection(
                    event,
                    content=stored_content[1],
                    announcement_content_id=stored_content[0],
                    policy=self.manual_policy,
                )
                request_context.budget.require_publishable()
                self.review_store.publish_official_event(projection)
            except BudgetExpired:
                raise
            except Exception:
                gaps.add("official_event_persistence_failed")
                continue
            projected_event_count += 1
        candidate_cap = len(listing.candidate_items) > self.official_policy.maximum_candidates
        if candidate_cap:
            gaps.add("official_announcement_candidate_cap_reached")
        body_cap = candidate_cap or bool(
            {"announcement_body_limit", "official_announcement_total_limit"}.intersection(
                gaps
            )
        )
        complete = (
            listing.listing_closure_complete
            and collection.official_negative_check_complete
            and not gaps
            and not candidate_cap
            and len(published_content) == len(candidate_rows)
            and projected_event_count == len(candidate_rows)
        )
        closure = self._closure(
            request_run_id=request_context.request_run_id,
            fund_code=fund_code,
            attempt_id=stored_listing.source_attempt_id,
            identity=identity,
            registration_ids=listing.matched_registration_ids,
            page_evidence=page_evidence,
            window_start=window_start,
            window_end=window_end,
            listing_count=listing.listing_count,
            candidate_count=len(candidate_rows),
            authenticated_body_count=len(published_content),
            projected_event_count=projected_event_count,
            listing_truncated=listing.listing_truncated,
            candidate_cap_reached=candidate_cap,
            body_cap_reached=body_cap,
            gaps=tuple(sorted(gaps)),
            complete=complete,
        )
        return closure

    def _post_listing_failure_closure(
        self,
        *,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        listing: OfficialListingResult,
        stored_listing: OfficialListingRows,
        window_start: datetime,
        window_end: datetime,
    ) -> OfficialCheckClosure:
        if stored_listing.source_attempt_id is None:
            raise DeepOfficialConfirmationError("stored official listing attempt is missing")
        request_context.budget.require_publishable()
        page_evidence = self._materialize_page_evidence(listing, stored_listing)
        with self.review_store.repository.connect() as connection:
            authenticated_body_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM fund_official_announcement_contents
                    WHERE brief_request_run_id=? AND fund_code=?
                      AND integrity_status='active'
                    """,
                    (request_context.request_run_id, fund_code),
                ).fetchone()[0]
            )
            projected_event_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM held_review_official_event_projections
                    WHERE brief_request_run_id=? AND fund_code=?
                    """,
                    (request_context.request_run_id, fund_code),
                ).fetchone()[0]
            )
        candidate_cap = len(listing.candidate_items) > self.official_policy.maximum_candidates
        gaps = set(listing.gap_codes)
        gaps.add("official_post_listing_processing_failed")
        if candidate_cap:
            gaps.add("official_announcement_candidate_cap_reached")
        return self._closure(
            request_run_id=request_context.request_run_id,
            fund_code=fund_code,
            attempt_id=stored_listing.source_attempt_id,
            identity=identity,
            registration_ids=listing.matched_registration_ids,
            page_evidence=page_evidence,
            window_start=window_start,
            window_end=window_end,
            listing_count=listing.listing_count,
            candidate_count=min(
                len(listing.candidate_items),
                self.official_policy.maximum_candidates,
            ),
            authenticated_body_count=authenticated_body_count,
            projected_event_count=projected_event_count,
            listing_truncated=listing.listing_truncated,
            candidate_cap_reached=candidate_cap,
            body_cap_reached=candidate_cap,
            gaps=tuple(sorted(gaps)),
            complete=False,
        )

    @staticmethod
    def _materialize_page_evidence(
        listing: OfficialListingResult,
        stored_listing: OfficialListingRows,
    ) -> Tuple[OfficialListingPageEvidence, ...]:
        pages_by_identity = {
            (item.url, item.checksum): item for item in stored_listing.page_evidence
        }
        return tuple(
            materialize_official_listing_page_evidence(
                capture,
                source_document_id=pages_by_identity[
                    (capture.canonical_page_url, capture.raw_sha256)
                ].id,
            )
            for capture in listing.page_captures
        )

    @staticmethod
    def _announcement_row(
        stored,
        product_name: str,
        checked_at: datetime,
    ) -> OfficialAnnouncementRow:
        value = stored.value
        if value.source_document_id is None:
            raise DeepOfficialConfirmationError("stored official announcement page is missing")
        row = OfficialAnnouncementRow(
            announcement_row_id=stored.id,
            fund_code=value.fund_code,
            product_name=product_name,
            listing_source_document_id=value.source_document_id,
            canonical_announcement_url=value.url,
            announcement_title=value.title,
            publisher=value.publisher,
            published_at=value.published_at,
            source_tier=value.source_tier,
            integrity_status="active",
            integrity_checked_at=checked_at,
        )
        row.validate()
        return row

    def _terminal_listing_failure(
        self,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        registration_ids: Tuple[str, ...],
        window_start: datetime,
        window_end: datetime,
        started_at: datetime,
        gap_code: str,
        outcome: SourceAttemptOutcome,
        error_code: SourceErrorCode,
    ) -> StoredOfficialCheckClosure:
        return self._terminal_without_listing(
            fund_code=fund_code,
            request_context=request_context,
            identity=identity,
            registration_ids=registration_ids,
            window_start=window_start,
            window_end=window_end,
            gap_code=gap_code,
            outcome=outcome,
            error_code=error_code,
            started_at=started_at,
            listing_truncated=True,
        )

    def _terminal_without_listing(
        self,
        *,
        fund_code: str,
        request_context: SourceRequestContext,
        identity: AuthenticatedOfficialManagerIdentity,
        registration_ids: Tuple[str, ...],
        window_start: datetime,
        window_end: datetime,
        gap_code: str,
        outcome: SourceAttemptOutcome,
        error_code: SourceErrorCode,
        started_at: Optional[datetime] = None,
        listing_truncated: bool = False,
    ) -> StoredOfficialCheckClosure:
        started = self._now() if started_at is None else started_at
        finished = self._now()
        cooldown = (
            finished + timedelta(minutes=30)
            if outcome is SourceAttemptOutcome.TRANSIENT_FAILURE
            else None
        )
        attempt = self._attempt(
            fund_code=fund_code,
            started_at=started,
            finished_at=finished,
            outcome=outcome,
            error_code=error_code,
            response_bytes=0,
            data_as_of=None,
            cooldown_until=cooldown,
        )
        request_context.budget.require_publishable()
        attempt_id = self.audit_store.record_source_attempt(
            request_context.request_run_id,
            attempt,
        )
        closure = self._closure(
            request_run_id=request_context.request_run_id,
            fund_code=fund_code,
            attempt_id=attempt_id,
            identity=identity,
            registration_ids=registration_ids,
            page_evidence=(),
            window_start=window_start,
            window_end=window_end,
            listing_count=0,
            candidate_count=0,
            authenticated_body_count=0,
            projected_event_count=0,
            listing_truncated=listing_truncated,
            candidate_cap_reached=False,
            body_cap_reached=False,
            gaps=(gap_code,),
            complete=False,
        )
        request_context.budget.require_publishable()
        return self.review_store.publish_official_check_closure(closure)

    def _closure(
        self,
        *,
        request_run_id: int,
        fund_code: str,
        attempt_id: int,
        identity: AuthenticatedOfficialManagerIdentity,
        registration_ids: Tuple[str, ...],
        page_evidence: Tuple[OfficialListingPageEvidence, ...],
        window_start: datetime,
        window_end: datetime,
        listing_count: int,
        candidate_count: int,
        authenticated_body_count: int,
        projected_event_count: int,
        listing_truncated: bool,
        candidate_cap_reached: bool,
        body_cap_reached: bool,
        gaps: Tuple[str, ...],
        complete: bool,
    ) -> OfficialCheckClosure:
        present = identity.state is OfficialManagerIdentityState.PRESENT
        closure = OfficialCheckClosure(
            brief_request_run_id=request_run_id,
            fund_code=fund_code,
            listing_source_attempt_id=attempt_id,
            official_registry_version=self.official_policy.official_registry_version,
            official_registry_checksum=self.official_policy.official_registry_checksum,
            source_registration_ids=registration_ids,
            manager_identity_state=identity.state,
            manager_identity_row_id=identity.row_id if present else None,
            manager_identity_source_document_id=(
                identity.source_document_id if present else None
            ),
            manager_identity_source_document_checksum=(
                identity.source_document_checksum if present else None
            ),
            manager_identity_normalized_name=identity.normalized_name if present else None,
            manager_identity_fingerprint=identity.fingerprint if present else None,
            listing_page_evidence=page_evidence,
            window_start=window_start,
            window_end=window_end,
            listing_count=listing_count,
            candidate_count=candidate_count,
            authenticated_body_count=authenticated_body_count,
            projected_event_count=projected_event_count,
            listing_truncated=listing_truncated,
            candidate_cap_reached=candidate_cap_reached,
            body_cap_reached=body_cap_reached,
            gap_codes=gaps,
            official_negative_check_complete=complete,
            policy_version=self.manual_policy.version,
            policy_checksum=self.manual_policy.checksum(),
            official_check_policy_version=self.official_policy.version,
            official_check_policy_checksum=self.official_policy.checksum(),
            created_at=self._now(),
            record_checksum="0" * 64,
        )
        closure = replace(closure, record_checksum=closure.expected_record_checksum())
        closure.validate()
        return closure

    def _attempt(
        self,
        *,
        fund_code: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: SourceAttemptOutcome,
        error_code: Optional[SourceErrorCode],
        response_bytes: int,
        data_as_of: Optional[datetime],
        cooldown_until: Optional[datetime] = None,
    ) -> SourceAttempt:
        attempt = SourceAttempt(
            source_id="fund_manager_official_documents",
            field_id="fund_manager_product_announcement",
            subject_key=f"fund:{fund_code}",
            attempt_number=1,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            data_as_of=data_as_of,
            error_code=error_code,
            cooldown_until=cooldown_until,
            force_actor=None,
            force_reason=None,
            registry_version=self.source_registry.version,
            registry_checksum=self.source_registry.checksum(),
            response_bytes=response_bytes,
        )
        attempt.validate()
        return attempt

    def _validate_request_context(self, context: SourceRequestContext) -> None:
        if type(context) is not SourceRequestContext:
            raise ValueError("official confirmation requires exact source request context")
        if context.audit_store is not self.audit_store:
            raise ValueError("official confirmation request uses a different audit store")
        if context.budget.mode is not RequestMode.DEEP:
            raise ValueError("official confirmation requires an explicit Deep request")
        if context.force_reason is not None:
            raise ValueError("official confirmation does not consume force authorization")

    def _now(self) -> datetime:
        value = validate_aware_datetime(self.clock(), "official confirmation clock")
        if value.utcoffset() != timedelta(0):
            raise ValueError("official confirmation clock must be UTC")
        return value.astimezone(timezone.utc)


__all__ = ["DeepOfficialConfirmationError", "DeepOfficialConfirmationService"]
