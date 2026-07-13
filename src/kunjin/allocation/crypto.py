from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass, fields
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kunjin.allocation.serialization import MAX_EXACT_PAYLOAD_BYTES
from kunjin.suitability.crypto import ProfileCryptoError

ALLOCATION_ENCRYPTION_INFO = b"kunjin/allocation-assessment/encryption/v1"
ALLOCATION_FINGERPRINT_INFO = b"kunjin/allocation-assessment/fingerprint/v1"
ALLOCATION_ASSOCIATED_DATA = b"kunjin/allocation-assessment/v1"


@dataclass(frozen=True)
class EncryptedAllocationAssessment:
    algorithm: str
    key_version: str
    nonce: str
    ciphertext: str
    keyed_fingerprint: str


class AllocationCipher:
    ALGORITHM = "AES-256-GCM"
    KEY_VERSION = "1"
    KEY_INFO = ALLOCATION_ENCRYPTION_INFO
    ASSOCIATED_DATA = ALLOCATION_ASSOCIATED_DATA
    FINGERPRINT_INFO = ALLOCATION_FINGERPRINT_INFO
    NONCE_BYTES = 12
    KEY_BYTES = 32

    def __init__(self, key_store: Any) -> None:
        self._key_store = key_store

    def encrypt(self, plaintext: bytes) -> EncryptedAllocationAssessment:
        if type(plaintext) is not bytes or len(plaintext) > MAX_EXACT_PAYLOAD_BYTES:
            raise ProfileCryptoError("allocation encryption failed")
        master_key = self._load_or_create_key()
        nonce = os.urandom(self.NONCE_BYTES)
        try:
            ciphertext = AESGCM(self._derive_key(master_key, self.KEY_INFO)).encrypt(
                nonce,
                plaintext,
                self.ASSOCIATED_DATA,
            )
            fingerprint = self._fingerprint(master_key, plaintext)
        except (TypeError, ValueError):
            raise ProfileCryptoError("allocation encryption failed") from None
        return EncryptedAllocationAssessment(
            algorithm=self.ALGORITHM,
            key_version=self.KEY_VERSION,
            nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            keyed_fingerprint=fingerprint,
        )

    def decrypt(self, value: EncryptedAllocationAssessment) -> bytes:
        self._validate_metadata(value)
        master_key = self._load_existing_key()
        if master_key is None:
            raise ProfileCryptoError("allocation key is unavailable")
        try:
            nonce = _decode_base64(value.nonce, expected_length=self.NONCE_BYTES)
            ciphertext = _decode_base64(
                value.ciphertext,
                maximum_decoded_length=MAX_EXACT_PAYLOAD_BYTES + 16,
            )
            if len(ciphertext) < 16:
                raise ValueError("ciphertext is shorter than the GCM tag")
            plaintext = AESGCM(self._derive_key(master_key, self.KEY_INFO)).decrypt(
                nonce,
                ciphertext,
                self.ASSOCIATED_DATA,
            )
            expected = self._fingerprint(master_key, plaintext)
            if not hmac.compare_digest(value.keyed_fingerprint, expected):
                raise ValueError("fingerprint mismatch")
            return plaintext
        except (InvalidTag, TypeError, ValueError, UnicodeError, binascii.Error):
            raise ProfileCryptoError("allocation decryption failed") from None

    def fingerprint(self, payload: bytes) -> str:
        if type(payload) is not bytes or len(payload) > MAX_EXACT_PAYLOAD_BYTES:
            raise ProfileCryptoError("allocation fingerprint failed")
        master_key = self._load_existing_key()
        if master_key is None:
            raise ProfileCryptoError("allocation key is unavailable")
        try:
            return self._fingerprint(master_key, payload)
        except (TypeError, ValueError):
            raise ProfileCryptoError("allocation fingerprint failed") from None

    def _load_existing_key(self) -> Optional[bytes]:
        try:
            key = self._key_store.load_existing_key()
        except Exception:
            raise ProfileCryptoError("allocation key is unavailable") from None
        if key is not None and (type(key) is not bytes or len(key) != self.KEY_BYTES):
            raise ProfileCryptoError("allocation key is unavailable")
        return key

    def _load_or_create_key(self) -> bytes:
        try:
            key = self._key_store.load_or_create_key()
        except Exception:
            raise ProfileCryptoError("allocation key is unavailable") from None
        if type(key) is not bytes or len(key) != self.KEY_BYTES:
            raise ProfileCryptoError("allocation key is unavailable")
        return key

    @staticmethod
    def _derive_key(master_key: bytes, info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(master_key)

    def _fingerprint(self, master_key: bytes, payload: bytes) -> str:
        fingerprint_key = self._derive_key(master_key, self.FINGERPRINT_INFO)
        return hmac.new(fingerprint_key, payload, hashlib.sha256).hexdigest()

    def _validate_metadata(self, value: EncryptedAllocationAssessment) -> None:
        declared = {item.name for item in fields(EncryptedAllocationAssessment)}
        valid = (
            type(value) is EncryptedAllocationAssessment
            and type(vars(value)) is dict
            and set(vars(value)) == declared
            and value.algorithm == self.ALGORITHM
            and value.key_version == self.KEY_VERSION
            and type(value.nonce) is str
            and len(value.nonce) == _base64_encoded_length(self.NONCE_BYTES)
            and type(value.ciphertext) is str
            and _base64_encoded_length(16)
            <= len(value.ciphertext)
            <= _base64_encoded_length(MAX_EXACT_PAYLOAD_BYTES + 16)
            and len(value.ciphertext) % 4 == 0
            and type(value.keyed_fingerprint) is str
            and len(value.keyed_fingerprint) == 64
            and all(character in "0123456789abcdef" for character in value.keyed_fingerprint)
        )
        if not valid:
            raise ProfileCryptoError("allocation decryption failed")


def _decode_base64(
    value: str,
    expected_length: Optional[int] = None,
    maximum_decoded_length: Optional[int] = None,
) -> bytes:
    if type(value) is not str or not value:
        raise ValueError("base64 value is required")
    if expected_length is not None and len(value) != _base64_encoded_length(expected_length):
        raise ValueError("decoded value has an invalid length")
    if maximum_decoded_length is not None and len(value) > _base64_encoded_length(
        maximum_decoded_length
    ):
        raise ValueError("decoded value has an invalid length")
    if len(value) % 4:
        raise ValueError("base64 value is not canonical")
    encoded = value.encode("ascii")
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 value is not canonical")
    if expected_length is not None and len(decoded) != expected_length:
        raise ValueError("decoded value has an invalid length")
    if maximum_decoded_length is not None and len(decoded) > maximum_decoded_length:
        raise ValueError("decoded value has an invalid length")
    return decoded


def _base64_encoded_length(decoded_length: int) -> int:
    return 4 * ((decoded_length + 2) // 3)
