from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pytest

from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    RequestMode,
    SourceAttempt,
    SourceAttemptOutcome,
    SourceErrorCode,
)
from kunjin.decision.source_registry import SourceRegistryV1
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.models import DocumentKind, FundIdentity, SourceDocument
from kunjin.funds.service import SourceRequestContext
from kunjin.funds.store import FundDisclosureStore
from kunjin.holding_review.deep import DeepOfficialConfirmationService
from kunjin.holding_review.models import OfficialManagerIdentityState
from kunjin.holding_review.official import (
    OfficialAnnouncementCollector,
    OfficialFetchResult,
    OfficialListingFetchResult,
)
from kunjin.holding_review.store import HoldingReviewStore
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 21, 4, tzinfo=timezone.utc)
FUND_CODE = "123456"
FUND_NAME = "交银科创50指数增强证券投资基金"
PUBLISHER = "交银施罗德基金管理有限公司"


@dataclass
class DeepContext:
    repository: Repository
    audit_store: DecisionAuditStore
    disclosure_store: FundDisclosureStore
    review_store: HoldingReviewStore
    request_context: SourceRequestContext


def deep_context(
    tmp_path: Path,
    *,
    manager_name: str | None = PUBLISHER,
    profile_retrieved_at: datetime = NOW - timedelta(days=1),
    identity_conflict: bool = False,
    mode: RequestMode = RequestMode.DEEP,
    request_character: str = "d",
    monotonic: Callable[[], float] = lambda: 10.0,
) -> DeepContext:
    repository = Repository(tmp_path / f"deep-{request_character}.db")
    repository.migrate()
    disclosure_store = FundDisclosureStore(repository)
    if manager_name is not None:
        source = SourceDocument(
            id=None,
            fund_code=FUND_CODE,
            document_kind=DocumentKind.BASIC_PROFILE,
            title="基金基本资料",
            url="https://www.fund001.com/fund/123456/profile.html",
            source_name="fund_manager_official_documents",
            source_tier=1,
            publisher=PUBLISHER,
            published_at=None,
            retrieved_at=profile_retrieved_at,
            checksum="1" * 64,
        )
        identity = FundIdentity(
            fund_code=FUND_CODE,
            fund_name=FUND_NAME,
            status="active",
            fund_type="index",
            established_date=None,
            manager_name=manager_name,
            source_document_id=None,
        )
        identities = (identity,)
        if identity_conflict:
            identities = (
                identity,
                FundIdentity(
                    fund_code=FUND_CODE,
                    fund_name=FUND_NAME,
                    status="active",
                    fund_type="index",
                    established_date=None,
                    manager_name="另一基金管理有限公司",
                    source_document_id=None,
                ),
            )
        disclosure_store.publish_section(
            FUND_CODE,
            DocumentKind.BASIC_PROFILE,
            source,
            identities,
            "success",
        )
    audit_store = DecisionAuditStore(repository)
    budget = RequestBudget.create(
        mode,
        request_id=request_character * 32,
        monotonic=monotonic,
        wall_clock=lambda: NOW,
    )
    request_run_id = audit_store.begin_request(budget)
    health = SourceHealthService(audit_store, wall_clock=lambda: NOW)
    request_context = SourceRequestContext(request_run_id, budget, audit_store, health)
    return DeepContext(
        repository,
        audit_store,
        disclosure_store,
        HoldingReviewStore(repository),
        request_context,
    )


def listing_html(
    *,
    title: str,
    published_at: str | None,
    url: str = "/fund/123456/notice.html",
) -> bytes:
    date = "" if published_at is None else f"<span>{published_at}</span>"
    return (
        "<!doctype html><html><body><main data-total-pages=\"1\">"
        f'<input id="fundcode" value="{FUND_CODE}"><h2>{FUND_NAME}</h2>'
        f'<li>{date}<a title="{title}" href="{url}">{title}</a></li>'
        "</main></body></html>"
    ).encode()


def listing_html_many(items: tuple[tuple[str, str, str], ...]) -> bytes:
    rows = "".join(
        f'<li><span>{published_at}</span><a title="{title}" href="{url}">'
        f"{title}</a></li>"
        for title, published_at, url in items
    )
    return (
        "<!doctype html><html><body><main data-total-pages=\"1\">"
        f'<input id="fundcode" value="{FUND_CODE}"><h2>{FUND_NAME}</h2>'
        f"{rows}</main></body></html>"
    ).encode()


def listing_factory(
    payload: bytes | BaseException,
    calls: list[tuple[str, int]],
):
    def factory(deadline_at: datetime):
        def fetch(source, fund_code: str, page: int, maximum_bytes: int):
            calls.append((fund_code, page))
            if isinstance(payload, BaseException):
                raise payload
            url = source.index_url(fund_code, page)
            return OfficialListingFetchResult(
                requested_url=url,
                final_url=url,
                content_type="text/html; charset=utf-8",
                payload=payload,
                retrieved_at=NOW,
            )

        return fetch

    return factory


def body_factory(
    body: str | BaseException,
    calls: list[str],
):
    def factory(rows, deadline_at: datetime):
        def fetch(url: str, maximum_bytes: int):
            calls.append(url)
            if isinstance(body, BaseException):
                raise body
            payload = (
                "<!doctype html><html><body><main>"
                f"<p>{body}</p></main></body></html>"
            ).encode()
            return OfficialFetchResult(
                requested_url=url,
                final_url=url,
                content_type="text/html; charset=utf-8",
                payload=payload,
                retrieved_at=NOW,
            )

        return fetch

    return factory


def service(
    context: DeepContext,
    *,
    listing_payload: bytes | BaseException,
    listing_calls: list[tuple[str, int]],
    body_calls: list[str],
    body: str | BaseException = "普通正文",
) -> DeepOfficialConfirmationService:
    return DeepOfficialConfirmationService(
        disclosure_store=context.disclosure_store,
        audit_store=context.audit_store,
        review_store=context.review_store,
        listing_fetch_factory=listing_factory(listing_payload, listing_calls),
        announcement_fetch_factory=body_factory(body, body_calls),
        clock=lambda: NOW,
    )


def record_source_attempt(
    context: DeepContext,
    *,
    source_id: str,
    field_id: str,
) -> None:
    registry = SourceRegistryV1()
    context.audit_store.record_source_attempt(
        context.request_context.request_run_id,
        SourceAttempt(
            source_id=source_id,
            field_id=field_id,
            subject_key=f"fund:{FUND_CODE}",
            attempt_number=1,
            outcome=SourceAttemptOutcome.SUCCESS,
            started_at=NOW,
            finished_at=NOW,
            data_as_of=NOW,
            error_code=None,
            cooldown_until=None,
            force_actor=None,
            force_reason=None,
            registry_version=registry.version,
            registry_checksum=registry.checksum(),
            response_bytes=1,
        ),
    )


def test_missing_manager_identity_records_one_terminal_attempt_without_network(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path, manager_name=None)
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=listing_calls,
        body_calls=body_calls,
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == []
    assert body_calls == []
    assert stored.value.manager_identity_state is OfficialManagerIdentityState.MISSING
    assert stored.value.official_negative_check_complete is False
    assert "official_manager_identity_missing" in stored.value.gap_codes
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_attempts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM held_review_official_check_closures")
            .fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "context_changes,expected_error",
    (
        ({"manager_name": None}, SourceErrorCode.SOURCE_UNAVAILABLE),
        (
            {"profile_retrieved_at": NOW - timedelta(days=31)},
            SourceErrorCode.VALIDATION_FAILURE,
        ),
        ({"identity_conflict": True}, SourceErrorCode.IDENTITY_CONFLICT),
    ),
)
def test_identity_terminal_attempt_uses_state_specific_error_code(
    tmp_path: Path,
    context_changes: dict[str, object],
    expected_error: SourceErrorCode,
) -> None:
    context = deep_context(tmp_path, **context_changes)
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=[],
        body_calls=[],
    )

    confirmation.confirm(FUND_CODE, context.request_context)

    attempts = context.audit_store.authenticated_request_source_attempts(
        context.request_context.request_run_id,
        context.request_context.budget,
        NOW,
    )
    assert len(attempts) == 1
    assert attempts[0].attempt.error_code is expected_error


@pytest.mark.parametrize(
    "context_changes,expected_state,expected_gap",
    (
        (
            {"profile_retrieved_at": NOW - timedelta(days=31)},
            OfficialManagerIdentityState.STALE,
            "official_manager_identity_stale",
        ),
        (
            {"identity_conflict": True},
            OfficialManagerIdentityState.CONFLICTED,
            "official_manager_identity_conflicted",
        ),
    ),
)
def test_stale_or_conflicted_manager_identity_is_terminal_without_network(
    tmp_path: Path,
    context_changes: dict[str, object],
    expected_state: OfficialManagerIdentityState,
    expected_gap: str,
) -> None:
    context = deep_context(tmp_path, **context_changes)
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=listing_calls,
        body_calls=[],
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == []
    assert stored.value.manager_identity_state is expected_state
    assert expected_gap in stored.value.gap_codes


def test_unregistered_manager_records_unsupported_without_network(tmp_path: Path) -> None:
    context = deep_context(tmp_path, manager_name="未注册基金管理有限公司")
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=listing_calls,
        body_calls=body_calls,
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == []
    assert body_calls == []
    assert stored.value.manager_identity_state is OfficialManagerIdentityState.PRESENT
    assert stored.value.source_registration_ids == ()
    assert stored.value.gap_codes == ("official_source_set_unsupported",)


def test_ordinary_listing_publishes_complete_zero_candidate_closure(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}第二季度报告"
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=listing_calls,
        body_calls=body_calls,
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]
    assert body_calls == []
    assert stored.value.official_negative_check_complete is True
    assert stored.value.listing_count == 1
    assert stored.value.candidate_count == 0
    assert len(stored.value.listing_page_evidence) == 1
    assert stored.value.listing_page_evidence[0].source_document_id > 0


def test_high_impact_candidate_publishes_body_event_and_complete_closure(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=listing_calls,
        body_calls=body_calls,
        body=f"{FUND_NAME}基金合同终止。",
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert len(body_calls) == 1
    assert stored.value.candidate_count == 1
    assert stored.value.authenticated_body_count == 1
    assert stored.value.projected_event_count == 1
    assert stored.value.official_negative_check_complete is True


def test_missing_publication_date_is_retained_but_never_fabricated_or_persisted(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at=None),
        listing_calls=[],
        body_calls=[],
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert stored.value.official_negative_check_complete is False
    assert "official_listing_publication_date_missing" in stored.value.gap_codes
    assert stored.value.listing_count == 0
    assert stored.value.listing_page_evidence[0].parsed_item_count == 1
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM fund_announcements").fetchone()[0] == 0


def test_listing_exception_converges_to_one_incomplete_attempt_without_retry(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path)
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=TimeoutError("late"),
        listing_calls=listing_calls,
        body_calls=[],
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]
    assert stored.value.official_negative_check_complete is False
    assert "official_listing_timeout" in stored.value.gap_codes
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_attempts").fetchone()[0] == 1


def test_body_timeout_converges_to_incomplete_closure_without_retry(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    body_calls: list[str] = []
    title = f"关于{FUND_NAME}基金合同终止公告"
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=[],
        body_calls=body_calls,
        body=TimeoutError("late"),
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert len(body_calls) == 1
    assert stored.value.official_negative_check_complete is False
    assert "official_announcement_timeout" in stored.value.gap_codes
    assert stored.value.authenticated_body_count == 0


def test_unrelated_request_attempt_does_not_block_official_confirmation(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path)
    record_source_attempt(context, source_id="eastmoney_nav", field_id="formal_nav")
    title = f"关于{FUND_NAME}第二季度报告"
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=listing_calls,
        body_calls=[],
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]
    assert stored.value.official_negative_check_complete is True


def test_existing_exact_official_attempt_is_rejected_before_network(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    record_source_attempt(
        context,
        source_id="fund_manager_official_documents",
        field_id="fund_manager_product_announcement",
    )
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=listing_calls,
        body_calls=[],
    )

    with pytest.raises(ValueError, match="already has"):
        confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == []


def test_body_size_cap_is_recorded_separately_from_candidate_cap(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=[],
        body_calls=[],
        body="中" * 1_500_000,
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert stored.value.candidate_cap_reached is False
    assert stored.value.body_cap_reached is True
    assert stored.value.official_negative_check_complete is False
    assert "announcement_body_limit" in stored.value.gap_codes


def test_total_body_cap_stops_before_all_candidates_are_authenticated(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    listing_payload = listing_html_many(
        tuple(
            (title, "2026-07-20", f"/fund/123456/notice-{index}.html")
            for index in range(10)
        )
    )
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=listing_payload,
        listing_calls=[],
        body_calls=body_calls,
        body=f"{FUND_NAME}基金合同终止。" + "公" * 160_000,
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert len(body_calls) < 10
    assert stored.value.candidate_cap_reached is False
    assert stored.value.body_cap_reached is True
    assert stored.value.authenticated_body_count < stored.value.candidate_count
    assert "official_announcement_total_limit" in stored.value.gap_codes


def test_candidate_cap_fetches_at_most_twenty_bodies_and_closes_incomplete(
    tmp_path: Path,
) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    listing_payload = listing_html_many(
        tuple(
            (title, "2026-07-20", f"/fund/123456/capped-{index}.html")
            for index in range(21)
        )
    )
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=listing_payload,
        listing_calls=[],
        body_calls=body_calls,
        body=f"{FUND_NAME}基金合同终止。",
    )

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert len(body_calls) == 20
    assert stored.value.candidate_count == 20
    assert stored.value.candidate_cap_reached is True
    assert stored.value.body_cap_reached is True
    assert stored.value.official_negative_check_complete is False
    assert "official_announcement_candidate_cap_reached" in stored.value.gap_codes


def test_unexpected_collector_failure_closes_existing_attempt_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=listing_calls,
        body_calls=body_calls,
    )

    def fail_collect(*args, **kwargs):
        raise RuntimeError("unexpected collector failure")

    monkeypatch.setattr(OfficialAnnouncementCollector, "collect", fail_collect)

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]
    assert body_calls == []
    assert stored.value.listing_count == 1
    assert stored.value.candidate_count == 1
    assert stored.value.authenticated_body_count == 0
    assert stored.value.projected_event_count == 0
    assert stored.value.official_negative_check_complete is False
    assert stored.value.gap_codes == ("official_post_listing_processing_failed",)
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_attempts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM held_review_official_check_closures")
            .fetchone()[0]
            == 1
        )


def test_post_listing_fallback_recounts_already_published_body_and_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}基金合同终止公告"
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=[],
        body_calls=[],
        body=f"{FUND_NAME}基金合同终止。",
    )
    original_closure = confirmation._closure
    closure_calls = 0

    def fail_first_closure(**kwargs):
        nonlocal closure_calls
        closure_calls += 1
        if closure_calls == 1:
            raise RuntimeError("unexpected closure assembly failure")
        return original_closure(**kwargs)

    monkeypatch.setattr(confirmation, "_closure", fail_first_closure)

    stored = confirmation.confirm(FUND_CODE, context.request_context)

    assert closure_calls == 2
    assert stored.value.authenticated_body_count == 1
    assert stored.value.projected_event_count == 1
    assert stored.value.official_negative_check_complete is False
    assert stored.value.gap_codes == ("official_post_listing_processing_failed",)
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_attempts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM held_review_official_check_closures")
            .fetchone()[0]
            == 1
        )


def test_budget_expiry_after_listing_raises_without_publishing_more_rows(
    tmp_path: Path,
) -> None:
    monotonic_value = [10.0]
    context = deep_context(tmp_path, monotonic=lambda: monotonic_value[0])
    title = f"关于{FUND_NAME}基金合同终止公告"
    listing_calls: list[tuple[str, int]] = []
    body_calls: list[str] = []

    def expiring_body_factory(rows, deadline_at: datetime):
        def fetch(url: str, maximum_bytes: int):
            body_calls.append(url)
            monotonic_value[0] = 500.0
            payload = (
                "<!doctype html><html><body><main>"
                f"<p>{FUND_NAME}基金合同终止。</p></main></body></html>"
            ).encode()
            return OfficialFetchResult(
                requested_url=url,
                final_url=url,
                content_type="text/html; charset=utf-8",
                payload=payload,
                retrieved_at=NOW,
            )

        return fetch

    confirmation = DeepOfficialConfirmationService(
        disclosure_store=context.disclosure_store,
        audit_store=context.audit_store,
        review_store=context.review_store,
        listing_fetch_factory=listing_factory(
            listing_html(title=title, published_at="2026-07-20"),
            listing_calls,
        ),
        announcement_fetch_factory=expiring_body_factory,
        clock=lambda: NOW,
    )

    with pytest.raises(BudgetExpired, match="deadline"):
        confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]
    assert len(body_calls) == 1
    with context.repository.connect() as connection:
        assert connection.execute("SELECT count(*) FROM source_attempts").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM fund_official_announcement_contents")
            .fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM held_review_official_event_projections")
            .fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM held_review_official_check_closures")
            .fetchone()[0]
            == 0
        )


def test_second_confirmation_is_rejected_before_network(tmp_path: Path) -> None:
    context = deep_context(tmp_path)
    title = f"关于{FUND_NAME}第二季度报告"
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=listing_html(title=title, published_at="2026-07-20"),
        listing_calls=listing_calls,
        body_calls=[],
    )
    confirmation.confirm(FUND_CODE, context.request_context)

    with pytest.raises(ValueError, match="already has"):
        confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == [(FUND_CODE, 1)]


def test_non_deep_request_is_rejected_before_network(tmp_path: Path) -> None:
    context = deep_context(tmp_path, mode=RequestMode.RAPID)
    listing_calls: list[tuple[str, int]] = []
    confirmation = service(
        context,
        listing_payload=AssertionError("must not fetch"),
        listing_calls=listing_calls,
        body_calls=[],
    )

    with pytest.raises(ValueError, match="Deep"):
        confirmation.confirm(FUND_CODE, context.request_context)

    assert listing_calls == []
