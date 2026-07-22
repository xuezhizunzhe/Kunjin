from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from kunjin.diagnosis.research import public_diagnosis_payload
from kunjin.funds.peers.research import build_portfolio_overlap_from_weights
from kunjin.services.sync import SyncError


@dataclass(frozen=True)
class ManualPortfolioPosition:
    fund_code: str
    weight: Decimal


def thematic_exposure_observation(theme_name: str) -> dict[str, str]:
    """State the disclosure boundary when a portfolio has no identified theme fund."""

    if not isinstance(theme_name, str) or not (name := " ".join(theme_name.split())):
        raise ValueError("theme name is invalid")
    return {
        "state": "no_explicit_theme_fund_identified",
        "text": (
            f"当前组合未识别到明确的{name}主题持仓；其他主动或指数基金的间接暴露"
            "需带日期披露确认，未知部分不按零处理。"
        ),
    }


class PortfolioReviewService:
    def __init__(
        self,
        *,
        sync_service,
        diagnosis_service,
        disclosure_store,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        overlap_builder=build_portfolio_overlap_from_weights,
    ) -> None:
        self._sync_service = sync_service
        self._diagnosis_service = diagnosis_service
        self._disclosure_store = disclosure_store
        self._clock = clock
        self._overlap_builder = overlap_builder

    def synced(self) -> dict[str, object]:
        try:
            sync = self._sync_service.sync_portfolio(trigger="portfolio_review")
        except SyncError as error:
            return _sync_failure(error.code)
        if sync.positions == 0:
            return {
                "input_source": "sync_empty",
                "conclusion": "本次同步没有读取到持仓，暂不能做组合诊断。",
                "portfolio_overview": None,
                "observed_exposures": [],
                "risks_and_unknowns": ["同步结果为空，可能需要重新登录或手动提供持仓。"],
                "next_step": "可在本对话提供基金名称或代码及大概比例，进行临时诊断。",
                "conditional_guidance": _guidance(),
            }
        diagnosis = public_diagnosis_payload(self._diagnosis_service.diagnose())
        return {
            "input_source": "yangjibao_sync",
            "conclusion": "已基于本次同步持仓完成组合观察，不构成交易指令。",
            "portfolio_overview": diagnosis["concentration"],
            "observed_exposures": diagnosis["relationships"],
            "risks_and_unknowns": list(diagnosis["missing_evidence"]),
            "next_step": "结合公开研究和自身目标进行人工复核。",
            "diagnosis": diagnosis,
            "conditional_guidance": _guidance(),
        }

    def manual(self, positions: Sequence[ManualPortfolioPosition]) -> dict[str, object]:
        weights = _weights(positions)
        bundles = {
            code: self._disclosure_store.load_bundle(code) for code in sorted(weights)
        }
        overlap = self._overlap_builder(weights, bundles, self._clock())
        hhi = sum((weight * weight for weight in weights.values()), Decimal("0"))
        largest = max(weights.values())
        details = overlap["portfolio_overlap"]
        return {
            "input_source": "manual_temporary",
            "conclusion": "这是基于临时比例的组合观察，不会保存为真实持仓。",
            "portfolio_overview": {
                "position_count": len(weights),
                "hhi": format(hhi, "f"),
                "largest_position_share": format(largest, "f"),
                "value_basis": "manual",
            },
            "observed_exposures": {
                "securities": details.get("securities", []),
                "industries": details.get("industries", []),
            },
            "risks_and_unknowns": [
                "临时比例来自用户输入，不是同步持仓。",
                *overlap["warnings"],
            ],
            "next_step": "补充带日期的基金披露资料后，可进一步核对重叠和行业关联。",
            "conditional_guidance": _guidance(),
        }


def _weights(positions: Sequence[ManualPortfolioPosition]) -> Mapping[str, Decimal]:
    if not 2 <= len(positions) <= 32:
        raise ValueError("manual portfolio requires two to thirty-two positions")
    result = {}
    for position in positions:
        if (
            type(position) is not ManualPortfolioPosition
            or len(position.fund_code) != 6
            or not position.fund_code.isdigit()
            or not position.weight.is_finite()
            or position.weight <= 0
        ):
            raise ValueError("manual portfolio position is invalid")
        if position.fund_code in result:
            raise ValueError("manual portfolio fund codes must be unique")
        result[position.fund_code] = position.weight
    if sum(result.values(), Decimal("0")) != Decimal("1"):
        raise ValueError("manual portfolio weights must total 100 percent")
    return result


def _sync_failure(code: str) -> dict[str, object]:
    labels = {
        "authentication_required": "登录状态失效或需要重新登录。",
        "source_unavailable": "持仓来源暂时不可用。",
        "validation_failure": "持仓数据结构无法验证。",
        "rate_limited": "持仓来源暂时限流。",
    }
    return {
        "input_source": "sync_failed",
        "conclusion": labels.get(code, "持仓同步失败，暂不能使用本次数据做诊断。"),
        "portfolio_overview": None,
        "observed_exposures": [],
        "risks_and_unknowns": ["不会使用旧缓存替代本次同步结果。"],
        "next_step": "可重新登录后再次手动同步，或在本对话提供基金名称或代码及大概比例。",
        "manual_fallback": {"available": True},
        "conditional_guidance": _guidance(),
    }


def _guidance() -> dict[str, object]:
    return {
        "label": "条件性建议",
        "text": "组合观察仅供继续研究和人工复核，不构成买卖指令。",
        "action_authorized": False,
        "automatic_trade": False,
    }
