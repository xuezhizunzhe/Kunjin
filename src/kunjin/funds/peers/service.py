from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from kunjin.funds.models import DisclosureBundle
from kunjin.funds.peers.classification import (
    PEER_MEMBER_LIMIT,
    PEER_RULE_VERSION,
    classify_peer,
    ordered_candidates,
)
from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerGroupMember,
    PeerGroupStatus,
    PeerSyncState,
)
from kunjin.funds.peers.sources import (
    PEER_DIRECTORY_REFERER,
    PEER_DIRECTORY_URL,
    parse_peer_directory,
)
from kunjin.funds.peers.store import PeerStore, canonical_fingerprint
from kunjin.funds.service import FundDisclosureService
from kunjin.funds.sources import FundTextClient
from kunjin.funds.store import FundDisclosureStore
from kunjin.services.research import ResearchSyncService
from kunjin.storage.repository import Repository

PEER_NAV_MAX_PAGES = 20
_FUND_CODE_PATTERN = re.compile(r"^\d{6}$")
_RULE_DESCRIPTION = "相同基金类型、管理方式和业绩基准族。"


@dataclass(frozen=True)
class PeerSyncResult:
    anchor_fund_code: str
    status: PeerSyncState
    peer_group_id: Optional[int]
    members: int
    attempted_candidates: int
    rejected_candidates: int
    warnings: Tuple[str, ...]
    errors: Tuple[Dict[str, str], ...]


class PeerResearchService:
    def __init__(
        self,
        directory_client: FundTextClient,
        disclosure_service: FundDisclosureService,
        disclosure_store: FundDisclosureStore,
        research_service: ResearchSyncService,
        repository: Repository,
        peer_store: PeerStore,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.directory_client = directory_client
        self.disclosure_service = disclosure_service
        self.disclosure_store = disclosure_store
        self.research_service = research_service
        self.repository = repository
        self.peer_store = peer_store
        self.now = now

    def sync_peers(
        self,
        anchor_fund_code: str,
        user_candidates: Sequence[str] = (),
    ) -> PeerSyncResult:
        self._validate_code(anchor_fund_code)
        for code in user_candidates:
            self._validate_code(code)

        attempted_at = self._aware_now()
        errors: List[Dict[str, str]] = []
        warnings: List[str] = []

        # The anchor must be classifiable before candidate discovery incurs a
        # directory request or any wider synchronization work.
        try:
            classification_sync = self.disclosure_service.sync_classification(
                anchor_fund_code
            )
            anchor_evidence_warnings, anchor_evidence_errors = (
                self._classification_evidence(
                    anchor_fund_code, classification_sync
                )
            )
            errors.extend(anchor_evidence_errors)
            anchor_bundle = self.disclosure_store.load_bundle(anchor_fund_code)
            anchor_classification = classify_peer(
                anchor_bundle, anchor_bundle, attempted_at.date()
            )
            if not anchor_classification.accepted:
                raise ValueError(anchor_classification.reason)
        except Exception as error:
            errors.append(self._error(anchor_fund_code, "classification", error))
            return self._failed_result(
                anchor_fund_code,
                "anchor_classification_failed",
                attempted_at,
                0,
                0,
                warnings,
                errors,
            )

        held_codes = tuple(
            sorted(
                {
                    position.fund_code
                    for position in self.repository.latest_positions()
                    if _FUND_CODE_PATTERN.fullmatch(position.fund_code)
                }
            )
        )
        current_group = self.peer_store.load_current_group(anchor_fund_code)
        directory_checksum: Optional[str] = None
        directory_url = PEER_DIRECTORY_URL
        directory_unavailable = False
        try:
            response = self.directory_client.fetch(
                PEER_DIRECTORY_URL, PEER_DIRECTORY_REFERER
            )
            directory = parse_peer_directory(response)
            directory_checksum = response.checksum
            directory_url = response.final_url
            candidates = ordered_candidates(
                anchor_fund_code,
                directory,
                user_candidates,
                held_codes,
            )
        except Exception as error:
            directory_unavailable = True
            warnings.append("candidate_discovery_unavailable")
            errors.append(self._error(anchor_fund_code, "directory", error))
            candidates = self._fallback_candidates(
                anchor_fund_code,
                user_candidates,
                held_codes,
                current_group,
            )
            if current_group is not None:
                directory_checksum = getattr(
                    current_group, "candidate_source_checksum", None
                )
                directory_url = getattr(
                    current_group, "candidate_source_url", PEER_DIRECTORY_URL
                )

        if directory_checksum is None:
            directory_checksum = hashlib.sha256(
                b"candidate_discovery_unavailable"
            ).hexdigest()

        members: List[PeerGroupMember] = []
        attempted_candidates = 0
        rejected_candidates = 0

        anchor_member = self._accepted_member(
            anchor_fund_code,
            MembershipKind.ANCHOR,
            anchor_classification.classification_key or "",
            anchor_classification.reason,
            anchor_classification.warnings + anchor_evidence_warnings,
            anchor_bundle,
            errors,
        )
        members.append(anchor_member)

        for fund_code, membership_kind in candidates:
            if fund_code == anchor_fund_code:
                continue
            if len(members) >= PEER_MEMBER_LIMIT:
                break
            attempted_candidates += 1
            try:
                classification_sync = self.disclosure_service.sync_classification(
                    fund_code
                )
                evidence_warnings, evidence_errors = self._classification_evidence(
                    fund_code, classification_sync
                )
                errors.extend(evidence_errors)
                candidate_bundle = self.disclosure_store.load_bundle(fund_code)
                classification = classify_peer(
                    anchor_bundle, candidate_bundle, attempted_at.date()
                )
            except Exception as error:
                rejected_candidates += 1
                errors.append(self._error(fund_code, "classification", error))
                continue
            if not classification.accepted:
                rejected_candidates += 1
                continue
            members.append(
                self._accepted_member(
                    fund_code,
                    membership_kind,
                    classification.classification_key or "",
                    classification.reason,
                    classification.warnings + evidence_warnings,
                    candidate_bundle,
                    errors,
                )
            )

        if len(members) < 2:
            return self._failed_result(
                anchor_fund_code,
                "peer_group_too_small",
                attempted_at,
                attempted_candidates,
                rejected_candidates,
                warnings,
                errors,
            )

        operational_partial = directory_unavailable or bool(errors) or any(
            member.warning is not None for member in members
        )
        group_status = (
            PeerGroupStatus.PARTIAL if operational_partial else PeerGroupStatus.SUCCESS
        )
        group_warnings = tuple(dict.fromkeys(warnings))
        group = PeerGroup(
            id=None,
            anchor_fund_code=anchor_fund_code,
            rule_version=PEER_RULE_VERSION,
            rule_key=anchor_classification.classification_key or "",
            rule_description=_RULE_DESCRIPTION,
            candidate_source_url=directory_url,
            candidate_source_tier=2,
            candidate_source_checksum=directory_checksum,
            input_fingerprint=canonical_fingerprint(
                {
                    "anchor": anchor_fund_code,
                    "rule_version": PEER_RULE_VERSION,
                    "directory_checksum": directory_checksum,
                    "members": [
                        {
                            "fund_code": member.fund_code,
                            "membership_kind": member.membership_kind.value,
                            "classification_key": member.classification_key,
                            "profile_source_document_id": member.profile_source_document_id,
                            "warning": member.warning,
                        }
                        for member in members
                    ],
                }
            ),
            created_at=attempted_at,
            status=group_status,
            members=tuple(members),
            warnings=group_warnings,
        )
        group_id = self.peer_store.publish_group(group)
        return PeerSyncResult(
            anchor_fund_code=anchor_fund_code,
            status=(
                PeerSyncState.PARTIAL
                if operational_partial
                else PeerSyncState.SUCCESS
            ),
            peer_group_id=group_id,
            members=len(members),
            attempted_candidates=attempted_candidates,
            rejected_candidates=rejected_candidates,
            warnings=group_warnings,
            errors=tuple(errors),
        )

    def refresh_existing_groups(self) -> Dict[str, PeerSyncResult]:
        results: Dict[str, PeerSyncResult] = {}
        for anchor_code in self.peer_store.list_anchor_codes():
            current_group: Optional[PeerGroup] = None
            try:
                current_group = self.peer_store.load_current_group(anchor_code)
                user_candidates = (
                    ()
                    if current_group is None
                    else tuple(
                        member.fund_code
                        for member in current_group.members
                        if member.membership_kind is MembershipKind.USER_SUPPLIED
                    )
                )
                results[anchor_code] = self.sync_peers(
                    anchor_code, user_candidates=user_candidates
                )
            except Exception as error:
                attempted_at = self._safe_now()
                try:
                    self.peer_store.mark_failure(
                        anchor_code,
                        "peer_group_refresh_failed",
                        "peer_group_refresh_failed",
                        attempted_at,
                    )
                except Exception:
                    pass
                results[anchor_code] = PeerSyncResult(
                    anchor_fund_code=anchor_code,
                    status=PeerSyncState.SOURCE_UNAVAILABLE,
                    peer_group_id=(
                        None if current_group is None else current_group.id
                    ),
                    members=(
                        0 if current_group is None else len(current_group.members)
                    ),
                    attempted_candidates=0,
                    rejected_candidates=0,
                    warnings=("peer_group_refresh_failed",),
                    errors=(self._error(anchor_code, "refresh", error),),
                )
        return results

    def _accepted_member(
        self,
        fund_code: str,
        membership_kind: MembershipKind,
        classification_key: str,
        acceptance_reason: str,
        classification_warnings: Sequence[str],
        classification_bundle: DisclosureBundle,
        errors: List[Dict[str, str]],
    ) -> PeerGroupMember:
        member_warnings = list(classification_warnings)
        try:
            result = self.disclosure_service.sync_profile(fund_code)
            if self._has_section_gap(result):
                member_warnings.append("profile_data_incomplete")
        except Exception as error:
            member_warnings.append("profile_sync_failed")
            errors.append(self._error(fund_code, "profile", error))
        try:
            result = self.disclosure_service.sync_holdings(fund_code)
            if self._has_section_gap(result):
                member_warnings.append("holdings_data_incomplete")
        except Exception as error:
            member_warnings.append("holdings_sync_failed")
            errors.append(self._error(fund_code, "holdings", error))
        try:
            result = self.research_service.sync_fund(
                fund_code, max_pages=PEER_NAV_MAX_PAGES
            )
            if getattr(result, "observations", 1) == 0:
                member_warnings.append("nav_data_missing")
        except Exception as error:
            member_warnings.append("nav_sync_failed")
            errors.append(self._error(fund_code, "nav", error))

        current_bundle = self.disclosure_store.load_bundle(fund_code)
        identity = current_bundle.identity or classification_bundle.identity
        source_document_id = (
            None if identity is None else identity.source_document_id
        )
        return PeerGroupMember(
            fund_code=fund_code,
            membership_kind=membership_kind,
            classification_key=classification_key,
            acceptance_reason=acceptance_reason,
            warning=(
                None
                if not member_warnings
                else ";".join(dict.fromkeys(member_warnings))
            ),
            profile_source_document_id=source_document_id,
        )

    @staticmethod
    def _has_section_gap(result: object) -> bool:
        sections = getattr(result, "sections", None)
        if not isinstance(sections, dict):
            return False
        return any(
            getattr(section, "status", "success") not in {"success", "not_disclosed"}
            for section in sections.values()
        )

    @staticmethod
    def _classification_evidence(
        fund_code: str, result: object
    ) -> Tuple[Tuple[str, ...], List[Dict[str, str]]]:
        sections = getattr(result, "sections", None)
        if not isinstance(sections, dict):
            return (), []
        errors: List[Dict[str, str]] = []
        for section_name, section in sections.items():
            status = str(getattr(section, "status", "success"))
            if status == "success":
                continue
            error_code = getattr(section, "error_code", None) or status
            errors.append(
                {
                    "fund_code": fund_code,
                    "stage": "classification",
                    "error_code": str(error_code),
                    "message": f"{section_name}:{status}",
                }
            )
        return (
            (() if not errors else ("classification_data_incomplete",)),
            errors,
        )

    @staticmethod
    def _fallback_candidates(
        anchor_fund_code: str,
        user_candidates: Sequence[str],
        held_codes: Sequence[str],
        current_group: Optional[PeerGroup],
    ) -> Tuple[Tuple[str, MembershipKind], ...]:
        ordered: List[Tuple[str, MembershipKind]] = [
            (anchor_fund_code, MembershipKind.ANCHOR)
        ]
        seen = {anchor_fund_code}

        def add(code: str, kind: MembershipKind) -> None:
            if code not in seen:
                seen.add(code)
                ordered.append((code, kind))

        for code in user_candidates:
            add(code, MembershipKind.USER_SUPPLIED)
        for code in sorted(set(held_codes)):
            add(code, MembershipKind.HELD)
        if current_group is not None:
            for member in current_group.members:
                add(member.fund_code, member.membership_kind)
        return tuple(ordered)

    def _failed_result(
        self,
        anchor_fund_code: str,
        error_code: str,
        attempted_at: datetime,
        attempted_candidates: int,
        rejected_candidates: int,
        warnings: List[str],
        errors: List[Dict[str, str]],
    ) -> PeerSyncResult:
        warnings.append(error_code)
        self.peer_store.mark_failure(
            anchor_fund_code,
            error_code,
            error_code,
            attempted_at,
        )
        current = self.peer_store.load_current_group(anchor_fund_code)
        return PeerSyncResult(
            anchor_fund_code=anchor_fund_code,
            status=PeerSyncState.SOURCE_UNAVAILABLE,
            peer_group_id=None if current is None else current.id,
            members=0 if current is None else len(current.members),
            attempted_candidates=attempted_candidates,
            rejected_candidates=rejected_candidates,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(errors),
        )

    @staticmethod
    def _error(fund_code: str, stage: str, error: Exception) -> Dict[str, str]:
        return {
            "fund_code": fund_code,
            "stage": stage,
            "error_code": str(
                getattr(error, "code", error.__class__.__name__.casefold())
            ),
            "message": str(error),
        }

    @staticmethod
    def _validate_code(fund_code: str) -> None:
        if not isinstance(fund_code, str) or _FUND_CODE_PATTERN.fullmatch(fund_code) is None:
            raise ValueError(f"invalid fund code: {fund_code}")

    def _aware_now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("peer synchronization time must be timezone-aware")
        return value

    def _safe_now(self) -> datetime:
        try:
            return self._aware_now()
        except Exception:
            return datetime.now(timezone.utc)
