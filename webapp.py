"""Telegram Mini App backend — same process, same DB/cache as the bot.

Auth is Telegram's own WebApp initData (HMAC on BOT_TOKEN), not a custom
session store — there is no user-facing web login here, only Telegram's.
Every route below is a thin wrapper around the same database.py/
bot_cache.py/avito_client.py functions handlers.py already calls for the
bot UI — no forked business logic, and both surfaces share the exact same
aiosqlite connection and in-memory chat cache.

Registration and Telegram Stars payment stay entirely on the bot side —
those are native Telegram primitives with no REST equivalent, and an
unapproved user is simply told to go talk to the bot.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BufferedInputFile, FSInputFile
from aiohttp import web

import ai_client
import avito_client
import bot_cache
import constants
import database
import guardrail
import models
import utils

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "webapp_static"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
INIT_DATA_MAX_AGE_SECONDS = 86400


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = INIT_DATA_MAX_AGE_SECONDS) -> dict | None:
    """Verifies Telegram's WebApp initData HMAC signature and freshness.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    Returns the parsed key/value pairs (with "user" still a raw JSON
    string) if valid, None otherwise.
    """
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed_hash, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", 0))
    except ValueError:
        return None
    if time.time() - auth_date > max_age_seconds:
        return None
    return parsed


def _extract_telegram_id(parsed_init_data: dict) -> int | None:
    user_json = parsed_init_data.get("user")
    if not user_json:
        return None
    try:
        user_obj = json.loads(user_json)
    except ValueError:
        return None
    telegram_id = user_obj.get("id")
    return int(telegram_id) if telegram_id is not None else None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)

    init_data = request.headers.get(INIT_DATA_HEADER, "")
    parsed = validate_init_data(init_data, request.app["bot_token"])
    if parsed is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    telegram_id = _extract_telegram_id(parsed)
    if telegram_id is None:
        return web.json_response({"error": "unauthorized"}, status=401)

    user = await database.get_user(telegram_id)
    if user is None:
        return web.json_response({"error": "not_registered"}, status=403)
    if user.status != constants.STATUS_APPROVED:
        return web.json_response({"error": "not_approved", "status": user.status}, status=403)
    if user.blocked_bot:
        return web.json_response({"error": "blocked"}, status=403)

    request["user"] = user
    return await handler(request)


async def _point_ids_for_user(user: models.User) -> set[int]:
    """Mirrors handlers._point_ids_for_user — same strict "Мои точки"
    subscription rule for every role, kept as a local copy rather than an
    import to avoid a new cross-module dependency for one small helper."""
    if user.role == constants.MANAGER and user.responsible_point_id:
        return {user.responsible_point_id}
    points = await database.get_user_points(user.telegram_id)
    return {p.id for p in points}


def _has_role_at_least(user: models.User, min_role: str) -> bool:
    return constants.ROLE_ORDER.get(user.role, 0) >= constants.ROLE_ORDER[min_role]


def _require_admin(request: web.Request) -> models.User | None:
    """Mirrors RoleAtLeast(constants.ADMIN) — every /api/admin/* route calls
    this first and returns its result (None means "proceed", a Response
    means "stop here and return this"), matching admin_router's own gate."""
    user: models.User = request["user"]
    if not _has_role_at_least(user, constants.ADMIN):
        return web.json_response({"error": "forbidden"}, status=403)
    return None


def _label(user: models.User) -> str:
    return user.full_name or user.username or str(user.telegram_id)


def _serialize_user(user: models.User) -> dict:
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "full_name": user.full_name,
        "last_name": user.last_name,
        "role": user.role,
        "role_label": constants.ROLE_LABELS.get(user.role, user.role),
        "status": user.status,
        "on_shift": bool(user.on_shift),
        "responsible_point_id": user.responsible_point_id,
        "blocked_bot": bool(user.blocked_bot),
        "rating_points": user.rating_points,
        "trade_point_name": user.trade_point_name,
        "created_at": user.created_at,
    }


def _serialize_point(point: models.Point) -> dict:
    return {
        "id": point.id,
        "name": point.name,
        "code": point.code,
        "address": point.address,
        "working_hours": point.working_hours,
        "is_active": bool(point.is_active),
        "name_is_custom": bool(point.name_is_custom),
    }


def _serialize_avito_account(acc: models.AvitoAccount) -> dict:
    return {
        "id": acc.id,
        "name": acc.name,
        "client_id": acc.client_id,
        "avito_user_id": acc.avito_user_id,
        "is_active": bool(acc.is_active),
        "has_token": bool(acc.access_token),
        "last_poll_error": acc.last_poll_error,
    }


def _serialize_template(t: models.Template) -> dict:
    return {"id": t.id, "kind": t.kind, "title": t.title, "body": t.body}


def _serialize_message(m: bot_cache.CachedMessage) -> dict:
    return {
        "direction": m.direction,
        "text": m.text,
        "has_image": m.has_image,
        "created_at": m.created_at.isoformat(),
    }


def _serialize_chat(chat: bot_cache.CachedChat, *, with_messages: bool = False) -> dict:
    data = {
        "short_id": chat.short_id,
        "client_name": chat.client_name,
        "item_title": chat.item_title,
        "item_url": chat.item_url,
        "unread_count": chat.unread_count,
        "is_new_lead": bool(chat.messages) and all(m.direction == "in" for m in chat.messages),
        "last_message_at": chat.last_message_at.isoformat() if chat.last_message_at else None,
        "last_replied_at": chat.last_replied_at.isoformat() if chat.last_replied_at else None,
    }
    if with_messages:
        data["messages"] = [_serialize_message(m) for m in chat.messages]
    return data


# --- routes: profile / shift -------------------------------------------------


async def api_me(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    points = await database.get_user_points(user.telegram_id)
    return web.json_response({
        "telegram_id": user.telegram_id,
        "full_name": user.full_name,
        "role": user.role,
        "role_label": constants.ROLE_LABELS.get(user.role, user.role),
        "on_shift": bool(user.on_shift),
        "rating_points": user.rating_points,
        "is_admin": _has_role_at_least(user, constants.ADMIN),
        "is_manager_or_above": _has_role_at_least(user, constants.MANAGER),
        "points": [{"id": p.id, "name": p.name} for p in points],
    })


async def api_shift(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    body = await request.json()
    on_shift = bool(body.get("on_shift"))
    await database.set_shift(user.telegram_id, on_shift)
    return web.json_response({"on_shift": on_shift})


async def api_profile(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    points = await database.get_user_points(user.telegram_id)
    leaderboard = await database.get_leaderboard(limit=10)
    rank = await database.get_leaderboard_rank(user.telegram_id)
    return web.json_response({
        "full_name": user.full_name,
        "role_label": constants.ROLE_LABELS.get(user.role, user.role),
        "on_shift": bool(user.on_shift),
        "rating_points": user.rating_points,
        "points": [p.name for p in points],
        "created_at": user.created_at,
        "leaderboard": [
            {"full_name": u.full_name, "rating_points": pts, "is_me": u.telegram_id == user.telegram_id}
            for u, pts in leaderboard
        ],
        "my_rank": rank,
    })


# --- routes: points -----------------------------------------------------------


async def api_points_all(request: web.Request) -> web.Response:
    points = await database.list_points(active_only=True)
    return web.json_response({"points": [{"id": p.id, "name": p.name, "address": p.address} for p in points]})


async def api_points_mine(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    points = await database.get_user_points(user.telegram_id)
    return web.json_response({"point_ids": [p.id for p in points]})


async def api_points_subscribe(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    body = await request.json()
    point_id = int(body["point_id"])
    if bool(body.get("subscribed")):
        await database.subscribe_user_to_point(user.telegram_id, point_id)
    else:
        await database.unsubscribe_user_from_point(user.telegram_id, point_id)
    return web.json_response({"ok": True})


# --- routes: chats -------------------------------------------------------------


async def api_chats(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    point_ids = await _point_ids_for_user(user)
    filter_ = request.query.get("filter", "unread")
    if filter_ == "recent":
        chats = await bot_cache.get_recent_replies_for_points(
            point_ids, timedelta(minutes=constants.RECENT_REPLIES_WINDOW_MINUTES)
        )
    else:
        chats = await bot_cache.get_unread_for_points(point_ids)
    return web.json_response({"chats": [_serialize_chat(c) for c in chats]})


async def _refresh_chat_from_avito(chat: bot_cache.CachedChat) -> None:
    """Mirrors handlers.py's _refresh_chat_from_avito — kept as a separate
    copy rather than a shared import since webapp.py and handlers.py are
    deliberately independent of each other (see the project's import
    graph). Live-syncs is_read from Avito for messages this chat already
    knows about, since background polling short-circuits chats it already
    believes are fully read to save API calls — fine for the ambient list,
    but a chat someone is actually opening deserves the real current state.

    Deliberately uses bot_cache.sync_is_read(), NOT add_message(): this
    path has no durable known_ids/database.append_message() pairing of its
    own (that lives in tasks._process_chat), so it must never be the thing
    that first discovers a brand-new message — doing so previously made
    such a message "known" only in memory, invisible to the DB, and it
    would resurface as a false "new" message and re-notify after the next
    restart. A genuinely new message is picked up by the next poll cycle
    (seconds away) exactly as before."""
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        return
    try:
        messages = await client.get_messages(chat.chat_id)
    except avito_client.AvitoAPIError:
        return
    for m in messages:
        if m.message_id is not None:
            await bot_cache.sync_is_read(chat.chat_id, m.message_id, m.is_read)


async def api_chat_detail(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)
    await _refresh_chat_from_avito(chat)
    return web.json_response(_serialize_chat(chat, with_messages=True))


async def api_chat_read(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)
    await bot_cache.mark_read(chat.chat_id)
    await database.set_chat_unread_count(chat.chat_id, 0)
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is not None:
        try:
            await client.mark_chat_read(chat.chat_id)
        except avito_client.AvitoAPIError:
            pass
    return web.json_response({"ok": True})


async def api_chat_reply(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)

    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty_text"}, status=400)

    if not await bot_cache.try_claim_action(f"send:{chat.chat_id}"):
        return web.json_response({"error": "already_sending"}, status=409)

    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)
    try:
        sent = await client.send_text_message(chat.chat_id, text)
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)

    now = datetime.utcnow()
    await bot_cache.add_message(
        chat.chat_id,
        bot_cache.CachedMessage(avito_message_id=sent.message_id, direction="out", text=text, has_image=False, created_at=now),
    )
    await bot_cache.mark_replied(chat.chat_id, user.telegram_id)
    await database.append_message(
        chat.chat_id, "out", text, False, sent_at=now.strftime("%Y-%m-%d %H:%M:%S"), avito_message_id=sent.message_id
    )
    await database.mark_chat_replied(chat.chat_id, user.telegram_id)
    await database.increment_rating(user.telegram_id)
    try:
        await client.mark_chat_read(chat.chat_id)
    except avito_client.AvitoAPIError:
        pass
    msg_ref = await bot_cache.register_sent_message(chat.chat_id, sent.message_id or "")
    return web.json_response({"ok": True, "msg_ref": msg_ref})


async def api_chat_reply_photo(request: web.Request) -> web.Response:
    """Multipart upload of one or more photos as a single logical reply —
    the web equivalent of handlers._send_photos, minus the media-group
    debounce (a browser file picker already hands us every file in one
    request, so there is no Telegram-side "several updates" problem to
    coalesce)."""
    user: models.User = request["user"]
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)

    photos: list[bytes] = []
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "photos":
            photos.append(await part.read(decode=False))
    if not photos:
        return web.json_response({"error": "no_photos"}, status=400)

    if not await bot_cache.try_claim_action(f"send:{chat.chat_id}"):
        return web.json_response({"error": "already_sending"}, status=409)

    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)

    sent_count = 0
    last_avito_message_id = ""
    for i, photo_bytes in enumerate(photos):
        try:
            image_id = await client.upload_image(photo_bytes, filename=f"webapp-{int(time.time())}-{i}.jpg")
            sent = await client.send_image_message(chat.chat_id, image_id)
        except avito_client.AvitoAPIError:
            continue
        sent_count += 1
        last_avito_message_id = sent.message_id or ""
        now = datetime.utcnow()
        await bot_cache.add_message(
            chat.chat_id,
            bot_cache.CachedMessage(avito_message_id=sent.message_id, direction="out", text="", has_image=True, created_at=now),
        )
        await database.append_message(
            chat.chat_id, "out", None, True, sent_at=now.strftime("%Y-%m-%d %H:%M:%S"), avito_message_id=sent.message_id
        )

    if sent_count == 0:
        return web.json_response({"error": "avito_rejected"}, status=502)

    await bot_cache.mark_replied(chat.chat_id, user.telegram_id)
    await database.mark_chat_replied(chat.chat_id, user.telegram_id)
    await database.increment_rating(user.telegram_id)
    try:
        await client.mark_chat_read(chat.chat_id)
    except avito_client.AvitoAPIError:
        pass
    msg_ref = await bot_cache.register_sent_message(chat.chat_id, last_avito_message_id)
    return web.json_response({"ok": True, "sent_count": sent_count, "msg_ref": msg_ref})


async def api_message_delete(request: web.Request) -> web.Response:
    msg_ref = request.match_info["msg_ref"]
    resolved = await bot_cache.resolve_sent_message(msg_ref)
    if resolved is None:
        return web.json_response({"error": "not_found"}, status=404)
    chat_id, avito_message_id = resolved
    chat = await bot_cache.get_chat(chat_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)
    try:
        await client.delete_message(chat_id, avito_message_id)
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)
    return web.json_response({"ok": True})


# --- routes: AI drafts / templates for a chat -----------------------------


async def api_chat_ai_draft(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)
    if chat.point_id is None:
        return web.json_response({"error": "no_point"}, status=400)
    point = await database.get_point(chat.point_id)
    if point is None:
        return web.json_response({"error": "no_point"}, status=400)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    prompt_override = (body.get("prompt") or "").strip() or None
    try:
        draft, flagged = await guardrail.guarded_generate(list(chat.messages), point, prompt_override=prompt_override)
    except ai_client.AIClientError:
        return web.json_response({"error": "ai_unavailable"}, status=502)
    return web.json_response({"draft": draft, "allow_send": not flagged})


async def api_chat_templates(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None or chat.point_id is None:
        return web.json_response({"templates": []})
    templates = await database.list_templates(chat.point_id)
    return web.json_response({"templates": [_serialize_template(t) for t in templates]})


async def api_chat_template_apply(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    template_id = int(request.match_info["template_id"])
    chat = await bot_cache.resolve_chat(short_id)
    template = await database.get_template(template_id)
    if chat is None or template is None:
        return web.json_response({"error": "not_found"}, status=404)

    if template.kind == constants.TEMPLATE_TEXT:
        draft = await _apply_point_placeholders(template.body)
        return web.json_response({"draft": draft, "allow_send": True})

    if chat.point_id is None:
        return web.json_response({"error": "no_point"}, status=400)
    point = await database.get_point(chat.point_id)
    if point is None:
        return web.json_response({"error": "no_point"}, status=400)
    prompt_override = await _apply_point_placeholders(template.body)
    try:
        draft, flagged = await guardrail.guarded_generate(list(chat.messages), point, prompt_override=prompt_override)
    except ai_client.AIClientError:
        return web.json_response({"error": "ai_unavailable"}, status=502)
    return web.json_response({"draft": draft, "allow_send": not flagged})


async def _apply_point_placeholders(text: str) -> str:
    """Mirrors handlers._apply_point_placeholders exactly (!<CODE>А / !<CODE>В)."""
    if "!" not in text:
        return text
    for point in await database.list_points(active_only=False):
        for key in filter(None, {point.code, point.name}):
            text = text.replace(f"!{key}А", point.address or "")
            text = text.replace(f"!{key}В", point.working_hours or "")
    return text


# --- routes: my own point templates (📋 Мои шаблоны, manager role) --------


async def api_templates_mine(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    if not user.responsible_point_id:
        return web.json_response({"templates": []})
    templates = await database.list_templates(user.responsible_point_id)
    return web.json_response({"templates": [_serialize_template(t) for t in templates]})


async def api_templates_mine_create(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    if not _has_role_at_least(user, constants.MANAGER):
        return web.json_response({"error": "forbidden"}, status=403)
    if not user.responsible_point_id:
        return web.json_response({"error": "no_responsible_point"}, status=400)
    body = await request.json()
    kind = body.get("kind") if body.get("kind") in (constants.TEMPLATE_TEXT, constants.TEMPLATE_AI_PROMPT) else constants.TEMPLATE_TEXT
    title = (body.get("title") or "").strip() or "Шаблон"
    text = (body.get("body") or "").strip()
    if not text:
        return web.json_response({"error": "empty_body"}, status=400)
    template = await database.create_template(
        point_id=user.responsible_point_id, kind=kind, title=title, body=text, created_by=user.telegram_id
    )
    return web.json_response(_serialize_template(template))


async def api_templates_mine_delete(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    if not _has_role_at_least(user, constants.MANAGER):
        return web.json_response({"error": "forbidden"}, status=403)
    template_id = int(request.match_info["template_id"])
    await database.deactivate_template(template_id)
    return web.json_response({"ok": True})


# --- routes: orders ------------------------------------------------------------


async def api_orders(request: web.Request) -> web.Response:
    user: models.User = request["user"]
    point_ids = await _point_ids_for_user(user)
    accounts = await database.list_avito_accounts(active_only=True)

    result = []
    errors = []
    for account in accounts:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            continue
        try:
            orders = await client.get_orders(statuses=constants.ORDER_ACTIVE_STATUSES)
        except avito_client.AvitoAPIError as exc:
            logger.exception("api_orders: failed for account %s", account.id)
            errors.append(f"{account.name}: {exc}")
            continue
        for order in orders:
            point_id = await database.resolve_order_point_id(order)
            if point_id not in point_ids:
                continue
            items = order.get("items") or []
            status = order.get("status", "")
            result.append({
                "id": order.get("id"),
                "account_id": account.id,
                "account_name": account.name,
                "status": status,
                "status_label": constants.ORDER_STATUS_LABELS.get(status, status),
                "title": (items[0].get("title") if items else None) or "(без названия)",
            })

    return web.json_response({"orders": result, "errors": errors})


async def api_order_detail(request: web.Request) -> web.Response:
    account_id = int(request.match_info["account_id"])
    order_id = request.match_info["order_id"]
    client = avito_client.get_pool().get(account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)
    try:
        orders = await client.get_orders()
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)
    order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
    if order is None:
        return web.json_response({"error": "not_found"}, status=404)

    account = await database.get_avito_account(account_id)
    point_id = await database.resolve_order_point_id(order)
    point = await database.get_point(point_id) if point_id else None

    delivery_info = order.get("delivery") or {}
    track_number = delivery_info.get("dispatchNumber") or delivery_info.get("trackingNumber")
    items = order.get("items") or []
    prices = order.get("prices") or {}

    chat_short_id = None
    order_chat_id = (items[0] if items else {}).get("chatId")
    if order_chat_id:
        chat_short_id = await bot_cache.get_short_id(order_chat_id)

    status = order.get("status", "")
    return web.json_response({
        "id": order.get("id"),
        "account_id": account_id,
        "account_name": account.name if account else None,
        "point_name": point.name if point else None,
        "point_address": point.address if point else None,
        "track_number": track_number,
        "items": [i.get("title") for i in items],
        "status": status,
        "status_label": constants.ORDER_STATUS_LABELS.get(status, status),
        "total": prices.get("total"),
        "commission": prices.get("commission"),
        "delivery_service": delivery_info.get("serviceName") or delivery_info.get("serviceType"),
        "delivery_type": delivery_info.get("serviceType"),
        "available_actions": [a.get("name") for a in (order.get("availableActions") or [])],
        "chat_short_id": chat_short_id,
        "has_barcode": bool(track_number),
    })


async def api_order_barcode(request: web.Request) -> web.Response:
    # The order-detail response (api_order_detail, just above) already
    # resolved and returned track_number — the frontend fetches this image
    # right after that response, so it passes the value straight through
    # as ?track=... instead of us re-deriving it. Re-deriving it here used
    # to mean a second full paginated client.get_orders() call (every page
    # of every active order on the account, just to find one by id) on top
    # of the one api_order_detail already made moments earlier — with ~97
    # active orders that was the entire ~20s of "the barcode takes forever
    # to show up", not the (near-instant) PNG rendering itself.
    track_number = request.query.get("track")
    if not track_number:
        account_id = int(request.match_info["account_id"])
        order_id = request.match_info["order_id"]
        client = avito_client.get_pool().get(account_id)
        if client is None:
            return web.Response(status=404)
        try:
            orders = await client.get_orders()
        except avito_client.AvitoAPIError:
            return web.Response(status=502)
        order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
        if order is None:
            return web.Response(status=404)
        delivery_info = order.get("delivery") or {}
        track_number = delivery_info.get("dispatchNumber") or delivery_info.get("trackingNumber")
    if not track_number:
        return web.Response(status=404)
    png_bytes = utils.generate_barcode_png(str(track_number))
    return web.Response(body=png_bytes, content_type="image/png")


async def api_order_action(request: web.Request) -> web.Response:
    account_id = int(request.match_info["account_id"])
    order_id = request.match_info["order_id"]
    client = avito_client.get_pool().get(account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)

    body = await request.json()
    action = body.get("action")
    try:
        if action in ("confirm", "reject"):
            await client.apply_order_transition(order_id, action)
        elif action == "setMarkings":
            orders = await client.get_orders()
            order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
            item_id = (order.get("items") or [{}])[0].get("avitoId") if order else None
            if item_id is None:
                return web.json_response({"error": "no_item"}, status=400)
            markings = [c.strip() for c in (body.get("markings") or "").split(",") if c.strip()]
            await client.set_order_markings(item_id, order_id, markings)
        elif action == "setCNCDetails":
            orders = await client.get_orders()
            order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
            marketplace_id = order.get("marketplaceId") if order else None
            if marketplace_id is None:
                return web.json_response({"error": "no_marketplace_id"}, status=400)
            comment = str(body.get("comment") or "").strip()
            await client.set_cnc_order_details(
                order_id, marketplace_id, int(body["period"]),
                address=body.get("address"), details=None if comment in ("", "-") else comment,
            )
        elif action == "checkConfirmationCode":
            orders = await client.get_orders()
            order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
            parcel_id = (order.get("delivery") or {}).get("dispatchNumber") if order else None
            if parcel_id is None:
                return web.json_response({"error": "no_parcel_id"}, status=400)
            await client.check_confirmation_code(parcel_id, str(body.get("code", "")).strip())
        else:
            return web.json_response({"error": "unknown_action"}, status=400)
    except avito_client.AvitoAPIError as exc:
        logger.exception("api_order_action: %s failed for order %s", action, order_id)
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)

    return web.json_response({"ok": True})


# ============================================================================
# Admin routes — every handler below mirrors a admin_router callback in
# handlers.py 1:1 (same database.py calls, same permission rules), just
# collapsing handlers.py's multi-step Telegram FSM dialogs into a single
# request each, since a web form collects every field before submitting.
# ============================================================================


async def _notify_user_safe(bot, telegram_id: int, text: str) -> None:
    if bot is None:
        return
    try:
        await bot.send_message(telegram_id, text)
        await database.mark_user_reachable(telegram_id)
    except TelegramForbiddenError:
        await database.mark_user_unreachable(telegram_id)
    except Exception:
        logger.exception("_notify_user_safe: failed to notify %s", telegram_id)


async def api_admin_users(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    users = await database.list_all_users()
    return web.json_response({"users": [_serialize_user(u) for u in users]})


async def api_admin_onshift(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    users = await database.list_on_shift_users()
    result = []
    for u in users:
        if u.role == constants.MANAGER and u.responsible_point_id:
            point = await database.get_point(u.responsible_point_id)
            point_label = point.name if point else "—"
        else:
            points = await database.get_user_points(u.telegram_id)
            point_label = ", ".join(p.name for p in points) or "—"
        result.append({**_serialize_user(u), "point_label": point_label})
    return web.json_response({"users": result})


async def api_admin_user_update(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    body = await request.json()
    if "full_name" in body:
        await database.update_user_full_name(target_id, (body["full_name"] or "").strip())
    if "trade_point_name" in body:
        await database.update_user_trade_point(target_id, (body["trade_point_name"] or "").strip())
    return web.json_response({"ok": True})


async def api_admin_user_role(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    body = await request.json()
    role = body.get("role")
    if role not in constants.ROLE_ORDER:
        return web.json_response({"error": "invalid_role"}, status=400)
    if actor.role == constants.ADMIN and role in (constants.ADMIN, constants.DIRECTOR):
        return web.json_response({"error": "рop_cannot_assign_admin_roles"}, status=403)

    target = await database.get_user(target_id)
    if target and target.role == constants.DIRECTOR and role != constants.DIRECTOR:
        if await database.count_approved_directors() <= 1:
            return web.json_response({"error": "cannot_leave_without_director"}, status=409)

    if role == constants.MANAGER:
        point_id = body.get("point_id")
        if not point_id:
            return web.json_response({"error": "point_id_required_for_manager"}, status=400)
        await database.set_user_role(target_id, role, actor.telegram_id)
        await database.set_responsible_point(target_id, int(point_id))
    else:
        await database.set_user_role(target_id, role, actor.telegram_id)

    await _notify_user_safe(request.app.get("bot"), target_id, f"🎭 Ваша роль изменена: {constants.ROLE_LABELS[role]}")
    return web.json_response({"ok": True})


async def api_admin_user_points_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    points = await database.get_user_points(target_id)
    return web.json_response({"point_ids": [p.id for p in points]})


async def api_admin_user_points(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    body = await request.json()
    wanted = {int(pid) for pid in (body.get("point_ids") or [])}
    current = {p.id for p in await database.get_user_points(target_id)}
    for point_id in wanted - current:
        await database.subscribe_user_to_point(target_id, point_id)
    for point_id in current - wanted:
        await database.unsubscribe_user_from_point(target_id, point_id)
    return web.json_response({"ok": True})


async def api_admin_user_block(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    target = await database.get_user(target_id)
    if target and target.role == constants.DIRECTOR and await database.count_approved_directors() <= 1:
        return web.json_response({"error": "cannot_leave_without_director"}, status=409)
    if actor.role == constants.ADMIN and target and _has_role_at_least(target, constants.ADMIN):
        return web.json_response({"error": "рop_cannot_block_admin"}, status=403)

    await database.set_user_status(target_id, constants.STATUS_BLOCKED, actor.telegram_id)
    await database.set_shift(target_id, False)
    await _notify_user_safe(request.app.get("bot"), target_id, "Ваш доступ отозван.")
    return web.json_response({"ok": True})


async def api_admin_user_unblock(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    target = await database.get_user(target_id)
    if target is None:
        return web.json_response({"error": "not_found"}, status=404)
    if target.status != constants.STATUS_BLOCKED:
        return web.json_response({"error": "not_blocked", "status": target.status}, status=409)
    await database.set_user_status(target_id, constants.STATUS_APPROVED, request["user"].telegram_id)
    await _notify_user_safe(request.app.get("bot"), target_id, "✅ Ваш доступ восстановлен.")
    return web.json_response({"ok": True})


async def api_admin_user_delete(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    target = await database.get_user(target_id)
    if target is None:
        return web.json_response({"error": "not_found"}, status=404)
    reason = await database.can_delete_user(target_id)
    if reason is not None:
        return web.json_response({"error": "cannot_delete", "detail": reason}, status=409)
    await database.delete_user_account(target_id)
    await _notify_user_safe(
        request.app.get("bot"), target_id,
        "Ваша учётная запись сброшена администратором. Отправьте /start, чтобы подать новую заявку.",
    )
    return web.json_response({"ok": True})


async def api_admin_requests(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    pending = await database.list_pending_users()
    result = []
    for u in pending:
        payment = await database.get_payment_for_user(u.telegram_id)
        result.append({**_serialize_user(u), "has_unrefunded_payment": payment is not None})
    return web.json_response({"requests": result})


async def api_admin_request_approve(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    body = await request.json() if request.body_exists else {}
    point_ids = {int(pid) for pid in (body.get("point_ids") or [])}

    await database.set_user_status(target_id, constants.STATUS_APPROVED, actor.telegram_id)
    for point_id in point_ids:
        await database.subscribe_user_to_point(target_id, point_id)

    bot = request.app.get("bot")
    welcome_text = await database.get_welcome_message()
    await _notify_user_safe(bot, target_id, welcome_text)
    return web.json_response({"ok": True})


async def api_admin_request_reject(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    target_id = int(request.match_info["user_id"])
    body = await request.json() if request.body_exists else {}
    refund = bool(body.get("refund"))

    if refund:
        payment = await database.get_payment_for_user(target_id)
        bot = request.app.get("bot")
        if payment and bot is not None:
            try:
                await bot.refund_star_payment(user_id=target_id, telegram_payment_charge_id=payment.telegram_charge_id)
                await database.mark_payment_refunded(payment.id)
            except Exception:
                logger.exception("api_admin_request_reject: refund failed for %s", target_id)

    await database.set_user_status(target_id, constants.STATUS_BLOCKED, actor.telegram_id)
    text = "Ваша заявка отклонена, оплата возвращена." if refund else "Ваша заявка отклонена."
    await _notify_user_safe(request.app.get("bot"), target_id, text)
    return web.json_response({"ok": True})


# --- admin: points -----------------------------------------------------------


async def api_admin_points(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    points = await database.list_points(active_only=False)
    return web.json_response({"points": [_serialize_point(p) for p in points]})


async def api_admin_point_update(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    point_id = int(request.match_info["point_id"])
    body = await request.json()
    if "name" in body and (body["name"] or "").strip():
        await database.rename_point(point_id, body["name"].strip())
    if "code" in body:
        await database.set_point_code(point_id, (body["code"] or "").strip().upper() or None)
    if "address" in body or "working_hours" in body:
        await database.update_point_details(
            point_id, address=body.get("address"), working_hours=body.get("working_hours")
        )
    return web.json_response({"ok": True})


async def api_admin_point_toggle(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    point_id = int(request.match_info["point_id"])
    point = await database.get_point(point_id)
    if point is None:
        return web.json_response({"error": "not_found"}, status=404)
    if point.is_active:
        await database.soft_delete_point(point_id)
    else:
        await database.reactivate_point(point_id)
    return web.json_response({"ok": True})


async def api_admin_points_bulk_import(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    text = body.get("text") or ""
    all_points = await database.list_points(active_only=False)

    updated: list[str] = []
    not_found: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            not_found.append(f"{line} (не смог разобрать строку)")
            continue
        code = parts[0].upper()
        hours = parts[-1]
        address = " ".join(parts[1:-1])

        point = await database.get_point_by_code(code)
        if point is None:
            code_lower = code.lower()
            candidates = [
                p for p in all_points
                if p.name.strip().lower() == code_lower or code_lower in p.name.lower().split()
            ]
            if len(candidates) == 1:
                point = candidates[0]
            elif len(candidates) > 1:
                not_found.append(f"{code} — несколько точек подходят по названию, разберите вручную")
                continue

        if point is None:
            not_found.append(f"{code} — точка не найдена (ни по коду, ни по названию)")
            continue

        await database.update_point_details(point.id, address=address, working_hours=hours)
        await database.set_point_code(point.id, code)
        updated.append(f"{code} → «{point.name}»")

    return web.json_response({"updated": updated, "not_found": not_found})


async def api_admin_points_sync(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    accounts = await database.list_avito_accounts(active_only=True)
    seen_coords = 0
    report = []
    for account in accounts:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            report.append(f"⚠️ {account.name}: клиент недоступен (перезапустите бота после добавления аккаунта)")
            continue
        offset = 0
        account_chats = 0
        account_coords = 0
        error_text = None
        while True:
            try:
                chats = await client.get_chats(limit=100, offset=offset)
            except avito_client.AvitoAPIError as exc:
                error_text = str(exc)
                break
            if not chats:
                break
            account_chats += len(chats)
            for chat in chats:
                if chat.item_lat is None or chat.item_lon is None:
                    continue
                account_coords += 1
                seen_coords += 1
                name = chat.location_title or chat.item_title or f"Точка {chat.item_lat:.4f},{chat.item_lon:.4f}"
                await database.upsert_point_from_avito(name=name, address=None, lat=chat.item_lat, lon=chat.item_lon)
            if len(chats) < 100:
                break
            offset += 100
        line = f"{account.name}: чатов {account_chats}, с координатами {account_coords}"
        if error_text:
            line += f" — ⚠️ ОШИБКА: {error_text}"
        report.append(line)

    points = await database.list_points()
    return web.json_response({"report": report, "seen_coords": seen_coords, "total_points": len(points)})


async def api_admin_points_conflicts(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    points = await database.list_points(active_only=False)
    all_coords: list[tuple[int, str, float, float]] = []
    for p in points:
        for c in await database.list_point_coordinates(p.id):
            all_coords.append((p.id, p.name, c.lat, c.lon))

    conflict_map: dict[tuple[int, int], tuple[str, str, float]] = {}
    for i in range(len(all_coords)):
        pid1, name1, lat1, lon1 = all_coords[i]
        for j in range(i + 1, len(all_coords)):
            pid2, name2, lat2, lon2 = all_coords[j]
            if pid1 == pid2:
                continue
            dist = utils.haversine_distance_m(lat1, lon1, lat2, lon2)
            if dist > constants.POINT_CONFLICT_WARNING_M:
                continue
            pair_key = (min(pid1, pid2), max(pid1, pid2))
            existing = conflict_map.get(pair_key)
            if existing is None or dist < existing[2]:
                conflict_map[pair_key] = (name1, name2, dist)

    conflicts = sorted(conflict_map.values(), key=lambda c: c[2])
    return web.json_response({
        "conflicts": [{"point_a": a, "point_b": b, "distance_m": round(d)} for a, b, d in conflicts],
        "warning_threshold_m": constants.POINT_CONFLICT_WARNING_M,
    })


async def api_admin_points_unassigned(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    chats = await database.list_chats_without_point()
    result = []
    for c in chats:
        short_id = await bot_cache.get_short_id(c.chat_id)
        result.append({"short_id": short_id, "client_name": c.client_name, "item_id": c.item_id})
    return web.json_response({"chats": result})


async def api_admin_points_reassign(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    short_id = body.get("chat_short_id")
    point_id = body.get("point_id")
    if not short_id or not point_id:
        return web.json_response({"error": "missing_fields"}, status=400)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None or not chat.item_id:
        return web.json_response({"error": "not_found"}, status=404)
    await database.reassign_item_point(chat.item_id, int(point_id), actor.telegram_id)
    chat.point_id = int(point_id)
    return web.json_response({"ok": True})


# --- admin: Avito accounts -----------------------------------------------


async def api_admin_avito_accounts(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    accounts = await database.list_avito_accounts(active_only=False)
    return web.json_response({"accounts": [_serialize_avito_account(a) for a in accounts]})


async def api_admin_avito_account_create(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    name = (body.get("name") or "").strip()
    client_id = (body.get("client_id") or "").strip()
    client_secret = (body.get("client_secret") or "").strip()
    if not (name and client_id and client_secret):
        return web.json_response({"error": "missing_fields"}, status=400)

    try:
        info = await avito_client.fetch_account_info(client_id, client_secret, avito_client.get_session())
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)
    avito_user_id = info.get("id")
    if not avito_user_id:
        return web.json_response({"error": "no_account_id_in_response"}, status=502)

    account = await database.create_avito_account(
        name=name, avito_user_id=avito_user_id, client_id=client_id, client_secret=client_secret
    )
    await avito_client.reload_accounts()
    return web.json_response(_serialize_avito_account(account))


async def api_admin_avito_account_toggle(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    account_id = int(request.match_info["account_id"])
    account = await database.get_avito_account(account_id)
    if account is None:
        return web.json_response({"error": "not_found"}, status=404)
    await database.set_avito_account_active(account_id, not account.is_active)
    await avito_client.reload_accounts()
    return web.json_response({"ok": True})


# --- admin: AI / proxy / payment / welcome / backup config ------------------


async def api_admin_ai_config_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    cfg = await database.get_ai_config()
    return web.json_response({
        "base_url": cfg.base_url, "model": cfg.model, "has_api_key": bool(cfg.api_key),
        "extra_header_name": cfg.extra_header_name, "extra_header_value": cfg.extra_header_value,
        "is_enabled": bool(cfg.is_enabled),
    })


async def api_admin_ai_config_patch(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    fields = {}
    for key in ("base_url", "model", "api_key"):
        if body.get(key):
            fields[key] = body[key].strip()
    if "is_enabled" in body:
        fields["is_enabled"] = 1 if body["is_enabled"] else 0
    if fields:
        await database.update_ai_config(actor_id=actor.telegram_id, **fields)
    return web.json_response({"ok": True})


async def api_admin_proxy_config_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    cfg = await database.get_proxy_config()
    return web.json_response({"is_enabled": bool(cfg.is_enabled), "proxy_url": cfg.proxy_url})


async def api_admin_proxy_config_patch(request: web.Request) -> web.Response:
    """Saving proxy settings always restarts the whole process (see the
    project plan) — systemd's Restart=always brings it back in ~3s already
    configured with the new proxy. We respond first, then force the exit
    on a short delay so the HTTP response actually reaches the browser."""
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    fields = {}
    if body.get("proxy_url"):
        fields["proxy_url"] = body["proxy_url"].strip()
        fields["is_enabled"] = 1
    if "is_enabled" in body:
        fields["is_enabled"] = 1 if body["is_enabled"] else 0
    if fields:
        await database.update_proxy_config(actor_id=actor.telegram_id, **fields)

    async def _delayed_restart() -> None:
        await asyncio.sleep(1.0)
        os._exit(0)

    asyncio.create_task(_delayed_restart())
    return web.json_response({"ok": True, "restarting": True})


async def api_admin_payment_config_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    cfg = await database.get_payment_config()
    return web.json_response({"is_enabled": bool(cfg.is_enabled), "amount_stars": cfg.amount_stars})


async def api_admin_payment_config_patch(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    fields = {}
    if "is_enabled" in body:
        fields["is_enabled"] = 1 if body["is_enabled"] else 0
    if "amount_stars" in body:
        try:
            fields["amount_stars"] = int(body["amount_stars"])
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_amount"}, status=400)
    if fields:
        await database.update_payment_config(actor_id=actor.telegram_id, **fields)
    return web.json_response({"ok": True})


async def api_admin_welcome_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    return web.json_response({"text": await database.get_welcome_message()})


async def api_admin_welcome_patch(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty_text"}, status=400)
    await database.update_welcome_message(text, actor.telegram_id)
    return web.json_response({"ok": True})


async def api_admin_backup_config_get(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    cfg = await database.get_backup_config()
    return web.json_response({
        "is_enabled": bool(cfg.is_enabled), "interval_hours": cfg.interval_hours,
        "last_backup_at": cfg.last_backup_at,
    })


async def api_admin_backup_config_patch(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    body = await request.json()
    fields = {}
    if "is_enabled" in body:
        fields["is_enabled"] = 1 if body["is_enabled"] else 0
    if "interval_hours" in body:
        try:
            fields["interval_hours"] = int(body["interval_hours"])
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_interval"}, status=400)
    if fields:
        await database.update_backup_config(actor_id=actor.telegram_id, **fields)
    return web.json_response({"ok": True})


async def api_admin_backup_run(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp
    bot = request.app.get("bot")
    db_path = request.app.get("db_path")
    if bot is None or not db_path:
        return web.json_response({"error": "unavailable"}, status=503)
    tmp_path = f"{db_path}.backup-{int(datetime.utcnow().timestamp())}.db"
    await database.vacuum_into(tmp_path)
    try:
        await bot.send_document(actor.telegram_id, FSInputFile(tmp_path), caption="💾 Резервная копия БД")
        await database.mark_backup_done(datetime.utcnow())
    except TelegramForbiddenError:
        return web.json_response({"error": "bot_blocked"}, status=409)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return web.json_response({"ok": True})


# --- admin: reviews ----------------------------------------------------------


async def api_admin_reviews(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    account_id_raw = request.query.get("account_id")
    if account_id_raw:
        account_id = int(account_id_raw)
    else:
        accounts = await database.list_avito_accounts(active_only=True)
        if not accounts:
            return web.json_response({"error": "no_accounts"}, status=404)
        account_id = accounts[0].id

    client = avito_client.get_pool().get(account_id)
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)
    try:
        info = await client.get_rating_info()
        data = await client.get_reviews(limit=10)
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)

    rating = info.get("rating") or {}
    reviews = []
    for review in data.get("reviews") or []:
        answer = review.get("answer")
        reviews.append({
            "id": review.get("id"),
            "sender_name": (review.get("sender") or {}).get("name", "Клиент"),
            "item_title": (review.get("item") or {}).get("title"),
            "text": review.get("text") or "",
            "score": review.get("score") or 0,
            "answer": answer.get("text") if answer else None,
            "can_answer": bool(review.get("canAnswer")) and answer is None,
        })
    return web.json_response({
        "account_id": account_id, "score": rating.get("score"), "reviews_count": rating.get("reviewsCount", 0),
        "reviews": reviews,
    })


async def api_admin_review_answer(request: web.Request) -> web.Response:
    if (resp := _require_admin(request)) is not None:
        return resp
    review_id = int(request.match_info["review_id"])
    body = await request.json()
    account_id = body.get("account_id")
    text = (body.get("text") or "").strip()
    if not account_id or not text:
        return web.json_response({"error": "missing_fields"}, status=400)
    client = avito_client.get_pool().get(int(account_id))
    if client is None:
        return web.json_response({"error": "avito_unavailable"}, status=503)
    try:
        await client.answer_review(review_id, text)
    except avito_client.AvitoAPIError as exc:
        return web.json_response({"error": "avito_rejected", "detail": str(exc)}, status=502)
    return web.json_response({"ok": True})


# --- admin: broadcast ----------------------------------------------------


async def api_admin_broadcast(request: web.Request) -> web.Response:
    actor: models.User = request["user"]
    if (resp := _require_admin(request)) is not None:
        return resp

    text = ""
    photo_bytes: bytes | None = None
    content_type = request.headers.get("Content-Type", "")
    if content_type.startswith("multipart/"):
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "text":
                text = (await part.text()).strip()
            elif part.name == "photo":
                photo_bytes = await part.read(decode=False)
    else:
        body = await request.json()
        text = (body.get("text") or "").strip()

    if not text:
        return web.json_response({"error": "empty_text"}, status=400)

    signature_name = actor.last_name or actor.full_name or "Администрация"
    role_label = constants.ROLE_LABELS.get(actor.role, "")
    full_text = f"{text}\n\n— {signature_name}, {role_label}"

    bot = request.app.get("bot")
    sent = failed = 0
    photo_file_id: str | None = None
    if bot is not None:
        for user in await database.list_approved_users():
            try:
                if photo_bytes is not None:
                    msg = await bot.send_photo(
                        user.telegram_id, BufferedInputFile(photo_bytes, filename="broadcast.jpg"), caption=full_text
                    )
                    if photo_file_id is None and msg.photo:
                        photo_file_id = msg.photo[-1].file_id
                else:
                    await bot.send_message(user.telegram_id, full_text)
                sent += 1
            except TelegramForbiddenError:
                await database.mark_user_unreachable(user.telegram_id)
                failed += 1
            except Exception:
                logger.exception("api_admin_broadcast: failed to send to %s", user.telegram_id)
                failed += 1

    await database.log_broadcast(actor.telegram_id, text, photo_file_id, sent, failed)
    return web.json_response({"ok": True, "sent": sent, "failed": failed})


def create_app(bot_token: str, bot=None, db_path: str | None = None) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=20 * 1024 * 1024)
    app["bot_token"] = bot_token
    app["bot"] = bot
    app["db_path"] = db_path

    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/shift", api_shift)
    app.router.add_get("/api/profile", api_profile)

    app.router.add_get("/api/points", api_points_all)
    app.router.add_get("/api/points/mine", api_points_mine)
    app.router.add_post("/api/points/subscribe", api_points_subscribe)

    app.router.add_get("/api/chats", api_chats)
    app.router.add_get("/api/chats/{short_id}", api_chat_detail)
    app.router.add_post("/api/chats/{short_id}/read", api_chat_read)
    app.router.add_post("/api/chats/{short_id}/reply", api_chat_reply)
    app.router.add_post("/api/chats/{short_id}/reply-photo", api_chat_reply_photo)
    app.router.add_post("/api/chats/{short_id}/ai-draft", api_chat_ai_draft)
    app.router.add_get("/api/chats/{short_id}/templates", api_chat_templates)
    app.router.add_post("/api/chats/{short_id}/templates/{template_id}/apply", api_chat_template_apply)
    app.router.add_delete("/api/messages/{msg_ref}", api_message_delete)

    app.router.add_get("/api/templates/mine", api_templates_mine)
    app.router.add_post("/api/templates/mine", api_templates_mine_create)
    app.router.add_delete("/api/templates/mine/{template_id}", api_templates_mine_delete)

    app.router.add_get("/api/orders", api_orders)
    app.router.add_get("/api/orders/{account_id}/{order_id}", api_order_detail)
    app.router.add_get("/api/orders/{account_id}/{order_id}/barcode.png", api_order_barcode)
    app.router.add_post("/api/orders/{account_id}/{order_id}/action", api_order_action)

    app.router.add_get("/api/admin/users", api_admin_users)
    app.router.add_patch("/api/admin/users/{user_id}", api_admin_user_update)
    app.router.add_post("/api/admin/users/{user_id}/role", api_admin_user_role)
    app.router.add_get("/api/admin/users/{user_id}/points", api_admin_user_points_get)
    app.router.add_post("/api/admin/users/{user_id}/points", api_admin_user_points)
    app.router.add_post("/api/admin/users/{user_id}/block", api_admin_user_block)
    app.router.add_post("/api/admin/users/{user_id}/unblock", api_admin_user_unblock)
    app.router.add_delete("/api/admin/users/{user_id}", api_admin_user_delete)
    app.router.add_get("/api/admin/onshift", api_admin_onshift)
    app.router.add_get("/api/admin/requests", api_admin_requests)
    app.router.add_post("/api/admin/requests/{user_id}/approve", api_admin_request_approve)
    app.router.add_post("/api/admin/requests/{user_id}/reject", api_admin_request_reject)

    app.router.add_get("/api/admin/points", api_admin_points)
    app.router.add_patch("/api/admin/points/{point_id}", api_admin_point_update)
    app.router.add_post("/api/admin/points/{point_id}/toggle", api_admin_point_toggle)
    app.router.add_post("/api/admin/points/bulk-import", api_admin_points_bulk_import)
    app.router.add_post("/api/admin/points/sync", api_admin_points_sync)
    app.router.add_get("/api/admin/points/conflicts", api_admin_points_conflicts)
    app.router.add_get("/api/admin/points/unassigned", api_admin_points_unassigned)
    app.router.add_post("/api/admin/points/reassign", api_admin_points_reassign)

    app.router.add_get("/api/admin/avito-accounts", api_admin_avito_accounts)
    app.router.add_post("/api/admin/avito-accounts", api_admin_avito_account_create)
    app.router.add_post("/api/admin/avito-accounts/{account_id}/toggle", api_admin_avito_account_toggle)

    app.router.add_get("/api/admin/ai-config", api_admin_ai_config_get)
    app.router.add_patch("/api/admin/ai-config", api_admin_ai_config_patch)
    app.router.add_get("/api/admin/proxy-config", api_admin_proxy_config_get)
    app.router.add_patch("/api/admin/proxy-config", api_admin_proxy_config_patch)
    app.router.add_get("/api/admin/payment-config", api_admin_payment_config_get)
    app.router.add_patch("/api/admin/payment-config", api_admin_payment_config_patch)
    app.router.add_get("/api/admin/welcome", api_admin_welcome_get)
    app.router.add_patch("/api/admin/welcome", api_admin_welcome_patch)
    app.router.add_get("/api/admin/backup-config", api_admin_backup_config_get)
    app.router.add_patch("/api/admin/backup-config", api_admin_backup_config_patch)
    app.router.add_post("/api/admin/backup/run", api_admin_backup_run)

    app.router.add_get("/api/admin/reviews", api_admin_reviews)
    app.router.add_post("/api/admin/reviews/{review_id}/answer", api_admin_review_answer)

    app.router.add_post("/api/admin/broadcast", api_admin_broadcast)

    async def index(_request: web.Request) -> web.Response:
        # Telegram's in-app WebView caches static assets very aggressively
        # by URL, independent of Cache-Control/ETag — a plain "git pull" of
        # style.css/app.js was silently served stale on reopen. Bust it by
        # appending a query string derived from the current file contents
        # (recomputed on every request, not cached in-process, so it's
        # correct even without an avito_bot restart after a deploy) and
        # forbid caching index.html itself so the browser always re-reads
        # this — and therefore the current — version string.
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        digest = hashlib.md5()
        digest.update((STATIC_DIR / "style.css").read_bytes())
        digest.update((STATIC_DIR / "app.js").read_bytes())
        version = digest.hexdigest()[:10]
        html = html.replace('href="style.css"', f'href="style.css?v={version}"')
        html = html.replace('src="app.js"', f'src="app.js?v={version}"')
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    app.router.add_get("/", index)
    app.router.add_static("/", STATIC_DIR, show_index=False)
    return app
