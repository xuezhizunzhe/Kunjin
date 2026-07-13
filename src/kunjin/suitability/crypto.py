from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import hmac
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from kunjin.security.keychain import CredentialStoreError, KeychainTokenStore


@dataclass(frozen=True)
class EncryptedProfile:
    algorithm: str
    key_version: str
    nonce: str
    ciphertext: str
    keyed_fingerprint: str


@dataclass(frozen=True)
class EncryptedAssessment:
    algorithm: str
    key_version: str
    nonce: str
    ciphertext: str
    keyed_fingerprint: str


class ProfileCryptoError(RuntimeError):
    code = "encrypted_profile_unavailable"


class ProfileKeyStore:
    SERVICE = "com.kunjin.profile-encryption"
    ACCOUNT = "v1"
    KEY_BYTES = 32
    _CREATE_LOCK = threading.Lock()

    def __init__(self, token_store: Optional[KeychainTokenStore] = None) -> None:
        self._token_store = token_store or KeychainTokenStore(
            service=self.SERVICE,
            account=self.ACCOUNT,
        )

    def save_key(self, key: bytes) -> None:
        if not isinstance(key, bytes) or len(key) != self.KEY_BYTES:
            raise ProfileCryptoError("profile encryption key is unavailable")
        encoded = base64.urlsafe_b64encode(key).decode("ascii")
        try:
            self._token_store.save(encoded)
        except (CredentialStoreError, OSError, ValueError):
            raise ProfileCryptoError("profile encryption key is unavailable") from None

    def load_existing_key(self) -> Optional[bytes]:
        try:
            encoded = self._token_store.load()
        except (CredentialStoreError, OSError, ValueError):
            raise ProfileCryptoError("profile encryption key is unavailable") from None
        if encoded is None:
            return None
        try:
            return _decode_base64(encoded, expected_length=self.KEY_BYTES)
        except (TypeError, ValueError, UnicodeError, binascii.Error):
            raise ProfileCryptoError("profile encryption key is unavailable") from None

    def load_or_create_key(self) -> bytes:
        with self._CREATE_LOCK:
            try:
                lock_path = os.path.join(
                    tempfile.gettempdir(),
                    f"kunjin-profile-encryption-{os.getuid()}-v1.lock",
                )
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    key = self.load_existing_key()
                    if key is not None:
                        return key
                    self.save_key(os.urandom(self.KEY_BYTES))
                    stored_key = self.load_existing_key()
                    if stored_key is None:
                        raise ProfileCryptoError(
                            "profile encryption key is unavailable"
                        )
                    return stored_key
                finally:
                    os.close(lock_fd)
            except ProfileCryptoError:
                raise
            except (OSError, ValueError):
                raise ProfileCryptoError(
                    "profile encryption key is unavailable"
                ) from None


class ProfileCipher:
    ALGORITHM = "AES-256-GCM"
    KEY_VERSION = "1"
    ASSOCIATED_DATA = b"kunjin:financial-profile:v1"
    FINGERPRINT_INFO = b"kunjin:financial-profile:fingerprint:v1"
    NONCE_BYTES = 12
    KEY_BYTES = 32

    def __init__(self, key_store: Any) -> None:
        self._key_store = key_store

    def encrypt(self, plaintext: bytes) -> EncryptedProfile:
        if not isinstance(plaintext, bytes):
            raise ProfileCryptoError("profile encryption failed")
        key = self._load_or_create_key()
        nonce = os.urandom(self.NONCE_BYTES)
        try:
            ciphertext = AESGCM(key).encrypt(
                nonce,
                plaintext,
                self.ASSOCIATED_DATA,
            )
            fingerprint = self._fingerprint(key, plaintext)
        except (TypeError, ValueError):
            raise ProfileCryptoError("profile encryption failed") from None
        return EncryptedProfile(
            algorithm=self.ALGORITHM,
            key_version=self.KEY_VERSION,
            nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            keyed_fingerprint=fingerprint,
        )

    def decrypt(self, encrypted: EncryptedProfile) -> bytes:
        self._validate_metadata(encrypted)
        key = self._load_existing_key()
        if key is None:
            raise ProfileCryptoError("profile encryption key is unavailable")
        try:
            nonce = _decode_base64(
                encrypted.nonce,
                expected_length=self.NONCE_BYTES,
            )
            ciphertext = _decode_base64(encrypted.ciphertext)
            if len(ciphertext) < 16:
                raise ValueError("ciphertext is shorter than the GCM tag")
            plaintext = AESGCM(key).decrypt(
                nonce,
                ciphertext,
                self.ASSOCIATED_DATA,
            )
            expected_fingerprint = self._fingerprint(key, plaintext)
            if not hmac.compare_digest(
                encrypted.keyed_fingerprint,
                expected_fingerprint,
            ):
                raise ValueError("fingerprint mismatch")
            return plaintext
        except (
            InvalidTag,
            TypeError,
            ValueError,
            UnicodeError,
            binascii.Error,
        ):
            raise ProfileCryptoError("profile decryption failed") from None

    def _load_existing_key(self) -> Optional[bytes]:
        try:
            if hasattr(self._key_store, "load_existing_key"):
                key = self._key_store.load_existing_key()
            else:
                stored = self._key_store.load()
                key = None if stored is None else _decode_base64(stored, self.KEY_BYTES)
        except ProfileCryptoError:
            raise
        except Exception:
            raise ProfileCryptoError("profile encryption key is unavailable") from None
        if key is not None and (
            not isinstance(key, bytes) or len(key) != self.KEY_BYTES
        ):
            raise ProfileCryptoError("profile encryption key is unavailable")
        return key

    def _load_or_create_key(self) -> bytes:
        try:
            if hasattr(self._key_store, "load_or_create_key"):
                key = self._key_store.load_or_create_key()
            else:
                key = self._load_existing_key()
                if key is None:
                    key = os.urandom(self.KEY_BYTES)
                    self._key_store.save(
                        base64.urlsafe_b64encode(key).decode("ascii")
                    )
        except ProfileCryptoError:
            raise
        except Exception:
            raise ProfileCryptoError("profile encryption key is unavailable") from None
        if not isinstance(key, bytes) or len(key) != self.KEY_BYTES:
            raise ProfileCryptoError("profile encryption key is unavailable")
        return key

    def _fingerprint(self, key: bytes, plaintext: bytes) -> str:
        fingerprint_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=self.FINGERPRINT_INFO,
        ).derive(key)
        return hmac.new(fingerprint_key, plaintext, hashlib.sha256).hexdigest()

    def _validate_metadata(self, encrypted: EncryptedProfile) -> None:
        valid = (
            isinstance(encrypted, EncryptedProfile)
            and encrypted.algorithm == self.ALGORITHM
            and encrypted.key_version == self.KEY_VERSION
            and isinstance(encrypted.nonce, str)
            and isinstance(encrypted.ciphertext, str)
            and isinstance(encrypted.keyed_fingerprint, str)
            and len(encrypted.keyed_fingerprint) == 64
            and all(
                character in "0123456789abcdef"
                for character in encrypted.keyed_fingerprint
            )
        )
        if not valid:
            raise ProfileCryptoError("profile decryption failed")


class AssessmentCipher:
    ALGORITHM = "AES-256-GCM"
    KEY_VERSION = "1"
    KEY_INFO = b"kunjin/suitability-assessment/encryption/v1"
    ASSOCIATED_DATA = b"kunjin/suitability-assessment/v1"
    FINGERPRINT_INFO = b"kunjin/suitability-assessment/fingerprint/v1"
    NONCE_BYTES = 12
    KEY_BYTES = 32

    def __init__(self, key_store: Any) -> None:
        self._key_store = key_store

    def encrypt(self, plaintext: bytes) -> EncryptedAssessment:
        if not isinstance(plaintext, bytes):
            raise ProfileCryptoError("assessment encryption failed")
        master_key = self._load_or_create_key()
        nonce = os.urandom(self.NONCE_BYTES)
        try:
            encryption_key = self._derive_key(master_key, self.KEY_INFO)
            ciphertext = AESGCM(encryption_key).encrypt(
                nonce,
                plaintext,
                self.ASSOCIATED_DATA,
            )
            fingerprint = self._fingerprint(master_key, plaintext)
        except (TypeError, ValueError):
            raise ProfileCryptoError("assessment encryption failed") from None
        return EncryptedAssessment(
            algorithm=self.ALGORITHM,
            key_version=self.KEY_VERSION,
            nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
            ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
            keyed_fingerprint=fingerprint,
        )

    def decrypt(self, encrypted: EncryptedAssessment) -> bytes:
        self._validate_metadata(encrypted)
        master_key = self._load_existing_key()
        if master_key is None:
            raise ProfileCryptoError("profile encryption key is unavailable")
        try:
            nonce = _decode_base64(
                encrypted.nonce,
                expected_length=self.NONCE_BYTES,
            )
            ciphertext = _decode_base64(encrypted.ciphertext)
            if len(ciphertext) < 16:
                raise ValueError("ciphertext is shorter than the GCM tag")
            encryption_key = self._derive_key(master_key, self.KEY_INFO)
            plaintext = AESGCM(encryption_key).decrypt(
                nonce,
                ciphertext,
                self.ASSOCIATED_DATA,
            )
            expected_fingerprint = self._fingerprint(master_key, plaintext)
            if not hmac.compare_digest(
                encrypted.keyed_fingerprint,
                expected_fingerprint,
            ):
                raise ValueError("fingerprint mismatch")
            return plaintext
        except (
            InvalidTag,
            TypeError,
            ValueError,
            UnicodeError,
            binascii.Error,
        ):
            raise ProfileCryptoError("assessment decryption failed") from None

    def fingerprint(self, payload: bytes) -> str:
        if not isinstance(payload, bytes):
            raise ProfileCryptoError("assessment fingerprint failed")
        master_key = self._load_existing_key()
        if master_key is None:
            raise ProfileCryptoError("profile encryption key is unavailable")
        try:
            return self._fingerprint(master_key, payload)
        except (TypeError, ValueError):
            raise ProfileCryptoError("assessment fingerprint failed") from None

    def _load_existing_key(self) -> Optional[bytes]:
        try:
            if hasattr(self._key_store, "load_existing_key"):
                key = self._key_store.load_existing_key()
            else:
                stored = self._key_store.load()
                key = None if stored is None else _decode_base64(stored, self.KEY_BYTES)
        except ProfileCryptoError:
            raise
        except Exception:
            raise ProfileCryptoError("profile encryption key is unavailable") from None
        if key is not None and (
            not isinstance(key, bytes) or len(key) != self.KEY_BYTES
        ):
            raise ProfileCryptoError("profile encryption key is unavailable")
        return key

    def _load_or_create_key(self) -> bytes:
        try:
            if hasattr(self._key_store, "load_or_create_key"):
                key = self._key_store.load_or_create_key()
            else:
                key = self._load_existing_key()
                if key is None:
                    key = os.urandom(self.KEY_BYTES)
                    self._key_store.save(
                        base64.urlsafe_b64encode(key).decode("ascii")
                    )
        except ProfileCryptoError:
            raise
        except Exception:
            raise ProfileCryptoError("profile encryption key is unavailable") from None
        if not isinstance(key, bytes) or len(key) != self.KEY_BYTES:
            raise ProfileCryptoError("profile encryption key is unavailable")
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

    def _validate_metadata(self, encrypted: EncryptedAssessment) -> None:
        valid = (
            isinstance(encrypted, EncryptedAssessment)
            and encrypted.algorithm == self.ALGORITHM
            and encrypted.key_version == self.KEY_VERSION
            and isinstance(encrypted.nonce, str)
            and isinstance(encrypted.ciphertext, str)
            and isinstance(encrypted.keyed_fingerprint, str)
            and len(encrypted.keyed_fingerprint) == 64
            and all(
                character in "0123456789abcdef"
                for character in encrypted.keyed_fingerprint
            )
        )
        if not valid:
            raise ProfileCryptoError("assessment decryption failed")


def _decode_base64(value: str, expected_length: Optional[int] = None) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("base64 value is required")
    encoded = value.encode("ascii")
    decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 value is not canonical")
    if expected_length is not None and len(decoded) != expected_length:
        raise ValueError("decoded value has an invalid length")
    return decoded
