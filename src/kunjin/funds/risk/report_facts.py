from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields
from decimal import Decimal
from typing import Optional, Tuple

from kunjin.funds.industry_taxonomy import (
    PRODUCTION_TAXONOMY_MAPPINGS,
    IndustryDistributionRow,
    IndustryTaxonomyMapping,
    validate_industry_distribution,
)
from kunjin.funds.risk.documents import MAX_EXCERPT_CHARACTERS
from kunjin.funds.risk.models import FactConfidence

MAX_REPORT_TABLES = 256
MAX_REPORT_ROWS = 20_000
MAX_REPORT_CELLS_PER_ROW = 32
MAX_REPORT_CELL_CHARACTERS = 4_096
MAX_REPORT_SECTION_CHARACTERS = 256

_FACT_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_UNIT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TOTAL_ASSET_DENOMINATOR = re.compile(
    r"基金总资产|(?:^|\b)(?:fund\s+)?total\s+assets?(?:\b|$)",
    flags=re.IGNORECASE,
)
_NET_ASSET_DENOMINATOR = re.compile(
    r"基金资产净值|基金净资产|(?:^|\b)(?:fund\s+)?net\s+assets?(?:\b|$)",
    flags=re.IGNORECASE,
)

COMMON_FACTS = frozenset(
    {
        "current_stock_asset_allocation_percent",
        "current_bond_asset_allocation_percent",
        "current_cash_asset_allocation_percent",
        "current_hong_kong_asset_allocation_percent",
        "current_largest_security_weight_percent",
        "current_top_ten_holdings_weight_percent",
        "current_largest_industry_name",
        "current_largest_industry_weight_percent",
        "current_industry_count",
        "holdings_evidence_complete",
    }
)

FIXED_INCOME_FACTS = frozenset(
    {
        "current_effective_duration",
        "current_weighted_average_maturity_days",
        "current_convertible_bond_asset_allocation_percent",
        "current_exchangeable_bond_asset_allocation_percent",
        "current_high_quality_fixed_income_percent",
        "current_below_aa_plus_exposure_percent",
        "current_unrated_non_sovereign_exposure_percent",
        "current_gross_leverage_percent",
        "current_largest_non_sovereign_issuer_percent",
    }
)

_INDICATOR_HEADERS = {("指标", "单位", "数值"), ("indicator", "unit", "value")}
_PERCENT_UNITS = {"%", "percent"}
_ASSET_FACTS = {
    "股票": "current_stock_asset_allocation_percent",
    "stock": "current_stock_asset_allocation_percent",
    "stocks": "current_stock_asset_allocation_percent",
    "债券": "current_bond_asset_allocation_percent",
    "bond": "current_bond_asset_allocation_percent",
    "bonds": "current_bond_asset_allocation_percent",
    "现金": "current_cash_asset_allocation_percent",
    "cash": "current_cash_asset_allocation_percent",
    "港股": "current_hong_kong_asset_allocation_percent",
    "hong kong": "current_hong_kong_asset_allocation_percent",
}
_DENOMINATOR_UNITS = {
    "基金总资产": "percent_of_total_assets",
    "total assets": "percent_of_total_assets",
    "基金资产净值": "percent_of_net_assets",
    "基金净资产": "percent_of_net_assets",
    "net assets": "percent_of_net_assets",
}
_CONCENTRATION_DENOMINATOR_UNITS = {
    **_DENOMINATOR_UNITS,
    "基金资产": "percent_of_fund_assets",
    "fund assets": "percent_of_fund_assets",
}
_TOP_TEN_SECURITY_SCOPES = {
    "报告期末前十大持仓",
    "报告期末前十名证券投资明细",
    "top ten holdings at the end of the reporting period",
}
_COMPLETE_SECURITY_SCOPES = {
    "报告期末全部证券持仓明细",
    "all security holdings at the end of the reporting period",
}
_COMPLETE_INDUSTRY_SCOPES = {
    "报告期末全部行业分布",
    "complete industry distribution at the end of the reporting period",
}
_INDUSTRY_STANDARD_HEADERS = {"分类标准", "classification standard"}
_INDUSTRY_CODE_HEADERS = {"行业代码", "industry code"}
_INDUSTRY_RANK_HEADERS = {"排名", "序号", "rank"}
_INDUSTRY_NAME_HEADERS = {"行业名称", "industry name"}
_RANKED_WEIGHT_HEADERS = {
    "占基金资产净值比例(%)": "percent_of_net_assets",
    "占基金净资产比例(%)": "percent_of_net_assets",
    "占基金总资产比例(%)": "percent_of_total_assets",
    "weight (% of net assets)": "percent_of_net_assets",
    "weight (% of total assets)": "percent_of_total_assets",
}
_DEFAULT_IGNORABLE_PATTERN = re.compile(
    "[\u00ad\u034f\u061c\u115f-\u1160\u17b4-\u17b5\u180b-\u180f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f"
    "\ufeff\uffa0\ufff0-\ufff8\U0001bca0-\U0001bca3"
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff]"
)
_DECIMAL_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_ASSET_ROW_PATTERN = re.compile(
    r"报告期末(股票|债券|现金|港股)资产占(基金总资产|基金资产净值|基金净资产)的"
    r"|(?:stocks?|bonds?|cash|hong kong assets?) represent(?:ation)? of (total assets|net assets)",
    flags=re.IGNORECASE,
)
_ASSET_SENTENCE_PATTERN = re.compile(
    r"报告期末(股票|债券|现金|港股)资产占(基金总资产|基金资产净值|基金净资产)"
    r"(?:的比例)?(?:为)?((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)%[。.]?"
    r"|(stocks?|bonds?|cash|hong kong assets?) represent "
    r"((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)% of (total assets|net assets)[.]?",
    flags=re.IGNORECASE,
)
_LARGEST_SECURITY_SENTENCE_PATTERN = re.compile(
    r"报告期末最大单一证券占(基金资产净值|基金净资产|基金总资产|基金资产)"
    r"((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)%[。.]?"
    r"|largest security represents ((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)% of "
    r"(net assets|total assets|fund assets)[.]?",
    flags=re.IGNORECASE,
)
_TOP_TEN_SENTENCE_PATTERN = re.compile(
    r"报告期末前十大持仓合计占(基金资产净值|基金净资产|基金总资产|基金资产)"
    r"((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)%[。.]?"
    r"|top ten holdings represent ((?:0|[1-9][0-9]*)(?:\.[0-9]+)?)% of "
    r"(net assets|total assets|fund assets)[.]?",
    flags=re.IGNORECASE,
)

_FIXED_INCOME_METRICS = {
    "报告期末组合有效久期": ("current_effective_duration", "年", "years"),
    "报告期末加权平均剩余期限": (
        "current_weighted_average_maturity_days",
        "天",
        "days",
    ),
    "portfolio effective duration at the end of the reporting period": (
        "current_effective_duration",
        "years",
        "years",
    ),
    "weighted average remaining maturity at the end of the reporting period": (
        "current_weighted_average_maturity_days",
        "days",
        "days",
    ),
}
_BOND_TYPE_FACTS = {
    "可转换债券": "current_convertible_bond_asset_allocation_percent",
    "可交换债券": "current_exchangeable_bond_asset_allocation_percent",
    "convertible bond": "current_convertible_bond_asset_allocation_percent",
    "exchangeable bond": "current_exchangeable_bond_asset_allocation_percent",
}
_COMPLETE_CREDIT_SCOPES = {
    "报告期末全部固定收益资产信用评级分布",
    "complete fixed income credit rating distribution at the end of the reporting period",
}
_CREDIT_RATING_HEADERS = {"信用评级", "credit rating"}
_ISSUER_CATEGORY_HEADERS = {"发行人类别", "issuer category"}
_CREDIT_WEIGHT_HEADERS = {
    "占基金资产净值比例(%)": "percent_of_net_assets",
    "占基金净资产比例(%)": "percent_of_net_assets",
    "占债券资产比例(%)": "percent_of_bond_assets",
    "占固定收益资产比例(%)": "percent_of_fixed_income_assets",
    "weight (% of net assets)": "percent_of_net_assets",
    "weight (% of bond assets)": "percent_of_bond_assets",
    "weight (% of fixed income assets)": "percent_of_fixed_income_assets",
}
_HIGH_QUALITY_RATINGS = frozenset({"aaa", "aa+", "aaa级", "aa+级"})
_BELOW_AA_PLUS_RATINGS = frozenset(
    {
        "aa",
        "aa-",
        "a+",
        "a",
        "a-",
        "bbb+",
        "bbb",
        "bbb-",
        "bb+",
        "bb",
        "bb-",
        "b+",
        "b",
        "b-",
        "ccc",
        "cc",
        "c",
        "d",
        "aa级",
        "aa-级",
        "a+级",
        "a级",
        "a-级",
        "bbb+级",
        "bbb级",
        "bbb-级",
        "bb+级",
        "bb级",
        "bb-级",
        "b+级",
        "b级",
        "b-级",
        "ccc级",
        "cc级",
        "c级",
        "d级",
    }
)
_UNRATED_RATINGS = frozenset({"未评级", "unrated"})
_SOVEREIGN_CATEGORIES = frozenset({"主权", "政府", "sovereign", "government"})
_POLICY_BANK_CATEGORIES = frozenset({"政策性银行", "policy bank"})
_NON_SOVEREIGN_CATEGORIES = frozenset(
    {"非主权", "其他非主权", "non-sovereign", "other non-sovereign"}
)
_COMPLETE_ISSUER_SCOPES = {
    "报告期末全部固定收益证券发行人分布",
    "complete fixed income issuer distribution at the end of the reporting period",
}
_ISSUER_NAME_HEADERS = {"发行人名称", "issuer name"}
_ISSUER_WEIGHT_HEADERS = {
    "占基金资产净值比例(%)": "percent_of_net_assets",
    "占基金净资产比例(%)": "percent_of_net_assets",
    "占基金资产比例(%)": "percent_of_fund_assets",
    "weight (% of net assets)": "percent_of_net_assets",
    "weight (% of fund assets)": "percent_of_fund_assets",
}
_LEVERAGE_HEADERS = {
    ("指标", "分母", "单位", "数值"),
    ("indicator", "denominator", "unit", "value"),
}
_LEVERAGE_LABELS = {
    "报告期末总资产杠杆率",
    "gross leverage at the end of the reporting period",
}
def _require_exact_record(value: object, expected_type: type, label: str) -> None:
    if type(value) is not expected_type:
        raise ValueError(f"{label} subclasses are not accepted")
    expected_fields = {field.name for field in fields(expected_type)}
    if type(vars(value)) is not dict or set(vars(value)) != expected_fields:
        raise ValueError(f"{label} has unexpected dataclass state")


def _has_control_characters(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validate_bounded_text(
    value: object,
    label: str,
    *,
    maximum: int,
) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or _has_control_characters(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty text")


def _validate_public_value(value: object) -> None:
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError("normalized value Decimal must be finite")
        return
    if type(value) is str:
        _validate_bounded_text(
            value,
            "normalized value",
            maximum=MAX_REPORT_CELL_CHARACTERS,
        )
        return
    if type(value) is tuple:
        for item in value:
            _validate_public_value(item)
        return
    raise ValueError("normalized value must be deeply immutable")


@dataclass(frozen=True)
class ReportCell:
    text: str
    is_header: bool

    def validate(self) -> None:
        _require_exact_record(self, ReportCell, "report cell")
        _validate_bounded_text(
            self.text,
            "report cell text",
            maximum=MAX_REPORT_CELL_CHARACTERS,
        )
        if type(self.is_header) is not bool:
            raise ValueError("report cell header state must be boolean")


@dataclass(frozen=True)
class ReportRow:
    cells: Tuple[ReportCell, ...]

    def validate(self) -> None:
        _require_exact_record(self, ReportRow, "report row")
        if type(self.cells) is not tuple:
            raise ValueError("report row cells must be an immutable tuple")
        if not self.cells or len(self.cells) > MAX_REPORT_CELLS_PER_ROW:
            raise ValueError("report row cell count is outside the configured limit")
        for cell in self.cells:
            if type(cell) is not ReportCell:
                raise ValueError("report row cells must use exact ReportCell records")
            cell.validate()
        headers = [
            " ".join(unicodedata.normalize("NFKC", cell.text).split()).casefold()
            for cell in self.cells
            if cell.is_header
        ]
        if len(headers) != len(set(headers)):
            raise ValueError("report row contains duplicate headers")


@dataclass(frozen=True)
class ReportTable:
    rows: Tuple[ReportRow, ...]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str

    def validate(self) -> None:
        _require_exact_record(self, ReportTable, "report table")
        if type(self.rows) is not tuple:
            raise ValueError("report table rows must be an immutable tuple")
        if not self.rows or len(self.rows) > MAX_REPORT_ROWS:
            raise ValueError("report table exceeds the configured row limit")
        for row in self.rows:
            if type(row) is not ReportRow:
                raise ValueError("report table rows must use exact ReportRow records")
            row.validate()
        widths = {len(row.cells) for row in self.rows}
        if len(widths) != 1:
            raise ValueError("report table rows must have the same number of cells")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("report table page number must be positive")
        if self.section_name is not None:
            _validate_bounded_text(
                self.section_name,
                "report table section name",
                maximum=MAX_REPORT_SECTION_CHARACTERS,
            )
        _validate_bounded_text(
            self.source_excerpt,
            "report table source excerpt",
            maximum=MAX_EXCERPT_CHARACTERS,
        )
        joined = " ".join(cell.text for row in self.rows for cell in row.cells)
        denominators = {
            name
            for name, pattern in (
                ("total_assets", _TOTAL_ASSET_DENOMINATOR),
                ("net_assets", _NET_ASSET_DENOMINATOR),
            )
            if pattern.search(joined)
        }
        if len(denominators) > 1:
            raise ValueError("report table contains mixed denominators")


@dataclass(frozen=True)
class CurrentReportObservation:
    fact_kind: str
    normalized_value: object
    unit: Optional[str]
    page_number: Optional[int]
    section_name: Optional[str]
    source_excerpt: str
    confidence_state: FactConfidence

    def validate(self) -> None:
        _require_exact_record(self, CurrentReportObservation, "current report observation")
        if type(self.fact_kind) is not str or not _FACT_KIND_PATTERN.fullmatch(self.fact_kind):
            raise ValueError("current report fact kind must be a lowercase stable code")
        _validate_public_value(self.normalized_value)
        if self.unit is not None and (
            type(self.unit) is not str or not _UNIT_PATTERN.fullmatch(self.unit)
        ):
            raise ValueError("current report unit must be one stable denominator")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("current report page number must be positive")
        if self.section_name is not None:
            _validate_bounded_text(
                self.section_name,
                "current report section name",
                maximum=MAX_REPORT_SECTION_CHARACTERS,
            )
        _validate_bounded_text(
            self.source_excerpt,
            "current report source excerpt",
            maximum=MAX_EXCERPT_CHARACTERS,
        )
        if type(self.confidence_state) is not FactConfidence:
            raise ValueError("current report confidence state is invalid")


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _normalized_preserving_case(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _normalized_label(value: str) -> str:
    normalized = _normalized(value)
    if re.search(r"[\u3400-\u9fff]", normalized):
        return normalized.replace(" ", "")
    return normalized


def _has_unsafe_name_characters(value: str) -> bool:
    return _DEFAULT_IGNORABLE_PATTERN.search(value) is not None or any(
        unicodedata.category(character) in {"Cf", "Cs"} for character in value
    )


def _percent_value(value: str) -> Optional[Decimal]:
    normalized = _normalized(value)
    if _DECIMAL_PATTERN.fullmatch(normalized) is None:
        return None
    parsed = Decimal(normalized)
    if parsed > 100:
        return None
    return parsed


def _nonnegative_decimal_value(value: str) -> Optional[Decimal]:
    normalized = _normalized(value)
    if _DECIMAL_PATTERN.fullmatch(normalized) is None:
        return None
    return Decimal(normalized)


def _excerpt(value: str) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= MAX_EXCERPT_CHARACTERS:
        return normalized
    return normalized[: MAX_EXCERPT_CHARACTERS - 3] + "..."


def _row_excerpt(row: ReportRow) -> str:
    return _excerpt(" | ".join(cell.text for cell in row.cells))


def _bound_value_excerpt(row: ReportRow) -> str:
    label, unit, value = (cell.text for cell in row.cells)
    return _excerpt(label + value + unit)


def _observation(
    fact_kind: str,
    normalized_value: object,
    unit: Optional[str],
    *,
    page_number: Optional[int],
    section_name: Optional[str],
    source_excerpt: str,
) -> CurrentReportObservation:
    if fact_kind not in COMMON_FACTS | FIXED_INCOME_FACTS:
        raise ValueError("current report fact kind is outside the allowlist")
    observation = CurrentReportObservation(
        fact_kind=fact_kind,
        normalized_value=normalized_value,
        unit=unit,
        page_number=page_number,
        section_name=section_name,
        source_excerpt=_excerpt(source_excerpt),
        confidence_state=FactConfidence.EXACT,
    )
    observation.validate()
    return observation


def _headers(table: ReportTable) -> Optional[Tuple[str, ...]]:
    first = table.rows[0]
    if not all(cell.is_header for cell in first.cells):
        return None
    return tuple(_normalized(cell.text) for cell in first.cells)


def _denominator_unit(value: str, *, concentration: bool) -> Optional[str]:
    values = _CONCENTRATION_DENOMINATOR_UNITS if concentration else _DENOMINATOR_UNITS
    return values.get(_normalized(value))


def _asset_allocation_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        if _headers(table) not in _INDICATOR_HEADERS:
            continue
        for row in table.rows[1:]:
            label, unit_text, value_text = (cell.text for cell in row.cells)
            if _normalized(unit_text) not in _PERCENT_UNITS:
                continue
            match = _ASSET_ROW_PATTERN.fullmatch(_normalized_label(label))
            value = _percent_value(value_text)
            if match is None or value is None:
                continue
            asset = _normalized(match.group(1) or match.group(3))
            denominator = match.group(2) or match.group(4)
            unit = _denominator_unit(denominator, concentration=False)
            if asset.endswith(" assets"):
                asset = asset[: -len(" assets")]
            if unit is None or asset not in _ASSET_FACTS:
                continue
            observations.append(
                _observation(
                    _ASSET_FACTS[asset],
                    value,
                    unit,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_bound_value_excerpt(row),
                )
            )
    return tuple(observations)


def _generic_concentration_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    largest_security = re.compile(
        r"报告期末最大单一证券占(基金资产净值|基金净资产|基金总资产|基金资产)"
        r"|largest security represents percentage of "
        r"(net assets|total assets|fund assets)",
        flags=re.IGNORECASE,
    )
    top_ten = re.compile(
        r"报告期末前十大持仓合计占(基金资产净值|基金净资产|基金总资产|基金资产)"
        r"|top ten holdings percentage of (net assets|total assets|fund assets)",
        flags=re.IGNORECASE,
    )
    observations = []
    for table in tables:
        if _headers(table) not in _INDICATOR_HEADERS:
            continue
        for row in table.rows[1:]:
            label, unit_text, value_text = (cell.text for cell in row.cells)
            if _normalized(unit_text) not in _PERCENT_UNITS:
                continue
            value = _percent_value(value_text)
            if value is None:
                continue
            normalized_label = _normalized_label(label)
            security_match = largest_security.fullmatch(normalized_label)
            top_ten_match = top_ten.fullmatch(normalized_label)
            if security_match is not None:
                denominator = security_match.group(1) or security_match.group(2)
                kind_values = (("current_largest_security_weight_percent", value),)
            elif top_ten_match is not None:
                denominator = top_ten_match.group(1) or top_ten_match.group(2)
                kind_values = (("current_top_ten_holdings_weight_percent", value),)
            else:
                continue
            denominator_unit = _denominator_unit(denominator, concentration=True)
            if denominator_unit is None:
                continue
            for fact_kind, normalized_value in kind_values:
                observations.append(
                    _observation(
                        fact_kind,
                        normalized_value,
                        None if fact_kind.endswith("_name") else denominator_unit,
                        page_number=table.page_number,
                        section_name=table.section_name,
                        source_excerpt=_bound_value_excerpt(row),
                    )
                )
    return tuple(observations)


def _ranked_table_rows(
    table: ReportTable,
    *,
    name_header: str,
) -> Optional[Tuple[Tuple[int, str, Decimal], ...]]:
    headers = _headers(table)
    if headers is None or len(headers) != 3:
        return None
    rank_header, actual_name_header, weight_header = headers
    if rank_header not in {"排名", "序号", "rank"} or actual_name_header != name_header:
        return None
    if weight_header not in _RANKED_WEIGHT_HEADERS:
        return None
    parsed = []
    for row in table.rows[1:]:
        rank_text, name, weight_text = (cell.text for cell in row.cells)
        normalized_rank = _normalized(rank_text)
        if not normalized_rank.isascii() or not normalized_rank.isdecimal():
            return None
        rank = int(normalized_rank)
        weight = _percent_value(weight_text)
        if (
            rank <= 0
            or str(rank) != normalized_rank
            or weight is None
            or not name.strip()
            or _has_unsafe_name_characters(name)
        ):
            return None
        parsed.append((rank, " ".join(name.split()), weight))
    ranks = tuple(item[0] for item in parsed)
    names = tuple(_normalized(item[1]) for item in parsed)
    weights = tuple(item[2] for item in parsed)
    if (
        not parsed
        or len(ranks) != len(set(ranks))
        or len(names) != len(set(names))
        or set(ranks) != set(range(1, len(ranks) + 1))
        or any(left < right for left, right in zip(weights, weights[1:]))
    ):
        return None
    return tuple(parsed)


def _security_concentration_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        parsed = _ranked_table_rows(table, name_header="证券名称")
        if parsed is None:
            continue
        section = _normalized(table.section_name or "")
        top_ten_scope = section in {
            _normalized(value) for value in _TOP_TEN_SECURITY_SCOPES
        }
        complete_scope = section in {
            _normalized(value) for value in _COMPLETE_SECURITY_SCOPES
        }
        if not top_ten_scope and not complete_scope:
            continue
        headers = _headers(table)
        if headers is None:
            continue
        unit = _RANKED_WEIGHT_HEADERS[headers[2]]
        first_row = table.rows[1 + next(index for index, item in enumerate(parsed) if item[0] == 1)]
        observations.append(
            _observation(
                "current_largest_security_weight_percent",
                next(item[2] for item in parsed if item[0] == 1),
                unit,
                page_number=table.page_number,
                section_name=table.section_name,
                source_excerpt=_row_excerpt(first_row),
            )
        )
        if top_ten_scope and len(parsed) == 10:
            top_ten_weight = sum((item[2] for item in parsed), Decimal("0"))
            if top_ten_weight <= 100:
                observations.append(
                    _observation(
                        "current_top_ten_holdings_weight_percent",
                        top_ten_weight,
                        unit,
                        page_number=table.page_number,
                        section_name=table.section_name,
                        source_excerpt=table.source_excerpt,
                    )
                )
        if complete_scope:
            observations.append(
                _observation(
                    "holdings_evidence_complete",
                    True,
                    None,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=table.source_excerpt,
                )
            )
    return tuple(observations)


def _industry_observations(
    tables: Tuple[ReportTable, ...],
    taxonomy_mappings: Tuple[IndustryTaxonomyMapping, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        headers = _headers(table)
        if headers is None or len(headers) != 5:
            continue
        standard_header, code_header, rank_header, name_header, weight_header = headers
        if (
            standard_header not in _INDUSTRY_STANDARD_HEADERS
            or code_header not in _INDUSTRY_CODE_HEADERS
            or rank_header not in _INDUSTRY_RANK_HEADERS
            or name_header not in _INDUSTRY_NAME_HEADERS
            or weight_header not in _RANKED_WEIGHT_HEADERS
        ):
            continue

        section = _normalized(table.section_name or "")
        complete_scope = section in {
            _normalized(value) for value in _COMPLETE_INDUSTRY_SCOPES
        }
        if not complete_scope:
            continue

        rows = []
        for row in table.rows[1:]:
            standard, code, rank_text, name, weight_text = (
                cell.text for cell in row.cells
            )
            if any(
                _has_unsafe_name_characters(value)
                for value in (standard, code, name)
            ):
                rows = []
                break
            normalized_rank = _normalized(rank_text)
            weight = _percent_value(weight_text)
            if (
                not normalized_rank.isascii()
                or not normalized_rank.isdecimal()
                or int(normalized_rank) <= 0
                or str(int(normalized_rank)) != normalized_rank
                or weight is None
            ):
                rows = []
                break
            rows.append(
                IndustryDistributionRow(
                    classification_standard=" ".join(standard.split()),
                    industry_code=" ".join(code.split()),
                    industry_name=" ".join(name.split()),
                    rank=int(normalized_rank),
                    weight=weight,
                    unit="percent",
                )
            )
        if not rows:
            continue

        validated = validate_industry_distribution(
            rows=tuple(rows),
            complete_scope=True,
            mappings=taxonomy_mappings,
        )
        if validated is None:
            continue
        largest = validated.rows[0]
        largest_row = table.rows[1]
        observation_unit = _RANKED_WEIGHT_HEADERS[weight_header]
        observations.extend(
            (
                _observation(
                    "current_largest_industry_name",
                    largest.industry_name,
                    None,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_row_excerpt(largest_row),
                ),
                _observation(
                    "current_largest_industry_weight_percent",
                    largest.weight,
                    observation_unit,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_row_excerpt(largest_row),
                ),
            )
        )
        observations.append(
            _observation(
                "current_industry_count",
                len(validated.rows),
                None,
                page_number=table.page_number,
                section_name=table.section_name,
                source_excerpt=table.source_excerpt,
            )
        )
    return tuple(observations)


def _explicit_common_text_observations(
    text_blocks: Tuple[str, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for raw_text in text_blocks:
        if type(raw_text) is not str or not raw_text.strip():
            raise ValueError("current report text blocks must be bounded non-empty text")
        text = _normalized_preserving_case(raw_text)
        asset = _ASSET_SENTENCE_PATTERN.fullmatch(text)
        security = _LARGEST_SECURITY_SENTENCE_PATTERN.fullmatch(text)
        top_ten = _TOP_TEN_SENTENCE_PATTERN.fullmatch(text)
        if asset is not None:
            asset_name = _normalized(asset.group(1) or asset.group(4))
            if asset_name.endswith(" assets"):
                asset_name = asset_name[: -len(" assets")]
            denominator = asset.group(2) or asset.group(6)
            value = _percent_value(asset.group(3) or asset.group(5))
            unit = _denominator_unit(denominator, concentration=False)
            kind_values = ((_ASSET_FACTS.get(asset_name), value, unit),)
        elif security is not None:
            denominator = security.group(1) or security.group(4)
            value = _percent_value(security.group(2) or security.group(3))
            kind_values = (
                (
                    "current_largest_security_weight_percent",
                    value,
                    _denominator_unit(denominator, concentration=True),
                ),
            )
        elif top_ten is not None:
            denominator = top_ten.group(1) or top_ten.group(4)
            value = _percent_value(top_ten.group(2) or top_ten.group(3))
            kind_values = (
                (
                    "current_top_ten_holdings_weight_percent",
                    value,
                    _denominator_unit(denominator, concentration=True),
                ),
            )
        else:
            continue
        for fact_kind, value, unit in kind_values:
            if fact_kind is None or value is None or (
                fact_kind != "current_largest_industry_name" and unit is None
            ):
                continue
            observations.append(
                _observation(
                    fact_kind,
                    value,
                    unit,
                    page_number=None,
                    section_name=None,
                    source_excerpt=raw_text,
                )
            )
    return tuple(observations)


def _duration_and_maturity_observations(
    tables: Tuple[ReportTable, ...],
    text_blocks: Tuple[str, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        if _headers(table) not in _INDICATOR_HEADERS:
            continue
        for row in table.rows[1:]:
            label, unit_text, value_text = (cell.text for cell in row.cells)
            metric = _FIXED_INCOME_METRICS.get(_normalized_label(label))
            value = _nonnegative_decimal_value(value_text)
            if metric is None or value is None or _normalized_label(unit_text) != metric[1]:
                continue
            observations.append(
                _observation(
                    metric[0],
                    value,
                    metric[2],
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_bound_value_excerpt(row),
                )
            )

    text_patterns = (
        (
            re.compile(r"报告期末组合有效久期为(" + _DECIMAL_PATTERN.pattern + r")年[。.]?"),
            "current_effective_duration",
            "years",
        ),
        (
            re.compile(
                r"报告期末加权平均剩余期限为(" + _DECIMAL_PATTERN.pattern + r")天[。.]?"
            ),
            "current_weighted_average_maturity_days",
            "days",
        ),
    )
    for block in text_blocks:
        normalized = _normalized_label(block)
        for pattern, fact_kind, unit in text_patterns:
            match = pattern.fullmatch(normalized)
            if match is None:
                continue
            observations.append(
                _observation(
                    fact_kind,
                    Decimal(match.group(1)),
                    unit,
                    page_number=None,
                    section_name=None,
                    source_excerpt=block,
                )
            )
    return tuple(observations)


def _convertible_exchangeable_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    chinese = re.compile(
        r"报告期末(可转换债券|可交换债券)资产占"
        r"(基金总资产|基金资产净值|基金净资产)的"
    )
    english = re.compile(
        r"(convertible bond|exchangeable bond) assets at the end of the reporting "
        r"period as a percentage of (total assets|net assets)"
    )
    observations = []
    for table in tables:
        if _headers(table) not in _INDICATOR_HEADERS:
            continue
        for row in table.rows[1:]:
            label, unit_text, value_text = (cell.text for cell in row.cells)
            if _normalized(unit_text) not in _PERCENT_UNITS:
                continue
            normalized_label = _normalized_label(label)
            match = chinese.fullmatch(normalized_label) or english.fullmatch(
                normalized_label
            )
            value = _percent_value(value_text)
            if match is None or value is None:
                continue
            denominator = _denominator_unit(match.group(2), concentration=False)
            fact_kind = _BOND_TYPE_FACTS.get(_normalized(match.group(1)))
            if denominator is None or fact_kind is None:
                continue
            observations.append(
                _observation(
                    fact_kind,
                    value,
                    denominator,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_bound_value_excerpt(row),
                )
            )
    return tuple(observations)


def _issuer_category(value: str) -> Optional[str]:
    normalized = _normalized_label(value)
    if normalized in _SOVEREIGN_CATEGORIES:
        return "sovereign"
    if normalized in _POLICY_BANK_CATEGORIES:
        return "policy_bank"
    if normalized in _NON_SOVEREIGN_CATEGORIES:
        return "non_sovereign"
    return None


def _canonical_credit_rating(value: str) -> Optional[str]:
    normalized = _normalized_label(value)
    if normalized in _HIGH_QUALITY_RATINGS | _BELOW_AA_PLUS_RATINGS:
        return normalized.removesuffix("级")
    if normalized in _UNRATED_RATINGS:
        return "unrated"
    return None


def _credit_distribution_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        headers = _headers(table)
        if headers is None or len(headers) != 3:
            continue
        rating_header, category_header, weight_header = headers
        if (
            rating_header not in _CREDIT_RATING_HEADERS
            or category_header not in _ISSUER_CATEGORY_HEADERS
            or weight_header not in _CREDIT_WEIGHT_HEADERS
            or _normalized(table.section_name or "")
            not in {_normalized(value) for value in _COMPLETE_CREDIT_SCOPES}
        ):
            continue
        unit = _CREDIT_WEIGHT_HEADERS[weight_header]
        high_quality = Decimal("0")
        below_aa_plus = Decimal("0")
        unrated_non_sovereign = Decimal("0")
        total = Decimal("0")
        seen = set()
        found = {"high": False, "below": False, "unrated_non_sovereign": False}
        valid = True
        for row in table.rows[1:]:
            rating_text, category_text, weight_text = (cell.text for cell in row.cells)
            rating = _canonical_credit_rating(rating_text)
            category = _issuer_category(category_text)
            weight = _percent_value(weight_text)
            key = (rating, category)
            if (
                rating is None
                or category is None
                or weight is None
                or key in seen
            ):
                valid = False
                break
            seen.add(key)
            total += weight
            if rating in _HIGH_QUALITY_RATINGS:
                high_quality += weight
                found["high"] = True
            elif rating in _BELOW_AA_PLUS_RATINGS:
                below_aa_plus += weight
                found["below"] = True
            elif rating == "unrated" and category == "non_sovereign":
                unrated_non_sovereign += weight
                found["unrated_non_sovereign"] = True
        if (
            not valid
            or not all(found.values())
            or total > 100
            or (
                unit in {"percent_of_bond_assets", "percent_of_fixed_income_assets"}
                and total != 100
            )
        ):
            continue
        for fact_kind, value in (
            ("current_high_quality_fixed_income_percent", high_quality),
            ("current_below_aa_plus_exposure_percent", below_aa_plus),
            (
                "current_unrated_non_sovereign_exposure_percent",
                unrated_non_sovereign,
            ),
        ):
            observations.append(
                _observation(
                    fact_kind,
                    value,
                    unit,
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=table.source_excerpt,
                )
            )
    return tuple(observations)


def _gross_leverage_observations(
    tables: Tuple[ReportTable, ...],
    text_blocks: Tuple[str, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        if _headers(table) not in _LEVERAGE_HEADERS:
            continue
        for row in table.rows[1:]:
            label, denominator_text, unit_text, value_text = (
                cell.text for cell in row.cells
            )
            value = _nonnegative_decimal_value(value_text)
            if (
                _normalized_label(label) not in _LEVERAGE_LABELS
                or _normalized(unit_text) not in _PERCENT_UNITS
                or _denominator_unit(denominator_text, concentration=False)
                != "percent_of_net_assets"
                or value is None
            ):
                continue
            observations.append(
                _observation(
                    "current_gross_leverage_percent",
                    value,
                    "percent_of_net_assets",
                    page_number=table.page_number,
                    section_name=table.section_name,
                    source_excerpt=_row_excerpt(row),
                )
            )

    pattern = re.compile(
        r"报告期末基金总资产占基金资产净值的比例为("
        + _DECIMAL_PATTERN.pattern
        + r")%[。.]?"
    )
    for block in text_blocks:
        match = pattern.fullmatch(_normalized_label(block))
        if match is not None:
            observations.append(
                _observation(
                    "current_gross_leverage_percent",
                    Decimal(match.group(1)),
                    "percent_of_net_assets",
                    page_number=None,
                    section_name=None,
                    source_excerpt=block,
                )
            )
    return tuple(observations)


def _issuer_concentration_observations(
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    observations = []
    for table in tables:
        headers = _headers(table)
        if headers is None or len(headers) != 3:
            continue
        category_header, name_header, weight_header = headers
        if (
            category_header not in _ISSUER_CATEGORY_HEADERS
            or name_header not in _ISSUER_NAME_HEADERS
            or weight_header not in _ISSUER_WEIGHT_HEADERS
            or _normalized(table.section_name or "")
            not in {_normalized(value) for value in _COMPLETE_ISSUER_SCOPES}
        ):
            continue
        unit = _ISSUER_WEIGHT_HEADERS[weight_header]
        seen_names = set()
        non_sovereign_weights = []
        total = Decimal("0")
        valid = True
        for row in table.rows[1:]:
            category_text, name_text, weight_text = (cell.text for cell in row.cells)
            category = _issuer_category(category_text)
            name = _normalized_label(name_text)
            weight = _percent_value(weight_text)
            if (
                category is None
                or not name
                or _has_unsafe_name_characters(name_text)
                or name in seen_names
                or weight is None
            ):
                valid = False
                break
            seen_names.add(name)
            total += weight
            if category == "non_sovereign":
                non_sovereign_weights.append(weight)
        if not valid or total > 100 or not non_sovereign_weights:
            continue
        observations.append(
            _observation(
                "current_largest_non_sovereign_issuer_percent",
                max(non_sovereign_weights),
                unit,
                page_number=table.page_number,
                section_name=table.section_name,
                source_excerpt=table.source_excerpt,
            )
        )
    return tuple(observations)


def _validated_unique_observations(
    observations: Tuple[CurrentReportObservation, ...],
) -> Tuple[CurrentReportObservation, ...]:
    unique = []
    seen = set()
    for observation in observations:
        observation.validate()
        key = (
            observation.fact_kind,
            repr(observation.normalized_value),
            observation.unit,
            observation.page_number,
            observation.section_name,
            observation.source_excerpt,
            observation.confidence_state,
        )
        if key not in seen:
            unique.append(observation)
            seen.add(key)
    return tuple(unique)


def extract_common_report_observations(
    *,
    text_blocks: Tuple[str, ...],
    tables: Tuple[ReportTable, ...],
    taxonomy_mappings: Tuple[IndustryTaxonomyMapping, ...] = (
        PRODUCTION_TAXONOMY_MAPPINGS
    ),
) -> Tuple[CurrentReportObservation, ...]:
    """Extract only exact current asset and concentration observations."""

    if (
        type(text_blocks) is not tuple
        or type(tables) is not tuple
        or type(taxonomy_mappings) is not tuple
    ):
        raise ValueError("current report evidence must use immutable tuples")
    validate_industry_distribution(
        rows=(),
        complete_scope=False,
        mappings=taxonomy_mappings,
    )
    for table in tables:
        if type(table) is not ReportTable:
            raise ValueError("current report tables must use exact ReportTable records")
        table.validate()
    observations = (
        _asset_allocation_observations(tables)
        + _generic_concentration_observations(tables)
        + _security_concentration_observations(tables)
        + _industry_observations(tables, taxonomy_mappings)
        + _explicit_common_text_observations(text_blocks)
    )
    return _validated_unique_observations(observations)


def extract_fixed_income_report_observations(
    *,
    text_blocks: Tuple[str, ...],
    tables: Tuple[ReportTable, ...],
) -> Tuple[CurrentReportObservation, ...]:
    """Extract only exact current fixed-income observations."""

    if type(text_blocks) is not tuple or type(tables) is not tuple:
        raise ValueError("current report evidence must use immutable tuples")
    for block in text_blocks:
        if type(block) is not str:
            raise ValueError("current report text blocks must be exact strings")
    for table in tables:
        if type(table) is not ReportTable:
            raise ValueError("current report tables must use exact ReportTable records")
        table.validate()
    observations = (
        _duration_and_maturity_observations(tables, text_blocks)
        + _convertible_exchangeable_observations(tables)
        + _credit_distribution_observations(tables)
        + _gross_leverage_observations(tables, text_blocks)
        + _issuer_concentration_observations(tables)
    )
    return _validated_unique_observations(observations)
