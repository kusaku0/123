import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from pricebot.config import Settings


async def check(settings: Settings):
    from pricebot.transport import BrowserTransport
    browser = BrowserTransport(settings.browser_executable)
    try:
        instance = await browser._get_browser()
        page = await instance.new_page()
        await page.set_content("<html><body><p id='ready'>ready</p></body></html>")
        assert await page.locator("#ready").inner_text() == "ready"
        await page.close()
        print("OK: зависимости импортируются, Chromium запускается и исполняет страницу.")
        print("Это локальная проверка, она не проверяет доступ к магазинам.")
    finally:
        await browser.close()


async def search(settings: Settings, query: str):
    from pricebot.runtime import Runtime
    from pricebot.render import render_results
    runtime = Runtime(settings)
    try:
        await runtime.start()
        results, cached = await runtime.service.search(query)
        print(render_results(query, results, city=settings.city, avito_city=settings.avito_city, cached=cached))
        return 0 if any(r.items for r in results) else 2
    finally:
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="Telegram-бот поиска цен для Липецка")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Локальная проверка Chromium без токена")
    group.add_argument("--search", metavar="QUERY", help="Живой поиск из терминала без Telegram")
    args = parser.parse_args()
    # Existing exported values take precedence over .env. Never print secrets.
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env(require_token=not (args.check or args.search))
        if args.check:
            asyncio.run(check(settings))
        elif args.search:
            sys.exit(asyncio.run(search(settings, args.search)))
        else:
            from pricebot.app import build_application
            build_application(settings).run_polling(allowed_updates=["message"], drop_pending_updates=False)
    except (ValueError, KeyboardInterrupt) as exc:
        if isinstance(exc, ValueError):
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    except Exception as exc:
        print(f"Не удалось запустить бота ({type(exc).__name__}). Проверьте токен, сеть и установку Chromium.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
