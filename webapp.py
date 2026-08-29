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

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import avito_client
import bot_cache
import constants
import database
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


async def api_chat_detail(request: web.Request) -> web.Response:
    short_id = request.match_info["short_id"]
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return web.json_response({"error": "not_found"}, status=404)
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


def create_app(bot_token: str) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["bot_token"] = bot_token

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

    app.router.add_get("/api/orders", api_orders)
    app.router.add_get("/api/orders/{account_id}/{order_id}", api_order_detail)
    app.router.add_get("/api/orders/{account_id}/{order_id}/barcode.png", api_order_barcode)
    app.router.add_post("/api/orders/{account_id}/{order_id}/action", api_order_action)

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    app.router.add_get("/", index)
    app.router.add_static("/", STATIC_DIR, show_index=False)
    return app
