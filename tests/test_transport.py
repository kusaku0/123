import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pricebot.models import SourceStatus
from pricebot.transport import BrowserTransport, FetchError, MAX_BYTES


class Page:
    def __init__(self, status=200, responses=()):
        self.url = "about:blank"
        self.status = status
        self.responses = responses
        self.listeners = {}
        self.gotos = []
        self.front = 0

    def is_closed(self):
        return False

    def on(self, event, handler):
        self.listeners[event] = handler

    def remove_listener(self, event, handler):
        if self.listeners.get(event) == handler:
            self.listeners.pop(event)

    async def goto(self, url, **kwargs):
        self.url = url
        self.gotos.append(url)
        for response in self.responses:
            if "response" in self.listeners:
                self.listeners["response"](response)
        return SimpleNamespace(status=self.status)

    async def wait_for_selector(self, *args, **kwargs):
        pass

    async def wait_for_timeout(self, *args):
        pass

    async def content(self):
        return "<html><h1>Search page</h1></html>"

    async def bring_to_front(self):
        self.front += 1


class Context:
    def __init__(self, page):
        self.page = page
        self.new_page = AsyncMock(return_value=page)
        self.route = AsyncMock()
        self.close = AsyncMock()
        self.listeners = {}
        self.browser = SimpleNamespace(is_connected=lambda: True)

    def on(self, event, handler):
        self.listeners[event] = handler


def fixture(tmp_path, *, local=True, page=None):
    page = page or Page()
    context = Context(page)
    browser = SimpleNamespace(
        new_context=AsyncMock(return_value=context), is_connected=lambda: True,
        close=AsyncMock(),
    )
    playwright = SimpleNamespace(chromium=SimpleNamespace(
        launch=AsyncMock(return_value=browser),
        launch_persistent_context=AsyncMock(return_value=context),
    ), stop=AsyncMock())
    transport = BrowserTransport(local_mode=local, profile_dir=tmp_path / "dedicated", timeout=0.02)
    transport._playwright = playwright
    return transport, page, context, playwright, browser


async def test_local_profile_is_headed_persistent_and_does_not_block_resources(tmp_path):
    transport, page, context, playwright, _ = fixture(tmp_path)
    await transport.fetch("https://www.ozon.ru/search/?text=phone")
    await transport.fetch("https://www.ozon.ru/search/?text=phone2")
    launch = playwright.chromium.launch_persistent_context
    launch.assert_awaited_once()
    assert launch.await_args.kwargs["headless"] is False
    assert launch.await_args.args[0] == str(tmp_path / "dedicated")
    context.new_page.assert_awaited_once()
    context.route.assert_not_awaited()
    context.close.assert_not_awaited()
    await transport.close()
    context.close.assert_awaited_once()
    playwright.stop.assert_awaited_once()


async def test_shared_local_context_launches_once_for_concurrent_sources(tmp_path):
    transport, _, _, playwright, _ = fixture(tmp_path)
    await asyncio.gather(transport.fetch("https://www.ozon.ru/search/?text=x"),
                         transport.fetch("https://www.avito.ru/lipetsk?q=x"))
    playwright.chromium.launch_persistent_context.assert_awaited_once()
    await transport.close()


async def test_default_headless_context_is_closed_after_fetch(tmp_path):
    transport, _, context, playwright, browser = fixture(tmp_path, local=False)
    await transport.fetch("https://www.avito.ru/lipetsk?q=x")
    assert playwright.chromium.launch.await_args.kwargs["headless"] is True
    context.route.assert_awaited_once()
    context.close.assert_awaited_once()
    await transport.close()
    browser.close.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_blocked_http_codes_are_distinguished_and_local_page_preserved(tmp_path, status):
    transport, page, context, _, _ = fixture(tmp_path, page=Page(status))
    with pytest.raises(FetchError) as caught:
        await transport.fetch("https://www.ozon.ru/search/?text=x")
    assert caught.value.status == SourceStatus.BLOCKED
    assert str(status) in caught.value.message
    context.close.assert_not_awaited()
    await transport.open_for_review("https://www.ozon.ru/search/?text=x")
    assert len(page.gotos) == 1  # No refresh that would erase the challenge.
    assert page.front == 1
    await transport.close()


async def test_manual_review_can_open_new_403_page(tmp_path):
    transport, page, _, _, _ = fixture(tmp_path, page=Page(403))
    await transport.open_for_review("https://www.avito.ru/lipetsk?q=x")
    assert len(page.gotos) == 1
    assert page.front == 1
    await transport.close()


async def test_manual_review_refused_outside_local_mode(tmp_path):
    transport, _, _, playwright, _ = fixture(tmp_path, local=False)
    with pytest.raises(FetchError, match="локальном"):
        await transport.open_for_review("https://www.avito.ru/lipetsk?q=x")
    playwright.chromium.launch.assert_not_awaited()


@pytest.mark.parametrize("url", ["http://ozon.ru/", "https://ozon.ru.evil.test/", "https://user@ozon.ru/", "https://ozon.ru:123/", "https://example.com/"])
async def test_unapproved_review_urls_never_launch_browser(tmp_path, url):
    transport, _, _, playwright, _ = fixture(tmp_path)
    with pytest.raises(FetchError):
        await transport.open_for_review(url)
    playwright.chromium.launch_persistent_context.assert_not_awaited()


def response(data=None, *, query="iphone 17", host="search.wb.ru", status=200, length=None, currency="rub"):
    body = json.dumps(data if data is not None else {"products": []}).encode()
    return SimpleNamespace(
        url=f"https://{host}/exactmatch/ru/common/v99/search?query={query}" + (f"&curr={currency}" if currency else ""),
        status=status, headers={"content-length": str(length if length is not None else len(body))},
        body=AsyncMock(return_value=body),
    )


async def test_wb_captures_matching_public_search_response_not_version_guess(tmp_path):
    data = {"data": {"products": [{"id": 123}]}}
    matching = response(data)
    wrong_query = response({"products": [{"id": 999}]}, query="different")
    wrong_host = response({"products": [{"id": 888}]}, host="evil.test")
    transport, page, _, _, _ = fixture(tmp_path, page=Page(responses=[wrong_query, wrong_host, matching]))
    assert await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017") == data
    wrong_query.body.assert_not_awaited()
    wrong_host.body.assert_not_awaited()
    assert not page.listeners
    assert not transport._capture_tasks
    await transport.close()


async def test_wb_response_is_not_reused_for_next_query(tmp_path):
    transport, page, _, _, _ = fixture(tmp_path, page=Page(responses=[response()]))
    await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017")
    page.responses = []
    with pytest.raises(FetchError, match="подтверждённые"):
        await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=new%20query")
    await transport.close()


async def test_wb_capture_rejects_large_body_before_loading(tmp_path):
    big = response(length=MAX_BYTES + 1)
    transport, _, _, _, _ = fixture(tmp_path, page=Page(responses=[big]))
    with pytest.raises(FetchError):
        await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017")
    big.body.assert_not_awaited()
    await transport.close()


@pytest.mark.parametrize("currency", ["usd", ""])
async def test_wb_capture_requires_explicit_ruble_request(tmp_path, currency):
    wrong_currency = response(currency=currency)
    transport, _, _, _, _ = fixture(tmp_path, page=Page(responses=[wrong_currency]))
    with pytest.raises(FetchError):
        await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017")
    wrong_currency.body.assert_not_awaited()
    await transport.close()


async def test_wb_capture_bounds_and_cleans_unfinished_response_tasks(tmp_path):
    gate = asyncio.Event()
    responses = [response() for _ in range(20)]
    for item in responses:
        item.body = AsyncMock(side_effect=gate.wait)
    transport, _, _, _, _ = fixture(tmp_path, page=Page(responses=responses))
    with pytest.raises(FetchError):
        await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017")
    assert sum(item.body.await_count for item in responses) == 12
    assert not transport._capture_tasks
    await transport.close()


async def test_wb_api_rate_limit_is_preserved_not_reported_as_empty_catalog(tmp_path):
    transport, _, _, _, _ = fixture(tmp_path, page=Page(responses=[response(status=429)]))
    with pytest.raises(FetchError, match="429") as caught:
        await transport.fetch_wildberries("https://www.wildberries.ru/catalog/0/search.aspx?search=iphone%2017")
    assert caught.value.status == SourceStatus.BLOCKED
    await transport.close()
