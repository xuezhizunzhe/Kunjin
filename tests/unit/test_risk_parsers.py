from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

from kunjin.funds.models import DocumentKind
from kunjin.funds.risk.audit import legacy_parser_provenance, native_parser_provenance
from kunjin.funds.risk.documents import OfficialDocumentCandidate, RetrievedArtifact
from kunjin.funds.risk.failures import (
    DocumentFailureReason,
    DocumentFailureStage,
)
from kunjin.funds.risk.legacy_doc import LegacyConversionResult, LegacyDocConversionError
from kunjin.funds.risk.parsers import (
    ParsedArtifactResult,
    ParsedMandateFact,
    RiskDocumentParseError,
    _html_content,
    parse_artifact,
    parse_artifact_with_provenance,
)
from kunjin.funds.risk.reports import document_kind_markers, report_period_end

FIXTURES = Path(__file__).parents[1] / "fixtures" / "funds" / "risk"
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
LEGACY_PROVENANCE = legacy_parser_provenance(
    image_id="sha256:" + "a" * 64,
    architecture="linux/arm64",
    libreoffice_version="24.2.7.2",
    package_manifest_checksum="b" * 64,
)


def artifact_for(
    path: Path,
    *,
    content_type: str,
    kind: DocumentKind = DocumentKind.PROSPECTUS,
) -> RetrievedArtifact:
    raw = path.read_bytes()
    periodic_titles = {
        DocumentKind.ANNUAL_REPORT: "Public synthetic fund 2025年年度报告",
        DocumentKind.SEMIANNUAL_REPORT: "Public synthetic fund 2026年半年度报告",
        DocumentKind.QUARTERLY_REPORT: "Public synthetic fund 2026年第2季度报告",
    }
    return RetrievedArtifact(
        candidate=OfficialDocumentCandidate(
            fund_code="519755",
            document_kind=kind,
            title=periodic_titles.get(kind, "Public synthetic official document"),
            url="https://www.efunds.com.cn/synthetic/document",
            publisher="Public synthetic publisher",
            published_at=NOW,
            source_tier=1,
        ),
        final_url="https://www.efunds.com.cn/synthetic/document",
        retrieved_at=NOW,
        content_type=content_type,
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        managed_path=path,
    )


def one(result: object, fact_kind: str) -> object:
    matches = [fact for fact in result.facts if fact.fact_kind == fact_kind]
    if len(matches) != 1:
        raise AssertionError(f"expected one {fact_kind}, got {len(matches)}")
    return matches[0]


class FakeLegacyConverter:
    def __init__(self, html: str) -> None:
        self.html = html
        self.converted = []

    def convert(self, artifact: RetrievedArtifact) -> LegacyConversionResult:
        self.converted.append(artifact)
        checksum = hashlib.sha256(self.html.encode("utf-8")).hexdigest()
        return LegacyConversionResult(
            normalized_html=self.html,
            parser_input_sha256=checksum,
            provenance=LEGACY_PROVENANCE,
        )


def legacy_artifact(
    path: Path,
    *,
    title: str = "交银示例混合型证券投资基金2026年第2季度报告",
    kind: DocumentKind = DocumentKind.QUARTERLY_REPORT,
) -> RetrievedArtifact:
    artifact = artifact_for(path, content_type="application/msword", kind=kind)
    return replace(
        artifact,
        candidate=replace(artifact.candidate, title=title),
    )


def write_docx(
    path: Path,
    paragraphs: tuple[str, ...],
    *,
    table_rows: tuple[tuple[str, ...], ...] = (),
    extra_entries: tuple[tuple[str, bytes], ...] = (),
) -> None:
    body = "".join("<w:p><w:r><w:t>" + paragraph + "</w:t></w:r></w:p>" for paragraph in paragraphs)
    body += "".join(
        "<w:tbl><w:tr>"
        + "".join("<w:tc><w:p><w:r><w:t>" + cell + "</w:t></w:r></w:p></w:tc>" for cell in row)
        + "</w:tr></w:tbl>"
        for row in table_rows
    )
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        b'  <Default Extension="rels" ContentType="'
        b'application/vnd.openxmlformats-package.relationships+xml"/>\n'
        b'  <Default Extension="xml" ContentType="application/xml"/>\n'
        b'  <Override PartName="/word/document.xml" ContentType="'
        b'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        b"</Types>"
    )
    document = (
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>"""
        + body
        + "</w:body></w:document>"
    ).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", document)
        for name, value in extra_entries:
            archive.writestr(name, value)


class RiskHtmlParserTest(unittest.TestCase):
    def test_structured_table_channel_preserves_existing_legal_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(
                """<html><body>
<h2>投资范围</h2><p>本基金不投资于股票。</p>
<h2>资产组合报告</h2>
<table><tr><th>指标</th><th>单位</th><th>数值</th></tr>
<tr><td>报告期末债券资产占基金总资产的</td><td>%</td><td>95</td></tr></table>
</body></html>""",
                encoding="utf-8",
            )
            artifact = artifact_for(path, content_type="text/html")
            blocks, tables = _html_content(path.read_bytes(), "utf-8")

            self.assertTrue(blocks)
            self.assertEqual(len(tables), 1)
            self.assertEqual(
                one(parse_artifact(artifact), "stock_exposure_max_percent").normalized_value,
                D("0"),
            )

    def test_common_current_sentence_binds_only_to_candidate_report_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(
                "<p>报告期末股票资产占基金总资产35.2%。</p>",
                encoding="utf-8",
            )
            artifact = artifact_for(
                path,
                content_type="text/html",
                kind=DocumentKind.QUARTERLY_REPORT,
            )
            artifact = replace(
                artifact,
                candidate=replace(
                    artifact.candidate,
                    title="公开合成基金2026年第2季度报告",
                ),
            )

            fact = one(parse_artifact(artifact), "current_stock_asset_allocation_percent")

        self.assertEqual(fact.effective_from, date(2026, 6, 30))
        self.assertEqual(fact.effective_to, date(2026, 6, 30))

    def test_common_current_fact_rejects_missing_candidate_report_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(
                "<p>报告期末股票资产占基金总资产35.2%。</p>",
                encoding="utf-8",
            )
            artifact = artifact_for(
                path,
                content_type="text/html",
                kind=DocumentKind.QUARTERLY_REPORT,
            )
            artifact = replace(
                artifact,
                candidate=replace(
                    artifact.candidate,
                    title="Public synthetic official document",
                ),
            )

            with self.assertRaises(RiskDocumentParseError) as caught:
                parse_artifact(artifact)

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
        )

    def test_common_mixed_denominator_table_does_not_promote_or_block_a_valid_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.html"
            path.write_text(
                """<h2>报告期末资产组合</h2>
<table><tr><th>指标</th><th>单位</th><th>数值</th></tr>
<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35</td></tr>
<tr><td>报告期末债券资产占基金资产净值的</td><td>%</td><td>60</td></tr></table>
<table><tr><th>指标</th><th>单位</th><th>数值</th></tr>
<tr><td>报告期末现金资产占基金总资产的</td><td>%</td><td>5</td></tr></table>""",
                encoding="utf-8",
            )
            artifact = artifact_for(
                path,
                content_type="text/html",
                kind=DocumentKind.QUARTERLY_REPORT,
            )
            parsed = parse_artifact(artifact)

        self.assertFalse(any(fact.fact_kind.startswith("current_stock") for fact in parsed.facts))
        self.assertFalse(any(fact.fact_kind.startswith("current_bond") for fact in parsed.facts))
        self.assertEqual(
            one(parsed, "current_cash_asset_allocation_percent").normalized_value,
            D("5"),
        )

    def test_parser_errors_keep_public_codes_and_allowlisted_reasons(self) -> None:
        unsupported = replace(
            artifact_for(
                FIXTURES / "pure-bond-prospectus.html",
                content_type="text/html",
            ),
            content_type="image/png",
        )

        with self.assertRaises(RiskDocumentParseError) as caught:
            parse_artifact(unsupported)

        self.assertEqual(caught.exception.code, "official_document_parse_failed")
        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.PARSER)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_FORMAT_INVALID,
        )

    def test_effective_date_ambiguity_and_resource_reasons_are_source_assigned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conflicting_dates = root / "private-effective-date-sentinel.html"
            conflicting_dates.write_text(
                "<p>生效日期：2026年1月1日</p><p>生效日期：2026年2月1日</p>",
                encoding="utf-8",
            )
            invalid_range = root / "private-ambiguous-field-sentinel.html"
            invalid_range.write_text(
                "<p>股票资产占基金资产的比例为95%-80%</p>",
                encoding="utf-8",
            )
            resource = root / "private-resource-sentinel.html"
            resource.write_text(
                "<p>本基金不投资于股票。</p>",
                encoding="utf-8",
            )

            cases = (
                (
                    artifact_for(conflicting_dates, content_type="text/html"),
                    DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
                    "private-effective-date-sentinel",
                ),
                (
                    artifact_for(invalid_range, content_type="text/html"),
                    DocumentFailureReason.PARSER_AMBIGUOUS_FACT,
                    "private-ambiguous-field-sentinel",
                ),
            )
            for artifact, expected_reason, private_sentinel in cases:
                with self.subTest(expected_reason=expected_reason):
                    with self.assertRaises(RiskDocumentParseError) as caught:
                        parse_artifact(artifact)
                    self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.PARSER)
                    self.assertEqual(caught.exception.failure.reason_code, expected_reason)
                    self.assertNotIn(private_sentinel, repr(caught.exception.failure))

            with patch("kunjin.funds.risk.parsers.MAX_EXTRACTED_CHARACTERS", 3):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    parse_artifact(artifact_for(resource, content_type="text/html"))
            self.assertEqual(caught.exception.code, "official_document_resource_limit")
            self.assertEqual(
                caught.exception.failure.reason_code,
                DocumentFailureReason.RESOURCE_LIMIT,
            )
            self.assertNotIn("private-resource-sentinel", repr(caught.exception.failure))

    def test_parser_failure_record_does_not_capture_private_exception_text(self) -> None:
        error = RiskDocumentParseError(
            "official_document_parse_failed",
            DocumentFailureReason.PARSER_FORMAT_INVALID,
            "private raw text and /private/fixture/path",
        )

        self.assertEqual(error.failure.stage, DocumentFailureStage.PARSER)
        self.assertNotIn("private raw text", repr(error.failure))
        self.assertNotIn("/private/fixture/path", repr(error.failure))
        with self.assertRaises(ValueError):
            RiskDocumentParseError(
                "private_parser_code",
                DocumentFailureReason.PARSER_FORMAT_INVALID,
                "private detail",
            )

    def test_stock_ceiling_keeps_exact_source_location_and_effective_date(self) -> None:
        result = parse_artifact(
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        )
        fact = one(result, "stock_exposure_max_percent")
        self.assertEqual(fact.normalized_value, D("0"))
        self.assertEqual(fact.confidence_state.value, "exact")
        self.assertIn("不投资于股票", fact.source_excerpt)
        self.assertEqual(fact.page_number, None)
        self.assertEqual(fact.section_name, "投资范围")
        self.assertEqual(fact.effective_from, date(2026, 1, 1))
        self.assertEqual(fact.effective_to, date(2026, 12, 31))
        self.assertNotIn("source_document_id", vars(fact))
        self.assertNotIn("fund_code", vars(fact))
        self.assertEqual(
            fact.fact_fingerprint,
            "7657930f34f443a651e048c0eeef1aad2ff941028e7312f8e6330757ee79f64f",
        )

    def test_explicit_identity_benchmark_and_liquidity_text_are_normalized(self) -> None:
        result = parse_artifact(
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        )
        self.assertEqual(one(result, "legal_product_type").normalized_value, "bond_fund")
        self.assertEqual(
            one(result, "benchmark_name").normalized_value,
            "公开合成高等级债券指数",
        )
        self.assertEqual(
            one(result, "minimum_liquid_assets_percent").normalized_value,
            D("5"),
        )
        self.assertIn("长期稳定回报", one(result, "investment_objective").normalized_value)
        expected = {
            "bond_exposure_min_percent": D("80"),
            "cash_exposure_min_percent": D("0"),
            "cash_exposure_max_percent": D("20"),
            "fund_exposure_max_percent": D("0"),
            "derivative_exposure_max_percent": D("0"),
            "domestic_exposure_min_percent": D("80"),
            "hong_kong_exposure_max_percent": D("0"),
            "overseas_exposure_max_percent": D("0"),
            "weighted_average_maturity_max": D("397"),
            "repo_exposure_max_percent": D("20"),
        }
        for fact_kind, value in expected.items():
            with self.subTest(fact_kind=fact_kind):
                self.assertEqual(one(result, fact_kind).normalized_value, value)
        self.assertEqual(one(result, "weighted_average_maturity_max").unit, "days")
        self.assertEqual(one(result, "redemption_restriction").normalized_value, "daily_open")
        self.assertEqual(one(result, "lockup_restriction").normalized_value, "absent")

    def test_literal_absence_is_allowed_but_script_text_is_ignored(self) -> None:
        result = parse_artifact(
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        )
        derivatives = one(result, "derivatives_use")
        self.assertEqual(derivatives.normalized_value, "absent")
        self.assertEqual(derivatives.confidence_state.value, "absent")
        self.assertEqual(one(result, "stock_exposure_max_percent").normalized_value, D("0"))

    def test_broad_index_and_sector_rules_are_only_literal_facts(self) -> None:
        broad = parse_artifact(
            artifact_for(
                FIXTURES / "broad-index-methodology.html",
                content_type="text/html",
                kind=DocumentKind.INDEX_METHODOLOGY,
            )
        )
        self.assertEqual(one(broad, "tracked_index_name").normalized_value, "公开合成300指数")
        methodology = one(broad, "index_methodology_present")
        self.assertIs(methodology.normalized_value, True)
        self.assertEqual(methodology.section_name, "指数目标")
        self.assertIn("跟踪指数", methodology.source_excerpt)
        self.assertEqual(one(broad, "constituent_count").normalized_value, 300)
        self.assertEqual(
            one(broad, "largest_industry_weight_max_percent").normalized_value,
            D("35"),
        )
        self.assertEqual(one(broad, "sector_theme_mandate").normalized_value, "absent")
        self.assertIn("0.35%", one(broad, "tracking_objective").normalized_value)

        sector = parse_artifact(
            artifact_for(
                FIXTURES / "sector-fund-summary.html",
                content_type="text/html",
                kind=DocumentKind.PRODUCT_SUMMARY,
            )
        )
        self.assertEqual(one(sector, "theme_exposure_min_percent").normalized_value, D("80"))
        self.assertEqual(one(sector, "stock_exposure_min_percent").normalized_value, D("80"))
        self.assertEqual(one(sector, "stock_exposure_max_percent").normalized_value, D("95"))
        self.assertEqual(one(sector, "theme_exposure_min_percent").effective_from, date(2026, 3, 1))
        self.assertFalse(any(fact.fact_kind == "derivatives_use" for fact in sector.facts))

    def test_index_legal_types_are_normalized_without_promoting_classification(self) -> None:
        cases = (
            ("基金类型：指数型基金", "index_fund"),
            ("基金类型：指数增强型基金", "index_enhanced_fund"),
            ("Fund Type: Index Fund", "index_fund"),
            ("Fund Type: Index Enhanced Fund", "index_enhanced_fund"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (clause, expected) in enumerate(cases):
                path = root / f"index-type-{index}.html"
                path.write_text(f"<h2>基金概况</h2><p>{clause}</p>", encoding="utf-8")
                with self.subTest(clause=clause):
                    result = parse_artifact(artifact_for(path, content_type="text/html"))
                    self.assertEqual(
                        one(result, "legal_product_type").normalized_value,
                        expected,
                    )

    def test_equivalent_duplicates_deduplicate_by_exact_fingerprint(self) -> None:
        source = """<h2>投资范围</h2><p>本基金不投资于股票。</p><p>本基金不投资于股票。</p>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.html"
            path.write_text(source, encoding="utf-8")
            result = parse_artifact(artifact_for(path, content_type="text/html"))
        facts = [fact for fact in result.facts if fact.fact_kind == "stock_exposure_max_percent"]
        self.assertEqual(len(facts), 1)
        self.assertEqual(result.conflicts, ())

    def test_conflicting_clauses_are_not_silently_collapsed(self) -> None:
        result = parse_artifact(
            artifact_for(FIXTURES / "conflicting-clauses.html", content_type="text/html")
        )
        self.assertIn("duplicate_conflicting_clause", result.conflicts)
        facts = [fact for fact in result.facts if fact.fact_kind == "stock_exposure_max_percent"]
        self.assertEqual([fact.normalized_value for fact in facts], [D("0"), D("20")])
        self.assertEqual(
            [fact.confidence_state.value for fact in facts],
            ["ambiguous", "ambiguous"],
        )
        self.assertEqual(len({fact.fact_fingerprint for fact in facts}), 2)

    def test_parser_is_deterministic_and_rejects_hidden_or_mutable_state(self) -> None:
        artifact = artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        first = parse_artifact(artifact)
        second = parse_artifact(artifact)
        self.assertEqual(first, second)
        self.assertIsInstance(first.facts, tuple)
        with self.assertRaises(FrozenInstanceError):
            first.warnings = ()  # type: ignore[misc]
        fact = first.facts[0]
        fact.validate()
        with self.assertRaises(FrozenInstanceError):
            fact.fact_kind = "changed"  # type: ignore[misc]
        object.__setattr__(fact, "hidden", "state")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            fact.validate()
        object.__setattr__(first, "hidden", "state")
        with self.assertRaisesRegex(ValueError, "unexpected dataclass state"):
            first.validate()

    def test_strict_record_type_and_canonical_value_are_enforced(self) -> None:
        fact = parse_artifact(
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        ).facts[0]

        class FactSubclass(ParsedMandateFact):
            pass

        subclass = FactSubclass(**vars(fact))
        with self.assertRaisesRegex(ValueError, "subclasses"):
            subclass.validate()
        with self.assertRaises(ValueError):
            replace(fact, normalized_value=["mutable"]).validate()

    def test_character_fact_and_excerpt_limits_are_bounded_without_partial_result(self) -> None:
        source = "<h2>投资范围</h2><p>本基金不投资于股票。</p>"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "limits.html"
            path.write_text(source, encoding="utf-8")
            artifact = artifact_for(path, content_type="text/html")
            with patch("kunjin.funds.risk.parsers.MAX_EXTRACTED_CHARACTERS", 3):
                with self.assertRaisesRegex(RiskDocumentParseError, "resource") as characters:
                    parse_artifact(artifact)
                self.assertEqual(characters.exception.code, "official_document_resource_limit")
            with patch("kunjin.funds.risk.parsers.MAX_FACTS", 0):
                with self.assertRaises(RiskDocumentParseError) as facts:
                    parse_artifact(artifact)
                self.assertEqual(facts.exception.code, "official_document_resource_limit")
            with patch("kunjin.funds.risk.parsers.MAX_EXCERPT_CHARACTERS", 8):
                result = parse_artifact(artifact)
        self.assertLessEqual(len(one(result, "stock_exposure_max_percent").source_excerpt), 8)
        self.assertIn("source_excerpt_truncated", result.warnings)

    def test_artifact_integrity_mismatch_and_unsupported_mime_fail_closed(self) -> None:
        artifact = artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        cases = (
            replace(artifact, byte_size=artifact.byte_size + 1),
            replace(artifact, sha256="0" * 64),
            replace(artifact, content_type="image/png"),
            replace(artifact, final_url="http://www.efunds.com.cn/unsafe"),
            replace(artifact, retrieved_at="2026-07-13T12:00:00Z"),
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(RiskDocumentParseError) as caught:
                parse_artifact(case)
            self.assertEqual(caught.exception.code, "official_document_parse_failed")

    def test_declared_utf8_and_gb18030_html_charsets_are_decoded(self) -> None:
        source = "<h2>投资范围</h2><p>本基金不投资于股票。</p>"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (("utf-8", "UTF-8"), ("gb18030", "gb18030"))
            for encoding, declared in cases:
                path = root / f"{encoding}.html"
                path.write_bytes(source.encode(encoding))
                artifact = artifact_for(
                    path,
                    content_type=f"text/html; charset={declared}",
                )
                with self.subTest(encoding=encoding):
                    self.assertEqual(
                        one(
                            parse_artifact(artifact), "stock_exposure_max_percent"
                        ).normalized_value,
                        D("0"),
                    )
            unsupported = artifact_for(
                root / "utf-8.html",
                content_type="text/html; charset=utf-7",
            )
            with self.assertRaises(RiskDocumentParseError):
                parse_artifact(unsupported)

    def test_symlink_and_hidden_artifact_state_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.html"
            target.write_text("<p>本基金不投资于股票。</p>", encoding="utf-8")
            link = root / "link.html"
            link.symlink_to(target)
            with self.assertRaises(RiskDocumentParseError):
                parse_artifact(artifact_for(link, content_type="text/html"))

        artifact = artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        object.__setattr__(artifact, "hidden", True)
        with self.assertRaises(RiskDocumentParseError):
            parse_artifact(artifact)


class RiskDocxParserTest(unittest.TestCase):
    def test_docx_ignores_historical_effective_dates_and_keeps_specific_index_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official.doc"
            write_docx(
                path,
                (
                    "交银示例新能源指数证券投资基金",
                    "2020年10月16日至2020年10月27日召开持有人大会,"
                    "决议自表决通过之日起生效。自2020年11月30日起,"
                    "新基金合同生效。",
                    "基金类型：股票型",
                    "本基金为指数型基金,紧密跟踪标的指数。",
                    "公司所处行业属于新能源或新能源汽车行业。",
                ),
                table_rows=(("基金代码", "519755"),),
            )
            result = parse_artifact(
                artifact_for(
                    path,
                    content_type="application/msword",
                    kind=DocumentKind.PROSPECTUS_UPDATE,
                )
            )

        index_type = one(result, "legal_product_type")
        self.assertEqual(index_type.normalized_value, "index_fund")
        self.assertIsNone(index_type.effective_from)
        self.assertEqual(one(result, "legal_asset_class").normalized_value, "equity_fund")
        self.assertEqual(one(result, "sector_theme_mandate").normalized_value, "present")
        self.assertNotIn("duplicate_conflicting_clause", result.conflicts)

    def test_docx_without_code_requires_exact_legal_name_from_official_title(self) -> None:
        legal_name = "交银施罗德多策略回报灵活配置混合型证券投资基金"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official.doc"
            write_docx(
                path,
                (
                    legal_name,
                    "本基金是一只混合型基金,其风险和预期收益高于债券型基金。",
                    "股票资产(含存托凭证)占基金资产的0%-95%",
                    "赎回金额 = 10,000×1.0160-50.80 = 10,109.20元",
                ),
            )
            artifact = artifact_for(path, content_type="application/msword")
            matching = replace(
                artifact,
                candidate=replace(
                    artifact.candidate,
                    title=legal_name + "招募说明书更新",
                ),
            )
            mismatched = replace(
                matching,
                candidate=replace(
                    matching.candidate,
                    title="交银施罗德其他混合型证券投资基金招募说明书更新",
                ),
            )

            result = parse_artifact(matching)
            with self.assertRaisesRegex(RiskDocumentParseError, "identity") as caught:
                parse_artifact(mismatched)

        self.assertEqual(one(result, "legal_product_type").normalized_value, "mixed_fund")
        self.assertEqual(one(result, "stock_exposure_min_percent").normalized_value, D("0"))
        self.assertEqual(one(result, "stock_exposure_max_percent").normalized_value, D("95"))
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
        )
        self.assertNotIn("交银施罗德其他", repr(caught.exception.failure))

    def test_docx_served_as_application_msword_extracts_traceable_literal_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "official.doc"
            write_docx(
                path,
                (
                    "投资范围",
                    "公司所处行业属于新能源或新能源汽车行业",
                    "投资于新能源主题证券的比例不低于非现金基金资产的80%",
                    "股票资产（含存托凭证）占基金资产的比例为80%-95%",
                ),
                table_rows=(("基金类型", "指数型基金", "基金代码", "519755"),),
            )
            result = parse_artifact(
                artifact_for(
                    path,
                    content_type="application/msword",
                    kind=DocumentKind.PRODUCT_SUMMARY,
                )
            )

        self.assertEqual(one(result, "legal_product_type").normalized_value, "index_fund")
        self.assertEqual(one(result, "theme_exposure_min_percent").normalized_value, D("80"))
        self.assertEqual(one(result, "stock_exposure_min_percent").normalized_value, D("80"))
        self.assertEqual(one(result, "stock_exposure_max_percent").normalized_value, D("95"))
        self.assertEqual(one(result, "sector_theme_mandate").normalized_value, "present")
        self.assertIsNone(one(result, "legal_product_type").page_number)

    def test_docx_rejects_macros_external_relationships_and_resource_bombs(self) -> None:
        external_relationship = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="x" Target="https://evil.example/a" TargetMode="External"/>
</Relationships>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            macro = root / "macro.docx"
            external = root / "external.docx"
            oversized = root / "oversized.docx"
            write_docx(
                macro, ("基金类型：混合型基金",), extra_entries=(("word/vbaProject.bin", b"x"),)
            )
            write_docx(
                external,
                ("基金类型：混合型基金",),
                extra_entries=(("word/_rels/document.xml.rels", external_relationship),),
            )
            write_docx(oversized, ("基金类型：混合型基金" + "x" * 1000,))

            for path in (macro, external):
                with self.subTest(path=path.name), self.assertRaises(RiskDocumentParseError):
                    parse_artifact(artifact_for(path, content_type="application/msword"))
            with patch("kunjin.funds.risk.parsers.MAX_DOCX_UNCOMPRESSED_BYTES", 32):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    parse_artifact(artifact_for(oversized, content_type="application/msword"))
            self.assertEqual(caught.exception.code, "official_document_resource_limit")

    def test_raw_zip_remains_unsupported_and_ole_requires_converter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_zip = root / "raw.zip"
            with zipfile.ZipFile(raw_zip, "w") as archive:
                archive.writestr("public.txt", "public")
            legacy = root / "legacy.doc"
            legacy.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
            with self.assertRaises(RiskDocumentParseError):
                parse_artifact(artifact_for(raw_zip, content_type="application/msword"))
            with self.assertRaises(LegacyDocConversionError):
                parse_artifact(artifact_for(legacy, content_type="application/msword"))


class RiskLegacyConvertedParserTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.ole_path = self.root / "synthetic.doc"
        self.ole_path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic routing only")
        self.fixture_html = (FIXTURES / "legacy-converted-report.html").read_text(encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def parse(self, html: str | None = None, **artifact_changes: object) -> ParsedArtifactResult:
        artifact = legacy_artifact(self.ole_path)
        if artifact_changes:
            artifact = replace(artifact, **artifact_changes)
        return parse_artifact_with_provenance(
            artifact,
            legacy_converter=FakeLegacyConverter(html or self.fixture_html),
        )

    def test_ole_parser_requires_injected_converter(self) -> None:
        with self.assertRaises(LegacyDocConversionError) as caught:
            parse_artifact_with_provenance(legacy_artifact(self.ole_path))

        self.assertEqual(caught.exception.code, "official_document_parse_failed")
        self.assertEqual(caught.exception.failure.stage, DocumentFailureStage.CONVERSION)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.LEGACY_CONVERTER_UNAVAILABLE,
        )

    def test_report_contract_helpers_use_only_explicit_supported_period_forms(self) -> None:
        self.assertEqual(report_period_end("示例基金2026年年度报告"), date(2026, 12, 31))
        self.assertEqual(report_period_end("示例基金2026年半年度报告"), date(2026, 6, 30))
        self.assertEqual(report_period_end("示例基金2026年中期报告"), date(2026, 6, 30))
        self.assertEqual(report_period_end("示例基金2026年第一季度报告"), date(2026, 3, 31))
        self.assertEqual(report_period_end("示例基金2026年第2季度报告"), date(2026, 6, 30))
        self.assertEqual(report_period_end("示例基金2026年第3季度报告"), date(2026, 9, 30))
        self.assertEqual(report_period_end("示例基金2026年第四季度报告"), date(2026, 12, 31))
        self.assertIsNone(report_period_end("发布于2026年7月13日"))
        self.assertIsNone(report_period_end("示例基金0000年第2季度报告"))
        self.assertEqual(document_kind_markers(DocumentKind.QUARTERLY_REPORT), ("季度报告",))

    def test_converted_html_requires_exact_fund_code_or_exact_legal_name(self) -> None:
        exact_code = self.fixture_html.replace(
            "交银示例混合型证券投资基金2026年第2季度报告",
            "2026年第2季度报告",
        ).replace("<p>基金名称：交银示例混合型证券投资基金</p>", "")
        without_code = self.fixture_html.replace("<tr><th>基金代码</th><td>519755</td></tr>", "")
        missing_both = without_code.replace(
            "交银示例混合型证券投资基金2026年第2季度报告",
            "2026年第2季度报告",
        ).replace("<p>基金名称：交银示例混合型证券投资基金</p>", "")

        self.parse(exact_code)
        self.parse(without_code)
        with self.assertRaises(RiskDocumentParseError) as caught:
            self.parse(missing_both)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
        )

    def test_converted_html_rejects_conflicting_fund_identity(self) -> None:
        conflicts = (
            self.fixture_html.replace("</body>", "<p>基金代码：000001</p></body>"),
            self.fixture_html.replace(
                "</body>",
                "<p>基金名称：其他示例混合型证券投资基金</p></body>",
            ),
        )
        for conflicting in conflicts:
            with self.subTest(conflicting=conflicting[-80:]):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    self.parse(conflicting)
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
                )

    def test_converted_identity_ignores_explicit_target_fund_section(self) -> None:
        target_fund = self.fixture_html.replace(
            "</body>",
            "<h2>目标基金基本情况</h2>"
            "<p>基金代码：159913</p>"
            "<p>基金名称：深证300价值交易型开放式指数证券投资基金</p>"
            "</body>",
        )

        self.parse(target_fund)

    def test_converted_identity_uses_nfc_without_compatibility_folding(self) -> None:
        fullwidth_code_only = (
            self.fixture_html.replace(
                "交银示例混合型证券投资基金2026年第2季度报告",
                "2026年第2季度报告",
            )
            .replace("<p>基金名称：交银示例混合型证券投资基金</p>", "")
            .replace("519755", "５１９７５５")
        )
        compatibility_name = self.fixture_html.replace(
            "<tr><th>基金代码</th><td>519755</td></tr>", ""
        ).replace("交银示例混合型证券投资基金", "ＡＢＣ示例混合型证券投资基金")
        candidate = replace(
            legacy_artifact(self.ole_path).candidate,
            title="ABC示例混合型证券投资基金2026年第2季度报告",
        )

        for html, artifact_changes in (
            (fullwidth_code_only, {}),
            (compatibility_name, {"candidate": candidate}),
        ):
            with self.subTest(html=html[:80]):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    self.parse(html, **artifact_changes)
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
                )

    def test_converted_identity_accepts_canonical_nfc_equivalence(self) -> None:
        decomposed = "Cafe\u0301示例混合型证券投资基金"
        composed = "Caf\u00e9示例混合型证券投资基金"
        html = self.fixture_html.replace("<tr><th>基金代码</th><td>519755</td></tr>", "").replace(
            "交银示例混合型证券投资基金", composed
        )
        candidate = replace(
            legacy_artifact(self.ole_path).candidate,
            title=decomposed + "2026年第2季度报告",
        )

        self.parse(html, candidate=candidate)

    def test_converted_text_fact_values_preserve_compatibility_characters(self) -> None:
        html = self.fixture_html.replace(
            "</body>",
            "<p>跟踪指数：ＡＢＣ５０指数</p></body>",
        )

        parsed = self.parse(html).document

        self.assertEqual(one(parsed, "tracked_index_name").normalized_value, "ＡＢＣ５０指数")

    def test_converted_html_requires_document_kind_heading_match(self) -> None:
        artifact = legacy_artifact(
            self.ole_path,
            title="交银示例混合型证券投资基金2026年年度报告",
            kind=DocumentKind.ANNUAL_REPORT,
        )
        with self.assertRaises(RiskDocumentParseError) as caught:
            parse_artifact_with_provenance(
                artifact,
                legacy_converter=FakeLegacyConverter(self.fixture_html),
            )
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
        )

    def test_fund_contract_accepts_exact_split_cover_over_stale_template_title(self) -> None:
        html = (
            "<!DOCTYPE html><html><head>"
            "<title>___证券投资基金招募说明书1</title>"
            "</head><body>"
            "<p>XXXX证券投资基金 募集申请材料－基金合同（草案）</p>"
            "<p>交银示例混合型证券投资基金</p>"
            "<p>基金合同</p>"
            "<p>基金代码：519755</p>"
            "</body></html>"
        )
        artifact = legacy_artifact(
            self.ole_path,
            title="交银示例混合型证券投资基金基金合同",
            kind=DocumentKind.FUND_CONTRACT,
        )

        parsed = parse_artifact_with_provenance(
            artifact,
            legacy_converter=FakeLegacyConverter(html),
        )

        self.assertEqual(
            parsed.document.artifact.candidate.document_kind,
            DocumentKind.FUND_CONTRACT,
        )

    def test_periodic_converted_html_requires_exact_report_period_match(self) -> None:
        wrong_period = self.fixture_html.replace("2026年第2季度报告", "2026年第1季度报告")

        with self.assertRaises(RiskDocumentParseError) as caught:
            self.parse(wrong_period)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
        )

    def test_periodic_converted_html_accepts_exact_leading_cover_paragraph(self) -> None:
        cover_paragraph = self.fixture_html.replace(
            "<h1>交银示例混合型证券投资基金2026年第2季度报告</h1>",
            "<p>交银示例混合型证券投资基金2026年第2季度报告</p>",
        )

        self.parse(cover_paragraph)

    def test_periodic_converted_html_rejects_late_body_title_as_cover_evidence(self) -> None:
        late_title = "".join(f"<p>封面占位段落{index}</p>" for index in range(9))
        late_title += "<p>交银示例混合型证券投资基金2026年第2季度报告</p>"
        body_only = self.fixture_html.replace(
            "<h1>交银示例混合型证券投资基金2026年第2季度报告</h1>",
            late_title,
        )

        with self.assertRaises(RiskDocumentParseError) as caught:
            self.parse(body_only)
        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
        )

    def test_periodic_converted_html_ignores_document_kind_words_in_body_headings(self) -> None:
        body_reference = self.fixture_html.replace(
            "</body>",
            "<h2>自基金合同生效以来基金累计净值增长率变动</h2></body>",
        )

        self.parse(body_reference)

    def test_periodic_converted_html_rejects_invalid_and_conflicting_period_evidence(self) -> None:
        invalid_year = self.fixture_html.replace("2026年第2季度报告", "0000年第2季度报告")
        conflicting_heading = self.fixture_html.replace(
            "</h1>",
            "</h1><h2>2026年第1季度报告及2026年第2季度报告</h2>",
            1,
        )

        for html, artifact in (
            (
                invalid_year,
                legacy_artifact(
                    self.ole_path,
                    title="交银示例混合型证券投资基金0000年第2季度报告",
                ),
            ),
            (conflicting_heading, legacy_artifact(self.ole_path)),
        ):
            with self.subTest(html=html[:100]):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    parse_artifact_with_provenance(
                        artifact,
                        legacy_converter=FakeLegacyConverter(html),
                    )
                self.assertEqual(
                    caught.exception.failure.reason_code,
                    DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
                )

    def test_periodic_converted_html_rejects_publication_before_report_period_end(self) -> None:
        future_report = self.fixture_html.replace(
            "2026年第2季度报告",
            "2027年第四季度报告",
        )
        candidate = replace(
            legacy_artifact(self.ole_path).candidate,
            title="交银示例混合型证券投资基金2027年第四季度报告",
        )

        with self.assertRaises(RiskDocumentParseError) as caught:
            self.parse(future_report, candidate=candidate)

        self.assertEqual(
            caught.exception.failure.reason_code,
            DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
        )

    def test_converted_html_title_is_identity_kind_and_period_evidence(self) -> None:
        cases = (
            (
                self.fixture_html.replace(
                    "公开合成基金报告",
                    "其他示例混合型证券投资基金2026年第2季度报告",
                ),
                DocumentFailureReason.PARSER_IDENTITY_MISMATCH,
            ),
            (
                self.fixture_html.replace(
                    "公开合成基金报告",
                    "交银示例混合型证券投资基金2026年年度报告",
                ),
                DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
            ),
            (
                self.fixture_html.replace(
                    "公开合成基金报告",
                    "交银示例混合型证券投资基金2026年第1季度报告",
                ),
                DocumentFailureReason.PARSER_EFFECTIVE_DATE_INVALID,
            ),
        )

        for html, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                with self.assertRaises(RiskDocumentParseError) as caught:
                    self.parse(html)
                self.assertEqual(caught.exception.failure.reason_code, expected_reason)

        self.parse()

    def test_explicit_table_cells_preserve_label_unit_and_value_binding(self) -> None:
        parsed = self.parse()
        stock = one(parsed.document, "current_stock_asset_allocation_percent")

        self.assertEqual(stock.normalized_value, D("35.2"))
        self.assertEqual(stock.unit, "percent_of_total_assets")
        self.assertIn("报告期末股票资产占基金总资产的", stock.source_excerpt)
        self.assertIn("35.2%", stock.source_excerpt)
        self.assertEqual(
            parsed.parser_input_sha256, hashlib.sha256(self.fixture_html.encode()).hexdigest()
        )
        self.assertEqual(parsed.provenance, LEGACY_PROVENANCE)
        self.assertEqual(parsed.document.artifact.sha256, legacy_artifact(self.ole_path).sha256)
        self.assertEqual(stock.effective_from, date(2026, 6, 30))
        self.assertEqual(stock.effective_to, date(2026, 6, 30))

    def test_converted_current_observations_require_unambiguous_bound_table_rows(self) -> None:
        html = self.fixture_html.replace(
            "</body>",
            """
<p>报告期末股票资产占基金总资产的99.9%</p>
<ul><li>报告期末最大行业为科技,占基金资产88.8%</li></ul>
<dl><dd>报告期末前十大持仓合计占基金资产77.7%</dd></dl>
<p>报告期末最大单一证券占基金资产66.6%</p>
<p>每日开放赎回</p>
</body>""",
        )
        parsed = self.parse(html).document

        self.assertEqual(
            one(parsed, "current_stock_asset_allocation_percent").normalized_value,
            D("35.2"),
        )
        self.assertFalse(
            any(
                fact.fact_kind
                in {
                    "current_largest_industry_name",
                    "current_largest_industry_weight_percent",
                    "current_top_ten_holdings_weight_percent",
                    "current_largest_security_weight_percent",
                }
                for fact in parsed.facts
            )
        )
        self.assertEqual(one(parsed, "redemption_restriction").normalized_value, "daily_open")

    def test_converted_bound_table_rows_allow_all_current_observation_facts(self) -> None:
        html = self.fixture_html.replace(
            "</table>\n</body>",
            """<tr><td>报告期末最大行业为科技,占基金资产</td><td>%</td><td>25.0</td></tr>
<tr><td>报告期末前十大持仓合计占基金资产</td><td>%</td><td>40.0</td></tr>
<tr><td>报告期末最大单一证券占基金资产</td><td>%</td><td>8.0</td></tr>
</table>
</body>""",
        )
        parsed = self.parse(html).document

        self.assertEqual(one(parsed, "current_largest_industry_name").normalized_value, "科技")
        self.assertEqual(
            one(parsed, "current_largest_industry_weight_percent").normalized_value,
            D("25.0"),
        )
        self.assertEqual(
            one(parsed, "current_top_ten_holdings_weight_percent").normalized_value,
            D("40.0"),
        )
        self.assertEqual(
            one(parsed, "current_largest_security_weight_percent").normalized_value,
            D("8.0"),
        )

    def test_converted_table_cells_allow_common_libreoffice_text_wrappers(self) -> None:
        wrapped = self.fixture_html.replace(
            "<tr><th>指标</th><th>单位</th><th>数值</th></tr>",
            "<tr><th><p><strong>指标</strong></p></th><th><span>单位</span></th>"
            "<th><b>数值</b></th></tr>",
        ).replace(
            "<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35.2</td></tr>",
            """<tr>
<td><p><span>报告期末股票</span><font><b>资产占基金总资产的</b></font></p></td>
<td><strong>%</strong></td>
<td><i><em><u><s><small><sup><sub>35.2</sub></sup></small></s></u></em></i></td>
</tr>""",
        )

        stock = one(
            self.parse(wrapped).document,
            "current_stock_asset_allocation_percent",
        )

        self.assertEqual(stock.normalized_value, D("35.2"))

    def test_converted_table_cell_br_preserves_safe_label_whitespace(self) -> None:
        wrapped = self.fixture_html.replace(
            "<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35.2</td></tr>",
            "<tr><td><p>报告期末股票资产<br>占基金总资产的</p></td>"
            "<td><strong>%</strong></td><td><b>35.2</b></td></tr>",
        )

        stock = one(
            self.parse(wrapped).document,
            "current_stock_asset_allocation_percent",
        )

        self.assertEqual(stock.normalized_value, D("35.2"))
        self.assertIn("股票资产 占基金总资产", stock.source_excerpt)

    def test_nested_converted_table_cells_publish_no_current_observation(self) -> None:
        nested = self.fixture_html.replace(
            "<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35.2</td></tr>",
            "<tr><td><table><tr><td>报告期末股票资产占基金总资产的</td>"
            "<td>%</td><td>35.2</td></tr></table></td><td>%</td><td>35.2</td></tr>",
        )
        parsed = self.parse(nested).document

        self.assertFalse(
            any(fact.fact_kind == "current_stock_asset_allocation_percent" for fact in parsed.facts)
        )

    def test_merged_or_flattened_ambiguous_cells_publish_no_financial_fact(self) -> None:
        ambiguous = self.fixture_html.replace(
            "<tr><td>报告期末股票资产占基金总资产的</td><td>%</td><td>35.2</td></tr>",
            '<tr><td colspan="3">报告期末股票资产占基金总资产的35.2%</td></tr>',
        ).replace(
            "<tr><td>报告期末债券资产占基金总资产的</td><td>%</td><td>60.0</td></tr>",
            "<tr><td>报告期末债券资产占基金总资产的60.0%</td></tr>",
        )
        parsed = self.parse(ambiguous).document

        self.assertFalse(
            any(
                fact.fact_kind
                in {
                    "current_stock_asset_allocation_percent",
                    "current_bond_asset_allocation_percent",
                }
                for fact in parsed.facts
            )
        )

    def test_native_html_pdf_and_docx_keep_native_provenance_and_existing_facts(self) -> None:
        docx_path = self.root / "native.docx"
        write_docx(
            docx_path,
            ("投资范围", "本基金不投资于股票。"),
            table_rows=(("基金代码", "519755"),),
        )
        artifacts = (
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html"),
            artifact_for(
                FIXTURES / "current-report.pdf",
                content_type="application/pdf",
                kind=DocumentKind.ANNUAL_REPORT,
            ),
            artifact_for(docx_path, content_type="application/msword"),
        )

        for artifact in artifacts:
            with self.subTest(path=artifact.managed_path.name):
                legacy_wrapper = parse_artifact(artifact)
                parsed = parse_artifact_with_provenance(artifact)
                self.assertEqual(parsed.document, legacy_wrapper)
                self.assertEqual(parsed.document.facts, legacy_wrapper.facts)
                self.assertEqual(parsed.parser_input_sha256, artifact.sha256)
                self.assertEqual(parsed.provenance, native_parser_provenance())


class RiskPdfParserTest(unittest.TestCase):
    def test_pdf_matches_html_facts_and_keeps_page_section_and_effective_date(self) -> None:
        pdf = parse_artifact(
            artifact_for(
                FIXTURES / "current-report.pdf",
                content_type="application/pdf",
                kind=DocumentKind.ANNUAL_REPORT,
            )
        )
        html = parse_artifact(
            artifact_for(FIXTURES / "pure-bond-prospectus.html", content_type="text/html")
        )
        for fact_kind in (
            "stock_exposure_max_percent",
            "convertible_bond_exposure_max_percent",
            "effective_duration_max",
            "gross_leverage_max_percent",
        ):
            self.assertEqual(
                one(pdf, fact_kind).normalized_value, one(html, fact_kind).normalized_value
            )
        fact = one(pdf, "stock_exposure_max_percent")
        self.assertEqual(fact.page_number, 1)
        self.assertEqual(fact.section_name, "INVESTMENT SCOPE")
        self.assertIn("does not invest in stocks", fact.source_excerpt)
        self.assertEqual(fact.effective_from, date(2026, 1, 1))
        self.assertEqual(one(pdf, "legal_product_type").normalized_value, "bond_fund")
        self.assertIn("controlled risk", one(pdf, "investment_objective").normalized_value)
        current_facts = {
            "current_stock_asset_allocation_percent": D("0"),
            "current_bond_asset_allocation_percent": D("95"),
            "current_cash_asset_allocation_percent": D("5"),
            "current_largest_industry_weight_percent": D("30"),
            "current_top_ten_holdings_weight_percent": D("35"),
            "current_largest_security_weight_percent": D("5"),
        }
        for fact_kind, expected in current_facts.items():
            with self.subTest(fact_kind=fact_kind):
                self.assertEqual(one(pdf, fact_kind).normalized_value, expected)
        self.assertEqual(
            one(pdf, "current_largest_industry_name").normalized_value,
            "Financials",
        )

    def test_page_limit_is_checked_before_text_extraction(self) -> None:
        artifact = artifact_for(FIXTURES / "current-report.pdf", content_type="application/pdf")
        with patch("kunjin.funds.risk.parsers.MAX_PDF_PAGES", 0):
            with patch(
                "kunjin.funds.risk.parsers._pdf_has_active_or_embedded_content"
            ) as active_scan:
                with patch("pypdf._page.PageObject.extract_text") as extract_text:
                    with self.assertRaises(RiskDocumentParseError) as caught:
                        parse_artifact(artifact)
        self.assertEqual(caught.exception.code, "official_document_resource_limit")
        active_scan.assert_not_called()
        extract_text.assert_not_called()

    def test_encrypted_malformed_embedded_and_image_only_pdf_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            encrypted_path = root / "encrypted.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.encrypt("public-synthetic-password")
            with encrypted_path.open("wb") as stream:
                writer.write(stream)

            embedded_path = root / "embedded.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_attachment("public.txt", b"public synthetic attachment")
            with embedded_path.open("wb") as stream:
                writer.write(stream)

            image_only_path = root / "image-only.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with image_only_path.open("wb") as stream:
                writer.write(stream)

            malformed_path = root / "malformed.pdf"
            malformed_path.write_bytes(b"%PDF-1.7\nnot a complete PDF")

            for path in (encrypted_path, embedded_path, image_only_path, malformed_path):
                with self.subTest(path=path.name):
                    with self.assertRaises(RiskDocumentParseError) as caught:
                        parse_artifact(artifact_for(path, content_type="application/pdf"))
                    self.assertEqual(caught.exception.code, "official_document_parse_failed")

    def test_ambiguous_replacement_character_pdf_text_fails_closed(self) -> None:
        artifact = artifact_for(FIXTURES / "current-report.pdf", content_type="application/pdf")
        with patch("pypdf._page.PageObject.extract_text", return_value="ambiguous \ufffd text"):
            with self.assertRaises(RiskDocumentParseError) as caught:
                parse_artifact(artifact)
        self.assertEqual(caught.exception.code, "official_document_parse_failed")

    def test_pdf_active_content_fails_closed(self) -> None:
        from pypdf.generic import DictionaryObject, NameObject

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key in ("/OpenAction", "/AA", "/AcroForm"):
                path = root / (key[1:] + ".pdf")
                writer = PdfWriter()
                writer.add_blank_page(width=100, height=100)
                writer._root_object[NameObject(key)] = DictionaryObject()
                with path.open("wb") as stream:
                    writer.write(stream)
                with self.subTest(key=key), self.assertRaises(RiskDocumentParseError):
                    parse_artifact(artifact_for(path, content_type="application/pdf"))

            javascript_path = root / "javascript.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.add_js("app.alert('public synthetic script')")
            with javascript_path.open("wb") as stream:
                writer.write(stream)
            with self.assertRaises(RiskDocumentParseError):
                parse_artifact(artifact_for(javascript_path, content_type="application/pdf"))


if __name__ == "__main__":
    unittest.main()
