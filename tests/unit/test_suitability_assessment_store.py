from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import EncryptedAssessment, EncryptedProfile
from kunjin.suitability.models import (
    AssessmentAmounts,
    AssessmentResult,
    AssessmentStatus,
    BlockReason,
    ConstraintReason,
)
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.store import (
    ProfileStore,
    SuitabilityAssessmentStore,
    SuitabilityPolicyStore,
)

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def assessment_result(
    *,
    amount_marker: str = "12345.67",
    status: AssessmentStatus = AssessmentStatus.CONSTRAINED,
) -> AssessmentResult:
    constraints = (
        (ConstraintReason.MONTHLY_CEILING_CONSTRAINED,)
        if status is AssessmentStatus.CONSTRAINED
        else ()
    )
    hard_blocks = (
        (BlockReason.EMERGENCY_RESERVE_SHORTFALL,) if status is AssessmentStatus.BLOCKED else ()
    )
    marker = Decimal(amount_marker)
    return AssessmentResult(
        status=status,
        hard_blocks=hard_blocks,
        constraints=constraints,
        required_reserve_months=6,
        risk_answers_consistent=True,
        profile_conflicts=(),
        debt_count=1,
        obligation_count=2,
        goal_count=3,
        amounts=AssessmentAmounts(
            verified_emergency_reserve=marker,
            required_emergency_reserve=marker,
            emergency_reserve_shortfall=marker,
            required_monthly_obligation_saving=marker,
            required_monthly_goal_saving=marker,
            monthly_safety_residual=marker,
            safe_monthly_ceiling=marker,
        ),
    )


def encrypted_assessment(marker: str) -> EncryptedAssessment:
    return EncryptedAssessment(
        algorithm="AES-256-GCM",
        key_version="1",
        nonce=f"nonce-{marker}",
        ciphertext=f"ciphertext-{marker}",
        keyed_fingerprint=marker * 64,
    )


class SuitabilityAssessmentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.policy_store = SuitabilityPolicyStore(self.repository)
        self.assessment_store = SuitabilityAssessmentStore(self.repository)
        self.policy = SuitabilityPolicyV1()
        self.policy_store.ensure(self.policy)
        self.profile_id = self._insert_profile()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _insert_profile(self) -> int:
        profile_store = ProfileStore(self.repository)
        metadata = profile_store.confirm(
            EncryptedProfile(
                algorithm="AES-256-GCM",
                key_version="1",
                nonce="profile-nonce",
                ciphertext="profile-ciphertext",
                keyed_fingerprint="f" * 64,
            ),
            NOW,
            NOW + timedelta(days=90),
        )
        return metadata.id

    def _insert_assessment(
        self,
        *,
        marker: str = "a",
        assessed_at: datetime = NOW,
        valid_until: datetime | None = None,
        result: AssessmentResult | None = None,
    ):
        return self.assessment_store.insert(
            profile_version_id=self.profile_id,
            policy_version=self.policy.version,
            input_fingerprint=marker * 64,
            result=result or assessment_result(),
            encrypted=encrypted_assessment(marker),
            assessed_at=assessed_at,
            valid_until=valid_until or assessed_at + timedelta(hours=24),
        )

    def _overwrite_assessment_field(self, field: str, value: str) -> None:
        allowed = {
            "status",
            "hard_blocks_json",
            "constraints_json",
            "safe_summary_json",
            "assessed_at",
            "valid_until",
            "created_at",
        }
        self.assertIn(field, allowed)
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                f"UPDATE suitability_assessments SET {field} = ? WHERE id = 1",
                (value,),
            )

    def test_policy_ensure_inserts_and_same_content_is_idempotent(self) -> None:
        first = self.policy_store.get(self.policy.version)
        second = self.policy_store.ensure(self.policy)

        self.assertIsNotNone(first)
        self.assertEqual(second, first)
        self.assertEqual(second.version, "1")
        self.assertEqual(second.canonical_policy_json, self.policy.canonical_json().decode())
        self.assertEqual(second.policy_checksum, self.policy.checksum())
        self.assertEqual(second.effective_at, self.policy.effective_at)
        with self.repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM suitability_policy_versions"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_policy_ensure_rejects_existing_version_with_different_content(self) -> None:
        other_repository = Repository(Path(self.temporary_directory.name) / "conflicting-policy.db")
        other_repository.migrate()
        with other_repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "1",
                    '{"version":"different"}',
                    "d" * 64,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )

        with self.assertRaisesRegex(ValueError, "content does not match"):
            SuitabilityPolicyStore(other_repository).ensure(self.policy)

    def test_policy_ensure_rejects_malicious_subclass_without_leaving_a_row(self) -> None:
        class MaliciousPolicy(SuitabilityPolicyV1):
            def validate(self) -> None:
                return None

            def canonical_json(self) -> bytes:
                return b'{"version":"evil"}'

            def checksum(self) -> str:
                return "e" * 64

        repository = Repository(Path(self.temporary_directory.name) / "malicious-policy.db")
        repository.migrate()
        store = SuitabilityPolicyStore(repository)

        with self.assertRaisesRegex(ValueError, "exact SuitabilityPolicyV1"):
            store.ensure(MaliciousPolicy())

        with repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM suitability_policy_versions"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(store.ensure(SuitabilityPolicyV1()).version, "1")

    def test_policy_ensure_validation_failure_leaves_no_row_and_can_retry(self) -> None:
        repository = Repository(Path(self.temporary_directory.name) / "invalid-policy.db")
        repository.migrate()
        store = SuitabilityPolicyStore(repository)
        invalid = replace(SuitabilityPolicyV1(), reserve_months_stable=7)

        with self.assertRaisesRegex(ValueError, "stable reserve months"):
            store.ensure(invalid)

        with repository.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM suitability_policy_versions"
            ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(store.ensure(SuitabilityPolicyV1()).version, "1")

    def test_policy_get_rejects_noncanonical_or_invalid_stored_fields(self) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions SET effective_at = ?",
                ("2026-07-12 12:00:00",),
            )

        with self.assertRaisesRegex(ValueError, "ISO datetime"):
            self.policy_store.get("1")

    def test_policy_get_rejects_json_version_that_does_not_match_row(self) -> None:
        canonical = '{"version":"2"}'

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions "
                "SET canonical_policy_json = ?, policy_checksum = ?",
                (canonical, hashlib.sha256(canonical.encode()).hexdigest()),
            )

        with self.assertRaisesRegex(ValueError, "version does not match"):
            self.policy_store.get("1")

    def test_policy_get_rejects_modified_v1_content_with_recomputed_checksum(self) -> None:
        payload = json.loads(self.policy.canonical_json())
        payload["reserve_months_stable"] = 7
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        checksum = hashlib.sha256(canonical.encode()).hexdigest()
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions "
                "SET canonical_policy_json = ?, policy_checksum = ?",
                (canonical, checksum),
            )

        with self.assertRaisesRegex(ValueError, "fixed V1"):
            self.policy_store.get("1")

    def test_policy_get_rejects_unknown_self_consistent_version(self) -> None:
        other_repository = Repository(Path(self.temporary_directory.name) / "unknown-policy.db")
        other_repository.migrate()
        canonical = '{"version":"2"}'
        with other_repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "2",
                    canonical,
                    hashlib.sha256(canonical.encode()).hexdigest(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )

        with self.assertRaisesRegex(ValueError, "unsupported"):
            SuitabilityPolicyStore(other_repository).get("2")

    def test_policy_effective_at_must_match_fixed_v1_absolute_instant(self) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions SET effective_at = ?",
                ((self.policy.effective_at + timedelta(seconds=1)).isoformat(),),
            )

        with self.assertRaisesRegex(ValueError, "effective_at"):
            self.policy_store.get("1")
        with self.assertRaisesRegex(ValueError, "effective_at"):
            self.policy_store.ensure(self.policy)

    def test_policy_effective_at_accepts_same_instant_with_another_offset(self) -> None:
        equivalent = self.policy.effective_at.astimezone(timezone(timedelta(hours=8)))
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_policy_no_update")
            connection.execute(
                "UPDATE suitability_policy_versions SET effective_at = ?",
                (equivalent.isoformat(),),
            )

        record = self.policy_store.ensure(self.policy)

        self.assertEqual(record.effective_at, self.policy.effective_at)

    def test_insert_round_trips_metadata_and_encrypted_fields(self) -> None:
        expected_encrypted = encrypted_assessment("a")
        metadata = self._insert_assessment()
        stored = self.assessment_store.latest_for(self.profile_id, "1")

        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata, metadata)
        self.assertEqual(stored.encrypted, expected_encrypted)

        by_id = self.assessment_store.get(metadata.id)
        self.assertEqual(by_id, stored)
        self.assertIsNone(self.assessment_store.get(metadata.id + 1))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.assessment_store.get(0)
        self.assertEqual(metadata.status, AssessmentStatus.CONSTRAINED)
        self.assertEqual(
            metadata.constraints,
            (ConstraintReason.MONTHLY_CEILING_CONSTRAINED,),
        )
        self.assertEqual(metadata.hard_blocks, ())
        self.assertEqual(
            metadata.safe_summary.as_dict(),
            {
                "debt_count": 1,
                "goal_count": 3,
                "obligation_count": 2,
                "required_reserve_months": 6,
                "risk_answers_consistent": True,
            },
        )

    def test_assessment_safe_summary_is_deeply_immutable(self) -> None:
        metadata = self._insert_assessment()

        with self.assertRaises(FrozenInstanceError):
            metadata.safe_summary.debt_count = 99

        external = metadata.safe_summary.as_dict()
        external["debt_count"] = 99
        self.assertEqual(metadata.safe_summary.debt_count, 1)

    def test_plaintext_json_fields_are_amount_free_and_compact(self) -> None:
        marker = "987654321.12"
        self._insert_assessment(result=assessment_result(amount_marker=marker))

        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT hard_blocks_json, constraints_json, safe_summary_json "
                "FROM suitability_assessments"
            ).fetchone()

        self.assertEqual(row["hard_blocks_json"], "[]")
        self.assertEqual(
            row["constraints_json"],
            '["monthly_ceiling_constrained"]',
        )
        self.assertEqual(
            json.loads(row["safe_summary_json"]),
            assessment_result(amount_marker=marker).safe_summary(),
        )
        self.assertNotIn(marker, "".join(str(value) for value in row))
        self.assertNotIn(" ", row["safe_summary_json"])

    def test_history_is_metadata_only_and_ordered_by_absolute_instant(self) -> None:
        later_absolute = datetime(2026, 7, 12, 12, tzinfo=timezone(timedelta(hours=8)))
        earlier_absolute = datetime(2026, 7, 12, 3, tzinfo=timezone.utc)
        early = self._insert_assessment(marker="a", assessed_at=earlier_absolute)
        late = self._insert_assessment(marker="b", assessed_at=later_absolute)

        history = self.assessment_store.history()

        self.assertEqual([item.id for item in history], [late.id, early.id])
        self.assertFalse(hasattr(history[0], "encrypted"))
        self.assertNotIn("ciphertext", repr(history))

    def test_latest_for_is_scoped_and_uses_absolute_instant_then_id(self) -> None:
        earlier_absolute = datetime(2026, 7, 12, 12, tzinfo=timezone(timedelta(hours=8)))
        later_absolute = datetime(2026, 7, 12, 5, tzinfo=timezone.utc)
        self._insert_assessment(marker="a", assessed_at=earlier_absolute)
        expected = self._insert_assessment(marker="b", assessed_at=later_absolute)

        latest = self.assessment_store.latest_for(self.profile_id, "1")

        self.assertIsNotNone(latest)
        self.assertEqual(latest.metadata.id, expected.id)
        self.assertEqual(latest.encrypted, encrypted_assessment("b"))
        self.assertIsNone(self.assessment_store.latest_for(self.profile_id, "missing"))

    def test_latest_for_orders_legacy_offsets_by_exact_integer_microseconds(self) -> None:
        first = self._insert_assessment(marker="a", assessed_at=NOW)
        second = self._insert_assessment(
            marker="b",
            assessed_at=NOW + timedelta(hours=1),
        )

        def rewrite(row_id: int, assessed_at: str, valid_until: str) -> None:
            with self.repository.connect() as connection, connection:
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    "UPDATE suitability_assessments SET assessed_at = ?, "
                    "valid_until = ?, created_at = ? WHERE id = ?",
                    (assessed_at, valid_until, assessed_at, row_id),
                )

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")

        cases = (
            (
                "absolute offset order",
                ("2026-07-12T12:00:00+08:00", "2026-07-12T13:00:00+08:00"),
                ("2026-07-12T05:00:00Z", "2026-07-12T06:00:00Z"),
                second.id,
            ),
            (
                "microsecond order",
                ("2026-07-12T13:00:00+08:00", "2026-07-12T14:00:00+08:00"),
                (
                    "2026-07-12T05:00:00.000001Z",
                    "2026-07-12T06:00:00.000001Z",
                ),
                second.id,
            ),
            (
                "equal instant id tie",
                ("2026-07-12T13:00:00+08:00", "2026-07-12T14:00:00+08:00"),
                ("2026-07-12T05:00:00Z", "2026-07-12T06:00:00Z"),
                second.id,
            ),
        )
        for label, first_times, second_times, expected_id in cases:
            with self.subTest(label=label):
                rewrite(first.id, *first_times)
                rewrite(second.id, *second_times)

                selected = self.assessment_store.latest_for(self.profile_id, "1")

                self.assertIsNotNone(selected)
                self.assertEqual(selected.metadata.id, expected_id)

    def test_insert_rejects_naive_datetimes_and_real_time_reversal(self) -> None:
        naive = datetime(2026, 7, 12, 12)
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            self._insert_assessment(assessed_at=naive)

        assessed_at = datetime(2026, 7, 12, 12, tzinfo=timezone(timedelta(hours=8)))
        earlier_absolute = datetime(2026, 7, 12, 3, 59, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ValueError, "after assessed_at"):
            self._insert_assessment(
                assessed_at=assessed_at,
                valid_until=earlier_absolute,
            )

    def test_insert_validates_result_encryption_and_fingerprint(self) -> None:
        invalid_result = replace(
            assessment_result(),
            status=AssessmentStatus.READY_FOR_ALLOCATION,
        )
        with self.assertRaisesRegex(ValueError, "status must match"):
            self._insert_assessment(result=invalid_result)

        with self.assertRaisesRegex(ValueError, "input_fingerprint"):
            self.assessment_store.insert(
                self.profile_id,
                "1",
                "not-a-fingerprint",
                assessment_result(),
                encrypted_assessment("a"),
                NOW,
                NOW + timedelta(hours=1),
            )
        with self.assertRaisesRegex(ValueError, "encrypted assessment"):
            self.assessment_store.insert(
                self.profile_id,
                "1",
                "a" * 64,
                assessment_result(),
                replace(encrypted_assessment("a"), algorithm="unknown"),
                NOW,
                NOW + timedelta(hours=1),
            )

    def test_read_rejects_invalid_json_container_unknown_enum_and_duplicates(self) -> None:
        corruptions = (
            ("hard_blocks_json", '{"not":"an array"}', "JSON array"),
            ("hard_blocks_json", '["not_a_reason"]', "supported block reason"),
            (
                "constraints_json",
                '["monthly_ceiling_constrained","monthly_ceiling_constrained"]',
                "duplicates",
            ),
            (
                "safe_summary_json",
                '{"debt_count":1,"debt_count":2}',
                "duplicate",
            ),
            ("safe_summary_json", '{"exact_amount":"99.00"}', "safe summary"),
            ("safe_summary_json", '{"debt_count":NaN}', "valid JSON"),
            ("status", "ready_for_allocation", "status does not match"),
        )
        for field, value, message in corruptions:
            with self.subTest(field=field, value=value):
                database = Path(self.temporary_directory.name) / (
                    f"corrupt-{field}-{abs(hash(value))}.db"
                )
                repository = Repository(database)
                repository.migrate()
                policy_store = SuitabilityPolicyStore(repository)
                policy_store.ensure(self.policy)
                profile = ProfileStore(repository).confirm(
                    EncryptedProfile("AES-256-GCM", "1", "n", "c", "f" * 64),
                    NOW,
                    NOW + timedelta(days=1),
                )
                store = SuitabilityAssessmentStore(repository)
                store.insert(
                    profile.id,
                    "1",
                    "a" * 64,
                    assessment_result(),
                    encrypted_assessment("a"),
                    NOW,
                    NOW + timedelta(hours=1),
                )
                with repository.connect() as connection, connection:
                    connection.execute("DROP TRIGGER suitability_assessment_no_update")
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    connection.execute(
                        f"UPDATE suitability_assessments SET {field} = ?",
                        (value,),
                    )
                with self.assertRaisesRegex(ValueError, message):
                    store.history()

    def test_latest_for_does_not_hide_a_corrupt_timestamp_row(self) -> None:
        self._insert_assessment(marker="a", assessed_at=NOW)
        self._insert_assessment(
            marker="b",
            assessed_at=NOW + timedelta(hours=1),
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE suitability_assessments SET assessed_at = ? WHERE id = 2",
                ("not-a-time",),
            )

        with self.assertRaisesRegex(ValueError, "ISO datetime"):
            self.assessment_store.latest_for(self.profile_id, "1")

    def test_latest_for_parses_only_selected_row_and_ignores_obsolete_malformed_row(
        self,
    ) -> None:
        obsolete = self._insert_assessment(marker="a", assessed_at=NOW)
        latest = self._insert_assessment(
            marker="b",
            assessed_at=NOW + timedelta(hours=1),
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE suitability_assessments SET safe_summary_json = ? WHERE id = ?",
                ('{"malformed":true}', obsolete.id),
            )

        selected = self.assessment_store.latest_for(self.profile_id, "1")

        self.assertIsNotNone(selected)
        self.assertEqual(selected.metadata.id, latest.id)
        self.assertEqual(selected.encrypted, encrypted_assessment("b"))

    def test_read_rejects_noncanonical_or_reversed_stored_datetimes(self) -> None:
        self._insert_assessment()
        self._overwrite_assessment_field("assessed_at", "2026-07-12 12:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "ISO datetime"):
            self.assessment_store.history()

        other_repository = Repository(Path(self.temporary_directory.name) / "reversed-time.db")
        other_repository.migrate()
        SuitabilityPolicyStore(other_repository).ensure(self.policy)
        profile = ProfileStore(other_repository).confirm(
            EncryptedProfile("AES-256-GCM", "1", "n", "c", "f" * 64),
            NOW,
            NOW + timedelta(days=1),
        )
        other_store = SuitabilityAssessmentStore(other_repository)
        other_store.insert(
            profile.id,
            "1",
            "a" * 64,
            assessment_result(),
            encrypted_assessment("a"),
            NOW,
            NOW + timedelta(hours=1),
        )
        with other_repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER suitability_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE suitability_assessments SET valid_until = ?",
                ((NOW - timedelta(seconds=1)).isoformat(),),
            )
        with self.assertRaisesRegex(ValueError, "after assessed_at"):
            other_store.latest_for(profile.id, "1")


if __name__ == "__main__":
    unittest.main()
