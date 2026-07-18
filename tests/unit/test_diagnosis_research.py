from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kunjin.diagnosis.research import public_diagnosis_payload
from kunjin.diagnosis.service import DiagnosisService
from kunjin.funds.store import FundDisclosureStore
from kunjin.storage.repository import Repository


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_public_diagnosis_payload_has_stable_sections_and_no_private_keys(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path / "projection.db")
    repository.migrate()
    result = DiagnosisService(
        repository,
        FundDisclosureStore(repository),
        clock=lambda: datetime(2026, 7, 19, 6, tzinfo=timezone.utc),
    ).diagnose()

    payload = public_diagnosis_payload(result)

    assert set(payload) == {
        "action_boundary",
        "as_of",
        "beginner_explanation_zh",
        "candidate_impact",
        "concentration",
        "conflicts",
        "coverage",
        "findings",
        "input_fingerprint",
        "missing_evidence",
        "relationships",
        "warnings",
    }
    assert payload["action_boundary"] == {
        "action_authorized": False,
        "action_maturity": "evidence_only",
        "exact_amount_available": False,
    }
    keys = {key.casefold() for key in _walk_keys(payload)}
    assert not keys.intersection(
        {"account_title", "amount", "cost", "income", "profit", "shares"}
    )
    assert payload["concentration"]["value_basis"] == "missing"
    assert payload["coverage"]["relationship"]["evidence_state"] == (
        "insufficient_data"
    )
