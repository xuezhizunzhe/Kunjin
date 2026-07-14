from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

_SECRET_KEYS = (
    "authorization",
    "token",
    "request-sign",
    "secret",
    "qr",
    "qr_url",
    "order_id",
    "card_number",
    "phone",
    "managed_path",
    "managed_artifact_path",
    "artifact_path",
    "local_path",
    "raw_body",
    "raw_response_body",
    "response_body",
    "parser_exception",
    "parser_exception_chain",
    "exception_chain",
    "embedded_file",
    "embedded_files",
    "embedded_file_metadata",
    "embedded_metadata",
    "monthly_net_income",
    "monthly_essential_expenses",
    "monthly_required_debt_service",
    "monthly_investment_ceiling",
    "minimum_operating_cash",
    "minimum_monthly_cash_buffer",
    "immediately_available_cash",
    "cash_like_assets",
    "emergency_reserve",
    "low_risk_fixed_income_assets",
    "manual_equity_fund_assets",
    "manual_bond_fund_assets",
    "manual_sector_fund_assets",
    "other_volatile_assets",
    "maximum_tolerable_loss",
    "maximum_tolerable_drawdown",
    "debt_principal",
    "outstanding_principal",
    "effective_annual_rate",
    "monthly_payment",
    "goal_amount",
    "target_amount",
    "obligation_amount",
    "amount_already_reserved",
    "profile_key",
    "encryption_key",
    "keychain_secret",
    "nonce",
    "ciphertext",
    "encrypted_payload",
    "keyed_fingerprint",
    "keyed_payload_fingerprint",
    "verified_emergency_reserve",
    "required_emergency_reserve",
    "emergency_reserve_shortfall",
    "required_monthly_obligation_saving",
    "required_monthly_goal_saving",
    "monthly_safety_residual",
    "safe_monthly_ceiling",
    "encrypted_amount_results",
    "total_financial_assets",
    "liquid_protection_assets",
    "protected_short_term_assigned",
    "protected_liquid_claims",
    "investable_stock_assets",
    "monthly_discretionary_allocation_ceiling",
    "confirmed_monthly_saving",
    "zero_return_funding",
    "funding_gap",
    "assigned_amount",
    "weighted_equity_contribution",
    "weighted_horizon_numerator",
    "stress_loss_amount",
    "fixed_income_stress_loss_amount",
    "equity_stress_loss_amount",
    "loss_amount_equity_ceiling_amount",
    "goal_funding_details",
    "obligation_funding_details",
    "assigned_sleeves",
    "allocation_exact_result",
    "exact_result_payload",
    "encrypted_exact_result",
    "allocation_nonce",
    "allocation_ciphertext",
    "allocation_keyed_fingerprint",
    "input_fingerprint",
    "profile_keyed_fingerprint",
    "suitability_input_fingerprint",
    "expected_profile_fingerprint",
    "expected_suitability_input_fingerprint",
    "encrypted_keyed_fingerprint",
)
_SECRET_KEY_SET = frozenset(key.casefold() for key in _SECRET_KEYS)
_SECRET_TYPE_NAMES = frozenset(
    {
        "AllocationCapitalInputs",
        "AllocationExecution",
        "AllocationExactResult",
        "AllocationInputs",
        "AllocationResult",
        "AssignedSleeveDetail",
        "EncryptedAllocationAssessment",
        "GoalFundingDetail",
        "ObligationFundingDetail",
    }
)
_SECRET_REPR_START_PATTERN = re.compile(
    rf"\b(?:{'|'.join(re.escape(name) for name in sorted(_SECRET_TYPE_NAMES))})\("
)
_EXACT_PAYLOAD_MARKERS = frozenset(
    {
        "aggregate_inputs",
        "assigned_sleeves",
        "goal_funding_details",
        "obligation_funding_details",
        "total_financial_assets",
    }
)
_JSON_DECODER = json.JSONDecoder()
_SECRET_PATTERN = re.compile(
    rf"(?i)(?<![\w-])(?P<key_quote>[\"']?)"
    rf"(?P<key>{'|'.join(re.escape(key) for key in _SECRET_KEYS)})"
    r"(?P=key_quote)(?P<separator>\s*[:=]\s*)"
    r"(?:(?P<decimal_value>Decimal\s*\(\s*(?:\"[^\"]*\"|'[^']*')\s*\))|"
    r"\"(?P<double_value>[^\"]*)\"|"
    r"'(?P<single_value>[^']*)'|"
    r"(?P<bare_value>[^\s,;}\]\"']+))"
)


def redact_secrets(value: Any) -> Any:
    return _redact_value(value)


def _redact_value(value: Any) -> Any:
    if type(value).__name__ in _SECRET_TYPE_NAMES:
        return "[REDACTED]"
    if isinstance(value, (bytes, bytearray)):
        try:
            decoded = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return value
        redacted = _redact_embedded_exact_payloads(decoded)
        if redacted == decoded:
            return value
        encoded = redacted.encode("utf-8")
        return bytearray(encoded) if isinstance(value, bytearray) else encoded
    if isinstance(value, dict):
        return {
            key: (
                "[REDACTED]"
                if isinstance(key, str) and key.casefold() in _SECRET_KEY_SET
                else _redact_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, set):
        return {_redact_value(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_redact_value(item) for item in value)
    if not isinstance(value, str):
        return value

    value = _redact_embedded_exact_payloads(value)
    value = _redact_secret_reprs(value)

    def replacement(match: re.Match) -> str:
        value_quote = ""
        if match.group("double_value") is not None:
            value_quote = '"'
        elif match.group("single_value") is not None:
            value_quote = "'"
        key_quote = match.group("key_quote")
        separator = match.group("separator") if key_quote else "="
        return (
            f"{key_quote}{match.group('key')}{key_quote}"
            f"{separator}{value_quote}[REDACTED]{value_quote}"
        )

    return _SECRET_PATTERN.sub(replacement, value)


def _is_exact_payload(value: object) -> bool:
    return isinstance(value, dict) and _EXACT_PAYLOAD_MARKERS.issubset(value)


def _redact_embedded_exact_payloads(value: str) -> str:
    fragments = []
    retained_from = 0
    search_from = 0
    redacted = False
    while True:
        start = value.find("{", search_from)
        if start < 0:
            break
        try:
            payload, consumed = _JSON_DECODER.raw_decode(value[start:])
        except json.JSONDecodeError:
            search_from = start + 1
            continue
        end = start + consumed
        if _is_exact_payload(payload):
            fragments.extend((value[retained_from:start], "[REDACTED]"))
            retained_from = end
            search_from = end
            redacted = True
        else:
            search_from = start + 1

    result = "".join(fragments) + value[retained_from:] if redacted else value
    if all(f'"{marker}"' in result for marker in _EXACT_PAYLOAD_MARKERS):
        return "[REDACTED]"
    return result


def _redact_secret_reprs(value: str) -> str:
    fragments = []
    retained_from = 0
    search_from = 0
    while True:
        match = _SECRET_REPR_START_PATTERN.search(value, search_from)
        if match is None:
            break
        end = _balanced_repr_end(value, match.end() - 1)
        fragments.extend((value[retained_from : match.start()], "[REDACTED]"))
        if end is None:
            return "".join(fragments)
        retained_from = end
        search_from = end
    if not fragments:
        return value
    return "".join(fragments) + value[retained_from:]


def _balanced_repr_end(value: str, opening_parenthesis: int) -> Optional[int]:
    depth = 0
    quote = None
    escaped = False
    for index in range(opening_parenthesis, len(value)):
        character = value[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.args = redact_secrets(record.args)
        record.msg = redact_secrets(str(record.getMessage()))
        record.args = ()
        for key in tuple(record.__dict__):
            if key in {"msg", "args"}:
                continue
            if key.casefold() in _SECRET_KEY_SET:
                setattr(record, key, "[REDACTED]")
            else:
                setattr(record, key, redact_secrets(getattr(record, key)))
        return True


def configure_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("kunjin")
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    return logger
