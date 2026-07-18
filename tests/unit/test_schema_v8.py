import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kunjin.storage.repository import Repository
from kunjin.storage.schema import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
)

PROFILE_VALUES = (
    1,
    "confirmed",
    "AES-256-GCM",
    "1",
    "profile-nonce",
    "profile-ciphertext",
    "profile-fingerprint",
    "2026-07-12T12:00:00+00:00",
    "2026-10-10T12:00:00+00:00",
    "2026-07-12T12:00:00+00:00",
)

POLICY_VALUES = (
    "1",
    '{"version":"1"}',
    "a" * 64,
    "2026-07-12T00:00:00+00:00",
    "2026-07-12T12:00:00+00:00",
)

ASSESSMENT_VALUES = (
    1,
    "1",
    "b" * 64,
    "blocked",
    '["emergency_reserve_shortfall"]',
    "[]",
    '{"debt_count":0}',
    "assessment-ciphertext",
    "AES-256-GCM",
    "1",
    "assessment-nonce",
    "c" * 64,
    "2026-07-12T12:00:00+00:00",
    "2026-07-13T12:00:00+00:00",
    "2026-07-12T12:00:00+00:00",
)


class SchemaV8Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def create_version_seven_database(self, name: str) -> Repository:
        repository = self.repository(name)
        with repository.connect() as connection, connection:
            for schema in (
                SCHEMA_V1,
                SCHEMA_V2,
                SCHEMA_V3,
                SCHEMA_V4,
                SCHEMA_V5,
                SCHEMA_V6,
                SCHEMA_V7,
            ):
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, f"2026-07-{version:02d}T00:00:00+00:00") for version in range(1, 8)],
            )
            connection.execute(
                """
                INSERT INTO financial_profile_versions(
                    id, version, status, encryption_algorithm,
                    encryption_key_version, nonce, encrypted_payload,
                    keyed_payload_fingerprint, confirmed_at, valid_until, created_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                PROFILE_VALUES,
            )
            connection.execute(
                """
                INSERT INTO transactions(
                    id, transaction_type, fund_code, amount, evidence_level,
                    field_evidence_json, created_at
                ) VALUES (1, 'subscription', '519755', '20.00',
                          'user_confirmed', '{}', '2026-07-11T00:00:00+00:00')
                """
            )
        return repository

    def insert_profile(self, repository: Repository) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO financial_profile_versions(
                    version, status, encryption_algorithm, encryption_key_version,
                    nonce, encrypted_payload, keyed_payload_fingerprint,
                    confirmed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                PROFILE_VALUES,
            )

    def insert_policy(self, repository: Repository) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_policy_versions(
                    version, canonical_policy_json, policy_checksum,
                    effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                POLICY_VALUES,
            )

    def insert_assessment(self, repository: Repository) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_assessments(
                    profile_version_id, policy_version, input_fingerprint,
                    status, hard_blocks_json, constraints_json, safe_summary_json,
                    encrypted_amount_results, encryption_algorithm,
                    encryption_key_version, nonce, keyed_payload_fingerprint,
                    assessed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ASSESSMENT_VALUES,
            )

    def prepared_repository(self, name: str = "prepared.db") -> Repository:
        repository = self.repository(name)
        repository.migrate()
        self.insert_profile(repository)
        self.insert_policy(repository)
        return repository

    def test_fresh_migration_adds_exact_tables_and_versions(self) -> None:
        repository = self.repository()
        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            policy_columns = connection.execute(
                "PRAGMA table_info(suitability_policy_versions)"
            ).fetchall()
            assessment_columns = connection.execute(
                "PRAGMA table_info(suitability_assessments)"
            ).fetchall()

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 21)))
        self.assertEqual(
            [str(row["name"]) for row in policy_columns],
            [
                "version",
                "canonical_policy_json",
                "policy_checksum",
                "effective_at",
                "created_at",
            ],
        )
        self.assertEqual(
            [str(row["name"]) for row in assessment_columns],
            [
                "id",
                "profile_version_id",
                "policy_version",
                "input_fingerprint",
                "status",
                "hard_blocks_json",
                "constraints_json",
                "safe_summary_json",
                "encrypted_amount_results",
                "encryption_algorithm",
                "encryption_key_version",
                "nonce",
                "keyed_payload_fingerprint",
                "assessed_at",
                "valid_until",
                "created_at",
            ],
        )

    def test_migration_preserves_populated_version_seven_data_and_triggers(self) -> None:
        repository = self.create_version_seven_database("version-seven.db")

        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            profile = connection.execute(
                "SELECT version, encrypted_payload FROM financial_profile_versions WHERE id = 1"
            ).fetchone()
            transaction = connection.execute(
                "SELECT fund_code, amount FROM transactions WHERE id = 1"
            ).fetchone()
            trigger_names = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 21)))
        self.assertEqual(
            dict(profile),
            {"version": 1, "encrypted_payload": "profile-ciphertext"},
        )
        self.assertEqual(dict(transaction), {"fund_code": "519755", "amount": "20.00"})
        self.assertIn("financial_profile_no_delete", trigger_names)
        self.assertIn("transactions_no_update", trigger_names)
        self.assertIn("suitability_policy_no_update", trigger_names)
        self.assertIn("suitability_assessment_no_delete", trigger_names)

    def test_failed_v8_migration_rolls_back_all_schema_changes_and_marker(self) -> None:
        repository = self.create_version_seven_database("failed-version-eight.db")
        broken_v8 = """
        CREATE TABLE partial_suitability_v8(id INTEGER PRIMARY KEY);
        CREATE TRIGGER partial_suitability_v8_trigger
        AFTER INSERT ON partial_suitability_v8
        BEGIN
            SELECT 1;
        END;
        CREATE TABLE syntax_error (
        """

        with patch("kunjin.storage.repository.SCHEMA_V8", broken_v8):
            with self.assertRaises(sqlite3.OperationalError):
                repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            partial_objects = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name IN ('partial_suitability_v8', "
                "'partial_suitability_v8_trigger')"
            ).fetchall()
            transaction = connection.execute(
                "SELECT fund_code, amount FROM transactions WHERE id = 1"
            ).fetchone()

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 8)))
        self.assertEqual(partial_objects, [])
        self.assertEqual(dict(transaction), {"fund_code": "519755", "amount": "20.00"})

        repository.migrate()
        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 21)))
        self.assertIn("suitability_assessments", repository.table_names())

    def test_assessment_foreign_keys_restrict_profile_and_policy_deletion(self) -> None:
        repository = self.repository()
        repository.migrate()

        with repository.connect() as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(suitability_assessments)"
            ).fetchall()

        by_table = {str(row["table"]): row for row in foreign_keys}
        self.assertEqual(str(by_table["financial_profile_versions"]["from"]), "profile_version_id")
        self.assertEqual(str(by_table["financial_profile_versions"]["to"]), "id")
        self.assertEqual(str(by_table["financial_profile_versions"]["on_delete"]), "RESTRICT")
        self.assertEqual(str(by_table["suitability_policy_versions"]["from"]), "policy_version")
        self.assertEqual(str(by_table["suitability_policy_versions"]["to"]), "version")
        self.assertEqual(str(by_table["suitability_policy_versions"]["on_delete"]), "RESTRICT")

        invalid_assessment = list(ASSESSMENT_VALUES)
        invalid_assessment[0] = 999
        with self.assertRaises(sqlite3.IntegrityError):
            with repository.connect() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO suitability_assessments(
                        profile_version_id, policy_version, input_fingerprint,
                        status, hard_blocks_json, constraints_json, safe_summary_json,
                        encrypted_amount_results, encryption_algorithm,
                        encryption_key_version, nonce, keyed_payload_fingerprint,
                        assessed_at, valid_until, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    invalid_assessment,
                )

        self.insert_profile(repository)
        invalid_assessment[0] = 1
        with self.assertRaises(sqlite3.IntegrityError):
            with repository.connect() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO suitability_assessments(
                        profile_version_id, policy_version, input_fingerprint,
                        status, hard_blocks_json, constraints_json, safe_summary_json,
                        encrypted_amount_results, encryption_algorithm,
                        encryption_key_version, nonce, keyed_payload_fingerprint,
                        assessed_at, valid_until, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    invalid_assessment,
                )

    def test_policy_version_is_unique_and_rows_are_immutable(self) -> None:
        repository = self.prepared_repository()

        with self.assertRaises(sqlite3.IntegrityError):
            with repository.connect() as connection, connection:
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
                        "b" * 64,
                        POLICY_VALUES[3],
                        POLICY_VALUES[4],
                    ),
                )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "suitability policies are immutable",
        ):
            with repository.connect() as connection, connection:
                connection.execute(
                    "UPDATE suitability_policy_versions "
                    "SET canonical_policy_json = '{}' WHERE version = '1'"
                )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "suitability policies are immutable",
        ):
            with repository.connect() as connection, connection:
                connection.execute("DELETE FROM suitability_policy_versions WHERE version = '1'")

    def test_assessment_rows_are_immutable(self) -> None:
        repository = self.prepared_repository()
        self.insert_assessment(repository)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "suitability assessments are immutable",
        ):
            with repository.connect() as connection, connection:
                connection.execute(
                    "UPDATE suitability_assessments SET status = 'constrained' WHERE id = 1"
                )
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "suitability assessments are immutable",
        ):
            with repository.connect() as connection, connection:
                connection.execute("DELETE FROM suitability_assessments WHERE id = 1")

    def test_assessment_status_and_encryption_algorithm_are_checked(self) -> None:
        for index, replacement in ((3, "ready"), (8, "AES-128-GCM")):
            with self.subTest(index=index, replacement=replacement):
                repository = self.prepared_repository(f"checked-{index}.db")
                values = list(ASSESSMENT_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_assessments(
                                profile_version_id, policy_version, input_fingerprint,
                                status, hard_blocks_json, constraints_json,
                                safe_summary_json, encrypted_amount_results,
                                encryption_algorithm, encryption_key_version, nonce,
                                keyed_payload_fingerprint, assessed_at, valid_until,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_all_assessment_status_values_are_accepted(self) -> None:
        for status in ("blocked", "constrained", "ready_for_allocation"):
            with self.subTest(status=status):
                repository = self.prepared_repository(f"status-{status}.db")
                values = list(ASSESSMENT_VALUES)
                values[3] = status
                with repository.connect() as connection, connection:
                    connection.execute(
                        """
                        INSERT INTO suitability_assessments(
                            profile_version_id, policy_version, input_fingerprint,
                            status, hard_blocks_json, constraints_json,
                            safe_summary_json, encrypted_amount_results,
                            encryption_algorithm, encryption_key_version, nonce,
                            keyed_payload_fingerprint, assessed_at, valid_until,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )

    def test_all_policy_and_assessment_payload_fields_are_required(self) -> None:
        repository = self.repository()
        repository.migrate()

        for index in range(len(POLICY_VALUES)):
            with self.subTest(table="policy", index=index):
                values = list(POLICY_VALUES)
                values[index] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

        self.insert_profile(repository)
        self.insert_policy(repository)
        for index in range(len(ASSESSMENT_VALUES)):
            with self.subTest(table="assessment", index=index):
                values = list(ASSESSMENT_VALUES)
                values[index] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_assessments(
                                profile_version_id, policy_version, input_fingerprint,
                                status, hard_blocks_json, constraints_json,
                                safe_summary_json, encrypted_amount_results,
                                encryption_algorithm, encryption_key_version, nonce,
                                keyed_payload_fingerprint, assessed_at, valid_until,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_policy_json_hex_and_nonempty_fields_are_checked(self) -> None:
        cases = (
            (0, ""),
            (1, "[]"),
            (1, "not-json"),
            (2, "A" * 64),
            (2, "g" * 64),
            (2, "a" * 63),
            (3, ""),
            (4, ""),
        )
        for case_number, (index, replacement) in enumerate(cases):
            with self.subTest(index=index, replacement=replacement):
                repository = self.repository(f"policy-integrity-{case_number}.db")
                repository.migrate()
                values = list(POLICY_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_assessment_json_hex_and_nonempty_fields_are_checked(self) -> None:
        cases = (
            (2, "A" * 64),
            (2, "g" * 64),
            (2, "b" * 63),
            (4, "{}"),
            (4, "not-json"),
            (5, "null"),
            (5, "not-json"),
            (6, "[]"),
            (6, "not-json"),
            (7, ""),
            (9, ""),
            (10, ""),
            (11, "A" * 64),
            (11, "g" * 64),
            (11, "c" * 63),
            (12, ""),
            (13, ""),
            (14, ""),
        )
        for case_number, (index, replacement) in enumerate(cases):
            with self.subTest(index=index, replacement=replacement):
                repository = self.prepared_repository(f"assessment-integrity-{case_number}.db")
                values = list(ASSESSMENT_VALUES)
                values[index] = replacement
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_assessments(
                                profile_version_id, policy_version, input_fingerprint,
                                status, hard_blocks_json, constraints_json,
                                safe_summary_json, encrypted_amount_results,
                                encryption_algorithm, encryption_key_version, nonce,
                                keyed_payload_fingerprint, assessed_at, valid_until,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_assessment_valid_until_must_be_later_than_assessed_at(self) -> None:
        for case_number, valid_until in enumerate(
            (
                ASSESSMENT_VALUES[12],
                "2026-07-12T11:59:59+00:00",
            )
        ):
            with self.subTest(valid_until=valid_until):
                repository = self.prepared_repository(f"validity-{case_number}.db")
                values = list(ASSESSMENT_VALUES)
                values[13] = valid_until
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_assessments(
                                profile_version_id, policy_version, input_fingerprint,
                                status, hard_blocks_json, constraints_json,
                                safe_summary_json, encrypted_amount_results,
                                encryption_algorithm, encryption_key_version, nonce,
                                keyed_payload_fingerprint, assessed_at, valid_until,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )

    def test_assessment_validity_comparison_uses_absolute_instants(self) -> None:
        repository = self.prepared_repository("cross-timezone-validity.db")

        later_instant_with_smaller_text = list(ASSESSMENT_VALUES)
        later_instant_with_smaller_text[12] = "2026-07-12T12:00:00+08:00"
        later_instant_with_smaller_text[13] = "2026-07-12T05:00:00+00:00"
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO suitability_assessments(
                    profile_version_id, policy_version, input_fingerprint,
                    status, hard_blocks_json, constraints_json, safe_summary_json,
                    encrypted_amount_results, encryption_algorithm,
                    encryption_key_version, nonce, keyed_payload_fingerprint,
                    assessed_at, valid_until, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                later_instant_with_smaller_text,
            )

        earlier_instant_with_larger_text = list(ASSESSMENT_VALUES)
        earlier_instant_with_larger_text[12] = "2026-07-12T12:00:00+00:00"
        earlier_instant_with_larger_text[13] = "2026-07-12T20:30:00+09:00"
        with self.assertRaises(sqlite3.IntegrityError):
            with repository.connect() as connection, connection:
                connection.execute(
                    """
                    INSERT INTO suitability_assessments(
                        profile_version_id, policy_version, input_fingerprint,
                        status, hard_blocks_json, constraints_json, safe_summary_json,
                        encrypted_amount_results, encryption_algorithm,
                        encryption_key_version, nonce, keyed_payload_fingerprint,
                        assessed_at, valid_until, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    earlier_instant_with_larger_text,
                )

    def test_unparseable_policy_and_assessment_timestamps_are_rejected(self) -> None:
        for case_number, index in enumerate((3, 4)):
            with self.subTest(table="policy", index=index):
                repository = self.repository(f"invalid-policy-time-{case_number}.db")
                repository.migrate()
                values = list(POLICY_VALUES)
                values[index] = "not-a-timestamp"
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_policy_versions(
                                version, canonical_policy_json, policy_checksum,
                                effective_at, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            values,
                        )

        for case_number, index in enumerate((12, 13, 14)):
            with self.subTest(table="assessment", index=index):
                repository = self.prepared_repository(f"invalid-assessment-time-{case_number}.db")
                values = list(ASSESSMENT_VALUES)
                values[index] = "not-a-timestamp"
                with self.assertRaises(sqlite3.IntegrityError):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            INSERT INTO suitability_assessments(
                                profile_version_id, policy_version, input_fingerprint,
                                status, hard_blocks_json, constraints_json,
                                safe_summary_json, encrypted_amount_results,
                                encryption_algorithm, encryption_key_version, nonce,
                                keyed_payload_fingerprint, assessed_at, valid_until,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            values,
                        )


if __name__ == "__main__":
    unittest.main()
