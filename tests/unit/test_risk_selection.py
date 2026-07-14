from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import candidate_fingerprint
from kunjin.funds.risk.documents import OfficialDocumentCandidate
from kunjin.funds.risk.selection import (
    PERIODIC_DOCUMENT_KINDS,
    SELECTION_POLICY_V1_CHECKSUM,
    SELECTION_REASON_CODES,
    SELECTION_STATES,
    DocumentSelectionPlan,
    PeriodicSelectionState,
    SelectionCandidate,
    select_current_candidates,
)

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def candidate(**changes: object) -> OfficialDocumentCandidate:
    values = {
        "fund_code": "519755",
        "document_kind": DocumentKind.QUARTERLY_REPORT,
        "title": "example fund 2026 second-quarter report",
        "url": "https://www.fund001.com/reports/519755-q2.doc",
        "publisher": "example fund manager",
        "published_at": NOW,
        "source_tier": 1,
    }
    values.update(changes)
    return OfficialDocumentCandidate(**values)


class RiskSelectionTests(unittest.TestCase):
    def test_selects_unique_newest_periodic_candidates_and_preserves_nonperiodic(self) -> None:
        old_quarter = candidate(
            title="example fund 2026 first-quarter report",
            url="https://www.fund001.com/reports/519755-q1.doc",
            published_at=NOW - timedelta(days=90),
        )
        new_quarter = candidate()
        annual = candidate(
            document_kind=DocumentKind.ANNUAL_REPORT,
            title="example fund 2025 annual report",
            url="https://www.fund001.com/reports/519755-annual.doc",
            published_at=NOW - timedelta(days=30),
        )
        product_summary = candidate(
            document_kind=DocumentKind.PRODUCT_SUMMARY,
            title="example fund product summary",
            url="https://www.fund001.com/reports/519755-summary.pdf",
            published_at=None,
        )

        plan = select_current_candidates(
            "519755",
            refresh_run_id=7,
            candidates=(old_quarter, new_quarter, annual, product_summary),
        )

        plan.validate()
        self.assertEqual(
            tuple(item.url for item in plan.attempted_candidates),
            (new_quarter.url, annual.url, product_summary.url),
        )
        quarterly = plan.status_for(DocumentKind.QUARTERLY_REPORT)
        self.assertEqual(quarterly.state, "selected")
        self.assertEqual(quarterly.selected_fingerprint, candidate_fingerprint(new_quarter))
        self.assertIsNone(quarterly.reason_code)
        self.assertEqual(
            quarterly.candidate_fingerprints,
            tuple(
                sorted(
                    (
                        candidate_fingerprint(old_quarter),
                        candidate_fingerprint(new_quarter),
                    )
                )
            ),
        )

    def test_newest_time_tie_fails_closed_without_attempting_periodic_kind(self) -> None:
        first = candidate(url="https://www.fund001.com/reports/519755-q2-a.doc")
        second = candidate(url="https://www.fund001.com/reports/519755-q2-b.doc")

        tied = select_current_candidates(
            "519755",
            refresh_run_id=8,
            candidates=(first, second),
        )

        state = tied.status_for(DocumentKind.QUARTERLY_REPORT)
        self.assertEqual(tied.attempted_candidates, ())
        self.assertEqual(state.state, "conflicted")
        self.assertEqual(state.reason_code, "current_periodic_candidate_conflict")
        self.assertEqual(
            state.candidate_fingerprints,
            tuple(sorted((candidate_fingerprint(first), candidate_fingerprint(second)))),
        )

    def test_missing_kinds_are_explicit_and_attempt_nothing(self) -> None:
        missing = select_current_candidates(
            "519755",
            refresh_run_id=9,
            candidates=(),
        )

        self.assertEqual(missing.attempted_candidates, ())
        for kind in PERIODIC_DOCUMENT_KINDS:
            with self.subTest(kind=kind):
                state = missing.status_for(kind)
                self.assertEqual(state.state, "missing")
                self.assertEqual(
                    state.reason_code,
                    "current_periodic_candidate_missing",
                )
                self.assertEqual(state.candidate_fingerprints, ())
                self.assertIsNone(state.selected_fingerprint)

    def test_nonperiodic_candidates_retain_discovery_order(self) -> None:
        prospectus = candidate(
            document_kind=DocumentKind.PROSPECTUS_UPDATE,
            title="updated prospectus",
            url="https://www.fund001.com/reports/519755-prospectus.doc",
            published_at=None,
        )
        summary = candidate(
            document_kind=DocumentKind.PRODUCT_SUMMARY,
            title="product summary",
            url="https://www.fund001.com/reports/519755-summary.pdf",
            published_at=None,
        )

        plan = select_current_candidates(
            "519755",
            refresh_run_id=10,
            candidates=(prospectus, summary),
        )

        self.assertEqual(plan.attempted_candidates, (prospectus, summary))
        self.assertEqual(plan.periodic_candidates, ())

    def test_manifest_order_and_checksum_are_independent_of_candidate_order(self) -> None:
        old = candidate(
            url="https://www.fund001.com/reports/519755-q1.doc",
            published_at=NOW - timedelta(days=90),
        )
        new = candidate()
        annual = candidate(
            document_kind=DocumentKind.ANNUAL_REPORT,
            title="example fund 2025 annual report",
            url="https://www.fund001.com/reports/519755-annual.doc",
            published_at=NOW - timedelta(days=30),
        )

        first = select_current_candidates(
            "519755", refresh_run_id=11, candidates=(old, new, annual)
        )
        second = select_current_candidates(
            "519755", refresh_run_id=11, candidates=(annual, new, old)
        )

        self.assertEqual(first.periodic_candidates, second.periodic_candidates)
        self.assertEqual(first.periodic_states, second.periodic_states)
        self.assertEqual(first.canonical_json, second.canonical_json)
        self.assertEqual(first.selection_checksum, second.selection_checksum)

    def test_manifest_is_canonical_ascii_and_checksum_bound(self) -> None:
        value = select_current_candidates(
            "519755", refresh_run_id=12, candidates=(candidate(),)
        )
        payload = json.loads(value.canonical_json)

        self.assertEqual(
            value.canonical_json,
            json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        )
        self.assertEqual(
            value.selection_checksum,
            hashlib.sha256(value.canonical_json.encode("ascii")).hexdigest(),
        )
        self.assertEqual(value.selection_policy_checksum, SELECTION_POLICY_V1_CHECKSUM)
        self.assertEqual(len(SELECTION_POLICY_V1_CHECKSUM), 64)

    def test_plan_rejects_canonical_json_and_checksum_tampering(self) -> None:
        value = select_current_candidates(
            "519755", refresh_run_id=13, candidates=(candidate(),)
        )
        noncanonical = replace(
            value,
            canonical_json=json.dumps(json.loads(value.canonical_json), indent=2),
        )
        with self.assertRaisesRegex(ValueError, "canonical"):
            noncanonical.validate()

        mismatched = replace(value, selection_checksum="0" * 64)
        with self.assertRaisesRegex(ValueError, "checksum"):
            mismatched.validate()

        unknown_policy = replace(value, selection_policy_checksum="1" * 64)
        with self.assertRaisesRegex(ValueError, "policy"):
            unknown_policy.validate()

    def test_selection_rejects_mutable_inputs_duplicate_fingerprints_and_wrong_fund(self) -> None:
        exact = candidate()
        with self.assertRaisesRegex(ValueError, "tuple"):
            select_current_candidates(
                "519755", refresh_run_id=14, candidates=[exact]  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            select_current_candidates(
                "519755", refresh_run_id=14, candidates=(exact, exact)
            )
        with self.assertRaisesRegex(ValueError, "fund"):
            select_current_candidates(
                "519755",
                refresh_run_id=14,
                candidates=(candidate(fund_code="519706"),),
            )

    def test_selection_rejects_non_utc_or_missing_periodic_publication_time(self) -> None:
        non_utc = candidate(published_at=NOW.astimezone(timezone(timedelta(hours=8))))
        with self.assertRaisesRegex(ValueError, "UTC"):
            select_current_candidates(
                "519755", refresh_run_id=15, candidates=(non_utc,)
            )
        with self.assertRaisesRegex(ValueError, "publication time"):
            select_current_candidates(
                "519755", refresh_run_id=15, candidates=(candidate(published_at=None),)
            )

    def test_selection_rejects_subclasses_and_invalid_scalar_types(self) -> None:
        class CandidateSubclass(OfficialDocumentCandidate):
            pass

        subclass = CandidateSubclass(**vars(candidate()))
        with self.assertRaisesRegex(ValueError, "subclasses"):
            select_current_candidates(
                "519755", refresh_run_id=16, candidates=(subclass,)
            )
        with self.assertRaisesRegex(ValueError, "refresh"):
            select_current_candidates(
                "519755", refresh_run_id=True, candidates=()  # type: ignore[arg-type]
            )

    def test_exact_records_are_frozen_and_reject_subclasses_or_hidden_state(self) -> None:
        plan = select_current_candidates(
            "519755", refresh_run_id=17, candidates=(candidate(),)
        )
        with self.assertRaises(FrozenInstanceError):
            plan.fund_code = "519706"

        class PlanSubclass(DocumentSelectionPlan):
            pass

        subclass = PlanSubclass(**vars(plan))
        with self.assertRaisesRegex(ValueError, "subclasses"):
            subclass.validate()

        state = plan.status_for(DocumentKind.QUARTERLY_REPORT)
        hidden = replace(state)
        object.__setattr__(hidden, "hidden", "state")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            hidden.validate()

    def test_public_constants_and_status_lookup_are_closed(self) -> None:
        self.assertEqual(SELECTION_STATES, frozenset({"selected", "missing", "conflicted"}))
        self.assertEqual(
            SELECTION_REASON_CODES,
            frozenset(
                {
                    "current_periodic_candidate_missing",
                    "current_periodic_candidate_conflict",
                }
            ),
        )
        self.assertEqual(
            PERIODIC_DOCUMENT_KINDS,
            (
                DocumentKind.ANNUAL_REPORT,
                DocumentKind.QUARTERLY_REPORT,
                DocumentKind.SEMIANNUAL_REPORT,
            ),
        )
        plan = select_current_candidates("519755", refresh_run_id=18, candidates=())
        with self.assertRaisesRegex(ValueError, "periodic"):
            plan.status_for(DocumentKind.PRODUCT_SUMMARY)

    def test_manual_record_validation_rejects_invalid_state_combinations(self) -> None:
        selected = SelectionCandidate(
            candidate_fingerprint="a" * 64,
            document_kind=DocumentKind.QUARTERLY_REPORT,
            url="https://www.fund001.com/reports/519755-q2.doc",
            published_at=NOW,
        )
        selected.validate()

        invalid = PeriodicSelectionState(
            document_kind=DocumentKind.QUARTERLY_REPORT,
            state="selected",
            candidate_fingerprints=("a" * 64,),
            selected_fingerprint=None,
            reason_code=None,
        )
        with self.assertRaisesRegex(ValueError, "selected fingerprint"):
            invalid.validate()


if __name__ == "__main__":
    unittest.main()
