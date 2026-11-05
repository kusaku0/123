import asyncio
import unittest
from decimal import Decimal
from unittest.mock import patch

import httpx

from pricebot.models import Product, SearchResult, SourceStatus
from pricebot.search import SearchService, relevant, resolve_wb_destination


def product(title="iPhone 15 128 GB", price="100", ident="1", currency="RUB"):
    return Product("test", title, f"https://example.com/item/{ident}", Decimal(price), currency)


class Provider:
    def __init__(self, source="test", items=None, *, status=SourceStatus.OK, error=None, gate=None):
        self.source = source
        self.items = [product()] if items is None else items
        self.status = status
        self.error = error
        self.gate = gate
        self.calls = 0
        self.started = asyncio.Event()

    async def search(self, query, limit=5):
        self.calls += 1
        self.started.set()
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return SearchResult(self.source, self.items, self.status, "Регион не подтверждён")


class RelevanceTests(unittest.TestCase):
    def test_model_numbers_match_whole_tokens(self):
        self.assertTrue(relevant("iphone 15", "Apple iPhone 15 128GB"))
        self.assertFalse(relevant("iphone 15", "Apple iPhone 150 128GB"))
        self.assertFalse(relevant("iphone 15 128", "Apple iPhone 15 256 GB"))

    def test_accessories_are_excluded_for_devices_only(self):
        for title in ("Чехол для iPhone 15", "iPhone 15 cable", "Защитное стекло iPhone 15", "Коробка iPhone 15"):
            with self.subTest(title=title):
                self.assertFalse(relevant("iphone 15", title))
        self.assertTrue(relevant("чехол iphone 15", "Прозрачный чехол для iPhone 15"))
        self.assertTrue(relevant("samsung galaxy", "Смартфон Samsung Galaxy S24"))

    def test_normalization_and_empty_queries(self):
        self.assertTrue(relevant(" КУПИТЬ  КОФЁ  ", "Кофе зерновой"))
        self.assertTrue(relevant("ｉＰｈｏｎｅ １５", "iPhone 15 128GB"))
        self.assertFalse(relevant("  ", "Whatever"))
        self.assertFalse(relevant("купить цена", "Whatever"))

    def test_device_queries_exclude_parts_services_rental_and_installments(self):
        examples = (
            ("ноутбук Lenovo", "Зарядка для ноутбука Lenovo"),
            ("Samsung S24", "Материнская плата Samsung S24"),
            ("iPhone 15", "Ремонт iPhone 15"),
            ("iPhone 15", "Аренда iPhone 15"),
            ("iPhone 15", "iPhone 15 рассрочка 1000 ₽ в месяц"),
        )
        for query, title in examples:
            with self.subTest(title=title):
                self.assertFalse(relevant(query, title))


class SearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_and_timeout_preserve_good_results(self):
        blocked = Provider("blocked", [], status=SourceStatus.BLOCKED)
        error = Provider("error", error=RuntimeError("secret-url-token"))
        gate = asyncio.Event()
        slow = Provider("slow", gate=gate)
        service = SearchService([Provider("good"), blocked, error, slow], timeout=0.02)
        results, cached = await service.search("iphone 15")
        self.assertFalse(cached)
        self.assertEqual([r.status for r in results], [SourceStatus.OK, SourceStatus.BLOCKED, SourceStatus.ERROR, SourceStatus.TIMEOUT])
        self.assertEqual(len(results[0].items), 1)
        self.assertNotIn("secret", results[2].message)
        await service.close()

    async def test_sort_dedup_filter_currency_nonfinite_and_irrelevant_results(self):
        provider = Provider(items=[
            product(price="300", ident="a"),
            product(price="100", ident="a"),
            product(price="200", ident="b"),
            product(price="400", ident="c"),
            product(price="500", ident="d"),
            product(price="1", ident="accessory", title="Чехол iPhone 15"),
            product(price="2", ident="model", title="iPhone 150"),
            product(price="3", ident="usd", currency="USD"),
            product(price="0", ident="free"),
            product(price="-2", ident="negative"),
            product(price="NaN", ident="nan"),
            product(price="sNaN", ident="snan"),
            product(price="Infinity", ident="inf"),
            product(price="-Infinity", ident="neg-inf"),
        ])
        service = SearchService([provider])
        results, _ = await service.search("iphone 15")
        self.assertEqual(results[0].status, SourceStatus.OK)
        self.assertEqual([item.price for item in results[0].items], [Decimal("100"), Decimal("200"), Decimal("400")])
        self.assertEqual(len({item.url for item in results[0].items}), 3)
        self.assertEqual(results[0].message, "Регион не подтверждён")
        await service.close()

    async def test_no_relevant_results_are_explicitly_empty(self):
        service = SearchService([Provider(items=[product(title="Чехол iPhone 15")])])
        results, _ = await service.search("iphone 15")
        self.assertEqual(results[0].status, SourceStatus.EMPTY)
        self.assertEqual(results[0].items, [])
        self.assertIn("нет совпадений", results[0].message)
        await service.close()

    async def test_normalized_query_cache_and_expiration(self):
        provider = Provider()
        service = SearchService([provider], ttl=30)
        with patch("pricebot.search.time") as clock:
            clock.monotonic.return_value = 100
            first, cached = await service.search("iPhone 15")
            self.assertFalse(cached)
            second, cached = await service.search("  IPHONE   15  ")
            self.assertTrue(cached)
            self.assertEqual(first, second)
            self.assertEqual(provider.calls, 1)
            clock.monotonic.return_value = 131
            _, cached = await service.search("iphone 15")
            self.assertFalse(cached)
            self.assertEqual(provider.calls, 2)
        await service.close()

    async def test_all_failures_are_not_cached(self):
        provider = Provider(status=SourceStatus.BLOCKED)
        service = SearchService([provider])
        for _ in range(2):
            _, cached = await service.search("iphone 15")
            self.assertFalse(cached)
        self.assertEqual(provider.calls, 2)
        await service.close()

    async def test_concurrent_same_query_shares_fetch_and_survives_caller_cancel(self):
        gate = asyncio.Event()
        provider = Provider(gate=gate)
        service = SearchService([provider])
        first = asyncio.create_task(service.search("iPhone 15"))
        await provider.started.wait()
        second = asyncio.create_task(service.search("IPHONE 15"))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.set()
        results, _ = await second
        self.assertEqual(provider.calls, 1)
        self.assertEqual(results[0].status, SourceStatus.OK)
        await service.close()

    async def test_cache_eviction_preserves_recent_query(self):
        provider = Provider(items=[])
        service = SearchService([provider], max_cache_entries=2)
        await service.search("first")
        await service.search("second")
        await service.search("first")
        await service.search("third")
        _, cached = await service.search("first")
        self.assertTrue(cached)
        _, cached = await service.search("second")
        self.assertFalse(cached)
        self.assertEqual(provider.calls, 4)
        await service.close()

    async def test_close_cancels_only_pending_service_work(self):
        provider = Provider(gate=asyncio.Event())
        service = SearchService([provider])
        request = asyncio.create_task(service.search("iphone 15"))
        await provider.started.wait()
        await service.close()
        with self.assertRaises(asyncio.CancelledError):
            await request


class DestinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolved_destination_comes_from_requested_coordinates(self):
        def handle(request):
            self.assertEqual(request.url.params["latitude"], "52.6088")
            self.assertEqual(request.url.params["longitude"], "39.5992")
            return httpx.Response(200, json={"geoInfo": {"destinations": [-1, -1000]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            self.assertEqual(await resolve_wb_destination(client, 52.6088, 39.5992), "-1000")

    async def test_unverified_destination_not_invented(self):
        for response in (
            httpx.Response(403),
            httpx.Response(200, json={"geoInfo": {"destinations": []}}),
            httpx.Response(200, json={"destinations": [True]}),
            httpx.Response(200, text="CAPTCHA"),
        ):
            with self.subTest(response=response):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
                    self.assertIsNone(await resolve_wb_destination(client, 52.6088, 39.5992))
