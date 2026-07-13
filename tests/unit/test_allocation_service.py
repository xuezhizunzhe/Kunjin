from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kunjin.allocation.crypto import AllocationCipher
from kunjin.allocation.models import AllocationBlockCode, AllocationStatus
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.allocation.service import (
    AllocationCalculationError,
    AllocationPolicyError,
    AllocationService,
    EncryptedProfileUnavailableError,
)
from kunjin.allocation.store import AllocationAssessmentStore, AllocationPolicyStore
from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import AssessmentCipher, ProfileCipher
from kunjin.suitability.models import AssessmentStatus, RiskReaction
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.service import ProfileService, SuitabilityService
from kunjin.suitability.store import (
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


class AllocationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.key_store = MemoryKeyStore()
        self.clock = MutableClock(NOW)
        self.profile_service = ProfileService(
            ProfileStore(self.repository),
            ProfileCipher(self.key_store),
            now=self.clock,
        )
        self.suitability_store = SuitabilityAssessmentStore(self.repository)
        self.suitability_service = SuitabilityService(
            self.profile_service,
            SuitabilityPolicyStore(self.repository),
            self.suitability_store,
            AssessmentCipher(self.key_store),
            SuitabilityPolicyV1(),
            now=self.clock,
        )
        self.allocation_store = AllocationAssessmentStore(self.repository)
        self.service = AllocationService(
            self.suitability_service,
            AllocationPolicyStore(self.repository),
            self.allocation_store,
            AllocationCipher(self.key_store),
            AllocationPolicyV1(),
            now=self.clock,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _confirm_and_assess(self, profile=None):
        metadata = self.profile_service.confirm_profile(profile or valid_profile())
        execution = self.suitability_service.assess()
        return metadata, execution

    def _tamper_allocation(self, field: str, value: object) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER IF EXISTS allocation_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                f"UPDATE allocation_assessments SET {field} = ?",
                (value,),
            )

    def _tamper_suitability(self, field: str, value: object) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER IF EXISTS suitability_assessment_no_update")
            connection.execute(
                f"UPDATE suitability_assessments SET {field} = ?",
                (value,),
            )

    def test_authenticated_snapshot_is_frozen_exact_and_current(self) -> None:
        profile_metadata, suitability = self._confirm_and_assess()

        snapshot = self.suitability_service.load_authenticated_snapshot()

        self.assertEqual(snapshot.profile, self.profile_service.load_active_profile())
        self.assertEqual(snapshot.profile_version_id, profile_metadata.id)
        self.assertEqual(snapshot.assessment_id, suitability.assessment_id)
        self.assertEqual(snapshot.result, suitability.result)
        self.assertEqual(snapshot.result.status, AssessmentStatus.CONSTRAINED)
        with self.assertRaises(FrozenInstanceError):
            snapshot.assessment_id = 999

    def test_cross_midnight_assessment_immediately_authenticates_for_allocation(self) -> None:
        instant = datetime(2026, 7, 12, 16, 30, tzinfo=timezone.utc)
        self.clock.value = instant.astimezone(timezone(timedelta(hours=8)))
        profile = replace(
            valid_profile(),
            obligations=(),
            goals=(
                replace(
                    valid_profile().goals[0],
                    target_date=datetime(2027, 7, 13).date(),
                    use_date_can_be_postponed=False,
                ),
            ),
        )
        _, suitability = self._confirm_and_assess(profile)

        snapshot = self.suitability_service.load_authenticated_snapshot()
        allocation = self.service.ranges()

        self.assertEqual(suitability.assessed_at, instant)
        self.assertEqual(snapshot.result, suitability.result)
        self.assertEqual(allocation.suitability_assessment_id, suitability.assessment_id)

    def test_blocked_suitability_returns_transient_block_without_persistence(self) -> None:
        blocked_profile = replace(valid_profile(), emergency_reserve=Decimal("0.00"))
        self._confirm_and_assess(blocked_profile)

        execution = self.service.ranges()

        self.assertEqual(execution.status, AllocationStatus.BLOCKED)
        self.assertEqual(execution.blocks, (AllocationBlockCode.SUITABILITY_BLOCKED,))
        self.assertIsNone(execution.permitted_region)
        self.assertEqual(execution.freshness, "transient")
        self.assertEqual(self.allocation_store.history(), ())

    def test_active_profile_without_phase_b_assessment_fails_stably(self) -> None:
        self.profile_service.confirm_profile(valid_profile())

        with self.assertRaises(AllocationCalculationError) as raised:
            self.service.ranges()

        self.assertEqual(raised.exception.code, "allocation_calculation_failed")
        self.assertEqual(self.allocation_store.history(), ())

    def test_constrained_suitability_persists_authenticated_range(self) -> None:
        profile_metadata, suitability = self._confirm_and_assess()

        execution = self.service.ranges()

        self.assertEqual(execution.status, AllocationStatus.RANGE_AVAILABLE)
        self.assertEqual(execution.freshness, "fresh")
        self.assertEqual(execution.profile_version_id, profile_metadata.id)
        self.assertEqual(execution.suitability_assessment_id, suitability.assessment_id)
        self.assertEqual(len(self.allocation_store.history()), 1)
        self.assertEqual(self.service.status()["state"], "fresh")

    def test_ready_suitability_persists_authenticated_range(self) -> None:
        _, suitability = self._confirm_and_assess(replace(valid_profile(), obligations=()))

        execution = self.service.ranges()

        self.assertEqual(suitability.result.status, AssessmentStatus.READY_FOR_ALLOCATION)
        self.assertEqual(execution.status, AllocationStatus.RANGE_AVAILABLE)
        self.assertEqual(len(self.allocation_store.history()), 1)

    def test_input_fingerprint_binds_every_required_identity(self) -> None:
        self._confirm_and_assess()
        snapshot = self.suitability_service.load_authenticated_snapshot()

        execution = self.service.ranges()
        stored = self.allocation_store.history()[0]
        payload = json.dumps(
            {
                "allocation_policy_checksum": AllocationPolicyV1().checksum(),
                "assessment_instant": NOW.isoformat(),
                "profile_keyed_fingerprint": snapshot.profile_keyed_fingerprint,
                "profile_version_id": snapshot.profile_version_id,
                "suitability_assessment_id": snapshot.assessment_id,
                "suitability_input_fingerprint": snapshot.input_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

        self.assertEqual(execution.assessment_id, stored.id)
        self.assertEqual(
            stored.input_fingerprint,
            AllocationCipher(self.key_store).fingerprint(payload),
        )

    def test_repeated_identical_range_is_idempotent(self) -> None:
        self._confirm_and_assess()

        first = self.service.ranges()
        second = self.service.ranges()

        self.assertEqual(first.assessment_id, second.assessment_id)
        self.assertEqual(len(self.allocation_store.history()), 1)

    def test_one_second_later_creates_a_new_exact_time_assessment(self) -> None:
        self._confirm_and_assess()
        first = self.service.ranges()

        self.clock.value += timedelta(seconds=1)
        second = self.service.ranges()

        self.assertNotEqual(second.assessment_id, first.assessment_id)
        self.assertEqual(second.assessed_at, first.assessed_at + timedelta(seconds=1))
        self.assertEqual(second.valid_until, first.valid_until)
        self.assertEqual(len(self.allocation_store.history()), 2)

    def test_valid_until_is_minimum_of_all_three_expiries(self) -> None:
        durable_profile = replace(
            valid_profile(),
            immediately_available_cash=Decimal("200000.00"),
            emergency_reserve=Decimal("100000.00"),
        )
        profile_metadata, suitability = self._confirm_and_assess(durable_profile)
        execution = self.service.ranges()
        self.assertEqual(execution.valid_until, NOW + timedelta(hours=24))

        self.clock.value = profile_metadata.valid_until - timedelta(hours=12)
        suitability = self.suitability_service.assess()
        execution = self.service.ranges()
        self.assertEqual(execution.valid_until, suitability.valid_until)

    def test_phase_c_horizon_block_is_not_persisted(self) -> None:
        self._confirm_and_assess(replace(valid_profile(), goals=()))

        execution = self.service.ranges()

        self.assertEqual(execution.status, AllocationStatus.BLOCKED)
        self.assertIn(AllocationBlockCode.ALLOCATION_HORIZON_MISSING, execution.blocks)
        self.assertEqual(self.allocation_store.history(), ())

    def test_phase_c_protected_capital_block_is_not_persisted(self) -> None:
        profile = replace(
            valid_profile(),
            immediately_available_cash=Decimal("32000.00"),
            cash_like_assets=Decimal("10000.00"),
        )
        self._confirm_and_assess(profile)

        execution = self.service.ranges()

        self.assertEqual(execution.status, AllocationStatus.BLOCKED)
        self.assertIn(
            AllocationBlockCode.PROTECTED_CAPITAL_OVERLAP_OR_SHORTFALL,
            execution.blocks,
        )
        self.assertEqual(self.allocation_store.history(), ())

    def test_inconsistent_risk_answers_cannot_authenticate_a_range(self) -> None:
        profile = replace(
            valid_profile(),
            reaction_10=RiskReaction.REDEEM,
            reaction_20=RiskReaction.HOLD,
        )
        self._confirm_and_assess(profile)

        execution = self.service.ranges()

        self.assertEqual(execution.blocks, (AllocationBlockCode.SUITABILITY_BLOCKED,))
        self.assertEqual(self.allocation_store.history(), ())

    def test_missing_key_does_not_create_replacement(self) -> None:
        self._confirm_and_assess()
        self.key_store.key = None

        with self.assertRaises(EncryptedProfileUnavailableError):
            self.service.ranges()

        self.assertIsNone(self.key_store.key)
        self.assertEqual(self.allocation_store.history(), ())

    def test_tampered_suitability_input_fingerprint_fails_closed(self) -> None:
        self._confirm_and_assess()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute(
                "UPDATE suitability_assessments SET input_fingerprint = ?",
                ("f" * 64,),
            )

        with self.assertRaises(AllocationCalculationError):
            self.service.ranges()
        self.assertEqual(self.allocation_store.history(), ())

    def test_valid_base64_suitability_ciphertext_tamper_fails_stably(self) -> None:
        _, suitability = self._confirm_and_assess(
            replace(
                valid_profile(),
                goals=(
                    replace(
                        valid_profile().goals[0],
                        name="PRIVATE-CIPHERTEXT-SENTINEL",
                    ),
                ),
            )
        )
        with self.repository.connect() as connection:
            ciphertext = connection.execute(
                "SELECT encrypted_amount_results FROM suitability_assessments WHERE id = ?",
                (suitability.assessment_id,),
            ).fetchone()[0]
        replacement = "A" if ciphertext[0] != "A" else "B"
        self._tamper_suitability(
            "encrypted_amount_results",
            replacement + ciphertext[1:],
        )

        with self.assertRaises(AllocationCalculationError) as raised:
            self.service.ranges()

        self.assertEqual(raised.exception.code, "allocation_calculation_failed")
        self.assertNotIn("PRIVATE-CIPHERTEXT-SENTINEL", str(raised.exception))
        self.assertEqual(self.allocation_store.history(), ())

    def test_schema_valid_suitability_summary_mismatch_fails_stably(self) -> None:
        self._confirm_and_assess()
        with self.repository.connect() as connection:
            summary = json.loads(
                connection.execute(
                    "SELECT safe_summary_json FROM suitability_assessments"
                ).fetchone()[0]
            )
        summary["goal_count"] += 1
        self._tamper_suitability(
            "safe_summary_json",
            json.dumps(summary, sort_keys=True, separators=(",", ":")),
        )

        with self.assertRaises(AllocationCalculationError) as raised:
            self.service.ranges()

        self.assertEqual(raised.exception.code, "allocation_calculation_failed")
        self.assertNotIn("goal_count", str(raised.exception))
        self.assertEqual(self.allocation_store.history(), ())

    def test_invalid_legacy_suitability_timestamp_is_normalized_stably(self) -> None:
        self._confirm_and_assess()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE suitability_assessments SET assessed_at = ?",
                ("not-a-time",),
            )

        with self.assertRaises(AllocationCalculationError) as raised:
            self.service.ranges()

        self.assertEqual(raised.exception.code, "allocation_calculation_failed")
        self.assertEqual(self.allocation_store.history(), ())

    def test_profile_switch_before_insert_is_not_persisted(self) -> None:
        self._confirm_and_assess()
        original_insert = self.allocation_store.insert

        def switch_then_insert(*args, **kwargs):
            self.profile_service.confirm_profile(valid_profile())
            return original_insert(*args, **kwargs)

        with patch.object(self.allocation_store, "insert", switch_then_insert):
            with self.assertRaises(AllocationCalculationError):
                self.service.ranges()
        self.assertEqual(self.allocation_store.history(), ())

    def test_profile_switch_after_commit_never_returns_fresh(self) -> None:
        first_profile, _ = self._confirm_and_assess()
        original_insert = self.allocation_store.insert

        def insert_then_switch(*args, **kwargs):
            metadata = original_insert(*args, **kwargs)
            self.profile_service.confirm_profile(valid_profile())
            return metadata

        with patch.object(self.allocation_store, "insert", insert_then_switch):
            with self.assertRaises(AllocationCalculationError):
                self.service.ranges()
        history = self.allocation_store.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].profile_version_id, first_profile.id)

    def test_suitability_switch_after_commit_never_returns_fresh(self) -> None:
        self._confirm_and_assess()
        original_insert = self.allocation_store.insert

        def insert_then_reassess(*args, **kwargs):
            metadata = original_insert(*args, **kwargs)
            self.clock.value += timedelta(minutes=1)
            self.suitability_service.assess()
            return metadata

        with patch.object(self.allocation_store, "insert", insert_then_reassess):
            with self.assertRaises(AllocationCalculationError):
                self.service.ranges()
        self.assertEqual(len(self.allocation_store.history()), 1)

    def test_suitability_switch_before_insert_is_not_persisted(self) -> None:
        self._confirm_and_assess()
        original_insert = self.allocation_store.insert

        def reassess_then_insert(*args, **kwargs):
            self.clock.value += timedelta(minutes=1)
            self.suitability_service.assess()
            return original_insert(*args, **kwargs)

        with patch.object(self.allocation_store, "insert", reassess_then_insert):
            with self.assertRaises(AllocationCalculationError):
                self.service.ranges()
        self.assertEqual(self.allocation_store.history(), ())

    def test_profile_switch_before_status_return_is_stale(self) -> None:
        self._confirm_and_assess()
        self.service.ranges()
        original_load = self.suitability_service.load_authenticated_snapshot
        first = original_load()
        calls = 0

        def switch_on_final_load():
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            self.profile_service.confirm_profile(valid_profile())
            return original_load()

        with patch.object(
            self.suitability_service,
            "load_authenticated_snapshot",
            side_effect=switch_on_final_load,
        ):
            status = self.service.status()
        self.assertEqual(status["state"], "stale")

    def test_latest_suitability_switch_before_status_return_is_not_fresh(self) -> None:
        self._confirm_and_assess()
        self.service.ranges()
        original_load = self.suitability_service.load_authenticated_snapshot
        first = original_load()
        calls = 0

        def reassess_on_final_load():
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            self.clock.value += timedelta(minutes=1)
            self.suitability_service.assess()
            return original_load()

        with patch.object(
            self.suitability_service,
            "load_authenticated_snapshot",
            side_effect=reassess_on_final_load,
        ):
            status = self.service.status()

        self.assertEqual(status["state"], "stale")
        self.assertNotEqual(status["freshness"], "fresh")

    def test_exact_ciphertext_and_plaintext_metadata_tamper_fail_status(self) -> None:
        self._confirm_and_assess()
        self.service.ranges()
        stored = self.allocation_store.history()[0]

        self._tamper_allocation("keyed_payload_fingerprint", "0" * 64)
        with self.assertRaises(AllocationCalculationError):
            self.service.status()

        self._tamper_allocation("keyed_payload_fingerprint", "1" * 64)
        self._tamper_allocation("safe_summary_json", '{"goal_count":999}')
        with self.assertRaises(AllocationCalculationError):
            self.service.status()
        self.assertEqual(stored.status, AllocationStatus.RANGE_AVAILABLE)

    def test_stale_status_authenticates_every_payload_surface(self) -> None:
        self._confirm_and_assess()
        execution = self.service.ranges()
        with self.repository.connect() as connection:
            original = dict(
                connection.execute(
                    "SELECT permitted_region_json, binding_constraints_json, "
                    "safe_summary_json, encrypted_amount_results, "
                    "keyed_payload_fingerprint FROM allocation_assessments WHERE id = ?",
                    (execution.assessment_id,),
                ).fetchone()
            )
        region = json.loads(original["permitted_region_json"])
        region["maximum_equity"] = "0.99"
        summary = json.loads(original["safe_summary_json"])
        summary["goal_count"] += 1
        ciphertext = original["encrypted_amount_results"]
        replacement = "A" if ciphertext[0] != "A" else "B"
        corruptions = (
            (
                "permitted_region_json",
                json.dumps(region, sort_keys=True, separators=(",", ":")),
            ),
            ("binding_constraints_json", "[]"),
            (
                "safe_summary_json",
                json.dumps(summary, sort_keys=True, separators=(",", ":")),
            ),
            ("encrypted_amount_results", replacement + ciphertext[1:]),
            ("keyed_payload_fingerprint", "0" * 64),
        )
        self.clock.value = NOW + timedelta(hours=24)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
        for field, corrupted in corruptions:
            with self.subTest(field=field):
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        f"UPDATE allocation_assessments SET {field} = ? WHERE id = ?",
                        (corrupted, execution.assessment_id),
                    )
                with self.assertRaises(AllocationCalculationError):
                    self.service.status()
                with self.repository.connect() as connection, connection:
                    connection.execute(
                        f"UPDATE allocation_assessments SET {field} = ? WHERE id = ?",
                        (original[field], execution.assessment_id),
                    )

    def test_history_authenticates_obsolete_payloads_before_output(self) -> None:
        first_profile, _ = self._confirm_and_assess()
        first = self.service.ranges()
        self.clock.value += timedelta(minutes=1)
        self._confirm_and_assess()
        self.service.ranges()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
            connection.execute(
                "UPDATE allocation_assessments SET keyed_payload_fingerprint = ? WHERE id = ?",
                ("0" * 64, first.assessment_id),
            )

        with self.assertRaises(AllocationCalculationError):
            self.service.history()

        self.assertEqual(first.profile_version_id, first_profile.id)

    def test_history_rejects_every_historical_provenance_binding_attack(self) -> None:
        first_profile, first_suitability = self._confirm_and_assess()
        first = self.service.ranges()
        self.clock.value += timedelta(minutes=1)
        second_profile, second_suitability = self._confirm_and_assess()
        with self.repository.connect() as connection:
            original = dict(
                connection.execute(
                    "SELECT profile_version_id, suitability_assessment_id, "
                    "policy_version, input_fingerprint, assessed_at, valid_until, created_at "
                    "FROM allocation_assessments WHERE id = ?",
                    (first.assessment_id,),
                ).fetchone()
            )
        assessed_at = datetime.fromisoformat(original["assessed_at"])
        valid_until = datetime.fromisoformat(original["valid_until"])
        attacks = (
            (
                "both foreign keys",
                {
                    "profile_version_id": second_profile.id,
                    "suitability_assessment_id": second_suitability.assessment_id,
                },
            ),
            ("profile foreign key", {"profile_version_id": second_profile.id}),
            (
                "suitability foreign key",
                {"suitability_assessment_id": second_suitability.assessment_id},
            ),
            ("input fingerprint", {"input_fingerprint": "f" * 64}),
            (
                "coordinated allocation times same date",
                {
                    "assessed_at": (assessed_at + timedelta(seconds=1)).isoformat(),
                    "created_at": (assessed_at + timedelta(seconds=1)).isoformat(),
                    "valid_until": (valid_until + timedelta(seconds=1)).isoformat(),
                },
            ),
            (
                "valid_until",
                {"valid_until": (valid_until - timedelta(seconds=1)).isoformat()},
            ),
            ("policy", {"policy_version": "2"}),
        )
        with self.repository.connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
        for label, changes in attacks:
            with self.subTest(label=label):
                assignments = ", ".join(f"{field} = ?" for field in changes)
                with self.repository.connect() as connection:
                    connection.execute("PRAGMA foreign_keys = OFF")
                    with connection:
                        connection.execute(
                            f"UPDATE allocation_assessments SET {assignments} WHERE id = ?",
                            (*changes.values(), first.assessment_id),
                        )
                with self.assertRaises(AllocationCalculationError):
                    self.service.history()
                with self.repository.connect() as connection:
                    connection.execute("PRAGMA foreign_keys = OFF")
                    with connection:
                        connection.execute(
                            "UPDATE allocation_assessments SET profile_version_id = ?, "
                            "suitability_assessment_id = ?, policy_version = ?, "
                            "input_fingerprint = ?, assessed_at = ?, valid_until = ?, "
                            "created_at = ? WHERE id = ?",
                            (
                                original["profile_version_id"],
                                original["suitability_assessment_id"],
                                original["policy_version"],
                                original["input_fingerprint"],
                                original["assessed_at"],
                                original["valid_until"],
                                original["created_at"],
                                first.assessment_id,
                            ),
                        )
        self.assertEqual(first_profile.id, first_suitability.profile_version_id)

    def test_coordinated_phase_b_and_phase_c_time_shift_fails_authentication(self) -> None:
        _, suitability = self._confirm_and_assess()
        allocation = self.service.ranges()
        with self.repository.connect() as connection:
            phase_b = dict(
                connection.execute(
                    "SELECT assessed_at, valid_until, created_at FROM suitability_assessments "
                    "WHERE id = ?",
                    (suitability.assessment_id,),
                ).fetchone()
            )
            phase_c = dict(
                connection.execute(
                    "SELECT assessed_at, valid_until, created_at FROM allocation_assessments "
                    "WHERE id = ?",
                    (allocation.assessment_id,),
                ).fetchone()
            )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
            for table, row_id, values in (
                ("suitability_assessments", suitability.assessment_id, phase_b),
                ("allocation_assessments", allocation.assessment_id, phase_c),
            ):
                assessed = datetime.fromisoformat(values["assessed_at"]) + timedelta(seconds=1)
                valid = datetime.fromisoformat(values["valid_until"]) + timedelta(seconds=1)
                connection.execute(
                    f"UPDATE {table} SET assessed_at = ?, created_at = ?, valid_until = ? "
                    "WHERE id = ?",
                    (assessed.isoformat(), assessed.isoformat(), valid.isoformat(), row_id),
                )

        with self.assertRaises(AllocationCalculationError):
            self.service.history()

    def test_future_allocation_assessment_is_never_current(self) -> None:
        self._confirm_and_assess()
        allocation = self.service.ranges()
        future = NOW + timedelta(hours=1)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
            connection.execute(
                "UPDATE allocation_assessments SET assessed_at = ?, created_at = ? WHERE id = ?",
                (future.isoformat(), future.isoformat(), allocation.assessment_id),
            )

        with self.assertRaises(AllocationCalculationError):
            self.service.status()

    def test_legitimate_superseded_historical_chain_remains_available(self) -> None:
        first_profile, _ = self._confirm_and_assess()
        first = self.service.ranges()
        self.clock.value += timedelta(minutes=1)
        self._confirm_and_assess()

        status = self.service.status()
        history = self.service.history()

        self.assertEqual(status["state"], "stale")
        self.assertEqual(history[0]["assessment_id"], first.assessment_id)
        self.assertEqual(history[0]["profile_version_id"], first_profile.id)

    def test_deterministic_recalculation_mismatch_fails_status(self) -> None:
        self._confirm_and_assess()
        self.service.ranges()

        with patch(
            "kunjin.allocation.service.evaluate_allocation",
            side_effect=ValueError("synthetic deterministic mismatch"),
        ):
            with self.assertRaises(AllocationCalculationError):
                self.service.status()

    def test_policy_content_mismatch_is_a_stable_policy_error(self) -> None:
        self._confirm_and_assess()
        self.service.policy()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_policy_no_update")
            connection.execute(
                "UPDATE allocation_policy_versions SET policy_checksum = ?",
                ("f" * 64,),
            )
        with self.assertRaises(AllocationPolicyError):
            self.service.ranges()

    def test_safe_views_never_expose_amounts_or_private_names(self) -> None:
        private_profile = replace(
            valid_profile(),
            monthly_net_income=Decimal("87654321.09"),
            goals=(replace(valid_profile().goals[0], name="PRIVATE-GOAL-SENTINEL"),),
        )
        self._confirm_and_assess(private_profile)
        execution = self.service.ranges()

        safe_text = json.dumps(execution.safe_json(), sort_keys=True)
        status_text = json.dumps(self.service.status(), sort_keys=True)
        history_text = json.dumps(self.service.history(), sort_keys=True)
        for rendered in (safe_text, status_text, history_text):
            self.assertNotIn("87654321.09", rendered)
            self.assertNotIn("PRIVATE-GOAL-SENTINEL", rendered)
        self.assertIn("PRIVATE-GOAL-SENTINEL", json.dumps(execution.local_view()))

    def test_stale_suitability_cannot_authenticate_a_range(self) -> None:
        self._confirm_and_assess()
        self.clock.value = NOW + timedelta(hours=24)

        with self.assertRaises(AllocationCalculationError):
            self.service.ranges()
        self.assertEqual(self.allocation_store.history(), ())
