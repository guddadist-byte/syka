"""Live reconciliation of one chat against Avito, for the interactive paths.

This is what runs when a human opens a chat card or taps «🔄 Обновить», in
the bot and in the Mini App alike. Background polling (tasks.poll_account_loop)
deliberately skips chats it already believes are settled, which is right for
the ambient list but leaves the card someone is actually looking at showing
whatever the last poll happened to catch.

It lives in its own module because handlers.py and webapp.py are
deliberately independent of each other (see the project's import graph) and
used to carry two hand-kept copies of this. The logic below is too
invariant-sensitive to maintain twice. It imports only the service layer —
avito_client, bot_cache, database, utils — and never handlers, webapp or
tasks, so it sits below all three and creates no cycle.

tasks._process_chat is NOT built on this, on purpose: its message walk is
interleaved with the notification decision, and notification stays exactly
one code path.

THE INVARIANT THIS MODULE HAS TO RESPECT. bot_cache is memory-only;
database.messages is durable and is what _process_chat's dedup (known_ids)
reads. Making a message known to the cache WITHOUT persisting it creates a
phantom: after the next restart the cache has forgotten it and the DB never
had it, so the poller sees it as brand new and notifies about a long-
answered chat. That was the "шлёт все отвеченные заново" regression
(commit e78de63), and the reaction then was to ban appending here at all —
which is why a reply typed in Avito's own app stayed invisible in the card
no matter how many times you pressed Обновить. The ban is lifted by
removing its cause rather than its effect: persist first, cache second, so
the two can never disagree in the dangerous direction.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import avito_client
import bot_cache
import database
import utils

logger = logging.getLogger(__name__)


async def refresh_chat_from_avito(chat: bot_cache.CachedChat) -> None:
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        return
    try:
        messages = await client.get_messages(chat.chat_id)
    except (avito_client.AvitoAPIError, asyncio.TimeoutError):
        return

    known = await database.get_known_message_read_flags(chat.chat_id)
    newest_out: tuple[str | None, datetime] | None = None

    for m in messages:
        if m.message_id is None:
            continue

        # Reconcile the cache for EVERY message, before anything else.
        # sync_is_read matches on what the CACHE holds, which is not the
        # same set as what the DB holds — a message can be cached without
        # being persisted (any bot_cache entry seeded outside the poller).
        # Gating this on DB membership silently stopped reconciling those,
        # which is how "replied in Avito, bot still shows unread" came back.
        await bot_cache.sync_is_read(chat.chat_id, m.message_id, m.is_read, image_url=m.image_url)

        if m.message_id in known:
            if m.image_url:
                # Fills a blank only (see set_message_image_url) — a message
                # persisted before the image_url column existed never passes
                # through append_message again.
                await database.set_message_image_url(m.message_id, m.image_url)
            if known[m.message_id] != m.is_read:
                # Guarded by the comparison on purpose: an unguarded UPDATE
                # would commit once per message per chat open, on the single
                # connection the whole process writes through.
                await database.set_message_is_read(m.message_id, m.is_read)
            continue

        if m.direction != "out":
            # An unknown INBOUND message is left alone, deliberately.
            # Notification belongs to the poller, and it decides by asking
            # whether the message is already in the messages table. Persist
            # it here and the poller would treat it as already handled —
            # so a client's new message would silently never reach the
            # other subscribers of the point, just because one person
            # happened to have the chat open. Nothing is lost by waiting:
            # a new inbound message makes the chat unread on Avito, which
            # puts it in the unread_only poll list and gets it picked up on
            # the next cycle. It is the outgoing case that was slow.
            continue

        created_at = utils.parse_utc(m.created_at) if m.created_at else datetime.utcnow()
        # Durable row first, cache second — never the other way round (see
        # the module docstring: the reverse order is what manufactures a
        # phantom).
        await database.append_message(
            chat.chat_id, "out", m.text, m.has_image,
            sent_at=created_at.strftime("%Y-%m-%d %H:%M:%S"),
            avito_message_id=m.message_id, image_url=m.image_url, is_read=m.is_read,
        )
        await bot_cache.add_message(
            chat.chat_id,
            bot_cache.CachedMessage(
                avito_message_id=m.message_id, direction="out", text=m.text,
                has_image=m.has_image, image_url=m.image_url,
                created_at=created_at, is_read=m.is_read,
            ),
        )
        newest_out = (m.text, created_at)

    refreshed = await bot_cache.get_chat(chat.chat_id)
    if refreshed is not None:
        # Opening a chat still does not mark it read — this writes the count
        # actually derived from Avito's own per-message flags, so it only
        # reaches 0 when Avito says every inbound message really was read.
        await database.set_chat_unread_count(chat.chat_id, refreshed.unread_count)

    if newest_out is not None:
        text, created_at = newest_out
        # Keep the chat list's preview in step with the card — otherwise the
        # list would still advertise the client's last message as the latest
        # thing that happened, while the card shows the reply.
        #
        # mark_chat_replied is deliberately NOT called: it records WHO
        # replied, and a reply typed in Avito's own app carries no Telegram
        # user. Guessing there would put a false name on someone else's work.
        await database.upsert_chat_summary(
            chat.chat_id, avito_account_id=chat.avito_account_id, point_id=chat.point_id,
            last_message_at=created_at.strftime("%Y-%m-%d %H:%M:%S"),
            last_message_text=text, last_message_dir="out",
        )
