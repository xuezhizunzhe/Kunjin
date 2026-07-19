from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Mapping, Sequence

from kunjin.decision.health import SourceHealthService, SourceStatusSnapshot
from kunjin.decision.models import (
    ActionKind,
    FreshnessContext,
    RequestFieldResolution,
    RiskEffect,
    SourceFieldRef,
    SourceFieldState,
    SourceTier,
)
from kunjin.funds.research import build_disclosure_report
from kunjin.funds.risk.models import EvidenceStatus, FundRiskClassification
from kunjin.funds.risk.policy import ClassificationPolicyV1
from kunjin.funds.store import FundDisclosureStore
from kunjin.selection.models import PersonalGateEvidence, validate_candidate_codes
from kunjin.selection.policy import ShortlistPolicyV1, personal_gate_passes
from kunjin.selection.research import public_personal_gate_payload
from kunjin.selection.service import project_personal_gate
from kunjin.storage.repository import Repository

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,127}$", flags=re.ASCII)
_FUND_CODE = re.compile(r"^[0-9]{6}$", flags=re.ASCII)
_SOURCE_FIELDS = (
    "adjusted_return_series",
    "current_manager_team",
    "fees_share_class_relationship",
    "formal_nav",
    "holdings_industries",
    "identity_active_status",
)
_SOURCE_BLOCKING_STATES = frozenset(
    {
        SourceFieldState.COOLDOWN.value,
        SourceFieldState.UNAVAILABLE.value,
        SourceFieldState.UNSUPPORTED.value,
    }
)
_PRIVATE_KEYS = frozenset(
    {
        "account_title",
        "amount",
        "asset",
        "cost",
        "debt",
        "income",
        "monthly_income",
        "portfolio_weight",
        "profit",
        "profile_id",
        "profile_version_id",
        "reserve",
        "shares",
        "total_value",
        "weight",
    }
)
_COMPONENT_NAMES = (
    "source_health",
    "profile",
    "formal_nav",
    "holdings",
    "d1",
    "portfolio_binding",
    "shortlist_entry",
)
REFRESH_COMMANDS = (
    ("formal_nav", "sync fund {code}"),
    ("profile", "sync fund-profile {code} --mode rapid"),
    ("holdings", "sync fund-holdings {code} --mode rapid"),
    ("d1_documents", "sync fund-documents {code}"),
    ("d1_classification", "fund classify {code}"),
)
_COMMAND_ORDER = {kind: index for index, (kind, _template) in enumerate(REFRESH_COMMANDS)}


def _canonical_utc(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _exact_dataclass(value: object, expected: type, name: str) -> None:
    if type(value) is not expected or set(vars(value)) != {
        item.name for item in fields(expected)
    }:
        raise ValueError(f"{name} must be an exact {expected.__name__}")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple((str(key), _freeze(item)) for key, item in sorted(value.items()))
    if type(value) in {list, tuple}:
        return tuple(_freeze(item) for item in value)
    if isinstance(value, Enum):
        return value.value
    return value


def _thaw(value: object) -> object:
    if type(value) is tuple:
        if value and all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            for item in value
        ):
            return {item[0]: _thaw(item[1]) for item in value}
        return [_thaw(item) for item in value]
    if type(value) is Decimal:
        return format(value, "f")
    if type(value) in {date, datetime}:
        return value.isoformat()
    return value


def _validate_dynamic(value: object, path: tuple[str, ...] = ()) -> None:
    if value is None or type(value) in {bool, int, str, date}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("readiness values must be finite")
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("readiness values must be finite")
        return
    if type(value) is datetime:
        _canonical_utc(value, "readiness datetime")
        return
    if type(value) is tuple:
        mapping_shape = bool(value) and all(
            type(item) is tuple
            and len(item) == 2
            and type(item[0]) is str
            for item in value
        )
        if mapping_shape:
            keys = tuple(item[0] for item in value)
            if keys != tuple(sorted(set(keys))):
                raise ValueError("readiness mapping keys must be unique and sorted")
            for key, item in value:
                if key.casefold() in _PRIVATE_KEYS and key not in {"amount_min", "amount_max"}:
                    raise ValueError("readiness values contain a private field")
                _validate_dynamic(item, (*path, key))
            return
        for item in value:
            _validate_dynamic(item, path)
        return
    location = ".".join(path) or "payload"
    raise ValueError(f"unsupported readiness value at {location}")


def _payload(value: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    frozen = _freeze(value)
    if type(frozen) is not tuple:
        raise ValueError("readiness component must be a mapping")
    _validate_dynamic(frozen)
    return frozen


@dataclass(frozen=True)
class CandidateReadinessEvidence:
    fund_code: str
    source_health: tuple[tuple[str, object], ...]
    profile: tuple[tuple[str, object], ...]
    formal_nav: tuple[tuple[str, object], ...]
    holdings: tuple[tuple[str, object], ...]
    d1: tuple[tuple[str, object], ...]
    portfolio_binding: tuple[tuple[str, object], ...]
    shortlist_entry: tuple[tuple[str, object], ...]

    def validate(self) -> None:
        _exact_dataclass(self, CandidateReadinessEvidence, "candidate readiness evidence")
        if (
            type(self.fund_code) is not str
            or _FUND_CODE.fullmatch(self.fund_code) is None
            or self.fund_code == "000000"
        ):
            raise ValueError("candidate readiness fund code is invalid")
        for name in _COMPONENT_NAMES:
            value = getattr(self, name)
            if type(value) is not tuple:
                raise ValueError(f"candidate {name} must be an exact tuple payload")
            _validate_dynamic(value, (name,))


@dataclass(frozen=True)
class ShortlistReadinessResult:
    as_of: datetime
    candidate_codes: tuple[str, ...]
    personal_gate: PersonalGateEvidence
    candidate_evidence: tuple[CandidateReadinessEvidence, ...]
    comparison_evidence_ready: bool
    conditional_shortlist_gate_ready: bool
    blocking_codes: tuple[str, ...]
    bounded_refresh_actions: tuple[tuple[str, str], ...]
    manual_supplementation: tuple[tuple[str, str], ...]
    action_maturity: str = "evidence_only"
    action_authorized: bool = False
    exact_amount_available: bool = False
    automatic_trade: bool = False

    def validate(self) -> None:
        _exact_dataclass(self, ShortlistReadinessResult, "shortlist readiness result")
        if _canonical_utc(self.as_of, "readiness as-of") != self.as_of:
            raise ValueError("readiness as-of must use canonical UTC")
        codes = validate_candidate_codes(self.candidate_codes)
        if type(self.candidate_codes) is not tuple:
            raise ValueError("candidate codes must be an exact tuple")
        if type(self.personal_gate) is not PersonalGateEvidence:
            raise ValueError("personal gate must be exact")
        self.personal_gate.validate()
        if type(self.candidate_evidence) is not tuple:
            raise ValueError("candidate evidence must be an exact tuple")
        for item in self.candidate_evidence:
            if type(item) is not CandidateReadinessEvidence:
                raise ValueError("candidate evidence must contain exact records")
            item.validate()
        if tuple(item.fund_code for item in self.candidate_evidence) != codes:
            raise ValueError("candidate evidence must close in request order")
        for name in ("comparison_evidence_ready", "conditional_shortlist_gate_ready"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be an exact boolean")
        if (
            type(self.blocking_codes) is not tuple
            or self.blocking_codes != tuple(sorted(set(self.blocking_codes)))
            or any(
                type(code) is not str or _IDENTIFIER.fullmatch(code) is None
                for code in self.blocking_codes
            )
        ):
            raise ValueError("blocking codes must be unique ascending identifiers")
        self._validate_actions(codes)
        self._validate_manual_supplementation(codes)
        if (
            self.action_maturity != "evidence_only"
            or self.action_authorized is not False
            or self.exact_amount_available is not False
            or self.automatic_trade is not False
        ):
            raise ValueError("readiness action boundary is fixed")

    def _validate_actions(self, codes: tuple[str, ...]) -> None:
        if type(self.bounded_refresh_actions) is not tuple:
            raise ValueError("bounded refresh actions must be an exact tuple")
        seen: set[tuple[str, str]] = set()
        previous_code_index = -1
        previous_action_index = -1
        for item in self.bounded_refresh_actions:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("bounded refresh actions must contain exact pairs")
            code, command = item
            if type(code) is not str or code not in codes or type(command) is not str:
                raise ValueError("bounded refresh action identity is invalid")
            kinds = tuple(
                kind
                for kind, template in REFRESH_COMMANDS
                if command == template.format(code=code)
            )
            if len(kinds) != 1 or "--force" in command:
                raise ValueError("bounded refresh command is outside the exact grammar")
            key = (code, kinds[0])
            if key in seen:
                raise ValueError("bounded refresh action types must be unique per candidate")
            seen.add(key)
            code_index = codes.index(code)
            action_index = _COMMAND_ORDER[kinds[0]]
            if code_index < previous_code_index or (
                code_index == previous_code_index and action_index <= previous_action_index
            ):
                raise ValueError("bounded refresh actions violate dependency order")
            if code_index != previous_code_index:
                previous_action_index = -1
            previous_code_index = code_index
            previous_action_index = action_index

    def _validate_manual_supplementation(self, codes: tuple[str, ...]) -> None:
        if type(self.manual_supplementation) is not tuple:
            raise ValueError("manual supplementation must be an exact tuple")
        if len(self.manual_supplementation) != len(set(self.manual_supplementation)):
            raise ValueError("manual supplementation must be unique")
        for item in self.manual_supplementation:
            if type(item) is not tuple or len(item) != 2:
                raise ValueError("manual supplementation must contain exact pairs")
            code, missing_item = item
            if (
                code not in codes
                or type(missing_item) is not str
                or _IDENTIFIER.fullmatch(missing_item) is None
            ):
                raise ValueError("manual supplementation identity is invalid")


class ShortlistReadinessService:
    def __init__(
        self,
        repository: Repository,
        disclosure_store: FundDisclosureStore,
        *,
        source_health_service: SourceHealthService,
        classification_loader: Callable[[str], FundRiskClassification | None],
        suitability_status_loader: Callable[[], Mapping[str, object]],
        allocation_status_loader: Callable[[], Mapping[str, object]],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if type(repository) is not Repository:
            raise ValueError("repository must be an exact Repository")
        if type(disclosure_store) is not FundDisclosureStore:
            raise ValueError("disclosure store must be an exact FundDisclosureStore")
        if disclosure_store.repository is not repository:
            raise ValueError("disclosure store must share the exact repository")
        if type(source_health_service) is not SourceHealthService:
            raise ValueError("source health service must be exact")
        if source_health_service.audit_store.repository is not repository:
            raise ValueError("source health service must share the exact repository")
        for value, name in (
            (classification_loader, "classification loader"),
            (suitability_status_loader, "suitability status loader"),
            (allocation_status_loader, "allocation status loader"),
            (clock, "clock"),
        ):
            if not callable(value):
                raise ValueError(f"{name} must be callable")
        self.repository = repository
        self.disclosure_store = disclosure_store
        self.source_health_service = source_health_service
        self.classification_loader = classification_loader
        self.suitability_status_loader = suitability_status_loader
        self.allocation_status_loader = allocation_status_loader
        self.clock = clock
        self._source_primary_references, self._source_requirements = (
            self._source_request_contract()
        )

    def review(self, candidate_codes: Sequence[str]) -> ShortlistReadinessResult:
        codes = validate_candidate_codes(candidate_codes)
        as_of = _canonical_utc(self.clock(), "readiness clock")
        positions, position_failed = self._load_positions()
        personal_gate = self._load_personal_gate()
        held_codes = {
            position.fund_code
            for position in positions
            if type(getattr(position, "fund_code", None)) is str
        }

        candidates = []
        private_states = []
        for code in codes:
            candidate, state = self._review_candidate(
                code,
                as_of,
                held_codes,
                position_failed,
                personal_gate,
            )
            candidates.append(candidate)
            private_states.append(state)

        common_dates = self._common_nav_dates(private_states)
        comparison_ready = len(common_dates) >= 2 and all(
            bool(state["base_comparison_ready"]) for state in private_states
        )
        actions, supplementation = self._refresh_projection(
            codes,
            private_states,
            common_dates_ready=len(common_dates) >= 2,
        )
        blocking_codes = set()
        for state in private_states:
            blocking_codes.update(state["blocking_codes"])
        if len(common_dates) < 2:
            blocking_codes.add("common_formal_nav_dates_missing")
        if position_failed:
            blocking_codes.add("portfolio_binding_load_failed")
        blocking_codes.update(personal_gate.blocking_codes)
        if not personal_gate_passes(personal_gate):
            blocking_codes.add("personal_gate_not_ready")
        result = ShortlistReadinessResult(
            as_of=as_of,
            candidate_codes=codes,
            personal_gate=personal_gate,
            candidate_evidence=tuple(candidates),
            comparison_evidence_ready=comparison_ready,
            conditional_shortlist_gate_ready=personal_gate_passes(personal_gate),
            blocking_codes=tuple(sorted(blocking_codes)),
            bounded_refresh_actions=actions,
            manual_supplementation=supplementation,
        )
        result.validate()
        return result

    def _load_positions(self) -> tuple[tuple[object, ...], bool]:
        try:
            return tuple(self.repository.latest_positions()), False
        except Exception:
            return (), True

    def _load_personal_gate(self) -> PersonalGateEvidence:
        try:
            suitability = self.suitability_status_loader()
            if not isinstance(suitability, Mapping):
                raise ValueError("suitability status must be a mapping")
        except Exception:
            suitability = {"state": "transient", "freshness": "transient"}
        try:
            allocation = self.allocation_status_loader()
            if not isinstance(allocation, Mapping):
                raise ValueError("allocation status must be a mapping")
        except Exception:
            allocation = {"state": "transient", "freshness": "transient"}
        return project_personal_gate(suitability, allocation)

    def _review_candidate(
        self,
        code: str,
        as_of: datetime,
        held_codes: set[str],
        position_failed: bool,
        personal_gate: PersonalGateEvidence,
    ) -> tuple[CandidateReadinessEvidence, dict[str, object]]:
        failures = set()
        bundle = None
        report = None
        try:
            bundle = self.disclosure_store.load_bundle(code)
            bundle.validate()
            report = build_disclosure_report(bundle, as_of)
        except Exception:
            failures.add("profile")
        try:
            history = tuple(self.repository.fund_history(code))
            for item in history:
                item.validate()
                if item.fund_code != code:
                    raise ValueError("NAV history fund code mismatch")
        except Exception:
            history = ()
            failures.add("formal_nav")
        try:
            classification = self.classification_loader(code)
            if classification is not None:
                if type(classification) is not FundRiskClassification:
                    raise ValueError("classification loader returned an invalid value")
                classification.validate()
                if classification.fund_code != code:
                    raise ValueError("classification fund code mismatch")
        except Exception:
            classification = None
            failures.add("d1")
        try:
            source_snapshot = self.source_health_service.stored_source_status_snapshot(
                f"fund:{code}",
                FreshnessContext(now=as_of),
                self._source_primary_references,
                self._source_requirements,
            )
            source_snapshot.validate()
        except Exception:
            source_snapshot = None
            failures.add("source_health")

        source_payload, source_state = self._source_payload(source_snapshot)
        profile_payload, profile_ready = self._profile_payload(report, bundle)
        nav_payload, nav_dates, nav_ready = self._nav_payload(history, failures, as_of)
        holdings_payload, holdings_ready = self._holdings_payload(report, failures)
        d1_payload, d1_ready, d1_documents_ready, mapped_layer = self._d1_payload(
            classification,
            bundle,
            as_of,
            failures,
        )
        position_state = "held" if code in held_codes else "not_held"
        portfolio_payload = _payload(
            {
                "position_state": position_state,
                "technical_failure": (
                    "portfolio_binding_load_failed" if position_failed else None
                ),
            }
        )
        source_ready = all(
            source_state["resolutions"].get(field_id) == RequestFieldResolution.USABLE.value
            for field_id in (
                "identity_active_status",
                "current_manager_team",
                "fees_share_class_relationship",
                "formal_nav",
                "holdings_industries",
            )
        )
        base_ready = (
            not failures
            and profile_ready
            and nav_ready
            and holdings_ready
            and d1_ready
            and source_ready
            and not position_failed
        )
        shortlist_payload = _payload(
            {
                "d1_conflict_free": bool(
                    classification is not None and not classification.conflicts
                ),
                "d1_current": d1_ready,
                "d1_evidence_verified": bool(
                    classification is not None
                    and classification.evidence_status is EvidenceStatus.VERIFIED
                ),
                "mapped_asset_layer": mapped_layer,
                "personal_gate_passes": personal_gate_passes(personal_gate),
                "portfolio_role_eligible": bool(
                    classification is not None
                    and classification.portfolio_role.value != "not_eligible"
                ),
                "position_state": position_state,
            }
        )
        candidate = CandidateReadinessEvidence(
            fund_code=code,
            source_health=source_payload,
            profile=profile_payload,
            formal_nav=nav_payload,
            holdings=holdings_payload,
            d1=d1_payload,
            portfolio_binding=portfolio_payload,
            shortlist_entry=shortlist_payload,
        )
        candidate.validate()
        blocking = set()
        if failures:
            blocking.add("candidate_component_load_failed")
        if not profile_ready:
            blocking.add("profile_evidence_not_ready")
        if not nav_ready:
            blocking.add("formal_nav_not_ready")
        if not holdings_ready:
            blocking.add("holdings_evidence_not_ready")
        if not d1_ready:
            blocking.add("d1_classification_not_ready")
        if not source_ready:
            blocking.add("source_health_not_ready")
        return candidate, {
            "base_comparison_ready": base_ready,
            "blocking_codes": blocking,
            "d1_documents_ready": d1_documents_ready,
            "d1_ready": d1_ready,
            "failures": failures,
            "holdings_ready": holdings_ready,
            "nav_dates": nav_dates,
            "nav_ready": nav_ready,
            "profile_ready": profile_ready,
            "source": source_state,
        }

    def _source_request_contract(
        self,
    ) -> tuple[tuple[SourceFieldRef, ...], tuple[object, ...]]:
        tier_priority = {
            SourceTier.TIER_1: 0,
            SourceTier.TIER_2: 1,
            SourceTier.PRIVATE_OBSERVATION: 2,
            SourceTier.USER_PROVIDED: 3,
        }
        primary_by_field: dict[str, tuple[int, str]] = {}
        for source in self.source_health_service.registry.sources:
            for field in source.fields:
                if field.field_id not in _SOURCE_FIELDS:
                    continue
                candidate = (tier_priority[field.source_tier], source.source_id)
                if (
                    field.field_id not in primary_by_field
                    or candidate < primary_by_field[field.field_id]
                ):
                    primary_by_field[field.field_id] = candidate
        if set(primary_by_field) != set(_SOURCE_FIELDS):
            raise ValueError("source registry lacks a readiness field")
        references = tuple(
            SourceFieldRef(primary_by_field[field_id][1], field_id)
            for field_id in _SOURCE_FIELDS
        )
        requirements = tuple(
            self.source_health_service.action_requirement(
                field_id,
                ActionKind.FACT_RESEARCH,
                RiskEffect.INFORMATION,
            )
            for field_id in _SOURCE_FIELDS
        )
        return references, requirements

    def _source_payload(
        self,
        snapshot: SourceStatusSnapshot | None,
    ) -> tuple[tuple[tuple[str, object], ...], dict[str, object]]:
        if snapshot is None:
            return _payload({"technical_failure": "source_status_load_failed"}), {
                "blocked_fields": set(_SOURCE_FIELDS),
                "resolutions": {},
            }
        projected = {
            (item.history.reference.source_id, item.history.reference.field_id): item.state.value
            for item in snapshot.projections
        }
        fields_payload = []
        resolutions: dict[str, str] = {}
        blocked_fields = set()
        for reference, resolution in zip(
            self._source_primary_references,
            snapshot.resolutions,
        ):
            policy = next(
                field
                for source in self.source_health_service.registry.sources
                if source.source_id == reference.source_id
                for field in source.fields
                if field.field_id == reference.field_id
            )
            states = [
                {
                    "field_id": item.field_id,
                    "source_id": item.source_id,
                    "state": projected[(item.source_id, item.field_id)],
                }
                for item in (reference, *policy.acceptable_alternatives)
            ]
            resolutions[reference.field_id] = resolution.value
            if resolution is RequestFieldResolution.MANUAL_SUPPLEMENT_REQUIRED or (
                resolution is RequestFieldResolution.PARTIAL
                and any(item["state"] in _SOURCE_BLOCKING_STATES for item in states)
            ):
                blocked_fields.add(reference.field_id)
            fields_payload.append(
                {
                    "acceptable_sources": states,
                    "field_id": reference.field_id,
                    "resolution": resolution.value,
                }
            )
        return _payload(
            {
                "evaluated_at": snapshot.evaluated_at,
                "fields": fields_payload,
            }
        ), {"blocked_fields": blocked_fields, "resolutions": resolutions}

    @staticmethod
    def _profile_payload(report, bundle) -> tuple[tuple[tuple[str, object], ...], bool]:
        if report is None or bundle is None:
            return _payload({"technical_failure": "profile_load_failed"}), False
        identity = report["identity"]
        managers = report["managers"]
        fees = report["fees"]
        benchmarks = report["benchmarks"]
        authenticated = all(
            (
                identity is not None
                and identity.get("status") == "active"
                and identity.get("source_document_id") in bundle.source_documents,
                bool(managers["current"])
                and bool(managers["source_document_ids"])
                and all(
                    item in bundle.source_documents
                    for item in managers["source_document_ids"]
                ),
                bool(fees["rules"])
                and bool(fees["source_document_ids"])
                and all(item in bundle.source_documents for item in fees["source_document_ids"]),
                bool(benchmarks["items"])
                and bool(benchmarks["source_document_ids"])
                and all(
                    item in bundle.source_documents
                    for item in benchmarks["source_document_ids"]
                ),
            )
        )
        value = {
            "authenticated": authenticated,
            "benchmarks": benchmarks,
            "conflicts": report["conflicts"],
            "fees": fees,
            "freshness": report["freshness"],
            "identity": identity,
            "managers": managers,
            "publication_dates": report["publication_dates"],
            "report_dates": report["report_dates"],
            "warnings": report["warnings"],
        }
        return _payload(value), bool(authenticated and not report["conflicts"])

    @staticmethod
    def _nav_payload(
        history,
        failures,
        as_of: datetime,
    ) -> tuple[tuple[tuple[str, object], ...], set[date], bool]:
        future_count = sum(item.nav_date > as_of.date() for item in history)
        dates = {item.nav_date for item in history if item.nav_date <= as_of.date()}
        ordered = sorted(dates)
        ready = (
            "formal_nav" not in failures
            and len(ordered) >= 2
            and future_count == 0
        )
        return _payload(
            {
                "end_date": ordered[-1] if ordered else None,
                "future_observation_count": future_count,
                "latest_date": ordered[-1] if ordered else None,
                "observation_count": len(history),
                "start_date": ordered[0] if ordered else None,
                "technical_failure": (
                    "formal_nav_load_failed" if "formal_nav" in failures else None
                ),
                "unique_date_count": len(ordered),
                "usable": ready,
            }
        ), dates, ready

    @staticmethod
    def _holdings_payload(report, failures) -> tuple[tuple[tuple[str, object], ...], bool]:
        if report is None or "profile" in failures:
            return _payload({"technical_failure": "holdings_load_failed"}), False
        holdings = report["holdings"]
        coverage = sum(
            (Decimal(str(item["weight"])) for item in holdings["items"]),
            Decimal("0"),
        )
        ready = (
            holdings["evidence_level"] == "verified_fact"
            and holdings["freshness"] == "current"
            and holdings["report_period"] is not None
            and bool(holdings["source_document_ids"])
        )
        return _payload(
            {
                "conflicts": report["conflicts"],
                "disclosed_coverage": coverage,
                "disclosure_scopes": holdings["disclosure_scopes"],
                "evidence_level": holdings["evidence_level"],
                "freshness": holdings["freshness"],
                "published_at": holdings["published_at"],
                "report_period": holdings["report_period"],
                "source_document_ids": holdings["source_document_ids"],
                "warnings": report["warnings"],
            }
        ), bool(ready and not report["conflicts"])

    @staticmethod
    def _d1_payload(classification, bundle, as_of, failures):
        documents_ready = bool(bundle is not None and bundle.source_documents)
        if classification is None:
            return _payload(
                {
                    "classification_present": False,
                    "technical_failure": (
                        "classification_load_failed" if "d1" in failures else None
                    ),
                }
            ), False, documents_ready, None
        freshness = (
            "current"
            if classification.classified_at <= as_of < classification.valid_until
            else "stale"
        )
        ready = (
            classification.evidence_status is EvidenceStatus.VERIFIED
            and freshness == "current"
            and not classification.conflicts
        )
        mapped_layer, mapping_reason = ShortlistPolicyV1().map_asset_layer(
            evidence_status=classification.evidence_status.value,
            risk_bucket=classification.risk_bucket.value,
            portfolio_role=classification.portfolio_role.value,
        )
        return _payload(
            {
                "classification_policy_checksum": ClassificationPolicyV1().checksum(),
                "classification_present": True,
                "classified_at": classification.classified_at,
                "conflicts": classification.conflicts,
                "evidence_status": classification.evidence_status.value,
                "freshness": freshness,
                "mapped_asset_layer": mapped_layer,
                "mapping_reason_code": mapping_reason,
                "missing_evidence": classification.missing_evidence,
                "policy_version": classification.policy_version,
                "portfolio_role": classification.portfolio_role.value,
                "reason_codes": classification.reason_codes,
                "risk_bucket": classification.risk_bucket.value,
                "valid_until": classification.valid_until,
            }
        ), ready, documents_ready, mapped_layer

    @staticmethod
    def _common_nav_dates(states: list[dict[str, object]]) -> set[date]:
        common = None
        for state in states:
            dates = set(state["nav_dates"])
            common = dates if common is None else common & dates
        return set() if common is None else common

    @staticmethod
    def _refresh_projection(codes, states, *, common_dates_ready):
        actions = []
        supplementation = []
        for code, state in zip(codes, states):
            blocked_fields = set(state["source"]["blocked_fields"])
            resolutions = state["source"]["resolutions"]
            source_failed = "source_health" in state["failures"]
            if source_failed:
                supplementation.append((code, "source_status"))
            gaps = {
                "formal_nav": (
                    not state["nav_ready"]
                    or not common_dates_ready
                    or resolutions.get("formal_nav")
                    != RequestFieldResolution.USABLE.value
                ),
                "profile": (
                    not state["profile_ready"]
                    or any(
                        resolutions.get(field_id)
                        != RequestFieldResolution.USABLE.value
                        for field_id in (
                            "identity_active_status",
                            "current_manager_team",
                            "fees_share_class_relationship",
                        )
                    )
                ),
                "holdings": (
                    not state["holdings_ready"]
                    or resolutions.get("holdings_industries")
                    != RequestFieldResolution.USABLE.value
                ),
                "d1_documents": not state["d1_ready"] and not state["d1_documents_ready"],
                "d1_classification": not state["d1_ready"],
            }
            blocked_by_action = {
                "formal_nav": {"formal_nav", "adjusted_return_series"},
                "profile": {
                    "identity_active_status",
                    "current_manager_team",
                    "fees_share_class_relationship",
                },
                "holdings": {"holdings_industries"},
                "d1_documents": set(),
                "d1_classification": set(),
            }
            for kind, template in REFRESH_COMMANDS:
                if not gaps[kind]:
                    continue
                affected = blocked_by_action[kind] & blocked_fields
                if source_failed:
                    supplementation.append((code, "source_status"))
                elif affected:
                    supplementation.extend((code, field) for field in sorted(affected))
                else:
                    actions.append((code, template.format(code=code)))
        return tuple(actions), tuple(dict.fromkeys(supplementation))


def public_shortlist_readiness_payload(
    result: ShortlistReadinessResult,
) -> dict[str, object]:
    result.validate()
    return {
        "request": {
            "candidate_codes": list(result.candidate_codes),
            "candidate_count": len(result.candidate_codes),
        },
        "personal_gate": public_personal_gate_payload(result.personal_gate),
        "candidate_evidence": [
            {
                "fund_code": item.fund_code,
                **{
                    name: _thaw(getattr(item, name))
                    for name in _COMPONENT_NAMES
                },
            }
            for item in result.candidate_evidence
        ],
        "comparison_evidence_ready": result.comparison_evidence_ready,
        "conditional_shortlist_gate_ready": result.conditional_shortlist_gate_ready,
        "blocking_codes": list(result.blocking_codes),
        "bounded_refresh_actions": [
            {"fund_code": code, "command": command}
            for code, command in result.bounded_refresh_actions
        ],
        "manual_supplementation": [
            {"fund_code": code, "missing_item": item}
            for code, item in result.manual_supplementation
        ],
        "action_boundary": {
            "action_maturity": "evidence_only",
            "action_authorized": False,
            "exact_amount_available": False,
            "automatic_trade": False,
        },
    }


__all__ = [
    "CandidateReadinessEvidence",
    "REFRESH_COMMANDS",
    "ShortlistReadinessResult",
    "ShortlistReadinessService",
    "public_shortlist_readiness_payload",
]
