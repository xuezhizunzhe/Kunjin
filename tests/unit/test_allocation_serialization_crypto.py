from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kunjin.allocation.crypto import (
    ALLOCATION_ASSOCIATED_DATA,
    ALLOCATION_ENCRYPTION_INFO,
    ALLOCATION_FINGERPRINT_INFO,
    AllocationCipher,
    EncryptedAllocationAssessment,
    _decode_base64,
)
from kunjin.allocation.models import (
    AggregateAllocationInputs,
    AllocationExactResult,
    AllocationSleeveKind,
    AssignedSleeveDetail,
    GoalFundingDetail,
    GoalFundingState,
    ObligationFundingDetail,
)
from kunjin.allocation.serialization import (
    MAX_COLLECTION_ITEMS,
    MAX_EXACT_PAYLOAD_BYTES,
    MAX_INTEGER_DIGITS,
    MAX_TEXT_CHARS,
    decode_exact_result,
    encode_exact_result,
)
from kunjin.suitability.crypto import (
    AssessmentCipher,
    EncryptedAssessment,
    EncryptedProfile,
    ProfileCipher,
    ProfileCryptoError,
)
from kunjin.suitability.models import UNSAFE_PRIVATE_TEXT_CODEPOINTS


def D(value: str) -> Decimal:
    return Decimal(value)


class FakeProfileKeyStore:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = key
        self.load_existing_calls = 0
        self.load_or_create_calls = 0

    def load_existing_key(self) -> bytes | None:
        self.load_existing_calls += 1
        return self.key

    def load_or_create_key(self) -> bytes:
        self.load_or_create_calls += 1
        if self.key is None:
            self.key = bytes(range(32))
        return self.key


def exact_result() -> AllocationExactResult:
    goals = (
        GoalFundingDetail(
            name="near-purpose",
            target_date=date(2027, 7, 12),
            target_amount=D("100.00"),
            amount_already_reserved=D("50.00"),
            confirmed_monthly_saving=D("10.00"),
            remaining_contribution_periods=5,
            zero_return_funding=D("100.00"),
            funding_state=GoalFundingState.FUNDABLE_WITHOUT_RETURN,
            horizon_equity_ceiling=D("0.00"),
        ),
        GoalFundingDetail(
            name="later-purpose",
            target_date=date(2032, 7, 12),
            target_amount=D("500.00"),
            amount_already_reserved=D("100.00"),
            confirmed_monthly_saving=D("0.00"),
            remaining_contribution_periods=73,
            zero_return_funding=D("100.00"),
            funding_state=GoalFundingState.FUNDING_GAP_WITHOUT_RETURN,
            horizon_equity_ceiling=D("0.50"),
        ),
    )
    obligations = (
        ObligationFundingDetail(
            name="known-obligation",
            due_date=date(2030, 7, 12),
            amount=D("100.00"),
            amount_already_reserved=D("100.00"),
            funding_gap=D("0.00"),
            confirmed_monthly_saving=D("0.00"),
            remaining_contribution_periods=49,
            zero_return_funding=D("100.00"),
            horizon_equity_ceiling=D("0.30"),
        ),
    )
    sleeves = (
        AssignedSleeveDetail(
            sleeve_kind=AllocationSleeveKind.GOAL,
            name="later-purpose",
            assigned_amount=D("100.00"),
            horizon_date=date(2032, 7, 12),
            horizon_equity_ceiling=D("0.50"),
            weighted_equity_contribution=D("50.0000"),
        ),
        AssignedSleeveDetail(
            sleeve_kind=AllocationSleeveKind.OBLIGATION,
            name="known-obligation",
            assigned_amount=D("100.00"),
            horizon_date=date(2030, 7, 12),
            horizon_equity_ceiling=D("0.30"),
            weighted_equity_contribution=D("30.0000"),
        ),
        AssignedSleeveDetail(
            sleeve_kind=AllocationSleeveKind.RESIDUAL,
            name="residual",
            assigned_amount=D("600.00"),
            horizon_date=date(2032, 7, 12),
            horizon_equity_ceiling=D("0.50"),
            weighted_equity_contribution=D("300.0000"),
        ),
    )
    return AllocationExactResult(
        assessment_date=date(2026, 7, 12),
        total_financial_assets=D("1000.00"),
        liquid_protection_assets=D("400.00"),
        verified_emergency_reserve=D("100.00"),
        minimum_operating_cash=D("50.00"),
        protected_short_term_assigned=D("50.00"),
        protected_liquid_claims=D("200.00"),
        investable_stock_assets=D("800.00"),
        monthly_discretionary_allocation_ceiling=D("25.00"),
        maximum_tolerable_loss=D("100.00"),
        maximum_tolerable_drawdown=D("0.20"),
        residual_horizon_date=date(2032, 7, 12),
        goal_funding_details=goals,
        obligation_funding_details=obligations,
        assigned_sleeves=sleeves,
        aggregate_inputs=AggregateAllocationInputs(
            weighted_horizon_numerator=D("380.0000"),
            weighted_horizon_equity_ceiling=D("0.47"),
            loss_amount_equity_ceiling=D("0.25"),
            drawdown_equity_ceiling=D("0.40"),
            willingness_equity_ceiling=D("0.50"),
            stability_equity_ceiling=D("0.30"),
            fixed_income_stress_loss=D("0.10"),
            equity_stress_loss=D("0.50"),
        ),
    )


class AllocationSerializationTest(unittest.TestCase):
    def test_public_resource_limits_are_fixed(self) -> None:
        self.assertEqual(MAX_EXACT_PAYLOAD_BYTES, 1_048_576)
        self.assertEqual(MAX_COLLECTION_ITEMS, 10_000)
        self.assertEqual(MAX_TEXT_CHARS, 4_096)
        self.assertEqual(MAX_INTEGER_DIGITS, 12)

    def test_round_trip_is_sorted_and_exact(self) -> None:
        value = exact_result()
        encoded = encode_exact_result(value)

        self.assertEqual(decode_exact_result(encoded), value)
        self.assertEqual(
            encoded, json.dumps(json.loads(encoded), sort_keys=True, separators=(",", ":")).encode()
        )
        self.assertIn(b'"total_financial_assets":"1000.00"', encoded)
        self.assertIn(b'"maximum_tolerable_drawdown":"0.20"', encoded)
        self.assertIn(b'"weighted_horizon_numerator":"380"', encoded)
        self.assertIn(b'"assessment_date":"2026-07-12"', encoded)
        self.assertNotIn(b"NaN", encoded)

    def test_encoder_rejects_wrong_type_and_invalid_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact AllocationExactResult"):
            encode_exact_result({})  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            object.__setattr__(exact_result(), "hidden", "private")
            value = exact_result()
            object.__setattr__(value, "hidden", "private")
            encode_exact_result(value)

    def test_tuple_order_is_preserved_as_exact_payload_state(self) -> None:
        value = exact_result()
        reordered = replace(
            value,
            goal_funding_details=tuple(reversed(value.goal_funding_details)),
        )
        encoded = encode_exact_result(reordered)
        self.assertEqual(decode_exact_result(encoded), reordered)
        names = [item["name"] for item in json.loads(encoded)["goal_funding_details"]]
        self.assertEqual(names, ["later-purpose", "near-purpose"])

    def test_decoder_rejects_noncanonical_decimal(self) -> None:
        payload = encode_exact_result(exact_result()).replace(b'"1000.00"', b'"1000.0"', 1)
        with self.assertRaisesRegex(ValueError, "canonical decimal"):
            decode_exact_result(payload)

    def test_decoder_rejects_noncanonical_ratio_and_derived_decimal(self) -> None:
        encoded = encode_exact_result(exact_result())
        for payload in (
            encoded.replace(b'"0.20"', b'".20"', 1),
            encoded.replace(b'"380"', b'"380.0"', 1),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "canonical decimal"):
                    decode_exact_result(payload)

    def test_decoder_rejects_exponent_and_excessive_decimal_before_arithmetic(self) -> None:
        encoded = encode_exact_result(exact_result())
        for replacement in (b'"1E+999999999"', b'"' + b"9" * 10021 + b'.00"'):
            with self.subTest(length=len(replacement)):
                payload = encoded.replace(b'"1000.00"', replacement, 1)
                with self.assertRaisesRegex(ValueError, "canonical decimal"):
                    decode_exact_result(payload)

    def test_decoder_rejects_duplicate_unexpected_and_missing_keys(self) -> None:
        encoded = encode_exact_result(exact_result())
        duplicate = encoded.replace(b"{", b'{"assessment_date":"2026-07-12",', 1)
        unexpected = encoded.replace(b"{", b'{"unexpected":"value",', 1)
        payload = json.loads(encoded)
        del payload["assessment_date"]
        missing = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        cases = (
            (duplicate, "duplicate allocation key"),
            (unexpected, "unexpected allocation keys"),
            (missing, "missing allocation keys"),
        )
        for candidate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    decode_exact_result(candidate)

    def test_decoder_rejects_float_nonfinite_bool_as_int_and_wrong_container(self) -> None:
        encoded = encode_exact_result(exact_result())
        cases = (
            (
                encoded.replace(
                    b'"remaining_contribution_periods":5', b'"remaining_contribution_periods":5.0'
                ),
                "floating-point",
            ),
            (
                encoded.replace(
                    b'"remaining_contribution_periods":5', b'"remaining_contribution_periods":NaN'
                ),
                "constant",
            ),
            (
                encoded.replace(
                    b'"remaining_contribution_periods":5', b'"remaining_contribution_periods":true'
                ),
                "non-negative integer",
            ),
            (
                json.dumps(
                    {**json.loads(encoded), "goal_funding_details": {}},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode(),
                "must be a list",
            ),
        )
        for candidate, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    decode_exact_result(candidate)

    def test_decoder_rejects_noncanonical_and_invalid_dates(self) -> None:
        encoded = encode_exact_result(exact_result())
        for replacement in (b"2026-7-12", b"2026-02-30", b"2026-07-12T00:00:00"):
            with self.subTest(replacement=replacement):
                payload = encoded.replace(b"2026-07-12", replacement, 1)
                with self.assertRaisesRegex(ValueError, "canonical ISO date"):
                    decode_exact_result(payload)

    def test_decoder_rejects_invalid_enum_and_declared_state(self) -> None:
        encoded = encode_exact_result(exact_result())
        invalid_enum = encoded.replace(b'"fundable_without_return"', b'"invented"', 1)
        inconsistent = encoded.replace(
            b'"zero_return_funding":"100.00"', b'"zero_return_funding":"99.00"', 1
        )
        with self.assertRaisesRegex(ValueError, "funding_state"):
            decode_exact_result(invalid_enum)
        with self.assertRaisesRegex(ValueError, "zero-return funding"):
            decode_exact_result(inconsistent)

    def test_decoder_requires_bytes_object_and_canonical_json_layout(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be bytes"):
            decode_exact_result("{}")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "JSON object"):
            decode_exact_result(b"[]")
        payload = json.loads(encode_exact_result(exact_result()))
        noncanonical = json.dumps(payload, sort_keys=True, indent=2).encode()
        with self.assertRaisesRegex(ValueError, "JSON is not canonical"):
            decode_exact_result(noncanonical)

    @patch("kunjin.allocation.serialization.json.loads")
    def test_oversized_payload_is_rejected_before_utf8_or_json(self, loads) -> None:
        payload = b"x" * (MAX_EXACT_PAYLOAD_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "payload is too large"):
            decode_exact_result(payload)
        loads.assert_not_called()

    def test_deep_json_and_huge_integer_are_stable_value_errors(self) -> None:
        deep = (b'{"x":' * 1_000) + b"0" + (b"}" * 1_000)
        with self.assertRaisesRegex(ValueError, "allocation exact result JSON is invalid"):
            decode_exact_result(deep)

        encoded = encode_exact_result(exact_result())
        huge = b"9" * (MAX_INTEGER_DIGITS + 1)
        payload = encoded.replace(
            b'"remaining_contribution_periods":5', b'"remaining_contribution_periods":' + huge
        )
        with self.assertRaisesRegex(ValueError, "JSON integer is too large"):
            decode_exact_result(payload)

    def test_collection_and_text_limits_are_enforced(self) -> None:
        encoded = encode_exact_result(exact_result())
        payload = json.loads(encoded)
        payload["goal_funding_details"] = [{}] * (MAX_COLLECTION_ITEMS + 1)
        oversized_list = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "too many items"):
            decode_exact_result(oversized_list)

        payload = json.loads(encoded)
        payload["goal_funding_details"][0]["name"] = "x" * (MAX_TEXT_CHARS + 1)
        oversized_text = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        with self.assertRaisesRegex(ValueError, "text is too long"):
            decode_exact_result(oversized_text)

    def test_text_rejects_unsafe_formatting_but_allows_chinese_emoji_and_zwj(self) -> None:
        value = exact_result()
        allowed_goal = replace(
            value.goal_funding_details[0],
            name="家庭\U0001f469\u200d\U0001f4bb目标",
        )
        allowed = replace(
            value,
            goal_funding_details=(allowed_goal,) + value.goal_funding_details[1:],
        )
        self.assertEqual(decode_exact_result(encode_exact_result(allowed)), allowed)

        invalid_names = (
            "bad\x00name",
            "bad\x1fname",
            "bad\x7fname",
            "bad\x85name",
            "bad\ud800name",
            *(f"bad{chr(codepoint)}name" for codepoint in UNSAFE_PRIVATE_TEXT_CODEPOINTS),
        )
        encoded = encode_exact_result(value)
        for invalid_name in invalid_names:
            with self.subTest(invalid_name=ascii(invalid_name)):
                invalid_goal = replace(value.goal_funding_details[0], name=invalid_name)
                invalid = replace(
                    value,
                    goal_funding_details=(invalid_goal,) + value.goal_funding_details[1:],
                )
                with self.assertRaisesRegex(ValueError, "unsupported characters"):
                    encode_exact_result(invalid)

                payload = json.loads(encoded)
                payload["goal_funding_details"][0]["name"] = invalid_name
                candidate = json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                with self.assertRaisesRegex(ValueError, "unsupported characters"):
                    decode_exact_result(candidate)


class AllocationCipherTest(unittest.TestCase):
    def test_allocation_domains_are_fixed(self) -> None:
        self.assertEqual(ALLOCATION_ENCRYPTION_INFO, b"kunjin/allocation-assessment/encryption/v1")
        self.assertEqual(
            ALLOCATION_FINGERPRINT_INFO, b"kunjin/allocation-assessment/fingerprint/v1"
        )
        self.assertEqual(ALLOCATION_ASSOCIATED_DATA, b"kunjin/allocation-assessment/v1")

    def test_round_trip_random_nonce_and_stable_fingerprint(self) -> None:
        store = FakeProfileKeyStore()
        cipher = AllocationCipher(store)
        payload = encode_exact_result(exact_result())
        first = cipher.encrypt(payload)
        second = cipher.encrypt(payload)
        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.keyed_fingerprint, second.keyed_fingerprint)
        self.assertEqual(cipher.decrypt(first), payload)
        self.assertEqual(cipher.fingerprint(payload), first.keyed_fingerprint)
        self.assertEqual(store.load_or_create_calls, 2)
        self.assertGreaterEqual(store.load_existing_calls, 2)

    @patch("kunjin.allocation.crypto.os.urandom", return_value=b"n" * 12)
    def test_known_hkdf_aes_and_hmac_vector(self, _) -> None:
        master_key = bytes(range(32))
        payload = b"known-allocation-payload"
        cipher = AllocationCipher(FakeProfileKeyStore(master_key))
        encryption_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=ALLOCATION_ENCRYPTION_INFO
        ).derive(master_key)
        fingerprint_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=ALLOCATION_FINGERPRINT_INFO
        ).derive(master_key)
        expected_ciphertext = AESGCM(encryption_key).encrypt(
            b"n" * 12, payload, ALLOCATION_ASSOCIATED_DATA
        )
        expected_fingerprint = hmac.new(fingerprint_key, payload, hashlib.sha256).hexdigest()
        encrypted = cipher.encrypt(payload)
        self.assertEqual(base64.urlsafe_b64decode(encrypted.ciphertext), expected_ciphertext)
        self.assertEqual(encrypted.keyed_fingerprint, expected_fingerprint)
        self.assertEqual(cipher.fingerprint(payload), expected_fingerprint)
        self.assertEqual(
            encryption_key.hex(), "6b8a37b27d396914cfa031b259950bde4b8d479a1f2d5703ae59fc435e5e3568"
        )
        self.assertEqual(
            fingerprint_key.hex(),
            "6b42989a5b61c171e186dc33ab531295c9d8dc1dc8506900773d46e7c7352777",
        )
        self.assertEqual(
            encrypted.ciphertext,
            "0E9AZuSbGmZgi95GbJk_S-QWRkYTR0XZ3MXXsXXH-GyTNZUHISe6qQ==",
        )
        self.assertEqual(
            encrypted.keyed_fingerprint,
            "647fcd1d01d4c26bdcb5b32e3671ba96f927019a883b190c4a2f4b3923bb8b32",
        )

    def test_decrypt_and_fingerprint_never_create_missing_key(self) -> None:
        store = FakeProfileKeyStore()
        cipher = AllocationCipher(store)
        encrypted = cipher.encrypt(b"private-allocation")
        self.assertEqual(store.load_or_create_calls, 1)
        store.key = None
        with self.assertRaisesRegex(ProfileCryptoError, "allocation key is unavailable"):
            cipher.decrypt(encrypted)
        with self.assertRaisesRegex(ProfileCryptoError, "allocation key is unavailable"):
            cipher.fingerprint(b"input")
        self.assertEqual(store.load_or_create_calls, 1)
        self.assertIsNone(store.key)

    def test_tamper_invalid_metadata_and_wrong_types_are_rejected(self) -> None:
        cipher = AllocationCipher(FakeProfileKeyStore())
        encrypted = cipher.encrypt(b"private-allocation")
        invalid = (
            replace(encrypted, algorithm="AES-128-GCM"),
            replace(encrypted, key_version="2"),
            replace(encrypted, nonce="not base64!"),
            replace(encrypted, ciphertext="not base64!"),
            replace(encrypted, keyed_fingerprint="A" * 64),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ProfileCryptoError, "allocation decryption failed"):
                    cipher.decrypt(value)
        with self.assertRaisesRegex(ProfileCryptoError, "allocation encryption failed"):
            cipher.encrypt("private")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ProfileCryptoError, "allocation fingerprint failed"):
            cipher.fingerprint("private")  # type: ignore[arg-type]

    def test_valid_base64_tamper_and_injected_metadata_state_are_rejected(self) -> None:
        cipher = AllocationCipher(FakeProfileKeyStore())
        encrypted = cipher.encrypt(b"private-allocation")
        ciphertext = bytearray(base64.urlsafe_b64decode(encrypted.ciphertext))
        ciphertext[0] ^= 1
        tampered = replace(
            encrypted,
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        )
        with self.assertRaisesRegex(ProfileCryptoError, "allocation decryption failed"):
            cipher.decrypt(tampered)

        object.__setattr__(encrypted, "unexpected", "private")
        with self.assertRaisesRegex(ProfileCryptoError, "allocation decryption failed"):
            cipher.decrypt(encrypted)

    def test_malformed_existing_key_is_not_replaced(self) -> None:
        store = FakeProfileKeyStore(b"short")
        cipher = AllocationCipher(store)
        with self.assertRaisesRegex(
            ProfileCryptoError,
            "allocation key is unavailable",
        ):
            cipher.fingerprint(b"input")
        self.assertEqual(store.key, b"short")
        self.assertEqual(store.load_or_create_calls, 0)

    @patch("kunjin.allocation.crypto.base64.b64decode")
    def test_huge_base64_is_rejected_before_decode(self, decode) -> None:
        cipher = AllocationCipher(FakeProfileKeyStore(bytes(range(32))))
        maximum_encoded = 4 * ((MAX_EXACT_PAYLOAD_BYTES + 16 + 2) // 3)
        encrypted = EncryptedAllocationAssessment(
            algorithm="AES-256-GCM",
            key_version="1",
            nonce=base64.urlsafe_b64encode(b"n" * 12).decode("ascii"),
            ciphertext="A" * (maximum_encoded + 4),
            keyed_fingerprint="0" * 64,
        )
        with self.assertRaisesRegex(ProfileCryptoError, "allocation decryption failed"):
            cipher.decrypt(encrypted)
        decode.assert_not_called()

    def test_ciphertext_maximum_boundary_is_accepted_by_base64_guard(self) -> None:
        decoded = b"x" * (MAX_EXACT_PAYLOAD_BYTES + 16)
        encoded = base64.urlsafe_b64encode(decoded).decode("ascii")
        self.assertEqual(
            _decode_base64(encoded, maximum_decoded_length=MAX_EXACT_PAYLOAD_BYTES + 16),
            decoded,
        )
        with self.assertRaisesRegex(ValueError, "invalid length"):
            _decode_base64(
                base64.urlsafe_b64encode(decoded + b"x").decode("ascii"),
                maximum_decoded_length=MAX_EXACT_PAYLOAD_BYTES + 16,
            )

    def test_encrypt_rejects_oversized_plaintext_before_key_or_aes(self) -> None:
        store = FakeProfileKeyStore()
        cipher = AllocationCipher(store)
        with self.assertRaisesRegex(ProfileCryptoError, "allocation encryption failed"):
            cipher.encrypt(b"x" * (MAX_EXACT_PAYLOAD_BYTES + 1))
        self.assertEqual(store.load_or_create_calls, 0)

    def test_key_store_exceptions_are_normalized_without_private_text(self) -> None:
        class MaliciousKeyStore:
            def load_existing_key(self) -> bytes:
                raise ProfileCryptoError("private-key-store-sentinel")

            def load_or_create_key(self) -> bytes:
                raise RuntimeError("private-key-store-sentinel")

        cipher = AllocationCipher(MaliciousKeyStore())
        encrypted = EncryptedAllocationAssessment(
            algorithm="AES-256-GCM",
            key_version="1",
            nonce=base64.urlsafe_b64encode(b"n" * 12).decode("ascii"),
            ciphertext=base64.urlsafe_b64encode(b"x" * 16).decode("ascii"),
            keyed_fingerprint="0" * 64,
        )
        operations = (
            lambda: cipher.encrypt(b"payload"),
            lambda: cipher.decrypt(encrypted),
            lambda: cipher.fingerprint(b"payload"),
        )
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    ProfileCryptoError, "^allocation key is unavailable$"
                ) as raised:
                    operation()
                self.assertNotIn("private-key-store-sentinel", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    @patch("kunjin.allocation.crypto.os.urandom", return_value=b"n" * 12)
    def test_profile_suitability_and_allocation_domains_cannot_cross_decrypt(self, _) -> None:
        store = FakeProfileKeyStore(bytes(range(32)))
        payload = b"same-private-payload"
        profile = ProfileCipher(store).encrypt(payload)
        suitability = AssessmentCipher(store).encrypt(payload)
        allocation = AllocationCipher(store).encrypt(payload)
        self.assertEqual(profile.nonce, suitability.nonce)
        self.assertEqual(suitability.nonce, allocation.nonce)
        self.assertEqual(
            len({profile.ciphertext, suitability.ciphertext, allocation.ciphertext}), 3
        )
        self.assertEqual(
            len(
                {
                    profile.keyed_fingerprint,
                    suitability.keyed_fingerprint,
                    allocation.keyed_fingerprint,
                }
            ),
            3,
        )

        allocation_cipher = AllocationCipher(store)
        for foreign in (profile, suitability):
            with self.subTest(foreign=type(foreign).__name__):
                with self.assertRaisesRegex(ProfileCryptoError, "allocation decryption failed"):
                    allocation_cipher.decrypt(
                        EncryptedAllocationAssessment(*foreign.__dict__.values())
                    )
        with self.assertRaisesRegex(ProfileCryptoError, "profile decryption failed"):
            ProfileCipher(store).decrypt(EncryptedProfile(*allocation.__dict__.values()))
        with self.assertRaisesRegex(ProfileCryptoError, "assessment decryption failed"):
            AssessmentCipher(store).decrypt(EncryptedAssessment(*allocation.__dict__.values()))


if __name__ == "__main__":
    unittest.main()
