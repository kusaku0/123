import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pricebot.app import BotHandlers
from pricebot.config import Settings
from pricebot.models import SearchResult, SourceStatus


def update(text="iPhone 15", user_id=42):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=message)


def handlers(settings=None, service=None):
    service = service or SimpleNamespace(search=AsyncMock(return_value=([
        SearchResult("wildberries", [], SourceStatus.BLOCKED, "Доступ ограничен."),
        SearchResult("avito", [], SourceStatus.EMPTY),
    ], False)))
    return BotHandlers(settings or Settings("123456:fake"), SimpleNamespace(service=service))


async def test_search_returns_partial_status_and_links_without_token():
    h = handlers()
    u = update("/search iPhone 15")
    await h.search(u, SimpleNamespace(args=["iPhone", "15"]))
    h.runtime.service.search.assert_awaited_once_with("iPhone 15")
    assert u.effective_message.reply_text.await_count == 2
    call = u.effective_message.reply_text.await_args
    assert "WB: доступ ограничен" in call.args[0]
    assert "/lipetsk?" in call.args[0]
    assert h.settings.bot_token not in call.args[0]
    assert call.kwargs["parse_mode"] == "HTML"


async def test_whitelist_rejects_before_search():
    h = handlers(Settings("123456:fake", allowed_user_ids=frozenset({1})))
    u = update(user_id=42)
    await h.search(u, SimpleNamespace(args=[]))
    h.runtime.service.search.assert_not_awaited()
    assert "Доступ" in u.effective_message.reply_text.await_args.args[0]


async def test_rate_limit_rejects_repeated_user_request():
    h = handlers()
    await h.search(update(), SimpleNamespace(args=[]))
    u = update()
    await h.search(u, SimpleNamespace(args=[]))
    assert "Подождите" in u.effective_message.reply_text.await_args.args[0]
    assert h.runtime.service.search.await_count == 1


async def test_invalid_query_does_not_call_service():
    h = handlers()
    for text in ("/search", "a", "x" * 161, "!!!"):
        await h.search(update(text), SimpleNamespace(args=[]))
    h.runtime.service.search.assert_not_awaited()


async def test_only_one_active_search_per_user():
    started, release = asyncio.Event(), asyncio.Event()

    async def search(query):
        started.set()
        await release.wait()
        return [], False

    h = handlers(service=SimpleNamespace(search=search))
    task = asyncio.create_task(h.search(update(), SimpleNamespace(args=[])))
    await started.wait()
    u = update()
    await h.search(u, SimpleNamespace(args=[]))
    assert "ещё выполняется" in u.effective_message.reply_text.await_args.args[0]
    release.set()
    await task
    assert h._active == 0
    assert not h._active_users


async def test_failure_releases_search_capacity():
    h = handlers(service=SimpleNamespace(search=AsyncMock(side_effect=RuntimeError("private"))))
    with pytest.raises(RuntimeError):
        await h.search(update(), SimpleNamespace(args=[]))
    assert h._active == 0
    assert not h._active_users
