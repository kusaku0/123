import os
import math
import re
import shutil
from dataclasses import dataclass, field


def _positive(name: str, default: str, maximum: float) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name}: требуется число") from exc
    if not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name}: значение должно быть больше 0 и не больше {maximum}")
    return value


def _count(name: str, default: str, maximum: int) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}: требуется целое число") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name}: значение должно быть от 1 до {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default="", repr=False)
    allowed_user_ids: frozenset[int] = frozenset()
    city: str = "Липецк"
    avito_city: str = "lipetsk"
    wb_dest: str | None = None
    wb_latitude: float = 52.6088
    wb_longitude: float = 39.5992
    browser_executable: str | None = None
    source_timeout: float = 35
    cache_ttl: float = 180
    user_cooldown: float = 10
    max_concurrent_searches: int = 2
    max_cache_entries: int = 128

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if require_token and (not token or ":" not in token):
            raise ValueError("Укажите BOT_TOKEN в .env или переменных окружения (токен от @BotFather).")
        try:
            ids = frozenset(int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
        except ValueError as exc:
            raise ValueError("ALLOWED_USER_IDS: укажите числовые ID через запятую") from exc
        latitude = float(os.getenv("WB_LATITUDE", "52.6088"))
        longitude = float(os.getenv("WB_LONGITUDE", "39.5992"))
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            raise ValueError("Недопустимые WB_LATITUDE/WB_LONGITUDE")
        wb_dest = os.getenv("WB_DEST", "").strip() or None
        if wb_dest and not re.fullmatch(r"-?\d+", wb_dest):
            raise ValueError("WB_DEST: требуется числовой идентификатор региона")
        return cls(
            bot_token=token,
            allowed_user_ids=ids,
            city=os.getenv("CITY", "Липецк"),
            avito_city=os.getenv("AVITO_CITY", "lipetsk"),
            wb_dest=wb_dest,
            wb_latitude=latitude,
            wb_longitude=longitude,
            browser_executable=os.getenv("BROWSER_EXECUTABLE") or shutil.which("chromium") or shutil.which("chromium-browser"),
            source_timeout=_positive("SOURCE_TIMEOUT", "35", 120),
            cache_ttl=_positive("CACHE_TTL", "180", 3600),
            user_cooldown=_positive("USER_COOLDOWN", "10", 3600),
            max_concurrent_searches=_count("MAX_CONCURRENT_SEARCHES", "2", 10),
            max_cache_entries=_count("MAX_CACHE_ENTRIES", "128", 10000),
        )
