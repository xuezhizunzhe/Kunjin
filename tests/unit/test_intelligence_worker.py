from __future__ import annotations

import ast
import hashlib
import http.client
import json
import socket
import time
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import kunjin.intelligence.acquisition as acquisition_module
import kunjin.intelligence.worker_main as worker_main
from kunjin.decision.budget import RequestBudget
from kunjin.decision.models import RequestMode, SourceErrorCode
from kunjin.decision.worker import (
    PUBLIC_WORKER_ENV,
    WorkerExecutionError,
    _validate_worker_target,
    _worker_environment,
)
from kunjin.intelligence.acquisition import (
    IntelligenceAcquisitionError,
    acquire_intelligence_source,
    run_intelligence_worker,
    source_binding,
)
from kunjin.intelligence.worker_protocol import (
    MAX_INTELLIGENCE_PAYLOAD_BYTES,
    MAX_INTELLIGENCE_REQUEST_BYTES,
    IntelligenceSourceKind,
    IntelligenceWorkerFailure,
    IntelligenceWorkerRedirect,
    IntelligenceWorkerRequest,
    IntelligenceWorkerResponse,
    decode_intelligence_worker_request,
    decode_intelligence_worker_result,
    encode_intelligence_worker_failure,
    encode_intelligence_worker_request,
    encode_intelligence_worker_success,
    validate_intelligence_source_url,
)

NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
REQUEST_ID = "a" * 32
GOV_URL = "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json"
STCN_LIST_URL = "https://www.stcn.com/article/list/fund.html"
STCN_DETAIL_URL = "https://www.stcn.com/article/detail/3359541.html"
EASTMONEY_URL = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m%3A90%2Bt%3A2&"
    "fields=f12%2Cf14%2Cf3%2Cf8%2Cf62%2Cf184%2Cf104%2Cf105"
)


def _request(
    source_kind: IntelligenceSourceKind = IntelligenceSourceKind.GOV_POLICY,
    url: str = GOV_URL,
    **overrides,
) -> IntelligenceWorkerRequest:
    values = {
        "source_kind": source_kind,
        "requested_url": url,
        "request_id": REQUEST_ID,
        "deadline_utc": NOW + timedelta(seconds=30),
        "maximum_bytes": MAX_INTELLIGENCE_PAYLOAD_BYTES,
    }
    values.update(overrides)
    return IntelligenceWorkerRequest(**values)


def _budget(worker_seconds: float = 2.0) -> RequestBudget:
    offset = [0.0]

    def clock() -> float:
        return time.monotonic() + offset[0]

    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=REQUEST_ID,
        monotonic=clock,
        wall_clock=lambda: NOW,
    )
    offset[0] = 88.0 - worker_seconds
    return budget


def _response(request: IntelligenceWorkerRequest) -> IntelligenceWorkerResponse:
    payload = '[{"TITLE":"test"}]'
    return IntelligenceWorkerResponse(
        requested_url=request.requested_url,
        final_url=request.requested_url,
        retrieved_at=NOW,
        content_type="application/json; charset=utf-8",
        payload_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        payload_utf8=payload,
    )


def test_protocol_round_trips_exact_canonical_frames() -> None:
    request = _request()
    frame = encode_intelligence_worker_request(request)
    assert len(frame) <= MAX_INTELLIGENCE_REQUEST_BYTES
    assert decode_intelligence_worker_request(frame) == request

    response = _response(request)
    result = decode_intelligence_worker_result(
        encode_intelligence_worker_success(request, response), request
    )
    assert result == response


def test_protocol_rejects_duplicate_keys_truncation_and_checksum_drift() -> None:
    request = _request()
    frame = encode_intelligence_worker_request(request)
    with pytest.raises(ValueError, match="canonical JSON"):
        decode_intelligence_worker_request(frame[:-1])
    duplicate = frame.replace(b'{', b'{"request_id":"' + REQUEST_ID.encode() + b'",', 1)
    with pytest.raises(ValueError, match="canonical JSON"):
        decode_intelligence_worker_request(duplicate)

    valid = encode_intelligence_worker_success(request, _response(request))
    value = json.loads(valid)
    value["payload"]["payload_sha256"] = "f" * 64
    tampered = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="checksum"):
        decode_intelligence_worker_result(tampered, request)


def test_protocol_rejects_source_content_type_impersonation() -> None:
    request = _request()
    with pytest.raises(ValueError, match="content type"):
        encode_intelligence_worker_success(
            request,
            replace(_response(request), content_type="text/html"),
        )


def test_protocol_rejects_misclassified_http_failure() -> None:
    request = _request()
    failure = IntelligenceWorkerFailure(
        requested_url=request.requested_url,
        retrieved_at=NOW,
        reason_code=SourceErrorCode.HTTP_5XX,
        retryable=True,
        http_status=429,
    )
    with pytest.raises(ValueError, match="HTTP status"):
        encode_intelligence_worker_failure(request, failure)


@pytest.mark.parametrize(
    ("kind", "url"),
    (
        (IntelligenceSourceKind.GOV_POLICY, GOV_URL),
        (IntelligenceSourceKind.STCN_FUND_LIST, STCN_LIST_URL),
        (IntelligenceSourceKind.STCN_FUND_DETAIL, STCN_DETAIL_URL),
        (IntelligenceSourceKind.EASTMONEY_MARKET, EASTMONEY_URL),
    ),
)
def test_source_urls_require_exact_https_host_path_and_query(kind, url) -> None:
    assert validate_intelligence_source_url(kind, url) == url
    for invalid in (
        url.replace("https://", "http://", 1),
        url.replace("https://", "https://user:pass@", 1),
        url + "#fragment",
        url.replace("www.gov.cn", "www.gov.cn.evil.example"),
        url.replace("www.stcn.com", "stcn.com"),
        url.replace("push2.eastmoney.com", "127.0.0.1"),
    ):
        if invalid != url:
            with pytest.raises(ValueError, match="source URL"):
                validate_intelligence_source_url(kind, invalid)


def test_source_kind_cannot_impersonate_another_preflighted_source() -> None:
    for kind, url in (
        (IntelligenceSourceKind.GOV_POLICY, STCN_LIST_URL),
        (IntelligenceSourceKind.STCN_FUND_LIST, GOV_URL),
        (IntelligenceSourceKind.STCN_FUND_DETAIL, STCN_LIST_URL),
        (IntelligenceSourceKind.EASTMONEY_MARKET, GOV_URL),
    ):
        with pytest.raises(ValueError, match="source URL"):
            encode_intelligence_worker_request(_request(kind, url))


def test_only_preflighted_source_kinds_have_attempt_bindings() -> None:
    assert source_binding(IntelligenceSourceKind.GOV_POLICY) == (
        "gov_cn_policy",
        "policy_events",
    )
    assert source_binding(IntelligenceSourceKind.STCN_FUND_LIST) == (
        "stcn_fund_news",
        "fund_media_events",
    )
    assert source_binding(IntelligenceSourceKind.STCN_FUND_DETAIL) == (
        "stcn_fund_news",
        "fund_media_events",
    )
    assert source_binding(IntelligenceSourceKind.EASTMONEY_MARKET) == (
        "eastmoney_market",
        "market_dimensions",
    )


def test_public_worker_environment_strips_private_and_transport_state(monkeypatch) -> None:
    private_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "COOKIE",
        "TOKEN",
        "AUTHORIZATION",
        "KEYCHAIN_PATH",
        "PYTHONPATH",
        "KUNJIN_DATA_DIR",
        "KUNJIN_STATE_DIR",
        "HOME",
    )
    for name in private_names:
        monkeypatch.setenv(name, "private")
    environment = _worker_environment(PUBLIC_WORKER_ENV)
    assert set(environment) <= {
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "KUNJIN_PHASE0_RUN_ID",
    }
    assert not set(private_names) & set(environment)


def test_intelligence_worker_is_the_only_new_allowed_public_target() -> None:
    _validate_worker_target("kunjin.intelligence.worker_main", PUBLIC_WORKER_ENV)
    with pytest.raises(ValueError, match="module and environment"):
        _validate_worker_target("kunjin.owner.worker_main", PUBLIC_WORKER_ENV)
    with pytest.raises(ValueError, match="module and environment"):
        _validate_worker_target("kunjin.intelligence.worker_main", "private_keychain")


class _Headers(dict):
    def get_content_type(self) -> str:
        return str(self.get("Content-Type", "")).split(";", 1)[0]


class _FakeHTTPResponse:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.headers = _Headers(
            {"Content-Type": content_type, "Content-Length": str(len(body))}
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _run_worker_main(request, opener) -> object:
    frames: list[bytes] = []
    output = SimpleNamespace(
        buffer=SimpleNamespace(
            write=lambda frame: frames.append(frame),
            flush=lambda: None,
        )
    )
    with (
        patch.object(
            worker_main,
            "_read_request",
            return_value=encode_intelligence_worker_request(request),
        ),
        patch.object(worker_main, "_build_opener", return_value=opener),
        patch.object(worker_main.sys, "stdout", output),
        patch.object(worker_main, "_utc_now", return_value=NOW),
    ):
        assert worker_main.main() == 0
    assert len(frames) == 1
    return decode_intelligence_worker_result(frames[0], request)


@pytest.mark.parametrize(
    ("error", "reason", "retryable"),
    (
        (urllib.error.URLError(socket.gaierror()), SourceErrorCode.DNS_FAILURE, True),
        (urllib.error.URLError(TimeoutError()), SourceErrorCode.NETWORK_TIMEOUT, True),
        (TimeoutError(), SourceErrorCode.NETWORK_TIMEOUT, True),
        (
            http.client.RemoteDisconnected("remote closed"),
            SourceErrorCode.TRANSIENT_NETWORK_FAILURE,
            True,
        ),
    ),
)
def test_worker_maps_dns_connect_and_read_timeouts(error, reason, retryable) -> None:
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    result = _run_worker_main(_request(), opener)
    assert result == IntelligenceWorkerFailure(
        requested_url=GOV_URL,
        retrieved_at=NOW,
        reason_code=reason,
        retryable=retryable,
        http_status=None,
    )


@pytest.mark.parametrize(
    ("status", "reason", "retryable"),
    (
        (400, SourceErrorCode.HTTP_4XX, False),
        (401, SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED, False),
        (404, SourceErrorCode.HTTP_NOT_FOUND, False),
        (410, SourceErrorCode.HTTP_GONE, False),
        (429, SourceErrorCode.HTTP_RATE_LIMITED, True),
        (500, SourceErrorCode.HTTP_5XX, True),
        (503, SourceErrorCode.HTTP_5XX, True),
    ),
)
def test_worker_maps_http_failures(status, reason, retryable) -> None:
    error = urllib.error.HTTPError(GOV_URL, status, "failure", {}, None)
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    result = _run_worker_main(_request(), opener)
    assert result.reason_code is reason
    assert result.retryable is retryable
    assert result.http_status == status


def test_worker_emits_redirect_without_following_it() -> None:
    headers = {"Location": "/article/detail/3359602.html"}
    error = urllib.error.HTTPError(STCN_DETAIL_URL, 302, "redirect", headers, None)
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    result = _run_worker_main(
        _request(IntelligenceSourceKind.STCN_FUND_DETAIL, STCN_DETAIL_URL), opener
    )
    assert result == IntelligenceWorkerRedirect(
        requested_url=STCN_DETAIL_URL,
        retrieved_at=NOW,
        location="/article/detail/3359602.html",
        http_status=302,
    )


@pytest.mark.parametrize(
    ("kind", "body", "content_type"),
    (
        (IntelligenceSourceKind.GOV_POLICY, b"not-json", "application/json"),
        (IntelligenceSourceKind.STCN_FUND_LIST, b"not-html", "text/html"),
    ),
)
def test_worker_rejects_malformed_source_payload(kind, body, content_type) -> None:
    url = GOV_URL if kind is IntelligenceSourceKind.GOV_POLICY else STCN_LIST_URL
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: _FakeHTTPResponse(body, content_type))
    result = _run_worker_main(_request(kind, url), opener)
    assert result.reason_code is SourceErrorCode.DECODE_FAILURE
    assert result.retryable is False


def test_worker_rejects_response_over_request_maximum() -> None:
    request = _request(maximum_bytes=8)
    response = _FakeHTTPResponse(b"123456789", "application/json")
    response.headers["Content-Length"] = "9"
    opener = SimpleNamespace(open=lambda *_args, **_kwargs: response)
    result = _run_worker_main(request, opener)
    assert result.reason_code is SourceErrorCode.OVERSIZED_RESPONSE


def test_parent_revalidates_redirect_and_schedules_one_new_get() -> None:
    first = _request(IntelligenceSourceKind.STCN_FUND_DETAIL, STCN_DETAIL_URL)
    redirected_url = "https://www.stcn.com/article/detail/3359602.html"
    calls = []

    def runner(request, budget):
        calls.append(request)
        if len(calls) == 1:
            return IntelligenceWorkerRedirect(
                request.requested_url,
                NOW,
                "/article/detail/3359602.html",
                302,
            )
        return replace(
            _response(request),
            content_type="text/html",
            payload_utf8="<html></html>",
            payload_sha256=hashlib.sha256(b"<html></html>").hexdigest(),
        )

    result = acquire_intelligence_source(first, _budget(), runner=runner)
    assert result.final_url == redirected_url
    assert [item.requested_url for item in calls] == [STCN_DETAIL_URL, redirected_url]


@pytest.mark.parametrize(
    "location",
    (
        "http://www.stcn.com/article/detail/3359602.html",
        "https://example.com/article/detail/3359602.html",
        "https://www.gov.cn/zhengce/zuixin/ZUIXINZHENGCE.json",
        "//127.0.0.1/private",
    ),
)
def test_parent_rejects_unsafe_or_cross_host_redirect(location) -> None:
    request = _request(IntelligenceSourceKind.STCN_FUND_DETAIL, STCN_DETAIL_URL)

    def runner(_request, _budget):
        return IntelligenceWorkerRedirect(STCN_DETAIL_URL, NOW, location, 302)

    with pytest.raises(IntelligenceAcquisitionError) as captured:
        acquire_intelligence_source(request, _budget(), runner=runner)
    assert captured.value.reason_code is SourceErrorCode.UNSAFE_REDIRECT


def test_run_intelligence_worker_reuses_shared_framed_transport() -> None:
    request = _request()
    expected = _response(request)
    with patch(
        "kunjin.intelligence.acquisition._run_framed_worker",
        return_value=expected,
    ) as runner:
        assert run_intelligence_worker(request, _budget()) is expected
    kwargs = runner.call_args.kwargs
    assert kwargs["module"] == "kunjin.intelligence.worker_main"
    assert kwargs["environment_profile"] == PUBLIC_WORKER_ENV


def test_late_output_and_cleanup_error_never_return_a_result() -> None:
    request = _request()
    with patch(
        "kunjin.intelligence.acquisition._run_framed_worker",
        side_effect=WorkerExecutionError("worker_timeout", "deadline reached"),
    ):
        with pytest.raises(WorkerExecutionError, match="deadline"):
            run_intelligence_worker(request, _budget())


def test_worker_modules_do_not_import_sqlite_or_storage() -> None:
    for module in (worker_main, acquisition_module):
        path = Path(module.__file__ or "")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert "sqlite3" not in imported
        assert not any("storage" in name or "store" in name for name in imported)
