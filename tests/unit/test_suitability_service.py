from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import ProfileCipher, ProfileCryptoError
from kunjin.suitability.service import LoadedProfile, ProfileService
from kunjin.suitability.store import ProfileInvalidationReason, ProfileStore
from tests.unit.test_suitability_models import valid_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


class MemoryKeyStore:
    def __init__(self) -> None:
        self.key = None
        self.create_calls = 0
        self.load_calls = 0

    def load_existing_key(self):
        self.load_calls += 1
        return self.key

    def load_or_create_key(self):
        self.create_calls += 1
        if self.key is None:
            self.key = bytes(range(32))
        return self.key


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class ProfileServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(
            Path(self.temporary_directory.name) / "kunjin.db"
        )
        self.repository.migrate()
        self.store = ProfileStore(self.repository)
        self.key_store = MemoryKeyStore()
        self.clock = MutableClock(NOW)
        self.service = ProfileService(
            self.store,
            ProfileCipher(self.key_store),
            now=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_confirmation_validates_encrypts_and_stores_a_90_day_window(self) -> None:
        profile = valid_profile()

        metadata = self.service.confirm_profile(profile)

        self.assertEqual(metadata.confirmed_at, profile.confirmed_at)
        self.assertEqual(
            metadata.valid_until, profile.confirmed_at + timedelta(days=90)
        )
        active = self.store.active_encrypted()
        self.assertIsNotNone(active)
        self.assertNotIn("12000", active.encrypted.ciphertext)
        self.assertEqual(self.key_store.create_calls, 1)

    def test_confirmed_profile_values_are_absent_from_plaintext_sqlite(self) -> None:
        distinctive_values = (
            "918273645001",
            "918273645002",
            "918273645003",
            "918273645004",
        )
        profile = replace(
            valid_profile(),
            monthly_net_income=Decimal(distinctive_values[0]),
            emergency_reserve=Decimal(distinctive_values[1]),
            debts=(
                replace(
                    valid_profile().debts[0],
                    outstanding_principal=Decimal(distinctive_values[2]),
                ),
            ),
            goals=(
                replace(
                    valid_profile().goals[0],
                    target_amount=Decimal(distinctive_values[3]),
                ),
            ),
        )

        self.service.confirm_profile(profile)

        database_bytes = self.repository.database.read_bytes()
        for value in distinctive_values:
            self.assertNotIn(value.encode("utf-8"), database_bytes)
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT encrypted_payload, keyed_payload_fingerprint "
                "FROM financial_profile_versions WHERE status = 'confirmed'"
            ).fetchone()
        self.assertIsNotNone(row)
        stored_values = " ".join(str(value) for value in row)
        for value in distinctive_values:
            self.assertNotIn(value, stored_values)

    def test_confirmation_uses_service_time_for_metadata_and_encrypted_payload(self) -> None:
        caller_times = (
            NOW - timedelta(days=365),
            NOW + timedelta(days=365),
        )

        for caller_time in caller_times:
            with self.subTest(caller_time=caller_time):
                metadata = self.service.confirm_profile(
                    replace(valid_profile(), confirmed_at=caller_time)
                )
                loaded = self.service.load_active_profile()

                self.assertEqual(metadata.confirmed_at, NOW)
                self.assertEqual(metadata.valid_until, NOW + timedelta(days=90))
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded.confirmed_at, NOW)

    def test_load_active_profile_decrypts_and_verifies_the_fingerprint(self) -> None:
        expected = valid_profile()
        self.service.confirm_profile(expected)

        self.assertEqual(self.service.load_active_profile(), expected)

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER financial_profile_payload_no_update")
            connection.execute(
                "UPDATE financial_profile_versions "
                "SET keyed_payload_fingerprint = ? WHERE status = 'confirmed'",
                ("f" * 64,),
            )
        with self.assertRaises(ProfileCryptoError):
            self.service.load_active_profile()

    def test_load_active_returns_authenticated_profile_and_binding_metadata(self) -> None:
        expected = valid_profile()
        metadata = self.service.confirm_profile(expected)
        active = self.store.active_encrypted()

        loaded = self.service.load_active()

        self.assertIsInstance(loaded, LoadedProfile)
        self.assertEqual(loaded.metadata, metadata)
        self.assertEqual(loaded.profile, expected)
        self.assertEqual(
            loaded.encrypted_keyed_fingerprint,
            active.encrypted.keyed_fingerprint,
        )
        self.assertEqual(self.service.load_active_profile(), expected)

    def test_latest_metadata_distinguishes_never_created_from_invalidated(self) -> None:
        self.assertIsNone(self.store.latest_metadata())
        confirmed = self.service.confirm_profile(valid_profile())
        self.assertEqual(self.store.latest_metadata(), confirmed)

        invalidated = self.service.invalidate("income_change")

        self.assertEqual(self.store.latest_metadata(), invalidated)
        self.assertIsNone(self.service.load_active())

    def test_missing_key_fails_closed_without_creating_a_replacement(self) -> None:
        self.service.confirm_profile(valid_profile())
        self.key_store.key = None
        create_calls = self.key_store.create_calls

        with self.assertRaises(ProfileCryptoError) as raised:
            self.service.load_active_profile()

        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")
        self.assertEqual(self.key_store.create_calls, create_calls)
        self.assertIsNone(self.key_store.key)

    def test_status_contains_only_metadata_and_freshness(self) -> None:
        load_calls = self.key_store.load_calls
        self.assertEqual(
            self.service.status(), {"state": "missing", "freshness": "missing"}
        )
        self.assertEqual(self.key_store.load_calls, load_calls)
        self.service.confirm_profile(valid_profile())

        status = self.service.status()

        self.assertEqual(
            set(status),
            {"state", "version", "confirmed_at", "valid_until", "freshness"},
        )
        self.assertEqual(status["state"], "confirmed")
        self.assertEqual(status["freshness"], "fresh")
        serialized = json.dumps(status)
        for sensitive in ("12000", "500000", "40000"):
            self.assertNotIn(sensitive, serialized)

    def test_history_contains_lifecycle_metadata_only(self) -> None:
        self.service.confirm_profile(valid_profile())
        self.service.invalidate(ProfileInvalidationReason.INCOME_CHANGE)

        history = self.service.history()

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["status"], "invalidated")
        self.assertEqual(history[0]["invalidation_reason"], "income_change")
        serialized = json.dumps(history)
        for sensitive in ("12000", "500000", "40000", "ciphertext", "nonce"):
            self.assertNotIn(sensitive, serialized)

    def test_expired_profile_reports_stale_without_mutating_the_database(self) -> None:
        self.service.confirm_profile(valid_profile())
        before = self.store.history()
        self.clock.value = NOW + timedelta(days=91)

        status = self.service.status()

        self.assertEqual(status["state"], "confirmed")
        self.assertEqual(status["freshness"], "stale")
        self.assertEqual(self.store.history(), before)
        self.assertIsNotNone(self.store.active_encrypted())

    def test_profile_is_stale_at_valid_until_under_half_open_interval(self) -> None:
        metadata = self.service.confirm_profile(valid_profile())
        self.clock.value = metadata.valid_until

        status = self.service.status()

        self.assertEqual(status["freshness"], "stale")

    def test_invalidate_uses_the_injected_aware_clock(self) -> None:
        self.service.confirm_profile(valid_profile())
        self.clock.value = NOW + timedelta(hours=2)

        metadata = self.service.invalidate("household_change")

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.invalidated_at, self.clock.value)
        self.assertEqual(
            self.service.status(), {"state": "missing", "freshness": "missing"}
        )

    def test_service_rejects_free_form_invalidation_reason(self) -> None:
        self.service.confirm_profile(valid_profile())

        with self.assertRaisesRegex(ValueError, "supported invalidation code"):
            self.service.invalidate("income fell from 12000 to 8000")

        self.assertEqual(self.service.status()["state"], "confirmed")

    def test_status_propagates_missing_key_without_creating_a_replacement(self) -> None:
        self.service.confirm_profile(valid_profile())
        self.key_store.key = None
        create_calls = self.key_store.create_calls

        with self.assertRaises(ProfileCryptoError) as raised:
            self.service.status()

        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")
        self.assertEqual(self.key_store.create_calls, create_calls)
        self.assertIsNone(self.key_store.key)

    def test_status_rejects_tampered_active_profile(self) -> None:
        self.service.confirm_profile(valid_profile())
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER financial_profile_payload_no_update")
            connection.execute(
                "UPDATE financial_profile_versions "
                "SET keyed_payload_fingerprint = ? WHERE status = 'confirmed'",
                ("f" * 64,),
            )

        with self.assertRaises(ProfileCryptoError) as raised:
            self.service.status()

        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")


if __name__ == "__main__":
    unittest.main()
