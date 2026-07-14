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
    MAX_REPORT_CELL_CHARACTERS,
    CurrentReportObservation,
    ReportCell,
    ReportRow,
    ReportTable,
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


def docx_with_table() -> bytes:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        b'  <Default Extension="xml" ContentType="application/xml"/>\n'
        b'  <Override PartName="/word/document.xml" ContentType="'
        b'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        b"</Types>"
    )
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>资产组合报告</w:t></w:r></w:p>
    <w:tbl>
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
    </w:tbl>
  </w:body>
</w:document>""".encode()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
    return payload.getvalue()


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


if __name__ == "__main__":
    unittest.main()
