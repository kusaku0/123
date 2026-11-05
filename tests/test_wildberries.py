import unittest
from decimal import Decimal

import httpx

from pricebot.models import SourceStatus
from pricebot.providers.wildberries import WildberriesProvider


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
