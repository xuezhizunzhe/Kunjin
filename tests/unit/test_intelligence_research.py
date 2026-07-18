from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from kunjin.decision.models import RequestMode, RequestTerminalStatus
from kunjin.intelligence.models import IntelligenceWorkflow, QueryInterval
from kunjin.intelligence.research import public_intelligence_payload
from kunjin.intelligence.service import (
    IntelligenceRequestSubject,
    PragmaticIntelligenceResult,
)
from kunjin.intelligence.store import AuthenticatedTerminalRequest

NOW = datetime(2026, 7, 18, 6, 0, tzinfo=timezone.utc)


def test_public_projection_accepts_only_the_authenticated_wrapper() -> None:
    parameters = inspect.signature(public_intelligence_payload).parameters

    assert tuple(parameters) == ("result",)


def test_null_snapshot_projection_is_partial_amount_free_and_hides_global_sentinel() -> None:
    result = PragmaticIntelligenceResult(
        report=None,
        terminal_request=AuthenticatedTerminalRequest(
            id=1,
            request_id="a" * 32,
            mode=RequestMode.RAPID,
            status=RequestTerminalStatus.PARTIAL,
            started_at=NOW,
            deadline_at=NOW + timedelta(seconds=90),
            finished_at=NOW + timedelta(seconds=1),
            omitted_work=("all_sources_without_usable_evidence",),
        ),
        subject=IntelligenceRequestSubject(
            workflow=IntelligenceWorkflow.NEWS_RECENT,
            interval=QueryInterval(
                NOW - timedelta(hours=72),
                NOW,
                "Asia/Shanghai",
            ),
            subject_scope="global_public",
            fund_code=None,
        ),
        items=(),
        item_uses=(),
        lineage_edges=(),
        events=(),
        source_summaries=(),
        sector_labels=(),
        fund_context=None,
        thesis_review=None,
    )

    payload = public_intelligence_payload(result)

    assert set(payload) == {
        "request",
        "items",
        "events",
        "dimensions",
        "experimental_shadow",
        "fund_relevance",
        "thesis_review",
        "conflicts",
        "cross_validation",
        "missing_evidence",
        "beginner_explanation_zh",
        "exact_amount_available",
        "action_maturity",
        "action_authorized",
    }
    assert payload["request"]["subject_scope"] == "global_public"
    assert payload["request"]["terminal_status"] == "partial"
    assert payload["exact_amount_available"] is False
    assert payload["cross_validation"]["complete"] is False
    assert "fund:000000" not in str(payload)
