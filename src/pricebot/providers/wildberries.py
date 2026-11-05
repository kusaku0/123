"""Best-effort access to Wildberries' public storefront search.

This endpoint is undocumented and can change or refuse data-centre traffic.
No seller credentials, wallet discount, proxy, or anti-bot bypass are used.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from pricebot.models import Product, SearchResult, SourceStatus
from pricebot.transport import FetchError

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SEARCH_BASE = "https://search.wb.ru/exactmatch/ru/common"


class WildberriesProvider:
    source = "wildberries"

    def __init__(
        self,
        client: httpx.AsyncClient,
        dest: str = "-1257786",
        api_version: str = "v18",
        region_label: str | None = None,
        browser=None,
        prefer_browser: bool = False,
    ) -> None:
        self.client = client
        self.dest = dest
        self.region_label = region_label
        self.browser = browser
        self.prefer_browser = prefer_browser
        if not api_version.startswith("v") or not api_version[1:].isdigit():
            raise ValueError("WB API version must look like v18")
        self.api_version = api_version

    async def search(self, query: str, limit: int = 5) -> SearchResult:
        if not query.strip() or limit < 1:
            return SearchResult(self.source, [], SourceStatus.EMPTY)
        if self.prefer_browser:
            return await self._browser_search(query, limit)
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": self.dest,
            "lang": "ru",
            "page": "1",
            "query": query,
            "resultset": "catalog",
            "sort": "priceup",
            "spp": "0",
            "suppressSpellcheck": "false",
        }
        versions = [self.api_version]
        if self.api_version != "v9":
            versions.append("v9")
        try:
            for index, version in enumerate(versions):
                async with self.client.stream(
                    "GET", f"{_SEARCH_BASE}/{version}/search", params=params
                ) as response:
                    if response.status_code == 404 and index + 1 < len(versions):
                        continue
                    if response.status_code in (401, 403, 429, 498):
                        message = {
                            429: "Wildberries ограничил частоту запросов. Подождите перед повторным поиском.",
                            401: "Поиск Wildberries отклонил запрос. Проверьте доступ к площадке в браузере.",
                            498: "Поиск Wildberries отклонил запрос. Проверьте доступ к площадке в браузере.",
                            403: "Wildberries запретил этот запрос. Откройте поиск на сайте для проверки доступа.",
                        }[response.status_code]
                        return SearchResult(
                            self.source,
                            [],
                            SourceStatus.BLOCKED,
                            message,
                        )
                    response.raise_for_status()
                    chunks = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(chunks) + len(chunk) > _MAX_RESPONSE_BYTES:
                            return self._error("Ответ Wildberries слишком большой.")
                        chunks.extend(chunk)
                    try:
                        data = json.loads(chunks)
                    except (ValueError, UnicodeDecodeError):
                        sample = bytes(chunks[:4096]).lower()
                        if any(word in sample for word in (b"captcha", b"access denied", b"robot")):
                            return SearchResult(
                                self.source,
                                [],
                                SourceStatus.BLOCKED,
                                "Wildberries запросил проверку доступа.",
                            )
                        return self._error("Wildberries вернул неожиданный формат ответа.")
                    return self._from_data(data, limit, region_verified=True)
        except httpx.TimeoutException:
            return SearchResult(
                self.source, [], SourceStatus.TIMEOUT, "Wildberries не ответил вовремя."
            )
        except (httpx.HTTPError, OSError):
            return self._error("Не удалось получить каталог Wildberries.")
        return self._error("Каталог Wildberries недоступен.")

    async def _browser_search(self, query: str, limit: int) -> SearchResult:
        if self.browser is None:
            return self._error("Браузер для поиска Wildberries ещё не запущен.")
        url = "https://www.wildberries.ru/catalog/0/search.aspx?" + urlencode({"search": query, "sort": "priceup"})
        try:
            # Read only catalog responses actually emitted by this public page.
            # No direct undocumented endpoint retries or session extraction.
            data = await self.browser.fetch_wildberries(url)
            return self._from_data(data, limit, region_verified=False)
        except FetchError as error:
            return SearchResult(self.source, [], error.status, error.message)
        except (ValueError, TypeError, KeyError):
            return self._error("Не удалось прочитать каталог на странице Wildberries.")

    def _from_data(self, data: object, limit: int, *, region_verified: bool) -> SearchResult:
        products = _product_rows(data)
        if products is None:
            return self._error("Формат каталога Wildberries изменился.")
        found: dict[str, Product] = {}
        for row in products:
            item = _parse_product(row)
            if item is not None:
                previous = found.get(item.url)
                if previous is None or item.price < previous.price:
                    found[item.url] = item
        items = sorted(found.values(), key=lambda item: item.price)[:limit]
        if products and not items and not any(_has_price_schema(row) for row in products):
            return self._error("Каталог Wildberries загрузился, но формат цен не распознан. Это не означает отсутствие товара.")
        return SearchResult(
            self.source, items, SourceStatus.OK if items else SourceStatus.EMPTY,
            f"Цена товара без доставки. Регион WB: {self.region_label}."
            if region_verified and self.region_label
            else "Цена без доставки. Регион WB не подтверждён; проверьте цену для своего пункта выдачи.",
        )

    def _error(self, message: str) -> SearchResult:
        return SearchResult(self.source, [], SourceStatus.ERROR, message)


def _product_rows(data: object) -> list | None:
    if not isinstance(data, dict):
        return None
    nested = data.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("products"), list):
        return nested["products"]
    if isinstance(data.get("products"), list):
        return data["products"]
    return None


def _has_price_schema(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    if "salePriceU" in row:
        return True
    sizes = row.get("sizes")
    return isinstance(sizes, list) and any(
        isinstance(size, dict) and isinstance(size.get("price"), dict)
        and "product" in size["price"] for size in sizes
    )


def _in_stock(row: dict) -> bool:
    quantity = row.get("totalQuantity")
    if isinstance(quantity, (int, float)) and quantity <= 0:
        return False
    stocks = row.get("stocks")
    if isinstance(stocks, list):
        # An explicit empty stock list means this size cannot be ordered.
        return any(
            isinstance(stock, dict)
            and isinstance(stock.get("qty"), (int, float))
            and stock["qty"] > 0
            for stock in stocks
        )
    return True


def _kopeks(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        price = Decimal(str(value)) / 100
    except InvalidOperation:
        return None
    return price if price.is_finite() and price > 0 else None


def _parse_product(row: object) -> Product | None:
    if not isinstance(row, dict) or not _in_stock(row):
        return None
    product_id = row.get("id")
    if isinstance(product_id, bool) or not str(product_id).isdigit():
        return None
    title = row.get("name")
    if not isinstance(title, str) or not title.strip():
        return None
    currency = row.get("currency")
    if currency is not None and str(currency).upper() not in ("RUB", "RUR", "643"):
        return None
    prices = []
    sizes = row.get("sizes")
    if isinstance(sizes, list):
        for size in sizes:
            if not isinstance(size, dict) or not _in_stock(size):
                continue
            price_data = size.get("price")
            if isinstance(price_data, dict):
                price = _kopeks(price_data.get("product"))
                if price is not None:
                    prices.append(price)
    if not prices:
        # The legacy top-level schema has no per-size prices. Do not use it
        # when explicit per-size stock data says every size is unavailable.
        if isinstance(sizes, list) and not any(
            isinstance(size, dict) and _in_stock(size) for size in sizes
        ):
            return None
        price = _kopeks(row.get("salePriceU"))
        if price is not None:
            prices.append(price)
    if not prices:
        return None
    return Product(
        source="wildberries",
        title=title.strip(),
        url=f"https://www.wildberries.ru/catalog/{product_id}/detail.aspx",
        price=min(prices),
        currency="RUB",
    )
