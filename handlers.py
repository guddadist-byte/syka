"""CRM routers: main menu, chat replies, templates, admin panel.

Router registration order in main.py is part of the FSM State Guard (see
the multi-layer defense described inline below) — commands_router and
menu_router must be registered before any router with a free-text
handler, so a stray "/start" or a tap on a known menu button is always
intercepted before it can be mistaken for a reply to an Avito client.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    ForceReply,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import ai_client
import avito_client
import bot_cache
import config
import constants
import database
import guardrail
import keyboards
import utils
from filters import ApprovedUser, RoleAtLeast, SafeFreeText
from states import AdminStates, ReplyStates, RegistrationStates

logger = logging.getLogger(__name__)

commands_router = Router(name="commands")
menu_router = Router(name="menu")
registration_router = Router(name="registration")
crm_router = Router(name="crm")
template_router = Router(name="template")
admin_router = Router(name="admin")

crm_router.message.filter(ApprovedUser())
crm_router.callback_query.filter(ApprovedUser())
template_router.message.filter(ApprovedUser())
template_router.callback_query.filter(ApprovedUser())
admin_router.message.filter(RoleAtLeast(constants.ADMIN))
admin_router.callback_query.filter(RoleAtLeast(constants.ADMIN))


# --- shared helpers ----------------------------------------------------------


async def _show_main_menu(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    if user is None or user.status != constants.STATUS_APPROVED:
        return
    await message.answer("🏠 Главное меню", reply_markup=keyboards.main_menu_kb(bool(user.on_shift), user.role))


async def _point_ids_for_user(user) -> set[int]:
    # Strictly by subscription for every role, including Director — same
    # rule as push notifications (see tasks.py._notify_subscribers): no
    # subscription to a point means it doesn't show up here either. Use
    # "📍 Мои точки" to subscribe; there is no more automatic "see everything".
    if user.role == constants.MANAGER and user.responsible_point_id:
        return {user.responsible_point_id}
    points = await database.get_user_points(user.telegram_id)
    return {p.id for p in points}


async def _render_chat_detail(target: Message, chat: bot_cache.CachedChat, state: FSMContext, actor_id: int) -> None:
    client_name = html.escape(chat.client_name or "клиентом")
    lines = [f"💬 Диалог с {client_name}"]
    if chat.item_title and chat.item_url:
        lines.append(f'📦 <a href="{html.escape(chat.item_url)}">{html.escape(chat.item_title)}</a>')
    elif chat.item_title:
        lines.append(f"📦 {html.escape(chat.item_title)}")
    lines.append("")
    for m in list(chat.messages)[-30:]:
        speaker = f"👤 <b>{client_name}</b>" if m.direction == "in" else "🧑‍💼 <b>Я</b>"
        text = html.escape(m.text) if m.text else "(фото)"
        lines.append(f"{speaker}: {text}")
    if not chat.messages:
        lines.append("(сообщений пока нет)")

    actor = await database.get_user(actor_id)
    can_reassign = bool(actor and constants.ROLE_ORDER.get(actor.role, 0) >= constants.ROLE_ORDER[constants.ADMIN])

    await target.answer("\n".join(lines), reply_markup=keyboards.chat_detail_kb(chat.short_id, can_reassign))
    prompt = await target.answer(
        f"✍️ Печатаете ответ клиенту «{client_name}»",
        reply_markup=ForceReply(input_field_placeholder=f"Ответ: {(chat.client_name or '')[:40]}"),
    )
    await state.set_state(ReplyStates.waiting_for_text)
    await state.update_data(
        chat_short_id=chat.short_id,
        client_name=chat.client_name,
        opened_at=datetime.utcnow().isoformat(),
        prompt_message_id=prompt.message_id,
    )


async def _safe_mark_chat_read(client: "avito_client.AvitoClient", chat_id: str) -> None:
    # unread_count is synced from Avito's own count on every poll (see
    # tasks.py), so any local zeroing (mark-read, a sent reply) has to be
    # mirrored to Avito too, or the next poll overwrites it right back.
    try:
        await client.mark_chat_read(chat_id)
    except avito_client.AvitoAPIError:
        pass


async def _restart_for_proxy_change(message: Message) -> None:
    await message.answer("⚠️ Настройки прокси сохранены, бот перезапускается...")
    raise SystemExit(0)


# --- commands (registered before everything else — State Guard layer 1) ----


@commands_router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    telegram_id = message.from_user.id
    user = await database.get_user(telegram_id)

    if user is None:
        await _start_registration(message, state)
        return

    if user.status == constants.STATUS_BLOCKED:
        await message.answer("Ваш доступ отозван.")
        return

    if user.status == constants.STATUS_PENDING:
        cooldown = await database.seconds_since_last_start(telegram_id)
        if cooldown is not None and cooldown < constants.START_COOLDOWN_SECONDS:
            await message.answer("⏳ Ваша заявка уже обрабатывается, ожидайте решения администратора.")
            return
        await database.touch_last_start(telegram_id)
        await message.answer("⏳ Ваша заявка на рассмотрении. Ожидайте решения администратора.")
        return

    await database.create_or_update_user(
        telegram_id, message.from_user.username, message.from_user.full_name, message.from_user.last_name
    )
    await _show_main_menu(message)


@commands_router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.")
    await _show_main_menu(message)


# --- registration / paid access ---------------------------------------------


async def _start_registration(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    cooldown = await database.seconds_since_last_start(telegram_id)
    if cooldown is not None and cooldown < constants.START_COOLDOWN_SECONDS:
        await message.answer("⏳ Ваша заявка уже обрабатывается, ожидайте решения администратора.")
        return

    await database.create_or_update_user(
        telegram_id, message.from_user.username, message.from_user.full_name, message.from_user.last_name
    )
    await database.touch_last_start(telegram_id)

    payment_cfg = await database.get_payment_config()
    if payment_cfg.is_enabled and not await database.has_paid(telegram_id):
        await state.set_state(RegistrationStates.waiting_for_payment)
        await message.answer_invoice(
            title="Доступ к CRM-боту",
            description="Оплата за рассмотрение заявки на доступ",
            payload=f"access_request:{telegram_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Доступ к боту", amount=payment_cfg.amount_stars)],
        )
        return

    await state.set_state(RegistrationStates.waiting_for_full_name)
    await message.answer("Добро пожаловать! Как к вам обращаться? Введите ФИО для заявки на доступ.")


@registration_router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@registration_router.message(F.successful_payment, StateFilter("*"))
async def process_successful_payment(message: Message, state: FSMContext) -> None:
    payment = message.successful_payment
    await database.log_payment(message.from_user.id, payment.telegram_payment_charge_id, payment.total_amount)
    await state.set_state(RegistrationStates.waiting_for_full_name)
    await message.answer("Оплата получена. Как к вам обращаться? Введите ФИО для заявки на доступ.")


async def _notify_admins_new_request(bot, telegram_id: int, full_name: str, trade_point_name: str | None = None) -> None:
    payment = await database.get_payment_for_user(telegram_id)
    kb = keyboards.access_decision_kb(telegram_id, has_unrefunded_payment=payment is not None)
    tt_line = f"\nТТ: {trade_point_name}" if trade_point_name else ""
    text = f"🆕 Заявка на доступ\n\n{full_name} (id {telegram_id}){tt_line}"
    for admin in await database.list_admins_and_directors():
        try:
            await bot.send_message(admin.telegram_id, text, reply_markup=kb)
        except TelegramForbiddenError:
            await database.mark_user_unreachable(admin.telegram_id)
        except Exception:
            # Previously any other failure here (bad markup, a transient
            # API error, ...) silently broke the whole notify loop with no
            # trace — admins would just never find out a request existed.
            # Logged and skipped now; the request is also always visible
            # via "📋 Заявки на вступление" regardless of delivery.
            logger.exception("_notify_admins_new_request: failed to notify %s", admin.telegram_id)


@registration_router.message(RegistrationStates.waiting_for_full_name, SafeFreeText())
async def process_full_name(message: Message, state: FSMContext) -> None:
    await state.update_data(full_name=message.text.strip())
    await state.set_state(RegistrationStates.waiting_for_trade_point)
    await message.answer("Укажите название торговой точки, на которой вы работаете:")


@registration_router.message(RegistrationStates.waiting_for_trade_point, SafeFreeText())
async def process_trade_point(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    data = await state.get_data()
    full_name = (data.get("full_name") or "").strip()
    trade_point_name = message.text.strip()
    await database.create_or_update_user(telegram_id, message.from_user.username, full_name, message.from_user.last_name)
    await database.update_user_trade_point(telegram_id, trade_point_name)
    await database.log_access_request(telegram_id, "requested", None, note=full_name)
    await state.clear()
    await message.answer("Заявка отправлена, ожидайте решения администратора.")
    await _notify_admins_new_request(message.bot, telegram_id, full_name, trade_point_name)


# --- main menu (State Guard layer 1 continued: StateFilter("*")) -----------


@menu_router.message(F.text == constants.BTN_HOME, StateFilter("*"))
async def go_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _show_main_menu(message)


@menu_router.message(F.text.in_({constants.BTN_SHIFT_ON, constants.BTN_SHIFT_OFF}), StateFilter("*"), ApprovedUser())
async def toggle_shift(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    if user is None:
        return
    # The button shows the current status (see main_menu_kb), so a tap means
    # "flip it": showing "Отдыхаю" means the user is on shift right now.
    new_state = message.text == constants.BTN_SHIFT_OFF
    await database.set_shift(message.from_user.id, new_state)
    await message.answer(
        "💼 Вы на смене" if new_state else "🛌 Вы отдыхаете",
        reply_markup=keyboards.main_menu_kb(new_state, user.role),
    )


@menu_router.message(F.text == constants.BTN_UNREAD, StateFilter("*"), ApprovedUser())
async def show_unread(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    point_ids = await _point_ids_for_user(user)
    chats = await bot_cache.get_unread_for_points(point_ids)
    if not chats:
        await message.answer("📩 Непрочитанных чатов нет.")
        return
    await message.answer("📩 Непрочитанные:", reply_markup=keyboards.chat_list_kb(chats))


@menu_router.message(F.text == constants.BTN_RECENT, StateFilter("*"), ApprovedUser())
async def show_recent(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    point_ids = await _point_ids_for_user(user)
    chats = await bot_cache.get_recent_replies_for_points(
        point_ids, timedelta(minutes=constants.RECENT_REPLIES_WINDOW_MINUTES)
    )
    if not chats:
        await message.answer("🕒 Недавно отвеченных чатов нет.")
        return
    await message.answer("🕒 Недавние (60 мин):", reply_markup=keyboards.chat_list_kb(chats))


@menu_router.message(F.text == constants.BTN_PROFILE, StateFilter("*"), ApprovedUser())
async def show_profile(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    lines = [
        f"👤 {user.full_name or user.username or user.telegram_id}",
        f"Роль: {constants.ROLE_LABELS[user.role]}",
    ]
    if user.trade_point_name:
        lines.append(f"Торговая точка: {user.trade_point_name}")
    if user.role == constants.MANAGER and user.responsible_point_id:
        point = await database.get_point(user.responsible_point_id)
        if point:
            lines.append(f"Точка: {point.name}")
    else:
        points = await database.get_user_points(user.telegram_id)
        if points:
            lines.append("Точки: " + ", ".join(p.name for p in points))
    lines.append(f"Смена: {'💼 На смене' if user.on_shift else '🛌 Отдыхаю'}")
    lines.append(f"Зарегистрирован: {utils.format_msk(user.created_at)}")
    lines.append(f"⭐ Рейтинг: {user.rating_points}")
    await message.answer("\n".join(lines), reply_markup=keyboards.profile_kb())


@menu_router.message(F.text == constants.BTN_MY_POINTS, StateFilter("*"), ApprovedUser())
async def show_my_points(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    if user is None:
        return
    all_points = await database.list_points()
    current = {p.id for p in await database.get_user_points(user.telegram_id)}
    kb = keyboards.point_multiselect_kb(all_points, current, "mysub", key=str(user.telegram_id))
    await message.answer(
        "📍 Выберите точки — по ним вы будете получать уведомления о новых сообщениях от клиентов. "
        "Без подписки уведомления не приходят.",
        reply_markup=kb,
    )


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_POINT}_mysub"))
async def cb_my_point_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    parts = payload.split(":")
    mode = parts[0]

    # user_id is always the caller, never trusted from the callback payload —
    # this handler must only ever be able to change the caller's own
    # subscriptions (unlike admin_router's "sub" mode, which acts on behalf
    # of an arbitrary target user and is gated by RoleAtLeast(admin)).
    user_id = callback.from_user.id

    if mode == "mysubdone":
        await callback.message.answer("Готово.")
        return

    all_points = await database.list_points()

    if mode == "mysuball":
        already_all = {p.id for p in await database.get_user_points(user_id)} == {p.id for p in all_points}
        if already_all:
            for point in all_points:
                await database.unsubscribe_user_from_point(user_id, point.id)
            current = set()
        else:
            for point in all_points:
                await database.subscribe_user_to_point(user_id, point.id)
            current = {p.id for p in all_points}
    else:
        point_id = int(parts[2])
        current = {p.id for p in await database.get_user_points(user_id)}
        if point_id in current:
            await database.unsubscribe_user_from_point(user_id, point_id)
            current.discard(point_id)
        else:
            await database.subscribe_user_to_point(user_id, point_id)
            current.add(point_id)

    await callback.message.edit_reply_markup(
        reply_markup=keyboards.point_multiselect_kb(all_points, current, "mysub", key=str(user_id))
    )


@menu_router.message(F.text == constants.BTN_MY_TEMPLATES, StateFilter("*"), RoleAtLeast(constants.MANAGER))
async def show_my_templates(message: Message) -> None:
    user = await database.get_user(message.from_user.id)
    if not user or not user.responsible_point_id:
        await message.answer("У вас пока не назначена ответственная точка.")
        return
    templates = await database.list_templates(user.responsible_point_id)
    text = "📋 Ваши шаблоны (нажмите, чтобы посмотреть/удалить):" if templates else "📋 Шаблонов пока нет."
    await message.answer(text, reply_markup=keyboards.template_manage_kb(templates))


@template_router.callback_query(F.data.startswith("tplmanage_"), RoleAtLeast(constants.MANAGER))
async def cb_template_manage_view(callback: CallbackQuery) -> None:
    await callback.answer()
    template_id = int(callback.data.rsplit("_", 1)[1])
    template = await database.get_template(template_id)
    if template is None:
        await callback.message.answer("Шаблон не найден.")
        return
    kind_label = "🧠 AI-промпт" if template.kind == constants.TEMPLATE_AI_PROMPT else "📝 Текст"
    await callback.message.answer(
        f"{kind_label} «{template.title}»:\n\n{template.body}",
        reply_markup=keyboards.template_detail_kb(template.id),
    )


@template_router.callback_query(F.data.startswith("tpldel_"), RoleAtLeast(constants.MANAGER))
async def cb_template_delete(callback: CallbackQuery) -> None:
    await callback.answer()
    template_id = int(callback.data.rsplit("_", 1)[1])
    await database.deactivate_template(template_id)
    await callback.message.edit_text("🗑 Шаблон удалён.")


@menu_router.message(F.text == constants.BTN_ADMIN_PANEL, StateFilter("*"), RoleAtLeast(constants.ADMIN))
async def show_admin_panel(message: Message) -> None:
    await message.answer("⚙️ Настройки", reply_markup=keyboards.admin_panel_kb())


@menu_router.message(F.text == constants.BTN_LEADERSHIP, StateFilter("*"), RoleAtLeast(constants.ADMIN))
async def show_leadership_menu(message: Message) -> None:
    await message.answer("👔 Меню руководителя", reply_markup=keyboards.leadership_menu_kb())


# --- CRM: chat detail, reply, delete, reassign, profile rating -------------


@crm_router.callback_query(F.data.startswith((f"{constants.PREFIX_CHAT}_", f"{constants.PREFIX_REPLY}_")))
async def open_chat(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, short_id = callback.data.split("_", 1)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        await callback.message.answer("Чат не найден или устарел.")
        return
    await _render_chat_detail(callback.message, chat, state, callback.from_user.id)


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_READ}_"))
async def cb_mark_read(callback: CallbackQuery) -> None:
    await callback.answer()
    _, short_id = callback.data.split("_", 1)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is not None:
        await bot_cache.mark_read(chat.chat_id)
        await database.set_chat_unread_count(chat.chat_id, 0)
        client = avito_client.get_pool().get(chat.avito_account_id)
        if client is not None:
            await _safe_mark_chat_read(client, chat.chat_id)
    await callback.message.answer("✅ Отмечено как прочитанное.")


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_REFRESH}_"))
async def cb_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, short_id = callback.data.split("_", 1)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is not None:
        try:
            for m in await client.get_messages(chat.chat_id):
                created_at = utils.parse_utc(m.created_at) if m.created_at else datetime.utcnow()
                await bot_cache.add_message(
                    chat.chat_id,
                    bot_cache.CachedMessage(
                        avito_message_id=m.message_id, direction=m.direction, text=m.text,
                        has_image=m.has_image, created_at=created_at,
                    ),
                )
        except avito_client.AvitoAPIError:
            pass
    await _render_chat_detail(callback.message, chat, state, callback.from_user.id)


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_BACKMENU}_"))
async def cb_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    user = await database.get_user(callback.from_user.id)
    if user is None:
        return
    await callback.message.answer(
        "🏠 Главное меню", reply_markup=keyboards.main_menu_kb(bool(user.on_shift), user.role)
    )


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_DELMSG}_"))
async def cb_delete_message(callback: CallbackQuery) -> None:
    _, msg_ref = callback.data.split("_", 1)
    resolved = await bot_cache.resolve_sent_message(msg_ref)
    if resolved is None:
        await callback.answer("Сообщение не найдено.", show_alert=True)
        return
    chat_id, avito_message_id = resolved
    chat = await bot_cache.get_chat(chat_id)
    if chat is None:
        await callback.answer("Чат не найден.", show_alert=True)
        return
    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        await callback.answer("Аккаунт недоступен.", show_alert=True)
        return
    try:
        await client.delete_message(chat_id, avito_message_id)
    except avito_client.AvitoAPIError:
        await callback.answer("Avito не позволяет удалить это сообщение", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("🗑 Сообщение удалено")


@crm_router.callback_query(F.data.startswith(f"{constants.PREFIX_REASSIGN}_"), RoleAtLeast(constants.ADMIN))
async def cb_reassign_start(callback: CallbackQuery) -> None:
    await callback.answer()
    _, short_id = callback.data.split("_", 1)
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None:
        return
    points = await database.list_points()
    kb = keyboards.point_multiselect_kb(points, selected=set(), mode="reassign", key=short_id)
    await callback.message.answer("Выберите точку для этого чата:", reply_markup=kb)


@crm_router.callback_query(F.data == f"{constants.PREFIX_RATING}_all")
async def cb_rating_all(callback: CallbackQuery) -> None:
    await callback.answer()
    leaders = await database.get_leaderboard(limit=10)
    lines = ["📊 Общий рейтинг:", ""]
    for i, (user, points) in enumerate(leaders, start=1):
        name = user.full_name or user.username or str(user.telegram_id)
        lines.append(f"{i}. {name} — {points}")
    rank = await database.get_leaderboard_rank(callback.from_user.id)
    if rank and rank > len(leaders):
        lines.append("")
        lines.append(f"Ваше место: {rank}")
    await callback.message.answer("\n".join(lines))


@crm_router.callback_query(F.data == "cancel_form")
async def cb_cancel_form(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await callback.message.answer("Отменено.")


# --- CRM: sending a reply (text / photo, incl. media-group debounce) -------


@crm_router.message(ReplyStates.waiting_for_text, SafeFreeText(), F.text)
async def receive_reply_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    opened_at = data.get("opened_at")
    if opened_at and datetime.utcnow() - datetime.fromisoformat(opened_at) > timedelta(
        minutes=constants.REPLY_STATE_TTL_MINUTES
    ):
        await state.clear()
        await message.answer("⏱ Время ожидания истекло, откройте чат заново.")
        return

    short_id = data.get("chat_short_id")
    chat = await bot_cache.resolve_chat(short_id) if short_id else None
    if chat is None:
        await state.clear()
        await message.answer("Чат больше не доступен.")
        return

    if not await bot_cache.try_claim_action(f"send:{chat.chat_id}"):
        await message.answer("⏳ Уже отправляется…")
        return

    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        await message.answer("⚠️ Аккаунт Avito недоступен, попробуйте позже.")
        return

    try:
        sent = await client.send_text_message(chat.chat_id, message.text)
    except avito_client.AvitoAPIError:
        await message.answer("⚠️ Не удалось отправить сообщение в Avito, попробуйте ещё раз.")
        return

    now = datetime.utcnow()
    await bot_cache.add_message(
        chat.chat_id,
        bot_cache.CachedMessage(
            avito_message_id=sent.message_id, direction="out", text=message.text, has_image=False, created_at=now
        ),
    )
    await bot_cache.mark_replied(chat.chat_id, message.from_user.id)
    await database.append_message(
        chat.chat_id, "out", message.text, False,
        sent_at=now.strftime("%Y-%m-%d %H:%M:%S"), avito_message_id=sent.message_id,
    )
    await database.mark_chat_replied(chat.chat_id, message.from_user.id)
    await database.increment_rating(message.from_user.id)
    await _safe_mark_chat_read(client, chat.chat_id)

    msg_ref = await bot_cache.register_sent_message(chat.chat_id, sent.message_id or "")
    await state.clear()
    await message.answer("✅ Отправлено", reply_markup=keyboards.sent_message_kb(msg_ref))
    await _show_main_menu(message)


_media_group_buffer: dict[str, list[Message]] = {}
_media_group_tasks: dict[str, asyncio.Task] = {}


@crm_router.message(ReplyStates.waiting_for_text, F.photo)
async def receive_reply_photo(message: Message, state: FSMContext) -> None:
    if message.media_group_id:
        _media_group_buffer.setdefault(message.media_group_id, []).append(message)
        if message.media_group_id not in _media_group_tasks:
            data = await state.get_data()
            _media_group_tasks[message.media_group_id] = asyncio.create_task(
                _flush_media_group(message.media_group_id, state, dict(data))
            )
        return
    await _send_photos(message, state, [message])


async def _flush_media_group(media_group_id: str, state: FSMContext, state_data: dict) -> None:
    await asyncio.sleep(constants.MEDIA_GROUP_DEBOUNCE_SECONDS)
    messages = _media_group_buffer.pop(media_group_id, [])
    _media_group_tasks.pop(media_group_id, None)
    if not messages:
        return
    await _send_photos(messages[0], state, messages, state_data_override=state_data)


async def _send_photos(anchor: Message, state: FSMContext, messages: list[Message],
                        state_data_override: dict | None = None) -> None:
    data = state_data_override if state_data_override is not None else await state.get_data()
    short_id = data.get("chat_short_id")
    chat = await bot_cache.resolve_chat(short_id) if short_id else None
    if chat is None:
        return

    if not await bot_cache.try_claim_action(f"send:{chat.chat_id}"):
        await anchor.answer("⏳ Уже отправляется…")
        return

    client = avito_client.get_pool().get(chat.avito_account_id)
    if client is None:
        await anchor.answer("⚠️ Аккаунт Avito недоступен, попробуйте позже.")
        return

    sent_count = 0
    last_avito_message_id = ""
    for msg in messages:
        photo = msg.photo[-1]
        file = await msg.bot.get_file(photo.file_id)
        buffer = await msg.bot.download_file(file.file_path)
        try:
            image_id = await client.upload_image(buffer.read(), filename=f"{photo.file_unique_id}.jpg")
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
        await anchor.answer("⚠️ Не удалось отправить фото в Avito.")
        return

    await bot_cache.mark_replied(chat.chat_id, anchor.from_user.id)
    await database.mark_chat_replied(chat.chat_id, anchor.from_user.id)
    await database.increment_rating(anchor.from_user.id)
    await _safe_mark_chat_read(client, chat.chat_id)
    await state.clear()

    msg_ref = await bot_cache.register_sent_message(chat.chat_id, last_avito_message_id)
    await anchor.answer(f"✅ Отправлено {sent_count} фото", reply_markup=keyboards.sent_message_kb(msg_ref))
    await _show_main_menu(anchor)


# --- templates ---------------------------------------------------------------


async def _apply_point_placeholders(text: str) -> str:
    """Substitutes !<CODE>А / !<CODE>В in template text with that point's
    address / working hours (e.g. "!ТКЧА" -> the ТКЧ point's address).

    Works for every point, not just ones with an explicit "🔤 Код" set —
    <CODE> matches either points.code (set via bulk import or manually,
    survives renames) or, as a fallback, the point's plain name (so it
    works immediately for a point literally named "ТКЧ" with no extra
    setup at all).
    """
    if "!" not in text:
        return text
    for point in await database.list_points(active_only=False):
        for key in filter(None, {point.code, point.name}):
            text = text.replace(f"!{key}А", point.address or "")
            text = text.replace(f"!{key}В", point.working_hours or "")
    return text


@template_router.callback_query(F.data.startswith(f"{constants.PREFIX_TPL}_"))
async def cb_template_action(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)

    if ":" in payload:
        template_id_str, short_id = payload.split(":", 1)
        template = await database.get_template(int(template_id_str))
        chat = await bot_cache.resolve_chat(short_id)
        if template is None or chat is None:
            return

        if template.kind == constants.TEMPLATE_TEXT:
            draft, allow_send = await _apply_point_placeholders(template.body), True
        else:
            point = await database.get_point(chat.point_id) if chat.point_id else None
            if point is None:
                await callback.message.answer("У чата не определена точка, AI-шаблон недоступен.")
                return
            prompt_override = await _apply_point_placeholders(template.body)
            try:
                draft, flagged = await guardrail.guarded_generate(list(chat.messages), point, prompt_override=prompt_override)
            except ai_client.AIClientError:
                await callback.message.answer("⚠️ Не удалось получить ответ от ИИ.")
                return
            allow_send = not flagged

        await state.update_data(chat_short_id=short_id, ai_draft=draft)
        await callback.message.answer(draft, reply_markup=keyboards.ai_draft_kb(short_id, allow_send))
        return

    short_id = payload
    chat = await bot_cache.resolve_chat(short_id)
    if chat is None or chat.point_id is None:
        await callback.message.answer("Шаблоны для этой точки не настроены.")
        return
    templates = await database.list_templates(chat.point_id)
    if not templates:
        await callback.message.answer("Шаблонов пока нет.")
        return
    await callback.message.answer("📋 Выберите шаблон:", reply_markup=keyboards.template_list_kb(templates, short_id))


@template_router.callback_query(F.data == "tplnew_text", RoleAtLeast(constants.MANAGER))
async def cb_template_new_text(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_template_title)
    await state.update_data(new_template_kind=constants.TEMPLATE_TEXT)
    await callback.message.answer("Введите заголовок шаблона:", reply_markup=keyboards.cancel_kb())


@template_router.callback_query(F.data == "tplnew_ai_prompt", RoleAtLeast(constants.MANAGER))
async def cb_template_new_ai(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_template_title)
    await state.update_data(new_template_kind=constants.TEMPLATE_AI_PROMPT)
    await callback.message.answer("Введите заголовок AI-шаблона:", reply_markup=keyboards.cancel_kb())


@template_router.message(AdminStates.waiting_for_template_title, SafeFreeText(), RoleAtLeast(constants.MANAGER))
async def template_title(message: Message, state: FSMContext) -> None:
    await state.update_data(new_template_title=message.text.strip())
    await state.set_state(AdminStates.waiting_for_template_body)
    kind = (await state.get_data()).get("new_template_kind")
    prompt = "Введите текст шаблона:" if kind == constants.TEMPLATE_TEXT else "Введите промпт для ИИ:"
    await message.answer(prompt)


@template_router.message(AdminStates.waiting_for_template_body, SafeFreeText(), RoleAtLeast(constants.MANAGER))
async def template_body(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    user = await database.get_user(message.from_user.id)
    if not user or not user.responsible_point_id:
        await state.clear()
        await message.answer("У вас не назначена точка.")
        return
    await database.create_template(
        point_id=user.responsible_point_id, kind=data.get("new_template_kind", constants.TEMPLATE_TEXT),
        title=data.get("new_template_title", "Шаблон"), body=message.text, created_by=message.from_user.id,
    )
    await state.clear()
    await message.answer("✅ Шаблон создан.")


# --- admin: leadership menu / users / access decisions ----------------------


@admin_router.callback_query(F.data == "adm_users")
async def cb_admin_users(callback: CallbackQuery) -> None:
    await callback.answer()
    users = await database.list_all_users()
    await callback.message.answer("👥 Все пользователи:", reply_markup=keyboards.user_management_kb(users))


@admin_router.callback_query(F.data == "adm_requests")
async def cb_admin_requests(callback: CallbackQuery) -> None:
    await callback.answer()
    pending = await database.list_pending_users()
    if not pending:
        await callback.message.answer("📋 Заявок на вступление нет.")
        return
    for user in pending:
        payment = await database.get_payment_for_user(user.telegram_id)
        kb = keyboards.access_decision_kb(user.telegram_id, has_unrefunded_payment=payment is not None)
        label = user.full_name or user.username or str(user.telegram_id)
        tt_line = f"\nТТ: {user.trade_point_name}" if user.trade_point_name else ""
        text = f"🆕 Заявка на доступ\n\n{label} (id {user.telegram_id}){tt_line}"
        await callback.message.answer(text, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("adm_useredit_"))
async def cb_admin_user_edit(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])
    await callback.message.answer("Что изменить?", reply_markup=keyboards.user_edit_menu_kb(target_id))


@admin_router.callback_query(F.data.startswith("adm_urole_"))
async def cb_admin_user_role_start(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])
    actor = await database.get_user(callback.from_user.id)
    allow_admin_roles = bool(actor and actor.role == constants.DIRECTOR)
    await callback.message.answer("Выберите новую роль:", reply_markup=keyboards.role_select_kb(target_id, allow_admin_roles))


@admin_router.callback_query(F.data.startswith("adm_uname_"))
async def cb_admin_user_name_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_user_fullname)
    await state.update_data(editing_user_id=target_id)
    await callback.message.answer("Введите новое ФИО:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_user_fullname, SafeFreeText())
async def admin_user_name_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_id = data.get("editing_user_id")
    if target_id:
        await database.update_user_full_name(target_id, message.text.strip())
    await state.clear()
    await message.answer("✅ ФИО обновлено.")


@admin_router.callback_query(F.data.startswith("adm_utrade_"))
async def cb_admin_user_trade_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_user_trade_point)
    await state.update_data(editing_user_id=target_id)
    await callback.message.answer("Введите название торговой точки:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_user_trade_point, SafeFreeText())
async def admin_user_trade_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    target_id = data.get("editing_user_id")
    if target_id:
        await database.update_user_trade_point(target_id, message.text.strip())
    await state.clear()
    await message.answer("✅ Торговая точка обновлена.")


@admin_router.callback_query(F.data == "adm_unblockbyid")
async def cb_admin_unblock_by_id_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_unblock_telegram_id)
    await callback.message.answer("Введите Telegram ID пользователя для разблокировки:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_unblock_telegram_id, SafeFreeText())
async def admin_unblock_by_id_finish(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("⚠️ Введите числовой Telegram ID.")
        return
    target_id = int(raw)

    target = await database.get_user(target_id)
    if target is None:
        await message.answer("⚠️ Пользователь с таким Telegram ID не найден.")
        return
    if target.status != constants.STATUS_BLOCKED:
        label = target.full_name or target.username or str(target.telegram_id)
        await message.answer(f"Пользователь {label} не заблокирован (статус: {target.status}).")
        return

    await database.set_user_status(target_id, constants.STATUS_APPROVED, message.from_user.id)
    try:
        await message.bot.send_message(target_id, "✅ Ваш доступ восстановлен.")
        await database.mark_user_reachable(target_id)
    except TelegramForbiddenError:
        await database.mark_user_unreachable(target_id)

    label = target.full_name or target.username or str(target.telegram_id)
    await message.answer(f"🔓 Пользователь {label} разблокирован.")


@admin_router.callback_query(F.data == "adm_deleteaccount")
async def cb_admin_delete_account_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_delete_telegram_id)
    await callback.message.answer(
        "Введите Telegram ID пользователя для удаления аккаунта:", reply_markup=keyboards.cancel_kb()
    )


@admin_router.message(AdminStates.waiting_for_delete_telegram_id, SafeFreeText())
async def admin_delete_account_confirm(message: Message, state: FSMContext) -> None:
    await state.clear()
    raw = message.text.strip()
    if not raw.isdigit():
        await message.answer("⚠️ Введите числовой Telegram ID.")
        return
    target_id = int(raw)

    target = await database.get_user(target_id)
    if target is None:
        await message.answer("⚠️ Пользователь с таким Telegram ID не найден.")
        return

    label = target.full_name or target.username or str(target.telegram_id)
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_delconfirm_{target_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="adm_delcancel"),
    )
    await message.answer(
        f"⚠️ Удалить аккаунт «{label}» (id {target_id})?\n"
        "Он сможет подать новую заявку через /start. Действие необратимо.",
        reply_markup=builder.as_markup(),
    )


@admin_router.callback_query(F.data == "adm_delcancel")
async def cb_admin_delete_account_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text("Отменено.")


@admin_router.callback_query(F.data.startswith("adm_delconfirm_"))
async def cb_admin_delete_account_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.rsplit("_", 1)[1])

    target = await database.get_user(target_id)
    if target is None:
        await callback.message.edit_text("⚠️ Пользователь с таким Telegram ID не найден.")
        return

    reason = await database.can_delete_user(target_id)
    if reason is not None:
        await callback.message.edit_text(f"⚠️ Нельзя удалить: {reason}.")
        return

    label = target.full_name or target.username or str(target.telegram_id)
    await database.delete_user_account(target_id)
    try:
        await callback.bot.send_message(
            target_id, "Ваша учётная запись сброшена администратором. Отправьте /start, чтобы подать новую заявку."
        )
    except TelegramForbiddenError:
        pass

    await callback.message.edit_text(f"🗑 Аккаунт «{label}» (id {target_id}) удалён.")


@admin_router.callback_query(F.data.startswith("adm_setrole_"))
async def cb_admin_set_role(callback: CallbackQuery) -> None:
    await callback.answer()
    payload = callback.data[len("adm_setrole_"):]
    role, user_id_str = payload.split(":")
    user_id = int(user_id_str)

    actor = await database.get_user(callback.from_user.id)
    if actor and actor.role == constants.ADMIN and role in (constants.ADMIN, constants.DIRECTOR):
        await callback.message.answer("⚠️ РОП не может назначать роль РОП/Админ.")
        return

    if role == constants.MANAGER:
        points = await database.list_points()
        kb = keyboards.point_multiselect_kb(points, selected=set(), mode="resp", key=str(user_id))
        await callback.message.answer("Выберите точку, за которую он будет отвечать:", reply_markup=kb)
        return

    target = await database.get_user(user_id)
    if target and target.role == constants.DIRECTOR and role != constants.DIRECTOR:
        if await database.count_approved_directors() <= 1:
            await callback.message.answer("⚠️ Нельзя оставить систему без Админа.")
            return

    await database.set_user_role(user_id, role, callback.from_user.id)
    await callback.message.answer("✅ Роль изменена.")
    try:
        await callback.bot.send_message(user_id, f"🎭 Ваша роль изменена: {constants.ROLE_LABELS[role]}")
    except TelegramForbiddenError:
        await database.mark_user_unreachable(user_id)


@admin_router.callback_query(F.data.startswith(f"{constants.PREFIX_APPR}_"))
async def cb_approve_user(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.split("_", 1)[1])
    await database.set_user_status(target_id, constants.STATUS_APPROVED, callback.from_user.id)
    welcome_text = await database.get_welcome_message()
    target_user = await database.get_user(target_id)
    try:
        await callback.bot.send_message(target_id, welcome_text)
        if target_user:
            await callback.bot.send_message(
                target_id, "🏠 Главное меню", reply_markup=keyboards.main_menu_kb(False, target_user.role)
            )
    except TelegramForbiddenError:
        await database.mark_user_unreachable(target_id)
    points = await database.list_points()
    kb = keyboards.point_multiselect_kb(points, selected=set(), mode="sub", key=str(target_id))
    await callback.message.answer("✅ Одобрено. Назначьте точки:", reply_markup=kb)


@admin_router.callback_query(F.data.startswith(f"{constants.PREFIX_BLK}_"))
async def cb_block_user(callback: CallbackQuery, fsm_storage: BaseStorage) -> None:
    await callback.answer()
    target_id = int(callback.data.split("_", 1)[1])
    target = await database.get_user(target_id)
    actor = await database.get_user(callback.from_user.id)

    if target and target.role == constants.DIRECTOR and await database.count_approved_directors() <= 1:
        await callback.message.answer("⚠️ Нельзя оставить систему без Админа.")
        return
    if actor and actor.role == constants.ADMIN and target and constants.ROLE_ORDER.get(target.role, 0) >= constants.ROLE_ORDER[constants.ADMIN]:
        await callback.message.answer("⚠️ РОП не может заблокировать РОП/Админа.")
        return

    await database.set_user_status(target_id, constants.STATUS_BLOCKED, callback.from_user.id)
    await database.set_shift(target_id, False)

    key = StorageKey(bot_id=callback.bot.id, chat_id=target_id, user_id=target_id)
    await fsm_storage.set_state(key=key, state=None)
    await fsm_storage.set_data(key=key, data={})

    try:
        await callback.bot.send_message(target_id, "Ваш доступ отозван.")
    except TelegramForbiddenError:
        await database.mark_user_unreachable(target_id)
    await callback.message.answer("🚫 Пользователь заблокирован.")


@admin_router.callback_query(F.data.startswith(f"{constants.PREFIX_BLKREFUND}_"))
async def cb_block_with_refund(callback: CallbackQuery) -> None:
    await callback.answer()
    target_id = int(callback.data.split("_", 1)[1])
    payment = await database.get_payment_for_user(target_id)
    if payment:
        try:
            await callback.bot.refund_star_payment(user_id=target_id, telegram_payment_charge_id=payment.telegram_charge_id)
            await database.mark_payment_refunded(payment.id)
        except Exception:
            logger.exception("cb_block_with_refund: refund failed for %s", target_id)
    await database.set_user_status(target_id, constants.STATUS_BLOCKED, callback.from_user.id)
    try:
        await callback.bot.send_message(target_id, "Ваша заявка отклонена, оплата возвращена.")
    except TelegramForbiddenError:
        await database.mark_user_unreachable(target_id)
    await callback.message.answer("❌ Отклонено, средства возвращены.")


# --- admin: point_ dispatcher (subscriptions / responsible point / reassign) -


@admin_router.callback_query(F.data.startswith(f"{constants.PREFIX_POINT}_"))
async def cb_point_action(callback: CallbackQuery) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    parts = payload.split(":")
    mode = parts[0]

    if mode == "subdone":
        await callback.message.answer("Готово.")
        return

    key, point_id = parts[1], int(parts[2])

    if mode == "sub":
        user_id = int(key)
        current = {p.id for p in await database.get_user_points(user_id)}
        if point_id in current:
            await database.unsubscribe_user_from_point(user_id, point_id)
            current.discard(point_id)
        else:
            await database.subscribe_user_to_point(user_id, point_id)
            current.add(point_id)
        all_points = await database.list_points()
        await callback.message.edit_reply_markup(
            reply_markup=keyboards.point_multiselect_kb(all_points, current, "sub", key)
        )
    elif mode == "resp":
        user_id = int(key)
        await database.set_user_role(user_id, constants.MANAGER, callback.from_user.id)
        await database.set_responsible_point(user_id, point_id)
        await callback.message.answer("✅ Роль «Ответственный точки» назначена.")
        try:
            await callback.bot.send_message(
                user_id, f"🎭 Ваша роль изменена: {constants.ROLE_LABELS[constants.MANAGER]}"
            )
        except TelegramForbiddenError:
            await database.mark_user_unreachable(user_id)
    elif mode == "reassign":
        chat = await bot_cache.resolve_chat(key)
        if chat and chat.item_id:
            await database.reassign_item_point(chat.item_id, point_id, callback.from_user.id)
            chat.point_id = point_id
            await callback.message.answer("✅ Точка переназначена.")


# --- admin: broadcasts --------------------------------------------------


@admin_router.callback_query(F.data == "adm_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    await callback.message.answer("Введите текст рассылки:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_broadcast_text, SafeFreeText())
async def admin_broadcast_text(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text)
    await state.set_state(AdminStates.waiting_for_broadcast_photo)
    await message.answer("Пришлите фото к рассылке или напишите «нет»:", reply_markup=keyboards.cancel_kb())


async def _preview_broadcast(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text, photo_id = data.get("broadcast_text", ""), data.get("broadcast_photo_id")
    if photo_id:
        await message.answer_photo(photo_id, caption=text, reply_markup=keyboards.broadcast_confirm_kb())
    else:
        await message.answer(text, reply_markup=keyboards.broadcast_confirm_kb())


@admin_router.message(AdminStates.waiting_for_broadcast_photo, F.photo)
async def admin_broadcast_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_photo_id=message.photo[-1].file_id)
    await _preview_broadcast(message, state)


@admin_router.message(AdminStates.waiting_for_broadcast_photo, SafeFreeText())
async def admin_broadcast_no_photo(message: Message, state: FSMContext) -> None:
    await _preview_broadcast(message, state)


@admin_router.callback_query(F.data == "adm_broadcastsend")
async def cb_admin_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    text, photo_id = data.get("broadcast_text", ""), data.get("broadcast_photo_id")
    await state.clear()

    actor = await database.get_user(callback.from_user.id)
    signature_name = (actor.last_name or actor.full_name or "Администрация") if actor else "Администрация"
    role_label = constants.ROLE_LABELS.get(actor.role, "") if actor else ""
    full_text = f"{text}\n\n— {signature_name}, {role_label}"

    sent = failed = 0
    for user in await database.list_approved_users():
        try:
            if photo_id:
                await callback.bot.send_photo(user.telegram_id, photo_id, caption=full_text)
            else:
                await callback.bot.send_message(user.telegram_id, full_text)
            sent += 1
        except TelegramForbiddenError:
            await database.mark_user_unreachable(user.telegram_id)
            failed += 1
        except Exception:
            logger.exception("broadcast: failed to send to %s", user.telegram_id)
            failed += 1

    await database.log_broadcast(callback.from_user.id, text, photo_id, sent, failed)
    await callback.message.answer(f"✅ Доставлено: {sent}, ⛔ недоступны: {failed}")


# --- admin: points -------------------------------------------------------


@admin_router.callback_query(F.data == "adm_onshift")
async def cb_admin_onshift(callback: CallbackQuery) -> None:
    await callback.answer()
    users = await database.list_on_shift_users()
    if not users:
        await callback.message.answer("🕐 Сейчас никто не на смене.")
        return
    lines = ["🕐 На смене сейчас:"]
    for user in users:
        label = user.full_name or user.username or str(user.telegram_id)
        role_label = constants.ROLE_LABELS.get(user.role, user.role)
        if user.role == constants.MANAGER and user.responsible_point_id:
            point = await database.get_point(user.responsible_point_id)
            point_label = point.name if point else "—"
        else:
            points = await database.get_user_points(user.telegram_id)
            point_label = ", ".join(p.name for p in points) or "—"
        lines.append(f"👤 {label} ({role_label}) — {point_label}")
    await callback.message.answer("\n".join(lines))


@admin_router.callback_query(F.data == "adm_pointsmenu")
async def cb_admin_points_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("🏢 Точки:", reply_markup=keyboards.admin_points_menu_kb())


@admin_router.callback_query(F.data == "adm_accountmenu")
async def cb_admin_account_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer("👤 Управление аккаунтом по ID:", reply_markup=keyboards.admin_account_menu_kb())


@admin_router.callback_query(F.data == "adm_points")
async def cb_admin_points(callback: CallbackQuery) -> None:
    await callback.answer()
    points = await database.list_points(active_only=False)
    builder = InlineKeyboardBuilder()
    for p in points:
        status = "🟢" if p.is_active else "🔴"
        builder.row(InlineKeyboardButton(text=f"{status} {p.name}", callback_data=f"adm_pointedit_{p.id}"))
    builder.row(InlineKeyboardButton(text="🗺 Синхронизировать точки с Avito", callback_data="adm_syncpoints"))
    await callback.message.answer("🏢 Подразделения:", reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "adm_pointconflicts")
async def cb_admin_point_conflicts(callback: CallbackQuery) -> None:
    await callback.answer("Проверяю…")
    points = await database.list_points(active_only=False)
    all_coords: list[tuple[int, str, float, float]] = []
    for p in points:
        for c in await database.list_point_coordinates(p.id):
            all_coords.append((p.id, p.name, c.lat, c.lon))

    # Minimum distance per distinct point pair — two points can each carry
    # many coordinates (one per ad ever matched to them), so the same pair
    # of points can show up via several coordinate combinations; only the
    # closest one is actually informative.
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

    if not conflict_map:
        await callback.message.answer(
            f"🔍 Точек ближе {constants.POINT_CONFLICT_WARNING_M:.0f} м друг к другу не найдено."
        )
        return

    conflicts = sorted(conflict_map.values(), key=lambda c: c[2])
    lines = [f"🔍 Точки на расстоянии до {constants.POINT_CONFLICT_WARNING_M:.0f} м друг от друга:", ""]
    for name1, name2, dist in conflicts:
        lines.append(f"⚠️ «{name1}» ↔ «{name2}»: {dist:.0f} м")
    lines.append(
        "\nЕсли это реально разные адреса — чаты между ними могут путаться "
        "(допуск автосвязки — {:.0f} м). Разбирайте вручную через «🔀 Переназначить точку» "
        "в конкретном чате.".format(constants.COORD_MAX_DISTANCE_M)
    )
    await callback.message.answer("\n".join(lines))


@admin_router.callback_query(F.data.startswith("adm_pointedit_"))
async def cb_admin_point_edit(callback: CallbackQuery) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    point = await database.get_point(point_id)
    if point is None:
        return
    coords = await database.list_point_coordinates(point_id)
    lines = [
        f"🏢 {point.name}", f"Код: {point.code or '—'}",
        f"Адрес: {point.address or '—'}", f"Часы: {point.working_hours or '—'}",
    ]
    if coords:
        # Many raw rows can share nearly the same spot (repeated syncs of
        # the same ad) — collapse to distinct clusters (~15m) so the admin
        # sees actual identifiable locations, not a meaningless count.
        unique = []
        for c in coords:
            if not any(utils.haversine_distance_m(c.lat, c.lon, u.lat, u.lon) <= 15.0 for u in unique):
                unique.append(c)
            if len(unique) >= 5:
                break
        lines.append(f"\nКоординаты ({len(coords)} меток, {len(unique)} уникальных мест):")
        for c in unique:
            maps_url = f"https://maps.google.com/?q={c.lat:.6f},{c.lon:.6f}"
            lines.append(f'📍 <a href="{maps_url}">{c.lat:.6f}, {c.lon:.6f}</a>')
    else:
        lines.append("Координат: нет")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"adm_pointrename_{point_id}"))
    builder.row(InlineKeyboardButton(text="🔤 Изменить код", callback_data=f"adm_pointcode_{point_id}"))
    builder.row(InlineKeyboardButton(text="📍 Изменить адрес", callback_data=f"adm_pointaddr_{point_id}"))
    builder.row(InlineKeyboardButton(text="🕒 Изменить часы работы", callback_data=f"adm_pointhours_{point_id}"))
    builder.row(InlineKeyboardButton(
        text="🟢 Активировать" if not point.is_active else "🔴 Удалить (скрыть)",
        callback_data=f"adm_pointtoggle_{point_id}",
    ))
    await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())


@admin_router.callback_query(F.data.startswith("adm_pointtoggle_"))
async def cb_admin_point_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    point = await database.get_point(point_id)
    if point is None:
        return
    if point.is_active:
        await database.soft_delete_point(point_id)
    else:
        await database.reactivate_point(point_id)
    await callback.message.answer("Готово.")


@admin_router.callback_query(F.data.startswith("adm_pointrename_"))
async def cb_admin_point_rename_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_point_name)
    await state.update_data(editing_point_id=point_id)
    await callback.message.answer("Введите новое название точки:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_point_name, SafeFreeText())
async def admin_point_rename_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    point_id = data.get("editing_point_id")
    if point_id:
        await database.rename_point(point_id, message.text.strip())
    await state.clear()
    await message.answer("✅ Точка переименована.")


@admin_router.callback_query(F.data.startswith("adm_pointaddr_"))
async def cb_admin_point_address_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_point_address)
    await state.update_data(editing_point_id=point_id)
    await callback.message.answer("Введите новый адрес точки:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_point_address, SafeFreeText())
async def admin_point_address_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    point_id = data.get("editing_point_id")
    if point_id:
        await database.update_point_details(point_id, address=message.text.strip())
    await state.clear()
    await message.answer("✅ Адрес обновлён.")


@admin_router.callback_query(F.data.startswith("adm_pointhours_"))
async def cb_admin_point_hours_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_point_hours)
    await state.update_data(editing_point_id=point_id)
    await callback.message.answer("Введите часы работы точки (например, 10:00–20:00):", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_point_hours, SafeFreeText())
async def admin_point_hours_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    point_id = data.get("editing_point_id")
    if point_id:
        await database.update_point_details(point_id, working_hours=message.text.strip())
    await state.clear()
    await message.answer("✅ Часы работы обновлены.")


@admin_router.callback_query(F.data.startswith("adm_pointcode_"))
async def cb_admin_point_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    point_id = int(callback.data.rsplit("_", 1)[1])
    await state.set_state(AdminStates.waiting_for_point_code)
    await state.update_data(editing_point_id=point_id)
    await callback.message.answer(
        "Введите короткий код точки (например, ТКЧ) — используется в массовом "
        "заполнении и в шаблонах (!ТКЧА, !ТКЧВ):",
        reply_markup=keyboards.cancel_kb(),
    )


@admin_router.message(AdminStates.waiting_for_point_code, SafeFreeText())
async def admin_point_code_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    point_id = data.get("editing_point_id")
    if point_id:
        await database.set_point_code(point_id, message.text.strip().upper())
    await state.clear()
    await message.answer("✅ Код точки обновлён.")


@admin_router.callback_query(F.data == "adm_bulkpoints")
async def cb_admin_bulk_points_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_bulk_points_import)
    await callback.message.answer(
        "Пришлите список точек, по одной на строку, в формате:\n"
        "<code>КОД Город ул. Адрес, дом Часы_работы</code>\n\n"
        "Например:\n"
        "<code>ТКЧ Ростов-на-Дону ул. Текучева 141а 8:00-20:00</code>\n\n"
        "Код должен быть первым словом, часы работы (или «Круглосуточно») — последним. "
        "Совпадение точки ищется по коду, а если код ещё не задан — по названию точки.",
        reply_markup=keyboards.cancel_kb(),
    )


@admin_router.message(AdminStates.waiting_for_bulk_points_import, SafeFreeText())
async def admin_bulk_points_finish(message: Message, state: FSMContext) -> None:
    await state.clear()
    all_points = await database.list_points(active_only=False)

    updated: list[str] = []
    not_found: list[str] = []
    for raw_line in message.text.splitlines():
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
            # Fall back to matching by name — first run before any point has
            # a code assigned yet (e.g. points named exactly "ТКЧ" already).
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

    lines = [f"✅ Обновлено: {len(updated)}"]
    lines += [f"  {line}" for line in updated]
    if not_found:
        lines.append(f"\n⚠️ Не разобрано/не найдено: {len(not_found)}")
        lines += [f"  {line}" for line in not_found]
    await message.answer("\n".join(lines))


@admin_router.callback_query(F.data == "adm_syncpoints")
async def cb_admin_sync_points(callback: CallbackQuery) -> None:
    await callback.answer("Синк запущен…")
    accounts = await database.list_avito_accounts(active_only=True)
    seen_coords = 0
    report_lines: list[str] = []
    for account in accounts:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            report_lines.append(f"⚠️ {account.name}: клиент недоступен (перезапустите бота после добавления аккаунта)")
            continue

        offset = 0
        account_chats = 0
        account_coords = 0
        error_text: str | None = None
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
        report_lines.append(line)

    points = await database.list_points()
    await callback.message.answer(
        "🗺 Синк завершён:\n" + "\n".join(report_lines) +
        f"\n\nВсего координат обработано: {seen_coords}, точек в базе сейчас: {len(points)}.\n"
        f"Проверьте список в «🏢 Настройки подразделений» — названия можно переименовать, "
        f"это уже не перезапишется следующим синком."
    )


@admin_router.callback_query(F.data == "adm_unassigned")
async def cb_admin_unassigned(callback: CallbackQuery) -> None:
    await callback.answer()
    chats = await database.list_chats_without_point()
    if not chats:
        await callback.message.answer("📭 Все чаты привязаны к точкам.")
        return
    await callback.message.answer("📭 Чаты без точки:", reply_markup=keyboards.unassigned_chats_kb(chats))


# --- admin: Avito accounts ------------------------------------------------


@admin_router.callback_query(F.data == "adm_avito")
async def cb_admin_avito(callback: CallbackQuery) -> None:
    await callback.answer()
    accounts = await database.list_avito_accounts(active_only=False)
    builder = InlineKeyboardBuilder()
    for acc in accounts:
        status = "🟢" if acc.is_active else "🔴"
        builder.row(InlineKeyboardButton(text=f"{status} {acc.name}", callback_data=f"adm_avitoedit_{acc.id}"))
    builder.row(InlineKeyboardButton(text="➕ Добавить", callback_data="adm_avitoadd"))
    await callback.message.answer("🔑 Avito-аккаунты:", reply_markup=builder.as_markup())


@admin_router.callback_query(F.data.startswith("adm_avitoedit_"))
async def cb_admin_avito_edit(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.rsplit("_", 1)[1])
    account = await database.get_avito_account(account_id)
    if account is None:
        return
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="🔴 Выключить" if account.is_active else "🟢 Включить", callback_data=f"adm_avitotoggle_{account_id}"
    ))
    text = f"🔑 {account.name}\nclient_id: {account.client_id}\nтокен: {'есть' if account.access_token else 'нет'}"
    if account.last_poll_error:
        text += f"\n⚠️ {account.last_poll_error}"
    await callback.message.answer(text, reply_markup=builder.as_markup())


@admin_router.callback_query(F.data.startswith("adm_avitotoggle_"))
async def cb_admin_avito_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.rsplit("_", 1)[1])
    account = await database.get_avito_account(account_id)
    if account is None:
        return
    await database.set_avito_account_active(account_id, not account.is_active)
    await avito_client.reload_accounts()
    await callback.message.answer("Готово.")


@admin_router.callback_query(F.data == "adm_avitoadd")
async def cb_admin_avito_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_avito_name)
    await callback.message.answer("Введите название аккаунта (для себя):", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_avito_name, SafeFreeText())
async def admin_avito_name(message: Message, state: FSMContext) -> None:
    await state.update_data(avito_name=message.text.strip())
    await state.set_state(AdminStates.waiting_for_avito_client_id)
    await message.answer("Введите client_id (из кабинета Avito, раздел «Интеграции и API»):")


@admin_router.message(AdminStates.waiting_for_avito_client_id, SafeFreeText())
async def admin_avito_client_id(message: Message, state: FSMContext) -> None:
    await state.update_data(avito_client_id=message.text.strip())
    await state.set_state(AdminStates.waiting_for_avito_client_secret)
    await message.answer("Введите client_secret:")


@admin_router.message(AdminStates.waiting_for_avito_client_secret, SafeFreeText())
async def admin_avito_client_secret(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    client_id = data["avito_client_id"]
    client_secret = message.text.strip()

    await message.answer("Проверяю учётные данные в Avito…")
    try:
        info = await avito_client.fetch_account_info(client_id, client_secret, avito_client.get_session())
    except avito_client.AvitoAPIError as exc:
        await message.answer(f"⚠️ Avito отклонил client_id/client_secret: {exc}\nПопробуйте ещё раз или /cancel.")
        return

    avito_user_id = info.get("id")
    if not avito_user_id:
        await message.answer("⚠️ Avito не вернул id аккаунта (ответ без поля «id»). Проверьте данные и попробуйте снова.")
        return

    account = await database.create_avito_account(
        name=data["avito_name"], avito_user_id=avito_user_id, client_id=client_id, client_secret=client_secret,
    )
    await avito_client.reload_accounts()
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(f"✅ Аккаунт «{account.name}» добавлен, avito_user_id={avito_user_id} (получен автоматически).")


# --- admin: AI settings ----------------------------------------------------


@admin_router.callback_query(F.data == "adm_ai")
async def cb_admin_ai(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_ai_config()
    lines = [
        "🧠 Настройки ИИ", f"base_url: {cfg.base_url}", f"model: {cfg.model}",
        f"api_key: {'установлен' if cfg.api_key else 'не задан'}",
        f"заголовок: {cfg.extra_header_name} = {cfg.extra_header_value}",
        f"включено: {'да' if cfg.is_enabled else 'нет'}",
    ]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ base_url", callback_data="adm_aiseturl"))
    builder.row(InlineKeyboardButton(text="✏️ model", callback_data="adm_aisetmodel"))
    builder.row(InlineKeyboardButton(text="✏️ api_key", callback_data="adm_aisetkey"))
    builder.row(InlineKeyboardButton(text="🔴 Выключить" if cfg.is_enabled else "🟢 Включить", callback_data="adm_aitoggle"))
    await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "adm_aitoggle")
async def cb_admin_ai_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_ai_config()
    await database.update_ai_config(actor_id=callback.from_user.id, is_enabled=0 if cfg.is_enabled else 1)
    await callback.message.answer("Готово.")


@admin_router.callback_query(F.data == "adm_aiseturl")
async def cb_admin_ai_set_url(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_ai_base_url)
    await callback.message.answer("Введите base_url:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_ai_base_url, SafeFreeText())
async def admin_ai_base_url(message: Message, state: FSMContext) -> None:
    await database.update_ai_config(actor_id=message.from_user.id, base_url=message.text.strip())
    await state.clear()
    await message.answer("✅ Сохранено.")


@admin_router.callback_query(F.data == "adm_aisetmodel")
async def cb_admin_ai_set_model(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_ai_model)
    await callback.message.answer("Введите название модели:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_ai_model, SafeFreeText())
async def admin_ai_model(message: Message, state: FSMContext) -> None:
    await database.update_ai_config(actor_id=message.from_user.id, model=message.text.strip())
    await state.clear()
    await message.answer("✅ Сохранено.")


@admin_router.callback_query(F.data == "adm_aisetkey")
async def cb_admin_ai_set_key(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_ai_api_key)
    await callback.message.answer("Введите API-ключ:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_ai_api_key, SafeFreeText())
async def admin_ai_api_key(message: Message, state: FSMContext) -> None:
    await database.update_ai_config(actor_id=message.from_user.id, api_key=message.text.strip())
    await state.clear()
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ Ключ сохранён.")


# --- admin: proxy settings (saving always restarts the process, see plan) --


@admin_router.callback_query(F.data == "adm_proxy")
async def cb_admin_proxy(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_proxy_config()
    lines = ["🌐 Прокси", f"включён: {'да' if cfg.is_enabled else 'нет'}", f"url: {cfg.proxy_url or '—'}"]
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ URL", callback_data="adm_proxyseturl"))
    builder.row(InlineKeyboardButton(text="🔴 Выключить" if cfg.is_enabled else "🟢 Включить", callback_data="adm_proxytoggle"))
    await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "adm_proxytoggle")
async def cb_admin_proxy_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_proxy_config()
    await database.update_proxy_config(actor_id=callback.from_user.id, is_enabled=0 if cfg.is_enabled else 1)
    await _restart_for_proxy_change(callback.message)


@admin_router.callback_query(F.data == "adm_proxyseturl")
async def cb_admin_proxy_set_url(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_proxy_url)
    await callback.message.answer("Введите URL прокси (http://... или socks5://...):", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_proxy_url, SafeFreeText())
async def admin_proxy_url(message: Message, state: FSMContext) -> None:
    await database.update_proxy_config(actor_id=message.from_user.id, proxy_url=message.text.strip(), is_enabled=1)
    await state.clear()
    await _restart_for_proxy_change(message)


# --- admin: paid access settings -------------------------------------------


@admin_router.callback_query(F.data == "adm_payment")
async def cb_admin_payment(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_payment_config()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔴 Выключить" if cfg.is_enabled else "🟢 Включить", callback_data="adm_paymenttoggle"))
    builder.row(InlineKeyboardButton(text="✏️ Сумма", callback_data="adm_paymentamount"))
    await callback.message.answer(
        f"⭐ Платный доступ\nВключён: {'да' if cfg.is_enabled else 'нет'}\nСумма: {cfg.amount_stars} ⭐",
        reply_markup=builder.as_markup(),
    )


@admin_router.callback_query(F.data == "adm_paymenttoggle")
async def cb_admin_payment_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_payment_config()
    await database.update_payment_config(actor_id=callback.from_user.id, is_enabled=0 if cfg.is_enabled else 1)
    await callback.message.answer("Готово.")


@admin_router.callback_query(F.data == "adm_paymentamount")
async def cb_admin_payment_amount_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_payment_amount)
    await callback.message.answer("Введите сумму в звёздах:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_payment_amount, SafeFreeText())
async def admin_payment_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    await database.update_payment_config(actor_id=message.from_user.id, amount_stars=amount)
    await state.clear()
    await message.answer("✅ Сохранено.")


# --- admin: welcome message --------------------------------------------


@admin_router.callback_query(F.data == "adm_welcome")
async def cb_admin_welcome(callback: CallbackQuery) -> None:
    await callback.answer()
    text = await database.get_welcome_message()
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✏️ Изменить", callback_data="adm_welcomeset"))
    builder.row(InlineKeyboardButton(text="👁 Предпросмотр", callback_data="adm_welcomepreview"))
    await callback.message.answer(f"✉️ Текущий текст:\n\n{text}", reply_markup=builder.as_markup())


@admin_router.callback_query(F.data == "adm_welcomepreview")
async def cb_admin_welcome_preview(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(await database.get_welcome_message())


@admin_router.callback_query(F.data == "adm_welcomeset")
async def cb_admin_welcome_set(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_welcome_text)
    await callback.message.answer("Введите новый текст приветствия:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_welcome_text, SafeFreeText())
async def admin_welcome_text(message: Message, state: FSMContext) -> None:
    await database.update_welcome_message(message.text, message.from_user.id)
    await state.clear()
    await message.answer("✅ Сохранено.")


# --- admin: backups --------------------------------------------------------


@admin_router.callback_query(F.data == "adm_backup")
async def cb_admin_backup(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_backup_config()
    last = utils.format_msk(cfg.last_backup_at) if cfg.last_backup_at else "ещё не было"
    text = f"💾 Резервные копии\nВключено: {'да' if cfg.is_enabled else 'нет'}\nПериодичность: {cfg.interval_hours} ч\nПоследняя: {last}"
    await callback.message.answer(text, reply_markup=keyboards.backup_settings_kb(bool(cfg.is_enabled)))


@admin_router.callback_query(F.data == "adm_backuptoggle")
async def cb_admin_backup_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    cfg = await database.get_backup_config()
    await database.update_backup_config(actor_id=callback.from_user.id, is_enabled=0 if cfg.is_enabled else 1)
    await callback.message.answer("Готово.")


@admin_router.callback_query(F.data == "adm_backupinterval")
async def cb_admin_backup_interval_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_backup_interval)
    await callback.message.answer("Введите периодичность в часах:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_backup_interval, SafeFreeText())
async def admin_backup_interval(message: Message, state: FSMContext) -> None:
    try:
        hours = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    await database.update_backup_config(actor_id=message.from_user.id, interval_hours=hours)
    await state.clear()
    await message.answer("✅ Сохранено.")


@admin_router.callback_query(F.data == "adm_backupnow")
async def cb_admin_backup_now(callback: CallbackQuery) -> None:
    await callback.answer("Бэкап запускается…")
    static_cfg = config.load_static_config()
    tmp_path = f"{static_cfg.db_path}.backup-{int(datetime.utcnow().timestamp())}.db"
    await database.vacuum_into(tmp_path)
    try:
        await callback.bot.send_document(
            callback.from_user.id, FSInputFile(tmp_path), caption="💾 Резервная копия БД"
        )
        await database.mark_backup_done(datetime.utcnow())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    await callback.message.answer("✅ Бэкап отправлен.")


# --- reviews (⭐ Отзывы Avito) ------------------------------------------------


@admin_router.callback_query(F.data == "adm_reviews")
async def cb_admin_reviews(callback: CallbackQuery) -> None:
    await callback.answer()
    accounts = await database.list_avito_accounts(active_only=True)
    if not accounts:
        await callback.message.answer("Нет подключённых Avito-аккаунтов.")
        return
    if len(accounts) == 1:
        await _show_reviews(callback.message, accounts[0].id)
        return
    await callback.message.answer(
        "⭐ Выберите аккаунт Avito:", reply_markup=keyboards.avito_account_picker_kb(accounts, "adm_reviewsacc")
    )


@admin_router.callback_query(F.data.startswith("adm_reviewsacc_"))
async def cb_admin_reviews_account(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.rsplit("_", 1)[1])
    await _show_reviews(callback.message, account_id)


async def _show_reviews(message: Message, account_id: int) -> None:
    client = avito_client.get_pool().get(account_id)
    if client is None:
        await message.answer("⚠️ Аккаунт Avito недоступен.")
        return
    try:
        info = await client.get_rating_info()
        data = await client.get_reviews(limit=10)
    except avito_client.AvitoAPIError as exc:
        logger.exception("_show_reviews: failed for account %s", account_id)
        await message.answer(f"⚠️ Не удалось получить отзывы от Avito: {exc}")
        return

    rating = info.get("rating") or {}
    score = rating.get("score")
    count = rating.get("reviewsCount", 0)
    lines = [f"⭐ Рейтинг: {score if score is not None else '—'} ({count} отзывов)", ""]

    reviews = data.get("reviews") or []
    unanswered: list[tuple[int, str]] = []
    if not reviews:
        lines.append("Отзывов пока нет.")
    for review in reviews:
        sender_name = (review.get("sender") or {}).get("name", "Клиент")
        item_title = (review.get("item") or {}).get("title")
        text = review.get("text") or "(без текста)"
        review_score = review.get("score") or 0
        answer = review.get("answer")
        lines.append(f"👤 {html.escape(sender_name)} · {'⭐' * review_score}")
        if item_title:
            lines.append(f"📦 {html.escape(item_title)}")
        lines.append(html.escape(text))
        if answer:
            lines.append(f"↳ Ваш ответ: {html.escape(answer.get('text', ''))}")
        elif review.get("canAnswer"):
            unanswered.append((review["id"], sender_name))
        lines.append("")

    await message.answer(
        "\n".join(lines).strip(), reply_markup=keyboards.review_reply_kb(unanswered, account_id)
    )


@admin_router.callback_query(F.data.startswith("revans_"))
async def cb_review_answer_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    review_id_str, account_id_str = payload.split(":")
    await state.set_state(AdminStates.waiting_for_review_answer)
    await state.update_data(review_id=int(review_id_str), review_account_id=int(account_id_str))
    await callback.message.answer("✍️ Введите текст ответа на отзыв:", reply_markup=keyboards.cancel_kb())


@admin_router.message(AdminStates.waiting_for_review_answer, SafeFreeText())
async def receive_review_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    review_id = data.get("review_id")
    account_id = data.get("review_account_id")
    await state.clear()
    client = avito_client.get_pool().get(account_id) if account_id is not None else None
    if client is None or review_id is None:
        await message.answer("⚠️ Не удалось отправить ответ.")
        return
    try:
        await client.answer_review(review_id, message.text)
    except avito_client.AvitoAPIError as exc:
        logger.exception("receive_review_answer: failed for review %s", review_id)
        await message.answer(f"⚠️ Avito отклонил ответ на отзыв: {exc}")
        return
    await message.answer("✅ Ответ отправлен.")


# --- orders (📦 Заказы Avito, ПВЗ/Самовывоз) — open to every approved user,
# aggregated across every Avito account, filtered by the viewer's own point
# subscriptions (same rule as "📩 Непрочитанные"/"🕒 Недавние") --------------

@menu_router.message(F.text == constants.BTN_ORDERS, StateFilter("*"), ApprovedUser())
async def show_orders_menu(message: Message) -> None:
    await _show_all_orders(message, message.from_user.id)


async def _show_all_orders(message: Message, actor_id: int) -> None:
    accounts = await database.list_avito_accounts(active_only=True)
    if not accounts:
        await message.answer("Нет подключённых Avito-аккаунтов.")
        return

    actor = await database.get_user(actor_id)
    point_ids = await _point_ids_for_user(actor) if actor else set()

    shown: list[tuple[dict, int, str]] = []
    errors: list[str] = []
    for account in accounts:
        client = avito_client.get_pool().get(account.id)
        if client is None:
            continue
        try:
            orders = await client.get_orders(statuses=constants.ORDER_ACTIVE_STATUSES)
        except avito_client.AvitoAPIError as exc:
            logger.exception("_show_all_orders: failed for account %s", account.id)
            errors.append(f"{account.name}: {exc}")
            continue
        for order in orders:
            point_id = await database.resolve_order_point_id(order)
            if point_id in point_ids:
                shown.append((order, account.id, account.name))

    if not shown:
        text = "📦 Активных заказов нет."
        if errors:
            text += "\n\n⚠️ Не удалось получить заказы от:\n" + "\n".join(errors)
        await message.answer(text)
        return

    text = "📦 Ваши заказы:"
    if errors:
        text += "\n\n⚠️ Не удалось получить заказы от:\n" + "\n".join(errors)
    kb = keyboards.orders_menu_kb([(order, account_id) for order, account_id, _name in shown])
    await message.answer(text, reply_markup=kb)


async def _show_order_detail(message: Message, order_id: str, account_id: int) -> None:
    client = avito_client.get_pool().get(account_id)
    if client is None:
        await message.answer("⚠️ Аккаунт Avito недоступен.")
        return
    try:
        orders = await client.get_orders()
    except avito_client.AvitoAPIError as exc:
        logger.exception("_show_order_detail: failed for account %s", account_id)
        await message.answer(f"⚠️ Не удалось получить заказ от Avito: {exc}")
        return
    order = next((o for o in orders if str(o.get("id")) == str(order_id)), None)
    if order is None:
        await message.answer("⚠️ Заказ не найден (возможно, статус уже изменился).")
        return

    account = await database.get_avito_account(account_id)
    point_id = await database.resolve_order_point_id(order)
    point = await database.get_point(point_id) if point_id else None

    lines = []
    if point is not None:
        point_line = f"🏢 {html.escape(point.name)}"
        if point.address:
            point_line += f", {html.escape(point.address)}"
        lines.append(point_line)
    if account is not None:
        lines.append(f"📇 Кабинет: {html.escape(account.name)}")

    lines.append(f"🧾 Номер заказа: {order.get('marketplaceId') or order.get('id')}")

    delivery_info = order.get("delivery") or {}
    track_number = delivery_info.get("dispatchNumber") or delivery_info.get("trackingNumber")
    if track_number:
        lines.append(f"📮 Трек-номер: {html.escape(str(track_number))}")

    items = order.get("items") or []
    titles = ", ".join(html.escape(item.get("title", "")) for item in items) or "(без названия)"
    lines.append(f"📦 Товар: {titles}")

    status = order.get("status", "")
    lines.append(f"Статус: {constants.ORDER_STATUS_LABELS.get(status, status)}")

    prices = order.get("prices") or {}
    if prices.get("total") is not None:
        lines.append(f"💰 Сумма: {prices['total']} ₽")
    if prices.get("commission") is not None:
        lines.append(f"Комиссия: {prices['commission']} ₽")

    service = delivery_info.get("serviceName") or delivery_info.get("serviceType", "")
    if service:
        lines.append(f"🚚 Служба доставки: {html.escape(str(service))}")

    detail_text = "\n".join(lines)

    chat_short_id = None
    order_chat_id = ((order.get("items") or [{}])[0]).get("chatId")
    if order_chat_id:
        chat_short_id = await bot_cache.get_short_id(order_chat_id)

    kb = keyboards.order_detail_kb(order, account_id, chat_short_id)

    barcode_png = None
    if track_number:
        try:
            barcode_png = utils.generate_barcode_png(str(track_number))
        except Exception:
            logger.exception("_show_order_detail: barcode generation failed for %s", track_number)

    if barcode_png is None:
        await message.answer(detail_text, reply_markup=kb)
    elif len(detail_text) <= 1024:
        await message.answer_photo(
            BufferedInputFile(barcode_png, filename=f"{track_number}.png"), caption=detail_text, reply_markup=kb
        )
    else:
        await message.answer_photo(BufferedInputFile(barcode_png, filename=f"{track_number}.png"))
        await message.answer(detail_text, reply_markup=kb)


@crm_router.callback_query(F.data.startswith("ordview_"))
async def cb_order_view(callback: CallbackQuery) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    order_id, account_id_str = payload.split(":")
    await _show_order_detail(callback.message, order_id, int(account_id_str))


@crm_router.callback_query(F.data == "ordback")
async def cb_order_back(callback: CallbackQuery) -> None:
    await callback.answer()
    await _show_all_orders(callback.message, callback.from_user.id)


@crm_router.callback_query(F.data.startswith("ordact_"))
async def cb_order_action(callback: CallbackQuery) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    action, order_id, account_id_str = payload.split(":")
    client = avito_client.get_pool().get(int(account_id_str))
    if client is None:
        await callback.message.answer("⚠️ Аккаунт Avito недоступен.")
        return
    try:
        await client.apply_order_transition(order_id, action)
    except avito_client.AvitoAPIError as exc:
        logger.exception("cb_order_action: %s failed for order %s", action, order_id)
        await callback.message.answer(f"⚠️ Avito отклонил это действие: {exc}")
        return
    await callback.message.answer("✅ Готово.")
    await _show_order_detail(callback.message, order_id, int(account_id_str))


@crm_router.callback_query(F.data.startswith("ordmark_"))
async def cb_order_markings_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    order_id, account_id_str = payload.split(":")
    await state.set_state(AdminStates.waiting_for_order_markings)
    await state.update_data(order_id=order_id, order_account_id=int(account_id_str))
    await callback.message.answer(
        "🏷 Введите коды маркировки «Честный знак» через запятую:", reply_markup=keyboards.cancel_kb()
    )


@crm_router.message(AdminStates.waiting_for_order_markings, SafeFreeText())
async def receive_order_markings(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    account_id = data.get("order_account_id")
    await state.clear()
    client = avito_client.get_pool().get(account_id) if account_id is not None else None
    if client is None or order_id is None:
        await message.answer("⚠️ Не удалось передать маркировку.")
        return
    markings = [code.strip() for code in message.text.split(",") if code.strip()]
    try:
        orders = await client.get_orders()
        order = next((o for o in orders if o.get("id") == order_id), None)
        item_id = (order.get("items") or [{}])[0].get("avitoId") if order else None
        if item_id is None:
            raise avito_client.AvitoAPIError("no item found for order")
        await client.set_order_markings(item_id, order_id, markings)
    except avito_client.AvitoAPIError as exc:
        logger.exception("receive_order_markings: failed for order %s", order_id)
        await message.answer(f"⚠️ Avito отклонил маркировку: {exc}")
        return
    await message.answer("✅ Маркировка передана.")
    await _show_order_detail(message, order_id, account_id)


@crm_router.callback_query(F.data.startswith("ordcnc_"))
async def cb_order_cnc_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    order_id, account_id_str = payload.split(":")
    client = avito_client.get_pool().get(int(account_id_str))
    marketplace_id = None
    if client is not None:
        try:
            orders = await client.get_orders()
            order = next((o for o in orders if o.get("id") == order_id), None)
            marketplace_id = order.get("marketplaceId") if order else None
        except avito_client.AvitoAPIError:
            pass
    await state.set_state(AdminStates.waiting_for_cnc_address)
    await state.update_data(order_id=order_id, order_account_id=int(account_id_str), marketplace_id=marketplace_id)
    await callback.message.answer("📍 Введите адрес получения товара:", reply_markup=keyboards.cancel_kb())


@crm_router.message(AdminStates.waiting_for_cnc_address, SafeFreeText())
async def receive_cnc_address(message: Message, state: FSMContext) -> None:
    await state.update_data(cnc_address=message.text.strip())
    await state.set_state(AdminStates.waiting_for_cnc_period)
    await message.answer("Введите срок бронирования товара в днях:")


@crm_router.message(AdminStates.waiting_for_cnc_period, SafeFreeText())
async def receive_cnc_period(message: Message, state: FSMContext) -> None:
    try:
        period = int(message.text.strip())
    except ValueError:
        await message.answer("Введите целое число дней.")
        return
    await state.update_data(cnc_period=period)
    await state.set_state(AdminStates.waiting_for_cnc_comment)
    await message.answer("Комментарий для покупателя (или «-», чтобы пропустить):")


@crm_router.message(AdminStates.waiting_for_cnc_comment, SafeFreeText())
async def receive_cnc_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    order_id = data.get("order_id")
    account_id = data.get("order_account_id")
    marketplace_id = data.get("marketplace_id")
    client = avito_client.get_pool().get(account_id) if account_id is not None else None
    if client is None or order_id is None or marketplace_id is None:
        await message.answer("⚠️ Не удалось подготовить заказ (не найден marketplaceId).")
        return
    comment = message.text.strip()
    try:
        await client.set_cnc_order_details(
            order_id, marketplace_id, data["cnc_period"],
            address=data.get("cnc_address"), details=None if comment == "-" else comment,
        )
    except avito_client.AvitoAPIError as exc:
        logger.exception("receive_cnc_comment: failed for order %s", order_id)
        await message.answer(f"⚠️ Avito отклонил подготовку заказа: {exc}")
        return
    await message.answer("✅ Заказ подготовлен.")
    await _show_order_detail(message, order_id, account_id)


@crm_router.callback_query(F.data.startswith("ordcode_"))
async def cb_order_confirm_code_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    _, payload = callback.data.split("_", 1)
    order_id, account_id_str = payload.split(":")
    client = avito_client.get_pool().get(int(account_id_str))
    parcel_id = None
    if client is not None:
        try:
            orders = await client.get_orders()
            order = next((o for o in orders if o.get("id") == order_id), None)
            parcel_id = (order.get("delivery") or {}).get("dispatchNumber") if order else None
        except avito_client.AvitoAPIError:
            pass
    if parcel_id is None:
        await callback.message.answer("⚠️ Не удалось определить номер посылки для этого заказа.")
        return
    await state.set_state(AdminStates.waiting_for_order_confirm_code)
    await state.update_data(order_id=order_id, order_confirm_parcel_id=parcel_id, order_account_id=int(account_id_str))
    await callback.message.answer(
        "✅ Введите код, который назвал покупатель при получении:", reply_markup=keyboards.cancel_kb()
    )


@crm_router.message(AdminStates.waiting_for_order_confirm_code, SafeFreeText())
async def receive_order_confirm_code(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    parcel_id = data.get("order_confirm_parcel_id")
    account_id = data.get("order_account_id")
    await state.clear()
    client = avito_client.get_pool().get(account_id) if account_id is not None else None
    if client is None or parcel_id is None:
        await message.answer("⚠️ Не удалось проверить код.")
        return
    try:
        await client.check_confirmation_code(parcel_id, message.text.strip())
    except avito_client.AvitoAPIError as exc:
        logger.exception("receive_order_confirm_code: failed")
        await message.answer(f"⚠️ Код не подошёл: {exc}")
        return
    await message.answer("✅ Код подтверждён, заказ можно выдавать.")
    if order_id is not None and account_id is not None:
        await _show_order_detail(message, order_id, account_id)
