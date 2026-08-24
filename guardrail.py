"""Defense-in-depth against the AI ever stating a price or valuation.

The system prompt (ai_client.py) already forbids this, but a prompt is not
a guarantee — this regex pass runs on every draft before it reaches an
employee. Intentionally aggressive (a bare 3+ digit number is flagged even
though that also catches phone numbers/dates): a leaked price is worse than
an employee having to type the reply by hand.
"""

from __future__ import annotations

import re

import ai_client
from bot_cache import CachedMessage
from models import Point

SAFE_FALLBACK_TEXT = (
    "Точную сумму озвучит сотрудник при осмотре товара на месте — "
    "пожалуйста, свяжитесь с точкой напрямую или приезжайте лично."
)

PRICE_PATTERNS = [
    re.compile(r"\d[\d\s]*(?:₽|руб(?:л[ья]?)?)", re.IGNORECASE),
    re.compile(r"\d{1,3}\s*%"),
    re.compile(r"(?:от|до|около|примерно)\s*\d{3,}", re.IGNORECASE),
    re.compile(r"\b\d{3,}\b"),
]


def contains_price_like_content(text: str) -> bool:
    return any(pattern.search(text) for pattern in PRICE_PATTERNS)


async def guarded_generate(messages: list[CachedMessage], point: Point,
                            prompt_override: str | None = None) -> tuple[str, bool]:
    draft = await ai_client.generate_reply(messages, point, prompt_override)
    if contains_price_like_content(draft):
        return SAFE_FALLBACK_TEXT, True
    return draft, False
