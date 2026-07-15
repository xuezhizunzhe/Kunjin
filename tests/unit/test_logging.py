import json
import logging
import unittest
from collections import UserDict
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from kunjin.allocation.crypto import EncryptedAllocationAssessment
from kunjin.allocation.engine import AllocationCapitalInputs, AllocationInputs
from kunjin.allocation.models import (
    AllocationResult,
    AllocationSafeSummary,
    AllocationStatus,
)
from kunjin.allocation.serialization import encode_exact_result
from kunjin.allocation.service import AllocationExecution
from kunjin.logging import SecretRedactionFilter, redact_secrets
from kunjin.suitability.models import AssessmentAmounts
from tests.unit.test_allocation_serialization_crypto import exact_result


class LoggingTest(unittest.TestCase):
    def test_redact_secrets_removes_auth_values(self) -> None:
        value = "Authorization: abc token=secret Request-Sign: deadbeef qr_url=https://x/y"
        redacted = redact_secrets(value)

        for secret in ("abc", "secret", "deadbeef", "https://x/y"):
            self.assertNotIn(secret, redacted)

    def test_filter_redacts_formatted_message(self) -> None:
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "token=%s", ("hidden",), None)
        SecretRedactionFilter().filter(record)

        self.assertEqual(record.getMessage(), "token=[REDACTED]")

    def test_redact_secrets_removes_ledger_sensitive_values(self) -> None:
        value = (
            "order_id=202607041234 card_number:6222021234567890 "
            "phone=13800138000 managed_path=/Users/person/private/import.jpg"
        )

        redacted = redact_secrets(value)

        for secret in (
            "202607041234",
            "6222021234567890",
            "13800138000",
            "/Users/person/private/import.jpg",
        ):
            self.assertNotIn(secret, redacted)
        for key in ("order_id", "card_number", "phone", "managed_path"):
            self.assertIn(f"{key}=[REDACTED]", redacted)

    def test_redact_secrets_removes_profile_financial_and_encryption_values(self) -> None:
        fields = {
            "monthly_net_income": "12001",
            "monthly_essential_expenses": "5001",
            "monthly_required_debt_service": "1501",
            "monthly_investment_ceiling": "1001",
            "minimum_operating_cash": "3001",
            "minimum_monthly_cash_buffer": "1002",
            "immediately_available_cash": "50001",
            "cash_like_assets": "10001",
            "emergency_reserve": "40001",
            "low_risk_fixed_income_assets": "5002",
            "manual_equity_fund_assets": "101",
            "manual_bond_fund_assets": "102",
            "manual_sector_fund_assets": "103",
            "other_volatile_assets": "104",
            "maximum_tolerable_loss": "20001",
            "maximum_tolerable_drawdown": "0.201",
            "debt_principal": "500001",
            "outstanding_principal": "500002",
            "effective_annual_rate": "0.0351",
            "monthly_payment": "1502",
            "goal_amount": "200001",
            "target_amount": "200002",
            "obligation_amount": "10002",
            "amount_already_reserved": "3002",
            "profile_key": "profile-secret",
            "encryption_key": "encryption-secret",
            "keychain_secret": "keychain-secret",
            "nonce": "nonce-secret",
            "ciphertext": "ciphertext-secret",
            "encrypted_payload": "payload-secret",
            "keyed_payload_fingerprint": "fingerprint-secret",
        }
        value = " ".join(f"{key}={secret}" for key, secret in fields.items())

        redacted = redact_secrets(value)

        for key, secret in fields.items():
            self.assertNotIn(secret, redacted)
            self.assertIn(f"{key}=[REDACTED]", redacted)

    def test_redaction_preserves_fund_codes_nav_values_and_dates(self) -> None:
        value = "fund_code=519755 nav=1.7467 nav_date=2026-07-12"

        self.assertEqual(redact_secrets(value), value)

    def test_redact_secrets_removes_phase_b_derived_and_encrypted_values(self) -> None:
        fields = {
            "verified_emergency_reserve": "91001.11",
            "required_emergency_reserve": "91002.22",
            "emergency_reserve_shortfall": "91003.33",
            "required_monthly_obligation_saving": "91004.44",
            "required_monthly_goal_saving": "91005.55",
            "monthly_safety_residual": "91006.66",
            "safe_monthly_ceiling": "91007.77",
            "encrypted_amount_results": "phase-b-ciphertext-sentinel",
        }
        for key, secret in fields.items():
            with self.subTest(key=key, format="bare"):
                self.assertEqual(
                    redact_secrets(f"{key}={secret}"),
                    f"{key}=[REDACTED]",
                )
            with self.subTest(key=key, format="json"):
                self.assertEqual(
                    redact_secrets(json.dumps({key: secret})),
                    json.dumps({key: "[REDACTED]"}),
                )
            with self.subTest(key=key, format="python_dict"):
                self.assertEqual(
                    redact_secrets(repr({key: secret})),
                    repr({key: "[REDACTED]"}),
                )

    def test_filter_redacts_phase_b_structured_fields(self) -> None:
        fields = {
            "verified_emergency_reserve": "92001.11",
            "required_emergency_reserve": "92002.22",
            "emergency_reserve_shortfall": "92003.33",
            "required_monthly_obligation_saving": "92004.44",
            "required_monthly_goal_saving": "92005.55",
            "monthly_safety_residual": "92006.66",
            "safe_monthly_ceiling": "92007.77",
            "encrypted_amount_results": "structured-ciphertext-sentinel",
        }
        record = logging.LogRecord(
            "x",
            logging.INFO,
            __file__,
            1,
            "assessment complete",
            (),
            None,
        )
        for key, secret in fields.items():
            setattr(record, key, secret)
        record.status = "blocked"
        record.amount = "diagnostic"
        record.goal = "count-only"
        record.debt = "type-only"

        SecretRedactionFilter().filter(record)

        for key in fields:
            self.assertEqual(getattr(record, key), "[REDACTED]")
        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.amount, "diagnostic")
        self.assertEqual(record.goal, "count-only")
        self.assertEqual(record.debt, "type-only")

    def test_redact_secrets_consumes_complete_phase_b_decimal_repr_values(self) -> None:
        fields = (
            "verified_emergency_reserve",
            "required_emergency_reserve",
            "emergency_reserve_shortfall",
            "required_monthly_obligation_saving",
            "required_monthly_goal_saving",
            "monthly_safety_residual",
            "safe_monthly_ceiling",
            "encrypted_amount_results",
        )
        for index, key in enumerate(fields, start=1):
            sentinel = f"9300{index}.0{index}"
            for quote in ("'", '"'):
                decimal_repr = f"Decimal( {quote}{sentinel}{quote} )"
                with self.subTest(key=key, quote=quote, format="bare_decimal"):
                    self.assertEqual(
                        redact_secrets(f"{key}={decimal_repr}"),
                        f"{key}=[REDACTED]",
                    )
                with self.subTest(key=key, quote=quote, format="dict_decimal"):
                    rendered = f"{{'{key}': {decimal_repr}}}"
                    self.assertEqual(
                        redact_secrets(rendered),
                        f"{{'{key}': [REDACTED]}}",
                    )

    def test_assessment_amounts_decimal_repr_is_redacted_in_exception_and_log(self) -> None:
        sentinels = tuple(Decimal(f"9400{index}.0{index}") for index in range(1, 8))
        amounts = AssessmentAmounts(*sentinels)
        message = f"assessment failed: {amounts!r}"
        error = RuntimeError(message)
        record = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            1,
            "assessment failed: %r",
            (amounts,),
            None,
        )

        redacted_exception = redact_secrets(str(error))
        SecretRedactionFilter().filter(record)

        for sentinel in sentinels:
            self.assertNotIn(str(sentinel), redacted_exception)
            self.assertNotIn(str(sentinel), record.getMessage())
        self.assertNotIn("Decimal(", redacted_exception)
        self.assertNotIn("Decimal(", record.getMessage())
        self.assertEqual(redacted_exception.count("[REDACTED]"), 7)
        self.assertEqual(record.getMessage().count("[REDACTED]"), 7)

    def test_phase_b_sentinels_do_not_survive_exception_or_log_redaction(self) -> None:
        sentinel = "919191.91"
        message = f"safe_monthly_ceiling={sentinel}"
        error = RuntimeError(message)
        record = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            1,
            "assessment failed: %s",
            (error,),
            None,
        )

        redacted_exception = redact_secrets(str(error))
        SecretRedactionFilter().filter(record)

        self.assertNotIn(sentinel, redacted_exception)
        self.assertNotIn(sentinel, record.getMessage())
        self.assertEqual(redacted_exception, "safe_monthly_ceiling=[REDACTED]")
        self.assertEqual(
            record.getMessage(),
            "assessment failed: safe_monthly_ceiling=[REDACTED]",
        )

    def test_redaction_keeps_generic_diagnostic_fields(self) -> None:
        value = "status=blocked amount=unknown goal=short_term debt=unsupported"

        self.assertEqual(redact_secrets(value), value)

    def test_allocation_amount_keys_are_redacted_recursively(self) -> None:
        sentinels = {
            "total_financial_assets": Decimal("811001.01"),
            "liquid_protection_assets": Decimal("811002.02"),
            "protected_short_term_assigned": Decimal("811003.03"),
            "protected_liquid_claims": Decimal("811004.04"),
            "investable_stock_assets": Decimal("811005.05"),
            "monthly_discretionary_allocation_ceiling": Decimal("811006.06"),
            "target_amount": Decimal("811007.07"),
            "amount_already_reserved": Decimal("811008.08"),
            "confirmed_monthly_saving": Decimal("811009.09"),
            "zero_return_funding": Decimal("811010.10"),
            "funding_gap": Decimal("811011.11"),
            "assigned_amount": Decimal("811012.12"),
            "weighted_equity_contribution": Decimal("811013.13"),
            "weighted_horizon_numerator": Decimal("811014.14"),
            "stress_loss_amount": Decimal("811015.15"),
            "fixed_income_stress_loss_amount": Decimal("811016.16"),
            "equity_stress_loss_amount": Decimal("811017.17"),
            "loss_amount_equity_ceiling_amount": Decimal("811018.18"),
        }
        value = {
            "goal_funding_details": [{"target_amount": sentinels["target_amount"]}],
            "obligation_funding_details": {"funding_gap": sentinels["funding_gap"]},
            "nested": sentinels,
        }

        redacted = redact_secrets(value)
        rendered = json.dumps(redacted, default=str)

        for sentinel in sentinels.values():
            self.assertNotIn(str(sentinel), rendered)
        self.assertEqual(redacted["goal_funding_details"], "[REDACTED]")
        self.assertEqual(redacted["obligation_funding_details"], "[REDACTED]")
        for key in sentinels:
            self.assertEqual(redacted["nested"][key], "[REDACTED]")

    def test_allocation_encryption_metadata_is_redacted_in_structured_logs(self) -> None:
        fields = {
            "allocation_exact_result": {"investable_stock_assets": Decimal("812001.01")},
            "exact_result_payload": b"phase-c-exact-payload-sentinel",
            "encrypted_exact_result": "phase-c-encrypted-result-sentinel",
            "allocation_nonce": "phase-c-nonce-sentinel",
            "allocation_ciphertext": "phase-c-ciphertext-sentinel",
            "allocation_keyed_fingerprint": "phase-c-fingerprint-sentinel",
        }
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "allocation complete", (), None)
        for key, secret in fields.items():
            setattr(record, key, secret)
        record.status = "range_available"
        record.maximum_equity_weight = "0.37"
        record.context = {"nested": {"investable_stock_assets": Decimal("812002.02")}}

        SecretRedactionFilter().filter(record)

        for key in fields:
            self.assertEqual(getattr(record, key), "[REDACTED]")
        self.assertEqual(record.status, "range_available")
        self.assertEqual(record.maximum_equity_weight, "0.37")
        self.assertEqual(
            record.context,
            {"nested": {"investable_stock_assets": "[REDACTED]"}},
        )

    def test_allocation_exact_result_object_is_redacted_before_log_rendering(self) -> None:
        value = exact_result()
        sentinel_amount = "100.00"
        sentinel_names = ("near-purpose", "later-purpose", "known-obligation")

        record = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            1,
            "allocation failed: %r",
            (value,),
            None,
        )
        error = RuntimeError(f"allocation failed: {value!r}")

        SecretRedactionFilter().filter(record)
        redacted_error = redact_secrets(str(error))

        self.assertEqual(record.getMessage(), "allocation failed: '[REDACTED]'")
        self.assertNotIn(sentinel_amount, record.getMessage())
        self.assertNotIn(sentinel_amount, redacted_error)
        for sentinel_name in sentinel_names:
            self.assertNotIn(sentinel_name, record.getMessage())
            self.assertNotIn(sentinel_name, redacted_error)

    def test_secret_repr_redaction_preserves_safe_parenthesized_suffix(self) -> None:
        value = f"exact={exact_result()!r} safe-suffix=(keep-this) done"

        redacted = redact_secrets(value)

        self.assertEqual(redacted, "exact=[REDACTED] safe-suffix=(keep-this) done")

    def test_secret_repr_redaction_preserves_text_between_two_objects(self) -> None:
        exact = exact_result()
        value = (
            f"first={exact.goal_funding_details[0]!r} "
            f"SAFE-MIDDLE=(keep) second={exact.obligation_funding_details[0]!r}"
        )

        redacted = redact_secrets(value)

        self.assertEqual(
            redacted,
            "first=[REDACTED] SAFE-MIDDLE=(keep) second=[REDACTED]",
        )

    def test_canonical_allocation_payload_is_redacted_as_text_and_bytes(self) -> None:
        payload = encode_exact_result(exact_result())
        text_payload = payload.decode("utf-8")

        for value in (payload, text_payload, f"allocation failed: {text_payload}"):
            with self.subTest(value_type=type(value).__name__):
                rendered = str(redact_secrets(value))
                self.assertNotIn("near-purpose", rendered)
                self.assertNotIn("known-obligation", rendered)
                self.assertNotIn('"amount":"100.00"', rendered)

    def test_canonical_payload_is_redacted_among_other_json_objects(self) -> None:
        payload = encode_exact_result(exact_result()).decode("utf-8")
        cases = (
            f'prefix={{"safe":"before"}} exact={payload} suffix={{"code":"ok"}}',
            f'{payload} trailing={{"safe":"after"}}',
            f'{{"safe":1}} {payload} {{"safe":2}} {payload}',
        )

        for value in cases:
            with self.subTest(value=value[:40]):
                rendered = redact_secrets(value)
                self.assertNotIn("near-purpose", rendered)
                self.assertNotIn("known-obligation", rendered)
                self.assertNotIn('"amount":"100.00"', rendered)
                self.assertIn("[REDACTED]", rendered)
        self.assertIn('{"safe":"before"}', redact_secrets(cases[0]))
        self.assertIn('{"code":"ok"}', redact_secrets(cases[0]))
        self.assertIn('{"safe":"after"}', redact_secrets(cases[1]))

    def test_prefixed_exact_payload_bytes_are_redacted_in_structured_log_context(self) -> None:
        payload = encode_exact_result(exact_result())
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "allocation payload captured", (), None
        )
        record.context = {"blob": b"safe-prefix:" + payload + b":safe-suffix"}

        SecretRedactionFilter().filter(record)

        rendered = str(record.context["blob"])
        self.assertIn("safe-prefix", rendered)
        self.assertIn("safe-suffix", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("near-purpose", rendered)
        self.assertNotIn("known-obligation", rendered)
        self.assertNotIn('"amount":"100.00"', rendered)

    def test_malformed_exact_payload_with_all_markers_fails_closed(self) -> None:
        payload = encode_exact_result(exact_result()).decode("utf-8")[:-1]

        redacted = redact_secrets(f"safe-prefix {payload}")

        self.assertEqual(redacted, "[REDACTED]")

    def test_actual_allocation_wrappers_and_fingerprints_are_redacted(self) -> None:
        exact = exact_result()
        encrypted = EncryptedAllocationAssessment(
            algorithm="AES-256-GCM",
            key_version="v1",
            nonce="allocation-nonce-sentinel",
            ciphertext="allocation-ciphertext-sentinel",
            keyed_fingerprint="allocation-fingerprint-sentinel",
        )
        result = AllocationResult(
            status=AllocationStatus.RANGE_AVAILABLE,
            capability="research_only",
            blocks=(),
            binding_constraints=(),
            profile_conflicts=(),
            safe_summary=AllocationSafeSummary(0, 0, 0, 0, 0, ()),
            permitted_region=None,
            exact=exact,
        )
        capital = AllocationCapitalInputs(
            assessment_date=exact.assessment_date,
            total_financial_assets=exact.total_financial_assets,
            liquid_protection_assets=exact.liquid_protection_assets,
            verified_emergency_reserve=exact.verified_emergency_reserve,
            minimum_operating_cash=exact.minimum_operating_cash,
            protected_short_term_assigned=exact.protected_short_term_assigned,
            protected_liquid_claims=exact.protected_liquid_claims,
            investable_stock_assets=exact.investable_stock_assets,
            monthly_discretionary_allocation_ceiling=(
                exact.monthly_discretionary_allocation_ceiling
            ),
            maximum_tolerable_loss=exact.maximum_tolerable_loss,
            maximum_tolerable_drawdown=exact.maximum_tolerable_drawdown,
            residual_horizon_date=exact.residual_horizon_date,
            goal_funding_details=exact.goal_funding_details,
            obligation_funding_details=exact.obligation_funding_details,
            assigned_sleeves=exact.assigned_sleeves,
        )
        inputs = AllocationInputs((), (), (), capital)
        execution = AllocationExecution(
            result=result,
            assessment_id=1,
            profile_version_id=1,
            profile_version=1,
            suitability_assessment_id=1,
            policy_version="1",
            assessed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            valid_until=datetime(2026, 7, 13, tzinfo=timezone.utc),
            freshness="fresh",
        )
        wrappers = (
            encrypted,
            exact.goal_funding_details[0],
            exact.obligation_funding_details[0],
            result,
            execution,
            inputs,
            capital,
        )
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "allocation wrappers: %r", (wrappers,), None
        )
        record.context = {
            "encrypted": encrypted,
            "wrappers": wrappers,
            "input_fingerprint": "input-fingerprint-sentinel",
            "profile_keyed_fingerprint": "profile-fingerprint-sentinel",
            "suitability_input_fingerprint": "suitability-fingerprint-sentinel",
        }

        SecretRedactionFilter().filter(record)

        rendered = record.getMessage()
        for sentinel in (
            "allocation-nonce-sentinel",
            "allocation-ciphertext-sentinel",
            "allocation-fingerprint-sentinel",
            "input-fingerprint-sentinel",
            "profile-fingerprint-sentinel",
            "suitability-fingerprint-sentinel",
        ):
            self.assertNotIn(sentinel, rendered)
            self.assertNotIn(sentinel, json.dumps(record.context))
        self.assertEqual(record.context["encrypted"], "[REDACTED]")
        self.assertEqual(record.context["wrappers"], ("[REDACTED]",) * len(wrappers))

    def test_aggregate_inputs_preserve_safe_percentages_only(self) -> None:
        value = {
            "aggregate_inputs": {
                "weighted_horizon_numerator": Decimal("814001.01"),
                "weighted_horizon_equity_ceiling": "0.47",
                "fixed_income_stress_loss": "0.10",
                "equity_stress_loss": "0.50",
            }
        }

        redacted = redact_secrets(value)

        record = logging.LogRecord(
            "x",
            logging.INFO,
            __file__,
            1,
            "aggregate: %r",
            (exact_result().aggregate_inputs,),
            None,
        )
        SecretRedactionFilter().filter(record)

        self.assertEqual(
            redacted,
            {
                "aggregate_inputs": {
                    "weighted_horizon_numerator": "[REDACTED]",
                    "weighted_horizon_equity_ceiling": "0.47",
                    "fixed_income_stress_loss": "0.10",
                    "equity_stress_loss": "0.50",
                }
            },
        )
        self.assertNotIn("380.0000", record.getMessage())
        self.assertIn("weighted_horizon_equity_ceiling=Decimal('0.47')", record.getMessage())
        self.assertIn("equity_stress_loss=Decimal('0.50')", record.getMessage())

    def test_d1_internal_document_and_parser_fields_are_redacted(self) -> None:
        value = {
            "managed_artifact_path": "/Users/private/fund-documents/a.pdf",
            "artifact_path": "/private/tmp/a.pdf",
            "local_path": "/Users/private/source.pdf",
            "raw_body": "raw-body-sentinel",
            "raw_response_body": "raw-response-sentinel",
            "response_body": "response-sentinel",
            "parser_exception": "parser-exception-sentinel",
            "parser_exception_chain": "parser-chain-sentinel",
            "embedded_file_metadata": {"name": "secret-attachment.bin"},
        }

        redacted = redact_secrets(value)
        rendered = json.dumps(redacted, ensure_ascii=False)

        for sentinel in (
            "/Users/private/fund-documents/a.pdf",
            "/private/tmp/a.pdf",
            "/Users/private/source.pdf",
            "raw-body-sentinel",
            "raw-response-sentinel",
            "response-sentinel",
            "parser-exception-sentinel",
            "parser-chain-sentinel",
            "secret-attachment.bin",
        ):
            self.assertNotIn(sentinel, rendered)

    def test_d1_selection_internal_fields_are_redacted_from_structured_logs(self) -> None:
        value = {
            "fund_code": "519755",
            "status": "partial",
            "selection_checksum": "a" * 64,
            "selection": {
                "candidate_fingerprint": "candidate-single-sentinel",
                "candidate_fingerprints": (
                    "candidate-one-sentinel",
                    "candidate-two-sentinel",
                ),
                "selected_fingerprint": "selected-candidate-sentinel",
                "unselected_urls": ("https://private.invalid/unselected-sentinel",),
                "raw_selection_json": "raw-selection-json-sentinel",
                "raw_selection_manifest_json": "raw-selection-manifest-sentinel",
                "selection_manifest_json": "selection-manifest-sentinel",
                "selection_canonical_json": "selection-canonical-sentinel",
                "input_manifest_json": "classification-manifest-sentinel",
                "normalized_html": "<html>normalized-html-sentinel</html>",
                "database_path": "/private/tmp/database-path-sentinel.db",
                "exception_text": "traceback exception-text-sentinel",
            },
            "selection_record": {
                "canonical_json": "selection-record-canonical-sentinel",
                "selection_checksum": "b" * 64,
                "created_at": "2026-07-15T00:00:00+00:00",
            },
        }

        redacted = redact_secrets(value)
        rendered = json.dumps(redacted, ensure_ascii=False)

        for sentinel in (
            "candidate-single-sentinel",
            "candidate-one-sentinel",
            "candidate-two-sentinel",
            "selected-candidate-sentinel",
            "unselected-sentinel",
            "raw-selection-json-sentinel",
            "raw-selection-manifest-sentinel",
            "selection-manifest-sentinel",
            "selection-canonical-sentinel",
            "classification-manifest-sentinel",
            "normalized-html-sentinel",
            "database-path-sentinel",
            "exception-text-sentinel",
            "selection-record-canonical-sentinel",
        ):
            self.assertNotIn(sentinel, rendered)
        self.assertEqual(redacted["fund_code"], "519755")
        self.assertEqual(redacted["status"], "partial")
        self.assertEqual(redacted["selection_checksum"], "a" * 64)
        self.assertEqual(redacted["selection_record"]["selection_checksum"], "b" * 64)
        self.assertEqual(
            redacted["selection_record"]["created_at"],
            "2026-07-15T00:00:00+00:00",
        )

    def test_d1_selection_internal_fields_are_redacted_from_embedded_text(self) -> None:
        payload = {
            "fund_code": "519755",
            "status": "partial",
            "candidate_fingerprints": [
                "json-candidate-one-sentinel",
                "json-candidate-two-sentinel",
            ],
            "unselected_urls": ["https://private.invalid/json-unselected-sentinel"],
            "raw_selection_json": "json-raw-selection-sentinel",
            "normalized_html": "<html>json-html-sentinel</html>",
            "database_path": "/private/tmp/json-database-sentinel.db",
            "exception_text": "traceback json-exception-sentinel",
            "selection_record": {
                "canonical_json": "json-selection-canonical-sentinel",
                "selection_checksum": "c" * 64,
            },
        }
        embedded_json = f"sync context={json.dumps(payload, separators=(',', ':'))} done"
        assignments = (
            "fund_code=519755 status=partial "
            "candidate_fingerprint=candidate-assignment-sentinel "
            "raw_selection_manifest_json=manifest-assignment-sentinel "
            "normalized_html='<html>html-assignment-sentinel</html>' "
            "database_path=/private/tmp/database-assignment-sentinel.db "
            "exception_text='traceback exception-assignment-sentinel'"
        )
        container_assignment = (
            'status=partial candidate_fingerprints=["container-one-sentinel",'
            '"container-two-sentinel"]'
        )

        redacted_json = redact_secrets(embedded_json)
        redacted_assignments = redact_secrets(assignments)
        redacted_container = redact_secrets(container_assignment)

        for sentinel in (
            "json-candidate-one-sentinel",
            "json-candidate-two-sentinel",
            "json-unselected-sentinel",
            "json-raw-selection-sentinel",
            "json-html-sentinel",
            "json-database-sentinel",
            "json-exception-sentinel",
            "json-selection-canonical-sentinel",
            "candidate-assignment-sentinel",
            "manifest-assignment-sentinel",
            "html-assignment-sentinel",
            "database-assignment-sentinel",
            "exception-assignment-sentinel",
            "container-one-sentinel",
            "container-two-sentinel",
        ):
            self.assertNotIn(sentinel, redacted_json)
            self.assertNotIn(sentinel, redacted_assignments)
            self.assertNotIn(sentinel, redacted_container)
        self.assertIn("fund_code", redacted_json)
        self.assertIn("519755", redacted_json)
        self.assertIn("status", redacted_json)
        self.assertIn("partial", redacted_json)
        self.assertIn("c" * 64, redacted_json)
        self.assertIn("fund_code=519755", redacted_assignments)
        self.assertIn("status=partial", redacted_assignments)

    def test_d1_selection_internal_fields_are_redacted_from_bytes(self) -> None:
        source = (
            b'fund_code=519755 candidate_fingerprints=["bytes-one-sentinel",'
            b'"bytes-two-sentinel"] status=partial'
        )

        for value in (source, bytearray(source)):
            with self.subTest(value_type=type(value).__name__):
                redacted = redact_secrets(value)
                self.assertIs(type(redacted), type(value))
                rendered = bytes(redacted).decode("utf-8")
                self.assertNotIn("bytes-one-sentinel", rendered)
                self.assertNotIn("bytes-two-sentinel", rendered)
                self.assertIn("fund_code=519755", rendered)
                self.assertIn("status=partial", rendered)
                self.assertIn("candidate_fingerprints=[REDACTED]", rendered)

    def test_non_utf8_bytes_fail_closed_without_exposing_selection_fields(self) -> None:
        source = b'candidate_fingerprints=["binary-secret-sentinel"]\xff'

        for value, expected in (
            (source, b"[REDACTED]"),
            (bytearray(source), bytearray(b"[REDACTED]")),
        ):
            with self.subTest(value_type=type(value).__name__):
                redacted = redact_secrets(value)
                self.assertIs(type(redacted), type(value))
                self.assertEqual(redacted, expected)
                self.assertNotIn(b"binary-secret-sentinel", bytes(redacted))

    def test_d1_selection_container_redaction_preserves_surrounding_safe_fields(
        self,
    ) -> None:
        checksum = "d" * 64
        cases = (
            (
                'candidate_fingerprints=["array-one-sentinel",'
                '{"nested":["array-two-sentinel]still-secret"]}]',
                "candidate_fingerprints=[REDACTED]",
            ),
            (
                "candidate_fingerprints=('tuple-one-sentinel', "
                "{'nested': ('tuple-two-sentinel)still-secret',)})",
                "candidate_fingerprints=[REDACTED]",
            ),
            (
                "unselected_urls={'primary': ['https://private.invalid/object-sentinel']}",
                "unselected_urls=[REDACTED]",
            ),
            (
                '"candidate_fingerprints": ["quoted-key-sentinel"]',
                '"candidate_fingerprints": [REDACTED]',
            ),
        )

        for sensitive, expected in cases:
            with self.subTest(sensitive=sensitive[:40]):
                value = (
                    f"fund_code=519755 status=partial {sensitive} "
                    f"selection_checksum={checksum} done"
                )
                redacted = redact_secrets(value)
                self.assertIn("fund_code=519755", redacted)
                self.assertIn("status=partial", redacted)
                self.assertIn(f"selection_checksum={checksum}", redacted)
                self.assertIn("done", redacted)
                self.assertIn(expected, redacted)
                self.assertNotIn("sentinel", redacted)

    def test_d1_selection_unclosed_or_mismatched_container_fails_closed(self) -> None:
        cases = (
            'candidate_fingerprints=["unclosed-array-sentinel"',
            'candidate_fingerprints=[{"nested":"mismatched-sentinel"]}',
            "unselected_urls={'nested': ('unclosed-tuple-sentinel'}",
            'raw_selection_json={"nested":["unclosed-object-sentinel"]',
        )

        for sensitive in cases:
            with self.subTest(sensitive=sensitive):
                value = f"fund_code=519755 {sensitive} trailing-secret-sentinel"
                redacted = redact_secrets(value)
                self.assertEqual(
                    redacted,
                    f"fund_code=519755 {sensitive.split('=', 1)[0]}=[REDACTED]",
                )
                self.assertNotIn("sentinel", redacted)

    def test_d1_selection_quoted_values_handle_escapes_and_preserve_safe_suffix(
        self,
    ) -> None:
        checksum = "e" * 64
        cases = (
            (
                r'candidate_fingerprint="prefix\"double-escaped-sentinel"',
                'candidate_fingerprint="[REDACTED]"',
            ),
            (
                r"candidate_fingerprint='prefix\'single-escaped-sentinel'",
                "candidate_fingerprint='[REDACTED]'",
            ),
            (
                'exception_text="prefix\\\\backslash-sentinel"',
                'exception_text="[REDACTED]"',
            ),
            (
                'candidate_fingerprint="[REDACTED]"',
                'candidate_fingerprint="[REDACTED]"',
            ),
            (
                "candidate_fingerprint='[REDACTED]'",
                "candidate_fingerprint='[REDACTED]'",
            ),
        )

        for sensitive, expected in cases:
            with self.subTest(sensitive=sensitive):
                value = f"fund_code=519755 {sensitive} status=partial selection_checksum={checksum}"
                redacted = redact_secrets(value)
                self.assertIn("fund_code=519755", redacted)
                self.assertIn(expected, redacted)
                self.assertIn("status=partial", redacted)
                self.assertIn(f"selection_checksum={checksum}", redacted)
                self.assertNotIn("sentinel", redacted)

    def test_d1_selection_unclosed_quoted_values_fail_closed(self) -> None:
        cases = (
            r'candidate_fingerprint="prefix\"double-unclosed-sentinel',
            r"candidate_fingerprint='prefix\'single-unclosed-sentinel",
            r'context={"status":"partial","candidate_fingerprint":"json\"unclosed-sentinel',
        )

        for value in cases:
            with self.subTest(value=value):
                redacted = redact_secrets(f"fund_code=519755 {value} trailing-secret-sentinel")
                self.assertIn("fund_code=519755", redacted)
                self.assertIn("candidate_fingerprint", redacted)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("sentinel", redacted)
                self.assertNotIn("trailing-secret", redacted)

    def test_d1_selection_fields_are_redacted_from_non_dict_mappings(self) -> None:
        mappings = (
            UserDict(
                {
                    "fund_code": "519755",
                    "status": "partial",
                    "candidate_fingerprints": ("userdict-secret-sentinel",),
                    "nested": MappingProxyType(
                        {
                            "normalized_html": "<html>nested-userdict-sentinel</html>",
                            "safe_code": "current_periodic_candidate_missing",
                        }
                    ),
                }
            ),
            MappingProxyType(
                {
                    "fund_code": "519755",
                    "status": "partial",
                    "unselected_urls": ("https://private.invalid/proxy-secret-sentinel",),
                    "nested": UserDict(
                        {
                            "exception_text": "traceback nested-proxy-sentinel",
                            "safe_code": "current_periodic_candidate_conflict",
                        }
                    ),
                }
            ),
        )

        for value in mappings:
            with self.subTest(value_type=type(value).__name__):
                redacted = redact_secrets(value)
                rendered = json.dumps(redacted, ensure_ascii=False)
                self.assertEqual(redacted["fund_code"], "519755")
                self.assertEqual(redacted["status"], "partial")
                self.assertIn(redacted["nested"]["safe_code"], rendered)
                self.assertNotIn("sentinel", rendered)

    def test_d1_escaped_and_double_serialized_selection_fields_fail_closed(self) -> None:
        payload = {
            "candidate_fingerprint": "double-json-candidate-secret-sentinel",
            "unselected_urls": ["https://private.invalid/double-json-url-secret-sentinel"],
            "exception_text": "traceback double-json-exception-secret-sentinel",
        }
        cases = (
            r"context={\"candidate_fingerprint\":\"escaped-key-secret-sentinel\"}",
            r"candidate_fingerprint=\"escaped-value-secret-sentinel\"",
            json.dumps(json.dumps(payload, separators=(",", ":"))),
        )

        for value in cases:
            with self.subTest(value=value):
                redacted = redact_secrets(f"fund_code=519755 {value} trailing-secret-sentinel")
                self.assertIn("fund_code=519755", redacted)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn("secret-sentinel", redacted)

    def test_d1_public_audit_fields_remain_visible_but_input_fingerprint_is_redacted(
        self,
    ) -> None:
        fingerprint = "a" * 64
        report = {
            "capability": "research_only",
            "fund_code": "519755",
            "classification": {
                "input_fingerprint": fingerprint,
                "policy_version": "1",
                "product_family": "broad_index_equity",
                "risk_bucket": "diversified_equity",
                "portfolio_role": "core_eligible",
            },
            "evidence_status": "verified",
            "sources": [
                {
                    "url": "https://www.fund001.com/public.pdf",
                    "title": "official title",
                    "checksum": "b" * 64,
                    "published_at": "2026-07-13T00:00:00+00:00",
                }
            ],
            "verified_facts": [{"source_excerpt": "bounded public excerpt"}],
        }

        redacted = redact_secrets(report)

        self.assertEqual(redacted["classification"]["input_fingerprint"], "[REDACTED]")
        rendered = json.dumps(redacted, ensure_ascii=False)
        for public_value in (
            "https://www.fund001.com/public.pdf",
            "official title",
            "bounded public excerpt",
            "b" * 64,
            "2026-07-13T00:00:00+00:00",
        ):
            self.assertIn(public_value, rendered)

        record = logging.LogRecord(
            "x",
            logging.INFO,
            __file__,
            1,
            "risk report: %r",
            (report,),
            None,
        )
        record.context = report
        SecretRedactionFilter().filter(record)
        self.assertEqual(record.context["classification"]["input_fingerprint"], "[REDACTED]")
        self.assertNotIn(fingerprint, record.getMessage())
        self.assertIn("[REDACTED]", record.getMessage())
        self.assertIn("https://www.fund001.com/public.pdf", record.getMessage())

    def test_disguised_d1_free_text_cannot_expose_input_fingerprint(self) -> None:
        fingerprint = "d" * 64
        value = (
            "research_only classification product_family evidence_status "
            f"input_fingerprint={fingerprint}"
        )
        record = logging.LogRecord(
            "x",
            logging.ERROR,
            __file__,
            1,
            "risk failure: %s",
            (RuntimeError(value),),
            None,
        )

        redacted = redact_secrets(value)
        SecretRedactionFilter().filter(record)

        self.assertNotIn(fingerprint, redacted)
        self.assertIn("input_fingerprint=[REDACTED]", redacted)
        self.assertNotIn(fingerprint, record.getMessage())
        self.assertIn("input_fingerprint=[REDACTED]", record.getMessage())

    def test_disguised_d1_mapping_cannot_expose_input_fingerprint(self) -> None:
        fingerprint = "e" * 64
        value = {
            "capability": "research_only",
            "fund_code": "519755",
            "evidence_status": "verified",
            "classification": {
                "input_fingerprint": fingerprint,
                "policy_version": "1",
                "product_family": "broad_index",
                "risk_bucket": "diversified_equity",
                "portfolio_role": "core_eligible",
            },
        }
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "risk audit", (), None)
        record.context = value

        redacted = redact_secrets(value)
        SecretRedactionFilter().filter(record)

        self.assertEqual(redacted["classification"]["input_fingerprint"], "[REDACTED]")
        self.assertEqual(record.context["classification"]["input_fingerprint"], "[REDACTED]")

    def test_non_d1_input_fingerprint_remains_redacted(self) -> None:
        value = {"input_fingerprint": "c" * 64, "status": "range_available"}

        self.assertEqual(redact_secrets(value)["input_fingerprint"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
