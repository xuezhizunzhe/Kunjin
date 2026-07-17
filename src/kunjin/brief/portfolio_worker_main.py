from __future__ import annotations

import sys
from datetime import datetime, timezone

from kunjin.adapters.yangjibao import (
    AuthenticationRequiredError,
    RateLimitedError,
    RemoteResponseError,
    YangjibaoClient,
)
from kunjin.brief.portfolio_worker_protocol import (
    MAX_PORTFOLIO_ACCOUNTS,
    MAX_PORTFOLIO_POSITIONS,
    MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT,
    MAX_PORTFOLIO_REQUEST_BYTES,
    PortfolioAccount,
    PortfolioObservationPayload,
    PortfolioPosition,
    decode_portfolio_request,
    encode_portfolio_error,
    encode_portfolio_success,
)
from kunjin.decision.models import canonical_decimal
from kunjin.security.keychain import CredentialStoreError, KeychainTokenStore


def _read_request() -> bytes:
    frame = sys.stdin.buffer.read(MAX_PORTFOLIO_REQUEST_BYTES + 1)
    if len(frame) > MAX_PORTFOLIO_REQUEST_BYTES:
        raise ValueError("portfolio worker request exceeds its limit")
    return frame


def _optional_decimal(value):
    return None if value is None else canonical_decimal(value)


def _success(request):
    client = YangjibaoClient(KeychainTokenStore())
    _raw_accounts, accounts = client.list_accounts()
    if type(accounts) is not list or len(accounts) > MAX_PORTFOLIO_ACCOUNTS:
        raise ValueError("portfolio account count exceeds its limit")
    projected_accounts = []
    projected_positions = []
    for account in accounts:
        account.validate()
        projected_accounts.append(
            PortfolioAccount(
                account.source_account_id,
                account.title,
                account.observed_at.astimezone(timezone.utc),
            )
        )
        _raw_holdings, positions = client.list_holdings(
            account.source_account_id,
            observed_at=account.observed_at,
        )
        if (
            type(positions) is not list
            or len(positions) > MAX_PORTFOLIO_POSITIONS_PER_ACCOUNT
            or len(projected_positions) + len(positions) > MAX_PORTFOLIO_POSITIONS
        ):
            raise ValueError("portfolio position count exceeds its limit")
        for position in positions:
            position.validate()
            projected_positions.append(
                PortfolioPosition(
                    position.source_account_id,
                    position.fund_code,
                    position.fund_name,
                    position.share_class,
                    canonical_decimal(position.shares),
                    _optional_decimal(position.formal_nav),
                    _optional_decimal(position.estimated_nav),
                    _optional_decimal(position.observed_profit),
                    position.observed_at.astimezone(timezone.utc),
                )
            )
    retrieved_at = datetime.now(timezone.utc)
    if projected_accounts:
        retrieved_at = max(
            retrieved_at,
            *(account.observed_at for account in projected_accounts),
        )
    return encode_portfolio_success(
        request,
        PortfolioObservationPayload(
            retrieved_at,
            tuple(projected_accounts),
            tuple(projected_positions),
        ),
    )


def main() -> int:
    try:
        request = decode_portfolio_request(_read_request())
    except ValueError:
        return 2
    try:
        frame = _success(request)
    except AuthenticationRequiredError:
        frame = encode_portfolio_error(request, "authentication_required", False)
    except RateLimitedError:
        frame = encode_portfolio_error(request, "rate_limited", True)
    except (CredentialStoreError, RemoteResponseError):
        frame = encode_portfolio_error(request, "source_unavailable", False)
    except (ArithmeticError, TypeError, ValueError):
        frame = encode_portfolio_error(request, "validation_failure", False)
    except Exception:
        frame = encode_portfolio_error(request, "source_unavailable", False)
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
