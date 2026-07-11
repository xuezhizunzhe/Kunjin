from __future__ import annotations

import unittest
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from kunjin.funds.html import FundParseError
from kunjin.funds.models import (
    AssetType,
    DocumentKind,
    FeeType,
    FundBenchmark,
    FundAnnouncement,
    FundFeeRule,
    FundIdentity,
    FundHolding,
    FundIndustryExposure,
    FundManagerTenure,
    FundShareClass,
    FundSizeObservation,
)
from kunjin.funds.parsers import (
    parse_announcements,
    parse_basic_profile,
    parse_fee_schedule,
    parse_industry_exposure,
    parse_manager_history,
    parse_quarterly_holdings,
    parse_size_history,
)
from kunjin.funds.sources import TextResponse


FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds"


def response(name: str, path: str) -> TextResponse:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return TextResponse(
        requested_url=f"https://fundf10.eastmoney.com/{path}",
        final_url=f"https://fundf10.eastmoney.com/{path}",
        text=text,
        retrieved_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
        checksum="a" * 64,
        content_type="text/html; charset=utf-8",
    )


class BasicProfileParserTest(unittest.TestCase):
    def test_parses_realistic_alternating_profile_rows(self) -> None:
        base = response("basic_profile.html", "jbgk_519755.html")
        realistic = """
        <p>成立日期：2015-06-02 基金经理：示例经理 类型：混合型</p>
        <table>
          <tr><td>基金全称</td><td>交银施罗德多策略回报灵活配置混合型证券投资基金</td><td>基金简称</td><td>交银多策略回报灵活配置混合A</td></tr>
          <tr><td>基金代码</td><td>519755（前端）</td><td>基金类型</td><td>混合型</td></tr>
          <tr><td>成立日期/规模</td><td>2015年06月02日 / 31.957亿份</td><td>资产规模</td><td>12.34亿元</td></tr>
          <tr><td>基金管理人</td><td>交银施罗德基金管理有限公司</td><td>基金状态</td><td>开放申购 开放赎回</td></tr>
        </table>
        """

        section = parse_basic_profile(
            TextResponse(**{**base.__dict__, "text": realistic}), "519755"
        )

        identity = next(item for item in section.records if isinstance(item, FundIdentity))
        self.assertEqual(identity.fund_name, "交银多策略回报灵活配置混合A")
        self.assertEqual(identity.established_date, date(2015, 6, 2))
        self.assertEqual(identity.status, "active")

    def test_parses_identity_share_classes_and_verbatim_benchmark(self) -> None:
        section = parse_basic_profile(
            response("basic_profile.html", "jbgk_519755.html"), "519755"
        )

        identity = next(record for record in section.records if isinstance(record, FundIdentity))
        share_classes = tuple(
            record for record in section.records if isinstance(record, FundShareClass)
        )
        benchmarks = tuple(
            record for record in section.records if isinstance(record, FundBenchmark)
        )

        self.assertEqual(section.section, DocumentKind.BASIC_PROFILE.value)
        self.assertEqual(section.state, "success")
        self.assertEqual(identity.fund_code, "519755")
        self.assertEqual(identity.fund_name, "KunJin稳健成长混合A")
        self.assertEqual(identity.status, "active")
        self.assertEqual(identity.fund_type, "混合型")
        self.assertEqual(identity.established_date, date(2023, 6, 15))
        self.assertEqual(identity.manager_name, "KunJin合成基金管理有限公司")
        self.assertEqual(
            [(item.related_fund_code, item.share_class) for item in share_classes],
            [("519755", "A"), ("519756", "C")],
        )
        self.assertEqual(
            benchmarks[0].description,
            "沪深300指数收益率 × 60% + 中债综合指数收益率 × 40%",
        )
        self.assertEqual(section.source.document_kind, DocumentKind.BASIC_PROFILE)
        self.assertEqual(section.source.source_name, "eastmoney_f10")
        self.assertEqual(section.source.source_tier, 2)
        self.assertEqual(section.source.publisher, "东方财富")
        self.assertIsNone(section.source.id)
        self.assertEqual(len(section.source.checksum), 64)
        self.assertTrue(
            all(
                character in "0123456789abcdef"
                for character in section.source.checksum
            )
        )
        self.assertNotEqual(section.source.checksum, "a" * 64)
        repeated = parse_basic_profile(
            response("basic_profile.html", "jbgk_519755.html"), "519755"
        )
        self.assertEqual(section.source.checksum, repeated.source.checksum)
        self.assertTrue(all(record.source_document_id is None for record in section.records))

    def test_requires_explicit_sibling_code_link_and_exact_base_name(self) -> None:
        text = """
        <dl>
          <dt>基金代码</dt><dd>519755</dd>
          <dt>基金简称</dt><dd>KunJin稳健成长混合A</dd>
          <dt>基金状态</dt><dd>正常</dd>
        </dl>
        <a href="/not-a-fund.html">KunJin稳健成长混合C</a>
        <a href="/jbgk_519756.html">KunJin稳健成长增强混合C</a>
        """
        source = response("basic_profile.html", "jbgk_519755.html")
        section = parse_basic_profile(TextResponse(**{**source.__dict__, "text": text}), "519755")

        self.assertFalse(any(isinstance(record, FundShareClass) for record in section.records))

    def test_rejects_conflicting_page_code(self) -> None:
        source = response("basic_profile.html", "jbgk_519755.html")
        conflicting = TextResponse(
            **{**source.__dict__, "text": source.text.replace("519755", "519754")}
        )

        with self.assertRaises(FundParseError) as raised:
            parse_basic_profile(conflicting, "519755")

        self.assertEqual(raised.exception.code, "identity_conflict")


class ManagerHistoryParserTest(unittest.TestCase):
    def test_splits_explicit_co_manager_names_into_individual_tenures(self) -> None:
        source = response("manager_history.html", "jjjl_519755.html")
        text = """
        <table>
          <tr><th>基金经理</th><th>任职日期</th><th>离任日期</th></tr>
          <tr><td><a>王艺伟</a>、<a>姜承操</a> <a>徐森洲</a></td><td>2025-01-09</td><td>至今</td></tr>
        </table>
        """

        section = parse_manager_history(
            TextResponse(**{**source.__dict__, "text": text}), "519755"
        )

        self.assertEqual(
            [item.manager_name for item in section.records],
            ["王艺伟", "姜承操", "徐森洲"],
        )

    def test_parses_overlapping_current_and_former_manager_tenures(self) -> None:
        section = parse_manager_history(
            response("manager_history.html", "jjjl_519755.html"), "519755"
        )

        self.assertEqual(section.section, DocumentKind.MANAGER_HISTORY.value)
        self.assertEqual(section.state, "success")
        self.assertEqual(
            section.records,
            (
                FundManagerTenure("519755", "张三", date(2024, 1, 1), None, None),
                FundManagerTenure("519755", "李四", date(2024, 6, 1), None, None),
                FundManagerTenure(
                    "519755", "王五", date(2023, 1, 1), date(2024, 3, 31), None
                ),
            ),
        )
        self.assertFalse(any(hasattr(record, "historical_return") for record in section.records))
        self.assertEqual(section.source.document_kind, DocumentKind.MANAGER_HISTORY)
        self.assertEqual(section.source.source_tier, 2)

    def test_explicit_empty_history_is_not_disclosed(self) -> None:
        source = response("manager_history.html", "jjjl_519755.html")
        empty = TextResponse(
            **{**source.__dict__, "text": "<p>暂无基金经理任职记录</p>"}
        )

        section = parse_manager_history(empty, "519755")

        self.assertEqual(section.state, "not_disclosed")
        self.assertEqual(section.records, ())

    def test_structured_history_takes_priority_over_empty_page_copy(self) -> None:
        source = response("manager_history.html", "jjjl_519755.html")
        mixed = TextResponse(
            **{**source.__dict__, "text": source.text + "<p>暂无基金经理任职记录</p>"}
        )

        section = parse_manager_history(mixed, "519755")

        self.assertEqual(section.state, "success")
        self.assertEqual(len(section.records), 3)

    def test_unlabeled_manager_text_is_not_guessed(self) -> None:
        source = response("manager_history.html", "jjjl_519755.html")
        unlabeled = TextResponse(
            **{**source.__dict__, "text": "<p>张三于2024年开始管理本基金</p>"}
        )

        with self.assertRaises(FundParseError) as raised:
            parse_manager_history(unlabeled, "519755")

        self.assertEqual(raised.exception.code, "missing_manager_history")


class FundFeeParserTest(unittest.TestCase):
    def test_parses_realistic_fee_tables_and_preserves_both_rate_columns(self) -> None:
        base = response("fee_schedule.html", "jjfl_519755.html")
        realistic = """
        <h3>运作费用</h3><table>
          <tr><td>管理费率</td><td>1.20%（每年）</td><td>托管费率</td><td>0.20%（每年）</td></tr>
          <tr><td>销售服务费率</td><td>0.00%（每年）</td><td>其他费用</td><td>--</td></tr>
        </table>
        <table><caption>申购费率（前端）</caption>
          <tr><th>适用金额</th><th><span>原费率</span>|<span>天天基金优惠费率</span></th></tr>
          <tr><td>小于50万元</td><td>1.50% | 0.15%</td></tr>
          <tr><td>大于等于50万元，小于100万元</td><td>1.00% | 0.10%</td></tr>
          <tr><td>大于等于100万元</td><td>每笔1000元</td></tr>
        </table>
        <table><caption>赎回费率</caption>
          <tr><th>适用期限</th><th>费率</th></tr>
          <tr><td>小于7天</td><td>1.50%</td></tr>
          <tr><td>大于等于7天，小于30天</td><td>0.75%</td></tr>
          <tr><td>大于等于30天</td><td>0.00%</td></tr>
        </table>
        """

        section = parse_fee_schedule(
            TextResponse(**{**base.__dict__, "text": realistic}), "519755"
        )

        subscriptions = [r for r in section.records if r.fee_type is FeeType.SUBSCRIPTION]
        self.assertEqual(
            [r.rate for r in subscriptions],
            [Decimal("1.50"), Decimal("0.15"), Decimal("1.00"), Decimal("0.10"), None],
        )
        self.assertIn("原费率", subscriptions[0].raw_rule_text)
        self.assertIn("天天基金优惠费率", subscriptions[1].raw_rule_text)
        self.assertIsNone(subscriptions[0].effective_from)
        self.assertEqual(subscriptions[0].amount_max, Decimal("500000"))
        self.assertEqual(subscriptions[2].amount_min, Decimal("500000"))
        self.assertEqual(subscriptions[2].amount_max, Decimal("1000000"))

    def test_parses_tiered_fee_rules_without_collapsing_them(self) -> None:
        section = parse_fee_schedule(
            response("fee_schedule.html", "jjfl_519755.html"), "519755"
        )

        self.assertEqual(section.section, DocumentKind.FEE_SCHEDULE.value)
        self.assertEqual(section.state, "success")
        self.assertEqual(section.source.document_kind, DocumentKind.FEE_SCHEDULE)
        self.assertTrue(all(isinstance(item, FundFeeRule) for item in section.records))
        self.assertEqual(
            section.records,
            (
                FundFeeRule(
                    "519755", FeeType.MANAGEMENT, None, rate=Decimal("1.20"),
                    rule_order=1, effective_from=date(2024, 1, 1),
                    raw_rule_text="管理费 | 全部 | 不分档 | 1.20% | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.CUSTODY, None, rate=Decimal("0.20"),
                    rule_order=2, effective_from=date(2024, 1, 1),
                    raw_rule_text="托管费 | 全部 | 不分档 | 0.20% | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.SALES_SERVICE, None, share_class="A",
                    rate=Decimal("0"), rule_order=3,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text="销售服务费 | A类 | 不分档 | 不收取 | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.SALES_SERVICE, None, share_class="C",
                    rate=Decimal("0.40"), rule_order=4,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text="销售服务费 | C类 | 不分档 | 0.40% | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.SUBSCRIPTION, None, share_class="A",
                    rate=Decimal("1.50"), amount_max=Decimal("1000000"),
                    rule_order=5, effective_from=date(2024, 1, 1),
                    raw_rule_text="申购费 | A类 | 申购金额<100万元 | 1.50% | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.SUBSCRIPTION, None, share_class="A",
                    rate=Decimal("0.80"), amount_min=Decimal("1000000"),
                    amount_max=Decimal("5000000"), rule_order=6,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text=(
                        "申购费 | A类 | 100万元<=申购金额<500万元 | 0.80% | 2024-01-01"
                    ),
                ),
                FundFeeRule(
                    "519755", FeeType.SUBSCRIPTION, None, share_class="A",
                    fixed_amount=Decimal("1000"), amount_min=Decimal("5000000"),
                    rule_order=7, effective_from=date(2024, 1, 1),
                    raw_rule_text=(
                        "申购费 | A类 | 申购金额>=500万元 | 每笔1000元 | 2024-01-01"
                    ),
                ),
                FundFeeRule(
                    "519755", FeeType.REDEMPTION, None, share_class="A",
                    rate=Decimal("1.50"), holding_days_max=6, rule_order=8,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text="赎回费 | A类 | 持有天数<7天 | 1.50% | 2024-01-01",
                ),
                FundFeeRule(
                    "519755", FeeType.REDEMPTION, None, share_class="A",
                    rate=Decimal("0.75"), holding_days_min=7,
                    holding_days_max=29, rule_order=9,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text=(
                        "赎回费 | A类 | 7天<=持有天数<30天 | 0.75% | 2024-01-01"
                    ),
                ),
                FundFeeRule(
                    "519755", FeeType.REDEMPTION, None, share_class="A",
                    rate=Decimal("0"), holding_days_min=30, rule_order=10,
                    effective_from=date(2024, 1, 1),
                    raw_rule_text="赎回费 | A类 | 持有天数>=30天 | 不收取 | 2024-01-01",
                ),
            ),
        )

    def test_rejects_unknown_fee_units_and_ambiguous_intervals(self) -> None:
        base = response("fee_schedule.html", "jjfl_519755.html")
        cases = (
            base.text.replace("每笔1000元", "每笔1000美元"),
            base.text.replace("持有天数&lt;7天", "持有约7天"),
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(FundParseError) as raised:
                    parse_fee_schedule(TextResponse(**{**base.__dict__, "text": text}), "519755")
                self.assertEqual(raised.exception.code, "ambiguous_fee_rule")


class FundSizeParserTest(unittest.TestCase):
    def test_parses_realistic_js_wrapped_size_data_without_publication_date(self) -> None:
        base = response("size_history.html", "FundArchivesDatas.aspx?type=gmbd")
        payload = (
            'var gmbd_apidata={content:"","data":'
            '[{"FSRQ":"2026-06-30","NETNAV":"269047546.82",'
            '"QMZFE":"161062195.44"},'
            '{"FSRQ":"2015-06-30","NETNAV":"8506326850.04"}]};'
        )

        section = parse_size_history(TextResponse(**{**base.__dict__, "text": payload}), "519755")

        self.assertEqual(section.records[0].net_assets, Decimal("269047546.82"))
        self.assertEqual(section.records[0].total_shares, Decimal("161062195.44"))
        self.assertIsNone(section.records[0].published_at)
        self.assertEqual(section.records[1].net_assets, Decimal("8506326850.04"))
        self.assertIsNone(section.records[1].total_shares)

    def test_normalizes_asset_and_share_units(self) -> None:
        section = parse_size_history(
            response("size_history.html", "gmbd_519755.html"), "519755"
        )

        self.assertEqual(section.section, DocumentKind.SIZE_HISTORY.value)
        self.assertEqual(section.state, "success")
        self.assertEqual(section.source.document_kind, DocumentKind.SIZE_HISTORY)
        self.assertEqual(
            section.records,
            (
                FundSizeObservation(
                    "519755", date(2026, 6, 30), Decimal("1234000000"),
                    Decimal("987650000"),
                    datetime(2026, 7, 10, 9, 30, tzinfo=timezone.utc), None,
                ),
                FundSizeObservation(
                    "519755", date(2026, 3, 31), Decimal("1180000000"),
                    Decimal("950000000"),
                    datetime(2026, 4, 20, tzinfo=ZoneInfo("Asia/Shanghai")), None,
                ),
            ),
        )

    def test_rejects_unknown_size_units(self) -> None:
        base = response("size_history.html", "gmbd_519755.html")
        malformed = TextResponse(
            **{**base.__dict__, "text": base.text.replace("12.34亿元", "12.34亿美元")}
        )

        with self.assertRaises(FundParseError) as raised:
            parse_size_history(malformed, "519755")

        self.assertEqual(raised.exception.code, "ambiguous_fee_rule")


class FundHoldingParserTest(unittest.TestCase):
    def test_parses_realistic_js_wrapped_top_ten_stock_holdings(self) -> None:
        base = response("quarterly_holdings.html", "FundArchivesDatas.aspx?type=jjcc")
        content = """
          <h4>2026年2季度股票投资明细</h4><table>
          <tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>占净值比例</th><th>持股数（万股）</th><th>持仓市值（万元）</th></tr>
          <tr><td>1</td><td>000001</td><td>平安银行</td><td>6.25%</td><td>12.30</td><td>456.70</td></tr>
          </table>"""
        wrapped = "var apidata={content:" + json.dumps(content, ensure_ascii=False) + "};"

        section = parse_quarterly_holdings(TextResponse(**{**base.__dict__, "text": wrapped}), "519755")

        holding = section.records[0]
        self.assertEqual(holding.report_period, date(2026, 6, 30))
        self.assertIsNone(holding.published_at)
        self.assertEqual(holding.disclosure_scope, "top10")
        self.assertEqual(holding.shares, Decimal("123000"))
        self.assertEqual(holding.market_value, Decimal("4567000"))

    def test_parses_realistic_industry_json_without_guessing_standard_version(self) -> None:
        base = response("industry_exposure.html", "f10/HYPZ/?fundCode=519755&year=2026")
        payload = json.dumps(
            {
                "Data": {
                    "QuarterInfos": [
                        {
                            "JZRQ": "2026-06-30",
                            "HYPZInfo": [
                                {
                                    "FSRQ": "2026-06-30",
                                    "HYDM": "J66",
                                    "HYMC": "货币金融服务",
                                    "SZ": "123.45",
                                    "ZJZBL": "12.50",
                                }
                            ],
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )

        section = parse_industry_exposure(TextResponse(**{**base.__dict__, "text": payload}), "519755")

        exposure = section.records[0]
        self.assertEqual(exposure.classification_standard, "证监会行业分类")
        self.assertIsNone(exposure.published_at)
        self.assertEqual(exposure.market_value, Decimal("1234500"))

    def test_parses_two_quarters_without_merging_or_losing_code_format(self) -> None:
        section = parse_quarterly_holdings(
            response("quarterly_holdings.html", "ccmx_519755.html"), "519755"
        )

        self.assertEqual(section.section, DocumentKind.QUARTERLY_HOLDINGS.value)
        self.assertEqual(section.state, "success")
        self.assertTrue(all(isinstance(item, FundHolding) for item in section.records))
        self.assertEqual(
            [(item.report_period, item.rank) for item in section.records],
            [(date(2026, 6, 30), 1), (date(2026, 6, 30), 2), (date(2026, 3, 31), 1)],
        )
        holding = section.records[0]
        self.assertEqual(
            holding.published_at,
            datetime(2026, 7, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertGreater(holding.published_at.date(), holding.report_period)
        self.assertEqual(holding.security_code, "000001")
        self.assertEqual(holding.asset_type, AssetType.STOCK)
        self.assertEqual(holding.weight, Decimal("6.25"))
        self.assertEqual(holding.disclosure_scope, "top10")
        self.assertNotIn("complete", {item.disclosure_scope for item in section.records})
        self.assertEqual(section.records[1].security_code, "019547")
        self.assertEqual(section.records[1].asset_type, AssetType.BOND)

    def test_parses_industry_standard_and_weights(self) -> None:
        section = parse_industry_exposure(
            response("industry_exposure.html", "hytz_519755.html"), "519755"
        )

        self.assertEqual(section.section, DocumentKind.INDUSTRY_EXPOSURE.value)
        self.assertTrue(all(isinstance(item, FundIndustryExposure) for item in section.records))
        self.assertEqual(section.records[0].classification_standard, "申万一级行业分类(2021)")
        self.assertEqual(section.records[0].industry_code, "801780")
        self.assertEqual(section.records[0].industry_name, "银行")
        self.assertEqual(section.records[0].weight, Decimal("12.50"))

    def test_rejects_out_of_range_holding_and_industry_weights(self) -> None:
        cases = (
            (
                "quarterly_holdings.html",
                "ccmx_519755.html",
                "6.25%",
                "100.01%",
                parse_quarterly_holdings,
            ),
            (
                "industry_exposure.html",
                "hytz_519755.html",
                "12.50%",
                "-0.01%",
                parse_industry_exposure,
            ),
        )
        for fixture, path, old, new, parser in cases:
            base = response(fixture, path)
            malformed = TextResponse(
                **{**base.__dict__, "text": base.text.replace(old, new)}
            )
            with self.subTest(fixture=fixture), self.assertRaises(FundParseError) as raised:
                parser(malformed, "519755")
            self.assertEqual(raised.exception.code, "invalid_disclosure_weight")

    def test_explicit_empty_holdings_and_industry_are_not_disclosed(self) -> None:
        cases = (
            (
                "quarterly_holdings.html",
                "ccmx_519755.html",
                "<p>暂无持仓数据</p>",
                parse_quarterly_holdings,
            ),
            (
                "industry_exposure.html",
                "hytz_519755.html",
                "<p>暂无行业配置</p>",
                parse_industry_exposure,
            ),
        )
        for fixture, path, text, parser in cases:
            base = response(fixture, path)
            empty = TextResponse(**{**base.__dict__, "text": text})

            with self.subTest(fixture=fixture):
                section = parser(empty, "519755")

            self.assertEqual(section.state, "not_disclosed")
            self.assertEqual(section.records, ())

    def test_structured_disclosures_take_priority_over_empty_page_copy(self) -> None:
        cases = (
            (
                "quarterly_holdings.html",
                "ccmx_519755.html",
                "<p>暂无持仓数据</p>",
                parse_quarterly_holdings,
            ),
            (
                "industry_exposure.html",
                "hytz_519755.html",
                "<p>暂无行业配置</p>",
                parse_industry_exposure,
            ),
        )
        for fixture, path, suffix, parser in cases:
            base = response(fixture, path)
            mixed = TextResponse(**{**base.__dict__, "text": base.text + suffix})

            with self.subTest(fixture=fixture):
                section = parser(mixed, "519755")

            self.assertEqual(section.state, "success")
            self.assertGreater(len(section.records), 0)

    def test_unstructured_pages_are_not_treated_as_empty_disclosures(self) -> None:
        cases = (
            (
                "quarterly_holdings.html",
                "ccmx_519755.html",
                parse_quarterly_holdings,
                "missing_quarterly_holdings",
            ),
            (
                "industry_exposure.html",
                "hytz_519755.html",
                parse_industry_exposure,
                "missing_industry_exposure",
            ),
        )
        for fixture, path, parser, error_code in cases:
            base = response(fixture, path)
            unstructured = TextResponse(
                **{**base.__dict__, "text": "<p>数据加载中</p>"}
            )
            with self.subTest(fixture=fixture), self.assertRaises(
                FundParseError
            ) as raised:
                parser(unstructured, "519755")
            self.assertEqual(raised.exception.code, error_code)


class FundAnnouncementParserTest(unittest.TestCase):
    def test_parses_realistic_announcement_json_as_tier_two_index(self) -> None:
        base = response("announcements.html", "f10/JJGG?fundcode=519755")
        payload = json.dumps({"Data": [{"TITLE": "交银多策略回报灵活配置混合型证券投资基金2025年第4季度报告", "NEWCATEGORY": "3", "PUBLISHDATEDesc": "2026-01-21", "ATTACHTYPE": ".pdf", "ID": "AN202601210001"}]}, ensure_ascii=False)

        section = parse_announcements(TextResponse(**{**base.__dict__, "text": payload}), "519755", "交银施罗德基金管理有限公司")

        announcement = section.records[0]
        self.assertEqual(announcement.category, "定期报告")
        self.assertEqual(announcement.publisher, "东方财富公告索引")
        self.assertEqual(announcement.source_tier, 2)
        self.assertTrue(announcement.url.startswith("https://"))

    def test_classifies_each_announcement_link_independently(self) -> None:
        section = parse_announcements(
            response("announcements.html", "jjgg_519755.html"),
            "519755",
            "交银施罗德基金管理有限公司",
        )

        self.assertEqual(section.section, DocumentKind.ANNOUNCEMENT.value)
        self.assertEqual(section.source.source_tier, 2)
        self.assertTrue(all(isinstance(item, FundAnnouncement) for item in section.records))
        official, discovery = section.records
        self.assertEqual(official.category, "定期报告")
        self.assertEqual(official.publisher, "交银施罗德基金管理有限公司")
        self.assertEqual(
            official.published_at,
            datetime(2026, 7, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertEqual(official.source_tier, 1)
        self.assertEqual(discovery.source_tier, 2)
        self.assertEqual(
            discovery.published_at.utcoffset(),
            ZoneInfo("Asia/Shanghai").utcoffset(discovery.published_at),
        )

    def test_publisher_mismatch_keeps_audited_domain_at_tier_two(self) -> None:
        base = response("announcements.html", "jjgg_519755.html")
        mismatched = TextResponse(
            **{
                **base.__dict__,
                "text": base.text.replace(
                    "交银施罗德基金管理有限公司", "其他基金管理有限公司"
                ),
            }
        )

        section = parse_announcements(
            mismatched, "519755", "交银施罗德基金管理有限公司"
        )

        self.assertEqual([item.source_tier for item in section.records], [2, 2])

    def test_explicit_empty_announcements_are_not_disclosed(self) -> None:
        base = response("announcements.html", "jjgg_519755.html")
        empty = TextResponse(**{**base.__dict__, "text": "<p>暂无基金公告</p>"})

        section = parse_announcements(
            empty, "519755", "交银施罗德基金管理有限公司"
        )

        self.assertEqual(section.state, "not_disclosed")
        self.assertEqual(section.records, ())

    def test_structured_announcements_take_priority_over_empty_page_copy(self) -> None:
        base = response("announcements.html", "jjgg_519755.html")
        mixed = TextResponse(
            **{**base.__dict__, "text": base.text + "<p>暂无基金公告</p>"}
        )

        section = parse_announcements(
            mixed, "519755", "交银施罗德基金管理有限公司"
        )

        self.assertEqual(section.state, "success")
        self.assertEqual(len(section.records), 2)

    def test_unstructured_announcement_page_is_not_treated_as_empty(self) -> None:
        base = response("announcements.html", "jjgg_519755.html")
        unstructured = TextResponse(
            **{**base.__dict__, "text": "<p>公告加载中</p>"}
        )

        with self.assertRaises(FundParseError) as raised:
            parse_announcements(
                unstructured, "519755", "交银施罗德基金管理有限公司"
            )

        self.assertEqual(raised.exception.code, "missing_announcements")


if __name__ == "__main__":
    unittest.main()
