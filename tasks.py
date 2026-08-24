"""Background polling: Avito message ingestion, backups.

Imports bot_cache and avito_client directly (both are self-contained
module singletons) rather than through handlers.py — that's the mechanism
that avoids a circular import between the poller and the button handlers.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import FSInputFile

import avito_client
import bot_cache
import constants
import database
import keyboards
import models
import utils

logger = logging.getLogger(__name__)


async def poll_account_loop(account: models.AvitoAccount, bot: Bot) -> None:
    backoff = constants.ERROR_BACKOFF_BASE_SECONDS
    cycle = 0
    while True:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            await asyncio.sleep(constants.POLL_INTERVAL_SECONDS)
            continue
        try:
            # Most cycles only ask Avito for its own "unread" chats — far
            # fewer chats to walk per poll, so a cycle finishes faster and
            # (post-restart especially) stops burning the ~1 req/sec/account
            # get_messages() budget on chats that haven't changed. Every
            # Nth cycle does a full unfiltered pass as a safety net, since
            # Avito's server-side "unread" semantics were never confirmed
            # to match our own (its chat objects carry no read/unread field
            # at all — see avito_client.get_chats).
            is_full_sync = cycle % constants.FULL_SYNC_EVERY_N_POLLS == 0
            chats = await client.get_chats(unread_only=not is_full_sync)
            for chat in chats:
                await _process_chat(chat, account, bot, client)
            await database.set_avito_account_error(account.id, None)
            backoff = constants.ERROR_BACKOFF_BASE_SECONDS
            cycle += 1
            await asyncio.sleep(constants.POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except avito_client.AvitoAPIError as exc:
            await database.set_avito_account_error(account.id, str(exc))
            await asyncio.sleep(min(backoff, constants.ERROR_BACKOFF_MAX_SECONDS))
            backoff = min(backoff * 2, constants.ERROR_BACKOFF_MAX_SECONDS)
        except Exception:
            logger.exception("poll_account_loop: unexpected error for account %s", account.id)
            await asyncio.sleep(min(backoff, constants.ERROR_BACKOFF_MAX_SECONDS))
            backoff = min(backoff * 2, constants.ERROR_BACKOFF_MAX_SECONDS)


async def _build_initial_messages(chat_id: str) -> list[bot_cache.CachedMessage]:
    messages: list[bot_cache.CachedMessage] = []
    for m in await database.get_recent_messages(chat_id, limit=50):
        messages.append(
            bot_cache.CachedMessage(
                avito_message_id=m.avito_message_id,
                direction=m.direction,
                text=m.text or "",
                has_image=bool(m.has_image),
                created_at=utils.parse_utc(m.sent_at),
            )
        )
    return messages


async def hydrate_cache_from_db() -> None:
    """Eagerly load every known chat from the durable DB into bot_cache.

    Called once at startup, before the bot serves any Telegram updates or
    the pollers run — without this, bot_cache starts empty and only
    refills chat-by-chat as the rate-limited poller re-discovers each chat
    from Avito, so "📩 Непрочитанные" is inaccurate/growing for the first
    stretch after every restart. This is a pure DB read (no Avito calls),
    so it's fast even for hundreds of chats.
    """
    for chat in await database.list_all_chats():
        initial_messages = await _build_initial_messages(chat.chat_id)
        await bot_cache.upsert_chat(
            chat.chat_id, point_id=chat.point_id, avito_account_id=chat.avito_account_id,
            client_name=chat.client_name or "", item_id=chat.item_id,
            initial_messages=initial_messages,
        )


async def _process_chat(chat: models.AvitoChat, account: models.AvitoAccount, bot: Bot,
                         client: "avito_client.AvitoClient") -> None:
    # Confirmed against a live account: the chat-list response already
    # embeds the ad's coordinates (context.value.location), no separate
    # item lookup needed. resolve_point_for_item handles both a missing
    # item_id (direct-to-profile message) and an item with no coordinates
    # by routing to the fallback point itself.
    point = await database.resolve_point_for_item(chat.item_id, chat.item_lat, chat.item_lon)
    point_id = point.id if point else None

    # Read durable state before touching bot_cache — if this chat isn't in
    # the in-memory cache yet (e.g. a chat hydrate_cache_from_db() didn't
    # know about — brand new since the last restart), upsert_chat seeds
    # its message history from this instead of starting empty. The extra
    # DB round trip only happens once per chat per process lifetime (only
    # when it's not cached yet), not every poll.
    was_cached = await bot_cache.get_chat(chat.chat_id) is not None
    initial_messages = [] if was_cached else await _build_initial_messages(chat.chat_id)

    cached = await bot_cache.upsert_chat(
        chat.chat_id, point_id=point_id, avito_account_id=account.id,
        client_name=chat.client_name, item_id=chat.item_id,
        item_title=chat.item_title, item_url=chat.item_url,
        initial_messages=initial_messages,
    )
    await database.set_chat_unread_count(chat.chat_id, cached.unread_count)
    await database.upsert_chat_summary(
        chat.chat_id, avito_account_id=account.id, point_id=point_id, item_id=chat.item_id,
        client_name=chat.client_name,
    )

    incoming_last = utils.parse_utc(chat.last_message_at) if chat.last_message_at else None
    if cached.last_message_at is not None and incoming_last is not None and incoming_last <= cached.last_message_at:
        return

    try:
        messages = await client.get_messages(chat.chat_id)
    except avito_client.AvitoAPIError:
        return

    known_ids = await database.get_known_message_ids(chat.chat_id)

    for message in messages:
        if message.message_id is not None and message.message_id in known_ids:
            # Already persisted before a restart — bot_cache is in-memory
            # and resets on every restart, so without this DB-backed check
            # every message in Avito's recent history would look "new"
            # again on the first poll after a restart and re-notify.
            continue

        created_at = utils.parse_utc(message.created_at) if message.created_at else datetime.utcnow()
        cached_message = bot_cache.CachedMessage(
            avito_message_id=message.message_id,
            direction=message.direction,
            text=message.text,
            has_image=message.has_image,
            created_at=created_at,
        )
        is_new = await bot_cache.add_message(chat.chat_id, cached_message)
        if not is_new:
            continue

        sent_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
        await database.append_message(
            chat.chat_id, message.direction, message.text, message.has_image,
            sent_at=sent_at_str, avito_message_id=message.message_id,
        )

        if message.direction != "in":
            continue

        await database.upsert_chat_summary(
            chat.chat_id, avito_account_id=account.id, point_id=point_id,
            last_message_at=sent_at_str, last_message_text=message.text, last_message_dir="in",
        )
        await _notify_subscribers(chat.chat_id, point_id, cached, cached_message, bot)

    # unread_count may have changed during the loop above — a new inbound
    # message, or an "out" reply sent directly in Avito's own app rather
    # than through this bot — persist the final, self-corrected value.
    final_chat = await bot_cache.get_chat(chat.chat_id)
    if final_chat is not None:
        await database.set_chat_unread_count(chat.chat_id, final_chat.unread_count)


async def _send_notification(bot: Bot, telegram_id: int, text: str, short_id: str) -> None:
    try:
        await bot.send_message(telegram_id, text, reply_markup=keyboards.chat_notification_kb(short_id))
    except TelegramForbiddenError:
        await database.mark_user_unreachable(telegram_id)
    except Exception:
        logger.exception("_send_notification: failed to notify %s", telegram_id)


async def _notify_subscribers(chat_id: str, point_id: int | None, cached_chat: bot_cache.CachedChat,
                               message: bot_cache.CachedMessage, bot: Bot) -> None:
    short_id = await bot_cache.get_short_id(chat_id)
    client_name = html.escape(cached_chat.client_name or "клиент")
    preview = html.escape(message.text[:200]) if message.text else "(фото)"
    if cached_chat.item_title and cached_chat.item_url:
        item_line = f'📦 <a href="{html.escape(cached_chat.item_url)}">{html.escape(cached_chat.item_title)}</a>'
    elif cached_chat.item_title:
        item_line = f"📦 {html.escape(cached_chat.item_title)}"
    else:
        item_line = "📦 Сообщение в профиль (без объявления)"
    text = f"📩 Новое сообщение от {client_name}\n{item_line}\n\n{preview}"

    if point_id is None:
        # Fully unresolved chat (coords present but no matching point) —
        # per explicit request, notifications are strictly opt-in via
        # "📍 Мои точки" with no exceptions, and nobody can be subscribed
        # to a point that doesn't exist yet, so this stays silent. It's
        # still visible for review in "📭 Чаты без точки".
        return

    recipients = {u.telegram_id: u for u in await database.list_point_subscribers(point_id, on_shift_only=True)}
    for user_id in recipients:
        await _send_notification(bot, user_id, text, short_id)


async def _reload_accounts_loop() -> None:
    while True:
        await asyncio.sleep(constants.ACCOUNT_RELOAD_INTERVAL_SECONDS)
        try:
            await avito_client.reload_accounts()
        except Exception:
            logger.exception("_reload_accounts_loop: failed")


async def _prune_messages_loop() -> None:
    while True:
        await asyncio.sleep(constants.MESSAGE_PRUNE_INTERVAL_SECONDS)
        try:
            await database.prune_old_messages()
        except Exception:
            logger.exception("_prune_messages_loop: failed")


async def run_backup_now(bot: Bot, db_path: str) -> None:
    tmp_path = f"{db_path}.backup-{int(datetime.utcnow().timestamp())}.db"
    await database.vacuum_into(tmp_path)
    try:
        cfg = await database.get_backup_config()
        recipients: list[int]
        if cfg.recipient_telegram_id:
            recipients = [cfg.recipient_telegram_id]
        else:
            recipients = [u.telegram_id for u in await database.list_admins_and_directors() if u.role == constants.DIRECTOR]

        document = FSInputFile(tmp_path)
        for telegram_id in recipients:
            try:
                await bot.send_document(telegram_id, document, caption="💾 Резервная копия БД")
            except TelegramForbiddenError:
                await database.mark_user_unreachable(telegram_id)
            except Exception:
                logger.exception("run_backup_now: failed to send backup to %s", telegram_id)
        await database.mark_backup_done(datetime.utcnow())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def _backup_loop(bot: Bot, db_path: str) -> None:
    while True:
        await asyncio.sleep(constants.BACKUP_LOOP_INTERVAL_SECONDS)
        try:
            cfg = await database.get_backup_config()
            if not cfg.is_enabled:
                continue
            if cfg.last_backup_at:
                elapsed = datetime.utcnow() - utils.parse_utc(cfg.last_backup_at)
                if elapsed < timedelta(hours=cfg.interval_hours):
                    continue
            await run_backup_now(bot, db_path)
        except Exception:
            logger.exception("_backup_loop: failed")


async def run_all_polls(bot: Bot, db_path: str) -> list[asyncio.Task]:
    accounts = await database.list_avito_accounts(active_only=True)
    tasks = [asyncio.create_task(poll_account_loop(account, bot)) for account in accounts]
    tasks.append(asyncio.create_task(_reload_accounts_loop()))
    tasks.append(asyncio.create_task(_prune_messages_loop()))
    tasks.append(asyncio.create_task(_backup_loop(bot, db_path)))
    return tasks


async def stop_all(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
