"""Ordinary Chromium browsing with an optional dedicated local review profile."""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from pricebot.models import SourceStatus

ALLOWED_HOSTS = {"www.ozon.ru", "ozon.ru", "www.avito.ru", "avito.ru", "www.aliexpress.com", "aliexpress.com", "aliexpress.ru", "www.aliexpress.ru", "www.wildberries.ru", "wildberries.ru"}
MAX_BYTES = 8 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, status: SourceStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _validate_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and not parsed.username and not parsed.password and parsed.port in (None, 443)):
            return parsed.hostname.removeprefix("www.")
    except ValueError:
        pass
    raise FetchError(SourceStatus.ERROR, "Недопустимый адрес площадки.")


def _http_error(status: int) -> FetchError | None:
    if status == 429:
        return FetchError(SourceStatus.BLOCKED, "Площадка ограничила частоту запросов (HTTP 429). Подождите перед повторным поиском.")
    if status == 401:
        return FetchError(SourceStatus.BLOCKED, "Площадка отклонила запрос (HTTP 401). Может требоваться вход или проверка доступа.")
    if status in {403, 498}:
        return FetchError(SourceStatus.BLOCKED, f"Площадка отклонила запрос (HTTP {status}). Это может быть проверка доступа.")
    if status >= 400:
        return FetchError(SourceStatus.ERROR, f"Площадка вернула ошибку HTTP {status} при загрузке поиска.")
    return None


class BrowserTransport:
    def __init__(self, executable: str | None = None, timeout: float = 30, concurrency: int = 6,
                 *, local_mode: bool = False, profile_dir: Path | str | None = None):
        self.executable = executable
        self.timeout = timeout
        self.local_mode = local_mode
        self.profile_dir = Path(profile_dir or ".pricebot/browser").resolve()
        self._playwright = None
        self._browser = None
        self._persistent_context = None
        self._context_closed = True
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(concurrency)
        self._pages = {}
        self._page_locks = {}
        self._capture_tasks = set()

    def _launch_options(self):
        proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
        return {"executable_path": self.executable,
                "proxy": {"server": proxy_url} if proxy_url else None}

    async def _start_playwright(self):
        if self._playwright is None:
            self._playwright = await async_playwright().start()

    async def _get_persistent_context(self):
        async with self._lock:
            if self._persistent_context is not None and not self._context_closed:
                return self._persistent_context
            await self._start_playwright()
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=False, locale="ru-RU", timezone_id="Europe/Moscow",
                **self._launch_options(),
            )
            self._persistent_context = context
            self._context_closed = False
            self._pages.clear()
            context.on("close", lambda *_: setattr(self, "_context_closed", True))
            return context

    async def _get_browser(self):
        # Retained for the local --check command and existing integrations.
        if self.local_mode:
            return (await self._get_persistent_context()).browser
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            await self._start_playwright()
            self._browser = await self._playwright.chromium.launch(headless=True, **self._launch_options())
            return self._browser

    async def _page(self, key: str):
        context = await self._get_persistent_context()
        page = self._pages.get(key)
        if page is None or page.is_closed():
            page = await context.new_page()
            self._pages[key] = page
        return page

    async def open_for_review(self, url: str) -> None:
        key = _validate_url(url)
        if not self.local_mode:
            raise FetchError(SourceStatus.ERROR, "Окно проверки доступно только при локальном запуске на вашем компьютере.")
        async with self._page_locks.setdefault(key, asyncio.Lock()):
            try:
                page = await self._page(key)
                # Preserve a current challenge/verification tab instead of erasing it.
                if page.url in {"", "about:blank"}:
                    await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                await page.bring_to_front()
            except BrowserTimeout as exc:
                raise FetchError(SourceStatus.TIMEOUT, "Страница проверки долго загружается. Посмотрите открытое окно браузера.") from exc
            except BrowserError as exc:
                raise FetchError(SourceStatus.ERROR, "Не удалось открыть локальное окно браузера. Перезапустите программу и повторите.") from exc

    async def fetch(self, url: str) -> str:
        html, _ = await self._load(url)
        return html

    async def fetch_wildberries(self, url: str) -> dict:
        if _validate_url(url) != "wildberries.ru":
            raise FetchError(SourceStatus.ERROR, "Для каталога WB нужен адрес Wildberries.")
        _, data = await self._load(url, capture_wb=True)
        if data is None:
            raise FetchError(SourceStatus.ERROR, "Страница WB не передала подтверждённые данные каталога. Проверьте открытое окно и повторите поиск.")
        return data

    async def _load(self, url: str, capture_wb: bool = False):
        key = _validate_url(url)
        async with self._slots:
            # A persistent tab is reused only by one request at a time.
            lock = self._page_locks.setdefault(key, asyncio.Lock()) if self.local_mode else asyncio.Lock()
            async with lock:
                context = None
                page = None
                handler = None
                tasks = set()
                data = None
                captured_error = None
                capture_ready = asyncio.Event()
                capture_count = 0
                query = parse_qs(urlsplit(url).query).get("search", [""])[0]

                async def capture(response):
                    nonlocal data, captured_error
                    try:
                        error = _http_error(response.status)
                        if error:
                            captured_error = error
                            capture_ready.set()
                            return
                        length = response.headers.get("content-length", "")
                        if length.isdigit() and int(length) > MAX_BYTES:
                            return
                        body = await response.body()
                        if len(body) > MAX_BYTES:
                            return
                        parsed = json.loads(body)
                        rows = parsed.get("products") if isinstance(parsed, dict) else None
                        if rows is None and isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                            rows = parsed["data"].get("products")
                        if isinstance(rows, list):
                            data = parsed
                            capture_ready.set()
                    except (BrowserError, ValueError, TypeError):
                        pass

                def on_response(response):
                    nonlocal capture_count
                    parsed = urlsplit(response.url)
                    params = parse_qs(parsed.query)
                    actual = params.get("query", [""])[0]
                    if (capture_count >= 12 or not query or parsed.scheme != "https"
                            or parsed.hostname != "search.wb.ru" or not parsed.path.startswith("/exactmatch/")
                            or params.get("curr", [""])[0].casefold() != "rub"
                            or " ".join(actual.casefold().split()) != " ".join(query.casefold().split())):
                        return
                    capture_count += 1
                    task = asyncio.create_task(capture(response))
                    tasks.add(task)
                    self._capture_tasks.add(task)

                try:
                    if self.local_mode:
                        page = await self._page(key)
                    else:
                        browser = await self._get_browser()
                        context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
                        page = await context.new_page()
                        async def route_handler(route):
                            if route.request.resource_type in {"image", "media", "font"}:
                                await route.abort()
                            else:
                                await route.continue_()
                        await context.route("**/*", route_handler)
                    if capture_wb:
                        handler = on_response
                        page.on("response", handler)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                    error = _http_error(response.status) if response else None
                    if error:
                        raise error
                    if capture_wb:
                        try:
                            await asyncio.wait_for(capture_ready.wait(), min(8, self.timeout))
                        except asyncio.TimeoutError:
                            pass
                        if data is None and captured_error is not None:
                            raise captured_error
                    else:
                        try:
                            await page.wait_for_selector('[data-marker="item"], a[href*="/product/"], a[href*="/item/"]', timeout=6000)
                        except BrowserTimeout:
                            pass
                        await page.wait_for_timeout(750)
                    html = await page.content()
                    if len(html.encode("utf-8")) > MAX_BYTES:
                        raise FetchError(SourceStatus.ERROR, "Страница поиска слишком большая.")
                    return html, data
                except FetchError:
                    raise
                except BrowserTimeout as exc:
                    raise FetchError(SourceStatus.TIMEOUT, "Площадка не ответила вовремя.") from exc
                except BrowserError as exc:
                    raise FetchError(SourceStatus.ERROR, "Не удалось открыть площадку в браузере. Проверьте открытое окно или перезапустите программу.") from exc
                finally:
                    if page is not None and handler is not None:
                        page.remove_listener("response", handler)
                    pending = list(tasks)
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    self._capture_tasks.difference_update(pending)
                    if context is not None:
                        await context.close()

    async def close(self):
        tasks = list(self._capture_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._persistent_context is not None and not self._context_closed:
            await self._persistent_context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._persistent_context = None
        self._context_closed = True
        self._browser = None
        self._playwright = None
        self._pages.clear()
