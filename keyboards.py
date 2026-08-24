"""Inline / reply keyboard builders.

callback_data convention throughout this file and handlers.py: always
"<prefix>_<payload>", parsed with callback.data.split("_", 1) (maxsplit=1)
so a raw Avito id containing "_" in payload never breaks parsing. Any
payload that itself needs more than one field uses a second, app-owned
":"-delimiter after that first split (e.g. "point_sub:123:45").
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

import constants
from bot_cache import CachedChat
from constants import ROLE_LABELS
from models import Point, Template, User


# --- main menu ---------------------------------------------------------------


def main_menu_kb(on_shift: bool, role: str) -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=constants.BTN_SHIFT_OFF if on_shift else constants.BTN_SHIFT_ON))
    builder.row(
        KeyboardButton(text=constants.BTN_UNREAD),
        KeyboardButton(text=constants.BTN_RECENT),
    )
    builder.row(KeyboardButton(text=constants.BTN_PROFILE))
    if role == constants.MANAGER:
        builder.row(KeyboardButton(text=constants.BTN_MY_TEMPLATES))
    if constants.ROLE_ORDER.get(role, 0) >= constants.ROLE_ORDER[constants.ADMIN]:
        builder.row(KeyboardButton(text=constants.BTN_ADMIN_PANEL))
    return builder.as_markup(resize_keyboard=True)


def cancel_reply_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text=constants.BTN_HOME))
    return builder.as_markup(resize_keyboard=True)


# --- chat lists ----------------------------------------------------------


def chat_list_kb(chats: list[CachedChat]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        label = f"💬 {chat.client_name or 'Клиент'}"
        if chat.unread_count:
            label = f"📩 {chat.unread_count} · {label}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"{constants.PREFIX_CHAT}_{chat.short_id}"))
    return builder.as_markup()


def unassigned_chats_kb(chats: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for chat in chats:
        label = f"📭 {chat.client_name or chat.chat_id}"
        builder.row(InlineKeyboardButton(text=label, callback_data=f"{constants.PREFIX_CHAT}_{chat.chat_id}"))
    return builder.as_markup()


def chat_notification_kb(short_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💬 Ответить", callback_data=f"{constants.PREFIX_REPLY}_{short_id}"))
    return builder.as_markup()


def chat_detail_kb(short_id: str, can_reassign: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Прочитано", callback_data=f"{constants.PREFIX_READ}_{short_id}"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"{constants.PREFIX_REFRESH}_{short_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⏰ Через 2 часа", callback_data=f"{constants.PREFIX_SNZ2H}_{short_id}"),
        InlineKeyboardButton(text="🌅 Завтра утром", callback_data=f"{constants.PREFIX_SNZAM}_{short_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🧠 ИИ-ответ", callback_data=f"{constants.PREFIX_AIDRAFT}_{short_id}"),
        InlineKeyboardButton(text="📋 Шаблоны", callback_data=f"{constants.PREFIX_TPL}_{short_id}"),
    )
    if can_reassign:
        builder.row(
            InlineKeyboardButton(
                text="🔀 Переназначить точку", callback_data=f"{constants.PREFIX_REASSIGN}_{short_id}"
            )
        )
    return builder.as_markup()


def sent_message_kb(msg_ref: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить сообщение", callback_data=f"{constants.PREFIX_DELMSG}_{msg_ref}")
    )
    return builder.as_markup()


def ai_draft_kb(short_id: str, allow_send: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if allow_send:
        builder.row(
            InlineKeyboardButton(text="✅ Отправить", callback_data=f"{constants.PREFIX_AISEND}_{short_id}")
        )
    builder.row(
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"{constants.PREFIX_AIEDIT}_{short_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"{constants.PREFIX_AICANCEL}_{short_id}"),
    )
    return builder.as_markup()


# --- templates -----------------------------------------------------------


def template_list_kb(templates: list[Template], short_id: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for template in templates:
        icon = "🧠" if template.kind == constants.TEMPLATE_AI_PROMPT else "📝"
        builder.row(
            InlineKeyboardButton(
                text=f"{icon} {template.title}",
                callback_data=f"{constants.PREFIX_TPL}_{template.id}:{short_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_form"))
    return builder.as_markup()


# --- admin panel / leadership menu -----------------------------------------


def admin_panel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👔 Меню руководителя", callback_data="adm_leadership"))
    builder.row(InlineKeyboardButton(text="🔑 Avito API", callback_data="adm_avito"))
    builder.row(InlineKeyboardButton(text="🧠 Настройки ИИ", callback_data="adm_ai"))
    builder.row(InlineKeyboardButton(text="🌐 Прокси", callback_data="adm_proxy"))
    builder.row(InlineKeyboardButton(text="🏢 Настройки подразделений", callback_data="adm_points"))
    builder.row(InlineKeyboardButton(text="📭 Чаты без точки", callback_data="adm_unassigned"))
    builder.row(InlineKeyboardButton(text="⭐ Платный доступ", callback_data="adm_payment"))
    builder.row(InlineKeyboardButton(text="✉️ Приветственное сообщение", callback_data="adm_welcome"))
    builder.row(InlineKeyboardButton(text="💾 Резервные копии", callback_data="adm_backup"))
    return builder.as_markup()


def leadership_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm_users"))
    builder.row(InlineKeyboardButton(text="📢 Сообщение всем", callback_data="adm_broadcast"))
    builder.row(InlineKeyboardButton(text="📭 Чаты без точки", callback_data="adm_unassigned"))
    return builder.as_markup()


def user_management_kb(users: list[User]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user in users:
        label = user.full_name or user.username or str(user.telegram_id)
        role_label = ROLE_LABELS.get(user.role, user.role)
        builder.row(
            InlineKeyboardButton(
                text=f"✏️ {label} ({role_label})", callback_data=f"adm_useredit_{user.telegram_id}"
            ),
            InlineKeyboardButton(text="🚫 Уволить", callback_data=f"{constants.PREFIX_BLK}_{user.telegram_id}"),
        )
    return builder.as_markup()


def role_select_kb(user_id: int, allow_admin_roles: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    roles = [constants.EMPLOYEE, constants.MANAGER]
    if allow_admin_roles:
        roles += [constants.ADMIN, constants.DIRECTOR]
    for role in roles:
        builder.row(
            InlineKeyboardButton(
                text=ROLE_LABELS[role], callback_data=f"adm_setrole_{role}:{user_id}"
            )
        )
    return builder.as_markup()


def point_multiselect_kb(points: list[Point], selected: set[int], mode: str, key: str) -> InlineKeyboardMarkup:
    """mode: 'sub' (multi, toggling, with a Done button) or 'single' (pick one, confirms immediately)."""
    builder = InlineKeyboardBuilder()
    for point in points:
        mark = "✅ " if point.id in selected else ""
        builder.row(
            InlineKeyboardButton(
                text=f"{mark}{point.name}",
                callback_data=f"{constants.PREFIX_POINT}_{mode}:{key}:{point.id}",
            )
        )
    if mode == "sub":
        builder.row(InlineKeyboardButton(text="✅ Готово", callback_data=f"{constants.PREFIX_POINT}_subdone:{key}"))
    return builder.as_markup()


def access_decision_kb(user_id: int, has_unrefunded_payment: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Одобрить", callback_data=f"{constants.PREFIX_APPR}_{user_id}"))
    if has_unrefunded_payment:
        builder.row(
            InlineKeyboardButton(text="❌ Отклонить без возврата", callback_data=f"{constants.PREFIX_BLK}_{user_id}"),
            InlineKeyboardButton(text="💸 Отклонить с возвратом", callback_data=f"{constants.PREFIX_BLKREFUND}_{user_id}"),
        )
    else:
        builder.row(InlineKeyboardButton(text="❌ Отклонить", callback_data=f"{constants.PREFIX_BLK}_{user_id}"))
    return builder.as_markup()


# --- profile / rating ------------------------------------------------------


def profile_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📊 Общий рейтинг", callback_data=f"{constants.PREFIX_RATING}_all"))
    return builder.as_markup()


# --- broadcast / backup / generic -------------------------------------------


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Отправить", callback_data="adm_broadcastsend"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_form"),
    )
    return builder.as_markup()


def backup_settings_kb(is_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔴 Выключить" if is_enabled else "🟢 Включить", callback_data="adm_backuptoggle"
        )
    )
    builder.row(InlineKeyboardButton(text="⏱ Периодичность", callback_data="adm_backupinterval"))
    builder.row(InlineKeyboardButton(text="📤 Сделать бэкап сейчас", callback_data="adm_backupnow"))
    return builder.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_form"))
    return builder.as_markup()
