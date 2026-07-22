import sqlite3
import tempfile
import unittest
from pathlib import Path

from kunjin.storage.repository import Repository
from kunjin.storage.schema import (
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
)

PROFILE_VALUES = (
    1,
    "confirmed",
    "AES-256-GCM",
    "1",
    "nonce",
    "ciphertext",
    "fingerprint",
    "2026-07-12T12:00:00+00:00",
    "2027-07-12T12:00:00+00:00",
    "2026-07-12T12:00:00+00:00",
)


class SchemaV7Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def repository(self, name: str = "kunjin.db") -> Repository:
        return Repository(Path(self.temporary_directory.name) / name)

    def insert_profile(
        self,
        repository: Repository,
        values=PROFILE_VALUES,
        invalidated_at=None,
        invalidation_reason=None,
    ) -> None:
        with repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO financial_profile_versions(
                    version, status, encryption_algorithm, encryption_key_version,
                    nonce, encrypted_payload, keyed_payload_fingerprint,
                    confirmed_at, valid_until, created_at,
                    invalidated_at, invalidation_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, invalidated_at, invalidation_reason),
            )

    def test_fresh_migration_adds_profile_table_and_all_version_records(self) -> None:
        repository = self.repository()
        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()

        self.assertIn("financial_profile_versions", repository.table_names())
        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 26)))

    def test_migration_preserves_populated_version_six_data(self) -> None:
        repository = self.repository("version-six.db")
        with repository.connect() as connection, connection:
            for schema in (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5, SCHEMA_V6):
                connection.executescript(schema)
            connection.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                [(version, f"2026-07-{version:02d}T00:00:00+00:00") for version in range(1, 7)],
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
            connection.execute(
                """
                INSERT INTO fund_nav(
                    fund_code, nav_date, unit_nav, accumulated_nav,
                    daily_growth, source, retrieved_at
                ) VALUES ('519755', '2026-07-10', '1.6680', '1.6680',
                          '0.10', 'eastmoney', '2026-07-11T00:00:00+00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO fund_source_documents(
                    id, fund_code, document_kind, title, url, source_name,
                    source_tier, publisher, retrieved_at, checksum
                ) VALUES (1, '519755', 'basic_profile', 'profile',
                          'https://example.test/profile', 'formal_source', 1,
                          'publisher', '2026-07-11T00:00:00+00:00', ?)
                """,
                ("a" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_identities(
                    id, fund_code, record_key, fund_name, status, fund_type,
                    manager_name, source_document_id
                ) VALUES (1, '519755', ?, 'preserved identity', 'active',
                          'mixed', 'manager', 1)
                """,
                ("b" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_holdings(
                    id, fund_code, record_key, report_period, published_at,
                    rank, security_code, security_name, asset_type, weight,
                    disclosure_scope, source_document_id
                ) VALUES (1, '519755', ?, '2026-Q1',
                          '2026-04-21T00:00:00+08:00', 1, '600000',
                          'preserved holding', 'stock', '5.25', 'top10', 1)
                """,
                ("c" * 64,),
            )
            connection.execute(
                """
                INSERT INTO fund_peer_groups(
                    id, anchor_fund_code, rule_version, rule_key,
                    rule_description, candidate_source_url,
                    candidate_source_tier, candidate_source_checksum,
                    input_fingerprint, created_at, status
                ) VALUES (1, '519755', '1', 'same-type', 'preserved peers',
                          'https://example.test/peers', 1, ?, ?,
                          '2026-07-11T00:00:00+00:00', 'success')
                """,
                ("d" * 64, "e" * 64),
            )

        repository.migrate()

        with repository.connect() as connection:
            versions = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            transaction = connection.execute(
                "SELECT fund_code, amount FROM transactions WHERE id = 1"
            ).fetchone()
            nav = connection.execute(
                "SELECT nav_date, unit_nav FROM fund_nav WHERE fund_code = '519755'"
            ).fetchone()
            identity = connection.execute(
                "SELECT fund_name, manager_name FROM fund_identities WHERE id = 1"
            ).fetchone()
            holding = connection.execute(
                "SELECT security_code, weight FROM fund_holdings WHERE id = 1"
            ).fetchone()
            peer_group = connection.execute(
                "SELECT rule_key, status FROM fund_peer_groups WHERE id = 1"
            ).fetchone()

        self.assertEqual([int(row["version"]) for row in versions], list(range(1, 26)))
        self.assertEqual(dict(transaction), {"fund_code": "519755", "amount": "20.00"})
        self.assertEqual(dict(nav), {"nav_date": "2026-07-10", "unit_nav": "1.6680"})
        self.assertEqual(
            dict(identity),
            {"fund_name": "preserved identity", "manager_name": "manager"},
        )
        self.assertEqual(dict(holding), {"security_code": "600000", "weight": "5.25"})
        self.assertEqual(dict(peer_group), {"rule_key": "same-type", "status": "success"})

    def test_profile_payload_and_confirmation_metadata_are_immutable(self) -> None:
        repository = self.repository()
        repository.migrate()
        self.insert_profile(repository)

        immutable_updates = {
            "version": 2,
            "encryption_algorithm": "not-aes",
            "encryption_key_version": "2",
            "nonce": "changed",
            "encrypted_payload": "changed",
            "keyed_payload_fingerprint": "changed",
            "confirmed_at": "2026-07-13T12:00:00+00:00",
            "valid_until": "2028-07-12T12:00:00+00:00",
            "created_at": "2026-07-13T12:00:00+00:00",
        }
        for column, value in immutable_updates.items():
            with self.subTest(column=column):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "profile payload is immutable"):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            f"UPDATE financial_profile_versions SET {column} = ? WHERE id = 1",
                            (value,),
                        )

    def test_profile_versions_cannot_be_deleted(self) -> None:
        repository = self.repository()
        repository.migrate()
        self.insert_profile(repository)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "profile versions are immutable"):
            with repository.connect() as connection, connection:
                connection.execute("DELETE FROM financial_profile_versions WHERE id = 1")

    def test_allowed_lifecycle_transitions_succeed(self) -> None:
        cases = (
            ("draft", "confirmed", None, None),
            (
                "draft",
                "invalidated",
                "2026-07-13T12:00:00+00:00",
                "financial circumstances changed",
            ),
            ("confirmed", "superseded", None, None),
            (
                "confirmed",
                "invalidated",
                "2026-07-13T12:00:00+00:00",
                "financial circumstances changed",
            ),
        )
        for index, (initial, target, invalidated_at, reason) in enumerate(cases, start=1):
            with self.subTest(initial=initial, target=target):
                repository = self.repository(f"allowed-{index}.db")
                repository.migrate()
                self.insert_profile(
                    repository,
                    (1, initial, *PROFILE_VALUES[2:]),
                )
                with repository.connect() as connection, connection:
                    connection.execute(
                        """
                        UPDATE financial_profile_versions
                        SET status = ?, invalidated_at = ?, invalidation_reason = ?
                        WHERE id = 1
                        """,
                        (target, invalidated_at, reason),
                    )
                with repository.connect() as connection:
                    row = connection.execute(
                        """
                        SELECT status, invalidated_at, invalidation_reason
                        FROM financial_profile_versions WHERE id = 1
                        """
                    ).fetchone()
                self.assertEqual(str(row["status"]), target)
                self.assertEqual(row["invalidated_at"], invalidated_at)
                self.assertEqual(row["invalidation_reason"], reason)

    def test_disallowed_lifecycle_transitions_are_rejected(self) -> None:
        cases = (
            ("draft", "superseded"),
            ("confirmed", "draft"),
            ("superseded", "draft"),
            ("superseded", "confirmed"),
            ("superseded", "invalidated"),
            ("invalidated", "draft"),
            ("invalidated", "confirmed"),
            ("invalidated", "superseded"),
        )
        for index, (initial, target) in enumerate(cases, start=1):
            with self.subTest(initial=initial, target=target):
                repository = self.repository(f"disallowed-{index}.db")
                repository.migrate()
                invalidated_at = None
                reason = None
                if initial == "invalidated":
                    invalidated_at = "2026-07-13T12:00:00+00:00"
                    reason = "financial circumstances changed"
                self.insert_profile(
                    repository,
                    (1, initial, *PROFILE_VALUES[2:]),
                    invalidated_at,
                    reason,
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid profile lifecycle transition",
                ):
                    with repository.connect() as connection, connection:
                        target_invalidated_at = None
                        target_reason = None
                        if target == "invalidated":
                            target_invalidated_at = "2026-07-14T12:00:00+00:00"
                            target_reason = "new invalidation attempt"
                        connection.execute(
                            """
                            UPDATE financial_profile_versions
                            SET status = ?, invalidated_at = ?, invalidation_reason = ?
                            WHERE id = 1
                            """,
                            (target, target_invalidated_at, target_reason),
                        )

    def test_terminal_profile_cannot_be_modified(self) -> None:
        for index, status in enumerate(("superseded", "invalidated"), start=1):
            with self.subTest(status=status):
                repository = self.repository(f"terminal-{index}.db")
                repository.migrate()
                invalidated_at = None
                reason = None
                if status == "invalidated":
                    invalidated_at = "2026-07-13T12:00:00+00:00"
                    reason = "financial circumstances changed"
                self.insert_profile(
                    repository,
                    (1, status, *PROFILE_VALUES[2:]),
                    invalidated_at,
                    reason,
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid profile lifecycle transition",
                ):
                    with repository.connect() as connection, connection:
                        connection.execute(
                            """
                            UPDATE financial_profile_versions
                            SET status = status
                            WHERE id = 1
                            """
                        )

    def test_invalidated_status_requires_complete_invalidation_metadata(self) -> None:
        invalid_metadata = (
            (None, None),
            ("2026-07-13T12:00:00+00:00", None),
            (None, "financial circumstances changed"),
            ("2026-07-13T12:00:00+00:00", "   "),
        )
        for index, (invalidated_at, reason) in enumerate(invalid_metadata, start=1):
            with self.subTest(invalidated_at=invalidated_at, reason=reason):
                repository = self.repository(f"invalidated-metadata-{index}.db")
                repository.migrate()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid profile invalidation metadata",
                ):
                    self.insert_profile(
                        repository,
                        (1, "invalidated", *PROFILE_VALUES[2:]),
                        invalidated_at,
                        reason,
                    )

    def test_non_invalidated_status_rejects_invalidation_metadata(self) -> None:
        for index, status in enumerate(("draft", "confirmed", "superseded"), start=1):
            with self.subTest(status=status):
                repository = self.repository(f"non-invalidated-metadata-{index}.db")
                repository.migrate()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "invalid profile invalidation metadata",
                ):
                    self.insert_profile(
                        repository,
                        (1, status, *PROFILE_VALUES[2:]),
                        "2026-07-13T12:00:00+00:00",
                        "must not be present",
                    )

        repository = self.repository("non-invalidated-metadata-update.db")
        repository.migrate()
        self.insert_profile(repository)
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "invalid profile invalidation metadata",
        ):
            with repository.connect() as connection, connection:
                connection.execute(
                    """
                    UPDATE financial_profile_versions
                    SET invalidated_at = ?, invalidation_reason = ?
                    WHERE id = 1
                    """,
                    ("2026-07-13T12:00:00+00:00", "must not be present"),
                )

    def test_update_to_invalidated_requires_complete_metadata(self) -> None:
        repository = self.repository()
        repository.migrate()
        self.insert_profile(repository)

        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "invalid profile invalidation metadata",
        ):
            with repository.connect() as connection, connection:
                connection.execute(
                    "UPDATE financial_profile_versions SET status = 'invalidated' WHERE id = 1"
                )

    def test_only_one_confirmed_profile_is_allowed_and_draft_status_is_valid(self) -> None:
        repository = self.repository()
        repository.migrate()
        self.insert_profile(repository)
        self.insert_profile(
            repository,
            (2, "draft", *PROFILE_VALUES[2:]),
        )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "UNIQUE constraint failed"):
            self.insert_profile(
                repository,
                (3, "confirmed", *PROFILE_VALUES[2:]),
            )


if __name__ == "__main__":
    unittest.main()
