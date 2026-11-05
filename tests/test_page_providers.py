import unittest
from urllib.parse import parse_qs, urlsplit

from pricebot.models import SourceStatus
from pricebot.providers.pages import PageProvider
from pricebot.transport import FetchError


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.urls = []

    async def fetch(self, url):
        self.urls.append(url)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class PageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_url_and_lipetsk_default(self):
        provider = PageProvider("avito", FakeTransport("Ничего не найдено"))
        url = urlsplit(provider.search_url("iPhone & кабель"))
        self.assertEqual(url.path, "/lipetsk")
        self.assertEqual(parse_qs(url.query)["q"], ["iPhone & кабель"])
        result = await provider.search("iPhone & кабель")
        self.assertEqual(result.status, SourceStatus.EMPTY)

    async def test_challenge_is_not_empty_results(self):
        result = await PageProvider("ozon", FakeTransport("<h1>Доступ ограничен</h1>")).search("телефон")
        self.assertEqual(result.status, SourceStatus.BLOCKED)
        self.assertEqual(result.items, [])

    async def test_script_mentions_of_captcha_are_not_a_challenge(self):
        response = '<script>const a="verify you are human";</script><h1>Ничего не найдено</h1>'
        result = await PageProvider("ozon", FakeTransport(response)).search("телефон")
        self.assertEqual(result.status, SourceStatus.EMPTY)

    async def test_unknown_page_is_not_claimed_empty(self):
        result = await PageProvider("aliexpress", FakeTransport("<h1>Welcome</h1>")).search("кабель")
        self.assertEqual(result.status, SourceStatus.ERROR)
        self.assertIn("рублях", result.message)

    async def test_fetch_failure_preserves_status(self):
        result = await PageProvider("avito", FakeTransport(
            FetchError(SourceStatus.TIMEOUT, "Долго отвечает"))).search("стол")
        self.assertEqual(result.status, SourceStatus.TIMEOUT)
        self.assertEqual(result.message, "Долго отвечает")

    async def test_invalid_city_rejected(self):
        with self.assertRaises(ValueError):
            PageProvider("avito", FakeTransport(""), city="../../other")


if __name__ == "__main__":
    unittest.main()
