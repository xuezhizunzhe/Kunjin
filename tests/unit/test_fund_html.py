import unittest

from kunjin.funds.html import (
    HtmlLink,
    HtmlTable,
    extract_labeled_values,
    extract_links,
    parse_tables,
)

BASE_URL = "https://fundf10.eastmoney.com/path/page.html"


class FundHtmlTableTest(unittest.TestCase):
    def test_parses_nested_text_entities_links_and_repeated_headers(self) -> None:
        text = """
        <h2>不应覆盖显式标题</h2>
        <table>
          <caption> 基金<!-- ignored -->经理 </caption>
          <thead>
            <tr><th>姓名</th><th>任职日期</th><th>离任日期</th></tr>
          </thead>
          <tbody>
            <tr>
              <td><a href="/manager/1"><strong>张&nbsp;三</strong></a></td>
              <td> 2024-01-01 </td><td><span>至今</span></td>
            </tr>
            <tr><th>姓名</th><th>任职日期</th><th>离任日期</th></tr>
          </tbody>
        </table>
        """

        self.assertEqual(
            parse_tables(text, BASE_URL),
            [
                HtmlTable(
                    caption="基金经理",
                    headers=("姓名", "任职日期", "离任日期"),
                    rows=(("张 三", "2024-01-01", "至今"),),
                    links=(("张 三", "https://fundf10.eastmoney.com/manager/1"),),
                )
            ],
        )

    def test_expands_rowspan_and_colspan_into_rectangular_rows(self) -> None:
        text = """
        <h3>费率结构</h3>
        <table>
          <tr><th rowspan="2">类别</th><th colspan="2">费率</th></tr>
          <tr><th>下限</th><th>上限</th></tr>
          <tr><td rowspan="2">申购</td><td>0%</td><td>1%</td></tr>
          <tr><td colspan="2">固定 10 元</td></tr>
        </table>
        """

        self.assertEqual(
            parse_tables(text, BASE_URL),
            [
                HtmlTable(
                    caption="费率结构",
                    headers=("类别", "费率", "费率"),
                    rows=(
                        ("类别", "下限", "上限"),
                        ("申购", "0%", "1%"),
                        ("申购", "固定 10 元", "固定 10 元"),
                    ),
                    links=(),
                )
            ],
        )

    def test_ignores_active_and_embedded_content_without_fetching_it(self) -> None:
        text = """
        <script>document.write('<table><tr><td>evil</td></tr></table>')</script>
        <style>.x { background: url(https://invalid.example/style.png) }</style>
        <iframe src="https://invalid.example/frame">
          <img src="https://invalid.example/nested.png"><p>frame text</p>
        </iframe>
        <table><tr><th>名称</th></tr><tr><td>安全内容</td></tr></table>
        <img src="https://invalid.example/image.png">
        """

        tables = parse_tables(text, BASE_URL)

        self.assertEqual(tables[0].rows, (("安全内容",),))
        self.assertNotIn("evil", repr(tables))
        self.assertEqual(extract_links(text, BASE_URL), [])

    def test_nested_table_does_not_corrupt_its_parent_row(self) -> None:
        text = """
        <table>
          <tr><th>名称</th><th>说明</th></tr>
          <tr><td>外层</td><td>之前<table><tr><td>内层</td></tr></table>之后</td></tr>
        </table>
        """

        self.assertEqual(
            parse_tables(text, BASE_URL)[0].rows,
            (("外层", "之前之后"),),
        )

    def test_malformed_html_returns_sanitized_partial_result(self) -> None:
        secret_body = "SECRET_PAGE_BODY"
        text = (
            "<h2>持仓</h2><table><tr><th>证券</th></tr>"
            f"<tr><td>{secret_body}<a href='/security/1'>浦发银行"
        )

        tables = parse_tables(text, BASE_URL)

        self.assertEqual(tables[0].caption, "持仓")
        self.assertEqual(tables[0].headers, ("证券",))
        self.assertEqual(tables[0].rows, ((f"{secret_body}浦发银行",),))
        self.assertEqual(
            tables[0].links,
            (("浦发银行", "https://fundf10.eastmoney.com/security/1"),),
        )


class FundHtmlExtractionTest(unittest.TestCase):
    def test_extracts_definition_table_and_paragraph_values(self) -> None:
        text = """
        <dl>
          <dt>基金代码</dt><dd>519755</dd>
          <dt>基金经理</dt><dd>张三</dd><dd>李四</dd>
        </dl>
        <table>
          <tr><th>项目</th><th>内容</th></tr>
          <tr><td>基金类型</td><td><span>混合型</span></td></tr>
        </table>
        <p>业绩比较基准：沪深300 &amp; 中债指数</p>
        """

        self.assertEqual(
            extract_labeled_values(text, BASE_URL),
            {
                "基金代码": ["519755"],
                "基金经理": ["张三", "李四"],
                "基金类型": ["混合型"],
                "业绩比较基准": ["沪深300 & 中债指数"],
            },
        )

    def test_extracts_normalized_links_in_document_order(self) -> None:
        text = """
        <a href="../fund/519755.html"><span>示例&nbsp;基金 A</span></a>
        <a href="https://example.com/report.pdf">季度报告</a>
        <a>没有地址</a>
        <script><a href="/hidden">隐藏链接</a></script>
        """

        self.assertEqual(
            extract_links(text, BASE_URL),
            [
                HtmlLink(
                    text="示例 基金 A",
                    url="https://fundf10.eastmoney.com/fund/519755.html",
                ),
                HtmlLink(text="季度报告", url="https://example.com/report.pdf"),
            ],
        )

    def test_empty_and_whitespace_only_documents_are_supported(self) -> None:
        self.assertEqual(parse_tables(" \n\t", BASE_URL), [])
        self.assertEqual(extract_labeled_values("", BASE_URL), {})
        self.assertEqual(extract_links("", BASE_URL), [])


if __name__ == "__main__":
    unittest.main()
