import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from decimal import Decimal
from typing import Protocol

from pricebot.models import Product, SearchResult, SourceStatus

logger = logging.getLogger(__name__)
STOP_WORDS = {"для", "или", "купить", "цена", "the", "and"}
ACCESSORIES = re.compile(r"\b(чехол\w*|кабел\w*|стекл\w*|пленк\w*|ремеш\w*|наклей\w*|муляж\w*|макет\w*|коробк\w*|заряд\w*|адаптер\w*|запчаст\w*|диспле\w*|экран\w*|аккумулятор\w*|материнск\w*|ремонт\w*|аренд\w*|рассроч\w*|ежемесяч\w*|case|cover|cable|protector|repair|rental)\b")
DEVICES = re.compile(r"\b(iphone|айфон|ipad|айпад|samsung|самсунг|galaxy|pixel|xiaomi|redmi|poco|ноутбук\w*|macbook)\b")


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


def relevant(query: str, title: str) -> bool:
    query, title = normalize(query), normalize(title)
    def terms_for(text: str) -> list[str]:
        # Listings write the same capacity as 128GB, 128 GB or 128 ГБ.
        aliases = {"гб": "gb", "тб": "tb", "мб": "mb"}
        text = re.sub(r"(\d+)\s*(gb|гб|tb|тб|mb|мб)\b", lambda m: m[1] + " " + aliases.get(m[2], m[2]), text)
        return re.findall(r"[^\W_]+", text)
    terms = [x for x in terms_for(query) if x not in STOP_WORDS]
    title_terms = set(terms_for(title))
    # Numeric model numbers must match whole tokens: iPhone 15 != iPhone 150.
    if not terms or not all(term in title_terms or (not term.isdigit() and len(term) > 3 and any(t.startswith(term) for t in title_terms)) for term in terms):
        return False
    if DEVICES.search(query) and not ACCESSORIES.search(query) and ACCESSORIES.search(title):
        return False
    return True


class Provider(Protocol):
    source: str
    async def search(self, query: str, limit: int = 5) -> SearchResult: ...


class SearchService:
    def __init__(self, providers: list[Provider], timeout: float = 35, ttl: float = 180, max_cache_entries: int = 128):
        self.providers = providers
        self.timeout = timeout
        self.ttl = ttl
        self.max_cache_entries = max_cache_entries
        self._cache: OrderedDict[str, tuple[float, list[SearchResult]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}

    async def _one(self, provider: Provider, query: str) -> SearchResult:
        try:
            result = await asyncio.wait_for(provider.search(query, limit=40), self.timeout)
        except asyncio.TimeoutError:
            return SearchResult(provider.source, [], SourceStatus.TIMEOUT, "Превышено время ожидания.")
        except Exception as exc:
            logger.warning("Search provider %s failed (%s)", provider.source, type(exc).__name__)
            return SearchResult(provider.source, [], SourceStatus.ERROR, "Не удалось получить результаты площадки.")
        if result.status != SourceStatus.OK:
            return result
        seen = set()
        products = []
        for item in sorted(result.items, key=lambda p: p.price if p.price.is_finite() else Decimal("Infinity")):
            if item.currency == "RUB" and item.price.is_finite() and item.price > 0 and item.url not in seen and relevant(query, item.title):
                products.append(item)
                seen.add(item.url)
        if not products:
            return SearchResult(provider.source, [], SourceStatus.EMPTY, "В полученной выдаче нет совпадений по всем словам запроса. Попробуйте другое название.")
        return SearchResult(result.source, products[:3], SourceStatus.OK, result.message)

    async def _collect(self, query: str) -> list[SearchResult]:
        return list(await asyncio.gather(*(self._one(provider, query) for provider in self.providers)))

    async def search(self, query: str) -> tuple[list[SearchResult], bool]:
        key = normalize(query)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl:
            self._cache.move_to_end(key)
            return cached[1], True
        if key not in self._inflight:
            task = asyncio.create_task(self._collect(query))
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._inflight.pop(key, None))
        results = await asyncio.shield(self._inflight[key])
        # Do not hide recoveries for minutes after complete source failure.
        if any(r.status in {SourceStatus.OK, SourceStatus.EMPTY} for r in results):
            self._cache[key] = time.monotonic(), results
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_cache_entries:
                self._cache.popitem(last=False)
        return results, False

    async def close(self):
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def invalidate(self, query: str):
        self._cache.pop(normalize(query), None)


async def resolve_wb_destination(client, latitude: float, longitude: float) -> str | None:
    """Only use a destination actually returned for the requested coordinates."""
    try:
        response = await client.get(
            "https://user-geo-data.wildberries.ru/get-geo-info",
            params={"latitude": latitude, "longitude": longitude}, timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        geo = data.get("geoInfo", data) if isinstance(data, dict) else {}
        destinations = geo.get("destinations", [])
        if isinstance(destinations, list) and destinations:
            value = destinations[-1]
            if isinstance(value, int) and not isinstance(value, bool):
                return str(value)
            if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                return value
    except Exception as exc:
        # No fallback is called a verified Lipetsk destination.
        logger.info("WB geolocation unavailable (%s)", type(exc).__name__)
    return None
