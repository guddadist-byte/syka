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
FULL_SYNC_EVERY_N_POLLS = 20
ACCOUNT_RELOAD_INTERVAL_SECONDS = 300
MESSAGE_PRUNE_INTERVAL_SECONDS = 3600
MESSAGE_RETENTION_DAYS = 30
BACKUP_LOOP_INTERVAL_SECONDS = 3600

ERROR_BACKOFF_BASE_SECONDS = 30
ERROR_BACKOFF_MAX_SECONDS = 600

AVITO_MIN_REQUEST_INTERVAL_SECONDS = 1.0
AVITO_MAX_RETRIES = 3

COORD_MAX_DISTANCE_M = 25.0
POINT_CONFLICT_WARNING_M = 150.0
RECENT_REPLIES_WINDOW_MINUTES = 60
SHORT_ID_LENGTH = 8

MSK_OFFSET_HOURS = 3

INFLIGHT_SHUTDOWN_TIMEOUT_SECONDS = 10.0

GRACE_ACCESS_REQUEST_NOTE_LIMIT = 500
