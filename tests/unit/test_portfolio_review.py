from __future__ import annotations

from decimal import Decimal

from kunjin.portfolio_review import (
    ManualPortfolioPosition,
    PortfolioReviewService,
    thematic_exposure_observation,
)
from kunjin.services.sync import SyncError


def test_manual_review_never_synchronizes() -> None:
    class MustNotSync:
        def sync_portfolio(self, **_kwargs):
            raise AssertionError("manual review must not synchronize")

    class Store:
        def load_bundle(self, code):
            return {"fund": code}

    review = PortfolioReviewService(
        sync_service=MustNotSync(),
        diagnosis_service=None,
        disclosure_store=Store(),
        overlap_builder=lambda weights, _bundles, _now: {
            "portfolio_overlap": {"securities": [], "industries": []},
            "warnings": [],
        },
    ).manual(
        (
            ManualPortfolioPosition("123456", Decimal("0.60")),
            ManualPortfolioPosition("654321", Decimal("0.40")),
        )
    )

    assert review["input_source"] == "manual_temporary"
    assert review["portfolio_overview"]["value_basis"] == "manual"
    assert review["conditional_guidance"]["automatic_trade"] is False


def test_synced_review_returns_manual_fallback_on_login_failure() -> None:
    class FailingSync:
        def sync_portfolio(self, **_kwargs):
            raise SyncError("authentication_required", "private details")

    review = PortfolioReviewService(
        sync_service=FailingSync(), diagnosis_service=None, disclosure_store=None
    ).synced()

    assert review["input_source"] == "sync_failed"
    assert review["manual_fallback"] == {"available": True}
    assert "private details" not in str(review)


def test_unidentified_theme_is_not_described_as_zero_underlying_exposure() -> None:
    result = thematic_exposure_observation("电力")

    assert result["state"] == "no_explicit_theme_fund_identified"
    assert "未识别到明确的电力主题持仓" in result["text"]
    assert "间接暴露需带日期披露确认" in result["text"]
    assert "零暴露" not in result["text"]
