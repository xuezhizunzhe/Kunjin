from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import AssessmentCipher, ProfileCipher, ProfileCryptoError
from kunjin.suitability.models import AssessmentStatus, BlockReason
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.service import (
    LoadedProfile,
    ProfileService,
    SuitabilityAssessmentError,
    SuitabilityPolicyError,
    SuitabilityService,
)
from kunjin.suitability.store import (
    ActiveProfileChangedError,
    ProfileStore,
    SuitabilityAssessmentStore,
    SuitabilityPolicyStore,
)
from tests.unit.test_suitability_models import valid_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


class MemoryKeyStore:
    def __init__(self) -> None:
        self.key = None

    def load_existing_key(self):
        return self.key

    def load_or_create_key(self):
        if self.key is None:
            self.key = bytes(range(32))
        return self.key


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class SuitabilityServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.key_store = MemoryKeyStore()
        self.clock = MutableClock(NOW)
        self.profile_store = ProfileStore(self.repository)
        self.profile_service = ProfileService(
            self.profile_store,
            ProfileCipher(self.key_store),
            now=self.clock,
        )
        self.policy = SuitabilityPolicyV1()
        self.policy_store = SuitabilityPolicyStore(self.repository)
        self.assessment_store = SuitabilityAssessmentStore(self.repository)
        self.assessment_cipher = AssessmentCipher(self.key_store)
        self.service = SuitabilityService(
            self.profile_service,
            self.policy_store,
            self.assessment_store,
            self.assessment_cipher,
            self.policy,
            now=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _tamper_assessment(self, assessment_id: int, **fields: object) -> None:
        assignments = ", ".join(f"{field} = ?" for field in fields)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute(
                f"UPDATE suitability_assessments SET {assignments} WHERE id = ?",
                (*fields.values(), assessment_id),
            )

    def _assess_same_instant(
        self,
        instant: datetime,
        offset_hours: int,
        profile,
    ):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "kunjin.db")
            repository.migrate()
            key_store = MemoryKeyStore()
            zone = timezone(timedelta(hours=offset_hours))
            clock = MutableClock((instant - timedelta(days=1)).astimezone(zone))
            profile_service = ProfileService(
                ProfileStore(repository),
                ProfileCipher(key_store),
                now=clock,
            )
            profile_service.confirm_profile(profile)
            clock.value = instant.astimezone(zone)
            service = SuitabilityService(
                profile_service,
                SuitabilityPolicyStore(repository),
                SuitabilityAssessmentStore(repository),
                AssessmentCipher(key_store),
                SuitabilityPolicyV1(),
                now=clock,
            )

            execution = service.assess()
            snapshot = service.load_authenticated_snapshot()
            return execution, snapshot

    def test_missing_profile_is_transient_block_and_is_not_persisted(self) -> None:
        execution = self.service.assess()

        self.assertEqual(execution.result.status, AssessmentStatus.BLOCKED)
        self.assertEqual(execution.result.hard_blocks, (BlockReason.PROFILE_MISSING,))
        self.assertIsNone(execution.assessment_id)
        self.assertEqual(self.assessment_store.history(), ())
        self.assertEqual(execution.safe_json()["capability"], "research_only")
        self.assertNotIn("amounts", execution.local_view())

    def test_invalidated_and_stale_profiles_return_nonpersisted_readiness_blocks(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        self.profile_service.invalidate("income_change")
        invalidated = self.service.assess()
        self.assertEqual(invalidated.result.hard_blocks, (BlockReason.PROFILE_INVALIDATED,))
        self.assertIsNone(invalidated.assessment_id)

        self.profile_service.confirm_profile(valid_profile())
        self.clock.value = NOW + timedelta(days=91)
        stale = self.service.assess()
        self.assertEqual(stale.result.hard_blocks, (BlockReason.PROFILE_STALE,))
        self.assertIsNone(stale.assessment_id)
        self.assertEqual(self.assessment_store.history(), ())

    def test_assess_persists_profile_policy_binding_and_exact_fingerprint_input(self) -> None:
        profile_metadata = self.profile_service.confirm_profile(valid_profile())
        loaded = self.profile_service.load_active()

        execution = self.service.assess()
        stored = self.assessment_store.latest_for(profile_metadata.id, self.policy.version)
        fingerprint_input = "|".join(
            (
                str(profile_metadata.id),
                loaded.encrypted_keyed_fingerprint,
                self.policy.checksum(),
                NOW.isoformat(),
            )
        ).encode("ascii")

        self.assertIsNotNone(stored)
        self.assertEqual(execution.assessment_id, stored.metadata.id)
        self.assertEqual(
            stored.metadata.input_fingerprint,
            self.assessment_cipher.fingerprint(fingerprint_input),
        )
        self.assertEqual(stored.metadata.profile_version_id, profile_metadata.id)
        self.assertEqual(stored.metadata.policy_version, self.policy.version)

    def test_legacy_date_only_input_fingerprint_requires_reassessment(self) -> None:
        profile = self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        loaded = self.profile_service.load_active()
        legacy_payload = "|".join(
            (
                str(profile.id),
                loaded.encrypted_keyed_fingerprint,
                self.policy.checksum(),
                NOW.date().isoformat(),
            )
        ).encode("ascii")
        legacy_fingerprint = self.assessment_cipher.fingerprint(legacy_payload)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute(
                "UPDATE suitability_assessments SET input_fingerprint = ? WHERE id = ?",
                (legacy_fingerprint, execution.assessment_id),
            )

        with self.assertRaises(SuitabilityAssessmentError):
            self.service.load_authenticated_snapshot()

    def test_valid_until_is_earlier_of_24_hours_and_profile_expiry(self) -> None:
        metadata = self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self.assertEqual(execution.valid_until, NOW + timedelta(hours=24))

        self.clock.value = metadata.valid_until - timedelta(hours=12)
        execution = self.service.assess()
        self.assertEqual(execution.valid_until, metadata.valid_until)

    def test_same_instant_offsets_use_utc_date_for_overdue_boundaries(self) -> None:
        cases = (
            (
                datetime(2026, 7, 12, 16, 30, tzinfo=timezone.utc),
                False,
            ),
            (
                datetime(2026, 7, 13, 4, 30, tzinfo=timezone.utc),
                True,
            ),
        )
        for instant, expected_overdue in cases:
            profile = replace(
                valid_profile(),
                obligations=(replace(valid_profile().obligations[0], due_date=date(2026, 7, 12)),),
                goals=(),
            )
            results = []
            for offset_hours in (0, 8, -12):
                execution, snapshot = self._assess_same_instant(
                    instant,
                    offset_hours,
                    profile,
                )
                self.assertEqual(execution.assessed_at, instant)
                self.assertEqual(snapshot.result, execution.result)
                results.append(execution.result)

            with self.subTest(instant=instant):
                self.assertEqual(results[1:], results[:1] * 2)
                self.assertEqual(
                    BlockReason.OBLIGATION_OVERDUE in results[0].hard_blocks,
                    expected_overdue,
                )

    def test_same_instant_offsets_use_utc_date_for_one_year_anniversary(self) -> None:
        cases = (
            (
                datetime(2026, 7, 12, 16, 30, tzinfo=timezone.utc),
                date(2027, 7, 13),
                False,
            ),
            (
                datetime(2026, 7, 13, 4, 30, tzinfo=timezone.utc),
                date(2027, 7, 13),
                True,
            ),
        )
        for instant, target_date, expected_blocked in cases:
            profile = replace(
                valid_profile(),
                obligations=(),
                goals=(
                    replace(
                        valid_profile().goals[0],
                        target_date=target_date,
                        use_date_can_be_postponed=False,
                    ),
                ),
            )
            results = [
                self._assess_same_instant(instant, offset_hours, profile)[0].result
                for offset_hours in (0, 8, -12)
            ]

            with self.subTest(instant=instant):
                self.assertEqual(results[1:], results[:1] * 2)
                self.assertEqual(
                    BlockReason.CRITICAL_GOAL_SHORTFALL in results[0].hard_blocks,
                    expected_blocked,
                )

    def test_current_time_requires_exact_aware_datetime_and_normalizes_to_utc(self) -> None:
        class DerivedDateTime(datetime):
            pass

        invalid_values = (
            "2026-07-12T12:00:00+00:00",
            NOW.replace(tzinfo=None),
            DerivedDateTime(2026, 7, 12, 12, tzinfo=timezone.utc),
        )
        for invalid in invalid_values:
            with self.subTest(value=invalid):
                self.clock.value = invalid
                with self.assertRaises(SuitabilityAssessmentError):
                    self.service.assess()

        offset_now = NOW.astimezone(timezone(timedelta(hours=8)))
        self.clock.value = offset_now
        execution = self.service.assess()
        self.assertEqual(execution.assessed_at, NOW)
        self.assertIs(execution.assessed_at.tzinfo, timezone.utc)

    def test_24_hour_ttl_is_absolute_across_spring_and_fall_dst(self) -> None:
        new_york = ZoneInfo("America/New_York")
        for assessed_at in (
            datetime(2026, 3, 8, 0, 30, tzinfo=new_york),
            datetime(2026, 11, 1, 0, 30, tzinfo=new_york),
        ):
            with self.subTest(assessed_at=assessed_at):
                with tempfile.TemporaryDirectory() as directory:
                    repository = Repository(Path(directory) / "kunjin.db")
                    repository.migrate()
                    key_store = MemoryKeyStore()
                    clock = MutableClock(assessed_at)
                    profile_service = ProfileService(
                        ProfileStore(repository),
                        ProfileCipher(key_store),
                        now=clock,
                    )
                    profile_service.confirm_profile(valid_profile())
                    active = ProfileStore(repository).active_encrypted()
                    loaded = LoadedProfile(
                        metadata=active.metadata,
                        profile=replace(valid_profile(), confirmed_at=assessed_at),
                        encrypted_keyed_fingerprint=(active.encrypted.keyed_fingerprint),
                    )
                    service = SuitabilityService(
                        profile_service,
                        SuitabilityPolicyStore(repository),
                        SuitabilityAssessmentStore(repository),
                        AssessmentCipher(key_store),
                        SuitabilityPolicyV1(),
                        now=clock,
                    )

                    with patch.object(
                        profile_service,
                        "load_active",
                        return_value=loaded,
                    ):
                        execution = service.assess()
                    expected = assessed_at.astimezone(timezone.utc) + timedelta(hours=24)

                    self.assertEqual(execution.valid_until, expected)
                    self.assertEqual(
                        execution.valid_until.timestamp() - assessed_at.timestamp(),
                        24 * 60 * 60,
                    )

    def test_profile_and_assessment_are_stale_at_their_expiry_instant(self) -> None:
        metadata = self.profile_service.confirm_profile(valid_profile())
        self.clock.value = metadata.valid_until

        execution = self.service.assess()

        self.assertEqual(execution.result.hard_blocks, (BlockReason.PROFILE_STALE,))
        self.assertIsNone(execution.assessment_id)

        self.clock.value = NOW
        self.service.assess()
        self.clock.value = NOW + timedelta(hours=24)
        self.assertEqual(self.service.status()["state"], "stale")

    def test_status_rejects_created_at_only_tamper_while_fresh(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self._tamper_assessment(
            execution.assessment_id,
            created_at=(NOW + timedelta(seconds=1)).isoformat(),
        )

        with self.assertRaises(SuitabilityAssessmentError) as raised:
            self.service.status()

        self.assertEqual(raised.exception.code, "assessment_calculation_failed")

    def test_status_rejects_created_at_only_tamper_while_stale(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self._tamper_assessment(
            execution.assessment_id,
            created_at=(NOW + timedelta(seconds=1)).isoformat(),
        )
        self.clock.value = NOW + timedelta(hours=24)

        with self.assertRaises(SuitabilityAssessmentError):
            self.service.status()

    def test_status_rejects_future_assessment_timing(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        future = NOW + timedelta(hours=1)
        self._tamper_assessment(
            execution.assessment_id,
            assessed_at=future.isoformat(),
            created_at=future.isoformat(),
            valid_until=(future + timedelta(hours=24)).isoformat(),
        )

        with self.assertRaises(SuitabilityAssessmentError):
            self.service.status()

    def test_status_rejects_tampered_assessment_ttl(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self._tamper_assessment(
            execution.assessment_id,
            valid_until=(NOW + timedelta(hours=24, seconds=1)).isoformat(),
        )

        with self.assertRaises(SuitabilityAssessmentError):
            self.service.status()

    def test_profile_version_change_makes_previous_status_stale(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        self.service.assess()
        self.profile_service.confirm_profile(valid_profile())

        status = self.service.status()

        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["freshness"], "stale")
        self.assertEqual(status["assessment_id"], 1)

    def test_profile_switch_before_atomic_insert_returns_transient_stale(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        original_insert = self.assessment_store.insert

        def switch_then_insert(*args, **kwargs):
            self.profile_service.confirm_profile(valid_profile())
            return original_insert(*args, **kwargs)

        with patch.object(self.assessment_store, "insert", switch_then_insert):
            execution = self.service.assess()

        self.assertEqual(execution.result.hard_blocks, (BlockReason.PROFILE_STALE,))
        self.assertIsNone(execution.assessment_id)
        self.assertEqual(self.assessment_store.history(), ())

    def test_profile_switch_after_insert_commit_does_not_return_fresh(self) -> None:
        first = self.profile_service.confirm_profile(valid_profile())
        original_insert = self.assessment_store.insert

        def insert_then_switch(*args, **kwargs):
            metadata = original_insert(*args, **kwargs)
            self.profile_service.confirm_profile(valid_profile())
            return metadata

        with patch.object(self.assessment_store, "insert", insert_then_switch):
            execution = self.service.assess()

        self.assertEqual(execution.result.hard_blocks, (BlockReason.PROFILE_STALE,))
        self.assertIsNone(execution.assessment_id)
        history = self.assessment_store.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].profile_version_id, first.id)

    def test_post_insert_binding_recheck_preserves_error_classification(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        loaded = self.profile_service.load_active()
        cases = (
            (ValueError("polluted-final-binding"), SuitabilityAssessmentError),
            (ProfileCryptoError("profile decryption failed"), ProfileCryptoError),
        )
        for failure, expected_error in cases:
            with self.subTest(expected_error=expected_error.__name__):
                with patch.object(
                    self.profile_service,
                    "load_active",
                    side_effect=(loaded, failure),
                ):
                    with self.assertRaises(expected_error) as raised:
                        self.service.assess()
                self.assertNotIn("polluted-final-binding", str(raised.exception))

    def test_store_atomic_insert_rejects_nonactive_profile(self) -> None:
        first = self.profile_service.confirm_profile(valid_profile())
        loaded = self.profile_service.load_active()
        self.profile_service.confirm_profile(valid_profile())
        result = self.service.assess().result
        encrypted = self.assessment_cipher.encrypt(
            b'{"emergency_reserve_shortfall":"0.00",'
            b'"monthly_safety_residual":"0.00",'
            b'"required_emergency_reserve":"0.00",'
            b'"required_monthly_goal_saving":"0.00",'
            b'"required_monthly_obligation_saving":"0.00",'
            b'"safe_monthly_ceiling":"0.00",'
            b'"verified_emergency_reserve":"0.00"}'
        )

        with self.assertRaises(ActiveProfileChangedError):
            self.assessment_store.insert(
                first.id,
                self.policy.version,
                "a" * 64,
                result,
                encrypted,
                NOW,
                NOW + timedelta(hours=24),
            )
        self.assertEqual(
            [item.profile_version_id for item in self.assessment_store.history()],
            [loaded.metadata.id + 1],
        )

    def test_status_final_binding_recheck_detects_switch_during_validation(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        original_decrypt = self.assessment_cipher.decrypt

        def decrypt_then_switch(encrypted):
            plaintext = original_decrypt(encrypted)
            self.profile_service.confirm_profile(valid_profile())
            return plaintext

        with patch.object(self.assessment_cipher, "decrypt", decrypt_then_switch):
            status = self.service.status()

        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["assessment_id"], execution.assessment_id)

    def test_policy_checksum_mismatch_fails_closed(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        self.policy_store.ensure(self.policy)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions SET policy_checksum = ?",
                ("f" * 64,),
            )

        with self.assertRaises(SuitabilityPolicyError) as raised:
            self.service.assess()

        self.assertEqual(raised.exception.code, "policy_unavailable")

    def test_missing_key_and_tampered_assessment_fail_closed(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self.key_store.key = None
        with self.assertRaises(ProfileCryptoError) as raised:
            self.service.status()
        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")

        self.key_store.key = bytes(range(32))
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute(
                "UPDATE suitability_assessments SET encrypted_amount_results = ? WHERE id = ?",
                ("tampered", execution.assessment_id),
            )
        with self.assertRaises(ProfileCryptoError) as raised:
            self.service.status()
        self.assertEqual(raised.exception.code, "encrypted_profile_unavailable")

    def test_safe_json_is_amount_free_and_local_view_has_exact_values_only(self) -> None:
        profile = replace(
            valid_profile(),
            monthly_net_income=Decimal("73129.00"),
            monthly_required_debt_service=Decimal("0"),
            emergency_reserve=Decimal("84217.00"),
            immediately_available_cash=Decimal("95311.00"),
            goals=(replace(valid_profile().goals[0], name="private-goal-name"),),
        )
        self.profile_service.confirm_profile(profile)

        execution = self.service.assess()
        safe = json.dumps(execution.safe_json(), sort_keys=True)
        local = json.dumps(execution.local_view(), sort_keys=True)

        for forbidden in (
            "73129",
            "84217",
            "95311",
            "private-goal-name",
            "ciphertext",
            "nonce",
            "fingerprint",
        ):
            self.assertNotIn(forbidden, safe)
        self.assertIn("84217.00", local)
        for forbidden in ("ciphertext", "nonce", "fingerprint", "AES-256-GCM"):
            self.assertNotIn(forbidden, local)
        self.assertEqual(execution.safe_json()["capability"], "research_only")
        self.assertEqual(
            execution.safe_json()["profile_conflicts"],
            ["monthly_required_debt_service_vs_debts"],
        )

    def test_fresh_status_authenticates_and_history_is_metadata_only(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()

        status = self.service.status()
        history = self.service.history()

        self.assertEqual(status["state"], "fresh")
        self.assertEqual(status["assessment_id"], execution.assessment_id)
        self.assertEqual(len(history), 1)
        serialized = json.dumps({"status": status, "history": history})
        for forbidden in ("12000", "500000", "education", "ciphertext", "nonce", "fingerprint"):
            self.assertNotIn(forbidden, serialized)

    def test_history_authenticates_fresh_assessment_before_emitting_metadata(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        execution = self.service.assess()
        self.assertEqual(len(self.service.history()), 1)
        self._tamper_assessment(
            execution.assessment_id,
            created_at=(NOW + timedelta(seconds=1)).isoformat(),
        )

        with self.assertRaises(SuitabilityAssessmentError) as raised:
            self.service.history()

        self.assertEqual(raised.exception.code, "assessment_calculation_failed")
        self.assertNotIn("12000", str(raised.exception))

    def test_history_authenticates_stale_superseded_assessment(self) -> None:
        self.profile_service.confirm_profile(valid_profile())
        stale = self.service.assess()
        self.clock.value += timedelta(minutes=1)
        self.profile_service.confirm_profile(valid_profile())
        self.service.assess()
        self.assertEqual(len(self.service.history()), 2)
        self._tamper_assessment(
            stale.assessment_id,
            created_at=(NOW + timedelta(seconds=1)).isoformat(),
        )

        with self.assertRaises(SuitabilityAssessmentError):
            self.service.history()

    def test_historical_snapshot_authenticates_superseded_profile_by_exact_ids(self) -> None:
        first_profile = self.profile_service.confirm_profile(valid_profile())
        first = self.service.assess()
        self.clock.value += timedelta(minutes=1)
        self.profile_service.confirm_profile(valid_profile())

        snapshot = self.service.load_authenticated_snapshot_by_ids(
            first_profile.id,
            first.assessment_id,
        )

        self.assertEqual(snapshot.profile_version_id, first_profile.id)
        self.assertEqual(snapshot.assessment_id, first.assessment_id)
        self.assertEqual(snapshot.result, first.result)
        self.assertEqual(snapshot.valid_until, first.valid_until)

    def test_public_methods_convert_store_and_metadata_failures(self) -> None:
        paths = (
            ("assess", self.profile_store, "active_encrypted"),
            ("status", self.profile_store, "active_encrypted"),
            ("history", self.assessment_store, "history"),
        )
        for method_name, target, attribute in paths:
            with self.subTest(method=method_name):
                with patch.object(target, attribute, side_effect=ValueError("polluted")):
                    with self.assertRaises(SuitabilityAssessmentError) as raised:
                        getattr(self.service, method_name)()
                self.assertEqual(raised.exception.code, "assessment_calculation_failed")
                self.assertNotIn("polluted", str(raised.exception))

        with patch.object(
            self.profile_store,
            "active_encrypted",
            side_effect=sqlite3.OperationalError("database is unavailable"),
        ):
            with self.assertRaises(SuitabilityAssessmentError) as raised:
                self.service.assess()
        self.assertEqual(raised.exception.code, "assessment_calculation_failed")
        self.assertNotIn("database", str(raised.exception))

    def test_no_active_and_expired_helper_failures_are_stable_errors(self) -> None:
        with patch.object(
            self.profile_store,
            "latest_metadata",
            side_effect=ValueError("polluted-latest"),
        ):
            with self.assertRaises(SuitabilityAssessmentError):
                self.service.assess()

        metadata = self.profile_service.confirm_profile(valid_profile())
        self.clock.value = metadata.valid_until
        with patch.object(
            self.assessment_store,
            "history",
            side_effect=ValueError("polluted-history"),
        ):
            with self.assertRaises(SuitabilityAssessmentError):
                self.service.status()


if __name__ == "__main__":
    unittest.main()
