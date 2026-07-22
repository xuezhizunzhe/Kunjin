from __future__ import annotations


def build_review_triggers(fund_code: str) -> dict[str, object]:
    """Return on-demand review conditions without scheduling or market monitoring."""

    return {
        "fund_code": fund_code,
        "conclusion": {
            "text": "以下是需要主动重新复核的条件，不是自动提醒、卖出信号或交易指令。",
            "review_now": False,
        },
        "review_triggers": [
            {
                "category": "个人情况变化",
                "conditions": [
                    "应急资金不足或这笔钱变为近期可能使用",
                    "预计持有期限缩短，或可承受波动程度下降",
                    "新增风险资产后，整体主题或单一基金集中度明显上升",
                ],
                "next_step": "先更新投资者画像，再结合组合复核这只基金。",
            },
            {
                "category": "基金公开资料变化",
                "conditions": [
                    "基金经理、基准、费用、合同或申赎安排出现正式公告变化",
                    "新的定期报告、持仓披露或规模资料发布",
                    "基金清盘、终止或重大申赎限制等公开事件",
                ],
                "next_step": "重新做基金复核，并保留公告来源、URL、发布日期和披露期。",
            },
            {
                "category": "市场与行业证据变化",
                "conditions": [
                    "与该基金有日期披露持仓、基准或指数关联的行业出现重要公开变化",
                    "原先支持判断的公开事实被新资料反证或证据明显过期",
                ],
                "next_step": "先做公开研究，再把有来源的事实与原有判断逐项对照。",
            },
        ],
        "on_demand_workflow": [
            "基金公开事实和市场线索：fund review",
            "组合集中度与披露重叠：portfolio review",
            "行业或市场变化：research summary 或 research panorama",
        ],
        "evidence_boundary": [
            "基金和行业关联只依据有日期的披露持仓、基准或指数，不代表实时完整持仓。",
            "单日涨跌或媒体叙事本身不足以证明应买卖。",
            "资料不足时，应补公开证据或个人约束，不把未知当作低风险。",
        ],
        "action_boundary": {
            "automatic_monitoring": False,
            "automatic_trade": False,
            "exact_amount_available": False,
        },
    }
