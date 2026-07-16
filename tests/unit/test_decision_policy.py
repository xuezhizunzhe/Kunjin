import json
import re

from kunjin.decision.models import (
    ActionKind,
    RequestFieldResolution,
    RequestMode,
    RiskEffect,
    SourceFieldState,
    WorkflowLevel,
)
from kunjin.decision.policy import EvidencePolicyV1
from kunjin.decision.source_registry import SourceRegistryV1


def test_phase0_enums_are_exact() -> None:
    assert [item.value for item in RequestMode] == ["rapid", "deep"]
    assert [item.value for item in RiskEffect] == [
        "information",
        "risk_maintaining",
        "risk_reducing",
        "risk_increasing",
    ]
    assert [item.value for item in SourceFieldState] == [
        "not_checked",
        "healthy",
        "degraded",
        "cooldown",
        "unavailable",
        "unsupported",
    ]
    assert [item.value for item in RequestFieldResolution] == [
        "usable",
        "partial",
        "manual_supplement_required",
    ]
    assert [item.value for item in WorkflowLevel] == [
        "rapid_evidence",
        "decision_evidence",
    ]
    assert ActionKind.SWITCH_FUNDS.value == "switch_funds"


def test_policy_and_registry_are_canonical_and_public() -> None:
    for item in (EvidencePolicyV1(), SourceRegistryV1()):
        canonical = item.canonical_json()
        assert (
            json.dumps(
                json.loads(canonical.decode("ascii")),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            == canonical
        )
        assert re.fullmatch(r"[0-9a-f]{64}", item.checksum())
        lowered = canonical.decode("ascii").casefold()
        for forbidden in ("ciphertext", "nonce", "access_token", "private_value"):
            assert forbidden not in lowered


def test_source_registry_is_finite_and_has_supplementation() -> None:
    registry = SourceRegistryV1()
    assert 1 <= len(registry.sources) <= 8
    identities = {
        (source.source_id, field.field_id)
        for source in registry.sources
        for field in source.fields
    }
    assert len(identities) == sum(len(source.fields) for source in registry.sources)
    assert all(
        field.supplementation.accepted_input
        for source in registry.sources
        for field in source.fields
    )
