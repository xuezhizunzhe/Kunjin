from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from kunjin.brief.models import thesis_record_fingerprint
from kunjin.decision.models import canonical_json_bytes
from kunjin.holding_review.models import AdjudicationDecision, ThesisMatchProjectionState
from kunjin.holding_review.store import HoldingReviewStoreError
from kunjin.holding_review.thesis import (
    ThesisMatcherPolicyV1,
    ThesisReviewError,
    ThesisReviewService,
)
from kunjin.models import InvestmentThesis
from tests.unit.test_holding_review_store import NOW

pytest_plugins = ("tests.unit.test_holding_review_store",)


def _replace_active_thesis(context, invalidation: str) -> tuple[int, InvestmentThesis]:
    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE fund_code=? AND active=1",
            ("123456",),
        )
    thesis = InvestmentThesis(
        fund_code="123456",
        rationale="Synthetic long-term thesis.",
        horizon="Three years.",
        invalidation=invalidation,
        created_at=NOW + timedelta(minutes=1),
    )
    return context["repository"].add_thesis(thesis), thesis


def _service(context) -> ThesisReviewService:
    return ThesisReviewService(
        context["repository"],
        holding_review_store=context["store"],
        clock=lambda: NOW + timedelta(minutes=2),
    )


def test_matcher_policy_is_immutable_and_checksum_is_canonical() -> None:
    policy = ThesisMatcherPolicyV1()
    assert policy.checksum() == hashlib.sha256(
        canonical_json_bytes({"version": "1"})
    ).hexdigest()
    with pytest.raises(Exception):
        policy.version = "2"


def test_match_project_covers_missing_no_match_and_candidate_only(context) -> None:
    service = _service(context)
    _replace_active_thesis(context, "No matching evidence phrase")
    no_match = service.match_project("123456", context["intelligence_run_id"])
    assert no_match.value.projection_state is ThesisMatchProjectionState.NO_MATCHING_EVIDENCE
    assert no_match.value.evidence_descriptors == ()

    thesis_id, thesis = _replace_active_thesis(context, "Authenticated excerpt item_one")
    matched = service.match_project("123456", context["intelligence_run_id"])
    assert matched.value.thesis_id == thesis_id
    assert matched.value.thesis_fingerprint == thesis_record_fingerprint(thesis_id, thesis)
    assert matched.value.projection_state is (
        ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH
    )
    assert matched.value.evidence_ids == ("item_one",)
    assert matched.value.evidence_descriptors[0].conflicted is True
    assert "review_disposition" not in matched.value.to_canonical_dict()
    assert "action_authorized" not in matched.value.to_canonical_dict()

    with context["repository"].connect() as connection, connection:
        connection.execute(
            "UPDATE investment_theses SET active=0 WHERE fund_code=?", ("123456",)
        )
    missing = service.match_project("123456", context["intelligence_run_id"])
    assert missing.value.projection_state is ThesisMatchProjectionState.THESIS_MISSING
    assert missing.value.thesis_id is None
    assert missing.value.evidence_descriptors == ()


def test_match_project_preserves_negated_candidate_and_stable_order(context) -> None:
    _replace_active_thesis(context, "Authenticated excerpt")
    service = _service(context)

    first = service.match_project("123456", context["intelligence_run_id"])
    second = service.match_project("123456", context["intelligence_run_id"])

    assert first == second
    assert first.value.evidence_ids == ("item_one", "item_two")


def test_match_project_rejects_foreign_item_wrong_subject_and_missing_use(context) -> None:
    _replace_active_thesis(context, "No matching evidence phrase")
    service = _service(context)
    projection = service.match_project("123456", context["intelligence_run_id"])
    inputs = context["store"].authenticated_thesis_projection_inputs(
        context["intelligence_run_id"]
    )
    forged = replace(
        projection.value,
        projection_state=ThesisMatchProjectionState.POSSIBLE_INVALIDATION_MATCH,
        evidence_descriptors=(inputs.evidence_descriptors[0],),
    )
    forged = replace(
        forged,
        evidence_set_checksum=forged.expected_evidence_set_checksum(),
    )
    forged = replace(forged, record_checksum=forged.expected_record_checksum())
    with pytest.raises(
        HoldingReviewStoreError,
        match="evidence descriptor authentication failed",
    ):
        context["store"].publish_thesis_match(forged)
    with pytest.raises(ThesisReviewError, match="snapshot binding failed"):
        _service(context).match_project("654321", context["intelligence_run_id"])

    with context["repository"].connect() as connection, connection:
        connection.execute("DROP TRIGGER intelligence_snapshot_item_use_no_delete")
        connection.execute(
            "DELETE FROM intelligence_snapshot_item_uses WHERE request_run_id=?",
            (context["intelligence_run_id"],),
        )
    with pytest.raises(ThesisReviewError, match="snapshot authentication failed"):
        _service(context).match_project("123456", context["intelligence_run_id"])


def test_adjudicate_requires_possible_match_and_explicit_decision(context) -> None:
    _replace_active_thesis(context, "No matching evidence phrase")
    no_match = _service(context).match_project(
        "123456", context["intelligence_run_id"]
    )
    with pytest.raises(ThesisReviewError, match="possible match"):
        _service(context).adjudicate(
            "123456", no_match.id, AdjudicationDecision.PRESENTED_MATCH_REJECTED
        )
    with pytest.raises(ThesisReviewError, match="explicit decision"):
        _service(context).adjudicate("123456", no_match.id, None)


def test_adjudication_is_idempotent_and_preserves_evidence(context) -> None:
    _replace_active_thesis(context, "Authenticated excerpt item_one")
    service = _service(context)
    projection = service.match_project("123456", context["intelligence_run_id"])
    first = service.adjudicate(
        "123456", projection.id, AdjudicationDecision.PRESENTED_MATCH_CONFIRMED
    )
    second = service.adjudicate(
        "123456", projection.id, AdjudicationDecision.PRESENTED_MATCH_CONFIRMED
    )

    assert first == second
    assert first.value.evidence_ids == projection.value.evidence_ids
    assert service.holding_review_store.authenticated_thesis_match(projection.id) == projection


def test_changed_adjudication_requires_current_supersession(context) -> None:
    _replace_active_thesis(context, "Authenticated excerpt item_one")
    service = _service(context)
    projection = service.match_project("123456", context["intelligence_run_id"])
    first = service.adjudicate(
        "123456", projection.id, AdjudicationDecision.PRESENTED_MATCH_REJECTED
    )
    with pytest.raises(ThesisReviewError, match="supersession"):
        service.adjudicate(
            "123456", projection.id, AdjudicationDecision.PRESENTED_MATCH_CONFIRMED
        )
    replacement = service.adjudicate(
        "123456",
        projection.id,
        AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
        supersedes_id=first.id,
    )
    assert service.holding_review_store.current_adjudication(projection.id) == replacement
    assert service.adjudicate(
        "123456",
        projection.id,
        AdjudicationDecision.PRESENTED_MATCH_CONFIRMED,
        supersedes_id=replacement.id,
    ) == replacement


def test_adjudication_rejects_cross_fund_and_thesis_replacement(context) -> None:
    _replace_active_thesis(context, "Authenticated excerpt item_one")
    service = _service(context)
    projection = service.match_project("123456", context["intelligence_run_id"])
    with pytest.raises(ThesisReviewError, match="projection binding failed"):
        service.adjudicate("654321", projection.id, AdjudicationDecision.UNCERTAIN)

    _replace_active_thesis(context, "Replacement condition")
    with pytest.raises(ThesisReviewError, match="projection authentication failed"):
        service.adjudicate("123456", projection.id, AdjudicationDecision.UNCERTAIN)


def test_newer_active_thesis_invalidates_old_projection(context) -> None:
    _replace_active_thesis(context, "Authenticated excerpt item_one")
    service = _service(context)
    projection = service.match_project("123456", context["intelligence_run_id"])
    context["repository"].add_thesis(
        InvestmentThesis(
            fund_code="123456",
            rationale="Newer synthetic thesis.",
            horizon="Three years.",
            invalidation="Replacement condition.",
            created_at=NOW + timedelta(minutes=3),
        )
    )

    with pytest.raises(ThesisReviewError, match="projection authentication failed"):
        service.adjudicate("123456", projection.id, AdjudicationDecision.UNCERTAIN)
