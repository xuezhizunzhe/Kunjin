from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import (
    canonical_fact_set_fingerprint,
    known_native_parser_provenance,
)
from kunjin.funds.risk.models import FactConfidence, MandateFact, decode_fact_value_json
from kunjin.models import (
    AccountObservation,
    FundNavObservation,
    InvestmentThesis,
    PositionObservation,
    SectorObservation,
    StoredPosition,
)
from kunjin.storage.schema import (
    LEGACY_SCHEMA_V8,
    SCHEMA_V1,
    SCHEMA_V2,
    SCHEMA_V3,
    SCHEMA_V4,
    SCHEMA_V5,
    SCHEMA_V6,
    SCHEMA_V7,
    SCHEMA_V8,
    SCHEMA_V9,
    SCHEMA_V10,
    SCHEMA_V11,
    SCHEMA_V12,
    SCHEMA_V13,
    SCHEMA_V14,
    SCHEMA_V15,
    SCHEMA_V16,
    SCHEMA_V17,
    SCHEMA_V18,
    SCHEMA_V19,
    SCHEMA_V20,
    SCHEMA_V21,
    SCHEMA_V22,
    SCHEMA_V23,
    SCHEMA_VERSION,
)
from kunjin.suitability.models import AssessmentStatus, BlockReason, ConstraintReason
from kunjin.suitability.policy import SuitabilityPolicyV1

_SUITABILITY_TABLES = {
    "suitability_policy_versions",
    "suitability_assessments",
}
_D1_TABLES = {
    "fund_document_artifacts",
    "fund_mandate_facts",
    "fund_classification_policy_versions",
    "fund_risk_classifications",
    "fund_document_refresh_runs",
    "fund_document_refresh_completions",
    "fund_document_candidate_runs",
    "fund_document_parser_provenance",
    "fund_document_parse_results",
    "fund_document_parse_runs",
    "fund_document_selection_manifests",
}
_D1_OBJECT_PREFIXES = (
    "fund_document_artifact",
    "fund_document_refresh",
    "fund_document_candidate",
    "fund_document_parser",
    "fund_document_parse",
    "fund_document_selection",
    "fund_document_fact_result",
    "fund_mandate_fact",
    "fund_classification_policy",
    "fund_risk_classification",
)
_DECISION_AUDIT_TABLES = {
    "request_runs",
    "source_attempts",
    "source_work_authorizations",
    "decision_snapshots",
}
_BRIEF_TABLES = {"brief_policy_versions", "fund_brief_snapshots"}
_BRIEF_OBJECT_PREFIXES = ("brief_policy_", "fund_brief_snapshot")
_INTELLIGENCE_TABLES = {
    "intelligence_policy_versions",
    "market_entities",
    "entity_aliases",
    "intelligence_news_items",
    "intelligence_news_excerpts",
    "intelligence_snapshot_item_uses",
    "intelligence_item_integrity_events",
    "intelligence_lineage_edges",
    "intelligence_events",
    "intelligence_event_items",
    "intelligence_event_entities",
    "market_dimension_observations",
    "market_state_snapshots",
    "intelligence_snapshots",
}
_INTELLIGENCE_OBJECT_PREFIXES = (
    "intelligence_",
    "market_entity_",
    "market_entities_",
    "market_dimension_",
    "market_dimension_observations_",
    "market_state_",
    "market_state_snapshots_",
    "entity_alias_",
    "entity_aliases_",
)
_INTELLIGENCE_DROP_TABLES = (
    "intelligence_snapshots",
    "market_state_snapshots",
    "intelligence_event_entities",
    "intelligence_event_items",
    "intelligence_events",
    "intelligence_lineage_edges",
    "intelligence_item_integrity_events",
    "intelligence_snapshot_item_uses",
    "intelligence_news_excerpts",
    "intelligence_news_items",
    "market_dimension_observations",
    "entity_aliases",
    "market_entities",
    "intelligence_policy_versions",
)
_HELD_REVIEW_TABLES = {
    "fund_official_announcement_contents",
    "held_review_official_event_projections",
    "thesis_match_projections",
    "thesis_evidence_adjudications",
    "holding_review_snapshots",
    "held_review_official_check_closures",
}
_HELD_REVIEW_OBJECT_PREFIXES = (
    "fund_official_announcement_content",
    "held_review_official_event_projection",
    "thesis_match_projection",
    "thesis_evidence_adjudication",
    "holding_review_snapshot",
    "held_review_official_check_closure",
)
_DECISION_AUDIT_OBJECT_NAMESPACES = (
    "request_run_",
    "request_runs_",
    "source_attempt_",
    "source_attempts_",
    "source_work_authorization_",
    "source_work_authorizations_",
    "decision_snapshot_",
    "decision_snapshots_",
)
_DECISION_AUDIT_OBJECT_ROOTS = frozenset(
    ("request_run", "source_attempt", "source_work_authorization", "decision_snapshot")
)
_DECISION_AUDIT_ACCESS_ACTIONS = frozenset(
    (sqlite3.SQLITE_READ, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)
)
_LEGACY_V8_POLICY_TABLE = "__kunjin_legacy_v8_policy_versions"
_LEGACY_V8_ASSESSMENT_TABLE = "__kunjin_legacy_v8_assessments"
_WAL_RETRY_TIMEOUT_SECONDS = 5.0
_WAL_RETRY_INTERVAL_SECONDS = 0.01


def _enable_wal(connection: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _WAL_RETRY_TIMEOUT_SECONDS
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if exc.args != ("database is locked",):
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_INTERVAL_SECONDS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _as_decimal(value: Optional[str]) -> Optional[Decimal]:
    return None if value is None else Decimal(value)


def _iter_sql_statements(script: str) -> Iterator[str]:
    buffer: List[str] = []
    for character in script:
        buffer.append(character)
        if character != ";":
            continue
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            buffer.clear()
    if "".join(buffer).strip():
        raise sqlite3.OperationalError("incomplete schema statement")


def _execute_schema(connection: sqlite3.Connection, script: str) -> None:
    for statement in _iter_sql_statements(script):
        connection.execute(statement)


def _migration_definitions() -> Tuple[Tuple[int, str], ...]:
    return (
        (1, SCHEMA_V1),
        (2, SCHEMA_V2),
        (3, SCHEMA_V3),
        (4, SCHEMA_V4),
        (5, SCHEMA_V5),
        (6, SCHEMA_V6),
        (7, SCHEMA_V7),
        (8, SCHEMA_V8),
        (9, SCHEMA_V9),
        (10, SCHEMA_V10),
        (11, SCHEMA_V11),
        (12, SCHEMA_V12),
        (13, SCHEMA_V13),
        (14, SCHEMA_V14),
        (15, SCHEMA_V15),
        (16, SCHEMA_V16),
        (17, SCHEMA_V17),
        (18, SCHEMA_V18),
        (19, SCHEMA_V19),
        (20, SCHEMA_V20),
        (21, SCHEMA_V21),
        (22, SCHEMA_V22),
        (23, SCHEMA_V23),
    )


def _read_schema_objects(connection: sqlite3.Connection) -> Dict[str, Tuple[str, str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    return {
        str(row["name"]): (
            str(row["type"]),
            str(row["tbl_name"]),
            "" if row["sql"] is None else str(row["sql"]),
        )
        for row in rows
    }


@lru_cache(maxsize=16)
def _expected_schema_objects(schemas: Tuple[str, ...]) -> Dict[str, Tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for schema in schemas:
            _execute_schema(connection, schema)
        return _read_schema_objects(connection)
    finally:
        connection.close()


def _owned_suitability_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(name).startswith("suitability_")
        or _ascii_identifier(value[1]) in _SUITABILITY_TABLES
    }


def _owned_d1_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(value[1]) in _D1_TABLES
        or _ascii_identifier(name).startswith(_D1_OBJECT_PREFIXES)
        or any(table in _ascii_identifier(value[2]) for table in _D1_TABLES)
    }


def _owned_decision_audit_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(value[1]) in _DECISION_AUDIT_TABLES
        or _ascii_identifier(name) in _DECISION_AUDIT_OBJECT_ROOTS
        or _ascii_identifier(name).startswith(_DECISION_AUDIT_OBJECT_NAMESPACES)
    }


def _owned_brief_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(value[1]) in _BRIEF_TABLES
        or _ascii_identifier(name).startswith(_BRIEF_OBJECT_PREFIXES)
    }


def _owned_intelligence_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(value[1]) in _INTELLIGENCE_TABLES
        or _ascii_identifier(name).startswith(_INTELLIGENCE_OBJECT_PREFIXES)
    }


def _owned_held_review_objects(
    objects: Dict[str, Tuple[str, str, str]],
) -> Dict[str, Tuple[str, str, str]]:
    return {
        name: value
        for name, value in objects.items()
        if _ascii_identifier(value[1]) in _HELD_REVIEW_TABLES
        or _ascii_identifier(name).startswith(_HELD_REVIEW_OBJECT_PREFIXES)
    }


def _quote_sqlite_identifier(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _trigger_probe_statements(
    connection: sqlite3.Connection,
    target: str,
) -> Tuple[str, ...]:
    quoted_target = _quote_sqlite_identifier(target)
    columns = tuple(
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_xinfo({quoted_target})").fetchall()
        if int(row["hidden"]) in {0, 1}
    )
    statements = [f"EXPLAIN INSERT INTO {quoted_target} DEFAULT VALUES"]
    if columns:
        assignments = ", ".join(
            f"{_quote_sqlite_identifier(column)} = {_quote_sqlite_identifier(column)}"
            for column in columns
        )
        statements.append(f"EXPLAIN UPDATE {quoted_target} SET {assignments} WHERE 0")
    for alias in ("rowid", "_rowid_", "oid"):
        quoted_alias = _quote_sqlite_identifier(alias)
        statements.append(
            f"EXPLAIN UPDATE {quoted_target} SET {quoted_alias} = {quoted_alias} WHERE 0"
        )
    statements.append(f"EXPLAIN DELETE FROM {quoted_target} WHERE 0")
    return tuple(statements)


def _reject_unexpected_schema_dependencies(
    connection: sqlite3.Connection,
    expected: Dict[str, Tuple[str, str, str]],
    actual: Dict[str, Tuple[str, str, str]],
    *,
    protected_tables: set,
    error_message: str,
) -> None:
    normalized_protected_tables = {_ascii_identifier(table) for table in protected_tables}
    extras = {name: value for name, value in actual.items() if name not in expected}
    if not extras:
        return
    normalized_extra_names = {_ascii_identifier(name) for name in extras}
    unexpected_virtual_tables = {
        _ascii_identifier(str(row["name"]))
        for row in connection.execute("PRAGMA table_list").fetchall()
        if str(row["schema"]) == "main"
        and _ascii_identifier(str(row["name"])) in normalized_extra_names
        and str(row["type"]).casefold() in {"virtual", "shadow"}
    }
    if unexpected_virtual_tables:
        raise sqlite3.DatabaseError(error_message)
    main_database = next(
        (
            str(row["file"])
            for row in connection.execute("PRAGMA database_list").fetchall()
            if str(row["name"]) == "main"
        ),
        "",
    )
    if not main_database:
        raise sqlite3.DatabaseError(error_message)
    probe = sqlite3.connect(main_database)
    probe.row_factory = sqlite3.Row

    extra_triggers = {
        _ascii_identifier(name): str(value[1])
        for name, value in extras.items()
        if value[0] == "trigger"
    }
    extra_views = {
        _ascii_identifier(name): str(name) for name, value in extras.items() if value[0] == "view"
    }

    monitored_sources = set(extra_triggers) | set(extra_views)
    observed_sources = set()
    dependency_sources = set()
    statement_observed_sources = set()
    statement_dependency_sources = set()

    def authorize(
        action: int,
        arg1: Optional[str],
        arg2: Optional[str],
        database_name: Optional[str],
        source: Optional[str],
    ) -> int:
        del arg2, database_name
        normalized_source = None if source is None else _ascii_identifier(source)
        if normalized_source in monitored_sources:
            statement_observed_sources.add(normalized_source)
            if (
                action in _DECISION_AUDIT_ACCESS_ACTIONS
                and arg1 is not None
                and _ascii_identifier(arg1) in normalized_protected_tables
            ):
                statement_dependency_sources.add(normalized_source)
        return sqlite3.SQLITE_OK

    compiled_trigger_targets = set()
    compiled_views = set()
    try:
        for name, value in extras.items():
            if value[0] != "table":
                continue
            quoted_name = _quote_sqlite_identifier(str(name))
            foreign_keys = probe.execute(f"PRAGMA foreign_key_list({quoted_name})").fetchall()
            if any(
                _ascii_identifier(str(row["table"])) in normalized_protected_tables
                for row in foreign_keys
            ):
                raise sqlite3.DatabaseError(error_message)

        probe.set_authorizer(authorize)
        try:
            for target in sorted(set(extra_triggers.values()), key=_ascii_identifier):
                target_compiled = False
                for statement in _trigger_probe_statements(probe, target):
                    statement_observed_sources.clear()
                    statement_dependency_sources.clear()
                    try:
                        probe.execute(statement).close()
                    except sqlite3.Error:
                        continue
                    observed_sources.update(statement_observed_sources)
                    dependency_sources.update(statement_dependency_sources)
                    target_compiled = True
                if target_compiled:
                    compiled_trigger_targets.add(_ascii_identifier(target))

            for normalized_name, name in sorted(extra_views.items()):
                statement = f"EXPLAIN SELECT * FROM {_quote_sqlite_identifier(name)}"
                statement_observed_sources.clear()
                statement_dependency_sources.clear()
                try:
                    probe.execute(statement).close()
                except sqlite3.Error:
                    continue
                observed_sources.update(statement_observed_sources)
                dependency_sources.update(statement_dependency_sources)
                compiled_views.add(normalized_name)
        finally:
            probe.set_authorizer(None)
    finally:
        probe.close()

    if dependency_sources:
        raise sqlite3.DatabaseError(error_message)
    if any(
        _ascii_identifier(target) not in compiled_trigger_targets
        or normalized_name not in observed_sources
        for normalized_name, target in extra_triggers.items()
    ):
        raise sqlite3.DatabaseError(error_message)
    if any(
        normalized_name not in compiled_views or normalized_name not in observed_sources
        for normalized_name in extra_views
    ):
        raise sqlite3.DatabaseError(error_message)


def _ascii_identifier(value: str) -> str:
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _validate_migration_markers(applied_versions: set) -> int:
    if any(version <= 0 or version > SCHEMA_VERSION for version in applied_versions):
        raise sqlite3.DatabaseError("invalid schema migration markers")
    if not applied_versions:
        return 0
    maximum = max(applied_versions)
    if applied_versions != set(range(1, maximum + 1)):
        raise sqlite3.DatabaseError("invalid schema migration markers")
    return maximum


def _reject_unexpected_suitability_foreign_keys(connection: sqlite3.Connection) -> None:
    table_rows = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    for row in table_rows:
        table = str(row["name"])
        normalized_table = _ascii_identifier(table)
        quoted_table = table.replace('"', '""')
        foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{quoted_table}")').fetchall()
        for foreign_key in foreign_keys:
            target = str(foreign_key["table"])
            normalized_target = _ascii_identifier(target)
            expected_policy_reference = (
                normalized_table == "suitability_assessments"
                and normalized_target == "suitability_policy_versions"
            )
            if normalized_target in _SUITABILITY_TABLES and not expected_policy_reference:
                raise sqlite3.DatabaseError(
                    "unexpected foreign key references legacy suitability schema"
                )


def _legacy_v8_normalization_checkpoint(stage: str) -> None:
    del stage


def _legacy_text(value: object, name: str, *, nonempty: bool = True) -> str:
    if type(value) is not str or "\x00" in value:
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    if nonempty and not value.strip():
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    return value


def _legacy_digest(value: object, name: str) -> str:
    text = _legacy_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    return text


def _legacy_datetime(value: object, name: str) -> datetime:
    text = _legacy_text(value, name)
    parse_value = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError:
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    canonical = parsed.isoformat()
    if canonical != text and not (text.endswith("Z") and canonical == parse_value):
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    return parsed


def _legacy_json(value: object, name: str) -> object:
    text = _legacy_text(value, name)

    def object_without_duplicates(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def reject_constant(_value):
        raise ValueError("unsupported constant")

    try:
        return json.loads(
            text,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}") from None


def _legacy_reason_values(value: object, enum_type, name: str) -> Tuple[str, ...]:
    parsed = _legacy_json(value, name)
    if not isinstance(parsed, list):
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    reasons = []
    for item in parsed:
        if type(item) is not str:
            raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
        try:
            enum_type(item)
        except ValueError:
            raise sqlite3.DatabaseError(f"invalid legacy V8 {name}") from None
        reasons.append(item)
    if len(reasons) != len(set(reasons)):
        raise sqlite3.DatabaseError(f"invalid legacy V8 {name}")
    return tuple(reasons)


def _validate_legacy_safe_summary(value: object) -> None:
    parsed = _legacy_json(value, "safe_summary_json")
    integer_keys = {
        "debt_count",
        "goal_count",
        "obligation_count",
        "required_reserve_months",
    }
    expected_keys = integer_keys | {"risk_answers_consistent"}
    if not isinstance(parsed, dict) or set(parsed) != expected_keys:
        raise sqlite3.DatabaseError("invalid legacy V8 safe_summary_json")
    if any(type(parsed[key]) is not int or parsed[key] < 0 for key in integer_keys):
        raise sqlite3.DatabaseError("invalid legacy V8 safe_summary_json")
    if type(parsed["risk_answers_consistent"]) is not bool:
        raise sqlite3.DatabaseError("invalid legacy V8 safe_summary_json")


def _validate_legacy_v8_rows(connection: sqlite3.Connection) -> None:
    profile_ids = {
        int(row["id"])
        for row in connection.execute("SELECT id FROM financial_profile_versions").fetchall()
        if type(row["id"]) is int and int(row["id"]) > 0
    }
    policy_versions = set()
    policy_rows = connection.execute(
        "SELECT * FROM suitability_policy_versions ORDER BY version"
    ).fetchall()
    fixed_policy = SuitabilityPolicyV1()
    fixed_policy.validate()
    fixed_canonical = fixed_policy.canonical_json().decode("utf-8")
    fixed_checksum = fixed_policy.checksum()
    fixed_effective_at = fixed_policy.effective_at.isoformat()
    for row in policy_rows:
        version = _legacy_text(row["version"], "policy version")
        canonical_policy = _legacy_text(row["canonical_policy_json"], "canonical_policy_json")
        parsed_policy = _legacy_json(canonical_policy, "canonical_policy_json")
        if not isinstance(parsed_policy, dict) or canonical_policy != json.dumps(
            parsed_policy, separators=(",", ":"), sort_keys=True
        ):
            raise sqlite3.DatabaseError("invalid legacy V8 canonical_policy_json")
        if parsed_policy.get("version") != version:
            raise sqlite3.DatabaseError("invalid legacy V8 policy JSON version")
        checksum = _legacy_digest(row["policy_checksum"], "policy_checksum")
        effective_at_text = _legacy_text(row["effective_at"], "effective_at")
        effective_at = _legacy_datetime(row["effective_at"], "effective_at")
        created_at = _legacy_datetime(row["created_at"], "policy created_at")
        if (
            version != fixed_policy.version
            or canonical_policy != fixed_canonical
            or checksum != fixed_checksum
            or effective_at_text != fixed_effective_at
        ):
            raise sqlite3.DatabaseError("invalid legacy V8 fixed suitability policy")
        if created_at < effective_at:
            raise sqlite3.DatabaseError("invalid legacy V8 policy timestamp ordering")
        policy_versions.add(version)

    assessment_rows = connection.execute(
        "SELECT * FROM suitability_assessments ORDER BY id"
    ).fetchall()
    for row in assessment_rows:
        if type(row["id"]) is not int or row["id"] <= 0:
            raise sqlite3.DatabaseError("invalid legacy V8 assessment id")
        if (
            type(row["profile_version_id"]) is not int
            or row["profile_version_id"] <= 0
            or row["profile_version_id"] not in profile_ids
        ):
            raise sqlite3.DatabaseError("invalid legacy V8 profile_version_id")
        policy_version = _legacy_text(row["policy_version"], "assessment policy version")
        if policy_version not in policy_versions:
            raise sqlite3.DatabaseError("invalid legacy V8 assessment policy version")
        _legacy_digest(row["input_fingerprint"], "input_fingerprint")
        try:
            status = AssessmentStatus(_legacy_text(row["status"], "assessment status"))
        except ValueError:
            raise sqlite3.DatabaseError("invalid legacy V8 assessment status") from None
        hard_blocks = _legacy_reason_values(
            row["hard_blocks_json"], BlockReason, "hard_blocks_json"
        )
        constraints = _legacy_reason_values(
            row["constraints_json"], ConstraintReason, "constraints_json"
        )
        expected_status = AssessmentStatus.READY_FOR_ALLOCATION
        if hard_blocks:
            expected_status = AssessmentStatus.BLOCKED
        elif constraints:
            expected_status = AssessmentStatus.CONSTRAINED
        if status is not expected_status:
            raise sqlite3.DatabaseError("invalid legacy V8 assessment status reasons")
        _validate_legacy_safe_summary(row["safe_summary_json"])
        _legacy_text(row["encrypted_amount_results"], "encrypted_amount_results")
        if _legacy_text(row["encryption_algorithm"], "encryption_algorithm") != "AES-256-GCM":
            raise sqlite3.DatabaseError("invalid legacy V8 encryption_algorithm")
        _legacy_text(row["encryption_key_version"], "encryption_key_version")
        _legacy_text(row["nonce"], "nonce")
        _legacy_digest(row["keyed_payload_fingerprint"], "keyed_payload_fingerprint")
        assessed_at = _legacy_datetime(row["assessed_at"], "assessed_at")
        valid_until = _legacy_datetime(row["valid_until"], "valid_until")
        _legacy_datetime(row["created_at"], "assessment created_at")
        if valid_until <= assessed_at:
            raise sqlite3.DatabaseError("invalid legacy V8 assessment timestamp ordering")


def _normalization_name_collisions(connection: sqlite3.Connection) -> None:
    reserved = {
        _LEGACY_V8_POLICY_TABLE,
        _LEGACY_V8_ASSESSMENT_TABLE,
    }
    actual_names = {_ascii_identifier(name) for name in _read_schema_objects(connection)}
    temp_names = {
        _ascii_identifier(str(row["name"]))
        for row in connection.execute(
            "SELECT name FROM sqlite_temp_master ORDER BY name"
        ).fetchall()
    }
    if reserved & actual_names or reserved & temp_names:
        raise sqlite3.DatabaseError("legacy V8 normalization name collision")
    if _SUITABILITY_TABLES & temp_names:
        raise sqlite3.DatabaseError("legacy V8 normalization name collision")


def _legacy_assessment_sequence(connection: sqlite3.Connection):
    matching_rows = []
    for row in connection.execute(
        "SELECT name, seq, typeof(name) AS name_type, typeof(seq) AS seq_type "
        "FROM sqlite_sequence ORDER BY rowid"
    ).fetchall():
        raw_name = row["name"]
        if type(raw_name) is bytes:
            try:
                comparable_name = raw_name.decode("ascii")
            except UnicodeDecodeError:
                comparable_name = ""
        elif type(raw_name) is str:
            comparable_name = raw_name
        else:
            comparable_name = ""
        if _ascii_identifier(comparable_name) == "suitability_assessments":
            matching_rows.append(row)
    if len(matching_rows) > 1:
        raise sqlite3.DatabaseError("invalid legacy V8 assessment sequence")
    if not matching_rows:
        return None
    row = matching_rows[0]
    if (
        row["name"] != "suitability_assessments"
        or row["seq_type"] != "integer"
        or int(row["seq"]) < 0
    ):
        raise sqlite3.DatabaseError("invalid legacy V8 assessment sequence")
    return row["seq"]


def _normalize_legacy_v8(connection: sqlite3.Connection) -> None:
    _normalization_name_collisions(connection)

    _reject_unexpected_suitability_foreign_keys(connection)
    _validate_legacy_v8_rows(connection)
    preserved_sequence = _legacy_assessment_sequence(connection)

    connection.execute(
        f'CREATE TEMP TABLE "{_LEGACY_V8_POLICY_TABLE}" AS '
        "SELECT * FROM main.suitability_policy_versions"
    )
    connection.execute(
        f'CREATE TEMP TABLE "{_LEGACY_V8_ASSESSMENT_TABLE}" AS '
        "SELECT * FROM main.suitability_assessments ORDER BY id"
    )
    _legacy_v8_normalization_checkpoint("backed_up")

    connection.execute("DROP TABLE main.suitability_assessments")
    connection.execute("DROP TABLE main.suitability_policy_versions")

    _execute_schema(connection, SCHEMA_V8)
    _legacy_v8_normalization_checkpoint("schema_created")
    connection.execute(
        f"""
        INSERT INTO suitability_policy_versions(
            version, canonical_policy_json, policy_checksum, effective_at, created_at
        )
        SELECT
            version, canonical_policy_json, policy_checksum, effective_at, created_at
        FROM temp."{_LEGACY_V8_POLICY_TABLE}"
        """
    )
    connection.execute(
        f"""
        INSERT INTO suitability_assessments(
            id, profile_version_id, policy_version, input_fingerprint, status,
            hard_blocks_json, constraints_json, safe_summary_json,
            encrypted_amount_results, encryption_algorithm, encryption_key_version,
            nonce, keyed_payload_fingerprint, assessed_at, valid_until, created_at
        )
        SELECT
            id, profile_version_id, policy_version, input_fingerprint, status,
            hard_blocks_json, constraints_json, safe_summary_json,
            encrypted_amount_results, encryption_algorithm, encryption_key_version,
            nonce, keyed_payload_fingerprint, assessed_at, valid_until, created_at
        FROM temp."{_LEGACY_V8_ASSESSMENT_TABLE}"
        ORDER BY id
        """
    )
    _legacy_v8_normalization_checkpoint("rows_copied")
    connection.execute(f'DROP TABLE temp."{_LEGACY_V8_ASSESSMENT_TABLE}"')
    connection.execute(f'DROP TABLE temp."{_LEGACY_V8_POLICY_TABLE}"')
    connection.execute("DELETE FROM sqlite_sequence WHERE name = 'suitability_assessments'")
    if preserved_sequence is not None:
        connection.execute(
            "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
            ("suitability_assessments", preserved_sequence),
        )
    _legacy_v8_normalization_checkpoint("legacy_dropped")

    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise sqlite3.IntegrityError("legacy V8 normalization violated foreign keys")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise sqlite3.DatabaseError("legacy V8 normalization failed integrity check")


def _normalize_exact_legacy_v8_if_needed(
    connection: sqlite3.Connection,
    applied_versions: set,
    migrations: Tuple[Tuple[int, str], ...],
) -> None:
    if applied_versions != set(range(1, 9)):
        return

    actual = _read_schema_objects(connection)
    schemas_before_v8 = tuple(schema for version, schema in migrations if version < 8)
    expected_before_v8 = _expected_schema_objects(schemas_before_v8)
    for name, expected_object in expected_before_v8.items():
        if actual.get(name) != expected_object:
            return

    actual_owned = _owned_suitability_objects(actual)
    strict_owned = _owned_suitability_objects(
        _expected_schema_objects(schemas_before_v8 + (SCHEMA_V8,))
    )
    if actual_owned == strict_owned:
        return
    legacy_owned = _owned_suitability_objects(
        _expected_schema_objects(schemas_before_v8 + (LEGACY_SCHEMA_V8,))
    )
    if actual_owned != legacy_owned:
        return
    _normalize_legacy_v8(connection)


def _normalize_empty_legacy_v19_if_needed(
    connection: sqlite3.Connection,
    applied_versions: set,
    migrations: Tuple[Tuple[int, str], ...],
) -> None:
    if applied_versions != set(range(1, 20)):
        return

    actual = _read_schema_objects(connection)
    expected_before = _expected_schema_objects(
        tuple(schema for version, schema in migrations if version < 19)
    )
    if any(actual.get(name) != value for name, value in expected_before.items()):
        return

    expected_through_v19 = _expected_schema_objects(
        tuple(schema for version, schema in migrations if version <= 19)
    )
    expected_owned = _owned_intelligence_objects(expected_through_v19)
    actual_owned = _owned_intelligence_objects(actual)
    if actual_owned == expected_owned:
        return
    if not actual_owned or not set(actual_owned).issubset(expected_owned):
        return
    if set(actual).difference(expected_before) != set(actual_owned):
        return

    actual_tables = {
        name for name, value in actual_owned.items() if value[0] == "table"
    }
    if not actual_tables.issubset(_INTELLIGENCE_TABLES):
        return
    for table in actual_tables:
        row = connection.execute(
            f"SELECT 1 FROM {_quote_sqlite_identifier(table)} LIMIT 1"
        ).fetchone()
        if row is not None:
            return

    for name, value in sorted(actual_owned.items()):
        if value[0] not in {"index", "trigger", "view"}:
            continue
        connection.execute(
            f"DROP {value[0].upper()} {_quote_sqlite_identifier(name)}"
        )
    for table in _INTELLIGENCE_DROP_TABLES:
        if table in actual_tables:
            connection.execute(f"DROP TABLE {_quote_sqlite_identifier(table)}")
    if _owned_intelligence_objects(_read_schema_objects(connection)):
        raise sqlite3.DatabaseError("empty legacy V19 normalization failed")
    connection.execute("DELETE FROM schema_migrations WHERE version=19")
    applied_versions.remove(19)


def _validate_applied_schema(
    connection: sqlite3.Connection,
    applied_versions: set,
    migrations: Tuple[Tuple[int, str], ...],
) -> None:
    maximum = _validate_migration_markers(applied_versions)

    schemas = tuple(schema for version, schema in migrations if version <= maximum)
    expected = _expected_schema_objects(schemas)
    actual = _read_schema_objects(connection)
    for name, expected_object in expected.items():
        if actual.get(name) != expected_object:
            raise sqlite3.DatabaseError("applied schema does not match migration markers")

    if maximum >= 8:
        expected_suitability = _owned_suitability_objects(expected)
        actual_suitability = _owned_suitability_objects(actual)
        if actual_suitability != expected_suitability:
            raise sqlite3.DatabaseError("suitability schema does not match V8")

    if 9 not in applied_versions:
        return
    expected_before_v9 = _expected_schema_objects(
        tuple(schema for version, schema in migrations if version < 9)
    )
    expected_through_v9 = _expected_schema_objects(
        tuple(schema for version, schema in migrations if version <= 9)
    )
    expected_v9 = {
        name: value for name, value in expected_through_v9.items() if name not in expected_before_v9
    }
    allocation_tables = {"allocation_policy_versions", "allocation_assessments"}
    actual_v9 = {
        name: value
        for name, value in actual.items()
        if name.startswith("allocation_") or value[1] in allocation_tables
    }
    if actual_v9 != expected_v9:
        raise sqlite3.DatabaseError("allocation schema does not match V9")

    if 10 not in applied_versions:
        return
    expected_v10 = _owned_d1_objects(expected)
    if _owned_d1_objects(actual) != expected_v10:
        raise sqlite3.DatabaseError("fund risk schema does not match the current D1 schema")

    if 14 not in applied_versions:
        return
    expected_v15 = _owned_decision_audit_objects(expected)
    if _owned_decision_audit_objects(actual) != expected_v15:
        raise sqlite3.DatabaseError("decision audit schema does not match V15")
    _reject_unexpected_schema_dependencies(
        connection,
        expected,
        actual,
        protected_tables=_DECISION_AUDIT_TABLES,
        error_message="decision audit schema does not match V15",
    )
    if 16 not in applied_versions:
        return
    if _owned_brief_objects(actual) != _owned_brief_objects(expected):
        raise sqlite3.DatabaseError("brief schema does not match V16")
    _reject_unexpected_schema_dependencies(
        connection,
        expected,
        actual,
        protected_tables=_BRIEF_TABLES,
        error_message="brief schema does not match V16",
    )
    if 19 not in applied_versions:
        return
    if _owned_intelligence_objects(actual) != _owned_intelligence_objects(expected):
        raise sqlite3.DatabaseError("intelligence schema does not match V19")
    _reject_unexpected_schema_dependencies(
        connection,
        expected,
        actual,
        protected_tables=_INTELLIGENCE_TABLES,
        error_message="intelligence schema does not match V19",
    )
    if 21 not in applied_versions:
        return
    held_review_version = 22 if 22 in applied_versions else 21
    held_review_error = f"held review schema does not match V{held_review_version}"
    if _owned_held_review_objects(actual) != _owned_held_review_objects(expected):
        raise sqlite3.DatabaseError(held_review_error)
    _reject_unexpected_schema_dependencies(
        connection,
        expected,
        actual,
        protected_tables=_HELD_REVIEW_TABLES,
        error_message=held_review_error,
    )


_FUND_MANDATE_FACT_NO_UPDATE = """
CREATE TRIGGER fund_mandate_fact_no_update
BEFORE UPDATE ON fund_mandate_facts
BEGIN
    SELECT RAISE(ABORT, 'fund mandate facts are immutable');
END;
"""

_V11_PARSE_ERROR_CODES = frozenset(
    {
        "official_document_parse_failed",
        "official_document_resource_limit",
    }
)


def _v11_optional_text(value: object, name: str, *, maximum: int) -> Optional[str]:
    if value is None:
        return None
    text = _legacy_text(value, name)
    if len(text) > maximum:
        raise sqlite3.DatabaseError(f"invalid legacy V11 {name}")
    return text


def _v11_date(value: object, name: str) -> Optional[date]:
    if value is None:
        return None
    text = _legacy_text(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise sqlite3.DatabaseError(f"invalid legacy V11 {name}") from None
    if parsed.isoformat() != text:
        raise sqlite3.DatabaseError(f"invalid legacy V11 {name}")
    return parsed


def _decode_v11_fact(row: sqlite3.Row, artifact: sqlite3.Row) -> MandateFact:
    try:
        fact_id = row["id"]
        source_document_id = row["source_document_id"]
        if type(fact_id) is not int or fact_id <= 0:
            raise ValueError("fact id must be positive")
        if type(source_document_id) is not int or source_document_id != artifact["id"]:
            raise ValueError("fact source document does not match artifact")
        normalized_json = _legacy_text(row["normalized_value_json"], "normalized_value_json")
        normalized_value = decode_fact_value_json(normalized_json)
        confidence = FactConfidence(_legacy_text(row["confidence_state"], "confidence_state"))
        fact = MandateFact(
            fund_code=_legacy_text(row["fund_code"], "fact fund_code"),
            fact_kind=_legacy_text(row["fact_kind"], "fact_kind"),
            normalized_value=normalized_value,
            unit=_v11_optional_text(row["unit"], "unit", maximum=64),
            source_document_id=source_document_id,
            page_number=row["page_number"],
            section_name=_v11_optional_text(row["section_name"], "section_name", maximum=256),
            source_excerpt=_legacy_text(row["source_excerpt"], "source_excerpt"),
            effective_from=_v11_date(row["effective_from"], "effective_from"),
            effective_to=_v11_date(row["effective_to"], "effective_to"),
            confidence_state=confidence,
            parser_version=_legacy_text(row["parser_version"], "fact parser_version"),
            fact_fingerprint=_legacy_digest(row["fact_fingerprint"], "fact_fingerprint"),
        )
        fact.validate()
    except (KeyError, TypeError, ValueError):
        raise sqlite3.DatabaseError("invalid legacy V11 mandate fact") from None
    if fact.fund_code != artifact["fund_code"]:
        raise sqlite3.DatabaseError("invalid legacy V11 fact fund binding")
    return fact


def _backfill_v11_document_audit(connection: sqlite3.Connection) -> None:
    provenance = known_native_parser_provenance("2")
    artifact_rows = connection.execute(
        "SELECT * FROM fund_document_artifacts ORDER BY id"
    ).fetchall()
    if not artifact_rows:
        return

    timestamps = []
    for artifact in artifact_rows:
        artifact_id = artifact["id"]
        if type(artifact_id) is not int or artifact_id <= 0:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact id")
        fund_code = _legacy_text(artifact["fund_code"], "artifact fund_code")
        if len(fund_code) != 6 or not fund_code.isascii() or not fund_code.isdigit():
            raise sqlite3.DatabaseError("invalid legacy V11 artifact fund code")
        _legacy_digest(artifact["sha256"], "artifact sha256")
        try:
            document_kind = DocumentKind(
                _legacy_text(artifact["document_kind"], "artifact document_kind")
            )
        except ValueError:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact document kind") from None
        if document_kind not in {
            DocumentKind.FUND_CONTRACT,
            DocumentKind.PROSPECTUS,
            DocumentKind.PROSPECTUS_UPDATE,
            DocumentKind.PRODUCT_SUMMARY,
            DocumentKind.ANNUAL_REPORT,
            DocumentKind.SEMIANNUAL_REPORT,
            DocumentKind.QUARTERLY_REPORT,
            DocumentKind.INDEX_METHODOLOGY,
            DocumentKind.CLASSIFICATION_ANNOUNCEMENT,
        }:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact document kind")
        for column in (
            "url",
            "landing_url",
            "publisher",
            "title",
            "content_type",
            "managed_path",
        ):
            _legacy_text(artifact[column], f"artifact {column}")
        published_at = artifact["published_at"]
        if published_at is not None:
            parsed_published_at = _legacy_datetime(published_at, "artifact published_at")
            if parsed_published_at.tzinfo is not timezone.utc:
                raise sqlite3.DatabaseError("invalid legacy V11 artifact published_at")
        byte_size = artifact["byte_size"]
        if type(byte_size) is not int or not 0 < byte_size <= 33554432:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact byte size")
        retrieved_at = _legacy_datetime(artifact["retrieved_at"], "artifact retrieved_at")
        if retrieved_at.tzinfo is not timezone.utc:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact retrieved_at")
        timestamps.append(str(artifact["retrieved_at"]))
        if _legacy_text(artifact["parser_version"], "artifact parser_version") != (
            provenance.parser_version
        ):
            raise sqlite3.DatabaseError("unsupported legacy V11 parser provenance")
        parse_status = _legacy_text(artifact["parse_status"], "artifact parse_status")
        if parse_status not in {"parsed", "failed"}:
            raise sqlite3.DatabaseError("invalid legacy V11 artifact parse status")

    connection.execute(
        """
        INSERT INTO fund_document_parser_provenance(
            parser_version, converter_kind, canonical_json,
            provenance_checksum, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            provenance.parser_version,
            provenance.converter_kind,
            provenance.canonical_json,
            provenance.provenance_checksum,
            min(timestamps),
        ),
    )
    provenance_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    for artifact in artifact_rows:
        artifact_id = int(artifact["id"])
        fact_rows = connection.execute(
            "SELECT * FROM fund_mandate_facts WHERE source_document_id = ? ORDER BY id",
            (artifact_id,),
        ).fetchall()
        facts = tuple(_decode_v11_fact(row, artifact) for row in fact_rows)
        if any(fact.parser_version != provenance.parser_version for fact in facts):
            raise sqlite3.DatabaseError("invalid legacy V11 fact parser provenance")
        parse_status = str(artifact["parse_status"])
        if parse_status == "failed":
            if facts:
                raise sqlite3.DatabaseError("failed legacy V11 artifact has mandate facts")
            error_code = artifact["parse_error_code"]
            if type(error_code) is not str or error_code not in _V11_PARSE_ERROR_CODES:
                raise sqlite3.DatabaseError("invalid legacy V11 parse error code")
            connection.execute(
                """
                INSERT INTO fund_document_parse_runs(
                    source_document_id, provenance_id, run_kind, outcome,
                    parse_result_id, public_error_code, failure_stage,
                    failure_reason, attempted_at
                ) VALUES (?, ?, 'legacy_backfill', 'failed', NULL, ?, NULL, NULL, ?)
                """,
                (artifact_id, provenance_id, error_code, artifact["retrieved_at"]),
            )
            continue

        if artifact["parse_error_code"] is not None:
            raise sqlite3.DatabaseError("parsed legacy V11 artifact has a parse error")
        fact_set_fingerprint = canonical_fact_set_fingerprint(
            tuple(fact.fact_fingerprint for fact in facts)
        )
        connection.execute(
            """
            INSERT INTO fund_document_parse_results(
                source_document_id, provenance_id, parser_input_sha256,
                fact_set_fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                provenance_id,
                artifact["sha256"],
                fact_set_fingerprint,
                artifact["retrieved_at"],
            ),
        )
        result_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "UPDATE fund_mandate_facts SET parse_result_id = ? WHERE source_document_id = ?",
            (result_id, artifact_id),
        )
        connection.execute(
            """
            INSERT INTO fund_document_parse_runs(
                source_document_id, provenance_id, run_kind, outcome,
                parse_result_id, public_error_code, failure_stage,
                failure_reason, attempted_at
            ) VALUES (?, ?, 'legacy_backfill', 'success', ?, NULL, NULL, NULL, ?)
            """,
            (artifact_id, provenance_id, result_id, artifact["retrieved_at"]),
        )


def _migrate_v12(connection: sqlite3.Connection) -> None:
    _execute_schema(connection, SCHEMA_V12)
    connection.execute("DROP TRIGGER fund_mandate_fact_no_update")
    _backfill_v11_document_audit(connection)
    _execute_schema(connection, _FUND_MANDATE_FACT_NO_UPDATE)


class Repository:
    def __init__(self, database: Path) -> None:
        self.database = Path(database)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(str(self.database))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function(
            "sha256",
            1,
            lambda value: hashlib.sha256(str(value).encode("utf-8")).digest(),
            deterministic=True,
        )
        connection.create_function(
            "kunjin_excerpt_expiry_cutoff",
            0,
            lambda: _utc_now().isoformat(),
        )
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            _enable_wal(connection)
            migrations = _migration_definitions()
            try:
                connection.execute("BEGIN IMMEDIATE")
                marker_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'schema_migrations'
                    """
                ).fetchone()
                if marker_exists is None:
                    _execute_schema(connection, SCHEMA_V1)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
                        """
                    )

                applied_versions = {
                    int(row["version"])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                _validate_migration_markers(applied_versions)
                _normalize_exact_legacy_v8_if_needed(connection, applied_versions, migrations)
                _normalize_empty_legacy_v19_if_needed(
                    connection,
                    applied_versions,
                    migrations,
                )
                _validate_applied_schema(connection, applied_versions, migrations)
                for version, schema in migrations:
                    if version in applied_versions:
                        continue
                    if version == 12:
                        _migrate_v12(connection)
                    else:
                        _execute_schema(connection, schema)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (?, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
                        """,
                        (version,),
                    )
                final_versions = {
                    int(row["version"])
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                _validate_applied_schema(connection, final_versions, migrations)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.database.chmod(0o600)

    def table_names(self) -> set:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def begin_sync(
        self,
        source: str,
        trigger: str,
        *,
        connection: Optional[sqlite3.Connection] = None,
        started_at: Optional[datetime] = None,
    ) -> int:
        if connection is not None and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        timestamp = (started_at or _utc_now()).astimezone(timezone.utc).isoformat()

        def write(active_connection: sqlite3.Connection) -> int:
            cursor = active_connection.execute(
                "INSERT INTO sync_runs(source, trigger, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (source, trigger, timestamp),
            )
            return int(cursor.lastrowid)

        if connection is not None:
            return write(connection)
        with self.connect() as owned_connection, owned_connection:
            return write(owned_connection)

    def commit_sync(
        self,
        sync_run_id: int,
        raw_snapshots: Sequence[Tuple[str, str, str, datetime]],
        observations: Sequence[Tuple[AccountObservation, Sequence[PositionObservation]]],
        *,
        connection: Optional[sqlite3.Connection] = None,
        observed_at: Optional[datetime] = None,
    ) -> None:
        if connection is not None and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        account_ids = tuple(account.source_account_id for account, _positions in observations)
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("portfolio snapshot contains duplicate accounts")
        for account, positions in observations:
            account.validate()
            for position in positions:
                position.validate()
                if position.source_account_id != account.source_account_id:
                    raise ValueError("position account id does not match account")

        snapshot_time = observed_at or max(
            (account.observed_at for account, _positions in observations),
            default=_utc_now(),
        )
        if snapshot_time.tzinfo is None or snapshot_time.utcoffset() is None:
            raise ValueError("portfolio snapshot time must be aware")
        snapshot_time = snapshot_time.astimezone(timezone.utc)
        finished_at = _utc_now().isoformat()

        def write(active_connection: sqlite3.Connection) -> None:
            sync_run = active_connection.execute(
                "SELECT source, status FROM sync_runs WHERE id = ?",
                (sync_run_id,),
            ).fetchone()
            if sync_run is None:
                raise ValueError("portfolio sync run does not exist")
            if str(sync_run["source"]) != "yangjibao":
                raise ValueError("portfolio sync run source must be yangjibao")
            if str(sync_run["status"]) != "running":
                raise ValueError("portfolio sync run must be running")
            for endpoint, payload_json, checksum, retrieved_at in raw_snapshots:
                active_connection.execute(
                    """
                    INSERT INTO raw_snapshots(
                        sync_run_id, endpoint, retrieved_at, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (sync_run_id, endpoint, retrieved_at.isoformat(), payload_json, checksum),
                )

            position_count = 0
            for account, positions in observations:
                if account.source != "yangjibao":
                    raise ValueError("portfolio account source must be yangjibao")
                if account.observed_at > snapshot_time:
                    raise ValueError("account observation follows portfolio snapshot")
                active_connection.execute(
                    """
                    INSERT INTO accounts(source, source_account_id, title, observed_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, source_account_id) DO UPDATE SET
                        title = excluded.title,
                        observed_at = excluded.observed_at
                    WHERE excluded.observed_at >= accounts.observed_at
                    """,
                    (
                        account.source,
                        account.source_account_id,
                        account.title,
                        account.observed_at.isoformat(),
                    ),
                )
                account_row = active_connection.execute(
                    "SELECT id FROM accounts WHERE source = ? AND source_account_id = ?",
                    (account.source, account.source_account_id),
                ).fetchone()
                account_id = int(account_row["id"])
                active_connection.execute(
                    """
                    INSERT INTO portfolio_observation_accounts(
                        sync_run_id, account_id, account_title, observed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        sync_run_id,
                        account_id,
                        account.title,
                        account.observed_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
                for position in positions:
                    if position.observed_at != account.observed_at:
                        raise ValueError("position observation time does not match account")
                    active_connection.execute(
                        """
                        INSERT INTO positions(
                            account_id, fund_code, fund_name, share_class, shares,
                            formal_nav, estimated_nav, observed_profit, observed_at,
                            sync_run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            account_id,
                            position.fund_code,
                            position.fund_name,
                            position.share_class,
                            str(position.shares),
                            _as_text(position.formal_nav),
                            _as_text(position.estimated_nav),
                            _as_text(position.observed_profit),
                            position.observed_at.isoformat(),
                            sync_run_id,
                        ),
                    )
                    position_count += 1

            active_connection.execute(
                """
                INSERT INTO portfolio_observation_snapshots(
                    sync_run_id, observed_at, account_count, position_count
                ) VALUES (?, ?, ?, ?)
                """,
                (sync_run_id, snapshot_time.isoformat(), len(observations), position_count),
            )
            updated = active_connection.execute(
                """
                UPDATE sync_runs
                SET status = 'success', finished_at = ?, error_code = NULL, error_message = NULL
                WHERE id = ?
                """,
                (finished_at, sync_run_id),
            )
            if updated.rowcount != 1:
                raise ValueError("portfolio sync run could not be completed")

        if connection is not None:
            write(connection)
            return
        with self.connect() as owned_connection, owned_connection:
            write(owned_connection)

    def fail_sync(self, sync_run_id: int, error_code: str, error_message: str) -> None:
        with self.connect() as connection, connection:
            connection.execute(
                """
                UPDATE sync_runs
                SET status = 'failed', finished_at = ?, error_code = ?, error_message = ?
                WHERE id = ?
                """,
                (_utc_now().isoformat(), error_code, error_message, sync_run_id),
            )

    def latest_positions(self) -> List[StoredPosition]:
        with self.connect() as connection:
            snapshot = connection.execute(
                """
                SELECT snapshots.sync_run_id
                FROM portfolio_observation_snapshots snapshots
                JOIN sync_runs ON sync_runs.id = snapshots.sync_run_id
                WHERE sync_runs.source = 'yangjibao' AND sync_runs.status = 'success'
                ORDER BY snapshots.observed_at DESC, snapshots.sync_run_id DESC
                LIMIT 1
                """
            ).fetchone()
            if snapshot is None:
                rows = connection.execute(
                    """
                    SELECT a.title AS account_title, p.*
                    FROM positions p
                    JOIN accounts a ON a.id = p.account_id
                    WHERE p.observed_at = (
                        SELECT MAX(p2.observed_at)
                        FROM positions p2 WHERE p2.account_id = p.account_id
                    )
                    ORDER BY a.title, p.fund_code
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT snapshot_accounts.account_title AS account_title, p.*
                    FROM positions p
                    JOIN portfolio_observation_accounts snapshot_accounts
                      ON snapshot_accounts.sync_run_id = p.sync_run_id
                     AND snapshot_accounts.account_id = p.account_id
                    WHERE p.sync_run_id = ?
                    ORDER BY snapshot_accounts.account_title, p.fund_code
                    """,
                    (int(snapshot["sync_run_id"]),),
                ).fetchall()
        return [
            StoredPosition(
                account_title=str(row["account_title"]),
                fund_code=str(row["fund_code"]),
                fund_name=str(row["fund_name"]),
                share_class=row["share_class"],
                shares=Decimal(str(row["shares"])),
                formal_nav=_as_decimal(row["formal_nav"]),
                estimated_nav=_as_decimal(row["estimated_nav"]),
                observed_profit=_as_decimal(row["observed_profit"]),
                observed_at=datetime.fromisoformat(str(row["observed_at"])),
            )
            for row in rows
        ]

    def latest_successful_sync(self, source: str) -> Optional[Dict[str, str]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sync_runs
                WHERE source = ? AND status = 'success'
                ORDER BY id DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
        return None if row is None else dict(row)

    def latest_raw_snapshot(self) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM raw_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else str(row["payload_json"])

    def replace_snapshot(
        self,
        account: AccountObservation,
        positions: Iterable[PositionObservation],
    ) -> None:
        sync_run_id = self.begin_sync(account.source, "test")
        try:
            self.commit_sync(sync_run_id, [], [(account, list(positions))])
        except Exception:
            self.fail_sync(sync_run_id, "validation_error", "snapshot validation failed")
            raise

    def save_fund_history(
        self,
        fund_code: str,
        fund_name: Optional[str],
        fund_type: Optional[str],
        source: str,
        observations: Sequence[FundNavObservation],
        *,
        connection: Optional[sqlite3.Connection] = None,
    ) -> None:
        if connection is not None and type(connection) is not sqlite3.Connection:
            raise ValueError("connection must be an exact sqlite3.Connection or None")
        if any(item.source_attempt_id is not None for item in observations):
            raise ValueError("generic NAV history writes must remain unbound")
        self._save_fund_history(
            fund_code,
            fund_name,
            fund_type,
            source,
            observations,
            source_attempt_id=None,
            connection=connection,
        )

    def save_authenticated_fund_history(
        self,
        fund_code: str,
        fund_name: Optional[str],
        fund_type: Optional[str],
        source: str,
        observations: Sequence[FundNavObservation],
        *,
        source_attempt_id: int,
        connection: sqlite3.Connection,
    ) -> None:
        if type(connection) is not sqlite3.Connection:
            raise ValueError("authenticated NAV write requires an exact connection")
        if type(source_attempt_id) is not int or source_attempt_id <= 0:
            raise ValueError("source attempt id must be a positive exact integer")
        if source != "eastmoney":
            raise ValueError("authenticated NAV write requires eastmoney source")
        if any(item.source_attempt_id is not None for item in observations):
            raise ValueError("input NAV observations must not carry persistence bindings")
        if not observations:
            raise ValueError("authenticated NAV write requires observations")
        latest = max(observations, key=lambda item: item.nav_date)
        retrieval_times = {item.retrieved_at for item in observations}
        if len(retrieval_times) != 1:
            raise ValueError("authenticated NAV batch must share one retrieval time")
        retrieved_at = next(iter(retrieval_times))
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("authenticated NAV retrieval time must be aware")
        retrieved_at = retrieved_at.astimezone(timezone.utc)
        expected_data_as_of = datetime.combine(
            latest.nav_date,
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).isoformat()
        attempt = connection.execute(
            """
            SELECT * FROM source_attempts
            WHERE id = ?
              AND source_id = 'eastmoney_nav'
              AND field_id = 'formal_nav'
              AND subject_key = ?
              AND outcome = 'success'
              AND data_as_of = ?
              AND started_at COLLATE BINARY <= ? COLLATE BINARY
              AND finished_at COLLATE BINARY >= ? COLLATE BINARY
            """,
            (
                source_attempt_id,
                f"fund:{fund_code}",
                expected_data_as_of,
                retrieved_at.isoformat(),
                retrieved_at.isoformat(),
            ),
        ).fetchone()
        if attempt is None:
            raise ValueError("NAV source attempt binding is invalid")
        self._save_fund_history(
            fund_code,
            fund_name,
            fund_type,
            source,
            observations,
            source_attempt_id=source_attempt_id,
            connection=connection,
        )

    def _save_fund_history(
        self,
        fund_code: str,
        fund_name: Optional[str],
        fund_type: Optional[str],
        source: str,
        observations: Sequence[FundNavObservation],
        *,
        source_attempt_id: Optional[int],
        connection: Optional[sqlite3.Connection],
    ) -> None:
        for observation in observations:
            observation.validate()
            if observation.fund_code != fund_code:
                raise ValueError("NAV fund code does not match requested fund")
        observed_at = max(
            (item.retrieved_at for item in observations),
            default=_utc_now(),
        ).isoformat()

        def write(active_connection: sqlite3.Connection) -> None:
            active_connection.execute(
                """
                INSERT INTO funds(fund_code, fund_name, fund_type, source, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(fund_code) DO UPDATE SET
                    fund_name = COALESCE(excluded.fund_name, funds.fund_name),
                    fund_type = COALESCE(excluded.fund_type, funds.fund_type),
                    source = excluded.source,
                    observed_at = excluded.observed_at
                """,
                (fund_code, fund_name, fund_type, source, observed_at),
            )
            for item in observations:
                active_connection.execute(
                    """
                    INSERT INTO fund_nav(
                        fund_code, nav_date, unit_nav, accumulated_nav,
                        daily_growth, source, retrieved_at,
                        corporate_action_state, source_attempt_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(fund_code, nav_date, source) DO UPDATE SET
                        unit_nav = excluded.unit_nav,
                        accumulated_nav = excluded.accumulated_nav,
                        daily_growth = excluded.daily_growth,
                        retrieved_at = excluded.retrieved_at,
                        corporate_action_state = excluded.corporate_action_state,
                        source_attempt_id = excluded.source_attempt_id
                    """,
                    (
                        item.fund_code,
                        item.nav_date.isoformat(),
                        str(item.unit_nav),
                        _as_text(item.accumulated_nav),
                        _as_text(item.daily_growth),
                        item.source,
                        item.retrieved_at.isoformat(),
                        item.corporate_action_state,
                        source_attempt_id,
                    ),
                )

        if connection is not None:
            write(connection)
            return
        with self.connect() as owned_connection, owned_connection:
            write(owned_connection)

    def fund_history(self, fund_code: str) -> List[FundNavObservation]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM fund_nav WHERE fund_code = ? ORDER BY nav_date",
                (fund_code,),
            ).fetchall()
        from datetime import date

        return [
            FundNavObservation(
                fund_code=str(row["fund_code"]),
                nav_date=date.fromisoformat(str(row["nav_date"])),
                unit_nav=Decimal(str(row["unit_nav"])),
                accumulated_nav=_as_decimal(row["accumulated_nav"]),
                daily_growth=_as_decimal(row["daily_growth"]),
                source=str(row["source"]),
                retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
                corporate_action_state=str(row["corporate_action_state"]),
                source_attempt_id=(
                    None if row["source_attempt_id"] is None else int(row["source_attempt_id"])
                ),
            )
            for row in rows
        ]

    def fund_profile(self, fund_code: str) -> Optional[Dict[str, Optional[str]]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM funds WHERE fund_code = ?",
                (fund_code,),
            ).fetchone()
        return None if row is None else dict(row)

    def save_sector_snapshots(self, observations: Sequence[SectorObservation]) -> None:
        for observation in observations:
            observation.validate()
        with self.connect() as connection, connection:
            for item in observations:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO sector_snapshots(
                        sector_code, sector_name, sector_kind, pct_change,
                        turnover_rate, advancers, decliners, source, retrieved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.sector_code,
                        item.sector_name,
                        item.sector_kind,
                        _as_text(item.pct_change),
                        _as_text(item.turnover_rate),
                        item.advancers,
                        item.decliners,
                        item.source,
                        item.retrieved_at.isoformat(),
                    ),
                )

    def latest_sector_snapshots(self) -> List[SectorObservation]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sector_snapshots
                WHERE retrieved_at = (SELECT MAX(retrieved_at) FROM sector_snapshots)
                ORDER BY CAST(pct_change AS REAL) DESC, sector_name
                """
            ).fetchall()
        return [
            SectorObservation(
                sector_code=str(row["sector_code"]),
                sector_name=str(row["sector_name"]),
                sector_kind=str(row["sector_kind"]),
                pct_change=_as_decimal(row["pct_change"]),
                turnover_rate=_as_decimal(row["turnover_rate"]),
                advancers=row["advancers"],
                decliners=row["decliners"],
                source=str(row["source"]),
                retrieved_at=datetime.fromisoformat(str(row["retrieved_at"])),
            )
            for row in rows
        ]

    def add_thesis(self, thesis: InvestmentThesis) -> int:
        thesis.validate()
        with self.connect() as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO investment_theses(
                    fund_code, rationale, horizon, invalidation, created_at, active
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    thesis.fund_code,
                    thesis.rationale,
                    thesis.horizon,
                    thesis.invalidation,
                    thesis.created_at.isoformat(),
                    1 if thesis.active else 0,
                ),
            )
            return int(cursor.lastrowid)

    def get_thesis(self, thesis_id: int) -> Optional[InvestmentThesis]:
        if type(thesis_id) is not int or thesis_id <= 0:
            raise ValueError("thesis id must be a positive integer")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM investment_theses WHERE id = ?",
                (thesis_id,),
            ).fetchone()
        if row is None:
            return None
        thesis = InvestmentThesis(
            fund_code=str(row["fund_code"]),
            rationale=str(row["rationale"]),
            horizon=str(row["horizon"]),
            invalidation=str(row["invalidation"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            active=bool(row["active"]),
        )
        thesis.validate()
        return thesis

    def latest_active_thesis(
        self,
        fund_code: str,
    ) -> Optional[Tuple[int, InvestmentThesis]]:
        if (
            type(fund_code) is not str
            or len(fund_code) != 6
            or not fund_code.isascii()
            or not fund_code.isdigit()
        ):
            raise ValueError("fund code must be exactly six ASCII digits")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM investment_theses
                WHERE fund_code = ? AND active = 1
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (fund_code,),
            ).fetchone()
        if row is None:
            return None
        thesis_id = int(row["id"])
        if thesis_id <= 0:
            raise ValueError("stored thesis id must be a positive integer")
        thesis = InvestmentThesis(
            fund_code=str(row["fund_code"]),
            rationale=str(row["rationale"]),
            horizon=str(row["horizon"]),
            invalidation=str(row["invalidation"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            active=bool(row["active"]),
        )
        thesis.validate()
        if thesis.fund_code != fund_code or not thesis.active:
            raise ValueError("stored active thesis binding is invalid")
        return thesis_id, thesis

    def list_theses(self, fund_code: Optional[str] = None) -> List[InvestmentThesis]:
        query = "SELECT * FROM investment_theses"
        parameters: Tuple[str, ...] = ()
        if fund_code is not None:
            query += " WHERE fund_code = ?"
            parameters = (fund_code,)
        query += " ORDER BY created_at DESC, id DESC"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            InvestmentThesis(
                fund_code=str(row["fund_code"]),
                rationale=str(row["rationale"]),
                horizon=str(row["horizon"]),
                invalidation=str(row["invalidation"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                active=bool(row["active"]),
            )
            for row in rows
        ]
