"""Conservative extraction from public marketplace pages; no challenge bypasses."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from pricebot.models import Product


_HOSTS = {
    "ozon": ("ozon.ru",),
    "avito": ("avito.ru",),
    "aliexpress": ("aliexpress.ru", "aliexpress.com"),
}
_RUBLE = re.compile(r"(?:₽|руб(?:\.|лей|ля)?|RUB|RUR)", re.I)
MAX_HTML = 8_000_000


def parse_price(value: object, currency: object = None) -> Decimal | None:
    """Accept an explicit RUB amount, never a bare card number or USD amount."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    code = str(currency or "").upper().strip()
    if code and code not in {"RUB", "RUR", "₽", "РУБ"}:
        return None
    markers = _RUBLE.findall(text)
    if (not code and not markers) or len(markers) > 1:
        return None
    # Parse the entire amount. Scanning a numeric substring would turn 1,299.00
    # into 1.29, negatives into positives, and variant ranges into cheap offers.
    amount = _RUBLE.sub("", text).strip()
    amount = re.sub(r"^(?:цена|price)\s*:?\s*", "", amount, flags=re.I)
    if re.fullmatch(r"[0-9]+(?:[.,][0-9]{1,2})?", amount):
        normalized = amount.replace(",", ".")
    elif re.fullmatch(r"[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+(?:[.,][0-9]{1,2})?", amount):
        normalized = re.sub(r"\s", "", amount).replace(",", ".")
    elif re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+\.[0-9]{1,2}", amount):
        normalized = amount.replace(",", "")
    elif re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{3})+,[0-9]{1,2}", amount):
        normalized = amount.replace(".", "").replace(",", ".")
    else:
        return None
    try:
        price = Decimal(normalized)
    except InvalidOperation:
        return None
    return price if price.is_finite() and 0 < price < Decimal("1000000000") else None


def _product(source: str, title: object, href: object, price: object,
             base_url: str, currency: object = None) -> Product | None:
    name = re.sub(r"\s+", " ", str(title or "")).strip()
    amount = parse_price(price, currency)
    if len(name) < 2 or len(name) > 1000 or amount is None:
        return None
    try:
        url = urlsplit(urljoin(base_url, str(href or "")))
        host = (url.hostname or "").lower()
        valid_host = any(host == allowed or host.endswith("." + allowed)
                         for allowed in _HOSTS[source])
        if (url.scheme != "https" or not valid_host or url.username or url.password
                or url.port not in (None, 443)):
            return None
        if source == "ozon" and "/product/" not in url.path:
            return None
        if source == "aliexpress" and "/item/" not in url.path:
            return None
        if source == "avito" and not re.search(r"_\d+/?$", url.path):
            return None
    except (ValueError, KeyError):
        return None
    clean_url = urlunsplit(("https", url.netloc, url.path, "", ""))
    return Product(source=source, title=name, url=clean_url, price=amount, currency="RUB")


def _json_product(node: dict, source: str, base_url: str) -> Product | None:
    kind = node.get("@type", "")
    kinds = kind if isinstance(kind, list) else [kind]
    if "Product" in kinds:
        offers = node.get("offers", {})
        if isinstance(offers, list):
            # Each distinct variant may have its own URL; only take a concrete offer.
            offers = next((x for x in offers if isinstance(x, dict)
                           and "price" in x), {})
        if not isinstance(offers, dict) or "price" not in offers:
            return None  # AggregateOffer.lowPrice is a range, not a quoted variant.
        if str(offers.get("availability", "")).endswith(("OutOfStock", "Discontinued")):
            return None
        return _product(source, node.get("name"), offers.get("url") or node.get("url"),
                        offers["price"], base_url, offers.get("priceCurrency"))

    if source == "ozon" and node.get("sku") and isinstance(node.get("mainState"), list):
        price, title = None, None
        for atom in node["mainState"]:
            if not isinstance(atom, dict):
                continue
            if atom.get("type") == "priceV2":
                price_atom = atom.get("priceV2")
                values = price_atom.get("price", []) if isinstance(price_atom, dict) else []
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    price = values[0].get("text")
            if atom.get("type") == "textAtom":
                title_atom = atom.get("textAtom")
                text = title_atom.get("text", "") if isinstance(title_atom, dict) else ""
                if isinstance(text, str) and (title is None or len(text) > len(title)):
                    title = text
        action = node.get("action", {})
        return _product(source, title, action.get("link") if isinstance(action, dict) else None,
                        price, base_url)

    if source == "aliexpress" and (node.get("itemId") or node.get("productId")):
        prices = node.get("prices", {})
        sale = prices.get("salePrice", {}) if isinstance(prices, dict) else {}
        if not isinstance(sale, dict):
            return None
        if "maxPrice" in sale:
            # AliExpress can advertise the cheapest variant of a broad listing.
            # An explicit range does not identify which variant matches the title.
            minimum = parse_price(sale.get("minPrice"), sale.get("currencyCode") or "RUB")
            maximum = parse_price(sale.get("maxPrice"), sale.get("currencyCode") or "RUB")
            if minimum is None or maximum is None or minimum != maximum:
                return None
        title = node.get("title")
        if isinstance(title, dict):
            title = title.get("displayTitle")
        href = node.get("productDetailUrl") or node.get("url")
        if not href:
            identifier = str(node.get("itemId") or node.get("productId"))
            if identifier.isdigit():
                href = f"/item/{identifier}.html"
        return _product(source, title, href,
                        sale.get("formattedPrice") or sale.get("minPrice"), base_url,
                        sale.get("currencyCode"))
    return None


def _json_documents(soup: BeautifulSoup):
    for script in soup.find_all("script", limit=500):
        content = script.string or script.get_text()
        if not content or len(content) > 2_000_000:
            continue
        raw = content.strip()
        known = script.get("type") in {"application/ld+json", "application/json"}
        known = known or script.get("id") in {"__NEXT_DATA__", "__AER_DATA__", "__NUXT_DATA__"}
        if not known:
            assignment = re.match(r"(?:window\.)?(?:runParams|__AER_DATA__|__INITIAL_STATE__)\s*=\s*", raw)
            if not assignment:
                continue
            raw = raw[assignment.end():]
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
            yield value
        except (ValueError, RecursionError):
            continue
    for element in soup.select("[data-state]")[:100]:
        raw = element.get("data-state", "")
        if len(raw) > 2_000_000:
            continue
        try:
            yield json.loads(raw)
        except (ValueError, RecursionError):
            continue


def _from_json(soup: BeautifulSoup, source: str, base_url: str):
    visited, decoded = 0, 0
    for document in _json_documents(soup):
        stack = [(document, 0)]
        while stack and visited < 30_000:
            node, depth = stack.pop()
            visited += 1
            if depth > 35:
                continue
            if isinstance(node, dict):
                # Skip explicit promoted/recommended content when the marker is present.
                if node.get("isAd") is True or node.get("isSponsored") is True:
                    continue
                if str(node.get("type", "")).lower() in {"recommendations", "advertisement"}:
                    continue
                product = _json_product(node, source, base_url)
                if product:
                    yield product
                stack.extend((value, depth + 1) for value in node.values())
            elif isinstance(node, list):
                stack.extend((value, depth + 1) for value in node)
            elif (isinstance(node, str) and decoded < 100 and len(node) < 300_000
                  and node.lstrip().startswith(("{", "["))):
                # Ozon widgetStates stores JSON as escaped strings.
                decoded += 1
                try:
                    stack.append((json.loads(node), depth + 1))
                except (ValueError, RecursionError):
                    pass


def _element_value(element: Tag | None) -> str:
    if element is None:
        return ""
    return str(element.get("content") or element.get("aria-label")
               or element.get_text(" ", strip=True))


def _from_cards(soup: BeautifulSoup, source: str, base_url: str):
    selectors = {
        "avito": '[data-marker="item"], [itemtype$="/Product"]',
        "ozon": '.tile-root, [data-widget="searchResultsV2"] [data-index], [data-product-id]',
        "aliexpress": '.search-card-item, [class*="SearchProductFeed_Preview"], [data-product-id]',
    }
    links = {
        "avito": 'a[data-marker="item-title"], a[itemprop="url"]',
        "ozon": 'a[href*="/product/"]',
        "aliexpress": 'a[href*="/item/"]',
    }
    prices = {
        "avito": '[itemprop="price"], [data-marker="item-price"]',
        "ozon": '[itemprop="price"], [data-testid="price"], [class*="price-current"]',
        "aliexpress": '[itemprop="price"], .search-card-item__price-main, [class*="PriceCurrent"], [class*="price-current"]',
    }
    for card in soup.select(selectors[source])[:500]:
        if card.get("data-is-ad") == "true" or card.select_one('[data-marker="item-ad-label"], [data-sponsored="true"]'):
            continue
        anchor = card.select_one(links[source])
        if anchor is None:
            continue
        title_element = card.select_one('[itemprop="name"], [data-marker="item-title"], h2, h3')
        title = _element_value(title_element) or str(anchor.get("title") or anchor.get("aria-label") or "")
        # AliExpress's cards often put title on the card anchor itself.
        if not title and source == "aliexpress":
            title = _element_value(card.select_one('[class*="title"], [class*="Title"]'))
        price_element = card.select_one(prices[source])
        currency = _element_value(card.select_one('[itemprop="priceCurrency"]'))
        product = _product(source, title, anchor.get("href"), _element_value(price_element),
                           base_url, currency or None)
        if product:
            yield product


def extract_products(html: str, source: str, base_url: str) -> list[Product]:
    """Extract only priced, RUB-denominated product cards on an allowed domain."""
    if source not in _HOSTS:
        raise ValueError(f"Unknown source: {source}")
    if len(html) > MAX_HTML:
        raise ValueError("Page too large")
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Product] = {}
    for product in (*_from_json(soup, source, base_url), *_from_cards(soup, source, base_url)):
        # When the page quotes different conditional prices, retain the larger value.
        # The checkout still determines the actual price and shipping.
        previous = found.get(product.url)
        if previous is None or product.price > previous.price:
            found[product.url] = product
    return sorted(found.values(), key=lambda item: (item.price, item.url))
