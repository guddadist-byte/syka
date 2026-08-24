"""Client for tooken.club's OpenAI-compatible Responses API.

NOT Chat Completions — tooken.club uses wire_api = "responses" (confirmed
from the user's own working client config): POST {base_url}/responses with
an `input` array, not POST {base_url}/chat/completions with `messages`.
Exact response field for the finished text (`output_text` vs
`output[0].content[0].text`) is unconfirmed without a live key — both are
handled in _extract_text below, see "Known limitations" in the plan.

No streaming: the bot always shows one finished draft with buttons, never
a token-by-token edit, so plain request/response is all that's needed.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

import database
from bot_cache import CachedMessage
from models import Point

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 30
CONTEXT_MESSAGE_LIMIT = 10

SYSTEM_PROMPT_TEMPLATE = """\
Ты — ассистент поддержки ломбарда «Гудда» в чате Avito, отвечаешь от лица \
сотрудника точки «{point_name}».

РАЗРЕШЕНО обсуждать:
- наличие товара/свободных мест, возможность записи, общую доступность услуги;
- часы работы: {working_hours};
- адрес точки: {address};
- общие условия работы (какие документы нужны, как в общих чертах проходит \
сдача/выкуп — без цифр).

СТРОГО ЗАПРЕЩЕНО:
- называть любые цены, суммы, проценты, ставки, оценочную стоимость залога \
или скупки — ни числом, ни диапазоном, ни приблизительно;
- давать любую оценку стоимости товара в любой форме («дорого/дёшево» и т.п.);
- если клиент спрашивает цену/оценку — ответь, что точную сумму озвучит \
сотрудник при осмотре товара на месте, и предложи приехать в точку или позвонить.

Отвечай кратко (2-4 предложения), вежливо, по-русски. Не придумывай факты, \
которых нет в контексте диалога.
"""


class AIClientError(Exception):
    pass


def _build_system_prompt(point: Point, prompt_override: str | None) -> str:
    base = SYSTEM_PROMPT_TEMPLATE.format(
        point_name=point.name,
        working_hours=point.working_hours or "уточняется на месте",
        address=point.address or "уточняется на месте",
    )
    if prompt_override:
        base += "\n\nДополнительная инструкция для этого конкретного случая:\n" + prompt_override
    return base


def _extract_text(data: dict) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text") and content.get("text"):
                return str(content["text"])
    raise AIClientError(f"AI response had no extractable text: {data!r}")


async def generate_reply(messages: list[CachedMessage], point: Point,
                          prompt_override: str | None = None) -> str:
    cfg = await database.get_ai_config()
    if not cfg.is_enabled or not cfg.api_key:
        raise AIClientError("AI provider is not configured")

    input_items = [{"role": "system", "content": _build_system_prompt(point, prompt_override)}]
    for message in messages[-CONTEXT_MESSAGE_LIMIT:]:
        role = "user" if message.direction == "in" else "assistant"
        input_items.append({"role": role, "content": message.text})

    payload = {"model": cfg.model, "input": input_items}
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    if cfg.extra_header_name:
        headers[cfg.extra_header_name] = cfg.extra_header_value

    url = f"{cfg.base_url.rstrip('/')}/responses"
    backoff = 1.0
    last_error: Exception | None = None

    async with aiohttp.ClientSession() as session:
        for _attempt in range(MAX_RETRIES):
            try:
                async with session.post(
                    url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status >= 500:
                        last_error = AIClientError(f"AI provider returned {resp.status}")
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                    if resp.status >= 400:
                        text = await resp.text()
                        raise AIClientError(f"AI provider returned {resp.status}: {text}")
                    data = await resp.json()
                    return _extract_text(data)
            except aiohttp.ClientError as exc:
                last_error = exc
                await asyncio.sleep(backoff)
                backoff *= 2

    raise AIClientError(f"AI request failed after {MAX_RETRIES} attempts: {last_error}")
