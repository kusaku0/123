import unittest
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from pricebot.models import Product, SearchResult, SourceStatus
from pricebot.render import render_results, search_links


class RenderTests(unittest.TestCase):
    def test_prices_sorted_and_region_warning_visible(self):
        results = [
            SearchResult("wildberries", [Product("wildberries", "iPhone 15", "https://www.wildberries.ru/catalog/1/detail.aspx", Decimal("12000"))], SourceStatus.OK, "Регион WB не подтверждён; настройте WB_DEST."),
            SearchResult("avito", [Product("avito", "iPhone 15 б/у", "https://www.avito.ru/lipetsk/item_1", Decimal("9900.50"))], SourceStatus.OK),
        ]
        rendered = render_results("iphone 15", results, city="Липецк", cached=True)
        self.assertLess(rendered.index("9 900.50 ₽"), rendered.index("12 000.00 ₽"))
        self.assertIn("Регион WB не подтверждён", rendered)
        self.assertIn("Город: Липецк", rendered)
        self.assertIn("без доставки", rendered)
        self.assertIn("среди найденных", rendered)
        self.assertIn("из кратковременного кэша", rendered)

    def test_untrusted_text_and_href_are_html_escaped(self):
        results = [SearchResult(
            "wildberries",
            [Product("wildberries", '<b>Телефон & "товар"</b>', 'https://www.wildberries.ru/catalog/1/detail.aspx?x="&y=<', Decimal("123"))],
            SourceStatus.OK,
            '<script>alert("price")</script> & регион',
        )]
        rendered = render_results('<i>iphone & "15"</i>', results, city="<Липецк>")
        self.assertIn('&lt;i&gt;iphone &amp; &quot;15&quot;&lt;/i&gt;', rendered)
        self.assertIn('&lt;b&gt;Телефон &amp; &quot;товар&quot;&lt;/b&gt;', rendered)
        self.assertIn('?x=&quot;&amp;y=&lt;', rendered)
        self.assertIn('&lt;script&gt;alert(&quot;price&quot;)&lt;/script&gt;', rendered)
        self.assertIn('Город: &lt;Липецк&gt;', rendered)
        self.assertNotIn("<script>", rendered)

    def test_source_failure_does_not_hide_working_source(self):
        rendered = render_results("iphone 15", [
            SearchResult("wildberries", [Product("wildberries", "iPhone 15", "https://www.wildberries.ru/catalog/1/detail.aspx", Decimal("999"))], SourceStatus.OK),
            SearchResult("ozon", [], SourceStatus.BLOCKED),
            SearchResult("aliexpress", [], SourceStatus.TIMEOUT),
            SearchResult("avito", [], SourceStatus.ERROR),
        ])
        self.assertIn("999.00 ₽", rendered)
        self.assertIn("Ozon: доступ ограничен", rendered)
        self.assertIn("AliExpress: не ответил вовремя", rendered)
        self.assertIn("Avito: поиск недоступен", rendered)
        self.assertNotIn("Подтверждённых цен получить не удалось", rendered)

    def test_all_failures_include_manual_search_alternatives(self):
        rendered = render_results("iphone 15", [SearchResult("ozon", [], SourceStatus.BLOCKED)])
        self.assertIn("Подтверждённых цен получить не удалось", rendered)
        self.assertIn("Открыть поиск:", rendered)
        for source in ("WB", "Ozon", "AliExpress", "Avito"):
            self.assertIn(f">{source}</a>", rendered)
        self.assertNotIn("Дешевле среди", rendered)

    def test_non_https_product_url_is_not_rendered(self):
        rendered = render_results("phone", [SearchResult("ozon", [Product("ozon", "Phone", "javascript:alert(1)", Decimal("1"))], SourceStatus.OK)])
        self.assertNotIn('href="javascript:', rendered)

    def test_manual_query_links_encode_reserved_characters(self):
        query = 'телефон & q="x"/15 + 1'
        links = search_links(query, "lipetsk")
        for source, key in (("wildberries", "search"), ("ozon", "text"), ("aliexpress", "SearchText"), ("avito", "q")):
            with self.subTest(source=source):
                self.assertEqual(parse_qs(urlsplit(links[source]).query)[key], [query])
        self.assertEqual(urlsplit(links["avito"]).path, "/lipetsk")

    def test_many_results_stay_below_telegram_message_limit(self):
        results = []
        for source in ("wildberries", "ozon", "aliexpress", "avito"):
            items = [Product(source, "Название товара " * 20, f"https://www.example.com/item/{i}", Decimal(i + 1)) for i in range(3)]
            results.append(SearchResult(source, items, SourceStatus.OK, "Регион неизвестен. " * 30))
        rendered = render_results("Поиск " * 60, results)
        # Telegram's limit applies after entity parsing; href bytes and HTML
        # escape sequences do not contribute to the visible text length.
        class VisibleText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []

            def handle_data(self, data):
                self.parts.append(data)

        parser = VisibleText()
        parser.feed(rendered)
        self.assertLessEqual(len("".join(parser.parts)), 4096)
