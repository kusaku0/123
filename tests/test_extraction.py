import json
import unittest
from decimal import Decimal

from pricebot.extraction import extract_products, parse_price


class ExtractionTests(unittest.TestCase):
    def test_rubles_only_and_concrete_prices(self):
        for text, currency, expected in [
            ("1\u202f299,50 ₽", None, Decimal("1299.50")),
            ("499", "RUB", Decimal("499")),
            ("99,9 руб.", None, Decimal("99.9")),
            ("1,299.00 RUB", None, Decimal("1299.00")),
            ("1.299,00 RUB", None, Decimal("1299.00")),
            ("Цена: 1 299 рублей", None, Decimal("1299")),
            ("$10.00", "USD", None),
            ("499", None, None),
            ("от 499 ₽", None, None),
            ("499–999 ₽", None, None),
            ("1 299 ₽ 1 999 ₽", None, None),
            ("0 ₽", None, None),
            ("-499 ₽", None, None),
            ("+499 ₽", None, None),
            ("1.999 ₽", None, None),
            ("1 234.567 ₽", None, None),
            ("1,234 RUB", None, None),
            ("499 ₽ в месяц", None, None),
            ("499 RUB USD", None, None),
        ]:
            with self.subTest(text=text):
                self.assertEqual(parse_price(text, currency), expected)

    def test_jsonld_itemlist_sorting_currency_stock_and_links(self):
        def product(name, price, url, currency="RUB", **offer):
            return {"@type": "Product", "name": name, "url": url,
                    "offers": {"price": price, "priceCurrency": currency, **offer}}
        data = {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "item": product("Дорогой кабель", 1200, "/product/cable-1/")},
            {"@type": "ListItem", "item": product("Дешёвый кабель", 900, "/product/cable-2/")},
            product("В долларах", 1, "/product/usd-3/", "USD"),
            product("Нет в наличии", 1, "/product/sold-4/", availability="https://schema.org/OutOfStock"),
            product("Чужая ссылка", 1, "https://ozon.ru.attacker.invalid/product/5/"),
            product("Userinfo", 1, "https://evil@ozon.ru/product/6/"),
            {"@type": "Product", "name": "Цена от", "url": "/product/range-7/",
             "offers": {"@type": "AggregateOffer", "lowPrice": "1", "priceCurrency": "RUB"}},
        ]}
        html = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        items = extract_products(html, "ozon", "https://www.ozon.ru/search/?text=кабель")
        self.assertEqual([item.price for item in items], [Decimal("900"), Decimal("1200")])
        self.assertEqual(items[0].url, "https://www.ozon.ru/product/cable-2/")

    def test_avito_dom(self):
        html = '''<div data-marker="item">
          <a data-marker="item-title" href="/lipetsk/telefony/iphone_12345"><h3>iPhone 13</h3></a>
          <meta itemprop="price" content="10000"><meta itemprop="priceCurrency" content="RUB">
          <span>Отзывы 5, рейтинг 4.9, телефон 123</span>
        </div><div data-marker="item" data-is-ad="true">
          <a data-marker="item-title" href="/lipetsk/telefony/advert_12346">Реклама</a>
          <meta itemprop="price" content="1"><meta itemprop="priceCurrency" content="RUB">
        </div>'''
        items = extract_products(html, "avito", "https://www.avito.ru/lipetsk?q=iphone")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "iPhone 13")
        self.assertEqual(items[0].price, Decimal("10000"))

    def test_ozon_encoded_widget_state(self):
        item = {"sku": "1234", "action": {"link": "/product/mouse-1234/?ref=tracking"},
                "mainState": [
                    {"type": "priceV2", "priceV2": {"price": [{"text": "1 299 ₽"}, {"text": "1 599 ₽"}]}},
                    {"type": "textAtom", "textAtom": {"text": "Мышь беспроводная"}},
                ]}
        data = {"widgetStates": {"searchResultsV2-123": json.dumps({"items": [item]})}}
        html = '<script type="application/json">' + json.dumps(data) + '</script>'
        items = extract_products(html, "ozon", "https://www.ozon.ru/search/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].price, Decimal("1299"))
        self.assertNotIn("tracking", items[0].url)

    def test_aliexpress_known_json_shape(self):
        data = {"data": {"items": [{"itemId": 100500123, "title": {"displayTitle": "USB кабель"},
                                   "prices": {"salePrice": {"minPrice": "200.50", "currencyCode": "RUB"}}}]}}
        html = '<script>window.runParams = ' + json.dumps(data) + ';</script>'
        items = extract_products(html, "aliexpress", "https://aliexpress.ru/wholesale?SearchText=usb")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://aliexpress.ru/item/100500123.html")

    def test_aliexpress_variant_range_is_not_a_concrete_price(self):
        def listing(identifier, low, high):
            return {"itemId": identifier, "title": "iPhone 15",
                    "prices": {"salePrice": {"minPrice": low, "maxPrice": high,
                                             "formattedPrice": f"{low} ₽", "currencyCode": "RUB"}}}
        data = {"items": [listing(1, "500", "50000"), listing(2, "1000", "1000")]}
        html = '<script type="application/json">' + json.dumps(data) + '</script>'
        items = extract_products(html, "aliexpress", "https://aliexpress.ru/wholesale")
        self.assertEqual([item.url for item in items], ["https://aliexpress.ru/item/2.html"])
        self.assertEqual(items[0].price, Decimal("1000"))

    def test_malformed_json_does_not_hide_valid_cards(self):
        html = '''<script type="application/ld+json">{broken}</script>
        <div class="search-card-item"><a title="USB cable" href="/item/100500123.html"></a>
        <span class="search-card-item__price-main">250 ₽</span></div>'''
        self.assertEqual(len(extract_products(html, "aliexpress", "https://aliexpress.ru")), 1)

    def test_huge_page_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_products("x" * 8_000_001, "ozon", "https://www.ozon.ru")


if __name__ == "__main__":
    unittest.main()
