from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kunjin.brief.models import OfficialEventCode
from kunjin.funds.official_domains import OfficialSourceRegistration
from kunjin.funds.risk.documents import (
    OfficialDocumentError,
    OfficialDocumentResourceLimitError,
)
from kunjin.holding_review.models import (
    OfficialListingPageEvidence,
    OfficialListingTerminalState,
    TriggeredReviewCode,
)
from kunjin.holding_review.official import (
    AnnouncementContentError,
    OfficialAnnouncementCollector,
    OfficialAnnouncementHttpFetcher,
    OfficialAnnouncementRow,
    OfficialCollectionContext,
    OfficialFetchResult,
    OfficialListingAcquirer,
    OfficialListingFetchResult,
    OfficialListingHttpFetcher,
    OfficialListingPageCapture,
    classify_official_listing_title,
    materialize_official_event_projection,
    materialize_official_listing_page_evidence,
    normalize_announcement_html,
    persistable_official_listing_items,
)
from kunjin.holding_review.policy import HeldFundManualReviewPolicyV1, OfficialCheckPolicyV1

NOW = datetime(2026, 7, 20, 8, tzinfo=timezone.utc)
PUBLISHED = NOW - timedelta(days=1)
PUBLISHER = "交银施罗德基金管理有限公司"
FUND_CODE = "123456"
FUND_NAME = "交银科创50指数增强证券投资基金"
PUBLIC_DNS_RESULT = (
    (2, 1, 6, "", ("93.184.216.34", 443)),
)


class FakeHttpResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        final_url: str,
        content_type: str = "text/html; charset=utf-8",
        declared_size: str | None = None,
    ) -> None:
        self._payload = payload
        self._offset = 0
        self._final_url = final_url
        self.headers = {
            "Content-Type": content_type,
            **({} if declared_size is None else {"Content-Length": declared_size}),
        }

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def geturl(self) -> str:
        return self._final_url

    def read(self, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeHttpOpener:
    def __init__(self, responses: tuple[FakeHttpResponse | BaseException, ...]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, float]] = []

    def open(self, request, *, timeout: float):
        self.calls.append((request.full_url, timeout))
        value = self._responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def row(
    *,
    row_id: int = 11,
    title: str | None = None,
    url: str = "https://www.fund001.com/notices/123456-1.html",
    published_at: datetime = PUBLISHED,
    integrity_status: str = "active",
) -> OfficialAnnouncementRow:
    value = OfficialAnnouncementRow(
        announcement_row_id=row_id,
        fund_code=FUND_CODE,
        product_name=FUND_NAME,
        listing_source_document_id=7,
        canonical_announcement_url=url,
        announcement_title=title or f"关于{FUND_NAME}基金合同终止公告",
        publisher=PUBLISHER,
        published_at=published_at,
        source_tier=1,
        integrity_status=integrity_status,
        integrity_checked_at=NOW,
    )
    value.validate()
    return value


def html(body: str, *, title: str = "公告") -> bytes:
    return (
        f"<!doctype html><html><head><title>{title}</title></head>"
        f"<body><main><p>{body}</p></main></body></html>"
    ).encode()


def response(
    candidate: OfficialAnnouncementRow,
    body: str,
    *,
    payload: bytes | None = None,
    content_type: str = "text/html; charset=utf-8",
    final_url: str | None = None,
) -> OfficialFetchResult:
    value = OfficialFetchResult(
        requested_url=candidate.canonical_announcement_url,
        final_url=final_url or candidate.canonical_announcement_url,
        content_type=content_type,
        payload=payload if payload is not None else html(body),
        retrieved_at=NOW,
    )
    value.validate()
    return value


def context(**changes: object) -> OfficialCollectionContext:
    values: dict[str, object] = {
        "brief_request_run_id": 17,
        "source_attempt_id": 19,
        "fund_code": FUND_CODE,
        "product_name": FUND_NAME,
        "source_set_complete": True,
        "window_complete": True,
        "terminal_query_complete": True,
        "upstream_gap_codes": (),
        "deadline_at": NOW + timedelta(minutes=1),
    }
    values.update(changes)
    value = OfficialCollectionContext(**values)
    value.validate()
    return value


def collector_for(mapping: dict[str, OfficialFetchResult | BaseException | None]):
    calls: list[tuple[str, int]] = []

    def fetch(url: str, maximum_bytes: int) -> OfficialFetchResult | None:
        calls.append((url, maximum_bytes))
        value = mapping[url]
        if isinstance(value, BaseException):
            raise value
        return value

    return OfficialAnnouncementCollector(fetch), calls


def paginated_registration() -> OfficialSourceRegistration:
    return OfficialSourceRegistration(
        registration_id="fund001_test",
        identity=PUBLISHER,
        source_kind="fund_manager",
        accepted_hosts=("www.fund001.com",),
        document_index_url_template=(
            "https://www.fund001.com/fund/{fund_code}/notices?page={page}"
        ),
        identity_aliases=("交银施罗德基金",),
        binds_fund_identity=True,
        requires_publication_date=True,
    )


def listing_html(
    items: tuple[tuple[str, str | None, str], ...],
    *,
    page: int,
    total_pages: int,
    fund_code: str = FUND_CODE,
    product_name: str = FUND_NAME,
    extra: str = "",
) -> bytes:
    rows = "".join(
        "<li>"
        + (f"<span>{published_at}</span>" if published_at is not None else "")
        + f'<a title="{title}" href="{url}">{title}</a></li>'
        for title, published_at, url in items
    )
    next_link = (
        f'<a rel="next" href="?page={page + 1}">next</a>'
        if page < total_pages
        else ""
    )
    return (
        "<!doctype html><html><body>"
        f'<main data-total-pages="{total_pages}">'
        f'<input id="fundcode" value="{fund_code}"><h2>{product_name}</h2>'
        f"{rows}{next_link}{extra}</main></body></html>"
    ).encode()


def listing_response(
    source: OfficialSourceRegistration,
    page: int,
    payload: bytes,
    *,
    final_url: str | None = None,
    content_type: str = "text/html; charset=utf-8",
    retrieved_at: datetime = NOW,
) -> OfficialListingFetchResult:
    requested = source.index_url(FUND_CODE, page)
    value = OfficialListingFetchResult(
        requested_url=requested,
        final_url=final_url or requested,
        content_type=content_type,
        payload=payload,
        retrieved_at=retrieved_at,
    )
    value.validate()
    return value


def listing_acquirer_for(
    mapping: dict[tuple[str, int], OfficialListingFetchResult | BaseException | None],
    *,
    maximum_pages: int = 10,
    maximum_items: int = 1000,
    maximum_page_bytes: int = 2 * 1024 * 1024,
):
    calls: list[tuple[str, int, int]] = []

    def fetch(
        source: OfficialSourceRegistration,
        fund_code: str,
        page: int,
        maximum_bytes: int,
    ) -> OfficialListingFetchResult | None:
        calls.append((source.registration_id, page, maximum_bytes))
        value = mapping[(source.registration_id, page)]
        if isinstance(value, BaseException):
            raise value
        return value

    return (
        OfficialListingAcquirer(
            fetch=fetch,
            registrations=(paginated_registration(),),
            maximum_pages=maximum_pages,
            maximum_items=maximum_items,
            maximum_page_bytes=maximum_page_bytes,
        ),
        calls,
    )


def test_normalize_utf8_html_uses_body_only_nfkc_and_stable_whitespace() -> None:
    payload = (
        "<!doctype html><html><head><title>不应进入正文</title></head>"
        "<body><p>Ａ基金\r\n   公告</p><div>第二行&nbsp; 内容</div></body></html>"
    ).encode()

    assert normalize_announcement_html(payload, "text/html; charset=UTF-8") == (
        "A基金 公告 第二行 内容"
    )


def test_normalize_gb18030_html_requires_matching_declared_and_meta_charset() -> None:
    source = (
        '<html><head><meta charset="gb18030"></head>'
        "<body><p>基金合同终止</p></body></html>"
    )

    assert normalize_announcement_html(
        source.encode("gb18030"), "text/html; charset=gb18030"
    ) == "基金合同终止"


@pytest.mark.parametrize(
    "payload,content_type,reason",
    (
        (html("正文"), "application/pdf", "announcement_container_invalid"),
        (b"%PDF-1.7", "text/html; charset=utf-8", "announcement_container_invalid"),
        (html("正文"), "text/html; charset=big5", "announcement_charset_unsupported"),
        (
            b'<html><head><meta charset="gb18030"></head><body>body</body></html>',
            "text/html; charset=utf-8",
            "announcement_charset_conflict",
        ),
        (
            b'<html><body><input type="password"></body></html>',
            "text/html; charset=utf-8",
            "announcement_login_page",
        ),
        (
            html("订阅后查看公告全文"),
            "text/html; charset=utf-8",
            "announcement_paywall",
        ),
    ),
)
def test_normalize_rejects_invalid_container_charset_or_login(
    payload: bytes, content_type: str, reason: str
) -> None:
    with pytest.raises(AnnouncementContentError) as captured:
        normalize_announcement_html(payload, content_type)

    assert captured.value.reason_code == reason


def test_normalize_enforces_decoded_utf8_body_limit() -> None:
    policy = HeldFundManualReviewPolicyV1()
    payload = html("中" * policy.maximum_announcement_body_bytes)

    with pytest.raises(AnnouncementContentError) as captured:
        normalize_announcement_html(
            payload,
            "text/html; charset=utf-8",
            maximum_bytes=policy.maximum_announcement_body_bytes,
        )

    assert captured.value.reason_code == "announcement_body_limit"


@pytest.mark.parametrize(
    "title_suffix,body_phrase,event_code,trigger",
    (
        (
            "基金财产清算报告",
            "基金财产进入清算程序",
            OfficialEventCode.FUND_LIQUIDATION_NOTICE,
            TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
        ),
        (
            "基金合同终止公告",
            "基金合同终止",
            OfficialEventCode.FUND_TERMINATION_NOTICE,
            TriggeredReviewCode.FULL_EXIT_FEASIBILITY_REVIEW,
        ),
        (
            "暂停赎回业务公告",
            "暂停赎回业务",
            OfficialEventCode.REDEMPTION_RESTRICTION_NOTICE,
            TriggeredReviewCode.REDEMPTION_RESTRICTION_REVIEW,
        ),
        (
            "基金经理变更公告",
            "基金经理发生变更",
            OfficialEventCode.MANAGER_CHANGE_NOTICE,
            TriggeredReviewCode.MANAGER_CHANGE_REVIEW,
        ),
        (
            "调整管理费率公告",
            "调整管理费率",
            OfficialEventCode.FEE_CHANGE_NOTICE,
            TriggeredReviewCode.FEE_CHANGE_REVIEW,
        ),
        (
            "变更业绩比较基准公告",
            "变更业绩比较基准",
            OfficialEventCode.BENCHMARK_CHANGE_NOTICE,
            TriggeredReviewCode.BENCHMARK_CHANGE_REVIEW,
        ),
    ),
)
def test_collector_projects_each_supported_event_from_same_fund_title_and_body(
    title_suffix: str,
    body_phrase: str,
    event_code: OfficialEventCode,
    trigger: TriggeredReviewCode,
) -> None:
    candidate = row(title=f"关于{FUND_NAME}{title_suffix}")
    fetched = response(candidate, f"{FUND_NAME}现公告如下：{body_phrase}。")
    collector, _ = collector_for({candidate.canonical_announcement_url: fetched})

    result = collector.collect((candidate,), context())

    assert result.gap_codes == ()
    assert result.official_negative_check_complete is True
    assert len(result.contents) == 1
    assert len(result.event_candidates) == 1
    event = result.event_candidates[0]
    assert event.event_code is event_code
    assert event.triggered_review_code is trigger
    assert event.normalized_content_sha256 == result.contents[0].normalized_content_sha256


@pytest.mark.parametrize(
    "body",
    (
        "本公告只讨论系统维护，与基金合同状态无关。",
        "其他证券投资基金基金合同终止。",
        f"{FUND_NAME}发布日常说明。其他证券投资基金基金合同终止。",
        f"{FUND_NAME}基金合同未终止。",
        f"{FUND_NAME}基金合同终止事项已经撤销。",
    ),
)
def test_positive_title_without_affirmative_same_fund_body_is_a_conflict(body: str) -> None:
    candidate = row()
    collector, _ = collector_for(
        {candidate.canonical_announcement_url: response(candidate, body)}
    )

    result = collector.collect((candidate,), context())

    assert result.event_candidates == ()
    assert "official_event_body_conflict" in result.gap_codes
    assert result.official_negative_check_complete is False


@pytest.mark.parametrize(
    "title,integrity_status",
    (
        (f"关于{FUND_NAME}基金合同终止公告的更正公告", "corrected"),
        (f"关于撤回{FUND_NAME}基金合同终止公告", "retracted"),
    ),
)
def test_correction_or_retraction_never_projects_active_event(
    title: str, integrity_status: str
) -> None:
    candidate = row(title=title, integrity_status=integrity_status)
    fetched = response(candidate, f"{FUND_NAME}基金合同终止。")
    collector, _ = collector_for({candidate.canonical_announcement_url: fetched})

    result = collector.collect((candidate,), context())

    assert result.event_candidates == ()
    assert "official_event_integrity_unresolved" in result.gap_codes
    assert result.official_negative_check_complete is False


def test_integrity_title_and_active_listing_conflict_without_body_fetch() -> None:
    candidate = row(
        title=f"关于{FUND_NAME}基金合同终止公告的更正公告",
        integrity_status="active",
    )
    collector, calls = collector_for(
        {candidate.canonical_announcement_url: AssertionError("must not fetch")}
    )

    result = collector.collect((candidate,), context())

    assert calls == []
    assert result.contents == ()
    assert result.event_candidates == ()
    assert "official_event_integrity_unresolved" in result.gap_codes


def test_title_only_fetch_failure_never_projects_event() -> None:
    candidate = row()
    collector, _ = collector_for({candidate.canonical_announcement_url: None})

    result = collector.collect((candidate,), context())

    assert result.contents == ()
    assert result.event_candidates == ()
    assert "official_announcement_content_missing" in result.gap_codes
    assert "official_event_body_incomplete" in result.gap_codes


def test_shared_listing_document_gets_distinct_composite_body_identities() -> None:
    first = row(row_id=11, url="https://www.fund001.com/notices/a.html")
    second = row(
        row_id=12,
        url="https://www.fund001.com/notices/b.html",
        published_at=PUBLISHED - timedelta(minutes=1),
    )
    mapping = {
        first.canonical_announcement_url: response(first, f"{FUND_NAME}基金合同终止。"),
        second.canonical_announcement_url: response(
            second, f"{FUND_NAME}基金合同终止，第二份正文。"
        ),
    }
    collector, _ = collector_for(mapping)

    result = collector.collect((second, first), context())

    assert [item.listing_source_document_id for item in result.contents] == [7, 7]
    assert [item.canonical_announcement_url for item in result.contents] == [
        first.canonical_announcement_url,
        second.canonical_announcement_url,
    ]
    assert (
        result.contents[0].normalized_content_sha256
        != result.contents[1].normalized_content_sha256
    )


def test_redirect_and_publisher_mismatch_fail_closed_with_explicit_gaps() -> None:
    redirected = row(row_id=11, url="https://www.fund001.com/notices/a.html")
    mismatched = replace(
        row(row_id=12, url="https://www.fund001.com/notices/b.html"),
        publisher="冒充的基金管理人",
    )
    collector, _ = collector_for(
        {
            redirected.canonical_announcement_url: response(
                redirected,
                f"{FUND_NAME}基金合同终止。",
                final_url="https://evil.example/notices/a.html",
            ),
            mismatched.canonical_announcement_url: response(
                mismatched, f"{FUND_NAME}基金合同终止。"
            ),
        }
    )

    result = collector.collect((redirected, mismatched), context())

    assert result.contents == ()
    assert "official_announcement_redirect_rejected" in result.gap_codes
    assert "official_announcement_publisher_mismatch" in result.gap_codes


def test_timeout_is_terminal_and_is_not_retried() -> None:
    candidate = row()
    collector, calls = collector_for(
        {candidate.canonical_announcement_url: TimeoutError("late")}
    )

    result = collector.collect((candidate,), context())

    assert len(calls) == 1
    assert "official_announcement_timeout" in result.gap_codes
    assert result.official_negative_check_complete is False


def test_late_result_is_ignored_without_retry_or_content_publication() -> None:
    candidate = row()
    fetched = replace(
        response(candidate, f"{FUND_NAME}基金合同终止。"),
        retrieved_at=NOW + timedelta(minutes=2),
    )
    collector, calls = collector_for({candidate.canonical_announcement_url: fetched})

    result = collector.collect((candidate,), context())

    assert len(calls) == 1
    assert result.contents == ()
    assert result.event_candidates == ()
    assert "official_announcement_late_result" in result.gap_codes


def test_ordinary_announcement_never_downloads_a_body() -> None:
    candidate = row(title=f"关于{FUND_NAME}2026年第二季度报告")
    collector, calls = collector_for(
        {candidate.canonical_announcement_url: AssertionError("must not fetch")}
    )

    result = collector.collect((candidate,), context())

    assert calls == []
    assert result.contents == ()
    assert result.event_candidates == ()
    assert result.official_negative_check_complete is True


def test_newer_ordinary_rows_do_not_exhaust_high_impact_candidate_cap() -> None:
    ordinary = tuple(
        row(
            row_id=index + 1,
            title=f"关于{FUND_NAME}第{index + 1}次普通提示公告",
            url=f"https://www.fund001.com/notices/ordinary-{index + 1}.html",
            published_at=PUBLISHED - timedelta(minutes=index),
        )
        for index in range(25)
    )
    high_impact = row(
        row_id=100,
        url="https://www.fund001.com/notices/high-impact.html",
        published_at=PUBLISHED - timedelta(hours=1),
    )
    collector, calls = collector_for(
        {
            **{
                item.canonical_announcement_url: AssertionError("must not fetch")
                for item in ordinary
            },
            high_impact.canonical_announcement_url: response(
                high_impact, f"{FUND_NAME}基金合同终止。"
            ),
        }
    )

    result = collector.collect((*ordinary, high_impact), context())

    assert [call[0] for call in calls] == [high_impact.canonical_announcement_url]
    assert len(result.event_candidates) == 1
    assert "official_announcement_candidate_cap_reached" not in result.gap_codes


def test_candidate_cap_stops_body_work_in_deterministic_publication_order() -> None:
    policy = HeldFundManualReviewPolicyV1()
    candidates = tuple(
        row(
            row_id=index + 1,
            url=f"https://www.fund001.com/notices/{index + 1}.html",
            published_at=PUBLISHED - timedelta(minutes=index),
        )
        for index in range(policy.maximum_announcement_candidates + 1)
    )
    collector, calls = collector_for(
        {
            item.canonical_announcement_url: response(
                item, f"{FUND_NAME}基金合同终止。"
            )
            for item in candidates
        }
    )

    result = collector.collect(tuple(reversed(candidates)), context())

    assert len(calls) == policy.maximum_announcement_candidates
    assert calls[0][0] == candidates[0].canonical_announcement_url
    assert "official_announcement_candidate_cap_reached" in result.gap_codes


def test_total_decoded_content_cap_stops_additional_fetches() -> None:
    policy = HeldFundManualReviewPolicyV1()
    body = f"{FUND_NAME}基金合同终止。" + "公" * 160_000
    candidates = tuple(
        row(
            row_id=index + 1,
            url=f"https://www.fund001.com/notices/large-{index + 1}.html",
            published_at=PUBLISHED - timedelta(minutes=index),
        )
        for index in range(10)
    )
    collector, calls = collector_for(
        {
            item.canonical_announcement_url: response(item, body)
            for item in candidates
        }
    )

    result = collector.collect(candidates, context())

    assert len(calls) < len(candidates)
    assert sum(item.normalized_content_bytes for item in result.contents) <= (
        policy.maximum_announcement_total_bytes
    )
    assert "official_announcement_total_limit" in result.gap_codes


@pytest.mark.parametrize(
    "changes,gap",
    (
        ({"source_set_complete": False}, "official_source_set_incomplete"),
        ({"window_complete": False}, "official_window_incomplete"),
        ({"terminal_query_complete": False}, "official_query_incomplete"),
        (
            {"upstream_gap_codes": ("source_failed",)},
            "source_failed",
        ),
    ),
)
def test_upstream_closure_gap_keeps_negative_check_incomplete(
    changes: dict[str, object], gap: str
) -> None:
    collector, calls = collector_for({})

    result = collector.collect((), context(**changes))

    assert calls == []
    assert gap in result.gap_codes
    assert result.official_negative_check_complete is False


def test_materialize_projection_requires_real_persisted_content_id_and_authenticates() -> None:
    candidate = row()
    collector, _ = collector_for(
        {
            candidate.canonical_announcement_url: response(
                candidate, f"{FUND_NAME}基金合同终止。"
            )
        }
    )
    collected = collector.collect((candidate,), context())

    projection = materialize_official_event_projection(
        collected.event_candidates[0],
        content=collected.contents[0],
        announcement_content_id=31,
        policy=HeldFundManualReviewPolicyV1(),
    )

    projection.validate()
    assert projection.announcement_content_id == 31
    assert projection.announcement_row_id == candidate.announcement_row_id
    assert projection.record_checksum == projection.expected_record_checksum()


def test_materialize_projection_rejects_candidate_content_checksum_mismatch() -> None:
    candidate = row()
    collector, _ = collector_for(
        {
            candidate.canonical_announcement_url: response(
                candidate, f"{FUND_NAME}基金合同终止。"
            )
        }
    )
    collected = collector.collect((candidate,), context())
    mismatched = replace(
        collected.contents[0],
        normalized_content="另一份认证正文",
        normalized_content_bytes=len("另一份认证正文".encode()),
    )
    mismatched = replace(
        mismatched,
        normalized_content_sha256=hashlib.sha256(
            mismatched.normalized_content.encode()
        ).hexdigest(),
        record_checksum="0" * 64,
    )
    mismatched = replace(mismatched, record_checksum=mismatched.expected_record_checksum())

    with pytest.raises(ValueError, match="does not bind"):
        materialize_official_event_projection(
            collected.event_candidates[0],
            content=mismatched,
            announcement_content_id=31,
            policy=HeldFundManualReviewPolicyV1(),
        )


def test_ordinary_public_financial_phrases_are_not_secret_markers() -> None:
    candidate = row()
    body = (
        f"{FUND_NAME}基金合同终止。The email address, account number, and exact amount "
        "are described in the public notice."
    )
    collector, _ = collector_for(
        {candidate.canonical_announcement_url: response(candidate, body)}
    )

    result = collector.collect((candidate,), context())

    assert len(result.contents) == 1
    assert result.gap_codes == ()


def test_registered_listing_one_page_reaches_authenticated_source_final_page() -> None:
    source = paginated_registration()
    payload = listing_html(
        (
            (f"关于{FUND_NAME}基金合同终止公告", "2026-07-19", "/notice/1.html"),
            (f"关于{FUND_NAME}第二季度报告", "2026-07-18", "/notice/2.html"),
        ),
        page=1,
        total_pages=1,
    )
    acquirer, calls = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, payload)}
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    result.validate()
    assert [call[1] for call in calls] == [1]
    assert result.matched_registration_ids == (source.registration_id,)
    assert result.listing_count == 2
    assert result.candidate_count == 1
    assert type(result.page_captures[0]) is OfficialListingPageCapture
    assert (
        result.page_captures[0].terminal_state
        is OfficialListingTerminalState.SOURCE_FINAL_PAGE
    )
    assert result.page_captures[0].raw_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.page_captures[0].raw_payload == payload
    stored_page = materialize_official_listing_page_evidence(
        result.page_captures[0], source_document_id=101
    )
    assert type(stored_page) is OfficialListingPageEvidence
    assert stored_page.source_document_id == 101
    with pytest.raises(ValueError, match="positive"):
        materialize_official_listing_page_evidence(
            result.page_captures[0], source_document_id=0
        )
    assert result.listing_closure_complete is True
    assert result.gap_codes == ()


def test_identity_bound_listing_ignores_navigation_before_product_scope() -> None:
    source = paginated_registration()
    payload = (
        "<!doctype html><html><body>"
        '<nav><li><a href="/login">登录入口</a></li></nav>'
        '<main data-total-pages="1">'
        f'<input id="fundcode" value="{FUND_CODE}"><h2>{FUND_NAME}</h2>'
        f'<li><span>2026-07-19</span><a href="/notice/1.html">关于{FUND_NAME}季度报告</a></li>'
        "</main></body></html>"
    ).encode()
    acquirer, _ = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, payload)}
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert result.listing_count == 1
    assert result.items[0].canonical_url == "https://www.fund001.com/notice/1.html"
    assert result.gap_codes == ()


def test_registered_listing_stops_at_authenticated_180_day_window_boundary() -> None:
    source = paginated_registration()
    pages = {
        (source.registration_id, 1): listing_response(
            source,
            1,
            listing_html(
                (
                    (f"关于{FUND_NAME}第二季度报告", "2026-07-19", "/notice/1.html"),
                    (f"关于{FUND_NAME}第一季度报告", "2026-04-21", "/notice/2.html"),
                ),
                page=1,
                total_pages=50,
            ),
        ),
        (source.registration_id, 2): listing_response(
            source,
            2,
            listing_html(
                (
                    (f"关于{FUND_NAME}提示公告", "2026-01-22", "/notice/3.html"),
                    (f"关于{FUND_NAME}历史公告", "2026-01-20", "/notice/4.html"),
                ),
                page=2,
                total_pages=50,
            ),
        ),
    }
    acquirer, calls = listing_acquirer_for(pages)

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert [call[1] for call in calls] == [1, 2]
    assert (
        result.page_captures[-1].terminal_state
        is OfficialListingTerminalState.WINDOW_BOUNDARY_REACHED
    )
    assert [item.canonical_url for item in result.items] == [
        "https://www.fund001.com/notice/1.html",
        "https://www.fund001.com/notice/2.html",
        "https://www.fund001.com/notice/3.html",
    ]
    assert [item.canonical_url for item in result.page_captures[1].parsed_items] == [
        "https://www.fund001.com/notice/3.html",
        "https://www.fund001.com/notice/4.html",
    ]
    assert result.listing_closure_complete is True


@pytest.mark.parametrize("failure_kind", ("within_page", "cross_page"))
def test_listing_order_failure_cannot_authenticate_window_stop(failure_kind: str) -> None:
    source = paginated_registration()
    if failure_kind == "within_page":
        page_one_items = (
            (f"关于{FUND_NAME}较旧公告", "2026-05-01", "/notice/old.html"),
            (f"关于{FUND_NAME}较新公告", "2026-06-01", "/notice/new.html"),
        )
        page_two_items = (
            (f"关于{FUND_NAME}历史公告", "2026-01-01", "/notice/history.html"),
        )
    else:
        page_one_items = (
            (f"关于{FUND_NAME}最新公告", "2026-07-01", "/notice/new.html"),
            (f"关于{FUND_NAME}较旧公告", "2026-05-01", "/notice/old.html"),
        )
        page_two_items = (
            (f"关于{FUND_NAME}跨页倒序公告", "2026-06-01", "/notice/conflict.html"),
            (f"关于{FUND_NAME}历史公告", "2026-01-01", "/notice/history.html"),
        )
    mapping = {
        (source.registration_id, 1): listing_response(
            source, 1, listing_html(page_one_items, page=1, total_pages=3)
        ),
        (source.registration_id, 2): listing_response(
            source, 2, listing_html(page_two_items, page=2, total_pages=3)
        ),
        (source.registration_id, 3): listing_response(
            source, 3, listing_html((), page=3, total_pages=3)
        ),
    }
    acquirer, calls = listing_acquirer_for(mapping)

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert [call[1] for call in calls] == [1, 2, 3]
    assert "official_listing_order_conflict" in result.gap_codes
    assert (
        result.page_captures[-1].terminal_state
        is OfficialListingTerminalState.SOURCE_FINAL_PAGE
    )
    assert result.listing_closure_complete is False


def test_missing_publication_date_prevents_boundary_stop_and_is_a_gap() -> None:
    source = paginated_registration()
    mapping = {
        (source.registration_id, 1): listing_response(
            source,
            1,
            listing_html(
                (
                    (f"关于{FUND_NAME}日期缺失公告", None, "/notice/missing.html"),
                    (f"关于{FUND_NAME}历史公告", "2026-01-01", "/notice/history.html"),
                ),
                page=1,
                total_pages=2,
            ),
        ),
        (source.registration_id, 2): listing_response(
            source, 2, listing_html((), page=2, total_pages=2)
        ),
    }
    acquirer, calls = listing_acquirer_for(mapping)

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert [call[1] for call in calls] == [1, 2]
    assert "official_listing_publication_date_missing" in result.gap_codes
    assert result.listing_closure_complete is False
    assert result.page_captures[0].parsed_item_count == 2
    assert result.page_captures[0].parsed_items[0].published_at is None
    assert [item.canonical_url for item in persistable_official_listing_items(
        result.page_captures[0]
    )] == ["https://www.fund001.com/notice/history.html"]


def test_duplicate_listing_item_is_retained_as_an_explicit_gap() -> None:
    source = paginated_registration()
    duplicated = (f"关于{FUND_NAME}提示公告", "2026-07-19", "/notice/1.html")
    payload = listing_html((duplicated, duplicated), page=1, total_pages=1)
    acquirer, _ = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, payload)}
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert "official_listing_item_duplicate" in result.gap_codes
    assert result.listing_closure_complete is False


def test_duplicate_raw_page_stops_without_looping() -> None:
    source = paginated_registration()
    payload = listing_html((), page=1, total_pages=3)
    acquirer, calls = listing_acquirer_for(
        {
            (source.registration_id, 1): listing_response(source, 1, payload),
            (source.registration_id, 2): listing_response(source, 2, payload),
        }
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert [call[1] for call in calls] == [1, 2]
    assert "official_listing_page_duplicate" in result.gap_codes
    assert result.listing_truncated is True


def test_listing_stops_incomplete_at_ten_page_cap() -> None:
    source = paginated_registration()
    mapping = {}
    for page in range(1, 11):
        payload = listing_html(
            (
                (
                    f"关于{FUND_NAME}第{page}页公告",
                    f"2026-07-{20 - page:02d}",
                    f"/notice/{page}.html",
                ),
            ),
            page=page,
            total_pages=50,
        )
        mapping[(source.registration_id, page)] = listing_response(source, page, payload)
    acquirer, calls = listing_acquirer_for(mapping)

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert len(calls) == 10
    assert result.listing_truncated is True
    assert "official_listing_page_cap_reached" in result.gap_codes
    assert result.page_captures[-1].terminal_state is None


def test_listing_page_and_item_limits_fail_closed() -> None:
    source = paginated_registration()
    oversized = listing_html(
        ((f"关于{FUND_NAME}普通公告", "2026-07-19", "/notice/1.html"),),
        page=1,
        total_pages=1,
        extra="x" * 1000,
    )
    byte_limited, _ = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, oversized)},
        maximum_page_bytes=100,
    )
    byte_result = byte_limited.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )
    assert "official_listing_page_byte_limit" in byte_result.gap_codes

    items = tuple(
        (f"关于{FUND_NAME}普通公告{index}", "2026-07-19", f"/notice/{index}.html")
        for index in range(3)
    )
    item_limited, _ = listing_acquirer_for(
        {
            (source.registration_id, 1): listing_response(
                source, 1, listing_html(items, page=1, total_pages=1)
            )
        },
        maximum_items=2,
    )
    item_result = item_limited.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )
    assert "official_listing_item_cap_reached" in item_result.gap_codes


@pytest.mark.parametrize(
    "failure,expected_gap",
    (
        (TimeoutError("late"), "official_listing_timeout"),
        (None, "official_listing_content_missing"),
    ),
)
def test_listing_fetch_failure_is_terminal_without_retry(
    failure: BaseException | None, expected_gap: str
) -> None:
    source = paginated_registration()
    acquirer, calls = listing_acquirer_for({(source.registration_id, 1): failure})

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert len(calls) == 1
    assert expected_gap in result.gap_codes


def test_listing_rejects_unsafe_redirect_and_login_page() -> None:
    source = paginated_registration()
    valid_payload = listing_html((), page=1, total_pages=1)
    redirected, _ = listing_acquirer_for(
        {
            (source.registration_id, 1): listing_response(
                source, 1, valid_payload, final_url="https://evil.example/notices"
            )
        }
    )
    redirected_result = redirected.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )
    assert "official_listing_redirect_rejected" in redirected_result.gap_codes

    login_payload = listing_html(
        (),
        page=1,
        total_pages=1,
        extra='<p>请登录</p><input type="password" name="password">',
    )
    login, _ = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, login_payload)}
    )
    login_result = login.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )
    assert "official_listing_login_page" in login_result.gap_codes


def test_unregistered_manager_never_calls_listing_fetch() -> None:
    acquirer, calls = listing_acquirer_for({})

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        "未注册基金管理人",
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert calls == []
    assert result.matched_registration_ids == ()
    assert result.gap_codes == ("official_source_set_unsupported",)


def test_listing_window_must_match_frozen_official_check_policy() -> None:
    acquirer, calls = listing_acquirer_for({})

    with pytest.raises(ValueError, match="window must match"):
        acquirer.collect_registered_listing(
            FUND_CODE,
            PUBLISHER,
            FUND_NAME,
            window_start=NOW - timedelta(days=179),
            window_end=NOW,
        )

    assert calls == []


def test_registered_publisher_host_mismatch_never_calls_listing_fetch() -> None:
    source = replace(
        paginated_registration(),
        identity="冒充基金管理有限公司",
        identity_aliases=(),
    )
    calls: list[object] = []

    def fetch(*args: object) -> None:
        calls.append(args)
        return None

    acquirer = OfficialListingAcquirer(fetch=fetch, registrations=(source,))
    result = acquirer.collect_registered_listing(
        FUND_CODE,
        source.identity,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert calls == []
    assert "official_listing_publisher_mismatch" in result.gap_codes


@pytest.mark.parametrize(
    "payload,expected_gap",
    (
        (
            listing_html((), page=1, total_pages=1, fund_code="654321"),
            "official_listing_fund_binding_ambiguous",
        ),
        (
            listing_html((), page=1, total_pages=1, product_name="另一只基金"),
            "official_listing_fund_binding_ambiguous",
        ),
        (
            listing_html(
                (),
                page=1,
                total_pages=1,
                extra="<p>订阅后查看</p>",
            ),
            "official_listing_paywall",
        ),
    ),
)
def test_listing_identity_and_paywall_fail_closed(
    payload: bytes, expected_gap: str
) -> None:
    source = paginated_registration()
    acquirer, calls = listing_acquirer_for(
        {(source.registration_id, 1): listing_response(source, 1, payload)}
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert len(calls) == 1
    assert expected_gap in result.gap_codes


def test_listing_reported_total_page_conflict_is_terminal() -> None:
    source = paginated_registration()
    acquirer, calls = listing_acquirer_for(
        {
            (source.registration_id, 1): listing_response(
                source, 1, listing_html((), page=1, total_pages=3)
            ),
            (source.registration_id, 2): listing_response(
                source, 2, listing_html((), page=2, total_pages=4)
            ),
        }
    )

    result = acquirer.collect_registered_listing(
        FUND_CODE,
        PUBLISHER,
        FUND_NAME,
        window_start=NOW - timedelta(days=180),
        window_end=NOW,
    )

    assert [call[1] for call in calls] == [1, 2]
    assert "official_listing_pagination_conflict" in result.gap_codes


def test_every_high_recall_signal_is_bound_by_official_policy_checksum() -> None:
    policy = OfficialCheckPolicyV1()
    required = {
        "旗下基金",
        "多只基金",
        "重大事项",
        "有关事项",
        "恢复大额赎回",
        "聘任",
        "离任",
    }

    assert required <= set(policy.candidate_lexemes)
    assert policy.candidate_detector_checksum
    assert policy.checksum()


@pytest.mark.parametrize(
    "title,classification",
    (
        ("旗下部分基金暂停大额赎回业务的公告", "ambiguous"),
        ("相关基金基金经理解聘公告", "ambiguous"),
        (f"关于{FUND_NAME}增聘基金经理公告", "candidate"),
        (f"关于{FUND_NAME}恢复赎回业务公告", "candidate"),
        (f"关于{FUND_NAME}基金合同终止公告的更正公告", "ambiguous"),
        (f"关于撤回{FUND_NAME}调整管理费率公告", "ambiguous"),
        (f"关于{FUND_NAME}重大事项公告", "ambiguous"),
        ("关于另一只基金基金合同终止公告", "ambiguous"),
        (f"关于{FUND_NAME}第二季度报告", "ordinary"),
    ),
)
def test_negative_check_title_detector_is_high_recall_and_separate_from_projection(
    title: str, classification: str
) -> None:
    assert classify_official_listing_title(title, FUND_NAME) == classification


def test_ambiguous_high_recall_title_fetches_body_but_cannot_project_an_event() -> None:
    ambiguous = row(title="旗下部分基金暂停大额赎回业务的公告")
    collector, calls = collector_for(
        {
            ambiguous.canonical_announcement_url: response(
                ambiguous, f"{FUND_NAME}现已暂停大额赎回业务。"
            )
        }
    )

    result = collector.collect((ambiguous,), context())

    assert len(calls) == 1
    assert len(result.contents) == 1
    assert result.event_candidates == ()
    assert "official_candidate_classification_ambiguous" in result.gap_codes


def test_event_signal_without_exact_product_title_is_fetched_and_remains_ambiguous() -> None:
    ambiguous = row(title="关于另一只基金基金合同终止公告")
    collector, calls = collector_for(
        {
            ambiguous.canonical_announcement_url: response(
                ambiguous, "另一只基金基金合同终止。"
            )
        }
    )

    result = collector.collect((ambiguous,), context())

    assert len(calls) == 1
    assert len(result.contents) == 1
    assert result.event_candidates == ()
    assert "official_candidate_classification_ambiguous" in result.gap_codes


def test_listing_http_fetcher_returns_transient_raw_capture_without_database_id() -> None:
    source = paginated_registration()
    payload = listing_html((), page=1, total_pages=1)
    url = source.index_url(FUND_CODE, 1)
    opener = FakeHttpOpener((FakeHttpResponse(payload, final_url=url),))
    fetch = OfficialListingHttpFetcher(
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    with patch(
        "kunjin.funds.risk.documents.socket.getaddrinfo",
        return_value=PUBLIC_DNS_RESULT,
    ):
        result = fetch(source, FUND_CODE, 1, 2 * 1024 * 1024)

    assert type(result) is OfficialListingFetchResult
    assert result.requested_url == url
    assert result.final_url == url
    assert result.payload == payload
    assert not hasattr(result, "source_document_id")
    assert len(opener.calls) == 1


def test_announcement_http_fetcher_binds_same_fund_publisher_and_returns_raw_body() -> None:
    candidate = row()
    payload = html(f"{FUND_NAME}基金合同终止。")
    opener = FakeHttpOpener(
        (FakeHttpResponse(payload, final_url=candidate.canonical_announcement_url),)
    )
    fetch = OfficialAnnouncementHttpFetcher(
        rows=(candidate,),
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    with patch(
        "kunjin.funds.risk.documents.socket.getaddrinfo",
        return_value=PUBLIC_DNS_RESULT,
    ):
        result = fetch(candidate.canonical_announcement_url, 512 * 1024)

    assert type(result) is OfficialFetchResult
    assert result.payload == payload
    assert result.retrieved_at == NOW
    assert len(opener.calls) == 1


def test_production_http_fetcher_is_not_called_for_ordinary_announcement() -> None:
    ordinary = row(title=f"关于{FUND_NAME}第二季度报告")
    opener = FakeHttpOpener(())
    fetch = OfficialAnnouncementHttpFetcher(
        rows=(ordinary,),
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    result = OfficialAnnouncementCollector(fetch).collect((ordinary,), context())

    assert opener.calls == []
    assert result.contents == ()
    assert result.event_candidates == ()


def test_announcement_http_fetcher_rejects_cross_fund_or_publisher_rows_before_io() -> None:
    candidate = row()
    other_fund = replace(candidate, fund_code="654321", announcement_row_id=12)
    with pytest.raises(ValueError, match="same fund"):
        OfficialAnnouncementHttpFetcher(
            rows=(candidate, other_fund),
            opener=FakeHttpOpener(()),
            deadline_at=NOW + timedelta(minutes=1),
            clock=lambda: NOW,
        )

    wrong_publisher = replace(candidate, publisher="冒充基金管理有限公司")
    with pytest.raises(ValueError, match="publisher"):
        OfficialAnnouncementHttpFetcher(
            rows=(wrong_publisher,),
            opener=FakeHttpOpener(()),
            deadline_at=NOW + timedelta(minutes=1),
            clock=lambda: NOW,
        )


def test_http_fetchers_reject_unregistered_redirect_without_retry() -> None:
    source = paginated_registration()
    payload = listing_html((), page=1, total_pages=1)
    opener = FakeHttpOpener(
        (FakeHttpResponse(payload, final_url="https://evil.example/notices"),)
    )
    fetch = OfficialListingHttpFetcher(
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    with (
        patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ),
        pytest.raises(OfficialDocumentError),
    ):
        fetch(source, FUND_CODE, 1, 2 * 1024 * 1024)

    assert len(opener.calls) == 1


@pytest.mark.parametrize("declared_size", ("101", None))
def test_announcement_http_fetcher_enforces_declared_and_streamed_byte_cap(
    declared_size: str | None,
) -> None:
    candidate = row()
    payload = html("x" * 200)
    opener = FakeHttpOpener(
        (
            FakeHttpResponse(
                payload,
                final_url=candidate.canonical_announcement_url,
                declared_size=declared_size,
            ),
        )
    )
    fetch = OfficialAnnouncementHttpFetcher(
        rows=(candidate,),
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    with (
        patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ),
        pytest.raises(OfficialDocumentResourceLimitError),
    ):
        fetch(candidate.canonical_announcement_url, 100)

    assert len(opener.calls) == 1


def test_announcement_http_fetcher_rejects_non_html_content_type() -> None:
    candidate = row()
    opener = FakeHttpOpener(
        (
            FakeHttpResponse(
                b"%PDF-1.7",
                final_url=candidate.canonical_announcement_url,
                content_type="application/pdf",
            ),
        )
    )
    fetch = OfficialAnnouncementHttpFetcher(
        rows=(candidate,),
        opener=opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )

    with (
        patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ),
        pytest.raises(OfficialDocumentError),
    ):
        fetch(candidate.canonical_announcement_url, 512 * 1024)


def test_http_fetcher_maps_expired_deadline_and_network_timeout_without_retry() -> None:
    candidate = row()
    expired_opener = FakeHttpOpener(())
    expired = OfficialAnnouncementHttpFetcher(
        rows=(candidate,),
        opener=expired_opener,
        deadline_at=NOW,
        clock=lambda: NOW,
    )
    with pytest.raises(TimeoutError):
        expired(candidate.canonical_announcement_url, 512 * 1024)
    assert expired_opener.calls == []

    timeout_opener = FakeHttpOpener((TimeoutError("late"),))
    timeout_fetch = OfficialAnnouncementHttpFetcher(
        rows=(candidate,),
        opener=timeout_opener,
        deadline_at=NOW + timedelta(minutes=1),
        clock=lambda: NOW,
    )
    with (
        patch(
            "kunjin.funds.risk.documents.socket.getaddrinfo",
            return_value=PUBLIC_DNS_RESULT,
        ),
        pytest.raises(TimeoutError),
    ):
        timeout_fetch(candidate.canonical_announcement_url, 512 * 1024)
    assert len(timeout_opener.calls) == 1
