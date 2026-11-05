"""Best-effort public-page search providers; challenges are reported explicitly."""

from __future__ import annotations

import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from pricebot.extraction import extract_products
from pricebot.models import SearchResult, SourceStatus
from pricebot.transport import FetchError


class PageProvider:
    def __init__(self, source: str, transport, city: str = "lipetsk"):
        if source not in {"ozon", "aliexpress", "avito"}:
            raise ValueError(f"Unknown page source: {source}")
        if not re.fullmatch(r"[a-z0-9_-]{1,80}", city):
            raise ValueError("City must be an Avito URL slug")
        self.source = source
        self.transport = transport
        self.city = city

    def search_url(self, query: str) -> str:
        if self.source == "ozon":
            return "https://www.ozon.ru/search/?" + urlencode({"text": query, "sorting": "price"})
        if self.source == "avito":
            return f"https://www.avito.ru/{self.city}?" + urlencode({"q": query, "s": "1"})
        return "https://aliexpress.ru/wholesale?" + urlencode({"SearchText": query, "SortType": "price_asc"})

    async def search(self, query: str, limit: int = 5) -> SearchResult:
        url = self.search_url(query)
        try:
            html = await self.transport.fetch(url)
            # Match visible challenge content, not strings in application bundles.
            soup = BeautifulSoup(html[:8_000_000], "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            visible = soup.get_text(" ", strip=True).lower()
            challenge = (
                "подтвердите, что вы не робот", "подтвердите что вы не робот",
                "доступ ограничен", "доступ временно ограничен", "проверка безопасности",
                "verify you are human", "access denied", "пройдите проверку",
                "подтвердите, что вы человек", "необходимо решить капчу",
            )
            if any(text in visible for text in challenge) or soup.select_one('iframe[src*="captcha"], form[action*="captcha"]'):
                return SearchResult(self.source, [], SourceStatus.BLOCKED,
                                    "Площадка требует CAPTCHA или ограничила доступ. Обход не выполняется.")
            products = extract_products(html, self.source, url)
            if products:
                note = {
                    "ozon": "Цена на странице может требовать карту Ozon. Доставку и итоговую сумму проверьте в магазине.",
                    "aliexpress": "Проверьте вариант товара, доставку и условия скидки: цена для нового покупателя может отличаться.",
                    "avito": "Цена объявления без доставки. Проверьте состояние товара и продавца перед оплатой.",
                }[self.source]
                return SearchResult(self.source, products[:max(0, limit)], SourceStatus.OK,
                                    note)
            empty = ("ничего не найдено", "по вашему запросу ничего", "нет подходящих объявлений",
                     "no results found", "we couldn't find any results")
            if any(text in visible for text in empty):
                return SearchResult(self.source, [], SourceStatus.EMPTY, "По запросу нет результатов.")
            return SearchResult(self.source, [], SourceStatus.ERROR,
                                "Не удалось прочитать цены в рублях: страница изменилась, требует входа или не загрузила товары.")
        except FetchError as error:
            return SearchResult(self.source, [], error.status, error.message)
        except (ValueError, TypeError, KeyError, AttributeError):
            return SearchResult(self.source, [], SourceStatus.ERROR,
                                "Структура страницы изменилась; цены не подтверждены.")
