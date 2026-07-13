from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from kunjin.funds.peers.analytics import PEER_CALCULATION_VERSION
from kunjin.funds.peers.models import (
    CHECKSUM_PATTERN,
    FUND_CODE_PATTERN,
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
)
from kunjin.storage.repository import Repository

_COMPARISON_KINDS = {"peer", "explicit", "portfolio_overlap"}
_COMPARISON_STATUSES = {"success", "partial", "insufficient_data"}


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse_strict_json(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_non_finite)


def _validate_fund_code(fund_code: str) -> None:
    if not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError(f"invalid fund code: {fund_code}")


def _validate_optional_fund_code(fund_code: Optional[str]) -> None:
    if fund_code is not None:
        _validate_fund_code(fund_code)


def _validate_required_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")


def _validate_optional_text(value: Optional[str], field_name: str) -> None:
    if value is not None:
        _validate_required_text(value, field_name)


def _validate_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _validate_fingerprint(value: str) -> None:
    if not CHECKSUM_PATTERN.fullmatch(value):
        raise ValueError("input fingerprint must be a lowercase SHA-256 digest")


def _warning_json(warnings: Tuple[str, ...]) -> Optional[str]:
    return None if not warnings else canonical_json(list(warnings))


def _load_warnings(value: Optional[str]) -> Tuple[str, ...]:
    if value is None:
        return ()
    decoded = _parse_strict_json(value)
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) and item.strip() for item in decoded
    ):
        raise ValueError("stored peer group warning must be a JSON string array")
    return tuple(decoded)


class PeerStore:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def publish_group(self, group: PeerGroup) -> int:
        group.validate()
        if group.id is not None:
            raise ValueError("peer group must not already be stored")

        attempted_at = group.created_at.isoformat()
        with self.repository.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO fund_peer_groups(
                    anchor_fund_code, rule_version, rule_key, rule_description,
                    candidate_source_url, candidate_source_tier,
                    candidate_source_checksum, input_fingerprint, created_at,
                    status, warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group.anchor_fund_code,
                    group.rule_version,
                    group.rule_key,
                    group.rule_description,
                    group.candidate_source_url,
                    group.candidate_source_tier,
                    group.candidate_source_checksum,
                    group.input_fingerprint,
                    attempted_at,
                    group.status.value,
                    _warning_json(group.warnings),
                ),
            )
            inserted = cursor.rowcount == 1
            row = connection.execute(
                """
                SELECT id, status, warning FROM fund_peer_groups
                WHERE anchor_fund_code = ? AND rule_version = ? AND input_fingerprint = ?
                """,
                (group.anchor_fund_code, group.rule_version, group.input_fingerprint),
            ).fetchone()
            if row is None:
                raise RuntimeError("peer group publication did not produce a stored row")
            group_id = int(row["id"])

            if inserted:
                for member in group.members:
                    connection.execute(
                        """
                        INSERT INTO fund_peer_group_members(
                            peer_group_id, fund_code, membership_kind,
                            classification_key, acceptance_reason, warning,
                            profile_source_document_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            group_id,
                            member.fund_code,
                            member.membership_kind.value,
                            member.classification_key,
                            member.acceptance_reason,
                            member.warning,
                            member.profile_source_document_id,
                        ),
                    )

            connection.execute(
                """
                INSERT INTO fund_peer_group_syncs(
                    anchor_fund_code, current_peer_group_id, state,
                    last_attempted_at, last_success_at, error_code, warning
                ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(anchor_fund_code) DO UPDATE SET
                    current_peer_group_id = excluded.current_peer_group_id,
                    state = excluded.state,
                    last_attempted_at = excluded.last_attempted_at,
                    last_success_at = excluded.last_success_at,
                    error_code = NULL,
                    warning = excluded.warning
                """,
                (
                    group.anchor_fund_code,
                    group_id,
                    str(row["status"]),
                    attempted_at,
                    attempted_at,
                    row["warning"],
                ),
            )
        return group_id

    def mark_failure(
        self,
        anchor_fund_code: str,
        error_code: str,
        warning: str,
        attempted_at: datetime,
    ) -> None:
        _validate_fund_code(anchor_fund_code)
        _validate_required_text(error_code, "error code")
        _validate_required_text(warning, "warning")
        _validate_aware(attempted_at, "attempted_at")
        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT INTO fund_peer_group_syncs(
                    anchor_fund_code, current_peer_group_id, state,
                    last_attempted_at, last_success_at, error_code, warning
                ) VALUES (?, NULL, 'source_unavailable', ?, NULL, ?, ?)
                ON CONFLICT(anchor_fund_code) DO UPDATE SET
                    state = 'source_unavailable',
                    last_attempted_at = excluded.last_attempted_at,
                    error_code = excluded.error_code,
                    warning = excluded.warning
                """,
                (anchor_fund_code, attempted_at.isoformat(), error_code, warning),
            )

    def load_current_group(self, anchor_fund_code: str) -> Optional[PeerGroup]:
        _validate_fund_code(anchor_fund_code)
        with self.repository.connect() as connection:
            row = connection.execute(
                """
                SELECT groups.*
                FROM fund_peer_group_syncs AS syncs
                JOIN fund_peer_groups AS groups
                  ON groups.id = syncs.current_peer_group_id
                WHERE syncs.anchor_fund_code = ?
                """,
                (anchor_fund_code,),
            ).fetchone()
            if row is None:
                return None
            member_rows = connection.execute(
                """
                SELECT * FROM fund_peer_group_members
                WHERE peer_group_id = ?
                ORDER BY CASE membership_kind
                    WHEN 'anchor' THEN 1
                    WHEN 'user_supplied' THEN 2
                    WHEN 'held' THEN 3
                    WHEN 'discovered' THEN 4
                    ELSE 5
                END, rowid
                """,
                (int(row["id"]),),
            ).fetchall()

        group = PeerGroup(
            id=int(row["id"]),
            anchor_fund_code=str(row["anchor_fund_code"]),
            rule_version=str(row["rule_version"]),
            rule_key=str(row["rule_key"]),
            rule_description=str(row["rule_description"]),
            candidate_source_url=str(row["candidate_source_url"]),
            candidate_source_tier=int(row["candidate_source_tier"]),
            candidate_source_checksum=str(row["candidate_source_checksum"]),
            input_fingerprint=str(row["input_fingerprint"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            status=PeerGroupStatus(str(row["status"])),
            members=tuple(
                PeerGroupMember(
                    fund_code=str(member["fund_code"]),
                    membership_kind=MembershipKind(str(member["membership_kind"])),
                    classification_key=str(member["classification_key"]),
                    acceptance_reason=str(member["acceptance_reason"]),
                    warning=(
                        None if member["warning"] is None else str(member["warning"])
                    ),
                    profile_source_document_id=(
                        None
                        if member["profile_source_document_id"] is None
                        else int(member["profile_source_document_id"])
                    ),
                )
                for member in member_rows
            ),
            warnings=_load_warnings(row["warning"]),
        )
        group.validate()
        return group

    def list_anchor_codes(self) -> Tuple[str, ...]:
        with self.repository.connect() as connection:
            rows = connection.execute(
                """
                SELECT anchor_fund_code FROM fund_peer_group_syncs
                WHERE current_peer_group_id IS NOT NULL
                ORDER BY anchor_fund_code
                """
            ).fetchall()
        return tuple(str(row["anchor_fund_code"]) for row in rows)

    def save_comparison(
        self,
        comparison_kind: str,
        anchor_fund_code: Optional[str],
        peer_group_id: Optional[int],
        as_of: datetime,
        status: str,
        input_fingerprint: str,
        result: Mapping[str, object],
        warning: Optional[str],
    ) -> int:
        if comparison_kind not in _COMPARISON_KINDS:
            raise ValueError(f"unsupported comparison kind: {comparison_kind}")
        _validate_optional_fund_code(anchor_fund_code)
        if peer_group_id is not None and (
            not isinstance(peer_group_id, int) or peer_group_id <= 0
        ):
            raise ValueError("peer group id must be positive")
        _validate_aware(as_of, "as_of")
        if status not in _COMPARISON_STATUSES:
            raise ValueError(f"unsupported comparison status: {status}")
        _validate_fingerprint(input_fingerprint)
        if not isinstance(result, Mapping):
            raise ValueError("comparison result must be a mapping")
        _validate_optional_text(warning, "warning")
        result_json = canonical_json(dict(result))

        with self.repository.connect() as connection, connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO fund_comparison_runs(
                    comparison_kind, anchor_fund_code, peer_group_id,
                    calculation_version, as_of, status, input_fingerprint,
                    result_json, warning
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comparison_kind,
                    anchor_fund_code,
                    peer_group_id,
                    PEER_CALCULATION_VERSION,
                    as_of.isoformat(),
                    status,
                    input_fingerprint,
                    result_json,
                    warning,
                ),
            )
            row = connection.execute(
                """
                SELECT id FROM fund_comparison_runs
                WHERE comparison_kind = ? AND input_fingerprint = ?
                  AND calculation_version = ?
                """,
                (comparison_kind, input_fingerprint, PEER_CALCULATION_VERSION),
            ).fetchone()
            if row is None:
                raise RuntimeError("comparison publication did not produce a stored row")
            return int(row["id"])

    def load_comparison(self, run_id: int) -> Optional[Dict[str, object]]:
        if not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("comparison run id must be positive")
        with self.repository.connect() as connection:
            row = connection.execute(
                "SELECT * FROM fund_comparison_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        result = _parse_strict_json(str(row["result_json"]))
        if not isinstance(result, dict):
            raise ValueError("stored comparison result must be a JSON object")
        return {
            "id": int(row["id"]),
            "comparison_kind": str(row["comparison_kind"]),
            "anchor_fund_code": (
                None if row["anchor_fund_code"] is None else str(row["anchor_fund_code"])
            ),
            "peer_group_id": (
                None if row["peer_group_id"] is None else int(row["peer_group_id"])
            ),
            "calculation_version": str(row["calculation_version"]),
            "as_of": datetime.fromisoformat(str(row["as_of"])),
            "status": str(row["status"]),
            "input_fingerprint": str(row["input_fingerprint"]),
            "result": result,
            "warning": None if row["warning"] is None else str(row["warning"]),
        }
