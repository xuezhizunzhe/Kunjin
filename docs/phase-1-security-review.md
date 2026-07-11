# KunJin Phase-one Security Review

Date: 2026-07-11

## Authorization Boundary

- Yangjibao authorization uses its browser-plugin QR endpoints.
- The resulting token is written directly to macOS Keychain under service
  `com.kunjin.yangjibao` and account `default`.
- Token reads and writes use `/usr/bin/security` with `shell=False`.
- No Alipay credential, browser cookie, browser profile, or password is read.

## Transport and Endpoint Boundary

- The adapter rejects every base URL that is not HTTPS.
- The default host is `https://browser-plug-api.yangjibao.com`.
- Only QR creation/state, account list/summary, fund holdings, and income GET
  endpoints are allowlisted.
- Dynamic QR and account identifiers must match a strict alphanumeric pattern.
- No POST, PUT, PATCH, DELETE, account-edit, holding-edit, purchase, or redemption
  method exists.

## Signing Provenance

The request-signing algorithm and browser-plugin signing constant were derived
from the publicly inspected `Ye-Yu-Mo/yjb-api` implementation. This is a client
signature, not a user password. Interface compatibility and service terms remain
operational risks because Yangjibao does not publish a stable developer contract.

## Secret Handling

- Authorization, token, signature, secret, and QR fields are redacted before
  raw snapshots are stored.
- Logging applies a second redaction filter.
- Structured CLI responses never include request headers or tokens.
- Runtime database permissions are `0600`; runtime directories are `0700`.
- Synthetic tests assert that a fake token does not appear in portfolio output.

## QR Rendering

KunJin never sends QR content to a third-party QR service. Terminal rendering uses
the optional local `qrcode` Python package. If it is unavailable, login reports
that the optional renderer must be installed; storage and offline analytics remain
available.

## Known Risks

- The unofficial interface may change without notice.
- Token lifetime and remote revocation behavior are not documented.
- Public market and fund sources in later phases require separate provenance,
  rate-limit, licensing, and data-quality reviews.
- Personal values must not be committed to Git fixtures or issue reports.

## Public Research Sources

Formal fund NAV and sector-ranking adapters use HTTPS-only Eastmoney public
interfaces without user authentication. They send no Yangjibao token. The fund
adapter stores formal NAV separately from any personal or intraday estimate. The
sector adapter stores daily strength and breadth only; it does not claim that the
data covers valuation, earnings, persistent flows, catalysts, or crowding.
