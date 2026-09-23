"""Roles, statuses, callback_data prefixes and known button texts.

No internal imports — every other module in the project imports from here,
so this file must stay a leaf.
"""

from __future__ import annotations

# --- Roles -------------------------------------------------------------
# DB-stable role codes (used in CHECK constraints and everywhere in code).
# Display names shown in the bot UI live separately in ROLE_LABELS — the
# two were deliberately decoupled (see ROLE_LABELS) after the "nice role
# names" request changed the labels without touching the stored codes.
EMPLOYEE = "employee"
MANAGER = "manager"
ADMIN = "admin"
DIRECTOR = "director"

ROLE_ORDER: dict[str, int] = {
    EMPLOYEE: 0,
    MANAGER: 1,
    ADMIN: 2,
    DIRECTOR: 3,
}

ROLE_LABELS: dict[str, str] = {
    EMPLOYEE: "🧑‍💼 Сотрудник точки",
    MANAGER: "📋 Ответственный точки",
    ADMIN: "🛡 РОП",
    DIRECTOR: "👑 Админ",
}

# --- Avito Delivery order statuses ----------------------------------------
ORDER_STATUS_LABELS: dict[str, str] = {
    "on_confirmation": "⏳ Ожидает подтверждения",
    "ready_to_ship": "📦 Готов к отправке",
    "in_transit": "🚚 В пути",
    "canceled": "❌ Отменён",
    "delivered": "✅ Доставлен",
    "on_return": "↩️ На возврате",
    "in_dispute": "⚠️ Открыт спор",
    "closed": "🔒 Закрыт",
}

# Statuses worth showing/polling — excludes final states (canceled/
# delivered/closed) where there's nothing left to do.
ORDER_ACTIVE_STATUSES = ["on_confirmation", "ready_to_ship", "in_transit", "on_return", "in_dispute"]
# Safety ceiling for GET .../orders pagination (20/page) — well above any
# realistic order count, guards against an "hasMore" that never goes false.
ORDER_MAX_PAGES = 20
# How long an interactive screen may reuse an already-fetched order list
# instead of re-walking every page again. One full get_orders() on this
# account is ~5 paginated requests at 1 req/sec, so reopening a card used
# to cost seconds of waiting for data that had just been fetched. Only the
# opt-in (use_cache=True) callers read it — the new-order poller never
# does, so notifications stay as timely as ORDER_POLL_INTERVAL_SECONDS.
ORDERS_CACHE_TTL_SECONDS = 300

# --- User status ---------------------------------------------------------
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_BLOCKED = "blocked"

# --- Template kinds --------------------------------------------------------
TEMPLATE_TEXT = "text"
TEMPLATE_AI_PROMPT = "ai_prompt"

# --- Main menu / reply keyboard button texts --------------------------------
BTN_HOME = "🏠 Главное меню"
BTN_SHIFT_ON = "💼 На смене"
BTN_SHIFT_OFF = "🛌 Отдыхаю"
BTN_UNREAD = "📩 Непрочитанные"
BTN_RECENT = "🕒 Недавние"
BTN_PROFILE = "👤 Мой профиль"
BTN_MY_POINTS = "📍 Мои точки"
BTN_MY_TEMPLATES = "📋 Мои шаблоны"
BTN_ORDERS = "📦 Заказы Avito"
BTN_ADMIN_PANEL = "⚙️ Админпанель"
BTN_LEADERSHIP = "👔 Меню руководителя"
BTN_CANCEL = "❌ Отмена"
BTN_BACK = "◀️ Назад"

# Union of every reply-keyboard button label the bot ever shows, anywhere.
# SafeFreeText (filters.py) rejects any of these from being treated as
# free-form input (a chat reply, an admin form field, ...) — the whole
# point is one registry so nothing gets missed by adding a screen later.
ALL_KNOWN_BUTTON_TEXTS: frozenset[str] = frozenset(
    {
        BTN_HOME,
        BTN_SHIFT_ON,
        BTN_SHIFT_OFF,
        BTN_UNREAD,
        BTN_RECENT,
        BTN_PROFILE,
        BTN_MY_POINTS,
        BTN_MY_TEMPLATES,
        BTN_ORDERS,
        BTN_ADMIN_PANEL,
        BTN_LEADERSHIP,
        BTN_CANCEL,
        BTN_BACK,
    }
)

# --- callback_data prefixes -------------------------------------------------
# Always parsed as callback.data.split("_", 1) -> (prefix, payload).
# Payload for anything chat-related is always bot_cache's short_id (clean
# hex, no "_"), never the raw Avito chat_id.
PREFIX_CHAT = "chat"
PREFIX_REPLY = "reply"
PREFIX_AIDRAFT = "aidraft"
PREFIX_AIDRAFT_AUTO = "aidraftauto"
PREFIX_AIDRAFT_PROMPT = "aidraftprompt"
PREFIX_AISEND = "aisend"
PREFIX_AIEDIT = "aiedit"
PREFIX_AICANCEL = "aicancel"
PREFIX_SHIFT = "shift"
PREFIX_APPR = "appr"
PREFIX_BLK = "blk"
PREFIX_BLKREFUND = "blkrefund"
PREFIX_POINT = "point"
PREFIX_TPL = "tpl"
PREFIX_ADM = "adm"
PREFIX_DELMSG = "delmsg"
PREFIX_REASSIGN = "reassign"
PREFIX_ORDUNASSIGNED = "ordunassigned"
PREFIX_ORDPOINT = "ordpoint"
PREFIX_ORDREFRESH = "ordrefresh"
PREFIX_ORDREFRESHONE = "ordrefone"
PREFIX_NOTES = "notes"
PREFIX_NOTEADD = "noteadd"
PREFIX_NOTEDEL = "notedel"
PREFIX_LATER = "later"
PREFIX_LATERPICK = "latpick"
PREFIX_LATERCANCEL = "latcancel"
PREFIX_READ = "read"
PREFIX_REFRESH = "refresh"
PREFIX_RATING = "rating"
PREFIX_BACKMENU = "backmenu"

# --- Timing / tuning constants ----------------------------------------------
REPLY_STATE_TTL_MINUTES = 15
MEDIA_GROUP_DEBOUNCE_SECONDS = 1.0
START_COOLDOWN_SECONDS = 30
DOUBLE_CLICK_TTL_SECONDS = 3.0

POLL_INTERVAL_SECONDS = 15
# Most cycles ask Avito only for its own unread chats; every Nth asks for
# all of them. The full pass is the ONLY way this bot ever sees a chat that
# has left Avito's unread list — which is exactly what happens when someone
# reads and answers a client directly in Avito's own app. So this interval
# is the ceiling on how long such a reply stays missing from the ambient
# list, and it used to be 20 (~5 minutes).
#
# 8 (~2 minutes) is affordable now and was not before: a full pass used to
# cost one throttled get_messages() per chat, because the messages table did
# not persist is_read and nothing could short-circuit. Since migration 015
# it does, so the overwhelming majority of chats on a full pass take the
# short exit and cost nothing.
# Вернулось к 20 с 8. Понижая, я исходил из того, что после миграции 015
# полная сверка стала дешёвой — но для чатов, у которых история уже вычищена
# по MESSAGE_RETENTION_DAYS, это было неверно: они не проходили короткий
# выход и стоили по запросу каждый раз. Корень починен отдельно (кэш берёт
# last_message_at из сводки), а здесь возвращается запас, пока дешевизна
# полной сверки не подтверждена замером, а не рассуждением.
FULL_SYNC_EVERY_N_POLLS = 20
# Safety ceiling for GET .../chats pagination during polling (100/page).
# Avito's own OpenAPI spec caps the offset parameter at 1000, so 10 pages
# (offset up to 900) is the real usable ceiling, not an arbitrary guess.
CHAT_POLL_MAX_PAGES = 10
# How long a chat we still count as unread may go without a live
# get_messages() re-check.
#
# A chat with an unanswered client message can be read by a human directly
# in Avito's own app, without a new message ever arriving — re-fetching is
# the only way we ever see that flip. But re-fetching it on EVERY cycle is
# what made notifications minutes late: the normal cycle asks Avito only
# for unread chats, so every one of them failed the short-circuit and cost
# one throttled request (1 req/sec per account), making a cycle
# 1 + <unread chats> seconds long before its 15s sleep even started. With
# 100 unread chats that is a ~2 minute cycle, and a brand-new message waits
# a full cycle plus its place in the queue.
#
# 120s cuts that dominant cost by 8x at POLL_INTERVAL_SECONDS=15.
#
# Note what this pacing does and does not cover: it only applies to chats
# Avito still returns as unread. A chat someone read AND answered in Avito's
# own app usually leaves that list altogether, so it is not reconciled here
# at all — it waits for the full pass (FULL_SYNC_EVERY_N_POLLS above). An
# earlier version of this comment claimed the reconciliation was bounded by
# these 120s in general; that was true only for the still-unread case.
UNREAD_RECHECK_SECONDS = 120
ACCOUNT_RELOAD_INTERVAL_SECONDS = 300
MESSAGE_PRUNE_INTERVAL_SECONDS = 3600
MESSAGE_RETENTION_DAYS = 30
# Чистка самих чатов, в отличие от чистки сообщений выше.
#
# Раз в сутки, а не раз в час: удаление чата каскадом уносит его messages, а
# это долговечная защита от повторных уведомлений. Операция необратимая, спешить
# с ней некуда, и по умолчанию она вообще выключена (chat_cleanup_config.is_enabled
# = 0) — сперва человек смотрит цифру "сколько удалится".
CHAT_CLEANUP_INTERVAL_SECONDS = 86400
# 90, а не 30, при том что сообщения живут 30 дней.
#
# Запас нужен из-за короткого выхода в _process_chat: у чата, которого нет ни в
# кэше, ни в базе, last_message_at пустой, поэтому он проваливается мимо
# короткого выхода и покупает один throttled get_messages(). Это ровно та цена,
# которая стоила 705-780 секунд на 1000 чатов. Порядок сортировки списка чатов
# в Avito не задокументирован, так что "старое всё равно не попадёт в первую
# тысячу" — предположение, а не факт. Возраст с запасом — то, чем мы платим
# вместо того, чтобы на это предположение опираться. Значение на строке chats
# правится из интерфейса, здесь — дефолт для новой установки.
CHAT_RETENTION_DAYS = 90
# Сколько чатов уносим за один заход. Ограничение не ради SQLite — ему эти
# тысячи строк безразличны — а ради опроса Avito. Каждый удалённый чат, который
# Avito ещё вернёт в списке, стоит одного throttled get_messages(), потому что
# без строки в базе ему неоткуда взять last_message_at для короткого выхода.
# Сколько таких вернётся, заранее неизвестно: порядок списка чатов у Avito не
# задокументирован. 200 в сутки — это потолок примерно в 200 секунд бюджета на
# аккаунт, размазанный по суткам, вместо непредсказуемой паузы сразу после
# первого включения. Первый заход и так самый большой: дальше чистить нечего.
CHAT_CLEANUP_BATCH_LIMIT = 200
# Что поднимаем в память при старте. Отдельная величина от CHAT_RETENTION_DAYS
# намеренно: гидрация ничего не удаляет, её порог безопасно держать жёстче, и
# именно он определяет, сколько времени бот молчит после рестарта.
CHAT_HYDRATION_DAYS = 90
BACKUP_LOOP_INTERVAL_SECONDS = 3600
# Orders share one 1 req/sec budget per account with the chat poller, and a
# pass costs several paginated requests. Orders have no second-level
# urgency; a client's message does — so orders poll rarely enough to stay
# out of the way.
ORDER_POLL_INTERVAL_SECONDS = 300
# How often to look for scheduled replies that have come due. 30s keeps the
# worst-case lateness under half a minute, which is well inside what anyone
# means by "send this at 18:00".
SCHEDULED_REPLY_CHECK_INTERVAL_SECONDS = 30
# Heartbeat granularity. It only exists so a start can say how long the bot
# was down, so a minute is plenty — and it keeps the write cheap.
HEARTBEAT_INTERVAL_SECONDS = 60
# Furthest ahead a reply may be scheduled (a week). Past that it is far more
# likely a typo in the minutes field than a real intention.
SCHEDULED_REPLY_MAX_MINUTES = 7 * 24 * 60

# Telegram caps a photo caption at 1024 characters (a plain message allows
# 4096). A broadcast whose text is longer therefore cannot ride along as a
# caption — it is sent as its own message right under the photo instead.
TELEGRAM_CAPTION_LIMIT = 1024

ERROR_BACKOFF_BASE_SECONDS = 30
ERROR_BACKOFF_MAX_SECONDS = 600

AVITO_MIN_REQUEST_INTERVAL_SECONDS = 1.0
AVITO_MAX_RETRIES = 3
# Hard ceiling on a single Avito HTTP attempt. aiohttp's default is 5
# minutes, which with AVITO_MAX_RETRIES meant one request could hold an
# account's serial poll loop for ~15 minutes.
AVITO_REQUEST_TIMEOUT_SECONDS = 30

COORD_MAX_DISTANCE_M = 25.0
POINT_CONFLICT_WARNING_M = 150.0
RECENT_REPLIES_WINDOW_MINUTES = 60
SHORT_ID_LENGTH = 8

MSK_OFFSET_HOURS = 3

INFLIGHT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

GRACE_ACCESS_REQUEST_NOTE_LIMIT = 500
