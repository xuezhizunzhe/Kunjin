from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kunjin.allocation.crypto import EncryptedAllocationAssessment
from kunjin.allocation.engine import evaluate_allocation
from kunjin.allocation.models import AllocationConstraintCode, AllocationStatus
from kunjin.allocation.policy import AllocationPolicyV1
from kunjin.allocation.store import (
    MAX_HISTORY_OFFSET,
    MAX_HISTORY_PAGE_SIZE,
    AllocationAssessmentConflictError,
    AllocationAssessmentMetadata,
    AllocationAssessmentStore,
    AllocationBindingChangedError,
    AllocationPolicyStore,
    StoredEncryptedAllocationAssessment,
)
from kunjin.storage.repository import Repository
from kunjin.suitability.crypto import EncryptedAssessment, EncryptedProfile
from kunjin.suitability.engine import evaluate as evaluate_suitability
from kunjin.suitability.models import FinancialGoal
from kunjin.suitability.policy import SuitabilityPolicyV1
from kunjin.suitability.store import (
    ProfileStore,
    SuitabilityAssessmentStore,
    SuitabilityPolicyStore,
)
from tests.unit.test_suitability_assessment_store import assessment_result
from tests.unit.test_suitability_models import valid_profile

NOW = datetime(2026, 7, 12, 12, tzinfo=timezone.utc)


def _allocation_result():
    profile = replace(
        valid_profile(),
        immediately_available_cash=Decimal("30000.00"),
        cash_like_assets=Decimal("20000.00"),
        emergency_reserve=Decimal("39000.00"),
        minimum_operating_cash=Decimal("5000.00"),
        low_risk_fixed_income_assets=Decimal("10000.00"),
        manual_equity_fund_assets=Decimal("10000.00"),
        debts=(),
        obligations=(),
        goals=(
            FinancialGoal(
                name="synthetic-purpose",
                target_amount=Decimal("1000.00"),
                target_date=date(2031, 7, 13),
                priority=1,
                amount_already_reserved=Decimal("0.00"),
                temporary_principal_loss_acceptable=False,
                use_date_can_be_postponed=False,
            ),
        ),
    )
    suitability = evaluate_suitability(profile, SuitabilityPolicyV1(), NOW)
    result = evaluate_allocation(profile, suitability, AllocationPolicyV1(), NOW)
    assert result.status is AllocationStatus.RANGE_AVAILABLE
    return result


def _encrypted(marker: int = 1) -> EncryptedAllocationAssessment:
    nonce = bytes([marker]) * 12
    ciphertext = bytes([marker]) * 32
    return EncryptedAllocationAssessment(
        algorithm="AES-256-GCM",
        key_version="1",
        nonce=base64.urlsafe_b64encode(nonce).decode("ascii"),
        ciphertext=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        keyed_fingerprint=f"{marker:x}" * 64,
    )


def _reencrypted(marker: int = 2) -> EncryptedAllocationAssessment:
    variant = _encrypted(marker)
    return replace(
        _encrypted(1),
        nonce=variant.nonce,
        ciphertext=variant.ciphertext,
    )


class AllocationStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.policy = AllocationPolicyV1()
        self.policy_store = AllocationPolicyStore(self.repository)
        self.policy_store.ensure(self.policy)
        SuitabilityPolicyStore(self.repository).ensure(SuitabilityPolicyV1())
        self.profile = ProfileStore(self.repository).confirm(
            EncryptedProfile("AES-256-GCM", "1", "n", "c", "a" * 64),
            NOW - timedelta(hours=1),
            NOW + timedelta(days=30),
        )
        self.suitability_store = SuitabilityAssessmentStore(self.repository)
        suitability_result = assessment_result()
        self.suitability = self.suitability_store.insert(
            self.profile.id,
            "1",
            "b" * 64,
            suitability_result,
            EncryptedAssessment("AES-256-GCM", "1", "n", "c", "c" * 64),
            NOW - timedelta(minutes=1),
            NOW + timedelta(hours=23),
        )
        self.store = AllocationAssessmentStore(self.repository)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _insert(self, **changes):
        fields = {
            "profile_version_id": self.profile.id,
            "suitability_assessment_id": self.suitability.id,
            "expected_profile_fingerprint": "a" * 64,
            "expected_suitability_input_fingerprint": "b" * 64,
            "suitability_policy_version": "1",
            "policy_version": "1",
            "input_fingerprint": "d" * 64,
            "result": _allocation_result(),
            "encrypted": _encrypted(),
            "assessed_at": NOW,
            "valid_until": NOW + timedelta(hours=23),
        }
        fields.update(changes)
        return self.store.insert(**fields)

    def _tamper(self, field: str, value: object) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER IF EXISTS allocation_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                f"UPDATE allocation_assessments SET {field} = ?",
                (value,),
            )

    def test_policy_ensure_is_idempotent_and_exact(self) -> None:
        first = self.policy_store.get("1")
        second = self.policy_store.ensure(self.policy)
        self.assertEqual(first, second)
        self.assertEqual(second.policy_checksum, self.policy.checksum())
        self.assertEqual(second.canonical_policy_json, self.policy.canonical_json().decode())

    def test_policy_rejects_subclass_and_content_conflict(self) -> None:
        class Malicious(AllocationPolicyV1):
            def validate(self) -> None:
                return None

        with self.assertRaisesRegex(ValueError, "exact AllocationPolicyV1"):
            self.policy_store.ensure(Malicious())

        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_policy_no_update")
            canonical = '{"version":"1"}'
            connection.execute(
                "UPDATE allocation_policy_versions SET canonical_policy_json = ?, "
                "policy_checksum = ?",
                (canonical, hashlib.sha256(canonical.encode()).hexdigest()),
            )
        with self.assertRaisesRegex(ValueError, "fixed V1|content does not match"):
            self.policy_store.ensure(self.policy)

    def test_store_constructors_reject_repository_boundary_attacks(self) -> None:
        class RepositorySubclass(Repository):
            pass

        class Proxy:
            database = Path(self.temporary_directory.name) / "proxy.db"

            def connect(self):
                return self.repository.connect()

        attacks = [
            RepositorySubclass(Path(self.temporary_directory.name) / "subclass.db"),
            Proxy(),
        ]
        extra_state = Repository(Path(self.temporary_directory.name) / "extra.db")
        extra_state.unexpected = True
        attacks.append(extra_state)
        overridden = Repository(Path(self.temporary_directory.name) / "override.db")
        overridden.connect = lambda: self.repository.connect()
        attacks.append(overridden)
        invalid_path = Repository(Path(self.temporary_directory.name) / "invalid.db")
        invalid_path.database = str(invalid_path.database)
        attacks.append(invalid_path)

        for repository in attacks:
            with self.subTest(repository=type(repository).__name__):
                with self.assertRaisesRegex(ValueError, "Repository|path"):
                    AllocationPolicyStore(repository)
                with self.assertRaisesRegex(ValueError, "Repository|path"):
                    AllocationAssessmentStore(repository)

    def test_stores_snapshot_database_and_never_reuse_mutated_caller(self) -> None:
        original_path = Path(self.temporary_directory.name) / "snapshot.db"
        redirected_path = Path(self.temporary_directory.name) / "redirected.db"
        caller = Repository(original_path)
        caller.migrate()
        policy_store = AllocationPolicyStore(caller)
        assessment_store = AllocationAssessmentStore(caller)
        self.assertEqual(set(vars(policy_store)), {"_repository"})
        self.assertEqual(set(vars(assessment_store)), {"_repository"})
        self.assertIsNot(policy_store._repository, caller)
        self.assertIsNot(assessment_store._repository, caller)

        caller.database = redirected_path
        caller.connect = lambda: (_ for _ in ()).throw(AssertionError("caller reused"))

        record = policy_store.ensure(AllocationPolicyV1())

        self.assertEqual(record.version, "1")
        self.assertEqual(assessment_store.history(), ())
        with Repository(original_path).connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM allocation_policy_versions"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertFalse(redirected_path.exists())

    def test_policy_created_at_is_fixed_for_get_and_ensure(self) -> None:
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_policy_no_update")
            connection.execute(
                "UPDATE allocation_policy_versions SET created_at = ?",
                ((self.policy.effective_at + timedelta(seconds=1)).isoformat(),),
            )
        with self.assertRaisesRegex(ValueError, "created_at"):
            self.policy_store.get("1")
        with self.assertRaisesRegex(ValueError, "created_at"):
            self.policy_store.ensure(self.policy)

    def test_insert_round_trips_frozen_amount_free_metadata(self) -> None:
        metadata = self._insert()
        stored = self.store.latest_for(self.profile.id, self.suitability.id, "1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata, metadata)
        self.assertEqual(stored.encrypted, _encrypted())
        with self.assertRaises(FrozenInstanceError):
            metadata.input_fingerprint = "e" * 64
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT permitted_region_json, binding_constraints_json, safe_summary_json "
                "FROM allocation_assessments"
            ).fetchone()
        plaintext = "".join(str(value) for value in row)
        self.assertNotIn("synthetic-purpose", plaintext)
        self.assertNotIn("30000.00", plaintext)
        self.assertEqual(
            row["permitted_region_json"],
            json.dumps(
                json.loads(row["permitted_region_json"]), separators=(",", ":"), sort_keys=True
            ),
        )

    def test_get_returns_one_authenticated_stored_row_by_id(self) -> None:
        metadata = self._insert()

        stored = self.store.get(metadata.id)

        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata, metadata)
        self.assertEqual(stored.encrypted, _encrypted())
        self.assertIsNone(self.store.get(metadata.id + 1))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.store.get(0)

    def test_insert_is_idempotent_for_same_fingerprint_and_ignores_new_ciphertext(self) -> None:
        first = self._insert(encrypted=_encrypted(1))
        second = self._insert(
            encrypted=_reencrypted(2),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )

        self.assertEqual(second, first)
        with self.repository.connect() as connection:
            rows = connection.execute("SELECT id, nonce FROM allocation_assessments").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], first.id)
        self.assertEqual(rows[0]["nonce"], _encrypted(1).nonce)

    def test_idempotent_insert_rejects_expired_backward_or_different_date_reuse(self) -> None:
        cases = (
            (
                "expired",
                NOW + timedelta(seconds=1),
                NOW + timedelta(seconds=1),
            ),
            ("backward", NOW - timedelta(seconds=1), None),
            ("different-date", NOW + timedelta(hours=12), None),
        )
        for name, incoming_time, expired_at in cases:
            with self.subTest(name=name):
                database = Path(self.temporary_directory.name) / f"reuse-{name}.db"
                repository = Repository(database)
                repository.migrate()
                AllocationPolicyStore(repository).ensure(AllocationPolicyV1())
                SuitabilityPolicyStore(repository).ensure(SuitabilityPolicyV1())
                profile = ProfileStore(repository).confirm(
                    EncryptedProfile("AES-256-GCM", "1", "n", "c", "a" * 64),
                    NOW - timedelta(hours=1),
                    NOW + timedelta(days=30),
                )
                suitability = SuitabilityAssessmentStore(repository).insert(
                    profile.id,
                    "1",
                    "b" * 64,
                    assessment_result(),
                    EncryptedAssessment("AES-256-GCM", "1", "n", "c", "c" * 64),
                    NOW - timedelta(minutes=1),
                    NOW + timedelta(hours=23),
                )
                store = AllocationAssessmentStore(repository)
                fields = {
                    "profile_version_id": profile.id,
                    "suitability_assessment_id": suitability.id,
                    "expected_profile_fingerprint": "a" * 64,
                    "expected_suitability_input_fingerprint": "b" * 64,
                    "suitability_policy_version": "1",
                    "policy_version": "1",
                    "input_fingerprint": "d" * 64,
                    "result": _allocation_result(),
                    "encrypted": _encrypted(1),
                    "assessed_at": NOW,
                    "valid_until": NOW + timedelta(hours=23),
                }
                store.insert(**fields)
                if expired_at is not None:
                    with repository.connect() as connection, connection:
                        connection.execute("DROP TRIGGER allocation_assessment_no_update")
                        connection.execute(
                            "UPDATE allocation_assessments SET valid_until = ?",
                            (expired_at.isoformat(),),
                        )
                fields.update(
                    assessed_at=incoming_time,
                    valid_until=NOW + timedelta(hours=23),
                    encrypted=_reencrypted(2),
                )
                with self.assertRaisesRegex(
                    AllocationAssessmentConflictError,
                    "different content",
                ):
                    store.insert(**fields)
                with repository.connect() as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) FROM allocation_assessments"
                    ).fetchone()[0]
                self.assertEqual(count, 1)

    def test_idempotent_insert_rejects_changed_authenticated_encryption_metadata(self) -> None:
        self._insert(encrypted=_encrypted(1))
        conflicts = (
            replace(_reencrypted(2), keyed_fingerprint="2" * 64),
            replace(_reencrypted(2), algorithm="AES-256-GCM-v2"),
            replace(_reencrypted(2), key_version="2"),
        )
        for encrypted in conflicts:
            with self.subTest(encrypted=encrypted):
                with self.assertRaisesRegex(
                    AllocationAssessmentConflictError,
                    "encryption metadata conflicts",
                ):
                    self._insert(encrypted=encrypted)
        with self.repository.connect() as connection:
            rows = connection.execute(
                "SELECT nonce, keyed_payload_fingerprint FROM allocation_assessments"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nonce"], _encrypted(1).nonce)
        self.assertEqual(rows[0]["keyed_payload_fingerprint"], "1" * 64)

    def test_insert_same_fingerprint_with_different_metadata_is_a_conflict(self) -> None:
        self._insert()
        result = _allocation_result()
        altered = replace(
            result,
            binding_constraints=(
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
                *result.binding_constraints,
            ),
        )
        altered.validate()
        with self.assertRaisesRegex(AllocationAssessmentConflictError, "different content"):
            self._insert(result=altered)
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM allocation_assessments").fetchone()[0]
        self.assertEqual(count, 1)

    def test_concurrent_same_fingerprint_insert_serializes_to_one_row(self) -> None:
        def insert(marker: int):
            return self._insert(encrypted=_encrypted(1) if marker == 1 else _reencrypted(marker))

        with ThreadPoolExecutor(max_workers=2) as executor:
            metadata = tuple(executor.map(insert, (1, 2)))

        self.assertEqual(metadata[0].id, metadata[1].id)
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM allocation_assessments").fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_rejects_blocked_results_and_bad_encryption_metadata(self) -> None:
        blocked = replace(
            _allocation_result(),
            status=AllocationStatus.BLOCKED,
        )
        with self.assertRaises(ValueError):
            self._insert(result=blocked)
        for encrypted in (
            replace(_encrypted(), algorithm="unknown"),
            replace(_encrypted(), nonce="not-base64"),
            replace(_encrypted(), ciphertext=base64.urlsafe_b64encode(b"short").decode()),
            replace(_encrypted(), keyed_fingerprint="A" * 64),
        ):
            with self.subTest(encrypted=encrypted):
                with self.assertRaises(ValueError):
                    self._insert(encrypted=encrypted)
        self.assertEqual(self.store.history(), ())

    def test_insert_asserts_profile_and_suitability_bindings_atomically(self) -> None:
        cases = (
            {"expected_profile_fingerprint": "f" * 64},
            {"expected_suitability_input_fingerprint": "f" * 64},
            {"suitability_policy_version": "other"},
            {"suitability_assessment_id": self.suitability.id + 100},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(AllocationBindingChangedError):
                    self._insert(**changes)
        self.assertEqual(self.store.history(), ())

    def test_insert_rejects_superseded_profile_or_nonlatest_suitability(self) -> None:
        old_suitability = self.suitability
        newer = self.suitability_store.insert(
            self.profile.id,
            "1",
            "e" * 64,
            evaluate_suitability(valid_profile(), SuitabilityPolicyV1(), NOW),
            EncryptedAssessment("AES-256-GCM", "1", "n2", "c2", "e" * 64),
            NOW,
            NOW + timedelta(hours=20),
        )
        with self.assertRaises(AllocationBindingChangedError):
            self._insert(suitability_assessment_id=old_suitability.id)

        ProfileStore(self.repository).confirm(
            EncryptedProfile("AES-256-GCM", "1", "n3", "c3", "f" * 64),
            NOW,
            NOW + timedelta(days=1),
        )
        with self.assertRaises(AllocationBindingChangedError):
            self._insert(
                suitability_assessment_id=newer.id,
                expected_suitability_input_fingerprint="e" * 64,
            )
        self.assertEqual(self.store.history(), ())

    def test_insert_rejects_expired_or_future_suitability(self) -> None:
        with self.assertRaises(AllocationBindingChangedError):
            self._insert(assessed_at=NOW + timedelta(days=2), valid_until=NOW + timedelta(days=3))
        with self.assertRaises(AllocationBindingChangedError):
            self._insert(assessed_at=NOW - timedelta(hours=2), valid_until=NOW + timedelta(hours=1))

    def test_insert_requires_exact_valid_until_minimum(self) -> None:
        with self.assertRaisesRegex(
            AllocationBindingChangedError,
            "valid_until",
        ):
            self._insert(valid_until=NOW + timedelta(hours=22))

    def test_read_rejects_cross_field_plaintext_conflicts(self) -> None:
        self._insert()
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT binding_constraints_json FROM allocation_assessments"
            ).fetchone()
        constraints = json.loads(row["binding_constraints_json"])
        local = {
            "horizon_binding",
            "loss_amount_binding",
            "drawdown_binding",
            "willingness_binding",
            "stability_binding",
        }
        changed = [item for item in constraints if item not in local]
        self._tamper(
            "binding_constraints_json",
            json.dumps(changed, separators=(",", ":"), sort_keys=True),
        )
        with self.assertRaisesRegex(ValueError, "local binding"):
            self.store.history()

    def test_insert_reads_back_before_commit(self) -> None:
        original = self.store._row_to_stored

        def fail_readback(row):
            raise ValueError("synthetic readback failure")

        self.store._row_to_stored = fail_readback
        try:
            with self.assertRaisesRegex(ValueError, "synthetic readback"):
                self._insert()
        finally:
            self.store._row_to_stored = original
        self.assertEqual(self.store.history(), ())

    def test_insert_rolls_back_every_valid_but_different_readback_field(self) -> None:
        original = self.store._row_to_stored
        result = _allocation_result()
        assert result.permitted_region is not None
        different_region = replace(
            result.permitted_region,
            maximum_equity=Decimal("0"),
            horizon_equity_ceiling=Decimal("0"),
            loss_amount_equity_ceiling=Decimal("0"),
            drawdown_equity_ceiling=Decimal("0"),
            willingness_equity_ceiling=Decimal("0"),
            stability_equity_ceiling=Decimal("0"),
        )
        different_region.validate()
        different_summary = replace(
            result.safe_summary,
            obligation_count=result.safe_summary.obligation_count + 1,
        )
        different_summary.validate()
        mutations = {
            "id": 999,
            "status": AllocationStatus.BLOCKED,
            "permitted_region": different_region,
            "binding_constraints": (
                AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
                *result.binding_constraints,
            ),
            "safe_summary": different_summary,
            "created_at": NOW + timedelta(seconds=1),
        }
        for field, value in mutations.items():
            with self.subTest(field=field):

                def altered_readback(row):
                    stored = original(row)
                    metadata = replace(stored.metadata, **{field: value})
                    self.assertIsInstance(metadata, AllocationAssessmentMetadata)
                    return StoredEncryptedAllocationAssessment(metadata, stored.encrypted)

                self.store._row_to_stored = altered_readback
                try:
                    with self.assertRaisesRegex(ValueError, "read-back does not match"):
                        self._insert(result=result)
                finally:
                    self.store._row_to_stored = original
                self.assertEqual(self.store.history(), ())

    def test_latest_and_history_use_absolute_time_then_id(self) -> None:
        first = self._insert(
            input_fingerprint="1" * 64,
            encrypted=_encrypted(1),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )
        second_time = (NOW + timedelta(minutes=1)).astimezone(timezone(timedelta(hours=8)))
        second = self._insert(
            input_fingerprint="2" * 64,
            encrypted=_encrypted(2),
            assessed_at=second_time,
            valid_until=NOW + timedelta(hours=23),
        )
        self.assertEqual([item.id for item in self.store.history()], [second.id, first.id])
        self.assertEqual(
            self.store.latest_for(self.profile.id, self.suitability.id, "1").metadata.id,
            second.id,
        )

    def test_latest_and_history_break_equal_timestamp_ties_by_descending_id(self) -> None:
        first = self._insert(
            input_fingerprint="1" * 64,
            encrypted=_encrypted(1),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )
        second = self._insert(
            input_fingerprint="2" * 64,
            encrypted=_encrypted(2),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )

        latest = self.store.latest_for(self.profile.id, self.suitability.id, "1")

        self.assertIsNotNone(latest)
        self.assertEqual(latest.metadata.id, second.id)
        self.assertEqual([item.id for item in self.store.history()], [second.id, first.id])

    def test_latest_parses_only_selected_row_and_ignores_obsolete_malformed_row(self) -> None:
        old = self._insert(
            input_fingerprint="1" * 64,
            encrypted=_encrypted(1),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )
        new_time = NOW + timedelta(minutes=1)
        new = self._insert(
            input_fingerprint="2" * 64,
            encrypted=_encrypted(2),
            assessed_at=new_time,
            valid_until=NOW + timedelta(hours=23),
        )
        with self.repository.connect() as connection, connection:
            connection.execute("DROP TRIGGER allocation_assessment_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE allocation_assessments SET valid_until = ? WHERE id = ?",
                ("malformed-obsolete-time", old.id),
            )

        latest = self.store.latest_for(self.profile.id, self.suitability.id, "1")

        self.assertIsNotNone(latest)
        self.assertEqual(latest.metadata.id, new.id)
        with self.assertRaisesRegex(ValueError, "ISO datetime"):
            self.store.history()

    def test_history_validates_pagination_and_preserves_sql_order(self) -> None:
        self._insert(
            input_fingerprint="1" * 64,
            encrypted=_encrypted(1),
            assessed_at=NOW,
            valid_until=NOW + timedelta(hours=23),
        )
        self._insert(
            input_fingerprint="2" * 64,
            encrypted=_encrypted(2),
            assessed_at=NOW + timedelta(minutes=1),
            valid_until=NOW + timedelta(hours=23),
        )
        self.assertEqual(MAX_HISTORY_PAGE_SIZE, 1_000)
        self.assertEqual(MAX_HISTORY_OFFSET, 2**63 - 1)
        self.assertEqual(len(self.store.history(limit=1, offset=0)), 1)
        self.assertEqual(len(self.store.history(limit=1, offset=1)), 1)
        self.assertGreater(
            self.store.history(limit=1, offset=0)[0].id,
            self.store.history(limit=1, offset=1)[0].id,
        )
        for limit in (0, MAX_HISTORY_PAGE_SIZE + 1, True, "1"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, "limit"):
                    self.store.history(limit=limit)
        self.assertEqual(self.store.history(offset=MAX_HISTORY_OFFSET), ())
        for offset in (-1, True, "0", MAX_HISTORY_OFFSET + 1, 10**100):
            with self.subTest(offset=offset):
                with patch.object(
                    self.store._repository,
                    "connect",
                    side_effect=AssertionError("invalid offset reached SQLite"),
                ):
                    with self.assertRaisesRegex(ValueError, "offset"):
                        self.store.history(offset=offset)

    def test_history_pages_beyond_ten_thousand_rows(self) -> None:
        self._insert()
        columns = (
            "profile_version_id, suitability_assessment_id, policy_version, "
            "input_fingerprint, status, permitted_region_json, "
            "binding_constraints_json, safe_summary_json, encrypted_amount_results, "
            "encryption_algorithm, encryption_key_version, nonce, "
            "keyed_payload_fingerprint, assessed_at, valid_until, created_at"
        )
        with self.repository.connect() as connection, connection:
            connection.execute(
                f"WITH RECURSIVE copies(value) AS ("
                "SELECT 1 UNION ALL SELECT value + 1 FROM copies WHERE value < 10004"
                f") INSERT INTO allocation_assessments({columns}) "
                f"SELECT {columns} FROM allocation_assessments CROSS JOIN copies "
                "WHERE allocation_assessments.id = 1"
            )

        first_page = self.store.history(limit=3)
        distant_page = self.store.history(limit=10, offset=10_000)

        self.assertEqual(len(first_page), 3)
        self.assertEqual(len(distant_page), 5)
        self.assertGreater(first_page[-1].id, distant_page[0].id)

    def test_binding_constraints_require_enum_declaration_order_on_write_and_read(self) -> None:
        result = _allocation_result()
        noncanonical = (
            *result.binding_constraints,
            AllocationConstraintCode.MONTHLY_CEILING_CONSTRAINED,
        )
        altered = replace(result, binding_constraints=noncanonical)
        altered.validate()
        with self.assertRaisesRegex(ValueError, "canonical enum order"):
            self._insert(result=altered)

        self._insert(result=result)
        self._tamper(
            "binding_constraints_json",
            json.dumps([item.value for item in noncanonical], separators=(",", ":")),
        )
        with self.assertRaisesRegex(ValueError, "canonical enum order"):
            self.store.history()

    def test_read_rejects_noncanonical_or_private_plaintext(self) -> None:
        corruptions = (
            (
                "permitted_region_json",
                '{"available":true,"maximum_equity":"0.3","maximum_equity":"0.3"}',
                "duplicate",
            ),
            (
                "permitted_region_json",
                '{"available":true,"exact_amount":"99.00"}',
                "permitted region",
            ),
            ("safe_summary_json", '{"goal_count":NaN}', "valid JSON"),
            ("safe_summary_json", '{"private_name":"secret"}', "safe summary"),
            ("binding_constraints_json", '["horizon_binding","horizon_binding"]', "duplicates"),
        )
        for field, value, message in corruptions:
            with self.subTest(field=field, value=value):
                self._insert()
                self._tamper(field, value)
                with self.assertRaisesRegex(ValueError, message):
                    self.store.history()
                with self.repository.connect() as connection, connection:
                    connection.execute("DROP TRIGGER IF EXISTS allocation_assessment_no_delete")
                    connection.execute("DELETE FROM allocation_assessments")

    def test_read_rejects_tampered_crypto_and_timestamp_rows(self) -> None:
        corruptions = (
            ("nonce", "bad", "nonce"),
            ("encrypted_amount_results", base64.urlsafe_b64encode(b"x").decode(), "ciphertext"),
            ("keyed_payload_fingerprint", "A" * 64, "fingerprint"),
            ("assessed_at", "2026-07-12 12:00:00+00:00", "ISO datetime"),
            ("valid_until", (NOW - timedelta(seconds=1)).isoformat(), "after assessed_at"),
        )
        for field, value, message in corruptions:
            with self.subTest(field=field):
                self._insert()
                self._tamper(field, value)
                with self.assertRaisesRegex(ValueError, message):
                    self.store.latest_for(self.profile.id, self.suitability.id, "1")
                with self.repository.connect() as connection, connection:
                    connection.execute("DROP TRIGGER IF EXISTS allocation_assessment_no_delete")
                    connection.execute("DELETE FROM allocation_assessments")

    def test_database_immutability_triggers_remain_enforced(self) -> None:
        self._insert()
        with self.repository.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE allocation_assessments SET input_fingerprint = ? WHERE id = 1",
                    ("e" * 64,),
                )


if __name__ == "__main__":
    unittest.main()
