from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Optional, Tuple, cast

from kunjin.funds.models import FUND_CODE_PATTERN, DocumentKind
from kunjin.funds.risk.audit import candidate_fingerprint, canonical_candidate_payload
from kunjin.funds.risk.documents import (
    OfficialDocumentCandidate,
    validate_safe_https_url,
)

PERIODIC_DOCUMENT_KINDS = (
    DocumentKind.ANNUAL_REPORT,
    DocumentKind.QUARTERLY_REPORT,
    DocumentKind.SEMIANNUAL_REPORT,
)
SELECTION_STATES = frozenset({"selected", "missing", "conflicted"})
SELECTION_REASON_CODES = frozenset(
    {"current_periodic_candidate_missing", "current_periodic_candidate_conflict"}
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PERIODIC_ORDER = {kind: index for index, kind in enumerate(PERIODIC_DOCUMENT_KINDS)}
_SELECTION_POLICY_V1_PAYLOAD = {
    "candidate_identity": "official_document_candidate_fingerprint_v1",
    "manifest_version": 1,
    "nonperiodic_policy": "preserve_discovery_order_v1",
    "periodic_document_kinds": [kind.value for kind in PERIODIC_DOCUMENT_KINDS],
    "selection_key": "published_at",
    "tie_policy": "conflict_on_distinct_newest_fingerprints",
    "version": "current_periodic_selection_v1",
}
SELECTION_POLICY_V1_CHECKSUM = hashlib.sha256(
    json.dumps(
        _SELECTION_POLICY_V1_PAYLOAD,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()


@dataclass(frozen=True)
class SelectionCandidate:
    candidate_fingerprint: str
    document_kind: DocumentKind
    url: str
    published_at: datetime

    def validate(self) -> None:
        _validate_exact_record(self, SelectionCandidate, "selection candidate")
        _validate_sha256(self.candidate_fingerprint, "selection candidate fingerprint")
        if type(self.document_kind) is not DocumentKind or self.document_kind not in (
            PERIODIC_DOCUMENT_KINDS
        ):
            raise ValueError("selection candidate document kind must be periodic")
        if type(self.url) is not str:
            raise ValueError("selection candidate URL must be exact text")
        validate_safe_https_url(self.url)
        if type(self.published_at) is not datetime or self.published_at.tzinfo is not timezone.utc:
            raise ValueError("selection candidate publication time must use canonical UTC")


@dataclass(frozen=True)
class PeriodicSelectionState:
    document_kind: DocumentKind
    state: str
    candidate_fingerprints: Tuple[str, ...]
    selected_fingerprint: Optional[str]
    reason_code: Optional[str]

    def validate(self) -> None:
        _validate_exact_record(self, PeriodicSelectionState, "periodic selection state")
        if type(self.document_kind) is not DocumentKind or self.document_kind not in (
            PERIODIC_DOCUMENT_KINDS
        ):
            raise ValueError("periodic selection state document kind is invalid")
        if type(self.state) is not str or self.state not in SELECTION_STATES:
            raise ValueError("periodic selection state is unknown")
        if type(self.candidate_fingerprints) is not tuple:
            raise ValueError("periodic selection fingerprints must be an immutable tuple")
        for fingerprint in self.candidate_fingerprints:
            _validate_sha256(fingerprint, "periodic selection candidate fingerprint")
        if len(set(self.candidate_fingerprints)) != len(self.candidate_fingerprints):
            raise ValueError("periodic selection candidate fingerprints cannot contain duplicates")
        if self.candidate_fingerprints != tuple(sorted(self.candidate_fingerprints)):
            raise ValueError("periodic selection candidate fingerprints must be sorted")

        if self.state == "selected":
            _validate_sha256(self.selected_fingerprint, "selected fingerprint")
            if self.selected_fingerprint not in self.candidate_fingerprints:
                raise ValueError("selected fingerprint must identify a candidate")
            if self.reason_code is not None:
                raise ValueError("selected periodic state cannot have a reason code")
            return
        if self.selected_fingerprint is not None:
            raise ValueError("unselected periodic state cannot have a selected fingerprint")
        if self.state == "missing":
            if self.candidate_fingerprints:
                raise ValueError("missing periodic state cannot contain candidates")
            if self.reason_code != "current_periodic_candidate_missing":
                raise ValueError("missing periodic state requires the missing reason code")
            return
        if len(self.candidate_fingerprints) < 2:
            raise ValueError("conflicted periodic state requires distinct candidates")
        if self.reason_code != "current_periodic_candidate_conflict":
            raise ValueError("conflicted periodic state requires the conflict reason code")


@dataclass(frozen=True)
class DocumentSelectionPlan:
    fund_code: str
    refresh_run_id: int
    periodic_candidates: Tuple[SelectionCandidate, ...]
    periodic_states: Tuple[PeriodicSelectionState, ...]
    attempted_candidates: Tuple[OfficialDocumentCandidate, ...]
    selection_policy_checksum: str
    canonical_json: str
    selection_checksum: str

    def validate(self) -> None:
        _validate_exact_record(self, DocumentSelectionPlan, "document selection plan")
        _validate_fund_and_refresh(self.fund_code, self.refresh_run_id)
        if type(self.periodic_candidates) is not tuple:
            raise ValueError("periodic selection candidates must be an immutable tuple")
        for item in self.periodic_candidates:
            item.validate()
        expected_candidates = tuple(sorted(self.periodic_candidates, key=_candidate_sort_key))
        if self.periodic_candidates != expected_candidates:
            raise ValueError("periodic selection candidates must be canonically ordered")
        fingerprints = tuple(item.candidate_fingerprint for item in self.periodic_candidates)
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("periodic selection candidate fingerprints cannot contain duplicates")

        if type(self.periodic_states) is not tuple:
            raise ValueError("periodic selection states must be an immutable tuple")
        if tuple(item.document_kind for item in self.periodic_states) != (
            PERIODIC_DOCUMENT_KINDS
        ):
            raise ValueError("periodic selection states must contain every periodic kind")
        for state in self.periodic_states:
            state.validate()
            _validate_state_against_candidates(state, self.periodic_candidates)

        if type(self.attempted_candidates) is not tuple:
            raise ValueError("attempted candidates must be an immutable tuple")
        attempted_fingerprints = []
        selected_by_kind = {
            state.document_kind: state.selected_fingerprint
            for state in self.periodic_states
            if state.state == "selected"
        }
        for item in self.attempted_candidates:
            item.validate()
            if item.fund_code != self.fund_code:
                raise ValueError("attempted candidate fund does not match selection fund")
            fingerprint = candidate_fingerprint(item)
            attempted_fingerprints.append(fingerprint)
            if item.document_kind in PERIODIC_DOCUMENT_KINDS and (
                selected_by_kind.get(item.document_kind) != fingerprint
            ):
                raise ValueError("attempted periodic candidate is not the selected candidate")
        if len(set(attempted_fingerprints)) != len(attempted_fingerprints):
            raise ValueError("attempted candidate fingerprints cannot contain duplicates")
        attempted_periodic = {
            candidate_fingerprint(item)
            for item in self.attempted_candidates
            if item.document_kind in PERIODIC_DOCUMENT_KINDS
        }
        if attempted_periodic != set(selected_by_kind.values()):
            raise ValueError("attempted periodic candidates do not match selection states")

        if self.selection_policy_checksum != SELECTION_POLICY_V1_CHECKSUM:
            raise ValueError("selection policy checksum is unknown")
        if type(self.canonical_json) is not str:
            raise ValueError("selection canonical JSON must be exact text")
        try:
            self.canonical_json.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("selection canonical JSON must be ASCII") from None
        try:
            payload = json.loads(
                self.canonical_json,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
            )
        except (TypeError, ValueError):
            raise ValueError("selection canonical JSON is invalid") from None
        expected_payload = _selection_manifest_payload(
            fund_code=self.fund_code,
            refresh_run_id=self.refresh_run_id,
            periodic_candidates=self.periodic_candidates,
            periodic_states=self.periodic_states,
            selection_policy_checksum=self.selection_policy_checksum,
        )
        if type(payload) is not dict or payload != expected_payload:
            raise ValueError("selection canonical JSON does not match the selection plan")
        if _canonical_json(payload) != self.canonical_json:
            raise ValueError("selection JSON must be canonical")
        _validate_sha256(self.selection_checksum, "selection checksum")
        expected_checksum = hashlib.sha256(self.canonical_json.encode("ascii")).hexdigest()
        if self.selection_checksum != expected_checksum:
            raise ValueError("selection checksum does not match canonical JSON")

    def status_for(self, kind: DocumentKind) -> PeriodicSelectionState:
        if type(kind) is not DocumentKind or kind not in PERIODIC_DOCUMENT_KINDS:
            raise ValueError("selection status requires a periodic document kind")
        return next(item for item in self.periodic_states if item.document_kind is kind)


def select_current_candidates(
    fund_code: str,
    *,
    refresh_run_id: int,
    candidates: Tuple[OfficialDocumentCandidate, ...],
) -> DocumentSelectionPlan:
    _validate_fund_and_refresh(fund_code, refresh_run_id)
    if type(candidates) is not tuple:
        raise ValueError("official document candidates must be an immutable tuple")

    fingerprints_by_candidate = {}
    periodic_records = []
    seen_fingerprints = set()
    for item in candidates:
        payload = canonical_candidate_payload(item)
        if item.fund_code != fund_code:
            raise ValueError("official document candidate fund does not match selection fund")
        fingerprint = candidate_fingerprint(item)
        if fingerprint in seen_fingerprints:
            raise ValueError("official document candidate fingerprints cannot contain duplicates")
        seen_fingerprints.add(fingerprint)
        fingerprints_by_candidate[id(item)] = fingerprint
        if item.document_kind not in PERIODIC_DOCUMENT_KINDS:
            continue
        if item.published_at is None:
            raise ValueError("periodic selection candidate publication time is required")
        record = SelectionCandidate(
            candidate_fingerprint=fingerprint,
            document_kind=DocumentKind(cast(str, payload["document_kind"])),
            url=cast(str, payload["url"]),
            published_at=item.published_at,
        )
        record.validate()
        periodic_records.append(record)

    canonical_candidates = tuple(sorted(periodic_records, key=_candidate_sort_key))
    states = tuple(
        _select_periodic_state(kind, canonical_candidates) for kind in PERIODIC_DOCUMENT_KINDS
    )
    selected_fingerprints = {
        state.document_kind: state.selected_fingerprint
        for state in states
        if state.state == "selected"
    }
    attempted = tuple(
        item
        for item in candidates
        if item.document_kind not in PERIODIC_DOCUMENT_KINDS
        or fingerprints_by_candidate[id(item)] == selected_fingerprints.get(item.document_kind)
    )
    payload = _selection_manifest_payload(
        fund_code=fund_code,
        refresh_run_id=refresh_run_id,
        periodic_candidates=canonical_candidates,
        periodic_states=states,
        selection_policy_checksum=SELECTION_POLICY_V1_CHECKSUM,
    )
    canonical_json = _canonical_json(payload)
    plan = DocumentSelectionPlan(
        fund_code=fund_code,
        refresh_run_id=refresh_run_id,
        periodic_candidates=canonical_candidates,
        periodic_states=states,
        attempted_candidates=attempted,
        selection_policy_checksum=SELECTION_POLICY_V1_CHECKSUM,
        canonical_json=canonical_json,
        selection_checksum=hashlib.sha256(canonical_json.encode("ascii")).hexdigest(),
    )
    plan.validate()
    return plan


def _select_periodic_state(
    kind: DocumentKind,
    candidates: Tuple[SelectionCandidate, ...],
) -> PeriodicSelectionState:
    matching = tuple(item for item in candidates if item.document_kind is kind)
    fingerprints = tuple(sorted(item.candidate_fingerprint for item in matching))
    if not matching:
        state = PeriodicSelectionState(
            document_kind=kind,
            state="missing",
            candidate_fingerprints=(),
            selected_fingerprint=None,
            reason_code="current_periodic_candidate_missing",
        )
    else:
        newest_time = max(item.published_at for item in matching)
        newest = tuple(item for item in matching if item.published_at == newest_time)
        if len(newest) == 1:
            state = PeriodicSelectionState(
                document_kind=kind,
                state="selected",
                candidate_fingerprints=fingerprints,
                selected_fingerprint=newest[0].candidate_fingerprint,
                reason_code=None,
            )
        else:
            state = PeriodicSelectionState(
                document_kind=kind,
                state="conflicted",
                candidate_fingerprints=fingerprints,
                selected_fingerprint=None,
                reason_code="current_periodic_candidate_conflict",
            )
    state.validate()
    return state


def _validate_state_against_candidates(
    state: PeriodicSelectionState,
    candidates: Tuple[SelectionCandidate, ...],
) -> None:
    matching = tuple(item for item in candidates if item.document_kind is state.document_kind)
    fingerprints = tuple(sorted(item.candidate_fingerprint for item in matching))
    if state.candidate_fingerprints != fingerprints:
        raise ValueError("periodic selection state candidates do not match the manifest")
    if not matching:
        if state.state != "missing":
            raise ValueError("periodic selection state must be missing without candidates")
        return
    newest_time = max(item.published_at for item in matching)
    newest = tuple(item for item in matching if item.published_at == newest_time)
    if len(newest) == 1:
        if state.state != "selected" or state.selected_fingerprint != (
            newest[0].candidate_fingerprint
        ):
            raise ValueError(
                "periodic selection state does not identify the unique newest candidate"
            )
        return
    if state.state != "conflicted":
        raise ValueError("periodic selection state must fail closed on a newest-time tie")


def _selection_manifest_payload(
    *,
    fund_code: str,
    refresh_run_id: int,
    periodic_candidates: Tuple[SelectionCandidate, ...],
    periodic_states: Tuple[PeriodicSelectionState, ...],
    selection_policy_checksum: str,
) -> dict[str, object]:
    return {
        "fund_code": fund_code,
        "manifest_version": 1,
        "periodic_candidates": [
            {
                "candidate_fingerprint": item.candidate_fingerprint,
                "document_kind": item.document_kind.value,
                "published_at": item.published_at.isoformat(),
                "url": item.url,
            }
            for item in periodic_candidates
        ],
        "periodic_states": [
            {
                "candidate_fingerprints": list(item.candidate_fingerprints),
                "document_kind": item.document_kind.value,
                "reason_code": item.reason_code,
                "selected_fingerprint": item.selected_fingerprint,
                "state": item.state,
            }
            for item in periodic_states
        ],
        "refresh_run_id": refresh_run_id,
        "selection_policy_checksum": selection_policy_checksum,
    }


def _candidate_sort_key(item: SelectionCandidate) -> tuple[object, ...]:
    return (
        _PERIODIC_ORDER[item.document_kind],
        item.published_at,
        item.url,
        item.candidate_fingerprint,
    )


def _validate_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if type(vars(value)) is not dict or set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


def _validate_sha256(value: object, label: str) -> None:
    if type(value) is not str or not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _validate_fund_and_refresh(fund_code: object, refresh_run_id: object) -> None:
    if type(fund_code) is not str or not FUND_CODE_PATTERN.fullmatch(fund_code):
        raise ValueError("document selection requires a six-digit fund code")
    if type(refresh_run_id) is not int or refresh_run_id <= 0:
        raise ValueError("document selection refresh run ID must be positive")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
