from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from kunjin.funds.html import (
    FundParseError,
    extract_labeled_values,
    extract_links,
    parse_tables,
)
from kunjin.funds.models import (
    AssetType,
    DocumentKind,
    FeeType,
    FundAnnouncement,
    FundBenchmark,
    FundFeeRule,
    FundHolding,
    FundIdentity,
    FundIndustryExposure,
    FundManagerTenure,
    FundShareClass,
    FundSizeObservation,
    SourceDocument,
)
from kunjin.funds.sources import TextResponse, classify_source

FUND_CODE_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SHARE_CLASS_PATTERN = re.compile(r"([AC])(?:类份额|类)?$", re.IGNORECASE)
DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日")
CURRENT_TENURE_VALUES = frozenset({"", "至今", "现任", "--", "-"})
NO_PROFILE_PHRASES = frozenset({"暂无基金基本资料", "暂无基本资料"})
NO_MANAGER_PHRASES = frozenset({"暂无基金经理任职记录", "暂无基金经理记录"})
NO_HOLDING_PHRASES = frozenset({"暂无持仓数据"})
NO_INDUSTRY_PHRASES = frozenset({"暂无行业配置"})
NO_ANNOUNCEMENT_PHRASES = frozenset({"暂无基金公告"})
NORMALIZATION_CONTRACT_VERSION = "2"


@dataclass(frozen=True)
class ParsedSection:
    section: str
    source: SourceDocument
    records: Tuple[object, ...]
    state: str
    warnings: Tuple[str, ...] = ()
    conflicts: Tuple[str, ...] = ()


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.ignored_tag: Optional[str] = None
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        tag = tag.lower()
        if self.ignored_tag is not None:
            if tag == self.ignored_tag:
                self.ignored_depth += 1
            return
        if tag in {"script", "style", "iframe"}:
            self.ignored_tag = tag
            self.ignored_depth = 1

    def handle_endtag(self, tag: str) -> None:
        if self.ignored_tag == tag.lower():
            self.ignored_depth -= 1
            if self.ignored_depth <= 0:
                self.ignored_tag = None
                self.ignored_depth = 0

    def handle_data(self, data: str) -> None:
        if self.ignored_tag is None:
            self.parts.append(data)


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _visible_text(value: str) -> str:
    parser = _VisibleTextParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception as exc:
        raise FundParseError("malformed_html") from exc
    return _normalize_text(" ".join(parser.parts))


def _has_explicit_phrase(text: str, phrases: Sequence[str]) -> bool:
    visible = _visible_text(text).replace(" ", "")
    return any(phrase.replace(" ", "") in visible for phrase in phrases)


def _source(response: TextResponse, fund_code: str, kind: DocumentKind) -> SourceDocument:
    titles = {
        DocumentKind.BASIC_PROFILE: "基金基本资料",
        DocumentKind.MANAGER_HISTORY: "基金经理任职记录",
        DocumentKind.FEE_SCHEDULE: "基金费率",
        DocumentKind.SIZE_HISTORY: "基金规模变动",
        DocumentKind.QUARTERLY_HOLDINGS: "基金季度持仓",
        DocumentKind.INDUSTRY_EXPOSURE: "基金行业配置",
        DocumentKind.ANNOUNCEMENT: "基金公告",
    }
    versioned_checksum = hashlib.sha256(
        f"{NORMALIZATION_CONTRACT_VERSION}:{response.checksum}".encode("ascii")
    ).hexdigest()
    source = SourceDocument(
        id=None,
        fund_code=fund_code,
        document_kind=kind,
        title=titles[kind],
        url=response.final_url,
        source_name=(
            "eastmoney_api"
            if (urlparse(response.final_url).hostname or "").lower() == "api.fund.eastmoney.com"
            else "eastmoney_f10"
        ),
        source_tier=2,
        publisher=("东方财富公告索引" if kind is DocumentKind.ANNOUNCEMENT else "东方财富"),
        published_at=None,
        retrieved_at=response.retrieved_at,
        checksum=versioned_checksum,
    )
    source.validate()
    return source


def _values_for_labels(
    labeled: Dict[str, List[str]], labels: Sequence[str]
) -> List[str]:
    result: List[str] = []
    for label in labels:
        result.extend(labeled.get(label, ()))
    return [_normalize_text(value) for value in result if _normalize_text(value)]


def _single_value(
    labeled: Dict[str, List[str]],
    labels: Sequence[str],
    *,
    required: bool = False,
    conflict_code: str = "conflicting_labeled_values",
) -> Optional[str]:
    values = _values_for_labels(labeled, labels)
    unique_values = tuple(dict.fromkeys(values))
    if len(unique_values) > 1:
        raise FundParseError(conflict_code)
    if not unique_values:
        if required:
            raise FundParseError("missing_required_field")
        return None
    return unique_values[0]


def _labeled_values_from_tables(response: TextResponse) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for table in parse_tables(response.text, response.final_url):
        for row in table.rows:
            if len(row) < 2 or len(row) % 2:
                continue
            for index in range(0, len(row), 2):
                label = _normalize_text(row[index])
                value = _normalize_text(row[index + 1])
                if label and value:
                    result.setdefault(label, []).append(value)
    return result


def _parse_date(value: str, error_code: str) -> date:
    normalized = _normalize_text(value)
    for pattern in DATE_PATTERNS:
        try:
            return (
                date.fromisoformat(normalized)
                if pattern == "%Y-%m-%d"
                else datetime.strptime(normalized, pattern).date()
            )
        except ValueError:
            continue
    raise FundParseError(error_code)


def _normalized_base_name(value: str) -> Tuple[str, Optional[str]]:
    normalized = unicodedata.normalize("NFKC", value).strip()
    match = SHARE_CLASS_PATTERN.search(normalized)
    if match is None:
        return "", None
    base = normalized[: match.start()]
    base = re.sub(r"[\s·•・()（）\[\]【】_-]+", "", base).casefold()
    return base, match.group(1).upper()


def _share_classes(response: TextResponse, fund_code: str, fund_name: str) -> Tuple[object, ...]:
    expected_base, current_class = _normalized_base_name(fund_name)
    if not expected_base or current_class is None:
        return ()

    candidates: List[Tuple[str, str, str]] = []
    for link in extract_links(response.text, response.final_url):
        parsed = urlparse(link.url)
        code_matches = FUND_CODE_PATTERN.findall(parsed.path)
        if len(code_matches) != 1:
            continue
        base_name, share_class = _normalized_base_name(link.text)
        if base_name != expected_base or share_class is None:
            continue
        candidates.append((code_matches[0], share_class, _normalize_text(link.text)))

    if not any(code != fund_code for code, _, _ in candidates):
        return ()

    records: List[object] = []
    seen = set()
    for related_code, share_class, related_name in candidates:
        key = (related_code, share_class)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            FundShareClass(
                fund_code=fund_code,
                related_fund_code=related_code,
                share_class=share_class,
                fund_name=related_name,
                source_document_id=None,
            )
        )
    return tuple(records)


def parse_basic_profile(response: TextResponse, fund_code: str) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.BASIC_PROFILE)
    labeled = extract_labeled_values(response.text, response.final_url)
    for label, values in _labeled_values_from_tables(response).items():
        labeled[label] = values
    if not _values_for_labels(labeled, ("基金代码", "基金简称", "基金名称")):
        if _has_explicit_phrase(response.text, NO_PROFILE_PHRASES):
            return ParsedSection(
                DocumentKind.BASIC_PROFILE.value, source, (), "not_disclosed"
            )
    page_code = _single_value(
        labeled,
        ("基金代码",),
        required=True,
        conflict_code="identity_conflict",
    )
    page_code_match = FUND_CODE_PATTERN.search(page_code or "")
    if page_code_match is None or page_code_match.group(1) != fund_code:
        raise FundParseError("identity_conflict")

    fund_name = _single_value(
        labeled,
        ("基金简称", "基金名称"),
        required=True,
        conflict_code="identity_conflict",
    )
    status_text = _single_value(labeled, ("基金状态", "交易状态"), required=True)
    status_values = {
        "正常": "active",
        "存续": "active",
        "active": "active",
        "开放申购开放赎回": "active",
        "开放申购 开放赎回": "active",
        "终止": "terminated",
        "清盘": "terminated",
        "terminated": "terminated",
    }
    status = status_values.get((status_text or "").casefold())
    if status is None:
        raise FundParseError("unsupported_fund_status")

    established_text = _single_value(labeled, ("成立日期/规模",))
    if established_text is None:
        established_text = _single_value(labeled, ("成立日期", "基金成立日"))
    if established_text is not None:
        established_match = re.search(
            r"\d{4}(?:[-/]\d{2}[-/]\d{2}|年\d{2}月\d{2}日)", established_text
        )
        established_text = None if established_match is None else established_match.group(0)
    identity = FundIdentity(
        fund_code=fund_code,
        fund_name=fund_name or "",
        status=status,
        fund_type=_single_value(labeled, ("基金类型",)),
        established_date=(
            _parse_date(established_text, "invalid_established_date")
            if established_text is not None
            else None
        ),
        manager_name=_single_value(labeled, ("基金管理人", "基金公司")),
        source_document_id=None,
    )
    records: List[object] = [identity]
    records.extend(_share_classes(response, fund_code, fund_name or ""))

    benchmark = _single_value(labeled, ("业绩比较基准", "业绩基准"))
    if benchmark is not None:
        records.append(
            FundBenchmark(
                fund_code=fund_code,
                description=benchmark,
                effective_from=None,
                effective_to=None,
                source_document_id=None,
            )
        )
    for record in records:
        record.validate()
    return ParsedSection(
        section=DocumentKind.BASIC_PROFILE.value,
        source=source,
        records=tuple(records),
        state="success",
    )


def _header_index(headers: Sequence[str], accepted: Sequence[str]) -> Optional[int]:
    normalized = [_normalize_text(header) for header in headers]
    for label in accepted:
        if label in normalized:
            return normalized.index(label)
    return None


def parse_manager_history(response: TextResponse, fund_code: str) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.MANAGER_HISTORY)
    records: List[object] = []
    for table in parse_tables(response.text, response.final_url):
        name_index = _header_index(table.headers, ("基金经理", "姓名"))
        start_index = _header_index(
            table.headers, ("任职日期", "任职起始日期", "起始期")
        )
        end_index = _header_index(
            table.headers, ("离任日期", "任职截止日期", "截止期")
        )
        if name_index is None or start_index is None or end_index is None:
            continue
        for row in table.rows:
            if max(name_index, start_index, end_index) >= len(row):
                raise FundParseError("malformed_manager_history")
            manager_name = _normalize_text(row[name_index])
            start_text = _normalize_text(row[start_index])
            end_text = _normalize_text(row[end_index])
            if not manager_name or not start_text:
                raise FundParseError("malformed_manager_history")
            end_date = (
                None
                if end_text in CURRENT_TENURE_VALUES
                else _parse_date(end_text, "invalid_manager_date")
            )
            manager_names = [
                value
                for value in re.split(r"[\s、，,/]+", manager_name)
                if value
            ]
            if not manager_names:
                raise FundParseError("malformed_manager_history")
            for individual_name in manager_names:
                record = FundManagerTenure(
                    fund_code=fund_code,
                    manager_name=individual_name,
                    start_date=_parse_date(start_text, "invalid_manager_date"),
                    end_date=end_date,
                    source_document_id=None,
                )
                try:
                    record.validate()
                except ValueError as exc:
                    raise FundParseError("invalid_manager_tenure") from exc
                records.append(record)

    if not records:
        if _has_explicit_phrase(response.text, NO_MANAGER_PHRASES):
            return ParsedSection(
                DocumentKind.MANAGER_HISTORY.value, source, (), "not_disclosed"
            )
        raise FundParseError("missing_manager_history")
    return ParsedSection(
        section=DocumentKind.MANAGER_HISTORY.value,
        source=source,
        records=tuple(records),
        state="success",
    )


FEE_TYPE_VALUES = {
    "管理费": FeeType.MANAGEMENT,
    "基金管理费": FeeType.MANAGEMENT,
    "托管费": FeeType.CUSTODY,
    "基金托管费": FeeType.CUSTODY,
    "销售服务费": FeeType.SALES_SERVICE,
    "申购费": FeeType.SUBSCRIPTION,
    "申购费率": FeeType.SUBSCRIPTION,
    "赎回费": FeeType.REDEMPTION,
    "赎回费率": FeeType.REDEMPTION,
    "管理费率": FeeType.MANAGEMENT,
    "托管费率": FeeType.CUSTODY,
    "销售服务费率": FeeType.SALES_SERVICE,
}
UNTIERED_FEE_CONDITIONS = frozenset({"", "--", "-", "全部", "不分档", "无"})
NO_CHARGE_VALUES = frozenset({"不收取", "不收费", "免收", "免费", "0"})


def _required_table_index(
    headers: Sequence[str], accepted: Sequence[str]
) -> Optional[int]:
    return _header_index(headers, accepted)


def _parse_decimal(value: str) -> Decimal:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation as exc:
        raise FundParseError("ambiguous_fee_rule") from exc


def _fee_share_class(value: str) -> Optional[str]:
    normalized = _normalize_text(value).upper()
    if normalized in {"", "全部", "不区分", "--", "-"}:
        return None
    match = re.fullmatch(r"([AC])(?:类|类份额)?", normalized)
    if match is None:
        raise FundParseError("ambiguous_fee_rule")
    return match.group(1)


def _fee_value(value: str) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    normalized = re.sub(r"\(每年\)$", "", _normalize_text(value))
    if normalized in NO_CHARGE_VALUES:
        return Decimal("0"), None
    rate_match = re.fullmatch(r"(\d+(?:\.\d+)?)%", normalized)
    if rate_match is not None:
        return _parse_decimal(rate_match.group(1)), None
    amount_match = re.fullmatch(r"(?:每笔)?(\d+(?:\.\d+)?)元(?:/笔)?", normalized)
    if amount_match is not None:
        return None, _parse_decimal(amount_match.group(1))
    raise FundParseError("ambiguous_fee_rule")


def _amount_in_yuan(number: str, unit: str) -> Decimal:
    multipliers = {"元": Decimal("1"), "万元": Decimal("10000"), "亿元": Decimal("100000000")}
    try:
        return _parse_decimal(number) * multipliers[unit]
    except KeyError as exc:
        raise FundParseError("ambiguous_fee_rule") from exc


def _fee_interval(
    condition: str,
) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[int], Optional[int]]:
    normalized = _normalize_text(condition).replace("≤", "<=").replace("≥", ">=")
    if normalized in UNTIERED_FEE_CONDITIONS:
        return None, None, None, None

    chinese_amount = re.fullmatch(
        r"大于等于(\d+(?:\.\d+)?)(元|万元|亿元)[，,]小于(\d+(?:\.\d+)?)(元|万元|亿元)",
        normalized,
    )
    if chinese_amount is not None:
        return (
            _amount_in_yuan(chinese_amount.group(1), chinese_amount.group(2)),
            _amount_in_yuan(chinese_amount.group(3), chinese_amount.group(4)),
            None,
            None,
        )
    chinese_amount = re.fullmatch(r"小于(\d+(?:\.\d+)?)(元|万元|亿元)", normalized)
    if chinese_amount is not None:
        return None, _amount_in_yuan(chinese_amount.group(1), chinese_amount.group(2)), None, None
    chinese_amount = re.fullmatch(r"大于等于(\d+(?:\.\d+)?)(元|万元|亿元)", normalized)
    if chinese_amount is not None:
        return _amount_in_yuan(chinese_amount.group(1), chinese_amount.group(2)), None, None, None
    chinese_holding = re.fullmatch(r"小于(\d+)天", normalized)
    if chinese_holding is not None:
        threshold = int(chinese_holding.group(1))
        return None, None, None, threshold - 1
    chinese_holding = re.fullmatch(r"大于等于(\d+)天[，,]小于(\d+)天", normalized)
    if chinese_holding is not None:
        lower, upper = int(chinese_holding.group(1)), int(chinese_holding.group(2))
        if lower >= upper:
            raise FundParseError("ambiguous_fee_rule")
        return None, None, lower, upper - 1
    chinese_holding = re.fullmatch(r"大于等于(\d+)天", normalized)
    if chinese_holding is not None:
        return None, None, int(chinese_holding.group(1)), None

    amount_patterns = (
        (r"(?:申购)?金额<(\d+(?:\.\d+)?)(元|万元|亿元)", (None, 1)),
        (
            r"(\d+(?:\.\d+)?)(元|万元|亿元)<="
            r"(?:申购)?金额<(\d+(?:\.\d+)?)(元|万元|亿元)",
            (1, 3),
        ),
        (r"(?:申购)?金额>=(\d+(?:\.\d+)?)(元|万元|亿元)", (1, None)),
    )
    for pattern, bounds in amount_patterns:
        match = re.fullmatch(pattern, normalized)
        if match is None:
            continue
        if bounds == (None, 1):
            return None, _amount_in_yuan(match.group(1), match.group(2)), None, None
        if bounds == (1, 3):
            return (
                _amount_in_yuan(match.group(1), match.group(2)),
                _amount_in_yuan(match.group(3), match.group(4)),
                None,
                None,
            )
        return _amount_in_yuan(match.group(1), match.group(2)), None, None, None

    holding_match = re.fullmatch(r"持有天数<(\d+)天", normalized)
    if holding_match is not None:
        threshold = int(holding_match.group(1))
        if threshold <= 0:
            raise FundParseError("ambiguous_fee_rule")
        return None, None, None, threshold - 1
    holding_match = re.fullmatch(r"(\d+)天<=持有天数<(\d+)天", normalized)
    if holding_match is not None:
        lower, upper = int(holding_match.group(1)), int(holding_match.group(2))
        if lower >= upper:
            raise FundParseError("ambiguous_fee_rule")
        return None, None, lower, upper - 1
    holding_match = re.fullmatch(r"持有天数>=(\d+)天", normalized)
    if holding_match is not None:
        return None, None, int(holding_match.group(1)), None
    raise FundParseError("ambiguous_fee_rule")


def parse_fee_schedule(response: TextResponse, fund_code: str) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.FEE_SCHEDULE)
    records: List[object] = []
    for table in parse_tables(response.text, response.final_url):
        if not table.headers:
            for row in table.rows:
                if len(row) not in {4, 6}:
                    continue
                for index in range(0, len(row), 2):
                    fee_text = _normalize_text(row[index])
                    if fee_text not in FEE_TYPE_VALUES:
                        continue
                    rate, fixed_amount = _fee_value(row[index + 1])
                    record = FundFeeRule(
                        fund_code=fund_code,
                        fee_type=FEE_TYPE_VALUES[fee_text],
                        source_document_id=None,
                        rate=rate,
                        fixed_amount=fixed_amount,
                        rule_order=len(records) + 1,
                        raw_rule_text=f"{fee_text} | {row[index + 1]}",
                    )
                    record.validate()
                    records.append(record)
            continue

        caption = _normalize_text(table.caption)
        tiered_fee_type = (
            FeeType.SUBSCRIPTION if "申购" in caption or "认购" in caption
            else FeeType.REDEMPTION if "赎回" in caption else None
        )
        condition_column = _header_index(table.headers, ("适用金额", "适用期限"))
        if tiered_fee_type is not None and condition_column is not None:
            value_columns = [
                (index, _normalize_text(header))
                for index, header in enumerate(table.headers)
                if _normalize_text(header)
                in {
                    "费率",
                    "认购费率",
                    "申购费率",
                    "赎回费率",
                    "原费率",
                    "天天基金优惠费率",
                }
            ]
            combined_value_column = _header_index(
                table.headers, ("原费率|天天基金优惠费率",)
            )
            for row in table.rows:
                if combined_value_column is not None:
                    if max(condition_column, combined_value_column) >= len(row):
                        raise FundParseError("ambiguous_fee_rule")
                    combined_values = [
                        _normalize_text(item)
                        for item in row[combined_value_column].split("|")
                    ]
                    if len(combined_values) == 1 and "每笔" in combined_values[0]:
                        value_items = [(combined_values[0], "固定金额")]
                    elif len(combined_values) == 2:
                        value_items = [
                            (combined_values[0], "原费率"),
                            (combined_values[1], "天天基金优惠费率"),
                        ]
                    else:
                        raise FundParseError("ambiguous_fee_rule")
                else:
                    value_items = [
                        (_normalize_text(row[index]), label)
                        for index, label in value_columns
                        if index < len(row)
                    ]
                if not value_items:
                    raise FundParseError("ambiguous_fee_rule")
                condition_text = _normalize_text(row[condition_column])
                bounds = _fee_interval(condition_text)
                for value_text, value_label in value_items:
                    rate, fixed_amount = _fee_value(value_text)
                    record = FundFeeRule(
                        fund_code=fund_code,
                        fee_type=tiered_fee_type,
                        source_document_id=None,
                        rate=rate,
                        fixed_amount=fixed_amount,
                        amount_min=bounds[0], amount_max=bounds[1],
                        holding_days_min=bounds[2], holding_days_max=bounds[3],
                        rule_order=len(records) + 1,
                        raw_rule_text=" | ".join(
                            (caption, condition_text, value_label, value_text)
                        ),
                    )
                    record.validate()
                    records.append(record)
            continue
        fee_index = _required_table_index(table.headers, ("费用类型", "费率类型"))
        class_index = _required_table_index(table.headers, ("份额类别", "收费类别"))
        condition_index = _required_table_index(table.headers, ("适用条件", "金额或期限"))
        value_index = _required_table_index(
            table.headers, ("费率或金额", "费率", "收费标准")
        )
        effective_index = _required_table_index(table.headers, ("生效日期", "有效日期"))
        indexes = (fee_index, class_index, condition_index, value_index, effective_index)
        if any(index is None for index in indexes):
            continue
        resolved = tuple(int(index) for index in indexes if index is not None)
        for row in table.rows:
            if max(resolved) >= len(row):
                raise FundParseError("ambiguous_fee_rule")
            fee_text, class_text, condition_text, value_text, effective_text = (
                _normalize_text(row[index]) for index in resolved
            )
            try:
                fee_type = FEE_TYPE_VALUES[fee_text]
            except KeyError as exc:
                raise FundParseError("ambiguous_fee_rule") from exc
            rate, fixed_amount = _fee_value(value_text)
            amount_min, amount_max, holding_min, holding_max = _fee_interval(condition_text)
            record = FundFeeRule(
                fund_code=fund_code,
                fee_type=fee_type,
                source_document_id=None,
                share_class=_fee_share_class(class_text),
                rate=rate,
                fixed_amount=fixed_amount,
                amount_min=amount_min,
                amount_max=amount_max,
                holding_days_min=holding_min,
                holding_days_max=holding_max,
                rule_order=len(records) + 1,
                effective_from=_parse_date(effective_text, "ambiguous_fee_rule"),
                raw_rule_text=" | ".join(
                    (fee_text, class_text, condition_text, value_text, effective_text)
                ),
            )
            try:
                record.validate()
            except ValueError as exc:
                raise FundParseError("ambiguous_fee_rule") from exc
            records.append(record)
    if not records:
        raise FundParseError("ambiguous_fee_rule")
    return ParsedSection(
        DocumentKind.FEE_SCHEDULE.value, source, tuple(records), "success"
    )


def _parse_size_value(value: str, unit: str) -> Decimal:
    normalized = _normalize_text(value)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(亿元|万元|元|亿份|万份|份)", normalized)
    if match is None:
        raise FundParseError("ambiguous_fee_rule")
    number, actual_unit = match.groups()
    allowed = {"asset": {"亿元", "万元", "元"}, "share": {"亿份", "万份", "份"}}
    if actual_unit not in allowed[unit]:
        raise FundParseError("ambiguous_fee_rule")
    multipliers = {
        "亿元": Decimal("100000000"), "万元": Decimal("10000"), "元": Decimal("1"),
        "亿份": Decimal("100000000"), "万份": Decimal("10000"), "份": Decimal("1"),
    }
    return _parse_decimal(number) * multipliers[actual_unit]


def _parse_published_at(value: str, error_code: str = "ambiguous_fee_rule") -> datetime:
    normalized = _normalize_text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        return datetime.combine(
            _parse_date(normalized, error_code),
            time.min,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FundParseError(error_code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundParseError(error_code)
    return parsed


def parse_size_history(response: TextResponse, fund_code: str) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.SIZE_HISTORY)
    records: List[object] = []
    data_match = re.search(
        r"[\"']?data[\"']?\s*:\s*(\[.*?\])\s*[,}]",
        response.text,
        re.DOTALL,
    )
    if data_match is not None:
        try:
            items = json.loads(data_match.group(1))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FundParseError("malformed_size_history") from exc
        for item in items:
            try:
                net_assets_value = item.get("NETNAV")
                total_shares_value = item.get("QMZFE")
                net_assets = (
                    None
                    if net_assets_value in {None, ""}
                    else Decimal(str(net_assets_value))
                )
                total_shares = (
                    None
                    if total_shares_value in {None, ""}
                    else Decimal(str(total_shares_value))
                )
                if net_assets is None and total_shares is None:
                    continue
                record = FundSizeObservation(
                    fund_code=fund_code,
                    report_date=_parse_date(str(item["FSRQ"]), "invalid_disclosure_date"),
                    net_assets=net_assets,
                    total_shares=total_shares,
                    published_at=None,
                    source_document_id=None,
                )
                record.validate()
            except (KeyError, InvalidOperation, ValueError) as exc:
                raise FundParseError("malformed_size_history") from exc
            records.append(record)
    for table in (() if records else parse_tables(response.text, response.final_url)):
        report_index = _header_index(table.headers, ("报告日期", "截止日期"))
        asset_index = _header_index(table.headers, ("净资产", "基金净资产"))
        shares_index = _header_index(table.headers, ("总份额", "基金份额"))
        published_index = _header_index(table.headers, ("公告时间", "公告日期"))
        indexes = (report_index, asset_index, shares_index, published_index)
        if any(index is None for index in indexes):
            continue
        resolved = tuple(int(index) for index in indexes if index is not None)
        for row in table.rows:
            if max(resolved) >= len(row):
                raise FundParseError("ambiguous_fee_rule")
            report_text, asset_text, shares_text, published_text = (
                _normalize_text(row[index]) for index in resolved
            )
            record = FundSizeObservation(
                fund_code=fund_code,
                report_date=_parse_date(report_text, "ambiguous_fee_rule"),
                net_assets=_parse_size_value(asset_text, "asset"),
                total_shares=_parse_size_value(shares_text, "share"),
                published_at=_parse_published_at(published_text),
                source_document_id=None,
            )
            try:
                record.validate()
            except ValueError as exc:
                raise FundParseError("ambiguous_fee_rule") from exc
            records.append(record)
    if not records:
        raise FundParseError("ambiguous_fee_rule")
    return ParsedSection(
        DocumentKind.SIZE_HISTORY.value, source, tuple(records), "success"
    )


def _json_payload(text: str, error_code: str) -> object:
    candidate = text.strip()
    if candidate.startswith("var "):
        equals = candidate.find("=")
        candidate = candidate[equals + 1 :].strip().rstrip(";")
    try:
        return json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FundParseError(error_code) from exc


def _js_content_payload(text: str) -> str:
    try:
        payload = _json_payload(text, "malformed_quarterly_holding")
    except FundParseError:
        payload = None
    if isinstance(payload, dict):
        content = payload.get("content") or payload.get("Content")
        if isinstance(content, str):
            return content
    match = re.search(r"\bcontent\s*:\s*(\"(?:\\.|[^\"\\])*\")", text, re.DOTALL)
    if match is None:
        raise FundParseError("malformed_quarterly_holding")
    try:
        content = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FundParseError("malformed_quarterly_holding") from exc
    if not isinstance(content, str):
        raise FundParseError("malformed_quarterly_holding")
    return content


def _quarter_end_from_title(title: str) -> date:
    normalized = _normalize_text(title)
    match = re.search(r"(\d{4})年(?:第)?([一二三四1234])季度", normalized)
    if match is None:
        match = re.search(r"(\d{4})年([1234])季", normalized)
    if match is None:
        raise FundParseError("invalid_disclosure_date")
    quarter_values = {"一": 1, "二": 2, "三": 3, "四": 4}
    quarter = quarter_values.get(
        match.group(2), int(match.group(2)) if match.group(2).isdigit() else 0
    )
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return date(int(match.group(1)), *month_day)


ASSET_TYPE_VALUES = {
    "股票": AssetType.STOCK,
    "债券": AssetType.BOND,
    "基金": AssetType.FUND,
    "现金": AssetType.CASH,
    "其他": AssetType.OTHER,
}
DISCLOSURE_SCOPE_VALUES = {
    "前十大持仓": "top10",
    "前十大重仓": "top10",
    "top10": "top10",
    "完整持仓": "complete",
    "全部持仓": "complete",
    "complete": "complete",
}


def _required_indexes(
    headers: Sequence[str], definitions: Sequence[Tuple[str, Sequence[str]]]
) -> Dict[str, int]:
    result: Dict[str, int] = {}
    for field_name, accepted in definitions:
        index = _header_index(headers, accepted)
        if index is None:
            raise FundParseError("missing_disclosure_column")
        result[field_name] = index
    return result


def _parse_disclosure_weight(value: str) -> Decimal:
    normalized = _normalize_text(value)
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)%", normalized)
    if match is None:
        raise FundParseError("invalid_disclosure_weight")
    weight = _parse_decimal(match.group(1))
    if weight < 0 or weight > 100:
        raise FundParseError("invalid_disclosure_weight")
    return weight


def parse_quarterly_holdings(
    response: TextResponse, fund_code: str
) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.QUARTERLY_HOLDINGS)
    records: List[object] = []
    stripped = response.text.lstrip()
    if stripped.startswith("var ") or stripped.startswith("{"):
        content = _js_content_payload(response.text)
        title_match = re.search(
            r"<h[1-6][^>]*>(.*?)</h[1-6]>", content, re.DOTALL | re.IGNORECASE
        )
        if title_match is None:
            raise FundParseError("invalid_disclosure_date")
        dynamic_definitions = (
            ("rank", ("序号", "排名")),
            ("code", ("股票代码", "证券代码")),
            ("name", ("股票名称", "证券名称")),
            ("weight", ("占净值比例", "占基金净值比")),
        )
        table_groups: List[Tuple[Optional[date], Tuple[object, ...]]] = []
        page_period = _quarter_end_from_title(_visible_text(title_match.group(1)))
        table_matches = tuple(
            re.finditer(r"<table\b[^>]*>.*?</table\s*>", content, re.DOTALL | re.IGNORECASE)
        )
        previous_table_end = 0
        for table_match in table_matches:
            parsed_tables = parse_tables(table_match.group(0), response.final_url)
            if len(parsed_tables) != 1:
                raise FundParseError("malformed_quarterly_holding")
            table = parsed_tables[0]
            local_headings = tuple(
                re.finditer(
                    r"<h[1-6][^>]*>(.*?)</h[1-6]>",
                    content[previous_table_end:table_match.start()],
                    re.DOTALL | re.IGNORECASE,
                )
            )
            previous_table_end = table_match.end()
            table_period = None
            if local_headings:
                try:
                    table_period = _quarter_end_from_title(
                        _visible_text(local_headings[-1].group(1))
                    )
                except FundParseError:
                    pass
            try:
                indexes = _required_indexes(table.headers, dynamic_definitions)
            except FundParseError:
                continue
            shares_index = _header_index(
                table.headers, ("持股数（万股）", "持股数(万股)", "持股数万股")
            )
            value_index = _header_index(
                table.headers, ("持仓市值（万元）", "持仓市值(万元)", "持仓市值万元")
            )
            table_records: List[object] = []
            for row in table.rows:
                if max(indexes.values()) >= len(row):
                    raise FundParseError("malformed_quarterly_holding")
                try:
                    shares_text = "" if shares_index is None else _normalize_text(row[shares_index])
                    value_text = "" if value_index is None else _normalize_text(row[value_index])
                    record = FundHolding(
                        fund_code=fund_code,
                        # An unlabelled table remains displayable, but is never
                        # treated as period-verified below.
                        report_period=page_period if table_period is None else table_period,
                        published_at=None,
                        rank=int(_normalize_text(row[indexes["rank"]])),
                        security_code=_normalize_text(row[indexes["code"]]),
                        security_name=_normalize_text(row[indexes["name"]]),
                        asset_type=AssetType.STOCK,
                        weight=_parse_disclosure_weight(row[indexes["weight"]]),
                        disclosure_scope="top10",
                        source_document_id=None,
                        shares=(
                            None
                            if not shares_text
                            else _parse_decimal(shares_text) * Decimal("10000")
                        ),
                        market_value=(
                            None
                            if not value_text
                            else _parse_decimal(value_text) * Decimal("10000")
                        ),
                    )
                    record.validate()
                except (IndexError, InvalidOperation, ValueError) as exc:
                    raise FundParseError("malformed_quarterly_holding") from exc
                table_records.append(record)
            if table_records:
                table_groups.append((table_period, tuple(table_records)))
        if table_groups:
            warnings = ["publication_date_requires_announcement_match"]
            conflicts: Tuple[str, ...] = ()
            state = "success"
            selected_period, selected_records = table_groups[0]
            if len(table_groups) == 1 and selected_period is None:
                state = "partial"
                warnings.append("top10_table_group_display_only")
                conflicts = ("top10_table_group_report_period_unbound",)
            elif len(table_groups) > 1:
                warnings.append("multiple_top10_table_groups")
                matching_groups = [
                    group for group in table_groups if group[0] == page_period
                ]
                if (
                    len(matching_groups) == 1
                    and all(group[0] is not None for group in table_groups)
                ):
                    selected_period, selected_records = matching_groups[0]
                    warnings.append("top10_table_group_selected_by_heading")
                else:
                    # A deterministic view helps a human inspect the response,
                    # but duplicate/unlabelled groups cannot establish one report period.
                    state = "partial"
                    warnings.append("top10_table_group_display_only")
                    conflicts = ("multiple_top10_table_groups_unbound",)
                    if matching_groups:
                        selected_period, selected_records = matching_groups[0]
            records = list(selected_records)
            return ParsedSection(
                DocumentKind.QUARTERLY_HOLDINGS.value,
                source,
                tuple(records),
                state,
                warnings=tuple(warnings),
                conflicts=conflicts,
            )
    definitions = (
        ("report_period", ("报告期", "报告日期", "截止日期")),
        ("published_at", ("公告时间", "公告日期", "披露日期")),
        ("scope", ("披露范围",)),
        ("rank", ("序号", "排名")),
        ("code", ("证券代码", "股票代码", "债券代码")),
        ("name", ("证券名称", "股票名称", "债券名称")),
        ("asset_type", ("资产类型", "证券类型")),
        ("weight", ("占净值比例", "持仓占比", "占基金净值比")),
    )
    for table in parse_tables(response.text, response.final_url):
        if _header_index(table.headers, ("披露范围",)) is None:
            continue
        indexes = _required_indexes(table.headers, definitions)
        for row in table.rows:
            if max(indexes.values()) >= len(row):
                raise FundParseError("malformed_quarterly_holding")
            try:
                rank = int(_normalize_text(row[indexes["rank"]]))
                asset_type = ASSET_TYPE_VALUES[
                    _normalize_text(row[indexes["asset_type"]])
                ]
                scope = DISCLOSURE_SCOPE_VALUES[
                    _normalize_text(row[indexes["scope"]]).casefold()
                ]
            except (KeyError, ValueError) as exc:
                raise FundParseError("malformed_quarterly_holding") from exc
            record = FundHolding(
                fund_code=fund_code,
                report_period=_parse_date(
                    row[indexes["report_period"]], "invalid_disclosure_date"
                ),
                published_at=_parse_published_at(
                    row[indexes["published_at"]], "invalid_disclosure_date"
                ),
                rank=rank,
                security_code=_normalize_text(row[indexes["code"]]),
                security_name=_normalize_text(row[indexes["name"]]),
                asset_type=asset_type,
                weight=_parse_disclosure_weight(row[indexes["weight"]]),
                disclosure_scope=scope,
                source_document_id=None,
            )
            try:
                record.validate()
            except ValueError as exc:
                raise FundParseError("malformed_quarterly_holding") from exc
            records.append(record)
    if not records:
        if _has_explicit_phrase(response.text, NO_HOLDING_PHRASES):
            return ParsedSection(
                DocumentKind.QUARTERLY_HOLDINGS.value,
                source,
                (),
                "not_disclosed",
            )
        raise FundParseError("missing_quarterly_holdings")
    return ParsedSection(
        DocumentKind.QUARTERLY_HOLDINGS.value, source, tuple(records), "success"
    )


def parse_industry_exposure(
    response: TextResponse, fund_code: str
) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.INDUSTRY_EXPOSURE)
    records: List[object] = []
    if response.text.lstrip().startswith("{"):
        payload = _json_payload(response.text, "malformed_industry_exposure")
        if not isinstance(payload, dict):
            raise FundParseError("malformed_industry_exposure")
        data = payload.get("Data") or payload.get("data") or payload
        quarter_infos = data.get("QuarterInfos") if isinstance(data, dict) else None
        if not isinstance(quarter_infos, list):
            raise FundParseError("malformed_industry_exposure")
        shared_items = data.get("HYPZInfo") if isinstance(data, dict) else None
        groups: List[Tuple[Optional[date], List[object]]] = []
        if isinstance(shared_items, list):
            groups.append((None, shared_items))
        else:
            for quarter in quarter_infos:
                if not isinstance(quarter, dict):
                    raise FundParseError("malformed_industry_exposure")
                report_period = _parse_date(
                    str(quarter.get("JZRQ") or quarter.get("FSRQ") or ""),
                    "invalid_disclosure_date",
                )
                items = quarter.get("HYPZInfo")
                if not isinstance(items, list):
                    raise FundParseError("malformed_industry_exposure")
                groups.append((report_period, items))
        for report_period, items in groups:
            for item in items:
                if not isinstance(item, dict):
                    raise FundParseError("malformed_industry_exposure")
                try:
                    fallback_period = "" if report_period is None else report_period.isoformat()
                    item_period = _parse_date(
                        str(item.get("FSRQ", fallback_period)),
                        "invalid_disclosure_date",
                    )
                    if report_period is not None and item_period != report_period:
                        raise FundParseError("industry_report_period_conflict")
                    record = FundIndustryExposure(
                        fund_code=fund_code,
                        report_period=item_period,
                        published_at=None,
                        classification_standard="证监会行业分类",
                        industry_name=_normalize_text(str(item["HYMC"])),
                        weight=Decimal(str(item["ZJZBL"])),
                        source_document_id=None,
                        industry_code=_normalize_text(str(item.get("HYDM", ""))) or None,
                        market_value=Decimal(str(item["SZ"])) * Decimal("10000"),
                    )
                    record.validate()
                except (KeyError, InvalidOperation, ValueError) as exc:
                    raise FundParseError("malformed_industry_exposure") from exc
                records.append(record)
        if records:
            return ParsedSection(
                DocumentKind.INDUSTRY_EXPOSURE.value,
                source,
                tuple(records),
                "success",
                warnings=("publication_date_requires_announcement_match",),
            )
    definitions = (
        ("report_period", ("报告期", "报告日期", "截止日期")),
        ("published_at", ("公告时间", "公告日期", "披露日期")),
        ("standard", ("行业分类标准", "分类标准")),
        ("name", ("行业名称", "行业类别")),
        ("weight", ("占净值比例", "行业占比", "占基金净值比")),
    )
    for table in parse_tables(response.text, response.final_url):
        if _header_index(table.headers, ("行业分类标准", "分类标准")) is None:
            continue
        indexes = _required_indexes(table.headers, definitions)
        industry_code_index = _header_index(table.headers, ("行业代码",))
        for row in table.rows:
            if max(indexes.values()) >= len(row):
                raise FundParseError("malformed_industry_exposure")
            industry_code = None
            if industry_code_index is not None:
                if industry_code_index >= len(row):
                    raise FundParseError("malformed_industry_exposure")
                industry_code = _normalize_text(row[industry_code_index]) or None
            record = FundIndustryExposure(
                fund_code=fund_code,
                report_period=_parse_date(
                    row[indexes["report_period"]], "invalid_disclosure_date"
                ),
                published_at=_parse_published_at(
                    row[indexes["published_at"]], "invalid_disclosure_date"
                ),
                classification_standard=_normalize_text(row[indexes["standard"]]),
                industry_name=_normalize_text(row[indexes["name"]]),
                weight=_parse_disclosure_weight(row[indexes["weight"]]),
                source_document_id=None,
                industry_code=industry_code,
            )
            try:
                record.validate()
            except ValueError as exc:
                raise FundParseError("malformed_industry_exposure") from exc
            records.append(record)
    if not records:
        if _has_explicit_phrase(response.text, NO_INDUSTRY_PHRASES):
            return ParsedSection(
                DocumentKind.INDUSTRY_EXPOSURE.value,
                source,
                (),
                "not_disclosed",
            )
        raise FundParseError("missing_industry_exposure")
    return ParsedSection(
        DocumentKind.INDUSTRY_EXPOSURE.value, source, tuple(records), "success"
    )


def parse_announcements(
    response: TextResponse, fund_code: str, manager_name: str
) -> ParsedSection:
    source = _source(response, fund_code, DocumentKind.ANNOUNCEMENT)
    records: List[object] = []
    if response.text.lstrip().startswith("{"):
        payload = _json_payload(response.text, "malformed_announcement")
        if not isinstance(payload, dict):
            raise FundParseError("malformed_announcement")
        data = payload.get("Data") or payload.get("data")
        if isinstance(data, dict):
            data = data.get("Datas") or data.get("List") or data.get("data")
        if not isinstance(data, list):
            raise FundParseError("malformed_announcement")
        categories = {
            "1": "发行运作",
            "2": "分红",
            "3": "定期报告",
            "4": "人事调整",
            "5": "基金销售",
            "6": "其他",
        }
        for item in data:
            try:
                announcement_id = _normalize_text(str(item["ID"]))
                attachment_type = _normalize_text(
                    str(item.get("ATTACHTYPE", ""))
                ).lower()
                url = (
                    f"https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf"
                    if attachment_type == ".pdf"
                    else f"https://fund.eastmoney.com/gonggao/{fund_code},{announcement_id}.html"
                )
                record = FundAnnouncement(
                    fund_code=fund_code,
                    title=_normalize_text(str(item["TITLE"])),
                    category=categories.get(str(item.get("NEWCATEGORY", ""))),
                    publisher="东方财富公告索引",
                    published_at=_parse_published_at(
                        str(item["PUBLISHDATEDesc"]), "invalid_disclosure_date"
                    ),
                    url=url,
                    source_tier=2,
                    source_document_id=None,
                )
                record.validate()
            except (KeyError, ValueError) as exc:
                raise FundParseError("malformed_announcement") from exc
            records.append(record)
        if records:
            return ParsedSection(
                DocumentKind.ANNOUNCEMENT.value,
                source,
                tuple(records),
                "success",
            )
    definitions = (
        ("title", ("标题", "公告标题")),
        ("category", ("类别", "公告类型")),
        ("publisher", ("发布者", "发布机构")),
        ("published_at", ("公告时间", "公告日期", "发布时间")),
        ("link", ("公告链接", "链接")),
    )
    for table in parse_tables(response.text, response.final_url):
        if _header_index(table.headers, ("公告链接", "链接")) is None:
            continue
        indexes = _required_indexes(table.headers, definitions)
        if len(table.links) != len(table.rows):
            raise FundParseError("ambiguous_announcement_link")
        for row, (_, url) in zip(table.rows, table.links):
            if max(indexes.values()) >= len(row):
                raise FundParseError("malformed_announcement")
            publisher = _normalize_text(row[indexes["publisher"]])
            record = FundAnnouncement(
                fund_code=fund_code,
                title=_normalize_text(row[indexes["title"]]),
                category=_normalize_text(row[indexes["category"]]) or None,
                publisher=publisher,
                published_at=_parse_published_at(
                    row[indexes["published_at"]], "invalid_disclosure_date"
                ),
                url=url,
                source_tier=classify_source(url, publisher, manager_name),
                source_document_id=None,
            )
            try:
                record.validate()
            except ValueError as exc:
                raise FundParseError("malformed_announcement") from exc
            records.append(record)
    if not records:
        if _has_explicit_phrase(response.text, NO_ANNOUNCEMENT_PHRASES):
            return ParsedSection(
                DocumentKind.ANNOUNCEMENT.value,
                source,
                (),
                "not_disclosed",
            )
        raise FundParseError("missing_announcements")
    return ParsedSection(
        DocumentKind.ANNOUNCEMENT.value, source, tuple(records), "success"
    )
