from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kunjin.brief.d2 import build_d2_relationships
from kunjin.brief.facts import SourceLinkedFactSet
from kunjin.brief.models import BriefEvidenceState, BriefFact
from kunjin.brief.portfolio_worker_protocol import (
    PortfolioAccount,
    PortfolioObservationPayload,
    PortfolioPosition,
    PortfolioWorkerRequest,
    encode_portfolio_success,
)
from kunjin.brief.public_acceptance_portfolio import (
    ACCEPTANCE_ATTESTATION,
    ACCEPTANCE_ATTESTATION_ENV,
    ACCEPTANCE_FIXTURE_FD_ENV,
    ACCEPTANCE_MARKER_FD_ENV,
    ACCEPTANCE_RUN_ID_ENV,
    SYNTHETIC_OBSERVATION_VERSION,
    PublicAcceptancePortfolioService,
    build_public_acceptance_portfolio_service,
    load_public_acceptance_capability,
)
from kunjin.brief.service import HeldFundBriefService
from kunjin.cli import build_context
from kunjin.decision.budget import RequestBudget
from kunjin.decision.health import SourceHealthService
from kunjin.decision.models import (
    EvidenceCompleteness,
    EvidenceFreshness,
    RequestMode,
    SourceTier,
)
from kunjin.decision.store import DecisionAuditStore
from kunjin.funds.service import SourceRequestContext
from kunjin.paths import RuntimePaths
from kunjin.services.sync import PortfolioSyncService
from kunjin.storage.repository import Repository


def _runtime() -> tuple[tempfile.TemporaryDirectory[str], Path, RuntimePaths]:
    temporary = tempfile.TemporaryDirectory(
        prefix="kunjin-phase1-acceptance.",
        dir="/private/tmp",
    )
    runtime = Path(temporary.name)
    runtime.chmod(0o700)
    case = runtime / "case-healthy"
    data = case / "data"
    state = case / "state"
    data.mkdir(parents=True, mode=0o700)
    state.mkdir(mode=0o700)
    case.chmod(0o700)
    return temporary, runtime, RuntimePaths(
        database=data / "kunjin.db",
        snapshots=data / "snapshots",
        logs=state / "logs",
    )


def _capability_fds(
    state: Path,
    *,
    fund_code: str = "000001",
    run_id: str = "a" * 64,
    unlink_fixture: bool = True,
) -> tuple[int, int, int]:
    fixture_path = state / "fixture"
    fixture_path.write_bytes(
        json.dumps(
            {
                "contract": "kunjin_phase1_public_portfolio_v1",
                "fund_code": fund_code,
                "run_id": run_id,
                "schema_version": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    )
    fixture_path.chmod(0o600)
    fixture_fd = os.open(fixture_path, os.O_RDONLY | os.O_NOFOLLOW)
    if unlink_fixture:
        fixture_path.unlink()
    marker_read_fd, marker_write_fd = os.pipe()
    return fixture_fd, marker_read_fd, marker_write_fd


def _environment(fixture_fd: int, marker_fd: int, run_id: str = "a" * 64) -> dict:
    return {
        ACCEPTANCE_ATTESTATION_ENV: ACCEPTANCE_ATTESTATION,
        ACCEPTANCE_FIXTURE_FD_ENV: str(fixture_fd),
        ACCEPTANCE_MARKER_FD_ENV: str(marker_fd),
        ACCEPTANCE_RUN_ID_ENV: run_id,
    }


def _source_context(repository: Repository):
    budget = RequestBudget.create(RequestMode.RAPID, request_id="b" * 32)
    audit = DecisionAuditStore(repository)
    run_id = audit.begin_request(budget)
    return SourceRequestContext(
        run_id,
        budget,
        audit,
        SourceHealthService(audit),
    )


def _public_fact(field_id: str, value: object, observed_at) -> BriefFact:
    return BriefFact(
        fact_id=field_id,
        field_id=field_id,
        value=value,
        unit=None,
        data_as_of=observed_at,
        published_at=observed_at,
        retrieved_at=observed_at,
        source_id="synthetic_public_source",
        source_tier=SourceTier.TIER_1,
        publisher="公开合成验收来源",
        canonical_url=f"https://example.invalid/{field_id}",
        freshness=EvidenceFreshness.CURRENT,
        completeness=EvidenceCompleteness.COMPLETE,
        conflict_ids=(),
        calculated=False,
        source_lineage_id="synthetic_public_lineage",
    )


def test_anonymous_capability_builds_same_request_synthetic_portfolio(monkeypatch) -> None:
    temporary, _runtime_root, paths = _runtime()
    fixture_fd, marker_read_fd, marker_write_fd = _capability_fds(paths.logs.parent)
    for key, value in _environment(fixture_fd, marker_write_fd).items():
        monkeypatch.setenv(key, value)
    try:
        capability = load_public_acceptance_capability(paths, "000001")
        assert capability is not None
        for key in (
            ACCEPTANCE_ATTESTATION_ENV,
            ACCEPTANCE_FIXTURE_FD_ENV,
            ACCEPTANCE_MARKER_FD_ENV,
        ):
            assert key not in os.environ
        assert os.environ[ACCEPTANCE_RUN_ID_ENV] == "a" * 64

        paths.ensure()
        repository = Repository(paths.database)
        repository.migrate()
        service = build_public_acceptance_portfolio_service(
            repository,
            PortfolioSyncService(None, repository),
            capability,
        )
        context = _source_context(repository)

        result = service.sync("000001", context)

        assert result.status == "success"
        assert result.position_present is True
        assert result.portfolio_binding.observation_version == SYNTHETIC_OBSERVATION_VERSION
        assert [item.fund_code for item in result.portfolio_binding.positions] == [
            "000001",
            "000001",
        ]
        assert len({item.account_title for item in result.portfolio_binding.positions}) == 2
        assert {str(item.formal_nav) for item in result.portfolio_binding.positions} == {"1"}
        brief_service = object.__new__(HeldFundBriefService)
        brief_service._portfolio_service = service
        omitted = []
        authenticated = brief_service._portfolio_binding(
            result,
            "000001",
            context,
            omitted,
        )
        assert authenticated is result.portfolio_binding
        assert omitted == []
        fact_set = SourceLinkedFactSet(
            fund_code="000001",
            facts=(
                _public_fact(
                    "identity_active_status",
                    {
                        "fund_code": "000001",
                        "fund_company": "公开基金公司",
                    },
                    context.budget.started_at,
                ),
                _public_fact(
                    "current_manager_team",
                    {"manager_name": "公开基金经理"},
                    context.budget.started_at,
                ),
                _public_fact(
                    "current_benchmark",
                    {"description": "公开基准"},
                    context.budget.started_at,
                ),
                _public_fact(
                    "holdings_industries",
                    {
                        "disclosure_scope": ("complete",),
                        "items": (
                            {
                                "asset_class": "stock",
                                "disclosed_weight": "100",
                                "rank": "1",
                                "security_code": "600000",
                                "security_name": "公开证券",
                            },
                        ),
                        "report_period": context.budget.started_at.date().isoformat(),
                    },
                    context.budget.started_at,
                ),
            ),
            official_events=(),
            missing_fields=(),
            conflicts=(),
            warnings=(),
        )
        fact_set.validate()
        d2 = build_d2_relationships(
            "000001",
            authenticated,
            {"000001": fact_set},
            context.budget.started_at,
            request_id=context.budget.request_id,
            request_mode=context.budget.mode,
        )
        assert any(
            item.relationship_type == "duplicate_holding_identity"
            for item in d2.relationships
        )
        assert d2.coverage.evidence_state is not BriefEvidenceState.INSUFFICIENT
        assert d2.holdings_coverage.evidence_state is not BriefEvidenceState.INSUFFICIENT
        expected_request = PortfolioWorkerRequest(
            1,
            context.budget.request_id,
            "portfolio_observation",
        )
        expected_accounts = (
            PortfolioAccount(
                "synthetic-account-1",
                "SYNTHETIC_NON_PERSONAL_1",
                context.budget.started_at,
            ),
            PortfolioAccount(
                "synthetic-account-2",
                "SYNTHETIC_NON_PERSONAL_2",
                context.budget.started_at,
            ),
        )
        expected_positions = tuple(
            PortfolioPosition(
                account.source_account_id,
                "000001",
                "SYNTHETIC_NON_PERSONAL_FUND",
                None,
                str(index),
                "1",
                None,
                None,
                context.budget.started_at,
            )
            for index, account in enumerate(expected_accounts, start=1)
        )
        expected_payload_sha256 = hashlib.sha256(
            encode_portfolio_success(
                expected_request,
                PortfolioObservationPayload(
                    context.budget.started_at,
                    expected_accounts,
                    expected_positions,
                ),
            )
        ).hexdigest()
        marker = json.loads(os.read(marker_read_fd, 4096).decode("ascii"))
        assert marker == {
            "contract": "kunjin_phase1_public_portfolio_used_v1",
            "fund_code": "000001",
            "observation_version": SYNTHETIC_OBSERVATION_VERSION,
            "payload_sha256": expected_payload_sha256,
            "request_id": context.budget.request_id,
            "run_id": "a" * 64,
            "schema_version": 1,
            "source_attempt_id": result.source_attempt_id,
        }
    finally:
        os.close(marker_read_fd)
        temporary.cleanup()


def test_capability_rejects_linked_fixture_and_never_falls_back(monkeypatch) -> None:
    temporary, _runtime_root, paths = _runtime()
    fixture_fd, marker_read_fd, marker_write_fd = _capability_fds(
        paths.logs.parent,
        unlink_fixture=False,
    )
    for key, value in _environment(fixture_fd, marker_write_fd).items():
        monkeypatch.setenv(key, value)
    try:
        with pytest.raises(ValueError, match="public acceptance"):
            load_public_acceptance_capability(paths, "000001")
    finally:
        os.close(fixture_fd)
        os.close(marker_read_fd)
        os.close(marker_write_fd)
        temporary.cleanup()


def test_build_context_selects_acceptance_service_only_for_matching_subject(
    monkeypatch,
) -> None:
    temporary, _runtime_root, paths = _runtime()
    fixture_fd, marker_read_fd, marker_write_fd = _capability_fds(paths.logs.parent)
    for key, value in _environment(fixture_fd, marker_write_fd).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("KUNJIN_DATA_DIR", str(paths.database.parent))
    monkeypatch.setenv("KUNJIN_STATE_DIR", str(paths.logs.parent))
    try:
        with patch("kunjin.security.keychain.KeychainTokenStore.load") as keychain_load:
            context = build_context(public_acceptance_subject="000001")
        assert type(context.brief_service._portfolio_service) is PublicAcceptancePortfolioService
        keychain_load.assert_not_called()
    finally:
        os.close(marker_read_fd)
        temporary.cleanup()


def test_process_tracking_run_id_alone_does_not_activate_fixture(monkeypatch) -> None:
    temporary, _runtime_root, paths = _runtime()
    monkeypatch.setenv(ACCEPTANCE_RUN_ID_ENV, "a" * 64)
    try:
        assert load_public_acceptance_capability(paths, None) is None
        assert os.environ[ACCEPTANCE_RUN_ID_ENV] == "a" * 64
    finally:
        temporary.cleanup()


def test_capability_rejects_named_marker_fifo(monkeypatch) -> None:
    temporary, runtime_root, paths = _runtime()
    fixture_fd, anonymous_read_fd, anonymous_write_fd = _capability_fds(paths.logs.parent)
    os.close(anonymous_read_fd)
    os.close(anonymous_write_fd)
    fifo_path = runtime_root / "named-marker"
    os.mkfifo(fifo_path, 0o600)
    fifo_read_fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    fifo_write_fd = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    for key, value in _environment(fixture_fd, fifo_write_fd).items():
        monkeypatch.setenv(key, value)
    try:
        with pytest.raises(ValueError, match="anonymous write-only pipe"):
            load_public_acceptance_capability(paths, "000001")
    finally:
        os.close(fifo_read_fd)
        os.close(fifo_write_fd)
        temporary.cleanup()
