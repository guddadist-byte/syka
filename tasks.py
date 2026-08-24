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
    while True:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            await asyncio.sleep(constants.POLL_INTERVAL_SECONDS)
            continue
        try:
            chats = await client.get_chats()
            for chat in chats:
                await _process_chat(chat, account, bot, client)
            await database.set_avito_account_error(account.id, None)
            backoff = constants.ERROR_BACKOFF_BASE_SECONDS
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


async def _process_chat(chat: models.AvitoChat, account: models.AvitoAccount, bot: Bot,
                         client: "avito_client.AvitoClient") -> None:
    # Confirmed against a live account: the chat-list response already
    # embeds the ad's coordinates (context.value.location), no separate
    # item lookup needed. resolve_point_for_item handles both a missing
    # item_id (direct-to-profile message) and an item with no coordinates
    # by routing to the fallback point itself.
    point = await database.resolve_point_for_item(chat.item_id, chat.item_lat, chat.item_lon)
    point_id = point.id if point else None

    # Read the durable count before touching bot_cache — if this chat isn't
    # in the in-memory cache yet (e.g. right after a restart), upsert_chat
    # seeds its unread_count from this instead of defaulting to 0.
    existing_summary = await database.get_chat_summary(chat.chat_id)
    initial_unread = existing_summary.unread_count if existing_summary else 0

    cached = await bot_cache.upsert_chat(
        chat.chat_id, point_id=point_id, avito_account_id=account.id,
        client_name=chat.client_name, item_id=chat.item_id,
        item_title=chat.item_title, item_url=chat.item_url,
        initial_unread_count=initial_unread,
    )
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

        live_chat = await bot_cache.get_chat(chat.chat_id)
        await database.set_chat_unread_count(chat.chat_id, live_chat.unread_count if live_chat else 1)
        await database.upsert_chat_summary(
            chat.chat_id, avito_account_id=account.id, point_id=point_id,
            last_message_at=sent_at_str, last_message_text=message.text, last_message_dir="in",
        )
        await _notify_subscribers(chat.chat_id, point_id, cached, cached_message, bot)


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

    recipients: dict[int, models.User] = {}
    if point_id is None:
        # Fully unresolved chat (coords present but no matching point) —
        # nobody could have subscribed to a point that doesn't exist yet,
        # so this still falls back to on-shift admins/directors so someone
        # sees it and can resolve it manually via "📭 Чаты без точки".
        for user in await database.list_admins_and_directors():
            if user.on_shift and not user.blocked_bot:
                recipients[user.telegram_id] = user
    else:
        # Strictly by subscription — no unconditional director exception,
        # per explicit user request: no subscription to the point means
        # nothing arrives, including for admins/directors.
        for user in await database.list_point_subscribers(point_id, on_shift_only=True):
            recipients[user.telegram_id] = user

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
