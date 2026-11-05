"""Marketplace lifecycle shared by the website and optional Telegram adapter."""
import httpx

from pricebot.config import Settings
from pricebot.providers.pages import PageProvider
from pricebot.providers.wildberries import WildberriesProvider
from pricebot.search import SearchService, resolve_wb_destination
from pricebot.transport import BrowserTransport


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15), follow_redirects=True,
            limits=httpx.Limits(max_connections=8),
            headers={"Accept": "application/json", "User-Agent": "LipetskPriceSearch/0.2"},
        )
        self.browser = BrowserTransport(
            settings.browser_executable, timeout=max(1, settings.source_timeout - 1),
            local_mode=settings.local_browser_mode, profile_dir=settings.browser_profile_dir,
        )
        self.service: SearchService | None = None

    async def start(self):
        destination = self.settings.wb_dest
        if not destination and not self.settings.local_browser_mode:
            destination = await resolve_wb_destination(self.client, self.settings.wb_latitude, self.settings.wb_longitude)
        wb = WildberriesProvider(
            self.client, dest=destination or "-1257786", region_label=self.settings.city if destination else None,
            browser=self.browser, prefer_browser=self.settings.local_browser_mode,
        )
        self.service = SearchService(
            [wb, *(PageProvider(source, self.browser, city=self.settings.avito_city) for source in ("ozon", "aliexpress", "avito"))],
            timeout=self.settings.source_timeout, ttl=self.settings.cache_ttl,
            max_cache_entries=self.settings.max_cache_entries,
        )

    async def close(self):
        if self.service:
            await self.service.close()
        await self.browser.close()
        await self.client.aclose()
