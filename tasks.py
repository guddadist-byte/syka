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
import time
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
            # Nth cycle does a full unfiltered pass as a safety net: a chat
            # can drop out of Avito's own unread_only list (a human read it
            # directly in Avito's app) without this bot ever refetching its
            # messages in between, so our cached is_read (see bot_cache)
            # could lag up to one full-sync interval behind reality — an
            # acceptable, self-correcting bound (a few minutes), not the
            # "forever" staleness this whole area used to have.
            is_full_sync = cycle % constants.FULL_SYNC_EVERY_N_POLLS == 0
            unread_only = not is_full_sync

            chats: list[models.AvitoChat] = []
            offset = 0
            for _ in range(constants.CHAT_POLL_MAX_PAGES):
                page = await client.get_chats(unread_only=unread_only, limit=100, offset=offset)
                if not page:
                    break
                chats.extend(page)
                if len(page) < 100:
                    break
                offset += 100

            started = time.monotonic()
            for chat in chats:
                await _process_chat(chat, account, bot, client)
            elapsed = time.monotonic() - started

            # The only line that says how long a cycle actually took. Three
            # rounds of "notifications are late" were diagnosed by reasoning
            # about the code because the poller logged nothing at all on a
            # healthy run — a cycle that quietly grew to minutes looked
            # exactly like one that took a second.
            logger.info(
                "poll cycle done: account=%s chats=%d %.1fs%s",
                account.name, len(chats), elapsed, " (full sync)" if is_full_sync else "",
            )

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
                image_url=m.image_url,
                created_at=utils.parse_utc(m.sent_at),
                # Persisted since migration 015. Before it, this fell back
                # to CachedMessage's is_read=True default, which is what
                # made every chat look "already read" after a restart and
                # forced upsert_chat to leave last_message_at unset.
                is_read=bool(m.is_read),
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
        read_at = utils.parse_utc(chat.read_at) if chat.read_at else None
        await bot_cache.upsert_chat(
            chat.chat_id, point_id=chat.point_id, avito_account_id=chat.avito_account_id,
            client_name=chat.client_name or "", item_id=chat.item_id,
            initial_messages=initial_messages, read_at=read_at,
            summary_last_message_at=(
                utils.parse_utc(chat.last_message_at) if chat.last_message_at else None
            ),
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
    initial_messages: list[bot_cache.CachedMessage] = []
    read_at = None
    summary_last_at = None
    if not was_cached:
        initial_messages = await _build_initial_messages(chat.chat_id)
        summary = await database.get_chat_summary(chat.chat_id)
        if summary is not None and summary.read_at:
            read_at = utils.parse_utc(summary.read_at)
        if summary is not None and summary.last_message_at:
            summary_last_at = utils.parse_utc(summary.last_message_at)

    cached = await bot_cache.upsert_chat(
        chat.chat_id, point_id=point_id, avito_account_id=account.id,
        client_name=chat.client_name, item_id=chat.item_id,
        item_title=chat.item_title, item_url=chat.item_url,
        initial_messages=initial_messages, read_at=read_at,
        summary_last_message_at=summary_last_at,
    )
    incoming_last = utils.parse_utc(chat.last_message_at) if chat.last_message_at else None
    if (
        cached.last_message_at is not None and incoming_last is not None
        and incoming_last <= cached.last_message_at
    ):
        # Nothing new since the last message we already hold.
        if cached.unread_count == 0:
            # And nothing outstanding to double-check — skip the
            # get_messages() round trip entirely.
            return
        # We still count this chat as unread, so it's worth re-confirming:
        # a human can read an old message directly in Avito's own app
        # without ever sending a new one, and re-fetching is the only way
        # that flip is ever observed (real_unread_count's whole reason for
        # existing, see bot_cache).
        #
        # But that re-check used to happen on EVERY cycle, and a normal
        # cycle asks Avito precisely for the unread chats — so every chat
        # in the list failed this test and spent one throttled request,
        # every 15 seconds, for as long as it stayed unanswered. That made
        # the cycle grow with the unread backlog and is the mechanism
        # behind "messages started arriving late". Pace it instead: still
        # re-checked, just not eight times a minute.
        if (
            cached.last_polled_at is not None
            and (datetime.utcnow() - cached.last_polled_at).total_seconds()
            < constants.UNREAD_RECHECK_SECONDS
        ):
            return

    # Deliberately below the short-circuit, not above it. These two writes
    # used to run for EVERY chat in the list, including the ones that
    # returned two lines later with nothing changed — two commits each, and
    # SQLite here runs at the default synchronous=FULL, so each one is an
    # fsync, on the single connection the whole process shares. Past the
    # short-circuit we know something actually differs, so the write earns
    # its cost.
    await database.set_chat_unread_count(chat.chat_id, cached.unread_count)
    await database.upsert_chat_summary(
        chat.chat_id, avito_account_id=account.id, point_id=point_id, item_id=chat.item_id,
        client_name=chat.client_name, item_url=chat.item_url,
    )

    try:
        messages = await client.get_messages(chat.chat_id)
    except (avito_client.AvitoAPIError, asyncio.TimeoutError):
        # TimeoutError is not an AvitoAPIError and not an aiohttp
        # ClientError either, so without naming it here one stuck request
        # aborts the whole account's cycle into the 30..600s error backoff
        # — total silence for every other chat on that account.
        return
    await bot_cache.mark_polled(chat.chat_id)

    known_flags = await database.get_known_message_read_flags(chat.chat_id)

    # Collected instead of notifying inside the loop: after a restart (or
    # any gap in polling), several client messages can show up as "new" in
    # a single pass, and notifying once per message instead of once per
    # chat is exactly the "every message pings separately" bug reported.
    new_in_messages: list[bot_cache.CachedMessage] = []

    for message in messages:
        created_at = utils.parse_utc(message.created_at) if message.created_at else datetime.utcnow()
        cached_message = bot_cache.CachedMessage(
            avito_message_id=message.message_id,
            direction=message.direction,
            text=message.text,
            has_image=message.has_image,
            image_url=message.image_url,
            created_at=created_at,
            is_read=message.is_read,
        )
        is_new = await bot_cache.add_message(chat.chat_id, cached_message)

        if message.message_id is not None and message.message_id in known_flags:
            if message.image_url:
                # Backfill only — messages persisted before the image_url
                # column existed never pass the append_message() below.
                await database.set_message_image_url(message.message_id, message.image_url)
            if known_flags[message.message_id] != message.is_read:
                # Same idea for is_read: this message will never be
                # re-inserted, so if Avito has flipped its flag since, only
                # this write keeps the durable row honest — and the durable
                # row is what the next restart hydrates from. Guarded by the
                # comparison because an unguarded UPDATE here would commit
                # once per already-known message per cycle, on the single
                # shared connection the whole process writes through.
                await database.set_message_is_read(message.message_id, message.is_read)
            # Already persisted before a restart — bot_cache is in-memory
            # and resets on every restart, so without this DB-backed check
            # every message in Avito's recent history would look "new"
            # again on the first poll after a restart and re-notify.
            # add_message() above still ran though, so a message we
            # already knew about but that flipped is_read since last time
            # gets that refreshed regardless of this continue.
            continue

        if not is_new:
            continue

        sent_at_str = created_at.strftime("%Y-%m-%d %H:%M:%S")
        await database.append_message(
            chat.chat_id, message.direction, message.text, message.has_image,
            sent_at=sent_at_str, avito_message_id=message.message_id,
            image_url=message.image_url, is_read=message.is_read,
        )

        if message.direction != "in":
            continue

        await database.upsert_chat_summary(
            chat.chat_id, avito_account_id=account.id, point_id=point_id,
            last_message_at=sent_at_str, last_message_text=message.text, last_message_dir="in",
        )

        if message.is_read:
            # Avito itself says this message has already been read, so by
            # definition it isn't something awaiting a reply right now —
            # whoever read it, whenever. Never notify about it, not even
            # the first time we see it: known_ids only covers what this bot
            # persisted itself, while a chat's history on Avito predates us
            # (and reaches further back than the 50 messages hydration
            # restores). Without this check the first full sync after a
            # restart walks the entire old archive and pings about every
            # long-since-answered conversation in it. It is still persisted
            # above — the archive backfills silently, it just doesn't ring.
            continue

        new_in_messages.append(cached_message)

    if new_in_messages:
        # Fire it off rather than awaiting it here. This runs inside
        # poll_account_loop's `for chat in chats` walk, so awaiting meant
        # every Telegram round trip — now through a SOCKS5 proxy, with
        # aiogram's 60s default timeout and no retry middleware — held up
        # the NEXT chat's get_messages. The Avito budget sat idle while the
        # loop waited on Telegram. Notifying is not something the poll
        # cycle needs the result of.
        task = asyncio.create_task(
            _notify_subscribers(chat.chat_id, point_id, cached, new_in_messages, bot)
        )
        # Keep a reference: asyncio only holds a weak one, so without this
        # a notification can be garbage-collected mid-flight.
        _notify_tasks.add(task)
        task.add_done_callback(_notify_tasks.discard)

    # unread_count may have changed during the loop above — a new inbound
    # message, or an "out" reply sent directly in Avito's own app rather
    # than through this bot — persist the final, self-corrected value.
    final_chat = await bot_cache.get_chat(chat.chat_id)
    if final_chat is not None:
        await database.set_chat_unread_count(chat.chat_id, final_chat.unread_count)


# In-flight notification tasks. asyncio keeps only a weak reference to a
# task, so one that nothing holds can be collected before it finishes.
_notify_tasks: set[asyncio.Task] = set()


async def _send_notification(bot: Bot, telegram_id: int, text: str, short_id: str) -> None:
    try:
        await bot.send_message(telegram_id, text, reply_markup=keyboards.chat_notification_kb(short_id))
    except TelegramForbiddenError:
        await database.mark_user_unreachable(telegram_id)
    except Exception:
        logger.exception("_send_notification: failed to notify %s", telegram_id)


async def _notify_subscribers(chat_id: str, point_id: int | None, cached_chat: bot_cache.CachedChat,
                               messages: list[bot_cache.CachedMessage], bot: Bot) -> None:
    short_id = await bot_cache.get_short_id(chat_id)
    client_name = html.escape(cached_chat.client_name or "клиент")
    last_message = messages[-1]
    # Not "(фото)": this fallback fires for ANY text-less message, so a
    # voice message used to be announced as a photo. Only claim a type the
    # parser actually identified (see avito_client.get_messages, which
    # already labels voice/call/link and leaves text empty only for a real
    # picture).
    if last_message.text:
        preview = html.escape(last_message.text[:200])
    elif last_message.image_url or last_message.has_image:
        preview = "📷 Фото"
    else:
        preview = "📎 Вложение"
    if cached_chat.item_title and cached_chat.item_url:
        item_line = f'📦 <a href="{html.escape(cached_chat.item_url)}">{html.escape(cached_chat.item_title)}</a>'
    elif cached_chat.item_title:
        item_line = f"📦 {html.escape(cached_chat.item_title)}"
    else:
        item_line = "📦 Сообщение в профиль (без объявления)"
    # Nobody has ever replied in this chat -> every message in it so far is
    # a brand-new lead, not just another message in an existing
    # conversation, even if the client sent several before anyone answered.
    is_new_lead = bool(cached_chat.messages) and all(m.direction == "in" for m in cached_chat.messages)
    if len(messages) > 1:
        header = (f"🆕 Новый клиент! {client_name} ({len(messages)} сообщения)" if is_new_lead
                  else f"📩 {len(messages)} новых сообщений от {client_name}")
    else:
        header = f"🆕 Новый клиент! {client_name}" if is_new_lead else f"📩 Новое сообщение от {client_name}"
    text = f"{header}\n{item_line}\n\n{preview}"

    if point_id is None:
        # Fully unresolved chat (coords present but no matching point) —
        # per explicit request, notifications are strictly opt-in via
        # "📍 Мои точки" with no exceptions, and nobody can be subscribed
        # to a point that doesn't exist yet, so this stays silent. It's
        # still visible for review in "📭 Чаты без точки".
        return

    recipients = {u.telegram_id: u for u in await database.list_point_subscribers(point_id, on_shift_only=True)}
    # Concurrently, not one after another: a point with five people on shift
    # used to cost five sequential Telegram round trips, and since Telegram
    # moved behind a SOCKS5 proxy each of those is far from free.
    # _send_notification swallows its own errors, so gather cannot raise.
    await asyncio.gather(*(_send_notification(bot, uid, text, short_id) for uid in recipients))


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


async def _notify_new_order(bot: Bot, order: dict, account_id: int) -> None:
    point_id = await database.resolve_order_point_id(order, avito_account_id=account_id)
    if point_id is None:
        # Same policy as chat notifications: no resolvable point -> stay
        # silent rather than guess-broadcast. The order is not lost: the
        # "📦 Заказы Avito" screens list it under "точка не определена".
        # Give the cabinet a default point (Настройки → Avito API) and
        # orders like this route — and notify — on their own.
        return

    # Unlike chat notifications, deliberately NOT limited to on-shift
    # staff: an order carries a shipping deadline and is announced exactly
    # once (see _orders_poll_loop's seen-ids dedup), so one placed
    # overnight or on a day off would otherwise reach nobody, ever.
    recipients = await database.list_point_subscribers(point_id, on_shift_only=False)
    if not recipients:
        return

    status_label = constants.ORDER_STATUS_LABELS.get(order.get("status", ""), order.get("status", ""))
    items = order.get("items") or []
    titles = ", ".join(html.escape(item.get("title", "")) for item in items) or "(без названия)"
    total = (order.get("prices") or {}).get("total")
    text = f"📦 Новый заказ Avito Доставки\n{status_label}\n{titles}"
    if total is not None:
        text += f"\n💰 {total} ₽"

    kb = keyboards.order_notification_kb(order.get("id"), account_id)
    for user in recipients:
        try:
            await bot.send_message(user.telegram_id, text, reply_markup=kb)
        except TelegramForbiddenError:
            await database.mark_user_unreachable(user.telegram_id)
        except Exception:
            logger.exception("_notify_new_order: failed to notify %s", user.telegram_id)


async def _orders_poll_loop(bot: Bot) -> None:
    # The first pass runs immediately instead of after a sleep. It does two
    # useful things at startup: it warms the order cache the screens read
    # (otherwise the first order list opened within the poll interval of a
    # restart pays for a full paginated fetch), and it gets orders that
    # arrived during the downtime notified that much sooner. Moving it
    # earlier cannot cause a re-notification: dedup lives in the
    # seen_avito_orders table, which survives the restart.
    while True:
        try:
            accounts = await database.list_avito_accounts(active_only=True)
            for account in accounts:
                client = avito_client.get_pool().get(account.id)
                if client is None:
                    continue
                try:
                    # Deliberately NOT use_cache=True: this loop exists to
                    # spot orders that appeared since the last pass, and a
                    # cached copy would delay every new-order notification
                    # by up to ORDERS_CACHE_TTL_SECONDS. It still refreshes
                    # the cache for the interactive screens as a side
                    # effect, so they usually read data at most one poll
                    # interval old.
                    orders = await client.get_orders(statuses=constants.ORDER_ACTIVE_STATUSES)
                except avito_client.AvitoAPIError:
                    continue
                seen_ids = await database.get_seen_order_ids(account.id)
                # First pass ever for this account (fresh DB, or an account
                # only just added): every currently-active order would look
                # brand new and blast one notification each — with ~100
                # active orders per account that's a flood, not news. Adopt
                # them silently instead; genuinely new orders notify from
                # the next pass on.
                is_first_pass = not seen_ids
                for order in orders:
                    order_id = order.get("id")
                    if order_id is None or str(order_id) in seen_ids:
                        continue
                    # Marked before notifying on purpose: guarantees at
                    # most one notification per order even if sending blows
                    # up midway.
                    await database.mark_order_seen(str(order_id), account.id)
                    if is_first_pass:
                        continue
                    await _notify_new_order(bot, order, account.id)
        except Exception:
            logger.exception("_orders_poll_loop: failed")
        await asyncio.sleep(constants.ORDER_POLL_INTERVAL_SECONDS)


async def _send_scheduled_reply(bot: Bot, item: models.ScheduledReply) -> None:
    """Deliver one claimed reply and run the same post-send steps as a live one.

    Everything after the send mirrors handlers.receive_reply_text /
    webapp.api_chat_reply exactly — a scheduled reply is a real reply, so it
    has to land in the cache, in the message history, clear the chat's unread
    state and earn its author the same rating point. Skipping any of those
    would leave the chat looking unanswered right after answering it.
    """
    client = avito_client.get_pool().get(item.avito_account_id)
    if client is None:
        await database.mark_scheduled_reply_failed(item.id, "Avito-аккаунт недоступен")
        await _tell_author(bot, item, "⚠️ Отложенный ответ не отправлен: аккаунт Avito недоступен.")
        return

    try:
        sent = await client.send_text_message(item.chat_id, item.text, item.message_uuid)
    except avito_client.AvitoAPIError as exc:
        await database.mark_scheduled_reply_failed(item.id, str(exc))
        await _tell_author(bot, item, f"⚠️ Отложенный ответ не отправлен: {exc}\n\nОтветьте клиенту вручную.")
        return

    now = datetime.utcnow()
    await bot_cache.add_message(
        item.chat_id,
        bot_cache.CachedMessage(
            avito_message_id=sent.message_id, direction="out", text=item.text,
            has_image=False, created_at=now,
        ),
    )
    if item.author_id is not None:
        await bot_cache.mark_replied(item.chat_id, item.author_id)
        await database.mark_chat_replied(item.chat_id, item.author_id)
        await database.increment_rating(item.author_id)
    await database.append_message(
        item.chat_id, "out", item.text, False,
        sent_at=now.strftime("%Y-%m-%d %H:%M:%S"), avito_message_id=sent.message_id,
    )
    try:
        await client.mark_chat_read(item.chat_id)
    except avito_client.AvitoAPIError:
        pass
    await database.mark_scheduled_reply_sent(item.id)

    short_id = await bot_cache.get_short_id(item.chat_id)
    await _tell_author(
        bot, item,
        f"✅ Отложенный ответ отправлен:\n\n<i>{html.escape(item.text[:200])}</i>",
        reply_markup=keyboards.chat_notification_kb(short_id) if short_id else None,
    )


async def _tell_author(bot: Bot, item: models.ScheduledReply, text: str, reply_markup=None) -> None:
    if item.author_id is None:
        return
    try:
        await bot.send_message(item.author_id, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        await database.mark_user_unreachable(item.author_id)
    except Exception:
        logger.exception("_tell_author: failed to notify %s", item.author_id)


async def _scheduled_replies_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(constants.SCHEDULED_REPLY_CHECK_INTERVAL_SECONDS)
        try:
            for item in await database.list_due_scheduled_replies(datetime.utcnow()):
                # Claim before sending: the claim is what makes "at most one
                # send" true, so nothing may happen before it succeeds.
                if not await database.claim_scheduled_reply(item.id):
                    continue
                try:
                    await _send_scheduled_reply(bot, item)
                except Exception:
                    logger.exception("_scheduled_replies_loop: failed to send %s", item.id)
                    await database.mark_scheduled_reply_failed(item.id, "внутренняя ошибка")
        except Exception:
            logger.exception("_scheduled_replies_loop: failed")


async def _heartbeat_loop() -> None:
    while True:
        try:
            await database.touch_heartbeat()
        except Exception:
            logger.exception("_heartbeat_loop: failed")
        await asyncio.sleep(constants.HEARTBEAT_INTERVAL_SECONDS)


def _downtime_line(cfg: models.StartupNotifyConfig, now: datetime) -> str:
    """How long the bot was actually away, and whether it left on its own.

    "last_stopped_at newer than the last heartbeat" means shutdown ran — a
    deploy. Otherwise the process died between heartbeats without getting to
    its shutdown path, which is the case worth waking up for.
    """
    if not cfg.last_heartbeat_at:
        return "🔹 Первый запуск — простой посчитать не с чем"
    last_seen = utils.parse_utc(cfg.last_heartbeat_at)
    minutes = max(0, int((now - last_seen).total_seconds() // 60))
    graceful = bool(cfg.last_stopped_at) and utils.parse_utc(cfg.last_stopped_at) >= last_seen
    how = "штатная остановка" if graceful else "⚠️ аварийная остановка"
    return f"🔹 Простой: {minutes} мин ({how})"


async def build_startup_report(*, webapp_enabled: bool, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    cfg = await database.get_startup_notify_config()

    accounts = await database.list_avito_accounts(active_only=True)
    broken = [a for a in accounts if a.last_poll_error]
    total_chats, unread = await bot_cache.count_chats()

    lines = [
        "✅ <b>Бот запущен</b>",
        f"🔹 Время: {utils.to_msk(now).strftime('%d.%m.%Y %H:%M')} МСК",
        _downtime_line(cfg, now),
        "",
        f"🔹 Схема БД: версия {await database.get_schema_version()}",
        f"🔹 Avito-аккаунты: {len(accounts)} активных"
        + (f", ⚠️ с ошибкой опроса: {len(broken)}" if broken else ""),
        f"🔹 Чатов в памяти: {total_chats}, из них непрочитанных: {unread}",
        f"🔹 Сотрудников на смене: {await database.count_on_shift()}",
        f"🔹 Отложенных ответов в очереди: {await database.count_pending_scheduled_replies()}",
        f"🔹 Мини-приложение: {'включено' if webapp_enabled else 'выключено'}",
    ]
    if broken:
        lines.append("")
        for account in broken:
            lines.append(f"⚠️ {html.escape(account.name)}: {html.escape(str(account.last_poll_error)[:120])}")
    return "\n".join(lines)


async def send_startup_report(bot: Bot, *, webapp_enabled: bool) -> None:
    """Tell the owners the bot is back up. Never lets a failure here stop the
    bot from starting — a status message is not worth a boot loop."""
    try:
        cfg = await database.get_startup_notify_config()
        if not cfg.is_enabled:
            return
        if cfg.recipient_telegram_id:
            recipients = [cfg.recipient_telegram_id]
        else:
            recipients = [
                u.telegram_id
                for u in await database.list_admins_and_directors()
                if u.role == constants.DIRECTOR
            ]
        if not recipients:
            return
        text = await build_startup_report(webapp_enabled=webapp_enabled)
        for telegram_id in recipients:
            try:
                await bot.send_message(telegram_id, text)
            except TelegramForbiddenError:
                await database.mark_user_unreachable(telegram_id)
            except Exception:
                logger.exception("send_startup_report: failed to notify %s", telegram_id)
    except Exception:
        logger.exception("send_startup_report: failed")


async def run_all_polls(bot: Bot, db_path: str) -> list[asyncio.Task]:
    accounts = await database.list_avito_accounts(active_only=True)
    tasks = [asyncio.create_task(poll_account_loop(account, bot)) for account in accounts]
    tasks.append(asyncio.create_task(_reload_accounts_loop()))
    tasks.append(asyncio.create_task(_prune_messages_loop()))
    tasks.append(asyncio.create_task(_backup_loop(bot, db_path)))
    tasks.append(asyncio.create_task(_orders_poll_loop(bot)))
    tasks.append(asyncio.create_task(_scheduled_replies_loop(bot)))
    tasks.append(asyncio.create_task(_heartbeat_loop()))
    return tasks


async def drain_notifications(timeout: float = 10.0) -> None:
    """Waits for notifications already in flight.

    Notifying is dispatched as a task so Telegram's latency stays off the
    poll loop's critical path (see _process_chat). The flip side is that
    nothing otherwise owns those tasks: on shutdown the loop would close
    underneath them and a notification the client is waiting on would be
    destroyed mid-send. Also used by tests, which need the same guarantee
    before asserting that something was delivered."""
    pending = list(_notify_tasks)
    if not pending:
        return
    await asyncio.wait(pending, timeout=timeout)


async def stop_all(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # After the pollers stop, before the loop goes away.
    await drain_notifications()
