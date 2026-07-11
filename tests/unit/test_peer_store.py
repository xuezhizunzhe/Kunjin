from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
)
from kunjin.funds.peers.store import PeerStore, canonical_fingerprint
from kunjin.storage.repository import Repository


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


class PeerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temporary_directory.name) / "kunjin.db")
        self.repository.migrate()
        self.store = PeerStore(self.repository)
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_source_documents(
                    id, fund_code, document_kind, title, url, source_name,
                    source_tier, publisher, published_at, retrieved_at, checksum
                ) VALUES (1, '519755', 'basic_profile', '基金资料',
                          'https://fundf10.eastmoney.com/jbgk_519755.html',
                          'eastmoney_f10', 2, '东方财富', NULL, ?, ?)
                """,
                (NOW.isoformat(), "a" * 64),
            )
            connection.execute(
                """
                INSERT INTO fund_source_documents(
                    id, fund_code, document_kind, title, url, source_name,
                    source_tier, publisher, published_at, retrieved_at, checksum
                ) VALUES (2, '000001', 'basic_profile', '基金资料',
                          'https://fundf10.eastmoney.com/jbgk_000001.html',
                          'eastmoney_f10', 2, '东方财富', NULL, ?, ?)
                """,
                (NOW.isoformat(), "b" * 64),
            )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def member(
        self,
        fund_code: str,
        kind: MembershipKind,
        source_document_id: int,
    ) -> PeerGroupMember:
        return PeerGroupMember(
            fund_code=fund_code,
            membership_kind=kind,
            classification_key="mixed_flexible|active_or_unspecified|equity_bond",
            acceptance_reason="classification_match",
            warning=None,
            profile_source_document_id=source_document_id,
        )

    def group(
        self,
        fingerprint_payload: object,
        created_at: datetime = NOW,
        members=None,
        warnings=(),
    ) -> PeerGroup:
        return PeerGroup(
            id=None,
            anchor_fund_code="519755",
            rule_version="1",
            rule_key="mixed_flexible|active_or_unspecified|equity_bond",
            rule_description="相同基金类型、管理方式和业绩基准族。",
            candidate_source_url="https://fund.eastmoney.com/js/fundcode_search.js",
            candidate_source_tier=2,
            candidate_source_checksum="c" * 64,
            input_fingerprint=canonical_fingerprint(fingerprint_payload),
            created_at=created_at,
            status=PeerGroupStatus.SUCCESS,
            members=members
            or (
                self.member("519755", MembershipKind.ANCHOR, 1),
                self.member("000001", MembershipKind.DISCOVERED, 2),
            ),
            warnings=warnings,
        )

    def test_publish_inserts_members_and_moves_pointer_atomically(self) -> None:
        group = self.group({"codes": ["519755", "000001"], "rule": "1"})

        group_id = self.store.publish_group(group)
        loaded = self.store.load_current_group("519755")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, group_id)
        self.assertEqual([member.fund_code for member in loaded.members], ["519755", "000001"])
        self.assertEqual(self.store.list_anchor_codes(), ("519755",))
        with self.repository.connect() as connection:
            sync = connection.execute(
                "SELECT * FROM fund_peer_group_syncs WHERE anchor_fund_code = '519755'"
            ).fetchone()
        self.assertEqual(int(sync["current_peer_group_id"]), group_id)
        self.assertEqual(sync["state"], "success")
        self.assertEqual(sync["last_success_at"], NOW.isoformat())

    def test_second_version_becomes_current_without_deleting_history(self) -> None:
        first_id = self.store.publish_group(self.group({"version": 1}))
        second = self.group(
            {"version": 2},
            created_at=NOW + timedelta(days=1),
            warnings=("candidate_limit_reached",),
        )
        second_id = self.store.publish_group(second)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.store.load_current_group("519755").id, second_id)
        self.assertEqual(
            self.store.load_current_group("519755").warnings,
            ("candidate_limit_reached",),
        )
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM fund_peer_groups").fetchone()[0]
        self.assertEqual(count, 2)

    def test_duplicate_canonical_fingerprint_is_idempotent(self) -> None:
        first = self.group({"b": 2, "a": [1, 3]})
        second = self.group({"a": [1, 3], "b": 2}, created_at=NOW + timedelta(hours=1))

        first_id = self.store.publish_group(first)
        second_id = self.store.publish_group(second)

        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first_id, second_id)
        with self.repository.connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM fund_peer_groups").fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_member_reference_rolls_back_whole_publication(self) -> None:
        current_id = self.store.publish_group(self.group({"version": 1}))
        invalid_group = self.group(
            {"version": 2},
            created_at=NOW + timedelta(days=1),
            members=(
                self.member("519755", MembershipKind.ANCHOR, 1),
                self.member("000002", MembershipKind.DISCOVERED, 999),
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.publish_group(invalid_group)

        self.assertEqual(self.store.load_current_group("519755").id, current_id)
        with self.repository.connect() as connection:
            group_count = connection.execute("SELECT COUNT(*) FROM fund_peer_groups").fetchone()[0]
            member_count = connection.execute(
                "SELECT COUNT(*) FROM fund_peer_group_members"
            ).fetchone()[0]
        self.assertEqual(group_count, 1)
        self.assertEqual(member_count, 2)

    def test_mark_failure_preserves_current_pointer_and_last_success(self) -> None:
        group_id = self.store.publish_group(self.group({"version": 1}))
        attempted_at = NOW + timedelta(days=1)

        self.store.mark_failure(
            "519755",
            "candidate_discovery_unavailable",
            "候选目录暂时不可用",
            attempted_at,
        )

        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fund_peer_group_syncs WHERE anchor_fund_code = '519755'"
            ).fetchone()
        self.assertEqual(int(row["current_peer_group_id"]), group_id)
        self.assertEqual(row["state"], "source_unavailable")
        self.assertEqual(row["last_attempted_at"], attempted_at.isoformat())
        self.assertEqual(row["last_success_at"], NOW.isoformat())
        self.assertEqual(row["error_code"], "candidate_discovery_unavailable")

    def test_first_total_failure_has_no_current_pointer(self) -> None:
        self.store.mark_failure(
            "519755",
            "candidate_discovery_unavailable",
            "候选目录暂时不可用",
            NOW,
        )

        self.assertIsNone(self.store.load_current_group("519755"))
        self.assertEqual(self.store.list_anchor_codes(), ())
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fund_peer_group_syncs WHERE anchor_fund_code = '519755'"
            ).fetchone()
        self.assertIsNone(row["current_peer_group_id"])
        self.assertIsNone(row["last_success_at"])
        self.assertEqual(row["state"], "source_unavailable")

    def test_comparison_json_is_canonical_and_rejects_non_finite_values(self) -> None:
        fingerprint = canonical_fingerprint({"codes": ["519755", "000001"]})
        run_id = self.store.save_comparison(
            "explicit",
            "519755",
            None,
            NOW,
            "success",
            fingerprint,
            {"z": 1, "a": {"中文": True}},
            None,
        )

        with self.repository.connect() as connection:
            result_json = connection.execute(
                "SELECT result_json FROM fund_comparison_runs WHERE id = ?", (run_id,)
            ).fetchone()["result_json"]
        self.assertEqual(result_json, '{"a":{"中文":true},"z":1}')
        self.assertEqual(self.store.load_comparison(run_id)["result"], {"a": {"中文": True}, "z": 1})

        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save_comparison(
                    "explicit",
                    "519755",
                    None,
                    NOW,
                    "success",
                    canonical_fingerprint({"value": repr(value)}),
                    {"value": value},
                    None,
                )

    def test_load_comparison_rejects_non_finite_json_constants(self) -> None:
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO fund_comparison_runs(
                    comparison_kind, anchor_fund_code, peer_group_id,
                    calculation_version, as_of, status, input_fingerprint,
                    result_json, warning
                ) VALUES ('explicit', '519755', NULL, '1', ?, 'success', ?, ?, NULL)
                """,
                (NOW.isoformat(), "d" * 64, '{"value":NaN}'),
            )
            run_id = int(cursor.lastrowid)

        with self.assertRaises(ValueError):
            self.store.load_comparison(run_id)

    def test_repeated_comparison_fingerprint_returns_existing_run(self) -> None:
        fingerprint = canonical_fingerprint({"codes": ["519755", "000001"]})
        first_id = self.store.save_comparison(
            "explicit", "519755", None, NOW, "success", fingerprint, {"value": 1}, None
        )
        second_id = self.store.save_comparison(
            "explicit",
            "519755",
            None,
            NOW + timedelta(hours=1),
            "partial",
            fingerprint,
            {"value": 2},
            "new warning",
        )

        self.assertEqual(first_id, second_id)
        loaded = self.store.load_comparison(first_id)
        self.assertEqual(loaded["status"], "success")
        self.assertEqual(loaded["result"], {"value": 1})


if __name__ == "__main__":
    unittest.main()
