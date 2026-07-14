from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields
from datetime import date, timezone
from typing import Optional, Tuple

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.documents import OfficialDocumentCandidate
from kunjin.funds.risk.failures import DocumentFailureReason


@dataclass(frozen=True)
class TextBlockView:
    text: str
    page_number: Optional[int]
    section_name: Optional[str]
    is_heading: bool
    is_title: bool

    def validate(self) -> None:
        if type(self) is not TextBlockView or set(vars(self)) != {
            field.name for field in fields(TextBlockView)
        }:
            raise ValueError("text block view must be an exact record")
        if type(self.text) is not str or not self.text.strip() or "\x00" in self.text:
            raise ValueError("text block view text is invalid")
        if self.page_number is not None and (
            type(self.page_number) is not int or self.page_number <= 0
        ):
            raise ValueError("text block view page number is invalid")
        if self.section_name is not None and (
            type(self.section_name) is not str
            or not self.section_name.strip()
            or "\x00" in self.section_name
        ):
            raise ValueError("text block view section is invalid")
        if type(self.is_heading) is not bool:
            raise ValueError("text block heading state is invalid")
        if type(self.is_title) is not bool or (self.is_heading and self.is_title):
            raise ValueError("text block title state is invalid")


class ConvertedDocumentContractError(ValueError):
    def __init__(self, reason_code: DocumentFailureReason) -> None:
        if reason_code is not DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID:
            raise ValueError("converted report contract reason is invalid")
        self.reason_code = reason_code
        super().__init__("converted report contract is invalid")


_KIND_PATTERNS = (
    (DocumentKind.PRODUCT_SUMMARY, re.compile(r"基金产品资料概要")),
    (DocumentKind.PROSPECTUS_UPDATE, re.compile(r"(?:更新.*招募说明书|招募说明书.*更新)")),
    (
        DocumentKind.PROSPECTUS,
        re.compile(r"^(?!.*(?:更新.*招募说明书|招募说明书.*更新)).*招募说明书"),
    ),
    (DocumentKind.FUND_CONTRACT, re.compile(r"基金合同")),
    (DocumentKind.SEMIANNUAL_REPORT, re.compile(r"(?:半年度报告|中期报告)")),
    (DocumentKind.ANNUAL_REPORT, re.compile(r"(?<!半)年度报告")),
    (DocumentKind.QUARTERLY_REPORT, re.compile(r"(?:第?[一二三四1-4]季度报告|季度报告)")),
    (
        DocumentKind.INDEX_METHODOLOGY,
        re.compile(r"(?:指数编制方案|指数方法论|指数编制细则|指数规则)"),
    ),
    (
        DocumentKind.CLASSIFICATION_ANNOUNCEMENT,
        re.compile(r"(?:基金转型|基金合并|投资范围变更|业绩比较基准变更|基金类型变更)"),
    ),
)

_KIND_MARKERS = {
    DocumentKind.PRODUCT_SUMMARY: ("基金产品资料概要",),
    DocumentKind.PROSPECTUS_UPDATE: ("招募说明书更新", "更新招募说明书"),
    DocumentKind.PROSPECTUS: ("招募说明书",),
    DocumentKind.FUND_CONTRACT: ("基金合同",),
    DocumentKind.SEMIANNUAL_REPORT: ("半年度报告", "中期报告"),
    DocumentKind.ANNUAL_REPORT: ("年度报告",),
    DocumentKind.QUARTERLY_REPORT: ("季度报告",),
    DocumentKind.INDEX_METHODOLOGY: ("指数编制方案", "指数方法论", "指数编制细则", "指数规则"),
    DocumentKind.CLASSIFICATION_ANNOUNCEMENT: (
        "基金转型",
        "基金合并",
        "投资范围变更",
        "业绩比较基准变更",
        "基金类型变更",
    ),
}

_QUARTERS = {
    "1": (3, 31),
    "一": (3, 31),
    "2": (6, 30),
    "二": (6, 30),
    "3": (9, 30),
    "三": (9, 30),
    "4": (12, 31),
    "四": (12, 31),
}
_MAX_LEADING_COVER_BLOCKS = 8


def report_period_end(title_or_heading: str) -> Optional[date]:
    if type(title_or_heading) is not str or not title_or_heading.strip():
        return None
    normalized = _normalized_converted_text(title_or_heading)
    periods = set()
    try:
        for match in re.finditer(r"(?<![0-9])([0-9]{4})年(?:半年度|中期)报告", normalized):
            periods.add(date(int(match.group(1)), 6, 30))
        for match in re.finditer(r"(?<![0-9])([0-9]{4})年年度报告", normalized):
            periods.add(date(int(match.group(1)), 12, 31))
        for match in re.finditer(
            r"(?<![0-9])([0-9]{4})年第?([一二三四1-4])季度报告",
            normalized,
        ):
            month, day = _QUARTERS[match.group(2)]
            periods.add(date(int(match.group(1)), month, day))
    except ValueError:
        return None
    if len(periods) != 1:
        return None
    return next(iter(periods))


def document_kind_markers(kind: DocumentKind) -> Tuple[str, ...]:
    if type(kind) is not DocumentKind:
        raise ValueError("document kind must be exact")
    return _KIND_MARKERS.get(kind, ())


def has_exact_leading_cover_title(
    blocks: Tuple[TextBlockView, ...],
    candidate_title: str,
) -> bool:
    if type(blocks) is not tuple or type(candidate_title) is not str or not candidate_title.strip():
        return False
    normalized_candidate_title = _normalized_converted_text(candidate_title)
    return any(
        type(block) is TextBlockView
        and _normalized_converted_text(block.text) == normalized_candidate_title
        for block in blocks[:_MAX_LEADING_COVER_BLOCKS]
    )


def validate_converted_document_contract(
    blocks: Tuple[TextBlockView, ...],
    candidate: OfficialDocumentCandidate,
) -> None:
    if type(blocks) is not tuple or type(candidate) is not OfficialDocumentCandidate:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    try:
        candidate.validate()
        for block in blocks:
            block.validate()
    except (AttributeError, ValueError) as exc:
        raise ConvertedDocumentContractError(
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID
        ) from exc

    leading_blocks = blocks[:_MAX_LEADING_COVER_BLOCKS]
    candidate_kinds = _document_kinds(candidate.title)
    heading_kinds = {
        kind
        for block in leading_blocks
        if block.is_heading
        for kind in _document_kinds(block.text)
    }
    title_kinds = {
        kind for block in blocks if block.is_title for kind in _document_kinds(block.text)
    }
    has_leading_cover_title = has_exact_leading_cover_title(blocks, candidate.title)
    if candidate_kinds != {candidate.document_kind}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if heading_kinds and heading_kinds != {candidate.document_kind}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if title_kinds and title_kinds != {candidate.document_kind}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if not heading_kinds and not title_kinds and not has_leading_cover_title:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)

    if candidate.document_kind not in {
        DocumentKind.ANNUAL_REPORT,
        DocumentKind.SEMIANNUAL_REPORT,
        DocumentKind.QUARTERLY_REPORT,
    }:
        return
    candidate_period = report_period_end(candidate.title)
    heading_periods = {
        period
        for block in leading_blocks
        if block.is_heading
        and _document_kinds(block.text)
        & {
            DocumentKind.ANNUAL_REPORT,
            DocumentKind.SEMIANNUAL_REPORT,
            DocumentKind.QUARTERLY_REPORT,
        }
        for period in (report_period_end(block.text),)
    }
    if candidate_period is None:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if heading_periods and heading_periods != {candidate_period}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    title_periods = {
        period
        for block in blocks
        if block.is_title
        and _document_kinds(block.text)
        & {
            DocumentKind.ANNUAL_REPORT,
            DocumentKind.SEMIANNUAL_REPORT,
            DocumentKind.QUARTERLY_REPORT,
        }
        for period in (report_period_end(block.text),)
    }
    cover_periods = {candidate_period} if has_leading_cover_title else set()
    if title_periods and title_periods != {candidate_period}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if cover_periods and cover_periods != {candidate_period}:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if not heading_periods and not title_periods and not cover_periods:
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)
    if (
        candidate.published_at is not None
        and candidate_period > candidate.published_at.astimezone(timezone.utc).date()
    ):
        raise ConvertedDocumentContractError(DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID)


def _document_kinds(value: str) -> set[DocumentKind]:
    normalized = _normalized_converted_text(value)
    return {kind for kind, pattern in _KIND_PATTERNS if pattern.search(normalized)}


def _normalized_converted_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())
