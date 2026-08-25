"""aiosqlite access layer: migration runner + all queries.

Single shared connection (WAL mode), writes serialized through a module
lock, reads unlocked (safe under WAL). No ORM — the table shapes are
simple enough that raw SQL + dataclasses (models.py) cover it without
extra dependency weight ("просто файл БД").
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

import constants
import models
import utils

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_conn: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()


def _require_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("database.init_db() has not been called yet")
    return _conn


async def _execute(sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
    conn = _require_conn()
    async with _write_lock:
        cur = await conn.execute(sql, tuple(params))
        await conn.commit()
        return cur


async def _fetchone(sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
    conn = _require_conn()
    cur = await conn.execute(sql, tuple(params))
    row = await cur.fetchone()
    await cur.close()
    return row


async def _fetchall(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    conn = _require_conn()
    cur = await conn.execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return list(rows)


# --- init / migrations -------------------------------------------------------


async def init_db(path: str) -> None:
    global _conn
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL")
    await _conn.execute("PRAGMA foreign_keys=ON")
    await _apply_migrations()


async def vacuum_into(path: str) -> None:
    conn = _require_conn()
    escaped = path.replace("'", "''")
    await conn.execute(f"VACUUM INTO '{escaped}'")


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def _apply_migrations() -> None:
    conn = _require_conn()
    cur = await conn.execute("PRAGMA user_version")
    row = await cur.fetchone()
    await cur.close()
    current_version = row[0] if row else 0

    migrations: list[tuple[int, Path]] = []
    for file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        try:
            version = int(file.name.split("_", 1)[0])
        except ValueError:
            continue
        migrations.append((version, file))
    migrations.sort(key=lambda pair: pair[0])

    for version, file in migrations:
        if version <= current_version:
            continue
        sql = file.read_text(encoding="utf-8")
        await conn.executescript(sql)
        await conn.execute(f"PRAGMA user_version = {version}")
        await conn.commit()


# --- bootstrap -----------------------------------------------------------


async def bootstrap_director(telegram_id: int) -> None:
    existing = await _fetchone("SELECT telegram_id FROM users WHERE telegram_id = ?", (telegram_id,))
    if existing is not None:
        await _execute(
            "UPDATE users SET role = ?, status = ? WHERE telegram_id = ?",
            (constants.DIRECTOR, constants.STATUS_APPROVED, telegram_id),
        )
        return
    await _execute(
        """
        INSERT INTO users (telegram_id, role, status, approved_at)
        VALUES (?, ?, ?, datetime('now'))
        """,
        (telegram_id, constants.DIRECTOR, constants.STATUS_APPROVED),
    )


# --- users -----------------------------------------------------------------


async def get_user(telegram_id: int) -> models.User | None:
    row = await _fetchone("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    return models.User.from_row(row) if row else None


async def create_or_update_user(telegram_id: int, username: str | None, full_name: str | None,
                                 last_name: str | None = None) -> models.User:
    existing = await get_user(telegram_id)
    if existing is None:
        await _execute(
            """
            INSERT INTO users (telegram_id, username, full_name, last_name)
            VALUES (?, ?, ?, ?)
            """,
            (telegram_id, username, full_name, last_name),
        )
    else:
        await _execute(
            """
            UPDATE users SET username = ?, full_name = ?,
                   last_name = COALESCE(?, last_name), last_seen_at = datetime('now')
            WHERE telegram_id = ?
            """,
            (username, full_name, last_name, telegram_id),
        )
    user = await get_user(telegram_id)
    assert user is not None
    return user


async def set_user_role(telegram_id: int, role: str, actor_id: int) -> None:
    await _execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id))
    await log_access_request(telegram_id, "role_changed", actor_id, note=role)


async def set_user_status(telegram_id: int, status: str, actor_id: int) -> None:
    if status == constants.STATUS_APPROVED:
        await _execute(
            """
            UPDATE users SET status = ?, approved_at = datetime('now'), approved_by = ?
            WHERE telegram_id = ?
            """,
            (status, actor_id, telegram_id),
        )
    else:
        await _execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, telegram_id))
    action = "approved" if status == constants.STATUS_APPROVED else "blocked" if status == constants.STATUS_BLOCKED else "unblocked"
    await log_access_request(telegram_id, action, actor_id)


async def count_approved_directors() -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM users WHERE role = ? AND status = ?",
        (constants.DIRECTOR, constants.STATUS_APPROVED),
    )
    return int(row["n"]) if row else 0


async def set_shift(telegram_id: int, on_shift: bool) -> None:
    await _execute("UPDATE users SET on_shift = ? WHERE telegram_id = ?", (1 if on_shift else 0, telegram_id))


async def list_pending_users() -> list[models.User]:
    rows = await _fetchall("SELECT * FROM users WHERE status = ? ORDER BY created_at", (constants.STATUS_PENDING,))
    return [models.User.from_row(r) for r in rows]


async def list_all_users() -> list[models.User]:
    rows = await _fetchall("SELECT * FROM users ORDER BY created_at DESC")
    return [models.User.from_row(r) for r in rows]


async def list_on_shift_users() -> list[models.User]:
    rows = await _fetchall(
        "SELECT * FROM users WHERE on_shift = 1 AND status = ? ORDER BY full_name",
        (constants.STATUS_APPROVED,),
    )
    return [models.User.from_row(r) for r in rows]


async def update_user_full_name(telegram_id: int, full_name: str) -> None:
    await _execute("UPDATE users SET full_name = ? WHERE telegram_id = ?", (full_name, telegram_id))


async def update_user_trade_point(telegram_id: int, trade_point_name: str) -> None:
    await _execute("UPDATE users SET trade_point_name = ? WHERE telegram_id = ?", (trade_point_name, telegram_id))


async def can_delete_user(telegram_id: int) -> str | None:
    """None if safe to hard-delete, otherwise a human-readable reason not to."""
    row = await _fetchone("SELECT COUNT(*) AS n FROM payments WHERE user_id = ?", (telegram_id,))
    if row and row["n"]:
        return "есть история платежей"
    row = await _fetchone("SELECT COUNT(*) AS n FROM broadcasts WHERE author_id = ?", (telegram_id,))
    if row and row["n"]:
        return "есть отправленные рассылки"
    return None


async def delete_user_account(telegram_id: int) -> None:
    """Hard-deletes a user so they can go through registration again.

    Caller must have already checked can_delete_user() returns None.
    Nullable FKs pointing at this user (approver, template author, last
    replier, someone else's access_requests actor) are cleared without
    touching the rows themselves; this user's own access_requests (their
    personal registration trail, NOT NULL FK, no cascade) are deleted
    outright. subscriptions.user_id is ON DELETE CASCADE and cleans up on
    its own.
    """
    for sql in (
        "UPDATE users SET approved_by = NULL WHERE approved_by = ?",
        "UPDATE templates SET created_by = NULL WHERE created_by = ?",
        "UPDATE ai_config SET updated_by = NULL WHERE updated_by = ?",
        "UPDATE proxy_config SET updated_by = NULL WHERE updated_by = ?",
        "UPDATE payment_config SET updated_by = NULL WHERE updated_by = ?",
        "UPDATE welcome_config SET updated_by = NULL WHERE updated_by = ?",
        "UPDATE backup_config SET updated_by = NULL WHERE updated_by = ?",
        "UPDATE access_requests SET actor_id = NULL WHERE actor_id = ?",
        "UPDATE chats SET last_replied_by = NULL WHERE last_replied_by = ?",
    ):
        await _execute(sql, (telegram_id,))
    await _execute("DELETE FROM access_requests WHERE user_id = ?", (telegram_id,))
    await _execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))


async def mark_user_unreachable(telegram_id: int) -> None:
    await _execute("UPDATE users SET blocked_bot = 1 WHERE telegram_id = ?", (telegram_id,))


async def mark_user_reachable(telegram_id: int) -> None:
    await _execute("UPDATE users SET blocked_bot = 0 WHERE telegram_id = ?", (telegram_id,))


async def touch_last_start(telegram_id: int) -> None:
    await _execute("UPDATE users SET last_start_at = datetime('now') WHERE telegram_id = ?", (telegram_id,))


async def seconds_since_last_start(telegram_id: int) -> float | None:
    row = await _fetchone("SELECT last_start_at FROM users WHERE telegram_id = ?", (telegram_id,))
    if row is None or row["last_start_at"] is None:
        return None
    delta = datetime.utcnow() - utils.parse_utc(row["last_start_at"])
    return delta.total_seconds()


async def increment_rating(telegram_id: int) -> None:
    await _execute("UPDATE users SET rating_points = rating_points + 1 WHERE telegram_id = ?", (telegram_id,))


async def get_user_rating(telegram_id: int) -> int:
    row = await _fetchone("SELECT rating_points FROM users WHERE telegram_id = ?", (telegram_id,))
    return int(row["rating_points"]) if row else 0


async def get_leaderboard(limit: int = 10) -> list[tuple[models.User, int]]:
    rows = await _fetchall(
        """
        SELECT * FROM users
        WHERE status = ?
        ORDER BY rating_points DESC, telegram_id
        LIMIT ?
        """,
        (constants.STATUS_APPROVED, limit),
    )
    return [(models.User.from_row(r), int(r["rating_points"])) for r in rows]


async def get_leaderboard_rank(telegram_id: int) -> int | None:
    row = await _fetchone(
        """
        SELECT COUNT(*) + 1 AS rank
        FROM users u1
        WHERE u1.status = ? AND u1.rating_points > (
            SELECT rating_points FROM users WHERE telegram_id = ?
        )
        """,
        (constants.STATUS_APPROVED, telegram_id),
    )
    return int(row["rank"]) if row else None


async def set_responsible_point(telegram_id: int, point_id: int) -> None:
    await _execute("UPDATE users SET responsible_point_id = ? WHERE telegram_id = ?", (point_id, telegram_id))


# --- points / coordinates ----------------------------------------------------


async def list_points(active_only: bool = True) -> list[models.Point]:
    if active_only:
        rows = await _fetchall("SELECT * FROM points WHERE is_active = 1 ORDER BY name")
    else:
        rows = await _fetchall("SELECT * FROM points ORDER BY name")
    return [models.Point.from_row(r) for r in rows]


async def get_point(point_id: int) -> models.Point | None:
    row = await _fetchone("SELECT * FROM points WHERE id = ?", (point_id,))
    return models.Point.from_row(row) if row else None


async def create_point(name: str, address: str | None = None, working_hours: str | None = None) -> models.Point:
    cur = await _execute(
        "INSERT INTO points (name, address, working_hours) VALUES (?, ?, ?)",
        (name, address, working_hours),
    )
    point = await get_point(cur.lastrowid)
    assert point is not None
    return point


async def rename_point(point_id: int, new_name: str) -> None:
    await _execute("UPDATE points SET name = ?, name_is_custom = 1 WHERE id = ?", (new_name, point_id))


async def update_point_details(point_id: int, address: str | None = None, working_hours: str | None = None) -> None:
    if address is not None:
        await _execute("UPDATE points SET address = ? WHERE id = ?", (address, point_id))
    if working_hours is not None:
        await _execute("UPDATE points SET working_hours = ? WHERE id = ?", (working_hours, point_id))


async def get_point_by_code(code: str) -> models.Point | None:
    row = await _fetchone("SELECT * FROM points WHERE code = ? COLLATE NOCASE", (code,))
    return models.Point.from_row(row) if row else None


async def set_point_code(point_id: int, code: str | None) -> None:
    await _execute("UPDATE points SET code = ? WHERE id = ?", (code, point_id))


async def soft_delete_point(point_id: int) -> None:
    await _execute("UPDATE points SET is_active = 0 WHERE id = ?", (point_id,))


async def reactivate_point(point_id: int) -> None:
    await _execute("UPDATE points SET is_active = 1 WHERE id = ?", (point_id,))


async def list_point_coordinates(point_id: int) -> list[models.PointCoordinate]:
    rows = await _fetchall("SELECT * FROM point_coordinates WHERE point_id = ? ORDER BY id", (point_id,))
    return [models.PointCoordinate.from_row(r) for r in rows]


async def add_point_coordinate(point_id: int, lat: float, lon: float, source: str = "manual") -> None:
    await _execute(
        "INSERT INTO point_coordinates (point_id, lat, lon, source) VALUES (?, ?, ?, ?)",
        (point_id, lat, lon, source),
    )


async def remove_point_coordinate(coord_id: int) -> None:
    await _execute("DELETE FROM point_coordinates WHERE id = ?", (coord_id,))


async def resolve_point_by_coords(lat: float, lon: float,
                                   max_distance_m: float = constants.COORD_MAX_DISTANCE_M) -> models.Point | None:
    rows = await _fetchall("SELECT * FROM point_coordinates")
    best_point_id: int | None = None
    best_distance = max_distance_m
    for row in rows:
        d = utils.haversine_distance_m(lat, lon, row["lat"], row["lon"])
        if d <= best_distance:
            best_distance = d
            best_point_id = row["point_id"]
    if best_point_id is None:
        return None
    return await get_point(best_point_id)


async def _add_coordinate_if_new(point_id: int, lat: float, lon: float, source: str,
                                  dedup_distance_m: float = 15.0) -> None:
    """Repeated syncs see the same ad coordinates every run — skip the
    insert if a coordinate this close is already stored for the point, so
    point_coordinates doesn't grow unbounded and stays readable in the
    admin point-detail screen."""
    for existing in await list_point_coordinates(point_id):
        if utils.haversine_distance_m(lat, lon, existing.lat, existing.lon) <= dedup_distance_m:
            return
    await add_point_coordinate(point_id, lat, lon, source=source)


async def upsert_point_from_avito(name: str, address: str | None, lat: float, lon: float) -> models.Point:
    existing = await resolve_point_by_coords(lat, lon)
    if existing is not None:
        await _add_coordinate_if_new(existing.id, lat, lon, source="avito_sync")
        if not existing.name_is_custom and address:
            await update_point_details(existing.id, address=address)
        return existing
    point = await create_point(name=name, address=address)
    await add_point_coordinate(point.id, lat, lon, source="avito_sync")
    return point


# --- item -> point routing (sticky, item_id is the primary routing key) ------


_FALLBACK_POINT_NAME = "📭 Без геоданных (профиль)"


async def get_or_create_fallback_point() -> models.Point:
    """Sticky synthetic point for chats with no ad-level geo data at all.

    name_is_custom=1 from creation so a later admin rename never gets
    reverted — the fallback point never receives coordinates, so the
    Avito-sync clustering path (`upsert_point_from_avito`) would never
    touch it anyway, but this keeps the invariant explicit.
    """
    row = await _fetchone("SELECT * FROM points WHERE is_fallback = 1 LIMIT 1")
    if row is not None:
        return models.Point.from_row(row)
    cur = await _execute(
        "INSERT INTO points (name, is_fallback, name_is_custom) VALUES (?, 1, 1)",
        (_FALLBACK_POINT_NAME,),
    )
    point = await get_point(cur.lastrowid)
    assert point is not None
    return point


async def resolve_point_for_item(item_id: str | None, lat: float | None, lon: float | None) -> models.Point | None:
    if not item_id:
        # Direct-to-profile message, not tied to any ad — no item_id to key
        # a sticky avito_items row on, always route to the fallback point.
        return await get_or_create_fallback_point()

    row = await _fetchone("SELECT * FROM avito_items WHERE item_id = ?", (item_id,))
    if row is not None:
        if row["point_id"] is None:
            return None
        return await get_point(row["point_id"])

    if lat is None or lon is None:
        # Ad exists but Avito gave us no coordinates for it — geo-less, not
        # an ambiguous coordinate mismatch, so it goes straight to the
        # fallback point rather than the manual "unassigned chats" queue.
        fallback = await get_or_create_fallback_point()
        await _execute(
            """
            INSERT INTO avito_items (item_id, point_id, lat, lon, resolved_by, resolved_at)
            VALUES (?, ?, ?, ?, 'fallback', datetime('now'))
            """,
            (item_id, fallback.id, lat, lon),
        )
        return fallback

    point = await resolve_point_by_coords(lat, lon)
    await _execute(
        """
        INSERT INTO avito_items (item_id, point_id, lat, lon, resolved_by, resolved_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        """,
        (item_id, point.id if point else None, lat, lon, "coords" if point else None),
    )
    return point


async def backfill_fallback_items() -> None:
    """One-time (idempotent) fixup for avito_items rows stuck unresolved

    before the fallback point existed (item had no lat/lon and no manual
    reassignment). Safe to call on every startup — only touches rows still
    NULL. chats.point_id for these self-heals on the next poll cycle via
    the normal upsert_chat_summary() call, no need to touch it here.
    """
    fallback = await get_or_create_fallback_point()
    await _execute(
        """
        UPDATE avito_items SET point_id = ?, resolved_by = 'fallback'
        WHERE point_id IS NULL AND lat IS NULL AND lon IS NULL
        """,
        (fallback.id,),
    )


async def reassign_item_point(item_id: str, point_id: int, actor_id: int) -> None:
    existing = await _fetchone("SELECT item_id FROM avito_items WHERE item_id = ?", (item_id,))
    if existing is None:
        await _execute(
            """
            INSERT INTO avito_items (item_id, point_id, resolved_by, resolved_at)
            VALUES (?, ?, 'manual', datetime('now'))
            """,
            (item_id, point_id),
        )
    else:
        await _execute(
            "UPDATE avito_items SET point_id = ?, resolved_by = 'manual', resolved_at = datetime('now') WHERE item_id = ?",
            (point_id, item_id),
        )
    await _execute("UPDATE chats SET point_id = ? WHERE item_id = ?", (point_id, item_id))


async def list_chats_without_point() -> list[models.ChatSummary]:
    rows = await _fetchall("SELECT * FROM chats WHERE point_id IS NULL ORDER BY last_message_at DESC")
    return [models.ChatSummary.from_row(r) for r in rows]


# --- subscriptions -----------------------------------------------------------


async def get_user_points(telegram_id: int) -> list[models.Point]:
    rows = await _fetchall(
        """
        SELECT p.* FROM points p
        JOIN subscriptions s ON s.point_id = p.id
        WHERE s.user_id = ?
        ORDER BY p.name
        """,
        (telegram_id,),
    )
    return [models.Point.from_row(r) for r in rows]


async def subscribe_user_to_point(user_id: int, point_id: int) -> None:
    await _execute(
        "INSERT OR IGNORE INTO subscriptions (user_id, point_id) VALUES (?, ?)",
        (user_id, point_id),
    )


async def unsubscribe_user_from_point(user_id: int, point_id: int) -> None:
    await _execute("DELETE FROM subscriptions WHERE user_id = ? AND point_id = ?", (user_id, point_id))


async def list_point_subscribers(point_id: int, on_shift_only: bool = True) -> list[models.User]:
    if on_shift_only:
        rows = await _fetchall(
            """
            SELECT u.* FROM users u
            JOIN subscriptions s ON s.user_id = u.telegram_id
            WHERE s.point_id = ? AND u.status = ? AND u.on_shift = 1 AND u.blocked_bot = 0
            """,
            (point_id, constants.STATUS_APPROVED),
        )
    else:
        rows = await _fetchall(
            """
            SELECT u.* FROM users u
            JOIN subscriptions s ON s.user_id = u.telegram_id
            WHERE s.point_id = ?
            """,
            (point_id,),
        )
    return [models.User.from_row(r) for r in rows]


async def list_admins_and_directors() -> list[models.User]:
    rows = await _fetchall(
        "SELECT * FROM users WHERE role IN (?, ?) AND status = ?",
        (constants.ADMIN, constants.DIRECTOR, constants.STATUS_APPROVED),
    )
    return [models.User.from_row(r) for r in rows]


async def list_approved_users(exclude_unreachable: bool = True) -> list[models.User]:
    if exclude_unreachable:
        rows = await _fetchall(
            "SELECT * FROM users WHERE status = ? AND blocked_bot = 0", (constants.STATUS_APPROVED,)
        )
    else:
        rows = await _fetchall("SELECT * FROM users WHERE status = ?", (constants.STATUS_APPROVED,))
    return [models.User.from_row(r) for r in rows]


# --- Avito accounts ------------------------------------------------------


async def list_avito_accounts(active_only: bool = True) -> list[models.AvitoAccount]:
    if active_only:
        rows = await _fetchall("SELECT * FROM avito_accounts WHERE is_active = 1")
    else:
        rows = await _fetchall("SELECT * FROM avito_accounts")
    return [models.AvitoAccount.from_row(r) for r in rows]


async def get_avito_account(account_id: int) -> models.AvitoAccount | None:
    row = await _fetchone("SELECT * FROM avito_accounts WHERE id = ?", (account_id,))
    return models.AvitoAccount.from_row(row) if row else None


async def get_avito_account_by_client_id(client_id: str) -> models.AvitoAccount | None:
    row = await _fetchone("SELECT * FROM avito_accounts WHERE client_id = ?", (client_id,))
    return models.AvitoAccount.from_row(row) if row else None


async def create_avito_account(name: str, avito_user_id: int, client_id: str, client_secret: str,
                                point_id: int | None = None) -> models.AvitoAccount:
    cur = await _execute(
        """
        INSERT INTO avito_accounts (point_id, avito_user_id, name, client_id, client_secret)
        VALUES (?, ?, ?, ?, ?)
        """,
        (point_id, avito_user_id, name, client_id, client_secret),
    )
    account = await get_avito_account(cur.lastrowid)
    assert account is not None
    return account


async def update_avito_account_credentials(account_id: int, client_id: str, client_secret: str) -> None:
    await _execute(
        "UPDATE avito_accounts SET client_id = ?, client_secret = ?, access_token = NULL, token_expires_at = NULL WHERE id = ?",
        (client_id, client_secret, account_id),
    )


async def set_avito_account_active(account_id: int, is_active: bool) -> None:
    await _execute("UPDATE avito_accounts SET is_active = ? WHERE id = ?", (1 if is_active else 0, account_id))


async def update_avito_token(account_id: int, access_token: str, expires_at: datetime) -> None:
    await _execute(
        "UPDATE avito_accounts SET access_token = ?, token_expires_at = ? WHERE id = ?",
        (access_token, expires_at.strftime("%Y-%m-%d %H:%M:%S"), account_id),
    )


async def set_avito_account_error(account_id: int, error: str | None) -> None:
    await _execute(
        "UPDATE avito_accounts SET last_poll_at = datetime('now'), last_poll_error = ? WHERE id = ?",
        (error, account_id),
    )


# --- AI config ---------------------------------------------------------------


async def get_ai_config() -> models.AIConfig:
    row = await _fetchone("SELECT * FROM ai_config WHERE id = 1")
    assert row is not None
    return models.AIConfig.from_row(row)


async def update_ai_config(actor_id: int | None = None, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if actor_id is not None:
        fields["updated_by"] = actor_id
    columns = ", ".join(f"{k} = ?" for k in fields)
    await _execute(f"UPDATE ai_config SET {columns} WHERE id = 1", tuple(fields.values()))


# --- proxy config --------------------------------------------------------


async def get_proxy_config() -> models.ProxyConfig:
    row = await _fetchone("SELECT * FROM proxy_config WHERE id = 1")
    assert row is not None
    return models.ProxyConfig.from_row(row)


async def update_proxy_config(actor_id: int | None = None, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if actor_id is not None:
        fields["updated_by"] = actor_id
    columns = ", ".join(f"{k} = ?" for k in fields)
    await _execute(f"UPDATE proxy_config SET {columns} WHERE id = 1", tuple(fields.values()))


# --- payment config / payments -----------------------------------------------


async def get_payment_config() -> models.PaymentConfig:
    row = await _fetchone("SELECT * FROM payment_config WHERE id = 1")
    assert row is not None
    return models.PaymentConfig.from_row(row)


async def update_payment_config(actor_id: int | None = None, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if actor_id is not None:
        fields["updated_by"] = actor_id
    columns = ", ".join(f"{k} = ?" for k in fields)
    await _execute(f"UPDATE payment_config SET {columns} WHERE id = 1", tuple(fields.values()))


async def log_payment(user_id: int, telegram_charge_id: str, amount_stars: int) -> None:
    await _execute(
        "INSERT INTO payments (user_id, telegram_charge_id, amount_stars) VALUES (?, ?, ?)",
        (user_id, telegram_charge_id, amount_stars),
    )


async def has_paid(user_id: int) -> bool:
    row = await _fetchone(
        "SELECT id FROM payments WHERE user_id = ? AND refunded_at IS NULL ORDER BY paid_at DESC LIMIT 1",
        (user_id,),
    )
    return row is not None


async def get_payment_for_user(user_id: int) -> models.Payment | None:
    row = await _fetchone(
        "SELECT * FROM payments WHERE user_id = ? AND refunded_at IS NULL ORDER BY paid_at DESC LIMIT 1",
        (user_id,),
    )
    return models.Payment.from_row(row) if row else None


async def mark_payment_refunded(payment_id: int) -> None:
    await _execute("UPDATE payments SET refunded_at = datetime('now') WHERE id = ?", (payment_id,))


# --- welcome message -----------------------------------------------------


async def get_welcome_message() -> str:
    row = await _fetchone("SELECT text FROM welcome_config WHERE id = 1")
    return row["text"] if row else ""


async def update_welcome_message(text: str, actor_id: int) -> None:
    await _execute(
        "UPDATE welcome_config SET text = ?, updated_at = datetime('now'), updated_by = ? WHERE id = 1",
        (text, actor_id),
    )


# --- backup config -------------------------------------------------------


async def get_backup_config() -> models.BackupConfig:
    row = await _fetchone("SELECT * FROM backup_config WHERE id = 1")
    assert row is not None
    return models.BackupConfig.from_row(row)


async def update_backup_config(actor_id: int | None = None, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if actor_id is not None:
        fields["updated_by"] = actor_id
    columns = ", ".join(f"{k} = ?" for k in fields)
    await _execute(f"UPDATE backup_config SET {columns} WHERE id = 1", tuple(fields.values()))


async def mark_backup_done(at: datetime) -> None:
    await _execute(
        "UPDATE backup_config SET last_backup_at = ? WHERE id = 1",
        (at.strftime("%Y-%m-%d %H:%M:%S"),),
    )


# --- chats / messages ------------------------------------------------------


async def get_chat_summary(chat_id: str) -> models.ChatSummary | None:
    row = await _fetchone("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))
    return models.ChatSummary.from_row(row) if row else None


async def list_all_chats() -> list[models.ChatSummary]:
    rows = await _fetchall("SELECT * FROM chats")
    return [models.ChatSummary.from_row(r) for r in rows]


async def upsert_chat_summary(chat_id: str, avito_account_id: int, point_id: int | None = None,
                               item_id: str | None = None, client_name: str | None = None,
                               last_message_at: str | None = None, last_message_text: str | None = None,
                               last_message_dir: str | None = None) -> None:
    existing = await get_chat_summary(chat_id)
    if existing is None:
        await _execute(
            """
            INSERT INTO chats (chat_id, avito_account_id, point_id, item_id, client_name,
                                last_message_at, last_message_text, last_message_dir)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_id, avito_account_id, point_id, item_id, client_name,
             last_message_at, last_message_text, last_message_dir),
        )
        return
    await _execute(
        """
        UPDATE chats SET
            point_id = COALESCE(?, point_id),
            item_id = COALESCE(?, item_id),
            client_name = COALESCE(?, client_name),
            last_message_at = COALESCE(?, last_message_at),
            last_message_text = COALESCE(?, last_message_text),
            last_message_dir = COALESCE(?, last_message_dir),
            updated_at = datetime('now')
        WHERE chat_id = ?
        """,
        (point_id, item_id, client_name, last_message_at, last_message_text, last_message_dir, chat_id),
    )


async def set_chat_unread_count(chat_id: str, unread_count: int) -> None:
    if unread_count == 0:
        await _execute(
            "UPDATE chats SET unread_count = 0, read_at = datetime('now') WHERE chat_id = ?", (chat_id,)
        )
    else:
        await _execute("UPDATE chats SET unread_count = ? WHERE chat_id = ?", (unread_count, chat_id))


async def mark_chat_replied(chat_id: str, user_id: int) -> None:
    await _execute(
        """
        UPDATE chats SET unread_count = 0, last_replied_by = ?, last_replied_at = datetime('now'),
               read_at = datetime('now')
        WHERE chat_id = ?
        """,
        (user_id, chat_id),
    )


async def get_recent_chats(point_ids: set[int] | None, within_minutes: int = constants.RECENT_REPLIES_WINDOW_MINUTES) -> list[models.ChatSummary]:
    cutoff = (datetime.utcnow() - timedelta(minutes=within_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    if point_ids is None:
        rows = await _fetchall(
            "SELECT * FROM chats WHERE last_replied_at >= ? ORDER BY last_replied_at DESC",
            (cutoff,),
        )
    else:
        if not point_ids:
            return []
        placeholders = ",".join("?" for _ in point_ids)
        rows = await _fetchall(
            f"SELECT * FROM chats WHERE last_replied_at >= ? AND point_id IN ({placeholders}) ORDER BY last_replied_at DESC",
            (cutoff, *point_ids),
        )
    return [models.ChatSummary.from_row(r) for r in rows]


async def get_unread_chats(point_ids: set[int] | None) -> list[models.ChatSummary]:
    if point_ids is None:
        rows = await _fetchall("SELECT * FROM chats WHERE unread_count > 0 ORDER BY last_message_at DESC")
    else:
        if not point_ids:
            return []
        placeholders = ",".join("?" for _ in point_ids)
        rows = await _fetchall(
            f"SELECT * FROM chats WHERE unread_count > 0 AND point_id IN ({placeholders}) ORDER BY last_message_at DESC",
            tuple(point_ids),
        )
    return [models.ChatSummary.from_row(r) for r in rows]


async def append_message(chat_id: str, direction: str, text: str | None, has_image: bool, sent_at: str,
                          avito_message_id: str | None = None, message_uuid: str | None = None) -> None:
    await _execute(
        """
        INSERT INTO messages (chat_id, avito_message_id, message_uuid, direction, text, has_image, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, avito_message_id, message_uuid, direction, text, 1 if has_image else 0, sent_at),
    )


async def get_known_message_ids(chat_id: str) -> set[str]:
    """Durable dedup set — survives process restarts, unlike bot_cache."""
    rows = await _fetchall(
        "SELECT avito_message_id FROM messages WHERE chat_id = ? AND avito_message_id IS NOT NULL",
        (chat_id,),
    )
    return {r["avito_message_id"] for r in rows}


async def get_recent_messages(chat_id: str, limit: int = 20) -> list[models.Message]:
    rows = await _fetchall(
        "SELECT * FROM messages WHERE chat_id = ? ORDER BY sent_at DESC LIMIT ?",
        (chat_id, limit),
    )
    return list(reversed([models.Message.from_row(r) for r in rows]))


async def prune_old_messages(older_than_days: int = constants.MESSAGE_RETENTION_DAYS) -> int:
    cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
    cur = await _execute("DELETE FROM messages WHERE sent_at < ?", (cutoff,))
    return cur.rowcount


# --- templates ---------------------------------------------------------------


async def list_templates(point_id: int | None, kind: str | None = None) -> list[models.Template]:
    if kind is None:
        rows = await _fetchall(
            "SELECT * FROM templates WHERE point_id = ? AND is_active = 1 ORDER BY title",
            (point_id,),
        )
    else:
        rows = await _fetchall(
            "SELECT * FROM templates WHERE point_id = ? AND kind = ? AND is_active = 1 ORDER BY title",
            (point_id, kind),
        )
    return [models.Template.from_row(r) for r in rows]


async def get_template(template_id: int) -> models.Template | None:
    row = await _fetchone("SELECT * FROM templates WHERE id = ?", (template_id,))
    return models.Template.from_row(row) if row else None


async def create_template(point_id: int | None, kind: str, title: str, body: str, created_by: int) -> models.Template:
    cur = await _execute(
        "INSERT INTO templates (point_id, kind, title, body, created_by) VALUES (?, ?, ?, ?, ?)",
        (point_id, kind, title, body, created_by),
    )
    template = await get_template(cur.lastrowid)
    assert template is not None
    return template


async def deactivate_template(template_id: int) -> None:
    await _execute("UPDATE templates SET is_active = 0 WHERE id = ?", (template_id,))


# --- broadcasts / access log ---------------------------------------------


async def log_broadcast(author_id: int, text: str | None, photo_file_id: str | None, sent: int, failed: int) -> int:
    cur = await _execute(
        "INSERT INTO broadcasts (author_id, text, photo_file_id, sent_count, failed_count) VALUES (?, ?, ?, ?, ?)",
        (author_id, text, photo_file_id, sent, failed),
    )
    return cur.lastrowid


async def log_access_request(user_id: int, action: str, actor_id: int | None, note: str | None = None) -> None:
    await _execute(
        "INSERT INTO access_requests (user_id, action, actor_id, note) VALUES (?, ?, ?, ?)",
        (user_id, action, actor_id, note),
    )


# --- credentials seed file ----------------------------------------------


async def seed_from_credentials_file(path: str | None) -> None:
    if not path:
        return
    file = Path(path)
    if not file.exists():
        return

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11 fallback, unused on py3.12 target
        return

    data = tomllib.loads(file.read_text(encoding="utf-8"))

    for entry in data.get("avito_accounts", []):
        client_id = entry.get("client_id")
        if not client_id or client_id == "REPLACE_ME":
            continue
        existing = await get_avito_account_by_client_id(client_id)
        if existing is not None:
            continue
        await create_avito_account(
            name=entry.get("name", "Avito account"),
            avito_user_id=int(entry.get("avito_user_id", 0)),
            client_id=client_id,
            client_secret=entry.get("client_secret", ""),
        )

    ai_section = data.get("ai") or {}
    if ai_section:
        current = await get_ai_config()
        # `updated_by IS NULL` is the "still untouched via the admin panel"
        # marker for the whole row — every update_ai_config() call sets it.
        # Without this gate, re-running with the file in place would silently
        # clobber an admin's later customization on every restart.
        untouched = current.updated_by is None
        updates: dict[str, Any] = {}
        if untouched:
            if ai_section.get("base_url"):
                updates["base_url"] = ai_section["base_url"]
            if ai_section.get("model"):
                updates["model"] = ai_section["model"]
            if ai_section.get("extra_header_name"):
                updates["extra_header_name"] = ai_section["extra_header_name"]
            if ai_section.get("extra_header_value"):
                updates["extra_header_value"] = ai_section["extra_header_value"]
        if not current.api_key and ai_section.get("api_key") not in (None, "", "REPLACE_ME"):
            updates["api_key"] = ai_section["api_key"]
        if updates:
            await update_ai_config(**updates)

    proxy_section = data.get("proxy") or {}
    if proxy_section and proxy_section.get("url"):
        current_proxy = await get_proxy_config()
        if not current_proxy.proxy_url:
            await update_proxy_config(
                is_enabled=1 if proxy_section.get("enabled") else 0,
                proxy_url=proxy_section.get("url"),
                proxy_login=proxy_section.get("login") or None,
                proxy_password=proxy_section.get("password") or None,
            )
