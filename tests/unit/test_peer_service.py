from __future__ import annotations

import hashlib
import unittest
from datetime import date, datetime, timezone
from typing import Dict, Optional, Sequence

from kunjin.funds.models import DisclosureBundle, FundBenchmark, FundIdentity
from kunjin.funds.peers.models import (
    MembershipKind,
    PeerGroup,
    PeerSyncState,
)
from kunjin.funds.peers.service import PeerResearchService
from kunjin.funds.peers.sources import PEER_DIRECTORY_URL
from kunjin.funds.sources import TextResponse


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def bundle(
    code: str,
    *,
    fund_type: Optional[str] = "混合型-灵活",
    benchmark: Optional[str] = "50%×沪深300指数收益率+50%×中债综合全价指数收益率",
) -> DisclosureBundle:
    return DisclosureBundle(
        fund_code=code,
        identity=FundIdentity(
            fund_code=code,
            fund_name=f"示例基金{code}",
            status="active",
            fund_type=fund_type,
            established_date=date(2020, 1, 1),
            manager_name="示例基金公司",
            source_document_id=int(code) + 1,
        ),
        share_classes=(),
        manager_tenures=(),
        fee_rules=(),
        sizes=(),
        benchmarks=(
            ()
            if benchmark is None
            else (
                FundBenchmark(
                    fund_code=code,
                    description=benchmark,
                    effective_from=None,
                    effective_to=None,
                    source_document_id=int(code) + 1,
                ),
            )
        ),
        holdings=(),
        industry_exposure=(),
        announcements=(),
        source_documents={},
        section_states={},
        section_statuses={},
    )


class FakeDirectoryClient:
    def __init__(self, codes: Sequence[str], fail: bool = False) -> None:
        self.codes = tuple(codes)
        self.fail = fail
        self.calls = []

    def fetch(self, url: str, referer: str) -> TextResponse:
        self.calls.append((url, referer))
        if self.fail:
            raise RuntimeError("directory unavailable")
        rows = ",".join(
            f'["{code}","PY","示例基金{code}","混合型-灵活","FULL"]'
            for code in self.codes
        )
        text = f"var r = [{rows}];"
        return TextResponse(
            requested_url=url,
            final_url=url,
            text=text,
            retrieved_at=NOW,
            checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            content_type="application/javascript; charset=utf-8",
        )


class FakeDisclosureService:
    def __init__(self, store: "FakeDisclosureStore") -> None:
        self.store = store
        self.classification_calls = []
        self.profile_calls = []
        self.holdings_calls = []
        self.profile_failures = set()
        self.holdings_failures = set()
        self.classification_results = {}

    def sync_classification(self, code: str) -> object:
        self.classification_calls.append(code)
        return self.classification_results.get(code, object())

    def sync_profile(self, code: str) -> object:
        self.profile_calls.append(code)
        if code in self.profile_failures:
            raise RuntimeError("profile failed")
        return object()

    def sync_holdings(self, code: str) -> object:
        self.holdings_calls.append(code)
        if code in self.holdings_failures:
            raise RuntimeError("holdings failed")
        return object()


class FakeDisclosureStore:
    def __init__(self, bundles: Dict[str, DisclosureBundle]) -> None:
        self.bundles = bundles

    def load_bundle(self, code: str) -> DisclosureBundle:
        return self.bundles[code]


class FakeResearchService:
    def __init__(self) -> None:
        self.calls = []
        self.failures = set()

    def sync_fund(self, code: str, max_pages: int = 0) -> object:
        self.calls.append((code, max_pages))
        if code in self.failures:
            raise RuntimeError("nav failed")
        return object()


class FakeRepository:
    def __init__(self, held_codes: Sequence[str] = ()) -> None:
        self.held_codes = tuple(held_codes)

    def latest_positions(self) -> list:
        return [type("Position", (), {"fund_code": code})() for code in self.held_codes]


class FakePeerStore:
    def __init__(self, current: Optional[PeerGroup] = None) -> None:
        self.current = current
        self.published = []
        self.failures = []
        self.anchors = () if current is None else (current.anchor_fund_code,)
        self.publish_failures = set()

    def publish_group(self, group: PeerGroup) -> int:
        if group.anchor_fund_code in self.publish_failures:
            raise RuntimeError("publish failed")
        self.published.append(group)
        self.current = group
        return 42

    def mark_failure(self, anchor: str, code: str, warning: str, attempted_at: datetime) -> None:
        self.failures.append((anchor, code, warning, attempted_at))

    def load_current_group(self, anchor: str) -> Optional[PeerGroup]:
        return self.current if self.current and self.current.anchor_fund_code == anchor else None

    def list_anchor_codes(self) -> tuple:
        return self.anchors


class PeerResearchServiceTest(unittest.TestCase):
    def make_service(
        self,
        codes: Sequence[str],
        bundles: Optional[Dict[str, DisclosureBundle]] = None,
        held: Sequence[str] = (),
        directory_failure: bool = False,
        peer_store: Optional[FakePeerStore] = None,
    ) -> tuple:
        bundles = bundles or {code: bundle(code) for code in codes}
        disclosure_store = FakeDisclosureStore(bundles)
        disclosure_service = FakeDisclosureService(disclosure_store)
        research_service = FakeResearchService()
        directory_client = FakeDirectoryClient(codes, fail=directory_failure)
        peer_store = peer_store or FakePeerStore()
        service = PeerResearchService(
            directory_client,
            disclosure_service,
            disclosure_store,
            research_service,
            FakeRepository(held),
            peer_store,
            now=lambda: NOW,
        )
        return service, directory_client, disclosure_service, research_service, peer_store

    def test_orders_candidates_and_only_fully_syncs_accepted_members(self) -> None:
        codes = ("519755", "000001", "000002", "000003")
        bundles = {code: bundle(code) for code in codes}
        bundles["000003"] = bundle("000003", benchmark="沪深300指数收益率")
        service, directory, disclosure, research, peer_store = self.make_service(
            codes, bundles, held=("000002",)
        )

        result = service.sync_peers("519755", user_candidates=("000001",))

        self.assertEqual(len(directory.calls), 1)
        self.assertEqual(disclosure.classification_calls[:3], ["519755", "000001", "000002"])
        self.assertNotIn("000003", disclosure.profile_calls)
        self.assertEqual(research.calls, [(code, 20) for code in disclosure.profile_calls])
        self.assertEqual(result.status, PeerSyncState.SUCCESS)
        self.assertEqual([m.membership_kind for m in peer_store.published[0].members[:3]], [
            MembershipKind.ANCHOR, MembershipKind.USER_SUPPLIED, MembershipKind.HELD,
        ])

    def test_bounds_discovered_validation_and_published_members(self) -> None:
        codes = ("519755",) + tuple(f"{value:06d}" for value in range(1, 61))
        service, _, disclosure, _, peer_store = self.make_service(codes)

        result = service.sync_peers("519755")

        self.assertLessEqual(len(disclosure.classification_calls) - 1, 40)
        self.assertEqual(result.members, 20)
        self.assertEqual(len(peer_store.published[0].members), 20)

    def test_isolates_full_sync_failures_and_keeps_classified_member(self) -> None:
        codes = ("519755", "000001")
        service, _, disclosure, research, peer_store = self.make_service(codes)
        disclosure.holdings_failures.add("000001")
        research.failures.add("000001")

        result = service.sync_peers("519755")

        self.assertEqual(result.status, PeerSyncState.PARTIAL)
        member = peer_store.published[0].members[1]
        self.assertIn("holdings_sync_failed", member.warning)
        self.assertIn("nav_sync_failed", member.warning)
        self.assertEqual(len(result.errors), 2)

    def test_too_small_preserves_prior_group(self) -> None:
        codes = ("519755", "000001")
        bundles = {"519755": bundle("519755"), "000001": bundle("000001", benchmark=None)}
        service, _, _, _, peer_store = self.make_service(codes, bundles)

        result = service.sync_peers("519755")

        self.assertEqual(result.status, PeerSyncState.SOURCE_UNAVAILABLE)
        self.assertFalse(peer_store.published)
        self.assertEqual(peer_store.failures[0][1], "peer_group_too_small")

    def test_directory_failure_reuses_explicit_held_and_current_members(self) -> None:
        codes = ("519755", "000001", "000002", "000003")
        current = type("StoredGroup", (), {
            "anchor_fund_code": "519755",
            "members": (
                type("StoredMember", (), {"fund_code": "519755", "membership_kind": MembershipKind.ANCHOR})(),
                type("StoredMember", (), {"fund_code": "000003", "membership_kind": MembershipKind.DISCOVERED})(),
            ),
        })()
        peer_store = FakePeerStore(current=current)
        service, _, disclosure, _, peer_store = self.make_service(
            codes,
            held=("000002",),
            directory_failure=True,
            peer_store=peer_store,
        )

        result = service.sync_peers("519755", user_candidates=("000001",))

        self.assertEqual(result.status, PeerSyncState.PARTIAL)
        self.assertIn("candidate_discovery_unavailable", result.warnings)
        self.assertEqual(disclosure.classification_calls[:4], ["519755", "000001", "000002", "000003"])
        self.assertEqual(result.members, 4)

    def test_refresh_reads_only_stored_anchors(self) -> None:
        codes = ("519755", "000001")
        service, _, _, _, peer_store = self.make_service(codes)
        peer_store.anchors = ("519755",)

        refreshed = service.refresh_existing_groups()

        self.assertEqual(tuple(refreshed), ("519755",))

    def test_anchor_is_validated_before_directory_fetch(self) -> None:
        events = []
        service, directory, disclosure, _, _ = self.make_service(("519755", "000001"))
        original_classification = disclosure.sync_classification
        original_fetch = directory.fetch
        disclosure.sync_classification = lambda code: (events.append(f"classify:{code}"), original_classification(code))[1]
        directory.fetch = lambda url, referer: (events.append("directory"), original_fetch(url, referer))[1]

        service.sync_peers("519755")

        self.assertEqual(events[:2], ["classify:519755", "directory"])
        self.assertEqual(directory.calls[0][0], PEER_DIRECTORY_URL)

    def test_stale_classification_bundle_is_usable_but_marks_partial_evidence(self) -> None:
        codes = ("519755", "000001")
        service, _, disclosure, _, peer_store = self.make_service(codes)
        failed_section = type(
            "Section",
            (),
            {"status": "source_unavailable", "error_code": "network_unavailable"},
        )()
        disclosure.classification_results["519755"] = type(
            "Result", (), {"sections": {"basic_profile": failed_section}}
        )()
        disclosure.classification_results["000001"] = type(
            "Result", (), {"sections": {"basic_profile": failed_section}}
        )()

        result = service.sync_peers("519755")

        self.assertEqual(result.status, PeerSyncState.PARTIAL)
        self.assertEqual(
            [member.warning for member in peer_store.published[0].members],
            ["classification_data_incomplete", "classification_data_incomplete"],
        )
        self.assertEqual(
            [(error["fund_code"], error["error_code"]) for error in result.errors],
            [("519755", "network_unavailable"), ("000001", "network_unavailable")],
        )

    def test_refresh_preserves_user_supplied_membership(self) -> None:
        codes = ("519755", "000001")
        current = type("StoredGroup", (), {
            "anchor_fund_code": "519755",
            "members": (
                type("StoredMember", (), {"fund_code": "519755", "membership_kind": MembershipKind.ANCHOR})(),
                type("StoredMember", (), {"fund_code": "000001", "membership_kind": MembershipKind.USER_SUPPLIED})(),
            ),
        })()
        peer_store = FakePeerStore(current=current)
        service, _, _, _, peer_store = self.make_service(codes, peer_store=peer_store)

        service.refresh_existing_groups()

        self.assertEqual(
            peer_store.published[0].members[1].membership_kind,
            MembershipKind.USER_SUPPLIED,
        )

    def test_refresh_isolates_anchor_failure_and_continues(self) -> None:
        codes = ("519755", "000001", "519756", "000002")
        service, _, _, _, peer_store = self.make_service(codes)
        peer_store.anchors = ("519755", "519756")
        peer_store.publish_failures.add("519755")

        refreshed = service.refresh_existing_groups()

        self.assertEqual(refreshed["519755"].status, PeerSyncState.SOURCE_UNAVAILABLE)
        self.assertEqual(refreshed["519756"].status, PeerSyncState.SUCCESS)
        self.assertEqual(peer_store.failures[0][0], "519755")


if __name__ == "__main__":
    unittest.main()
