from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import EncryptedProfile
from kunjin.suitability.store import ProfileInvalidationReason, ProfileStore

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
VALID_UNTIL = NOW + timedelta(days=90)


def encrypted(marker: str) -> EncryptedProfile:
    return EncryptedProfile(
        algorithm="AES-256-GCM",
        key_version="1",
        nonce=f"nonce-{marker}",
        ciphertext=f"ciphertext-{marker}",
        keyed_fingerprint=marker * 64,
    )


class ProfileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.store = ProfileStore(self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_first_confirmation_creates_confirmed_version_one(self) -> None:
        metadata = self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)

        self.assertEqual(metadata.version, 1)
        self.assertEqual(metadata.status, "confirmed")
        self.assertEqual(metadata.confirmed_at, NOW)
        self.assertEqual(metadata.valid_until, VALID_UNTIL)

        stored = self.store.encrypted_by_id(metadata.id)
        self.assertEqual(stored.metadata, metadata)
        self.assertEqual(stored.encrypted, encrypted("a"))
        self.assertIsNone(self.store.encrypted_by_id(metadata.id + 1))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.store.encrypted_by_id(0)

    def test_second_confirmation_supersedes_first_in_one_transaction(self) -> None:
        first = self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)
        second_time = NOW + timedelta(days=1)

        second = self.store.confirm(encrypted("b"), second_time, second_time + timedelta(days=90))

        self.assertEqual(second.version, 2)
        self.assertEqual(
            [(item.version, item.status) for item in self.store.history()],
            [(2, "confirmed"), (1, "superseded")],
        )
        with self.repository.connect() as connection:
            confirmed_count = connection.execute(
                "SELECT COUNT(*) FROM financial_profile_versions WHERE status = 'confirmed'"
            ).fetchone()[0]
        self.assertEqual(confirmed_count, 1)
        self.assertNotEqual(first.id, second.id)

    def test_failed_insert_rolls_back_superseding_the_active_version(self) -> None:
        self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)
        with self.repository.connect() as connection, connection:
            connection.executescript(
                """
                CREATE TRIGGER fail_second_profile
                BEFORE INSERT ON financial_profile_versions
                WHEN NEW.version = 2
                BEGIN
                    SELECT RAISE(ABORT, 'forced profile insert failure');
                END;
                """
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced profile"):
            self.store.confirm(
                encrypted("b"),
                NOW + timedelta(days=1),
                VALID_UNTIL + timedelta(days=1),
            )

        self.assertEqual(
            [(item.version, item.status) for item in self.store.history()],
            [(1, "confirmed")],
        )

    def test_invalidation_changes_only_lifecycle_fields_and_is_terminal(self) -> None:
        self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)
        with self.repository.connect() as connection:
            before = dict(
                connection.execute(
                    "SELECT * FROM financial_profile_versions WHERE version = 1"
                ).fetchone()
            )

        invalidated_at = NOW + timedelta(hours=1)
        metadata = self.store.invalidate_active(
            ProfileInvalidationReason.INCOME_CHANGE, invalidated_at
        )

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.status, "invalidated")
        self.assertEqual(metadata.invalidated_at, invalidated_at)
        self.assertEqual(metadata.invalidation_reason, "income_change")
        self.assertIsNone(self.store.invalidate_active("user_requested", invalidated_at))
        with self.repository.connect() as connection:
            after = dict(
                connection.execute(
                    "SELECT * FROM financial_profile_versions WHERE version = 1"
                ).fetchone()
            )
        for key in (
            "id",
            "version",
            "encryption_algorithm",
            "encryption_key_version",
            "nonce",
            "encrypted_payload",
            "keyed_payload_fingerprint",
            "confirmed_at",
            "valid_until",
            "created_at",
        ):
            self.assertEqual(after[key], before[key], key)

    def test_history_is_newest_first_and_excludes_encrypted_payload(self) -> None:
        self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)
        self.store.confirm(
            encrypted("b"),
            NOW + timedelta(days=1),
            VALID_UNTIL + timedelta(days=1),
        )

        history = self.store.history()

        self.assertEqual([item.version for item in history], [2, 1])
        self.assertFalse(hasattr(history[0], "encrypted"))
        self.assertNotIn("ciphertext", repr(history))

    def test_loading_active_encrypted_row_returns_all_encryption_metadata(self) -> None:
        expected = encrypted("a")
        stored = self.store.confirm(expected, NOW, VALID_UNTIL)

        active = self.store.active_encrypted()

        self.assertIsNotNone(active)
        self.assertEqual(active.metadata, stored)
        self.assertEqual(active.encrypted, expected)

    def test_rejects_naive_datetimes_and_free_form_invalidation_reason(self) -> None:
        naive = datetime(2026, 7, 12, 12)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.confirm(encrypted("a"), naive, VALID_UNTIL)

        self.store.confirm(encrypted("a"), NOW, VALID_UNTIL)
        with self.assertRaisesRegex(ValueError, "supported invalidation code"):
            self.store.invalidate_active("   ", NOW)
        with self.assertRaisesRegex(ValueError, "supported invalidation code"):
            self.store.invalidate_active("income fell to 8000", NOW)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self.store.invalidate_active("income_change", naive)


if __name__ == "__main__":
    unittest.main()
