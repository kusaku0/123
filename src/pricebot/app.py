import asyncio
import logging
import time
from collections import OrderedDict

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from pricebot.config import Settings
from pricebot.render import render_results
from pricebot.runtime import Runtime

logger = logging.getLogger(__name__)


class BotHandlers:
    def __init__(self, settings: Settings, runtime: Runtime):
        self.settings = settings
        self.runtime = runtime
        self._last: OrderedDict[int, float] = OrderedDict()
        self._active_users: set[int] = set()
        self._active = 0

    async def permitted(self, update: Update) -> bool:
        if not update.effective_user or not update.effective_message:
            return False
        if self.settings.allowed_user_ids and update.effective_user.id not in self.settings.allowed_user_ids:
            await update.effective_message.reply_text("Доступ к этому боту ограничен владельцем.")
            return False
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.permitted(update):
            return
        await update.effective_message.reply_text(
            f"Пришлите название товара, например: iPhone 15 128 GB.\n"
            f"Ищу на WB, Ozon, AliExpress и Avito. Город: {self.settings.city}.\n"
            "Команда: /search название товара.\n\n"
            "Сравниваю найденные цены в рублях без доставки. На Avito учитывайте состояние товара. "
            "Если сайт ограничит доступ или покажет CAPTCHA, сообщу об этом и дам ссылку на ручной поиск. "
            "Поиск обычно занимает до минуты."
        )

    async def search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self.permitted(update):
            return
        message = update.effective_message
        if message.text.startswith("/"):
            query = " ".join(context.args or [])
        else:
            query = message.text.strip()
        if len(query) < 2 or len(query) > 160 or not any(c.isalnum() for c in query):
            await message.reply_text("Укажите название товара: от 2 до 160 символов. Например: /search пылесос Xiaomi")
            return
        uid = update.effective_user.id
        now = time.monotonic()
        if uid in self._active_users:
            await message.reply_text("Ваш предыдущий поиск ещё выполняется.")
            return
        if now - self._last.get(uid, float("-inf")) < self.settings.user_cooldown:
            await message.reply_text(f"Подождите {self.settings.user_cooldown:g} секунд между запросами.")
            return
        if self._active >= self.settings.max_concurrent_searches:
            await message.reply_text("Сейчас выполняется несколько поисков. Попробуйте через минуту.")
            return
        self._last[uid] = now
        self._last.move_to_end(uid)
        while len(self._last) > 10000:
            self._last.popitem(last=False)
        self._active_users.add(uid)
        self._active += 1
        try:
            await message.reply_text("Ищу предложения на четырёх площадках…")
            if self.runtime.service is None:
                await message.reply_text("Поиск ещё не запущен. Попробуйте позже.")
                return
            results, cached = await self.runtime.service.search(query)
            text = render_results(query, results, city=self.settings.city, avito_city=self.settings.avito_city, cached=cached)
            await message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
        finally:
            self._active -= 1
            self._active_users.discard(uid)

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        # Never log exception messages/tracebacks: Telegram URLs contain the bot token.
        logger.warning("Bot handler failed (%s)", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text("Не удалось завершить запрос. Попробуйте немного позже.")
            except Exception:
                pass


def build_application(settings: Settings) -> Application:
    runtime = Runtime(settings)
    handlers = BotHandlers(settings, runtime)

    async def post_init(app: Application):
        webhook = await app.bot.get_webhook_info()
        if webhook.url:
            raise ValueError("У бота уже настроен webhook. Сначала осознанно отключите его в текущей интеграции для перехода на long polling.")
        await runtime.start()

    async def post_shutdown(app: Application):
        await runtime.close()

    application = (Application.builder().token(settings.bot_token).concurrent_updates(16)
                   .post_init(post_init).post_shutdown(post_shutdown).build())
    application.add_handler(CommandHandler(["start", "help"], handlers.start))
    application.add_handler(CommandHandler("search", handlers.search))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.search))
    application.add_error_handler(handlers.error)
    return application
