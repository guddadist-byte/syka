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
    is_read: bool = True
    image_url: str | None = None


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
    last_read_at: datetime | None = None


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


def _real_unread_count(messages: "deque[CachedMessage]") -> int:
    """Real unread count, straight from Avito's own per-message is_read
    field (GET .../messenger/v3/.../messages/ — confirmed in Avito's own
    OpenAPI spec, is_read: "True, если сообщение уже было прочитано
    запрашиваемым пользователем"). Not a local guess: a client ("in")
    message is unread exactly when Avito itself says it hasn't been read
    yet — this is true whether a human read it through this bot, through
    Avito's own app, or not at all, since is_read is refreshed from a real
    Avito response on every poll (see tasks._process_chat).

    CachedMessage.is_read defaults to True, so a message hydrated from our
    own DB at startup (which doesn't persist this field — see
    tasks._build_initial_messages) counts as read until a live poll
    confirms otherwise, rather than resurrecting a stale local guess.
    """
    return sum(1 for m in messages if m.direction == "in" and not m.is_read)


async def upsert_chat(chat_id: str, *, point_id: int | None, avito_account_id: int,
                       client_name: str, item_id: str | None = None,
                       item_title: str | None = None, item_url: str | None = None,
                       initial_messages: list[CachedMessage] | None = None,
                       read_at: datetime | None = None) -> CachedChat:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            # initial_messages seeds a freshly-created (post-restart) cache
            # entry from durable DB history — without this, a chat whose
            # messages were all persisted before the restart would come
            # back with an empty dialog (add_message is never called again
            # for a message tasks.py already knows), even though the DB
            # has the real history. read_at is the same idea for the
            # "marked read without replying" boundary — without seeding it
            # too, every such chat would come back as unread on restart.
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
                last_read_at=read_at,
            )
            if initial_messages:
                chat.messages.extend(initial_messages)
                # Deliberately NOT setting chat.last_message_at here: these
                # messages' is_read defaults to True (the messages table
                # doesn't persist Avito's real flag), so unread_count below
                # is 0 until a live poll confirms otherwise. If
                # last_message_at were set too, _process_chat's
                # short-circuit ("nothing new since last_message_at and
                # already 0 unread") would see it as already up to date and
                # skip the one get_messages() call that could ever correct
                # that default — leaving it None forces exactly one real
                # poll per chat after a restart (add_message sets a real,
                # confirmed value once that happens).
                chat.unread_count = _real_unread_count(chat.messages)
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
    """Appends a genuinely new message, or — if this avito_message_id is
    already cached — refreshes just its is_read flag in place and returns
    False. The refresh path matters: tasks._process_chat's durable
    known_ids dedup means an already-seen message is never re-persisted or
    re-notified, but Avito's own is_read for it can still flip from False
    to True later (a human reads it directly in Avito's app, without ever
    sending a reply) — without updating it here, that chat would stay
    "unread" in our cache forever."""
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return False
        if message.avito_message_id is not None:
            for existing in chat.messages:
                if existing.avito_message_id == message.avito_message_id:
                    if existing.is_read != message.is_read:
                        existing.is_read = message.is_read
                        chat.unread_count = _real_unread_count(chat.messages)
                    # Fill in an attachment we couldn't resolve before (a
                    # message cached before image_url existed, or hydrated
                    # from a DB row predating the column). Only fills a
                    # blank — never overwrites a URL we already have.
                    if message.image_url and not existing.image_url:
                        existing.image_url = message.image_url
                        existing.has_image = True
                    # Also true (not just when is_read changed): this is
                    # what actually confirms, with real Avito data, that
                    # this chat's state is current as of this message's
                    # timestamp — needed so upsert_chat's post-restart
                    # None stops forcing a re-fetch every single cycle
                    # once the first real one has happened.
                    chat.last_message_at = max(chat.last_message_at or message.created_at, message.created_at)
                    return False
        chat.messages.append(message)
        if len(chat.messages) > 1 and chat.messages[-2].created_at > message.created_at:
            # Out-of-order arrival: e.g. a reply sent through this bot gets
            # appended in real time, and moments later the poller discovers
            # a client message that was actually sent (by created_at)
            # *before* that reply but only just got fetched from Avito.
            # Rendering/unread-counting both trust append order to be
            # chronological, so a blind append here would show "my reply"
            # as the last line even while notifying about a newer client
            # message — re-sort instead of trusting arrival order.
            ordered = sorted(chat.messages, key=lambda m: m.created_at)
            chat.messages.clear()
            chat.messages.extend(ordered)
        chat.last_message_at = max(chat.last_message_at or message.created_at, message.created_at)
        chat.unread_count = _real_unread_count(chat.messages)
        return True


async def sync_is_read(chat_id: str, avito_message_id: str, is_read: bool,
                        image_url: str | None = None) -> None:
    """Reconciles is_read for an ALREADY-known message only — unlike
    add_message(), never appends a message bot_cache hasn't seen before.

    This is what the "open a chat" / "🔄 Обновить" live-refresh path
    (handlers.py/webapp.py) must use instead of add_message(): those
    callers have no durable known_ids/database.append_message() pairing of
    their own (that pairing lives in tasks._process_chat, see add_message's
    docstring). Calling add_message() directly from there let a message
    become "known" in bot_cache — and therefore permanently skipped by
    _process_chat's `is_new` check — without ever being persisted to the
    messages table. Bot_cache is in-memory only, so the next restart
    forgets it while the DB (and known_ids) still doesn't have it either;
    _process_chat then sees it as genuinely new and re-notifies about an
    old, already-answered chat — this was the "шлёт все отвеченные заново"
    regression."""
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is None:
            return
        for existing in chat.messages:
            if existing.avito_message_id == avito_message_id:
                if existing.is_read != is_read:
                    existing.is_read = is_read
                    chat.unread_count = _real_unread_count(chat.messages)
                if image_url and not existing.image_url:
                    existing.image_url = image_url
                    existing.has_image = True
                return


async def mark_read(chat_id: str) -> None:
    async with _lock:
        chat = _chats.get(chat_id)
        if chat is not None:
            chat.unread_count = 0
            chat.last_read_at = chat.last_message_at or datetime.utcnow()


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
