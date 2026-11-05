"""Normal Chromium browsing; no stealth, CAPTCHA solvers or rotating proxies."""
import asyncio
import os
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from pricebot.models import SourceStatus

ALLOWED_HOSTS = {"www.ozon.ru", "ozon.ru", "www.avito.ru", "avito.ru", "www.aliexpress.com", "aliexpress.com", "aliexpress.ru", "www.aliexpress.ru"}


class FetchError(Exception):
    def __init__(self, status: SourceStatus, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class BrowserTransport:
    def __init__(self, executable: str | None = None, timeout: float = 30, concurrency: int = 6):
        self.executable = executable
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(concurrency)

    async def _get_browser(self):
        async with self._lock:
            if self._browser and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            # Honor managed hosting proxy and its trust settings; no user proxy required.
            proxy_url = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
            proxy = {"server": proxy_url} if proxy_url else None
            self._browser = await self._playwright.chromium.launch(
                executable_path=self.executable, headless=True, proxy=proxy,
            )
            return self._browser

    async def fetch(self, url: str) -> str:
        if urlsplit(url).hostname not in ALLOWED_HOSTS:
            raise FetchError(SourceStatus.ERROR, "Недопустимый адрес площадки.")
        async with self._slots:
            context = None
            try:
                browser = await self._get_browser()
                context = await browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow")
                page = await context.new_page()
                # Images/media aren't needed for price extraction.
                async def route_handler(route):
                    if route.request.resource_type in {"image", "media", "font"}:
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route("**/*", route_handler)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                if response and response.status in {401, 403, 429}:
                    raise FetchError(SourceStatus.BLOCKED, "Площадка ограничила доступ. Попробуйте позже или откройте поиск на сайте.")
                if response and response.status >= 400:
                    raise FetchError(SourceStatus.ERROR, "Площадка вернула ошибку при загрузке поиска.")
                # Bounded wait for rendered search results, including API populated cards.
                try:
                    await page.wait_for_selector(
                        '[data-marker="item"], '
                        'a[href*="/product/"], a[href*="/item/"]', timeout=6000,
                    )
                except BrowserTimeout:
                    pass
                # A short stabilization period avoids reading a partially rendered card.
                await page.wait_for_timeout(750)
                html = await page.content()
                if len(html) > 8_000_000:
                    raise FetchError(SourceStatus.ERROR, "Страница поиска слишком большая.")
                return html
            except FetchError:
                raise
            except BrowserTimeout as exc:
                raise FetchError(SourceStatus.TIMEOUT, "Площадка не ответила вовремя.") from exc
            except BrowserError as exc:
                raise FetchError(SourceStatus.ERROR, "Не удалось открыть площадку в Chromium; проверьте установку браузера и доступ из сети сервера.") from exc
            finally:
                if context:
                    await context.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
