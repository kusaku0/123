"""Web interface: same marketplace search, no Telegram API or token needed."""
import argparse
import asyncio
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pricebot.config import Settings
from pricebot.models import SourceStatus
from pricebot.render import search_links
from pricebot.runtime import Runtime
from pricebot.search import normalize
from pricebot.transport import FetchError

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MARKETPLACE_HOSTS = {"wildberries": "wildberries.ru", "ozon": "ozon.ru", "avito": "avito.ru", "aliexpress": "aliexpress.ru"}


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=160)
    refresh: bool = False

    @field_validator("query", mode="before")
    @classmethod
    def clean_query(cls, value):
        if not isinstance(value, str):
            raise ValueError("Введите название товара.")
        value = " ".join(value.split())
        if not any(c.isalnum() for c in value):
            raise ValueError("В названии должны быть буквы или цифры.")
        return value


class BrowserInput(SearchInput):
    source: str


def safe_product_url(source: str, url: str) -> bool:
    try:
        parsed = urlsplit(url)
        domains = [MARKETPLACE_HOSTS[source]]
        if source == "aliexpress":
            domains.append("aliexpress.com")
        return (len(url) <= 2048 and parsed.scheme == "https" and not parsed.username
                and not parsed.password and parsed.port in (None, 443)
                and any(parsed.hostname == host or (parsed.hostname or "").endswith("." + host) for host in domains))
    except (ValueError, KeyError):
        return False


def create_app(settings: Settings | None = None, runtime=None) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env(require_token=False)
    configured_runtime = runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        current = configured_runtime if configured_runtime is not None else Runtime(settings)
        app.state.runtime = current
        app.state.active_searches = 0
        app.state.checked_at = OrderedDict()
        try:
            await current.start()
            yield
        finally:
            await current.close()

    app = FastAPI(title="Ценник — поиск цен", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health():
        # This is application liveness, not a claim that marketplaces are reachable.
        return {"status": "ok", "city": settings.city, "local_browser_mode": settings.local_browser_mode}

    @app.post("/api/browser/open")
    async def open_browser(payload: BrowserInput, request: Request):
        # This launches a window on the server machine, so it is a local-PC feature.
        if not settings.local_browser_mode:
            raise HTTPException(403, "Окно магазина доступно только в локальном режиме на вашем компьютере.")
        host = request.url.hostname
        client = request.client.host if request.client else ""
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if (client not in {"127.0.0.1", "::1"} or host not in {"localhost", "127.0.0.1", "::1"}
                or (origin is not None and origin != expected_origin)):
            raise HTTPException(403, "Открывать окно магазина можно только с этого компьютера.")
        links = search_links(payload.query, settings.avito_city)
        if payload.source not in links:
            raise HTTPException(422, "Неизвестная площадка.")
        try:
            await request.app.state.runtime.browser.open_for_review(links[payload.source])
            return {"status": "opened", "message": "Окно магазина открыто на этом компьютере. После проверки нажмите «Найти дешевле» ещё раз."}
        except FetchError as exc:
            raise HTTPException(503, exc.message) from None
        except Exception as exc:
            logger.warning("Local browser window failed (%s)", type(exc).__name__)
            raise HTTPException(503, "Не удалось открыть окно магазина. Проверьте запущенное окно программы.") from None

    @app.post("/api/search")
    async def search(payload: SearchInput, request: Request):
        state = request.app.state
        if state.active_searches >= settings.max_concurrent_searches:
            raise HTTPException(429, "Сейчас выполняется несколько поисков. Попробуйте через минуту.", headers={"Retry-After": "30"})
        state.active_searches += 1
        try:
            service = state.runtime.service
            if service is None:
                raise HTTPException(503, "Поиск ещё запускается. Попробуйте немного позже.")
            if payload.refresh:
                service.invalidate(payload.query)
            results, cached = await service.search(payload.query)
            links = search_links(payload.query, settings.avito_city)
            products = sorted((p for r in results if r.status == SourceStatus.OK for p in r.items[:3]
                               if p.currency == "RUB" and p.price.is_finite() and p.price > 0 and safe_product_url(p.source, p.url)), key=lambda p: p.price)
            key = normalize(payload.query)
            if not cached or key not in state.checked_at:
                state.checked_at[key] = datetime.now(timezone.utc).isoformat()
            state.checked_at.move_to_end(key)
            checked_at = state.checked_at[key]
            while len(state.checked_at) > settings.max_cache_entries:
                state.checked_at.popitem(last=False)
            return {
                "query": payload.query, "city": settings.city, "cached": cached,
                "checked_at": checked_at,
                "products": [{"source": p.source, "title": p.title[:1000], "url": p.url,
                              "price": str(p.price), "currency": p.currency} for p in products],
                "sources": [{"source": r.source, "status": r.status.value, "message": r.message,
                             "can_open_browser": settings.local_browser_mode,
                             "count": sum(p.source == r.source for p in products), "search_url": links.get(r.source, "")}
                            for r in results],
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Web search failed (%s)", type(exc).__name__)
            raise HTTPException(503, "Не удалось завершить поиск. Попробуйте позже.") from None
        finally:
            state.active_searches -= 1

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app


def main():
    parser = argparse.ArgumentParser(description="Ценник — веб-поиск цен без Telegram")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес прослушивания (для сервера: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true", help="Проверить Chromium без запуска сервера и внешних запросов")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть в диапазоне 1..65535")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.check:
            from pricebot.__main__ import check
            load_dotenv()
            asyncio.run(check(Settings.from_env(require_token=False)))
            return
        import uvicorn
        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
