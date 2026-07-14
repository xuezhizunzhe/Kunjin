from __future__ import annotations

import io
import unittest
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from kunjin.funds.risk.failures import DocumentFailureReason
from kunjin.funds.risk.models import FactConfidence
from kunjin.funds.risk.parsers import (
    RiskDocumentParseError,
    _converted_html_content,
    _docx_content,
    _html_content,
    _pdf_content,
    parsed_fact_from_current_observation,
)
from kunjin.funds.risk.report_facts import (
    COMMON_FACTS,
    MAX_REPORT_CELL_CHARACTERS,
    CurrentReportObservation,
    ReportCell,
    ReportRow,
    ReportTable,
    extract_common_report_observations,
)


def report_table() -> ReportTable:
    return ReportTable(
        rows=(
            ReportRow(
                cells=(
                    ReportCell("指标", True),
                    ReportCell("单位", True),
                    ReportCell("数值", True),
                )
            ),
            ReportRow(
                cells=(
                    ReportCell("报告期末股票资产占基金总资产的", False),
                    ReportCell("%", False),
                    ReportCell("35.2", False),
                )
            ),
        ),
        page_number=None,
        section_name="资产组合报告",
        source_excerpt="指标 | 单位 | 数值；报告期末股票资产占基金总资产的 | % | 35.2",
    )


def common_table(
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    *,
    section_name: str,
) -> ReportTable:
    return ReportTable(
        rows=(
            ReportRow(tuple(ReportCell(value, True) for value in headers)),
            *(ReportRow(tuple(ReportCell(value, False) for value in row)) for row in rows),
        ),
        page_number=3,
        section_name=section_name,
        source_excerpt="；".join(" | ".join(row) for row in (headers, *rows)),
    )


def docx_with_table(*, table_count: int = 1) -> bytes:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        b'  <Default Extension="xml" ContentType="application/xml"/>\n'
        b'  <Override PartName="/word/document.xml" ContentType="'
        b'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        b"</Types>"
    )
    table = """<w:tbl>
      <w:tr><w:trPr><w:tblHeader/></w:trPr>
        <w:tc><w:p><w:r><w:t>指标</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>单位</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>数值</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>报告期末股票资产占基金总资产的</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>%</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>35.2</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>"""
    document = (
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>资产组合报告</w:t></w:r></w:p>"""
        + table * table_count
        + """
  </w:body>
</w:document>"""
    ).encode()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return payload.getvalue()


def replace_docx_document(payload: bytes, old: bytes, new: bytes) -> bytes:
    source = io.BytesIO(payload)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as archive:
        entries = tuple((item.filename, archive.read(item.filename)) for item in archive.infolist())
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value.replace(old, new, 1))
    return target.getvalue()


class CurrentReportRecordTest(unittest.TestCase):
    def test_exact_records_are_frozen_and_validate(self) -> None:
        table = report_table()
        table.validate()
        observation = CurrentReportObservation(
            fact_kind="current_stock_asset_allocation_percent",
            normalized_value=D("35.2"),
            unit="percent_of_total_assets",
            page_number=2,
            section_name="资产组合报告",
            source_excerpt="报告期末股票资产占基金总资产的35.2%",
            confidence_state=FactConfidence.EXACT,
        )
        observation.validate()

        with self.assertRaises(FrozenInstanceError):
            table.page_number = 3  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            observation.unit = None  # type: ignore[misc]

    def test_records_reject_subclasses_mutable_containers_and_unexpected_state(self) -> None:
        class CellSubclass(ReportCell):
            pass

        class RowSubclass(ReportRow):
            pass

        class TableSubclass(ReportTable):
            pass

        class ObservationSubclass(CurrentReportObservation):
            pass

        with self.assertRaisesRegex(ValueError, "subclasses"):
            CellSubclass("指标", True).validate()
        with self.assertRaisesRegex(ValueError, "immutable tuple"):
            ReportRow(cells=[ReportCell("指标", True)]).validate()  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "subclasses"):
            RowSubclass((ReportCell("指标", True),)).validate()
        with self.assertRaisesRegex(ValueError, "subclasses"):
            TableSubclass(
                (ReportRow((ReportCell("指标", True),)),),
                None,
                None,
                "指标",
            ).validate()
        with self.assertRaisesRegex(ValueError, "deeply immutable"):
            CurrentReportObservation(
                "current_industry_count",
                [1],  # type: ignore[arg-type]
                None,
                None,
                None,
                "行业数量为1",
                FactConfidence.EXACT,
            ).validate()
        with self.assertRaisesRegex(ValueError, "subclasses"):
            ObservationSubclass(
                "current_industry_count",
                1,
                None,
                None,
                None,
                "行业数量为1",
                FactConfidence.EXACT,
            ).validate()
        cell = ReportCell("指标", True)
        object.__setattr__(cell, "unexpected", "value")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            cell.validate()

    def test_cells_rows_and_tables_reject_ambiguous_or_oversized_shapes(self) -> None:
        invalid_cells = (
            ReportCell("", False),
            ReportCell("contains\x00control", False),
            ReportCell("x" * (MAX_REPORT_CELL_CHARACTERS + 1), False),
            ReportCell("value", 1),  # type: ignore[arg-type]
        )
        for cell in invalid_cells:
            with self.subTest(cell=repr(cell)):
                with self.assertRaises(ValueError):
                    cell.validate()

        with self.assertRaisesRegex(ValueError, "duplicate headers"):
            ReportRow(
                (
                    ReportCell("单位", True),
                    ReportCell("单位", True),
                )
            ).validate()
        with self.assertRaisesRegex(ValueError, "duplicate headers"):
            ReportRow(
                (
                    ReportCell("Value", True),
                    ReportCell("value", True),
                )
            ).validate()
        with self.assertRaisesRegex(ValueError, "same number of cells"):
            replace(
                report_table(),
                rows=report_table().rows
                + (ReportRow((ReportCell("extra", False),)),),
            ).validate()
        with self.assertRaisesRegex(ValueError, "mixed denominators"):
            replace(
                report_table(),
                rows=report_table().rows
                + (
                    ReportRow(
                        (
                            ReportCell("债券占基金资产净值比例", False),
                            ReportCell("%", False),
                            ReportCell("60", False),
                        )
                    ),
                ),
            ).validate()
        with patch("kunjin.funds.risk.report_facts.MAX_REPORT_ROWS", 1):
            with self.assertRaisesRegex(ValueError, "row limit"):
                report_table().validate()

    def test_observations_reject_invalid_boundaries_and_mixed_units(self) -> None:
        base = CurrentReportObservation(
            "current_stock_asset_allocation_percent",
            D("35.2"),
            "percent_of_total_assets",
            1,
            "资产组合报告",
            "股票占基金总资产35.2%",
            FactConfidence.EXACT,
        )
        invalid = (
            replace(base, fact_kind="CurrentStock"),
            replace(base, normalized_value=D("NaN")),
            replace(base, unit="percent_of_total_assets|percent_of_net_assets"),
            replace(base, page_number=0),
            replace(base, section_name=""),
            replace(base, source_excerpt=""),
            replace(base, confidence_state="exact"),  # type: ignore[arg-type]
        )
        for observation in invalid:
            with self.subTest(observation=repr(observation)):
                with self.assertRaises(ValueError):
                    observation.validate()


class CurrentReportAdapterTest(unittest.TestCase):
    html = """<!doctype html><html><body>
<h2>资产组合报告</h2>
<table>
<tr><th>指标</th><th>单位</th><th>数值</th></tr>
<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35.2</td></tr>
</table>
</body></html>"""

    def test_html_converted_html_and_docx_preserve_the_same_supported_table(self) -> None:
        _, html_tables = _html_content(self.html.encode(), "utf-8")
        _, _, converted_tables = _converted_html_content(self.html.encode())
        _, docx_tables = _docx_content(docx_with_table())

        self.assertEqual(html_tables, (report_table(),))
        self.assertEqual(converted_tables, html_tables)
        self.assertEqual(docx_tables, html_tables)

    def test_pdf_keeps_explicit_text_only_and_never_reconstructs_tables(self) -> None:
        fake_reader = unittest.mock.MagicMock()
        fake_reader.is_encrypted = False
        fake_reader.pages = [
            unittest.mock.MagicMock(
                extract_text=unittest.mock.MagicMock(
                    return_value=(
                        "CURRENT ASSET ALLOCATION\n"
                        "Stocks    percent of total assets    35.2\n"
                    )
                )
            )
        ]
        with patch("kunjin.funds.risk.parsers.PdfReader", return_value=fake_reader):
            with patch(
                "kunjin.funds.risk.parsers._pdf_has_active_or_embedded_content",
                return_value=False,
            ):
                blocks, tables = _pdf_content(b"synthetic PDF")

        self.assertTrue(blocks)
        self.assertEqual(tables, ())

    def test_unsupported_html_table_shapes_are_not_retained(self) -> None:
        cases = (
            self.html.replace(
                "<td>报告期末股票资产占基金总资产的</td>",
                "<td><table><tr><td>nested</td></tr></table></td>",
            ),
            self.html.replace("<th>指标</th>", '<th colspan="2">指标</th>'),
            self.html.replace("<th>数值</th>", "<th>单位</th>"),
            self.html.replace("<td>35.2</td>", "<td></td>"),
        )
        for html in cases:
            with self.subTest(html=html[:100]):
                _, tables = _html_content(html.encode(), "utf-8")
                self.assertEqual(tables, ())

    def test_oversized_table_cell_is_a_resource_failure(self) -> None:
        oversized = self.html.replace("35.2", "x" * (MAX_REPORT_CELL_CHARACTERS + 1))

        with self.assertRaises(RiskDocumentParseError) as caught:
            _html_content(oversized.encode(), "utf-8")

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.RESOURCE_LIMIT,
        )

    def test_docx_table_row_limit_fails_closed_instead_of_dropping_the_table(self) -> None:
        with patch("kunjin.funds.risk.parsers.MAX_REPORT_ROWS", 1):
            with patch("kunjin.funds.risk.report_facts.MAX_REPORT_ROWS", 1):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    _docx_content(docx_with_table())

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.RESOURCE_LIMIT,
        )

    def test_docx_table_row_limit_is_cumulative_across_tables(self) -> None:
        with patch("kunjin.funds.risk.parsers.MAX_REPORT_ROWS", 3):
            with patch("kunjin.funds.risk.report_facts.MAX_REPORT_ROWS", 3):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    _docx_content(docx_with_table(table_count=2))

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.RESOURCE_LIMIT,
        )

    def test_invalid_docx_table_rows_still_consume_the_shared_row_budget(self) -> None:
        invalid_then_valid = replace_docx_document(
            docx_with_table(table_count=2),
            b"<w:tc><w:p><w:r><w:t>\xe6\x95\xb0\xe5\x80\xbc</w:t>",
            b"<w:tc><w:p><w:r><w:t>\xe5\x8d\x95\xe4\xbd\x8d</w:t>",
        )
        with patch("kunjin.funds.risk.parsers.MAX_REPORT_ROWS", 3):
            with patch("kunjin.funds.risk.report_facts.MAX_REPORT_ROWS", 3):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    _docx_content(invalid_then_valid)

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.RESOURCE_LIMIT,
        )

    def test_docx_legacy_horizontal_merge_is_not_retained_as_structured_evidence(self) -> None:
        for merge_value in (b"restart", b"continue"):
            with self.subTest(merge_value=merge_value):
                merged = replace_docx_document(
                    docx_with_table(),
                    b"<w:tc><w:p><w:r><w:t>\xe6\x8c\x87\xe6\xa0\x87</w:t>",
                    b'<w:tc><w:tcPr><w:hMerge w:val="'
                    + merge_value
                    + b'"/></w:tcPr><w:p><w:r><w:t>\xe6\x8c\x87\xe6\xa0\x87</w:t>',
                )

                _, tables = _docx_content(merged)

                self.assertEqual(tables, ())

    def test_observation_adapter_authenticates_every_source_field(self) -> None:
        observation = CurrentReportObservation(
            "current_stock_asset_allocation_percent",
            D("35.2"),
            "percent_of_total_assets",
            2,
            "资产组合报告",
            "报告期末股票资产占基金总资产的35.2%",
            FactConfidence.EXACT,
        )
        fact = parsed_fact_from_current_observation(
            observation,
            effective_from=date(2026, 6, 30),
            effective_to=date(2026, 6, 30),
        )

        self.assertEqual(fact.fact_kind, observation.fact_kind)
        self.assertEqual(fact.normalized_value, observation.normalized_value)
        self.assertEqual(fact.unit, observation.unit)
        self.assertEqual(fact.page_number, observation.page_number)
        self.assertEqual(fact.section_name, observation.section_name)
        self.assertEqual(fact.source_excerpt, observation.source_excerpt)
        self.assertEqual(fact.confidence_state, observation.confidence_state)
        changed_observations = (
            replace(observation, fact_kind="current_bond_asset_allocation_percent"),
            replace(observation, normalized_value=D("35.3")),
            replace(observation, unit="percent_of_net_assets"),
            replace(observation, page_number=3),
            replace(observation, section_name="投资组合报告"),
            replace(observation, source_excerpt="报告期末股票占基金总资产35.2%"),
            replace(observation, confidence_state=FactConfidence.PRESENT),
        )
        for changed_observation in changed_observations:
            with self.subTest(changed=changed_observation):
                changed = parsed_fact_from_current_observation(
                    changed_observation,
                    effective_from=date(2026, 6, 30),
                    effective_to=date(2026, 6, 30),
                )
                self.assertNotEqual(changed.fact_fingerprint, fact.fact_fingerprint)
        for effective_from, effective_to in (
            (date(2026, 6, 29), date(2026, 6, 30)),
            (date(2026, 6, 30), date(2026, 7, 1)),
        ):
            with self.subTest(effective_from=effective_from, effective_to=effective_to):
                changed = parsed_fact_from_current_observation(
                    observation,
                    effective_from=effective_from,
                    effective_to=effective_to,
                )
                self.assertNotEqual(changed.fact_fingerprint, fact.fact_fingerprint)


class CommonReportFactExtractionTest(unittest.TestCase):
    def test_common_fact_allowlist_accepts_only_explicit_supported_rows_and_sentences(self) -> None:
        allocation = common_table(
            ("指标", "单位", "数值"),
            (
                ("报告期末股票资产占基金总资产的", "%", "35.2"),
                ("报告期末债券资产占基金总资产的", "%", "60"),
                ("报告期末现金资产占基金总资产的", "%", "4.8"),
            ),
            section_name="报告期末资产组合",
        )
        hong_kong = common_table(
            ("指标", "单位", "数值"),
            (("报告期末港股资产占基金资产净值的", "%", "8"),),
            section_name="报告期末资产组合",
        )
        industries = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", "金融", "20")),
            section_name="报告期末全部行业分布",
        )

        observations = extract_common_report_observations(
            text_blocks=(
                "报告期末最大单一证券占基金资产净值8.5%。",
                "报告期末前十大持仓合计占基金资产净值40%。",
            ),
            tables=(allocation, hong_kong, industries),
        )
        values = {item.fact_kind: item.normalized_value for item in observations}

        self.assertEqual(
            set(values),
            COMMON_FACTS - {"holdings_evidence_complete"},
        )
        self.assertEqual(values["current_stock_asset_allocation_percent"], D("35.2"))
        self.assertEqual(values["current_hong_kong_asset_allocation_percent"], D("8"))
        self.assertEqual(values["current_largest_industry_name"], "科技")
        self.assertEqual(values["current_industry_count"], 2)
        self.assertTrue(all(item.confidence_state is FactConfidence.EXACT for item in observations))

    def test_common_security_tables_require_exact_ranks_and_explicit_scope(self) -> None:
        rows = tuple((str(rank), f"证券{rank}", str(11 - rank)) for rank in range(1, 11))
        top_ten = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            rows,
            section_name="报告期末前十大持仓",
        )
        complete = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            rows[:3],
            section_name="报告期末全部证券持仓明细",
        )

        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(top_ten, complete),
        )
        values = [(item.fact_kind, item.normalized_value) for item in observations]

        self.assertIn(("current_top_ten_holdings_weight_percent", D("55")), values)
        self.assertIn(("current_largest_security_weight_percent", D("10")), values)
        self.assertIn(("holdings_evidence_complete", True), values)

    def test_common_industry_table_requires_complete_parse_for_count(self) -> None:
        complete = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", "金融", "20"), ("3", "医药", "15")),
            section_name="报告期末全部行业分布",
        )

        observations = extract_common_report_observations(text_blocks=(), tables=(complete,))
        values = {item.fact_kind: item.normalized_value for item in observations}

        self.assertEqual(values["current_largest_industry_name"], "科技")
        self.assertEqual(values["current_largest_industry_weight_percent"], D("25"))
        self.assertEqual(values["current_industry_count"], 3)

    def test_common_rejects_missing_headers_mixed_denominators_and_ranges(self) -> None:
        missing_header = common_table(
            ("项目", "单位", "数值"),
            (("报告期末股票资产占基金总资产的", "%", "35"),),
            section_name="报告期末资产组合",
        )
        ranged = common_table(
            ("指标", "单位", "数值"),
            (
                ("报告期末股票资产占基金总资产的", "%", "约35"),
                ("报告期末债券资产占基金总资产的", "%", "30-35"),
            ),
            section_name="报告期末资产组合",
        )
        mixed = replace(
            report_table(),
            rows=report_table().rows
            + (
                ReportRow(
                    (
                        ReportCell("报告期末债券资产占基金资产净值的", False),
                        ReportCell("%", False),
                        ReportCell("60", False),
                    )
                ),
            ),
        )

        self.assertEqual(
            extract_common_report_observations(text_blocks=(), tables=(missing_header, ranged)),
            (),
        )
        with self.assertRaisesRegex(ValueError, "mixed denominators"):
            extract_common_report_observations(text_blocks=(), tables=(mixed,))
        self.assertEqual(
            extract_common_report_observations(
                text_blocks=(
                    "报告期末股票资产约占基金总资产35%。",
                    "报告期末债券资产占基金总资产30%-35%。",
                ),
                tables=(),
            ),
            (),
        )

    def test_common_rejects_partial_unknown_duplicate_and_incomplete_scopes(self) -> None:
        partial_industries = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", "金融", "20")),
            section_name="报告期末前两大行业",
        )
        unknown_other = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", "其他", "20")),
            section_name="报告期末全部行业分布",
        )
        duplicate_rank = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            tuple(("1" if rank == 2 else str(rank), f"证券{rank}", "5") for rank in range(1, 11)),
            section_name="报告期末前十大持仓",
        )
        fewer_than_ten = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            tuple((str(rank), f"证券{rank}", "5") for rank in range(1, 10)),
            section_name="报告期末前十大持仓",
        )
        incomplete_appendix = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            (("1", "证券1", "10"), ("2", "证券2", "5")),
            section_name="报告期末全部证券持仓明细（附录不完整）",
        )
        out_of_order = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            tuple(
                (str(rank), f"证券{rank}", "4" if rank == 1 else "5")
                for rank in range(1, 11)
            ),
            section_name="报告期末前十大持仓",
        )

        partial = extract_common_report_observations(text_blocks=(), tables=(partial_industries,))
        unknown = extract_common_report_observations(text_blocks=(), tables=(unknown_other,))
        duplicate = extract_common_report_observations(text_blocks=(), tables=(duplicate_rank,))
        fewer = extract_common_report_observations(text_blocks=(), tables=(fewer_than_ten,))
        incomplete = extract_common_report_observations(
            text_blocks=(),
            tables=(incomplete_appendix,),
        )
        unordered = extract_common_report_observations(text_blocks=(), tables=(out_of_order,))

        self.assertFalse(any(item.fact_kind == "current_industry_count" for item in partial))
        self.assertFalse(
            any(item.fact_kind.startswith("current_largest_industry") for item in partial)
        )
        self.assertFalse(any(item.fact_kind == "current_industry_count" for item in unknown))
        self.assertFalse(
            any(item.fact_kind.startswith("current_largest_industry") for item in unknown)
        )
        self.assertFalse(any(item.fact_kind.startswith("current_top_ten") for item in duplicate))
        self.assertFalse(any(item.fact_kind.startswith("current_top_ten") for item in fewer))
        self.assertFalse(any(item.fact_kind == "holdings_evidence_complete" for item in incomplete))
        self.assertFalse(
            any(item.fact_kind.startswith("current_largest_security") for item in incomplete)
        )
        self.assertFalse(any(item.fact_kind.startswith("current_top_ten") for item in unordered))

    def test_common_rejects_summary_only_and_unknown_largest_industry_evidence(self) -> None:
        generic = common_table(
            ("指标", "单位", "数值"),
            (("报告期末最大行业为科技,占基金资产净值", "%", "25"),),
            section_name="报告期末资产组合",
        )
        unknown = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "Other", "25"), ("2", "科技", "20")),
            section_name="报告期末全部行业分布",
        )

        observations = extract_common_report_observations(
            text_blocks=("报告期末最大行业为科技，占基金资产净值25%。",),
            tables=(generic, unknown),
        )

        self.assertFalse(
            any(item.fact_kind.startswith("current_largest_industry") for item in observations)
        )
        self.assertFalse(any(item.fact_kind == "current_industry_count" for item in observations))

    def test_common_ranked_tables_reject_normalized_duplicate_names(self) -> None:
        duplicate_security = common_table(
            ("排名", "证券名称", "占基金资产净值比例(%)"),
            (("1", "Security A", "10"), ("2", "security\u3000a", "5")),
            section_name="报告期末全部证券持仓明细",
        )
        duplicate_industry = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", " 科技 ", "20")),
            section_name="报告期末全部行业分布",
        )

        for table in (duplicate_security, duplicate_industry):
            with self.subTest(section=table.section_name):
                self.assertEqual(
                    extract_common_report_observations(text_blocks=(), tables=(table,)),
                    (),
                )

    def test_common_ranked_tables_reject_invisible_names_and_unknown_industry_prefixes(
        self,
    ) -> None:
        invisible_tables = (
            common_table(
                ("排名", "证券名称", "占基金资产净值比例(%)"),
                (("1", "证券\u200bA", "10"), ("2", "证券B", "5")),
                section_name="报告期末全部证券持仓明细",
            ),
            common_table(
                ("排名", "行业名称", "占基金资产净值比例(%)"),
                (("1", "科技\ufe0f", "25"), ("2", "金融", "20")),
                section_name="报告期末全部行业分布",
            ),
        )
        unknown_names = (
            "其他行业",
            "其他行业合计",
            "未分类",
            "Other industries",
            "Other-Unclassified",
            "Others-Unclassified",
            "Other（Unclassified）",
        )
        legitimate_names = ("其他制造业", "其他金融业", "Other Consumer Services")

        for table in invisible_tables:
            with self.subTest(name=table.rows[1].cells[1].text):
                self.assertEqual(
                    extract_common_report_observations(text_blocks=(), tables=(table,)),
                    (),
                )
        for legitimate_name in legitimate_names:
            table = common_table(
                ("排名", "行业名称", "占基金资产净值比例(%)"),
                (("1", legitimate_name, "25"), ("2", "科技", "20")),
                section_name="报告期末全部行业分布",
            )
            with self.subTest(name=legitimate_name):
                observations = extract_common_report_observations(
                    text_blocks=(),
                    tables=(table,),
                )
                self.assertEqual(
                    next(
                        item.normalized_value
                        for item in observations
                        if item.fact_kind == "current_largest_industry_name"
                    ),
                    legitimate_name,
                )
        for unknown_name in unknown_names:
            table = common_table(
                ("排名", "行业名称", "占基金资产净值比例(%)"),
                (("1", "科技", "25"), ("2", unknown_name, "20")),
                section_name="报告期末全部行业分布",
            )
            with self.subTest(name=unknown_name):
                self.assertEqual(
                    extract_common_report_observations(text_blocks=(), tables=(table,)),
                    (),
                )

    def test_common_rejects_mandate_wording_and_never_infers_remainders(self) -> None:
        observations = extract_common_report_observations(
            text_blocks=(
                "本基金股票资产占基金资产的比例为0%-95%。",
                "本基金投资港股资产的比例不超过基金资产净值的50%。",
                "基金合同约定前十大持仓集中度不得超过60%。",
            ),
            tables=(
                common_table(
                    ("指标", "单位", "数值"),
                    (
                        ("报告期末股票资产占基金总资产的", "%", "35"),
                        ("报告期末债券资产占基金总资产的", "%", "60"),
                    ),
                    section_name="报告期末资产组合",
                ),
                common_table(
                    ("排名", "行业名称", "占基金资产净值比例(%)"),
                    (("1", "科技", "25"), ("2", "金融", "20")),
                    section_name="基金投资策略行业排序",
                ),
            ),
        )

        self.assertEqual(
            {item.fact_kind for item in observations},
            {
                "current_stock_asset_allocation_percent",
                "current_bond_asset_allocation_percent",
            },
        )


if __name__ == "__main__":
    unittest.main()
