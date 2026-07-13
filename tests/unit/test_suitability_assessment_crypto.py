from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kunjin.suitability.assessment_serialization import (
    decode_assessment_amounts,
    encode_assessment_amounts,
)
from kunjin.suitability.crypto import (
    AssessmentCipher,
    EncryptedAssessment,
    ProfileCipher,
    ProfileCryptoError,
)
from kunjin.suitability.models import AssessmentAmounts


class FakeProfileKeyStore:
    def __init__(self, key: bytes | None = None) -> None:
        self.key = key
        self.load_or_create_calls = 0

    def load_existing_key(self) -> bytes | None:
        return self.key

    def load_or_create_key(self) -> bytes:
        self.load_or_create_calls += 1
        if self.key is None:
            self.key = bytes(range(32))
        return self.key


def assessment_amounts() -> AssessmentAmounts:
    return AssessmentAmounts(
        verified_emergency_reserve=Decimal("50000.00"),
        required_emergency_reserve=Decimal("39000.00"),
        emergency_reserve_shortfall=Decimal("0.00"),
        required_monthly_obligation_saving=Decimal("123.45"),
        required_monthly_goal_saving=Decimal("678.90"),
        monthly_safety_residual=Decimal("4321.09"),
        safe_monthly_ceiling=Decimal("1000.00"),
    )


class AssessmentSerializationTest(unittest.TestCase):
    def test_round_trip_uses_exact_sorted_decimal_string_fields(self) -> None:
        amounts = assessment_amounts()

        encoded = encode_assessment_amounts(amounts)

        self.assertEqual(decode_assessment_amounts(encoded), amounts)
        self.assertEqual(
            encoded,
            b'{"emergency_reserve_shortfall":"0.00",'
            b'"monthly_safety_residual":"4321.09",'
            b'"required_emergency_reserve":"39000.00",'
            b'"required_monthly_goal_saving":"678.90",'
            b'"required_monthly_obligation_saving":"123.45",'
            b'"safe_monthly_ceiling":"1000.00",'
            b'"verified_emergency_reserve":"50000.00"}',
        )

    def test_encoder_rejects_invalid_amounts_and_wrong_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "amounts must be AssessmentAmounts"):
            encode_assessment_amounts({})

        invalid = replace(
            assessment_amounts(),
            monthly_safety_residual=Decimal("NaN"),
        )
        with self.assertRaisesRegex(ValueError, "monthly safety residual must be finite"):
            encode_assessment_amounts(invalid)

        sub_cent = replace(
            assessment_amounts(),
            monthly_safety_residual=Decimal("1.001"),
        )
        with self.assertRaisesRegex(ValueError, "whole cents"):
            encode_assessment_amounts(sub_cent)

    def test_encoder_uses_fixed_two_decimals_and_normalizes_negative_zero(self) -> None:
        amounts = replace(
            assessment_amounts(),
            verified_emergency_reserve=Decimal("5E+3"),
            emergency_reserve_shortfall=Decimal("-0.00"),
            monthly_safety_residual=Decimal("4321.0"),
        )

        payload = json.loads(encode_assessment_amounts(amounts))

        self.assertEqual(payload["verified_emergency_reserve"], "5000.00")
        self.assertEqual(payload["emergency_reserve_shortfall"], "0.00")
        self.assertEqual(payload["monthly_safety_residual"], "4321.00")

    def test_large_plain_and_positive_exponent_values_encode_identically(self) -> None:
        positive_plain = Decimal("1" + "0" * 80 + ".00")
        positive_exponent = Decimal("1E+80")
        negative_plain = Decimal("-" + "1" + "0" * 80 + ".00")
        negative_exponent = Decimal("-1E+80")

        plain = replace(
            assessment_amounts(),
            verified_emergency_reserve=positive_plain,
            monthly_safety_residual=negative_plain,
        )
        exponent = replace(
            assessment_amounts(),
            verified_emergency_reserve=positive_exponent,
            monthly_safety_residual=negative_exponent,
        )

        self.assertEqual(
            encode_assessment_amounts(plain),
            encode_assessment_amounts(exponent),
        )

    def test_large_zero_exponents_normalize_positive_and_negative_zero(self) -> None:
        positive_zero = replace(
            assessment_amounts(),
            emergency_reserve_shortfall=Decimal("0E+80"),
        )
        negative_zero = replace(
            assessment_amounts(),
            emergency_reserve_shortfall=Decimal("-0E+80"),
        )

        self.assertEqual(
            encode_assessment_amounts(positive_zero),
            encode_assessment_amounts(negative_zero),
        )
        self.assertIn(
            b'"emergency_reserve_shortfall":"0.00"',
            encode_assessment_amounts(negative_zero),
        )

    def test_decoder_rejects_float_and_nonstandard_constants(self) -> None:
        encoded = encode_assessment_amounts(assessment_amounts())
        for invalid_value, message in (
            ("1.25", "JSON floating-point values are not allowed"),
            ("NaN", "JSON constant values are not allowed"),
            ("Infinity", "JSON constant values are not allowed"),
            ("-Infinity", "JSON constant values are not allowed"),
        ):
            with self.subTest(invalid_value=invalid_value):
                invalid = encoded.replace(b'"0.00"', invalid_value.encode("ascii"), 1)
                with self.assertRaisesRegex(ValueError, message) as raised:
                    decode_assessment_amounts(invalid)
                self.assertNotIn(invalid_value, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_decoder_redacts_malformed_json_and_unicode_payloads(self) -> None:
        sentinel = "recognizable-private-assessment-sentinel"
        for invalid in (
            ('{"' + sentinel + '":').encode("utf-8"),
            b'{"safe_monthly_ceiling":"1000.00"}\xff',
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError,
                    "^assessment amounts JSON is invalid$",
                ) as raised:
                    decode_assessment_amounts(invalid)
                self.assertNotIn(sentinel, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_decoder_requires_exact_keys(self) -> None:
        payload = json.loads(encode_assessment_amounts(assessment_amounts()))
        payload["extra"] = "1"
        with self.assertRaisesRegex(ValueError, "unexpected assessment amount keys"):
            decode_assessment_amounts(json.dumps(payload).encode("utf-8"))

        del payload["extra"]
        del payload["safe_monthly_ceiling"]
        with self.assertRaisesRegex(ValueError, "missing assessment amount keys"):
            decode_assessment_amounts(json.dumps(payload).encode("utf-8"))

    def test_decoder_rejects_wrong_types_invalid_and_nonfinite_decimals(self) -> None:
        payload = json.loads(encode_assessment_amounts(assessment_amounts()))
        for invalid_value, message in (
            (1, "must be encoded as a decimal string"),
            (True, "must be encoded as a decimal string"),
            (None, "must be encoded as a decimal string"),
            ("not-a-decimal", "must be a valid decimal"),
            ("NaN", "must be finite"),
            ("Infinity", "must be finite"),
        ):
            with self.subTest(invalid_value=invalid_value):
                payload["monthly_safety_residual"] = invalid_value
                with self.assertRaisesRegex(ValueError, message):
                    decode_assessment_amounts(json.dumps(payload).encode("utf-8"))

    def test_decoder_rejects_noncanonical_decimal_strings(self) -> None:
        payload = json.loads(encode_assessment_amounts(assessment_amounts()))
        for invalid_value in ("1000", "1000.0", "1E+3", "-0.00", "1.001"):
            with self.subTest(invalid_value=invalid_value):
                payload["safe_monthly_ceiling"] = invalid_value
                with self.assertRaisesRegex(
                    ValueError,
                    "canonical decimal string|whole cents",
                ):
                    decode_assessment_amounts(json.dumps(payload).encode("utf-8"))

    def test_decoder_rejects_duplicate_keys(self) -> None:
        encoded = encode_assessment_amounts(assessment_amounts())
        duplicate = encoded.replace(
            b'{',
            b'{"safe_monthly_ceiling":"999.00",',
            1,
        )

        with self.assertRaisesRegex(ValueError, "duplicate assessment amount key"):
            decode_assessment_amounts(duplicate)

    def test_decoder_rejects_noncanonical_json_layout(self) -> None:
        payload = json.loads(encode_assessment_amounts(assessment_amounts()))
        noncanonical = json.dumps(payload, sort_keys=False).encode("utf-8")

        with self.assertRaisesRegex(ValueError, "JSON is not canonical"):
            decode_assessment_amounts(noncanonical)

    def test_decoder_rejects_non_object_and_non_bytes_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            decode_assessment_amounts(b"[]")
        with self.assertRaisesRegex(ValueError, "must be bytes"):
            decode_assessment_amounts("{}")


class AssessmentCipherTest(unittest.TestCase):
    def test_domain_constants_are_stable(self) -> None:
        self.assertEqual(AssessmentCipher.ALGORITHM, "AES-256-GCM")
        self.assertEqual(AssessmentCipher.KEY_VERSION, "1")
        self.assertEqual(
            AssessmentCipher.KEY_INFO,
            b"kunjin/suitability-assessment/encryption/v1",
        )
        self.assertEqual(
            AssessmentCipher.ASSOCIATED_DATA,
            b"kunjin/suitability-assessment/v1",
        )
        self.assertEqual(
            AssessmentCipher.FINGERPRINT_INFO,
            b"kunjin/suitability-assessment/fingerprint/v1",
        )

    def test_round_trip_uses_random_nonce_and_stable_fingerprint(self) -> None:
        key_store = FakeProfileKeyStore()
        cipher = AssessmentCipher(key_store)
        encoded = encode_assessment_amounts(assessment_amounts())

        first = cipher.encrypt(encoded)
        second = cipher.encrypt(encoded)

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.keyed_fingerprint, second.keyed_fingerprint)
        self.assertEqual(cipher.decrypt(first), encoded)

    def test_tampering_is_rejected_without_plaintext(self) -> None:
        cipher = AssessmentCipher(FakeProfileKeyStore())
        encrypted = cipher.encrypt(b"private-assessment-amounts")
        tampered = replace(
            encrypted,
            ciphertext=encrypted.ciphertext[:-2] + "AA",
        )

        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^assessment decryption failed$",
        ) as raised:
            cipher.decrypt(tampered)

        self.assertNotIn("private-assessment-amounts", str(raised.exception))
        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")

    def test_invalid_metadata_is_rejected(self) -> None:
        cipher = AssessmentCipher(FakeProfileKeyStore())
        encrypted = cipher.encrypt(b"assessment")
        invalid_values = (
            replace(encrypted, algorithm="AES-128-GCM"),
            replace(encrypted, key_version="2"),
            replace(encrypted, nonce="not base64!"),
            replace(encrypted, ciphertext="not base64!"),
            replace(encrypted, keyed_fingerprint="not-a-fingerprint"),
        )

        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProfileCryptoError,
                    "^assessment decryption failed$",
                ):
                    cipher.decrypt(invalid)

    def test_decrypt_and_fingerprint_never_create_or_replace_a_missing_key(self) -> None:
        key_store = FakeProfileKeyStore()
        cipher = AssessmentCipher(key_store)
        encrypted = cipher.encrypt(b"assessment")
        self.assertEqual(key_store.load_or_create_calls, 1)
        key_store.key = None

        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^profile encryption key is unavailable$",
        ):
            cipher.decrypt(encrypted)
        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^profile encryption key is unavailable$",
        ):
            cipher.fingerprint(b"input")

        self.assertIsNone(key_store.key)
        self.assertEqual(key_store.load_or_create_calls, 1)

    def test_malformed_existing_key_is_rejected_without_replacement(self) -> None:
        key_store = FakeProfileKeyStore(b"too-short")
        cipher = AssessmentCipher(key_store)

        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^profile encryption key is unavailable$",
        ):
            cipher.fingerprint(b"input")

        self.assertEqual(key_store.key, b"too-short")
        self.assertEqual(key_store.load_or_create_calls, 0)

    def test_fingerprint_is_deterministic_lowercase_hmac_sha256(self) -> None:
        cipher = AssessmentCipher(FakeProfileKeyStore(bytes(range(32))))

        first = cipher.fingerprint(b"same-input")
        second = cipher.fingerprint(b"same-input")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertTrue(all(character in "0123456789abcdef" for character in first))
        self.assertNotEqual(first, cipher.fingerprint(b"different-input"))

    @patch("kunjin.suitability.crypto.os.urandom", return_value=b"n" * 12)
    def test_hkdf_encryption_and_hmac_match_independent_known_vector(self, _) -> None:
        master_key = bytes(range(32))
        payload = b"known-assessment-payload"
        cipher = AssessmentCipher(FakeProfileKeyStore(master_key))

        fingerprint_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"kunjin/suitability-assessment/fingerprint/v1",
        ).derive(master_key)
        expected_fingerprint = hmac.new(
            fingerprint_key,
            payload,
            hashlib.sha256,
        ).hexdigest()
        encryption_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=b"kunjin/suitability-assessment/encryption/v1",
        ).derive(master_key)
        expected_ciphertext = AESGCM(encryption_key).encrypt(
            b"n" * 12,
            payload,
            b"kunjin/suitability-assessment/v1",
        )

        encrypted = cipher.encrypt(payload)

        self.assertEqual(
            fingerprint_key.hex(),
            "16739cefa662a62cd3c177069f5b56cad7184341c937e55e1e738d4766d82154",
        )
        self.assertEqual(
            expected_fingerprint,
            "7eebea97b72a1318cad21d7e1921e5b1383cc9700ab726a91b9121af6ca34029",
        )
        self.assertEqual(cipher.fingerprint(payload), expected_fingerprint)
        self.assertEqual(base64.urlsafe_b64decode(encrypted.ciphertext), expected_ciphertext)

    @patch("kunjin.suitability.crypto.os.urandom", return_value=b"n" * 12)
    def test_profile_and_assessment_domains_differ_under_same_nonce(self, _) -> None:
        key_store = FakeProfileKeyStore(bytes(range(32)))
        profile_cipher = ProfileCipher(key_store)
        assessment_cipher = AssessmentCipher(key_store)
        encoded = encode_assessment_amounts(AssessmentAmounts.zero())

        profile_encrypted = profile_cipher.encrypt(encoded)
        assessment_encrypted = assessment_cipher.encrypt(encoded)

        self.assertEqual(profile_encrypted.nonce, assessment_encrypted.nonce)
        self.assertNotEqual(profile_encrypted.ciphertext, assessment_encrypted.ciphertext)
        self.assertNotEqual(
            profile_encrypted.keyed_fingerprint,
            assessment_encrypted.keyed_fingerprint,
        )
        with self.assertRaisesRegex(ProfileCryptoError, "assessment decryption failed"):
            assessment_cipher.decrypt(
                EncryptedAssessment(
                    profile_encrypted.algorithm,
                    profile_encrypted.key_version,
                    profile_encrypted.nonce,
                    profile_encrypted.ciphertext,
                    profile_encrypted.keyed_fingerprint,
                )
            )
        with self.assertRaisesRegex(ProfileCryptoError, "profile decryption failed"):
            profile_cipher.decrypt(
                type(profile_encrypted)(
                    assessment_encrypted.algorithm,
                    assessment_encrypted.key_version,
                    assessment_encrypted.nonce,
                    assessment_encrypted.ciphertext,
                    assessment_encrypted.keyed_fingerprint,
                )
            )

    def test_encrypted_assessment_metadata_fields_are_stable(self) -> None:
        encrypted = AssessmentCipher(FakeProfileKeyStore()).encrypt(b"assessment")

        self.assertIsInstance(encrypted, EncryptedAssessment)
        self.assertEqual(encrypted.algorithm, "AES-256-GCM")
        self.assertEqual(encrypted.key_version, "1")
        self.assertEqual(len(base64.urlsafe_b64decode(encrypted.nonce)), 12)


if __name__ == "__main__":
    unittest.main()
