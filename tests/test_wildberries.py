import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx

from pricebot.models import SourceStatus
from pricebot.providers.wildberries import WildberriesProvider
from pricebot.transport import FetchError


class WildberriesTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, data, *, status=200, limit=5):
        def handle(request):
            self.assertEqual(request.url.host, "search.wb.ru")
            self.assertEqual(request.url.params["sort"], "priceup")
            self.assertEqual(request.url.params["curr"], "rub")
            self.assertEqual(request.url.params["dest"], "-1257786")
            return httpx.Response(status, json=data)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await WildberriesProvider(client).search("iphone 16", limit=limit)

    async def test_current_schema_sorted_stock_and_wallet_discount(self):
        result = await self.request(
            {
                "products": [
                    {
                        "id": 2,
                        "name": "iPhone 16 256 GB",
                        "totalQuantity": 8,
                        "sizes": [
                            {"stocks": [{"qty": 0}], "price": {"product": 100}},
                            {"stocks": [{"qty": 3}], "price": {"product": 4200000, "wallet": 3900000}},
                            {"stocks": [{"qty": 5}], "price": {"product": 4300000}},
                        ],
                    },
                    {"id": 1, "name": "iPhone 16 128 GB", "salePriceU": 4000000},
                    {"id": 3, "name": "Gone", "totalQuantity": 0, "salePriceU": 100},
                    {"id": 4, "name": "Bad currency", "currency": "USD", "salePriceU": 100},
                ]
            }
        )
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertEqual([p.price for p in result.items], [Decimal("40000"), Decimal("42000")])
        self.assertEqual(result.items[1].source, "wildberries")
        self.assertEqual(result.items[0].url, "https://www.wildberries.ru/catalog/1/detail.aspx")
        self.assertIn("не подтверждён", result.message)

    async def test_legacy_nested_schema_and_duplicate_id(self):
        result = await self.request(
            {"data": {"products": [
                {"id": 1, "name": "Phone", "salePriceU": 9900},
                {"id": 1, "name": "Phone", "salePriceU": 10000},
                {"id": 2, "name": "Phone", "salePriceU": 12000},
            ]}}, limit=1
        )
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].price, Decimal("99"))

    async def test_bad_prices_and_missing_stock(self):
        result = await self.request({"products": [
            {"id": 1, "name": "Phone", "salePriceU": "NaN"},
            {"id": 2, "name": "Phone", "salePriceU": "-5"},
            {"id": 3, "name": "Phone", "salePriceU": True},
            {"id": 4, "name": "Phone", "salePriceU": 100, "sizes": [{"stocks": [], "price": {"product": 1}}]},
            {"id": "bad", "name": "Phone", "salePriceU": 100},
            {"id": 5, "name": "", "salePriceU": 100},
        ]})
        self.assertEqual(result.status, SourceStatus.EMPTY)
        self.assertEqual(result.items, [])

    async def test_empty_does_not_mask_changed_schema(self):
        result = await self.request({"products": []})
        self.assertEqual(result.status, SourceStatus.EMPTY)
        result = await self.request({"new_catalog": []})
        self.assertEqual(result.status, SourceStatus.ERROR)

    async def test_blocked_status(self):
        for status in (401, 403, 429, 498):
            with self.subTest(status=status):
                result = await self.request({}, status=status)
                self.assertEqual(result.status, SourceStatus.BLOCKED)
                self.assertNotIn("CAPTCHA", result.message)
                if status == 429:
                    self.assertIn("частоту", result.message)
                elif status in (401, 498):
                    self.assertIn("отклонил", result.message)
                else:
                    self.assertIn("запретил", result.message)

    async def test_nonempty_unknown_price_schema_is_not_empty(self):
        result = await self.request({"products": [{"id": 123, "name": "Phone", "newPrice": 500}]})
        self.assertEqual(result.status, SourceStatus.ERROR)
        self.assertIn("формат цен не распознан", result.message)

    async def test_recognized_but_unavailable_price_is_empty(self):
        result = await self.request({"products": [{
            "id": 123, "name": "Phone", "totalQuantity": 0,
            "sizes": [{"stocks": [], "price": {"product": 500}}],
        }]})
        self.assertEqual(result.status, SourceStatus.EMPTY)

    async def test_browser_path_uses_public_search_and_no_direct_api(self):
        def fail_direct(request):
            self.fail("Browser mode must not call the separate direct API")

        browser = SimpleNamespace(fetch_wildberries=AsyncMock(return_value={"products": [
            {"id": 7, "name": "Phone B", "salePriceU": 4300},
            {"id": 6, "name": "Phone A", "salePriceU": 3200},
        ]}))
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_direct)) as client:
            result = await WildberriesProvider(client, browser=browser, prefer_browser=True).search("телефон & iphone", limit=1)
        url = urlsplit(browser.fetch_wildberries.await_args.args[0])
        self.assertEqual(url.hostname, "www.wildberries.ru")
        self.assertEqual(url.path, "/catalog/0/search.aspx")
        self.assertEqual(parse_qs(url.query), {"search": ["телефон & iphone"], "sort": ["priceup"]})
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertEqual([p.price for p in result.items], [Decimal("32")])

    async def test_browser_region_is_not_claimed_from_http_dest(self):
        browser = SimpleNamespace(fetch_wildberries=AsyncMock(return_value={"data": {"products": [
            {"id": 123, "name": "Phone", "salePriceU": 3200},
        ]}}))
        async with httpx.AsyncClient() as client:
            result = await WildberriesProvider(client, dest="-123", region_label="Липецк",
                                              browser=browser, prefer_browser=True).search("phone")
        self.assertEqual(result.status, SourceStatus.OK)
        self.assertIn("Регион WB не подтверждён", result.message)
        self.assertNotIn("Регион WB: Липецк", result.message)

    async def test_browser_status_failures_are_preserved(self):
        for status in (SourceStatus.BLOCKED, SourceStatus.TIMEOUT, SourceStatus.ERROR):
            with self.subTest(status=status):
                browser = SimpleNamespace(fetch_wildberries=AsyncMock(side_effect=FetchError(status, "Проверка доступа.")))
                async with httpx.AsyncClient() as client:
                    result = await WildberriesProvider(client, browser=browser, prefer_browser=True).search("phone")
                self.assertEqual(result.status, status)
                self.assertEqual(result.items, [])
                self.assertEqual(result.message, "Проверка доступа.")

    async def test_browser_schema_empty_and_unknown_are_distinct(self):
        for data, expected in (({"products": []}, SourceStatus.EMPTY),
                               ({"not_products": []}, SourceStatus.ERROR),
                               ({"products": [{"id": 1, "name": "Phone", "newPrice": 9}]}, SourceStatus.ERROR)):
            with self.subTest(data=data):
                browser = SimpleNamespace(fetch_wildberries=AsyncMock(return_value=data))
                async with httpx.AsyncClient() as client:
                    result = await WildberriesProvider(client, browser=browser, prefer_browser=True).search("phone")
                self.assertEqual(result.status, expected)

    async def test_browser_not_configured_returns_error_without_direct_fallback(self):
        def fail_direct(request):
            self.fail("Missing browser must not cause a direct API retry")

        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_direct)) as client:
            result = await WildberriesProvider(client, prefer_browser=True).search("phone")
        self.assertEqual(result.status, SourceStatus.ERROR)

    async def test_captcha_html(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<h1>Captcha</h1>")
        )) as client:
            result = await WildberriesProvider(client).search("phone")
        self.assertEqual(result.status, SourceStatus.BLOCKED)

    async def test_timeout_keeps_error_detail_private(self):
        def handle(request):
            raise httpx.ReadTimeout("secret-url-token", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await WildberriesProvider(client).search("phone")
        self.assertEqual(result.status, SourceStatus.TIMEOUT)
        self.assertNotIn("secret", result.message)

    async def test_404_tries_legacy_version_once(self):
        calls = []

        def handle(request):
            calls.append(request.url.path)
            if "v18" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, json={"data": {"products": []}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await WildberriesProvider(client).search("phone")
        self.assertEqual(result.status, SourceStatus.EMPTY)
        self.assertEqual(calls, ["/exactmatch/ru/common/v18/search", "/exactmatch/ru/common/v9/search"])

    async def test_oversized_response_is_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * (8 * 1024 * 1024 + 1))
        )) as client:
            result = await WildberriesProvider(client).search("phone")
        self.assertEqual(result.status, SourceStatus.ERROR)

    async def test_blank_query_does_not_request_network(self):
        def handle(request):
            self.fail("Unexpected network request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await WildberriesProvider(client).search("  ")
        self.assertEqual(result.status, SourceStatus.EMPTY)


if __name__ == "__main__":
    unittest.main()
