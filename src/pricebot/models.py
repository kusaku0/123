from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class SourceStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True)
class Product:
    source: str
    title: str
    url: str
    price: Decimal
    currency: str = "RUB"


@dataclass
class SearchResult:
    source: str
    items: list[Product] = field(default_factory=list)
    status: SourceStatus = SourceStatus.EMPTY
    message: str = ""
