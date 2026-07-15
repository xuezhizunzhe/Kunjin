from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch

from kunjin.funds.industry_taxonomy import (
    SW_LEVEL1_2021,
    IndustryTaxonomyMapping,
)
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
    EMPTY_SEQUENCE_CELL_TEXT,
    FIXED_INCOME_FACTS,
    MAX_REPORT_CELL_CHARACTERS,
    CurrentReportObservation,
    ReportCell,
    ReportRow,
    ReportTable,
    extract_common_report_observations,
    extract_fixed_income_report_observations,
    is_real_asset_table_header,
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


def four_column_asset_table(
    rows: tuple[tuple[str, str, str, str], ...] = (
        (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "35.2"),
        (EMPTY_SEQUENCE_CELL_TEXT, "其中:债券", "987.00", "60"),
        ("7", "银行存款和结算备付金合计", "100.00", "4.8"),
    ),
    *,
    headers: tuple[str, ...] = (
        "序号",
        "项目",
        "金额(元)",
        "占基金总资产的比例(%)",
    ),
    page_number: int = 3,
) -> ReportTable:
    return ReportTable(
        rows=(
            ReportRow(tuple(ReportCell(value, True) for value in headers)),
            *(
                ReportRow(
                    tuple(
                        ReportCell(
                            value,
                            False,
                            value == EMPTY_SEQUENCE_CELL_TEXT,
                        )
                        for value in row
                    )
                )
                for row in rows
            ),
        ),
        page_number=page_number,
        section_name="报告期末资产组合",
        source_excerpt="；".join(" | ".join(row) for row in (headers, *rows)),
    )


def synthetic_taxonomy_mapping() -> IndustryTaxonomyMapping:
    entries = (
        ("801080", "电子", ()),
        ("801780", "银行", ("银行业",)),
    )
    payload = {
        "entries": [
            {"aliases": list(aliases), "code": code, "name": name}
            for code, name, aliases in entries
        ],
        "published_at": "2021-12-01",
        "source_url": "https://example.com/sw-level1-2021.json",
        "taxonomy_id": SW_LEVEL1_2021.taxonomy_id,
        "version": SW_LEVEL1_2021.version,
    }
    canonical_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return IndustryTaxonomyMapping(
        metadata=SW_LEVEL1_2021,
        source_url="https://example.com/sw-level1-2021.json",
        published_at=date(2021, 12, 1),
        entries=entries,
        canonical_json=canonical_json,
        checksum=hashlib.sha256(canonical_json.encode("ascii")).hexdigest(),
    )


def controlled_industry_table(
    rows: tuple[tuple[str, str, str, str, str], ...] = (
        ("申万一级行业分类（2021）", "801780", "1", "银行", "12.50"),
        ("申万一级行业分类（2021）", "801080", "2", "电子", "8.35"),
    ),
    *,
    section_name: str = "报告期末全部行业分布",
    headers: tuple[str, str, str, str, str] = (
        "分类标准",
        "行业代码",
        "排名",
        "行业名称",
        "占基金资产净值比例(%)",
    ),
) -> ReportTable:
    return common_table(headers, rows, section_name=section_name)


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

    def test_report_cell_structural_placeholder_requires_authenticated_state(self) -> None:
        ReportCell(EMPTY_SEQUENCE_CELL_TEXT, False, True).validate()
        invalid_cells = (
            ReportCell(EMPTY_SEQUENCE_CELL_TEXT, False),
            ReportCell(EMPTY_SEQUENCE_CELL_TEXT, True, True),
            ReportCell("1", False, True),
            ReportCell(EMPTY_SEQUENCE_CELL_TEXT, False, 1),  # type: ignore[arg-type]
        )

        for cell in invalid_cells:
            with self.subTest(cell=repr(cell)), self.assertRaises(ValueError):
                cell.validate()

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
    def test_real_shape_four_column_asset_table_extracts_only_stock_and_bond(self) -> None:
        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(four_column_asset_table(),),
        )
        values = {item.fact_kind: item.normalized_value for item in observations}

        self.assertTrue(
            is_real_asset_table_header(("序号", "项目", "金额（元）", "占基金总资产的比例（%）"))
        )
        self.assertEqual(
            values,
            {
                "current_stock_asset_allocation_percent": D("35.2"),
                "current_bond_asset_allocation_percent": D("60"),
            },
        )
        self.assertTrue(all(item.unit == "percent_of_total_assets" for item in observations))
        self.assertTrue(
            all(EMPTY_SEQUENCE_CELL_TEXT not in item.source_excerpt for item in observations)
        )
        self.assertEqual(
            {item.source_excerpt for item in observations},
            {
                "其中:股票 | 1,234.56 | 35.2",
                "其中:债券 | 987.00 | 60",
            },
        )
        self.assertNotIn("current_cash_asset_allocation_percent", values)

    def test_real_shape_four_column_asset_table_requires_exact_header_and_width(
        self,
    ) -> None:
        wrong_header = four_column_asset_table(
            headers=("序号", "项目", "金额(元)", "占基金资产净值的比例(%)")
        )
        wrong_width = four_column_asset_table(
            rows=((EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "35.2", "额外"),),
            headers=("序号", "项目", "金额(元)", "占基金总资产的比例(%)", "额外"),
        )

        self.assertFalse(
            is_real_asset_table_header(("序号", "项目", "金额(元)", "占基金资产净值的比例(%)"))
        )
        self.assertEqual(
            extract_common_report_observations(text_blocks=(), tables=(wrong_header, wrong_width)),
            (),
        )

    def test_four_column_asset_rows_reject_empty_keys_invalid_amounts_and_percents(
        self,
    ) -> None:
        rejected_rows = (
            ("1", EMPTY_SEQUENCE_CELL_TEXT, "1,234.56", "35.2"),
            ("01", "其中:股票", "1,234.56", "35.2"),
            ("one", "其中:股票", "1,234.56", "35.2"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", EMPTY_SEQUENCE_CELL_TEXT, "35.2"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "NaN", "35.2"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "-1", "35.2"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,23.45", "35.2"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "100.01"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "-0.01"),
            (EMPTY_SEQUENCE_CELL_TEXT, "其中:基金", "1,234.56", "35.2"),
        )

        for row in rejected_rows:
            with self.subTest(row=row):
                self.assertEqual(
                    extract_common_report_observations(
                        text_blocks=(), tables=(four_column_asset_table(rows=(row,)),)
                    ),
                    (),
                )

    def test_four_column_asset_rows_reject_header_cells_and_flagged_keys(self) -> None:
        all_header_row = replace(
            four_column_asset_table(
                rows=((EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "35.2"),)
            ),
            rows=(
                four_column_asset_table().rows[0],
                ReportRow(
                    (
                        ReportCell(EMPTY_SEQUENCE_CELL_TEXT, False, True),
                        ReportCell("其中:股票", True),
                        ReportCell("1,234.56", True),
                        ReportCell("35.2", True),
                    )
                ),
            ),
        )
        flagged_key = four_column_asset_table(
            rows=(("1", EMPTY_SEQUENCE_CELL_TEXT, "1,234.56", "35.2"),)
        )

        self.assertEqual(
            extract_common_report_observations(
                text_blocks=(), tables=(all_header_row, flagged_key)
            ),
            (),
        )

    def test_four_column_asset_sequence_accepts_nfkc_canonical_ascii_decimal(self) -> None:
        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(
                four_column_asset_table(
                    rows=(("７", "其中:股票", "1,234.56", "35.2"),)
                ),
            ),
        )

        self.assertEqual(
            tuple(item.normalized_value for item in observations),
            (D("35.2"),),
        )

    def test_four_column_asset_equivalent_duplicates_dedupe_but_conflicts_remain(
        self,
    ) -> None:
        table = four_column_asset_table(
            rows=((EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,234.56", "35.2"),)
        )
        conflicting = four_column_asset_table(
            rows=((EMPTY_SEQUENCE_CELL_TEXT, "其中:股票", "1,250.00", "36.2"),),
        )

        equivalent = extract_common_report_observations(text_blocks=(), tables=(table, table))
        conflicts = extract_common_report_observations(text_blocks=(), tables=(table, conflicting))

        self.assertEqual(len(equivalent), 1)
        self.assertEqual(
            tuple(item.normalized_value for item in conflicts),
            (D("35.2"), D("36.2")),
        )

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
            COMMON_FACTS
            - {
                "current_largest_industry_name",
                "current_largest_industry_weight_percent",
                "current_industry_count",
                "holdings_evidence_complete",
            },
        )
        self.assertEqual(values["current_stock_asset_allocation_percent"], D("35.2"))
        self.assertEqual(values["current_hong_kong_asset_allocation_percent"], D("8"))
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

    def test_three_column_free_text_industry_table_emits_no_industry_facts(self) -> None:
        complete = common_table(
            ("排名", "行业名称", "占基金资产净值比例(%)"),
            (("1", "科技", "25"), ("2", "金融", "20"), ("3", "医药", "15")),
            section_name="报告期末全部行业分布",
        )

        observations = extract_common_report_observations(text_blocks=(), tables=(complete,))
        self.assertFalse(
            any("industry" in observation.fact_kind for observation in observations)
        )

    def test_five_column_industry_table_is_closed_with_empty_production_mapping(
        self,
    ) -> None:
        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(controlled_industry_table(),),
        )

        self.assertFalse(
            any("industry" in observation.fact_kind for observation in observations)
        )

    def test_five_column_industry_table_emits_all_facts_with_complete_mapping(
        self,
    ) -> None:
        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(controlled_industry_table(),),
            taxonomy_mappings=(synthetic_taxonomy_mapping(),),
        )

        self.assertEqual(
            {
                observation.fact_kind: observation.normalized_value
                for observation in observations
            },
            {
                "current_largest_industry_name": "银行",
                "current_largest_industry_weight_percent": D("12.50"),
                "current_industry_count": 2,
            },
        )
        weight = next(
            observation
            for observation in observations
            if observation.fact_kind == "current_largest_industry_weight_percent"
        )
        self.assertEqual(weight.unit, "percent_of_net_assets")

    def test_controlled_industry_table_requires_exact_header_aliases(self) -> None:
        accepted_english = controlled_industry_table(
            headers=(
                "classification standard",
                "industry code",
                "rank",
                "industry name",
                "weight (% of net assets)",
            )
        )
        rejected_inferred = controlled_industry_table(
            headers=(
                "分类体系",
                "代码",
                "序号",
                "行业",
                "净值占比(%)",
            )
        )

        accepted = extract_common_report_observations(
            text_blocks=(),
            tables=(accepted_english,),
            taxonomy_mappings=(synthetic_taxonomy_mapping(),),
        )
        rejected = extract_common_report_observations(
            text_blocks=(),
            tables=(rejected_inferred,),
            taxonomy_mappings=(synthetic_taxonomy_mapping(),),
        )

        self.assertEqual(len(accepted), 3)
        self.assertEqual(rejected, ())

    def test_controlled_industry_distribution_fails_closed_on_invalid_rows(self) -> None:
        mapping = synthetic_taxonomy_mapping()
        valid_rows = controlled_industry_table().rows[1:]
        raw_rows = tuple(
            tuple(cell.text for cell in row.cells) for row in valid_rows
        )
        invalid_cases = {
            "mixed standard": (
                raw_rows[0],
                ("中证行业分类", *raw_rows[1][1:]),
            ),
            "missing code": (
                (raw_rows[0][0], "N/A", *raw_rows[0][2:]),
                raw_rows[1],
            ),
            "unmapped code": (
                (raw_rows[0][0], "801999", *raw_rows[0][2:]),
                raw_rows[1],
            ),
            "name mismatch": (
                (*raw_rows[0][:3], "电子", raw_rows[0][4]),
                raw_rows[1],
            ),
            "duplicate code": (
                raw_rows[0],
                (raw_rows[1][0], raw_rows[0][1], *raw_rows[1][2:]),
            ),
            "duplicate name": (
                raw_rows[0],
                (*raw_rows[1][:3], raw_rows[0][3], raw_rows[1][4]),
            ),
            "duplicate rank": (
                raw_rows[0],
                (*raw_rows[1][:2], "1", *raw_rows[1][3:]),
            ),
            "unsafe unicode": (
                (*raw_rows[0][:3], "银\u200b行", raw_rows[0][4]),
                raw_rows[1],
            ),
            "unsafe surrogate": (
                (*raw_rows[0][:3], "银\ud800行", raw_rows[0][4]),
                raw_rows[1],
            ),
            "tied max": (
                raw_rows[0],
                (*raw_rows[1][:4], raw_rows[0][4]),
            ),
        }

        for label, rows in invalid_cases.items():
            with self.subTest(label=label):
                observations = extract_common_report_observations(
                    text_blocks=(),
                    tables=(controlled_industry_table(rows=rows),),
                    taxonomy_mappings=(mapping,),
                )
                self.assertEqual(observations, ())

    def test_controlled_industry_distribution_requires_complete_scope(self) -> None:
        observations = extract_common_report_observations(
            text_blocks=(),
            tables=(
                controlled_industry_table(section_name="报告期末前两大行业"),
            ),
            taxonomy_mappings=(synthetic_taxonomy_mapping(),),
        )

        self.assertEqual(observations, ())

    def test_taxonomy_mapping_dependency_requires_an_exact_immutable_tuple(self) -> None:
        with self.assertRaisesRegex(ValueError, "immutable tuples"):
            extract_common_report_observations(
                text_blocks=(),
                tables=(controlled_industry_table(),),
                taxonomy_mappings=[synthetic_taxonomy_mapping()],  # type: ignore[arg-type]
            )

    def test_controlled_industry_distribution_rejects_mixed_denominators(self) -> None:
        mixed = controlled_industry_table(
            rows=(
                (
                    "申万一级行业分类（2021）",
                    "801780",
                    "1",
                    "银行（占基金总资产）",
                    "12.50",
                ),
                (
                    "申万一级行业分类（2021）",
                    "801080",
                    "2",
                    "电子（占基金资产净值）",
                    "8.35",
                ),
            )
        )

        with self.assertRaisesRegex(ValueError, "mixed denominators"):
            extract_common_report_observations(
                text_blocks=(),
                tables=(mixed,),
                taxonomy_mappings=(synthetic_taxonomy_mapping(),),
            )

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
            "其他行业（未分类）",
            "其他类别合计",
            "其他及未分类",
            "其他合计",
            "其他行业总计",
            "未分类",
            "未分类行业",
            "Other industries",
            "Other Industries (Unclassified)",
            "Other-Unclassified",
            "Others-Unclassified",
            "Other（Unclassified）",
            "Other and Unclassified",
            "Other/Unclassified Total",
            "Others/Unclassified",
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
                self.assertEqual(
                    extract_common_report_observations(
                        text_blocks=(),
                        tables=(table,),
                    ),
                    (),
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


class FixedIncomeReportFactExtractionTest(unittest.TestCase):
    def complete_credit_table(
        self,
        *,
        rows: tuple[tuple[str, str, str], ...] = (
            ("AAA", "其他非主权", "50"),
            ("AA+", "其他非主权", "30"),
            ("AA", "其他非主权", "10"),
            ("未评级", "主权", "5"),
            ("未评级", "政策性银行", "3"),
            ("未评级", "其他非主权", "2"),
        ),
        section_name: str = "报告期末全部固定收益资产信用评级分布",
        weight_header: str = "占固定收益资产比例(%)",
    ) -> ReportTable:
        return common_table(
            ("信用评级", "发行人类别", weight_header),
            rows,
            section_name=section_name,
        )

    def complete_issuer_table(
        self,
        *,
        rows: tuple[tuple[str, str, str], ...] = (
            ("主权", "中华人民共和国财政部", "20"),
            ("政策性银行", "国家开发银行", "15"),
            ("其他非主权", "发行人甲", "8"),
            ("其他非主权", "发行人乙", "5"),
        ),
        section_name: str = "报告期末全部固定收益证券发行人分布",
    ) -> ReportTable:
        return common_table(
            ("发行人类别", "发行人名称", "占基金资产净值比例(%)"),
            rows,
            section_name=section_name,
        )

    def test_fixed_income_fact_allowlist_extracts_nine_explicit_current_facts(
        self,
    ) -> None:
        metrics = common_table(
            ("指标", "单位", "数值"),
            (
                ("报告期末组合有效久期", "年", "3.2"),
                ("报告期末加权平均剩余期限", "天", "180"),
                ("报告期末可转换债券资产占基金总资产的", "%", "1.5"),
                ("报告期末可交换债券资产占基金总资产的", "%", "0.5"),
            ),
            section_name="报告期末固定收益指标",
        )
        leverage = common_table(
            ("指标", "分母", "单位", "数值"),
            (("报告期末总资产杠杆率", "基金资产净值", "%", "115.2"),),
            section_name="报告期末杠杆指标",
        )

        observations = extract_fixed_income_report_observations(
            text_blocks=(),
            tables=(
                metrics,
                leverage,
                self.complete_credit_table(),
                self.complete_issuer_table(),
            ),
        )
        values = {item.fact_kind: item.normalized_value for item in observations}

        self.assertEqual(set(values), FIXED_INCOME_FACTS)
        self.assertEqual(values["current_effective_duration"], D("3.2"))
        self.assertEqual(values["current_weighted_average_maturity_days"], D("180"))
        self.assertEqual(
            values["current_convertible_bond_asset_allocation_percent"], D("1.5")
        )
        self.assertEqual(
            values["current_exchangeable_bond_asset_allocation_percent"], D("0.5")
        )
        self.assertEqual(values["current_high_quality_fixed_income_percent"], D("80"))
        self.assertEqual(values["current_below_aa_plus_exposure_percent"], D("10"))
        self.assertEqual(
            values["current_unrated_non_sovereign_exposure_percent"], D("2")
        )
        self.assertEqual(values["current_gross_leverage_percent"], D("115.2"))
        self.assertEqual(
            values["current_largest_non_sovereign_issuer_percent"], D("8")
        )

    def test_fixed_income_duration_and_maturity_are_never_substituted(self) -> None:
        duration = common_table(
            ("指标", "单位", "数值"),
            (("报告期末组合有效久期", "年", "3.2"),),
            section_name="报告期末固定收益指标",
        )
        maturity = common_table(
            ("指标", "单位", "数值"),
            (("报告期末加权平均剩余期限", "天", "180"),),
            section_name="报告期末固定收益指标",
        )

        duration_kinds = {
            item.fact_kind
            for item in extract_fixed_income_report_observations(
                text_blocks=(), tables=(duration,)
            )
        }
        maturity_kinds = {
            item.fact_kind
            for item in extract_fixed_income_report_observations(
                text_blocks=(), tables=(maturity,)
            )
        }

        self.assertEqual(duration_kinds, {"current_effective_duration"})
        self.assertEqual(maturity_kinds, {"current_weighted_average_maturity_days"})

    def test_fixed_income_convertible_and_exchangeable_rows_are_separate(self) -> None:
        convertible = common_table(
            ("指标", "单位", "数值"),
            (("报告期末可转换债券资产占基金资产净值的", "%", "1.5"),),
            section_name="报告期末固定收益指标",
        )
        exchangeable = common_table(
            ("指标", "单位", "数值"),
            (("报告期末可交换债券资产占基金资产净值的", "%", "0.5"),),
            section_name="报告期末固定收益指标",
        )

        first = extract_fixed_income_report_observations(
            text_blocks=(), tables=(convertible,)
        )
        second = extract_fixed_income_report_observations(
            text_blocks=(), tables=(exchangeable,)
        )

        self.assertEqual(
            {item.fact_kind for item in first},
            {"current_convertible_bond_asset_allocation_percent"},
        )
        self.assertEqual(
            {item.fact_kind for item in second},
            {"current_exchangeable_bond_asset_allocation_percent"},
        )

    def test_fixed_income_credit_requires_complete_scope_and_all_explicit_buckets(
        self,
    ) -> None:
        incomplete = self.complete_credit_table(section_name="信用评级前五名")
        missing_below = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "90"),
                ("未评级", "主权", "5"),
                ("未评级", "其他非主权", "5"),
            )
        )
        missing_unrated_non_sovereign = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "90"),
                ("AA", "其他非主权", "5"),
                ("未评级", "主权", "5"),
            )
        )
        absent_rating = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "90"),
                ("-", "其他非主权", "5"),
                ("未评级", "其他非主权", "5"),
            )
        )

        for table in (
            incomplete,
            missing_below,
            missing_unrated_non_sovereign,
            absent_rating,
        ):
            with self.subTest(section=table.section_name, excerpt=table.source_excerpt):
                observations = extract_fixed_income_report_observations(
                    text_blocks=(), tables=(table,)
                )
                self.assertFalse(
                    any(
                        item.fact_kind
                        in {
                            "current_high_quality_fixed_income_percent",
                            "current_below_aa_plus_exposure_percent",
                            "current_unrated_non_sovereign_exposure_percent",
                        }
                        for item in observations
                    )
                )

    def test_fixed_income_credit_excludes_only_explicit_sovereign_and_policy_bank(
        self,
    ) -> None:
        observations = extract_fixed_income_report_observations(
            text_blocks=(), tables=(self.complete_credit_table(),)
        )
        values = {item.fact_kind: item.normalized_value for item in observations}

        self.assertEqual(values["current_high_quality_fixed_income_percent"], D("80"))
        self.assertEqual(
            values["current_unrated_non_sovereign_exposure_percent"], D("2")
        )

    def test_fixed_income_issuer_requires_complete_unique_rows(self) -> None:
        duplicate = self.complete_issuer_table(
            rows=(
                ("主权", "中华人民共和国财政部", "20"),
                ("其他非主权", "发行人甲", "8"),
                ("其他非主权", "发行人甲", "5"),
            )
        )
        incomplete = self.complete_issuer_table(section_name="前五大发行人")
        unknown_category = self.complete_issuer_table(
            rows=(("未知", "发行人甲", "8"),)
        )
        empty_after_exclusions = self.complete_issuer_table(
            rows=(
                ("主权", "中华人民共和国财政部", "60"),
                ("政策性银行", "国家开发银行", "40"),
            )
        )

        for table in (duplicate, incomplete, unknown_category, empty_after_exclusions):
            with self.subTest(section=table.section_name, excerpt=table.source_excerpt):
                self.assertEqual(
                    extract_fixed_income_report_observations(
                        text_blocks=(), tables=(table,)
                    ),
                    (),
                )

    def test_fixed_income_leverage_requires_exact_net_asset_denominator(self) -> None:
        valid = common_table(
            ("指标", "分母", "单位", "数值"),
            (("报告期末总资产杠杆率", "基金资产净值", "%", "115.2"),),
            section_name="报告期末杠杆指标",
        )
        invalid = common_table(
            ("指标", "分母", "单位", "数值"),
            (("报告期末总资产杠杆率", "基金总资产", "%", "115.2"),),
            section_name="报告期末杠杆指标",
        )

        valid_observations = extract_fixed_income_report_observations(
            text_blocks=(), tables=(valid,)
        )
        invalid_observations = extract_fixed_income_report_observations(
            text_blocks=(), tables=(invalid,)
        )

        self.assertEqual(
            [(item.fact_kind, item.normalized_value, item.unit) for item in valid_observations],
            [("current_gross_leverage_percent", D("115.2"), "percent_of_net_assets")],
        )
        self.assertEqual(invalid_observations, ())

    def test_fixed_income_credit_rejects_unknown_other_duplicate_and_incomplete_total(
        self,
    ) -> None:
        unknown_rating = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "80"),
                ("未知评级", "其他非主权", "10"),
                ("未评级", "其他非主权", "10"),
            )
        )
        unexplained_other = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "80"),
                ("其他", "其他非主权", "10"),
                ("未评级", "其他非主权", "10"),
            )
        )
        duplicate_category = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "50"),
                ("AAA", "其他非主权", "30"),
                ("AA", "其他非主权", "10"),
                ("未评级", "其他非主权", "10"),
            )
        )
        incomplete_total = self.complete_credit_table(
            rows=(
                ("AAA", "其他非主权", "70"),
                ("AA", "其他非主权", "10"),
                ("未评级", "其他非主权", "10"),
            )
        )

        for table in (
            unknown_rating,
            unexplained_other,
            duplicate_category,
            incomplete_total,
        ):
            with self.subTest(excerpt=table.source_excerpt):
                self.assertEqual(
                    extract_fixed_income_report_observations(
                        text_blocks=(), tables=(table,)
                    ),
                    (),
                )

    def test_fixed_income_credit_rejects_duplicate_canonical_rating_aliases(self) -> None:
        duplicate_aliases = (
            self.complete_credit_table(
                rows=(
                    ("AAA", "其他非主权", "40"),
                    ("AAA级", "其他非主权", "40"),
                    ("AA", "其他非主权", "10"),
                    ("未评级", "其他非主权", "10"),
                )
            ),
            self.complete_credit_table(
                rows=(
                    ("AAA", "其他非主权", "40"),
                    ("AA", "其他非主权", "5"),
                    ("AA级", "其他非主权", "5"),
                    ("未评级", "其他非主权", "50"),
                )
            ),
        )

        for table in duplicate_aliases:
            with self.subTest(excerpt=table.source_excerpt):
                self.assertEqual(
                    extract_fixed_income_report_observations(
                        text_blocks=(), tables=(table,)
                    ),
                    (),
                )

    def test_fixed_income_issuer_rejects_normalized_chinese_name_duplicates(
        self,
    ) -> None:
        duplicate = self.complete_issuer_table(
            rows=(
                ("主权", "中华人民共和国财政部", "87"),
                ("其他非主权", "发行人甲", "8"),
                ("其他非主权", "发行 人甲", "5"),
            )
        )

        self.assertEqual(
            extract_fixed_income_report_observations(
                text_blocks=(), tables=(duplicate,)
            ),
            (),
        )

    def test_fixed_income_issuer_rejects_total_weight_above_one_hundred(self) -> None:
        tables = (
            self.complete_issuer_table(
                rows=(
                    ("主权", "中华人民共和国财政部", "60"),
                    ("其他非主权", "发行人甲", "50"),
                )
            ),
            common_table(
                ("发行人类别", "发行人名称", "占基金资产比例(%)"),
                (
                    ("政策性银行", "国家开发银行", "70"),
                    ("其他非主权", "发行人甲", "40"),
                ),
                section_name="报告期末全部固定收益证券发行人分布",
            ),
        )

        for table in tables:
            with self.subTest(header=table.rows[0].cells[2].text):
                self.assertEqual(
                    extract_fixed_income_report_observations(
                        text_blocks=(), tables=(table,)
                    ),
                    (),
                )

    def test_fixed_income_combined_convertible_exchangeable_row_is_not_split(self) -> None:
        combined = common_table(
            ("指标", "单位", "数值"),
            (("报告期末可转换债券(含可交换债券)资产占基金资产净值的", "%", "2"),),
            section_name="报告期末固定收益指标",
        )

        self.assertEqual(
            extract_fixed_income_report_observations(
                text_blocks=(), tables=(combined,)
            ),
            (),
        )

    def test_fixed_income_explicit_text_rejects_mandates_and_does_not_fill_missing_rows(
        self,
    ) -> None:
        observations = extract_fixed_income_report_observations(
            text_blocks=(
                "报告期末组合有效久期为3.2年。",
                "报告期末加权平均剩余期限为180天。",
                "报告期末基金总资产占基金资产净值的比例为115.2%。",
                "组合有效久期不得超过5年。",
                "基金总资产占基金资产净值的比例不得超过140%。",
            ),
            tables=(),
        )

        self.assertEqual(
            {item.fact_kind for item in observations},
            {
                "current_effective_duration",
                "current_weighted_average_maturity_days",
                "current_gross_leverage_percent",
            },
        )


if __name__ == "__main__":
    unittest.main()
