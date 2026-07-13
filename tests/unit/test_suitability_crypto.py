from __future__ import annotations

import base64
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from kunjin.security.keychain import KeychainTokenStore
from kunjin.suitability.crypto import (
    EncryptedProfile,
    ProfileCipher,
    ProfileCryptoError,
    ProfileKeyStore,
)


class MemorySecretStore:
    def __init__(self) -> None:
        self.value = None

    def load(self):
        return self.value

    def save(self, value):
        self.value = value

    def delete(self):
        self.value = None


class RaceCapableTokenStore:
    def __init__(self) -> None:
        self.value = None
        self.saved_values = []
        self._active_loads = 0
        self._peer_entered = threading.Event()
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            self._active_loads += 1
            if self._active_loads > 1:
                self._peer_entered.set()
            current = self.value
        if current is None:
            self._peer_entered.wait(0.05)
        with self._lock:
            self._active_loads -= 1
            return self.value

    def save(self, value):
        time.sleep(0.01)
        with self._lock:
            self.saved_values.append(value)
            self.value = value


class DiscardingTokenStore:
    def load(self):
        return None

    def save(self, value):
        return None


class ProfileCipherTest(unittest.TestCase):
    def test_round_trip_uses_random_nonce_and_stable_keyed_fingerprint(self) -> None:
        store = MemorySecretStore()
        cipher = ProfileCipher(store)

        first = cipher.encrypt(b'{"amount":"12000"}')
        second = cipher.encrypt(b'{"amount":"12000"}')

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)
        self.assertEqual(first.keyed_fingerprint, second.keyed_fingerprint)
        self.assertEqual(cipher.decrypt(first), b'{"amount":"12000"}')

    def test_tampered_ciphertext_is_rejected_without_plaintext(self) -> None:
        cipher = ProfileCipher(MemorySecretStore())
        encrypted = cipher.encrypt(b"private-profile")
        tampered = EncryptedProfile(
            encrypted.algorithm,
            encrypted.key_version,
            encrypted.nonce,
            encrypted.ciphertext[:-2] + "AA",
            encrypted.keyed_fingerprint,
        )

        with self.assertRaisesRegex(
            ProfileCryptoError, "^profile decryption failed$"
        ) as raised:
            cipher.decrypt(tampered)

        self.assertNotIn("private-profile", str(raised.exception))
        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")

    def test_missing_key_does_not_generate_a_replacement_during_decrypt(self) -> None:
        store = MemorySecretStore()
        cipher = ProfileCipher(store)
        encrypted = cipher.encrypt(b"private-profile")
        store.delete()

        with self.assertRaisesRegex(
            ProfileCryptoError, "^profile encryption key is unavailable$"
        ):
            cipher.decrypt(encrypted)

        self.assertIsNone(store.value)

    def test_invalid_metadata_is_rejected(self) -> None:
        cipher = ProfileCipher(MemorySecretStore())
        encrypted = cipher.encrypt(b"private-profile")

        for invalid in (
            EncryptedProfile(
                "AES-128-GCM",
                encrypted.key_version,
                encrypted.nonce,
                encrypted.ciphertext,
                encrypted.keyed_fingerprint,
            ),
            EncryptedProfile(
                encrypted.algorithm,
                "2",
                encrypted.nonce,
                encrypted.ciphertext,
                encrypted.keyed_fingerprint,
            ),
            EncryptedProfile(
                encrypted.algorithm,
                encrypted.key_version,
                encrypted.nonce,
                encrypted.ciphertext,
                "not-a-fingerprint",
            ),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProfileCryptoError, "^profile decryption failed$"
                ):
                    cipher.decrypt(invalid)

    def test_invalid_or_wrong_length_base64_is_rejected(self) -> None:
        cipher = ProfileCipher(MemorySecretStore())
        encrypted = cipher.encrypt(b"private-profile")

        for nonce, ciphertext in (
            ("not base64!", encrypted.ciphertext),
            (base64.urlsafe_b64encode(b"short").decode("ascii"), encrypted.ciphertext),
            (encrypted.nonce, "not base64!"),
        ):
            with self.subTest(nonce=nonce, ciphertext=ciphertext):
                invalid = EncryptedProfile(
                    encrypted.algorithm,
                    encrypted.key_version,
                    nonce,
                    ciphertext,
                    encrypted.keyed_fingerprint,
                )
                with self.assertRaisesRegex(
                    ProfileCryptoError, "^profile decryption failed$"
                ):
                    cipher.decrypt(invalid)


class ProfileKeyStoreTest(unittest.TestCase):
    def test_concurrent_first_use_returns_one_read_back_master_key(self) -> None:
        token_store = RaceCapableTokenStore()
        stores = (ProfileKeyStore(token_store), ProfileKeyStore(token_store))
        results = []
        errors = []

        def load(store):
            try:
                results.append(store.load_or_create_key())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=load, args=(store,)) for store in stores]
        with patch(
            "kunjin.suitability.crypto.os.urandom",
            side_effect=(b"a" * 32, b"b" * 32),
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [b"a" * 32, b"a" * 32])
        self.assertEqual(len(token_store.saved_values), 1)

    @patch("kunjin.suitability.crypto.fcntl.flock")
    def test_interprocess_lock_failure_is_redacted(self, flock_mock) -> None:
        sentinel = "recognizable-lock-failure-secret"
        flock_mock.side_effect = OSError(sentinel)

        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^profile encryption key is unavailable$",
        ) as raised:
            ProfileKeyStore(MemorySecretStore()).load_or_create_key()

        self.assertNotIn(sentinel, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_missing_key_after_save_is_redacted(self) -> None:
        with self.assertRaisesRegex(
            ProfileCryptoError,
            "^profile encryption key is unavailable$",
        ) as raised:
            ProfileKeyStore(DiscardingTokenStore()).load_or_create_key()

        self.assertIsNone(raised.exception.__cause__)

    @patch("kunjin.security.keychain.subprocess.run")
    def test_uses_dedicated_service_and_account_without_shell(self, run_mock) -> None:
        raw_key = bytes(range(32))
        encoded_key = base64.urlsafe_b64encode(raw_key).decode("ascii")
        run_mock.side_effect = (
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, encoded_key + "\n", ""),
        )
        store = ProfileKeyStore()

        store.save_key(raw_key)
        self.assertEqual(store.load_existing_key(), raw_key)

        save_command = run_mock.call_args_list[0].args[0]
        load_command = run_mock.call_args_list[1].args[0]
        for command in (save_command, load_command):
            self.assertIn("com.kunjin.profile-encryption", command)
            self.assertIn("v1", command)
        self.assertFalse(run_mock.call_args_list[0].kwargs["shell"])
        self.assertFalse(run_mock.call_args_list[1].kwargs["shell"])

    def test_invalid_stored_key_is_rejected_without_exposing_it(self) -> None:
        secret = "recognizable-secret-key-material"
        token_store = KeychainTokenStore(
            service="com.kunjin.profile-encryption", account="v1"
        )
        with patch.object(token_store, "load", return_value=secret):
            store = ProfileKeyStore(token_store)
            with self.assertRaisesRegex(
                ProfileCryptoError, "^profile encryption key is unavailable$"
            ) as raised:
                store.load_existing_key()

        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    @patch("kunjin.security.keychain.subprocess.run")
    def test_keychain_error_is_redacted_without_exception_chain(self, run_mock) -> None:
        secret = "recognizable-raw-key-material"
        run_mock.side_effect = subprocess.CalledProcessError(
            1,
            ["security"],
            stderr=secret,
        )

        with self.assertRaisesRegex(
            ProfileCryptoError, "^profile encryption key is unavailable$"
        ) as raised:
            ProfileKeyStore().load_existing_key()

        self.assertNotIn(secret, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_save_rejects_wrong_key_length(self) -> None:
        store = ProfileKeyStore()
        with self.assertRaisesRegex(
            ProfileCryptoError, "^profile encryption key is unavailable$"
        ):
            store.save_key(b"too-short")


if __name__ == "__main__":
    unittest.main()
