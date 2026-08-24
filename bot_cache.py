"""SmartCache — independent in-memory chat store.

Exists specifically so tasks.py (background poller) and handlers.py /
ai_handlers.py (button callbacks) can both read/write live chat state
without importing each other — that's what actually avoids the circular
import between the poller and the handlers. Neither of those two modules
is imported here.

Single-process asyncio app, so a plain asyncio.Lock around mutating /
iterating blocks is enough; critical sections never await on I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from constants import DOUBLE_CLICK_TTL_SECONDS, SHORT_ID_LENGTH

Direction = Literal["in", "out"]


@dataclass
class CachedMessage:
    avito_message_id: str | None
    direction: Direction
    text: str
    has_image: bool
    created_at: datetime


@dataclass
class CachedChat:
    chat_id: str
    short_id: str
    avito_account_id: int
    point_id: int | None
    client_name: str
    item_id: str | None = None
    item_title: str | None = None
    item_url: str | None = None
    messages: deque[CachedMessage] = field(default_factory=lambda: deque(maxlen=50))
    unread_count: int = 0
    last_message_at: datetime | None = None
    last_replied_at: datetime | None = None
    last_replied_by: int | None = None


_chats: dict[str, CachedChat] = {}
_short_index: dict[str, str] = {}
_sent_index: dict[str, tuple[str, str]] = {}
_claims: dict[str, float] = {}
_lock = asyncio.Lock()


async def init_cache() -> None:
    async with _lock:
        _chats.clear()
        _short_index.clear()
        _sent_index.clear()
        _claims.clear()


def _make_short_id(chat_id: str) -> str:
    digest = hashlib.md5(chat_id.encode("utf-8")).hexdigest()
    length = SHORT_ID_LENGTH
    candidate = digest[:length]
    while candidate in _short_index and _short_index[candidate] != chat_id:
        length += 2
        candidate = digest[:length]
    return candidate


async def get_short_id(chat_id: str) -> str:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is not None:
            return chat.short_id
        short_id = _make_short_id(chat_id)
        _short_index[short_id] = chat_id
        return short_id


async def upsert_chat(chat_id: str, *, point_id: int | None, avito_account_id: int,
                       client_name: str, item_id: str | None = None,
                       item_title: str | None = None, item_url: str | None = None,
                       initial_unread_count: int = 0) -> CachedChat:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            # initial_unread_count seeds a freshly-created (post-restart)
            # cache entry from the durable DB count — without this, a chat
            # whose only unread message was already persisted before the
            # restart would come back with unread_count=0 here (add_message
            # is never called again for a message tasks.py already knows),
            # even though it's genuinely still unread in the DB.
            short_id = _make_short_id(chat_id)
            chat = CachedChat(
                chat_id=chat_id,
                short_id=short_id,
                avito_account_id=avito_account_id,
                point_id=point_id,
                client_name=client_name,
                item_id=item_id,
                item_title=item_title,
                item_url=item_url,
                unread_count=initial_unread_count,
            )
            _chats[chat_id] = chat
            _short_index[short_id] = chat_id
        else:
            chat.avito_account_id = avito_account_id
            if point_id is not None:
                chat.point_id = point_id
            if client_name:
                chat.client_name = client_name
            if item_id is not None:
                chat.item_id = item_id
            if item_title is not None:
                chat.item_title = item_title
            if item_url is not None:
                chat.item_url = item_url
        return chat


async def get_chat(chat_id: str) -> CachedChat | None:
    async with _lock:
        return _chats.get(chat_id)


async def get_chat_by_short_id(short_id: str) -> CachedChat | None:
    async with _lock:
        chat_id = _short_index.get(short_id)
        if chat_id is None:
            return None
        return _chats.get(chat_id)


async def resolve_chat(key: str) -> CachedChat | None:
    chat = await get_chat_by_short_id(key)
    if chat is not None:
        return chat
    return await get_chat(key)


async def add_message(chat_id: str, message: CachedMessage) -> bool:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return False
        if message.avito_message_id is not None:
            for existing in chat.messages:
                if existing.avito_message_id == message.avito_message_id:
                    return False
        chat.messages.append(message)
        chat.last_message_at = message.created_at
        if message.direction == "in":
            chat.unread_count += 1
        return True


async def mark_read(chat_id: str) -> None:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is not None:
            chat.unread_count = 0


async def mark_replied(chat_id: str, by_user_id: int) -> None:
    await mark_read(chat_id)
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is not None:
            chat.last_replied_at = datetime.utcnow()
            chat.last_replied_by = by_user_id


async def get_unread_for_points(point_ids: set[int] | None) -> list[CachedChat]:
    async with _lock:
        chats = list(_chats.values())
    result = [c for c in chats if c.unread_count > 0]
    if point_ids is not None:
        result = [c for c in result if c.point_id in point_ids]
    result.sort(key=lambda c: c.last_message_at or datetime.min, reverse=True)
    return result


async def get_recent_replies_for_points(point_ids: set[int] | None, within: timedelta) -> list[CachedChat]:
    cutoff = datetime.utcnow() - within
    async with _lock:
        chats = list(_chats.values())
    result = [c for c in chats if c.last_replied_at is not None and c.last_replied_at >= cutoff]
    if point_ids is not None:
        result = [c for c in result if c.point_id in point_ids]
    result.sort(key=lambda c: c.last_replied_at or datetime.min, reverse=True)
    return result


async def register_sent_message(chat_id: str, avito_message_id: str) -> str:
    async with _lock:
        digest = hashlib.md5(f"{chat_id}:{avito_message_id}".encode("utf-8")).hexdigest()[:12]
        _sent_index[digest] = (chat_id, avito_message_id)
        return digest


async def resolve_sent_message(msg_ref: str) -> tuple[str, str] | None:
    async with _lock:
        return _sent_index.get(msg_ref)


async def try_claim_action(key: str, ttl_seconds: float = DOUBLE_CLICK_TTL_SECONDS) -> bool:
    """Anti-double-click guard: True and claims `key` if free/expired, else False."""
    now = time.monotonic()
    async with _lock:
        expiry = _claims.get(key)
        if expiry is not None and expiry > now:
            return False
        _claims[key] = now + ttl_seconds
        return True
