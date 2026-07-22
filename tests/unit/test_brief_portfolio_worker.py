from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kunjin.adapters.yangjibao import AuthenticationRequiredError
from kunjin.brief.portfolio import BoundedPortfolioService, run_portfolio_worker
from kunjin.brief.portfolio_worker_protocol import (
    MAX_PORTFOLIO_ACCOUNTS,
    MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT,
    MAX_PORTFOLIO_RESPONSE_BYTES,
    SCHEMA_VERSION,
    PortfolioAccount,
    PortfolioObservationPayload,
    PortfolioPosition,
    PortfolioWorkerRequest,
    PortfolioWorkerResponse,
    decode_portfolio_request,
    decode_portfolio_response,
    encode_portfolio_error,
    encode_portfolio_request,
    encode_portfolio_success,
)
from kunjin.decision.budget import BudgetExpired, RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    RequestMode,
    SourceAttemptOutcome,
    SourceErrorCode,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.decision.worker import PRIVATE_KEYCHAIN_WORKER_ENV
from kunjin.funds.service import SourceRequestContext
from kunjin.security.keychain import CredentialStoreError
from kunjin.services.sync import PortfolioSyncService
from kunjin.storage.repository import Repository

NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
TOKEN_SENTINEL = "token-private-sentinel-4acdb7"
SHARES_SENTINEL = "73129.17"


def _request(request_id: str = "a" * 32) -> PortfolioWorkerRequest:
    return PortfolioWorkerRequest(SCHEMA_VERSION, request_id, "portfolio_observation")


def _payload() -> PortfolioObservationPayload:
    return PortfolioObservationPayload(
        retrieved_at=NOW,
        accounts=(PortfolioAccount("account-1", "学习账户", NOW),),
        positions=(
            PortfolioPosition(
                "account-1",
                "123456",
                "测试基金A",
                "A",
                SHARES_SENTINEL,
                "1.25",
                None,
                "-12.34",
                NOW,
            ),
        ),
    )


def _response(request: PortfolioWorkerRequest) -> PortfolioWorkerResponse:
    return PortfolioWorkerResponse(
        schema_version=SCHEMA_VERSION,
        request_id=request.request_id,
        operation=request.operation,
        ok=True,
        payload=_payload(),
        reason_code=None,
        retryable=None,
        message=None,
        keychain_read_count=1,
        keychain_mutation_attempt_count=0,
    )


def _valid_client_type():
    class Client:
        def __init__(self, store) -> None:
            self.store = store

        def list_accounts(self):
            assert self.store.load() == TOKEN_SENTINEL
            account = SimpleNamespace(
                source="yangjibao",
                source_account_id="account-1",
                title="learning-account",
                observed_at=NOW,
                validate=lambda: None,
            )
            return {}, [account]

        def list_holdings(self, account_id, observed_at=None):
            assert account_id == "account-1"
            assert self.store.load() == TOKEN_SENTINEL
            position = SimpleNamespace(
                source_account_id=account_id,
                fund_code="123456",
                fund_name="fixture-fund-A",
                share_class="A",
                shares=Decimal("1"),
                formal_nav=Decimal("1"),
                estimated_nav=None,
                observed_profit=None,
                observed_at=observed_at,
                validate=lambda: None,
            )
            return {}, [position]

    return Client


def _run_worker_main(monkeypatch, *, token_store, client_type) -> bytes:
    from kunjin.brief import portfolio_worker_main as worker_main

    output = io.BytesIO()
    monkeypatch.setattr(worker_main, "KeychainTokenStore", lambda: token_store)
    monkeypatch.setattr(worker_main, "YangjibaoClient", client_type)
    monkeypatch.setattr(
        worker_main,
        "_read_request",
        lambda: encode_portfolio_request(_request()),
    )
    monkeypatch.setattr(worker_main.sys, "stdout", SimpleNamespace(buffer=output))
    assert worker_main.main() == 0
    return output.getvalue()


def _context(repository: Repository, request_id: str = "a" * 32):
    ticks = [10.0]
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=request_id,
        monotonic=lambda: ticks[0],
        wall_clock=lambda: NOW,
    )
    audit = DecisionAuditStore(repository)
    run_id = audit.begin_request(budget)
    health = SourceHealthService(audit, wall_clock=lambda: NOW)
    return SourceRequestContext(run_id, budget, audit, health), ticks


def test_portfolio_request_is_exact_canonical_and_contains_no_secret() -> None:
    request = _request()

    frame = encode_portfolio_request(request)

    assert decode_portfolio_request(frame) == request
    assert json.loads(frame) == {
        "operation": "portfolio_observation",
        "request_id": "a" * 32,
        "schema_version": SCHEMA_VERSION,
    }
    assert TOKEN_SENTINEL.encode() not in frame
    with pytest.raises(ValueError):
        decode_portfolio_request(frame[:-1] + b',"token":"' + TOKEN_SENTINEL.encode() + b'"}')


def test_portfolio_response_round_trips_typed_private_observations_without_secret() -> None:
    request = _request()

    frame = encode_portfolio_success(
        request,
        _payload(),
        keychain_read_count=1,
        keychain_mutation_attempt_count=0,
    )
    response = decode_portfolio_response(frame, request)

    assert response == _response(request)
    assert SHARES_SENTINEL.encode() in frame
    assert TOKEN_SENTINEL.encode() not in frame
    assert len(frame) < MAX_PORTFOLIO_RESPONSE_BYTES


def test_portfolio_response_requires_credential_attestation() -> None:
    request = _request()

    frame = encode_portfolio_success(
        request,
        _payload(),
        keychain_read_count=1,
        keychain_mutation_attempt_count=0,
    )
    response = decode_portfolio_response(frame, request)

    assert response.keychain_read_count == 1
    assert response.keychain_mutation_attempt_count == 0


@pytest.mark.parametrize(
    "field,value",
    (
        ("keychain_read_count", True),
        ("keychain_read_count", 2),
        ("keychain_mutation_attempt_count", -1),
    ),
)
def test_portfolio_response_rejects_invalid_credential_attestation(
    field: str, value: object
) -> None:
    request = _request()
    raw = json.loads(
        encode_portfolio_error(
            request,
            "authentication_required",
            False,
            keychain_read_count=1,
            keychain_mutation_attempt_count=0,
        )
    )
    raw[field] = value

    with pytest.raises(ValueError, match="credential attestation"):
        decode_portfolio_response(
            json.dumps(raw, separators=(",", ":")).encode(), request
        )


def test_portfolio_response_rejects_missing_credential_attestation() -> None:
    request = _request()
    raw = json.loads(
        encode_portfolio_success(
            request,
            _payload(),
            keychain_read_count=1,
            keychain_mutation_attempt_count=0,
        )
    )
    del raw["keychain_read_count"]

    with pytest.raises(ValueError, match="success shape"):
        decode_portfolio_response(
            json.dumps(raw, separators=(",", ":")).encode(), request
        )


def test_portfolio_protocol_rejects_unexpected_dataclass_state() -> None:
    request = _request()
    payload = _payload()
    object.__setattr__(payload.accounts[0], "token", TOKEN_SENTINEL)

    with pytest.raises(ValueError, match="state"):
        encode_portfolio_success(
            request,
            payload,
            keychain_read_count=1,
            keychain_mutation_attempt_count=0,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value["payload"]["positions"][0].update(shares="1.00"),
        lambda value: value["payload"]["positions"][0].update(fund_code="12345x"),
        lambda value: value["payload"]["positions"][0].update(
            observed_at="2030-01-01T00:00:00+00:00"
        ),
        lambda value: value["payload"]["accounts"].append(dict(value["payload"]["accounts"][0])),
    ),
)
def test_portfolio_response_rejects_noncanonical_or_malformed_records(mutation) -> None:
    request = _request()
    value = json.loads(
        encode_portfolio_success(
            request,
            _payload(),
            keychain_read_count=1,
            keychain_mutation_attempt_count=0,
        )
    )
    mutation(value)

    with pytest.raises(ValueError):
        decode_portfolio_response(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            request,
        )


def test_private_worker_transport_uses_only_private_environment_profile() -> None:
    request = _request()
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=request.request_id,
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    expected = _response(request)

    with patch("kunjin.brief.portfolio._run_framed_worker", return_value=expected) as runner:
        assert run_portfolio_worker(request, budget) is expected

    kwargs = runner.call_args.kwargs
    assert kwargs["module"] == "kunjin.brief.portfolio_worker_main"
    assert kwargs["environment_profile"] == PRIVATE_KEYCHAIN_WORKER_ENV
    assert kwargs["max_response_bytes"] == MAX_PORTFOLIO_RESPONSE_BYTES
    assert TOKEN_SENTINEL not in repr(runner.call_args)


def test_private_worker_transport_rejects_keychain_mutation_attestation() -> None:
    request = _request()
    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=request.request_id,
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )
    unsafe = decode_portfolio_response(
        encode_portfolio_success(
            request,
            _payload(),
            keychain_read_count=1,
            keychain_mutation_attempt_count=1,
        ),
        request,
    )

    def run_unsafe_worker(_request, _budget, *, validator, **_kwargs):
        validator(unsafe, _request, _budget)
        return unsafe

    with patch("kunjin.brief.portfolio._run_framed_worker", side_effect=run_unsafe_worker):
        with pytest.raises(ValueError, match="credential attestation is unsafe"):
            run_portfolio_worker(request, budget)


def test_worker_main_calls_only_read_methods_and_never_emits_token(monkeypatch) -> None:
    calls = []

    class TokenStore:
        def load(self):
            calls.append("load")
            return TOKEN_SENTINEL

        def save(self, _value):
            raise AssertionError("worker must never mutate Keychain")

        def delete(self):
            raise AssertionError("worker must never mutate Keychain")

    class Client:
        def __init__(self, store):
            self.store = store

        def list_accounts(self):
            assert self.store.load() == TOKEN_SENTINEL
            calls.append("list_accounts")
            account = SimpleNamespace(
                source="yangjibao",
                source_account_id="account-1",
                title="学习账户",
                observed_at=NOW,
                validate=lambda: None,
            )
            return {"token": TOKEN_SENTINEL}, [account]

        def list_holdings(self, account_id, observed_at=None):
            assert self.store.load() == TOKEN_SENTINEL
            calls.append(("list_holdings", account_id))
            position = SimpleNamespace(
                source_account_id=account_id,
                fund_code="123456",
                fund_name="测试基金A",
                share_class="A",
                shares=Decimal(SHARES_SENTINEL),
                formal_nav=Decimal("1.25"),
                estimated_nav=None,
                observed_profit=Decimal("-12.34"),
                observed_at=observed_at,
                validate=lambda: None,
            )
            return {"authorization": TOKEN_SENTINEL}, [position]

    decoded_frame = _run_worker_main(
        monkeypatch,
        token_store=TokenStore(),
        client_type=Client,
    )
    decoded = decode_portfolio_response(decoded_frame, _request())
    assert decoded.ok is True
    assert decoded.keychain_read_count == 1
    assert decoded.keychain_mutation_attempt_count == 0
    assert calls == ["load", "list_accounts", ("list_holdings", "account-1")]
    assert TOKEN_SENTINEL.encode() not in decoded_frame


def test_worker_reads_keychain_once_and_attests_no_mutation(monkeypatch) -> None:
    loads = []

    class TokenStore:
        def load(self):
            loads.append("load")
            return TOKEN_SENTINEL

    frame = _run_worker_main(
        monkeypatch,
        token_store=TokenStore(),
        client_type=_valid_client_type(),
    )
    response = decode_portfolio_response(frame, _request())

    assert loads == ["load"]
    assert response.ok is True
    assert response.keychain_read_count == 1
    assert response.keychain_mutation_attempt_count == 0
    assert TOKEN_SENTINEL.encode() not in frame


def test_worker_expired_token_is_nonretryable_and_attests_one_read(monkeypatch) -> None:
    loads = []

    class TokenStore:
        def load(self):
            loads.append("load")
            return TOKEN_SENTINEL

    class ExpiredClient:
        def __init__(self, _store) -> None:
            pass

        def list_accounts(self):
            raise AuthenticationRequiredError("Yangjibao authorization expired")

    frame = _run_worker_main(
        monkeypatch,
        token_store=TokenStore(),
        client_type=ExpiredClient,
    )
    response = decode_portfolio_response(frame, _request())

    assert loads == ["load"]
    assert response.ok is False
    assert response.reason_code == "authentication_required"
    assert response.retryable is False
    assert response.keychain_read_count == 1
    assert response.keychain_mutation_attempt_count == 0
    assert TOKEN_SENTINEL.encode() not in frame


def test_worker_missing_token_is_nonretryable_and_attests_one_read(monkeypatch) -> None:
    loads = []

    class TokenStore:
        def load(self):
            loads.append("load")
            return None

    class MissingTokenClient:
        def __init__(self, store) -> None:
            self.store = store

        def list_accounts(self):
            assert self.store.load() is None
            raise AuthenticationRequiredError("Yangjibao authorization is required")

    frame = _run_worker_main(
        monkeypatch,
        token_store=TokenStore(),
        client_type=MissingTokenClient,
    )
    response = decode_portfolio_response(frame, _request())

    assert loads == ["load"]
    assert response.ok is False
    assert response.reason_code == "authentication_required"
    assert response.retryable is False
    assert response.keychain_read_count == 1
    assert response.keychain_mutation_attempt_count == 0


def test_worker_mutation_attempt_is_attested_and_rejected_by_parent(monkeypatch) -> None:
    keychain_calls = []

    class TokenStore:
        def load(self):
            keychain_calls.append("load")
            return TOKEN_SENTINEL

        def save(self, _token):
            keychain_calls.append("save")

        def delete(self):
            keychain_calls.append("delete")

    ValidClient = _valid_client_type()

    class MutationClient(ValidClient):
        def __init__(self, store) -> None:
            super().__init__(store)
            with pytest.raises(CredentialStoreError, match="read-only"):
                store.save("replacement")

    frame = _run_worker_main(
        monkeypatch,
        token_store=TokenStore(),
        client_type=MutationClient,
    )
    response = decode_portfolio_response(frame, _request())
    assert response.ok is True
    assert response.keychain_read_count == 1
    assert response.keychain_mutation_attempt_count == 1
    assert keychain_calls == ["load"]
    assert TOKEN_SENTINEL.encode() not in frame

    budget = RequestBudget.create(
        RequestMode.RAPID,
        request_id=response.request_id,
        monotonic=lambda: 1.0,
        wall_clock=lambda: NOW,
    )

    def run_unsafe_worker(_request, _budget, *, validator, **_kwargs):
        validator(response, _request, _budget)
        return response

    with patch("kunjin.brief.portfolio._run_framed_worker", side_effect=run_unsafe_worker):
        with pytest.raises(ValueError, match="credential attestation is unsafe"):
            run_portfolio_worker(_request(), budget)


def test_worker_main_sanitizes_unexpected_exception(monkeypatch) -> None:
    from kunjin.brief import portfolio_worker_main as worker_main

    output = io.BytesIO()
    monkeypatch.setattr(worker_main, "_read_request", lambda: encode_portfolio_request(_request()))
    monkeypatch.setattr(
        worker_main,
        "_success",
        lambda _request, _ledger: (_ for _ in ()).throw(RuntimeError(TOKEN_SENTINEL)),
    )
    monkeypatch.setattr(worker_main.sys, "stdout", SimpleNamespace(buffer=output))

    assert worker_main.main() == 0
    response = decode_portfolio_response(output.getvalue(), _request())
    assert response.ok is False
    assert response.reason_code == "source_unavailable"
    assert response.keychain_read_count == 0
    assert response.keychain_mutation_attempt_count == 0
    assert TOKEN_SENTINEL.encode() not in output.getvalue()


def test_worker_rejects_oversized_account_list_before_holdings_requests(monkeypatch) -> None:
    from kunjin.brief import portfolio_worker_main as worker_main

    holdings_calls = []

    class Client:
        def __init__(self, _store):
            pass

        def list_accounts(self):
            account = SimpleNamespace(
                source="yangjibao",
                source_account_id="account-1",
                title="\u5b66\u4e60\u8d26\u6237",
                observed_at=NOW,
                validate=lambda: None,
            )
            return {}, [account] * (MAX_PORTFOLIO_ACCOUNTS + 1)

        def list_holdings(self, _account_id, observed_at=None):
            holdings_calls.append(observed_at)
            return [], []

    monkeypatch.setattr(
        worker_main,
        "KeychainTokenStore",
        lambda: SimpleNamespace(load=lambda: TOKEN_SENTINEL),
    )
    monkeypatch.setattr(worker_main, "YangjibaoClient", Client)

    with pytest.raises(ValueError, match="account count"):
        worker_main._success(_request(), worker_main._CredentialLedger())
    assert holdings_calls == []


def test_worker_stops_after_oversized_account_holdings(monkeypatch) -> None:
    from kunjin.brief import portfolio_worker_main as worker_main

    holdings_calls = []

    def account(account_id: str):
        return SimpleNamespace(
            source="yangjibao",
            source_account_id=account_id,
            title="\u5b66\u4e60\u8d26\u6237",
            observed_at=NOW,
            validate=lambda: None,
        )

    class Client:
        def __init__(self, _store):
            pass

        def list_accounts(self):
            return {}, [account("account-1"), account("account-2")]

        def list_holdings(self, account_id, observed_at=None):
            holdings_calls.append((account_id, observed_at))
            return [], [object()] * (MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT + 1)

    monkeypatch.setattr(
        worker_main,
        "KeychainTokenStore",
        lambda: SimpleNamespace(load=lambda: TOKEN_SENTINEL),
    )
    monkeypatch.setattr(worker_main, "YangjibaoClient", Client)

    with pytest.raises(ValueError, match="position count"):
        worker_main._success(_request(), worker_main._CredentialLedger())
    assert holdings_calls == [("account-1", NOW)]


def test_bounded_portfolio_parent_commits_without_raw_snapshot_or_private_audit(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository)

    result = BoundedPortfolioService(
        repository,
        worker_runner=lambda request, _budget: _response(request),
    ).sync("123456", context)

    assert result.status == "success"
    assert result.position_present is True
    assert result.accounts == 1
    assert result.positions == 1
    assert result.portfolio_binding.source_state == "same_request_success"
    assert result.portfolio_binding.snapshot_complete is True
    assert result.portfolio_binding.request_id == context.budget.request_id
    assert result.portfolio_binding.request_mode is context.budget.mode
    assert result.portfolio_binding.request_started_at == context.budget.started_at
    assert result.portfolio_binding.request_deadline_at == context.budget.deadline_at
    assert result.portfolio_binding.observed_at == NOW
    assert result.portfolio_binding.positions[0].account_title == "学习账户"
    assert result.portfolio_binding.positions[0].shares == Decimal(SHARES_SENTINEL)
    stored = repository.latest_positions()
    assert stored[0].shares == Decimal(SHARES_SENTINEL)
    assert repository.latest_raw_snapshot() is None
    attempts = context.audit_store.source_attempt_history(
        "yangjibao_portfolio_observation",
        "personal_position_observation",
        "fund:123456",
    )
    assert attempts[0].attempt.outcome is SourceAttemptOutcome.SUCCESS
    assert result.source_attempt_id == attempts[0].id
    assert result.portfolio_binding.observation_version == f"source_attempt_{attempts[0].id}"
    assert SHARES_SENTINEL not in repr(result)
    assert SHARES_SENTINEL not in repr(result.portfolio_binding)
    with repository.connect() as connection:
        audit_text = " ".join(
            str(tuple(row))
            for row in connection.execute(
                "SELECT * FROM source_attempts WHERE request_run_id = ?",
                (context.request_run_id,),
            )
        )
    assert SHARES_SENTINEL not in audit_text
    assert TOKEN_SENTINEL not in audit_text


def test_authentication_required_is_nonretryable_and_does_not_replace_portfolio(
    tmp_path,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    seed_context, _ticks = _context(repository, "9" * 32)
    BoundedPortfolioService(
        repository,
        worker_runner=lambda request, _budget: _response(request),
    ).sync("123456", seed_context)
    before = repository.latest_positions()
    context, _ticks = _context(repository, "b" * 32)

    def worker(request, _budget):
        return PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            False,
            None,
            "authentication_required",
            False,
            "portfolio source error: authentication_required",
            1,
            0,
        )

    result = BoundedPortfolioService(repository, worker_runner=worker).sync("123456", context)

    assert result.status == "unavailable"
    assert result.error_code == "authentication_required"
    assert result.portfolio_binding.source_state == "unbound"
    assert result.portfolio_binding.snapshot_complete is False
    assert result.portfolio_binding.positions == ()
    assert result.position_present is None
    assert repository.latest_positions() == before
    attempt = context.audit_store.source_attempt_history(
        "yangjibao_portfolio_observation",
        "personal_position_observation",
        "fund:123456",
    )[0].attempt
    assert attempt.outcome is SourceAttemptOutcome.UNAVAILABLE
    assert attempt.error_code is SourceErrorCode.PAYWALL_OR_AUTH_REQUIRED
    assert attempt.cooldown_until is None
    assert (
        result.source_attempt_id
        == context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )[0].id
    )


def test_cancelled_worker_result_never_writes_attempt_or_portfolio(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "c" * 32)

    def worker(request, budget):
        response = _response(request)
        budget.cancel("test_cancelled")
        return response

    with pytest.raises(BudgetExpired):
        BoundedPortfolioService(repository, worker_runner=worker).sync("123456", context)

    assert repository.latest_positions() == []
    assert (
        context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )
        == ()
    )


def test_cancelled_worker_error_never_writes_attempt(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "e" * 32)

    def worker(request, budget):
        response = PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            False,
            None,
            "rate_limited",
            True,
            "portfolio source error: rate_limited",
            1,
            0,
        )
        budget.cancel("test_cancelled")
        return response

    with pytest.raises(BudgetExpired):
        BoundedPortfolioService(repository, worker_runner=worker).sync("123456", context)

    assert (
        context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )
        == ()
    )


@pytest.mark.parametrize(
    "response_factory",
    (
        lambda request: PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            True,
            _payload(),
            "source_unavailable",
            False,
            "portfolio source error: source_unavailable",
            1,
            0,
        ),
        lambda request: PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            False,
            None,
            "rate_limited",
            False,
            "portfolio source error: rate_limited",
            1,
            0,
        ),
        lambda request: PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            False,
            None,
            "source_unavailable",
            False,
            "private detail " + TOKEN_SENTINEL,
            1,
            0,
        ),
    ),
)
def test_parent_revalidates_complete_response_shape_without_writes(
    tmp_path,
    response_factory,
) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "f" * 32)

    with pytest.raises(ValueError):
        BoundedPortfolioService(
            repository,
            worker_runner=lambda request, _budget: response_factory(request),
        ).sync("123456", context)

    assert repository.latest_positions() == []
    assert (
        context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )
        == ()
    )


def test_rate_limit_cooldown_prevents_next_worker_call(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    first_context, _ticks = _context(repository, "1" * 32)

    def rate_limited(request, _budget):
        return PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            False,
            None,
            "rate_limited",
            True,
            "portfolio source error: rate_limited",
            1,
            0,
        )

    first = BoundedPortfolioService(
        repository,
        worker_runner=rate_limited,
    ).sync("123456", first_context)
    assert first.error_code == "rate_limited"

    second_context, _ticks = _context(repository, "2" * 32)
    worker_calls = []
    second = BoundedPortfolioService(
        repository,
        worker_runner=lambda _request, _budget: worker_calls.append("called"),
    ).sync("123456", second_context)

    assert worker_calls == []
    assert second.status == "skipped_cooldown"
    assert second.portfolio_binding.source_state == "unbound"
    assert second.portfolio_binding.snapshot_complete is False
    assert second.portfolio_binding.positions == ()
    assert second.position_present is None
    attempt = second_context.audit_store.source_attempt_history(
        "yangjibao_portfolio_observation",
        "personal_position_observation",
        "fund:123456",
    )[0].attempt
    assert attempt.outcome is SourceAttemptOutcome.SKIPPED_COOLDOWN
    assert attempt.error_code is SourceErrorCode.COOLDOWN_ACTIVE
    assert (
        second.source_attempt_id
        == second_context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )[0].id
    )


def test_success_binding_does_not_read_or_inherit_latest_repository_snapshot(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "6" * 32)

    with patch.object(
        repository,
        "latest_positions",
        side_effect=AssertionError("must not reverse-load a concurrent portfolio snapshot"),
    ):
        result = BoundedPortfolioService(
            repository,
            worker_runner=lambda request, _budget: _response(request),
        ).sync("123456", context)

    assert tuple(item.fund_code for item in result.portfolio_binding.positions) == ("123456",)
    assert result.portfolio_binding.positions[0].shares == Decimal(SHARES_SENTINEL)


def test_portfolio_attempt_and_snapshot_roll_back_together(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "3" * 32)

    class FailingSyncService:
        def commit_observations(self, *args, **kwargs):
            PortfolioSyncService(None, repository).commit_observations(*args, **kwargs)
            raise RuntimeError("synthetic commit failure")

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        BoundedPortfolioService(
            repository,
            worker_runner=lambda request, _budget: _response(request),
            sync_service=FailingSyncService(),
        ).sync("123456", context)

    assert repository.latest_positions() == []
    assert (
        context.audit_store.source_attempt_history(
            "yangjibao_portfolio_observation",
            "personal_position_observation",
            "fund:123456",
        )
        == ()
    )


def test_successful_empty_snapshot_removes_old_positions(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    first_context, _ticks = _context(repository, "4" * 32)
    BoundedPortfolioService(
        repository,
        worker_runner=lambda request, _budget: _response(request),
    ).sync("123456", first_context)
    assert len(repository.latest_positions()) == 1

    second_context, _ticks = _context(repository, "5" * 32)
    empty = PortfolioObservationPayload(NOW, (), ())
    result = BoundedPortfolioService(
        repository,
        worker_runner=lambda request, _budget: PortfolioWorkerResponse(
            request.schema_version,
            request.request_id,
            request.operation,
            True,
            empty,
            None,
            None,
            None,
            1,
            0,
        ),
    ).sync("123456", second_context)

    assert result.status == "success"
    assert result.position_present is False
    assert repository.latest_positions() == []


def test_parent_rejects_payload_outside_request_lifetime_without_writes(tmp_path) -> None:
    repository = Repository(tmp_path / "kunjin.db")
    repository.migrate()
    context, _ticks = _context(repository, "d" * 32)
    request = _request(context.budget.request_id)
    stale = PortfolioObservationPayload(
        retrieved_at=NOW - timedelta(days=1),
        accounts=(PortfolioAccount("account-1", "学习账户", NOW - timedelta(days=1)),),
        positions=(),
    )

    with pytest.raises(ValueError, match="lifetime"):
        BoundedPortfolioService(
            repository,
            worker_runner=lambda _request, _budget: PortfolioWorkerResponse(
                SCHEMA_VERSION,
                request.request_id,
                request.operation,
                True,
                stale,
                None,
                None,
                None,
                1,
                0,
            ),
        ).sync("123456", context)

    assert repository.latest_positions() == []
