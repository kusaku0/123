from html import escape
from urllib.parse import quote, urlsplit

from pricebot.models import SearchResult, SourceStatus

LABELS = {"wildberries": "WB", "ozon": "Ozon", "aliexpress": "AliExpress", "avito": "Avito"}
STATUSES = {SourceStatus.OK: "найдены варианты", SourceStatus.EMPTY: "нет совпадений в полученной выдаче", SourceStatus.BLOCKED: "доступ ограничен", SourceStatus.TIMEOUT: "не ответил вовремя", SourceStatus.ERROR: "поиск недоступен"}


def search_links(query: str, avito_city: str = "lipetsk") -> dict[str, str]:
    q = quote(query, safe="")
    return {
        "wildberries": f"https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
        "ozon": f"https://www.ozon.ru/search/?text={q}&sorting=price",
        "aliexpress": f"https://aliexpress.ru/wholesale?SearchText={q}",
        "avito": f"https://www.avito.ru/{quote(avito_city, safe='')}?q={q}&s=1",
    }


def render_results(query: str, results: list[SearchResult], *, city: str = "Липецк", avito_city: str = "lipetsk", cached: bool = False) -> str:
    items = sorted((p for r in results if r.status == SourceStatus.OK for p in r.items[:3]), key=lambda p: p.price)[:8]
    lines = [f"<b>Поиск: {escape(query[:160])}</b>", f"Город: {escape(city[:60])}. Цены в рублях, без доставки."]
    if cached:
        lines.append("Повторный результат из кратковременного кэша.")
    if items:
        lines.append("\n<b>Дешевле среди найденных предложений:</b>")
        for i, p in enumerate(items, 1):
            host = urlsplit(p.url).hostname or ""
            if not host or urlsplit(p.url).scheme != "https":
                continue
            price = f"{p.price:,.2f}".replace(",", " ")
            title = escape(p.title[:100])
            label = escape(LABELS.get(p.source, p.source))
            lines.append(f'{i}. <b>{price} ₽</b> · {label}\n<a href="{escape(p.url, quote=True)}">{title}</a>')
    else:
        lines.append("\nПодтверждённых цен получить не удалось.")
    lines.append("\n<b>Площадки:</b>")
    for r in results:
        label = escape(LABELS.get(r.source, r.source))
        lines.append(f"• {label}: {STATUSES[r.status]}.")
        if r.message:
            lines.append(escape(r.message[:170]))
    lines.append("\nЦена и наличие могут измениться. Проверьте комплектацию, состояние, продавца и сумму при оформлении. Регион доставки WB/Ozon/AliExpress требует проверки на сайте. Полный охват выдачи не гарантируется.")
    links = search_links(query, avito_city)
    lines.append("Открыть поиск: " + " · ".join(f'<a href="{escape(url, quote=True)}">{LABELS[source]}</a>' for source, url in links.items()))
    return "\n".join(lines)
