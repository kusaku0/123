import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from pricebot.config import Settings
from pricebot.models import Product, SearchResult, SourceStatus
from pricebot.web import create_app


def fake_runtime(results=None, *, cached=False):
    if results is None:
        results = [
            SearchResult("wildberries", [Product("wildberries", "iPhone 15 128GB", "https://www.wildberries.ru/catalog/123/detail.aspx", Decimal("32000.99"))], SourceStatus.OK, "Регион WB не подтверждён."),
            SearchResult("ozon", [], SourceStatus.BLOCKED, "Площадка требует CAPTCHA."),
            SearchResult("aliexpress", [], SourceStatus.TIMEOUT, "Площадка не ответила вовремя."),
            SearchResult("avito", [Product("avito", "iPhone 15 128 ГБ", "https://www.avito.ru/lipetsk/telefony/iphone_123", Decimal("28000"))], SourceStatus.OK, "Проверьте состояние товара."),
        ]
    return SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(),
        service=SimpleNamespace(search=AsyncMock(return_value=(results, cached))),
    )


def settings(**kwargs):
    return Settings("", user_cooldown=0.001, **kwargs)


def test_web_starts_without_telegram_token_and_closes_runtime(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    runtime = fake_runtime()
    with TestClient(create_app(runtime=runtime)) as client:
        runtime.start.assert_awaited_once()
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["city"] == "Липецк"
    runtime.close.assert_awaited_once()


def test_page_and_local_assets_are_available():
    runtime = fake_runtime()
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "/static/app.js" in page.text
        assert "/static/style.css" in page.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/style.css").status_code == 200
        runtime.service.search.assert_not_awaited()


def test_search_preserves_partial_results_sorted_and_reports_every_source():
    runtime = fake_runtime()
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "  iPhone   15  "})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "iPhone 15"
    assert body["city"] == "Липецк"
    assert body["cached"] is False
    assert datetime.fromisoformat(body["checked_at"].replace("Z", "+00:00")).tzinfo is not None
    assert [p["source"] for p in body["products"]] == ["avito", "wildberries"]
    assert [Decimal(p["price"]) for p in body["products"]] == [Decimal("28000"), Decimal("32000.99")]
    assert all(p["currency"] == "RUB" for p in body["products"])
    statuses = {source["source"]: source for source in body["sources"]}
    assert statuses["ozon"]["status"] == "blocked"
    assert statuses["aliexpress"]["status"] == "timeout"
    assert statuses["wildberries"]["count"] == 1
    assert "lipetsk" in statuses["avito"]["search_url"]
    assert "iPhone" in statuses["avito"]["search_url"]
    runtime.service.search.assert_awaited_once_with("iPhone 15")


def test_cached_search_flag_is_exposed():
    runtime = fake_runtime(cached=True)
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "iPhone 15"})
    assert response.status_code == 200
    assert response.json()["cached"] is True


@pytest.mark.parametrize("payload", [None, {}, {"query": ""}, {"query": "a"}, {"query": "  "}, {"query": "!!!"}, {"query": "x" * 161}, {"query": 42}, {"query": ["iPhone"]}])
def test_invalid_queries_do_not_trigger_marketplace_requests(payload):
    runtime = fake_runtime()
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json=payload)
    assert response.status_code == 422
    runtime.service.search.assert_not_awaited()


def test_source_failures_are_not_mislabeled_as_no_products():
    runtime = fake_runtime([
        SearchResult("wildberries", [], SourceStatus.BLOCKED, "CAPTCHA"),
        SearchResult("ozon", [], SourceStatus.ERROR, "Сбой сети"),
        SearchResult("aliexpress", [], SourceStatus.TIMEOUT),
        SearchResult("avito", [], SourceStatus.EMPTY),
    ])
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "пылесос"})
    assert response.status_code == 200
    body = response.json()
    assert body["products"] == []
    assert [source["status"] for source in body["sources"]] == ["blocked", "error", "timeout", "empty"]


def test_internal_error_does_not_expose_exception_details_and_releases_capacity():
    runtime = fake_runtime()
    runtime.service.search.side_effect = RuntimeError("BOT_TOKEN=secret internal network URL")
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "iPhone 15"})
        assert response.status_code == 503
        assert "secret" not in response.text
        assert "internal network" not in response.text
        runtime.service.search.side_effect = None
        second = client.post("/api/search", json={"query": "iPhone 16"})
    assert second.status_code != 503


def test_product_names_remain_data_not_html():
    title = '<img src=x onerror="alert(1)"> iPhone 15'
    runtime = fake_runtime([SearchResult("avito", [Product("avito", title, "https://www.avito.ru/lipetsk/telefony/item_123", Decimal("100"))], SourceStatus.OK)])
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "iPhone 15"})
        page = client.get("/")
    assert "application/json" in response.headers["content-type"]
    assert response.json()["products"][0]["title"] == title
    assert title not in page.text


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "http://www.avito.ru/lipetsk/item_123",
    "https://www.avito.ru.evil.example/item_123",
    "https://www.avito.ru@evil.example/item_123",
    "https://user:pass@www.avito.ru/lipetsk/item_123",
    "https://www.avito.ru:8080/lipetsk/item_123",
])
def test_untrusted_product_links_are_not_returned(url):
    runtime = fake_runtime([SearchResult("avito", [Product("avito", "iPhone 15", url, Decimal("100"))], SourceStatus.OK)])
    with TestClient(create_app(settings(), runtime=runtime)) as client:
        response = client.post("/api/search", json={"query": "iPhone 15"})
    assert response.status_code == 200
    assert response.json()["products"] == []
    assert response.json()["sources"][0]["count"] == 0


async def test_concurrent_request_limit_rejects_excess_without_waiting():
    started, release = asyncio.Event(), asyncio.Event()
    runtime = fake_runtime()

    async def search(query):
        started.set()
        await release.wait()
        return [], False

    runtime.service.search = search
    app = create_app(settings(max_concurrent_searches=1), runtime=runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/search", json={"query": "iPhone 15"}))
            await asyncio.wait_for(started.wait(), 1)
            second = await client.post("/api/search", json={"query": "iPhone 16"})
            assert second.status_code == 429
            release.set()
            assert (await first).status_code == 200
