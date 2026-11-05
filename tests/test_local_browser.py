from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pricebot.config import Settings
from pricebot.models import SearchResult, SourceStatus
from pricebot.web import create_app


def runtime():
    return SimpleNamespace(
        start=AsyncMock(), close=AsyncMock(), browser=SimpleNamespace(open_for_review=AsyncMock()),
        service=SimpleNamespace(search=AsyncMock(return_value=([
            SearchResult("avito", [], SourceStatus.BLOCKED, "HTTP 403")
        ], False)), invalidate=Mock()),
    )


async def test_loopback_can_open_only_generated_marketplace_url():
    fake = runtime()
    app = create_app(Settings(local_browser_mode=True), fake)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
            reply = await client.post("/api/browser/open", json={"query": "iphone 17", "source": "avito"}, headers={"Origin": "http://127.0.0.1:8000"})
            assert reply.status_code == 200
            assert reply.json()["status"] == "opened"
            fake.browser.open_for_review.assert_awaited_once_with("https://www.avito.ru/lipetsk?q=iphone%2017&s=1")
            bad = await client.post("/api/browser/open", json={"query": "iphone 17", "source": "https://evil.example"})
            assert bad.status_code == 422


@pytest.mark.parametrize("enabled,client_host,host,origin", [
    (False, "127.0.0.1", "127.0.0.1:8000", None),
    (True, "192.0.2.1", "127.0.0.1:8000", None),
    (True, "127.0.0.1", "evil.example", "http://evil.example"),
    (True, "127.0.0.1", "localhost:8000", "http://evil.example"),
    (True, "127.0.0.1", "localhost:8000", "null"),
])
async def test_remote_websites_and_server_mode_cannot_launch_local_windows(enabled, client_host, host, origin):
    fake = runtime()
    app = create_app(Settings(local_browser_mode=enabled), fake)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=(client_host, 1234)), base_url=f"http://{host}") as client:
            reply = await client.post("/api/browser/open", json={"query": "iphone 17", "source": "avito"}, headers={"Origin": origin} if origin else {})
            assert reply.status_code == 403
            fake.browser.open_for_review.assert_not_awaited()


async def test_explicit_retry_invalidates_cached_failure_and_reports_local_mode():
    fake = runtime()
    app = create_app(Settings(local_browser_mode=True), fake)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8000") as client:
            reply = await client.post("/api/search", json={"query": "iphone 17", "refresh": True})
            assert reply.status_code == 200
            fake.service.invalidate.assert_called_once_with("iphone 17")
            assert reply.json()["sources"][0]["can_open_browser"] is True


@pytest.mark.parametrize("value,expected", [("true", True), ("false", False), ("1", True), ("0", False)])
def test_local_mode_is_explicit_configuration(monkeypatch, value, expected):
    monkeypatch.setenv("LOCAL_BROWSER_MODE", value)
    assert Settings.from_env(require_token=False).local_browser_mode == expected


def test_invalid_local_mode_rejected(monkeypatch):
    monkeypatch.setenv("LOCAL_BROWSER_MODE", "maybe")
    with pytest.raises(ValueError, match="LOCAL_BROWSER_MODE"):
        Settings.from_env(require_token=False)
